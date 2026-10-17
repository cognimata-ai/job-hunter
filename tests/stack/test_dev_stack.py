"""The dev stack (Task 18): digest-pinned images, real UID/GID secret access, the dev part of V21.

Run explicitly with `-m stack`. The stack runs as a compose project of its own, `jobhunter-stacktest` (every
compose call passes `-p jobhunter-stacktest`; ruling T21-g), never as the normal dev stack `jobhunter-dev`,
whose database it must not touch. The fixture removes every container, network and volume of
`jobhunter-stacktest`, and its egress probe container, on every path: before the start (what a killed earlier
run left behind) and after the module, also when the start fails. It never stops or removes anything of
`jobhunter-dev`: when that project has a running container or a network (its fixed subnet `172.30.0.0/24`
and port `127.0.0.1:8080` would collide), the module is skipped with a message saying so.
"""

import stat
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from jobhunter.db.alembic_runner import code_head

pytestmark = pytest.mark.stack
REPO = Path(__file__).resolve().parents[2]
PROJECT = "jobhunter-stacktest"
DEV_PROJECT = "jobhunter-dev"  # the normal dev stack (`name:` of compose.dev.yaml): only ever looked at
DC = [
    "docker",
    "compose",
    "-p",
    PROJECT,
    "--env-file",
    str(REPO / "deploy/images.lock"),
    "-f",
    str(REPO / "deploy/compose.dev.yaml"),
]
PROBE_NAME = f"{PROJECT}-egress-probe"
LOG_TAIL_LINES = 60
# Every subprocess has a timeout (fix round 1, item 6): a hung docker call fails the test instead of the run.
CALL_TIMEOUT_S = 120
SECRET_NAMES = (
    "session_secret",
    "data_key",
    "pg_bootstrap",
    "db_app",
    "db_migrator",
    "db_backup",
    "db_restore",
)


def _dev_stack_in_use() -> str | None:
    """What of `jobhunter-dev` exists (running containers, networks), or `None`; it changes nothing."""
    found = []
    for kind, command in (
        ("running containers", ["docker", "ps", "--quiet"]),
        ("networks", ["docker", "network", "ls", "--quiet"]),
    ):
        listed = subprocess.run(
            [*command, "--filter", f"label=com.docker.compose.project={DEV_PROJECT}"],
            capture_output=True,
            text=True,
            check=True,
            timeout=CALL_TIMEOUT_S,
        )
        if listed.stdout.split():
            found.append(f"{kind} ({len(listed.stdout.split())})")
    return ", ".join(found) or None


def _remove_stack() -> subprocess.CompletedProcess[str]:
    subprocess.run(
        ["docker", "rm", "-f", PROBE_NAME], capture_output=True, check=False, timeout=CALL_TIMEOUT_S
    )
    return subprocess.run(
        [*DC, "down", "-v", "--remove-orphans"], capture_output=True, text=True, check=False, timeout=300
    )


def _logs_tail() -> str:
    logs = subprocess.run(
        [*DC, "logs", "--no-color", "--tail", str(LOG_TAIL_LINES)],
        capture_output=True,
        text=True,
        check=False,
        timeout=CALL_TIMEOUT_S,
    )
    return logs.stdout + logs.stderr


@pytest.fixture(scope="module")
def stack() -> Iterator[None]:
    in_use = _dev_stack_in_use()
    if in_use is not None:
        pytest.skip(
            f"the dev stack (compose project {DEV_PROJECT}) has {in_use}: its fixed subnet and port 8080 "
            f"would collide with {PROJECT}. This test never stops or removes it; stop it yourself "
            "(`docker compose --env-file deploy/images.lock -f deploy/compose.dev.yaml down`, keeping its "
            "volumes) to run the stack tests."
        )
    _remove_stack()
    try:
        subprocess.run([str(REPO / "scripts/dev-secrets.sh")], check=True, timeout=CALL_TIMEOUT_S)
        up = subprocess.run([*DC, "up", "-d", "--build", "--wait"], check=False, timeout=900)
        if up.returncode != 0:
            pytest.fail(f"docker compose up failed ({up.returncode}); last log lines:\n{_logs_tail()}")
        python = next(
            line.split("=", 1)[1]
            for line in (REPO / "deploy/images.lock").read_text().splitlines()
            if line.startswith("python=")
        )
        probe = ["docker", "run", "-d", "--rm", "--name", PROBE_NAME, "--network", f"{PROJECT}_egress"]
        subprocess.run(
            [*probe, python, "python", "-m", "http.server", "8099"], check=True, timeout=CALL_TIMEOUT_S
        )
        yield
    finally:
        down = _remove_stack()
    assert down.returncode == 0, down.stderr


def exec_(svc: str, code: str) -> int:
    return subprocess.run([*DC, "exec", "-T", svc, "python", "-c", code], timeout=CALL_TIMEOUT_S).returncode


PROBE = (
    "import socket,sys\ntry:\n socket.create_connection(({host!r},{port}),timeout=3)\n"
    "except OSError:\n sys.exit(1)"
)


def image_check(head: str) -> str:
    """The check run inside the image; `head` is the code head of the test process (ruling T18-a)."""
    return "\n".join(
        [
            "import importlib.resources as r, os, pathlib, shutil, jobhunter",
            "from jobhunter.db.alembic_runner import code_head",
            "site = pathlib.Path('/opt/venv/lib/python3.13/site-packages')",
            "assert pathlib.Path(jobhunter.__file__).resolve().is_relative_to(site / 'jobhunter'), "
            "jobhunter.__file__",
            "assert not list(site.glob('*jobhunter*.pth')), 'editable install'",
            "assert r.files('jobhunter.db').joinpath('seed_data/settings_v1.json').is_file()",
            f"assert r.files('jobhunter.db').joinpath('migrations/versions/{head}.py').is_file()",
            "assert r.files('jobhunter.auth').joinpath('common_passwords.txt').is_file()",
            f"assert code_head() == {head!r}, code_head()",
            "assert not pathlib.Path('/src').exists()",
            "assert os.getuid() == 10001, os.getuid()",
            "assert shutil.which('uv') is None, shutil.which('uv')",
        ]
    )


def test_final_image_runs_the_installed_package_with_its_resources(stack) -> None:
    head = code_head()
    # The head's file name is its revision id, so the check names the head migration file of this checkout.
    assert (REPO / f"backend/src/jobhunter/db/migrations/versions/{head}.py").is_file()
    r = subprocess.run(
        ["docker", "run", "--rm", "jobhunter-app:dev", "python", "-I", "-c", image_check(head)],
        capture_output=True,
        text=True,
        timeout=CALL_TIMEOUT_S,
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_dev_stack_serves_through_devproxy(stack) -> None:
    r = httpx.get("http://localhost:8080/healthz", timeout=5)
    assert r.status_code == 200 and r.headers["x-content-type-options"] == "nosniff"
    deadline = time.monotonic() + 60
    while httpx.get("http://localhost:8080/readyz", timeout=5).status_code != 200:
        assert time.monotonic() < deadline, "readyz never 200"
        time.sleep(2)


def run_in(role: str, *command: str) -> subprocess.CompletedProcess[str]:
    """`command` in a one-off container of the compose service `role`, without its dependencies."""
    return subprocess.run(
        [*DC, "run", "--rm", "--no-deps", "-T", role, *command],
        capture_output=True,
        text=True,
        timeout=CALL_TIMEOUT_S,
    )


@pytest.mark.parametrize("role", ["api", "worker", "migrate"])
def test_startup_assertions_pass_under_real_uids(stack, role) -> None:
    r = run_in(role, "jobhunter", "startup-check", "--role", role)
    assert r.returncode == 0, r.stdout + r.stderr
    assert run_in(role, "id", "-u").stdout.split() == ["10001"]
    ids = run_in(role, "id", "-G").stdout.split()
    assert "10010" in ids


def test_postgres_reads_secrets_as_999_with_group_10010(stack) -> None:
    out = subprocess.run(
        [*DC, "exec", "-T", "postgres", "id"], capture_output=True, text=True, timeout=CALL_TIMEOUT_S
    ).stdout
    assert "uid=999" in out and "10010" in out


def test_v21_dev_api_has_no_egress_but_worker_does(stack) -> None:
    deadline = time.monotonic() + 15
    while exec_("worker", PROBE.format(host=PROBE_NAME, port=8099)) != 0:
        assert time.monotonic() < deadline, "probe never reachable from worker"
        time.sleep(1)
    # Control: the probe runs in the api and reaches its own network, so the failures below are the network's.
    assert exec_("api", PROBE.format(host="postgres", port=5432)) == 0
    assert exec_("api", PROBE.format(host="1.1.1.1", port=443)) == 1
    assert exec_("api", PROBE.format(host=PROBE_NAME, port=8099)) == 1


def mode_and_group(path: Path) -> tuple[str, int]:
    st = path.stat()
    return oct(stat.S_IMODE(st.st_mode)), st.st_gid


def test_secrets_are_group_10010_0440_and_not_overwritten(stack) -> None:  # stack: .secrets/dev exists alone
    d = REPO / ".secrets/dev"
    before = {name: (d / name).read_bytes() for name in SECRET_NAMES}
    subprocess.run([str(REPO / "scripts/dev-secrets.sh")], check=True, timeout=CALL_TIMEOUT_S)
    assert {name: (d / name).read_bytes() for name in SECRET_NAMES} == before
    assert {name: mode_and_group(d / name) for name in SECRET_NAMES} == dict.fromkeys(
        SECRET_NAMES, ("0o440", 10010)
    )
    assert mode_and_group(d) == ("0o750", 10010)
