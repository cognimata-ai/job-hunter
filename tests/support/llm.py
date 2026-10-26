"""LLM test helpers (Task 3; Tasks 4 and 5 extend it): spend owners, reservation arguments, a completed call's
response fields, the stored month rows, and the settings save of class `budget` that Task 17 will make.

Task 4 adds the transport side: `FakeAnthropic` (the recorded-SSE fake as a test drives it), the autouse
fixture that gives every test its own fake state, `LocalSSEServer` (a real socket on `127.0.0.1:0`) and
`synthetic_sse` (a stream in the event shapes V13 recorded).

Task 5 adds the gateway side: the `fake_llm` fixture (`FakeLLM`: recordings of the test schema `GatewayOut`
written for the test, gateways on them, the tries `served`, the backoff waits `sleeps`), the test node and
prompt, and the C35 helpers `create_llm_call_operation`, `reclaim_stale_now` and `ledger_states`.

Task 6 adds the payload contract side (§10.9): `CapturingTransport`, `capture_request` (one `gateway._chat`
try on it, no database, no network) and `assert_single_call_payload`.

Task 13 adds `write_synthetic_recording`, the builder of a hand-authored synthetic stream and its sidecars
(the committed `extract_profile` fixture; Task 20's `propose_queries` one).

Every row these helpers write holds ids, hashes and amounts only, never prompt or CV text.
"""

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Literal, Self

import anthropic
import httpx2
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.config import AppConfig, load_config
from jobhunter.db.sql import q
from jobhunter.llm import fake
from jobhunter.llm.budget import BudgetParams, BudgetRow, month_of
from jobhunter.llm.cache_monitor import CacheMonitor
from jobhunter.llm.gateway import (
    CallContext,
    LLMGateway,
    NodeConfig,
    RenderedPrompt,
    SystemBlock,
    _chat,
    to_messages,
)
from jobhunter.llm.pricing import MODELS, Usage
from jobhunter.llm.schemas import transformed
from jobhunter.llm.spend_ledger import (
    CallOutcome,
    EvalRunOwner,
    OperationOwner,
    SpendOwner,
    change_cap,
    ensure_month,
)
from jobhunter.llm.transport import make_http_client
from jobhunter.types import JSONValue, get_object
from jobhunter.worker import operations as ops
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, ConfigDict, SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.m2 import insert_eval_run
from tests.support.pg import PgServer, pg_one

_ACTIVE_EXECUTION_CONFIG: Final = "SELECT execution_config_id FROM active_config WHERE id = 1"
_MONTH: Final = q(
    "SELECT month, cap_usd, overrun_buffer_usd, counted_usd, scoring_counted_usd, nonscoring_counted_usd, "
    "bucket_usd, bucket_at, cap_lowered_at, counted_at_cap_change_usd, cap_reached_alerted_at, updated_at "
    "FROM llm_budget WHERE month = :month"
)
_ACTIVE_PAYLOAD: Final = q(
    "SELECT v.payload FROM active_config a JOIN settings_versions v ON v.id = a.settings_version_id "
    "WHERE a.id = 1 FOR UPDATE OF a"
)
_NEXT_VERSION: Final = q(
    "INSERT INTO settings_versions (version_no, schema_version, payload, scope_version_id, model_policy_id, "
    "execution_config_id, evaluation_policy_id, effect_classes) "
    "SELECT (SELECT max(version_no) + 1 FROM settings_versions), v.schema_version, :payload, "
    "v.scope_version_id, v.model_policy_id, v.execution_config_id, v.evaluation_policy_id, '{budget}' "
    "FROM active_config a JOIN settings_versions v ON v.id = a.settings_version_id WHERE a.id = 1 "
    "RETURNING id",
    jsonb=["payload"],
)
_ACTIVATE: Final = q("UPDATE active_config SET settings_version_id = :v, updated_at = now() WHERE id = 1")

# The seeded execution config never changes within a session (`reset_database` keeps the seeded rows), so
# `reserve_kw` can name it without a database round trip; the owner fixtures read it.
_execution_config_id: int | None = None


def _seeded_execution_config(server: PgServer) -> int:
    global _execution_config_id
    if _execution_config_id is None:
        row = pg_one(server, "app", _ACTIVE_EXECUTION_CONFIG)
        assert row is not None
        _execution_config_id = int(str(row[0]))
    return _execution_config_id


async def new_operation_owner(
    engine: AsyncEngine, *, epoch: int = 1, key: str | None = None
) -> OperationOwner:
    """A claimed (`running`) `extract_profile` operation as a spend owner."""
    async with engine.begin() as conn:
        op_id, _ = await ops.create_operation(
            conn,
            kind="extract_profile",
            payload={"draft_id": 1},
            dedup_key=key or f"llm-test:{uuid.uuid4()}",
            defer=None,
        )
    return await claim_owner(engine, op_id, epoch=epoch)


async def claim_owner(engine: AsyncEngine, operation_id: int, *, epoch: int = 1) -> OperationOwner:
    """A new execution's claim of the `pending` operation `operation_id`, as a spend owner."""
    async with engine.begin() as conn:
        claim = await ops.claim_operation(conn, operation_id, epoch=epoch)
    assert claim is not None, f"operation {operation_id} is not claimable"
    return OperationOwner(claim)


async def reclaim(engine: AsyncEngine, owner: OperationOwner) -> None:
    """The `operations` reconciler's stale reclaim of `owner`'s operation: `claim_id := NULL`, back to
    `pending` (§3.3.3), so the old claim never holds again."""
    async with engine.begin() as conn:
        reclaimed = await ops.reclaim_stale_operations(
            conn, now=utcnow() + ops.OPERATION_STALE + timedelta(seconds=1)
        )
    assert owner.claim.operation_id in reclaimed


async def new_eval_owner(engine: AsyncEngine) -> EvalRunOwner:
    """A `running` schema dry-run `eval_runs` row as a spend owner."""
    async with engine.begin() as conn:
        return EvalRunOwner(await insert_eval_run(conn))


@pytest.fixture
async def op_owner(
    engine: AsyncEngine, current_epoch: int, migrated: PgServer
) -> AsyncIterator[OperationOwner]:
    """The spend owner of an operation claimed by the current worker epoch (§3.3.3 `operations` predicate)."""
    _seeded_execution_config(migrated)
    yield await new_operation_owner(engine, epoch=current_epoch)


@pytest.fixture
async def eval_owner(engine: AsyncEngine, clean_db: None, migrated: PgServer) -> AsyncIterator[EvalRunOwner]:
    """The spend owner of a `running` schema dry-run."""
    _seeded_execution_config(migrated)
    yield await new_eval_owner(engine)


def reserve_kw(
    owner: SpendOwner, *, now: datetime, bound: int, max_tokens: int, **overrides: Any
) -> dict[str, Any]:
    """The keyword arguments of `spend_ledger.reserve` for one try of `owner`: an `extract_profile` call
    (900 s) of an operation, a `schema_dry_run` call (300 s) of an eval run, on `claude-opus-5-5`, with the
    input bound `bound`; `overrides` replace any of them."""
    assert _execution_config_id is not None, "an owner fixture reads the seeded execution config first"
    if isinstance(owner, EvalRunOwner):
        purpose, deadline_s, eval_run_id = "schema_dry_run", 300, owner.eval_run_id
    else:
        purpose, deadline_s, eval_run_id = "extract_profile", 900, None
    kw: dict[str, Any] = {
        "owner": owner,
        "purpose": purpose,
        "purpose_class": "nonscoring",
        "model": "claude-opus-5-5",
        "effort": "medium",
        "max_tokens": max_tokens,
        "max_tokens_stage": "first",
        "transport_try": 1,
        "logical_call_no": 1,
        "deadline_s": deadline_s,
        "input_tokens_bound": bound,
        "native_pdf_extra": False,
        "execution_config_id": _execution_config_id,
        "prompt_template_version": "test-v1",
        "rendered_prompt_sha256": b"\x01" * 32,
        "prefix_sha256": None,
        "input_refs": {"profile_draft_id": None},
        "profile_draft_id": None,
        "eval_run_id": eval_run_id,
        "transport": "stream",
        "now": now,
    }
    kw.update(overrides)
    return kw


def scoring_kw(
    owner: SpendOwner,
    *,
    now: datetime,
    bound: int,
    max_tokens: int,
    target_id: int,
    attempt_id: int,
    **overrides: Any,
) -> dict[str, Any]:
    """`reserve_kw` for a scoring `generate` call of attempt `attempt_id` of target `target_id`. Migration
    0008 closed the two foreign keys M2 staged (`llm_calls.attempt_id` and `llm_calls.target_id`), so both
    name real rows: `tests.support.m3.insert_claimed` makes a pair."""
    values: dict[str, Any] = {
        "purpose": "generate",
        "purpose_class": "scoring",
        "deadline_s": 600,
        "attempt_id": attempt_id,
        "target_id": target_id,
        **overrides,
    }
    return reserve_kw(owner, now=now, bound=bound, max_tokens=max_tokens, **values)


def completed_call(**overrides: Any) -> CallOutcome:
    """A completed streamed response of `claude-opus-5-5` with a synthetic structured output."""
    values: dict[str, Any] = {
        "status": "completed",
        "model_served": "claude-opus-5-5",
        "http_status": 200,
        "stop_reason": "end_turn",
        "provider_request_id": "req_test_0001",
        "output_json": {"synthetic": True},
        **overrides,
    }
    return CallOutcome(**values)


def error_call(http_status: int | None = 429, **overrides: Any) -> CallOutcome:
    """An error response without content (or, with `http_status=None`, a connect failure)."""
    return CallOutcome(status="error", http_status=http_status, error_code="test_error", **overrides)


USAGE: Final = Usage(1000, 200, 0, 0)  # 1000 x $4 + 200 x $20 per MTok = $0.008 on claude-opus-5-5


async def budget_rows(engine: AsyncEngine, *months: date) -> tuple[Any, ...]:
    """The stored `llm_budget` rows of `months` (no accrual) as `BudgetRow`s, `None` for a month without one.

    Typed `Any` so that a test reads the row of a month it knows exists without narrowing it first (the pinned
    Review Focus 4 tests)."""
    rows: list[BudgetRow | None] = []
    async with engine.connect() as conn:
        for month in months:
            row = (await conn.execute(_MONTH, {"month": month})).first()
            rows.append(None if row is None else BudgetRow(**row._mapping))
    return tuple(rows)


async def save_budget(engine: AsyncEngine, *, now: datetime, **llm: JSONValue) -> None:
    """A settings save of class `budget` as Task 17 makes it, committed: `active_config FOR UPDATE`, a new
    settings version with `llm.<key> = value` for each of `llm` (for example `cap_usd="10.00"`), activated,
    then the month of `now` ensured from the new values and its row changed under its lock."""
    async with engine.begin() as conn:
        payload = dict(get_object((await conn.execute(_ACTIVE_PAYLOAD)).scalar_one(), ""))
        payload["llm"] = {**get_object(payload, "llm"), **llm}
        version = int((await conn.execute(_NEXT_VERSION, {"payload": payload})).scalar_one())
        await conn.execute(_ACTIVATE, {"v": version})
        p = BudgetParams.from_payload(payload)
        await ensure_month(conn, month_of(now), p, now)
        await change_cap(
            conn, month=month_of(now), cap_usd=p.cap_usd, overrun_buffer_usd=p.overrun_buffer_usd, now=now
        )


@dataclass(frozen=True)
class LedgerRow:
    state: str
    reserved_usd: Decimal
    settled_usd: Decimal | None
    evidence: str | None
    budget_month: date
    admitted_cap_usd: Decimal
    counted_before_usd: Decimal
    call_status: str
    cost_usd: Decimal | None
    error_code: str | None
    input_refs: dict[str, JSONValue]


async def ledger_row(engine: AsyncEngine, ledger_id: int) -> LedgerRow:
    """A ledger row with its call's status, cost, error code and `input_refs`."""
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                sa.text(
                    "SELECT l.state, l.reserved_usd, l.settled_usd, l.evidence, l.budget_month, "
                    "l.admitted_cap_usd, l.counted_before_usd, c.status AS call_status, c.cost_usd, "
                    "c.error_code, c.input_refs FROM llm_spend_ledger l "
                    "JOIN llm_calls c ON c.spend_ledger_id = l.id WHERE l.id = :id"
                ),
                {"id": ledger_id},
            )
        ).one()
    return LedgerRow(**row._mapping)


_COUNTS: Final = {
    "llm_spend_ledger": sa.text("SELECT count(*) FROM llm_spend_ledger"),
    "llm_calls": sa.text("SELECT count(*) FROM llm_calls"),
    "llm_budget": sa.text("SELECT count(*) FROM llm_budget"),
}


async def count_rows(engine: AsyncEngine, table: str) -> int:
    """The number of rows in `table` (`llm_spend_ledger`, `llm_calls` or `llm_budget`)."""
    async with engine.connect() as conn:
        return int((await conn.execute(_COUNTS[table])).scalar_one())


async def open_alerts(engine: AsyncEngine, code: str) -> list[tuple[str, str, int]]:
    """`(dedup_key, severity, occurrences)` of the open alerts with `code`."""
    async with engine.connect() as conn:
        rows = await conn.execute(
            sa.text(
                "SELECT dedup_key, severity, occurrences FROM alerts "
                "WHERE code = :code AND resolved_at IS NULL ORDER BY id"
            ),
            {"code": code},
        )
        return [(r.dedup_key, r.severity, int(r.occurrences)) for r in rows]


# --- Task 4: the recorded-SSE fake, a local SSE server and synthetic streams -------------------------------

# The committed recordings, whatever the cwd.
FAKE_LLM_DIR: Final = Path(__file__).resolve().parents[1] / "contract/fixtures/anthropic"


@pytest.fixture(autouse=True)
def _fake_llm_state() -> Iterator[fake.FakeLLMState]:
    """Every test has its own fake LLM state: every try `ok`, no `count_tokens` answer, no request served."""
    with fake.fresh_state() as state:
        yield state


def fake_llm_config(root: Path, secrets_dir: Path) -> AppConfig:
    """A worker config in fake LLM mode over the recordings under `root` (no secret files needed)."""
    env = {
        "JOBHUNTER_ENV": "test",
        "JOBHUNTER_DB_NAME": "jobhunter",
        "JOBHUNTER_FAKE_LLM": "1",
        "JOBHUNTER_FAKE_LLM_DIR": str(root),
    }
    return load_config("worker", env, secrets_dir)


class FakeAnthropic:
    """The recorded-SSE fake as a test drives it: clients in fake mode, the scenario of the test, the
    `count_tokens` answer, and the message tries the fake received (`served`, `requests`)."""

    def __init__(self, cfg: AppConfig) -> None:
        if not cfg.fake_llm:
            raise ValueError("FakeAnthropic needs a config in fake LLM mode")
        self.cfg = cfg

    @property
    def root(self) -> Path:
        return self.cfg.fake_llm_dir

    @property
    def state(self) -> fake.FakeLLMState:
        return fake.state()

    @property
    def served(self) -> int:
        """Every message try that reached the fake (a `count_tokens` request is none)."""
        return self.state.served

    @property
    def requests(self) -> list[fake.ServedRequest]:
        return self.state.requests

    def client(self, *, read_timeout_s: float = 120.0) -> httpx2.AsyncClient:
        """A gateway http client on the fake (the caller closes it); `read_timeout_s` as in
        `make_http_client`."""
        return make_http_client(self.cfg, read_timeout_s=read_timeout_s)

    def scenario(self, *names: fake.Scenario) -> None:
        fake.scenario(*names)

    def count_tokens(self, result: int | Literal["error"]) -> None:
        fake.count_tokens(result)


@pytest.fixture
def fake_anthropic(tmp_path: Path) -> FakeAnthropic:
    """The fake over the committed recordings (`tests/contract/fixtures/anthropic`)."""
    return FakeAnthropic(fake_llm_config(FAKE_LLM_DIR, tmp_path / "no-secrets"))


_SYNTHETIC_SIGNATURE: Final = "U3ludGhldGljU2lnbmF0dXJlRm9yVGhlVGVzdHNPbmx5" * 2  # key-shaped on purpose
_SYNTHETIC_USAGE: Final = {
    "input_tokens": 120,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0,
    "output_tokens": 60,
}


def synthetic_sse(
    body: Any,
    *,
    model: str,
    stop_reason: str = "end_turn",
    usage: dict[str, int] | None = None,
    thinking: str | None = None,
    message_id: str = "msg_0123456789synthetic",
) -> bytes:
    """A streamed Messages answer whose text is `body` (a JSON value, or a string sent as it is), in the event
    shapes of the V13 recordings (docs/spikes/V13.md): `message_start` (the message with `container`,
    `stop_details` and `diagnostics` null, its usage with the `cache_creation` breakdown), one text block
    (`content_block_start`, a `ping`, two `text_delta`s, `content_block_stop`), `message_delta` with the
    cumulative usage and `output_tokens_details`, and `message_stop`. Every value is synthetic.

    `thinking` adds a thinking block with a key-shaped signature before the text block. V13 recorded none
    (effort `low` did not think), so its shape is the SDK's `ThinkingBlock` and `signature_delta`; it exists
    to test that a recording keeps no thinking."""
    text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    totals = {**_SYNTHETIC_USAGE, **(usage or {})}
    start_usage = {
        "input_tokens": totals["input_tokens"],
        "cache_creation_input_tokens": totals["cache_creation_input_tokens"],
        "cache_read_input_tokens": totals["cache_read_input_tokens"],
        "cache_creation": {
            "ephemeral_5m_input_tokens": totals["cache_creation_input_tokens"],
            "ephemeral_1h_input_tokens": 0,
        },
        "output_tokens": 8,
        "service_tier": "standard",
        "inference_geo": "global",
        "speed": "standard",
    }
    message: dict[str, Any] = {
        "model": model,
        "id": message_id,
        "type": "message",
        "role": "assistant",
        "content": [],
        "container": None,
        "stop_reason": None,
        "stop_sequence": None,
        "stop_details": None,
        "usage": start_usage,
        "diagnostics": None,
    }
    events = [fake.SSEEvent("message_start", {"type": "message_start", "message": message})]
    index = 0
    if thinking is not None:
        events += [
            _block_start(index, {"type": "thinking", "thinking": "", "signature": ""}),
            _delta(index, {"type": "thinking_delta", "thinking": thinking}),
            _delta(index, {"type": "signature_delta", "signature": _SYNTHETIC_SIGNATURE}),
            fake.SSEEvent("content_block_stop", {"type": "content_block_stop", "index": index}),
        ]
        index += 1
    half = len(text) // 2
    events += [
        _block_start(index, {"type": "text", "text": ""}),
        fake.SSEEvent("ping", {"type": "ping"}),
        _delta(index, {"type": "text_delta", "text": text[:half]}),
        _delta(index, {"type": "text_delta", "text": text[half:]}),
        fake.SSEEvent("content_block_stop", {"type": "content_block_stop", "index": index}),
        fake.SSEEvent(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {
                    "stop_reason": stop_reason,
                    "stop_sequence": None,
                    "stop_details": None,
                    "container": None,
                },
                "usage": {**totals, "output_tokens_details": {"thinking_tokens": 0}},
            },
        ),
        fake.SSEEvent("message_stop", {"type": "message_stop"}),
    ]
    return fake.render_sse(events)


# The one response header a hand-authored stream keeps: the fixture PII gate allows it, the fake serves it.
SYNTHETIC_HEADERS: Final = {"content-type": "text/event-stream; charset=utf-8"}


def _sidecar_json(doc: Any) -> bytes:
    """A sidecar as the recorder writes one: indented, sorted keys, UTF-8, a final newline."""
    return (json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def write_synthetic_recording(
    root: Path,
    purpose: str,
    model: str,
    schema: type[BaseModel],
    body: Any,
    *,
    case: str,
    usage: dict[str, int],
) -> Path:
    """Write a hand-authored synthetic stream (Tasks 13 and 20) in the layout `llm/fake.py` serves, and return
    the stream's path, `root/<purpose>/<model>/<schema12>.sse` of the schema a request of `schema` sends:
    - the stream, `synthetic_sse(body, model=model, usage=usage)` with a message id of its schema key;
    - `<schema12>.headers.json`: status 200 and the content type only (`SYNTHETIC_HEADERS`), the one header
      the fixture PII gate (`tests/contract/test_fixture_pii.py`) and the fake both expect;
    - `<schema12>.components.json` labelled `"source": "synthetic"`: no recording, so the recorded-bounds
      check (`tests/payload/test_recorded_bounds.py`) skips it.
    The same arguments always give the same bytes, so a test can rebuild a committed fixture and compare."""
    sent = anthropic.transform_schema(schema)
    path = fake.recording_path(root, purpose, model, sent)
    path.parent.mkdir(parents=True, exist_ok=True)
    message_id = f"msg_synthetic_{fake.schema12(sent)}"
    path.write_bytes(synthetic_sse(body, model=model, usage=usage, message_id=message_id))
    path.with_suffix(".headers.json").write_bytes(
        _sidecar_json({"status": 200, "headers": SYNTHETIC_HEADERS})
    )
    components = {"source": "synthetic", "case": case, "purpose": purpose, "model": model, "usage": usage}
    path.with_suffix(".components.json").write_bytes(_sidecar_json(components))
    return path


def _block_start(index: int, block: dict[str, Any]) -> fake.SSEEvent:
    return fake.SSEEvent(
        "content_block_start", {"type": "content_block_start", "index": index, "content_block": block}
    )


def _delta(index: int, delta: dict[str, Any]) -> fake.SSEEvent:
    return fake.SSEEvent(
        "content_block_delta", {"type": "content_block_delta", "index": index, "delta": delta}
    )


@dataclass(frozen=True)
class ReceivedRequest:
    """A request as `LocalSSEServer` read it off its socket; header names are lower-case."""

    method: str
    path: str
    headers: dict[str, str]
    body: bytes = field(repr=False)


class _ToLocalServer(httpx2.AsyncBaseTransport):
    """Sends each request to `127.0.0.1:<port>` over plain HTTP with its headers unchanged, so its `Host` is
    still the API's; the request's timeouts travel with it."""

    def __init__(self, port: int) -> None:
        self._port = port
        self._inner = httpx2.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        url = request.url.copy_with(scheme="http", host="127.0.0.1", port=self._port)
        forwarded = httpx2.Request(
            request.method, url, headers=request.headers, stream=request.stream, extensions=request.extensions
        )
        return await self._inner.handle_async_request(forwarded)

    async def aclose(self) -> None:
        await self._inner.aclose()


class LocalSSEServer:
    """An HTTP/1.1 server on `127.0.0.1:0` that answers every request with `body` as `text/event-stream`.

    `stall_after_first_event` writes the first event and then nothing until the server closes;
    `stall_before_headers` writes nothing at all. Use it as `async with LocalSSEServer(...) as server`, and
    reach it with `make_http_client(cfg, mount=server.transport())`.
    """

    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        stall_after_first_event: bool = False,
        stall_before_headers: bool = False,
    ) -> None:
        self._body = body
        self._status = status
        self._stall_after_first_event = stall_after_first_event
        self._stall_before_headers = stall_before_headers
        self.requests: list[ReceivedRequest] = []
        self.first_event_sent = False
        self.port = 0
        self._server: asyncio.Server | None = None
        self._release = asyncio.Event()

    async def __aenter__(self) -> Self:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = int(self._server.sockets[0].getsockname()[1])
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self._release.set()  # every stalled answer ends, so its connection closes
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    def transport(self) -> httpx2.AsyncBaseTransport:
        return _ToLocalServer(self.port)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1").split("\r\n")
            method, target, _version = head[0].split(" ", 2)
            headers = {}
            for line in head[1:]:
                if line:
                    name, _, value = line.partition(":")
                    headers[name.strip().lower()] = value.strip()
            body = await reader.readexactly(int(headers.get("content-length", "0")))
            self.requests.append(ReceivedRequest(method, target, headers, body))
            if self._stall_before_headers:
                await self._release.wait()
                return
            status_line = f"HTTP/1.1 {self._status} X\r\n"
            writer.write(
                f"{status_line}content-type: text/event-stream\r\nconnection: close\r\n\r\n".encode()
            )
            if self._stall_after_first_event:
                first, separator, _rest = self._body.partition(b"\n\n")
                writer.write(first + separator)
                await writer.drain()
                self.first_event_sent = True
                await self._release.wait()
                return
            writer.write(self._body)
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            return  # the client went away
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()


# --- Task 5: the gateway on the fake, and the C35 helpers --------------------------------------------------


class GatewayOut(BaseModel):
    """The structured-output schema of the gateway tests (synthetic, no profile shape)."""

    model_config = ConfigDict(extra="forbid")

    answer: str
    score: int


GATEWAY_BODY: Final[dict[str, JSONValue]] = {"answer": "a synthetic answer", "score": 3}
# The purposes the `fake_llm` fixture records `GatewayOut` for, on every model.
GATEWAY_PURPOSES: Final = ("extract_profile", "propose_queries", "schema_dry_run")
# A non-scoring node of an operation: `extract_profile`'s purpose with small stages and its 900 s deadline.
GATEWAY_NODE: Final = NodeConfig(
    purpose="extract_profile",
    model="claude-opus-5-5",
    effort="medium",
    max_tokens=1000,
    max_tokens_retry=2000,
    deadline_s=900,
    schema=GatewayOut,
)


def gateway_prompt(text: str = "A synthetic question.", *, cached_prefix: bool = False) -> RenderedPrompt:
    """A synthetic prompt: one system block (cached when `cached_prefix`) and one user message."""
    return RenderedPrompt(
        system=(SystemBlock("You answer in JSON.", cache=cached_prefix),),
        messages=(HumanMessage(text),),
        template_version="gateway-test@1",
        input_refs={"test": "gateway"},
    )


class Sleeps:
    """An injected `sleep` that records each wait and only yields to the loop."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)
        await asyncio.sleep(0)


class FakeLLM(FakeAnthropic):
    """The fake over recordings written for the test (`GatewayOut` for every model and `GATEWAY_PURPOSES`),
    with gateways on it. `served` counts the message tries that reached the fake; `sleeps` records the backoff
    waits of every gateway made here with the default sleep."""

    def __init__(self, cfg: AppConfig) -> None:
        super().__init__(cfg)
        self.sleeps = Sleeps()
        self._clients: list[httpx2.AsyncClient] = []
        self.handlers: dict[ops.OperationKind, ops.OperationHandler] = {}  # registered for this test only

    def record(
        self,
        purpose: str,
        model: str,
        body: Any = GATEWAY_BODY,
        *,
        schema: type[BaseModel] = GatewayOut,
        **sse: Any,
    ) -> Path:
        """Write the stream the fake serves for `(purpose, model, schema)`: `synthetic_sse(body, **sse)`."""
        path = fake.recording_path(self.root, purpose, model, anthropic.transform_schema(schema))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(synthetic_sse(body, model=model, **sse))
        return path

    def gateway(
        self,
        engine: AsyncEngine,
        *,
        read_timeout_s: float = 120.0,
        sleep: Callable[[float], Any] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        cache: CacheMonitor | None = None,
    ) -> LLMGateway:
        """A gateway on a fresh fake client (closed after the test)."""
        client = self.client(read_timeout_s=read_timeout_s)
        self._clients.append(client)
        return LLMGateway(
            engine,
            self.cfg,
            http_client=client,
            monotonic=monotonic,
            sleep=self.sleeps if sleep is None else sleep,
            cache=CacheMonitor() if cache is None else cache,
        )

    async def aclose(self) -> None:
        for client in self._clients:
            await client.aclose()


_active_fake: list[FakeLLM] = []


@pytest.fixture
async def fake_llm(tmp_path: Path) -> AsyncIterator[FakeLLM]:
    """The fake over `GatewayOut` recordings under `tmp_path`, for the gateway; any operation handler that
    `create_llm_call_operation` registered is removed after the test."""
    root = tmp_path / "anthropic"
    fake_llm = FakeLLM(fake_llm_config(root, tmp_path / "no-secrets"))
    for purpose in GATEWAY_PURPOSES:
        for model in MODELS:
            fake_llm.record(purpose, model)
    _active_fake.append(fake_llm)
    try:
        yield fake_llm
    finally:
        _active_fake.remove(fake_llm)
        for kind, handler in fake_llm.handlers.items():
            if ops.HANDLERS.get(kind) is handler:
                del ops.HANDLERS[kind]
        await fake_llm.aclose()


async def active_execution_config(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(sa.text(_ACTIVE_EXECUTION_CONFIG))).scalar_one())


def call_context(owner: SpendOwner, execution_config_id: int, **overrides: Any) -> CallContext:
    """A non-scoring call of `owner` with `GATEWAY_NODE`'s deadline from now; `overrides` replace fields."""
    values: dict[str, Any] = {
        "owner": owner,
        "purpose_class": "nonscoring",
        "execution_config_id": execution_config_id,
        "deadline": time.monotonic() + GATEWAY_NODE.deadline_s,
        **overrides,
    }
    return CallContext(**values)


async def create_llm_call_operation(engine: AsyncEngine) -> int:
    """A `pending` operation of kind `eval_run` whose handler, registered for this test only (the `fake_llm`
    fixture removes it), makes one gateway call of `GATEWAY_NODE` as the operation's spend owner. The handler
    closes over its gateway and uses nothing of the operation context but the claim (no `deps`)."""
    if not _active_fake:
        raise RuntimeError("create_llm_call_operation needs the fake_llm fixture")
    fake_llm = _active_fake[-1]
    gateway = fake_llm.gateway(engine)
    execution_config_id = await active_execution_config(engine)

    async def handler(ctx: ops.OperationContext) -> None:
        owner = OperationOwner(ctx.claim)
        await gateway.call(GATEWAY_NODE, gateway_prompt(), call_context(owner, execution_config_id))

    if ops.HANDLERS.get("eval_run", handler) is not handler:
        raise RuntimeError("another eval_run handler is registered")
    ops.HANDLERS["eval_run"] = fake_llm.handlers["eval_run"] = handler
    async with engine.begin() as conn:
        op_id, _ = await ops.create_operation(
            conn, kind="eval_run", payload={}, dedup_key=f"llm-call:{uuid.uuid4()}", defer=None
        )
    return op_id


async def reclaim_stale_now(engine: AsyncEngine, op_id: int) -> None:
    """The `operations` reconciler's reclaim statement alone, with `now` past `OPERATION_STALE`: every stale
    `running` operation back to `pending`, `claim_id := NULL`, one more attempt. `op_id` must be one."""
    stale = utcnow() + ops.OPERATION_STALE + timedelta(seconds=1)
    async with engine.begin() as conn:
        reclaimed = await ops._reclaim_stale(conn, now=stale)
    assert op_id in [operation_id for operation_id, _ in reclaimed]


_OPERATION_LEDGER: Final = sa.text(
    "SELECT l.state, l.evidence FROM llm_spend_ledger l JOIN llm_calls c ON c.spend_ledger_id = l.id "
    "WHERE c.input_refs -> 'owner' ->> 'kind' = 'operation' "
    "AND (c.input_refs -> 'owner' ->> 'id')::bigint = :op ORDER BY l.id"
)


async def ledger_states(engine: AsyncEngine, op_id: int) -> list[str]:
    """The ledger states of operation `op_id`'s tries, oldest first: `state`, or `state:evidence`."""
    async with engine.connect() as conn:
        rows = await conn.execute(_OPERATION_LEDGER, {"op": op_id})
        return [row.state if row.evidence is None else f"{row.state}:{row.evidence}" for row in rows]


@dataclass(frozen=True)
class Try:
    """One try as the ledger and its call record it, oldest first (`tries`)."""

    ledger_id: int
    state: str
    evidence: str | None
    reserved_usd: Decimal
    settled_usd: Decimal | None
    input_tokens_bound: int
    budget_month: date
    call_id: int
    status: str
    error_code: str | None
    http_status: int | None
    stop_reason: str | None
    refusal_category: str | None
    model_served: str | None
    provider_request_id: str | None
    max_tokens: int
    max_tokens_stage: str
    transport_try: int
    logical_call_no: int | None
    input_tokens: int | None
    cache_read_input_tokens: int | None
    output_tokens: int | None
    cost_usd: Decimal | None
    output_json: Any
    first_event_at: datetime | None


_TRIES: Final = sa.text(
    "SELECT l.id AS ledger_id, l.state, l.evidence, l.reserved_usd, l.settled_usd, l.input_tokens_bound, "
    "l.budget_month, c.id AS call_id, c.status, c.error_code, c.http_status, c.stop_reason, "
    "c.refusal_category, c.model_served, c.provider_request_id, c.max_tokens, c.max_tokens_stage, "
    "c.transport_try, c.logical_call_no, c.input_tokens, c.cache_read_input_tokens, c.output_tokens, "
    "c.cost_usd, c.output_json, c.first_event_at "
    "FROM llm_spend_ledger l JOIN llm_calls c ON c.spend_ledger_id = l.id ORDER BY l.id"
)


async def tries(engine: AsyncEngine) -> list[Try]:
    """Every try in the database, oldest first."""
    async with engine.connect() as conn:
        return [Try(**row._mapping) for row in await conn.execute(_TRIES)]


# --- Task 6: the payload contract tests (§10.9) ------------------------------------------------------------

PAYLOAD_KEY: Final = SecretStr("payload-test-key-0001")  # never a real key: nothing leaves the process
# The request keys no call may carry (G6, G11).
BANNED_REQUEST_KEYS: Final = ("tool_choice", "tools", "temperature", "top_p", "top_k", "fallbacks", "betas")


@dataclass(frozen=True)
class CapturedRequest:
    """One request as `CapturingTransport` received it."""

    method: str
    url: httpx2.URL
    headers: httpx2.Headers
    body: dict[str, Any] = field(repr=False)


class CapturingTransport(httpx2.AsyncBaseTransport):
    """The transport of the API origin in a payload test (`make_http_client(cfg, mount=…)`): it keeps every
    request and answers each with `answer`, a 200 SSE stream. Nothing leaves the process: every other origin
    is unreachable, so a request that does not name the API origin fails instead of being captured."""

    def __init__(self, answer: bytes) -> None:
        self._answer = answer
        self.requests: list[CapturedRequest] = []

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(await request.aread())
        self.requests.append(CapturedRequest(request.method, request.url, request.headers, body))
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream; charset=utf-8", "request-id": "req_payload_0001"},
            content=self._answer,
        )


async def capture_request(
    cfg: AppConfig, node: NodeConfig, prompt: RenderedPrompt, answer: Any
) -> CapturedRequest:
    """The request one `gateway._chat` try sends for `node` (its model, effort, first `max_tokens` stage and
    schema) with `prompt`, on a capturing transport that answers with `answer` as the structured output. The
    answer must parse as `node.schema`, so the whole try ran. No database and no network: the `payload` CI
    job has neither."""
    transport = CapturingTransport(synthetic_sse(answer, model=node.model))
    async with make_http_client(cfg, mount=transport) as client:
        runnable = _chat(
            node.model,
            node.effort,
            node.max_tokens,
            node.schema,
            api_key=PAYLOAD_KEY,
            http_client=client,
            purpose=node.purpose,
        )
        out = await runnable.ainvoke(to_messages(prompt))
    assert out["parsing_error"] is None and isinstance(out["parsed"], node.schema)
    (request,) = transport.requests
    return request


def cache_control_paths(doc: Any, path: str = "") -> list[str]:
    """The paths of every `cache_control` key in `doc`."""
    found: list[str] = []
    if isinstance(doc, dict):
        for key, value in doc.items():
            if key == "cache_control":
                found.append(f"{path}/{key}")
            found.extend(cache_control_paths(value, f"{path}/{key}"))
    elif isinstance(doc, list):
        for i, item in enumerate(doc):
            found.extend(cache_control_paths(item, f"{path}/{i}"))
    return found


def assert_single_call_payload(
    request: CapturedRequest,
    node: NodeConfig,
    *,
    model_policy: dict[str, JSONValue],
    execution: dict[str, JSONValue],
) -> None:
    """The §10.9 assertions for a single (uncached) call of `node`, against the stored policy and execution
    config it was configured from: the API host, the json_schema output format with exactly the transformed
    schema, the policy's model and effort, the execution config's first `max_tokens` stage, `stream`, none
    of the banned keys, a user turn last, and no `cache_control` anywhere (§10.6: a single call)."""
    policy_node = get_object(model_policy, f"nodes.{node.purpose}")
    stages = get_object(execution, node.purpose)
    assert (request.method, request.url.scheme, request.url.host, request.url.path) == (
        "POST",
        "https",
        "api.anthropic.com",
        "/v1/messages",
    )
    assert request.headers["x-api-key"] == PAYLOAD_KEY.get_secret_value()
    assert "anthropic-beta" not in request.headers  # no betas (G6)
    body = request.body
    assert body["model"] == policy_node["model"] == node.model
    assert body["output_config"]["format"] == {"type": "json_schema", "schema": transformed(node.schema)}
    assert body["output_config"]["effort"] == policy_node["effort"]
    assert body["max_tokens"] == stages["max_tokens"]
    assert body["stream"] is True
    assert not set(BANNED_REQUEST_KEYS) & set(body), sorted(set(BANNED_REQUEST_KEYS) & set(body))
    assert "budget_tokens" not in (body.get("thinking") or {})  # effort only, never a thinking budget (G6)
    assert body["messages"] and body["messages"][-1]["role"] == "user"
    assert {message["role"] for message in body["messages"]} == {"user"}  # never an assistant prefill
    assert cache_control_paths(body) == []
