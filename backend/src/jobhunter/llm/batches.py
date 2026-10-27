"""The Message Batches client (spec §10.10 L3937, §7.12; plan Task 16, A2 B11, Carries T8-a, T8-b): the raw
SDK, because LangChain cannot do batches, inside `llm/` (the boundary lint, ruff `TID251`).

`BatchesClient(cfg, *, http_client, clock)` builds `anthropic.AsyncAnthropic(base_url=ANTHROPIC_BASE_URL,
api_key=<the anthropic_key secret>, max_retries=0, timeout=TIMEOUT, http_client=http_client)` the way the
gateway builds its own (`gateway._sdk_client`: the explicit origin and key, no SDK retry, no telemetry and no
header from the environment). `http_client` is the gateway's shared client (`LLMGateway.http_client`), which
keeps the fake mount in fake LLM mode; the client's own timeout is the online one, so every request here
passes `TIMEOUT`, `anthropic.Timeout(connect=10, read=120, write=60, pool=10)`, whose `write=60` is the
batches' (L3937: a create uploads up to `BATCH_MAX_BYTES` of params). In fake mode only, the key is the
gateway's placeholder; without the secret outside fake mode the client is not configured and every method
raises `LLMNotConfigured` before any request.

The methods:
- `create(requests) -> Created | Rejected | Ambiguous`: `messages.batches.create(requests=[{custom_id,
  params}])` under `asyncio.timeout(CREATE_DEADLINE_S)`, the "10-minute client deadline" of L3985. A
  definitive 4xx (every 4xx but 408, a request timeout) is `Rejected(status, error_type, message)`; a
  timeout (the SDK's, the deadline's, a 408), a connection loss, a 5xx or a 529, and any other SDK failure
  are `Ambiguous(error_class)`, because none of them proves that the batch was not created (§10.10 step 4c).
  In fake LLM mode only, the create also sends `X-JH-Fake-Key`, the JSON map of each `custom_id` to its
  answer key (`fake.batch_keys`, `fake.answer_key("generate", input_refs, 0)`), and `X-JH-Purpose:
  generate` (Carry T8-a); no request to the API carries either;
- `retrieve(id) -> BatchStatus` and `cancel(id) -> BatchStatus`;
- `results(id) -> ResultsStream`: an async iterator of `ItemResult(custom_id, result_type, message,
  error_type)`, the errored item's type being the SDK's nested `error.error.type` (Carry T8-b); its
  `complete` is true only once the iteration ended without a transport error (the stream read to its end,
  §10.10 step 7);
- `list_all() -> AsyncIterator[BatchSummary]`: every batch of the workspace, newest first, paginated by hand
  with `limit=LIST_PAGE` (100) and `after_id` (the listing's `last_id`) while `has_more`.
A failure of `retrieve`, `results`, `cancel` or `list_all` is `BatchApiError(operation, status,
error_class)`: the status and the class only, never the provider's message (which may quote a request).

Every method logs ids, counts, statuses and codes only: never a param, a message or a provider error text.
"""

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any, Final, Literal, cast

import anthropic
import httpx2
from anthropic.types import Message
from anthropic.types.messages import MessageBatch, MessageBatchIndividualResponse
from anthropic.types.messages.batch_create_params import Request

from jobhunter.clock import utcnow
from jobhunter.config import AppConfig
from jobhunter.llm import gateway
from jobhunter.llm.errors import LLMError, LLMNotConfigured
from jobhunter.llm.fake import KEY_HEADER, PURPOSE_HEADER, answer_key, batch_keys
from jobhunter.observability.logging import get_logger
from jobhunter.types import JSONValue

# L3985: `create` has a 10-minute client deadline (A2 B11).
CREATE_DEADLINE_S: Final = 600
# L3937: the batches' timeout; `write` is the online one's doubled for the upload.
TIMEOUT: Final = anthropic.Timeout(connect=10, read=120, write=60, pool=10)
LIST_PAGE: Final = 100
# The batches are the generator pass only (§10.10): every item is a `generate` request of round 0.
_PURPOSE: Final = "generate"
# A 408 is the server's request timeout: like the client's own, it proves nothing about the creation.
_TIMEOUT_STATUS: Final = 408

ProcessingStatus = Literal["in_progress", "canceling", "ended"]
ResultType = Literal["succeeded", "errored", "canceled", "expired"]

log = get_logger("jobhunter.llm.batches")


@dataclass(frozen=True)
class BatchRequest:
    """One item of a create: its `custom_id`, its params (the online generator body without `stream`,
    `gateway.batch_request_body`) and the ids of its inputs, from which fake mode alone derives its answer
    key."""

    custom_id: str
    params: Mapping[str, JSONValue] = field(repr=False)
    input_refs: Mapping[str, JSONValue] = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True)
class RequestCounts:
    """A batch's `request_counts`."""

    processing: int
    succeeded: int
    errored: int
    canceled: int
    expired: int

    @property
    def total(self) -> int:
        return self.processing + self.succeeded + self.errored + self.canceled + self.expired


@dataclass(frozen=True)
class Created:
    """The provider created the batch (§10.10 step 4a)."""

    provider_batch_id: str
    created_at: datetime
    expires_at: datetime
    request_counts: RequestCounts


@dataclass(frozen=True)
class Rejected:
    """A definitive 4xx (step 4b): the batch was not created. `message` is the provider's and never logged."""

    status: int
    error_type: str
    message: str = field(repr=False)


@dataclass(frozen=True)
class Ambiguous:
    """No proof either way (step 4c): the create may or may not have made a batch."""

    error_class: str


CreateOutcome = Created | Rejected | Ambiguous


@dataclass(frozen=True)
class BatchStatus:
    """A retrieval's (or a cancel's) view of a batch."""

    processing_status: ProcessingStatus
    request_counts: RequestCounts
    ended_at: datetime | None
    expires_at: datetime


@dataclass(frozen=True)
class BatchSummary:
    """One batch of the listing (the ambiguous reconciler's candidates, L3984)."""

    id: str
    created_at: datetime
    request_counts: RequestCounts
    processing_status: ProcessingStatus


@dataclass(frozen=True)
class ItemResult:
    """One result line: the item's `custom_id`, its result type, the message of a succeeded item, and the
    provider error type of an errored one (`error.error.type`, Carry T8-b)."""

    custom_id: str
    result_type: ResultType
    message: Message | None = field(default=None, repr=False)
    error_type: str | None = None


class BatchApiError(LLMError):
    """A batch API call other than the create failed: the operation, the HTTP status (`None` without a
    response) and the error's class, never the provider's message."""

    def __init__(self, operation: str, *, status: int | None, error_class: str) -> None:
        super().__init__(f"batch {operation} failed: {error_class} (status {status})")
        self.operation = operation
        self.status = status
        self.error_class = error_class


def _counts(batch: MessageBatch) -> RequestCounts:
    c = batch.request_counts
    return RequestCounts(
        processing=c.processing,
        succeeded=c.succeeded,
        errored=c.errored,
        canceled=c.canceled,
        expired=c.expired,
    )


def _status(batch: MessageBatch) -> BatchStatus:
    return BatchStatus(
        processing_status=batch.processing_status,
        request_counts=_counts(batch),
        ended_at=batch.ended_at,
        expires_at=batch.expires_at,
    )


def _failure(operation: str, exc: anthropic.APIError | httpx2.HTTPError) -> BatchApiError:
    status = exc.status_code if isinstance(exc, anthropic.APIStatusError) else None
    return BatchApiError(operation, status=status, error_class=type(exc).__name__)


def _error_of(exc: anthropic.APIStatusError) -> tuple[str, str]:
    """The provider's `error.type` and `error.message` of an error response (`http_<status>` and an empty
    message when the body names none)."""
    body = exc.body
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return f"http_{exc.status_code}", ""
    kind, message = error.get("type"), error.get("message")
    return (
        kind if isinstance(kind, str) else f"http_{exc.status_code}",
        message if isinstance(message, str) else "",
    )


def _item(line: MessageBatchIndividualResponse) -> ItemResult:
    result = line.result
    if result.type == "succeeded":
        return ItemResult(line.custom_id, "succeeded", message=result.message)
    if result.type == "errored":
        return ItemResult(line.custom_id, "errored", error_type=result.error.error.type)
    return ItemResult(line.custom_id, result.type)


class ResultsStream:
    """The result lines of one batch as `ItemResult`s (module docstring); `complete` once the iteration
    ended without a transport error. It can be iterated once."""

    def __init__(self, batch_id: str, lines: AsyncIterator[MessageBatchIndividualResponse]) -> None:
        self._batch_id = batch_id
        self._lines = lines
        self.complete = False
        self.read = 0

    def __aiter__(self) -> AsyncIterator[ItemResult]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[ItemResult]:
        try:
            async for line in self._lines:
                self.read += 1
                yield _item(line)
        except (anthropic.APIError, httpx2.HTTPError) as exc:
            failure = _failure("results", exc)
            log.warning(
                "llm_batch_results_cut",
                provider_batch_id=self._batch_id,
                read=self.read,
                error_class=failure.error_class,
            )
            raise failure from None
        self.complete = True
        log.info("llm_batch_results_read", provider_batch_id=self._batch_id, read=self.read)


class BatchesClient:
    """The raw SDK's Message Batches API on the gateway's `httpx2` client (module docstring)."""

    def __init__(
        self, cfg: AppConfig, *, http_client: httpx2.AsyncClient, clock: Callable[[], datetime] = utcnow
    ) -> None:
        self._key = gateway._api_key(cfg)
        self._http = http_client
        self._clock = clock
        self._fake_llm = cfg.fake_llm

    @property
    def configured(self) -> bool:
        """Whether this client can make calls: it has the key, or it is on the fake LLM."""
        return self._key is not None

    def _sdk(self) -> anthropic.AsyncAnthropic:
        if self._key is None:
            raise LLMNotConfigured()
        return gateway._sdk_client(self._key, self._http, TIMEOUT, {})

    def _create_headers(self, requests: Sequence[BatchRequest]) -> dict[str, str]:
        """The fake's headers (Carry T8-a), in fake LLM mode only: none outside it."""
        if not self._fake_llm:
            return {}
        keys = {r.custom_id: answer_key(_PURPOSE, r.input_refs, 0) for r in requests}
        return {PURPOSE_HEADER: _PURPOSE, KEY_HEADER: batch_keys(keys)}

    async def create(self, requests: Sequence[BatchRequest]) -> CreateOutcome:
        """`messages.batches.create` of `requests`, classified (module docstring)."""
        if not requests:
            raise ValueError("a batch needs at least one request")
        sdk = self._sdk()
        body = [cast(Request, {"custom_id": r.custom_id, "params": dict(r.params)}) for r in requests]
        started = self._clock()
        try:
            async with asyncio.timeout(CREATE_DEADLINE_S):
                batch = await sdk.messages.batches.create(
                    requests=body, extra_headers=self._create_headers(requests), timeout=TIMEOUT
                )
        except anthropic.APIStatusError as exc:
            status = exc.status_code
            if 400 <= status < 500 and status != _TIMEOUT_STATUS:
                rejected = Rejected(status, *_error_of(exc))
                log.warning(
                    "llm_batch_create_rejected",
                    requests=len(requests),
                    http_status=status,
                    error_type=rejected.error_type,
                )
                return rejected
            return self._ambiguous(requests, type(exc).__name__, status=status)
        except TimeoutError:  # the 10-minute client deadline
            return self._ambiguous(requests, "TimeoutError", status=None)
        except anthropic.APIError as exc:  # a timeout, a connection loss, an unreadable answer
            return self._ambiguous(requests, type(exc).__name__, status=None)
        created = Created(
            provider_batch_id=batch.id,
            created_at=batch.created_at,
            expires_at=batch.expires_at,
            request_counts=_counts(batch),
        )
        log.info(
            "llm_batch_created",
            provider_batch_id=batch.id,
            requests=len(requests),
            request_count_total=created.request_counts.total,
            sent_at=started.isoformat(),
        )
        return created

    def _ambiguous(
        self, requests: Sequence[BatchRequest], error_class: str, *, status: int | None
    ) -> Ambiguous:
        log.warning(
            "llm_batch_create_ambiguous", requests=len(requests), error_class=error_class, http_status=status
        )
        return Ambiguous(error_class)

    async def retrieve(self, batch_id: str) -> BatchStatus:
        """`messages.batches.retrieve`; `BatchApiError` on failure."""
        try:
            batch = await self._sdk().messages.batches.retrieve(batch_id, timeout=TIMEOUT)
        except (anthropic.APIError, httpx2.HTTPError) as exc:
            raise _failure("retrieve", exc) from None
        status = _status(batch)
        log.info(
            "llm_batch_retrieved",
            provider_batch_id=batch_id,
            processing_status=status.processing_status,
            processing=status.request_counts.processing,
        )
        return status

    async def cancel(self, batch_id: str) -> BatchStatus:
        """`messages.batches.cancel`; `BatchApiError` on failure."""
        try:
            batch = await self._sdk().messages.batches.cancel(batch_id, timeout=TIMEOUT)
        except (anthropic.APIError, httpx2.HTTPError) as exc:
            raise _failure("cancel", exc) from None
        log.info(
            "llm_batch_cancel_requested",
            provider_batch_id=batch_id,
            processing_status=batch.processing_status,
        )
        return _status(batch)

    async def results(self, batch_id: str) -> ResultsStream:
        """The batch's result stream (module docstring); `BatchApiError` when it cannot be opened."""
        try:
            decoder = await self._sdk().messages.batches.results(batch_id, timeout=TIMEOUT)
        except (anthropic.APIError, httpx2.HTTPError) as exc:
            raise _failure("results", exc) from None
        return ResultsStream(batch_id, decoder.__aiter__())

    async def list_all(self) -> AsyncIterator[BatchSummary]:
        """Every batch of the workspace, newest first (module docstring); `BatchApiError` on failure."""
        sdk = self._sdk()
        after: str | None = None
        pages = listed = 0
        while True:
            kwargs: dict[str, Any] = {"limit": LIST_PAGE, "timeout": TIMEOUT}
            if after is not None:
                kwargs["after_id"] = after
            try:
                page = await sdk.messages.batches.list(**kwargs)
            except (anthropic.APIError, httpx2.HTTPError) as exc:
                raise _failure("list", exc) from None
            pages += 1
            for batch in page.data:
                listed += 1
                yield BatchSummary(
                    id=batch.id,
                    created_at=batch.created_at,
                    request_counts=_counts(batch),
                    processing_status=batch.processing_status,
                )
            if not page.has_more or not page.data or page.last_id is None:
                break
            after = page.last_id
        log.info("llm_batches_listed", pages=pages, batches=listed)
