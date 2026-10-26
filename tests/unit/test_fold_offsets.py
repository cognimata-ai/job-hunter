"""`fold_with_offsets` (M3 Task 3): `fold(s)` plus, for each folded character, the index in `s` of the
original character it came from (the offset map of the V3 grounding, spec §11.6 L4362). A character that
expands (`ß` to `ss`, `„` to `,,`) gives every one of its folded characters its own index; a run of
whitespace collapses to one space at the index of the run's first character; what `fold` strips or removes
has no folded character. `fold(s) == fold_with_offsets(s)[0]` for every input."""

# ruff: noqa: RUF001  (typographic quotes and dashes are the inputs under test)

from hypothesis import given, settings
from hypothesis import strategies as st
from jobhunter.normalize.text import fold, fold_with_offsets


def pairs(s: str) -> list[tuple[str, int]]:
    folded, offsets = fold_with_offsets(s)
    assert len(offsets) == len(folded)
    return list(zip(folded, offsets, strict=True))


def test_an_umlaut_loses_its_mark_and_keeps_its_index() -> None:
    assert pairs("Bär") == [("b", 0), ("a", 1), ("r", 2)]
    # decomposed, the mark joins its letter: the folded `a` comes from the letter, `r` keeps its own index
    assert pairs("Ba\N{COMBINING DIAERESIS}r") == [("b", 0), ("a", 1), ("r", 3)]


def test_sharp_s_expands_and_both_letters_point_at_it() -> None:
    assert pairs("Straße") == [("s", 0), ("t", 1), ("r", 2), ("a", 3), ("s", 4), ("s", 4), ("e", 5)]
    assert pairs("GRO\N{LATIN CAPITAL LETTER SHARP S}") == [("g", 0), ("r", 1), ("o", 2), ("s", 3), ("s", 3)]


def test_typographic_quotes_and_dashes_fold_at_their_index() -> None:
    assert pairs("„Hallo“ – ok") == [
        (",", 0),
        (",", 0),
        ("h", 1),
        ("a", 2),
        ("l", 3),
        ("l", 4),
        ("o", 5),
        ('"', 6),
        (" ", 7),
        ("-", 8),
        (" ", 9),
        ("o", 10),
        ("k", 11),
    ]


def test_whitespace_collapses_to_the_first_of_its_run_and_is_trimmed() -> None:
    s = "  Python \n\t Entwickler  "
    folded, offsets = fold_with_offsets(s)
    assert folded == "python entwickler" == fold(s)
    assert offsets[:7] == [2, 3, 4, 5, 6, 7, 8]  # the space stands for " \n\t " from index 8
    assert offsets[7:] == list(range(12, 22))
    assert s[offsets[0]] == "P" and s[offsets[7]] == "E"


def test_removed_characters_have_no_folded_character() -> None:
    s = "Ent\xadwick\N{ZERO WIDTH SPACE}ler\xa0GmbH"  # soft hyphen, zero-width space, no-break space
    folded, offsets = fold_with_offsets(s)
    assert folded == "entwickler gmbh" == fold(s)
    assert offsets == [0, 1, 2, 4, 5, 6, 7, 9, 10, 11, 12, 13, 14, 15, 16]
    assert "".join(s[offset] for offset in offsets) == "Entwickler\xa0GmbH"


def test_a_compatibility_ligature_expands_at_its_index() -> None:
    assert pairs("Ef\N{LATIN SMALL LIGATURE FI}zienz") == [  # NFKC gives "fi"
        ("e", 0),
        ("f", 1),
        ("f", 2),
        ("i", 2),
        ("z", 3),
        ("i", 4),
        ("e", 5),
        ("n", 6),
        ("z", 7),
    ]


def test_hangul_jamo_compose_into_one_syllable_at_the_first_jamo() -> None:
    # L + V + T compose into one syllable (NFKC), across three characters that each start a segment
    s = "x\N{HANGUL CHOSEONG KIYEOK}\N{HANGUL JUNGSEONG A}\N{HANGUL JONGSEONG KIYEOK}y"
    folded, offsets = fold_with_offsets(s)
    assert folded == fold(s) == "x\N{HANGUL SYLLABLE GAG}y"
    assert offsets == [0, 1, 4]
    # between non-ASCII letters too: the jamo pieces join, the umlauts keep their own indices
    s = "ä\N{HANGUL CHOSEONG KIYEOK}\N{HANGUL JUNGSEONG A}ö"
    assert fold_with_offsets(s) == ("a\N{HANGUL SYLLABLE GA}o", [0, 1, 3])


def test_the_empty_and_the_blank_string() -> None:
    assert fold_with_offsets("") == ("", [])
    assert fold_with_offsets(" \t\n ") == ("", [])


# Characters whose normalisation reaches across neighbours: combining marks (also at the start), Hangul jamo
# and a syllable, Indic two-part vowels whose second part is a starter (Oriya, Tamil), Tibetan vowels that
# decompose into marks, spacing diacritics, a ligature, letters NFKC or the fold maps (Ohm, Angstrom, sharp s,
# dotted and dotless i), and the characters `norm_text` removes or maps to a space; then plain text.
TRICKY = (
    "".join(
        map(
            chr,
            (
                *(0x0301, 0x0308, 0x0327, 0x0323),
                *(0x1100, 0x1161, 0x11A8, 0xAC00),
                *(0x0B47, 0x0B3E, 0x0B57, 0x0BC6, 0x0BBE),
                *(0x0F71, 0x0F72, 0x0F73),
                *(0x00A8, 0x02DC, 0xFB01, 0x2126, 0x212B, 0x00DF, 0x1E9E, 0x0130, 0x0131),
                *(0x00AD, 0x200B, 0x200D, 0xFEFF, 0x00A0, 0x202F, 0x2028),
            ),
        )
    )
    + " \t\nAaeoßÄäö„“‚‘–—…"
)
texts = st.one_of(
    st.text(), st.text(alphabet=TRICKY), st.text(alphabet=st.sampled_from(TRICKY) | st.characters())
)


@settings(max_examples=500)
@given(texts)
def test_the_folded_text_is_fold_and_the_offsets_map_back(s: str) -> None:
    folded, offsets = fold_with_offsets(s)
    assert folded == fold(s)
    assert len(offsets) == len(folded)
    assert offsets == sorted(offsets)
    assert all(0 <= offset < len(s) for offset in offsets)
    # Round trip: the original text from the index of a run of folded characters that share it up to the
    # index of the next folded character folds to that run (a character that expands folds to all of it).
    start = 0
    while start < len(folded):
        end = start + 1
        while end < len(folded) and offsets[end] == offsets[start]:
            end += 1
        source_end = offsets[end] if end < len(folded) else len(s)
        assert fold(s[offsets[start] : source_end]) == folded[start:end].strip(), (s, start)
        start = end
