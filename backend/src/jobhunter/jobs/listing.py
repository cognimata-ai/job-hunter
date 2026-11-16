"""The Jobs list engine (spec §15.3, §14.3 L5078; plan M4 Task 4, R8, B1, B2, B4, B5, B8, B15, B18, A1 #33).

`select_page` selects one page of cards in the caller's read snapshot (`db.snapshot.read_snapshot`): it reads
the active tuple and scope from `active_config` without a lock, whether a purge fence is open, and the matches
of its keyword and location (`read_matches`), then runs one statement per segment of the page's walk until it
holds `limit + 1` rows, and returns the first `limit`, the position after the last one (`next`, when there is
more) and, on a first page that asks for them, the counts. `read_active` is that read of the active tuple and
scope; `card_set` is the same cards under the same filters as SQL to aggregate over (the facets of M4 Task 5),
and `display_timezone` the active settings' zone of a `custom` window (B31).

**Cards** (B2). A card is an unmerged job with a current revision, not a synthetic golden-case job, not under
a purge fence (§20.2, M5 Task 8: `jobs._sql.NOT_FENCED`, as its detail answers 404), whose current revision
has no target of the active tuple in `deferred_duplicate`. The fence's conjunct is added only while a fence is
open: `select_page` and the facets read `privacy.fences.any_fence_open` once per request, in the caller's
snapshot, and pass it on as `fence_open` (plan `2026-11-16-jobs-list-perf` Task P1, Ruling JP-1). Within one
`REPEATABLE READ` snapshot every statement would see the same fences, so this is the per-statement "no fence
is open" of D21 read once; a caller outside such a snapshot would lose that agreement. A group card is its
representative's row. Each card has one score row: `t` the active-tuple target of its current revision, `r`
the result `t` published, `d` the current instance of `r` under the active scope, `h` its hard-filter result
under that scope and `u` the user's actions on it (no row: unseen).

**Filters.** The non-score filters (posted, keyword, location, work mode, company, source, ad language,
employment, seniority, availability, partial) match a card when its representative matches all of them or one
of its group's deferred members does (§8.7 L3342); user status, contested, the scoring state and the score
filters are the representative's own. The keyword's and the location's matches are read once per request, in
the caller's snapshot like the fence (`read_matches`): the revisions whose full text and the jobs whose title
the keyword matches, and the jobs within the radius in the posted window. Every statement binds them
(`Matches`), the card's term and a deferred member's alike, so each agrees with the others as a term decided
in the statement would (plan `2026-11-16-jobs-list-perf` Task P2; `jobs._sql`'s "Plans at scale"). Every SQL
fragment is a static string of `jobs._sql`, a statement is
composed only by choosing among them, and every value is a bound parameter; the sort and the score variant
choose fragments from closed mappings (§16.7). The scoring-state buckets are exclusive (B8); `cancelled`, and
a published target whose result has no current instance under the active scope, are in none, so never
listed. An active score filter (B15) lists only cards with a score.

**Sections.** A score sort walks the scored section first: the current instances of the active scope by
`det_rank_mid` or `det_rank_low` (the variant, then job id, both descending), joined to their result, the
active-tuple target that published it and the card whose current revision it scores. When that runs out
inside a page, the unscored section starts from its beginning in the same snapshot. The unscored section and
the `posted` sort follow the NULLS LAST rule of L5078: the non-NULL branch `N` (value descending, then job id
descending), then the NULL branch `Z` (job id descending). A page with no position in the section reads `N`
and then `Z` from their starts; a position with a value reads `N` after it and then `Z` from its start, so a
NULL row with a higher job id than the last valued one is never skipped; a position without a value reads `Z`
below its job id. In the unscored section, `N` is the targets of `score_targets_unscored_rank` with a
similarity, merged with the not-a-job-ad cards with one (B1, when that bucket is selected), and `Z` is the
targets without a similarity, the cards with no target of the active tuple (`jobs_pkey` backwards; without an
active tuple every card, A1 #33) and the not-a-job-ad cards without one. `posted` walks `jobs_list_posted_any`
and `first_seen` `jobs_list_first_seen`, each in one section `all`, a group card by its representative's
dates. A segment that no selected bucket can fill is not run.

**Positions.** A row's sort value is its section's: a score `Decimal` with two places, a similarity `float`
(or `None`), a UTC `datetime` (or `None` for `posted`), so a `Position` converts exactly into a
`api.pagination.JobCursor`. Scores are bound as `CAST(:v AS numeric)` from their fixed-point strings (B7).

**Counts** (B18), on a first page only: the cards matching every filter and those of them without a score
(the unscored section), each a `count(*)` over a sub-select limited to `COUNT_CAP + 1` rows; above the cap
both stop at `COUNT_CAP` and `capped` is set.

**Plans.** The statements keep their index walks and their cost low on 50,000 cards (M4 Task 5, B29):
`jobs._sql`'s "Plans at scale", and the location filter's jsonpath bounds (`_near_box`).
"""

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from math import cos, degrees, isfinite, radians
from types import MappingProxyType
from typing import Final, Literal

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.api.pagination import JobSection
from jobhunter.geo.gazetteer import EARTH_RADIUS_KM
from jobhunter.jobs import _sql
from jobhunter.jobs.query import JobsQuery, LocationFilter, score_filter_active
from jobhunter.normalize.text import fold
from jobhunter.privacy.fences import any_fence_open
from jobhunter.settings.model import Display
from jobhunter.types import JSONValue

COUNT_CAP: Final = 10_000  # B18
INSIDE_SHARE: Final = 0.99  # of half the radius: the inner box's margin below the radius
LONGITUDE_BOUND_BELOW: Final = 89  # `job_within_km` bounds longitude only while |lat| + radius / 110.5 < 89
NO_LONGITUDE_BOUND: Final = 360.0  # a band longitude bound that every candidate meets

SortValue = Decimal | float | datetime | None


@dataclass(frozen=True)
class Position:
    """The place after a row of the list: its section, its sort value and its job id."""

    section: JobSection
    value: SortValue
    job_id: int


@dataclass(frozen=True)
class SelectedRow:
    job_id: int
    section: JobSection
    sort_value: SortValue


@dataclass(frozen=True)
class Counts:
    total: int
    unscored: int
    capped: bool


@dataclass(frozen=True)
class PageSelection:
    rows: list[SelectedRow]
    next: Position | None
    counts: Counts | None


# The sections a sort lists (§14.3) and the buckets each source of the unscored section can fill
_SORT_SECTIONS: Final[Mapping[str, tuple[JobSection, ...]]] = MappingProxyType(
    {
        "score_mid": ("scored", "unscored"),
        "score_low": ("scored", "unscored"),
        "posted": ("all",),
        "first_seen": ("all",),
    }
)
_SCORED_BUCKETS: Final = frozenset({"scored", "filtered"})
_TARGET_BUCKETS: Final = frozenset({"unscored", "filtered", "failed", "expired"})
_ALL_MODES: Final = frozenset({"remote", "hybrid", "onsite", "unknown"})
_ALL_LANGS: Final = frozenset({"de", "en", "other", "unknown"})
_ALL_AVAILABILITY: Final = frozenset({"active", "unknown", "stale", "closed"})

Branch = Literal["ranked", "unranked"]


@dataclass(frozen=True)
class Active:
    """The active tuple (`None` before the first confirmed profile) and scope that `active_config` names, and
    the scope's payload (`scope_versions.payload`: the cards' keyword chips read it, `jobs.cards`)."""

    llm_tuple_id: int | None
    scope_version_id: int
    scope: Mapping[str, JSONValue]


@dataclass(frozen=True)
class CardSet:
    """The cards that match every filter of a query, as SQL over a card's score row (the aliases of
    `jobs._sql`): `sql` is its `FROM … WHERE …`, to which a caller may add ` AND` conjuncts, and `binds` its
    parameters."""

    sql: str
    binds: Mapping[str, object]


@dataclass(frozen=True)
class Matches:
    """The matches of a query's keyword and location, read once per request in the caller's snapshot
    (`read_matches`): the revisions whose full text and the jobs whose title the keyword matches
    (`jobs._sql.KEYWORD_MATCHES`), and the jobs within the radius in the posted window
    (`jobs._sql.NEAR_MATCHES`); `None` while not read, as for a query without that term. A filter binds them;
    one whose sets were not read is refused."""

    keyword_revisions: tuple[int, ...] | None = None
    keyword_jobs: tuple[int, ...] | None = None
    near_jobs: tuple[int, ...] | None = None


@dataclass(frozen=True)
class _Filter:
    """The conjuncts `q` adds to a walk over a card's score row (each led by ` AND `) and their binds."""

    sql: str
    binds: Mapping[str, object]


@dataclass(frozen=True)
class _Segment:
    """One statement of a page and the section of its rows."""

    tag: str
    sql: str
    binds: Mapping[str, object]
    section: JobSection


# --- the filters -----------------------------------------------------------------------------------------


def _read(matched: tuple[int, ...] | None) -> str:
    """The matches as an array literal, built once per request: psycopg would adapt a list again for every
    statement, about 40 ms for 50,000 ids, against 3 ms for the literal (every element an `int`)."""
    if matched is None:
        raise ValueError("the query's matches were not read")
    return "{" + ",".join(str(int(job_or_revision)) for job_or_revision in matched) + "}"


def _window_terms(window: tuple[datetime | None, datetime | None]) -> tuple[list[str], dict[str, object]]:
    """The posted window's terms (`_sql.ROW_TEMPLATES` names) and their binds."""
    posted_after, posted_before = window
    terms: list[str] = []
    binds: dict[str, object] = {}
    if posted_after is not None:
        terms.append("posted_after")
        binds["posted_after"] = posted_after
    if posted_before is not None:
        terms.append("posted_before")
        binds["posted_before"] = posted_before
    return terms, binds


def _row_terms(
    q: JobsQuery, window: tuple[datetime | None, datetime | None], matches: Matches
) -> tuple[list[tuple[str, ...]], dict[str, object]]:
    """The non-score terms `q` selects, each a tuple of `_sql.ROW_TEMPLATES` names that are ORed, and their
    binds; the keyword's and the location's are their `matches`."""
    dated, binds = _window_terms(window)
    terms: list[tuple[str, ...]] = [(name,) for name in dated]
    if q.q is not None:
        terms.append(("keyword",))
        binds |= {
            "keyword_revisions": _read(matches.keyword_revisions),
            "keyword_jobs": _read(matches.keyword_jobs),
        }
    if q.loc is not None:
        terms.append(("near_matched", "remote") if q.include_remote else ("near_matched",))
        binds["near_jobs"] = _read(matches.near_jobs)
    if q.work_mode != _ALL_MODES:
        known = sorted(q.work_mode - {"unknown"})
        terms.append(
            (*(("known_mode",) if known else ()), *(("unknown_mode",) if "unknown" in q.work_mode else ()))
        )
        if known:
            binds["work_modes"] = known
    if q.company:
        terms.append(("company",))
        binds["companies"] = sorted(q.company)
    if q.source:
        terms.append(("source",))
        binds["sources"] = sorted(q.source)
    if q.lang != _ALL_LANGS:
        terms.append(("lang",))
        binds["langs"] = sorted(q.lang)
    if q.employment:
        types = sorted(q.employment - {"unknown"})
        terms.append(
            (*(("employment",) if types else ()), *(("no_employment",) if "unknown" in q.employment else ()))
        )
        if types:
            binds["employment"] = types
    if q.seniority:
        terms.append(("seniority", "no_seniority") if "mid" in q.seniority else ("seniority",))
        binds["levels"] = sorted(q.seniority)
    if q.availability != _ALL_AVAILABILITY:
        terms.append(("availability",))
        binds["availability"] = sorted(q.availability)
    if q.partial != "any":
        terms.append(("partial",) if q.partial == "only" else ("full",))
    return terms, binds


def _near_box(lat: float, lon: float, radius_km: int) -> dict[str, float]:
    """The bounds of the location filter's two jsonpath steps (`jobs._sql.NEAR_INSIDE`, `NEAR_BAND`).

    **The band** is a superset of `job_within_km`'s matches: its own pre-check, widened by
    `NEAR_BAND_WIDENING`. Latitude: `lo..hi`, |Δlat| <= radius / 110.5. Longitude: `lo_lon..hi_lon`, the
    function's |Δlon| bound radius / (110.5 * cos(|lat| + radius / 110.5)), but only where the function
    applies it (|lat| + radius / 110.5 below 89, here with the widening) and the bounds stay within ±180, so
    that the shorter way round the meridians is the direct one for a candidate in [-180, 180]; elsewhere they
    are ±`NO_LONGITUDE_BOUND`, which bounds nothing.

    **The inner box**, `lo_in..hi_in` by `west..east`, lies within the radius: it spans `INSIDE_SHARE` of
    half the radius each way on `geo_km`'s sphere (of radius `geo.gazetteer.EARTH_RADIUS_KM`), in latitude
    along the meridian and in longitude along the parallel of the box's latitude nearest the equator, where a
    degree is longest. A point of the box is reached from the centre by a meridian arc and then a parallel
    arc, each at most that share of half the radius, and the great circle is no longer than that path, so its
    `geo_km` distance is below the radius whatever the float rounding.
    """
    widening = _sql.NEAR_BAND_WIDENING
    band = radius_km / 110.5
    lo_lon, hi_lon = -NO_LONGITUDE_BOUND, NO_LONGITUDE_BOUND
    if abs(lat) + band + widening < LONGITUDE_BOUND_BELOW:
        reach = radius_km / (110.5 * cos(radians(abs(lat) + band))) + widening
        if -180 < lon - reach and lon + reach < 180:
            lo_lon, hi_lon = lon - reach, lon + reach
    half = degrees(INSIDE_SHARE * radius_km / (2 * EARTH_RADIUS_KM))
    lo_in, hi_in = lat - half, lat + half
    nearest = 0.0 if lo_in <= 0 <= hi_in else min(abs(lo_in), abs(hi_in))
    wide = degrees(INSIDE_SHARE * radius_km / (2 * EARTH_RADIUS_KM * cos(radians(min(nearest, 90.0)))))
    return {
        "lo": lat - band - widening,
        "hi": lat + band + widening,
        "lo_lon": lo_lon,
        "hi_lon": hi_lon,
        "lo_in": lo_in,
        "hi_in": hi_in,
        "west": lon - wide,
        "east": lon + wide,
    }


def _rendered(terms: Sequence[tuple[str, ...]], alias: str) -> str:
    fragments = _sql.ROW_SQL[alias]
    return (
        "(" + " AND ".join("(" + " OR ".join(fragments[name] for name in term) + ")" for term in terms) + ")"
    )


def _filter(
    q: JobsQuery,
    window: tuple[datetime | None, datetime | None],
    active: Active,
    *,
    fence_open: bool,
    matches: Matches,
) -> _Filter:
    """Every predicate of `q` over a card's score row (`j`, `t`, `r`, `d`, `h`, `u`), module docstring; the
    fence's only while the caller's snapshot shows one open (`fence_open`), the keyword's as its `matches`."""
    terms, binds = _row_terms(q, window, matches)
    conjuncts: list[str] = [_sql.NOT_FENCED] if fence_open else []  # a fenced job is no card (docstring)
    if terms and active.llm_tuple_id is None:  # no tuple, no deferred member
        conjuncts.append(_rendered(terms, "j"))
    elif terms:
        conjuncts.append(
            "("
            + _rendered(terms, "j")
            + " OR "
            + _sql.DEFERRED_MEMBER
            + _rendered(terms, "m")
            + _sql.DEFERRED_MEMBER_END
            + ")"
        )
    if "hidden" not in q.status:
        conjuncts.append(_sql.NOT_HIDDEN)
    if not {"unseen", "seen"} <= q.status:  # else every row of the status set's OR
        conjuncts.append(
            "(" + " OR ".join(_sql.STATUS_SQL[s] for s in _sql.STATUS_ORDER if s in q.status) + ")"
        )
    conjuncts.append(
        "(" + " OR ".join(_sql.BUCKET_SQL[b] for b in _sql.BUCKET_ORDER if b in q.scoring_state) + ")"
    )
    if score_filter_active(q):
        variant = "score_low" if q.sort == "score_low" else "score_mid"
        conjuncts += [_sql.SCORE_PRESENT, _sql.SCORE_RANGE[variant], _sql.COVERAGE]
        binds |= {"score_min": q.score_min, "score_max": q.score_max, "coverage_min": q.coverage_min}
        if q.confidence != "any":
            conjuncts.append(_sql.CONFIDENCE[q.confidence])
    if q.contested != "any":
        conjuncts.append(_sql.CONTESTED[q.contested])
    return _Filter("".join(" AND " + c for c in conjuncts), MappingProxyType(binds))


# --- the walk --------------------------------------------------------------------------------------------


def _statement(walk: _sql.Walk, where: _Filter, *, bounded: bool) -> str:
    return walk.head + (walk.after if bounded else "") + where.sql + walk.order


def _merged(branch: Branch, walks: Sequence[_sql.Walk], where: _Filter, *, bounded: bool) -> str:
    """One walk's statement, or the walks' statements (each limited) merged in the branch's order."""
    if len(walks) == 1:
        return _statement(walks[0], where, bounded=bounded)
    parts = " UNION ALL ".join("(" + _statement(w, where, bounded=bounded) + ")" for w in walks)
    return _sql.MERGED_HEAD + parts + _sql.MERGED_ORDER[branch]


def _unscored_walks(q: JobsQuery, active: Active, branch: Branch) -> list[_sql.Walk]:
    """The sources of one branch of the unscored section that a selected bucket can fill."""
    if score_filter_active(q):  # B15: scored cards only
        return []
    tuple_ = active.llm_tuple_id is not None
    targets = tuple_ and bool(q.scoring_state & _TARGET_BUCKETS) and q.contested != "only"
    not_an_ad = tuple_ and "not_a_job_ad" in q.scoring_state
    untargeted = "unscored" in q.scoring_state and q.contested != "only"
    if branch == "ranked":
        return [w for w, on in ((_sql.TARGETS_RANKED, targets), (_sql.NOT_AN_AD_RANKED, not_an_ad)) if on]
    sources = (
        (_sql.TARGETS_UNRANKED, targets),
        (_sql.UNTARGETED, untargeted),
        (_sql.NOT_AN_AD_UNRANKED, not_an_ad),
    )
    return [w for w, on in sources if on]


def _bound(after: Position) -> dict[str, object]:
    value = after.value
    binds: dict[str, object] = {"after_id": after.job_id}
    if isinstance(value, Decimal):
        binds["after_value"] = format(value, "f")  # B7: a score is bound from its fixed-point string
    elif value is not None:
        binds["after_value"] = value
    return binds


def _segments(q: JobsQuery, active: Active, where: _Filter, after: Position | None) -> Iterator[_Segment]:
    """The statements of the page's walk in order (module docstring, Sections)."""
    no_bound: Mapping[str, object] = MappingProxyType({})
    bound = no_bound if after is None else MappingProxyType(_bound(after))
    match q.sort:
        case "score_mid" | "score_low":
            if after is None or after.section == "scored":
                if active.llm_tuple_id is not None and q.scoring_state & _SCORED_BUCKETS:
                    walk = _sql.SCORED[q.sort]
                    yield _Segment(
                        "scored", _statement(walk, where, bounded=after is not None), bound, "scored"
                    )
                yield from _nulls_last(q, active, where, None, no_bound)
            else:
                yield from _nulls_last(q, active, where, after, bound)
        case "posted":
            yield from _posted(where, after, bound, no_bound)
        case "first_seen":
            yield _Segment(
                "first_seen", _statement(_sql.FIRST_SEEN, where, bounded=after is not None), bound, "all"
            )


def _nulls_last(
    q: JobsQuery,
    active: Active,
    where: _Filter,
    after: Position | None,
    bound: Mapping[str, object],
) -> Iterator[_Segment]:
    """The unscored section from `after` (module docstring, the NULLS LAST rule)."""
    if after is None or after.value is not None:
        if ranked := _unscored_walks(q, active, "ranked"):
            text = _merged("ranked", ranked, where, bounded=after is not None)
            yield _Segment("unscored_ranked", text, bound, "unscored")
    if unranked := _unscored_walks(q, active, "unranked"):
        bounded = after is not None and after.value is None
        text = _merged("unranked", unranked, where, bounded=bounded)
        yield _Segment("unscored_unranked", text, bound if bounded else MappingProxyType({}), "unscored")


def _posted(
    where: _Filter, after: Position | None, bound: Mapping[str, object], no_bound: Mapping[str, object]
) -> Iterator[_Segment]:
    """The `posted` sort from `after` (the NULLS LAST rule)."""
    if after is None or after.value is not None:
        yield _Segment("posted", _statement(_sql.POSTED, where, bounded=after is not None), bound, "all")
    bounded = after is not None and after.value is None
    text = _statement(_sql.POSTED_UNKNOWN, where, bounded=bounded)
    yield _Segment("posted_unknown", text, bound if bounded else no_bound, "all")


def _checked(q: JobsQuery, after: Position | None) -> None:
    """Refuse a position that no page of this sort ends at (`ValueError`)."""
    if after is None:
        return
    value = after.value
    match after.section:
        case "scored":
            fits = isinstance(value, Decimal) and value.is_finite()
        case "unscored":
            fits = value is None or (
                isinstance(value, float | int) and not isinstance(value, bool) and isfinite(value)
            )
        case _:
            dated = isinstance(value, datetime) and value.utcoffset() is not None
            fits = dated or (value is None and q.sort == "posted")
    if after.section not in _SORT_SECTIONS[q.sort] or not fits or after.job_id < 1:
        raise ValueError(f"not a position of the {q.sort} sort")


def _sort_value(section: JobSection, raw: object) -> SortValue:
    match raw:
        case None:
            return None
        case Decimal() if section == "scored":
            return raw
        case float() if section == "unscored":
            return raw
        case datetime() if section == "all":
            return raw.astimezone(UTC)
    raise TypeError(f"not a sort value of the {section} section: {type(raw).__name__}")


async def _counts(conn: AsyncConnection, where: _Filter, binds: Mapping[str, object]) -> Counts:
    """B18: both counts of a first page, capped at `COUNT_CAP` (read at the call, so a test may patch it)."""
    cap = COUNT_CAP
    params = {**binds, **where.binds, "cap": cap + 1}
    total_sql = _sql.TAG["count"] + _sql.COUNT_HEAD + where.sql + _sql.COUNT_TAIL
    unscored_sql = (
        _sql.TAG["count_unscored"] + _sql.COUNT_HEAD + _sql.UNSCORED_SECTION + where.sql + _sql.COUNT_TAIL
    )
    total = int((await conn.execute(_sql.clause(total_sql), params)).scalar_one())
    unscored = int((await conn.execute(_sql.clause(unscored_sql), params)).scalar_one())
    return Counts(total=min(total, cap), unscored=min(unscored, cap), capped=total > cap)


async def read_active(conn: AsyncConnection) -> Active:
    """The active tuple, scope and scope payload, read without a lock (a read snapshot takes none)."""
    row = (await conn.execute(_sql.ACTIVE)).one()
    llm_tuple = None if row.llm_tuple_id is None else int(row.llm_tuple_id)
    return Active(llm_tuple, int(row.scope_version_id), MappingProxyType(dict(row.scope)))


async def read_matches(
    conn: AsyncConnection, q: JobsQuery, *, window: tuple[datetime | None, datetime | None]
) -> Matches:
    """The matches of `q`'s keyword and location (`Matches`), each read once in the caller's snapshot
    (`jobs._sql`'s "Plans at scale"); `window` is the posted window's absolute bounds."""
    matches = Matches()
    if q.q is not None:
        matches = await _keyword_matches(conn, q.q)
    if q.loc is not None:
        matches = replace(matches, near_jobs=await _near_matches(conn, q.loc, window))
    return matches


async def _keyword_matches(conn: AsyncConnection, keyword: str) -> Matches:
    """The keyword's two sets, read by its two text indexes: sequential scans are off for that one statement,
    and then the caller's setting is back. A failure leaves the setting to the transaction, which it
    aborts."""
    setting = (await conn.execute(_sql.SEQSCAN)).scalar_one()
    await conn.execute(_sql.SET_SEQSCAN, {"setting": "off"})
    statement = _sql.clause(_sql.TAG["keyword"] + _sql.KEYWORD_MATCHES)
    row = (await conn.execute(statement, {"q": keyword, "qf": fold(keyword)})).one()
    await conn.execute(_sql.SET_SEQSCAN, {"setting": setting})
    return Matches(keyword_revisions=tuple(row.revisions), keyword_jobs=tuple(row.jobs))


async def _near_matches(
    conn: AsyncConnection, loc: LocationFilter, window: tuple[datetime | None, datetime | None]
) -> tuple[int, ...]:
    """The jobs within `loc`'s radius whose date is in the posted window (`jobs._sql.NEAR_MATCHES`)."""
    dated, binds = _window_terms(window)
    where = " AND ".join(_sql.ROW_SQL["j"][name] for name in (*dated, "near"))
    statement = _sql.clause(_sql.TAG["near"] + _sql.NEAR_MATCHES + where + _sql.NEAR_MATCHES_END)
    binds |= {
        "lat": loc.lat,
        "lon": loc.lon,
        "radius_km": loc.radius_km,
        "near_box": _near_box(loc.lat, loc.lon, loc.radius_km),
    }
    return tuple((await conn.execute(statement, binds)).scalar_one())


async def display_timezone(conn: AsyncConnection) -> str:
    """The active settings' `display.timezone`, the zone of a `custom` posted window (B31); the settings
    model's default when the payload has none."""
    zone = (await conn.execute(_sql.DISPLAY_TIMEZONE)).scalar_one()
    return Display().timezone if zone is None else str(zone)


def card_set(
    q: JobsQuery,
    *,
    window: tuple[datetime | None, datetime | None],
    active: Active,
    fence_open: bool,
    matches: Matches,
) -> CardSet:
    """The cards `select_page` lists for `q` under `active`, in no order (the facets' set, `jobs.facets`):
    every filter of `q` over the card's score row, with the sort's score variant; `fence_open` is
    `privacy.fences.any_fence_open` and `matches` is `read_matches` in the caller's snapshot (module
    docstring, Cards and Filters)."""
    where = _filter(q, window, active, fence_open=fence_open, matches=matches)
    context = {"tuple": active.llm_tuple_id, "scope": active.scope_version_id}
    return CardSet(_sql.CARD_FROM + _sql.CARD + where.sql, MappingProxyType({**context, **where.binds}))


async def select_page(
    conn: AsyncConnection,
    q: JobsQuery,
    *,
    window: tuple[datetime | None, datetime | None],
    after: Position | None,
    limit: int,
    with_counts: bool,
) -> PageSelection:
    """One page of the Jobs list after `after` (the first page when `None`), in the caller's snapshot:
    `window` is the posted window's absolute bounds (`query.resolve_window`), `limit` the page size (at least
    1); the counts only on a first page that asks for them. A position that no page of `q.sort` ends at raises
    `ValueError`."""
    if limit < 1:
        raise ValueError("the limit must be at least 1")
    _checked(q, after)
    active = await read_active(conn)
    fence_open = await any_fence_open(conn)  # once (module docstring, Cards)
    matches = await read_matches(conn, q, window=window)  # once (module docstring, Filters)
    where = _filter(q, window, active, fence_open=fence_open, matches=matches)
    context = {"tuple": active.llm_tuple_id, "scope": active.scope_version_id}
    rows: list[SelectedRow] = []
    for segment in _segments(q, active, where, after):
        if len(rows) > limit:
            break
        params = {**context, **where.binds, **segment.binds, "limit": limit + 1 - len(rows)}
        result = await conn.execute(_sql.clause(_sql.TAG[segment.tag] + segment.sql), params)
        rows += [
            SelectedRow(int(r.job_id), segment.section, _sort_value(segment.section, r.sort_value))
            for r in result
        ]
    following = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        following = Position(last.section, last.sort_value, last.job_id)
    counts = await _counts(conn, where, context) if with_counts and after is None else None
    return PageSelection(rows=rows, next=following, counts=counts)
