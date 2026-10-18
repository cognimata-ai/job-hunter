"""PostgreSQL container harness for the integration and crash tests (spec §19.1, §19.3).

`start_pg_server` runs the digest-pinned `pgvector` image of `deploy/images.lock` with the repository's
`postgresql.conf` and `postgres/init` scripts, so the roles, extensions, template database and default
privileges under test are the ones production gets. Role passwords are random per server and reach the
container only as files in a read-only mount; they appear on no command line and in no log.
"""

import asyncio
import base64
import os
import secrets
import socket
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Literal
from urllib.parse import quote

import psycopg
import pytest
from jobhunter.config import ENV_PREFIXES, ROLE_SECRETS, AppConfig, Role, load_config
from psycopg.rows import TupleRow

PgRole = Literal["superuser", "migrator", "app", "backup", "restore"]

_REPO_ROOT: Final = Path(__file__).resolve().parents[2]
_IMAGES_LOCK: Final = _REPO_ROOT / "deploy" / "images.lock"
_POSTGRES_DIR: Final = _REPO_ROOT / "deploy" / "postgres"
# The scrubbed fixtures a test worker is served from (M1: `JOBHUNTER_FAKE_SOURCES`, no provider key needed).
FIXTURES_DIR: Final = _REPO_ROOT / "tests" / "contract" / "fixtures"

# Database user and secret file (in the container's /run/secrets) of every PgRole.
_DB_USER: Final[Mapping[PgRole, str]] = MappingProxyType(
    {
        "superuser": "jobhunter_bootstrap",
        "migrator": "jobhunter_migrator",
        "app": "jobhunter_app",
        "backup": "jobhunter_backup",
        "restore": "jobhunter_restore",
    }
)
_PASSWORD_FILE: Final[Mapping[PgRole, str]] = MappingProxyType(
    {
        "superuser": "pg_bootstrap",
        "migrator": "db_migrator",
        "app": "db_app",
        "backup": "db_backup",
        "restore": "db_restore",
    }
)
_APP_ROLES: Final[tuple[Role, ...]] = ("api", "worker", "migrate")

_DB_NAME: Final = "jobhunter"
_READY_MESSAGE: Final = "database system is ready to accept connections"
_START_TIMEOUT_S: Final = 60.0
_POLL_INTERVAL_S: Final = 0.25
_LOG_TAIL_LINES: Final = 50
_PULL_TIMEOUT_S: Final = 600.0
_PORT_ATTEMPTS: Final = 3
# Every server carries the pid of the test process that started it, so a later run can remove what a killed
# run left behind.
_OWNER_LABEL: Final = "jh-test-pg.pid"

# The one copy of the statement that inserts a queued manual run over the active configuration.
MANUAL_RUN_SQL: Final = (
    "INSERT INTO runs (trigger,scheduled_slot,status,include_paid,settings_version_id,scope_version_id) "
    "SELECT 'manual',NULL,'queued',false,settings_version_id,scope_version_id FROM active_config RETURNING id"
)


def pg_image() -> str:
    """The `pgvector` line of `deploy/images.lock`: `repo:tag@sha256:digest`."""
    for line in _IMAGES_LOCK.read_text(encoding="utf-8").splitlines():
        name, separator, reference = line.partition("=")
        if separator and name.strip() == "pgvector":
            return reference.strip()
    raise LookupError("deploy/images.lock has no pgvector line")


@dataclass(frozen=True)
class PgServer:
    container: str
    host: str
    port: int
    # Keyed by PgRole. Not part of repr() (a server shown in a failure report must not leak), nor of ==/hash.
    passwords: Mapping[str, str] = field(repr=False, compare=False)
    workdir: Path

    def dsn(
        self, role: PgRole, db: str = _DB_NAME, *, driver: Literal["psycopg", "sqlalchemy"] = "psycopg"
    ) -> str:
        scheme = "postgresql+psycopg" if driver == "sqlalchemy" else "postgresql"
        password = quote(self.passwords[role], safe="")
        return f"{scheme}://{_DB_USER[role]}:{password}@{self.host}:{self.port}/{quote(db, safe='')}"

    def secrets_dir(self, role: Role) -> Path:
        """The secret files given to the application role `role`: exactly `ROLE_SECRETS[role].required`."""
        return self.workdir / "secrets" / role

    def logs(self) -> str:
        """Everything the container logged so far; PostgreSQL logs to stderr, so both streams are combined."""
        done = subprocess.run(
            ["docker", "logs", self.container],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=60,
        )
        return done.stdout


def _docker(*args: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=timeout,
    )


def _require_docker() -> None:
    try:
        ok = subprocess.run(["docker", "info"], capture_output=True, check=False, timeout=60).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        ok = False
    if not ok:
        pytest.fail("start dockerd first: dockerd &", pytrace=False)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _write_secret(path: Path, value: str, mode: int) -> None:
    """Create `path` with `mode` from the start, whatever the umask, so a secret is never briefly readable."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "w", encoding="ascii") as handle:
        os.fchmod(handle.fileno(), mode)
        handle.write(value)


def _make_dir(path: Path) -> Path:
    # 0755: the postgres user of the container (uid 999) must be able to traverse the harness mounts.
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o755)
    return path


def _write_secrets(workdir: Path) -> dict[str, str]:
    """Write the secret files of the server and of every application role; return the role passwords."""
    passwords: dict[str, str] = {role: secrets.token_hex(24) for role in _PASSWORD_FILE}

    # Mounted into the container as /run/secrets. The real UID/GID arrangement is tested in Task 18.
    pgsecrets = _make_dir(workdir / "pgsecrets")
    for pg_role, file_name in _PASSWORD_FILE.items():
        _write_secret(pgsecrets / file_name, passwords[pg_role], 0o444)

    # What each application role is given: its required secrets and nothing else.
    shared: dict[str, str] = {
        "db_app": passwords["app"],
        "db_migrator": passwords["migrator"],
        "session_secret": base64.b64encode(os.urandom(32)).decode("ascii"),
        "data_key": base64.b64encode(os.urandom(32)).decode("ascii"),
    }
    _make_dir(workdir / "secrets")
    for app_role in _APP_ROLES:
        role_dir = _make_dir(workdir / "secrets" / app_role)
        for name in sorted(ROLE_SECRETS[app_role].required):
            _write_secret(role_dir / name, shared[name], 0o440)
    return passwords


def _container_status(server: PgServer) -> str:
    done = _docker("inspect", "--format", "{{.State.Status}}", server.container)
    return done.stdout.strip() if done.returncode == 0 else "missing"


def _app_role_connects(server: PgServer) -> bool:
    try:
        with psycopg.connect(server.dsn("app"), connect_timeout=2):
            return True
    except psycopg.Error:
        return False


def _log_tail(server: PgServer) -> str:
    return "\n".join(server.logs().splitlines()[-_LOG_TAIL_LINES:])


def _wait_ready(server: PgServer, *, ready_messages: int = 0) -> None:
    """Wait until the app role connects and the log shows `ready_messages` readiness lines.

    The first start of the image logs the readiness line twice: for the temporary server that runs the init
    scripts, and for the final one.
    """
    deadline = time.monotonic() + _START_TIMEOUT_S
    while True:
        status = _container_status(server)
        if status != "running":
            pytest.fail(
                f"postgres container {server.container} is {status}; last log lines:\n{_log_tail(server)}",
                pytrace=False,
            )
        if _app_role_connects(server) and server.logs().count(_READY_MESSAGE) >= ready_messages:
            return
        if time.monotonic() >= deadline:
            pytest.fail(
                f"postgres container {server.container} not ready after {_START_TIMEOUT_S:.0f} s; "
                f"last log lines:\n{_log_tail(server)}",
                pytrace=False,
            )
        time.sleep(_POLL_INTERVAL_S)


def _ensure_image() -> None:
    """Make the pinned image present first, so the timeout of `docker run` never covers a pull."""
    image = pg_image()
    if _docker("image", "inspect", image).returncode == 0:
        return
    pulled = _docker("pull", image, timeout=_PULL_TIMEOUT_S)
    if pulled.returncode != 0:
        pytest.fail(f"docker pull {image} failed:\n{pulled.stderr.strip()}", pytrace=False)


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # it exists, it just belongs to someone else
    return True


def remove_stale_pg_servers() -> None:
    """Remove (with their volumes) the servers whose starting test process no longer exists.

    A hard kill (SIGKILL, an OOM kill) skips every fixture teardown, so the container outlives its run.
    Never touches a server whose process is alive, such as a concurrent run's.
    """
    listing = _docker(
        "ps",
        "-a",
        "--filter",
        f"label={_OWNER_LABEL}",
        "--format",
        '{{.Names}} {{.Label "' + _OWNER_LABEL + '"}}',
    )
    for line in listing.stdout.splitlines():
        name, _, pid_text = line.partition(" ")
        if pid_text.strip().isdigit() and not _process_alive(int(pid_text)):
            _remove_container(name)


def _run_container(name: str, port: int, workdir: Path) -> subprocess.CompletedProcess[str]:
    # fmt: off
    return _docker(
        "run",
        "-d",
        "--name", name,
        "--label", f"{_OWNER_LABEL}={os.getpid()}",
        "-p", f"127.0.0.1:{port}:5432",
        "-e", "POSTGRES_USER=jobhunter_bootstrap",
        "-e", f"POSTGRES_DB={_DB_NAME}",
        "-e", "POSTGRES_PASSWORD_FILE=/run/secrets/pg_bootstrap",
        "-v", f"{workdir / 'pgsecrets'}:/run/secrets:ro",
        "-v", f"{_POSTGRES_DIR / 'init'}:/docker-entrypoint-initdb.d:ro",
        "-v", f"{_POSTGRES_DIR / 'postgresql.conf'}:/etc/postgresql/postgresql.conf:ro",
        pg_image(),
        "postgres",
        "-c", "config_file=/etc/postgresql/postgresql.conf",
        "-c", "shared_buffers=128MB",
        "-c", "effective_cache_size=512MB",
        "-c", "max_connections=150",
    )
    # fmt: on


def _start_container(name: str, workdir: Path) -> int:
    """Run the container on a free host port and return the port.

    The port is probed and then released, so another process can take it in between. Docker then refuses
    to publish it and the start is tried again with a new port. (A Docker-assigned ephemeral port is no
    option: Docker gives a container a new one at every `docker start`, and `PgServer.port` must survive
    `pg_stop` / `pg_start` / `restart_pg_server`.)
    """
    for attempt in range(1, _PORT_ATTEMPTS + 1):
        port = _free_port()
        run = _run_container(name, port, workdir)
        if run.returncode == 0:
            return port
        _remove_container(name)  # a refused publish leaves the container behind in state "created"
        port_taken = "address already in use" in run.stderr or "port is already allocated" in run.stderr
        if not port_taken or attempt == _PORT_ATTEMPTS:
            pytest.fail(f"docker run failed:\n{run.stderr.strip()}", pytrace=False)
    raise AssertionError("unreachable")


def start_pg_server(workdir: Path) -> PgServer:
    """Start a fresh, initialised PostgreSQL container with its secrets under `workdir`.

    If the container never becomes ready it is removed (with its anonymous volume) and the failure message
    carries the last 50 log lines.
    """
    _require_docker()
    _ensure_image()
    remove_stale_pg_servers()
    passwords = _write_secrets(workdir)
    name = f"jh-test-pg-{secrets.token_hex(4)}"
    started = False
    try:
        port = _start_container(name, workdir)
        server = PgServer(
            container=name,
            host="127.0.0.1",
            port=port,
            passwords=MappingProxyType(passwords),
            workdir=workdir,
        )
        _wait_ready(server, ready_messages=2)
        started = True
        return server
    finally:
        if not started:
            _remove_container(name)


def _remove_container(name: str) -> None:
    """`docker rm -f -v`: the image declares an anonymous data volume, which `-v` removes too."""
    done = _docker("rm", "-f", "-v", name)
    if done.returncode != 0 and "No such container" not in done.stderr:
        raise RuntimeError(f"docker rm {name} failed: {done.stderr.strip()}")


def remove_pg_server(server: PgServer) -> None:
    """Remove the container and its anonymous data volume; removing a container that is gone is fine."""
    _remove_container(server.container)


def _docker_or_fail(*args: str) -> None:
    done = _docker(*args)
    if done.returncode != 0:
        pytest.fail(f"docker {args[0]} failed: {done.stderr.strip()}", pytrace=False)


def pg_stop(server: PgServer) -> None:
    _docker_or_fail("stop", server.container)


def pg_start(server: PgServer) -> None:
    _docker_or_fail("start", server.container)
    _wait_ready(server)


def restart_pg_server(server: PgServer) -> None:
    _docker_or_fail("restart", server.container)
    _wait_ready(server)


def server_env(server: PgServer, role: Role, **env: str) -> dict[str, str]:
    """The `JOBHUNTER_*` environment of `role` against `server`; `env` adds to it or overrides it.

    The worker is in fake mode: its sources are served from `FIXTURES_DIR` (it has no provider secret).
    """
    fake_sources = (
        {"JOBHUNTER_FAKE_SOURCES": "1", "JOBHUNTER_FIXTURES_DIR": str(FIXTURES_DIR)}
        if role == "worker"
        else {}
    )
    return {
        "JOBHUNTER_ENV": "test",
        "JOBHUNTER_DB_HOST": server.host,
        "JOBHUNTER_DB_PORT": str(server.port),
        "JOBHUNTER_DB_NAME": _DB_NAME,
        "JOBHUNTER_PUBLIC_ORIGIN": "https://testserver",
        "JOBHUNTER_SECRETS_DIR": str(server.secrets_dir(role)),
        **fake_sources,
        **env,
    }


def config_for(server: PgServer, role: Role, **env: str) -> AppConfig:
    """The `AppConfig` of `role` against `server`, loaded from the harness environment only."""
    return load_config(role, server_env(server, role, **env))


def process_env(server: PgServer, role: Role, **extra: str) -> dict[str, str]:
    """The environment of a child process of `role` against `server`.

    It is the test process's environment without the variables the application reads (`JOBHUNTER_*`,
    `LANGSMITH_*`, `LANGCHAIN_*`, `ANTHROPIC_*`), plus `server_env` and `extra`.
    """
    inherited = {name: value for name, value in os.environ.items() if not name.startswith(ENV_PREFIXES)}
    return {**inherited, **server_env(server, role), **extra}


def pg_rows(
    server: PgServer, role: PgRole, statement: str, *params: object, db: str = _DB_NAME
) -> list[tuple[object, ...]]:
    """Run `statement` as `role` in autocommit mode; `[]` for a statement that returns no rows."""
    with psycopg.connect(server.dsn(role, db), autocommit=True) as conn:
        cursor = conn.execute(statement, params or None)
        return cursor.fetchall() if cursor.description is not None else []


def pg_one(
    server: PgServer, role: PgRole, statement: str, *params: object, db: str = _DB_NAME
) -> tuple[object, ...] | None:
    """The first row of `statement` run as `role`, or `None` for a statement without rows."""
    rows = pg_rows(server, role, statement, *params, db=db)
    return rows[0] if rows else None


def lock_waits(server: PgServer) -> int:
    """How many lock requests wait right now (`pg_locks` rows not granted), across the whole server.

    A barrier's readiness acknowledgement: a statement that another transaction's lock holds back shows up
    here.
    """
    row = pg_one(server, "superuser", "SELECT count(*) FROM pg_locks WHERE NOT granted")
    return 0 if row is None else int(str(row[0]))


async def blocked_or_done(server: PgServer, task: asyncio.Task[Any]) -> bool:
    """Whether `task` has ended or some statement waits for a lock: what `await_until` polls at a barrier."""
    return task.done() or lock_waits(server) > 0


def _insert_id(conn: psycopg.Connection[TupleRow], statement: str, *params: object) -> int:
    row = conn.execute(statement, params).fetchone()
    if row is None:
        raise LookupError(f"no id returned by: {statement}")
    return int(row[0])


def ensure_config_rows(conn: psycopg.Connection[TupleRow]) -> tuple[int, int]:
    """Make sure a settings version exists; return `(settings_version_id, scope_version_id)` of the lowest.

    When `settings_versions` is empty it inserts a minimal chain (a scope, model, execution and evaluation
    version with random hashes and an empty payload, and a settings version over them). Nothing is committed:
    the caller's transaction decides.
    """
    lowest = conn.execute("SELECT id, scope_version_id FROM settings_versions ORDER BY id LIMIT 1").fetchone()
    if lowest is None:
        scope_id = _insert_id(
            conn,
            "INSERT INTO scope_versions (scope_hash, payload) VALUES (%s, '{}') RETURNING id",
            os.urandom(32),
        )
        model_id = _insert_id(
            conn,
            "INSERT INTO model_policies (policy_hash, payload) VALUES (%s, '{}') RETURNING id",
            os.urandom(32),
        )
        execution_id = _insert_id(
            conn,
            "INSERT INTO execution_configs (config_hash, payload) VALUES (%s, '{}') RETURNING id",
            os.urandom(32),
        )
        evaluation_id = _insert_id(
            conn,
            "INSERT INTO evaluation_policies (version_no, policy_hash, payload, projected_critic_rate) "
            "VALUES ((SELECT coalesce(max(version_no), 0) + 1 FROM evaluation_policies), %s, '{}', 0) "
            "RETURNING id",
            os.urandom(32),
        )
        settings_id = _insert_id(
            conn,
            "INSERT INTO settings_versions (version_no, schema_version, payload, scope_version_id, "
            "model_policy_id, execution_config_id, evaluation_policy_id, effect_classes) "
            "VALUES (1, 1, '{}', %s, %s, %s, %s, ARRAY['none']) RETURNING id",
            scope_id,
            model_id,
            execution_id,
            evaluation_id,
        )
        return settings_id, scope_id
    return int(lowest[0]), int(lowest[1])
