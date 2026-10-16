import time

import psycopg
import pytest

from tests.support.pg import PgServer
from tests.support.pg import pg_rows as q  # the shared helper of tests/support/pg.py

pytestmark = pytest.mark.integration


def test_image_is_digest_pinned(pg_server: PgServer) -> None:
    from tests.support.pg import pg_image

    assert "@sha256:" in pg_image() and pg_image().startswith("pgvector/pgvector:0.8.7-pg17-bookworm@")


def test_roles(pg_server: PgServer) -> None:
    rows = {
        name: elevated
        for name, elevated in q(
            pg_server,
            "superuser",
            "SELECT rolname, rolsuper OR rolcreaterole FROM pg_roles "
            "WHERE rolname LIKE 'jobhunter_%' AND rolname <> 'jobhunter_bootstrap'",
        )
    }
    assert rows == {
        "jobhunter_migrator": False,
        "jobhunter_app": False,
        "jobhunter_backup": False,
        "jobhunter_restore": False,
    }
    assert q(
        pg_server, "superuser", "SELECT rolcreatedb FROM pg_roles WHERE rolname='jobhunter_restore'"
    ) == [(True,)]
    assert q(
        pg_server, "superuser", "SELECT pg_has_role('jobhunter_backup','pg_read_all_data','member')"
    ) == [(True,)]
    assert q(
        pg_server,
        "superuser",
        "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname='jobhunter'",
    ) == [("jobhunter_migrator",)]


@pytest.mark.parametrize("db", ["jobhunter", "jobhunter_template"])
def test_extensions(pg_server: PgServer, db: str) -> None:
    ext = {
        name: version
        for name, version in q(pg_server, "superuser", "SELECT extname, extversion FROM pg_extension", db=db)
    }
    assert ext["vector"] == "0.8.7" and "pg_trgm" in ext and "unaccent" in ext
    assert q(
        pg_server, "superuser", "SELECT datistemplate FROM pg_database WHERE datname='jobhunter_template'"
    ) == [(True,)]


def test_default_privileges_cover_later_tables(pg_server: PgServer) -> None:
    q(pg_server, "migrator", "CREATE TABLE probe_dp (id serial PRIMARY KEY, v text)")
    try:
        # The id default calls nextval: that needs USAGE on the sequence, a later object too.
        q(pg_server, "app", "INSERT INTO probe_dp (v) VALUES ('a')")
        assert q(pg_server, "app", "SELECT last_value FROM probe_dp_id_seq") == [(1,)]
        q(pg_server, "app", "UPDATE probe_dp SET v='b'")
        assert q(pg_server, "app", "SELECT v FROM probe_dp") == [("b",)]
        q(pg_server, "app", "DELETE FROM probe_dp")
        with pytest.raises(psycopg.Error) as denied:
            q(pg_server, "app", "CREATE TABLE app_ddl (x int)")
        assert denied.value.sqlstate == "42501"  # insufficient_privilege
    finally:
        q(pg_server, "migrator", "DROP TABLE probe_dp")


def test_restore_role_scratch_db_and_no_prod_read(pg_server: PgServer) -> None:
    q(pg_server, "restore", "CREATE DATABASE jh_scratch TEMPLATE jobhunter_template", db="postgres")
    q(pg_server, "restore", "DROP DATABASE jh_scratch", db="postgres")
    q(pg_server, "migrator", "CREATE TABLE probe_ro (x int)")
    try:
        # No rights on the production database (spec 7.1): not even to connect to it.
        with pytest.raises(psycopg.OperationalError, match="permission denied for database"):
            q(pg_server, "restore", "SELECT * FROM probe_ro")
        assert q(
            pg_server, "superuser", "SELECT has_table_privilege('jobhunter_restore', 'probe_ro', 'SELECT')"
        ) == [(False,)]
        for role, privilege, allowed in [
            ("jobhunter_app", "CONNECT", True),
            ("jobhunter_backup", "CONNECT", True),
            ("jobhunter_restore", "CONNECT", False),
            ("jobhunter_restore", "TEMP", False),
        ]:
            has = f"SELECT has_database_privilege('{role}', 'jobhunter', '{privilege}')"
            assert q(pg_server, "superuser", has) == [(allowed,)], (role, privilege)
    finally:
        q(pg_server, "migrator", "DROP TABLE probe_ro")


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("log_statement", "none"),
        ("log_min_duration_statement", "-1"),
        ("log_parameter_max_length", "0"),
        ("log_parameter_max_length_on_error", "0"),
        ("log_min_error_statement", "panic"),
        ("log_error_verbosity", "terse"),
        ("idle_in_transaction_session_timeout", "1min"),
    ],
)
def test_server_settings(pg_server: PgServer, name: str, value: str) -> None:
    assert q(pg_server, "superuser", f"SHOW {name}") == [(value,)]


def _logs_with(server: PgServer, needle: str, timeout_s: float = 5.0) -> str:
    """The container log once `needle` is in it: `docker logs` can lag the server by a moment."""
    deadline = time.monotonic() + timeout_s
    while True:
        logs = server.logs()
        if needle in logs or time.monotonic() >= deadline:
            return logs
        time.sleep(0.1)


def test_l1_postgres_logs_contain_no_values(pg_server: PgServer) -> None:
    email, phone = "max.mustermann@example.org", "+49 170 1234567"
    q(pg_server, "migrator", "CREATE TABLE l1_probe (email text UNIQUE, phone text)")
    try:
        with psycopg.connect(pg_server.dsn("app")) as c:
            c.execute("INSERT INTO l1_probe VALUES (%s, %s)", (email, phone))
            c.commit()
            c.execute("SELECT pg_sleep(0.3), %s::text", (phone,))
            c.commit()
            with pytest.raises(psycopg.Error) as dup:
                c.execute("INSERT INTO l1_probe VALUES (%s, %s)", (email, phone))
            assert dup.value.sqlstate == "23505"
        logs = _logs_with(pg_server, "duplicate key")
        assert "duplicate key" in logs and email not in logs and "170 1234567" not in logs
    finally:
        q(pg_server, "migrator", "DROP TABLE l1_probe")
