"""The Message Batches client (spec §10.10 L3937, §7.12; plan Task 16, A2 B11, Carries T8-a, T8-b, Ruling
T16-a): the raw SDK, because LangChain cannot do batches, inside `llm/` (the boundary lint, ruff `TID251`).

`BatchesClient(cfg, *, http_client, clock, monotonic, sleep)` builds
`anthropic.AsyncAnthropic(base_url=ANTHROPIC_BASE_URL, api_key=<the anthropic_key secret>, max_retries=0,
timeout=TIMEOUT, http_client=http_client)` the way the gateway builds its own (`gateway._sdk_client`: the
explicit origin and key, no SDK retry, no telemetry and no header from the environment). `http_client` is the
gateway's shared client (`LLMGateway.http_client`), which keeps the fake mount in fake LLM mode; the client's
own timeout is the online one, so every request here passes `TIMEOUT`, `anthropic.Timeout(connect=10,
read=120, write=60, pool=10)`, whose `write=60` is the batches' (L3937: a create uploads up to
`BATCH_MAX_BYTES` of params). In fake mode only, the key is the gateway's placeholder; without the secret
outside fake mode the client is not configured and every method raises `LLMNotConfigured` before any request.

The methods:
- `create(requests) -> Created | Rejected | Ambiguous`: `messages.batches.create(requests=[{custom_id,
  params}])`, every try within the call's `CREATE_DEADLINE_S` (the "10-minute client deadline" of L3985,
  on `monotonic`). Ruling T16-a classifies each answer:
  - a definitive 4xx, one the gateway does not retry (`gateway.is_retryable_status` false: 400, 401, 403,
    404, 413, 422, ...), is `Rejected(status, error_type, message)` at once (§10.10 step 4b);
  - a 409 or a 429 (refused before it was processed), or a connect failure before any byte of the request
    was written (`gateway._connect_failed_before_send` on the cause chain), made no batch: the create is
    sent again within the call, after the gateway's backoff (`gateway.retry_wait`: the response's
    `Retry-After`, else 2 s then 8 s with jitter; waited with `sleep`), up to the gateway's
    `TRANSPORT_TRIES` (3) and while the wait ends before the deadline. When the tries run out it is
    `Rejected` (4b) with the last response's status and error type, or `Rejected(None, "connect_failed",
    "", evidence="connect_failed_before_send")` for a connect failure; `Rejected.evidence` is the items'
    refund evidence (`batch_submit_rejected` otherwise);
  - a timeout (the SDK's, the deadline's, a 408), a connection lost after the send started, a 5xx or a 529,
    and any other SDK failure are `Ambiguous(error_class)`, never sent again: none of them proves that the
    batch was not created (§10.10 step 4c).
  In fake LLM mode only, the create also sends `X-JH-Fake-Key`, the JSON map of each `custom_id` to its
  answer key (`fake.batch_keys`, `fake.answer_key("generate", input_refs, 0)`), and `X-JH-Purpose:
  generate` (Carry T8-a); no request to the API carries either;
- `retrieve(id) -> BatchStatus` and `cancel(id) -> BatchStatus`;
- `results(id) -> ResultsStream`: an async iterator of `ItemResult(custom_id, result_type, message,
  error_type)`, the errored item's type being the SDK's nested `error.error.type` (Carry T8-b); each line
  validated (`MessageBatchIndividualResponse.model_validate`: the SDK's decoder constructs a line without
  checking it), and a line that is no JSON or no valid result ends the stream with `BatchApiError("results",
  status=None, error_class=<the error's class>)`, raised `from None` (the error's message quotes the line,
  which may hold model output); its `complete` is true only once the iteration ended without such an error
  (the stream read to its end, §10.10 step 7);
- `list_all() -> AsyncIterator[BatchSummary]`: every batch of the workspace, newest first, paginated by hand
  with `limit=LIST_PAGE` (100) and `after_id` (the listing's `last_id`) while `has_more`.
A failure of `retrieve`, `results`, `cancel` or `list_all` is `BatchApiError(operation, status,
error_class)`: the status and the class only, never the provider's message (which may quote a request).

Every method logs ids, counts, statuses and codes only: never a param, a message or a provider error text.
"""

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
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
from jobhunter.llm.gateway import (
    CONNECT_FAILED,
    TRANSPORT_TRIES,
    _connect_failed_before_send,
    is_retryable_status,
    retry_wait,
)
from jobhunter.observability.logging import get_logger
from jobhunter.types import JSONValue

# L3985: `create` has a 10-minute client deadline (A2 B11).
CREATE_DEADLINE_S: Final = 600
# L3937: the batches' timeout; `write` is the online one's doubled for the upload.
TIMEOUT: Final = anthropic.Timeout(connect=10, read=120, write=60, pool=10)
LIST_PAGE: Final = 100
# The batches are the generator pass only (§10.10): every item is a `generate` request of round 0.
_PURPOSE: Final = "generate"
# Ruling T16-a: the statuses the gateway retries that refuse a request before it is processed, so a create
# answered with one made no batch and is sent again within the call; the gateway's other retryable statuses
# (408, 5xx, 529) may follow a received request and are ambiguous.
_NOT_SENT: Final = frozenset({409, 429})
# The refund evidence of a rejected create's items (§10.10 4b, the ledger's CHECK).
SUBMIT_REJECTED: Final = "batch_submit_rejected"
CONNECT_FAILED_BEFORE_SEND: Final = "connect_failed_before_send"

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
    """No batch was created (step 4b): a definitive 4xx, or a create provably not sent within the call's
    tries (a 409, a 429, a connect failure before any byte left; Ruling T16-a). `status` is the response's
    (`None` for a connect failure), `evidence` the items' refund evidence; `message` is the provider's and
    never logged."""

    status: int | None
    error_type: str
    message: str = field(repr=False)
    evidence: str = SUBMIT_REJECTED


@dataclass(frozen=True)
class _NotSent:
    """A send that made no batch, worth sending again: what it is when the tries run out, and the response's
    headers (its `Retry-After`; none for a connect failure)."""

    rejected: Rejected
    headers: Mapping[str, str]


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
                # the SDK constructs a line without validating it: checked here, strictly
                checked = MessageBatchIndividualResponse.model_validate(line.to_dict(warnings=False))
                self.read += 1
                yield _item(checked)
        except (anthropic.APIError, httpx2.HTTPError) as exc:
            failure = _failure("results", exc)
            log.warning(
                "llm_batch_results_cut",
                provider_batch_id=self._batch_id,
                read=self.read,
                error_class=failure.error_class,
            )
            raise failure from None
        except ValueError as exc:  # no JSON, or no valid result: the class only, the message quotes the line
            error_class = type(exc).__name__
            log.warning(
                "llm_batch_results_unreadable",
                provider_batch_id=self._batch_id,
                read=self.read,
                error_class=error_class,
            )
            raise BatchApiError("results", status=None, error_class=error_class) from None
        self.complete = True
        log.info("llm_batch_results_read", provider_batch_id=self._batch_id, read=self.read)


class BatchesClient:
    """The raw SDK's Message Batches API on the gateway's `httpx2` client (module docstring)."""

    def __init__(
        self,
        cfg: AppConfig,
        *,
        http_client: httpx2.AsyncClient,
        clock: Callable[[], datetime] = utcnow,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ) -> None:
        self._key = gateway._api_key(cfg)
        self._http = http_client
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
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
        """`messages.batches.create` of `requests`, classified and, while it was provably not sent, retried
        within the call (module docstring)."""
        if not requests:
            raise ValueError("a batch needs at least one request")
        sdk = self._sdk()
        body = [cast(Request, {"custom_id": r.custom_id, "params": dict(r.params)}) for r in requests]
        headers = self._create_headers(requests)
        deadline = self._monotonic() + CREATE_DEADLINE_S
        for transport_try in range(1, TRANSPORT_TRIES + 1):
            started = self._clock()
            outcome = await self._try(sdk, body, headers, remaining=deadline - self._monotonic())
            if not isinstance(outcome, _NotSent):
                return self._logged(outcome, requests, transport_try, started)
            wait = retry_wait(outcome.headers, transport_try, now=self._clock())
            if transport_try == TRANSPORT_TRIES or self._monotonic() + wait >= deadline:
                return self._logged(outcome.rejected, requests, transport_try, started)
            log.info(
                "llm_batch_create_retried",
                requests=len(requests),
                transport_try=transport_try,
                http_status=outcome.rejected.status,
                error_type=outcome.rejected.error_type,
                wait_s=round(wait, 3),
            )
            await self._sleep(wait)
        raise AssertionError("unreachable: the last try returns")  # pragma: no cover

    async def _try(
        self,
        sdk: anthropic.AsyncAnthropic,
        body: list[Request],
        headers: Mapping[str, str],
        *,
        remaining: float,
    ) -> "CreateOutcome | _NotSent":
        """One send of the create, classified (module docstring); `_NotSent` when no batch can exist."""
        try:
            async with asyncio.timeout(remaining):  # what is left of the 10-minute client deadline
                batch = await sdk.messages.batches.create(
                    requests=body, extra_headers=dict(headers), timeout=TIMEOUT
                )
        except anthropic.APIStatusError as exc:
            status = exc.status_code
            if status in _NOT_SENT:  # 409, 429: refused before it was processed
                return _NotSent(Rejected(status, *_error_of(exc)), exc.response.headers)
            if 400 <= status < 500 and not is_retryable_status(status):  # 4b: definitive
                return Rejected(status, *_error_of(exc))
            return Ambiguous(type(exc).__name__)  # 408, 5xx, 529: it may have been received
        except TimeoutError:  # the 10-minute client deadline
            return Ambiguous("TimeoutError")
        except anthropic.APIConnectionError as exc:  # its timeout too
            if _connect_failed_before_send(exc):  # no byte of the request left the process
                not_sent = Rejected(None, CONNECT_FAILED, "", evidence=CONNECT_FAILED_BEFORE_SEND)
                return _NotSent(not_sent, {})
            return Ambiguous(type(exc).__name__)
        except anthropic.APIError as exc:  # an unreadable answer
            return Ambiguous(type(exc).__name__)
        return Created(
            provider_batch_id=batch.id,
            created_at=batch.created_at,
            expires_at=batch.expires_at,
            request_counts=_counts(batch),
        )

    def _logged(
        self, outcome: CreateOutcome, requests: Sequence[BatchRequest], tries: int, started: datetime
    ) -> CreateOutcome:
        """`outcome`, logged with ids, counts and codes only."""
        match outcome:
            case Created():
                log.info(
                    "llm_batch_created",
                    provider_batch_id=outcome.provider_batch_id,
                    requests=len(requests),
                    request_count_total=outcome.request_counts.total,
                    transport_tries=tries,
                    sent_at=started.isoformat(),
                )
            case Rejected():
                log.warning(
                    "llm_batch_create_rejected",
                    requests=len(requests),
                    http_status=outcome.status,
                    error_type=outcome.error_type,
                    evidence=outcome.evidence,
                    transport_tries=tries,
                )
            case Ambiguous():
                log.warning(
                    "llm_batch_create_ambiguous",
                    requests=len(requests),
                    error_class=outcome.error_class,
                    transport_tries=tries,
                )
        return outcome

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
