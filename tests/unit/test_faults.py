# tests/unit/test_faults.py
import asyncio
import json
import os
import signal
import subprocess
import sys

import pytest
from jobhunter import faults

CODE = "from jobhunter.faults import fault_point; fault_point('p.x'); print('alive')"


def run(env: dict[str, str]) -> int:
    return subprocess.run([sys.executable, "-c", CODE], env={**os.environ, **env}).returncode


def test_fault_exits_86_in_test_env() -> None:
    assert run({"JOBHUNTER_ENV": "test", "JOBHUNTER_FAULT": "p.x"}) == 86


def test_fault_ignored_outside_test_env() -> None:
    assert run({"JOBHUNTER_ENV": "dev", "JOBHUNTER_FAULT": "p.x"}) == 0


def test_other_point_ignored() -> None:
    assert run({"JOBHUNTER_ENV": "test", "JOBHUNTER_FAULT": "p.y"}) == 0


# --- Additional coverage of behaviour the task brief specifies in prose -----------------------------------


def test_exit_code_constant() -> None:
    assert faults.FAULT_EXIT_CODE == 86


def test_pause_stops_the_process_until_it_is_continued() -> None:
    env = {**os.environ, "JOBHUNTER_ENV": "test", "JOBHUNTER_FAULT": "p.x:pause"}
    proc = subprocess.Popen([sys.executable, "-c", CODE], env=env, stdout=subprocess.PIPE, text=True)
    try:
        _, status = os.waitpid(proc.pid, os.WUNTRACED)  # returns once the child has been stopped
        assert os.WIFSTOPPED(status) and os.WSTOPSIG(status) == signal.SIGSTOP
        proc.send_signal(signal.SIGCONT)
        out, _ = proc.communicate()
    finally:
        proc.kill()
        proc.wait()
    assert out.strip() == "alive" and proc.returncode == 0


def test_sync_fault_point_does_not_act_on_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOBHUNTER_ENV", "test")
    monkeypatch.setenv("JOBHUNTER_FAULT", "p.x:sleep=30")
    faults.fault_point("p.x")  # returns at once: only the async variant sleeps


async def test_async_fault_point_sleeps(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setenv("JOBHUNTER_ENV", "test")
    monkeypatch.setenv("JOBHUNTER_FAULT", "p.x:sleep=2.5")
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await faults.afault_point("p.x")
    await faults.afault_point("p.y")  # another point: no sleep
    assert slept == [2.5]


async def test_async_fault_point_ignored_outside_test_env(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setenv("JOBHUNTER_ENV", "prod")
    monkeypatch.setenv("JOBHUNTER_FAULT", "p.x:sleep=2.5")
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await faults.afault_point("p.x")
    assert slept == []


def test_async_fault_point_exits_86_in_test_env() -> None:
    code = (
        "import asyncio\nfrom jobhunter.faults import afault_point\n"
        "asyncio.run(afault_point('p.x'))\nprint('alive')"
    )
    env = {**os.environ, "JOBHUNTER_ENV": "test", "JOBHUNTER_FAULT": "p.x"}
    assert subprocess.run([sys.executable, "-c", code], env=env).returncode == 86


@pytest.mark.parametrize("fault", ["p.x:explode", "p.x:sleep=soon", "p.x:sleep=-1"])
def test_a_malformed_armed_fault_is_loud(monkeypatch: pytest.MonkeyPatch, fault: str) -> None:
    monkeypatch.setenv("JOBHUNTER_ENV", "test")
    monkeypatch.setenv("JOBHUNTER_FAULT", fault)
    with pytest.raises(ValueError, match="JOBHUNTER_FAULT"):
        faults.fault_point("p.x")


# --- The readiness acknowledgement of the crash tests (Task 13, ruling P11) --------------------------------


async def test_async_fault_point_logs_the_point_before_it_acts(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    from tests.support.logs import logged

    logged_before_sleep: list[list[object]] = []

    async def fake_sleep(seconds: float) -> None:
        logged_before_sleep.append([event["point"] for event in logged(caplog, "fault_point_reached")])

    monkeypatch.setenv("JOBHUNTER_ENV", "test")
    monkeypatch.setenv("JOBHUNTER_FAULT", "p.x:sleep=2.5")
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await faults.afault_point("p.y")  # not armed here: nothing is logged
    await faults.afault_point("p.x")
    assert logged_before_sleep == [["p.x"]]
    (event,) = logged(caplog, "fault_point_reached")
    assert event["action"] == "sleep"


def test_async_fault_point_logs_the_point_before_it_exits() -> None:
    code = "import asyncio\nfrom jobhunter.faults import afault_point\nasyncio.run(afault_point('p.x'))"
    env = {**os.environ, "JOBHUNTER_ENV": "test", "JOBHUNTER_FAULT": "p.x"}
    done = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert done.returncode == 86
    events = [json.loads(line) for line in (done.stdout + done.stderr).splitlines() if line.startswith("{")]
    assert [(e["event"], e["point"], e["action"]) for e in events] == [("fault_point_reached", "p.x", "exit")]
