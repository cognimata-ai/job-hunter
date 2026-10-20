import unicodedata

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from jobhunter.normalize import text
from jobhunter.normalize.text import fold, norm_text, unaccent

REMOVED = [
    "\N{SOFT HYPHEN}",
    "\N{ZERO WIDTH SPACE}",
    "\N{ZERO WIDTH NON-JOINER}",
    "\N{ZERO WIDTH JOINER}",
    "\N{ZERO WIDTH NO-BREAK SPACE}",
]


def test_norm_text_steps() -> None:
    # 1. NFKC: the ligature, a full-width letter and a superscript fold to their plain forms.
    assert norm_text("ﬁnd") == "find"
    assert norm_text("\N{FULLWIDTH LATIN CAPITAL LETTER A}BC") == "ABC"
    assert norm_text("m²") == "m2"
    # NFKC composes: "e" + combining acute becomes the single code point "é".
    assert norm_text("é") == "é"
    # 2. Soft hyphen, zero-width space/non-joiner/joiner and the BOM are removed, not replaced by a space.
    for ch in REMOVED:
        assert norm_text(f"soft{ch}ware") == "software"
    # The range is exactly U+200B-U+200D: U+200A (hair space) is whitespace, U+200E (LRM) is kept.
    assert norm_text("a\N{HAIR SPACE}b") == "a b"
    assert norm_text("a\N{LEFT-TO-RIGHT MARK}b") == "a\N{LEFT-TO-RIGHT MARK}b"
    # 3. NBSP and U+202F become a space.
    assert norm_text("a\N{NO-BREAK SPACE}b") == "a b"
    assert norm_text("a\N{NARROW NO-BREAK SPACE}b") == "a b"
    # 4. Runs of whitespace collapse to one space, whatever kind of whitespace.
    assert norm_text("a   b\t\tc\n\nd \r\n e") == "a b c d e"
    # Step 2 runs before step 4: a removed character between two spaces leaves a single space.
    assert norm_text("a \N{ZERO WIDTH SPACE} b") == "a b"
    # 5. The result is trimmed (including NBSP and removed characters at the ends).
    assert norm_text("  \N{NO-BREAK SPACE} a b \t\n") == "a b"
    assert norm_text("\N{ZERO WIDTH NO-BREAK SPACE} a \N{ZERO WIDTH SPACE}") == "a"
    assert norm_text(" \N{NO-BREAK SPACE}\N{ZERO WIDTH SPACE}\t") == ""
    assert norm_text("") == ""
    # Case and accents are not touched by norm_text.
    assert norm_text("Müller  Straße") == "Müller Straße"


@settings(deadline=None)
@given(st.text())
def test_norm_text_output_has_no_removed_or_irregular_whitespace(s: str) -> None:
    out = norm_text(s)
    assert not any(ch in out for ch in REMOVED)
    assert out == out.strip()
    assert "  " not in out
    assert not any(c.isspace() and c != " " for c in out)


UNACCENT_PAIRS = [
    ("ß", "ss"),
    ("æ", "ae"),
    ("œ", "oe"),
    ("ø", "o"),
    ("ł", "l"),
    ("đ", "d"),
    ("þ", "th"),
    ("ð", "d"),
    ("\N{LATIN SMALL LETTER DOTLESS I}", "i"),
    # The capital forms follow the PostgreSQL rules file: the whole replacement is upper case.
    ("Æ", "AE"),
    ("Œ", "OE"),
    ("Ø", "O"),
    ("Ł", "L"),
    ("Đ", "D"),
    ("Þ", "TH"),
    ("Ð", "D"),
    ("ẞ", "SS"),
    # NFKD with the combining marks removed.
    ("é", "e"),
    ("Ü", "U"),
    ("ñ", "n"),
    ("ç", "c"),
    ("ő", "o"),
    ("ž", "z"),
    ("ǎ", "a"),
    ("ǭ", "o"),
    ("Ǻ", "A"),
    ("ế", "e"),
    ("İ", "I"),
    ("é", "e"),
]


@pytest.mark.parametrize(("char", "plain"), UNACCENT_PAIRS)
def test_unaccent_map(char: str, plain: str) -> None:
    assert unaccent(char) == plain
    assert unaccent(f"x{char}y") == f"x{plain}y"
    if char == "ß":  # the plan's check of this pair through `fold` (Task 2: `test_unaccent_map` holds it)
        assert fold("Straße") == "strasse"


def test_transliteration_table_is_consistent() -> None:
    chars = [c for group, _ in text._TRANSLITERATIONS for c in group]
    assert len(chars) == len(set(chars)), "a character is listed twice"
    # NFKD runs first: an entry for a character that NFKD changes could never be reached.
    assert [c for c in chars if unicodedata.normalize("NFKD", c) != c] == []
    assert text._UNCHANGED.isdisjoint(chars)
    assert {c: r for group, r in text._TRANSLITERATIONS for c in group} == text._MAP


@pytest.mark.parametrize("char", list("ǢǣǮǯǼǽǾǿ"))
def test_unaccent_leaves_the_letters_postgresql_leaves(char: str) -> None:
    # PostgreSQL removes a mark only from a plain Latin or Greek letter; these carry one on AE, O with stroke
    # or EZH.
    assert unaccent(char) == char


def test_unaccent_keeps_case_and_unrelated_characters() -> None:
    assert unaccent("Müller Straße Café") == "Muller Strasse Cafe"
    assert unaccent("plain ASCII 123 (m/w/d) & co.") == "plain ASCII 123 (m/w/d) & co."
    assert unaccent("") == ""
    # Characters of other scripts that are not accented Latin letters stay as they are.
    assert unaccent("日本語 한국어") == "日本語 한국어"


@settings(deadline=None)
@given(st.text())
def test_unaccent_leaves_no_combining_mark(s: str) -> None:
    assert not any(unicodedata.combining(c) for c in unaccent(s))


@settings(deadline=None)
@given(st.text(alphabet=st.characters(max_codepoint=0x7F)))
def test_unaccent_is_the_identity_on_ascii(s: str) -> None:
    assert unaccent(s) == s


def test_fold() -> None:
    assert fold("STRASSE") == "strasse"
    assert fold("Müller") == "muller"
    assert fold("Łódź Đakovo Þór") == "lodz dakovo thor"
    assert fold("İstanbul") == "istanbul"
    # norm_text runs first.
    assert fold("  Soft\N{SOFT HYPHEN}ware\N{NO-BREAK SPACE}Entwickler  ") == "software entwickler"
    # casefold runs after unaccent: the capital sharp s and the final sigma end in their plain forms.
    assert fold("GROẞ") == "gross"
    assert fold("ΟΔΟΣ") == fold("οδος") == fold("οδοσ")
