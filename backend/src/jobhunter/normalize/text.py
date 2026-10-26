"""Text primitives (spec §8.1): `norm_text`, `unaccent` and `fold`, and `fixed_point`, a normaliser applied
until the value no longer changes (`fold` and `normalize.companies.company_key` are idempotent on ordinary
text, but not on a few inputs: a spacing diacritic, a name ending in two country words).

`fold_with_offsets(s)` is `fold(s)` with its offset map (the V3 grounding, §11.6): for each folded character,
the index in `s` of the original character it came from. NFKC is the one step that reaches across
characters (a letter composes with its marks, Hangul jamo into a syllable), so `s` is cut into pieces whose
NFKC forms concatenate to `NFKC(s)`: a piece starts at each character of combining class 0, unless NFKC joins
it to the piece before (the two are then one piece). Every character a piece produces points at the piece's
first character. The concatenation is checked against `NFKC(s)`; if it ever differs, the pieces are cut
before ASCII characters only (no NFKC composition or reordering crosses one), and failing that `s` is one
piece. The other steps are per character (the removals, the no-break spaces, `unaccent`, `casefold`) or the
whitespace collapse and trim, applied to the characters with their indices, so the text is `fold(s)`
exactly.

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
_NO_BREAK_SPACES: Final = frozenset("\xa0\N{NARROW NO-BREAK SPACE}")


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


def _nfkc(s: str) -> str:
    return unicodedata.normalize("NFKC", s)


def _cut(s: str, starts: list[int]) -> list[tuple[int, str]]:
    """`s` cut at `starts` (ascending, the first 0), as (start, text)."""
    return [(start, s[start:end]) for start, end in zip(starts, [*starts[1:], len(s)], strict=False)]


def _segments(s: str) -> list[tuple[int, str]]:
    """`s` cut before every character of canonical combining class 0."""
    return _cut(s, [i for i, char in enumerate(s) if i == 0 or unicodedata.combining(char) == 0])


def _independent_pieces(s: str) -> list[tuple[int, str]]:
    """The segments of `s`, each joined to the piece before it when NFKC of the two is not the two NFKCs (an
    ASCII segment never is: no composition or reordering crosses an ASCII character)."""
    pieces: list[tuple[int, str]] = []
    for start, text in _segments(s):
        if pieces and not text[0].isascii():
            first, before = pieces[-1]
            if _nfkc(before + text) != _nfkc(before) + _nfkc(text):
                pieces[-1] = (first, before + text)
                continue
        pieces.append((start, text))
    return pieces


def _ascii_pieces(s: str) -> list[tuple[int, str]]:
    """`s` cut before every ASCII character."""
    return _cut(s, [i for i, char in enumerate(s) if i == 0 or char.isascii()])


def _normalized_pieces(s: str) -> list[tuple[int, str]]:
    """`(start, NFKC of the piece)` for pieces of `s` whose NFKC forms concatenate to `NFKC(s)`."""
    whole = _nfkc(s)
    for cut in (_independent_pieces, _ascii_pieces):
        normalized = [(start, _nfkc(text)) for start, text in cut(s)]
        if "".join(text for _, text in normalized) == whole:
            return normalized
    return [(0, whole)]


def fold_with_offsets(s: str) -> tuple[str, list[int]]:
    """`fold(s)` and, for each of its characters, the index in `s` of the original character it came from
    (see the module docstring); the indices never decrease."""
    chars: list[str] = []
    offsets: list[int] = []
    for start, text in _normalized_pieces(s):  # norm_text: NFKC, then the removals and the no-break spaces
        for char in text:
            if ord(char) in _REMOVED:
                continue
            chars.append(" " if char in _NO_BREAK_SPACES else char)
            offsets.append(start)
    normalized = "".join(chars)
    collapsed: list[tuple[str, int]] = []  # norm_text: every run of whitespace becomes one space
    position = 0
    for run in _WHITESPACE.finditer(normalized):
        collapsed += zip(normalized[position : run.start()], offsets[position : run.start()], strict=True)
        collapsed.append((" ", offsets[run.start()]))
        position = run.end()
    collapsed += zip(normalized[position:], offsets[position:], strict=True)
    first, last = 0, len(collapsed)  # norm_text: trimmed as `str.strip` trims
    while first < last and collapsed[first][0].isspace():
        first += 1
    while last > first and collapsed[last - 1][0].isspace():
        last -= 1
    folded: list[str] = []
    folded_offsets: list[int] = []
    for char, offset in collapsed[first:last]:  # unaccent and casefold, character by character
        for out in _unaccent_char(char).casefold():
            folded.append(out)
            folded_offsets.append(offset)
    return "".join(folded), folded_offsets
