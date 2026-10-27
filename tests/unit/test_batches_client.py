"""The Message Batches client (spec §10.10 L3937, L3985; plan Task 16, A2 B11, Carries T8-a, T8-b): `create`
classified as `Created`, `Rejected` (a definitive 4xx) or `Ambiguous` (a timeout, a connection loss, a 5xx or
529, the 10-minute deadline), the batches' own timeout on every request, `list_all` paging by `after_id`,
`results` complete only after the stream's end, `retrieve` and `cancel`.

Every request goes to the fake Batches API (`jobhunter.llm.fake`) on the gateway's http client, or to a
transport of the test's own for the answers the fake has no script for (a 529, a 408, a stall). Offline; the
params are synthetic.
"""

import asyncio
import dataclasses
import json
from collections.abc import AsyncIterator
from datetime import timedelta
from types import MappingProxyType
from typing import Any

import httpx2
import pytest
from jobhunter.config import AppConfig
from jobhunter.llm import batches as batches_mod
from jobhunter.llm.batches import (
    Ambiguous,
    BatchApiError,
    BatchesClient,
    BatchRequest,
    Created,
    ItemResult,
    Rejected,
)
from jobhunter.llm.errors import LLMNotConfigured
from jobhunter.llm.fake import BatchScript
from jobhunter.llm.transport import make_http_client

from tests.support.llm import GATEWAY_BODY, FakeLLM, GatewayOut

MODEL = "claude-sonnet-5-5"


def params(*, max_tokens: int = 6000) -> dict[str, Any]:
    """An item's params (the shape of the online generator body without `stream`); the `generate`
    recording of `GatewayOut` that `fake_llm` wrote answers it."""
    from jobhunter.llm.gateway import WireSchema

    return {
        "model": MODEL,
        "max_tokens": max_tokens,
        "system": [{"type": "text", "text": "You score a job ad.", "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": "A synthetic ad."}],
        "output_config": {
            "effort": "medium",
            "format": {"type": "json_schema", "schema": dict(WireSchema.from_model(GatewayOut).json)},
        },
    }


def requests(*ids: str) -> list[BatchRequest]:
    return [BatchRequest(cid, params(), {"job_revision_id": 100 + i}) for i, cid in enumerate(ids)]


@pytest.fixture
async def http(fake_llm: FakeLLM) -> AsyncIterator[httpx2.AsyncClient]:
    async with fake_llm.client() as client:
        yield client


@pytest.fixture
def client(fake_llm: FakeLLM, http: httpx2.AsyncClient) -> BatchesClient:
    return BatchesClient(fake_llm.cfg, http_client=http)


class Answering(httpx2.AsyncBaseTransport):
    """The API origin's transport: every request kept (with its timeout extension), each answered by `answer`
    (a status and a JSON body), or never answered (`None`)."""

    def __init__(self, answer: tuple[int, dict[str, Any]] | None) -> None:
        self.answer = answer
        self.seen: list[httpx2.Request] = []

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.seen.append(request)
        if self.answer is None:
            await asyncio.Event().wait()
        status, body = self.answer  # type: ignore[misc]
        return httpx2.Response(status, headers={"request-id": "req_test"}, json=body)


def error_body(kind: str) -> dict[str, Any]:
    return {"type": "error", "error": {"type": kind, "message": "test: refused"}, "request_id": "req_test"}


def on(cfg: AppConfig, transport: Answering) -> httpx2.AsyncClient:
    return make_http_client(cfg, mount=transport)


# ---- create ----------------------------------------------------------------------------------------------


async def test_create_on_the_fake_is_created_with_its_times_and_counts(
    fake_llm: FakeLLM, client: BatchesClient
) -> None:
    fake_llm.batches(BatchScript(create="ok"))
    created = await client.create(requests("t1_a1", "t2_a1"))
    assert isinstance(created, Created)
    stored = fake_llm.state.batches[created.provider_batch_id]
    assert stored.custom_ids == {"t1_a1", "t2_a1"}
    assert created.created_at == stored.created_at and created.expires_at == stored.created_at + timedelta(
        hours=24
    )
    assert (created.request_counts.processing, created.request_counts.total) == (2, 2)
    # fake mode: each item's answer key, from its input refs (Carry T8-a)
    assert stored.keys == {"t1_a1": "generate:100:0", "t2_a1": "generate:101:0"}


async def test_a_definitive_400_is_rejected(fake_llm: FakeLLM, client: BatchesClient) -> None:
    fake_llm.batches(BatchScript(create="reject_400"))
    rejected = await client.create(requests("t1_a1"))
    assert isinstance(rejected, Rejected)
    assert (rejected.status, rejected.error_type) == (400, "invalid_request_error")
    assert "message" not in repr(rejected)  # the provider's text never reaches a log line through repr
    assert fake_llm.state.batches == {}


@pytest.mark.parametrize(
    ("outcome", "error_class", "stored"),
    [
        ("error_500", "InternalServerError", 0),
        ("timeout", "APITimeoutError", 0),
        ("ambiguous_created", "APIConnectionError", 1),  # created, then the connection dropped
        ("ambiguous_not_created", "APIConnectionError", 0),
    ],
)
async def test_a_5xx_a_timeout_or_a_connection_loss_is_ambiguous(
    fake_llm: FakeLLM, client: BatchesClient, outcome: Any, error_class: str, stored: int
) -> None:
    fake_llm.batches(BatchScript(create=outcome))
    assert await client.create(requests("t1_a1")) == Ambiguous(error_class)
    assert len(fake_llm.state.batches) == stored


@pytest.mark.parametrize(
    ("status", "kind", "expected"),
    [
        (529, "overloaded_error", Ambiguous("OverloadedError")),
        (503, "api_error", Ambiguous("InternalServerError")),
        (408, "timeout_error", Ambiguous("APIStatusError")),  # the server's timeout proves nothing either
        (429, "rate_limit_error", None),
        (413, "request_too_large", None),
    ],
)
async def test_the_status_decides_rejected_or_ambiguous(
    fake_llm: FakeLLM, status: int, kind: str, expected: Ambiguous | None
) -> None:
    transport = Answering((status, error_body(kind)))
    async with on(fake_llm.cfg, transport) as http:
        outcome = await BatchesClient(fake_llm.cfg, http_client=http).create(requests("t1_a1"))
    if expected is None:
        assert outcome == Rejected(status, kind, "test: refused")
    else:
        assert isinstance(outcome, Ambiguous) and outcome.error_class == expected.error_class
    assert len(transport.seen) == 1  # no SDK retry (max_retries=0)


async def test_create_runs_under_the_ten_minute_deadline(
    fake_llm: FakeLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert batches_mod.CREATE_DEADLINE_S == 600  # L3985: the creation window's 10 minutes
    monkeypatch.setattr(batches_mod, "CREATE_DEADLINE_S", 0.05)
    transport = Answering(None)  # never answers: only the deadline ends the wait
    async with on(fake_llm.cfg, transport) as http:
        outcome = await BatchesClient(fake_llm.cfg, http_client=http).create(requests("t1_a1"))
    assert outcome == Ambiguous("TimeoutError")


async def test_every_request_carries_the_batches_timeout(fake_llm: FakeLLM) -> None:
    """L3937: `write=60` for the upload (the online timeout's is 30), on the shared client's every request."""
    batch = {
        "id": "msgbatch_t",
        "type": "message_batch",
        "processing_status": "in_progress",
        "request_counts": {"processing": 1, "succeeded": 0, "errored": 0, "canceled": 0, "expired": 0},
        "ended_at": None,
        "created_at": "2026-10-26T12:00:00Z",
        "expires_at": "2026-10-27T12:00:00Z",
        "archived_at": None,
        "cancel_initiated_at": None,
        "results_url": None,
    }
    transport = Answering((200, batch))
    async with on(fake_llm.cfg, transport) as http:
        client = BatchesClient(fake_llm.cfg, http_client=http)
        await client.create(requests("t1_a1"))
        await client.retrieve("msgbatch_t")
        await client.cancel("msgbatch_t")
    assert [r.extensions["timeout"] for r in transport.seen] == [
        {"connect": 10, "read": 120, "write": 60, "pool": 10}
    ] * 3


async def test_a_client_without_a_key_outside_fake_mode_sends_nothing(fake_llm: FakeLLM) -> None:
    live = dataclasses.replace(fake_llm.cfg, fake_llm=False, secrets=MappingProxyType({}))
    transport = Answering((200, {}))
    async with on(live, transport) as http:
        client = BatchesClient(live, http_client=http)
        assert not client.configured
        with pytest.raises(LLMNotConfigured):
            await client.create(requests("t1_a1"))
    assert transport.seen == []


# ---- retrieve, cancel, the listing, the results ------------------------------------------------------------


async def test_retrieve_and_cancel_report_the_batch(fake_llm: FakeLLM, client: BatchesClient) -> None:
    fake_llm.batches(BatchScript(create="ok", polls_until_ended=5))
    created = await client.create(requests("t1_a1", "t2_a1"))
    assert isinstance(created, Created)
    status = await client.retrieve(created.provider_batch_id)
    assert (status.processing_status, status.request_counts.processing, status.ended_at) == (
        "in_progress",
        2,
        None,
    )
    assert status.expires_at == created.expires_at
    cancelled = await client.cancel(created.provider_batch_id)
    assert cancelled.processing_status == "canceling"
    with pytest.raises(BatchApiError) as missing:
        await client.retrieve("msgbatch_absent")
    assert (missing.value.operation, missing.value.status, missing.value.error_class) == (
        "retrieve",
        404,
        "NotFoundError",
    )


async def test_list_all_pages_by_after_id_newest_first(
    fake_llm: FakeLLM, http: httpx2.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert batches_mod.LIST_PAGE == 100
    monkeypatch.setattr(batches_mod, "LIST_PAGE", 2)
    fake_llm.batches(BatchScript(create="ok"))
    client = BatchesClient(fake_llm.cfg, http_client=http)
    made = []
    for i in range(5):
        created = await client.create(requests(f"t{i}_a1"))
        assert isinstance(created, Created)
        made.append(created.provider_batch_id)
    listings: list[httpx2.URL] = []

    async def seen(request: httpx2.Request) -> None:
        if request.method == "GET" and request.url.path == "/v1/messages/batches":
            listings.append(request.url)

    http.event_hooks["request"].append(seen)
    listed = [summary async for summary in client.list_all()]
    assert [s.id for s in listed] == made[::-1]
    assert [s.request_counts.total for s in listed] == [1] * 5
    assert [(u.params.get("limit"), u.params.get("after_id")) for u in listings] == [
        ("2", None),
        ("2", made[3]),
        ("2", made[1]),
    ]


async def test_results_are_complete_only_after_the_streams_end(
    fake_llm: FakeLLM, client: BatchesClient
) -> None:
    fake_llm.batches(
        BatchScript(create="ok", polls_until_ended=0, items={"t2_a1": "errored:invalid_request"})
    )
    created = await client.create(requests("t1_a1", "t2_a1", "t3_a1"))
    assert isinstance(created, Created)
    stream = await client.results(created.provider_batch_id)
    assert not stream.complete
    items: list[ItemResult] = []
    async for item in stream:
        items.append(item)
        assert not stream.complete  # not before the end
    assert stream.complete
    by_id = {item.custom_id: item for item in items}
    assert set(by_id) == {"t1_a1", "t2_a1", "t3_a1"}
    assert (by_id["t2_a1"].result_type, by_id["t2_a1"].error_type, by_id["t2_a1"].message) == (
        "errored",
        "invalid_request_error",  # the SDK's nested error.error.type (Carry T8-b)
        None,
    )
    succeeded = by_id["t1_a1"]
    assert succeeded.result_type == "succeeded" and succeeded.message is not None
    assert json.loads(succeeded.message.content[0].text) == GATEWAY_BODY


async def test_a_cut_results_stream_is_never_complete(fake_llm: FakeLLM, client: BatchesClient) -> None:
    fake_llm.batches(BatchScript(create="ok", polls_until_ended=0, stream_cut_after=1))
    created = await client.create(requests("t1_a1", "t2_a1", "t3_a1"))
    assert isinstance(created, Created)
    stream = await client.results(created.provider_batch_id)
    read: list[str] = []
    with pytest.raises(BatchApiError) as cut:
        async for item in stream:
            read.append(item.custom_id)
    assert cut.value.operation == "results" and cut.value.status is None
    assert not stream.complete and len(read) <= 1
