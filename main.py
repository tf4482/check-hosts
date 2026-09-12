#!/usr/bin/env python3

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from ipaddress import IPv4Network, IPv6Network, ip_address, ip_network
from pathlib import Path
from time import perf_counter
from typing import Any, TextIO

import psycopg2
from psycopg2.extras import execute_values

from utils_python.config_loader import load_config as load_json_config

APP_NAME = "check-hosts"
CONFIG_FILENAME = "config.json"
ANSI_COLORS = {
    "blue": "\033[1;94m",
    "cyan": "\033[1;96m",
    "green": "\033[1;92m",
    "grey": "\033[1;90m",
    "magenta": "\033[1;95m",
    "red": "\033[1;91m",
    "white": "\033[1;97m",
    "yellow": "\033[1;93m",
}
ANSI_RESET = "\033[0m"
CONFIG_DEFAULTS: dict[str, Any] = {
    "database": {
        "host": "127.0.0.1",
        "port": 5432,
        "name": "host_status",
        "user": "status_user",
        "password": "change-me",
    },
    "settings": {
        "ping_concurrency": 20,
        "ping_timeout_seconds": 1,
        "vpn_container": "wireguard",
        "ssh_concurrency": 4,
        "ssh_timeout_seconds": 3,
        "ssh_check_mode": "login",
        "allowed_networks": [],
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
    duration_seconds: float = 0.0
    detail: str | None = None


@dataclass(frozen=True)
class VpnBatchResult:
    reachable: frozenset[str]
    duration_seconds: float
    detail: str | None = None


def supports_color(stream: TextIO | None = None) -> bool:
    output = stream or sys.stdout
    return (
        "NO_COLOR" not in os.environ
        and os.environ.get("TERM") != "dumb"
        and output.isatty()
    )


def colored(text: str, color: str, *, enabled: bool) -> str:
    if not enabled:
        return text
    return f"{ANSI_COLORS[color]}{text}{ANSI_RESET}"


def print_message(
    icon: str,
    message: str,
    color: str = "white",
    *,
    stream: TextIO | None = None,
) -> None:
    output = stream or sys.stdout
    print(
        colored(f"{icon}  {message}", color, enabled=supports_color(output)),
        file=output,
    )


def print_header(*, stream: TextIO | None = None) -> None:
    output = stream or sys.stdout
    color = supports_color(output)
    print(file=output)
    print(colored("╭──────────────────────────────╮", "cyan", enabled=color), file=output)
    print(colored("│  🌐 Check Hosts — starting   │", "cyan", enabled=color), file=output)
    print(colored("╰──────────────────────────────╯", "cyan", enabled=color), file=output)


def print_check_plan(
    config: dict[str, Any],
    run_ping: bool,
    run_ssh: bool,
    *,
    stream: TextIO | None = None,
) -> None:
    output = stream or sys.stdout
    settings = config["settings"]
    print_message("⚙️", "Configuration loaded", "green", stream=output)
    if run_ping:
        print_message(
            "📡",
            f"Ping: {len(config['ping_hosts'])} hosts, "
            f"concurrency {int(settings.get('ping_concurrency', 20))}, "
            f"timeout {int(settings.get('ping_timeout_seconds', 1))}s",
            "blue",
            stream=output,
        )
    if run_ssh:
        print_message(
            "🔐",
            f"SSH: {len(config['ssh_hosts'])} hosts, "
            f"mode {str(settings.get('ssh_check_mode', 'login')).lower()}, "
            f"concurrency {int(settings.get('ssh_concurrency', 4))}, "
            f"timeout {int(settings.get('ssh_timeout_seconds', 3))}s",
            "magenta",
            stream=output,
        )

    allowed = settings.get("allowed_networks", [])
    validation = ", ".join(str(network) for network in allowed) if allowed else "disabled"
    print_message("🛡️", f"Network validation: {validation}", "cyan", stream=output)


def print_summary(
    results: Iterable[HostStatus],
    duration_seconds: float,
    *,
    stream: TextIO | None = None,
) -> None:
    output = stream or sys.stdout
    statuses = list(results)
    online = sum(result.online for result in statuses)
    offline = len(statuses) - online
    color = "green" if offline == 0 else "yellow"
    icon = "🎉" if offline == 0 else "⚠️"
    print(file=output)
    print_message(
        icon,
        f"Completed in {duration_seconds:.2f}s — "
        f"{online} online, {offline} offline, {len(statuses)} checked",
        color,
        stream=output,
    )


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


def parse_allowed_networks(
    settings: dict[str, Any],
) -> tuple[IPv4Network | IPv6Network, ...]:
    values = settings.get("allowed_networks", [])
    if not isinstance(values, list):
        raise ValueError("settings.allowed_networks must be a list of CIDR networks")

    try:
        return tuple(ip_network(str(value), strict=False) for value in values)
    except ValueError as error:
        raise ValueError(
            f"Invalid network in settings.allowed_networks: {error}"
        ) from error


def validate_host_networks(
    hosts: Iterable[Host],
    networks: tuple[IPv4Network | IPv6Network, ...],
    section: str,
) -> None:
    """Reject literal IP addresses outside configured networks; permit DNS names."""
    if not networks:
        return

    for host in hosts:
        addresses = (("address", host.address), ("vpn_address", host.vpn_address))
        for field, value in addresses:
            if value is None:
                continue
            try:
                parsed = ip_address(value)
            except ValueError:
                continue
            if not any(parsed in network for network in networks):
                allowed = ", ".join(str(network) for network in networks)
                raise ValueError(
                    f"{section} host {host.name!r} has {field} {value!r} outside "
                    f"allowed networks: {allowed}"
                )


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
) -> HostStatus:
    started = perf_counter()
    async with semaphore:
        online = await command_succeeds(
            "ping", "-n", "-c", "1", "-W", str(timeout), host.address
        )

    return HostStatus(
        host=host,
        online=online,
        duration_seconds=perf_counter() - started,
        detail=None if online else "primary ping failed",
    )


async def check_vpn_ping_hosts(
    hosts: list[Host], timeout: int, vpn_container: str
) -> VpnBatchResult:
    addresses = list(
        dict.fromkeys(host.vpn_address for host in hosts if host.vpn_address)
    )
    if not addresses:
        return VpnBatchResult(frozenset(), 0.0)

    # Positional parameters keep configured addresses out of the shell program.
    script = """
timeout=$1
shift
for address do
    (ping -n -c 1 -W "$timeout" "$address" >/dev/null 2>&1 && printf '%s\\n' "$address"; true) &
done
wait
""".strip()
    started = perf_counter()
    try:
        process = await asyncio.create_subprocess_exec(
            "docker",
            "exec",
            vpn_container,
            "sh",
            "-c",
            script,
            "check-hosts",
            str(timeout),
            *addresses,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as error:
        return VpnBatchResult(
            frozenset(), perf_counter() - started, f"VPN check unavailable: {error}"
        )

    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=timeout + 5
        )
    except TimeoutError:
        process.kill()
        await process.communicate()
        return VpnBatchResult(
            frozenset(), perf_counter() - started, "VPN ping batch timed out"
        )

    duration = perf_counter() - started
    if process.returncode != 0:
        message = stderr.decode(errors="replace").strip().splitlines()
        detail = message[-1] if message else f"docker exec exited {process.returncode}"
        return VpnBatchResult(frozenset(), duration, f"VPN check failed: {detail}")

    reachable = frozenset(stdout.decode(errors="replace").splitlines())
    return VpnBatchResult(reachable, duration)


async def check_ping_hosts(
    hosts: list[Host], concurrency: int, timeout: int, vpn_container: str
) -> list[HostStatus]:
    semaphore = asyncio.Semaphore(concurrency)
    primary_results = list(
        await asyncio.gather(
            *(check_ping_host(host, semaphore, timeout) for host in hosts)
        )
    )
    fallback_hosts = [
        result.host
        for result in primary_results
        if not result.online and result.host.vpn_address
    ]
    if not fallback_hosts:
        return primary_results

    vpn_result = await check_vpn_ping_hosts(fallback_hosts, timeout, vpn_container)
    combined = []
    for result in primary_results:
        vpn_address = result.host.vpn_address
        if result.online or vpn_address is None:
            combined.append(result)
            continue

        online = vpn_address in vpn_result.reachable
        combined.append(
            HostStatus(
                host=result.host,
                online=online,
                duration_seconds=(
                    result.duration_seconds + vpn_result.duration_seconds
                ),
                detail=(
                    f"VPN ping replied via {vpn_address}"
                    if online
                    else vpn_result.detail or "primary and VPN ping failed"
                ),
            )
        )
    return combined


def ssh_failure_detail(stderr: bytes, return_code: int) -> str:
    lines = [line.strip() for line in stderr.decode(errors="replace").splitlines()]
    return next(
        (line for line in reversed(lines) if line),
        f"ssh exited with status {return_code}",
    )


async def check_ssh_tcp_host(
    host: Host, semaphore: asyncio.Semaphore, timeout: int
) -> HostStatus:
    started = perf_counter()
    writer: asyncio.StreamWriter | None = None
    try:
        async with semaphore:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host.address, host.ssh_port), timeout=timeout
            )
        return HostStatus(host, True, perf_counter() - started)
    except TimeoutError:
        detail = "TCP connection timed out"
    except OSError as error:
        detail = f"TCP connection failed: {error}"
    finally:
        if writer is not None:
            writer.close()
            await writer.wait_closed()

    return HostStatus(host, False, perf_counter() - started, detail)


async def check_ssh_host(
    host: Host, semaphore: asyncio.Semaphore, timeout: int
) -> HostStatus:
    destination = f"{host.ssh_user}@{host.address}" if host.ssh_user else host.address
    started = perf_counter()

    async with semaphore:
        try:
            process = await asyncio.create_subprocess_exec(
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                f"ConnectTimeout={timeout}",
                "-o",
                "ConnectionAttempts=1",
                "-p",
                str(host.ssh_port),
                destination,
                "true",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as error:
            return HostStatus(
                host, False, perf_counter() - started, f"SSH unavailable: {error}"
            )

        try:
            _, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout + 2
            )
        except TimeoutError:
            process.kill()
            await process.communicate()
            return HostStatus(
                host, False, perf_counter() - started, "SSH process timed out"
            )

    return_code = process.returncode or 0
    return HostStatus(
        host=host,
        online=return_code == 0,
        duration_seconds=perf_counter() - started,
        detail=None if return_code == 0 else ssh_failure_detail(stderr, return_code),
    )


async def check_ssh_hosts(
    hosts: list[Host], concurrency: int, timeout: int, mode: str = "login"
) -> list[HostStatus]:
    if mode not in {"login", "tcp"}:
        raise ValueError("settings.ssh_check_mode must be 'login' or 'tcp'")

    semaphore = asyncio.Semaphore(concurrency)
    check = check_ssh_host if mode == "login" else check_ssh_tcp_host
    return list(
        await asyncio.gather(
            *(check(host, semaphore, timeout) for host in hosts)
        )
    )


async def run_checks(
    config: dict[str, Any], *, run_ping: bool, run_ssh: bool
) -> list[HostStatus]:
    settings = config["settings"]
    allowed_networks = parse_allowed_networks(settings)
    checks = []

    if run_ping:
        ping_hosts = load_hosts(config, "ping_hosts")
        validate_host_networks(ping_hosts, allowed_networks, "ping_hosts")
        checks.append(
            check_ping_hosts(
                ping_hosts,
                concurrency=int(settings.get("ping_concurrency", 20)),
                timeout=int(settings.get("ping_timeout_seconds", 1)),
                vpn_container=str(settings.get("vpn_container", "wireguard")),
            )
        )

    if run_ssh:
        ssh_hosts = load_hosts(config, "ssh_hosts")
        validate_host_networks(ssh_hosts, allowed_networks, "ssh_hosts")
        checks.append(
            check_ssh_hosts(
                ssh_hosts,
                concurrency=int(settings.get("ssh_concurrency", 4)),
                timeout=int(settings.get("ssh_timeout_seconds", 3)),
                mode=str(settings.get("ssh_check_mode", "login")).lower(),
            )
        )

    groups = await asyncio.gather(*checks)
    return [result for group in groups for result in group]


def update_statuses(config: dict[str, Any], results: Iterable[HostStatus]) -> None:
    checked_at = datetime.now()  # noqa: DTZ005
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
        execute_values(
            cursor,
            """
                UPDATE hosts AS target
                   SET status = source.status,
                       last_check = source.checked_at
                  FROM (VALUES %s) AS source(status, checked_at, name, os)
                 WHERE target.name = source.name
                   AND target.os = source.os
                """,
            rows,
            page_size=len(rows),
        )


def print_results(
    results: Iterable[HostStatus], *, stream: TextIO | None = None
) -> None:
    output = stream or sys.stdout
    color = supports_color(output)
    for result in results:
        status = "online" if result.online else "offline"
        detail = f", {result.detail}" if result.detail else ""
        icon = "✅" if result.online else "❌"
        status_color = "green" if result.online else "red"
        name = colored(result.host.name, "white", enabled=color)
        status_text = colored(status, status_color, enabled=color)
        metadata = colored(
            f"({result.duration_seconds:.2f}s{detail})", "grey", enabled=color
        )
        print(f"  {icon} {name}: {status_text} {metadata}", file=output)


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
    started = perf_counter()
    args = parse_args()
    print_header()
    print_message("📂", "Loading configuration…", "blue")
    config = load_config(args.config)
    run_ping, run_ssh = select_checks(args.ping, args.ssh)
    print_check_plan(config, run_ping, run_ssh)
    print_message("🚀", "Running host checks…", "cyan")
    results = asyncio.run(
        run_checks(config, run_ping=run_ping, run_ssh=run_ssh)
    )

    print()
    print_message("📋", "Host results", "white")
    print_results(results)

    database = config["database"]
    print()
    print_message(
        "💾",
        f"Updating PostgreSQL at {database.get('host')}:{database.get('port')} "
        f"/{database.get('name')}…",
        "blue",
    )
    update_statuses(config, results)
    print_message("✅", "Database statuses updated", "green")
    print_summary(results, perf_counter() - started)


if __name__ == "__main__":
    main()
