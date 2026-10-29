"""scripts/spikes/record_anthropic.py (M3 Task 26, Codex r1 #40): `BudgetedTransport` authorises every paid
HTTP attempt before it leaves, and `TimingTransport` stamps what comes back.

`BudgetedTransport` counts each attempt in the budget file at its worst case (the gateway's own text bound
of what it sends) against the spike's `SPIKE_CALLS` allowance and `MAX_ACTUAL_USD`, then settles it from its
answer. An attempt the budget refuses is answered here with a synthetic 400 `invalid_request_error`: it never
reaches the network, and the gateway takes it as a definitive request error and sends nothing more for it.

Most tests drive the transport directly over a faulting fake network (`httpx2.MockTransport`). The last ones
run the real gateway loop on the recorded-SSE fake and the shared test database: a 529 on the first attempt
leads to a second authorised attempt, and an attempt beyond the allowance is answered 400 locally, with
nothing sent and no retry. No key is read and nothing leaves the process.
"""

import contextlib
import importlib.util
import itertools
import json
import shutil
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import httpx2
import pytest
from jobhunter.llm import fake, pricing
from jobhunter.llm.bounds import PROMPT_OVERHEAD_TOKENS
from jobhunter.llm.cache_monitor import CacheMonitor
from jobhunter.llm.errors import LLMRequestError
from jobhunter.llm.gateway import LLMGateway
from jobhunter.llm.schema_dry_run import PROBE_ONLY_SCHEMA
from jobhunter.llm.schemas import SCHEMAS_V2, transformed
from jobhunter.llm.schemas.queries import QueryProposalOut
from jobhunter.llm.spend_ledger import EvalRunOwner
from jobhunter.llm.transport import make_http_client
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.llm import (
    FAKE_LLM_DIR,
    GATEWAY_NODE,
    FakeLLM,
    Sleeps,
    active_execution_config,
    call_context,
    critic_body,
    gateway_prompt,
    generator_wire_body,
    synthetic_sse,
    tries,
)

REPO = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
SCHEMA = transformed(QueryProposalOut)
MODEL: Final = "claude-sonnet-5-5"
USAGE = {
    "input_tokens": 900,
    "cache_creation_input_tokens": 4100,
    "cache_read_input_tokens": 0,
    "output_tokens": 300,
}


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts/spikes" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rec = _load("record_anthropic")


def request_body(
    model: str = MODEL, *, max_tokens: int = 6000, text: str = "A synthetic ad."
) -> dict[str, Any]:
    """A message request as the gateway sends one: three system blocks, the marker on the second."""
    return {
        "model": model,
        "max_tokens": max_tokens,
        "stream": True,
        "system": [
            {"type": "text", "text": "A synthetic rubric block."},
            {
                "type": "text",
                "text": "<candidate_profile>{}</candidate_profile>",
                "cache_control": {"type": "ephemeral"},
            },
            {"type": "text", "text": "A synthetic role."},
        ],
        "messages": [{"role": "user", "content": text}],
        "output_config": {"format": {"type": "json_schema", "schema": SCHEMA}, "effort": "medium"},
        "thinking": {"type": "adaptive"},
    }


def expected_worst(body: dict[str, Any], *, batch: bool = False) -> Decimal:
    """The gateway's reservation of `body`, computed here on its own: the UTF-8 bytes of every system text,
    message text and the schema as canonical JSON, plus the model's overhead, at `pricing.worst_case_usd`."""
    sizes = [len(block["text"].encode()) for block in body["system"]]
    sizes += [len(message["content"].encode()) for message in body["messages"]]
    sizes.append(len(fake.canonical_schema_json(body["output_config"]["format"]["schema"]).encode()))
    bound = sum(sizes) + PROMPT_OVERHEAD_TOKENS[body["model"]]
    return pricing.worst_case_usd(
        body["model"], input_tokens_bound=bound, max_tokens=body["max_tokens"], batch=batch
    )


def stream_answer(request: httpx2.Request) -> httpx2.Response:
    model = json.loads(request.content)["model"]
    body = synthetic_sse({"queries": []}, model=model, usage=USAGE)
    return httpx2.Response(200, headers={"content-type": "text/event-stream; charset=utf-8"}, content=body)


def error_answer(status: int, kind: str) -> httpx2.Response:
    return httpx2.Response(status, json={"type": "error", "error": {"type": kind, "message": "synthetic"}})


class Network:
    """A faulting fake network: answers each request with the next handler of `script` (the last repeats)
    and keeps what reached it."""

    def __init__(self, *script: Callable[[httpx2.Request], httpx2.Response]) -> None:
        self.script = script
        self.requests: list[httpx2.Request] = []

    def transport(self) -> httpx2.AsyncBaseTransport:
        def handle(request: httpx2.Request) -> httpx2.Response:
            self.requests.append(request)
            return self.script[min(len(self.requests), len(self.script)) - 1](request)

        return httpx2.MockTransport(handle)


def budgeted(tmp: Path, network: Network, *, spike: str = "v14", case: str | None = "call") -> Any:
    transport = rec.BudgetedTransport(
        network.transport(), budget=rec.Budget(tmp / "raw"), spike=spike, now=lambda: NOW
    )
    transport.case = case
    return transport


async def post(
    transport: Any, payload: Any, path: str = "/v1/messages", method: str = "POST"
) -> httpx2.Response:
    async with httpx2.AsyncClient(transport=transport) as client:
        return await client.request(method, f"https://api.anthropic.com{path}", json=payload)


def ledger(tmp: Path) -> list[dict[str, Any]]:
    path = tmp / "raw/anthropic-budget.json"
    entries: list[dict[str, Any]] = json.loads(path.read_text("utf-8"))["entries"] if path.exists() else []
    return entries


def seed(tmp: Path, *entries: dict[str, Any]) -> None:
    (tmp / "raw").mkdir(parents=True, exist_ok=True)
    (tmp / "raw/anthropic-budget.json").write_text(json.dumps({"entries": list(entries)}), "utf-8")


# --- each attempt authorised, counted and settled --------------------------------------------------------


async def test_an_attempt_is_counted_at_its_worst_case_before_it_leaves_and_settled_from_its_usage(
    tmp_path: Path,
) -> None:
    seen: list[dict[str, Any]] = []

    def check_then_answer(request: httpx2.Request) -> httpx2.Response:
        seen.append(ledger(tmp_path)[-1])  # what the budget file holds while the attempt is on the wire
        return stream_answer(request)

    body = request_body()
    response = await post(budgeted(tmp_path, Network(check_then_answer)), body)
    assert response.status_code == 200
    (sending,) = seen
    assert (sending["spike"], sending["case"], sending["logical_case"], sending["attempt"]) == (
        "v14",
        "call#1",
        "call",
        1,
    )
    assert sending["state"] == "sending" and sending["settled_usd"] is None
    assert Decimal(sending["worst_case_usd"]) == expected_worst(body)
    (done,) = ledger(tmp_path)
    usage = pricing.Usage(900, 300, 4100, 0)
    assert (done["state"], done["status"], done["settlement"]) == ("done", 200, "billed")
    assert Decimal(done["settled_usd"]) == pricing.settled_usd(MODEL, usage)
    assert done["usage"] == USAGE and done["stop_reason"] == "end_turn"


async def test_a_529_then_a_stream_is_two_authorised_attempts(tmp_path: Path) -> None:
    network = Network(lambda r: error_answer(529, "overloaded_error"), stream_answer)
    transport = budgeted(tmp_path, network)
    assert (await post(transport, request_body())).status_code == 529
    assert (await post(transport, request_body())).status_code == 200
    first, second = ledger(tmp_path)
    assert (first["case"], first["status"], first["settled_usd"], first["settlement"]) == (
        "call#1",
        529,
        "0.000000",
        "not_billed:error_response_no_content",
    )
    assert (second["case"], second["attempt"], second["settlement"]) == ("call#2", 2, "billed")
    assert transport.stopped is None and transport.refusals == [] and len(network.requests) == 2


async def test_an_attempt_beyond_the_allowance_is_answered_400_locally_with_nothing_sent(
    tmp_path: Path,
) -> None:
    network = Network(stream_answer)
    transport = budgeted(tmp_path, network)  # v14: 2 attempts
    assert rec.SPIKE_CALLS["v14"] == 2
    for _ in range(2):
        assert (await post(transport, request_body())).status_code == 200
    transport.case = "another"
    refused = await post(transport, request_body())
    assert refused.status_code == 400
    error = refused.json()["error"]
    assert error["type"] == "invalid_request_error" and "v14 has sent 2 of its 2 calls" in error["message"]
    assert len(network.requests) == 2 and len(ledger(tmp_path)) == 2  # never sent, never counted
    assert transport.refusals == ["v14 has sent 2 of its 2 calls"]


@pytest.mark.parametrize(("over", "sent"), [("0", True), ("0.000001", False)], ids=["at-10.00", "past-10.00"])
async def test_an_attempt_past_the_total_is_refused_before_anything_is_sent(
    tmp_path: Path, over: str, sent: bool
) -> None:
    body = request_body()
    settled = rec.MAX_ACTUAL_USD - expected_worst(body) + Decimal(over)
    seed(
        tmp_path,
        {
            "spike": "v13",
            "case": "earlier",
            "state": "done",
            "worst_case_usd": "9",
            "settled_usd": str(settled),
        },
    )
    network = Network(stream_answer)
    response = await post(budgeted(tmp_path, network), body)
    assert (response.status_code, len(network.requests), len(ledger(tmp_path))) == (
        (200, 1, 2) if sent else (400, 0, 1)
    )
    if not sent:
        assert f"above ${rec.MAX_ACTUAL_USD}" in response.json()["error"]["message"]


async def test_a_call_counted_by_an_earlier_run_is_never_sent_again(tmp_path: Path) -> None:
    earlier = {
        "spike": "v14",
        "case": "call#1",
        "logical_case": "call",
        "attempt": 1,
        "state": "done",
        "worst_case_usd": "0.2",
        "settled_usd": "0.01",
    }
    seed(tmp_path, earlier)
    network = Network(stream_answer)
    refused = await post(budgeted(tmp_path, network), request_body())
    assert refused.status_code == 400 and "never sent twice" in refused.json()["error"]["message"]
    assert network.requests == [] and ledger(tmp_path) == [earlier]


async def test_a_definitive_error_stops_every_later_attempt(tmp_path: Path) -> None:
    network = Network(lambda r: error_answer(400, "invalid_request_error"))
    transport = budgeted(tmp_path, network, spike="v5")
    assert (await post(transport, request_body())).status_code == 400
    transport.case = "next"
    refused = await post(transport, request_body())
    assert "the spike stopped" in refused.json()["error"]["message"]
    assert transport.stopped == "call#1 was answered http 400"
    assert len(network.requests) == 1 and [e["case"] for e in ledger(tmp_path)] == ["call#1"]


async def test_a_stream_cut_before_its_end_stays_at_its_worst_case(tmp_path: Path) -> None:
    def cut(request: httpx2.Request) -> httpx2.Response:
        events = fake.parse_sse(stream_answer(request).content)
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, content=fake.render_sse(events[:3])
        )

    body = request_body()
    transport = budgeted(tmp_path, Network(cut), spike="v5")
    assert (await post(transport, body)).status_code == 200
    (done,) = ledger(tmp_path)
    assert (done["state"], done["settlement"]) == ("done", "ambiguous:kept_at_worst_case")
    assert Decimal(done["settled_usd"]) == expected_worst(body)
    assert transport.stopped is None  # a lost stream is the gateway's to retry


async def test_a_connect_failure_before_the_send_settles_nothing(tmp_path: Path) -> None:
    def refuse_connection(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("synthetic: connection refused", request=request)

    with pytest.raises(httpx2.ConnectError):
        await post(budgeted(tmp_path, Network(refuse_connection), spike="v5"), request_body())
    (done,) = ledger(tmp_path)
    assert (done["settled_usd"], done["settlement"], done["error"]) == (
        "0.000000",
        "not_billed:connect_failed_before_send",
        "ConnectError",
    )


async def test_an_attempt_that_matches_no_planned_call_is_refused(tmp_path: Path) -> None:
    network = Network(stream_answer)
    transport = rec.BudgetedTransport(
        network.transport(), budget=rec.Budget(tmp_path / "raw"), spike="v5", now=lambda: NOW
    )
    refused = await post(transport, request_body())
    assert refused.status_code == 400 and "no call the spike planned" in refused.json()["error"]["message"]
    assert network.requests == [] and ledger(tmp_path) == []
    # the spike's own matcher names a planned call by its request
    planned = rec.BudgetedTransport(
        network.transport(),
        budget=rec.Budget(tmp_path / "raw"),
        spike="v5",
        now=lambda: NOW,
        planned=lambda body: "matched" if body["model"] == MODEL else None,
    )
    assert (await post(planned, request_body())).status_code == 200
    assert [e["case"] for e in ledger(tmp_path)] == ["matched#1"]


async def test_a_request_without_a_byte_bound_is_refused(tmp_path: Path) -> None:
    body = request_body()
    body["messages"] = [{"role": "user", "content": [{"type": "document", "source": {"type": "base64"}}]}]
    network = Network(stream_answer)
    refused = await post(budgeted(tmp_path, network, spike="v5"), body)
    assert refused.status_code == 400 and "no bound" in refused.json()["error"]["message"]
    assert network.requests == []


async def test_the_free_routes_pass_uncounted_and_every_other_route_is_refused(tmp_path: Path) -> None:
    network = Network(lambda r: httpx2.Response(200, json={"input_tokens": 12}))
    transport = budgeted(tmp_path, network, spike="v5")
    counted = await post(transport, {"model": MODEL, "messages": []}, "/v1/messages/count_tokens")
    assert counted.json() == {"input_tokens": 12}
    assert (await post(transport, None, "/v1/messages/batches/msgbatch_x", "GET")).status_code == 200
    assert (await post(transport, None, "/v1/messages/batches/msgbatch_x/results", "GET")).status_code == 200
    assert (await post(transport, None, "/v1/messages/batches", "GET")).status_code == 200
    assert len(network.requests) == 4 and ledger(tmp_path) == []
    for method, path in (("POST", "/v1/messages/batches/msgbatch_x/cancel"), ("GET", "/v1/models")):
        refused = await post(transport, {}, path, method)
        assert refused.status_code == 400 and "no route a spike sends" in refused.json()["error"]["message"]
    assert len(network.requests) == 4


async def test_a_batch_create_reserves_the_sum_of_its_items_and_stays_open_until_settled(
    tmp_path: Path,
) -> None:
    params = [{k: v for k, v in request_body(text=f"Ad {i}.").items() if k != "stream"} for i in (1, 2, 3)]
    items = [{"custom_id": f"t{i}_a1", "params": p} for i, p in enumerate(params, start=1)]
    created = {"id": "msgbatch_synthetic", "type": "message_batch", "processing_status": "in_progress"}
    network = Network(lambda r: httpx2.Response(200, json=created))
    transport = budgeted(tmp_path, network, spike="v12", case=None)
    transport.case = "batch"
    response = await post(transport, {"requests": items}, "/v1/messages/batches")
    assert response.json()["id"] == "msgbatch_synthetic"
    (entry,) = ledger(tmp_path)
    worst = sum((expected_worst(p, batch=True) for p in params), Decimal(0))
    assert Decimal(entry["worst_case_usd"]) == worst and entry["items"] == 3 and entry["batch"] is True
    # counted at its worst case until the results are read: the budget holds it as not done
    assert (entry["state"], entry["provider_batch_id"], entry["settled_usd"]) == (
        "created",
        "msgbatch_synthetic",
        None,
    )
    assert rec.Budget(tmp_path / "raw").unsettled_worst_case_usd() == worst


# --- the timing transport --------------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        self.now += 0.5
        return self.now


class ChunkedStream(httpx2.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk


async def test_the_timing_transport_stamps_every_chunk_and_keeps_the_exact_request_body(
    tmp_path: Path,
) -> None:
    sse = synthetic_sse({"queries": []}, model=MODEL)
    events = fake.parse_sse(sse)
    # cut the stream mid-event and mid-character: an event is stamped with the chunk that completes it
    cut = sse.index(b"event: content_block_delta") + 7
    chunks = [sse[:cut], sse[cut:-10], sse[-10:]]

    def answer(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, stream=ChunkedStream(chunks)
        )

    clock = Clock()
    timing = rec.TimingTransport(httpx2.MockTransport(answer), clock=clock)
    content = json.dumps(request_body(), separators=(",", ":")).encode()
    async with httpx2.AsyncClient(transport=timing) as client:
        response = await client.post(
            "https://api.anthropic.com/v1/messages", content=content, headers={"x-api-key": "fake-key-0001"}
        )
        assert response.content == sse
    (exchange,) = timing.exchanges
    assert exchange.request_content == content and exchange.request_body() == request_body()
    assert "x-api-key" not in exchange.request_headers  # never the key
    assert exchange.request_headers["accept-encoding"] == "identity"
    assert [len(c) for _, c in exchange.chunks] == [len(c) for c in chunks]
    stamps = [at for at, _ in exchange.chunks]
    assert exchange.started < exchange.headers_at < stamps[0] < stamps[1] < stamps[2] <= exchange.ended
    timed = exchange.event_times()
    assert [e for _, e in timed] == events
    # the events before the cut arrive with the first chunk, the cut one with the second
    first = sum(1 for e in events[: [e.name for e in events].index("content_block_delta")])
    assert {at for at, _ in timed[:first]} == {stamps[0]} and timed[first][0] == stamps[1]
    assert exchange.max_gap_s() == max(b[0] - a[0] for a, b in itertools.pairwise(timed))


async def test_a_refused_attempt_never_reaches_the_timing_transport(tmp_path: Path) -> None:
    network = Network(stream_answer)
    timing = rec.TimingTransport(network.transport())
    transport = rec.BudgetedTransport(
        timing, budget=rec.Budget(tmp_path / "raw"), spike="v14", now=lambda: NOW
    )
    transport.case = "call"
    for _ in range(3):
        await post(transport, request_body())
    assert [e.case for e in timing.exchanges] == ["call#1", "call#2"]  # the third was refused before it
    assert len(network.requests) == 2


# --- through the real gateway on the fake ----------------------------------------------------------------


@pytest.mark.integration
async def test_the_gateway_retries_a_529_on_a_second_attempt_and_takes_a_local_400_as_definitive(
    engine: AsyncEngine, fake_llm: FakeLLM, eval_owner: EvalRunOwner, tmp_path: Path
) -> None:
    fake_llm.scenario("overloaded_529", "ok")
    timing = rec.TimingTransport(fake.RecordedSSETransport(fake_llm.root))
    transport = rec.BudgetedTransport(
        timing, budget=rec.Budget(tmp_path / "raw"), spike="v14", now=lambda: NOW
    )
    ec = await active_execution_config(engine)
    sleeps = Sleeps()
    async with make_http_client(fake_llm.cfg, mount=transport) as client:
        gateway = LLMGateway(engine, fake_llm.cfg, http_client=client, sleep=sleeps, cache=CacheMonitor())

        def context() -> Any:
            return call_context(eval_owner, ec, eval_run_id=eval_owner.eval_run_id)

        transport.case = "first"
        result = await gateway.call(GATEWAY_NODE, gateway_prompt(), context())
        assert result.stop_reason == "end_turn"
        first, second = ledger(tmp_path)
        assert [(e["case"], e["status"], e["settlement"]) for e in (first, second)] == [
            ("first#1", 529, "not_billed:error_response_no_content"),
            ("first#2", 200, "billed"),
        ]
        assert len(sleeps.waits) == 1 and fake_llm.served == 2
        # v14's allowance of 2 attempts is spent: the next call's attempt is answered 400 here
        transport.case = "second"
        with pytest.raises(LLMRequestError) as refused:
            await gateway.call(GATEWAY_NODE, gateway_prompt(), context())
    assert refused.value.status == 400
    assert fake_llm.served == 2 and len(timing.exchanges) == 2  # nothing more reached the fake
    assert len(sleeps.waits) == 1  # and the gateway did not retry the local 400
    assert len(ledger(tmp_path)) == 2
    *_, last = await tries(engine)
    assert (last.state, last.evidence, last.http_status, last.error_code) == (
        "not_billed",
        "error_response_no_content",
        400,
        "http_400",
    )
    assert transport.refusals == ["v14 has sent 2 of its 2 calls"]


# --- the recorder's gateway mode end to end on the fake --------------------------------------------------


def shared_database(engine: AsyncEngine) -> Callable[[], Any]:
    """The recorder's throwaway database replaced by the shared test database (tests only)."""

    @contextlib.asynccontextmanager
    async def database() -> AsyncIterator[AsyncEngine]:
        yield engine

    return database


def v2_dry_run_answers(tmp: Path) -> Path:
    """Answers for the four v2 cases on the fake: the committed critic, profile and queries streams (version
    2 keeps those schemas), a synthetic C answer on Sonnet, and the probe's count answer."""
    root = tmp / "fake-anthropic"
    for directory in ("schema_dry_run", fake.COUNT_DIR):
        shutil.copytree(FAKE_LLM_DIR / directory, root / directory)
    c = transformed(SCHEMAS_V2["generator"])
    fake.recording_path(root, "schema_dry_run", "claude-sonnet-5-5", c).write_bytes(
        synthetic_sse(generator_wire_body(), model="claude-sonnet-5-5")
    )
    refusal = {
        "type": "error",
        "error": {"type": "invalid_request_error", "message": "synthetic: no minimum"},
    }
    count = fake.count_recording_path(root, "claude-sonnet-5-5", PROBE_ONLY_SCHEMA)
    count.write_text(json.dumps({"status": 400, "body": refusal}), "utf-8")
    return root


@pytest.mark.integration
async def test_the_v4v2_run_records_a_passed_live_dry_run_and_commits_its_four_streams(
    engine: AsyncEngine, clean_db: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    answers = v2_dry_run_answers(tmp_path)
    code = await rec.main_async(
        ["--spike", "v4v2", "--live"],
        root=tmp_path / "raw",
        fixtures_out=tmp_path / "fixtures",
        environ={"JOBHUNTER_ANTHROPIC_API_KEY": "fake-anthropic-key-0001"},
        transport_factory=lambda: fake.RecordedSSETransport(answers),
        database=shared_database(engine),
    )
    out = capsys.readouterr().out
    assert code == rec.OK, out
    entries = ledger(tmp_path)
    assert [(e["case"], e["state"], e["settlement"]) for e in entries] == [
        (f"{name}#1", "done", "billed")
        for name in (
            "generator-claude-sonnet-5-5",
            "critic-claude-opus-5-5",
            "profile-claude-opus-5-5",
            "queries-claude-opus-5-5",
        )
    ]
    report = json.loads((tmp_path / "raw/anthropic/v4v2/report.json").read_text("utf-8"))
    assert (report["passed"], report["metrics_transport"], report["status"]) == (True, "live", "completed")
    # the four answers where the fake serves them, and the probe's count answer
    for schema, model in (
        (transformed(SCHEMAS_V2["generator"]), "claude-sonnet-5-5"),
        (transformed(SCHEMAS_V2["critic"]), "claude-opus-5-5"),
        (transformed(SCHEMAS_V2["profile"]), "claude-opus-5-5"),
        (transformed(SCHEMAS_V2["queries"]), "claude-opus-5-5"),
    ):
        path = fake.recording_path(tmp_path / "fixtures", "schema_dry_run", model, schema)
        assert (
            path.is_file() and json.loads(path.with_suffix(".components.json").read_text())["spike"] == "v4v2"
        )
    probe = fake.count_recording_path(tmp_path / "fixtures", "claude-sonnet-5-5", PROBE_ONLY_SCHEMA)
    assert json.loads(probe.read_text("utf-8"))["status"] == 400
    # a second run sends nothing: every case is counted
    again = await rec.main_async(
        ["--spike", "v4v2", "--live"],
        root=tmp_path / "raw",
        fixtures_out=tmp_path / "fixtures",
        environ={"JOBHUNTER_ANTHROPIC_API_KEY": "fake-anthropic-key-0001"},
        transport_factory=lambda: (_ for _ in ()).throw(AssertionError("nothing is sent twice")),
        database=shared_database(engine),
    )
    assert again == rec.OK and len(ledger(tmp_path)) == 4


@pytest.mark.integration
async def test_the_v5_run_sends_its_twenty_calls_one_at_a_time_and_commits_each_stream(
    engine: AsyncEngine, clean_db: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "fake-anthropic"
    generator = transformed(SCHEMAS_V2["generator"])
    critic = transformed(SCHEMAS_V2["critic"])
    for purpose, model, schema, body in (
        ("generate", "claude-sonnet-5-5", generator, generator_wire_body()),
        ("critic", "claude-opus-5-5", critic, critic_body()),
    ):
        path = fake.recording_path(root, purpose, model, schema)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(synthetic_sse(body, model=model, usage={"cache_read_input_tokens": 4_400}))
    fake.count_tokens(1_234)
    code = await rec.main_async(
        ["--spike", "v5", "--live", "--through-gateway"],
        root=tmp_path / "raw",
        fixtures_out=tmp_path / "fixtures",
        environ={"JOBHUNTER_ANTHROPIC_API_KEY": "fake-anthropic-key-0001"},
        transport_factory=lambda: fake.RecordedSSETransport(root),
        database=shared_database(engine),
    )
    out = capsys.readouterr().out
    assert code == rec.OK, out
    entries = ledger(tmp_path)
    assert [e["case"] for e in entries] == [f"{r}{k}{p}#1" for k in range(1, 6) for r in "gc" for p in "ab"]
    assert {e["settlement"] for e in entries} == {"billed"}
    assert fake.state().served == 20  # one attempt each, one call at a time
    report = json.loads((tmp_path / "raw/anthropic/v5/report.json").read_text("utf-8"))
    assert report["analysis"]["measured"] == 20 and report["analysis"]["accepted"] is True
    assert set(report["counts"]) == {"generate", "critic"}  # the free counts of the token split (T25-a)
    # each attempt keeps its stage's max_tokens, its stop reason and its thinking tokens (Ruling T26-c)
    (first,) = report["calls"][0]["attempts"]
    assert (first["max_tokens"], first["stop_reason"], first["thinking_tokens"]) == (6_000, "end_turn", 0)
    assert report["analysis"]["max_tokens_by_model"] == {
        "claude-sonnet-5-5": {"attempts": 10, "max_tokens": 0},
        "claude-opus-5-5": {"attempts": 10, "max_tokens": 0},
    }
    # every stream committed by its case, and the second call of the first pairs where the fake serves it
    fixtures = tmp_path / "fixtures"
    for k in range(1, 6):
        for position in "ab":
            assert (
                fake.recording_path(fixtures, "generate", "claude-sonnet-5-5", generator)
                .with_name(f"{fake.schema12(generator)}.g{k}{position}.sse")
                .is_file()
            )
            assert (
                fake.recording_path(fixtures, "critic", "claude-opus-5-5", critic)
                .with_name(f"{fake.schema12(critic)}.c{k}{position}.sse")
                .is_file()
            )
    primary = fake.recording_path(fixtures, "generate", "claude-sonnet-5-5", generator)
    assert json.loads(primary.with_suffix(".components.json").read_text("utf-8"))["case"] == "g1b"
    revise = json.loads(
        fake.recording_path(fixtures, "revise", "claude-sonnet-5-5", generator)
        .with_suffix(".components.json")
        .read_text("utf-8")
    )
    assert (revise["case"], revise["purpose"], revise["serves"]) == ("g1b", "generate", "revise")
    critic_primary = fake.recording_path(fixtures, "critic", "claude-opus-5-5", critic)
    assert json.loads(critic_primary.with_suffix(".components.json").read_text("utf-8"))["case"] == "c1b"
    (run,) = (tmp_path / "raw/anthropic/v5").glob("run-*")  # each run keeps its raw exchanges apart
    assert run.name == report["run"] and (run / "report.json").is_file()
    timing = json.loads((run / "00-g1a-1.timing.json").read_text("utf-8"))
    assert timing["events"] and timing["chunks"]
