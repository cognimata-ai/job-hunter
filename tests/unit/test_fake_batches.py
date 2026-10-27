"""The fake Message Batches API (M3 Task 8): create, retrieve, results, cancel and the listing, answered from
the test's `BatchScript` (`fake.batches`, `tests.support.batches.scripted_batches`) on the same mount as the
Messages API.

The requests go through the SDK's own batch client on the fake (`gateway._sdk_client`, no retries), so every
body the fake answers is also parsed as the SDK's types; the result lines are checked as raw JSON too.
Everything runs offline.
"""

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import anthropic
import httpx2
import pytest
from jobhunter.llm import fake, gateway
from jobhunter.llm.gateway import WireSchema
from jobhunter.llm.transport import ANTHROPIC_BASE_URL
from pydantic import SecretStr

from tests.support.batches import (
    BatchScript,
    ItemOutcome,
    StoredBatch,
    scripted_batches,
    synthetic_batch_results_jsonl,
)
from tests.support.llm import (
    GATEWAY_BODY,
    FakeLLM,
    GatewayOut,
    generator_wire_body,
    synthetic_message_json,
    synthetic_sse,
)

KEY = SecretStr("fake-batches-test-key-0001")  # visibly fake; the fake ignores keys
MODEL = "claude-sonnet-5-5"
IDS = ("t1_a1", "t2_a1", "t3_a1")
OTHER_DRAFT = {**generator_wire_body(), "summary_en": "Another target's draft."}


def params(*, max_tokens: int = 6000) -> dict[str, Any]:
    """A batch item's params: the online generator body without `stream` (selects the `generate` recording of
    `GatewayOut` that the `fake_llm` fixture wrote)."""
    return {
        "model": MODEL,
        "max_tokens": max_tokens,
        "system": [{"type": "text", "text": "You score a job ad.", "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": "A synthetic ad."}],
        "output_config": {
            "effort": "medium",
            "format": {"type": "json_schema", "schema": WireSchema.from_model(GatewayOut).json},
        },
    }


class Batches:
    """The SDK's batch client and a plain http client, both on the fake."""

    def __init__(self, http: httpx2.AsyncClient) -> None:
        self.http = http
        self.sdk = gateway._sdk_client(KEY, http, http.timeout, {})

    async def create(
        self,
        ids: Sequence[str] = IDS,
        *,
        keys: Mapping[str, str] | None = None,
        max_tokens: int = 6000,
    ) -> Any:
        requests = [{"custom_id": cid, "params": params(max_tokens=max_tokens)} for cid in ids]
        headers = {} if keys is None else {fake.KEY_HEADER: fake.batch_keys(keys)}
        return await self.sdk.messages.batches.create(requests=cast(Any, requests), extra_headers=headers)

    async def results(self, batch_id: str) -> list[Any]:
        """The SDK's parsed result lines (`results()` retrieves the batch first, as the SDK always does)."""
        return [line async for line in await self.sdk.messages.batches.results(batch_id)]

    async def raw_results(self, batch_id: str) -> list[dict[str, Any]]:
        response = await self.http.get(f"{ANTHROPIC_BASE_URL}/v1/messages/batches/{batch_id}/results")
        assert response.status_code == 200
        return [json.loads(line) for line in response.text.splitlines()]

    async def listing(self, **query: Any) -> dict[str, Any]:
        response = await self.http.get(f"{ANTHROPIC_BASE_URL}/v1/messages/batches", params=query)
        assert response.status_code == 200
        body: dict[str, Any] = response.json()
        return body


@pytest.fixture
async def api(fake_llm: FakeLLM) -> AsyncIterator[Batches]:
    async with fake_llm.client() as http:
        yield Batches(http)


def stored(batch_id: str) -> StoredBatch:
    return fake.state().batches[batch_id]


def _causes(exc: BaseException) -> list[BaseException]:
    seen, current = [], exc.__cause__ or exc.__context__
    while current is not None:
        seen.append(current)
        current = current.__cause__ or current.__context__
    return seen


# ---- the round trip ----------------------------------------------------------------------------------------


async def test_a_three_item_batch_round_trips_create_retrieve_results_and_the_listing(
    fake_llm: FakeLLM, api: Batches
) -> None:
    batches = scripted_batches()  # BatchScript(): one in-progress retrieval, every item `succeeded`
    fake_llm.answer("generate", 501)  # the default C draft
    fake_llm.answer("generate", 502, OTHER_DRAFT, usage={"input_tokens": 700, "output_tokens": 90})
    keys = {"t1_a1": "generate:501:0", "t2_a1": "generate:502:0"}  # t3_a1: none, the recording
    created = await api.create(keys=keys)
    assert created.id == "msgbatch_1" and created.type == "message_batch"
    assert created.processing_status == "in_progress" and created.ended_at is None
    assert created.expires_at - created.created_at == timedelta(hours=24)
    assert created.request_counts.model_dump() == {
        "processing": 3,
        "succeeded": 0,
        "errored": 0,
        "canceled": 0,
        "expired": 0,
    }
    assert created.results_url is None
    batch = batches[created.id]
    assert batch.custom_ids == frozenset(IDS) and batch.requests["t2_a1"] == params()
    assert batch.keys == keys

    polled = await api.sdk.messages.batches.retrieve(created.id)
    assert polled.processing_status == "in_progress"
    ended = await api.sdk.messages.batches.retrieve(created.id)
    assert ended.processing_status == "ended" and ended.ended_at is not None
    assert ended.request_counts.model_dump() == {
        "processing": 0,
        "succeeded": 3,
        "errored": 0,
        "canceled": 0,
        "expired": 0,
    }
    assert ended.results_url == f"{ANTHROPIC_BASE_URL}/v1/messages/batches/{created.id}/results"

    lines = await api.results(created.id)
    assert sorted(line.custom_id for line in lines) == list(IDS)  # each custom_id once
    assert [line.custom_id for line in lines] != list(IDS)  # shuffled
    texts = {line.custom_id: line.result.message.content[0].text for line in lines}
    assert json.loads(texts["t1_a1"]) == generator_wire_body()
    assert json.loads(texts["t2_a1"]) == OTHER_DRAFT
    assert json.loads(texts["t3_a1"]) == GATEWAY_BODY
    messages = {line.custom_id: line.result.message for line in lines}
    assert {m.stop_reason for m in messages.values()} == {"end_turn"}
    assert {m.model for m in messages.values()} == {MODEL}
    assert (messages["t2_a1"].usage.input_tokens, messages["t2_a1"].usage.output_tokens) == (700, 90)
    assert batch.retrievals == 3 and batch.results_read == 1  # `results()` retrieves first

    listing = await api.sdk.messages.batches.list()
    (listed,) = listing.data
    assert (listed.id, listed.processing_status, listed.ended_at) == (created.id, "ended", ended.ended_at)
    assert listed.created_at == created.created_at
    assert listed.request_counts == ended.request_counts
    assert batch.retrievals == 3  # the listing is no retrieval


async def test_a_succeeded_item_is_the_message_json_of_its_answer(fake_llm: FakeLLM, api: Batches) -> None:
    """The fake's message of a keyed answer equals `synthetic_message_json` of the same body (two builders of
    one shape), with the four token counts, the model, the stop reason and the id; no thinking block."""
    usage = {"input_tokens": 610, "cache_creation_input_tokens": 40, "cache_read_input_tokens": 0}
    body = generator_wire_body()
    fake.answer(
        "generate:9:0",
        synthetic_sse(body, model=MODEL, usage={**usage, "output_tokens": 55}, thinking="hidden"),
    )
    scripted_batches(polls_until_ended=0)
    created = await api.create(["t9_a1"], keys={"t9_a1": "generate:9:0"})
    (line,) = await api.raw_results(created.id)
    expected = synthetic_message_json(body, model=MODEL, usage={**usage, "output_tokens": 55})
    assert line == {"custom_id": "t9_a1", "result": {"type": "succeeded", "message": expected}}
    assert set(line["result"]["message"]["usage"]) == {
        "input_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "output_tokens",
    }
    assert [block["type"] for block in line["result"]["message"]["content"]] == ["text"]


# ---- polls, the listing and the clock ----------------------------------------------------------------------


async def test_zero_polls_means_ended_from_creation_in_retrieve_and_listing(api: Batches) -> None:
    batches = scripted_batches(polls_until_ended=0)
    created = await api.create()
    assert created.processing_status == "ended" and created.ended_at == created.created_at
    assert created.request_counts.succeeded == 3 and created.results_url is not None
    listing = await api.sdk.messages.batches.list()  # an ended candidate at its first listing
    (listed,) = listing.data
    assert (listed.id, listed.processing_status, listed.ended_at) == (created.id, "ended", created.ended_at)
    assert listed.request_counts == created.request_counts
    assert batches[created.id].retrievals == 0  # no retrieval was needed
    retrieved = await api.sdk.messages.batches.retrieve(created.id)
    assert (retrieved.processing_status, retrieved.ended_at) == ("ended", created.ended_at)


async def test_the_listing_reports_the_status_the_last_retrieval_left(api: Batches) -> None:
    scripted_batches(polls_until_ended=2)
    created = await api.create()
    for _ in range(2):
        assert (await api.sdk.messages.batches.retrieve(created.id)).processing_status == "in_progress"
        (listed,) = (await api.sdk.messages.batches.list()).data
        assert (listed.processing_status, listed.ended_at) == ("in_progress", None)
    ended = await api.sdk.messages.batches.retrieve(created.id)
    (listed,) = (await api.sdk.messages.batches.list()).data
    assert (listed.processing_status, listed.ended_at) == ("ended", ended.ended_at)


async def test_the_batch_times_come_from_the_states_clock(api: Batches) -> None:
    now = [datetime(2026, 10, 27, 9, 0, tzinfo=UTC)]
    fake.state().clock = lambda: now[0]
    created = await api.create()
    assert (created.created_at, created.expires_at) == (now[0], now[0] + timedelta(hours=24))
    now[0] += timedelta(minutes=5)
    await api.sdk.messages.batches.retrieve(created.id)
    now[0] += timedelta(minutes=5)
    ended = await api.sdk.messages.batches.retrieve(created.id)
    assert ended.ended_at == datetime(2026, 10, 27, 9, 10, tzinfo=UTC)


async def test_the_listing_is_newest_first_and_pages_with_limit_and_after_id(api: Batches) -> None:
    ids = [(await api.create([f"t{n}_a1"])).id for n in range(1, 4)]
    first = await api.listing(limit=2)
    assert [b["id"] for b in first["data"]] == [ids[2], ids[1]]
    assert (first["has_more"], first["first_id"], first["last_id"]) == (True, ids[2], ids[1])
    assert all("created_at" in b and "request_counts" in b for b in first["data"])
    second = await api.listing(limit=2, after_id=ids[1])
    assert [b["id"] for b in second["data"]] == [ids[0]] and second["has_more"] is False
    walked = [b.id async for b in api.sdk.messages.batches.list(limit=1)]  # the SDK's auto-pagination
    assert walked == ids[::-1]


async def test_an_empty_listing_lists_nothing_while_the_batches_exist(api: Batches) -> None:
    created = await api.create()
    scripted_batches(listing="empty")
    assert (await api.listing())["data"] == []
    assert created.id in fake.state().batches


async def test_an_error_500_listing_raises_until_the_script_changes(api: Batches) -> None:
    created = await api.create()
    scripted_batches(listing="error_500")
    for _ in range(3):  # every call, for as long as the script says so (C22)
        with pytest.raises(anthropic.InternalServerError):
            await api.sdk.messages.batches.list()
    scripted_batches()
    assert [b.id for b in (await api.sdk.messages.batches.list()).data] == [created.id]


# ---- the item outcomes -------------------------------------------------------------------------------------

OUTCOME_IDS: Mapping[str, ItemOutcome] = {
    "t1_a1": "succeeded",
    "t2_a1": "succeeded:max_tokens",
    "t3_a1": "succeeded:refusal",
    "t4_a1": "succeeded:unexpected_stop",
    "t5_a1": "succeeded:invalid",
    "t6_a1": "errored:invalid_request",
    "t7_a1": "errored:other",
    "t8_a1": "canceled",
    "t9_a1": "expired",
    "t10_a1": "missing",
}


async def test_each_item_outcome_shapes_its_result_line_and_the_counts(api: Batches) -> None:
    scripted_batches(items=OUTCOME_IDS, polls_until_ended=0)
    created = await api.create(list(OUTCOME_IDS), max_tokens=4321)
    assert created.request_counts.model_dump() == {
        "processing": 0,
        "succeeded": 6,  # the five `succeeded:*` and the missing one, whose result the provider lost
        "errored": 2,
        "canceled": 1,
        "expired": 1,
    }
    lines = {line["custom_id"]: line["result"] for line in await api.raw_results(created.id)}
    assert set(lines) == set(OUTCOME_IDS) - {"t10_a1"}  # a missing item has no line
    recorded = json.dumps(GATEWAY_BODY, ensure_ascii=False)
    ok, cut, refused, tool, invalid = (lines[f"t{n}_a1"]["message"] for n in range(1, 6))
    assert (ok["stop_reason"], ok["content"]) == ("end_turn", [{"type": "text", "text": recorded}])
    assert cut["stop_reason"] == "max_tokens" and cut["usage"]["output_tokens"] == 4321
    assert recorded.startswith(cut["content"][0]["text"]) and len(cut["content"][0]["text"]) < len(recorded)
    assert (refused["stop_reason"], refused["content"]) == ("refusal", [])
    assert refused["stop_details"] == {
        "type": "refusal",
        "category": fake.REFUSAL_CATEGORY,
        "explanation": None,
    }
    assert {m["stop_details"] for m in (ok, cut, tool, invalid)} == {None}
    assert tool["stop_reason"] == "tool_use"
    assert invalid["stop_reason"] == "end_turn" and invalid["content"][0]["text"] == fake.PARSE_ERROR_TEXT
    assert lines["t6_a1"]["type"] == "errored"
    assert lines["t6_a1"]["error"]["error"]["type"] == "invalid_request_error"
    assert lines["t7_a1"]["error"]["error"]["type"] == "api_error"
    assert (lines["t8_a1"], lines["t9_a1"]) == ({"type": "canceled"}, {"type": "expired"})
    parsed = {line.custom_id: line.result for line in await api.results(created.id)}  # the SDK's types
    assert parsed["t6_a1"].type == "errored" and parsed["t6_a1"].error.error.type == "invalid_request_error"
    assert parsed["t3_a1"].message.stop_details.category == fake.REFUSAL_CATEGORY
    assert stored(created.id).outcomes == OUTCOME_IDS


async def test_the_builder_writes_the_lines_the_fake_serves(api: Batches) -> None:
    """`synthetic_batch_results_jsonl` builds what the fake serves for the same outcomes and answer (the
    builder for result parsers fed without the fake)."""
    fake.answer("generate:5:0", synthetic_sse(generator_wire_body(), model=MODEL))
    keys = {cid: "generate:5:0" for cid in OUTCOME_IDS}
    scripted_batches(items=OUTCOME_IDS, polls_until_ended=0)
    created = await api.create(list(OUTCOME_IDS), keys=keys, max_tokens=4321)
    served = {line["custom_id"]: line for line in await api.raw_results(created.id)}
    built = synthetic_batch_results_jsonl(
        OUTCOME_IDS, model=MODEL, body=generator_wire_body(), max_tokens=4321
    )
    expected = {line["custom_id"]: line for line in map(json.loads, built.decode("utf-8").splitlines())}
    assert served == expected and len(built.splitlines()) == len(OUTCOME_IDS) - 1


async def test_missing_items_are_absent_from_the_results(api: Batches) -> None:
    scripted_batches(items={"t2_a1": "missing"}, polls_until_ended=0)
    created = await api.create()
    assert sorted(line.custom_id for line in await api.results(created.id)) == ["t1_a1", "t3_a1"]
    assert created.request_counts.succeeded == 3


async def test_an_item_without_an_answer_or_a_recording_is_errored(api: Batches) -> None:
    scripted_batches(polls_until_ended=0)
    requests = [{"custom_id": "t1_a1", "params": {**params(), "model": "claude-none-0-0"}}]  # no recording
    created = await api.sdk.messages.batches.create(requests=cast(Any, requests))
    assert created.request_counts.errored == 1
    (line,) = await api.raw_results(created.id)
    assert line["result"]["error"]["error"]["type"] == "not_found_error"


async def test_results_are_shuffled_by_the_batch_id_and_stable(api: Batches) -> None:
    scripted_batches(polls_until_ended=0)
    for size in range(2, 7):
        ids = [f"t{n}_a1" for n in range(1, size + 1)]
        created = await api.create(ids)
        first = [line["custom_id"] for line in await api.raw_results(created.id)]
        again = [line["custom_id"] for line in await api.raw_results(created.id)]
        assert sorted(first) == sorted(ids) and first == again
        assert first != ids  # never the submission order
        assert stored(created.id).results_read == 2


async def test_stream_cut_after_two_lines_truncates_the_results(api: Batches) -> None:
    scripted_batches(polls_until_ended=0, stream_cut_after=2)
    created = await api.create()
    url = f"{ANTHROPIC_BASE_URL}/v1/messages/batches/{created.id}/results"
    served = b""
    async with api.http.stream("GET", url) as response:  # what the fake put on the wire
        with pytest.raises(httpx2.RemoteProtocolError):
            async for chunk in response.aiter_raw():
                served += chunk
    cut = [json.loads(line) for line in served.decode("utf-8").splitlines()]
    assert len(cut) == 2 and served.endswith(b"\n")  # two whole lines, then the connection dropped
    # the SDK's reader fails too, before the third line (its 64-byte read chunks may hold back the tail)
    received: list[str] = []
    with pytest.raises(httpx2.RemoteProtocolError):
        async for line in await api.sdk.messages.batches.results(created.id):
            received.append(line.custom_id)
    assert received == [line["custom_id"] for line in cut][: len(received)]
    scripted_batches()  # the next read is whole
    whole = [line.custom_id for line in await api.results(created.id)]
    assert sorted(whole) == list(IDS) and whole[:2] == [line["custom_id"] for line in cut]
    assert stored(created.id).results_read == 3


async def test_results_of_a_batch_in_progress_are_refused(api: Batches) -> None:
    created = await api.create()
    response = await api.http.get(f"{ANTHROPIC_BASE_URL}/v1/messages/batches/{created.id}/results")
    assert response.status_code == 400 and response.json()["error"]["type"] == "invalid_request_error"
    assert stored(created.id).results_read == 0


# ---- create outcomes ---------------------------------------------------------------------------------------


async def test_ambiguous_created_stores_the_batch_and_drops_the_connection(api: Batches) -> None:
    batches = scripted_batches(create="ambiguous_created")
    with pytest.raises(anthropic.APIConnectionError) as dropped:
        await api.create()
    assert any(isinstance(cause, httpx2.RemoteProtocolError) for cause in _causes(dropped.value))
    (batch,) = batches.values()
    assert batch.custom_ids == frozenset(IDS) and batch.processing_status == "in_progress"
    (listed,) = (await api.listing())["data"]  # the reconciler finds it by listing
    assert (listed["id"], sum(listed["request_counts"].values())) == (batch.id, 3)


@pytest.mark.parametrize(
    ("outcome", "error"),
    [
        ("ambiguous_not_created", anthropic.APIConnectionError),
        ("reject_400", anthropic.BadRequestError),
        ("error_500", anthropic.InternalServerError),
        ("timeout", anthropic.APITimeoutError),
    ],
)
async def test_a_create_that_fails_stores_no_batch(
    api: Batches, outcome: Any, error: type[Exception]
) -> None:
    batches = scripted_batches(create=outcome)
    with pytest.raises(error):
        await api.create()
    assert batches == {}


@pytest.mark.parametrize(
    "requests",
    [
        [{"custom_id": "t1 a1", "params": params()}],  # outside ^[a-zA-Z0-9_-]{1,64}$
        [{"custom_id": "t" * 65, "params": params()}],
        [{"custom_id": "t1_a1", "params": params()}, {"custom_id": "t1_a1", "params": params()}],
        [{"custom_id": "t1_a1", "params": {**params(), "stream": True}}],
        [],
    ],
    ids=["pattern", "length", "duplicate", "stream", "empty"],
)
async def test_an_invalid_create_is_a_400(api: Batches, requests: list[dict[str, Any]]) -> None:
    with pytest.raises(anthropic.BadRequestError):
        await api.sdk.messages.batches.create(requests=cast(Any, requests))
    assert fake.state().batches == {}


async def test_without_a_script_a_create_is_answered_as_batch_script_defaults(api: Batches) -> None:
    assert fake.state().batch_script is None  # nothing registered, as in the dev stack
    created = await api.create()
    assert created.processing_status == "in_progress" and created.request_counts.processing == 3
    assert stored(created.id).script == BatchScript()
    assert (await api.sdk.messages.batches.retrieve(created.id)).processing_status == "in_progress"
    ended = await api.sdk.messages.batches.retrieve(created.id)
    assert ended.processing_status == "ended" and ended.request_counts.succeeded == 3


async def test_a_batch_keeps_the_script_it_was_created_under(api: Batches) -> None:
    scripted_batches(items={"t1_a1": "expired"}, polls_until_ended=0)
    created = await api.create()
    scripted_batches(default_item="errored:other", polls_until_ended=5)  # for later batches only
    assert {line.custom_id: line.result.type for line in await api.results(created.id)} == {
        "t1_a1": "expired",
        "t2_a1": "succeeded",
        "t3_a1": "succeeded",
    }


# ---- cancel ------------------------------------------------------------------------------------------------


async def test_cancel_on_an_in_progress_batch_cancels_the_unprocessed_items(api: Batches) -> None:
    batches = scripted_batches(items={"t1_a1": "succeeded"}, polls_until_ended=3)  # t1_a1 was processed
    created = await api.create()
    canceling = await api.sdk.messages.batches.cancel(created.id)
    assert canceling.processing_status == "canceling" and canceling.cancel_initiated_at is not None
    assert batches[created.id].cancel_requested is True
    (listed,) = (await api.sdk.messages.batches.list()).data
    assert listed.processing_status == "canceling"
    ended = await api.sdk.messages.batches.retrieve(created.id)  # then ended, whatever the polls left
    assert ended.processing_status == "ended"
    assert (ended.request_counts.succeeded, ended.request_counts.canceled) == (1, 2)
    results = {line.custom_id: line.result.type for line in await api.results(created.id)}
    assert results == {"t1_a1": "succeeded", "t2_a1": "canceled", "t3_a1": "canceled"}


async def test_cancel_of_an_ended_batch_changes_nothing(api: Batches) -> None:
    scripted_batches(polls_until_ended=0)
    created = await api.create()
    after = await api.sdk.messages.batches.cancel(created.id)
    assert (after.processing_status, after.request_counts.succeeded) == ("ended", 3)
    assert after.cancel_initiated_at is None


async def test_a_failing_cancel_is_a_500_and_leaves_the_batch_in_progress(api: Batches) -> None:
    batches = scripted_batches(cancel="error_500")
    created = await api.create()
    with pytest.raises(anthropic.InternalServerError):
        await api.sdk.messages.batches.cancel(created.id)
    assert batches[created.id].cancel_requested is False
    assert (await api.sdk.messages.batches.retrieve(created.id)).processing_status == "in_progress"


async def test_an_unknown_batch_is_a_404(api: Batches) -> None:
    with pytest.raises(anthropic.NotFoundError):
        await api.sdk.messages.batches.retrieve("msgbatch_404")
    with pytest.raises(anthropic.NotFoundError):
        await api.sdk.messages.batches.cancel("msgbatch_404")


def test_a_script_refuses_unknown_outcomes_and_negative_counts() -> None:
    with pytest.raises(ValueError, match="outcome"):
        BatchScript(items={"t1_a1": cast(Any, "lost")})
    with pytest.raises(ValueError, match="outcome"):
        BatchScript(default_item=cast(Any, "lost"))
    with pytest.raises(ValueError, match="polls_until_ended"):
        BatchScript(polls_until_ended=-1)
    with pytest.raises(ValueError, match="stream_cut_after"):
        BatchScript(stream_cut_after=-1)
    with pytest.raises(ValueError, match="create"):
        BatchScript(create=cast(Any, "sometimes"))
