# shell-sheath

Policy-filtered host and container diagnostics via a FastMCP server. Run it on
Linux as a systemd service to inspect the host directly, without container mounts
or namespace workarounds.

## Install as a systemd service

Requirements:
- A systemd-based Linux host and Python 3.10 or newer.
- `pipx` installed with the system package manager (e.g. `sudo apt install pipx`
  on Debian/Ubuntu or `sudo pacman -S python-pipx` on Arch).
- Host executables for the diagnostics you use. Docker commands require the
  Docker CLI and access to the daemon; these are not installed by this repo.
- Optional: `just` for convenience recipes.

Review the code before granting it host access, then run from this checkout:

```sh
just test
just install-service
# Without just:
# sudo bash scripts/install-service.sh
```

The installer creates a dedicated `shell-sheath` system user, installs the
application into a root-owned pipx environment under `/opt/shell-sheath`, copies
`deploy/shell-sheath.service` into `/etc/systemd/system`, and enables and starts
it. It requires network access to download Python dependencies. Re-run after
updating this checkout to reinstall and restart; existing configuration and
request logs are preserved.

The MCP client URL is **`http://127.0.0.1:8765/mcp`** (Streamable HTTP). Configure
an HTTP/Streamable HTTP MCP connection in your client; it is not a stdio service.

## Configuration and operation

All `SHELL_SHEATH_*` service settings are loaded by systemd from the required
system environment file **`/etc/shell-sheath.env`** on each service start. The
installer creates it from `deploy/shell-sheath.env.example`; on updates it adds
missing settings without overwriting your existing values.

To configure the service:

```sh
sudoedit /etc/shell-sheath.env
```

Set the values you need using these defaults:

```ini
SHELL_SHEATH_TRANSPORT=http
SHELL_SHEATH_HOST=127.0.0.1
SHELL_SHEATH_PORT=8765
SHELL_SHEATH_LOG_FILE=/var/lib/shell-sheath/requests.jsonl
SHELL_SHEATH_LOG_HANDLERS=
```

| Variable | Purpose |
| --- | --- |
| `SHELL_SHEATH_TRANSPORT` | Use `http` for systemd. `stdio` is for a CLI launched by an MCP client, not this service. |
| `SHELL_SHEATH_HOST` | Listening address. Keep `127.0.0.1` unless you have secured remote access. |
| `SHELL_SHEATH_PORT` | HTTP port. Update your MCP client URL when changing it. |
| `SHELL_SHEATH_LOG_FILE` | Absolute path for rejected-request logs. Keep it under `/var/lib/shell-sheath`, the writable service state directory. |
| `SHELL_SHEATH_LOG_HANDLERS` | Optional comma-separated `module:factory` names for additional Python logging handlers. Empty means JSONL only. |

Use `KEY=value`, not `export KEY=value`. This is a systemd environment file, not
a shell script: do not use `$VARIABLE` expansion or commands. Quote values that
contain spaces. Variables exported in your interactive shell do not configure
the system service. Keep the file root-owned and do not allow the service account
to modify it.

Save the file, then restart and check the service:

```sh
just restart
just status
# Without just:
# sudo systemctl restart shell-sheath.service
# systemctl --no-pager status shell-sheath.service
```

A restart is sufficient for env-file edits; `daemon-reload` is needed only when
the unit itself changes. If upgrading from the old unit with inline settings,
run `just install-service` once to install the new unit and fill missing settings.
The Python CLI does not read this system file itself; systemd supplies the
variables to the service. For foreground use, set variables in the CLI's own
environment instead.

Other recipes:

```sh
just status
just logs           # systemd stdout/stderr logs, follows live
just stop
```

Rejected requests are recorded in `/var/lib/shell-sheath/requests.jsonl`.
The service starts in `/`, so supply absolute paths for host file diagnostics.
There is no interactive terminal; prefer non-TTY Docker exec commands.

### Request log handlers and custom actions

Rejected commands produce standard Python `logging.LogRecord` events at `INFO`
level with three extra fields: `record.ts` (UTC ISO timestamp), `record.cmd`
(the original command), and `record.reason` (the rejection category).
`record.getMessage()` returns the reason. Accepted commands and subprocess
output are not logged through this interface.

The default JSONL file handler always remains enabled and preserves the existing
`{"ts": ..., "cmd": ..., "reason": ...}` schema. Additional handlers receive the
same event in configured order, after the file handler. To also send JSON events
to the service's journal, set this in `/etc/shell-sheath.env`:

```ini
SHELL_SHEATH_LOG_HANDLERS=request_logging:journal_handler
```

Custom actions use standard `logging.Handler` subclasses; no separate plugin
framework is needed. For example, an installed Python module named `my_actions`
could contain:

```python
import logging
import sys

class AlertHandler(logging.Handler):
    def emit(self, record):
        if record.reason == "destructive":
            # Replace with a notification or another bounded custom action.
            print(f"Blocked destructive request: {record.cmd!r}", file=sys.stderr)

def make_handler():
    return AlertHandler()
```

Each factory takes no arguments and returns a `logging.Handler`. It can read
its own configuration from environment variables, and configure handler levels,
filters, or formatters. Install the module and its dependencies into the same
pipx environment as Shell Sheath, using an installable Python package:

```sh
sudo env PIPX_HOME=/opt/shell-sheath/pipx \
  PIPX_BIN_DIR=/opt/shell-sheath/bin \
  pipx inject shell-sheath /absolute/path/to/action-package
```

Then configure one or more factories and restart:

```ini
SHELL_SHEATH_LOG_HANDLERS=request_logging:journal_handler,my_actions:make_handler
```

```sh
just restart
just logs
```

Handler factories are loaded before the MCP server starts. Invalid imports,
factory errors, or non-handler return values fail startup rather than silently
ignoring configuration. During delivery, a handler exception is reported to
stderr/journald and does not stop rejection or delivery to subsequent handlers.
Delivery is best-effort: there are no retries, durable queues, or guarantees if
an action or the file write fails.

Actions run synchronously, so keep them fast and use explicit timeouts for
network calls. They run with the service account's privileges and systemd
restrictions; install only trusted plugins. Commands can contain sensitive
arguments, so consider what gets sent to external destinations. Keep plugin code
root-owned, not in the service's writable state directory.

### Optional Docker access

Docker access is **not enabled by the installer**. If your Docker socket is
owned by the standard `docker` group:

```sh
just enable-docker
# Equivalent:
# sudo usermod -aG docker shell-sheath
# sudo systemctl restart shell-sheath.service
```

For rootless Docker or a custom socket, configure `DOCKER_HOST` in
`/etc/shell-sheath.env` and arrange the appropriate permissions. Check both the
socket and parent-directory permissions. Do not make the socket world-writable.

`docker exec` validates exec options, the container command, and its arguments:

```text
docker exec api ls /app
docker exec --workdir /app api git status --short
docker exec api cat /etc/os-release | head -n 10
```

Shell/interpreter wrappers, privileged exec, environment injection, unknown
commands, and destructive operations are rejected. See `src/safe_ops.py` for
the exact allowlists.

### Host permissions and security

The account is non-root. The unit makes the host filesystem read-only to this
process, with writable service state and private temporary storage. Host read
access still follows normal Unix permissions. Some diagnostics may return
permission errors or incomplete output. If system journal access is needed,
explicitly add the account to `systemd-journal` and restart the service; do not
run the entire service as root merely to make diagnostics work.

**Docker socket access effectively grants host-root control**, even to a
non-root account. The unit's restrictions do not constrain operations performed
by the Docker daemon on its behalf. Command filtering is not a complete security
boundary: the current host policy still permits broad `curl` arguments and
`docker compose` operations, for example. Tighten the policy before granting
untrusted agents access.

The HTTP endpoint has no application authentication configured. Keep it on
localhost, where other local users may also be able to reach it. For remote
access, prefer an SSH tunnel:

```sh
ssh -N -L 8765:127.0.0.1:8765 user@host
```

Do not expose it on a public interface without authentication, TLS, and a
review of the command policy. A tunnel alone does not prevent other local users
on either machine from connecting to the localhost endpoint.

## Local use and tests

```sh
just install-local   # pipx install --force .
just serve           # foreground HTTP server on localhost
shell-sheath         # stdio transport for local MCP clients
just test
```

Tests use a FastMCP stub and mocked subprocesses; no Docker daemon or installed
FastMCP dependency is required. The service uses FastMCP 2.14.5, pinned in
`pyproject.toml`.

## Uninstall

```sh
sudo systemctl disable --now shell-sheath.service
sudo rm /etc/systemd/system/shell-sheath.service
sudo systemctl daemon-reload
sudo rm -rf /opt/shell-sheath
```

This leaves `/etc/shell-sheath.env`, `/var/lib/shell-sheath`, and the service
account intact for inspection or reinstallation. Remove them separately if no
longer needed.
