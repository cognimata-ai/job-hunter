"""llm/fake.py (M2 Task 4): the recorded-SSE fake transport, with the real `_chat` stack above it.

The stored streams are the two V13 recordings (`propose_queries`, Sonnet and Opus, the §11.12 schema literal
of `scripts/spikes/v13_structured_stream.py`). Every test runs offline: the fake mode client has no route to
the network (`test_fake_mode_never_reaches_the_network`).
"""

import importlib.util
import json
import time
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import anthropic
import httpx2
import pytest
from jobhunter.llm import fake
from jobhunter.llm.budget import Purpose
from jobhunter.llm.gateway import _chat
from jobhunter.llm.pricing import MODELS, Model
from jobhunter.llm.transport import ANTHROPIC_BASE_URL
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import SecretStr

from tests.support.llm import FakeAnthropic

REPO = Path(__file__).resolve().parents[2]
KEY = SecretStr("fake-anthropic-key-0001")  # visibly fake, shaped like no provider key


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts/spikes" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


V13 = _load("v13_structured_stream")
V13_MODELS = sorted(V13.MODELS.values())


def _query_items(parsed: Any) -> list[dict[str, Any]]:
    assert isinstance(parsed, dict) and isinstance(parsed["queries"], list)
    items: list[dict[str, Any]] = parsed["queries"]
    return items


def _structurally_valid(item: Any) -> bool:
    """One §11.12 item as the schema literal types it (no semantic limit)."""
    return (
        isinstance(item, dict)
        and set(item) == {"text", "lang", "target_title", "exploration", "rationale"}
        and all(isinstance(item[k], str) for k in ("text", "lang", "target_title", "rationale"))
        and isinstance(item["exploration"], bool)
    )


async def _ask(
    fake_anthropic: FakeAnthropic,
    model: Model,
    *,
    read_timeout_s: float = 120.0,
    purpose: Purpose = V13.PURPOSE,
    max_tokens: int = V13.MAX_TOKENS,
) -> dict[str, Any]:
    """One structured call through `_chat` on the fake, as V13 made it."""
    async with fake_anthropic.client(read_timeout_s=read_timeout_s) as client:
        runnable = _chat(
            model,
            V13.EFFORT,
            max_tokens,
            V13.SCHEMA,
            api_key=KEY,
            http_client=client,
            purpose=purpose,
        )
        out = await runnable.ainvoke([SystemMessage(V13.SYSTEM), HumanMessage(V13.USER)])
    assert isinstance(out, dict)
    return out


def _causes(exc: BaseException) -> Iterator[BaseException]:
    cause = exc.__cause__
    while cause is not None:
        yield cause
        cause = cause.__cause__


def _raw(out: dict[str, Any]) -> AIMessage:
    raw = out["raw"]
    assert isinstance(raw, AIMessage)
    return raw


def _messages_body(model: str, *, max_tokens: int = V13.MAX_TOKENS) -> dict[str, Any]:
    """A `/v1/messages` body that selects the V13 recording of `model`, for the HTTP-level tests."""
    return {
        "model": model,
        "max_tokens": max_tokens,
        "stream": True,
        "messages": [{"role": "user", "content": V13.USER}],
        "output_config": {
            "effort": V13.EFFORT,
            "format": {"type": "json_schema", "schema": anthropic.transform_schema(V13.SCHEMA)},
        },
    }


@pytest.fixture
def recorded(fake_anthropic: FakeAnthropic) -> Iterator[dict[str, str]]:
    """The stored `ok` text of each V13 model (the concatenated text deltas of its recording)."""
    texts = {}
    for model in V13_MODELS:
        path = fake.recording_path(
            fake_anthropic.root, V13.PURPOSE, model, anthropic.transform_schema(V13.SCHEMA)
        )
        texts[model] = fake.text_of(fake.parse_sse(path.read_bytes()))
    yield texts


# --- the stored recordings ---------------------------------------------------------------------------------


@pytest.mark.parametrize("model", V13_MODELS)
def test_each_v13_recording_is_stored_with_its_sidecars(fake_anthropic: FakeAnthropic, model: Model) -> None:
    schema = anthropic.transform_schema(V13.SCHEMA)
    path = fake.recording_path(fake_anthropic.root, V13.PURPOSE, model, schema)
    assert path == fake_anthropic.root / V13.PURPOSE / model / f"{fake.schema12(schema)}.sse"
    components = json.loads(path.with_suffix(".components.json").read_text("utf-8"))
    assert (components["source"], components["spike"], components["model"]) == ("recorded", "v13", model)
    assert components["component_bytes"]["schema"] == len(fake.canonical_schema_json(schema).encode())
    headers = json.loads(path.with_suffix(".headers.json").read_text("utf-8"))
    assert headers["status"] == 200
    assert not {"request-id", "anthropic-organization-id", "cf-ray"} & {h.lower() for h in headers["headers"]}
    events = fake.parse_sse(path.read_bytes())
    assert events[0].name == "message_start" and events[-1].name == "message_stop"


def test_the_schema_key_is_the_canonical_json_sha256_prefix() -> None:
    a = {"type": "object", "properties": {"b": {"type": "string"}, "a": {"type": "integer"}}}
    b = {"properties": {"a": {"type": "integer"}, "b": {"type": "string"}}, "type": "object"}
    assert fake.schema12(a) == fake.schema12(b) and len(fake.schema12(a)) == 12
    assert fake.schema12(a) != fake.schema12({**a, "required": ["a"]})


# --- the per-try names -------------------------------------------------------------------------------------


@pytest.mark.parametrize("model", V13_MODELS)
async def test_ok_serves_the_recorded_stream(fake_anthropic: FakeAnthropic, model: Model) -> None:
    out = await _ask(fake_anthropic, model)
    raw = _raw(out)
    assert out["parsing_error"] is None
    items = _query_items(out["parsed"])
    assert items and all(_structurally_valid(item) for item in items)
    assert raw.response_metadata["stop_reason"] == "end_turn"
    assert raw.response_metadata["model_name"] == model
    assert fake_anthropic.served == 1 and [r.scenario for r in fake_anthropic.requests] == ["ok"]
    (served,) = fake_anthropic.requests
    assert (served.purpose, served.model, served.status) == (V13.PURPOSE, model, 200)


async def test_rate_limited_is_a_429_with_retry_after(fake_anthropic: FakeAnthropic) -> None:
    fake_anthropic.scenario("rate_limited")
    with pytest.raises(anthropic.RateLimitError) as exc:
        await _ask(fake_anthropic, V13_MODELS[0])
    assert exc.value.status_code == 429 and exc.value.response.headers["retry-after"] == "1"
    assert exc.value.response.headers["request-id"].startswith("req_fake_")
    assert fake_anthropic.served == 1


async def test_overloaded_529_is_a_529_overloaded_error(fake_anthropic: FakeAnthropic) -> None:
    fake_anthropic.scenario("overloaded_529")
    with pytest.raises(anthropic.APIStatusError) as exc:
        await _ask(fake_anthropic, V13_MODELS[0])
    assert exc.value.status_code == 529 and isinstance(exc.value, anthropic.OverloadedError)


async def test_http_401_is_an_authentication_error(fake_anthropic: FakeAnthropic) -> None:
    fake_anthropic.scenario("http_401")
    with pytest.raises(anthropic.AuthenticationError) as exc:
        await _ask(fake_anthropic, V13_MODELS[0])
    assert exc.value.status_code == 401


async def test_connect_refused_fails_before_any_response(fake_anthropic: FakeAnthropic) -> None:
    fake_anthropic.scenario("connect_refused")
    with pytest.raises(anthropic.APIConnectionError) as exc:
        await _ask(fake_anthropic, V13_MODELS[0])
    assert not isinstance(exc.value, anthropic.APITimeoutError)
    # langchain re-raises the SDK error as its own subclass `from` it; the SDK's cause is the transport's
    assert any(isinstance(cause, httpx2.ConnectError) for cause in _causes(exc.value))
    assert [(r.scenario, r.status) for r in fake_anthropic.requests] == [("connect_refused", None)]


async def test_stall_after_first_event_sends_one_message_start_and_then_nothing(
    fake_anthropic: FakeAnthropic,
) -> None:
    fake_anthropic.scenario("stall_after_first_event")
    received = b""
    async with fake_anthropic.client(read_timeout_s=0.3) as client:
        async with client.stream(
            "POST",
            f"{ANTHROPIC_BASE_URL}/v1/messages",
            json=_messages_body(V13_MODELS[0]),
            headers={fake.PURPOSE_HEADER: V13.PURPOSE},
        ) as response:
            assert response.status_code == 200
            started = time.monotonic()
            with pytest.raises(httpx2.ReadTimeout):
                async for chunk in response.aiter_bytes():
                    received += chunk
            waited = time.monotonic() - started
    assert [event.name for event in fake.parse_sse(received)] == ["message_start"]
    assert 0.2 <= waited <= 1.0  # the request's own read timeout (0.3 s), as a socket read would time out


async def test_stall_after_first_event_times_the_structured_call_out(fake_anthropic: FakeAnthropic) -> None:
    fake_anthropic.scenario("stall_after_first_event")
    started = time.monotonic()
    with pytest.raises(httpx2.ReadTimeout):
        await _ask(fake_anthropic, V13_MODELS[0], read_timeout_s=0.3)
    assert time.monotonic() - started <= 1.5


@pytest.mark.parametrize("model", V13_MODELS)
async def test_max_tokens_stops_at_the_requested_max_tokens_with_partial_text(
    fake_anthropic: FakeAnthropic, recorded: dict[str, str], model: Model
) -> None:
    fake_anthropic.scenario("max_tokens")
    raw = _raw(await _ask(fake_anthropic, model, max_tokens=777))
    assert raw.response_metadata["stop_reason"] == "max_tokens"
    assert raw.usage_metadata is not None and raw.usage_metadata["output_tokens"] == 777
    text = raw.text
    assert text and len(text) < len(recorded[model]) and recorded[model].startswith(text)


async def test_refusal_stops_with_a_category_and_no_text(fake_anthropic: FakeAnthropic) -> None:
    fake_anthropic.scenario("refusal")
    raw = _raw(await _ask(fake_anthropic, V13_MODELS[0]))
    assert raw.response_metadata["stop_reason"] == "refusal"
    assert raw.response_metadata["stop_details"]["category"] == fake.REFUSAL_CATEGORY
    assert raw.response_metadata["stop_details"]["type"] == "refusal"
    assert raw.text == ""


@pytest.mark.parametrize("model", V13_MODELS)
async def test_model_mismatch_serves_another_model(fake_anthropic: FakeAnthropic, model: Model) -> None:
    fake_anthropic.scenario("model_mismatch")
    out = await _ask(fake_anthropic, model)
    served = _raw(out).response_metadata["model_name"]
    assert served != model and served in MODELS
    assert out["parsing_error"] is None  # the content is the stored one: only the served model differs


async def test_parse_error_ends_the_turn_with_text_that_does_not_parse(fake_anthropic: FakeAnthropic) -> None:
    fake_anthropic.scenario("parse_error")
    out = await _ask(fake_anthropic, V13_MODELS[0])
    assert _raw(out).response_metadata["stop_reason"] == "end_turn"
    assert out["parsed"] is None and out["parsing_error"] is not None


# --- the content variants ----------------------------------------------------------------------------------


def _leaves(doc: Any, path: tuple[Any, ...] = ()) -> Iterator[tuple[tuple[Any, ...], Any]]:
    if isinstance(doc, dict):
        for key, value in doc.items():
            yield from _leaves(value, (*path, key))
    elif isinstance(doc, list):
        for i, value in enumerate(doc):
            yield from _leaves(value, (*path, i))
    else:
        yield path, doc


@pytest.mark.parametrize("model", V13_MODELS)
async def test_enum_stripped_puts_one_enum_field_outside_its_enum(
    fake_anthropic: FakeAnthropic, recorded: dict[str, str], model: Model
) -> None:
    fake_anthropic.scenario("enum_stripped")
    out = await _ask(fake_anthropic, model)
    stored = dict(_leaves(json.loads(recorded[model])))
    changed = {path: value for path, value in _leaves(out["parsed"]) if stored.get(path) != value}
    allowed = V13.SCHEMA["properties"]["queries"]["items"]["properties"]["lang"]["enum"]
    assert len(changed) == 1
    ((path, value),) = changed.items()
    assert path[0] == "queries" and path[-1] == "lang" and value not in allowed
    assert _raw(out).response_metadata["stop_reason"] == "end_turn"


@pytest.mark.parametrize("model", V13_MODELS)
async def test_set_invalid_is_a_structurally_valid_queries_body_with_five_items(
    fake_anthropic: FakeAnthropic, model: Model
) -> None:
    fake_anthropic.scenario("set_invalid")
    out = await _ask(fake_anthropic, model)
    items = _query_items(out["parsed"])
    assert len(items) == 5 and all(_structurally_valid(item) for item in items)
    assert len({item["text"] for item in items}) == 5


# --- sequences ---------------------------------------------------------------------------------------------


async def test_a_sequence_takes_the_next_name_per_try_and_repeats_the_last(
    fake_anthropic: FakeAnthropic,
) -> None:
    fake_anthropic.scenario("rate_limited", "ok")
    with pytest.raises(anthropic.RateLimitError):
        await _ask(fake_anthropic, V13_MODELS[0])
    for _ in range(2):  # the last name repeats
        assert _query_items((await _ask(fake_anthropic, V13_MODELS[0]))["parsed"])
    assert [r.scenario for r in fake_anthropic.requests] == ["rate_limited", "ok", "ok"]


async def test_a_single_name_repeats_on_every_try(fake_anthropic: FakeAnthropic) -> None:
    fake_anthropic.scenario("overloaded_529")
    for _ in range(3):
        with pytest.raises(anthropic.OverloadedError):
            await _ask(fake_anthropic, V13_MODELS[0])
    assert [r.scenario for r in fake_anthropic.requests] == ["overloaded_529"] * 3


async def test_a_new_scenario_starts_its_sequence_from_the_first_name(fake_anthropic: FakeAnthropic) -> None:
    fake_anthropic.scenario("rate_limited", "ok")
    with pytest.raises(anthropic.RateLimitError):
        await _ask(fake_anthropic, V13_MODELS[0])
    fake_anthropic.scenario("http_401", "ok")
    with pytest.raises(anthropic.AuthenticationError):
        await _ask(fake_anthropic, V13_MODELS[0])


def test_an_unknown_or_empty_scenario_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown fake LLM scenario"):
        fake.scenario("ok", "rate_limited_then_ok")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="at least one"):
        fake.scenario()


def test_without_a_scenario_every_try_is_ok() -> None:
    assert fake.state().script == ("ok",)


def test_fresh_state_restores_the_previous_one() -> None:
    outer = fake.scenario("refusal")
    with fake.fresh_state() as inner:
        assert fake.state() is inner and inner.script == ("ok",)
        fake.scenario("http_401")
    assert fake.state() is outer and outer.script == ("refusal",)


# --- count_tokens ------------------------------------------------------------------------------------------


async def _count(fake_anthropic: FakeAnthropic) -> httpx2.Response:
    async with fake_anthropic.client() as client:
        return await client.post(
            f"{ANTHROPIC_BASE_URL}/v1/messages/count_tokens",
            json={"model": V13_MODELS[0], "messages": [{"role": "user", "content": V13.USER}]},
        )


async def test_count_tokens_answers_the_count_set_for_the_test(fake_anthropic: FakeAnthropic) -> None:
    fake_anthropic.count_tokens(1800)
    response = await _count(fake_anthropic)
    assert response.status_code == 200 and response.json() == {"input_tokens": 1800}
    assert fake_anthropic.state.count_tokens_requests == 1
    assert fake_anthropic.served == 0  # a count is no message try


async def test_count_tokens_error_is_an_error_response(fake_anthropic: FakeAnthropic) -> None:
    fake_anthropic.count_tokens("error")
    response = await _count(fake_anthropic)
    assert response.status_code >= 400 and response.json()["type"] == "error"
    assert fake_anthropic.served == 0


async def test_count_tokens_without_an_answer_is_an_error_response(fake_anthropic: FakeAnthropic) -> None:
    response = await _count(fake_anthropic)
    assert response.status_code == 404 and "count_tokens" in response.json()["error"]["message"]


# --- what the fake refuses ---------------------------------------------------------------------------------


async def test_a_request_without_a_recording_is_a_404(fake_anthropic: FakeAnthropic) -> None:
    with pytest.raises(anthropic.NotFoundError):
        await _ask(fake_anthropic, V13_MODELS[0], purpose="extract_profile")  # no such recording
    assert [(r.scenario, r.status) for r in fake_anthropic.requests] == [("ok", 404)]


@pytest.mark.parametrize("purpose", ["../propose_queries", "propose queries", ""])
async def test_a_purpose_that_is_no_directory_name_is_a_404(
    fake_anthropic: FakeAnthropic, purpose: str
) -> None:
    async with fake_anthropic.client() as client:
        response = await client.post(
            f"{ANTHROPIC_BASE_URL}/v1/messages",
            json=_messages_body(V13_MODELS[0]),
            headers={fake.PURPOSE_HEADER: purpose},
        )
    assert response.status_code == 404


async def test_fake_mode_never_reaches_the_network(fake_anthropic: FakeAnthropic) -> None:
    async with fake_anthropic.client() as client:
        with pytest.raises(httpx2.ConnectError, match="fake LLM mode"):
            await client.get("https://example.com/")
        with pytest.raises(httpx2.ConnectError, match="fake LLM mode"):
            await client.get("http://api.anthropic.com/v1/messages")
