"""`scripts/rotate-db-role.sh`, a database role's password rotation (spec §19.8 "Secret rotation"; plan M5
Task 28, Deviation 39, R7).

Every case is a `--dry-run`: a real run installs into the host's secrets directory as root and changes a role
of a running database, which no unit test touches (the rotation drill on the compose test stack runs it,
`tests/stack/test_prod_drills.py`). A dry run checks its arguments and the new file (that it holds a value,
read by `grep -q`, which prints nothing), then prints each command it would run, executing none: shims stand
first on PATH for every command that would change something, and record any call. The new file holds a
planted dummy value, which must never appear in what the script prints. The one-off container's script (the
`sh -c` text the dry run prints) is then run here against a recording `psql`, with its `/run/secrets` a
temporary directory, to show where the password goes: into a psql variable, read by psql from the mounted
file, never onto a command line.
"""

import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest
import yaml

from tests.support.prodstack import PT

REPO: Final = Path(__file__).resolve().parents[2]
SCRIPT: Final = REPO / "scripts/rotate-db-role.sh"
PLANTED: Final = "planted-by-the-rotation-dry-run"
BOOTSTRAP: Final = "planted-bootstrap-of-the-dry-run"
ROLES: Final = {
    "db_app": "jobhunter_app",
    "db_migrator": "jobhunter_migrator",
    "db_backup": "jobhunter_backup",
    "db_restore": "jobhunter_restore",
}
# The running services that hold each secret (deploy/compose.yaml), restarted once the role's password
# changed: postgres reads them at its init only, and migrate and restore are one-off.
HOLDERS: Final = {
    "db_app": ["api", "worker"],
    "db_migrator": [],
    "db_backup": ["backup"],
    "db_restore": ["backup"],
}
PROD_COMPOSE: Final = shlex.split(
    "docker compose -p jobhunter --env-file deploy/images.lock --env-file deploy/release.lock "
    "--env-file /etc/jobhunter/jobhunter.env -f deploy/compose.yaml"
)
PROD_SECRETS: Final = "/etc/jobhunter/secrets"
# What changes something in a real run; none may run in a dry run.
CHANGES: Final = ("install", "docker", "psql", "sudo", "chown", "chmod", "rm", "mv", "cp", "tee")
NOTE: Final = "rotate-db-role: "
ONE_OFF: Final = ["run", "--rm", "--no-deps", "-T", "postgres", "sh", "-c"]


@dataclass(frozen=True)
class DryRun:
    done: subprocess.CompletedProcess[str]
    new_file: Path
    calls: list[str]  # the shims' calls: none in a dry run

    @property
    def commands(self) -> list[list[str]]:
        """The commands printed, each split by bash itself (`printf %q` writes a multi-line argument as
        `$'…'`, which `shlex` does not read)."""
        return [split(line) for line in self.done.stdout.splitlines() if not line.startswith(NOTE)]

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


def dry_run(tmp_path: Path, *args: str, value: str = PLANTED, cwd: Path = REPO) -> DryRun:
    """`rotate-db-role.sh --dry-run <args>` with a new file holding `value`; the last two of `args` are the
    secret's name and, when it is `NEW`, the new file's path."""
    new_file = tmp_path / "new-password"
    new_file.write_text(f"{value}\n")
    directory, log = shims(tmp_path)
    argv = [str(SCRIPT), "--dry-run", *(str(new_file) if arg == "NEW" else arg for arg in args)]
    env = {**os.environ, "PATH": f"{directory}:{os.environ['PATH']}"}
    done = subprocess.run(argv, capture_output=True, text=True, env=env, cwd=cwd, timeout=60, check=False)
    calls = log.read_text().splitlines() if log.exists() else []
    return DryRun(done, new_file, calls)


def one_off_script(compose_tail: list[str]) -> tuple[str, list[str]]:
    """The one-off container's `sh -c` text and its arguments, from the compose command that runs it."""
    assert compose_tail[: len(ONE_OFF)] == ONE_OFF, compose_tail
    return compose_tail[len(ONE_OFF)], compose_tail[len(ONE_OFF) + 1 :]


@pytest.mark.parametrize("name", list(ROLES))
def test_the_password_never_reaches_a_command_line(tmp_path: Path, name: str) -> None:
    """The dry run prints the install of the new file, the one-off that sets the role's password and the
    restart, never the value, and runs nothing. The one-off's script, run against a recording psql, gives psql
    the role and the mounted file's path as variables and the bootstrap password in its environment: psql
    reads the new password from the file itself (`\\set pw `cat :'secret'``) and sets it as a variable
    (`:'pw'`), so it is on no command line."""
    ran = dry_run(tmp_path, name, "NEW")
    assert ran.done.returncode == 0, ran.done.stderr
    assert PLANTED not in ran.done.stdout + ran.done.stderr
    assert ran.calls == []
    assert ran.commands[0] == [
        "install", "-o", "root", "-g", "10010", "-m", "0440", str(ran.new_file), f"{PROD_SECRETS}/{name}",
    ]  # fmt: skip
    [one_off, *_] = ran.compose_commands(PROD_COMPOSE)
    script, args = one_off_script(one_off)
    assert args == ["rotate-db-role", name, ROLES[name]]
    assert all(PLANTED not in part for command in ran.commands for part in command)

    # The one-off's script, with its secrets mounted under a temporary directory instead of /run/secrets.
    mounted = tmp_path / "run-secrets"
    mounted.mkdir()
    (mounted / name).write_text(f"{PLANTED}\n")
    (mounted / "pg_bootstrap").write_text(f"{BOOTSTRAP}\n")
    recorded = tmp_path / "psql.log"
    fake = tmp_path / "fake-bin"
    fake.mkdir()
    (fake / "psql").write_text(
        "#!/bin/sh\n"
        f'{{ printf "argv:"; printf " [%s]" "$@"; echo; echo "PGPASSWORD:${{PGPASSWORD:+set}}"; '
        f"echo stdin:; cat; }} > {shlex.quote(str(recorded))}\n"
    )
    (fake / "psql").chmod(0o755)
    local = script.replace("/run/secrets", str(mounted))
    env = {**os.environ, "PATH": f"{fake}:{os.environ['PATH']}", "POSTGRES_USER": "jobhunter_bootstrap"}
    done = subprocess.run(
        ["sh", "-c", local, *args], capture_output=True, text=True, env=env, timeout=30, check=False
    )
    assert done.returncode == 0, done.stderr
    record = recorded.read_text()
    argv_line = record.splitlines()[0]
    assert PLANTED not in argv_line and BOOTSTRAP not in argv_line
    assert f"[role={ROLES[name]}]" in argv_line and f"[secret={mounted}/{name}]" in argv_line
    assert "[-h] [postgres] [-U] [jobhunter_bootstrap]" in argv_line
    assert "PGPASSWORD:set" in record
    stdin = record.split("stdin:\n", 1)[1]
    assert "\\set pw `cat :'secret'`" in stdin and "ALTER ROLE :\"role\" PASSWORD :'pw';" in stdin
    assert PLANTED not in stdin and PLANTED not in done.stdout + done.stderr


@pytest.mark.parametrize("name", list(ROLES))
def test_it_restarts_the_holders_of_each_secret(tmp_path: Path, name: str) -> None:
    """After the role's password, the services that hold the secret are restarted: api and worker for
    `db_app`, backup for `db_backup` and `db_restore`; `db_migrator` is held by none (migrate and restore are
    one-off), so nothing is restarted and the script says so."""
    services = yaml.safe_load((REPO / "deploy/compose.yaml").read_text(encoding="utf-8"))["services"]
    holding = [
        service
        for service, spec in services.items()
        if name in spec.get("secrets", []) and service not in ("postgres", "migrate", "restore")
    ]
    assert holding == HOLDERS[name]
    ran = dry_run(tmp_path, name, "NEW")
    assert ran.done.returncode == 0, ran.done.stderr
    tails = ran.compose_commands(PROD_COMPOSE)
    assert tails[0][: len(ONE_OFF)] == ONE_OFF
    if HOLDERS[name]:
        assert tails[1:] == [["restart", *HOLDERS[name]]]
    else:
        assert tails[1:] == []
        assert f"{NOTE}no running service holds {name}: nothing to restart" in ran.done.stdout.splitlines()
    assert ran.done.stdout.splitlines()[0] == f"{NOTE}dry run: the commands are printed, none is run"


def test_the_defaults_are_the_prod_command_line_and_secrets_directory(tmp_path: Path) -> None:
    """Without `--compose` and `--secrets-dir`: Task 21's prod command line, from the checkout (the RUNBOOK
    calls it without either), and /etc/jobhunter/secrets."""
    ran = dry_run(tmp_path, "db_app", "NEW", cwd=tmp_path)
    assert ran.done.returncode == 0, ran.done.stderr
    assert ran.commands[0][-1] == f"{PROD_SECRETS}/db_app"
    assert ran.commands[1] == ["cd", str(REPO)]  # the prod command line's paths are the checkout's
    composed = [command for command in ran.commands if command[:2] == ["docker", "compose"]]
    assert len(composed) == 2 and all(command[: len(PROD_COMPOSE)] == PROD_COMPOSE for command in composed)


def test_compose_and_secrets_dir_replace_both(tmp_path: Path) -> None:
    """The drill's call: PT's command line and `.secrets/test` (relative to where it is called). The dry run
    names neither /etc/jobhunter nor the project `jobhunter`: every compose command is PT's."""
    ran = dry_run(tmp_path, "--compose", shlex.join(PT), "--secrets-dir", ".secrets/test", "db_app", "NEW")
    assert ran.done.returncode == 0, ran.done.stderr
    assert "/etc/jobhunter" not in ran.done.stdout
    assert ran.commands[0][-1] == f"{REPO}/.secrets/test/db_app"
    composed = [command for command in ran.commands if command[:2] == ["docker", "compose"]]
    assert len(composed) == 2 and all(command[: len(PT)] == PT for command in composed)
    assert not any(
        command[i : i + 2] == ["-p", "jobhunter"] for command in composed for i in range(len(command))
    )
    assert ran.calls == []


@pytest.mark.parametrize(
    ("args", "value", "message"),
    [
        (["db_bootstrap", "NEW"], PLANTED, "unknown secret db_bootstrap"),
        (["db_app", "NEW"], " \n", "holds no password"),
        (["db_app"], PLANTED, "usage:"),
    ],
)
def test_an_unknown_secret_or_an_empty_file_is_refused(
    tmp_path: Path, args: list[str], value: str, message: str
) -> None:
    """Refused before anything is printed as a command: an unknown secret name, a new file with no password
    (an empty password would leave the role without one), a missing argument."""
    ran = dry_run(tmp_path, *args, value=value)
    assert ran.done.returncode != 0
    assert message in ran.done.stderr
    assert ran.commands == [] and ran.calls == []
