"""scripts/smoke_m0.py (Task 21): the not-fresh hint and the psql call, with `subprocess` stubbed."""

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/smoke_m0.py"
CLI_USER_EXISTS = "a user already exists; there is only one (`user set-password` changes the password)\n"


@pytest.fixture(scope="module")
def smoke() -> ModuleType:
    spec = importlib.util.spec_from_file_location("smoke_m0", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stub_run(
    smoke: ModuleType, monkeypatch: pytest.MonkeyPatch, *, stderr: str = "", returncode: int = 1
) -> dict[str, Any]:
    """Replace the script's `subprocess.run`; return the keyword arguments of the last call."""
    seen: dict[str, Any] = {}

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.update(kwargs)
        return subprocess.CompletedProcess(args, returncode, "", stderr)

    monkeypatch.setattr(smoke, "subprocess", SimpleNamespace(run=run))
    return seen


def test_the_cli_s_own_message_means_the_stack_is_not_fresh(smoke, monkeypatch) -> None:
    stub_run(smoke, monkeypatch, stderr=CLI_USER_EXISTS)
    with pytest.raises(smoke.CheckFailed, match="stack not fresh"):
        smoke.check_user_create(smoke.Context(client=None))


def test_another_already_exists_error_is_reported_as_it_is(smoke, monkeypatch) -> None:
    stub_run(
        smoke, monkeypatch, stderr="Error response from daemon: network jobhunter-dev_proxy already exists\n"
    )
    with pytest.raises(smoke.CheckFailed) as failed:
        smoke.check_user_create(smoke.Context(client=None))
    assert "stack not fresh" not in str(failed.value) and "network jobhunter-dev_proxy" in str(failed.value)


def test_psql_decodes_its_output_as_utf_8(smoke, monkeypatch) -> None:
    seen = stub_run(smoke, monkeypatch, returncode=0)
    smoke.psql("SELECT 1")
    assert (seen["text"], seen["encoding"]) == (True, "utf-8")
