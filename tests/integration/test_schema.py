import psycopg
import pytest

from tests.support.pg import PgRole, PgServer, ensure_config_rows, pg_rows

pytestmark = pytest.mark.integration

M0_TABLES = {
    "ranking_state",
    "users",
    "sessions",
    "login_attempts",
    "login_lockouts",
    "api_idempotency",
    "scope_versions",
    "model_policies",
    "execution_configs",
    "evaluation_policies",
    "rubric_versions",
    "prompt_schema_versions",
    "settings_versions",
    "active_config",
    "schedule_state",
    "operations",
    "runs",
    "run_attempts",
    "worker_heartbeat",
    "alerts",
    "purge_fences",
    "alembic_version",
}
LATER = {"score_cycles", "deletion_manifests"}  # sources, postings: M1; llm_tuples, profile_versions: M2


def rows(server: PgServer, statement: str, role: PgRole = "migrator") -> list[tuple[object, ...]]:
    return pg_rows(server, role, statement)  # shared helper (Task 4)


def test_tables(migrated: PgServer) -> None:
    tables = {r[0] for r in rows(migrated, "SELECT tablename FROM pg_tables WHERE schemaname='public'")}
    assert M0_TABLES <= tables and not (tables & LATER)


def test_active_config_m2_columns_nullable(migrated: PgServer) -> None:
    """The rubric and prompt-schema pointers stay nullable through M2: the seed sets both (M2 Task 6), and
    M3's first migration restores the spec's NOT NULL (M0 Deviation 1, M2 plan Deviation 32)."""
    assert rows(
        migrated,
        "SELECT column_name, is_nullable FROM information_schema.columns WHERE table_name='active_config' "
        "AND column_name IN ('rubric_version_id','prompt_schema_version_id','settings_version_id') "
        "ORDER BY 1",
    ) == [("prompt_schema_version_id", "YES"), ("rubric_version_id", "YES"), ("settings_version_id", "NO")]
    # Nullable, but set: the migrate seed points both at the seeded rows (tests/integration/test_seed_m2.py).
    assert rows(
        migrated,
        "SELECT rubric_version_id IS NOT NULL, prompt_schema_version_id IS NOT NULL FROM active_config",
    ) == [(True, True)]


def test_text_search_and_unaccent(migrated: PgServer) -> None:
    assert rows(migrated, "SELECT immutable_unaccent('Ärger Straße')") == [("Arger Strasse",)]
    assert rows(
        migrated, "SELECT to_tsvector('german_unaccent','Müller') = to_tsvector('german_unaccent','Muller')"
    ) == [(True,)]


RUN = (
    "INSERT INTO runs (trigger,scheduled_slot,status,include_paid,settings_version_id,"
    "scope_version_id{extra_cols}) "
    "VALUES ({trigger},{slot},'{status}',true,<sv>,<sc>{extra_vals})"
)


def run_sql(
    status: str,
    trigger: str = "'schedule'",
    slot: str = "'2026-10-16T06:00Z'",
    extra_cols: str = "",
    extra_vals: str = "",
) -> str:
    return RUN.format(trigger=trigger, slot=slot, status=status, extra_cols=extra_cols, extra_vals=extra_vals)


@pytest.mark.parametrize(
    ("statements", "sqlstate"),
    [
        (
            [run_sql("queued"), run_sql("queued", slot="'2026-10-16T12:00Z'")],
            "23505",
        ),  # runs_one_active_collect
        ([run_sql("missed"), run_sql("missed")], "23505"),  # runs_slot_uq
        ([run_sql("queued", trigger="'manual'")], "23514"),
        ([run_sql("missed", extra_cols=",attempt_no", extra_vals=",1")], "23514"),
        ([run_sql("running", extra_cols=",attempt_no,started_at", extra_vals=",1,now()")], "23514"),
        (
            ["INSERT INTO operations (kind,payload,dedup_key,status) VALUES ('export','{}','e','running')"],
            "23514",
        ),
        (
            ["INSERT INTO operations (kind,payload,dedup_key,status) VALUES ('export','{}','e','pending')"]
            * 2,
            "23505",
        ),
        (
            [
                "INSERT INTO operations "
                "(kind,payload,dedup_key,status,claim_id,worker_epoch,heartbeat_at,finished_at) "
                "VALUES ('export','{}','e','succeeded',gen_random_uuid(),1,now(),now())"
            ],
            "23514",
        ),  # full ownership, finished row
        (
            [
                "INSERT INTO operations (kind,payload,dedup_key,status,claim_id,worker_epoch,heartbeat_at) "
                "VALUES ('export','{}','e','pending',gen_random_uuid(),1,now())"
            ],
            "23514",
        ),  # full ownership, pending row
        (
            [
                "INSERT INTO api_idempotency (route,idem_key,request_sha256,status,expires_at) "
                "VALUES ('r','short','\\x00','in_progress',now())"
            ],
            "23514",
        ),
        (
            ["INSERT INTO users (id,username,password_hash,password_changed_at) VALUES (2,'abc','h',now())"],
            "23514",
        ),
        (["INSERT INTO login_lockouts VALUES ('user:x', now(), 'r')"], "23514"),
        (["INSERT INTO purge_fences (kind,manifest_id) VALUES ('source',1)"], "23514"),
        (["INSERT INTO purge_fences (kind,manifest_id) VALUES ('jobs',1)"], "23514"),
        (
            [
                "INSERT INTO schedule_state VALUES (1, now(), 3599, true, NULL, <sv>, now()) "
                "ON CONFLICT (id) DO UPDATE SET interval_s = 3599"
            ],
            "23514",
        ),
        (["INSERT INTO alerts (code,severity,dedup_key,message) VALUES ('a','warn','k','m')"] * 2, "23505"),
    ],
)
def test_constraints_reject(migrated: PgServer, statements: list[str], sqlstate: str) -> None:
    with psycopg.connect(migrated.dsn("migrator")) as c:
        sv, sc = ensure_config_rows(c)
        with pytest.raises(psycopg.Error) as exc:
            for s in statements:
                c.execute(s.replace("<sv>", str(sv)).replace("<sc>", str(sc)))
        assert exc.value.sqlstate == sqlstate
        c.rollback()


def test_is_fenced_global_and_jobs(migrated: PgServer) -> None:
    with psycopg.connect(migrated.dsn("migrator")) as c:
        assert c.execute("SELECT is_fenced(5)").fetchone() == (False,)
        c.execute("INSERT INTO purge_fences (kind,job_ids,manifest_id) VALUES ('jobs','{5}',1)")
        assert c.execute("SELECT is_fenced(5), is_fenced(6)").fetchone() == (True, False)
        c.execute("UPDATE purge_fences SET released_at = now()")
        assert c.execute("SELECT is_fenced(5)").fetchone() == (False,)
        c.execute("INSERT INTO purge_fences (kind,manifest_id) VALUES ('global',2)")
        assert c.execute("SELECT is_fenced(6, 9)").fetchone() == (True,)
        c.rollback()


def test_code_head_matches_db(migrated: PgServer) -> None:
    from jobhunter.db.alembic_runner import code_head

    assert rows(migrated, "SELECT version_num FROM alembic_version") == [(code_head(),)]
