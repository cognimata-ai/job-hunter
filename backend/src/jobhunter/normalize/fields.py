"""Canonical field selection (spec §8.8): a job's fields from the latest observations of its postings.

`select_canonical` is pure: it reads `ObservationView`s (one per posting: its latest observation) and returns
`CanonicalFields`, every `jobs` column the §8.8 table selects, with the provenance of each, the det facts and
their hash, and the material projection (`llm_input`) with its hash and lineage. "First wins" orders the
candidates of a field by the row's criteria, then by **source rank** (`SOURCE_RANK`, the seeded
`sources.precedence_rank`: the ATS boards 1, JSearch and SerpApi 2, Arbeitnow 3, Adzuna 4) where the row names
it, and ties go to the most recent `observed_at`, then the lowest posting id (`precedence_key`). Recency only
breaks the ties of a single-valued choice: a union or an intersection depends on content alone, so a posting
seen again with the same content changes no value and no hash. The rows, as read here:

- `description_text`: a `garbage` description only when nothing else exists (§8.9), then `full` before
  `partial` (a partial never overwrites a full), `ok` before `short`, then source rank. A garbage-only job is
  `partial` (§8.9): `description_completeness` and the job's `completeness` say `partial`, and
  `description_quality` says `garbage`.
- `title`: source rank; its `title` is the display form of `normalize_title`, which also gives `title_norm`
  and `seniority_tokens`.
- `company_display`: the watchlist `display_name` (`WatchlistDisplay`, credited to the observation from the
  board that supplied it), else the best ATS observation's company, else the most frequent raw form (by
  `norm_text`; equally frequent forms by their most recent observation, then the lowest posting id).
  `company_key` is `company_key(company_display)`.
- `locations`: the union of the ATS observations' locations, ordered by content (folded label, then the
  candidates' gazetteer ids); a place that several boards state (same folded label and candidates) is taken
  from the board first by source rank and lowest posting id, never by recency. Without an ATS location, the
  best-ranked observation's parsed locations as they are, every candidate kept. `location_kind` is `multi`
  for two locations or more, `country_wide` or `city` for one (as its observation says), and for none the
  best-ranked `remote` or `country_wide` marker, else `unknown`.
- `work_mode_possible`: the intersection of the structured observations' values; an empty intersection is a
  conflict (`conflicts`, provenance rule `CONFLICT_RULE`) that keeps the best-ranked one. Without a
  structured value, the best-ranked text-derived one; otherwise all three modes, source `none`.
- `remote_countries`: the union of all values (only structured ISO codes are ever parsed).
- `employment_types`: the best observation by employment source (`structured` before `text`), then source
  rank: an ATS or JSearch structured value before Adzuna's `contract_time`/`contract_type` (structured, rank
  4) before a parsed string.
- `salary`: `stated` before `predicted`, then source rank (ATS > SerpApi > Adzuna).
- `posted_at_effective`: the best precision (`exact` > `hour` > `day` > `relative_day` > `ingest`), then
  source rank (ATS `exact` before Arbeitnow `exact`); the date is `min(provider date, first_seen_at)` and
  `posted_precision` stays the provider's. No provider date: `unknown`, no date.
- `ad_language`: a structured language (`ad_language_structured`, else `source_structured["language"]`:
  Greenhouse; `de`/`en` by their code or name, any other language `other`) by source rank, else
  `detect_language` of the selected description, else `unknown`.
- `industry_structured`: an ATS `industry`, then Personio's `occupationCategory`, then Adzuna's `industry`
  (its `category.tag`; never `unknown`), each by source rank.
- `structured_seniority` (Task 16 ruling T16-a, not in the table): the first `source_structured["seniority"]`
  (SmartRecruiters, Personio) by source rank.
- `source_ids`: every source of the observations, sorted. Outbound links stay on the postings.

`field_provenance` has an entry for every column but `source_ids`; a field that no observation supplies (no
salary, no date, ...) names the best-ranked observation with the rule `none`. `lineage` is the primary
provenance of each `llm_input` field and `contributors` every observation that supplied it, the primary first:
the boards of a location union and the postings of a work-mode intersection, which §8.8's lineage rows name
too, so a source purge finds the revision.
"""

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Final

from jobhunter.db import seed
from jobhunter.db.seed import content_hash
from jobhunter.normalize.companies import company_key
from jobhunter.normalize.language import detect_language
from jobhunter.normalize.text import fold, norm_text
from jobhunter.normalize.titles import normalize_title
from jobhunter.scoring.filters import DetFacts, det_facts_hash
from jobhunter.sources.base import NormalizedPosting
from jobhunter.sources.parsing.locations import Location
from jobhunter.sources.parsing.salary import Salary
from jobhunter.types import JSONValue

SOURCE_RANK: Final[Mapping[str, int]] = MappingProxyType({s.id: s.precedence_rank for s in seed.SOURCES})
ATS_SOURCES: Final = frozenset(s.id for s in seed.SOURCES if s.kind == "ats")
# The rule of a structured work-mode conflict in `field_provenance`: a job's stored provenance tells whether
# its last selection had the conflict (`revisions` raises and resolves `dedup.field_conflict` by it).
CONFLICT_RULE: Final = "structured_conflict"
CONFLICT_FIELDS: Final = ("work_mode_possible",)  # the fields whose structured values can conflict
_ALL_WORK_MODES: Final = frozenset({"remote", "hybrid", "onsite"})

_QUALITY_ORDER: Final = {"ok": 0, "short": 1, "garbage": 2}
_EMPLOYMENT_ORDER: Final = {"structured": 0, "text": 1}
_PRECISION_ORDER: Final = {"exact": 0, "hour": 1, "day": 2, "relative_day": 3, "ingest": 4}
_LANGUAGE_NAMES: Final = {
    **dict.fromkeys(("de", "deu", "ger", "german", "deutsch"), "de"),
    **dict.fromkeys(("en", "eng", "english", "englisch"), "en"),
}
_LANGUAGE_SUBTAG: Final = re.compile(r"[-_\s]")
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND: Final = timedelta(microseconds=1)


@dataclass(frozen=True)
class ObservationView:
    """The latest observation of one posting of the job, as the selection reads it."""

    posting_id: int
    observation_id: int
    source_instance_id: int
    source_id: str
    observed_at: datetime
    normalized: NormalizedPosting
    completeness: str  # posting_observations.completeness
    quality: str  # posting_observations.quality


@dataclass(frozen=True)
class WatchlistDisplay:
    """The `display_name` of the watchlist company whose board supplied `observation`, one of the job's."""

    display_name: str
    observation: ObservationView


@dataclass(frozen=True)
class Provenance:
    """Which observation supplied a field, and by which rule (`jobs.field_provenance[field]`, §8.8)."""

    source_instance_id: int
    posting_id: int
    observation_id: int
    observed_at: datetime
    rule: str

    def to_json(self) -> dict[str, JSONValue]:
        return {
            "source_instance_id": self.source_instance_id,
            "posting_id": self.posting_id,
            "observation_id": self.observation_id,
            "observed_at": self.observed_at.astimezone(UTC).isoformat(),
            "rule": self.rule,
        }


@dataclass(frozen=True)
class CanonicalFields:
    """The job's canonical fields (the `jobs` columns of §8.8), description, provenance and projections."""

    company_key: str
    company_display: str
    title: str
    title_norm: str
    seniority_tokens: frozenset[str]
    structured_seniority: str | None
    locations: tuple[Location, ...]
    location_kind: str
    work_mode_possible: frozenset[str]
    work_mode_source: str
    remote_countries: tuple[str, ...]
    employment_types: tuple[str, ...]
    employment_source: str
    salary: Salary | None
    industry_structured: str | None
    ad_language: str
    completeness: str
    posted_at_effective: datetime | None
    posted_precision: str
    source_ids: tuple[str, ...]
    description_text: str
    description_completeness: str
    description_quality: str
    field_provenance: dict[str, Provenance]
    det_facts: DetFacts
    det_facts_hash: bytes
    llm_input: dict[str, JSONValue]
    material_hash: bytes
    lineage: dict[str, Provenance]
    contributors: dict[str, tuple[Provenance, ...]]
    conflicts: tuple[str, ...]


def precedence_key(view: ObservationView) -> tuple[int, int, int, int]:
    """Source rank, then the most recent `observed_at`, then the lowest posting id (then observation id, so
    the order is total): the order "first wins" uses after a row's own criteria."""
    micros = (view.observed_at - _EPOCH) // _MICROSECOND
    return SOURCE_RANK[view.source_id], -micros, view.posting_id, view.observation_id


def _provenance(view: ObservationView, rule: str) -> Provenance:
    return Provenance(view.source_instance_id, view.posting_id, view.observation_id, view.observed_at, rule)


def _description_key(view: ObservationView) -> tuple[object, ...]:
    garbage = view.quality == "garbage"
    return garbage, view.completeness != "full", _QUALITY_ORDER[view.quality], *precedence_key(view)


def _company(
    views: Sequence[ObservationView], best: ObservationView, watchlist_display: WatchlistDisplay | None
) -> tuple[str, Provenance]:
    if watchlist_display is not None:
        supplier = watchlist_display.observation
        return watchlist_display.display_name, _provenance(supplier, "watchlist_display_name")
    named = [view for view in views if norm_text(view.normalized.company_raw)]
    if ats_named := [view for view in named if view.source_id in ATS_SOURCES]:
        supplier = min(ats_named, key=precedence_key)
        return norm_text(supplier.normalized.company_raw), _provenance(supplier, "ats")
    if not named:
        return "", _provenance(best, "none")
    counts = Counter(norm_text(view.normalized.company_raw) for view in named)
    supplier = min(
        named, key=lambda v: (-counts[norm_text(v.normalized.company_raw)], *precedence_key(v)[1:])
    )
    return norm_text(supplier.normalized.company_raw), _provenance(supplier, "most_frequent_raw_form")


def _location_identity(location: Location) -> tuple[str, tuple[str, ...]]:
    return fold(location.label), tuple(sorted(candidate.gazetteer_id for candidate in location.candidates))


def _one_location_kind(supplier: ObservationView) -> str:
    return "country_wide" if supplier.normalized.location_kind == "country_wide" else "city"


def _union_key(view: ObservationView) -> tuple[int, int, int]:
    """Source rank, then the lowest posting id: the order of the boards of a union. Never recency: a union is
    no single-valued choice, and a board seen again with the same content must not change it."""
    return SOURCE_RANK[view.source_id], view.posting_id, view.observation_id


def _locations(
    views: Sequence[ObservationView], best: ObservationView
) -> tuple[tuple[Location, ...], str, tuple[Provenance, ...]]:
    boards = sorted(
        (v for v in views if v.source_id in ATS_SOURCES and v.normalized.locations), key=_union_key
    )
    if boards:
        union: dict[tuple[str, tuple[str, ...]], tuple[Location, ObservationView]] = {}
        for view in boards:  # a place several boards state comes from the first board by `_union_key`
            for location in view.normalized.locations:
                union.setdefault(_location_identity(location), (location, view))
        ordered = [union[identity] for identity in sorted(union)]  # by content: folded label, candidates
        locations = tuple(location for location, _ in ordered)
        kind = "multi" if len(locations) >= 2 else _one_location_kind(ordered[0][1])
        return locations, kind, tuple(_provenance(view, "ats_union") for view in boards)
    if parsed := [view for view in views if view.normalized.locations]:
        supplier = min(parsed, key=precedence_key)
        locations = tuple(supplier.normalized.locations)
        kind = "multi" if len(locations) >= 2 else _one_location_kind(supplier)
        return locations, kind, (_provenance(supplier, "best_ranked"),)
    if markers := [view for view in views if view.normalized.location_kind in ("remote", "country_wide")]:
        supplier = min(markers, key=precedence_key)
        return (), supplier.normalized.location_kind, (_provenance(supplier, "marker"),)
    return (), "unknown", (_provenance(best, "none"),)


def _work_modes(
    views: Sequence[ObservationView], best: ObservationView
) -> tuple[frozenset[str], str, tuple[Provenance, ...], bool]:
    """The work modes, their source, the observations that supplied them, and whether they conflicted."""
    structured = sorted(
        (v for v in views if v.normalized.work_mode_source == "structured"), key=precedence_key
    )
    if structured:
        common = frozenset.intersection(*(view.normalized.work_mode_possible for view in structured))
        if common:
            return (
                common,
                "structured",
                tuple(_provenance(v, "structured_intersection") for v in structured),
                False,
            )
        kept = structured[0]
        return (
            kept.normalized.work_mode_possible,
            "structured",
            (_provenance(kept, CONFLICT_RULE),),
            True,
        )
    if text := [view for view in views if view.normalized.work_mode_source == "text"]:
        supplier = min(text, key=precedence_key)
        return supplier.normalized.work_mode_possible, "text", (_provenance(supplier, "text"),), False
    return _ALL_WORK_MODES, "none", (_provenance(best, "none"),), False


def _employment(
    views: Sequence[ObservationView], best: ObservationView
) -> tuple[tuple[str, ...], str, Provenance]:
    candidates = [
        view
        for view in views
        if view.normalized.employment_types and view.normalized.employment_source in _EMPLOYMENT_ORDER
    ]
    if not candidates:
        return (), "none", _provenance(best, "none")
    supplier = min(
        candidates, key=lambda v: (_EMPLOYMENT_ORDER[v.normalized.employment_source], *precedence_key(v))
    )
    source = supplier.normalized.employment_source
    return tuple(sorted(set(supplier.normalized.employment_types))), source, _provenance(supplier, source)


def _salary(views: Sequence[ObservationView], best: ObservationView) -> tuple[Salary | None, Provenance]:
    candidates = [(salary, view) for view in views if (salary := view.normalized.salary) is not None]
    if not candidates:
        return None, _provenance(best, "none")
    salary, supplier = min(candidates, key=lambda pair: (pair[0].kind != "stated", *precedence_key(pair[1])))
    return salary, _provenance(supplier, salary.kind)


def _posted(
    views: Sequence[ObservationView], best: ObservationView, first_seen_at: datetime
) -> tuple[datetime | None, str, Provenance]:
    candidates = [
        (posted_at, view)
        for view in views
        if (posted_at := view.normalized.posted_at) is not None
        and view.normalized.posted_precision in _PRECISION_ORDER
    ]
    if not candidates:
        return None, "unknown", _provenance(best, "none")
    provider, supplier = min(
        candidates,
        key=lambda pair: (_PRECISION_ORDER[pair[1].normalized.posted_precision], *precedence_key(pair[1])),
    )
    clamped = provider > first_seen_at
    rule = "clamped_to_first_seen_at" if clamped else "best_precision"
    return min(provider, first_seen_at), supplier.normalized.posted_precision, _provenance(supplier, rule)


def _language_code(value: str) -> str | None:
    """`de` or `en` for a language code or name of either (`de-DE`, `English`), `other` for any other value,
    `None` for a blank one."""
    primary = _LANGUAGE_SUBTAG.split(fold(value), maxsplit=1)[0]
    if not primary:
        return None
    return _LANGUAGE_NAMES.get(primary, "other")


def _structured_language(view: ObservationView) -> str | None:
    value: JSONValue = view.normalized.ad_language_structured
    if value is None:
        value = view.normalized.source_structured.get("language")
    return _language_code(value) if isinstance(value, str) else None


def _ad_language(
    views: Sequence[ObservationView], description: str, described: Provenance
) -> tuple[str, Provenance]:
    structured = [(language, view) for view in views if (language := _structured_language(view)) is not None]
    if structured:
        language, supplier = min(structured, key=lambda pair: precedence_key(pair[1]))
        return language, _provenance(supplier, "structured")
    detected = detect_language(description)
    return detected, replace(described, rule="unknown" if detected == "unknown" else "detected")


def _text(doc: Mapping[str, JSONValue], key: str) -> str | None:
    value = doc.get(key)
    return text if isinstance(value, str) and (text := norm_text(value)) else None


def _industry_of(view: ObservationView) -> tuple[int, str] | None:
    """The class of the view's structured industry in the §8.8 row (ATS 0, Personio's occupation 1, Adzuna and
    any other source 2) and the value; `None` when it has none."""
    structured = view.normalized.source_structured
    if (industry := _text(structured, "industry")) is not None and fold(industry) != "unknown":
        return (0 if view.source_id in ATS_SOURCES else 2), industry
    if view.source_id == "personio" and (occupation := _text(structured, "occupationCategory")) is not None:
        return 1, occupation
    return None


def _industry(views: Sequence[ObservationView], best: ObservationView) -> tuple[str | None, Provenance]:
    candidates = [(industry, view) for view in views if (industry := _industry_of(view)) is not None]
    if not candidates:
        return None, _provenance(best, "none")
    (_, value), supplier = min(candidates, key=lambda pair: (pair[0][0], *precedence_key(pair[1])))
    return value, _provenance(supplier, "structured")


def _seniority(views: Sequence[ObservationView], best: ObservationView) -> tuple[str | None, Provenance]:
    candidates = [
        (seniority, view)
        for view in views
        if (seniority := _text(view.normalized.source_structured, "seniority")) is not None
    ]
    if not candidates:
        return None, _provenance(best, "none")
    seniority, supplier = min(candidates, key=lambda pair: precedence_key(pair[1]))
    return seniority, _provenance(supplier, "structured")


def select_canonical(
    observations: Sequence[ObservationView],
    *,
    first_seen_at: datetime,
    watchlist_display: WatchlistDisplay | None,
) -> CanonicalFields:
    """The §8.8 selection over the latest observation of each posting (see the module docstring).

    `first_seen_at` is the job's, the bound of `posted_at_effective`; `watchlist_display` is the display name
    of the watchlist company whose board supplied one of the observations, with that observation, if any.
    Raises `ValueError` for no observation, or for a `watchlist_display` whose observation is not one of them.
    """
    if not observations:
        raise ValueError("select_canonical needs at least one observation")
    views = tuple(observations)
    if watchlist_display is not None and not any(
        (view.posting_id, view.observation_id)
        == (watchlist_display.observation.posting_id, watchlist_display.observation.observation_id)
        for view in views
    ):
        raise ValueError("the watchlist display's observation is not one of the observations")
    best = min(views, key=precedence_key)

    described = min(views, key=_description_key)
    description = _provenance(described, "description")
    description_text = described.normalized.description_text
    description_quality = described.quality
    description_completeness = "partial" if description_quality == "garbage" else described.completeness

    title = normalize_title(best.normalized.title_raw)  # source rank, then the tie-break: `best`
    titled_by = _provenance(best, "source_rank")
    company_display, company_by = _company(views, best, watchlist_display)
    locations, location_kind, located_by = _locations(views, best)
    work_modes, work_mode_source, moded_by, work_mode_conflict = _work_modes(views, best)
    remote_countries = tuple(
        sorted({country for view in views for country in view.normalized.remote_countries})
    )
    countries_from = [view for view in views if view.normalized.remote_countries]
    countries_by = (
        _provenance(min(countries_from, key=precedence_key), "structured_union")
        if countries_from
        else _provenance(best, "none")
    )
    employment_types, employment_source, employed_by = _employment(views, best)
    salary, salary_by = _salary(views, best)
    posted_at, posted_precision, posted_by = _posted(views, best, first_seen_at)
    ad_language, language_by = _ad_language(views, description_text, description)
    industry, industry_by = _industry(views, best)
    seniority, seniority_by = _seniority(views, best)

    provenance = {
        "company_key": company_by,
        "company_display": company_by,
        "title": titled_by,
        "title_norm": titled_by,
        "seniority_tokens": titled_by,
        "structured_seniority": seniority_by,
        "locations": located_by[0],
        "location_kind": located_by[0],
        "work_mode_possible": moded_by[0],
        "work_mode_source": moded_by[0],
        "remote_countries": countries_by,
        "employment_types": employed_by,
        "employment_source": employed_by,
        "salary": salary_by,
        "industry_structured": industry_by,
        "ad_language": language_by,
        "completeness": description,
        "posted_at_effective": posted_by,
        "posted_precision": posted_by,
        "description_text": description,
        "description_completeness": description,
        "description_quality": description,
    }
    facts = DetFacts(
        company_key=company_key(company_display),
        title_norm=title.title_norm,
        seniority_tokens=title.seniority_tokens,
        structured_seniority=seniority,
        locations=locations,
        location_kind=location_kind,
        work_mode_possible=work_modes,
        work_mode_source=work_mode_source,
        remote_countries=remote_countries,
        employment_types=frozenset(employment_types),
        employment_source=employment_source,
        salary=salary,
        industry_structured=industry,
        ad_language=ad_language,
        posted_at_effective=posted_at,
    )
    llm_input: dict[str, JSONValue] = {
        "title": title.title,
        "company_display": company_display,
        "locations": [location.label for location in locations],
        "work_mode_possible": [mode for mode in sorted(work_modes)],
        "work_mode_source": work_mode_source,
        "employment_types": list(employment_types),
        "employment_source": employment_source,
        "ad_language": ad_language,
        "description_completeness": description_completeness,
        "description_text": description_text,
    }
    contributors = {
        "title": (titled_by,),
        "company_display": (company_by,),
        "locations": located_by,
        "work_mode_possible": moded_by,
        "work_mode_source": moded_by,
        "employment_types": (employed_by,),
        "employment_source": (employed_by,),
        "ad_language": (language_by,),
        "description_completeness": (description,),
        "description_text": (description,),
    }
    return CanonicalFields(
        company_key=facts.company_key,
        company_display=company_display,
        title=title.title,
        title_norm=title.title_norm,
        seniority_tokens=facts.seniority_tokens,
        structured_seniority=seniority,
        locations=locations,
        location_kind=location_kind,
        work_mode_possible=work_modes,
        work_mode_source=work_mode_source,
        remote_countries=remote_countries,
        employment_types=employment_types,
        employment_source=employment_source,
        salary=salary,
        industry_structured=industry,
        ad_language=ad_language,
        completeness=description_completeness,
        posted_at_effective=posted_at,
        posted_precision=posted_precision,
        source_ids=tuple(sorted({view.source_id for view in views})),
        description_text=description_text,
        description_completeness=description_completeness,
        description_quality=description_quality,
        field_provenance=provenance,
        det_facts=facts,
        det_facts_hash=det_facts_hash(facts),
        llm_input=llm_input,
        material_hash=content_hash(llm_input),
        lineage={field: suppliers[0] for field, suppliers in contributors.items()},
        contributors=contributors,
        conflicts=CONFLICT_FIELDS if work_mode_conflict else (),
    )
