"""scripts/smoke_m1.py (Task 25): every compose call it makes layers the fixtures over the dev stack (no
provider is ever called), the not-fresh hint names both files, and the paid run is posted only once the
running worker is in fake mode; `subprocess` stubbed."""

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import httpx
import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
CLI_USER_EXISTS = "a user already exists; there is only one (`user set-password` changes the password)\n"


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def import_smoke(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """scripts/smoke_m1.py loaded as when it runs as a script (`smoke_m0` beside it on `sys.path`).

    Task 25 review, Minor 7: its `from smoke_m0 import …` must leave no cached `smoke_m0` behind. `delitem`
    drops one cached before (monkeypatch puts it back afterwards); `setitem` then records the entry as absent,
    so the copy loaded here is removed again when the test ends.
    """
    monkeypatch.syspath_prepend(str(SCRIPTS))
    monkeypatch.delitem(sys.modules, "smoke_m0", raising=False)
    monkeypatch.setitem(sys.modules, "smoke_m0", load("smoke_m0"))
    return load("smoke_m1")


@pytest.fixture
def smoke(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    return import_smoke(monkeypatch)


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


class Client:
    """The `httpx.Client` of `POST /api/runs`: records each post and answers a new run."""

    def __init__(self) -> None:
        self.posts: list[str] = []

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        self.posts.append(url)
        return httpx.Response(202, json={"run": {"id": 7}, "created": True})


def stub_printenv(
    smoke: ModuleType, monkeypatch: pytest.MonkeyPatch, *, returncode: int, stdout: str
) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, returncode, stdout, "")

    monkeypatch.setattr(
        smoke, "subprocess", SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired)
    )
    return calls


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [(1, ""), (0, "0\n"), (0, "not-the-flag-value\n")],  # unset, off, anything but `1`
)
def test_the_paid_run_is_not_posted_unless_the_worker_is_in_fake_mode(
    smoke, monkeypatch, returncode: int, stdout: str
) -> None:
    calls = stub_printenv(smoke, monkeypatch, returncode=returncode, stdout=stdout)
    client = Client()
    with pytest.raises(smoke.CheckFailed) as failed:
        smoke.check_run_now_paid(smoke.Context(client=client, cookie="c"))
    assert client.posts == []  # no run was posted
    assert "fake_sources=False" in str(failed.value)
    assert "not-the-flag-value" not in str(failed.value)  # the boolean only, never the value
    (args,) = calls
    files = [Path(args[i + 1]).name for i, arg in enumerate(args) if arg == "-f"]
    assert files == ["compose.dev.yaml", "compose.fixtures.yaml"]
    assert args[args.index("exec") :] == ["exec", "-T", "worker", "printenv", "JOBHUNTER_FAKE_SOURCES"]


def test_the_paid_run_is_posted_on_the_fixtures_worker(smoke, monkeypatch) -> None:
    stub_printenv(smoke, monkeypatch, returncode=0, stdout="1\n")
    client = Client()
    assert smoke.check_run_now_paid(smoke.Context(client=client, cookie="c")) == "fake_sources=True"
    assert client.posts == [f"{smoke.ORIGIN}/api/runs"] and smoke.STATE.paid_run_id == 7


def test_a_failing_exec_is_reported_without_the_variable(smoke, monkeypatch) -> None:
    stub_printenv(smoke, monkeypatch, returncode=125, stdout="")
    client = Client()
    with pytest.raises(smoke.CheckFailed, match="exit 125"):
        smoke.check_run_now_paid(smoke.Context(client=client, cookie="c"))
    assert client.posts == []


@pytest.mark.parametrize("cached", [False, True])
def test_loading_the_smoke_leaves_sys_modules_as_it_was(cached: bool) -> None:
    before = ModuleType("smoke_m0") if cached else None  # a copy some other test cached, or none
    with pytest.MonkeyPatch.context() as outer:
        if before is None:
            outer.delitem(sys.modules, "smoke_m0", raising=False)
        else:
            outer.setitem(sys.modules, "smoke_m0", before)
        with pytest.MonkeyPatch.context() as monkeypatch:
            smoke = import_smoke(monkeypatch)
            assert sys.modules["smoke_m0"] is not before  # the script's own, loaded from scripts/
            assert smoke.Context is sys.modules["smoke_m0"].Context
        assert sys.modules.get("smoke_m0") is before
