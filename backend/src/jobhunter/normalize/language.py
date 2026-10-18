"""Ad language detection (spec §8.9): deterministic, no external model.

Over at least 50 tokens, the share of German stop words and the share of English stop words are compared. The
text is `de` when the German share is at least 0.2 above the English one, `en` for the reverse and `unknown`
otherwise. The lists are shipped as data files (`normalize/data/stopwords_de.txt` and `stopwords_en.txt`, one
word per line): `stopwords_en.txt` is PostgreSQL's `english.stop` (127 words), `stopwords_de.txt` the first
200 of the 231 words of its `german.stop`, in file order (the files carry no frequencies to trim by).
PostgreSQL 16 and 17 ship identical lists. Tokens and list entries are both `fold`ed, so `für` matches
`FUR` and `fur`.
"""

import functools
import re
from importlib import resources
from typing import Final, Literal

from jobhunter.normalize.text import fold

AdLanguage = Literal["de", "en", "unknown"]

MIN_TOKENS: Final = 50
# `de_ratio - en_ratio >= 0.2` is decided on the counts, `5 * (de - en) >= tokens`, so it is exact.
_MIN_GAP_DENOMINATOR: Final = 5
_TOKEN: Final = re.compile(r"[^\W\d_]+")


@functools.cache
def _stop_words(name: str) -> frozenset[str]:
    text = (resources.files("jobhunter.normalize") / "data" / name).read_text(encoding="utf-8")
    return frozenset(fold(word) for word in text.split("\n") if word)


def detect_language(text: str) -> AdLanguage:
    """`de`, `en` or `unknown` from the stop-word shares of the text's folded word tokens."""
    tokens = _TOKEN.findall(fold(text))
    if len(tokens) < MIN_TOKENS:
        return "unknown"
    german = _stop_words("stopwords_de.txt")
    english = _stop_words("stopwords_en.txt")
    gap = sum(token in german for token in tokens) - sum(token in english for token in tokens)
    if _MIN_GAP_DENOMINATOR * gap >= len(tokens):
        return "de"
    if -_MIN_GAP_DENOMINATOR * gap >= len(tokens):
        return "en"
    return "unknown"
