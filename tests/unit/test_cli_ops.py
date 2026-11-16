"""The V15 operations commands (spec §17.6, §17.7; plan M5 Task 5, O-ops Q14), run as the worker role:
`jobhunter ops notify-test` pushes one test notification to ntfy, and `jobhunter ops deadman-sync --show
[--interval-hours N]` applies the dead-man check's period (from `schedule_state`, or from N when no database
is reachable) and prints it as read back. Each prints one JSON line on stdout and exits 0, or prints the
error's code and exits 1. Neither ever prints the topic URL, the ping URL, the check's uuid, the ntfy token or
the API key, on any path.

respx serves ntfy and healthchecks (no network); the database of `deadman-sync` is a port nothing listens
on. Every secret is a low-entropy stand-in.
"""

import json
import socket
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Final

import httpx
import pytest
import respx
from jobhunter import cli

from tests.support.healthchecks import API_KEY, API_URL, CHECK_UUID, PING_URL, FakeCheck, fake_healthchecks

TOPIC: Final = "https://ntfy.test/jh-alerts-4f2a9c"
TOKEN: Final = "tk-test-token"
ORIGIN: Final = "https://jobs.example.test"
SECRET_TEXTS: Final = (TOPIC, "jh-alerts-4f2a9c", TOKEN, PING_URL, CHECK_UUID, API_KEY, "hc-ping")


def free_port() -> int:
    """A local port nothing listens on: a connection to it is refused at once."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


@pytest.fixture
def answering_port() -> Iterator[int]:
    """A local port where a server answers every connection by closing it: a database server that refuses,
    as one that refuses the password or the database does before a session (psycopg: no SQLSTATE)."""
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
def worker_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """`worker_env(**secrets)`: the process environment of a test worker whose database nobody serves, with
    the secrets given (and its two required ones); no topic and no healthchecks API until a test sets them."""

    def setup(**secrets: str) -> None:
        directory = tmp_path / "secrets"
        directory.mkdir(exist_ok=True)
        for name, value in {"db_app": "pw", "data_key": "a" * 43 + "=", **secrets}.items():
            (directory / name).unlink(missing_ok=True)  # replaced, not edited: non-root cannot write 0440
            (directory / name).write_text(value)
            (directory / name).chmod(0o440)
        env = {
            "JOBHUNTER_ENV": "test",
            "JOBHUNTER_DB_HOST": "127.0.0.1",
            "JOBHUNTER_DB_PORT": str(free_port()),
            "JOBHUNTER_DB_NAME": "jobhunter",
            "JOBHUNTER_PUBLIC_ORIGIN": ORIGIN,
            "JOBHUNTER_SECRETS_DIR": str(directory),
            "HEALTHCHECKS_API_URL": API_URL,
        }
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        monkeypatch.delenv("NTFY_TOPIC_URL", raising=False)

    return setup


def ran(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, Any]]:
    """`jobhunter <argv>`'s exit status and the one JSON line it printed on stdout; nothing it printed, on
    stdout or stderr, holds a secret."""
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    for text in SECRET_TEXTS:
        assert text not in out and text not in err, text
    [line] = out.splitlines()
    printed: dict[str, Any] = json.loads(line)
    return code, printed


def test_the_ops_commands_parse() -> None:
    parser = cli._build_parser()
    assert parser.parse_args(["ops", "notify-test"]).ops_command == "notify-test"
    args = parser.parse_args(["ops", "deadman-sync", "--show", "--interval-hours", "6"])
    assert (args.ops_command, args.show, args.interval_hours) == ("deadman-sync", True, 6)
    for hours in ("0", "169", "x"):  # schedule_state's interval: 1 h to 7 d
        with pytest.raises(SystemExit):
            parser.parse_args(["ops", "deadman-sync", "--interval-hours", hours])


def test_notify_test_prints_no_secret(
    worker_env: Callable[..., None], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    worker_env(ntfy_token=TOKEN)
    assert ran(capsys, "ops", "notify-test") == (1, {"sent": False, "error": "not_configured"})

    monkeypatch.setenv("NTFY_TOPIC_URL", TOPIC)
    with respx.mock(assert_all_mocked=True) as router:
        topic = router.post(TOPIC).mock(return_value=httpx.Response(200))
        assert ran(capsys, "ops", "notify-test") == (0, {"sent": True})
        [call] = topic.calls
        # one push of the test alert, at warn's priority 3 (Q14: "Job Hunter - test")
        assert call.request.content == b"Job Hunter test notification"
        headers = call.request.headers
        assert (headers["Title"], headers["Priority"], headers["Tags"]) == ("Job Hunter - test", "3", "warn")
        assert headers["Click"] == f"{ORIGIN}/settings#alerts"
        assert headers["Authorization"] == f"Bearer {TOKEN}"

        # one try, and a refusal names its code only
        failures: list[tuple[httpx.Response | Exception, str]] = [
            (httpx.Response(503), "http_503"),
            (httpx.ConnectError(f"refused {TOPIC} with {TOKEN}"), "connect"),
            (httpx.ReadTimeout(f"timed out {TOPIC}"), "timeout"),
        ]
        for outcome, code in failures:
            topic.reset()
            topic.mock(side_effect=[outcome])
            assert ran(capsys, "ops", "notify-test") == (1, {"sent": False, "error": code})
            assert topic.call_count == 1


def test_deadman_sync_show_prints_only_the_period(
    worker_env: Callable[..., None], capsys: pytest.CaptureFixture[str]
) -> None:
    worker_env()
    assert ran(capsys, "ops", "deadman-sync", "--show") == (1, {"error": "not_configured"})
    worker_env(healthchecks_ping_url=PING_URL)  # one of the two secrets is not enough
    assert ran(capsys, "ops", "deadman-sync", "--show") == (1, {"error": "not_configured"})

    worker_env(healthchecks_ping_url=f"{PING_URL}\n", healthchecks_api_key=f"{API_KEY}\n")
    with fake_healthchecks(FakeCheck(timeout=86_400, grace=60, status="paused")) as (check, router):
        # no database and no period given: nothing to apply, nothing sent
        assert ran(capsys, "ops", "deadman-sync", "--show") == (1, {"error": "no_period"})
        assert router.calls.call_count == 0

        # no database server: the period of `--interval-hours`, applied, the paused check resumed by a ping
        printed = ran(capsys, "ops", "deadman-sync", "--show", "--interval-hours", "6")
        assert printed == (
            0,
            {"timeout_s": 21_600, "grace_s": 5_400, "paused": False, "source": "interval_hours"},
        )
        assert check.updates == [{"timeout": 21_600, "grace": 5_400}] and check.pings == ["success"]
        assert all(check.api_keys_right)

        # in sync already: read back, nothing changed; without `--show` nothing but the exit status
        assert ran(capsys, "ops", "deadman-sync", "--show", "--interval-hours", "6")[1]["timeout_s"] == 21_600
        assert len(check.updates) == 1
        assert cli.main(["ops", "deadman-sync", "--interval-hours", "6"]) == 0
        assert capsys.readouterr() == ("", "")

        # an API failure prints its code
        check.api_status = 401
        assert ran(capsys, "ops", "deadman-sync", "--show", "--interval-hours", "24") == (
            1,
            {"error": "http_401"},
        )

    worker_env(
        healthchecks_ping_url="https://hc-ping.test/pingkeyxyz/collection", healthchecks_api_key=API_KEY
    )
    assert ran(capsys, "ops", "deadman-sync", "--show", "--interval-hours", "6") == (
        1,
        {"error": "ping_url_form"},
    )


def test_a_database_server_that_answers_is_never_bypassed(
    worker_env: Callable[..., None],
    answering_port: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Fix round 1, M4: `--interval-hours` stands in for `schedule_state` only when no server answered; a
    server that answered with a refusal fails the command (`database`), and nothing goes to healthchecks."""
    worker_env(healthchecks_ping_url=PING_URL, healthchecks_api_key=API_KEY)
    monkeypatch.setenv("JOBHUNTER_DB_PORT", str(answering_port))
    with fake_healthchecks() as (_, router):
        for argv in (("--show",), ("--show", "--interval-hours", "6")):
            assert ran(capsys, "ops", "deadman-sync", *argv) == (1, {"error": "database"})
        assert router.calls.call_count == 0
