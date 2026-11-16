"""The restic wrapper (`backup/restic.py`) on a fake runner, and the runner it uses (`backup/runner.py`), on
real child processes (plan M5 Task 10, R13, Deviation 30).

Every call names the host and the lock wait (restic 0.19.1 refuses `--host` on `init`, which has no snapshot
to name a host for); the repository password reaches the child as a file path, the S3 keys only in its
environment, never in its argv or this process's environment; a backup returns the summary's snapshot id; a
restore names its subfolder (`snapshotID:subfolder`), so the files land directly in the target;
`init_if_absent` initialises only a repository that restic reports absent (exit 10).
"""

import asyncio
import json
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import pytest
from jobhunter.backup.restic import (
    RESTIC_BIN,
    RESTIC_HOST,
    RETRY_LOCK,
    Restic,
    ResticError,
)
from jobhunter.backup.runner import STDERR_TAIL_BYTES, CommandResult, CommandTimeout, SubprocessRunner
from jobhunter.config import AppConfig, load_config

SNAPSHOT_ID: Final = "5f" * 32
KEY_ID: Final = "AKIDTESTNOTAKEY00"
SECRET_KEY: Final = "s3-secret-not-a-real-key"
PASSWORD: Final = "restic-password-not-real"
ENV: Final = {
    "JOBHUNTER_ENV": "test",
    "JOBHUNTER_DB_HOST": "h",
    "JOBHUNTER_DB_NAME": "jobhunter",
}


def write_secrets(directory: Path, **files: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name, value in files.items():
        (directory / name).unlink(missing_ok=True)  # replaced, not edited: a non-root owner cannot write 0440
        (directory / name).write_text(value)
        (directory / name).chmod(0o440)
    return directory


def backup_config(tmp_path: Path, *, s3: bool = False, **env: str) -> AppConfig:
    secrets = {"db_backup": "b", "db_restore": "r", "restic_password": PASSWORD}
    if s3:
        secrets |= {"s3_access_key_id": KEY_ID, "s3_secret_access_key": SECRET_KEY}
    directory = write_secrets(tmp_path / "secrets", **secrets)
    full = {**ENV, "JOBHUNTER_STAGING_DIR": str(tmp_path / "staging"), "RESTIC_REPOSITORY": "/repo", **env}
    return load_config("backup", full, directory)


@dataclass(frozen=True)
class Call:
    argv: tuple[str, ...]
    env: dict[str, str]
    timeout_s: float


@dataclass
class FakeRunner:
    """Answers each restic subcommand (`argv[1]`) with the next of its scripted results (exit 0 and no output
    when none is left), and records every call."""

    script: dict[str, list[CommandResult]] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)

    async def run(
        self, argv: Sequence[str], *, env: Mapping[str, str], timeout_s: float, stdin: bytes | None = None
    ) -> CommandResult:
        self.calls.append(Call(tuple(argv), dict(env), timeout_s))
        scripted = self.script.get(argv[1], [])
        return scripted.pop(0) if scripted else CommandResult(0, b"", "")

    def commands(self) -> list[str]:
        return [call.argv[1] for call in self.calls]


def exited(code: int, stdout: bytes = b"") -> CommandResult:
    return CommandResult(code, stdout, "stderr that names nothing")


def summary(snapshot_id: str = SNAPSHOT_ID) -> bytes:
    status = {"message_type": "status", "percent_done": 0.5, "total_files": 2}
    done = {"message_type": "summary", "files_new": 2, "total_files_processed": 2, "snapshot_id": snapshot_id}
    return f"{json.dumps(status)}\n{json.dumps(done)}\n".encode()


def value_of(argv: Sequence[str], flag: str) -> str:
    """The value after `flag`, which `argv` holds exactly once."""
    assert list(argv).count(flag) == 1, (flag, argv)
    return argv[argv.index(flag) + 1]


async def exercise_every_call(restic: Restic, tmp_path: Path) -> None:
    assert await restic.init_if_absent() is True
    await restic.backup(paths=[tmp_path / "file"], tags=["manifests"])
    await restic.restore(snapshot=SNAPSHOT_ID, subfolder="/data/manifests", target=tmp_path / "target")
    await restic.snapshots(tags=["manifests"])
    await restic.forget_snapshots([SNAPSHOT_ID])
    await restic.forget_prune(tag="set", keep_daily=7, keep_weekly=4, keep_monthly=12)


def every_call_script() -> dict[str, list[CommandResult]]:
    return {"snapshots": [exited(10), exited(0, b"[]")], "backup": [exited(0, summary())]}


async def test_every_call_passes_host_and_retry_lock(tmp_path: Path) -> None:
    runner = FakeRunner(every_call_script())
    await exercise_every_call(Restic(backup_config(tmp_path), runner), tmp_path)

    assert runner.commands() == ["snapshots", "init", "backup", "restore", "snapshots", "forget", "forget"]
    for call in runner.calls:
        assert call.argv[0] == RESTIC_BIN == "/usr/local/bin/restic"
        assert value_of(call.argv, "--retry-lock") == RETRY_LOCK == "30m"
        if call.argv[1] == "init":  # restic 0.19.1: `init --host` is "unknown flag: --host"
            assert "--host" not in call.argv
        else:
            assert value_of(call.argv, "--host") == RESTIC_HOST == "jobhunter-backup"
        assert call.timeout_s > 30 * 60  # longer than the lock wait itself
    forget = runner.calls[-1].argv
    assert value_of(forget, "--tag") == "set"
    assert [value_of(forget, f"--keep-{period}") for period in ("daily", "weekly", "monthly")] == [
        "7",
        "4",
        "12",
    ]
    assert "--prune" in forget


async def test_forget_snapshots_names_them_by_id(tmp_path: Path) -> None:
    """`forget_snapshots` forgets exactly the snapshots it names, by id (final fix C1; restic ignores the host
    filter for explicit ids), and prunes nothing itself. It names at least one, and only snapshot ids."""
    ids = ["a1" * 32, "b2" * 32]
    runner = FakeRunner()
    restic = Restic(backup_config(tmp_path), runner)
    await restic.forget_snapshots(ids)
    [call] = runner.calls
    assert call.argv == (RESTIC_BIN, "forget", "--retry-lock", RETRY_LOCK, "--host", RESTIC_HOST, "--", *ids)
    for wrong in ([], ["--prune"], ["a1" * 31]):
        with pytest.raises(ValueError):
            await restic.forget_snapshots(wrong)
    assert len(runner.calls) == 1


async def test_s3_keys_reach_the_child_env_only(tmp_path: Path) -> None:
    environ_before = dict(os.environ)
    cfg = backup_config(
        tmp_path,
        s3=True,
        RESTIC_REPOSITORY="s3:https://s3.example.test/bucket",
        AWS_DEFAULT_REGION="eu-central-1",
    )
    runner = FakeRunner(every_call_script())
    await exercise_every_call(Restic(cfg, runner), tmp_path)

    expected_env = {
        "RESTIC_REPOSITORY": "s3:https://s3.example.test/bucket",
        "RESTIC_PASSWORD_FILE": str(tmp_path / "secrets" / "restic_password"),
        "RESTIC_CACHE_DIR": str(tmp_path / "staging" / ".restic-cache"),
        "AWS_DEFAULT_REGION": "eu-central-1",
        "AWS_ACCESS_KEY_ID": KEY_ID,
        "AWS_SECRET_ACCESS_KEY": SECRET_KEY,
    }
    for call in runner.calls:
        assert call.env == expected_env  # exactly: nothing of this process's environment
        for value in (KEY_ID, SECRET_KEY, PASSWORD):
            assert not any(value in arg for arg in call.argv)
        assert PASSWORD not in call.env.values()  # the password by its file only
    assert dict(os.environ) == environ_before

    # A local repository without the S3 secrets: no AWS variable at all.
    local = FakeRunner(every_call_script())
    await exercise_every_call(Restic(backup_config(tmp_path / "local"), local), tmp_path)
    assert all(not any(name.startswith("AWS_") for name in call.env) for call in local.calls)


async def test_backup_parses_the_snapshot_id(tmp_path: Path) -> None:
    listing = tmp_path / "files.list"
    runner = FakeRunner({"backup": [exited(0, summary())]})
    restic = Restic(backup_config(tmp_path), runner)

    assert await restic.backup(files_from=listing, tags=["manifests", "set:12"]) == SNAPSHOT_ID
    argv = runner.calls[0].argv
    assert {"--json", "--quiet"} <= set(argv)
    # Verbatim: each line is a file name, never a pattern, and a missing file fails the backup (exit 3)
    # instead of being skipped (restic 0.19.1's `--files-from` skips a pattern that matches nothing, exit 0).
    assert value_of(argv, "--files-from-verbatim") == str(listing)
    assert [argv[i + 1] for i, arg in enumerate(argv) if arg == "--tag"] == ["manifests", "set:12"]

    paths = FakeRunner({"backup": [exited(0, summary())]})
    await Restic(backup_config(tmp_path), paths).backup(paths=[tmp_path / "a", tmp_path / "b"], tags=["x"])
    assert paths.calls[0].argv[-3:] == ("--", str(tmp_path / "a"), str(tmp_path / "b"))

    for result, code in (
        (exited(0, b'{"message_type":"status"}\n'), "no_snapshot_id"),  # no summary
        (exited(0, summary("not-an-id")), "no_snapshot_id"),
        (exited(3, summary()), "incomplete"),  # a source file could not be read
        (exited(1), "failed"),
        (exited(11), "locked"),
        (exited(12), "wrong_password"),
    ):
        failing = Restic(backup_config(tmp_path), FakeRunner({"backup": [result]}))
        with pytest.raises(ResticError) as refused:
            await failing.backup(files_from=listing, tags=["manifests"])
        assert (refused.value.command, refused.value.code) == ("backup", code)


async def test_restore_names_the_subfolder(tmp_path: Path) -> None:
    runner = FakeRunner()
    restic = Restic(backup_config(tmp_path), runner)

    await restic.restore(snapshot=SNAPSHOT_ID, subfolder="/data/manifests", target=tmp_path / "target")
    argv = runner.calls[0].argv
    assert argv[1] == "restore"
    assert value_of(argv, "--target") == str(tmp_path / "target")
    # `snapshotID:subfolder` (restic >= 0.17): the subfolder's content lands directly in the target.
    assert argv[-2:] == ("--", f"{SNAPSHOT_ID}:/data/manifests")

    failing = Restic(backup_config(tmp_path), FakeRunner({"restore": [exited(1)]}))
    with pytest.raises(ResticError) as refused:
        await failing.restore(snapshot=SNAPSHOT_ID, subfolder="/data/manifests", target=tmp_path / "target")
    assert (refused.value.command, refused.value.code) == ("restore", "failed")


async def test_snapshots_are_read_from_the_json_listing(tmp_path: Path) -> None:
    listing = [
        {
            "time": "2026-11-10T07:11:12.785474021Z",
            "paths": ["/data/manifests/a.json"],
            "hostname": "jobhunter-backup",
            "tags": ["manifests"],
            "id": SNAPSHOT_ID,
        },
        {"time": "2026-11-10T10:00:00.5+02:00", "paths": ["/data/staging/set"], "id": "a1" * 32},
    ]
    runner = FakeRunner({"snapshots": [exited(0, json.dumps(listing).encode())]})
    found = await Restic(backup_config(tmp_path), runner).snapshots(tags=["manifests", "set:3"])

    assert value_of(runner.calls[0].argv, "--tag") == "manifests,set:3"  # every tag (restic: comma = and)
    assert [(s.id, s.time.isoformat(), s.tags, s.paths) for s in found] == [
        (SNAPSHOT_ID, "2026-11-10T07:11:12.785474+00:00", ("manifests",), ("/data/manifests/a.json",)),
        ("a1" * 32, "2026-11-10T08:00:00.500000+00:00", (), ("/data/staging/set",)),
    ]
    untagged = FakeRunner({"snapshots": [exited(0, b"[]")]})
    assert await Restic(backup_config(tmp_path), untagged).snapshots(tags=()) == []
    assert "--tag" not in untagged.calls[0].argv

    garbled = Restic(backup_config(tmp_path), FakeRunner({"snapshots": [exited(0, b'[{"id": 1}]')]}))
    with pytest.raises(ResticError) as refused:
        await garbled.snapshots(tags=())
    assert refused.value.code == "unparsable"


async def test_a_snapshots_file_count_is_its_summarys(tmp_path: Path) -> None:
    """restic >= 0.17 stores a backup's summary in its snapshot, and the listing shows it: `files` is its
    `total_files_processed`, 0 for a snapshot that holds no file (restic's exit 3 with no listed file
    readable, final fix B8), and `None` when the listing carries no summary (a snapshot of an older restic).
    A count that is no integer is no listing restic wrote."""
    summaries: list[dict[str, object] | None] = [
        {"files_new": 2, "total_files_processed": 2},
        {"files_new": 0, "total_files_processed": 0},
        None,
    ]
    listing = [
        {
            "time": f"2026-11-10T07:0{n}:00Z",
            "paths": ["/data/manifests/a.json"],
            "id": key * 32,
            **({} if summary is None else {"summary": summary}),
        }
        for n, (key, summary) in enumerate(zip(("a1", "b2", "c3"), summaries, strict=True))
    ]
    runner = FakeRunner({"snapshots": [exited(0, json.dumps(listing).encode())]})

    found = await Restic(backup_config(tmp_path), runner).snapshots(tags=["manifests"])

    assert [(info.id, info.files) for info in found] == [("a1" * 32, 2), ("b2" * 32, 0), ("c3" * 32, None)]
    for count in ("2", True, -1):
        garbled = [{**listing[0], "summary": {"total_files_processed": count}}]
        restic = Restic(
            backup_config(tmp_path), FakeRunner({"snapshots": [exited(0, json.dumps(garbled).encode())]})
        )
        with pytest.raises(ResticError) as refused:
            await restic.snapshots(tags=())
        assert refused.value.code == "unparsable", count


async def test_snapshots_are_sorted_oldest_first(tmp_path: Path) -> None:
    """Whatever order restic prints, newest first here, and whatever offset each time carries: by time."""
    times = {
        "c3": "2026-11-12T01:00:00+00:00",  # the newest
        "a1": "2026-11-10T09:00:00+02:00",  # 07:00 UTC: the oldest, though its text sorts after b2's
        "b2": "2026-11-10T08:30:00Z",
        "d4": "2026-11-10T08:30:00+00:00",  # the same instant as b2: restic's order kept
    }
    listing = [{"time": time, "paths": ["/data/manifests"], "id": key * 32} for key, time in times.items()]
    runner = FakeRunner({"snapshots": [exited(0, json.dumps(listing).encode())]})

    found = await Restic(backup_config(tmp_path), runner).snapshots(tags=["manifests"])

    assert [info.id for info in found] == ["a1" * 32, "b2" * 32, "d4" * 32, "c3" * 32]
    assert [info.time for info in found] == sorted(info.time for info in found)


@pytest.mark.parametrize(
    ("probe", "init", "outcome", "commands"),
    [
        (exited(0, b"[]"), None, False, ["snapshots"]),  # it exists: never initialised again
        (exited(10), exited(0), True, ["snapshots", "init"]),  # restic: the repository does not exist
        (exited(12), None, "wrong_password", ["snapshots"]),  # it exists, with another password
        (exited(11), None, "locked", ["snapshots"]),
        (exited(1), None, "failed", ["snapshots"]),  # unreachable, say: never taken as absent
        (exited(10), exited(1), "failed", ["snapshots", "init"]),
    ],
)
async def test_init_if_absent_never_reinitialises(
    tmp_path: Path, probe: CommandResult, init: CommandResult | None, outcome: bool | str, commands: list[str]
) -> None:
    runner = FakeRunner({"snapshots": [probe], "init": [] if init is None else [init]})
    restic = Restic(backup_config(tmp_path), runner)
    if isinstance(outcome, bool):
        assert await restic.init_if_absent() is outcome
    else:
        with pytest.raises(ResticError) as refused:
            await restic.init_if_absent()
        assert refused.value.code == outcome
    assert runner.commands() == commands


def test_a_restic_without_a_repository_is_refused(tmp_path: Path) -> None:
    cfg = backup_config(tmp_path, RESTIC_REPOSITORY="")
    assert cfg.restic is None
    with pytest.raises(ValueError, match="RESTIC_REPOSITORY"):
        Restic(cfg, FakeRunner())


# --- the runner -----------------------------------------------------------------------------------------


async def test_the_subprocess_runner_gives_the_child_exactly_its_env() -> None:
    result = await SubprocessRunner().run(["/usr/bin/env"], env={"ONLY": "this"}, timeout_s=30)
    assert (result.returncode, result.stdout) == (0, b"ONLY=this\n")

    echoed = await SubprocessRunner().run(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"],
        env={},
        timeout_s=30,
        stdin=b"given on stdin",
    )
    assert echoed.stdout == b"given on stdin"


async def test_a_command_past_its_timeout_is_stopped(tmp_path: Path) -> None:
    marker = tmp_path / "still-running"
    code = f"import pathlib, time; time.sleep(3); pathlib.Path({str(marker)!r}).touch()"
    started = time.monotonic()
    with pytest.raises(CommandTimeout) as timed_out:
        await SubprocessRunner().run([sys.executable, "-c", code], env={}, timeout_s=0.5)
    assert time.monotonic() - started < 2.5
    assert timed_out.value.command == Path(sys.executable).name
    await asyncio.sleep(3)  # past the child's own end: it was stopped, so it never wrote the marker
    assert not marker.exists()


async def test_stderr_is_returned_as_a_tail_and_never_logged(
    log_events: Callable[[], list[dict[str, Any]]],
) -> None:
    code = "import sys; sys.stderr.write('x' * 5000 + 'TAIL-NAMES-A-SECRET'); sys.exit(3)"
    result = await SubprocessRunner().run([sys.executable, "-c", code], env={}, timeout_s=30)

    assert result.returncode == 3
    assert result.stderr_tail.endswith("TAIL-NAMES-A-SECRET")
    assert len(result.stderr_tail) == STDERR_TAIL_BYTES
    events = log_events()
    assert [(e["event"], e["command"], e["returncode"]) for e in events] == [
        ("command_finished", Path(sys.executable).name, 3)
    ]
    assert "TAIL-NAMES-A-SECRET" not in json.dumps(events)
