"""Admission of a score cycle (spec §9.6, L3605-3660; plan Task 11, Ruling P12, Codex r4 B2): the lane
contract, what an admission picks, the attempt limit, and the two admission calls of the cycle's `admit` node.

**The one lane contract** (Codex r4 B2). `LANE_VALUES` and `Lane` are the seven lanes of `score_targets.lane`
and `score_attempts.lane`; the two CHECKs of migration 0008 write the same list as one SQL literal
(`LANE_VALUES_SQL`, a migration never imports application code), which Task 12's
`test_lane_values_pin_both_checks` keeps in step. `Picked.lane`, the claim's `lane = a.lane` and the batch
preparation's `'batch'` are typed by it.

`MAX_SCORE_ATTEMPTS` is §7.16.3's `max_score_attempts` (L2863), against `score_targets.charged_attempts`: the
admission, the claim and `fail` count to it.

**The admissible targets** of a tuple, one predicate (`_ADMISSIBLE_WHERE`): the due (`not_before` passed),
unfenced (`NOT is_fenced(job_id, llm_tuple_id)`) `pending` targets with `charged_attempts <
MAX_SCORE_ATTEMPTS`. `any_admissible(conn, llm_tuple_id, *, lane, now)` is the cycle start's gate on it
(`cycles.start_cycle`, `cycles.periodic_gate`), so a target no round could admit (fenced, or its attempts
spent) starts no cycle.

**The two calls, stubs until Task 12** (Ruling P12; Task 12 replaces both bodies, adds the lane allocation of
§9.6.2 behind `admit` and gives it its `sweeps` argument):
- `admit(conn, cycle, *, now)`, a cycle's first round: the admissible targets of the cycle's tuple; those of
  lane `score_now` first (lane `score_now`, outside `S`, as everywhere), then the others in id order as lane
  `similarity`, at most the cycle's `admission_size` (`S`) of them;
- `admit_urgent(conn, cycle, *, now, slots_left)`, a later claim round: the admissible targets of lane
  `score_now` (outside `S`), then those of lane `batch_followup` (lane `batch_followup`) up to `slots_left`,
  the slots of `S` the earlier rounds left.
All three read without a lock: the claim re-checks every condition under its own `FOR UPDATE SKIP LOCKED`.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Final, Literal

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q

if TYPE_CHECKING:
    # Only for the annotations: `cycles` imports this module (its sweeps count to `MAX_SCORE_ATTEMPTS`).
    from jobhunter.scoring.cycles import CycleRow

Lane = Literal[
    "similarity", "oldest", "exploration", "always_include", "score_now", "batch_followup", "batch"
]
LANE_VALUES: Final[tuple[Lane, ...]] = (
    "similarity",
    "oldest",
    "exploration",
    "always_include",
    "score_now",
    "batch_followup",
    "batch",
)
MAX_SCORE_ATTEMPTS: Final = 4  # L2863, `max_score_attempts`, against `charged_attempts`


_LANES: Final[dict[str, Lane]] = {lane: lane for lane in LANE_VALUES}


def lane_of(value: str) -> Lane:
    """`value` as a lane (a stored or checkpointed one); `ValueError` for any other text."""
    lane = _LANES.get(value)
    if lane is None:
        raise ValueError(f"unknown admission lane {value!r}")
    return lane


@dataclass(frozen=True)
class Picked:
    """One admitted target: its lane and, for an always-include admission, why."""

    target_id: int
    lane: Lane
    always_include_reason: str | None = None


# The admissible targets of a tuple (module docstring), over `score_targets t`: whether one exists (of
# `:lane`, when not NULL); the first round's, Score now first, then by id; and those of one lane by id, at
# most `:limit` (NULL: all).
_ADMISSIBLE_WHERE: Final = (
    "t.llm_tuple_id = :tuple AND t.state = 'pending' AND (t.not_before IS NULL OR t.not_before <= :now) "
    "AND t.charged_attempts < :max_attempts AND NOT is_fenced(t.job_id, t.llm_tuple_id)"
)
_ANY: Final = q(
    f"SELECT EXISTS (SELECT 1 FROM score_targets t WHERE {_ADMISSIBLE_WHERE} "  # noqa: S608 (the constant above)
    "AND (CAST(:lane AS text) IS NULL OR t.lane = :lane))"
)
_ADMISSIBLE: Final = f"SELECT t.id, t.lane FROM score_targets t WHERE {_ADMISSIBLE_WHERE}"  # noqa: S608
_FIRST_ROUND: Final = q(f"{_ADMISSIBLE} ORDER BY (t.lane IS NOT DISTINCT FROM 'score_now') DESC, t.id")
_OF_LANE: Final = q(f"{_ADMISSIBLE} AND t.lane = :lane ORDER BY t.id LIMIT :limit")
_SCORE_NOW: Final[Lane] = "score_now"
_BATCH_FOLLOWUP: Final[Lane] = "batch_followup"


async def any_admissible(
    conn: AsyncConnection, llm_tuple_id: int, *, lane: Lane | None, now: datetime
) -> bool:
    """Whether the tuple has an admissible target (of `lane`, when given) at `now` (module docstring)."""
    params = {"tuple": llm_tuple_id, "now": now, "max_attempts": MAX_SCORE_ATTEMPTS, "lane": lane}
    return bool((await conn.execute(_ANY, params)).scalar_one())


async def admit(conn: AsyncConnection, cycle: "CycleRow", *, now: datetime) -> list[Picked]:
    """The first round's admission (module docstring; Task 12 replaces the body)."""
    params = {"tuple": cycle.llm_tuple_id, "now": now, "max_attempts": MAX_SCORE_ATTEMPTS}
    picked: list[Picked] = []
    others = 0
    for row in await conn.execute(_FIRST_ROUND, params):
        if row.lane == _SCORE_NOW:
            picked.append(Picked(int(row.id), _SCORE_NOW))
        elif others < cycle.admission_size:
            picked.append(Picked(int(row.id), "similarity"))
            others += 1
        else:
            break
    return picked


async def admit_urgent(
    conn: AsyncConnection, cycle: "CycleRow", *, now: datetime, slots_left: int
) -> list[Picked]:
    """A later round's admission (module docstring; Task 12 replaces the body)."""
    params = {"tuple": cycle.llm_tuple_id, "now": now, "max_attempts": MAX_SCORE_ATTEMPTS}
    rows = await conn.execute(_OF_LANE, {**params, "lane": _SCORE_NOW, "limit": None})
    picked = [Picked(int(row.id), _SCORE_NOW) for row in rows]
    if slots_left > 0:
        rows = await conn.execute(_OF_LANE, {**params, "lane": _BATCH_FOLLOWUP, "limit": slots_left})
        picked.extend(Picked(int(row.id), _BATCH_FOLLOWUP) for row in rows)
    return picked
