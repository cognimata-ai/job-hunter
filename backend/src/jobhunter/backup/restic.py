"""The restic wrapper of the backup and restore roles (spec §19.5; plan M5 Task 10, R13, Deviation 30), for
the pinned restic 0.19.1 (`deploy/images.lock`), at `RESTIC_BIN` in the backup image.

**Every call** runs `restic <command> --retry-lock 30m --host jobhunter-backup …`: one host for every
snapshot, whichever container made it, and a wait of up to 30 minutes for another run's lock (a prune's, say)
instead of a failure. `init` alone has no `--host` (restic refuses the flag there: a repository has no host).
The child's environment is exactly `RESTIC_REPOSITORY`, `RESTIC_PASSWORD_FILE` (the `restic_password` secret's
file, so the password itself is in no environment and no argv), `RESTIC_CACHE_DIR`, `AWS_DEFAULT_REGION` when
set, and `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` read from their secret files when both are present:
this process's environment never holds a key. The runner logs the command's name and return code only.

**The calls.** `init_if_absent` asks `snapshots` whether the repository exists and runs `init` only when
restic answers that it does not (exit 10): any other failure (a wrong password, a lock, an unreachable
endpoint) raises, so an existing repository is never initialised again; the runbook runs it once. `backup`
backs up `paths` and the files named in `files_from`, one name per line read verbatim
(`--files-from-verbatim`: never a pattern, and a listed file that is missing fails the backup with exit 3,
where `--files-from` would skip it and still exit 0), with each of `tags`, and returns the `snapshot_id` of
the `--json` summary. `restore` runs `restic restore <snapshot>:<subfolder> --target <target>` (the
`snapshotID:subfolder` form of restic >= 0.17, read from 0.19.1's `restore --help`): the subfolder's content
lands directly in `target`, not under its absolute path. `snapshots` lists the snapshots carrying every one of
`tags`, oldest first. `forget_prune` forgets the snapshots tagged `tag` beyond the daily, weekly and monthly
counts and prunes the data they alone held.

Every failure raises `ResticError(command, code)`: the code is `incomplete` (exit 3: a source file could not
be read), `repository_absent` (10), `locked` (11), `wrong_password` (12), `failed` (any other exit status),
`timeout`, `no_snapshot_id` (a backup without a summary that names one) or `unparsable` (a listing that is
not restic's JSON). Neither the code nor the message carries restic's output.
"""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from jobhunter.backup.runner import CommandResult, CommandRunner, CommandTimeout
from jobhunter.config import AppConfig, ResticSettings

RESTIC_BIN: Final = "/usr/local/bin/restic"
RESTIC_HOST: Final = "jobhunter-backup"
RETRY_LOCK: Final = "30m"
# The lock wait plus the work: a prune of the whole repository, or a backup set's upload, after 30 minutes of
# waiting for another run's lock.
COMMAND_TIMEOUT_S: Final = 4 * 3600.0
PASSWORD_SECRET: Final = "restic_password"  # noqa: S105 - the secret file's name
REPOSITORY_ABSENT_EXIT: Final = 10
_EXIT_CODES: Final = {
    3: "incomplete",
    REPOSITORY_ABSENT_EXIT: "repository_absent",
    11: "locked",
    12: "wrong_password",
}
_SNAPSHOT_ID: Final = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class SnapshotInfo:
    id: str
    time: datetime
    tags: tuple[str, ...]
    paths: tuple[str, ...]


class ResticError(Exception):
    """`restic <command>` failed; `code` says how (module docstring), `returncode` is its exit status."""

    def __init__(self, command: str, code: str, returncode: int | None = None) -> None:
        super().__init__(f"restic {command}: {code}")
        self.command = command
        self.code = code
        self.returncode = returncode


def _child_env(cfg: AppConfig, settings: ResticSettings) -> dict[str, str]:
    env = {
        "RESTIC_REPOSITORY": settings.repository,
        "RESTIC_PASSWORD_FILE": str(cfg.secrets_dir / PASSWORD_SECRET),
        "RESTIC_CACHE_DIR": str(settings.cache_dir),
    }
    if settings.region is not None:
        env["AWS_DEFAULT_REGION"] = settings.region
    key_id, secret_key = cfg.secrets.get("s3_access_key_id"), cfg.secrets.get("s3_secret_access_key")
    if key_id and secret_key:
        env["AWS_ACCESS_KEY_ID"] = key_id.decode("utf-8").strip()
        env["AWS_SECRET_ACCESS_KEY"] = secret_key.decode("utf-8").strip()
    return env


def _tag(tag: str) -> str:
    if not tag or "," in tag:
        raise ValueError("a restic tag is a non-empty name without a comma")
    return tag


def _snapshot_id(stdout: bytes) -> str:
    """The `snapshot_id` of the `--json` summary line of `restic backup`."""
    for line in stdout.splitlines():
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if isinstance(message, dict) and message.get("message_type") == "summary":
            snapshot_id = message.get("snapshot_id")
            if isinstance(snapshot_id, str) and _SNAPSHOT_ID.fullmatch(snapshot_id):
                return snapshot_id
    raise ResticError("backup", "no_snapshot_id", 0)


def _strings(value: Any) -> tuple[str, ...]:
    if value is None:  # restic leaves out `tags` (or writes null) for a snapshot without any
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("not a list of strings")
    return tuple(value)


def _snapshot_infos(stdout: bytes) -> list[SnapshotInfo]:
    try:
        listing = json.loads(stdout)
        if not isinstance(listing, list):
            raise ValueError("not a list")
        infos = []
        for item in listing:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise ValueError("not a snapshot")
            time = datetime.fromisoformat(item["time"]).astimezone(UTC)
            infos.append(
                SnapshotInfo(item["id"], time, _strings(item.get("tags")), _strings(item.get("paths")))
            )
    except (ValueError, TypeError, KeyError):
        raise ResticError("snapshots", "unparsable", 0) from None
    return infos


class Restic:
    """restic for the role of `cfg`, through `runner` (module docstring)."""

    def __init__(self, cfg: AppConfig, runner: CommandRunner) -> None:
        if cfg.restic is None:
            raise ValueError("RESTIC_REPOSITORY is not set")
        self._runner = runner
        self._env = _child_env(cfg, cfg.restic)

    def __repr__(self) -> str:
        return "Restic()"  # its environment holds the S3 keys

    async def _call(self, command: str, *args: str, host: bool = True) -> CommandResult:
        argv = [RESTIC_BIN, command, "--retry-lock", RETRY_LOCK]
        if host:
            argv += ["--host", RESTIC_HOST]
        try:
            return await self._runner.run([*argv, *args], env=self._env, timeout_s=COMMAND_TIMEOUT_S)
        except CommandTimeout:
            raise ResticError(command, "timeout") from None

    async def _run(self, command: str, *args: str, host: bool = True) -> CommandResult:
        """`_call`, raising `ResticError` for a non-zero exit status."""
        result = await self._call(command, *args, host=host)
        if result.returncode != 0:
            raise ResticError(command, _EXIT_CODES.get(result.returncode, "failed"), result.returncode)
        return result

    async def init_if_absent(self) -> bool:
        """Initialise the repository when restic reports it absent; whether it did."""
        probe = await self._call("snapshots", "--json", "--latest", "1")
        if probe.returncode == 0:
            return False
        if probe.returncode != REPOSITORY_ABSENT_EXIT:
            raise ResticError("snapshots", _EXIT_CODES.get(probe.returncode, "failed"), probe.returncode)
        await self._run("init", host=False)
        return True

    async def backup(
        self, *, paths: Sequence[Path] = (), files_from: Path | None = None, tags: Sequence[str]
    ) -> str:
        """Back up `paths` and the files `files_from` names; the new snapshot's id."""
        if not paths and files_from is None:
            raise ValueError("a backup needs paths or a file list")
        args = ["--json", "--quiet"]
        for tag in tags:
            args += ["--tag", _tag(tag)]
        if files_from is not None:
            args += ["--files-from-verbatim", str(files_from)]
        if paths:
            args += ["--", *(str(path) for path in paths)]
        return _snapshot_id((await self._run("backup", *args)).stdout)

    async def restore(self, *, snapshot: str, subfolder: str, target: Path) -> None:
        """Restore `subfolder` of `snapshot` into `target`: its content lands directly there."""
        await self._run("restore", "--target", str(target), "--", f"{snapshot}:{subfolder}")

    async def snapshots(self, *, tags: Sequence[str]) -> list[SnapshotInfo]:
        """The snapshots that carry every one of `tags` (all of them for none), oldest first."""
        args = ["--json"]
        if tags:
            args += ["--tag", ",".join(_tag(tag) for tag in tags)]
        return _snapshot_infos((await self._run("snapshots", *args)).stdout)

    async def forget_prune(self, *, tag: str, keep_daily: int, keep_weekly: int, keep_monthly: int) -> None:
        """Forget the snapshots tagged `tag` beyond the counts kept, and prune what they alone held."""
        await self._run(
            "forget",
            "--tag",
            _tag(tag),
            "--keep-daily",
            str(keep_daily),
            "--keep-weekly",
            str(keep_weekly),
            "--keep-monthly",
            str(keep_monthly),
            "--prune",
        )
