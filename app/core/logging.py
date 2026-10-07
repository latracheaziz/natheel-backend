"""Structured JSON logging with secret redaction."""
from __future__ import annotations

import json
import logging
import re
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)

_SENSITIVE_KEYS = re.compile(
    r"(access_token|refresh_token|id_token|client_secret|password|authorization|secret|encryption_key|"
    r"code_verifier|fb_exchange_token|upload_token|cookie)", re.I)
# key=value / "key": "value" patterns inside free-form messages
_INLINE_PATTERNS = [
    re.compile(r"(?i)(access_token|refresh_token|client_secret|password|code|fb_exchange_token)=([^&\s\"']+)"),
    re.compile(r"(?i)(\"(?:access_token|refresh_token|client_secret|password)\"\s*:\s*\")([^\"]+)(\")"),
    re.compile(r"(?i)(bearer\s+)([A-Za-z0-9\-._~+/]+=*)"),
]
_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime", "taskName"}


def redact_text(value: str) -> str:
    value = _INLINE_PATTERNS[0].sub(r"\1=[REDACTED]", value)
    value = _INLINE_PATTERNS[1].sub(r"\1[REDACTED]\3", value)
    return _INLINE_PATTERNS[2].sub(r"\1[REDACTED]", value)


def redact(value: Any, key: str | None = None) -> Any:
    if key and _SENSITIVE_KEYS.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {k: redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": redact_text(record.getMessage()),
        }
        if (rid := request_id_ctx.get()) is not None:
            payload["request_id"] = rid
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = redact(value, key)
        if record.exc_info:
            # Only the exception type and a redacted message; no frames, no local variables.
            exc = record.exc_info[1]
            payload["exception"] = f"{type(exc).__name__}: {redact_text(str(exc))[:500]}"
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # httpx logs full request URLs, which may contain OAuth codes in query strings.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
