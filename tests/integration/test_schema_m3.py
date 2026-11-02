"""The M3 schema (migration 0008): the 16 tables and the two views, M2's staged foreign keys closed, the
columns and CHECKs beyond the spec DDL, the singletons, the indexes of Deviation 2, the six transition
triggers, the guard that opens the migration and NOT NULL on the two `active_config` pointers.
"""

import asyncio
import importlib
import re
import uuid
from decimal import Decimal

import pytest
import sqlalchemy as sa
from alembic import command
from jobhunter.db.alembic_runner import alembic_config, code_head
from jobhunter.db.sql import UNIQUE_VIOLATION, PgErrorInfo, pg_error
from sqlalchemy.ext.asyncio import AsyncConnection

from tests.support.m2 import ProfileChain, insert_eval_run, insert_profile_chain
from tests.support.m3 import (
    NOW,
    Job,
    insert_attempt,
    insert_batch_item,
    insert_bulk_operation,
    insert_cycle,
    insert_draft,
    insert_golden_case,
    insert_job,
    insert_m1_configuration,
    insert_m2_seed_rows,
    insert_published,
    insert_scoring_call,
    insert_submission,
    insert_target,
)
from tests.support.pg import pg_one, pg_rows
from tests.support.schema import FOREIGN_KEY_COLUMNS, refused, refused_check

pytestmark = pytest.mark.integration
M2_HEAD = "0007_m2_draft_text_edits"
M3_SCHEMA = "0008_m3_scoring"
M3_TABLES = {
    "score_cycles",
    "bulk_operations",
    "score_targets",
    "admission_lane_credits",
    "score_attempts",
    "score_drafts",
    "critic_notes",
    "llm_results",
    "result_evidence",
    "det_results",
    "batch_submissions",
    "batch_items",
    "user_job_actions",
    "score_feedback",
    "golden_cases",
    "eval_case_results",
}
# The foreign keys 0008 adds to an earlier table, or that reach one, with their ON DELETE action ('a' NO
# ACTION, 'n' SET NULL, 'c' CASCADE). The first five are M2's staged ones (M2 Deviation 1).
CLOSED_FKS = {
    "llm_calls.attempt_id": "a",
    "llm_calls.target_id": "a",
    "llm_calls.golden_case_id": "c",
    "llm_calls.batch_item_id": "a",
    "llm_spend_ledger.bulk_operation_id": "n",
    "score_targets.deferred_group_id": "n",
    "score_targets.bulk_operation_id": "n",
    "result_evidence.llm_result_id": "c",
    "det_results.llm_result_id": "c",
    "user_job_actions.job_id": "c",
    "score_feedback.llm_result_id": "n",
    "golden_cases.labelled_from_result_id": "n",
    "eval_case_results.golden_case_id": "c",
    "score_attempts.cycle_id": "n",
    "batch_submissions.operation_id": "n",
}
STAGED_TARGETS = {
    "llm_calls.attempt_id": "score_attempts",
    "llm_calls.target_id": "score_targets",
    "llm_calls.golden_case_id": "golden_cases",
    "llm_calls.batch_item_id": "batch_items",
    "llm_spend_ledger.bulk_operation_id": "bulk_operations",
}
# The deferrable (and initially deferred) foreign keys among the M3 tables and `llm_calls`: these five only.
DEFERRABLE = {
    "llm_results.target_id",
    "score_targets.published_result_id",
    "score_targets.resume_draft_id",
    "batch_items.llm_call_id",
    "llm_calls.batch_item_id",
}
FOREIGN_KEYS = (
    "SELECT CAST(c.conrelid AS regclass) || '.' || a.attname, CAST(c.confrelid AS regclass), "
    "c.condeferrable AND c.condeferred FROM pg_constraint c "
    "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey) WHERE c.contype = 'f'"
)


async def test_m3_tables_views_and_closed_fks(engine) -> None:
    async with engine.connect() as conn:
        tables = sa.text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        names = set((await conn.execute(tables)).scalars())
        assert names >= M3_TABLES
        views = sa.text("SELECT viewname FROM pg_views WHERE schemaname = 'public'")
        assert set((await conn.execute(views)).scalars()) >= {"current_scores", "display_scores"}
        actions = {name: action for name, action in await conn.execute(sa.text(FOREIGN_KEY_COLUMNS))}
        assert {name: actions.get(name) for name in CLOSED_FKS} == CLOSED_FKS
        keys = [
            (str(name), str(target), deferred)
            for name, target, deferred in await conn.execute(sa.text(FOREIGN_KEYS))
        ]
        assert {name: target for name, target, _ in keys if name in STAGED_TARGETS} == STAGED_TARGETS
        owners = M3_TABLES | {"llm_calls"}
        assert {name for name, _, deferred in keys if deferred and name.split(".")[0] in owners} == DEFERRABLE
        nullable = sa.text(
            "SELECT is_nullable FROM information_schema.columns WHERE table_name = 'active_config' "
            "AND column_name IN ('rubric_version_id', 'prompt_schema_version_id')"
        )
        assert list((await conn.execute(nullable)).scalars()) == ["NO", "NO"]


def test_0008_follows_0007_and_has_no_downgrade() -> None:
    module = importlib.import_module(f"jobhunter.db.migrations.versions.{M3_SCHEMA}")
    assert (module.revision, module.down_revision) == (M3_SCHEMA, M2_HEAD)
    with pytest.raises(NotImplementedError, match="expand/contract"):
        module.downgrade()


COLUMNS = (
    "SELECT data_type, numeric_precision, numeric_scale, is_nullable, column_default "
    "FROM information_schema.columns WHERE table_name = :t AND column_name = :c"
)


@pytest.mark.parametrize(
    ("table", "column", "expected"),
    [
        # Deviation 10: the submission's sums keep the ledger's six decimals
        ("batch_submissions", "reserved_usd", ("numeric", 12, 6, "NO", None)),
        ("batch_submissions", "followup_held_usd", ("numeric", 12, 6, "NO", None)),
        # M2 Carry-forward, Deviation 2: the buffer in force at admission; NULL on M2's rows
        ("llm_spend_ledger", "admitted_buffer_usd", ("numeric", 10, 2, "YES", None)),
        # Deviation 21: the markers of the adversarial golden-case rows
        ("jobs", "synthetic_eval", ("boolean", None, None, "NO", "false")),
        ("profile_versions", "synthetic_eval", ("boolean", None, None, "NO", "false")),
        # Deviation 40: the explicit parser family of the version's generator schema
        ("prompt_schema_versions", "parser_family", ("text", None, None, "NO", "'generator_v1'::text")),
        # Ruling T4-a: `json`, not the spec's `jsonb`, so the stored schemas keep their key order
        ("prompt_schema_versions", "schemas", ("json", None, None, "NO", None)),
        # Deviation 27: the item's follow-up hold and its once-only release
        ("batch_items", "hold_usd", ("numeric", 12, 6, "NO", "0")),
        ("batch_items", "hold_released_at", ("timestamp with time zone", None, None, "YES", None)),
        # Deviation 27, Codex r2 B12 and B15: the operation and claim that prepared a submission, and the
        # once-only marker of its follow-on
        ("batch_submissions", "operation_id", ("bigint", 64, 0, "YES", None)),
        ("batch_submissions", "operation_claim_id", ("uuid", None, None, "YES", None)),
        ("batch_submissions", "follow_on_at", ("timestamp with time zone", None, None, "YES", None)),
        # Codex r3 B15, r4 B2: the lane of the admission that made the attempt
        ("score_attempts", "lane", ("text", None, None, "YES", None)),
        # Codex r1 #11, r2 B17: the last valid review's injection assessment
        ("llm_results", "critic_injection", ("text", None, None, "YES", None)),
    ],
)
async def test_the_columns_beyond_the_spec_ddl(
    engine, table: str, column: str, expected: tuple[object, ...]
) -> None:
    async with engine.connect() as c:
        row = (await c.execute(sa.text(COLUMNS), {"t": table, "c": column})).one()
    assert tuple(row) == expected


LANE_CHECK = (
    "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = CAST(:t AS regclass) "
    "AND contype = 'c' AND pg_get_constraintdef(oid) LIKE '%lane%'"
)


async def test_the_lane_checks_of_targets_and_attempts_are_one_list(engine) -> None:
    """Both CHECKs are written from one SQL literal (`admission.LANE_VALUES` pins it, Task 12)."""
    async with engine.connect() as c:
        targets = (await c.execute(sa.text(LANE_CHECK), {"t": "score_targets"})).scalar_one()
        attempts = (await c.execute(sa.text(LANE_CHECK), {"t": "score_attempts"})).scalar_one()
    assert targets == attempts
    for lane in (
        "similarity",
        "oldest",
        "exploration",
        "always_include",
        "score_now",
        "batch",
        "batch_followup",
    ):
        assert f"'{lane}'" in targets
    assert targets.count("::text") == 7


async def test_the_parser_family_check(engine) -> None:
    family = "UPDATE prompt_schema_versions SET parser_family = :f"
    async with engine.connect() as c:
        for allowed in ("generator_v2_items", "generator_v1"):
            await c.execute(sa.text(family), {"f": allowed})
        await refused_check(
            c, "prompt_schema_versions", family, {"f": "generator_v3"}, "generator_v1", "generator_v2_items"
        )


async def _scaffold(c: AsyncConnection) -> tuple[ProfileChain, Job]:
    return await insert_profile_chain(c), await insert_job(c)


SUBMISSION = (
    "INSERT INTO batch_submissions (state, llm_tuple_id, bulk_operation_id, request_count, request_hash, "
    "reserved_usd, followup_held_usd, provider_batch_id, submit_started_at, released_at) "
    "VALUES (:state, :tuple, :bulk, 1, '\\x00', 0, 0, :provider, :started, :released)"
)


async def test_singletons_checks_and_precision(engine) -> None:
    async with engine.connect() as c:
        tuple_id = (await insert_profile_chain(c)).llm_tuple_id

        # one running cycle
        await insert_cycle(c, tuple_id, status="completed")
        await insert_cycle(c, tuple_id)
        with pytest.raises(sa.exc.IntegrityError) as bad:
            async with c.begin_nested():
                await insert_cycle(c, tuple_id)
        assert pg_error(bad.value) == PgErrorInfo(UNIQUE_VIOLATION, "score_cycles_one_running")

        # one active bulk operation
        await insert_bulk_operation(c, tuple_id, state="completed")
        bulk = await insert_bulk_operation(c, tuple_id)
        with pytest.raises(sa.exc.IntegrityError) as bad:
            async with c.begin_nested():
                await insert_bulk_operation(c, tuple_id)
        assert pg_error(bad.value) == PgErrorInfo(UNIQUE_VIOLATION, "bulk_operations_one_active")

        # one submission in flight; a released one is outside the singleton, also once it is found ended
        flight = {"tuple": tuple_id, "bulk": bulk, "provider": None, "started": None, "released": None}
        await c.execute(sa.text(SUBMISSION), {**flight, "state": "preparing"})
        for state, started in (("preparing", None), ("submit_ambiguous", NOW)):
            info = await refused(c, SUBMISSION, {**flight, "state": state, "started": started})
            assert info == PgErrorInfo(UNIQUE_VIOLATION, "batch_one_in_flight"), state
        released = {**flight, "state": "released", "started": NOW, "released": NOW}
        await c.execute(sa.text(SUBMISSION), released)
        await c.execute(sa.text(SUBMISSION), {**released, "state": "ended", "provider": "msgbatch_test"})

        # Deviation 10: the submission's sums keep six decimals
        precise = await insert_submission(
            c,
            tuple_id,
            bulk,
            state="abandoned",
            reserved_usd=Decimal("0.123456"),
            followup_held_usd=Decimal("1.000001"),
        )
        sums = sa.text("SELECT reserved_usd, followup_held_usd FROM batch_submissions WHERE id = :i")
        assert tuple((await c.execute(sums, {"i": precise})).one()) == (
            Decimal("0.123456"),
            Decimal("1.000001"),
        )

        # Deviation 2: a billed settlement may leave spent_usd above budget_usd; a negative one is refused
        spend = "UPDATE bulk_operations SET spent_usd = :s WHERE id = :i"
        await c.execute(sa.text(spend), {"s": Decimal("5.000001"), "i": bulk})  # the budget is 5
        await refused_check(
            c, "bulk_operations", spend, {"s": Decimal("-0.000001"), "i": bulk}, "spent_usd >= (0)"
        )


TARGET = (
    "INSERT INTO score_targets (job_revision_id, llm_tuple_id, job_id, state, state_reason, override, "
    "override_at, claim_id, claim_epoch, claim_heartbeat_at, refusal_category, transient_failures) "
    "VALUES (:revision, :tuple, :job, :state, :reason, :override, :override_at, :claim, :epoch, :heartbeat, "
    ":category, :transient)"
)
TARGET_BASE = {
    "state": "pending",
    "reason": None,
    "override": "none",
    "override_at": None,
    "claim": None,
    "epoch": None,
    "heartbeat": None,
    "category": None,
    "transient": 0,
}
CLAIMED = {"state": "in_progress", "claim": uuid.uuid4(), "epoch": 1, "heartbeat": NOW}


@pytest.mark.parametrize(
    ("overrides", "fragments"),
    [
        ({**CLAIMED, "claim": None}, ("claim_id", "claim_epoch", "claim_heartbeat_at")),
        ({**CLAIMED, "epoch": None}, ("claim_id", "claim_epoch", "claim_heartbeat_at")),
        ({**CLAIMED, "heartbeat": None}, ("claim_id", "claim_epoch", "claim_heartbeat_at")),
        ({"override": "score_now"}, ("override", "override_at")),
        ({"state": "refused"}, ("refused", "refusal_category")),
        ({"state": "failed_request"}, ("failed", "state_reason")),
        ({"transient": -1}, ("transient_failures >= 0",)),  # A1 issue 4 (Deviation 2)
    ],
    ids=[
        "in_progress_without_claim_id",
        "in_progress_without_epoch",
        "in_progress_without_heartbeat",
        "score_now_without_override_at",
        "refused_without_category",
        "failed_request_without_reason",
        "negative_transient_failures",
    ],
)
async def test_target_checks(engine, overrides, fragments) -> None:
    async with engine.connect() as c:
        chain, job = await _scaffold(c)
        target = {**TARGET_BASE, "revision": job.revision_id, "tuple": chain.llm_tuple_id, "job": job.job_id}
        await refused_check(c, "score_targets", TARGET, {**target, **overrides}, *fragments)
        await c.execute(sa.text(TARGET), {**target, **CLAIMED})  # valid: the three claim columns


ITEM = (
    "INSERT INTO batch_items (submission_id, target_id, attempt_no, custom_id, hold_usd) "
    "VALUES (:submission, :target, :no, :custom, :hold)"
)


async def test_batch_item_checks(engine) -> None:
    async with engine.connect() as c:
        chain, job = await _scaffold(c)
        bulk = await insert_bulk_operation(c, chain.llm_tuple_id)
        submission = await insert_submission(c, chain.llm_tuple_id, bulk, state="abandoned")
        target = await insert_target(c, job, chain.llm_tuple_id)
        item = {"submission": submission, "target": target, "no": 1, "custom": f"t{target}_a1", "hold": 0}
        await c.execute(sa.text(ITEM), {**item, "hold": Decimal("0.012345")})
        # the custom id names the target and the attempt: 't<target>_a<attempt>'
        await refused_check(c, "batch_items", ITEM, {**item, "no": 2}, "custom_id", "target_id", "attempt_no")
        # Deviation 27: the follow-up hold is never negative
        negative = {**item, "no": 3, "custom": f"t{target}_a3", "hold": Decimal("-0.000001")}
        await refused_check(c, "batch_items", ITEM, negative, "hold_usd >= (0)")


DET = (
    "INSERT INTO det_results (llm_result_id, scope_version_id, job_id, det_algorithm_version, det_criteria, "
    "knockouts, industry_match, caps_applied, score_low, score_mid, score_high, coverage, confidence, "
    "det_inputs, det_inputs_hash) SELECT :result, scope_version_id, :job, 'det-test', '{}', '{}', 'neutral', "
    "'{}', :low, :mid, :high, 0.9, 'high', '{}', '\\x00' FROM active_config"
)


async def test_the_mid_score_is_the_rounded_mean(engine) -> None:
    async with engine.connect() as c:
        chain, job = await _scaffold(c)
        result = (await insert_published(c, job, chain.llm_tuple_id)).result_id
        det = {"result": result, "job": job.job_id, "low": Decimal(40), "high": Decimal("60.01")}
        await refused_check(c, "det_results", DET, {**det, "mid": Decimal(50)}, "round")
        await c.execute(sa.text(DET), {**det, "mid": Decimal("50.01")})  # round(100.01 / 2, 2)


RESULT = (
    "INSERT INTO llm_results (target_id, attempt_id, final_draft_id, job_revision_id, profile_version_id, "
    "rubric_version_id, prompt_schema_version_id, model_policy_id, evaluation_policy_id, status, criteria, "
    "job_facts, knockout_candidates, fact_confirmations, not_a_job_ad, injection_suspected, critic_outcome, "
    "critic_injection, revisions_used, summary_en, summary_de) "
    "SELECT :target, :attempt, :draft, :revision, t.profile_version_id, t.rubric_version_id, "
    "t.prompt_schema_version_id, t.model_policy_id, a.evaluation_policy_id, :status, '{}', '{}', '{}', '{}', "
    "false, false, :outcome, :injection, 0, 'Invented summary.', 'Erfundene Zusammenfassung.' "
    "FROM llm_tuples t, active_config a WHERE t.id = :tuple"
)


async def test_the_critic_injection_is_recorded_exactly_when_the_critic_ran(engine) -> None:
    """Codex r1 #11, r2 B17: the last valid review's assessment, or `unavailable` when none is valid."""
    async with engine.connect() as c:
        chain, job = await _scaffold(c)
        target = await insert_target(c, job, chain.llm_tuple_id)
        attempt = await insert_attempt(c, target)
        _, call = await insert_scoring_call(c, attempt_id=attempt, target_id=target)
        draft = await insert_draft(c, attempt, call)
        result = {
            "target": target,
            "attempt": attempt,
            "draft": draft,
            "revision": job.revision_id,
            "tuple": chain.llm_tuple_id,
            "status": "accepted",
        }
        for outcome, injection in (("not_run", "none"), ("accepted", None)):
            params = {**result, "outcome": outcome, "injection": injection}
            await refused_check(c, "llm_results", RESULT, params, "critic_injection IS NULL", "not_run")
        params = {**result, "outcome": "accepted", "injection": "present"}
        await refused_check(c, "llm_results", RESULT, params, "present_no_influence", "unavailable")
        # a contested publication after two invalid critic outputs (L3557-3558)
        contested = {**result, "status": "contested", "outcome": "contested", "injection": "unavailable"}
        await c.execute(sa.text(RESULT), contested)


EVAL_CASE = (
    "INSERT INTO eval_case_results (eval_run_id, golden_case_id, repetition, outcome, critic_ran, "
    "llm_call_ids, output) VALUES (:run, :case, :repetition, :outcome, false, '{}', '{}')"
)


async def test_eval_case_results_checks(engine) -> None:
    async with engine.connect() as c:
        chain, job = await _scaffold(c)
        case = {"run": await insert_eval_run(c), "outcome": "failed", "repetition": 3}
        case["case"] = await insert_golden_case(c, job, chain.profile_version_id)
        await c.execute(sa.text(EVAL_CASE), case)
        # at most three repetitions (A1 issue 12, Deviation 2)
        await refused_check(c, "eval_case_results", EVAL_CASE, {**case, "repetition": 4}, "repetition")
        # the spec's five outcomes: an interrupted eval run is failed and re-run, never resumed (Deviation 29)
        interrupted = {**case, "repetition": 1, "outcome": "interrupted"}
        await refused_check(c, "eval_case_results", EVAL_CASE, interrupted, "filtered_out", "failed")


LEDGER = (
    "INSERT INTO llm_spend_ledger (budget_month, kind, purpose, purpose_class, model_requested, state, "
    "reserved_usd, settled_usd, bulk_operation_id, admitted_cap_usd, counted_before_usd, evidence) "
    "VALUES ('2026-10-01', 'request', :purpose, :cls, 'claude-sonnet-5-5', :state, 1, :settled, :bulk, "
    "100, 0, :evidence)"
)
CALL = (
    "INSERT INTO llm_calls (purpose, attempt_id, target_id, logical_call_no, execution_config_id, "
    "model_requested, effort, max_tokens, transport, prompt_template_version, rendered_prompt_sha256, "
    "input_refs, input_tokens_bound, status, spend_ledger_id) "
    "SELECT 'generate', :attempt, :target, :logical, execution_config_id, 'claude-sonnet-5-5', 'medium', "
    "6000, 'stream', 'v2', '\\x00', '{}', 1000, 'reserved', :ledger FROM active_config"
)


async def test_the_m2_checks_over_the_closed_columns_still_hold(engine) -> None:
    """`llm_calls` and `llm_spend_ledger` are altered, never recreated: the CHECKs over the five closed
    columns and M2 Deviation 30's two evidence CHECKs keep refusing what they refused (A1 issue 36)."""
    async with engine.connect() as c:
        chain, job = await _scaffold(c)
        bulk = await insert_bulk_operation(c, chain.llm_tuple_id)
        target = await insert_target(c, job, chain.llm_tuple_id)
        attempt = await insert_attempt(c, target)
        await c.execute(
            sa.text(
                "INSERT INTO llm_budget (month, cap_usd, overrun_buffer_usd, bucket_usd, bucket_at) "
                "VALUES ('2026-10-01', 100, 2, 1, now())"
            )
        )
        scoring = {
            "purpose": "generate",
            "cls": "scoring",
            "state": "reserved",
            "settled": None,
            "evidence": None,
        }
        ledger = (await c.execute(sa.text(LEDGER + " RETURNING id"), {**scoring, "bulk": bulk})).scalar_one()
        call = {"attempt": attempt, "target": target, "logical": 1, "ledger": ledger}
        await refused_check(
            c,
            "llm_calls",
            CALL,
            {**call, "attempt": None, "target": None},
            "eval_run_id",
            "attempt_id",
            "target_id",
        )
        await refused_check(c, "llm_calls", CALL, {**call, "logical": None}, "logical_call_no", "transport")
        await c.execute(sa.text(CALL), call)  # valid: the closed foreign keys name real rows
        nonscoring = {**scoring, "purpose": "extract_profile", "cls": "nonscoring"}
        await refused_check(
            c, "llm_spend_ledger", LEDGER, {**nonscoring, "bulk": bulk}, "bulk_operation_id IS NULL"
        )
        unexplained: dict[str, object] = {**nonscoring, "bulk": None, "state": "not_billed", "settled": 0}
        await refused_check(c, "llm_spend_ledger", LEDGER, unexplained, "evidence IS NOT NULL")
        silent: dict[str, object] = {**nonscoring, "bulk": None, "state": "billed", "settled": 2}
        await refused_check(c, "llm_spend_ledger", LEDGER, silent, "reservation_bound_violated")


# Deviation 2 (M1's note 9 precedent): each column a retention or purge delete reaches through its
# referencing side, with the index the foreign key's lookup uses.
DEVIATION_2_INDEXES = [
    ("llm_calls", "target_id", "llm_calls_target"),
    ("llm_calls", "golden_case_id", "llm_calls_golden_case"),
    ("llm_calls", "batch_item_id", "llm_calls_batch_item"),
    ("llm_spend_ledger", "bulk_operation_id", "llm_spend_bulk"),
    ("score_attempts", "cycle_id", "score_attempts_cycle"),
    ("score_attempts", "resumed_from_attempt_id", "score_attempts_resumed"),
    ("critic_notes", "draft_id", "critic_notes_draft"),
    ("llm_results", "attempt_id", "llm_results_attempt"),
    ("llm_results", "final_draft_id", "llm_results_final_draft"),
    ("score_targets", "resume_draft_id", "score_targets_resume_draft"),
    ("score_targets", "filter_scope_version_id", "score_targets_filter_scope"),
    ("score_targets", "batch_submission_id", "score_targets_submission"),
    ("batch_items", "submission_id", "batch_items_submission"),
    ("det_results", "llm_result_id", "det_results_result"),
    ("score_feedback", "llm_result_id", "score_feedback_result"),
    ("score_feedback", "det_result_id", "score_feedback_det_result"),
    ("score_feedback", "job_id", "score_feedback_job"),
    ("score_feedback", "job_revision_id", "score_feedback_revision"),
    ("golden_cases", "labelled_from_result_id", "golden_cases_labelled_from"),
    ("golden_cases", "profile_version_id", "golden_cases_profile"),
    ("golden_cases", "scope_version_id", "golden_cases_scope"),
    ("eval_case_results", "golden_case_id", "eval_case_results_case"),
    ("batch_submissions", "operation_id", "batch_submissions_operation"),  # Deviation 27
]


@pytest.mark.parametrize(("table", "column", "index"), DEVIATION_2_INDEXES)
async def test_a_delete_finds_the_referencing_rows_by_index(
    engine, table: str, column: str, index: str
) -> None:
    """The lookup a foreign key's action runs on the referencing side (`<column> = $1`, a generic plan, as a
    referential trigger prepares it) has an index that leads with `column` (the named one, checked in the
    catalog), and the generic plan is an index search on `column`. Which index the planner picks is not
    asserted: on these near-empty test tables the costs tie, and rows left by other tests can tip the choice
    to a composite index (e.g. `golden_cases (job_revision_id, profile_version_id)`). The names are constants
    of this module."""
    async with engine.connect() as c:
        await c.execute(sa.text("SET LOCAL plan_cache_mode = force_generic_plan"))
        await c.execute(sa.text("SET LOCAL enable_seqscan = off"))
        await c.execute(sa.text(f"PREPARE lookup (bigint) AS SELECT 1 FROM {table} WHERE {column} = $1"))  # noqa: S608
        plan = "\n".join((await c.execute(sa.text("EXPLAIN EXECUTE lookup (1)"))).scalars())
        await c.execute(sa.text("DEALLOCATE lookup"))
        used = re.findall(r"(?:Index (?:Only )?Scan using|Bitmap Index Scan on) (\S+)", plan)
        rows = (
            await c.execute(
                sa.text(
                    "SELECT i.relname, a.attname FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid "
                    "JOIN pg_attribute a ON a.attrelid = x.indrelid AND a.attnum = x.indkey[0] "
                    "WHERE x.indrelid = CAST(:t AS regclass)"
                ),
                {"t": table},
            )
        ).all()
        await c.rollback()
    leading = {str(name): str(first) for name, first in rows}
    assert leading.get(index) == column, leading
    assert used and f"({column} = $1)" in plan, plan


async def test_the_synthetic_job_index(engine) -> None:
    async with engine.connect() as c:
        definition = (
            await c.execute(
                sa.text("SELECT indexdef FROM pg_indexes WHERE indexname = 'jobs_synthetic_eval'")
            )
        ).scalar_one()
    assert (
        definition == "CREATE INDEX jobs_synthetic_eval ON public.jobs USING btree (id) WHERE synthetic_eval"
    )


# One assert_transition trigger per M3 machine (Task 2 registers the edges), on its state column.
MACHINES = {
    "score_targets": "state",
    "score_attempts": "outcome",
    "score_cycles": "status",
    "bulk_operations": "state",
    "batch_submissions": "state",
    "batch_items": "result_type",
}


async def test_one_transition_trigger_per_m3_machine(engine) -> None:
    triggers = sa.text(
        "SELECT CAST(tgrelid AS regclass), pg_get_triggerdef(oid) FROM pg_trigger "
        "WHERE tgfoid = 'assert_transition'::regproc AND NOT tgisinternal "
        "AND CAST(tgrelid AS regclass) = ANY (CAST(:tables AS regclass[]))"
    )
    async with engine.connect() as c:
        found = {
            str(table): definition
            for table, definition in await c.execute(triggers, {"tables": list(MACHINES)})
        }
    assert found == {
        table: f"CREATE TRIGGER {table}_assert_transition BEFORE UPDATE OF {column} ON public.{table} "
        f"FOR EACH ROW EXECUTE FUNCTION assert_transition('{column}')"
        for table, column in MACHINES.items()
    }


async def _published_target(c: AsyncConnection) -> int:
    chain, job = await _scaffold(c)
    return (await insert_published(c, job, chain.llm_tuple_id)).target_id


async def _published_attempt(c: AsyncConnection) -> int:
    chain, job = await _scaffold(c)
    return (await insert_published(c, job, chain.llm_tuple_id)).attempt_id


async def _completed_cycle(c: AsyncConnection) -> int:
    return await insert_cycle(c, (await insert_profile_chain(c)).llm_tuple_id, status="completed")


async def _completed_bulk(c: AsyncConnection) -> int:
    return await insert_bulk_operation(c, (await insert_profile_chain(c)).llm_tuple_id, state="completed")


async def _reconciled_submission(c: AsyncConnection) -> int:
    chain = await insert_profile_chain(c)
    bulk = await insert_bulk_operation(c, chain.llm_tuple_id)
    return await insert_submission(
        c,
        chain.llm_tuple_id,
        bulk,
        state="reconciled",
        provider_batch_id="msgbatch_test",
        submit_started_at=NOW,
        results_complete_at=NOW,
    )


async def _succeeded_item(c: AsyncConnection) -> int:
    chain, job = await _scaffold(c)
    bulk = await insert_bulk_operation(c, chain.llm_tuple_id)
    submission = await insert_submission(c, chain.llm_tuple_id, bulk, state="abandoned")
    target = await insert_target(c, job, chain.llm_tuple_id)
    return await insert_batch_item(
        c, submission, target, result_type="succeeded", decision="published", processed_at=NOW
    )


@pytest.mark.parametrize(
    ("table", "make", "update", "message"),
    [
        # published states are terminal (§7.16.3)
        ("score_targets", _published_target, "SET state = 'pending'", "published_accepted -> pending"),
        ("score_attempts", _published_attempt, "SET outcome = 'running'", "published_accepted -> running"),
        ("score_cycles", _completed_cycle, "SET status = 'running'", "completed -> running"),
        ("bulk_operations", _completed_bulk, "SET state = 'active'", "completed -> active"),
        ("batch_submissions", _reconciled_submission, "SET state = 'preparing'", "reconciled -> preparing"),
        ("batch_items", _succeeded_item, "SET result_type = 'awaiting'", "succeeded -> awaiting"),
    ],
)
async def test_the_trigger_refuses_a_disallowed_update(engine, table, make, update, message) -> None:
    async with engine.connect() as c:
        row = await make(c)
        with pytest.raises(sa.exc.IntegrityError, match=f"disallowed transition {table}: {message}"):
            async with c.begin_nested():
                await c.execute(sa.text(f"UPDATE {table} {update} WHERE id = :i"), {"i": row})


async def test_the_guard_refuses_null_pointers_and_names_the_remedy(fresh_pg_server) -> None:
    """0008 opens with a guard: an install whose `active_config` row has a NULL rubric or prompt-schema
    pointer (one that never ran M2's `migrate`) gets a clear error instead of a bare NOT NULL violation, and
    stays at 0007. Once both pointers are set, the upgrade passes; the migration inserts no row. The state at
    0007 is put in place by direct SQL (ruling T1-b): M2's seeded rows, with the pointers still NULL."""
    dsn = fresh_pg_server.dsn("migrator", driver="sqlalchemy")
    await asyncio.to_thread(command.upgrade, alembic_config(dsn), M2_HEAD)
    insert_m1_configuration(fresh_pg_server)
    rubric, prompt_schema = insert_m2_seed_rows(fresh_pg_server, point=False)
    point = "UPDATE active_config SET rubric_version_id = %s, prompt_schema_version_id = %s"
    for pointers in ((None, None), (rubric, None), (None, prompt_schema)):
        pg_one(fresh_pg_server, "migrator", point, *pointers)
        with pytest.raises(sa.exc.DBAPIError, match=r"run the M2 release's `migrate` first") as refusal:
            await asyncio.to_thread(command.upgrade, alembic_config(dsn), "head")
        assert pg_error(refusal.value).sqlstate == "55000", pointers  # object_not_in_prerequisite_state
        assert pg_one(fresh_pg_server, "migrator", "SELECT version_num FROM alembic_version") == (M2_HEAD,)
        assert pg_one(fresh_pg_server, "migrator", "SELECT to_regclass('score_targets')") == (None,)

    pg_one(fresh_pg_server, "migrator", point, rubric, prompt_schema)
    await asyncio.to_thread(command.upgrade, alembic_config(dsn), "head")
    assert pg_one(fresh_pg_server, "migrator", "SELECT version_num FROM alembic_version") == (code_head(),)
    stored = "SELECT rubric_version_id, prompt_schema_version_id FROM active_config"
    assert pg_one(fresh_pg_server, "migrator", stored) == (rubric, prompt_schema)
    counts = " UNION ALL ".join(f"SELECT count(*) FROM {table}" for table in sorted(M3_TABLES))  # noqa: S608
    assert pg_rows(fresh_pg_server, "migrator", counts) == [(0,)] * len(M3_TABLES)  # no INSERT (§19.3 step 2)


async def test_an_install_without_an_active_config_row_upgrades(fresh_pg_server) -> None:
    """A database migrated but never seeded has no `active_config` row: the guard has nothing to refuse."""
    dsn = fresh_pg_server.dsn("migrator", driver="sqlalchemy")
    await asyncio.to_thread(command.upgrade, alembic_config(dsn), "head")
    assert pg_one(fresh_pg_server, "migrator", "SELECT count(*) FROM active_config") == (0,)
    assert pg_one(fresh_pg_server, "migrator", "SELECT version_num FROM alembic_version") == (code_head(),)
