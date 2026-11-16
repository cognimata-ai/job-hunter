"""The Jobs list's fence conjunct, rendered (plan `2026-11-16-jobs-list-perf` Task P1, Ruling JP-1).

The list and its facets read "is any purge fence open" once per request, in their read snapshot, and pass it
to `listing._filter` as `fence_open`. With none open the filter names neither `is_fenced` nor `purge_fences`:
M4's SQL for the same query. With one open it adds `NOT is_fenced(j.id)` alone, and nothing else changes.
`listing.card_set`, the facets' set, renders the same filter.
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
QUERIES: Final = (
    JobsQuery(),
    JobsQuery(q="entwickler", sort="posted"),
    JobsQuery(loc=LocationFilter(gazetteer_id="geonames:2950159", lat=52.52437, lon=13.41053, radius_km=30)),
    JobsQuery(
        status=frozenset({"unseen", "seen", "saved", "applied", "hidden"}),
        company=frozenset({"acme"}),
        source=frozenset({"adzuna"}),
        score_min=40,
        confidence="high",
        contested="exclude",
    ),
)
# The keyword's and the location's matches, as `listing.read_matches` reads them (plan Task P2)
MATCHES: Final = Matches(keyword_revisions=(11, 12), keyword_jobs=(21,), near_jobs=(31,))
FENCE_TERMS: Final = ("is_fenced", "purge_fences")
NOT_FENCED: Final = " AND NOT is_fenced(j.id)"

cases = pytest.mark.parametrize(
    ("query", "window", "active"),
    [
        (query, window, active)
        for query in QUERIES
        for window in (ANY, SINCE)
        for active in (WITH_TUPLE, WITHOUT_TUPLE)
    ],
)


@cases
def test_without_an_open_fence_the_filter_names_no_fence(
    query: JobsQuery, window: Window, active: Active
) -> None:
    where = _filter(query, window, active, fence_open=False, matches=MATCHES)
    cards = card_set(query, window=window, active=active, fence_open=False, matches=MATCHES)
    for sql in (where.sql, cards.sql):
        assert not [term for term in FENCE_TERMS if term in sql], sql


@cases
def test_with_an_open_fence_the_filter_adds_not_is_fenced_alone(
    query: JobsQuery, window: Window, active: Active
) -> None:
    fenced = _filter(query, window, active, fence_open=True, matches=MATCHES)
    unfenced = _filter(query, window, active, fence_open=False, matches=MATCHES)
    assert fenced.sql == NOT_FENCED + unfenced.sql
    assert "purge_fences" not in fenced.sql  # the snapshot already showed a fence: no "none is open" term
    assert fenced.binds == unfenced.binds
    fenced_cards = card_set(query, window=window, active=active, fence_open=True, matches=MATCHES)
    unfenced_cards = card_set(query, window=window, active=active, fence_open=False, matches=MATCHES)
    assert fenced_cards.sql.count("is_fenced") == 1 and "purge_fences" not in fenced_cards.sql
    assert fenced_cards.sql.replace(NOT_FENCED, "", 1) == unfenced_cards.sql
    assert fenced_cards.binds == unfenced_cards.binds
