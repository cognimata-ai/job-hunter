"""P-AN1 (spec §5.3.4, §18.1) under ruling T10-a: the Arbeitnow coverage model over a growing feed.

The simulated feed has the page shape spike V8 recorded (docs/spikes/V8.md): every page is four runs of
100, 50, 50 and 100 items, each newest first. Runs 1 to 3 continue exactly from one page to the next. Run 4
does not: its pages are cut from the stream by a key unrelated to `created_at`, so any page can hold items
from anywhere in the span, as V8's page 2 did. The feed grows by `g` items per run, more than `cap * 300` in
at least one run, and an open gap exists from the start. Each run is driven through the pure model
(`plan_legs`, `step`, `conclude`, `covered_span`) the way the traversal drives it, and these properties are
checked:

- every run's first leg is `fresh` from page 1, and every item on the first `cap` pages at run time is read by
  that run (the head is never starved, whatever the growth);
- every gap has `lower < upper`;
- a seek page lands within one page of the first item older than the gap's upper bound, counted in a feed
  ordered by `created_at`;
- an open gap is closed, narrowed or abandoned, and every such change is reported; no gap is ever dropped, and
  no open gap outlives the ad age;
- no span is marked covered, and no gap closed, without every feed item in it having been read;
- every item read lies below the accounting frontier afterwards (it is in a covered span or a recorded gap),
  no time between the old and the new frontier is left out of every covered span and gap, and no two open
  gaps overlap (no span is recorded as a hole twice).
"""

import random
from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise

from hypothesis import example, given, settings
from hypothesis import strategies as st
from jobhunter.sources.arbeitnow import (
    PAGE_SIZE,
    FirehoseState,
    Gap,
    Leg,
    StopReason,
    conclude,
    covered_span,
    page_cap,
    plan_legs,
    seek_page,
    step,
)

T0 = datetime(2026, 10, 1, tzinfo=UTC)
RUNS: tuple[int, ...] = (100, 50, 50, 100)  # V8: the four runs of a page
Span = tuple[datetime, datetime]


@dataclass(frozen=True)
class Item:
    id: int
    ts: datetime
    run: int  # the run of the page it is served in (0..3)
    key: float  # the order run 4 is cut into pages by: not `created_at`


def newest_first(items: Sequence[Item]) -> list[Item]:
    return sorted(items, key=lambda item: (item.ts, item.id), reverse=True)


class Feed:
    """The feed as served at one moment: page `n` is the `n`-th slice of each of the four runs."""

    def __init__(self, items: Sequence[Item], *, now: datetime, retention: timedelta | None) -> None:
        self.items = [i for i in items if i.ts <= now and (retention is None or i.ts > now - retention)]
        self.runs: list[list[Item]] = []
        for run in range(4):
            members = [item for item in self.items if item.run == run]
            self.runs.append(newest_first(members) if run < 3 else sorted(members, key=lambda item: item.key))
        self.last_page = max(-(-len(members) // size) for members, size in zip(self.runs, RUNS, strict=True))

    def page(self, n: int) -> list[Item]:
        served: list[Item] = []
        for members, size in zip(self.runs, RUNS, strict=True):
            served += newest_first(members[(n - 1) * size : n * size])
        return served

    def ends_at(self, n: int) -> bool:
        """Page `n` is the end of the feed: its `data` is empty or it has no `links.next`."""
        return n >= self.last_page


def covers(spans: Sequence[Span], lower: datetime, upper: datetime) -> bool:
    """Whether the union of `spans` (each `[lower, upper)`) contains `[lower, upper)`."""
    reached = lower
    for start, end in sorted(spans):
        if start > reached:
            break
        reached = max(reached, end)
    return reached >= upper


def frontier(state: FirehoseState) -> datetime | None:
    bounds = [g.upper for g in state.gaps]
    if state.newest_covered_upper is not None:
        bounds.append(state.newest_covered_upper)
    return max(bounds, default=None)


# a surge opens gaps, then quiet runs let the read reach the end of a feed whose bottom lies inside an open
# gap: the gap is closed and its older part reopened (narrowing), so that path is exercised on every run
@example(
    seed=963,
    interval_h=24,
    growth=[30, 400, 30, 0],
    surge=0,
    initial=600,
    retention_h=48,
    max_age_days=5,
    low_at=None,
)
@settings(max_examples=60, deadline=None)
@given(
    seed=st.integers(0, 2**32 - 1),
    interval_h=st.sampled_from([1, 6, 24, 72]),
    growth=st.lists(
        st.one_of(st.integers(0, 40), st.integers(0, 1500)), min_size=2, max_size=8
    ),  # quiet runs too
    surge=st.integers(0, 7),
    initial=st.integers(0, 900),
    retention_h=st.one_of(st.none(), st.integers(24, 240)),
    max_age_days=st.integers(1, 5),
    low_at=st.one_of(st.none(), st.tuples(st.integers(0, 7), st.integers(1, 4))),
)
def test_a_growing_feed_never_starves_the_head_and_never_hides_a_gap(
    seed: int,
    interval_h: int,
    growth: list[int],
    surge: int,
    initial: int,
    retention_h: int | None,
    max_age_days: int,
    low_at: tuple[int, int] | None,
) -> None:
    rng = random.Random(seed)  # noqa: S311 - a reproducible simulation, not a secret
    interval, cap = timedelta(hours=interval_h), page_cap(interval_h * 3600)
    retention = None if retention_h is None else timedelta(hours=retention_h)
    growth = list(growth)
    growth[surge % len(growth)] += cap * PAGE_SIZE + 1  # at least one run outgrows the cap

    items: list[Item] = []

    def add(count: int, after: datetime, until: datetime) -> None:
        width = int((until - after).total_seconds())
        for _ in range(count):
            ts = after + timedelta(seconds=rng.randint(1, width))
            items.append(Item(len(items), ts, rng.choices(range(4), weights=RUNS)[0], rng.random()))

    add(initial, T0 - timedelta(hours=60), T0)
    state = FirehoseState(
        T0 - timedelta(hours=3), (Gap(T0 - timedelta(hours=30), T0 - timedelta(hours=10), "open"),)
    )
    seen: set[int] = set()  # the ids of raw_items: every item read so far
    now = T0
    for run_no, grown in enumerate(growth):
        add(grown, now, now + interval)
        now += interval
        feed = Feed(items, now=now, retention=retention)
        # n_newer: distinct ids read so far with item_ts >= the gap's upper bound (Task 14's prefetch)
        seen_times = sorted(i.ts for i in items if i.id in seen)
        n_newer = {
            g: len(seen_times) - bisect_left(seen_times, g.upper) for g in state.gaps if g.status == "open"
        }
        legs = plan_legs(state, n_newer=n_newer, now=now, max_ad_age_days=max_age_days)
        assert legs[0] == Leg("fresh", None, 1)
        feed_times = sorted(i.ts for i in feed.items)
        for leg in legs[1:]:
            assert leg.gap is not None and leg.start_page == seek_page(n_newer[leg.gap])
            # in a feed ordered by created_at, the first item older than the gap's upper bound follows the
            # items newer than it: position `newer`, on page `newer // PAGE_SIZE + 1`
            newer = len(feed_times) - bisect_left(feed_times, leg.gap.upper)
            assert abs(seek_page(newer) - (newer // PAGE_SIZE + 1)) <= 1

        # the traversal: a leg starts only while pages remain; the end of the feed, the cap and the
        # remaining-low stop end it, and nothing else does (T10-a)
        reads: list[tuple[Leg, list[datetime]]] = []
        read: set[int] = set()
        pages = 0
        stop: StopReason | None = None
        for leg in legs:
            if stop is not None:
                break
            page_no, first = leg.start_page, True
            times: list[datetime] = []
            while stop is None:
                if pages == cap:
                    stop = "cap"
                    break
                served = feed.page(page_no)
                pages += 1
                read |= {i.id for i in served}
                times += [i.ts for i in served]
                if low_at == (run_no, pages):
                    stop = "remaining"
                elif feed.ends_at(page_no):
                    stop = "end"
                else:
                    target = None if leg.gap is None else leg.gap.upper
                    decision = step(
                        [i.ts for i in served],
                        leg,
                        state,
                        first_item_older_than_target=first and target is not None and served[0].ts < target,
                        last_item_newer_than_target=first and target is not None and served[-1].ts > target,
                    )
                    first = False
                    assert decision in ("continue", "step_back", "step_forward")  # no leg stops on a time
                    page_no = max(1, page_no - 1) if decision == "step_back" else page_no + 1
            reads.append((leg, times))
        assert stop is not None
        stopped_by: StopReason = stop

        # the head: the run read every page up to the cap, unless the feed ended or the remaining-low rule
        # stopped it first; no page budget is left for a seek leg (T10-a)
        expected = pages if stopped_by == "remaining" else min(cap, max(1, feed.last_page))
        assert pages == expected and len(reads) == 1
        assert {i.id for n in range(1, pages + 1) for i in feed.page(n)} <= read

        after, changed = conclude(
            state, reads, pages=pages, cap=cap, stopped_by=stopped_by, now=now, max_ad_age_days=max_age_days
        )
        span = covered_span(reads, stopped_by=stopped_by)

        # gaps are chronological intervals
        assert all(g.lower < g.upper for g in (*after.gaps, *changed))
        # never dropped, every change reported, no open gap past the ad age
        before = {(g.lower, g.upper): g for g in state.gaps}
        now_ = {(g.lower, g.upper): g for g in after.gaps}
        assert before.keys() <= now_.keys() and set(changed) <= set(after.gaps)
        assert all(g in changed for bounds, g in now_.items() if before.get(bounds) != g)
        cutoff = now - timedelta(days=max_age_days)
        assert not any(g.status == "open" and g.upper <= cutoff for g in after.gaps)
        new = [g for g in changed if (g.lower, g.upper) not in before]
        # coverage only where every feed item was read: the whole feed, from page 1 to its end
        if span is not None:
            assert stopped_by == "end" and all(i.id in read for i in feed.items if span[0] <= i.ts < span[1])
            assert after.newest_covered_upper is not None and after.newest_covered_upper >= span[1]
        else:
            assert after.newest_covered_upper == state.newest_covered_upper
        for bounds, gap in before.items():
            if gap.status == "open" and now_[bounds].status == "closed":
                assert span is not None and span[0] < gap.upper and gap.lower < span[1]
                remainders = [
                    (r.lower, r.upper) for r in new if gap.lower <= r.lower and r.upper <= gap.upper
                ]
                assert covers([span, *remainders], gap.lower, gap.upper)
            elif gap.status == "open":
                assert now_[bounds].status in ("open", "abandoned")
        # the accounting frontier moves past everything read, leaves no hole and never records a hole twice
        old_frontier, new_frontier = frontier(state), frontier(after)
        newest_read = max((moment for _, times in reads for moment in times), default=None)
        assert newest_read is None or (new_frontier is not None and new_frontier >= newest_read)
        holes = sorted((g.lower, g.upper) for g in after.gaps if g.status == "open")
        assert all(earlier[1] <= later[0] for earlier, later in pairwise(holes))
        if old_frontier is not None and new_frontier is not None and new_frontier > old_frontier:
            accounted = [(g.lower, g.upper) for g in new] + ([span] if span else [])
            assert covers(accounted, old_frontier, new_frontier)

        seen |= read
        state = after
