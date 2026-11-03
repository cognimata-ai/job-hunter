"""RFC 9457 problem details (spec §14.4) and the exception handlers of the FastAPI app.

Every error response is `application/problem+json` with `type = PROBLEM_TYPE_BASE + code`. `problem_body` is
the one place that builds that document; the handlers here and the edge middleware both use it.

The handlers (installed through `exception_handlers()`):
- `Problem`: its own document;
- `jobhunter.errors.Refusal`, what domain code raises instead of a `Problem` (M4 Task 1, R6): the document of
  `problem_of(refusal)`, the `Problem` with its status, code, title, detail, fields (as `FieldError`s, in
  order) and extra members, so a refusal answers byte for byte what the `Problem` it replaces did;
- `RequestValidationError`: 422 `validation_failed`, one `FieldError` per pydantic error (`field` is the `loc`
  joined with "."; `code` is the pydantic error type; `message` is pydantic's `msg`). The `msg` can echo the
  sender's own input (a validator's `ValueError` text, a parse error's offending character): it goes back to
  that sender only and is never logged (ruling T14-h). Pydantic's `input` member is never copied;
- Starlette's `HTTPException`: mapped by `code_for_http_status` (404 and 405 are `not_found`; a 405 keeps its
  `Allow` header);
- any other `Exception`: the last-resort 500 `internal`. It logs `unhandled_exception` with the error class
  only and persists the deduplicated `api.unhandled_exception` alert (§17.1). Starlette runs it from
  `ServerErrorMiddleware`, which then re-raises to the edge.

This module is a task boundary (ruff `BLE001` is allowed here). No exception message or traceback reaches a
log line or an alert, and none reaches a response body except the 422 validation messages above: they can
carry SQL, input or secrets.

`domain_problem(exc)` is the one mapping of the other domain errors a route meets, which the route raises
`from None` in place of its own mapping (M4 Task 1, R6): a `LookupError` that is neither a `KeyError` nor an
`IndexError` → 404 `not_found` (a named domain lookup such as `auth.users.UserMissing` or
`scoring.eval_runner.NoCandidate`, whichever subclass; ruling T1-a); `FencedOut` → 409 `conflict_state` with
`detail = "purge_in_progress"`; `CoMemberChanged` (a read-then-lock round that gave up on a job set changing
under its lock, M4 Task 8), checked before the other `InvariantViolation`s → `retryable_conflict()`, 409
`conflict_state` with `retryable: true`, which `api.retry.with_deadlock_retry` answers its last deadlock with
too; `InvariantViolation` and `TransitionConflict` → 409 `conflict_state`; anything else `None`. Their
messages, which name tables and ids, stay out of the body. A `KeyError` or `IndexError` is a bug, which the
last-resort handler answers 500 and alerts on; so there is no global `LookupError` handler either.
`as_problem(exc)` is the one view of a `Problem` or a `Refusal` that a route's catch logs (ruling T1-c).
"""

import asyncio
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, Final

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse, Response
from starlette.types import Scope

from jobhunter.errors import (
    RESERVED_PROBLEM_MEMBERS,
    CoMemberChanged,
    ErrorCode,
    FencedOut,
    InvariantViolation,
    Refusal,
    TransitionConflict,
)
from jobhunter.observability.alerts import raise_alert
from jobhunter.observability.logging import get_logger
from jobhunter.types import JSONValue

ProblemCode = ErrorCode  # the codes of §14.4, which live in `jobhunter.errors`: a `Refusal` carries one too

PROBLEM_TYPE_BASE: Final = "https://jobhunter.local/problems/"
PROBLEM_MEDIA_TYPE: Final = "application/problem+json"
UNHANDLED_ALERT_CODE: Final = "api.unhandled_exception"
# The `detail` of a fenced-out owner's 409 (`domain_problem`).
PURGE_IN_PROGRESS: Final = "purge_in_progress"
# The problem member that tells the client to send the same request again (`retryable_conflict`).
RETRYABLE: Final = "retryable"
# How long the last-resort handler waits for the alert write; a database that does not answer is no reason to
# hold the 500 back for longer.
ALERT_PERSIST_TIMEOUT_S: Final = 5.0

# The members `problem_body` writes itself; `extra` cannot replace them (`jobhunter.errors`', ruling T1-b).
_STANDARD_MEMBERS: Final = RESERVED_PROBLEM_MEMBERS

ExceptionHandlers = dict[int | type[Exception], Callable[[Request, Any], Coroutine[Any, Any, Response]]]

log = get_logger("jobhunter.api")


@dataclass(frozen=True)
class FieldError:
    field: str
    code: str
    message: str


class Problem(Exception):
    """An error response the api means to send: raise it from a route or a dependency.

    `str(problem)` is its `code`. `detail` and every `FieldError.message` go to the client: a `detail` carries
    no secret and no PII; a validation message may echo the sender's own input (ruling T14-h), so no field
    of a problem is ever logged.
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


def problem_of(refusal: Refusal) -> Problem:
    """The problem of a domain refusal: its status, code, title, detail, fields and extra members."""
    fields = refusal.fields
    errors = None if fields is None else [FieldError(f.field, f.code, f.message) for f in fields]
    return Problem(
        refusal.status, refusal.code, refusal.title, detail=refusal.detail, errors=errors, extra=refusal.extra
    )


def as_problem(exc: Problem | Refusal) -> Problem:
    """`exc` itself for a `Problem`, `problem_of(exc)` for a `Refusal`."""
    return exc if isinstance(exc, Problem) else problem_of(exc)


def retryable_conflict() -> Problem:
    """409 `conflict_state` with `retryable: true`: a concurrent change the same request may outlast."""
    return Problem(
        409,
        "conflict_state",
        "A concurrent change got in the way",
        detail="the jobs changed while this request was being applied; send it again",
        extra={RETRYABLE: True},
    )


def domain_problem(exc: BaseException) -> Problem | None:
    """The problem of a domain error a route meets, or `None` for any other (module docstring)."""
    if isinstance(exc, LookupError) and not isinstance(exc, KeyError | IndexError):
        return Problem(404, "not_found", "Not found")
    if isinstance(exc, FencedOut):
        return Problem(409, "conflict_state", "A purge is in progress", detail=PURGE_IN_PROGRESS)
    if isinstance(exc, CoMemberChanged):  # before the `InvariantViolation` it is
        return retryable_conflict()
    if isinstance(exc, InvariantViolation | TransitionConflict):
        return Problem(409, "conflict_state", "The current state does not allow this")
    return None


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


async def _on_refusal(request: Request, exc: Refusal) -> Response:
    return _respond(request, problem_of(exc))


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


def code_for_http_status(status: int) -> ProblemCode:
    """The problem code of an `HTTPException` that Starlette, FastAPI or a library raised (not a `Problem`).

    - 404 and 405 (routing: no route, or no route for the method) → `not_found`;
    - 401 (e.g. FastAPI's security helpers) → `unauthenticated`;
    - 409 (a library's conflict) → `conflict_state`;
    - 413 (a body over the size limit) → `upload_rejected`;
    - 429 (a rate limit) → `throttled`;
    - any other 4xx (e.g. 400 for a body that cannot be parsed) → `validation_failed`;
    - 5xx → `internal`.
    """
    if status in (404, 405):
        return "not_found"
    if status == 401:
        return "unauthenticated"
    if status == 409:
        return "conflict_state"
    if status == 413:
        return "upload_rejected"
    if status == 429:
        return "throttled"
    if status >= 500:
        return "internal"
    return "validation_failed"


async def _on_http_exception(request: Request, exc: StarletteHTTPException) -> Response:
    # Their `detail` is not passed on: it is library text, not a client message. Their headers (`Allow` of a
    # 405, `WWW-Authenticate` of a 401) are.
    status = exc.status_code if 400 <= exc.status_code <= 599 else 500
    problem = Problem(status, code_for_http_status(status), _http_status_title(status), headers=exc.headers)
    return _respond(request, problem)


async def _on_unhandled(request: Request, exc: Exception) -> Response:
    log.error("unhandled_exception", error_class=type(exc).__name__)
    await persist_unhandled_alert(getattr(request.app.state, "engine", None), exc)
    return _respond(request, internal_problem())


def exception_handlers() -> ExceptionHandlers:
    """The handlers for `FastAPI(exception_handlers=...)`; they replace FastAPI's defaults."""
    return {
        Problem: _on_problem,
        Refusal: _on_refusal,
        RequestValidationError: _on_validation_error,
        StarletteHTTPException: _on_http_exception,
        Exception: _on_unhandled,
    }
