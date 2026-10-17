"""The outermost ASGI layer of the api (spec §14.1, §16.6, §17.1).

`EdgeMiddleware` wraps the whole FastAPI app, Starlette's `ServerErrorMiddleware` included, so every HTTP
response passes through it: route responses, problems, the inner last-resort 500, and what the edge renders
itself. For each HTTP request it:

1. assigns a UUIDv7 request id, stored in `scope["state"]["request_id"]` (`request.state.request_id`) and
   bound to the structlog contextvars for the request only (unbound again when the request ends, ruling
   T14-d);
2. sets `X-Request-Id` and `SECURITY_HEADERS` on every `http.response.start`;
3. refuses a state-changing `/api` request that fails the CSRF check (§14.1, §16.5) with its own 403
   `csrf_failed` problem, before the app sees it (`csrf_failure`);
4. counts `HTTP_REQUESTS` by route template (or `"unmatched"`, never the raw path; ruling T14-c) and status;
5. renders a 500 `internal` problem when the inner app raises before a response started, then persists the
   `api.unhandled_exception` alert. When a response has already started, the inner handler rendered it and
   persisted the alert (or a body failed mid-stream), so the edge only logs.

Nothing propagates to the server: uvicorn would log the exception with its traceback, and an exception message
must never reach a log line. This module is a task boundary (ruff `BLE001` is allowed here).
"""

from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Final, Literal

import structlog
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from jobhunter.api.errors import (
    Problem,
    internal_problem,
    persist_unhandled_alert,
    problem_response,
    request_instance,
)
from jobhunter.config import AppConfig
from jobhunter.ids import uuid7
from jobhunter.observability.logging import get_logger
from jobhunter.observability.metrics import HTTP_REQUESTS

# §16.6, mirrored here for direct `api` access; Caddy sets the same headers in front of it.
SECURITY_HEADERS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
        "Content-Security-Policy": (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
            "form-action 'self'; object-src 'none'"
        ),
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=(), interest-cohort=()",
        "Cross-Origin-Opener-Policy": "same-origin",
        "Cross-Origin-Resource-Policy": "same-origin",
    }
)
REQUEST_ID_HEADER: Final = "X-Request-Id"
# The `route` label of a request that matched no route (a 404) or matched a path but not its method (a 405).
UNMATCHED_ROUTE: Final = "unmatched"
# §16.5: the methods that may change state, and the custom header a cross-origin page cannot send without a
# CORS preflight (and the api answers no preflight). The login has no session yet: Origin only.
STATE_CHANGING_METHODS: Final = frozenset({"POST", "PUT", "PATCH", "DELETE"})
CSRF_HEADER: Final = "X-JH-Request"
LOGIN_PATH: Final = "/api/auth/login"
CsrfFailure = Literal["origin_missing", "origin_mismatch", "header_missing"]

log = get_logger("jobhunter.api.edge")


def route_label(scope: Scope) -> str:
    """The template of the route that served the request (e.g. `/api/runs/{run_id}`), else `UNMATCHED_ROUTE`.

    Starlette's router stores the matched route in `scope["route"]`, also for a path that matched with another
    method (the 405 case), which is why the method is checked too.
    """
    route = scope.get("route")
    path = getattr(route, "path", None)
    methods = getattr(route, "methods", None)
    if not isinstance(path, str) or (methods is not None and scope.get("method") not in methods):
        return UNMATCHED_ROUTE
    return path


def _header_values(scope: Scope, name: bytes) -> list[str]:
    return [value.decode("latin-1") for key, value in scope.get("headers", []) if key == name]


def csrf_failure(scope: Scope, public_origin: str | None) -> CsrfFailure | None:
    """Why the request of `scope` fails the CSRF check, or `None` when it passes or is not subject to it.

    It applies to POST, PUT, PATCH and DELETE under `/api`. `Origin` must be sent once and equal
    `public_origin` exactly (scheme, host and port, as browsers send it); without a configured origin every
    such request fails. `X-JH-Request: 1` is also required, except on the login.
    """
    if scope.get("method") not in STATE_CHANGING_METHODS:
        return None
    path = str(scope.get("path", ""))
    if path != "/api" and not path.startswith("/api/"):
        return None
    origins = _header_values(scope, b"origin")
    if not origins:
        return "origin_missing"
    if public_origin is None or origins != [public_origin]:
        return "origin_mismatch"
    if path != LOGIN_PATH and _header_values(scope, CSRF_HEADER.lower().encode("ascii")) != ["1"]:
        return "header_missing"
    return None


def _csrf_problem() -> Problem:
    return Problem(403, "csrf_failed", "Cross-site request refused")


class EdgeMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        config: Callable[[], AppConfig | None],
        engine: Callable[[], AsyncEngine | None],
    ) -> None:
        self.app = app
        # Both return `None` until the app state is set (the lifespan, or `create_app` with injected state).
        # `config` serves the CSRF check (Task 16); `engine` the alert of the last-resort 500.
        self._config = config
        self._engine = engine

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = str(uuid7())
        # A dict of this request's own: the server's copy of the lifespan state is never written to.
        scope["state"] = {**scope.get("state", {}), "request_id": request_id}
        status: int | None = None

        async def send_with_edge_headers(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
                message.setdefault("headers", [])
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers[name] = value
                headers[REQUEST_ID_HEADER] = request_id
            await send(message)

        with structlog.contextvars.bound_contextvars(request_id=request_id):
            try:
                cfg = self._config()
                failure = csrf_failure(scope, None if cfg is None else cfg.public_origin)
                if failure is None:
                    await self.app(scope, receive, send_with_edge_headers)
                else:
                    log.warning("csrf_rejected", reason=failure)
                    response = problem_response(
                        _csrf_problem(), instance=request_instance(scope), request_id=request_id
                    )
                    await response(scope, receive, send_with_edge_headers)
            except Exception as exc:
                await self._last_resort(
                    scope,
                    receive,
                    send_with_edge_headers,
                    exc,
                    request_id=request_id,
                    started=status is not None,
                )
            finally:
                if status is not None:
                    HTTP_REQUESTS.labels(route=route_label(scope), status=str(status)).inc()

    async def _last_resort(
        self, scope: Scope, receive: Receive, send: Send, exc: Exception, *, request_id: str, started: bool
    ) -> None:
        error_class = type(exc).__name__
        if started:
            # Nothing more can be sent. The usual case is a 500 that `ServerErrorMiddleware` sent and
            # re-raised after its handler had logged `unhandled_exception` and persisted the alert.
            log.info("unhandled_exception_after_start", error_class=error_class)
            return
        log.error("unhandled_exception", error_class=error_class)
        response = problem_response(
            internal_problem(), instance=request_instance(scope), request_id=request_id
        )
        try:
            await response(scope, receive, send)
        except Exception as failure:
            # The client is gone or the transport broke; the alert below is still the outcome.
            log.warning("edge_response_failed", error_class=type(failure).__name__)
        await persist_unhandled_alert(self._engine(), exc)
