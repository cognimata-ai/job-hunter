"""Titles (spec §8.2): gender tags, abbreviations, separators, the folded form and the seniority tokens."""

import re
from dataclasses import dataclass
from typing import Final, Literal

from jobhunter.normalize.text import fold, norm_text

SeniorityToken = Literal[
    "intern", "working_student", "apprentice", "junior", "senior", "lead", "principal", "head"
]
# The §12.4 enum order. `mid` is the level without a token.
SENIORITY_LADDER: Final = (
    "intern",
    "working_student",
    "apprentice",
    "junior",
    "mid",
    "senior",
    "lead",
    "principal",
    "head",
)

# §8.2 step 2, first rule, verbatim: a parenthesised or bracketed list of gender tags.
GENDER_TAG: Final[re.Pattern[str]] = re.compile(
    r"[\(\[]\s*(m|w|f|d|x|div|gn|i|\*|all genders?|alle geschlechter)"
    r"(\s*[/|,_\-]\s*(m|w|f|d|x|div|gn|i|\*))*\s*[\)\]]",
    re.IGNORECASE,
)
# Second rule: a trailing bare `m/w/d` or `w/m/d`.
_BARE_TAG: Final = re.compile(r"(?<!\S)(?:m/w/d|w/m/d)\s*$", re.IGNORECASE)
# Third rule: a gender-inclusive suffix after a letter. `*in`, `*innen`, `:in`, `:innen`, `_in`, `/-in`, `/in`
# and, with the inflected plural, `_innen`, `/innen`, `/-innen` ignore the case. `In` and `Innen` count only
# after a lower-case letter (`EntwicklerIn`) and, like all of them, only before a word boundary: `Berlin`,
# `Admin` and `Plug-In` are not tagged.
_GENDER_SUFFIX: Final = re.compile(r"(?<=[^\W\d_])(?i:(?:[*:_]|/-?)in(?:nen)?)\b|(?<=[a-zäöüß])In(?:nen)?\b")

# §8.2 step 3. A period that is glued to the next word ("Sr.Engineer") leaves a space behind.
_SENIOR: Final = re.compile(r"\b(?:sr|snr)\b(?:\.(?P<glued>(?=\w))|\.?)", re.IGNORECASE)
_JUNIOR: Final = re.compile(r"\bjr\b(?:\.(?P<glued>(?=\w))|\.?)", re.IGNORECASE)

# §8.2 step 4: the separators are hyphen, en dash, em dash, pipe, comma and colon.
_SEPARATORS: Final = "-\N{EN DASH}\N{EM DASH}|,:"
_SEPARATOR_RUN: Final = re.compile(rf"(?:\s*[{re.escape(_SEPARATORS)}])+\s*")


def strip_gender_tags(s: str) -> str:
    """Remove the gender tags of §8.2 step 2 and tidy the whitespace they leave behind."""
    s = GENDER_TAG.sub(" ", s)
    s = _BARE_TAG.sub("", s)
    s = _GENDER_SUFFIX.sub("", s)
    return " ".join(s.split())


def _expand_abbreviations(s: str) -> str:
    s = _SENIOR.sub(lambda m: "senior" if m["glued"] is None else "senior ", s)
    return _JUNIOR.sub(lambda m: "junior" if m["glued"] is None else "junior ", s)


# §8.2 step 6. An entry matches as a whole word or phrase; an entry that ends in `*` also matches the longer
# words that start with it: `praktik*` covers Praktikum, Praktikant and Praktikantin, `teamleit*` covers
# Teamleiter, Teamleiterin and Teamleitung, `werkstudent*` covers Werkstudentin and Werkstudenten.
_SENIORITY_TABLE: Final[tuple[tuple[SeniorityToken, tuple[str, ...]], ...]] = (
    ("intern", ("praktik*", "intern", "internship")),
    ("working_student", ("werkstudent*", "working student")),
    ("apprentice", ("ausbildung", "azubi", "auszubildende", "apprentice", "trainee")),
    ("junior", ("junior", "jr", "einsteiger", "berufseinsteiger", "entry level", "graduate")),
    ("senior", ("senior", "sr", "snr")),
    ("lead", ("lead", "teamlead", "team lead", "teamleit*", "tech lead")),
    ("principal", ("principal", "staff")),
    ("head", ("head of", "leiter", "leiterin", "director", "vp")),
)


def _entry_pattern(entry: str) -> str:
    prefix = entry.endswith("*")
    words = re.escape(entry.removesuffix("*")).replace(r"\ ", r"\s+")
    return words + r"\w*" if prefix else words


_TOKEN_PATTERNS: Final = tuple(
    (token, re.compile(r"(?<!\w)(?:" + "|".join(map(_entry_pattern, entries)) + r")(?!\w)", re.IGNORECASE))
    for token, entries in _SENIORITY_TABLE
)


def seniority_tokens(title_norm: str) -> frozenset[SeniorityToken]:
    """The canonical seniority tokens found in a folded title (whole words or the prefixes of the table)."""
    return frozenset(token for token, pattern in _TOKEN_PATTERNS if pattern.search(title_norm))


@dataclass(frozen=True)
class NormalizedTitle:
    """`title` is the display form (steps 1-2, ends trimmed), `title_norm` the comparison form (steps 1-5)."""

    title: str
    title_norm: str
    seniority_tokens: frozenset[SeniorityToken]


def normalize_title(raw: str) -> NormalizedTitle:
    """§8.2 steps 1-6."""
    text = strip_gender_tags(norm_text(raw))
    title = text.strip(" " + _SEPARATORS)
    collapsed = _SEPARATOR_RUN.sub(" ", _expand_abbreviations(text))
    title_norm = fold(collapsed)
    return NormalizedTitle(title, title_norm, seniority_tokens(title_norm))
