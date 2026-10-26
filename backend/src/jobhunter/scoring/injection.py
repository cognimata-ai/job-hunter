"""The V5 injection detector (spec §11.6 L4364): a deterministic scan of the ad text for text written to steer
an automated assessment. A pure function of the text: no connection, no clock, no logging (a hit is ad text).

`detect(text)` returns one `InjectionHit` per match of each pattern, in text order, named by the pattern's
stable name (`PATTERN_NAMES`). The patterns are exactly the spec's list, case-insensitive, German and English,
each spelled as a regular expression that matches the spec's phrase with any of its qualifiers in any number
and order (Codex r1 #43):

- `ignore_instructions`: `ignore (all|previous|above) instructions`, as `ignore`, any of `all`, `previous`,
  `above`, `prior` and `the`, then `instructions` ("Ignore all previous instructions" hits; Codex r1 #43);
- `ignoriere`: `ignoriere (alle|vorherige)`, as `ignoriere`, any of `alle`, `vorherige`, `vorherigen` and
  `obigen`, then optionally `anweisungen` or `instruktionen`;
- `system_prompt`: `system prompt`;
- `you_are_an_ai`: `you are (a|an) (ai|language model|assistant)`;
- `as_an_ai`: `as an ai`;
- `score_this`: `score this (job|ad)`;
- `rate_this_me`: `rate (this|me)`;
- `bewerte`: `bewerte (diese|dieses|mich)`;
- `give_full_marks`: `give (this|me) (100|10/10|full marks)`;
- the role tags `role_tag_system` (`<system>`, and its closing tag), `role_tag_assistant` (`assistant:`) and
  `role_tag_inst` (`[INST]`, and its closing tag);
- `zero_width_run`: more than 5 zero-width characters, a run of 6 or more of U+200B, U+200C, U+200D, U+2060
  and U+FEFF.

Words are separated by any whitespace, and a phrase starts (and, where it ends in a word, ends) at a word
boundary, so `accurate this` or `has an aim` is no hit. The `ignoriere` pattern is the plan's spelling, whose
qualifiers and noun are optional: it hits `ignoriere` followed by whitespace.

A hit, or the generator's `injection_observed`, sets the draft's `injection_suspected` (the validator): an
issue for the critic (T6) and a badge, never a failure and never a change of a rating.
"""

import re
from dataclasses import dataclass
from typing import Final

_ZERO_WIDTH: Final = (
    "\N{ZERO WIDTH SPACE}\N{ZERO WIDTH NON-JOINER}\N{ZERO WIDTH JOINER}\N{WORD JOINER}"
    "\N{ZERO WIDTH NO-BREAK SPACE}"
)
_SPELLINGS: Final[tuple[tuple[str, str], ...]] = (
    ("ignore_instructions", r"\bignore\s+(?:(?:all|previous|above|prior|the)\s+)*instructions\b"),
    (
        "ignoriere",
        r"\bignoriere\s+(?:(?:alle|vorherige|vorherigen|obigen)\s+)*(?:anweisungen|instruktionen)?",
    ),
    ("system_prompt", r"\bsystem\s+prompt"),
    ("you_are_an_ai", r"\byou\s+are\s+(?:a|an)\s+(?:ai|language\s+model|assistant)\b"),
    ("as_an_ai", r"\bas\s+an\s+ai\b"),
    ("score_this", r"\bscore\s+this\s+(?:job|ad)\b"),
    ("rate_this_me", r"\brate\s+(?:this|me)\b"),
    ("bewerte", r"\bbewerte\s+(?:diese|dieses|mich)\b"),
    ("give_full_marks", r"\bgive\s+(?:this|me)\s+(?:100|10/10|full\s+marks)\b"),
    ("role_tag_system", r"</?system>"),
    ("role_tag_assistant", r"\bassistant:"),
    ("role_tag_inst", r"\[/?INST\]"),
    ("zero_width_run", f"[{_ZERO_WIDTH}]{{6,}}"),
)
# Compiled once.
_PATTERNS: Final = tuple((name, re.compile(spelling, re.IGNORECASE)) for name, spelling in _SPELLINGS)
PATTERN_NAMES: Final = tuple(name for name, _ in _SPELLINGS)


@dataclass(frozen=True)
class InjectionHit:
    """One match: the pattern's stable name and the span `text[start:end]` it covers."""

    name: str
    start: int
    end: int


def detect(text: str) -> list[InjectionHit]:
    """Every match of every pattern in `text`, ordered by start, then by the order of `PATTERN_NAMES`."""
    hits = [
        InjectionHit(name, match.start(), match.end())
        for name, pattern in _PATTERNS
        for match in pattern.finditer(text)
    ]
    order = {name: i for i, name in enumerate(PATTERN_NAMES)}
    return sorted(hits, key=lambda hit: (hit.start, order[hit.name]))
