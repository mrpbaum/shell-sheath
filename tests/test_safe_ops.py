"""Policy tests; FastMCP is stubbed so no server or Docker daemon is needed."""
import importlib.util
import json
import tempfile
from pathlib import Path
import subprocess
import sys
import types
import unittest
from unittest.mock import patch


class FakeFastMCP:
    def __init__(self, name):
        pass

    def tool(self, function):
        return function


spec = importlib.util.spec_from_file_location(
    "safe_ops", Path(__file__).resolve().parents[1] / "src" / "safe_ops.py"
)
ops = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {"fastmcp": types.SimpleNamespace(FastMCP=FakeFastMCP)}), patch.object(
    sys, "path", [str(Path(__file__).resolve().parents[1] / "src"), *sys.path]
):
    spec.loader.exec_module(ops)


class DockerExecTests(unittest.TestCase):
    def test_read_only_commands(self):
        for command in [
            "docker exec api ls /app",
            "docker exec -it api cat /etc/os-release",
            "docker exec --user 1000 --workdir /app api git status --short",
            "docker exec --user=1000 --workdir=/app api git log -n 5",
            "docker exec -- api ps aux",
            "docker exec api head -n 5 '/app/a file'",
        ]:
            with self.subTest(command=command):
                self.assertTrue(ops.is_known_safe(command))

    def test_unsafe_or_incomplete_commands(self):
        for command in [
            "docker exec", "docker exec api", "docker exec -u",
            "docker exec --workdir= api ls", "docker exec '' ls",
            "docker exec --privileged api ls", "docker exec -d api ls",
            "docker exec -e PATH=/tmp api ls", "docker exec --unknown api ls",
            "docker exec api sh -c 'ls'", "docker exec api python -c 'print(1)'",
            "docker exec api rm /app/file", "docker exec api git reset --hard",
            "docker exec api git branch -D main", "docker exec api git -c x=y status",
            "docker exec api git log --output=/tmp/log",
            "docker exec api git diff --ext-diff",
            "docker exec api curl -o /app/file https://example.com",
            "docker exec api docker ps", "docker --context local exec api sh",
            "docker exec api cat '",
        ]:
            with self.subTest(command=command):
                self.assertFalse(ops.is_known_safe(command))

    def test_try_cmd_rejections_never_execute(self):
        with patch.object(ops, "log_request"), patch.object(ops, "run_cmd") as run:
            for command in [
                "docker exec api sh -c ls", "docker exec api rm /app/file",
                "docker exec --privileged api ls", "docker exec api ls; uptime",
                "docker exec api sh -c 'ls | head'", "docker exec api ls | sh",
            ]:
                with self.subTest(command=command):
                    self.assertTrue(ops.try_cmd(command).startswith("Error:"))
            run.assert_not_called()

    def test_pipeline_execution_preserves_container_argv(self):
        results = [
            subprocess.CompletedProcess([], 0, "a\nb\n", ""),
            subprocess.CompletedProcess([], 0, "a\n", ""),
        ]
        with patch.object(ops.subprocess, "run", side_effect=results) as run:
            self.assertEqual(ops.try_cmd("docker exec api ls /app | head -n 1"), "a")
            self.assertEqual(run.call_args_list[0].args[0], ["docker", "exec", "api", "ls", "/app"])
            self.assertEqual(run.call_args_list[1].kwargs["input"], "a\nb\n")
            self.assertFalse(any(call.kwargs.get("shell") for call in run.call_args_list))


class RequestLogIntegrationTests(unittest.TestCase):
    def test_rejection_is_logged_without_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "requests.jsonl"
            logger = ops.create_request_logger(path)
            try:
                with patch.object(ops, "_get_request_logger", return_value=logger), patch.object(ops, "run_cmd") as run:
                    self.assertTrue(ops.try_cmd("rm /app/file").startswith("Error:"))
                    run.assert_not_called()
                record = json.loads(path.read_text())
                self.assertEqual(record["cmd"], "rm /app/file")
                self.assertEqual(record["reason"], "destructive")
                self.assertIn("ts", record)
            finally:
                for handler in logger.handlers:
                    handler.close()


class StartupTests(unittest.TestCase):
    def setUp(self):
        logger_patch = patch.object(ops, "_get_request_logger")
        self.logger = logger_patch.start()
        self.addCleanup(logger_patch.stop)

    def test_default_transport_is_stdio(self):
        with patch.dict(ops.os.environ, {}, clear=True), patch.object(ops.mcp, "run", create=True) as run:
            ops.main()
            run.assert_called_once_with()

    def test_http_defaults_to_localhost(self):
        with patch.dict(ops.os.environ, {"SHELL_SHEATH_TRANSPORT": "http"}, clear=True), patch.object(ops.mcp, "run", create=True) as run:
            ops.main()
            run.assert_called_once_with(transport="http", host="127.0.0.1", port=8765)
            self.logger.assert_called_once_with()

    def test_http_environment_overrides(self):
        env = {"SHELL_SHEATH_TRANSPORT": "http", "SHELL_SHEATH_HOST": "127.0.0.2", "SHELL_SHEATH_PORT": "9000"}
        with patch.dict(ops.os.environ, env, clear=True), patch.object(ops.mcp, "run", create=True) as run:
            ops.main()
            run.assert_called_once_with(transport="http", host="127.0.0.2", port=9000)

    def test_plugin_configuration_failure_prevents_startup(self):
        self.logger.side_effect = ValueError("invalid plugin")
        with patch.dict(ops.os.environ, {"SHELL_SHEATH_TRANSPORT": "http"}, clear=True), patch.object(ops.mcp, "run", create=True) as run:
            with self.assertRaisesRegex(ValueError, "invalid plugin"):
                ops.main()
            run.assert_not_called()

    def test_unknown_transport_is_rejected(self):
        with patch.dict(ops.os.environ, {"SHELL_SHEATH_TRANSPORT": "invalid"}, clear=True):
            with self.assertRaises(ValueError):
                ops.main()


if __name__ == "__main__":
    unittest.main()
