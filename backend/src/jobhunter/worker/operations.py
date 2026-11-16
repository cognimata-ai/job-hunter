"""Durable operations with claim tokens (spec §3.3.2 to §3.3.4, §7.3, §7.16.8, §17.4).

An `operations` row is a durable intent: a mutation that needs follow-up work inserts one in its own
transaction (`create_operation`), and the `run_operation(operation_id)` job only names it. Every execution
claims the row with a fresh `claim_id` (`pending → running`); the heartbeat, every lock of the owner row and
every completion write check `claim_id = :c AND status = 'running'`, and every way out of `running` clears the
claim, its epoch and its heartbeat in the same statement (`transition()`). So an old execution, once its row
was reclaimed, can never satisfy the predicate again, even within the same worker epoch (§18.2 Operation ABA).

The ways out of `running`:
- `finish_operation`: `succeeded`, or `failed` with an error code (the owner's write);
- `requeue_operation`: back to `pending` with `attempts + 1` and a `not_before` (the owner's write);
- `wait_operation`: back to `pending` with a `not_before` and an error code, `attempts` unchanged and never
  `failed` (the owner's write, M5 Task 8): a handler waiting on a condition that has its own end (a purge in
  progress, a running eval run), which a requeue every minute would fail after `MAX_OPERATION_ATTEMPTS`;
- `reclaim_stale_operations` (the `operations` reconciler, heartbeat older than 3 min) and the `operations`
  takeover sweep (an older worker epoch): back to `pending` with `attempts + 1`, no reason;
- `cancel_operation`: `cancelled` (delete-everything, a superseding purge or scope).

A requeue or reclaim that reaches `MAX_OPERATION_ATTEMPTS` ends the row `failed('attempts_exhausted')`
instead. Every permanent failure (a `failed` finish, or exhaustion) raises the `operation.failed` alert (warn,
subject `operation:<id>`) exactly once, in the transaction that ends the row (§7.3; ruling T10-g).

**The global fence** (§20.2, §20.4; M5 Task 8, P3, D15): while delete-everything's `global` purge fence
stands, `claim_operation` claims no operation but a `purge`: it moves the row's `not_before` to the database's
`now()` plus `GLOBAL_FENCE_RETRY`, spends no attempt and returns `None`, so an operation that waits out
delete-everything never fails for it (M4 Ruling T12-b's drain ends here). The `operations` reconciler
re-defers its job once that time has passed.

**Failure hooks** (M2 plan Deviation 13). `register_operation_handler(kind, on_failed=hook)` registers a
kind's hook in `FAILURE_HOOKS` (`HANDLERS` keeps the handlers only). Every permanent failure runs it right
after the `failed` transition, in the same transaction: a `failed` finish with its error code, and an
exhaustion by a requeue (the requeue's code), by the stale reclaim or by the takeover sweep (`None`). So a
domain row the operation drives (a profile draft, a query proposal) never outlives its operation in a live
state. A hook may lock only rows after the owner row in the §9.1 order, and it moves its domain row only while
that row is still live: a row the handler's own write already ended in the same transaction is left as it is.

**The `eval_run` hook** (M3 Task 19, written here; pinned by Task 27's registration test): `eval_run_failed`
ends the run of a failed `eval_run` operation `failed('interrupted')` while it is still `running`
(`fail_eval_run`, which the eval runner shares); `scoring.eval_runner` registers it with the kind's handler.
`batch_submit` has no hook: its cleanup needs `active_config`, an earlier lock than the operation row a hook
runs after, so the `batch_submissions` tick reconciler recovers its rows in a fenced transaction of its own
(Codex r3 B2); a failed `create_targets_for_tuple` needs none (`missing_targets` requests it again).

**Handler-side ends** (M2 Task 11). A handler, or a graph node it runs, ends its operation inside its own
transaction with `OperationContext.finish_in`, `requeue_in` or `wait_in` (M5 Task 8), so the last domain write
and the operation's end commit together. Each records the end in the context's outcome holder, and
`execute_operation`, which reads it once the handler has returned, then writes nothing more for that
execution. Such a call must be part of a transaction that commits before the handler returns (an exception
that rolls it back propagates, and the execution is then requeued as `bug` like any other raise). The context
also carries the worker's `OperationDeps` (`deps`), which the M2 handlers need; an M1 handler runs with
`deps=None`. A handler records its progress (`operations.progress`, which the API polls) with `set_progress`,
under the claim predicate like every owner write (M2 Task 18).

**Execution locks** (M3 Task 14, Codex r3 B20). A kind of `OPERATION_LOCKS` runs its jobs under that
procrastinate lock (`batch_submit`: `batch`, one submission at a time, worker-side; `replay_raw_items`:
`replay`, so two replays whose id sets overlap never resolve one item at once, M4 Task 12; `purge`: `purge`,
one purge at a time, M5 Task 12):
`defer_operation_job` reads the row's kind and passes the lock on every enqueue, the first one
(`create_operation`), the `operations` reconciler's re-enqueue of a lost job and a fenced delivery's re-defer
alike.

`execute_operation` is the task body's protocol; it runs the kind's handler through `executions.run_owned`,
the one ownership-loss protocol shared with runs. A claim that the epoch fence refuses (a superseded worker,
`FencedOut`) re-defers the operation's job under its queueing lock `op:<id>` instead of consuming it (M0
carry-forward 6, plan Deviation 18), so the current worker claims it at once; first it asks its own worker
for the graceful stop (`on_superseded`, ruling T24-a), so the superseded worker's loops fetch it no more.
Every time comparison of the
sweeps and reconcilers uses the caller's clock (`ctx.now`, or `now`), never the database's `now()`.

This module is a task boundary (ruff `BLE001` is allowed here); it needs no broad catch, because `run_owned`
reports an unexpected handler error as an outcome, which `execute_operation` persists as a requeue (`bug`).
"""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from jobhunter.clock import utcnow
from jobhunter.db.guards import global_fence_active
from jobhunter.db.sql import q
from jobhunter.db.transitions import transition, transition_where
from jobhunter.errors import FencedOut, TransitionConflict
from jobhunter.faults import afault_point
from jobhunter.observability.alerts import raise_alert
from jobhunter.observability.logging import get_logger
from jobhunter.types import JSONValue
from jobhunter.worker.epoch import fence_epoch, register_takeover_sweep
from jobhunter.worker.executions import EXECUTIONS, AlreadyExecuting, ExecKey, ExecutionRegistry, run_owned
from jobhunter.worker.reconcilers import ReconcileContext, reconciler
from jobhunter.worker.tasks import Deferrer, defer_in_tx, job_task_name

if TYPE_CHECKING:
    # Only for the annotations of `OperationDeps`: the gateway, the profile graphs and the embedder pull in
    # the SDK, langgraph and modules that import this one; the api, which creates operations, needs none.
    from jobhunter.config import AppConfig
    from jobhunter.embeddings import EmbedderClient
    from jobhunter.graphs.profile.graph import ProfileGraphs
    from jobhunter.graphs.score.deps import ScoreDeps
    from jobhunter.llm.batches import BatchesClient
    from jobhunter.llm.gateway import LLMGateway
    from jobhunter.observability.deadman import DeadmanPinger
    from jobhunter.profile.parse_client import ParseClient
    from jobhunter.profile.storage import CvStore

OperationKind = Literal[
    "extract_profile_text",
    "extract_profile",
    "embed_profile",
    "propose_queries",
    "apply_scope",
    "create_targets_for_tuple",
    "batch_submit",
    "export",
    "purge",
    "eval_run",
    "watchlist_validate",
    "replay_raw_items",  # M4 Task 12 registers its handler (migration 0009 adds it to the CHECK)
]

# The procrastinate execution lock of a kind's `run_operation` job, applied at every enqueue
# (`defer_operation_job`): the batch submissions run one at a time, worker-side (M3 Task 14, L284, A2 R4), and
# so do the replays of rejected raw items (M4 Task 12, Ruling T12-c: overlapping id sets) and the purges (M5
# Task 12, D5: one purge at a time, so execution follows the manifests' UUIDv7 stream order).
OPERATION_LOCKS: Final[Mapping[OperationKind, str | None]] = MappingProxyType(
    {"batch_submit": "batch", "replay_raw_items": "replay", "purge": "purge"}
)
MAX_OPERATION_ATTEMPTS: Final = 10
# These thresholds compare times the database wrote (`heartbeat_at`, `created_at`: its `now()`) with the
# caller's clock (`ctx.now`, the worker's `utcnow()`). That is safe because the worker and PostgreSQL run on
# one VPS and read its one kernel clock, so the two never drift apart (ruling T21-d). If the database moves to
# another host, their skew shifts every threshold: a worker clock ahead of the database's reclaims live claims
# early (their owners are then fenced in mid-work), one behind reclaims dead ones late; the cutoffs then have
# to be computed in SQL from the database's `now()`.
OPERATION_STALE: Final = timedelta(minutes=3)
REDEFER_AFTER: Final = timedelta(minutes=2)
# At most this many lost jobs are re-deferred per tick; the next tick takes the rest.
REDEFER_BATCH: Final = 100
# How long a requeued operation waits before it can be claimed again: after an unexpected handler error
# (`bug`) or a refused local start (`already_executing`).
RETRY_DELAY: Final = timedelta(minutes=1)
# How long an operation the global fence kept from its claim waits before it is claimable again (module
# docstring, The global fence).
GLOBAL_FENCE_RETRY: Final = timedelta(minutes=1)
# The one kind the global fence lets through: delete-everything's own (and every other purge's).
PURGE_KIND: Final = "purge"
# A purge operation's dedup key is this prefix and its manifest's uuid: A0's (`privacy.purge.open_purge`), the
# `purge_fences` reconciler's and the retry route's operations of one manifest share it (M5 Task 12).
PURGE_DEDUP_PREFIX: Final = "purge:"

FAILED_ALERT_CODE: Final = "operation.failed"
# The error code of an operation that a requeue or reclaim took past its last attempt.
ATTEMPTS_EXHAUSTED: Final = "attempts_exhausted"
_TABLE: Final = "operations"
# How often `create_operation` retries when the live row it conflicted with ended before it could be read.
_CREATE_TRIES: Final = 3

log = get_logger("jobhunter.worker.operations")


@dataclass(frozen=True)
class OperationClaim:
    """One execution's claim of an operation. `attempts` is the row's count when it was claimed (the
    failed tries before this one), constant for the claim: the M2 threads are named by it (Deviation 27)."""

    operation_id: int
    claim_id: UUID
    kind: OperationKind
    payload: Mapping[str, JSONValue]
    attempts: int


@dataclass(frozen=True)
class OperationDeps:
    """What the M2 and M3 handlers need from the worker process, built once in `worker.main` (M2 Task 11, M3
    Task 27).

    `embedder` is `None` when the worker has none, as for `WorkerRuntime.embedder`; `clock` is the injected
    clock of the handlers' times. `score_steps` are the score steps' dependencies (`WorkerRuntime.score_deps`:
    the engine, the gateway, the clock and the gateway's monotonic clock), which the `eval_run` handler scores
    its cases with; `batches_client` is the process's Message Batches client (`WorkerRuntime.batches_client`),
    which the `batch_submit` handler creates its batch with. Neither handler builds its own (Ruling P33).
    `deadman` is the process's dead-man pinger (`WorkerRuntime.deadman`, M5 Task 5), `None` without one: with
    the Batches client, a live purge's external effects (`privacy.purge.LiveEffects`, M5 Task 12, Deviation
    56). It is last and optional, so a test that builds the dependencies without it has none.
    """

    cfg: "AppConfig"
    gateway: "LLMGateway"
    parse_client: "ParseClient"
    cv_store: "CvStore"
    profile_graphs: "ProfileGraphs"
    embedder: "EmbedderClient | None"
    clock: Callable[[], datetime]
    score_steps: "ScoreDeps"
    batches_client: "BatchesClient"
    deadman: "DeadmanPinger | None" = None


@dataclass(frozen=True)
class OperationEnd:
    """How a handler ended its operation in its own transaction: `finished` (`succeeded` or `failed`),
    `requeued` (`pending`, or `failed` when that was its last attempt) or `waited` (`pending`, no attempt
    spent)."""

    kind: Literal["finished", "requeued", "waited"]
    status: Literal["succeeded", "failed", "pending"]
    error_code: str | None


@dataclass
class _Outcome:
    """The context's mutable outcome holder (the context itself is frozen)."""

    end: OperationEnd | None = None


@dataclass(frozen=True)
class OperationContext:
    """What a handler runs with: its claim, the engine, and the worker's `deps` (`None` for M1 handlers)."""

    claim: OperationClaim
    engine: AsyncEngine
    deps: OperationDeps | None = None
    _outcome: _Outcome = field(default_factory=_Outcome, init=False, repr=False, compare=False)

    @property
    def ended(self) -> OperationEnd | None:
        """The end a handler-side `finish_in` or `requeue_in` recorded, or `None`."""
        return self._outcome.end

    async def finish_in(
        self,
        conn: AsyncConnection,
        *,
        status: Literal["succeeded", "failed"],
        error_code: str | None = None,
    ) -> None:
        """End the claimed operation `succeeded` or `failed(error_code)` in the caller's transaction (with its
        alert and failure hook, `finish_operation`), so the caller's last domain write and the operation's
        end commit together; `execute_operation` writes nothing more for this execution."""
        await finish_operation(conn, self.claim, status=status, error_code=error_code)
        self._outcome.end = OperationEnd("finished", status, error_code)

    async def requeue_in(self, conn: AsyncConnection, *, error_code: str, not_before: datetime) -> None:
        """Give the claimed operation back for a retry after `not_before` in the caller's transaction, under
        the attempt limit and the failure hook of `requeue_operation`; `execute_operation` writes nothing
        more."""
        status = await requeue_operation(conn, self.claim, error_code=error_code, not_before=not_before)
        self._outcome.end = OperationEnd("requeued", status, error_code)

    async def wait_in(self, conn: AsyncConnection, *, error_code: str, not_before: datetime) -> None:
        """Give the claimed operation back until `not_before` in the caller's transaction without spending an
        attempt (`wait_operation`), for a wait on a condition that has its own end; `execute_operation`
        writes nothing more."""
        await wait_operation(conn, self.claim, error_code=error_code, not_before=not_before)
        self._outcome.end = OperationEnd("waited", "pending", error_code)


OperationHandler = Callable[[OperationContext], Awaitable[None]]
# Called with the operation's id, its payload and the last error code (a `failed` finish's, a requeue's, or
# `None` after a reclaim or the takeover sweep), in the transaction that ended the operation `failed`.
type FailureHook = Callable[[AsyncConnection, int, Mapping[str, JSONValue], str | None], Awaitable[None]]

# The handler of each kind; the modules that own a kind register it (none in M0).
HANDLERS: dict[OperationKind, OperationHandler] = {}
# The failure hook of a kind that has one (M2 plan Deviation 13).
FAILURE_HOOKS: dict[OperationKind, FailureHook] = {}


def register_operation_handler(
    kind: OperationKind, *, on_failed: FailureHook | None = None
) -> Callable[[OperationHandler], OperationHandler]:
    """Decorator: run the function for every operation of `kind`, and `on_failed` (when given) in every
    transaction that ends one `failed` (module docstring)."""

    def register(handler: OperationHandler) -> OperationHandler:
        registered = HANDLERS.get(kind)
        if registered is not None and registered is not handler:
            raise ValueError(f"another handler is already registered for operation kind {kind!r}")
        if on_failed is not None:
            hook = FAILURE_HOOKS.get(kind)
            if hook is not None and hook is not on_failed:
                raise ValueError(f"another failure hook is already registered for operation kind {kind!r}")
            FAILURE_HOOKS[kind] = on_failed
        HANDLERS[kind] = handler
        return handler

    return register


# ---- the `eval_run` failure hook (M3 Task 19; Task 27 pins it) ---------------------------------------------

EVAL_RUN_INTERRUPTED: Final = "interrupted"
_BIGINT_END: Final = 2**63
# What the ledger counts for an eval run's calls (§6.9.1): billed at their cost, not billed at 0, the rest
# (reserved, dispatched, ambiguous) at their reservation; `eval_runs.cost_usd` is numeric(10,4), rounded half
# up. Informational: the ledger, never the run row, is the authority for spend.
EVAL_RUN_COST_SQL: Final = (
    "(SELECT round(coalesce(sum(CASE l.state WHEN 'billed' THEN l.settled_usd WHEN 'not_billed' THEN 0 "
    "ELSE l.reserved_usd END), 0), 4) FROM llm_calls c JOIN llm_spend_ledger l ON l.id = c.spend_ledger_id "
    "WHERE c.eval_run_id = t.id)"
)


async def fail_eval_run(conn: AsyncConnection, eval_run_id: int, reason: str) -> bool:
    """End eval run `eval_run_id` `running → failed` with `metrics || {"failed": reason}` and the `cost_usd`
    its calls count (`EVAL_RUN_COST_SQL`), in the caller's transaction; `False`, writing nothing, when the
    run is not `running` (already `completed` or `failed`: left as it is) or does not exist."""
    moved = await transition_where(
        conn,
        "eval_runs",
        ["running"],
        "failed",
        where="t.id = :run",
        params={"run": eval_run_id, "reason": reason},
        set_sql={
            "metrics": "coalesce(t.metrics, CAST('{}' AS jsonb)) || jsonb_build_object('failed', "
            "CAST(:reason AS text))",
            "cost_usd": EVAL_RUN_COST_SQL,
        },
    )
    return bool(moved)


async def eval_run_failed(
    conn: AsyncConnection, operation_id: int, payload: Mapping[str, JSONValue], error_code: str | None
) -> None:
    """The `eval_run` operation's failure hook (M3 Task 19, Codex r5 B2): its run, while still `running`,
    ends `failed` with `metrics.failed = "interrupted"` (`fail_eval_run`); a run already `completed` or
    `failed` is left as it is. It writes the run row after the operation row the finishing transaction
    holds (steps 5 and 10 of the lock schedule) and needs no earlier lock."""
    run = payload.get("eval_run_id")
    if isinstance(run, bool) or not isinstance(run, int) or not 0 < run < _BIGINT_END:
        log.error("eval_run_hook_payload_invalid", operation_id=operation_id)
        return  # no run a `bigint` names: the operation still ends `failed`
    if await fail_eval_run(conn, run, EVAL_RUN_INTERRUPTED):
        log.warning("eval_run_failed", operation_id=operation_id, eval_run_id=run, error_code=error_code)


async def _run_failure_hook(
    conn: AsyncConnection,
    kind: OperationKind,
    operation_id: int,
    payload: Mapping[str, JSONValue],
    error_code: str | None,
) -> None:
    hook = FAILURE_HOOKS.get(kind)
    if hook is not None:
        await hook(conn, operation_id, payload, error_code)


# The conflict target repeats the index `operations_live_dedup` of §7.3 exactly:
# `(kind, dedup_key) WHERE status IN ('pending','running')`.
_INSERT = q(
    "INSERT INTO operations (kind, payload, dedup_key, status) "
    "VALUES (:kind, :payload, :dedup_key, 'pending') "
    "ON CONFLICT (kind, dedup_key) WHERE status IN ('pending','running') DO NOTHING RETURNING id",
    jsonb=["payload"],
)
_KIND = q("SELECT kind FROM operations WHERE id = :i")
_LIVE_ID = q(
    "SELECT id FROM operations "
    "WHERE kind = :kind AND dedup_key = :dedup_key AND status IN ('pending','running')"
)
_CLAIMABLE = q(
    "SELECT kind, payload, attempts FROM operations "
    "WHERE id = :i AND status = 'pending' AND (not_before IS NULL OR not_before <= now()) FOR UPDATE"
)
# The global fence's postponement (module docstring): the status stays `pending`, so no transition.
_POSTPONE = q("UPDATE operations SET not_before = now() + :retry WHERE id = :i")
_HEARTBEAT = q(
    "UPDATE operations SET heartbeat_at = now() "
    "WHERE id = :i AND claim_id = :c AND status = 'running' RETURNING id"
)
_LOCK_OWNER = q("SELECT id FROM operations WHERE id = :i AND claim_id = :c AND status = 'running' FOR SHARE")
_SET_PROGRESS = q(
    "UPDATE operations SET progress = :progress "
    "WHERE id = :i AND claim_id = :c AND status = 'running' RETURNING id",
    jsonb=["progress"],
)
# The three reads that lead to `_release` also read the kind and the payload: its failure hook needs both.
_OWNED_ATTEMPTS = q(
    "SELECT attempts, kind, payload FROM operations "
    "WHERE id = :i AND claim_id = :c AND status = 'running' FOR UPDATE"
)
_STALE = q(
    "SELECT id, claim_id, attempts, kind, payload FROM operations "
    "WHERE status = 'running' AND heartbeat_at < :cutoff ORDER BY id FOR UPDATE SKIP LOCKED"
)
# Not SKIP LOCKED, unlike `_STALE`: the takeover must take back every operation of an older epoch (§3.3.1
# step 5), so it waits for an old owner's row lock (a heartbeat, `lock_operation_owner`) instead of leaving
# that claim valid. The takeover's `lock_timeout` bounds the wait, as for the `runs` sweep.
_OLDER_EPOCH = q(
    "SELECT id, claim_id, attempts, kind, payload FROM operations "
    "WHERE status = 'running' AND worker_epoch < :epoch ORDER BY id FOR UPDATE"
)
# Due `pending` operations without a live job (the predicate of `tasks.live_job_exists`, inline so that
# only real candidates are locked and the batch is never filled by rows that still have their job).
# `operations_pending` serves the `status = 'pending'` scan.
_REDEFER_DUE = q(
    "SELECT o.id FROM operations o WHERE o.status = 'pending' "
    "AND greatest(o.created_at, coalesce(o.not_before, o.created_at)) <= :cutoff "
    "AND NOT EXISTS (SELECT 1 FROM procrastinate_jobs j WHERE j.task_name = :task "
    "AND j.status IN ('todo', 'doing') AND j.args @> jsonb_build_object('operation_id', o.id)) "
    "ORDER BY o.id LIMIT :limit FOR UPDATE OF o SKIP LOCKED"
)


async def create_operation(
    conn: AsyncConnection,
    *,
    kind: OperationKind,
    payload: Mapping[str, JSONValue],
    dedup_key: str,
    defer: Deferrer | None,
) -> tuple[int, bool]:
    """The live (`pending` or `running`) operation of `kind` and `dedup_key`, inserted when there is none.

    Returns `(id, created)`. Only a new row defers its job, `run_operation(operation_id=id)` under the
    queueing lock `op:<id>`, in the caller's transaction (`defer=None` defers nothing; the `operations`
    reconciler re-defers it after `REDEFER_AFTER`). `payload` holds ids only, never content.
    """
    params = {"kind": kind, "payload": dict(payload), "dedup_key": dedup_key}
    for _ in range(_CREATE_TRIES):
        inserted = (await conn.execute(_INSERT, params)).scalar_one_or_none()
        if inserted is not None:
            operation_id = int(inserted)
            if defer is not None:
                await defer_operation_job(conn, defer, operation_id)
            return operation_id, True
        # The insert met a live row; a statement later it may have ended, and the next insert then succeeds.
        live = (await conn.execute(_LIVE_ID, {"kind": kind, "dedup_key": dedup_key})).scalar_one_or_none()
        if live is not None:
            return int(live), False
    raise RuntimeError(f"operations: the live {kind} row of a dedup key kept ending during its creation")


async def defer_operation_job(conn: AsyncConnection, defer: Deferrer, operation_id: int) -> int | None:
    """Defer `run_operation(operation_id)` with `defer` under the queueing lock `op:<id>` and the execution
    lock of the operation's kind (`OPERATION_LOCKS`, read from its row), in the caller's transaction: the job
    id, or `None` when that operation's job already waits. Every enqueue goes through here: the first one,
    `_redefer_lost_jobs`' and a fenced delivery's re-defer (M3 Task 14, Codex r3 B20)."""
    kind = (await conn.execute(_KIND, {"i": operation_id})).scalar_one_or_none()
    lock = None if kind is None else OPERATION_LOCKS.get(kind)
    return await defer(
        conn, "run_operation", lock=lock, queueing_lock=f"op:{operation_id}", operation_id=operation_id
    )


async def claim_operation(conn: AsyncConnection, operation_id: int, *, epoch: int) -> OperationClaim | None:
    """Claim a `pending` operation whose `not_before` has passed, with a fresh `claim_id`; `None` otherwise
    (absent, not `pending`, not due, claimed meanwhile, or kept back by the global fence).

    `pending → running` sets the claim, the worker `epoch` and the first heartbeat in one statement. Raises
    `FencedOut`, having written nothing, when `epoch` is no longer the current worker epoch
    (`epoch.fence_epoch`, the first statement, before the row is locked; plan Deviation 18): a superseded
    worker claims nothing, and its caller re-defers the job. While delete-everything's `global` fence stands,
    an operation of any kind but `purge` is not claimed: its `not_before` moves to `now() +
    GLOBAL_FENCE_RETRY`, its attempts stay as they are, and `None` is returned (module docstring, The global
    fence).
    """
    await fence_epoch(conn, epoch)
    row = (await conn.execute(_CLAIMABLE, {"i": operation_id})).first()
    if row is None:
        return None
    if row.kind != PURGE_KIND and await global_fence_active(conn):
        await conn.execute(_POSTPONE, {"i": operation_id, "retry": GLOBAL_FENCE_RETRY})
        return None
    claim_id = uuid4()
    try:
        await transition(
            conn,
            _TABLE,
            operation_id,
            ["pending"],
            "running",
            set_={"claim_id": claim_id, "worker_epoch": epoch, "heartbeat_at": sa.func.now()},
        )
    except TransitionConflict:
        return None
    return OperationClaim(
        operation_id=operation_id,
        claim_id=claim_id,
        kind=row.kind,
        payload=row.payload,
        attempts=int(row.attempts),
    )


def _owner(claim: OperationClaim) -> dict[str, object]:
    return {"i": claim.operation_id, "c": claim.claim_id}


async def heartbeat_operation(conn: AsyncConnection, claim: OperationClaim) -> bool:
    """Record a heartbeat of `claim`; `False` when the claim no longer owns the row (stop the execution)."""
    return (await conn.execute(_HEARTBEAT, _owner(claim))).first() is not None


async def lock_operation_owner(conn: AsyncConnection, claim: OperationClaim) -> None:
    """Hold the owner row `FOR SHARE` for the transaction, or raise `FencedOut` when `claim` lost it.

    Every reservation, dispatch marker and domain write made on the operation's behalf runs after it.
    """
    if (await conn.execute(_LOCK_OWNER, _owner(claim))).first() is None:
        raise FencedOut("operation")


async def set_progress(
    conn: AsyncConnection, claim: OperationClaim, progress: Mapping[str, JSONValue]
) -> None:
    """Replace the claimed operation's `progress` (counts and flags only, never content; the API polls it),
    in the caller's transaction; `FencedOut`, having written nothing, when `claim` lost the row (M2 Task 18).

    It updates the owner row, so a transaction that waits on that row's update (an `INSERT … ON CONFLICT` on
    `operations_live_dedup` that meets this live row) waits until the caller commits: a caller that holds a
    long transaction makes it the last statement (the scope apply's chunk does).
    """
    if (await conn.execute(_SET_PROGRESS, {**_owner(claim), "progress": dict(progress)})).first() is None:
        raise FencedOut("operation")


async def finish_operation(
    conn: AsyncConnection,
    claim: OperationClaim,
    *,
    status: Literal["succeeded", "failed"],
    error_code: str | None = None,
) -> None:
    """End the claimed operation `succeeded` or `failed(error_code)`; `FencedOut` when `claim` lost the row.

    A `failed` end needs an `error_code`, and raises the `operation.failed` alert and runs the kind's failure
    hook (with `error_code`) in the same transaction (ruling T10-g, Deviation 13), so no caller can forget
    them; a fenced finish runs neither.
    """
    if status == "failed" and error_code is None:
        raise ValueError("a failed operation needs an error code")
    try:
        await transition(
            conn,
            _TABLE,
            claim.operation_id,
            ["running"],
            status,
            reason=error_code,
            owner={"claim_id": claim.claim_id},
        )
    except TransitionConflict as exc:
        raise FencedOut("operation") from exc
    if status == "failed":
        await _raise_failed_alert(conn, claim.operation_id)
        await _run_failure_hook(conn, claim.kind, claim.operation_id, claim.payload, error_code)


async def _raise_failed_alert(conn: AsyncConnection, operation_id: int) -> None:
    await raise_alert(
        conn,
        code=FAILED_ALERT_CODE,
        severity="warn",
        subject=f"operation:{operation_id}",
        message="operation failed permanently",
    )


async def _release(
    conn: AsyncConnection,
    operation_id: int,
    claim_id: UUID,
    attempts: int,
    *,
    kind: OperationKind,
    payload: Mapping[str, JSONValue],
    reason: str | None,
    not_before: datetime | None,
) -> Literal["pending", "failed"]:
    """Take the row from the execution `claim_id`: back to `pending` with one more attempt, or `failed`.

    The one copy of the attempts rule, for the owner's requeue, the stale reclaim and the takeover sweep. An
    exhaustion raises the alert and runs the failure hook of `kind` with `payload` and `reason` (the
    requeue's code, `None` for a reclaim) in the caller's transaction. `TransitionConflict` propagates when
    `claim_id` no longer owns the row.
    """
    owner = {"claim_id": claim_id}
    attempts_after = attempts + 1
    if attempts_after >= MAX_OPERATION_ATTEMPTS:
        await transition(
            conn,
            _TABLE,
            operation_id,
            ["running"],
            "failed",
            reason=ATTEMPTS_EXHAUSTED,
            owner=owner,
            set_={"attempts": MAX_OPERATION_ATTEMPTS},
        )
        await _raise_failed_alert(conn, operation_id)
        await _run_failure_hook(conn, kind, operation_id, payload, reason)
        log.warning("operation_attempts_exhausted", operation_id=operation_id)
        return "failed"
    await transition(
        conn,
        _TABLE,
        operation_id,
        ["running"],
        "pending",
        reason=reason,
        owner=owner,
        set_={"attempts": attempts_after, "not_before": not_before},
    )
    return "pending"


async def requeue_operation(
    conn: AsyncConnection, claim: OperationClaim, *, error_code: str, not_before: datetime
) -> Literal["pending", "failed"]:
    """Give the claimed operation back for a retry after `not_before`, recording `error_code`.

    Returns the new status: `failed` (with the `operation.failed` alert and the kind's failure hook, given
    `error_code`) when this was the last attempt. Raises `FencedOut`, having written nothing, when `claim` no
    longer owns the row.
    """
    row = (await conn.execute(_OWNED_ATTEMPTS, _owner(claim))).first()
    if row is None:
        raise FencedOut("operation")
    try:
        return await _release(
            conn,
            claim.operation_id,
            claim.claim_id,
            int(row.attempts),
            kind=row.kind,
            payload=row.payload,
            reason=error_code,
            not_before=not_before,
        )
    except TransitionConflict as exc:
        raise FencedOut("operation") from exc


async def wait_operation(
    conn: AsyncConnection, claim: OperationClaim, *, error_code: str, not_before: datetime
) -> None:
    """Give the claimed operation back, `running → pending` with `not_before` and `error_code`, its attempts
    unchanged and never `failed` (module docstring): the handler-side form of the global fence's claim rule,
    for a wait on a condition that has its own end. Raises `FencedOut`, having written nothing, when `claim`
    no longer owns the row."""
    try:
        await transition(
            conn,
            _TABLE,
            claim.operation_id,
            ["running"],
            "pending",
            reason=error_code,
            owner={"claim_id": claim.claim_id},
            set_={"not_before": not_before},
        )
    except TransitionConflict as exc:
        raise FencedOut("operation") from exc


async def cancel_operation(conn: AsyncConnection, operation_id: int, *, reason: str) -> bool:
    """End a `pending` or `running` operation `cancelled(reason)`; `False` when it had already ended.

    A running execution is fenced by it (its next heartbeat or owner write finds no row). To stop a local
    execution at once, the caller cancels `("operation", operation_id)` in `EXECUTIONS` after its commit.
    """
    try:
        await transition(conn, _TABLE, operation_id, ["pending", "running"], "cancelled", reason=reason)
    except TransitionConflict:
        return False
    return True


async def reclaim_stale_operations(conn: AsyncConnection, *, now: datetime) -> list[int]:
    """Take back every `running` operation whose heartbeat is older than `OPERATION_STALE` at `now`.

    Each goes back to `pending` (or ends `failed` on its last attempt); rows locked by an in-flight
    heartbeat or owner write are skipped this time. Returns the ids taken back, in id order.
    """
    return [operation_id for operation_id, _ in await _reclaim_stale(conn, now=now)]


async def _reclaim_stale(conn: AsyncConnection, *, now: datetime) -> list[tuple[int, UUID]]:
    """`reclaim_stale_operations`, returning each id with the claim it was taken from."""
    rows = (await conn.execute(_STALE, {"cutoff": now - OPERATION_STALE})).all()
    for row in rows:
        await _release(
            conn,
            row.id,
            row.claim_id,
            int(row.attempts),
            kind=row.kind,
            payload=row.payload,
            reason=None,
            not_before=None,
        )
    return [(int(row.id), row.claim_id) for row in rows]


@register_takeover_sweep("operations")
async def sweep_older_epoch_operations(conn: AsyncConnection, epoch: int) -> int:
    """At takeover: take back every `running` operation of an older worker epoch; the count.

    It waits for the row locks of those owners (none is skipped), so once its transaction commits no older
    claim is valid any more.
    """
    rows = (await conn.execute(_OLDER_EPOCH, {"epoch": epoch})).all()
    for row in rows:
        await _release(
            conn,
            row.id,
            row.claim_id,
            int(row.attempts),
            kind=row.kind,
            payload=row.payload,
            reason=None,
            not_before=None,
        )
    return len(rows)


async def _redefer_lost_jobs(conn: AsyncConnection, *, now: datetime, defer: Deferrer) -> int:
    """Defer `run_operation` again for up to `REDEFER_BATCH` due `pending` operations without a live job.

    Only those candidates are locked (`FOR UPDATE SKIP LOCKED`). Returns how many jobs were deferred.
    """
    params = {"cutoff": now - REDEFER_AFTER, "task": job_task_name("run_operation"), "limit": REDEFER_BATCH}
    redeferred = 0
    for operation_id in (await conn.execute(_REDEFER_DUE, params)).scalars().all():
        job_id = await defer_operation_job(conn, defer, int(operation_id))
        redeferred += job_id is not None
    return redeferred


@reconciler("operations", "tick")
async def reconcile_operations(ctx: ReconcileContext) -> int:
    """Re-defer lost jobs, then reclaim stale executions and stop their local tasks; rows repaired.

    A `pending` operation without a live job is re-deferred once `ctx.now` is `REDEFER_AFTER` past its
    creation and its `not_before` (at most `REDEFER_BATCH` per tick). A `running` one whose heartbeat is
    older than `OPERATION_STALE` is taken back; after that commit, its local execution is cancelled and
    awaited when, and only when, it runs for the claim that was taken back (its registry token).

    Both transactions are fenced by `ctx.epoch` first, before any operation row is locked (Ruling FR-h): a
    superseded worker raises `FencedOut` having written nothing, which stops its cadence.
    """
    async with ctx.engine.begin() as conn:
        await fence_epoch(conn, ctx.epoch)
        redeferred = await _redefer_lost_jobs(conn, now=ctx.now, defer=ctx.defer)
    async with ctx.engine.begin() as conn:
        await fence_epoch(conn, ctx.epoch)
        reclaimed = await _reclaim_stale(conn, now=ctx.now)
    for operation_id, claim_id in reclaimed:
        key: ExecKey = ("operation", operation_id)
        live = EXECUTIONS.get(key)
        # No await between the check and the cancel: the entry cannot change in between.
        if live is not None and live[0] == claim_id:
            await EXECUTIONS.cancel_and_await(key)
    return redeferred + len(reclaimed)


async def _requeue(engine: AsyncEngine, claim: OperationClaim, *, error_code: str) -> None:
    """Requeue `claim` in a transaction of its own; a fenced claim returns quietly (it wrote nothing)."""
    try:
        async with engine.begin() as conn:
            status = await requeue_operation(
                conn, claim, error_code=error_code, not_before=utcnow() + RETRY_DELAY
            )
    except FencedOut:
        log.info("operation_fenced", operation_id=claim.operation_id, write="requeue")
        return
    log.info("operation_requeued", operation_id=claim.operation_id, error_code=error_code, status=status)


async def _finish(
    engine: AsyncEngine,
    claim: OperationClaim,
    *,
    status: Literal["succeeded", "failed"],
    error_code: str | None = None,
) -> None:
    """Finish `claim` in a transaction of its own (a `failed` finish raises `operation.failed` in it)."""
    try:
        async with engine.begin() as conn:
            await finish_operation(conn, claim, status=status, error_code=error_code)
    except FencedOut:
        log.info("operation_fenced", operation_id=claim.operation_id, write="finish")
        return
    log.info("operation_finished", operation_id=claim.operation_id, status=status, error_code=error_code)


async def _invoke(handler: OperationHandler, ctx: OperationContext) -> None:
    """The handler call as a coroutine of its own: a refused start closes it before the handler is called."""
    await handler(ctx)


def _log_handler_end(operation_id: int, end: OperationEnd) -> None:
    """The log line of an end the handler wrote itself (`finish_in`, `requeue_in`, `wait_in`), once it has
    returned."""
    if end.kind == "finished":
        log.info(
            "operation_finished", operation_id=operation_id, status=end.status, error_code=end.error_code
        )
    elif end.kind == "waited":
        log.info("operation_waiting", operation_id=operation_id, error_code=end.error_code)
    else:
        log.info(
            "operation_requeued", operation_id=operation_id, error_code=end.error_code, status=end.status
        )


async def execute_operation(
    engine: AsyncEngine,
    operation_id: int,
    *,
    epoch: int,
    deps: OperationDeps | None = None,
    registry: ExecutionRegistry = EXECUTIONS,
    heartbeat_period_s: float = 30.0,
    on_superseded: Callable[[], None] = lambda: None,
) -> None:
    """Claim and run operation `operation_id` (the `run_operation` task body), one protocol for every end.

    1. Claim it; return when it is not claimable. When `epoch` is no longer the current worker epoch
       (`FencedOut` with `worker_superseded`; a fence of any other owner propagates, F5 review Minor 1),
       `on_superseded()` asks this worker for its graceful stop (ruling T24-a; the task body
       requests the runtime's shutdown), then the job is deferred again, `run_operation(operation_id)` under
       `op:<id>` in a transaction of its own (deduplicated by that queueing lock while one waits): the
       current worker claims it, the stopping one fetches no more. A kind without a handler ends
       `failed('no_handler')` with the `operation.failed` alert and the kind's failure hook (both run by
       `finish_operation`).
    2. Run the handler through `run_owned(("operation", id), claim_id, …)` with `OperationContext(claim,
       engine, deps)`: the registry's task for the operation, next to the heartbeat task
       `heartbeat:operation:<id>` whose loss cancels it. `registry` is the process's `EXECUTIONS` (its local
       exclusion: `AlreadyExecuting`, and the one the `operations` reconciler cancels in); a test that plays
       a second process's execution passes one of its own (C35).
    3. By outcome: `lost` (a refused or failed heartbeat, a reconciler's or the shutdown's cancel, even one
       the handler swallowed, or `FencedOut`) returns with no further owner write; `raised` requeues with
       `error_code='bug'` and `not_before = now + RETRY_DELAY`; `returned` finishes `succeeded`, unless the
       handler already ended the operation itself (`finish_in`, `requeue_in`, `wait_in`: the context's
       outcome holder), which is then only logged, with no further write. A local execution that is still
       live for the operation (`AlreadyExecuting`) requeues the new claim as `already_executing`.

    Finish and requeue each run in a transaction of their own; their `FencedOut` is caught outside it, and the
    fenced transaction wrote nothing, because its `transition()` is its first write. The caller's own
    cancellation propagates (the work is cancelled first) and nothing is written: the row's next owner
    (reclaim or takeover) records the outcome.
    """
    await afault_point("operation.before_claim")
    try:
        async with engine.begin() as conn:
            claim = await claim_operation(conn, operation_id, epoch=epoch)
    except FencedOut as fenced:  # the fenced transaction wrote nothing (`fence_epoch` logged it)
        if not fenced.worker_superseded:
            raise  # not this worker's own fence: a failure of the delivery, never a reason to stop
        on_superseded()  # first: the loops stop fetching while the re-defer below commits
        async with engine.begin() as conn:
            job_id = await defer_operation_job(conn, defer_in_tx, operation_id)
        log.info("operation_redeferred", operation_id=operation_id, deduplicated=job_id is None)
        return
    if claim is None:
        log.info("operation_not_claimable", operation_id=operation_id)
        return
    handler = HANDLERS.get(claim.kind)
    if handler is None:
        log.error("operation_without_handler", operation_id=operation_id, kind=claim.kind)
        await _finish(engine, claim, status="failed", error_code="no_handler")
        return

    async def beat() -> bool:
        async with engine.begin() as conn:
            return await heartbeat_operation(conn, claim)

    ctx = OperationContext(claim=claim, engine=engine, deps=deps)
    try:
        outcome = await run_owned(
            ("operation", operation_id),
            claim.claim_id,
            _invoke(handler, ctx),
            beat=beat,
            period_s=heartbeat_period_s,
            registry=registry,
        )
    except AlreadyExecuting:
        log.warning("operation_already_executing", operation_id=operation_id)
        await _requeue(engine, claim, error_code="already_executing")
        return
    match outcome.status:
        case "lost":
            return
        case "raised":
            # The class only: an exception message may carry content.
            log.error(
                "operation_handler_failed",
                operation_id=operation_id,
                kind=claim.kind,
                error_class=type(outcome.error).__name__,
            )
            await _requeue(engine, claim, error_code="bug")
        case "returned":
            if ctx.ended is not None:  # the handler's own transaction ended it
                _log_handler_end(operation_id, ctx.ended)
                return
            await _finish(engine, claim, status="succeeded")
