"""The recorded-SSE fake of the Anthropic Messages API (plan Task 4; `JOBHUNTER_FAKE_LLM`).

`RecordedSSETransport(root)` is an `httpx2` transport that `transport.make_http_client` mounts on the API
origin in fake mode. The real gateway code runs above it: the SDK, langchain's streaming and the structured
output parser see the same bytes as from the API.

A `POST /v1/messages` is answered from `root/<purpose>/<model>/<schema12>.sse`, chosen by:
- the `X-JH-Purpose` request header (`PURPOSE_HEADER`, which `gateway._chat` sends);
- the request's `model`;
- `schema12(output_config.format.schema)`: the first 12 hex digits of the SHA-256 of the schema as canonical
  JSON (`canonical_schema_json`).

Next to a stream, `<schema12>.headers.json` (`{"status", "headers"}`, optional) holds its response headers and
`<schema12>.components.json` the byte size of each rendered component of the request it answered (written by
`scripts/spikes/record_anthropic.py` for a recording; `"source": "recorded"` or `"synthetic"`). A request
without a recording, or whose purpose or model is no plain directory name, is answered 404 `not_found_error`.

Each HTTP try takes the next name of the current `scenario(*names)` (the last one repeats; `ok` is the stored
stream; without a scenario every try is `ok`):
- `ok`: the stored stream, byte for byte;
- `rate_limited` (429, `retry-after: 1`), `overloaded_529` (529), `http_401` (401): an error response;
- `connect_refused`: `httpx2.ConnectError` before any response;
- `stall_after_first_event`: the stored `message_start` event, then nothing; after the request's own read
  timeout (the per-request timeout the SDK sends) the stream raises `httpx2.ReadTimeout`, as a socket read
  would;
- `max_tokens`: the stored text cut to its first half, `stop_reason='max_tokens'` and `output_tokens` equal to
  the request's `max_tokens`;
- `refusal`: no content, `stop_reason='refusal'` and `stop_details.category = REFUSAL_CATEGORY`;
- `model_mismatch`: the stored stream served by another model of `pricing.MODELS`;
- `parse_error`: `stop_reason='end_turn'` with text that is no JSON;
- `enum_stripped`: the stored body with its first enum-constrained field holding `OUT_OF_ENUM`, as if the API
  had stripped the constraint from the schema;
- `set_invalid`: a structurally valid queries body (§11.12) with `SET_INVALID_ITEMS` (5) items.
`count_tokens(result)` sets the answer of `POST /v1/messages/count_tokens`, a JSON endpoint: `{"input_tokens":
result}`, or a 500 `api_error` for `"error"`; without an answer it is a 404. A count is no message try.

The state of a test (its scenario, the count answer, the requests served) lives in a context variable;
`fresh_state()` gives a block its own. Outside any (a dev worker in fake mode) a process-wide state counts the
tries without keeping the requests.
"""

import asyncio
import hashlib
import json
import re
from collections.abc import AsyncIterator, Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Final, Literal, get_args

import httpx2

from jobhunter.llm.pricing import MODELS

PURPOSE_HEADER: Final = "X-JH-Purpose"

Scenario = Literal[
    "ok",
    "rate_limited",
    "overloaded_529",
    "stall_after_first_event",
    "max_tokens",
    "refusal",
    "model_mismatch",
    "http_401",
    "connect_refused",
    "parse_error",
    "enum_stripped",
    "set_invalid",
]
SCENARIOS: Final[tuple[Scenario, ...]] = get_args(Scenario)

REFUSAL_CATEGORY: Final = "general_harms"
OUT_OF_ENUM: Final = "__not_in_enum__"
SET_INVALID_ITEMS: Final = 5
PARSE_ERROR_TEXT: Final = "This answer is not JSON."
# What a committed stream keeps of a thinking block (thinking content is never stored, G6).
THINKING_OMITTED: Final = "[thinking omitted]"
SIGNATURE_OMITTED: Final = "[signature omitted]"

_NAME: Final = re.compile(r"[a-z][a-z0-9_]{0,63}")
_MODEL: Final = re.compile(r"[a-z0-9][a-z0-9.-]{0,63}")
_SSE_CONTENT_TYPE: Final = "text/event-stream; charset=utf-8"


# --- the stream format -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SSEEvent:
    """One server-sent event: its `event:` name and its `data:` JSON object."""

    name: str
    data: dict[str, Any]


def parse_sse(body: bytes) -> list[SSEEvent]:
    """The events of an SSE body; comment lines and events without data are skipped."""
    events: list[SSEEvent] = []
    for block in body.decode("utf-8").replace("\r\n", "\n").split("\n\n"):
        name, data = "message", []
        for line in block.split("\n"):
            if line.startswith("event:"):
                name = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data.append(line[len("data:") :].removeprefix(" "))
        if data:
            parsed = json.loads("\n".join(data))
            if not isinstance(parsed, dict):
                raise ValueError(f"an SSE {name} event's data is no JSON object")
            events.append(SSEEvent(name, parsed))
    return events


def render_sse(events: Iterable[SSEEvent]) -> bytes:
    """The SSE body of `events`, one compact JSON `data:` line each."""
    return "".join(
        f"event: {e.name}\ndata: {json.dumps(e.data, ensure_ascii=False, separators=(',', ':'))}\n\n"
        for e in events
    ).encode("utf-8")


def _text_blocks(events: Iterable[SSEEvent]) -> set[int]:
    return {
        int(e.data["index"])
        for e in events
        if e.name == "content_block_start" and e.data.get("content_block", {}).get("type") == "text"
    }


def text_of(events: list[SSEEvent]) -> str:
    """The text the stream's text blocks carry, concatenated (thinking excluded)."""
    blocks = _text_blocks(events)
    parts: list[str] = []
    for e in events:
        if e.name == "content_block_start" and int(e.data["index"]) in blocks:
            parts.append(str(e.data["content_block"].get("text", "")))
        elif e.name == "content_block_delta" and int(e.data["index"]) in blocks:
            if e.data["delta"].get("type") == "text_delta":
                parts.append(str(e.data["delta"]["text"]))
    return "".join(parts)


def with_text(events: list[SSEEvent], text: str) -> list[SSEEvent]:
    """`events` with the text of their first text block replaced by `text` (one delta) and their other text
    blocks dropped."""
    blocks = sorted(_text_blocks(events))
    if not blocks:
        raise ValueError("the stream has no text block")
    first, out, placed = blocks[0], [], False
    for e in events:
        index = int(e.data.get("index", -1)) if e.name.startswith("content_block") else -1
        if index in blocks and index != first:
            continue
        if e.name == "content_block_start" and index == first:
            block = {**e.data["content_block"], "text": ""}
            out.append(replace(e, data={**e.data, "content_block": block}))
        elif e.name == "content_block_delta" and index == first:
            if not placed:
                out.append(replace(e, data={**e.data, "delta": {"type": "text_delta", "text": text}}))
                placed = True
        elif e.name == "content_block_stop" and index == first and not placed:
            delta = {
                "type": "content_block_delta",
                "index": first,
                "delta": {"type": "text_delta", "text": text},
            }
            out.extend([SSEEvent("content_block_delta", delta), e])
            placed = True
        else:
            out.append(e)
    return out


def _with_message_delta(events: list[SSEEvent], **delta: Any) -> list[SSEEvent]:
    """`events` with `delta` merged into the `message_delta` event's `delta`, and its `usage` updated by a
    `usage` entry of `delta`."""
    usage = delta.pop("usage", {})
    out = []
    for e in events:
        if e.name == "message_delta":
            data = {**e.data, "delta": {**e.data["delta"], **delta}, "usage": {**e.data["usage"], **usage}}
            e = replace(e, data=data)
        out.append(e)
    return out


# --- the recording key -------------------------------------------------------------------------------------


def canonical_schema_json(schema: Mapping[str, Any]) -> str:
    """A schema as canonical JSON: sorted keys, no whitespace, UTF-8 as is. Its byte length is the schema
    component of a request's input bound."""
    return json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def schema12(schema: Mapping[str, Any]) -> str:
    """The first 12 hex digits of the SHA-256 of `canonical_schema_json(schema)`."""
    return hashlib.sha256(canonical_schema_json(schema).encode("utf-8")).hexdigest()[:12]


def recording_path(root: Path, purpose: str, model: str, schema: Mapping[str, Any]) -> Path:
    """Where the stream of `(purpose, model, schema)` is stored under `root`; `schema` is the sent one
    (`output_config.format.schema`)."""
    return root / purpose / model / f"{schema12(schema)}.sse"


# --- the per-test state ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ServedRequest:
    """One message try as the fake received it; `status` is None when no response was given."""

    scenario: Scenario
    purpose: str | None
    model: str | None
    max_tokens: int | None
    status: int | None
    body: Mapping[str, Any] = field(repr=False)


@dataclass(eq=False)
class FakeLLMState:
    """The scenario, the `count_tokens` answer and the tries of one test (or of the process)."""

    record: bool = True  # keep each request (a test); the process-wide state only counts
    script: tuple[Scenario, ...] = ("ok",)
    tries: int = 0  # the message tries the current script has taken
    served: int = 0  # every message try that reached the fake, whatever its scenario
    requests: list[ServedRequest] = field(default_factory=list)
    count_tokens_answer: int | Literal["error"] | None = None
    count_tokens_requests: int = 0

    def take(self) -> Scenario:
        name = self.script[min(self.tries, len(self.script) - 1)]
        self.tries += 1
        self.served += 1
        return name


_PROCESS_STATE: Final = FakeLLMState(record=False)
_STATE: Final[ContextVar[FakeLLMState]] = ContextVar("jobhunter_fake_llm_state")


def state() -> FakeLLMState:
    """The current state: the innermost `fresh_state()`, else the process-wide one."""
    return _STATE.get(_PROCESS_STATE)


@contextmanager
def fresh_state() -> Iterator[FakeLLMState]:
    """A new state (every try `ok`, no count answer, no request) for the block; the previous one after it."""
    new = FakeLLMState()
    token = _STATE.set(new)
    try:
        yield new
    finally:
        _STATE.reset(token)


def scenario(*names: Scenario) -> FakeLLMState:
    """Start the ordered per-try sequence `names` in the current state; the last name repeats."""
    if not names:
        raise ValueError("a fake LLM scenario needs at least one name")
    if unknown := [name for name in names if name not in SCENARIOS]:
        raise ValueError(f"unknown fake LLM scenario {unknown}; known: {', '.join(SCENARIOS)}")
    current = state()
    current.script, current.tries = tuple(names), 0
    return current


def count_tokens(result: int | Literal["error"]) -> FakeLLMState:
    """Set the answer of `/v1/messages/count_tokens` in the current state."""
    if result != "error" and (not isinstance(result, int) or isinstance(result, bool) or result < 0):
        raise ValueError("count_tokens takes a non-negative int or 'error'")
    current = state()
    current.count_tokens_answer = result
    return current


# --- the transport -----------------------------------------------------------------------------------------


def _error(status: int, kind: str, message: str, request_id: str, **headers: str) -> httpx2.Response:
    body = {"type": "error", "error": {"type": kind, "message": message}, "request_id": request_id}
    return httpx2.Response(status, headers={"request-id": request_id, **headers}, json=body)


class _NoRecording(Exception):
    """The request selects no stored stream, or the scenario cannot be made from it."""


class _StalledStream(httpx2.AsyncByteStream):
    """`first`, then nothing: after `read_timeout_s` (None: never) it raises `httpx2.ReadTimeout`."""

    def __init__(self, first: bytes, read_timeout_s: float | None, request: httpx2.Request) -> None:
        self._first = first
        self._read_timeout_s = read_timeout_s
        self._request = request

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self._first
        if self._read_timeout_s is None:
            await asyncio.Event().wait()
        await asyncio.sleep(self._read_timeout_s or 0)
        raise httpx2.ReadTimeout("fake: no SSE event within the read timeout", request=self._request)


def _enum_paths(
    schema: Any, doc: Any, path: tuple[Any, ...], defs: Mapping[str, Any]
) -> Iterator[tuple[Any, ...]]:
    """The paths in `doc` of the string values whose schema node has an `enum`, in document order."""
    if not isinstance(schema, dict):
        return
    if isinstance(ref := schema.get("$ref"), str) and ref.startswith("#/$defs/"):
        schema = defs.get(ref.removeprefix("#/$defs/"), {})
    if "enum" in schema and isinstance(doc, str):
        yield path
        return
    for option in schema.get("anyOf", []):
        yield from _enum_paths(option, doc, path, defs)
    if isinstance(doc, dict):
        for key, value in doc.items():
            yield from _enum_paths(schema.get("properties", {}).get(key), value, (*path, key), defs)
    elif isinstance(doc, list):
        for i, value in enumerate(doc):
            yield from _enum_paths(schema.get("items"), value, (*path, i), defs)


def _set(doc: Any, path: tuple[Any, ...], value: Any) -> None:
    for step in path[:-1]:
        doc = doc[step]
    doc[path[-1]] = value


class RecordedSSETransport(httpx2.AsyncBaseTransport):
    """The fake Messages API over the recordings under `root` (see the module docstring)."""

    def __init__(self, root: Path) -> None:
        self._root = root

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        current = state()
        content = await request.aread()
        if request.method == "POST" and request.url.path == "/v1/messages/count_tokens":
            return self._count(current)
        if request.method != "POST" or request.url.path != "/v1/messages":
            return _error(
                404, "not_found_error", f"fake: no route {request.method} {request.url.path}", "req_fake"
            )
        body: dict[str, Any] = json.loads(content)
        name = current.take()
        request_id = f"req_fake_{current.served:04d}"
        purpose = request.headers.get(PURPOSE_HEADER)
        model = body.get("model") if isinstance(body.get("model"), str) else None
        max_tokens = body.get("max_tokens") if isinstance(body.get("max_tokens"), int) else None
        response: httpx2.Response | None = None
        try:
            if name == "connect_refused":
                raise httpx2.ConnectError("fake: connection refused", request=request)
            response = self._respond(name, purpose, model, body, request, request_id)
            return response
        finally:
            if current.record:
                status = None if response is None else response.status_code
                current.requests.append(ServedRequest(name, purpose, model, max_tokens, status, body))

    def _count(self, current: FakeLLMState) -> httpx2.Response:
        current.count_tokens_requests += 1
        answer = current.count_tokens_answer
        if answer is None:
            return _error(
                404, "not_found_error", "fake: no count_tokens answer was set (fake.count_tokens)", "req_fake"
            )
        if answer == "error":
            return _error(500, "api_error", "fake: count_tokens failed", "req_fake")
        return httpx2.Response(200, json={"input_tokens": answer})

    def _respond(
        self,
        name: Scenario,
        purpose: str | None,
        model: str | None,
        body: dict[str, Any],
        request: httpx2.Request,
        request_id: str,
    ) -> httpx2.Response:
        if name == "rate_limited":
            return _error(429, "rate_limit_error", "fake: rate limited", request_id, **{"retry-after": "1"})
        if name == "overloaded_529":
            return _error(529, "overloaded_error", "fake: overloaded", request_id)
        if name == "http_401":
            return _error(401, "authentication_error", "fake: invalid x-api-key", request_id)
        try:
            path, schema = self._locate(purpose, model, body)
            stored = path.read_bytes()
            status, headers = self._headers(path)
            headers["request-id"] = request_id
            if name == "ok":
                return httpx2.Response(status, headers=headers, content=stored)
            events = parse_sse(stored)
            if name == "stall_after_first_event":
                timeout = request.extensions.get("timeout", {}).get("read")
                stream = _StalledStream(render_sse(events[:1]), timeout, request)
                return httpx2.Response(status, headers=headers, stream=stream)
            return httpx2.Response(
                status, headers=headers, content=render_sse(self._variant(name, events, body, schema))
            )
        except _NoRecording as exc:
            return _error(404, "not_found_error", f"fake: {exc}", request_id)

    def _locate(self, purpose: str | None, model: str | None, body: Mapping[str, Any]) -> tuple[Path, Any]:
        schema = body.get("output_config", {}).get("format", {}).get("schema")
        if purpose is None or not _NAME.fullmatch(purpose):
            raise _NoRecording(f"no recording for purpose {purpose!r} (send {PURPOSE_HEADER})")
        if model is None or not _MODEL.fullmatch(model) or not isinstance(schema, dict):
            raise _NoRecording("a message request without a model or an output_config schema")
        path = recording_path(self._root, purpose, model, schema)
        if not path.is_file():
            raise _NoRecording(f"no recording at {purpose}/{model}/{path.name}")
        return path, schema

    @staticmethod
    def _headers(path: Path) -> tuple[int, dict[str, str]]:
        status, headers = 200, {"content-type": _SSE_CONTENT_TYPE}
        sidecar = path.with_suffix(".headers.json")
        if sidecar.is_file():
            recorded = json.loads(sidecar.read_text("utf-8"))
            status = int(recorded["status"])
            headers.update({str(k).lower(): str(v) for k, v in recorded["headers"].items()})
        return status, headers

    @staticmethod
    def _variant(
        name: Scenario, events: list[SSEEvent], body: Mapping[str, Any], schema: Any
    ) -> list[SSEEvent]:
        if name == "max_tokens":
            text = text_of(events)
            cut = with_text(events, text[: max(1, len(text) // 2)])
            return _with_message_delta(
                cut, stop_reason="max_tokens", usage={"output_tokens": body["max_tokens"]}
            )
        if name == "refusal":
            kept = [e for e in events if not e.name.startswith("content_block") and e.name != "ping"]
            details = {"type": "refusal", "category": REFUSAL_CATEGORY, "explanation": None}
            return _with_message_delta(kept, stop_reason="refusal", stop_details=details)
        if name == "model_mismatch":
            served = str(body["model"])
            other = next(m for m in MODELS if m != served)
            return [
                replace(e, data={**e.data, "message": {**e.data["message"], "model": other}})
                if e.name == "message_start"
                else e
                for e in events
            ]
        if name == "parse_error":
            return with_text(events, PARSE_ERROR_TEXT)
        try:
            doc = json.loads(text_of(events))
        except ValueError as exc:
            raise _NoRecording(f"{name} needs a stored JSON body") from exc
        if name == "enum_stripped":
            first = next(_enum_paths(schema, doc, (), schema.get("$defs", {})), None)
            if first is None:
                raise _NoRecording("enum_stripped needs a stored body with an enum-constrained field")
            _set(doc, first, OUT_OF_ENUM)
        else:  # set_invalid
            items = doc.get("queries") if isinstance(doc, dict) else None
            if not isinstance(items, list) or not items:
                raise _NoRecording("set_invalid needs a stored queries body")
            doc["queries"] = [
                items[i]
                if i < len(items)
                else {**items[i % len(items)], "text": f"{items[i % len(items)]['text']} {i}"}
                for i in range(SET_INVALID_ITEMS)
            ]
        return with_text(events, json.dumps(doc, ensure_ascii=False))
