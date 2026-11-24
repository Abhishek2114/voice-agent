"""Logging with sensitive-data redaction (phone numbers, emails, secrets, tokens)."""

from __future__ import annotations

import logging
import re

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)(password|passwd|secret|token|api_key|authorization|cookie)(\"?\s*[:=]\s*\"?)[^\s\",&]+"),
     r"\1\2[REDACTED]"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[EMAIL]"),
    # E.164 (+919812345678), Indian 5+5 grouping with optional country code; deliberately does
    # not match IP addresses, ports, timestamps or dates
    (re.compile(r"\+\d{10,15}\b|(?<![\w:.\-])(?:\+\d{1,3}[\s-]?)?\d{5}[\s-]?\d{5}(?![\w:.\-])"), "[PHONE]"),
]  # fmt: skip


def redact(text: str) -> str:
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        record.msg = redact(msg)
        record.args = ()
        return True


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    for noisy in ("aioice", "aiortc", "httpx", "httpcore", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
