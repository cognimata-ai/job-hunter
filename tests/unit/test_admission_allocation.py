"""The lane allocation and the selection of a score cycle (spec §9.6.2 L3605-3651; plan Task 12, Codex #11,
R3-3, r4 B12, Deviation 24, Controller carry T11-d), pure: `admission.allocate`, `select` and `debit`.

The two worked examples of the spec (Codex R3-3's trim, Codex #11's spill); the tie rules (a trim takes
similarity first, a leftover slot goes to a safeguard first); the accrual cap; always-include drawing on the
similarity slots only and at most `S // 2` of them (Deviation 24: the floor, so `S = 1` admits none); the
spill by actual remaining capacity in the order oldest, exploration, similarity, at no credit; Score now
first and outside `S`; one pick per target across the lanes; and a batch follow-up keeping lane
`batch_followup` whichever slot it takes (T11-d).

Every datum is invented.
"""

from decimal import Decimal
from fractions import Fraction as F

import pytest
from jobhunter.scoring import admission, cycles
from jobhunter.scoring.admission import (
    ALWAYS_INCLUDE_LIMIT,
    LANES,
    SHARE,
    TIE_ORDER,
    CandidateSets,
    CreditedLane,
    Picked,
    allocate,
    debit,
    select,
)

ZERO: dict[CreditedLane, F] = {"similarity": F(0), "oldest": F(0), "exploration": F(0)}


def test_the_lane_constants_are_the_spec_values() -> None:
    assert LANES == (
        ("similarity", Decimal("0.70")),
        ("oldest", Decimal("0.20")),
        ("exploration", Decimal("0.10")),
    )
    assert SHARE == {"similarity": F(7, 10), "oldest": F(1, 5), "exploration": F(1, 10)}
    assert TIE_ORDER == {"oldest": 0, "exploration": 1, "similarity": 2}
    assert ALWAYS_INCLUDE_LIMIT(10, {"similarity": 7, "oldest": 2, "exploration": 1}) == 5
    assert ALWAYS_INCLUDE_LIMIT(10, {"similarity": 3, "oldest": 4, "exploration": 3}) == 3
    assert ALWAYS_INCLUDE_LIMIT(1, {"similarity": 1, "oldest": 0, "exploration": 0}) == 0


def test_codex_r3_3_trim_example() -> None:  # L3658
    c1 = allocate({"similarity": F(0), "oldest": F(0), "exploration": F(0)}, 10)
    assert c1.slots == {"similarity": 7, "oldest": 2, "exploration": 1}
    sel = select(CandidateSets([], list(range(100, 109)), [1, 2, 3], []), c1.slots, 10, score_now=[])
    assert (sel.taken, sel.spilled["oldest"], len(sel.picked)) == (
        {"similarity": 0, "oldest": 2, "exploration": 1},
        7,
        10,
    )  # Codex #11
    after = debit(c1.credits, sel.taken)  # 7 / 0 / 0
    assert after == {"similarity": 7, "oldest": 0, "exploration": 0}
    c2 = allocate(after, 10)  # 8 / 2 / 1 -> trimmed to 7 / 2 / 1
    assert c2.credits == {"similarity": 8, "oldest": 2, "exploration": 1}
    assert c2.slots == {"similarity": 7, "oldest": 2, "exploration": 1}


def test_codex_11_spill_example_admits_ten_and_only_the_slot_backed_takes_cost_credit() -> None:
    sel = select(
        CandidateSets([], list(range(100, 109)), [1, 2, 3], []),
        {"similarity": 7, "oldest": 2, "exploration": 1},
        10,
        score_now=[],
    )
    assert [p.target_id for p in sel.picked] == [100, 101, 1, *range(102, 109)]
    assert [p.lane for p in sel.picked] == ["oldest", "oldest", "exploration", *["oldest"] * 7]
    assert sel.spilled == {"oldest": 7, "exploration": 0, "similarity": 0}
    assert sum(sel.taken.values()) == 3


def test_always_include_never_exceeds_half_of_s_and_never_a_safeguard_slot() -> None:
    sims, oldest, expl = list(range(100, 120)), list(range(200, 220)), list(range(300, 320))
    sel = select(
        CandidateSets(sims, oldest, expl, [(t, "watchlist") for t in range(50)]),
        {"similarity": 7, "oldest": 2, "exploration": 1},
        10,
        score_now=[],
    )
    assert (
        sum(p.lane == "always_include" for p in sel.picked) == 5
        and sel.taken["oldest"] == 2
        and sel.taken["exploration"] == 1
    )
    assert [p for p in sel.picked if p.lane == "always_include"] == [
        Picked(t, "always_include", "watchlist") for t in range(5)
    ]
    assert [p.target_id for p in sel.picked if p.lane == "similarity"] == [100, 101]
    assert sel.taken == {"similarity": 7, "oldest": 2, "exploration": 1}
    assert sel.always_included == 5
    assert (
        select(
            CandidateSets([], [], [], [(1, "watchlist")]),
            {"similarity": 1, "oldest": 0, "exploration": 0},
            1,
            score_now=[],
        ).picked
        == []
    )  # S = 1: half is 0


def test_always_include_is_bounded_by_the_similarity_slots_too() -> None:
    sel = select(
        CandidateSets([], list(range(200, 210)), [], [(t, "title_match") for t in range(10)]),
        {"similarity": 2, "oldest": 8, "exploration": 0},
        10,
        score_now=[],
    )
    assert [p.target_id for p in sel.picked if p.lane == "always_include"] == [0, 1]
    assert sel.taken == {"similarity": 2, "oldest": 8, "exploration": 0}


def test_a_trim_takes_similarity_first_on_a_tie() -> None:
    # after accrual 8 / 3 / 0: one slot too many, similarity and oldest both 1 above their share
    allocation = allocate({"similarity": F(1), "oldest": F(1), "exploration": F(-1)}, 10)
    assert allocation.credits == {"similarity": 8, "oldest": 3, "exploration": 0}
    assert allocation.slots == {"similarity": 7, "oldest": 3, "exploration": 0}


def test_a_leftover_slot_goes_to_the_largest_remainder_and_to_a_safeguard_on_a_tie() -> None:
    assert allocate(ZERO, 1).slots == {"similarity": 1, "oldest": 0, "exploration": 0}  # 0.7 / 0.2 / 0.1
    # 3.5 / 1.0 / 0.5: similarity and exploration tie at 0.5, exploration wins
    assert allocate(ZERO, 5).slots == {"similarity": 3, "oldest": 1, "exploration": 1}
    # 2.8 / 0.8 / 0.4: two leftovers, similarity and oldest tie at 0.8
    assert allocate(ZERO, 4).slots == {"similarity": 3, "oldest": 1, "exploration": 0}
    assert allocate(ZERO, 2).slots == {"similarity": 1, "oldest": 1, "exploration": 0}


def test_accrual_is_capped_at_share_times_s_plus_one() -> None:
    allocation = allocate({"similarity": F(50), "oldest": F(-1), "exploration": F(5)}, 10)
    assert allocation.credits == {"similarity": 8, "oldest": 1, "exploration": 2}
    # 8 / 1 / 2 is one slot too many: similarity and exploration both 1 above their share, similarity trims
    assert allocation.slots == {"similarity": 7, "oldest": 1, "exploration": 2}
    assert allocate({"similarity": F(1, 3), "oldest": F(0), "exploration": F(0)}, 10).credits == {
        "similarity": F(22, 3),
        "oldest": 2,
        "exploration": 1,
    }


def test_a_cycle_of_no_slots_admits_only_score_now() -> None:
    allocation = allocate({"similarity": F(7), "oldest": F(2), "exploration": F(1, 2)}, 0)
    assert allocation.slots == {"similarity": 0, "oldest": 0, "exploration": 0}
    sel = select(CandidateSets([1], [1, 2], [2], [(3, "watchlist")]), allocation.slots, 0, score_now=[9])
    assert sel.picked == [Picked(9, "score_now")]


def test_allocate_refuses_a_negative_size() -> None:
    with pytest.raises(ValueError, match="S"):
        allocate(ZERO, -1)


def test_the_spill_follows_the_actual_remaining_capacity_in_order_oldest_exploration_similarity() -> None:
    sel = select(
        CandidateSets([100, 101, 102, 103], [200, 201, 202], [300, 301, 302], []),
        {"similarity": 2, "oldest": 2, "exploration": 6},
        10,
        score_now=[],
    )
    # by slot: similarity 2, oldest 2, exploration 3 of 6; the 3 spare go to oldest (1), then similarity (2)
    assert sel.taken == {"similarity": 2, "oldest": 2, "exploration": 3}
    assert sel.spilled == {"oldest": 1, "exploration": 0, "similarity": 2}
    assert [(p.target_id, p.lane) for p in sel.picked] == [
        (100, "similarity"),
        (101, "similarity"),
        (200, "oldest"),
        (201, "oldest"),
        (300, "exploration"),
        (301, "exploration"),
        (302, "exploration"),
        (202, "oldest"),
        (102, "similarity"),
        (103, "similarity"),
    ]
    # the spill is free: only the slot-backed takes are debited, a short lane keeps its credit
    assert debit({"similarity": F(2), "oldest": F(2), "exploration": F(6)}, sel.taken) == {
        "similarity": 0,
        "oldest": 0,
        "exploration": 3,
    }


def test_score_now_comes_first_outside_s_and_a_target_is_picked_once_across_the_lanes() -> None:
    sel = select(
        CandidateSets([5, 1, 2], [6, 5, 1, 3], [3, 2, 4], [(6, "watchlist"), (1, "title_match")]),
        {"similarity": 3, "oldest": 1, "exploration": 1},
        5,
        score_now=[6, 7],
    )
    assert sel.picked[:2] == [Picked(6, "score_now"), Picked(7, "score_now")]
    ids = [p.target_id for p in sel.picked]
    assert len(ids) == len(set(ids))
    assert sum(p.lane != "score_now" for p in sel.picked) == 5
    assert sel.picked[2:] == [
        Picked(1, "always_include", "title_match"),
        Picked(5, "similarity"),
        Picked(2, "similarity"),
        Picked(3, "oldest"),
        Picked(4, "exploration"),
    ]


def test_a_batch_followup_keeps_its_lane_whichever_slot_it_takes() -> None:
    # Controller carry T11-d: a follow-up admitted by always-include, or by a lane past the always-include
    # limit, is lane `batch_followup` (reason `batch_followup`), so later rounds and reports still see it;
    # the slot it took is still that lane's, for the credit
    sel = select(
        CandidateSets([], [8, 9, 10], [], [(8, "batch_followup"), (9, "batch_followup"), (11, "watchlist")]),
        {"similarity": 1, "oldest": 2, "exploration": 0},
        3,
        score_now=[],
    )
    assert sel.picked == [
        Picked(8, "batch_followup", "batch_followup"),
        Picked(9, "batch_followup", "batch_followup"),
        Picked(10, "oldest"),
    ]
    assert sel.taken == {"similarity": 1, "oldest": 2, "exploration": 0}
    assert sel.always_included == 1  # the follow-up of the always-include phase, counted against S // 2


def test_a_followup_known_from_the_lane_lists_keeps_its_lane() -> None:
    # `candidates` cuts always-include short, so a follow-up deep in it is known from the lane lists' flags
    sel = select(
        CandidateSets([], [9, 10], [], [(4, "watchlist")], followups=frozenset({9})),
        {"similarity": 1, "oldest": 1, "exploration": 0},
        2,
        score_now=[],
    )
    assert sel.picked == [
        Picked(4, "always_include", "watchlist"),
        Picked(9, "batch_followup", "batch_followup"),
    ]
    assert (sel.taken, sel.always_included) == ({"similarity": 1, "oldest": 1, "exploration": 0}, 1)


def test_the_cycle_owner_is_one_constant() -> None:
    assert cycles.CYCLE_OWNER is admission.CYCLE_OWNER == "score_cycle"
