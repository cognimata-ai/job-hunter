"""`scripts/rotate-restic-password.sh`, the restic repository password's rotation (spec §19.8 "Secret
rotation"; M5 follow-ups Task F4 (c)).

Every case of the script itself is a `--dry-run`: a real run installs into the host's secrets directory as
root and changes the keys of a repository, which no unit test touches (the rotation stack case on the compose
test stack runs it, `tests/stack/test_prod_drills.py`). A dry run checks its arguments and the new file, then
prints each command it would run, executing none: shims stand first on PATH for every command that would
change something, and record any call. The new file holds a planted dummy value, which must never appear in
what the script prints.

The one-off backup container's script (the `sh -c` text the dry run prints) is then run here against a fake
`restic` that keeps a repository's keys in a directory (`FAKE_RESTIC`, below: restic 0.19.1's `key list
--json` fields `current` and `id`, exit 12 for a password that opens no key, and its refusal to remove the
key in use), with its `/run/secrets` and `/dev/shm` temporary directories and the new password on its stdin,
as the script redirects the new file there. Each restic call names its password file by a flag, so neither
password reaches a command line or an environment; the S3 keys reach restic's environment only.
"""

import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import pytest

from tests.support.prodstack import PT

REPO: Final = Path(__file__).resolve().parents[2]
SCRIPT: Final = REPO / "scripts/rotate-restic-password.sh"
NEW: Final = "planted-new-restic-password-of-the-dry-run"
OLD: Final = "planted-old-restic-password-of-the-dry-run"
S3_KEY_ID: Final = "planted-s3-key-id"
S3_SECRET: Final = "planted-s3-secret-access-key"
PROD_COMPOSE: Final = shlex.split(
    "docker compose -p jobhunter --env-file deploy/images.lock --env-file deploy/release.lock "
    "--env-file /etc/jobhunter/jobhunter.env -f deploy/compose.yaml"
)
PROD_SECRETS: Final = "/etc/jobhunter/secrets"
CHANGES: Final = ("install", "docker", "restic", "sudo", "chown", "chmod", "rm", "mv", "cp", "tee")
NOTE: Final = "rotate-restic-password: "
ONE_OFF: Final = ["run", "--rm", "--no-deps", "-T", "backup", "sh", "-c"]

# A restic that keeps a repository's keys as files `<FAKE_REPO>/<id>` holding their passwords, and records
# each call (its arguments, and whether the S3 keys and the repository reached its environment) in FAKE_LOG.
FAKE_RESTIC: Final = f"""#!{sys.executable}
import json, os, sys
from pathlib import Path

args = sys.argv[1:]
repo, log = Path(os.environ["FAKE_REPO"]), Path(os.environ["FAKE_LOG"])
s3 = (os.environ.get("AWS_ACCESS_KEY_ID"), os.environ.get("AWS_SECRET_ACCESS_KEY")) == ({S3_KEY_ID!r},
    {S3_SECRET!r})
with log.open("a") as out:
    out.write(json.dumps({{"argv": args, "s3": s3, "repository": os.environ.get("RESTIC_REPOSITORY"),
                          "password_env": "RESTIC_PASSWORD" in os.environ}}) + "\\n")

def value(flag):
    return Path(args[args.index(flag) + 1]).read_text().strip()

def opened():
    password = value("--password-file")
    for key in sorted(repo.iterdir()):
        if key.read_text() == password:
            return key.name
    sys.exit(12)

current = opened()
if args[:2] == ["key", "list"]:
    print(json.dumps([{{"current": key.name == current, "id": key.name}} for key in sorted(repo.iterdir())]))
elif args[:2] == ["key", "add"]:
    key = repo / f"{{len(list(repo.iterdir())) + 1:064x}}"
    key.write_text(os.environ.get("FAKE_ADDED", value("--new-password-file")))
    print("saved new key with ID", key.name)
elif args[:2] == ["key", "remove"]:
    if args[-1] == current:
        print("Fatal: refusing to remove key currently used to access repository", file=sys.stderr)
        sys.exit(1)
    (repo / args[-1]).unlink()
    print("removed key", args[-1])
else:
    sys.exit(1)
"""


@dataclass(frozen=True)
class DryRun:
    done: subprocess.CompletedProcess[str]
    new_file: Path
    calls: list[str]  # the shims' calls: none in a dry run

    @property
    def lines(self) -> list[str]:
        return [line for line in self.done.stdout.splitlines() if not line.startswith(NOTE)]

    @property
    def commands(self) -> list[list[str]]:
        """The commands printed, each split by bash itself (`printf %q` writes a multi-line argument as
        `$'…'`, which `shlex` does not read); a `< <file>` redirection is not a word of its command."""
        return [split(line) for line in self.lines]

    def compose_commands(self, compose: list[str]) -> list[list[str]]:
        return [command[len(compose) :] for command in self.commands if command[: len(compose)] == compose]


def split(line: str) -> list[str]:
    """The words of a printed command line, as bash reads them."""
    script = 'eval "set -- $1"; printf "%s\\0" "$@"'
    done = subprocess.run(["bash", "-c", script, "split", line], capture_output=True, text=True, check=True)
    return done.stdout.split("\0")[:-1]


def shims(tmp_path: Path) -> tuple[Path, Path]:
    """A directory of recording shims for `CHANGES`, and their log."""
    directory, log = tmp_path / "shims", tmp_path / "calls.log"
    directory.mkdir()
    for name in CHANGES:
        shim = directory / name
        shim.write_text(f'#!/bin/sh\necho "{name} $*" >> {shlex.quote(str(log))}\nexit 99\n')
        shim.chmod(0o755)
    return directory, log


def dry_run(tmp_path: Path, *args: str, value: str = NEW, cwd: Path = REPO) -> DryRun:
    """`rotate-restic-password.sh --dry-run <args>` with a new file holding `value`; an argument `NEW` is the
    new file's path."""
    new_file = tmp_path / "new-password"
    new_file.write_text(f"{value}\n")
    directory, log = shims(tmp_path)
    argv = [str(SCRIPT), "--dry-run", *(str(new_file) if arg == "NEW" else arg for arg in args)]
    env = {**os.environ, "PATH": f"{directory}:{os.environ['PATH']}"}
    done = subprocess.run(argv, capture_output=True, text=True, env=env, cwd=cwd, timeout=60, check=False)
    calls = log.read_text().splitlines() if log.exists() else []
    return DryRun(done, new_file, calls)


@dataclass(frozen=True)
class OneOff:
    done: subprocess.CompletedProcess[str]
    calls: list[dict[str, Any]]  # the fake restic's calls
    keys: dict[str, str]  # the repository's keys afterwards: id to password
    shm: Path


def one_off(tmp_path: Path, keys: list[str], *, new: str = NEW, **env: str) -> OneOff:
    """The dry run's one-off script, run here as the backup container runs it: its `/run/secrets` holding the
    installed password (`OLD`) and the S3 keys, its `/dev/shm` a temporary directory, the new password on its
    stdin, and a repository whose keys open with `keys`, in order."""
    ran = dry_run(tmp_path, "NEW", value=new)
    assert ran.done.returncode == 0, ran.done.stderr
    [tail] = ran.compose_commands(PROD_COMPOSE)[:1]
    assert tail[: len(ONE_OFF)] == ONE_OFF, tail
    script, args = tail[len(ONE_OFF)], tail[len(ONE_OFF) + 1 :]
    assert args == ["rotate-restic-password"]
    secrets, shm, repo, fake = (tmp_path / name for name in ("run-secrets", "shm", "repo", "fake-bin"))
    for directory in (secrets, shm, repo, fake):
        directory.mkdir()
    (secrets / "restic_password").write_text(f"{OLD}\n")
    (secrets / "s3_access_key_id").write_text(f"{S3_KEY_ID}\n")
    (secrets / "s3_secret_access_key").write_text(f"{S3_SECRET}\n")
    for n, password in enumerate(keys, start=1):
        (repo / f"{n:064x}").write_text(password)
    (fake / "restic").write_text(FAKE_RESTIC)
    (fake / "restic").chmod(0o755)
    (fake / "python").symlink_to(sys.executable)  # the backup image's python
    local = script.replace("/run/secrets", str(secrets)).replace("/dev/shm", str(shm))  # noqa: S108
    log = tmp_path / "restic.log"
    full_env = {
        "PATH": f"{fake}:/usr/bin:/bin",
        "FAKE_REPO": str(repo),
        "FAKE_LOG": str(log),
        "RESTIC_REPOSITORY": "s3:http://s3.test/bucket/restic",
        **env,
    }
    done = subprocess.run(
        ["sh", "-c", local, *args],
        input=f"{new}\n",
        capture_output=True,
        text=True,
        env=full_env,
        timeout=60,
        check=False,
    )
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return OneOff(done, calls, {key.name: key.read_text() for key in repo.iterdir()}, shm)


def file_of(call: dict[str, Any], flag: str) -> str:
    argv: list[str] = call["argv"]
    return Path(argv[argv.index(flag) + 1]).name


def test_the_password_never_reaches_a_command_line(tmp_path: Path) -> None:
    """The dry run prints the one-off backup container that changes the repository's keys, with the new file
    on its stdin, then the install of the new file and the restart of backup, never the value, and runs
    nothing."""
    ran = dry_run(tmp_path, "NEW")
    assert ran.done.returncode == 0, ran.done.stderr
    assert NEW not in ran.done.stdout + ran.done.stderr
    assert ran.calls == []
    assert ran.done.stdout.splitlines()[0] == f"{NOTE}dry run: the commands are printed, none is run"
    assert ran.commands[0] == ["cd", str(REPO)]
    assert ran.lines[1].endswith(f" < {ran.new_file}")  # the one-off reads the new password from its stdin
    tails = ran.compose_commands(PROD_COMPOSE)
    assert [tail[: len(ONE_OFF)] for tail in tails[:1]] == [ONE_OFF]
    target = f"{PROD_SECRETS}/restic_password"
    assert ran.commands[2] == [
        "install",
        "-o",
        "root",
        "-g",
        "10010",
        "-m",
        "0440",
        str(ran.new_file),
        target,
    ]
    assert tails[1:] == [["restart", "backup"]]
    assert len(ran.commands) == 4
    assert all(NEW not in part for command in ran.commands for part in command)


def test_the_one_off_adds_the_new_key_checks_it_and_removes_the_old_one(tmp_path: Path) -> None:
    """In the one-off: the key the installed password opens is read, the new password is found to open none
    yet, `restic key add` adds it, the new password then opens the repository through its own key, `restic
    key remove` removes the old key (as the new key, since restic refuses to remove the key in use), and the
    installed password opens nothing any more. Every call waits for another's repository lock as the backup
    jobs' do and keeps no cache; its passwords are files named by flags, the new one on `/dev/shm` (removed at
    the end), so no password is on a command line or in an environment; the S3 keys and the repository are in
    restic's environment."""
    done = one_off(tmp_path, [OLD])
    assert done.done.returncode == 0, done.done.stderr
    assert list(done.keys.values()) == [NEW]
    [new_id] = done.keys
    old_id = f"{1:064x}"
    steps = [
        (call["argv"][:2], call["argv"][-1] if call["argv"][1] == "remove" else None) for call in done.calls
    ]
    assert steps == [
        (["key", "list"], None),
        (["key", "list"], None),
        (["key", "add"], None),
        (["key", "list"], None),
        (["key", "remove"], old_id),
        (["key", "list"], None),
    ]
    files = [file_of(call, "--password-file") for call in done.calls]
    assert files == ["restic_password", "new", "restic_password", "new", "new", "restic_password"]
    assert file_of(done.calls[2], "--new-password-file") == "new"
    for call in done.calls:
        argv = call["argv"]
        assert "--no-cache" in argv and argv[argv.index("--retry-lock") + 1] == "30m", argv
        assert not any(secret in part for part in argv for secret in (NEW, OLD, S3_KEY_ID, S3_SECRET)), argv
        assert call["s3"] is True and call["password_env"] is False
        assert call["repository"] == "s3:http://s3.test/bucket/restic"
    assert list(done.shm.iterdir()) == []
    output = done.done.stdout + done.done.stderr
    assert NEW not in output and OLD not in output
    assert f"removed the old key {old_id[:8]}" in output and new_id[:8] in output


def test_a_new_password_that_opens_a_key_already_is_not_added_again(tmp_path: Path) -> None:
    """A run again after one that stopped between the add and the removal: the new password opens a key of
    its own already, so no key is added; the old one is removed."""
    done = one_off(tmp_path, [OLD, NEW])
    assert done.done.returncode == 0, done.done.stderr
    assert list(done.keys.values()) == [NEW]
    assert [call["argv"][1] for call in done.calls if call["argv"][1] != "list"] == ["remove"]


def test_an_installed_password_that_opens_nothing_goes_on_to_the_install(tmp_path: Path) -> None:
    """A run again after one that stopped after the removal, before the install: the installed password
    opens no key any more, the new one does, so the one-off changes nothing and ends well, and the script goes
    on to install the new file and restart backup."""
    done = one_off(tmp_path, [NEW])
    assert done.done.returncode == 0, done.done.stderr
    assert list(done.keys.values()) == [NEW]
    assert [call["argv"][1] for call in done.calls] == ["list", "list"]
    assert "opens no key" in done.done.stdout


@pytest.mark.parametrize(
    ("keys", "new", "env", "message"),
    [
        ([OLD], OLD, {}, "the new password is the installed one"),
        (["another"], NEW, {}, "neither the installed password nor the new one opens the repository"),
        ([OLD], NEW, {"FAKE_ADDED": "not-the-new-password"}, "the new key does not open the repository"),
    ],
)
def test_the_old_key_stays_unless_the_new_one_opens_the_repository(
    tmp_path: Path, keys: list[str], new: str, env: dict[str, str], message: str
) -> None:
    """No key is removed unless the new password opens the repository through a key other than the old one:
    the same password again, a repository neither opens, or a key added that does not open with the new
    password each stop the one-off, which exits non-zero (so nothing is installed or restarted)."""
    done = one_off(tmp_path, keys, new=new, **env)
    assert done.done.returncode != 0
    assert message in done.done.stderr
    assert not [call for call in done.calls if call["argv"][1] == "remove"]
    assert len(done.keys) >= len(keys)
    assert list(done.shm.iterdir()) == []


def test_the_defaults_are_the_prod_command_line_and_secrets_directory(tmp_path: Path) -> None:
    ran = dry_run(tmp_path, "NEW", cwd=tmp_path)
    assert ran.done.returncode == 0, ran.done.stderr
    assert ran.commands[0] == ["cd", str(REPO)]  # the prod command line's paths are the checkout's
    assert ran.commands[2][-1] == f"{PROD_SECRETS}/restic_password"
    composed = [command for command in ran.commands if command[:2] == ["docker", "compose"]]
    assert len(composed) == 2 and all(command[: len(PROD_COMPOSE)] == PROD_COMPOSE for command in composed)


def test_compose_and_secrets_dir_replace_both(tmp_path: Path) -> None:
    """The stack case's call: PT's command line and `.secrets/test` (relative to where it is called)."""
    ran = dry_run(tmp_path, "--compose", shlex.join(PT), "--secrets-dir", ".secrets/test", "NEW")
    assert ran.done.returncode == 0, ran.done.stderr
    assert "/etc/jobhunter" not in ran.done.stdout
    assert ran.commands[2][-1] == f"{REPO}/.secrets/test/restic_password"
    composed = [command for command in ran.commands if command[:2] == ["docker", "compose"]]
    assert len(composed) == 2 and all(command[: len(PT)] == PT for command in composed)
    assert ran.calls == []


@pytest.mark.parametrize(
    ("args", "value", "message"),
    [
        (["NEW"], " \n", "holds no password"),
        ([], NEW, "usage:"),
        (["NEW", "extra"], NEW, "usage:"),
        (["--secrets-dir"], NEW, "usage:"),
    ],
)
def test_an_empty_file_or_a_wrong_argument_is_refused(
    tmp_path: Path, args: list[str], value: str, message: str
) -> None:
    ran = dry_run(tmp_path, *args, value=value)
    assert ran.done.returncode != 0
    assert message in ran.done.stderr
    assert ran.commands == [] and ran.calls == []
