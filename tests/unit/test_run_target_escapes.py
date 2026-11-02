"""Which errors escaping a score attempt `run_target` backs off as transient (Ruling T11-g)."""

import psycopg
import pytest
from jobhunter.errors import NodeFailed
from jobhunter.graphs.score.nodes.run_target import escaped_db_transient


def raw(sqlstate: str) -> psycopg.Error:
    """A raw psycopg error of the checkpointer's pool, of the class psycopg maps `sqlstate` to."""
    error_class: type[psycopg.Error] = psycopg.errors.lookup(sqlstate)  # noqa: TID251 (constructing, not classifying)
    return error_class("synthetic")


@pytest.mark.parametrize("sqlstate", ["40001", "40P01"])
def test_a_raw_serialization_failure_or_deadlock_is_transient(sqlstate: str) -> None:
    assert escaped_db_transient(raw(sqlstate))


def test_a_raw_lost_connection_is_transient() -> None:
    assert escaped_db_transient(psycopg.OperationalError("the server closed the connection"))


@pytest.mark.parametrize(
    "error",
    [
        raw("23505"),  # a unique violation: a bug, not a blip
        psycopg.ProgrammingError("syntax"),
        RuntimeError("a bug"),
        NodeFailed("load_context", "ValueError"),
    ],
)
def test_anything_else_is_not_transient(error: BaseException) -> None:
    assert not escaped_db_transient(error)


def test_the_stand_in_of_a_retryable_error_is_transient() -> None:
    assert escaped_db_transient(NodeFailed("load_context", "OperationalError", retryable_db_error=True))
