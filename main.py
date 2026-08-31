#!/usr/bin/env python3

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg2
from psycopg2.extras import execute_batch

from utils_python.config_loader import load_config as load_json_config

APP_NAME = "check-hosts"
CONFIG_FILENAME = "config.json"
CONFIG_DEFAULTS: dict[str, Any] = {
    "database": {
        "host": "127.0.0.1",
        "port": 5432,
        "name": "host_status",
        "user": "status_user",
        "password": "change-me",
    },
    "settings": {
        "ping_concurrency": 8,
        "ping_timeout_seconds": 1,
        "vpn_container": "wireguard",
        "ssh_concurrency": 4,
        "ssh_timeout_seconds": 10,
    },
    "ping_hosts": [],
    "ssh_hosts": [],
}

@dataclass(frozen=True)
class Host:
    name: str
    address: str
    os: str
    vpn_address: str | None = None
    ssh_user: str | None = None
    ssh_port: int = 22


@dataclass(frozen=True)
class HostStatus:
    host: Host
    online: bool


def load_config(path: Path | None = None) -> dict[str, Any]:
    config = load_json_config(
        app_name=APP_NAME,
        config_filename=CONFIG_FILENAME,
        defaults=CONFIG_DEFAULTS,
        config_path=path,
        local_dir=Path.cwd(),
    )

    required = {"database", "settings", "ping_hosts", "ssh_hosts"}
    missing = required.difference(config)
    if missing:
        raise ValueError(f"Missing configuration sections: {', '.join(sorted(missing))}")
    return config


def load_hosts(config: dict[str, Any], section: str) -> list[Host]:
    hosts = []
    for item in config[section]:
        required = {"name", "address", "os"}
        missing = required.difference(item)
        if missing:
            raise ValueError(
                f"Host in {section} is missing: {', '.join(sorted(missing))}"
            )
        hosts.append(
            Host(
                name=str(item["name"]),
                address=str(item["address"]),
                os=str(item["os"]),
                vpn_address=(
                    str(item["vpn_address"]) if item.get("vpn_address") else None
                ),
                ssh_user=str(item["ssh_user"]) if item.get("ssh_user") else None,
                ssh_port=int(item.get("ssh_port", 22)),
            )
        )
    return hosts


async def command_succeeds(*command: str) -> bool:
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return False
    return await process.wait() == 0


async def check_ping_host(
    host: Host,
    semaphore: asyncio.Semaphore,
    timeout: int,
    vpn_container: str,
) -> HostStatus:
    async with semaphore:
        online = await command_succeeds(
            "ping", "-n", "-c", "1", "-W", str(timeout), host.address
        )

        if not online and host.vpn_address:
            online = await command_succeeds(
                "docker",
                "exec",
                vpn_container,
                "ping",
                "-c",
                "1",
                "-W",
                str(timeout),
                host.vpn_address,
            )

        return HostStatus(host=host, online=online)


async def check_ping_hosts(
    hosts: list[Host], concurrency: int, timeout: int, vpn_container: str
) -> list[HostStatus]:
    semaphore = asyncio.Semaphore(concurrency)
    return list(
        await asyncio.gather(
            *(
                check_ping_host(host, semaphore, timeout, vpn_container)
                for host in hosts
            )
        )
    )


async def check_ssh_host(
    host: Host, semaphore: asyncio.Semaphore, timeout: int
) -> HostStatus:
    destination = f"{host.ssh_user}@{host.address}" if host.ssh_user else host.address

    async with semaphore:
        try:
            process = await asyncio.create_subprocess_exec(
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                f"ConnectTimeout={timeout}",
                "-p",
                str(host.ssh_port),
                destination,
                "true",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            return HostStatus(host=host, online=False)

        try:
            return_code = await asyncio.wait_for(process.wait(), timeout=timeout + 2)
        except TimeoutError:
            process.kill()
            await process.wait()
            return_code = 1

    return HostStatus(host=host, online=return_code == 0)


async def check_ssh_hosts(
    hosts: list[Host], concurrency: int, timeout: int
) -> list[HostStatus]:
    semaphore = asyncio.Semaphore(concurrency)
    return list(
        await asyncio.gather(
            *(check_ssh_host(host, semaphore, timeout) for host in hosts)
        )
    )


async def run_checks(
    config: dict[str, Any], *, run_ping: bool, run_ssh: bool
) -> list[HostStatus]:
    settings = config["settings"]
    checks = []

    if run_ping:
        checks.append(
            check_ping_hosts(
                load_hosts(config, "ping_hosts"),
                concurrency=int(settings.get("ping_concurrency", 16)),
                timeout=int(settings.get("ping_timeout_seconds", 1)),
                vpn_container=str(settings.get("vpn_container", "wg-easy")),
            )
        )

    if run_ssh:
        checks.append(
            check_ssh_hosts(
                load_hosts(config, "ssh_hosts"),
                concurrency=int(settings.get("ssh_concurrency", 8)),
                timeout=int(settings.get("ssh_timeout_seconds", 10)),
            )
        )

    groups = await asyncio.gather(*checks)
    return [result for group in groups for result in group]


def update_statuses(config: dict[str, Any], results: Iterable[HostStatus]) -> None:
    checked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")  # noqa: DTZ005
    rows = [
        (
            "online" if result.online else "offline",
            checked_at,
            result.host.name,
            result.host.os,
        )
        for result in results
    ]
    if not rows:
        return

    database = config["database"]
    required = {"host", "port", "name", "user", "password"}
    missing = required.difference(database)
    if missing:
        raise ValueError(f"Missing database settings: {', '.join(sorted(missing))}")

    with psycopg2.connect(
        host=database["host"],
        port=database["port"],
        dbname=database["name"],
        user=database["user"],
        password=database["password"],
    ) as connection, connection.cursor() as cursor:
        execute_batch(
            cursor,
            """
                UPDATE hosts
                   SET status = %s,
                       last_check = %s
                 WHERE name = %s
                   AND os = %s
                """,
            rows,
        )


def print_results(results: Iterable[HostStatus]) -> None:
    for result in results:
        status = "online" if result.online else "offline"
        print(f"{result.host.name}: {status}")


def select_checks(ping: bool, ssh: bool) -> tuple[bool, bool]:
    """Return ping/SSH selections, defaulting to both when neither is supplied."""
    return ping or not ssh, ssh or not ping


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check hosts with ICMP ping, SSH, or both"
    )
    parser.add_argument(
        "--ping",
        action="store_true",
        help="run only ICMP ping checks unless --ssh is also supplied",
    )
    parser.add_argument(
        "--ssh",
        action="store_true",
        help="run only SSH checks unless --ping is also supplied",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help=(
            "exact JSON configuration path; otherwise search ./config.json and "
            "~/.config/check-hosts/config.json"
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    run_ping, run_ssh = select_checks(args.ping, args.ssh)
    results = asyncio.run(
        run_checks(config, run_ping=run_ping, run_ssh=run_ssh)
    )
    update_statuses(config, results)
    print_results(results)


if __name__ == "__main__":
    main()
