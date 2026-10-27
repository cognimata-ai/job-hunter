"""Score cycles (spec §3.3.1 steps 3-4, §3.4 step 6, §6.9.2, §7.6 L1718, §9.3, §9.4.3, §17.4 L5502, §17.6
L5596; plan Task 11, Codex r1 #16, #19, #20, #25, r4 B1, B8, r5 B4, r7 B5, Deviations 5, 19, 20, 39): the
score lane's admission guard, the trigger, the start of a cycle, the reclaim of claims, and the two ends of an
orphaned cycle (the takeover sweeps and the `orphaned_cycles` reconciler).

**The active configuration** (`scoring_config`): `active_config FOR SHARE` (`lock_active_scoring`, step 2 of
the lock schedule) and what its pointers name (the model policy, the execution config, the settings' budget
and cycle sizes), read once per transaction and passed on to the guard and the gates.

**`pause_reason(conn, *, fake_llm, now, config=None)`**, the one admission guard of the score lane, which
`start_cycle` and Task 16's batch preparation consult alike: `reservation_bound_violated` while an
unacknowledged open `llm.reservation_bound_violated` exists, `auth_failed` likewise for `llm.auth_failed` (an
acknowledgement lifts either while the alert stays open, Deviation 19), `cap_reached` while the scoring
headroom of `now`'s UTC month (`spend_ledger.read_month_or_new`, no lock) is below `r_first` (the ledger
decides, never `llm.cap_reached`: a previous month's open alert cannot pause a new month, a second denial in
a month still pauses, and the pause lifts by itself when headroom returns), `global_fence` while a global
purge fence is active, and `schema_unverified` while `validate.schema_verified` of the active prompt-schema
version and model policy is false outside fake LLM mode; `None` otherwise, in that order. Scoring pauses at
admission, never inside a running cycle.

**`request_cycle(conn, *, trigger, defer)`** defers `score_cycle(trigger)` under the lock `score` and the
queueing lock `score_cycle:<trigger>` in the caller's transaction (the API's routes, the tick, `batch_poll`),
`False` when a job of that trigger is already waiting: it, or the running cycle's next claim round, serves the
request. It takes no lock and stamps no epoch; the cycle row is the worker's.

**`start_cycle(conn, *, trigger, now, epoch, fake_llm)`**, the `score_cycle` task's first transaction, in the
lock schedule's order: `fence_epoch`; `scoring_config` (the tuple, the scope, the evaluation policy; no tuple,
no cycle); `pause_reason`; then by trigger:
- `periodic` and `manual`: an admissible target of the tuple (`admission.any_admissible`, the rounds' own
  predicate: `pending`, due, unfenced, charged attempts left) and, under the month row's lock (`ensure_month`
  first, so the first cycle of a fresh UTC month creates the row it gates on), `bucket_usd ≥
  min(min_cycle_targets x ĉ, B_max)`;
- `batch_followup`: the same, for an admissible target of lane `batch_followup`;
- `score_now`: an admissible target of lane `score_now`, and no month lock (nothing of it is bucket-gated; the
  bucket is read without a lock for the record).
`S = max(0, min(max_targets_per_cycle, floor(bucket_usd / ĉ)))`, `0` for `score_now`; the row is inserted
`running` with `ON CONFLICT DO NOTHING` (the partial unique index `score_cycles_one_running` arbitrates, no
exception, so the transaction stays usable): no row is a cycle still running (`score_cycle_skipped`).
Every refusal is logged `score_cycle_not_started` with its reason. `ĉ` (`c_hat`) is the trailing 7-day mean
`cost_usd` of the tuple's online, charged attempts that ended `published_accepted` or `published_contested`
under the evaluation policy, from 20 of them on; else the §21.2 central estimate of the model policy.

**The reclaim** (`reclaim`, §9.4.3): the `in_progress` targets a predicate selects, locked `FOR UPDATE` in id
order, go to `pending` while charged attempts are left (`charged_attempts < MAX_SCORE_ATTEMPTS`), else to
`failed_attempts_exhausted('attempts_exhausted')` with their follow-up hold released (a terminal move,
`bulk.release_target_hold`); their running attempts end `claim_lost` with the reclaim's detail. A reclaim is
never the claim's owner (a sweep, the reconciler, a failed cycle), so it locks the candidates' bulk rows
first (`bulk.lock_for_targets`, Ruling P52: the bulk row, step 7, before the target rows, step 10). The
predicates are this module's: `older_epoch` (`claim_epoch < :epoch`) and `cycle` (the target's current
attempt belongs to `:cycle`). An attempt's own failure is never a reclaim: `run_target` ends it through
`fail` (Ruling T11-a).

**An orphaned cycle** is failed `orphaned` with the report closed `{"reason": "orphaned"}`, its claims
reclaimed, and the warn alert `score_cycle.orphaned` raised (subject `cycle:<id>`), which the next completed
cycle resolves (`graphs.score.nodes.finalize_cycle`):
- the takeover sweep `score_targets` (§3.3.1 step 3) reclaims every claim of an older epoch, detail
  `worker_takeover`, and the sweep `score_cycles` (step 4) fails every running cycle of an older epoch, in
  this order (registration). Each runs in the takeover's transaction today; Task 27 gives each sweep a
  transaction of its own that starts with `fence_epoch` and `active_config FOR SHARE`;
- the tick reconciler `orphaned_cycles` (§17.4) fails a running cycle whose `heartbeat_at` is older than 3 min
  (rows another transaction holds are skipped this time), detail `cycle_orphaned`, fenced by the worker epoch
  first; after the commit it cancels the cycle's local execution when that runs for the cycle's epoch.
`fail_cycle` is the cycle task's own end for a bug: `failed('bug')` with the report `{"reason": "bug",
"error_class": …}` and the critical alert `score_cycle.failed`, its claims reclaimed (detail `cycle_failed`).

Ids, counts, codes and amounts only reach a log line, a report or an alert.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Final, Literal, get_args

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q
from jobhunter.db.transitions import transition, transition_where
from jobhunter.errors import FencedOut, TransitionConflict
from jobhunter.llm import budget, bulk, spend_ledger
from jobhunter.llm.budget import BudgetParams, BudgetRow
from jobhunter.llm.gateway import AUTH_FAILED_ALERT
from jobhunter.llm.spend_ledger import BOUND_VIOLATED_ALERT
from jobhunter.observability.alerts import raise_alert
from jobhunter.observability.logging import get_logger
from jobhunter.scoring.admission import CYCLE_OWNER as CYCLE_OWNER
from jobhunter.scoring.admission import MAX_SCORE_ATTEMPTS, Lane, any_admissible
from jobhunter.settings import validate
from jobhunter.settings.llm_projection import central_cost_per_target
from jobhunter.settings.model import ExecutionConfig, ModelPolicy
from jobhunter.types import JSONValue, get_int
from jobhunter.worker.epoch import fence_epoch, register_takeover_sweep
from jobhunter.worker.executions import EXECUTIONS, ExecKey
from jobhunter.worker.reconcilers import ReconcileContext, reconciler
from jobhunter.worker.tasks import Deferrer

CycleTrigger = Literal["periodic", "score_now", "batch_followup", "manual"]
CYCLE_TRIGGERS: Final[tuple[CycleTrigger, ...]] = get_args(CycleTrigger)
PauseReason = Literal[
    "reservation_bound_violated", "auth_failed", "cap_reached", "global_fence", "schema_unverified"
]
ReclaimKind = Literal["older_epoch", "cycle"]

ORPHANED_ALERT: Final = "score_cycle.orphaned"
FAILED_ALERT: Final = "score_cycle.failed"
ORPHANED_AFTER: Final = timedelta(minutes=3)
C_HAT_WINDOW: Final = timedelta(days=7)
C_HAT_MIN_SAMPLES: Final = 20
ORPHANED: Final = "orphaned"
BUG: Final = "bug"
# The details of a reclaimed attempt (`claim_lost`), by who reclaimed it.
WORKER_TAKEOVER: Final = "worker_takeover"
CYCLE_ORPHANED: Final = "cycle_orphaned"
CYCLE_FAILED: Final = "cycle_failed"
ATTEMPTS_EXHAUSTED: Final = "attempts_exhausted"

_TRIGGERS: Final[Mapping[str, CycleTrigger]] = {trigger: trigger for trigger in CYCLE_TRIGGERS}
_LANE_OF_TRIGGER: Final[Mapping[CycleTrigger, Lane]] = {
    "score_now": "score_now",
    "batch_followup": "batch_followup",
}

log = get_logger("jobhunter.scoring.cycles")


def cycle_trigger(value: str) -> CycleTrigger:
    """`value` as a cycle trigger (a `score_cycle` job's argument); `ValueError` for any other text."""
    trigger = _TRIGGERS.get(value)
    if trigger is None:
        raise ValueError(f"unknown score cycle trigger {value!r}")
    return trigger


# ---- the active configuration ----------------------------------------------------------------------------

_ACTIVE: Final = q(
    "SELECT llm_tuple_id, scope_version_id, evaluation_policy_id, model_policy_id, execution_config_id, "
    "prompt_schema_version_id, settings_version_id FROM active_config WHERE id = 1 FOR SHARE"
)
_NAMED: Final = q(
    "SELECT m.payload AS model_policy, e.payload AS execution, s.payload AS settings "
    "FROM model_policies m, execution_configs e, settings_versions s "
    "WHERE m.id = :model_policy AND e.id = :execution AND s.id = :settings"
)


@dataclass(frozen=True)
class ActiveScoring:
    """The pointers of `active_config` the score lane reads, under `FOR SHARE`."""

    llm_tuple_id: int | None
    scope_version_id: int
    evaluation_policy_id: int
    model_policy_id: int
    execution_config_id: int
    prompt_schema_version_id: int
    settings_version_id: int


async def lock_active_scoring(conn: AsyncConnection) -> ActiveScoring:
    """`active_config FOR SHARE` (step 2 of the lock schedule) and the pointers the score lane reads."""
    row = (await conn.execute(_ACTIVE)).one()
    return ActiveScoring(
        llm_tuple_id=None if row.llm_tuple_id is None else int(row.llm_tuple_id),
        scope_version_id=int(row.scope_version_id),
        evaluation_policy_id=int(row.evaluation_policy_id),
        model_policy_id=int(row.model_policy_id),
        execution_config_id=int(row.execution_config_id),
        prompt_schema_version_id=int(row.prompt_schema_version_id),
        settings_version_id=int(row.settings_version_id),
    )


@dataclass(frozen=True)
class CycleSettings:
    """The active settings' budget params and cycle sizes."""

    budget: BudgetParams
    max_targets_per_cycle: int
    min_cycle_targets: int


@dataclass(frozen=True)
class ScoringConfig:
    """The active configuration as the score lane's admission reads it (module docstring)."""

    active: ActiveScoring
    model_policy: ModelPolicy
    execution: ExecutionConfig
    settings: CycleSettings


async def scoring_config(conn: AsyncConnection) -> ScoringConfig:
    """`active_config FOR SHARE` and what its pointers name, in two statements (module docstring)."""
    active = await lock_active_scoring(conn)
    params = {
        "model_policy": active.model_policy_id,
        "execution": active.execution_config_id,
        "settings": active.settings_version_id,
    }
    row = (await conn.execute(_NAMED, params)).one()
    payload: JSONValue = row.settings
    if not isinstance(payload, dict):
        raise LookupError(f"settings version {active.settings_version_id} holds no document")
    return ScoringConfig(
        active=active,
        model_policy=ModelPolicy.model_validate(row.model_policy),
        execution=ExecutionConfig.model_validate(row.execution),
        settings=CycleSettings(
            budget=BudgetParams.from_payload(payload),
            max_targets_per_cycle=get_int(payload, "scoring.max_targets_per_cycle"),
            min_cycle_targets=get_int(payload, "scoring.min_cycle_targets"),
        ),
    )


# ---- the pause guard --------------------------------------------------------------------------------------

_UNACKNOWLEDGED: Final = q(
    "SELECT EXISTS (SELECT 1 FROM alerts WHERE code = :code AND resolved_at IS NULL "
    "AND acknowledged_at IS NULL)"
)
_GLOBAL_FENCE: Final = q(
    "SELECT EXISTS (SELECT 1 FROM purge_fences WHERE kind = 'global' AND released_at IS NULL)"
)


async def _unacknowledged(conn: AsyncConnection, code: str) -> bool:
    return bool((await conn.execute(_UNACKNOWLEDGED, {"code": code})).scalar_one())


async def pause_reason(
    conn: AsyncConnection, *, fake_llm: bool, now: datetime, config: ScoringConfig | None = None
) -> PauseReason | None:
    """Why the score lane admits nothing now, or `None` (module docstring); `now`, the caller's clock, names
    the month whose headroom decides `cap_reached`, and `config` is the caller's `scoring_config` of this
    transaction (read here when not given)."""
    if await _unacknowledged(conn, BOUND_VIOLATED_ALERT):
        return "reservation_bound_violated"
    if await _unacknowledged(conn, AUTH_FAILED_ALERT):
        return "auth_failed"
    read = await scoring_config(conn) if config is None else config
    p = read.settings.budget
    row = await spend_ledger.read_month_or_new(conn, budget.month_of(now), p, now=now)
    if budget.headroom(row, "scoring", p) < budget.r_first(read.model_policy, read.execution):
        return "cap_reached"
    if (await conn.execute(_GLOBAL_FENCE)).scalar_one():
        return "global_fence"
    if not fake_llm and not await validate.schema_verified(
        conn,
        prompt_schema_version_id=read.active.prompt_schema_version_id,
        model_policy_id=read.active.model_policy_id,
    ):
        return "schema_unverified"
    return None


# ---- ĉ ------------------------------------------------------------------------------------------------

_HISTORY: Final = q(
    "SELECT count(*) AS n, avg(a.cost_usd) AS mean FROM score_attempts a "
    "JOIN score_targets t ON t.id = a.target_id WHERE t.llm_tuple_id = :tuple "
    "AND a.evaluation_policy_id = :policy AND a.mode = 'online' AND a.charged "
    "AND a.outcome IN ('published_accepted', 'published_contested') AND a.ended_at >= :since "
    "AND a.ended_at <= :now"
)


async def c_hat(
    conn: AsyncConnection,
    *,
    llm_tuple_id: int,
    evaluation_policy_id: int,
    model_policy: ModelPolicy,
    now: datetime,
) -> tuple[Decimal, Literal["history", "central"]]:
    """`ĉ` and where it came from (module docstring), at $0.000001; a history mean that rounds to no cost
    (`c_hat_usd > 0`) falls back to the central estimate too."""
    params = {"tuple": llm_tuple_id, "policy": evaluation_policy_id, "since": now - C_HAT_WINDOW, "now": now}
    row = (await conn.execute(_HISTORY, params)).one()
    if int(row.n) >= C_HAT_MIN_SAMPLES and row.mean is not None:
        mean = Decimal(row.mean).quantize(budget.USD_QUANTUM, rounding=ROUND_HALF_UP)
        if mean > 0:
            return mean, "history"
    return central_cost_per_target(model_policy), "central"


# ---- the trigger and the start ---------------------------------------------------------------------------


async def request_cycle(conn: AsyncConnection, *, trigger: CycleTrigger, defer: Deferrer) -> bool:
    """Defer `score_cycle(trigger)` in the caller's transaction (module docstring); `False` when a job of
    that trigger already waits."""
    job = await defer(
        conn, "score_cycle", lock="score", queueing_lock=f"score_cycle:{trigger}", trigger=trigger
    )
    log.info("score_cycle_requested", trigger=trigger, requested=job is not None, job_id=job)
    return job is not None


_RUNNING: Final = q("SELECT EXISTS (SELECT 1 FROM score_cycles WHERE status = 'running')")
_INSERT_CYCLE: Final = q(
    "INSERT INTO score_cycles (trigger, llm_tuple_id, scope_version_id, evaluation_policy_id, worker_epoch, "
    "status, bucket_usd_at_start, c_hat_usd, admission_size) VALUES (:trigger, :tuple, :scope, :policy, "
    ":epoch, 'running', :bucket, :c_hat, :size) ON CONFLICT DO NOTHING RETURNING id"
)


async def cycle_running(conn: AsyncConnection) -> bool:
    """Whether a score cycle is `running`."""
    return bool((await conn.execute(_RUNNING)).scalar_one())


def admission_size(bucket_usd: Decimal, estimate: Decimal, max_targets_per_cycle: int) -> int:
    """`S = max(0, min(max_targets_per_cycle, floor(bucket_usd / ĉ)))` (L3598)."""
    affordable = int((bucket_usd / estimate).to_integral_value(rounding=ROUND_FLOOR))
    return max(0, min(max_targets_per_cycle, affordable))


async def _locked_bucket(
    conn: AsyncConnection, settings: CycleSettings, estimate: Decimal, *, now: datetime
) -> tuple[BudgetRow, bool]:
    """The month row of `now` under its lock (created first when absent: Codex r4 B8), accrued, and whether
    its bucket holds `min(min_cycle_targets x ĉ, B_max)` (L1176)."""
    month = budget.month_of(now)
    await spend_ledger.ensure_month(conn, month, settings.budget, now)
    row = await spend_ledger.lock_month(conn, month, settings.budget, now=now)
    capacity = budget.bucket_capacity(budget.row_params(row, settings.budget), month)
    return row, row.bucket_usd >= min(settings.min_cycle_targets * estimate, capacity)


async def periodic_gate(conn: AsyncConnection, config: ScoringConfig, *, now: datetime) -> bool:
    """The periodic trigger's gate (§3.4 step 6, L1176): an admissible target of the active tuple
    (`admission.any_admissible`) and the bucket under the month lock (`ensure_month` first); the caller read
    `config` (`scoring_config`, holding `active_config`) and checked the pause guard and the running cycle."""
    tuple_id = config.active.llm_tuple_id
    if tuple_id is None or not await any_admissible(conn, tuple_id, lane=None, now=now):
        return False
    estimate, _ = await c_hat(
        conn,
        llm_tuple_id=tuple_id,
        evaluation_policy_id=config.active.evaluation_policy_id,
        model_policy=config.model_policy,
        now=now,
    )
    _, holds = await _locked_bucket(conn, config.settings, estimate, now=now)
    return holds


def _not_started(trigger: CycleTrigger, reason: str) -> None:
    log.info("score_cycle_not_started", trigger=trigger, reason=reason)


async def start_cycle(
    conn: AsyncConnection, *, trigger: CycleTrigger, now: datetime, epoch: int, fake_llm: bool
) -> int | None:
    """Start a cycle of `trigger` as worker `epoch` in the caller's transaction (module docstring); its id, or
    `None` when nothing may start. `FencedOut` when `epoch` is not the current worker epoch."""
    await fence_epoch(conn, epoch)
    config = await scoring_config(conn)
    active = config.active
    if active.llm_tuple_id is None:
        _not_started(trigger, "no_tuple")
        return None
    reason = await pause_reason(conn, fake_llm=fake_llm, now=now, config=config)
    if reason is not None:
        _not_started(trigger, f"paused:{reason}")
        return None
    tuple_id = active.llm_tuple_id
    if not await any_admissible(conn, tuple_id, lane=_LANE_OF_TRIGGER.get(trigger), now=now):
        _not_started(trigger, "no_pending")
        return None
    estimate, source = await c_hat(
        conn,
        llm_tuple_id=tuple_id,
        evaluation_policy_id=active.evaluation_policy_id,
        model_policy=config.model_policy,
        now=now,
    )
    settings = config.settings
    if trigger == "score_now":  # outside the bucket: the row is read for the record, never locked
        row = await spend_ledger.read_month_or_new(conn, budget.month_of(now), settings.budget, now=now)
        size = 0
    else:
        row, holds = await _locked_bucket(conn, settings, estimate, now=now)
        if not holds:
            _not_started(trigger, "bucket")
            return None
        size = admission_size(row.bucket_usd, estimate, settings.max_targets_per_cycle)
    params = {
        "trigger": trigger,
        "tuple": tuple_id,
        "scope": active.scope_version_id,
        "policy": active.evaluation_policy_id,
        "epoch": epoch,
        "bucket": row.bucket_usd,
        "c_hat": estimate,
        "size": size,
    }
    cycle_id = (await conn.execute(_INSERT_CYCLE, params)).scalar_one_or_none()
    if cycle_id is None:
        log.info("score_cycle_skipped", trigger=trigger, reason="running")
        return None
    log.info(
        "score_cycle_started",
        cycle_id=int(cycle_id),
        trigger=trigger,
        llm_tuple_id=tuple_id,
        worker_epoch=epoch,
        admission_size=size,
        c_hat_usd=str(estimate),
        c_hat_source=source,
        bucket_usd=str(row.bucket_usd),
    )
    return int(cycle_id)


# ---- the cycle row -----------------------------------------------------------------------------------------

_LOCK_CYCLE: Final = q(
    "SELECT id, trigger, llm_tuple_id, scope_version_id, evaluation_policy_id, worker_epoch, admission_size, "
    "admitted, c_hat_usd FROM score_cycles WHERE id = :c AND worker_epoch = :e AND status = 'running' "
    "FOR SHARE"
)


@dataclass(frozen=True)
class CycleRow:
    """A running cycle as its owner reads it under the owner predicate."""

    id: int
    trigger: str
    llm_tuple_id: int
    scope_version_id: int
    evaluation_policy_id: int
    worker_epoch: int
    admission_size: int
    admitted: Mapping[str, int]
    c_hat_usd: Decimal


async def lock_cycle(conn: AsyncConnection, cycle_id: int, *, epoch: int) -> CycleRow | None:
    """The cycle `FOR SHARE` under its owner predicate (`worker_epoch = :e AND status = 'running'`, step 5);
    `None` when it is no longer this owner's running cycle."""
    row = (await conn.execute(_LOCK_CYCLE, {"c": cycle_id, "e": epoch})).first()
    if row is None:
        return None
    stored = row.admitted if isinstance(row.admitted, dict) else {}
    return CycleRow(
        id=int(row.id),
        trigger=str(row.trigger),
        llm_tuple_id=int(row.llm_tuple_id),
        scope_version_id=int(row.scope_version_id),
        evaluation_policy_id=int(row.evaluation_policy_id),
        worker_epoch=int(row.worker_epoch),
        admission_size=int(row.admission_size),
        admitted={str(lane): int(n) for lane, n in stored.items()},
        c_hat_usd=Decimal(row.c_hat_usd),
    )


# ---- the reclaim (§9.4.3) ----------------------------------------------------------------------------------

_RECLAIM_WHERE: Final[Mapping[ReclaimKind, str]] = {
    "older_epoch": "t.claim_epoch < :epoch",
    "cycle": (
        "EXISTS (SELECT 1 FROM score_attempts a WHERE a.target_id = t.id AND a.attempt_no = t.attempt_no "
        "AND a.cycle_id = :cycle)"
    ),
}
# The candidates of a reclaim, read without a lock (for `bulk.lock_for_targets`), and the claims, locked in id
# order; both statements are the registry's constants over this module's predicates.
_CANDIDATES: Final = {
    kind: q(
        f"SELECT t.id FROM score_targets t WHERE t.state = 'in_progress' AND ({where}) ORDER BY t.id"  # noqa: S608
    )
    for kind, where in _RECLAIM_WHERE.items()
}
_LOCK_CLAIMS: Final = {
    kind: q(
        "SELECT t.id, t.attempt_no, t.charged_attempts FROM score_targets t "  # noqa: S608 (the constants above)
        f"WHERE t.state = 'in_progress' AND ({where}) ORDER BY t.id FOR UPDATE"
    )
    for kind, where in _RECLAIM_WHERE.items()
}
_BY_IDS: Final = "t.id = ANY(CAST(:ids AS bigint[]))"
_ATTEMPTS_OF: Final = (
    "(t.target_id, t.attempt_no) IN (SELECT * FROM unnest(CAST(:targets AS bigint[]), "
    "CAST(:numbers AS integer[])))"
)


@dataclass(frozen=True)
class Reclaimed:
    """A reclaimed claim: its target, the attempt number it ended and where the target went."""

    target_id: int
    attempt_no: int
    state: Literal["pending", "failed_attempts_exhausted"]


async def reclaim(
    conn: AsyncConnection, kind: ReclaimKind, params: Mapping[str, object], *, detail: str
) -> list[Reclaimed]:
    """Reclaim the claims of `kind` (module docstring) in the caller's transaction; what it moved."""
    # Never the claim's owner: the bulk rows (step 7) before the target rows (step 10)
    candidates = [int(i) for i in (await conn.execute(_CANDIDATES[kind], dict(params))).scalars()]
    if not candidates:
        return []
    await bulk.lock_for_targets(conn, candidates)
    locked = (await conn.execute(_LOCK_CLAIMS[kind], dict(params))).all()
    if not locked:
        return []
    moved: list[Reclaimed] = []
    left = [int(r.id) for r in locked if int(r.charged_attempts) < MAX_SCORE_ATTEMPTS]
    spent = [int(r.id) for r in locked if int(r.charged_attempts) >= MAX_SCORE_ATTEMPTS]
    numbers = {int(r.id): int(r.attempt_no) for r in locked}
    if left:
        await transition_where(
            conn, "score_targets", ["in_progress"], "pending", where=_BY_IDS, params={"ids": left}
        )
        moved.extend(Reclaimed(t, numbers[t], "pending") for t in left)
    if spent:
        for target_id in spent:  # a terminal move releases the follow-up hold (the bulk row is held)
            await bulk.release_target_hold(conn, target_id)
        await transition_where(
            conn,
            "score_targets",
            ["in_progress"],
            "failed_attempts_exhausted",
            reason=ATTEMPTS_EXHAUSTED,
            where=_BY_IDS,
            params={"ids": spent},
        )
        moved.extend(Reclaimed(t, numbers[t], "failed_attempts_exhausted") for t in spent)
    await transition_where(
        conn,
        "score_attempts",
        ["running"],
        "claim_lost",
        reason=detail,
        where=_ATTEMPTS_OF,
        params={"targets": [m.target_id for m in moved], "numbers": [m.attempt_no for m in moved]},
    )
    log.info(
        "score_claims_reclaimed",
        detail=detail,
        pending=len(left),
        exhausted=len(spent),
        target_ids=[m.target_id for m in moved],
    )
    return moved


# ---- a cycle's ends other than `finalize_cycle` ----------------------------------------------------------

_OLDER_CYCLES: Final = q(
    "SELECT id FROM score_cycles WHERE status = 'running' AND worker_epoch < :epoch ORDER BY id FOR UPDATE"
)
_STALE_CYCLES: Final = q(
    "SELECT id, worker_epoch FROM score_cycles WHERE status = 'running' AND heartbeat_at < :cutoff "
    "ORDER BY id FOR UPDATE SKIP LOCKED"
)


async def _orphan(conn: AsyncConnection, cycle_id: int, *, detail: str) -> int:
    """Fail the locked running cycle `orphaned` with its report closed, reclaim its claims and raise the
    warn alert; how many claims it reclaimed."""
    await transition(
        conn,
        "score_cycles",
        cycle_id,
        ["running"],
        "failed",
        reason=ORPHANED,
        set_={"report": {"reason": ORPHANED}},
    )
    reclaimed = await reclaim(conn, "cycle", {"cycle": cycle_id}, detail=detail)
    await raise_alert(
        conn,
        code=ORPHANED_ALERT,
        severity="warn",
        subject=f"cycle:{cycle_id}",
        message="a score cycle was failed as orphaned",
        context={"cycle_id": cycle_id, "reclaimed": len(reclaimed)},
    )
    log.warning("score_cycle_orphaned", cycle_id=cycle_id, reclaimed=len(reclaimed), detail=detail)
    return len(reclaimed)


async def fail_cycle(conn: AsyncConnection, cycle_id: int, *, epoch: int, error_class: str) -> bool:
    """The cycle task's end for a bug (module docstring), under the owner predicate; `False` when the cycle
    is no longer this owner's running cycle (nothing is written then)."""
    try:
        await transition(
            conn,
            "score_cycles",
            cycle_id,
            ["running"],
            "failed",
            reason=BUG,
            owner={"worker_epoch": epoch},
            set_={"report": {"reason": BUG, "error_class": error_class}},
        )
    except TransitionConflict:
        return False
    reclaimed = await reclaim(conn, "cycle", {"cycle": cycle_id}, detail=CYCLE_FAILED)
    await raise_alert(
        conn,
        code=FAILED_ALERT,
        severity="critical",
        subject=f"cycle:{cycle_id}",
        message="a score cycle failed",
        context={"cycle_id": cycle_id, "error_class": error_class, "reclaimed": len(reclaimed)},
    )
    log.error("score_cycle_failed", cycle_id=cycle_id, error_class=error_class, reclaimed=len(reclaimed))
    return True


@register_takeover_sweep("score_targets")
async def sweep_older_epoch_claims(conn: AsyncConnection, epoch: int) -> int:
    """§3.3.1 step 3: reclaim every claim of an older epoch (detail `worker_takeover`); the count."""
    return len(await reclaim(conn, "older_epoch", {"epoch": epoch}, detail=WORKER_TAKEOVER))


@register_takeover_sweep("score_cycles")
async def sweep_older_epoch_cycles(conn: AsyncConnection, epoch: int) -> int:
    """§3.3.1 step 4: fail every running cycle of an older epoch `orphaned` (its claims were reclaimed by the
    `score_targets` sweep before it; any left are reclaimed here); the count."""
    cycle_ids = [int(c) for c in (await conn.execute(_OLDER_CYCLES, {"epoch": epoch})).scalars()]
    for cycle_id in cycle_ids:
        await _orphan(conn, cycle_id, detail=WORKER_TAKEOVER)
    return len(cycle_ids)


@reconciler("orphaned_cycles", "tick")
async def reconcile_orphaned_cycles(ctx: ReconcileContext) -> int:
    """Fail every running cycle whose heartbeat is older than `ORPHANED_AFTER` at `ctx.now` `orphaned`
    (module docstring); the count. Fenced by the worker epoch first: the worker's own `FencedOut` propagates
    (Ruling FR-w), another owner's ends the pass with 0, having written nothing."""
    try:
        async with ctx.engine.begin() as conn:
            await fence_epoch(conn, ctx.epoch)
            stale = (await conn.execute(_STALE_CYCLES, {"cutoff": ctx.now - ORPHANED_AFTER})).all()
            for row in stale:
                await _orphan(conn, int(row.id), detail=CYCLE_ORPHANED)
    except FencedOut as fenced:
        if fenced.worker_superseded:
            raise  # `fence_epoch` logged it
        return 0
    for row in stale:  # after the commit: a hung local execution of that cycle stops
        key: ExecKey = ("cycle", int(row.id))
        live = EXECUTIONS.get(key)
        # No await between the check and the cancel: the entry cannot change in between.
        if live is not None and live[0] == int(row.worker_epoch):
            await EXECUTIONS.cancel_and_await(key)
    return len(stale)
