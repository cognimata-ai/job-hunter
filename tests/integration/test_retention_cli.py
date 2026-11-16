"""`jobhunter retention run` (spec §19.8 "Disk pressure", §20.1; M5 follow-ups Task F4 (a), Review Focus 4):
one retention pass now, as the worker role, beside a running worker.

The command runs `privacy.retention.run_retention_daily`, the pass of the scheduled `retention_daily`, once,
and prints its counts on one JSON line. It reads the current worker epoch and fences every write of the pass
with it, as the worker fences its own nightly pass: a takeover that commits first stops it before its first
write (`fenced_out`), and without any epoch (no worker ever started) it writes nothing (`no_worker_epoch`). A
rule that fails is counted `-1` and raises `retention.failed`, as in the scheduled pass, and the command exits
1. Nothing printed holds the database password or the DSN.

The worker's configuration is the harness's (`server_env`) with the test's CV store. The database is seeded
and read with psycopg as the application role: the command runs its own event loop.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest
from jobhunter import cli
from jobhunter.db.sql import q
from jobhunter.privacy import retention
from jobhunter.privacy.retention import RULES
from jobhunter.worker import epoch as worker_epoch
from sqlalchemy.ext.asyncio import AsyncConnection

from tests.support.pg import PgServer, pg_one, pg_rows, server_env

pytestmark = pytest.mark.integration

EPOCH: Final = 4  # the current epoch the tests set, so a pass under the fixture's epoch 1 would be refused
_SET_EPOCH: Final = "UPDATE worker_heartbeat SET worker_epoch = %s WHERE id = 1"
_ATTEMPT: Final = (
    "INSERT INTO login_attempts (at, ip, username_hmac, success) VALUES (%s, '198.51.100.1', '\\x00', false)"
)
_ALERT: Final = (
    "INSERT INTO alerts (code, severity, dedup_key, message, resolved_at) VALUES ('a', 'info', %s, 'm', %s)"
)
_FAILED_ALERTS: Final = (
    "SELECT dedup_key FROM alerts WHERE code = 'retention.failed' AND resolved_at IS NULL ORDER BY dedup_key"
)


def seed(server: PgServer) -> None:
    """A login attempt 31 days old and one 29 days old, an alert resolved 91 days ago and one 89 days ago:
    the first of each pair is past its rule's window (30 and 90 days)."""
    now = datetime.now(UTC)
    for days in (31, 29):
        pg_rows(server, "app", _ATTEMPT, now - timedelta(days=days))
    for key, days in (("old", 91), ("new", 89)):
        pg_rows(server, "app", _ALERT, key, now - timedelta(days=days))


def seeded_left(server: PgServer) -> tuple[int, list[str]]:
    """How many login attempts are left, and the dedup keys of the alerts `seed` inserted that are left."""
    attempts = pg_one(server, "app", "SELECT count(*) FROM login_attempts")
    keys = pg_rows(server, "app", "SELECT dedup_key FROM alerts WHERE code = 'a' ORDER BY dedup_key")
    assert attempts is not None
    return int(str(attempts[0])), [str(row[0]) for row in keys]


def worker_env(
    server: PgServer, cv_store_dir: Path, monkeypatch: pytest.MonkeyPatch, **env: str
) -> list[str]:
    """The process environment of the CLI as the worker against `server`; the texts no output may hold."""
    values = server_env(server, "worker", JOBHUNTER_CV_STORE_DIR=str(cv_store_dir), **env)
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("NTFY_TOPIC_URL", raising=False)
    return [server.passwords["app"], "postgresql"]


def retention_run(capsys: pytest.CaptureFixture[str], secrets: list[str]) -> tuple[int, dict[str, Any]]:
    """`jobhunter retention run`'s exit status and its one JSON line; nothing printed holds one of
    `secrets`."""
    code = cli.main(["retention", "run"])
    out, err = capsys.readouterr()
    for text in secrets:
        assert text not in out and text not in err
    [line] = out.splitlines()
    printed: dict[str, Any] = json.loads(line)
    return code, printed


def fenced_epochs(monkeypatch: pytest.MonkeyPatch, *, takeover: PgServer | None = None) -> list[int]:
    """The epoch each of the pass's own fences proves (`retention.fence_epoch`), in order. With `takeover`,
    a newer worker claims the next epoch, and commits, just before the first fence is taken."""
    fence = worker_epoch.fence_epoch
    seen: list[int] = []

    async def spied(conn: AsyncConnection, epoch: int) -> None:
        if takeover is not None and not seen:
            pg_rows(
                takeover, "app", "UPDATE worker_heartbeat SET worker_epoch = worker_epoch + 1 WHERE id = 1"
            )
        seen.append(epoch)
        await fence(conn, epoch)

    monkeypatch.setattr(retention, "fence_epoch", spied)
    return seen


def test_the_retention_command_parses() -> None:
    args = cli._build_parser().parse_args(["retention", "run"])
    assert (args.command, args.retention_command) == ("retention", "run")
    with pytest.raises(SystemExit):
        cli._build_parser().parse_args(["retention"])


def test_it_runs_one_pass_under_the_current_epoch_and_prints_its_counts(
    migrated: PgServer,
    current_epoch: int,
    cv_store_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every rule of `RULES` runs, in order, each fenced by the current epoch (its transactions and its alert
    write, as in the worker's pass); the expired rows go, the others stay; the output is the counts by rule,
    numbers only, and the command exits 0."""
    secrets = worker_env(migrated, cv_store_dir, monkeypatch)
    pg_rows(migrated, "app", _SET_EPOCH, EPOCH)
    seed(migrated)
    epochs = fenced_epochs(monkeypatch)

    code, printed = retention_run(capsys, secrets)

    assert code == 0, printed
    assert list(printed) == ["counts"]
    counts = printed["counts"]
    assert list(counts) == [rule.name for rule in RULES]
    assert all(isinstance(count, int) and count >= 0 for count in counts.values()), counts
    assert (counts["login_attempts"], counts["alerts"]) == (1, 1)
    assert seeded_left(migrated) == (1, ["new"])
    assert set(epochs) == {EPOCH} and len(epochs) >= len(RULES)
    assert pg_rows(migrated, "app", _FAILED_ALERTS) == []


def test_a_takeover_before_its_first_write_stops_it(
    migrated: PgServer,
    current_epoch: int,
    cv_store_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A newer worker claims an epoch after the command read the current one: the pass's first fence refuses
    it (as the nightly pass of a superseded worker is refused), it writes nothing, no rule and no alert, and
    the command prints `fenced_out` and exits 1."""
    secrets = worker_env(migrated, cv_store_dir, monkeypatch)
    pg_rows(migrated, "app", _SET_EPOCH, EPOCH)
    seed(migrated)
    epochs = fenced_epochs(monkeypatch, takeover=migrated)

    assert retention_run(capsys, secrets) == (1, {"error": "fenced_out"})

    assert epochs == [EPOCH]
    assert seeded_left(migrated) == (2, ["new", "old"])
    assert pg_rows(migrated, "app", _FAILED_ALERTS) == []


def test_without_a_worker_epoch_it_writes_nothing(
    migrated: PgServer,
    cv_store_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No worker has claimed an epoch on this database (`worker_heartbeat` is empty): nothing can fence the
    pass, so it does not run."""
    secrets = worker_env(migrated, cv_store_dir, monkeypatch)
    seed(migrated)

    assert retention_run(capsys, secrets) == (1, {"error": "no_worker_epoch"})

    assert seeded_left(migrated) == (2, ["new", "old"])


def test_a_rule_that_fails_counts_minus_one_raises_its_alert_and_exits_1(
    migrated: PgServer,
    current_epoch: int,
    cv_store_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """As in the scheduled pass: the failed rule is reported `-1` and raises `retention.failed` (its name as
    the subject), and the rules after it still run; the command exits 1. The error's text is not printed."""
    secrets = worker_env(migrated, cv_store_dir, monkeypatch)
    seed(migrated)
    monkeypatch.setitem(retention._DELETES, "alerts", q("DELETE FROM no_such_table WHERE at < :cutoff"))

    code, printed = retention_run(capsys, [*secrets, "no_such_table"])

    assert code == 1
    counts = printed["counts"]
    assert counts["alerts"] == -1
    assert [name for name, count in counts.items() if count < 0] == ["alerts"]
    assert counts["login_attempts"] == 1 and counts["procrastinate_jobs"] >= 0
    assert pg_rows(migrated, "app", _FAILED_ALERTS) == [("retention.failed:alerts",)]


def test_a_database_that_does_not_answer_prints_database(
    migrated: PgServer,
    current_epoch: int,
    cv_store_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The worker's startup assertions pass without a database; the pass then finds none on the configured
    port and prints `database`, nothing else."""
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    secrets = worker_env(
        migrated, cv_store_dir, monkeypatch, JOBHUNTER_DB_HOST="127.0.0.1", JOBHUNTER_DB_PORT=str(port)
    )

    assert retention_run(capsys, secrets) == (1, {"error": "database"})
