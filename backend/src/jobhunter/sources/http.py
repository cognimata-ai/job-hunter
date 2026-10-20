"""The HTTP layer of the source adapters (spec §5.2; plan Deviations 19 and 25).

`make_client` builds the `httpx.AsyncClient` of a source: the §5.2 timeouts, the configured User-Agent,
redirects off, and in fake mode the fixture transport (`configure`, once per process). `send` makes exactly
one attempt and returns its `HttpOutcome`, the page and the error together, which the adapter's `settle`
classifies:

- a `ConnectError` is `SourceError("transient", before_send=True)`: no request byte was written (§6.1
  `connect_failed_before_send`); on a redirect hop of the attempt it is not, as the first request was sent;
- a timeout, the total deadline, or a response lost after the send is `SourceError("ambiguous")`;
- a body over the cap is read no further and is `ContractViolation("body_too_large")` with the first
  `ERROR_BODY_BYTES` kept as the page (Deviation 19), and a body that does not decode (`content-encoding`) is
  `ContractViolation("undecodable_body")` with what was read; nothing else of the body is judged here, so an
  HTML page where JSON is expected is a page whose `json()` raises `ContractViolation("not_json")`;
- any status is a page, a 3xx included, except that a redirect within the Personio host family
  (`*.jobs.personio.de`, https) is followed, at most `MAX_PERSONIO_REDIRECTS` times.

The page keeps only the `STORED_HEADERS`, with lower-case names. Each attempt logs one `source_request` line
with the source, the host, the path and the query without its credential parameters; the full URL is never
logged.

`missing_credential` is the paid adapters' local refusal (final review C2, defence in depth): a request whose
credential is missing or empty is never built and never sent, whatever the instance's health says.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

import httpx

from jobhunter.clock import utcnow
from jobhunter.config import DEFAULT_HTTP_USER_AGENT, AppConfig
from jobhunter.db.seed import SOURCES
from jobhunter.observability.logging import get_logger
from jobhunter.sources.base import (
    MISSING_SECRET,
    ContractViolation,
    RawPage,
    RequestSpec,
    SourceError,
    public_query,
)
from jobhunter.sources.fixtures import fixture_transport


class Timeouts:
    """The §5.2 timeouts, in seconds."""

    CONNECT: Final = 5.0
    READ: Final = 30.0
    READ_FULL_BOARD: Final = 60.0  # Arbeitnow and the ATS boards
    WRITE: Final = 10.0
    POOL: Final = 5.0
    DEADLINE: Final = 90.0  # the total of one attempt


MAX_BODY_BYTES: Final = 64 * 1024 * 1024
ERROR_BODY_BYTES: Final = 4096
# The §5.2 allowlist; `x-ratelimit-*` is every header whose name starts with `x-ratelimit-`.
STORED_HEADERS: Final = (
    "date",
    "content-type",
    "x-ratelimit-*",
    "x-rapidapi-proxy-response",
    "retry-after",
    "age",
    "cf-cache-status",
    "cache-control",
)
_STORED_NAMES: Final = frozenset(name for name in STORED_HEADERS if not name.endswith("*"))
_STORED_PREFIXES: Final = tuple(name.removesuffix("*") for name in STORED_HEADERS if name.endswith("*"))
FULL_BOARD_SOURCES: Final = frozenset(
    {"arbeitnow", "greenhouse", "lever", "ashby", "personio", "recruitee", "smartrecruiters"}
)
_SOURCE_IDS: Final = frozenset(source.id for source in SOURCES)
PERSONIO_HOST_SUFFIX: Final = ".jobs.personio.de"
MAX_PERSONIO_REDIRECTS: Final = 3

_log = get_logger("jobhunter.sources.http")


@dataclass(frozen=True)
class HttpOutcome:
    """One attempt: the page when a response arrived, the error when the attempt failed, or both (a body
    over the cap is a page and a `ContractViolation`)."""

    page: RawPage | None
    error: SourceError | None


@dataclass(frozen=True)
class _Configuration:
    user_agent: str = DEFAULT_HTTP_USER_AGENT
    transport: httpx.AsyncBaseTransport | None = None  # None: httpx's own transport


_configuration = _Configuration()


def configure(cfg: AppConfig) -> None:
    """Process-wide: the User-Agent of `cfg`, and in fake mode the fixture transport for every new client (on
    the wall clock, so the recorded SerpApi account stays current)."""
    global _configuration
    transport = None
    if cfg.fake_sources:
        log = cfg.environ.get("JOBHUNTER_FIXTURE_LOG")
        transport = fixture_transport(cfg.fixtures_dir, log_path=Path(log) if log else None, clock=utcnow)
    _configuration = _Configuration(cfg.http_user_agent, transport)


def serves_fixtures() -> bool:
    """Whether `configure` put the fixture transport in place: the clients made from now on are served from
    the fixtures (fake mode)."""
    return _configuration.transport is not None


def _reset_configuration() -> None:
    """Back to the defaults (for tests)."""
    global _configuration
    _configuration = _Configuration()


class _SourceClient(httpx.AsyncClient):
    """A client of one source, carrying the source id (for the log) and its body cap (for `send`)."""

    def __init__(
        self,
        *,
        source_id: str,
        max_body_bytes: int,
        timeout: httpx.Timeout,
        user_agent: str,
        transport: httpx.AsyncBaseTransport | None,
    ) -> None:
        super().__init__(
            timeout=timeout, headers={"User-Agent": user_agent}, follow_redirects=False, transport=transport
        )
        self.source_id = source_id
        self.max_body_bytes = max_body_bytes


def make_client(source_id: str, *, max_body_bytes: int = MAX_BODY_BYTES) -> httpx.AsyncClient:
    """The client of `source_id` (one of the `sources` ids): §5.2 timeouts, User-Agent, no redirects."""
    if source_id not in _SOURCE_IDS:
        raise ValueError(f"unknown source {source_id!r}")
    read = Timeouts.READ_FULL_BOARD if source_id in FULL_BOARD_SOURCES else Timeouts.READ
    timeout = httpx.Timeout(connect=Timeouts.CONNECT, read=read, write=Timeouts.WRITE, pool=Timeouts.POOL)
    return _SourceClient(
        source_id=source_id,
        max_body_bytes=max_body_bytes,
        timeout=timeout,
        user_agent=_configuration.user_agent,
        transport=_configuration.transport,
    )


def stored_headers(headers: httpx.Headers) -> dict[str, str]:
    """The `STORED_HEADERS` of `headers`, with lower-case names (repeated headers joined by `, `)."""
    return {
        name: value
        for name, value in ((name.lower(), value) for name, value in headers.items())
        if name in _STORED_NAMES or name.startswith(_STORED_PREFIXES)
    }


def _personio_redirect(request: httpx.Request, response: httpx.Response) -> httpx.Request | None:
    """The next request when `response` redirects from one Personio host to another over https."""
    target = response.next_request
    if target is None:
        return None
    within = all(
        url.scheme == "https" and url.host.endswith(PERSONIO_HOST_SUFFIX) for url in (request.url, target.url)
    )
    return target if within else None


async def _read_page(
    response: httpx.Response, spec: RequestSpec, fetched_at: datetime, max_body_bytes: int
) -> HttpOutcome:
    """Read the body up to the cap: the page, with a `ContractViolation` when it is not whole."""
    status, headers = response.status_code, stored_headers(response.headers)
    body = bytearray()
    error: ContractViolation | None = None
    try:
        async for chunk in response.aiter_bytes():
            body += chunk[: max_body_bytes + 1 - len(body)]
            if len(body) > max_body_bytes:
                error = ContractViolation("body_too_large", status=status)
                break
    except httpx.DecodingError:
        error = ContractViolation("undecodable_body", status=status)
    kept = bytes(body) if error is None else bytes(body[:ERROR_BODY_BYTES])
    return HttpOutcome(RawPage(spec, status, headers, kept, fetched_at), error)


async def _attempt(
    client: httpx.AsyncClient,
    spec: RequestSpec,
    request: httpx.Request,
    *,
    now: Callable[[], datetime],
    max_body_bytes: int,
    hops: list[httpx.Request],
) -> HttpOutcome:
    while True:
        hops.append(request)
        # Redirects stay off whatever the client's own setting: only the Personio family is followed, here.
        response = await client.send(request, stream=True, follow_redirects=False)
        try:
            fetched_at = now()
            follow = None
            if len(hops) <= MAX_PERSONIO_REDIRECTS:
                follow = _personio_redirect(request, response)
            if follow is None:
                return await _read_page(response, spec, fetched_at, max_body_bytes)
        finally:
            await response.aclose()
        request = follow


async def send(
    client: httpx.AsyncClient,
    spec: RequestSpec,
    request: httpx.Request,
    *,
    now: Callable[[], datetime],
    deadline_s: float = Timeouts.DEADLINE,
) -> HttpOutcome:
    """Make one attempt of `request` for `spec` within `deadline_s`; never raises a transport error."""
    source_id = client.source_id if isinstance(client, _SourceClient) else None
    max_body_bytes = client.max_body_bytes if isinstance(client, _SourceClient) else MAX_BODY_BYTES
    loop = asyncio.get_running_loop()
    started = loop.time()
    hops: list[httpx.Request] = []
    try:
        async with asyncio.timeout(deadline_s):
            outcome = await _attempt(client, spec, request, now=now, max_body_bytes=max_body_bytes, hops=hops)
    except httpx.ConnectError:
        # Only the first request of the attempt fails before anything was sent.
        outcome = HttpOutcome(None, SourceError("transient", code="connect", before_send=len(hops) <= 1))
    except httpx.TimeoutException:
        outcome = HttpOutcome(None, SourceError("ambiguous", code="timeout"))
    except TimeoutError:
        outcome = HttpOutcome(None, SourceError("ambiguous", code="deadline"))
    except httpx.TransportError:
        outcome = HttpOutcome(None, SourceError("ambiguous", code="transport"))
    _log.info(
        "source_request",
        source_id=source_id,
        source_instance_id=spec.source_instance_id,
        cell_id=spec.cell_id,
        page_no=spec.page_no,
        request_class=spec.request_class,
        method=request.method,
        host=request.url.host,
        path=request.url.path,
        query=public_query(request.url),
        status=None if outcome.page is None else outcome.page.status,
        error_kind=None if outcome.error is None else outcome.error.kind,
        error_code=None if outcome.error is None else outcome.error.code,
        redirects=max(0, len(hops) - 1),
        duration_ms=round((loop.time() - started) * 1000),
    )
    return outcome


def missing_credential(spec: RequestSpec, *credentials: bytes) -> SourceError | None:
    """The refusal of a paid request for `spec` when any of its `credentials` is missing or empty (blank),
    else `None`; the adapter then builds no request and sends nothing.

    Should an attempt reach an adapter without its key (its instance restored to `ok` from a stale view of
    the secrets, final review C2), it fails as `auth` (code `missing_secret`) with nothing sent: the
    settlement refunds it (`before_send`: no byte left the process, `connect_failed_before_send`) and the
    breaker opens `auth_failed` at once, so no later attempt is reserved. Logged `source_request_refused` with
    the source instance and the code only, never a credential or its length.
    """
    if all(credential.strip() for credential in credentials):
        return None
    _log.warning(
        "source_request_refused",
        source_instance_id=spec.source_instance_id,
        cell_id=spec.cell_id,
        page_no=spec.page_no,
        error_kind="auth",
        error_code=MISSING_SECRET,
    )
    return SourceError("auth", code=MISSING_SECRET, before_send=True)
