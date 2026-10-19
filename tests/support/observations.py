"""Canonical-selection test data (Task 17): normalised postings and observation views with sane defaults.

`normalized(**overrides)` is a `NormalizedPosting` that states little: the title `Python Developer` at
`Acme`, no location (kind `unknown`), every work mode (source `none`), no remote country, no employment
type, no salary, no posted date (`unknown`), the language-neutral `DESCRIPTION` (full, quality `ok`;
`detect_language` finds `unknown`), no structured language and nothing in `source_structured`. Its
`description_quality` is computed from its description unless given, as the parser does.

`obs(source_id, posting_id, observed_at, **overrides)` is the `ObservationView` of such a posting (the
overrides go to `normalized`): the observation id is `OBSERVATION_BASE + posting_id` unless `observation_id`
is given, the source instance is `INSTANCE[source_id]`, and `completeness` and `quality` copy the
description's, as `posting_observations` does. `stated(min, max)` and `predicted(min, max)` are EUR salaries
per year of that kind.
"""

from datetime import datetime
from typing import Any, Final

from jobhunter.db.seed import SOURCES
from jobhunter.normalize.fields import ObservationView
from jobhunter.normalize.quality import description_quality
from jobhunter.sources.base import NormalizedPosting
from jobhunter.sources.parsing.salary import Salary, SalaryKind, from_bounds

DESCRIPTION: Final = "Python backend platform engineering role " * 8
OBSERVATION_BASE: Final = 1000
# A fixed source instance id per source, for views that never reach the database.
INSTANCE: Final = {source.id: index for index, source in enumerate(SOURCES, start=101)}

_DEFAULTS: Final[dict[str, Any]] = {
    "title_raw": "Python Developer",
    "company_raw": "Acme",
    "locations": [],
    "location_kind": "unknown",
    "work_mode_possible": frozenset({"remote", "hybrid", "onsite"}),
    "work_mode_source": "none",
    "remote_countries": (),
    "employment_types": (),
    "employment_source": "none",
    "salary": None,
    "posted_at": None,
    "posted_precision": "unknown",
    "posted_raw": None,
    "item_ts": None,
    "description_text": DESCRIPTION,
    "description_html_sanitized": None,
    "description_completeness": "full",
    "ad_language_structured": None,
    "primary_link": None,
    "outbound_url_private": None,
    "publisher": None,
    "raw_keys": (),
    "requisition_id": None,
    "source_structured": {},
}


def normalized(native_id: str = "n1", **overrides: Any) -> NormalizedPosting:
    """A `NormalizedPosting` of the defaults with `overrides` applied (an unknown field is a `TypeError`)."""
    if unknown := set(overrides) - set(NormalizedPosting.model_fields):
        raise TypeError(f"not NormalizedPosting fields: {sorted(unknown)}")
    fields = {**_DEFAULTS, **overrides}
    fields.setdefault("description_quality", description_quality(fields["description_text"]))
    return NormalizedPosting(native_id=native_id, **fields)


def obs(source_id: str, posting_id: int, observed_at: datetime, **overrides: Any) -> ObservationView:
    """The view of the latest observation of posting `posting_id` of `source_id`, made at `observed_at`."""
    observation_id = overrides.pop("observation_id", OBSERVATION_BASE + posting_id)
    posting = normalized(f"{source_id}-{posting_id}", **overrides)
    return ObservationView(
        posting_id=posting_id,
        observation_id=observation_id,
        source_instance_id=INSTANCE[source_id],
        source_id=source_id,
        observed_at=observed_at,
        normalized=posting,
        completeness=posting.description_completeness,
        quality=posting.description_quality,
    )


def _salary(low: float, high: float, kind: SalaryKind) -> Salary:
    salary = from_bounds(low, high, currency="EUR", period="year", kind=kind, raw=None)
    if salary is None:
        raise ValueError(f"no salary from {low} and {high}")
    return salary


def stated(low: float, high: float) -> Salary:
    """A stated EUR salary per year from `low` to `high`."""
    return _salary(low, high, "stated")


def predicted(low: float, high: float) -> Salary:
    """A predicted (Adzuna estimate) EUR salary per year from `low` to `high`."""
    return _salary(low, high, "predicted")
