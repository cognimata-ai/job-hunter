"""Node 7, `embed` (spec §9.2): every revision awaiting embedding, in batches of 16, as the run's owner.

The node runs `embeddings.embed_awaiting` over every revision `awaiting_embedding` (not only the run's: the
backlog of an earlier outage goes first, oldest first), with the run's owner row as each batch's post-I/O
guard (`ledger.lock_run_owner`: a superseded attempt raises `FencedOut` before it commits a vector), taken
after the batch's job rows (`guard_step="owner"`, step 5 of the lock schedule after step 4), and the fault
point `collect.embed.between_batches` after each batch (crash case C7). A revision whose job is under a purge
fence gets no vector (§20.2, M5 Task 8): it stays awaiting, and counts in `revisions_awaiting`. Its update's
counters: `revisions_embedded`, `revisions_truncated` (flagged by the service at 1,024 tokens) and
`revisions_awaiting`, the revisions still awaiting when it ended, which make the run `partial` (§7.16.4, I6;
Task 23 decides the status).

**It never fails the run for time.** The node's §9.2 timeout is `NODE_TIMEOUT` (20 min); it starts no batch
after `BUDGET` (that timeout less a batch's `BATCH_TIMEOUT_S` and `TX_MARGIN` for its transaction), measured
on the deps' clock, nor once the worker's admission stopped (its shutdown). The rest stays
`awaiting_embedding`, the run is `partial`, and the `awaiting_embedding` reconciler catches up later. At about
0.6 docs/s on the V7 CPU the budget covers about 600 revisions.

**Failures.** The embedder `unavailable` stops the loop: the rest stays awaiting, counted, and the node
raises `embedder.unavailable` (warn, subject `embedder`, §17.6) in a transaction of its own. Any other
`EmbedderError` (`bad_response`, `checksum`) wrote nothing of its batch: the node raises the same alert with
that kind and lets the error reach the graph's error handler, which persists the node's outcome
(`graph.error_handled`: the counter `embedder_errors`). Neither kind fails the run; `FencedOut` ends the
attempt quietly. Deps without an embedder client (tests of other nodes) embed nothing and count what awaits.
"""

from datetime import timedelta
from typing import Final

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter import embeddings
from jobhunter.embeddings import BATCH_TIMEOUT_S, EmbedderError, EmbedderErrorKind
from jobhunter.graphs.collect.deps import CollectDeps
from jobhunter.graphs.collect.nodes import Node
from jobhunter.graphs.collect.state import CollectState, CollectUpdate
from jobhunter.observability.logging import get_logger
from jobhunter.quota.ledger import lock_run_owner
from jobhunter.worker.runs import RunOwner

EMBED: Final = "embed"
BETWEEN_BATCHES: Final = "collect.embed.between_batches"
NODE_TIMEOUT: Final = timedelta(minutes=20)  # §9.2 node 7; the graph's `TIMEOUTS["embed"]`
TX_MARGIN: Final = timedelta(seconds=60)  # room for a batch's transaction after its call
BUDGET: Final = NODE_TIMEOUT - timedelta(seconds=BATCH_TIMEOUT_S) - TX_MARGIN
REVISIONS_EMBEDDED: Final = "revisions_embedded"
REVISIONS_AWAITING: Final = "revisions_awaiting"
REVISIONS_TRUNCATED: Final = "revisions_truncated"

log = get_logger("jobhunter.graphs.collect.embed")


def _update(embedded: int, awaiting: int, truncated: int) -> CollectUpdate:
    counters = {REVISIONS_EMBEDDED: embedded, REVISIONS_AWAITING: awaiting, REVISIONS_TRUNCATED: truncated}
    return {"counters": counters, "steps": [EMBED]}


def make_embed(deps: CollectDeps) -> Node:
    async def raise_unavailable(kind: EmbedderErrorKind, run_id: int) -> None:
        async with deps.engine.begin() as conn:
            await embeddings.raise_unavailable(conn, kind=kind, run_id=run_id)

    async def embed(state: CollectState) -> CollectUpdate:
        owner = RunOwner(state["run_id"], state["attempt_no"])
        client = deps.embedder
        if client is None:
            async with deps.engine.connect() as conn:
                awaiting = await embeddings.count_awaiting(conn)
            log.warning("embed_skipped", run_id=owner.run_id, reason="no_embedder", awaiting=awaiting)
            return _update(0, awaiting, 0)
        deadline = deps.clock() + BUDGET

        def stop() -> bool:
            return deps.admission_stopped() or deps.clock() >= deadline

        async def guard(conn: AsyncConnection) -> None:
            await lock_run_owner(conn, owner)

        try:
            report = await embeddings.embed_awaiting(
                deps.engine, client, guard=guard, guard_step="owner", fault=BETWEEN_BATCHES, stop=stop
            )
        except EmbedderError as err:
            await raise_unavailable(err.kind, owner.run_id)
            raise  # `error_handled` persists the node's outcome
        if report.unavailable:
            await raise_unavailable("unavailable", owner.run_id)
        log.info(
            "embed_done",
            run_id=owner.run_id,
            attempt_no=owner.attempt_no,
            embedded=report.embedded,
            awaiting=report.awaiting,
            truncated=report.truncated,
            unavailable=report.unavailable,
        )
        return _update(report.embedded, report.awaiting, report.truncated)

    return embed
