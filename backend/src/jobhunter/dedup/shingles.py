"""Text confirmation of a probable duplicate (spec §8.7 G5): 5-word shingles over `fold(description)`.

A text's words are the runs of word characters of its folded form (`normalize.text.fold`), so case, accents,
`ß` and punctuation never separate two copies of one ad. A shingle is `n` consecutive words joined by a
space; a text of fewer than `n` words has none.

`text_confirms(a, b)` applies the G5 thresholds to two descriptions with their completeness, after a trailing
`…` (an aggregator's truncation mark, or `...`, its NFKC form) is removed from each:

- a side with fewer than `MIN_SHINGLES` shingles requires the exact equality of the folded texts (score 1.0,
  or 0.0);
- **full vs full**: Jaccard ≥ 0.6 or containment of the smaller set in the larger ≥ 0.8; the score is the
  better of the two measures;
- **partial vs full**: containment of the partial's shingles in the full's ≥ 0.7, in either argument order;
- **partial vs partial**: containment of the smaller set in the larger ≥ 0.9.

It returns whether the pair confirms and its score, which `groups` compares when a job qualifies for two
groups ("the higher G5 score").
"""

import re
from dataclasses import dataclass
from typing import Final, Literal

from jobhunter.normalize.text import fold

SHINGLE_WORDS: Final = 5
MIN_SHINGLES: Final = 20  # a side with fewer shingles requires exact folded equality
FULL_JACCARD: Final = 0.6
FULL_CONTAINMENT: Final = 0.8
PARTIAL_IN_FULL: Final = 0.7
PARTIAL_CONTAINMENT: Final = 0.9
_TRUNCATIONS: Final = ("…", "...")  # the mark, and its NFKC form (`norm_text`)
_WORD: Final = re.compile(r"\w+")

Completeness = Literal["full", "partial"]


@dataclass(frozen=True)
class TextSide:
    """One side of a G5 comparison: a description and its completeness."""

    text: str
    completeness: Completeness


def _words(text: str) -> list[str]:
    return _WORD.findall(fold(text))


def shingles(text: str, n: int = SHINGLE_WORDS) -> frozenset[str]:
    """The distinct `n`-word shingles of `fold(text)`'s words (none when it has fewer than `n` words)."""
    words = _words(text)
    return frozenset(" ".join(words[i : i + n]) for i in range(len(words) - n + 1))


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """`len(a & b) / len(a | b)`; 0.0 when both are empty."""
    union = len(a | b)
    return len(a & b) / union if union else 0.0


def containment(small: frozenset[str], big: frozenset[str]) -> float:
    """The share of `small` that `big` contains, `len(small & big) / len(small)`; 0.0 for an empty `small`."""
    return len(small & big) / len(small) if small else 0.0


def _untruncated(text: str) -> str:
    stripped = text.rstrip()
    for mark in _TRUNCATIONS:
        if stripped.endswith(mark):
            return stripped.removesuffix(mark).rstrip()
    return stripped


def text_confirms(a: TextSide, b: TextSide) -> tuple[bool, float]:
    """Whether the two descriptions confirm a probable duplicate under G5 (module docstring); the score."""
    text_a, text_b = _untruncated(a.text), _untruncated(b.text)
    set_a, set_b = shingles(text_a), shingles(text_b)
    if len(set_a) < MIN_SHINGLES or len(set_b) < MIN_SHINGLES:
        equal = fold(text_a) == fold(text_b)
        return equal, 1.0 if equal else 0.0
    small, big = (set_a, set_b) if len(set_a) <= len(set_b) else (set_b, set_a)
    if a.completeness == "full" and b.completeness == "full":
        score = max(jaccard(set_a, set_b), containment(small, big))
        return jaccard(set_a, set_b) >= FULL_JACCARD or containment(small, big) >= FULL_CONTAINMENT, score
    if a.completeness == "partial" and b.completeness == "partial":
        score = containment(small, big)
        return score >= PARTIAL_CONTAINMENT, score
    partial, full = (set_a, set_b) if a.completeness == "partial" else (set_b, set_a)
    score = containment(partial, full)
    return score >= PARTIAL_IN_FULL, score
