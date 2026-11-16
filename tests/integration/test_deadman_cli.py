"""`jobhunter ops deadman-sync` against a reachable database (plan M5 Task 5, O-ops Q14; fix round 1, M4 and
M7 (c)).

With a database server that answers, the command takes the dead-man lock, reads `schedule_state` and applies
its period, whatever `--interval-hours` says, and `--show` prints `"source": "schedule_state"`. A server that
answers with an error fails it with `database`: a refused password (no SQLSTATE while connecting, but a server
listening) and a query the database refuses (a SQLSTATE) alike; healthchecks is never called then. Nothing
printed holds a secret or the DSN.

The worker's configuration is the harness's (`server_env`) with a secrets directory of its own: the harness's
worker secrets, and low-entropy healthchecks stand-ins. respx serves healthchecks (no network).
"""

import json
import shutil
from pathlib import Path
from typing import Any, Final

import pytest
from jobhunter import cli, cli_ops

from tests.support.healthchecks import API_KEY, API_URL, CHECK_UUID, PING_URL, FakeCheck, fake_healthchecks
from tests.support.pg import PgServer, pg_one, server_env

pytestmark = pytest.mark.integration

_LOCK_HELD: Final = (  # 7261005, `deadman.DEADMAN_LOCK_KEY`
    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND objid = 7261005 AND granted)"
)


def worker_env(
    server: PgServer,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    db_app: str | None = None,
    **env: str,
) -> list[str]:
    """The process environment of the CLI as the worker against `server`, with both healthchecks secrets (and
    `db_app` in place of the harness's password when given); the texts no output may hold."""
    secrets = tmp_path / "secrets"
    secrets.mkdir(parents=True)
    for source in server.secrets_dir("worker").iterdir():
        shutil.copyfile(source, secrets / source.name)
    if db_app is not None:
        (secrets / "db_app").write_text(db_app)
    (secrets / "healthchecks_ping_url").write_text(PING_URL)
    (secrets / "healthchecks_api_key").write_text(API_KEY)
    for path in secrets.iterdir():
        path.chmod(0o440)
    values = {
        **server_env(server, "worker"),
        "JOBHUNTER_SECRETS_DIR": str(secrets),
        "HEALTHCHECKS_API_URL": API_URL,
    }
    for name, value in {**values, **env}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("NTFY_TOPIC_URL", raising=False)
    return [PING_URL, CHECK_UUID, API_KEY, server.passwords["app"], "postgresql"]


def ran(capsys: pytest.CaptureFixture[str], secrets: list[str], *argv: str) -> tuple[int, dict[str, Any]]:
    """`jobhunter ops deadman-sync <argv>`'s exit status and its one JSON line; nothing printed holds one of
    `secrets`."""
    code = cli.main(["ops", "deadman-sync", *argv])
    out, err = capsys.readouterr()
    for text in secrets:
        assert text not in out and text not in err
    [line] = out.splitlines()
    printed: dict[str, Any] = json.loads(line)
    return code, printed


def test_deadman_sync_reads_schedule_state_under_the_lock(
    migrated: PgServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secrets = worker_env(migrated, tmp_path, monkeypatch)
    seen_locked: list[object] = []

    def read(request: Any) -> Any:  # the check read happens under the dead-man lock
        seen_locked.append(pg_one(migrated, "app", _LOCK_HELD))
        return check.read(request)

    with fake_healthchecks(FakeCheck(timeout=999, grace=60, status="up")) as (check, router):
        router.routes["read"].mock(side_effect=read)
        # the seeded 6 h of `schedule_state`, though `--interval-hours` says 24
        assert ran(capsys, secrets, "--show", "--interval-hours", "24") == (
            0,
            {"timeout_s": 21_600, "grace_s": 5_400, "paused": False, "source": "schedule_state"},
        )
        assert check.updates == [{"timeout": 21_600, "grace": 5_400}] and check.pings == []
        assert seen_locked and set(seen_locked) == {(True,)}
        assert pg_one(migrated, "app", _LOCK_HELD) == (False,)  # released at the end

        # a disabled schedule pauses the check
        pg_one(migrated, "migrator", "UPDATE schedule_state SET enabled = false")
        assert ran(capsys, secrets, "--show") == (
            0,
            {"timeout_s": 21_600, "grace_s": 5_400, "paused": True, "source": "schedule_state"},
        )
        assert check.pauses == 1
    # the command records nothing: the worker's next sync stamps the row
    assert pg_one(migrated, "app", "SELECT synced_at, applied_interval_s FROM deadman_state") == (None, None)


def test_a_database_that_answers_with_an_error_is_never_bypassed(
    migrated: PgServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with fake_healthchecks() as (_, router):
        # a refused password: no SQLSTATE while connecting, but a server that answered
        secrets = worker_env(migrated, tmp_path / "password", monkeypatch, db_app="not-the-password")
        assert ran(capsys, secrets, "--show", "--interval-hours", "6") == (1, {"error": "database"})

        # a query the database refuses, with its SQLSTATE: the `postgres` database has no `schedule_state`;
        # the SQLSTATE alone decides, with no probe of the server's port
        async def no_probe(host: str, port: int) -> bool:
            raise AssertionError("a SQLSTATE needs no probe")

        monkeypatch.setattr(cli_ops, "_server_listens", no_probe)
        secrets = worker_env(migrated, tmp_path / "query", monkeypatch, JOBHUNTER_DB_NAME="postgres")
        assert ran(capsys, secrets, "--show", "--interval-hours", "6") == (1, {"error": "database"})
        assert router.calls.call_count == 0
    assert pg_one(migrated, "app", _LOCK_HELD) == (False,)
