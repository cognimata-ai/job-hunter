"""The Jobs list's keyword and location, bound from their matches (plan `2026-11-16-jobs-list-perf` Task P2).

A page reads the matching sets of its costly row terms once per request, in its read snapshot
(`listing.read_matches`): the revisions whose full text and the jobs whose title match the keyword, and the
jobs within the radius in the posted window. Every statement of the page binds them as arrays, so the filter
renders no text match and decides no radius of its own, for the card or for a deferred member, and
`listing.card_set` (the facets' set) renders the same. Each set is bound as an array literal built once per
request, since psycopg adapts a list again for every statement (about 40 ms for 50,000 ids). A query whose
sets were not read is refused rather than rendered without them.
"""

from datetime import UTC, datetime
from types import MappingProxyType
from typing import Final

import pytest
from jobhunter.jobs.listing import Active, Matches, _filter, card_set
from jobhunter.jobs.query import JobsQuery, LocationFilter

Window = tuple[datetime | None, datetime | None]
ANY: Final[Window] = (None, None)
SINCE: Final[Window] = (datetime(2026, 10, 17, tzinfo=UTC), None)
WITH_TUPLE: Final = Active(llm_tuple_id=7, scope_version_id=3, scope=MappingProxyType({}))
WITHOUT_TUPLE: Final = Active(llm_tuple_id=None, scope_version_id=3, scope=MappingProxyType({}))
KEYWORD: Final = JobsQuery(q="entwickler")
BERLIN_30_KM: Final = JobsQuery(
    loc=LocationFilter(gazetteer_id="geonames:2950159", lat=52.52437, lon=13.41053, radius_km=30)
)
READ: Final = Matches(keyword_revisions=(11, 12), keyword_jobs=(21,), near_jobs=(31, 32))
# What a text match of its own would name: the revisions' full text or the jobs' titles
TEXT_MATCH: Final = ("job_revisions", "websearch_to_tsquery", "<%", "title_norm")
# What a radius decided by the statement would name: the jsonpath steps, the function, their binds
RADIUS_MATCH: Final = ("jsonb_path_exists", "job_within_km", "locations", ":near_box", ":lat", ":radius_km")


@pytest.mark.parametrize("active", [WITH_TUPLE, WITHOUT_TUPLE])
@pytest.mark.parametrize("window", [ANY, SINCE])
def test_a_keyword_filter_binds_its_read_matches_and_matches_no_text(window: Window, active: Active) -> None:
    where = _filter(KEYWORD, window, active, fence_open=False, matches=READ)
    cards = card_set(KEYWORD, window=window, active=active, fence_open=False, matches=READ)
    for sql in (where.sql, cards.sql):
        assert not [term for term in TEXT_MATCH if term in sql], sql
    terms = 2 if active.llm_tuple_id is not None else 1  # the card's, and a deferred member's
    assert where.sql.count(":keyword_revisions") == where.sql.count(":keyword_jobs") == terms, where.sql
    for binds in (where.binds, cards.binds):
        assert (binds["keyword_revisions"], binds["keyword_jobs"]) == ("{11,12}", "{21}")
        assert not {"q", "qf"} & binds.keys()


def test_a_query_without_a_keyword_binds_no_matches() -> None:
    where = _filter(JobsQuery(), ANY, WITH_TUPLE, fence_open=False, matches=READ)
    assert ":keyword_" not in where.sql
    assert not {"keyword_revisions", "keyword_jobs"} & where.binds.keys()


@pytest.mark.parametrize("unread", [Matches(), Matches(keyword_revisions=(11,)), Matches(keyword_jobs=(21,))])
def test_a_keyword_whose_matches_were_not_read_is_refused(unread: Matches) -> None:
    with pytest.raises(ValueError, match="not read"):
        _filter(KEYWORD, ANY, WITH_TUPLE, fence_open=False, matches=unread)
    with pytest.raises(ValueError, match="not read"):
        card_set(KEYWORD, window=ANY, active=WITH_TUPLE, fence_open=False, matches=unread)


@pytest.mark.parametrize("include_remote", [True, False])
@pytest.mark.parametrize("active", [WITH_TUPLE, WITHOUT_TUPLE])
@pytest.mark.parametrize("window", [ANY, SINCE])
def test_a_location_filter_binds_its_read_matches_and_decides_no_radius(
    window: Window, active: Active, include_remote: bool
) -> None:
    query = BERLIN_30_KM.model_copy(update={"include_remote": include_remote})
    where = _filter(query, window, active, fence_open=False, matches=READ)
    cards = card_set(query, window=window, active=active, fence_open=False, matches=READ)
    for sql in (where.sql, cards.sql):
        assert not [term for term in RADIUS_MATCH if term in sql], sql
    terms = 2 if active.llm_tuple_id is not None else 1  # the card's, and a deferred member's
    assert where.sql.count(":near_jobs") == terms, where.sql
    assert where.sql.count("location_kind = 'remote'") == (terms if include_remote else 0), where.sql
    for binds in (where.binds, cards.binds):
        assert binds["near_jobs"] == "{31,32}"
        assert not {"lat", "lon", "radius_km", "near_box"} & binds.keys()


@pytest.mark.parametrize("unread", [Matches(), Matches(keyword_revisions=(11,), keyword_jobs=(21,))])
def test_a_location_whose_matches_were_not_read_is_refused(unread: Matches) -> None:
    with pytest.raises(ValueError, match="not read"):
        _filter(BERLIN_30_KM, ANY, WITH_TUPLE, fence_open=False, matches=unread)


def test_a_set_that_matched_nothing_binds_an_empty_array() -> None:
    nothing = Matches(keyword_revisions=(), keyword_jobs=(), near_jobs=())
    query = BERLIN_30_KM.model_copy(update={"q": "zahnarzt"})
    binds = _filter(query, ANY, WITH_TUPLE, fence_open=False, matches=nothing).binds
    assert (binds["keyword_revisions"], binds["keyword_jobs"], binds["near_jobs"]) == ("{}", "{}", "{}")
