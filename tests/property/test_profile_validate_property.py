"""`validate_profile` never raises (spec §12.4, M2 Task 12): for any profile the schema accepts, including
malformed dates, NaN and infinite numbers, empty and oversized lists and strings full of contact-like values,
it returns a profile of the same type and issues with well-formed paths and no quoted value; the input is not
changed, and validating the repaired profile again repairs nothing more.

Every datum is generated; no real person's data is used.
"""

import re
from datetime import date
from typing import Any, get_args

from hypothesis import given, settings
from hypothesis import strategies as st
from jobhunter.llm.schemas.profile import (
    Certification,
    ConfirmedProfile,
    Education,
    Language,
    ProfileOut,
    Role,
    Skill,
    TargetTitle,
    UserEntered,
)
from jobhunter.profile.validate import Issue, validate_profile

TODAY = date(2026, 10, 20)
CODES = {
    "limit",
    "date_invalid",
    "date_order",
    "years_mismatch",
    "contact_removed",
    "ungrounded",
    "language_without_evidence",
    "code_other",
    "unverifiable",
}
PATH = re.compile(r"[a-z_]+(\[\d+\])?(\.[a-z_]+(\[\d+\])?)*")
CONTACTS = [
    "max.mustermann@example.org",
    "+49 30 1234 5678",
    "https://example.org/max",
    "www.example.org",
    "Musterstraße 12",
    "10115 Berlin",
    "Geburtsdatum: 01.02.1990",
]
TEXT = "Backend-Entwickler\n5 Jahre Python in der Größe der Datenbank\nEnglisch \u2013 C1\n"

plain = st.text(max_size=40)  # umlauts, ß, control characters, combining marks and all
texts = st.one_of(
    plain,
    st.sampled_from(CONTACTS),
    st.builds(lambda a, c: f"{a} {c}", plain, st.sampled_from(CONTACTS)),
    st.sampled_from(["", " ", "Größe", "GRÖSSE der Datenbank", "5 Jahre Python", "Englisch - C1"]),
    st.text(min_size=300, max_size=320),
)
optional_texts = st.one_of(st.none(), texts)
numbers = st.floats(allow_nan=True, allow_infinity=True) | st.integers(-100, 100000).map(float)
optional_numbers = st.one_of(st.none(), numbers)
years = st.one_of(st.none(), st.integers(-100, 100000))
day = st.dates(min_value=date(1900, 1, 1), max_value=date(2100, 12, 31))
date_texts = st.one_of(
    day.map(date.isoformat),
    day.map(lambda d: d.strftime("%Y-%m")),
    day.map(lambda d: d.strftime("%Y")),
    plain,
    st.sampled_from(
        ["", "present", "Present", "2019-02-30", "0000-01-01", "2019-13", "\uff12\uff10\uff11\uff19-03-01"]
    ),
)
starts = st.one_of(st.none(), date_texts)
ends = st.one_of(st.none(), date_texts, st.just("present"))


def lists(element: st.SearchStrategy[Any], max_size: int = 4) -> st.SearchStrategy[list[Any]]:
    return st.lists(element, max_size=max_size)


roles = st.builds(
    Role,
    title=texts,
    employer=optional_texts,
    start=starts,
    end=ends,
    seniority=st.sampled_from(get_args(Role.model_fields["seniority"].annotation)),
    domain=optional_texts,
    highlights=lists(texts, 10),
)
skills = st.builds(
    Skill,
    name=texts,
    category=st.sampled_from(get_args(Skill.model_fields["category"].annotation)),
    level=st.sampled_from(get_args(Skill.model_fields["level"].annotation)),
    years=optional_numbers,
    evidence=texts,
)
languages = st.builds(
    Language,
    code=st.sampled_from(get_args(Language.model_fields["code"].annotation)),
    code_other=optional_texts,
    cefr=st.sampled_from(get_args(Language.model_fields["cefr"].annotation)),
    evidence=texts,
)
profiles = st.builds(
    ProfileOut,
    summary=texts,
    target_titles=lists(st.builds(TargetTitle, title=texts, lang=st.sampled_from(["de", "en"])), 14),
    seniority=st.sampled_from(get_args(ProfileOut.model_fields["seniority"].annotation)),
    total_years_experience=optional_numbers,
    roles=lists(roles, 5),
    skills=lists(skills, 5),
    languages=lists(languages, 3),
    education=lists(
        st.builds(
            Education,
            level=st.sampled_from(get_args(Education.model_fields["level"].annotation)),
            field=texts,
            institution=optional_texts,
            year=years,
        )
    ),
    certifications=lists(st.builds(Certification, name=texts, issuer=optional_texts, year=years)),
    domains=lists(texts, 22),
)
transmitted = st.one_of(st.none(), st.just(""), st.just(TEXT), plain)

fuzz = settings(max_examples=300, deadline=None)


def dump(profile: ProfileOut) -> str:
    return profile.model_dump_json()  # NaN is not equal to itself, its JSON form (null) is


def strings_of(node: object) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [s for v in node.values() for s in strings_of(v)]
    if isinstance(node, list):
        return [s for v in node for s in strings_of(v)]
    return []


@fuzz
@given(out=profiles, text=transmitted)
def test_validate_profile_never_raises_and_its_issues_are_well_formed(
    out: ProfileOut, text: str | None
) -> None:
    before = dump(out)
    fixed, issues = validate_profile(out, transmitted_text=text, today=TODAY)
    assert isinstance(fixed, ProfileOut)
    assert dump(out) == before
    assert all(isinstance(i, Issue) and i.code in CODES and PATH.fullmatch(i.path) for i in issues)
    quotable = {s for s in strings_of(out.model_dump()) if len(s.strip()) >= 12}
    assert not [s for s in quotable for i in issues if s in i.message]


@fuzz
@given(out=profiles, text=transmitted)
def test_validating_the_repaired_profile_again_repairs_nothing_more(
    out: ProfileOut, text: str | None
) -> None:
    once, _ = validate_profile(out, transmitted_text=text, today=TODAY)
    twice, issues = validate_profile(once, transmitted_text=text, today=TODAY)
    assert dump(twice) == dump(once)
    assert not {"contact_removed", "date_invalid"} & {i.code for i in issues}


@settings(max_examples=60, deadline=None)
@given(out=profiles)
def test_a_confirmed_profile_keeps_its_user_entered_block(out: ProfileOut) -> None:
    confirmed = ConfirmedProfile(
        **out.model_dump(), user_entered=UserEntered(work_authorization_de="authorized")
    )
    fixed, _ = validate_profile(confirmed, transmitted_text=TEXT, today=TODAY)
    assert isinstance(fixed, ConfirmedProfile)
    assert fixed.user_entered == confirmed.user_entered
