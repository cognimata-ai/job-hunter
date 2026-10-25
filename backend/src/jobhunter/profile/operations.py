"""The profile operations (spec §7.16.8, §9.5, §12; M2 plan Task 11): their handlers and failure hooks.

**`extract_profile_text`** (payload `{"draft_id": <id>}`, created with the draft by the upload,
`upload.request_draft_operation`). In one transaction, in the §9.1 order: the owner row
(`lock_operation_owner`), then the draft `FOR UPDATE`. A draft in `uploaded` moves to `extracting_text`; one
already in `extracting_text` (an earlier execution's start, before a reclaim) goes on as it is; any other
state, or a draft that is gone, ends the operation `succeeded` as obsolete without touching it (`finish_in`).
Then the handler invokes `profile_graphs.text` on a thread of its own (`graph.thread_id("t", draft_id,
claim.operation_id, claim.attempts)`) with the §9.1 invoke shape (`graph.invoke_config`; `configurable["op"]`
is this context). The
graph's nodes end the operation in the transaction of their last domain write: `text_review`, or
`failed(code)` (`graphs.profile.nodes.parse`, `.strip_contacts`).

**Its failure hook** (`text_phase_failed`, Deviation 13) runs in every transaction that ends such an
operation `failed`. It reads the draft `FOR UPDATE` and moves it only while it is still live in the text
phase: `extracting_text → failed(<code or 'attempts_exhausted'>)`; a draft still `uploaded` (the operation
failed before its first transition: `no_handler`, or ten `bug` requeues) takes the two §7.16.8 edges
`uploaded → extracting_text → failed(code)` with the same `failure_code`, so it never stays `uploaded` and
answers 409 to every later upload. A draft the handler's own write already ended in that transaction (a
node's `failed(code)` with its `finish_in`), or one in a later phase, is left as it is, so `transition()`
never meets a terminal row. The draft row comes after the owner row in the §9.1 order. A payload without a
usable `draft_id` is logged (`profile_hook_payload_invalid`, the operation id and an error class) and moves
nothing: the hook runs inside a reclaim, the takeover sweep or a tick, which it must never fail.

**`extract_profile`** (Task 13; payload `{"draft_id": <id>}`, dedup key `extract_profile:<id>`). Only the
transaction that moves the draft into `extracting_profile` creates it: the approval or the retry (Task 14) or
the native opt-in upload (Task 9). In one transaction, the owner row, then the draft `FOR UPDATE`: a draft in
`extracting_profile` goes on; one in any other state (`text_review` and `failed` included) or gone ends the
operation `succeeded` as obsolete without touching it, so no extraction ever starts on text the user has not
approved (§12.3). Then the handler invokes `profile_graphs.extract` on the execution's own thread
(`thread_id("p", draft_id, claim.operation_id, claim.attempts)`) with the same invoke shape. Its nodes end
the operation in the transaction of their last domain write: `ready`, or `failed(code)`, or a requeue
(`graphs.profile.nodes.check_pdf`, `.extract_profile`, `.draft`).

**Its failure hook** (`extract_phase_failed`) moves a draft still in `extracting_profile` to
`failed(<code or 'attempts_exhausted'>)`: a requeue's last attempt (`llm_transient`), or a reclaim or the
takeover sweep that spent the attempts (`attempts_exhausted`). A draft the handler's own write already ended,
or one in another state, is left as it is. It needs no generation predicate: while its operation is live the
draft stays in that operation's generation (a new approval needs `text_review`, a retry `failed`, and each
creates a new operation), and the hook runs in the transaction that ends that operation. A malformed payload
is logged and moves nothing, as for the text phase.

**`embed_profile`** (Task 19; payload `{"profile_version_id": <id>}`, dedup key `embed_profile:<id>`, created
with the version by the confirm and by the `profile_vectors` reconciler, `profile.vectors.request_embedding`).
It stores the vectors of a profile version's summary and target titles (`profile.vectors` says which texts and
how they are written). No domain row is driven by this operation, so it has no failure hook: its permanent
failure (ten requeues) is the `operation.failed` alert, and the reconciler creates a new operation once the
embedder is healthy again. The steps:
1. Without a lock, the version's state. A version that is gone or purged, or under an active purge fence,
   ends the operation `succeeded` with the progress `{"skipped": "purged" | "fenced"}`; so does a version
   whose vectors are already complete (a second run is a no-op, and needs no embedder).
2. Otherwise the texts go to the embedder in one batch as queries, outside any transaction. A worker
   without an embedder (`OperationDeps.embedder is None`) and any `EmbedderError` give the operation back
   (`requeue_in`, code `embedder_unavailable`, `not_before` ten minutes on the injected clock) and write
   nothing.
3. Then, in one transaction: the owner row (`lock_operation_owner`), the version row `FOR SHARE` and its state
   again, and, only if the version is still usable, the embeddings, the `profile_vectors` rows, the deletion
   of the kinds it no longer has and the operation's end (`finish_in`). A version purged or fenced between the
   embedder call and this transaction (the fault point `embed_profile.after_embed` sits there) ends
   `succeeded` with the skip and writes nothing, so a purged profile never gets a vector back. A claim lost
   meanwhile is `FencedOut` at the owner lock and nothing is written.
Logs carry the operation id, the version id, counts and short codes, never a text.

**`propose_queries`** (Task 20; payload `{"proposal_id": <id>, "scope_version_id": <id>}`, the scope active at
the request, dedup key `propose_queries:<id>`, created with its `running` proposal by
`queries.proposals.request_proposal`, which registers nothing). In one
transaction, the owner row, then the proposal `FOR UPDATE`: a proposal still `running` goes on; one in any
other state, or gone, ends the operation `succeeded` as obsolete without touching it. Then the handler
invokes `profile_graphs.propose` on the execution's own thread (`thread_id("q", proposal_id,
claim.operation_id, claim.attempts)`, `queries-<proposal_id>-q<operation_id>-a<attempts>`) with the same
invoke shape. Its one node ends the proposal and the operation in one transaction: `ready`, `failed(code)`,
or a requeue (`graphs.profile.nodes.propose_queries`).

**Its failure hook** (`proposal_failed`) moves a proposal still `running` to `failed`: a requeue's last
attempt, or a reclaim or the takeover sweep that spent the attempts (the code is the operation's;
`query_proposals` has no reason column). A proposal the node's own write already ended is left as it is. A
payload without a usable `proposal_id` is logged (`query_proposal_hook_payload_invalid`) and moves nothing, as
for the drafts.
"""

from collections.abc import Mapping
from typing import Final

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.transitions import transition
from jobhunter.embeddings import EmbedderError
from jobhunter.faults import afault_point
from jobhunter.graphs.profile.graph import invoke_config
from jobhunter.graphs.profile.nodes import EXTRACTING_PROFILE, end_obsolete, fail_draft, lock_draft
from jobhunter.graphs.profile.state import initial_proposal_state, initial_state
from jobhunter.observability.logging import get_logger
from jobhunter.profile import vectors
from jobhunter.queries import proposals
from jobhunter.types import JSONValue, get_int
from jobhunter.worker.operations import (
    ATTEMPTS_EXHAUSTED,
    OperationContext,
    lock_operation_owner,
    register_operation_handler,
    set_progress,
)

EXTRACT_PROFILE_TEXT: Final = "extract_profile_text"
EXTRACT_PROFILE: Final = "extract_profile"
_DRAFTS: Final = "profile_drafts"
_BIGINT_END: Final = 2**63

log = get_logger("jobhunter.profile.operations")


def _draft_id(payload: Mapping[str, JSONValue]) -> int:
    return get_int(dict(payload), "draft_id")


def _hook_payload_id(
    operation_id: int, payload: Mapping[str, JSONValue], key: str, *, event: str
) -> int | None:
    """The payload's `key` id for a failure hook, or `None` (logged as `event`) when the payload holds none
    that a `bigint` column can hold: the hook runs inside a reclaim, a takeover or a tick, which must not fail
    with it."""
    try:
        subject_id = get_int(dict(payload), key)
    except (KeyError, TypeError) as exc:
        log.error(event, operation_id=operation_id, error_class=type(exc).__name__)
        return None
    if not 0 < subject_id < _BIGINT_END:
        log.error(event, operation_id=operation_id, error_class="OutOfRange")
        return None
    return subject_id


def _hook_draft_id(operation_id: int, payload: Mapping[str, JSONValue]) -> int | None:
    """The payload's draft id for a draft's failure hook (`_hook_payload_id`)."""
    return _hook_payload_id(operation_id, payload, "draft_id", event="profile_hook_payload_invalid")


async def text_phase_failed(
    conn: AsyncConnection, operation_id: int, payload: Mapping[str, JSONValue], error_code: str | None
) -> None:
    """The text phase's failure hook (module docstring)."""
    draft_id = _hook_draft_id(operation_id, payload)
    if draft_id is None:
        return  # no draft to name: the operation still ends `failed`
    status = await lock_draft(conn, draft_id)
    if status not in ("uploaded", "extracting_text"):
        return  # ended by the handler's own write, or in another phase: not this hook's to move
    code = error_code or ATTEMPTS_EXHAUSTED
    if status == "uploaded":
        await transition(conn, _DRAFTS, draft_id, ["uploaded"], "extracting_text")
    await fail_draft(conn, draft_id, code, from_state="extracting_text")
    log.warning("profile_draft_failed", operation_id=operation_id, draft_id=draft_id, code=code)


@register_operation_handler("extract_profile_text", on_failed=text_phase_failed)
async def extract_profile_text(ctx: OperationContext) -> None:
    """The text phase of a draft (module docstring)."""
    if ctx.deps is None:
        raise RuntimeError("extract_profile_text runs with the worker's OperationDeps")
    claim = ctx.claim
    draft_id = _draft_id(claim.payload)
    async with ctx.engine.begin() as conn:
        await lock_operation_owner(conn, claim)
        status = await lock_draft(conn, draft_id)
        if status == "uploaded":
            await transition(conn, _DRAFTS, draft_id, ["uploaded"], "extracting_text")
        elif status != "extracting_text":
            await end_obsolete(conn, ctx, draft_id, status)
            return
    await ctx.deps.profile_graphs.text.ainvoke(
        initial_state(draft_id, claim.operation_id), invoke_config("t", draft_id, ctx), durability="sync"
    )


async def extract_phase_failed(
    conn: AsyncConnection, operation_id: int, payload: Mapping[str, JSONValue], error_code: str | None
) -> None:
    """The extract phase's failure hook (module docstring)."""
    draft_id = _hook_draft_id(operation_id, payload)
    if draft_id is None:
        return  # no draft to name: the operation still ends `failed`
    if await lock_draft(conn, draft_id) != EXTRACTING_PROFILE:
        return  # ended by the handler's own write, or in another phase: not this hook's to move
    code = error_code or ATTEMPTS_EXHAUSTED
    await fail_draft(conn, draft_id, code, from_state=EXTRACTING_PROFILE)
    log.warning("profile_draft_failed", operation_id=operation_id, draft_id=draft_id, code=code)


@register_operation_handler("extract_profile", on_failed=extract_phase_failed)
async def extract_profile(ctx: OperationContext) -> None:
    """The extract phase of a draft (module docstring)."""
    if ctx.deps is None:
        raise RuntimeError("extract_profile runs with the worker's OperationDeps")
    claim = ctx.claim
    draft_id = _draft_id(claim.payload)
    async with ctx.engine.begin() as conn:
        await lock_operation_owner(conn, claim)
        status = await lock_draft(conn, draft_id)
        if status != EXTRACTING_PROFILE:
            await end_obsolete(conn, ctx, draft_id, status)
            return
    await ctx.deps.profile_graphs.extract.ainvoke(
        initial_state(draft_id, claim.operation_id), invoke_config("p", draft_id, ctx), durability="sync"
    )


async def proposal_failed(
    conn: AsyncConnection, operation_id: int, payload: Mapping[str, JSONValue], error_code: str | None
) -> None:
    """The query proposal's failure hook (module docstring)."""
    proposal_id = _hook_payload_id(
        operation_id, payload, "proposal_id", event="query_proposal_hook_payload_invalid"
    )
    if proposal_id is None:
        return  # no proposal to name: the operation still ends `failed`
    proposal = await proposals.lock_proposal(conn, proposal_id)
    if proposal is None or proposal.status != proposals.RUNNING:
        return  # ended by the node's own write: not this hook's to move
    await proposals.fail(conn, proposal_id)
    code = error_code or ATTEMPTS_EXHAUSTED
    log.warning("query_proposal_hook_failed", operation_id=operation_id, proposal_id=proposal_id, code=code)


@register_operation_handler("propose_queries", on_failed=proposal_failed)
async def propose_queries(ctx: OperationContext) -> None:
    """A query proposal (module docstring)."""
    if ctx.deps is None:
        raise RuntimeError("propose_queries runs with the worker's OperationDeps")
    claim = ctx.claim
    proposal_id = get_int(dict(claim.payload), "proposal_id")
    async with ctx.engine.begin() as conn:
        await lock_operation_owner(conn, claim)
        proposal = await proposals.lock_proposal(conn, proposal_id)
        if proposal is None or proposal.status != proposals.RUNNING:
            await proposals.end_obsolete(
                conn, ctx, proposal_id, None if proposal is None else proposal.status
            )
            return
    await ctx.deps.profile_graphs.propose.ainvoke(
        initial_proposal_state(proposal_id, claim.operation_id),
        invoke_config("q", proposal_id, ctx),
        durability="sync",
    )


async def _end_embed(ctx: OperationContext, conn: AsyncConnection, skipped: vectors.Skipped | None) -> None:
    """End the `embed_profile` operation `succeeded` in `conn`, after its owner lock; a skip records why."""
    if skipped is not None:
        await set_progress(conn, ctx.claim, {"skipped": skipped})
    await ctx.finish_in(conn, status="succeeded")


def _log_embed_end(
    ctx: OperationContext, version_id: int, skipped: vectors.Skipped | None, *, count: int, embedded: bool
) -> None:
    """The log line of an `embed_profile` that ended `succeeded`, after its commit: ids and counts only."""
    if skipped is not None:
        log.info(
            "embed_profile_skipped",
            operation_id=ctx.claim.operation_id,
            version_id=version_id,
            reason=skipped,
        )
    else:
        log.info(
            "embed_profile_done",
            operation_id=ctx.claim.operation_id,
            version_id=version_id,
            vectors=count,
            embedded=embedded,
        )


async def _requeue_embed(
    ctx: OperationContext, version_id: int, *, reason: str, error: EmbedderError | None = None
) -> None:
    """Give the `embed_profile` operation back for `vectors.REQUEUE_AFTER` (module docstring, step 2)."""
    if ctx.deps is None:
        raise RuntimeError("embed_profile runs with the worker's OperationDeps")
    async with ctx.engine.begin() as conn:
        await ctx.requeue_in(
            conn, error_code=vectors.REQUEUE_CODE, not_before=ctx.deps.clock() + vectors.REQUEUE_AFTER
        )
    log.warning(
        "embed_profile_requeued",
        operation_id=ctx.claim.operation_id,
        version_id=version_id,
        reason=reason,
        error_kind=None if error is None else error.kind,
        error_code=None if error is None else error.code,
    )


@register_operation_handler("embed_profile")
async def embed_profile(ctx: OperationContext) -> None:
    """The vectors of a profile version (module docstring)."""
    if ctx.deps is None:
        raise RuntimeError("embed_profile runs with the worker's OperationDeps")
    claim = ctx.claim
    version_id = get_int(dict(claim.payload), "profile_version_id")
    async with ctx.engine.connect() as conn:  # plain reads, rolled back when the block ends
        read = await vectors.read_version(conn, version_id)
        texts = [] if read.profile is None else vectors.vector_texts(read.profile)
        complete = read.profile is not None and await vectors.vectors_complete(conn, version_id, texts)
    if read.profile is None or complete:  # step 1: nothing to embed
        async with ctx.engine.begin() as conn:
            await lock_operation_owner(conn, claim)
            await _end_embed(ctx, conn, read.skipped)
        _log_embed_end(ctx, version_id, read.skipped, count=len(texts), embedded=False)
        return
    if ctx.deps.embedder is None:  # step 2
        await _requeue_embed(ctx, version_id, reason="no_embedder")
        return
    try:
        embedded = await ctx.deps.embedder.embed([text for _, text in texts], input_kind=vectors.INPUT_KIND)
    except EmbedderError as err:
        await _requeue_embed(ctx, version_id, reason="embedder_error", error=err)
        return
    await afault_point(vectors.AFTER_EMBED_POINT)
    async with ctx.engine.begin() as conn:  # step 3
        await lock_operation_owner(conn, claim)
        locked = await vectors.read_version(conn, version_id, lock=True)  # again, under the version's lock
        written = 0
        if locked.skipped is None:
            written = await vectors.write_vectors(conn, version_id, texts, embedded)
        await _end_embed(ctx, conn, locked.skipped)
    _log_embed_end(ctx, version_id, locked.skipped, count=written, embedded=True)
