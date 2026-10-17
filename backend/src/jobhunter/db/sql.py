"""SQL helpers: JSONB-typed bind parameters and classification of database errors.

Database errors that pass through SQLAlchemy are `sqlalchemy.exc.DBAPIError` subclasses. `pg_error` is the
only code that looks beneath one, to read the SQLSTATE and the constraint name from the driver's exception.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

import psycopg
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql.elements import TextClause

UNIQUE_VIOLATION: Final = "23505"
CHECK_VIOLATION: Final = "23514"
UNDEFINED_TABLE: Final = "42P01"
LOCK_NOT_AVAILABLE: Final = "55P03"


def q(statement: str, *, jsonb: Iterable[str] = ()) -> TextClause:
    """`sa.text(statement)` with the named parameters in `jsonb` bound as `JSONB`.

    Every JSON value that reaches SQL goes through such a parameter, never through a string.
    """
    clause = sa.text(statement)
    names = tuple(jsonb)
    if not names:
        return clause
    return clause.bindparams(*(sa.bindparam(name, type_=JSONB) for name in names))


@dataclass(frozen=True)
class PgErrorInfo:
    sqlstate: str | None
    constraint: str | None


def pg_error(exc: sa.exc.DBAPIError) -> PgErrorInfo:
    """The SQLSTATE and the violated constraint (for a unique index: the index name) of a wrapped error."""
    orig = exc.orig
    if isinstance(orig, psycopg.Error):
        return PgErrorInfo(orig.sqlstate, orig.diag.constraint_name)
    return PgErrorInfo(None, None)


def is_unique_violation(exc: sa.exc.DBAPIError, constraint: str) -> bool:
    info = pg_error(exc)
    return info.sqlstate == UNIQUE_VIOLATION and info.constraint == constraint
