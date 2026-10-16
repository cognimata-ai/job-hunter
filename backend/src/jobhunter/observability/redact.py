"""Redaction of log events (spec §16.8): by key, and by pattern inside string values.

`redact_event` is the structlog processor that runs last before rendering. It redacts the values of
sensitive keys, and masks e-mail addresses, phone numbers, key-like tokens, secrets in query strings and
the loaded secret values in every string, in nested dicts, lists, tuples and sets, in nested dict keys, and
in the string form of any other object. It cannot know a secret that has neither a listed key, nor a
recognisable shape, nor a registration with `register_secret_values`; callers must not log such values.

`redact_event` never raises: a cyclic or over-deep container renders as `[RECURSION]`, and any other
failure replaces the whole event with a fail-closed one that carries no logged data.
"""

import re
import threading
from collections.abc import Iterable, MutableMapping
from typing import Any, Final
from urllib.parse import quote, quote_plus

from jobhunter.secret_names import SECRET_NAMES

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
# Deliberately suffixes and not substrings: `input_tokens`, `max_tokens` and `dedup_key` are not secrets.
REDACT_SUFFIXES: Final[tuple[str, ...]] = ("_api_key", "_app_key", "_secret", "_password", "_token")

REDACTED: Final = "[REDACTED]"
# Strings longer than this are cut before any pattern runs, which bounds the cost of every pattern. The
# head and the tail stay, so that the last line of a traceback survives.
MAX_TEXT_LENGTH: Final = 16384
TRUNCATION_MARKER: Final = "…[TRUNCATED]…"
# Containers nested deeper than this, and containers that contain themselves, render as `[RECURSION]`.
MAX_DEPTH: Final = 32
RECURSION: Final = "[RECURSION]"
# What a failed redaction keeps of the event: values that structlog itself sets.
_FAIL_CLOSED_KEYS: Final = ("ts", "level", "logger")
# Structlog sets these two itself at the top level of the event; they are never user text there.
_UNMASKED_TOP_LEVEL_KEYS: Final = frozenset({"ts", "level"})

UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
# The local part and the domain are length-bounded (RFC 5321), which keeps the search linear.
EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,255}\.[A-Za-z]{2,}")
PHONE = re.compile(r"(?<![\w+\-])(?:\+\d{1,3}[ \d/()\-]{6,}\d|0\d{2,5}[ /\-]\d{3,}(?:[ \-]\d{2,})*)(?!\w)")
KEY = re.compile(r"\b(?:sk-[A-Za-z0-9_\-]{20,}|lsv2_[A-Za-z0-9_]{20,}|[A-Za-z0-9+/_\-]{40,}={0,2})")
# A long run of lower-case words joined by underscores is an identifier (an event name), not a key.
_SNAKE_CASE_WORDS = re.compile(r"[a-z]+(?:_[a-z]+)+")
QUERY_SECRET = re.compile(
    r"(?i)([?&](?:app_id|app_key|api_key|apikey|key|token|access_token|secret|password)=)[^&\s'\"#]+"
)

# A capturing group makes `split` keep the UUID spans, at the odd indices.
_SPLIT_ON_UUID = re.compile(f"({UUID.pattern})")

_MIN_SECRET_LENGTH: Final = 8
_secret_lock = threading.Lock()
_secret_values: set[str] = set()
_secret_pattern: re.Pattern[str] | None = None


def register_secret_values(values: Iterable[str]) -> None:
    """Make `mask_text` replace every verbatim occurrence of these values with `[SECRET]`.

    Values are stripped; those shorter than 8 characters after stripping are ignored, as they would
    mask ordinary words. The percent-encoded forms (`quote`, `quote_plus`) are registered as well, as
    they appear in connection strings and URLs. Registrations accumulate over calls.
    """
    global _secret_pattern
    new: set[str] = set()
    for value in values:
        if len(stripped := value.strip()) >= _MIN_SECRET_LENGTH:
            new.update((stripped, quote(stripped, safe=""), quote_plus(stripped)))
    with _secret_lock:
        _secret_values.update(new)
        if _secret_values:
            # Longest first, so that a value that contains another one is replaced as a whole.
            ordered = sorted(_secret_values, key=lambda value: (-len(value), value))
            _secret_pattern = re.compile("|".join(re.escape(value) for value in ordered))


def _reset_secret_values() -> None:
    """Forget every registered value (for tests)."""
    global _secret_pattern
    with _secret_lock:
        _secret_values.clear()
        _secret_pattern = None


def _mask_key_like(match: re.Match[str]) -> str:
    token = match.group(0)
    return token if _SNAKE_CASE_WORDS.fullmatch(token) else "[KEY]"


def mask_text(value: str) -> str:
    """Mask secrets and personal data in `value`; canonical UUIDs stay as they are.

    Order: truncate (keeping head and tail), registered secret values, e-mail addresses (on the whole
    string, so a UUID in the local part is masked with it), secrets in query strings, then phone numbers
    and key-like tokens in the parts between UUIDs.
    """
    if len(value) > MAX_TEXT_LENGTH:
        half = MAX_TEXT_LENGTH // 2
        value = value[:half] + TRUNCATION_MARKER + value[-half:]
    if (secrets := _secret_pattern) is not None:
        value = secrets.sub("[SECRET]", value)
    value = EMAIL.sub("[EMAIL]", value)
    value = QUERY_SECRET.sub(rf"\1{REDACTED}", value)
    parts = _SPLIT_ON_UUID.split(value)
    for i in range(0, len(parts), 2):
        parts[i] = KEY.sub(_mask_key_like, PHONE.sub("[PHONE]", parts[i]))
    return "".join(parts)


def _is_sensitive_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    folded = key.lower()
    return (
        folded in REDACT_KEYS
        or folded in SECRET_NAMES
        or folded.startswith(REDACT_PREFIXES)
        or folded.endswith(REDACT_SUFFIXES)
    )


def _redact_mapping(data: dict[Any, Any], *, top_level: bool, active: set[int]) -> dict[Any, Any]:
    out: dict[Any, Any] = {}
    for key, value in data.items():
        if _is_sensitive_key(key):
            out[key] = REDACTED
            continue
        # Keys of the event itself are keyword names; keys of nested dicts are data and can be anything.
        out_key = key if top_level or not isinstance(key, str) else mask_text(key)
        out[out_key] = (
            value if top_level and key in _UNMASKED_TOP_LEVEL_KEYS else _redact_value(value, active)
        )
    return out


def _redact_value(value: Any, active: set[int]) -> Any:
    """Return `value` with every string masked. Containers are rebuilt, never changed in place.

    `active` holds the ids of the containers on the path from the event to `value`.
    """
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return mask_text(value)
    if not isinstance(value, dict | list | tuple | set | frozenset):
        return mask_text(str(value))
    if id(value) in active or len(active) >= MAX_DEPTH:
        return RECURSION
    active.add(id(value))
    try:
        if isinstance(value, dict):
            return _redact_mapping(value, top_level=False, active=active)
        return [_redact_value(item, active) for item in value]
    finally:
        active.discard(id(value))


def redact_event(logger: object, method: str, event: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    """Structlog processor: redact listed keys and mask patterns in all strings, recursively.

    Never raises. If redaction fails, the result is `ts`, `level` and `logger` (those that are present
    and plain strings), `event="redaction_failed"` and the exception's class name: nothing else of the
    event, and no exception message, as either could carry what was to be redacted.
    """
    try:
        return _redact_mapping(dict(event), top_level=True, active=set())
    except Exception as exc:
        failed: dict[str, Any] = {key: event[key] for key in _FAIL_CLOSED_KEYS if type(event.get(key)) is str}
        failed["event"] = "redaction_failed"
        failed["error_type"] = type(exc).__name__
        return failed
