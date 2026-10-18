"""SerpApi Google Jobs `extensions[]` and `detected_extensions` (spec §5.3.2), German and English.

Both are parsed. The structured `detected_extensions` values are read first, then the chips of `extensions[]`
fill what is still missing: the first posted date and the first salary win, work modes and employment types
are collected (each employment type once).
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from jobhunter.normalize.text import fold
from jobhunter.sources.parsing.dates import ParsedDate, parse_relative
from jobhunter.sources.parsing.employment import EmploymentType, parse_employment
from jobhunter.sources.parsing.salary import Salary, parse_salary

# `Homeoffice`, `Home Office`, `Work from home` and `Remote` say that remote work is possible, not that the
# job is fully remote, hence both modes.
_REMOTE: Final = re.compile(r"\bhome[- ]?office\b|\bwork from home\b|\bremote\b")
_REMOTE_MODES: Final = frozenset({"remote", "hybrid"})


@dataclass(frozen=True)
class Extensions:
    """What the chips say. `None` and empty values mean "not stated" (unknown), never "no"."""

    posted: ParsedDate | None
    work_modes: frozenset[str] | None
    employment: tuple[EmploymentType, ...]
    salary: Salary | None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def parse_extensions(
    items: Sequence[str], detected: Mapping[str, object] | None, *, reference: datetime
) -> Extensions:
    """Parse the `extensions[]` chips and `detected_extensions`; `reference` is the response `Date`."""
    chips = list(items)
    structured = detected or {}
    posted_at = _text(structured.get("posted_at"))
    schedule = _text(structured.get("schedule_type"))
    pay = _text(structured.get("salary"))
    # The structured values come first, so they win where the two sources disagree.
    ordered = [text for text in (posted_at, schedule, pay) if text is not None] + chips

    posted: ParsedDate | None = None
    salary: Salary | None = None
    employment: list[EmploymentType] = []
    remote = structured.get("work_from_home") is True
    for chip in ordered:
        if posted is None:
            posted = parse_relative(chip, reference=reference)
        if salary is None:
            salary = parse_salary(chip)
        employment.extend(kind for kind in parse_employment(chip) if kind not in employment)
        remote = remote or _REMOTE.search(fold(chip)) is not None
    return Extensions(
        posted=posted,
        work_modes=_REMOTE_MODES if remote else None,
        employment=tuple(employment),
        salary=salary,
    )
