"""Rejected-request logging composed from standard Python logging handlers."""
import importlib
import json
import logging
from pathlib import Path


class RequestJSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({"ts": record.ts, "cmd": record.cmd, "reason": record.reason})


class RequestLogger(logging.Logger):
    """Deliver to every handler even when a custom action raises an exception."""

    def callHandlers(self, record: logging.LogRecord) -> None:
        for handler in self.handlers:
            if record.levelno >= handler.level:
                try:
                    handler.handle(record)
                except Exception:
                    logging.getLogger("shell_sheath.logging_errors").exception(
                        "Request log handler %s failed", type(handler).__name__
                    )


def journal_handler() -> logging.Handler:
    """Optional JSON stderr output, captured by journald under systemd."""
    handler = logging.StreamHandler()
    handler.setFormatter(RequestJSONFormatter())
    return handler


def create_request_logger(log_file: Path, handler_specs: str = "") -> logging.Logger:
    """Create a JSONL logger plus comma-separated module:factory handlers.

    Factories take no arguments and must return a logging.Handler. Invalid
    configuration fails startup; exceptions during delivery are isolated.
    """
    logger = RequestLogger("shell_sheath.requests", level=logging.INFO)
    logger.propagate = False
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8", delay=True)
        file_handler.setFormatter(RequestJSONFormatter())
        logger.addHandler(file_handler)
        for spec in handler_specs.split(","):
            spec = spec.strip()
            if not spec:
                continue
            module_name, separator, factory_name = spec.partition(":")
            if not separator or not module_name or not factory_name:
                raise ValueError(f"Invalid request log handler {spec!r}; expected module:factory")
            factory = getattr(importlib.import_module(module_name), factory_name)
            handler = factory()
            if not isinstance(handler, logging.Handler):
                raise TypeError(f"Request log factory {spec!r} must return a logging.Handler")
            logger.addHandler(handler)
        return logger
    except Exception:
        for handler in logger.handlers:
            handler.close()
        raise
