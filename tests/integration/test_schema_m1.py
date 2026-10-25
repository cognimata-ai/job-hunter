from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from jobhunter.db.sql import CHECK_VIOLATION, UNIQUE_VIOLATION, PgErrorInfo, pg_error
from jobhunter.db.transitions import CLEAR_REASON, transition
from jobhunter.llm.prompts.render import prompt_schema_hash, prompt_schema_v1

from tests.support.fixtures import reset_database
from tests.support.pg import MANUAL_RUN_SQL

pytestmark = pytest.mark.integration

M1_TABLES = {
    "sources",
    "watchlist_companies",
    "source_instances",
    "queries",
    "coverage_cells",
    "coverage_intervals",
    "fetch_requests",
    "raw_responses",
    "raw_items",
    "ats_snapshots",
    "ats_snapshot_members",
    "quota_windows",
    "quota_ledger",
    "postings",
    "posting_observations",
    "jobs",
    "job_revisions",
    "job_revision_occurrences",
    "job_revision_sources",
    "ad_keys",
    "ad_key_sources",
    "merge_events",
    "identity_conflicts",
    "duplicate_groups",
    "duplicate_group_members",
    "dedup_decisions",
    "job_tombstones",
    "tombstone_keys",
    "embeddings",
    "hard_filter_results",
}


async def test_tables_seeds_and_default_instances(engine) -> None:
    async with engine.connect() as c:
        tables = sa.text("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
        names = set((await c.execute(tables)).scalars())
        assert M1_TABLES <= names
        rows = (
            await c.execute(
                sa.text(
                    "SELECT id, kind, precedence_rank, can_close, default_completeness "
                    "FROM sources ORDER BY id"
                )
            )
        ).all()
        assert [tuple(r) for r in rows if r.id in ("adzuna", "greenhouse", "jsearch")] == [
            ("adzuna", "quota_search", 4, False, "partial"),
            ("greenhouse", "ats", 1, True, "full"),
            ("jsearch", "paid_search", 2, False, "full"),
        ]
        assert len(rows) == 10
        defaults = sa.text("SELECT source_id FROM source_instances WHERE instance_key='default'")
        assert sorted((await c.execute(defaults)).scalars()) == ["adzuna", "arbeitnow", "jsearch", "serpapi"]


CELL = (
    "INSERT INTO coverage_cells (source_instance_id) "
    "SELECT id FROM source_instances WHERE source_id='arbeitnow' AND instance_key='default' RETURNING id"
)


@pytest.mark.parametrize(
    ("statement", "constraint"),
    [
        (  # skipped without skip_reason
            "INSERT INTO fetch_requests (run_id, run_attempt_no, cell_id, source_instance_id, phase, "
            "operation, request_key, page_no, request_class, spec, status) "
            "VALUES (:run,1,:cell,(SELECT source_instance_id FROM coverage_cells WHERE id=:cell),"
            "'primary','list','\\x00',1,'free','{}','skipped')",
            "fetch_requests_skipped_reason_check",
        ),
        (  # tokens above capacity
            "INSERT INTO quota_windows (provider, window_start, window_end, is_current, limit_units, "
            "reserve_units, state, tokens, tokens_at, capacity) "
            "VALUES ('jsearch', now(), now() + interval '1 day', true, 200, 20, 'validated', 3, now(), 2)",
            "quota_windows_tokens_check",
        ),
        (  # open without timestamps
            "INSERT INTO source_instances (source_id, instance_key, health) "
            "VALUES ('jsearch', 'x', 'breaker_open')",
            "source_instances_breaker_check",
        ),
        (  # covered with a reason
            "INSERT INTO coverage_intervals (cell_id, kind, span, reason) "
            "VALUES (:cell, 'covered', tstzrange(now() - interval '1 day', now()), 'page_cap')",
            "coverage_intervals_covered_reason_check",
        ),
    ],
)
async def test_checks_reject_violations(engine, statement, constraint) -> None:
    async with engine.begin() as c:  # the dependencies are valid rows, so only the intended CHECK can fail
        run = (await c.execute(sa.text(MANUAL_RUN_SQL))).scalar_one()
        cell = (await c.execute(sa.text(CELL))).scalar_one()
        with pytest.raises(sa.exc.IntegrityError) as bad:
            await c.execute(sa.text(statement), {"run": run, "cell": cell})
    # The exact named constraint, through M0's helper.
    assert pg_error(bad.value) == PgErrorInfo(CHECK_VIOLATION, constraint)


BREAKER_ROW = (
    "INSERT INTO source_instances (source_id, instance_key, health, breaker_opened_at, half_open_after) "
    "VALUES ('jsearch', :key, :health, CAST(:opened AS timestamptz), CAST(:probe AS timestamptz))"
)


@pytest.mark.parametrize(
    ("health", "timestamped", "admitted"),
    [
        ("auth_failed", True, True),  # plan Deviation 2: every open state carries the half-open timestamps
        ("auth_failed", False, False),
        ("contract_broken", True, True),
        ("not_found", True, True),
        ("breaker_open", True, True),
        ("ok", True, False),  # a closed breaker carries none
        ("ok", False, True),
    ],
)
async def test_the_widened_breaker_check_pairs_every_open_state_with_its_timestamps(
    engine, health: str, timestamped: bool, admitted: bool
) -> None:
    at = datetime(2026, 10, 1, tzinfo=UTC) if timestamped else None
    params = {"key": f"breaker-{health}", "health": health, "opened": at, "probe": at}
    async with engine.begin() as c:
        if admitted:
            await c.execute(sa.text(BREAKER_ROW), params)
            return
        with pytest.raises(sa.exc.IntegrityError) as bad:
            await c.execute(sa.text(BREAKER_ROW), params)
    assert pg_error(bad.value) == PgErrorInfo(CHECK_VIOLATION, "source_instances_breaker_check")


# Ledger L383: each column a retention delete reaches through its referencing side, with the index on it.
RETENTION_FK_INDEXES = [
    ("fetch_requests", "parent_fetch_request_id", "fetch_requests_parent"),
    ("raw_responses", "body_ref_id", "raw_responses_body_ref"),
    ("ats_snapshots", "event_snapshot_id", "ats_snapshots_event"),
    ("postings", "last_absent_snapshot_id", "postings_last_absent_snapshot"),
    ("job_revisions", "embedding_id", "job_revisions_embedding"),
]


@pytest.mark.parametrize(("table", "column", "index"), RETENTION_FK_INDEXES)
async def test_a_retention_delete_finds_the_referencing_rows_by_index(
    engine, table: str, column: str, index: str
) -> None:
    """The lookup a foreign key's action runs on the referencing side (`<column> = $1`, a generic plan, as a
    referential trigger prepares it) uses the partial index; the names are constants of this module."""
    async with engine.connect() as c:
        await c.execute(sa.text("SET LOCAL plan_cache_mode = force_generic_plan"))
        await c.execute(sa.text("SET LOCAL enable_seqscan = off"))  # the tables are empty: no cost choice
        await c.execute(sa.text(f"PREPARE lookup (bigint) AS SELECT 1 FROM {table} WHERE {column} = $1"))  # noqa: S608
        plan = "\n".join((await c.execute(sa.text("EXPLAIN EXECUTE lookup (1)"))).scalars())
        await c.execute(sa.text("DEALLOCATE lookup"))
        await c.rollback()
    assert index in plan, plan


async def test_one_current_window_per_provider(engine) -> None:
    ins = sa.text(
        "INSERT INTO quota_windows (provider, window_start, window_end, is_current, limit_units, "
        "reserve_units, state, tokens, tokens_at, capacity) "
        "VALUES ('adzuna', CAST(:s AS timestamptz), CAST(:s AS timestamptz) + interval '30 days', true, "
        "2250, 0, 'validated', 1, CAST(:s AS timestamptz), 1)"
    )
    async with engine.begin() as c:
        await c.execute(ins, {"s": datetime(2026, 10, 1, tzinfo=UTC)})
        with pytest.raises(sa.exc.IntegrityError) as bad:
            await c.execute(ins, {"s": datetime(2026, 10, 2, tzinfo=UTC)})
    assert (pg_error(bad.value).sqlstate, pg_error(bad.value).constraint) == (
        UNIQUE_VIOLATION,
        "quota_windows_current",
    )


FETCH_REQUEST = (
    "INSERT INTO fetch_requests (run_id, run_attempt_no, cell_id, source_instance_id, phase, operation, "
    "request_key, page_no, request_class, spec, status) "
    "VALUES (:r,1,:c,(SELECT source_instance_id FROM coverage_cells WHERE id=:c),'primary','list',"
    "'\\x01',1,'free','{}','planned') RETURNING id"
)


async def test_new_tables_have_transition_triggers(engine) -> None:
    async with engine.begin() as c:
        rid = (await c.execute(sa.text(MANUAL_RUN_SQL))).scalar_one()
        cell = (await c.execute(sa.text(CELL))).scalar_one()
        fr = (await c.execute(sa.text(FETCH_REQUEST), {"r": rid, "c": cell})).scalar_one()
        with pytest.raises(
            sa.exc.IntegrityError, match="disallowed transition fetch_requests: planned -> succeeded"
        ):
            await c.execute(sa.text("UPDATE fetch_requests SET status='succeeded' WHERE id=:i"), {"i": fr})


RAW_RESPONSE = (
    "INSERT INTO raw_responses (fetch_request_id, source_instance_id, http_status, headers, body_zstd, "
    "body_sha256, fetched_at, expires_at) "
    "SELECT id, source_instance_id, 200, '{}', '\\x00', '\\x00', now(), now() + interval '30 days' "
    "FROM fetch_requests WHERE id = :f RETURNING id"
)
REJECTED_ITEM = (
    "INSERT INTO raw_items (raw_response_id, item_index, item_sha256, disposition, reason_code, disposed_at) "
    "VALUES (:r, :i, '\\x00', 'rejected', 'contract', now()) RETURNING id"
)
_RAW_ITEM_REASON_CHECK = (
    "SELECT c.conname FROM pg_constraint c WHERE c.conrelid = 'raw_items'::regclass AND c.contype = 'c' "
    "AND pg_get_constraintdef(c.oid) LIKE '%= (reason_code IS NOT NULL)%'"
)


async def test_a_replayed_raw_item_leaves_rejected_through_the_registry(engine) -> None:
    async with engine.begin() as c:
        rid = (await c.execute(sa.text(MANUAL_RUN_SQL))).scalar_one()
        cell = (await c.execute(sa.text(CELL))).scalar_one()
        fr = (await c.execute(sa.text(FETCH_REQUEST), {"r": rid, "c": cell})).scalar_one()
        response = (await c.execute(sa.text(RAW_RESPONSE), {"f": fr})).scalar_one()
        kept, cleared = [
            (await c.execute(sa.text(REJECTED_ITEM), {"r": response, "i": i})).scalar_one() for i in (0, 1)
        ]
        reason_check = (await c.execute(sa.text(_RAW_ITEM_REASON_CHECK))).scalar_one()
        # `reason=None` keeps the old reason: the §7.7 CHECK refuses a pending item with a reason code.
        with pytest.raises(sa.exc.IntegrityError) as bad:
            async with c.begin_nested():  # rolled back to here, so the transaction goes on
                await transition(c, "raw_items", kept, ["rejected"], "pending", set_={"normalized": {"t": 1}})
        assert pg_error(bad.value) == PgErrorInfo(CHECK_VIOLATION, reason_check)
        # With `CLEAR_REASON` the replay is valid: the registry clears `disposed_at` on leaving `rejected`.
        await transition(
            c,
            "raw_items",
            cleared,
            ["rejected"],
            "pending",
            reason=CLEAR_REASON,
            set_={"normalized": {"t": 1}},
        )
        row = (
            await c.execute(
                sa.text(
                    "SELECT disposition, reason_code, disposed_at, normalized FROM raw_items WHERE id = :i"
                ),
                {"i": cleared},
            )
        ).one()
        assert tuple(row) == ("pending", None, None, {"t": 1})


V2 = (
    "INSERT INTO settings_versions (version_no, schema_version, payload, scope_version_id, model_policy_id, "
    "execution_config_id, evaluation_policy_id, effect_classes) "
    "SELECT 2, schema_version, payload, scope_version_id, model_policy_id, execution_config_id, "
    "evaluation_policy_id, '{}' FROM settings_versions WHERE version_no = 1 RETURNING id"
)


# The whole `active_config` row, and what `seed_defaults` writes into it from settings version 1.
ACTIVE_CONFIG = (
    "SELECT settings_version_id, scope_version_id, pending_scope_version_id, profile_version_id, "
    "rubric_version_id, prompt_schema_version_id, model_policy_id, execution_config_id, "
    "evaluation_policy_id, llm_tuple_id FROM active_config WHERE id = 1"
)
# The rubric and prompt-schema pointers name the rows the migrate seed inserted (M2): rubric `v1` and the
# prompt-schema version of this code's templates and schemas.
SEEDED_CONFIG = (
    "SELECT id, scope_version_id, NULL::bigint, NULL::bigint, "
    "(SELECT r.id FROM rubric_versions r WHERE r.version = 'v1'), "
    "(SELECT p.id FROM prompt_schema_versions p WHERE p.prompt_schema_hash = :prompt_schema_hash), "
    "model_policy_id, execution_config_id, evaluation_policy_id, NULL::bigint "
    "FROM settings_versions WHERE version_no = 1"
)
SEEDED_PARAMS = {"prompt_schema_hash": prompt_schema_hash(*prompt_schema_v1())}
# Every pointer of the row moved off the seed: a second scope (active) with the seeded one pending, a profile
# and a tuple (they are NULL together), a rubric, a prompt schema, and a second model policy, execution config
# and evaluation policy. The profile and the tuple are a real chain (a CV file, a draft confirmed into a
# version, a tuple over it): the foreign keys of `active_config` refuse an id that names no row.
# `reset_database` deletes the chain (`_M2_DELETE`) and, once nothing names them, the five configuration rows.
DIRTY_CONFIG = (
    "INSERT INTO scope_versions (scope_hash, payload) VALUES ('\\x5eed', '{}')",
    "INSERT INTO model_policies (policy_hash, payload) VALUES ('\\x5eed', '{}')",
    "INSERT INTO execution_configs (config_hash, payload) VALUES ('\\x5eed', '{}')",
    "INSERT INTO evaluation_policies (version_no, policy_hash, payload, projected_critic_rate) "
    "VALUES (2, '\\x5eed', '{}', 0.1)",
    "INSERT INTO rubric_versions (version, content_hash, weights, anchors) "
    "VALUES ('test', '\\x5eed', '{}', '{}')",
    "INSERT INTO prompt_schema_versions (prompt_schema_hash, templates, schemas) "
    "VALUES ('\\x5eed', '{}', '{}')",
    "INSERT INTO cv_files (sha256, mime, size_bytes) VALUES ('\\x5eed', 'text/plain', 1)",
    "INSERT INTO profile_drafts (cv_file_id, status, reviewed_text_sha256, draft_json) "
    "SELECT id, 'ready', '\\x5eed', '{}' FROM cv_files",
    "INSERT INTO profile_versions (version_no, draft_id, cv_file_id, profile_json, profile_hash) "
    "SELECT 1, id, cv_file_id, '{}', '\\x5eed' FROM profile_drafts",
    "UPDATE profile_drafts SET status = 'confirmed', confirm_response = '{}', "
    "confirmed_profile_version_id = (SELECT id FROM profile_versions)",
    "INSERT INTO llm_tuples (profile_version_id, rubric_version_id, prompt_schema_version_id, "
    "model_policy_id) SELECT v.id, r.id, p.id, m.id FROM profile_versions v, rubric_versions r, "
    "prompt_schema_versions p, model_policies m WHERE r.content_hash = '\\x5eed' "
    "AND p.prompt_schema_hash = '\\x5eed' AND m.policy_hash = '\\x5eed'",
    "UPDATE active_config SET pending_scope_version_id = scope_version_id, "
    "scope_version_id = (SELECT id FROM scope_versions WHERE scope_hash = '\\x5eed'), "
    "profile_version_id = (SELECT id FROM profile_versions), llm_tuple_id = (SELECT id FROM llm_tuples), "
    "rubric_version_id = (SELECT id FROM rubric_versions WHERE content_hash = '\\x5eed'), "
    "prompt_schema_version_id = "
    "(SELECT id FROM prompt_schema_versions WHERE prompt_schema_hash = '\\x5eed'), "
    "model_policy_id = (SELECT id FROM model_policies WHERE policy_hash = '\\x5eed'), "
    "execution_config_id = (SELECT id FROM execution_configs WHERE config_hash = '\\x5eed'), "
    "evaluation_policy_id = (SELECT id FROM evaluation_policies WHERE policy_hash = '\\x5eed')",
)
# What the dirty configuration left, by table: the rows of the test (0 after a reset) and of the M2 chain.
DIRTY_ROWS = (
    "SELECT (SELECT count(*) FROM scope_versions WHERE scope_hash = '\\x5eed'), "
    "(SELECT count(*) FROM model_policies WHERE policy_hash = '\\x5eed'), "
    "(SELECT count(*) FROM execution_configs WHERE config_hash = '\\x5eed'), "
    "(SELECT count(*) FROM evaluation_policies WHERE policy_hash = '\\x5eed'), "
    "(SELECT count(*) FROM rubric_versions WHERE content_hash = '\\x5eed'), "
    "(SELECT count(*) FROM prompt_schema_versions WHERE prompt_schema_hash = '\\x5eed'), "
    "(SELECT count(*) FROM cv_files), (SELECT count(*) FROM profile_drafts), "
    "(SELECT count(*) FROM profile_versions), (SELECT count(*) FROM llm_tuples)"
)


async def test_reset_reseeds_the_default_instances_and_the_configuration(engine, migrated) -> None:
    try:
        async with engine.connect() as c:
            seeded = tuple((await c.execute(sa.text(SEEDED_CONFIG), SEEDED_PARAMS)).one())
            assert None not in seeded[4:6]  # both seeded rows exist
            assert tuple((await c.execute(sa.text(ACTIVE_CONFIG))).one()) == seeded  # as `migrate` left it
        async with engine.begin() as c:
            await c.execute(
                sa.text(
                    "INSERT INTO watchlist_companies (display_name, company_key, ats_type, board_slug, "
                    "detection, validation_status) VALUES ('X','x','greenhouse','x','manual','ok')"
                )
            )
            await c.execute(sa.text("UPDATE ranking_state SET generation = 7"))
            # A newer version that the schedule and the active config point at, as the M1 tests do.
            v2 = (await c.execute(sa.text(V2))).scalar_one()
            await c.execute(sa.text("UPDATE active_config SET settings_version_id = :v"), {"v": v2})
            await c.execute(sa.text("UPDATE schedule_state SET settings_version_id = :v"), {"v": v2})
            for statement in DIRTY_CONFIG:  # ledger L27: every other pointer of the row moved as well
                await c.execute(sa.text(statement))
            assert tuple((await c.execute(sa.text(ACTIVE_CONFIG))).one()) != seeded
            assert tuple((await c.execute(sa.text(DIRTY_ROWS))).one()) == (1,) * 10
        # Must not fail on schedule_state's FK: the schedule is re-pointed before version 2 is deleted.
        reset_database(migrated)
        async with engine.connect() as c:
            v1 = (
                await c.execute(sa.text("SELECT id FROM settings_versions WHERE version_no = 1"))
            ).scalar_one()
            defaults = sa.text("SELECT source_id FROM source_instances WHERE instance_key='default'")
            assert sorted((await c.execute(defaults)).scalars()) == [
                "adzuna",
                "arbeitnow",
                "jsearch",
                "serpapi",
            ]
            assert (
                await c.execute(sa.text("SELECT max(version_no) FROM settings_versions"))
            ).scalar_one() == 1
            schedule = (
                await c.execute(sa.text("SELECT settings_version_id FROM schedule_state"))
            ).scalar_one()
            active = (await c.execute(sa.text("SELECT settings_version_id FROM active_config"))).scalar_one()
            assert schedule == v1 == active
            # every column of the row is the seed's again, and the scope no settings version uses is deleted
            assert tuple((await c.execute(sa.text(ACTIVE_CONFIG))).one()) == seeded
            # the scope, the five configuration rows and the whole M2 chain of the test are gone too
            assert tuple((await c.execute(sa.text(DIRTY_ROWS))).one()) == (0,) * 10
            assert (await c.execute(sa.text("SELECT generation FROM ranking_state"))).scalar_one() == 0
            assert (await c.execute(sa.text("SELECT count(*) FROM watchlist_companies"))).scalar_one() == 0
    finally:
        reset_database(migrated)  # also after a failure: nothing points at the test's rows any more
