"""`jobhunter api` (ruling T18-b): the startup assertions run first (exit 78), and only then uvicorn."""

import base64
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
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
    }


def test_uvicorn_logs_no_request(monkeypatch, tmp_path, uvicorn_calls) -> None:
    """Ruling T21-1 (b): uvicorn's access log, a line per request with the client address, stays off, as Caddy
    keeps none (spec §20.6 item 2): the sign-in and re-authentication events alone record the address."""
    api_env(monkeypatch, tmp_path, **API_SECRETS)
    assert main(["api", "--proxy-headers", "--forwarded-allow-ips", "172.30.0.2"]) == 0
    ((_, kwargs),) = uvicorn_calls
    assert kwargs["access_log"] is False


def test_importing_the_cli_does_not_load_uvicorn_or_the_api_command() -> None:
    code = (
        "import sys, jobhunter.cli\n"
        "loaded = [m for m in ('uvicorn', 'jobhunter.cli_api', 'fastapi') if m in sys.modules]\n"
        "assert not loaded, loaded\n"
    )
    done = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
