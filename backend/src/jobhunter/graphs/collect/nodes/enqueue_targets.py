"""Node 9, `enqueue_targets` (spec §9.2 node 9, §9.7; plan M3 Task 14, R9, Deviation 9, A2 R2): the score
targets of the run's current revisions, for the run's tuple.

A run carries the tuple that was active when it was created (`runs.llm_tuple_id`, `state["llm_tuple_id"]`);
without one (no profile active) the node records its step and changes nothing. With one, it works through the
run's jobs (`RUN_JOB_SQL`: changed since the run started, `jobs.updated_at`, or whose current revision the
run created; the node-8 pattern), unmerged and not synthetic, in id order, `targets.CHUNK` jobs per
transaction:
1. `active_config FOR SHARE` (step 2). When the run's tuple is no longer the active one (a confirm or a save
   activated another during the run), the node creates nothing more and counts the run's revisions left
   without a target of it (`targets_skipped_tuple_superseded`): the activation's `create_targets_for_tuple`
   owns the new tuple, and I5 passes with `"targets": "tuple_superseded"` (R9);
2. the chunk's job rows `FOR SHARE` in id order (step 4), then the run's owner row (`ledger.lock_run_owner`,
   step 5: a superseded attempt raises `FencedOut` before it writes anything);
3. `targets.cancel_targets(llm_tuple_id=<the run's>, revision_ids=<the chunk's jobs' revisions that stopped
   being current>, reason="revision_superseded")`: their unpublished targets (A2 R2), an `in_progress` one
   with its attempt, an `in_batch` one individually (L3979). It runs before the creations, so its bulk rows
   (`bulk.lock_for_targets`, step 7) precede the transaction's first target-row lock (Ruling P52, carry
   T13-a: a creation's upsert locks a conflicting target row);
4. `targets.create_or_revive` of each current revision without a live target of the tuple (`similarity` from
   the profile's vectors, `NULL` while it awaits its embedding; the initial state by the creation rules; a
   fenced job skipped), into no bulk operation, in job id order;
5. `ranking.bump` when anything moved (step 11, last).

**A deadlock pair** (step 3): the owner of an `in_progress` follow-up target takes its target row (step 5) and
then its bulk row (step 7) in its publish or fail, while this cancellation, never the owner, takes the bulk
row first (P52) and then waits for the target row. Both hold `active_config FOR SHARE`, so the two can meet.
PostgreSQL detects the cycle and aborts one: this node retries it (`DB_RETRY`), the owner's attempt retries
its `db_transient` (`escaped_db_transient`), and the outcome is that of the two in either serial order (the
target published or failed and then left alone as terminal, or cancelled first and the owner's write fenced).
An activation, which holds `active_config FOR UPDATE`, never meets an owner.

The counts go to `state["counters"]`: `targets_created`, `targets_revived`, `targets_cancelled`,
`targets_skipped_<reason>`, and `targets_state_<state>` for the state each created or revived target was left
in (Ruling T14-a); the run report's `revisions.targets` shows them (`report.targets_section`). A second pass
(a database retry, a resumed attempt) finds the created targets and changes nothing. Its timeout is 5 min and
it retries a retryable database error (`graph.TIMEOUTS`, `DB_RETRY`).
"""

from collections import Counter
from datetime import datetime
from typing import Final

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db import ranking
from jobhunter.db.sql import q
from jobhunter.graphs.collect.deps import CollectDeps
from jobhunter.graphs.collect.nodes import Node, run_started_at
from jobhunter.graphs.collect.state import CollectState, CollectUpdate
from jobhunter.observability.logging import get_logger
from jobhunter.quota.ledger import lock_run_owner
from jobhunter.scoring import cycles, targets
from jobhunter.worker.runs import RunOwner

ENQUEUE_TARGETS: Final = "enqueue_targets"
# M3 sets it, with the target creation below and I5's check of the run's targets (plan Deviation 10).
SCORE_TARGETS_ENABLED: Final = True
TARGETS_CREATED: Final = "targets_created"
TARGETS_REVIVED: Final = "targets_revived"
TARGETS_CANCELLED: Final = "targets_cancelled"
SKIPPED_PREFIX: Final = "targets_skipped_"
STATE_PREFIX: Final = "targets_state_"
# The creation's skip reason of a run whose tuple a confirm or a save superseded during the run.
SKIPPED_TUPLE_SUPERSEDED: Final = "skipped_tuple_superseded"
# A job of the run (`jobs j`): changed since the run started (`:started`), or its current revision created by
# it (a reused earlier revision changes the job, a new one is created).
RUN_JOB_SQL: Final = (
    "(j.updated_at >= :started OR EXISTS (SELECT 1 FROM job_revisions r WHERE r.id = j.current_revision_id "
    "AND r.created_at >= :started))"
)
_PAGE: Final = q(
    f"SELECT j.id, j.current_revision_id, {targets.NEEDS_TARGET_SQL} AS needs FROM jobs j "  # noqa: S608
    f"WHERE {targets.RETAINED_JOB_SQL} AND {RUN_JOB_SQL} AND j.id > :after ORDER BY j.id LIMIT :limit"
)
# The revisions of the chunk's jobs that stopped being current, with an unpublished live target of the tuple.
_SUPERSEDED: Final = q(
    "SELECT DISTINCT t.job_revision_id FROM score_targets t JOIN jobs j ON j.id = t.job_id "
    "WHERE t.job_id = ANY(CAST(:jobs AS bigint[])) AND t.llm_tuple_id = :tuple "
    "AND t.job_revision_id <> j.current_revision_id "
    "AND t.state NOT IN ('published_accepted', 'published_contested', 'cancelled') ORDER BY 1"
)
_LEFT: Final = q(
    "SELECT count(*) FROM jobs j "  # noqa: S608 (the constants above)
    f"WHERE {targets.RETAINED_JOB_SQL} AND {RUN_JOB_SQL} AND {targets.NEEDS_TARGET_SQL}"
)

log = get_logger("jobhunter.graphs.collect.enqueue_targets")


def _counters(made: Counter[str], cancelled: int) -> dict[str, int]:
    """The node's counters, the zero ones left out."""
    counters = {
        TARGETS_CREATED: made["created"],
        TARGETS_REVIVED: made["revived"],
        TARGETS_CANCELLED: cancelled,
        **{
            f"{SKIPPED_PREFIX}{name.removeprefix('skipped_')}": n
            for name, n in made.items()
            if name.startswith("skipped_")
        },
        **{
            f"{STATE_PREFIX}{name.removeprefix('state_')}": n
            for name, n in made.items()
            if name.startswith("state_")
        },
    }
    return {name: n for name, n in sorted(counters.items()) if n}


async def _chunk(
    conn: AsyncConnection,
    owner: RunOwner,
    *,
    llm_tuple_id: int,
    scope_version_id: int,
    started: datetime,
    after: int,
    now: datetime,
) -> tuple[int | None, Counter[str], int]:
    """One chunk after job `after` (module docstring): the last job id it covered (`None` when none was
    left), what the creations did, and how many targets it cancelled."""
    page = (
        await conn.execute(
            _PAGE, {"tuple": llm_tuple_id, "started": started, "after": after, "limit": targets.CHUNK}
        )
    ).all()
    if not page:
        return None, Counter(), 0
    job_ids = [int(row.id) for row in page]
    await targets.lock_jobs(conn, job_ids)  # step 4
    await lock_run_owner(conn, owner)  # step 5
    superseded = [
        int(r) for r in (await conn.execute(_SUPERSEDED, {"jobs": job_ids, "tuple": llm_tuple_id})).scalars()
    ]
    cancelled = 0
    if superseded:  # first: its bulk rows before any target row (module docstring, step 3)
        cancelled = (
            await targets.cancel_targets(
                conn,
                llm_tuple_id=llm_tuple_id,
                revision_ids=superseded,
                reason=targets.REVISION_SUPERSEDED,
                now=now,
            )
        ).targets
    made = await targets.create_chunk(
        conn,
        [(int(row.id), int(row.current_revision_id)) for row in page if row.needs],
        llm_tuple_id=llm_tuple_id,
        scope_version_id=scope_version_id,
        bulk_operation_id=None,
        now=now,
    )
    if targets.moved(made) or cancelled:
        await ranking.bump(conn)  # step 11, last
    last = job_ids[-1] if len(page) == targets.CHUNK else None
    return last, made, cancelled


def make_enqueue_targets(deps: CollectDeps) -> Node:
    """Node 9 over `deps` (module docstring)."""

    async def enqueue_targets(state: CollectState) -> CollectUpdate:
        tuple_id = state["llm_tuple_id"]
        if tuple_id is None or not SCORE_TARGETS_ENABLED:
            return {"steps": [ENQUEUE_TARGETS]}
        owner = RunOwner(state["run_id"], state["attempt_no"])
        async with deps.engine.connect() as conn:
            started = await run_started_at(conn, owner.run_id)
        made: Counter[str] = Counter()
        cancelled = 0
        after: int | None = 0
        while after is not None:
            async with deps.engine.begin() as conn:
                active = await cycles.lock_active_scoring(conn)  # step 2
                if active.llm_tuple_id != tuple_id:
                    left = await conn.execute(_LEFT, {"tuple": tuple_id, "started": started})
                    made[SKIPPED_TUPLE_SUPERSEDED] += int(left.scalar_one())
                    break
                after, chunk_made, chunk_cancelled = await _chunk(
                    conn,
                    owner,
                    llm_tuple_id=tuple_id,
                    scope_version_id=active.scope_version_id,
                    started=started,
                    after=after,
                    now=deps.clock(),
                )
            made.update(chunk_made)
            cancelled += chunk_cancelled
        counters = _counters(made, cancelled)
        log.info(
            "enqueue_targets_done",
            run_id=owner.run_id,
            attempt_no=owner.attempt_no,
            llm_tuple_id=tuple_id,
            **counters,
        )
        update: CollectUpdate = {"steps": [ENQUEUE_TARGETS]}
        if counters:
            update["counters"] = counters
        return update

    return enqueue_targets
