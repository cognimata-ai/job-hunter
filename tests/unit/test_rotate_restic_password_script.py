"""`scripts/rotate-restic-password.sh`, the restic repository password's rotation (spec §19.8 "Secret
rotation"; M5 follow-ups Task F4 (c), Ruling FU-10).

Its order: (1) a key added for the new password and (2) checked, in a one-off backup container that mounts the
installed (old) password and reads the new one from its stdin; (3) the new file installed; (4) backup
restarted; (5) the old key removed, in a second one-off that mounts the new password. So no restic call of the
running backup service ever meets a removed password, and a removal that fails leaves the old key, which the
script names by its id.

A `--dry-run` checks its arguments and the new file, then prints each command it would run, executing none:
shims stand first on PATH for every command that would change something, and record any call. The new file
holds a planted dummy value, which must never appear in what the script prints.

The two one-off scripts (the `sh -c` texts the dry run prints) are run here against a fake `restic` that keeps
a repository's keys in a directory (`FAKE_RESTIC`, below: restic 0.19.1's `key list --json` fields `current`
and `id`, exit 12 for a password that opens no key, and its refusal to remove the key in use), with their
`/run/secrets` and `/dev/shm` temporary directories. Each restic call names its password file by a flag, so no
password reaches a command line or an environment; the S3 keys reach restic's environment only. The whole
script is then run, not as a dry run, with a fake `docker` that runs each one-off's script here in the same
way and records the restart, a fake `install` that copies, and a fake `id`: it shows the five steps in their
order, and what a removal that fails leaves and says.
"""

import json
import os
import re
import shlex
import shutil
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
SECRET_VALUES: Final = (NEW, OLD, S3_KEY_ID, S3_SECRET)
PROD_COMPOSE: Final = shlex.split(
    "docker compose -p jobhunter --env-file deploy/images.lock --env-file deploy/release.lock "
    "--env-file /etc/jobhunter/jobhunter.env -f deploy/compose.yaml"
)
PROD_SECRETS: Final = "/etc/jobhunter/secrets"
CHANGES: Final = ("install", "docker", "restic", "sudo", "chown", "chmod", "rm", "mv", "cp", "tee", "mktemp")
NOTE: Final = "rotate-restic-password: "
ONE_OFF: Final = ["run", "--rm", "--no-deps", "-T", "backup", "sh", "-c"]
NAME: Final = "rotate-restic-password"
NO_KEY_YET: Final = "<old-key-id>"  # the dry run's stand-in for the old key's id
OLD_ID: Final = f"{1:064x}"  # the fake repository's first key

# A restic that keeps a repository's keys as files `<FAKE_REPO>/<id>` holding their passwords, and records
# each call (its arguments, and whether the S3 keys and the repository reached its environment) in FAKE_LOG.
# FAKE_ADDED stores another password for an added key; FAKE_REMOVE_FAILS makes `key remove` fail;
# FAKE_LIST_FAILS_AFTER_REMOVE makes every `key list` after a removal fail (exit 1).
FAKE_RESTIC: Final = f"""#!{sys.executable}
import json, os, sys
from pathlib import Path

args = sys.argv[1:]
repo, log = Path(os.environ["FAKE_REPO"]), Path(os.environ["FAKE_LOG"])
s3 = (os.environ.get("AWS_ACCESS_KEY_ID"), os.environ.get("AWS_SECRET_ACCESS_KEY")) == ({S3_KEY_ID!r},
    {S3_SECRET!r})

def value(flag):
    return Path(args[args.index(flag) + 1]).read_text().strip()

with log.open("a") as out:
    out.write(json.dumps({{"argv": args, "s3": s3, "repository": os.environ.get("RESTIC_REPOSITORY"),
                          "password_env": "RESTIC_PASSWORD" in os.environ,
                          "password": value("--password-file")}}) + "\\n")

def opened():
    password = value("--password-file")
    for key in sorted(repo.iterdir()):
        if key.read_text() == password:
            return key.name
    sys.exit(12)

removed = repo.parent / "fake-restic-removed"
if args[:2] == ["key", "list"] and removed.exists() and os.environ.get("FAKE_LIST_FAILS_AFTER_REMOVE"):
    sys.exit(1)
current = opened()
if args[:2] == ["key", "list"]:
    print(json.dumps([{{"current": key.name == current, "id": key.name}} for key in sorted(repo.iterdir())]))
elif args[:2] == ["key", "add"]:
    key = repo / f"{{len(list(repo.iterdir())) + 1:064x}}"
    key.write_text(os.environ.get("FAKE_ADDED", value("--new-password-file")))
    print("saved new key with ID", key.name)
elif args[:2] == ["key", "remove"]:
    if args[-1] == current or os.environ.get("FAKE_REMOVE_FAILS"):
        print("Fatal: refusing to remove key currently used to access repository", file=sys.stderr)
        sys.exit(1)
    (repo / args[-1]).unlink()
    removed.touch()
    print("removed key", args[-1])
else:
    sys.exit(1)
"""

# A docker whose `compose … run … sh -c <script> <name> <args>` runs the one-off's script here, its
# `/run/secrets` the secrets directory the script installs into and its `/dev/shm` a temporary directory, with
# the caller's stdin; `compose … restart …` exits FAKE_RESTART_EXIT (0 by default). Each call is recorded in
# FAKE_CALLS after the compose prefix.
FAKE_DOCKER: Final = f"""#!{sys.executable}
import os, subprocess, sys

args = sys.argv[1:]
calls = os.environ["FAKE_CALLS"]
if "run" in args:
    at = args.index("sh")
    script, rest = args[at + 2], args[at + 3 :]
    with open(calls, "a") as out:
        out.write(" ".join(["run", *rest]) + "\\n")
    script = script.replace("/run/secrets", os.environ["FAKE_SECRETS"])
    script = script.replace("/dev/shm", os.environ["FAKE_SHM"])
    sys.exit(subprocess.run(["/bin/sh", "-c", script, *rest]).returncode)
with open(calls, "a") as out:
    out.write(" ".join(args[args.index("restart") :]) + "\\n")
sys.exit(int(os.environ.get("FAKE_RESTART_EXIT", "0")))
"""
# An install that copies, or fails (exit 1) with nothing copied (FAKE_INSTALL_FAILS) or after copying
# (FAKE_INSTALL_FAILS_AFTER_WRITE: its target is written, as by an install cut off before its owner or mode).
FAKE_INSTALL: Final = """#!/bin/sh
for arg; do source=$target; target=$arg; done
echo "install $target" >> "$FAKE_CALLS"
[ -z "${FAKE_INSTALL_FAILS:-}" ] || exit 1
cp "$source" "$target"
[ -z "${FAKE_INSTALL_FAILS_AFTER_WRITE:-}" ] || exit 1
"""
# The real mv, recorded, or failing (exit 1, nothing renamed) with FAKE_MV_FAILS.
FAKE_MV: Final = f"""#!/bin/sh
echo "mv $*" >> "$FAKE_CALLS"
[ -z "${{FAKE_MV_FAILS:-}}" ] || exit 1
exec {shutil.which("mv")} "$@"
"""
# The temporary file of the install (F6 fix round 2): mktemp's name in the secrets directory, as the calls
# show it.
STAGED: Final = re.compile(r"/\.restic_password\.[A-Za-z0-9]{6}(?=\s|$)")
TMP: Final = ".restic_password.<tmp>"


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

    def one_offs(self) -> tuple[tuple[str, list[str]], tuple[str, list[str]]]:
        """The two one-offs' scripts and arguments: the key's addition and check, then its removal."""
        found = [
            (tail[len(ONE_OFF)], tail[len(ONE_OFF) + 1 :])
            for tail in self.compose_commands(PROD_COMPOSE)
            if tail[: len(ONE_OFF)] == ONE_OFF
        ]
        add, remove = found
        return add, remove


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
class Site:
    """A host of the fakes: its secrets directory (the backup container's `/run/secrets`), its `/dev/shm`,
    the repository's keys, the fakes' directory and logs."""

    secrets: Path
    shm: Path
    repo: Path
    bin: Path
    restic_log: Path
    calls_log: Path

    def env(self, **extra: str) -> dict[str, str]:
        return {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "FAKE_REPO": str(self.repo),
            "FAKE_LOG": str(self.restic_log),
            "FAKE_CALLS": str(self.calls_log),
            "FAKE_SECRETS": str(self.secrets),
            "FAKE_SHM": str(self.shm),
            "RESTIC_REPOSITORY": "s3:http://s3.test/bucket/restic",
            **extra,
        }

    @property
    def keys(self) -> dict[str, str]:
        return {key.name: key.read_text() for key in sorted(self.repo.iterdir())}

    @property
    def restic_calls(self) -> list[dict[str, Any]]:
        lines = self.restic_log.read_text().splitlines() if self.restic_log.exists() else []
        return [json.loads(line) for line in lines]

    @property
    def calls(self) -> list[str]:
        """The fakes' calls, the temporary file of the install named `TMP`."""
        lines = self.calls_log.read_text().splitlines() if self.calls_log.exists() else []
        return [STAGED.sub(f"/{TMP}", line) for line in lines]

    @property
    def installed(self) -> list[str]:
        """The two install calls of step 3: into the temporary file, then its rename over the secret."""
        return [
            f"install {self.secrets}/{TMP}",
            f"mv -f -T {self.secrets}/{TMP} {self.secrets}/restic_password",
        ]

    def files(self) -> list[str]:
        """The secrets directory's file names: a temporary file left behind would be listed."""
        return sorted(path.name for path in self.secrets.iterdir())


def a_site(tmp_path: Path, keys: list[str], *, installed: str = OLD) -> Site:
    """The fakes, with `installed` as the installed restic_password and a repository whose keys open with
    `keys`, in order."""
    names = ("site-secrets", "shm", "repo", "fake-bin", "restic.log", "calls.log")
    site = Site(*(tmp_path / name for name in names))
    for directory in (site.secrets, site.shm, site.repo, site.bin):
        directory.mkdir()
    (site.secrets / "restic_password").write_text(f"{installed}\n")
    (site.secrets / "s3_access_key_id").write_text(f"{S3_KEY_ID}\n")
    (site.secrets / "s3_secret_access_key").write_text(f"{S3_SECRET}\n")
    for n, password in enumerate(keys, start=1):
        (site.repo / f"{n:064x}").write_text(password)
    fakes = (("restic", FAKE_RESTIC), ("docker", FAKE_DOCKER), ("install", FAKE_INSTALL), ("mv", FAKE_MV))
    for name, body in fakes:
        (site.bin / name).write_text(body)
        (site.bin / name).chmod(0o755)
    (site.bin / "id").write_text("#!/bin/sh\necho 0\n")  # as root
    (site.bin / "id").chmod(0o755)
    (site.bin / "python").symlink_to(sys.executable)  # the backup image's python
    return site


def one_off(
    site: Site, script: str, args: list[str], *, stdin: str = "", **env: str
) -> subprocess.CompletedProcess[str]:
    """A one-off's script run here as the backup container runs it, its `/run/secrets` and `/dev/shm` the
    site's."""
    local = script.replace("/run/secrets", str(site.secrets)).replace("/dev/shm", str(site.shm))  # noqa: S108
    return subprocess.run(
        ["sh", "-c", local, *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=site.env(**env),
        timeout=60,
        check=False,
    )


def full_run(tmp_path: Path, site: Site, **env: str) -> subprocess.CompletedProcess[str]:
    """The script itself, not a dry run, on the site's fakes, with the new file holding `NEW`."""
    new_file = tmp_path / "new-password"
    new_file.write_text(f"{NEW}\n")
    argv = [
        str(SCRIPT),
        "--compose",
        "docker compose -p fake",
        "--secrets-dir",
        str(site.secrets),
        str(new_file),
    ]
    return subprocess.run(
        argv, capture_output=True, text=True, env=site.env(**env), cwd=REPO, timeout=120, check=False
    )


def file_of(call: dict[str, Any], flag: str) -> str:
    argv: list[str] = call["argv"]
    return Path(argv[argv.index(flag) + 1]).name


def steps(site: Site) -> list[tuple[str, str]]:
    """Each restic call's subcommand and the password it was made with (`OLD` or `NEW`, by name)."""
    names = {OLD: "old", NEW: "new"}
    return [(call["argv"][1], names.get(call["password"], "other")) for call in site.restic_calls]


def assert_no_secret_anywhere(site: Site, done: subprocess.CompletedProcess[str]) -> None:
    for call in site.restic_calls:
        argv = call["argv"]
        assert not any(secret in part for part in argv for secret in SECRET_VALUES), argv
        assert "--no-cache" in argv and argv[argv.index("--retry-lock") + 1] == "30m", argv
        assert call["s3"] is True and call["password_env"] is False
        assert call["repository"] == "s3:http://s3.test/bucket/restic"
    assert not any(secret in done.stdout + done.stderr for secret in SECRET_VALUES)
    assert list(site.shm.iterdir()) == []


def test_the_dry_run_prints_the_five_steps_in_their_order(tmp_path: Path) -> None:
    """Ruling FU-10's order: the one-off that adds and checks the new key, with the new file on its stdin; the
    install of the new file; the restart of backup; the one-off that removes the old key, named by the id the
    first one-off prints (`<old-key-id>` in a dry run). Never the value, and nothing runs."""
    ran = dry_run(tmp_path, "NEW")
    assert ran.done.returncode == 0, ran.done.stderr
    assert NEW not in ran.done.stdout + ran.done.stderr
    assert ran.calls == []
    assert ran.done.stdout.splitlines()[0] == f"{NOTE}dry run: the commands are printed, none is run"
    assert ran.commands[0] == ["cd", str(REPO)]
    assert ran.lines[1].endswith(f" < {ran.new_file}")  # the first one-off reads the new password from stdin
    # step 3 (F6 fix round 2): the new file installed into a temporary file of the secrets directory, with its
    # owner, group and mode, then renamed over the secret; a dry run names mktemp's template
    target, staged = f"{PROD_SECRETS}/restic_password", f"{PROD_SECRETS}/.restic_password.XXXXXX"
    assert ran.commands[2] == [
        "install",
        "-o",
        "root",
        "-g",
        "10010",
        "-m",
        "0440",
        str(ran.new_file),
        staged,
    ]
    assert ran.commands[3] == ["mv", "-f", "-T", staged, target]
    tails = ran.compose_commands(PROD_COMPOSE)
    assert [tail[: len(ONE_OFF)] if tail[0] == "run" else tail for tail in tails] == [
        ONE_OFF,
        ["restart", "backup"],
        ONE_OFF,
    ]
    assert len(ran.commands) == 6
    assert " < " not in ran.lines[5]  # the second one-off mounts the new password: no stdin
    (_, add_args), (_, remove_args) = ran.one_offs()
    assert (add_args, remove_args) == ([NAME], [NAME, NO_KEY_YET])
    assert all(NEW not in part for command in ran.commands for part in command)


def test_the_first_one_off_adds_and_checks_the_new_key_and_names_the_old_one(tmp_path: Path) -> None:
    """The key the installed password opens is read, the new password is found to open none yet, `restic key
    add` adds it (opened with the installed password), and the new password then opens the repository
    through its own key. Both keys stay; the last line on stdout names the old key, for the removal. Every
    call waits for another's repository lock as the backup jobs' do and keeps no cache; its passwords are
    files named by flags, the new one on `/dev/shm` (removed at the end); the S3 keys are in restic's
    environment."""
    (add, add_args), _ = dry_run(tmp_path, "NEW").one_offs()
    site = a_site(tmp_path, [OLD])
    done = one_off(site, add, add_args, stdin=f"{NEW}\n")
    assert done.returncode == 0, done.stderr
    assert list(site.keys.values()) == [OLD, NEW]
    assert steps(site) == [("list", "old"), ("list", "new"), ("add", "old"), ("list", "new")]
    assert file_of(site.restic_calls[2], "--new-password-file") == "new"
    assert done.stdout.splitlines() == [f"old-key {OLD_ID}"]
    assert f"the old key is {OLD_ID[:8]}" in done.stderr
    assert_no_secret_anywhere(site, done)


def test_a_new_password_that_opens_a_key_already_is_not_added_again(tmp_path: Path) -> None:
    """A run again after one that stopped after the add, before the install: no second key is added."""
    (add, add_args), _ = dry_run(tmp_path, "NEW").one_offs()
    site = a_site(tmp_path, [OLD, NEW])
    done = one_off(site, add, add_args, stdin=f"{NEW}\n")
    assert done.returncode == 0, done.stderr
    assert [step for step, _ in steps(site)] == ["list", "list"]
    assert done.stdout.splitlines() == [f"old-key {OLD_ID}"]


@pytest.mark.parametrize(
    ("keys", "installed", "env", "message"),
    [
        ([OLD], NEW, {}, "the installed password opens no key"),
        ([OLD], OLD, {"FAKE_ADDED": "not-the-new-password"}, "the new key does not open the repository"),
    ],
)
def test_the_first_one_off_stops_unless_the_new_key_opens_the_repository(
    tmp_path: Path, keys: list[str], installed: str, env: dict[str, str], message: str
) -> None:
    """The first one-off names no old key, and so nothing is installed, restarted or removed, when the
    installed password opens nothing, or when the added key does not open with the new password."""
    (add, add_args), _ = dry_run(tmp_path, "NEW").one_offs()
    site = a_site(tmp_path, keys, installed=installed)
    done = one_off(site, add, add_args, stdin=f"{NEW}\n", **env)
    assert done.returncode != 0
    assert message in done.stderr
    assert "old-key" not in done.stdout
    assert "remove" not in [step for step, _ in steps(site)]
    assert list(site.shm.iterdir()) == []


def test_the_first_one_off_names_the_other_keys_when_the_new_password_is_installed_already(
    tmp_path: Path,
) -> None:
    """Fix round 1, review I1: the new password is the installed one already (an earlier run installed it and
    stopped). The one-off changes nothing and, in place of `old-key`, names every other key in full
    (`installed-already <ids>`), for the script's message."""
    (add, add_args), _ = dry_run(tmp_path, "NEW").one_offs()
    site = a_site(tmp_path, [OLD, NEW], installed=NEW)
    done = one_off(site, add, add_args, stdin=f"{NEW}\n")
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [f"installed-already {OLD_ID}"]
    assert [step for step, _ in steps(site)] == ["list", "list"]
    assert list(site.keys.values()) == [OLD, NEW]


def test_the_second_one_off_removes_the_old_key_with_the_new_password(tmp_path: Path) -> None:
    """After the install and the restart, the second one-off mounts the new password: it removes the old key
    as the new one and checks that it is gone."""
    _, (remove, remove_args) = dry_run(tmp_path, "NEW").one_offs()
    site = a_site(tmp_path, [OLD, NEW], installed=NEW)
    done = one_off(site, remove, [NAME, OLD_ID])
    assert remove_args == [NAME, NO_KEY_YET]
    assert done.returncode == 0, done.stderr
    assert list(site.keys.values()) == [NEW]
    assert steps(site) == [("list", "new"), ("remove", "new"), ("list", "new")]
    assert site.restic_calls[1]["argv"][-1] == OLD_ID
    assert f"removed the old key {OLD_ID[:8]}" in done.stderr
    assert_no_secret_anywhere(site, done)


@pytest.mark.parametrize(
    ("keys", "env", "message"),
    [
        ([OLD, NEW], {"FAKE_REMOVE_FAILS": "1"}, "restic key remove exited 1"),
        ([NEW], {}, "the installed password opens the old key 00000000 itself"),
    ],
)
def test_the_second_one_off_fails_when_the_old_key_stays(
    tmp_path: Path, keys: list[str], env: dict[str, str], message: str
) -> None:
    """A removal restic refuses, or an id that is the installed password's own key, fails the one-off and
    leaves every key in place."""
    _, (remove, _) = dry_run(tmp_path, "NEW").one_offs()
    site = a_site(tmp_path, keys, installed=NEW)
    done = one_off(site, remove, [NAME, OLD_ID], **env)
    assert done.returncode != 0
    assert message in done.stderr
    assert list(site.keys.values()) == keys


def test_a_rotation_adds_installs_restarts_then_removes(tmp_path: Path) -> None:
    """The whole script, on the fakes: the first one-off (the old password installed), the install of the new
    file, the restart of backup, then the second one-off with the old key's id (the new password installed),
    in that order; afterwards the repository opens with the new password only, and the script says so."""
    site = a_site(tmp_path, [OLD])
    done = full_run(tmp_path, site)
    assert done.returncode == 0, done.stderr
    assert site.calls == [f"run {NAME}", *site.installed, "restart backup", f"run {NAME} {OLD_ID}"]
    assert site.files() == ["restic_password", "s3_access_key_id", "s3_secret_access_key"]
    assert steps(site) == [
        ("list", "old"),
        ("list", "new"),
        ("add", "old"),
        ("list", "new"),
        ("list", "new"),  # the second one-off: the installed password is the new one now
        ("remove", "new"),
        ("list", "new"),
    ]
    assert list(site.keys.values()) == [NEW]
    assert (site.secrets / "restic_password").read_text() == f"{NEW}\n"
    assert done.stdout.splitlines()[-1] == (
        f"{NOTE}done: installed restic_password, restarted backup and removed the old key {OLD_ID[:8]}; the "
        "repository opens with the new password only"
    )
    assert_no_secret_anywhere(site, done)


@pytest.mark.parametrize(
    ("env", "keys_left", "stopped"),
    [
        ({"FAKE_REMOVE_FAILS": "1"}, [OLD, NEW], "restic key remove exited 1"),
        ({"FAKE_LIST_FAILS_AFTER_REMOVE": "1"}, [NEW], "restic key list exited 1"),  # review M3
    ],
)
def test_a_removal_that_fails_names_the_old_key_and_the_listing(
    tmp_path: Path, env: dict[str, str], keys_left: list[str], stopped: str
) -> None:
    """Step 5 fails after the install and the restart, either in `restic key remove` (the old key stays) or in
    the check after it (the key is gone): the second one-off's line says which, and the script exits non-zero
    saying the old key, named by its full id (never a password), may still be in the repository, so list the
    keys and remove it by hand if it is listed (review M3); the new password is installed, backup
    restarted."""
    site = a_site(tmp_path, [OLD])
    done = full_run(tmp_path, site, **env)
    assert done.returncode == 1
    assert site.calls[-2:] == ["restart backup", f"run {NAME} {OLD_ID}"]
    assert list(site.keys.values()) == keys_left
    assert (site.secrets / "restic_password").read_text() == f"{NEW}\n"
    *_, why, last = done.stderr.splitlines()
    assert stopped in why
    assert f"the old key {OLD_ID} may still be in the repository" in last
    assert "list the keys, and if it is listed, remove it by hand (docs/RUNBOOK.md, section 7)" in last
    assert "done:" not in done.stdout
    assert not any(secret in done.stdout + done.stderr for secret in SECRET_VALUES)


def test_a_run_again_after_the_install_says_restart_backup_first(tmp_path: Path) -> None:
    """Review I1: an earlier run installed the new file and stopped, maybe before its restart, so the backup
    container may still mount the old password. A run again finds the new password installed: it changes
    nothing, restarts nothing, removes nothing, and exits 1 saying to restart backup first, then to remove
    the old key by hand (RUNBOOK section 7), naming the other keys in full; never "run it again", and a run
    again says the same."""
    site = a_site(tmp_path, [OLD, NEW], installed=NEW)
    for _ in range(2):
        done = full_run(tmp_path, site)
        assert done.returncode == 1
        last = done.stderr.splitlines()[-1]
        assert (
            "restic_password holds the new password already (an earlier run installed it and stopped)" in last
        )
        assert "restart backup first (docker compose -p fake restart backup)" in last
        assert "then remove the old key by hand with the command of docs/RUNBOOK.md, section 7" in last
        assert last.endswith(f"the repository's other keys: {OLD_ID}")
        assert "run it again" not in done.stderr
    assert site.calls == [f"run {NAME}", f"run {NAME}"]
    assert list(site.keys.values()) == [OLD, NEW]
    assert (site.secrets / "restic_password").read_text() == f"{NEW}\n"
    assert not any(secret in done.stdout + done.stderr for secret in SECRET_VALUES)


def test_a_run_again_after_a_finished_rotation_has_nothing_to_rotate(tmp_path: Path) -> None:
    """The new password is installed and opens the repository's only key: nothing is left to rotate, and
    nothing is changed."""
    site = a_site(tmp_path, [NEW], installed=NEW)
    done = full_run(tmp_path, site)
    assert done.returncode == 1
    assert "opens the repository's only key: nothing is left to rotate" in done.stderr.splitlines()[-1]
    assert site.calls == [f"run {NAME}"] and list(site.keys.values()) == [NEW]


def installed_unchanged(site: Site, new_file: Path) -> str:
    """The last line of a step 3 that failed: the live secret unchanged, the new key added and verified, the
    retained new file named (F6 fix round 2, Ruling F6-3)."""
    return (
        f"{NOTE}{site.secrets}/restic_password is unchanged (it still holds the old password); the new "
        f"password's key is added and verified, so the repository opens with both, and {new_file} holds the "
        "new one: keep it, and run it again"
    )


def test_an_install_that_fails_leaves_both_keys_and_a_run_again_finishes(tmp_path: Path) -> None:
    """Review M2, step 3: the install fails after the key was added. The script exits 1: the repository opens
    with both passwords and the file holds the old one, so it says to run it again, and nothing was
    restarted or removed. A run again adds no second key and finishes the rotation."""
    site = a_site(tmp_path, [OLD])
    before = site.files()
    done = full_run(tmp_path, site, FAKE_INSTALL_FAILS="1")
    assert done.returncode == 1
    assert site.calls == [f"run {NAME}", site.installed[0]]
    assert list(site.keys.values()) == [OLD, NEW]
    assert (site.secrets / "restic_password").read_text() == f"{OLD}\n"
    assert site.files() == before  # the temporary file is removed
    assert done.stderr.splitlines()[-1] == installed_unchanged(site, tmp_path / "new-password")
    again = full_run(tmp_path, site)
    assert again.returncode == 0, again.stderr
    assert [step for step, _ in steps(site)].count("add") == 1
    assert list(site.keys.values()) == [NEW]
    assert (site.secrets / "restic_password").read_text() == f"{NEW}\n"
    assert not any(secret in done.stdout + done.stderr for secret in SECRET_VALUES)


@pytest.mark.parametrize(
    ("env", "calls"),
    [
        ({"FAKE_INSTALL_FAILS_AFTER_WRITE": "1"}, 1),  # the temporary file written, then install fails
        ({"FAKE_MV_FAILS": "1"}, 2),  # the temporary file installed, then its rename fails
    ],
)
def test_an_install_cut_off_after_its_temporary_file_leaves_the_secret_as_it_was(
    tmp_path: Path, env: dict[str, str], calls: int
) -> None:
    """F6 fix round 2 (Codex I1, Fable M2; Ruling F6-3): step 3 installs into a temporary file of the secrets
    directory and renames it over the secret, so a failure after the temporary file is written leaves the
    secret byte-identical and no temporary file behind. The script exits 1 saying the secret is unchanged,
    that the new key is added and verified, and naming the retained new file; nothing is restarted or
    removed, and a run again finishes the rotation."""
    site = a_site(tmp_path, [OLD])
    live = site.secrets / "restic_password"
    before, files = live.read_bytes(), site.files()
    done = full_run(tmp_path, site, **env)
    assert done.returncode == 1
    assert site.calls == [f"run {NAME}", *site.installed[:calls]]
    assert live.read_bytes() == before
    assert site.files() == files
    assert list(site.keys.values()) == [OLD, NEW]
    assert done.stderr.splitlines()[-1] == installed_unchanged(site, tmp_path / "new-password")
    assert "done:" not in done.stdout
    again = full_run(tmp_path, site)
    assert again.returncode == 0, again.stderr
    assert list(site.keys.values()) == [NEW] and live.read_text() == f"{NEW}\n"
    assert not any(secret in done.stdout + done.stderr for secret in SECRET_VALUES)


def test_a_restart_that_fails_says_restart_then_remove_by_hand(tmp_path: Path) -> None:
    """Review M2, step 4: the restart fails after the install. The script exits 1 saying the file holds the
    new password but backup was not restarted, to restart it, then to remove the old key, named by its full
    id, by hand; no key was removed."""
    site = a_site(tmp_path, [OLD])
    done = full_run(tmp_path, site, FAKE_RESTART_EXIT="1")
    assert done.returncode == 1
    assert site.calls == [f"run {NAME}", *site.installed, "restart backup"]
    assert list(site.keys.values()) == [OLD, NEW]
    assert (site.secrets / "restic_password").read_text() == f"{NEW}\n"
    assert done.stderr.splitlines()[-1].endswith(
        f"{site.secrets}/restic_password holds the new password, but backup was not restarted: restart it, "
        f"then remove the old key {OLD_ID} by hand (docs/RUNBOOK.md, section 7)"
    )
    assert not any(secret in done.stdout + done.stderr for secret in SECRET_VALUES)


def test_a_key_that_does_not_open_installs_and_restarts_nothing(tmp_path: Path) -> None:
    """Step 2 fails: nothing past the first one-off runs, and the installed password is the old one."""
    site = a_site(tmp_path, [OLD])
    done = full_run(tmp_path, site, FAKE_ADDED="not-the-new-password")
    assert done.returncode == 1
    assert site.calls == [f"run {NAME}"]
    assert (site.secrets / "restic_password").read_text() == f"{OLD}\n"
    assert "is unchanged and nothing was restarted" in done.stderr


def test_the_defaults_are_the_prod_command_line_and_secrets_directory(tmp_path: Path) -> None:
    ran = dry_run(tmp_path, "NEW", cwd=tmp_path)
    assert ran.done.returncode == 0, ran.done.stderr
    assert ran.commands[0] == ["cd", str(REPO)]  # the prod command line's paths are the checkout's
    assert ran.commands[3][-1] == f"{PROD_SECRETS}/restic_password"
    composed = [command for command in ran.commands if command[:2] == ["docker", "compose"]]
    assert len(composed) == 3 and all(command[: len(PROD_COMPOSE)] == PROD_COMPOSE for command in composed)


def test_compose_and_secrets_dir_replace_both(tmp_path: Path) -> None:
    """The stack case's call: PT's command line and `.secrets/test` (relative to where it is called)."""
    ran = dry_run(tmp_path, "--compose", shlex.join(PT), "--secrets-dir", ".secrets/test", "NEW")
    assert ran.done.returncode == 0, ran.done.stderr
    assert "/etc/jobhunter" not in ran.done.stdout
    assert ran.commands[3][-1] == f"{REPO}/.secrets/test/restic_password"
    composed = [command for command in ran.commands if command[:2] == ["docker", "compose"]]
    assert len(composed) == 3 and all(command[: len(PT)] == PT for command in composed)
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
