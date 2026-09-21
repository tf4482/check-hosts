# Host Status Checker

A Python 3.12 command-line application that concurrently checks host availability
with ICMP ping, SSH, or both and writes the results to PostgreSQL.

## Requirements

- Python 3.12 or newer
- PostgreSQL
- The system `ping` and `ssh` commands

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

### Shared utility submodule

The application reuses the `utils_python` submodule for cross-project concerns:

- `config_loader` and `filecheck`: configuration discovery, path checks, loading,
  and private placeholder creation.
- `colored_text` and `convert_to_string`: TTY-aware ANSI formatting and consistent
  value-to-text conversion.
- `check_config`: required-value validation.
- `create_config`: typed, nested in-memory configuration overrides.
- `list_files`, `directory_traversal`, and `select_file`: recursive interactive
  YAML configuration selection.
- `copy_files`: metadata-preserving configuration backups.
- `dpkg_check`: optional Debian package details in the runtime doctor.

All utility modules are now used either directly by the application or
transitively by another shared utility.

## Standalone executable

Install the locked development tools and build a single-file Linux executable:

```bash
uv sync --dev
uv run pyinstaller --onefile --name check-hosts main.py
```

The executable is written to `dist/check-hosts`:

```bash
./dist/check-hosts --help
./dist/check-hosts --ssh --config config.yml
```

The executable bundles the Python application and Python dependencies, but keeps
configuration external. Without `--config`, it searches for `config.yml` in the
current working directory and then `~/.config/check-hosts/config.yml`. The system
`ping` and `ssh` commands are also still required at runtime.

## Database

The application expects a PostgreSQL `hosts` table with at least these columns:

```sql
CREATE TABLE hosts (
    name TEXT PRIMARY KEY,
    os TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'offline',
    last_check TIMESTAMP
);
```

Each completed run reconciles the table against tested entity names: missing names
are inserted, matching names are updated, and rows for names not tested are
removed. Entity names must therefore be unique.

## Configuration

Copy the example configuration and replace its placeholder values:

```bash
cp config.example.yml config.yml
chmod 600 config.yml
```

Without `--config`, the application searches for configuration in this order:

1. `config.yml` in the current working directory.
2. `~/.config/check-hosts/config.yml`.

If neither file exists, the shared configuration loader creates a placeholder at
`~/.config/check-hosts/config.yml` with `0600` permissions and exits so it can be
edited safely. Use `--config` to load one exact path without fallback behavior.

```yaml
database:
  host: 127.0.0.1
  port: 5432
  name: host_status
  user: status_user
  password: change-me

settings:
  suffix: example.com
  ping_concurrency: 20
  ping_timeout_seconds: 1
  ssh_concurrency: 4
  ssh_timeout_seconds: 3
  ssh_check_mode: login
  allowed_networks: []

ping_hosts:
  - name: workstation-01
    address: 192.0.2.10
    os: Linux
  - name: laptop-01
    os: Windows

ssh_hosts:
  - name: server-01
    address: 198.51.100.10
    os: Linux
    ssh_user: automation
    ssh_port: 22
```

### Ping checks

Each host `address` is checked with one ICMP packet.

Host `address` is optional. When it is omitted or blank, the application uses
`<name>.<settings.suffix>` instead. The shared `settings.suffix` value is required
for any host without an explicit address; it may be written with or without its
leading dot.

### SSH checks

Set `ssh_check_mode` to one of:

- `login` (default): run the remote command `true` with batch mode enabled. This
  verifies the SSH service, host key, authentication, and command execution.
  Configure non-interactive authentication, such as an SSH key and agent, before
  running the application. Interactive password prompts are not supported.
- `tcp`: only verify that a TCP connection can be opened to `address:ssh_port`.
  This is faster but does not verify SSH authentication or command execution.

Each SSH check makes one connection attempt. The default connection timeout is
three seconds, which is intended for local networks.

### Network validation

`allowed_networks` optionally restricts literal host IP addresses to a list of
IPv4 or IPv6 CIDR networks. An empty list disables validation. DNS names remain
valid because their resolved addresses may change.

For example:

```yaml
"allowed_networks": ["192.168.0.0/16", "10.8.0.0/24"]
```

When enabled, an address outside every configured network causes a configuration
error before any checks start. This can catch mistakes such as `198.168.1.10`
instead of `192.168.1.10` without waiting for a network timeout.

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
check-hosts --ping --config /path/to/config.yml
```

Interactively select a YAML configuration from a directory and its
subdirectories:

```bash
check-hosts --select-config ./configs
```

Apply typed, nested overrides without modifying the source file. Values are
parsed as JSON when possible; unquoted values remain strings:

```bash
check-hosts \
  --set settings.ssh_timeout_seconds=2 \
  --set settings.ssh_check_mode='"tcp"' \
  --set settings.allowed_networks='["192.168.0.0/16","10.8.0.0/24"]'
```

Back up the selected configuration before running checks:

```bash
check-hosts --config ./config.yml --backup-config ./backups
```

The backup preserves file metadata and uses the source filename. The source and
destination must be different paths.

Run the standalone dependency doctor without loading configuration, checking
hosts, or updating PostgreSQL:

```bash
check-hosts --doctor
```

The doctor checks the required `ping` and `ssh` executables. On Debian-based
systems it also reports matching installed packages. It exits non-zero when a
required executable is missing.

The source file can also be invoked directly:

```bash
python main.py --ssh --config config.yml
```

Results include elapsed time and, for failed checks, a concise diagnostic. Host
results are printed before their PostgreSQL update, followed by a final summary:

```text
⚙️  Configuration loaded
📡  Ping: 2 hosts, concurrency 20, timeout 1s
🔐  SSH: 1 hosts, mode login, concurrency 4, timeout 3s
🚀  Running host checks…

📋  Host results
  ✅ workstation-01: online (0.01s)
  ❌ laptop-01: offline (1.04s, primary ping failed)
  ✅ server-01: online (0.12s)

💾  Updating PostgreSQL at 127.0.0.1:5432 /host_status…
✅  Database statuses updated
⚠️  Completed in 1.06s — 2 online, 1 offline, 3 checked
```

Interactive terminals use ANSI colors for stages, statuses, timing metadata,
and summaries. Redirected output remains plain text. Set the standard `NO_COLOR`
environment variable or `TERM=dumb` to disable colors explicitly; emojis remain
present so log stages and statuses are still easy to scan.

## Behavior and limitations

- Ping and SSH checks have independent concurrency limits.
- When both are selected, the two check groups run concurrently and are written
  in one database transaction.
- An unreachable host does not produce a non-zero process exit code; startup,
  configuration, or database errors do.
- Command output is suppressed, but a concise failure reason is included in the
  result. Missing commands, authentication failures, and connection failures are
  represented as an offline result.
- Ping and SSH checks update the same `status` and `last_check` columns. Avoid
  placing the same host in both lists unless this overwrite behavior is desired.
- Interactive configuration selection occurs only when `--select-config` is
  supplied, so unattended executions remain non-interactive.
- Configuration overrides are in-memory only; backups always copy the original
  loaded file rather than a generated override result.
