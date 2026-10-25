"""Text primitives (spec §8.1): `norm_text`, `unaccent` and `fold`, and `fixed_point`, a normaliser applied
until the value no longer changes (`fold` and `normalize.companies.company_key` are idempotent on ordinary
text, but not on a few inputs: a spacing diacritic, a name ending in two country words).

`fold(s)` is defined in the specification as `immutable_unaccent(norm_text(s)).casefold()`. The database
computes `immutable_unaccent`; this module computes the same function in Python for text that never reaches
the database (title and company keys, description shingles). `unaccent` is NFKD with the combining marks
removed, plus a map of the letters and punctuation that PostgreSQL's `unaccent.rules` transliterates and NFKD
does not decompose.

The parity is tested, not assumed: `tests/integration/test_fold_matches_db.py` compares `fold` with the
database code point by code point over Basic Latin, Latin-1 Supplement, Latin Extended-A, -B and Additional,
the standard combining diacritics, Greek and Coptic, and General Punctuation. Outside those blocks the rules
file differs from NFKD and `unaccent` does not follow it: it leaves IPA letters, arrows and CJK punctuation
alone and strips accents from Cyrillic and Hebrew letters that the rules file keeps. When the comparison
shows a character of a pinned block that differs, extend `_TRANSLITERATIONS` (or `_UNCHANGED`); do not
change the test.
"""

import functools
import re
import unicodedata
from collections.abc import Callable
from typing import Final

# Spec §8.1 step 2: soft hyphen, zero-width space, non-joiner, joiner and the byte order mark are removed.
_REMOVED: Final = dict.fromkeys(
    map(
        ord,
        "\xad\N{ZERO WIDTH SPACE}\N{ZERO WIDTH NON-JOINER}\N{ZERO WIDTH JOINER}\N{ZERO WIDTH NO-BREAK SPACE}",
    )
)
_WHITESPACE: Final = re.compile(r"\s+")


def norm_text(s: str) -> str:
    """Unicode NFKC, remove U+00AD, U+200B-U+200D and U+FEFF, NBSP and U+202F to a space, collapse, trim."""
    s = unicodedata.normalize("NFKC", s)
    s = s.translate(_REMOVED)
    s = s.replace("\xa0", " ").replace("\N{NARROW NO-BREAK SPACE}", " ")
    return _WHITESPACE.sub(" ", s).strip()


# What PostgreSQL's `unaccent.rules` (PostgreSQL 17) transliterates to ASCII and NFKD leaves alone, as
# (characters, replacement) pairs: the letters without a decomposition, the typographic punctuation and the
# few symbols of the pinned blocks. Capitals keep their case, as in the rules file.
_TRANSLITERATIONS: Final[tuple[tuple[str, str], ...]] = (
    # Ligatures and special letters of the European alphabets.
    ("ß", "ss"),
    ("ẞ", "SS"),
    ("æ", "ae"),
    ("Æ", "AE"),
    ("œ", "oe"),
    ("Œ", "OE"),
    ("ø", "o"),
    ("Ø", "O"),
    ("ł", "l"),
    ("Ł", "L"),
    ("đ", "d"),
    ("Đ", "D"),
    ("þ", "th"),
    ("Þ", "TH"),
    ("ð", "d"),
    ("Ð", "D"),
    ("\u0131", "i"),
    ("ĸ", "q"),
    ("ƕ", "hv"),
    ("Ƣ", "OI"),
    ("ƣ", "oi"),
    ("ȸ", "db"),
    ("ȹ", "qp"),
    ("Ỻ", "LL"),
    ("ỻ", "ll"),
    # Letters with a stroke, bar, hook or tail.
    ("Ⱥ", "A"),
    ("ƁƂɃ", "B"),
    ("ƀƃ", "b"),
    ("ƇȻ", "C"),
    ("ƈȼ", "c"),
    ("ƉƊƋ", "D"),
    ("ƌȡ", "d"),
    ("ƐɆ", "E"),
    ("ɇ", "e"),
    ("Ƒ", "F"),
    ("ƒ", "f"),
    ("ƓǤ", "G"),
    ("ǥ", "g"),
    ("Ħ", "H"),
    ("ħ", "h"),
    ("ƖƗ", "I"),
    ("Ɉ", "J"),
    ("ȷɉ", "j"),
    ("Ƙ", "K"),
    ("ƙ", "k"),
    ("Ƚ", "L"),
    ("ƚȴ", "l"),
    ("ŊƝ", "N"),
    ("ŋƞȵ", "n"),
    ("Ƥ", "P"),
    ("ƥ", "p"),
    ("Ɍ", "R"),
    ("ɍ", "r"),
    ("ȿẜẝ", "s"),
    ("ŦƬƮȾ", "T"),
    ("ŧƫƭȶ", "t"),
    ("Ʉ", "U"),
    ("ƲỼ", "V"),
    ("ỽ", "v"),
    ("ƳɎỾ", "Y"),
    ("ƴɏỿ", "y"),
    ("ƵȤ", "Z"),
    ("ƶȥɀ", "z"),
    # Punctuation and symbols: quotes, dashes, guillemets, signs.
    ("\u034f", ""),  # combining grapheme joiner
    ("\xa1", "!"),
    ("\xbf", "?"),
    ("\xa9", "(C)"),
    ("\xae", "(R)"),
    ("\xb1", "+/-"),
    ("\xd7\u204e", "*"),  # multiplication sign, low asterisk
    ("\xf7\u2044", "/"),  # division sign, fraction slash
    ("\u2010\u2012\u2013\u2014\u2015", "-"),  # hyphen, figure dash, en dash, em dash, horizontal bar
    ("\u2016", "||"),  # double vertical line
    ("\xab", "<<"),
    ("\xbb", ">>"),
    ("\u2039", "<"),
    ("\u203a", ">"),
    ("\u2045", "["),
    ("\u2046", "]"),
    ("\u02b9\u02bb\u02bc\u02bd\u2018\u2019\u201b\u2032", "'"),  # primes, apostrophes and single quotes
    ("\u02ba\u201c\u201d\u201f", '"'),  # double prime and double quotes
    ("\u201a", ","),
    ("\u201e", ",,"),
)
_MAP: Final = {char: replacement for chars, replacement in _TRANSLITERATIONS for char in chars}

# Letters that PostgreSQL leaves alone although NFKD would reduce them: a diacritic on a letter that is not a
# plain Latin or Greek letter (the macron, acute and caron of AE, O with stroke and EZH).
_UNCHANGED: Final = frozenset("ǢǣǮǯǼǽǾǿ")


@functools.cache
def _unaccent_char(char: str) -> str:
    if char in _UNCHANGED:
        return char
    decomposed = unicodedata.normalize("NFKD", char)
    bare = "".join(c for c in decomposed if not unicodedata.combining(c))
    # NFC puts back what NFKD only split without removing a mark (a Hangul syllable, for example).
    return unicodedata.normalize("NFC", "".join(_MAP.get(c, c) for c in bare))


def unaccent(s: str) -> str:
    """NFKD with the combining marks removed and the PostgreSQL ligature and special-letter map applied."""
    if s.isascii():
        return s
    return "".join(map(_unaccent_char, s))


def fold(s: str) -> str:
    """`unaccent(norm_text(s)).casefold()`: the comparison form of a string (`ß` becomes `ss`)."""
    return unaccent(norm_text(s)).casefold()


FIXED_POINT_ROUNDS: Final = 8


def fixed_point(normalize: Callable[[str], str], value: str) -> str | None:
    """`normalize` applied to `value` until the result no longer changes, at most `FIXED_POINT_ROUNDS` times;
    `None` when it still changes after that (no stable normal form)."""
    for _ in range(FIXED_POINT_ROUNDS):
        again = normalize(value)
        if again == value:
            return value
        value = again
    return None
