"""scripts/spikes/record_anthropic.py (M2 Task 4): the budgeted, crash-safe recorder of the paid Anthropic
spikes.

Every test runs offline. The recorder's network transport comes from `transport_factory`, which the tests
replace by one over an `httpx2.MockTransport` that counts how often it is created: a refused send creates
none, so no socket can open. The key is a visibly fake value in an explicit environment; the machine's real
key is never read here.
"""

import fcntl
import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

import anthropic
import httpx2
import pytest
from jobhunter.llm import bounds, fake, pricing

from tests.support.llm import synthetic_sse

REPO = Path(__file__).resolve().parents[2]
FAKE_KEY = "fake-anthropic-key-0001"  # visibly fake, and shaped like no provider key
ENV = {"JOBHUNTER_ANTHROPIC_API_KEY": FAKE_KEY}
REMOVED = ("request-id", "anthropic-organization-id", "cf-ray")


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts/spikes" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def rec() -> ModuleType:
    return _load("record_anthropic")


V13 = _load("v13_structured_stream")
QUERIES = {
    "queries": [
        {
            "text": f"Backend Engineer Python {i}",
            "lang": "en",
            "target_title": "Backend Engineer",
            "exploration": i == 5,
            "rationale": "Synthetic.",
        }
        for i in range(6)
    ]
}
USAGE = {
    "input_tokens": 900,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0,
    "output_tokens": 300,
}

Handler = Callable[[httpx2.Request], httpx2.Response]


class Factory:
    """The recorder's network transport factory: counts the transports it creates."""

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.created = 0
        self.requests: list[httpx2.Request] = []

    def __call__(self) -> httpx2.AsyncBaseTransport:
        self.created += 1

        def handle(request: httpx2.Request) -> httpx2.Response:
            self.requests.append(request)
            return self.handler(request)

        return httpx2.MockTransport(handle)


def recorded_stream(request: httpx2.Request) -> httpx2.Response:
    """A successful streamed answer for the requested model, with the headers a recording must drop."""
    model = json.loads(request.content)["model"]
    body = synthetic_sse(QUERIES, model=model, usage=USAGE, thinking="Synthetic thinking.")
    headers = {
        "content-type": "text/event-stream; charset=utf-8",
        "request-id": "req_0123456789abcdef",
        "anthropic-organization-id": "00000000-0000-4000-8000-000000000000",
        "cf-ray": "0123456789abcdef-FRA",
    }
    return httpx2.Response(200, headers=headers, content=body)


def run(
    rec: ModuleType, tmp: Path, factory: Factory, *args: str, environ: dict[str, str] | None = None
) -> int:
    code: int = rec.main(
        list(args),
        root=tmp / "raw",  # test only: the CLI has no --root, so a run can never start a second budget
        fixtures_out=tmp / "fixtures",
        environ=ENV if environ is None else environ,
        transport_factory=factory,
    )
    return code


def budget(tmp: Path) -> Any:
    return json.loads((tmp / "raw/anthropic-budget.json").read_text("utf-8"))


def seed_budget(tmp: Path, *entries: dict[str, Any]) -> None:
    (tmp / "raw").mkdir(parents=True, exist_ok=True)
    (tmp / "raw/anthropic-budget.json").write_text(json.dumps({"entries": list(entries)}), "utf-8")


def entry(spike: str, case: str, worst_case_usd: str = "0.01") -> dict[str, Any]:
    return {"spike": spike, "case": case, "state": "done", "worst_case_usd": worst_case_usd}


def every_file(root: Path) -> Iterator[str]:
    for path in root.rglob("*"):
        if path.is_file():
            yield path.read_text("utf-8", errors="replace")


def expected_worst_case(call: dict[str, Any]) -> Decimal:
    schema_json = fake.canonical_schema_json(anthropic.transform_schema(call["schema"]))
    rendered = bounds.RenderedRequest(
        system=tuple(call["system"]), messages=(call["user"],), schema_json=schema_json
    )
    return pricing.worst_case_usd(
        call["model"],
        input_tokens_bound=bounds.text_input_bound(call["model"], rendered),
        max_tokens=call["max_tokens"],
    )


# --- refusals before any transport exists -----------------------------------------------------------------


def test_the_ninth_message_is_refused_before_any_transport_exists(rec: ModuleType, tmp_path: Path) -> None:
    # 8 messages counted: 6 of V4, 1 of V19, and the Sonnet case of V13; the Opus case would be the 9th
    seed_budget(
        tmp_path,
        *(entry("v4", f"case{i}") for i in range(6)),
        entry("v19", "native_pdf"),
        entry("v13", "sonnet"),
    )
    factory = Factory(recorded_stream)
    assert run(rec, tmp_path, factory, "--spike", "v13", "--live") == rec.REFUSED
    assert factory.created == 0 and factory.requests == []
    assert len(budget(tmp_path)["entries"]) == 8


def test_a_send_whose_worst_case_would_pass_080_is_refused_unsent(rec: ModuleType, tmp_path: Path) -> None:
    seed_budget(tmp_path, entry("v4", "case0", "0.79"))
    factory = Factory(recorded_stream)
    assert run(rec, tmp_path, factory, "--spike", "v13", "--live") == rec.REFUSED
    assert factory.created == 0
    assert [e["case"] for e in budget(tmp_path)["entries"]] == ["case0"]


def test_the_spike_call_count_is_enforced(rec: ModuleType, tmp_path: Path) -> None:
    seed_budget(tmp_path, entry("v13", "earlier_a"), entry("v13", "earlier_b"))  # V13 has 2 of its 2
    factory = Factory(recorded_stream)
    assert run(rec, tmp_path, factory, "--spike", "v13", "--live") == rec.REFUSED
    assert factory.created == 0 and len(budget(tmp_path)["entries"]) == 2


def test_a_case_is_never_sent_twice(rec: ModuleType, tmp_path: Path) -> None:
    factory = Factory(recorded_stream)
    assert run(rec, tmp_path, factory, "--spike", "v13", "--live") == 0
    assert factory.created == 2
    assert run(rec, tmp_path, factory, "--spike", "v13", "--live") == 0  # nothing left to send
    assert factory.created == 2 and len(budget(tmp_path)["entries"]) == 2


def test_a_second_invocation_fails_at_once_while_the_lock_is_held(rec: ModuleType, tmp_path: Path) -> None:
    (tmp_path / "raw").mkdir()
    fd = os.open(tmp_path / "raw/anthropic-budget.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        factory = Factory(recorded_stream)
        assert run(rec, tmp_path, factory, "--spike", "v13", "--live") == rec.LOCKED
        assert factory.created == 0
    finally:
        os.close(fd)


def test_a_missing_key_refuses_before_anything(rec: ModuleType, tmp_path: Path) -> None:
    factory = Factory(recorded_stream)
    assert run(rec, tmp_path, factory, "--spike", "v13", "--live", environ={}) == rec.REFUSED
    assert factory.created == 0 and not (tmp_path / "raw/anthropic-budget.json").exists()


def test_a_spike_without_a_definition_is_refused(
    rec: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(rec.SPIKE_MODULES, "v19", "no_such_spike_module")
    factory = Factory(recorded_stream)
    assert run(rec, tmp_path, factory, "--spike", "v19", "--live") == rec.REFUSED
    assert factory.created == 0


def test_a_plan_longer_than_the_spike_call_count_is_refused_whole(
    rec: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(rec.SPIKE_CALLS, "v13", 1)
    factory = Factory(recorded_stream)
    assert run(rec, tmp_path, factory, "--spike", "v13", "--live") == rec.REFUSED
    assert factory.created == 0 and not (tmp_path / "raw/anthropic-budget.json").exists()


# --- counted before the send, and the count survives a crash ----------------------------------------------


def test_each_send_is_counted_with_its_worst_case_before_the_transport_exists(
    rec: ModuleType, tmp_path: Path
) -> None:
    calls = {c["case"]: c for c in V13.calls()}
    seen_at_send: list[Any] = []

    def check_then_answer(request: httpx2.Request) -> httpx2.Response:
        seen_at_send.append(budget(tmp_path)["entries"][-1])
        return recorded_stream(request)

    assert run(rec, tmp_path, Factory(check_then_answer), "--spike", "v13", "--live") == 0
    assert [(e["case"], e["state"]) for e in seen_at_send] == [("sonnet", "sending"), ("opus", "sending")]
    for counted in seen_at_send:
        assert Decimal(counted["worst_case_usd"]) == expected_worst_case(calls[counted["case"]])
    state = budget(tmp_path)
    assert state["sent"] == 2
    assert Decimal(state["worst_case_usd"]) == sum(expected_worst_case(c) for c in calls.values())
    assert Decimal(state["worst_case_usd"]) <= Decimal("0.80")


# A recorder process whose send never returns: it marks that the request reached the network, then hangs
# until the test kills it (SIGKILL: nothing after the send runs, not even a `finally`).
CRASH_DRIVER = """
import importlib.util, sys, time
from pathlib import Path
import httpx2
spec = importlib.util.spec_from_file_location("record_anthropic", sys.argv[1])
rec = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rec)
tmp, marker = Path(sys.argv[2]), Path(sys.argv[3])
def hang(request):
    marker.write_text("sending")
    time.sleep(3600)
rec.main(
    ["--spike", "v13", "--live"],
    root=tmp / "raw",
    fixtures_out=tmp / "fixtures",
    environ={"JOBHUNTER_ANTHROPIC_API_KEY": "fake-anthropic-key-0001"},
    transport_factory=lambda: httpx2.MockTransport(hang),
)
"""


def test_the_count_survives_a_crash_during_the_send(rec: ModuleType, tmp_path: Path) -> None:
    marker = tmp_path / "reached-the-network"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "JOBHUNTER_ANTHROPIC"))}
    script = str(REPO / "scripts/spikes/record_anthropic.py")
    proc = subprocess.Popen(
        [sys.executable, "-c", CRASH_DRIVER, script, str(tmp_path), str(marker)],
        cwd=REPO,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 60
        while not marker.exists():
            assert proc.poll() is None and time.monotonic() < deadline, "the recorder never reached the send"
            time.sleep(0.05)
    finally:
        proc.kill()
        proc.wait(10)
    assert proc.returncode == -signal.SIGKILL
    (counted,) = budget(tmp_path)["entries"]
    assert (counted["case"], counted["state"]) == ("sonnet", "sending")
    assert Decimal(counted["worst_case_usd"]) > 0 and counted["settled_usd"] is None
    assert budget(tmp_path)["sent"] == 1
    # the rerun sends only the case that was never counted
    factory = Factory(recorded_stream)
    assert run(rec, tmp_path, factory, "--spike", "v13", "--live") == 0
    assert factory.created == 1 and json.loads(factory.requests[0].content)["model"] == "claude-opus-5-5"
    assert budget(tmp_path)["sent"] == 2


def test_a_transport_failure_stays_counted_at_its_worst_case_and_stops_the_spike(
    rec: ModuleType, tmp_path: Path
) -> None:
    def fail(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("timed out", request=request)

    factory = Factory(fail)
    assert run(rec, tmp_path, factory, "--spike", "v13", "--live") == rec.STOPPED
    (done,) = budget(tmp_path)["entries"]
    # langchain re-raises the SDK error as its own subclass of it (V5.md)
    assert (done["state"], done["status"], done["error"]) == ("done", None, "AnthropicTimeoutError")
    assert done["settled_usd"] == done["worst_case_usd"]  # the outcome is unknown: kept at its worst case
    assert factory.created == 1


def test_an_error_response_stops_the_spike_and_settles_nothing(rec: ModuleType, tmp_path: Path) -> None:
    def bad_request(request: httpx2.Request) -> httpx2.Response:
        error = {"type": "error", "error": {"type": "invalid_request_error", "message": "synthetic"}}
        return httpx2.Response(400, json=error)

    factory = Factory(bad_request)
    assert run(rec, tmp_path, factory, "--spike", "v13", "--live") == rec.STOPPED
    assert factory.created == 1  # the Opus case is never sent
    (done,) = budget(tmp_path)["entries"]
    assert (done["state"], done["status"], done["settled_usd"]) == ("done", 400, "0.000000")
    assert not (tmp_path / "fixtures").exists()


def test_a_stream_that_did_not_end_the_turn_is_settled_but_not_committed(
    rec: ModuleType, tmp_path: Path
) -> None:
    def max_tokens(request: httpx2.Request) -> httpx2.Response:
        model = json.loads(request.content)["model"]
        body = synthetic_sse(QUERIES, model=model, usage=USAGE, stop_reason="max_tokens")
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    factory = Factory(max_tokens)
    assert run(rec, tmp_path, factory, "--spike", "v13", "--live") == rec.STOPPED
    assert factory.created == 1  # the second call is never sent
    (done,) = budget(tmp_path)["entries"]
    assert (done["status"], done["stop_reason"], done["settlement"]) == (200, "max_tokens", "billed")
    assert (tmp_path / "raw/anthropic/v13/sonnet.check.json").exists()  # the V13 field paths are still read
    assert not (tmp_path / "fixtures").exists()


# --- dry run -----------------------------------------------------------------------------------------------


def test_the_dry_run_checks_the_payloads_offline_and_writes_no_budget(
    rec: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    factory = Factory(recorded_stream)
    assert run(rec, tmp_path, factory, "--spike", "v13", "--dry-run") == 0
    assert factory.created == 0 and not (tmp_path / "raw/anthropic-budget.json").exists()
    out = capsys.readouterr().out
    for call in V13.calls():
        assert f"v13 {call['case']} {call['model']} effort=low max_tokens=1024" in out
        assert f"worst_case_usd={expected_worst_case(call)}" in out
    assert out.count("payload ok") == 2 and FAKE_KEY not in out


def test_the_dry_run_needs_no_key(rec: ModuleType, tmp_path: Path) -> None:
    assert run(rec, tmp_path, Factory(recorded_stream), "--spike", "v13", "--dry-run", environ={}) == 0


# --- what a live run writes -------------------------------------------------------------------------------


def test_a_live_run_writes_raw_captures_the_fixture_and_its_sidecars_and_settles(
    rec: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    factory = Factory(recorded_stream)
    assert run(rec, tmp_path, factory, "--spike", "v13", "--live") == 0
    assert all(r.headers["x-api-key"] == FAKE_KEY for r in factory.requests)
    assert all(r.url.host == "api.anthropic.com" for r in factory.requests)
    out = capsys.readouterr().out
    for call in V13.calls():
        model = call["model"]
        schema = anthropic.transform_schema(call["schema"])
        # the raw captures (gitignored): the request without its key, the response as received
        raw = tmp_path / "raw/anthropic/v13"
        request = json.loads((raw / f"{call['case']}.request.json").read_text("utf-8"))
        assert request["url"] == "https://api.anthropic.com/v1/messages"
        assert "x-api-key" not in {h.lower() for h in request["headers"]}
        assert request["body"]["output_config"]["format"]["schema"] == schema
        response_headers = json.loads((raw / f"{call['case']}.response.headers.json").read_text("utf-8"))
        assert response_headers["headers"]["request-id"] == "req_0123456789abcdef"
        assert (raw / f"{call['case']}.response.sse").read_bytes().startswith(b"event: message_start")
        # the committed recording: the three headers removed, no thinking text, no signature, no provider id
        sse = fake.recording_path(tmp_path / "fixtures", call["purpose"], model, schema)
        headers = json.loads(sse.with_suffix(".headers.json").read_text("utf-8"))
        assert headers["status"] == 200 and not set(REMOVED) & {h.lower() for h in headers["headers"]}
        events = fake.parse_sse(sse.read_bytes())
        text = sse.read_text("utf-8")
        assert "Synthetic thinking." not in text and "msg_0" not in text
        assert json.loads(fake.text_of(events)) == QUERIES
        components = json.loads(sse.with_suffix(".components.json").read_text("utf-8"))
        assert components == {
            "source": "recorded",
            "spike": "v13",
            "case": call["case"],
            "purpose": call["purpose"],
            "model": model,
            "effort": "low",
            "max_tokens": 1024,
            "component_bytes": {
                "system": [len(s.encode()) for s in call["system"]],
                "messages": [len(call["user"].encode())],
                "schema": len(fake.canonical_schema_json(schema).encode()),
            },
            "usage": USAGE,
        }
    # settled from the recorded usage, next to each counted entry
    for counted in budget(tmp_path)["entries"]:
        usage = pricing.Usage(900, 300, 0, 0)
        assert counted["state"] == "done" and counted["status"] == 200
        assert Decimal(counted["settled_usd"]) == pricing.settled_usd(counted["model"], usage)
    assert "v13 sonnet 200" in out and "v13 opus 200" in out
    for text in [*every_file(tmp_path), out]:
        assert FAKE_KEY not in text


def test_the_v13_check_reads_the_field_paths_and_writes_no_content(rec: ModuleType, tmp_path: Path) -> None:
    assert run(rec, tmp_path, Factory(recorded_stream), "--spike", "v13", "--live") == 0
    report = json.loads((tmp_path / "raw/anthropic/v13/sonnet.check.json").read_text("utf-8"))
    assert report["request"]["output_config.format.type"] == "json_schema"
    assert report["request"]["output_config.effort"] == "low"
    assert report["request"]["absent"] == ["temperature", "tool_choice", "tools", "top_k", "top_p"]
    assert report["response_metadata"]["model_name"] == "claude-sonnet-5-5"
    assert report["response_metadata"]["stop_reason"] == "end_turn"
    assert "Backend Engineer" not in json.dumps(report)  # field paths and flags, never the content
