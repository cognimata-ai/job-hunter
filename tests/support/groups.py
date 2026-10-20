"""Duplicate-group test data (Task 20): `JobView`s with sane defaults, Location fixtures and canonical jobs.

`job(**overrides)` is a `groups.JobView` of a senior software engineer at `acme` in Berlin (`BERLIN`, kind
`city`), full text `FULL_TEXT`, first seen at `T`, `active`, source rank 1, no ATS board and no requisition
id; any field can be overridden. `varied(n, offset)` is a text of `n` distinct words of letters (`n - 4`
distinct 5-shingles).

The Location fixtures are built by hand, not from the gazetteer: `BERLIN`, `LEIPZIG` (about 150 km from
Berlin) and `ZURICH` carry their GeoNames ids and coordinates; `POTSDAM_24KM` is a point 24 km west of Berlin
under a test id of its own; `UNRESOLVED` has no candidate.

`canonical_job(engine, job_with_postings, source_id, …)` is a job with one posting of `source_id` (the
`job_with_postings` fixture) whose canonical fields and current revision come from `revisions.recompute_job`:
what identity resolution leaves behind, without a run. The overrides go to the observation's `normalized`.
"""

from datetime import UTC, datetime
from typing import Any, Final

from jobhunter.dedup import revisions
from jobhunter.dedup.groups import JobView
from jobhunter.sources.parsing.locations import Candidate, Location
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.collect import JobWithPostings

T: Final = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
# Letters only (`worteba`), so a text of them passes the §8.9 quality gate: digits would make it `garbage`.
WORDS: Final = tuple("wort" + "".join(chr(97 + (i // 26**k) % 26) for k in (2, 1, 0)) for i in range(400))


def varied(n: int, offset: int = 0) -> str:
    """`n` distinct words from `WORDS`, starting at `offset`: `n - 4` distinct 5-shingles."""
    return " ".join(WORDS[(offset + i) % len(WORDS)] for i in range(n))


FULL_TEXT: Final = varied(60)


def _place(label: str, gazetteer_id: str, country: str, lat: float, lon: float) -> Location:
    candidate = Candidate(gazetteer_id=gazetteer_id, country_code=country, lat=lat, lon=lon)
    return Location(label=label, resolution="unique", candidates=[candidate], postal_code=None, source="test")


BERLIN: Final = _place("Berlin", "geonames:2950159", "DE", 52.52437, 13.41053)
LEIPZIG: Final = _place("Leipzig", "geonames:2879139", "DE", 51.33962, 12.37129)
ZURICH: Final = _place("Zürich", "geonames:2657896", "CH", 47.36667, 8.55)
POTSDAM_24KM: Final = _place("Potsdam West", "test:potsdam-24km", "DE", 52.52437, 13.0555)
UNRESOLVED: Final = Location(
    label="Somewhere", resolution="unresolved", candidates=[], postal_code=None, source="test"
)

_DEFAULTS: Final[dict[str, Any]] = {
    "id": 1,
    "company_key": "acme",
    "title_norm": "senior software engineer",
    "seniority_tokens": frozenset({"senior"}),
    "locations": (BERLIN,),
    "location_kind": "city",
    "completeness": "full",
    "description_text": FULL_TEXT,
    "first_seen_at": T,
    "availability": "active",
    "source_rank": 1,
    "ats_boards": frozenset(),
    "requisition_ids": frozenset(),
    "merged": False,
}


def job(**overrides: Any) -> JobView:
    """A `JobView` of the defaults with `overrides` applied (an unknown field is a `TypeError`)."""
    return JobView(**{**_DEFAULTS, **overrides})


async def canonical_job(
    engine: AsyncEngine,
    job_with_postings: JobWithPostings,
    source_id: str,
    *,
    observed_at: datetime,
    **overrides: Any,
) -> int:
    """A job with one `source_id` posting observed at `observed_at` (`normalized` with `overrides`: Berlin,
    kind `city` and the title `Senior Software Engineer` unless given), its canonical fields and current
    revision recomputed; its id."""
    fields: dict[str, Any] = {
        "title_raw": "Senior Software Engineer",
        "locations": [BERLIN],
        "location_kind": "city",
        **overrides,
    }
    made = await job_with_postings({source_id: fields}, observed_at=observed_at)
    async with engine.begin() as conn:
        await revisions.recompute_job(conn, made.job_id)
    return made.job_id
