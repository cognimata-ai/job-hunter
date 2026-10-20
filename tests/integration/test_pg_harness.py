"""Tests of the PostgreSQL harness itself: a failed start leaves nothing behind, stale servers are removed."""

import os
import secrets
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from tests.support import pg
from tests.support.pg import pg_image, pg_rows, remove_pg_server, remove_stale_pg_servers, start_pg_server

pytestmark = pytest.mark.integration


def _docker_lines(*args: str) -> set[str]:
    done = subprocess.run(["docker", *args], capture_output=True, text=True, check=True)
    return set(done.stdout.split())


def _containers() -> set[str]:
    return _docker_lines("ps", "-a", "--filter", "name=jh-test-pg-", "--format", "{{.Names}}")


def _volumes() -> set[str]:
    return _docker_lines("volume", "ls", "-q")


@pytest.mark.parametrize(
    ("secret", "content", "mode", "expected"),
    [
        # The container's postgres user (uid 999) cannot read a mode-000 file, whoever owns it.
        ("db_app", "unused", 0o000, "secret db_app missing, empty or unreadable"),
        ("db_migrator", "", 0o444, "secret db_migrator missing, empty or unreadable"),
        # A newline-only file passes the shell check but yields an empty password: the SQL check refuses it.
        ("db_backup", "\n", 0o444, "login role without password: jobhunter_backup"),
    ],
    ids=["unreadable", "empty", "blank"],
)
def test_unusable_role_secret_fails_the_start_and_leaves_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, secret: str, content: str, mode: int, expected: str
) -> None:
    real_write = pg._write_secret

    def write(path: Path, value: str, file_mode: int) -> None:
        if path.parent.name == "pgsecrets" and path.name == secret:
            real_write(path, content, mode)
        else:
            real_write(path, value, file_mode)

    monkeypatch.setattr(pg, "_write_secret", write)
    containers, volumes = _containers(), _volumes()

    with pytest.raises(pytest.fail.Exception) as failed:
        start_pg_server(tmp_path / "pg")

    assert expected in str(failed.value)
    assert (_containers(), _volumes()) == (containers, volumes)


def _run_labelled(label_pid: int) -> str:
    name = f"jh-test-pg-stale{secrets.token_hex(4)}"
    command = [
        *("docker", "run", "-d", "--name", name, "--label", f"jh-test-pg.pid={label_pid}"),
        *("--entrypoint", "sleep", pg_image(), "600"),
    ]
    subprocess.run(command, capture_output=True, check=True)
    return name


def test_stale_servers_are_removed_but_never_one_of_a_live_process() -> None:
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    volumes = _volumes()
    stale, live = _run_labelled(gone.pid), _run_labelled(os.getpid())
    try:
        remove_stale_pg_servers()
        remaining = _containers()
        assert stale not in remaining and live in remaining
    finally:
        subprocess.run(["docker", "rm", "-f", "-v", stale, live], capture_output=True, check=False)
    assert _volumes() == volumes


def test_start_retries_when_the_chosen_port_is_taken(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    real_free_port = pg._free_port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        offers = [taken.getsockname()[1]]
        monkeypatch.setattr(pg, "_free_port", lambda: offers.pop() if offers else real_free_port())

        server = start_pg_server(tmp_path / "pg")
        try:
            assert server.port != taken.getsockname()[1]
            assert pg_rows(server, "app", "SELECT 1") == [(1,)]
        finally:
            remove_pg_server(server)


def test_the_worker_role_is_served_from_the_fixtures(make_config) -> None:
    worker, api = make_config("worker"), make_config("api")
    assert (worker.fake_sources, worker.fixtures_dir) == (True, pg.FIXTURES_DIR)
    assert api.fake_sources is False
