"""The Arbeitnow firehose coverage model under ruling T10-a (spec §5.3.4, §5.6; docs/spikes/V8.md).

Every run reads to the page cap; only the end of the feed stops it early. A read proves coverage only when it
ran from page 1 to the end of the feed: then `[oldest read, newest read)` is covered. A read stopped by the
cap or the remaining-low rule proves nothing, because V8's page 2 opens its fourth run 12 s below page 1's
newest item: any page not read can hold items from anywhere in the span. Such a read records the gap from the
cell's accounting frontier to its newest item.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from jobhunter.sources.arbeitnow import FirehoseState, Gap, conclude, covered_span, filled_at, page_cap
from jobhunter.sources.base import RawPage, RequestSpec

T = datetime(2026, 10, 16, tzinfo=UTC)
H = timedelta(hours=1)
D = timedelta(days=1)


def hours_back(start: datetime, count: int) -> list[datetime]:
    """`count` item times one hour apart, newest first, from `start`."""
    return [start - n * H for n in range(count)]


def test_caps() -> None:
    assert (page_cap(3600), page_cap(21600), page_cap(604800)) == (3, 3, 7)
    # min(10, max(3, ceil(interval_h / 30) + 1)), in exact integer arithmetic at the boundaries
    assert (page_cap(60 * 3600), page_cap(60 * 3600 + 1), page_cap(270 * 3600), page_cap(271 * 3600)) == (
        3,
        4,
        10,
        10,
    )
    with pytest.raises(ValueError):
        page_cap(-1)


def test_never_covered_cell_reads_to_the_cap() -> None:
    # T10-a (was test_never_covered_cell_reads_page_one_only): no page bounds the feed's creation times (V8),
    # so a never-covered cell reads on after page 1 like every other run, and the cap stop proves no coverage.
    state = FirehoseState(newest_covered_upper=None, gaps=())
    read = hours_back(T, 10) + hours_back(T - 30 * H, 20)  # pages 2 and 3, older than anything on page 1
    capped, gaps = conclude(state, read, pages=3, cap=3, stopped_by="cap", now=T, max_ad_age_days=30)
    assert gaps == [Gap(T - 49 * H, T, "open")] and capped == FirehoseState(
        None, (Gap(T - 49 * H, T, "open"),)
    )
    assert covered_span(read, stopped_by="cap") is None
    # the same pages ending the feed: everything read is covered, a never-covered cell claims nothing older
    ended, gaps = conclude(state, read, pages=3, cap=3, stopped_by="end", now=T, max_ad_age_days=30)
    assert gaps == [] and ended == FirehoseState(newest_covered_upper=T, gaps=())
    assert covered_span(read, stopped_by="end") == (T - 49 * H, T)


def test_stop_at_cap_records_the_gap_and_a_later_end_closes_it() -> None:
    # T10-a (was ..._and_a_later_seek_closes_it): the cap-stopped read proves nothing, so the gap runs from
    # the watermark to the newest item read and `newest_covered_upper` stays; a later read that reaches the
    # end of the feed closes it (no page bounds a gap, so nothing seeks it).
    state = FirehoseState(newest_covered_upper=T - 40 * H, gaps=())
    read = hours_back(T, 30)  # 30 h of items, newest first
    state2, gaps = conclude(state, read, pages=3, cap=3, stopped_by="cap", now=T, max_ad_age_days=30)
    assert (
        gaps == [Gap(lower=T - 40 * H, upper=T, status="open")] and state2.newest_covered_upper == T - 40 * H
    )
    head = hours_back(T + H, 45)  # T+1h back to T-43h: page 2 is the last page of the feed
    state3, gaps3 = conclude(state2, head, pages=2, cap=3, stopped_by="end", now=T + H, max_ad_age_days=30)
    assert [g.status for g in state3.gaps] == ["closed"] and state3.newest_covered_upper == T + H
    assert gaps3 == [Gap(T - 40 * H, T, "closed")]


def test_a_read_that_does_not_reach_the_end_never_closes_a_gap() -> None:
    gap = Gap(T - 40 * H, T, "open")
    state = FirehoseState(newest_covered_upper=T - 40 * H, gaps=(gap,))
    head = [T + H, T + H / 2]
    read = head + hours_back(T, 45)  # every hour of the gap and more, then the cap
    state2, gaps = conclude(state, read, pages=3, cap=3, stopped_by="cap", now=T + H, max_ad_age_days=30)
    assert covered_span(read, stopped_by="cap") is None
    # the old gap stays open; the newest item read moved the frontier, the span up to it is the new gap
    assert gaps == [Gap(T, T + H, "open")]
    assert state2 == FirehoseState(T - 40 * H, (gap, Gap(T, T + H, "open")))
    # the remaining-low stop proves no more than the cap
    state3, _ = conclude(state, head, pages=1, cap=3, stopped_by="remaining", now=T + H, max_ad_age_days=30)
    assert state3 == FirehoseState(T - 40 * H, (gap, Gap(T, T + H, "open")))


def test_a_read_ending_above_the_gap_floor_narrows_the_gap_and_never_drops_it() -> None:
    # T10-a (was test_seek_cut_short_narrows_the_gap_and_never_drops_it): narrowing comes from a read that
    # reached the end of the feed above the gap's lower bound; the expected rows are the brief's.
    gap = Gap(T - 40 * H, T - 29 * H, "open")
    state = FirehoseState(newest_covered_upper=T, gaps=(gap,))
    read = hours_back(T, 34)  # down to T-33h, then the end of the feed: the older items left it
    state2, gaps = conclude(state, read, pages=2, cap=3, stopped_by="end", now=T, max_ad_age_days=30)
    assert [(g.lower, g.upper, g.status) for g in gaps] == [
        (gap.lower, gap.upper, "closed"),
        (gap.lower, T - 33 * H, "open"),
    ]
    assert state2 == FirehoseState(T, tuple(gaps))
    # a gap reaching above the newest item read (a stale page 1) keeps its upper part open too
    high = Gap(T - 5 * H, T + 2 * H, "open")
    stale = FirehoseState(T + 2 * H, (high,))
    _, gaps = conclude(stale, read, pages=1, cap=3, stopped_by="end", now=T + 3 * H, max_ad_age_days=30)
    assert gaps == [Gap(high.lower, high.upper, "closed"), Gap(T, T + 2 * H, "open")]


def test_the_end_of_a_feed_that_dropped_unread_items_records_what_fell_off() -> None:
    state = FirehoseState(newest_covered_upper=T - 50 * H, gaps=())
    read = hours_back(T, 31)  # the feed ends at T-30h: [T-50h, T-30h) was never read and is gone
    state2, gaps = conclude(state, read, pages=1, cap=3, stopped_by="end", now=T, max_ad_age_days=30)
    assert gaps == [Gap(T - 50 * H, T - 30 * H, "open")] and state2.newest_covered_upper == T
    assert covered_span(read, stopped_by="end") == (T - 30 * H, T)


def test_old_gap_is_abandoned_not_skipped() -> None:
    state = FirehoseState(newest_covered_upper=T, gaps=(Gap(T - 40 * 24 * H, T - 31 * 24 * H, "open"),))
    # T10-a: stopped_by "end" (the brief's "overlap" stop no longer exists)
    _, gaps = conclude(state, [T], pages=1, cap=3, stopped_by="end", now=T, max_ad_age_days=30)
    assert gaps[0].status == "abandoned"


def test_a_new_gap_older_than_the_ad_age_is_recorded_abandoned() -> None:
    state = FirehoseState(newest_covered_upper=T - 40 * D, gaps=())
    read = [T, T - 35 * D]
    _, gaps = conclude(state, read, pages=1, cap=3, stopped_by="end", now=T, max_ad_age_days=30)
    assert gaps == [Gap(T - 40 * D, T - 35 * D, "abandoned")]


def test_an_empty_feed_proves_nothing_and_opens_nothing() -> None:
    gap = Gap(T - 40 * H, T - 29 * H, "open")
    state = FirehoseState(newest_covered_upper=T - 2 * H, gaps=(gap,))
    assert conclude(state, [], pages=1, cap=3, stopped_by="end", now=T, max_ad_age_days=30) == (
        state,
        [],
    )
    assert covered_span([], stopped_by="end") is None
    # all items of one second: the end proves no span of positive length, nothing is claimed
    lone = conclude(state, [T, T], pages=1, cap=3, stopped_by="end", now=T, max_ad_age_days=30)
    assert lone == (FirehoseState(T - 2 * H, (gap, Gap(T - 2 * H, T, "open"))), [Gap(T - 2 * H, T, "open")])


def test_conclude_refuses_a_run_the_traversal_cannot_produce() -> None:
    state = FirehoseState(T, ())
    kw: dict[str, Any] = {"now": T, "max_ad_age_days": 30}
    with pytest.raises(ValueError, match="cap"):
        conclude(state, [T], pages=4, cap=3, stopped_by="end", **kw)
    with pytest.raises(ValueError, match="cap"):
        conclude(state, [T], pages=2, cap=3, stopped_by="cap", **kw)
    with pytest.raises(ValueError, match="page"):
        conclude(state, [T], pages=0, cap=3, stopped_by="end", **kw)


def test_gaps_are_well_formed() -> None:
    with pytest.raises(ValueError):
        Gap(T, T, "open")
    with pytest.raises(ValueError):
        Gap(T, T - H, "open")


# ---- fix wave F4 (ledger L137): no coverage past the earliest page's fill instant --------------------------


def page_with(headers: dict[str, str], fetched_at: datetime) -> RawPage:
    spec = RequestSpec(
        source_instance_id=1,
        operation="list",
        cell_id=1,
        page_no=1,
        params={},
        cursor_in=None,
        detail_native_id=None,
        parent_fetch_request_id=None,
        window=None,
        request_class="free",
    )
    return RawPage(spec, 200, headers, b"{}", fetched_at)


def test_a_page_was_made_at_its_date_less_its_age() -> None:
    date = "Sun, 18 Oct 2026 20:53:22 GMT"  # V8 phase 1: a Cloudflare hit 1440 s old
    assert filled_at(page_with({"date": date, "age": "1440"}, T)) == datetime(
        2026, 10, 18, 20, 29, 22, tzinfo=UTC
    )
    assert filled_at(page_with({"date": date}, T)) == datetime(2026, 10, 18, 20, 53, 22, tzinfo=UTC)
    assert filled_at(page_with({"age": "60"}, T)) == T - timedelta(seconds=60)  # no Date: its arrival
    assert filled_at(page_with({"age": "soon", "date": "garbage"}, T)) == T  # unreadable: the arrival, age 0


def test_a_read_to_the_end_covers_nothing_newer_than_its_earliest_page_fill() -> None:
    read = hours_back(T, 11)  # T - 10 h .. T
    assert covered_span(read, stopped_by="end", filled_at=T - 2 * H) == (T - 10 * H, T - 2 * H)
    assert covered_span(read, stopped_by="end", filled_at=T + H) == (T - 10 * H, T)  # made after: no clamp
    assert covered_span(read, stopped_by="end", filled_at=T - 10 * H) is None  # made before every item read
    assert covered_span(read, stopped_by="cap", filled_at=T + H) is None
    # a covered cell: the read part newer than the fill is a gap, so everything read is still accounted for
    state = FirehoseState(newest_covered_upper=T - 20 * H, gaps=())
    after, gaps = conclude(
        state, read, pages=1, cap=3, stopped_by="end", now=T, max_ad_age_days=30, filled_at=T - 2 * H
    )
    assert gaps == [Gap(T - 20 * H, T - 10 * H, "open"), Gap(T - 2 * H, T, "open")]
    assert after == FirehoseState(T - 2 * H, tuple(gaps))
    # the fill below the old frontier: only the part above the frontier is a new gap
    covered_late = FirehoseState(newest_covered_upper=T - H, gaps=())
    _, gaps = conclude(
        covered_late, read, pages=1, cap=3, stopped_by="end", now=T, max_ad_age_days=30, filled_at=T - 2 * H
    )
    assert gaps == [Gap(T - H, T, "open")]
    # a fill before every item read proves nothing: the read is one gap from the frontier, as a capped read
    _, gaps = conclude(
        state, read, pages=1, cap=3, stopped_by="end", now=T, max_ad_age_days=30, filled_at=T - 11 * H
    )
    assert gaps == [Gap(T - 20 * H, T, "open")]
