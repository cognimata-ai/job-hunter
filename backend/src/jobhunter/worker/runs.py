"""Collection runs: admission, attempts, heartbeats, the fenced finalize and the run reconcilers.

Spec §3.3.3, §3.3.4, §7.6, §7.16.4 and §17.4 to §17.6.

A run is owned by its current attempt. The ownership predicate is `attempt_no = :a AND status = 'running'`
(§3.3.3): the heartbeat (`heartbeat_run`), the finalize (`finalize_run`) and every later owner write check it.
Every attempt has a checkpoint thread of its own, `run-<run_id>-a<attempt_no>` (`thread_id`, mirrored in
`runs.current_thread_id`), and a `run_attempts` row that records the worker epoch owning it.

**Admission** is one rule for every delivery of `run_collect(run_id, n)` (`execute_run`):
1. Local check first (`EXECUTIONS`): a live task with token `n` makes the delivery a `duplicate`, which writes
   nothing; a live task with an older token is cancelled and awaited before step 2 (§3.3.3: before any resume
   within one process, the previous asyncio task for the run is cancelled and awaited).
2. DB check under the run row `FOR UPDATE` (`start_attempt`), once `epoch.fence_epoch` proved that `epoch`
   is still the current worker epoch (else `foreign_epoch`): `started` (a `queued` run at attempt `n - 1`
   moves to `running` and gets `run_attempts(n, epoch)`), `resumed` (attempt `n` is `running` and this
   epoch's sweep or reconciler created it), `superseded`, `foreign_epoch` or `finished`. Only `started` and
   `resumed` execute. A delivery whose epoch the fence refused re-defers `run_collect(id, n)` in a transaction
   of its own (M0 carry-forward 6, plan Deviation 18): its job is consumed, and the current worker's
   reconcilers would take minutes to repair the attempt; the queueing lock `run:<id>:<n>` keeps it to one
   waiting job. That refusal proves this worker superseded, so it first asks for its own graceful stop
   (`on_superseded`, ruling T24-a): its loops stop fetching, and it never re-takes its own job under the
   global `collect` lock; the current worker does.
3. The graph runs through `executions.run_owned(("run", id), n, …)`, next to a heartbeat whose loss cancels
   it. That heartbeat is a task of its own (every 30 s), so it covers every node however long it runs (the
   fetch fan-out, `embed`): no node beats itself (Task 24, carry 4).

**New attempts** (`begin_new_attempt`) come from the takeover sweep `runs` (the current attempt belongs to an
older worker epoch) and from the reconciler `stale_runs` (heartbeat older than `RUN_STALE`). Each moves the
locked row `running → running` with `attempt_no + 1`, a new thread and `resume_count + 1`, and defers the new
attempt's job; after `MAX_RESUMES` resumes the run ends `failed('resume_exhausted')` with a critical alert.
Both are fenced by the current worker epoch, like the admission (`epoch.fence_epoch`). `stale_runs` then
cancels and awaits the superseded local execution, so it releases the procrastinate `collect` lock before the
replacement can run.

The reconcilers `queued_runs` (a queued run without a live job: re-deferred once, then
`failed('orphaned_queue')`) and `stale_data` (§17.5, §17.6) complete the set. Every time threshold uses the
caller's clock (`ctx.now`), never the database's `now()`.

Every run that reaches a terminal status is counted in `RUNS` (and, when it started, timed in `RUN_DURATION`)
by the owner of the transaction that ended it, once that transaction has committed (ruling T12-e): the
functions that end a run inside a caller's transaction report a `RunEnd`, and the owner passes it to
`observe_run_ends` after its commit (`execute_run`, the reconcilers, and the takeover sweep through
`epoch.Swept`). The tick counts the `missed` and `skipped_overlap` runs it inserts (`tick.observe_tick`).

**The run end** (Task 23, plan Deviation 16) is owned by the graph: its `finalize` node ends the run in one
owner-fenced transaction and observes the metrics after that commit. `run_owned` discards the graph's value,
so `execute_run` learns of that end from the database (`run_completion`, the terminal row of the attempt);
only a graph that returns without ending the run (the M0 no-op graph) is finalized here, `succeeded` with the
report `{"noop": true}`. A graph that raises after its `finalize` committed (for example writing its last
checkpoint) is not finalized again either. A heartbeat that fires during the `finalize` transaction waits
for its row lock and then finds the run ended: the beat is refused and the attempt is `lost`
(`execution_lost`), which is harmless, because `lost` writes nothing: the run is never finalized twice and
its end is counted once, by the node, or, when the cancel landed before the node's commit returned to it, by
`execute_run` (`observe_graph_end` records which; addendum 11). The same holds for a graph that raised after
its `finalize` committed but before the node observed the end.

This module is a task boundary (ruff `BLE001` is allowed here). It needs one broad catch only, around the
lookup of an end to count (`_observe_unobserved_end`: its outcome is the log line): `run_owned` reports an
unexpected graph error as an outcome, which `execute_run` persists as `failed('bug')` plus the `run.failed`
alert; an `OutcomeConflict` (the outcome reducer's, raised outside any node, §17.2 I7) as
`failed_invariant('I7')` plus `run.failed_invariant`. LangGraph is imported for annotations only, so a process
that only requests runs (the API) skips it.
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, Literal, get_args

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from jobhunter.db.sql import q
from jobhunter.db.transitions import CLEAR_REASON, transition
from jobhunter.errors import FencedOut, TransitionConflict
from jobhunter.faults import afault_point
from jobhunter.graphs.collect.state import initial_state
from jobhunter.graphs.outcomes import OutcomeConflict
from jobhunter.observability.alerts import raise_alert, resolve_alert
from jobhunter.observability.health import read_run_freshness, run_age_state
from jobhunter.observability.logging import get_logger
from jobhunter.observability.metrics import RUN_DURATION, RUNS
from jobhunter.types import JSONValue
from jobhunter.worker.epoch import Swept, fence_epoch, register_takeover_sweep
from jobhunter.worker.executions import EXECUTIONS, AlreadyExecuting, ExecKey, run_owned
from jobhunter.worker.reconcilers import ReconcileContext, reconciler
from jobhunter.worker.tasks import Deferrer, defer_in_tx, defer_run_attempt, job_task_name
from jobhunter.worker.tick import insert_active_collect

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig
    from langgraph.graph.state import CompiledStateGraph
    from langgraph.types import Durability

# These thresholds compare times the database wrote (`heartbeat_at`, `queued_at`: its `now()`) with the
# caller's clock (`ctx.now`, the worker's `utcnow()`). That is safe because the worker and PostgreSQL run on
# one VPS and read its one kernel clock, so the two never drift apart (ruling T21-d). If the database moves to
# another host, their skew shifts every threshold: a worker clock ahead of the database's gives live attempts
# a new attempt early (spending resumes and fencing a healthy execution), one behind recovers dead ones late;
# the cutoffs then have to be computed in SQL from the database's `now()`.
RUN_STALE: Final = timedelta(minutes=3)
QUEUED_REDEFER_AFTER: Final = timedelta(minutes=2)
# A queued run that was re-deferred once and still has no live job this long after it was queued is orphaned.
QUEUED_ORPHANED_AFTER: Final = timedelta(minutes=4)
MAX_RESUMES: Final = 3
# At most this many queued runs without a live job are repaired per tick; the next tick takes the rest.
QUEUED_BATCH: Final = 100
RECURSION_LIMIT: Final = 200
MAX_CONCURRENCY: Final = 4  # the graph's parallel tasks: the fetch fan-out (§9.2)

REDEFERRED: Final = "redeferred"
RESUME_EXHAUSTED_ALERT: Final = "run.resume_exhausted"
ORPHANED_QUEUE_ALERT: Final = "run.orphaned_queue"
FAILED_ALERT: Final = "run.failed"
FAILED_INVARIANT_ALERT: Final = "run.failed_invariant"
STALE_DATA_ALERT: Final = "schedule.stale_data"
STALE_DATA_SUBJECT: Final = "collect"

_TABLE: Final = "runs"
# How often `request_manual_run` tries when the active collection it conflicted with ended before it was read.
_REQUEST_TRIES: Final = 3

Admission = Literal["started", "resumed", "duplicate", "superseded", "foreign_epoch", "finished"]
FinalStatus = Literal["succeeded", "partial", "failed_invariant", "failed"]
FINAL_STATUSES: Final[tuple[FinalStatus, ...]] = get_args(FinalStatus)  # the type is the one list
EndReason = Literal["worker_takeover", "heartbeat_stale"]

# The graph a run executes; any compiled state graph (the worker's is the collection graph).
type RunGraph = CompiledStateGraph[Any, Any, Any, Any]

log = get_logger("jobhunter.worker.runs")


def thread_id(run_id: int, attempt_no: int) -> str:
    """The checkpoint thread of attempt `attempt_no` of run `run_id` (the `run_attempts.thread_id` CHECK)."""
    return f"run-{run_id}-a{attempt_no}"


@dataclass(frozen=True)
class RunOwner:
    """An admitted attempt: its writes are fenced by `attempt_no = :attempt_no AND status = 'running'`."""

    run_id: int
    attempt_no: int


@dataclass(frozen=True)
class RunRow:
    id: int
    kind: str
    trigger: str
    status: str
    scheduled_slot: datetime | None
    include_paid: bool
    attempt_no: int
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    status_reason: str | None


@dataclass(frozen=True)
class RunEnd:
    """A run that a transaction ended: its terminal status and, when it had started, its duration."""

    status: str
    duration_s: float | None


@dataclass(frozen=True)
class RunCompletion:
    """How an attempt ended its run (the durable completion lookup of `execute_run`, `run_completion`)."""

    status: FinalStatus
    reason: str | None
    finished_at: datetime


def observe_run_ends(ends: Iterable[RunEnd]) -> None:
    """Count each ended run in `RUNS` and time it in `RUN_DURATION`; call only after the ending commit."""
    for end in ends:
        RUNS.labels(status=end.status).inc()
        if end.duration_s is not None:
            RUN_DURATION.observe(end.duration_s)


# The attempts (run id, attempt number) whose end their graph's `finalize` observed in this process, kept
# until `execute_run` is done with the attempt (addendum 11, below `execute_run`'s `lost`).
_GRAPH_OBSERVED: set[tuple[int, int]] = set()


def observe_graph_end(owner: RunOwner, end: RunEnd) -> None:
    """`observe_run_ends([end])` for the end `owner`'s graph committed (its `finalize` node), recorded so
    that `execute_run` does not observe it again."""
    _GRAPH_OBSERVED.add((owner.run_id, owner.attempt_no))
    observe_run_ends([end])


# The run row and its current attempt, the run row locked: the DB step of the admission.
_ADMISSION = q(
    "SELECT r.status, r.attempt_no, a.worker_epoch, a.ended_at FROM runs r "
    "LEFT JOIN run_attempts a ON a.run_id = r.id AND a.attempt_no = r.attempt_no "
    "WHERE r.id = :i FOR UPDATE OF r"
)
_LOCK_RUNNING = q("SELECT status, attempt_no, resume_count FROM runs WHERE id = :i FOR UPDATE")
_INSERT_ATTEMPT = q(
    "INSERT INTO run_attempts (run_id, attempt_no, worker_epoch, thread_id) VALUES (:i, :a, :epoch, :thread)"
)
_END_ATTEMPT = q(
    "UPDATE run_attempts SET ended_at = now(), end_reason = :reason "
    "WHERE run_id = :i AND attempt_no = :a AND ended_at IS NULL"
)
_HEARTBEAT = q(
    "UPDATE runs SET heartbeat_at = now() "
    "WHERE id = :i AND attempt_no = :a AND status = 'running' RETURNING id"
)
_ANNOTATE = q(
    "UPDATE runs SET status_reason = :r WHERE id = :i AND status = 'queued' AND attempt_no = :a RETURNING id"
)
_DURATION = q("SELECT extract(epoch FROM finished_at - started_at) FROM runs WHERE id = :i")
# The run's terminal row at the attempt, when the attempt's own `finalize_run` wrote it: that closes the
# attempt `completed`, while a reconciler or the takeover sweep that ends the run (`resume_exhausted`) keeps
# its `attempt_no` and closes the attempt `heartbeat_stale` or `worker_takeover` (F5 review, Important 1).
_COMPLETION = q(
    "SELECT r.status, r.status_reason, r.finished_at FROM runs r JOIN run_attempts a "
    "ON a.run_id = r.id AND a.attempt_no = r.attempt_no WHERE r.id = :i AND r.attempt_no = :a "
    "AND r.status = ANY(:final) AND a.end_reason = 'completed'"
)
_ACTIVE_CONFIG = q("SELECT settings_version_id, scope_version_id FROM active_config WHERE id = 1 FOR SHARE")
_ACTIVE_RUN = q(
    "SELECT id, kind, trigger, status, scheduled_slot, include_paid, attempt_no, queued_at, started_at, "
    "finished_at, status_reason FROM runs WHERE kind = 'collect' AND status IN ('queued', 'running')"
)
_RUN_BY_ID = q(
    "SELECT id, kind, trigger, status, scheduled_slot, include_paid, attempt_no, queued_at, started_at, "
    "finished_at, status_reason FROM runs WHERE id = :i"
)
# Not SKIP LOCKED: the sweep runs once per takeover, so it waits for an old owner's short write instead of
# leaving that run to the old epoch.
_OLDER_EPOCH = q(
    "SELECT r.id FROM runs r JOIN run_attempts a ON a.run_id = r.id AND a.attempt_no = r.attempt_no "
    "WHERE r.status = 'running' AND a.worker_epoch < :epoch ORDER BY r.id FOR UPDATE OF r"
)
# `runs_running_hb` serves the scan.
_STALE = q(
    "SELECT id, attempt_no FROM runs WHERE status = 'running' AND heartbeat_at < :cutoff "
    "ORDER BY id FOR UPDATE SKIP LOCKED"
)
# Queued runs without a live job (the predicate of `tasks.live_job_exists`, inline so that only real
# candidates are locked and the batch is never filled by runs that still have their job).
_QUEUED_WITHOUT_JOB = q(
    "SELECT r.id, r.attempt_no, r.queued_at, r.status_reason FROM runs r "
    "WHERE r.status = 'queued' AND r.queued_at <= :cutoff "
    "AND NOT EXISTS (SELECT 1 FROM procrastinate_jobs j WHERE j.task_name = :task "
    "AND j.status IN ('todo', 'doing') AND j.args @> jsonb_build_object('run_id', r.id)) "
    "ORDER BY r.id LIMIT :limit FOR UPDATE OF r SKIP LOCKED"
)


def _run_row(row: sa.Row[Any]) -> RunRow:
    return RunRow(
        id=int(row.id),
        kind=row.kind,
        trigger=row.trigger,
        status=row.status,
        scheduled_slot=row.scheduled_slot,
        include_paid=bool(row.include_paid),
        attempt_no=int(row.attempt_no),
        queued_at=row.queued_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        status_reason=row.status_reason,
    )


async def start_attempt(
    conn: AsyncConnection, run_id: int, attempt_no: int, *, epoch: int
) -> tuple[Admission, RunOwner | None]:
    """The DB step of the admission of `run_collect(run_id, attempt_no)` by worker `epoch`, row `FOR UPDATE`.

    - `started`: the run is `queued` at attempt `attempt_no - 1`. It moves to `running` at `attempt_no` with a
      first heartbeat and its thread, and `run_attempts(attempt_no, epoch)` is inserted. The move clears the
      queued run's `status_reason` (`redeferred`), which says nothing about a running run.
    - `resumed`: the run is `running` at `attempt_no`, whose open `run_attempts` row belongs to `epoch` (this
      epoch's sweep or reconciler created it). Its heartbeat is refreshed (fenced), so the reconciler does not
      take back an attempt whose job waited in the queue.
    - `finished`: the run has a terminal status (or is gone), whatever attempt the delivery names.
    - `superseded`: the run is at another attempt.
    - `foreign_epoch`: the run is `running` at `attempt_no`, owned by another epoch; or `epoch` is no longer
      the current worker epoch (`fence_epoch`, the first statement, before the run row is locked), so a
      superseded worker admits nothing.

    Only `started` and `resumed` return the owner; the others write nothing.
    """
    admission, owner, _ = await _admit(conn, run_id, attempt_no, epoch=epoch)
    return admission, owner


async def _admit(
    conn: AsyncConnection, run_id: int, attempt_no: int, *, epoch: int
) -> tuple[Admission, RunOwner | None, bool]:
    """`start_attempt`, and whether the epoch fence refused `epoch` (the `foreign_epoch` that re-defers)."""
    try:
        await fence_epoch(conn, epoch)
    except FencedOut:
        return "foreign_epoch", None, True
    row = (await conn.execute(_ADMISSION, {"i": run_id})).first()
    if row is None:
        return "finished", None, False
    owner = RunOwner(run_id=run_id, attempt_no=attempt_no)
    if row.status == "queued":
        if row.attempt_no != attempt_no - 1:
            return "superseded", None, False
        thread = thread_id(run_id, attempt_no)
        await transition(
            conn,
            _TABLE,
            run_id,
            ["queued"],
            "running",
            reason=CLEAR_REASON,
            owner={"attempt_no": row.attempt_no},
            set_={"attempt_no": attempt_no, "heartbeat_at": sa.func.now(), "current_thread_id": thread},
        )
        await conn.execute(_INSERT_ATTEMPT, {"i": run_id, "a": attempt_no, "epoch": epoch, "thread": thread})
        return "started", owner, False
    if row.status != "running":
        return "finished", None, False
    if row.attempt_no != attempt_no:
        return "superseded", None, False
    if row.worker_epoch != epoch or row.ended_at is not None:
        return "foreign_epoch", None, False
    await heartbeat_run(conn, owner)
    return "resumed", owner, False


async def heartbeat_run(conn: AsyncConnection, owner: RunOwner) -> bool:
    """Record a heartbeat of `owner`; `False` when it no longer owns the run (stop the execution)."""
    return (await conn.execute(_HEARTBEAT, {"i": owner.run_id, "a": owner.attempt_no})).first() is not None


async def finalize_run(
    conn: AsyncConnection,
    owner: RunOwner,
    *,
    status: FinalStatus,
    reason: str | None,
    report: Mapping[str, JSONValue],
) -> float | None:
    """End `owner`'s run `status(reason)` with its `report` and close its attempt `completed`.

    Returns the run's duration in seconds (from its first `started_at`), for the caller to observe once the
    transaction has committed (ruling T12-e). Raises `FencedOut`, having written nothing, when `owner` no
    longer owns the run: the `transition()` is the first write.
    """
    try:
        await transition(
            conn,
            _TABLE,
            owner.run_id,
            ["running"],
            status,
            reason=reason,
            owner={"attempt_no": owner.attempt_no},
            set_={"report": report},
        )
    except TransitionConflict as exc:
        raise FencedOut("run") from exc
    await conn.execute(_END_ATTEMPT, {"i": owner.run_id, "a": owner.attempt_no, "reason": "completed"})
    return await _duration(conn, owner.run_id)


async def _duration(conn: AsyncConnection, run_id: int) -> float | None:
    """The run's duration in seconds, from its first `started_at` to its `finished_at` (`None` if unset)."""
    seconds = (await conn.execute(_DURATION, {"i": run_id})).scalar_one()
    return None if seconds is None else float(seconds)


async def run_completion(conn: AsyncConnection, run_id: int, attempt_no: int) -> RunCompletion | None:
    """The end attempt `attempt_no` gave its run: the run's terminal row at that attempt, when that attempt's
    `finalize_run` wrote it (the attempt closed `completed`). `None` while the run is `running`, and when
    another attempt, no finalize, or a reconciler or takeover that ended the run at this attempt number
    (`resume_exhausted`, the attempt closed `heartbeat_stale` or `worker_takeover`) ended it: such an end is
    counted by its own owner, never by the attempt's execution."""
    params = {"i": run_id, "a": attempt_no, "final": list(FINAL_STATUSES)}
    row = (await conn.execute(_COMPLETION, params)).first()
    if row is None:
        return None
    return RunCompletion(status=row.status, reason=row.status_reason, finished_at=row.finished_at)


async def begin_new_attempt(
    conn: AsyncConnection,
    run_id: int,
    *,
    epoch: int,
    end_reason: EndReason,
    defer: Deferrer,
    ends: list[RunEnd] | None = None,
) -> int | None:
    """Take a `running` run from its current attempt and give it the next one, owned by `epoch`.

    Under the run row `FOR UPDATE`: the current attempt `a` ends with `end_reason`; the run moves
    `running → running` (fenced by `attempt_no = a`) with `attempt_no = a + 1`, the new thread, a fresh
    heartbeat and `resume_count + 1`; `run_attempts(a + 1, epoch)` is inserted and `run_collect(id, a + 1)`
    deferred in the caller's transaction. Returns `a + 1`.

    Returns `None` when the run is not `running`, and when this would be resume `MAX_RESUMES + 1`: then the
    run ends `failed('resume_exhausted')` with that `resume_count` and the critical `run.resume_exhausted`
    alert, its current attempt is closed with `end_reason`, and the end is appended to `ends` (when given) for
    the transaction's owner to observe after its commit (`observe_run_ends`).

    Raises `FencedOut`, having written nothing, when `epoch` is not the current worker epoch (`fence_epoch`,
    its first statement). A caller that locks run rows before it calls this fences its transaction first, so
    the `worker_heartbeat` row is always locked before a run row (the takeover sweep holds it already).
    """
    await fence_epoch(conn, epoch)
    row = (await conn.execute(_LOCK_RUNNING, {"i": run_id})).first()
    if row is None or row.status != "running":
        return None
    current = int(row.attempt_no)
    resumes = int(row.resume_count) + 1
    fenced = {"attempt_no": current}
    ended = {"i": run_id, "a": current, "reason": end_reason}
    if resumes > MAX_RESUMES:
        await transition(
            conn,
            _TABLE,
            run_id,
            ["running"],
            "failed",
            reason="resume_exhausted",
            owner=fenced,
            set_={"resume_count": resumes},
        )
        await conn.execute(_END_ATTEMPT, ended)
        await raise_alert(
            conn,
            code=RESUME_EXHAUSTED_ALERT,
            severity="critical",
            subject=f"run:{run_id}",
            message="run resumes exhausted",
        )
        if ends is not None:
            ends.append(RunEnd(status="failed", duration_s=await _duration(conn, run_id)))
        log.warning("run_resume_exhausted", run_id=run_id, attempt_no=current, end_reason=end_reason)
        return None
    following = current + 1
    thread = thread_id(run_id, following)
    await transition(
        conn,
        _TABLE,
        run_id,
        ["running"],
        "running",
        owner=fenced,
        set_={
            "attempt_no": following,
            "resume_count": resumes,
            "current_thread_id": thread,
            "heartbeat_at": sa.func.now(),
        },
    )
    await conn.execute(_END_ATTEMPT, ended)
    await conn.execute(_INSERT_ATTEMPT, {"i": run_id, "a": following, "epoch": epoch, "thread": thread})
    await defer_run_attempt(conn, defer, run_id, following)
    log.info(
        "run_new_attempt", run_id=run_id, attempt_no=following, end_reason=end_reason, resume_count=resumes
    )
    return following


async def annotate_run(conn: AsyncConnection, run_id: int, *, attempt_no: int, reason: str) -> bool:
    """Set the `status_reason` of a `queued` run at `attempt_no`, changing nothing else; `False` on no row.

    The only write of a reason on a queued run (ruling P17): it changes no status, so it is no transition.
    """
    params = {"r": reason, "i": run_id, "a": attempt_no}
    return (await conn.execute(_ANNOTATE, params)).first() is not None


async def request_manual_run(
    conn: AsyncConnection, *, include_paid: bool, defer: Deferrer
) -> tuple[RunRow, bool]:
    """The active collection, or a new `manual` one: `(row, created)`, in the caller's transaction.

    Holds `active_config` `FOR SHARE` (the first lock of the global order) for the settings and scope
    versions. A `queued` or `running` collection is returned as it is (`created=False`). Otherwise the run is
    inserted through `tick.insert_active_collect` and its job deferred in the same transaction. When that
    insert meets a collection another transaction committed after the check (`active_conflict`, logged by the
    helper), that collection is read again and returned.
    """
    config = (await conn.execute(_ACTIVE_CONFIG)).one()
    for _ in range(_REQUEST_TRIES):
        active = (await conn.execute(_ACTIVE_RUN)).first()
        if active is not None:
            return _run_row(active), False
        outcome = await insert_active_collect(
            conn,
            trigger="manual",
            scheduled_slot=None,
            include_paid=include_paid,
            settings_version_id=config.settings_version_id,
            scope_version_id=config.scope_version_id,
            path="manual",
        )
        if isinstance(outcome, int):
            await defer_run_attempt(conn, defer, outcome, 1)
            log.info("manual_run_queued", run_id=outcome, include_paid=include_paid)
            return _run_row((await conn.execute(_RUN_BY_ID, {"i": outcome})).one()), True
        if outcome == "slot_conflict":
            raise RuntimeError("runs: a manual run has no slot, yet its insert met runs_slot_uq")
        # `active_conflict`: the next check reads the collection that won (or inserts when it has ended).
    raise RuntimeError("runs: the active collection kept ending while a manual run was requested")


@register_takeover_sweep("runs")
async def sweep_older_epoch_runs(conn: AsyncConnection, epoch: int) -> Swept:
    """At takeover: give every `running` run whose current attempt belongs to an older epoch a new attempt.

    The runs it ends (`resume_exhausted`) are observed once the takeover transaction has committed. The
    takeover holds the `worker_heartbeat` row with `epoch` written, so each new attempt's fence passes.
    """
    run_ids = [int(run_id) for run_id in (await conn.execute(_OLDER_EPOCH, {"epoch": epoch})).scalars()]
    ends: list[RunEnd] = []
    for run_id in run_ids:
        await begin_new_attempt(
            conn, run_id, epoch=epoch, end_reason="worker_takeover", defer=defer_in_tx, ends=ends
        )
    return Swept(count=len(run_ids), after_commit=lambda: observe_run_ends(ends))


@reconciler("stale_runs", "tick")
async def reconcile_stale_runs(ctx: ReconcileContext) -> int:
    """Give every `running` run whose heartbeat is older than `RUN_STALE` at `ctx.now` a new attempt.

    After that commit, the local execution of a run is cancelled and awaited when, and only when, it runs for
    the attempt that was taken back (its registry token, ruling T12-d), so it releases the procrastinate
    `collect` lock before the replacement attempt's job can run. Rows locked by an in-flight heartbeat or
    owner write are skipped this time. The runs it ends (`resume_exhausted`) are observed after the commit.
    Returns the number of runs taken back: 0 when `ctx.epoch` is no longer the current worker epoch, because a
    superseded worker gives no run a new attempt (the current worker's reconciler does).
    """
    ends: list[RunEnd] = []
    try:
        async with ctx.engine.begin() as conn:
            # Before any run row is locked: the `worker_heartbeat` row comes first in the lock order.
            await fence_epoch(conn, ctx.epoch)
            stale = (await conn.execute(_STALE, {"cutoff": ctx.now - RUN_STALE})).all()
            taken = [(int(row.id), int(row.attempt_no)) for row in stale]
            for run_id, _ in taken:
                await begin_new_attempt(
                    conn, run_id, epoch=ctx.epoch, end_reason="heartbeat_stale", defer=ctx.defer, ends=ends
                )
    except FencedOut:
        return 0  # the fenced transaction wrote nothing (`fence_epoch` logged it)
    observe_run_ends(ends)
    for run_id, superseded in taken:
        key: ExecKey = ("run", run_id)
        live = EXECUTIONS.get(key)
        # No await between the check and the cancel: the entry cannot change in between.
        if live is not None and live[0] == superseded:
            await EXECUTIONS.cancel_and_await(key)
    return len(taken)


@reconciler("queued_runs", "tick")
async def reconcile_queued_runs(ctx: ReconcileContext) -> int:
    """Repair `queued` runs without a live job `QUEUED_REDEFER_AFTER` after they were queued; the count.

    A run without a reason is re-deferred once and annotated `redeferred`. A `redeferred` run that still has
    no live job `QUEUED_ORPHANED_AFTER` after it was queued ends `failed('orphaned_queue')` with the warn
    alert `run.orphaned_queue`; it is counted in `RUNS` after the commit (never timed: it never started). At
    most `QUEUED_BATCH` runs per tick. Fenced by `ctx.epoch` first, before any run row is locked (Ruling
    FR-h): a superseded worker raises `FencedOut` having written nothing, which stops its cadence.
    """
    params = {
        "cutoff": ctx.now - QUEUED_REDEFER_AFTER,
        "task": job_task_name("run_collect"),
        "limit": QUEUED_BATCH,
    }
    repaired = 0
    ends: list[RunEnd] = []
    async with ctx.engine.begin() as conn:
        await fence_epoch(conn, ctx.epoch)
        for row in (await conn.execute(_QUEUED_WITHOUT_JOB, params)).all():
            run_id, attempt_no = int(row.id), int(row.attempt_no)
            if row.status_reason is None:
                if await defer_run_attempt(conn, ctx.defer, run_id, attempt_no + 1) is None:
                    continue  # a waiting job holds the queueing lock: it starts the run
                await annotate_run(conn, run_id, attempt_no=attempt_no, reason=REDEFERRED)
                log.info("queued_run_redeferred", run_id=run_id)
                repaired += 1
            elif row.status_reason == REDEFERRED and ctx.now - row.queued_at >= QUEUED_ORPHANED_AFTER:
                await transition(
                    conn,
                    _TABLE,
                    run_id,
                    ["queued"],
                    "failed",
                    reason="orphaned_queue",
                    owner={"attempt_no": attempt_no},
                )
                await raise_alert(
                    conn,
                    code=ORPHANED_QUEUE_ALERT,
                    severity="warn",
                    subject=f"run:{run_id}",
                    message="queued run orphaned",
                )
                ends.append(RunEnd(status="failed", duration_s=None))
                log.warning("queued_run_orphaned", run_id=run_id)
                repaired += 1
    observe_run_ends(ends)
    return repaired


@reconciler("stale_data", "tick")
async def reconcile_stale_data(ctx: ReconcileContext) -> int:
    """Raise `schedule.stale_data` (critical) exactly while `run_age_state` is `critical`, else resolve it.

    That is the one freshness rule system health reports too (ruling T17-g): after the grace (twice the
    interval after the first settings version, §17.5), no `succeeded` or `partial` run within twice the
    interval, aged from the first settings version when none succeeded yet. Returns 1 when it raised or
    resolved the alert, else 0. Fenced by `ctx.epoch` first (Ruling FR-h): a superseded worker raises
    `FencedOut` having written nothing, which stops its cadence.
    """
    async with ctx.engine.begin() as conn:
        await fence_epoch(conn, ctx.epoch)
        if run_age_state(ctx.now, await read_run_freshness(conn)) == "critical":
            await raise_alert(
                conn,
                code=STALE_DATA_ALERT,
                severity="critical",
                subject=STALE_DATA_SUBJECT,
                message="no successful run within twice the interval",
            )
            return 1
        return int(await resolve_alert(conn, code=STALE_DATA_ALERT, subject=STALE_DATA_SUBJECT))


async def _invoke(graph: RunGraph, owner: RunOwner) -> None:
    """The graph call as a coroutine of its own: a refused start closes it before the graph runs."""
    config: RunnableConfig = {
        "configurable": {"thread_id": thread_id(owner.run_id, owner.attempt_no)},
        "recursion_limit": RECURSION_LIMIT,
        "max_concurrency": MAX_CONCURRENCY,
    }
    # `durability` acts on the graph's own saver; LangGraph warns when it is given to a graph without one.
    saver = graph.checkpointer
    durability: Durability | None = "sync" if saver is not None and not isinstance(saver, bool) else None
    await graph.ainvoke(initial_state(owner.run_id, owner.attempt_no), config, durability=durability)


# The critical alert an end raises in `_finalize`, by status, with its message.
_END_ALERTS: Final[Mapping[str, tuple[str, str]]] = MappingProxyType(
    {
        "failed": (FAILED_ALERT, "run failed"),
        "failed_invariant": (FAILED_INVARIANT_ALERT, "run invariant violated"),
    }
)


async def _finalize(
    engine: AsyncEngine,
    owner: RunOwner,
    *,
    status: FinalStatus,
    reason: str | None,
    report: Mapping[str, JSONValue],
) -> None:
    """Finalize `owner`'s run in a transaction of its own; a `failed` end raises `run.failed` in it, a
    `failed_invariant` one `run.failed_invariant` (both critical, subject `run:<id>`).

    `FencedOut` is caught outside the transaction, which wrote nothing; the run's new owner records the
    outcome. The run metrics are observed only once the transaction has committed (ruling T12-e).
    """
    try:
        async with engine.begin() as conn:
            duration = await finalize_run(conn, owner, status=status, reason=reason, report=report)
            if (alert := _END_ALERTS.get(status)) is not None:
                code, message = alert
                await raise_alert(
                    conn,
                    code=code,
                    severity="critical",
                    subject=f"run:{owner.run_id}",
                    message=message,
                )
    except FencedOut:
        log.info("run_fenced", run_id=owner.run_id, attempt_no=owner.attempt_no, write="finalize")
        return
    observe_run_ends([RunEnd(status=status, duration_s=duration)])
    log.info("run_finished", run_id=owner.run_id, attempt_no=owner.attempt_no, status=status, reason=reason)


# The report of a run the outcome reducer failed (§17.2 I7): its invariant result, no other section.
_OUTCOME_CONFLICT_REPORT: Final[Mapping[str, JSONValue]] = MappingProxyType(
    {"execution": {"invariants": {"I7": {"ok": False, "detail": {"error_class": "OutcomeConflict"}}}}}
)


async def _redefer(engine: AsyncEngine, run_id: int, attempt_no: int) -> None:
    """Defer `run_collect(run_id, attempt_no)` again, in a transaction of its own (carry-forward 6)."""
    async with engine.begin() as conn:
        job_id = await defer_run_attempt(conn, defer_in_tx, run_id, attempt_no)
    log.info("run_redeferred", run_id=run_id, attempt_no=attempt_no, deduplicated=job_id is None)


async def _settle_raised(engine: AsyncEngine, owner: RunOwner, error: Exception | None) -> None:
    """The `raised` outcome of `owner`'s graph (see `execute_run`, step 3)."""
    async with engine.connect() as conn:
        completion = await run_completion(conn, owner.run_id, owner.attempt_no)
    # The class only: an exception message may carry content.
    error_class = type(error).__name__
    if completion is not None:
        # The graph's `finalize` committed the run's end before the error (and observed it, unless the error
        # was its commit's reply: `_observe_unobserved_end` counts it then).
        log.warning(
            "run_graph_failed_after_end",
            run_id=owner.run_id,
            attempt_no=owner.attempt_no,
            status=completion.status,
            error_class=error_class,
        )
        await _observe_unobserved_end(engine, owner)
    elif isinstance(error, OutcomeConflict):
        log.error(
            "run_outcome_conflict", run_id=owner.run_id, attempt_no=owner.attempt_no, work_id=error.work_id
        )
        await _finalize(
            engine, owner, status="failed_invariant", reason="I7", report=_OUTCOME_CONFLICT_REPORT
        )
    else:
        log.error(
            "run_graph_failed", run_id=owner.run_id, attempt_no=owner.attempt_no, error_class=error_class
        )
        await _finalize(engine, owner, status="failed", reason="bug", report={})


async def execute_run(
    engine: AsyncEngine,
    graph: RunGraph,
    run_id: int,
    attempt_no: int,
    *,
    epoch: int,
    heartbeat_period_s: float = 30.0,
    on_superseded: Callable[[], None] = lambda: None,
) -> Admission:
    """Admit and run attempt `attempt_no` of run `run_id` (the `run_collect` task body); its admission.

    1. Local check: a live local execution of the run with token `attempt_no` makes this a `duplicate`
       (nothing is written); one with an older token is cancelled and awaited first.
    2. `start_attempt` in a transaction of its own (fenced by the current worker epoch); anything but
       `started` or `resumed` returns at once. When the fence refused `epoch` (a superseded worker), the
       delivery's job is deferred again first, `run_collect(run_id, attempt_no)` through `defer_run_attempt`
       in a transaction of its own (`None`, logged `deduplicated`, when that attempt's job already waits), so
       the current worker runs it at once (carry-forward 6). Before that re-defer, `on_superseded()` asks
       this worker for its graceful stop (ruling T24-a; the task body requests the runtime's shutdown): its
       loops have stopped fetching by the time the re-deferred job commits, so this worker cannot take it
       again. The other `foreign_epoch` (the attempt belongs to another epoch while this one is current)
       re-defers nothing and stops nothing: this worker would take the job again at once, and the
       `stale_runs` reconciler repairs that run.
    3. The graph runs through `run_owned(("run", id), attempt_no, …)`: the registry's task for the run, next
       to the heartbeat task `heartbeat:run:<id>` (`heartbeat_run` every `heartbeat_period_s`) whose loss
       cancels it. By outcome: `lost` (a refused or failed heartbeat, the `stale_runs` reconciler's or the
       shutdown's cancel, or `FencedOut`) returns with no owner write, having counted an end its graph
       committed but did not observe (`_observe_unobserved_end`, addendum 11); `raised` first looks the
       attempt's end up (`run_completion`): when the graph's `finalize` had already ended the run, the error
       came after it (logged `run_graph_failed_after_end`) and nothing is written (the end is counted here
       when the node had not observed it); otherwise it finalizes, in a new transaction, an `OutcomeConflict`
       as `failed_invariant('I7')` with the critical `run.failed_invariant` alert (the reducer conflicts
       outside any node, so the graph's `finalize` cannot see it) and any other error as `failed('bug')` with
       the critical `run.failed` alert; `returned` looks the attempt's end up (`run_completion`): a graph that
       ended the run itself (its `finalize` node, which observed the metrics and logged `run_finished`) leaves
       nothing to write, and one that did not (the M0 no-op graph) is finalized `succeeded` with the report
       `{"noop": true}`. A local execution that is still live when the work starts (`AlreadyExecuting`, for
       example an older one that ignored its cancel past the timeout) means nothing ran: `duplicate` is
       returned and no further owner write is made; the admitted attempt is left without an executor, and
       `stale_runs` gives the run a new attempt once its heartbeat is stale.

    The caller's own cancellation propagates (the graph is cancelled and awaited first) and nothing is
    written: the run's next owner (the stale-run reconciler or the takeover sweep) records the outcome.
    """
    key: ExecKey = ("run", run_id)
    live = EXECUTIONS.get(key)
    if live is not None:
        token = live[0]
        if token == attempt_no:
            log.info("run_admission", run_id=run_id, attempt_no=attempt_no, admission="duplicate")
            return "duplicate"
        if isinstance(token, int) and token < attempt_no:
            await EXECUTIONS.cancel_and_await(key)
    await afault_point("collect.before_admission")
    async with engine.begin() as conn:
        admission, owner, epoch_refused = await _admit(conn, run_id, attempt_no, epoch=epoch)
    log.info("run_admission", run_id=run_id, attempt_no=attempt_no, admission=admission)
    if epoch_refused:
        on_superseded()  # first: the loops stop fetching while the re-defer below commits
        await _redefer(engine, run_id, attempt_no)
    if owner is None:
        return admission
    admitted = owner

    async def beat() -> bool:
        async with engine.begin() as conn:
            return await heartbeat_run(conn, admitted)

    try:
        outcome = await run_owned(
            key, attempt_no, _invoke(graph, admitted), beat=beat, period_s=heartbeat_period_s
        )
    except AlreadyExecuting:
        log.warning("run_already_executing", run_id=run_id, attempt_no=attempt_no, admission=admission)
        return "duplicate"
    try:
        match outcome.status:
            case "lost":
                await _observe_unobserved_end(engine, admitted)
            case "raised":
                await _settle_raised(engine, admitted, outcome.error)
            case "returned":
                async with engine.connect() as conn:
                    completion = await run_completion(conn, run_id, attempt_no)
                if completion is None:  # a graph without a finalize of its own (M0's no-op graph)
                    await _finalize(engine, admitted, status="succeeded", reason=None, report={"noop": True})
    finally:
        _GRAPH_OBSERVED.discard((run_id, attempt_no))
    return admission


async def _observe_unobserved_end(engine: AsyncEngine, owner: RunOwner) -> None:
    """Addendum 11: count an end that `owner`'s graph committed but did not observe.

    A heartbeat that waited on the run row during the graph's `finalize` transaction finds the run ended once
    it commits, so the beat is refused and the work cancelled (`lost`). When that cancel lands while the
    `finalize` node still awaits its commit's reply, the end is committed but the node never observes it; so
    it is when that await raises after the server committed (`raised`). Here such an end is counted in
    `RUNS` and `RUN_DURATION` and logged `run_finished` (`observed_by` names this path), unless the node
    observed it (`observe_graph_end`). An attempt that ended nothing counts nothing; nor does one whose run a
    reconciler ended at its attempt number (`stale_runs`' `resume_exhausted`, counted by that reconciler):
    `run_completion` finds only an end the attempt's own `finalize_run` wrote. The lookup failing (the
    database gone) is logged, error class only, and the end stays uncounted.
    """
    if (owner.run_id, owner.attempt_no) in _GRAPH_OBSERVED:
        return
    try:
        async with engine.connect() as conn:
            completion = await run_completion(conn, owner.run_id, owner.attempt_no)
            duration = None if completion is None else await _duration(conn, owner.run_id)
    except Exception as exc:
        log.warning("lost_run_end_unread", run_id=owner.run_id, error_class=type(exc).__name__)
        return
    if completion is None:
        return
    observe_run_ends([RunEnd(status=completion.status, duration_s=duration)])
    log.info(
        "run_finished",
        run_id=owner.run_id,
        attempt_no=owner.attempt_no,
        status=completion.status,
        reason=completion.reason,
        observed_by="execute_run",
    )
