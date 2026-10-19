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
from jobhunter.sources.arbeitnow import (
    FirehoseState,
    Gap,
    Leg,
    conclude,
    covered_span,
    page_cap,
    plan_legs,
    seek_page,
    step,
)

T = datetime(2026, 10, 16, tzinfo=UTC)
H = timedelta(hours=1)
D = timedelta(days=1)
FRESH = Leg("fresh", None, 1)


def hours_back(start: datetime, count: int) -> list[datetime]:
    """`count` item times one hour apart, newest first, from `start`."""
    return [start - n * H for n in range(count)]


def test_caps_and_seek() -> None:
    assert (page_cap(3600), page_cap(21600), page_cap(604800)) == (3, 3, 7)
    assert (seek_page(0), seek_page(299), seek_page(650)) == (1, 1, 2)
    # min(10, max(3, ceil(interval_h / 30) + 1)), in exact integer arithmetic at the boundaries
    assert (page_cap(60 * 3600), page_cap(60 * 3600 + 1), page_cap(270 * 3600), page_cap(271 * 3600)) == (
        3,
        4,
        10,
        10,
    )
    assert (seek_page(300), seek_page(599), seek_page(600)) == (1, 1, 2)
    with pytest.raises(ValueError):
        page_cap(-1)
    with pytest.raises(ValueError):
        seek_page(-1)


def test_fresh_head_is_always_the_first_leg() -> None:
    state = FirehoseState(newest_covered_upper=T - 2 * H, gaps=(Gap(T - 40 * H, T - 29 * H, "open"),))
    legs = plan_legs(state, n_newer={state.gaps[0]: 900}, now=T, max_ad_age_days=30)
    assert legs[0] == Leg("fresh", None, 1) and legs[1] == Leg("seek", state.gaps[0], 3)


def test_seek_legs_follow_the_head_newest_gap_first_and_only_for_open_gaps_within_the_ad_age() -> None:
    old, mid, new = (
        Gap(T - 50 * H, T - 45 * H, "open"),
        Gap(T - 30 * H, T - 20 * H, "open"),
        Gap(T - 9 * H, T - H, "open"),
    )
    done, dropped = Gap(T - 15 * H, T - 12 * H, "closed"), Gap(T - 60 * H, T - 55 * H, "abandoned")
    beyond = Gap(T - 40 * D, T - 31 * D, "open")
    state = FirehoseState(newest_covered_upper=None, gaps=(old, done, new, beyond, dropped, mid))
    legs = plan_legs(state, n_newer={old: 1200, mid: 600, new: 10}, now=T, max_ad_age_days=30)
    assert legs == [FRESH, Leg("seek", new, 1), Leg("seek", mid, 2), Leg("seek", old, 4)]


def test_never_covered_cell_reads_to_the_cap() -> None:
    # T10-a (was test_never_covered_cell_reads_page_one_only): no page bounds the feed's creation times (V8),
    # so a never-covered cell reads on after page 1 like every other run, and the cap stop proves no coverage.
    state = FirehoseState(newest_covered_upper=None, gaps=())
    assert plan_legs(state, n_newer={}, now=T, max_ad_age_days=30) == [FRESH]
    page1 = hours_back(T, 10)
    assert step(
        page1, FRESH, state, first_item_older_than_target=False, last_item_newer_than_target=False
    ) == ("continue")
    read = page1 + hours_back(T - 30 * H, 20)  # pages 2 and 3, older than anything on page 1
    capped, gaps = conclude(
        state, [(FRESH, read)], pages=3, cap=3, stopped_by="cap", now=T, max_ad_age_days=30
    )
    assert gaps == [Gap(T - 49 * H, T, "open")] and capped == FirehoseState(
        None, (Gap(T - 49 * H, T, "open"),)
    )
    assert covered_span([(FRESH, read)], stopped_by="cap") is None
    # the same pages ending the feed: everything read is covered, a never-covered cell claims nothing older
    ended, gaps = conclude(
        state, [(FRESH, read)], pages=3, cap=3, stopped_by="end", now=T, max_ad_age_days=30
    )
    assert gaps == [] and ended == FirehoseState(newest_covered_upper=T, gaps=())
    assert covered_span([(FRESH, read)], stopped_by="end") == (T - 49 * H, T)


def test_the_fresh_leg_reads_past_the_overlap() -> None:
    # T10-a: a page wholly older than `newest_covered_upper - 2 h` does not end the leg; the next page can
    # hold newer items (V8: page 2's fourth run opens 12 s below page 1's newest item).
    state = FirehoseState(newest_covered_upper=T - 2 * H, gaps=())
    old_page = hours_back(T - 10 * H, 30)
    assert step(
        old_page, FRESH, state, first_item_older_than_target=True, last_item_newer_than_target=True
    ) == ("continue")
    assert step([], FRESH, state, first_item_older_than_target=False, last_item_newer_than_target=False) == (
        "continue"
    )


def test_a_seek_leg_positions_itself_by_the_flags_and_never_stops_on_a_time() -> None:
    gap = Gap(T - 40 * H, T - 29 * H, "open")
    state, seek = FirehoseState(T, (gap,)), Leg("seek", gap, 2)
    page = hours_back(T - 20 * H, 30)  # it passes gap.lower: no time ends a leg under T10-a
    kw = {"first_item_older_than_target": True, "last_item_newer_than_target": False}
    assert step(page, seek, state, **kw) == "step_back"
    kw = {"first_item_older_than_target": False, "last_item_newer_than_target": True}
    assert step(page, seek, state, **kw) == "step_forward"
    kw = {"first_item_older_than_target": False, "last_item_newer_than_target": False}
    assert step(page, seek, state, **kw) == "continue"


def test_stop_at_cap_records_the_gap_and_a_later_end_closes_it() -> None:
    # T10-a (was ..._and_a_later_seek_closes_it): the cap-stopped read proves nothing, so the gap runs from
    # the watermark to the newest item read and `newest_covered_upper` stays; a seek leg cannot close it (no
    # page bounds a gap), a later read that reaches the end of the feed does.
    state = FirehoseState(newest_covered_upper=T - 40 * H, gaps=())
    read = hours_back(T, 30)  # 30 h of items, newest first
    state2, gaps = conclude(
        state, [(FRESH, read)], pages=3, cap=3, stopped_by="cap", now=T, max_ad_age_days=30
    )
    assert (
        gaps == [Gap(lower=T - 40 * H, upper=T, status="open")] and state2.newest_covered_upper == T - 40 * H
    )
    legs = plan_legs(state2, n_newer={g: 30 for g in state2.gaps}, now=T + H, max_ad_age_days=30)
    assert [leg.kind for leg in legs] == ["fresh", "seek"]
    head = hours_back(T + H, 45)  # T+1h back to T-43h: page 2 is the last page of the feed
    state3, gaps3 = conclude(
        state2, [(legs[0], head)], pages=2, cap=3, stopped_by="end", now=T + H, max_ad_age_days=30
    )
    assert [g.status for g in state3.gaps] == ["closed"] and state3.newest_covered_upper == T + H
    assert gaps3 == [Gap(T - 40 * H, T, "closed")]


def test_a_read_that_does_not_reach_the_end_never_closes_a_gap() -> None:
    gap = Gap(T - 40 * H, T, "open")
    state = FirehoseState(newest_covered_upper=T - 40 * H, gaps=(gap,))
    legs = plan_legs(state, n_newer={gap: 30}, now=T + H, max_ad_age_days=30)
    head, seek = [T + H, T + H / 2], hours_back(T, 45)  # the seek leg reads every hour of the gap and more
    reads = [(legs[0], head), (legs[1], seek)]
    state2, gaps = conclude(state, reads, pages=3, cap=3, stopped_by="cap", now=T + H, max_ad_age_days=30)
    assert covered_span(reads, stopped_by="cap") is None
    # a seek leg means the fresh leg did not reach the end (the end ends the traversal): nothing is proven
    assert covered_span(reads, stopped_by="end") is None
    # the old gap stays open; the newest item read moved the frontier, the span up to it is the new gap
    assert gaps == [Gap(T, T + H, "open")]
    assert state2 == FirehoseState(T - 40 * H, (gap, Gap(T, T + H, "open")))
    # the remaining-low stop proves no more than the cap
    state3, _ = conclude(
        state, [(FRESH, head)], pages=1, cap=3, stopped_by="remaining", now=T + H, max_ad_age_days=30
    )
    assert state3 == FirehoseState(T - 40 * H, (gap, Gap(T, T + H, "open")))


def test_a_seek_leg_cut_short_proves_nothing_and_never_drops_the_gap() -> None:
    # T10-a (with the brief's inputs of test_seek_cut_short_narrows_the_gap_and_never_drops_it): the seek
    # leg's pages do not bound the gap, so the gap is neither closed nor narrowed, and it stays in the state.
    gap = Gap(T - 40 * H, T - 29 * H, "open")
    state = FirehoseState(newest_covered_upper=T, gaps=(gap,))
    legs = plan_legs(state, n_newer={gap: 300}, now=T, max_ad_age_days=30)
    state2, gaps = conclude(
        state,
        [(legs[0], [T]), (legs[1], hours_back(T - 29 * H, 5))],
        pages=3,
        cap=3,
        stopped_by="cap",
        now=T,
        max_ad_age_days=30,
    )
    assert gaps == [] and state2 == state


def test_a_read_ending_above_the_gap_floor_narrows_the_gap_and_never_drops_it() -> None:
    # T10-a (was test_seek_cut_short_narrows_the_gap_and_never_drops_it): narrowing comes from a read that
    # reached the end of the feed above the gap's lower bound; the expected rows are the brief's.
    gap = Gap(T - 40 * H, T - 29 * H, "open")
    state = FirehoseState(newest_covered_upper=T, gaps=(gap,))
    read = hours_back(T, 34)  # down to T-33h, then the end of the feed: the older items left it
    state2, gaps = conclude(
        state, [(FRESH, read)], pages=2, cap=3, stopped_by="end", now=T, max_ad_age_days=30
    )
    assert [(g.lower, g.upper, g.status) for g in gaps] == [
        (gap.lower, gap.upper, "closed"),
        (gap.lower, T - 33 * H, "open"),
    ]
    assert state2 == FirehoseState(T, tuple(gaps))
    # a gap reaching above the newest item read (a stale page 1) keeps its upper part open too
    high = Gap(T - 5 * H, T + 2 * H, "open")
    stale = FirehoseState(T + 2 * H, (high,))
    _, gaps = conclude(
        stale, [(FRESH, read)], pages=1, cap=3, stopped_by="end", now=T + 3 * H, max_ad_age_days=30
    )
    assert gaps == [Gap(high.lower, high.upper, "closed"), Gap(T, T + 2 * H, "open")]


def test_the_end_of_a_feed_that_dropped_unread_items_records_what_fell_off() -> None:
    state = FirehoseState(newest_covered_upper=T - 50 * H, gaps=())
    read = hours_back(T, 31)  # the feed ends at T-30h: [T-50h, T-30h) was never read and is gone
    state2, gaps = conclude(
        state, [(FRESH, read)], pages=1, cap=3, stopped_by="end", now=T, max_ad_age_days=30
    )
    assert gaps == [Gap(T - 50 * H, T - 30 * H, "open")] and state2.newest_covered_upper == T
    assert covered_span([(FRESH, read)], stopped_by="end") == (T - 30 * H, T)


def test_old_gap_is_abandoned_not_skipped() -> None:
    state = FirehoseState(newest_covered_upper=T, gaps=(Gap(T - 40 * 24 * H, T - 31 * 24 * H, "open"),))
    legs = plan_legs(state, n_newer={g: 0 for g in state.gaps}, now=T, max_ad_age_days=30)
    assert [leg.kind for leg in legs] == ["fresh"]
    # T10-a: stopped_by "end" (the brief's "overlap" stop no longer exists)
    _, gaps = conclude(state, [(legs[0], [T])], pages=1, cap=3, stopped_by="end", now=T, max_ad_age_days=30)
    assert gaps[0].status == "abandoned"


def test_a_new_gap_older_than_the_ad_age_is_recorded_abandoned() -> None:
    state = FirehoseState(newest_covered_upper=T - 40 * D, gaps=())
    read = [T, T - 35 * D]
    _, gaps = conclude(state, [(FRESH, read)], pages=1, cap=3, stopped_by="end", now=T, max_ad_age_days=30)
    assert gaps == [Gap(T - 40 * D, T - 35 * D, "abandoned")]


def test_an_empty_feed_proves_nothing_and_opens_nothing() -> None:
    gap = Gap(T - 40 * H, T - 29 * H, "open")
    state = FirehoseState(newest_covered_upper=T - 2 * H, gaps=(gap,))
    assert conclude(state, [(FRESH, [])], pages=1, cap=3, stopped_by="end", now=T, max_ad_age_days=30) == (
        state,
        [],
    )
    assert covered_span([(FRESH, [])], stopped_by="end") is None
    # all items of one second: the end proves no span of positive length, nothing is claimed
    lone = conclude(state, [(FRESH, [T, T])], pages=1, cap=3, stopped_by="end", now=T, max_ad_age_days=30)
    assert lone == (FirehoseState(T - 2 * H, (gap, Gap(T - 2 * H, T, "open"))), [Gap(T - 2 * H, T, "open")])


def test_conclude_refuses_a_run_the_traversal_cannot_produce() -> None:
    state, seek = FirehoseState(T, ()), Leg("seek", Gap(T - 2 * H, T - H, "open"), 1)
    kw: dict[str, Any] = {"now": T, "max_ad_age_days": 30}
    with pytest.raises(ValueError, match="fresh"):
        conclude(state, [], pages=1, cap=3, stopped_by="end", **kw)
    with pytest.raises(ValueError, match="fresh"):
        conclude(state, [(seek, [T])], pages=1, cap=3, stopped_by="end", **kw)
    with pytest.raises(ValueError, match="fresh"):
        conclude(state, [(FRESH, [T]), (FRESH, [T])], pages=2, cap=3, stopped_by="end", **kw)
    with pytest.raises(ValueError, match="cap"):
        conclude(state, [(FRESH, [T])], pages=4, cap=3, stopped_by="end", **kw)
    with pytest.raises(ValueError, match="cap"):
        conclude(state, [(FRESH, [T])], pages=2, cap=3, stopped_by="cap", **kw)
    with pytest.raises(ValueError, match="page"):
        conclude(state, [(FRESH, [T])], pages=0, cap=3, stopped_by="end", **kw)


def test_gaps_and_legs_are_well_formed() -> None:
    with pytest.raises(ValueError):
        Gap(T, T, "open")
    with pytest.raises(ValueError):
        Gap(T, T - H, "open")
    with pytest.raises(ValueError):
        Leg("fresh", None, 2)
    with pytest.raises(ValueError):
        Leg("fresh", Gap(T - H, T, "open"), 1)
    with pytest.raises(ValueError):
        Leg("seek", None, 1)
    with pytest.raises(ValueError):
        Leg("seek", Gap(T - H, T, "open"), 0)
