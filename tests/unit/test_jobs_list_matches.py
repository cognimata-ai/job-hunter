"""The Jobs list's keyword, bound from its matches (plan `2026-11-16-jobs-list-perf` Task P2).

A keyword page reads the keyword's two matching sets once per request, in its read snapshot
(`listing.read_matches`): the revisions whose full text and the jobs whose title match it. Every statement of
the page binds them as arrays, so the filter renders no text match of its own, for the card or for a deferred
member, and `listing.card_set` (the facets' set) renders the same. A query whose sets were not read is
refused rather than rendered without them.
"""

from datetime import UTC, datetime
from types import MappingProxyType
from typing import Final

import pytest
from jobhunter.jobs.listing import Active, Matches, _filter, card_set
from jobhunter.jobs.query import JobsQuery

Window = tuple[datetime | None, datetime | None]
ANY: Final[Window] = (None, None)
SINCE: Final[Window] = (datetime(2026, 10, 17, tzinfo=UTC), None)
WITH_TUPLE: Final = Active(llm_tuple_id=7, scope_version_id=3, scope=MappingProxyType({}))
WITHOUT_TUPLE: Final = Active(llm_tuple_id=None, scope_version_id=3, scope=MappingProxyType({}))
KEYWORD: Final = JobsQuery(q="entwickler")
READ: Final = Matches(keyword_revisions=(11, 12), keyword_jobs=(21,))
# What a text match of its own would name: the revisions' full text or the jobs' titles
TEXT_MATCH: Final = ("job_revisions", "websearch_to_tsquery", "<%", "title_norm")


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
        assert (binds["keyword_revisions"], binds["keyword_jobs"]) == ([11, 12], [21])
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
