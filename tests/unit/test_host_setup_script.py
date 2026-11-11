"""`scripts/host-setup.sh`, the production host's one-time setup (spec §19.8 "Host setup"; plan M5 Task 22).

Every case is a `--dry-run` with a temporary `--secrets-from` (Ruling T22-1 (b)): a real run's targets are
the host's /etc/jobhunter, its systemd and the `jobhunter` prod project, which no test touches. A dry run
reads the names in the source directory (never a file's content) and prints each command it would run,
executing none: shims stand first on PATH for every command a real run executes, and record any call. Each
source file holds a planted dummy value, which must never appear in the output.
"""

import os
import subprocess
from pathlib import Path
from typing import Final

import pytest
from jobhunter.secret_names import SECRET_NAMES

REPO: Final = Path(__file__).resolve().parents[2]
SCRIPT: Final = REPO / "scripts/host-setup.sh"
# deploy/secrets.example's names, in its order: the secrets each installed by its own command.
NAMES: Final = tuple(
    line.split()[0]
    for line in (REPO / "deploy/secrets.example").read_text(encoding="utf-8").splitlines()
    if line.strip() and not line.startswith("#")
)
# The two that may be empty files (deploy/secrets.example): no rotation in progress, no ntfy token.
OPTIONAL: Final = ("data_key_previous", "ntfy_token")
ROLES: Final = ("api", "worker", "migrate", "backup", "restore")
PLANTED: Final = "planted-by-the-dry-run-test"
# What a real run executes; none may run in a dry run.
EXECUTED: Final = (
    "groupadd",
    "getent",
    "id",
    "install",
    "chmod",
    "chown",
    "swapon",
    "lsblk",
    "findmnt",
    "systemctl",
    "docker",
    "rm",
    "nft",
    "sudo",
)
SECRETS_DIR: Final = "/etc/jobhunter/secrets"
COMPOSE: Final = (
    "docker compose -p jobhunter --env-file deploy/images.lock --env-file deploy/release.lock "
    "--env-file /etc/jobhunter/jobhunter.env -f deploy/compose.yaml"
)
NOTE: Final = "host-setup: "


def dry_run(
    tmp_path: Path, present: tuple[str, ...], *flags: str
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """The script's dry run on a new source directory holding `present`, each file a planted value; it fails
    the test when the run executed any command of `EXECUTED`."""
    source = tmp_path / "secrets-src"
    source.mkdir()
    for name in present:
        (source / name).write_text(f"{PLANTED}-{name}\n", encoding="utf-8")
    shims = tmp_path / "bin"
    shims.mkdir()
    record = tmp_path / "executed"
    for command in EXECUTED:
        (shims / command).write_text(f'#!/bin/sh\nprintf "%s\\n" "{command} $*" >> "{record}"\nexit 97\n')
        (shims / command).chmod(0o755)
    env = {"PATH": f"{shims}{os.pathsep}{os.environ['PATH']}", "HOME": str(tmp_path), "LC_ALL": "C.UTF-8"}
    done = subprocess.run(
        [str(SCRIPT), "--secrets-from", str(source), *flags, "--dry-run"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert not record.exists(), record.read_text()
    return done, source


def commands(stdout: str) -> list[str]:
    """The printed commands, without the notes (`host-setup: …`) between them."""
    return [line for line in stdout.splitlines() if not line.startswith(NOTE)]


def expected(source: Path, *, empty: tuple[str, ...] = (), deleted: bool = False) -> list[str]:
    """§19.8's host steps in the order of plan M5 Task 22: the group, the directory, each secret (an empty
    file for each of `empty`), the swap check, the egress rule's unit, each role's startup check; then, with
    `--delete-sources`, the sources that were installed."""
    sources = {name: "/dev/null" if name in empty else str(source / name) for name in NAMES}
    installs = [f"install -o root -g 10010 -m 0440 {sources[name]} {SECRETS_DIR}/{name}" for name in NAMES]
    lines = [
        "groupadd --gid 10010 jobhunter-secrets",
        f"install -d -o root -g 10010 -m 0750 {SECRETS_DIR}",
        *installs,
        "swapon --show=NAME --noheadings --raw",
        "install -o root -g root -m 0644 /opt/jobhunter/deploy/firewall/jobhunter-egress.service "
        "/etc/systemd/system/jobhunter-egress.service",
        "systemctl daemon-reload",
        "systemctl enable --now jobhunter-egress.service",
        "cd /opt/jobhunter",
        *(f"{COMPOSE} run --rm --no-deps {role} jobhunter startup-check --role {role}" for role in ROLES),
    ]
    if deleted:
        lines += [f"rm -f -- {source / name}" for name in NAMES if name not in empty]
    return lines


def test_the_names_are_the_secrets_of_the_spec() -> None:
    assert len(NAMES) == len(SECRET_NAMES) and set(NAMES) == SECRET_NAMES
    assert set(OPTIONAL) <= SECRET_NAMES


def test_the_steps_run_in_order(tmp_path: Path) -> None:
    done, source = dry_run(tmp_path, NAMES, "--delete-sources")
    assert (done.returncode, done.stderr) == (0, ""), done.stderr
    assert commands(done.stdout) == expected(source, deleted=True)
    # A dry run deletes nothing: every source is still there, as it was.
    assert {path.name: path.read_text(encoding="utf-8") for path in source.iterdir()} == {
        name: f"{PLANTED}-{name}\n" for name in NAMES
    }


def test_a_missing_optional_secret_becomes_an_empty_file(tmp_path: Path) -> None:
    present = tuple(name for name in NAMES if name not in OPTIONAL)
    done, source = dry_run(tmp_path, present, "--delete-sources")
    assert (done.returncode, done.stderr) == (0, ""), done.stderr
    assert commands(done.stdout) == expected(source, empty=OPTIONAL, deleted=True)
    for name in OPTIONAL:  # never over a file that exists, such as the previous key during a rotation
        assert f"{NOTE}{name} is not in {source}: an empty file, unless {SECRETS_DIR}/{name} exists" in (
            done.stdout.splitlines()
        )


def test_it_never_prints_a_secret(tmp_path: Path) -> None:
    done, _ = dry_run(tmp_path, NAMES, "--delete-sources")
    assert done.returncode == 0, done.stderr
    assert PLANTED not in done.stdout and PLANTED not in done.stderr
    # The names alone: no line shows a file's content.
    for line in commands(done.stdout):
        assert "-dry-run-test" not in line, line


def test_it_never_uses_chmod_0400(tmp_path: Path) -> None:
    """§19.8 step 3: the app services run as uid 10001 and read their secrets through group 10010, so a file
    is 0440 and never 0400; the script sets each mode by `install -m`, never by chmod."""
    code = [
        line.split("#", 1)[0] for line in SCRIPT.read_text(encoding="utf-8").splitlines()[1:] if line.strip()
    ]
    assert not [line for line in code if "chmod" in line or "0400" in line or " 400" in line]
    done, _ = dry_run(tmp_path, NAMES)
    assert done.returncode == 0, done.stderr
    printed = commands(done.stdout)
    assert not [line for line in printed if line.split()[0] == "chmod"]
    secret_installs = [line for line in printed if line.startswith("install ") and f"{SECRETS_DIR}/" in line]
    assert len(secret_installs) == len(NAMES)
    assert all(" -o root -g 10010 -m 0440 " in line for line in secret_installs)


@pytest.mark.parametrize("missing", [name for name in NAMES if name not in OPTIONAL])
def test_a_missing_required_secret_stops_it(tmp_path: Path, missing: str) -> None:
    done, source = dry_run(tmp_path, tuple(name for name in NAMES if name != missing))
    assert done.returncode == 1
    # It stops before its first step, naming the file it misses.
    assert commands(done.stdout) == []
    assert done.stderr == (
        f"host-setup: {missing} is not in {source} (deploy/secrets.example lists it); nothing was changed\n"
    )
