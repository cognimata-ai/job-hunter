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
from typing import Any, Final

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
from jobhunter.llm.gateway import TRANSPORT_TRIES
from jobhunter.llm.transport import make_http_client

from tests.support.llm import GATEWAY_BODY, FakeLLM, GatewayOut, Sleeps

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


Answer = tuple[int, dict[str, Any]] | str | None


class Answering(httpx2.AsyncBaseTransport):
    """The API origin's transport: every request kept (with its timeout extension), the n-th answered by the
    n-th of `answers` (the last one repeats): a status and a JSON body, `"connect"` (a connect failure before
    any byte was sent), `"read_timeout"` (the answer never read), or `None` (never answered)."""

    def __init__(self, *answers: Answer) -> None:
        self.answers = answers
        self.seen: list[httpx2.Request] = []

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        answer = self.answers[min(len(self.seen), len(self.answers) - 1)]
        self.seen.append(request)
        if answer is None:
            await asyncio.Event().wait()
        if answer == "connect":
            raise httpx2.ConnectError("test: connection refused", request=request)
        if answer == "read_timeout":
            raise httpx2.ReadTimeout("test: no answer", request=request)
        assert isinstance(answer, tuple)
        status, body = answer
        return httpx2.Response(
            status, headers={"request-id": "req_test", **_HEADERS.get(status, {})}, json=body
        )


# A 429's own wait, which the retry honours (gateway.retry_wait); the other errors wait the jittered backoff.
_HEADERS: dict[int, dict[str, str]] = {429: {"retry-after": "7"}}
BATCH: dict[str, Any] = {
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
OK: Final = (200, BATCH)


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
    ("status", "kind"),
    [
        (400, "invalid_request_error"),
        (401, "authentication_error"),
        (403, "permission_error"),
        (404, "not_found_error"),
        (413, "request_too_large"),
        (422, "invalid_request_error"),
    ],
)
async def test_a_definitive_4xx_is_rejected_at_once(fake_llm: FakeLLM, status: int, kind: str) -> None:
    """Ruling T16-a: a 4xx that `gateway.is_retryable_status` does not retry is definitive (§10.10 4b)."""
    transport = Answering((status, error_body(kind)))
    sleeps = Sleeps()
    async with on(fake_llm.cfg, transport) as http:
        outcome = await BatchesClient(fake_llm.cfg, http_client=http, sleep=sleeps).create(requests("t1_a1"))
    assert outcome == Rejected(status, kind, "test: refused")
    assert isinstance(outcome, Rejected) and outcome.evidence == "batch_submit_rejected"
    assert (len(transport.seen), sleeps.waits) == (1, [])  # no SDK retry (max_retries=0), none of ours


@pytest.mark.parametrize(
    ("answer", "error_class"),
    [
        ((408, error_body("timeout_error")), "APIStatusError"),  # the server's timeout proves nothing
        ((500, error_body("api_error")), "InternalServerError"),
        ((503, error_body("api_error")), "InternalServerError"),
        ((529, error_body("overloaded_error")), "OverloadedError"),
        ("read_timeout", "APITimeoutError"),
    ],
)
async def test_a_408_a_5xx_or_a_lost_answer_is_ambiguous_and_never_retried(
    fake_llm: FakeLLM, answer: Answer, error_class: str
) -> None:
    """§10.10 4c: once the request may have been received, the create is never sent again."""
    transport = Answering(answer, OK)
    sleeps = Sleeps()
    async with on(fake_llm.cfg, transport) as http:
        outcome = await BatchesClient(fake_llm.cfg, http_client=http, sleep=sleeps).create(requests("t1_a1"))
    assert outcome == Ambiguous(error_class)
    assert (len(transport.seen), sleeps.waits) == (1, [])


@pytest.mark.parametrize(
    ("first", "waits"),
    [
        ((409, error_body("conflict_error")), None),
        ((429, error_body("rate_limit_error")), [7.0]),  # its Retry-After
        ("connect", None),  # nothing left the process
    ],
)
async def test_a_create_that_was_not_sent_is_retried_in_the_same_call(
    fake_llm: FakeLLM, first: Answer, waits: list[float] | None
) -> None:
    """Ruling T16-a: a 409, a 429 or a connect failure before any byte left made no batch, so the create is
    sent again within the call, after the gateway's wait (`Retry-After` first); one batch is created."""
    transport = Answering(first, OK)
    sleeps = Sleeps()
    async with on(fake_llm.cfg, transport) as http:
        outcome = await BatchesClient(fake_llm.cfg, http_client=http, sleep=sleeps).create(requests("t1_a1"))
    assert isinstance(outcome, Created) and outcome.provider_batch_id == "msgbatch_t"
    assert len(transport.seen) == 2 and len(sleeps.waits) == 1
    if waits is None:  # the jittered backoff after the first try: 2 s ± 25 %
        assert 1.5 <= sleeps.waits[0] <= 2.5
    else:
        assert sleeps.waits == waits


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ((429, error_body("rate_limit_error")), Rejected(429, "rate_limit_error", "test: refused")),
        (
            "connect",
            Rejected(None, "connect_failed", "", evidence="connect_failed_before_send"),
        ),
    ],
)
async def test_a_create_never_sent_after_the_bound_is_rejected(
    fake_llm: FakeLLM, answer: Answer, expected: Rejected
) -> None:
    """Ruling T16-a: after the gateway's three tries the not-sent create is `Rejected` (§10.10 4b): no batch
    can exist; a connect failure carries the evidence `connect_failed_before_send`."""
    transport = Answering(answer)
    sleeps = Sleeps()
    async with on(fake_llm.cfg, transport) as http:
        outcome = await BatchesClient(fake_llm.cfg, http_client=http, sleep=sleeps).create(requests("t1_a1"))
    assert isinstance(outcome, Rejected) and (outcome, outcome.evidence) == (expected, expected.evidence)
    assert len(transport.seen) == TRANSPORT_TRIES == 3 and len(sleeps.waits) == 2


async def test_the_retries_stop_at_the_creates_deadline(
    fake_llm: FakeLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wait that would pass `CREATE_DEADLINE_S` is not waited: the not-sent create is rejected there."""
    monkeypatch.setattr(batches_mod, "CREATE_DEADLINE_S", 5)  # less than the 429's 7 s
    transport = Answering((429, error_body("rate_limit_error")))
    sleeps = Sleeps()
    async with on(fake_llm.cfg, transport) as http:
        outcome = await BatchesClient(fake_llm.cfg, http_client=http, sleep=sleeps).create(requests("t1_a1"))
    assert outcome == Rejected(429, "rate_limit_error", "test: refused")
    assert (len(transport.seen), sleeps.waits) == (1, [])


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
    transport = Answering(OK)
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


class ResultsLines(httpx2.AsyncBaseTransport):
    """An ended batch whose results file holds `lines` (raw JSONL)."""

    def __init__(self, lines: bytes) -> None:
        self.lines = lines

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/results"):
            return httpx2.Response(200, headers={"request-id": "req_test"}, content=self.lines)
        url = str(request.url.copy_with(path="/v1/messages/batches/msgbatch_t/results", query=None))
        ended = {
            **BATCH,
            "processing_status": "ended",
            "ended_at": "2026-10-26T13:00:00Z",
            "results_url": url,
        }
        return httpx2.Response(200, headers={"request-id": "req_test"}, json=ended)


@pytest.mark.parametrize(
    "bad",
    [
        b"this line is no JSON, it quotes model output\n",
        b'{"custom_id": "t2_a1", "result": {"type": "weird"}}\n',
        b'{"custom_id": "t2_a1", "result": {"type": "errored"}}\n',
        b'{"custom_id": "t2_a1"}\n',
    ],
)
async def test_an_unreadable_result_line_ends_the_stream_with_its_class_only(
    fake_llm: FakeLLM, bad: bytes
) -> None:
    """Fix round 1 (Minor 8): a line that is no JSON, or no valid result, is a `BatchApiError` carrying the
    error's class only (its message could quote the line), and the stream is not complete."""
    good = b'{"custom_id": "t1_a1", "result": {"type": "expired"}}\n'
    async with make_http_client(fake_llm.cfg, mount=ResultsLines(good + bad)) as http:
        stream = await BatchesClient(fake_llm.cfg, http_client=http).results("msgbatch_t")
        read: list[str] = []
        with pytest.raises(BatchApiError) as unreadable:
            async for item in stream:
                read.append(item.custom_id)
    assert read == ["t1_a1"] and not stream.complete
    failure = unreadable.value
    assert (failure.operation, failure.status) == ("results", None)
    assert failure.error_class in ("JSONDecodeError", "ValidationError")
    assert "model output" not in str(failure) and failure.__cause__ is None and failure.__suppress_context__
