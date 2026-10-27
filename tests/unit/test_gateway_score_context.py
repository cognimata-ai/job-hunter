"""The gateway's score context, without a database (spec §9.4.2, §10.3, §10.9, §10.10; plan Task 7): what
`_reserve` and `_dispatch` hand the spend ledger for a score call, the batch request body, the pinned node
configuration and its per-round effort, and the storable check of the provider metadata at settlement."""

import dataclasses
import json
import logging
import time
from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import TracebackType
from typing import Any, cast
from uuid import UUID

import pytest
from jobhunter.llm import gateway, spend_ledger
from jobhunter.llm.cache_monitor import CacheMonitor
from jobhunter.llm.gateway import CallContext, LLMGateway, NodeConfig, WireSchema, pinned_node_config
from jobhunter.llm.spend_ledger import CallOutcome, Reservation, TargetAttemptOwner, storable_outcome
from jobhunter.llm.transport import make_http_client
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.support.llm import (
    FAKE_LLM_DIR,
    GATEWAY_BODY,
    GatewayOut,
    capture_request,
    fake_llm_config,
    gateway_prompt,
)
from tests.support.logs import logged

REPO = Path(__file__).resolve().parents[2]
SEED = json.loads((REPO / "backend/src/jobhunter/db/seed_data/settings_v1.json").read_text("utf-8"))
OWNER = TargetAttemptOwner(target_id=12, claim_id=UUID(int=5), attempt_no=2, attempt_id=11)
SCORE_NODE = NodeConfig(
    purpose="generate",
    model="claude-sonnet-5-5",
    effort="medium",
    max_tokens=6000,
    max_tokens_retry=12000,
    deadline_s=600,
    schema=WireSchema.from_model(GatewayOut),
)
RES = Reservation(
    ledger_id=1,
    llm_call_id=2,
    month=date(2026, 10, 1),
    reserved_usd=Decimal("0.1"),
    purpose_class="scoring",
    model="claude-sonnet-5-5",
    batch=False,
)


class _Transaction:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        return None


class _Engine:
    """An engine whose transactions are empty: the ledger calls are captured, and nothing reaches a
    database."""

    def begin(self) -> _Transaction:
        return _Transaction()


def fake_gateway(tmp_path: Path, http_client: Any) -> LLMGateway:
    cfg = fake_llm_config(FAKE_LLM_DIR, tmp_path / "no-secrets")
    return LLMGateway(cast(AsyncEngine, _Engine()), cfg, http_client=http_client, cache=CacheMonitor())


def score_context(**overrides: Any) -> CallContext:
    values: dict[str, Any] = {
        "owner": OWNER,
        "purpose_class": "scoring",
        "execution_config_id": 3,
        "deadline": time.monotonic() + 600,
        "attempt_id": OWNER.attempt_id,
        "target_id": OWNER.target_id,
        "round": 2,
        "bulk_operation_id": 13,
        "logical_call_no": 4,
        **overrides,
    }
    return CallContext(**values)


# ---- what a score call hands the ledger --------------------------------------------------------------------


def test_the_score_context_defaults_to_no_score_ids() -> None:
    ctx = CallContext(owner=OWNER, purpose_class="nonscoring", execution_config_id=1, deadline=0.0)
    assert (ctx.attempt_id, ctx.target_id, ctx.round, ctx.bulk_operation_id, ctx.golden_case_id) == (
        None,
    ) * 5
    assert (ctx.logical_call_no, ctx.start_stage) == (1, "first")


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        (
            {},
            {"attempt_id": 11, "target_id": 12, "round": 2, "bulk_operation_id": 13, "golden_case_id": None},
        ),
        (  # a golden case's critic call of an eval run: no attempt, its case and run (§11.13)
            {
                "attempt_id": None,
                "target_id": None,
                "round": 0,
                "bulk_operation_id": None,
                "golden_case_id": 21,
                "eval_run_id": 22,
                "purpose_class": "nonscoring",
            },
            {
                "attempt_id": None,
                "target_id": None,
                "round": 0,
                "bulk_operation_id": None,
                "golden_case_id": 21,
            },
        ),
    ],
    ids=["score_attempt", "golden_case"],
)
async def test_reserve_passes_the_score_ids_and_the_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any], expected: dict[str, Any]
) -> None:
    seen: dict[str, Any] = {}

    async def capture(conn: AsyncConnection, **kw: Any) -> Reservation:
        seen.update(kw)
        return RES

    monkeypatch.setattr(spend_ledger, "reserve", capture)
    ctx = score_context(**overrides)
    cfg = fake_llm_config(FAKE_LLM_DIR, tmp_path / "no-secrets")
    async with make_http_client(cfg) as client:
        gw = fake_gateway(tmp_path, client)
        res = await gw._reserve(SCORE_NODE, gateway_prompt(), ctx, "first", 1, 6000, 4321, b"r" * 32, None)
    assert res is RES
    assert {key: seen[key] for key in expected} == expected
    assert (seen["eval_run_id"], seen["logical_call_no"], seen["transport"], seen["deadline_s"]) == (
        ctx.eval_run_id,
        4,
        "stream",
        600,
    )
    assert (seen["owner"], seen["purpose_class"], seen["execution_config_id"]) == (
        OWNER,
        ctx.purpose_class,
        3,
    )
    assert "batch_item_id" not in seen  # an online call is no batch item


@pytest.mark.parametrize(
    ("start_stage", "stage", "transport_try", "first_try"),
    [
        ("first", "first", 1, True),  # the logical call's first try
        ("first", "first", 2, False),  # a retry
        ("first", "enlarged", 1, False),  # the enlarged stage after the first
        ("enlarged", "enlarged", 1, True),  # a call resumed at its enlarged stage starts there
        ("enlarged", "enlarged", 3, False),
    ],
)
async def test_dispatch_counts_a_logical_call_on_its_first_try_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    start_stage: gateway.Stage,
    stage: gateway.Stage,
    transport_try: int,
    first_try: bool,
) -> None:
    """§9.4.2 L3567: `logical_calls` moves in the dispatch-marker transaction of a logical call's first try,
    which is the first try of the stage the call starts at."""
    seen: list[tuple[object, bool]] = []

    async def capture(
        conn: AsyncConnection,
        res: Reservation,
        owner: object,
        *,
        now: object,
        first_try_of_logical_call: bool,
    ) -> None:
        seen.append((owner, first_try_of_logical_call))

    monkeypatch.setattr(spend_ledger, "mark_dispatched", capture)
    cfg = fake_llm_config(FAKE_LLM_DIR, tmp_path / "no-secrets")
    async with make_http_client(cfg) as client:
        gw = fake_gateway(tmp_path, client)
        await gw._dispatch(SCORE_NODE, score_context(start_stage=start_stage), RES, stage, transport_try)
    assert seen == [(OWNER, first_try)]


# ---- the batch request body (§10.9, §10.10, L3932) ---------------------------------------------------------


async def test_the_batch_body_is_the_online_body_with_max_tokens_and_no_stream(tmp_path: Path) -> None:
    prompt = gateway_prompt(cached_prefix=True)
    cfg = fake_llm_config(FAKE_LLM_DIR, tmp_path / "no-secrets")
    async with make_http_client(cfg) as client:
        gw = fake_gateway(tmp_path, client)
        body = gw.batch_request_body(SCORE_NODE, prompt, max_tokens=6000)
        counted = gw.count_body(SCORE_NODE, prompt)
    assert body == {**counted, "max_tokens": 6000}
    assert "stream" not in body
    # the online request of the same node sends exactly these params, plus `stream` (§10.9)
    sent = (await capture_request(cfg, SCORE_NODE, prompt, GATEWAY_BODY)).body
    assert sent["stream"] is True
    assert {key: value for key, value in sent.items() if key != "stream"} == body


# ---- the pinned configuration (§10.3, L3813) ---------------------------------------------------------------


class _Rows:
    def __init__(self, row: object | None) -> None:
        self._row = row

    def first(self) -> object | None:
        return self._row


class _PinnedConn:
    """A connection that answers the pinned-configuration read with the seeded payloads and records the ids it
    was asked for (the statement itself runs on PostgreSQL: `tests/integration/test_gateway_score_calls.py`).
    """

    def __init__(self, found: bool = True) -> None:
        self.params: list[Mapping[str, object]] = []
        self._found = found

    async def execute(self, statement: object, params: Mapping[str, object]) -> _Rows:
        self.params.append(params)
        row = type(
            "Row",
            (),
            {"model_policy": SEED["scoring"]["model_policy"], "execution": SEED["scoring"]["execution"]},
        )
        return _Rows(row if self._found else None)


async def test_the_pinned_node_is_read_by_id_with_the_critics_round_effort() -> None:
    conn = _PinnedConn()
    schema = WireSchema.from_model(GatewayOut)
    critic = await pinned_node_config(
        cast(AsyncConnection, conn), "critic", model_policy_id=7, execution_config_id=8, schema=schema
    )
    assert critic == NodeConfig("critic", "claude-opus-5-5", "medium", 6000, 12000, 600, schema)
    light = await pinned_node_config(
        cast(AsyncConnection, conn),
        "critic",
        model_policy_id=7,
        execution_config_id=8,
        schema=schema,
        effort="low",
    )
    assert light == dataclasses.replace(critic, effort="low")  # the light round's effort, nothing else
    assert conn.params == [{"model_policy_id": 7, "execution_config_id": 8}] * 2
    with pytest.raises(ValueError, match="effort"):
        await pinned_node_config(
            cast(AsyncConnection, conn),
            "critic",
            model_policy_id=7,
            execution_config_id=8,
            schema=schema,
            effort=cast(Any, "extreme"),
        )
    with pytest.raises(LookupError):
        await pinned_node_config(
            cast(AsyncConnection, _PinnedConn(found=False)),
            "generate",
            model_policy_id=7,
            execution_config_id=8,
            schema=schema,
        )


# ---- the provider metadata at settlement (M2 Carry-forward J1, Deviation 44) -------------------------------


def test_unstorable_provider_metadata_is_replaced_and_logged_by_field_name(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    call = CallOutcome(
        status="completed",
        model_served="claude-sonnet-5-5\ud800",
        http_status=200,
        stop_reason="end\x00turn",
        refusal_category="cat\udfffegory\x00",
        provider_request_id="req_\x00x",
        output_json={"kept": "as it is"},
    )
    stored = storable_outcome(call)
    assert (stored.model_served, stored.stop_reason, stored.refusal_category, stored.provider_request_id) == (
        "claude-sonnet-5-5?",
        "end?turn",
        "cat?egory?",
        "req_?x",
    )
    assert (
        dataclasses.replace(
            stored,
            model_served=call.model_served,
            stop_reason=call.stop_reason,
            refusal_category=call.refusal_category,
            provider_request_id=call.provider_request_id,
        )
        == call
    )  # every other field as it was
    (event,) = logged(caplog, "llm_provider_metadata_unstorable")
    assert event["fields"] == ["model_served", "stop_reason", "refusal_category", "provider_request_id"]
    assert "req_" not in json.dumps(event, ensure_ascii=False, default=str)  # names only, never the values


def test_storable_provider_metadata_is_left_as_it_is(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    call = CallOutcome(status="completed", model_served="claude-sonnet-5-5", stop_reason="end_turn")
    assert storable_outcome(call) == call
    assert storable_outcome(CallOutcome(status="ambiguous")) == CallOutcome(status="ambiguous")
    assert logged(caplog, "llm_provider_metadata_unstorable") == []
