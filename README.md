# Host Status Checker

A Python 3.12 command-line application that concurrently checks host availability
with ICMP ping, SSH, or both and writes the results to PostgreSQL.

## Requirements

- Python 3.12 or newer
- PostgreSQL
- The system `ping` and `ssh` commands
- Docker when using the optional VPN ping fallback

## Installation

Clone the repository with its utility submodule:

```bash
git clone --recurse-submodules <repository-url>
cd check-hosts
```

For an existing checkout, initialize the submodule with:

```bash
git submodule update --init --recursive
```

Create and activate a virtual environment, then install the project:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

This installs the `check-hosts` command and its Python dependencies.

## Standalone executable

Install the locked development tools and build a single-file Linux executable:

```bash
uv sync --dev
uv run pyinstaller --onefile --name check-hosts main.py
```

The executable is written to `dist/check-hosts`:

```bash
./dist/check-hosts --help
./dist/check-hosts --ssh --config config.json
```

The executable bundles the Python application and Python dependencies, but keeps
configuration external. Without `--config`, it searches for `config.json` in the
current working directory and then `~/.config/check-hosts/config.json`. The system
`ping`, `ssh`, and optional `docker` commands are also still required at runtime.

## Database

The application expects a PostgreSQL `hosts` table with at least these columns:

```sql
CREATE TABLE hosts (
    name TEXT NOT NULL,
    os TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'offline',
    last_check TIMESTAMP,
    PRIMARY KEY (name, os)
);
```

Each configured host must already have a matching row. Updates identify hosts by
the combination of `name` and `os`.

## Configuration

Copy the example configuration and replace its placeholder values:

```bash
cp config.example.json config.json
chmod 600 config.json
```

Without `--config`, the application searches for configuration in this order:

1. `config.json` in the current working directory.
2. `~/.config/check-hosts/config.json`.

If neither file exists, the shared configuration loader creates a placeholder at
`~/.config/check-hosts/config.json` with `0600` permissions and exits so it can be
edited safely. Use `--config` to load one exact path without fallback behavior.

```json
{
  "database": {
    "host": "127.0.0.1",
    "port": 5432,
    "name": "host_status",
    "user": "status_user",
    "password": "change-me"
  },
  "settings": {
    "ping_concurrency": 8,
    "ping_timeout_seconds": 1,
    "vpn_container": "wireguard",
    "ssh_concurrency": 4,
    "ssh_timeout_seconds": 10
  },
  "ping_hosts": [
    {
      "name": "workstation-01",
      "address": "192.0.2.10",
      "os": "Linux"
    },
    {
      "name": "laptop-01",
      "address": "192.0.2.20",
      "vpn_address": "10.8.0.10",
      "os": "Windows"
    }
  ],
  "ssh_hosts": [
    {
      "name": "server-01",
      "address": "198.51.100.10",
      "os": "Linux",
      "ssh_user": "automation",
      "ssh_port": 22
    }
  ]
}
```

### Ping checks

The primary `address` is checked with one ICMP packet. If that fails and the host
has a `vpn_address`, the application runs a second ping inside `vpn_container`
with `docker exec`.

### SSH checks

SSH checks run the remote command `true` with batch mode enabled. Configure
non-interactive authentication, such as an SSH key and agent, before running the
application. Interactive password prompts are not supported.

## Usage

Run both check types, which is the default:

```bash
check-hosts
check-hosts --ping --ssh
```

Run one check type:

```bash
check-hosts --ping
check-hosts --ssh
```

Supplying neither protocol flag runs both. Supplying one flag runs only that
protocol; supplying both flags explicitly runs both.

Use another configuration file:

```bash
check-hosts --ping --config /path/to/config.json
```

The source file can also be invoked directly:

```bash
python main.py --ssh --config config.json
```

Results are printed after their PostgreSQL updates complete:

```text
workstation-01: online
laptop-01: offline
server-01: online
```

## Behavior and limitations

- Ping and SSH checks have independent concurrency limits.
- When both are selected, the two check groups run concurrently and are written
  in one database transaction.
- An unreachable host does not produce a non-zero process exit code; startup,
  configuration, or database errors do.
- Command output is suppressed. Missing commands, authentication failures, and
  connection failures are currently represented as an offline result.
- Ping and SSH checks update the same `status` and `last_check` columns. Avoid
  placing the same host in both lists unless this overwrite behavior is desired.
