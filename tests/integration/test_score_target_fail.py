"""The `fail` node and the follow-up holds on PostgreSQL (spec §7.16.3, §6.9.3-6.9.4, §9.4.1; plan Task 10, A2
T12, T15, Codex r1 #27, Rulings P8, P31): every row of the outcome table, the backoff sequence, the fourth
charged attempt, the cap reached with the quarantined batch spend, and the once-only hold releases of
`llm.bulk`.

`fail` is called as the subgraph calls it, with the state the routing leaves (its `error_code`), on a claimed
target; a refusal also goes through the whole subgraph, whose category `fail` reads from the call.

Every datum is invented.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from jobhunter.graphs.score.deps import ScoreDeps
from jobhunter.graphs.score.nodes import fail as fail_mod
from jobhunter.graphs.score.state import TargetState, initial_state
from jobhunter.llm import bulk, fake
from jobhunter.llm.budget import month_of
from jobhunter.llm.prompts.render import prompt_schema_v2
from jobhunter.llm.spend_ledger import CAP_REACHED_ALERT, current_params, ensure_month
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.llm import FakeLLM, open_alerts
from tests.support.m3 import NOW, insert_batch_item, insert_bulk_operation, insert_submission
from tests.support.scoring import (
    ClaimedTarget,
    activate_test_policy,
    attempt_end,
    claim_again,
    claimed_target,
    ended_with,
    invoke_subgraph,
    score_config,
    score_deps,
    score_llm_tuple,
    synthetic_c_draft,
    target_of,
)
from tests.support.settings import generation

pytestmark = pytest.mark.integration
FIXED = datetime(2026, 10, 27, 9, 30, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)
HOLD = Decimal("0.150000")


@pytest.fixture
async def deps(engine: AsyncEngine, fake_llm: FakeLLM, clean_db: None) -> ScoreDeps:
    made = await score_deps(engine, fake_llm.gateway(engine))
    return ScoreDeps(made.engine, made.gateway, lambda: FIXED, made.monotonic, made.saver)


def at_fail(claimed: ClaimedTarget, error_code: str) -> TargetState:
    state = initial_state(claimed.ref, claimed.attempt_id)
    state["error_code"] = error_code
    return state


async def run_fail(deps: ScoreDeps, claimed: ClaimedTarget, error_code: str) -> Any:
    node = fail_mod.make_fail(deps)
    return await node(at_fail(claimed, error_code), await score_config(deps, claimed))


async def set_target(engine: AsyncEngine, target_id: int, **columns: object) -> None:
    sets = ", ".join(f"{name} = :{name}" for name in columns)
    async with engine.begin() as conn:
        await conn.execute(
            sa.text(f"UPDATE score_targets SET {sets} WHERE id = :t"),  # noqa: S608 (test column names)
            {**columns, "t": target_id},
        )


# ---- the outcome table ---------------------------------------------------------------------------------

TERMINAL = [
    # error_code, the target's state and state_reason, the attempt's outcome and outcome_detail
    ("max_tokens", "failed_max_tokens", "max_tokens", "failed_max_tokens", "max_tokens"),
    ("invalid_output", "failed_invalid_output", "invalid_output", "failed_invalid_output", "invalid_output"),
    (
        "unexpected_stop_reason",
        "failed_invalid_output",
        "unexpected_stop_reason",
        "failed_invalid_output",
        "unexpected_stop_reason",
    ),
    ("llm_request_400", "failed_request", "400", "failed_request", "400"),
    ("llm_request_413", "failed_request", "413", "failed_request", "413"),
    ("model_mismatch", "failed_request", "model_mismatch", "failed_request", "model_mismatch"),
    ("input_unbounded", "failed_request", "input_unbounded", "failed_request", "input_unbounded"),
    ("refused", "refused", "unspecified", "refused", "unspecified"),
]


@pytest.mark.parametrize(("code", "state", "reason", "outcome", "detail"), TERMINAL)
async def test_a_terminal_error_moves_the_target_and_ends_the_attempt(
    engine: AsyncEngine, deps: ScoreDeps, code: str, state: str, reason: str, outcome: str, detail: str
) -> None:
    claimed = await claimed_target(engine)
    before = await generation(engine)
    command = await run_fail(deps, claimed, code)
    assert ended_with(command) == outcome
    target = await target_of(engine, claimed.target_id)
    assert (target.state, target.state_reason) == (state, reason)
    assert (target.claim_id, target.claim_epoch, target.claim_heartbeat_at) == (None, None, None)
    assert target.claimed_at is not None  # kept as history
    assert target.refusal_category == ("unspecified" if state == "refused" else None)
    ended = await attempt_end(engine, claimed.attempt_id)
    assert (ended.outcome, ended.outcome_detail) == (outcome, detail) and ended.ended_at is not None
    assert await generation(engine) == before + 1  # the ranking bump, last


@pytest.mark.parametrize("code", ["llm_transient", "llm_not_configured", "prompt_version_unscorable"])
async def test_a_transient_error_defers_the_target_with_its_backoff(
    engine: AsyncEngine, deps: ScoreDeps, code: str
) -> None:
    claimed = await claimed_target(engine)
    command = await run_fail(deps, claimed, code)
    assert ended_with(command) == "transient_error"
    target = await target_of(engine, claimed.target_id)
    assert (target.state, target.not_before, target.transient_failures) == ("pending", FIXED + 15 * MINUTE, 1)
    assert target.claim_id is None and target.claimed_at is not None
    ended = await attempt_end(engine, claimed.attempt_id)
    assert (ended.outcome, ended.outcome_detail) == ("transient_error", code)


async def test_a_denied_reservation_defers_the_target_for_the_budget(
    engine: AsyncEngine, deps: ScoreDeps
) -> None:
    claimed = await claimed_target(engine)
    await month_ready(engine)
    command = await run_fail(deps, claimed, "budget_exhausted")
    assert ended_with(command) == "budget_exhausted"
    target = await target_of(engine, claimed.target_id)
    assert (target.state, target.state_reason, target.claim_id) == (
        "budget_deferred",
        "budget_exhausted",
        None,
    )
    ended = await attempt_end(engine, claimed.attempt_id)
    assert (ended.outcome, ended.outcome_detail) == ("budget_exhausted", "budget_exhausted")


async def test_a_lost_claim_writes_nothing_and_logs_it(
    engine: AsyncEngine, deps: ScoreDeps, log_events: Any
) -> None:
    claimed = await claimed_target(engine)
    before = await generation(engine)
    command = await run_fail(deps, claimed, "claim_lost")
    assert ended_with(command) == "claim_lost"
    target = await target_of(engine, claimed.target_id)
    assert (target.state, str(target.claim_id)) == ("in_progress", claimed.claim_id)
    assert (await attempt_end(engine, claimed.attempt_id)).outcome == "running"  # its ender writes it
    assert await generation(engine) == before
    assert [e["target_id"] for e in log_events() if e["event"] == "score_claim_lost"] == [claimed.target_id]


async def test_a_claim_lost_meanwhile_leaves_the_target_to_its_new_owner(
    engine: AsyncEngine, deps: ScoreDeps
) -> None:
    claimed = await claimed_target(engine)
    stale = ClaimedTarget(**{**claimed.__dict__, "claim_id": str(uuid4())})
    command = await run_fail(deps, stale, "max_tokens")  # the attempt's predicate no longer holds
    assert ended_with(command) == "claim_lost"
    assert (await target_of(engine, claimed.target_id)).state == "in_progress"
    assert (await attempt_end(engine, claimed.attempt_id)).outcome == "running"


async def test_a_refusal_through_the_subgraph_keeps_the_call_s_category(
    engine: AsyncEngine, deps: ScoreDeps, fake_llm: FakeLLM
) -> None:
    _, schemas = prompt_schema_v2()
    generator = schemas["generator"]
    assert isinstance(generator, dict)
    fake_llm.record("generate", "claude-sonnet-5-5", synthetic_c_draft(), schema=generator)
    claimed = await claimed_target(engine)
    fake.answer(fake.answer_key("generate", {"job_revision_id": claimed.job_revision_id}, 0), "refusal")
    final = await invoke_subgraph(deps, claimed)
    assert (final["error_code"], final["outcome"]) == ("refused", "refused")
    target = await target_of(engine, claimed.target_id)
    category = fake.REFUSAL_CATEGORY
    assert (target.state, target.state_reason, target.refusal_category) == ("refused", category, category)
    ended = await attempt_end(engine, claimed.attempt_id)
    assert (ended.outcome, ended.outcome_detail) == ("refused", category)


# ---- the backoff and the charged attempts --------------------------------------------------------------


async def test_the_backoff_doubles_from_15_minutes_to_its_6_hour_ceiling(
    engine: AsyncEngine, deps: ScoreDeps
) -> None:
    claimed = await claimed_target(engine)
    waits = []
    for failures in range(7):
        await run_fail(deps, claimed, "llm_transient")
        target = await target_of(engine, claimed.target_id)
        assert (target.state, target.transient_failures) == ("pending", failures + 1)
        waits.append(target.not_before - FIXED)
        await set_target(engine, claimed.target_id, not_before=None)  # due again
        claimed = await claim_again(engine, claimed.target_id)
    assert waits == [15 * MINUTE, 30 * MINUTE, HOUR, 2 * HOUR, 4 * HOUR, 6 * HOUR, 6 * HOUR]


@pytest.mark.parametrize(("charged", "state"), [(3, "pending"), (4, "failed_transient")])
async def test_a_transient_error_of_the_fourth_charged_attempt_fails_the_target(
    engine: AsyncEngine, deps: ScoreDeps, charged: int, state: str
) -> None:
    claimed = await claimed_target(engine)
    await set_target(engine, claimed.target_id, charged_attempts=charged)
    await run_fail(deps, claimed, "llm_transient")
    target = await target_of(engine, claimed.target_id)
    assert target.state == state
    if state == "failed_transient":
        assert (target.state_reason, target.not_before, target.transient_failures) == (
            "llm_transient",
            None,
            0,
        )
    assert (await attempt_end(engine, claimed.attempt_id)).outcome == "transient_error"


def test_the_backoff_formula() -> None:
    assert [fail_mod.backoff(n) for n in (0, 1, 2, 3, 4, 5, 6, 40)] == [
        15 * MINUTE,
        30 * MINUTE,
        HOUR,
        2 * HOUR,
        4 * HOUR,
        6 * HOUR,
        6 * HOUR,
        6 * HOUR,
    ]


# ---- the cap reached --------------------------------------------------------------------------------------


async def month_ready(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await ensure_month(conn, month_of(FIXED), await current_params(conn), FIXED)


async def test_the_cap_reached_alert_is_raised_once_with_the_quarantined_batch_spend(
    engine: AsyncEngine, deps: ScoreDeps
) -> None:
    first = await claimed_target(engine)
    second = await claimed_target(engine)
    async with engine.begin() as conn:
        tuple_id = await score_llm_tuple(conn)
        operation = await insert_bulk_operation(conn, tuple_id, state="cancelled")
        for state, reserved in (("submit_ambiguous", "1.25"), ("submit_rejected", "9"), ("abandoned", "4")):
            await insert_submission(
                conn, tuple_id, operation, state=state, reserved_usd=Decimal(reserved), submit_started_at=NOW
            )
        quarantined = (await conn.execute(sa.text(bulk.QUARANTINED_USD_SQL))).scalar_one()
    assert quarantined == Decimal("1.25")  # the submit_ambiguous submission only (S:L1199, Ruling P31)
    await month_ready(engine)
    await run_fail(deps, first, "budget_exhausted")
    await run_fail(deps, second, "budget_exhausted")
    alerts = await open_alerts(engine, CAP_REACHED_ALERT)
    assert alerts == [(f"{CAP_REACHED_ALERT}:{month_of(FIXED).isoformat()}", "critical", 1)]  # once a month
    async with engine.connect() as conn:
        context = (
            await conn.execute(
                sa.text("SELECT context FROM alerts WHERE code = :c"), {"c": CAP_REACHED_ALERT}
            )
        ).scalar_one()
        marked = (
            await conn.execute(
                sa.text("SELECT cap_reached_alerted_at FROM llm_budget WHERE month = :m"),
                {"m": month_of(FIXED)},
            )
        ).scalar_one()
    assert context == {"quarantined_batch_usd": "1.250000"} and marked == FIXED
    for claimed in (first, second):
        assert (await target_of(engine, claimed.target_id)).state == "budget_deferred"


# ---- the follow-up holds (Ruling P8) ----------------------------------------------------------------------


async def followup_item(
    engine: AsyncEngine, *, held: Decimal, item_hold: Decimal = HOLD
) -> tuple[int, ClaimedTarget, int]:
    """A claimed target of an active bulk operation holding `held`, with a processed follow-up `batch_items`
    row holding `item_hold`: `(bulk_operation_id, claimed, item_id)`."""
    async with engine.begin() as conn:
        await activate_test_policy(conn)
        tuple_id = await score_llm_tuple(conn)
        operation = await insert_bulk_operation(conn, tuple_id)
        await conn.execute(
            sa.text("UPDATE bulk_operations SET followup_held_usd = :h WHERE id = :b"),
            {"h": held, "b": operation},
        )
        submission = await insert_submission(
            conn,
            tuple_id,
            operation,
            state="reconciled",
            submit_started_at=NOW,
            provider_batch_id=f"msgbatch_{uuid4().hex[:12]}",
            results_complete_at=NOW,
            reconciled_at=NOW,
        )
    claimed = await claimed_target(engine, bulk_operation_id=operation)
    async with engine.begin() as conn:
        item = await insert_batch_item(
            conn,
            submission,
            claimed.target_id,
            attempt_no=claimed.attempt_no + 1,  # the item's own number: the custom id is unique per attempt
            result_type="succeeded",
            processed_at=NOW,
            decision="followup",
            hold_usd=item_hold,
        )
    return operation, claimed, item


async def held_of(engine: AsyncEngine, operation: int) -> Decimal:
    async with engine.connect() as conn:
        found = await conn.execute(
            sa.text("SELECT followup_held_usd FROM bulk_operations WHERE id = :b"), {"b": operation}
        )
        return Decimal(found.scalar_one())


async def released_at(engine: AsyncEngine, item: int) -> datetime | None:
    async with engine.connect() as conn:
        found = await conn.execute(
            sa.text("SELECT hold_released_at FROM batch_items WHERE id = :i"), {"i": item}
        )
        value: datetime | None = found.scalar_one()
        return value


async def test_an_item_hold_is_released_once(engine: AsyncEngine, clean_db: None) -> None:
    operation, _, item = await followup_item(engine, held=Decimal("0.400000"))
    async with engine.begin() as conn:
        assert await bulk.release_item_hold(conn, item_id=item) == HOLD
    stamped = await released_at(engine, item)
    assert stamped is not None and await held_of(engine, operation) == Decimal("0.250000")
    async with engine.begin() as conn:
        assert await bulk.release_item_hold(conn, item_id=item) == Decimal(0)  # once only
    assert await released_at(engine, item) == stamped and await held_of(engine, operation) == Decimal(
        "0.250000"
    )


async def test_a_target_hold_is_released_once_and_never_below_zero(
    engine: AsyncEngine, clean_db: None
) -> None:
    operation, claimed, item = await followup_item(engine, held=Decimal("0.100000"))  # less than the item's
    async with engine.begin() as conn:
        assert await bulk.release_target_hold(conn, claimed.target_id) == HOLD
    assert await held_of(engine, operation) == Decimal(0)  # greatest(0, ...)
    async with engine.begin() as conn:
        assert await bulk.release_target_hold(conn, claimed.target_id) == Decimal(0)
    assert await released_at(engine, item) is not None and await held_of(engine, operation) == Decimal(0)


async def test_a_target_without_a_followup_item_releases_nothing(engine: AsyncEngine, clean_db: None) -> None:
    claimed = await claimed_target(engine)
    async with engine.begin() as conn:
        assert await bulk.release_target_hold(conn, claimed.target_id) == Decimal(0)
        await bulk.lock_for_targets(conn, [claimed.target_id])  # no bulk row to lock: a no-op


@pytest.mark.parametrize(
    ("code", "released"),
    [("max_tokens", True), ("refused", True), ("llm_transient", False), ("budget_exhausted", False)],
)
async def test_only_a_terminal_failure_releases_the_follow_up_hold(
    engine: AsyncEngine, deps: ScoreDeps, code: str, released: bool
) -> None:
    operation, claimed, item = await followup_item(engine, held=Decimal("0.300000"))
    await month_ready(engine)
    await run_fail(deps, claimed, code)
    assert (await released_at(engine, item) is not None) is released
    assert await held_of(engine, operation) == (Decimal("0.150000") if released else Decimal("0.300000"))
