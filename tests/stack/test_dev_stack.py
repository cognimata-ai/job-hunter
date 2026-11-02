"""The dev stack (Task 18): digest-pinned images, real UID/GID secret access, the dev part of V21.

M2 Task 23 runs it with the fake-LLM overlay (`deploy/compose.fakellm.yaml`: no request reaches Anthropic) and
a non-empty dummy `anthropic_key` that is no key (`DUMMY_KEY`, given to `scripts/dev-secrets.sh` alone; an
`anthropic_key` that exists already is kept, as the script keeps every secret, and one this module created is
removed at its end), and adds the M2 services: `docparse` runs, the worker reads every secret it is given and
holds the secrets and the parse groups, and a PDF upload is stored on `cv_store` and reaches `text_review`
through the worker and docparse on the `parse_jobs` tmpfs. The checks never read a secret's value into
anything they print: a secret file is compared by its inode, size and modification time.

Run explicitly with `-m stack`. The stack runs as a compose project of its own, `jobhunter-stacktest` (every
compose call passes `-p jobhunter-stacktest`; ruling T21-g), never as the normal dev stack `jobhunter-dev`,
whose database it must not touch. Its images are not its own, though: the build tags them as
`compose.dev.yaml` names them, so it rebuilds the shared `jobhunter-app:dev` image tag (and the other `:dev`
tags it builds), which the next start of `jobhunter-dev` then runs (M0 Carry-forward 7). The fixture removes
every container, network and volume of `jobhunter-stacktest`, and its egress probe container, on every path:
before the start (what a killed earlier run left behind) and after the module, also when the start fails. It
never stops or removes anything of `jobhunter-dev`: when that project has a running container or a network
(its fixed subnet `172.30.0.0/24` and port `127.0.0.1:8080` would collide), the module is skipped with a
message saying so.
"""

import os
import stat
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import httpx
import pytest
from jobhunter.db.alembic_runner import code_head

from tests.support.profile import SYNTHETIC_CV, minimal_pdf

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
    "-f",
    str(REPO / "deploy/compose.fakellm.yaml"),  # M2 Task 23: every Claude call answered by the recorded fake
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
    "anthropic_key",
)
SECRETS_DIR: Final = REPO / ".secrets/dev"
KEY_VARIABLE: Final = "JOBHUNTER_ANTHROPIC_API_KEY"
# What `scripts/dev-secrets.sh` writes as `anthropic_key` when it creates it here: not empty, and no key.
DUMMY_KEY: Final = "dummy-not-a-key"
# Whether this module created `.secrets/dev/anthropic_key` (and so removes it at its end).
_created_key: list[bool] = []
ORIGIN: Final = "http://localhost:8080"
USERNAME: Final = "stacktest"
PASSWORD: Final = "Kupferkessel-Fichtenzweig-4"  # the fixed credential of the stack test's own user
SESSION_COOKIE: Final = "__Host-jh_session"
TEXT_PDF: Final = minimal_pdf(SYNTHETIC_CV.splitlines())
TEXT_REVIEW_WITHIN_S: Final = 120
# The services the stack builds (`up --build`), and those of them whose existing image is used as it is when
# `JOBHUNTER_STACK_PREBUILT` names them (comma-separated, e.g. `embedder`): for a host that can reach PyPI
# but not the embedder's other build sources (its Debian mirror, the model download). Unset, every image is
# built, as always; a named image that does not exist fails the start.
BUILT_SERVICES: Final = ("migrate", "api", "worker", "embedder", "docparse")
PREBUILT: Final = tuple(name for name in os.environ.get("JOBHUNTER_STACK_PREBUILT", "").split(",") if name)


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


def dev_secrets() -> None:
    """`scripts/dev-secrets.sh` on `.secrets/dev` with the dummy key for the script alone (never the key the
    test process may have)."""
    env = {name: value for name, value in os.environ.items() if name != KEY_VARIABLE}
    env[KEY_VARIABLE] = DUMMY_KEY
    subprocess.run(
        [str(REPO / "scripts/dev-secrets.sh")],
        check=True,
        timeout=CALL_TIMEOUT_S,
        env=env,
        capture_output=True,
    )


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
    key_file = SECRETS_DIR / "anthropic_key"
    _created_key[:] = [not key_file.exists() and not key_file.is_symlink()]
    try:
        dev_secrets()
        if PREBUILT:
            built = [name for name in BUILT_SERVICES if name not in PREBUILT]
            subprocess.run([*DC, "build", *built], check=True, timeout=900)
            up = subprocess.run([*DC, "up", "-d", "--wait"], check=False, timeout=900)
        else:
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
        if _created_key == [True]:  # only the file this module created; the others are kept
            key_file.unlink(missing_ok=True)
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
            "assert r.files('jobhunter.geo').joinpath('data/de_places.tsv').is_file()",
            "assert r.files('jobhunter.geo').joinpath('data/non_de_places.tsv').is_file()",
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


def test_embedder_is_healthy_and_readiness_reports_no_embedder_failure(stack) -> None:
    """Task 25: the embedder service passes its healthcheck (`embedder-health`: `/health` 200 with the model's
    checksum verified), and `/readyz`, which probes it (§17.5), ends 200 with no failing code at all."""
    container = subprocess.run(
        [*DC, "ps", "--quiet", "embedder"], capture_output=True, text=True, check=True, timeout=CALL_TIMEOUT_S
    ).stdout.split()
    assert len(container) == 1, container
    health = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Health.Status}}", container[0]],
        capture_output=True,
        text=True,
        check=True,
        timeout=CALL_TIMEOUT_S,
    ).stdout.strip()
    assert health == "healthy"
    deadline = time.monotonic() + 60
    while (r := httpx.get("http://localhost:8080/readyz", timeout=10)).status_code != 200:
        assert r.status_code == 503 and "embedder" not in r.json()["failing"], (r.status_code, r.text)
        assert time.monotonic() < deadline, f"readyz never 200 (last: {r.text})"
        time.sleep(2)
    assert r.json() == {}


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


def unchanged_signature(path: Path) -> tuple[int, int, int]:
    """What shows a file was neither replaced nor rewritten, without reading its value: inode, size, mtime."""
    st = path.stat()
    return st.st_ino, st.st_size, st.st_mtime_ns


def test_secrets_are_group_10010_0440_and_not_overwritten(stack) -> None:  # stack: .secrets/dev exists alone
    d = SECRETS_DIR
    before = {name: unchanged_signature(d / name) for name in SECRET_NAMES}
    dev_secrets()
    assert {name: unchanged_signature(d / name) for name in SECRET_NAMES} == before
    assert {name: mode_and_group(d / name) for name in SECRET_NAMES} == dict.fromkeys(
        SECRET_NAMES, ("0o440", 10010)
    )
    assert mode_and_group(d) == ("0o750", 10010)
    if _created_key == [True]:
        assert (d / "anthropic_key").stat().st_size == len(DUMMY_KEY)  # the dummy, not empty


# --- M2 Task 23: docparse, the worker's secrets and groups, an upload through the text phase -------------


def run_capture(*command: str, stdin: str | None = None, timeout: int = CALL_TIMEOUT_S) -> str:
    """`command` (0 required); its output, which holds no secret's value."""
    done = subprocess.run(command, input=stdin, capture_output=True, text=True, timeout=timeout, check=False)
    assert done.returncode == 0, (done.returncode, done.stdout[-1000:], done.stderr[-1000:])
    return done.stdout


def test_docparse_is_running_without_a_network(stack) -> None:
    (container,) = run_capture(*DC, "ps", "--quiet", "docparse").split()
    state = run_capture(
        "docker", "inspect", "--format", "{{.State.Status}} {{.HostConfig.NetworkMode}}", container
    )
    assert state.split() == ["running", "none"]


WORKER_SECRETS_CHECK: Final = "\n".join(
    [
        "import os, pathlib",
        "root = pathlib.Path('/run/secrets')",
        "names = sorted(p.name for p in root.iterdir())",
        "assert names == ['anthropic_key', 'data_key', 'db_app'], names",
        # every file read through, its length only kept: no value is ever printed
        "empty = [name for name in names if not len((root / name).read_bytes())]",
        "assert {10010, 10011} <= set(os.getgroups()), sorted(os.getgroups())",
        "print(','.join(empty))",
    ]
)


def test_the_worker_reads_every_secret_passes_its_assertions_and_holds_both_groups(stack) -> None:
    empty = run_capture(*DC, "exec", "-T", "worker", "python", "-c", WORKER_SECRETS_CHECK).strip()
    if _created_key == [True]:
        assert empty == ""  # the dummy key is not empty
    else:
        assert empty in ("", "anthropic_key")  # only an earlier run's key may be empty: it is kept
    run_capture(*DC, "exec", "-T", "worker", "jobhunter", "startup-check", "--role", "worker")
    assert set(run_capture(*DC, "exec", "-T", "worker", "id", "-G").split()) >= {"10010", "10011"}
    status = run_capture(*DC, "exec", "-T", "worker", "printenv", "JOBHUNTER_FAKE_LLM").strip()
    assert status == "1"  # the overlay is in force: no Claude call leaves the stack


CV_STORE_CHECK: Final = "\n".join(
    [
        "import pathlib, stat",
        "files = sorted(pathlib.Path('/data/cv').glob('*.bin'))",
        "assert len(files) == 1, len(files)",
        "st = files[0].stat()",
        "assert (st.st_uid, stat.S_IMODE(st.st_mode)) == (10001, 0o600), (st.st_uid, oct(st.st_mode))",
    ]
)


def login(client: httpx.Client) -> dict[str, str]:
    """The stack test's user, created and logged in through devproxy: the headers of its requests."""
    create = ["jobhunter", "user", "create", "--username", USERNAME, "--password-stdin"]
    run_capture(*DC, "run", "--rm", "-T", "api", *create, stdin=f"{PASSWORD}\n", timeout=300)
    r = client.post(
        "/api/auth/login", json={"username": USERNAME, "password": PASSWORD}, headers={"Origin": ORIGIN}
    )
    assert r.status_code == 204, r.status_code
    for set_cookie in r.headers.get_list("set-cookie"):
        pair = set_cookie.split(";", 1)[0].strip()
        if pair.startswith(f"{SESSION_COOKIE}="):
            return {"Cookie": pair, "Origin": ORIGIN, "X-JH-Request": "1"}
    raise AssertionError(f"no {SESSION_COOKIE} cookie in the login's answer")


def test_a_pdf_upload_is_stored_and_reaches_text_review_through_docparse(stack) -> None:
    with httpx.Client(base_url=ORIGIN, timeout=30) as client:
        headers = login(client)
        r = client.post(
            "/api/profile/cv",
            files={"file": ("Lebenslauf.pdf", TEXT_PDF, "application/pdf")},
            headers=headers,
        )
        assert r.status_code == 202, (r.status_code, r.text)
        draft_id = int(r.json()["draft_id"])
        run_capture(
            *DC, "exec", "-T", "api", "python", "-c", CV_STORE_CHECK
        )  # the api wrote it, on the volume
        deadline = time.monotonic() + TEXT_REVIEW_WITHIN_S
        while (draft := client.get(f"/api/profile/drafts/{draft_id}", headers=headers).json())[
            "status"
        ] != "text_review":
            # the worker's text phase: docparse reads the PDF on the parse_jobs tmpfs
            assert draft["status"] in ("uploaded", "extracting_text"), (
                draft["status"],
                draft["failure_code"],
            )
            assert time.monotonic() < deadline, f"no text_review within {TEXT_REVIEW_WITHIN_S} s"
            time.sleep(1)
    assert draft["stripped_text_preview"]  # docparse extracted the text
