"""The fake transport's keyed answers (M3 Task 8): `fake.answer`, the `X-JH-Fake-Key` request header and the
resolution order of a message try (a keyed answer, then the scenario script, then the recording).

Every request runs offline on the fake, through the real `_chat` stack, or through `LLMGateway.call` with its
ledger steps replaced (no database: `_reserve`, `_dispatch` and the billed settlement are captured).
"""

import time
from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import anthropic
import pytest
from jobhunter.config import load_config
from jobhunter.llm import fake, gateway
from jobhunter.llm.cache_monitor import CacheMonitor
from jobhunter.llm.gateway import (
    CallContext,
    GatewayResult,
    LLMGateway,
    NodeConfig,
    RenderedPrompt,
    SystemBlock,
    WireSchema,
    _chat,
)
from jobhunter.llm.pricing import Model, Usage
from jobhunter.llm.prompts.render import prompt_schema_v2
from jobhunter.llm.schemas.critic import CriticOut
from jobhunter.llm.schemas.generator import GeneratorWire
from jobhunter.llm.spend_ledger import Reservation, TargetAttemptOwner
from jobhunter.llm.transport import make_http_client
from jobhunter.scoring.validator import StructuralFailure, parse_generator
from jobhunter.types import get_object
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.llm import (
    GATEWAY_BODY,
    CapturingTransport,
    FakeLLM,
    GatewayOut,
    critic_body,
    fake_llm_config,
    generator_wire_body,
    synthetic_sse,
)

KEY = SecretStr("fake-keys-test-key-0001")  # visibly fake; the fake ignores keys
MODEL: Model = "claude-sonnet-5-5"
GATEWAY_WIRE = WireSchema.from_model(GatewayOut).json
GENERATOR_WIRE = get_object(prompt_schema_v2()[1], "generator")  # the stored version 2 generator schema (C)
ONE: Mapping[str, Any] = {"answer": "the first target's draft", "score": 1}
TWO: Mapping[str, Any] = {"answer": "the second target's draft", "score": 4}


async def _ask(
    fake_llm: FakeLLM,
    key: str | None,
    *,
    purpose: str = "generate",
    schema: Mapping[str, Any] = GATEWAY_WIRE,
) -> dict[str, Any]:
    """One structured try through `_chat` on the fake, sending `key` as `X-JH-Fake-Key` (none for `None`)."""
    async with fake_llm.client() as client:
        runnable = _chat(
            MODEL,
            "medium",
            1000,
            schema,
            api_key=KEY,
            http_client=client,
            purpose=cast(Any, purpose),
            fake_key=key,
        )
        out = await runnable.ainvoke([HumanMessage("A synthetic question.")])
    assert isinstance(out, dict)
    return out


def _raw(out: Mapping[str, Any]) -> AIMessage:
    raw = out["raw"]
    assert isinstance(raw, AIMessage)
    return raw


# ---- the key -----------------------------------------------------------------------------------------------


def test_the_key_names_the_purpose_the_first_input_id_and_the_round() -> None:
    refs = {"job_revision_id": 7, "golden_case_id": 8, "profile_draft_id": 9, "truncated": False}
    assert fake.answer_key("revise", refs, 2) == "revise:7:2"
    assert fake.answer_key("critic", {"golden_case_id": 8, "profile_draft_id": 9}, 1) == "critic:8:1"
    assert fake.answer_key("extract_profile", {"profile_draft_id": 9}, None) == "extract_profile:9:0"
    assert fake.answer_key("schema_dry_run", {"case": "generator"}, None) == "schema_dry_run:None:0"
    assert fake.KEY_HEADER == "X-JH-Fake-Key"


def test_an_answer_must_be_a_body_or_a_known_scenario() -> None:
    with pytest.raises(ValueError, match="scenario"):
        fake.answer("generate:1:0", cast(Any, "not_a_scenario"))
    with pytest.raises(ValueError, match="key"):
        fake.answer("", b"event: ping\ndata: {}\n\n")


# ---- the resolution order ----------------------------------------------------------------------------------


async def test_two_keys_get_their_registered_bodies(fake_llm: FakeLLM) -> None:
    fake.answer("generate:1:0", synthetic_sse(ONE, model=MODEL))
    fake.answer("generate:2:0", synthetic_sse(TWO, model=MODEL, usage={"output_tokens": 77}))
    second = await _ask(fake_llm, "generate:2:0")
    first = await _ask(fake_llm, "generate:1:0")
    again = await _ask(fake_llm, "generate:2:0")  # a standing answer serves every request of its key
    assert (first["parsed"], second["parsed"], again["parsed"]) == (ONE, TWO, TWO)
    usage = _raw(second).usage_metadata
    assert usage is not None and usage["output_tokens"] == 77
    assert [(r.key, r.scenario, r.status) for r in fake_llm.requests] == [
        ("generate:2:0", "ok", 200),
        ("generate:1:0", "ok", 200),
        ("generate:2:0", "ok", 200),
    ]
    assert fake_llm.state.tries == 0  # no keyed request took a try of the script
    assert fake_llm.served == 3


async def test_an_unknown_key_falls_back_to_the_scenario_script_and_then_the_recording(
    fake_llm: FakeLLM,
) -> None:
    fake_llm.scenario("rate_limited", "ok")
    fake.answer("generate:1:0", synthetic_sse(ONE, model=MODEL))
    with pytest.raises(anthropic.RateLimitError):
        await _ask(fake_llm, "generate:2:0")  # no answer for this key: the script's first name
    keyed = await _ask(fake_llm, "generate:1:0")  # answered by its key, before the script
    recorded = await _ask(fake_llm, "generate:2:0")  # the script's next name, `ok`: the recording
    unkeyed = await _ask(fake_llm, None)
    assert keyed["parsed"] == ONE
    assert recorded["parsed"] == GATEWAY_BODY and unkeyed["parsed"] == GATEWAY_BODY
    assert [(r.key, r.scenario, r.status) for r in fake_llm.requests] == [
        ("generate:2:0", "rate_limited", 429),
        ("generate:1:0", "ok", 200),
        ("generate:2:0", "ok", 200),
        (None, "ok", 200),
    ]
    assert (fake_llm.state.tries, fake_llm.served) == (3, 4)


async def test_a_consumed_answer_serves_once(fake_llm: FakeLLM) -> None:
    fake.answer("generate:1:0", synthetic_sse(ONE, model=MODEL), consume=True)
    assert (await _ask(fake_llm, "generate:1:0"))["parsed"] == ONE
    assert (await _ask(fake_llm, "generate:1:0"))["parsed"] == GATEWAY_BODY  # then the key falls back

    # one-shot answers serve in order before the key's standing answer, and a scenario applies to the
    # recording as the script's would
    fake.answer("generate:1:0", synthetic_sse(TWO, model=MODEL))
    fake.answer("generate:1:0", "refusal", consume=True)
    fake.answer("generate:1:0", synthetic_sse(ONE, model=MODEL), consume=True)
    refused = _raw(await _ask(fake_llm, "generate:1:0"))
    assert refused.response_metadata["stop_reason"] == "refusal" and refused.text == ""
    assert (await _ask(fake_llm, "generate:1:0"))["parsed"] == ONE
    assert (await _ask(fake_llm, "generate:1:0"))["parsed"] == TWO
    assert (await _ask(fake_llm, "generate:1:0"))["parsed"] == TWO
    assert [r.scenario for r in fake_llm.requests] == ["ok", "ok", "refusal", "ok", "ok", "ok"]
    assert fake_llm.state.tries == 1  # only the fallback took a try of the script


async def test_a_keyed_error_scenario_is_answered_before_the_script(fake_llm: FakeLLM) -> None:
    fake.answer("generate:3:0", "overloaded_529", consume=True)
    with pytest.raises(anthropic.OverloadedError):
        await _ask(fake_llm, "generate:3:0")
    assert (await _ask(fake_llm, "generate:3:0"))["parsed"] == GATEWAY_BODY
    assert [(r.scenario, r.status) for r in fake_llm.requests] == [("overloaded_529", 529), ("ok", 200)]


# ---- FakeLLM.answer ----------------------------------------------------------------------------------------


async def test_fake_llm_answer_without_a_body_serves_a_valid_c_draft(fake_llm: FakeLLM) -> None:
    usage = {"input_tokens": 900, "cache_read_input_tokens": 400, "output_tokens": 300}
    key = fake_llm.answer("generate", 41, usage=usage)
    assert key == "generate:41:0"
    out = await _ask(fake_llm, key, schema=GENERATOR_WIRE)
    assert out["parsing_error"] is None
    assert out["parsed"] == generator_wire_body()
    GeneratorWire.model_validate(out["parsed"])
    assert not isinstance(parse_generator(out["parsed"], family="generator_v2_items"), StructuralFailure)
    raw = _raw(out)
    assert raw.response_metadata["model_name"] == MODEL  # the seeded policy's generate model
    assert raw.usage_metadata is not None
    assert (raw.usage_metadata["output_tokens"], raw.usage_metadata["input_token_details"]["cache_read"]) == (
        300,
        400,
    )


async def test_fake_llm_answer_keys_by_revision_and_round_with_the_schemas_default_body(
    fake_llm: FakeLLM,
) -> None:
    critic_key = fake_llm.answer("critic", 41, round=1, schema=CriticOut)
    draft_key = fake_llm.answer("generate", 42, {"answer": "a given body", "score": 2})
    assert (critic_key, draft_key) == ("critic:41:1", "generate:42:0")
    critic = await _ask(fake_llm, critic_key, purpose="critic")
    assert critic["parsed"] == critic_body()
    CriticOut.model_validate(critic["parsed"])
    assert _raw(critic).response_metadata["model_name"] == "claude-opus-5-5"  # the policy's critic model
    assert (await _ask(fake_llm, draft_key))["parsed"] == {"answer": "a given body", "score": 2}
    with pytest.raises(ValueError, match="default body"):
        fake_llm.answer("generate", 43, schema=GatewayOut)


# ---- the gateway sends the key in fake mode only -----------------------------------------------------------

OWNER = TargetAttemptOwner(target_id=12, claim_id=UUID(int=5), attempt_no=1, attempt_id=11)
SCORE_NODE = NodeConfig(
    purpose="generate",
    model=MODEL,
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
    model=MODEL,
    batch=False,
)


def score_prompt(job_revision_id: int) -> RenderedPrompt:
    return RenderedPrompt(
        system=(SystemBlock("You score a job ad.", cache=True),),
        messages=(HumanMessage("A synthetic ad."),),
        template_version="score-test@1",
        input_refs={"job_revision_id": job_revision_id, "prompt_schema_version_id": 2, "truncated": False},
    )


def score_context(round: int) -> CallContext:
    return CallContext(
        owner=OWNER,
        purpose_class="scoring",
        execution_config_id=3,
        deadline=time.monotonic() + 600,
        attempt_id=OWNER.attempt_id,
        target_id=OWNER.target_id,
        round=round,
    )


class _NoEngine:
    def begin(self) -> Any:
        raise AssertionError("the ledger steps are replaced: no transaction is opened")


@pytest.fixture
def answered(monkeypatch: pytest.MonkeyPatch) -> list[Mapping[str, Any]]:
    """The structured outputs of the gateway's tries, with the reservation, the dispatch marker and the billed
    settlement replaced (no database)."""
    outs: list[Mapping[str, Any]] = []

    async def reserve(self: LLMGateway, *args: Any) -> Reservation:
        return RES

    async def dispatch(self: LLMGateway, *args: Any) -> None:
        return None

    async def response(self: LLMGateway, t: Any, receipt: Any, out: Mapping[str, Any], *rest: Any) -> Any:
        outs.append(out)
        result = GatewayResult(
            parsed=GatewayOut(answer="captured", score=0),
            llm_call_id=RES.llm_call_id,
            usage=Usage(0, 0, 0, 0),
            model_served=MODEL,
            stop_reason="end_turn",
            cost_usd=Decimal(0),
        )
        return gateway._Next("return", Decimal(0), result=result)

    monkeypatch.setattr(LLMGateway, "_reserve", reserve)
    monkeypatch.setattr(LLMGateway, "_dispatch", dispatch)
    monkeypatch.setattr(LLMGateway, "_response", response)
    return outs


def _keyed_config(tmp_path: Path) -> Any:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "anthropic_key").write_bytes(b"unit-test-key-never-sent\n")
    (secrets / "anthropic_key").chmod(0o440)
    return load_config("worker", {"JOBHUNTER_ENV": "test", "JOBHUNTER_DB_NAME": "jobhunter"}, secrets)


async def _captured_headers(cfg: Any, round: int) -> Any:
    transport = CapturingTransport(synthetic_sse(GATEWAY_BODY, model=MODEL))
    async with make_http_client(cfg, mount=transport) as client:
        gw = LLMGateway(cast(AsyncEngine, _NoEngine()), cfg, http_client=client, cache=CacheMonitor())
        await gw.call(SCORE_NODE, score_prompt(77), score_context(round))
    (request,) = transport.requests
    return request.headers


async def test_the_gateway_sends_the_fake_key_in_fake_mode(
    tmp_path: Path, answered: list[Mapping[str, Any]]
) -> None:
    cfg = fake_llm_config(tmp_path / "anthropic", tmp_path / "no-secrets")
    headers = await _captured_headers(cfg, round=2)
    assert headers[fake.KEY_HEADER] == "generate:77:2"
    assert headers[fake.PURPOSE_HEADER] == "generate"


async def test_gateway_sends_no_fake_key_outside_fake_mode(
    tmp_path: Path, answered: list[Mapping[str, Any]]
) -> None:
    cfg = _keyed_config(tmp_path)
    assert not cfg.fake_llm
    headers = await _captured_headers(cfg, round=2)
    assert fake.KEY_HEADER not in headers
    assert {name.lower() for name in headers if name.lower().startswith("x-jh-")} == {"x-jh-purpose"}
    assert len(answered) == 1  # the try ran to its end


async def test_two_targets_of_one_test_get_their_own_drafts_through_the_gateway(
    fake_llm: FakeLLM, tmp_path: Path, answered: list[Mapping[str, Any]]
) -> None:
    fake_llm.answer("generate", 77, ONE)
    fake_llm.answer("generate", 78, TWO)
    async with fake_llm.client() as client:
        gw = LLMGateway(
            cast(AsyncEngine, _NoEngine()), fake_llm.cfg, http_client=client, cache=CacheMonitor()
        )
        for revision in (78, 77, 79):
            await gw.call(SCORE_NODE, score_prompt(revision), score_context(0))
    assert [out["parsed"] for out in answered] == [TWO, ONE, GATEWAY_BODY]  # 79 has no answer: the recording
    assert [r.key for r in fake_llm.requests] == ["generate:78:0", "generate:77:0", "generate:79:0"]
