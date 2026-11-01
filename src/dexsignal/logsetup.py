"""Logging: secret redaction and optional JSON output (REVIEW findings 12, 18)."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from datetime import datetime, timezone
from urllib.parse import quote, quote_plus

REDACTED = "***"


class RedactingFilter(logging.Filter):
    """Removes configured secrets from every log message, including exception text in args."""

    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        # Also redact URL-encoded forms: keys appear encoded in request URLs, which
        # urllib3 logs on retries (review round 2, finding 5).
        variants = {v for s in secrets if s for v in (s, quote(s, safe=""), quote(s), quote_plus(s))}
        self._secrets = sorted(variants, key=len, reverse=True)
        self._formatter = logging.Formatter()

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        record.msg = self.redact(record.getMessage())
        record.args = ()
        if record.exc_info:
            # Render the traceback now so it can be redacted, then drop the live exception.
            record.exc_text = self.redact(self._formatter.formatException(record.exc_info))
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = self.redact(record.exc_text)
        if record.stack_info:
            record.stack_info = self.redact(record.stack_info)
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
