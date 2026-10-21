"""The budgeted recorder of the paid Anthropic spikes (plan Task 4; plan Global Constraints, "Paid API").

    uv run python scripts/spikes/record_anthropic.py --spike {v13,v4,v19} --dry-run|--live

It is the only way a spike sends a paid message, and the only place that maps `JOBHUNTER_ANTHROPIC_API_KEY`
to a secret: the key is written to a temporary `anthropic_key` file, read into a worker `AppConfig` as every
worker reads its secrets, and the file is deleted at once. The key is never printed, logged or written
anywhere else: the captured request drops `x-api-key`, every written byte has the key's value removed, and an
exception is printed as its class only.

The calls go through the production code: `gateway._chat` on `transport.make_http_client(cfg, mount=…)`,
where the mount is a capturing transport over the network (a direct connection; it asks for an identity
encoding, so the captured stream is the bytes the SDK parsed).

**Budget.** For all of M2: at most `MAX_MESSAGES` (8) paid messages and a running sum of worst-case
reservations of at most `MAX_WORST_CASE_USD` ($0.80), each the exact `pricing.worst_case_usd` of the request
about to be sent (its `extra_usd` included), plus each spike's own call count (`SPIKE_CALLS`). The whole
invocation holds an exclusive `flock` on `.fixtures-raw/anthropic-budget.lock` (a second invocation fails at
once). Before every send, `.fixtures-raw/anthropic-budget.json` (`{"entries": [...], "sent", "worst_case_usd",
"settled_usd", "limits"}`) is rewritten by atomic rename with the message counted (`"state": "sending"`) and
its worst case added; only then is a transport created. A crash after that line leaves the message counted.
After the response the entry is `done` with its status and its settled cost: from the recorded usage when the
stream completed, $0 for an error response (no content), its worst case when the outcome is unknown. A case
that has an entry is never sent again, and the first unexpected outcome stops the spike.

**Files.**
- `.fixtures-raw/anthropic/<spike>/<case>.request.json` (the sent request without its key),
  `<case>.response.headers.json`, `<case>.response.sse` (or `.response.json` for an error) and
  `<case>.check.json` (the spike's field-path verdict): raw, gitignored, never committed.
- `tests/contract/fixtures/anthropic/<purpose>/<model>/<schema12>.sse`: the committed recording of a
  synthetic-input call, in the layout `llm/fake.py` serves. It keeps no provider identity and no thinking: the
  message id is replaced, thinking text and signatures become `fake.THINKING_OMITTED` and
  `fake.SIGNATURE_OMITTED`, and of the response headers only `content-type` is kept (so `request-id`,
  `anthropic-organization-id` and `cf-ray` are removed) in `<schema12>.headers.json`.
  `<schema12>.components.json` holds the byte size of each rendered component of the sent request (system
  blocks, messages, the `output_config` schema as canonical JSON) and the recorded usage, so a payload test
  can compute the recording's input bound without the request capture; a native-PDF call's also holds its
  `native_pdf_count_tokens` and its `input_tokens_bound`.
- A spike whose module sets `RECORD_EVERY_ANSWER = True` (V4: the dry-run replays each case's live result,
  controller ruling R7-3) commits whatever answer a call got and goes on to its next call: a completed stream
  whatever its stop reason, or an error response, whose body is committed without its `request_id` at the same
  path (its status and `content-type` in `<schema12>.headers.json`, and no sidecar: an error has no usage). It
  still counts every send first and never sends a case twice. `--commit-recorded` writes that committed error
  answer from the raw capture of a counted case (one answered before its spike recorded every answer); it
  sends nothing and needs no key.

**Spikes.** A spike is a module next to this one (`SPIKE_MODULES`) that sends nothing itself: `calls()` gives
its calls in order, as dicts with `case`, `purpose`, `model`, `effort`, `max_tokens`, `system` (a list of
texts), `user` (the one user message: a text, or a list of content blocks with an explicit
`input_tokens_bound` or `native_pdf_count_tokens`), `schema` (a JSON schema dict, or a Pydantic model as the
gateway passes its node's: langchain needs a `title` on a dict schema, which is then sent too) and optionally
`extra_usd` (a decimal string), `input_tokens_bound` and `native_pdf_count_tokens` (the call's `count_tokens`:
its bound is `bounds.native_pdf_bound` of it); `check(observation)` returns the spike's verdict of one call
(field paths, never content). V13 is `v13_structured_stream.py`, V4 `v4_schema_dry_run.py` and V19
`v19_token_bound.py`. `counting_gateway(key)` gives the spikes a gateway for the free
`messages.count_tokens` endpoint, with the key mapped as a live run maps it.

`--dry-run` sends nothing, needs no key and writes nothing: it prints the plan, each call's input bound and
worst case, the budget the live run would meet, and the request each call would send, built by the same code
on a transport that answers locally.
"""

import argparse
import asyncio
import copy
import fcntl
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any, Final, TextIO

import anthropic
import httpx2
from jobhunter.config import AppConfig, load_config
from jobhunter.llm import bounds, fake, pricing
from jobhunter.llm.cache_monitor import CacheMonitor
from jobhunter.llm.gateway import LLMGateway, _chat
from jobhunter.llm.transport import ANTHROPIC_BASE_URL, make_http_client
from jobhunter.observability.logging import configure_logging
from jobhunter.observability.redact import register_secret_values
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, SecretStr
from sqlalchemy.ext.asyncio import create_async_engine

HERE: Final = Path(__file__).resolve().parent
REPO: Final = HERE.parents[1]
# The repository's directories, whatever the cwd: one budget for the whole milestone (there is no --root
# flag; `main(root=..., fixtures_out=...)` is for tests only).
DEFAULT_ROOT: Final = REPO / ".fixtures-raw"
DEFAULT_FIXTURES: Final = REPO / "tests/contract/fixtures/anthropic"
KEY_VAR: Final = "JOBHUNTER_ANTHROPIC_API_KEY"

MAX_MESSAGES: Final = 8
MAX_WORST_CASE_USD: Final = Decimal("0.80")
SPIKE_CALLS: Final = {"v13": 2, "v4": 4, "v19": 1}  # V4: the 4 gating cases, no probe message (R7-2)
SPIKE_MODULES: Final = {"v13": "v13_structured_stream", "v4": "v4_schema_dry_run", "v19": "v19_token_bound"}
BANNED_REQUEST_KEYS: Final = ("tool_choice", "tools", "temperature", "top_p", "top_k", "betas", "fallbacks")
# The response headers a committed recording keeps; every other one (request-id, anthropic-organization-id,
# cf-ray, the rate-limit counters of the account) is removed.
COMMITTED_HEADERS: Final = ("content-type",)
_SECRET_HEADERS: Final = frozenset({"x-api-key", "authorization", "cookie", "proxy-authorization"})

OK, FAILED, LOCKED, REFUSED, STOPPED = 0, 1, 3, 4, 5

TransportFactory = Callable[[], httpx2.AsyncBaseTransport]
Clock = Callable[[], datetime]


# --- the plan ----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PlannedCall:
    spike: str
    case: str
    purpose: str
    model: pricing.Model
    effort: pricing.Effort
    max_tokens: int
    system: tuple[str, ...]
    user: str | tuple[Mapping[str, Any], ...]
    schema: Mapping[str, Any] | type[BaseModel]
    extra_usd: Decimal = Decimal(0)
    bound_override: int | None = None
    native_pdf_count_tokens: int | None = None

    @property
    def transformed_schema(self) -> dict[str, Any]:
        """The `output_config.format.schema` the call sends."""
        if isinstance(self.schema, type):
            return anthropic.transform_schema(self.schema)
        return anthropic.transform_schema(copy.deepcopy(dict(self.schema)))

    def input_tokens_bound(self) -> int:
        if self.native_pdf_count_tokens is not None:
            return bounds.native_pdf_bound(self.model, self.native_pdf_count_tokens)
        if self.bound_override is not None:
            return self.bound_override
        if not isinstance(self.user, str):
            raise ValueError(f"{self.spike} {self.case}: content blocks need an explicit input_tokens_bound")
        rendered = bounds.RenderedRequest(
            system=self.system,
            messages=(self.user,),
            schema_json=fake.canonical_schema_json(self.transformed_schema),
        )
        return bounds.text_input_bound(self.model, rendered)

    def worst_case_usd(self) -> Decimal:
        return pricing.worst_case_usd(
            self.model,
            input_tokens_bound=self.input_tokens_bound(),
            max_tokens=self.max_tokens,
            extra_usd=self.extra_usd,
        )

    def messages(self) -> list[BaseMessage]:
        content: Any = self.user if isinstance(self.user, str) else [dict(block) for block in self.user]
        return [*(SystemMessage(text) for text in self.system), HumanMessage(content)]


def load_spike(spike: str) -> ModuleType | None:
    """The definition module of `spike`, or None when there is none yet."""
    path = HERE / f"{SPIKE_MODULES[spike]}.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(f"jobhunter_spike_{spike}", path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def plan(spike: str, module: ModuleType) -> list[PlannedCall]:
    """The validated calls of `module.calls()`."""
    calls: list[PlannedCall] = []
    for raw in module.calls():
        model, effort = raw["model"], raw["effort"]
        if model not in pricing.MODELS or effort not in pricing.SUPPORTED_EFFORTS[model]:
            raise ValueError(f"{spike} {raw['case']}: model {model!r} with effort {effort!r} is not allowed")
        user = raw["user"] if isinstance(raw["user"], str) else tuple(raw["user"])
        if raw.get("input_tokens_bound") is not None and raw.get("native_pdf_count_tokens") is not None:
            raise ValueError(
                f"{spike} {raw['case']}: an input_tokens_bound or a native_pdf_count_tokens, not both"
            )
        calls.append(
            PlannedCall(
                spike=spike,
                case=str(raw["case"]),
                purpose=str(raw["purpose"]),
                model=model,
                effort=effort,
                max_tokens=int(raw["max_tokens"]),
                system=tuple(str(s) for s in raw["system"]),
                user=user,
                schema=raw["schema"],
                extra_usd=Decimal(str(raw.get("extra_usd", "0"))),
                bound_override=raw.get("input_tokens_bound"),
                native_pdf_count_tokens=raw.get("native_pdf_count_tokens"),
            )
        )
    if len({c.case for c in calls}) != len(calls):
        raise ValueError(f"{spike}: case names must be unique")
    return calls


# --- the budget file ---------------------------------------------------------------------------------------


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


class Budget:
    """`.fixtures-raw/anthropic-budget.json`; the totals are always recomputed from the entries."""

    def __init__(self, root: Path) -> None:
        self.path = root / "anthropic-budget.json"
        data = json.loads(self.path.read_text("utf-8")) if self.path.exists() else {}
        self.entries: list[dict[str, Any]] = list(data.get("entries", []))

    def sent(self) -> int:
        return len(self.entries)

    def worst_case_sum(self) -> Decimal:
        return sum((Decimal(str(e["worst_case_usd"])) for e in self.entries), Decimal(0))

    def settled_sum(self) -> Decimal:
        return sum((Decimal(str(e["settled_usd"])) for e in self.entries if e.get("settled_usd")), Decimal(0))

    def has(self, call: PlannedCall) -> bool:
        return self.entry(call) is not None

    def entry(self, call: PlannedCall) -> dict[str, Any] | None:
        """The counted entry of `call`, if it has one."""
        same = (e for e in self.entries if e.get("spike") == call.spike and e.get("case") == call.case)
        return next(same, None)

    def refusal(self, call: PlannedCall, worst_case: Decimal) -> str | None:
        """Why `call` must not be sent, or None."""
        spike_sent = sum(1 for e in self.entries if e.get("spike") == call.spike)
        if spike_sent + 1 > SPIKE_CALLS[call.spike]:
            return f"{call.spike} has sent {spike_sent} of its {SPIKE_CALLS[call.spike]} calls"
        if self.sent() + 1 > MAX_MESSAGES:
            return f"{self.sent()} of the {MAX_MESSAGES} paid messages of M2 are counted"
        if self.worst_case_sum() + worst_case > MAX_WORST_CASE_USD:
            return (
                f"the worst-case sum would be {self.worst_case_sum() + worst_case} "
                f"(${self.worst_case_sum()} counted + ${worst_case}), above ${MAX_WORST_CASE_USD}"
            )
        return None

    def count(self, call: PlannedCall, worst_case: Decimal, now: datetime) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "spike": call.spike,
            "case": call.case,
            "purpose": call.purpose,
            "model": call.model,
            "effort": call.effort,
            "max_tokens": call.max_tokens,
            "input_tokens_bound": call.input_tokens_bound(),
            "extra_usd": str(call.extra_usd),
            "worst_case_usd": str(worst_case),
            "state": "sending",
            "sent_at": _iso(now),
            "status": None,
            "error": None,
            "settled_usd": None,
            "settlement": None,
            "usage": None,
            "stop_reason": None,
        }
        self.entries.append(entry)
        self.save()  # counted before the send: a crash after this line leaves it counted
        return entry

    def save(self) -> None:
        doc = {
            "limits": {
                "messages": MAX_MESSAGES,
                "worst_case_usd": str(MAX_WORST_CASE_USD),
                "spikes": SPIKE_CALLS,
            },
            "sent": self.sent(),
            "worst_case_usd": str(self.worst_case_sum()),
            "settled_usd": str(self.settled_sum()),
            "entries": self.entries,
        }
        _atomic_write(self.path, (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode("utf-8"))

    def left(self) -> str:
        left_usd = MAX_WORST_CASE_USD - self.worst_case_sum()
        return f"messages_left={MAX_MESSAGES - self.sent()} worst_case_left={left_usd}"


@contextmanager
def exclusive(root: Path) -> Iterator[bool]:
    """Hold the exclusive `flock` on `anthropic-budget.lock` for the whole invocation; False when it is
    taken."""
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(root / "anthropic-budget.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


# --- the key -----------------------------------------------------------------------------------------------


def _worker_config(key: str) -> AppConfig:
    """A worker config whose `anthropic_key` secret is `key`, read from a temporary secret file that is gone
    when this returns."""
    directory = Path(tempfile.mkdtemp(prefix="jobhunter-spike-"))
    try:
        fd = os.open(directory / "anthropic_key", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, key.encode("utf-8"))
        finally:
            os.close(fd)
        return load_config("worker", {"JOBHUNTER_ENV": "dev", "JOBHUNTER_DB_NAME": "spike"}, directory)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@asynccontextmanager
async def counting_gateway(key: str) -> AsyncIterator[LLMGateway]:
    """A gateway for the free `messages.count_tokens` endpoint (V4's probe, V19): the key mapped as a live run
    maps it and registered with the log redaction, a direct connection to the API, and an engine that is never
    connected (a count reserves nothing). Its owner must never call `LLMGateway.call` on it."""
    register_secret_values([key])
    cfg = _worker_config(key)
    engine = create_async_engine("postgresql+psycopg://unused@127.0.0.1:1/unused")
    try:
        async with make_http_client(cfg, mount=httpx2.AsyncHTTPTransport()) as client:
            yield LLMGateway(engine, cfg, http_client=client, cache=CacheMonitor())
    finally:
        await engine.dispose()


def _without_key(data: bytes, key: str) -> bytes:
    return data.replace(key.encode("utf-8"), b"[secret:" + KEY_VAR.encode() + b"]") if key else data


# --- the capturing transport -------------------------------------------------------------------------------


class _Tee(httpx2.AsyncByteStream):
    def __init__(self, inner: httpx2.AsyncByteStream, sink: bytearray) -> None:
        self._inner = inner
        self._sink = sink

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._inner:
            self._sink.extend(chunk)
            yield chunk

    async def aclose(self) -> None:
        await self._inner.aclose()


@dataclass
class Capture(httpx2.AsyncBaseTransport):
    """Records the one request it forwards (without its credentials) and the response bytes as they stream."""

    inner: httpx2.AsyncBaseTransport
    request: dict[str, Any] | None = None
    status: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    body: bytearray = field(default_factory=bytearray)

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        content = await request.aread()
        request.headers["accept-encoding"] = "identity"
        self.request = {
            "method": request.method,
            "url": str(request.url),
            "headers": {k: v for k, v in request.headers.items() if k.lower() not in _SECRET_HEADERS},
            "body": json.loads(content) if content else None,
        }
        response = await self.inner.handle_async_request(request)
        self.status, self.headers = response.status_code, dict(response.headers)
        stream = response.stream
        if not isinstance(stream, httpx2.AsyncByteStream):
            raise TypeError("the network transport answered with a sync stream")
        return httpx2.Response(
            response.status_code,
            headers=response.headers,
            stream=_Tee(stream, self.body),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self.inner.aclose()


class _AnswersLocally(httpx2.AsyncBaseTransport):
    """The dry run's network: a 400 for every request, so nothing is sent anywhere."""

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        error = {"type": "error", "error": {"type": "invalid_request_error", "message": "dry run: not sent"}}
        return httpx2.Response(400, json=error)


@dataclass
class Outcome:
    capture: Capture
    out: dict[str, Any] | None
    error: str | None  # the exception's class only


async def _send(call: PlannedCall, cfg: AppConfig, inner: httpx2.AsyncBaseTransport) -> Outcome:
    """One call through the gateway's client factory, with the key of the config's `anthropic_key` secret (a
    placeholder in a dry run, whose config has none)."""
    secret = cfg.secrets.get("anthropic_key")
    key = secret.decode("utf-8") if secret else "dry-run-placeholder-key"
    capture = Capture(inner)
    async with make_http_client(cfg, mount=capture) as client:
        runnable = _chat(
            call.model,
            call.effort,
            call.max_tokens,
            call.schema if isinstance(call.schema, type) else dict(call.schema),
            api_key=SecretStr(key),
            http_client=client,
            purpose=call.purpose,
        )
        try:
            out = await runnable.ainvoke(call.messages())
        except Exception as exc:  # noqa: BLE001 - recorded by class only: its text can carry request data
            return Outcome(capture, None, type(exc).__name__)
    return Outcome(capture, out, None)


# --- what a call left behind -------------------------------------------------------------------------------


def _system_texts(body: Mapping[str, Any]) -> list[str]:
    system = body.get("system")
    if system is None:
        return []
    if isinstance(system, str):
        return [system]
    return [str(block.get("text", "")) for block in system]


def _message_texts(body: Mapping[str, Any]) -> list[str]:
    texts = []
    for message in body.get("messages", []):
        content = message.get("content")
        if isinstance(content, str):
            texts.append(content)
        else:
            texts.append("".join(str(b.get("text", "")) for b in content if b.get("type") == "text"))
    return texts


def payload_problems(call: PlannedCall, captured: Mapping[str, Any] | None) -> list[str]:
    """How the captured request departs from the call's policy (§10.9), as names; [] when it does not."""
    if captured is None or captured.get("body") is None:
        return ["no request was captured"]
    body: Mapping[str, Any] = captured["body"]
    output_config = body.get("output_config", {})
    checks = {
        "host api.anthropic.com": captured["url"] == f"{ANTHROPIC_BASE_URL}/v1/messages",
        "stream true": body.get("stream") is True,
        "model": body.get("model") == call.model,
        "max_tokens": body.get("max_tokens") == call.max_tokens,
        "output_config.format json_schema": output_config.get("format", {}).get("type") == "json_schema",
        "output_config.format.schema": output_config.get("format", {}).get("schema")
        == call.transformed_schema,
        "output_config.effort": output_config.get("effort") == call.effort,
        "last message user": (body.get("messages") or [{}])[-1].get("role") == "user",
        "system as planned": _system_texts(body) == list(call.system),
        "purpose header": captured["headers"].get(fake.PURPOSE_HEADER.lower()) == call.purpose,
    }
    if isinstance(call.user, str):
        checks["user message as planned"] = _message_texts(body)[-1:] == [call.user]
    problems = [name for name, ok in checks.items() if not ok]
    problems += [f"{key} present" for key in BANNED_REQUEST_KEYS if key in body]
    return problems


def _merged_usage(events: list[fake.SSEEvent]) -> dict[str, int] | None:
    """The response's usage: `message_delta`'s cumulative counts, `message_start`'s for what it lacks."""
    start = next((e.data["message"].get("usage", {}) for e in events if e.name == "message_start"), {})
    delta = next((e.data.get("usage", {}) for e in events if e.name == "message_delta"), None)
    if delta is None:
        return None
    keys = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
    return {k: int(delta.get(k) if delta.get(k) is not None else start.get(k) or 0) for k in keys}


def committed_stream(events: list[fake.SSEEvent], message_id: str) -> list[fake.SSEEvent]:
    """`events` without provider identity or thinking: the message id replaced, each thinking block reduced to
    one `fake.THINKING_OMITTED` delta and one `fake.SIGNATURE_OMITTED` signature."""
    thinking = {
        int(e.data["index"])
        for e in events
        if e.name == "content_block_start"
        and e.data["content_block"].get("type") in ("thinking", "redacted_thinking")
    }
    out: list[fake.SSEEvent] = []
    seen: set[tuple[int, str]] = set()
    for e in events:
        data = e.data
        if e.name == "message_start":
            data = {**data, "message": {**data["message"], "id": message_id}}
        elif e.name == "content_block_start" and int(data["index"]) in thinking:
            block = dict(data["content_block"])
            for name in ("signature", "data"):
                if name in block:
                    block[name] = fake.SIGNATURE_OMITTED
            if block.get("thinking"):
                block["thinking"] = fake.THINKING_OMITTED
            data = {**data, "content_block": block}
        elif e.name == "content_block_delta" and int(data["index"]) in thinking:
            kind = str(data["delta"].get("type"))
            if (int(data["index"]), kind) in seen:
                continue
            seen.add((int(data["index"]), kind))
            if kind == "thinking_delta":
                data = {**data, "delta": {"type": kind, "thinking": fake.THINKING_OMITTED}}
            elif kind == "signature_delta":
                data = {**data, "delta": {"type": kind, "signature": fake.SIGNATURE_OMITTED}}
        out.append(fake.SSEEvent(e.name, data))
    return out


# --- one invocation ----------------------------------------------------------------------------------------


@dataclass
class Recorder:
    spike: str
    module: ModuleType
    calls: list[PlannedCall]
    root: Path
    fixtures_out: Path
    transport_factory: TransportFactory
    now: Clock
    out: TextIO

    def say(self, line: str) -> None:
        print(line, file=self.out, flush=True)

    async def dry_run(self) -> int:
        budget = Budget(self.root)
        with tempfile.TemporaryDirectory(prefix="jobhunter-dry-run-") as no_secrets:
            cfg = load_config(
                "worker", {"JOBHUNTER_ENV": "dev", "JOBHUNTER_DB_NAME": "spike"}, Path(no_secrets)
            )
        code = OK
        for call in self.calls:
            worst = call.worst_case_usd()
            self.say(
                f"{call.spike} {call.case} {call.model} effort={call.effort} max_tokens={call.max_tokens} "
                f"input_tokens_bound={call.input_tokens_bound()} worst_case_usd={worst} "
                f"schema12={fake.schema12(call.transformed_schema)}"
            )
            if budget.has(call):
                self.say(f"{call.spike} {call.case} already counted: a live run skips it")
            elif (why := budget.refusal(call, worst)) is not None:
                self.say(f"{call.spike} {call.case} a live run would be REFUSED: {why}")
            else:
                budget.entries.append({"spike": call.spike, "case": call.case, "worst_case_usd": str(worst)})
            outcome = await _send(call, cfg, _AnswersLocally())
            problems = payload_problems(call, outcome.capture.request)
            self.say(
                f"{call.spike} {call.case} payload {'ok' if not problems else 'BAD: ' + ', '.join(problems)}"
            )
            if problems:
                code = STOPPED
        self.say(
            f"budget after a live run: sent={budget.sent()}/{MAX_MESSAGES} "
            f"worst_case_usd={budget.worst_case_sum()}"
        )
        return code

    async def live(self, key: str) -> int:
        with exclusive(self.root) as held:
            if not held:
                self.say(
                    f"{self.spike}: LOCKED, another recorder holds {self.root / 'anthropic-budget.lock'}"
                )
                return LOCKED
            budget = Budget(self.root)
            pending = []
            for call in self.calls:
                if budget.has(call):
                    self.say(f"{call.spike} {call.case} already counted; never sent twice")
                else:
                    pending.append(call)
            if not pending:
                self.say(f"{self.spike}: nothing left to send ({budget.left()})")
                return OK
            cfg = _worker_config(key)
            if cfg.secrets.get("anthropic_key") != key.encode("utf-8"):
                self.say(f"{self.spike}: FAILED, the anthropic_key secret did not load; nothing sent")
                return FAILED
            for call in pending:
                worst = call.worst_case_usd()
                if (why := budget.refusal(call, worst)) is not None:
                    self.say(f"{call.spike} {call.case} REFUSED: {why}; nothing sent")
                    return REFUSED
                entry = budget.count(call, worst, self.now())
                outcome = await _send(call, cfg, self.transport_factory())
                if not self._finish(call, entry, budget, outcome, key):
                    self.say(f"{call.spike} {call.case} STOP: unexpected outcome; nothing more sent")
                    return STOPPED
            return OK

    def _finish(
        self, call: PlannedCall, entry: dict[str, Any], budget: Budget, outcome: Outcome, key: str
    ) -> bool:
        """Settle and record one sent call; whether it completed as expected."""
        capture = outcome.capture
        raw_dir = self.root / "anthropic" / call.spike
        if capture.request is not None:
            _atomic_write(raw_dir / f"{call.case}.request.json", _json(capture.request, key))
        status = capture.status
        streamed = status == 200 and capture.headers.get("content-type", "").startswith("text/event-stream")
        events: list[fake.SSEEvent] = []
        if status is not None:
            headers = {"status": status, "headers": dict(sorted(capture.headers.items()))}
            _atomic_write(raw_dir / f"{call.case}.response.headers.json", _json(headers, key))
            suffix = ".response.sse" if streamed else ".response.json"
            _atomic_write(raw_dir / f"{call.case}{suffix}", _without_key(bytes(capture.body), key))
            if streamed:
                try:
                    events = fake.parse_sse(bytes(capture.body))
                except ValueError:
                    events = []
        usage = _merged_usage(events)
        complete = bool(events) and events[-1].name == "message_stop" and usage is not None
        if complete and usage is not None:
            settled = pricing.settled_usd(
                call.model,
                pricing.Usage(
                    usage["input_tokens"],
                    usage["output_tokens"],
                    usage["cache_creation_input_tokens"],
                    usage["cache_read_input_tokens"],
                ),
            )
            settlement = "billed"
        elif status is not None and status >= 400:
            settled, settlement = Decimal("0.000000"), "not_billed:error_response_no_content"
        else:
            settled, settlement = Decimal(entry["worst_case_usd"]), "ambiguous:kept_at_worst_case"
        stop_reason = next(
            (e.data["delta"].get("stop_reason") for e in events if e.name == "message_delta"), None
        )
        every_answer = self.records_every_answer
        error_answer = every_answer and not streamed and status is not None and status >= 400
        entry.update(
            state="done",
            status=status,
            error=outcome.error,
            settled_usd=str(settled),
            settlement=settlement,
            usage=usage,
            stop_reason=stop_reason,
            finished_at=_iso(self.now()),
        )
        budget.save()
        self.say(
            f"{call.spike} {call.case} {status if status is not None else '-'} stop_reason={stop_reason} "
            f"error={outcome.error} worst_case_usd={entry['worst_case_usd']} settled_usd={settled} "
            f"{budget.left()}"
        )
        if error_answer and status is not None and capture.request is not None:
            # The spike records every answer (V4): this error is the case's live result; the spike goes on.
            self._record_error(
                call, capture.request["body"], status, capture.headers, bytes(capture.body), key
            )
            return True
        if not complete or outcome.error is not None or usage is None:
            if every_answer:
                self.say(f"{call.spike} {call.case} no answer to record; the spike goes on")
                return True
            return False
        observation = self._observation(call, events, capture, outcome)
        report = self.module.check(observation)
        _atomic_write(raw_dir / f"{call.case}.check.json", _json(report, key))
        self.say(f"{call.spike} {call.case} check missing={report.get('missing')}")
        # Only a turn that ended and parsed is the stored `ok` stream of the fake, unless the spike records
        # every answer.
        parsed = observation["parsed_ok"]
        if (stop_reason != "end_turn" or not parsed) and not every_answer:
            self.say(f"{call.spike} {call.case} not committed: stop_reason={stop_reason} parsed={parsed}")
            return False
        self._commit(call, events, capture, usage, key)
        return True

    @property
    def records_every_answer(self) -> bool:
        return getattr(self.module, "RECORD_EVERY_ANSWER", False) is True

    def _record_error(
        self,
        call: PlannedCall,
        request_body: Mapping[str, Any],
        status: int,
        headers: Mapping[str, str],
        body: bytes,
        key: str,
    ) -> None:
        """Check an error answer and commit it as the fake's answer of the call."""
        report = self.module.check(self._error_observation(call, request_body, status, body))
        _atomic_write(self.root / "anthropic" / call.spike / f"{call.case}.check.json", _json(report, key))
        self.say(f"{call.spike} {call.case} answer {status} recorded; check missing={report.get('missing')}")
        self._commit_error(call, request_body, status, headers, body, key)

    def commit_recorded(self) -> int:
        """`--commit-recorded`: commit the fake's answer of each counted case of a spike that records every
        answer, whose raw capture is an error response; nothing is sent and no key is read."""
        if not self.records_every_answer:
            self.say(f"{self.spike}: REFUSED, the spike does not record every answer; nothing written")
            return REFUSED
        with exclusive(self.root) as held:
            if not held:
                self.say(
                    f"{self.spike}: LOCKED, another recorder holds {self.root / 'anthropic-budget.lock'}"
                )
                return LOCKED
            budget = Budget(self.root)
            raw_dir = self.root / "anthropic" / self.spike
            for call in self.calls:
                entry = budget.entry(call) or {}
                status = entry.get("status")
                if entry.get("state") != "done" or not isinstance(status, int) or status < 400:
                    continue
                request = json.loads((raw_dir / f"{call.case}.request.json").read_text("utf-8"))
                response = json.loads((raw_dir / f"{call.case}.response.headers.json").read_text("utf-8"))
                body = (raw_dir / f"{call.case}.response.json").read_bytes()
                self._record_error(call, request["body"], status, response["headers"], body, "")
            return OK

    def _commit(
        self,
        call: PlannedCall,
        events: list[fake.SSEEvent],
        capture: Capture,
        usage: dict[str, int],
        key: str,
    ) -> None:
        body: Mapping[str, Any] = capture.request["body"] if capture.request else {}
        schema = body["output_config"]["format"]["schema"]
        path = fake.recording_path(self.fixtures_out, call.purpose, call.model, schema)
        stream = committed_stream(events, f"msg_recorded_{fake.schema12(schema)}")
        _atomic_write(path, _without_key(fake.render_sse(stream), key))
        kept = {k: v for k, v in capture.headers.items() if k.lower() in COMMITTED_HEADERS}
        _atomic_write(
            path.with_suffix(".headers.json"), _json({"status": capture.status, "headers": kept}, key)
        )
        components = {
            "source": "recorded",
            "spike": call.spike,
            "case": call.case,
            "purpose": call.purpose,
            "model": call.model,
            "effort": call.effort,
            "max_tokens": call.max_tokens,
            "component_bytes": {
                "system": [len(t.encode("utf-8")) for t in _system_texts(body)],
                "messages": [len(t.encode("utf-8")) for t in _message_texts(body)],
                "schema": len(fake.canonical_schema_json(schema).encode("utf-8")),
            },
            "usage": usage,
        }
        if call.native_pdf_count_tokens is not None:
            components["native_pdf_count_tokens"] = call.native_pdf_count_tokens
            components["input_tokens_bound"] = call.input_tokens_bound()
        _atomic_write(path.with_suffix(".components.json"), _json(components, key))

    def _commit_error(
        self,
        call: PlannedCall,
        request_body: Mapping[str, Any],
        status: int,
        headers: Mapping[str, str],
        body: bytes,
        key: str,
    ) -> None:
        """The fake's answer of an error response: its JSON body without the `request_id`, and its status and
        `content-type`; no sidecar (an error carries no usage)."""
        schema = request_body["output_config"]["format"]["schema"]
        path = fake.recording_path(self.fixtures_out, call.purpose, call.model, schema)
        error = json.loads(body)
        if not isinstance(error, dict):
            raise ValueError("an error response that is no JSON object")
        error.pop("request_id", None)
        _atomic_write(path, _json(error, key))
        kept = {k: v for k, v in headers.items() if k.lower() in COMMITTED_HEADERS}
        _atomic_write(path.with_suffix(".headers.json"), _json({"status": status, "headers": kept}, key))

    @staticmethod
    def _error_observation(
        call: PlannedCall, request_body: Mapping[str, Any], status: int, body: bytes
    ) -> dict[str, Any]:
        try:
            error = json.loads(body)
        except ValueError:
            error = None
        detail = error.get("error", {}) if isinstance(error, dict) else {}
        return {
            "requested_model": call.model,
            "transformed_schema": call.transformed_schema,
            "request": request_body,
            "status": status,
            "error_type": detail.get("type"),
            "error_message": detail.get("message"),
        }

    @staticmethod
    def _observation(
        call: PlannedCall, events: list[fake.SSEEvent], capture: Capture, outcome: Outcome
    ) -> dict[str, Any]:
        out = outcome.out or {}
        raw = out.get("raw")
        parsing_error = out.get("parsing_error")
        return {
            "requested_model": call.model,
            "transformed_schema": call.transformed_schema,
            "request": capture.request["body"] if capture.request else {},
            "events": [{"name": e.name, "data": e.data} for e in events],
            "response_metadata": dict(raw.response_metadata) if isinstance(raw, AIMessage) else {},
            "usage_metadata": dict(raw.usage_metadata or {}) if isinstance(raw, AIMessage) else {},
            "parsed_ok": out.get("parsed") is not None,
            "parsing_error": None if parsing_error is None else type(parsing_error).__name__,
        }


def _json(doc: Any, key: str) -> bytes:
    return _without_key(
        (json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n").encode("utf-8"),
        key,
    )


def _parse(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="record_anthropic.py", description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--spike", required=True, choices=sorted(SPIKE_CALLS))
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--commit-recorded", action="store_true")
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    root: Path | None = None,
    fixtures_out: Path | None = None,
    transport_factory: TransportFactory | None = None,
    out: TextIO | None = None,
    now: Clock | None = None,
) -> int:
    """Run one spike; the exit status is `OK`, `FAILED`, `LOCKED`, `REFUSED` or `STOPPED`. `root`,
    `fixtures_out` and `transport_factory` (tests only) replace the repository directories and the network."""
    configure_logging()  # it also holds the SDK's and httpx2's loggers at WARNING
    stream = sys.stdout if out is None else out
    key = ""
    try:
        args = _parse(sys.argv[1:] if argv is None else argv)
        env = os.environ if environ is None else environ
        module = load_spike(args.spike)
        if module is None:
            print(
                f"{args.spike}: REFUSED, the spike has no definition ({SPIKE_MODULES[args.spike]}.py); "
                "nothing sent",
                file=stream,
            )
            return REFUSED
        calls = plan(args.spike, module)
        if len(calls) > SPIKE_CALLS[args.spike]:
            print(
                f"{args.spike}: REFUSED, {len(calls)} calls planned but the spike may send "
                f"{SPIKE_CALLS[args.spike]}; "
                "nothing sent",
                file=stream,
            )
            return REFUSED
        recorder = Recorder(
            args.spike,
            module,
            calls,
            DEFAULT_ROOT if root is None else root,
            DEFAULT_FIXTURES if fixtures_out is None else fixtures_out,
            transport_factory or httpx2.AsyncHTTPTransport,
            now or (lambda: datetime.now(UTC)),
            stream,
        )
        if args.dry_run:
            return asyncio.run(recorder.dry_run())
        if args.commit_recorded:
            return recorder.commit_recorded()
        key = env.get(KEY_VAR, "").strip()
        if not key:
            print(f"{args.spike}: REFUSED, {KEY_VAR} is not set; nothing sent", file=stream)
            return REFUSED
        register_secret_values([key])  # from here on a log line masks it
        return asyncio.run(recorder.live(key))
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 - the class only: an exception's text can carry request data
        print(f"record_anthropic: {type(exc).__name__}", file=stream)
        return FAILED


if __name__ == "__main__":
    # The SDK and langchain read ANTHROPIC_* variables (a base URL, a key, custom headers): none may steer a
    # paid call, so this process drops them before anything reads them (G1).
    for name in [name for name in os.environ if name.startswith("ANTHROPIC_")]:
        del os.environ[name]
    raise SystemExit(main())
