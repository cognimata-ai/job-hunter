"""The LLM gateway (spec §10): the only module that constructs a Claude client (§10.1; ruff `TID251`).

This part (plan Task 4) is the client factory of §10.2, `_chat`: one structured, streamed chat runnable per
HTTP try. Task 5 adds the call loop above it.

`GatewayChatAnthropic` is the G3 fallback, which spike V5 shows is needed (`docs/spikes/V5.md`): langchain's
`ChatAnthropic` types `timeout` as a float, so it refuses `anthropic.Timeout`, and it has no `http_client`
field (one passed is moved into `model_kwargs`, the request body), building its own cached client from
`base_url` and the environment instead. The subclass takes both and overrides the async client factory to
build `anthropic.AsyncAnthropic(base_url=…, api_key=…, timeout=…, max_retries=0, http_client=…)`. There is no
sync client: every gateway call is async.
"""

from functools import cached_property
from typing import Any, Final, cast

import anthropic
import httpx2
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import LanguageModelInput
from langchain_core.runnables import Runnable
from pydantic import BaseModel, Field, SecretStr, model_validator

from jobhunter.llm.budget import Purpose
from jobhunter.llm.fake import PURPOSE_HEADER
from jobhunter.llm.pricing import SUPPORTED_EFFORTS, Effort, Model
from jobhunter.llm.transport import ANTHROPIC_BASE_URL, TIMEOUT

StructuredChat = Runnable[LanguageModelInput, dict[str, Any]]

# The SDK client's OpenTelemetry option: no spans, no trace-context propagation.
_NO_TELEMETRY: Final[anthropic.OpenTelemetryOptions] = {"traces": False, "propagation": False}


class GatewayChatAnthropic(ChatAnthropic):
    """`ChatAnthropic` on an injected `httpx2` client with an `anthropic.Timeout` (G1, G3, G7).

    The SDK client it builds sends `timeout` with each request, which wins over the http client's own
    (V5), so `_chat` passes `timeout=http_client.timeout`. The base URL must be `ANTHROPIC_BASE_URL` and the
    retries 0: the gateway is the single retry layer.
    """

    # The parent's field is `float | None` with the same alias; the SDK's timeout object replaces it. `_chat`
    # always passes the http client's; the default is the production timeout.
    default_request_timeout: anthropic.Timeout = Field(default=TIMEOUT, alias="timeout")  # type: ignore[assignment]
    http_client: httpx2.AsyncClient = Field(exclude=True, repr=False)

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
        headers = dict(self.default_headers or {})
        client = anthropic.AsyncAnthropic(
            api_key=self.anthropic_api_key.get_secret_value(),
            base_url=ANTHROPIC_BASE_URL,
            timeout=self.default_request_timeout,
            max_retries=0,
            http_client=self.http_client,
            default_headers=headers,
            # Any option, even this one, makes the SDK read none of its `ANTHROPIC_OPEN_TELEMETRY*` variables:
            # no span (which could carry request or response content) and no trace context on a request.
            open_telemetry=_NO_TELEMETRY,
        )
        # The SDK's constructor always merges `ANTHROPIC_CUSTOM_HEADERS` from the environment under the given
        # headers into `_custom_headers`, the one store that both of its header layers read, and after its
        # auth headers (anthropic 1.15.0 `_client.py`, `_base_client.py`; its own `lib/_scoped_client.py`
        # edits that store the same way). Such a header could add a beta (G2, G6), replace the key or forward
        # a header meant for another service, so the gateway's headers are exactly its own (G1).
        client._custom_headers = headers
        return client

    @cached_property
    def _client(self) -> anthropic.Client:
        raise NotImplementedError("the gateway makes async calls only")


def _chat(
    model: Model,
    effort: Effort,
    max_tokens: int,
    schema: type[BaseModel] | dict[str, Any],
    *,
    api_key: SecretStr,
    http_client: httpx2.AsyncClient,
    purpose: Purpose | None = None,
) -> StructuredChat:
    """The §10.2 runnable of one try: `ainvoke(messages)` streams once and returns `{"raw", "parsed",
    "parsing_error"}` (`with_structured_output(…, method="json_schema", include_raw=True)`).

    The request carries `output_config.format` (`anthropic.transform_schema(schema)`) and
    `output_config.effort`, `stream: true` and exactly `max_tokens`; no tools, `tool_choice`, sampling
    parameters, betas or fallbacks (G2, G4, G6). `purpose` is sent as `X-JH-Purpose`, by which the fake
    transport chooses its recording; the API ignores the header.
    """
    if not api_key.get_secret_value():
        raise ValueError("an empty api_key: the gateway passes the anthropic_key secret explicitly (G1)")
    if max_tokens <= 0:
        raise ValueError("max_tokens must be positive (G4)")
    if effort not in SUPPORTED_EFFORTS[model]:
        raise ValueError(f"{model} does not support effort {effort!r} (G5)")
    llm = GatewayChatAnthropic(
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
    )
    # `include_raw=True`: the output is always the `{"raw", "parsed", "parsing_error"}` dict.
    return cast(StructuredChat, llm.with_structured_output(schema, method="json_schema", include_raw=True))
