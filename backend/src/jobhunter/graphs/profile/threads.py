"""The names of the profile graphs' checkpoint threads (spec §9.1; M2 plan Deviation 27; split out of
`graph.py` by M5 Task 9 so that a module without LangGraph can read them).

`thread_id(kind, subject_id, operation_id, attempts)` is `profile-<draft_id>-t<operation_id>-a<attempts>` for
the text phase, `-p…` for the extract phase and `queries-<proposal_id>-q<operation_id>-a<attempts>` for a
proposal (`ThreadKind`). `THREAD_PATTERN` is the grammar of those names, built from the same prefix map, in a
form both Python's `re` and PostgreSQL's regular expressions read: one alternative per prefix with its kinds,
then the operation id, its first and only group, then the attempt; each number as `str(int)` writes it
(`NUMBER`). `thread_operation_id(thread)` reads the operation id back from a name `thread_id` wrote (`None`
for any other name), and `THREAD_PREFIXES` are the prefixes: the `profile_checkpoints` retention rule (Task
23, §20.1) and a manifest's `thread` bound (`privacy.manifests.THREAD_FORMS`) read a thread's owner by them.

`graph.py` re-exports every public name here.
"""

import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, Literal

type ThreadKind = Literal["t", "p", "q"]

_THREAD_PREFIX: Final[Mapping[ThreadKind, str]] = MappingProxyType(
    {"t": "profile", "p": "profile", "q": "queries"}
)
THREAD_PREFIXES: Final = frozenset(_THREAD_PREFIX.values())
NUMBER: Final = "(?:0|[1-9][0-9]*)"
THREAD_PATTERN: Final = (
    "(?:"
    + "|".join(
        f"{re.escape(prefix)}-{NUMBER}-[{''.join(k for k, p in _THREAD_PREFIX.items() if p == prefix)}]"
        for prefix in sorted(THREAD_PREFIXES)
    )
    + f")({NUMBER})-a{NUMBER}"
)
_THREAD_NAME: Final = re.compile(THREAD_PATTERN)


def thread_id(kind: ThreadKind, subject_id: int, operation_id: int, attempts: int) -> str:
    """The checkpoint thread of one execution (module docstring): the subject is the draft (`t`, `p`) or the
    proposal (`q`); `operation_id` and `attempts` are the claim's."""
    return f"{_THREAD_PREFIX[kind]}-{subject_id}-{kind}{operation_id}-a{attempts}"


def thread_operation_id(thread: str) -> int | None:
    """The operation id in the name of a thread `thread_id` wrote; `None` for any other name."""
    named = _THREAD_NAME.fullmatch(thread)
    return None if named is None else int(named[1])
