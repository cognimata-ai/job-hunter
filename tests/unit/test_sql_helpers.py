import sqlalchemy as sa
from jobhunter.db.sql import PgErrorInfo, is_unique_violation, pg_error, q
from sqlalchemy.dialects.postgresql import JSONB


def test_q_binds_only_the_named_parameters_as_jsonb() -> None:
    binds = (
        q("INSERT INTO t (a, b) VALUES (:a, :b)", jsonb=["a"])
        .compile(dialect=sa.create_engine("postgresql+psycopg://u@h/d").dialect)
        .binds
    )
    assert isinstance(binds["a"].type, JSONB) and not isinstance(binds["b"].type, JSONB)


def test_q_without_jsonb_is_a_plain_text_clause() -> None:
    assert str(q("SELECT :x")) == str(sa.text("SELECT :x"))


def test_an_error_of_another_driver_is_unclassified() -> None:
    exc = sa.exc.DBAPIError("SELECT 1", {}, ValueError("not psycopg"))
    assert pg_error(exc) == PgErrorInfo(None, None)
    assert not is_unique_violation(exc, "alerts_open_dedup")
