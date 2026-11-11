"""`jobhunter api` (ruling T18-b): the startup assertions run first (exit 78), and only then uvicorn."""

import base64
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from jobhunter.cli import main
from jobhunter.config import ENV_PREFIXES

# What the compose api service listens on: every interface of its container.
ALL_INTERFACES = "0.0.0.0"  # noqa: S104
API_SECRETS = {
    "session_secret": base64.b64encode(b"s" * 32),
    "data_key": base64.b64encode(b"d" * 32),
    "db_app": b"pw",
}


@pytest.fixture
def uvicorn_calls(monkeypatch) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
    import uvicorn

    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    return calls


def api_env(monkeypatch, tmp_path: Path, **secrets: bytes) -> None:
    """The process environment of the api role with `secrets` as its secret files (mode 0440)."""
    for name in list(os.environ):
        if name.startswith(ENV_PREFIXES):
            monkeypatch.delenv(name)
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    for name, content in secrets.items():
        (secrets_dir / name).write_bytes(content)
        (secrets_dir / name).chmod(0o440)
    env = {
        "JOBHUNTER_ENV": "dev",
        "JOBHUNTER_DB_NAME": "jobhunter",
        "JOBHUNTER_PUBLIC_ORIGIN": "http://localhost:8080",
        "JOBHUNTER_SECRETS_DIR": str(secrets_dir),
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)


def test_a_failing_startup_assertion_exits_78_before_uvicorn_is_touched(
    monkeypatch, tmp_path, uvicorn_calls, capsys
) -> None:
    api_env(monkeypatch, tmp_path, **{**API_SECRETS, "session_secret": base64.b64encode(b"short")})
    with pytest.raises(SystemExit) as exc:
        main(["api", "--host", ALL_INTERFACES, "--port", "8000", "--proxy-headers"])
    assert exc.value.code == 78
    assert uvicorn_calls == []
    assert '"check": "session_secret_length"' in capsys.readouterr().out


def test_the_api_serves_the_app_with_the_given_options_once_the_assertions_pass(
    monkeypatch, tmp_path, uvicorn_calls
) -> None:
    api_env(monkeypatch, tmp_path, **API_SECRETS)
    argv = [
        "api",
        "--host",
        ALL_INTERFACES,
        "--port",
        "8000",
        "--proxy-headers",
        "--forwarded-allow-ips=172.30.0.2",
    ]
    assert main(argv) == 0
    assert uvicorn_calls == [
        (
            ("jobhunter.api.app:app",),
            {
                "host": ALL_INTERFACES,
                "port": 8000,
                "proxy_headers": True,
                "forwarded_allow_ips": "172.30.0.2",
                "log_config": None,
                "access_log": False,
                "ws": "none",
            },
        )
    ]


def test_without_options_the_api_listens_on_loopback_and_trusts_no_proxy_headers(
    monkeypatch, tmp_path, uvicorn_calls
) -> None:
    api_env(monkeypatch, tmp_path, **API_SECRETS)
    assert main(["api"]) == 0
    ((args, kwargs),) = uvicorn_calls
    assert args == ("jobhunter.api.app:app",)
    assert kwargs == {
        "host": "127.0.0.1",
        "port": 8000,
        "proxy_headers": False,
        "forwarded_allow_ips": None,
        "log_config": None,
        "access_log": False,
        "ws": "none",
    }


# A client address only this module's requests carry, sent as the trusted forwarder's X-Forwarded-For.
ADDRESS = "203.0.113.77"
PROBE_PATH = "/ws-probe?q=address-probe"
PLAIN = f"GET /healthz HTTP/1.1\r\nHost: api\r\nX-Forwarded-For: {ADDRESS}\r\nConnection: close\r\n\r\n"
# The sample nonce of RFC 6455 §1.3, computed here: the test needs a well-formed key, not a literal one.
WEBSOCKET_KEY = base64.b64encode(b"the sample nonce").decode("ascii")
UPGRADE = (
    f"GET {PROBE_PATH} HTTP/1.1\r\nHost: api\r\nX-Forwarded-For: {ADDRESS}\r\nConnection: Upgrade\r\n"
    f"Upgrade: websocket\r\nSec-WebSocket-Key: {WEBSOCKET_KEY}\r\nSec-WebSocket-Version: 13\r\n\r\n"
)
UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi")


@pytest.fixture
def uvicorn_loggers() -> Iterator[None]:
    """A real `uvicorn.Config` sets its loggers' levels, handlers and propagation: restore them after."""
    loggers = [logging.getLogger(name) for name in UVICORN_LOGGERS]
    state = [(lg.level, list(lg.handlers), lg.propagate) for lg in loggers]
    yield
    for lg, (level, handlers, propagate) in zip(loggers, state, strict=True):
        lg.setLevel(level)
        lg.handlers[:] = handlers
        lg.propagate = propagate


def status_line(port: int, request: str) -> str:
    """The status line the server answers `request` with, sent on a connection of its own."""
    with socket.create_connection(("127.0.0.1", port), timeout=10) as conn:
        conn.sendall(request.encode("ascii"))
        received = b""
        while b"\r\n" not in received:
            chunk = conn.recv(4096)
            if not chunk:
                break
            received += chunk
    return received.split(b"\r\n", 1)[0].decode("ascii")


def serve_and_probe(options: dict[str, Any]) -> tuple[list[str], list[logging.LogRecord]]:
    """The api served by uvicorn with `options` (port 0: an ephemeral one), sent a plain request and a
    WebSocket upgrade from `ADDRESS`; the two status lines, and every record of a `uvicorn` logger that
    reached the root logger, where the app's redacting handler writes them out."""
    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.name.split(".")[0] == "uvicorn":
                records.append(record)

    from jobhunter.api.app import create_app
    from jobhunter.config import load_config
    from jobhunter.db.session import make_engine

    cfg = load_config("api")
    app = create_app(cfg=cfg, engine=make_engine(cfg, pool_size=1))  # no request here touches the database
    server = uvicorn.Server(uvicorn.Config(app, **{**options, "host": "127.0.0.1", "port": 0}))
    capture = Capture()
    logging.getLogger().addHandler(capture)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 30
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline, "uvicorn did not start"
            time.sleep(0.05)
        port = server.servers[0].sockets[0].getsockname()[1]
        statuses = [status_line(port, PLAIN), status_line(port, UPGRADE)]
    finally:
        server.should_exit = True
        thread.join(timeout=30)
        logging.getLogger().removeHandler(capture)
    assert not thread.is_alive(), "uvicorn did not stop"
    return statuses, records


def naming_the_client(records: list[logging.LogRecord]) -> list[str]:
    """The records that hold the client address or the probed path, as `logger: message`."""
    found = []
    for record in records:
        text = f"{record.getMessage()} {record.args!r}"
        if ADDRESS in text or PROBE_PATH in text:
            found.append(f"{record.name}: {record.getMessage()}")
    return found


def test_uvicorn_logs_no_request_and_no_websocket_with_the_client_address(
    monkeypatch, tmp_path, uvicorn_calls, uvicorn_loggers
) -> None:
    """Rulings T21-1 (b) and T21-3 I1 (spec §20.6 item 2): served with `run_api`'s own settings, the api logs
    neither a plain request (no access log) nor a WebSocket upgrade (no WebSocket protocol: the upgrade is a
    plain request, a 404) with the client address, which uvicorn takes from the trusted X-Forwarded-For."""
    api_env(monkeypatch, tmp_path, **API_SECRETS)
    assert main(["api", "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1"]) == 0
    ((args, options),) = uvicorn_calls
    assert args == ("jobhunter.api.app:app",)
    statuses, records = serve_and_probe(options)
    assert statuses == ["HTTP/1.1 200 OK", "HTTP/1.1 404 Not Found"]
    assert records, "no uvicorn record was captured at all"  # its start and stop lines, at least
    assert naming_the_client(records) == []


def test_uvicorn_s_defaults_would_log_the_client_address(
    monkeypatch, tmp_path, uvicorn_calls, uvicorn_loggers
) -> None:
    """The control of the test above: with uvicorn's own defaults for the two settings, the same requests are
    logged with the address, the plain one on `uvicorn.access` and the upgrade (refused, 403) on
    `uvicorn.error`, so the capture sees what it is meant to catch."""
    api_env(monkeypatch, tmp_path, **API_SECRETS)
    assert main(["api", "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1"]) == 0
    ((_, options),) = uvicorn_calls
    statuses, records = serve_and_probe({**options, "access_log": True, "ws": "auto"})
    assert statuses == ["HTTP/1.1 200 OK", "HTTP/1.1 403 Forbidden"]
    named = naming_the_client(records)
    assert {line.split(":")[0] for line in named} == {"uvicorn.access", "uvicorn.error"}, named


def test_importing_the_cli_does_not_load_uvicorn_or_the_api_command() -> None:
    code = (
        "import sys, jobhunter.cli\n"
        "loaded = [m for m in ('uvicorn', 'jobhunter.cli_api', 'fastapi') if m in sys.modules]\n"
        "assert not loaded, loaded\n"
    )
    done = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
