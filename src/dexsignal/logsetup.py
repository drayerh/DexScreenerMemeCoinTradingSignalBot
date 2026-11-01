"""Logging: secret redaction and optional JSON output (REVIEW findings 12, 18)."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from datetime import datetime, timezone

REDACTED = "***"


class RedactingFilter(logging.Filter):
    """Removes configured secrets from every log message, including exception text in args."""

    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        self._secrets = sorted({s for s in secrets if s}, key=len, reverse=True)

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if self._secrets:
            record.msg = self.redact(record.getMessage())
            record.args = ()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {
                "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="seconds"),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
        )


def configure(level: int, fmt: str, secrets: Iterable[str]) -> None:
    handler = logging.StreamHandler()
    handler.addFilter(RedactingFilter(secrets))
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
