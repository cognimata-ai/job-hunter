"""Redaction of log events (spec §16.8): by key, and by pattern inside every string value.

`redact_event` is the last structlog stage before rendering, so no field reaches the output unmasked.
"""

import re
from collections.abc import MutableMapping
from typing import Any, Final

REDACT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "authorization",
        "cookie",
        "set-cookie",
        "x-rapidapi-key",
        "api_key",
        "app_key",
        "app_id",
        "password",
        "token",
        "secret",
        "redirect_url",
        "outbound_url_private",
        "stripped_text",
        "reviewed_text",
    }
)
REDACT_PREFIXES: Final[tuple[str, ...]] = ("description", "profile", "cv", "raw_text")

REDACTED: Final = "[REDACTED]"
# Structlog sets these two itself; they are never user text.
_UNMASKED_KEYS: Final = frozenset({"ts", "level"})

UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE = re.compile(r"(?<![\w+\-])(?:\+\d{1,3}[ \d/()\-]{6,}\d|0\d{2,5}[ /\-]\d{3,}(?:[ \-]\d{2,})*)(?!\w)")
KEY = re.compile(r"\b(?:sk-[A-Za-z0-9_\-]{20,}|lsv2_[A-Za-z0-9_]{20,}|[A-Za-z0-9+/_\-]{40,}={0,2})")

# A capturing group makes `split` keep the UUID spans, at the odd indices.
_SPLIT_ON_UUID = re.compile(f"({UUID.pattern})")


def mask_text(value: str) -> str:
    """Replace e-mail addresses, phone numbers and key-like tokens; canonical UUIDs stay as they are."""
    parts = _SPLIT_ON_UUID.split(value)
    for i in range(0, len(parts), 2):
        parts[i] = KEY.sub("[KEY]", PHONE.sub("[PHONE]", EMAIL.sub("[EMAIL]", parts[i])))
    return "".join(parts)


def _is_sensitive_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    folded = key.lower()
    return folded in REDACT_KEYS or folded.startswith(REDACT_PREFIXES)


def _redact_mapping(data: dict[Any, Any]) -> dict[Any, Any]:
    out: dict[Any, Any] = {}
    for key, value in data.items():
        if _is_sensitive_key(key):
            out[key] = REDACTED
        elif key in _UNMASKED_KEYS:
            out[key] = value
        else:
            out[key] = _redact_value(value)
    return out


def _redact_value(value: Any) -> Any:
    """Return `value` with every string masked. Containers are rebuilt, never changed in place."""
    if isinstance(value, str):
        return mask_text(value)
    if isinstance(value, dict):
        return _redact_mapping(value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    return value


def redact_event(logger: object, method: str, event: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    """Structlog processor: redact listed keys, mask patterns in all strings, recursively."""
    return _redact_mapping(dict(event))
