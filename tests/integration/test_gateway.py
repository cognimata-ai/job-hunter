"""The gateway call loop on the recorded-SSE fake (spec §10.2, §10.4, §10.5, §10.7, G9-G11; plan Task 5).

Each case asserts the ledger rows, the `llm_calls` rows and the per-try counter
`jobhunter_llm_calls_total{model,effort,purpose,outcome}`, and that the month's counters equal the ledger.
"""

import asyncio
import contextlib
import dataclasses
import time
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any, cast

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.errors import FencedOut
from jobhunter.llm import gateway as gateway_module
from jobhunter.llm import spend_ledger
from jobhunter.llm.bounds import PROMPT_OVERHEAD_TOKENS, native_pdf_bound, text_input_bound
from jobhunter.llm.cache_monitor import CacheMonitor, write_alerts
from jobhunter.llm.errors import (
    BudgetExhausted,
    DeadlineExhausted,
    InputUnbounded,
    InvalidOutput,
    LLMRequestError,
    LLMTransient,
    MaxTokensExhausted,
    ModelMismatch,
    Refused,
)
from jobhunter.llm.fake import REFUSAL_CATEGORY
from jobhunter.llm.gateway import GatewayResult
from jobhunter.llm.pricing import CONTEXT_WINDOW, Usage, settled_usd, worst_case_usd
from jobhunter.llm.spend_ledger import OperationOwner
from jobhunter.observability.metrics import REGISTRY
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.clock import FakeClock
from tests.support.faults import PausePoint
from tests.support.llm import (
    GATEWAY_BODY,
    GATEWAY_NODE,
    FakeLLM,
    GatewayOut,
    Try,
    active_execution_config,
    call_context,
    gateway_prompt,
    open_alerts,
    reclaim,
    save_budget,
    tries,
)

MODEL = GATEWAY_NODE.model
# `synthetic_sse`'s usage: 120 input tokens, no cache, 60 output tokens
SYNTHETIC_USAGE = Usage(120, 60, 0, 0)
_SPEND_STATES = ("counted", "billed", "ambiguous", "reserved")


def calls_total(outcome: str, *, purpose: str = "extract_profile", model: str = MODEL) -> float:
    labels = {"model": model, "effort": "medium", "purpose": purpose, "outcome": outcome}
    return REGISTRY.get_sample_value("jobhunter_llm_calls_total", labels) or 0.0


class Counted:
    """The per-try counter's increments since construction, by outcome."""

    def __init__(self, *outcomes: str, purpose: str = "extract_profile") -> None:
        self.purpose = purpose
        self.before = {o: calls_total(o, purpose=purpose) for o in outcomes}

    def delta(self) -> dict[str, float]:
        return {o: calls_total(o, purpose=self.purpose) - n for o, n in self.before.items()}


def spend_gauges() -> dict[str, float | None]:
    return {s: REGISTRY.get_sample_value("jobhunter_llm_spend_usd", {"state": s}) for s in _SPEND_STATES}


async def no_drift(engine: AsyncEngine, rows: list[Try]) -> bool:
    async with engine.connect() as conn:
        return all([await spend_ledger.recompute(conn, m) is None for m in {r.budget_month for r in rows}])


def states(rows: list[Try]) -> list[str]:
    return [r.state if r.evidence is None else f"{r.state}:{r.evidence}" for r in rows]


@pytest.fixture
async def owner(engine, current_epoch, op_owner: OperationOwner) -> OperationOwner:
    return op_owner


@pytest.fixture
async def ec(engine) -> int:
    return await active_execution_config(engine)


async def test_ok_is_one_billed_try_with_every_response_field(engine, fake_llm: FakeLLM, owner, ec) -> None:
    counted = Counted("ok")
    result = await fake_llm.gateway(engine).call(GATEWAY_NODE, gateway_prompt(), call_context(owner, ec))
    assert isinstance(result, GatewayResult) and result.parsed == GatewayOut.model_validate(GATEWAY_BODY)
    (row,) = await tries(engine)
    assert states([row]) == ["billed"] and row.settled_usd == settled_usd(MODEL, SYNTHETIC_USAGE)
    assert (row.status, row.error_code, row.http_status, row.stop_reason) == (
        "completed",
        None,
        200,
        "end_turn",
    )
    assert (row.model_served, row.provider_request_id) == (MODEL, "req_fake_0001")
    assert (row.input_tokens, row.cache_read_input_tokens, row.output_tokens) == (120, 0, 60)
    assert row.output_json == GATEWAY_BODY and row.first_event_at is not None
    assert (row.max_tokens, row.max_tokens_stage, row.transport_try, row.logical_call_no) == (
        1000,
        "first",
        1,
        1,
    )
    bound = text_input_bound(MODEL, gateway_prompt().as_request(GATEWAY_NODE.schema))
    assert row.input_tokens_bound == bound
    assert row.reserved_usd == worst_case_usd(MODEL, input_tokens_bound=bound, max_tokens=1000)
    assert (result.llm_call_id, result.usage, result.model_served) == (row.call_id, SYNTHETIC_USAGE, MODEL)
    assert (result.stop_reason, result.cost_usd) == ("end_turn", row.settled_usd)
    assert counted.delta() == {"ok": 1}
    assert fake_llm.served == 1 and await no_drift(engine, [row])
    # the request: the policy's effort and stage, streamed, the purpose header for the fake
    (request,) = fake_llm.requests
    body = request.body
    assert (body["max_tokens"], body["stream"], body["output_config"]["effort"]) == (1000, True, "medium")
    assert request.purpose == "extract_profile"


async def test_rate_limited_then_ok_waits_retry_after_and_bills_the_second_try(
    engine, fake_llm: FakeLLM, owner, ec
) -> None:
    fake_llm.scenario("rate_limited", "ok")
    counted = Counted("http_429", "ok")
    await fake_llm.gateway(engine).call(GATEWAY_NODE, gateway_prompt(), call_context(owner, ec))
    rows = await tries(engine)
    assert states(rows) == ["not_billed:error_response_no_content", "billed"]
    assert [(r.status, r.http_status, r.error_code, r.transport_try) for r in rows] == [
        ("error", 429, "http_429", 1),
        ("completed", 200, None, 2),
    ]
    assert rows[0].provider_request_id == "req_fake_0001" and rows[0].cost_usd == 0
    assert fake_llm.sleeps.waits == [1.0]  # the fake's `retry-after: 1` wins over the 2 s backoff
    assert counted.delta() == {"http_429": 1, "ok": 1}
    assert fake_llm.served == 2 and await no_drift(engine, rows)


async def test_overloaded_on_every_try_is_transient_after_three_tries(
    engine, fake_llm: FakeLLM, owner, ec
) -> None:
    fake_llm.scenario("overloaded_529")
    counted = Counted("http_529")
    with pytest.raises(LLMTransient) as transient:
        await fake_llm.gateway(engine).call(GATEWAY_NODE, gateway_prompt(), call_context(owner, ec))
    assert transient.value.code == "http_529"
    assert transient.value.__context__ is None and transient.value.__cause__ is None  # no provider error
    rows = await tries(engine)
    assert states(rows) == ["not_billed:error_response_no_content"] * 3
    assert [r.transport_try for r in rows] == [1, 2, 3]
    first, second = fake_llm.sleeps.waits  # no wait after the last try
    assert 1.5 <= first <= 2.5 and 6.0 <= second <= 10.0
    assert counted.delta() == {"http_529": 3}
    assert fake_llm.served == 3 and await no_drift(engine, rows)


async def test_max_tokens_then_ok_moves_to_the_enlarged_stage(engine, fake_llm: FakeLLM, owner, ec) -> None:
    fake_llm.scenario("max_tokens", "ok")
    counted = Counted("max_tokens_first_stage", "ok")
    result = await fake_llm.gateway(engine).call(GATEWAY_NODE, gateway_prompt(), call_context(owner, ec))
    rows = await tries(engine)
    assert states(rows) == ["billed", "billed"]
    assert [(r.max_tokens, r.max_tokens_stage, r.transport_try) for r in rows] == [
        (1000, "first", 1),
        (2000, "enlarged", 1),
    ]
    assert [r.max_tokens for r in fake_llm.requests] == [1000, 2000]
    assert (rows[0].status, rows[0].stop_reason, rows[0].error_code) == (
        "completed",
        "max_tokens",
        "max_tokens_first_stage",
    )
    assert rows[0].output_json is None and rows[1].output_json == GATEWAY_BODY
    assert rows[1].reserved_usd > rows[0].reserved_usd  # the enlarged stage reserves its own worst case
    assert result.llm_call_id == rows[1].call_id and result.cost_usd == sum(r.settled_usd or 0 for r in rows)
    assert fake_llm.sleeps.waits == []  # no backoff before the enlarged stage
    assert counted.delta() == {"max_tokens_first_stage": 1, "ok": 1}
    assert await no_drift(engine, rows)


async def test_max_tokens_in_both_stages_is_max_tokens_exhausted(
    engine, fake_llm: FakeLLM, owner, ec
) -> None:
    fake_llm.scenario("max_tokens")
    with pytest.raises(MaxTokensExhausted):
        await fake_llm.gateway(engine).call(GATEWAY_NODE, gateway_prompt(), call_context(owner, ec))
    rows = await tries(engine)
    assert states(rows) == ["billed", "billed"]
    assert [(r.max_tokens_stage, r.status, r.error_code) for r in rows] == [
        ("first", "completed", "max_tokens_first_stage"),
        ("enlarged", "completed", "max_tokens"),
    ]


async def test_a_call_resumed_at_the_enlarged_stage_never_sends_the_first_stage(
    engine, fake_llm: FakeLLM, owner, ec
) -> None:
    """Final review FR-2: a call whose `start_stage` is `enlarged` (its paid first stage stopped at
    `max_tokens` before a crash) sends the enlarged request first: its reservation is the enlarged worst case,
    its first try needs the first try's 30 s of a fresh deadline, and the logical call counts this
    execution's tries."""
    deadline = time.monotonic() + 119.5  # less than a later try's 120 s, more than a first try's 30 s
    ctx = call_context(owner, ec, start_stage="enlarged", deadline=deadline)
    result = await fake_llm.gateway(engine).call(GATEWAY_NODE, gateway_prompt(), ctx)
    (row,) = await tries(engine)
    assert [r.max_tokens for r in fake_llm.requests] == [GATEWAY_NODE.max_tokens_retry]
    assert (row.max_tokens, row.max_tokens_stage, row.transport_try, row.error_code) == (
        2000,
        "enlarged",
        1,
        None,
    )
    bound = text_input_bound(MODEL, gateway_prompt().as_request(GATEWAY_NODE.schema))
    assert row.reserved_usd == worst_case_usd(MODEL, input_tokens_bound=bound, max_tokens=2000)
    assert (result.llm_call_id, result.cost_usd) == (row.call_id, row.settled_usd)
    assert await no_drift(engine, [row])
    # at the enlarged stage a max_tokens stop is the terminal one, with no first-stage try before it
    fake_llm.scenario("max_tokens")
    with pytest.raises(MaxTokensExhausted):
        await fake_llm.gateway(engine).call(
            GATEWAY_NODE, gateway_prompt(), call_context(owner, ec, start_stage="enlarged")
        )
    rows = await tries(engine)
    assert [(r.max_tokens_stage, r.error_code) for r in rows] == [
        ("enlarged", None),
        ("enlarged", "max_tokens"),
    ]
    assert [r.max_tokens for r in fake_llm.requests] == [2000, 2000]


async def test_refusal_is_billed_and_refused_with_its_category(engine, fake_llm: FakeLLM, owner, ec) -> None:
    fake_llm.scenario("refusal")
    counted = Counted("refused")
    with pytest.raises(Refused) as refused:
        await fake_llm.gateway(engine).call(GATEWAY_NODE, gateway_prompt(), call_context(owner, ec))
    assert refused.value.category == REFUSAL_CATEGORY
    (row,) = await tries(engine)
    assert states([row]) == ["billed"] and row.settled_usd is not None and row.settled_usd > 0
    assert (row.status, row.stop_reason, row.error_code, row.refusal_category) == (
        "completed",
        "refusal",
        "refused",
        REFUSAL_CATEGORY,
    )
    assert row.output_json is None
    assert counted.delta() == {"refused": 1} and fake_llm.served == 1


async def test_model_mismatch_is_billed_alerted_and_never_used(engine, fake_llm: FakeLLM, owner, ec) -> None:
    fake_llm.scenario("model_mismatch")
    counted = Counted("model_mismatch")
    with pytest.raises(ModelMismatch) as mismatch:
        await fake_llm.gateway(engine).call(GATEWAY_NODE, gateway_prompt(), call_context(owner, ec))
    assert mismatch.value.requested == MODEL and mismatch.value.served != MODEL
    (row,) = await tries(engine)
    assert states([row]) == ["billed"]
    assert (row.status, row.error_code, row.model_served) == (
        "completed",
        "model_mismatch",
        mismatch.value.served,
    )
    assert row.output_json is None
    assert await open_alerts(engine, "llm.model_mismatch") == [
        (f"llm.model_mismatch:model:{MODEL}", "critical", 1)
    ]
    assert counted.delta() == {"model_mismatch": 1} and fake_llm.served == 1


async def test_http_401_is_a_request_error_with_the_auth_alert_until_a_call_succeeds(
    engine, fake_llm: FakeLLM, owner, ec
) -> None:
    fake_llm.scenario("http_401")
    counted = Counted("http_401")
    gw = fake_llm.gateway(engine)
    with pytest.raises(LLMRequestError) as refused:
        await gw.call(GATEWAY_NODE, gateway_prompt(), call_context(owner, ec))
    assert refused.value.status == 401 and refused.value.__context__ is None
    (row,) = await tries(engine)
    assert states([row]) == ["not_billed:error_response_no_content"]
    assert (row.status, row.http_status, row.error_code) == ("error", 401, "http_401")
    assert await open_alerts(engine, "llm.auth_failed") == [("llm.auth_failed:anthropic", "critical", 1)]
    assert counted.delta() == {"http_401": 1} and fake_llm.served == 1  # never retried
    fake_llm.scenario("ok")
    await gw.call(GATEWAY_NODE, gateway_prompt(), call_context(owner, ec))
    assert await open_alerts(engine, "llm.auth_failed") == []  # a successful call resolves it


async def test_connect_refused_is_not_billed_and_retried(engine, fake_llm: FakeLLM, owner, ec) -> None:
    fake_llm.scenario("connect_refused", "ok")
    counted = Counted("connect_failed", "ok")
    await fake_llm.gateway(engine).call(GATEWAY_NODE, gateway_prompt(), call_context(owner, ec))
    rows = await tries(engine)
    assert states(rows) == ["not_billed:connect_failed_before_send", "billed"]
    assert (rows[0].status, rows[0].http_status, rows[0].error_code) == ("error", None, "connect_failed")
    (wait,) = fake_llm.sleeps.waits
    assert 1.5 <= wait <= 2.5
    assert counted.delta() == {"connect_failed": 1, "ok": 1} and await no_drift(engine, rows)


async def test_a_parse_error_is_billed_and_invalid_output_for_the_callers_repair(
    engine, fake_llm: FakeLLM, owner, ec
) -> None:
    fake_llm.scenario("parse_error")
    counted = Counted("invalid_output")
    with pytest.raises(InvalidOutput) as invalid:
        await fake_llm.gateway(engine).call(GATEWAY_NODE, gateway_prompt(), call_context(owner, ec))
    assert invalid.value.code == "structural" and invalid.value.__context__ is None
    (row,) = await tries(engine)
    assert states([row]) == ["billed"]
    assert (row.status, row.stop_reason, row.error_code, row.output_json) == (
        "completed",
        "end_turn",
        "invalid_output",
        None,
    )
    assert counted.delta() == {"invalid_output": 1} and fake_llm.served == 1


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"answer": "a synthetic\u0000answer", "score": 3}, id="nul"),
        # as raw JSON text: the escape of a lone surrogate, which `json.loads` takes and no UTF-8 holds
        pytest.param('{"answer": "a synthetic\\ud800answer", "score": 3}', id="lone-surrogate"),
    ],
)
async def test_an_unstorable_output_is_billed_invalid_output_and_its_settlement_commits(
    engine, fake_llm: FakeLLM, owner, ec, body: Any
) -> None:
    """Final review FR-1 (d): a parsed output that holds a NUL (which no JSONB stores) or a lone surrogate is
    an invalid output, decided before the settlement writes `output_json`: the try settles billed with its
    usage, `output_json` NULL and `invalid_output`, and nothing is left `dispatched` or `ambiguous`."""
    fake_llm.record("extract_profile", MODEL, body)
    counted = Counted("invalid_output")
    with pytest.raises(InvalidOutput) as invalid:
        await fake_llm.gateway(engine).call(GATEWAY_NODE, gateway_prompt(), call_context(owner, ec))
    assert invalid.value.code == "structural" and invalid.value.__context__ is None
    (row,) = await tries(engine)
    assert states([row]) == ["billed"] and row.settled_usd == settled_usd(MODEL, SYNTHETIC_USAGE)
    assert (row.status, row.stop_reason, row.error_code, row.output_json) == (
        "completed",
        "end_turn",
        "invalid_output",
        None,
    )
    assert (row.input_tokens, row.output_tokens, row.cost_usd) == (120, 60, row.settled_usd)
    assert counted.delta() == {"invalid_output": 1} and fake_llm.served == 1
    assert await no_drift(engine, [row])


async def test_an_unexpected_stop_reason_is_invalid_output_with_a_warn_alert(
    engine, fake_llm: FakeLLM, owner, ec
) -> None:
    node = dataclasses.replace(GATEWAY_NODE, purpose="propose_queries")
    fake_llm.record("propose_queries", MODEL, stop_reason="pause_turn")
    counted = Counted("unexpected_stop_reason", purpose="propose_queries")
    with pytest.raises(InvalidOutput) as invalid:
        await fake_llm.gateway(engine).call(node, gateway_prompt(), call_context(owner, ec))
    assert invalid.value.code == "unexpected_stop_reason"
    (row,) = await tries(engine)
    assert states([row]) == ["billed"]
    assert (row.status, row.stop_reason, row.error_code, row.output_json) == (
        "completed",
        "pause_turn",
        "unexpected_stop_reason",
        None,
    )
    assert await open_alerts(engine, "llm.unexpected_stop_reason") == [
        ("llm.unexpected_stop_reason:purpose:propose_queries", "warn", 1)
    ]
    assert counted.delta() == {"unexpected_stop_reason": 1}


async def test_the_logical_call_number_is_recorded_on_every_try(engine, fake_llm: FakeLLM, owner, ec) -> None:
    fake_llm.scenario("rate_limited", "ok")
    await fake_llm.gateway(engine).call(
        GATEWAY_NODE, gateway_prompt(), call_context(owner, ec, logical_call_no=2)
    )
    rows = await tries(engine)
    assert [(r.logical_call_no, r.transport_try) for r in rows] == [(2, 1), (2, 2)]


async def test_native_pdf_reserves_the_counted_bound_and_the_buffer_share(
    engine, fake_llm: FakeLLM, owner, ec
) -> None:
    fake_llm.count_tokens(1800)
    await fake_llm.gateway(engine).call(
        GATEWAY_NODE, gateway_prompt(), call_context(owner, ec, native_pdf=True)
    )
    (row,) = await tries(engine)
    bound = native_pdf_bound(MODEL, 1800)
    # ceil(1.25 x 1800), plus the model's overhead
    assert row.input_tokens_bound == bound == 2250 + PROMPT_OVERHEAD_TOKENS[MODEL]
    # the seeded month row's buffer is $2.00, so the share is $0.50
    assert row.reserved_usd == worst_case_usd(
        MODEL, input_tokens_bound=bound, max_tokens=1000, extra_usd=Decimal("0.50")
    )
    assert fake_llm.state.count_tokens_requests == 1 and fake_llm.served == 1


@pytest.mark.parametrize("answer", ["error", CONTEXT_WINDOW[MODEL] + 1])
async def test_an_unbounded_native_pdf_reserves_and_sends_nothing(
    engine, fake_llm: FakeLLM, owner, ec, answer: Any
) -> None:
    fake_llm.count_tokens(answer)
    with pytest.raises(InputUnbounded) as unbounded:
        await fake_llm.gateway(engine).call(
            GATEWAY_NODE, gateway_prompt(), call_context(owner, ec, native_pdf=True)
        )
    assert unbounded.value.reason == ("count_failed" if answer == "error" else "context_window")
    # the SDK's error, whose request holds the count body (the native PDF), is neither cause nor context
    assert unbounded.value.__cause__ is None and unbounded.value.__context__ is None
    assert await tries(engine) == [] and fake_llm.served == 0


async def test_the_count_is_made_on_the_body_that_is_sent(engine, fake_llm: FakeLLM, owner, ec) -> None:
    gw = fake_llm.gateway(engine)
    body = gw.count_body(GATEWAY_NODE, gateway_prompt())
    await gw.call(GATEWAY_NODE, gateway_prompt(), call_context(owner, ec))
    (request,) = fake_llm.requests
    sent = dict(request.body)
    assert {k: v for k, v in sent.items() if k not in ("max_tokens", "stream")} == body
    fake_llm.count_tokens(42)
    assert await gw.count_tokens(MODEL, body) == 42


async def test_a_budget_refusal_sends_nothing(engine, fake_llm: FakeLLM, owner, ec) -> None:
    await save_budget(engine, now=utcnow(), cap_usd="2.01")  # A = $0.01: below the worst case
    with pytest.raises(BudgetExhausted):
        await fake_llm.gateway(engine).call(GATEWAY_NODE, gateway_prompt(), call_context(owner, ec))
    assert await tries(engine) == [] and fake_llm.served == 0


class FailingSettlementCommit:
    """An engine whose transactions behave as the real ones, except that one in which `spend_ledger.settle`
    ran rolls back at its end and raises, as a failed commit would: everything the gateway did inside the
    settlement transaction happened, and none of it was committed."""

    def __init__(self, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> None:
        self._engine = engine
        self._settled_on: set[int] = set()
        real = spend_ledger.settle

        async def settle(conn: Any, *args: Any, **kwargs: Any) -> spend_ledger.SettleFigures:
            figures = await real(conn, *args, **kwargs)
            self._settled_on.add(id(conn))
            return figures

        monkeypatch.setattr(spend_ledger, "settle", settle)

    @contextlib.asynccontextmanager
    async def begin(self) -> AsyncIterator[Any]:
        async with self._engine.connect() as conn:
            tx = await conn.begin()
            try:
                yield conn
            except BaseException:
                await tx.rollback()
                raise
            if id(conn) in self._settled_on:
                await tx.rollback()
                raise RuntimeError("injected at the settlement's commit")
            await tx.commit()

    def connect(self) -> Any:
        return self._engine.connect()


def cache_ratio() -> float | None:
    labels = {"model": MODEL, "effort": "medium", "role": "extract_profile"}
    return REGISTRY.get_sample_value("jobhunter_llm_cache_read_ratio", labels)


async def test_the_spend_metrics_move_only_after_the_settlement_commits(
    engine, fake_llm: FakeLLM, owner, ec, monkeypatch
) -> None:
    monitor = CacheMonitor()  # both gateways share it, as one worker's calls do
    prompt = gateway_prompt(cached_prefix=True)
    prefix = prompt.prefix_sha256()
    assert prefix is not None
    # the first call reads 50 tokens from the cache: ratio 50 / (120 + 50)
    fake_llm.record("extract_profile", MODEL, usage={"cache_read_input_tokens": 50})
    await fake_llm.gateway(engine, cache=monitor).call(GATEWAY_NODE, prompt, call_context(owner, ec))
    after_first = spend_gauges()
    assert after_first["billed"] is not None and after_first["billed"] > 0
    assert cache_ratio() == 50 / 170
    reservation_ratio = REGISTRY.get_sample_value(
        "jobhunter_llm_reservation_ratio_count", {"purpose": "extract_profile"}
    )
    counted = Counted("ok")
    # the second reads nothing 0 s later: observed, it would move the ratio and start the zero-read streak
    fake_llm.record("extract_profile", MODEL)
    failing = fake_llm.gateway(cast(AsyncEngine, FailingSettlementCommit(engine, monkeypatch)), cache=monitor)
    with pytest.raises(RuntimeError, match="injected"):
        await failing.call(GATEWAY_NODE, prompt, call_context(owner, ec))
    assert spend_gauges() == after_first and counted.delta() == {"ok": 0}
    assert (
        REGISTRY.get_sample_value("jobhunter_llm_reservation_ratio_count", {"purpose": "extract_profile"})
        == reservation_ratio
    )
    assert cache_ratio() == 50 / 170 and monitor.ratio(MODEL, "medium", "extract_profile") == 50 / 170
    assert monitor.streak(MODEL, "medium", "extract_profile", prefix) == 0
    rows = await tries(engine)
    assert states(rows) == ["billed", "dispatched"]  # the settlement rolled back; the reconciler takes it


async def test_a_failed_cache_alert_write_never_fails_the_billed_call(
    engine, fake_llm: FakeLLM, owner, ec, monkeypatch, log_events
) -> None:
    monitor = CacheMonitor()
    gw = fake_llm.gateway(engine, cache=monitor)
    prompt = gateway_prompt(cached_prefix=True)
    for _ in range(3):  # the prefix's first call, then two misses
        await gw.call(GATEWAY_NODE, prompt, call_context(owner, ec))

    async def unreachable_database(*args: Any, **kwargs: Any) -> None:
        raise sa.exc.OperationalError("INSERT", {}, ConnectionError("database unreachable"))

    monkeypatch.setattr(gateway_module, "write_alerts", unreachable_database)
    result = await gw.call(GATEWAY_NODE, prompt, call_context(owner, ec))  # the third miss: the streak alert
    assert result.parsed == GatewayOut.model_validate(GATEWAY_BODY)
    (event,) = [e for e in log_events() if e["event"] == "llm_cache_alerts_unwritten"]
    assert (event["alerts"], event["error_class"]) == (1, "OperationalError")
    assert await open_alerts(engine, "llm.cache_miss_streak") == []
    monkeypatch.setattr(gateway_module, "write_alerts", write_alerts)
    await gw.call(GATEWAY_NODE, prompt, call_context(owner, ec))  # the given-back alert is written with it
    ((_, severity, occurrences),) = await open_alerts(engine, "llm.cache_miss_streak")
    assert (severity, occurrences) == ("warn", 2)
    assert states(await tries(engine)) == ["billed"] * 5


async def test_a_reclaimed_owner_never_sends_and_releases_its_try(
    engine, fake_llm: FakeLLM, owner, ec, monkeypatch
) -> None:
    pause = PausePoint("gateway.after_reserve")
    monkeypatch.setattr(gateway_module, "afault_point", pause)
    call = asyncio.create_task(
        fake_llm.gateway(engine).call(GATEWAY_NODE, gateway_prompt(), call_context(owner, ec))
    )
    await asyncio.wait_for(pause.reached.wait(), 10)
    await reclaim(engine, owner)  # claim_id := NULL: the recorded owner predicate no longer holds
    pause.resume()
    with pytest.raises(FencedOut):
        await asyncio.wait_for(call, 10)
    (row,) = await tries(engine)
    assert states([row]) == ["not_billed:never_dispatched"]
    assert (row.status, row.error_code, row.cost_usd) == ("cancelled", "never_dispatched", Decimal(0))
    assert fake_llm.served == 0 and await no_drift(engine, [row])  # no request reached the transport


async def test_a_retry_or_the_enlarged_stage_needs_120_s_left(engine, fake_llm: FakeLLM, owner, ec) -> None:
    gw = fake_llm.gateway(engine)
    fake_llm.scenario("max_tokens")
    with pytest.raises(DeadlineExhausted):
        await gw.call(
            GATEWAY_NODE, gateway_prompt(), call_context(owner, ec, deadline=time.monotonic() + 119.5)
        )
    assert [r.max_tokens_stage for r in await tries(engine)] == ["first"]  # no enlarged try
    fake_llm.scenario("connect_refused")
    with pytest.raises(DeadlineExhausted):
        await gw.call(
            GATEWAY_NODE, gateway_prompt(), call_context(owner, ec, deadline=time.monotonic() + 119.5)
        )
    assert [r.transport_try for r in await tries(engine)] == [1, 1]  # no retry either
    assert fake_llm.served == 2


async def test_the_deadline_ends_a_try_in_flight_as_ambiguous(
    engine, fake_llm: FakeLLM, owner, ec, monkeypatch
) -> None:
    clock = FakeClock(10_000.0)

    async def skip_ahead(point: str) -> None:
        if point == "gateway.after_dispatch":
            clock.now += 29.0  # 1.5 s of the call's deadline left once the stream starts

    monkeypatch.setattr(gateway_module, "afault_point", skip_ahead)
    fake_llm.scenario("stall_after_first_event")  # with the 120 s read timeout: only the deadline ends it
    counted = Counted("deadline")
    started = time.monotonic()
    with pytest.raises(DeadlineExhausted):
        await fake_llm.gateway(engine, monotonic=clock).call(
            GATEWAY_NODE, gateway_prompt(), call_context(owner, ec, deadline=clock.now + 30.5)
        )
    assert 1.4 <= time.monotonic() - started < 5
    (row,) = await tries(engine)
    assert states([row]) == ["ambiguous"] and (row.status, row.error_code) == ("ambiguous", "deadline")
    assert row.cost_usd == row.reserved_usd and counted.delta() == {"deadline": 1}
    assert await no_drift(engine, [row])


async def test_a_cached_system_block_carries_the_5_minute_marker_only(
    engine, fake_llm: FakeLLM, owner, ec
) -> None:
    await fake_llm.gateway(engine).call(
        GATEWAY_NODE, gateway_prompt(cached_prefix=True), call_context(owner, ec)
    )
    (request,) = fake_llm.requests
    (block,) = request.body["system"]
    assert block["cache_control"] == {"type": "ephemeral"}  # never a 1 h ttl: pricing is the 5 min rate
    (row,) = await tries(engine)
    assert gateway_prompt(cached_prefix=True).prefix_sha256() is not None
    assert gateway_prompt().prefix_sha256() is None
    async with engine.connect() as conn:
        stored = (
            await conn.execute(
                sa.text("SELECT prefix_sha256 FROM llm_calls WHERE id = :i"), {"i": row.call_id}
            )
        ).scalar_one()
    assert bytes(stored) == gateway_prompt(cached_prefix=True).prefix_sha256()


async def test_the_cache_tokens_are_billed_once_at_their_own_prices(
    engine, fake_llm: FakeLLM, owner, ec
) -> None:
    # The API's `input_tokens` excludes the cache tokens; langchain's `usage_metadata` adds them in (V13).
    cached = {"input_tokens": 120, "cache_creation_input_tokens": 30, "cache_read_input_tokens": 50}
    fake_llm.record("extract_profile", MODEL, usage=cached)
    result = await fake_llm.gateway(engine).call(GATEWAY_NODE, gateway_prompt(), call_context(owner, ec))
    assert result.usage == Usage(120, 60, cache_creation_input_tokens=30, cache_read_input_tokens=50)
    (row,) = await tries(engine)
    assert (row.input_tokens, row.cache_read_input_tokens) == (120, 50)
    assert row.settled_usd == settled_usd(MODEL, Usage(120, 60, 30, 50))


async def test_null_cache_counts_are_zero(engine, fake_llm: FakeLLM, owner, ec) -> None:
    nulls = {"cache_creation_input_tokens": None, "cache_read_input_tokens": None}
    fake_llm.record("extract_profile", MODEL, usage=nulls)
    result = await fake_llm.gateway(engine).call(GATEWAY_NODE, gateway_prompt(), call_context(owner, ec))
    assert result.usage == SYNTHETIC_USAGE
    (row,) = await tries(engine)
    assert (row.input_tokens, row.cache_read_input_tokens, row.state) == (120, 0, "billed")


async def test_a_failed_settlement_of_a_cancelled_try_never_hides_the_cancellation(
    engine, fake_llm: FakeLLM, owner, ec, monkeypatch, log_events
) -> None:
    fake_llm.scenario("stall_after_first_event")  # 120 s read timeout: only the cancellation ends it
    call = asyncio.create_task(
        fake_llm.gateway(engine).call(GATEWAY_NODE, gateway_prompt(), call_context(owner, ec))
    )
    for _ in range(500):
        if fake_llm.served:
            break
        await asyncio.sleep(0.01)
    assert fake_llm.served == 1

    async def unreachable_database(*args: Any, **kwargs: Any) -> spend_ledger.SettleFigures:
        raise sa.exc.OperationalError("SELECT 1", {}, ConnectionError("database unreachable"))

    monkeypatch.setattr(spend_ledger, "settle", unreachable_database)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    (row,) = await tries(engine)
    assert row.state == "dispatched"  # left to llm_ledger_unsettled, which makes it ambiguous
    (event,) = [e for e in log_events() if e["event"] == "llm_cancelled_try_unsettled"]
    assert (event["llm_call_id"], event["error_class"]) == (row.call_id, "OperationalError")
