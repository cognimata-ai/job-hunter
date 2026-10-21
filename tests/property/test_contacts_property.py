"""Contact stripping properties (spec §12.3, M2 Task 10).

Stripping never leaves a generated e-mail address or phone number in the text, stripping twice changes
nothing (Task 14 and the LLM payload rely on it), the replacements are ordered, disjoint offsets into the
original text that rebuild `stripped_text`, and an edit set gives the same text whatever order its edits were
made in.

Every datum is generated; no real person's data is used.
"""

from typing import get_args
from unittest.mock import patch

from hypothesis import assume, given, settings
from hypothesis import strategies as st
from jobhunter.profile import contacts
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


SEPARATORS = [
    *[" ", "/", "-", ".", " - ", " / ", ") ", " (", ")-", "\xa0", "\N{NARROW NO-BREAK SPACE}"],
    *["\N{EN DASH}", " \N{EN DASH} ", "\N{EM DASH}"],
]
BREAKS = ["\n", " \n", "\n ", "\r\n", "\r\n "]


def digits_in(text: str) -> int:
    return sum(c in DIGITS for c in text)


def phones(*, allow_break: bool = True) -> st.SearchStrategy[str]:
    """A number of the table's forms: `+<cc>`, `00<cc>` or a leading 0 (also `(0)30` and `0 30`, with an area
    code of two or three digits), then at least 7 digits in two to six groups with the separators of the table
    (NBSP and Unicode dashes too) and, where the first line is still short, one line break."""

    @st.composite
    def numbers(draw: st.DrawFn) -> str:
        kind = draw(st.sampled_from(["plus", "double0", "national", "paren0", "zero_sep"]))
        cc = draw(st.sampled_from(["49", "44", "1", "43", "41", "33"]))
        area = draw(st.sampled_from("123456789")) + draw(st.text(alphabet=DIGITS, min_size=1, max_size=4))
        if kind == "plus":
            head, enough = "+" + cc, 8
        elif kind == "double0":
            head, enough = "00" + cc, 10
        elif kind == "national":
            head, enough = "0" + area, 8
        elif kind == "paren0":
            head, enough = "(0)" + area, 8
        else:
            head, enough = (
                "0" + draw(st.sampled_from([" ", " / ", "/", "\xa0"])) + area[: draw(st.integers(2, 3))],
                8,
            )
        groups = draw(
            st.lists(st.text(alphabet=DIGITS, min_size=2, max_size=5), min_size=2, max_size=6).filter(
                lambda g: digits_in(head) + sum(map(len, g)) >= enough
            )
        )
        joints = [draw(st.sampled_from(SEPARATORS)) for _ in groups]
        if allow_break and draw(st.booleans()):
            # A break may stand before group k when the line before it holds too few digits and the line after
            # it does not start with a year (a short number never takes in a year range).
            before_digits = digits_in(head)
            allowed = []
            for k, group in enumerate(groups):
                year_like = len(group) == 4 and group[:2] in {"19", "20"}
                if before_digits < enough and not year_like:
                    allowed.append(k)
                before_digits += len(group)
            if allowed:
                joints[draw(st.sampled_from(allowed))] = draw(st.sampled_from(BREAKS))
        return head + "".join(j + g for j, g in zip(joints, groups, strict=True))

    return numbers()


BEFORE = st.sampled_from(["", " ", "Tel: ", "Mail ", "(", "<", "\n", "Kontakt:\n"])
AFTER = st.sampled_from(["", ".", ", ", " ", "\n", ")", ">", ";"])
FILLER = st.text(alphabet="xyz ", max_size=30)  # no word of it can be a label, a name or a keyword
settings_ = settings(max_examples=400, deadline=None)


@settings_
@given(emails(), BEFORE, AFTER, FILLER)
def test_a_generated_email_address_is_replaced_and_nothing_else_changes(
    value: str, before: str, after: str, filler: str
) -> None:
    result = strip_contacts(f"{filler}\n{before}{value}{after}\n{filler}")
    assert result.stripped_text == f"{filler}\n{before}[EMAIL]{after}\n{filler}"
    assert dict(result.report) == {**dict.fromkeys(CATEGORIES, 0), "email": 1}
    assert scan_value(f"{before}{value}{after}") == ["email"]


@settings_
@given(phones(), BEFORE, AFTER, FILLER)
def test_a_generated_phone_number_is_replaced_and_nothing_else_changes(
    value: str, before: str, after: str, filler: str
) -> None:
    assume(not (before == "(" and ")" in value))  # `(0100) 00 00`: the paren is the number's own
    result = strip_contacts(f"{filler}\n{before}{value}{after}\n{filler}")
    assert result.stripped_text == f"{filler}\n{before}[PHONE]{after}\n{filler}"
    assert dict(result.report) == {**dict.fromkeys(CATEGORIES, 0), "phone": 1}


@settings_
@given(
    emails(),
    phones(allow_break=False),
    st.sampled_from(
        ["Religion und Ethik", "Geburtsort Musterstadt", "Gender: divers", "Familienstand ledig"]
    ),
)
def test_restoring_a_personal_replacement_keeps_the_contacts_inside_it_replaced(
    email: str, phone: str, head: str
) -> None:
    text = f"{head}, Kontakt: {email}, Tel. {phone}\nSkills\n"
    base = strip_contacts(text)
    assert base.stripped_text == "[PERSONAL]\nSkills\n"
    assert restore(text, base, 0).stripped_text == f"{head}, Kontakt: [EMAIL], Tel. [PHONE]\nSkills\n"
    for label, value, token in (("Kinder", email, "[EMAIL]"), ("Familienstand", phone, "[PHONE]")):
        cell = f"{label}\n{value}\nSkills\n"
        assert strip_contacts(cell).stripped_text == "[PERSONAL]\nSkills\n"
        assert restore(cell, strip_contacts(cell), 0).stripped_text == f"{label}\n{token}\nSkills\n"


# Fragments that meet every rule and every boundary of the rules: glued together at random, they put a
# replacement next to the characters of another one.
FRAGMENTS = [
    *"Max Mustermann max mustermann Erika Musterfrau Dr. Senior Backend Engineer Lebenslauf Berlin".split(),
    *"Straße straße Weg Platz Hauptstr. Musterstraße Allee 12 10115 01067 Tel. +49 0049 030 0171".split(),
    *"1234567 23125456 01.02.1990 2019 - / . , ( ) : | @ example.org www. www.example.org https://".split(),
    *"http://example.org/a linkedin.com/in/ github.com/ xing.com/profile/ Geburtsdatum".split(),
    *"geb. geboren DOB".split(),
    *["Hansaring 5", "Clustering 3", "Software Engineering 2", "Mittlerer Ring 2", "Cloud-Sharing 4"],
    *["(01.02.1990)", "(ledig)", "(TT.MM.JJJJ)", "(deutsch)", "Geburtsdatum-Ort:", "Geburtsort-Datum:"],
    *["Leiter Controlling\n", "Leiterin Finanzen\n", "Kaufmann für Büromanagement\n", "Max Kaufmann"],
    *["Am Spring 5", "Spring 5", "0-306-40615-2", "0 30 / 231 254 56"],
    *"Familienstand ledig Kinder Children Gender Nationalität [EMAIL] [PHONE] [NAME] [PERSONAL]".split(),
    *"max.mustermann@example.org erika@mail.example.org ß ä Ö x Q9 0 00 +".split(),
    *[" ", " ", " ", "\n", "\n", "\n\n", "\t", "\r\n", "\u00a0"],
    *["Geburtsdatum\n01.02.1990\n", "Familienstand\nledig\n", "Kinder\n2\n", "Religion\n\n\nevangelisch\n"],
    *["x" * 150, "y" * 190, "z" * 250, "word " * 40, "abc " * 60, "geb." * 60, "Religion: " * 25],
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


@settings(max_examples=2000, deadline=None)
@given(st.one_of(documents, anything, tables))
def test_three_rounds_are_enough_for_one_pass_to_reach_the_fixed_point(text: str) -> None:
    """`_MAX_ROUNDS` is 6; the chains of claims that expose one another are at most three links long."""
    with patch.object(contacts, "_MAX_ROUNDS", 3):
        once = strip_contacts(text).stripped_text
        assert strip_contacts(once).stripped_text == once


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
    # What a restored personal span lets through is a replacement of its own, restored by its span, unless
    # it has the extent of the personal span itself (`dob@example.org`): that span names the personal one.
    inner = {(r.start, r.end) for r in result.replacements}
    final = apply_edits(text, Edits(restored=result.edits.restored | inner))
    personal = {(r.start, r.end) for r in strip_contacts(text).replacements if r.category == "personal"}
    assert all((r.start, r.end) in personal for r in final.replacements)
    if not final.replacements:
        assert final.stripped_text == text


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
