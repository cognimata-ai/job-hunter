"""The extract phase of `profile_graph`, `extract_profile` (spec §9.1, §9.5, §10, §12.1, §12.3, §12.7; plan
Task 13, Deviations 7, 8, 26, 27 and 28): `check_pdf → extract_profile → validate → draft`, through the real
gateway on the fake LLM transport and the hand-authored synthetic stream of `SYNTHETIC_PROFILE`.

Every operation runs in process (`op_runner`). An approval and a retry are made as Task 14's routes will make
them (`approve_text`, `retry_draft`). Every name and datum is invented (Erika Musterfrau, Musterfirma GmbH,
example.org); no real CV, and no paid call.
"""

import asyncio
import json
from dataclasses import asdict
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db.transitions import transition
from jobhunter.graphs.profile.graph import thread_id
from jobhunter.llm import bounds, fake
from jobhunter.llm.bounds import PROMPT_OVERHEAD_TOKENS
from jobhunter.llm.gateway import NodeConfig
from jobhunter.llm.pricing import CONTEXT_WINDOW, Usage, settled_usd, worst_case_usd
from jobhunter.llm.prompts.render import extract_profile_native_pdf_prompt
from jobhunter.llm.schemas.profile import ProfileOut
from jobhunter.profile.operations import extract_phase_failed
from jobhunter.profile.upload import request_draft_operation
from jobhunter.profile.validate import validate_profile
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.faults import PausePoint
from tests.support.fixtures import count_rows
from tests.support.llm import ledger_states, open_alerts, reclaim_stale_now, save_budget, tries
from tests.support.profile import (
    EXTRACT_MODEL,
    SYNTHETIC_CV,
    SYNTHETIC_PROFILE,
    SYNTHETIC_USAGE,
    OpRunner,
    Upload,
    approve_text,
    cv_row,
    draft_row,
    encrypted_pdf,
    image_only_pdf,
    minimal_pdf,
    reopen_text_review,
    retry_draft,
    sha256,
)

pytestmark = pytest.mark.integration

# Words of the synthetic CV and of the profile the model answers: no log line or checkpoint may hold them.
CONTENT_WORDS = ("Zahlungsdienst", "Musterfirma", "Datenplattform", "erika.musterfrau")
CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")
NATIVE_PDF = minimal_pdf(["Lebenslauf", "Senior Backend-Entwicklerin", "Python, FastAPI, PostgreSQL"])
EXTRACT_STEPS = ["check_pdf", "extract_profile", "validate", "draft"]
# The seeded model policy and execution config of `extract_profile` (§10.3).
NODE = NodeConfig(
    purpose="extract_profile",
    model=EXTRACT_MODEL,
    effort="medium",
    max_tokens=16000,
    max_tokens_retry=32000,
    deadline_s=900,
    schema=ProfileOut,
)


def served() -> int:
    """Every message try that reached the fake LLM in this test (a `count_tokens` request is none)."""
    return fake.state().served


async def op_row(engine: AsyncEngine, op_id: int) -> sa.Row[Any]:
    async with engine.connect() as conn:
        statement = sa.text(
            "SELECT status, attempts, last_error_code, not_before, claim_id, xmin::text AS xmin "
            "FROM operations WHERE id = :i"
        )
        return (await conn.execute(statement, {"i": op_id})).one()


async def draft_xmin(engine: AsyncEngine, draft_id: int) -> str:
    """The transaction that last wrote the draft's row (equal for two rows written in one transaction)."""
    async with engine.connect() as conn:
        statement = sa.text("SELECT xmin::text FROM profile_drafts WHERE id = :d")
        return str((await conn.execute(statement, {"d": draft_id})).scalar_one())


async def call_row(engine: AsyncEngine, call_id: int) -> sa.Row[Any]:
    async with engine.connect() as conn:
        statement = sa.text(
            "SELECT c.purpose, c.input_refs, c.effort, c.model_requested, c.execution_config_id, "
            "c.profile_draft_id, l.purpose_class, l.state FROM llm_calls c "
            "JOIN llm_spend_ledger l ON l.id = c.spend_ledger_id WHERE c.id = :i"
        )
        return (await conn.execute(statement, {"i": call_id})).one()


async def reviewed_draft(engine: AsyncEngine, op_runner: OpRunner, upload: Upload) -> tuple[int, int]:
    """A pasted `SYNTHETIC_CV` through the text phase, approved as stripped: the draft and its operation."""
    draft_id = await upload(SYNTHETIC_CV)
    await op_runner.run_pending()
    assert (await draft_row(engine, draft_id)).status == "text_review"
    return draft_id, await approve_text(engine, draft_id)


async def native_draft(op_runner: OpRunner, upload: Upload, pdf: bytes = NATIVE_PDF) -> tuple[int, int]:
    """A native-PDF opt-in upload (§12.7): the draft, `extracting_profile` at once, and its operation."""
    draft_id = await upload(pdf, native_pdf_opt_in=True)
    [op_id] = await op_runner.due()
    return draft_id, op_id


def expected(transmitted_text: str | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The `draft_json` and `validation_issues` of the synthetic answer, validated against
    `transmitted_text`."""
    out, issues = validate_profile(
        ProfileOut.model_validate(SYNTHETIC_PROFILE), transmitted_text=transmitted_text, today=utcnow().date()
    )
    return out.model_dump(mode="json"), [asdict(issue) for issue in issues]


def codes(issues: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [(issue["path"], issue["code"]) for issue in issues]


async def assert_failed_with_the_operation(engine: AsyncEngine, draft_id: int, op_id: int, code: str) -> None:
    """The draft `failed(code)` and its operation `failed(code)`, in one transaction, without a requeue."""
    draft, op = await draft_row(engine, draft_id), await op_row(engine, op_id)
    assert (draft.status, draft.failure_code, draft.status_reason) == ("failed", code, code)
    assert (op.status, op.last_error_code, op.attempts) == ("failed", code, 0)
    assert await draft_xmin(engine, draft_id) == op.xmin
    assert draft.draft_json is None


# --- the text path ---------------------------------------------------------------------------------------


async def test_an_approved_text_reaches_ready_with_a_grounded_profile(engine, op_runner, upload) -> None:
    draft_id, op_id = await reviewed_draft(engine, op_runner, upload)
    assert await op_runner.run_pending() == [op_id]
    draft = await draft_row(engine, draft_id)
    assert (draft.status, draft.failure_code) == ("ready", None)
    profile, issues = expected(draft.reviewed_text)
    assert draft.draft_json == profile
    assert draft.validation_issues == issues
    # grounded: every evidence is in the reviewed text; the fixture plants the years figure only
    assert codes(issues) == [("total_years_experience", "years_mismatch")]
    assert bytes(draft.transmitted_text_sha256) == bytes(draft.reviewed_text_sha256)
    assert bytes(draft.reviewed_text_sha256) == sha256(draft.reviewed_text)
    op = await op_row(engine, op_id)
    assert (op.status, op.attempts, op.claim_id) == ("succeeded", 0, None)
    assert await draft_xmin(engine, draft_id) == op.xmin  # `ready` and the operation's end commit together
    (call,) = await tries(engine)
    assert draft.llm_call_id == call.call_id and call.output_json == SYNTHETIC_PROFILE
    assert served() == 1
    state = await op_runner.deps.profile_graphs.extract.aget_state(
        {"configurable": {"thread_id": thread_id("p", draft_id, op_id, 0)}}
    )
    assert state.values == {
        "draft_id": draft_id,
        "operation_id": op_id,
        "llm_call_id": call.call_id,
        "steps": EXTRACT_STEPS,
        "outcome": "ready",
    }


async def test_the_call_is_a_non_scoring_extract_profile_call_with_the_active_config(
    engine, op_runner, upload
) -> None:
    draft_id, op_id = await reviewed_draft(engine, op_runner, upload)
    await op_runner.run_pending()
    draft, cv = await draft_row(engine, draft_id), await cv_row(engine, draft_id)
    (attempt,) = await tries(engine)
    assert (attempt.status, attempt.error_code, attempt.state, attempt.logical_call_no) == (
        "completed",
        None,
        "billed",
        1,
    )
    assert (attempt.max_tokens, attempt.max_tokens_stage, attempt.transport_try) == (16000, "first", 1)
    # the logical call's cost is the try's settlement, from its own usage
    usage = Usage(SYNTHETIC_USAGE["input_tokens"], SYNTHETIC_USAGE["output_tokens"], 0, 0)
    assert attempt.cost_usd == attempt.settled_usd == settled_usd(EXTRACT_MODEL, usage)
    call = await call_row(engine, attempt.call_id)
    assert (call.purpose, call.purpose_class, call.model_requested, call.effort) == (
        "extract_profile",
        "nonscoring",
        "claude-opus-5-5",
        "medium",
    )
    async with engine.connect() as conn:
        active = (
            await conn.execute(sa.text("SELECT execution_config_id FROM active_config WHERE id = 1"))
        ).scalar_one()
    assert call.execution_config_id == active and call.profile_draft_id == draft_id
    reviewed_hex = bytes(draft.reviewed_text_sha256).hex()
    refs = dict(call.input_refs)
    owner = refs.pop("owner")
    assert (owner["kind"], owner["id"]) == ("operation", op_id)
    assert refs == {
        "profile_draft_id": draft_id,
        "cv_file_id": cv.id,
        "reviewed_text_sha256": reviewed_hex,
        "transmitted_text_sha256": reviewed_hex,
        "native_pdf": False,
        "deadline_s": 900,
    }
    (request,) = fake.state().requests
    assert (request.purpose, request.model, request.max_tokens) == ("extract_profile", EXTRACT_MODEL, 16000)
    assert request.body["output_config"]["effort"] == "medium"
    # exactly the approved text inside <cv_text>: the string whose sha256 is transmitted_text_sha256
    assert request.body["messages"] == [
        {"role": "user", "content": f"<cv_text>{draft.reviewed_text}</cv_text>"}
    ]


async def test_a_structural_failure_is_repaired_once(engine, op_runner, upload) -> None:
    fake.scenario("parse_error", "ok")
    draft_id, _ = await reviewed_draft(engine, op_runner, upload)
    await op_runner.run_pending()
    draft = await draft_row(engine, draft_id)
    assert draft.status == "ready"
    rows = await tries(engine)
    assert [(r.logical_call_no, r.status, r.error_code, r.state) for r in rows] == [
        (1, "completed", "invalid_output", "billed"),
        (2, "completed", None, "billed"),
    ]
    assert rows[0].output_json is None
    assert draft.llm_call_id == rows[1].call_id
    assert served() == 2


async def test_a_second_structural_failure_fails_the_draft(engine, op_runner, upload) -> None:
    fake.scenario("parse_error")  # the last name repeats: the repair is invalid too
    draft_id, op_id = await reviewed_draft(engine, op_runner, upload)
    await op_runner.run_pending()
    await assert_failed_with_the_operation(engine, draft_id, op_id, "invalid_output")
    assert served() == 2  # one repair, never a third call
    assert [r.logical_call_no for r in await tries(engine)] == [1, 2]
    assert await count_rows(engine, "alerts", code="operation.failed") == 1


@pytest.mark.parametrize(
    ("scenario", "code", "requests"),
    [
        ("refusal", "refused", 1),
        ("model_mismatch", "model_mismatch", 1),
        ("max_tokens", "max_tokens", 2),  # the first stage, then the enlarged one
        ("http_401", "llm_request_401", 1),
    ],
)
async def test_a_terminal_answer_fails_the_draft_without_a_repair(
    engine, op_runner, upload, scenario: fake.Scenario, code: str, requests: int
) -> None:
    fake.scenario(scenario)
    draft_id, op_id = await reviewed_draft(engine, op_runner, upload)
    await op_runner.run_pending()
    await assert_failed_with_the_operation(engine, draft_id, op_id, code)
    assert served() == requests
    assert {r.logical_call_no for r in await tries(engine)} == {1}


async def test_a_cap_below_the_worst_case_fails_budget_exhausted_with_no_request(
    engine, op_runner, upload
) -> None:
    draft_id, op_id = await reviewed_draft(engine, op_runner, upload)
    # A = cap - buffer = $0.10, below the extraction's worst case (16,000 output tokens alone are $0.32)
    await save_budget(engine, now=utcnow(), cap_usd="2.10")
    await op_runner.run_pending()
    await assert_failed_with_the_operation(engine, draft_id, op_id, "budget_exhausted")
    assert served() == 0
    assert await count_rows(engine, "llm_calls") == 0 and await count_rows(engine, "llm_spend_ledger") == 0


async def test_an_overloaded_api_requeues_the_operation(engine, op_runner, upload) -> None:
    fake.scenario("overloaded_529")
    draft_id, op_id = await reviewed_draft(engine, op_runner, upload)
    before = utcnow()
    await op_runner.run_one(op_id)
    after = utcnow()
    assert served() == 3 and len(op_runner.sleeps.waits) == 2  # three 529 tries, two backoffs
    op = await op_row(engine, op_id)
    assert (op.status, op.attempts, op.last_error_code, op.claim_id) == ("pending", 1, "llm_transient", None)
    assert before + timedelta(minutes=5) <= op.not_before <= after + timedelta(minutes=5)
    draft = await draft_row(engine, draft_id)
    assert (draft.status, draft.failure_code) == ("extracting_profile", None)  # the next attempt retries
    assert [(r.state, r.error_code) for r in await tries(engine)] == [
        ("not_billed", "http_529"),
        ("not_billed", "http_529"),
        ("not_billed", "http_529"),
    ]


async def test_the_last_transient_attempt_fails_the_draft_through_the_hook(engine, op_runner, upload) -> None:
    fake.scenario("overloaded_529")
    draft_id, op_id = await reviewed_draft(engine, op_runner, upload)
    async with engine.begin() as conn:
        await conn.execute(sa.text("UPDATE operations SET attempts = 9 WHERE id = :i"), {"i": op_id})
    await op_runner.run_one(op_id)
    op = await op_row(engine, op_id)
    assert (op.status, op.last_error_code) == ("failed", "attempts_exhausted")
    draft = await draft_row(engine, draft_id)
    assert (draft.status, draft.failure_code) == ("failed", "llm_transient")  # the requeue's code
    assert await draft_xmin(engine, draft_id) == op.xmin
    # the requeue that spent the last attempt ended the phase `failed`, not `requeued`
    state = await op_runner.deps.profile_graphs.extract.aget_state(
        {"configurable": {"thread_id": thread_id("p", draft_id, op_id, 9)}}
    )
    assert (state.values["steps"], state.values["outcome"]) == (["check_pdf", "extract_profile"], "failed")


async def test_a_requeued_operation_leaves_requeued_as_the_phases_outcome(engine, op_runner, upload) -> None:
    fake.scenario("overloaded_529")
    draft_id, op_id = await reviewed_draft(engine, op_runner, upload)
    await op_runner.run_one(op_id)
    state = await op_runner.deps.profile_graphs.extract.aget_state(
        {"configurable": {"thread_id": thread_id("p", draft_id, op_id, 0)}}
    )
    assert state.values["outcome"] == "requeued"


async def test_the_hook_moves_a_draft_left_in_extracting_profile_and_tolerates_a_bad_payload(
    engine, op_runner, upload, log_events
) -> None:
    draft_id, op_id = await reviewed_draft(engine, op_runner, upload)
    async with engine.begin() as conn:
        await extract_phase_failed(conn, op_id, {"draft_id": draft_id}, None)  # after a reclaim: no code
        await extract_phase_failed(conn, op_id, {"draft_id": "seven"}, None)  # logged, nothing moved
    draft = await draft_row(engine, draft_id)
    assert (draft.status, draft.failure_code) == ("failed", "attempts_exhausted")
    async with engine.begin() as conn:
        await extract_phase_failed(conn, op_id, {"draft_id": draft_id}, "refused")  # already failed: left
    assert (await draft_row(engine, draft_id)).failure_code == "attempts_exhausted"
    [invalid] = [e for e in log_events() if e["event"] == "profile_hook_payload_invalid"]
    assert (invalid["operation_id"], invalid["error_class"]) == (op_id, "TypeError")


# --- the native-PDF path ---------------------------------------------------------------------------------


async def test_a_native_pdf_reserves_the_counted_bound_and_the_buffer_share(
    engine, op_runner, upload
) -> None:
    fake.count_tokens(1800)
    draft_id, op_id = await native_draft(op_runner, upload)
    assert await op_runner.run_pending() == [op_id]
    draft, cv = await draft_row(engine, draft_id), await cv_row(engine, draft_id)
    assert (draft.status, draft.native_pdf, draft.reviewed_text_sha256) == ("ready", True, None)
    assert draft.transmitted_text_sha256 is None  # no text was transmitted
    assert cv.page_count == 1  # `check_pdf` recorded docparse's count
    (attempt,) = await tries(engine)
    bound = 2250 + PROMPT_OVERHEAD_TOKENS[EXTRACT_MODEL]  # ceil(1.25 x 1800) + the overhead
    assert attempt.input_tokens_bound == bound
    # the seeded month row's buffer is $2.00: the native share is 0.25 x $2.00
    assert attempt.reserved_usd == worst_case_usd(
        EXTRACT_MODEL, input_tokens_bound=bound, max_tokens=16000, extra_usd=Decimal("0.50")
    )
    assert fake.state().count_tokens_requests == 1 and served() == 1
    (request,) = fake.state().requests
    assert request.body["messages"][0]["content"][0]["type"] == "document"
    # the count was made on the exact request that was sent: its body without `max_tokens` and `stream`
    counted = op_runner.deps.gateway.count_body(NODE, extract_profile_native_pdf_prompt(NATIVE_PDF))
    assert counted == {k: v for k, v in request.body.items() if k not in ("max_tokens", "stream")}
    refs = (await call_row(engine, attempt.call_id)).input_refs
    assert (refs["native_pdf"], refs["reviewed_text_sha256"], refs["transmitted_text_sha256"]) == (
        True,
        None,
        None,
    )
    # Deviation 8: no transmitted text, so every evidence is unverifiable; the contact scan still ran
    profile, issues = expected(None)
    assert draft.draft_json == profile and draft.validation_issues == issues
    assert {code for _, code in codes(issues)} == {"unverifiable", "years_mismatch"}
    assert len(op_runner.docparse_jobs) == 1


@pytest.mark.parametrize("answer", ["error", CONTEXT_WINDOW[EXTRACT_MODEL] + 1], ids=["error", "too_large"])
async def test_an_unbounded_native_pdf_fails_input_unbounded_with_nothing_sent(
    engine, op_runner, upload, answer: Any
) -> None:
    fake.count_tokens(answer)
    draft_id, op_id = await native_draft(op_runner, upload)
    await op_runner.run_pending()
    await assert_failed_with_the_operation(engine, draft_id, op_id, "input_unbounded")
    assert served() == 0 and fake.state().count_tokens_requests == 1
    assert await count_rows(engine, "llm_calls") == 0 and await count_rows(engine, "llm_spend_ledger") == 0


async def test_a_native_draft_meeting_a_disabled_native_path_sends_nothing(
    engine, op_runner, upload, monkeypatch
) -> None:
    """A native draft admitted before the flag changed ends `failed('native_pdf_disabled')`, before docparse
    and before any count."""
    fake.count_tokens(1800)
    draft_id, op_id = await native_draft(op_runner, upload)
    monkeypatch.setattr(bounds, "NATIVE_PDF_ENABLED", False)
    await op_runner.run_pending()
    await assert_failed_with_the_operation(engine, draft_id, op_id, "native_pdf_disabled")
    assert served() == 0 and fake.state().count_tokens_requests == 0
    assert op_runner.docparse_jobs == []


@pytest.mark.parametrize(
    ("pdf", "code"),
    [
        (encrypted_pdf(NATIVE_PDF), "encrypted"),
        (minimal_pdf([f"Seite {n}" for n in range(1, 22)], lines_per_page=1), "too_many_pages"),
    ],
    ids=["encrypted", "21_pages"],
)
async def test_a_native_pdf_failing_the_upload_checks_sends_no_request_of_any_kind(
    engine, op_runner, upload, pdf: bytes, code: str
) -> None:
    """§12.1, Deviation 7: docparse checks encryption and pages (`check_pdf`), before any token count."""
    fake.count_tokens(1800)
    draft_id, op_id = await native_draft(op_runner, upload, pdf)
    await op_runner.run_pending()
    await assert_failed_with_the_operation(engine, draft_id, op_id, code)
    assert served() == 0 and fake.state().count_tokens_requests == 0  # count_tokens included
    assert len(op_runner.docparse_jobs) == 1
    assert await count_rows(engine, "llm_calls") == 0


# --- obsolete operations ---------------------------------------------------------------------------------


async def _text_review_draft(engine: AsyncEngine, op_runner: OpRunner, upload: Upload) -> int:
    draft_id = await upload(SYNTHETIC_CV)
    await op_runner.run_pending()
    return draft_id


async def _failed_draft(engine: AsyncEngine, op_runner: OpRunner, upload: Upload) -> int:
    draft_id = await upload(image_only_pdf())
    await op_runner.run_pending()
    return draft_id


@pytest.mark.parametrize(
    ("make_draft", "status"), [(_text_review_draft, "text_review"), (_failed_draft, "failed")]
)
async def test_an_operation_whose_draft_is_not_extracting_profile_is_obsolete(
    engine, op_runner, upload, make_draft: Any, status: str
) -> None:
    """No extraction ever starts on text the user has not approved (§12.3)."""
    draft_id = await make_draft(engine, op_runner, upload)
    assert (await draft_row(engine, draft_id)).status == status
    before = await draft_xmin(engine, draft_id)
    async with engine.begin() as conn:
        op_id, _ = await request_draft_operation(conn, "extract_profile", draft_id, defer=None)
    await op_runner.run_one(op_id)
    op = await op_row(engine, op_id)
    assert (op.status, op.last_error_code) == ("succeeded", None)
    assert await draft_xmin(engine, draft_id) == before  # the draft is untouched
    assert served() == 0 and await count_rows(engine, "llm_calls") == 0


# --- receipts: a reclaimed execution reuses what was paid -----------------------------------------------


async def _reclaim_during(op_runner: OpRunner, engine: AsyncEngine, op_id: int, pause: PausePoint) -> None:
    """Execution 1 of `op_id` held at `pause`, the reconciler's reclaim, execution 2 to its end, then
    execution 1 resumed: its next owner write is fenced, and it writes nothing."""
    first = asyncio.create_task(op_runner.run_one(op_id, pause=pause))
    await asyncio.wait_for(pause.reached.wait(), 60)
    await reclaim_stale_now(engine, op_id)
    await op_runner.run_one(op_id)
    pause.resume()
    await asyncio.wait_for(first, 30)


async def test_a_reclaim_after_an_invalid_first_call_makes_only_the_repair(engine, op_runner, upload) -> None:
    fake.scenario("parse_error", "ok")
    draft_id, op_id = await reviewed_draft(engine, op_runner, upload)
    await _reclaim_during(op_runner, engine, op_id, PausePoint("profile.extract.after_call"))
    assert (await draft_row(engine, draft_id)).status == "ready"
    assert served() == 2  # call 1 before the crash, the repair after it
    assert [(r.logical_call_no, r.error_code) for r in await tries(engine)] == [
        (1, "invalid_output"),
        (2, None),
    ]
    op = await op_row(engine, op_id)
    assert (op.status, op.attempts) == ("succeeded", 1)


async def test_a_reclaim_after_a_valid_repair_makes_no_request(engine, op_runner, upload) -> None:
    fake.scenario("parse_error", "ok")
    draft_id, op_id = await reviewed_draft(engine, op_runner, upload)
    await _reclaim_during(op_runner, engine, op_id, PausePoint("profile.extract.after_call", arrival=2))
    draft = await draft_row(engine, draft_id)
    assert draft.status == "ready"
    assert served() == 2
    rows = await tries(engine)
    assert [r.logical_call_no for r in rows] == [1, 2] and draft.llm_call_id == rows[1].call_id


async def test_a_reclaim_after_a_refusal_fails_the_draft_without_a_request(engine, op_runner, upload) -> None:
    fake.scenario("refusal")
    draft_id, op_id = await reviewed_draft(engine, op_runner, upload)
    await _reclaim_during(op_runner, engine, op_id, PausePoint("profile.extract.after_call"))
    draft, op = await draft_row(engine, draft_id), await op_row(engine, op_id)
    assert (draft.status, draft.failure_code) == ("failed", "refused")
    assert (op.status, op.last_error_code, op.attempts) == ("failed", "refused", 1)
    assert served() == 1


async def test_a_reclaim_after_a_first_stage_max_tokens_try_resumes_at_the_enlarged_stage(
    engine, op_runner, upload
) -> None:
    """Carried ruling (Task 5) and final review FR-2: a settled `max_tokens_first_stage` try is neither
    terminal nor usable, and its paid first stage is known to be too small. Execution 1 is held after the
    enlarged stage's reservation, between the first stage's settlement and the enlarged try; execution 2 makes
    logical call 1 again from its enlarged stage (`max_tokens` 32,000), never sends the first stage again,
    and reaches `ready`; execution 1's enlarged try is fenced at its dispatch marker and never sent."""
    fake.scenario("max_tokens", "ok")
    draft_id, op_id = await reviewed_draft(engine, op_runner, upload)
    await _reclaim_during(op_runner, engine, op_id, PausePoint("gateway.after_reserve", arrival=2))
    draft = await draft_row(engine, draft_id)
    assert (draft.status, draft.failure_code) == ("ready", None)
    assert [r.max_tokens for r in fake.state().requests] == [NODE.max_tokens, NODE.max_tokens_retry]
    rows = await tries(engine)
    assert [(r.logical_call_no, r.max_tokens_stage, r.max_tokens, r.error_code) for r in rows] == [
        (1, "first", 16000, "max_tokens_first_stage"),
        (1, "enlarged", 32000, "never_dispatched"),
        (1, "enlarged", 32000, None),
    ]
    assert await ledger_states(engine, op_id) == ["billed", "not_billed:never_dispatched", "billed"]
    assert draft.llm_call_id == rows[2].call_id


async def test_a_requeue_after_a_transient_enlarged_stage_resumes_at_the_enlarged_stage(
    engine, op_runner, upload
) -> None:
    """FR-2, the other way to the same receipt: the enlarged stage's three tries are overloaded and the
    operation is requeued. The next attempt starts at the enlarged stage with three tries of its own, and the
    first stage is sent once in all."""
    fake.scenario("max_tokens", "overloaded_529", "overloaded_529", "overloaded_529", "ok")
    draft_id, op_id = await reviewed_draft(engine, op_runner, upload)
    await op_runner.run_one(op_id)
    assert (await op_row(engine, op_id)).last_error_code == "llm_transient"
    async with engine.begin() as conn:  # the requeue's five minutes have passed
        await conn.execute(sa.text("UPDATE operations SET not_before = now() WHERE id = :i"), {"i": op_id})
    await op_runner.run_one(op_id)
    assert (await draft_row(engine, draft_id)).status == "ready"
    assert [r.max_tokens for r in fake.state().requests] == [16000, 32000, 32000, 32000, 32000]
    rows = await tries(engine)
    assert [(r.max_tokens_stage, r.transport_try, r.error_code) for r in rows] == [
        ("first", 1, "max_tokens_first_stage"),
        ("enlarged", 1, "http_529"),
        ("enlarged", 2, "http_529"),
        ("enlarged", 3, "http_529"),
        ("enlarged", 1, None),
    ]


async def test_crash_after_settlement_reuses_the_paid_output(engine, op_runner, upload) -> None:
    draft_id, op_id = await reviewed_draft(engine, op_runner, upload)
    pause = PausePoint("profile.extract.after_call")
    first = asyncio.create_task(op_runner.run_one(op_id, pause=pause))
    await asyncio.wait_for(pause.reached.wait(), 60)
    await reclaim_stale_now(engine, op_id)
    await op_runner.run_one(op_id)
    draft = await draft_row(engine, draft_id)
    assert draft.status == "ready"
    assert served() == 1 and await ledger_states(engine, op_id) == ["billed"]
    written = await draft_xmin(engine, draft_id)
    pause.resume()
    await asyncio.wait_for(first, 30)
    assert await draft_xmin(engine, draft_id) == written  # the first execution wrote nothing
    second = await op_runner.deps.profile_graphs.extract.aget_state(
        {"configurable": {"thread_id": thread_id("p", draft_id, op_id, 1)}}
    )
    assert second.values["steps"] == EXTRACT_STEPS


async def test_a_retry_after_invalid_output_is_a_new_operation_and_a_new_call(
    engine, op_runner, upload
) -> None:
    fake.scenario("parse_error")
    draft_id, first_op = await reviewed_draft(engine, op_runner, upload)
    await op_runner.run_pending()
    assert (await draft_row(engine, draft_id)).failure_code == "invalid_output"
    fake.scenario("ok")
    second_op = await retry_draft(engine, draft_id)
    assert second_op != first_op
    assert await op_runner.run_pending() == [second_op]
    draft = await draft_row(engine, draft_id)
    # the retried draft carries no trace of its earlier failure once it is ready
    assert (draft.status, draft.failure_code, draft.status_reason) == ("ready", None, None)
    rows = await tries(engine)
    assert [r.logical_call_no for r in rows] == [
        1,
        2,
        1,
    ]  # the old operation's receipts are not the new one's
    assert served() == 3 and draft.llm_call_id == rows[2].call_id
    owner = (await call_row(engine, rows[2].call_id)).input_refs["owner"]
    assert owner["id"] == second_op


async def test_the_ready_write_clears_a_stale_failure_code_and_reason(engine, op_runner, upload) -> None:
    """Even a draft reopened without clearing them (a route that forgot) is `ready` with no failure trace."""
    fake.scenario("refusal")
    draft_id, _ = await reviewed_draft(engine, op_runner, upload)
    await op_runner.run_pending()
    assert (await draft_row(engine, draft_id)).failure_code == "refused"
    fake.scenario("ok")
    async with engine.begin() as conn:
        await transition(conn, "profile_drafts", draft_id, ["failed"], "extracting_profile")
        op_id, _ = await request_draft_operation(conn, "extract_profile", draft_id, defer=None)
    stale = await draft_row(engine, draft_id)
    assert (stale.failure_code, stale.status_reason) == ("refused", "refused")
    assert await op_runner.run_pending() == [op_id]
    draft = await draft_row(engine, draft_id)
    assert (draft.status, draft.failure_code, draft.status_reason) == ("ready", None, None)


async def test_old_extraction_cannot_publish_into_a_new_approval(
    engine, op_runner, upload, log_events
) -> None:
    draft_id, op_a = await reviewed_draft(engine, op_runner, upload)
    pause = PausePoint("profile.draft.before_tx")
    first = asyncio.create_task(op_runner.run_one(op_a, pause=pause))  # a fresh registry: another process
    await asyncio.wait_for(pause.reached.wait(), 60)
    await reclaim_stale_now(engine, op_a)
    await op_runner.run_one(op_a)  # execution 2 reuses A's paid output and finishes A
    draft = await draft_row(engine, draft_id)
    assert draft.status == "ready" and (await op_row(engine, op_a)).status == "succeeded"
    # the user edits and approves new text: operation B, a call made with that text
    await reopen_text_review(engine, draft_id)
    new_text = draft.reviewed_text.replace(", Docker", "")
    op_b = await approve_text(engine, draft_id, new_text)
    await op_runner.run_one(op_b)
    published = await draft_xmin(engine, draft_id)
    pause.resume()
    # execution 1's draft transaction is fenced by the claim (`FencedOut` at `lock_operation_owner`, A is
    # finished); the generation predicate is pinned by test_a_write_for_another_generation_writes_nothing
    await asyncio.wait_for(first, 30)
    assert await draft_xmin(engine, draft_id) == published
    draft = await draft_row(engine, draft_id)
    assert draft.status == "ready"
    rows = await tries(engine)
    assert served() == 2 and len(rows) == 2
    b_call = await call_row(engine, rows[1].call_id)
    assert b_call.input_refs["owner"]["id"] == op_b
    assert draft.llm_call_id == rows[1].call_id
    assert bytes(draft.transmitted_text_sha256) == sha256(new_text)
    # B's validation, against the new text: the Docker evidence is no longer in it
    assert ("skills[5].evidence", "ungrounded") in codes(draft.validation_issues)
    assert draft.validation_issues == expected(new_text)[1]
    fenced = [e for e in log_events() if e["event"] == "execution_lost"]
    assert len(fenced) == 1  # execution 1 ended as lost, with no owner write


async def _change_generation(engine: AsyncEngine, draft_id: int) -> None:
    """A new text generation under the live operation, a reordering no route allows: what the owner
    predicate of every extract-phase write must withstand (ruling: the native draft's own predicate is
    `native_pdf` alone, since its NULL `reviewed_text_sha256` never compares equal)."""
    text = "Ein anderer Lebenslauf: " + "Backend-Entwicklung mit Python. " * 12
    async with engine.begin() as conn:
        await conn.execute(
            sa.text(
                "UPDATE profile_drafts SET native_pdf = false, reviewed_text = :t, reviewed_text_sha256 = :h "
                "WHERE id = :d AND status = 'extracting_profile'"
            ),
            {"t": text, "h": sha256(text), "d": draft_id},
        )


@pytest.mark.parametrize("path", ["text", "native"])
@pytest.mark.parametrize(
    ("scenario", "point", "last_step"),
    [
        ("ok", "profile.draft.before_tx", "draft"),
        ("refusal", "profile.extract.after_call", "extract_profile"),
        ("ok", "profile.extract.after_call", "validate"),
    ],
    ids=["ready_write", "failed_write", "validate_gate"],
)
async def test_a_write_for_another_generation_writes_nothing(
    engine, op_runner, upload, log_events, path: str, scenario: fake.Scenario, point: str, last_step: str
) -> None:
    """Ruling 1 (carried from Task 1): a text draft's writes are guarded by `{"reviewed_text_sha256": <the
    call's>, "native_pdf": False}`, a native draft's by `{"native_pdf": True}`. A stale execution's `ready`
    or `failed(code)` write, or the `validate` node's gate, meets another generation: nothing moves, the
    operation ends obsolete, and the draft, left in `extracting_profile` with nothing to drive it, is
    reported: a warning and the warn alert `profile.extract_generation_mismatch`, ids only."""
    fake.scenario(scenario)
    fake.count_tokens(1800)
    if path == "text":
        draft_id, op_id = await reviewed_draft(engine, op_runner, upload)
    else:
        draft_id, op_id = await native_draft(op_runner, upload)
    pause = PausePoint(point)
    execution = asyncio.create_task(op_runner.run_one(op_id, pause=pause))
    await asyncio.wait_for(pause.reached.wait(), 60)
    await _change_generation(engine, draft_id)
    changed = await draft_xmin(engine, draft_id)
    pause.resume()
    await asyncio.wait_for(execution, 30)
    draft = await draft_row(engine, draft_id)
    assert (draft.status, draft.failure_code, draft.draft_json) == ("extracting_profile", None, None)
    assert await draft_xmin(engine, draft_id) == changed
    op = await op_row(engine, op_id)
    assert (op.status, op.last_error_code) == ("succeeded", None)
    [obsolete] = [e for e in log_events() if e["event"] == "profile_operation_obsolete"]
    assert (obsolete["operation_id"], obsolete["draft_id"]) == (op_id, draft_id)
    # the node that met the other generation is the one that ended the phase
    state = await op_runner.deps.profile_graphs.extract.aget_state(
        {"configurable": {"thread_id": thread_id("p", draft_id, op_id, 0)}}
    )
    assert (state.values["steps"][-1], state.values["outcome"]) == (last_step, "obsolete")
    [mismatch] = [e for e in log_events() if e["event"] == "profile_extract_generation_mismatch"]
    assert (mismatch["level"], mismatch["operation_id"], mismatch["draft_id"]) == ("warning", op_id, draft_id)
    assert await open_alerts(engine, "profile.extract_generation_mismatch") == [
        (f"profile.extract_generation_mismatch:profile_draft:{draft_id}", "warn", 1)
    ]
    assert await alert_context(engine, "profile.extract_generation_mismatch") == {
        "operation_id": op_id,
        "draft_id": draft_id,
    }


async def test_a_draft_that_left_the_phase_ends_the_operation_quietly(engine, op_runner, upload) -> None:
    """The obsolete end of a draft that left `extracting_profile` (here: failed by another writer under the
    paused execution) is the ordinary one: no alert, and the draft keeps the other writer's end."""
    draft_id, op_id = await reviewed_draft(engine, op_runner, upload)
    pause = PausePoint("profile.draft.before_tx")
    execution = asyncio.create_task(op_runner.run_one(op_id, pause=pause))
    await asyncio.wait_for(pause.reached.wait(), 60)
    async with engine.begin() as conn:
        await transition(
            conn,
            "profile_drafts",
            draft_id,
            ["extracting_profile"],
            "failed",
            reason="elsewhere",
            set_={"failure_code": "elsewhere"},
        )
    pause.resume()
    await asyncio.wait_for(execution, 30)
    draft = await draft_row(engine, draft_id)
    assert (draft.status, draft.failure_code, draft.draft_json) == ("failed", "elsewhere", None)
    assert (await op_row(engine, op_id)).status == "succeeded"
    assert await open_alerts(engine, "profile.extract_generation_mismatch") == []


async def alert_context(engine: AsyncEngine, code: str) -> dict[str, Any]:
    async with engine.connect() as conn:
        statement = sa.text("SELECT context FROM alerts WHERE code = :c AND resolved_at IS NULL")
        return dict((await conn.execute(statement, {"c": code})).scalar_one())


# --- the sha256 guard ------------------------------------------------------------------------------------


@pytest.mark.parametrize("corrupt", ["reviewed_text", "reviewed_text_sha256"])
async def test_a_reviewed_text_that_does_not_match_its_sha256_is_never_sent(
    engine, op_runner, upload, corrupt: str
) -> None:
    """§9.5: the model receives exactly the approved text. A `reviewed_text` that no longer hashes to
    `reviewed_text_sha256` is a bug (`InvariantViolation`, as `NodeFailed`): nothing is reserved or sent, and
    the operation is requeued as `bug`."""
    draft_id, op_id = await reviewed_draft(engine, op_runner, upload)
    column = {"reviewed_text": "reviewed_text || ' (changed)'", "reviewed_text_sha256": "sha256('other')"}
    async with engine.begin() as conn:
        await conn.execute(
            sa.text(f"UPDATE profile_drafts SET {corrupt} = {column[corrupt]} WHERE id = :d"),  # noqa: S608
            {"d": draft_id},
        )
    await op_runner.run_one(op_id)
    assert served() == 0 and fake.state().count_tokens_requests == 0
    assert await count_rows(engine, "llm_calls") == 0 and await count_rows(engine, "llm_spend_ledger") == 0
    op = await op_row(engine, op_id)
    assert (op.status, op.last_error_code, op.attempts) == ("pending", "bug", 1)
    draft = await draft_row(engine, draft_id)
    assert (draft.status, draft.failure_code, draft.draft_json) == ("extracting_profile", None, None)


# --- no content outside the content tables ---------------------------------------------------------------


async def test_the_extract_phase_keeps_cv_text_and_output_out_of_logs_and_checkpoints(
    engine, op_runner, upload, log_events
) -> None:
    fake.scenario("parse_error", "ok")
    draft_id, _ = await reviewed_draft(engine, op_runner, upload)
    await op_runner.run_pending()
    assert (await draft_row(engine, draft_id)).status == "ready"
    async with engine.connect() as conn:
        blobs: list[str] = []
        for table in CHECKPOINT_TABLES:
            rows = await conn.execute(sa.text(f"SELECT * FROM {table}"))  # noqa: S608 - constant names
            blobs.extend(json.dumps(list(row), default=repr, ensure_ascii=False) for row in rows)
    assert blobs, "the scan must see the threads' checkpoints"
    rendered = json.dumps(log_events(), ensure_ascii=False)
    for word in CONTENT_WORDS:
        assert all(word not in blob for blob in blobs), word
        assert word not in rendered, word
