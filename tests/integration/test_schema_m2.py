"""The M2 schema (migration 0006): tables, foreign keys, CHECKs, indexes and the five new machines.

The five foreign keys M2 staged (Deviation 1) are closed by M3's migration 0008; the cases that write those
columns name real M3 rows.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
import sqlalchemy as sa
from jobhunter.db.sql import CHECK_VIOLATION, FOREIGN_KEY_VIOLATION, UNIQUE_VIOLATION, PgErrorInfo, pg_error
from jobhunter.db.transitions import transition
from jobhunter.errors import TransitionConflict
from sqlalchemy.ext.asyncio import AsyncConnection

from tests.support.m2 import (
    digest,
    insert_cv_file,
    insert_eval_run,
    insert_llm_call,
    insert_profile_chain,
    insert_proposal,
    insert_vector,
)
from tests.support.m3 import insert_attempt, insert_bulk_operation, insert_job, insert_target
from tests.support.schema import FOREIGN_KEY_COLUMNS, refused, refused_check

pytestmark = pytest.mark.integration
M2_TABLES = {
    "llm_tuples",
    "cv_files",
    "profile_drafts",
    "profile_versions",
    "query_proposals",
    "profile_vectors",
    "llm_budget",
    "llm_spend_ledger",
    "llm_calls",
    "eval_runs",
}


# M2 Deviation 1: the five columns whose referenced table is M3's, with the spec's delete action ('a' NO
# ACTION, 'n' SET NULL, 'c' CASCADE). Migration 0008 added each foreign key, so none is staged any more.
DEVIATION_1_FOREIGN_KEYS = {
    "llm_calls.attempt_id": "a",
    "llm_calls.target_id": "a",
    "llm_calls.golden_case_id": "c",
    "llm_calls.batch_item_id": "a",
    "llm_spend_ledger.bulk_operation_id": "n",
}
STAGED_FOREIGN_KEYS: set[str] = set()


async def test_m2_tables_and_staged_fks(engine) -> None:
    async with engine.connect() as conn:
        tables = sa.text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        names = set((await conn.execute(tables)).scalars())
        assert M2_TABLES <= names
        assert {"score_targets", "score_attempts", "bulk_operations", "golden_cases", "batch_items"} <= names
        fks = {name for name, _ in await conn.execute(sa.text(FOREIGN_KEY_COLUMNS))}
        assert {
            "active_config.profile_version_id",
            "active_config.llm_tuple_id",
            "runs.llm_tuple_id",
            "queries.proposal_id",
            "profile_drafts.llm_call_id",
            "query_proposals.llm_call_id",
            "llm_calls.eval_run_id",
        } <= fks
        assert {name for name in DEVIATION_1_FOREIGN_KEYS if name not in fks} == STAGED_FOREIGN_KEYS


async def test_staged_foreign_keys_have_the_spec_delete_actions(engine) -> None:
    expected = {
        "active_config.profile_version_id": "a",
        "active_config.llm_tuple_id": "a",
        "runs.llm_tuple_id": "a",
        "queries.proposal_id": "n",
        "profile_drafts.llm_call_id": "n",
        "query_proposals.llm_call_id": "n",
        "llm_calls.eval_run_id": "c",
        "llm_calls.profile_draft_id": "n",
        "profile_vectors.profile_version_id": "c",
        "profile_versions.cv_file_id": "n",
        **DEVIATION_1_FOREIGN_KEYS,
    }
    async with engine.connect() as conn:
        actions = {name: action for name, action in await conn.execute(sa.text(FOREIGN_KEY_COLUMNS))}
    assert {name: actions[name] for name in expected} == expected


async def test_the_draft_and_version_foreign_keys_are_deferrable(engine) -> None:
    async with engine.connect() as conn:
        rows = await conn.execute(
            sa.text(
                "SELECT CAST(conrelid AS regclass), CAST(confrelid AS regclass), condeferrable, condeferred "
                "FROM pg_constraint WHERE contype = 'f' AND "
                "((conrelid = 'profile_drafts'::regclass AND confrelid = 'profile_versions'::regclass) OR "
                "(conrelid = 'profile_versions'::regclass AND confrelid = 'profile_drafts'::regclass))"
            )
        )
        found = sorted((str(a), str(b), d, i) for a, b, d, i in rows)
    assert found == [
        ("profile_drafts", "profile_versions", True, True),
        ("profile_versions", "profile_drafts", True, True),
    ]


# One valid ledger row per case, then the case's overrides.
LEDGER = (
    "INSERT INTO llm_spend_ledger (budget_month, kind, purpose, eval_run, purpose_class, model_requested, "
    "state, reserved_usd, settled_usd, bulk_operation_id, admitted_cap_usd, counted_before_usd, evidence) "
    "VALUES ('2026-10-01', :kind, :purpose, :eval_run, :cls, 'claude-opus-5-5', :state, :reserved, :settled, "
    ":bulk, 100, 0, :evidence)"
)
LEDGER_BASE = {
    "kind": "request",
    "purpose": "extract_profile",
    "eval_run": False,
    "cls": "nonscoring",
    "state": "reserved",
    "reserved": 1,
    "settled": None,
    "bulk": None,
    "evidence": None,
}
BUDGET_ROW = (
    "INSERT INTO llm_budget (month, cap_usd, overrun_buffer_usd, bucket_usd, bucket_at) "
    "VALUES ('2026-10-01', 100, 2, 1, now())"
)
# A case's `bulk` that stands for a real `bulk_operations` row, inserted by `real_bulk` (M3 closed the FK).
BULK = object()


async def real_bulk(c: AsyncConnection, overrides: dict[str, object]) -> dict[str, object]:
    """`overrides` with a `BULK` placeholder replaced by the id of a fresh active bulk operation."""
    if overrides.get("bulk") is not BULK:
        return overrides
    bulk = await insert_bulk_operation(c, (await insert_profile_chain(c)).llm_tuple_id)
    return {**overrides, "bulk": bulk}


@pytest.mark.parametrize(
    "overrides",
    [
        {"state": "not_billed", "settled": 0, "evidence": "never_dispatched"},
        {"state": "not_billed", "settled": 0, "evidence": "batch_item_expired"},
        # the usual billed row needs no evidence (Deviation 30 refuses only a NULL on a bound violation)
        {"state": "billed", "settled": Decimal("0.4")},
        {"state": "billed", "settled": 1},
        {"state": "ambiguous"},
        # a billed row above its reservation is allowed only with the violation's own evidence
        {"state": "billed", "settled": 2, "evidence": "reservation_bound_violated"},
        # a scoring row of a bulk operation (the foreign key M3 closed names a real row)
        {"purpose": "generate", "cls": "scoring", "bulk": BULK},
        {"kind": "adjustment", "state": None},
    ],
    ids=[
        "not_billed",
        "batch_expired",
        "billed_within_reservation",
        "billed_at_reservation",
        "ambiguous",
        "bound_violated",
        "bulk_operation",
        "adjustment",
    ],
)
async def test_ledger_rows_the_spec_allows(engine, overrides) -> None:
    async with engine.connect() as c:
        await c.execute(sa.text(BUDGET_ROW))
        await c.execute(sa.text(LEDGER), {**LEDGER_BASE, **await real_bulk(c, overrides)})


@pytest.mark.parametrize(
    ("overrides", "fragments"),
    [
        ({"state": "not_billed", "settled": 0, "evidence": "whatever"}, ("never_dispatched",)),
        # Deviation 30: a NULL evidence is refused where the spec CHECK would pass it (NULL is not false)
        (
            {"state": "not_billed", "settled": 0, "evidence": None},
            ("never_dispatched", "evidence IS NOT NULL"),
        ),
        (
            {"state": "billed", "settled": 2, "evidence": None},
            ("settled_usd <= reserved_usd", "reservation_bound_violated"),
        ),
        (
            {"state": "not_billed", "settled": Decimal("0.5"), "evidence": "never_dispatched"},
            ("settled_usd", "never_dispatched"),
        ),
        ({"state": "billed", "settled": None}, ("settled_usd IS NOT NULL",)),
        (
            {"state": "billed", "settled": 2, "evidence": "other"},
            ("settled_usd <= reserved_usd", "reservation_bound_violated"),
        ),
        # purpose 'extract_profile' cannot be booked as scoring
        ({"cls": "scoring"}, ("'critic'", "purpose_class")),
        ({"purpose": "generate", "cls": "scoring", "eval_run": True}, ("eval_run", "purpose_class")),
        ({"bulk": BULK}, ("bulk_operation_id IS NULL",)),
        ({"state": None}, ("kind", "state IS NOT NULL")),
        ({"kind": "adjustment"}, ("kind", "state IS NOT NULL")),
    ],
    ids=[
        "unlisted_evidence",
        "not_billed_without_evidence",
        "billed_over_reservation_without_evidence",
        "charged_not_billed",
        "billed_without_settlement",
        "billed_over_reservation",
        "extract_as_scoring",
        "eval_run_as_scoring",
        "bulk_on_nonscoring",
        "request_without_state",
        "adjustment_with_state",
    ],
)
async def test_ledger_checks(engine, overrides, fragments) -> None:
    async with engine.connect() as c:
        await c.execute(sa.text(BUDGET_ROW))
        await c.execute(sa.text(LEDGER), LEDGER_BASE)  # the base row is valid: only the override is refused
        refusal = {**LEDGER_BASE, **await real_bulk(c, overrides)}
        await refused_check(c, "llm_spend_ledger", LEDGER, refusal, *fragments)


BUDGET = (
    "INSERT INTO llm_budget (month, cap_usd, overrun_buffer_usd, counted_usd, scoring_counted_usd, "
    "nonscoring_counted_usd, bucket_usd, bucket_at, cap_lowered_at, counted_at_cap_change_usd) "
    "VALUES (:month, 100, 2, :counted, :scoring, :nonscoring, 1, now(), :lowered, :at_change)"
)
BUDGET_BASE = {
    "month": date(2026, 10, 1),
    "counted": 3,
    "scoring": 1,
    "nonscoring": 2,
    "lowered": None,
    "at_change": None,
}


@pytest.mark.parametrize(
    ("overrides", "fragments"),
    [
        ({"counted": 4}, ("counted_usd", "scoring_counted_usd")),
        ({"month": date(2026, 10, 2)}, ("day",)),
        ({"lowered": datetime(2026, 10, 5, tzinfo=UTC)}, ("cap_lowered_at", "counted_at_cap_change_usd")),
        ({"at_change": 3}, ("cap_lowered_at", "counted_at_cap_change_usd")),
    ],
    ids=["counted_is_the_sum", "first_of_the_month", "lowered_needs_figure", "figure_needs_lowered"],
)
async def test_budget_checks(engine, overrides, fragments) -> None:
    async with engine.connect() as c:
        await c.execute(sa.text(BUDGET), BUDGET_BASE)  # valid: only the override is refused
        refusal = {**BUDGET_BASE, "month": date(2026, 11, 1), **overrides}
        await refused_check(c, "llm_budget", BUDGET, refusal, *fragments)


CALL = (
    "INSERT INTO llm_calls (purpose, attempt_id, target_id, logical_call_no, eval_run_id, "
    "execution_config_id, model_requested, effort, max_tokens, transport, prompt_template_version, "
    "rendered_prompt_sha256, input_refs, input_tokens_bound, status, stop_reason, output_tokens, "
    "spend_ledger_id) VALUES (:purpose, :attempt, :target, :logical, :eval_run, "
    "(SELECT execution_config_id FROM active_config), :model, 'medium', :max_tokens, 'stream', 'v1', "
    "'\\x00', '{}', 1000, :status, :stop, :out, :ledger)"
)
CALL_BASE = {
    "purpose": "extract_profile",
    "attempt": None,
    "target": None,
    "logical": None,
    "eval_run": None,
    "model": "claude-opus-5-5",
    "max_tokens": 16000,
    "status": "reserved",
    "stop": None,
    "out": None,
}


async def new_ledger(c: AsyncConnection, purpose: str = "extract_profile") -> int:
    """One more `reserved` non-scoring ledger row; every call needs a ledger row of its own."""
    await c.execute(sa.text(BUDGET_ROW + " ON CONFLICT DO NOTHING"))
    params = {**LEDGER_BASE, "purpose": purpose}
    return int((await c.execute(sa.text(LEDGER + " RETURNING id"), params)).scalar_one())


async def test_llm_call_rows_the_spec_allows(engine) -> None:
    async with engine.connect() as c:
        eval_run = await insert_eval_run(c)
        target = await insert_target(c, await insert_job(c), (await insert_profile_chain(c)).llm_tuple_id)
        attempt = await insert_attempt(c, target)
        for overrides in (
            {},
            {"status": "completed", "stop": "end_turn", "out": 12},
            # an eval run owns its calls (no attempt, no target)
            {"purpose": "critic", "eval_run": eval_run},
            # a scoring call of its attempt and target (the foreign keys M3 closed name real rows)
            {"purpose": "generate", "attempt": attempt, "target": target, "logical": 1},
        ):
            params = {**CALL_BASE, **overrides, "ledger": await new_ledger(c)}
            await c.execute(sa.text(CALL), params)


@pytest.mark.parametrize(
    ("overrides", "fragments"),
    [
        ({"purpose": "generate"}, ("eval_run_id", "attempt_id", "target_id")),
        # the CHECK refuses the row before its foreign keys are checked (they run after the insert)
        ({"purpose": "generate", "attempt": 77, "target": 88}, ("logical_call_no", "transport")),
        ({"status": "completed", "out": 12}, ("stop_reason", "output_tokens")),
        ({"status": "completed", "stop": "end_turn"}, ("stop_reason", "output_tokens")),
        ({"model": "claude-haiku-4"}, ("claude-fable-5-1",)),
        ({"max_tokens": 255}, ("64000",)),
        ({"status": "running"}, ("cancelled",)),
    ],
    ids=[
        "scoring_call_without_owner",
        "scoring_call_without_logical_call_no",
        "completed_without_stop_reason",
        "completed_without_output_tokens",
        "unknown_model",
        "max_tokens_below_floor",
        "unknown_status",
    ],
)
async def test_llm_call_checks(engine, overrides, fragments) -> None:
    async with engine.connect() as c:
        await c.execute(sa.text(CALL), {**CALL_BASE, "ledger": await new_ledger(c)})  # valid
        ledger = await new_ledger(c)
        await refused_check(c, "llm_calls", CALL, {**CALL_BASE, **overrides, "ledger": ledger}, *fragments)


async def test_a_ledger_row_has_one_call(engine) -> None:
    async with engine.connect() as c:
        ledger = await new_ledger(c)
        await c.execute(sa.text(CALL), {**CALL_BASE, "ledger": ledger})
        info = await refused(c, CALL, {**CALL_BASE, "ledger": ledger})
        assert info == PgErrorInfo(UNIQUE_VIOLATION, "llm_calls_spend_ledger_id_key")


CV = (
    "INSERT INTO cv_files (sha256, mime, size_bytes, stripped_text, purged_at, extraction_method, "
    "native_pdf_opt_in) VALUES ('\\x01', :mime, :size, :text, :purged, :method, :opt_in)"
)
CV_BASE = {
    "mime": "application/pdf",
    "size": 10,
    "text": None,
    "purged": None,
    "method": None,
    "opt_in": False,
}
DRAFT = (
    "INSERT INTO profile_drafts (cv_file_id, status, reviewed_text_sha256, native_pdf, draft_json, "
    "failure_code, confirm_response, confirmed_profile_version_id) "
    "VALUES (:cv, :status, :sha, :native, CAST(:draft AS jsonb), :failure, CAST(:confirm AS jsonb), :version)"
)
DRAFT_BASE = {
    "status": "uploaded",
    "sha": None,
    "native": False,
    "draft": None,
    "failure": None,
    "confirm": None,
    "version": None,
}
VERSION = (
    "INSERT INTO profile_versions (version_no, draft_id, cv_file_id, profile_json, profile_hash, purged_at) "
    "VALUES (:no, :draft, :cv, CAST(:json AS jsonb), :hash, :purged)"
)


@pytest.mark.parametrize(
    ("overrides", "fragments"),
    [
        ({"size": 0}, ("size_bytes",)),
        ({"size": 10_485_761}, ("size_bytes",)),
        ({"mime": "text/html"}, ("application/pdf", "text/plain")),
        ({"purged": datetime(2026, 10, 1, tzinfo=UTC), "text": "x"}, ("purged_at", "stripped_text")),
        ({"opt_in": True, "method": "pypdf"}, ("native_pdf_opt_in", "native_pdf")),
        ({"method": "tesseract"}, ("pdfplumber",)),
    ],
    ids=["empty", "over_10_MiB", "mime", "purged_with_text", "opt_in_without_native_pdf", "method"],
)
async def test_cv_file_checks(engine, overrides, fragments) -> None:
    async with engine.connect() as c:
        await c.execute(sa.text(CV), CV_BASE)
        await c.execute(sa.text(CV), {**CV_BASE, "opt_in": True, "method": "native_pdf"})
        await refused_check(c, "cv_files", CV, {**CV_BASE, **overrides}, *fragments)


@pytest.mark.parametrize(
    ("overrides", "fragments"),
    [
        # Deviation 2: confirm_response is the stored outcome of a confirm, so only a confirmed draft has it
        ({"confirm": "{}"}, ("confirm_response", "confirmed")),
        ({"status": "confirmed", "draft": "{}"}, ("confirmed_profile_version_id",)),
        ({"status": "ready", "sha": b"\x01"}, ("draft_json", "content_purged_at")),
        ({"status": "extracting_profile"}, ("reviewed_text_sha256", "native_pdf")),
        ({"status": "failed"}, ("failure_code",)),
        ({"status": "paused"}, ("text_review", "discarded")),
    ],
    ids=[
        "confirm_response_when_open",
        "confirmed_without_version",
        "ready_without_draft_json",
        "extracting_without_text_hash",
        "failed_without_code",
        "unknown_status",
    ],
)
async def test_profile_draft_checks(engine, overrides, fragments) -> None:
    async with engine.connect() as c:
        cv = await insert_cv_file(c)
        await c.execute(sa.text(DRAFT), {**DRAFT_BASE, "cv": cv})  # valid
        await c.execute(sa.text(DRAFT), {**DRAFT_BASE, "cv": cv, "status": "discarded"})
        await refused_check(c, "profile_drafts", DRAFT, {**DRAFT_BASE, "cv": cv, **overrides}, *fragments)


async def test_the_native_pdf_opt_in_satisfies_the_extraction_check(engine) -> None:
    async with engine.connect() as c:
        cv = await insert_cv_file(c)
        native = {**DRAFT_BASE, "cv": cv, "status": "extracting_profile", "native": True}
        await c.execute(sa.text(DRAFT), native)  # no reviewed text: the PDF itself is sent


@pytest.mark.parametrize(
    ("overrides", "fragments"),
    [
        ({"json": "{}", "purged": datetime(2026, 10, 1, tzinfo=UTC)}, ("purged_at", "profile_json")),
        ({"json": None}, ("purged_at", "profile_json")),
        ({"no": 0}, ("version_no",)),
    ],
    ids=["purged_keeps_content", "live_without_content", "version_zero"],
)
async def test_profile_version_checks(engine, overrides, fragments) -> None:
    new_draft = "INSERT INTO profile_drafts (cv_file_id, status) VALUES (:cv, 'discarded') RETURNING id"
    async with engine.connect() as c:
        cv = await insert_cv_file(c)
        first, second = [(await c.execute(sa.text(new_draft), {"cv": cv})).scalar_one() for _ in range(2)]
        base = {"cv": cv, "no": 1, "json": "{}", "purged": None, "hash": digest(1), "draft": first}
        await c.execute(sa.text(VERSION), base)  # valid
        refusal = {**base, "no": 2, "hash": digest(2), "draft": second, **overrides}
        await refused_check(c, "profile_versions", VERSION, refusal, *fragments)


async def test_one_open_draft_and_purged_hash_reuse(engine) -> None:
    async with engine.connect() as c:
        cv = await insert_cv_file(c)
        draft = {"cv": cv}
        open_draft = "INSERT INTO profile_drafts (cv_file_id, status) VALUES (:cv, :status)"
        await c.execute(sa.text(open_draft), {**draft, "status": "uploaded"})
        # every open state shares the one slot; a closed draft does not
        for status in ("uploaded", "extracting_text", "text_review"):
            info = await refused(c, open_draft, {**draft, "status": status})
            assert info == PgErrorInfo(UNIQUE_VIOLATION, "profile_drafts_one_open"), status
        await c.execute(sa.text(open_draft), {**draft, "status": "discarded"})
        await c.execute(sa.text(open_draft), {**draft, "status": "discarded"})

        # Codex N12: a purged version with the same profile_hash neither blocks nor absorbs a new live one
        closed = [
            (
                await c.execute(
                    sa.text(
                        "INSERT INTO profile_drafts (cv_file_id, status) "
                        "VALUES (:cv, 'discarded') RETURNING id"
                    ),
                    draft,
                )
            ).scalar_one()
            for _ in range(4)
        ]
        version = (
            "INSERT INTO profile_versions (version_no, draft_id, cv_file_id, profile_json, profile_hash, "
            "purged_at) VALUES (:no, :draft, :cv, CAST(:json AS jsonb), :hash, :purged)"
        )
        live = {"json": "{}", "purged": None}
        purged = {"json": None, "purged": datetime(2026, 10, 1, tzinfo=UTC)}
        same = digest(1)
        await c.execute(sa.text(version), {"no": 1, "draft": closed[0], "cv": cv, "hash": same, **purged})
        await c.execute(sa.text(version), {"no": 2, "draft": closed[1], "cv": cv, "hash": same, **purged})
        await c.execute(sa.text(version), {"no": 3, "draft": closed[2], "cv": cv, "hash": same, **live})
        info = await refused(c, version, {"no": 4, "draft": closed[3], "cv": cv, "hash": same, **live})
        assert info == PgErrorInfo(UNIQUE_VIOLATION, "profile_versions_live_hash")
        # a draft is confirmed into at most one version
        info = await refused(c, version, {"no": 5, "draft": closed[2], "cv": cv, "hash": digest(2), **live})
        assert info.sqlstate == UNIQUE_VIOLATION and info.constraint == "profile_versions_draft_id_key"


async def test_proposals_vectors_and_eval_runs(engine) -> None:
    async with engine.connect() as c:
        chain = await insert_profile_chain(c)
        pv = chain.profile_version_id
        proposal = "INSERT INTO query_proposals (profile_version_id, status) VALUES (:pv, :status)"
        await c.execute(sa.text(proposal), {"pv": pv, "status": "running"})
        await insert_proposal(c, pv, "ready", [{"text": "python entwickler"}])
        await refused_check(
            c, "query_proposals", proposal, {"pv": pv, "status": "ready"}, "status", "items IS NOT NULL"
        )
        await refused_check(c, "query_proposals", proposal, {"pv": pv, "status": "done"}, "purged")

        await insert_vector(c, pv, "summary")
        await insert_vector(c, pv, "title:12")
        vector = (
            "INSERT INTO profile_vectors (profile_version_id, kind, embedding_id) "
            "SELECT :pv, :kind, min(id) FROM embeddings"
        )
        for kind in ("title:", "title:x", "Summary", "title:-1"):
            await refused_check(c, "profile_vectors", vector, {"pv": pv, "kind": kind}, "summary", "title:")
        info = await refused(c, vector, {"pv": pv, "kind": "summary"})
        assert info == PgErrorInfo(UNIQUE_VIOLATION, "profile_vectors_pkey")

        await insert_eval_run(c)
        defaults = (
            await c.execute(sa.text("SELECT repetitions, cost_usd, passed, finished_at FROM eval_runs"))
        ).one()
        assert tuple(defaults) == (1, Decimal(0), None, None)
        run = (
            "INSERT INTO eval_runs (kind, route, status, repetitions) VALUES (:kind, :route, :status, :reps)"
        )
        ok = {"kind": "golden", "route": "forced_critic", "status": "running", "reps": 3}
        await c.execute(sa.text(run), ok)
        for override, fragment in (
            ({"kind": "smoke"}, "canary"),
            ({"route": "batch"}, "forced_critic"),
            ({"status": "paused"}, "completed"),
            ({"reps": 4}, "repetitions"),
        ):
            await refused_check(c, "eval_runs", run, {**ok, **override}, fragment)


async def test_deletes_follow_the_staged_foreign_keys(engine) -> None:
    async with engine.connect() as c:
        chain = await insert_profile_chain(c)
        eval_run = await insert_eval_run(c)
        _, call = await insert_llm_call(c, draft_id=chain.draft_id, eval_run_id=eval_run)
        _, other = await insert_llm_call(c, draft_id=chain.draft_id)
        proposal = await insert_proposal(c, chain.profile_version_id)
        await c.execute(sa.text("UPDATE profile_drafts SET llm_call_id = :c"), {"c": other})
        await c.execute(sa.text("UPDATE query_proposals SET llm_call_id = :c"), {"c": other})
        await c.execute(
            sa.text(
                "INSERT INTO queries (text, lang, origin, proposal_id) VALUES ('python entwickler', 'de', "
                "'llm_proposal', :p)"
            ),
            {"p": proposal},
        )

        async def scalar(statement: str, **params: object) -> object:
            return (await c.execute(sa.text(statement), params)).scalar_one()

        # an eval run takes its calls with it
        await c.execute(sa.text("DELETE FROM eval_runs WHERE id = :e"), {"e": eval_run})
        assert await scalar("SELECT count(*) FROM llm_calls WHERE id = :i", i=call) == 0
        assert await scalar("SELECT count(*) FROM llm_calls WHERE id = :i", i=other) == 1
        # a deleted call only loses the pointers to it
        await c.execute(sa.text("DELETE FROM llm_calls WHERE id = :c"), {"c": other})
        assert await scalar("SELECT llm_call_id IS NULL FROM profile_drafts") is True
        assert await scalar("SELECT llm_call_id IS NULL FROM query_proposals") is True
        # a purged proposal leaves its queries
        await c.execute(sa.text("DELETE FROM query_proposals"))
        assert await scalar("SELECT proposal_id IS NULL FROM queries") is True


async def test_the_active_config_and_run_pointers_name_real_rows(engine) -> None:
    async with engine.connect() as c:
        chain = await insert_profile_chain(c)
        point = "UPDATE active_config SET profile_version_id = :p, llm_tuple_id = :t"
        info = await refused(c, point, {"p": chain.profile_version_id, "t": -1})
        assert info == PgErrorInfo(FOREIGN_KEY_VIOLATION, "active_config_llm_tuple_id_fkey")
        info = await refused(c, point, {"p": -1, "t": chain.llm_tuple_id})
        assert info == PgErrorInfo(FOREIGN_KEY_VIOLATION, "active_config_profile_version_id_fkey")
        await c.execute(sa.text(point), {"p": chain.profile_version_id, "t": chain.llm_tuple_id})

        run = (
            "INSERT INTO runs (trigger, scheduled_slot, status, include_paid, settings_version_id, "
            "scope_version_id, llm_tuple_id) SELECT 'manual', NULL, 'queued', false, settings_version_id, "
            "scope_version_id, :t FROM active_config"
        )
        info = await refused(c, run, {"t": -1})
        assert info == PgErrorInfo(FOREIGN_KEY_VIOLATION, "runs_llm_tuple_id_fkey")
        await c.execute(sa.text(run), {"t": chain.llm_tuple_id})

        query = (
            "INSERT INTO queries (text, lang, origin, proposal_id) "
            "VALUES ('python entwickler', 'de', 'llm_proposal', :p)"
        )
        info = await refused(c, query, {"p": -1})
        assert info == PgErrorInfo(FOREIGN_KEY_VIOLATION, "queries_proposal_id_fkey")
        await c.execute(sa.text(query), {"p": await insert_proposal(c, chain.profile_version_id)})


# A row of each machine in a state, and an UPDATE of its state that the matrix does not allow.
async def _draft(c: AsyncConnection) -> int:
    cv = await insert_cv_file(c)
    return int(
        (
            await c.execute(
                sa.text(
                    "INSERT INTO profile_drafts (cv_file_id, status) VALUES (:cv, 'uploaded') RETURNING id"
                ),
                {"cv": cv},
            )
        ).scalar_one()
    )


async def _ledger(c: AsyncConnection) -> int:
    return (await insert_llm_call(c))[0]


async def _call(c: AsyncConnection) -> int:
    return (await insert_llm_call(c))[1]


async def _purged_proposal(c: AsyncConnection) -> int:
    # M5 gives `purged` its edges in (the profile purge, from every state); none leads out of it
    return await insert_proposal(c, (await insert_profile_chain(c)).profile_version_id, "purged")


@pytest.mark.parametrize(
    ("table", "make", "update", "message"),
    [
        ("profile_drafts", _draft, "SET status = 'discarded'", "uploaded -> discarded"),
        ("llm_spend_ledger", _ledger, "SET state = 'billed', settled_usd = 1", "reserved -> billed"),
        (
            "llm_calls",
            _call,
            "SET status = 'completed', stop_reason = 'end_turn', output_tokens = 1",
            "reserved -> completed",
        ),
        ("query_proposals", _purged_proposal, "SET status = 'running'", "purged -> running"),
        (
            "eval_runs",
            lambda c: insert_eval_run(c, "completed"),
            "SET status = 'running'",
            "completed -> running",
        ),
    ],
)
async def test_the_trigger_refuses_a_disallowed_update(engine, table, make, update, message) -> None:
    async with engine.connect() as c:
        row = await make(c)
        with pytest.raises(sa.exc.IntegrityError, match=f"disallowed transition {table}: {message}") as bad:
            async with c.begin_nested():
                await c.execute(sa.text(f"UPDATE {table} {update} WHERE id = :i"), {"i": row})
        assert pg_error(bad.value).sqlstate == CHECK_VIOLATION


async def _fresh(c: AsyncConnection, table: str, column: str, row: int) -> bool:
    """Whether `column` of the row was written by the last statement (the test backdates it first)."""
    statement = f"SELECT {column} > now() - interval '1 minute' FROM {table} WHERE id = :i"  # noqa: S608
    return bool((await c.execute(sa.text(statement), {"i": row})).scalar_one())


async def test_a_profile_draft_walks_its_machine_through_the_registry(engine) -> None:
    async with engine.connect() as c:
        draft = await _draft(c)
        await c.execute(sa.text("UPDATE profile_drafts SET status_changed_at = now() - interval '1 hour'"))
        assert not await _fresh(c, "profile_drafts", "status_changed_at", draft)

        async def step(
            src: str, dst: str, reason: str | None = None, set_: dict[str, object] | None = None
        ) -> None:
            await transition(c, "profile_drafts", draft, [src], dst, reason=reason, set_=set_)
            status = (await c.execute(sa.text("SELECT status FROM profile_drafts"))).scalar_one()
            assert status == dst

        await step("uploaded", "extracting_text")
        assert await _fresh(c, "profile_drafts", "status_changed_at", draft)
        await step("extracting_text", "text_review")
        reviewed = digest(7)
        await step(
            "text_review",
            "extracting_profile",
            set_={"reviewed_text": "Max Mustermann, Entwickler", "reviewed_text_sha256": reviewed},
        )
        await step(
            "extracting_profile",
            "ready",
            set_={
                "draft_json": {"skills": []},
                "validation_issues": [],
                "transmitted_text_sha256": reviewed,
                "updated_at": sa.func.now(),
            },
        )
        await step("ready", "text_review", set_={"draft_json": None})  # the user edits the reviewed text
        await step("text_review", "extracting_profile", reason="reapproved")
        await step("extracting_profile", "failed", "llm_error", {"failure_code": "llm_error"})
        await step("failed", "extracting_profile")  # retry
        await step("extracting_profile", "failed", "timeout", {"failure_code": "timeout"})
        await step("failed", "discarded", "replaced_by_new_upload")
        assert tuple(
            (await c.execute(sa.text("SELECT status_reason, draft_json IS NULL FROM profile_drafts"))).one()
        ) == ("replaced_by_new_upload", True)
        with pytest.raises(ValueError, match="disallowed transition"):
            await transition(c, "profile_drafts", draft, ["discarded"], "ready")


async def test_a_draft_write_can_be_guarded_by_the_text_it_was_made_from(engine) -> None:
    """`owner_cols` (Task 13): the extraction result lands only on the text the draft node started with."""
    async with engine.connect() as c:
        cv = await insert_cv_file(c)
        sha, other = digest(1), digest(2)
        draft = (
            await c.execute(
                sa.text(
                    "INSERT INTO profile_drafts (cv_file_id, status, reviewed_text_sha256) "
                    "VALUES (:cv, 'extracting_profile', :sha) RETURNING id"
                ),
                {"cv": cv, "sha": sha},
            )
        ).scalar_one()
        ready: dict[str, object] = {"draft_json": {"skills": []}}
        for owner in ({"reviewed_text_sha256": other}, {"native_pdf": True}):
            with pytest.raises(TransitionConflict):
                await transition(
                    c, "profile_drafts", draft, ["extracting_profile"], "ready", owner=owner, set_=ready
                )
        await transition(
            c,
            "profile_drafts",
            draft,
            ["extracting_profile"],
            "ready",
            owner={"reviewed_text_sha256": sha, "native_pdf": False},
            set_=ready,
        )


async def test_the_ledger_walks_its_machine_through_the_registry(engine) -> None:
    async with engine.connect() as c:
        ledger, _ = await insert_llm_call(c)
        row = sa.text(
            "SELECT state, evidence, settled_usd, dispatched_at IS NOT NULL, settled_at IS NOT NULL "
            "FROM llm_spend_ledger WHERE id = :i"
        )

        async def state() -> tuple[object, ...]:
            return tuple((await c.execute(row, {"i": ledger})).one())

        assert await state() == ("reserved", None, None, False, False)
        await transition(c, "llm_spend_ledger", ledger, ["reserved"], "dispatched")
        assert await state() == ("dispatched", None, None, True, False)
        await transition(c, "llm_spend_ledger", ledger, ["dispatched"], "ambiguous", reason="timed_out")
        assert await state() == ("ambiguous", "timed_out", None, True, True)
        # a late online settlement
        await transition(
            c,
            "llm_spend_ledger",
            ledger,
            ["ambiguous"],
            "billed",
            reason="late_response",
            set_={"settled_usd": Decimal("0.123457")},
        )
        assert await state() == ("billed", "late_response", Decimal("0.123457"), True, True)
        with pytest.raises(ValueError, match="disallowed transition"):
            await transition(c, "llm_spend_ledger", ledger, ["billed"], "not_billed")

        # the other settled branches
        for first, last, evidence in (
            ("reserved", "not_billed", "never_dispatched"),
            ("dispatched", "not_billed", "error_response_no_content"),
            ("ambiguous", "not_billed", "batch_item_expired"),
        ):
            other, _ = await insert_llm_call(c)
            if first != "reserved":
                await transition(c, "llm_spend_ledger", other, ["reserved"], "dispatched")
            if first == "ambiguous":
                await transition(c, "llm_spend_ledger", other, ["dispatched"], "ambiguous")
            await transition(
                c, "llm_spend_ledger", other, [first], last, reason=evidence, set_={"settled_usd": 0}
            )


async def test_a_call_walks_its_machine_through_the_registry(engine) -> None:
    async with engine.connect() as c:
        _, call = await insert_llm_call(c)
        await transition(c, "llm_calls", call, ["reserved"], "dispatched")
        await transition(c, "llm_calls", call, ["dispatched"], "ambiguous")
        # a late online settlement carries the whole response
        await transition(
            c,
            "llm_calls",
            call,
            ["ambiguous"],
            "completed",
            set_={
                "model_served": "claude-opus-5-5",
                "http_status": 200,
                "stop_reason": "end_turn",
                "provider_request_id": "req_synthetic",
                "input_tokens": 1000,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "output_tokens": 200,
                "cost_usd": Decimal("0.008000"),
                "output_json": {"skills": []},
                "first_event_at": datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
                "ended_at": datetime(2026, 10, 1, 12, 1, tzinfo=UTC),
            },
        )
        stored = (
            await c.execute(
                sa.text("SELECT status, output_json, cost_usd, ended_at FROM llm_calls WHERE id = :i"),
                {"i": call},
            )
        ).one()
        assert tuple(stored) == (
            "completed",
            {"skills": []},
            Decimal("0.008000"),
            datetime(2026, 10, 1, 12, 1, tzinfo=UTC),
        )
        _, failing = await insert_llm_call(c)
        await transition(c, "llm_calls", failing, ["reserved"], "dispatched")
        await transition(
            c,
            "llm_calls",
            failing,
            ["dispatched"],
            "error",
            set_={"http_status": 400, "error_code": "invalid_request", "refusal_category": None},
        )
        _, never_sent = await insert_llm_call(c)
        await transition(c, "llm_calls", never_sent, ["reserved"], "cancelled")
        with pytest.raises(ValueError, match="disallowed transition"):
            await transition(c, "llm_calls", never_sent, ["cancelled"], "dispatched")


async def test_proposals_and_eval_runs_walk_their_machines_through_the_registry(engine) -> None:
    async with engine.connect() as c:
        pv = (await insert_profile_chain(c)).profile_version_id
        _, call = await insert_llm_call(c, purpose="propose_queries")
        proposal = await insert_proposal(c, pv)
        await transition(
            c,
            "query_proposals",
            proposal,
            ["running"],
            "ready",
            set_={"items": [{"text": "python entwickler", "exploration": True}], "llm_call_id": call},
        )
        failed = await insert_proposal(c, pv)
        await transition(c, "query_proposals", failed, ["running"], "failed")
        with pytest.raises(ValueError, match="disallowed transition"):
            await transition(c, "query_proposals", failed, ["failed"], "ready")

        run = await insert_eval_run(c)
        await c.execute(sa.text("UPDATE eval_runs SET started_at = now() - interval '1 hour'"))
        await transition(
            c,
            "eval_runs",
            run,
            ["running"],
            "completed",
            set_={"passed": True, "metrics": {"cases": 0}, "cost_usd": Decimal("0.0123")},
        )
        assert tuple(
            (await c.execute(sa.text("SELECT status, passed, metrics, cost_usd FROM eval_runs"))).one()
        ) == ("completed", True, {"cases": 0}, Decimal("0.0123"))
        assert await _fresh(c, "eval_runs", "finished_at", run)
        crashed = await insert_eval_run(c)
        await transition(c, "eval_runs", crashed, ["running"], "failed")
        with pytest.raises(ValueError, match="disallowed transition"):
            await transition(c, "eval_runs", crashed, ["failed"], "completed")
