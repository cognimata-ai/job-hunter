import uuid
from decimal import Decimal

import psycopg
import pytest
import sqlalchemy as sa
from jobhunter.db.sql import LOCK_NOT_AVAILABLE
from sqlalchemy.ext.asyncio import AsyncConnection

from tests.support.fences import insert_test_fence
from tests.support.fixtures import TRUNCATED_TABLES, reset_database
from tests.support.m2 import (
    ProfileChain,
    digest,
    insert_embedding,
    insert_eval_run,
    insert_llm_call,
    insert_profile_chain,
    insert_proposal,
    insert_vector,
)
from tests.support.m3 import (
    NOW,
    active_ids,
    insert_attempt,
    insert_batch_item,
    insert_bulk_operation,
    insert_cycle,
    insert_det_result,
    insert_golden_case,
    insert_group,
    insert_id,
    insert_job,
    insert_published,
    insert_row,
    insert_scoring_call,
    insert_submission,
    insert_target,
)
from tests.support.pg import MANUAL_RUN_SQL, pg_one, pg_rows

pytestmark = pytest.mark.integration

# The seeded schedule, derived in SQL from the stored settings version 1: its interval, and its anchor on the
# date (in the display zone) on which that version was created.
SEEDED = """\
SELECT ((s.created_at AT TIME ZONE z.tz)::date + (s.payload->'schedule'->>'anchor_local')::time)
         AT TIME ZONE z.tz,
       (s.payload->'schedule'->>'interval_hours')::int * 3600
FROM settings_versions s, LATERAL (SELECT s.payload->'display'->>'timezone' AS tz) z
WHERE s.version_no = 1"""
SCHEDULE = "SELECT anchor_utc, interval_s, enabled FROM schedule_state"
# The slot lies on the schedule, and it is the latest one that is not after the database's now() as the reset
# read it. That instant lies between two instants the test reads before and after the reset, so the slot is
# not after the later one (first parameter) and the slot's successor is after the earlier one (second).
SLOT_IS_LATEST = """\
SELECT last_evaluated_slot <= %s AND last_evaluated_slot + make_interval(secs => interval_s) > %s,
       mod(extract(epoch FROM last_evaluated_slot - anchor_utc)::bigint, interval_s) = 0
FROM schedule_state"""
DIRT = (
    MANUAL_RUN_SQL,
    "INSERT INTO operations (kind,payload,dedup_key,status) VALUES ('export','{}','e','pending')",
    "INSERT INTO alerts (code,severity,dedup_key,message) VALUES ('a','warn','k','m')",
    "INSERT INTO login_attempts (ip, username_hmac, success) VALUES ('127.0.0.1', '\\x00', false)",
)
COUNTS = (
    "SELECT (SELECT count(*) FROM runs), (SELECT count(*) FROM operations), (SELECT count(*) FROM alerts), "
    "(SELECT count(*) FROM login_attempts)"
)


def test_a_test_that_uses_migrated_is_cleaned_by_itself(migrated, request) -> None:
    # `clean_db` is requested by the autouse fixture of tests/integration/conftest.py.
    assert "clean_db" in request.fixturenames


def test_a_test_without_the_database_is_not(request) -> None:
    assert "clean_db" not in request.fixturenames


def test_reset_empties_the_tables_and_restores_the_seeded_schedule(migrated) -> None:
    for statement in DIRT:
        pg_rows(migrated, "migrator", statement)
    assert pg_one(migrated, "migrator", COUNTS) == (1, 1, 1, 1)
    pg_rows(
        migrated,
        "migrator",
        "UPDATE schedule_state SET interval_s = 7200, anchor_utc = anchor_utc + interval '1 hour', "
        "enabled = false, last_evaluated_slot = NULL",
    )

    before = pg_one(migrated, "migrator", "SELECT now()")
    reset_database(migrated)
    after = pg_one(migrated, "migrator", "SELECT now()")

    assert pg_one(migrated, "migrator", COUNTS) == (0, 0, 0, 0)
    seeded = pg_one(migrated, "migrator", SEEDED)
    assert seeded is not None
    assert pg_one(migrated, "migrator", SCHEDULE) == (*seeded, True)
    assert before is not None and after is not None
    assert pg_one(migrated, "migrator", SLOT_IS_LATEST, after[0], before[0]) == (True, True)


def test_a_leaked_lock_fails_the_reset_instead_of_hanging_it(migrated) -> None:
    with psycopg.connect(migrated.dsn("migrator")) as leaked:
        leaked.execute("LOCK TABLE alerts IN ACCESS SHARE MODE")  # held until this transaction ends
        with pytest.raises(psycopg.Error) as blocked:
            reset_database(
                migrated, lock_timeout="50ms"
            )  # short only to fail fast; the lock is never released
        assert blocked.value.sqlstate == LOCK_NOT_AVAILABLE
    reset_database(migrated)  # the lock is gone: the reset works again


# Every column of the one `active_config` row.
ACTIVE_CONFIG = (
    "SELECT id, settings_version_id, scope_version_id, pending_scope_version_id, profile_version_id, "
    "rubric_version_id, prompt_schema_version_id, model_policy_id, execution_config_id, "
    "evaluation_policy_id, llm_tuple_id FROM active_config"
)
M2_COUNTS = (
    "SELECT (SELECT count(*) FROM cv_files), (SELECT count(*) FROM profile_drafts), "
    "(SELECT count(*) FROM profile_versions), (SELECT count(*) FROM llm_tuples), "
    "(SELECT count(*) FROM query_proposals), (SELECT count(*) FROM profile_vectors), "
    "(SELECT count(*) FROM llm_budget), (SELECT count(*) FROM llm_spend_ledger), "
    "(SELECT count(*) FROM llm_calls), (SELECT count(*) FROM eval_runs)"
)
TEST_ROWS = (
    "SELECT (SELECT count(*) FROM rubric_versions WHERE content_hash = %s), "
    "(SELECT count(*) FROM prompt_schema_versions WHERE prompt_schema_hash = %s)"
)
SHIPPED_RUBRICS = "SELECT version FROM rubric_versions ORDER BY version"


# The M3 tables, and the M1 tables `_M3_DELETE` empties because a TRUNCATE of them would cascade into them.
M3_COUNTS = " UNION ALL ".join(
    f"SELECT '{table}', count(*) FROM {table}"  # noqa: S608 (constant names)
    for table in (
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
        "jobs",
        "job_revisions",
        "duplicate_groups",
        "job_tombstones",
        "embeddings",
        "operations",
    )
)


async def insert_scored_jobs(c: AsyncConnection, chain: ProfileChain) -> None:
    """One row of every M3 table, and every reference the reset must undo: a scored job (cycle, bulk
    operation, published target, attempt, its call, draft, critic note, result with an instance and
    evidence), a second job in a batch (submission of an operation, item, the batch call naming it), a
    deferred member of a duplicate group, a golden case with its eval call and case result, feedback, a user
    action, a lane credit, an embedded revision and a job restored from a tombstone."""
    tuple_id = chain.llm_tuple_id
    embedding = await insert_embedding(c, "m3-reset", input_kind="passage")
    scored = await insert_job(c, embedding_id=embedding)
    batched, deferred = await insert_job(c), await insert_job(c)
    tombstone = await insert_id(c, "job_tombstones", {"former_job_id": 999_999})
    await c.execute(
        sa.text("UPDATE jobs SET restored_from_tombstone_id = :t WHERE id = :j"),
        {"t": tombstone, "j": deferred.job_id},
    )
    group = await insert_group(c, scored, [deferred])
    cycle = await insert_cycle(c, tuple_id)
    bulk = await insert_bulk_operation(c, tuple_id)
    await insert_row(c, "admission_lane_credits", {"llm_tuple_id": tuple_id, "lane": "oldest", "credit": -1})

    published = await insert_published(c, scored, tuple_id, bulk_operation_id=bulk, cycle_id=cycle)
    _, critic_call = await insert_scoring_call(
        c, attempt_id=published.attempt_id, target_id=published.target_id, purpose="critic"
    )
    await insert_row(
        c,
        "critic_notes",
        {
            "attempt_id": published.attempt_id,
            "round": 1,
            "draft_id": published.draft_id,
            "llm_call_id": critic_call,
            "effort": "medium",
            "triggers": ["T2"],
            "verdict_returned": "accept",
            "derived_outcome": "accept",
            "review_json": {},
            "validation": {},
        },
    )
    scope = (await active_ids(c)).scope_version_id
    det = await insert_det_result(
        c, published.result_id, scored.job_id, scope, low=Decimal(40), high=Decimal(60)
    )
    evidence = {
        "llm_result_id": published.result_id,
        "subject": "skills",
        "source_kind": "description",
        "quote": "Erfundenes Zitat",
        "span_start": 0,
        "span_end": 16,
    }
    await insert_row(c, "result_evidence", evidence)

    operation = await insert_id(
        c,
        "operations",
        {"kind": "batch_submit", "payload": {}, "dedup_key": "batch_submit:1", "status": "pending"},
    )
    submission = await insert_submission(c, tuple_id, bulk, operation_id=operation)
    in_batch = await insert_target(
        c, batched, tuple_id, state="in_batch", batch_submission_id=submission, attempt_no=1, lane="batch"
    )
    batch_attempt = await insert_attempt(c, in_batch, mode="batch", lane="batch")
    item = await insert_batch_item(c, submission, in_batch, hold_usd=Decimal("0.010000"))
    _, batch_call = await insert_scoring_call(
        c,
        attempt_id=batch_attempt,
        target_id=in_batch,
        transport="batch",
        batch_item_id=item,
        bulk_operation_id=bulk,
    )
    await c.execute(
        sa.text("UPDATE batch_items SET llm_call_id = :c WHERE id = :i"), {"c": batch_call, "i": item}
    )
    await insert_target(c, deferred, tuple_id, state="deferred_duplicate", deferred_group_id=group)

    eval_run = await insert_eval_run(c)
    case = await insert_golden_case(
        c, scored, chain.profile_version_id, labelled_from_result_id=published.result_id
    )
    _, eval_call = await insert_llm_call(c, purpose="critic", eval_run_id=eval_run)
    await c.execute(
        sa.text("UPDATE llm_calls SET golden_case_id = :g WHERE id = :c"), {"g": case, "c": eval_call}
    )
    await insert_row(
        c,
        "eval_case_results",
        {
            "eval_run_id": eval_run,
            "golden_case_id": case,
            "outcome": "accepted",
            "critic_ran": True,
            "llm_call_ids": [eval_call],
            "output": {},
        },
    )
    await insert_row(
        c,
        "score_feedback",
        {
            "job_id": scored.job_id,
            "job_revision_id": scored.revision_id,
            "profile_version_id": chain.profile_version_id,
            "llm_result_id": published.result_id,
            "det_result_id": det,
            "kind": "label",
            "label": "good",
        },
    )
    await insert_row(
        c, "user_job_actions", {"job_id": scored.job_id, "saved_at": NOW, "note": "Erfundene Notiz"}
    )


async def test_two_resets_keep_active_config(engine, migrated) -> None:
    """`TRUNCATE ... CASCADE` of a table `active_config` reaches through its foreign keys would empty
    `active_config` itself: the M2 and M3 tables are deleted instead, and the pointers are restored first. The
    rows here cover every reference between them, the deferrable ones included (they hold at commit)."""
    seeded = pg_one(migrated, "migrator", ACTIVE_CONFIG)
    assert seeded is not None and pg_one(migrated, "migrator", M2_COUNTS) == (0,) * 10
    async with engine.begin() as c:
        chain = await insert_profile_chain(c)
        eval_run = await insert_eval_run(c)
        _, call = await insert_llm_call(c, draft_id=chain.draft_id, eval_run_id=eval_run)
        proposal = await insert_proposal(c, chain.profile_version_id)
        await insert_vector(c, chain.profile_version_id)
        await c.execute(sa.text("UPDATE profile_drafts SET llm_call_id = :c"), {"c": call})
        await c.execute(
            sa.text("UPDATE query_proposals SET llm_call_id = :c WHERE id = :p"), {"c": call, "p": proposal}
        )
        await c.execute(
            sa.text(
                "UPDATE active_config SET profile_version_id = :p, llm_tuple_id = :t, "
                "rubric_version_id = :r, prompt_schema_version_id = :s"
            ),
            {
                "p": chain.profile_version_id,
                "t": chain.llm_tuple_id,
                "r": chain.rubric_version_id,
                "s": chain.prompt_schema_version_id,
            },
        )
        await insert_scored_jobs(c, chain)
    m2_counts = pg_one(migrated, "migrator", M2_COUNTS)
    assert m2_counts is not None and 0 not in m2_counts
    assert [table for table, count in pg_rows(migrated, "migrator", M3_COUNTS) if count == 0] == []
    assert pg_one(migrated, "migrator", ACTIVE_CONFIG) != seeded

    for _ in range(2):  # the second reset meets the state the first one left
        reset_database(migrated)
        assert pg_rows(migrated, "migrator", ACTIVE_CONFIG) == [seeded]
        assert pg_one(migrated, "migrator", M2_COUNTS) == (0,) * 10
        assert [table for table, count in pg_rows(migrated, "migrator", M3_COUNTS) if count != 0] == []
        assert pg_one(migrated, "migrator", TEST_ROWS, digest(1), digest(1)) == (0, 0)
        # the seed's two rubric rows stay, v1 too, which nothing points at (M3 Task 3)
        assert pg_rows(migrated, "migrator", SHIPPED_RUBRICS) == [("v1",), ("v2",)]


# Every foreign key as (referencing table, referenced table).
REFERENCES = (
    "SELECT DISTINCT CAST(CAST(conrelid AS regclass) AS text), CAST(CAST(confrelid AS regclass) AS text) "
    "FROM pg_constraint WHERE contype = 'f'"
)
# What the reset keeps or deletes row by row: the chain `active_config` stands on (M2), the configuration and
# the seeded single rows. A TRUNCATE that reached any of them would empty it, or `active_config` with it.
KEPT = {
    "active_config",
    "llm_calls",
    "llm_spend_ledger",
    "llm_budget",
    "profile_drafts",
    "profile_versions",
    "cv_files",
    "llm_tuples",
    "eval_runs",
    "settings_versions",
    "scope_versions",
    "model_policies",
    "execution_configs",
    "evaluation_policies",
    "rubric_versions",
    "prompt_schema_versions",
    "schedule_state",
    "ranking_state",
    "sources",
    "transition_matrix",
    "deadman_state",  # restored row by row (M5)
    "schema_compat",  # migrate's record of the chain (M5)
}


def test_reset_truncates_no_configuration_path(migrated) -> None:
    """`TRUNCATE ... CASCADE` empties every table that references a truncated one, and so on: walked along
    the referencing foreign keys of the fully migrated schema from `TRUNCATED_TABLES`, that closure holds no
    table the reset keeps, so a later foreign key cannot bring the cascade back silently."""
    edges = pg_rows(migrated, "migrator", REFERENCES)
    assert ("llm_calls", "score_targets") in edges  # the M3 foreign keys are in the graph
    assert ("purge_fences", "deletion_manifests") in edges  # and the M5 ones
    assert ("purged_batch_items", "batch_submissions") in edges
    closure, frontier = set(TRUNCATED_TABLES), set(TRUNCATED_TABLES)
    while frontier:
        frontier = {str(child) for child, parent in edges if parent in frontier} - closure
        closure |= frontier
    assert sorted(closure & KEPT) == []
    removed = {"jobs", "job_revisions", "duplicate_groups", "operations", "embeddings", "job_tombstones"}
    assert sorted(removed & closure) == []  # the six tables `_TRUNCATE` no longer names
    assert "score_targets" not in closure
    assert set(M5_TABLES) <= closure and "batch_submissions" not in closure


# The tables of M5 that hold rows a test writes (manifests, shipments, backups, exports, purged batch items).
M5_TABLES = (
    "deletion_manifests",
    "manifest_shipments",
    "manifest_shipment_items",
    "backup_sets",
    "backup_runs",
    "exports",
    "purged_batch_items",
)
M5_COUNTS = " UNION ALL ".join(
    f"SELECT '{table}', count(*) FROM {table}"  # noqa: S608 (constant names)
    for table in M5_TABLES
)
DEADMAN = "SELECT * FROM deadman_state"
SEEDED_DEADMAN = [(1, *(None,) * 8)]
COMPAT = "SELECT revision, compat_floor, destructive, recorded_at FROM schema_compat ORDER BY revision"


async def test_reset_empties_the_m5_tables_and_restores_deadman_state(engine, migrated) -> None:
    compat = pg_rows(migrated, "migrator", COMPAT)
    assert compat and pg_rows(migrated, "migrator", DEADMAN) == SEEDED_DEADMAN
    async with engine.begin() as c:
        chain = await insert_profile_chain(c)
        submission = await insert_submission(
            c, chain.llm_tuple_id, await insert_bulk_operation(c, chain.llm_tuple_id)
        )
        fence = await insert_test_fence(c, kind="global")
        manifest = (
            await c.execute(sa.text("SELECT manifest_id FROM purge_fences WHERE id = :f"), {"f": fence})
        ).scalar_one()
        shipment = await insert_id(c, "manifest_shipments", {"restic_snapshot_id": "snap"})
        await insert_row(
            c,
            "manifest_shipment_items",
            {"shipment_id": shipment, "manifest_uuid": uuid.uuid4(), "file_sha256": b"\x00"},
        )
        backup_set = await insert_id(c, "backup_sets", {"status": "running"})
        await insert_row(
            c, "backup_runs", {"kind": "restore_test", "backup_set_id": backup_set, "status": "running"}
        )
        await insert_row(c, "exports", {"status": "running", "expires_at": NOW})
        await insert_row(
            c,
            "purged_batch_items",
            {"submission_id": submission, "custom_id": "t1_a1", "manifest_id": manifest},
        )
        await c.execute(
            sa.text(
                "UPDATE deadman_state SET applied_interval_s = 3600, applied_timeout_s = 60, "
                "applied_grace_s = 300, applied_paused = false, last_pinged_run_id = 1, synced_at = now(), "
                "last_error_code = 'http_503', next_try_at = now()"
            )
        )
    assert [table for table, count in pg_rows(migrated, "migrator", M5_COUNTS) if count == 0] == []
    assert pg_rows(migrated, "migrator", DEADMAN) != SEEDED_DEADMAN

    reset_database(migrated)
    assert [table for table, count in pg_rows(migrated, "migrator", M5_COUNTS) if count != 0] == []
    assert pg_rows(migrated, "migrator", DEADMAN) == SEEDED_DEADMAN
    assert pg_rows(migrated, "migrator", COMPAT) == compat  # migrate's record stays as it was

    # a reset also gives back a row a test deleted
    pg_rows(migrated, "migrator", "DELETE FROM deadman_state")
    reset_database(migrated)
    assert pg_rows(migrated, "migrator", DEADMAN) == SEEDED_DEADMAN
