"""Contact stripping (spec §12.3; M2 Task 10): pure, no I/O, no logging, no value in any message.

Before any CV text leaves the machine, `strip_contacts` replaces contact details and personal data with
tokens (`[EMAIL]`, `[PHONE]`, `[URL]`, `[ADDRESS]`, `[PERSONAL]`, `[NAME]`) and the user reviews the
result. It is a best-effort aid; the user's review is the guarantee. Task 11 calls `strip_contacts`, Task
12 calls `scan_value` on the extracted profile, and Task 14 rebuilds the reviewed text with `apply_edits`.

**The rules** are the table of §12.3, rule by rule. Where a rule needed a decision:

- *E-mail*: a local part of word characters and `. % + -`, `@`, a dotted domain, a TLD of letters only.
- *Phone*: `+<cc>`, `00<cc>` or a leading `0` and a digit 1-9, then digit groups with space, tab, `/`, `-`,
  `.` or `()` as separators (Unicode dashes too) and at least 7 digits after the prefix (the country code
  of `+` and `00` counts as digits). A number that is short on its first line goes on over **one** line
  break; a number that is complete never takes in the next line. A date is no phone number: `01.02.1990`
  has one digit after its zero, where a national number has an area code of two digits or more.
- *URL*: `http(s)://...`, `www....`, `linkedin.com/in/...`, `xing.com/profile/...` and `github.com/<user>`
  (with one or two path segments after it). Trailing punctuation stays outside.
- *Address*: a street and a house number of one to three digits (`12`, `12a`, `12-14`). The street is a word
  that ends in a suffix of the table (`strasse` too), or a capitalised word before `Straße`, `Allee`,
  `Gasse`, `Ufer`, `Damm`, `Ring` or `Weg`; `NOT_A_STREET` holds the English words that end in a suffix
  (`Spring 5`). The postal code and city are the spec's regex: five digits, white space, a capitalised word
  (the white space and the city are bounded).
- *Personal*: a birth label anywhere; a protected-attribute label at the start of a line (after a bullet or
  a table pipe) or followed by a colon anywhere. The span is the label and the rest of its line, at most
  `_REST_OF_LINE_MAX` characters; a label with no value after it is no hit. `Kinder`, `Children` and
  `Gender` need a colon, a pipe or a tab after them (`Kinder- und Jugendarbeit` stays).
- *Name*: the header name and its later occurrences; see `ROLE_KEYWORDS` and `_header_name`.

**The order the categories claim text in** is `APPLY_ORDER`: personal, url, email, phone, address, name.
Each category runs on the text with everything claimed so far masked out (NUL, which no pattern matches or
crosses), so a span that one category has claimed is never claimed, or counted, by another. The order
settles the overlaps: a birth line takes the date in it (which is also phone-shaped) and the rest of its
line; a URL takes an e-mail address in its query; an e-mail address takes the digits of its local part
before the phone rule sees them; a phone number takes its digits before the postal-code rule; the name rule
looks at what is left. The sequence repeats on what is left until a round claims nothing (`_detect`), so a
second `strip_contacts` finds nothing new: it is idempotent, and a token matches no rule.

**Offsets** (`Replacement`, `Edits`, `remove_span`) always refer to the **original** text, never to the
stripped string. All patterns are compiled once at import and built from bounded quantifiers whose
neighbouring parts cannot match the same characters, so a match is linear in the text (the M1 F1 lesson;
see the unit tests).

**Edits** (for Task 14). The user's review is an `Edits` value: the spans of the replacements put back
(`restored`) and the extra spans marked `[REMOVED]` (`removed`), both as `(start, end)` offsets into the
original. The stripped text is a pure function of the original and its `Edits`, so Task 14 stores
`StripResult.edits` and rebuilds with `apply_edits(original, edits)` on every request; the order the edits
were made in does not matter. `restore(original, result, index)` adds replacement `index` of
`strip_contacts(original)` (the same list whatever was restored before) and `remove_span(original, result,
start, end)` adds an extra span; both return the fresh `StripResult` of the extended edit set. A removed
span that overlaps a replacement, or another removed span, merges with it into one `[REMOVED]` over the
union, so a half-marked e-mail address never leaves its other half in the text. A restored span that names
no replacement of the original is ignored, so a stale edit set cannot put back the wrong thing.

`scan_value(value)` runs the same rules without the name rule on one profile value (Task 12).
"""

import re
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
_COMPOUND_KEYWORDS: Final = tuple(sorted(k for k in ROLE_KEYWORDS if len(k) >= 6))
# English words that end in a street suffix and read like a street before a number (`Spring 5`, `String 2`).
NOT_A_STREET: Final = frozenset(
    "spring string engineering monitoring offering hiring scoring steering mentoring".split()
    + "tutoring filtering authoring referring reporting".split()
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


@dataclass(frozen=True)
class StripResult:
    """The stripped text, the counts per category (never a value), the replacements still in the text, and the
    edit set this result was built from (empty for `strip_contacts`)."""

    stripped_text: str
    report: Mapping[Category, int]
    replacements: tuple[Replacement, ...]
    edits: Edits = field(default_factory=Edits)


# --- the patterns --------------------------------------------------------------------------------

_EOL: Final = "\r\n\x0b\x0c\x85\u2028\u2029"
# The rest of a line runs over claimed (masked) characters too: a second pass reads their tokens there.
_REST: Final = rf"[^{_EOL}]{{0,{_REST_OF_LINE_MAX}}}"
# A label inside a word, a URL or an e-mail address is no label.
_LABEL_START: Final = r"(?<![\w/@.:=&?%#~+\-])"
_NO_VALUE: Final = " \t\xa0:|.,;\u2022\u00b7*>\u2013\u2014-"
_DISTINCT_LABELS: Final = (
    r"Familienstand|Marital[ \t]{1,3}status|Staatsangeh(?:ö|oe)rigkeit|Nationalit(?:ä|ae)t|Nationality"
    r"|Konfession|Religion|Geschlecht"
)
_AMBIGUOUS_LABELS: Final = r"Kinder|Children|Gender"
_BIRTH_LABELS: Final = (
    r"Geburtsdatum|Geburtsort|Date[ \t]{1,3}of[ \t]{1,3}birth|Place[ \t]{1,3}of[ \t]{1,3}birth|DOB|geboren"
)
_LINE_START: Final = rf"(?<![^{_EOL}])[ \t\xa0\u2022\u00b7*|>\-\u2013\u2014]{{0,10}}"
# One pattern for all the labels, so that the first label of a line takes the rest of the line, other labels
# included: a birth label anywhere; a protected-attribute label at the start of a line (after a bullet or a
# table pipe) or followed by a colon anywhere; `Kinder`, `Children` and `Gender` only with a colon, a pipe
# or a tab after them.
_PERSONAL: Final = re.compile(
    rf"(?:{_LINE_START}(?P<line>(?i:{_DISTINCT_LABELS})(?!\w)"
    rf"|(?i:{_AMBIGUOUS_LABELS})(?!\w)(?=[ \t]*[:|]|\t))"
    rf"|{_LABEL_START}(?:(?P<birth>(?i:{_BIRTH_LABELS})(?!\w)|(?i:geb\.))"
    rf"|(?P<inline>(?i:{_DISTINCT_LABELS}|{_AMBIGUOUS_LABELS})(?!\w)(?=[ \t]{{0,3}}:))))"
    rf"(?P<rest>{_REST})"
)

_URL_CHAR: Final = r"[^\s<>\"'\[\]\x00]"
_URL: Final = re.compile(
    r"(?<![\w@./\-])(?:"
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
_PHONE_GROUPS: Final = rf"(?:{_PHONE_SEP}{{1,3}}[0-9]{{1,14}}){{0,8}}(?![0-9])"
_PHONE: Final = re.compile(
    rf"(?<![\w+])(?:\((?=0[1-9][0-9]{{1,5}}\)))?"
    rf"(?:\+[0-9]{{1,14}}|00[0-9]{{1,14}}|0[1-9][0-9]{{1,12}}){_PHONE_GROUPS}"
)
# What a number that is short on its first line may go on with: one line break, then more digit groups.
_PHONE_TAIL: Final = re.compile(rf"{_PHONE_SEP}{{0,3}}\r?\n{_PHONE_SEP}{{0,3}}[0-9]{{1,14}}{_PHONE_GROUPS}")
# The digits a phone number holds at least, the digits of its prefix included (`0`: 7 after it; `+`, `00`: a
# country code and 7).
_PHONE_MIN_DIGITS: Final = {"+": 8, "0": 8, "00": 10}

_HOUSE: Final = r"[0-9]{1,3}[A-Za-z]?(?:[ \t]?[/\-][ \t]?[0-9]{1,3}[A-Za-z]?)?(?!\w)"
_STREET: Final = re.compile(
    r"(?<![\w\-])(?:"
    r"(?P<attached>[\w\-]{1,40}(?i:stra(?:ß|ss)e|str\.|weg|allee|platz|gasse|ring|damm|ufer))"
    r"|[A-ZÄÖÜ][\w\-]{1,30}[ \t]{1,3}(?:Stra(?:ß|ss)e|Str\.|Allee|Gasse|Ufer|Damm|Ring|Weg)"
    rf")[ \t]{{0,2}}{_HOUSE}"
)
# Spec §12.3: `\b\d{5}\s+[A-ZÄÖÜ][\wäöüß-]+`, with the white space and the city bounded.
_POSTAL: Final = re.compile(r"(?<!\w)[0-9]{5}\s{1,3}[A-ZÄÖÜ][\w\-]{1,40}(?![\w\-])")

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
_WORD_EDGES: Final = ".,;:()[]{}|/-\u2013\u2014"


# --- finding the spans, category by category --------------------------------------------------


def _personal_spans(text: str) -> Iterator[Span]:
    """The spans of a label and the rest of its line; a label with nothing after it is skipped."""
    for m in _PERSONAL.finditer(text):
        if m["rest"].strip(_NO_VALUE):
            label = next(g for g in ("line", "birth", "inline") if m[g] is not None)
            yield m.start(label), m.end()


def _regex_spans(pattern: re.Pattern[str]) -> Callable[[str], Iterator[Span]]:
    def spans(text: str) -> Iterator[Span]:
        for m in pattern.finditer(text):
            yield m.start(), m.end()

    return spans


def _street_spans(text: str) -> Iterator[Span]:
    for m in _STREET.finditer(text):
        word = m["attached"]
        if word is None or word.casefold() not in NOT_A_STREET:
            yield m.start(), m.end()


def _digits(text: str) -> int:
    return sum(c in "0123456789" for c in text)


def _phone_spans(text: str) -> Iterator[Span]:
    position = 0
    while (m := _PHONE.search(text, position)) is not None:
        start, end = m.start(), m.end()
        position = end
        number = m.group().lstrip("(")
        enough = _PHONE_MIN_DIGITS["+" if number[0] == "+" else "00" if number.startswith("00") else "0"]
        digits = _digits(number)
        if digits < enough:
            tail = _PHONE_TAIL.match(text, end)
            if tail is None or digits + _digits(tail.group()) < enough:
                continue
            end = position = tail.end()
        yield start, end


def _is_role_word(token: str) -> bool:
    word = token.casefold().strip(_WORD_EDGES)
    if not word:
        return False
    if any(piece in ROLE_KEYWORDS for piece in _WORD_SPLIT.split(word)):
        return True
    return any(word.endswith(k) and len(word) > len(k) for k in _COMPOUND_KEYWORDS)


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


def _header_name(text: str) -> tuple[Span, list[str]] | None:
    """The header name: the span of the first line that is a name, and its words.

    Lines of white space are passed over. A line that holds a title or heading word (`ROLE_KEYWORDS`) is
    passed over too, `_HEADER_SKIPS` times at most; any other line decides: it is the name when it is two to
    four capitalised tokens with no digit and no punctuation, else there is no header name. A line that
    holds a token or a claimed span is never a name or a heading, which is what keeps a second pass from
    finding a name the first one did not (a line the first pass replaced is such a line).
    """
    skipped = 0
    for line in _LINE.finditer(text):
        content = line.group().strip()
        if not content:
            continue
        if len(content) > _HEADER_LINE_MAX or _MASK in content or _PLACEHOLDER.search(content):
            return None
        tokens = content.split()
        if any(_is_role_word(t) for t in tokens):
            skipped += 1
            if skipped > _HEADER_SKIPS:
                return None
            continue
        words = _name_words(tokens)
        if words is None or any(c in "0123456789" for c in content):
            return None
        start = line.start() + line.group().index(content[0])
        return (start, start + len(content)), words
    return None


def _name_spans(text: str) -> Iterator[Span]:
    """The header name, then every later occurrence of its tokens as a full name, in any case."""
    header = _header_name(text)
    if header is None:
        return
    span, words = header
    yield span
    pattern = re.compile(
        r"(?<![\w\-])" + r"[ \t\xa0]{1,3}".join(map(re.escape, words)) + r"(?![\w\-])", re.IGNORECASE
    )
    for m in pattern.finditer(_masked(text, [span])):
        yield m.start(), m.end()


def _masked(text: str, spans: list[Span]) -> str:
    """`text` with each of the (ordered, disjoint) spans replaced by as many mask characters."""
    out: list[str] = []
    position = 0
    for start, end in spans:
        out += [text[position:start], _MASK * (end - start)]
        position = end
    out.append(text[position:])
    return "".join(out)


_FINDERS: Final[Mapping[Category, tuple[Callable[[str], Iterator[Span]], ...]]] = MappingProxyType(
    {
        "personal": (_personal_spans,),
        "url": (_regex_spans(_URL),),
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


def _detect(text: str, *, with_name: bool) -> tuple[Replacement, ...]:
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
            if category == "name" and not with_name:
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


def _build(original: str, base: tuple[Replacement, ...], edits: Edits) -> StripResult:
    for span in edits.removed:
        _check_span(span, len(original))
    # The replacements still in force and the removed spans, as one list sorted by position. Overlapping
    # entries merge into one group; a group that holds a removed span is `[REMOVED]` over its union.
    parts: list[tuple[int, int, Replacement | None]] = [
        (r.start, r.end, r) for r in base if (r.start, r.end) not in edits.restored
    ]
    parts += [(start, end, None) for start, end in edits.removed]
    parts.sort(key=lambda part: part[:2])
    segments: list[tuple[int, int, str]] = []
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
            segments.append((start, end, REPLACEMENT[first.category]))
        else:
            segments.append((start, end, REMOVED))
    out: list[str] = []
    position = 0
    for start, end, token in segments:
        out += [original[position:start], token]
        position = end
    out.append(original[position:])
    counts = dict.fromkeys(CATEGORIES, 0)
    for r in kept:
        counts[r.category] += 1
    return StripResult("".join(out), MappingProxyType(counts), tuple(kept), edits)


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
