"""scripts/smoke_m1.py (Task 25): every compose call it makes layers the fixtures over the dev stack (no
provider is ever called), and the not-fresh hint names both files; `subprocess` stubbed."""

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
CLI_USER_EXISTS = "a user already exists; there is only one (`user set-password` changes the password)\n"


@pytest.fixture
def smoke(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.syspath_prepend(str(SCRIPTS))  # as when it runs as a script: `smoke_m0` beside it
    spec = importlib.util.spec_from_file_location("smoke_m1", SCRIPTS / "smoke_m1.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stub_run(smoke: ModuleType, monkeypatch: pytest.MonkeyPatch, *, stderr: str) -> list[list[str]]:
    """Replace the script's `subprocess.run` with one that fails with `stderr`; return the argument lists."""
    calls: list[list[str]] = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 1, "", stderr)

    monkeypatch.setattr(
        smoke, "subprocess", SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired)
    )
    return calls


def test_the_one_off_api_container_runs_on_the_fixtures_stack(smoke, monkeypatch) -> None:
    calls = stub_run(smoke, monkeypatch, stderr="boom\n")
    with pytest.raises(smoke.CheckFailed, match="boom"):
        smoke.check_user_create(smoke.Context(client=None))
    (args,) = calls
    files = [Path(args[i + 1]).name for i, arg in enumerate(args) if arg == "-f"]
    assert args[:4] == ["docker", "compose", "--env-file", str(SCRIPTS.parent / "deploy/images.lock")]
    assert files == ["compose.dev.yaml", "compose.fixtures.yaml"] and args[args.index("run") :][:4] == [
        "run",
        "--rm",
        "-T",
        "api",
    ]


def test_the_not_fresh_hint_names_the_fixtures_stack(smoke, monkeypatch) -> None:
    stub_run(smoke, monkeypatch, stderr=CLI_USER_EXISTS)
    with pytest.raises(smoke.CheckFailed, match=r"stack not fresh: .*compose\.fixtures\.yaml down -v"):
        smoke.check_user_create(smoke.Context(client=None))
