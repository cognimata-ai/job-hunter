"""The domain refusal type (M4 Task 1, R6): domain code raises `jobhunter.errors.Refusal`, and the api answers
it in one place with the document the `Problem` it replaces gave, byte for byte (`api.errors.problem_of` and
the `Refusal` handler). `api.errors.domain_problem` is the one mapping of the other domain errors a route
meets.
"""

import json
from typing import Final

import httpx
import pytest
from fastapi import APIRouter
from jobhunter.api.app import create_app
from jobhunter.api.errors import Problem, as_problem, domain_problem, problem_body, problem_of
from jobhunter.auth.users import UserMissing
from jobhunter.errors import (
    RESERVED_PROBLEM_MEMBERS,
    FencedOut,
    InvariantViolation,
    Refusal,
    RefusalField,
    TransitionConflict,
)
from jobhunter.settings.save import schema_dry_run_required
from jobhunter.types import JSONValue
from starlette.responses import JSONResponse

INSTANCE: Final = "/api/settings"
REQUEST_ID: Final = "0190a0b0-0000-7000-8000-000000000001"


def rendered(problem: Problem) -> list[tuple[str, JSONValue]]:
    """The members of `problem`'s document in the order the response writes them."""
    return list(problem_body(problem, instance=INSTANCE, request_id=REQUEST_ID).items())


def test_problem_of_keeps_status_code_title_detail_errors_and_extra() -> None:
    missing = [("score", "model-a"), ("critic", "model-b")]
    # what `settings/save.py` built before the change, with the same arguments
    before = Problem(
        409,
        "conflict_state",
        "The model policy has no passed live schema dry-run for score on model-a, critic on model-b",
        detail="schema_dry_run_required",
    )
    refusal = schema_dry_run_required(missing)
    assert isinstance(refusal, Refusal) and str(refusal) == "conflict_state"
    assert rendered(problem_of(refusal)) == rendered(before)

    fields = [
        RefusalField("queries.0.query_id", "unknown_query", "no such query"),
        RefusalField("watchlist.0", "unknown_company", "no such company"),
    ]
    two = Refusal(
        422, "validation_failed", "Settings validation failed", fields=fields, extra={"retryable": True}
    )
    fields.reverse()  # the refusal holds its own copy
    assert rendered(problem_of(two)) == [
        ("type", "https://jobhunter.local/problems/validation_failed"),
        ("title", "Settings validation failed"),
        ("status", 422),
        ("code", "validation_failed"),
        ("instance", INSTANCE),
        ("request_id", REQUEST_ID),
        (
            "errors",
            [
                {"field": "queries.0.query_id", "code": "unknown_query", "message": "no such query"},
                {"field": "watchlist.0", "code": "unknown_company", "message": "no such company"},
            ],
        ),
        ("retryable", True),
    ]
    assert str(two) == "validation_failed"

    no_fields = dict(rendered(problem_of(Refusal(409, "conflict_state", "Busy", detail="later"))))
    assert "errors" not in no_fields and no_fields["detail"] == "later"
    assert dict(rendered(problem_of(Refusal(422, "validation_failed", "Empty", fields=[]))))["errors"] == []


def test_a_refusal_is_4xx_only() -> None:
    for status in (200, 399, 500, 503, 599):
        with pytest.raises(ValueError, match=f"not {status}"):
            Refusal(status, "internal", "Internal server error")
    assert [Refusal(status, "validation_failed", "t").status for status in (400, 499)] == [400, 499]


def test_a_refusal_refuses_a_reserved_extra_member() -> None:
    """Ruling T1-b: at construction, as a `Problem` does, never only when the api answers it."""
    assert {"status", "errors"} <= RESERVED_PROBLEM_MEMBERS
    for member in sorted(RESERVED_PROBLEM_MEMBERS):  # the one set: the api's `Problem` refuses the same names
        with pytest.raises(ValueError, match=member):
            Refusal(409, "conflict_state", "Busy", extra={member: 1, "retryable": True})
        with pytest.raises(ValueError, match=member):
            Problem(409, "conflict_state", "Busy", extra={member: 1, "retryable": True})
    assert Refusal(409, "conflict_state", "Busy", extra={"retryable": True}).extra == {"retryable": True}


def test_as_problem_keeps_a_problem_and_maps_a_refusal() -> None:
    """Ruling T1-c: the one view the route catches log a refusal or a problem through."""
    problem = Problem(409, "idempotency_in_progress", "A request with this Idempotency-Key is still running")
    assert as_problem(problem) is problem
    refusal = Refusal(422, "validation_failed", "Invalid", fields=[RefusalField("a", "b", "c")])
    assert rendered(as_problem(refusal)) == rendered(problem_of(refusal))


class _NoSuchDraft(LookupError):
    """A domain lookup that found no row, as `UserMissing` and `eval_runner.NoCandidate` are."""


def test_domain_problem_maps_the_domain_errors() -> None:
    cases: list[tuple[BaseException, tuple[int, str, str | None]]] = [
        (LookupError("profile_drafts 7 has no row"), (404, "not_found", None)),
        (InvariantViolation("ranking_state row 1 is missing"), (409, "conflict_state", None)),
        (FencedOut("purge_fence"), (409, "conflict_state", "purge_in_progress")),
        (TransitionConflict("score_targets", 7, "published"), (409, "conflict_state", None)),
    ]
    for exc, expected in cases:
        problem = domain_problem(exc)
        assert problem is not None, exc
        assert (problem.status, problem.code, problem.detail) == expected
        assert str(exc) not in json.dumps(rendered(problem))  # its message names tables and ids: not sent
    assert domain_problem(KeyError("settings_version_id")) is None  # a bug, never a 404
    assert domain_problem(IndexError("list index out of range")) is None  # a bug too (ruling T1-a)
    assert domain_problem(ValueError("bad")) is None
    # a named domain `LookupError` is a 404, whichever subclass it is
    for named in (UserMissing("no user"), _NoSuchDraft("profile_drafts 7")):
        problem = domain_problem(named)
        assert problem is not None and (problem.status, problem.code) == (404, "not_found"), named


async def test_a_raised_refusal_is_answered_with_its_problem_document() -> None:
    refusal = Refusal(
        422,
        "validation_failed",
        "Profile validation failed",
        detail="see errors",
        fields=[RefusalField("body.expected_rescore_ack", "ack_required", "acknowledge the estimate")],
    )
    router = APIRouter()

    @router.get("/api/_refuse")
    async def refuse() -> None:
        raise refusal

    app = create_app(extra_routers=[router])  # no state: this response needs neither config nor database
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as c:
        r = await c.get("/api/_refuse")
    assert r.status_code == 422 and r.headers["content-type"] == "application/problem+json"
    request_id = r.headers["x-request-id"]
    expected = problem_body(problem_of(refusal), instance="/api/_refuse", request_id=request_id)
    assert r.content == JSONResponse(expected).body  # what the `Problem` handler renders, byte for byte
