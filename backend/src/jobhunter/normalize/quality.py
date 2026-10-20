"""Description quality gate (spec §8.9).

The gate measures the description as it will be stored: one `norm_text`-normalised line per non-empty line, so
indentation and blank lines do not count as characters.
"""

from typing import Final, Literal

from jobhunter.normalize.text import norm_text

Quality = Literal["ok", "short", "garbage"]

SHORT_BELOW: Final = 200
GARBAGE_BELOW: Final = 60
MAX_NON_LETTER_SHARE: Final = 0.4
MAX_PLACEHOLDER_LINES: Final = 2
_PLACEHOLDERS: Final = frozenset({"null", "undefined"})


def description_quality(text: str) -> Quality:
    """`garbage`: >= 3 `null`/`undefined` lines, > 40% non-letters or < 60 characters; `short`: < 200."""
    lines = [line for line in map(norm_text, text.splitlines()) if line]
    if sum(line.casefold() in _PLACEHOLDERS for line in lines) > MAX_PLACEHOLDER_LINES:
        return "garbage"
    normalised = "\n".join(lines)
    length = len(normalised)
    if length < GARBAGE_BELOW:
        return "garbage"
    non_letters = length - sum(char.isalpha() for char in normalised)
    if non_letters > MAX_NON_LETTER_SHARE * length:
        return "garbage"
    return "short" if length < SHORT_BELOW else "ok"
