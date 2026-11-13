"""`jobhunter backup ship`'s printed codes (`cli_backup`; the final re-review's M4-1): a database error on the
shipment lock's own connection prints `database`, as the module promises for every database error, and a
lock session that ended prints `ship_lock_lost`; neither prints `unexpected`, a message or a traceback.

The command runs as the backup role through `cli.main`, its database a local port where a server answers
every connection by closing it (psycopg's `OperationalError` on the lock's connection, before any session),
its repository a local directory nothing reads. Every secret is a low-entropy stand-in.
"""

import json
import socket
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from jobhunter import cli, cli_backup
from jobhunter.backup.shipper import ShipLockLost

SECRETS = {"db_backup": "pw-backup", "db_restore": "pw-restore", "restic_password": "pw-restic"}


@pytest.fixture
def answering_port() -> Iterator[int]:
    """A local port where a server answers every connection by closing it: a database server that refuses
    before a session (psycopg: an `OperationalError`, no SQLSTATE)."""
    server = socket.create_server(("127.0.0.1", 0))
    stop = threading.Event()

    def serve() -> None:
        server.settimeout(0.1)
        while not stop.is_set():
            try:
                accepted, _ = server.accept()
            except TimeoutError:
                continue
            accepted.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield int(server.getsockname()[1])
    finally:
        stop.set()
        thread.join(5)
        server.close()


@pytest.fixture
def backup_env(tmp_path: Path, answering_port: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """The process environment of the backup container, its database `answering_port`."""
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    for name, value in SECRETS.items():
        (secrets / name).write_text(value)
        (secrets / name).chmod(0o440)
    manifests, staging = tmp_path / "manifests", tmp_path / "staging"
    for directory in (manifests, staging):
        directory.mkdir(mode=0o700)
    env = {
        "JOBHUNTER_ENV": "test",
        "JOBHUNTER_DB_HOST": "127.0.0.1",
        "JOBHUNTER_DB_PORT": str(answering_port),
        "JOBHUNTER_DB_NAME": "jobhunter",
        "JOBHUNTER_SECRETS_DIR": str(secrets),
        "JOBHUNTER_MANIFESTS_DIR": str(manifests),
        "JOBHUNTER_STAGING_DIR": str(staging),
        "RESTIC_REPOSITORY": str(tmp_path / "repository"),
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)


def ran(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, Any]]:
    """`jobhunter <argv>`'s exit status and its one JSON line; no secret, no traceback."""
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    assert "Traceback" not in out + err
    for value in SECRETS.values():
        assert value not in out + err
    [line] = out.splitlines()
    printed: dict[str, Any] = json.loads(line)
    return code, printed


@pytest.mark.usefixtures("backup_env")
def test_a_database_error_on_the_lock_connection_prints_database(capsys: pytest.CaptureFixture[str]) -> None:
    """The shipment lock's connection is psycopg's own, so its error is no `SQLAlchemyError`: still
    `database`, never `unexpected`."""
    assert ran(capsys, "backup", "ship") == (1, {"error": "database"})


@pytest.mark.usefixtures("backup_env")
def test_a_lost_lock_session_prints_ship_lock_lost(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A lock session that ended during the shipment (a PostgreSQL restart, a cut connection): the shipper
    wrote nothing and stopped with `ShipLockLost`, which the CLI names as `ship_lock_lost`."""

    async def lost(*args: object, **kwargs: object) -> object:
        raise ShipLockLost

    monkeypatch.setattr(cli_backup, "ship_manifests", lost)
    assert ran(capsys, "backup", "ship") == (1, {"error": "ship_lock_lost"})
