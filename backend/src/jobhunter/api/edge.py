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
4. stops new work while delete-everything's `global` purge fence stands (§20.2, §20.4; M5 Task 8, P3, D15):
   a state-changing `/api` request that `fence_exempt` does not exempt (the login, the logout, the purge
   retry and every route under `/api/privacy/`, matched by method and path, since no route is matched yet
   and so no template is known) is answered with its own 409 `delete_everything_in_progress` problem while
   the fence is active (one read on the edge's engine, before the app sees the request and before its body
   is read; logged `global_fence_refused` at info). Otherwise the edge sets `db.fence_guard.WRITE_GUARD` for
   the rest of the request, so each transaction the request begins reads the fence first and a fence
   committed after the edge's read refuses it, and resets it once the app returned: an in-process client
   runs the app in its own task, where a value left set would guard its later transactions. Without an
   engine (the state is not set yet) nothing is read and nothing is guarded;
5. bounds the request body at `MAX_BODY_BYTES` (1 MiB) before the app sees it, so no JSON is parsed past
   it (final review FR-4): a declared `Content-Length` over the limit is refused before any byte is read, and
   a body without a trustworthy one (chunked, or not a number) is counted as it streams and refused as soon as
   it passes the limit, without reading further. Either is a 413 `upload_rejected` with `too_large` on `body`
   (the upload routes' code for a body that is too large), logged `request_body_too_large` at info, and no
   alert. A body within the limit is held (at most 1 MiB) and given to the app in the messages it came in.
   The routes in `OWN_BODY_LIMITS` read their bodies themselves under their own limits (`profile.upload`:
   the multipart CV upload up to 10 MiB while it streams, the paste under its own 1 MiB cap with its own
   field), so the edge reads nothing of theirs;
6. counts `HTTP_REQUESTS` by route template (or `"unmatched"`, never the raw path; ruling T14-c) and status;
7. renders a 500 `internal` problem when the inner app raises before a response started, then persists the
   `api.unhandled_exception` alert. When a response has already started, the inner handler rendered it and
   persisted the alert (or a body failed mid-stream), so the edge only logs.

Nothing propagates to the server: uvicorn would log the exception with its traceback, and an exception message
must never reach a log line. This module is a task boundary (ruff `BLE001` is allowed here).
"""

import re
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Final, Literal

import structlog
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from jobhunter.api.errors import (
    FieldError,
    Problem,
    internal_problem,
    persist_unhandled_alert,
    problem_of,
    problem_response,
    request_instance,
)
from jobhunter.config import AppConfig
from jobhunter.db.fence_guard import WRITE_GUARD, delete_everything_in_progress
from jobhunter.db.guards import global_fence_active
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
# What a `global` purge fence lets through (module docstring, step 4): the logout, the login, every route
# under `PRIVACY_PREFIX`, and the retry of a failed purge (`PURGE_RETRY`, the only recovery of a failed
# delete-everything, whose fence stays), matched against the whole path with ASCII digits only.
LOGOUT_PATH: Final = "/api/auth/logout"
PRIVACY_PREFIX: Final = "/api/privacy/"
PURGE_RETRY: Final = re.compile(r"/api/system/purges/[0-9]+/retry")
CsrfFailure = Literal["origin_missing", "origin_mismatch", "header_missing"]
# The body ceiling of every request (module docstring, step 5), and the routes that read their own bodies
# under their own limits (`profile.upload.MAX_UPLOAD_BYTES` and `PASTE_BODY_MAX_BYTES`).
MAX_BODY_BYTES: Final = 2**20
OWN_BODY_LIMITS: Final = frozenset({("POST", "/api/profile/cv"), ("POST", "/api/profile/cv-text")})

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


def _state_changing_api(scope: Scope) -> bool:
    """A POST, PUT, PATCH or DELETE under `/api`: what the CSRF check and the fence's stop apply to."""
    path = str(scope.get("path", ""))
    return scope.get("method") in STATE_CHANGING_METHODS and (path == "/api" or path.startswith("/api/"))


def fence_exempt(method: str, path: str) -> bool:
    """Whether a state-changing `/api` request goes on while delete-everything's `global` fence stands
    (module docstring, step 4): `POST` of the login, the logout or a purge's retry, or any method under
    `/api/privacy/`. Matched on `scope["path"]`, before routing."""
    if path.startswith(PRIVACY_PREFIX):
        return True
    return method == "POST" and (path in (LOGIN_PATH, LOGOUT_PATH) or PURGE_RETRY.fullmatch(path) is not None)


def fence_guarded(scope: Scope) -> bool:
    """Whether the request of `scope` is stopped by the `global` fence and guarded (module docstring, step 4):
    a state-changing `/api` request that `fence_exempt` does not exempt."""
    method, path = str(scope.get("method", "")), str(scope.get("path", ""))
    return _state_changing_api(scope) and not fence_exempt(method, path)


def csrf_failure(scope: Scope, public_origin: str | None) -> CsrfFailure | None:
    """Why the request of `scope` fails the CSRF check, or `None` when it passes or is not subject to it.

    It applies to POST, PUT, PATCH and DELETE under `/api`. `Origin` must be sent once and equal
    `public_origin` exactly (scheme, host and port, as browsers send it); without a configured origin every
    such request fails. `X-JH-Request: 1` is also required, except on the login.
    """
    if not _state_changing_api(scope):
        return None
    path = str(scope.get("path", ""))
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


async def _global_fence(engine: AsyncEngine) -> bool:
    """Whether delete-everything's `global` fence stands: one read, on a connection of its own."""
    async with engine.connect() as conn:  # rolled back when the block ends
        return await global_fence_active(conn)


def _too_large_problem() -> Problem:
    message = f"The request body is larger than {MAX_BODY_BYTES} bytes"
    return Problem(
        413,
        "upload_rejected",
        "Request body too large",
        errors=[FieldError(field="body", code="too_large", message=message)],
    )


def _declared_length(scope: Scope) -> int | None:
    """The request's `Content-Length` when it is one decimal number, else `None` (none, or not a number)."""
    values = _header_values(scope, b"content-length")
    if len(values) != 1 or not values[0].isascii() or not values[0].isdigit():
        return None
    return int(values[0])


async def bounded_body(scope: Scope, receive: Receive) -> Receive | None:
    """The request's body read to its end within `MAX_BODY_BYTES`, as a `receive` that gives its messages
    again and then the server's own; `None` when the body is over the limit (module docstring, step 5). A
    disconnect ends the reading and is given on as it came."""
    declared = _declared_length(scope)
    if declared is not None and declared > MAX_BODY_BYTES:
        return None
    messages: list[Message] = []
    received = 0
    while True:
        message = await receive()
        messages.append(message)
        if message["type"] != "http.request":
            break
        received += len(message.get("body", b""))
        if received > MAX_BODY_BYTES:
            return None
        if not message.get("more_body", False):
            break
    pending = iter(messages)

    async def replay() -> Message:
        return next(pending, None) or await receive()

    return replay


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
        # `config` serves the CSRF check (Task 16); `engine` the global fence's read (M5 Task 8) and the
        # alert of the last-resort 500.
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
                engine = self._engine()
                fenced_engine = engine if engine is not None and fence_guarded(scope) else None
                if failure is not None:
                    log.warning("csrf_rejected", reason=failure)
                    response = problem_response(
                        _csrf_problem(), instance=request_instance(scope), request_id=request_id
                    )
                    await response(scope, receive, send_with_edge_headers)
                elif fenced_engine is not None and await _global_fence(fenced_engine):
                    log.info("global_fence_refused", method=scope.get("method"))
                    response = problem_response(
                        problem_of(delete_everything_in_progress()),
                        instance=request_instance(scope),
                        request_id=request_id,
                    )
                    await response(scope, receive, send_with_edge_headers)
                else:
                    token = None if fenced_engine is None else WRITE_GUARD.set(True)
                    try:
                        await self._admitted(scope, receive, send_with_edge_headers, request_id=request_id)
                    finally:
                        if token is not None:
                            WRITE_GUARD.reset(token)
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

    async def _admitted(self, scope: Scope, receive: Receive, send: Send, *, request_id: str) -> None:
        """A request the CSRF check and the fence let through: its body bounded (module docstring, step 5),
        then the app."""
        if (scope.get("method"), scope.get("path")) in OWN_BODY_LIMITS:
            await self.app(scope, receive, send)
        elif (bounded := await bounded_body(scope, receive)) is not None:
            await self.app(scope, bounded, send)
        else:
            log.info("request_body_too_large", method=scope.get("method"), limit_bytes=MAX_BODY_BYTES)
            response = problem_response(
                _too_large_problem(), instance=request_instance(scope), request_id=request_id
            )
            await response(scope, receive, send)

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
