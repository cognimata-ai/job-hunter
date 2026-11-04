"""The Jobs list query (spec §15.3, §14.3; plan M4 Task 4, B4, B7, B15, B19, B31): its defaults, its 422s,
the absolute bounds of the posted window and the filters' hash."""

from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from jobhunter.jobs.query import (
    JobsQuery,
    LocationFilter,
    jobs_filters_hash,
    normalized_filters,
    resolve_window,
    score_filter_active,
)
from pydantic import ValidationError

NOW = datetime(2026, 11, 2, 9, 30, tzinfo=UTC)
BERLIN = {"gazetteer_id": "geonames:2950159", "lat": 52.52437, "lon": 13.41053, "radius_km": 30}


def refused_at(**fields: Any) -> list[tuple[str, ...]]:
    """The `loc` of every error `JobsQuery(**fields)` raises; the call must fail."""
    with pytest.raises(ValidationError) as caught:
        JobsQuery(**fields)
    return [tuple(str(part) for part in error["loc"]) for error in caught.value.errors()]


def test_equivalent_queries_hash_equal() -> None:
    after = NOW - timedelta(days=30)
    bare = JobsQuery()
    spelled = JobsQuery(
        sort="score_mid",
        posted="30d",
        score_min=0,
        score_max=100,
        coverage_min=0,
        confidence="any",
        include_remote=True,
        work_mode=["unknown", "onsite", "remote", "hybrid", "remote"],
        lang=["unknown", "other", "en", "de", "de"],
        status=["applied", "saved", "seen", "unseen", "seen"],
        availability=["unknown", "active", "unknown"],
        scoring_state=["expired", "failed", "filtered", "unscored", "scored", "scored"],
        contested="any",
        partial="any",
    )
    assert jobs_filters_hash(bare, posted_after=after) == jobs_filters_hash(spelled, posted_after=after)

    # the sort is no filter; the same instant in another zone is the same cut-off
    other_sort = JobsQuery(sort="posted")
    in_berlin = after.astimezone(ZoneInfo("Europe/Berlin"))
    assert jobs_filters_hash(other_sort, posted_after=in_berlin) == jobs_filters_hash(
        bare, posted_after=after
    )

    # reordered and repeated free-text multi-selects
    one = JobsQuery(company=["b-gmbh", "a-ag", "b-gmbh"], source=["jsearch", "adzuna"])
    two = JobsQuery(company=["a-ag", "b-gmbh"], source=["adzuna", "jsearch", "adzuna"])
    assert jobs_filters_hash(one, posted_after=after) == jobs_filters_hash(two, posted_after=after)

    # another cut-off, another filter value, another hash
    hashes = {
        jobs_filters_hash(bare, posted_after=after),
        jobs_filters_hash(bare, posted_after=after + timedelta(microseconds=1)),
        jobs_filters_hash(bare, posted_after=None),
        jobs_filters_hash(bare, posted_after=after, posted_before=NOW),
        jobs_filters_hash(JobsQuery(status=["unseen"]), posted_after=after),
        jobs_filters_hash(JobsQuery(loc=BERLIN), posted_after=after),
        jobs_filters_hash(JobsQuery(q="python"), posted_after=after),
    }
    assert len(hashes) == 7
    # a custom window's upper bound: the same instant in any zone, another instant another hash
    custom = JobsQuery(posted="custom", posted_from=date(2026, 10, 1), posted_to=date(2026, 10, 31))
    upper = datetime(2026, 10, 31, 23, 0, tzinfo=UTC)
    assert jobs_filters_hash(custom, posted_after=after, posted_before=upper) == jobs_filters_hash(
        custom, posted_after=after, posted_before=upper.astimezone(ZoneInfo("Europe/Berlin"))
    )
    assert jobs_filters_hash(custom, posted_after=after, posted_before=upper) != jobs_filters_hash(
        custom, posted_after=after, posted_before=upper + timedelta(hours=1)
    )


def test_normalized_filters_list_every_filter_but_the_sort() -> None:
    after = NOW - timedelta(days=30)
    filters = normalized_filters(JobsQuery(work_mode=["remote", "hybrid"], loc=BERLIN), posted_after=after)
    assert "sort" not in filters
    assert set(filters) == (set(JobsQuery.model_fields) - {"sort"}) | {"posted_after", "posted_before"}
    assert filters["work_mode"] == ["hybrid", "remote"]
    assert filters["status"] == ["applied", "saved", "seen", "unseen"]
    assert filters["company"] == []
    assert filters["posted_after"] == "2026-10-03T09:30:00.000000+00:00"
    assert filters["posted_before"] is None  # a relative window has no upper bound
    assert filters["loc"] == BERLIN
    assert filters["posted_from"] is None


def test_window_resolves_to_absolute_bounds() -> None:
    assert resolve_window(JobsQuery(), now=NOW, tz="Europe/Berlin") == (NOW - timedelta(days=30), None)
    assert resolve_window(JobsQuery(posted="24h"), now=NOW, tz="Europe/Berlin") == (
        NOW - timedelta(hours=24),
        None,
    )
    assert resolve_window(JobsQuery(posted="any"), now=NOW, tz="Europe/Berlin") == (None, None)
    custom = JobsQuery(posted="custom", posted_from=date(2026, 10, 1), posted_to=date(2026, 10, 31))
    after, before = resolve_window(custom, now=NOW, tz="Europe/Berlin")
    # 1 October is summer time (UTC+2), 1 November winter time (UTC+1)
    assert after == datetime(2026, 9, 30, 22, 0, tzinfo=UTC)
    assert before == datetime(2026, 10, 31, 23, 0, tzinfo=UTC)
    assert after is not None and after.utcoffset() == timedelta(0)
    assert before is not None and before.utcoffset() == timedelta(0)


def test_custom_needs_from_and_to_in_order() -> None:
    assert refused_at(posted="custom") == [("posted_from",), ("posted_to",)]
    assert refused_at(posted="custom", posted_from=date(2026, 10, 1)) == [("posted_to",)]
    assert refused_at(posted="30d", posted_from=date(2026, 10, 1), posted_to=date(2026, 10, 2)) == [
        ("posted_from",),
        ("posted_to",),
    ]
    assert refused_at(posted="custom", posted_from=date(2026, 10, 2), posted_to=date(2026, 10, 1)) == [
        ("posted_to",)
    ]
    same_day = JobsQuery(posted="custom", posted_from=date(2026, 10, 1), posted_to=date(2026, 10, 1))
    assert same_day.posted_to == date(2026, 10, 1)
    assert refused_at(score_min=60, score_max=59) == [("score_max",)]
    assert refused_at(score_min=-1) == [("score_min",)]
    assert refused_at(score_max=101) == [("score_max",)]
    assert refused_at(coverage_min=101) == [("coverage_min",)]


@pytest.mark.parametrize("field", ["work_mode", "lang", "status", "availability", "scoring_state"])
def test_an_empty_multiselect_is_refused(field: str) -> None:
    assert refused_at(**{field: []}) == [(field,)]


def test_the_free_multiselects_may_be_empty_and_the_closed_ones_hold_their_values() -> None:
    assert JobsQuery(company=[], source=[], employment=[], seniority=[]).company == frozenset()
    assert JobsQuery(employment=["full_time", "unknown"]).employment == {"full_time", "unknown"}
    assert JobsQuery(seniority=["mid", "senior"]).seniority == {"mid", "senior"}
    assert refused_at(employment=["full_time", "permanent"]) == [("employment",)]
    assert refused_at(seniority=["unknown"]) == [("seniority",)]
    assert refused_at(lang=["fr"]) == [("lang", "0")]
    assert refused_at(scoring_state=["cancelled"]) == [("scoring_state", "0")]
    assert refused_at(colour="red") == [("colour",)]


def test_the_defaults_are_those_of_the_spec() -> None:
    query = JobsQuery()
    assert (query.sort, query.posted, query.confidence, query.contested, query.partial) == (
        "score_mid",
        "30d",
        "any",
        "any",
        "any",
    )
    assert query.work_mode == {"remote", "hybrid", "onsite", "unknown"}
    assert query.lang == {"de", "en", "other", "unknown"}
    assert query.status == {"unseen", "seen", "saved", "applied"}
    assert query.availability == {"active", "unknown"}
    assert query.scoring_state == {"scored", "unscored", "filtered", "failed", "expired"}
    assert (query.loc, query.include_remote, query.q) == (None, True, None)


def test_score_filter_active_only_off_defaults() -> None:
    assert not score_filter_active(JobsQuery())
    assert not score_filter_active(JobsQuery(score_min=0, score_max=100, coverage_min=0, contested="only"))
    assert score_filter_active(JobsQuery(score_min=1))
    assert score_filter_active(JobsQuery(score_max=99))
    assert score_filter_active(JobsQuery(coverage_min=1))
    assert score_filter_active(JobsQuery(confidence="medium"))
    assert score_filter_active(JobsQuery(confidence="high"))


def test_a_nul_in_q_is_refused() -> None:
    assert refused_at(q="python\x00") == [("q",)]
    assert refused_at(q="python\ud800") == [("q",)]  # a lone surrogate cannot be UTF-8
    assert refused_at(q="") == [("q",)]
    assert refused_at(q="x" * 201) == [("q",)]
    assert JobsQuery(q="x" * 200).q == "x" * 200
    assert refused_at(company=["acme\x00"]) == [("company",)]
    assert refused_at(source=["\udfff"]) == [("source",)]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("lat", float("nan")),
        ("lat", float("inf")),
        ("lon", float("nan")),
        ("lon", float("-inf")),
        ("radius_km", float("nan")),
        ("radius_km", float("inf")),
        ("lat", "NaN"),
        ("lon", "Infinity"),
    ],
)
def test_a_non_finite_location_number_is_refused(field: str, value: object) -> None:
    assert refused_at(loc={**BERLIN, field: value}) == [("loc", field)]


def test_the_location_filter_holds_a_gazetteer_place() -> None:
    assert LocationFilter(**BERLIN).radius_km == 30
    assert refused_at(loc={**BERLIN, "gazetteer_id": "2950159"}) == [("loc", "gazetteer_id")]
    assert refused_at(loc={**BERLIN, "gazetteer_id": "geonames:" + "1" * 13}) == [("loc", "gazetteer_id")]
    assert refused_at(loc={**BERLIN, "lat": 90.5}) == [("loc", "lat")]
    assert refused_at(loc={**BERLIN, "lon": -180.5}) == [("loc", "lon")]
    assert refused_at(loc={**BERLIN, "radius_km": 0}) == [("loc", "radius_km")]
    assert refused_at(loc={**BERLIN, "radius_km": 201}) == [("loc", "radius_km")]
