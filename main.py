#!/usr/bin/env python3

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from ipaddress import IPv4Network, IPv6Network, ip_address, ip_network
from pathlib import Path
from time import perf_counter
from typing import Any, TextIO

from utils_python.check_config import is_filled
from utils_python.config_loader import load_config as load_json_config
from utils_python.config_loader import find_config_path
from utils_python.copy_files import copy_files
from utils_python.create_config import parse_key_value_list
from utils_python.dpkg_check import is_package_installed
from utils_python.filecheck import filecheck
from utils_python.select_file import select_file

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
        "suffix": "example.com",
        "ping_concurrency": 20,
        "ping_timeout_seconds": 1,
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
    ssh_user: str | None = None
    ssh_port: int = 22


@dataclass(frozen=True)
class HostStatus:
    host: Host
    online: bool
    duration_seconds: float = 0.0
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
    normalized_color = color[1:] if color.startswith("l") else color
    return f"{ANSI_COLORS[normalized_color]}{text}{ANSI_RESET}"


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
    if not is_filled(config["settings"].get("suffix")):
        raise ValueError("Missing required setting: settings.suffix")
    return config


def parse_override_value(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def merge_config(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_config(merged[key], value)
        else:
            merged[key] = value
    return merged


def apply_config_overrides(
    config: dict[str, Any], override_args: list[str]
) -> dict[str, Any]:
    if not override_args:
        return config
    invalid = [
        item
        for item in override_args
        if "=" not in item or not item.partition("=")[0].strip().strip(".")
    ]
    if invalid:
        raise ValueError(
            "Configuration overrides must use a non-empty dotted KEY=JSON_VALUE: "
            + ", ".join(invalid)
        )
    overrides = parse_key_value_list(override_args, value_parser=parse_override_value)
    return merge_config(config, overrides)


def choose_config(directory: Path) -> Path:
    selected = select_file(str(directory), ".json")
    if selected is None:
        raise SystemExit("Configuration selection cancelled")
    if not filecheck(selected):
        raise FileNotFoundError(f"Selected configuration file not found: {selected}")
    return Path(selected)


def active_config_path(explicit_path: Path | None) -> Path | None:
    if explicit_path is not None:
        return explicit_path
    return find_config_path(APP_NAME, CONFIG_FILENAME, local_dir=Path.cwd())


def backup_config(config_path: Path, target_directory: Path) -> Path:
    source = config_path.expanduser().resolve()
    target = target_directory.expanduser().resolve()
    destination = target / source.name
    if source == destination:
        raise ValueError("Configuration backup destination equals the source file")
    if not filecheck(str(source)):
        raise FileNotFoundError(f"Configuration file not found: {source}")

    copy_files(str(source.parent), str(target), [source.name])
    return destination


def run_doctor(*, stream: TextIO | None = None) -> bool:
    output = stream or sys.stdout
    checks = (("ping", ("iputils-ping",)), ("ssh", ("openssh-client",)))
    has_dpkg = shutil.which("dpkg") is not None
    healthy = True

    print_message("🩺", "Runtime dependency check", "cyan", stream=output)
    for command, packages in checks:
        executable = shutil.which(command)
        available = executable is not None
        healthy = healthy and available
        print_message(
            "✅" if available else "❌",
            f"{command}: {'available at ' + executable if executable else 'not found'} "
            "(required)",
            "green" if available else "red",
            stream=output,
        )
        if has_dpkg:
            installed = [package for package in packages if is_package_installed(package)]
            package_status = ", ".join(installed) if installed else "not installed via dpkg"
            print_message("📦", f"{command} package: {package_status}", "grey", stream=output)

    if not has_dpkg:
        print_message(
            "ℹ️",
            "dpkg is unavailable; package-level checks were skipped",
            "grey",
            stream=output,
        )
    return healthy


def load_hosts(config: dict[str, Any], section: str) -> list[Host]:
    suffix = str(config["settings"].get("suffix")).strip().lstrip(".")
    hosts = []
    for item in config[section]:
        required = {"name", "os"}
        missing = {key for key in required if key not in item or not is_filled(item[key])}
        if missing:
            raise ValueError(
                f"Host in {section} is missing: {', '.join(sorted(missing))}"
            )
        address = item.get("address")
        if not is_filled(address):
            address = f"{str(item['name']).strip()}.{suffix}"
        hosts.append(
            Host(
                name=str(item["name"]),
                address=str(address),
                os=str(item["os"]),
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
        try:
            parsed = ip_address(host.address)
        except ValueError:
            continue
        if not any(parsed in network for network in networks):
            allowed = ", ".join(str(network) for network in networks)
            raise ValueError(
                f"{section} host {host.name!r} has address {host.address!r} outside "
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


async def check_ping_hosts(
    hosts: list[Host], concurrency: int, timeout: int
) -> list[HostStatus]:
    semaphore = asyncio.Semaphore(concurrency)
    return list(
        await asyncio.gather(
            *(check_ping_host(host, semaphore, timeout) for host in hosts)
        )
    )


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
    import psycopg2
    from psycopg2.extras import execute_values

    checked_at = datetime.now()  # noqa: DTZ005
    rows_by_name = {
        result.host.name: (
            "online" if result.online else "offline",
            checked_at,
            result.host.name,
            result.host.os,
        )
        for result in results
    }
    rows = list(rows_by_name.values())

    database = config["database"]
    required = {"host", "port", "name", "user", "password"}
    missing = {
        key for key in required if key not in database or not is_filled(database[key])
    }
    if missing:
        raise ValueError(f"Missing database settings: {', '.join(sorted(missing))}")

    with psycopg2.connect(
        host=database["host"],
        port=database["port"],
        dbname=database["name"],
        user=database["user"],
        password=database["password"],
    ) as connection, connection.cursor() as cursor:
        if not rows:
            cursor.execute("DELETE FROM hosts")
            return

        execute_values(
            cursor,
            """
                WITH source(status, checked_at, name, os) AS (VALUES %s),
                removed AS (
                    DELETE FROM hosts AS target
                     WHERE NOT EXISTS (
                        SELECT 1
                          FROM source
                         WHERE source.name = target.name
                     )
                ),
                updated AS (
                    UPDATE hosts AS target
                       SET status = source.status,
                           last_check = source.checked_at,
                           os = source.os
                      FROM source
                     WHERE target.name = source.name
                )
                INSERT INTO hosts (status, last_check, name, os)
                SELECT source.status, source.checked_at, source.name, source.os
                  FROM source
                 WHERE NOT EXISTS (
                    SELECT 1
                      FROM hosts AS target
                     WHERE target.name = source.name
                 )
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
    config_source = parser.add_mutually_exclusive_group()
    config_source.add_argument(
        "--config",
        type=Path,
        help=(
            "exact JSON configuration path; otherwise search ./config.json and "
            "~/.config/check-hosts/config.json"
        ),
    )
    config_source.add_argument(
        "--select-config",
        type=Path,
        metavar="DIRECTORY",
        help="interactively select a JSON configuration below DIRECTORY",
    )
    parser.add_argument(
        "--set",
        dest="overrides",
        action="append",
        default=[],
        metavar="KEY=JSON_VALUE",
        help="override one nested configuration value in memory; repeat as needed",
    )
    parser.add_argument(
        "--backup-config",
        type=Path,
        metavar="DIRECTORY",
        help="copy the selected configuration into DIRECTORY before checks",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="check required runtime executables, then exit",
    )
    return parser.parse_args()


def main() -> None:
    started = perf_counter()
    args = parse_args()
    print_header()
    if args.doctor:
        raise SystemExit(0 if run_doctor() else 1)

    config_path = choose_config(args.select_config) if args.select_config else args.config
    print_message("📂", "Loading configuration…", "blue")
    config = load_config(config_path)
    config = apply_config_overrides(config, args.overrides)
    if args.overrides:
        print_message(
            "🧩",
            f"Applied {len(args.overrides)} in-memory configuration override(s)",
            "magenta",
        )

    if args.backup_config:
        source_path = active_config_path(config_path)
        if source_path is None:
            raise FileNotFoundError("Cannot locate the loaded configuration for backup")
        destination = backup_config(source_path, args.backup_config)
        print_message("📦", f"Configuration backed up to {destination}", "green")

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
