"""Contact stripping (spec §12.3; M2 Task 10): pure, no I/O, no logging, no value in any message.

Before any CV text leaves the machine, `strip_contacts` replaces contact details and personal data with
tokens (`[EMAIL]`, `[PHONE]`, `[URL]`, `[ADDRESS]`, `[PERSONAL]`, `[NAME]`) and the user reviews the
result. It is a best-effort aid; the user's review is the guarantee. Task 11 calls `strip_contacts`, Task
12 calls `scan_value` on the extracted profile, and Task 14 rebuilds the reviewed text with `apply_edits`.

**The rules** are the table of §12.3, rule by rule. Where a rule needed a decision:

- *E-mail*: a local part of word characters and `. % + -`, `@`, a dotted domain, a TLD of letters only.
- *Phone*: `+<cc>`, `00<cc>` or a leading `0` (`030`, `(0)30`, `0 30`: a lone 0 is followed by a blank or a
  slash, never a hyphen, and an area code of two digits or more), then digit groups with space, tab, `/`,
  `-`, `.` or `()` as separators (Unicode dashes too) and at least 7 digits after the prefix (the country
  code of `+` and `00` counts as digits). A number that is short on its first line goes on over **one** line
  break, unless the next line starts with a year range: a year that a word, a dash and another year, or
  `bis`, `heute`, `present` or `today` follow (`Projekt 0815` / `2019 - 2022 Senior Engineer`). A year-like
  group that more digits follow is part of the number (`+49 30` / `2012 3456`: Berlin has 030 20xx
  exchanges). A complete number never takes in the next line, but a second number on its line that is
  short does (`(0)30 23125456 030 231` / `25456`), unless that next line is a number of its own; the line a
  number goes on to may end in such a number in turn. A run of numbers longer than one match is one number.
  A number whose last group starts a postal code and its city on the same line ends before it when it is
  complete without it. A `+` number may be glued to a digit (`2022+49 30 ...`). A date is no phone number
  (`01.02.1990` has its zero inside its group) and neither is an ISBN (`0-306-40615-2`).
- *URL*: `http(s)://...`, `www....`, `linkedin.com/in/...`, `xing.com/profile/...` and `github.com/<user>`
  (with one or two path segments after it). Trailing punctuation stays outside. A URL that runs into a
  glued e-mail address or phone number ends before it (`github.com/max/geb.a@b.de`).
- *Address*: a street and a house number of one to three digits (`12`, `12a`, `12-14`; not `4.0`). The street
  is a word that ends in a suffix of the table (`strasse` too), a capitalised word before `Straße`, `Allee`,
  `Gasse`, `Ufer`, `Damm`, `Ring` or `Weg`, or a capitalised word that ends in `er` before `Platz`
  (`Potsdamer Platz`, not `Auf Platz 1`). Not a street: the words of `NOT_A_STREET` (the English `ring`
  words of `ENGLISH_RING_WORDS` such as `Clustering` and `Engineering`, as whole words; a `weg` that is a
  way of life or of thought, `Bildungsweg`, never a way one walks such as `Heimweg`), an ordinal before
  `Platz`; but after `Am`, `Im`, `Zum` or `Zur`, capitalised as in a street name, such a word is a street
  name (`Am Spring 5` is an address; `Spring 5` and `Erfahrung im Spring 5` are not). No rule about the
  letter before `ring`: `Hansaring` is a street. A word that is no street does not hide a street that
  starts in it (`Karriereweg 3-Heimweg 4`), and a postal code right before a street is part of it (`01067 Am
  Spring 5`). The postal code and city are the spec's regex: five digits, white space, a capitalised word
  (the white space and the city are bounded).
- *Personal*: a birth label anywhere; a protected-attribute label at the start of a line (after a bullet or
  a table pipe) or followed by a colon anywhere; a label is a word of its own (`Nationality-neutral` is none).
  The span is the label and the rest of its line, at most `_REST_OF_LINE_MAX` characters, and a cut inside a
  word goes on to the end of the word (at most as far again), so a token is never cut in half; it stops at
  a label there, which starts a replacement of its own with the rest of the line after it. Anywhere but
  at the start of a line a label is a hit when anything but punctuation follows it. At the start of a line
  the rest has no value when it holds only punctuation (`empty`), or only label words, what joins them and a
  format mask (`labels`: `Geburtsdatum/-ort`, `Familienstand / Kinder`, `Geburtsdatum (TT.MM.JJJJ)`,
  `Geschlecht (M / W / D)`); a format mask is upper-case letters of a date format in brackets or a list of
  two or more single letters joined by `/`, any other bracket (`(01.02.1990)`, `(ledig)`, `(USA)`, `(M)`)
  holds a value. `docparse` writes every table cell on a line of its own (`Geburtsdatum`,
  then `01.02.1990`), so a label without a value takes the next non-empty line too, in the same replacement
  (after at most two blank lines; never a line that is itself a label line or a token). If there is none,
  an empty label is no hit, and a label of label words is claimed as it is. A label may be joined to `ort` or
  `datum` by a hyphen (`Geburtsdatum-Ort`), to no other word.
  `Kinder`, `Children` and `Gender` need a colon, a pipe or a tab after them, or must stand alone on their
  line (`Kinder- und Jugendarbeit` stays). A span whose last line ends in a number that is short on it and
  goes on below (`Geschlecht (M/W/D)` / `Tel. +49 30` / `2012 3456`) takes the rest of the number and of
  that line, up to a postal code and its city or an e-mail address that starts in it.
- *Boundaries*: a label, a URL or a street right after a year (19xx, 20xx) starts there as after a blank
  (`2019 - 2022www.example.org`, glued table cells), except a label from which an e-mail address goes on.
- *Name*: the header name and its later occurrences; see `ROLE_KEYWORDS` and `_header_names`. `Name: Max
  Mustermann` (also `Vorname/Nachname:`) names its value; a line `Name`, `Persönliche Daten` or `Personal
  details` is a heading that is passed over. A title word that is also a surname (`Leiter`, `Kaufmann`) is a
  title as the first word of a line (`Leiter Controlling`, `Kaufmann für Büromanagement`) and after an
  inflected adjective (`Technischer Leiter`, `Geprüfte Kauffrau`); a line that holds one elsewhere is a
  title line when it is no name (`Stellv. Leiter`), and when it is one (`Max Kaufmann`, but also `IT
  Leiter`) it is taken as a name and the search goes on, so that a name below it is taken too. The
  feminine `-in` form of a title word is a title word when its stem has four letters or more (`Architektin`;
  `Evin` is no feminine `ev`). A title or heading line that holds a contact is passed over as well
  (`Senior Engineer | max@example.org`).

**The order the categories claim text in** is `APPLY_ORDER`: personal, url, email, phone, address, name.
Each category runs on the text with everything claimed so far masked out (NUL, which no pattern matches or
crosses), so a span that one category has claimed is never claimed, or counted, by another. The order
settles the overlaps: a birth line takes the date in it (which is also phone-shaped) and the rest of its
line; a URL takes an e-mail address in its query; an e-mail address takes the digits of its local part
before the phone rule sees them; a phone number takes its digits before the postal-code rule; the name rule
looks at what is left.

**Idempotence**: a token matches no rule, and a second `strip_contacts` finds nothing new. A claim can take
away what kept an earlier category's pattern from matching (a phone number that ends in a word was what
kept `www.` from being a URL), so the sequence repeats on what is left (`_detect`), at most `_MAX_ROUNDS`
times. Such a chain needs a claim that ends in a word character, which only a phone number or an e-mail
address does, and exposes a match of an earlier category, which only a URL or a label can be: it is two
links long, three at the most. A run of labels longer than the cut is no chain (a label may start where the
previous span ended), and neither is a run of glued phone numbers (a number may start where the previous
one ended, `_PHONE_ADJACENT`). The unit and property tests check one pass against a second one, also with the
rounds cut to three; the bound is therefore an argument and a test, not a proof.

**Offsets** (`Replacement`, `Edits`, `remove_span`) always refer to the **original** text, never to the
stripped string. All patterns are compiled once at import (the name pattern per header, from escaped
literals) and every repeat in every one of them is bounded (`{m,n}`, never `*`, `+` or `{n,}`; a test reads
the parsed patterns), whose neighbouring parts cannot match the same characters, so a match is linear in
the text (the M1 F1 lesson).

**Edits** (for Task 14). The user's review is an `Edits` value: the spans of the replacements put back
(`restored`) and the extra spans marked `[REMOVED]` (`removed`), both as `(start, end)` offsets into the
original. The stripped text is a pure function of the original and its `Edits`, so Task 14 stores
`StripResult.edits` and rebuilds with `apply_edits(original, edits)` on every request; the order the edits
were made in does not matter.

- `restore(original, result, index)`: `index` always names a replacement of the fresh
  `strip_contacts(original).replacements`, whatever was restored or removed before. It does **not** refer to
  the `replacements` of an edited `result`, which shrinks and grows with the edits (pre-flight F29: the
  `PUT /text` body carries indexes into the fresh strip).
- `remove_span(original, result, start, end)` adds an extra span.
- Both return the fresh `StripResult` of the extended edit set. A removed span that overlaps a replacement,
  or another removed span, merges with it into one `[REMOVED]` over the union, so a half-marked e-mail
  address never leaves its other half in the text. A restored span that names no replacement is ignored, so
  a stale edit set cannot put back the wrong thing.
- Restoring a `[PERSONAL]` span (a false positive such as `Religion und Ethik, Kontakt: ...`) must not
  reveal what is inside it: personal runs first and takes in the rest of its line, so the other categories
  are run again over the original without the personal rule, and what overlaps the restored span is replaced
  as usual (also a number that runs past the end of the span). A claim that runs into a replacement that
  stays is clipped to the parts that no staying replacement covers, and each part keeps the claim's
  category (`Kinder` / `030 2312` / `54563 Berlin` restores to `Kinder` / `[PHONE]` / `[ADDRESS]`).
  Those contacts are replacements of their own (they appear in `result.replacements`, but have no index in
  the fresh strip); the user restores one by its `(start, end)` in `Edits.restored`, except one that has
  the extent of the personal span itself (`dob@example.org`): that span names the personal replacement.

**The review map** (`StripResult.segments`, Task 14 ruling R14-2): the runs of the stripped text in order,
each with the span of the stripped text and the span of the original it stands for (`Segment`): text kept as
it is, a replacement token (its category, its index into the fresh strip when it has one, and whether it can
be restored: not a contact with the extent of a personal span, which the edit set cannot name), or
`[REMOVED]`. `_build` makes the map in the same pass that renders the stripped text, so the two never
disagree. The review UI highlights the replacements with it and turns a selection in the stripped text into
original offsets; it holds offsets and categories only, never a value.

`scan_value(value)` runs the same rules without the name rule on one profile value (Task 12).
"""

import re
from bisect import bisect_right
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Final, Literal, get_args

Category = Literal["email", "phone", "url", "address", "personal", "name"]
CATEGORIES: Final[tuple[Category, ...]] = get_args(Category)
REPLACEMENT: Final[Mapping[Category, str]] = MappingProxyType(
    {
        "email": "[EMAIL]",
        "phone": "[PHONE]",
        "url": "[URL]",
        "address": "[ADDRESS]",
        "personal": "[PERSONAL]",
        "name": "[NAME]",
    }
)
REMOVED: Final = "[REMOVED]"
# The order the categories claim text in (see the module docstring).
APPLY_ORDER: Final[tuple[Category, ...]] = ("personal", "url", "email", "phone", "address", "name")

# Words that make a line a title, a heading or an organisation, not a person's name (lower case). The
# header-name rule passes over a line that holds one (`_HEADER_SKIPS` such lines at most, before the name)
# and never takes one for a name. Besides the spec's CV words and the common DE/EN job titles it holds the
# headings that open a CV before the name (`Persönliche Daten`) and the legal forms of a company (`Beispiel
# GmbH`); the headings of the sections that follow the header (`Berufserfahrung`) are not in it: a text
# that starts with one has no header name. A German compound that ends in one of the words of six letters
# or more is a title too (`Projektleiter`, `Softwareentwickler`).
ROLE_KEYWORDS: Final[frozenset[str]] = frozenset(
    # What a CV calls itself, and the headings that open it.
    {"lebenslauf", "curriculum", "vitae", "resume", "cv", "profil", "profile", "bewerbung", "application"}
    | {"persönliche", "daten", "personal", "details", "kontakt", "contact"}
    # Seniority and level.
    | {"senior", "junior", "lead", "head", "principal", "staff", "chief", "trainee", "praktikant", "intern"}
    # English titles.
    | {"engineer", "engineering", "developer", "manager", "consultant", "architect", "analyst", "designer"}
    | {"administrator", "specialist", "expert", "director", "officer", "owner", "scientist", "technician"}
    | {"assistant", "coordinator", "supervisor", "executive", "associate", "programmer", "tester", "student"}
    | {"freelancer", "founder", "ceo", "cto", "cfo", "coo", "vp"}
    # German titles.
    | {"entwickler", "entwicklerin", "berater", "beraterin", "ingenieur", "ingenieurin", "architekt"}
    | {"analytiker", "leiter", "leiterin", "spezialist", "experte", "expertin", "referent", "referentin"}
    | {"koordinator", "techniker", "sachbearbeiter", "mitarbeiter", "programmierer", "werkstudent"}
    | {"geschäftsführer", "geschäftsführerin", "inhaber", "gründer", "freiberufler"}
    | {"kaufmann", "kauffrau"}
    # Fields a title is followed by.
    | {"software", "backend", "frontend", "fullstack", "devops", "data", "product", "project", "projekt"}
    | {"sales", "marketing", "vertrieb", "qa"}
    # Legal forms.
    | {"gmbh", "ag", "kg", "ug", "mbh", "ohg", "gbr", "se", "inc", "ltd", "llc", "corp", "plc", "e.v", "ev"}
)
# Role words that are also surnames (`Max Kaufmann`, `Erika Leiter`): they make a title inside a compound
# (`Industriekaufmann`, `Teamleiter`), as the first word of a line (`Leiter Controlling`) and after an
# inflected adjective (`_TITLE_ADJECTIVE`); anywhere else in a line they may be a surname (`_header_names`).
_COMPOUND_ONLY: Final = frozenset({"kaufmann", "kauffrau", "leiter", "leiterin"})
_COMPOUND_KEYWORDS: Final = tuple(sorted(k for k in ROLE_KEYWORDS if len(k) >= 6))
# An inflected adjective or participle before such a word makes the line a title (`Technischer Leiter`,
# `Kaufmännische Leiterin`, `Stellvertretender Leiter`, `Gelernter Kaufmann`, `Geprüfte Kauffrau`,
# `Diplomierte Kauffrau`, `Selbstständiger Kaufmann`): a stem that titles end in and an adjective ending. No
# common first name ends like that (`Peter`, `Walter`, `Kevin`, `Justin`).
_TITLE_ADJECTIVE: Final = re.compile(
    r"[^\W\d_]{0,30}(?:isch|lich|end|iert|prüft|lernt|ständig)(?:e|er|es|en)", re.IGNORECASE
)
# The feminine `-in` form of a title word needs a stem of this many letters (`Architektin`; `Evin` and `Agin`
# are no feminine `ev` and `ag`, which are legal forms).
_FEMININE_STEM_MIN: Final = 4
# English words that end in `ring` and are no street before a number (`Clustering 3`, `Software
# Engineering 2`, `Spring 5`). Spec §12.3 lists `ring` as a street suffix and stripping is the safe
# default (a false positive is restored in the review), so a word is excluded only when it is on this
# list: there is no rule about the letters before `ring` (`Hansaring`, `Hansering` and `Ostring` are
# German streets). The match is on the whole word, in any case; of a hyphenated word the last part
# counts (`Cloud-Engineering`).
ENGLISH_RING_WORDS: Final = frozenset(
    [
        *"spring string docstring substring clustering rendering catering engineering monitoring".split(),
        *"mentoring tutoring refactoring offering gathering ordering filtering sharing hiring wiring".split(),
        *"tiering scoring recovering delivering steering covering authoring manufacturing featuring".split(),
        *"referring transferring occurring recurring deferring preferring conferring measuring".split(),
        *"structuring restructuring configuring ensuring assuring insuring securing restoring".split(),
        *"exploring ignoring storing centering entering registering mastering partnering powering".split(),
        *"pioneering volunteering answering altering bordering discovering uncovering suffering".split(),
        *"considering remembering administering sheltering tendering scattering sponsoring caring".split(),
        *"firing acquiring requiring inspiring stirring".split(),
    ]
)
# Words that end in `weg` and are no street (`Zweiter Bildungsweg 2`): abstract compounds that name a way of
# learning, of a life or a career, or of thought (the `Werdegang` kind), never a way that one walks or drives.
# A word that names a physical way (`Heimweg`, `Umweg`, `Rückweg`, `Fußweg`) can name a street and is a
# street, as stripping is the safe default.
_GERMAN_PATH_WORDS: Final = frozenset("bildungsweg lösungsweg karriereweg berufsweg lebensweg ausweg".split())
NOT_A_STREET: Final = ENGLISH_RING_WORDS | _GERMAN_PATH_WORDS
# A word before `Platz` that is an ordinal, not a place (`Erster Platz 1`).
_ORDINALS: Final = frozenset(
    "erster zweiter dritter vierter fünfter sechster siebter achter neunter zehnter letzter bester".split()
)

_MAX_ROUNDS: Final = 6  # repetitions of the whole sequence of categories (see `_detect`)
_HEADER_SKIPS: Final = 3  # title or heading lines passed over before the header name
_REST_OF_LINE_MAX: Final = 200  # a label takes the rest of its line up to here
_HEADER_LINE_MAX: Final = 120
_MASK: Final = "\x00"

Span = tuple[int, int]


@dataclass(frozen=True)
class Replacement:
    """A span of the **original** text that the rules replaced with the token of `category`."""

    category: Category
    start: int
    end: int


@dataclass(frozen=True)
class Edits:
    """The user's edits, as offsets into the original text: replacements put back, extra removals."""

    restored: frozenset[Span] = frozenset()
    removed: frozenset[Span] = frozenset()


SegmentKind = Literal["text", "replacement", "removed"]


@dataclass(frozen=True)
class Segment:
    """One run of the stripped text (`stripped_start`, `stripped_end`) and the span of the original it stands
    for (`raw_start`, `raw_end`): `text` (the same characters in both), `replacement` (the token of
    `category`; `replacement_index` is its index into the fresh `strip_contacts(original).replacements`,
    `None` for a contact inside a restored personal span; `restorable` is whether an edit set can put it
    back), or `removed` (`[REMOVED]` over the union of a removal and what it merged with). Offsets only."""

    kind: SegmentKind
    stripped_start: int
    stripped_end: int
    raw_start: int
    raw_end: int
    category: Category | None = None
    replacement_index: int | None = None
    restorable: bool = False


@dataclass(frozen=True)
class StripResult:
    """The stripped text, the counts per category (never a value), the replacements still in the text, the
    edit set this result was built from (empty for `strip_contacts`) and the review map (`segments`)."""

    stripped_text: str
    report: Mapping[Category, int]
    replacements: tuple[Replacement, ...]
    edits: Edits = field(default_factory=Edits)
    segments: tuple[Segment, ...] = ()


# --- the patterns --------------------------------------------------------------------------------

_EOL: Final = "\r\n\x0b\x0c\x85\u2028\u2029"
# A year (19xx, 20xx, not inside a longer number) right before a label, a URL or a street ends a range of
# years, as a blank would: `2019 - 2022www.example.org`, `2019 - 2022Geburtsdatum: ...` (glued table cells).
_AFTER_YEAR: Final = r"(?<=(?<![0-9])(?:19|20)[0-9]{2})"
# A label inside a word, a URL or an e-mail address is no label, and a label read after a year is none when
# an e-mail address goes on from it (`2022geb.a@b.de` is one address).
_LABEL_START: Final = rf"(?:(?<![\w/@.:=&?%#~+\-])|{_AFTER_YEAR}(?![\w.%+\-]{{1,64}}@))"
# Punctuation and blanks: a rest of nothing else holds no value. `_JOINERS` are what also joins the words of
# a compound label (`Geburtsdatum/-ort`, `Familienstand / Kinder`).
_PUNCT: Final = " \t\xa0:|.,;\u2022\u00b7*>\u2013\u2014-"
_JOINERS: Final = _PUNCT + "/&"
# At the start of a line a label whose rest holds only label words, joiners and a format mask is a label of
# labels: it takes the cell below it, and if there is none it is claimed as it is.
_LABEL_WORDS: Final = frozenset(
    [
        *"geburtsdatum geburtsort familienstand marital status staatsangehörigkeit".split(),
        *"staatsangehoerigkeit nationalität nationalitaet nationality konfession religion".split(),
        *"geschlecht gender kinder children".split(),
        *"date of birth place dob geboren geb und and oder or ort datum".split(),
    ]
)
# A format mask in brackets, `(TT.MM.JJJJ)` or `(DD/MM/YYYY)`: upper-case letters and separators, every run of
# letters one repeated letter of a date format. Any other bracket, one with a digit or a lower-case word or
# letters that are no format (`(USA)`), holds a value.
_MASK_HINT: Final = re.compile(r"\([A-Z./\- ]{2,20}\)")
_FORMAT_LETTERS: Final = frozenset("TMJDY")
# A list of options in brackets, two to ten single letters joined by `/` (`(M / W / D)`, `(m/w/d)`,
# `(M/F)`), is a mask too: the value is in the cell below. One letter (`(M)`) is a value.
_OPTION_LIST: Final = re.compile(r"\([ \t]{0,3}[A-Za-z](?:[ \t]{0,3}/[ \t]{0,3}[A-Za-z]){1,9}[ \t]{0,3}\)")
_CAPITALS: Final = re.compile(r"[A-Z]{1,20}")
_LETTERS: Final = re.compile(r"[^\W\d_]{1,40}")
_DISTINCT_LABELS: Final = (
    r"Familienstand|Marital[ \t]{1,3}status|Staatsangeh(?:ö|oe)rigkeit|Nationalit(?:ä|ae)t|Nationality"
    r"|Konfession|Religion|Geschlecht"
)
_AMBIGUOUS_LABELS: Final = r"Kinder|Children|Gender"
_BIRTH_LABELS: Final = (
    r"Geburtsdatum|Geburtsort|Date[ \t]{1,3}of[ \t]{1,3}birth|Place[ \t]{1,3}of[ \t]{1,3}birth|DOB|geboren"
)
_LINE_START: Final = rf"(?<![^{_EOL}])[ \t\xa0\u2022\u00b7*|>\-\u2013\u2014]{{0,10}}"
# A label is a word of its own: not `Nationality-neutral`, not `Gender-based`; only `-ort` and `-datum` may
# join it (`Geburtsdatum-Ort`, `Geburtsort-Datum`).
_LABEL_END: Final = r"(?!\w)(?!-(?!(?i:ort|datum)(?!\w))[^\W\d_])"
# A label that a second pass reads right after a token (`_PERSONAL_ADJACENT`): a birth label, or a
# protected-attribute label with a colon after it. No named groups: it is a lookahead inside `_REST`.
_LABEL_AHEAD: Final = (
    rf"(?:(?i:{_BIRTH_LABELS}){_LABEL_END}|(?i:geb\.)"
    rf"|(?i:{_DISTINCT_LABELS}|{_AMBIGUOUS_LABELS}){_LABEL_END}(?=[ \t]{{0,3}}:))"
)
# A cut that falls inside a run of non-blank characters goes on to the end of the run (at most as far
# again), so a token (an e-mail address) is never cut in half; it stops at a label, which starts a
# replacement of its own and takes the rest of the line after it (`_PERSONAL_ADJACENT`), as a second pass
# would read it after a token.
_RUN_ON: Final = rf"(?:(?<=\S)(?:(?!{_LABEL_AHEAD})\S){{1,{_REST_OF_LINE_MAX}}})?"
# The rest of a line runs over claimed (masked) characters too: a second pass reads their tokens there.
_REST: Final = rf"[^{_EOL}]{{0,{_REST_OF_LINE_MAX}}}{_RUN_ON}"
_REST_OF_LINE: Final = re.compile(_REST)
# One pattern for all the labels, so that the first label of a line takes the rest of the line, other labels
# included. A label at the start of a line (after a bullet or a table pipe) is group `line`: a
# protected-attribute or birth label, or `Kinder`, `Children`, `Gender` with a colon, a pipe or a tab after
# them or alone on the line. Elsewhere: a birth label (`birth`), or a protected-attribute label with a colon
# after it (`inline`).
_PERSONAL: Final = re.compile(
    rf"(?:{_LINE_START}(?P<line>(?i:{_DISTINCT_LABELS}|{_BIRTH_LABELS}){_LABEL_END}|(?i:geb\.)"
    rf"|(?i:{_AMBIGUOUS_LABELS}){_LABEL_END}"
    rf"(?=[ \t]{{0,{_REST_OF_LINE_MAX}}}[:|]|\t|[ \t.,;]{{0,{_REST_OF_LINE_MAX}}}(?:[{_EOL}]|\Z)))"
    rf"|{_LABEL_START}(?:(?P<birth>(?i:{_BIRTH_LABELS}){_LABEL_END}|(?i:geb\.))"
    rf"|(?P<inline>(?i:{_DISTINCT_LABELS}|{_AMBIGUOUS_LABELS}){_LABEL_END}(?=[ \t]{{0,3}}:))))"
    rf"(?P<rest>{_REST})"
)
# A label that starts exactly where the previous personal span ended: a second pass reads a token there,
# so the lookbehind of `_PERSONAL` does not apply (a run of labels longer than the cut is one pass, not
# one per cut).
_PERSONAL_ADJACENT: Final = re.compile(
    rf"(?:(?P<birth>(?i:{_BIRTH_LABELS}){_LABEL_END}|(?i:geb\.))"
    rf"|(?P<inline>(?i:{_DISTINCT_LABELS}|{_AMBIGUOUS_LABELS}){_LABEL_END}(?=[ \t]{{0,3}}:)))"
    rf"(?P<rest>{_REST})"
)
# The line after a label that has no value on its own: up to two blank lines, then the next non-empty line.
_BLANKS: Final = rf"[ \t\xa0]{{0,{_REST_OF_LINE_MAX}}}"
_NEXT_LINE: Final = re.compile(
    rf"(?:(?:\r\n|[{_EOL}]){_BLANKS}){{0,2}}(?:\r\n|[{_EOL}])"
    rf"(?P<line>{_BLANKS}(?P<value>[^ \t\xa0{_EOL}][^{_EOL}]{{0,{_REST_OF_LINE_MAX - 1}}}{_RUN_ON}))"
)

_URL_CHAR: Final = r"[^\s<>\"'\[\]\x00]"
_URL: Final = re.compile(
    rf"(?:(?<![\w@./\-])|{_AFTER_YEAR})(?:"
    rf"https?://{_URL_CHAR}{{1,2048}}"
    rf"|www\.[\w\-]{{1,63}}(?:\.[\w\-]{{1,63}}){{0,6}}(?:[/?#]{_URL_CHAR}{{0,1024}})?"
    r"|(?:[a-z]{2,3}\.)?linkedin\.com/in/[\w%.\-]{1,100}/?"
    r"|(?:www\.)?xing\.com/profile/[\w%.\-]{1,100}/?"
    r"|(?:www\.)?github\.com/[\w\-]{1,39}(?:/[\w.\-]{1,100}){0,2}"
    r")(?<![.,;:!?)\]}>])",
    re.IGNORECASE,
)

# The local part and the labels are length-bounded (RFC 5321), which keeps the search linear.
_EMAIL: Final = re.compile(r"[\w.%+\-]{1,64}@[\w\-]{1,63}(?:\.[\w\-]{1,63}){0,8}\.[^\W\d_]{2,24}")

_PHONE_SEP: Final = r"[ \t\xa0\u202f./()\-\u2010-\u2015]"
_PHONE_SEP_CHARS: Final = " \t\xa0\u202f./()-" + "".join(map(chr, range(0x2010, 0x2016)))
_PHONE_GROUPS: Final = rf"(?:{_PHONE_SEP}{{1,3}}[0-9]{{1,14}}){{0,8}}(?![0-9])"
# The prefix: `+<cc>`, `00<cc>`, or a leading 0 as `030`, `(0)30` or `0 30` (a 0 of its own, a blank or a
# slash, then an area code of two digits or more; a date such as `01.02.1990` has the 0 inside its group, and
# the ISBN `0-306-40615-2` has a hyphen, so neither matches).
_PHONE_NUMBER: Final = (
    rf"(?:\((?=0[1-9][0-9]{{1,5}}\)))?(?:\+[0-9]{{1,14}}|00[0-9]{{1,14}}|0[1-9][0-9]{{1,12}}"
    rf"|\(0\)[ \xa0]?[1-9][0-9]{{1,12}}|0[ \t\xa0/]{{1,3}}[1-9][0-9]{{1,5}}){_PHONE_GROUPS}"
)
# Not inside a word or after a `+`; a `+` number may follow a digit (`2019 - 2022+49 30 ...`, glued cells), a
# number with a leading 0 may not (it would cut a number out of any long digit string).
_PHONE: Final = re.compile(rf"(?:(?<![\w+])|(?<=[0-9])(?=\+)){_PHONE_NUMBER}")
# A number that starts exactly where the previous one ended (`+49 30 2312 5456+49 30 ...`): a second pass
# reads a token there, so the lookbehind of `_PHONE` does not apply (a run of glued numbers is one round,
# not one round per number).
_PHONE_ADJACENT: Final = re.compile(_PHONE_NUMBER)
# A number at the end of a line that may be glued to the word before it (`max@example.org+49 30`), never to a
# digit (`2012 3456` holds no number `012 3456`).
_PHONE_IN_LINE: Final = re.compile(rf"(?:(?<![0-9+])|(?<=[0-9])(?=\+)){_PHONE_NUMBER}")
# What a number that is short on its first line may go on with: one line break, then more digit groups. A
# line that starts with a year range is no continuation: a year (19xx, 20xx) that a word, a dash and another
# year, or an open end follow (`Projekt 0815` / `2019 - 2022 ...`, `1999 bis 2004`, `2022 - heute`). A year
# that more digits follow is a group of the number (`+49 30` / `2012 3456`).
_YEAR: Final = r"(?:19|20)[0-9]{2}(?![0-9])"
_YEAR_RANGE: Final = (
    rf"{_YEAR}[ \t\xa0]{{0,3}}(?:[^\W\d_]|[\-\u2010-\u2015][ \t\xa0]{{0,3}}"
    rf"(?:{_YEAR}|(?i:bis|heute|present|today)(?!\w)))"
)
_PHONE_TAIL: Final = re.compile(
    rf"{_PHONE_SEP}{{0,3}}\r?\n{_PHONE_SEP}{{0,3}}(?!{_YEAR_RANGE})[0-9]{{1,14}}{_PHONE_GROUPS}"
)
# The groups after a match that used up `_PHONE_GROUPS` (`_phone_spans` repeats it, each time on new text).
_PHONE_MORE: Final = re.compile(rf"(?:{_PHONE_SEP}{{1,3}}[0-9]{{1,14}}){{1,8}}(?![0-9])")
# A group of a matched number that starts a number of its own: a 0 and an area code after a separator.
_NEW_NUMBER: Final = re.compile(r"(?<=[ \t\xa0\u202f./()\-\u2010-\u2015])0[1-9]")
# The digits a phone number holds at least, the digits of its prefix included (`0`: 7 after it; `+`, `00`: a
# country code and 7).
_PHONE_MIN_DIGITS: Final = {"+": 8, "0": 8, "00": 10}

# A house number is not the start of a decimal or a version (`Arbeitsplatz 4.0`).
_HOUSE: Final = r"[0-9]{1,3}[A-Za-z]?(?:[ \t]?[/\-][ \t]?[0-9]{1,3}[A-Za-z]?)?(?!\w)(?![.,][0-9])"
# A street: a word that ends in a suffix (`attached`), a capitalised word before `Straße`, `Allee`, ...
# (`lead`), or a capitalised word of six letters or more that ends in `er` before `Platz` (`Potsdamer
# Platz`, not `Auf Platz`).
_STREET: Final = re.compile(
    rf"(?:(?<![\w\-])|{_AFTER_YEAR})(?:"
    r"(?P<attached>[\w\-]{1,40}(?i:stra(?:ß|ss)e|str\.|weg|allee|platz|gasse|ring|damm|ufer))"
    r"|(?P<lead>[A-ZÄÖÜ][\w\-]{1,30})[ \t]{1,3}(?:Stra(?:ß|ss)e|Str\.|Allee|Gasse|Ufer|Damm|Ring|Weg)"
    r"|(?P<lead_er>[A-ZÄÖÜ][\w\-]{3,28}er)[ \t]{1,3}Platz"
    rf")[ \t]{{0,2}}{_HOUSE}"
)
# Spec §12.3: `\b\d{5}\s+[A-ZÄÖÜ][\wäöüß-]+`, with the white space and the city bounded.
_POSTAL: Final = re.compile(r"(?<!\w)[0-9]{5}\s{1,3}(?P<city>[A-ZÄÖÜ][\w\-]{1,40})(?![\w\-])")
_POSTAL_BEFORE: Final = re.compile(r"(?<!\w)[0-9]{5}\s{1,3}\Z")
_POSTAL_INLINE: Final = re.compile(r"(?<!\w)[0-9]{5}[ \t\xa0]{1,3}[A-ZÄÖÜ][\w\-]{1,40}(?![\w\-])")

# A line longer than the header-name rule looks at is cut into pieces of this size; the first piece decides.
_LINE: Final = re.compile(rf"[^{_EOL}]{{1,1000}}")
_PLACEHOLDER: Final = re.compile(r"\[(?:EMAIL|PHONE|URL|ADDRESS|PERSONAL|NAME|REMOVED)\]")
_NAME_WORD: Final = re.compile(r"(?:[^\W\d_]|['\u2019\-\u0300-\u036f]){1,40}")
_TITLE_SPLIT: Final = re.compile(r"[.\-]")
_WORD_SPLIT: Final = re.compile(r"[-/]")
# The words of an academic title (`Dr.-Ing.`, `Dipl.-Ing.`, `Dr. med.`, `M.Sc.`); one letter is an initial.
_TITLE_WORDS: Final = frozenset("dr prof dipl ing med dent rer nat phil jur mag habil sc mba phd eng".split())
# Lower-case words that stand inside a name (`Max von Mustermann`). `der`, `den`, `zu` and `zur` are left
# out: they are articles and prepositions in a line such as `Leitung der Abteilung`.
_NAME_PARTICLES: Final = frozenset("von van vom de di da du la le el al bin ter ten".split())
# `Name: Max Mustermann`: the label of the header name (a value after the colon) and the lines that are a
# heading for a name cell, not a name (`Name`, `Persönliche Angaben`).
_NAME_LABEL: Final = re.compile(
    r"(?i:(?:vor-?[ \t]{1,3}und[ \t]{1,3})?(?:vorname|nachname|familienname|name|full[ \t]{1,3}name"
    r"|first[ \t]{1,3}name|last[ \t]{1,3}name|surname)"
    r"(?:[ \t]{0,3}[/,&][ \t]{0,3}(?:vorname|nachname|familienname|name))?)[ \t]{0,3}:[ \t]{0,3}"
)
_NAME_HEADINGS: Final = frozenset(
    [
        *"name vorname nachname familienname surname".split(),
        *["full name", "first name", "last name", "vorname/nachname", "vor- und nachname"],
        *["persönliche angaben", "angaben zur person", "personal data", "personal information"],
        *["persönliche daten", "personal details"],
    ]
)
_WORD_EDGES: Final = ".,;:()[]{}|/-\u2013\u2014"


# --- finding the spans, category by category --------------------------------------------------


def _value_cell(text: str, at: int) -> int | None:
    """The end of the value line of a label that stands alone on its line, or None. `docparse` writes each
    table cell on a line of its own, so `Geburtsdatum` is followed by `01.02.1990`. The value is the next
    non-empty line, after at most two blank lines; a line that is a label line, a token or a claimed span is
    none (a second pass reads a claim as a token, and a label takes the line after it by itself)."""
    m = _NEXT_LINE.match(text, at)
    if m is None:
        return None
    value = m.start("value")
    if text[value] == _MASK or _PLACEHOLDER.match(text, value) or _PERSONAL.match(text, m.start("line")):
        return None
    return m.end()


def _is_format_mask(hint: str) -> bool:
    runs = _CAPITALS.findall(hint)
    return bool(runs) and all(len(set(run)) == 1 and run[0] in _FORMAT_LETTERS for run in runs)


def _rest_kind(rest: str) -> Literal["empty", "labels", "value"]:
    """What the rest of a label's line holds: nothing (`empty`), only label words, joiners and a format
    mask (`labels`: `/-ort`, `und`, `(TT.MM.JJJJ)`), or a value (anything else, a bracket with a digit or a
    lower-case word too)."""
    if not rest.strip(_PUNCT):
        return "empty"
    unmasked = _MASK_HINT.sub(lambda m: " " if _is_format_mask(m.group()) else m.group(), rest)
    unmasked = _OPTION_LIST.sub(" ", unmasked)
    bare = _LETTERS.sub(lambda m: " " if m.group().casefold() in _LABEL_WORDS else m.group(), unmasked)
    return "value" if bare.strip(_JOINERS) else "labels"


def _personal_spans(text: str) -> Iterator[Span]:
    """The spans of a label and the rest of its line. A label that starts its line and has nothing after it
    (or only label words and a format mask) takes the value line below it as well (one span); if there is
    none, a label of label words is claimed as it is. Anywhere else a label is a hit when anything but
    punctuation follows it."""
    position = 0
    adjacent = -1  # where the last span ended: a label may start there
    while True:
        m = _PERSONAL_ADJACENT.match(text, position) if position == adjacent else None
        m = m or _PERSONAL.search(text, position)
        if m is None:
            return
        groups = m.groupdict()
        label = next(g for g in ("line", "birth", "inline") if groups.get(g) is not None)
        start = m.start(label)
        end = position = m.end()
        kind = _rest_kind(m["rest"]) if label == "line" else ("value" if m["rest"].strip(_PUNCT) else "empty")
        if kind != "value" and label == "line" and (cell_end := _value_cell(text, end)) is not None:
            end = cell_end
        elif kind == "empty":
            continue
        end = position = adjacent = _number_run_on(text, start, end)
        yield start, end


def _number_run_on(text: str, start: int, end: int) -> int:
    """The end of a personal span, or more when a phone number that is short on the span's last line goes on
    over the line break after it (`Geschlecht (M/W/D)` / `Tel. +49 30` / `2012 3456`: the value cell holds the
    first line of the number). The span takes the rest of the number and of its line, so no part of it is
    left in the clear (`_number_lines`). The number may be glued to what stands before it in the cell
    (`max@example.org+49 30`)."""
    return _number_lines(text, start, end, rest_of_line=True)


def _number_lines(text: str, start: int, end: int, *, rest_of_line: bool) -> int:
    """`end`, or more while the last line of `text[start:end]` ends in a number that is short on it and goes
    on over the line break (`_number_end`), with the rest of that line when `rest_of_line`; the line it went
    on to may end in such a number in turn. A postal code and its city, or an e-mail address, that starts in
    the rest of a number keeps its own claim (`54563 Berlin`, `5456@example.org`), and the span ends before
    it, unless the "city" is a label that the personal rule claims."""
    while True:
        # The last number of the last line; a number is 152 characters at most, so the last 200 do.
        low = max(start, end - 200)
        line = max(low, *(text.rfind(c, low, end) + 1 for c in _EOL))
        numbers = list(_PHONE_IN_LINE.finditer(text, line, end))
        if not numbers:
            return end
        last = numbers[-1]
        stop = _number_end(text, last.start(), last.end())
        if stop is None or stop <= end:
            return end
        for pattern in (_POSTAL, _EMAIL):
            # A claim that starts in the rest of the number runs on past it (a city, a domain). A postal code
            # and its city are 50 characters at most; an e-mail address has its `@` within 65 characters of
            # where it starts, and the rest of it within 700.
            if pattern is _POSTAL:
                claim = pattern.search(text, last.end(), stop + 50)
            elif (at := text.find("@", last.end(), stop + 65)) != -1:
                claim = pattern.search(text, last.end(), at + 700)
            else:
                claim = None
            if claim is None or claim.start() >= stop:
                continue
            # A "city" that is a label (`25456 Geburtsort`) is claimed by the personal rule: nothing would
            # claim the code, so the span takes it.
            if pattern is _POSTAL and _PERSONAL.match(text, claim.start("city")):
                continue
            return max(end, _trim_back(text, claim.start(), end))
        rest = _REST_OF_LINE.match(text, stop) if rest_of_line else None
        end = stop if rest is None else rest.end()


def _regex_spans(pattern: re.Pattern[str]) -> Callable[[str], Iterator[Span]]:
    def spans(text: str) -> Iterator[Span]:
        for m in pattern.finditer(text):
            yield m.start(), m.end()

    return spans


# `Am Spring 5`, `Im Spring 5`, `Zum Spring 5`: a word of `NOT_A_STREET` after a preposition is a street name.
# A street name keeps the preposition capitalised, so the match is case-sensitive: `Erfahrung im Spring 5
# Projekt` and `Betrieb im Monitoring 24/7` are no address.
_PREPOSITION: Final = re.compile(r"(?:Am|Im|Zum|Zur)[ \t]{1,3}\Z")


def _street_spans(text: str) -> Iterator[Span]:
    """The streets with a house number. Not a street: a word of `NOT_A_STREET` (whole word, any case, the
    last part of a hyphenated word), an ordinal before `Platz` (`Erster Platz 1`)."""
    taken = 0  # where the last street ended
    position = 0
    while (m := _STREET.search(text, position)) is not None:
        start = position = m.start()
        word = (m["attached"] or "").casefold()
        if word and (word in NOT_A_STREET or word.rsplit("-", 1)[-1] in NOT_A_STREET):
            window = max(taken, start - 9)
            preposition = _PREPOSITION.search(text, window, start)
            if preposition is None or (preposition.start() > 0 and text[preposition.start() - 1].isalnum()):
                # What is no street does not hide one that starts in it (`Karriereweg 3-Heimweg 4`).
                position += 1
                continue
            start = preposition.start()
        if (m["lead_er"] or "").casefold() in _ORDINALS:
            position += 1
            continue
        # A postal code right before the street (`01067 Am Spring 5`) belongs to the same replacement.
        before = _POSTAL_BEFORE.search(text, max(taken, start - 8), start)
        start = start if before is None else before.start()
        taken = position = m.end()
        yield start, taken


def _digits(text: str) -> int:
    return sum(c in "0123456789" for c in text)


def _enough(number: str) -> int:
    number = number.lstrip("(")
    return _PHONE_MIN_DIGITS["+" if number[0] == "+" else "00" if number.startswith("00") else "0"]


def _number_end(text: str, start: int, end: int, *, second: bool = True) -> int | None:
    """Where the number `text[start:end]` ends, or None when it has too few digits. A number that is short
    on its line goes on over one line break (`_PHONE_TAIL`). So does a second number in the same match that
    is short on its line (`second`; `(0)30 23125456 030 231` / `25456`: a group that starts with 0 after a
    separator starts a number of its own), unless the next line is a number of its own; a complete number
    never takes in the next line."""
    number = text[start:end]
    complete = _digits(number) >= _enough(number)
    if complete and not second:
        return end
    if complete:
        starts = [s.start() for s in _NEW_NUMBER.finditer(number)]
        if not starts:
            return end
        number = number[starts[-1] :]
        if _digits(number) >= _enough(number):
            return end
    tail = _PHONE_TAIL.match(text, end)
    if tail is None or _digits(number) + _digits(tail.group()) < _enough(number):
        return end if complete else None
    if complete:
        own = _PHONE_ADJACENT.match(
            text, next(i for i in range(tail.start(), tail.end()) if text[i].isdigit())
        )
        if own is not None and _number_end(text, own.start(), own.end(), second=False) is not None:
            return end
    return tail.end()


def _phone_spans(text: str) -> Iterator[Span]:
    position = 0
    adjacent = -1  # where the last number ended: a number may start there
    while True:
        m = _PHONE_ADJACENT.match(text, position) if position == adjacent else None
        m = m or _PHONE.search(text, position)
        if m is None:
            return
        start, end = m.start(), m.end()
        position = end
        # More groups than one match holds (a run of numbers glued by blanks): none is left behind.
        while (more := _PHONE_MORE.match(text, end)) is not None:
            end = more.end()
        number_end = _number_end(text, start, end)
        if number_end is None:
            continue
        # A number that a line ends in goes on below; the line it goes on to may end in one too.
        end = _number_lines(text, start, number_end, rest_of_line=False)
        # A last group that starts a postal code and its city on the same line (`0 30 / 231 254 56 10115
        # Berlin`): the number ends before it when it is complete without it, and the address rule claims it.
        # (A capitalised word on the next line is no city here: `+49 30 2312 0000` / `Skills`.)
        postal = _POSTAL_INLINE.search(text, start, end + 50)
        if postal is not None and postal.start() < end:
            head = _trim_back(text, postal.start(), start)
            if _digits(text[start:head]) >= _enough(m.group()):
                end = head
        position = adjacent = end
        yield start, end


def _trim_back(text: str, stop: int, floor: int) -> int:
    """`stop`, moved back over white space and phone separators, never before `floor`."""
    while stop > floor and (text[stop - 1].isspace() or text[stop - 1] in _PHONE_SEP_CHARS):
        stop -= 1
    return stop


def _is_role_word(token: str, previous: str | None) -> bool:
    """Whether a token of a line is a title or heading word: a keyword, its feminine `-in` form (of a stem
    of `_FEMININE_STEM_MIN` letters or more), or a compound that ends in one. A word that is also a surname
    (`_COMPOUND_ONLY`) counts as the first token of a line (`previous` is None: `Leiter Controlling`,
    `Kaufmann für Büromanagement`) and after an inflected adjective (`Technischer Leiter`), not after a
    first name (`Max Kaufmann`)."""
    word = token.casefold().strip(_WORD_EDGES)
    if not word:
        return False
    titled = previous is None or _TITLE_ADJECTIVE.fullmatch(previous.strip(_WORD_EDGES)) is not None
    for piece in _WORD_SPLIT.split(word):
        stem = piece[:-2] if piece.endswith("in") and len(piece) - 2 >= _FEMININE_STEM_MIN else ""
        for candidate in (piece, stem):
            if candidate in ROLE_KEYWORDS and (titled or candidate not in _COMPOUND_ONLY):
                return True
    return any(word.endswith(k) and len(word) > len(k) for k in _COMPOUND_KEYWORDS)


def _is_role_line(tokens: list[str]) -> bool:
    return any(_is_role_word(t, tokens[i - 1] if i else None) for i, t in enumerate(tokens))


def _may_be_surname(tokens: list[str]) -> bool:
    """Whether the line holds a title word that is also a surname (`Leiter`, `Kaufmann`) and that is no
    title word where it stands (`Max Kaufmann`, `IT Leiter`, `Stellv. Leiter`, `Max Leiter/in`)."""
    return any(
        piece in _COMPOUND_ONLY
        for t in tokens
        for piece in _WORD_SPLIT.split(t.casefold().strip(_WORD_EDGES))
    )


def _is_title(token: str) -> bool:
    """`Dr.`, `Prof.`, `Dr.-Ing.`, `M.Sc.`, `J.`: ends in a period, holds title words and initials only."""
    if not token.endswith(".") or len(token) > 12:
        return False
    pieces = [p for p in _TITLE_SPLIT.split(token) if p]
    return bool(pieces) and all(len(p) == 1 or p.casefold() in _TITLE_WORDS for p in pieces)


def _name_words(tokens: list[str]) -> list[str] | None:
    """The words of a person's name: two to four capitalised words, a particle allowed between two of them,
    and titles and initials left out. None when the tokens are anything else (a period ends a title or an
    initial only, not a word of a sentence)."""
    words: list[str] = []
    capitalised = 0
    for i, token in enumerate(tokens):
        if _is_title(token):
            continue
        if _NAME_WORD.fullmatch(token) and token[0].isupper():
            capitalised += 1
        elif not (words and i < len(tokens) - 1 and token in _NAME_PARTICLES):
            return None
        words.append(token)
    return words if 2 <= capitalised <= 4 and words[-1] not in _NAME_PARTICLES else None


def _header_names(text: str) -> list[tuple[Span, list[str]]]:
    """The header names, each as the span of the name in its line and its words: the first line that holds
    a name, and the lines before it that may be a name or a title.

    Lines of white space are passed over. A line that holds a title or heading word (`ROLE_KEYWORDS`), or is
    a heading for a name cell (`Name`, `Name:`), is passed over too, `_HEADER_SKIPS` times at most.
    `Name:` with a name after it (also `Vorname/Nachname:`) is the name label: the name after the colon
    is the header name. A line with a title word that is also a surname where it may be a surname
    (`_may_be_surname`: `Max Kaufmann`, `IT Leiter`, `Stellv. Leiter`) is passed over as well (it counts),
    but when it is a name it is a header name too: the search goes on, so that the name below a title that
    reads like a name is never left in the clear (when in doubt, both lines are names). Any other line
    decides: it is the name when it is two to four capitalised tokens with no digit and no punctuation, and
    the search ends. A line that holds a token or a claimed span and is no title or heading line ends the
    search too, which is what keeps a second pass from finding a name the first one did not (a line the
    first pass replaced is such a line); a title or heading line that holds one is passed over in both.
    """
    found: list[tuple[Span, list[str]]] = []
    skipped = 0
    for line in _LINE.finditer(text):
        raw = line.group()
        content = raw.strip()
        if not content:
            continue
        # Claims and tokens do not count: a claim is as long as its text, a second pass reads a shorter token.
        if len(_PLACEHOLDER.sub("", content).replace(_MASK, "")) > _HEADER_LINE_MAX:
            return found
        start = line.start() + len(raw) - len(raw.lstrip())
        label = _NAME_LABEL.match(content)
        value = content[label.end() :] if label is not None else content
        if label is not None:
            start += label.end() + len(value) - len(value.lstrip())
            value = value.lstrip()
        tokens = value.split()
        # A token reads as the mask it stands for in the first pass (`[PERSONAL]` is no title word
        # `personal`).
        neutral = _PLACEHOLDER.sub(lambda m: _MASK * len(m.group()), value).split()
        heading = not value or (
            label is None and (content.casefold().strip(" :.") in _NAME_HEADINGS or _is_role_line(neutral))
        )
        # A title or heading line is passed over also when it holds a claim (`Zur Spring 5, Projekt`, a second
        # pass reads the same title word next to a token); any other such line ends the search.
        if not heading and (_MASK in content or _PLACEHOLDER.search(content)):
            return found
        words = None if heading or any(c in "0123456789" for c in content) else _name_words(tokens)
        undecided = not heading and label is None and _may_be_surname(tokens)
        if words is not None and not heading:
            found.append(((start, start + len(value)), words))
        if not (heading or undecided):
            return found
        skipped += 1
        if skipped > _HEADER_SKIPS:
            return found
    return found


def _name_pattern(words: list[str]) -> re.Pattern[str]:
    """The later occurrences of a name: its words joined by one to three blanks, in any case."""
    joined = r"[ \t\xa0]{1,3}".join(map(re.escape, words))
    return re.compile(r"(?<![\w\-])" + joined + r"(?![\w\-])", re.IGNORECASE)


def _name_spans(text: str) -> Iterator[Span]:
    """The header names, then every later occurrence of their words as a full name, in any case.
    Occurrences of two header names that overlap are one span."""
    headers = _header_names(text)
    spans = [span for span, _ in headers]
    yield from spans
    masked = _masked(text, spans)
    later = sorted(m.span() for _, words in headers for m in _name_pattern(words).finditer(masked))
    merged: list[Span] = []
    for start, end in later:
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    yield from merged


def _masked(text: str, spans: list[Span]) -> str:
    """`text` with each of the (ordered, disjoint) spans replaced by as many mask characters."""
    out: list[str] = []
    position = 0
    for start, end in spans:
        out += [text[position:start], _MASK * (end - start)]
        position = end
    out.append(text[position:])
    return "".join(out)


# The local part of an e-mail address at the end of a URL (`github.com/max/geb.a` before `@b.de`).
_LOCAL_PART_END: Final = re.compile(r"[\w.%+\-]{1,64}\Z")


def _url_spans(text: str) -> Iterator[Span]:
    """The URLs. A URL that runs into an e-mail address or a phone number glued to it ends before it, so the
    address or the number keeps its own claim and no part of it is left in the clear: `github.com/max/geb.a`
    before `@b.de`, `https://example.org/a\u2013+49 30 ...` (the number goes on past the URL). An `@` after a
    URL with no e-mail address after it (`linkedin.com/in/max@x`) leaves the URL whole."""
    position = 0
    while (m := _URL.search(text, position)) is not None:
        start, end = m.span()
        cut = end
        if text.startswith("@", end):
            local = _LOCAL_PART_END.search(text, max(start, end - 64), end)
            if local is not None and _EMAIL.match(text, local.start()) is not None:
                cut = local.start()
        else:
            for p in range(max(start + 1, end - 16), end):
                if text[p] in "+0" and not text[p - 1].isdigit():
                    number = _PHONE_ADJACENT.match(text, p)
                    if number is not None and number.end() > end and _number_end(text, p, number.end()):
                        cut = p
                        break
        url = _URL.match(text, start, cut) if cut < end else m
        position = end if url is None else url.end()
        if url is not None:
            yield start, url.end()


_FINDERS: Final[Mapping[Category, tuple[Callable[[str], Iterator[Span]], ...]]] = MappingProxyType(
    {
        "personal": (_personal_spans,),
        "url": (_url_spans,),
        "email": (_regex_spans(_EMAIL),),
        "phone": (_phone_spans,),
        "address": (_street_spans, _regex_spans(_POSTAL)),
        "name": (_name_spans,),
    }
)


def _absorb(spans: list[Span], claims: list[Replacement]) -> tuple[list[Span], list[Replacement]]:
    """Spans that run over claimed characters take in the claims they overlap (a second pass reads a claim as
    one token inside the span). Both lists are ordered and disjoint; so are the results."""
    out: list[Span] = []
    kept: list[Replacement] = []
    j = 0
    for start, end in spans:
        while j < len(claims) and claims[j].end <= start:
            kept.append(claims[j])
            j += 1
        while j < len(claims) and claims[j].start < end:
            start, end = min(start, claims[j].start), max(end, claims[j].end)
            j += 1
        out.append((start, end))
    return out, kept + claims[j:]


def _detect(text: str, *, with_name: bool, with_personal: bool = True) -> tuple[Replacement, ...]:
    """The replacements of the rules in the original text, ordered by position.

    The categories run in `APPLY_ORDER` on the text with everything claimed so far masked out. A claim can
    take away the characters that kept an earlier category's pattern from matching (a phone number that
    ends in the middle of a word was a word character before `www.`), so the sequence repeats on what is
    left, up to `_MAX_ROUNDS` times, until a round claims nothing. The masked text is what a second
    `strip_contacts` would read, tokens for masks, so this is what makes the stripping idempotent.
    """
    masked = text
    found: list[Replacement] = []
    for _ in range(_MAX_ROUNDS):
        new = 0
        for category in APPLY_ORDER:
            if (category == "name" and not with_name) or (category == "personal" and not with_personal):
                continue
            for finder in _FINDERS[category]:
                spans = sorted(finder(masked))
                if any(_MASK in masked[start:end] for start, end in spans):
                    spans, found = _absorb(spans, found)
                found = [*found, *(Replacement(category, start, end) for start, end in spans)]
                found.sort(key=lambda r: r.start)
                masked = _masked(masked, spans)
                new += len(spans)
        if new == 0:
            break
    return tuple(found)


# --- the edit set --------------------------------------------------------------------------------


def _check_span(span: Span, length: int) -> None:
    start, end = span
    if not 0 <= start < end <= length:
        raise ValueError(f"span ({start}, {end}) is not inside a text of {length} characters")


def _uncovered(
    original: str, claim: Replacement, staying: list[Replacement], ends: list[int]
) -> Iterator[Span]:
    """The parts of `claim` that no staying replacement covers (ordered, disjoint, with `ends` their ends),
    without the white space at their edges; a part with no letter or digit is no part (it reveals nothing)."""
    position = claim.start
    k = bisect_right(ends, claim.start)  # the first staying replacement that ends after the claim starts
    while position < claim.end:
        cover = staying[k] if k < len(staying) and staying[k].start < claim.end else None
        stop = claim.end if cover is None else max(position, cover.start)
        part = original[position:stop]
        if any(c.isalnum() for c in part):
            yield position + len(part) - len(part.lstrip()), stop - len(part) + len(part.rstrip())
        if cover is None:
            return
        position = max(position, cover.end)
        k += 1


def _inner_claims(original: str, base: tuple[Replacement, ...], edits: Edits) -> list[Replacement]:
    """The replacements that a restored personal replacement lets through. A personal span takes in
    everything else in its line (e-mail addresses, numbers), so those claims are not in `base`; once the span
    is restored they apply again: the other categories run over the original text without the personal rule,
    and a claim that overlaps a restored span counts (also one that runs past its end: `Kinder` / `030 2312`
    / `5456`). A claim that runs into a replacement that stays is clipped to the parts that none covers, each
    with the claim's category (`Kinder` / `030 2312` / `54563 Berlin`: the number runs into the postal
    code, and `030 2312` is replaced), so no part of it is left in the clear."""
    spans = [
        (r.start, r.end) for r in base if r.category == "personal" and (r.start, r.end) in edits.restored
    ]
    if not spans:
        return []
    staying = [r for r in base if (r.start, r.end) not in edits.restored]
    ends = [r.end for r in staying]
    inner: list[Replacement] = []
    j = 0
    for claim in _detect(original, with_name=True, with_personal=False):
        while j < len(spans) and spans[j][1] <= claim.start:
            j += 1
        if j == len(spans) or spans[j][0] >= claim.end:
            continue
        inner += (Replacement(claim.category, s, e) for s, e in _uncovered(original, claim, staying, ends))
    return inner


def _build(original: str, base: tuple[Replacement, ...], edits: Edits) -> StripResult:
    for span in edits.removed:
        _check_span(span, len(original))
    # The replacements still in force (those of the fresh strip that are not restored, and the contacts inside
    # a restored personal span) and the removed spans, as one list sorted by position. Overlapping entries
    # merge into one group; a group that holds a removed span is `[REMOVED]` over its union.
    # A restored span names the personal replacement, not a contact of the same extent inside it.
    personal = {(r.start, r.end) for r in base if r.category == "personal"}
    restored_inner = edits.restored - personal
    parts: list[tuple[int, int, Replacement | None]] = [
        (r.start, r.end, r) for r in base if (r.start, r.end) not in edits.restored
    ]
    parts += [
        (c.start, c.end, c)
        for c in _inner_claims(original, base, edits)
        if (c.start, c.end) not in restored_inner
    ]
    parts += [(start, end, None) for start, end in edits.removed]
    parts.sort(key=lambda part: part[:2])
    # Each group: its span, and the replacement it is (one that stays) or None for `[REMOVED]`.
    groups: list[tuple[int, int, Replacement | None]] = []
    kept: list[Replacement] = []
    index = 0
    while index < len(parts):
        start, end, first = parts[index]
        members = [first]
        index += 1
        while index < len(parts) and parts[index][0] < end:
            end = max(end, parts[index][1])
            members.append(parts[index][2])
            index += 1
        if len(members) == 1 and first is not None:
            kept.append(first)
            groups.append((start, end, first))
        else:
            groups.append((start, end, None))
    # The text and its map in one pass (the module docstring's review map).
    fresh_index = {r: i for i, r in enumerate(base)}
    out: list[str] = []
    segments: list[Segment] = []
    position = cursor = 0  # in the original, in the stripped text

    def keep_text(stop: int) -> None:
        nonlocal cursor
        if stop > position:
            segments.append(Segment("text", cursor, cursor + stop - position, position, stop))
            out.append(original[position:stop])
            cursor += stop - position

    for start, end, member in groups:
        keep_text(start)
        if member is None:
            token = REMOVED
            segment = Segment("removed", cursor, cursor + len(token), start, end)
        else:
            token = REPLACEMENT[member.category]
            at = fresh_index.get(member)
            segment = Segment(
                "replacement",
                cursor,
                cursor + len(token),
                start,
                end,
                category=member.category,
                replacement_index=at,
                restorable=at is not None or (member.start, member.end) not in personal,
            )
        out.append(token)
        segments.append(segment)
        cursor += len(token)
        position = end
    keep_text(len(original))
    counts = dict.fromkeys(CATEGORIES, 0)
    for r in kept:
        counts[r.category] += 1
    return StripResult("".join(out), MappingProxyType(counts), tuple(kept), edits, tuple(segments))


def apply_edits(original: str, edits: Edits) -> StripResult:
    """The stripped text of `original` with the user's `edits` applied (Task 14 rebuilds with this on every
    edit request). The result depends on the edit set only, not on the order the edits were made in."""
    return _build(original, _detect(original, with_name=True), edits)


def strip_contacts(text: str) -> StripResult:
    """Replace contact details and personal data in `text` (§12.3); the counts hold no value."""
    return apply_edits(text, Edits())


def restore(original: str, result: StripResult, index: int) -> StripResult:
    """Put replacement `index` of `strip_contacts(original)` back (the user's false positive).

    `result` carries the edits made so far; the result is built from the original with one more restored
    span. Raises `IndexError` when `index` names no replacement of `strip_contacts(original)`.
    """
    base = _detect(original, with_name=True)
    if not 0 <= index < len(base):
        raise IndexError(f"replacement {index} of {len(base)}")
    target = base[index]
    edits = replace(result.edits, restored=result.edits.restored | {(target.start, target.end)})
    return _build(original, base, edits)


def remove_span(original: str, result: StripResult, start: int, end: int) -> StripResult:
    """Mark the span `original[start:end]` `[REMOVED]` as well. Raises `ValueError` for a span outside the
    original text; its offsets never refer to the stripped string."""
    _check_span((start, end), len(original))
    edits = replace(result.edits, removed=result.edits.removed | {(start, end)})
    return _build(original, _detect(original, with_name=True), edits)


def scan_value(value: str) -> list[Category]:
    """The categories found in one profile value by the same rules without the name rule, each once, in the
    order of `Category` (email, phone, url, address, personal)."""
    found = {r.category for r in _detect(value, with_name=False)}
    return [c for c in CATEGORIES if c in found]
