"""The batch preparation (spec §10.10 L3939-3951, §6.9.3 L1188-1190, §18.2 L5709, L5727; plan Task 16,
Deviations 27 and 38, Rulings T16-c, T16-d, Codex r1 #18, #22, #23, r2 B8-B12, r3 B1-B3, B19, B20, r4 B9, r5
B1): the eligibility of each candidate, the order, the cuts to `batch_max_items`, to the byte cap, to the
allocation and to the hard cap's headroom, the render before the month row, the minimum before and after the
CAS, a claim between the candidates and the CAS, a second submission in flight, the follow-up holds,
`reserve_batch` on the migrated database, `abandon`, the lock schedule against funded reservations, a cap edit
and node 9's cancel, a reclaimed execution, the recovery of a lost one whatever the sweep order, and the batch
lock of a re-deferred job.

`prepare` runs directly (`tests.support.batches.prepared_submission`) with a copy of the active settings
whose `scoring` fields a test may take below the settings model's floors (`batch_min_targets` 200), or through
the `batch_submit` operation (`op_runner`). The targets are synthetic ads of the score tuple.
"""

import asyncio
import uuid
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, cast

import procrastinate
import psycopg
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db.sql import LOCK_NOT_AVAILABLE, pg_error
from jobhunter.db.transitions import transition
from jobhunter.errors import NodeFailed
from jobhunter.graphs.score import nodes as score_nodes
from jobhunter.graphs.score.state import TargetState
from jobhunter.llm import budget, bulk, spend_ledger
from jobhunter.llm.batches import BatchRequest, Rejected
from jobhunter.llm.bounds import PROMPT_OVERHEAD_FLOOR, PROMPT_OVERHEAD_TOKENS
from jobhunter.llm.budget import SIZING_INPUT_BOUND_TOKENS, month_of
from jobhunter.llm.errors import BudgetExhausted, BulkAllocationExceeded
from jobhunter.llm.gateway import LLMGateway, RenderedPrompt, pinned_node_config
from jobhunter.llm.pricing import worst_case_usd
from jobhunter.llm.prompts.render import score_schema
from jobhunter.llm.spend_ledger import (
    BatchItemReservation,
    BatchSubmissionOwner,
    Reservation,
    TargetAttemptOwner,
    change_cap,
    ensure_month,
    reserve,
    reserve_batch,
)
from jobhunter.scoring import batches as batches_mod
from jobhunter.scoring import cycles, steps
from jobhunter.scoring import targets as targets_mod
from jobhunter.scoring.batches import ELIGIBLE_SQL, NotPrepared, Prepared
from jobhunter.settings.dryrun import active_settings
from jobhunter.settings.model import ModelPolicy, Settings
from jobhunter.worker import epoch as epoch_module
from jobhunter.worker import operations as ops
from jobhunter.worker.epoch import WorkerIdentity, claim_epoch_and_sweep
from jobhunter.worker.lock import LockSession
from jobhunter.worker.tasks import job_task_name
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.batches import (
    BulkTargets,
    batch_ledger,
    bulk_row,
    bulk_with_targets,
    items_of,
    prepared_submission,
    submission_rows,
)
from tests.support.faults import PausePoint, raising_at
from tests.support.fixtures import count_rows
from tests.support.llm import FakeLLM, _read_execution_config, reserve_kw, scoring_kw
from tests.support.m2 import insert_profile_chain
from tests.support.m3 import (
    NOW,
    Job,
    insert_batch_item,
    insert_det_result,
    insert_published,
    insert_submission,
)
from tests.support.pg import PgServer, lock_wait_details, pg_one
from tests.support.procs import await_until
from tests.support.profile import OpRunner
from tests.support.reconcile import run_reconciler
from tests.support.scoring import (
    claim_again,
    claimed_target,
    fence_jobs,
    invoke_subgraph,
    operations_of,
    reclaim_now,
    score_deps,
    synthetic_c_draft,
    target_of,
)

pytestmark = pytest.mark.integration
Q = Decimal("0.000001")
_LOCK_MONTH = sa.text("SELECT 1 FROM llm_budget WHERE month = :m FOR UPDATE")
_LOCK_BULK = sa.text("SELECT 1 FROM bulk_operations WHERE id = :b FOR NO KEY UPDATE")
_BATCH_ROWS = sa.text(
    "SELECT (SELECT count(*) FROM batch_submissions) AS submissions, "
    "(SELECT count(*) FROM score_attempts WHERE mode = 'batch') AS attempts, "
    "(SELECT count(*) FROM batch_items) AS items, "
    "(SELECT count(*) FROM llm_spend_ledger) AS entries"
)
_ITEM_TEMPLATE = sa.text(
    "SELECT c.prompt_template_version FROM batch_items i JOIN llm_calls c ON c.id = i.llm_call_id "
    "WHERE i.target_id = :t"
)
_RESERVED = sa.text(
    "SELECT l.reserved_usd FROM batch_items i JOIN llm_calls c ON c.id = i.llm_call_id "
    "JOIN llm_spend_ledger l ON l.id = c.spend_ledger_id WHERE i.submission_id = :s ORDER BY i.id"
)


@pytest.fixture
async def gateway(engine: AsyncEngine, fake_llm: FakeLLM, current_epoch: int) -> LLMGateway:
    async with engine.connect() as conn:  # `scoring_kw` names the seeded execution config
        await _read_execution_config(conn)
    return fake_llm.gateway(engine)


async def settings_with(engine: AsyncEngine, **scoring: Any) -> Settings:
    """The active settings with `scoring` fields replaced, unvalidated (a direct `prepare` takes them)."""
    async with engine.connect() as conn:
        active = await active_settings(conn)
    return active.model_copy(update={"scoring": active.scoring.model_copy(update=scoring)})


async def execute(engine: AsyncEngine, statement: str, **params: Any) -> None:
    async with engine.begin() as conn:
        await conn.execute(sa.text(statement), params)


async def batch_rows(engine: AsyncEngine) -> tuple[int, int, int, int]:
    async with engine.connect() as conn:
        row = (await conn.execute(_BATCH_ROWS)).one()
    return int(row.submissions), int(row.attempts), int(row.items), int(row.entries)


def item_targets(prepared: Prepared) -> list[int]:
    """The targets of the prepared requests, in request order (`t<target>_a<attempt>`)."""
    return [int(r.custom_id.split("_")[0][1:]) for r in prepared.requests]


async def dry_prepare(
    engine: AsyncEngine, gateway: LLMGateway, setup: BulkTargets, settings: Settings
) -> tuple[Prepared, list[Decimal]]:
    """A preparation rolled back: what it would prepare, and each item's reservation in request order."""
    assert setup.operation_id is not None
    async with engine.begin() as conn:
        claim = await ops.claim_operation(conn, setup.operation_id, epoch=1)
    assert claim is not None
    async with engine.connect() as conn:
        await conn.begin()
        result = await batches_mod.prepare(
            conn,
            bulk_operation_id=setup.bulk_operation_id,
            llm_tuple_id=setup.llm_tuple_id,
            operation=claim,
            settings=settings,
            fake_llm=True,
            gateway=gateway,
            now=utcnow(),
        )
        assert isinstance(result, Prepared)
        reserved = [
            Decimal(r) for r in (await conn.execute(_RESERVED, {"s": result.submission_id})).scalars()
        ]
        await conn.rollback()
    async with engine.begin() as conn:  # the operation pending again for the real preparation
        await ops.requeue_operation(conn, claim, error_code="test", not_before=utcnow())
    return result, reserved


# ---- eligibility (L3939, §10.10; Codex r4 B9, r5 B1) -------------------------------------------------------

_FIRST_AT = "UPDATE score_targets SET {set} WHERE id = :t"


async def _exclude(engine: AsyncEngine, setup: BulkTargets, case: str) -> None:
    target = setup.targets[0]
    t = target.target_id
    if case == "batch_followup_lane":
        await execute(engine, _FIRST_AT.format(set="lane = 'batch_followup'"), t=t)
    elif case == "score_now_lane":
        await execute(engine, _FIRST_AT.format(set="lane = 'score_now'"), t=t)
    elif case == "batch_expired_reason":
        await execute(engine, _FIRST_AT.format(set="state_reason = 'batch_expired'"), t=t)
    elif case == "earlier_item_with_a_result":
        async with engine.begin() as conn:
            await conn.execute(sa.text(_FIRST_AT.format(set="attempt_no = 1")), {"t": t})
            done = await insert_submission(
                conn,
                setup.llm_tuple_id,
                setup.bulk_operation_id,
                state="reconciled",
                submit_started_at=NOW,
                provider_batch_id="msgbatch_earlier",
                results_complete_at=NOW,
                reconciled_at=NOW,
            )
            await insert_batch_item(
                conn, done, t, result_type="canceled", processed_at=NOW, decision="retry_online"
            )
    elif case == "attempts_exhausted":
        await execute(engine, _FIRST_AT.format(set="charged_attempts = 4"), t=t)
    elif case == "override":
        await execute(engine, _FIRST_AT.format(set="override = 'score_now', override_at = now()"), t=t)
    elif case == "fenced_job":
        async with engine.begin() as conn:
            await fence_jobs(conn, target.job_id)
    elif case == "backoff_not_passed":
        await execute(engine, _FIRST_AT.format(set="not_before = now() + interval '1 hour'"), t=t)
    else:
        raise AssertionError(case)


@pytest.mark.parametrize(
    "case",
    [
        "batch_followup_lane",
        "score_now_lane",
        "batch_expired_reason",
        "earlier_item_with_a_result",
        "attempts_exhausted",
        "override",
        "fenced_job",
        "backoff_not_passed",
    ],
)
async def test_every_eligibility_clause_excludes_exactly_its_target(
    engine: AsyncEngine, gateway: LLMGateway, case: str
) -> None:
    """No re-batching (L5727) and the §10.10 checklist: each clause keeps out its target alone."""
    setup = await bulk_with_targets(engine, 3)
    await _exclude(engine, setup, case)
    result, _ = await prepared_submission(
        engine, gateway, setup, settings=await settings_with(engine, batch_min_targets=1)
    )
    assert isinstance(result, Prepared), result
    assert sorted(item_targets(result)) == sorted(t.target_id for t in setup.targets[1:])
    assert (await target_of(engine, setup.targets[0].target_id)).state == "pending"


async def test_eligibility_is_decided_per_candidate(
    engine: AsyncEngine, fake_llm: FakeLLM, gateway: LLMGateway
) -> None:
    """Codex r5 B1: the draft fragment is correlated to each candidate row and binds `:policy` only."""
    setup = await bulk_with_targets(engine, 2)
    drafted = await _online_draft(engine, fake_llm, setup)
    policy = await _active_policy(engine)
    async with engine.connect() as conn:
        eligible = await conn.execute(
            sa.text(f"SELECT t.id FROM score_targets t WHERE {ELIGIBLE_SQL} ORDER BY t.id"),  # noqa: S608
            {"b": setup.bulk_operation_id, "policy": policy},
        )
        assert list(eligible.scalars()) == [t.target_id for t in setup.targets]
    result, _ = await prepared_submission(
        engine, gateway, setup, settings=await settings_with(engine, batch_min_targets=2)
    )
    assert isinstance(result, Prepared)
    assert sorted(item_targets(result)) == [t.target_id for t in setup.targets]
    assert drafted not in item_targets(result)


async def _active_policy(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        found = await conn.execute(sa.text("SELECT evaluation_policy_id FROM active_config WHERE id = 1"))
        return int(found.scalar_one())


async def _online_draft(engine: AsyncEngine, fake_llm: FakeLLM, setup: BulkTargets) -> int:
    """A target of the bulk operation whose online attempt stored its round-0 draft, then lost its claim
    (the C11 crash): `pending` again with its draft under the active policy."""
    claimed = await claimed_target(engine, bulk_operation_id=setup.bulk_operation_id)
    fake_llm.answer("generate", claimed.job_revision_id, synthetic_c_draft())
    deps = await score_deps(engine, fake_llm.gateway(engine))
    patched = pytest.MonkeyPatch()
    patched.setattr(score_nodes, "afault_point", raising_at(score_nodes.AFTER_DRAFT))
    try:
        with pytest.raises(NodeFailed):
            await invoke_subgraph(deps, claimed)
    finally:
        patched.undo()
    await reclaim_now(engine, claimed.target_id)
    return claimed.target_id


async def test_a_target_with_an_online_draft_is_resumed_not_batched(
    engine: AsyncEngine, fake_llm: FakeLLM, gateway: LLMGateway
) -> None:
    """Codex r4 B9: a target whose online attempt stored a generation and returned to `pending` is resumed
    by `load_context` from that draft, never batched and paid for again."""
    setup = await bulk_with_targets(engine, 2)
    drafted = await _online_draft(engine, fake_llm, setup)
    served = fake_llm.served
    result, _ = await prepared_submission(
        engine, gateway, setup, settings=await settings_with(engine, batch_min_targets=1)
    )
    assert isinstance(result, Prepared) and drafted not in item_targets(result)
    assert (await target_of(engine, drafted)).state == "pending"
    resumed = await claim_again(engine, drafted)  # the next cycle's claim of it
    final = await invoke_subgraph(await score_deps(engine, fake_llm.gateway(engine)), resumed)
    assert final["resumed"] is True and final["outcome"] == "published_accepted"
    assert fake_llm.served == served  # no second generate call
    assert not [r for r in fake_llm.requests[served:] if r.purpose == "generate"]


# ---- the order and the cuts (L3942, L3943; Deviation 27) ---------------------------------------------------


async def test_the_order_is_availability_then_the_previous_score_then_similarity(
    engine: AsyncEngine, gateway: LLMGateway
) -> None:
    setup = await bulk_with_targets(engine, 4)
    a, b, c, d = setup.targets  # similarity descending with the insertion order
    async with engine.begin() as conn:
        await conn.execute(sa.text("UPDATE jobs SET availability = 'stale' WHERE id = :j"), {"j": a.job_id})
        other = (await insert_profile_chain(conn, 41)).llm_tuple_id  # an earlier tuple scored c's job
        published = await insert_published(conn, Job(c.job_id, c.job_revision_id), other)
        scope = (await conn.execute(sa.text("SELECT scope_version_id FROM active_config"))).scalar_one()
        await insert_det_result(
            conn, published.result_id, c.job_id, int(scope), low=Decimal(60), high=Decimal(80)
        )
    result, _ = await prepared_submission(
        engine, gateway, setup, settings=await settings_with(engine, batch_min_targets=1)
    )
    assert isinstance(result, Prepared)
    # active first; among them the previous tuple's score_mid, then similarity; the stale job last
    assert item_targets(result) == [c.target_id, b.target_id, d.target_id, a.target_id]
    assert [t for t, _ in await items_of(engine, result.submission_id)] == item_targets(result)


async def test_the_cut_to_batch_max_items(engine: AsyncEngine, gateway: LLMGateway) -> None:
    setup = await bulk_with_targets(engine, 6)
    result, _ = await prepared_submission(
        engine, gateway, setup, settings=await settings_with(engine, batch_min_targets=1, batch_max_items=4)
    )
    assert isinstance(result, Prepared) and result.request_count == 4
    assert item_targets(result) == [t.target_id for t in setup.targets[:4]]


async def test_the_cut_to_the_byte_cap(
    engine: AsyncEngine, gateway: LLMGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deviation 27, A2 B14: at most `BATCH_MAX_BYTES` (200 MiB, the provider's 256 MB with room) of rendered
    params; patched here to what three of five requests take."""
    assert batches_mod.BATCH_MAX_BYTES == 200 * 1024 * 1024
    setup = await bulk_with_targets(engine, 5)
    settings = await settings_with(engine, batch_min_targets=1)
    dry, _ = await dry_prepare(engine, gateway, setup, settings)
    sizes = [batches_mod.request_bytes(r) for r in dry.requests]
    monkeypatch.setattr(batches_mod, "BATCH_MAX_BYTES", sum(sizes[:3]) + sizes[3] // 2)
    result, _ = await prepared_submission(engine, gateway, setup, settings=settings)
    assert isinstance(result, Prepared) and result.request_count == 3
    assert item_targets(result) == item_targets(dry)[:3]


async def test_the_cut_to_the_allocation(engine: AsyncEngine, gateway: LLMGateway) -> None:
    """L1189: the largest `n` whose worst cases plus `n x hold` fit; each item's worst case is its own."""
    setup = await bulk_with_targets(engine, 5)
    settings = await settings_with(engine, batch_min_targets=1)
    dry, reserved = await dry_prepare(engine, gateway, setup, settings)
    budget_usd = sum(reserved[:3]) + 3 * dry.hold_usd + reserved[3] / 2
    await execute(
        engine,
        "UPDATE bulk_operations SET budget_usd = :u WHERE id = :b",
        u=budget_usd,
        b=setup.bulk_operation_id,
    )
    result, _ = await prepared_submission(engine, gateway, setup, settings=settings)
    assert isinstance(result, Prepared) and result.request_count == 3
    assert result.reserved_usd == sum(reserved[:3])
    account = await bulk_row(engine, setup.bulk_operation_id)
    assert account.spent_usd + account.followup_held_usd <= account.budget_usd
    assert (account.spent_usd, account.followup_held_usd) == (sum(reserved[:3]), 3 * dry.hold_usd)


async def set_scoring_headroom(engine: AsyncEngine, usd: Decimal) -> None:
    """This month's scoring headroom set to `usd`: spend counted outside any entry (the counters alone, the
    scoring part with the total, so the non-scoring reserve is unchanged)."""
    now = utcnow()
    month = month_of(now)
    async with engine.begin() as conn:
        p = await spend_ledger.current_params(conn)
        await ensure_month(conn, month, p, now)
        row = await spend_ledger.read_month(conn, month, p, now=now)
        assert row is not None
        await conn.execute(
            sa.text(
                "UPDATE llm_budget SET counted_usd = counted_usd + :d, "
                "scoring_counted_usd = scoring_counted_usd + :d WHERE month = :m"
            ),
            {"d": budget.headroom(row, "scoring", p) - usd, "m": month},
        )


async def test_the_cut_to_the_hard_cap_headroom(engine: AsyncEngine, gateway: LLMGateway) -> None:
    """Ruling T16-d: the cut is also the largest prefix whose worst cases fit the month's scoring headroom
    (each item's admission, L1159), so a headroom of three items and a half prepares three."""
    setup = await bulk_with_targets(engine, 5)
    settings = await settings_with(engine, batch_min_targets=1)
    dry, reserved = await dry_prepare(engine, gateway, setup, settings)
    await set_scoring_headroom(engine, sum(reserved[:3]) + reserved[3] / 2)
    result, _ = await prepared_submission(engine, gateway, setup, settings=settings)
    assert isinstance(result, Prepared) and result.request_count == 3
    assert item_targets(result) == item_targets(dry)[:3] and result.reserved_usd == sum(reserved[:3])
    assert await count_rows(engine, "score_targets", state="pending") == 2


async def test_no_item_within_the_headroom_is_budget_exhausted(
    engine: AsyncEngine, gateway: LLMGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling T16-d: only when no item fits the headroom read under the month row (here below the first
    one's worst case: the pause check, made on an unlocked read, passed) is nothing prepared, as
    `budget_exhausted`, with nothing written."""
    setup = await bulk_with_targets(engine, 3)
    settings = await settings_with(engine, batch_min_targets=1)
    _, reserved = await dry_prepare(engine, gateway, setup, settings)
    await set_scoring_headroom(engine, reserved[0] / 2)

    async def unpaused(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(cycles, "pause_reason", unpaused)
    result, _ = await prepared_submission(engine, gateway, setup, settings=settings)
    assert result == NotPrepared("budget_exhausted")
    assert await batch_rows(engine) == (0, 0, 0, 0)
    assert await count_rows(engine, "score_targets", state="pending") == 3


_PROBE_MONTH = "SELECT 1 FROM llm_budget WHERE month = %s FOR UPDATE NOWAIT"


def month_row_free(server: PgServer, month: object) -> bool:
    """Whether no transaction holds the month row (a `NOWAIT` lock of it, taken and given back)."""
    try:
        pg_one(server, "superuser", _PROBE_MONTH, month)
    except psycopg.Error as exc:
        if exc.sqlstate != LOCK_NOT_AVAILABLE:
            raise
        return False
    return True


async def test_the_prompts_are_rendered_and_sized_before_the_month_row(
    engine: AsyncEngine, gateway: LLMGateway, migrated: PgServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task review Minor 7: the render, its bound, hashes and size run before the month row is locked; only
    the cut and the writes hold it (the CAS re-checks every candidate)."""
    setup = await bulk_with_targets(engine, 3)
    month = month_of(utcnow())
    async with engine.begin() as conn:  # the row exists: a lock of it is the row's
        await ensure_month(conn, month, await spend_ledger.current_params(conn), utcnow())
    rendered: list[bool] = []
    sized: list[bool] = []
    render, size = steps.role_prompt, batches_mod.request_bytes

    def probed_render(*args: Any, **kwargs: Any) -> RenderedPrompt:
        rendered.append(month_row_free(migrated, month))
        return render(*args, **kwargs)

    def probed_size(request: BatchRequest) -> int:
        sized.append(month_row_free(migrated, month))
        return size(request)

    monkeypatch.setattr(steps, "role_prompt", probed_render)
    monkeypatch.setattr(batches_mod, "request_bytes", probed_size)
    result, _ = await prepared_submission(
        engine, gateway, setup, settings=await settings_with(engine, batch_min_targets=1)
    )
    assert isinstance(result, Prepared) and result.request_count == 3
    assert rendered == [True] * 3 and sized == [True] * 3


async def test_fits_with_the_section_21_4_figures(engine: AsyncEngine, current_epoch: int) -> None:
    """§21.4 at the measured overhead: a $37.24 allocation (40 % of $93.10) holds 347 items of the sized
    request (37,678 input tokens, 6,000 output), each with its follow-up hold at the central critic rate."""
    setup = await bulk_with_targets(engine, 0, budget_usd=Decimal("37.24"), operation=False)
    bound = PROMPT_OVERHEAD_TOKENS["claude-sonnet-5-5"] + SIZING_INPUT_BOUND_TOKENS - PROMPT_OVERHEAD_FLOOR
    worst = worst_case_usd("claude-sonnet-5-5", input_tokens_bound=bound, max_tokens=6000, batch=True)
    hold = (Decimal("0.450") * budget.c_hat_followup(ModelPolicy())).quantize(Q, ROUND_HALF_UP)
    # By hand: 37,678 x $1.25/M + 6,000 x $5/M = $0.0770975, rounded up to $0.077098; 0.45 x $0.066939 =
    # $0.03012255, half-up $0.030123; an item and its hold $0.107221, and 347 x 0.107221 = $37.205687 fits
    # $37.24 while 348 x 0.107221 = $37.312908 does not.
    assert (worst, hold) == (Decimal("0.077098"), Decimal("0.030123"))
    n = 347
    async with engine.begin() as conn:
        b = setup.bulk_operation_id
        assert await bulk.fits(conn, bulk_operation_id=b, worst_case_usd=n * worst, hold_usd=n * hold)
        assert not await bulk.fits(
            conn, bulk_operation_id=b, worst_case_usd=(n + 1) * worst, hold_usd=(n + 1) * hold
        )
        await bulk.add_hold(conn, b, Decimal("37.24") - n * (worst + hold) + Q)  # held already counts
        assert not await bulk.fits(conn, bulk_operation_id=b, worst_case_usd=n * worst, hold_usd=n * hold)


async def test_the_holds_are_given_back_never_below_zero(engine: AsyncEngine, current_epoch: int) -> None:
    setup = await bulk_with_targets(engine, 0, operation=False)
    b = setup.bulk_operation_id
    policy = await _active_policy(engine)
    async with engine.begin() as conn:
        hold = await bulk.hold_per_item(conn, evaluation_policy_id=policy, model_policy=ModelPolicy())
        assert hold == (Decimal("0.3") * Decimal("0.066939")).quantize(Q, ROUND_HALF_UP)  # the test policy
        await bulk.add_hold(conn, b, Decimal("0.5"))
        assert await bulk.release_hold(conn, b, Decimal("0.2")) == Decimal("0.2")
        assert await bulk.release_hold(conn, b, Decimal("0.5")) == Decimal("0.3")  # what was left
    assert (await bulk_row(engine, b)).followup_held_usd == 0


# ---- the minimum before and after the CAS (Codex r2 B8, B9) ------------------------------------------------


async def test_below_batch_min_targets_nothing_is_prepared(engine: AsyncEngine, gateway: LLMGateway) -> None:
    setup = await bulk_with_targets(engine, 3)
    result, _ = await prepared_submission(
        engine, gateway, setup, settings=await settings_with(engine, batch_min_targets=10)
    )
    assert result == NotPrepared("below_minimum")
    assert await batch_rows(engine) == (0, 0, 0, 0)
    assert await count_rows(engine, "score_targets", state="pending") == 3


async def _paused_prepare(
    engine: AsyncEngine,
    gateway: LLMGateway,
    setup: BulkTargets,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[PausePoint, asyncio.Task[tuple[Prepared | NotPrepared, ops.OperationClaim]]]:
    pause = PausePoint(batches_mod.AFTER_CANDIDATES)
    monkeypatch.setattr(batches_mod, "afault_point", pause)
    task = asyncio.create_task(prepared_submission(engine, gateway, setup, settings=settings))
    await asyncio.wait_for(pause.reached.wait(), 60)
    return pause, task


@pytest.mark.parametrize("claimed", [1, 10])
async def test_below_minimum_after_the_cas_rolls_everything_back(
    engine: AsyncEngine, gateway: LLMGateway, monkeypatch: pytest.MonkeyPatch, claimed: int
) -> None:
    setup = await bulk_with_targets(engine, 10)
    pause, task = await _paused_prepare(
        engine, gateway, setup, await settings_with(engine, batch_min_targets=10), monkeypatch
    )
    for target in setup.targets[:claimed]:  # online claims between the candidates and the CAS
        await claim_again(engine, target.target_id)
    pause.resume()
    result, _ = await asyncio.wait_for(task, 60)
    assert result == NotPrepared("below_minimum")
    assert await batch_rows(engine) == (0, 0, 0, 0)  # no submission, attempt, item or ledger row
    assert await count_rows(engine, "score_targets", state="pending") == 10 - claimed
    assert await count_rows(engine, "score_targets", state="in_progress") == claimed


async def test_below_minimum_after_the_cas_ends_the_operation_skipped(
    engine: AsyncEngine, op_runner: OpRunner, current_epoch: int
) -> None:
    setup = await bulk_with_targets(engine, 200)  # the settings' batch_min_targets
    pause = PausePoint(batches_mod.AFTER_CANDIDATES)
    assert setup.operation_id is not None
    running = asyncio.create_task(op_runner.run_one(setup.operation_id, pause=pause))
    await asyncio.wait_for(pause.reached.wait(), 60)
    await claim_again(engine, setup.targets[0].target_id)
    pause.resume()
    await asyncio.wait_for(running, 120)
    (op,) = await operations_of(engine, "batch_submit")
    assert (op.status, op.progress) == ("succeeded", {"skipped": "below_minimum"})
    assert await batch_rows(engine) == (0, 0, 0, 0)


async def test_an_online_claim_between_candidate_selection_and_the_cas_is_harmless(
    engine: AsyncEngine, gateway: LLMGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex r2 B8: the CAS (`FOR UPDATE SKIP LOCKED`) skips the claimed candidate; its `attempt_no + 1` was
    taken first, so no `UNIQUE (target_id, attempt_no)` conflict can arise."""
    setup = await bulk_with_targets(engine, 12)
    pause, task = await _paused_prepare(
        engine, gateway, setup, await settings_with(engine, batch_min_targets=10), monkeypatch
    )
    online = await claim_again(engine, setup.targets[3].target_id)
    pause.resume()
    result, _ = await asyncio.wait_for(task, 60)
    assert isinstance(result, Prepared) and result.request_count == 11
    assert online.target_id not in item_targets(result)
    target = await target_of(engine, online.target_id)
    assert (target.state, target.attempt_no) == ("in_progress", 1)


# ---- one in flight, the allocation under the locks (Codex r2 B10, r3 B19) ----------------------------------


@pytest.mark.parametrize(("state", "reason"), [("submitted", "in_flight"), ("submit_ambiguous", "ambiguous")])
async def test_a_second_in_flight_submission_is_not_prepared(
    engine: AsyncEngine, gateway: LLMGateway, state: str, reason: str
) -> None:
    setup = await bulk_with_targets(engine, 3)
    async with engine.begin() as conn:
        columns: dict[str, Any] = {"submit_started_at": NOW}
        if state == "submitted":
            columns["provider_batch_id"] = "msgbatch_in_flight"
        await insert_submission(conn, setup.llm_tuple_id, setup.bulk_operation_id, state=state, **columns)
    result, _ = await prepared_submission(
        engine, gateway, setup, settings=await settings_with(engine, batch_min_targets=1)
    )
    assert result == NotPrepared(reason)  # type: ignore[arg-type]
    assert await count_rows(engine, "batch_submissions") == 1


async def test_batch_one_in_flight_has_the_last_word(
    engine: AsyncEngine, gateway: LLMGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A precondition read that missed an in-flight submission (none can, while one `batch_submit` runs at
    a time): the singleton index refuses the insert and nothing is written."""
    setup = await bulk_with_targets(engine, 3)
    async with engine.begin() as conn:
        await insert_submission(
            conn,
            setup.llm_tuple_id,
            setup.bulk_operation_id,
            state="submitted",
            submit_started_at=NOW,
            provider_batch_id="msgbatch_in_flight",
        )

    async def none_in_flight(conn: object) -> None:
        return None

    monkeypatch.setattr(batches_mod, "_in_flight", none_in_flight)
    result, _ = await prepared_submission(
        engine, gateway, setup, settings=await settings_with(engine, batch_min_targets=1)
    )
    assert result == NotPrepared("in_flight")
    assert await batch_rows(engine) == (1, 0, 0, 0)
    assert await count_rows(engine, "score_targets", state="pending") == 3


async def test_an_insufficient_allocation_before_the_prepare_is_not_prepared(
    engine: AsyncEngine, gateway: LLMGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = await bulk_with_targets(engine, 3, budget_usd=Decimal("0.01"))
    called: list[object] = []

    async def spy(*args: object, **kwargs: object) -> list[Reservation]:
        called.append(kwargs)
        raise AssertionError("reserve_batch must not run without an allocation")

    monkeypatch.setattr(spend_ledger, "reserve_batch", spy)
    result, _ = await prepared_submission(
        engine, gateway, setup, settings=await settings_with(engine, batch_min_targets=1)
    )
    assert result == NotPrepared("no_allocation") and called == []
    assert await batch_rows(engine) == (0, 0, 0, 0)


async def _funded_follow_up(engine: AsyncEngine, setup: BulkTargets) -> TargetAttemptOwner:
    """A claimed target of the bulk operation with an unreleased `followup` item of a reconciled submission:
    the owner of a funded online follow-up (Codex r1 #27)."""
    claimed = await claimed_target(engine, bulk_operation_id=setup.bulk_operation_id)
    async with engine.begin() as conn:
        done = await insert_submission(
            conn,
            setup.llm_tuple_id,
            setup.bulk_operation_id,
            state="reconciled",
            submit_started_at=NOW,
            provider_batch_id="msgbatch_followed",
            results_complete_at=NOW,
            reconciled_at=NOW,
        )
        await insert_batch_item(
            conn,
            done,
            claimed.target_id,
            attempt_no=9,
            result_type="succeeded",
            processed_at=NOW,
            decision="followup",
        )
    return TargetAttemptOwner(
        claimed.target_id, uuid.UUID(claimed.claim_id), claimed.attempt_no, claimed.attempt_id
    )


def _funded_kw(owner: TargetAttemptOwner, bulk_operation_id: int) -> dict[str, Any]:
    return scoring_kw(
        owner,
        now=utcnow(),
        bound=4000,
        max_tokens=6000,
        model="claude-sonnet-5-5",
        purpose="critic",
        round=1,
        bulk_operation_id=bulk_operation_id,
    )


_LOCK_BULK_NOWAIT = sa.text("SELECT 1 FROM bulk_operations WHERE id = :b FOR NO KEY UPDATE NOWAIT")
_INSERTED_INTO = (
    "SELECT count(*) FROM pg_locks WHERE pid = %s AND locktype = 'relation' "
    "AND relation = CAST(%s AS regclass) AND mode = 'RowExclusiveLock'"
)


def inserted_into(server: PgServer, pid: int, table: str) -> bool:
    """Whether backend `pid` has written a row of `table` in its transaction (its `RowExclusiveLock`)."""
    found = pg_one(server, "superuser", _INSERTED_INTO, pid, table)
    return found is not None and int(str(found[0])) > 0


async def waits_on_the_month_row_alone(engine: AsyncEngine, server: PgServer, bulk_operation_id: int) -> None:
    """The one waiting session (a funded reservation) waits for the paused prepare at the month row (its
    `ensure_month` or `lock_month` statement, behind the prepare's accrual), and the prepare holds the bulk
    row too: a `NOWAIT` lock of it is refused."""
    (wait,) = lock_wait_details(server)
    assert "llm_budget" in wait.query, wait
    assert not inserted_into(server, wait.pid, "llm_spend_ledger")  # it reserved nothing yet
    async with engine.connect() as probe:
        await probe.begin()
        with pytest.raises(sa.exc.OperationalError) as refused:
            await probe.execute(_LOCK_BULK_NOWAIT, {"b": bulk_operation_id})
        assert pg_error(refused.value).sqlstate == LOCK_NOT_AVAILABLE
        await probe.rollback()


async def test_a_competing_funded_reservation_waits_and_sees_the_new_allocation(
    engine: AsyncEngine, gateway: LLMGateway, migrated: PgServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex r2 B10, r3 B19: the prepare holds the month and bulk rows from its sizing to its reservations, so
    a funded follow-up started meanwhile waits on the month row and, once the prepare committed, is funded
    against the new `spent_usd` and `followup_held_usd` or charged to the bucket."""
    setup = await bulk_with_targets(engine, 3)
    owner = await _funded_follow_up(engine, setup)
    settings = await settings_with(engine, batch_min_targets=1)
    async with engine.begin() as conn:  # the month exists: the prepare's lock is the row's, not an insert's
        await ensure_month(conn, month_of(utcnow()), await spend_ledger.current_params(conn), utcnow())
    dry, reserved = await dry_prepare(engine, gateway, setup, settings)
    r = worst_case_usd("claude-sonnet-5-5", input_tokens_bound=4000, max_tokens=6000)
    # The three items and their holds fill the allocation. A follow-up is funded while `spent_usd + r ≤
    # budget_usd` (L1191: the holds are its own share), so before the prepare it fits, after it it does not.
    budget_usd = sum(reserved) + 3 * dry.hold_usd
    assert r <= budget_usd and sum(reserved) + r > budget_usd
    await execute(
        engine,
        "UPDATE bulk_operations SET budget_usd = :u WHERE id = :b",
        u=budget_usd,
        b=setup.bulk_operation_id,
    )
    pause, task = await _paused_prepare(engine, gateway, setup, settings, monkeypatch)

    async def funded() -> Reservation:
        async with engine.begin() as conn:
            return await reserve(conn, **_funded_kw(owner, setup.bulk_operation_id))

    follow_up = asyncio.create_task(funded())

    async def waiting() -> bool:
        return bool(lock_wait_details(migrated)) or follow_up.done()

    await await_until(waiting, 20, 0.02)
    assert not follow_up.done()
    await waits_on_the_month_row_alone(engine, migrated, setup.bulk_operation_id)
    pause.resume()
    result, _ = await asyncio.wait_for(task, 60)
    assert isinstance(result, Prepared) and result.request_count == 3
    res = await asyncio.wait_for(follow_up, 30)
    async with engine.connect() as conn:
        funded_by = (
            await conn.execute(
                sa.text("SELECT bulk_operation_id FROM llm_spend_ledger WHERE id = :l"), {"l": res.ledger_id}
            )
        ).scalar_one()
    assert funded_by is None  # the allocation the prepare consumed no longer fits it: the bucket pays
    account = await bulk_row(engine, setup.bulk_operation_id)
    assert account.spent_usd + account.followup_held_usd <= account.budget_usd


@pytest.mark.parametrize("first", ["reservation", "prepare"])
async def test_prepare_and_a_funded_online_reservation_never_deadlock(
    engine: AsyncEngine, gateway: LLMGateway, migrated: PgServer, monkeypatch: pytest.MonkeyPatch, first: str
) -> None:
    """Codex r3 B1, with the real foreign key: a funded reservation holds the month row, then takes the bulk
    row; the prepare takes the month row, then the bulk row, and inserts its submission (whose foreign key
    key-shares the bulk row) only after both. Whichever goes first, the other waits on the month row alone,
    and both commit."""
    setup = await bulk_with_targets(engine, 3)
    owner = await _funded_follow_up(engine, setup)
    settings = await settings_with(engine, batch_min_targets=1)
    month = month_of(utcnow())
    async with engine.begin() as conn:
        await ensure_month(conn, month, await spend_ledger.current_params(conn), utcnow())
    if first == "reservation":
        async with engine.connect() as holder:  # the reservation's steps up to its month row, held
            await holder.begin()
            await spend_ledger.current_params(holder)
            await holder.execute(_LOCK_MONTH, {"m": month})
            task = asyncio.create_task(prepared_submission(engine, gateway, setup, settings=settings))

            async def prepare_waits() -> bool:
                return bool(lock_wait_details(migrated)) or task.done()

            await await_until(prepare_waits, 20, 0.02)
            (wait,) = lock_wait_details(migrated)
            assert wait.relation == "llm_budget" and not task.done()
            assert not inserted_into(migrated, wait.pid, "batch_submissions")  # no owner row before the month
            # the bulk row is free: the reservation takes it without waiting for the prepare
            await holder.execute(_LOCK_BULK_NOWAIT, {"b": setup.bulk_operation_id})
            await holder.commit()
        result, _ = await asyncio.wait_for(task, 60)
        assert isinstance(result, Prepared)
    else:
        pause, task = await _paused_prepare(engine, gateway, setup, settings, monkeypatch)

        async def funded() -> Reservation:
            async with engine.begin() as conn:
                return await reserve(conn, **_funded_kw(owner, setup.bulk_operation_id))

        follow_up = asyncio.create_task(funded())

        async def waiting() -> bool:
            return bool(lock_wait_details(migrated)) or follow_up.done()

        await await_until(waiting, 20, 0.02)
        assert not follow_up.done()
        await waits_on_the_month_row_alone(engine, migrated, setup.bulk_operation_id)
        pause.resume()
        result, _ = await asyncio.wait_for(task, 60)
        assert isinstance(result, Prepared)
        await asyncio.wait_for(follow_up, 30)
    assert await count_rows(engine, "batch_submissions") == 2  # the reconciled one and the new one


async def test_eight_concurrent_sessions_with_a_batch_preparation_never_exceed_the_cap(
    engine: AsyncEngine, gateway: LLMGateway, migrated: PgServer
) -> None:
    """L5709: scoring, profile extraction, a batch preparation and a cap edit, all queued on the month row at
    once, serialise on it: the counted spend never passes `max(cap, counted_at_cap_change)`."""
    setup = await bulk_with_targets(engine, 3)
    settings = await settings_with(engine, batch_min_targets=1)
    owners = [await claimed_target(engine) for _ in range(3)]
    month = month_of(utcnow())
    async with engine.begin() as conn:
        await ensure_month(conn, month, await spend_ledger.current_params(conn), utcnow())
        op_id, _ = await ops.create_operation(
            conn, kind="eval_run", payload={}, dedup_key="cap-test", defer=None
        )
        claim = await ops.claim_operation(conn, op_id, epoch=1)
    assert claim is not None
    extraction = spend_ledger.OperationOwner(claim)
    kinds = ["scoring", "nonscoring", "batch", "cap", "scoring", "nonscoring", "scoring", "nonscoring"]
    big = {"bound": 300_000, "max_tokens": 32_000}  # about $1.6 at Sonnet's prices, a cap edit's worth

    async def session(n: int, kind: str) -> object:
        try:
            if kind == "batch":
                return (await prepared_submission(engine, gateway, setup, settings=settings))[0]
            async with engine.begin() as conn:
                if kind == "cap":
                    await change_cap(
                        conn,
                        month=month,
                        cap_usd=Decimal("3.00"),
                        overrun_buffer_usd=Decimal("0.50"),
                        now=utcnow(),
                    )
                    return None
                if kind == "scoring":
                    c = owners[[0, 4, 6].index(n)]
                    owner = TargetAttemptOwner(c.target_id, uuid.UUID(c.claim_id), c.attempt_no, c.attempt_id)
                    kw = scoring_kw(owner, now=utcnow(), model="claude-sonnet-5-5", **big)
                else:
                    kw = reserve_kw(extraction, now=utcnow(), model="claude-sonnet-5-5", **big)
                return await reserve(conn, **kw)
        except BudgetExhausted as exc:
            return exc

    async with engine.connect() as blocker:  # the barrier: all eight queue on the month row
        await blocker.begin()
        await blocker.execute(_LOCK_MONTH, {"m": month})
        tasks = [asyncio.create_task(session(n, kind)) for n, kind in enumerate(kinds)]

        async def all_waiting() -> bool:
            return len(lock_wait_details(migrated)) >= len(kinds) or any(t.done() for t in tasks)

        await await_until(all_waiting, 30)
        assert not any(t.done() for t in tasks)
        await blocker.rollback()
    await asyncio.gather(*tasks)
    async with engine.connect() as conn:
        row = await spend_ledger.read_month(
            conn, month, await spend_ledger.current_params(conn), now=utcnow()
        )
        assert row is not None and budget.cap_holds(row)
        assert await spend_ledger.recompute(conn, month) is None  # the counters agree with the ledger


# ---- reserve_batch on the migrated database (§7.11; Codex r1 #22) ------------------------------------------


async def test_reserve_batch_against_the_migrated_database(engine: AsyncEngine, current_epoch: int) -> None:
    setup = await bulk_with_targets(engine, 2, budget_usd=Decimal(5))
    now = utcnow()
    async with engine.begin() as conn:
        execution = int(
            (await conn.execute(sa.text("SELECT execution_config_id FROM active_config"))).scalar_one()
        )
        policy = await _active_policy(engine)
        sub = await insert_submission(conn, setup.llm_tuple_id, setup.bulk_operation_id, request_count=2)
        items: list[BatchItemReservation] = []
        for target in setup.targets:
            await conn.execute(
                sa.text("UPDATE score_targets SET attempt_no = 1 WHERE id = :t"), {"t": target.target_id}
            )
            attempt = int(
                (
                    await conn.execute(
                        sa.text(
                            "INSERT INTO score_attempts (target_id, attempt_no, mode, outcome, "
                            "execution_config_id, evaluation_policy_id, lane) "
                            "VALUES (:t, 1, 'batch', 'running', :e, :p, 'batch') RETURNING id"
                        ),
                        {"t": target.target_id, "e": execution, "p": policy},
                    )
                ).scalar_one()
            )
            item_id = await insert_batch_item(conn, sub, target.target_id)
            items.append(
                BatchItemReservation(
                    target_id=target.target_id,
                    attempt_id=attempt,
                    batch_item_id=item_id,
                    input_tokens_bound=20_000,
                    rendered_prompt_sha256=b"\x01" * 32,
                    prefix_sha256=b"\x02" * 32,
                    input_refs={"job_revision_id": target.job_revision_id},
                )
            )
        reservations = await reserve_batch(
            conn,
            owner=BatchSubmissionOwner(sub, "preparing"),
            items=items,
            model="claude-sonnet-5-5",
            effort="medium",
            max_tokens=6000,
            execution_config_id=execution,
            prompt_template_version="generate@2",
            bulk_operation_id=setup.bulk_operation_id,
            hold_usd=Decimal("0.02"),
            now=now,
        )
    worst = worst_case_usd("claude-sonnet-5-5", input_tokens_bound=20_000, max_tokens=6000, batch=True)
    assert [r.reserved_usd for r in reservations] == [worst, worst] and all(r.batch for r in reservations)
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                sa.text(
                    "SELECT c.transport, c.transport_try, c.logical_call_no, c.round, c.attempt_id, "
                    "c.target_id, c.batch_item_id, c.max_tokens_stage, c.status, c.input_refs, l.batch, "
                    "l.bulk_operation_id, "
                    "l.state, l.admitted_buffer_usd, l.purpose_class FROM llm_calls c "
                    "JOIN llm_spend_ledger l ON l.id = c.spend_ledger_id ORDER BY c.id"
                )
            )
        ).all()
    for row, item in zip(rows, items, strict=True):
        assert (row.transport, row.transport_try, row.logical_call_no, row.round) == ("batch", 1, None, 0)
        assert (row.attempt_id, row.target_id, row.batch_item_id) == (
            item.attempt_id,
            item.target_id,
            item.batch_item_id,
        )
        assert (row.max_tokens_stage, row.status, row.state) == ("first", "reserved", "reserved")
        assert (row.batch, row.bulk_operation_id, row.purpose_class) == (
            True,
            setup.bulk_operation_id,
            "scoring",
        )
        assert row.admitted_buffer_usd == Decimal("2.00")
        assert row.input_refs["owner"] == {"kind": "batch_submission", "id": sub, "state": "preparing"}
    assert (await bulk_row(engine, setup.bulk_operation_id)).spent_usd == 2 * worst
    # under the same locks, an allocation that cannot take another two items is refused before any write
    with pytest.raises(BulkAllocationExceeded):
        async with engine.begin() as conn:
            await conn.execute(
                sa.text("UPDATE bulk_operations SET budget_usd = :u WHERE id = :b"),
                {"u": 3 * worst, "b": setup.bulk_operation_id},
            )
            await reserve_batch(
                conn,
                owner=BatchSubmissionOwner(sub, "preparing"),
                items=items,
                model="claude-sonnet-5-5",
                effort="medium",
                max_tokens=6000,
                execution_config_id=execution,
                prompt_template_version="generate@2",
                bulk_operation_id=setup.bulk_operation_id,
                hold_usd=Decimal(0),
                now=now,
            )
    assert await count_rows(engine, "llm_spend_ledger") == 2


class Captured(Exception):
    """The request a capturing caller received, raised to stop the step there."""

    def __init__(self, prompt: RenderedPrompt) -> None:
        super().__init__("captured")
        self.prompt = prompt


async def capture(role: str, prompt: RenderedPrompt, **_: object) -> Any:
    raise Captured(prompt)


async def test_prepared_items_carry_the_online_generators_params(
    engine: AsyncEngine, gateway: LLMGateway
) -> None:
    """L3932: each item's params are `batch_request_body` of the generator request the online attempt would
    send for its target (`load_inputs`, `generate_step`'s prompt), at the pinned first stage, and its call
    the rendered prompt's `template_version`."""
    setup = await bulk_with_targets(engine, 2)
    result, _ = await prepared_submission(
        engine, gateway, setup, settings=await settings_with(engine, batch_min_targets=1)
    )
    assert isinstance(result, Prepared)
    by_target = dict(zip(item_targets(result), result.requests, strict=True))
    async with engine.connect() as conn:
        for target in setup.targets:
            attempt, version = (
                await conn.execute(
                    sa.text(
                        "SELECT a.id, u.prompt_schema_version_id FROM score_attempts a "
                        "JOIN score_targets t ON t.id = a.target_id "
                        "JOIN llm_tuples u ON u.id = t.llm_tuple_id "
                        "WHERE a.target_id = :t AND a.mode = 'batch'"
                    ),
                    {"t": target.target_id},
                )
            ).one()
            state = cast(
                TargetState,
                {"target_id": target.target_id, "attempt_id": attempt, "prompt_schema_version_id": version},
            )
            inputs, row = await score_nodes.load_inputs(conn, state)
            with pytest.raises(Captured) as captured:  # the request the online generate step would send
                await steps.generate_step(inputs, cast(Any, None), cast(Any, capture))
            prompts = [captured.value.prompt]
            node = await pinned_node_config(
                conn,
                "generate",
                model_policy_id=row.model_policy_id,
                execution_config_id=row.execution_config_id,
                schema=score_schema("generate", inputs.version),
            )
            expected = gateway.batch_request_body(node, prompts[0], max_tokens=node.max_tokens)
            assert dict(by_target[target.target_id].params) == expected
            assert by_target[target.target_id].input_refs == prompts[0].input_refs
            template = (await conn.execute(_ITEM_TEMPLATE, {"t": target.target_id})).scalar_one()
            assert template == prompts[0].template_version  # the rendered prompt's own (review Minor 5)


# ---- the recordings against node 9's cancel (Ruling T16-c) -----------------------------------------------


@pytest.mark.parametrize("recording", ["submit_start", "record_rejected", "abandon"])
async def test_a_recording_waits_for_a_node_9_cancel_at_the_target_row(
    engine: AsyncEngine, gateway: LLMGateway, migrated: PgServer, recording: str
) -> None:
    """Ruling T16-c: node 9's cancel (`targets.cancel_targets`, `_cancel`) locks a target row, then ends its
    attempt. The recordings that move a submission's targets and attempts (the dispatch marker's counters of
    `submit_start`, `record_rejected`, `abandon`) take the target rows before the attempt rows too: with the
    cancel held mid-transaction, the recording waits at that target row having written no attempt, so no
    lock cycle can form; both commit, and the cancelled target stays cancelled."""
    setup = await bulk_with_targets(engine, 3)
    result, claim = await prepared_submission(
        engine, gateway, setup, settings=await settings_with(engine, batch_min_targets=1)
    )
    assert isinstance(result, Prepared)
    s = result.submission_id
    if recording == "record_rejected":
        async with engine.begin() as conn:
            assert await batches_mod.submit_start(conn, s, operation=claim, now=utcnow()) == "submit_started"
    cancelled = setup.targets[1]

    async def record() -> object:
        async with engine.begin() as conn:
            if recording == "submit_start":
                return await batches_mod.submit_start(conn, s, operation=claim, now=utcnow())
            if recording == "record_rejected":
                rejected = Rejected(400, "invalid_request_error", "test: refused")
                await batches_mod.record_rejected(conn, s, rejected, operation=claim, now=utcnow())
                return None
            return await batches_mod.abandon(conn, s, now=utcnow())

    async with engine.connect() as holder:  # node 9's cancel of one of the batch's targets, uncommitted
        await holder.begin()
        done = await targets_mod.cancel_targets(
            holder,
            llm_tuple_id=setup.llm_tuple_id,
            revision_ids=[cancelled.job_revision_id],
            reason="revision_superseded",
            now=utcnow(),
        )
        assert (done.targets, done.attempts) == (1, 1)
        task = asyncio.create_task(record())

        async def waiting() -> bool:
            return bool(lock_wait_details(migrated)) or task.done()

        await await_until(waiting, 20, 0.02)
        assert not task.done()
        (wait,) = lock_wait_details(migrated)
        assert wait.relation == "score_targets", wait
        assert not inserted_into(migrated, wait.pid, "score_attempts")  # no attempt row written yet
        await holder.commit()
    await asyncio.wait_for(task, 30)
    expected = {
        "submit_start": "submit_started",
        "record_rejected": "submit_rejected",
        "abandon": "abandoned",
    }
    assert (await submission_rows(engine))[0].state == expected[recording]
    assert (await target_of(engine, cancelled.target_id)).state == "cancelled"
    others = {"submit_start": "in_batch", "record_rejected": "pending", "abandon": "pending"}
    assert await count_rows(engine, "score_targets", state=others[recording]) == 2


# ---- abandon, a reclaimed execution, the recovery (Codex r1 #23, r2 B11, B12, r3 B2, B3, B20) --------------


async def test_abandon_credits_the_bulk_debit(engine: AsyncEngine, gateway: LLMGateway) -> None:
    setup = await bulk_with_targets(engine, 3)
    before = await bulk_row(engine, setup.bulk_operation_id)
    result, _ = await prepared_submission(
        engine, gateway, setup, settings=await settings_with(engine, batch_min_targets=1)
    )
    assert isinstance(result, Prepared)
    during = await bulk_row(engine, setup.bulk_operation_id)
    assert during.spent_usd == before.spent_usd + result.reserved_usd > before.spent_usd
    assert during.followup_held_usd == 3 * result.hold_usd > 0
    async with engine.begin() as conn:
        assert await batches_mod.abandon(conn, result.submission_id, now=utcnow(), defer=None)
    after = await bulk_row(engine, setup.bulk_operation_id)
    assert (after.spent_usd, after.followup_held_usd) == (before.spent_usd, before.followup_held_usd)
    assert await batch_ledger(engine) == [("not_billed", "never_dispatched")] * 3
    assert await count_rows(engine, "score_targets", state="pending") == 3
    (sub,) = await submission_rows(engine)
    assert sub.state == "abandoned" and sub.follow_on_at is not None
    async with engine.begin() as conn:  # a second abandon finds nothing preparing
        assert not await batches_mod.abandon(conn, result.submission_id, now=utcnow(), defer=None)


def claimant(lock: LockSession, pid: int) -> WorkerIdentity:
    assert lock.backend_pid is not None
    return WorkerIdentity(uuid.uuid4(), "h", pid, lock.backend_pid, "v")


async def _reclaim(engine: AsyncEngine, operation_id: int) -> None:
    async with engine.begin() as conn:
        reclaimed = await ops._reclaim_stale(conn, now=utcnow() + ops.OPERATION_STALE + timedelta(seconds=1))
    assert operation_id in [op for op, _ in reclaimed]


async def test_a_reclaimed_execution_cannot_prepare_or_send(
    engine: AsyncEngine, op_runner: OpRunner, fake_llm: FakeLLM, queue_app: procrastinate.App
) -> None:
    """Codex r2 B12: an old execution paused inside `prepare` is `FencedOut` at the owner lock once its
    operation was reclaimed; a `submit_started` row of an old claim is recovered by the new execution (its
    claim differs from the stored one) and the old one, resumed, sends nothing."""
    from jobhunter.llm.fake import BatchScript

    fake_llm.batches(BatchScript(create="ok"))
    setup = await bulk_with_targets(engine, 200)
    assert setup.operation_id is not None
    old = PausePoint(batches_mod.BEFORE_OWNER_LOCK)
    running = asyncio.create_task(op_runner.run_one(setup.operation_id, pause=old))
    await asyncio.wait_for(old.reached.wait(), 60)
    await _reclaim(engine, setup.operation_id)
    old.resume()
    await asyncio.wait_for(running, 60)  # FencedOut at lock_operation_owner: lost, nothing written
    assert await batch_rows(engine) == (0, 0, 0, 0)

    old = PausePoint(batches_mod.AFTER_SUBMIT_START)  # the next execution, paused after its submit-start
    running = asyncio.create_task(op_runner.run_one(setup.operation_id, pause=old))
    await asyncio.wait_for(old.reached.wait(), 120)
    (sub,) = await submission_rows(engine)
    assert sub.state == "submit_started"
    await _reclaim(engine, setup.operation_id)
    await op_runner.run_one(setup.operation_id, pause=PausePoint("no.such.point"))  # the new execution
    (sub,) = await submission_rows(engine)
    assert (sub.state, sub.reconcile_report) == ("submit_ambiguous", {"error_class": "execution_lost"})
    (op,) = await operations_of(engine, "batch_submit")
    assert (op.status, op.progress) == ("succeeded", {"skipped": "ambiguous"})
    old.resume()
    await asyncio.wait_for(running, 60)
    assert fake_llm.state.batches == {}  # the old execution never sent
    assert (await submission_rows(engine))[0].state == "submit_ambiguous"


@pytest.mark.parametrize("order", ["before_operations", "after_operations"])
async def test_recovery_is_independent_of_the_sweep_order(
    engine: AsyncEngine,
    op_runner: OpRunner,
    fake_llm: FakeLLM,
    worker_lock: LockSession,
    queue_app: procrastinate.App,
    monkeypatch: pytest.MonkeyPatch,
    order: str,
) -> None:
    """Codex r3 B3: a `submit_started` row of a dead epoch's operation, its attempts not exhausted, is
    `submit_ambiguous` when the takeover commits whether the `batch_submissions` sweep runs before or after
    the `operations` sweep (the epoch predicate)."""
    from jobhunter.llm.fake import BatchScript

    fake_llm.batches(BatchScript(create="ok"))
    setup = await bulk_with_targets(engine, 200)
    assert setup.operation_id is not None
    sweeps = dict(epoch_module._SWEEPS)
    batch_sweep = sweeps.pop("batch_submissions")
    if order == "before_operations":
        sweeps = {"batch_submissions": batch_sweep, **sweeps}
    else:
        sweeps = {**sweeps, "batch_submissions": batch_sweep}
    monkeypatch.setattr(epoch_module, "_SWEEPS", sweeps)
    pause = PausePoint(batches_mod.AFTER_SUBMIT_START)
    running = asyncio.create_task(op_runner.run_one(setup.operation_id, pause=pause))
    await asyncio.wait_for(pause.reached.wait(), 120)
    epoch, counts = await claim_epoch_and_sweep(engine, claimant(worker_lock, 2))
    assert (epoch, counts["batch_submissions"], counts["operations"]) == (2, 1, 1)
    (sub,) = await submission_rows(engine)
    assert (sub.state, sub.reconcile_report) == ("submit_ambiguous", {"error_class": "execution_lost"})
    pause.resume()
    await asyncio.wait_for(running, 60)
    assert fake_llm.state.batches == {}


async def test_the_tick_reconciler_recovers_a_row_a_failed_operation_left(
    engine: AsyncEngine, op_runner: OpRunner, fake_llm: FakeLLM, queue_app: procrastinate.App
) -> None:
    """Codex r3 B2: a failure hook cannot take `active_config` after the operation row, so the
    `batch_submissions` tick reconciler recovers what an operation that ended `failed` left behind."""
    from jobhunter.llm.fake import BatchScript

    fake_llm.batches(BatchScript(create="ok"))
    setup = await bulk_with_targets(engine, 200)
    assert setup.operation_id is not None
    pause = PausePoint(batches_mod.AFTER_PREPARE_COMMIT)
    running = asyncio.create_task(op_runner.run_one(setup.operation_id, pause=pause))
    await asyncio.wait_for(pause.reached.wait(), 120)
    assert await run_reconciler("batch_submissions", engine, now=utcnow(), epoch=1) == 0  # its owner lives
    async with engine.begin() as conn:  # the operation ends failed under its claim
        claim_id = (
            await conn.execute(
                sa.text("SELECT claim_id FROM operations WHERE id = :i"), {"i": setup.operation_id}
            )
        ).scalar_one()
        await transition(
            conn,
            "operations",
            setup.operation_id,
            ["running"],
            "failed",
            reason="test",
            owner={"claim_id": claim_id},
        )
    assert await run_reconciler("batch_submissions", engine, now=utcnow(), epoch=1) == 1
    (sub,) = await submission_rows(engine)
    assert sub.state == "abandoned"
    assert await batch_ledger(engine) == [("not_billed", "never_dispatched")] * 200
    pause.resume()
    await asyncio.wait_for(running, 60)  # its submit-start finds no owner: lost, nothing more written
    assert (await submission_rows(engine))[0].state == "abandoned" and fake_llm.state.batches == {}


async def test_a_lost_batch_job_is_re_deferred_with_its_lock(
    engine: AsyncEngine, current_epoch: int, queue_app: procrastinate.App
) -> None:
    """Codex r3 B20: the `operations` reconciler re-defers a lost `batch_submit` job under the `batch` lock
    (`OPERATION_LOCKS`), so two re-deferred batch operations never run at once."""
    setup = await bulk_with_targets(engine, 0, operation=False)
    async with engine.begin() as conn:
        first, _ = await ops.create_operation(
            conn,
            kind="batch_submit",
            payload={"bulk_operation_id": setup.bulk_operation_id, "llm_tuple_id": setup.llm_tuple_id},
            dedup_key="batch_submit:x:1",
            defer=None,
        )
        second, _ = await ops.create_operation(
            conn,
            kind="batch_submit",
            payload={"bulk_operation_id": setup.bulk_operation_id, "llm_tuple_id": setup.llm_tuple_id},
            dedup_key="batch_submit:x:after:1",
            defer=None,
        )
    assert (
        await run_reconciler("operations", engine, now=utcnow() + timedelta(minutes=5), epoch=current_epoch)
        == 2
    )
    async with engine.begin() as conn:
        jobs = (
            (
                await conn.execute(
                    sa.text("SELECT lock FROM procrastinate_jobs WHERE task_name = :task ORDER BY id"),
                    {"task": job_task_name("run_operation")},
                )
            )
            .scalars()
            .all()
        )
        assert list(jobs) == ["batch", "batch"]
        worker = (
            await conn.execute(sa.text("SELECT worker_id FROM procrastinate_register_worker_v1()"))
        ).scalar_one()
        fetch = sa.text("SELECT id FROM procrastinate_fetch_job_v2(NULL, :w)")
        taken = (await conn.execute(fetch, {"w": worker})).scalar_one_or_none()
        assert taken is not None  # the first one runs
        assert (await conn.execute(fetch, {"w": worker})).scalar_one_or_none() is None  # the second waits
    del first, second
