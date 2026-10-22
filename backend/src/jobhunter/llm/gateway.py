"""The LLM gateway (spec §10): the only module that constructs a Claude client (§10.1; ruff `TID251`), and the
one call loop every Claude call goes through.

**The client** (plan Task 4). `GatewayChatAnthropic` is the G3 fallback, which spike V5 shows is needed
(`docs/spikes/V5.md`): langchain's `ChatAnthropic` types `timeout` as a float, so it refuses
`anthropic.Timeout`, and it has no `http_client` field (one passed is moved into `model_kwargs`, the request
body), building its own cached client from `base_url` and the environment instead. The subclass takes both and
overrides the async client factory to build `anthropic.AsyncAnthropic(base_url=…, api_key=…, timeout=…,
max_retries=0, http_client=…)`. There is no sync client: every gateway call is async. `_chat` is the §10.2
runnable of one HTTP try.

**The loop** (`LLMGateway.call`, §10.2 in its order; plan Task 5). One logical call makes at most 3 transport
tries per `max_tokens` stage and 2 stages. Every try runs four steps, each its own transaction on the engine,
so a crash, a takeover or a stall can never double-spend, spend past the cap or refund without evidence (§6.9,
G9):
1. **reserve** (`spend_ledger.reserve`, under the owner predicate and the month lock) its worst case, with
   `logical_call_no` and the node's `deadline_s`; `BudgetExhausted` and `FencedOut` leave nothing behind;
2. **the dispatch marker** (`spend_ledger.mark_dispatched`, under the owner predicate again). A lost owner is
   `FencedOut`: the try is released `not_billed(never_dispatched)` in its own transaction and nothing is sent;
3. **stream once** (`_chat(...).ainvoke`), bounded by what is left of `CallContext.deadline` (plan Deviation
   28: the active `deadline_s` is enforced per call);
4. **settle** the try on its own (`spend_ledger.settle`), and only after that transaction has committed the
   spend metrics (`record_settlement_metrics`) and `jobhunter_llm_calls_total` move.

The fault points `gateway.after_reserve` and `gateway.after_dispatch` mark the windows between the steps. A
crash or cancellation between them leaves the try to `llm_ledger_unsettled`: `reserved` → released once its
owner no longer holds, `dispatched` → `ambiguous` (§17.4).

**Not configured** (plan Task 23). A worker with neither the `anthropic_key` secret nor the fake LLM still
starts: its gateway is built without a key (`configured` is false; `worker.main` logs `llm_not_configured`
once), and every `call`, `count_body` and `count_tokens` raises `LLMNotConfigured` as its first step, before
the input bound, any transaction (no reservation, no ledger row, no call row) and any request. The LLM nodes
end their row `failed('llm_not_configured')` (`graphs.profile.nodes.llm_outcome`). The fake and the keyed
gateway are unchanged.

Before the loop: the input bound. A text request is `bounds.text_input_bound` of `RenderedPrompt.as_request`;
a native PDF (`CallContext.native_pdf`) is `count_tokens` on the exact request and `bounds.native_pdf_bound`,
and its tries reserve with `native_pdf_extra=True`. A failed count, a count above the context window, or a
text that cannot be encoded as UTF-8 is `InputUnbounded`, with nothing reserved or sent. A first try needs
`FIRST_TRY_MIN_REMAINING_S` of the deadline left, any later try `RETRY_MIN_REMAINING_S` (`DeadlineExhausted`).

**Each try's end** (§10.4, §10.5, §10.7): the ledger state, the `llm_calls` status and `error_code` (also the
`outcome` label of `jobhunter_llm_calls_total`, `ok` when there is none), and what follows.
- A response that streamed to its end is `billed` from its usage and its call `completed`, checked in this
  order: a served model other than the requested one is `model_mismatch`, `ModelMismatch` and the critical
  `llm.model_mismatch`; `max_tokens` in the first stage is `max_tokens_first_stage` and the enlarged stage
  follows (no backoff), in the enlarged stage `max_tokens` and `MaxTokensExhausted`; `refusal` is `refused`
  and `Refused(category or "unspecified")`; `end_turn` with its parsed output returns the `GatewayResult`
  (the `output_json`), with a parsing or structural error it is `invalid_output` and
  `InvalidOutput("structural")`, the caller's one repair; any other stop reason is `unexpected_stop_reason`,
  `InvalidOutput("unexpected_stop_reason")` and the warn `llm.unexpected_stop_reason` (Deviation 6).
- An error response (4xx, 5xx) is `not_billed(error_response_no_content)`, its call `error` with
  `http_<status>`. 408, 409, 429, 5xx and 529 are retried after the `Retry-After` it asks for, else after the
  jittered 2 s / 8 s; any other status is `LLMRequestError(status)`. 401 and 403 raise the critical
  `llm.auth_failed`, which the next billed try resolves.
- A connect failure before the send is `not_billed(connect_failed_before_send)`, `error` with
  `connect_failed`, and retried after the backoff.
- A lost stream (a read timeout, an error event inside a 200 stream, a stream that ends without its final
  event) is `ambiguous` with `stream_interrupted`: it keeps its reservation, and the retry after the backoff
  reserves anew (C15).
- The call's deadline passing in flight is `ambiguous` with `deadline`, then `DeadlineExhausted`.
- A cancellation (`TimeoutPolicy`, a reconciler, the shutdown) is `ambiguous` with `cancelled`, settled in the
  cancelled task, and the `CancelledError` propagates (V11).

Every `completed` call without an `output_json` therefore carries an `error_code` (receipts, Task 13). The
first-stage `max_tokens` code is not a terminal one: that logical call went on to its enlarged stage. A retry
whose wait would leave less than `RETRY_MIN_REMAINING_S`, or the last try, is `LLMTransient(code)`.

**What a try records** (G10): the requested and served model, the usage with both cache counts (langchain's
`input_tokens` includes them, so they are subtracted, and a missing count is 0), `stop_reason`, the refusal
category, the provider `request-id` (from the HTTP response: langchain does not expose it; `TryReceipt`) and
the `execution_config_id`. Once its settlement has committed, a completed call feeds the in-process
`CacheMonitor` (§10.8), whose alerts are then written in a transaction of their own. The per-try log line
`llm_try` names the purpose, model, effort, stage, try, outcome and ids only: never a prompt, a response or
any CV or profile text. The domain errors raised here carry codes, statuses and model ids only, and each is
raised after the handler that classified its cause has ended, so neither its `__cause__` nor its
`__context__` refers to a provider error (which holds the request) or to an encoding error (which holds the
prompt).

**The reconciler** `llm_refusal_rate` (heavy, §17.6): more than 5% refusals among the completed calls of the
last 24 h, with at least 20 of them, opens `llm.refusal_rate` (warn); a rate of 5% or less resolves it (a
window without calls is 0%), and too few calls at a rate above 5% leave it as it is.
"""

import asyncio
import contextlib
import hashlib
import json
import math
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from email.utils import parsedate_to_datetime
from functools import cached_property
from typing import Any, Final, Literal, cast

import anthropic
import httpx2
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import Runnable
from pydantic import BaseModel, Field, SecretStr, model_validator
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from jobhunter.clock import utcnow
from jobhunter.config import AppConfig
from jobhunter.db.sql import q
from jobhunter.errors import FencedOut
from jobhunter.faults import afault_point
from jobhunter.llm import spend_ledger
from jobhunter.llm.bounds import RenderedRequest, native_pdf_bound, text_input_bound
from jobhunter.llm.budget import Purpose, PurposeClass, SettleOutcome
from jobhunter.llm.cache_monitor import CacheMonitor, write_alerts
from jobhunter.llm.errors import (
    DeadlineExhausted,
    InputUnbounded,
    InvalidOutput,
    LLMNotConfigured,
    LLMRequestError,
    LLMTransient,
    MaxTokensExhausted,
    ModelMismatch,
    Refused,
)
from jobhunter.llm.fake import PURPOSE_HEADER, canonical_schema_json
from jobhunter.llm.pricing import CONTEXT_WINDOW, MODELS, SUPPORTED_EFFORTS, Effort, Model, Usage
from jobhunter.llm.spend_ledger import CallOutcome, Reservation, SettleFigures, SpendOwner
from jobhunter.llm.transport import ANTHROPIC_BASE_URL, TIMEOUT
from jobhunter.observability.alerts import open_alert, raise_alert, resolve_alert
from jobhunter.observability.logging import get_logger
from jobhunter.observability.metrics import LLM_CALLS
from jobhunter.types import JSONValue, get_int, get_object, get_str
from jobhunter.worker.epoch import fence_epoch
from jobhunter.worker.reconcilers import ReconcileContext, reconciler

StructuredChat = Runnable[LanguageModelInput, dict[str, Any]]
NodePurpose = Purpose
Stage = Literal["first", "enlarged"]

# The SDK client's OpenTelemetry option: no spans, no trace-context propagation.
_NO_TELEMETRY: Final[anthropic.OpenTelemetryOptions] = {"traces": False, "propagation": False}

FIRST_TRY_MIN_REMAINING_S: Final = 30  # a first try needs a viable window
RETRY_MIN_REMAINING_S: Final = 120  # a retry needs room for a full idle timeout
TRANSPORT_TRIES: Final = 3  # per max_tokens stage; 2 stages
RETRYABLE_STATUSES: Final = frozenset({408, 409, 429, 500, 502, 503, 504, 529})
BACKOFF_S: Final = (2.0, 8.0)  # after the first and the second try
JITTER: Final = 0.25

AUTH_FAILED_ALERT: Final = "llm.auth_failed"
MODEL_MISMATCH_ALERT: Final = "llm.model_mismatch"
UNEXPECTED_STOP_ALERT: Final = "llm.unexpected_stop_reason"  # plan Deviation 6
REFUSAL_RATE_ALERT: Final = "llm.refusal_rate"
_AUTH_SUBJECT: Final = "anthropic"

# `llm_calls.error_code` of a try (module docstring).
INVALID_OUTPUT: Final = "invalid_output"
REFUSED: Final = "refused"
MAX_TOKENS_FIRST_STAGE: Final = "max_tokens_first_stage"
MAX_TOKENS: Final = "max_tokens"
UNEXPECTED_STOP_REASON: Final = "unexpected_stop_reason"
MODEL_MISMATCH: Final = "model_mismatch"
CONNECT_FAILED: Final = "connect_failed"
STREAM_INTERRUPTED: Final = "stream_interrupted"
DEADLINE: Final = "deadline"
CANCELLED: Final = "cancelled"

# The keys of a send's payload that `messages.count_tokens` takes (the rest are `max_tokens` and `stream`).
_COUNT_KEYS: Final = frozenset({"model", "messages", "system", "output_config", "thinking"})
# What a transport error before any byte left looks like, somewhere in its cause chain.
_BEFORE_SEND: Final = (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout)
# The key the fake transport is sent instead of the secret (it ignores keys; no real key goes there).
_FAKE_API_KEY: Final = "fake-llm-mode-no-key"
_RANDOM: Final = random.SystemRandom()

log = get_logger("jobhunter.llm.gateway")


# ---- the client (§10.1, §10.2; plan Task 4) ---------------------------------------------------------------


class TryReceipt:
    """What the transport reported of one try (G10): the HTTP response (status and headers; its body is the
    SDK's), and when its headers arrived. The provider `request-id` is a response header only."""

    def __init__(self, clock: Callable[[], datetime] = utcnow) -> None:
        self._clock = clock
        self.response: httpx2.Response | None = None
        self.responded_at: datetime | None = None

    def record(self, response: httpx2.Response) -> None:
        """Keep the first response of the try and the time it arrived."""
        if self.response is None:
            self.response, self.responded_at = response, self._clock()

    @property
    def status(self) -> int | None:
        return None if self.response is None else self.response.status_code

    @property
    def request_id(self) -> str | None:
        return None if self.response is None else self.response.headers.get("request-id")

    @property
    def headers(self) -> Mapping[str, str]:
        return {} if self.response is None else self.response.headers

    async def aclose(self) -> None:
        """Release the response's connection after a failed try (a no-op once the SDK or the transport closed
        it, as both do on a timeout or a cancellation; an error event inside a 200 stream leaves it open). A
        connection that fails to close is already broken and is not reused, so that failure is dropped."""
        if self.response is not None and not self.response.is_closed:
            with contextlib.suppress(httpx2.HTTPError, OSError):
                await self.response.aclose()


def _sdk_client(
    api_key: SecretStr, http_client: httpx2.AsyncClient, timeout: anthropic.Timeout, headers: dict[str, str]
) -> anthropic.AsyncAnthropic:
    """The SDK client of every gateway request: the explicit origin and key, no SDK retry (G1, G7)."""
    client = anthropic.AsyncAnthropic(
        api_key=api_key.get_secret_value(),
        base_url=ANTHROPIC_BASE_URL,
        timeout=timeout,
        max_retries=0,
        http_client=http_client,
        default_headers=headers,
        # Any option, even this one, makes the SDK read none of its `ANTHROPIC_OPEN_TELEMETRY*` variables: no
        # span (which could carry request or response content) and no trace context on a request.
        open_telemetry=_NO_TELEMETRY,
    )
    # The SDK's constructor always merges `ANTHROPIC_CUSTOM_HEADERS` from the environment under the given
    # headers into `_custom_headers`, the one store that both of its header layers read, and after its auth
    # headers (anthropic 1.15.0 `_client.py`, `_base_client.py`; its own `lib/_scoped_client.py` edits that
    # store the same way). Such a header could add a beta (G2, G6), replace the key or forward a header meant
    # for another service, so the gateway's headers are exactly its own (G1).
    client._custom_headers = headers
    return client


class GatewayChatAnthropic(ChatAnthropic):
    """`ChatAnthropic` on an injected `httpx2` client with an `anthropic.Timeout` (G1, G3, G7).

    The SDK client it builds sends `timeout` with each request, which wins over the http client's own
    (V5), so `_chat` passes `timeout=http_client.timeout`. The base URL must be `ANTHROPIC_BASE_URL` and the
    retries 0: the gateway is the single retry layer. A `receipt` keeps the try's HTTP response (G10).
    """

    # The parent's field is `float | None` with the same alias; the SDK's timeout object replaces it. `_chat`
    # always passes the http client's; the default is the production timeout.
    default_request_timeout: anthropic.Timeout = Field(default=TIMEOUT, alias="timeout")  # type: ignore[assignment]
    http_client: httpx2.AsyncClient = Field(exclude=True, repr=False)
    receipt: TryReceipt | None = Field(default=None, exclude=True, repr=False)

    @model_validator(mode="after")
    def _gateway_client_only(self) -> "GatewayChatAnthropic":
        if self.anthropic_api_url != ANTHROPIC_BASE_URL:
            raise ValueError(f"the gateway's base URL is {ANTHROPIC_BASE_URL} (G1)")
        if self.max_retries != 0:
            raise ValueError("the gateway is the single retry layer: max_retries must be 0 (G7)")
        if not self.anthropic_api_key.get_secret_value():
            raise ValueError("the gateway needs an explicit api_key (G1)")
        return self

    @cached_property
    def _async_client(self) -> anthropic.AsyncClient:
        return _sdk_client(
            self.anthropic_api_key,
            self.http_client,
            self.default_request_timeout,
            dict(self.default_headers or {}),
        )

    @cached_property
    def _client(self) -> anthropic.Client:
        raise NotImplementedError("the gateway makes async calls only")

    async def _acreate(self, payload: dict[Any, Any]) -> Any:
        # The parent returns the SDK's raw response (`with_raw_response`); its HTTP response carries the
        # provider `request-id`, which langchain does not pass on (V13).
        raw = await super()._acreate(payload)
        if self.receipt is not None:
            self.receipt.record(raw.http_response)
        return raw


def _chat_model(
    model: Model,
    effort: Effort,
    max_tokens: int,
    *,
    api_key: SecretStr,
    http_client: httpx2.AsyncClient,
    purpose: Purpose | None = None,
    receipt: TryReceipt | None = None,
) -> GatewayChatAnthropic:
    if not api_key.get_secret_value():
        raise ValueError("an empty api_key: the gateway passes the anthropic_key secret explicitly (G1)")
    if max_tokens <= 0:
        raise ValueError("max_tokens must be positive (G4)")
    if effort not in SUPPORTED_EFFORTS[model]:
        raise ValueError(f"{model} does not support effort {effort!r} (G5)")
    return GatewayChatAnthropic(
        model=model,
        base_url=ANTHROPIC_BASE_URL,
        api_key=api_key,
        max_tokens=max_tokens,
        reasoning_effort=effort,
        streaming=True,
        max_retries=0,
        timeout=http_client.timeout,
        http_client=http_client,
        default_headers={PURPOSE_HEADER: purpose} if purpose is not None else None,
        receipt=receipt,
    )


def _chat(
    model: Model,
    effort: Effort,
    max_tokens: int,
    schema: type[BaseModel] | dict[str, Any],
    *,
    api_key: SecretStr,
    http_client: httpx2.AsyncClient,
    purpose: Purpose | None = None,
    receipt: TryReceipt | None = None,
) -> StructuredChat:
    """The §10.2 runnable of one try: `ainvoke(messages)` streams once and returns `{"raw", "parsed",
    "parsing_error"}` (`with_structured_output(…, method="json_schema", include_raw=True)`).

    The request carries `output_config.format` (`anthropic.transform_schema(schema)`) and
    `output_config.effort`, `stream: true` and exactly `max_tokens`; no tools, `tool_choice`, sampling
    parameters, betas or fallbacks (G2, G4, G6). `purpose` is sent as `X-JH-Purpose`, by which the fake
    transport chooses its recording; the API ignores the header. `receipt` keeps the HTTP response.
    """
    llm = _chat_model(
        model, effort, max_tokens, api_key=api_key, http_client=http_client, purpose=purpose, receipt=receipt
    )
    # `include_raw=True`: the output is always the `{"raw", "parsed", "parsing_error"}` dict.
    return cast(StructuredChat, llm.with_structured_output(schema, method="json_schema", include_raw=True))


def output_schema(schema: type[BaseModel]) -> dict[str, Any]:
    """The `output_config.format.schema` a request of `schema` sends (G2)."""
    return anthropic.transform_schema(schema)


# ---- nodes and prompts -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class NodeConfig:
    """One node's call: model and effort (the model policy), the two `max_tokens` stages and the deadline (the
    execution config), and its structured-output schema (§10.3)."""

    purpose: NodePurpose
    model: Model
    effort: Effort
    max_tokens: int
    max_tokens_retry: int
    deadline_s: int
    schema: type[BaseModel]


def node_config(
    purpose: NodePurpose,
    *,
    model_policy: Mapping[str, JSONValue],
    execution: Mapping[str, JSONValue],
    schema: type[BaseModel],
) -> NodeConfig:
    """The node of `purpose` from the stored payloads: `nodes.<purpose>` of the model policy (its model and
    effort; a missing or unsupported effort is a `ValueError`, G5) and `<purpose>` of the execution config."""
    node = get_object(dict(model_policy), f"nodes.{purpose}")
    model = get_str(node, "model")
    if model not in MODELS:
        raise ValueError(f"model policy node {purpose}: unknown model {model!r}")
    effort = node.get("effort")
    if effort is None:
        raise ValueError(f"model policy node {purpose} has no effort (G5)")
    if not isinstance(effort, str) or effort not in SUPPORTED_EFFORTS[model]:
        raise ValueError(f"model policy node {purpose}: {model} does not support effort {effort!r} (G5)")
    stages = get_object(dict(execution), purpose)
    return NodeConfig(
        purpose=purpose,
        model=model,
        effort=effort,
        max_tokens=get_int(stages, "max_tokens"),
        max_tokens_retry=get_int(stages, "max_tokens_retry"),
        deadline_s=get_int(stages, "deadline_s"),
        schema=schema,
    )


# The active model policy and execution config (§13.3: every non-score call uses the active ones).
_ACTIVE_LLM_CONFIG: Final = q(
    "SELECT a.execution_config_id, m.payload AS model_policy, e.payload AS execution FROM active_config a "
    "JOIN model_policies m ON m.id = a.model_policy_id "
    "JOIN execution_configs e ON e.id = a.execution_config_id WHERE a.id = 1"
)


async def active_node_config(
    engine: AsyncEngine, purpose: NodePurpose, schema: type[BaseModel]
) -> tuple[NodeConfig, int]:
    """The node of `purpose` from the active model policy and execution config, read without a lock, and the
    execution config's id (the profile graphs' LLM nodes, Tasks 13 and 20)."""
    async with engine.connect() as conn:
        row = (await conn.execute(_ACTIVE_LLM_CONFIG)).one()
    node = node_config(purpose, model_policy=row.model_policy, execution=row.execution, schema=schema)
    return node, int(row.execution_config_id)


@dataclass(frozen=True)
class SystemBlock:
    """One `system` block; `cache` puts the 5-minute cache breakpoint at its end (§10.6; never a 1 h ttl, so
    the pricing's 1.25x cache-write rate holds)."""

    text: str
    cache: bool = False


def _canonical(doc: Any) -> bytes:
    """`doc` as canonical JSON in UTF-8; `UnicodeEncodeError` for a lone surrogate."""
    return json.dumps(doc, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _message_text(message: HumanMessage) -> str:
    """The text of a user message, its byte-bounded component (§10.7)."""
    if isinstance(message.content, str):
        return message.content
    parts: list[str] = []
    for block in message.content:
        if isinstance(block, str):
            parts.append(block)
        elif block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
        else:
            raise ValueError(
                "a non-text block has no byte bound: a native PDF is counted (CallContext.native_pdf)"
            )
    return "".join(parts)


@dataclass(frozen=True)
class RenderedPrompt:
    """A rendered request: the `system` blocks, the user turns (never an assistant turn, G6), the template
    version and the ids of its inputs (`llm_calls.input_refs`, ids only)."""

    system: tuple[SystemBlock, ...]
    messages: tuple[HumanMessage, ...]
    template_version: str
    input_refs: Mapping[str, JSONValue]

    def __post_init__(self) -> None:
        if not self.messages:
            raise ValueError("a prompt needs a user message")
        if not all(isinstance(m, HumanMessage) for m in self.messages):
            raise ValueError("a prompt has user turns only: never an assistant prefill (G6)")

    def rendered_sha256(self) -> bytes:
        """The SHA-256 of the whole rendered request (its blocks, markers and messages)."""
        doc = {
            "system": [{"text": b.text, "cache": b.cache} for b in self.system],
            "messages": [m.content for m in self.messages],
        }
        return hashlib.sha256(_canonical(doc)).digest()

    def prefix_sha256(self) -> bytes | None:
        """The SHA-256 of the cached prefix, the system blocks up to the last breakpoint; `None` without one
        (the cache monitor's key, §10.8)."""
        marked = [i for i, block in enumerate(self.system) if block.cache]
        if not marked:
            return None
        return hashlib.sha256(_canonical([b.text for b in self.system[: marked[-1] + 1]])).digest()

    def as_request(self, schema: type[BaseModel]) -> RenderedRequest:
        """Every byte-bounded component: the system texts, the message texts and the sent schema."""
        return RenderedRequest(
            system=tuple(block.text for block in self.system),
            messages=tuple(_message_text(m) for m in self.messages),
            schema_json=canonical_schema_json(output_schema(schema)),
        )


def _hashes(prompt: RenderedPrompt) -> tuple[bytes, bytes | None] | None:
    """`(rendered_sha256, prefix_sha256)` of `prompt`; `None` when a component cannot be encoded as UTF-8."""
    try:
        return prompt.rendered_sha256(), prompt.prefix_sha256()
    except UnicodeEncodeError:
        return None


def to_messages(prompt: RenderedPrompt) -> list[BaseMessage]:
    """The langchain messages of `prompt`: one system message (a plain string for a single uncached block,
    else text blocks with `cache_control: {"type": "ephemeral"}` on the cached ones), then the user turns."""
    messages: list[BaseMessage] = []
    if len(prompt.system) == 1 and not prompt.system[0].cache:
        messages.append(SystemMessage(prompt.system[0].text))
    elif prompt.system:
        blocks: list[str | dict[str, Any]] = [
            {"type": "text", "text": b.text, **({"cache_control": {"type": "ephemeral"}} if b.cache else {})}
            for b in prompt.system
        ]
        messages.append(SystemMessage(blocks))
    messages.extend(prompt.messages)
    return messages


@dataclass
class CallContext:
    """Who a call is for: the spend owner and class, the pinned execution config, the deadline (monotonic,
    the node's active `deadline_s` from now, enforced per call: Deviation 28), the draft or eval run it
    records, its `logical_call_no` (2 for a node's one repair) and whether its input is a native PDF."""

    owner: SpendOwner
    purpose_class: PurposeClass
    execution_config_id: int
    deadline: float
    profile_draft_id: int | None = None
    eval_run_id: int | None = None
    logical_call_no: int = 1
    native_pdf: bool = False


@dataclass(frozen=True)
class GatewayResult:
    """A successful logical call: the parsed output, its call and usage, the served model, its stop reason,
    and what the logical call counts at (each billed try at its cost, each ambiguous one at its
    reservation)."""

    parsed: BaseModel
    llm_call_id: int
    usage: Usage
    model_served: str
    stop_reason: str
    cost_usd: Decimal


# ---- retry policy (§10.4) ----------------------------------------------------------------------------------


def is_retryable_status(status: int) -> bool:
    """408, 409, 429, 500, 502, 503, 504 and 529; every other status is a non-retryable request error."""
    return status in RETRYABLE_STATUSES


def jittered_backoff(transport_try: int, *, rng: random.Random = _RANDOM) -> float:
    """The wait after try `transport_try` without a `Retry-After`: 2 s, then 8 s, each ± 25%."""
    if transport_try < 1:
        raise ValueError("transport tries count from 1")
    base = BACKOFF_S[min(transport_try, len(BACKOFF_S)) - 1]
    return base * rng.uniform(1 - JITTER, 1 + JITTER)


def _positive(seconds: float) -> float | None:
    return seconds if math.isfinite(seconds) and seconds > 0 else None


def retry_after_seconds(headers: Mapping[str, str], *, now: datetime) -> float | None:
    """The wait an error response asks for: `retry-after-ms`, or `Retry-After` in seconds or as an HTTP date
    (against `now`); `None` without a positive one."""
    if ms := (headers.get("retry-after-ms") or "").strip():
        try:
            return _positive(float(ms) / 1000)
        except ValueError:
            pass
    value = (headers.get("retry-after") or "").strip()
    if not value:
        return None
    try:
        return _positive(float(value))
    except ValueError:
        pass
    try:
        return _positive((parsedate_to_datetime(value) - now).total_seconds())
    except (TypeError, ValueError):
        return None


def retry_wait(
    headers: Mapping[str, str], transport_try: int, *, now: datetime, rng: random.Random = _RANDOM
) -> float:
    """`retry_after_seconds(e) or jittered_backoff(transport_try)` (§10.2): `Retry-After` wins."""
    return retry_after_seconds(headers, now=now) or jittered_backoff(transport_try, rng=rng)


def min_remaining_s(stage: Stage, transport_try: int) -> int:
    """The deadline a try needs left: 30 s for the first try, 120 s for any later one (a retry or the enlarged
    stage)."""
    return FIRST_TRY_MIN_REMAINING_S if (stage, transport_try) == ("first", 1) else RETRY_MIN_REMAINING_S


def _connect_failed_before_send(exc: BaseException) -> bool:
    """Whether a transport error happened before any byte of the request left: a connect or pool failure
    anywhere in its cause chain."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, _BEFORE_SEND):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


def usage_of(usage_metadata: Mapping[str, Any]) -> Usage:
    """The pricing `Usage` of langchain's `usage_metadata` (V13): its `input_tokens` includes both cache
    counts, which are subtracted; a missing or null count is 0."""
    details = usage_metadata.get("input_token_details") or {}
    cache_read = int(details.get("cache_read") or 0)
    cache_creation = int(details.get("cache_creation") or 0)
    uncached = int(usage_metadata.get("input_tokens") or 0) - cache_read - cache_creation
    output = int(usage_metadata.get("output_tokens") or 0)
    if min(uncached, cache_read, cache_creation, output) < 0:
        raise ValueError("usage_metadata has negative token counts")
    return Usage(uncached, output, cache_creation, cache_read)


# ---- the call loop (§10.2) ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Try:
    node: NodeConfig
    ctx: CallContext
    res: Reservation
    stage: Stage
    transport_try: int
    max_tokens: int
    started_at: datetime


@dataclass(frozen=True)
class _Next:
    """What follows a settled try: the result, the enlarged stage, or a retry after `wait`; `counted` is what
    the try counts at."""

    kind: Literal["return", "enlarge", "retry"]
    counted: Decimal
    wait: float = 0.0
    result: GatewayResult | None = None


def _api_key(cfg: AppConfig) -> SecretStr | None:
    """The key every request sends (G1): the worker's `anthropic_key` secret, or in fake LLM mode a
    placeholder; `None` without either (the gateway is not configured, module docstring)."""
    if cfg.fake_llm:
        return SecretStr(_FAKE_API_KEY)  # the fake ignores the key; a real one is never sent there
    key = cfg.secrets.get("anthropic_key", b"").decode("utf-8").strip()
    return SecretStr(key) if key else None


class LLMGateway:
    """The §10.2 loop over one `httpx2` client (module docstring). `clock` stamps the rows, `monotonic` runs
    the deadline (the loop's own clock in production) and `sleep` waits out the backoff."""

    def __init__(
        self,
        engine: AsyncEngine,
        cfg: AppConfig,
        *,
        http_client: httpx2.AsyncClient,
        clock: Callable[[], datetime] = utcnow,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
        cache: CacheMonitor,
    ) -> None:
        self._engine = engine
        self._http = http_client
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        self._cache = cache
        self._key = _api_key(cfg)

    @property
    def configured(self) -> bool:
        """Whether this gateway can make calls: it has the key, or it is on the fake LLM (module
        docstring)."""
        return self._key is not None

    def _configured_key(self) -> SecretStr:
        """The key of a configured gateway; `LLMNotConfigured` otherwise, before anything is reserved or
        sent."""
        if self._key is None:
            raise LLMNotConfigured()
        return self._key

    @property
    def http_client(self) -> httpx2.AsyncClient:
        """The `httpx2` client every try and every token count is sent on (the schema dry-run reads what it
        sent there, through a request hook)."""
        return self._http

    @property
    def monotonic(self) -> Callable[[], float]:
        """The clock the loop compares `CallContext.deadline` with: a caller builds its deadline from it."""
        return self._monotonic

    async def call(self, node: NodeConfig, prompt: RenderedPrompt, ctx: CallContext) -> GatewayResult:
        """One logical call: ≤ 3 transport tries per `max_tokens` stage, ≤ 2 stages (≤ 6 HTTP tries); a
        gateway without a key raises `LLMNotConfigured` first."""
        api_key = self._configured_key()
        messages = to_messages(prompt)  # the last is a user turn: never a prefill (RenderedPrompt)
        hashes = _hashes(prompt)
        if hashes is None:  # raised outside the handler: the encoding error holds the whole prompt
            raise InputUnbounded("unencodable")
        rendered, prefix = hashes
        bound = await self._input_bound(node, prompt, ctx)
        counted = Decimal(0)
        stages: tuple[tuple[Stage, int], ...] = (
            ("first", node.max_tokens),
            ("enlarged", node.max_tokens_retry),
        )
        for stage, max_tokens in stages:
            for transport_try in range(1, TRANSPORT_TRIES + 1):
                if ctx.deadline - self._monotonic() < min_remaining_s(stage, transport_try):
                    raise DeadlineExhausted("too little of the node deadline is left for another try")
                # built before the reservation: nothing is reserved for a request that cannot be made
                receipt = TryReceipt(self._clock)
                runnable = _chat(
                    node.model,
                    node.effort,
                    max_tokens,
                    node.schema,
                    api_key=api_key,
                    http_client=self._http,
                    purpose=node.purpose,
                    receipt=receipt,
                )
                res = await self._reserve(
                    node, prompt, ctx, stage, transport_try, max_tokens, bound, rendered, prefix
                )
                await afault_point("gateway.after_reserve")
                await self._dispatch(node, ctx, res, stage, transport_try)
                await afault_point("gateway.after_dispatch")
                t = _Try(node, ctx, res, stage, transport_try, max_tokens, self._clock())
                step = await self._send(t, runnable, receipt, messages, prefix, counted)
                counted += step.counted
                if step.result is not None:
                    return step.result
                if step.kind == "enlarge":
                    break
                await self._sleep(step.wait)
        raise MaxTokensExhausted("the enlarged max_tokens stage stopped at max_tokens too")

    def count_body(self, node: NodeConfig, prompt: RenderedPrompt) -> dict[str, Any]:
        """The `messages.count_tokens` body of the exact request a try of `node` sends: langchain's payload of
        the send without `max_tokens` (and `stream`, which it adds when it streams)."""
        api_key = self._configured_key()
        llm = _chat_model(node.model, node.effort, node.max_tokens, api_key=api_key, http_client=self._http)
        output_config = {"format": {"type": "json_schema", "schema": output_schema(node.schema)}}
        payload: dict[str, Any] = llm._get_request_payload(to_messages(prompt), output_config=output_config)
        if extra := set(payload) - _COUNT_KEYS - {"max_tokens"}:
            raise ValueError(f"the request has keys count_tokens does not take: {sorted(extra)}")
        return {key: value for key, value in payload.items() if key in _COUNT_KEYS}

    async def count_tokens(self, model: Model, body: Mapping[str, Any]) -> int:
        """`POST /v1/messages/count_tokens` for `body` on `model` (free; native PDF and V19). The SDK's
        `APIError` propagates; a gateway without a key raises `LLMNotConfigured` and sends nothing."""
        client = _sdk_client(self._configured_key(), self._http, self._http.timeout, {})
        counted = await client.messages.count_tokens(**{**body, "model": model})
        return int(counted.input_tokens)

    async def _input_bound(self, node: NodeConfig, prompt: RenderedPrompt, ctx: CallContext) -> int:
        """The reservation's input bound (§10.7), before anything is reserved; `InputUnbounded` otherwise.

        Each `InputUnbounded` is raised after the handler that classified its cause has ended, so neither its
        `__cause__` nor its `__context__` refers to that error: an encoding error holds the prompt, and the
        SDK's error holds its request, the count body with the native PDF."""
        if not ctx.native_pdf:
            try:
                return text_input_bound(node.model, prompt.as_request(node.schema))
            except UnicodeEncodeError:
                pass
            raise InputUnbounded("unencodable")
        count: int | None = None
        failure = ""
        try:
            count = await self.count_tokens(node.model, self.count_body(node, prompt))
        except anthropic.APIError as exc:
            failure = type(exc).__name__
        if count is None:
            log.warning("llm_count_failed", purpose=node.purpose, model=node.model, error_class=failure)
            raise InputUnbounded("count_failed")
        if count > CONTEXT_WINDOW[node.model]:
            log.warning(
                "llm_input_over_context_window", purpose=node.purpose, model=node.model, counted=count
            )
            raise InputUnbounded("context_window")
        return native_pdf_bound(node.model, count)

    async def _reserve(
        self,
        node: NodeConfig,
        prompt: RenderedPrompt,
        ctx: CallContext,
        stage: Stage,
        transport_try: int,
        max_tokens: int,
        bound: int,
        rendered: bytes,
        prefix: bytes | None,
    ) -> Reservation:
        """Step 1 in its own transaction."""
        async with self._engine.begin() as conn:
            return await spend_ledger.reserve(
                conn,
                owner=ctx.owner,
                purpose=node.purpose,
                purpose_class=ctx.purpose_class,
                model=node.model,
                effort=node.effort,
                max_tokens=max_tokens,
                max_tokens_stage=stage,
                transport_try=transport_try,
                logical_call_no=ctx.logical_call_no,
                deadline_s=node.deadline_s,
                input_tokens_bound=bound,
                native_pdf_extra=ctx.native_pdf,
                execution_config_id=ctx.execution_config_id,
                prompt_template_version=prompt.template_version,
                rendered_prompt_sha256=rendered,
                prefix_sha256=prefix,
                input_refs=prompt.input_refs,
                profile_draft_id=ctx.profile_draft_id,
                eval_run_id=ctx.eval_run_id,
                transport="stream",
                now=self._clock(),
            )

    async def _dispatch(
        self, node: NodeConfig, ctx: CallContext, res: Reservation, stage: Stage, transport_try: int
    ) -> None:
        """Step 2 in its own transaction. A fenced marker releases the try in another and re-raises."""
        try:
            async with self._engine.begin() as conn:
                await spend_ledger.mark_dispatched(conn, res, ctx.owner, now=self._clock())
        except FencedOut:
            async with self._engine.begin() as conn:
                await spend_ledger.release_never_dispatched(conn, res, now=self._clock())
            log.info(
                "llm_try_fenced",
                purpose=node.purpose,
                model=node.model,
                effort=node.effort,
                stage=stage,
                transport_try=transport_try,
                logical_call_no=ctx.logical_call_no,
                llm_call_id=res.llm_call_id,
                ledger_id=res.ledger_id,
            )
            raise

    async def _send(
        self,
        t: _Try,
        runnable: StructuredChat,
        receipt: TryReceipt,
        messages: list[BaseMessage],
        prefix: bytes | None,
        counted: Decimal,
    ) -> _Next:
        """Steps 3 and 4: stream once within the deadline, then settle by how it ended (module docstring).

        The handlers only classify; what follows is raised outside them, so no provider error (which holds the
        request) becomes the implicit context of a domain error. A cancellation is settled in its handler and
        re-raised there.
        """
        status = 0
        try:
            async with asyncio.timeout(t.ctx.deadline - self._monotonic()) as scope:
                out = await runnable.ainvoke(messages)
        except asyncio.CancelledError:
            await self._settle_cancelled(t, receipt)
            raise
        except TimeoutError:
            if not scope.expired():
                raise
            ending = DEADLINE
        except anthropic.APIStatusError as exc:
            receipt.record(exc.response)
            status = exc.status_code
            # an error event inside a streamed 200 is a lost stream, not an error response
            ending = "error_response" if status >= 400 else STREAM_INTERRUPTED
        except anthropic.APIConnectionError as exc:  # its `APITimeoutError` too
            ending = CONNECT_FAILED if _connect_failed_before_send(exc) else STREAM_INTERRUPTED
        except (httpx2.TransportError, anthropic.APIError):
            # a transport error inside the stream reaches here unwrapped (V5: a stalled stream's ReadTimeout)
            ending = STREAM_INTERRUPTED
        else:
            return await self._response(t, receipt, out, prefix, counted)
        if ending == DEADLINE:
            await self._settle_lost(t, receipt, DEADLINE)
            raise DeadlineExhausted("the node deadline passed during the try")
        if ending == "error_response":
            return await self._error_response(t, receipt, status)
        if ending == CONNECT_FAILED:
            return await self._connect_failed(t)
        return await self._lost(t, receipt)

    async def _settle_cancelled(self, t: _Try, receipt: TryReceipt) -> None:
        """Settle a cancelled try `ambiguous` in the cancelled task. A database failure here must not replace
        the cancellation: it is logged (its class only) and `llm_ledger_unsettled` settles the entry later."""
        try:
            await self._settle_lost(t, receipt, CANCELLED)
        except SQLAlchemyError as exc:
            log.warning(
                "llm_cancelled_try_unsettled",
                llm_call_id=t.res.llm_call_id,
                ledger_id=t.res.ledger_id,
                error_class=type(exc).__name__,
            )

    async def _response(
        self, t: _Try, receipt: TryReceipt, out: Mapping[str, Any], prefix: bytes | None, counted: Decimal
    ) -> _Next:
        """A response that streamed to its end: billed from its usage, then interpreted (§10.5)."""
        raw = out.get("raw")
        meta: Mapping[str, Any] = raw.response_metadata if isinstance(raw, AIMessage) else {}
        stop_reason, served = meta.get("stop_reason"), meta.get("model_name")
        if not isinstance(raw, AIMessage) or raw.usage_metadata is None or not isinstance(stop_reason, str):
            return await self._lost(t, receipt)  # it ended without its final event: no usage to bill
        if not isinstance(served, str):
            return await self._lost(t, receipt)
        usage = usage_of(cast(Mapping[str, Any], raw.usage_metadata))
        parsed = out.get("parsed")
        details = meta.get("stop_details")  # present only when not null (V13)
        category = details.get("category") if isinstance(details, dict) else None
        if not isinstance(category, str) or not category:
            category = "unspecified"
        code: str | None
        if served != t.node.model:
            code = MODEL_MISMATCH
        elif stop_reason == "max_tokens":
            code = MAX_TOKENS_FIRST_STAGE if t.stage == "first" else MAX_TOKENS
        elif stop_reason == "refusal":
            code = REFUSED
        elif stop_reason == "end_turn":
            ok = isinstance(parsed, t.node.schema) and out.get("parsing_error") is None
            code = None if ok else INVALID_OUTPUT
        else:
            code = UNEXPECTED_STOP_REASON
        call = CallOutcome(
            status="completed",
            model_served=served,
            http_status=receipt.status,
            error_code=code,
            stop_reason=stop_reason,
            refusal_category=category if code == REFUSED else None,
            provider_request_id=receipt.request_id,
            output_json=cast(BaseModel, parsed).model_dump(mode="json") if code is None else None,
            first_event_at=receipt.responded_at,
        )
        async with self._engine.begin() as conn:
            figures = await spend_ledger.settle(
                conn, t.res, outcome="billed", evidence=None, usage=usage, call=call, now=self._clock()
            )
            await resolve_alert(conn, code=AUTH_FAILED_ALERT, subject=_AUTH_SUBJECT)  # the key works again
            if code == MODEL_MISMATCH:
                await raise_alert(
                    conn,
                    code=MODEL_MISMATCH_ALERT,
                    severity="critical",
                    subject=f"model:{t.node.model}",
                    message="the provider served another model than requested; its output is not used",
                    context={"purpose": t.node.purpose, "requested": t.node.model, "served": served},
                )
            elif code == UNEXPECTED_STOP_REASON:
                await raise_alert(
                    conn,
                    code=UNEXPECTED_STOP_ALERT,
                    severity="warn",
                    subject=f"purpose:{t.node.purpose}",
                    message="an LLM call stopped for an unexpected reason",
                    context={"purpose": t.node.purpose, "model": t.node.model, "stop_reason": stop_reason},
                )
        self._committed(t, receipt, "billed", figures, code)
        await self._observe_cache(t, served, prefix, usage)
        match code:
            case None:
                result = GatewayResult(
                    parsed=cast(BaseModel, parsed),
                    llm_call_id=t.res.llm_call_id,
                    usage=usage,
                    model_served=served,
                    stop_reason=stop_reason,
                    cost_usd=counted + figures.settled_usd,
                )
                return _Next("return", figures.settled_usd, result=result)
            case "max_tokens_first_stage":
                return _Next("enlarge", figures.settled_usd)
            case "max_tokens":
                raise MaxTokensExhausted("the enlarged max_tokens stage stopped at max_tokens too")
            case "refused":
                raise Refused(category)
            case "model_mismatch":
                raise ModelMismatch(t.node.model, served)
            case "invalid_output":
                raise InvalidOutput("structural")
            case _:
                raise InvalidOutput("unexpected_stop_reason")

    async def _observe_cache(self, t: _Try, served: str, prefix: bytes | None, usage: Usage) -> None:
        """After the settlement committed (plan F32): the cache monitor's ratio gauge and streak, then the
        alerts that queued, in a transaction of their own. A failed alert write never fails the billed call:
        it is logged (its class only) and the alerts go back to the monitor, written after the next call."""
        self._cache.observe(
            served,
            t.node.effort,
            t.node.purpose,
            prefix,
            t.started_at,
            usage.cache_read_input_tokens,
            input_tokens=usage.input_tokens,
            cache_creation_tokens=usage.cache_creation_input_tokens,
        )
        alerts = self._cache.drain()
        if not alerts:
            return
        try:
            async with self._engine.begin() as conn:
                await write_alerts(conn, alerts)
        except SQLAlchemyError as exc:
            self._cache.requeue(alerts)
            log.warning("llm_cache_alerts_unwritten", alerts=len(alerts), error_class=type(exc).__name__)

    async def _error_response(self, t: _Try, receipt: TryReceipt, status: int) -> _Next:
        """An error response without content: not billed; retried for a retryable status (§10.4)."""
        code = f"http_{status}"
        call = CallOutcome(
            status="error", http_status=status, error_code=code, provider_request_id=receipt.request_id
        )
        async with self._engine.begin() as conn:
            figures = await spend_ledger.settle(
                conn,
                t.res,
                outcome="not_billed",
                evidence="error_response_no_content",
                usage=None,
                call=call,
                now=self._clock(),
            )
            if status in (401, 403):
                await raise_alert(
                    conn,
                    code=AUTH_FAILED_ALERT,
                    severity="critical",
                    subject=_AUTH_SUBJECT,
                    message="the Anthropic API refused the key; LLM calls fail until it is fixed",
                    context={"http_status": status},
                )
        self._committed(t, receipt, "not_billed", figures, code)
        if not is_retryable_status(status):
            raise LLMRequestError(status)
        wait = retry_wait(receipt.headers, t.transport_try, now=self._clock())
        if (
            t.transport_try == TRANSPORT_TRIES
            or self._monotonic() + wait > t.ctx.deadline - RETRY_MIN_REMAINING_S
        ):
            raise LLMTransient(code)
        return _Next("retry", figures.settled_usd, wait=wait)

    async def _connect_failed(self, t: _Try) -> _Next:
        """No byte left: not billed (`connect_failed_before_send`), retried after the backoff."""
        call = CallOutcome(status="error", error_code=CONNECT_FAILED)
        async with self._engine.begin() as conn:
            figures = await spend_ledger.settle(
                conn,
                t.res,
                outcome="not_billed",
                evidence="connect_failed_before_send",
                usage=None,
                call=call,
                now=self._clock(),
            )
        self._committed(t, None, "not_billed", figures, CONNECT_FAILED)
        if t.transport_try == TRANSPORT_TRIES:
            raise LLMTransient(CONNECT_FAILED)
        return _Next("retry", figures.settled_usd, wait=jittered_backoff(t.transport_try))

    async def _lost(self, t: _Try, receipt: TryReceipt) -> _Next:
        """A lost stream: `ambiguous` (it keeps its reservation), retried with a new reservation (C15)."""
        figures = await self._settle_lost(t, receipt, STREAM_INTERRUPTED)
        if t.transport_try == TRANSPORT_TRIES:
            raise LLMTransient(STREAM_INTERRUPTED)
        return _Next("retry", figures.settled_usd, wait=jittered_backoff(t.transport_try))

    async def _settle_lost(self, t: _Try, receipt: TryReceipt, code: str) -> SettleFigures:
        """Settle a try of unknown outcome `ambiguous`, in its own transaction (also while cancelled), then
        release its response's connection."""
        call = CallOutcome(
            status="ambiguous",
            http_status=receipt.status,
            error_code=code,
            provider_request_id=receipt.request_id,
            first_event_at=receipt.responded_at,
        )
        async with self._engine.begin() as conn:
            figures = await spend_ledger.settle(
                conn, t.res, outcome="ambiguous", evidence=None, usage=None, call=call, now=self._clock()
            )
        self._committed(t, receipt, "ambiguous", figures, code)
        await receipt.aclose()
        return figures

    def _committed(
        self,
        t: _Try,
        receipt: TryReceipt | None,
        outcome: SettleOutcome,
        figures: SettleFigures,
        code: str | None,
    ) -> None:
        """After a settlement committed: the spend metrics, the per-try counter and the per-try log line."""
        spend_ledger.record_settlement_metrics(figures)
        label = code or "ok"
        LLM_CALLS.labels(
            model=t.node.model, effort=t.node.effort, purpose=t.node.purpose, outcome=label
        ).inc()
        log.info(
            "llm_try",
            purpose=t.node.purpose,
            model=t.node.model,
            effort=t.node.effort,
            stage=t.stage,
            transport_try=t.transport_try,
            logical_call_no=t.ctx.logical_call_no,
            settlement=outcome,
            outcome=label,
            http_status=None if receipt is None else receipt.status,
            provider_request_id=None if receipt is None else receipt.request_id,
            llm_call_id=t.res.llm_call_id,
            ledger_id=t.res.ledger_id,
        )


# ---- the refusal-rate reconciler (§17.6) -------------------------------------------------------------------

REFUSAL_WINDOW: Final = timedelta(hours=24)
REFUSAL_MIN_CALLS: Final = 20
REFUSAL_MAX_PCT: Final = 5  # the alert needs strictly more
_REFUSAL_SUBJECT: Final = "llm"
# `llm_calls_recent` serves the window.
_REFUSALS: Final = q(
    "SELECT count(*) AS calls, count(*) FILTER (WHERE stop_reason = 'refusal') AS refusals "
    "FROM llm_calls WHERE status = 'completed' AND started_at >= :since"
)


@reconciler("llm_refusal_rate", "heavy")
async def reconcile_refusal_rate(ctx: ReconcileContext) -> int:
    """§17.6 `llm.refusal_rate` over the completed calls started in the 24 h before `ctx.now`: opened (warn,
    once) while more than 5% of them were refused and there were at least 20; resolved once the rate is 5% or
    less ("rate below threshold"), a window without calls counting as 0%. With fewer than 20 calls and a
    rate above 5% there is too little to raise on and nothing to resolve, so the alert is left as it is.
    Returns 1 when it opened or resolved the alert, else 0. Fenced by the worker epoch first: the worker's
    own `FencedOut` propagates (Ruling FR-w), another owner's ends the pass with 0."""
    try:
        async with ctx.engine.begin() as conn:
            await fence_epoch(conn, ctx.epoch)
            row = (await conn.execute(_REFUSALS, {"since": ctx.now - REFUSAL_WINDOW})).one()
            calls, refusals = int(row.calls), int(row.refusals)
            above = refusals * 100 > REFUSAL_MAX_PCT * calls  # exact: no float; 0 of 0 is not above
            if above and calls >= REFUSAL_MIN_CALLS:
                opened = await open_alert(
                    conn,
                    code=REFUSAL_RATE_ALERT,
                    severity="warn",
                    subject=_REFUSAL_SUBJECT,
                    message="more than 5% of the LLM calls of the last 24 hours were refused",
                    context={"calls": calls, "refusals": refusals},
                )
                if opened is not None:
                    log.warning("llm_refusal_rate_high", calls=calls, refusals=refusals)
                return 0 if opened is None else 1
            if above:
                return 0
            return int(await resolve_alert(conn, code=REFUSAL_RATE_ALERT, subject=_REFUSAL_SUBJECT))
    except FencedOut as fenced_out:
        if fenced_out.worker_superseded:
            raise  # `fence_epoch` logged it
        return 0
