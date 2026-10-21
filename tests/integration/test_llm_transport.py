"""The gateway client factory on a real socket (M2 Task 4, spike V5 local part).

`LocalSSEServer` (tests/support/llm.py) listens on `127.0.0.1:0`. The client reaches it through a transport
mounted on `https://api.anthropic.com`, so the base URL stays the constant and the request still names the
API host. The read timeouts here are real socket timeouts: the server stops writing and the client's own
idle read timeout fires.
"""

import json
import time
from typing import Any

import anthropic
import httpx2
import pytest
from jobhunter.config import AppConfig, load_config
from jobhunter.llm import transport
from jobhunter.llm.gateway import GatewayChatAnthropic, _chat
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import SecretStr, ValidationError

from tests.support.llm import LocalSSEServer, synthetic_sse

pytestmark = pytest.mark.integration

KEY = SecretStr("fake-anthropic-key-0001")
SCHEMA: dict[str, Any] = {
    "title": "probe",
    "type": "object",
    "additionalProperties": False,
    "required": ["answer"],
    "properties": {"answer": {"type": "string", "enum": ["yes", "no"]}},
}
MESSAGES = [SystemMessage("Answer the synthetic probe."), HumanMessage("Is this a probe?")]
STREAM = synthetic_sse({"answer": "yes"}, model="claude-opus-5-5")


def _config(tmp_path: Any, env: str = "test") -> AppConfig:
    return load_config("worker", {"JOBHUNTER_ENV": env, "JOBHUNTER_DB_NAME": "x"}, tmp_path / "no-secrets")


async def test_chat_reaches_the_server_through_the_api_origin_with_the_policy_payload(tmp_path: Any) -> None:
    async with LocalSSEServer(STREAM) as server:
        async with transport.make_http_client(_config(tmp_path), mount=server.transport()) as client:
            runnable = _chat(
                "claude-opus-5-5",
                "medium",
                1234,
                SCHEMA,
                api_key=KEY,
                http_client=client,
                purpose="extract_profile",
            )
            out = await runnable.ainvoke(MESSAGES)
    assert out["parsed"] == {"answer": "yes"} and out["parsing_error"] is None
    (request,) = server.requests
    assert (request.method, request.path, request.headers["host"]) == (
        "POST",
        "/v1/messages",
        "api.anthropic.com",
    )
    assert request.headers["x-api-key"] == KEY.get_secret_value()
    assert request.headers["x-jh-purpose"] == "extract_profile"
    body = json.loads(request.body)
    assert body["stream"] is True and body["model"] == "claude-opus-5-5" and body["max_tokens"] == 1234
    assert body["output_config"]["format"] == {
        "type": "json_schema",
        "schema": anthropic.transform_schema(SCHEMA),
    }
    assert body["output_config"]["effort"] == "medium"
    assert not {"tool_choice", "tools", "temperature", "top_p", "top_k", "betas", "fallbacks"} & set(body)
    assert body["messages"][-1]["role"] == "user"


async def test_the_base_url_and_key_ignore_the_anthropic_environment(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # In this dev container ANTHROPIC_BASE_URL points at another service: the explicit arguments must win.
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("ANTHROPIC_API_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key-must-not-be-sent")
    async with LocalSSEServer(STREAM) as server:
        async with transport.make_http_client(_config(tmp_path), mount=server.transport()) as client:
            await _chat("claude-opus-5-5", "low", 100, SCHEMA, api_key=KEY, http_client=client).ainvoke(
                MESSAGES
            )
    (request,) = server.requests
    assert request.headers["host"] == "api.anthropic.com"
    assert request.headers["x-api-key"] == KEY.get_secret_value()


async def test_an_injected_one_second_read_timeout_fires_on_a_stalled_stream(tmp_path: Any) -> None:
    cfg = _config(tmp_path)
    async with LocalSSEServer(STREAM, stall_after_first_event=True) as server:
        async with transport.make_http_client(cfg, read_timeout_s=1.0, mount=server.transport()) as client:
            runnable = _chat("claude-opus-5-5", "low", 100, SCHEMA, api_key=KEY, http_client=client)
            started = time.monotonic()
            with pytest.raises(httpx2.ReadTimeout):  # mid-stream: the SDK does not wrap it (V5.md)
                await runnable.ainvoke(MESSAGES)
            elapsed = time.monotonic() - started
    assert 0.5 <= elapsed <= 1.5
    assert len(server.requests) == 1 and server.first_event_sent


async def test_the_sdk_sends_its_own_timeout_with_each_request(tmp_path: Any) -> None:
    """The SDK client's `timeout` is applied to each request and wins over the http client's (V5.md)."""
    async with LocalSSEServer(STREAM, stall_after_first_event=True) as server:
        async with transport.make_http_client(
            _config(tmp_path), read_timeout_s=30.0, mount=server.transport()
        ) as client:
            llm = GatewayChatAnthropic(
                model="claude-opus-5-5",
                base_url=transport.ANTHROPIC_BASE_URL,
                api_key=KEY,
                max_tokens=100,
                reasoning_effort="low",
                streaming=True,
                max_retries=0,
                timeout=transport.http_timeout(1.0),
                http_client=client,
            )
            runnable = llm.with_structured_output(SCHEMA, method="json_schema", include_raw=True)
            started = time.monotonic()
            with pytest.raises(httpx2.ReadTimeout):
                await runnable.ainvoke(MESSAGES)
    assert time.monotonic() - started <= 1.5


async def test_a_stall_before_the_response_headers_is_an_api_timeout_error(tmp_path: Any) -> None:
    async with LocalSSEServer(STREAM, stall_before_headers=True) as server:
        async with transport.make_http_client(
            _config(tmp_path), read_timeout_s=1.0, mount=server.transport()
        ) as client:
            runnable = _chat("claude-opus-5-5", "low", 100, SCHEMA, api_key=KEY, http_client=client)
            started = time.monotonic()
            with pytest.raises(anthropic.APITimeoutError):  # before the headers: the SDK wraps it
                await runnable.ainvoke(MESSAGES)
    assert 0.5 <= time.monotonic() - started <= 1.5


@pytest.mark.slow
async def test_v5_the_production_idle_read_timeout_fires_at_120_s(tmp_path: Any) -> None:
    async with LocalSSEServer(STREAM, stall_after_first_event=True) as server:
        async with transport.make_http_client(_config(tmp_path), mount=server.transport()) as client:
            runnable = _chat("claude-opus-5-5", "low", 100, SCHEMA, api_key=KEY, http_client=client)
            started = time.monotonic()
            with pytest.raises(httpx2.ReadTimeout):
                await runnable.ainvoke(MESSAGES)
            elapsed = time.monotonic() - started
    assert 115 <= elapsed <= 125, elapsed  # spec §22 V5: 120 s ± 5


# --- the client factory ------------------------------------------------------------------------------------


async def test_the_http_client_carries_the_one_timeout_with_only_the_read_value_injectable(
    tmp_path: Any,
) -> None:
    assert transport.TIMEOUT == anthropic.Timeout(connect=10, read=120, write=30, pool=10)
    async with transport.make_http_client(_config(tmp_path)) as client:
        assert client.timeout == transport.TIMEOUT
    async with transport.make_http_client(_config(tmp_path), read_timeout_s=2.0) as client:
        assert client.timeout == anthropic.Timeout(connect=10, read=2.0, write=30, pool=10)


async def test_prod_refuses_an_injected_read_timeout_or_transport(tmp_path: Any) -> None:
    cfg = _config(tmp_path, env="prod")
    with pytest.raises(ValueError, match="tests only"):
        transport.make_http_client(cfg, read_timeout_s=2.0)
    with pytest.raises(ValueError, match="tests only"):
        transport.make_http_client(cfg, mount=httpx2.MockTransport(lambda request: httpx2.Response(500)))
    async with transport.make_http_client(cfg) as client:
        assert client.timeout == transport.TIMEOUT


async def test_chat_refuses_an_empty_key_or_a_non_positive_max_tokens(tmp_path: Any) -> None:
    async with transport.make_http_client(_config(tmp_path)) as client:
        with pytest.raises(ValueError, match="api_key"):
            _chat("claude-opus-5-5", "low", 100, SCHEMA, api_key=SecretStr(""), http_client=client)
        with pytest.raises(ValueError, match="max_tokens"):
            _chat("claude-opus-5-5", "low", 0, SCHEMA, api_key=KEY, http_client=client)


def test_v5_chat_anthropic_alone_takes_neither_the_sdk_timeout_nor_the_http_client() -> None:
    """Why `GatewayChatAnthropic` exists (G3, V5.md): the plain class types `timeout` as a float, and moves an
    `http_client` into `model_kwargs`, which it would send in the request body."""
    base = GatewayChatAnthropic.__base__
    assert base is not None and (base.__module__, base.__name__) == (
        "langchain_anthropic.chat_models",
        "ChatAnthropic",
    )
    common: dict[str, Any] = {
        "model": "claude-opus-5-5",
        "base_url": transport.ANTHROPIC_BASE_URL,
        "api_key": KEY,
    }
    with pytest.raises(ValidationError, match="float_type"):
        base(**common, timeout=transport.TIMEOUT)
    with pytest.warns(UserWarning, match="http_client was transferred to model_kwargs"):
        llm = base(**common, http_client=object())
    assert "http_client" in llm.model_kwargs
