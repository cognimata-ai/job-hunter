"""The privacy statement's operator details (spec §20.6, §19.2; M5 follow-ups Task F1, Ruling FU-1).

The five values of `docs/PRIVACY.md`'s Operator section (the operator, the contact address, the controller,
the VPS provider and the S3 backup provider) are personal, so they never enter the repository or an image:
the statement keeps its five markers, and the deployment's operator file supplies the values, which the SPA
puts in their place (`GET /api/privacy/operator`). On the VPS the file is `/etc/jobhunter/operator.json`,
root's, mode 0644, mounted read-only into the api only, at `/run/config/operator.json`
(`config.DEFAULT_OPERATOR_FILE`, the setting `JOBHUNTER_OPERATOR_FILE`).

**The file** is a JSON object with exactly the keys of `FIELDS`, each a string that is not blank, of at most
`MAX_CHARS` characters, with no control character (Unicode `Cc`, and the line and paragraph separators) and
no lone surrogate. `load` reads at most `MAX_BYTES + 1` bytes of it: the details, `None` when there is no
file at the path, else `OperatorFileInvalid`, whose `reason` names the problem: a key of `FIELDS` or a rule,
never a value and never an unknown key (which may be a value typed in the wrong place). It is raised without
its cause, since a JSON parser's error keeps the whole text.

**When it is read.** The api reads it once, at its start (`at_start`, from the lifespan or `create_app`), so
a changed file applies after `restart api`. In prod the startup check `operator_file`
(`config.run_startup_assertions`) has refused to start the api without a valid file (fail-closed, Ruling
FU-1), and `at_start` raises for one gone or broken since. In dev and test the file is optional: without a
valid one the statement shows its markers, and an invalid one is logged `operator_file_invalid` with its path
and the problem.

No log line, metric, export or trace carries a value: `OperatorDetails`' repr shows none.
"""

import json
import os
import stat
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from jobhunter.observability.logging import get_logger

FIELDS: Final = ("operator", "contact", "controller", "vps_provider", "s3_provider")
MAX_CHARS: Final = 200
# Five values of 200 characters, each escaped as `\uXXXX`, are about 6 KiB: the limit leaves room for any
# layout, and bounds what is read.
MAX_BYTES: Final = 16384
# What a value must not hold: control characters (C0, DEL, C1) and the line and paragraph separators.
_CONTROL_CATEGORIES: Final = frozenset({"Cc", "Zl", "Zp"})
_READ_FLAGS: Final = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK

log = get_logger("jobhunter.privacy.operator")


class OperatorFileInvalid(Exception):
    """The operator file cannot be used; `reason` names the problem (module docstring), never a value."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason

    def __str__(self) -> str:
        return self.reason


@dataclass(frozen=True, slots=True)
class OperatorDetails:
    """The five values, as the file has them. None of them is in the repr."""

    operator: str = field(repr=False)
    contact: str = field(repr=False)
    controller: str = field(repr=False)
    vps_provider: str = field(repr=False)
    s3_provider: str = field(repr=False)


class _DuplicateKey(ValueError):
    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    found: dict[str, object] = {}
    for key, value in pairs:
        if key in found:
            raise _DuplicateKey(key)
        found[key] = value
    return found


def _read(path: Path) -> bytes | None:
    """At most `MAX_BYTES + 1` bytes of the regular file at `path`; `None` for no file. Opened without
    blocking, so a FIFO in its place is refused rather than waited on."""
    try:
        fd = os.open(path, _READ_FLAGS)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError:
        raise OperatorFileInvalid("cannot be read") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OperatorFileInvalid("is not a regular file")
        data = b""
        while len(data) <= MAX_BYTES and (chunk := os.read(fd, MAX_BYTES + 1 - len(data))):
            data += chunk
        return data
    except OSError:
        raise OperatorFileInvalid("cannot be read") from None
    finally:
        os.close(fd)


def _check_value(key: str, value: object) -> str:
    if not isinstance(value, str):
        raise OperatorFileInvalid(f"{key} must be a string")
    if not value.strip():
        raise OperatorFileInvalid(f"{key} must not be empty")
    if len(value) > MAX_CHARS:
        raise OperatorFileInvalid(f"{key} must be at most {MAX_CHARS} characters")
    categories = {unicodedata.category(char) for char in value}
    if categories & _CONTROL_CATEGORIES:
        raise OperatorFileInvalid(f"{key} must contain no control character")
    if "Cs" in categories:
        raise OperatorFileInvalid(f"{key} must be valid Unicode text")
    return value


def parse(raw: bytes) -> OperatorDetails:
    """The details of an operator file's bytes, or `OperatorFileInvalid` (module docstring)."""
    if len(raw) > MAX_BYTES:
        raise OperatorFileInvalid(f"is larger than {MAX_BYTES} bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise OperatorFileInvalid("is not valid UTF-8") from None
    try:
        document = json.loads(text, object_pairs_hook=_unique_keys)
    except _DuplicateKey as exc:
        key = exc.key if exc.key in FIELDS else None
        raise OperatorFileInvalid(f"has {key or 'a key'} more than once") from None
    except json.JSONDecodeError as exc:
        where = f"line {exc.lineno}, column {exc.colno}"
        raise OperatorFileInvalid(f"is not valid JSON ({where})") from None
    except RecursionError:  # arrays or objects nested thousands deep
        raise OperatorFileInvalid("is nested too deeply") from None
    if not isinstance(document, dict):
        raise OperatorFileInvalid("must be a JSON object")
    if missing := [key for key in FIELDS if key not in document]:
        raise OperatorFileInvalid(f"lacks {', '.join(missing)}")
    if other := len(document) - len(FIELDS):
        keys = "key" if other == 1 else "keys"
        raise OperatorFileInvalid(f"has {other} {keys} other than {', '.join(FIELDS)}")
    values = {key: _check_value(key, document[key]) for key in FIELDS}
    return OperatorDetails(**values)


def load(path: Path) -> OperatorDetails | None:
    """The details of the operator file at `path`; `None` when there is none; `OperatorFileInvalid` when it
    cannot be used."""
    raw = _read(path)
    return None if raw is None else parse(raw)


def at_start(path: Path, *, prod: bool) -> OperatorDetails | None:
    """What the api serves for its whole run, read when it starts (module docstring): the details; outside
    prod `None` for no file and, logged, for an invalid one; in prod `OperatorFileInvalid` for either."""
    try:
        details = load(path)
    except OperatorFileInvalid as exc:
        if prod:
            raise
        log.warning("operator_file_invalid", path=str(path), problem=exc.reason)
        return None
    if details is None and prod:
        raise OperatorFileInvalid("is missing")
    return details
