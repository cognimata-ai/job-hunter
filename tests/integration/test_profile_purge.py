"""The profile version purge (spec §20.1, §20.2's profile purge, §3.3.3, §18.2 N12; plan M5 Task 14, D11,
Deviations 18, 25, 53 and 54): `privacy.kinds.profile_purge`, the retention rule `profile_purges`, and the
writers that hold `active_config` so that a version's eligibility and its call set cannot change after A0.

`retention_daily`'s rule opens one purge per eligible version (`profile_purge.open_due`, called here without a
deferrer); each `purge` operation is run as the worker runs it (`tests.support.purges`' `Purger`). Most tests
run on the two-year-old seed (`tests.support.privacy_seed`), whose oldest user version is purgeable at `now`
with every row a purge meets; the tests that confirm new versions do it through `profile.activation`, and the
race tests go through the api routes with their middleware. Every row is invented.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

import httpx
import pytest
import sqlalchemy as sa
from jobhunter.api.routes import profile as profile_routes
from jobhunter.clock import utcnow
from jobhunter.db.seed import content_hash
from jobhunter.db.transitions import transition
from jobhunter.errors import FencedOut, InvariantViolation, Refusal
from jobhunter.llm import spend_ledger
from jobhunter.llm.schemas.profile import ConfirmedProfile
from jobhunter.llm.spend_ledger import OperationOwner, Reservation
from jobhunter.privacy import deletion_graph, purge
from jobhunter.privacy.kinds import profile_purge
from jobhunter.privacy.manifests import ManifestRow, check_postconditions
from jobhunter.profile import activation
from jobhunter.queries import proposals
from jobhunter.scoring import eval_runner, golden
from jobhunter.worker import operations as ops
from jobhunter.worker.tasks import defer_in_tx
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.support.eval import (
    case_rows,
    eval_deps,
    execute,
    fake_eval_answers,
    run_row,
    seed_adversarial,
    start_eval,
)
from tests.support.faults import PausePoint
from tests.support.fences import insert_test_fence
from tests.support.llm import USAGE, FakeLLM, claim_owner, completed_call
from tests.support.m2 import insert_eval_run, insert_llm_call, insert_profile_chain
from tests.support.m3 import Job, insert_golden_case, insert_job
from tests.support.pg import LockWait, PgServer, lock_wait_details
from tests.support.privacy_seed import SeedIds, seed_two_year_old_graph
from tests.support.procs import await_until
from tests.support.profile import SYNTHETIC_PROFILE, idem
from tests.support.purges import Purger, completed
from tests.support.scoring import activate_test_policy

# `queue_app`: a confirm defers its operations' jobs with `defer_in_tx`, which needs the task app
pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("current_epoch", "queue_app")]

WAIT_S: Final = 30.0
_OPENED: Final = sa.text(
    "SELECT m.id AS manifest_id, m.request, o.id AS operation_id FROM deletion_manifests m "
    "JOIN operations o ON o.kind = 'purge' AND o.payload -> 'manifest_id' = to_jsonb(m.id) "
    "WHERE m.kind = 'profile_purge' ORDER BY m.id"
)
_FENCES: Final = sa.text(
    "SELECT kind, profile_version_id, job_ids, released_at FROM purge_fences WHERE manifest_id = :m"
)
_VERSION: Final = sa.text(
    "SELECT profile_json IS NULL AS redacted, purged_at, version_no, profile_hash FROM profile_versions "
    "WHERE id = :v"
)
_LIVE_PROPOSAL_OPERATIONS: Final = sa.text(
    "SELECT o.id FROM operations o JOIN query_proposals p ON o.dedup_key = 'propose_queries:' || p.id "
    "WHERE o.kind = 'propose_queries' AND o.status IN ('pending', 'running') AND p.profile_version_id = :v "
    "ORDER BY o.id"
)
_OPERATION_STATUS: Final = sa.text("SELECT status FROM operations WHERE id = :o")
_PROPOSALS: Final = sa.text(
    "SELECT id, status, items, llm_call_id FROM query_proposals WHERE profile_version_id = :v ORDER BY id"
)
_CALL: Final = sa.text(
    "SELECT c.status, c.output_json, c.content_purged_at, c.cost_usd, c.output_tokens, l.state AS entry, "
    "l.settled_usd FROM llm_calls c JOIN llm_spend_ledger l ON l.id = c.spend_ledger_id WHERE c.id = :c"
)
_RESERVATION: Final = sa.text(
    "SELECT l.id AS ledger_id, c.id AS call_id, l.budget_month, l.reserved_usd, l.purpose_class, "
    "l.model_requested, l.batch FROM llm_calls c JOIN llm_spend_ledger l ON l.id = c.spend_ledger_id "
    "WHERE c.id = :c"
)
_CALL_COUNT: Final = sa.text("SELECT count(*) FROM llm_calls")
_LEDGER_COUNT: Final = sa.text("SELECT count(*) FROM llm_spend_ledger")
_TARGETS_OF_TUPLE: Final = sa.text("SELECT count(*) FROM score_targets WHERE llm_tuple_id = :t")
_VECTORS: Final = sa.text("SELECT count(*) FROM profile_vectors WHERE profile_version_id = :v")
_TUPLES: Final = sa.text("SELECT id FROM llm_tuples WHERE profile_version_id = :v ORDER BY id")
_CV_ROW: Final = sa.text(
    "SELECT storage_path, raw_text_enc IS NULL AS raw_gone, stripped_text IS NULL AS stripped_gone, "
    "purged_at FROM cv_files WHERE id = :c"
)
_DRAFTS: Final = sa.text(
    "SELECT draft_json IS NULL AS json_gone, reviewed_text IS NULL AS text_gone, content_purged_at, status "
    "FROM profile_drafts WHERE id = :d"
)
_CASE_ROWS: Final = (
    "SELECT (SELECT count(*) FROM golden_cases WHERE id = :g) + (SELECT count(*) FROM eval_case_results "
    "WHERE golden_case_id = :g) + (SELECT count(*) FROM llm_calls WHERE golden_case_id = :g)"
)
_EXECUTION_CONFIG: Final = sa.text("SELECT execution_config_id FROM active_config WHERE id = 1")
_ACTIVE_VERSION: Final = sa.text("SELECT profile_version_id FROM active_config WHERE id = 1")
_LOCK_ACTIVE: Final = sa.text("SELECT 1 FROM active_config WHERE id = 1 FOR UPDATE")
_PID: Final = sa.text("SELECT pg_backend_pid()")
_CV: Final = sa.text(
    "INSERT INTO cv_files (sha256, mime, size_bytes) VALUES (:h, 'text/plain', 1) RETURNING id"
)
_READY_DRAFT: Final = sa.text(
    "INSERT INTO profile_drafts (cv_file_id, status, reviewed_text_sha256, draft_json) "
    "VALUES (:cv, 'ready', :h, CAST(:profile AS jsonb)) RETURNING id"
)


# ---- helpers -----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Opened:
    manifest_id: int
    version_id: int
    operation_id: int


async def opened(engine: AsyncEngine) -> list[Opened]:
    """Every profile purge opened so far, oldest first."""
    async with engine.connect() as conn:
        rows = (await conn.execute(_OPENED)).all()
    return [
        Opened(int(r.manifest_id), int(r.request["profile_version_id"]), int(r.operation_id)) for r in rows
    ]


async def of_version(engine: AsyncEngine, version_id: int) -> Opened:
    (found,) = [o for o in await opened(engine) if o.version_id == version_id]
    return found


async def open_due(engine: AsyncEngine, now: datetime | None = None) -> int:
    return await profile_purge.open_due(engine, now=utcnow() if now is None else now, epoch=1, defer=None)


async def seeded(engine: AsyncEngine, now: datetime | None = None) -> SeedIds:
    async with engine.begin() as conn:
        return await seed_two_year_old_graph(conn, now=utcnow() if now is None else now)


async def one(engine: AsyncEngine, statement: sa.TextClause | str, **params: object) -> Any:
    clause = sa.text(statement) if isinstance(statement, str) else statement
    async with engine.connect() as conn:
        return (await conn.execute(clause, params)).one()


async def scalar(engine: AsyncEngine, statement: sa.TextClause | str, **params: object) -> Any:
    clause = sa.text(statement) if isinstance(statement, str) else statement
    async with engine.connect() as conn:
        return (await conn.execute(clause, params)).scalar_one_or_none()


async def ids(engine: AsyncEngine, statement: sa.TextClause, **params: object) -> list[int]:
    async with engine.connect() as conn:
        return [int(i) for i in (await conn.execute(statement, params)).scalars()]


async def run_purge(engine: AsyncEngine, purger: Purger, version_id: int) -> ManifestRow:
    """The purge opened for `version_id`, run to its completion; its manifest."""
    found = await of_version(engine, version_id)
    await purger.run(found.operation_id)
    return await completed(purger, found.manifest_id, found.operation_id)


def profile_of(seniority: str) -> str:
    """The synthetic profile at `seniority` (its own content hash), as a draft's JSON."""
    return ConfirmedProfile.model_validate({**SYNTHETIC_PROFILE, "seniority": seniority}).model_dump_json()


async def ready_draft(engine: AsyncEngine, seniority: str) -> int:
    profile = profile_of(seniority)
    digest = content_hash(profile)
    async with engine.begin() as conn:
        cv = (await conn.execute(_CV, {"h": digest})).scalar_one()
        return int(
            (await conn.execute(_READY_DRAFT, {"cv": cv, "h": digest, "profile": profile})).scalar_one()
        )


async def confirm(engine: AsyncEngine, draft_id: int) -> int:
    """`confirm_draft` as the route runs it (`active_config FOR UPDATE` first): the version it activated; a
    `Refusal` goes through."""
    async with engine.begin() as conn:
        await conn.execute(_LOCK_ACTIVE)
        status, body = await activation.confirm_draft(
            conn, draft_id, expected_rescore_ack=True, now=utcnow(), defer=defer_in_tx
        )
    assert status == 201, body
    version = body["profile_version_id"]
    assert isinstance(version, int)
    return version


async def confirm_profile(engine: AsyncEngine, seniority: str) -> int:
    """A ready draft of the synthetic profile at `seniority`, confirmed and activated: its version."""
    return await confirm(engine, await ready_draft(engine, seniority))


async def reservation_of(engine: AsyncEngine, call_id: int) -> Reservation:
    row = await one(engine, _RESERVATION, c=call_id)
    return Reservation(
        ledger_id=int(row.ledger_id),
        llm_call_id=int(row.call_id),
        month=row.budget_month,
        reserved_usd=row.reserved_usd,
        purpose_class=row.purpose_class,
        model=row.model_requested,
        batch=bool(row.batch),
    )


async def call_kw(
    engine: AsyncEngine, owner: OperationOwner, *, purpose: str, refs: dict[str, Any], **columns: Any
) -> dict[str, Any]:
    """The keyword arguments of `spend_ledger.reserve` for one online try of `owner`, non-scoring."""
    return {
        "owner": owner,
        "purpose": purpose,
        "purpose_class": "nonscoring",
        "model": "claude-opus-5-5",
        "effort": "medium",
        "max_tokens": 2000,
        "max_tokens_stage": "first",
        "transport_try": 1,
        "logical_call_no": 1,
        "deadline_s": 300,
        "input_tokens_bound": 1000,
        "native_pdf_extra": False,
        "execution_config_id": int(await scalar(engine, _EXECUTION_CONFIG)),
        "prompt_template_version": "test-v1",
        "rendered_prompt_sha256": b"\x02" * 32,
        "prefix_sha256": None,
        "input_refs": refs,
        "profile_draft_id": None,
        "eval_run_id": None,
        "transport": "stream",
        "now": utcnow(),
        **columns,
    }


async def reserve(engine: AsyncEngine, kw: dict[str, Any]) -> Reservation:
    async with engine.begin() as conn:
        return await spend_ledger.reserve(conn, **kw)


async def dispatch(engine: AsyncEngine, kw: dict[str, Any]) -> Reservation:
    """A reservation and its dispatch marker, committed: a try the gateway has sent."""
    async with engine.begin() as conn:
        res = await spend_ledger.reserve(conn, **kw)
        await spend_ledger.mark_dispatched(conn, res, kw["owner"], now=kw["now"])
    return res


async def proposal_try(
    engine: AsyncEngine, owner: OperationOwner, proposal: int, version: int
) -> Reservation:
    refs = {"proposal_id": proposal, "profile_version_id": version}
    return await dispatch(engine, await call_kw(engine, owner, purpose="propose_queries", refs=refs))


ANSWER: Final = {"queries": [{"text": "Erfundene späte Suche", "lang": "de"}]}


async def settle_billed(engine: AsyncEngine, res: Reservation) -> None:
    """The provider's response to the try, settled as the gateway settles it: billed, with its output."""
    async with engine.begin() as conn:
        await spend_ledger.settle(
            conn,
            res,
            outcome="billed",
            evidence=None,
            usage=USAGE,
            call=completed_call(output_json=ANSWER),
            now=utcnow(),
        )


async def assert_billed_without_output(engine: AsyncEngine, call_id: int) -> None:
    """The try settled `billed` with its usage and cost, its call `completed`, its output never stored."""
    call = await one(engine, _CALL, c=call_id)
    assert (call.status, call.entry, call.output_json) == ("completed", "billed", None)
    assert call.content_purged_at is not None and call.output_tokens == USAGE.output_tokens
    assert call.settled_usd > 0 and call.cost_usd == call.settled_usd


def redacted_calls(manifest: ManifestRow) -> list[int]:
    assert manifest.scope is not None
    (op,) = [
        op for op in manifest.scope["operations"] if op["op"] == "field_redact" and op["table"] == "llm_calls"
    ]
    return list(op["ids"])


async def pid_of(conn: AsyncConnection) -> int:
    return int((await conn.execute(_PID)).scalar_one())


async def waits(server: PgServer, *tasks: "asyncio.Task[Any]", count: int = 1) -> list[LockWait]:
    """The lock waits once `count` statements wait (no task may end meanwhile: its error is raised here)."""

    async def ready() -> bool:
        return len(lock_wait_details(server)) >= count or any(t.done() for t in tasks)

    await await_until(ready, WAIT_S, 0.02)
    for task in tasks:
        if task.done():
            task.result()
            raise AssertionError("a task that should wait ended")
    return lock_wait_details(server)


def one_wait(found: list[LockWait], relation: str) -> LockWait:
    matching = [w for w in found if w.relation == relation]
    assert len(matching) == 1, found
    return matching[0]


def background(work: Awaitable[Any]) -> "asyncio.Task[Any]":
    async def run() -> Any:
        return await work

    return asyncio.create_task(run())


def on_arrival(
    point: str, arrival: int, action: Callable[[], Awaitable[None]]
) -> Callable[[str], Awaitable[None]]:
    """An `afault_point` that runs `action` at the `arrival`-th arrival at `point`."""
    seen = 0

    async def fault(name: str) -> None:
        nonlocal seen
        if name == point:
            seen += 1
            if seen == arrival:
                await action()

    return fault


def two_activations(engine: AsyncEngine) -> Callable[[], Awaitable[tuple[int, int]]]:
    async def activate() -> tuple[int, int]:
        return await confirm_profile(engine, "lead"), await confirm_profile(engine, "head")

    return activate


# ---- the purge on the seed ---------------------------------------------------------------------------------


async def test_an_eligible_old_version_is_purged_with_its_row_and_tuples_kept(
    engine: AsyncEngine, purger: Purger
) -> None:
    """On the two-year-old seed: one purge opens, for the oldest version alone, with a `profile` fence over
    it. Its manifest holds the affected version and tuple, the watermark and §20.2's steps 2-6 as typed
    operations, the export ops last; the purge deletes the tuple's targets, the vectors and the inactive case,
    redacts the proposals, every call of the version, the profile, the CV file and texts and the draft's
    content, and keeps the version's row, its tuple and every ledger row. Phase B is idempotent: run again,
    it changes nothing."""
    now = utcnow()
    seed = await seeded(engine, now)
    v, t = seed.purgeable_version_id, seed.purgeable_tuple_id
    storage_path = (await one(engine, _CV_ROW, c=seed.purgeable_cv_file_id)).storage_path
    async with engine.connect() as conn:
        ledger_before = int((await conn.execute(_LEDGER_COUNT)).scalar_one())
        tuples_before = [int(i) for i in (await conn.execute(_TUPLES, {"v": v})).scalars()]
    assert tuples_before == [t]

    assert await open_due(engine, now) == 1
    [o] = await opened(engine)
    assert o.version_id == v
    async with engine.connect() as conn:
        [fence] = (await conn.execute(_FENCES, {"m": o.manifest_id})).all()
    assert (fence.kind, fence.profile_version_id, fence.job_ids, fence.released_at) == (
        "profile",
        v,
        [],
        None,
    )
    manifest = await run_purge(engine, purger, v)

    scope = manifest.scope
    assert scope is not None
    assert scope["affected"] == {"job_ids": [], "profile_version_ids": [v], "tuple_ids": [t]}
    assert scope["watermark"] == {"fenced_at": scope["fenced_at"], "max_ids": scope["max_ids"]}
    proposal_ids = sorted([seed.ready_proposal_id, seed.failed_proposal_id, seed.running_proposal_id])
    assert [op for op in scope["operations"] if not deletion_graph.is_export_op(op)] == [
        {"op": "fence", "kind": "profile", "key": str(v), "job_ids": []},
        {
            "op": "row_delete",
            "table": "score_targets",
            "ids": [t],
            "bound": {"by": "parent", "table": "llm_tuples", "column": "llm_tuple_id"},
        },
        {
            "op": "row_delete",
            "table": "profile_vectors",
            "ids": [v],
            "bound": {"by": "parent", "table": "profile_versions", "column": "profile_version_id"},
        },
        {
            "op": "field_redact",
            "table": "query_proposals",
            "ids": proposal_ids,
            "columns": ["items"],
            "marker": {"status": "purged"},
        },
        {
            "op": "field_redact",
            "table": "llm_calls",
            "ids": list(seed.purgeable_call_ids),
            "columns": ["output_json"],
            "marker": {"content_purged_at": "set"},
        },
        {"op": "row_delete", "table": "golden_cases", "ids": [seed.purgeable_case_id], "bound": {"by": "id"}},
        {
            "op": "field_redact",
            "table": "profile_versions",
            "ids": [v],
            "columns": ["profile_json"],
            "marker": {"purged_at": "set"},
        },
        {
            "op": "field_redact",
            "table": "cv_files",
            "ids": [seed.purgeable_cv_file_id],
            "columns": ["raw_text_enc", "stripped_text"],
            "marker": {"purged_at": "set"},
        },
        {"op": "file_remove", "volume": "cv_store", "keys": [storage_path]},
        {
            "op": "field_redact",
            "table": "profile_drafts",
            "ids": [seed.purgeable_draft_id],
            "columns": ["draft_json", "reviewed_text"],
            "marker": {"content_purged_at": "set"},
        },
    ]
    assert [op["op"] for op in scope["operations"] if deletion_graph.is_export_op(op)] == [
        "field_redact",
        "file_remove",
    ]

    version = await one(engine, _VERSION, v=v)
    assert version.redacted and version.purged_at is not None and version.version_no == 1
    async with engine.connect() as conn:
        assert [int(i) for i in (await conn.execute(_TUPLES, {"v": v})).scalars()] == [t]  # the tuple stays
        assert (await conn.execute(_TARGETS_OF_TUPLE, {"t": t})).scalar_one() == 0
        assert (await conn.execute(_VECTORS, {"v": v})).scalar_one() == 0
        assert (await conn.execute(_LEDGER_COUNT)).scalar_one() == ledger_before  # never touched
        proposals_after = (await conn.execute(_PROPOSALS, {"v": v})).all()
    assert [(p.status, p.items) for p in proposals_after] == [("purged", None)] * 3
    for call_id in seed.purgeable_call_ids:
        call = await one(engine, _CALL, c=call_id)
        assert call.output_json is None and call.content_purged_at is not None
    assert await scalar(engine, _CASE_ROWS, g=seed.purgeable_case_id) == 0
    cv = await one(engine, _CV_ROW, c=seed.purgeable_cv_file_id)
    assert (cv.storage_path, cv.raw_gone, cv.stripped_gone) == (None, True, True) and cv.purged_at is not None
    draft = await one(engine, _DRAFTS, d=seed.purgeable_draft_id)
    assert (draft.json_gone, draft.text_gone, draft.status) == (True, True, "confirmed")
    assert draft.content_purged_at is not None
    for other in (seed.previous_version_id, seed.active_version_id):  # the retained versions are untouched
        assert not (await one(engine, _VERSION, v=other)).redacted

    # phase B again from its start (a retry after a lost lock, or a replay): it finds nothing left to do
    proc = purge.KINDS["profile_purge"]
    again = purge.PurgeRun(
        engine=engine,
        cfg=purger.cfg,
        now=utcnow(),
        cv_store=purger.deps.cv_store,
        manifest=manifest,
        mode="replay",
        effects=None,
        owner=None,
    )
    await proc.execute(again, scope)
    async with engine.connect() as conn:
        assert await check_postconditions(conn, scope, files=purger.file_roots) == []
    assert (await one(engine, _VERSION, v=v)).purged_at == version.purged_at  # the first redaction's time

    assert await open_due(engine, now) == 0  # purged: never eligible again


async def test_the_active_and_previous_versions_are_never_eligible(engine: AsyncEngine) -> None:
    """D11: the active version and the previous one (the newest confirmed before it, in activation order) are
    never selected, however eligible otherwise. The seed's previous version (old targets, an inactive case, no
    feedback) is opened only once a new activation leaves it behind; a fresh version (no target, no case, no
    feedback) is skipped while it is active and while it is the previous one, and opened after two more."""
    now = utcnow()
    seed = await seeded(engine, now)
    assert await open_due(engine, now) == 1
    assert [o.version_id for o in await opened(engine)] == [seed.purgeable_version_id]

    fresh = await confirm_profile(engine, "principal")  # active; the seed's active one is now the previous
    assert await open_due(engine) == 1
    assert [o.version_id for o in await opened(engine)][1:] == [seed.previous_version_id]
    assert await confirm_profile(engine, "lead") != fresh  # `fresh` is the previous one now
    assert await open_due(engine) == 0
    await confirm_profile(engine, "head")
    assert await open_due(engine) == 1
    assert [o.version_id for o in await opened(engine)][2:] == [fresh]
    # the seed's formerly active version: feedback names it and its case is active, so never
    assert seed.active_version_id not in {o.version_id for o in await opened(engine)}


@pytest.mark.parametrize("blocker", ["feedback", "active_case"])
async def test_feedback_or_an_active_golden_case_blocks_the_purge(engine: AsyncEngine, blocker: str) -> None:
    """A `score_feedback` row naming the version, or an active golden case on it, keeps it; removing the
    feedback or deactivating the case makes it eligible again (each only makes a version more eligible)."""
    now = utcnow()
    seed = await seeded(engine, now)
    v = seed.purgeable_version_id
    async with engine.begin() as conn:
        if blocker == "feedback":
            feedback = int(
                (
                    await conn.execute(
                        sa.text(
                            "INSERT INTO score_feedback (job_id, profile_version_id, score_snapshot, kind, "
                            "label, created_at) VALUES (:j, :v, '{}', 'label', 'good', :t) RETURNING id"
                        ),
                        {"j": seed.closed_job_id, "v": v, "t": now},
                    )
                ).scalar_one()
            )
        else:
            await golden.set_flags(conn, seed.purgeable_case_id, active=True, is_canary=None, now=now)
    assert await open_due(engine, now) == 0
    async with engine.begin() as conn:
        if blocker == "feedback":
            await golden.delete_feedback(conn, feedback)
        else:
            await golden.set_flags(conn, seed.purgeable_case_id, active=False, is_canary=None, now=now)
    assert await open_due(engine, now) == 1
    assert [o.version_id for o in await opened(engine)] == [v]


@pytest.mark.parametrize("history", ["ended_recently", "not_terminal"])
async def test_unexpired_score_history_blocks_the_purge(engine: AsyncEngine, history: str) -> None:
    """A target of the version's tuple that is not terminal, or that ended within
    `retention.score_history_months` (12 by default, calendar months), keeps the version; one that ended
    before that cutoff does not."""
    now = utcnow()
    seed = await seeded(engine, now)
    target = int(
        await scalar(
            engine,
            "SELECT id FROM score_targets WHERE llm_tuple_id = :t AND state = 'cancelled'",
            t=seed.purgeable_tuple_id,
        )
    )
    cutoff = profile_purge.months_before(now, 12)
    async with engine.begin() as conn:
        if history == "ended_recently":
            await conn.execute(
                sa.text("UPDATE score_targets SET state_changed_at = :t WHERE id = :i"),
                {"t": cutoff + timedelta(days=1), "i": target},
            )
        else:  # revived: `cancelled → pending` (§7.16.3)
            await transition(conn, "score_targets", target, ["cancelled"], "pending", reason=None)
    assert await open_due(engine, now) == 0
    async with engine.begin() as conn:
        if history == "not_terminal":
            await transition(
                conn, "score_targets", target, ["pending"], "cancelled", reason="tuple_superseded"
            )
        await conn.execute(
            sa.text("UPDATE score_targets SET state_changed_at = :t WHERE id = :i"),
            {"t": cutoff - timedelta(days=1), "i": target},
        )
    assert await open_due(engine, now) == 1


async def test_proposals_become_purged_with_items_and_call_output_nulled(
    engine: AsyncEngine, purger: Purger
) -> None:
    """A `ready`, a `failed` and a `running` proposal all end `purged` with `items` NULL; the running one's
    `pending` operation is cancelled at admission, before anything else. Every call of the version is
    redacted, published or not: the ready proposal's call, its earlier attempt's call that no proposal names,
    the failed proposal's call and the extraction call; their status, usage and cost stay."""
    now = utcnow()
    seed = await seeded(engine, now)
    v = seed.purgeable_version_id
    async with engine.connect() as conn:
        before = {p.id: p for p in (await conn.execute(_PROPOSALS, {"v": v})).all()}
    assert {p.status for p in before.values()} == {"ready", "failed", "running"}
    assert before[seed.ready_proposal_id].items is not None
    calls_before = {c: await one(engine, _CALL, c=c) for c in seed.purgeable_call_ids}
    assert sum(c.output_json is not None for c in calls_before.values()) == 4  # all but the in-flight one

    assert await open_due(engine, now) == 1
    assert await scalar(engine, _OPERATION_STATUS, o=seed.running_operation_id) == "cancelled"
    assert await ids(engine, _LIVE_PROPOSAL_OPERATIONS, v=v) == []
    running = "SELECT status FROM query_proposals WHERE id = :p"
    assert await scalar(engine, running, p=seed.running_proposal_id) == "running"  # phase B purges it
    manifest = await run_purge(engine, purger, v)
    assert redacted_calls(manifest) == list(seed.purgeable_call_ids)

    async with engine.connect() as conn:
        after = {p.id: p for p in (await conn.execute(_PROPOSALS, {"v": v})).all()}
    assert {p: (row.status, row.items) for p, row in after.items()} == dict.fromkeys(before, ("purged", None))
    assert after[seed.ready_proposal_id].llm_call_id == before[seed.ready_proposal_id].llm_call_id  # ids stay
    for call_id, was in calls_before.items():
        now_ = await one(engine, _CALL, c=call_id)
        assert now_.output_json is None and now_.content_purged_at is not None
        assert (now_.status, now_.entry, now_.cost_usd) == (was.status, was.entry, was.cost_usd)


# ---- the call set is closed at admission (Deviation 54) ---------------------------------------------------


async def test_a_late_proposal_response_after_completion_stores_no_output(
    engine: AsyncEngine, purger: Purger
) -> None:
    """The running proposal's call is still `dispatched` when the purge completes; its response, released
    afterwards, settles `billed` with its usage and cost, and its output is not stored."""
    now = utcnow()
    seed = await seeded(engine, now)
    assert await open_due(engine, now) == 1
    await run_purge(engine, purger, seed.purgeable_version_id)
    call = await one(engine, _CALL, c=seed.in_flight_call_id)
    assert (call.status, call.entry, call.output_json) == ("dispatched", "dispatched", None)
    assert call.content_purged_at is not None
    await settle_billed(engine, await reservation_of(engine, seed.in_flight_call_id))
    await assert_billed_without_output(engine, seed.in_flight_call_id)


async def test_a_settlement_racing_the_redaction_stores_no_output(
    engine: AsyncEngine, purger: Purger, migrated: PgServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The settlement of the in-flight call reads it before the redaction commits and writes it after: its
    update waits for the redaction's row lock, and its `CASE` is evaluated again on the redacted row, so the
    output is NULL while the usage and cost are settled."""
    seed = await seeded(engine)
    assert await open_due(engine) == 1
    found = await of_version(engine, seed.purgeable_version_id)
    paused = PausePoint(profile_purge.AFTER_REDACTION)  # the redaction's chunk, before its commit
    monkeypatch.setattr(profile_purge, "afault_point", paused)
    purging = background(purger.run(found.operation_id))
    await asyncio.wait_for(paused.reached.wait(), WAIT_S)
    settling = background(settle_billed(engine, await reservation_of(engine, seed.in_flight_call_id)))
    wait = one_wait(await waits(migrated, settling), "llm_calls")
    assert wait.key == str(seed.in_flight_call_id)  # the settlement's write, after its read, waits
    paused.resume()
    await asyncio.wait_for(asyncio.gather(purging, settling), WAIT_S)
    await completed(purger, found.manifest_id, found.operation_id)
    await assert_billed_without_output(engine, seed.in_flight_call_id)


async def test_a_late_extraction_response_stores_no_output(engine: AsyncEngine, purger: Purger) -> None:
    """An extraction try of the version's draft, dispatched before the purge (its operation is no proposal's,
    so admission leaves it): the purge redacts its call, and its response settles without its output."""
    seed = await seeded(engine)
    async with engine.begin() as conn:
        operation, _ = await ops.create_operation(
            conn,
            kind="extract_profile",
            payload={"draft_id": seed.purgeable_draft_id},
            dedup_key="x",
            defer=None,
        )
    owner = await claim_owner(engine, operation)
    draft = seed.purgeable_draft_id
    res = await dispatch(
        engine,
        await call_kw(
            engine, owner, purpose="extract_profile", refs={"profile_draft_id": draft}, profile_draft_id=draft
        ),
    )
    assert await open_due(engine) == 1
    manifest = await run_purge(engine, purger, seed.purgeable_version_id)
    assert res.llm_call_id in redacted_calls(manifest)
    await settle_billed(engine, res)
    await assert_billed_without_output(engine, res.llm_call_id)


async def test_no_call_of_the_version_is_reserved_after_admission(
    engine: AsyncEngine, purger: Purger
) -> None:
    """A proposal execution (the running proposal's operation, claimed) dispatched one try before admission:
    that call is in the computed set, and its response, released after the completion, stores nothing. Its
    next reservation, after A0 cancelled its operation, is `FencedOut` and writes no call."""
    seed = await seeded(engine)
    v, proposal = seed.purgeable_version_id, seed.running_proposal_id
    owner = await claim_owner(engine, seed.running_operation_id)
    sent = await proposal_try(engine, owner, proposal, v)
    assert await open_due(engine) == 1
    calls = await scalar(engine, _CALL_COUNT)
    refs = {"proposal_id": proposal, "profile_version_id": v}
    with pytest.raises(FencedOut):
        await reserve(
            engine, await call_kw(engine, owner, purpose="propose_queries", refs=refs, logical_call_no=2)
        )
    assert await scalar(engine, _CALL_COUNT) == calls  # nothing written
    manifest = await run_purge(engine, purger, v)
    assert sent.llm_call_id in redacted_calls(manifest)
    await settle_billed(engine, sent)
    await assert_billed_without_output(engine, sent.llm_call_id)


@pytest.mark.parametrize("step", ["reservation", "dispatch_marker"])
async def test_a_reservation_racing_admission_is_in_the_computed_set(
    engine: AsyncEngine, purger: Purger, migrated: PgServer, step: str
) -> None:
    """A0 waits for a try's open transaction: a reservation holds `active_config` and the operation `FOR
    SHARE`, a dispatch marker the operation alone, which A0's cancel waits for. Once the try commits, A0
    cancels the operation, and A2 names the try's call: no call of the version escapes the computed set."""
    seed = await seeded(engine)
    v, proposal = seed.purgeable_version_id, seed.running_proposal_id
    owner = await claim_owner(engine, seed.running_operation_id)
    refs = {"proposal_id": proposal, "profile_version_id": v}
    kw = await call_kw(engine, owner, purpose="propose_queries", refs=refs)
    async with engine.connect() as holder:
        await holder.begin()
        if step == "reservation":
            res = await spend_ledger.reserve(holder, **kw)
        else:
            res = await reserve(engine, kw)  # committed before; its marker is the open transaction
            await spend_ledger.mark_dispatched(holder, res, owner, now=kw["now"])
        holder_pid = await pid_of(holder)
        admitting = background(open_due(engine))
        found = await waits(migrated, admitting)
        if step == "reservation":  # A0's first lock: the reservation's step 0 holds it shared
            wait = one_wait(found, "active_config")
            assert wait.key == "1"
        else:  # A0's cancel, the owner row's update
            wait = one_wait(found, "operations")
            assert wait.key == str(seed.running_operation_id)
        assert wait.blocking_pids == (holder_pid,)
        await holder.commit()
    assert await asyncio.wait_for(admitting, WAIT_S) == 1
    assert await scalar(engine, _OPERATION_STATUS, o=seed.running_operation_id) == "cancelled"
    if step == "reservation":  # its marker comes after A0: fenced, and the try released unsent
        with pytest.raises(FencedOut):
            async with engine.begin() as conn:
                await spend_ledger.mark_dispatched(conn, res, owner, now=utcnow())
        async with engine.begin() as conn:
            await spend_ledger.release_never_dispatched(conn, res, now=utcnow())
    manifest = await run_purge(engine, purger, v)
    assert res.llm_call_id in redacted_calls(manifest)
    call = await one(engine, _CALL, c=res.llm_call_id)
    assert call.content_purged_at is not None and call.output_json is None
    if step == "dispatch_marker":
        await settle_billed(engine, res)
        await assert_billed_without_output(engine, res.llm_call_id)


@pytest.mark.parametrize("first", ["request", "admission"])
async def test_a_paused_proposal_request_cannot_reopen_the_call_set(
    engine: AsyncEngine,
    purger: Purger,
    migrated: PgServer,
    logged_in_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    first: str,
) -> None:
    """Through the route and its middleware. (request) The request holds `active_config FOR SHARE`, has read
    the active version V and is paused before `request_proposal`: an activation started meanwhile waits for
    it; the request creates V's operation and commits; two activations make V purgeable; A0 cancels that
    operation. (admission) A0 holds `active_config FOR UPDATE` while the request starts: the request waits,
    and then proposes for the version active now. In both, no `propose_queries` operation of V is live after
    A0, no call of V is reserved after it, and a response settled after the completion stores nothing."""
    seed = await seeded(engine)
    v = await confirm_profile(engine, "principal")  # active, with the running proposal its confirm requested
    async with engine.connect() as conn:
        (confirm_op,) = [int(i) for i in (await conn.execute(_LIVE_PROPOSAL_OPERATIONS, {"v": v})).scalars()]
        (proposal,) = [int(p.id) for p in (await conn.execute(_PROPOSALS, {"v": v})).all()]

    async def request() -> httpx.Response:
        return await logged_in_client.post(
            "/api/profile/query-proposals", headers=idem(f"proposal-{first}-01")
        )

    if first == "request":
        async with (
            engine.begin() as conn
        ):  # its execution is gone: the running proposal has no live operation
            assert await ops.cancel_operation(conn, confirm_op, reason="test")
        paused = PausePoint(profile_routes.PROPOSAL_BEFORE_REQUEST)
        monkeypatch.setattr(profile_routes, "afault_point", paused)
        requesting = background(request())
        await asyncio.wait_for(paused.reached.wait(), WAIT_S)
        activating = background(confirm_profile(engine, "lead"))
        assert one_wait(await waits(migrated, activating), "active_config").key == "1"
        paused.resume()
        r = await asyncio.wait_for(requesting, WAIT_S)
        assert (r.status_code, r.json()) == (202, {"proposal_id": proposal})
        await asyncio.wait_for(activating, WAIT_S)
        (operation,) = await ids(engine, _LIVE_PROPOSAL_OPERATIONS, v=v)  # the request's, for V
        assert operation != confirm_op
        owner = await claim_owner(engine, operation)
        sent = await proposal_try(engine, owner, proposal, v)
        await confirm_profile(engine, "head")  # V is neither active nor the previous one now
        assert await open_due(engine) == 3  # the seed's two old versions and V
    else:
        owner = await claim_owner(engine, confirm_op)
        sent = await proposal_try(engine, owner, proposal, v)
        await two_activations(engine)()
        active = int(await scalar(engine, _ACTIVE_VERSION))
        async with engine.connect() as holder:
            await holder.begin()
            await holder.execute(sa.text("SELECT 1 FROM profile_versions WHERE id = :v FOR SHARE"), {"v": v})
            admitting = background(
                open_due(engine)
            )  # A0 of V waits at its version lock, `active_config` held
            assert one_wait(await waits(migrated, admitting), "profile_versions").key == str(v)
            requesting = background(request())
            found = await waits(migrated, admitting, requesting, count=2)
            assert one_wait(found, "active_config").key == "1"
            await holder.rollback()
        assert await asyncio.wait_for(admitting, WAIT_S) == 3
        r = await asyncio.wait_for(requesting, WAIT_S)
        assert r.status_code == 202
        made = int(r.json()["proposal_id"])
        assert (
            await scalar(engine, "SELECT profile_version_id FROM query_proposals WHERE id = :p", p=made)
            == active
        )
    assert await ids(engine, _LIVE_PROPOSAL_OPERATIONS, v=v) == []
    calls = await scalar(engine, _CALL_COUNT)
    refs = {"proposal_id": proposal, "profile_version_id": v}
    with pytest.raises(FencedOut):
        await reserve(
            engine, await call_kw(engine, owner, purpose="propose_queries", refs=refs, logical_call_no=2)
        )
    assert await scalar(engine, _CALL_COUNT) == calls
    manifest = await run_purge(engine, purger, v)
    assert sent.llm_call_id in redacted_calls(manifest)
    await settle_billed(engine, sent)
    await assert_billed_without_output(engine, sent.llm_call_id)
    assert seed.purgeable_version_id in {o.version_id for o in await opened(engine)}


@pytest.mark.parametrize("state", ["profile_fence", "global_fence", "purged"])
async def test_request_proposal_refuses_a_fenced_or_purged_version(
    engine: AsyncEngine, purger: Purger, state: str
) -> None:
    """`request_proposal`'s own check, called directly (the callers' rule makes it unreachable): a version
    under an unreleased `profile` or `global` fence, or purged, is an `InvariantViolation`, nothing written;
    the active version is proposed for as before."""
    seed = await seeded(engine)
    v = seed.purgeable_version_id
    if state == "global_fence":
        async with engine.begin() as conn:
            await insert_test_fence(conn, kind="global")
    else:
        assert await open_due(engine) == 1
        if state == "purged":
            await run_purge(engine, purger, v)
    calls = await scalar(engine, "SELECT count(*) FROM operations")
    with pytest.raises(InvariantViolation):
        async with engine.begin() as conn:
            await proposals.request_proposal(conn, v, defer=None)
    assert await scalar(engine, "SELECT count(*) FROM operations") == calls
    if state != "global_fence":
        async with engine.begin() as conn:
            proposal_id, _, _ = await proposals.request_proposal(conn, seed.active_version_id, defer=None)
        assert proposal_id > 0


@pytest.mark.parametrize("first", ["activation", "admission"])
async def test_case_activation_and_admission_serialise(
    engine: AsyncEngine,
    purger: Purger,
    migrated: PgServer,
    logged_in_client: httpx.AsyncClient,
    first: str,
) -> None:
    """Through the route. (activation) The activation holds `active_config FOR SHARE` (here waiting for its
    case's job row) when the retention rule runs: A0 waits for it, then reads the active case, and no purge
    opens. (admission) A0 holds `active_config FOR UPDATE` (here waiting for the version's row): the
    activation waits, then meets the fence: 409 `conflict_state`, `purge_in_progress`, the case stays
    inactive. After the completion an activation of a case on the version is still refused: the version is
    purged."""
    seed = await seeded(engine)
    v, case = seed.purgeable_version_id, seed.purgeable_case_id
    url = f"/api/golden-cases/{case}"
    async with engine.connect() as holder:
        await holder.begin()
        if first == "activation":
            await holder.execute(
                sa.text("SELECT 1 FROM jobs WHERE id = :j FOR UPDATE"), {"j": seed.closed_job_id}
            )
            activating = background(logged_in_client.patch(url, json={"active": True}))
            assert one_wait(await waits(migrated, activating), "jobs").key == str(seed.closed_job_id)
            admitting = background(open_due(engine))
            found = await waits(migrated, activating, admitting, count=2)
            assert one_wait(found, "active_config").key == "1"
        else:
            await holder.execute(sa.text("SELECT 1 FROM profile_versions WHERE id = :v FOR SHARE"), {"v": v})
            admitting = background(open_due(engine))
            assert one_wait(await waits(migrated, admitting), "profile_versions").key == str(v)
            activating = background(logged_in_client.patch(url, json={"active": True}))
            found = await waits(migrated, admitting, activating, count=2)
            assert one_wait(found, "active_config").key == "1"
        await holder.rollback()
    r = await asyncio.wait_for(activating, WAIT_S)
    admitted = await asyncio.wait_for(admitting, WAIT_S)
    if first == "activation":
        assert r.status_code == 200 and r.json()["active"] is True
        assert admitted == 0 and await opened(engine) == []
        return
    assert (r.status_code, r.json()["code"], r.json()["detail"]) == (
        409,
        "conflict_state",
        "purge_in_progress",
    )
    assert admitted == 1
    assert await scalar(engine, "SELECT active FROM golden_cases WHERE id = :g", g=case) is False
    await run_purge(engine, purger, v)
    current = "SELECT current_revision_id FROM jobs WHERE id = :j"
    revision = int(await scalar(engine, current, j=seed.active_job_id))
    async with engine.begin() as conn:  # a case on the purged version (none can be made after A2 otherwise)
        late = await insert_golden_case(conn, Job(seed.active_job_id, revision), v, active=False)
    r = await logged_in_client.patch(f"/api/golden-cases/{late}", json={"active": True})
    assert (r.status_code, r.json()["detail"]) == (409, "purge_in_progress")


# ---- what the purge deletes and keeps ----------------------------------------------------------------------


async def test_the_versions_inactive_golden_cases_are_deleted(engine: AsyncEngine, purger: Purger) -> None:
    """The version's inactive case goes with its eval case result and its eval calls (cascade); an eval call's
    entry still `dispatched` (a run that included the case before it was made inactive) is settled first,
    `ambiguous` at its reservation, so no entry is left without its call (G04)."""
    seed = await seeded(engine)
    case = seed.purgeable_case_id
    async with engine.begin() as conn:
        run = await insert_eval_run(conn, "completed")
        ledger, call = await insert_llm_call(conn, purpose="generate", eval_run_id=run)
        await conn.execute(
            sa.text("UPDATE llm_calls SET golden_case_id = :g WHERE id = :c"), {"g": case, "c": call}
        )
        await transition(conn, "llm_spend_ledger", ledger, ["reserved"], "dispatched")
        await transition(conn, "llm_calls", call, ["reserved"], "dispatched")
    assert await scalar(engine, _CASE_ROWS, g=case) == 4  # the case, its seeded result and call, this call
    assert await open_due(engine) == 1
    await run_purge(engine, purger, seed.purgeable_version_id)
    assert await scalar(engine, _CASE_ROWS, g=case) == 0
    assert await scalar(engine, "SELECT state FROM llm_spend_ledger WHERE id = :l", l=ledger) == "ambiguous"


@pytest.mark.parametrize("shared", [False, True])
async def test_the_cv_file_is_removed_only_when_no_retained_version_uses_it(
    engine: AsyncEngine, purger: Purger, shared: bool
) -> None:
    """The version's CV file goes from the store, and its row loses the texts (`purged_at` set), unless a
    retained version uses the same file (here the previous one): then neither is in the manifest, and both
    stay."""
    seed = await seeded(engine)
    cv = seed.purgeable_cv_file_id
    storage_path = (await one(engine, _CV_ROW, c=cv)).storage_path
    stored = purger.cfg.cv_store_dir / storage_path
    stored.write_bytes(b"sealed")
    if shared:
        async with engine.begin() as conn:
            await conn.execute(
                sa.text("UPDATE profile_versions SET cv_file_id = :c WHERE id = :v"),
                {"c": cv, "v": seed.previous_version_id},
            )
    assert await open_due(engine) == 1
    manifest = await run_purge(engine, purger, seed.purgeable_version_id)
    assert manifest.scope is not None
    tables = [(op["op"], op.get("table", op.get("volume"))) for op in manifest.scope["operations"]]
    row = await one(engine, _CV_ROW, c=cv)
    if shared:
        assert ("field_redact", "cv_files") not in tables and ("file_remove", "cv_store") not in tables
        assert stored.exists() and row.storage_path == storage_path and row.purged_at is None
        assert not (row.raw_gone or row.stripped_gone)
    else:
        assert ("field_redact", "cv_files") in tables and ("file_remove", "cv_store") in tables
        assert not stored.exists() and row.storage_path is None and row.purged_at is not None
        assert row.raw_gone and row.stripped_gone


async def test_profile_purge_purges_exports(engine: AsyncEngine, purger: Purger) -> None:
    """Deviation 18: a profile purge deletes every export, which may hold the purged content: the seed's
    `ready` export ends `purged` without its path, and its file is gone."""
    seed = await seeded(engine)
    path = await scalar(engine, "SELECT storage_path FROM exports WHERE id = :e", e=seed.ready_export_id)
    exported = purger.cfg.exports_dir / path
    exported.write_bytes(b"sealed")
    assert await open_due(engine) == 1
    await run_purge(engine, purger, seed.purgeable_version_id)
    export = await one(
        engine, "SELECT status, storage_path FROM exports WHERE id = :e", e=seed.ready_export_id
    )
    assert (export.status, export.storage_path) == ("purged", None)
    assert not exported.exists()


# ---- activation, identical content and the eval ------------------------------------------------------------


async def test_activating_the_fenced_version_is_409_profile_purge_in_progress(engine: AsyncEngine) -> None:
    """A confirm of content identical to a version under its profile purge's fence would reuse that version:
    409 `profile_purge_in_progress`, nothing written, the draft still `ready`."""
    await seeded(engine)
    v = await confirm_profile(engine, "principal")
    await two_activations(engine)()
    assert await open_due(engine) == 3
    await of_version(engine, v)
    draft = await ready_draft(engine, "principal")
    versions = await scalar(engine, "SELECT count(*) FROM profile_versions")
    with pytest.raises(Refusal) as refused:
        await confirm(engine, draft)
    assert (refused.value.status, refused.value.code) == (409, "profile_purge_in_progress")
    assert await scalar(engine, "SELECT count(*) FROM profile_versions") == versions
    assert await scalar(engine, "SELECT status FROM profile_drafts WHERE id = :d", d=draft) == "ready"


async def test_identical_content_confirmed_later_is_a_new_version(
    engine: AsyncEngine, purger: Purger
) -> None:
    """§18.2 N12, its live half: content identical to a purged version, confirmed after the purge, becomes a
    new version with a new id, its own tuple and the next number; the purged row keeps its id, its hash and no
    content."""
    await seeded(engine)
    v = await confirm_profile(engine, "principal")
    await two_activations(engine)()
    assert await open_due(engine) == 3
    await run_purge(engine, purger, v)
    purged = await one(engine, _VERSION, v=v)
    assert purged.redacted and purged.purged_at is not None
    highest = await scalar(engine, "SELECT max(version_no) FROM profile_versions WHERE NOT synthetic_eval")
    again = await confirm_profile(engine, "principal")
    assert again > v
    new = await one(engine, _VERSION, v=again)
    assert not new.redacted and new.version_no == highest + 1 and new.profile_hash == purged.profile_hash
    assert await scalar(engine, _ACTIVE_VERSION) == again
    old_tuples, new_tuples = await ids(engine, _TUPLES, v=v), await ids(engine, _TUPLES, v=again)
    assert old_tuples and new_tuples and set(old_tuples).isdisjoint(new_tuples)
    assert (await one(engine, _VERSION, v=v)).redacted  # never revived


@pytest.mark.parametrize("cases", ["active", "inactive", "none"])
async def test_synthetic_eval_versions_are_never_selected(engine: AsyncEngine, cases: str) -> None:
    """Deviation 25: a synthetic version (`synthetic_eval`) is never selected, with active cases, with
    inactive cases only, or with no case left; `seed-adversarial` run afterwards reuses each one intact."""
    now = utcnow()
    seed = await seeded(engine, now)
    await seed_adversarial(engine)
    async with engine.connect() as conn:
        case_ids = [
            int(i)
            for i in (
                await conn.execute(
                    sa.text("SELECT id FROM golden_cases WHERE source = 'adversarial' ORDER BY id")
                )
            ).scalars()
        ]
    async with engine.begin() as conn:
        for case_id in case_ids:
            if cases == "inactive":
                await golden.set_flags(conn, case_id, active=False, is_canary=None, now=now)
            elif cases == "none":
                await golden.delete_case(conn, case_id)
    assert await open_due(engine, now) == 1
    assert [o.version_id for o in await opened(engine)] == [seed.purgeable_version_id]
    again = await seed_adversarial(engine)
    assert (again.profiles, again.jobs, again.cases) == (0, 0, len(case_ids) if cases == "none" else 0)
    synthetic = await one(
        engine,
        "SELECT count(*) AS n, count(*) FILTER (WHERE purged_at IS NULL AND profile_json IS NOT NULL) "
        "AS intact "
        "FROM profile_versions WHERE synthetic_eval",
    )
    assert synthetic.n == synthetic.intact > 0


async def test_the_eval_skips_cases_of_the_fenced_profile(
    engine: AsyncEngine,
    purger: Purger,
    fake_llm: FakeLLM,
    current_epoch: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A golden run listed an active case of a user's version V; while the run is on its first case the case
    is made inactive and V's purge admitted: when the run comes to the case it finds V's `profile` fence and
    skips it as `fenced`, with no row. The purge then deletes the case."""
    async with engine.begin() as conn:
        await activate_test_policy(conn)
    await seed_adversarial(engine)
    await fake_eval_answers(engine, fake_llm)
    async with engine.begin() as conn:
        version = (await insert_profile_chain(conn, 71)).profile_version_id
        for n in (72, 73):  # the previous version and the newest one
            await insert_profile_chain(conn, n)
        case = await insert_golden_case(conn, await insert_job(conn), version)  # after the adversarial ones
    started = await start_eval(engine)

    async def admit() -> None:
        async with engine.begin() as conn:
            await golden.set_flags(conn, case, active=False, is_canary=None, now=utcnow())
        assert await open_due(engine) == 1

    monkeypatch.setattr(eval_runner, "afault_point", on_arrival(eval_runner.BEFORE_CASE, 1, admit))
    await execute(engine, started.operation_id, eval_deps(engine, fake_llm), epoch=current_epoch)
    run = await run_row(engine, started.eval_run_id)
    assert (run.status, run.metrics["cases"]["fenced"]) == ("completed", [case])
    assert case not in {r.golden_case_id for r in await case_rows(engine, started.eval_run_id)}
    await run_purge(engine, purger, version)
    assert await scalar(engine, _CASE_ROWS, g=case) == 0
