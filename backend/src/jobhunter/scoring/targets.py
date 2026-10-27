"""Score targets (spec §6.9.4, §7.11, §7.16.3, §8.7, §9.4, §9.6.3, §9.6.4; plan Tasks 9 and 13, Deviations 7
and 24, Codex r1 #19-#21, #36, r4 B8, r5 B4, Rulings P8, P9, P28, P47, P52, Controller rulings and carries
T10-f, T11-c, T12-a, T12-c): the SQL that more than one module of the score lane shares, the sweeps that move
targets without a user, the moves a user asks for, and `budget_release`. Task 14 adds creation and revival.

`RESUMABLE_DRAFT_SQL` (Codex r2 B23, r4 B9, r5 B1) is the one predicate of "this target has a draft to resume
from": a `score_drafts` row on any attempt of the target under the evaluation policy `:policy`, whatever the
attempt's mode (an online attempt's draft or a batch draft). It is an SQL predicate correlated to the outer
alias `t` of `score_targets` and binds `:policy` only:
- `load_context` (the `score_target` subgraph, Task 9) reads it as a column of its own target row, under the
  claim predicate, and resumes the target's attempt chain when it holds;
- Task 16's `ELIGIBLE_SQL` negates it per candidate row, so a target that `load_context` would resume is
  never batched again.

**The sweeps**, each in the caller's transaction, each direction one `transition_where` statement with a
static predicate over the tuple's targets (the expiry's forward move two, one per reason). No sweep ever
moves a target whose `override` is not `none` (§7.16.3), the exhaustion sweep excepted (an override buys no
fifth attempt). Each statement locks the rows it moves in id order, `FOR UPDATE OF s SKIP LOCKED` (other
domain rows, step 10 of the lock schedule; a row another transaction holds is skipped this time and taken by
the next sweep, so a sweep never waits for a target row). No sweep bumps the ranking: its caller does, once,
last.
- `expiry_sweep(conn, *, llm_tuple_id, max_ad_age_days, now) -> ExpirySwept(expired, revived)` (§9.6.3):
  `pending | budget_deferred` → `expired_unscored('closed')` when the job is `closed`, else
  `expired_unscored('too_old')` when `coalesce(posted_at_effective, first_seen_at) < now - max_ad_age_days`;
  `stale` and `unknown` never expire; `expired_unscored` → `pending` with the reason cleared when neither
  holds (A2 A6). Every target the forward move expires has its follow-up hold released
  (`bulk.release_target_hold`, Ruling P9);
- `refilter_sweep(conn, *, llm_tuple_id, scope_version_id) -> RefilterSwept(filtered, unfiltered)`: `pending
  | budget_deferred | deferred_duplicate` whose job's `hard_filter_results` row of the scope failed →
  `filtered_out` with `filter_scope_version_id` (the group column cleared on leaving `deferred_duplicate`);
  `filtered_out` whose row passed → `pending` (A2 R7, Deviation 24). A job without a row moves nowhere: the
  move needs positive evidence;
- `regroup_sweep(conn, *, llm_tuple_id) -> RegroupSwept(deferred, undeferred)`: `pending | budget_deferred`
  members for which §8.7's deferral predicate holds (`DEFERRAL_ELIGIBLE_SQL`: a member of an active group
  whose representative is another job, its membership's `score_individually` false; Task 15 adds the
  conflicting-facts clause) → `deferred_duplicate` with the group; `deferred_duplicate` targets for which it
  no longer holds for their group (dissolved, left, elected, `score_individually`) → `pending`. The entering
  states guard the entry only, so an unchanged deferred member stays deferred across every sweep (Codex r1
  #36);
- `exhaustion_sweep(conn, *, llm_tuple_id) -> int` (Deviation 7, Codex r1 #21): `pending | budget_deferred`
  with `charged_attempts >= MAX_SCORE_ATTEMPTS` → `failed_attempts_exhausted('attempts_exhausted')`, its
  follow-up hold released.
The refilter and regroup moves keep the target's reason (§7.16.3 names none for them); the reverse expiry
clears it. No sweep touches an `in_progress` target or its attempt (Controller carry T10-f).

**The follow-up holds** (Ruling P52). The two sweeps that release holds read their candidates without a lock
and lock the candidates' bulk rows (`bulk.lock_for_targets`, step 7) before their first target row; their
moves are restricted to those candidates (a candidate a move skips keeps its hold). `admit` runs the four
sweeps in one transaction with exhaustion fourth, after the others locked target rows, so its first adapter
takes the bulk rows of both sweeps' candidates at once first (`lock_hold_candidates`, Controller carry
T12-c); each sweep's own `lock_for_targets` then re-takes rows the transaction holds.

**The user's moves**, in the API's transaction (`active_config FOR SHARE`, step 2; the route's idempotency
row, step 3; then the target row `FOR UPDATE`, step 10; `ranking_state` last; never the month row, so none of
them can wait on `budget_release`, which holds the month row and skips a target row another transaction
holds: Codex r1 #19, #20):
- `score_now(conn, *, job_id, force, now, defer) -> ScoreNow | ScoreNowRefused` (§9.6.4): the job's
  current-revision target of the active tuple (`not_found` without one). `pending | budget_deferred |
  expired_unscored | deferred_duplicate` → `pending` with `override = 'score_now'`; `refused | failed_*` take
  the Retry edge (`charged_attempts := 0`, L2880); `filtered_out` only with `force` (`override =
  'score_anyway'`, else `filtered_requires_force`); any other state (`in_progress`, `in_batch`, published,
  `cancelled`) is `conflict_state`. Before the target moves, the 409 pre-check: the scoring headroom of
  `now`'s month (`spend_ledger.read_month_or_new`, no lock: a fresh month answers its zero row and writes
  none) below `r_first` is `budget_exhausted` and nothing changes (L3678; racy by design, the attempt's own
  reservation is the authority). Every move sets `override_at = now` and `lane = 'score_now'` and clears a
  transient backoff (`not_before`, its count kept: Controller ruling T11-c); a move out of `budget_deferred`,
  `expired_unscored` or a Retry state clears the reason of the state it leaves; then
  `cycles.request_cycle("score_now")` (a running cycle's next claim round, or the waiting job, serves it) and
  `ranking.bump`;
- `retry(conn, *, target_id, now, defer)`: the Retry edge alone for `refused | failed_*` (`conflict_state`
  otherwise, `not_found` for no target), as above;
- `score_individually(conn, *, group_id, job_id, now, defer)` (§8.7): the membership's `score_individually
  := true` and, for a `deferred_duplicate` target, `pending` with `override = 'individual'` (`not_found`
  without the membership or the target); a target in any other state keeps it (the flag protects the
  member's later revisions).

**`budget_release`** (§6.9.4 L1201, tick, `release_deferred`): `fence_epoch`; `active_config FOR SHARE`;
`ensure_month` and the month row `FOR UPDATE` (`lock_month`: the first tick of a fresh month creates the row
it releases against, Codex r4 B8); every open `llm.cap_reached` of an earlier month resolved
(`resolve_stale_cap_alerts`, Codex r5 B4); then `k = floor(scoring headroom / r_first)` and, when `k >= 1`,
the `k` oldest `budget_deferred` targets of the active tuple (`state_changed_at`) → `pending` with the reason
cleared (locked `FOR UPDATE SKIP LOCKED` by a statement of their own first, so a target another transaction
holds, a Score now's, is skipped and the release never waits for it), the current month's `llm.cap_reached`
resolved, and `ranking.bump` when a target moved.

**`expiry_sweep`** (heavy, L5513): `fence_epoch`, `active_config FOR SHARE`, the expiry sweep of the active
tuple with the active `max_ad_age_days`, and `ranking.bump` when it moved a target.

Ids, counts, codes and amounts only reach a log line.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_FLOOR
from typing import Final, Literal

from sqlalchemy import TextClause
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db import ranking
from jobhunter.db.sql import q
from jobhunter.db.transitions import CLEAR_REASON, transition, transition_where
from jobhunter.errors import FencedOut
from jobhunter.faults import afault_point
from jobhunter.llm import budget, bulk, spend_ledger
from jobhunter.observability.logging import get_logger
from jobhunter.scoring import cycles
from jobhunter.scoring.admission import MAX_SCORE_ATTEMPTS
from jobhunter.types import JSONValue, get_int
from jobhunter.worker.epoch import fence_epoch
from jobhunter.worker.reconcilers import ReconcileContext, reconciler
from jobhunter.worker.tasks import Deferrer

RESUMABLE_DRAFT_SQL: Final = (
    "EXISTS (SELECT 1 FROM score_attempts a JOIN score_drafts d ON d.attempt_id = a.id "
    "WHERE a.target_id = t.id AND a.evaluation_policy_id = :policy)"
)

ScoreNowCode = Literal["not_found", "filtered_requires_force", "conflict_state", "budget_exhausted"]

CLOSED: Final = "closed"
TOO_OLD: Final = "too_old"
# The fault point after `budget_release` moved its targets, before its alert and ranking writes (the tests
# hold it there with the month row and the released target rows locked).
RELEASED_POINT: Final = "budget_release.after_release"
ACCEPTED_STATES: Final = ("pending", "budget_deferred", "expired_unscored", "deferred_duplicate")
RETRYABLE_STATES: Final = (
    "refused",
    "failed_max_tokens",
    "failed_invalid_output",
    "failed_request",
    "failed_transient",
    "failed_attempts_exhausted",
)
# A user's move out of these states clears the reason of the state it leaves; a `pending`,
# `deferred_duplicate` or `filtered_out` target keeps the reason it carries (a batch retry's, L2898).
_REASON_CLEARED: Final = frozenset({"budget_deferred", "expired_unscored", *RETRYABLE_STATES})
_ENTERING: Final = ("pending", "budget_deferred")
_SCORE_NOW: Final = "score_now"

log = get_logger("jobhunter.scoring.targets")


@dataclass(frozen=True)
class ExpirySwept:
    """What the expiry sweep moved: expired, and returned to `pending`."""

    expired: int
    revived: int

    @property
    def moved(self) -> int:
        return self.expired + self.revived


@dataclass(frozen=True)
class RefilterSwept:
    """What the refilter sweep moved: out to `filtered_out`, and back to `pending`."""

    filtered: int
    unfiltered: int

    @property
    def moved(self) -> int:
        return self.filtered + self.unfiltered


@dataclass(frozen=True)
class RegroupSwept:
    """What the regroup sweep moved: deferred, and returned to `pending`."""

    deferred: int
    undeferred: int

    @property
    def moved(self) -> int:
        return self.deferred + self.undeferred


@dataclass(frozen=True)
class ScoreNow:
    """A user's move done: the target and its state after it."""

    target_id: int
    state: str


@dataclass(frozen=True)
class ScoreNowRefused:
    """A user's move refused, with nothing written: the API's error code."""

    code: ScoreNowCode


# ---- the sweeps ----------------------------------------------------------------------------------------


def _locked(predicate: str, *, with_job: bool = False) -> str:
    """The `where` of a sweep's move: the rows of `score_targets s` (with its job `j`) that `predicate`
    selects, locked in id order, skipping a row another transaction holds (module docstring); `FOR UPDATE OF
    s` locks the target rows only, never a job row."""
    source = "score_targets s JOIN jobs j ON j.id = s.job_id" if with_job else "score_targets s"
    # The caller's static predicate over the registry's table: no input reaches the statement.
    return f"t.id IN (SELECT s.id FROM {source} WHERE {predicate} ORDER BY s.id FOR UPDATE OF s SKIP LOCKED)"  # noqa: S608


_ENTERING_SQL: Final = "s.state IN ('pending', 'budget_deferred')"
_CANDIDATE: Final = "s.id = ANY(CAST(:ids AS bigint[]))"
# §9.6.3: the job is closed, or older than the cutoff by its posted date, else by its first sighting.
_EXPIRING: Final = "(j.availability = 'closed' OR coalesce(j.posted_at_effective, j.first_seen_at) < :cutoff)"
_EXPIRY_CANDIDATES: Final = q(
    "SELECT s.id FROM score_targets s JOIN jobs j ON j.id = s.job_id "  # noqa: S608 (the constants above)
    f"WHERE s.llm_tuple_id = :tuple AND {_ENTERING_SQL} AND s.override = 'none' AND {_EXPIRING} ORDER BY s.id"
)
_EXPIRE_CLOSED: Final = _locked(
    f"{_CANDIDATE} AND {_ENTERING_SQL} AND s.override = 'none' AND j.availability = 'closed'", with_job=True
)
_EXPIRE_OLD: Final = _locked(
    f"{_CANDIDATE} AND {_ENTERING_SQL} AND s.override = 'none' "
    "AND coalesce(j.posted_at_effective, j.first_seen_at) < :cutoff",
    with_job=True,
)
_REVIVE: Final = _locked(
    f"s.llm_tuple_id = :tuple AND s.state = 'expired_unscored' AND s.override = 'none' AND NOT {_EXPIRING}",
    with_job=True,
)
_RESULT: Final = (
    "EXISTS (SELECT 1 FROM hard_filter_results h WHERE h.job_id = s.job_id AND h.scope_version_id = :scope "
    "AND {passed}h.passed)"
)
_FILTER_OUT: Final = _locked(
    "s.llm_tuple_id = :tuple AND s.state IN ('pending', 'budget_deferred', 'deferred_duplicate') "
    f"AND s.override = 'none' AND {_RESULT.format(passed='NOT ')}"
)
_FILTER_BACK: Final = _locked(
    "s.llm_tuple_id = :tuple AND s.state = 'filtered_out' AND s.override = 'none' "
    f"AND {_RESULT.format(passed='')}"
)


def _deferral_eligible(*, own_group: bool) -> str:
    """§8.7's deferral predicate over `s`, without the entering states (the membership flag and the
    representative; Task 15 adds the conflicting-facts clause); with `own_group`, for the group the target is
    deferred to."""
    group = " AND dm.group_id = s.deferred_group_id" if own_group else ""
    return (
        "EXISTS (SELECT 1 FROM duplicate_group_members dm JOIN duplicate_groups dg ON dg.id = dm.group_id "  # noqa: S608
        "WHERE dm.job_id = s.job_id AND dg.status = 'active' AND dg.representative_job_id IS NOT NULL "
        f"AND dg.representative_job_id <> s.job_id AND NOT dm.score_individually{group})"
    )


DEFERRAL_ELIGIBLE_SQL: Final = _deferral_eligible(own_group=False)
_DEFER: Final = (
    _locked(
        f"s.llm_tuple_id = :tuple AND {_ENTERING_SQL} AND s.override = 'none' AND {DEFERRAL_ELIGIBLE_SQL}"
    )
    + " AND m.job_id = t.job_id"
)
_UNDEFER: Final = _locked(
    "s.llm_tuple_id = :tuple AND s.state = 'deferred_duplicate' AND s.override = 'none' "
    f"AND NOT {_deferral_eligible(own_group=True)}"
)
_EXHAUSTED_SQL: Final = f"{_ENTERING_SQL} AND s.charged_attempts >= :max_attempts"
_EXHAUSTION_CANDIDATES: Final = q(
    f"SELECT s.id FROM score_targets s WHERE s.llm_tuple_id = :tuple AND {_EXHAUSTED_SQL} ORDER BY s.id"  # noqa: S608
)
_EXHAUST: Final = _locked(f"{_CANDIDATE} AND {_EXHAUSTED_SQL}")
_ACTIVE_SETTINGS: Final = q(
    "SELECT v.payload FROM active_config a JOIN settings_versions v ON v.id = a.settings_version_id "
    "WHERE a.id = 1"
)


def _cutoff(now: datetime, max_ad_age_days: int) -> datetime:
    return now - timedelta(days=max_ad_age_days)


async def _ids(conn: AsyncConnection, statement: TextClause, params: dict[str, object]) -> list[int]:
    return [int(i) for i in (await conn.execute(statement, params)).scalars()]


async def active_max_ad_age_days(conn: AsyncConnection) -> int:
    """The active settings' `scope.max_ad_age_days`, read without a lock: the caller holds `active_config`."""
    payload: JSONValue = (await conn.execute(_ACTIVE_SETTINGS)).scalar_one()
    return get_int(payload, "scope.max_ad_age_days")


def _logged(sweep: str, llm_tuple_id: int, **moved: int) -> None:
    if any(moved.values()):
        log.info("score_targets_swept", sweep=sweep, llm_tuple_id=llm_tuple_id, **moved)


async def _release_holds(conn: AsyncConnection, moved: list[RowMapping]) -> None:
    """The follow-up hold of every target a terminal sweep moved (the bulk rows are held: P52)."""
    for row in moved:
        await bulk.release_target_hold(conn, int(row["id"]))


async def lock_hold_candidates(
    conn: AsyncConnection, *, llm_tuple_id: int, max_ad_age_days: int, now: datetime
) -> list[int]:
    """The candidates of the expiry's forward move and of the exhaustion sweep, read without a lock, with the
    bulk rows of their unreleased follow-up items locked (module docstring, Controller carry T12-c); their
    ids in order."""
    expiring = await _ids(
        conn, _EXPIRY_CANDIDATES, {"tuple": llm_tuple_id, "cutoff": _cutoff(now, max_ad_age_days)}
    )
    exhausted = await _ids(
        conn, _EXHAUSTION_CANDIDATES, {"tuple": llm_tuple_id, "max_attempts": MAX_SCORE_ATTEMPTS}
    )
    candidates = sorted({*expiring, *exhausted})
    await bulk.lock_for_targets(conn, candidates)
    return candidates


async def expiry_sweep(
    conn: AsyncConnection, *, llm_tuple_id: int, max_ad_age_days: int, now: datetime
) -> ExpirySwept:
    """§9.6.3's sweep in both directions (module docstring)."""
    cutoff = _cutoff(now, max_ad_age_days)
    candidates = await _ids(conn, _EXPIRY_CANDIDATES, {"tuple": llm_tuple_id, "cutoff": cutoff})
    expired: list[RowMapping] = []
    if candidates:
        await bulk.lock_for_targets(conn, candidates)  # step 7, before the first target row (P52)
        for reason, where, params in (
            (CLOSED, _EXPIRE_CLOSED, {"ids": candidates}),
            (TOO_OLD, _EXPIRE_OLD, {"ids": candidates, "cutoff": cutoff}),
        ):
            expired += await transition_where(
                conn,
                "score_targets",
                _ENTERING,
                "expired_unscored",
                reason=reason,
                where=where,
                params=params,
            )
        await _release_holds(conn, expired)
    revived = await transition_where(
        conn,
        "score_targets",
        ["expired_unscored"],
        "pending",
        reason=CLEAR_REASON,
        where=_REVIVE,
        params={"tuple": llm_tuple_id, "cutoff": cutoff},
    )
    swept = ExpirySwept(expired=len(expired), revived=len(revived))
    _logged("expiry", llm_tuple_id, expired=swept.expired, revived=swept.revived)
    return swept


async def refilter_sweep(conn: AsyncConnection, *, llm_tuple_id: int, scope_version_id: int) -> RefilterSwept:
    """The refilter against the scope's `hard_filter_results`, both directions (module docstring)."""
    params: dict[str, object] = {"tuple": llm_tuple_id, "scope": scope_version_id}
    out = await transition_where(
        conn,
        "score_targets",
        ["pending", "budget_deferred", "deferred_duplicate"],
        "filtered_out",
        where=_FILTER_OUT,
        params=params,
        set_={"filter_scope_version_id": scope_version_id},
    )
    back = await transition_where(
        conn, "score_targets", ["filtered_out"], "pending", where=_FILTER_BACK, params=params
    )
    swept = RefilterSwept(filtered=len(out), unfiltered=len(back))
    _logged("refilter", llm_tuple_id, filtered=swept.filtered, unfiltered=swept.unfiltered)
    return swept


async def regroup_sweep(conn: AsyncConnection, *, llm_tuple_id: int) -> RegroupSwept:
    """§8.7's deferral, both directions (module docstring)."""
    params: dict[str, object] = {"tuple": llm_tuple_id}
    deferred = await transition_where(
        conn,
        "score_targets",
        _ENTERING,
        "deferred_duplicate",
        where=_DEFER,
        params=params,
        from_sql="duplicate_group_members m",
        set_sql={"deferred_group_id": "m.group_id"},
    )
    undeferred = await transition_where(
        conn, "score_targets", ["deferred_duplicate"], "pending", where=_UNDEFER, params=params
    )
    swept = RegroupSwept(deferred=len(deferred), undeferred=len(undeferred))
    _logged("regroup", llm_tuple_id, deferred=swept.deferred, undeferred=swept.undeferred)
    return swept


async def exhaustion_sweep(conn: AsyncConnection, *, llm_tuple_id: int) -> int:
    """The targets whose charged attempts are used up outside `in_progress`, ended (module docstring); how
    many."""
    candidates = await _ids(
        conn, _EXHAUSTION_CANDIDATES, {"tuple": llm_tuple_id, "max_attempts": MAX_SCORE_ATTEMPTS}
    )
    if not candidates:
        return 0
    await bulk.lock_for_targets(conn, candidates)  # step 7, before the first target row (P52)
    moved = await transition_where(
        conn,
        "score_targets",
        _ENTERING,
        "failed_attempts_exhausted",
        reason=cycles.ATTEMPTS_EXHAUSTED,
        where=_EXHAUST,
        params={"ids": candidates, "max_attempts": MAX_SCORE_ATTEMPTS},
    )
    await _release_holds(conn, moved)
    _logged("exhaustion", llm_tuple_id, exhausted=len(moved))
    return len(moved)


@reconciler("expiry_sweep", "heavy")
async def reconcile_expiry(ctx: ReconcileContext) -> int:
    """The expiry sweep of the active tuple with the active `max_ad_age_days` at `ctx.now` (module
    docstring); how many targets it moved. Fenced by the worker epoch first: the worker's own `FencedOut`
    propagates (Ruling FR-w), another owner's ends the pass with 0, having written nothing."""
    try:
        async with ctx.engine.begin() as conn:
            await fence_epoch(conn, ctx.epoch)
            active = await cycles.lock_active_scoring(conn)
            if active.llm_tuple_id is None:
                return 0
            swept = await expiry_sweep(
                conn,
                llm_tuple_id=active.llm_tuple_id,
                max_ad_age_days=await active_max_ad_age_days(conn),
                now=ctx.now,
            )
            if swept.moved:
                await ranking.bump(conn)  # step 11, last
    except FencedOut as fenced:
        if fenced.worker_superseded:
            raise  # `fence_epoch` logged it
        return 0
    return swept.moved


# ---- budget_release (§6.9.4) -------------------------------------------------------------------------------

# The `k` oldest, locked by a statement of their own: inside the move, a `LIMIT` that the plan evaluates again
# per row skips the rows the statement has moved already and takes the next ones.
_OLDEST_DEFERRED: Final = q(
    "SELECT id FROM score_targets WHERE llm_tuple_id = :tuple AND state = 'budget_deferred' "
    "ORDER BY state_changed_at, id LIMIT :k FOR UPDATE SKIP LOCKED"
)


async def release_deferred(conn: AsyncConnection, *, now: datetime) -> int:
    """`budget_release`'s work after its fence, in the caller's transaction (module docstring); how many
    targets it returned to `pending`."""
    config = await cycles.scoring_config(conn)  # step 2
    p = config.settings.budget
    month = budget.month_of(now)
    await spend_ledger.ensure_month(conn, month, p, now)
    row = await spend_ledger.lock_month(conn, month, p, now=now)  # step 6
    stale = await spend_ledger.resolve_stale_cap_alerts(conn, current=month)
    r_first = budget.r_first(config.model_policy, config.execution)
    headroom = budget.headroom(row, "scoring", p)
    k = int((headroom / r_first).to_integral_value(rounding=ROUND_FLOOR))
    if k < 1:
        return 0
    released: list[RowMapping] = []
    tuple_id = config.active.llm_tuple_id
    if tuple_id is not None:
        oldest = await _ids(conn, _OLDEST_DEFERRED, {"tuple": tuple_id, "k": k})  # step 10, skipping held rows
        released = await transition_where(
            conn,
            "score_targets",
            ["budget_deferred"],
            "pending",
            reason=CLEAR_REASON,
            where="t.id = ANY(CAST(:ids AS bigint[]))",
            params={"ids": oldest},
        )
        await afault_point(RELEASED_POINT)
    resolved = await spend_ledger.resolve_cap_reached(conn, month)
    if released:
        await ranking.bump(conn)  # step 11, last
    if released or resolved or stale:
        log.info(
            "score_budget_released",
            month=month.isoformat(),
            k=k,
            released=len(released),
            cap_alert_resolved=resolved,
            stale_cap_alerts_resolved=stale,
            target_ids=[int(r["id"]) for r in released],
        )
    return len(released)


@reconciler("budget_release", "tick")
async def reconcile_budget_release(ctx: ReconcileContext) -> int:
    """`release_deferred` at `ctx.now` in a transaction fenced by the worker epoch (module docstring); how
    many targets it released. The worker's own `FencedOut` propagates, another owner's ends the pass with
    0."""
    try:
        async with ctx.engine.begin() as conn:
            await fence_epoch(conn, ctx.epoch)
            return await release_deferred(conn, now=ctx.now)
    except FencedOut as fenced:
        if fenced.worker_superseded:
            raise
        return 0


# ---- the user's moves (§9.6.4, §7.16.3, §8.7) -------------------------------------------------------------

_JOB_TARGET: Final = q(
    "SELECT t.id, t.state FROM score_targets t JOIN jobs j ON j.current_revision_id = t.job_revision_id "
    "WHERE j.id = :job AND j.merged_into_job_id IS NULL AND t.llm_tuple_id = :tuple FOR UPDATE OF t"
)
_TARGET: Final = q("SELECT id, state FROM score_targets WHERE id = :t FOR UPDATE")
_MEMBER: Final = q(
    "SELECT job_id FROM duplicate_group_members WHERE group_id = :g AND job_id = :j FOR UPDATE"
)
_FLAG_MEMBER: Final = q(
    "UPDATE duplicate_group_members SET score_individually = true WHERE group_id = :g AND job_id = :j"
)


def _refused(code: ScoreNowCode, action: str, **ids: int) -> ScoreNowRefused:
    log.info("score_target_action_refused", action=action, code=code, **ids)
    return ScoreNowRefused(code)


async def _headroom_left(conn: AsyncConnection, config: cycles.ScoringConfig, *, now: datetime) -> bool:
    """The 409 pre-check (module docstring): whether `now`'s month still admits `r_first` for scoring."""
    p = config.settings.budget
    row = await spend_ledger.read_month_or_new(conn, budget.month_of(now), p, now=now)
    return budget.headroom(row, "scoring", p) >= budget.r_first(config.model_policy, config.execution)


async def _to_pending(
    conn: AsyncConnection, target_id: int, state: str, *, override: str, now: datetime
) -> None:
    """The locked target from `state` to `pending` with `override`, as a user's move sets it (module
    docstring): the Retry edge resets the charged attempts."""
    set_: dict[str, object] = {
        "override": override,
        "override_at": now,
        "lane": _SCORE_NOW,
        "not_before": None,
    }
    if state in RETRYABLE_STATES:
        set_["charged_attempts"] = 0
    reason = CLEAR_REASON if state in _REASON_CLEARED else None
    await transition(conn, "score_targets", target_id, [state], "pending", reason=reason, set_=set_)


async def _requested(
    conn: AsyncConnection, target_id: int, *, action: str, state: str, defer: Deferrer
) -> ScoreNow:
    """A move done: the `score_now` cycle requested and the ranking bumped (step 11, last)."""
    await cycles.request_cycle(conn, trigger="score_now", defer=defer)
    await ranking.bump(conn)
    log.info("score_target_action", action=action, target_id=target_id, from_state=state)
    return ScoreNow(target_id, "pending")


async def score_now(
    conn: AsyncConnection, *, job_id: int, force: bool, now: datetime, defer: Deferrer
) -> ScoreNow | ScoreNowRefused:
    """§9.6.4's Score now of the job's current-revision target of the active tuple (module docstring)."""
    config = await cycles.scoring_config(conn)  # step 2
    tuple_id = config.active.llm_tuple_id
    row = None
    if tuple_id is not None:
        row = (await conn.execute(_JOB_TARGET, {"job": job_id, "tuple": tuple_id})).first()
    if row is None:
        return _refused("not_found", "score_now", job_id=job_id)
    target_id, state = int(row.id), str(row.state)
    if state in ACCEPTED_STATES or state in RETRYABLE_STATES:
        override = _SCORE_NOW
    elif state == "filtered_out" and force:
        override = "score_anyway"
    elif state == "filtered_out":
        return _refused("filtered_requires_force", "score_now", target_id=target_id)
    else:
        return _refused("conflict_state", "score_now", target_id=target_id)
    if not await _headroom_left(conn, config, now=now):
        return _refused("budget_exhausted", "score_now", target_id=target_id)
    await _to_pending(conn, target_id, state, override=override, now=now)
    return await _requested(conn, target_id, action="score_now", state=state, defer=defer)


async def retry(
    conn: AsyncConnection, *, target_id: int, now: datetime, defer: Deferrer
) -> ScoreNow | ScoreNowRefused:
    """The user's Retry of a `refused` or `failed_*` target (L2880; module docstring)."""
    await cycles.lock_active_scoring(conn)  # step 2
    row = (await conn.execute(_TARGET, {"t": target_id})).first()
    if row is None:
        return _refused("not_found", "retry", target_id=target_id)
    state = str(row.state)
    if state not in RETRYABLE_STATES:
        return _refused("conflict_state", "retry", target_id=target_id)
    await _to_pending(conn, target_id, state, override=_SCORE_NOW, now=now)
    return await _requested(conn, target_id, action="retry", state=state, defer=defer)


async def score_individually(
    conn: AsyncConnection, *, group_id: int, job_id: int, now: datetime, defer: Deferrer
) -> ScoreNow | ScoreNowRefused:
    """§8.7's Score individually of the group member `job_id` (module docstring)."""
    active = await cycles.lock_active_scoring(conn)  # step 2
    member = (await conn.execute(_MEMBER, {"g": group_id, "j": job_id})).first()
    row = None
    if member is not None and active.llm_tuple_id is not None:
        row = (await conn.execute(_JOB_TARGET, {"job": job_id, "tuple": active.llm_tuple_id})).first()
    if row is None:
        return _refused("not_found", "score_individually", group_id=group_id, job_id=job_id)
    target_id, state = int(row.id), str(row.state)
    await conn.execute(_FLAG_MEMBER, {"g": group_id, "j": job_id})
    if state != "deferred_duplicate":
        log.info("score_target_action", action="score_individually", target_id=target_id, from_state=state)
        return ScoreNow(target_id, state)
    await transition(
        conn,
        "score_targets",
        target_id,
        [state],
        "pending",
        set_={"override": "individual", "override_at": now, "lane": _SCORE_NOW, "not_before": None},
    )
    return await _requested(conn, target_id, action="score_individually", state=state, defer=defer)
