"""Integration test L1 (spec §16.8, §18.2): PostgreSQL's own log never carries a statement's bind parameters
or a failed row's values, which the application's redaction cannot reach (plan M5 Task 21).

On a server of its own with the repository's `postgresql.conf` (`log_parameter_max_length = 0`,
`log_parameter_max_length_on_error = 0`, `log_min_error_statement = panic`, `log_error_verbosity = terse`,
`log_statement = none`, `log_min_duration_statement = -1`), the application role inserts PII-shaped values, an
e-mail address and a phone number, through psycopg (server-side binding) in a slow statement, then the same
values again into a unique and into a check constraint. The client gets each violation's detail with the
values in it; the server's log gets each error's message, and neither value.
"""

import time
from typing import Final

import psycopg
import pytest

from tests.support.pg import PgServer

pytestmark = pytest.mark.integration
EMAIL: Final = "ada.lovelace.l1@example.org"
PHONE: Final = "+49 151 2345 6789"
# Run as the application role, with the values bound: a statement that takes a while (a count over five
# million generated rows), as a slow insert would. A server that logged statements over a duration would log
# it.
SLOW_INSERT: Final = (
    "INSERT INTO l1_contacts (email, phone, note) "
    "SELECT %s, %s, %s FROM (SELECT count(*) AS n FROM generate_series(1, 5000000)) AS slow WHERE slow.n > 0"
)
INSERT: Final = "INSERT INTO l1_contacts (email, phone, note) VALUES (%s, %s, %s)"
UNIQUE_ERROR: Final = 'duplicate key value violates unique constraint "l1_contacts_email_phone_key"'
CHECK_ERROR: Final = 'new row for relation "l1_contacts" violates check constraint "l1_contacts_note_check"'
LOG_WITHIN_S: Final = 10.0


def logged_after(server: PgServer, *messages: str) -> str:
    """The server's log once it holds every one of `messages` (the log is read as Docker has it)."""
    deadline = time.monotonic() + LOG_WITHIN_S
    while True:
        logs = server.logs()
        if all(message in logs for message in messages):
            return logs
        assert time.monotonic() < deadline, f"the server never logged {messages}:\n{logs[-2000:]}"
        time.sleep(0.1)


def test_neither_value_reaches_the_server_log(fresh_pg_server: PgServer) -> None:
    server = fresh_pg_server
    with psycopg.connect(server.dsn("migrator"), autocommit=True) as conn:  # the schema owner, as migrate
        conn.execute(
            "CREATE TABLE l1_contacts (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, "
            "email text NOT NULL, phone text NOT NULL, note text CHECK (note IS NULL), UNIQUE (email, phone))"
        )
    with psycopg.connect(server.dsn("app"), autocommit=True) as conn:  # the default privileges' DML grant
        started = time.monotonic()
        conn.execute(SLOW_INSERT, (EMAIL, PHONE, None))
        assert time.monotonic() - started > 0.1  # slow by any `log_min_duration_statement` worth setting
        with pytest.raises(psycopg.IntegrityError) as unique:
            conn.execute(INSERT, (EMAIL, PHONE, None))
        with pytest.raises(psycopg.IntegrityError) as check:
            conn.execute(INSERT, (f"{EMAIL}.second", PHONE, EMAIL))
    # The client is told the values (the error's detail), so the server had them in the error it logged.
    assert (unique.value.sqlstate, check.value.sqlstate) == ("23505", "23514")
    assert EMAIL in str(unique.value.diag.message_detail) and PHONE in str(unique.value.diag.message_detail)
    assert EMAIL in str(check.value.diag.message_detail) and PHONE in str(check.value.diag.message_detail)
    logs = logged_after(server, UNIQUE_ERROR, CHECK_ERROR)
    assert EMAIL not in logs and PHONE not in logs
    assert "l1_contacts (email" not in logs  # no statement text either (`log_min_error_statement = panic`)
