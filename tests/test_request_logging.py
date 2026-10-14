import io
import json
import logging
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

with patch.object(sys, "path", [str(Path(__file__).resolve().parents[1] / "src"), *sys.path]):
    from request_logging import create_request_logger


class RecordingHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


class BrokenHandler(logging.Handler):
    def emit(self, record):
        raise RuntimeError("action failed")


class RequestLoggingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "nested" / "requests.jsonl"

    def logger(self, specs=""):
        logger = create_request_logger(self.path, specs)
        for handler in logger.handlers:
            self.addCleanup(handler.close)
        return logger

    def emit(self, logger, command="rm /tmp/file", reason="destructive"):
        logger.info(reason, extra={"ts": "2026-01-01T00:00:00+00:00", "cmd": command, "reason": reason})

    def test_default_jsonl_schema_and_append(self):
        logger = self.logger()
        self.emit(logger)
        self.emit(logger, "unknown\ncommand", "not_implemented")
        records = [json.loads(line) for line in self.path.read_text().splitlines()]
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0], {"ts": "2026-01-01T00:00:00+00:00", "cmd": "rm /tmp/file", "reason": "destructive"})
        self.assertEqual(records[1]["cmd"], "unknown\ncommand")

    def test_multiple_plugins_receive_structured_records(self):
        first, second = RecordingHandler(), RecordingHandler()
        module = types.SimpleNamespace(first=lambda: first, second=lambda: second)
        with patch.dict(sys.modules, {"test_actions": module}):
            logger = self.logger(" test_actions:first, test_actions:second ")
        self.emit(logger)
        for handler in (first, second):
            self.assertEqual(len(handler.records), 1)
            self.assertEqual(handler.records[0].cmd, "rm /tmp/file")
            self.assertEqual(handler.records[0].reason, "destructive")
            self.assertEqual(handler.records[0].levelno, logging.INFO)
        self.assertTrue(self.path.exists())

    def test_plugin_failure_does_not_interrupt_other_handlers(self):
        last = RecordingHandler()
        module = types.SimpleNamespace(broken=BrokenHandler, last=lambda: last)
        with patch.dict(sys.modules, {"test_actions": module}):
            logger = self.logger("test_actions:broken,test_actions:last")
        with self.assertLogs("shell_sheath.logging_errors", level="ERROR") as errors:
            self.emit(logger)
        self.assertIn("BrokenHandler failed", errors.output[0])
        self.assertEqual(len(last.records), 1)
        self.assertEqual(json.loads(self.path.read_text())["reason"], "destructive")

    def test_handler_levels_and_filters_are_respected(self):
        handler = RecordingHandler()
        module = types.SimpleNamespace(handler=lambda: handler)
        with patch.dict(sys.modules, {"test_actions": module}):
            logger = self.logger("test_actions:handler")
        handler.setLevel(logging.ERROR)
        self.emit(logger)
        handler.setLevel(logging.INFO)
        handler.addFilter(lambda record: record.reason == "destructive")
        self.emit(logger, reason="not_implemented")
        self.emit(logger)
        self.assertEqual(len(handler.records), 1)

    def test_journal_handler_outputs_json_to_stderr(self):
        stream = io.StringIO()
        with patch.object(sys, "stderr", stream):
            logger = self.logger("request_logging:journal_handler")
            self.emit(logger)
        self.assertEqual(json.loads(stream.getvalue())["cmd"], "rm /tmp/file")

    def test_invalid_plugins_fail_configuration(self):
        module = types.SimpleNamespace(wrong=lambda: object())
        with patch.dict(sys.modules, {"test_actions": module}):
            for spec, exception in [
                ("no_colon", ValueError), (":factory", ValueError),
                ("test_actions:", ValueError), ("test_actions:missing", AttributeError),
                ("test_actions:wrong", TypeError),
            ]:
                with self.subTest(spec=spec), self.assertRaises(exception):
                    self.logger(spec)


if __name__ == "__main__":
    unittest.main()
