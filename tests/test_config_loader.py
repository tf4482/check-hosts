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

from utils_python.config_loader import load_config


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


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
            config_path = Path(directory) / "config.json"
            expected = {
                "database": {},
                "settings": {},
                "ping_hosts": [],
                "ssh_hosts": [],
            }
            write_json(config_path, expected)

            config = self.main.load_config(config_path)

        self.assertEqual(config, expected)

    def test_project_loader_rejects_missing_sections(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            write_json(config_path, {"settings": {}})

            with self.assertRaisesRegex(ValueError, "Missing configuration sections"):
                self.main.load_config(config_path)

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
            self.main.Host("lan", "192.168.1.10", "Linux", "10.8.0.2"),
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

    def test_vpn_fallbacks_are_batched_into_one_process(self) -> None:
        class FakeProcess:
            returncode = 0

            async def communicate(self) -> tuple[bytes, bytes]:
                return b"10.8.0.2\n", b""

        commands = []

        async def fake_subprocess(*command, **kwargs):
            commands.append(command)
            return FakeProcess()

        hosts = [
            self.main.Host("one", "192.0.2.1", "Linux", "10.8.0.2"),
            self.main.Host("two", "192.0.2.2", "Linux", "10.8.0.3"),
        ]
        with patch.object(
            self.main.asyncio, "create_subprocess_exec", fake_subprocess
        ):
            result = asyncio.run(
                self.main.check_vpn_ping_hosts(hosts, timeout=1, vpn_container="vpn")
            )

        self.assertEqual(len(commands), 1)
        self.assertIn("10.8.0.2", commands[0])
        self.assertIn("10.8.0.3", commands[0])
        self.assertEqual(result.reachable, frozenset({"10.8.0.2"}))

    def test_print_results_includes_duration_and_failure_detail(self) -> None:
        host = self.main.Host("server", "192.0.2.1", "Linux")
        result = self.main.HostStatus(host, False, 1.234, "connection refused")
        output = StringIO()

        self.main.print_results([result], stream=output)

        self.assertEqual(
            output.getvalue(),
            "  ❌ server: offline (1.23s, connection refused)\n",
        )

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
            self.main.print_message("✅", "Online", "green", stream=output)

        self.assertIn("\033[1;92m", output.getvalue())
        self.assertIn("✅  Online", output.getvalue())
        self.assertTrue(output.getvalue().endswith("\033[0m\n"))

    def test_no_color_environment_variable_disables_ansi_colors(self) -> None:
        class TtyBuffer(StringIO):
            def isatty(self) -> bool:
                return True

        output = TtyBuffer()
        with patch.dict("os.environ", {"NO_COLOR": "1"}, clear=True):
            self.main.print_message("✅", "Online", "green", stream=output)

        self.assertEqual(output.getvalue(), "✅  Online\n")


if __name__ == "__main__":
    unittest.main()
