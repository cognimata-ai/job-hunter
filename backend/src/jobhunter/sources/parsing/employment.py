"""Employment types (spec §5.3.1-§5.3.3): the JSearch enum, German and English text, Adzuna's fields."""

import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, Literal

from jobhunter.normalize.text import fold

EmploymentType = Literal[
    "full_time",
    "part_time",
    "contract",
    "temporary",
    "internship",
    "working_student",
    "apprenticeship",
    "freelance",
]

JSEARCH_TYPES: Final[Mapping[str, EmploymentType]] = MappingProxyType(
    {
        "FULLTIME": "full_time",
        "PARTTIME": "part_time",
        "CONTRACTOR": "contract",
        "INTERN": "internship",
    }
)

# Patterns over `fold`ed text (lower case, no accents). `unbefristet` (permanent) is not `befristet`.
_PHRASES: Final[tuple[tuple[EmploymentType, re.Pattern[str]], ...]] = (
    ("full_time", re.compile(r"\bvollzeit|\bfull[- ]?time\b")),
    ("part_time", re.compile(r"\bteilzeit|\bpart[- ]?time\b")),
    ("contract", re.compile(r"\bvertrag\b|\bcontract(?:or)?\b")),
    ("temporary", re.compile(r"\bbefristet|\btemporary\b")),
    ("internship", re.compile(r"praktikum|\binternship\b")),
    ("working_student", re.compile(r"\bwerkstudent|\bworking[- ]student\b")),
    ("apprenticeship", re.compile(r"\bausbildung\b|\bapprenticeship\b")),
    ("freelance", re.compile(r"\bfreelanc|\bfreiberuf")),
)


def parse_employment(text: str) -> list[EmploymentType]:
    """The employment types named in `text`, in order of first mention, each once."""
    folded = fold(text)
    found = sorted((m.start(), kind) for kind, pattern in _PHRASES if (m := pattern.search(folded)))
    return [kind for _, kind in found]


def adzuna_employment(contract_time: str | None, contract_type: str | None) -> list[EmploymentType]:
    """Adzuna's `contract_time` (`full_time`, `part_time`) and `contract_type` (`permanent`, `contract`)."""
    types: list[EmploymentType] = []
    match (contract_time or "").strip().casefold():
        case "full_time":
            types.append("full_time")
        case "part_time":
            types.append("part_time")
    if (contract_type or "").strip().casefold() == "contract":
        types.append("contract")
    return types
