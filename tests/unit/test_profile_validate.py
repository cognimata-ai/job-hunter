"""Profile validation (spec §12.4, §12.3 grounding and re-scan, M2 Task 12): the repairs and the issues of
`validate_profile`, every semantic limit at its edge and one past it, and `validate_user_entered`.

The module is pure: nothing here needs a database, a clock or the network. Every person, employer and contact
value is invented (Max Mustermann, Beispiel GmbH, example.org).
"""

import re
from collections.abc import Callable
from datetime import date
from typing import Any

import pytest
from jobhunter.llm.schemas.profile import (
    Certification,
    ConfirmedProfile,
    Education,
    Language,
    ProfileOut,
    Role,
    SecurityClearance,
    Skill,
    TargetTitle,
    UserEntered,
)
from jobhunter.profile import schema
from jobhunter.profile.validate import Issue, validate_profile, validate_user_entered

TODAY = date(2026, 10, 20)

# The reviewed text the model received (placeholders where contact data was stripped). Grounding is checked
# against it: umlauts, ß, case and line breaks are the points where an exact comparison would fail.
REVIEWED = (
    "[NAME]\n[EMAIL] | [PHONE]\n\n"
    "Berufserfahrung\n"
    "03/2019 - 03/2024  Senior Backend Developer, Beispiel GmbH\n"
    "- Built the billing service for 40,000 customers\n"
    "- Verantwortung für die\nAbrechnung der Kunden\n\n"
    "Kenntnisse\n"
    "5 Jahre Python in der Backend-Entwicklung\n"
    "PostgreSQL, Docker\n"
    "Größe der Datenbank: 2 TB\n"
    "Straße der Pioniere 7\n\n"
    "Sprachen\n"
    "Deutsch (Muttersprache)\n"
    "Englisch \u2013 C1\n"
    "Französisch Grundkenntnisse\n\n"
    "Ausbildung\n"
    "Master Informatik, TU Beispielstadt, 2018\n"
)

EMAIL = "max.mustermann@example.org"


def target(**kw: Any) -> TargetTitle:
    return TargetTitle(**{"title": "Backend Developer", "lang": "en", **kw})


def role(**kw: Any) -> Role:
    return Role(
        **{
            "title": "Senior Backend Developer",
            "employer": "Beispiel GmbH",
            "start": "2019-03-01",
            "end": "2024-03-01",
            "seniority": "senior",
            "domain": "E-Commerce",
            "highlights": ["Built the billing service"],
            **kw,
        }
    )


def skill(**kw: Any) -> Skill:
    return Skill(
        **{
            "name": "Python",
            "category": "programming_language",
            "level": "advanced",
            "years": 5.0,
            "evidence": "5 Jahre Python in der Backend-Entwicklung",
            **kw,
        }
    )


def language(**kw: Any) -> Language:
    return Language(
        **{"code": "de", "code_other": None, "cefr": "native", "evidence": "Deutsch (Muttersprache)", **kw}
    )


def education(**kw: Any) -> Education:
    return Education(
        **{"level": "master", "field": "Informatik", "institution": "TU Beispielstadt", "year": 2018, **kw}
    )


def certification(**kw: Any) -> Certification:
    return Certification(**{"name": "Cloud Practitioner", "issuer": "Beispiel Academy", "year": 2022, **kw})


def profile_out(*, highlights: list[str] | None = None, **fields: Any) -> ProfileOut:
    """A clean synthetic profile (no issue at `TODAY` against `REVIEWED`); `fields` replaces top-level fields,
    `highlights` the highlights of the one role."""
    base: dict[str, Any] = {
        "summary": "Backend engineer for payment systems.",
        "target_titles": [target(), target(title="Backend-Entwickler", lang="de")],
        "seniority": "senior",
        "total_years_experience": 5.0,
        "roles": [role() if highlights is None else role(highlights=highlights)],
        "skills": [skill()],
        "languages": [language()],
        "education": [education()],
        "certifications": [certification()],
        "domains": ["E-Commerce"],
    }
    return ProfileOut(**{**base, **fields})


def run(
    out: ProfileOut, *, text: str | None = REVIEWED, today: date = TODAY
) -> tuple[ProfileOut, list[Issue]]:
    return validate_profile(out, transmitted_text=text, today=today)


def pairs(issues: list[Issue]) -> list[tuple[str, str]]:
    return [(i.path, i.code) for i in issues]


def with_code(issues: list[Issue], code: str) -> list[Issue]:
    return [i for i in issues if i.code == code]


# --- the clean case, the pin and what an issue may hold ---------------------------------------------------


def test_a_clean_profile_has_no_issue_and_comes_back_equal() -> None:
    out = profile_out()
    fixed, issues = run(out)
    assert issues == []
    assert fixed == out


def test_contact_value_in_profile_json_is_removed_with_an_issue() -> None:
    out = profile_out(
        summary="Backend engineer, reachable at max.mustermann@example.org",
        highlights=["Led the team (Tel. +49 30 1234 5678)", "Built the billing service"],
    )
    fixed, issues = validate_profile(out, transmitted_text=REVIEWED, today=date(2026, 10, 20))
    assert "example.org" not in fixed.summary and fixed.roles[0].highlights == ["Built the billing service"]
    assert {i.code for i in issues} >= {"contact_removed"} and all(
        "example.org" not in i.message for i in issues
    )


def test_the_input_profile_is_never_changed() -> None:
    out = profile_out(summary=f"Reach me at {EMAIL}", highlights=["Tel. +49 30 1234 5678", "kept"])
    before = out.model_dump()
    run(out)
    assert out.model_dump() == before


# --- contact removal --------------------------------------------------------------------------------------

type Maker = Callable[[str], ProfileOut]
type Getter = Callable[[ProfileOut], str | None]

STRING_FIELDS: list[tuple[str, Maker, Getter]] = [
    ("summary", lambda v: profile_out(summary=v), lambda p: p.summary),
    (
        "target_titles[0].title",
        lambda v: profile_out(target_titles=[target(title=v)]),
        lambda p: p.target_titles[0].title,
    ),
    ("roles[0].title", lambda v: profile_out(roles=[role(title=v)]), lambda p: p.roles[0].title),
    ("roles[0].employer", lambda v: profile_out(roles=[role(employer=v)]), lambda p: p.roles[0].employer),
    ("roles[0].domain", lambda v: profile_out(roles=[role(domain=v)]), lambda p: p.roles[0].domain),
    ("skills[0].name", lambda v: profile_out(skills=[skill(name=v)]), lambda p: p.skills[0].name),
    ("skills[0].evidence", lambda v: profile_out(skills=[skill(evidence=v)]), lambda p: p.skills[0].evidence),
    (
        "languages[0].code_other",
        lambda v: profile_out(languages=[language(code="other", code_other=v)]),
        lambda p: p.languages[0].code_other,
    ),
    (
        "languages[0].evidence",
        lambda v: profile_out(languages=[language(evidence=v)]),
        lambda p: p.languages[0].evidence,
    ),
    (
        "education[0].field",
        lambda v: profile_out(education=[education(field=v)]),
        lambda p: p.education[0].field,
    ),
    (
        "education[0].institution",
        lambda v: profile_out(education=[education(institution=v)]),
        lambda p: p.education[0].institution,
    ),
    (
        "certifications[0].name",
        lambda v: profile_out(certifications=[certification(name=v)]),
        lambda p: p.certifications[0].name,
    ),
    (
        "certifications[0].issuer",
        lambda v: profile_out(certifications=[certification(issuer=v)]),
        lambda p: p.certifications[0].issuer,
    ),
]


@pytest.mark.parametrize(("path", "make", "get"), STRING_FIELDS, ids=[f[0] for f in STRING_FIELDS])
def test_a_contact_in_any_string_field_blanks_that_string_with_an_issue_at_its_path(
    path: str, make: Maker, get: Getter
) -> None:
    fixed, issues = run(make(f"Contact {EMAIL} for details"))
    assert get(fixed) == ""
    removed = with_code(issues, "contact_removed")
    assert [i.path for i in removed] == [path]
    assert all("example.org" not in i.message and "mustermann" not in i.message for i in issues)


def test_a_contact_in_a_list_of_strings_drops_the_item_and_names_its_input_position() -> None:
    out = profile_out(
        highlights=["Built the billing service", f"Contact {EMAIL}", "Led the migration"],
        domains=["Logistik", "www.example.org/cv", "Payments"],
    )
    fixed, issues = run(out)
    assert fixed.roles[0].highlights == ["Built the billing service", "Led the migration"]
    assert fixed.domains == ["Logistik", "Payments"]
    assert pairs(with_code(issues, "contact_removed")) == [
        ("roles[0].highlights[1]", "contact_removed"),
        ("domains[1]", "contact_removed"),
    ]


@pytest.mark.parametrize(
    ("value", "category"),
    [
        (f"Reach me at {EMAIL}", "email"),
        ("Tel. +49 30 1234 5678", "phone"),
        ("Portfolio: https://example.org/max", "url"),
        ("Wohnhaft Musterstraße 12", "address"),
        ("Geburtsdatum: 01.02.1990", "personal"),
    ],
)
def test_every_contact_category_is_removed_and_named_without_its_value(value: str, category: str) -> None:
    fixed, issues = run(profile_out(summary=value))
    assert fixed.summary == ""
    (issue,) = with_code(issues, "contact_removed")
    assert issue.path == "summary"
    assert category in issue.message
    assert not any(part in issue.message for part in value.split() if len(part) > 4 and part != category)


def test_a_value_without_a_contact_is_never_touched() -> None:
    out = profile_out(
        summary="Led a team of 5 engineers; shipped v3.12.4",
        highlights=["01.2019 - 03.2021 Platform migration", "Reduced the cost by 30 %"],
    )
    fixed, issues = run(out)
    assert fixed == out and issues == []


def test_no_message_quotes_a_value_of_the_profile() -> None:
    out = profile_out(
        summary=f"Writes to {EMAIL}",
        highlights=["Call +49 30 1234 5678", "xyzzy-highlight-that-is-too-long-" + "x" * 300],
        skills=[skill(name="Quuxsoft Tool", evidence="not in the text at all")],
        roles=[role(start="Frühjahr 2019", end="2999-01-01", title="Zorgwart")],
        languages=[language(code="other", code_other=None, evidence="")],
    )
    _, issues = run(out)
    assert issues
    for issue in issues:
        for value in (
            "example.org",
            "1234",
            "xyzzy",
            "Quuxsoft",
            "Frühjahr",
            "2999",
            "Zorgwart",
            "not in the",
        ):
            assert value not in issue.message, (issue.path, issue.code)


# --- dates ------------------------------------------------------------------------------------------------


def role_dates(start: str | None, end: str | None, *, today: date = TODAY) -> tuple[Role, list[Issue]]:
    out = profile_out(roles=[role(start=start, end=end)], total_years_experience=None)
    fixed, issues = run(out, today=today)
    return fixed.roles[0], issues


@pytest.mark.parametrize(
    ("start", "end", "stored_start", "stored_end"),
    [
        ("2019-03", "2020-05", "2019-03-01", "2020-05-01"),
        ("2019-03-15", "2020-05-31", "2019-03-15", "2020-05-31"),
        ("2019-03-01", "present", "2019-03-01", "present"),
        ("2019-03", "Present", "2019-03-01", "present"),
        (" 2019-03 ", "2020-05", "2019-03-01", "2020-05-01"),
        ("2020-02-29", "2020-02-29", "2020-02-29", "2020-02-29"),
        (None, None, None, None),
        (None, "present", None, "present"),
        ("2019-03", None, "2019-03-01", None),
    ],
)
def test_month_dates_become_the_first_of_the_month_and_valid_dates_stay(
    start: str | None, end: str | None, stored_start: str | None, stored_end: str | None
) -> None:
    fixed, issues = role_dates(start, end)
    assert (fixed.start, fixed.end) == (stored_start, stored_end)
    assert issues == []


@pytest.mark.parametrize(
    "bad",
    [
        "2019",
        "2019-13",
        "2019-00",
        "2019-02-30",
        "2019-1-1",
        "2019-03-1",
        "March 2019",
        "03/2019",
        "heute",
        "",
        "   ",
        "0000-01-01",
        "2019-03-01T00:00:00",
        "\uff12\uff10\uff11\uff19-03-01",
        "2019-03-01 2020",
    ],
)
@pytest.mark.parametrize("field", ["start", "end"])
def test_an_unparsable_date_becomes_null_with_date_invalid_and_is_not_quoted(bad: str, field: str) -> None:
    fixed, issues = role_dates(bad, "2020-05-01") if field == "start" else role_dates("2019-03-01", bad)
    assert getattr(fixed, field) is None
    assert pairs(issues) == [(f"roles[0].{field}", "date_invalid")]
    assert bad.strip() == "" or bad not in issues[0].message


def test_start_after_end_is_date_order_and_both_dates_are_kept() -> None:
    fixed, issues = role_dates("2021-01-01", "2020-01-01")
    assert (fixed.start, fixed.end) == ("2021-01-01", "2020-01-01")
    assert pairs(issues) == [("roles[0].end", "date_order")]
    assert issues[0].proposal is None


def test_start_equal_to_end_is_in_order() -> None:
    assert role_dates("2020-01", "2020-01")[1] == []


def test_a_year_only_end_the_schema_turned_into_december_is_after_today_and_proposes_the_current_month() -> (
    None
):
    # A CV that says "2019 - 2026" gave 2019-01-01 and 2026-12-01 (prompt rule 4); today is 2026-10-20.
    fixed, issues = role_dates("2019-01-01", "2026-12-01")
    assert fixed.end == "2026-12-01"
    assert pairs(issues) == [("roles[0].end", "date_order")]
    assert issues[0].proposal == "2026-10-01"


def test_a_future_month_end_is_the_first_of_that_month_and_proposes_the_current_month() -> None:
    fixed, issues = role_dates("2019-01", "2026-11")
    assert fixed.end == "2026-11-01"
    assert pairs(issues) == [("roles[0].end", "date_order")]
    assert issues[0].proposal == "2026-10-01"


def test_the_current_month_is_not_after_today_once_it_is_the_first_of_the_month() -> None:
    fixed, issues = role_dates("2019-01", "2026-10")
    assert fixed.end == "2026-10-01" and issues == []


def test_an_end_date_is_after_today_only_from_the_next_day() -> None:
    assert role_dates("2019-01-01", "2026-10-20")[1] == []
    assert pairs(role_dates("2019-01-01", "2026-10-21")[1]) == [("roles[0].end", "date_order")]


def test_the_proposal_is_the_first_of_the_month_of_the_given_today() -> None:
    _, issues = role_dates("2019-01-01", "2027-06-01", today=date(2027, 3, 31))
    assert issues[0].proposal == "2027-03-01"


def test_a_start_after_today_is_date_order_without_a_proposal() -> None:
    fixed, issues = role_dates("2027-01", "present")
    assert fixed.start == "2027-01-01"
    assert pairs(issues) == [("roles[0].start", "date_order")]
    assert issues[0].proposal is None


def test_a_role_that_starts_and_ends_in_the_future_reports_both_dates() -> None:
    _, issues = role_dates("2027-01-01", "2027-06-01")
    assert pairs(issues) == [("roles[0].start", "date_order"), ("roles[0].end", "date_order")]
    assert issues[0].proposal is None and issues[1].proposal == "2026-10-01"


def test_dates_are_repaired_per_role_and_the_path_names_the_role() -> None:
    out = profile_out(
        roles=[role(), role(start="oops", end="2020-05"), role(start="2021-01-01", end="2020-01-01")],
        total_years_experience=None,
    )
    fixed, issues = run(out)
    assert [r.start for r in fixed.roles] == ["2019-03-01", None, "2021-01-01"]
    assert fixed.roles[1].end == "2020-05-01"
    assert pairs(issues) == [("roles[1].start", "date_invalid"), ("roles[2].end", "date_order")]


# --- total_years_experience -------------------------------------------------------------------------------


def years_issue(roles: list[Role], extracted: float | None, *, today: date = TODAY) -> list[Issue]:
    out = profile_out(roles=roles, total_years_experience=extracted)
    fixed, issues = run(out, today=today)
    assert (
        fixed.total_years_experience == extracted
    )  # the extracted value is kept; the proposal is not applied
    return with_code(issues, "years_mismatch")


def recomputed(roles: list[Role], *, today: date = TODAY) -> object:
    """The recomputed total, read from the proposal of the mismatch that an extracted 59 always gives."""
    (issue,) = years_issue(roles, 59.0, today=today)
    assert issue.path == "total_years_experience"
    return issue.proposal


def test_overlapping_roles_are_merged_and_present_ends_today() -> None:
    roles = [role(start="2018-01", end="2021-12"), role(start="2020-01", end="present")]
    # 2018-01-01 to 2026-10-20 is 3,214 days, 8.80 years of 365.25 days.
    assert recomputed(roles) == 8.8
    assert years_issue(roles, 5.0)[0].proposal == 8.8
    assert years_issue(roles, 8.8) == []
    assert years_issue(roles, 9.7) == []  # 0.9 apart


def test_a_year_apart_is_no_issue_and_more_than_a_year_is() -> None:
    roles = [role(start="2020-01-01", end="2024-01-01")]  # 1,461 days: exactly 4.0 years
    assert recomputed(roles) == 4.0
    assert years_issue(roles, 5.0) == []
    assert years_issue(roles, 3.0) == []
    assert [i.proposal for i in years_issue(roles, 5.01)] == [4.0]
    assert [i.proposal for i in years_issue(roles, 2.99)] == [4.0]


def test_present_is_the_given_today() -> None:
    roles = [role(start="2020-01-01", end="present")]
    assert recomputed(roles, today=date(2024, 1, 1)) == 4.0
    assert recomputed(roles, today=date(2028, 1, 1)) == 8.0


def test_disjoint_nested_and_adjacent_roles() -> None:
    assert (
        recomputed([role(start="2010-01-01", end="2012-01-01"), role(start="2015-01-01", end="2016-01-01")])
        == 3.0
    )
    nested = [role(start="2010-01-01", end="2014-01-01"), role(start="2011-01-01", end="2012-01-01")]
    assert recomputed(nested) == 4.0
    adjacent = [role(start="2010-01-01", end="2012-01-01"), role(start="2012-01-01", end="2014-01-01")]
    assert recomputed(adjacent) == 4.0
    assert recomputed(list(reversed(adjacent))) == 4.0


def test_roles_without_both_dates_add_nothing() -> None:
    roles = [
        role(start="2010-01-01", end="2014-01-01"),
        role(start=None, end="2020-01-01"),
        role(start="2020-01-01", end=None),
        role(start="not a date", end="2021-01-01"),
    ]
    assert recomputed(roles) == 4.0


def test_a_future_end_counts_to_today_and_a_future_start_counts_nothing() -> None:
    assert (
        recomputed([role(start="2026-01-01", end="2026-12-01")]) == 0.8
    )  # 2026-01-01 to 2026-10-20: 292 days
    assert (
        recomputed([role(start="2010-01-01", end="2014-01-01"), role(start="2027-01-01", end="present")])
        == 4.0
    )


def test_a_start_after_the_end_counts_nothing() -> None:
    roles = [role(start="2010-01-01", end="2014-01-01"), role(start="2021-01-01", end="2020-01-01")]
    assert recomputed(roles) == 4.0


def test_nothing_to_recompute_is_no_mismatch() -> None:
    assert years_issue([], 12.0) == []
    assert years_issue([role(start=None, end=None)], 12.0) == []
    assert years_issue([role()], None) == []


# --- grounding --------------------------------------------------------------------------------------------


def evidence_issues(
    *, skills: list[Skill] | None = None, languages: list[Language] | None = None, text: str | None = REVIEWED
) -> list[Issue]:
    out = profile_out(skills=skills or [], languages=languages or [])
    return [
        i
        for i in run(out, text=text)[1]
        if i.code in ("ungrounded", "unverifiable", "language_without_evidence")
    ]


@pytest.mark.parametrize(
    "evidence",
    [
        "5 Jahre Python in der Backend-Entwicklung",
        "GRÖSSE DER DATENBANK",
        "größe der datenbank: 2 tb",
        "STRASSE DER PIONIERE",
        "Verantwortung für die Abrechnung der Kunden",  # the text breaks the line after "die"
        "verantwortung   für\tdie\n\nabrechnung",
        "Englisch - C1",  # the text has an en dash
        "Built the billing service",  # a part of a line
        "Master Informatik,\u00a0TU Beispielstadt",  # a no-break space
    ],
)
def test_evidence_found_in_the_text_after_fold_is_grounded(evidence: str) -> None:
    assert evidence_issues(skills=[skill(evidence=evidence)]) == []


def test_fold_decides_the_umlaut_and_eszett_case_in_both_directions() -> None:
    text = "Die GRÖSSE der Straße"
    assert evidence_issues(skills=[skill(evidence="Größe der STRASSE")], text=text) == []
    assert evidence_issues(skills=[skill(evidence="Groesse")], text=text) != []  # ö is o, not oe


def test_evidence_not_in_the_text_is_ungrounded_at_its_path() -> None:
    issues = evidence_issues(skills=[skill(), skill(name="Rust", evidence="10 Jahre Rust")])
    assert pairs(issues) == [("skills[1].evidence", "ungrounded")]
    assert "Rust" not in issues[0].message


def test_language_evidence_is_grounded_too() -> None:
    issues = evidence_issues(
        languages=[language(), language(code="fr", cefr="B1", evidence="Französisch C2")]
    )
    assert pairs(issues) == [("languages[1].evidence", "ungrounded")]


def test_a_placeholder_in_the_evidence_is_matched_like_any_text() -> None:
    assert evidence_issues(skills=[skill(evidence="[EMAIL] | [PHONE]")]) == []


def test_evidence_is_not_grounded_in_an_empty_text() -> None:
    assert pairs(evidence_issues(skills=[skill()], text="")) == [("skills[0].evidence", "ungrounded")]


def test_a_native_pdf_has_no_text_so_every_evidence_is_unverifiable() -> None:
    issues = evidence_issues(
        skills=[skill(), skill(name="Rust", evidence="10 Jahre Rust")],
        languages=[language(), language(code="en", cefr="C1", evidence="whatever")],
        text=None,
    )
    assert pairs(issues) == [
        ("skills[0].evidence", "unverifiable"),
        ("skills[1].evidence", "unverifiable"),
        ("languages[0].evidence", "unverifiable"),
        ("languages[1].evidence", "unverifiable"),
    ]


def test_blank_skill_evidence_is_not_a_grounding_issue_but_blank_language_evidence_is_an_issue() -> None:
    assert evidence_issues(skills=[skill(evidence=""), skill(evidence="  \n ")]) == []
    assert evidence_issues(skills=[skill(evidence="")], text=None) == []
    issues = evidence_issues(languages=[language(evidence=""), language(code="en", evidence="  \n")])
    assert pairs(issues) == [
        ("languages[0].evidence", "language_without_evidence"),
        ("languages[1].evidence", "language_without_evidence"),
    ]


def test_a_language_without_evidence_is_reported_for_a_native_pdf_too() -> None:
    issues = evidence_issues(languages=[language(evidence="")], text=None)
    assert pairs(issues) == [("languages[0].evidence", "language_without_evidence")]


def test_a_language_whose_evidence_was_a_contact_has_lost_it() -> None:
    _, issues = run(profile_out(languages=[language(evidence=f"Deutsch, {EMAIL}")]))
    assert pairs(issues) == [
        ("languages[0].evidence", "contact_removed"),
        ("languages[0].evidence", "language_without_evidence"),
    ]


# --- code_other -------------------------------------------------------------------------------------------


def code_other_issues(*languages: Language) -> list[Issue]:
    out = profile_out(languages=list(languages))
    return with_code(run(out)[1], "code_other")


def test_code_other_is_required_when_the_code_is_other() -> None:
    assert code_other_issues(language(code="other", code_other="Polabisch")) == []
    assert code_other_issues(language(code="other", code_other=None)) != []
    assert code_other_issues(language(code="other", code_other="")) != []
    assert code_other_issues(language(code="other", code_other="   ")) != []
    assert pairs(code_other_issues(language(), language(code="other", code_other=None))) == [
        ("languages[1].code_other", "code_other")
    ]


@pytest.mark.parametrize("code", ["de", "en", "fr", "es", "it", "nl", "pl", "pt"])
def test_code_other_must_be_null_for_every_other_code(code: str) -> None:
    assert code_other_issues(language(code=code, code_other=None)) == []
    assert pairs(code_other_issues(language(code=code, code_other="Deutsch"))) == [
        ("languages[0].code_other", "code_other")
    ]


def test_code_other_is_reported_and_never_repaired() -> None:
    out = profile_out(languages=[language(code="de", code_other="Deutsch")])
    fixed, _ = run(out)
    assert fixed.languages[0].code_other == "Deutsch"


# --- limits: every edge and one past it -------------------------------------------------------------------


def limit_paths(out: ProfileOut) -> list[str]:
    return [i.path for i in with_code(run(out)[1], "limit")]


def blank_skills(n: int) -> list[Skill]:
    return [skill(name=f"Skill {i}", evidence="") for i in range(n)]


def test_the_limits_table_is_the_spec_table_verbatim() -> None:
    assert (schema.SUMMARY_MAX, schema.TARGET_TITLES) == (1200, (1, 12))
    assert (schema.ROLES_MAX, schema.HIGHLIGHTS_MAX, schema.HIGHLIGHT_CHARS) == (30, 8, 300)
    assert (schema.SKILLS_MAX, schema.SKILL_YEARS) == (120, (0, 50))
    assert (schema.LANGUAGES_MAX, schema.CODE_OTHER_CHARS) == (10, 40)
    assert (schema.EDUCATION_MAX, schema.EDUCATION_YEAR) == (10, (1950, None))
    assert (schema.CERTIFICATIONS_MAX, schema.DOMAINS_MAX, schema.TOTAL_YEARS) == (30, 20, (0, 60))
    assert dict(schema.LIMITS) == {
        "SUMMARY_MAX": 1200,
        "TARGET_TITLES": (1, 12),
        "ROLES_MAX": 30,
        "HIGHLIGHTS_MAX": 8,
        "HIGHLIGHT_CHARS": 300,
        "SKILLS_MAX": 120,
        "SKILL_YEARS": (0, 50),
        "LANGUAGES_MAX": 10,
        "CODE_OTHER_CHARS": 40,
        "EDUCATION_MAX": 10,
        "EDUCATION_YEAR": (1950, None),
        "CERTIFICATIONS_MAX": 30,
        "DOMAINS_MAX": 20,
        "TOTAL_YEARS": (0, 60),
    }


@pytest.mark.parametrize(("size", "over"), [(1200, False), (1201, True)])
def test_summary_limit(size: int, over: bool) -> None:
    assert limit_paths(profile_out(summary="x" * size)) == (["summary"] if over else [])


@pytest.mark.parametrize(("count", "over"), [(0, True), (1, False), (12, False), (13, True)])
def test_target_titles_limit(count: int, over: bool) -> None:
    out = profile_out(target_titles=[target(title=f"Title {i}") for i in range(count)])
    assert limit_paths(out) == (["target_titles"] if over else [])


@pytest.mark.parametrize(("count", "over"), [(30, False), (31, True)])
def test_roles_limit(count: int, over: bool) -> None:
    out = profile_out(roles=[role() for _ in range(count)])
    assert limit_paths(out) == (["roles"] if over else [])


@pytest.mark.parametrize(("count", "over"), [(8, False), (9, True)])
def test_highlights_count_limit(count: int, over: bool) -> None:
    out = profile_out(highlights=[f"Highlight {i}" for i in range(count)])
    assert limit_paths(out) == (["roles[0].highlights"] if over else [])


@pytest.mark.parametrize(("size", "over"), [(300, False), (301, True)])
def test_highlight_length_limit_names_the_highlight(size: int, over: bool) -> None:
    out = profile_out(highlights=["short", "x" * size, "short"])
    assert limit_paths(out) == (["roles[0].highlights[1]"] if over else [])


def test_the_highlights_of_every_role_are_limited() -> None:
    out = profile_out(roles=[role(), role(highlights=["x" * 301] + ["h"] * 8)])
    assert limit_paths(out) == ["roles[1].highlights", "roles[1].highlights[0]"]


@pytest.mark.parametrize(("count", "over"), [(120, False), (121, True)])
def test_skills_limit(count: int, over: bool) -> None:
    assert limit_paths(profile_out(skills=blank_skills(count))) == (["skills"] if over else [])


@pytest.mark.parametrize(
    ("years", "over"),
    [
        (0, False),
        (0.0, False),
        (50, False),
        (50.0, False),
        (None, False),
        (-0.01, True),
        (50.01, True),
        (1e9, True),
    ],
)
def test_skill_years_limit(years: float | None, over: bool) -> None:
    out = profile_out(skills=[skill(name="A", years=3.0), skill(years=years)])
    assert limit_paths(out) == (["skills[1].years"] if over else [])


def test_skill_years_nan_and_infinity_are_out_of_range() -> None:
    for bad in (float("nan"), float("inf"), float("-inf")):
        assert limit_paths(profile_out(skills=[skill(years=bad)])) == ["skills[0].years"]


@pytest.mark.parametrize(("count", "over"), [(10, False), (11, True)])
def test_languages_limit(count: int, over: bool) -> None:
    out = profile_out(languages=[language() for _ in range(count)])
    assert limit_paths(out) == (["languages"] if over else [])


@pytest.mark.parametrize(("size", "over"), [(40, False), (41, True)])
def test_code_other_length_limit(size: int, over: bool) -> None:
    out = profile_out(languages=[language(code="other", code_other="x" * size)])
    assert limit_paths(out) == (["languages[0].code_other"] if over else [])


@pytest.mark.parametrize(("count", "over"), [(10, False), (11, True)])
def test_education_limit(count: int, over: bool) -> None:
    assert limit_paths(profile_out(education=[education() for _ in range(count)])) == (
        ["education"] if over else []
    )


@pytest.mark.parametrize(
    ("year", "over"), [(1950, False), (1949, True), (2026, False), (2027, True), (None, False)]
)
def test_education_year_limit_is_1950_to_the_current_year(year: int | None, over: bool) -> None:
    out = profile_out(education=[education(), education(year=year)])
    assert limit_paths(out) == (["education[1].year"] if over else [])


def test_the_education_year_ceiling_follows_the_given_today() -> None:
    out = profile_out(education=[education(year=2027)])
    assert [i.path for i in with_code(run(out, today=date(2027, 1, 1))[1], "limit")] == []
    assert [i.path for i in with_code(run(out, today=date(2026, 12, 31))[1], "limit")] == [
        "education[0].year"
    ]


@pytest.mark.parametrize(("count", "over"), [(30, False), (31, True)])
def test_certifications_limit(count: int, over: bool) -> None:
    out = profile_out(certifications=[certification() for _ in range(count)])
    assert limit_paths(out) == (["certifications"] if over else [])


@pytest.mark.parametrize(("count", "over"), [(20, False), (21, True)])
def test_domains_limit(count: int, over: bool) -> None:
    assert limit_paths(profile_out(domains=[f"Domain {i}" for i in range(count)])) == (
        ["domains"] if over else []
    )


@pytest.mark.parametrize(
    ("years", "over"),
    [(0.0, False), (60.0, False), (5.0, False), (-0.1, True), (60.1, True), (float("nan"), True)],
)
def test_total_years_limit(years: float, over: bool) -> None:
    assert limit_paths(profile_out(total_years_experience=years)) == (
        ["total_years_experience"] if over else []
    )


def test_the_excess_is_kept_never_truncated() -> None:
    out = profile_out(
        summary="x" * 1500,
        highlights=["y" * 400] + ["h"] * 9,
        skills=blank_skills(125),
        domains=[f"Domain {i}" for i in range(25)],
        roles=[role(highlights=["y" * 400] + ["h"] * 9)] * 31,
    )
    fixed, issues = run(out)
    assert fixed.summary == "x" * 1500
    assert len(fixed.skills) == 125 and len(fixed.domains) == 25 and len(fixed.roles) == 31
    assert fixed.roles[0].highlights == ["y" * 400] + ["h"] * 9
    assert {"summary", "skills", "domains", "roles"} <= {i.path for i in with_code(issues, "limit")}


def test_limit_messages_state_the_limit_and_no_value() -> None:
    _, issues = run(profile_out(summary="secret-text-" + "x" * 1300))
    (issue,) = with_code(issues, "limit")
    assert "1200" in issue.message or "1,200" in issue.message
    assert "secret-text" not in issue.message


# --- never raises: empty and missing values ---------------------------------------------------------------


def test_an_empty_profile_is_validated_without_raising() -> None:
    out = ProfileOut(
        summary="",
        target_titles=[],
        seniority="junior",
        total_years_experience=None,
        roles=[],
        skills=[],
        languages=[],
        education=[],
        certifications=[],
        domains=[],
    )
    for text in (REVIEWED, "", None):
        fixed, issues = run(out, text=text)
        assert fixed == out
        assert pairs(issues) == [("target_titles", "limit")]


def test_empty_strings_and_missing_optional_fields_are_validated_without_raising() -> None:
    out = profile_out(
        summary="",
        target_titles=[target(title="")],
        roles=[
            Role(
                title="",
                employer=None,
                start=None,
                end=None,
                seniority="unspecified",
                domain=None,
                highlights=[""],
            )
        ],
        skills=[Skill(name="", category="other", level="basic", years=None, evidence="")],
        languages=[Language(code="other", code_other=None, cefr="unspecified", evidence="")],
        education=[Education(level="other", field="", institution=None, year=None)],
        certifications=[Certification(name="", issuer=None, year=None)],
        domains=[""],
        total_years_experience=None,
    )
    fixed, issues = run(out)
    assert fixed == out
    assert sorted(pairs(issues)) == [
        ("languages[0].code_other", "code_other"),
        ("languages[0].evidence", "language_without_evidence"),
    ]


def test_a_confirmed_profile_keeps_its_type_and_its_user_entered_block() -> None:
    clearance = SecurityClearance(name="Ü2", level=None)
    out = ConfirmedProfile(
        **profile_out(summary=f"Mail {EMAIL}").model_dump(),
        user_entered=UserEntered(work_authorization_de="authorized", security_clearances=[clearance]),
    )
    fixed, issues = run(out)
    assert isinstance(fixed, ConfirmedProfile)
    assert fixed.summary == ""
    assert fixed.user_entered == out.user_entered
    assert pairs(issues) == [("summary", "contact_removed")]


def test_validation_twice_gives_the_same_profile_and_no_repair_issue() -> None:
    out = profile_out(
        summary=f"Mail {EMAIL}",
        highlights=["Tel. +49 30 1234 5678", "kept"],
        roles=[role(start="2019-03", end="oops")],
    )
    once, _ = run(out)
    twice, issues = run(once)
    assert twice == once
    assert not {"contact_removed", "date_invalid"} & {i.code for i in issues}


def test_the_issue_is_a_frozen_value_with_an_empty_default_proposal() -> None:
    issue = Issue(path="summary", code="limit", message="m")
    assert issue.proposal is None
    with pytest.raises(AttributeError):
        issue.path = "x"  # type: ignore[misc]


PATH = re.compile(r"[a-z_]+(\[\d+\])?(\.[a-z_]+(\[\d+\])?)*")


def test_paths_are_dotted_field_names_with_zero_based_indexes() -> None:
    out = profile_out(
        summary="x" * 1300,
        target_titles=[],
        roles=[role(start="bad", highlights=["a"] * 9)],
        skills=[skill(years=99.0, evidence="nowhere")],
        languages=[language(code="other", evidence="")],
        education=[education(year=1900)],
        total_years_experience=99.0,
    )
    _, issues = run(out)
    assert issues
    assert all(PATH.fullmatch(i.path) for i in issues), pairs(issues)
    assert {i.path for i in issues} >= {
        "summary",
        "target_titles",
        "roles[0].start",
        "roles[0].highlights",
        "skills[0].years",
        "skills[0].evidence",
        "languages[0].evidence",
        "languages[0].code_other",
        "education[0].year",
        "total_years_experience",
    }


# --- validate_user_entered --------------------------------------------------------------------------------


def user_entered_issues(**fields: Any) -> list[Issue]:
    return validate_user_entered(UserEntered(**fields))


def test_the_default_user_entered_block_has_no_issue() -> None:
    assert validate_user_entered(UserEntered()) == []
    assert user_entered_issues(work_authorization_de="requires_sponsorship") == []


def test_clearance_names_and_levels_are_limited_to_60_characters() -> None:
    ok = [SecurityClearance(name="n" * 60, level="l" * 60), SecurityClearance(name="Ü1", level=None)]
    assert user_entered_issues(security_clearances=ok) == []
    bad = [SecurityClearance(name="n" * 61, level="l" * 61)]
    assert pairs(user_entered_issues(security_clearances=bad)) == [
        ("user_entered.security_clearances[0].name", "limit"),
        ("user_entered.security_clearances[0].level", "limit"),
    ]


def test_a_clearance_needs_a_name() -> None:
    blank = [SecurityClearance(name="Ü1", level=None), SecurityClearance(name="  ", level="Ü2")]
    assert pairs(user_entered_issues(security_clearances=blank)) == [
        ("user_entered.security_clearances[1].name", "limit")
    ]


def test_at_most_ten_clearances() -> None:
    def clearances(n: int) -> list[SecurityClearance]:
        return [SecurityClearance(name=f"C{i}", level=None) for i in range(n)]

    assert user_entered_issues(security_clearances=clearances(10)) == []
    assert pairs(user_entered_issues(security_clearances=clearances(11))) == [
        ("user_entered.security_clearances", "limit")
    ]


def test_user_entered_messages_quote_no_value() -> None:
    bad = [SecurityClearance(name="secret-" + "n" * 61, level=None)]
    (issue,) = user_entered_issues(security_clearances=bad)
    assert "secret-" not in issue.message
