import pytest
import sqlalchemy as sa
from jobhunter.db.session import make_null_engine
from jobhunter.db.sql import (
    UNDEFINED_TABLE,
    UNIQUE_VIOLATION,
    PgErrorInfo,
    is_serialization_failure,
    is_unique_violation,
    pg_error,
)

pytestmark = pytest.mark.integration
ALERT = sa.text("INSERT INTO alerts (code,severity,dedup_key,message) VALUES ('a','warn','sql-errors','m')")


async def test_wrapped_errors_are_classified_and_savepoints_keep_the_transaction(migrated) -> None:
    engine = make_null_engine(migrated.dsn("app", driver="sqlalchemy"))
    async with engine.connect() as c:
        with pytest.raises(sa.exc.ProgrammingError) as missing:
            async with c.begin_nested():
                await c.execute(sa.text("SELECT 1 FROM no_such_table"))
        assert pg_error(missing.value).sqlstate == UNDEFINED_TABLE
        await c.execute(ALERT)  # usable: only the savepoint rolled back
        with pytest.raises(sa.exc.IntegrityError) as dup:
            async with c.begin_nested():
                await c.execute(ALERT)
        assert pg_error(dup.value) == PgErrorInfo(UNIQUE_VIOLATION, "alerts_open_dedup")
        assert is_unique_violation(dup.value, "alerts_open_dedup") and not is_unique_violation(
            dup.value, "runs_slot_uq"
        )
        await c.rollback()
    await engine.dispose()


async def test_only_serialization_failures_and_deadlocks_are_retryable(migrated) -> None:
    """`is_serialization_failure`: SQLSTATE 40001 and 40P01, which always rolled back; nothing else, and never
    an error whose connection was invalidated (its commit outcome is unknown)."""
    engine = make_null_engine(migrated.dsn("app", driver="sqlalchemy"))
    raised: dict[str, sa.exc.DBAPIError] = {}
    async with engine.connect() as c:
        for code in ("40001", "40P01", "08006", "23505", "57014"):
            with pytest.raises(sa.exc.DBAPIError) as info:
                async with c.begin_nested():
                    await c.execute(
                        sa.text(f"DO $$ BEGIN RAISE EXCEPTION 'injected' USING ERRCODE = '{code}'; END $$")
                    )
            raised[code] = info.value
        await c.rollback()
    await engine.dispose()
    assert {code: is_serialization_failure(exc) for code, exc in raised.items()} == {
        "40001": True,
        "40P01": True,
        "08006": False,
        "23505": False,
        "57014": False,
    }
    raised["40001"].connection_invalidated = True
    assert not is_serialization_failure(raised["40001"])
    assert not is_serialization_failure(RuntimeError("40001"))
