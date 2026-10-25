"""Profile validation (spec §12.3 grounding and re-scan, §12.4 limits, dates and years, §12.5 languages).

`validate_profile` runs over the extraction output, repairs what has a safe repair and reports every issue for
the user's review; `validate_user_entered` checks the block the review form adds; `recompute_total_years` is
the years figure that the review form shows. The module is pure: no database, no I/O, no clock (`today` is a
parameter) and no logging. The profile that `validate_profile` returns shares no object with its input.

**Repairs**, which never lose information the user could have used:
- a month date `YYYY-MM` becomes `YYYY-MM-01`; `"Present"` and a padded date are written in their normal form;
- a date that is none of those becomes `null` (`date_invalid`); a year-only date (`"2018"`) proposes the
  prompt's own repair, `YYYY-01-01` for a start and `YYYY-12-01` for an end;
- a string field with a contact (`scan_value`) becomes `""`, a list item (a highlight, a domain) is dropped
  (`contact_removed`).

**Reports, never repairs**: an excess (a list over its limit, a text over its length, a number out of range),
a date after today or an end before its start, a total of years more than 1 apart from the roles', an evidence
quote that is not in the transmitted text (or is too short to be a quote), a language without evidence and a
wrong `code_other`. The value stays as it is, so that the user decides; `proposal` carries the value the
validator would suggest.

**Issue paths** are the field names joined by `.`, with a zero-based `[index]` after a list, from the root of
the profile, and all of them name a position in the profile that `validate_profile` returns: `summary`,
`roles[0].highlights[1]`, `languages[2].code_other`. The user-entered block's paths start with
`user_entered.`. A list item that was dropped for its contact data exists no more, so `contact_removed` names
the list (`roles[0].highlights`, `domains`), once per dropped item.

**No message holds a value.** A message is made of fixed text, the limits of `profile/schema.py` and the
names of the contact categories; the user finds the value through the path.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Final, Literal

from jobhunter.llm.schemas.profile import (
    Certification,
    Education,
    Language,
    ProfileOut,
    Role,
    Skill,
    TargetTitle,
    UserEntered,
)
from jobhunter.normalize.text import fold
from jobhunter.profile import schema
from jobhunter.profile.contacts import scan_value
from jobhunter.types import JSONValue

type IssueCode = Literal[
    "limit",
    "date_invalid",
    "date_order",
    "years_mismatch",
    "contact_removed",
    "ungrounded",
    "language_without_evidence",
    "code_other",
    "unverifiable",
]


@dataclass(frozen=True)
class Issue:
    """One thing the user should look at: where (`path`), what kind (`code`), a message made of fixed text
    and, where there is one, the value the validator suggests (`proposal`)."""

    path: str
    code: IssueCode
    message: str
    proposal: JSONValue = None


# A year is 365.25 days: four years are 1,461 days. A month date is the first of the month (§12.4).
_DAYS_PER_YEAR: Final = 365.25
_YEARS_TOLERANCE: Final = 1.0
_DATE: Final = re.compile(r"([0-9]{4})-([0-9]{2})(?:-([0-9]{2}))?")
_YEAR: Final = re.compile(r"[0-9]{4}")


@dataclass
class _Run:
    """The state of one validation: the date it measures against, the folded text evidence is looked up in
    (`None` for a native PDF) and the issues so far."""

    today: date
    haystack: str | None
    issues: list[Issue]

    def add(self, path: str, code: IssueCode, message: str, proposal: JSONValue = None) -> None:
        self.issues.append(Issue(path, code, message, proposal))

    def limit(self, path: str, message: str) -> None:
        self.add(path, "limit", message)


def _at_most(n: int, unit: str) -> str:
    return f"At most {n} {unit} allowed."


def _range(what: str, lo: int, hi: int | str, unit: str = "") -> str:
    return f"{what} must be between {lo} and {hi}{unit}."


# --- contacts (§12.3: the post-extraction re-scan) --------------------------------------------------------


def _contact_message(categories: Sequence[str]) -> str:
    return f"Contact data ({', '.join(categories)}) was found in this value and removed."


def _scrub(run: _Run, value: str, path: str) -> str:
    """`value`, or `""` with a `contact_removed` issue when it holds a contact."""
    found = scan_value(value)
    if not found:
        return value
    run.add(path, "contact_removed", _contact_message(found))
    return ""


def _scrub_optional(run: _Run, value: str | None, path: str) -> str | None:
    return None if value is None else _scrub(run, value, path)


def _scrub_items(run: _Run, items: list[str], path: str, noun: str) -> list[str]:
    """`items` without those that hold a contact; each dropped item is reported at the list's `path`."""
    kept: list[str] = []
    for item in items:
        found = scan_value(item)
        if found:
            message = f"A {noun} held contact data ({', '.join(found)}) and was removed."
            run.add(path, "contact_removed", message)
        else:
            kept.append(item)
    return kept


# --- dates (§12.4) ----------------------------------------------------------------------------------------


def _parse_date(value: str) -> date | None:
    """`YYYY-MM-DD`, or `YYYY-MM` as the first of the month; `None` for anything else."""
    match = _DATE.fullmatch(value.strip())
    if match is None:
        return None
    try:
        return date(int(match[1]), int(match[2]), int(match[3] or 1))
    except ValueError:
        return None


def _parse_end(value: str | None, today: date) -> tuple[date | None, bool]:
    """An end as `(date, present)`: `present` is `today`; `null` and a non-date are `(None, False)`."""
    if value is None:
        return None, False
    if value.strip().casefold() == "present":
        return today, True
    return _parse_date(value), False


def _current_month(today: date, start: date | None) -> str:
    """The end date that the validator proposes for one after today: the 1st of the current month, or the
    start when that is later in the month (a day-precise start must not come after its end)."""
    first = today.replace(day=1)
    return start.isoformat() if start is not None and first < start <= today else first.isoformat()


def _year_only_proposal(value: str, *, end: bool, today: date, start: date | None) -> str | None:
    """The prompt's repair of a year-only date (rule 4: January for a start, December for an end), or `None`
    for any other value. An end after today is proposed as the current month instead."""
    stripped = value.strip()
    if not _YEAR.fullmatch(stripped) or int(stripped) < 1:
        return None
    year = int(stripped)
    if not end:
        return date(year, 1, 1).isoformat()
    december = date(year, 12, 1)
    return _current_month(today, start) if december > today else december.isoformat()


@dataclass(frozen=True)
class _Span:
    """What a role's two dates come to after the repair: the stored strings, and the dates they stand for."""

    start_text: str | None
    end_text: str | None
    start: date | None
    end: date | None  # the end date, or `today` for `present`
    present: bool


def _repair_dates(run: _Run, path: str, role: Role) -> _Span:
    start_text: str | None = None
    start: date | None = None
    if role.start is not None:
        start = _parse_date(role.start)
        if start is None:
            proposal = _year_only_proposal(role.start, end=False, today=run.today, start=None)
            run.add(f"{path}.start", "date_invalid", "Not a date (YYYY-MM-DD or YYYY-MM); removed.", proposal)
        else:
            start_text = start.isoformat()
    end_text: str | None = None
    end, present = _parse_end(role.end, run.today)
    if role.end is not None:
        if present:
            end_text = "present"
        elif end is None:
            proposal = _year_only_proposal(role.end, end=True, today=run.today, start=start)
            message = "Not a date (YYYY-MM-DD, YYYY-MM or present); removed."
            run.add(f"{path}.end", "date_invalid", message, proposal)
        else:
            end_text = end.isoformat()
    return _Span(start_text, end_text, start, end, present)


def _check_order(run: _Run, path: str, span: _Span) -> None:
    if span.start is not None and span.start > run.today:
        run.add(f"{path}.start", "date_order", "The start date is after today.")
    if span.end is not None and not span.present:
        if span.end > run.today:
            proposal = _current_month(run.today, span.start)
            run.add(
                f"{path}.end",
                "date_order",
                "The end date is after today; the current month is proposed.",
                proposal,
            )
        if span.start is not None and span.start > span.end:
            run.add(f"{path}.end", "date_order", "The end date is before the start date.")


def recompute_total_years(roles: Sequence[Role], today: date) -> float | None:
    """The years of experience the roles' dates give, to 0.1: the intervals of the roles with both dates
    (`present` and an end after today count to `today`; a start after today or after the end, an open end
    and a date that does not parse count nothing), overlapping or touching intervals merged, days / 365.25.
    `None` when no role has an interval."""
    intervals: list[tuple[date, date]] = []
    for role in roles:
        start = None if role.start is None else _parse_date(role.start)
        end, _ = _parse_end(role.end, today)
        if start is not None and end is not None and start <= min(end, today):
            intervals.append((start, min(end, today)))
    if not intervals:
        return None
    merged: list[tuple[date, date]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return round(sum((end - start).days for start, end in merged) / _DAYS_PER_YEAR, 1)


# --- sections ---------------------------------------------------------------------------------------------


def _target_titles(run: _Run, titles: list[TargetTitle]) -> list[TargetTitle]:
    lo, hi = schema.TARGET_TITLES
    if not lo <= len(titles) <= hi:
        run.limit("target_titles", _range("The number of target titles", lo, hi))
    return [
        title.model_copy(update={"title": _scrub(run, title.title, f"target_titles[{i}].title")})
        for i, title in enumerate(titles)
    ]


def _role(run: _Run, index: int, role: Role) -> Role:
    path = f"roles[{index}]"
    title = _scrub(run, role.title, f"{path}.title")
    employer = _scrub_optional(run, role.employer, f"{path}.employer")
    domain = _scrub_optional(run, role.domain, f"{path}.domain")
    highlights = _scrub_items(run, role.highlights, f"{path}.highlights", "highlight")
    span = _repair_dates(run, path, role)
    _check_order(run, path, span)
    if len(highlights) > schema.HIGHLIGHTS_MAX:
        run.limit(f"{path}.highlights", _at_most(schema.HIGHLIGHTS_MAX, "highlights"))
    for i, highlight in enumerate(highlights):
        if len(highlight) > schema.HIGHLIGHT_CHARS:
            run.limit(f"{path}.highlights[{i}]", _at_most(schema.HIGHLIGHT_CHARS, "characters"))
    return role.model_copy(
        update={
            "title": title,
            "employer": employer,
            "domain": domain,
            "highlights": highlights,
            "start": span.start_text,
            "end": span.end_text,
        }
    )


def _check_evidence(run: _Run, path: str, evidence: str, *, language: bool) -> None:
    """§12.3 grounding: the folded evidence must be a part of the folded transmitted text. It must also be a
    quote: V3's floor (§11.6, at least 4 characters and 3 letters or digits) is applied here, so that a blank
    or trivial evidence cannot ground a claim, and it holds for a native PDF too. A language without any
    evidence is `language_without_evidence` (§12.5)."""
    folded = fold(evidence)
    if not folded and language:
        run.add(path, "language_without_evidence", "A language needs a quote from the CV as evidence.")
    elif len(folded) < schema.QUOTE_MIN_CHARS or sum(c.isalnum() for c in folded) < schema.QUOTE_MIN_ALNUM:
        run.add(path, "ungrounded", "The evidence is missing or too short to be a quote from your CV text.")
    elif run.haystack is None:
        run.add(path, "unverifiable", "The evidence cannot be checked: the original PDF was sent, not text.")
    elif folded not in run.haystack:
        run.add(path, "ungrounded", "The evidence was not found in your CV text.")


def _skill(run: _Run, index: int, skill: Skill) -> Skill:
    path = f"skills[{index}]"
    name = _scrub(run, skill.name, f"{path}.name")
    evidence = _scrub(run, skill.evidence, f"{path}.evidence")
    lo, hi = schema.SKILL_YEARS
    if skill.years is not None and not lo <= skill.years <= hi:
        run.limit(f"{path}.years", _range("The years", lo, hi))
    _check_evidence(run, f"{path}.evidence", evidence, language=False)
    return skill.model_copy(update={"name": name, "evidence": evidence})


def _language(run: _Run, index: int, lang: Language) -> Language:
    path = f"languages[{index}]"
    code_other = _scrub_optional(run, lang.code_other, f"{path}.code_other")
    evidence = _scrub(run, lang.evidence, f"{path}.evidence")
    if lang.code == "other":
        if code_other is None or not code_other.strip():
            run.add(
                f"{path}.code_other", "code_other", "The language's name is required when the code is other."
            )
    elif code_other is not None:
        run.add(
            f"{path}.code_other", "code_other", "The language's name must be null unless the code is other."
        )
    if code_other is not None and len(code_other) > schema.CODE_OTHER_CHARS:
        run.limit(f"{path}.code_other", _at_most(schema.CODE_OTHER_CHARS, "characters"))
    _check_evidence(run, f"{path}.evidence", evidence, language=True)
    return lang.model_copy(update={"code_other": code_other, "evidence": evidence})


def _education(run: _Run, index: int, entry: Education) -> Education:
    path = f"education[{index}]"
    field = _scrub(run, entry.field, f"{path}.field")
    institution = _scrub_optional(run, entry.institution, f"{path}.institution")
    lo, hi = schema.EDUCATION_YEAR
    ceiling = run.today.year if hi is None else hi
    if entry.year is not None and not lo <= entry.year <= ceiling:
        run.limit(f"{path}.year", _range("The year", lo, "the current year" if hi is None else hi))
    return entry.model_copy(update={"field": field, "institution": institution})


def _certification(run: _Run, index: int, cert: Certification) -> Certification:
    path = f"certifications[{index}]"
    name = _scrub(run, cert.name, f"{path}.name")
    issuer = _scrub_optional(run, cert.issuer, f"{path}.issuer")
    return cert.model_copy(update={"name": name, "issuer": issuer})


def _total_years(run: _Run, extracted: float | None, roles: list[Role]) -> None:
    lo, hi = schema.TOTAL_YEARS
    if extracted is not None and not lo <= extracted <= hi:
        run.limit("total_years_experience", _range("The total", lo, hi, " years"))
    recomputed = recompute_total_years(roles, run.today)
    if recomputed is None or extracted is None:
        return
    # Rounded to nine places, so that 8.8 against 7.8 is exactly one year apart and not 1.0000000000000009.
    if round(abs(extracted - recomputed), 9) > _YEARS_TOLERANCE:
        run.add(
            "total_years_experience",
            "years_mismatch",
            "The total differs by more than a year from the one the roles' dates give.",
            recomputed if lo <= recomputed <= hi else None,  # a figure the limit would refuse is no proposal
        )


def validate_profile[P: ProfileOut](
    out: P, *, transmitted_text: str | None, today: date
) -> tuple[P, list[Issue]]:
    """The repaired profile and its issues (module docstring). Never raises, and `out` is not changed.

    `transmitted_text` is the reviewed text the model received, or `None` for a native PDF, whose evidence is
    then `unverifiable`. A `ConfirmedProfile` comes back as one, its `user_entered` block untouched.
    """
    own = out.model_copy(deep=True)  # nothing of `out` is shared with the result
    run = _Run(today, None if transmitted_text is None else fold(transmitted_text), [])

    summary = _scrub(run, own.summary, "summary")
    if len(summary) > schema.SUMMARY_MAX:
        run.limit("summary", _at_most(schema.SUMMARY_MAX, "characters"))
    target_titles = _target_titles(run, own.target_titles)

    if len(own.roles) > schema.ROLES_MAX:
        run.limit("roles", _at_most(schema.ROLES_MAX, "roles"))
    roles = [_role(run, i, role) for i, role in enumerate(own.roles)]

    if len(own.skills) > schema.SKILLS_MAX:
        run.limit("skills", _at_most(schema.SKILLS_MAX, "skills"))
    skills = [_skill(run, i, skill) for i, skill in enumerate(own.skills)]

    if len(own.languages) > schema.LANGUAGES_MAX:
        run.limit("languages", _at_most(schema.LANGUAGES_MAX, "languages"))
    languages = [_language(run, i, lang) for i, lang in enumerate(own.languages)]

    if len(own.education) > schema.EDUCATION_MAX:
        run.limit("education", _at_most(schema.EDUCATION_MAX, "entries"))
    education = [_education(run, i, entry) for i, entry in enumerate(own.education)]

    if len(own.certifications) > schema.CERTIFICATIONS_MAX:
        run.limit("certifications", _at_most(schema.CERTIFICATIONS_MAX, "certifications"))
    certifications = [_certification(run, i, cert) for i, cert in enumerate(own.certifications)]

    domains = _scrub_items(run, own.domains, "domains", "domain")
    if len(domains) > schema.DOMAINS_MAX:
        run.limit("domains", _at_most(schema.DOMAINS_MAX, "domains"))

    _total_years(run, own.total_years_experience, roles)

    repaired_profile = own.model_copy(
        update={
            "summary": summary,
            "target_titles": target_titles,
            "roles": roles,
            "skills": skills,
            "languages": languages,
            "education": education,
            "certifications": certifications,
            "domains": domains,
        }
    )
    return repaired_profile, run.issues


def validate_user_entered(u: UserEntered) -> list[Issue]:
    """The issues of the user-entered block (§12.4): a clearance has a name, a name and a level are at most
    `CLEARANCE_CHARS` long, and there are at most `CLEARANCES_MAX` clearances. The values are the user's own,
    so nothing is repaired or scanned; the paths start with `user_entered.`."""
    issues: list[Issue] = []
    clearances = u.security_clearances
    base = "user_entered.security_clearances"
    if len(clearances) > schema.CLEARANCES_MAX:
        issues.append(Issue(base, "limit", _at_most(schema.CLEARANCES_MAX, "clearances")))
    for index, clearance in enumerate(clearances):
        path = f"{base}[{index}]"
        if not clearance.name.strip():
            issues.append(Issue(f"{path}.name", "limit", "A clearance needs a name."))
        elif len(clearance.name) > schema.CLEARANCE_CHARS:
            issues.append(Issue(f"{path}.name", "limit", _at_most(schema.CLEARANCE_CHARS, "characters")))
        if clearance.level is not None and len(clearance.level) > schema.CLEARANCE_CHARS:
            issues.append(Issue(f"{path}.level", "limit", _at_most(schema.CLEARANCE_CHARS, "characters")))
    return issues
