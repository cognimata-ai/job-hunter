"""Admission of a score cycle (spec §9.6, L3595-3662; plan Tasks 11 and 12, Rulings P1, P12, P35, Codex r1
#14, #21, R3-3, r4 B2, B12, Deviation 24, Controller carry T11-d): the lane contract, the lane allocation, the
candidate queries, the persistent lane credits, and the two admission calls of the cycle's `admit` node.

**The one lane contract** (Codex r4 B2). `LANE_VALUES` and `Lane` are the seven lanes of `score_targets.lane`
and `score_attempts.lane`; the two CHECKs of migration 0008 write the same list as one SQL literal
(`LANE_VALUES_SQL`, a migration never imports application code), which `test_lane_values_pin_both_checks`
keeps in step. `Picked.lane`, the claim's `lane = a.lane` and the batch preparation's `'batch'` are typed by
it. `MAX_SCORE_ATTEMPTS` is §7.16.3's `max_score_attempts` (L2863), against `score_targets.charged_attempts`:
the admission, the claim and `fail` count to it.

**The admissible targets** of a tuple, one predicate (`_ADMISSIBLE_WHERE`, over `score_targets t`): the due
(`not_before` passed), unfenced (`NOT is_fenced(job_id, llm_tuple_id)`) `pending` targets with
`charged_attempts < MAX_SCORE_ATTEMPTS` (the exhaustion sweep has moved the others already; the predicate is
kept here and in the claim, so no path admits a fifth attempt: Codex r1 #21). `any_admissible(conn,
llm_tuple_id, *, lane, now)` is the cycle start's gate on it (`cycles.start_cycle`, `cycles.periodic_gate`),
and every candidate query below reads through it.

**The allocation** (§9.6.2, pure, exact `Fraction`s; `Decimal` only at the table): `allocate(credits, S)`
accrues `share x S` to each credited lane (`LANES`), capped at `share x S + 1`; whole credits become slots;
while they sum to more than `S`, the lane with the largest excess over `share x S` loses one (similarity first
on ties, `TIE_ORDER`), its credit carrying; leftover slots go to the largest remainders (the safeguards
first on ties); the slots then sum to exactly `S`. `select(sets, slots, S, *, score_now)` picks the Score now
targets first (unbounded, outside `S`); then always-include from the similarity slots, at most
`ALWAYS_INCLUDE_LIMIT(S, slots) = min(slots["similarity"], S // 2)` (Deviation 24: the floor, the "at most
half of `S`" of the property test L5697); the similarity lane for the rest of its slots; the oldest and
exploration lanes by slot; then the spill of the actual remaining capacity, in the order oldest, exploration,
similarity. Each target is picked once. Only the slot-backed takes are debited (`debit`, `credit -
taken[lane]`), a spill costs nothing, and nothing is ever rejected: the rest stays `pending`. A remainder slot
borrows at most one credit, so the credits stay at or above -1 (the table's CHECK).

**A batch follow-up** (`lane = 'batch_followup'`, Controller carry T11-d) is admitted as lane
`batch_followup` with the reason `batch_followup` whichever slot it takes (always-include's, or a lane's past
the always-include limit), so the claim keeps its lane, later rounds (`admit_urgent`) and the
`batch_followup` trigger still find it after a backoff, and the reports count it as one; the slot it took is
still debited to its lane. The bulk accounting identifies batched targets through `batch_items`, never through
the lane (Deviation 24).

**The candidates** (`candidates(conn, *, llm_tuple_id, cycle_id, profile_version_id, now, limit,
always_limit)`, without a lock: the claim re-checks every condition under its own `FOR UPDATE SKIP LOCKED`),
every lane among the admissible targets in lane order, each cut short: `admit` reads `len(score_now) + S`
ids of the similarity, oldest and exploration lists (`limit`: a lane's takes and the ids picked before it
never reach further down its list) and `len(score_now) + S // 2` of always-include (`always_limit`: the
always-include phase comes right after Score now and takes at most `ALWAYS_INCLUDE_LIMIT <= S // 2`), so the
always-include query walks `score_targets_admission` in similarity order and stops early. The lane queries
flag their follow-ups (`CandidateSets.followups`), so `select` knows a follow-up that the always-include cut
left out:
- `similarity`: `similarity IS NOT NULL`, by similarity descending (`score_targets_admission`), **excluding**
  targets whose revision is `partial` and the non-representative members of an active duplicate group (U14):
  those reach the LLM through the oldest and exploration lanes, or through always-include;
- `oldest`: by `created_at` ascending (`score_targets_oldest`), any similarity;
- `exploration`: by `md5(<cycle id> || ':' || <target id>)` (`COLLATE "C"`), a reproducible random order
  seeded by the cycle;
- `always_include`, highest similarity first and NULL last, with its reason: a batch follow-up
  (`batch_followup`, whatever else it qualifies for); a full-text twin (`full_text_twin`: a `full` job of an
  active group whose representative is `partial`, or the `full` representative of an active group with a
  `partial` member, which §8.7's re-election makes of the twin that joined a partial representative,
  `fuller_member_joined`); a watched company's job (`watchlist`: a posting from the `source_instances` row of
  a watchlist company enabled in `watchlist_companies` and in the active settings' `watchlist[]`, Controller
  Ruling T12-b); a title match (`title_match`: `similarity(jobs.title_norm, t) >= TITLE_MATCH_MIN`
  for a target title `t` of the tuple's confirmed profile, `profile_json -> 'target_titles'`, normalised by
  `normalize.titles.normalize_title`), in that order.
Null-similarity targets (awaiting their embedding) are candidates of oldest, exploration and always-include.

**The lane credits** (`LaneCredits`): `lock(conn, llm_tuple_id)` inserts the tuple's three
`admission_lane_credits` rows at 0 when absent and takes them `FOR UPDATE` (step 10 of the lock schedule);
`credits` are `Fraction`s; `save(conn)` stores them at `numeric(10,4)`, rounded half-even.

**The two calls** (Ruling P12; the `admit` node's transaction, which holds `active_config FOR SHARE` and the
cycle `FOR SHARE` under its owner predicate):
- `admit(conn, cycle, *, now, sweeps)`, a cycle's first round: the pre-lock phase, then the sweeps `expiry`,
  `refilter`, `regroup` and `exhaustion` in that order (injected: `Sweeps`; the node passes `CYCLE_SWEEPS`,
  Task 13's sweeps of `scoring.targets` for the cycle's tuple and scope, the expiry with the active
  `max_ad_age_days`; the pre-lock locks the bulk rows of both hold-releasing sweeps' candidates once, and
  those two sweeps move only the candidates it covered, `held`: Ruling P52, carries T12-c, T13-a); the Score
  now targets (lane `score_now`, id order) and the candidates; `LaneCredits.lock`; `allocate(credits, S)` with
  `S = cycle.admission_size`; `select`; the debited credits saved; `admitted` written; and, when a sweep moved
  a target, `ranking.bump` as the last statement. The picks in admission order.
- `admit_urgent(conn, cycle, *, now, slots_left)`, a later claim round (Codex r1 #14): no sweep, no credit;
  every admissible Score now target (outside `S`), then the admissible follow-ups (lane `batch_followup`,
  reason `batch_followup`) up to `slots_left` (they count against `S`, L3600), ordered as always-include
  orders them; `admitted` gains the round's counts.
Both write `admitted` (`record_admitted`: the cycle's counts plus the round's) under the cycle's owner
predicate, `id`, `worker_epoch` and `status = 'running'`; a cycle that is no longer its owner's raises
`FencedOut(CYCLE_OWNER)` and the caller's transaction writes nothing. The lane and the always-include reason
reach the target through the claim (`lane = a.lane`, `always_include_reason = a.reason`).

Ids, counts and lanes only reach a log line; the profile's titles never do.
"""

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from fractions import Fraction
from math import floor
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal, Protocol, Self, get_args

from sqlalchemy import TextClause
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db import ranking
from jobhunter.db.sql import q
from jobhunter.errors import FencedOut
from jobhunter.normalize.titles import normalize_title
from jobhunter.observability.logging import get_logger
from jobhunter.types import JSONValue

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

# The three lanes with a persistent credit (`admission_lane_credits.lane`), and why an always-include target
# is one (`score_targets.always_include_reason`, L2221-2222).
CreditedLane = Literal["similarity", "oldest", "exploration"]
AlwaysIncludeReason = Literal["watchlist", "title_match", "full_text_twin", "batch_followup"]
ALWAYS_INCLUDE_REASONS: Final[tuple[AlwaysIncludeReason, ...]] = get_args(AlwaysIncludeReason)

LANES: Final[tuple[tuple[CreditedLane, Decimal], ...]] = (
    ("similarity", Decimal("0.70")),
    ("oldest", Decimal("0.20")),
    ("exploration", Decimal("0.10")),
)
SHARE: Final[Mapping[CreditedLane, Fraction]] = MappingProxyType(
    {lane: Fraction(share) for lane, share in LANES}
)
TIE_ORDER: Final[Mapping[CreditedLane, int]] = MappingProxyType(
    {"oldest": 0, "exploration": 1, "similarity": 2}  # L3607: the safeguards win ties
)
SPILL_ORDER: Final[tuple[CreditedLane, ...]] = ("oldest", "exploration", "similarity")
TITLE_MATCH_MIN: Final = 0.6  # pg_trgm `similarity` against a confirmed target title (L3633)
CREDIT_QUANTUM: Final = 10_000  # `numeric(10,4)`
# The owner a lost cycle predicate fences out (`FencedOut(CYCLE_OWNER)`): one piece of work lost. `cycles`
# re-exports it (it imports this module, never the other way round).
CYCLE_OWNER: Final = "score_cycle"

_SCORE_NOW: Final[Lane] = "score_now"
_BATCH_FOLLOWUP: Final[Lane] = "batch_followup"
_ALWAYS_INCLUDE: Final[Lane] = "always_include"

_LANES: Final[dict[str, Lane]] = {lane: lane for lane in LANE_VALUES}
_CREDITED: Final[dict[str, CreditedLane]] = {lane: lane for lane, _ in LANES}
_REASONS: Final[dict[str, AlwaysIncludeReason]] = {reason: reason for reason in ALWAYS_INCLUDE_REASONS}

log = get_logger("jobhunter.scoring.admission")


def lane_of(value: str) -> Lane:
    """`value` as a lane (a stored or checkpointed one); `ValueError` for any other text."""
    lane = _LANES.get(value)
    if lane is None:
        raise ValueError(f"unknown admission lane {value!r}")
    return lane


def _credited_lane(value: str) -> CreditedLane:
    lane = _CREDITED.get(value)
    if lane is None:
        raise ValueError(f"unknown credited lane {value!r}")
    return lane


def _reason(value: str) -> AlwaysIncludeReason:
    reason = _REASONS.get(value)
    if reason is None:
        raise ValueError(f"unknown always-include reason {value!r}")
    return reason


@dataclass(frozen=True)
class Picked:
    """One admitted target: its lane and, for an always-include admission or a batch follow-up, why."""

    target_id: int
    lane: Lane
    always_include_reason: str | None = None


# ---- the allocation (pure) -------------------------------------------------------------------------------


def ALWAYS_INCLUDE_LIMIT(S: int, slots: Mapping[CreditedLane, int]) -> int:
    """How many always-include targets a cycle takes, at most: of the similarity slots, at most half of `S`
    (the floor: Deviation 24)."""
    return min(slots["similarity"], S // 2)


@dataclass(frozen=True)
class Allocation:
    """A cycle's slots per credited lane (summing to `S`), and its credits after the accrual, before the
    debit."""

    slots: dict[CreditedLane, int]
    credits: dict[CreditedLane, Fraction]


def allocate(credits: Mapping[CreditedLane, Fraction], S: int) -> Allocation:
    """§9.6.2's allocation of `S` slots over the credited lanes (module docstring), L3617-3629."""
    if S < 0:
        raise ValueError(f"S must not be negative, got {S}")
    accrued = {lane: min(credits[lane] + SHARE[lane] * S, SHARE[lane] * S + 1) for lane, _ in LANES}
    slots = {lane: max(0, floor(accrued[lane])) for lane, _ in LANES}
    while sum(slots.values()) > S:  # carried whole credits never buy more than S slots
        lane = max(
            (lane for lane in slots if slots[lane] > 0),
            key=lambda lane: (slots[lane] - SHARE[lane] * S, TIE_ORDER[lane]),
        )
        slots[lane] -= 1  # the largest excess (similarity first); its credit carries
    spare = S - sum(slots.values())
    for lane in sorted(slots, key=lambda lane: (-(accrued[lane] - slots[lane]), TIE_ORDER[lane])):
        if spare <= 0:
            break  # the largest remainders get the leftover slots
        slots[lane], spare = slots[lane] + 1, spare - 1
    if sum(slots.values()) != S:
        raise AssertionError(f"the lane allocation gave {sum(slots.values())} slots for S = {S}")
    return Allocation(slots=slots, credits=accrued)


@dataclass(frozen=True)
class CandidateSets:
    """The candidates of each lane in lane order (module docstring); a target may be in several. The
    follow-ups are those of `followups` (the lane lists' follow-ups, which `candidates` flags) and those of
    `always_include` with the reason `batch_followup`."""

    similarity: list[int]
    oldest: list[int]
    exploration: list[int]
    always_include: list[tuple[int, AlwaysIncludeReason]]
    followups: frozenset[int] = frozenset()


@dataclass(frozen=True)
class Selection:
    """A cycle's picks in admission order, the slot-backed takes per lane (the debit), the spill, and how many
    the always-include phase took (follow-ups included: at most `ALWAYS_INCLUDE_LIMIT`)."""

    picked: list[Picked]
    taken: dict[CreditedLane, int]
    spilled: dict[CreditedLane, int]
    always_included: int


def select(
    sets: CandidateSets,
    slots: Mapping[CreditedLane, int],
    S: int,
    *,
    score_now: Sequence[int],
) -> Selection:
    """§9.6.2's selection (module docstring), L3614-3651."""
    followups = sets.followups | {
        target for target, reason in sets.always_include if reason == "batch_followup"
    }
    lists: Mapping[CreditedLane, list[int]] = {
        "similarity": sets.similarity,
        "oldest": sets.oldest,
        "exploration": sets.exploration,
    }
    picked: list[Picked] = []
    chosen: set[int] = set()

    def take(candidates: Iterable[int], limit: int) -> list[int]:
        got: list[int] = []
        for target in candidates:
            if len(got) >= limit:
                break
            if target not in chosen:
                chosen.add(target)
                got.append(target)
        return got

    def admit_as(lane: CreditedLane, targets: Iterable[int]) -> None:
        for target in targets:
            followup = target in followups
            picked.append(
                Picked(target, _BATCH_FOLLOWUP, "batch_followup") if followup else Picked(target, lane)
            )

    picked.extend(Picked(target, _SCORE_NOW) for target in take(score_now, len(score_now)))  # outside S
    reasons = dict(sets.always_include)
    always = take((target for target, _ in sets.always_include), ALWAYS_INCLUDE_LIMIT(S, slots))
    for target in always:
        reason = reasons[target]
        always_lane = _BATCH_FOLLOWUP if reason == "batch_followup" else _ALWAYS_INCLUDE
        picked.append(Picked(target, always_lane, reason))
    similar = take(lists["similarity"], slots["similarity"] - len(always))
    admit_as("similarity", similar)
    taken: dict[CreditedLane, int] = {"similarity": len(always) + len(similar)}
    for lane in ("oldest", "exploration"):
        got = take(lists[lane], slots[lane])
        admit_as(lane, got)
        taken[lane] = len(got)
    remaining = S - sum(taken.values())
    spilled: dict[CreditedLane, int] = dict.fromkeys(SPILL_ORDER, 0)
    for lane in SPILL_ORDER:  # by the ACTUAL remaining capacity; a spill costs no credit
        if remaining <= 0:
            break
        extra = take(lists[lane], remaining)
        admit_as(lane, extra)
        spilled[lane], remaining = len(extra), remaining - len(extra)
    return Selection(picked=picked, taken=taken, spilled=spilled, always_included=len(always))


def debit(
    credits: Mapping[CreditedLane, Fraction], taken: Mapping[CreditedLane, int]
) -> dict[CreditedLane, Fraction]:
    """The credits after a cycle: each lane pays one credit per slot-backed take (module docstring)."""
    return {lane: credits[lane] - taken[lane] for lane, _ in LANES}


# ---- the candidate queries ---------------------------------------------------------------------------------

_ADMISSIBLE_WHERE: Final = (
    "t.llm_tuple_id = :tuple AND t.state = 'pending' AND (t.not_before IS NULL OR t.not_before <= :now) "
    "AND t.charged_attempts < :max_attempts AND NOT is_fenced(t.job_id, t.llm_tuple_id)"
)
_ANY: Final = q(
    f"SELECT EXISTS (SELECT 1 FROM score_targets t WHERE {_ADMISSIBLE_WHERE} "  # noqa: S608 (the constant above)
    "AND (CAST(:lane AS text) IS NULL OR t.lane = :lane))"
)
# A non-representative member of an active duplicate group (a group without a representative included).
_GROUPED_UNDER_ANOTHER: Final = (
    "EXISTS (SELECT 1 FROM duplicate_group_members m JOIN duplicate_groups g ON g.id = m.group_id "
    "WHERE m.job_id = t.job_id AND g.status = 'active' AND g.representative_job_id IS DISTINCT FROM t.job_id)"
)
# A lane list's row: the target, and whether it is a batch follow-up (`select` admits one as lane
# `batch_followup`).
_LANE_ROW: Final = "t.id, t.lane IS NOT DISTINCT FROM 'batch_followup' AS followup"
_SIMILARITY: Final = q(
    f"SELECT {_LANE_ROW} FROM score_targets t JOIN job_revisions r ON r.id = t.job_revision_id "  # noqa: S608
    f"WHERE {_ADMISSIBLE_WHERE} AND t.similarity IS NOT NULL AND r.description_completeness <> 'partial' "
    f"AND NOT {_GROUPED_UNDER_ANOTHER} ORDER BY t.similarity DESC NULLS LAST, t.id LIMIT :limit"
)
_OLDEST: Final = q(
    f"SELECT {_LANE_ROW} FROM score_targets t WHERE {_ADMISSIBLE_WHERE} "  # noqa: S608
    "ORDER BY t.created_at, t.id LIMIT :limit"
)
_EXPLORATION: Final = q(
    f"SELECT {_LANE_ROW} FROM score_targets t WHERE {_ADMISSIBLE_WHERE} "  # noqa: S608
    "ORDER BY md5(CAST(:cycle AS text) || ':' || CAST(t.id AS text)) COLLATE \"C\", t.id LIMIT :limit"
)
_FULL_TEXT_TWIN: Final = (
    "j.completeness = 'full' AND EXISTS (SELECT 1 FROM duplicate_group_members m "
    "JOIN duplicate_groups g ON g.id = m.group_id LEFT JOIN jobs rj ON rj.id = g.representative_job_id "
    "WHERE m.job_id = t.job_id AND g.status = 'active' AND (rj.completeness = 'partial' "
    "OR (g.representative_job_id = t.job_id AND EXISTS (SELECT 1 FROM duplicate_group_members o "
    "JOIN jobs oj ON oj.id = o.job_id WHERE o.group_id = g.id AND o.job_id <> t.job_id "
    "AND oj.completeness = 'partial'))))"
)
_WATCHLIST: Final = (
    "EXISTS (SELECT 1 FROM postings p JOIN source_instances s ON s.id = p.source_instance_id "
    "JOIN watchlist_companies w ON w.id = s.watchlist_company_id WHERE p.job_id = t.job_id AND w.enabled "
    "AND w.id = ANY(CAST(:watched AS bigint[])))"
)
_TITLE_MATCH: Final = (
    "EXISTS (SELECT 1 FROM unnest(CAST(:titles AS text[])) AS x(title) "
    "WHERE similarity(j.title_norm, x.title) >= :title_min)"
)
_ALWAYS: Final = q(
    "SELECT c.id, c.reason FROM (SELECT t.id, t.similarity, CASE "  # noqa: S608 (the constants above)
    f"WHEN t.lane = 'batch_followup' THEN 'batch_followup' WHEN {_FULL_TEXT_TWIN} THEN 'full_text_twin' "
    f"WHEN {_WATCHLIST} THEN 'watchlist' WHEN {_TITLE_MATCH} THEN 'title_match' END AS reason "
    f"FROM score_targets t JOIN jobs j ON j.id = t.job_id WHERE {_ADMISSIBLE_WHERE}) c "
    "WHERE c.reason IS NOT NULL ORDER BY c.similarity DESC NULLS LAST, c.id LIMIT :limit"
)
_OF_LANE: Final = q(
    f"SELECT t.id FROM score_targets t WHERE {_ADMISSIBLE_WHERE} AND t.lane = :lane "  # noqa: S608
    "ORDER BY t.similarity DESC NULLS LAST, t.id LIMIT :limit"
)
_SCORE_NOW_TARGETS: Final = q(
    f"SELECT t.id FROM score_targets t WHERE {_ADMISSIBLE_WHERE} AND t.lane = 'score_now' "  # noqa: S608
    "ORDER BY t.id"
)
_TARGET_TITLES: Final = q(
    "SELECT profile_json -> 'target_titles' AS titles FROM profile_versions WHERE id = :pv"
)
_PROFILE_VERSION: Final = q("SELECT profile_version_id FROM llm_tuples WHERE id = :tuple")
_WATCHLIST_SETTINGS: Final = q(
    "SELECT v.payload -> 'watchlist' FROM active_config a JOIN settings_versions v "
    "ON v.id = a.settings_version_id WHERE a.id = 1"
)


def _admissible(llm_tuple_id: int, now: datetime) -> dict[str, object]:
    return {"tuple": llm_tuple_id, "now": now, "max_attempts": MAX_SCORE_ATTEMPTS}


async def any_admissible(
    conn: AsyncConnection, llm_tuple_id: int, *, lane: Lane | None, now: datetime
) -> bool:
    """Whether the tuple has an admissible target (of `lane`, when given) at `now` (module docstring)."""
    params = {**_admissible(llm_tuple_id, now), "lane": lane}
    return bool((await conn.execute(_ANY, params)).scalar_one())


async def target_titles(conn: AsyncConnection, profile_version_id: int) -> list[str]:
    """The confirmed profile's target titles, normalised (`title_norm`), once each; none for a purged one."""
    stored: JSONValue = (await conn.execute(_TARGET_TITLES, {"pv": profile_version_id})).scalar_one_or_none()
    titles: list[str] = []
    for item in stored if isinstance(stored, list) else []:
        title = item.get("title") if isinstance(item, dict) else None
        if isinstance(title, str):
            norm = normalize_title(title).title_norm
            if norm and norm not in titles:
                titles.append(norm)
    return titles


async def watched_companies(conn: AsyncConnection) -> list[int]:
    """The watchlist companies the active settings enable (`watchlist[].enabled`, Controller Ruling
    T12-b); the query also asks `watchlist_companies.enabled`."""
    stored: JSONValue = (await conn.execute(_WATCHLIST_SETTINGS)).scalar_one_or_none()
    watched: list[int] = []
    for entry in stored if isinstance(stored, list) else []:
        if not isinstance(entry, dict) or entry.get("enabled") is not True:
            continue
        company = entry.get("watchlist_company_id")
        if isinstance(company, int):
            watched.append(company)
    return watched


async def _ids(conn: AsyncConnection, statement: TextClause, params: Mapping[str, object]) -> list[int]:
    return [int(row.id) for row in await conn.execute(statement, params)]


async def _lane(
    conn: AsyncConnection, statement: TextClause, params: Mapping[str, object], followups: set[int]
) -> list[int]:
    """A lane list's ids, its follow-ups added to `followups`."""
    ids: list[int] = []
    for row in await conn.execute(statement, params):
        ids.append(int(row.id))
        if row.followup:
            followups.add(int(row.id))
    return ids


async def candidates(
    conn: AsyncConnection,
    *,
    llm_tuple_id: int,
    cycle_id: int,
    profile_version_id: int,
    now: datetime,
    limit: int | None = None,
    always_limit: int | None = None,
) -> CandidateSets:
    """The candidates of every lane (module docstring): the similarity, oldest and exploration lists at most
    `limit` long, always-include at most `always_limit` (`None`: whole), and the lane lists' follow-ups."""
    params = {**_admissible(llm_tuple_id, now), "limit": limit}
    always_params = {
        **_admissible(llm_tuple_id, now),
        "limit": always_limit,
        "titles": await target_titles(conn, profile_version_id),
        "title_min": TITLE_MATCH_MIN,
        "watched": await watched_companies(conn),
    }
    always = await conn.execute(_ALWAYS, always_params)
    followups: set[int] = set()
    return CandidateSets(
        similarity=await _lane(conn, _SIMILARITY, params, followups),
        oldest=await _lane(conn, _OLDEST, params, followups),
        exploration=await _lane(conn, _EXPLORATION, {**params, "cycle": cycle_id}, followups),
        always_include=[(int(row.id), _reason(str(row.reason))) for row in always],
        followups=frozenset(followups),
    )


# ---- the lane credits ------------------------------------------------------------------------------------

_SEED_CREDITS: Final = q(
    "INSERT INTO admission_lane_credits (llm_tuple_id, lane) "
    "SELECT :tuple, lane FROM unnest(CAST(:lanes AS text[])) AS l(lane) ON CONFLICT DO NOTHING"
)
_LOCK_CREDITS: Final = q(
    "SELECT lane, credit FROM admission_lane_credits WHERE llm_tuple_id = :tuple ORDER BY lane FOR UPDATE"
)
_SAVE_CREDITS: Final = q(
    "UPDATE admission_lane_credits c SET credit = v.credit "
    "FROM unnest(CAST(:lanes AS text[]), CAST(:credits AS numeric[])) AS v(lane, credit) "
    "WHERE c.llm_tuple_id = :tuple AND c.lane = v.lane"
)


def stored_credit(credit: Fraction) -> Decimal:
    """`credit` as `admission_lane_credits.credit` stores it: `numeric(10,4)`, rounded half-even."""
    return Decimal(round(credit * CREDIT_QUANTUM)).scaleb(-4)


@dataclass
class LaneCredits:
    """A tuple's persistent lane credits, locked (module docstring)."""

    llm_tuple_id: int
    credits: dict[CreditedLane, Fraction]

    @classmethod
    async def lock(cls, conn: AsyncConnection, llm_tuple_id: int) -> Self:
        """The tuple's three rows, inserted at 0 when absent, `FOR UPDATE`."""
        lanes = [lane for lane, _ in LANES]
        await conn.execute(_SEED_CREDITS, {"tuple": llm_tuple_id, "lanes": lanes})
        rows = await conn.execute(_LOCK_CREDITS, {"tuple": llm_tuple_id})
        credits = {_credited_lane(str(row.lane)): Fraction(Decimal(row.credit)) for row in rows}
        return cls(llm_tuple_id=llm_tuple_id, credits=credits)

    async def save(self, conn: AsyncConnection) -> None:
        """Store `credits` (`stored_credit`); a credit below -1 is refused by the table's CHECK."""
        lanes = [lane for lane, _ in LANES]
        params = {
            "tuple": self.llm_tuple_id,
            "lanes": lanes,
            "credits": [stored_credit(self.credits[lane]) for lane in lanes],
        }
        await conn.execute(_SAVE_CREDITS, params)


# ---- the two calls ---------------------------------------------------------------------------------------


class Sweep(Protocol):
    """One of the sweeps `admit` runs first (Task 13's), in the caller's transaction: the targets it moved.
    `held` are the targets whose bulk rows the round's pre-lock locked (`Sweeps.prelock`)."""

    async def __call__(
        self, conn: AsyncConnection, cycle: "CycleRow", /, *, now: datetime, held: frozenset[int]
    ) -> int: ...


class PreLock(Protocol):
    """The pre-lock phase `admit` runs before its sweeps: the targets whose bulk rows it locked."""

    async def __call__(
        self, conn: AsyncConnection, cycle: "CycleRow", /, *, now: datetime
    ) -> frozenset[int]: ...


async def _locks_nothing(conn: AsyncConnection, cycle: "CycleRow", /, *, now: datetime) -> frozenset[int]:
    return frozenset()


@dataclass(frozen=True)
class Sweeps:
    """The sweeps of a cycle's first round, run in this order (L3610-3612, and the exhaustion sweep), after
    the pre-lock phase (`prelock`, none by default)."""

    expiry: Sweep
    refilter: Sweep
    regroup: Sweep
    exhaustion: Sweep
    prelock: PreLock = _locks_nothing


# The adapters of `CYCLE_SWEEPS` onto `targets`' sweeps (Task 13), for the cycle's tuple and scope, in the
# `admit` node's transaction (`active_config FOR SHARE` held, so the active `max_ad_age_days` is read without
# a lock). `targets` imports this module (`MAX_SCORE_ATTEMPTS`), so each adapter imports it when it runs.


async def _prelock(conn: AsyncConnection, cycle: "CycleRow", /, *, now: datetime) -> frozenset[int]:
    """The bulk rows of the expiry and exhaustion candidates' follow-up holds, locked once, in id order,
    before the first target row any of the four sweeps locks (Ruling P52, Controller carries T12-c and
    T13-a: the exhaustion sweep runs fourth); the candidates it covered, which those two sweeps then move
    and no other."""
    from jobhunter.scoring import targets

    max_age = await targets.active_max_ad_age_days(conn)
    return await targets.lock_hold_candidates(
        conn, llm_tuple_id=cycle.llm_tuple_id, max_ad_age_days=max_age, now=now
    )


async def _expiry(conn: AsyncConnection, cycle: "CycleRow", /, *, now: datetime, held: frozenset[int]) -> int:
    from jobhunter.scoring import targets

    max_age = await targets.active_max_ad_age_days(conn)
    swept = await targets.expiry_sweep(
        conn, llm_tuple_id=cycle.llm_tuple_id, max_ad_age_days=max_age, now=now, held=held
    )
    return swept.moved


async def _refilter(
    conn: AsyncConnection, cycle: "CycleRow", /, *, now: datetime, held: frozenset[int]
) -> int:
    from jobhunter.scoring import targets

    swept = await targets.refilter_sweep(
        conn, llm_tuple_id=cycle.llm_tuple_id, scope_version_id=cycle.scope_version_id
    )
    return swept.moved


async def _regroup(
    conn: AsyncConnection, cycle: "CycleRow", /, *, now: datetime, held: frozenset[int]
) -> int:
    from jobhunter.scoring import targets

    return (await targets.regroup_sweep(conn, llm_tuple_id=cycle.llm_tuple_id)).moved


async def _exhaustion(
    conn: AsyncConnection, cycle: "CycleRow", /, *, now: datetime, held: frozenset[int]
) -> int:
    from jobhunter.scoring import targets

    return await targets.exhaustion_sweep(conn, llm_tuple_id=cycle.llm_tuple_id, held=held)


# The sweeps the `admit` node runs (Task 13's, L3610-3612 and Deviation 7).
CYCLE_SWEEPS: Final = Sweeps(
    expiry=_expiry, refilter=_refilter, regroup=_regroup, exhaustion=_exhaustion, prelock=_prelock
)

_ADMITTED: Final = q(
    "UPDATE score_cycles SET admitted = :admitted WHERE id = :c AND worker_epoch = :e AND status = 'running'",
    jsonb=["admitted"],
)


async def record_admitted(
    conn: AsyncConnection, cycle: "CycleRow", picked: Sequence[Picked]
) -> dict[str, int]:
    """Add a round's admissions per lane to the cycle's `admitted` under its owner predicate; the sum.
    `FencedOut` when the cycle is no longer this owner's running one."""
    total = dict(cycle.admitted)
    for lane, n in Counter(p.lane for p in picked).items():
        total[lane] = total.get(lane, 0) + n
    written = await conn.execute(_ADMITTED, {"admitted": total, "c": cycle.id, "e": cycle.worker_epoch})
    if written.rowcount != 1:
        raise FencedOut(CYCLE_OWNER)
    return total


async def admit(conn: AsyncConnection, cycle: "CycleRow", *, now: datetime, sweeps: Sweeps) -> list[Picked]:
    """A cycle's first round of admission (module docstring); the picks in admission order."""
    held = await sweeps.prelock(conn, cycle, now=now)  # step 7, before the sweeps' first target row (P52)
    moved = {
        "expiry": await sweeps.expiry(conn, cycle, now=now, held=held),
        "refilter": await sweeps.refilter(conn, cycle, now=now, held=held),
        "regroup": await sweeps.regroup(conn, cycle, now=now, held=held),
        "exhaustion": await sweeps.exhaustion(conn, cycle, now=now, held=held),
    }
    size = cycle.admission_size
    score_now = await _ids(conn, _SCORE_NOW_TARGETS, _admissible(cycle.llm_tuple_id, now))
    profile_version = (await conn.execute(_PROFILE_VERSION, {"tuple": cycle.llm_tuple_id})).scalar_one()
    sets = await candidates(
        conn,
        llm_tuple_id=cycle.llm_tuple_id,
        cycle_id=cycle.id,
        profile_version_id=int(profile_version),
        now=now,
        limit=len(score_now) + size,
        always_limit=len(score_now) + size // 2,  # ALWAYS_INCLUDE_LIMIT is at most S // 2, whatever the slots
    )
    credits = await LaneCredits.lock(conn, cycle.llm_tuple_id)  # step 10
    allocation = allocate(credits.credits, size)
    selection = select(sets, allocation.slots, size, score_now=score_now)
    credits.credits = debit(allocation.credits, selection.taken)
    await credits.save(conn)
    total = await record_admitted(conn, cycle, selection.picked)
    if any(moved.values()):
        await ranking.bump(conn)  # step 11, last
    log.info(
        "score_admission_allocated",
        cycle_id=cycle.id,
        admission_size=size,
        swept=moved,
        slots=allocation.slots,
        taken=selection.taken,
        spilled=selection.spilled,
        admitted=total,
    )
    return selection.picked


async def admit_urgent(
    conn: AsyncConnection, cycle: "CycleRow", *, now: datetime, slots_left: int
) -> list[Picked]:
    """A later round's admission (module docstring): every Score now target, then up to `slots_left`
    follow-ups."""
    params = _admissible(cycle.llm_tuple_id, now)
    picked = [Picked(target, _SCORE_NOW) for target in await _ids(conn, _SCORE_NOW_TARGETS, params)]
    if slots_left > 0:
        followups = await _ids(conn, _OF_LANE, {**params, "lane": _BATCH_FOLLOWUP, "limit": slots_left})
        picked.extend(Picked(target, _BATCH_FOLLOWUP, "batch_followup") for target in followups)
    if picked:
        await record_admitted(conn, cycle, picked)
    return picked
