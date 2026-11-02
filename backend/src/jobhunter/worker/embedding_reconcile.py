"""The `awaiting_embedding` reconciler (spec §17.4 L5514, §17.6; M1 Carry-forward 14, plan M3 Task 15, Rulings
P15 and P44): moved out of `jobhunter.embeddings`, which the api imports, so only the worker registers it
(`worker.main._REGISTERING_MODULES` lists this module).

It runs only while the embedder is healthy and **no run is `running`** (ruling T22-a: a running run's `embed`
node embeds the backlog itself, oldest first, so the two never compete for one batch). Its first transaction
proves the worker epoch (`fence_epoch`) and reads whether a run is running; a pass that skips logs
`awaiting_embedding_skipped` with its `reason`. It embeds the revisions awaiting for more than
`AWAITING_GRACE` (`embeddings.embed_awaiting`), fenced by the worker epoch, at most `RECONCILE_LIMIT` (32, two
batches) per pass. `reconcile_heavy` runs on the `tick` queue, whose one worker the minute tick shares, so one
pass holds that queue for about 1 min at the measured 0.57 docs/s, and for at most twice
`embeddings.BATCH_TIMEOUT_S` (4 min) when the embedder is slow. A run that starts during a pass can overlap it
by that one pass.

After the embeddings are written it sets the similarity of the active tuple's targets that have none
(`similarity.fill_missing_similarity`, at most `SIMILARITY_LIMIT` per pass: those whose revision is embedded
and whose tuple's profile has vectors, a revision embedded by this pass or earlier), in one transaction in
the lock schedule's order: `fence_epoch` (step 1), `active_config FOR SHARE` (step 2), the targets `FOR UPDATE
SKIP LOCKED` (other domain rows); then, when the embedder answered, `embedder.unavailable` resolved once no
revision awaits at all ("embedder healthy and backlog cleared", §17.6). Ids and counts only reach a log line.
"""

from datetime import timedelta
from typing import Final

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q
from jobhunter.embeddings import (
    ALERT_SUBJECT,
    BATCH,
    UNAVAILABLE_ALERT,
    EmbedReport,
    count_awaiting,
    embed_awaiting,
)
from jobhunter.observability.alerts import resolve_alert
from jobhunter.observability.logging import get_logger
from jobhunter.scoring.similarity import fill_missing_similarity
from jobhunter.worker.epoch import fence_epoch
from jobhunter.worker.reconcilers import ReconcileContext, reconciler

# §17.4: the reconciler embeds revisions awaiting for longer than this.
AWAITING_GRACE: Final = timedelta(minutes=10)
# The most revisions one reconciler pass embeds (ruling T22-a): 2 batches, about 1 min on the V7 CPU.
RECONCILE_LIMIT: Final = 2 * BATCH
# The most targets one pass sets a similarity on (`fill_missing_similarity`'s chunk).
SIMILARITY_LIMIT: Final = 500

_RUN_RUNNING: Final = q("SELECT EXISTS (SELECT 1 FROM runs WHERE status = 'running')")
_LOCK_TUPLE: Final = q("SELECT llm_tuple_id FROM active_config WHERE id = 1 FOR SHARE")

log = get_logger("jobhunter.worker.embedding_reconcile")


async def _after_embedding(ctx: ReconcileContext, report: EmbedReport) -> int:
    """The pass's last transaction (module docstring): the similarities set, then the alert resolved when the
    backlog is cleared; how many targets got a similarity."""
    async with ctx.engine.begin() as conn:
        await fence_epoch(conn, ctx.epoch)  # step 1
        tuple_id = (await conn.execute(_LOCK_TUPLE)).scalar_one_or_none()  # step 2
        filled = 0
        if tuple_id is not None:
            filled = await fill_missing_similarity(
                conn, llm_tuple_id=int(tuple_id), now=ctx.now, limit=SIMILARITY_LIMIT
            )
        if not report.unavailable and await count_awaiting(conn) == 0:
            await resolve_alert(conn, code=UNAVAILABLE_ALERT, subject=ALERT_SUBJECT)
    if filled:
        log.info("awaiting_embedding_similarity_set", llm_tuple_id=tuple_id, targets=filled)
    return filled


@reconciler("awaiting_embedding", "heavy")
async def reconcile_awaiting_embedding(ctx: ReconcileContext) -> int:
    """§17.4 `awaiting_embedding` (see the module docstring); how many revisions it embedded.

    0, having asked the embedder for no vector: without a client (`ctx.embedder`), while a run is `running`
    and while the embedder is not healthy (each logged `awaiting_embedding_skipped`). Raises `FencedOut` when
    `ctx.epoch` is no longer the current worker epoch (`fence_epoch` logged it; the fenced transaction wrote
    nothing), so that `run_reconcilers` stops the cadence and the superseded worker stops, as for every
    other reconciler (Ruling FR-r).
    """
    client = ctx.embedder
    if client is None:
        log.warning("awaiting_embedding_skipped", reason="no_embedder")
        return 0

    async def fenced(conn: AsyncConnection) -> None:
        await fence_epoch(conn, ctx.epoch)

    async with ctx.engine.begin() as conn:
        await fenced(conn)
        running = bool((await conn.execute(_RUN_RUNNING)).scalar_one())
    if running:
        # The pass yields to a running run (Ruling T22-a), whatever its state: a run left `running` by a
        # dead worker keeps every pass skipping (logged `run_running` each time) until `stale_runs` gives
        # it a new attempt or ends it, once its heartbeat is 3 min stale (`failed('resume_exhausted')`
        # after 3 resumes); its revisions wait meanwhile, and the next run's embed node takes them up.
        log.info("awaiting_embedding_skipped", reason="run_running")
        return 0
    health = await client.health()
    if not health.ok:
        # Not the model id: its 40-hex revision reads as a key to the redaction (§16.8).
        log.info("awaiting_embedding_skipped", reason="embedder_unhealthy", checksum_ok=health.checksum_ok)
        return 0
    report = await embed_awaiting(
        ctx.engine, client, guard=fenced, limit=RECONCILE_LIMIT, created_before=ctx.now - AWAITING_GRACE
    )
    await _after_embedding(ctx, report)
    return report.embedded
