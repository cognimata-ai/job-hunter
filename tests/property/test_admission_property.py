"""The admission allocation over random cycles (spec §18.2 L5697-5698, §9.6.2; plan Task 12, Codex #11, R3-3,
Deviation 24): `admission.allocate`, `select` and `debit` as `admit` chains them, cycle after cycle.

- **Fair rotation.** For every `S` from 1 to 30, with a permanent backlog in every lane and a permanent
  always-include backlog, every window of 10 consecutive cycles gives the similarity lane (always-include
  included), the oldest lane and the exploration lane exactly 7, 2 and 1 slots per unit of `S` (at `S = 1`:
  7, 2 and 1), always-include takes `min(similarity slots, S // 2)` and never a safeguard slot.
- **Changing conditions.** Over random sequences of cycles in which `S` changes (1 to 30) and each lane's
  candidates vary independently (overlapping lanes, empty ones, Score now targets): the slots sum to exactly
  `S` before selection, the admissions outside Score now never exceed `S`, the spill is never asked for a
  negative count (no lane takes more than its slots, the slot-backed takes never exceed `S`), always-include
  stays within `S // 2` and the similarity slots, credits stay within `[-1, share x S + 1]`, and afterwards a
  permanent backlog at any `S` returns to exact 7 / 2 / 1 windows.
- **Bounded reads.** `select` picks the same, follow-ups' lanes included, from similarity, oldest and
  exploration lists cut to their first `len(score_now) + S` ids as from the whole lists (always-include
  whole, as `candidates` reads it), so `admit` reads no more of them than that.

Every datum is generated.
"""

from collections.abc import Sequence
from fractions import Fraction
from itertools import count

from hypothesis import given, settings
from hypothesis import strategies as st
from jobhunter.scoring.admission import (
    LANES,
    SHARE,
    AlwaysIncludeReason,
    CandidateSets,
    CreditedLane,
    Selection,
    allocate,
    debit,
    select,
)

WARM_UP = 20  # cycles at a new S before the windows are exact again (14 at most in simulation)
WINDOW = 10
EXACT = {"similarity": 7, "oldest": 2, "exploration": 1}
ZERO: dict[CreditedLane, Fraction] = {lane: Fraction(0) for lane, _ in LANES}
REASONS: tuple[AlwaysIncludeReason, ...] = ("watchlist", "title_match", "full_text_twin")
# The invariants count always-include through its lane, so the cycles there have no follow-up (a follow-up is
# lane `batch_followup` whichever slot it takes); the bounded reads have them.
WITH_FOLLOWUPS: tuple[AlwaysIncludeReason, ...] = (*REASONS, "batch_followup")
# One cycle: S, the similarity, oldest, exploration and always-include candidates, and the Score now targets.
type Cycle = tuple[int, list[int], list[int], list[int], list[tuple[int, AlwaysIncludeReason]], list[int]]


def backlog(ids: "count[int]", s: int) -> CandidateSets:
    """A permanent backlog in every lane and in always-include: `2 S` fresh ids each, disjoint."""
    return CandidateSets(
        [next(ids) for _ in range(2 * s)],
        [next(ids) for _ in range(2 * s)],
        [next(ids) for _ in range(2 * s)],
        [(next(ids), "watchlist") for _ in range(2 * s)],
    )


def backlog_cycles(
    credits: dict[CreditedLane, Fraction], s: int, n: int
) -> tuple[list[dict[CreditedLane, int]], dict[CreditedLane, Fraction]]:
    """`n` cycles at `S = s` with a permanent backlog: what each lane took, and the credits after."""
    ids = count(1)
    taken: list[dict[CreditedLane, int]] = []
    for _ in range(n):
        allocation = allocate(credits, s)
        sel = select(backlog(ids, s), allocation.slots, s, score_now=[])
        assert sel.taken == allocation.slots  # a backlog fills every slot by itself, nothing spills
        always = sum(p.lane == "always_include" for p in sel.picked)
        assert always == min(allocation.slots["similarity"], s // 2)
        taken.append(sel.taken)
        credits = debit(allocation.credits, sel.taken)
    return taken, credits


def assert_exact_windows(taken: Sequence[dict[CreditedLane, int]], s: int) -> None:
    for start in range(len(taken) - WINDOW + 1):
        window = taken[start : start + WINDOW]
        totals = {lane: sum(t[lane] for t in window) for lane, _ in LANES}
        assert totals == {lane: share * s for lane, share in EXACT.items()}, (start, totals)


@settings(deadline=None)
@given(st.integers(1, 30))
def test_every_window_of_ten_cycles_gives_7_2_1_per_unit_of_s(s: int) -> None:
    taken, _ = backlog_cycles(dict(ZERO), s, 40)
    assert_exact_windows(taken, s)


lane_ids = st.lists(st.integers(1, 80), unique=True, max_size=45)


def cycles_of(reasons: tuple[AlwaysIncludeReason, ...]) -> st.SearchStrategy[list[Cycle]]:
    return st.lists(
        st.tuples(
            st.integers(1, 30),  # S
            lane_ids,  # similarity
            lane_ids,  # oldest
            lane_ids,  # exploration
            st.lists(
                st.tuples(st.integers(1, 80), st.sampled_from(reasons)),
                unique_by=lambda pair: pair[0],
                max_size=45,
            ),
            st.lists(st.integers(81, 90), unique=True, max_size=4),  # Score now
        ),
        min_size=1,
        max_size=40,
    )


def check_cycle(sel: Selection, slots: dict[CreditedLane, int], s: int, sets: CandidateSets) -> None:
    """The invariants of one cycle's selection (module docstring)."""
    assert sum(p.lane != "score_now" for p in sel.picked) <= s
    assert all(0 <= sel.taken[lane] <= slots[lane] for lane, _ in LANES)
    assert sum(sel.taken.values()) <= s  # so the spill's ask, S - taken, is never negative
    assert all(n >= 0 for n in sel.spilled.values())
    always = sum(p.lane == "always_include" for p in sel.picked)
    assert always <= min(s // 2, slots["similarity"])
    ids = [p.target_id for p in sel.picked]
    assert len(ids) == len(set(ids))
    # never a safeguard slot: a safeguard lane fills its slots from whatever of its candidates are left
    safeguards: tuple[tuple[CreditedLane, list[int]], ...] = (
        ("oldest", sets.oldest),
        ("exploration", sets.exploration),
    )
    for lane, listed in safeguards:
        if len(listed) >= len(ids) + slots[lane]:
            assert sel.taken[lane] == slots[lane]


@settings(max_examples=300, deadline=None)
@given(cycles_of(REASONS), st.integers(1, 30))
def test_slots_sum_to_s_and_credits_stay_bounded_under_changing_conditions(
    sequence: list[Cycle], final: int
) -> None:
    credits = dict(ZERO)
    for s, similarity, oldest, exploration, always, score_now in sequence:
        allocation = allocate(credits, s)
        assert sum(allocation.slots.values()) == s
        sets = CandidateSets(similarity, oldest, exploration, always)
        sel = select(sets, allocation.slots, s, score_now=score_now)
        check_cycle(sel, allocation.slots, s, sets)
        credits = debit(allocation.credits, sel.taken)
        for lane, share in SHARE.items():
            assert -1 <= allocation.credits[lane] <= share * s + 1
            assert -1 <= credits[lane] <= share * s + 1
    taken, after = backlog_cycles(credits, final, WARM_UP + 30)
    assert_exact_windows(taken[WARM_UP:], final)
    for lane, share in SHARE.items():
        assert -1 <= after[lane] <= share * final + 1


@settings(max_examples=300, deadline=None)
@given(cycles_of(WITH_FOLLOWUPS))
def test_select_reads_no_more_than_score_now_plus_s_ids_of_a_lane(sequence: list[Cycle]) -> None:
    credits = dict(ZERO)
    for s, similarity, oldest, exploration, always, score_now in sequence:
        allocation = allocate(credits, s)
        bound = len(score_now) + s
        whole = CandidateSets(similarity, oldest, exploration, always)
        cut = CandidateSets(similarity[:bound], oldest[:bound], exploration[:bound], always)
        sel = select(whole, allocation.slots, s, score_now=score_now)
        assert select(cut, allocation.slots, s, score_now=score_now) == sel
        credits = debit(allocation.credits, sel.taken)
