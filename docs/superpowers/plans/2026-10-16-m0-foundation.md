# M0 Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the tested foundation of Job Hunter: toolchain, config with startup assertions, database roles, the M0 schema with `active_config` and complete default settings, the singleton worker with epochs, takeover and a shutdown coordinator, durable operations, the procrastinate tick with slot runs, an authenticated API skeleton, logging, metrics, a dev stack and CI.

**Architecture:** One Python package `jobhunter` (in `backend/src/`) serves three roles: `api` (FastAPI behind an outermost edge middleware), `worker` (procrastinate plus LangGraph) and `migrate` (async Alembic plus seeding). PostgreSQL 17 with pgvector holds every piece of state: queue, sessions, throttling, operations, runs and checkpoints. The worker owns a session-level advisory lock on a dedicated daemon thread with a stoppable watchdog. A process-local execution registry guarantees that at most one local task runs per owned run or operation. Every state change goes through a CAS `transition()` helper with ownership predicates and automatic clearing of ownership columns.

**Tech Stack:** Python 3.13.16, uv, FastAPI 0.143.2, uvicorn 0.54.0, Pydantic 2.14.1, SQLAlchemy[asyncio] 2.1.4 (Core), Alembic 1.20.0, psycopg 3.3.6, psycopg-pool 3.3.3, procrastinate 3.10.0, langgraph 1.2.15, langgraph-checkpoint-postgres 3.2.0, argon2-cffi 25.1.0, pyotp 2.10.0, cryptography 50.0.2, structlog 26.1.0, prometheus-client 0.26.0, pytest 9.1.1, pytest-asyncio 1.4.0, hypothesis 6.170.0, ruff 0.17.1, mypy 2.4.0; PostgreSQL image `pgvector/pgvector:0.8.7-pg17-bookworm` (digest-pinned); Node 22, Vite 8.3.4, React 19.3.1, openapi-typescript 7.13.0, vitest 5.0.5.

**Spec:** docs/superpowers/specs/2026-10-16-job-hunter-design.md

## Global Constraints

- Python `3.13.16`. Every dependency is pinned exactly in `uv.lock` / `package-lock.json`. A version marked † in §4.1 is re-confirmed at the first `uv lock` / `npm install`, and a differing resolution is recorded in the commit message.
- Exact pins (§4.1): `fastapi==0.143.2`, `uvicorn==0.54.0`, `pydantic==2.14.1`, `sqlalchemy[asyncio]==2.1.4`, `alembic==1.20.0`, `psycopg[binary]==3.3.6`, `psycopg-pool==3.3.3`, `procrastinate==3.10.0`, `langgraph==1.2.15`, `langgraph-checkpoint-postgres==3.2.0`, `langchain-core==1.6.11`, `argon2-cffi==25.1.0`, `pyotp==2.10.0`, `cryptography==50.0.2`, `structlog==26.1.0`, `prometheus-client==0.26.0`, `httpx==0.28.1`, `pytest==9.1.1`, `pytest-asyncio==1.4.0`, `hypothesis==6.170.0`, `ruff==0.17.1`, `mypy==2.4.0`.
- Container images are pinned **by digest** in `deploy/images.lock` and referenced as `name@sha256:…` everywhere (§4.1, §16.11).
- `mypy --strict` passes on `backend/src`. `tests` are type-checked too, with untyped test functions allowed; helper parameters that feed `Literal` parameters are themselves `Literal`-typed. `ruff check` and `ruff format --check` pass.
- Broad `except Exception` is allowed **only** at task boundaries (ruff `BLE001` with per-file allowlists), and every such catch persists an outcome (a state transition or a deduplicated alert) (§17.1).
- Database errors that pass through SQLAlchemy are caught as the `sqlalchemy.exc.DBAPIError` subclasses `IntegrityError`, `ProgrammingError` and `OperationalError`, and classified with `db.sql.pg_error(exc)`, which reads the SQLSTATE and constraint name from `exc.orig` (for example `23505`, `42P01`, `55P03`). Code never names a `psycopg.errors` class: ruff bans that import (Task 1), and code that talks to psycopg directly checks `exc.sqlstate`. A statement that may fail while its transaction must go on runs in a savepoint (`conn.begin_nested()`), and the error is caught outside the savepoint block.
- A transaction whose writes must persist never ends by raising. Code computes an outcome inside `async with engine.begin()`, lets the block commit, and only then raises the `Problem` or returns the error response. Login failures, lockouts, alert resolution and session slides follow this rule.
- State, table and column names are exactly those of §1.6 and §7. Enumerations are `text` + `CHECK`. Every timestamp is `timestamptz`, and Python datetimes are timezone-aware (ruff `DTZ`).
- Every state change goes through `transition()` (§7.16). A transition out of `running` clears that table's ownership columns in the same `UPDATE`. Sweeps loop over rows and call `transition()`.
- Every JSON value bound into SQL uses a `JSONB`-typed bind parameter (`db.sql.q(..., jsonb=...)`, or automatically in `transition()`). Decimal settings values are JSON strings (for example `"100.00"`).
- Global lock order (§3.3.3): `active_config` → job rows → owner row → `llm_budget` → `quota_windows` → ledger rows → other domain rows → `ranking_state`.
- No secret or PII in logs, problem details, metrics or fixtures. The structlog redaction processor runs **last** (§16.8).
- Advisory lock key `7261001`. The worker heartbeat runs every 10 s with a 5 s statement timeout and is stale after 60 s. The watchdog exit code is `70`. A startup-assertion failure exits with `78`. Login throttling serialises on transaction advisory lock `7261002`.
- Run and operation heartbeats: every 30 s, stale after 3 min (§3.3.4).
- Fault hooks (`JOBHUNTER_FAULT`) act only when `JOBHUNTER_ENV=test` and are refused in `prod`.
- Integration and crash tests use the digest-pinned `pgvector/pgvector:0.8.7-pg17-bookworm` container of Task 4, with a clean database per test. They never use the system PostgreSQL 16. Correctness tests never assert wall-clock performance; races and crashes are forced with barriers or readiness acknowledgements.
- No test and no CI job makes a paid API call.
- `Idempotency-Key` matches `^[A-Za-z0-9_-]{16,128}$`. The session cookie is `__Host-jh_session` with `Secure; HttpOnly; SameSite=Strict; Path=/`.
- Every API response, including middleware rejections and 500s, carries `X-Request-Id` and the §16.6 security headers. Problems use `application/problem+json` with `type = "https://jobhunter.local/problems/<code>"` (§14.4).
- Commits use Conventional Commits with the scope `m0`, for example `feat(m0): …`.

## Review Focus

1. **Database restart under a running stack** (a Postgres upgrade or OOM restart). `api` serves again after the database returns, without a restart, and `/readyz` reports `db` while it is down. The worker exits `70` and its replacement resumes. Pinned by Task 14 (`test_api_recovers_after_db_restart`) and Task 8 (`test_watchdog_exits_on_db_restart`).
2. **Long downtime** (the VPS was off for days). The first tick records every missed slot plus exactly one `catch_up` run in one transaction, and a second tick changes nothing. Pinned by Task 11 (`test_long_downtime_records_every_slot_once`).
3. **Unicode and whitespace in credentials.** A password typed as NFD on one keyboard and NFC on another verifies. A username with surrounding spaces or in a different case logs in. Pinned by Task 15 (`test_nfc_and_nfd_passwords_verify_alike`, `test_username_match_ignores_case_and_outer_spaces`).
4. **Run now racing the tick at the same instant.** Exactly one active collection exists, Run now returns it, and neither path fails. Pinned by Task 17 (`test_run_now_blocked_behind_tick_returns_tick_run`, `test_tick_blocked_behind_run_now_records_overlap`).
5. **The wall clock stepping backwards** (an NTP correction or a VM snapshot resume). The tick inserts nothing, never moves its cursor backwards, and resumes normally once time catches up. Pinned by Task 11 (`test_tick_with_clock_behind_cursor_is_noop`).

---

## File Structure

The paths follow §4.2 for the M0 subset. New modules that §4.2 does not list are marked *(new)*.

| Path | Responsibility |
|---|---|
| `pyproject.toml`, `uv.lock`, `ruff.toml`, `mypy.ini`, `.python-version`, `.gitignore` | Toolchain, pins, lint rules (banned API, BLE001 allowlist), strict typing |
| `backend/src/jobhunter/__init__.py` | `__version__` |
| `backend/src/jobhunter/config.py` | Env and secret loading, `AppConfig`, the startup assertions of §16.8 |
| `backend/src/jobhunter/{faults,types,clock,ids,errors,crypto}.py` *(new)* | Crash hooks; `JSONValue`; `utcnow()`; `uuid7()`; domain exceptions; HKDF and AES-256-GCM with key ids |
| `backend/src/jobhunter/cli.py` | argparse CLI: `startup-check`, `migrate`, `worker`, `worker-health`, `http-check`, `user …`, `sessions …` |
| `backend/src/jobhunter/observability/{logging,redact,metrics,alerts,health}.py` | structlog JSON with redaction last; Prometheus registry; alerts with dedup and the open-alerts gauge; readiness and system health |
| `backend/src/jobhunter/db/{session,sql,transitions,alembic_runner,migrate,seed}.py`, `db/seed_data/settings_v1.json` | Engines; JSONB-typed SQL; CAS transitions; Alembic runner; the async `migrate` order of §19.3; idempotent seeds of the complete §13.1 defaults |
| `backend/src/jobhunter/db/migrations/{env.py,script.py.mako,versions/}` | Alembic `0001_m0_baseline`, `0002_m0_transition_matrix` |
| `backend/src/jobhunter/graphs/checkpointer.py`, `graphs/collect/graph.py` | Dedicated autocommit pool and `AsyncPostgresSaver`; M0 no-op `collect_graph` |
| `backend/src/jobhunter/worker/{lock,watchdog,epoch}.py` | Lock session, standby and takeover, stoppable watchdog, epoch claim with takeover sweeps |
| `backend/src/jobhunter/worker/{tasks,reconcilers,heartbeat,executions}.py` | procrastinate blueprint and defer; reconcilers with durable failure alerts; owner heartbeat; process-local execution registry *(executions new)* |
| `backend/src/jobhunter/worker/{operations,tick,runs,shutdown,main}.py` | Operations with claim tokens; slots; run attempts and admission *(runs new)*; shutdown coordinator; worker composition |
| `backend/src/jobhunter/privacy/retention.py` | `retention_daily` rules for the M0 tables |
| `backend/src/jobhunter/auth/{passwords,users,sessions,throttle,totp}.py`, `auth/common_passwords.txt` *(new)* | Argon2id, the single user, sessions, serialised DB-backed throttling, TOTP |
| `backend/src/jobhunter/api/{app,edge,errors,deps,openapi_export}.py`, `api/routes/{health,auth,runs,system}.py`, `api_idempotency.py` | App factory; outermost edge middleware (request id, headers, CSRF, last-resort 500); routes; the `Idempotency-Key` store *(edge new)* |
| `deploy/images.lock`, `deploy/postgres/init/10-init.sh`, `deploy/postgres/postgresql.conf` | Image digests; roles, extensions, template DB, default privileges; server settings of §19.1 |
| `Dockerfile`, `deploy/compose.dev.yaml`, `deploy/Caddyfile.dev`, `scripts/dev-secrets.sh` | App image, dev stack, dev secrets (gitignored output) |
| `frontend/…` | SPA skeleton, typed client, generated OpenAPI types |
| `.github/workflows/ci.yml`, `scripts/check-openapi-drift.sh`, `scripts/smoke_m0.py` | CI, OpenAPI drift check, acceptance smoke test |
| `tests/support/{pg.py,fixtures.py,procs.py}`, `tests/{unit,property,integration,crash,perf,stack}/` | Container harness, isolation fixtures, tracked subprocesses, tests |
| `docs/spikes/V1.md`, `V2.md`, `V6.md`, `V16.md`, `V20.md`, `V21.md` | Spike records |

---

### Task 1: Repository scaffold and toolchain

**Files:**
- Create: `pyproject.toml`, `ruff.toml`, `mypy.ini`, `.python-version`, `.gitignore`, `backend/src/jobhunter/__init__.py`, `backend/src/jobhunter/py.typed`, `tests/__init__.py`, `tests/conftest.py`, `tests/support/__init__.py`, `tests/support/fixtures.py`
- Test: `tests/unit/test_toolchain.py`

**Interfaces:**
- Consumes: nothing.
- Produces: the package `jobhunter` with `__version__: str = "0.1.0"`; the console script `jobhunter = "jobhunter.cli:main"`; pytest markers `integration`, `crash`, `stack`, `slow`, `perf`; `asyncio_mode = "auto"`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_toolchain.py
import re, subprocess, sys
from pathlib import Path
import pytest
REPO = Path(__file__).resolve().parents[2]

def test_package_version() -> None:
    import jobhunter
    assert re.fullmatch(r"\d+\.\d+\.\d+", jobhunter.__version__)

def test_sqlalchemy_async_extra_installed() -> None:
    import greenlet  # noqa: F401  (sqlalchemy[asyncio] pulls it in; 2.1 does not by default)
    from sqlalchemy.ext.asyncio import create_async_engine
    assert create_async_engine("postgresql+psycopg://u:p@127.0.0.1:1/x").dialect.is_async

@pytest.mark.parametrize(("source", "rule"), [
    ("import anthropic\nanthropic.Anthropic()\n", "TID251"),
    ("from langchain_anthropic import ChatAnthropic\n", "TID251"),
    ("def f() -> None:\n    try:\n        pass\n    except Exception:\n        pass\n", "BLE001"),
    ("import datetime\ndatetime.datetime.now()\n", "DTZ005"),
    ("from psycopg.errors import UniqueViolation\n", "TID251"),
])
def test_ruff_rules_fire(tmp_path: Path, source: str, rule: str) -> None:
    target = tmp_path / "probe.py"
    target.write_text(source)
    result = subprocess.run([sys.executable, "-m", "ruff", "check", "--config", str(REPO / "ruff.toml"),
                             "--no-cache", str(target)], capture_output=True, text=True)
    assert result.returncode == 1 and rule in result.stdout
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_toolchain.py -q`
Expected: FAIL, either a uv error about the missing `pyproject.toml` or `ModuleNotFoundError: No module named 'jobhunter'`.

- [ ] **Step 3: Create the toolchain files**

- `pyproject.toml`:
  - `[project] name = "jobhunter"`, `requires-python = "==3.13.*"`, the runtime pins of the Global Constraints plus `segno` (†, terminal QR for TOTP);
  - `[dependency-groups] dev =` pytest, pytest-asyncio, hypothesis, ruff, mypy, httpx, `pip-audit` (†);
  - `[project.scripts] jobhunter = "jobhunter.cli:main"`;
  - hatchling build with `packages = ["backend/src/jobhunter"]`;
  - `[tool.pytest.ini_options]`: `asyncio_mode = "auto"`, `asyncio_default_fixture_loop_scope = "function"`, `testpaths = ["tests"]`, the five markers, `addopts = "-m 'not stack and not perf'"`.
- `ruff.toml`:
  - `target-version = "py313"`, `line-length = 110`;
  - `select = ["E","F","W","I","B","BLE","UP","S","TID","ASYNC","RUF","DTZ"]`;
  - `[lint.flake8-tidy-imports.banned-api]` for `anthropic.Anthropic`, `anthropic.AsyncAnthropic` and `langchain_anthropic.ChatAnthropic`, message `"Only llm/gateway.py may reach Claude (§10.1)"`; and `psycopg.errors`, message `"Catch sqlalchemy.exc.DBAPIError subclasses and classify with db.sql.pg_error; raw psycopg code checks exc.sqlstate"`;
  - per-file-ignores: `backend/src/jobhunter/llm/**` = `TID251`; the task-boundary files `backend/src/jobhunter/worker/{tasks,reconcilers,main,watchdog,operations,runs,shutdown}.py` and `backend/src/jobhunter/api/{edge,errors}.py` = `BLE001`; `tests/**` = `S101,S603,S607`.
- `mypy.ini`: `strict = True`, `files = backend/src, tests`, `plugins = pydantic.mypy`, `[mypy-pyotp.*] ignore_missing_imports = True`, `[mypy-tests.*] disallow_untyped_defs = False` and `disallow_incomplete_defs = False`.
- `.python-version` = `3.13.16`.
- `.gitignore`: `.venv/`, `__pycache__/`, `.secrets/`, `frontend/node_modules/`, `frontend/dist/`, `frontend/openapi.json`.
- `tests/conftest.py`: `pytest_plugins = ["tests.support.fixtures"]`.
- Run `uv lock`. If `uv --version` is not 0.13.1, run `uv self update 0.13.1`; if that fails, keep the installed uv and record its version in the commit message.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv sync && uv run pytest tests/unit/test_toolchain.py -q && uv run mypy && uv run ruff check .`
Expected: PASS (7 passed); mypy `Success: no issues found`; ruff `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock ruff.toml mypy.ini .python-version .gitignore backend tests
git commit -m "feat(m0): repository scaffold, pinned toolchain with sqlalchemy[asyncio], lint and type rules"
```

---

### Task 2: Config, startup assertions and fault hooks

**Files:**
- Create: `backend/src/jobhunter/config.py`, `backend/src/jobhunter/faults.py`, `backend/src/jobhunter/cli.py`
- Test: `tests/unit/test_config.py`, `tests/unit/test_faults.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `Role = Literal["api", "worker", "migrate"]`; `EnvName = Literal["prod", "dev", "test"]`
  - `@dataclass(frozen=True, slots=True) class DbSettings: host: str; port: int; name: str`
  - `@dataclass(frozen=True, slots=True) class AppConfig`: `role: Role`, `env: EnvName`, `db: DbSettings`, `public_origin: str | None`, `log_level: str`, `fault: str | None`, `secrets: Mapping[str, bytes]`, `environ: Mapping[str, str]`, `secrets_dir: Path`; methods `secret(name: str) -> bytes`, `secret_b64(name: str) -> bytes`, `dsn(driver: Literal["sqlalchemy", "psycopg"]) -> str`
  - `ALL_SECRETS: Final[frozenset[str]]` (the 21 names of §19.2); `@dataclass(frozen=True) class RoleSecrets: required: frozenset[str]; optional: frozenset[str]`; `ROLE_SECRETS: Final[Mapping[Role, RoleSecrets]]`
  - `class StartupCheckFailed(Exception)` with attribute `check: str`
  - `load_config(role: Role, environ: Mapping[str, str] | None = None, secrets_dir: Path | None = None) -> AppConfig`
  - `run_startup_assertions(cfg: AppConfig) -> None`
  - `startup_or_exit(role: Role) -> AppConfig` (logs `startup_check_failed` with `check=<name>`, then exits `78`)
  - `faults.fault_point(name: str) -> None`; `async def faults.afault_point(name: str) -> None`; `FAULT_EXIT_CODE: Final = 86`
  - `cli.main(argv: Sequence[str] | None = None) -> int`, with the subcommand `startup-check --role {api,worker,migrate}`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_config.py
import base64, os, subprocess, sys
from pathlib import Path
import pytest
from jobhunter.config import Role, StartupCheckFailed, load_config, run_startup_assertions

def b64(n: int, fill: bytes = b"k") -> bytes: return base64.b64encode(fill * n)

def write_secrets(d: Path, **files: bytes) -> Path:
    d.mkdir(exist_ok=True)
    for name, value in files.items():
        p = d / name; p.write_bytes(value); p.chmod(0o440)
    return d

BASE_ENV = {"JOBHUNTER_ENV": "test", "JOBHUNTER_DB_HOST": "h", "JOBHUNTER_DB_PORT": "5432",
            "JOBHUNTER_DB_NAME": "jobhunter", "JOBHUNTER_PUBLIC_ORIGIN": "https://testserver"}
API_SECRETS = {"session_secret": b64(32, b"s"), "data_key": b64(32, b"d"), "db_app": b"pw"}
WORKER_SECRETS = {"data_key": b64(32), "db_app": b"pw"}

def check_of(role: Role, env: dict[str, str], secrets_dir: Path) -> str:
    with pytest.raises(StartupCheckFailed) as exc:
        run_startup_assertions(load_config(role, env, secrets_dir))
    return exc.value.check

def test_valid_api_config_passes(tmp_path: Path) -> None:
    cfg = load_config("api", BASE_ENV, write_secrets(tmp_path / "s", **API_SECRETS))
    run_startup_assertions(cfg)
    assert cfg.dsn("sqlalchemy").startswith("postgresql+psycopg://jobhunter_app:pw@h:5432/jobhunter")

def test_migrate_dsn_uses_migrator(tmp_path: Path) -> None:
    cfg = load_config("migrate", BASE_ENV, write_secrets(tmp_path / "s", db_migrator=b"m p"))
    assert cfg.dsn("psycopg") == "postgresql://jobhunter_migrator:m%20p@h:5432/jobhunter"

@pytest.mark.parametrize(("role", "env_extra", "secrets", "expected"), [
    ("api", {}, {k: v for k, v in API_SECRETS.items() if k != "db_app"}, "secrets_present"),
    ("api", {}, {**API_SECRETS, "anthropic_key": b"sk-x"}, "foreign_secret_mounted"),
    ("api", {"JOBHUNTER_ENV": "staging"}, API_SECRETS, "env_valid"),
    ("api", {"LANGSMITH_GATEWAY": "1"}, API_SECRETS, "langsmith_gateway_set"),
    ("worker", {}, {**WORKER_SECRETS, "anthropic_key": b"lsv2_abc"}, "anthropic_key_is_langsmith"),
    ("worker", {"JOBHUNTER_ENV": "prod", "JOBHUNTER_FAKE_LLM": "1"}, WORKER_SECRETS, "fake_mode_in_prod"),
    ("worker", {"JOBHUNTER_ENV": "prod", "JOBHUNTER_FAULT": "x"}, WORKER_SECRETS, "fake_mode_in_prod"),
    ("worker", {"LANGSMITH_TRACING": "true", "LANGSMITH_HIDE_INPUTS": "true", "LANGSMITH_HIDE_OUTPUTS": "true",
                "LANGSMITH_ENDPOINT": "https://api.smith.langchain.com"}, WORKER_SECRETS, "langsmith_config"),
    ("api", {}, {**API_SECRETS, "session_secret": b64(31, b"s")}, "session_secret_length"),
    ("api", {}, {**API_SECRETS, "data_key": b64(33, b"d")}, "data_key_length"),
    ("api", {}, {**API_SECRETS, "data_key_previous": b64(16)}, "data_key_previous_length"),
    ("api", {}, {**API_SECRETS, "data_key": b64(32, b"s")}, "keys_identical"),
    ("worker", {"JOBHUNTER_ENV": "prod"}, WORKER_SECRETS, "healthchecks_in_prod"),
    ("api", {"JOBHUNTER_ENV": "prod", "ANTHROPIC_BASE_URL": "http://proxy"}, API_SECRETS, "anthropic_env_in_prod"),
])
def test_assertion_failures(tmp_path: Path, role: Role, env_extra: dict[str, str],
                            secrets: dict[str, bytes], expected: str) -> None:
    assert check_of(role, {**BASE_ENV, **env_extra}, write_secrets(tmp_path / "s", **secrets)) == expected

def test_world_readable_secret_fails(tmp_path: Path) -> None:
    d = write_secrets(tmp_path / "s", **API_SECRETS); (d / "db_app").chmod(0o444)
    assert check_of("api", BASE_ENV, d) == "secrets_not_world_readable"

def test_unreadable_secret_fails_loudly(tmp_path: Path) -> None:
    if os.geteuid() == 0: pytest.skip("root reads every file")
    d = write_secrets(tmp_path / "s", **API_SECRETS); (d / "db_app").chmod(0o000)
    assert check_of("api", BASE_ENV, d) == "secrets_readable"

def test_cli_exits_78_and_never_logs_the_value(tmp_path: Path) -> None:
    secret = b64(20, b"Z")                     # too short → session_secret_length
    d = write_secrets(tmp_path / "s", **{**API_SECRETS, "session_secret": secret})
    env = {**os.environ, **BASE_ENV, "JOBHUNTER_SECRETS_DIR": str(d)}
    r = subprocess.run([sys.executable, "-m", "jobhunter.cli", "startup-check", "--role", "api"],
                       env=env, capture_output=True, text=True)
    assert r.returncode == 78 and "session_secret_length" in r.stdout
    assert secret.decode() not in r.stdout + r.stderr
```

```python
# tests/unit/test_faults.py
import os, subprocess, sys
def run(env: dict[str, str]) -> int:
    code = "from jobhunter.faults import fault_point; fault_point('p.x'); print('alive')"
    return subprocess.run([sys.executable, "-c", code], env={**os.environ, **env}).returncode
def test_fault_exits_86_in_test_env() -> None:
    assert run({"JOBHUNTER_ENV": "test", "JOBHUNTER_FAULT": "p.x"}) == 86
def test_fault_ignored_outside_test_env() -> None:
    assert run({"JOBHUNTER_ENV": "dev", "JOBHUNTER_FAULT": "p.x"}) == 0
def test_other_point_ignored() -> None:
    assert run({"JOBHUNTER_ENV": "test", "JOBHUNTER_FAULT": "p.y"}) == 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_config.py tests/unit/test_faults.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'jobhunter.config'`

- [ ] **Step 3: Implement `config.py`, `faults.py` and the `startup-check` command**

`load_config`:
- The secrets directory is the `secrets_dir` argument, else `JOBHUNTER_SECRETS_DIR`, else `/run/secrets`. The same validation applies whichever directory is used.
- It records every file of that directory whose name is in `ALL_SECRETS`, with its mode. It reads the bytes, stripping one trailing newline. An unreadable file is recorded as unreadable, never skipped.
- `environ` keeps only names that start with `JOBHUNTER_`, `LANGSMITH_` or `ANTHROPIC_`.
- `JOBHUNTER_ENV ∉ {prod, dev, test}` raises `StartupCheckFailed("env_valid")`.

`ROLE_SECRETS`:

| Role | Required | Optional |
|---|---|---|
| `api` | `session_secret`, `data_key`, `db_app` | `data_key_previous` |
| `worker` | `data_key`, `db_app` | `data_key_previous`, `anthropic_key`, `rapidapi_key`, `serpapi_key`, `adzuna_app_id`, `adzuna_app_key`, `langsmith_key`, `ntfy_token`, `healthchecks_ping_url`, `healthchecks_api_key` |
| `migrate` | `db_migrator` | — |

`run_startup_assertions` checks run in this order, and the first failure raises with its name:
1. `secrets_present`, `secrets_readable`, `secrets_not_world_readable` (`mode & 0o004`), `foreign_secret_mounted` (a file in `ALL_SECRETS` that is neither required nor optional);
2. `langsmith_gateway_set`, `anthropic_key_is_langsmith` (prefix `lsv2_`);
3. `fake_mode_in_prod`: any of `JOBHUNTER_FAKE_SOURCES`, `JOBHUNTER_FAKE_LLM` or `JOBHUNTER_FAULT` set in prod;
4. `langsmith_config`: when `LANGSMITH_TRACING == "true"`, `HIDE_INPUTS` and `HIDE_OUTPUTS` must be `"true"` and `ENDPOINT` must equal `https://eu.api.smith.langchain.com`;
5. `session_secret_length` (base64 decodes to ≥ 32 bytes), `data_key_length` (exactly 32), `data_key_previous_length` (exactly 32 when present), `keys_identical`;
6. `healthchecks_in_prod`: a prod worker has both `healthchecks_ping_url` and `healthchecks_api_key`;
7. `anthropic_env_in_prod`: `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_URL` or `ANTHROPIC_API_KEY` set in prod.

`dsn`:
- The user is `jobhunter_app` for `api` and `worker`, and `jobhunter_migrator` for `migrate`.
- The password is `db_app` or `db_migrator`, quoted with `urllib.parse.quote(..., safe="")`.
- The prefix is `postgresql+psycopg://` or `postgresql://`.

`faults`:
- `JOBHUNTER_FAULT` has the form `<point>[:pause|:sleep=<s>]`.
- In the test env a match runs `os._exit(86)` by default, or `os.kill(os.getpid(), SIGSTOP)` for `pause`.
- `afault_point` additionally supports `sleep` via `await asyncio.sleep(s)`.

`cli.main` adds the argparse subparser `startup-check`, which calls `startup_or_exit(role)` and returns 0. Until Task 3, logging is plain JSON on stdout. `if __name__ == "__main__": raise SystemExit(main())`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_config.py tests/unit/test_faults.py -q`
Expected: PASS (22 passed; `test_unreadable_secret_fails_loudly` is skipped when running as root)

- [ ] **Step 5: Commit**

```bash
git add backend/src/jobhunter/config.py backend/src/jobhunter/faults.py backend/src/jobhunter/cli.py tests/unit
git commit -m "feat(m0): config loading, startup assertions (§16.8) and test-only fault hooks"
```

---

### Task 3: Structured logging with redaction, ids, clock and metrics

**Files:**
- Create: `backend/src/jobhunter/observability/__init__.py`, `observability/logging.py`, `observability/redact.py`, `observability/metrics.py`, `backend/src/jobhunter/ids.py`, `backend/src/jobhunter/clock.py`, `backend/src/jobhunter/types.py`
- Modify: `backend/src/jobhunter/config.py` (`startup_or_exit` logs through structlog)
- Test: `tests/unit/test_redact.py`, `tests/unit/test_ids.py`, `tests/unit/test_metrics.py`

**Interfaces:**
- Consumes: `config.startup_or_exit`.
- Produces:
  - `types.JSONValue` (`type JSONValue = None | bool | int | float | str | list[JSONValue] | dict[str, JSONValue]`)
  - `clock.utcnow() -> datetime` (aware, UTC)
  - `ids.uuid7() -> uuid.UUID`
  - `redact.REDACT_KEYS: Final[frozenset[str]]`, `redact.REDACT_PREFIXES: Final[tuple[str, ...]]`, `redact.mask_text(value: str) -> str`, `redact.redact_event(logger: object, method: str, event: MutableMapping[str, Any]) -> MutableMapping[str, Any]`
  - `logging.configure_logging(level: str = "INFO", *, stream: IO[str] | None = None) -> None`; `logging.get_logger(name: str) -> structlog.stdlib.BoundLogger`
  - `metrics.REGISTRY: CollectorRegistry`, with:
    - `RUNS` (`Counter("jobhunter_runs", …, ["status"])`);
    - `RUN_DURATION` (`Histogram("jobhunter_run_duration_seconds")`);
    - `HTTP_REQUESTS` (`Counter("jobhunter_http_requests", …, ["route", "status"])`);
    - `TABLE_BYTES` (`Gauge("jobhunter_table_bytes", …, ["table"])`);
    - `ALERTS_OPEN` (`Gauge("jobhunter_alerts_open", …, ["severity"])`).
  - `metrics.start_metrics_server(port: int) -> None`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_redact.py
import io, json
import structlog
from jobhunter.ids import uuid7
from jobhunter.observability.logging import configure_logging, get_logger
from jobhunter.observability.redact import mask_text, redact_event

def test_listed_keys_and_prefixes_are_redacted() -> None:
    ev = redact_event(None, "info", {"event": "x", "Authorization": "Bearer abc", "password": "p",
        "description_text": "ad", "profile_json": {}, "cv_file": "f", "raw_text_enc": "r",
        "nested": {"set-cookie": "c", "ok": 1}, "run_id": 7})
    assert ev["Authorization"] == ev["password"] == ev["description_text"] == "[REDACTED]"
    assert ev["profile_json"] == ev["cv_file"] == ev["raw_text_enc"] == "[REDACTED]"
    assert ev["nested"] == {"set-cookie": "[REDACTED]", "ok": 1} and ev["run_id"] == 7

def test_values_are_masked() -> None:
    out = mask_text("mail max.mustermann@example.org, call +49 170 1234567 or 0170 1234567, "
                    "key sk-ant-api03-AbCdEf0123456789xyzXYZ")
    assert "example.org" not in out and "1234567" not in out and "AbCdEf" not in out
    assert out.count("[EMAIL]") == 1 and out.count("[PHONE]") == 2 and out.count("[KEY]") == 1

def test_dates_and_uuids_survive() -> None:
    fixed = "01929345-0123-7123-8123-012345678901"            # digit-heavy uuid7 shape
    for rid in (str(uuid7()), fixed):
        assert mask_text(f"2026-10-16T06:00:00+00:00 {rid}") == f"2026-10-16T06:00:00+00:00 {rid}"

def test_json_output_has_spec_fields_and_redaction_runs_last() -> None:
    buf = io.StringIO(); configure_logging("INFO", stream=buf)
    structlog.contextvars.bind_contextvars(request_id="r1", token="t0ps3cret")
    get_logger("t").info("run_started", run_id=3, detail="write to max@example.org")
    structlog.contextvars.clear_contextvars()
    line = json.loads(buf.getvalue().strip().splitlines()[-1])
    assert {"ts", "level", "event", "logger", "request_id", "run_id"} <= line.keys()
    assert line["token"] == "[REDACTED]" and "max@example.org" not in buf.getvalue()
```

```python
# tests/unit/test_ids.py
import time
from jobhunter.ids import uuid7
def test_uuid7_layout_and_order() -> None:
    before = time.time_ns() // 1_000_000
    ids = [uuid7() for _ in range(1000)]
    after = time.time_ns() // 1_000_000
    for u in ids:
        assert u.version == 7 and (u.int >> 62) & 0b11 == 0b10
        assert before <= (u.int >> 80) <= after
    assert ids == sorted(ids) and len(set(ids)) == 1000
```

```python
# tests/unit/test_metrics.py
from prometheus_client import generate_latest
from jobhunter.observability import metrics
def test_series_names() -> None:
    metrics.RUNS.labels(status="succeeded").inc()
    metrics.HTTP_REQUESTS.labels(route="/healthz", status="200").inc()
    metrics.ALERTS_OPEN.labels(severity="warn").set(2)
    text = generate_latest(metrics.REGISTRY).decode()
    for name in ("jobhunter_runs_total", "jobhunter_run_duration_seconds", "jobhunter_http_requests_total",
                 "jobhunter_table_bytes", 'jobhunter_alerts_open{severity="warn"} 2.0'):
        assert name in text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_redact.py tests/unit/test_ids.py tests/unit/test_metrics.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'jobhunter.ids'`

- [ ] **Step 3: Implement the modules**

- **`REDACT_KEYS`** (matched case-insensitively): `authorization`, `cookie`, `set-cookie`, `x-rapidapi-key`, `api_key`, `app_key`, `app_id`, `password`, `token`, `secret`, `redirect_url`, `outbound_url_private`, `stripped_text`, `reviewed_text`.
- **`REDACT_PREFIXES`**: `description`, `profile`, `cv`, `raw_text`.
- **`redact_event`** recurses into dicts and lists and applies `mask_text` to every string value, except under the keys `ts` and `level`.
- **`mask_text`** leaves canonical UUID spans (`[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}`) untouched, and applies these patterns, in this order, to the rest:

```python
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE = re.compile(r"(?<![\w+\-])(?:\+\d{1,3}[ \d/()\-]{6,}\d|0\d{2,5}[ /\-]\d{3,}(?:[ \-]\d{2,})*)(?!\w)")
KEY   = re.compile(r"\b(?:sk-[A-Za-z0-9_\-]{20,}|lsv2_[A-Za-z0-9_]{20,}|[A-Za-z0-9+/_\-]{40,}={0,2})")
```

- **`configure_logging` processor chain**: `merge_contextvars`, `add_log_level`, `add_logger_name`, `TimeStamper(fmt="iso", utc=True, key="ts")`, `format_exc_info`, `redact_event`, `JSONRenderer()`. It passes `cache_logger_on_first_use=False`, so a later call (the `log_events` fixture of Task 14) also reaches loggers that modules created at import time.
- **`uuid7`** follows RFC 9562: 48-bit Unix milliseconds, version `0b0111`, a 12-bit counter, variant `0b10`, 62 random bits. Within one millisecond the counter increments, so ids stay strictly ordered; random bits are re-drawn when the counter overflows.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/src/jobhunter tests/unit
git commit -m "feat(m0): structlog JSON logging with last-stage redaction, uuid7, metrics registry"
```

---

### Task 4: PostgreSQL container harness, roles, server settings and image lock

**Files:**
- Create: `deploy/images.lock`, `deploy/postgres/init/10-init.sh`, `deploy/postgres/postgresql.conf`, `tests/support/pg.py`, `tests/integration/__init__.py`, `tests/integration/conftest.py`, `tests/integration/test_pg_init.py`
- Modify: `tests/support/fixtures.py`
- Test: `tests/integration/test_pg_init.py`

**Interfaces:**
- Consumes: `config.load_config`, `config.Role`.
- Produces:
  - `deploy/images.lock`: lines `<name>=<repo>:<tag>@sha256:<digest>`; Task 4 records `pgvector`, and Task 18 adds the others.
  - `pg.PgRole = Literal["superuser", "migrator", "app", "backup", "restore"]`; `pg.pg_image() -> str` (the `pgvector` line of `images.lock`)
  - `@dataclass(frozen=True) class PgServer`: `container: str`, `host: str`, `port: int`, `passwords: Mapping[str, str]`, `workdir: Path`; methods `dsn(role: PgRole, db: str = "jobhunter", *, driver: Literal["psycopg", "sqlalchemy"] = "psycopg") -> str`, `secrets_dir(role: Role) -> Path`, `logs() -> str`
  - `start_pg_server(workdir: Path) -> PgServer`; `remove_pg_server(server) -> None`; `pg_stop(server) -> None`; `pg_start(server) -> None`; `restart_pg_server(server) -> None`
  - `config_for(server: PgServer, role: Role, **env: str) -> AppConfig`
  - Fixtures: `pg_server` (session); `fresh_pg_server` (function); `make_config(role: Role, server: PgServer | None = None, **env: str) -> AppConfig`; `proc_env(role: Role, server: PgServer | None = None, **extra: str) -> dict[str, str]`

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_pg_init.py
import psycopg, pytest
from tests.support.pg import PgRole, PgServer
pytestmark = pytest.mark.integration

def q(server: PgServer, role: PgRole, sql: str, db: str = "jobhunter") -> list[tuple[object, ...]]:
    with psycopg.connect(server.dsn(role, db), autocommit=True) as c:
        cur = c.execute(sql); return cur.fetchall() if cur.description else []

def test_image_is_digest_pinned(pg_server: PgServer) -> None:
    from tests.support.pg import pg_image
    assert "@sha256:" in pg_image() and pg_image().startswith("pgvector/pgvector:0.8.7-pg17-bookworm@")

def test_roles(pg_server: PgServer) -> None:
    rows = dict(q(pg_server, "superuser", "SELECT rolname, rolsuper OR rolcreaterole FROM pg_roles "
                  "WHERE rolname LIKE 'jobhunter_%' AND rolname <> 'jobhunter_bootstrap'"))
    assert rows == {"jobhunter_migrator": False, "jobhunter_app": False, "jobhunter_backup": False, "jobhunter_restore": False}
    assert q(pg_server, "superuser", "SELECT rolcreatedb FROM pg_roles WHERE rolname='jobhunter_restore'") == [(True,)]
    assert q(pg_server, "superuser", "SELECT pg_has_role('jobhunter_backup','pg_read_all_data','member')") == [(True,)]
    assert q(pg_server, "superuser", "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname='jobhunter'") == [("jobhunter_migrator",)]

@pytest.mark.parametrize("db", ["jobhunter", "jobhunter_template"])
def test_extensions(pg_server: PgServer, db: str) -> None:
    ext = dict(q(pg_server, "superuser", "SELECT extname, extversion FROM pg_extension", db))
    assert ext["vector"] == "0.8.7" and "pg_trgm" in ext and "unaccent" in ext
    assert q(pg_server, "superuser", "SELECT datistemplate FROM pg_database WHERE datname='jobhunter_template'") == [(True,)]

def test_default_privileges_cover_later_tables(pg_server: PgServer) -> None:
    q(pg_server, "migrator", "CREATE TABLE probe_dp (id int PRIMARY KEY, v text)")
    try:
        q(pg_server, "app", "INSERT INTO probe_dp VALUES (1,'a')"); q(pg_server, "app", "UPDATE probe_dp SET v='b'")
        assert q(pg_server, "app", "SELECT v FROM probe_dp") == [("b",)]
        q(pg_server, "app", "DELETE FROM probe_dp")
        with pytest.raises(psycopg.Error) as denied:
            q(pg_server, "app", "CREATE TABLE app_ddl (x int)")
        assert denied.value.sqlstate == "42501"                                # insufficient_privilege
    finally:
        q(pg_server, "migrator", "DROP TABLE probe_dp")

def test_restore_role_scratch_db_and_no_prod_read(pg_server: PgServer) -> None:
    q(pg_server, "restore", "CREATE DATABASE jh_scratch TEMPLATE jobhunter_template", db="postgres")
    q(pg_server, "restore", "DROP DATABASE jh_scratch", db="postgres")
    q(pg_server, "migrator", "CREATE TABLE probe_ro (x int)")
    try:
        with pytest.raises(psycopg.Error) as denied:
            q(pg_server, "restore", "SELECT * FROM probe_ro")
        assert denied.value.sqlstate == "42501"
    finally:
        q(pg_server, "migrator", "DROP TABLE probe_ro")

@pytest.mark.parametrize(("name", "value"), [
    ("log_statement", "none"), ("log_min_duration_statement", "-1"), ("log_parameter_max_length", "0"),
    ("log_parameter_max_length_on_error", "0"), ("log_min_error_statement", "panic"),
    ("log_error_verbosity", "terse"), ("idle_in_transaction_session_timeout", "1min"),
])
def test_server_settings(pg_server: PgServer, name: str, value: str) -> None:
    assert q(pg_server, "superuser", f"SHOW {name}") == [(value,)]

def test_l1_postgres_logs_contain_no_values(pg_server: PgServer) -> None:
    email, phone = "max.mustermann@example.org", "+49 170 1234567"
    q(pg_server, "migrator", "CREATE TABLE l1_probe (email text UNIQUE, phone text)")
    try:
        with psycopg.connect(pg_server.dsn("app")) as c:
            c.execute("INSERT INTO l1_probe VALUES (%s, %s)", (email, phone)); c.commit()
            c.execute("SELECT pg_sleep(0.3), %s::text", (phone,)); c.commit()
            with pytest.raises(psycopg.Error) as dup:
                c.execute("INSERT INTO l1_probe VALUES (%s, %s)", (email, phone))
            assert dup.value.sqlstate == "23505"
        logs = pg_server.logs()
        assert "duplicate key" in logs and email not in logs and "170 1234567" not in logs
    finally:
        q(pg_server, "migrator", "DROP TABLE l1_probe")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `dockerd >/tmp/dockerd.log 2>&1 & sleep 3; uv run pytest tests/integration/test_pg_init.py -q`
Expected: FAIL with `fixture 'pg_server' not found`

- [ ] **Step 3: Implement the image lock, init script, server config and harness**

**`deploy/images.lock`**: `docker pull pgvector/pgvector:0.8.7-pg17-bookworm`, then `docker inspect --format '{{index .RepoDigests 0}}'`, and record `pgvector=pgvector/pgvector:0.8.7-pg17-bookworm@sha256:<digest>`.

**`deploy/postgres/postgresql.conf`**:
- the values of §19.1 verbatim (`shared_buffers=2GB`, `effective_cache_size=6GB`, `work_mem=32MB`, `maintenance_work_mem=512MB`, `max_connections=60`, `idle_in_transaction_session_timeout=60s`, `tcp_keepalives_idle=30`, `tcp_keepalives_interval=10`, `tcp_keepalives_count=3`, and the six logging settings);
- plus `listen_addresses='*'` and `logging_collector=off`.

**`deploy/postgres/init/10-init.sh`** (bash, `set -euo pipefail`):
- It runs one `psql -v ON_ERROR_STOP=1 -v db="$POSTGRES_DB" -v app_pw="$(cat /run/secrets/db_app)" -v mig_pw=… -v bak_pw=… -v rst_pw=… --username "$POSTGRES_USER" --dbname "$POSTGRES_DB"` with a quoted heredoc (`<<'EOSQL'`). The database name and every password reach SQL only as psql variables (`:"db"`, `:'app_pw'`).
- Statements, in order:
  1. `CREATE ROLE jobhunter_migrator LOGIN PASSWORD :'mig_pw'`, the same for `jobhunter_app` and `jobhunter_backup`, and `CREATE ROLE jobhunter_restore LOGIN CREATEDB PASSWORD :'rst_pw'`;
  2. `ALTER DATABASE :"db" OWNER TO jobhunter_migrator`;
  3. the three `CREATE EXTENSION` statements;
  4. `CREATE DATABASE jobhunter_template`, then `\connect jobhunter_template` and the same three extensions, then `ALTER DATABASE jobhunter_template IS_TEMPLATE true`, then `\connect :"db"`;
  5. the two `ALTER DEFAULT PRIVILEGES FOR ROLE jobhunter_migrator …` statements of §19.3;
  6. `GRANT pg_read_all_data TO jobhunter_backup`;
  7. `REVOKE CREATE ON SCHEMA public FROM PUBLIC`.

**`tests/support/pg.py`**:
- `start_pg_server` writes random hex passwords (`pg_bootstrap`, `db_app`, `db_migrator`, `db_backup`, `db_restore`) into `workdir/pgsecrets` with mode 0444. This harness mount is readable by the container's uid 999; the real UID/GID arrangement is tested in Task 18.
- It writes the per-role app secret directories `workdir/secrets/{api,worker,migrate}` (files mode 0440), holding the role sets of `ROLE_SECRETS` (`session_secret` and `data_key` are base64 of 32 random bytes, shared by the roles that receive them).
- It picks a free host port by binding `127.0.0.1:0`, then runs:

```
docker run -d --name jh-test-pg-<hex> -p 127.0.0.1:<port>:5432 \
  -e POSTGRES_USER=jobhunter_bootstrap -e POSTGRES_DB=jobhunter -e POSTGRES_PASSWORD_FILE=/run/secrets/pg_bootstrap \
  -v <pgsecrets>:/run/secrets:ro -v <repo>/deploy/postgres/init:/docker-entrypoint-initdb.d:ro \
  -v <repo>/deploy/postgres/postgresql.conf:/etc/postgresql/postgresql.conf:ro \
  <pg_image()> postgres -c config_file=/etc/postgresql/postgresql.conf \
  -c shared_buffers=128MB -c effective_cache_size=512MB -c max_connections=150
```

- It waits until a `psycopg.connect` as `jobhunter_app` succeeds **and** `docker logs` shows `database system is ready to accept connections` twice. The timeout is 60 s.
- `pg_stop` runs `docker stop`, `pg_start` runs `docker start`, and `restart_pg_server` runs `docker restart`; the last two wait until the app role connects again.
- If `docker info` fails, `pytest.fail("start dockerd first: dockerd &")`.

**`tests/support/fixtures.py`**:
- `pg_server` (session; teardown `remove_pg_server`), `fresh_pg_server` (function), `make_config` (returns `config_for(server or pg_server, role, **env)`), and `proc_env`.
- `config_for` sets `JOBHUNTER_ENV=test`, the server's host and port, `JOBHUNTER_PUBLIC_ORIGIN=https://testserver` and `JOBHUNTER_SECRETS_DIR=server.secrets_dir(role)`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/integration/test_pg_init.py -q`
Expected: PASS (14 passed)

- [ ] **Step 5: Commit**

```bash
git add deploy/images.lock deploy/postgres tests/support tests/integration
git commit -m "feat(m0): digest-pinned postgres harness, init (roles, extensions, template, default privileges), log settings"
```

---

### Task 5: Baseline schema migration

**Files:**
- Create: `backend/src/jobhunter/db/__init__.py`, `db/migrations/env.py`, `db/migrations/script.py.mako`, `db/migrations/versions/0001_m0_baseline.py`, `backend/src/jobhunter/db/alembic_runner.py`
- Modify: `tests/support/fixtures.py` (fixture `migrated`), `tests/support/pg.py` (`ensure_config_rows`)
- Test: `tests/integration/test_schema.py`

**Interfaces:**
- Consumes: `PgServer`, `config_for`.
- Produces:
  - `alembic_runner.alembic_config(dsn: str) -> alembic.config.Config`; `alembic_runner.upgrade_head(dsn: str) -> None` (sync; Task 6 calls it through `asyncio.to_thread`); `alembic_runner.code_head() -> str`
  - Fixture `migrated` (session): `pg_server` after `upgrade_head(server.dsn("migrator", driver="sqlalchemy"))`. Task 6 replaces this with `run_migrate(config_for(server, "migrate"))`.
  - `pg.ensure_config_rows(conn: psycopg.Connection) -> tuple[int, int]`: when `settings_versions` is empty, it inserts a minimal scope, model, execution, evaluation and settings chain (random hashes, payload `{}`). It returns `(settings_version_id, scope_version_id)` of the lowest settings version.

The migration `0001_m0_baseline` contains this DDL **verbatim from the spec** (`op.execute` of static SQL):
- §7.1: the extension statements, `immutable_unaccent`, `german_unaccent`, `ranking_state`;
- §7.2: `users`, `sessions`, `login_attempts`, `login_lockouts`, `api_idempotency`;
- §7.3: `scope_versions`, `model_policies`, `execution_configs`, `evaluation_policies`, `rubric_versions`, `prompt_schema_versions`, `settings_versions`, `active_config`, `schedule_state`, `operations` (with both indexes);
- §7.6: `runs` (all four indexes), `run_attempts`, `worker_heartbeat`;
- §7.14: `alerts` (with `alerts_open_dedup`), `purge_fences` (with both indexes).

These are the only differences from the spec text. Each carries a SQL comment naming its owner milestone (see Deviations 1–2):
- `active_config.rubric_version_id` and `active_config.prompt_schema_version_id` are **nullable** until M2 seeds those rows and restores `NOT NULL`;
- the FKs `active_config.profile_version_id → profile_versions`, `active_config.llm_tuple_id → llm_tuples` and `runs.llm_tuple_id → llm_tuples` are added in M2;
- `purge_fences.source_id → sources` is added in M1, and `purge_fences.manifest_id → deletion_manifests` in M5;
- `is_fenced(p_job bigint, p_tuple bigint DEFAULT NULL)` has the `global` and `jobs` branches only; M1 adds `source` and M2 adds `profile` with `CREATE OR REPLACE`, ending at the §7.14 body.

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_schema.py
import psycopg, pytest
from tests.support.pg import PgRole, PgServer, ensure_config_rows
pytestmark = pytest.mark.integration

M0_TABLES = {"ranking_state", "users", "sessions", "login_attempts", "login_lockouts", "api_idempotency",
    "scope_versions", "model_policies", "execution_configs", "evaluation_policies", "rubric_versions",
    "prompt_schema_versions", "settings_versions", "active_config", "schedule_state", "operations", "runs",
    "run_attempts", "worker_heartbeat", "alerts", "purge_fences", "alembic_version"}
LATER = {"llm_tuples", "profile_versions", "score_cycles", "sources", "postings", "deletion_manifests"}

def rows(server: PgServer, statement: str, role: PgRole = "migrator") -> list[tuple[object, ...]]:
    with psycopg.connect(server.dsn(role)) as c:
        cur = c.execute(statement); out = cur.fetchall() if cur.description else []
        c.rollback(); return out

def test_tables(migrated: PgServer) -> None:
    tables = {r[0] for r in rows(migrated, "SELECT tablename FROM pg_tables WHERE schemaname='public'")}
    assert M0_TABLES <= tables and not (tables & LATER)

def test_active_config_m2_columns_nullable(migrated: PgServer) -> None:
    assert rows(migrated, "SELECT column_name, is_nullable FROM information_schema.columns WHERE table_name='active_config' "
                "AND column_name IN ('rubric_version_id','prompt_schema_version_id','settings_version_id') ORDER BY 1") == [
        ("prompt_schema_version_id", "YES"), ("rubric_version_id", "YES"), ("settings_version_id", "NO")]

def test_text_search_and_unaccent(migrated: PgServer) -> None:
    assert rows(migrated, "SELECT immutable_unaccent('Ärger Straße')") == [("Arger Strasse",)]
    assert rows(migrated, "SELECT to_tsvector('german_unaccent','Müller') = to_tsvector('german_unaccent','Muller')") == [(True,)]

RUN = ("INSERT INTO runs (trigger,scheduled_slot,status,include_paid,settings_version_id,scope_version_id{extra_cols}) "
       "VALUES ({trigger},{slot},'{status}',true,<sv>,<sc>{extra_vals})")
def run_sql(status: str, trigger: str = "'schedule'", slot: str = "'2026-10-16T06:00Z'",
            extra_cols: str = "", extra_vals: str = "") -> str:
    return RUN.format(trigger=trigger, slot=slot, status=status, extra_cols=extra_cols, extra_vals=extra_vals)

@pytest.mark.parametrize(("statements", "sqlstate"), [
    ([run_sql("queued"), run_sql("queued", slot="'2026-10-16T12:00Z'")], "23505"),     # runs_one_active_collect
    ([run_sql("missed"), run_sql("missed")], "23505"),                                   # runs_slot_uq
    ([run_sql("queued", trigger="'manual'")], "23514"),
    ([run_sql("missed", extra_cols=",attempt_no", extra_vals=",1")], "23514"),
    ([run_sql("running", extra_cols=",attempt_no,started_at", extra_vals=",1,now()")], "23514"),
    (["INSERT INTO operations (kind,payload,dedup_key,status) VALUES ('export','{}','e','running')"], "23514"),
    (["INSERT INTO operations (kind,payload,dedup_key,status) VALUES ('export','{}','e','pending')"] * 2, "23505"),
    (["INSERT INTO operations (kind,payload,dedup_key,status,claim_id,worker_epoch,heartbeat_at,finished_at) "
      "VALUES ('export','{}','e','succeeded',gen_random_uuid(),1,now(),now())"], "23514"),   # full ownership, finished row
    (["INSERT INTO operations (kind,payload,dedup_key,status,claim_id,worker_epoch,heartbeat_at) "
      "VALUES ('export','{}','e','pending',gen_random_uuid(),1,now())"], "23514"),           # full ownership, pending row
    (["INSERT INTO api_idempotency (route,idem_key,request_sha256,status,expires_at) VALUES ('r','short','\\x00','in_progress',now())"], "23514"),
    (["INSERT INTO users (id,username,password_hash,password_changed_at) VALUES (2,'abc','h',now())"], "23514"),
    (["INSERT INTO login_lockouts VALUES ('user:x', now(), 'r')"], "23514"),
    (["INSERT INTO purge_fences (kind,manifest_id) VALUES ('source',1)"], "23514"),
    (["INSERT INTO purge_fences (kind,manifest_id) VALUES ('jobs',1)"], "23514"),
    (["INSERT INTO schedule_state VALUES (1, now(), 3599, true, NULL, <sv>, now()) ON CONFLICT (id) DO UPDATE SET interval_s = 3599"], "23514"),
    (["INSERT INTO alerts (code,severity,dedup_key,message) VALUES ('a','warn','k','m')"] * 2, "23505"),
])
def test_constraints_reject(migrated: PgServer, statements: list[str], sqlstate: str) -> None:
    with psycopg.connect(migrated.dsn("migrator")) as c:
        sv, sc = ensure_config_rows(c)
        with pytest.raises(psycopg.Error) as exc:
            for s in statements: c.execute(s.replace("<sv>", str(sv)).replace("<sc>", str(sc)))
        assert exc.value.sqlstate == sqlstate
        c.rollback()

def test_is_fenced_global_and_jobs(migrated: PgServer) -> None:
    with psycopg.connect(migrated.dsn("migrator")) as c:
        assert c.execute("SELECT is_fenced(5)").fetchone() == (False,)
        c.execute("INSERT INTO purge_fences (kind,job_ids,manifest_id) VALUES ('jobs','{5}',1)")
        assert c.execute("SELECT is_fenced(5), is_fenced(6)").fetchone() == (True, False)
        c.execute("UPDATE purge_fences SET released_at = now()")
        assert c.execute("SELECT is_fenced(5)").fetchone() == (False,)
        c.execute("INSERT INTO purge_fences (kind,manifest_id) VALUES ('global',2)")
        assert c.execute("SELECT is_fenced(6, 9)").fetchone() == (True,)
        c.rollback()

def test_code_head_matches_db(migrated: PgServer) -> None:
    from jobhunter.db.alembic_runner import code_head
    assert rows(migrated, "SELECT version_num FROM alembic_version") == [(code_head(),)]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/integration/test_schema.py -q`
Expected: FAIL with `fixture 'migrated' not found`

- [ ] **Step 3: Implement the Alembic environment and `0001_m0_baseline`**

- `env.py` uses `sqlalchemy.create_engine(url, poolclass=NullPool)` in online mode with `transaction_per_migration=True`.
- `alembic_config` builds the `Config` in code, with `script_location` set to the package directory (`Path(__file__).parent / "migrations"`). There is no `alembic.ini` and no repository-relative path, so the installed non-editable wheel finds its migrations (Task 18 checks this in the final image).
- The two ownership cases of `test_constraints_reject` set `claim_id`, `worker_epoch` and `heartbeat_at` together. The spec's CHECK compares `status = 'running'` with *all three* being set, so a non-running row with only some of them set passes it. Clearing every field on exit is `transition()`'s job, and Task 7 tests that. The DDL stays exactly as in the spec.
- Revision id `0001_m0_baseline`. `downgrade()` raises `NotImplementedError("expand/contract: no downgrades")`.
- The DDL is copied from the spec text; the file header names the source sections and lists every difference above.
- The M0 `is_fenced` body:

```sql
CREATE FUNCTION is_fenced(p_job bigint, p_tuple bigint DEFAULT NULL) RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT EXISTS (SELECT 1 FROM purge_fences f WHERE f.released_at IS NULL
                   AND (f.kind = 'global' OR (f.kind = 'jobs' AND p_job = ANY (f.job_ids))));
$$;  -- M1 adds the 'source' branch, M2 the 'profile' branch (CREATE OR REPLACE to the §7.14 text)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/integration/test_schema.py -q`
Expected: PASS (21 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/src/jobhunter/db tests
git commit -m "feat(m0): alembic baseline with the M0 tables from §7, including active_config"
```

---

### Task 6: Async `migrate`: step order, grants, queue and checkpointer setup, complete default seeds

**Files:**
- Create: `backend/src/jobhunter/db/session.py`, `backend/src/jobhunter/db/sql.py`, `backend/src/jobhunter/db/migrate.py`, `backend/src/jobhunter/db/seed.py`, `backend/src/jobhunter/db/seed_data/settings_v1.json`, `backend/src/jobhunter/graphs/__init__.py`, `backend/src/jobhunter/graphs/checkpointer.py`
- Modify: `backend/src/jobhunter/cli.py` (`migrate`), `backend/src/jobhunter/types.py` (typed JSON accessors), `tests/support/fixtures.py` (`migrated` calls `run_migrate`)
- Test: `tests/unit/test_seed_math.py`, `tests/unit/test_json_access.py`, `tests/integration/test_migrate.py`, `tests/integration/test_sql_errors.py`

**Interfaces:**
- Consumes: `AppConfig`, `alembic_runner.upgrade_head`.
- Produces:
  - `session.make_engine(cfg: AppConfig, *, pool_size: int = 10) -> AsyncEngine` (`create_async_engine(cfg.dsn("sqlalchemy"), pool_pre_ping=True, pool_size=pool_size, max_overflow=0)`); `session.make_null_engine(dsn: str) -> AsyncEngine` (`poolclass=NullPool`)
  - `sql.q(statement: str, *, jsonb: Iterable[str] = ()) -> TextClause` (binds the named parameters with `sqlalchemy.dialects.postgresql.JSONB`)
  - `sql.UNIQUE_VIOLATION: Final = "23505"`, `sql.CHECK_VIOLATION: Final = "23514"`, `sql.UNDEFINED_TABLE: Final = "42P01"`, `sql.LOCK_NOT_AVAILABLE: Final = "55P03"`
  - `@dataclass(frozen=True) class PgErrorInfo`: `sqlstate: str | None`, `constraint: str | None`; `sql.pg_error(exc: sqlalchemy.exc.DBAPIError) -> PgErrorInfo`; `sql.is_unique_violation(exc: sqlalchemy.exc.DBAPIError, constraint: str) -> bool`
  - `types.json_get(doc: JSONValue, path: str) -> JSONValue` (a dotted path through objects), plus `types.get_object`, `get_list`, `get_str`, `get_bool` and `get_int` with the same parameters, returning `dict[str, JSONValue]`, `list[JSONValue]`, `str`, `bool` and `int` (`get_int` rejects `bool`). A missing key raises `KeyError(path)`. A value of another kind raises `TypeError(f"{path}: expected <kind>, got <type name>")`. With these, no code indexes into the `JSONValue` union, which `mypy --strict` rejects.
  - `MigrateStep = Literal["reconstruct_and_advance", "alembic", "advance_again", "grants", "queue_and_checkpointer", "seed", "replay"]`; `MIGRATE_ORDER: Final[tuple[MigrateStep, ...]]`
  - `async def run_migrate_async(cfg: AppConfig, *, now: datetime | None = None) -> list[MigrateStep]`; `run_migrate(cfg: AppConfig, *, now: datetime | None = None) -> list[MigrateStep]`, a sync wrapper (`asyncio.run`) that raises `RuntimeError` when called inside a running loop, used only by the CLI and by sync session fixtures
  - `BACKUP_GRANT_TABLES: Final = ("backup_sets", "backup_runs", "manifest_shipments", "manifest_shipment_items")`; `async def apply_explicit_grants(conn: AsyncConnection) -> list[str]`
  - `seed.canonical_json(value: JSONValue) -> bytes`; `seed.content_hash(value: JSONValue) -> bytes`; `seed.load_settings_v1() -> dict[str, JSONValue]`; `seed.scope_payload(settings: Mapping[str, JSONValue]) -> dict[str, JSONValue]` (the `scope` object without `max_ad_age_days`)
  - `seed.local_anchor_to_utc(day: date, hhmm: str, tz: str) -> datetime`; `seed.latest_slot_at_or_before(anchor: datetime, interval_s: int, t: datetime) -> datetime`
  - `async def seed.seed_defaults(conn: AsyncConnection, *, now: datetime) -> None` (idempotent)
  - `async def checkpointer.setup_checkpointer(dsn: str) -> None`; `checkpointer.open_saver(dsn: str) -> AbstractAsyncContextManager[AsyncPostgresSaver]` (pool `min_size=1, max_size=4`, connection kwargs `autocommit=True, prepare_threshold=0, row_factory=dict_row`)
  - CLI `jobhunter migrate`

**`settings_v1.json`** holds every §13.1 default (decimals as strings):

```json
{"schedule": {"enabled": true, "interval_hours": 6, "anchor_local": "06:00", "include_paid": true},
 "display": {"timezone": "Europe/Berlin", "ui_language": "en"},
 "scope": {"locations": [], "remote_within_germany": true, "work_modes": ["remote", "hybrid", "onsite"],
   "employment_types": ["full_time", "part_time", "contract", "temporary", "internship", "working_student", "apprenticeship", "freelance"],
   "seniority_range": {"mode": "profile_relative", "below": 1, "above": 1, "min": null, "max": null},
   "industries_include": [], "industries_exclude": [], "accepted_ad_languages": ["de", "en"],
   "language_policy_default": "hard", "language_policy": {}, "excluded_companies": [], "excluded_keywords": [],
   "exclude_keywords_in_description": false, "min_salary_eur_year": null,
   "knockout_severity": {"degree": "soft", "certification": "soft", "security_clearance": "hard", "work_permit": "hard",
                         "onsite_required": "hard", "contract_type": "hard"},
   "max_ad_age_days": 30},
 "evaluation": {"relevance_threshold": 60, "t2_band": 5, "t2_straddle_max_coverage": "0.7", "t3_min_mid": 80,
                "t8_sample_pct": 5, "critic_light_triggers": ["T2", "T8"]},
 "sources": {"enabled": {"jsearch": true, "serpapi": true, "adzuna": true, "arbeitnow": true, "ats": true}},
 "queries": [], "watchlist": [], "collection": {"stale_ttl_days": null},
 "quota": {"jsearch_monthly_limit": 200, "serpapi_monthly_limit": 250, "reserve_pct": 10, "serpapi_hourly_cap": 45,
           "adzuna_cap_minute": 20, "adzuna_cap_day": 225, "adzuna_cap_week": 900, "adzuna_cap_month": 2250,
           "paid_max_requests_per_run": "auto"},
 "scoring": {"model_policy": {"preset": "B", "nodes": {
      "generate": {"model": "claude-sonnet-5-5", "effort": "medium"}, "revise": {"model": "claude-sonnet-5-5", "effort": "medium"},
      "critic": {"model": "claude-opus-5-5", "effort": "medium"}, "extract_profile": {"model": "claude-opus-5-5", "effort": "medium"},
      "propose_queries": {"model": "claude-opus-5-5", "effort": "medium"}}, "critic_light_effort": "low"},
   "execution": {"generate": {"max_tokens": 6000, "max_tokens_retry": 12000, "deadline_s": 600},
      "revise": {"max_tokens": 6000, "max_tokens_retry": 12000, "deadline_s": 600},
      "critic": {"max_tokens": 6000, "max_tokens_retry": 12000, "deadline_s": 600},
      "extract_profile": {"max_tokens": 16000, "max_tokens_retry": 32000, "deadline_s": 900},
      "propose_queries": {"max_tokens": 8000, "max_tokens_retry": 16000, "deadline_s": 300}},
   "max_targets_per_cycle": 60, "min_cycle_targets": 10, "batch_bulk_rescore": true, "batch_min_targets": 200,
   "batch_max_items": 1000, "bulk_rescore_max_budget_share": "0.4"},
 "llm": {"cap_usd": "100.00", "nonscoring_reserve_pct": 5, "overrun_buffer_usd": "2.00", "min_bucket_capacity_usd": "1.00"},
 "retention": {"score_history_months": 12}, "notifications": {"push_min_severity": "warn"}}
```

`seed_defaults` writes these rows, all idempotent (`ON CONFLICT DO NOTHING` on the hash, `version_no` or `id`; `active_config` and `schedule_state` only when absent):
- `model_policies(settings.scoring.model_policy)`;
- `execution_configs(settings.scoring.execution)`;
- `evaluation_policies(version_no=1, settings.evaluation, projected_critic_rate=0.450)`;
- `scope_versions(scope_payload(settings))`;
- `settings_versions(version_no=1, schema_version=1, payload=settings, effect_classes='{none}')`;
- `active_config(1, settings_version_id, scope_version_id, model_policy_id, execution_config_id, evaluation_policy_id)`, with the profile, tuple, rubric and prompt-schema pointers NULL;
- `schedule_state(1, anchor_utc = local_anchor_to_utc(<now's date in Europe/Berlin>, "06:00", "Europe/Berlin"), interval_s=21600, enabled=true, last_evaluated_slot = latest_slot_at_or_before(anchor_utc, 21600, now), settings_version_id)`;
- `ranking_state(1, 0)`.

Every hash is `content_hash` of the payload as stored, and every payload is bound through `q(..., jsonb=["payload"])`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_seed_math.py
from datetime import UTC, date, datetime
from jobhunter.db.seed import canonical_json, latest_slot_at_or_before, load_settings_v1, local_anchor_to_utc, scope_payload
from jobhunter.types import get_bool, get_str
SPEC_FIELDS = ["schedule.enabled", "schedule.interval_hours", "schedule.anchor_local", "schedule.include_paid",
    "display.timezone", "display.ui_language", "scope.locations", "scope.remote_within_germany", "scope.work_modes",
    "scope.employment_types", "scope.seniority_range", "scope.industries_include", "scope.industries_exclude",
    "scope.accepted_ad_languages", "scope.language_policy_default", "scope.language_policy", "scope.excluded_companies",
    "scope.excluded_keywords", "scope.exclude_keywords_in_description", "scope.min_salary_eur_year",
    "scope.knockout_severity", "scope.max_ad_age_days", "evaluation.relevance_threshold", "evaluation.t2_band",
    "evaluation.t2_straddle_max_coverage", "evaluation.t3_min_mid", "evaluation.t8_sample_pct",
    "evaluation.critic_light_triggers", "sources.enabled", "queries", "watchlist", "collection.stale_ttl_days",
    "quota.jsearch_monthly_limit", "quota.serpapi_monthly_limit", "quota.reserve_pct", "quota.serpapi_hourly_cap",
    "quota.adzuna_cap_minute", "quota.adzuna_cap_day", "quota.adzuna_cap_week", "quota.adzuna_cap_month",
    "quota.paid_max_requests_per_run", "scoring.model_policy", "scoring.execution", "llm.cap_usd",
    "llm.nonscoring_reserve_pct", "llm.overrun_buffer_usd", "llm.min_bucket_capacity_usd", "scoring.max_targets_per_cycle",
    "scoring.min_cycle_targets", "scoring.batch_bulk_rescore", "scoring.batch_min_targets", "scoring.batch_max_items",
    "scoring.bulk_rescore_max_budget_share", "retention.score_history_months", "notifications.push_min_severity"]

def test_settings_v1_has_every_spec_field() -> None:
    s = load_settings_v1()
    for path in SPEC_FIELDS:
        node: object = s
        for part in path.split("."):
            assert isinstance(node, dict) and part in node, path
            node = node[part]
    assert get_bool(s, "scope.remote_within_germany") is True and get_str(s, "llm.cap_usd") == "100.00"
    assert "max_ad_age_days" not in scope_payload(s) and scope_payload(s)["work_modes"] == ["remote", "hybrid", "onsite"]

def test_anchor_summer_and_winter() -> None:
    assert local_anchor_to_utc(date(2026, 10, 16), "06:00", "Europe/Berlin") == datetime(2026, 10, 16, 4, 0, tzinfo=UTC)
    assert local_anchor_to_utc(date(2026, 12, 1), "06:00", "Europe/Berlin") == datetime(2026, 12, 1, 5, 0, tzinfo=UTC)

def test_latest_slot() -> None:
    a = datetime(2026, 10, 16, 4, 0, tzinfo=UTC)
    assert latest_slot_at_or_before(a, 21600, datetime(2026, 10, 16, 10, 0, tzinfo=UTC)) == datetime(2026, 10, 16, 10, 0, tzinfo=UTC)
    assert latest_slot_at_or_before(a, 21600, datetime(2026, 10, 16, 9, 59, tzinfo=UTC)) == a
    assert latest_slot_at_or_before(datetime(2026, 12, 1, 5, 0, tzinfo=UTC), 21600,
                                    datetime(2026, 12, 1, 3, 0, tzinfo=UTC)) == datetime(2026, 11, 30, 23, 0, tzinfo=UTC)

def test_canonical_json() -> None:
    assert canonical_json({"b": 1, "a": "ü"}) == '{"a":"ü","b":1}'.encode()
```

```python
# tests/unit/test_json_access.py
from collections.abc import Callable
import pytest
from jobhunter.types import JSONValue, get_bool, get_int, get_list, get_object, get_str, json_get
DOC: JSONValue = {"a": {"flag": True, "n": 3, "s": "x", "l": [1, "y"], "o": {}}}

def test_accessors_narrow() -> None:
    assert get_bool(DOC, "a.flag") is True and get_int(DOC, "a.n") == 3 and get_str(DOC, "a.s") == "x"
    assert get_list(DOC, "a.l") == [1, "y"] and get_object(DOC, "a.o") == {} and json_get(DOC, "a.n") == 3

@pytest.mark.parametrize(("call", "error", "text"), [
    (lambda: get_int(DOC, "a.flag"), TypeError, "a.flag: expected int, got bool"),
    (lambda: get_object(DOC, "a.s.x"), TypeError, "a.s: expected object, got str"),
    (lambda: get_str(DOC, "a.missing"), KeyError, "a.missing"),
])
def test_accessors_reject(call: Callable[[], object], error: type[Exception], text: str) -> None:
    with pytest.raises(error, match=text): call()
```

```python
# tests/integration/test_migrate.py
from datetime import UTC, datetime
import psycopg, pytest
from jobhunter.db.alembic_runner import code_head
from jobhunter.db.migrate import MIGRATE_ORDER, apply_explicit_grants, run_migrate, run_migrate_async
from jobhunter.db.seed import load_settings_v1
from jobhunter.graphs.checkpointer import setup_checkpointer
from tests.support.pg import PgRole, PgServer
pytestmark = pytest.mark.integration
NOW = datetime(2026, 10, 16, 10, 0, tzinfo=UTC)

def one(server: PgServer, statement: str, role: PgRole = "migrator") -> tuple[object, ...] | None:
    with psycopg.connect(server.dsn(role)) as c: return c.execute(statement).fetchone()

async def test_first_install_order_and_artifacts(fresh_pg_server, make_config) -> None:
    assert await run_migrate_async(make_config("migrate", fresh_pg_server), now=NOW) == list(MIGRATE_ORDER)
    assert list(MIGRATE_ORDER) == ["reconstruct_and_advance", "alembic", "advance_again", "grants",
                                   "queue_and_checkpointer", "seed", "replay"]
    assert one(fresh_pg_server, "SELECT version_num FROM alembic_version") == (code_head(),)
    for t in ("procrastinate_jobs", "checkpoints", "checkpoint_writes", "checkpoint_blobs"):
        assert one(fresh_pg_server, f"SELECT to_regclass('{t}') IS NOT NULL") == (True,)
    assert one(fresh_pg_server, "SELECT count(*) FROM procrastinate_jobs", role="app") == (0,)

def test_sync_wrapper_refuses_a_running_loop(make_config) -> None:
    import asyncio
    async def inner() -> None:
        with pytest.raises(RuntimeError): run_migrate(make_config("migrate"))
    asyncio.run(inner())

async def test_seed_values(fresh_pg_server, make_config) -> None:
    await run_migrate_async(make_config("migrate", fresh_pg_server), now=NOW)
    s = load_settings_v1()
    assert one(fresh_pg_server, "SELECT payload FROM settings_versions WHERE version_no = 1") == (s,)
    assert one(fresh_pg_server, "SELECT schema_version, effect_classes FROM settings_versions") == (1, ["none"])
    assert one(fresh_pg_server, "SELECT payload->'nodes'->'critic'->>'model' FROM model_policies") == ("claude-opus-5-5",)
    assert one(fresh_pg_server, "SELECT version_no, projected_critic_rate::text FROM evaluation_policies") == (1, "0.450")
    assert one(fresh_pg_server, "SELECT a.settings_version_id = s.id, a.scope_version_id = s.scope_version_id, "
               "a.rubric_version_id IS NULL, a.prompt_schema_version_id IS NULL, a.llm_tuple_id IS NULL "
               "FROM active_config a, settings_versions s WHERE s.version_no = 1") == (True, True, True, True, True)
    assert one(fresh_pg_server, "SELECT payload ? 'max_ad_age_days', payload->>'remote_within_germany' FROM scope_versions") == (False, "true")
    assert one(fresh_pg_server, "SELECT anchor_utc, interval_s, enabled, last_evaluated_slot FROM schedule_state") == (
        datetime(2026, 10, 16, 4, tzinfo=UTC), 21600, True, datetime(2026, 10, 16, 10, tzinfo=UTC))
    assert one(fresh_pg_server, "SELECT generation FROM ranking_state") == (0,)

async def test_migrate_is_idempotent(fresh_pg_server, make_config) -> None:
    cfg = make_config("migrate", fresh_pg_server)
    await run_migrate_async(cfg, now=NOW); await run_migrate_async(cfg, now=datetime(2026, 10, 17, tzinfo=UTC))
    assert one(fresh_pg_server, "SELECT (SELECT count(*) FROM model_policies), (SELECT count(*) FROM settings_versions), "
               "(SELECT count(*) FROM scope_versions), (SELECT count(*) FROM active_config), (SELECT anchor_utc FROM schedule_state)") == (
        1, 1, 1, 1, datetime(2026, 10, 16, 4, tzinfo=UTC))

async def test_grants_step_skips_absent_tables(migrated) -> None:
    from jobhunter.db.session import make_null_engine
    engine = make_null_engine(migrated.dsn("migrator", driver="sqlalchemy"))
    async with engine.begin() as conn:
        assert await apply_explicit_grants(conn) == []
    await engine.dispose()

async def test_checkpointer_setup_is_idempotent(migrated) -> None:
    await setup_checkpointer(migrated.dsn("migrator")); await setup_checkpointer(migrated.dsn("migrator"))
```

```python
# tests/integration/test_sql_errors.py
import pytest, sqlalchemy as sa
from jobhunter.db.session import make_null_engine
from jobhunter.db.sql import UNDEFINED_TABLE, UNIQUE_VIOLATION, PgErrorInfo, is_unique_violation, pg_error
pytestmark = pytest.mark.integration
ALERT = sa.text("INSERT INTO alerts (code,severity,dedup_key,message) VALUES ('a','warn','sql-errors','m')")

async def test_wrapped_errors_are_classified_and_savepoints_keep_the_transaction(migrated) -> None:
    engine = make_null_engine(migrated.dsn("app", driver="sqlalchemy"))
    async with engine.connect() as c:
        with pytest.raises(sa.exc.ProgrammingError) as missing:
            async with c.begin_nested(): await c.execute(sa.text("SELECT 1 FROM no_such_table"))
        assert pg_error(missing.value).sqlstate == UNDEFINED_TABLE
        await c.execute(ALERT)                                              # usable: only the savepoint rolled back
        with pytest.raises(sa.exc.IntegrityError) as dup:
            async with c.begin_nested(): await c.execute(ALERT)
        assert pg_error(dup.value) == PgErrorInfo(UNIQUE_VIOLATION, "alerts_open_dedup")
        assert is_unique_violation(dup.value, "alerts_open_dedup") and not is_unique_violation(dup.value, "runs_slot_uq")
        await c.rollback()
    await engine.dispose()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_seed_math.py tests/unit/test_json_access.py tests/integration/test_migrate.py tests/integration/test_sql_errors.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'jobhunter.db.migrate'`

- [ ] **Step 3: Implement `run_migrate_async`, the seeds and the checkpointer setup**

- **`run_migrate_async`** runs `MIGRATE_ORDER` as the migrator and logs `migrate_step` with `step=` for each step.
- Steps 1, 3 and 7 log `migrate_step_noop` with `reason="manifest_stream_arrives_in_M5"`.
- **`alembic`**: `await asyncio.to_thread(upgrade_head, cfg.dsn("sqlalchemy"))`.
- **`grants`**: for each table of `BACKUP_GRANT_TABLES` where `to_regclass` is not NULL, `GRANT INSERT, UPDATE ON <t> TO jobhunter_backup`.
- **`queue_and_checkpointer`**:
  - `procrastinate.App(connector=PsycopgConnector(conninfo=cfg.dsn("psycopg"), min_size=1, max_size=1))`, opened with `await app.open_async()`;
  - `await app.schema_manager.apply_schema_async()` only when `to_regclass('procrastinate_jobs') IS NULL`. Upgrades of the procrastinate schema are owned by M5 (Deviation 5);
  - then `await setup_checkpointer(cfg.dsn("psycopg"))` on one `psycopg.AsyncConnection.connect(dsn, autocommit=True, prepare_threshold=0, row_factory=dict_row)`.
- **`seed`**: `seed_defaults` inside one transaction. There are no nested event loops anywhere in the orchestration.
- **The `types` accessors** walk `path.split(".")` with `isinstance` checks. `load_settings_v1` checks the root with `get_object`, and `scope_payload` and `seed_defaults` read the settings only through `get_object` / `get_bool` / `get_int` / `get_str`.
- **`pg_error`**: `orig = exc.orig`. When `isinstance(orig, psycopg.Error)`, it returns `PgErrorInfo(orig.sqlstate, orig.diag.constraint_name)`; otherwise `PgErrorInfo(None, None)`. For a unique index, the constraint name is the index name. This is the only code that looks beneath a SQLAlchemy exception.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_seed_math.py tests/unit/test_json_access.py tests/integration -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/src/jobhunter tests
git commit -m "feat(m0): async migrate in the §19.3 order with grants, queue and checkpointer setup, complete §13.1 default seeds and active_config"
```

---

### Task 7: Transition helper, transition matrix, alerts and per-test isolation

**Files:**
- Create: `backend/src/jobhunter/errors.py`, `backend/src/jobhunter/db/transitions.py`, `backend/src/jobhunter/db/migrations/versions/0002_m0_transition_matrix.py`, `backend/src/jobhunter/observability/alerts.py`, `tests/crash/__init__.py`, `tests/crash/conftest.py`
- Modify: `backend/src/jobhunter/db/seed.py` (`sync_transition_matrix` in the seed step), `tests/support/fixtures.py`, `tests/integration/conftest.py`
- Test: `tests/unit/test_transition_registry.py`, `tests/integration/test_transitions.py`, `tests/integration/test_transition_property.py`, `tests/integration/test_alerts.py`

**Interfaces:**
- Consumes: `migrated`, `make_engine`, `q`.
- Produces:
  - `errors.DomainError(Exception)`; `errors.TransitionConflict(DomainError)` (`table`, `row_id`, `to_state`); `errors.FencedOut(DomainError)` (`owner: str`); `errors.InvariantViolation(DomainError)`
  - `@dataclass(frozen=True) class TransitionSpec`: `table: str`, `state_col: str`, `reason_col: str | None`, `changed_at: Mapping[str, str]`, `owner_cols: frozenset[str]`, `clear_on_exit: Mapping[str, tuple[str, ...]]`, `allowed: frozenset[tuple[str, str]]`
  - `TRANSITIONS: Final[Mapping[str, TransitionSpec]]` (`runs`, `operations`)
  - `async def transition(conn: AsyncConnection, table: str, row_id: int, from_states: Sequence[str], to_state: str, *, reason: str | None = None, owner: Mapping[str, object] | None = None, set_: Mapping[str, object] | None = None) -> None` (raises `TransitionConflict` on 0 rows; `ValueError` for an edge outside `allowed`, an owner key outside `owner_cols`, or a `set_` key that `clear_on_exit` will clear)
  - `async def seed.sync_transition_matrix(conn: AsyncConnection) -> int`
  - `alerts.Severity = Literal["info", "warn", "critical"]`
  - `async def alerts.raise_alert(conn: AsyncConnection, *, code: str, severity: Severity, subject: str, message: str, context: Mapping[str, JSONValue] | None = None) -> int` (dedup key `f"{code}:{subject}"`)
  - `async def alerts.resolve_alert(conn: AsyncConnection, *, code: str, subject: str) -> bool`
  - `async def alerts.refresh_alert_gauge(conn: AsyncConnection) -> dict[Severity, int]` (sets `ALERTS_OPEN` for all three severities)
  - Fixtures:
    - `clean_db`: autouse in `tests/integration` and `tests/crash` whenever `migrated` is in use (tests that need it regardless, such as the hypothesis property, request `migrated` and `clean_db` explicitly). It is a sync fixture on a psycopg connection, so sync tests (the hypothesis property) and async tests both use it. **Before and after** each test it runs, as migrator, `TRUNCATE runs, run_attempts, operations, sessions, login_attempts, login_lockouts, api_idempotency, alerts, purge_fences, worker_heartbeat, users, procrastinate_jobs, procrastinate_periodic_defers, procrastinate_workers, checkpoints, checkpoint_writes, checkpoint_blobs CASCADE`, then resets `schedule_state` to its seeded anchor and interval with `last_evaluated_slot = latest slot ≤ now()` and `enabled = true`.
    - `engine`: an async app-role `make_engine`, disposed after the test.
    - `migrator_engine`.

The allowed edges (§7.16.4 and §7.16.8). Each to-state maps to its changed-at column:

| Table | Edges | Changed-at column | `clear_on_exit` |
|---|---|---|---|
| `runs` | `queued→running`, `running→running`, `running→succeeded`, `running→partial`, `running→failed_invariant`, `running→failed`, `queued→failed`, `queued→cancelled`, `running→cancelled` | `running → started_at` (skipped on a self-edge); the terminal states → `finished_at` | — |
| `operations` | `pending→running`, `running→succeeded`, `running→failed`, `running→pending`, `pending→cancelled`, `running→cancelled` | `running → started_at`; `succeeded`, `failed`, `cancelled` → `finished_at` | `running: (claim_id, worker_epoch, heartbeat_at)` |

Owner columns: `runs: {attempt_no}`, `operations: {claim_id}`. Reason columns: `runs.status_reason`, `operations.last_error_code`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_transition_registry.py
from jobhunter.db.transitions import TRANSITIONS
def test_run_and_operation_edges() -> None:
    assert ("succeeded", "running") not in TRANSITIONS["runs"].allowed
    assert ("running", "running") in TRANSITIONS["runs"].allowed
    assert TRANSITIONS["operations"].allowed == {("pending", "running"), ("running", "succeeded"), ("running", "failed"),
        ("running", "pending"), ("pending", "cancelled"), ("running", "cancelled")}
    assert TRANSITIONS["operations"].clear_on_exit == {"running": ("claim_id", "worker_epoch", "heartbeat_at")}
```

```python
# tests/integration/test_transitions.py
import pytest, sqlalchemy as sa
from jobhunter.db.sql import CHECK_VIOLATION, pg_error
from jobhunter.db.transitions import transition
from jobhunter.errors import TransitionConflict
pytestmark = pytest.mark.integration
INS = sa.text("INSERT INTO runs (trigger,scheduled_slot,status,include_paid,settings_version_id,scope_version_id) "
              "SELECT 'manual',NULL,'queued',false,settings_version_id,scope_version_id FROM active_config RETURNING id")
OP = sa.text("INSERT INTO operations (kind,payload,dedup_key,status) VALUES ('export','{}','e','pending') RETURNING id")

async def test_cas_success_conflict_and_owner(engine) -> None:
    async with engine.begin() as c:
        rid = (await c.execute(INS)).scalar_one()
        await transition(c, "runs", rid, ["queued"], "running", set_={"attempt_no": 1, "heartbeat_at": sa.func.now(),
                         "current_thread_id": f"run-{rid}-a1"})
        assert tuple((await c.execute(sa.text("SELECT status, started_at IS NOT NULL FROM runs WHERE id=:i"), {"i": rid})).one()) == ("running", True)
        with pytest.raises(TransitionConflict): await transition(c, "runs", rid, ["queued"], "running")
        with pytest.raises(TransitionConflict): await transition(c, "runs", rid, ["running"], "succeeded", owner={"attempt_no": 2})
        await transition(c, "runs", rid, ["running"], "succeeded", owner={"attempt_no": 1}, reason="done", set_={"report": {"noop": True, "ü": [1]}})
        assert tuple((await c.execute(sa.text("SELECT status_reason, finished_at IS NOT NULL, report FROM runs WHERE id=:i"), {"i": rid})).one()) == (
            "done", True, {"noop": True, "ü": [1]})

@pytest.mark.parametrize(("to", "reason"), [("succeeded", None), ("failed", "boom"), ("cancelled", "superseded"), ("pending", "retry")])
async def test_exit_from_running_clears_ownership(engine, to, reason) -> None:
    async with engine.begin() as c:
        op = (await c.execute(OP)).scalar_one()
        await transition(c, "operations", op, ["pending"], "running",
                         set_={"claim_id": sa.func.gen_random_uuid(), "worker_epoch": 1, "heartbeat_at": sa.func.now()})
        await transition(c, "operations", op, ["running"], to, reason=reason)
        row = (await c.execute(sa.text("SELECT status, claim_id, worker_epoch, heartbeat_at FROM operations WHERE id=:i"), {"i": op})).one()
        assert tuple(row) == (to, None, None, None)

async def test_helper_refuses_unknown_edge_owner_key_and_cleared_set(engine) -> None:
    async with engine.begin() as c:
        with pytest.raises(ValueError): await transition(c, "runs", 1, ["succeeded"], "running")
        with pytest.raises(ValueError): await transition(c, "runs", 1, ["queued"], "running", owner={"claim_id": "x"})
        with pytest.raises(ValueError): await transition(c, "operations", 1, ["running"], "succeeded", set_={"claim_id": None})

async def test_trigger_rejects_disallowed_direct_update(engine) -> None:
    async with engine.begin() as c:
        rid = (await c.execute(INS)).scalar_one()
        await c.execute(sa.text("UPDATE runs SET status='cancelled', finished_at=now() WHERE id=:i"), {"i": rid})
        with pytest.raises(sa.exc.IntegrityError, match="disallowed transition runs: cancelled -> queued") as bad:
            await c.execute(sa.text("UPDATE runs SET status='queued', finished_at=NULL WHERE id=:i"), {"i": rid})
        assert pg_error(bad.value).sqlstate == CHECK_VIOLATION
```

```python
# tests/integration/test_transition_property.py
import asyncio
import pytest, sqlalchemy as sa
from hypothesis import HealthCheck, given, settings, strategies as st
from jobhunter.db.session import make_engine
from jobhunter.db.transitions import TRANSITIONS, transition
from jobhunter.errors import TransitionConflict
pytestmark = pytest.mark.integration
STATES = ["queued", "running", "succeeded", "partial", "failed", "failed_invariant", "cancelled"]
STEP = st.tuples(st.lists(st.sampled_from(STATES), min_size=1, max_size=3, unique=True), st.sampled_from(STATES))

@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.lists(STEP, min_size=1, max_size=12))
def test_transition_calls_match_the_matrix(migrated, clean_db, make_config, steps) -> None:
    spec = TRANSITIONS["runs"]               # migrated: schema and active_config exist; clean_db: no rows leak in or out
    async def scenario() -> None:
        engine = make_engine(make_config("api", migrated))
        async with engine.connect() as c:    # autobegin; each example rolls back below
            rid = (await c.execute(sa.text("INSERT INTO runs (trigger,scheduled_slot,status,include_paid,settings_version_id,"
                "scope_version_id) SELECT 'manual',NULL,'queued',false,settings_version_id,scope_version_id FROM active_config "
                "RETURNING id"))).scalar_one()
            state, attempt = "queued", 0
            for froms, to in steps:
                legal = all((f, to) in spec.allowed for f in froms)
                if not legal:
                    with pytest.raises(ValueError): await transition(c, "runs", rid, froms, to)
                    continue
                extra = ({"attempt_no": attempt + 1, "heartbeat_at": sa.func.now(),
                          "current_thread_id": f"run-{rid}-a{attempt + 1}"} if to == "running" else {})
                if state in froms:
                    await transition(c, "runs", rid, froms, to, set_=extra)
                    state, attempt = to, attempt + (1 if to == "running" else 0)
                else:
                    with pytest.raises(TransitionConflict): await transition(c, "runs", rid, froms, to, set_=extra)
                assert (await c.execute(sa.text("SELECT status FROM runs WHERE id=:i"), {"i": rid})).scalar_one() == state
            await c.rollback()
        await engine.dispose()
    asyncio.run(scenario())
```

```python
# tests/integration/test_alerts.py
import pytest, sqlalchemy as sa
from jobhunter.observability.alerts import raise_alert, refresh_alert_gauge, resolve_alert
pytestmark = pytest.mark.integration
async def test_dedup_resolve_and_gauge(engine) -> None:
    async with engine.begin() as c:
        a = await raise_alert(c, code="run.orphaned_queue", severity="warn", subject="run:5", message="m", context={"run_id": 5})
        assert await raise_alert(c, code="run.orphaned_queue", severity="warn", subject="run:5", message="m") == a
        assert (await c.execute(sa.text("SELECT occurrences, context FROM alerts WHERE id=:i"), {"i": a})).one() == (2, {"run_id": 5})
        assert await refresh_alert_gauge(c) == {"info": 0, "warn": 1, "critical": 0}
        assert await resolve_alert(c, code="run.orphaned_queue", subject="run:5") is True
        assert await raise_alert(c, code="run.orphaned_queue", severity="warn", subject="run:5", message="m") != a
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_transition_registry.py tests/integration/test_transitions.py tests/integration/test_transition_property.py tests/integration/test_alerts.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'jobhunter.db.transitions'`

- [ ] **Step 3: Implement the helper, the migration, the matrix sync, the alerts and the fixtures**

- **`0002_m0_transition_matrix`** creates the table, the trigger function and two triggers:

```sql
CREATE TABLE transition_matrix (table_name text NOT NULL, from_state text NOT NULL, to_state text NOT NULL,
                                PRIMARY KEY (table_name, from_state, to_state));
CREATE FUNCTION assert_transition() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE o text := to_jsonb(OLD) ->> TG_ARGV[0]; n text := to_jsonb(NEW) ->> TG_ARGV[0];
BEGIN
  IF o IS DISTINCT FROM n AND NOT EXISTS (SELECT 1 FROM transition_matrix
       WHERE table_name = TG_TABLE_NAME AND from_state = o AND to_state = n) THEN
    RAISE EXCEPTION 'disallowed transition %: % -> %', TG_TABLE_NAME, o, n USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER runs_assert_transition BEFORE UPDATE OF status ON runs FOR EACH ROW EXECUTE FUNCTION assert_transition('status');
CREATE TRIGGER operations_assert_transition BEFORE UPDATE OF status ON operations FOR EACH ROW EXECUTE FUNCTION assert_transition('status');
```

- **`sync_transition_matrix`** replaces the rows of every registered table inside the seed transaction.
- **`transition`**:
  - It validates every `(from, to)` pair against `allowed` before touching the DB.
  - It builds `UPDATE <table> SET <state>=:to[, <reason>=:reason][, <changed_at>=now()][, <cleared col>=NULL …][, <set_ cols>] WHERE id=:id AND <state> = ANY(:from)[ AND <owner col> = :owner_<col>] RETURNING id`.
  - The cleared columns are those of `clear_on_exit[f]` for each `f ∈ from_states` with `f ≠ to_state`. Identifiers come only from the registry.
  - `set_` values that are `Mapping` or `list` are bound as `JSONB`; SQLAlchemy expressions such as `sa.func.now()` are rendered as expressions.
- **`raise_alert`**: `INSERT … ON CONFLICT (dedup_key) WHERE resolved_at IS NULL DO UPDATE SET occurrences = alerts.occurrences + 1, last_at = now() RETURNING id`, with `context` as JSONB.
- **`refresh_alert_gauge`**: `SELECT severity, count(*) … WHERE resolved_at IS NULL GROUP BY severity`, then sets `ALERTS_OPEN.labels(severity)` for all three severities, missing ones as 0.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit tests/integration -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/src/jobhunter tests
git commit -m "feat(m0): CAS transitions that clear ownership on exit, DB transition matrix, alerts with gauge, per-test isolation"
```

---

### Task 8: Worker singleton: lock session, standby, takeover, stoppable watchdog, epoch

**Files:**
- Create: `backend/src/jobhunter/worker/__init__.py`, `worker/lock.py`, `worker/watchdog.py`, `worker/epoch.py`, `tests/support/procs.py`, `tests/crash/lock_holder.py`
- Modify: `backend/src/jobhunter/cli.py` (`worker-health`), `tests/support/fixtures.py` (fixture `procs`)
- Test: `tests/integration/test_epoch.py`, `tests/crash/test_singleton.py`

**Interfaces:**
- Consumes: `AppConfig`, `make_engine`, `migrated`, `clean_db`.
- Produces:
  - `lock.ADVISORY_LOCK_KEY: Final = 7261001`, `HEARTBEAT_PERIOD_S: Final = 10`, `HEARTBEAT_STALE_S: Final = 60`, `STANDBY_SLEEP_S: Final = 10`, `WATCHDOG_EXIT_CODE: Final = 70`
  - `class LockSession`:
    - `__init__(self, dsn: str)`;
    - `connect(self) -> int` (a sync `psycopg.Connection` with `autocommit=True, keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3, application_name="jobhunter-worker-lock"` and `SET statement_timeout = '5s'`; returns the backend pid);
    - `try_lock(self) -> bool`; `terminate_stale_holder(self) -> bool`; `heartbeat(self, instance_id: UUID, epoch: int) -> bool`; `close(self) -> None` (closing releases the lock).
  - `lock.acquire_lock(session: LockSession, *, sleep: Callable[[float], None] = time.sleep) -> None` (logs `standby` per wait)
  - `watchdog.Watchdog(threading.Thread)`: `__init__(self, session: LockSession, instance_id: UUID, epoch: int, *, period_s: float = HEARTBEAT_PERIOD_S, exit: Callable[[int], NoReturn] = os._exit)` with `daemon=True`; `stop(self, timeout_s: float = 15.0) -> None` (sets the stop event and joins; after `stop` returns, the watchdog never calls `exit`)
  - `@dataclass(frozen=True) class WorkerIdentity`: `instance_id: UUID`, `hostname: str`, `pid: int`, `lock_backend_pid: int`, `app_version: str`
  - `epoch.TakeoverSweep = Callable[[AsyncConnection, int], Awaitable[int]]`; `epoch.register_takeover_sweep(name: str) -> Callable[[TakeoverSweep], TakeoverSweep]`
  - `async def epoch.claim_epoch_and_sweep(engine: AsyncEngine, ident: WorkerIdentity) -> tuple[int, dict[str, int]]`
  - CLI `jobhunter worker-health` (exit 0 when `heartbeat_at` is ≤ 60 s old, else 1)
  - `procs.Proc`: `pid: int`; `wait_event(self, event: str, timeout_s: float, **match: object) -> dict[str, Any]` (the first JSON log line with that `event` and matching fields, read by a background reader thread); `is_stopped(self) -> bool` (`/proc/<pid>/stat` state `T`); `signal(self, sig: int) -> None`; `exit_within(self, timeout_s: float) -> int | None` (the exit code, negative for a signal, or `None` when the process is still alive at the deadline; it never raises on timeout); `output(self) -> str`
  - `procs.ProcessGroup.spawn(self, args: list[str], env: Mapping[str, str]) -> Proc`; fixture `procs` yields a `ProcessGroup`, and its finalizer always sends `SIGCONT` then `SIGKILL` to every process still alive and waits for it
  - `procs.wait_until(pred: Callable[[], bool], timeout_s: float, step_s: float = 0.2) -> None`; `async def procs.await_until(pred: Callable[[], Awaitable[bool]], timeout_s: float, step_s: float = 0.2) -> None`
  - `tests/crash/lock_holder.py` connects, acquires, claims the epoch, starts the watchdog, prints `{"event": "holder_ready", "instance_id": "<uuid>", "epoch": N, "lock_pid": P}` and sleeps

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_epoch.py
import uuid
import pytest, sqlalchemy as sa
from jobhunter.worker.epoch import WorkerIdentity, claim_epoch_and_sweep
pytestmark = pytest.mark.integration
async def test_epoch_increments_and_records_holder(engine) -> None:
    e1, _ = await claim_epoch_and_sweep(engine, WorkerIdentity(uuid.uuid4(), "h", 1, 4242, "0.1.0"))
    e2, _ = await claim_epoch_and_sweep(engine, WorkerIdentity(uuid.uuid4(), "h", 2, 4343, "0.1.0"))
    assert e2 == e1 + 1
    async with engine.connect() as c:
        assert tuple((await c.execute(sa.text("SELECT worker_epoch, lock_backend_pid FROM worker_heartbeat"))).one()) == (e2, 4343)
```

```python
# tests/crash/test_singleton.py
import signal, subprocess, sys
import psycopg, pytest
from tests.support.procs import wait_until
pytestmark = [pytest.mark.crash, pytest.mark.slow]
HOLDER = [sys.executable, "-m", "tests.crash.lock_holder"]

def sql1(server, statement: str, *args: object):
    with psycopg.connect(server.dsn("superuser"), autocommit=True) as c:
        cur = c.execute(statement, args or None); return cur.fetchone() if cur.description else None

def test_second_holder_stays_standby_while_fresh(migrated, proc_env, procs) -> None:
    a = procs.spawn(HOLDER, proc_env("worker", migrated)); ready = a.wait_event("holder_ready", 20)
    b = procs.spawn(HOLDER, proc_env("worker", migrated))
    b.wait_event("standby", 15); b.wait_event("standby", 15, attempt=2)       # two full standby cycles
    assert sql1(migrated, "SELECT worker_instance_id::text FROM worker_heartbeat") == (ready["instance_id"],)
    assert a.exit_within(0.1) is None                                         # a is still alive

def test_takeover_terminates_stale_holder(migrated, proc_env, procs) -> None:
    a = procs.spawn(HOLDER, proc_env("worker", migrated)); ra = a.wait_event("holder_ready", 20)
    a.signal(signal.SIGSTOP); wait_until(a.is_stopped, 5)
    sql1(migrated, "UPDATE worker_heartbeat SET heartbeat_at = now() - interval '2 minutes'")
    b = procs.spawn(HOLDER, proc_env("worker", migrated)); rb = b.wait_event("holder_ready", 30)
    assert rb["epoch"] == ra["epoch"] + 1
    assert sql1(migrated, "SELECT pid FROM pg_locks WHERE locktype='advisory' AND objid=7261001 AND granted") == (rb["lock_pid"],)
    a.signal(signal.SIGCONT)
    assert a.exit_within(15) == 70

def test_c18_watchdog_exits_70_when_lock_session_dies(migrated, proc_env, procs) -> None:
    a = procs.spawn(HOLDER, proc_env("worker", migrated)); ready = a.wait_event("holder_ready", 20)
    sql1(migrated, "SELECT pg_terminate_backend(%s)", ready["lock_pid"])
    assert a.exit_within(12) == 70

def test_watchdog_exits_on_db_restart(fresh_pg_server, make_config, proc_env, procs) -> None:      # Review Focus 1
    from jobhunter.db.migrate import run_migrate
    from tests.support.pg import restart_pg_server
    run_migrate(make_config("migrate", fresh_pg_server))
    a = procs.spawn(HOLDER, proc_env("worker", fresh_pg_server)); a.wait_event("holder_ready", 20)
    restart_pg_server(fresh_pg_server)
    assert a.exit_within(15) == 70

def test_no_termination_when_pid_mismatch(migrated, proc_env, procs) -> None:
    a = procs.spawn(HOLDER, proc_env("worker", migrated)); ra = a.wait_event("holder_ready", 20)
    a.signal(signal.SIGSTOP); wait_until(a.is_stopped, 5)
    sql1(migrated, "UPDATE worker_heartbeat SET heartbeat_at = now() - interval '2 minutes', lock_backend_pid = 1")
    b = procs.spawn(HOLDER, proc_env("worker", migrated)); b.wait_event("standby", 15); b.wait_event("standby", 15, attempt=2)
    assert sql1(migrated, "SELECT pid FROM pg_locks WHERE locktype='advisory' AND objid=7261001 AND granted") == (ra["lock_pid"],)

def test_worker_health_cli(migrated, proc_env, procs) -> None:
    env = proc_env("worker", migrated)
    a = procs.spawn(HOLDER, env); a.wait_event("holder_ready", 20)
    assert subprocess.run([sys.executable, "-m", "jobhunter.cli", "worker-health"], env={**env}).returncode == 0
    a.signal(signal.SIGSTOP); wait_until(a.is_stopped, 5)
    sql1(migrated, "UPDATE worker_heartbeat SET heartbeat_at = now() - interval '61 seconds'")
    assert subprocess.run([sys.executable, "-m", "jobhunter.cli", "worker-health"], env={**env}).returncode == 1

def test_watchdog_stop_never_exits(migrated, make_config) -> None:
    import uuid
    from jobhunter.worker.lock import LockSession, acquire_lock
    from jobhunter.worker.watchdog import Watchdog
    s = LockSession(make_config("worker").dsn("psycopg")); s.connect(); acquire_lock(s)
    exits: list[int] = []
    w = Watchdog(s, uuid.uuid4(), 1, period_s=0.1, exit=lambda code: exits.append(code))  # type: ignore[arg-type,return-value]
    w.start(); w.stop(); s.close()
    assert not w.is_alive() and exits == [] and w.daemon
```

(`wait_event("standby", 15, attempt=2)` matches through `**match`: every `standby` log line carries its `attempt=n` field, so the second call waits for the second full standby cycle.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/integration/test_epoch.py tests/crash/test_singleton.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'jobhunter.worker'`

- [ ] **Step 3: Implement the lock session, `acquire_lock`, `Watchdog`, `claim_epoch_and_sweep`, `worker-health` and the process helpers**

- **`try_lock`**: `SELECT pg_try_advisory_lock(7261001)`.
- **`terminate_stale_holder`** calls `pg_terminate_backend(pid)` only when `worker_heartbeat` exists, `heartbeat_at < now() − interval '60 seconds'`, **and** `lock_backend_pid` equals the granted `pg_locks` pid for `objid = 7261001`.
- **`acquire_lock`** loops: `try_lock`; else `terminate_stale_holder()` (an immediate retry when `True`), else log `standby` with `attempt=n` and `sleep(10)`.
- **`Watchdog.run`**: `while not self._stop.wait(self.period_s)`, call `session.heartbeat(...)`; on `False` or an exception, call `exit(70)` unless `_stop` is set. `stop()` sets the event and `join(timeout_s)`. The heartbeat statement is the one in §3.3.1.
- **`claim_epoch_and_sweep`**: the upsert `INSERT INTO worker_heartbeat (…) VALUES (1, …, 1, …) ON CONFLICT (id) DO UPDATE SET worker_epoch = worker_heartbeat.worker_epoch + 1, worker_instance_id = EXCLUDED.worker_instance_id, hostname = EXCLUDED.hostname, pid = EXCLUDED.pid, lock_backend_pid = EXCLUDED.lock_backend_pid, app_version = EXCLUDED.app_version, started_at = now(), heartbeat_at = now() RETURNING worker_epoch`. It then runs every registered sweep, in registration order, in the same transaction.
- **`procs.Proc`** starts a daemon reader thread that parses each stdout line as JSON, ignoring non-JSON lines, into a list guarded by a condition variable.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/integration/test_epoch.py tests/crash/test_singleton.py -q`
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/src/jobhunter/worker backend/src/jobhunter/cli.py tests
git commit -m "feat(m0): worker singleton with lock session, standby, takeover, stoppable watchdog and epochs"
```

---

### Task 9: procrastinate app, in-transaction defer (V1), stalled jobs (V2), reconcilers with durable failure alerts

**Files:**
- Create: `backend/src/jobhunter/worker/tasks.py`, `backend/src/jobhunter/worker/reconcilers.py`, `docs/spikes/V1.md`, `docs/spikes/V2.md`, `tests/crash/stall_worker.py`
- Modify: `tests/support/fixtures.py` (fixture `queue_app`)
- Test: `tests/integration/test_queue.py`, `tests/integration/test_reconcilers.py`, `tests/crash/test_v2_stalled.py`

**Interfaces:**
- Consumes: `AppConfig`, `make_engine`, `raise_alert`, `resolve_alert`.
- Produces:
  - `TASK_NAMESPACE: Final = "jh"`; `TaskName = Literal["run_collect", "run_operation", "tick", "reconcile_heavy", "retention_daily"]`; `job_task_name(name: TaskName) -> str` (`"jh:<name>"`)
  - `blueprint: procrastinate.Blueprint` with five tasks, every one `pass_context=True`. Task 9 registers them with stub bodies that log `task_stub`; Tasks 10–13 fill them in:
    - `run_collect` (queue `collect`, `lock="collect"`);
    - `run_operation` (queue `aux`);
    - `tick` (queue `tick`, periodic `* * * * *`, `queueing_lock="tick"`);
    - `reconcile_heavy` (queue `tick`, periodic `*/10 * * * *`);
    - `retention_daily` (queue `tick`, periodic `17 3 * * *`).
  - `QUEUES: Final[Mapping[str, int]] = {"collect": 1, "score": 1, "aux": 2, "tick": 1}`
  - `make_queue_app(dsn: str, *, max_size: int = 4) -> procrastinate.App`. The first call in a process creates the **task app** (the blueprint added under namespace `jh`); later calls with the same dsn return it; another dsn gives a separate `App` without tasks, used for job-manager queries. `task_app() -> procrastinate.App`.
  - `class Deferrer(Protocol)`: `async def __call__(self, conn: AsyncConnection, task: TaskName, *, lock: str | None = None, queueing_lock: str | None = None, **kwargs: JSONValue) -> int | None`
  - `async def defer_in_tx(conn: AsyncConnection, task: TaskName, *, lock: str | None = None, queueing_lock: str | None = None, **kwargs: JSONValue) -> int | None` (`None` means a job with that queueing lock is already waiting)
  - `async def live_job_exists(conn: AsyncConnection, task: TaskName, *, args_match: Mapping[str, JSONValue]) -> bool`
  - `async def release_stalled_jobs(app: procrastinate.App, *, seconds_since_heartbeat: float = 60.0) -> int`; `async def delete_finished_jobs(app: procrastinate.App, *, older_than_hours: int = 168) -> None`
  - `Cadence = Literal["start", "tick", "heavy", "daily"]`; `@dataclass(frozen=True) class ReconcileContext`: `engine: AsyncEngine`, `queue: procrastinate.App`, `defer: Deferrer`, `now: datetime`, `epoch: int`
  - `ReconcilerFn = Callable[[ReconcileContext], Awaitable[int]]`; `REGISTRY: dict[str, tuple[frozenset[Cadence], ReconcilerFn]]`; `reconciler(name: str, *cadences: Cadence) -> Callable[[ReconcilerFn], ReconcilerFn]`; `async def run_reconcilers(ctx: ReconcileContext, cadence: Cadence) -> dict[str, int]`
  - Reconciler `stalled_jobs` (cadences `start`, `tick`)
  - Fixture `queue_app` (function, async): `app = make_queue_app(make_config("worker").dsn("psycopg"))`; `async with app.open_async(): yield app`

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_queue.py
import pytest, sqlalchemy as sa
from jobhunter.worker.tasks import defer_in_tx, delete_finished_jobs, job_task_name, live_job_exists
pytestmark = pytest.mark.integration
COUNT = sa.text("SELECT count(*) FROM procrastinate_jobs WHERE task_name = :t")

async def test_v1_rollback_leaves_no_job(engine, queue_app) -> None:
    async with engine.connect() as c:
        tx = await c.begin()
        assert await defer_in_tx(c, "run_collect", lock="collect", queueing_lock="run:1:1", run_id=1, attempt_no=1)
        await tx.rollback()
        assert (await c.execute(COUNT, {"t": job_task_name("run_collect")})).scalar_one() == 0

async def test_v1_commit_leaves_exactly_one_job(engine, queue_app) -> None:
    async with engine.begin() as c:
        await defer_in_tx(c, "run_collect", lock="collect", queueing_lock="run:1:1", run_id=1, attempt_no=1)
    async with engine.connect() as c:
        assert (await c.execute(COUNT, {"t": "jh:run_collect"})).scalar_one() == 1
        assert await live_job_exists(c, "run_collect", args_match={"run_id": 1})
        assert not await live_job_exists(c, "run_collect", args_match={"run_id": 2})

async def test_already_enqueued_keeps_caller_transaction_usable(engine, queue_app) -> None:
    async with engine.begin() as c:
        assert await defer_in_tx(c, "run_operation", queueing_lock="op:7", operation_id=7) is not None
        assert await defer_in_tx(c, "run_operation", queueing_lock="op:7", operation_id=7) is None
        assert (await c.execute(sa.text("SELECT 1"))).scalar_one() == 1        # still usable
    async with engine.connect() as c:
        assert (await c.execute(COUNT, {"t": "jh:run_operation"})).scalar_one() == 1

async def test_delete_finished_jobs_keeps_recent(engine, queue_app) -> None:
    async with engine.begin() as c:
        await defer_in_tx(c, "retention_daily", timestamp=1); await defer_in_tx(c, "retention_daily", timestamp=2)
        await c.execute(sa.text("UPDATE procrastinate_jobs SET status='doing'"))
        await c.execute(sa.text("UPDATE procrastinate_jobs SET status='succeeded'"))
        await c.execute(sa.text("UPDATE procrastinate_events SET at = now() - interval '8 days' "
                                "WHERE job_id = (SELECT min(id) FROM procrastinate_jobs)"))
    await delete_finished_jobs(queue_app, older_than_hours=168)
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT count(*) FROM procrastinate_jobs"))).scalar_one() == 1
```

```python
# tests/integration/test_reconcilers.py
import pytest, sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.worker import reconcilers as r
from jobhunter.worker.tasks import defer_in_tx
pytestmark = pytest.mark.integration
async def test_failure_is_persisted_isolated_and_resolved(engine, queue_app, monkeypatch) -> None:
    state = {"fail": True}
    async def flaky(ctx: r.ReconcileContext) -> int:
        if state["fail"]: raise RuntimeError("detail that must not be stored")
        return 0
    async def ok(ctx: r.ReconcileContext) -> int: return 3
    monkeypatch.setattr(r, "REGISTRY", {})
    r.reconciler("flaky", "tick")(flaky); r.reconciler("ok", "tick")(ok); r.reconciler("daily_only", "daily")(ok)
    ctx = r.ReconcileContext(engine=engine, queue=queue_app, defer=defer_in_tx, now=utcnow(), epoch=1)
    assert await r.run_reconcilers(ctx, "tick") == {"flaky": -1, "ok": 3}
    await r.run_reconcilers(ctx, "tick")
    async with engine.connect() as c:
        row = (await c.execute(sa.text("SELECT severity, occurrences, context, message FROM alerts "
                                       "WHERE code='reconciler.failed' AND resolved_at IS NULL"))).one()
    assert tuple(row[:3]) == ("warn", 2, {"error_class": "RuntimeError", "reconciler": "flaky"})
    assert "must not be stored" not in row[3]
    state["fail"] = False; await r.run_reconcilers(ctx, "tick")
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT count(*) FROM alerts WHERE code='reconciler.failed' AND resolved_at IS NULL"))).scalar_one() == 0
```

```python
# tests/crash/test_v2_stalled.py
import signal, sys
import pytest, sqlalchemy as sa
from jobhunter.worker.tasks import release_stalled_jobs
from tests.support.procs import await_until
pytestmark = [pytest.mark.crash, pytest.mark.slow]
async def test_v2_killed_worker_job_is_released(engine, queue_app, migrated, proc_env, procs) -> None:
    p = procs.spawn([sys.executable, "-m", "tests.crash.stall_worker"], proc_env("worker", migrated))
    p.wait_event("stall_job_started", 30)                                  # acknowledgement from inside the job
    p.signal(signal.SIGKILL); assert p.exit_within(10) == -signal.SIGKILL
    async with engine.begin() as c:
        await c.execute(sa.text("UPDATE procrastinate_workers SET last_heartbeat = now() - interval '5 minutes'"))
    assert await release_stalled_jobs(queue_app) == 1
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT status FROM procrastinate_jobs"))).scalar_one() == "todo"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/integration/test_queue.py tests/integration/test_reconcilers.py tests/crash/test_v2_stalled.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'jobhunter.worker.tasks'`

- [ ] **Step 3: Implement `tasks.py` and `reconcilers.py`**

- **`defer_in_tx`**:
  - `raw = (await conn.get_raw_connection()).driver_connection`;
  - inside `async with conn.begin_nested():` it runs `await task_app().tasks[job_task_name(task)].configure(connection=raw, lock=lock, queueing_lock=queueing_lock).defer_async(**kwargs)`;
  - a `procrastinate.exceptions.AlreadyEnqueued` rolls back that savepoint only and returns `None`. It is procrastinate's own exception, raised by its connector on the raw driver connection, so it is not a SQLAlchemy-wrapped driver error. Any other exception leaves the savepoint and propagates.

  The external connection bypasses the connector pool, so this works against any database without opening the app (procrastinate 3.10 `ConfigureTaskOptions.connection`).
- **`live_job_exists`**: `SELECT EXISTS (… procrastinate_jobs WHERE task_name = :t AND status IN ('todo','doing') AND args @> :m)`, with `m` JSONB-bound.
- **`release_stalled_jobs`**: `get_stalled_jobs(seconds_since_heartbeat=…)`, then `retry_job(job)` for each job (every M0 task is idempotent); returns the count.
- **`delete_finished_jobs`**: `delete_old_jobs(nb_hours=…, include_failed=True, include_cancelled=True, include_aborted=True)`.
- **`run_reconcilers`** runs the matching reconcilers in registration order, each in its own transaction:
  - On an exception (BLE001 allowed here), a separate transaction calls `raise_alert(code="reconciler.failed", severity="warn", subject=name, message=f"reconciler {name} failed", context={"reconciler": name, "error_class": type(exc).__name__})`, logs `reconciler_failed` without the message, and records `-1`.
  - On success, it calls `resolve_alert(code="reconciler.failed", subject=name)`.
- **`stall_worker.py`** registers `jh_stall:stall` on its own `App`, which prints `{"event": "stall_job_started"}` and sleeps 600 s. It defers the job and runs `run_worker_async(queues=["aux"], update_heartbeat_interval=1)`.
- **`V1.md` and `V2.md`** record the assumption, the method (these tests), the observed result and the decision:
  - V1: in-transaction defer through the caller's connection; correctness still rests on the reconcilers (§3.3.2);
  - V2: `get_stalled_jobs(seconds_since_heartbeat=60)` plus `retry_job`.

  If the observed API differs, record the actual names and adapt the bodies without changing the signatures.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/integration/test_queue.py tests/integration/test_reconcilers.py tests/crash/test_v2_stalled.py -q`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/src/jobhunter/worker docs/spikes/V1.md docs/spikes/V2.md tests
git commit -m "feat(m0): procrastinate blueprint with savepoint-safe in-transaction defer, stalled jobs, durable reconciler failures (V1, V2)"
```

---

### Task 10: Execution registry and durable operations with claim tokens

**Files:**
- Create: `backend/src/jobhunter/worker/executions.py`, `backend/src/jobhunter/worker/heartbeat.py`, `backend/src/jobhunter/worker/operations.py`
- Modify: `backend/src/jobhunter/worker/tasks.py` (the `run_operation` body calls `execute_operation`)
- Test: `tests/unit/test_executions.py`, `tests/integration/test_operations.py`

**Interfaces:**
- Consumes: `transition`, `raise_alert`, `defer_in_tx`, `live_job_exists`, `reconciler`, `register_takeover_sweep`, `FencedOut`.
- Produces:
  - `ExecKey = tuple[Literal["run", "operation"], int]`
  - `class ExecutionRegistry`:
    - `get(self, key: ExecKey) -> tuple[object, asyncio.Task[None]] | None` (token and live task);
    - `start(self, key: ExecKey, token: object, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]` (raises `AlreadyExecuting` when a live task holds the key);
    - `async def cancel_and_await(self, key: ExecKey, *, timeout_s: float = 30.0) -> bool`;
    - `async def cancel_all(self, *, timeout_s: float = 30.0) -> int`.

    Finished tasks are removed automatically. `EXECUTIONS: Final = ExecutionRegistry()`.
  - `async def heartbeat_loop(beat: Callable[[], Awaitable[bool]], *, period_s: float, on_lost: Callable[[], None]) -> None` (on `False` or an exception, calls `on_lost()` once and returns)
  - `OperationKind = Literal["extract_profile_text", "extract_profile", "embed_profile", "propose_queries", "apply_scope", "create_targets_for_tuple", "batch_submit", "export", "purge", "eval_run", "watchlist_validate"]`
  - `MAX_OPERATION_ATTEMPTS: Final = 10`; `OPERATION_STALE: Final = timedelta(minutes=3)`; `REDEFER_AFTER: Final = timedelta(minutes=2)`
  - `@dataclass(frozen=True) class OperationClaim`: `operation_id: int`, `claim_id: UUID`, `kind: OperationKind`, `payload: Mapping[str, JSONValue]`
  - `async def create_operation(conn, *, kind: OperationKind, payload: Mapping[str, JSONValue], dedup_key: str, defer: Deferrer | None) -> tuple[int, bool]` (`created` is `False` for an existing live row; it defers only when `created`)
  - `async def claim_operation(conn, operation_id: int, *, epoch: int) -> OperationClaim | None`
  - `async def heartbeat_operation(conn, claim: OperationClaim) -> bool`
  - `async def lock_operation_owner(conn, claim: OperationClaim) -> None` (raises `FencedOut`)
  - `async def finish_operation(conn, claim: OperationClaim, *, status: Literal["succeeded", "failed"], error_code: str | None = None) -> None` (raises `FencedOut`)
  - `async def requeue_operation(conn, claim: OperationClaim, *, error_code: str, not_before: datetime) -> Literal["pending", "failed"]` (raises `FencedOut`)
  - `async def cancel_operation(conn, operation_id: int, *, reason: str) -> bool`
  - `async def reclaim_stale_operations(conn, *, now: datetime) -> list[int]`
  - Takeover sweep `operations`; reconciler `operations` (cadence `tick`; it calls `EXECUTIONS.cancel_and_await(("operation", id))` for every id it reclaimed)
  - `@dataclass(frozen=True) class OperationContext`: `claim: OperationClaim`, `engine: AsyncEngine`
  - `OperationHandler = Callable[[OperationContext], Awaitable[None]]`; `HANDLERS: dict[OperationKind, OperationHandler]`; `register_operation_handler(kind: OperationKind) -> Callable[[OperationHandler], OperationHandler]`
  - `async def execute_operation(engine: AsyncEngine, operation_id: int, *, epoch: int, heartbeat_period_s: float = 30.0) -> None`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_executions.py
import asyncio
import pytest
from jobhunter.worker.executions import AlreadyExecuting, ExecutionRegistry
async def test_one_live_task_per_key_and_cancel_and_await() -> None:
    reg, started = ExecutionRegistry(), asyncio.Event()
    async def forever() -> None: started.set(); await asyncio.sleep(3600)
    task = reg.start(("run", 1), 1, forever()); await started.wait()
    with pytest.raises(AlreadyExecuting): reg.start(("run", 1), 2, forever())
    assert await reg.cancel_and_await(("run", 1)) is True and task.cancelled() and reg.get(("run", 1)) is None
    assert await reg.cancel_and_await(("run", 1)) is False
```

```python
# tests/integration/test_operations.py
import asyncio, uuid
from datetime import timedelta
import pytest, sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.errors import FencedOut
from jobhunter.worker import operations as ops
from jobhunter.worker.executions import EXECUTIONS
from jobhunter.worker.tasks import defer_in_tx, live_job_exists
from jobhunter.worker.epoch import WorkerIdentity, claim_epoch_and_sweep
pytestmark = pytest.mark.integration
ROW = sa.text("SELECT status, claim_id, worker_epoch, heartbeat_at, attempts, last_error_code FROM operations WHERE id=:i")

async def new_op(engine, defer=defer_in_tx, kind="export") -> tuple[int, bool]:
    async with engine.begin() as c:
        return await ops.create_operation(c, kind=kind, payload={"export_id": 1, "note": "ü"}, dedup_key=f"{kind}:1", defer=defer)

async def claim(engine, op: int):
    async with engine.begin() as c: return await ops.claim_operation(c, op, epoch=1)

async def age(engine, op: int, minutes: int) -> None:
    async with engine.begin() as c:
        await c.execute(sa.text("UPDATE operations SET heartbeat_at = now() - make_interval(mins => :m) WHERE id=:i"), {"m": minutes, "i": op})

async def row(engine, op: int):
    async with engine.connect() as c: return tuple((await c.execute(ROW, {"i": op})).one())

async def test_create_dedups_and_defers_only_new_rows(engine, queue_app) -> None:
    (a, created_a), (b, created_b) = await new_op(engine), await new_op(engine)
    assert a == b and (created_a, created_b) == (True, False)
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT count(*) FROM procrastinate_jobs"))).scalar_one() == 1
        assert (await c.execute(sa.text("SELECT payload FROM operations WHERE id=:i"), {"i": a})).scalar_one() == {"export_id": 1, "note": "ü"}

async def test_completion_failure_and_exhaustion_clear_ownership(engine, queue_app) -> None:
    op, _ = await new_op(engine); c1 = await claim(engine, op)
    async with engine.begin() as c: await ops.finish_operation(c, c1, status="succeeded")
    assert (await row(engine, op))[:4] == ("succeeded", None, None, None)
    op2, _ = await new_op(engine, kind="purge"); c2 = await claim(engine, op2)
    async with engine.begin() as c: await ops.finish_operation(c, c2, status="failed", error_code="bad_input")
    assert (await row(engine, op2))[:4] == ("failed", None, None, None)
    op3, _ = await new_op(engine, kind="eval_run")
    async with engine.begin() as c: await c.execute(sa.text("UPDATE operations SET attempts = 9 WHERE id=:i"), {"i": op3})
    c3 = await claim(engine, op3)
    async with engine.begin() as c:
        assert await ops.requeue_operation(c, c3, error_code="transient", not_before=utcnow()) == "failed"
    assert await row(engine, op3) == ("failed", None, None, None, 10, "attempts_exhausted")
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT severity FROM alerts WHERE code='operation.failed'"))).scalar_one() == "warn"

async def test_operation_aba_old_execution_is_fenced(engine, queue_app) -> None:     # §18.2 Operation ABA, C35 (M0 part)
    op, _ = await new_op(engine); a = await claim(engine, op)
    await age(engine, op, 4)
    async with engine.begin() as c: assert await ops.reclaim_stale_operations(c, now=utcnow()) == [op]
    b = await claim(engine, op)
    assert b is not None and a is not None and b.claim_id != a.claim_id
    async with engine.begin() as c:
        assert await ops.heartbeat_operation(c, a) is False
        with pytest.raises(FencedOut): await ops.lock_operation_owner(c, a)
    for call in (lambda c: ops.finish_operation(c, a, status="succeeded"),
                 lambda c: ops.requeue_operation(c, a, error_code="x", not_before=utcnow())):
        async with engine.begin() as c:
            with pytest.raises(FencedOut): await call(c)
    async with engine.begin() as c:
        await ops.lock_operation_owner(c, b); await ops.finish_operation(c, b, status="succeeded")
    assert (await row(engine, op))[0::4] == ("succeeded", 1)

async def test_takeover_sweep_returns_older_epoch_operations(engine, queue_app) -> None:
    op, _ = await new_op(engine)
    e1, _ = await claim_epoch_and_sweep(engine, WorkerIdentity(uuid.uuid4(), "h", 1, 1, "v"))
    async with engine.begin() as c: assert await ops.claim_operation(c, op, epoch=e1)
    e2, counts = await claim_epoch_and_sweep(engine, WorkerIdentity(uuid.uuid4(), "h", 2, 2, "v"))
    assert e2 == e1 + 1 and counts["operations"] == 1 and (await row(engine, op))[:4] == ("pending", None, None, None)

async def test_c30_reconciler_redefers_lost_job_after_two_minutes(engine, queue_app) -> None:
    from jobhunter.worker.reconcilers import ReconcileContext, run_reconcilers
    op, _ = await new_op(engine, defer=None)
    ctx = lambda dt: ReconcileContext(engine=engine, queue=queue_app, defer=defer_in_tx, now=utcnow() + dt, epoch=1)
    await run_reconcilers(ctx(timedelta(minutes=1)), "tick")
    async with engine.connect() as c: assert not await live_job_exists(c, "run_operation", args_match={"operation_id": op})
    await run_reconcilers(ctx(timedelta(minutes=2, seconds=1)), "tick")
    async with engine.connect() as c: assert await live_job_exists(c, "run_operation", args_match={"operation_id": op})

async def test_lost_ownership_stops_execution_without_owner_writes(engine, queue_app, monkeypatch) -> None:
    handler_started, cancelled = asyncio.Event(), asyncio.Event()
    async def handler(ctx: ops.OperationContext) -> None:
        handler_started.set()
        try: await asyncio.sleep(3600)
        except asyncio.CancelledError: cancelled.set(); raise
    monkeypatch.setitem(ops.HANDLERS, "export", handler)
    op, _ = await new_op(engine)
    run = asyncio.create_task(ops.execute_operation(engine, op, epoch=1, heartbeat_period_s=0.2))
    await handler_started.wait(); await age(engine, op, 4)
    async with engine.begin() as c: await ops.reclaim_stale_operations(c, now=utcnow())
    await asyncio.wait_for(run, 5)
    assert cancelled.is_set() and EXECUTIONS.get(("operation", op)) is None
    assert await row(engine, op) == ("pending", None, None, None, 1, None)
    assert not [t for t in asyncio.all_tasks() if t.get_name() == f"heartbeat:operation:{op}"]

async def test_unknown_kind_fails_with_no_handler_and_alert(engine, queue_app, monkeypatch) -> None:
    monkeypatch.delitem(ops.HANDLERS, "purge", raising=False)
    op, _ = await new_op(engine, kind="purge")
    await ops.execute_operation(engine, op, epoch=1)
    assert (await row(engine, op))[0::5] == ("failed", "no_handler")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_executions.py tests/integration/test_operations.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'jobhunter.worker.executions'`

- [ ] **Step 3: Implement the registry, the heartbeat loop and the operations**

Every state change goes through `transition()`, which clears `claim_id`, `worker_epoch` and `heartbeat_at` on every exit from `running` (Task 7).

**Bodies:**
- **claim**: `pending→running` with `set_={"claim_id": uuid4(), "worker_epoch": epoch, "heartbeat_at": now()}`, when `not_before IS NULL OR not_before <= now()`.
- **finish**: `running→succeeded|failed` with `owner={"claim_id": …}`. A `TransitionConflict` maps to `FencedOut("operation")`.
- **requeue and reclaim**: `running→pending` with `set_={"attempts": attempts + 1, "not_before": …}`. When `attempts + 1 >= 10`, the transition is `running→failed` with `reason='attempts_exhausted'`, `set_={"attempts": 10}`, plus `raise_alert(code="operation.failed", severity="warn", subject=f"operation:{id}", message="operation failed permanently")`.
- **`create_operation`**: `INSERT … ON CONFLICT (kind, dedup_key) WHERE status IN ('pending','running') DO NOTHING RETURNING id`.
  - Inserted: when `defer` is given, defer `run_operation(operation_id=…)` with `queueing_lock=f"op:{id}"` in the same transaction; return `(id, True)`.
  - Conflict: select the live row's id and return `(id, False)`; nothing is deferred.
- **`heartbeat_operation`** / **`lock_operation_owner`**: predicate `id = :i AND claim_id = :c AND status = 'running'` (`FOR SHARE` for the lock); zero rows means `False` / `FencedOut`.
- **Sweeps and reconcilers** select candidate ids `FOR UPDATE SKIP LOCKED` and transition row by row:
  - the takeover sweep takes `running` operations with `worker_epoch < :epoch`;
  - the reconciler re-defers `pending` operations without a live job once `now − greatest(created_at, coalesce(not_before, created_at)) ≥ 2 min`, then reclaims `running` ones whose heartbeat is older than 3 min.

**`execute_operation`** (one protocol for every way ownership can be lost):
1. claim; return when `None`. A missing handler gives `finish(failed, 'no_handler')` plus `operation.failed`; return.
2. `task = EXECUTIONS.start(("operation", id), claim.claim_id, handler(ctx))`, plus a heartbeat task named `f"heartbeat:operation:{id}"` whose `on_lost` calls `task.cancel()`.
3. Await `task`:
   - `CancelledError` (ownership lost) or `FencedOut` → return with **no further owner writes**;
   - another exception → `requeue_operation(error_code="bug", not_before=now + 1 min)`;
   - normal return → `finish(succeeded)`.

   Finish and requeue each run in their own `engine.begin()` block. A `FencedOut` from either is caught outside that block and returns quietly; the fenced transaction wrote nothing, because the `transition()` is its first write.
4. In `finally`: cancel and await the heartbeat task.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_executions.py tests/integration/test_operations.py -q`
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/src/jobhunter/worker tests
git commit -m "feat(m0): execution registry and durable operations with claim tokens, uniform ownership-loss handling"
```

---

### Task 11: Tick and slots

**Files:**
- Create: `backend/src/jobhunter/worker/tick.py`
- Modify: `backend/src/jobhunter/worker/tasks.py` (the `tick` body)
- Test: `tests/unit/test_slots.py`, `tests/property/test_slots_property.py`, `tests/integration/test_tick.py`, `tests/perf/test_tick_perf.py`

**Interfaces:**
- Consumes: `seed.latest_slot_at_or_before`, `Deferrer`, `defer_in_tx`, `release_stalled_jobs`, `run_reconcilers`.
- Produces:
  - `CATCH_UP_AFTER: Final = timedelta(minutes=5)`
  - `compute_due_slots(anchor_utc: datetime, interval_s: int, last_evaluated: datetime | None, now: datetime) -> list[datetime]`
  - `@dataclass(frozen=True) class TickReport`: `missed: int`, `queued_run_id: int | None`, `skipped_overlap: bool`, `cursor: datetime | None`
  - `async def run_tick(conn: AsyncConnection, *, now: datetime, defer: Deferrer) -> TickReport`
  - The `tick` task body: `release_stalled_jobs`, then `run_reconcilers(ctx, "tick")`, then `run_tick` in its own transaction

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_slots.py
from datetime import UTC, datetime, timedelta
from jobhunter.worker.tick import compute_due_slots
A = datetime(2026, 10, 16, 4, tzinfo=UTC); H6 = 21600
def test_due_slots_half_open_interval() -> None:
    assert compute_due_slots(A, H6, A, A + timedelta(hours=12)) == [A + timedelta(hours=6), A + timedelta(hours=12)]
    assert compute_due_slots(A, H6, A, A + timedelta(hours=5, minutes=59)) == []
def test_slots_with_clock_behind_cursor_are_empty() -> None:    # Review Focus 5 (pure part)
    assert compute_due_slots(A, H6, A + timedelta(hours=12), A + timedelta(hours=1)) == []
def test_dst_never_shifts_utc_slots() -> None:
    a = datetime(2026, 10, 24, 4, tzinfo=UTC)                    # 06:00 CEST; DST ends 2026-10-25
    assert compute_due_slots(a, 86400, a, a + timedelta(days=3)) == [a + timedelta(days=d) for d in (1, 2, 3)]
def test_first_evaluation_without_cursor_returns_only_latest() -> None:
    assert compute_due_slots(A, H6, None, A + timedelta(hours=13)) == [A + timedelta(hours=12)]
```

```python
# tests/property/test_slots_property.py
from datetime import UTC, datetime, timedelta
from hypothesis import given, strategies as st
from jobhunter.worker.tick import compute_due_slots
A = datetime(2026, 1, 1, tzinfo=UTC)
@given(st.integers(3600, 604800), st.lists(st.integers(0, 10**7), min_size=1, max_size=40))
def test_every_slot_exactly_once_in_order(interval: int, offsets: list[int]) -> None:
    cursor, seen = A, []
    for t in sorted(offsets):
        due = compute_due_slots(A, interval, cursor, A + timedelta(seconds=t))
        seen += due; cursor = due[-1] if due else cursor
    expected = [A + timedelta(seconds=k * interval) for k in range(1, max(offsets) // interval + 1)]
    assert seen == expected
```

```python
# tests/integration/test_tick.py
from datetime import UTC, datetime, timedelta
import pytest, sqlalchemy as sa
from jobhunter.worker.tick import run_tick
from jobhunter.worker.tasks import defer_in_tx, live_job_exists
pytestmark = pytest.mark.integration
A = datetime(2026, 10, 16, 4, tzinfo=UTC)

async def schedule(engine, *, interval: int, cursor: datetime, enabled: bool = True) -> None:
    async with engine.begin() as c:
        await c.execute(sa.text("UPDATE schedule_state SET anchor_utc=:a, interval_s=:i, last_evaluated_slot=:c, enabled=:e"),
                        {"a": A, "i": interval, "c": cursor, "e": enabled})

async def runs(engine) -> list[tuple[datetime, str, str]]:
    async with engine.connect() as c:
        return [tuple(r) for r in await c.execute(sa.text("SELECT scheduled_slot, status, trigger FROM runs ORDER BY scheduled_slot, id"))]

async def tick(engine, now: datetime):
    async with engine.begin() as c: return await run_tick(c, now=now, defer=defer_in_tx)

async def test_due_slot_queues_one_scheduled_run(engine, queue_app) -> None:
    await schedule(engine, interval=21600, cursor=A)
    rep = await tick(engine, A + timedelta(hours=6, minutes=1))
    assert rep.missed == 0 and await runs(engine) == [(A + timedelta(hours=6), "queued", "schedule")]
    async with engine.connect() as c:
        assert await live_job_exists(c, "run_collect", args_match={"run_id": rep.queued_run_id, "attempt_no": 1})
        assert (await c.execute(sa.text("SELECT r.settings_version_id = a.settings_version_id AND r.scope_version_id = a.scope_version_id "
                                        "FROM runs r, active_config a"))).scalar_one() is True
    assert (await tick(engine, A + timedelta(hours=6, minutes=2))).queued_run_id is None and len(await runs(engine)) == 1

async def test_overlap_records_skipped_overlap(engine, queue_app) -> None:
    await schedule(engine, interval=21600, cursor=A)
    await tick(engine, A + timedelta(hours=6)); rep = await tick(engine, A + timedelta(hours=12))
    assert rep.skipped_overlap and await runs(engine) == [(A + timedelta(hours=6), "queued", "schedule"),
                                                         (A + timedelta(hours=12), "skipped_overlap", "schedule")]

async def test_downtime_records_missed_and_catch_up(engine, queue_app) -> None:
    await schedule(engine, interval=21600, cursor=A)
    assert (await tick(engine, A + timedelta(hours=18, minutes=30))).missed == 2
    assert await runs(engine) == [(A + timedelta(hours=6), "missed", "schedule"), (A + timedelta(hours=12), "missed", "schedule"),
                                  (A + timedelta(hours=18), "queued", "catch_up")]

async def test_long_downtime_records_every_slot_once(engine, queue_app) -> None:     # Review Focus 2
    await schedule(engine, interval=3600, cursor=A)
    rep = await tick(engine, A + timedelta(days=9, minutes=30))
    expected = [(A + timedelta(hours=h), "missed", "schedule") for h in range(1, 216)] + [(A + timedelta(hours=216), "queued", "catch_up")]
    assert rep.missed == 215 and await runs(engine) == expected
    assert (await tick(engine, A + timedelta(days=9, minutes=31))).missed == 0 and await runs(engine) == expected

async def test_tick_with_clock_behind_cursor_is_noop(engine, queue_app) -> None:      # Review Focus 5
    await schedule(engine, interval=21600, cursor=A + timedelta(hours=12))
    rep = await tick(engine, A + timedelta(hours=1))
    assert rep.cursor == A + timedelta(hours=12) and await runs(engine) == []
    assert (await tick(engine, A + timedelta(hours=18))).queued_run_id is not None

async def test_disabled_schedule_moves_cursor_without_rows(engine, queue_app) -> None:
    await schedule(engine, interval=21600, cursor=A, enabled=False)
    rep = await tick(engine, A + timedelta(days=2))
    assert await runs(engine) == [] and rep.cursor == A + timedelta(days=2)
```

```python
# tests/perf/test_tick_perf.py   (marker perf: deselected by default; records a measurement, not a correctness gate)
import time
from datetime import UTC, datetime, timedelta
import pytest, sqlalchemy as sa
from jobhunter.worker.tick import run_tick
from jobhunter.worker.tasks import defer_in_tx
pytestmark = [pytest.mark.perf, pytest.mark.integration]
async def test_measure_long_downtime_tick(engine, queue_app, record_property) -> None:
    a = datetime(2026, 1, 1, tzinfo=UTC)
    async with engine.begin() as c:
        await c.execute(sa.text("UPDATE schedule_state SET anchor_utc=:a, interval_s=3600, last_evaluated_slot=:a"), {"a": a})
    t0 = time.perf_counter()
    async with engine.begin() as c: await run_tick(c, now=a + timedelta(days=30), defer=defer_in_tx)
    record_property("tick_720_slots_seconds", time.perf_counter() - t0)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_slots.py tests/property/test_slots_property.py tests/integration/test_tick.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'jobhunter.worker.tick'`

- [ ] **Step 3: Implement `compute_due_slots` and `run_tick`**

**`compute_due_slots`**: with `k(t) = floor((t − anchor) / interval)` in whole seconds (floor division, so negative values work), return `anchor + k·interval` for every `k ∈ (k(last), k(now)]`. When `last` is `None`, return only the latest slot `≤ now`. When `now < last`, return `[]`.

**`run_tick`** (lock order: `active_config` first):
1. `SELECT settings_version_id, scope_version_id FROM active_config WHERE id = 1 FOR SHARE`, then `schedule_state … FOR UPDATE`.
2. When `enabled = false`: `last_evaluated_slot := max(cursor, latest slot ≤ now)`; return.
3. `include_paid` comes from the active `settings_versions.payload->'schedule'->>'include_paid'`.
4. Every slot but the latest is inserted in one statement, `INSERT … SELECT unnest(CAST(:slots AS timestamptz[]))`, as `runs(status='missed', trigger='schedule')` with `ON CONFLICT (kind, scheduled_slot) WHERE scheduled_slot IS NOT NULL DO NOTHING`.
5. The latest slot `s` is inserted inside `async with conn.begin_nested()` as `status='queued'` with `trigger='schedule'` if `now − s ≤ 5 min`, else `'catch_up'`. There is no pre-check; the unique indexes decide. The `sqlalchemy.exc.IntegrityError` is caught **outside** the savepoint block, where the savepoint has already rolled back and the transaction is still usable, and is classified with `is_unique_violation`:
   - `runs_one_active_collect` → log `active_collect_conflict` with `path="tick"` and `sqlstate`, then insert `skipped_overlap` (same slot and trigger) in a new savepoint;
   - `runs_slot_uq` → no-op;
   - anything else re-raises.

   `test_overlap_records_skipped_overlap` (sequential) and Task 17's `test_tick_blocked_behind_run_now_records_overlap` (blocked on the index) both take this branch.
6. For a queued run: `defer(conn, "run_collect", lock="collect", queueing_lock=f"run:{id}:1", run_id=id, attempt_no=1)`.
7. `last_evaluated_slot := max(cursor, s)`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_slots.py tests/property/test_slots_property.py tests/integration/test_tick.py -q`
Expected: PASS (11 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/src/jobhunter/worker tests
git commit -m "feat(m0): UTC slot tick under active_config with missed, skipped_overlap and catch_up outcomes"
```

---

### Task 12: Collection run lifecycle, admission, no-op graph and checkpointer (V6)

**Files:**
- Create: `backend/src/jobhunter/worker/runs.py`, `backend/src/jobhunter/graphs/collect/__init__.py`, `backend/src/jobhunter/graphs/collect/graph.py`, `docs/spikes/V6.md`
- Modify: `backend/src/jobhunter/worker/tasks.py` (the `run_collect` body)
- Test: `tests/integration/test_checkpointer_v6.py`, `tests/integration/test_runs.py`

**Interfaces:**
- Consumes: `transition`, `raise_alert`, `resolve_alert`, `Deferrer`, `register_takeover_sweep`, `reconciler`, `heartbeat_loop`, `EXECUTIONS`, `open_saver`, `metrics.RUNS`, `metrics.RUN_DURATION`, `FencedOut`.
- Produces:
  - `graph.CollectState(TypedDict)`: the full graph state, `run_id: int`, `attempt_no: int`, `steps: Annotated[list[str], operator.add]`
  - `graph.CollectUpdate(TypedDict, total=False)`: the partial update a node returns, with the same keys and types. Nodes are typed `async def node(state: CollectState) -> CollectUpdate`, following LangGraph's partial-update convention, so a node that returns only `steps` passes `mypy --strict`
  - `graph.build_noop_collect_graph(checkpointer: BaseCheckpointSaver[str]) -> CompiledStateGraph` (nodes `start` → `finalize`; `finalize` awaits `afault_point("collect.before_finalize")`)
  - `runs.thread_id(run_id: int, attempt_no: int) -> str` (`f"run-{run_id}-a{attempt_no}"`)
  - `@dataclass(frozen=True) class RunOwner`: `run_id: int`, `attempt_no: int`
  - `@dataclass(frozen=True) class RunRow`: `id: int`, `kind: str`, `trigger: str`, `status: str`, `scheduled_slot: datetime | None`, `include_paid: bool`, `attempt_no: int`, `queued_at: datetime`, `started_at: datetime | None`, `finished_at: datetime | None`, `status_reason: str | None`
  - `Admission = Literal["started", "resumed", "duplicate", "superseded", "foreign_epoch", "finished"]`
  - `async def start_attempt(conn, run_id: int, attempt_no: int, *, epoch: int) -> tuple[Admission, RunOwner | None]`
  - `async def heartbeat_run(conn, owner: RunOwner) -> bool`
  - `async def finalize_run(conn, owner: RunOwner, *, status: Literal["succeeded", "partial", "failed_invariant", "failed"], reason: str | None, report: Mapping[str, JSONValue]) -> None` (raises `FencedOut`)
  - `async def begin_new_attempt(conn, run_id: int, *, epoch: int, end_reason: Literal["worker_takeover", "heartbeat_stale"], defer: Deferrer) -> int | None`
  - `async def request_manual_run(conn, *, include_paid: bool, defer: Deferrer) -> tuple[RunRow, bool]`
  - `async def execute_run(engine: AsyncEngine, graph: CompiledStateGraph, run_id: int, attempt_no: int, *, epoch: int, heartbeat_period_s: float = 30.0) -> Admission`
  - Takeover sweep `runs`; reconcilers `stale_runs`, `queued_runs` and `stale_data` (cadence `tick`). `stale_runs` calls `EXECUTIONS.cancel_and_await(("run", id))` after each new attempt it creates.
  - `RUN_STALE: Final = timedelta(minutes=3)`; `QUEUED_REDEFER_AFTER: Final = timedelta(minutes=2)`; `MAX_RESUMES: Final = 3`

**Admission protocol** (§3.3.3; one rule for every delivery of `run_collect(run_id, n)`):
1. **Local check first.** `EXECUTIONS.get(("run", id))`:
   - a live task with token `n` means **`duplicate`**: return without writes;
   - a live task with an older token means the old local graph is cancelled and awaited (`cancel_and_await`) before step 2.
2. **DB check** under `SELECT … FROM runs WHERE id = :i FOR UPDATE`:
   - **`started`**: `status = 'queued'` and `attempt_no = n − 1`. Transition `queued→running`, insert `run_attempts(n, epoch)`.
   - **`resumed`**: `status = 'running'`, `attempt_no = n`, and `run_attempts[n]` has `worker_epoch = :epoch` and `ended_at IS NULL`. This is an attempt that this epoch's sweep or reconciler created.
   - **`superseded`**: `attempt_no ≠ n`.
   - **`foreign_epoch`**: `running` with `attempt_no = n` but owned by another epoch.
   - **`finished`**: a terminal status.

   Only `started` and `resumed` execute.
3. The graph runs as `EXECUTIONS.start(("run", id), n, …)`, next to a heartbeat task whose `on_lost` cancels it.

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_checkpointer_v6.py
import operator
from typing import Annotated, TypedDict
import pytest
from langgraph.graph import END, START, StateGraph
from jobhunter.graphs.checkpointer import open_saver
pytestmark = pytest.mark.integration
class S(TypedDict): steps: Annotated[list[str], operator.add]

async def test_v6_resume_reruns_only_unfinished_node_and_thread_delete(migrated) -> None:
    calls = {"a": 0, "b": 0}
    async def a(s: S) -> S: calls["a"] += 1; return {"steps": ["a"]}
    async def b(s: S) -> S:
        calls["b"] += 1
        if calls["b"] == 1: raise RuntimeError("crash in b")
        return {"steps": ["b"]}
    g = StateGraph(S); g.add_node("a", a); g.add_node("b", b)
    g.add_edge(START, "a"); g.add_edge("a", "b"); g.add_edge("b", END)
    async with open_saver(migrated.dsn("app")) as saver:
        app = g.compile(checkpointer=saver); cfg = {"configurable": {"thread_id": "v6-run-1-a1"}}
        with pytest.raises(RuntimeError): await app.ainvoke({"steps": []}, cfg, durability="sync")
        out = await app.ainvoke(None, cfg, durability="sync")
        assert calls == {"a": 1, "b": 2} and out["steps"] == ["a", "b"]
        await app.ainvoke({"steps": []}, {"configurable": {"thread_id": "v6-run-2-a1"}}, durability="sync")
        await saver.adelete_thread("v6-run-1-a1")
        assert await saver.aget_tuple({"configurable": {"thread_id": "v6-run-1-a1"}}) is None
        assert await saver.aget_tuple({"configurable": {"thread_id": "v6-run-2-a1"}}) is not None
```

```python
# tests/integration/test_runs.py
import asyncio, uuid
from datetime import timedelta
import pytest, sqlalchemy as sa
from langgraph.graph import END, START, StateGraph
from jobhunter.clock import utcnow
from jobhunter.errors import FencedOut
from jobhunter.graphs.checkpointer import open_saver
from jobhunter.graphs.collect.graph import CollectState, CollectUpdate, build_noop_collect_graph
from jobhunter.worker import runs
from jobhunter.worker.epoch import WorkerIdentity, claim_epoch_and_sweep
from jobhunter.worker.executions import EXECUTIONS
from jobhunter.worker.reconcilers import ReconcileContext, run_reconcilers
from jobhunter.worker.tasks import defer_in_tx, live_job_exists
pytestmark = pytest.mark.integration

def gated_graph(gate: asyncio.Event, entered: asyncio.Event):
    async def wait(s: CollectState) -> CollectUpdate: entered.set(); await gate.wait(); return {"steps": ["wait"]}
    g = StateGraph(CollectState); g.add_node("wait", wait); g.add_edge(START, "wait"); g.add_edge("wait", END)
    return g.compile()

async def queued(engine) -> int:
    async with engine.begin() as c:
        row, created = await runs.request_manual_run(c, include_paid=False, defer=defer_in_tx)
        assert created; return row.id

async def fetch(engine, rid: int):
    async with engine.connect() as c:
        return tuple((await c.execute(sa.text("SELECT status, attempt_no, resume_count, current_thread_id, report, status_reason "
                                              "FROM runs WHERE id=:i"), {"i": rid})).one())

async def test_happy_path_noop_run(engine, queue_app, migrated) -> None:
    rid = await queued(engine)
    async with open_saver(migrated.dsn("app")) as saver:
        assert await runs.execute_run(engine, build_noop_collect_graph(saver), rid, 1, epoch=1) == "started"
        assert await saver.aget_tuple({"configurable": {"thread_id": f"run-{rid}-a1"}}) is not None
    assert (await fetch(engine, rid))[:5] == ("succeeded", 1, 0, f"run-{rid}-a1", {"noop": True})
    async with engine.connect() as c:
        assert tuple((await c.execute(sa.text("SELECT worker_epoch, end_reason FROM run_attempts WHERE run_id=:i"), {"i": rid})).one()) == (1, "completed")

async def test_duplicate_delivery_while_executing_is_noop(engine, queue_app) -> None:
    rid, gate, entered = await queued(engine), asyncio.Event(), asyncio.Event()
    first = asyncio.create_task(runs.execute_run(engine, gated_graph(gate, entered), rid, 1, epoch=1))
    await entered.wait()
    assert await runs.execute_run(engine, gated_graph(gate, entered), rid, 1, epoch=1) == "duplicate"
    gate.set(); assert await first == "started"
    assert await runs.execute_run(engine, gated_graph(gate, entered), rid, 1, epoch=1) == "finished"

async def test_superseded_attempt_job_is_noop(engine, queue_app) -> None:
    rid = await queued(engine)
    async with engine.begin() as c: assert (await runs.start_attempt(c, rid, 1, epoch=1))[0] == "started"
    async with engine.begin() as c:
        assert await runs.begin_new_attempt(c, rid, epoch=1, end_reason="heartbeat_stale", defer=defer_in_tx) == 2
    async with engine.begin() as c: assert (await runs.start_attempt(c, rid, 1, epoch=1)) == ("superseded", None)
    assert (await fetch(engine, rid))[:4] == ("running", 2, 1, f"run-{rid}-a2")

async def test_sweep_created_attempt_is_admitted_only_for_its_epoch(engine, queue_app, migrated) -> None:
    rid = await queued(engine)
    e1, _ = await claim_epoch_and_sweep(engine, WorkerIdentity(uuid.uuid4(), "h", 1, 1, "v"))
    async with engine.begin() as c: await runs.start_attempt(c, rid, 1, epoch=e1)
    e2, counts = await claim_epoch_and_sweep(engine, WorkerIdentity(uuid.uuid4(), "h", 2, 2, "v"))
    assert counts["runs"] == 1
    async with engine.connect() as c:
        assert await live_job_exists(c, "run_collect", args_match={"run_id": rid, "attempt_no": 2})
    async with engine.begin() as c: assert (await runs.start_attempt(c, rid, 2, epoch=e1))[0] == "foreign_epoch"
    async with open_saver(migrated.dsn("app")) as saver:
        assert await runs.execute_run(engine, build_noop_collect_graph(saver), rid, 2, epoch=e2) == "resumed"
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT end_reason FROM run_attempts WHERE run_id=:i AND attempt_no=1"), {"i": rid})).scalar_one() == "worker_takeover"
    assert (await fetch(engine, rid))[:2] == ("succeeded", 2)

async def test_stale_recovery_cancels_live_old_graph_before_replacement(engine, queue_app, migrated) -> None:
    rid, gate, entered = await queued(engine), asyncio.Event(), asyncio.Event()
    old = asyncio.create_task(runs.execute_run(engine, gated_graph(gate, entered), rid, 1, epoch=1, heartbeat_period_s=3600))
    await entered.wait()
    async with engine.begin() as c: await c.execute(sa.text("UPDATE runs SET heartbeat_at = now() - interval '4 minutes' WHERE id=:i"), {"i": rid})
    await run_reconcilers(ReconcileContext(engine=engine, queue=queue_app, defer=defer_in_tx, now=utcnow(), epoch=1), "tick")
    await asyncio.wait_for(old, 5)
    assert EXECUTIONS.get(("run", rid)) is None and (await fetch(engine, rid))[:2] == ("running", 2)
    async with open_saver(migrated.dsn("app")) as saver:
        assert await runs.execute_run(engine, build_noop_collect_graph(saver), rid, 2, epoch=1) == "resumed"
    assert (await fetch(engine, rid))[:2] == ("succeeded", 2)

async def test_old_owner_is_fenced(engine, queue_app) -> None:
    rid = await queued(engine)
    async with engine.begin() as c: _, old = await runs.start_attempt(c, rid, 1, epoch=1)
    async with engine.begin() as c: await runs.begin_new_attempt(c, rid, epoch=1, end_reason="heartbeat_stale", defer=defer_in_tx)
    assert old is not None
    async with engine.begin() as c:
        with pytest.raises(FencedOut): await runs.finalize_run(c, old, status="succeeded", reason=None, report={})
        assert await runs.heartbeat_run(c, old) is False

async def test_resume_exhausted_after_three_resumes(engine, queue_app) -> None:
    rid = await queued(engine)
    async with engine.begin() as c: await runs.start_attempt(c, rid, 1, epoch=1)
    for expected in (2, 3, 4, None):
        async with engine.begin() as c:
            assert await runs.begin_new_attempt(c, rid, epoch=1, end_reason="heartbeat_stale", defer=defer_in_tx) == expected
    assert (await fetch(engine, rid))[0::5] == ("failed", "resume_exhausted") and (await fetch(engine, rid))[2] == 4
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT severity FROM alerts WHERE code='run.resume_exhausted'"))).scalar_one() == "critical"

async def test_queued_runs_reconciler_redefers_once_then_fails(engine, queue_app) -> None:
    rid = await queued(engine)
    ctx = lambda m: ReconcileContext(engine=engine, queue=queue_app, defer=defer_in_tx, now=utcnow() + timedelta(minutes=m), epoch=1)
    async with engine.begin() as c: await c.execute(sa.text("DELETE FROM procrastinate_jobs"))
    await run_reconcilers(ctx(2.1), "tick"); assert (await fetch(engine, rid))[5] == "redeferred"
    async with engine.begin() as c: await c.execute(sa.text("DELETE FROM procrastinate_jobs"))
    await run_reconcilers(ctx(4.2), "tick"); assert (await fetch(engine, rid))[0::5] == ("failed", "orphaned_queue")

async def test_stale_data_alert_after_grace_and_two_intervals(engine, queue_app) -> None:
    ctx = lambda h: ReconcileContext(engine=engine, queue=queue_app, defer=defer_in_tx, now=utcnow() + timedelta(hours=h), epoch=1)
    await run_reconcilers(ctx(11), "tick")                                 # 6 h interval: within the 12 h grace
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT count(*) FROM alerts WHERE code='schedule.stale_data'"))).scalar_one() == 0
    await run_reconcilers(ctx(13), "tick")
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT severity FROM alerts WHERE code='schedule.stale_data' AND resolved_at IS NULL"))).scalar_one() == "critical"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/integration/test_checkpointer_v6.py tests/integration/test_runs.py -q`
Expected: FAIL with `ImportError: cannot import name 'runs' from 'jobhunter.worker'` (V6 may already pass, since it exercises the library and `open_saver` only).

- [ ] **Step 3: Implement `runs.py`, the no-op graph and `execute_run`**

- **`start_attempt`** implements the DB part of the admission protocol and returns `(admission, owner)`.
  - `started` uses `set_={"attempt_no": n, "heartbeat_at": now(), "current_thread_id": thread_id(id, n)}` and inserts `run_attempts(run_id, n, epoch, thread_id)`.
- **`heartbeat_run`**: `UPDATE runs SET heartbeat_at = now() WHERE id = :i AND attempt_no = :a AND status = 'running'`.
- **`finalize_run`**: `transition(conn, "runs", id, ["running"], status, reason=reason, owner={"attempt_no": a}, set_={"report": report})`. A `TransitionConflict` maps to `FencedOut("run")`. It then sets `run_attempts.ended_at = now(), end_reason = 'completed'`, increments `RUNS.labels(status)` and observes `RUN_DURATION`.
- **`begin_new_attempt`**: row `FOR UPDATE`; when the run is not `running`, return `None`. `next = resume_count + 1`.
  - When `next > MAX_RESUMES`: `running→failed` with `reason='resume_exhausted'`, `set_={"resume_count": next}`, plus `raise_alert(code="run.resume_exhausted", severity="critical", subject=f"run:{id}", message="run resumes exhausted")`; return `None`.
  - Otherwise: `running→running` with `set_={"attempt_no": a + 1, "resume_count": next, "current_thread_id": …, "heartbeat_at": now()}`; end the old `run_attempts` row (`ended_at`, `end_reason`); insert the new row with `epoch`; `defer(conn, "run_collect", lock="collect", queueing_lock=f"run:{id}:{a + 1}", run_id=id, attempt_no=a + 1)`; return `a + 1`.
- **Sweep `runs`**: every `running` run whose current `run_attempts.worker_epoch < :epoch` → `begin_new_attempt(..., end_reason="worker_takeover")`.
- **Reconciler `stale_runs`**: `running` with `heartbeat_at < now − 3 min` → `begin_new_attempt(..., end_reason="heartbeat_stale")`. After the transaction commits, `EXECUTIONS.cancel_and_await(("run", id))`, so the old local graph releases the procrastinate `collect` lock before the replacement can run.
- **Reconciler `queued_runs`**: a `queued` run without a live job, with `now − queued_at ≥ 2 min`.
  - When `status_reason` is NULL: re-defer and set `status_reason = 'redeferred'` (no status change).
  - When it is `'redeferred'` and `now − queued_at ≥ 4 min`: `queued→failed('orphaned_queue')` plus `raise_alert(code="run.orphaned_queue", severity="warn", subject=f"run:{id}", message="queued run orphaned")`.
- **Reconciler `stale_data`** (§17.5, §17.6):
  - `age = now − max(finished_at)` over runs `succeeded` or `partial` (or `now − first settings version created_at` when there is none);
  - `grace_until = first settings_versions.created_at + 2 × interval_s`;
  - when `now > grace_until` and `age > 2 × interval_s`: `raise_alert(code="schedule.stale_data", severity="critical", subject="collect", message="no successful run within twice the interval")`; otherwise `resolve_alert`.
- **`request_manual_run`**:
  - `active_config FOR SHARE` (settings and scope ids);
  - return the `queued` or `running` collection as `(row, False)` when one exists;
  - else insert `runs(trigger='manual', scheduled_slot=NULL, status='queued', include_paid, settings_version_id, scope_version_id)` inside `async with conn.begin_nested()` and defer the task;
  - a `sqlalchemy.exc.IntegrityError` caught outside that block with `is_unique_violation(exc, "runs_one_active_collect")` logs `active_collect_conflict` with `path="manual"` and `sqlstate`, re-reads the active run and returns `(row, False)`; anything else re-raises. Task 17's `test_run_now_blocked_behind_tick_returns_tick_run` takes this branch, because the pre-check cannot see the tick's uncommitted run.
- **The no-op graph**: nodes `start` and `finalize` are `async def …(state: CollectState) -> CollectUpdate` and return `{"steps": ["start"]}` and `{"steps": ["finalize"]}`.
- **`execute_run`**: the admission protocol, then `ainvoke(…, {"configurable": {"thread_id": …}, "recursion_limit": 200}, durability="sync")`.
  - On completion: `finalize_run(status="succeeded", reason=None, report={"noop": True})` in its own `engine.begin()` block.
  - `CancelledError` or `FencedOut` → return quietly, with no owner writes.
  - Any other exception → in a **new** transaction, `finalize_run(status="failed", reason="bug", report={})` and then `raise_alert(code="run.failed", severity="critical", subject=f"run:{id}", message="run failed")`.
  - A `FencedOut` from either finalize is caught **outside** its `async with` block. The fenced transaction wrote nothing (the `transition()` is its first write), and the run's new owner records the outcome.
  - The heartbeat task is always cancelled and awaited.
- **`V6.md`** records `test_checkpointer_v6.py` and `test_checkpointer_setup_is_idempotent` (Task 6).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/integration/test_checkpointer_v6.py tests/integration/test_runs.py -q`
Expected: PASS (10 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/src/jobhunter docs/spikes/V6.md tests
git commit -m "feat(m0): run admission protocol, cancel-and-await replacement, fenced finalize, run reconcilers, PostgresSaver (V6)"
```

---

### Task 13: Worker process: composition, shutdown coordinator, retention, crash cases

**Files:**
- Create: `backend/src/jobhunter/worker/main.py`, `backend/src/jobhunter/worker/shutdown.py`, `backend/src/jobhunter/privacy/__init__.py`, `backend/src/jobhunter/privacy/retention.py`
- Modify: `backend/src/jobhunter/worker/tasks.py` (the `retention_daily` and `reconcile_heavy` bodies; `WorkerRuntime` in the job context), `backend/src/jobhunter/cli.py` (`worker`)
- Test: `tests/integration/test_retention.py`, `tests/crash/test_worker_process.py`

**Interfaces:**
- Consumes: everything from Tasks 7–12.
- Produces:
  - `@dataclass class WorkerRuntime`: `cfg: AppConfig`, `engine: AsyncEngine`, `queue: procrastinate.App`, `saver: AsyncPostgresSaver`, `collect_graph: CompiledStateGraph`, `epoch: int`, `shutdown: ShutdownState`. Tasks read it from `context.additional_context["runtime"]`.
  - `@dataclass class ShutdownState`: `admission_stopped: bool`, `requested: asyncio.Event`
  - `GRACE_S: Final = 100`
  - `async def shutdown.coordinate(worker_tasks: Sequence[asyncio.Task[None]], state: ShutdownState) -> int` (0 after a requested shutdown, 1 when a worker loop ended by itself)
  - `async def worker_main(cfg: AppConfig) -> int`
  - `@dataclass(frozen=True) class RetentionRule`: `name: str`, `window: timedelta`; `RULES: Final[tuple[RetentionRule, ...]]`
  - `async def run_retention_daily(engine: AsyncEngine, queue: procrastinate.App, saver: AsyncPostgresSaver, *, now: datetime) -> dict[str, int]`, returning deleted counts keyed `api_idempotency`, `sessions`, `login_attempts`, `alerts`, `operations`, `runs`, `procrastinate_jobs` (−1, because procrastinate returns no count) and `checkpoints` (threads deleted)
  - Heavy reconciler `metrics_refresh`: `refresh_alert_gauge`, plus `TABLE_BYTES` from `pg_total_relation_size` for each public table
  - CLI `jobhunter worker`

Retention windows (§20.1):
- `api_idempotency`: `expires_at < now`;
- `sessions`: absolute or idle expiry before `now − 1 day`;
- `login_attempts`: older than 30 days;
- `alerts`: resolved more than 90 days ago;
- `operations`: finished more than 30 days ago;
- `runs` with their `run_attempts`: terminal and finished, or `missed`/`skipped_overlap` and queued, more than 13 months ago;
- procrastinate finished jobs: older than 7 days;
- LangGraph threads (`saver.adelete_thread`): those of runs finished more than 14 days ago.

`worker_main` order (§3.3.1, §3.3.5; review findings #2 and #3):
1. `configure_logging(cfg.log_level)`.
2. `LockSession.connect` and `acquire_lock` via `asyncio.to_thread`.
3. `make_engine(cfg, pool_size=10)`.
4. `queue = make_queue_app(cfg.dsn("psycopg"), max_size=4)` and `import jobhunter.worker.operations, jobhunter.worker.runs`, so the task app, the takeover sweeps, the reconcilers and the handlers exist **before** takeover.
5. `claim_epoch_and_sweep`. Its sweeps defer through the sweep transaction's own connection. Then log `startup_check_passed` with `check="advisory_lock_acquired"`.
6. `Watchdog(...).start()`.
7. `await queue.open_async()`, `open_saver(...)`, `build_noop_collect_graph(saver)`, `WorkerRuntime`.
8. `run_reconcilers(ctx, "start")`; outside the test env, `start_metrics_server(9100)`.
9. SIGTERM and SIGINT handlers (`loop.add_signal_handler`) set `state.requested`.
10. Create one task per entry of `QUEUES`: `queue.run_worker_async(queues=[q], concurrency=n, install_signal_handlers=False, shutdown_graceful_timeout=GRACE_S, additional_context={"runtime": runtime}, name=f"jh-{q}")`. Then log `worker_ready` with `instance_id` and `epoch`.
11. `code = await coordinate(tasks, state)`.
12. Teardown: `await EXECUTIONS.cancel_all()`; `watchdog.stop()`; close the saver, `await queue.close_async()`, `await engine.dispose()`; `lock_session.close()` (this releases the lock); log `worker_shutdown_complete`; return `code`.

`coordinate`:
1. `await asyncio.wait({*worker_tasks, requested_task}, return_when=FIRST_COMPLETED)`.
2. Set `admission_stopped = True`.
3. Cancel every worker task. procrastinate stops fetching, waits up to `GRACE_S` for running jobs, then cancels them.
4. `await asyncio.wait(worker_tasks, timeout=GRACE_S + 10)`.
5. Return 0 if shutdown was requested, else 1 (logged `worker_loop_ended`).

No state is rewritten on shutdown (§3.3.5).

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_retention.py
from datetime import timedelta
import pytest, sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.graphs.checkpointer import open_saver
from jobhunter.privacy.retention import run_retention_daily
pytestmark = pytest.mark.integration
async def test_m0_rules_delete_only_expired_rows(engine, queue_app, migrated) -> None:
    now = utcnow()
    async with engine.begin() as c:
        for ago in (31, 29):
            await c.execute(sa.text("INSERT INTO login_attempts (at, ip, username_hmac, success) VALUES (:t, '198.51.100.1', '\\x00', false)"),
                            {"t": now - timedelta(days=ago)})
        await c.execute(sa.text("INSERT INTO alerts (code,severity,dedup_key,message,resolved_at) VALUES ('a','info','k1','m',:r), ('a','info','k2','m',:s)"),
                        {"r": now - timedelta(days=91), "s": now - timedelta(days=89)})
        await c.execute(sa.text("INSERT INTO api_idempotency (route,idem_key,request_sha256,status,expires_at) "
                                "VALUES ('r','aaaaaaaaaaaaaaaa','\\x00','in_progress',:e), ('r','bbbbbbbbbbbbbbbb','\\x00','in_progress',:f)"),
                        {"e": now - timedelta(seconds=1), "f": now + timedelta(days=1)})
    async with open_saver(migrated.dsn("app")) as saver:
        report = await run_retention_daily(engine, queue_app, saver, now=now)
    assert (report["login_attempts"], report["alerts"], report["api_idempotency"]) == (1, 1, 1)
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT count(*) FROM login_attempts"))).scalar_one() == 1
        assert (await c.execute(sa.text("SELECT dedup_key FROM alerts"))).scalar_one() == "k2"
```

```python
# tests/crash/test_worker_process.py
import signal, sys
import psycopg, pytest
from tests.support.procs import wait_until
pytestmark = [pytest.mark.crash, pytest.mark.slow]
WORKER = [sys.executable, "-m", "jobhunter.cli", "worker"]

def one(server, statement):
    with psycopg.connect(server.dsn("superuser"), autocommit=True) as c:
        cur = c.execute(statement); return cur.fetchone() if cur.description else None

def due_slot_now(server) -> None:
    one(server, "UPDATE schedule_state SET anchor_utc = date_trunc('minute', now()), interval_s = 3600, "
                "last_evaluated_slot = date_trunc('minute', now()) - interval '1 hour'")

def test_idle_sigterm_exits_zero_and_releases_lock(migrated, proc_env, procs) -> None:
    w = procs.spawn(WORKER, proc_env("worker", migrated)); w.wait_event("worker_ready", 60)
    w.signal(signal.SIGTERM); assert w.exit_within(30) == 0
    w.wait_event("worker_shutdown_complete", 1)
    assert one(migrated, "SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND objid=7261001") == (0,)

def test_busy_sigterm_drains_inflight_run(migrated, proc_env, procs) -> None:
    w = procs.spawn(WORKER, proc_env("worker", migrated, JOBHUNTER_FAULT="collect.before_finalize:sleep=5"))
    w.wait_event("worker_ready", 60); due_slot_now(migrated)
    w.wait_event("fault_point_reached", 120, point="collect.before_finalize")
    w.signal(signal.SIGTERM); assert w.exit_within(60) == 0
    assert one(migrated, "SELECT status FROM runs WHERE scheduled_slot IS NOT NULL ORDER BY id DESC LIMIT 1") == ("succeeded",)

def test_c17_zombie_worker_writes_nothing(migrated, proc_env, procs) -> None:
    a = procs.spawn(WORKER, proc_env("worker", migrated, JOBHUNTER_FAULT="collect.before_finalize:pause"))
    a.wait_event("worker_ready", 60); due_slot_now(migrated)
    a.wait_event("fault_point_reached", 120, point="collect.before_finalize"); wait_until(a.is_stopped, 10)
    one(migrated, "UPDATE worker_heartbeat SET heartbeat_at = now() - interval '2 minutes'")
    one(migrated, "UPDATE procrastinate_workers SET last_heartbeat = now() - interval '5 minutes'")
    b = procs.spawn(WORKER, proc_env("worker", migrated)); b.wait_event("worker_ready", 60)
    wait_until(lambda: one(migrated, "SELECT status, attempt_no FROM runs WHERE scheduled_slot IS NOT NULL") == ("succeeded", 2), 120)
    a.signal(signal.SIGCONT); assert a.exit_within(20) == 70
    assert one(migrated, "SELECT end_reason FROM run_attempts WHERE attempt_no=1") == ("worker_takeover",)
    assert one(migrated, "SELECT status, attempt_no FROM runs WHERE scheduled_slot IS NOT NULL") == ("succeeded", 2)

def test_c27_killed_worker_job_released_and_run_resumes(migrated, proc_env, procs) -> None:
    a = procs.spawn(WORKER, proc_env("worker", migrated, JOBHUNTER_FAULT="collect.before_finalize:pause"))
    a.wait_event("worker_ready", 60); due_slot_now(migrated)
    a.wait_event("fault_point_reached", 120, point="collect.before_finalize"); wait_until(a.is_stopped, 10)
    a.signal(signal.SIGKILL); a.exit_within(10)
    one(migrated, "UPDATE procrastinate_workers SET last_heartbeat = now() - interval '5 minutes'")
    b = procs.spawn(WORKER, proc_env("worker", migrated)); b.wait_event("worker_ready", 60)
    wait_until(lambda: one(migrated, "SELECT status, attempt_no FROM runs WHERE scheduled_slot IS NOT NULL") == ("succeeded", 2), 120)
    assert one(migrated, "SELECT count(*) FROM procrastinate_jobs WHERE status='doing'") == (0,)
```

(`afault_point` logs `{"event": "fault_point_reached", "point": …}` before it acts, which gives the tests their readiness acknowledgement. Add that to `faults.py` here.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/integration/test_retention.py tests/crash/test_worker_process.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'jobhunter.privacy'`

- [ ] **Step 3: Implement `worker_main`, `coordinate`, retention and the heavy reconciler**

Follow the order and the coordinator contract above. `reconcile_heavy` runs the `heavy` reconcilers; `retention_daily` calls `run_retention_daily`; the CLI `worker` is `raise SystemExit(asyncio.run(worker_main(startup_or_exit("worker"))))`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/integration/test_retention.py tests/crash/test_worker_process.py -q`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/src/jobhunter tests
git commit -m "feat(m0): worker composition (task app before takeover), FIRST_COMPLETED shutdown coordinator, retention, C17/C27"
```

---

### Task 14: API core: app factory, edge middleware, RFC 9457, health, OpenAPI

**Files:**
- Create: `backend/src/jobhunter/api/__init__.py`, `api/app.py`, `api/edge.py`, `api/errors.py`, `api/deps.py`, `api/openapi_export.py`, `api/routes/__init__.py`, `api/routes/health.py`, `backend/src/jobhunter/observability/health.py`
- Modify: `backend/src/jobhunter/cli.py` (`http-check`), `tests/support/fixtures.py` (fixture `api_client`)
- Test: `tests/integration/test_api_core.py`

**Interfaces:**
- Consumes: `make_engine`, `code_head`, `uuid7`, `HTTP_REQUESTS`, `AppConfig`, `pg_error`, `configure_logging`.
- Produces:
  - `ProblemCode = Literal["unauthenticated", "csrf_failed", "throttled", "invalid_cursor", "stale_base_version", "validation_failed", "budget_exhausted", "filtered_requires_force", "run_active", "not_found", "conflict_state", "upload_rejected", "idempotency_key_required", "idempotency_key_reused", "idempotency_in_progress", "eval_gate_failed", "internal"]`
  - `class Problem(Exception)`: `__init__(self, status: int, code: ProblemCode, title: str, *, detail: str | None = None, errors: list[FieldError] | None = None, headers: Mapping[str, str] | None = None, extra: Mapping[str, JSONValue] | None = None)`; `@dataclass class FieldError: field: str; code: str; message: str`
  - `errors.problem_body(problem: Problem, *, instance: str, request_id: str) -> dict[str, JSONValue]` (shared by the handlers and the edge middleware)
  - `SECURITY_HEADERS: Final[Mapping[str, str]]` (§16.6 exactly)
  - `class EdgeMiddleware`: `__init__(self, app: ASGIApp, *, config: Callable[[], AppConfig | None])`. This is the outermost ASGI layer, outside Starlette's `ServerErrorMiddleware`. For each HTTP request it:
    1. assigns a `uuid7()` request id into `scope["state"]["request_id"]` and the structlog contextvars;
    2. adds `X-Request-Id` and `SECURITY_HEADERS` to every `http.response.start`;
    3. counts `HTTP_REQUESTS`;
    4. renders CSRF rejections itself (Task 16);
    5. renders a 500 `internal` problem when the inner app raises before starting a response, and only logs when a response has already started.
  - `class JobHunterApp`: `__init__(self, api: FastAPI, edge: EdgeMiddleware)`; `__call__` delegates to the edge; `api: FastAPI` is exposed for OpenAPI export
  - `create_app(*, cfg: AppConfig | None = None, engine: AsyncEngine | None = None, extra_routers: Sequence[APIRouter] = ()) -> JobHunterApp`; module-level `app = create_app()` for uvicorn
  - `deps.get_engine(request: Request) -> AsyncEngine`; `deps.get_config(request: Request) -> AppConfig`; `deps.request_id(request: Request) -> str`
  - `ReadinessCode = Literal["db", "migrations", "worker_heartbeat"]`; `@dataclass(frozen=True) class ReadinessReport: ok: bool; failing: list[ReadinessCode]`
  - `async def check_readiness(engine: AsyncEngine, *, code_head: str, now: datetime) -> ReadinessReport`
  - `openapi_export.main(argv: Sequence[str] | None = None) -> int`
  - Fixture `api_client`: `httpx.AsyncClient(transport=ASGITransport(app=create_app(cfg=make_config("api"), engine=engine), raise_app_exceptions=False, client=("198.51.100.10", 50000)), base_url="https://testserver", headers={"Origin": "https://testserver"})`
  - Fixture `log_events` (function): runs `configure_logging("INFO", stream=buf)` on a `StringIO` and yields `Callable[[], list[dict[str, Any]]]`, which returns the JSON events logged so far; the finalizer restores the default stream
  - CLI `jobhunter http-check <url>`

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_api_core.py
import json, uuid
import httpx, pytest, sqlalchemy as sa
from fastapi import APIRouter
from pydantic import BaseModel, Field
pytestmark = pytest.mark.integration
HEADERS = {"strict-transport-security": "max-age=63072000; includeSubDomains",
    "content-security-policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'",
    "x-content-type-options": "nosniff", "referrer-policy": "no-referrer",
    "permissions-policy": "camera=(), microphone=(), geolocation=(), interest-cohort=()",
    "cross-origin-opener-policy": "same-origin", "cross-origin-resource-policy": "same-origin"}

def assert_edge(r: httpx.Response) -> None:
    assert all(r.headers[k] == v for k, v in HEADERS.items()) and uuid.UUID(r.headers["x-request-id"]).version == 7
    if r.headers.get("content-type") == "application/problem+json":
        assert r.json()["request_id"] == r.headers["x-request-id"]

async def fresh_heartbeat(engine) -> None:
    async with engine.begin() as c:
        await c.execute(sa.text("INSERT INTO worker_heartbeat VALUES (1, gen_random_uuid(), 1, 'h', 1, 1, 'v', now(), now()) "
                                "ON CONFLICT (id) DO UPDATE SET heartbeat_at = now()"))

async def test_healthz(api_client) -> None:
    r = await api_client.get("/healthz"); assert r.status_code == 200 and r.json() == {"status": "ok"}; assert_edge(r)

async def test_readyz(api_client, engine) -> None:
    r = await api_client.get("/readyz"); assert r.status_code == 503 and r.json() == {"failing": ["worker_heartbeat"]}
    await fresh_heartbeat(engine); assert (await api_client.get("/readyz")).status_code == 200

async def test_readyz_reports_missing_schema(fresh_pg_server, make_config, log_events) -> None:
    from jobhunter.api.app import create_app
    from jobhunter.db.session import make_engine
    cfg = make_config("api", fresh_pg_server); engine = make_engine(cfg)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(cfg=cfg, engine=engine)), base_url="https://testserver") as c:
        r = await c.get("/readyz")
    assert r.status_code == 503 and r.json() == {"failing": ["migrations", "worker_heartbeat"]}
    # both probes saw 42P01 through SQLAlchemy; a poisoned transaction would give 25P02 for the second one
    assert [(e["probe"], e["sqlstate"]) for e in log_events() if e["event"] == "readiness_probe_failed"] == [
        ("migrations", "42P01"), ("worker_heartbeat", "42P01")]
    await engine.dispose()

async def test_problems_for_404_422_and_500_carry_edge_headers(engine, make_config) -> None:
    from jobhunter.api.app import create_app
    router = APIRouter()
    class Body(BaseModel): n: int = Field(ge=1, le=5)
    @router.post("/api/_probe")
    async def probe(b: Body) -> dict[str, int]: return {"n": b.n}
    @router.get("/api/_boom")
    async def boom() -> None: raise RuntimeError("secret detail")
    app = create_app(cfg=make_config("api"), engine=engine, extra_routers=[router])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="https://testserver",
                                 headers={"Origin": "https://testserver", "X-JH-Request": "1"}) as c:
        r = await c.get("/nope"); assert_edge(r)
        assert r.status_code == 404 and r.headers["content-type"] == "application/problem+json"
        assert r.json()["code"] == "not_found" and r.json()["type"] == "https://jobhunter.local/problems/not_found" and r.json()["instance"] == "/nope"
        r = await c.post("/api/_probe", json={"n": 9}); assert_edge(r)
        assert r.status_code == 422 and r.json()["code"] == "validation_failed" and r.json()["errors"][0]["field"] == "body.n"
        r = await c.get("/api/_boom"); assert_edge(r)
        assert r.status_code == 500 and r.json()["code"] == "internal" and "secret detail" not in r.text and "Traceback" not in r.text

def test_openapi_export(tmp_path) -> None:
    from jobhunter.api.openapi_export import main
    out = tmp_path / "openapi.json"; assert main([str(out)]) == 0
    assert json.loads(out.read_text())["openapi"].startswith("3.1")

@pytest.mark.slow
async def test_api_recovers_after_db_restart(fresh_pg_server, make_config) -> None:     # Review Focus 1
    from jobhunter.api.app import create_app
    from jobhunter.db.migrate import run_migrate_async
    from jobhunter.db.session import make_engine
    from tests.support.pg import pg_start, pg_stop
    await run_migrate_async(make_config("migrate", fresh_pg_server))
    cfg = make_config("api", fresh_pg_server); engine = make_engine(cfg); await fresh_heartbeat(engine)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(cfg=cfg, engine=engine)), base_url="https://testserver") as c:
        assert (await c.get("/readyz")).status_code == 200
        pg_stop(fresh_pg_server)
        r = await c.get("/readyz"); assert r.status_code == 503 and r.json() == {"failing": ["db"]}
        pg_start(fresh_pg_server); await fresh_heartbeat(engine)
        assert (await c.get("/readyz")).status_code == 200
    await engine.dispose()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/integration/test_api_core.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'jobhunter.api'`

- [ ] **Step 3: Implement the app factory, the edge middleware, the errors and the health checks**

- **Inner handlers** (FastAPI, `api/errors.py`, BLE001 allowed):
  - `Problem` → its JSON;
  - `RequestValidationError` → 422 `validation_failed`, with `errors[]` whose `field` joins `loc` with `.` and whose `code` is the pydantic error type;
  - Starlette 404 → `not_found`; 405 → status 405 with code `not_found`;
  - `Exception` → 500 `internal`, logged `unhandled_exception` with `error_class` only.

  All take `request_id` from `request.state.request_id` and use `problem_body`.
- **Edge middleware**: as specified in the Interfaces. It is the reason 500s and rejections still get headers: Starlette's `ServerErrorMiddleware` sits inside it.
- **`check_readiness`** uses one `engine.connect()`:
  - `SELECT 1` under `asyncio.wait_for(…, 2.0)`. A `sqlalchemy.exc.DBAPIError`, `OSError` or `TimeoutError` → `failing = ["db"]`, and nothing else is probed.
  - Each schema probe runs in its own savepoint (`async with conn.begin_nested():`). The `sqlalchemy.exc.DBAPIError` is caught outside that block, after the savepoint has rolled back, so a failed probe never aborts the transaction that the next probe uses:
    - `migrations`: `SELECT version_num FROM alembic_version`. SQLSTATE `42P01` (`UNDEFINED_TABLE`, a `ProgrammingError`) or any other SQLSTATE, an empty table, or a different version → failing.
    - `worker_heartbeat`: `SELECT now() - heartbeat_at FROM worker_heartbeat WHERE id = 1`. `42P01` or any other SQLSTATE, no row, or an age above 60 s → failing.
    - A probe error with `exc.connection_invalidated` set means that the database went away mid-check → `failing = ["db"]`.
  - Every failed probe logs `readiness_probe_failed` with `probe` and `sqlstate` (`null` when no error was raised), never the message.
  - `failing` is in the order `db`, `migrations`, `worker_heartbeat`.
- **Routes**: `/healthz` → `{"status":"ok"}`; `/readyz` → 200 `{}` or 503 `{"failing":[…]}`.
- **State**: with `cfg` and `engine` injected, `create_app` sets `api.state.cfg`, `.engine` and `.code_head` immediately (tests do not run the lifespan). Otherwise the lifespan loads `startup_or_exit("api")`, creates the engine (pool 10), computes `code_head()`, and outside the test env starts `start_metrics_server(8001)`.
- **`openapi_export.main`**: `create_app(cfg=None).api.openapi()`, dumped with `indent=2, sort_keys=True`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/integration/test_api_core.py -q`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/src/jobhunter tests
git commit -m "feat(m0): API core with outermost edge middleware, RFC 9457 problems, readiness and OpenAPI export"
```

---

### Task 15: Users, passwords, TOTP, encryption and the user CLI

**Files:**
- Create: `backend/src/jobhunter/crypto.py`, `backend/src/jobhunter/auth/__init__.py`, `auth/passwords.py`, `auth/users.py`, `auth/totp.py`, `auth/common_passwords.txt`
- Modify: `backend/src/jobhunter/cli.py` (`user create|set-password|totp enable|totp disable`)
- Test: `tests/unit/test_crypto.py`, `tests/unit/test_passwords.py`, `tests/integration/test_users.py`

**Interfaces:**
- Consumes: `AppConfig.secret_b64`, `make_engine`.
- Produces:
  - `crypto.hkdf_sha256(root: bytes, info: bytes, length: int = 32) -> bytes`; `crypto.key_id(key: bytes) -> bytes` (the first 8 bytes of `sha256(key)`); `crypto.encrypt(key: bytes, plaintext: bytes, *, aad: bytes) -> bytes` (format `b"\x01" ‖ key_id ‖ nonce(12) ‖ ciphertext+tag`); `crypto.decrypt(keys: Sequence[bytes], blob: bytes, *, aad: bytes) -> bytes` (raises `crypto.DecryptError`)
  - `passwords.ARGON2: Final = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4, hash_len=32, salt_len=16)`; `normalize_password(p: str) -> str` (NFC); `class WeakPassword(ValueError)` with `reason: Literal["too_short", "common"]`; `check_password_policy(p: str) -> None`; `hash_password(p: str) -> str`; `verify_password(stored: str, p: str) -> tuple[bool, str | None]`; `dummy_verify(p: str) -> None`
  - `users.normalize_username(u: str) -> str` (NFC, stripped); `@dataclass(frozen=True) class UserRow: username: str; password_hash: str; totp_secret_enc: bytes | None; totp_enabled: bool`; `class UserExists(Exception)`
  - `async def users.create_user(conn, username: str, password: str, *, now: datetime) -> None`; `async def users.find_user(conn, username: str) -> UserRow | None`; `async def users.set_password(conn, password: str, *, now: datetime) -> int`; `async def users.update_hash(conn, new_hash: str) -> None`; `async def users.enable_totp(conn, secret_b32: str, *, data_key: bytes) -> None`; `async def users.disable_totp(conn) -> None`; `users.totp_secret(row: UserRow, *, keys: Sequence[bytes]) -> str | None`
  - `totp.new_secret() -> str`; `totp.provisioning_uri(secret: str, username: str) -> str` (issuer `Job Hunter`); `totp.verify(secret: str, code: str, *, at: datetime) -> bool` (`valid_window=1`)
  - CLI:
    - `user create --username <u> [--password-stdin]` prompts twice with `getpass`. `--password-stdin` reads one line and is refused in prod with exit 2.
    - `user totp enable` prints the URI and a `segno` terminal QR code, prompts for the current code, and stores the secret only when the code verifies.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_crypto.py
import os, pytest
from jobhunter.crypto import DecryptError, decrypt, encrypt, hkdf_sha256, key_id
def test_roundtrip_key_id_and_rotation() -> None:
    old, new = os.urandom(32), os.urandom(32)
    blob = encrypt(old, b"JBSWY3DPEHPK3PXP", aad=b"totp")
    assert blob[0] == 1 and blob[1:9] == key_id(old) and decrypt([new, old], blob, aad=b"totp") == b"JBSWY3DPEHPK3PXP"
    for bad in (lambda: decrypt([old], blob, aad=b"other"), lambda: decrypt([old], blob[:-1] + bytes([blob[-1] ^ 1]), aad=b"totp"),
                lambda: decrypt([new], blob, aad=b"totp")):
        with pytest.raises(DecryptError): bad()
def test_hkdf_is_deterministic_and_separated() -> None:
    root = b"r" * 32
    assert hkdf_sha256(root, b"session") == hkdf_sha256(root, b"session") != hkdf_sha256(root, b"login-username")
```

```python
# tests/unit/test_passwords.py
import unicodedata
from importlib.resources import files
import pytest
from argon2 import PasswordHasher
from jobhunter.auth.passwords import WeakPassword, check_password_policy, hash_password, verify_password
def test_argon2id_parameters() -> None:
    assert hash_password("correct horse battery").startswith("$argon2id$v=19$m=65536,t=3,p=4$")
def test_policy_length_and_common_list() -> None:
    with pytest.raises(WeakPassword) as e:
        check_password_policy("elevenchars")
    assert e.value.reason == "too_short"
    words = [w for w in files("jobhunter.auth").joinpath("common_passwords.txt").read_text().split() if not w.startswith("#")]
    common = next(w for w in words if len(w) >= 12)
    with pytest.raises(WeakPassword) as e2:
        check_password_policy(common.upper())
    assert e2.value.reason == "common"
    check_password_policy("Tannenbaum-Ölfass-7")
def test_nfc_and_nfd_passwords_verify_alike() -> None:          # Review Focus 3
    nfc = unicodedata.normalize("NFC", "Müllerstraße-2026!")
    assert verify_password(hash_password(nfc), unicodedata.normalize("NFD", nfc))[0]
def test_rehash_when_parameters_change() -> None:
    old = PasswordHasher(time_cost=2, memory_cost=65536, parallelism=4).hash("Tannenbaum-Ölfass-7")
    ok, new = verify_password(old, "Tannenbaum-Ölfass-7")
    assert ok and new is not None and new.startswith("$argon2id$v=19$m=65536,t=3,p=4$")
```

```python
# tests/integration/test_users.py
import os, subprocess, sys
from datetime import UTC, datetime, timedelta
import pyotp, pytest, sqlalchemy as sa
from jobhunter.auth import totp, users
from jobhunter.clock import utcnow
pytestmark = pytest.mark.integration
PW = "Tannenbaum-Ölfass-7"

async def test_single_user_and_username_rules(engine) -> None:
    async with engine.begin() as c:
        await users.create_user(c, "  Ada Lovelace ", PW, now=utcnow())
        with pytest.raises(users.UserExists): await users.create_user(c, "other", PW, now=utcnow())
        assert (await c.execute(sa.text("SELECT 1"))).scalar_one() == 1                  # transaction still usable
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT username FROM users"))).scalar_one() == "Ada Lovelace"

async def test_username_match_ignores_case_and_outer_spaces(engine) -> None:      # Review Focus 3
    async with engine.begin() as c: await users.create_user(c, "Straße", PW, now=utcnow())
    async with engine.connect() as c:
        assert await users.find_user(c, " STRASSE ") is not None and await users.find_user(c, "strasse2") is None

async def test_set_password_revokes_sessions(engine) -> None:
    async with engine.begin() as c:
        await users.create_user(c, "ada", PW, now=utcnow())
        await c.execute(sa.text("INSERT INTO sessions (token_hmac,user_id,idle_expires_at,absolute_expires_at) VALUES ('\\x01',1,now(),now())"))
        assert await users.set_password(c, "Another-Long-Pass-9", now=utcnow()) == 1

async def test_totp_window_and_encrypted_storage(engine) -> None:
    key, secret, now = os.urandom(32), totp.new_secret(), datetime(2026, 10, 16, 10, tzinfo=UTC)
    assert totp.verify(secret, pyotp.TOTP(secret).at(now - timedelta(seconds=30)), at=now)
    assert not totp.verify(secret, pyotp.TOTP(secret).at(now - timedelta(seconds=90)), at=now)
    async with engine.begin() as c:
        await users.create_user(c, "ada", PW, now=utcnow()); await users.enable_totp(c, secret, data_key=key)
        row = await users.find_user(c, "ada")
    assert row is not None and row.totp_enabled and secret.encode() not in (row.totp_secret_enc or b"")
    assert users.totp_secret(row, keys=[key]) == secret

def test_cli_create_with_stdin_never_echoes(migrated, proc_env) -> None:
    env = proc_env("api", migrated)
    r = subprocess.run([sys.executable, "-m", "jobhunter.cli", "user", "create", "--username", "cli-user", "--password-stdin"],
                       input=PW + "\n", env=env, capture_output=True, text=True)
    assert r.returncode == 0 and PW not in r.stdout + r.stderr
    r = subprocess.run([sys.executable, "-m", "jobhunter.cli", "user", "create", "--username", "x", "--password-stdin"],
                       input=PW + "\n", env={**env, "JOBHUNTER_ENV": "prod"}, capture_output=True, text=True)
    assert r.returncode == 2
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_crypto.py tests/unit/test_passwords.py tests/integration/test_users.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'jobhunter.crypto'`

- [ ] **Step 3: Implement crypto, passwords, users, TOTP and the CLI commands**

- **`common_passwords.txt`**: SecLists `Passwords/Common-Credentials/10k-most-common.txt` (MIT). The first line is a `#` comment naming the source and the upstream commit SHA. It is loaded once into a casefolded frozenset.
- **Passwords**: every password function applies `normalize_password` first. `verify_password` uses `ARGON2.verify` and `check_needs_rehash`. `dummy_verify` verifies against a hash computed at import.
- **`create_user`**:
  - the username is 3–64 characters after `normalize_username`, else `ValueError("username_length")`;
  - `INSERT INTO users (id, username, password_hash, password_changed_at) VALUES (1, …) ON CONFLICT (id) DO NOTHING RETURNING id`; no row raises `UserExists`, and the transaction stays valid.
- **`find_user`** reads `id = 1` and compares `stored.casefold() == normalize_username(given).casefold()`.
- **TOTP storage**: `enable_totp` stores `crypto.encrypt(data_key, secret.encode(), aad=b"totp")`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_crypto.py tests/unit/test_passwords.py tests/integration/test_users.py -q`
Expected: PASS (11 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/src/jobhunter tests
git commit -m "feat(m0): single user with Argon2id, common-password policy, encrypted TOTP and user CLI"
```

---

### Task 16: Login, sessions, serialised throttling, CSRF and rate limit

**Files:**
- Create: `backend/src/jobhunter/auth/sessions.py`, `backend/src/jobhunter/auth/throttle.py`, `backend/src/jobhunter/api/routes/auth.py`, `docs/spikes/V20.md`
- Modify: `backend/src/jobhunter/api/edge.py` (CSRF), `backend/src/jobhunter/api/app.py` (auth router, rate limiter), `backend/src/jobhunter/api/deps.py` (`require_session`), `backend/src/jobhunter/cli.py` (`sessions revoke-all|unlock`), `tests/support/fixtures.py` (`make_user`, `logged_in_client`)
- Test: `tests/integration/test_auth.py`, `tests/integration/test_throttle_concurrency.py`

**Interfaces:**
- Consumes: `users.*`, `passwords.*`, `totp.verify`, `crypto.hkdf_sha256`, `raise_alert`, `resolve_alert`, `Problem`, `problem_body`, `EdgeMiddleware`.
- Produces:
  - `sessions.SESSION_COOKIE: Final = "__Host-jh_session"`; `IDLE: Final = timedelta(hours=12)`; `ABSOLUTE: Final = timedelta(days=30)`; `SLIDE_EVERY: Final = timedelta(minutes=1)`; `COOKIE_MAX_AGE_S: Final = 2_592_000`
  - `sessions.session_pepper(session_secret: bytes) -> bytes` (`hkdf_sha256(secret, b"session")`); `sessions.token_hmac(token: str, pepper: bytes) -> bytes`
  - `@dataclass(frozen=True) class SessionInfo`: `token_hmac: bytes`, `username: str`, `totp_enabled: bool`, `idle_expires_at: datetime`, `absolute_expires_at: datetime`
  - `async def sessions.create_session(conn, *, pepper: bytes, ip: str | None, user_agent: str | None, now: datetime) -> str`; `async def sessions.validate_session(conn, token: str, *, pepper: bytes, now: datetime) -> SessionInfo | None`; `async def sessions.delete_session(conn, token: str, *, pepper: bytes) -> None`; `async def sessions.revoke_all(conn) -> int`
  - `throttle.THROTTLE_LOCK_KEY: Final = 7261002`; `IP_MAX_FAILURES: Final = 5`; `IP_WINDOW`, `IP_LOCK` (15 min each); `GLOBAL_MAX_FAILURES: Final = 20`; `GLOBAL_WINDOW`, `GLOBAL_LOCK` (1 h each)
  - `throttle.username_hmac(username: str, session_secret: bytes) -> bytes` (HMAC-SHA256 of the casefolded NFC username with `hkdf_sha256(secret, b"login-username")`)
  - `async def throttle.serialize(conn) -> None` (`SELECT pg_advisory_xact_lock(7261002)`)
  - `async def throttle.active_lockout(conn, ip: str, *, now: datetime) -> timedelta | None`; `async def throttle.record_attempt(conn, *, ip: str, username_hmac: bytes, success: bool, now: datetime) -> None` (requires `serialize` earlier in the same transaction); `async def throttle.unlock_all(conn) -> int`
  - `api.app.SessionRateLimiter(limit: int = 600, window_s: float = 60.0)` with `hit(key: bytes, now: float) -> bool`
  - `deps.require_session(request: Request) -> SessionInfo` (401 `unauthenticated` with the cookie cleared; 429 `throttled` past 600/min)
  - `routes.auth.LoginOutcome = Locked | Rejected | Accepted`, frozen dataclasses `Locked(retry_after: timedelta)`, `Rejected()` and `Accepted(token: str)`; `async def routes.auth.decide_login(conn: AsyncConnection, *, ip: str, username_hmac: bytes, credentials_ok: bool, new_hash: str | None, pepper: bytes, user_agent: str | None, now: datetime) -> LoginOutcome` (writes, never raises a `Problem`)
  - Routes:
    - `POST /api/auth/login`: `LoginIn{username: str(1..256), password: str(1..1024), totp: str | None (^\d{6}$)}` → 204 with `Set-Cookie`;
    - `POST /api/auth/logout` → 204;
    - `GET /api/auth/session` → `SessionOut{username, totp_enabled, idle_expires_at, absolute_expires_at}`.
  - Fixtures: `make_user(username: str = "ada", password: str = "Tannenbaum-Ölfass-7") -> Awaitable[None]`; `logged_in_client`, a **separate** client on the same app as `api_client`, logged in, with the default headers `Origin: https://testserver` and `X-JH-Request: 1`

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_auth.py
import uuid
import httpx, pytest, sqlalchemy as sa
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
pytestmark = pytest.mark.integration
PW = "Tannenbaum-Ölfass-7"
def login(c, password=PW, **extra):
    return c.post("/api/auth/login", json={"username": "ada", "password": password, **extra})

async def test_login_sets_host_cookie_and_stores_only_hmac(api_client, make_user, engine) -> None:
    await make_user(); r = await login(api_client)
    assert r.status_code == 204
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("__Host-jh_session=") and "Domain" not in cookie
    for attr in ("Secure", "HttpOnly", "SameSite=Strict", "Path=/", "Max-Age=2592000"): assert attr in cookie
    token = cookie.split(";")[0].split("=", 1)[1]
    async with engine.connect() as c: stored = (await c.execute(sa.text("SELECT token_hmac FROM sessions"))).scalar_one()
    assert token.encode() not in stored and len(stored) == 32

async def test_failures_are_generic_and_constant_path(api_client, make_user, monkeypatch) -> None:
    await make_user(); called: list[int] = []
    import jobhunter.api.routes.auth as auth_routes
    monkeypatch.setattr(auth_routes, "dummy_verify", lambda p: called.append(1))
    a = await login(api_client, password="wrong-password-123")
    b = await api_client.post("/api/auth/login", json={"username": "nobody", "password": "wrong-password-123"})
    assert a.status_code == b.status_code == 401 and a.json()["title"] == b.json()["title"] and called == [1]

async def test_login_checks_origin_only_and_rejection_has_edge_headers(api_client, make_user) -> None:
    await make_user()
    r = await api_client.post("/api/auth/login", json={"username": "ada", "password": PW}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["code"] == "csrf_failed"
    assert r.headers["x-content-type-options"] == "nosniff" and r.json()["request_id"] == r.headers["x-request-id"]
    assert uuid.UUID(r.headers["x-request-id"]).version == 7
    assert (await login(api_client)).status_code == 204

async def test_ip_lockout_after_five_failures(api_client, make_user, engine) -> None:
    await make_user()
    for _ in range(5): assert (await login(api_client, password="x" * 12)).status_code == 401
    async with engine.connect() as c:                      # a separate connection sees committed rows only
        assert tuple((await c.execute(sa.text(
            "SELECT (SELECT count(*) FROM login_attempts WHERE host(ip) = '198.51.100.10' AND NOT success), "
            "(SELECT count(*) FROM login_lockouts WHERE scope = 'ip:198.51.100.10' AND locked_until > now())"))).one()) == (5, 1)
    r = await login(api_client)
    assert r.status_code == 429 and r.json()["code"] == "throttled" and 880 <= r.json()["retry_after_s"] <= 900
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT count(*) FROM login_attempts WHERE username_hmac = convert_to('ada','UTF8')"))).scalar_one() == 0

async def test_global_lockout_after_twenty_failures(engine, make_config, make_user) -> None:
    from jobhunter.api.app import create_app
    await make_user(); app = create_app(cfg=make_config("api"), engine=engine)
    def client(ip: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(ip, 1)), base_url="https://testserver",
                                 headers={"Origin": "https://testserver"})
    for i in range(20):
        async with client(f"203.0.113.{i + 1}") as c: assert (await login(c, password="y" * 12)).status_code == 401
    async with engine.connect() as c:
        assert tuple((await c.execute(sa.text("SELECT (SELECT count(*) FROM login_attempts WHERE NOT success), "
                                              "(SELECT count(*) FROM login_lockouts WHERE scope = 'global')"))).one()) == (20, 1)
    async with client("192.0.2.200") as c: assert (await login(c)).status_code == 429
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT severity FROM alerts WHERE code='auth.lockout' AND resolved_at IS NULL"))).scalar_one() == "critical"

async def test_session_idle_absolute_and_slide(logged_in_client, engine) -> None:
    assert (await logged_in_client.get("/api/auth/session")).status_code == 200
    async with engine.begin() as c:
        await c.execute(sa.text("UPDATE sessions SET last_seen_at = now() - interval '30 seconds'"))
        before = (await c.execute(sa.text("SELECT idle_expires_at FROM sessions"))).scalar_one()
    await logged_in_client.get("/api/auth/session")
    async with engine.begin() as c:
        assert (await c.execute(sa.text("SELECT idle_expires_at FROM sessions"))).scalar_one() == before
        await c.execute(sa.text("UPDATE sessions SET idle_expires_at = now() - interval '1 second'"))
    r = await logged_in_client.get("/api/auth/session")
    assert r.status_code == 401 and r.json()["code"] == "unauthenticated" and "Max-Age=0" in r.headers["set-cookie"]

async def test_csrf_on_state_changing_routes(logged_in_client) -> None:
    r = await logged_in_client.post("/api/auth/logout", headers={"X-JH-Request": ""})
    assert r.status_code == 403 and r.json()["code"] == "csrf_failed" and r.json()["request_id"] == r.headers["x-request-id"]
    assert (await logged_in_client.post("/api/auth/logout")).status_code == 204
    assert (await logged_in_client.get("/api/auth/session")).status_code == 401

async def test_totp_required_when_enabled(api_client, make_user, engine, make_config) -> None:
    import pyotp
    from jobhunter.auth import totp, users
    await make_user(); secret = totp.new_secret()
    async with engine.begin() as c: await users.enable_totp(c, secret, data_key=make_config("api").secret_b64("data_key"))
    assert (await login(api_client)).status_code == 401
    assert (await login(api_client, totp=pyotp.TOTP(secret).now())).status_code == 204

async def test_rate_limit_600_per_minute(logged_in_client) -> None:
    codes = [(await logged_in_client.get("/api/auth/session")).status_code for _ in range(601)]
    assert codes[:600].count(200) == 600 and codes[600] == 429

async def test_v20_dev_real_client_ip_through_trusted_proxy(engine, make_config, make_user) -> None:
    from jobhunter.api.app import create_app
    await make_user()
    app = ProxyHeadersMiddleware(create_app(cfg=make_config("api"), engine=engine), trusted_hosts=["172.30.0.2"])
    for peer, xff in (("172.30.0.2", "203.0.113.7"), ("198.51.100.9", "203.0.113.8")):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(peer, 4000)), base_url="https://testserver",
                                     headers={"Origin": "https://testserver", "X-Forwarded-For": xff}) as c:
            await login(c, password="z" * 12)
    async with engine.connect() as c:
        assert [r[0] for r in await c.execute(sa.text("SELECT host(ip) FROM login_attempts ORDER BY id"))] == ["203.0.113.7", "198.51.100.9"]
```

```python
# tests/integration/test_throttle_concurrency.py
import asyncio
import pytest, sqlalchemy as sa
from jobhunter.auth import throttle
from jobhunter.clock import utcnow
from tests.support.procs import await_until
pytestmark = pytest.mark.integration
IP = "198.51.100.77"

async def failures(engine, n: int, ip: str = IP) -> None:
    async with engine.begin() as c:
        for _ in range(n):
            await c.execute(sa.text("INSERT INTO login_attempts (ip, username_hmac, success) VALUES (CAST(:ip AS inet), '\\x01', false)"), {"ip": ip})

async def blocked_on_advisory(engine) -> bool:
    async with engine.connect() as c:
        return (await c.execute(sa.text("SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND objid=7261002 AND NOT granted"))).scalar_one() == 1

@pytest.mark.parametrize(("pre", "ip_of", "scope"), [(3, lambda i: IP, f"ip:{IP}"), (18, lambda i: f"203.0.113.{i}", "global")])
async def test_concurrent_failures_reach_the_threshold(engine, pre, ip_of, scope) -> None:
    for i in range(pre): await failures(engine, 1, ip_of(100 + i))
    a, b = await engine.connect(), await engine.connect()
    ta, tb = await a.begin(), await b.begin()
    await throttle.serialize(a)
    await throttle.record_attempt(a, ip=ip_of(1), username_hmac=b"\x01", success=False, now=utcnow())   # count = pre+1, below threshold
    async def second() -> None:
        await throttle.serialize(b)
        await throttle.record_attempt(b, ip=ip_of(2), username_hmac=b"\x01", success=False, now=utcnow())
        await tb.commit()
    task = asyncio.create_task(second())
    await await_until(lambda: blocked_on_advisory(engine), 5)          # b is provably waiting behind a
    await ta.commit(); await asyncio.wait_for(task, 5)
    await a.close(); await b.close()
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT count(*) FROM login_lockouts WHERE scope = :s AND locked_until > now()"), {"s": scope})).scalar_one() == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/integration/test_auth.py tests/integration/test_throttle_concurrency.py -q`
Expected: FAIL with `fixture 'make_user' not found`

- [ ] **Step 3: Implement sessions, throttling, CSRF, the rate limit and the auth routes**

**Login** (`routes/auth.py`, which imports `dummy_verify` by name):
1. Credentials are verified **outside** any transaction: `find_user`, `verify_password` or `dummy_verify`, then TOTP when enabled.
2. Then `outcome = await decide_login(conn, …)` runs in one `async with engine.begin()` block, which **ends normally**, so everything it wrote commits:
   1. `throttle.serialize`;
   2. `active_lockout(ip)`, which may resolve an expired `auth.lockout` alert; when locked → `Locked(retry_after)`, even if the credentials were valid;
   3. otherwise `record_attempt(success=…)`, which may write lockouts and the `auth.lockout` alert;
   4. on failure → `Rejected()`;
   5. on success → `update_hash` when rehashed, then `create_session` → `Accepted(token)`.
3. Only after the block has committed does the route map the outcome:
   - `Locked` → raise 429 `throttled` with `extra={"retry_after_s": ceil(seconds)}`;
   - `Rejected` → raise 401 `unauthenticated`, title `"Invalid credentials"`;
   - `Accepted` → 204 with `Set-Cookie: __Host-jh_session=<token>; Max-Age=2592000; Path=/; Secure; HttpOnly; SameSite=Strict`.

   Raising the `Problem` inside the block would roll back the attempt, the lockout and the alert (Global Constraints). `test_ip_lockout_after_five_failures` and `test_global_lockout_after_twenty_failures` read the committed rows through a separate connection after the HTTP responses.

The IP is `request.client.host`.

**`record_attempt`** inserts the attempt, then counts under the lock:
- when the IP's failures in the last 15 min reach 5: upsert `login_lockouts('ip:<ip>', now + 15 min, 'ip_failures')`;
- when all failures in the last hour reach 20: upsert `login_lockouts('global', now + 1 h, 'global_failures')` and `raise_alert(code="auth.lockout", severity="critical", subject="global", message="global login lockout")`.

**`active_lockout`** returns the larger remaining time of the rows still in force. When the global row has expired, it resolves `auth.lockout`.

**Sessions**:
- The token is `secrets.token_urlsafe(32)`, stored as an HMAC. On create: `idle_expires_at = now + 12 h`, `absolute_expires_at = now + 30 d`, `user_agent_sha256`.
- `validate_session` rejects rows that are revoked or past either expiry. It slides (`last_seen_at = now`, `idle_expires_at = least(now + 12 h, absolute)`) only when `now − last_seen_at ≥ 1 min`.

**CSRF**, in `EdgeMiddleware`: for POST, PUT, PATCH and DELETE under `/api/`:
- `Origin` must equal `cfg.public_origin`;
- `X-JH-Request: 1` is also required, except on `/api/auth/login`;
- otherwise the edge itself sends a 403 `csrf_failed` problem (`problem_body`, edge headers, request id).

**Rate limit**: `require_session` runs `validate_session` in its own `engine.begin()` block, which commits (including a slide) before anything is raised. It then raises 401 when there is no valid session, or calls `SessionRateLimiter.hit(token_hmac, time.monotonic())` (an in-memory sliding 60 s window; the api runs one replica) and raises 429 past the limit.

**CLI**: `sessions revoke-all` calls `revoke_all`; `sessions unlock` calls `unlock_all` plus `resolve_alert(code="auth.lockout", subject="global")`.

**`V20.md`** records the dev part (these tests plus the devproxy check of Task 18); the Caddy part lands in M5.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/integration/test_auth.py tests/integration/test_throttle_concurrency.py -q`
Expected: PASS (12 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/src/jobhunter docs/spikes/V20.md tests
git commit -m "feat(m0): login with serialised DB throttling, __Host- sessions, edge CSRF, per-session rate limit, optional TOTP"
```

---

### Task 17: Idempotency store, Run now and system health

**Files:**
- Create: `backend/src/jobhunter/api_idempotency.py`, `backend/src/jobhunter/api/routes/runs.py`, `backend/src/jobhunter/api/routes/system.py`
- Modify: `backend/src/jobhunter/observability/health.py` (`system_health`), `backend/src/jobhunter/api/app.py` (routers; `make_queue_app(cfg.dsn("psycopg"))` at construction)
- Test: `tests/unit/test_idempotency_fingerprint.py`, `tests/integration/test_runs_api.py`

**Interfaces:**
- Consumes: `request_manual_run`, `run_tick`, `defer_in_tx`, `make_queue_app`, `check_readiness`, `refresh_alert_gauge`, `require_session`, `Problem`, `q`, `pg_error`, the `log_events` fixture.
- Produces:
  - `IDEMPOTENCY_TTL: Final = timedelta(days=7)`; `IDEM_KEY_RE: Final = re.compile(r"^[A-Za-z0-9_-]{16,128}$")`; `IN_PROGRESS_LOCK_TIMEOUT: Final = "5s"`
  - `request_fingerprint(body: JSONValue, path_params: Mapping[str, JSONValue]) -> bytes`
  - `@dataclass(frozen=True) class StoredResponse`: `status: int`, `body: dict[str, JSONValue]`
  - `async def run_idempotent(conn: AsyncConnection, *, route: str, key: str | None, fingerprint: bytes, now: datetime, handler: Callable[[AsyncConnection], Awaitable[StoredResponse]]) -> StoredResponse`
  - `POST /api/runs`: `RunNowIn{include_paid: bool}` → 202 `RunNowOut{run: RunOut, created: bool}`, where `RunOut{id, kind, trigger, status, scheduled_slot, include_paid, attempt_no, queued_at, started_at, finished_at}`
  - `RunAgeState = Literal["ok", "warn", "critical", "grace", "none"]`
  - `GET /api/system/health` → `SystemHealthOut`:
    - `ready: {ok, failing}`;
    - `last_run: RunOut | null`, `last_run_age_s: float | null`;
    - `last_successful_run_age_s: float | null` (counting `succeeded` and `partial`);
    - `last_fully_successful_run_age_s: float | null`;
    - `run_age_state: RunAgeState` (warn > 1.5 × interval, critical > 2 × interval, `grace` until the first settings version's `created_at + 2 × interval`, `none` before any run);
    - `queue_depths: dict[str, dict[str, int]]`;
    - `alerts_open: {info, warn, critical}`;
    - `disk: {path: "/", total_bytes, free_bytes}`;
    - `database_bytes: int`;
    - `table_bytes: dict[str, int]`;
    - `versions: {app, python, fastapi, sqlalchemy, psycopg, procrastinate, langgraph}`.
  - `async def system_health(conn: AsyncConnection, *, engine: AsyncEngine, code_head: str, now: datetime) -> SystemHealthOut` (also refreshes `ALERTS_OPEN`)

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_idempotency_fingerprint.py
from jobhunter.api_idempotency import request_fingerprint
def test_key_order_does_not_matter() -> None:
    assert request_fingerprint({"a": 1, "b": [1, 2]}, {}) == request_fingerprint({"b": [1, 2], "a": 1}, {})
    assert request_fingerprint({"a": 1}, {}) != request_fingerprint({"a": 2}, {})
```

```python
# tests/integration/test_runs_api.py
import asyncio
from datetime import timedelta
import pytest, sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.worker import runs
from jobhunter.worker.tasks import defer_in_tx
from jobhunter.worker.tick import run_tick
from tests.support.procs import await_until
pytestmark = pytest.mark.integration
KEY = "run-now-key-000001"

async def post(c, key: str | None = KEY, include_paid: bool = False):
    return await c.post("/api/runs", json={"include_paid": include_paid}, headers={"Idempotency-Key": key} if key else {})

async def waiting_on_lock(engine) -> bool:
    async with engine.connect() as c:
        return (await c.execute(sa.text("SELECT count(*) FROM pg_locks WHERE NOT granted AND locktype = 'transactionid'"))).scalar_one() >= 1

async def due_slot(engine):
    now = utcnow()
    async with engine.begin() as c:
        await c.execute(sa.text("UPDATE schedule_state SET anchor_utc=:a, interval_s=3600, last_evaluated_slot=:l"),
                        {"a": now - timedelta(minutes=1), "l": now - timedelta(hours=1, minutes=1)})
    return now

async def test_key_required_and_format(logged_in_client) -> None:
    for key in (None, "short", "has space in it!!!"):
        r = await post(logged_in_client, key); assert r.status_code == 400 and r.json()["code"] == "idempotency_key_required"

async def test_same_key_returns_stored_response_and_one_run(logged_in_client, engine, queue_app) -> None:
    a = await post(logged_in_client); b = await post(logged_in_client)
    assert a.status_code == b.status_code == 202 and a.json() == b.json() and a.json()["created"] is True
    assert a.json()["run"]["trigger"] == "manual" and a.json()["run"]["scheduled_slot"] is None
    async with engine.begin() as c: await c.execute(sa.text("UPDATE runs SET status='cancelled', finished_at=now()"))
    assert (await post(logged_in_client)).json() == a.json()
    async with engine.connect() as c: assert (await c.execute(sa.text("SELECT count(*) FROM runs"))).scalar_one() == 1

async def test_reused_key_with_other_body_is_422(logged_in_client, queue_app) -> None:
    await post(logged_in_client); r = await post(logged_in_client, include_paid=True)
    assert r.status_code == 422 and r.json()["code"] == "idempotency_key_reused"

async def test_new_key_returns_active_run_and_expired_key_runs_again(logged_in_client, engine, queue_app) -> None:
    a = await post(logged_in_client); b = await post(logged_in_client, key="run-now-key-000002")
    assert b.json()["created"] is False and b.json()["run"]["id"] == a.json()["run"]["id"]
    async with engine.begin() as c:
        await c.execute(sa.text("UPDATE api_idempotency SET expires_at = now() - interval '1 second'"))
        await c.execute(sa.text("UPDATE runs SET status='cancelled', finished_at=now()"))
    assert (await post(logged_in_client)).json()["created"] is True

async def test_run_now_blocked_behind_tick_returns_tick_run(logged_in_client, engine, queue_app, log_events) -> None:   # Review Focus 4
    now = await due_slot(engine)
    conn = await engine.connect(); tx = await conn.begin()
    rep = await run_tick(conn, now=now, defer=defer_in_tx)                   # tick's run inserted, not committed
    req = asyncio.create_task(post(logged_in_client))
    await await_until(lambda: waiting_on_lock(engine), 5)                   # Run now provably blocked on the index
    await tx.commit(); await conn.close()
    r = await asyncio.wait_for(req, 5)
    assert r.status_code == 202 and r.json() == {"run": r.json()["run"], "created": False} and r.json()["run"]["id"] == rep.queued_run_id
    assert [e["path"] for e in log_events() if e["event"] == "active_collect_conflict"] == ["manual"]   # the IntegrityError branch

async def test_tick_blocked_behind_run_now_records_overlap(engine, queue_app, log_events) -> None:       # Review Focus 4
    now = await due_slot(engine)
    conn = await engine.connect(); tx = await conn.begin()
    manual, created = await runs.request_manual_run(conn, include_paid=False, defer=defer_in_tx)
    async def tick() -> object:
        async with engine.begin() as c: return await run_tick(c, now=now, defer=defer_in_tx)
    t = asyncio.create_task(tick())
    await await_until(lambda: waiting_on_lock(engine), 5)
    await tx.commit(); await conn.close()
    rep = await asyncio.wait_for(t, 5)
    assert created and rep.skipped_overlap and rep.queued_run_id is None
    assert [e["path"] for e in log_events() if e["event"] == "active_collect_conflict"] == ["tick"]
    async with engine.connect() as c:
        assert [r[0] for r in await c.execute(sa.text("SELECT id FROM runs WHERE status IN ('queued','running')"))] == [manual.id]

async def test_same_key_in_flight_elsewhere_is_409(logged_in_client, engine, queue_app, monkeypatch) -> None:
    import jobhunter.api_idempotency as idem
    monkeypatch.setattr(idem, "IN_PROGRESS_LOCK_TIMEOUT", "200ms")
    conn = await engine.connect(); tx = await conn.begin()                   # another request holds the key, uncommitted
    await conn.execute(sa.text("INSERT INTO api_idempotency (route, idem_key, request_sha256, status, expires_at) VALUES "
                               "('POST /api/runs', :k, decode(repeat('00', 32), 'hex'), 'in_progress', now() + interval '7 days')"), {"k": KEY})
    r = await post(logged_in_client)                                          # waits on the key, then lock_timeout: SQLSTATE 55P03
    assert r.status_code == 409 and r.json()["code"] == "idempotency_in_progress"
    await tx.rollback(); await conn.close()
    async with engine.connect() as c: assert (await c.execute(sa.text("SELECT count(*) FROM runs"))).scalar_one() == 0

async def test_system_health_requires_session_and_reports(api_client, logged_in_client, engine, queue_app) -> None:
    assert (await api_client.get("/api/system/health")).status_code == 401
    await due_slot(engine)
    async with engine.begin() as c: await run_tick(c, now=utcnow() + timedelta(hours=2), defer=defer_in_tx)
    body = (await logged_in_client.get("/api/system/health")).json()
    # anchor and anchor+1h are missed; anchor+2h is about 1 min old at this tick, within CATCH_UP_AFTER, so 'schedule' (Task 11)
    assert body["last_run"]["status"] == "queued" and body["last_run"]["trigger"] == "schedule"   # newest by (queued_at, id)
    assert body["alerts_open"] == {"info": 0, "warn": 0, "critical": 0} and body["run_age_state"] in ("grace", "none")
    assert body["queue_depths"]["collect"]["todo"] == 1 and body["versions"]["procrastinate"] == "3.10.0"
    assert "worker_heartbeat" in body["ready"]["failing"] and body["table_bytes"]["runs"] > 0
    assert body["disk"]["total_bytes"] > body["disk"]["free_bytes"] > 0 and body["database_bytes"] > 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_idempotency_fingerprint.py tests/integration/test_runs_api.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'jobhunter.api_idempotency'`

- [ ] **Step 3: Implement the store, the routes and `system_health`**

**`run_idempotent`** runs inside the caller's transaction:
1. A missing key, or one that fails `IDEM_KEY_RE`, → 400 `idempotency_key_required`.
2. `SELECT set_config('lock_timeout', :t, true)` with `t = IN_PROGRESS_LOCK_TIMEOUT`, read at call time; then `INSERT … (status='in_progress', expires_at = now + 7 d) ON CONFLICT (route, idem_key) DO NOTHING RETURNING 1`; then `set_config('lock_timeout', '0', true)`, so the handler runs without the timeout (Task 4 sets no server-wide `lock_timeout`).
   - A `sqlalchemy.exc.OperationalError` with `pg_error(exc).sqlstate == LOCK_NOT_AVAILABLE` (55P03) → 409 `idempotency_in_progress`. Nothing in this transaction has to persist, so raising here is correct. Any other error re-raises. `test_same_key_in_flight_elsewhere_is_409` takes this branch.
   - Inserted: `resp = await handler(conn)`, then `UPDATE … SET status='completed', response_status, response_body` (JSONB-bound), and return `resp`.
3. Otherwise, select the row:
   - expired → delete it and retry the insert once;
   - another fingerprint → 422 `idempotency_key_reused`;
   - `in_progress` → 409 `idempotency_in_progress`;
   - else return the stored response.

**`POST /api/runs`** wraps `request_manual_run(include_paid=…, defer=defer_in_tx)` in `run_idempotent` (route `"POST /api/runs"`), inside one `engine.begin()` transaction.

**`system_health`**:
- `check_readiness`;
- `runs ORDER BY queued_at DESC, id DESC LIMIT 1`;
- success ages from `max(finished_at)`;
- `run_age_state` from `schedule_state.interval_s` and the first settings version's `created_at`;
- procrastinate jobs grouped by `queue_name` and `status`;
- `refresh_alert_gauge`;
- `shutil.disk_usage("/")`, `pg_database_size(current_database())` and `pg_total_relation_size` per public table;
- `importlib.metadata.version` for each library.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_idempotency_fingerprint.py tests/integration/test_runs_api.py -q`
Expected: PASS (9 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/src/jobhunter tests
git commit -m "feat(m0): Idempotency-Key store, Run now endpoint, system health with run-age states, disk and gauges"
```

---

### Task 18: Dev stack, secret ownership, image digests and the dev part of V21

**Files:**
- Create: `Dockerfile`, `.dockerignore`, `deploy/compose.dev.yaml`, `deploy/Caddyfile.dev`, `scripts/dev-secrets.sh`, `docs/spikes/V21.md`
- Modify: `deploy/images.lock`, `docs/spikes/V20.md`
- Test: `tests/stack/__init__.py`, `tests/stack/test_dev_stack.py`

**Interfaces:**
- Consumes: the CLI commands `migrate`, `worker`, `worker-health`, `http-check` and `startup-check`; `jobhunter.api.app:app`.
- Produces:
  - **`deploy/images.lock`** adds, all digest-pinned via `docker pull` and `docker inspect --format '{{index .RepoDigests 0}}'`:
    - `python=python:3.13.16-slim-bookworm@sha256:…`;
    - `caddy=caddy:2.11.7-alpine@sha256:…`;
    - `uv=ghcr.io/astral-sh/uv:0.13.1@sha256:…`;
    - `actionlint=rhysd/actionlint:1.7.7@sha256:…`.
  - **Image `jobhunter-app:dev`**, built from the `Dockerfile` below with the build args `PYTHON_IMAGE` and `UV_IMAGE` from `deploy/images.lock`. It is a non-editable install (`uv sync --frozen --no-dev --no-editable`) into `/opt/venv`, so the runtime stage holds no source tree; `USER 10001:10001`.
  - **Every `docker compose` call** passes `--env-file deploy/images.lock`. The lock file's `<name>=<image>` lines are valid compose variables, and the compose file references them as `${pgvector:?}`, `${python:?}`, `${uv:?}` and `${caddy:?}`, so a missing digest fails loudly.
  - **Compose project `jobhunter-dev`**: the `x-app` anchor of §19.1 (`read_only`, `tmpfs: [/tmp]`, `cap_drop: [ALL]`, `no-new-privileges`, `user: "10001:10001"`, `group_add: ["10010"]`, local logging). Services:
    - `postgres` (digest image, `user: "999:999"`, `group_add: ["10010"]`, secrets `pg_bootstrap, db_app, db_migrator, db_backup, db_restore`);
    - `migrate`;
    - `api` (`uvicorn jobhunter.api.app:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips=172.30.0.2`);
    - `worker` (`stop_grace_period: 120s`, `deploy.replicas: 1`);
    - `devproxy` (Caddy digest; `devedge` plus `proxy` with `ipv4_address: 172.30.0.2`; `127.0.0.1:8080:8080`).
  - **Networks**: `proxy` (`internal: true`, subnet `172.30.0.0/24`), `internal` (`internal: true`), `egress`, `devedge`.
  - **The one canonical dev origin** is `JOBHUNTER_PUBLIC_ORIGIN=http://localhost:8080`.
  - **`scripts/dev-secrets.sh [dir]`** (default `.secrets/dev`):
    - creates missing secrets (`openssl rand -base64 32` for `session_secret` and `data_key`; `openssl rand -hex 24` for the passwords) with `chgrp 10010` and `chmod 0440`, and the directory with mode 0750;
    - never overwrites an existing secret;
    - if `chgrp 10010` or `chmod` fails, it prints `"dev-secrets: cannot set group 10010 on <file>; run as root (sudo) — the app runs as uid 10001 with group 10010 and cannot read the file otherwise"` to stderr and exits 1.

- [ ] **Step 1: Write the failing test**

```python
# tests/stack/test_dev_stack.py
import subprocess, time
from pathlib import Path
import httpx, pytest
pytestmark = pytest.mark.stack
REPO = Path(__file__).resolve().parents[2]
DC = ["docker", "compose", "--env-file", str(REPO / "deploy/images.lock"), "-f", str(REPO / "deploy/compose.dev.yaml")]
PROBE_NAME = "jh-egress-probe"

@pytest.fixture(scope="module")
def stack():
    subprocess.run([str(REPO / "scripts/dev-secrets.sh")], check=True)
    subprocess.run(DC + ["up", "-d", "--build", "--wait"], check=True, timeout=900)
    python = next(l.split("=", 1)[1] for l in (REPO / "deploy/images.lock").read_text().splitlines() if l.startswith("python="))
    subprocess.run(["docker", "run", "-d", "--rm", "--name", PROBE_NAME, "--network", "jobhunter-dev_egress",
                    python, "python", "-m", "http.server", "8099"], check=True)
    yield
    subprocess.run(["docker", "rm", "-f", PROBE_NAME], check=False)
    subprocess.run(DC + ["down", "-v"], check=True)

def exec_(svc: str, code: str) -> int:
    return subprocess.run(DC + ["exec", "-T", svc, "python", "-c", code]).returncode

PROBE = "import socket,sys\ntry:\n socket.create_connection(({host!r},{port}),timeout=3)\nexcept OSError:\n sys.exit(1)"

IMAGE_CHECK = "\n".join([
    "import importlib.resources as r, pathlib, jobhunter",
    "from jobhunter.db.alembic_runner import code_head",
    "site = pathlib.Path('/opt/venv/lib/python3.13/site-packages')",
    "assert pathlib.Path(jobhunter.__file__).resolve().is_relative_to(site / 'jobhunter'), jobhunter.__file__",
    "assert not list(site.glob('*jobhunter*.pth')), 'editable install'",
    "assert r.files('jobhunter.db').joinpath('seed_data/settings_v1.json').is_file()",
    "assert r.files('jobhunter.db').joinpath('migrations/versions/0002_m0_transition_matrix.py').is_file()",
    "assert r.files('jobhunter.auth').joinpath('common_passwords.txt').is_file()",
    "assert code_head() == '0002_m0_transition_matrix'",
    "assert not pathlib.Path('/src').exists()",
])

def test_final_image_runs_the_installed_package_with_its_resources(stack) -> None:
    r = subprocess.run(["docker", "run", "--rm", "jobhunter-app:dev", "python", "-I", "-c", IMAGE_CHECK], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr

def test_dev_stack_serves_through_devproxy(stack) -> None:
    r = httpx.get("http://localhost:8080/healthz", timeout=5)
    assert r.status_code == 200 and r.headers["x-content-type-options"] == "nosniff"
    deadline = time.monotonic() + 60
    while httpx.get("http://localhost:8080/readyz", timeout=5).status_code != 200:
        assert time.monotonic() < deadline, "readyz never 200"; time.sleep(2)

@pytest.mark.parametrize("role", ["api", "worker", "migrate"])
def test_startup_assertions_pass_under_real_uids(stack, role) -> None:
    r = subprocess.run(DC + ["run", "--rm", "--no-deps", "-T", role, "jobhunter", "startup-check", "--role", role],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    ids = subprocess.run(DC + ["run", "--rm", "--no-deps", "-T", role, "id", "-G"], capture_output=True, text=True).stdout.split()
    assert "10010" in ids

def test_postgres_reads_secrets_as_999_with_group_10010(stack) -> None:
    out = subprocess.run(DC + ["exec", "-T", "postgres", "id"], capture_output=True, text=True).stdout
    assert "uid=999" in out and "10010" in out

def test_v21_dev_api_has_no_egress_but_worker_does(stack) -> None:
    deadline = time.monotonic() + 15
    while exec_("worker", PROBE.format(host=PROBE_NAME, port=8099)) != 0:
        assert time.monotonic() < deadline, "probe never reachable from worker"; time.sleep(1)
    assert exec_("api", PROBE.format(host="1.1.1.1", port=443)) == 1
    assert exec_("api", PROBE.format(host=PROBE_NAME, port=8099)) == 1

def test_secrets_are_group_10010_0440_and_not_overwritten() -> None:
    d = REPO / ".secrets/dev"; before = (d / "db_app").read_bytes()
    subprocess.run([str(REPO / "scripts/dev-secrets.sh")], check=True)
    st = (d / "db_app").stat()
    assert (d / "db_app").read_bytes() == before and oct(st.st_mode & 0o777) == "0o440" and st.st_gid == 10010
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/stack/test_dev_stack.py -m stack -q`
Expected: FAIL with `No such file or directory: '…/scripts/dev-secrets.sh'`

- [ ] **Step 3: Write the Dockerfile, the compose file, the Caddyfile and the secrets script, and record the digests**

- `Dockerfile`. BuildKit does not expand variables in `COPY --from`, so the uv image becomes a named stage:

```dockerfile
ARG PYTHON_IMAGE
ARG UV_IMAGE
FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /src
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-editable --no-install-project
COPY backend ./backend
RUN uv sync --frozen --no-dev --no-editable

FROM ${PYTHON_IMAGE} AS runtime
COPY --from=build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1
WORKDIR /
USER 10001:10001
```

- `.dockerignore` excludes `.git`, `.venv`, `.secrets`, `frontend/node_modules`, `frontend/dist` and `__pycache__`.
- `compose.dev.yaml` takes the digests from `deploy/images.lock` through `--env-file`: `build: {context: .., dockerfile: Dockerfile, args: {PYTHON_IMAGE: "${python:?}", UV_IMAGE: "${uv:?}"}}` with `image: jobhunter-app:dev` for the app services, `image: "${pgvector:?}"` for postgres and `image: "${caddy:?}"` for devproxy. Dependencies: `migrate` → postgres healthy; `api` and `worker` → `migrate: service_completed_successfully`. Healthchecks as in §19.1.
- `Caddyfile.dev`:

```
:8080 {
  reverse_proxy api:8000 {
    header_up X-Forwarded-For {remote_host}
  }
}
```

- `V21.md` records the observed results. It notes that internet reachability from the worker is not asserted in the dev container (outbound traffic goes through the agent proxy) and that DNS behaviour plus the `DOCKER-USER` rule are verified in M5.
- `V20.md` adds a manual login through `http://localhost:8080` with a spoofed `X-Forwarded-For`: `login_attempts.ip` is the Docker gateway address. It also notes the intended dev cookie behaviour: browsers accept `Secure` cookies on `http://localhost`, and M4's playwright scenarios verify the browser session. M0's smoke test proves backend session handling only.

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/stack/test_dev_stack.py -m stack -q`
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**

```bash
git add Dockerfile .dockerignore deploy scripts/dev-secrets.sh docs/spikes tests/stack
git commit -m "feat(m0): dev stack with digest-pinned images, real UID/GID secret access, internal-only api network (V21 dev part)"
```

---

### Task 19: Frontend skeleton, typed client, OpenAPI types and V16

**Files:**
- Create: `frontend/package.json`, `frontend/package-lock.json`, `frontend/tsconfig.json`, `frontend/vite.config.ts`, `frontend/eslint.config.js`, `frontend/index.html`, `frontend/src/main.tsx`, `frontend/src/api/client.ts`, `frontend/src/api/client.test.ts`, `frontend/src/api/schema.d.ts` (generated), `scripts/check-openapi-drift.sh`, `docs/spikes/V16.md`
- Test: `frontend/src/api/client.test.ts`

**Interfaces:**
- Consumes: `python -m jobhunter.api.openapi_export`.
- Produces:
  - `export class ProblemError extends Error { status: number; code: string; requestId: string | null; errors: {field: string; code: string; message: string}[] }`
  - `export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T>`
  - npm scripts: `build`, `typecheck` (`tsc --noEmit`), `lint` (`eslint .`), `test` (`vitest run`), `gen:api` (`openapi-typescript openapi.json -o src/api/schema.d.ts`)
  - `scripts/check-openapi-drift.sh`: regenerates `openapi.json` and `schema.d.ts` and exits 1 on a diff

- [ ] **Step 1: Write the failing test**

```ts
// frontend/src/api/client.test.ts
import { afterEach, describe, expect, it, vi } from "vitest";
import { apiFetch, ProblemError } from "./client";
afterEach(() => vi.restoreAllMocks());
describe("apiFetch", () => {
  it("adds X-JH-Request only on state-changing methods and keeps same-origin credentials", async () => {
    const f = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("{}", { status: 200 }));
    await apiFetch("/api/auth/session");
    await apiFetch("/api/runs", { method: "POST", body: "{}" });
    const [getInit, postInit] = f.mock.calls.map((c) => c[1] as RequestInit);
    expect(new Headers(getInit.headers).has("X-JH-Request")).toBe(false);
    expect(new Headers(postInit.headers).get("X-JH-Request")).toBe("1");
    expect(postInit.credentials).toBe("same-origin");
  });
  it("throws ProblemError from problem details", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify(
      { code: "throttled", title: "Too many attempts", status: 429, request_id: "r-1", errors: [] }),
      { status: 429, headers: { "content-type": "application/problem+json" } }));
    await expect(apiFetch("/api/auth/login", { method: "POST" })).rejects.toMatchObject(
      { status: 429, code: "throttled", requestId: "r-1" } satisfies Partial<ProblemError>);
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd frontend && npm install && npx vitest run`
Expected: FAIL with `Failed to resolve import "./client"`

- [ ] **Step 3: Implement the client and pin the toolchain (V16)**

- Exact pins in `package.json` (each † version is recorded at install):
  - `vite@8.3.4`, `react@19.3.1`, `react-dom@19.3.1`, `openapi-typescript@7.13.0`, `vitest@5.0.5`;
  - `@types/react` and `@types/react-dom` (†, matching React 19);
  - `@vitejs/plugin-react` (†, Vite 8 compatible);
  - `eslint`, `@eslint/js` and `typescript-eslint` (†);
  - `typescript@7.0.2`.
- **V16**: run `npm run build && npm run lint && npm run typecheck`. If any fails under 7.0.2, pin `typescript@5.9.3`, re-run, and record the outcome in `V16.md`.
- `main.tsx` renders `<h1>Job Hunter</h1>`, with no inline script or style (§16.6 CSP).
- Generate the types (`uv run python -m jobhunter.api.openapi_export frontend/openapi.json && npm --prefix frontend run gen:api`) and commit `schema.d.ts`.

- [ ] **Step 4: Run the checks to verify they pass**

Run: `cd frontend && npx vitest run && npm run build && npm run lint && npm run typecheck && cd .. && scripts/check-openapi-drift.sh`
Expected: PASS (2 tests; build, lint and typecheck clean; the drift script exits 0)

- [ ] **Step 5: Commit**

```bash
git add frontend scripts/check-openapi-drift.sh docs/spikes/V16.md
git commit -m "feat(m0): SPA skeleton with typed API client, React type packages and generated OpenAPI types (V16)"
```

---

### Task 20: CI workflow

**Files:**
- Create: `.github/workflows/ci.yml`
- Test: `actionlint` (digest from `deploy/images.lock`) over the workflow

**Interfaces:**
- Consumes: every command of Tasks 1–19.
- Produces: the CI jobs `lint`, `unit`, `integration`, `frontend` and `audit` (the §18.8 subset for M0).

- [ ] **Step 1: Write the workflow**

| Job | Steps |
|---|---|
| `lint` | `actions/checkout@v5`; `astral-sh/setup-uv@v6` with `version: "0.13.1"`; `uv sync --frozen`; `uv run ruff check .`; `uv run ruff format --check .`; `uv run mypy`; `actions/setup-node@v5` with `node-version: "22.22.3"`; `npm ci --prefix frontend`; `npm run --prefix frontend lint`; `npm run --prefix frontend typecheck`; `gitleaks/gitleaks-action@v2` |
| `unit` | uv setup; `uv run pytest tests/unit tests/property -q` |
| `integration` | uv setup; `docker pull "$(grep ^pgvector= deploy/images.lock \| cut -d= -f2)"`; `uv run pytest tests/integration tests/crash -q` |
| `frontend` | Node setup; `npm ci --prefix frontend`; `npm --prefix frontend test`; `npm --prefix frontend run build`; uv setup; `scripts/check-openapi-drift.sh` |
| `audit` | `uv run pip-audit --strict`; `npm audit --prefix frontend --omit=dev --audit-level=high` |

The workflow triggers on `push` and `pull_request` and has `permissions: contents: read`. No job uses a provider secret.

- [ ] **Step 2: Lint the workflow**

Run: `docker run --rm -v "$PWD:/repo" -w /repo "$(grep ^actionlint= deploy/images.lock | cut -d= -f2)" -color`
Expected: no output, exit 0

- [ ] **Step 3: Run the workflow commands locally**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest tests/unit tests/property tests/integration tests/crash -q`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci(m0): lint, unit, integration (digest-pinned pgvector, crash cases), frontend and audit jobs"
```

---

### Task 21: M0 acceptance

**Files:**
- Create: `scripts/smoke_m0.py`
- Modify: `docs/superpowers/plans/2026-10-16-job-hunter-roadmap.md` (the M0 status row)

**Interfaces:**
- Consumes: the dev stack, the CLI and the API.
- Produces: `scripts/smoke_m0.py` (exit 0 when every check passes; prints one JSON line per check)

- [ ] **Step 1: Write the smoke script**

`scripts/smoke_m0.py` drives `deploy/compose.dev.yaml` (always with `--env-file deploy/images.lock`) with `subprocess` and `httpx`, using the canonical origin `http://localhost:8080`:
1. `docker compose … run --rm -T api jobhunter user create --username smoke --password-stdin` with stdin `Tannenbaum-Ölfass-7\n`; expect exit 0.
2. `POST http://localhost:8080/api/auth/login` with `Origin: http://localhost:8080`; expect 204. Replay the token from `Set-Cookie` as an explicit `Cookie` header (httpx does not send `Secure` cookies over plain HTTP; this proves the backend session only).
3. Poll `GET /api/system/health` for up to 60 s until `ready.ok is true`.
4. Make a slot due (dev only): `docker compose … exec -T postgres psql -U jobhunter_bootstrap -d jobhunter -c "UPDATE schedule_state SET anchor_utc = date_trunc('minute', now()), interval_s = 3600, last_evaluated_slot = date_trunc('minute', now()) - interval '2 hours'"`.
5. Poll `GET /api/system/health` for up to 150 s until `last_run.status == "succeeded"` and `last_run.trigger == "schedule"`. Then the same psql shows exactly one `missed` run.
6. `POST /api/runs` twice with `Idempotency-Key: smoke-run-now-0001`, `Origin` and `X-JH-Request: 1`; expect two identical 202 bodies.

- [ ] **Step 2: Bring up the stack and run the whole suite**

Run:

```bash
dockerd >/tmp/dockerd.log 2>&1 & sleep 3
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run pytest -q && uv run pytest tests/stack -m stack -q && uv run pytest tests/perf -m perf -q
(cd frontend && npm test && npm run build && npm run lint && npm run typecheck) && scripts/check-openapi-drift.sh
docker compose --env-file deploy/images.lock -f deploy/compose.dev.yaml up -d --build --wait
uv run python scripts/smoke_m0.py
```

Expected: every command exits 0, and the smoke script prints six `{"check": …, "ok": true}` lines.

- [ ] **Step 3: Update the roadmap status**

In `docs/superpowers/plans/2026-10-16-job-hunter-roadmap.md`, change the M0 status cell from `planned` to `done (<short git SHA>)`. Leave the other rows unchanged.

- [ ] **Step 4: Commit**

```bash
git add scripts/smoke_m0.py docs/superpowers/plans/2026-10-16-job-hunter-roadmap.md
git commit -m "test(m0): acceptance smoke test; mark M0 done in the roadmap"
```

---

## Deviations

Each deviation either changes the order in which the spec arrives across milestones, or adds something the spec leaves open. None changes user-visible behaviour of the final system.

1. **Tables and FKs staged across milestones** (chair decision on review #17).
   - **In M0 now:** `active_config` (created and seeded) and the empty `rubric_versions` and `prompt_schema_versions` tables. M1's identity-resolution and hard-filter transactions can therefore take `active_config FOR SHARE` from day one.
   - **Owned by M2:**
     - `active_config.rubric_version_id` and `prompt_schema_version_id` are nullable in M0. M2 seeds the referenced rows, sets the pointers and restores `NOT NULL`.
     - The FKs from `active_config.profile_version_id` and `active_config.llm_tuple_id`, and from `runs.llm_tuple_id`, arrive with `profile_versions` and `llm_tuples`.
   - **Owned by M3:** `score_cycles`.
   - **Owned by M1:** the FK `purge_fences.source_id → sources`.
   - **Owned by M5:** the FK `purge_fences.manifest_id → deletion_manifests`.
2. **`is_fenced()` in M0** covers `global` and `jobs`. M1 adds the `source` branch together with `postings` (its first consumer is M1's `resolve_identity`). M2 adds the `profile` branch together with `llm_tuples` (first consumers: M2's activation and M3's claim). Each replacement is a `CREATE OR REPLACE` ending at the §7.14 body.
3. **Seed ownership, exactly by milestone:**
   - **M0:**
     - `ranking_state`;
     - model policy B, execution config v1, and evaluation policy v1 (`projected_critic_rate` 0.450, the spec's fallback estimate);
     - the default scope version;
     - **settings version 1 with every §13.1 default**, `schema_version = 1`, including the scope, collection, quota, budget and scoring fields;
     - `active_config`, `schedule_state` and the `transition_matrix` rows.
   - **M1:** the `sources` rows and the default `source_instances`.
   - **M2:** rubric v1 (`scoring/rubric/v1.yaml`: §11.2 weights and §11.3 anchors) and the initial prompt-schema version (`llm/prompts`, `llm/schemas`). M2 owns them because M2's profile activation creates `llm_tuples` that reference them. The roadmap lists Rubric v1 under M3; M3 implements the rubric's scoring logic against this seed.
   - **M3:** no new required seed. A recomputed `projected_critic_rate` (§11.7.4) becomes a new immutable `evaluation_policies` version, never an update of version 1.
   - **M2 also adds** the Pydantic settings model, validation, editing, versioning and the effect-class flows. M0 ships the defaults as data, checked field by field against §13.1.
4. **New table `transition_matrix`.** It backs the `assert_transition` trigger that §7.16 describes but gives no DDL for.
5. **`migrate` steps 1, 3 and 7** (identity advancement and manifest replay) are no-ops until M5, which owns restores, the manifest stream and procrastinate schema upgrades (§19.6). M0 applies the pinned 3.10.0 queue schema once, on first install. Step 4 grants only on backup tables that exist.
6. **Later pieces of the takeover sweep, readiness and health.**
   - Takeover sweep steps 3–4 (targets, cycles) arrive in M3.
   - The embedder readiness check arrives in M1.
   - The `/api/system/health` fields for backups, quota, LLM spend, embedder and LangSmith arrive with their subsystems.
   - M0 implements everything that exists now: run ages with thresholds and grace, `schedule.stale_data`, disk and database size, queues, open alerts and the gauge.
   - The api's in-app `worker.heartbeat_stale` alert arrives with the alert catalogue in M5. In M0 the heartbeat state is visible through `/readyz` and `/api/system/health`.
7. **New alert code `reconciler.failed`** (warn, deduplicated per reconciler, resolved on its next success). It is not in the §17.6 catalogue; it makes reconciler failures durable (§17.1).
8. **Layout differences from §4.2.**
   - New: the `auth/` package, `worker/runs.py`, `worker/executions.py`, `api/edge.py`, `db/migrate.py`, `db/seed.py`, `db/sql.py`, `db/alembic_runner.py`, `faults.py`, `crypto.py`, `clock.py`, `ids.py` and `types.py`.
   - No `db/models/`: SQLAlchemy Core `text()` with JSONB-typed binds, and the DDL stays verbatim in the migrations.
9. **New dependencies.** `segno` (†, the terminal QR code that §16.2 requires) and `pip-audit` (†, §16.11) are not in §4.1.
10. **`JOBHUNTER_SECRETS_DIR`** (default `/run/secrets`) points tests and dev at another directory. The same validation applies.
11. **`user create --password-stdin`** exists for scripted bootstrap and is refused in `prod`.
12. **The integration Postgres** is started by a docker-CLI pytest fixture, locally and in CI, instead of a GitHub Actions `services:` container. The init scripts, secrets and config must be mounted, and services cannot mount repository files.
13. **The dev stack uses an HTTP-only Caddy (`devproxy`)** at `172.30.0.2`, with one canonical origin `http://localhost:8080`. Browser cookie behaviour on `http://localhost` is verified in M4 with playwright; production TLS arrives in M5.
14. **Toolchain versions.** The dev container ships uv 0.11.32 and Node 22.22.0. CI pins uv 0.13.1 and Node 22.22.3, which are authoritative; local versions are recorded.
15. **The CI job set for M0** is `lint`, `unit`, `integration`, `frontend` and `audit`. Image digests are recorded now (Task 18). The rest arrives later:
    - `contract` → M1;
    - `payload` → M2;
    - `e2e` (playwright and the network tests N1/N2) → M4 with the UI;
    - the `images` job (image builds plus the embedder checksum) and container vulnerability scanning → M5.
16. **The crash matrix in M0** covers C17, C18, C27, C30 and the ownership part of C35 (fenced heartbeat, owner lock and writes of the stale execution). The LLM dispatch-marker part of C35, C15 (stalled stream, M2) and the other cases arrive with their subsystems in M1–M5.
