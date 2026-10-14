# safe_ops_mcp.py
"""
Flexible FastMCP server for debugging containers and the host.
Agent can try any command via try_cmd.
Unknown or unimplemented commands are logged and rejected cleanly.
"""

from fastmcp import FastMCP
import subprocess
import re
import shlex
import os
from datetime import datetime, timezone
from pathlib import Path
from request_logging import create_request_logger

mcp = FastMCP("SafeOps")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

LOG_FILE = Path(os.environ.get("SHELL_SHEATH_LOG_FILE", str(Path.home() / ".safe_ops_requests.jsonl")))

# Commands currently considered safe (extend as you implement more)
KNOWN_SAFE = {
    "docker": ["ps", "logs", "inspect", "stats", "top", "images", "compose", "exec"],
    "git": ["status", "log", "diff", "show", "branch", "rev-parse", "ls-files"],
    "df": [],
    "free": [],
    "uptime": [],
    "ps": [],
    "ss": [],
    "journalctl": [],
    "systemctl": ["status", "is-active", "is-enabled"],
    "cat": [],
    "tail": [],
    "head": [],
    "ls": [],
    "curl": [],
}

DESTRUCTIVE = re.compile(
    r"\b(rm|rmi|kill|stop|prune|down|destroy|remove|delete|"
    r"reset\s+--hard|clean\s+-f|push\s+--force|"
    r"reboot|shutdown|systemctl\s+(stop|restart|disable))\b",
    re.IGNORECASE,
)

SHELL_META = re.compile(r"[;&|`$<>]")


_request_logger = None


def _get_request_logger():
    global _request_logger
    if _request_logger is None:
        _request_logger = create_request_logger(
            LOG_FILE, os.environ.get("SHELL_SHEATH_LOG_HANDLERS", "")
        )
    return _request_logger


def log_request(cmd: str, reason: str) -> None:
    _get_request_logger().info(
        reason,
        extra={
            "ts": datetime.now(timezone.utc).isoformat(),
            "cmd": cmd,
            "reason": reason,
        },
    )


# Deliberately narrower than the host policy: no shells, interpreters, nested
# Docker, or commands such as curl that can write files or mutate remote state.
CONTAINER_SAFE = {"df", "free", "uptime", "ps", "cat", "tail", "head", "ls"}
CONTAINER_GIT_SUBCOMMANDS = {"status", "log", "diff", "show", "rev-parse", "ls-files"}


def is_safe_docker_exec(parts: list[str]) -> bool:
    """Validate Docker options, then the actual argv executed in the container."""
    index = 2  # docker exec
    while index < len(parts) and parts[index].startswith("-"):
        option = parts[index]
        if option == "--":
            index += 1
            break
        if option in {"-i", "-t", "-it", "-ti", "--interactive", "--tty"}:
            index += 1
        elif option in {"-u", "--user", "-w", "--workdir"}:
            if index + 1 >= len(parts) or not parts[index + 1] or parts[index + 1].startswith("-"):
                return False
            index += 2
        elif option.startswith(("--user=", "--workdir=")):
            if not option.split("=", 1)[1]:
                return False
            index += 1
        else:
            # In particular: --privileged, environment injection, and detach.
            return False

    if index >= len(parts) or not parts[index] or parts[index].startswith("-"):
        return False
    argv = parts[index + 1:]  # skip the container name / ID
    if not argv or any(SHELL_META.search(arg) or DESTRUCTIVE.search(arg) for arg in argv):
        return False
    if argv[0] in CONTAINER_SAFE:
        return True
    if argv[0] == "git":
        if len(argv) < 2 or argv[1] not in CONTAINER_GIT_SUBCOMMANDS:
            return False
        # These options can write files or invoke external programs.
        for arg in argv[2:]:
            option = arg.split("=", 1)[0]
            # Git also accepts unambiguous abbreviations of long options.
            if option.startswith("--") and option != "--" and any(
                blocked.startswith(option)
                for blocked in {"--output", "--ext-diff", "--textconv", "--filters"}
            ):
                return False
        return True
    return False


def is_known_safe(cmd: str) -> bool:
    try:
        parts = shlex.split(cmd)
    except ValueError:
        return False
    if not parts:
        return False
    binary = parts[0]
    if binary not in KNOWN_SAFE:
        return False
    if binary == "docker":
        if len(parts) > 1 and parts[1] == "exec":
            return is_safe_docker_exec(parts)
        # Do not let Docker global flags bypass subcommand assessment.
        if len(parts) < 2 or parts[1].startswith("-"):
            return False
    allowed_subs = KNOWN_SAFE[binary]
    if not allowed_subs:
        return True
    sub = parts[1] if len(parts) > 1 else ""
    return sub in allowed_subs or sub.startswith("-")


SAFE_FILTERS = {"tail", "head", "grep", "cut", "wc", "sort", "uniq"}

def is_safe_pipeline(command: str) -> bool:
    if "|" not in command:
        return True
    parts = [p.strip() for p in command.split("|")]
    if len(parts) > 2:  # only one pipe
        return False
    left, right = parts
    # left side must be a known-safe command
    if not is_known_safe(left):
        return False
    # right side must be a simple filter
    try:
        filter_parts = shlex.split(right)
    except ValueError:
        return False
    return bool(filter_parts) and filter_parts[0] in SAFE_FILTERS


def run_cmd(cmd: str, timeout: int = 30) -> str:
    """Internal helper that actually executes a known-safe command."""
    try:
        output = None
        errors = []
        exit_code = 0
        for stage in cmd.split("|"):
            parts = shlex.split(stage)
            result = subprocess.run(
                parts,
                input=output,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            output = result.stdout
            if result.stderr:
                errors.append(result.stderr)
            if result.returncode != 0:
                exit_code = result.returncode
        out = output or ""
        if errors:
            out += "\n[stderr]\n" + "\n".join(errors)
        if exit_code != 0:
            out = f"[exit {exit_code}]\n{out}"
        return out.strip() or "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: command timed out"
    except FileNotFoundError:
        return f"Error: binary not found: {parts[0]}"
    except Exception as e:
        return f"Error: {e}"


# ---------------------------------------------------------------------------
# Main catch-all tool
# ---------------------------------------------------------------------------

@mcp.tool
def try_cmd(command: str, timeout: int = 30) -> str:
    """
    Try any shell command. If it's not yet implemented or is unsafe,
    you will get a clear error. The request is logged so it can be added later.

    Examples:
    - "docker ps -a"
    - "docker logs --tail 100 api"
    - "docker exec api ls /app"
    - "docker exec -i api git status"
    - "git status"
    - "df -h"
    - "journalctl -u docker -n 50"
    - "curl -s http://localhost:8080/health"
    """
    if SHELL_META.search(command.replace("|", "")):
        log_request(command, "shell_metacharacters")
        return (
            "Error: shell metacharacters not allowed. "
            "Pass a simple command or a single pipe to an approved filter "
            "(no redirects or ;). "
            "Try a different approach or ask for a specific tool to be added."
        )

    if DESTRUCTIVE.search(command):
        log_request(command, "destructive")
        return (
            f"Error: destructive operation blocked: {command}\n"
            "This is logged. Try a non-destructive alternative."
        )

    if not is_safe_pipeline(command):
        log_request(command, "unsafe_pipeline")
        filters = ", ".join(sorted(SAFE_FILTERS))
        return (
            "Error: unsafe pipeline blocked. Only one pipe from a supported "
            f"command to an approved filter is allowed: {filters}."
        )

    if not is_known_safe(command.split("|", 1)[0]):
        log_request(command, "not_implemented")
        supported = ", ".join(sorted(KNOWN_SAFE.keys()))
        return (
            f"Error: command not yet implemented: {command}\n"
            "This request has been logged. "
            "Try one of the currently supported commands, or describe what you need "
            "so a proper tool can be added.\n"
            f"Currently supported binaries: {supported}"
        )

    return run_cmd(command, timeout=timeout)



def main() -> None:
    """Keep stdio for local clients; systemd selects localhost HTTP via env."""
    transport = os.environ.get("SHELL_SHEATH_TRANSPORT", "stdio")
    if transport not in {"stdio", "http"}:
        raise ValueError("SHELL_SHEATH_TRANSPORT must be stdio or http")
    # Load plugins before accepting requests, so invalid configuration fails fast.
    _get_request_logger()
    if transport == "stdio":
        mcp.run()
    elif transport == "http":
        mcp.run(
            transport="http",
            host=os.environ.get("SHELL_SHEATH_HOST", "127.0.0.1"),
            port=int(os.environ.get("SHELL_SHEATH_PORT", "8765")),
        )


if __name__ == "__main__":
    main()