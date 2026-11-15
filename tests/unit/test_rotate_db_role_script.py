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
import sys
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
    "pg_bootstrap": [],  # postgres reads it at its init only (M5 follow-ups Task F4 (c))
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


# A psql for the bootstrap one-off: the role's password is FAKE_ROLE_PASSWORD's content, and a login with
# another PGPASSWORD is refused as postgres refuses it (exit 2). Each call appends to FAKE_PSQL_LOG its argv,
# whether it logged in with the old (FAKE_OLD) or the new (FAKE_NEW) password, whether a `secret=` file holds
# the new one, and its stdin (no value); an ALTER (a call without `-c`) sets the role's password to that
# file's.
FAKE_PSQL: Final = """#!/bin/sh
role=$(cat "$FAKE_ROLE_PASSWORD")
{
    echo "call:"
    printf "argv:"; printf " [%s]" "$@"; echo
    [ "$PGPASSWORD" = "$FAKE_OLD" ] && echo "PGPASSWORD:old"
    [ "$PGPASSWORD" = "$FAKE_NEW" ] && echo "PGPASSWORD:new"
    for arg in "$@"; do
        case $arg in secret=*) [ "$(cat "${arg#secret=}")" = "$FAKE_NEW" ] && echo "secret:new" ;; esac
    done
} >> "$FAKE_PSQL_LOG"
if [ "$PGPASSWORD" != "$role" ]; then
    echo 'psql: error: FATAL:  password authentication failed for user "jobhunter_bootstrap"' >&2
    exit 2
fi
case " $* " in *" -c "*) exit 0 ;; esac
sql=$(cat)
{ echo "stdin:"; printf '%s\\n' "$sql"; } >> "$FAKE_PSQL_LOG"
for arg in "$@"; do case $arg in secret=*) cat "${arg#secret=}" > "$FAKE_ROLE_PASSWORD" ;; esac; done
"""
# A docker whose `compose … run … sh -c <script> <args>` runs the one-off's script here (`/run/secrets` the
# secrets directory the script installs into, `/dev/shm` a temporary directory, POSTGRES_USER the bootstrap
# role's name, the caller's stdin) and whose `restart` exits 0; each call is recorded in FAKE_CALLS.
FAKE_DOCKER: Final = f"""#!{sys.executable}
import os, subprocess, sys

args = sys.argv[1:]
with open(os.environ["FAKE_CALLS"], "a") as out:
    out.write(("run" if "run" in args else " ".join(args[args.index("restart") :])) + "\\n")
if "run" in args:
    at = args.index("sh")
    script = args[at + 2].replace("/run/secrets", os.environ["FAKE_SECRETS"])
    script = script.replace("/dev/shm", os.environ["FAKE_SHM"])
    env = {{**os.environ, "POSTGRES_USER": "jobhunter_bootstrap"}}
    sys.exit(subprocess.run(["/bin/sh", "-c", script, *args[at + 3 :]], env=env).returncode)
"""
FAKE_INSTALL: Final = """#!/bin/sh
for arg; do source=$target; target=$arg; done
echo "install $target" >> "$FAKE_CALLS"
cp "$source" "$target"
"""


@dataclass(frozen=True)
class BootstrapSite:
    """The fakes of a `pg_bootstrap` rotation: the secrets directory (the one-off's `/run/secrets`), its
    `/dev/shm`, the fakes' directory, the role's password and the logs."""

    secrets: Path
    shm: Path
    bin: Path
    role: Path
    psql_log: Path
    calls_log: Path

    def env(self) -> dict[str, str]:
        return {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "POSTGRES_USER": "jobhunter_bootstrap",
            "FAKE_ROLE_PASSWORD": str(self.role),
            "FAKE_PSQL_LOG": str(self.psql_log),
            "FAKE_CALLS": str(self.calls_log),
            "FAKE_SECRETS": str(self.secrets),
            "FAKE_SHM": str(self.shm),
            "FAKE_OLD": BOOTSTRAP,
            "FAKE_NEW": PLANTED,
        }

    def one_off(self, script: str, args: list[str]) -> subprocess.CompletedProcess[str]:
        """The one-off's script run here, with the new password on its stdin."""
        local = script.replace("/run/secrets", str(self.secrets)).replace("/dev/shm", str(self.shm))  # noqa: S108
        return subprocess.run(
            ["sh", "-c", local, *args],
            input=f"{PLANTED}\n",
            capture_output=True,
            text=True,
            env=self.env(),
            timeout=30,
            check=False,
        )

    def full_run(self, tmp_path: Path) -> subprocess.CompletedProcess[str]:
        """`rotate-db-role.sh pg_bootstrap`, not a dry run, on the fakes."""
        new_file = tmp_path / "new-password"
        new_file.write_text(f"{PLANTED}\n")
        argv = [str(SCRIPT), "--compose", "docker compose -p fake", "--secrets-dir", str(self.secrets)]
        return subprocess.run(
            [*argv, "pg_bootstrap", str(new_file)],
            capture_output=True,
            text=True,
            env=self.env(),
            cwd=REPO,
            timeout=60,
            check=False,
        )

    def psql_calls(self) -> list[str]:
        text = self.psql_log.read_text() if self.psql_log.exists() else ""
        return [f"call:{block}" for block in text.split("call:")[1:]]

    def calls(self) -> list[str]:
        return self.calls_log.read_text().splitlines() if self.calls_log.exists() else []

    def role_password(self) -> str:
        return self.role.read_text().strip()


def a_bootstrap_site(tmp_path: Path, *, role: str) -> BootstrapSite:
    """The fakes, `BOOTSTRAP` installed as pg_bootstrap and `role` the bootstrap role's password."""
    names = ("site-secrets", "shm", "fake-bin", "role-password", "psql.log", "calls.log")
    site = BootstrapSite(*(tmp_path / name for name in names))
    for directory in (site.secrets, site.shm, site.bin):
        directory.mkdir()
    (site.secrets / "pg_bootstrap").write_text(f"{BOOTSTRAP}\n")
    site.role.write_text(f"{role}\n")
    for name, body in (("psql", FAKE_PSQL), ("docker", FAKE_DOCKER), ("install", FAKE_INSTALL)):
        (site.bin / name).write_text(body)
        (site.bin / name).chmod(0o755)
    (site.bin / "id").write_text("#!/bin/sh\necho 0\n")  # as root
    (site.bin / "id").chmod(0o755)
    return site


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


@pytest.mark.parametrize("name", list(HOLDERS))
def test_it_restarts_the_holders_of_each_secret(tmp_path: Path, name: str) -> None:
    """After the role's password, the services that hold the secret are restarted: api and worker for
    `db_app`, backup for `db_backup` and `db_restore`; `db_migrator` is held by none (migrate and restore are
    one-off), nor is `pg_bootstrap` (postgres reads it at its init only), so nothing is restarted and the
    script says so."""
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


def test_the_bootstrap_password_is_set_with_the_old_one_then_installed(tmp_path: Path) -> None:
    """M5 follow-ups Task F4 (c): `pg_bootstrap` is the password the one-off itself connects with, so its
    order is the other way round. The dry run prints the one-off postgres container first, with the new file
    on its stdin, then the install of the new file, and restarts nothing; never the value. The one-off's
    script, run against a recording psql with its `/run/secrets` holding the installed (old) password and its
    `/dev/shm` a temporary directory, connects as the bootstrap role with the old password in psql's
    environment, reads the new one from its stdin into a file on `/dev/shm`, and gives psql that file's path
    and the role as variables: psql reads the new password itself (`\\set pw `cat :'secret'``) and sets it
    (`:'pw'`), so neither password is on a command line. The file is removed at the end."""
    ran = dry_run(tmp_path, "pg_bootstrap", "NEW")
    assert ran.done.returncode == 0, ran.done.stderr
    assert PLANTED not in ran.done.stdout + ran.done.stderr
    assert ran.calls == []
    printed = [line for line in ran.done.stdout.splitlines() if not line.startswith(NOTE)]
    assert ran.commands[0] == ["cd", str(REPO)]
    assert printed[1].endswith(f" < {ran.new_file}")  # the one-off reads the new password from its stdin
    target = f"{PROD_SECRETS}/pg_bootstrap"
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
    assert len(ran.commands) == 3
    [one_off] = ran.compose_commands(PROD_COMPOSE)
    script, args = one_off_script(one_off)
    assert args == ["rotate-db-role"]
    assert f"{NOTE}no running service holds pg_bootstrap: nothing to restart" in ran.done.stdout.splitlines()

    site = a_bootstrap_site(tmp_path, role=BOOTSTRAP)
    done = site.one_off(script, args)
    assert done.returncode == 0, done.stderr
    probe, alter = site.psql_calls()
    # first a login with the new password, which the role refuses (it still has the old one)
    assert "PGPASSWORD:new" in probe and "[-c] [SELECT 1]" in probe and "stdin:" not in probe
    # then the ALTER, connected with the old password, the new one read by psql from the /dev/shm file
    argv_line = alter.splitlines()[1]
    assert PLANTED not in argv_line and BOOTSTRAP not in argv_line
    assert "[-h] [postgres] [-U] [jobhunter_bootstrap]" in argv_line
    assert "[role=jobhunter_bootstrap]" in argv_line and f"[secret={site.shm}/" in argv_line
    assert "PGPASSWORD:old" in alter and "secret:new" in alter
    stdin = alter.split("stdin:\n", 1)[1]
    assert "\\set pw `cat :'secret'`" in stdin and "ALTER ROLE :\"role\" PASSWORD :'pw';" in stdin
    assert PLANTED not in site.psql_log.read_text() and PLANTED not in done.stdout + done.stderr
    assert site.role_password() == PLANTED
    assert list(site.shm.iterdir()) == []


def test_the_bootstrap_one_off_sets_nothing_when_the_role_has_the_new_password(tmp_path: Path) -> None:
    """Fix round 1, review M1: an earlier run set the role's password and was cut off before its install, so
    the role has the new password and the file still holds the old one. The one-off logs in with the new
    password, says so and sets nothing (no ALTER), and ends well, so the script goes on to install."""
    script, args = one_off_script(dry_run(tmp_path, "pg_bootstrap", "NEW").compose_commands(PROD_COMPOSE)[0])
    site = a_bootstrap_site(tmp_path, role=PLANTED)
    done = site.one_off(script, args)
    assert done.returncode == 0, done.stderr
    [probe] = site.psql_calls()
    assert "PGPASSWORD:new" in probe and "stdin:" not in probe
    assert "the bootstrap role has the new password already (an earlier run set it)" in done.stderr
    assert site.role_password() == PLANTED
    assert list(site.shm.iterdir()) == []


def test_a_bootstrap_rotation_sets_the_password_then_installs_the_file(tmp_path: Path) -> None:
    """The whole script, not a dry run, on fakes (a docker that runs the one-off's script here, an install
    that copies, an `id` of root): the role takes the new password, then the file is installed; nothing
    restarts."""
    site = a_bootstrap_site(tmp_path, role=BOOTSTRAP)
    done = site.full_run(tmp_path)
    assert done.returncode == 0, done.stderr
    assert site.calls() == ["run", f"install {site.secrets}/pg_bootstrap"]
    assert site.role_password() == PLANTED
    assert (site.secrets / "pg_bootstrap").read_text() == f"{PLANTED}\n"
    assert done.stdout.splitlines()[-1] == (
        f"{NOTE}done: the bootstrap superuser has its new password; restarted nothing"
    )
    assert PLANTED not in done.stdout + done.stderr and BOOTSTRAP not in done.stdout + done.stderr


def test_a_bootstrap_run_cut_off_after_its_alter_is_finished_by_a_run_again(tmp_path: Path) -> None:
    """Review M1: the role has the new password and the file still holds the old one (a run cut off between
    its ALTER and its install, which printed nothing about it). A run again does not loop on a false "still
    has its old password": it sets nothing and installs the file."""
    site = a_bootstrap_site(tmp_path, role=PLANTED)
    done = site.full_run(tmp_path)
    assert done.returncode == 0, done.stderr
    assert site.calls() == ["run", f"install {site.secrets}/pg_bootstrap"]
    assert len(site.psql_calls()) == 1  # the login with the new password only, no ALTER
    assert (site.secrets / "pg_bootstrap").read_text() == f"{PLANTED}\n"
    assert done.stdout.splitlines()[-1] == (
        f"{NOTE}done: the bootstrap superuser has its new password; restarted nothing"
    )


def test_a_bootstrap_one_off_that_sets_nothing_says_so_accurately(tmp_path: Path) -> None:
    """Neither the new password nor the installed one logs in (the role has another): the one-off fails, and
    the script says only what it knows, that the password was not set and the file is unchanged, and to run
    it again once the cause is fixed; nothing is installed."""
    site = a_bootstrap_site(tmp_path, role="another-password-entirely")
    done = site.full_run(tmp_path)
    assert done.returncode == 1
    assert site.calls() == ["run"]
    assert (site.secrets / "pg_bootstrap").read_text() == f"{BOOTSTRAP}\n"
    assert done.stderr.splitlines()[-1] == (
        f"{NOTE}the one-off did not set the bootstrap superuser's password (the line above says why; neither "
        f"the new password nor the installed one set it) and {site.secrets}/pg_bootstrap is unchanged: "
        "run it again once the cause is fixed"
    )
    assert "still has its old password" not in done.stderr


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
