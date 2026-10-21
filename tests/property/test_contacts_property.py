"""Contact stripping properties (spec §12.3, M2 Task 10).

Stripping never leaves a generated e-mail address or phone number in the text, stripping twice changes
nothing (Task 14 and the LLM payload rely on it), the replacements are ordered, disjoint offsets into the
original text that rebuild `stripped_text`, and an edit set gives the same text whatever order its edits were
made in.

Every datum is generated; no real person's data is used.
"""

from typing import get_args

from hypothesis import given, settings
from hypothesis import strategies as st
from jobhunter.profile.contacts import (
    REPLACEMENT,
    Category,
    Edits,
    StripResult,
    apply_edits,
    remove_span,
    restore,
    scan_value,
    strip_contacts,
)

CATEGORIES: tuple[Category, ...] = get_args(Category)
LOWER = "abcdefghijklmnopqrstuvwxyz"
DIGITS = "0123456789"


@st.composite
def emails(draw: st.DrawFn) -> str:
    local = draw(st.sampled_from(LOWER)) + draw(st.text(alphabet=LOWER + DIGITS + "._-+", max_size=15))
    labels = draw(
        st.lists(st.text(alphabet=LOWER + DIGITS + "-", min_size=1, max_size=10), min_size=1, max_size=3)
    )
    tld = draw(st.sampled_from(["org", "de", "com", "io", "example"]))
    return f"{local}@{'.'.join(labels)}.{tld}"


@st.composite
def phones(draw: st.DrawFn) -> str:
    """A number of the table's forms: `+<cc>`, `00<cc>` or a leading 0, then at least 7 digits in groups."""
    kind = draw(st.sampled_from(["plus", "double0", "national"]))
    if kind == "national":
        head = (
            "0" + draw(st.sampled_from("123456789")) + draw(st.text(alphabet=DIGITS, min_size=1, max_size=4))
        )
    else:
        head = ("+" if kind == "plus" else "00") + draw(st.sampled_from(["49", "44", "1", "43", "41", "33"]))
    groups = draw(
        st.lists(st.text(alphabet=DIGITS, min_size=2, max_size=5), min_size=2, max_size=4).filter(
            lambda g: sum(map(len, g)) >= 7
        )
    )
    separators = st.sampled_from([" ", "/", "-", ".", " - ", " / ", ") ", " ("])
    number = head
    for group in groups:
        number += draw(separators) + group
    return number


BEFORE = st.sampled_from(["", " ", "Tel: ", "Mail ", "(", "<", "\n", "Kontakt:\n"])
AFTER = st.sampled_from(["", ".", ", ", " ", "\n", ")", ">", ";"])
settings_ = settings(max_examples=400, deadline=None)


@settings_
@given(emails(), BEFORE, AFTER, st.text(alphabet=LOWER + " ", max_size=30))
def test_a_generated_email_address_never_survives(value: str, before: str, after: str, filler: str) -> None:
    text = f"{filler}\n{before}{value}{after}{filler}"
    assert value not in strip_contacts(text).stripped_text
    assert scan_value(f"{before}{value}{after}") != []


@settings_
@given(phones(), BEFORE, AFTER, st.text(alphabet=LOWER + " ", max_size=30))
def test_a_generated_phone_number_never_survives(value: str, before: str, after: str, filler: str) -> None:
    text = f"{filler}\n{before}{value}{after}{filler}"
    assert value not in strip_contacts(text).stripped_text
    assert scan_value(f"{before}{value}{after}") != []


# Fragments that meet every rule and every boundary of the rules: glued together at random, they put a
# replacement next to the characters of another one.
FRAGMENTS = [
    *"Max Mustermann max mustermann Erika Musterfrau Dr. Senior Backend Engineer Lebenslauf Berlin".split(),
    *"Straße straße Weg Platz Hauptstr. Musterstraße Allee 12 10115 01067 Tel. +49 0049 030 0171".split(),
    *"1234567 23125456 01.02.1990 2019 - / . , ( ) : | @ example.org www. www.example.org https://".split(),
    *"http://example.org/a linkedin.com/in/ github.com/ xing.com/profile/ Geburtsdatum".split(),
    *"geb. geboren DOB".split(),
    *"Familienstand ledig Kinder Children Gender Nationalität [EMAIL] [PHONE] [NAME] [PERSONAL]".split(),
    *"max.mustermann@example.org erika@mail.example.org ß ä Ö x Q9 0 00 +".split(),
    *[" ", " ", " ", "\n", "\n", "\n\n", "\t", "\r\n", "\u00a0"],
    *["Geburtsdatum\n01.02.1990\n", "Familienstand\nledig\n", "Kinder\n2\n", "Religion\n\n\nevangelisch\n"],
]

documents = st.lists(st.sampled_from(FRAGMENTS), max_size=45).map("".join)
anything = st.text(alphabet=st.characters(blacklist_categories=["Cs"]), max_size=300)

LABELS = [
    *"Geburtsdatum Geburtsort geboren geb. DOB Familienstand Staatsangehörigkeit Nationalität".split(),
    *["Nationality"],
    *"Konfession Religion Geschlecht Gender Kinder Children".split(),
    *["Date of birth", "Place of birth", "Marital status"],
]
VALUES = ["01.02.1990", "ledig", "evangelisch", "Musterstadt", "2", "deutsch", "weiblich", "max@example.org"]


@st.composite
def table_rows(draw: st.DrawFn) -> list[tuple[str, str, str]]:
    """Rows of a personal data table as `docparse` writes them: label and value on lines of their own, the
    label with or without a colon, up to two blank lines between the cells and after a row."""
    rows = draw(st.lists(st.tuples(st.sampled_from(LABELS), st.sampled_from(VALUES)), min_size=1, max_size=6))
    out = []
    for label, value in rows:
        gap = draw(st.sampled_from(["\n", "\n\n", "\n \n", "\n\n\n", "\r\n"]))
        out.append((label + draw(st.sampled_from(["", ":", " :"])), gap, value))
    return out


tables = table_rows().map(lambda rows: "\n".join(f"{label}{gap}{value}" for label, gap, value in rows))


@settings(max_examples=2000, deadline=None)
@given(st.one_of(documents, anything, tables))
def test_stripping_twice_changes_nothing(text: str) -> None:
    once = strip_contacts(text)
    twice = strip_contacts(once.stripped_text)
    assert twice.stripped_text == once.stripped_text
    assert twice.replacements == ()


@settings(max_examples=400, deadline=None)
@given(table_rows())
def test_a_table_with_every_cell_on_its_own_line_is_stripped_row_by_row(
    rows: list[tuple[str, str, str]],
) -> None:
    text = "\n".join(f"{label}{gap}{value}" for label, gap, value in rows)
    r = strip_contacts(text)
    assert r.stripped_text == "\n".join(["[PERSONAL]"] * len(rows))
    assert dict(r.report) == {**dict.fromkeys(CATEGORIES, 0), "personal": len(rows)}


def render(original: str, result: StripResult) -> str:
    out: list[str] = []
    position = 0
    for r in result.replacements:
        out += [original[position : r.start], REPLACEMENT[r.category]]
        position = r.end
    return "".join(out) + original[position:]


@settings(max_examples=1000, deadline=None)
@given(st.one_of(documents, anything, tables))
def test_the_replacements_are_ordered_disjoint_offsets_into_the_original(text: str) -> None:
    result = strip_contacts(text)
    previous_end = 0
    for r in result.replacements:
        assert previous_end <= r.start < r.end <= len(text)
        previous_end = r.end
    assert render(text, result) == result.stripped_text
    assert dict(result.report) == {c: sum(r.category == c for r in result.replacements) for c in CATEGORIES}
    # The scan of a value is the same rules without the name rule, in the order of the categories.
    assert scan_value(text) == [c for c in CATEGORIES if c != "name" and result.report[c]]


@settings(max_examples=500, deadline=None)
@given(st.one_of(documents, anything, tables))
def test_restoring_every_replacement_gives_the_original(text: str) -> None:
    result = strip_contacts(text)
    for i in range(len(result.replacements)):
        result = restore(text, result, i)
    assert result.stripped_text == text


@settings(max_examples=500, deadline=None)
@given(st.data())
def test_an_edit_set_gives_the_same_text_in_any_order(data: st.DataObject) -> None:
    text = data.draw(documents)
    base = strip_contacts(text)
    restores = (
        data.draw(st.sets(st.sampled_from(range(len(base.replacements))), max_size=4))
        if base.replacements
        else set()
    )
    spans = data.draw(
        st.sets(
            st.tuples(st.integers(0, len(text)), st.integers(0, len(text))).filter(lambda s: s[0] < s[1]),
            max_size=4,
        )
        if text
        else st.just(set())
    )
    operations: list[tuple[str, int, int]] = [("restore", i, 0) for i in restores] + [
        ("remove", s, e) for s, e in spans
    ]
    order = data.draw(st.permutations(operations))
    result = base
    for kind, a, b in order:
        result = restore(text, result, a) if kind == "restore" else remove_span(text, result, a, b)
    expected = apply_edits(
        text,
        Edits(
            restored=frozenset((base.replacements[i].start, base.replacements[i].end) for i in restores),
            removed=frozenset(spans),
        ),
    )
    assert result == expected
    assert apply_edits(text, result.edits) == result
