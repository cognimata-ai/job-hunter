"""The compose test stack (plan M5 Task 21): the production compose run with `JOBHUNTER_ENV=test`, its S3 and
HTTP stand-ins, and the helpers every stack module of M5 shares.

- `PT` is the test stack (`deploy/compose.yaml` with `deploy/compose.test.yaml`, project
  `jobhunter-prodtest`), `PB` the same files as the restore target (`jobhunter-prodtest-b`), `S3` the S3
  stand-in (`deploy/compose.s3stub.yaml`, `jobhunter-s3stub`). Each is a `docker compose` argv with
  absolute paths.
- `prod_session(*services)`, which every stack module's fixture wraps: it skips the module while another stack
  holds the dev stack's subnet 172.30.0.0/24 (`other_stack_up`: the dev stack, M4's `jobhunter-stacktest`, or
  a prod-test project a killed run left behind), makes the dummy secrets (`ensure_test_secrets`), starts the
  S3 stand-in first (its network `jobhunter-s3net` must exist before any `PT` or `PB` command names `backup`
  or `restore`), then `PT` with `services` (`STACK_SERVICES` when none is named). At its end, after a failure
  too, it removes `PT`, `PB` and the stand-in with their volumes (`down -v`, M5-U2), each even when an earlier
  one failed. It never touches another compose project.
- `up(argv, *services, build=True)` builds the images first, then `up -d --no-build --wait`; with
  `build=False` (Task 27's `prev` image) nothing is built. The images named in `JOBHUNTER_STACK_PREBUILT`
  (comma-separated, e.g. `embedder`) are never built: their existing image is used as it is.
- `caddy_root_ca`, `stub_log` and `psql` read the running stack: Caddy's internal root certificate (to verify
  TLS against `https://localhost:8443`), what `stubsvc` recorded, and a query's output as the bootstrap role.
  `caddy_root_pem` waits for that root and returns it as text.
- `create_user` makes the stack's single user; `probe` sends requests through Caddy from a client at a known
  address on `edge` (`probe1` at 172.30.1.11, `probe2` at 172.30.1.12), to Caddy's name or to an address of
  it (Ruling T22-3).

No helper prints, logs or copies a secret value: the secret files are made by `scripts/test-secrets.sh` alone.
"""

import json
import os
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Final

import pytest

REPO: Final = Path(__file__).resolve().parents[2]
DEPLOY: Final = REPO / "deploy"
PT_PROJECT: Final = "jobhunter-prodtest"
PB_PROJECT: Final = "jobhunter-prodtest-b"
S3_PROJECT: Final = "jobhunter-s3stub"
# The projects that hold 172.30.0.0/24 when up: the dev stack, M4's stack test, and the two prod-test ones.
SUBNET_HOLDERS: Final = ("jobhunter-dev", "jobhunter-stacktest", PT_PROJECT, PB_PROJECT)


def _compose(project: str, env_files: tuple[str, ...], files: tuple[str, ...]) -> list[str]:
    argv = ["docker", "compose", "-p", project]
    for name in env_files:
        argv += ["--env-file", str(DEPLOY / name)]
    for name in files:
        argv += ["-f", str(DEPLOY / name)]
    return argv


_TEST_ENV: Final = ("images.lock", "compose.test.env")
_TEST_FILES: Final = ("compose.yaml", "compose.test.yaml")
PT: Final[list[str]] = _compose(PT_PROJECT, _TEST_ENV, _TEST_FILES)
PB: Final[list[str]] = _compose(PB_PROJECT, _TEST_ENV, _TEST_FILES)
S3: Final[list[str]] = _compose(S3_PROJECT, ("images.lock",), ("compose.s3stub.yaml",))

# What a stack module brings up by default; `migrate`, `postgres` and `embedder` start with them, as their
# dependencies. `backup` joins with Task 24, whose scheduler its command runs.
STACK_SERVICES: Final[tuple[str, ...]] = ("caddy", "api", "worker", "docparse", "stubsvc")
# The services of the test stack that are built (compose.test.yaml); and those JOBHUNTER_STACK_PREBUILT names.
BUILT_SERVICES: Final = ("migrate", "api", "worker", "backup", "restore", "docparse", "embedder")
PREBUILT: Final = tuple(name for name in os.environ.get("JOBHUNTER_STACK_PREBUILT", "").split(",") if name)
SECRETS_GROUP: Final = 10010
SECRETS_DIR: Final = REPO / ".secrets/test"
ORIGIN: Final = "https://localhost:8443"
CALL_TIMEOUT_S: Final = 180
BUILD_TIMEOUT_S: Final = 1800
UP_TIMEOUT_S: Final = 900
LOG_TAIL_LINES: Final = 80
CADDY_ROOT: Final = "/data/caddy/pki/authorities/local/root.crt"
CADDY_ROOT_WITHIN_S: Final = 60.0
# Run in a probe: the requests of stdin's job, each on a TLS connection of its own to the job's `caddy`
# (Caddy's name or an address of it), as `localhost` (the only name `tls internal` certifies), verified
# against the root given; prints what came back, as JSON.
PROBE_SCRIPT: Final = r"""
import http.client, json, socket, ssl, sys
job = json.load(sys.stdin)
context = ssl.create_default_context(cadata=job["ca"])
answers = []
for method, path, headers, body in job["requests"]:
    connection = http.client.HTTPSConnection("localhost", 443, context=context, timeout=15)
    connection.sock = context.wrap_socket(
        socket.create_connection((job["caddy"], 443), timeout=15), server_hostname="localhost"
    )
    data = None if body is None else json.dumps(body).encode()
    connection.request(method, path, body=data, headers={"Host": "localhost", **headers})
    response = connection.getresponse()
    text = response.read().decode("utf-8", "replace")
    answers.append({"status": response.status, "headers": dict(response.getheaders()), "body": text})
    connection.close()
print(json.dumps(answers))
"""


def run(
    argv: list[str], *, timeout: float = CALL_TIMEOUT_S, check: bool = True, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    """`argv` with its output captured; a failure (when `check`) fails the test with both streams."""
    done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False, input=stdin)
    if check and done.returncode != 0:
        pytest.fail(f"{' '.join(argv[-6:])} exited {done.returncode}:\n{done.stdout}\n{done.stderr}")
    return done


def _project_in_use(project: str) -> str | None:
    """What of `project` exists (running containers, networks), or `None`; it changes nothing."""
    found = []
    for kind, command in (
        ("running containers", ["docker", "ps", "--quiet"]),
        ("networks", ["docker", "network", "ls", "--quiet"]),
    ):
        listed = run([*command, "--filter", f"label=com.docker.compose.project={project}"])
        if names := listed.stdout.split():
            found.append(f"{kind} ({len(names)})")
    return ", ".join(found) or None


def other_stack_up() -> str | None:
    """Which stack holds the dev stack's subnet 172.30.0.0/24, and with what; `None` when none does."""
    for project in SUBNET_HOLDERS:
        if (in_use := _project_in_use(project)) is not None:
            return f"compose project {project} has {in_use}"
    return None


def ensure_test_secrets() -> None:
    """The host group `jobhunter-secrets` (gid 10010) when it is absent, then `scripts/test-secrets.sh` as
    root, which creates the missing dummy secrets of `.secrets/test` and keeps the others (M5-U2)."""
    if run(["getent", "group", str(SECRETS_GROUP)], check=False).returncode != 0:
        run(["sudo", "-n", "groupadd", "--gid", str(SECRETS_GROUP), "jobhunter-secrets"])
    run(["sudo", "-n", str(REPO / "scripts/test-secrets.sh"), str(SECRETS_DIR)])


def logs_tail(argv: list[str], *services: str) -> str:
    done = run([*argv, "logs", "--no-color", "--tail", str(LOG_TAIL_LINES), *services], check=False)
    return done.stdout + done.stderr


def up(argv: list[str], *services: str, build: bool = True) -> None:
    """`up -d --wait` of `services` (`STACK_SERVICES` when none is named) with their dependencies; the images
    built first unless `build` is false, but never one `JOBHUNTER_STACK_PREBUILT` names."""
    if build and (built := [name for name in BUILT_SERVICES if name not in PREBUILT]):
        run([*argv, "build", *built], timeout=BUILD_TIMEOUT_S)
    named = services or STACK_SERVICES
    done = run([*argv, "up", "-d", "--no-build", "--wait", *named], timeout=UP_TIMEOUT_S, check=False)
    if done.returncode != 0:
        pytest.fail(f"up of {named} failed ({done.returncode}):\n{done.stderr}\n{logs_tail(argv)}")


def down_v(argv: list[str]) -> None:
    """The project's containers, networks and volumes removed (`down -v`), the profiled ones included."""
    run([*argv, "--profile", "*", "down", "-v", "--remove-orphans"], timeout=300)


def s3_up() -> None:
    """The S3 stand-in, healthy; it creates the network `jobhunter-s3net`."""
    run([*S3, "up", "-d", "--wait"], timeout=UP_TIMEOUT_S)


def s3_down() -> None:
    run([*S3, "down", "-v", "--remove-orphans"], timeout=300)


@contextmanager
def prod_session(*services: str) -> Iterator[None]:
    """The test stack with `services` up for the duration (module docstring); skipped while another stack
    holds 172.30.0.0/24."""
    holder = other_stack_up()
    if holder is not None:
        pytest.skip(
            f"{holder}: it holds the subnet 172.30.0.0/24 the compose test stack needs. This module never "
            "stops another stack; bring it down yourself (the dev stack: `$DC down`, keeping its volumes; a "
            "prod-test project a killed run left: `docker compose -p <project> … down -v`) to run it."
        )
    ensure_test_secrets()
    with ExitStack() as teardown:
        # Registered in reverse: PT first, then PB, then the stand-in, each even when an earlier one failed.
        teardown.callback(s3_down)
        teardown.callback(down_v, PB)
        teardown.callback(down_v, PT)
        s3_up()
        up(PT, *services)
        yield


def caddy_root_ca(argv: list[str]) -> Path:
    """Caddy's internal root certificate (public, no secret), copied out to a new temporary file."""
    pem = run([*argv, "exec", "-T", "caddy", "cat", CADDY_ROOT]).stdout
    if "BEGIN CERTIFICATE" not in pem:
        pytest.fail("Caddy's internal root certificate is not where it should be")
    handle, name = tempfile.mkstemp(prefix="jh-caddy-root-", suffix=".crt")
    with os.fdopen(handle, "w", encoding="ascii") as file:
        file.write(pem)
    return Path(name)


def caddy_root_pem(argv: list[str]) -> str:
    """Caddy's internal root (public), once Caddy has made it at its start."""
    deadline = time.monotonic() + CADDY_ROOT_WITHIN_S
    while run([*argv, "exec", "-T", "caddy", "test", "-f", CADDY_ROOT], check=False).returncode != 0:
        if time.monotonic() >= deadline:
            pytest.fail("Caddy never made its internal root")
        time.sleep(0.5)
    copy = caddy_root_ca(argv)
    try:
        return copy.read_text(encoding="ascii")
    finally:
        copy.unlink()


def create_user(argv: list[str], username: str, password: str) -> None:
    """The single user, made in the running api container by the user CLI (the password on its stdin)."""
    run(
        [
            *argv,
            "exec",
            "-T",
            "api",
            "jobhunter",
            "user",
            "create",
            "--username",
            username,
            "--password-stdin",
        ],
        stdin=f"{password}\n",
    )


def probe(
    argv: list[str],
    name: str,
    ca: str,
    requests: list[tuple[str, str, dict[str, str], Any]],
    *,
    caddy: str = "caddy",
) -> list[dict[str, Any]]:
    """`requests` (method, path, headers, JSON body or `None`) sent by the probe `name` from its address on
    `edge`, to `caddy` (its name, or an address of it), each answer's status, headers and body."""
    job = json.dumps({"ca": ca, "caddy": caddy, "requests": requests})
    done = run([*argv, "run", "--rm", "-T", name, "python", "-I", "-c", PROBE_SCRIPT], stdin=job)
    answers: list[dict[str, Any]] = json.loads(done.stdout.strip().splitlines()[-1])
    return answers


def stub_log(argv: list[str]) -> list[dict[str, object]]:
    """What `stubsvc` recorded since its start or its last reset (tests/stack/stubs/http_recorder.py)."""
    read = "import sys, urllib.request; sys.stdout.write(urllib.request.urlopen('http://127.0.0.1:8099/_log').read().decode())"
    entries = json.loads(run([*argv, "exec", "-T", "stubsvc", "python", "-I", "-c", read]).stdout)
    if not isinstance(entries, list):
        pytest.fail(f"stubsvc's log is no list: {entries!r}")
    return entries


def psql(argv: list[str], sql: str) -> str:
    """`sql` run by psql as the bootstrap role on the stack's database, its unaligned output (`-AtX`)."""
    script = 'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -AtX -v ON_ERROR_STOP=1 -c "$1"'
    return run([*argv, "exec", "-T", "postgres", "sh", "-c", script, "psql", sql]).stdout
