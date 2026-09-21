from __future__ import annotations

import asyncio
import importlib
import json
import stat
import sys
import tempfile
import types
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import yaml

from utils_python.config_loader import load_config
from utils_python.list_files import list_files


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def write_yaml(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


class ConfigLoaderTests(unittest.TestCase):
    def test_explicit_path_is_loaded_without_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "custom.json"
            write_json(config_path, {"source": "explicit"})

            config = load_config(
                "example", "config.json", {}, config_path=config_path
            )

        self.assertEqual(config, {"source": "explicit"})

    def test_missing_explicit_path_raises_file_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.json"

            with self.assertRaises(FileNotFoundError):
                load_config("example", "config.json", {}, config_path=missing)

    def test_local_config_takes_precedence_over_user_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local_dir = root / "local"
            home = root / "home"
            write_json(local_dir / "config.json", {"source": "local"})
            write_json(
                home / ".config" / "example" / "config.json",
                {"source": "user"},
            )

            with patch("utils_python.config_loader.Path.home", return_value=home):
                config = load_config(
                    "example", "config.json", {}, local_dir=local_dir
                )

        self.assertEqual(config, {"source": "local"})

    def test_user_config_is_used_when_local_config_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local_dir = root / "local"
            home = root / "home"
            write_json(
                home / ".config" / "example" / "config.json",
                {"source": "user"},
            )

            with patch("utils_python.config_loader.Path.home", return_value=home):
                config = load_config(
                    "example", "config.json", {}, local_dir=local_dir
                )

        self.assertEqual(config, {"source": "user"})

    def test_missing_config_creates_private_placeholder_and_exits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local_dir = root / "local"
            home = root / "home"
            defaults = {"database": {"password": "change-me"}}
            expected_path = home / ".config" / "example" / "config.json"

            with (
                patch("utils_python.config_loader.Path.home", return_value=home),
                self.assertRaises(SystemExit) as context,
            ):
                load_config(
                    "example", "config.json", defaults, local_dir=local_dir
                )

            self.assertEqual(context.exception.code, 1)
            self.assertEqual(
                json.loads(expected_path.read_text(encoding="utf-8")), defaults
            )
            self.assertEqual(stat.S_IMODE(expected_path.stat().st_mode), 0o600)

    def test_explicit_yaml_path_is_loaded_without_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "custom.yml"
            write_yaml(config_path, {"source": "yaml", "enabled": True})

            config = load_config(
                "example", "config.yml", {}, config_path=config_path
            )

        self.assertEqual(config, {"source": "yaml", "enabled": True})

    def test_missing_yaml_config_creates_private_yaml_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            defaults = {"settings": {"suffix": "example.test"}}
            expected_path = home / ".config" / "example" / "config.yml"

            with (
                patch("utils_python.config_loader.Path.home", return_value=home),
                self.assertRaises(SystemExit),
            ):
                load_config("example", "config.yml", defaults, local_dir=root / "local")

            self.assertEqual(
                yaml.safe_load(expected_path.read_text(encoding="utf-8")), defaults
            )
            self.assertEqual(stat.S_IMODE(expected_path.stat().st_mode), 0o600)


class ProjectConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        psycopg2 = types.ModuleType("psycopg2")
        psycopg2.connect = lambda **kwargs: None
        extras = types.ModuleType("psycopg2.extras")
        extras.execute_values = lambda *args: None
        psycopg2.extras = extras
        cls.module_patcher = patch.dict(
            sys.modules,
            {"psycopg2": psycopg2, "psycopg2.extras": extras},
        )
        cls.module_patcher.start()
        cls.main = importlib.import_module("main")

    @classmethod
    def tearDownClass(cls) -> None:
        sys.modules.pop("main", None)
        cls.module_patcher.stop()

    def test_project_loader_accepts_required_sections(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yml"
            expected = {
                "database": {},
                "settings": {"suffix": "example.test"},
                "ping_hosts": [],
                "ssh_hosts": [],
            }
            write_yaml(config_path, expected)

            config = self.main.load_config(config_path)

        self.assertEqual(config, expected)

    def test_project_loader_rejects_missing_sections(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yml"
            write_yaml(config_path, {"settings": {}})

            with self.assertRaisesRegex(ValueError, "Missing configuration sections"):
                self.main.load_config(config_path)

    def test_project_loader_rejects_missing_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yml"
            write_yaml(
                config_path,
                {
                    "database": {},
                    "settings": {},
                    "ping_hosts": [],
                    "ssh_hosts": [],
                },
            )

            with self.assertRaisesRegex(ValueError, "settings.suffix"):
                self.main.load_config(config_path)

    def test_load_hosts_rejects_blank_required_values(self) -> None:
        config = {
            "settings": {"suffix": "example.test"},
            "ssh_hosts": [{"name": "  ", "address": "192.0.2.1", "os": "Linux"}]
        }

        with self.assertRaisesRegex(ValueError, "missing: name"):
            self.main.load_hosts(config, "ssh_hosts")

    def test_load_hosts_uses_name_and_suffix_when_address_is_omitted(self) -> None:
        config = {
            "settings": {"suffix": ".example.test"},
            "ping_hosts": [{"name": "workstation-01", "os": "Linux"}],
        }

        hosts = self.main.load_hosts(config, "ping_hosts")

        self.assertEqual(hosts[0].address, "workstation-01.example.test")

    def test_load_hosts_keeps_an_explicit_address(self) -> None:
        config = {
            "settings": {"suffix": "example.test"},
            "ssh_hosts": [
                {"name": "server-01", "address": "192.0.2.10", "os": "Linux"}
            ],
        }

        hosts = self.main.load_hosts(config, "ssh_hosts")

        self.assertEqual(hosts[0].address, "192.0.2.10")

    def test_protocol_flag_selection(self) -> None:
        cases = {
            (False, False): (True, True),
            (True, False): (True, False),
            (False, True): (False, True),
            (True, True): (True, True),
        }

        for flags, expected in cases.items():
            with self.subTest(flags=flags):
                self.assertEqual(self.main.select_checks(*flags), expected)

    def test_protocol_flags_are_parsed(self) -> None:
        cases = {
            ("main.py",): (False, False),
            ("main.py", "--ping"): (True, False),
            ("main.py", "--ssh"): (False, True),
            ("main.py", "--ping", "--ssh"): (True, True),
        }

        for argv, expected in cases.items():
            with self.subTest(argv=argv), patch.object(sys, "argv", argv):
                args = self.main.parse_args()
                self.assertEqual((args.ping, args.ssh), expected)

    def test_maintenance_flags_are_parsed(self) -> None:
        argv = (
            "main.py",
            "--select-config",
            "configs",
            "--set",
            "settings.ssh_timeout_seconds=2",
            "--backup-config",
            "backups",
        )

        with patch.object(sys, "argv", argv):
            args = self.main.parse_args()

        self.assertEqual(args.select_config, Path("configs"))
        self.assertEqual(
            args.overrides, ["settings.ssh_timeout_seconds=2"]
        )
        self.assertEqual(args.backup_config, Path("backups"))

    def test_config_and_interactive_selection_are_mutually_exclusive(self) -> None:
        with (
            patch.object(
                sys,
                "argv",
                ("main.py", "--config", "one.yml", "--select-config", "configs"),
            ),
            patch("sys.stderr"),
            self.assertRaises(SystemExit) as context,
        ):
            self.main.parse_args()

        self.assertEqual(context.exception.code, 2)

    def test_typed_config_overrides_merge_without_mutating_source(self) -> None:
        config = {
            "settings": {"ssh_timeout_seconds": 3, "allowed_networks": []},
            "database": {"port": 5432},
        }

        updated = self.main.apply_config_overrides(
            config,
            [
                "settings.ssh_timeout_seconds=2",
                'settings.allowed_networks=["192.168.0.0/16"]',
                "database.port=5433",
            ],
        )

        self.assertEqual(config["settings"]["ssh_timeout_seconds"], 3)
        self.assertEqual(updated["settings"]["ssh_timeout_seconds"], 2)
        self.assertEqual(updated["settings"]["allowed_networks"], ["192.168.0.0/16"])
        self.assertEqual(updated["database"]["port"], 5433)

    def test_invalid_config_override_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "KEY=JSON_VALUE"):
            self.main.apply_config_overrides({}, ["settings.timeout"])

    def test_choose_config_uses_shared_selector_and_file_check(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            selected = Path(directory) / "nested" / "config.yml"
            write_yaml(selected, {})

            with patch.object(
                self.main, "select_file", return_value=str(selected)
            ) as selector:
                result = self.main.choose_config(Path(directory))

        selector.assert_called_once_with(directory, ".yml")
        self.assertEqual(result, selected)

    def test_shared_file_listing_recurses_and_filters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / "root.json", {})
            write_json(root / "nested" / "nested.json", {})
            (root / "nested" / "ignored.txt").write_text("ignored", encoding="utf-8")

            files = list_files(directory, ".json")

        self.assertEqual(
            files,
            sorted(
                [
                    str(root / "root.json"),
                    str(root / "nested" / "nested.json"),
                ]
            ),
        )

    def test_backup_config_preserves_file_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source" / "config.yml"
            target = root / "backup"
            write_yaml(source, {"source": "active"})

            destination = self.main.backup_config(source, target)

            self.assertEqual(destination, target / "config.yml")
            self.assertEqual(
                yaml.safe_load(destination.read_text(encoding="utf-8")),
                {"source": "active"},
            )

    def test_doctor_reports_commands_and_package_state(self) -> None:
        output = StringIO()
        executables = {
            "dpkg": "/usr/bin/dpkg",
            "ping": "/usr/bin/ping",
            "ssh": "/usr/bin/ssh",
        }

        with (
            patch.object(
                self.main.shutil,
                "which",
                side_effect=lambda command: executables[command],
            ),
            patch.object(
                self.main,
                "is_package_installed",
                side_effect=lambda package: package in {"iputils-ping", "openssh-client"},
            ),
        ):
            healthy = self.main.run_doctor(stream=output)

        self.assertTrue(healthy)
        self.assertIn("ping: available at /usr/bin/ping", output.getvalue())
        self.assertNotIn("docker", output.getvalue())
        self.assertIn("openssh-client", output.getvalue())

    def test_positional_protocol_mode_is_rejected(self) -> None:
        with (
            patch.object(sys, "argv", ("main.py", "ping")),
            patch("sys.stderr"),
            self.assertRaises(SystemExit) as context,
        ):
            self.main.parse_args()

        self.assertEqual(context.exception.code, 2)

    def test_run_checks_dispatches_only_selected_protocols(self) -> None:
        config = {
            "settings": {},
            "ping_hosts": [],
            "ssh_hosts": [],
        }
        calls = []

        async def fake_ping(*args, **kwargs):
            calls.append("ping")
            return []

        async def fake_ssh(*args, **kwargs):
            calls.append("ssh")
            return []

        with (
            patch.object(self.main, "check_ping_hosts", fake_ping),
            patch.object(self.main, "check_ssh_hosts", fake_ssh),
        ):
            asyncio.run(
                self.main.run_checks(config, run_ping=True, run_ssh=False)
            )
            self.assertEqual(calls, ["ping"])

            calls.clear()
            asyncio.run(
                self.main.run_checks(config, run_ping=False, run_ssh=True)
            )
            self.assertEqual(calls, ["ssh"])

            calls.clear()
            asyncio.run(
                self.main.run_checks(config, run_ping=True, run_ssh=True)
            )
            self.assertEqual(calls, ["ping", "ssh"])

    def test_ssh_check_uses_configured_user_and_port(self) -> None:
        class FakeProcess:
            returncode = 0

            async def communicate(self) -> tuple[bytes, bytes]:
                return b"", b""

        commands = []

        async def fake_subprocess(*command, **kwargs):
            commands.append(command)
            return FakeProcess()

        host = self.main.Host(
            name="server",
            address="192.0.2.1",
            os="Linux",
            ssh_user="user0",
            ssh_port=2222,
        )

        with patch.object(
            self.main.asyncio,
            "create_subprocess_exec",
            fake_subprocess,
        ):
            result = asyncio.run(
                self.main.check_ssh_host(host, asyncio.Semaphore(1), timeout=5)
            )

        self.assertTrue(result.online)
        self.assertEqual(len(commands), 1)
        command = commands[0]
        self.assertIn("BatchMode=yes", command)
        self.assertIn("ConnectionAttempts=1", command)
        self.assertIn("2222", command)
        self.assertIn("user0@192.0.2.1", command)
        self.assertEqual(command[-1], "true")

    def test_ssh_failure_includes_last_diagnostic_line(self) -> None:
        class FakeProcess:
            returncode = 255

            async def communicate(self) -> tuple[bytes, bytes]:
                return b"", b"debug line\nPermission denied (publickey).\n"

        async def fake_subprocess(*command, **kwargs):
            return FakeProcess()

        host = self.main.Host("server", "192.0.2.1", "Linux")
        with patch.object(
            self.main.asyncio, "create_subprocess_exec", fake_subprocess
        ):
            result = asyncio.run(
                self.main.check_ssh_host(host, asyncio.Semaphore(1), timeout=3)
            )

        self.assertFalse(result.online)
        self.assertEqual(result.detail, "Permission denied (publickey).")

    def test_tcp_mode_opens_configured_address_and_port(self) -> None:
        class FakeWriter:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

            async def wait_closed(self) -> None:
                pass

        writer = FakeWriter()
        connections = []

        async def fake_open_connection(address, port):
            connections.append((address, port))
            return object(), writer

        host = self.main.Host("server", "192.0.2.1", "Linux", ssh_port=2222)
        with patch.object(self.main.asyncio, "open_connection", fake_open_connection):
            results = asyncio.run(
                self.main.check_ssh_hosts([host], concurrency=1, timeout=3, mode="tcp")
            )

        self.assertTrue(results[0].online)
        self.assertEqual(connections, [("192.0.2.1", 2222)])
        self.assertTrue(writer.closed)

    def test_invalid_ssh_mode_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "ssh_check_mode"):
            asyncio.run(
                self.main.check_ssh_hosts([], concurrency=1, timeout=3, mode="banner")
            )

    def test_allowed_networks_accept_matching_addresses_and_dns_names(self) -> None:
        networks = self.main.parse_allowed_networks(
            {"allowed_networks": ["192.168.0.0/16", "10.8.0.0/24"]}
        )
        hosts = [
            self.main.Host("lan", "192.168.1.10", "Linux"),
            self.main.Host("dns", "server.example.test", "Linux"),
        ]

        self.main.validate_host_networks(hosts, networks, "ping_hosts")

    def test_allowed_networks_reject_outside_address(self) -> None:
        networks = self.main.parse_allowed_networks(
            {"allowed_networks": ["192.168.0.0/16"]}
        )
        hosts = [self.main.Host("typo", "198.168.1.10", "Linux")]

        with self.assertRaisesRegex(ValueError, "198.168.1.10.*outside"):
            self.main.validate_host_networks(hosts, networks, "ssh_hosts")

    def test_print_results_includes_duration_and_failure_detail(self) -> None:
        host = self.main.Host("server", "192.0.2.1", "Linux")
        result = self.main.HostStatus(host, False, 1.234, "connection refused")
        output = StringIO()

        self.main.print_results([result], stream=output)

        self.assertEqual(
            output.getvalue(),
            "  ❌ server: offline (1.23s, connection refused)\n",
        )

    def test_database_rejects_blank_required_values_before_connecting(self) -> None:
        host = self.main.Host("server", "192.0.2.1", "Linux")
        config = {
            "database": {
                "host": "127.0.0.1",
                "port": 5432,
                "name": "database",
                "user": "   ",
                "password": "secret",
            }
        }

        with self.assertRaisesRegex(ValueError, "Missing database settings: user"):
            self.main.update_statuses(config, [self.main.HostStatus(host, True)])

    def test_database_reconciles_tested_entity_names(self) -> None:
        class FakeCursor:
            def __init__(self) -> None:
                self.executed = []

            def execute(self, query) -> None:
                self.executed.append(query)

            def __enter__(self):
                return self

            def __exit__(self, *args) -> None:
                pass

        class FakeConnection:
            def __init__(self, cursor) -> None:
                self.cursor_instance = cursor

            def cursor(self):
                return self.cursor_instance

            def __enter__(self):
                return self

            def __exit__(self, *args) -> None:
                pass

        cursor = FakeCursor()
        connection = FakeConnection(cursor)
        execute_values_calls = []
        config = {
            "database": {
                "host": "127.0.0.1",
                "port": 5432,
                "name": "database",
                "user": "user",
                "password": "secret",
            }
        }
        results = [
            self.main.HostStatus(self.main.Host("one", "192.0.2.1", "Linux"), True),
            self.main.HostStatus(self.main.Host("two", "192.0.2.2", "Windows"), False),
        ]

        with (
            patch.object(sys.modules["psycopg2"], "connect", return_value=connection),
            patch.object(
                sys.modules["psycopg2.extras"],
                "execute_values",
                side_effect=lambda *args, **kwargs: execute_values_calls.append(
                    (args, kwargs)
                ),
            ),
        ):
            self.main.update_statuses(config, results)

        self.assertEqual(len(execute_values_calls), 1)
        query, rows = execute_values_calls[0][0][1:3]
        self.assertIn("DELETE FROM hosts AS target", query)
        self.assertIn("UPDATE hosts AS target", query)
        self.assertIn("INSERT INTO hosts", query)
        self.assertEqual({row[2] for row in rows}, {"one", "two"})
        self.assertEqual(cursor.executed, [])

    def test_database_clears_entries_when_no_entities_are_tested(self) -> None:
        class FakeCursor:
            def __init__(self) -> None:
                self.executed = []

            def execute(self, query) -> None:
                self.executed.append(query)

            def __enter__(self):
                return self

            def __exit__(self, *args) -> None:
                pass

        class FakeConnection:
            def __init__(self, cursor) -> None:
                self.cursor_instance = cursor

            def cursor(self):
                return self.cursor_instance

            def __enter__(self):
                return self

            def __exit__(self, *args) -> None:
                pass

        cursor = FakeCursor()
        connection = FakeConnection(cursor)
        config = {
            "database": {
                "host": "127.0.0.1",
                "port": 5432,
                "name": "database",
                "user": "user",
                "password": "secret",
            }
        }

        with patch.object(sys.modules["psycopg2"], "connect", return_value=connection):
            self.main.update_statuses(config, [])

        self.assertEqual(cursor.executed, ["DELETE FROM hosts"])

    def test_redirected_output_uses_emojis_without_ansi_colors(self) -> None:
        output = StringIO()

        self.main.print_message("🚀", "Running checks…", "cyan", stream=output)

        self.assertEqual(output.getvalue(), "🚀  Running checks…\n")
        self.assertNotIn("\033[", output.getvalue())

    def test_interactive_output_uses_ansi_colors(self) -> None:
        class TtyBuffer(StringIO):
            def isatty(self) -> bool:
                return True

        output = TtyBuffer()
        with patch.dict("os.environ", {}, clear=True):
            self.main.print_message("✅", "Online", "lgreen", stream=output)

        self.assertIn("\033[1;92m", output.getvalue())
        self.assertIn("✅  Online", output.getvalue())
        self.assertTrue(output.getvalue().endswith("\033[0m\n"))

    def test_no_color_environment_variable_disables_ansi_colors(self) -> None:
        class TtyBuffer(StringIO):
            def isatty(self) -> bool:
                return True

        output = TtyBuffer()
        with patch.dict("os.environ", {"NO_COLOR": "1"}, clear=True):
            self.main.print_message("✅", "Online", "lgreen", stream=output)

        self.assertEqual(output.getvalue(), "✅  Online\n")


if __name__ == "__main__":
    unittest.main()
