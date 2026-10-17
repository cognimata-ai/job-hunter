# tests/stack/test_dev_stack.py
"""The dev stack (Task 18): digest-pinned images, real UID/GID secret access, the dev part of V21.

Run explicitly with `-m stack`. The fixture removes every container, network and volume of the compose project
`jobhunter-dev`, and the egress probe container, on every path: before the start (what a killed earlier run
left behind) and after the module, also when the start fails.
"""

import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from jobhunter.db.alembic_runner import code_head

pytestmark = pytest.mark.stack
REPO = Path(__file__).resolve().parents[2]
DC = [
    "docker",
    "compose",
    "--env-file",
    str(REPO / "deploy/images.lock"),
    "-f",
    str(REPO / "deploy/compose.dev.yaml"),
]
PROBE_NAME = "jh-egress-probe"
LOG_TAIL_LINES = 60


def _remove_stack() -> subprocess.CompletedProcess[str]:
    subprocess.run(["docker", "rm", "-f", PROBE_NAME], capture_output=True, check=False, timeout=120)
    return subprocess.run(
        [*DC, "down", "-v", "--remove-orphans"], capture_output=True, text=True, check=False, timeout=300
    )


def _logs_tail() -> str:
    logs = subprocess.run(
        [*DC, "logs", "--no-color", "--tail", str(LOG_TAIL_LINES)],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    return logs.stdout + logs.stderr


@pytest.fixture(scope="module")
def stack() -> Iterator[None]:
    _remove_stack()
    try:
        subprocess.run([str(REPO / "scripts/dev-secrets.sh")], check=True)
        up = subprocess.run([*DC, "up", "-d", "--build", "--wait"], check=False, timeout=900)
        if up.returncode != 0:
            pytest.fail(f"docker compose up failed ({up.returncode}); last log lines:\n{_logs_tail()}")
        python = next(
            line.split("=", 1)[1]
            for line in (REPO / "deploy/images.lock").read_text().splitlines()
            if line.startswith("python=")
        )
        probe = ["docker", "run", "-d", "--rm", "--name", PROBE_NAME, "--network", "jobhunter-dev_egress"]
        subprocess.run([*probe, python, "python", "-m", "http.server", "8099"], check=True)
        yield
    finally:
        down = _remove_stack()
    assert down.returncode == 0, down.stderr


def exec_(svc: str, code: str) -> int:
    return subprocess.run([*DC, "exec", "-T", svc, "python", "-c", code]).returncode


PROBE = (
    "import socket,sys\ntry:\n socket.create_connection(({host!r},{port}),timeout=3)\n"
    "except OSError:\n sys.exit(1)"
)


def image_check(head: str) -> str:
    """The check run inside the image; `head` is the code head of the test process (ruling T18-a)."""
    return "\n".join(
        [
            "import importlib.resources as r, pathlib, jobhunter",
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
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_dev_stack_serves_through_devproxy(stack) -> None:
    r = httpx.get("http://localhost:8080/healthz", timeout=5)
    assert r.status_code == 200 and r.headers["x-content-type-options"] == "nosniff"
    deadline = time.monotonic() + 60
    while httpx.get("http://localhost:8080/readyz", timeout=5).status_code != 200:
        assert time.monotonic() < deadline, "readyz never 200"
        time.sleep(2)


@pytest.mark.parametrize("role", ["api", "worker", "migrate"])
def test_startup_assertions_pass_under_real_uids(stack, role) -> None:
    r = subprocess.run(
        [*DC, "run", "--rm", "--no-deps", "-T", role, "jobhunter", "startup-check", "--role", role],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    ids = subprocess.run(
        [*DC, "run", "--rm", "--no-deps", "-T", role, "id", "-G"], capture_output=True, text=True
    ).stdout.split()
    assert "10010" in ids


def test_postgres_reads_secrets_as_999_with_group_10010(stack) -> None:
    out = subprocess.run([*DC, "exec", "-T", "postgres", "id"], capture_output=True, text=True).stdout
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


def test_secrets_are_group_10010_0440_and_not_overwritten(stack) -> None:  # stack: .secrets/dev exists alone
    d = REPO / ".secrets/dev"
    before = (d / "db_app").read_bytes()
    subprocess.run([str(REPO / "scripts/dev-secrets.sh")], check=True)
    st = (d / "db_app").stat()
    assert (d / "db_app").read_bytes() == before and oct(st.st_mode & 0o777) == "0o440" and st.st_gid == 10010
