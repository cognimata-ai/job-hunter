"""The HTTP side of every Claude call (spec §10.1 G1, G3; plan Task 4): the API origin, the one timeout, and
the `httpx2` client the SDK sends through.

- `ANTHROPIC_BASE_URL` is the only origin the gateway names; `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_URL` and
  `ANTHROPIC_API_KEY` in the environment are never read (G1).
- `TIMEOUT` is `anthropic.Timeout(connect=10, read=120, write=30, pool=10)` (G3). Its read value is the idle
  time between SSE events, which carry pings. `make_http_client` builds the client's timeout from it with only
  the read value overridable, and `gateway._chat` passes `timeout=http_client.timeout` to the SDK client,
  because the SDK sends its own `timeout` with each request and that wins over the http client's (spike V5,
  `docs/spikes/V5.md`). So the client object carries the one timeout of every request.
- In fake LLM mode (`AppConfig.fake_llm`) the client sends every request for the API origin to
  `fake.RecordedSSETransport(cfg.fake_llm_dir)`, and nothing else is reachable.

`httpx2` is the SDK's transport (`anthropic` 1.x). It is not `httpx`, the adapters' transport, and an object
of one is never passed to the other.
"""

from typing import Final

import anthropic
import httpx2

from jobhunter.config import AppConfig
from jobhunter.llm import fake

ANTHROPIC_BASE_URL: Final = "https://api.anthropic.com"

_CONNECT_S: Final = 10.0
_READ_S: Final = 120.0
_WRITE_S: Final = 30.0
_POOL_S: Final = 10.0
TIMEOUT: Final = anthropic.Timeout(connect=_CONNECT_S, read=_READ_S, write=_WRITE_S, pool=_POOL_S)


def http_timeout(read_timeout_s: float = _READ_S) -> anthropic.Timeout:
    """`TIMEOUT` with its read value replaced by `read_timeout_s`."""
    return anthropic.Timeout(connect=_CONNECT_S, read=read_timeout_s, write=_WRITE_S, pool=_POOL_S)


class _Unreachable(httpx2.AsyncBaseTransport):
    """The transport of every origin but the API's when the API's is replaced: nothing leaves the process."""

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError(
            f"fake LLM mode or an injected transport: {request.url.host} is unreachable",
            request=request,
        )


def make_http_client(
    cfg: AppConfig,
    *,
    read_timeout_s: float = _READ_S,
    mount: httpx2.AsyncBaseTransport | None = None,
) -> httpx2.AsyncClient:
    """The `httpx2` client of the gateway; its owner closes it (`aclose`).

    `read_timeout_s` (tests only) replaces the 120 s idle read timeout. `mount` (tests and the spike recorder
    only) is the transport of `https://api.anthropic.com` in place of the network or, in fake mode, of the
    fake. Whenever the API origin has such a transport, the environment's proxy settings are ignored (an
    `httpx2` proxy mount from `NO_PROXY` would otherwise outrank it) and every other origin is unreachable,
    so a fake or test client can never reach the network.
    """
    if cfg.env == "prod" and (read_timeout_s != _READ_S or mount is not None):
        raise ValueError("an injected read timeout or transport is for tests only")
    timeout = http_timeout(read_timeout_s)
    if mount is None and cfg.fake_llm:
        mount = fake.RecordedSSETransport(cfg.fake_llm_dir)
    if mount is None:
        return anthropic.DefaultAsyncHttpxClient(timeout=timeout)
    return anthropic.DefaultAsyncHttpxClient(
        timeout=timeout,
        trust_env=False,
        transport=_Unreachable(),
        mounts={ANTHROPIC_BASE_URL: mount},
    )
