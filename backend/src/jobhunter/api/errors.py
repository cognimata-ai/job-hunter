"""RFC 9457 problem details (spec §14.4) and the exception handlers of the FastAPI app.

Every error response is `application/problem+json` with `type = PROBLEM_TYPE_BASE + code`. `problem_body` is
the one place that builds that document; the handlers here and the edge middleware both use it.

The handlers (installed through `exception_handlers()`):
- `Problem`: its own document;
- `RequestValidationError`: 422 `validation_failed`, one `FieldError` per pydantic error (`field` is the `loc`
  joined with "."; `code` is the pydantic error type; never the input);
- Starlette's `HTTPException`: 404 and 405 are `not_found` (a 405 keeps its `Allow` header);
- any other `Exception`: the last-resort 500 `internal`. It logs `unhandled_exception` with the error class
  only and persists the deduplicated `api.unhandled_exception` alert (§17.1). Starlette runs it from
  `ServerErrorMiddleware`, which then re-raises to the edge.

This module is a task boundary (ruff `BLE001` is allowed here). No exception message or traceback reaches a
response body, a log line or an alert: they can carry SQL, input or secrets.
"""

import asyncio
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, Final, Literal

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse, Response
from starlette.types import Scope

from jobhunter.observability.alerts import raise_alert
from jobhunter.observability.logging import get_logger
from jobhunter.types import JSONValue

ProblemCode = Literal[
    "unauthenticated",
    "csrf_failed",
    "throttled",
    "invalid_cursor",
    "stale_base_version",
    "validation_failed",
    "budget_exhausted",
    "filtered_requires_force",
    "run_active",
    "not_found",
    "conflict_state",
    "upload_rejected",
    "idempotency_key_required",
    "idempotency_key_reused",
    "idempotency_in_progress",
    "eval_gate_failed",
    "internal",
]

PROBLEM_TYPE_BASE: Final = "https://jobhunter.local/problems/"
PROBLEM_MEDIA_TYPE: Final = "application/problem+json"
UNHANDLED_ALERT_CODE: Final = "api.unhandled_exception"
# How long the last-resort handler waits for the alert write; a database that does not answer is no reason to
# hold the 500 back for longer.
ALERT_PERSIST_TIMEOUT_S: Final = 5.0

# The members `problem_body` writes itself; `extra` cannot replace them.
_STANDARD_MEMBERS: Final = frozenset(
    {"type", "title", "status", "code", "detail", "instance", "request_id", "errors"}
)

ExceptionHandlers = dict[int | type[Exception], Callable[[Request, Any], Coroutine[Any, Any, Response]]]

log = get_logger("jobhunter.api")


@dataclass(frozen=True)
class FieldError:
    field: str
    code: str
    message: str


class Problem(Exception):
    """An error response the api means to send: raise it from a route or a dependency.

    `str(problem)` is its `code`; `detail` and every `FieldError.message` go to the client, so they carry no
    secret and no PII.
    """

    def __init__(
        self,
        status: int,
        code: ProblemCode,
        title: str,
        *,
        detail: str | None = None,
        errors: list[FieldError] | None = None,
        headers: Mapping[str, str] | None = None,
        extra: Mapping[str, JSONValue] | None = None,
    ) -> None:
        super().__init__(code)
        if not 400 <= status <= 599:
            raise ValueError(f"a problem has a 4xx or 5xx status, not {status}")
        if clash := _STANDARD_MEMBERS & set(extra or {}):
            raise ValueError(f"extra cannot replace the standard members {sorted(clash)}")
        self.status = status
        self.code: ProblemCode = code
        self.title = title
        self.detail = detail
        self.errors = None if errors is None else list(errors)
        self.headers = dict(headers or {})
        self.extra = dict(extra or {})


def problem_body(problem: Problem, *, instance: str, request_id: str) -> dict[str, JSONValue]:
    """The problem document of `problem` for the request `instance` (its path) with id `request_id`."""
    body: dict[str, JSONValue] = {
        "type": PROBLEM_TYPE_BASE + problem.code,
        "title": problem.title,
        "status": problem.status,
        "code": problem.code,
    }
    if problem.detail is not None:
        body["detail"] = problem.detail
    body["instance"] = instance
    body["request_id"] = request_id
    if problem.errors is not None:
        body["errors"] = [{"field": e.field, "code": e.code, "message": e.message} for e in problem.errors]
    body.update(problem.extra)
    return body


def problem_response(problem: Problem, *, instance: str, request_id: str) -> Response:
    return JSONResponse(
        problem_body(problem, instance=instance, request_id=request_id),
        status_code=problem.status,
        headers=problem.headers,
        media_type=PROBLEM_MEDIA_TYPE,
    )


def internal_problem() -> Problem:
    return Problem(500, "internal", "Internal server error")


def request_instance(scope: Scope) -> str:
    """The `instance` of a problem: the request path, without the query string."""
    return str(scope.get("path", ""))


def _request_id(request: Request) -> str:
    # The edge sets it before the app runs. An error path must not fail on its absence, so "" stands in.
    request_id = getattr(request.state, "request_id", None)
    return request_id if isinstance(request_id, str) else ""


def _respond(request: Request, problem: Problem) -> Response:
    return problem_response(
        problem, instance=request_instance(request.scope), request_id=_request_id(request)
    )


async def persist_unhandled_alert(engine: AsyncEngine | None, exc: BaseException) -> None:
    """Raise (or count again) the `api.unhandled_exception` alert of `exc`'s class in its own transaction.

    Best effort: with no engine yet, or when the write fails or takes longer than `ALERT_PERSIST_TIMEOUT_S`,
    it logs `alert_persist_failed` with `error_class` (the class of `exc`) and `persist_error_class` (the
    class of the write's failure, `None` without an engine) and returns. It never raises.
    """
    error_class = type(exc).__name__
    if engine is None:
        log.error("alert_persist_failed", error_class=error_class, persist_error_class=None)
        return
    try:
        async with asyncio.timeout(ALERT_PERSIST_TIMEOUT_S), engine.begin() as conn:
            await raise_alert(
                conn,
                code=UNHANDLED_ALERT_CODE,
                severity="warn",
                subject=error_class,
                message="unhandled exception in api",
                context={"error_class": error_class},
            )
    except Exception as failure:
        # The database that would hold the outcome is the one that failed: this line is the outcome left.
        log.error("alert_persist_failed", error_class=error_class, persist_error_class=type(failure).__name__)


async def _on_problem(request: Request, exc: Problem) -> Response:
    return _respond(request, exc)


async def _on_validation_error(request: Request, exc: RequestValidationError) -> Response:
    errors = [
        FieldError(
            field=".".join(str(part) for part in error["loc"]),
            code=str(error["type"]),
            message=str(error["msg"]),
        )
        for error in exc.errors()
    ]
    return _respond(request, Problem(422, "validation_failed", "Request validation failed", errors=errors))


def _http_status_title(status: int) -> str:
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return "Error"


async def _on_http_exception(request: Request, exc: StarletteHTTPException) -> Response:
    # Routing raises 404 and 405; the rest come from Starlette or FastAPI themselves (a body that cannot be
    # parsed is a 400). Their `detail` is not passed on: it is library text, not a client message.
    status = exc.status_code if 400 <= exc.status_code <= 599 else 500
    code: ProblemCode
    if status in (404, 405):
        code = "not_found"
    elif status >= 500:
        code = "internal"
    else:
        code = "validation_failed"
    return _respond(request, Problem(status, code, _http_status_title(status), headers=exc.headers))


async def _on_unhandled(request: Request, exc: Exception) -> Response:
    log.error("unhandled_exception", error_class=type(exc).__name__)
    await persist_unhandled_alert(getattr(request.app.state, "engine", None), exc)
    return _respond(request, internal_problem())


def exception_handlers() -> ExceptionHandlers:
    """The handlers for `FastAPI(exception_handlers=...)`; they replace FastAPI's defaults."""
    return {
        Problem: _on_problem,
        RequestValidationError: _on_validation_error,
        StarletteHTTPException: _on_http_exception,
        Exception: _on_unhandled,
    }
