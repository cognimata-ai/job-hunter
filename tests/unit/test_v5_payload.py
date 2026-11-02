"""Spike V5's paid part with V14 folded in (M3 Task 26, Codex r1 #41): the exact requests it sends, asserted
offline before any live run, and the analysis of what it measures.

`scripts/spikes/v5_stream_timing.py` plans 20 streamed calls at effort `medium` (`gateway_calls("v5")`): 10
generator calls on `claude-sonnet-5-5` with schema C and 10 critic calls on `claude-opus-5-5`, as 5 + 5
identical-prefix pairs rendered by `render.score_prompt`, and V14's optional pair (`gateway_calls("v14")`):
a generator call and a critic-schema call on Sonnet with one prefix. Each request is captured here as one
gateway try sends it (`gateway._chat` on `to_messages`, as `LLMGateway.call` does) through the recorder's
`TimingTransport`, over a fake that answers with a synthetic stream: no database, no network, no key.
"""

import importlib.util
import json
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx2
import pytest
from jobhunter.cli_fixtures import contact_names, role_names
from jobhunter.config import AppConfig, load_config
from jobhunter.db.seed import load_settings_v1
from jobhunter.llm import fake, pricing
from jobhunter.llm.bounds import text_input_bound
from jobhunter.llm.gateway import _chat, to_messages
from jobhunter.llm.prompts.render import estimated_tokens
from jobhunter.llm.schemas import SCHEMAS_V2, transformed
from jobhunter.llm.transport import make_http_client
from jobhunter.observability.redact import EMAIL, PHONE
from jobhunter.types import get_object
from pydantic import SecretStr

from tests.support.fixture_scan import PHONE_DE, PHONE_MORE
from tests.support.llm import (
    BANNED_REQUEST_KEYS,
    cache_control_paths,
    critic_body,
    generator_wire_body,
    synthetic_sse,
)

REPO = Path(__file__).resolve().parents[2]
SONNET, OPUS = "claude-sonnet-5-5", "claude-opus-5-5"
POLICY: dict[str, Any] = get_object(load_settings_v1(), "scoring.model_policy")
EXECUTION: dict[str, Any] = get_object(load_settings_v1(), "scoring.execution")
GENERATOR_SCHEMA = transformed(SCHEMAS_V2["generator"])
CRITIC_SCHEMA = transformed(SCHEMAS_V2["critic"])
KEY = SecretStr("payload-test-key-0001")  # never a real key: nothing leaves the process


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts/spikes" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rec = _load("record_anthropic")
V5 = _load("v5_stream_timing")
CALLS = V5.gateway_calls("v5")
V14 = V5.gateway_calls("v14")


@pytest.fixture
def cfg(tmp_path: Path) -> AppConfig:
    return load_config("worker", {"JOBHUNTER_ENV": "test", "JOBHUNTER_DB_NAME": "payload"}, tmp_path / "none")


def answer(request: httpx2.Request) -> httpx2.Response:
    sent = json.loads(request.read())
    schema = sent["output_config"]["format"]["schema"]
    doc = critic_body() if schema == CRITIC_SCHEMA else generator_wire_body()
    sse = synthetic_sse(doc, model=sent["model"])
    return httpx2.Response(200, headers={"content-type": "text/event-stream; charset=utf-8"}, content=sse)


async def captured(cfg: AppConfig, call: dict[str, Any]) -> Any:
    """The exchange one gateway try of `call` makes, as the recorder's `TimingTransport` keeps it."""
    timing = rec.TimingTransport(httpx2.MockTransport(answer))
    node, prompt = call["node"], call["prompt"]
    async with make_http_client(cfg, mount=timing) as client:
        runnable = _chat(
            node.model,
            node.effort,
            node.max_tokens,
            node.schema.json,
            api_key=KEY,
            http_client=client,
            purpose=node.purpose,
        )
        out = await runnable.ainvoke(to_messages(prompt))
    assert out["parsing_error"] is None
    node.schema.model.model_validate(out["parsed"])
    (exchange,) = timing.exchanges
    return exchange


def prefix_of(body: dict[str, Any]) -> list[str]:
    """The texts up to the cache breakpoint."""
    marked = [i for i, block in enumerate(body["system"]) if "cache_control" in block]
    return [block["text"] for block in body["system"][: marked[-1] + 1]]


def test_the_plan_is_five_generator_pairs_and_five_critic_pairs_in_order() -> None:
    assert [c["case"] for c in CALLS] == [
        f"{role}{k}{position}" for k in range(1, 6) for role in "gc" for position in "ab"
    ]
    assert len(CALLS) == 20 and rec.SPIKE_CALLS["v5"] == 30  # Ruling T26-c: up to 10 enlarged stages
    for call in CALLS:
        node = call["node"]
        role = "generate" if call["case"][0] == "g" else "critic"
        assert (node.purpose, node.model, node.effort) == (role, POLICY["nodes"][role]["model"], "medium")
        stages = EXECUTION[role]
        assert (node.max_tokens, node.max_tokens_retry, node.deadline_s) == (
            stages["max_tokens"],
            stages["max_tokens_retry"],
            stages["deadline_s"],
        )
    assert {c["node"].model for c in CALLS if c["case"][0] == "g"} == {SONNET}
    assert {c["node"].model for c in CALLS if c["case"][0] == "c"} == {OPUS}


def test_v14_is_a_generator_call_then_a_critic_schema_call_on_sonnet_with_one_prefix() -> None:
    generator, critic = V14
    assert len(V14) == rec.SPIKE_CALLS["v14"] == 2
    assert (generator["node"].purpose, generator["node"].model, generator["node"].effort) == (
        "generate",
        SONNET,
        "medium",
    )
    assert (critic["node"].purpose, critic["node"].model, critic["node"].effort) == (
        "critic",
        SONNET,
        "medium",
    )
    assert critic["node"].schema.json == CRITIC_SCHEMA and generator["node"].schema.json == GENERATOR_SCHEMA
    assert generator["prompt"].system[:2] == critic["prompt"].system[:2]
    v5_prefixes = {c["prompt"].system[1].text for c in CALLS}
    assert generator["prompt"].system[1].text not in v5_prefixes  # its own prefix: no entry of V5's to read


@pytest.mark.parametrize("call", CALLS + V14, ids=[c["case"] for c in CALLS + V14])
async def test_each_request_is_the_three_block_layout_with_the_marker_on_the_profile_only(
    cfg: AppConfig, call: dict[str, Any]
) -> None:
    exchange = await captured(cfg, call)
    body = exchange.request_body()
    prompt, node = call["prompt"], call["node"]
    assert (exchange.method, exchange.path) == ("POST", "/v1/messages")
    # three system blocks: the shared rubric, the profile with the one breakpoint, the role
    assert len(body["system"]) == 3
    assert body["system"] == [
        {"type": "text", "text": prompt.system[0].text},
        {"type": "text", "text": prompt.system[1].text, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": prompt.system[2].text},
    ]
    assert cache_control_paths(body) == ["/system/1/cache_control"]
    assert prompt.system[1].text.startswith("<candidate_profile>")
    # the prefix (rubric and profile, up to the breakpoint) is at least 4,000 tokens at chars/4
    assert estimated_tokens("".join(prefix_of(body))) >= V5.PREFIX_MIN_TOKENS == 4_000
    # one user turn, never an assistant turn
    assert body["messages"] == [{"role": "user", "content": prompt.messages[0].content}]
    assert body["messages"][0]["content"].startswith('<job_ad untrusted="true">')
    expected = GENERATOR_SCHEMA if node.purpose == "generate" else CRITIC_SCHEMA
    assert body["output_config"] == {
        "format": {"type": "json_schema", "schema": expected},
        "effort": "medium",
    }
    assert (body["model"], body["max_tokens"], body["stream"]) == (node.model, node.max_tokens, True)
    assert not set(BANNED_REQUEST_KEYS) & set(body)
    assert exchange.request_headers["x-jh-purpose"] == node.purpose
    assert fake.KEY_HEADER.lower() not in exchange.request_headers  # the fake's key never goes live
    # what the budget reserves for the attempt is the gateway's own reservation of the prompt
    figures = rec.message_figures(body)
    bound = text_input_bound(node.model, prompt.as_request(node.schema))
    assert figures.input_tokens_bound == bound
    assert figures.worst_case_usd == pricing.worst_case_usd(
        node.model, input_tokens_bound=bound, max_tokens=node.max_tokens
    )


@pytest.mark.parametrize(
    "block",
    [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
        {"type": "document", "source": {"type": "text", "media_type": "text/plain", "data": "synthetic"}},
    ],
)
def test_a_non_text_system_block_has_no_byte_bound(block: dict[str, Any]) -> None:
    """The budget reserves a request by its bytes, so a system block it cannot size (one that is not `text`)
    is refused, as a non-text message block is, instead of being reserved at 0 bytes."""
    body = {
        "model": SONNET,
        "max_tokens": 6_000,
        "system": [{"type": "text", "text": "Synthetic rubric."}, block],
        "messages": [{"role": "user", "content": "Synthetic ad."}],
        "output_config": {"format": {"type": "json_schema", "schema": GENERATOR_SCHEMA}},
    }
    with pytest.raises(ValueError, match="non-text system block"):
        rec.message_figures(body)
    body["system"] = [{"type": "text", "text": "Synthetic rubric."}]
    assert rec.message_figures(body).input_tokens_bound > 0  # a text block is sized


async def test_a_pair_has_one_prefix_and_each_pair_its_own_prefix_and_ad(cfg: AppConfig) -> None:
    bodies = {c["case"]: (await captured(cfg, c)).request_body() for c in CALLS}
    prefixes, ads = {}, {}
    for k in range(1, 6):
        pair = [bodies[f"{role}{k}{position}"] for role in "gc" for position in "ab"]
        assert len({tuple(prefix_of(b)) for b in pair}) == 1  # the generator and the critic share it
        assert bodies[f"g{k}a"] == bodies[f"g{k}b"] and bodies[f"c{k}a"] == bodies[f"c{k}b"]
        prefixes[k] = tuple(prefix_of(pair[0]))
        ads[k] = pair[0]["messages"][0]["content"]
    assert len(set(prefixes.values())) == 5 and len(set(ads.values())) == 5
    # the rubric block is the same for every pair: only the profile block differs
    assert len({p[0] for p in prefixes.values()}) == 1


V12 = _load("v12_batches")


@pytest.mark.parametrize(
    "call",
    CALLS[::2] + V14 + V12.batch_requests("v12"),
    ids=[c.get("case", c.get("custom_id")) for c in CALLS[::2] + V14 + V12.batch_requests("v12")],
)
def test_the_synthetic_inputs_hold_nothing_the_fixture_scan_refuses(call: dict[str, Any]) -> None:
    """The recorded answers quote their inputs, and every committed recording passes the PII gate: the
    profiles, ads and drafts hold no e-mail, no phone number and no name next to a role or contact word."""
    prompt = call["prompt"]
    for text in [block.text for block in prompt.system] + [m.content for m in prompt.messages]:
        assert not EMAIL.search(text)
        assert not PHONE.search(text) and not PHONE_DE.search(text) and not PHONE_MORE.search(text)
        assert not role_names(text) and not contact_names(text)


def test_the_v5_dry_run_checks_every_payload_offline_within_the_budget(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = rec.main(
        ["--spike", "v5", "--dry-run"],
        root=tmp_path / "raw",
        fixtures_out=tmp_path / "fixtures",
        environ={},
        transport_factory=lambda: (_ for _ in ()).throw(AssertionError("a dry run opens no transport")),
    )
    out = capsys.readouterr().out
    assert code == rec.OK, out
    assert out.count("payload ok") == 20 and "BAD" not in out
    worst = sum(
        (
            pricing.worst_case_usd(
                c["node"].model,
                input_tokens_bound=text_input_bound(
                    c["node"].model, c["prompt"].as_request(c["node"].schema)
                ),
                max_tokens=c["node"].max_tokens,
            )
            for c in CALLS
        ),
        Decimal(0),
    )
    assert f"committed_usd={worst} of 10.00" in out
    assert not (tmp_path / "raw").exists() and not (tmp_path / "fixtures").exists()  # it wrote nothing


# --- the analysis ----------------------------------------------------------------------------------------


def attempt(case: str, *, read: int = 0, created: int = 0, gap: float = 2.0, status: int = 200) -> Any:
    usage = {
        "input_tokens": 1_800,
        "cache_creation_input_tokens": created,
        "cache_read_input_tokens": read,
        "output_tokens": 900,
    }
    return V5.AttemptTiming(
        case=case,
        status=status,
        duration_s=40.0,
        ttfe_s=1.5,
        max_gap_s=gap,
        events=80,
        usage=usage if status == 200 else None,
        stop_reason="end_turn" if status == 200 else None,
    )


def measured(**changes: Any) -> list[Any]:
    """The 20 calls of a run that met every acceptance criterion; `changes` replaces a call's fields."""
    calls = []
    for k in range(1, 6):
        for role, model, created in (("g", SONNET, 6_100), ("c", OPUS, 5_940)):
            for position in "ab":
                case = f"{role}{k}{position}"
                warm = position == "b"
                call = V5.CallTiming(
                    case=case,
                    role="generate" if role == "g" else "critic",
                    pair=k,
                    position=position,
                    model=model,
                    effort="medium",
                    outcome="ok",
                    call_s=45.0 + (200.0 if case == "g1a" else 0.0),
                    attempts=(
                        attempt(f"{case}#1", read=created if warm else 0, created=0 if warm else created),
                    ),
                )
                calls.append(changes.get(case, call))
    return calls


def test_a_run_that_meets_every_criterion_is_accepted() -> None:
    report = V5.analyse(measured())
    assert report["accepted"] is True and report["failed"] == []
    assert report["measured"] == 20 and report["p99_call_s"] == 245.0
    assert report["max_gap_s"] == 2.0
    assert report["first_generator_call_s"] == 245.0  # the grammar compile: reported, not excluded (G28)
    assert report["second_of_pair_cache_read"] == {
        f"{r}{k}b": n for k in range(1, 6) for r, n in (("g", 6_100), ("c", 5_940))
    }
    # V14: no critic call on Opus read the Sonnet generator's entry; G7: the cached part differs by schema
    assert report["critic_read_generator_entry"] is False
    assert report["cache_creation_by_role"] == {"generate": [6_100] * 5, "critic": [5_940] * 5}


@pytest.mark.parametrize(
    ("case", "change", "failed"),
    [
        ("c3a", {"call_s": 300.0}, ["p99_under_300_s"]),
        ("g2b", {"attempts": "gap"}, ["gaps_under_30_s"]),
        ("c4b", {"attempts": "cold"}, ["second_of_pair_reads_cache"]),
        ("g5a", {"outcome": "transient_http_529"}, ["all_measured"]),
    ],
)
def test_each_criterion_fails_on_its_own(case: str, change: dict[str, Any], failed: list[str]) -> None:
    base = {c.case: c for c in measured()}[case]
    if change.get("attempts") == "gap":
        change = {"attempts": (attempt(f"{case}#1", read=6_100, gap=30.0),)}
    elif change.get("attempts") == "cold":
        change = {"attempts": (attempt(f"{case}#1", read=3_999, created=0),)}
    replaced = V5.CallTiming(**{**base.__dict__, **change})
    report = V5.analyse(measured(**{case: replaced}))
    assert report["accepted"] is False and report["failed"] == failed


def test_a_retried_call_is_measured_once_and_reported() -> None:
    base = {c.case: c for c in measured()}["g3a"]
    retried = V5.CallTiming(**{**base.__dict__, "attempts": (attempt("g3a#1", status=529), *base.attempts)})
    report = V5.analyse(measured(g3a=retried))
    assert report["retried"] == {"g3a": 2} and report["accepted"] is True
    assert report["attempts"] == 21


def test_the_max_tokens_rate_per_model_and_each_attempts_stop_and_thinking_are_reported() -> None:
    """Ruling T26-c: an attempt cut at the first stage's `max_tokens` is followed by the enlarged stage; the
    report counts each model's attempts and its first-stage cuts, and keeps every attempt's stop reason and
    thinking tokens."""
    base = {c.case: c for c in measured()}
    cut = V5.AttemptTiming(
        **{**attempt("c2a#1").__dict__, "stop_reason": "max_tokens", "thinking_tokens": 5_100}
    )
    enlarged = V5.CallTiming(**{**base["c2a"].__dict__, "attempts": (cut, attempt("c2a#2"))})
    report = V5.analyse(measured(c2a=enlarged))
    assert report["max_tokens_by_model"] == {
        SONNET: {"attempts": 10, "max_tokens": 0},
        OPUS: {"attempts": 11, "max_tokens": 1},
    }
    assert report["stop_reasons"]["c2a"] == ["max_tokens", "end_turn"]
    assert report["thinking_tokens"]["c2a"] == [5_100, None]
    assert report["retried"] == {"c2a": 2} and report["accepted"] is True


def test_an_attempts_thinking_tokens_come_from_its_final_usage() -> None:
    sse = synthetic_sse(generator_wire_body(), model=SONNET)
    events = fake.parse_sse(sse)
    assert V5._thinking_tokens(events) == 0  # synthetic_sse's `output_tokens_details`
    # two cumulative `message_delta` usages with different counts: the final one is the attempt's
    *body, delta, stop = events
    assert (delta.name, stop.name) == ("message_delta", "message_stop")

    def counted(thinking: int) -> fake.SSEEvent:
        usage = {**delta.data["usage"], "output_tokens_details": {"thinking_tokens": thinking}}
        return fake.SSEEvent(delta.name, {**delta.data, "usage": usage})

    assert V5._thinking_tokens([*body, counted(1_200), counted(3_400), stop]) == 3_400
    assert V5._thinking_tokens([*body, counted(3_400), counted(1_200), stop]) == 1_200
    assert V5._thinking_tokens(body) is None  # no usage that ended: unknown, never 0


async def test_an_attempts_figures_come_from_its_exchange() -> None:
    """`attempt_timing` on the recorder's `Exchange` of one streamed attempt: its usage is the recorder's own
    (`Exchange.usage`, the settlement's `_merged_usage`), its stage the request's `max_tokens`."""
    usage = {"input_tokens": 1_792, "cache_creation_input_tokens": 8_644, "output_tokens": 2_400}
    sse = synthetic_sse(generator_wire_body(), model=SONNET, usage=usage)

    def answer(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=sse)

    timing = rec.TimingTransport(httpx2.MockTransport(answer))
    body = {"model": SONNET, "max_tokens": 12_000, "stream": True}
    async with httpx2.AsyncClient(transport=timing) as client:
        await client.post("https://api.anthropic.com/v1/messages", content=json.dumps(body).encode())
    (exchange,) = timing.exchanges
    figures = V5.attempt_timing(exchange)
    assert figures.usage == exchange.usage() == rec._merged_usage(fake.parse_sse(sse))
    assert figures.usage is not None and figures.usage["cache_creation_input_tokens"] == 8_644
    assert (figures.status, figures.stop_reason, figures.max_tokens, figures.thinking_tokens) == (
        200,
        "end_turn",
        12_000,
        0,
    )
    assert figures.events == len(fake.parse_sse(sse)) and figures.ttfe_s is not None


def test_a_run_stopped_by_a_refusal_reports_what_it_measured() -> None:
    report = V5.analyse(measured()[:7])
    assert report["measured"] == 7 and report["accepted"] is False
    assert report["failed"] == ["all_measured"]
