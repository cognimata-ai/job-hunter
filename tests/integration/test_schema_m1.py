from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from jobhunter.db.sql import CHECK_VIOLATION, UNIQUE_VIOLATION, PgErrorInfo, pg_error
from jobhunter.db.transitions import CLEAR_REASON, transition

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
        assert M1_TABLES <= names and "profile_vectors" not in names
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


async def test_reset_reseeds_the_default_instances_and_the_configuration(engine, migrated) -> None:
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
    # Must not fail on schedule_state's FK: the schedule is re-pointed before version 2 is deleted.
    reset_database(migrated)
    async with engine.connect() as c:
        v1 = (await c.execute(sa.text("SELECT id FROM settings_versions WHERE version_no = 1"))).scalar_one()
        defaults = sa.text("SELECT source_id FROM source_instances WHERE instance_key='default'")
        assert sorted((await c.execute(defaults)).scalars()) == ["adzuna", "arbeitnow", "jsearch", "serpapi"]
        assert (await c.execute(sa.text("SELECT max(version_no) FROM settings_versions"))).scalar_one() == 1
        schedule = (await c.execute(sa.text("SELECT settings_version_id FROM schedule_state"))).scalar_one()
        active = (await c.execute(sa.text("SELECT settings_version_id FROM active_config"))).scalar_one()
        assert schedule == v1 == active
        assert (await c.execute(sa.text("SELECT generation FROM ranking_state"))).scalar_one() == 0
        assert (await c.execute(sa.text("SELECT count(*) FROM watchlist_companies"))).scalar_one() == 0
