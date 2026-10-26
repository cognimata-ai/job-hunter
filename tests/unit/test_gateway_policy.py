"""The gateway's retry and deadline policy (spec §10.2, §10.4; plan Task 5), without a database."""

import json
import random
import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from typing import Any, cast

import pytest
from jobhunter.llm import gateway
from jobhunter.llm.cache_monitor import CacheMonitor
from jobhunter.llm.errors import DeadlineExhausted, InputUnbounded
from jobhunter.llm.gateway import (
    FIRST_TRY_MIN_REMAINING_S,
    RETRY_MIN_REMAINING_S,
    LLMGateway,
    WireSchema,
    is_retryable_status,
    jittered_backoff,
    min_remaining_s,
    node_config,
    retry_after_seconds,
    retry_wait,
)
from jobhunter.llm.spend_ledger import EvalRunOwner
from jobhunter.llm.transport import make_http_client
from jobhunter.types import JSONValue
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.clock import FakeClock
from tests.support.llm import FAKE_LLM_DIR, GATEWAY_NODE, GatewayOut, fake_llm_config, gateway_prompt

REPO = Path(__file__).resolve().parents[2]

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


def test_the_constants_are_the_spec_values() -> None:
    assert (FIRST_TRY_MIN_REMAINING_S, RETRY_MIN_REMAINING_S) == (30, 120)


def test_the_backoff_is_2_s_then_8_s_each_within_25_percent() -> None:
    rng = random.Random(20261021)  # noqa: S311 - seeded for a reproducible spread, not a secret
    first = [jittered_backoff(1, rng=rng) for _ in range(2000)]
    second = [jittered_backoff(2, rng=rng) for _ in range(2000)]
    assert 1.5 <= min(first) and max(first) <= 2.5
    assert 6.0 <= min(second) and max(second) <= 10.0
    # the jitter spans its range: not a constant
    assert min(first) < 1.6 and max(first) > 2.4
    assert min(second) < 6.4 and max(second) > 9.6


def test_the_backoff_refuses_a_try_number_below_one() -> None:
    with pytest.raises(ValueError):
        jittered_backoff(0)


def test_retry_after_is_read_in_seconds_milliseconds_or_as_an_http_date() -> None:
    assert retry_after_seconds({"retry-after": "7"}, now=NOW) == 7.0
    assert retry_after_seconds({"retry-after": " 2.5 "}, now=NOW) == 2.5
    assert retry_after_seconds({"retry-after-ms": "1500"}, now=NOW) == 1.5
    date = format_datetime(NOW + timedelta(seconds=40), usegmt=True)
    assert retry_after_seconds({"retry-after": date}, now=NOW) == 40.0
    assert retry_after_seconds({}, now=NOW) is None
    assert retry_after_seconds({"retry-after": "soon"}, now=NOW) is None
    assert retry_after_seconds({"retry-after": "-3"}, now=NOW) is None
    past = format_datetime(NOW - timedelta(seconds=5), usegmt=True)
    assert retry_after_seconds({"retry-after": past}, now=NOW) is None


def test_retry_after_wins_over_the_backoff() -> None:
    rng = random.Random(1)  # noqa: S311 - seeded for a reproducible spread, not a secret
    assert retry_wait({"retry-after": "30"}, 1, now=NOW, rng=rng) == 30.0
    assert retry_wait({"retry-after": "1"}, 2, now=NOW, rng=rng) == 1.0
    # without one, the jittered backoff of the try
    assert 1.5 <= retry_wait({}, 1, now=NOW, rng=rng) <= 2.5
    assert 6.0 <= retry_wait({"retry-after": "bogus"}, 2, now=NOW, rng=rng) <= 10.0


@pytest.mark.parametrize("status", [408, 409, 429, 500, 502, 503, 504, 529])
def test_retryable_statuses(status: int) -> None:
    assert is_retryable_status(status)


@pytest.mark.parametrize("status", [200, 400, 401, 403, 404, 413, 422, 501, 505, 530])
def test_every_other_status_is_not_retryable(status: int) -> None:
    assert not is_retryable_status(status)


def test_a_first_try_needs_30_s_and_any_later_try_120_s() -> None:
    assert min_remaining_s("first", 1) == 30
    assert min_remaining_s("first", 2) == 120
    assert min_remaining_s("first", 3) == 120
    assert min_remaining_s("enlarged", 1) == 120
    assert min_remaining_s("enlarged", 3) == 120


def test_a_call_resumed_at_the_enlarged_stage_makes_its_first_try_with_30_s() -> None:
    """Final review FR-2: a call that starts at the enlarged stage (`CallContext.start_stage`) has a fresh
    deadline, and its first try is a first try; its retries need 120 s as any other."""
    assert min_remaining_s("enlarged", 1, start_stage="enlarged") == 30
    assert min_remaining_s("enlarged", 2, start_stage="enlarged") == 120
    assert min_remaining_s("enlarged", 3, start_stage="enlarged") == 120


class _NoEngine:
    """An engine a test must never reach: the gateway refuses before any transaction."""

    def begin(self) -> Any:
        raise AssertionError("the gateway opened a transaction")

    def connect(self) -> Any:
        raise AssertionError("the gateway opened a connection")


async def _refused_call(tmp_path: Path, *, left_s: float, text: str = "A synthetic question.") -> None:
    clock = FakeClock(1_000.0)
    cfg = fake_llm_config(FAKE_LLM_DIR, tmp_path / "no-secrets")
    async with make_http_client(cfg) as client:
        gw = LLMGateway(
            cast(AsyncEngine, _NoEngine()), cfg, http_client=client, monotonic=clock, cache=CacheMonitor()
        )
        ctx = gateway.CallContext(
            owner=EvalRunOwner(1),
            purpose_class="nonscoring",
            execution_config_id=1,
            deadline=clock.now + left_s,
            eval_run_id=1,
        )
        await gw.call(GATEWAY_NODE, gateway_prompt(text), ctx)


async def test_a_first_try_with_less_than_30_s_left_is_never_reserved(tmp_path: Path) -> None:
    with pytest.raises(DeadlineExhausted):
        await _refused_call(tmp_path, left_s=29.999)


async def test_a_lone_surrogate_is_refused_before_any_reservation(tmp_path: Path) -> None:
    started = time.monotonic()
    with pytest.raises(InputUnbounded) as refused:
        await _refused_call(tmp_path, left_s=900, text="half a pair: \ud800")
    assert refused.value.reason == "unencodable"
    assert "\ud800" not in str(refused.value)
    # the encoding error, whose `.object` is the whole rendered prompt, is neither its cause nor its context
    assert refused.value.__cause__ is None and refused.value.__context__ is None
    assert time.monotonic() - started < 5


SEED = json.loads((REPO / "backend/src/jobhunter/db/seed_data/settings_v1.json").read_text("utf-8"))


def test_node_config_reads_the_model_policy_and_the_execution_config() -> None:
    policy, execution = SEED["scoring"]["model_policy"], SEED["scoring"]["execution"]
    node = node_config(
        "extract_profile", model_policy=policy, execution=execution, schema=WireSchema.from_model(GatewayOut)
    )
    assert node == gateway.NodeConfig(
        purpose="extract_profile",
        model="claude-opus-5-5",
        effort="medium",
        max_tokens=16_000,
        max_tokens_retry=32_000,
        deadline_s=900,
        schema=WireSchema.from_model(GatewayOut),
    )
    queries = node_config(
        "propose_queries", model_policy=policy, execution=execution, schema=WireSchema.from_model(GatewayOut)
    )
    assert (queries.max_tokens, queries.max_tokens_retry, queries.deadline_s) == (8_000, 16_000, 300)


def test_node_config_refuses_a_policy_without_a_supported_effort() -> None:
    execution = SEED["scoring"]["execution"]
    no_effort: dict[str, JSONValue] = {"nodes": {"extract_profile": {"model": "claude-opus-5-5"}}}
    with pytest.raises(ValueError, match="no effort"):
        node_config(
            "extract_profile",
            model_policy=no_effort,
            execution=execution,
            schema=WireSchema.from_model(GatewayOut),
        )
    bad_effort: dict[str, JSONValue] = {
        "nodes": {"extract_profile": {"model": "claude-opus-5-5", "effort": "extreme"}}
    }
    with pytest.raises(ValueError, match="does not support effort"):
        node_config(
            "extract_profile",
            model_policy=bad_effort,
            execution=execution,
            schema=WireSchema.from_model(GatewayOut),
        )
    unknown: dict[str, JSONValue] = {
        "nodes": {"extract_profile": {"model": "claude-opus-4", "effort": "low"}}
    }
    with pytest.raises(ValueError, match="unknown model"):
        node_config(
            "extract_profile",
            model_policy=unknown,
            execution=execution,
            schema=WireSchema.from_model(GatewayOut),
        )


async def test_the_input_bound_raises_unencodable_without_the_encoding_error(tmp_path: Path) -> None:
    cfg = fake_llm_config(FAKE_LLM_DIR, tmp_path / "no-secrets")
    async with make_http_client(cfg) as client:
        gw = LLMGateway(cast(AsyncEngine, _NoEngine()), cfg, http_client=client, cache=CacheMonitor())
        ctx = gateway.CallContext(
            owner=EvalRunOwner(1), purpose_class="nonscoring", execution_config_id=1, deadline=0.0
        )
        with pytest.raises(InputUnbounded) as refused:
            await gw._input_bound(GATEWAY_NODE, gateway_prompt("half a pair: \ud800"), ctx)
    assert refused.value.reason == "unencodable"
    assert refused.value.__cause__ is None and refused.value.__context__ is None


# --- Task 23: a worker without the `anthropic_key` secret and not in fake mode ------------------------------


def _keyless_config(tmp_path: Path, secrets: dict[str, bytes] | None = None) -> Any:
    from jobhunter.config import load_config

    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    for name, value in (secrets or {}).items():
        (secrets_dir / name).write_bytes(value)
        (secrets_dir / name).chmod(0o440)
    env = {"JOBHUNTER_ENV": "test", "JOBHUNTER_DB_NAME": "jobhunter"}  # no JOBHUNTER_FAKE_LLM
    return load_config("worker", env, secrets_dir)


def _sends_nothing(request: Any) -> Any:
    raise AssertionError(f"the gateway sent a request to {request.url}")


@pytest.mark.parametrize(
    "secrets", [None, {"anthropic_key": b""}, {"anthropic_key": b" \n"}], ids=["absent", "empty", "blank"]
)
@pytest.mark.parametrize("native_pdf", [False, True], ids=["text", "native_pdf"])
async def test_a_gateway_without_a_key_raises_not_configured_before_any_reservation(
    tmp_path: Path, secrets: dict[str, bytes] | None, native_pdf: bool
) -> None:
    """It is built (a worker without a key starts), and every call raises `LLMNotConfigured` first: before
    the input bound (a native PDF's `count_tokens` included), any transaction or any request."""
    import httpx2
    from jobhunter.llm.errors import LLMNotConfigured

    cfg = _keyless_config(tmp_path, secrets)
    assert not cfg.fake_llm and not cfg.secrets.get("anthropic_key", b"").strip()
    async with make_http_client(cfg, mount=httpx2.MockTransport(_sends_nothing)) as client:
        gw = LLMGateway(cast(AsyncEngine, _NoEngine()), cfg, http_client=client, cache=CacheMonitor())
        assert gw.configured is False
        ctx = gateway.CallContext(
            owner=EvalRunOwner(1),
            purpose_class="nonscoring",
            execution_config_id=1,
            deadline=time.monotonic() + 900,
            eval_run_id=1,
            native_pdf=native_pdf,
        )
        with pytest.raises(LLMNotConfigured) as refused:
            await gw.call(GATEWAY_NODE, gateway_prompt(), ctx)
        with pytest.raises(LLMNotConfigured):
            await gw.count_tokens(GATEWAY_NODE.model, {"messages": []})
        with pytest.raises(LLMNotConfigured):
            gw.count_body(GATEWAY_NODE, gateway_prompt())
    assert str(refused.value) == "the LLM gateway is not configured: the worker has no anthropic_key secret"


async def test_a_gateway_with_the_key_or_in_fake_mode_is_configured(tmp_path: Path) -> None:
    import httpx2

    (tmp_path / "keyed").mkdir()
    keyed = _keyless_config(tmp_path / "keyed", {"anthropic_key": b"unit-test-key-never-sent\n"})
    fake_cfg = fake_llm_config(FAKE_LLM_DIR, tmp_path / "no-secrets")
    for cfg in (keyed, fake_cfg):
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(_sends_nothing)) as client:
            gw = LLMGateway(cast(AsyncEngine, _NoEngine()), cfg, http_client=client, cache=CacheMonitor())
            assert gw.configured is True
