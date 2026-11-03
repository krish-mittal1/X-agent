"""Structured logging with correlation IDs and secret redaction."""

from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
import uuid
from datetime import UTC, datetime
from typing import Any

correlation_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default="-"
)

_SECRET_PATTERNS = [
    re.compile(r"(?i)(authorization\s*[:=]\s*)bearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)(oauth_signature=\"?)[^\"&,\s]+"),
    re.compile(r"(?i)(api[_-]?key\"?\s*[:=]\s*\"?)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)(access[_-]?token\"?\s*[:=]\s*\"?)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{12,}"),
]


def redact(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: m.group(1) + "***", text)
    return text


def new_correlation_id() -> str:
    value = uuid.uuid4().hex[:16]
    correlation_id_var.set(value)
    return value


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "correlation_id"):
            record.correlation_id = correlation_id_var.get()
        return True


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
            "correlation_id": getattr(record, "correlation_id", "-"),
        }
        for key, value in getattr(record, "extra_fields", {}).items():
            payload[key] = value
        if record.exc_info:
            payload["exc_info"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


class _HumanFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = (
            f"{self.formatTime(record, '%Y-%m-%d %H:%M:%S')} "
            f"{record.levelname:<7} [{getattr(record, 'correlation_id', '-')}] "
            f"{record.name}: {redact(record.getMessage())}"
        )
        extra = getattr(record, "extra_fields", {})
        if extra:
            base += " " + json.dumps(extra, default=str)
        if record.exc_info:
            base += "\n" + redact(self.formatException(record.exc_info))
        return base


def configure_logging(level: str = "INFO", json_logs: bool = True) -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter() if json_logs else _HumanFormatter())
    handler.addFilter(_ContextFilter())
    root.addHandler(handler)
    for noisy in ("httpx", "httpcore", "uvicorn.access", "apscheduler.executors.default"):
        logging.getLogger(noisy).setLevel("WARNING")


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_event(
    logger: logging.Logger, level: int, message: str, **fields: Any
) -> None:
    logger.log(level, message, extra={"extra_fields": fields})
