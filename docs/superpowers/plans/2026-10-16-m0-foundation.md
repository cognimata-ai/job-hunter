# M0 Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the tested foundation of Job Hunter: toolchain, config with startup assertions, database roles and the M0 schema, the singleton worker with epochs and takeover, durable operations, the procrastinate tick with slot runs, an authenticated API skeleton, logging, metrics, a dev stack and CI.

**Architecture:** One Python package `jobhunter` (in `backend/src/`) serves three roles: `api` (FastAPI), `worker` (procrastinate plus LangGraph) and `migrate` (Alembic plus seeding). PostgreSQL 17 with pgvector holds every piece of state: queue, sessions, throttling, operations, runs and checkpoints. The worker owns a session-level advisory lock on a dedicated thread with a watchdog. Every ownership-sensitive write goes through a CAS `transition()` helper with an ownership predicate.

**Tech Stack:** Python 3.13.16, uv, FastAPI 0.143.2, uvicorn 0.54.0, Pydantic 2.14.1, SQLAlchemy 2.1.4 (Core, async), Alembic 1.20.0, psycopg 3.3.6, psycopg-pool 3.3.3, procrastinate 3.10.0, langgraph 1.2.15, langgraph-checkpoint-postgres 3.2.0, argon2-cffi 25.1.0, pyotp 2.10.0, cryptography 50.0.2, structlog 26.1.0, prometheus-client 0.26.0, pytest 9.1.1, pytest-asyncio 1.4.0, hypothesis 6.170.0, ruff 0.17.1, mypy 2.4.0; PostgreSQL image `pgvector/pgvector:0.8.7-pg17-bookworm`; Node 22, Vite 8.3.4, React 19.3.1, openapi-typescript 7.13.0, vitest 5.0.5.

**Spec:** docs/superpowers/specs/2026-10-16-job-hunter-design.md

## Global Constraints

- Python `3.13.16`. Every dependency is pinned exactly in `uv.lock` / `package-lock.json`. A version marked † in §4.1 is re-confirmed at the first `uv lock` / `npm install`, and a differing resolution is recorded in the commit message.
- Exact pins (§4.1): `fastapi==0.143.2`, `uvicorn==0.54.0`, `pydantic==2.14.1`, `sqlalchemy==2.1.4`, `alembic==1.20.0`, `psycopg[binary]==3.3.6`, `psycopg-pool==3.3.3`, `procrastinate==3.10.0`, `langgraph==1.2.15`, `langgraph-checkpoint-postgres==3.2.0`, `langchain-core==1.6.11`, `argon2-cffi==25.1.0`, `pyotp==2.10.0`, `cryptography==50.0.2`, `structlog==26.1.0`, `prometheus-client==0.26.0`, `httpx==0.28.1`, `pytest==9.1.1`, `pytest-asyncio==1.4.0`, `hypothesis==6.170.0`, `ruff==0.17.1`, `mypy==2.4.0`.
- `mypy --strict` passes on `backend/src`. `tests` are type-checked too, with untyped test functions allowed. `ruff check` and `ruff format --check` pass.
- Broad `except Exception` is allowed **only** at task boundaries (ruff `BLE001` with per-file allowlists). Swallowing an exception without persisting an outcome is a review-blocking defect (§17.1).
- State names, table names and column names are exactly those of §1.6 and §7. Enumerations are `text` + `CHECK`, never PG enums. Every timestamp is `timestamptz`, and Python datetimes are timezone-aware (ruff `DTZ`).
- Every state change goes through `transition()` (§7.16). Set-based statements that change a state column are forbidden; sweeps loop over rows and call `transition()`.
- Global lock order (§3.3.3): `active_config` → job rows → owner row → `llm_budget` → `quota_windows` → ledger rows → other domain rows → `ranking_state`.
- No secret or PII in logs, problem details, metrics or fixtures. The structlog redaction processor runs **last** (§16.8).
- Advisory lock key `7261001`. The worker heartbeat runs every 10 s with a 5 s statement timeout and is stale after 60 s. The watchdog exit code is `70`. A startup-assertion failure exits with `78`.
- Run, operation and score-cycle heartbeats: every 30 s, stale after 3 min (§3.3.4).
- Fault hooks (`JOBHUNTER_FAULT`) act only when `JOBHUNTER_ENV=test` and are refused in `prod`.
- Integration and crash tests use the real `pgvector/pgvector:0.8.7-pg17-bookworm` container started by the session fixture of Task 4. They never use the system PostgreSQL 16, which lacks pgvector 0.8.
- No test and no CI job makes a paid API call.
- `Idempotency-Key` matches `^[A-Za-z0-9_-]{16,128}$`. The session cookie is `__Host-jh_session` with `Secure; HttpOnly; SameSite=Strict; Path=/`.
- Problem responses use `application/problem+json`, with `type = "https://jobhunter.local/problems/<code>"` (§14.4).
- Commits use Conventional Commits with the scope `m0`, for example `feat(m0): …`.

## Review Focus

1. **Database restart under a running stack** (a Postgres upgrade or OOM restart). `api` serves again after the database returns, without a restart, and `/readyz` reports `db` while it is down. The worker exits `70` and its replacement resumes. Pinned by Task 14 (`test_api_recovers_after_db_restart`) and Task 8 (`test_watchdog_exits_on_db_restart`).
2. **Long downtime** (the VPS was off for days). The first tick inserts every missed slot plus exactly one `catch_up` run in one bounded transaction, and a second tick changes nothing. Pinned by Task 11 (`test_long_downtime_is_bounded_and_once_only`).
3. **Unicode and whitespace in credentials.** A password typed as NFD on one keyboard and NFC on another verifies. A username with surrounding spaces or in a different case logs in. Pinned by Task 15 (`test_nfc_and_nfd_passwords_verify_alike`, `test_username_match_ignores_case_and_outer_spaces`).
4. **Run now racing the tick at the same instant.** There is exactly one active collection, both callers see it, and neither gets a 500. Pinned by Task 17 (`test_run_now_races_tick`).
5. **The wall clock stepping backwards** (an NTP correction or a VM snapshot resume). The tick inserts nothing, never moves its cursor backwards, and resumes normally once time catches up. Pinned by Task 11 (`test_tick_with_clock_behind_cursor_is_noop`).

---

## File Structure

The paths follow §4.2 for the M0 subset. New modules that §4.2 does not list are marked *(new)*.

| Path | Responsibility |
|---|---|
| `pyproject.toml`, `uv.lock`, `ruff.toml`, `mypy.ini`, `.python-version`, `.gitignore` | Toolchain, pins, lint rules (banned API, BLE001 allowlist), strict typing |
| `backend/src/jobhunter/__init__.py` | `__version__` |
| `backend/src/jobhunter/config.py` | Env and secret loading, `AppConfig`, the startup assertions of §16.8 |
| `backend/src/jobhunter/faults.py` *(new)* | Crash-injection hooks for the test env |
| `backend/src/jobhunter/types.py`, `clock.py`, `ids.py`, `errors.py`, `crypto.py` *(new)* | `JSONValue`; `utcnow()`; `uuid7()`; domain exceptions; HKDF and AES-256-GCM with key ids |
| `backend/src/jobhunter/cli.py` | argparse CLI: `startup-check`, `migrate`, `worker`, `worker-health`, `http-check`, `user …`, `sessions …` |
| `backend/src/jobhunter/observability/{logging,redact,metrics,alerts,health}.py` | structlog JSON with redaction last; Prometheus registry; alert raise/resolve with dedup; readiness and system health |
| `backend/src/jobhunter/db/session.py` | Async and sync SQLAlchemy engines with pre-ping |
| `backend/src/jobhunter/db/transitions.py` | Transition metadata registry and CAS `transition()` |
| `backend/src/jobhunter/db/migrations/{env.py,script.py.mako,versions/}` | Alembic: `0001_m0_baseline`, `0002_m0_transition_matrix` |
| `backend/src/jobhunter/db/migrate.py`, `seed.py`, `seed_data/*.json` *(new)* | The 7-step `migrate` order of §19.3; idempotent seeds |
| `backend/src/jobhunter/graphs/checkpointer.py` | Dedicated autocommit pool, `AsyncPostgresSaver`, `setup` |
| `backend/src/jobhunter/graphs/collect/graph.py` | M0 no-op `collect_graph` (`start` → `finalize`) |
| `backend/src/jobhunter/worker/lock.py`, `watchdog.py`, `epoch.py` | Lock session, standby and takeover, heartbeat watchdog, epoch claim with takeover sweeps |
| `backend/src/jobhunter/worker/tasks.py` | procrastinate blueprint, tasks, periodic schedules, in-transaction defer, stalled-job release |
| `backend/src/jobhunter/worker/reconcilers.py` | Reconciler registry and runner, plus the M0 reconcilers |
| `backend/src/jobhunter/worker/heartbeat.py` | Generic 30 s owner heartbeat loop |
| `backend/src/jobhunter/worker/operations.py` | Durable operations with claim tokens and the handler registry |
| `backend/src/jobhunter/worker/tick.py` | Slot arithmetic and the tick transaction |
| `backend/src/jobhunter/worker/runs.py` *(new)* | Run attempts, finalize, new attempts, Run-now service |
| `backend/src/jobhunter/worker/shutdown.py`, `main.py` | Graceful shutdown and worker composition |
| `backend/src/jobhunter/privacy/retention.py` | `retention_daily` rules for the M0 tables |
| `backend/src/jobhunter/auth/{passwords,users,sessions,throttle,totp}.py`, `auth/common_passwords.txt` *(new)* | Argon2id, the single user, sessions, DB-backed throttling, TOTP |
| `backend/src/jobhunter/api/{app,errors,security,deps,openapi_export}.py`, `api/routes/{health,auth,runs,system}.py` | App factory, RFC 9457, headers, CSRF, rate limit, routes |
| `backend/src/jobhunter/api_idempotency.py` | The `Idempotency-Key` store |
| `deploy/postgres/init/10-init.sh`, `deploy/postgres/postgresql.conf` | Roles, extensions, template DB, default privileges; server settings of §19.1 |
| `Dockerfile`, `deploy/compose.dev.yaml`, `deploy/Caddyfile.dev`, `scripts/dev-secrets.sh` | App image, dev stack, dev secrets (gitignored output) |
| `frontend/{package.json,tsconfig.json,vite.config.ts,eslint.config.js,index.html}`, `frontend/src/{main.tsx,api/client.ts,api/schema.d.ts}` | SPA skeleton, typed client, generated OpenAPI types |
| `.github/workflows/ci.yml` | lint, unit, integration, frontend, audit |
| `scripts/smoke_m0.py` | Scripted acceptance smoke test |
| `tests/support/{pg.py,fixtures.py,procs.py}`, `tests/{unit,property,integration,crash,stack}/` | Container harness, fixtures, subprocess helpers, tests |
| `docs/spikes/V1.md`, `V2.md`, `V6.md`, `V16.md`, `V20.md`, `V21.md` | Spike records |

---

### Task 1: Repository scaffold and toolchain

**Files:**
- Create: `pyproject.toml`, `ruff.toml`, `mypy.ini`, `.python-version`, `.gitignore`, `backend/src/jobhunter/__init__.py`, `backend/src/jobhunter/py.typed`, `tests/__init__.py`, `tests/conftest.py`
- Test: `tests/unit/test_toolchain.py`

**Interfaces:**
- Consumes: nothing.
- Produces: the package `jobhunter` with `__version__: str = "0.1.0"`; the console script `jobhunter = "jobhunter.cli:main"`; pytest markers `integration`, `crash`, `stack`, `slow`; `asyncio_mode = "auto"`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_toolchain.py
import re, subprocess
from pathlib import Path
import pytest
REPO = Path(__file__).resolve().parents[2]

def test_package_version() -> None:
    import jobhunter
    assert re.fullmatch(r"\d+\.\d+\.\d+", jobhunter.__version__)

@pytest.mark.parametrize(("source", "rule"), [
    ("import anthropic\nanthropic.Anthropic()\n", "TID251"),
    ("from langchain_anthropic import ChatAnthropic\n", "TID251"),
    ("def f() -> None:\n    try:\n        pass\n    except Exception:\n        pass\n", "BLE001"),
    ("import datetime\ndatetime.datetime.now()\n", "DTZ005"),
])
def test_ruff_rules_fire(tmp_path: Path, source: str, rule: str) -> None:
    target = tmp_path / "probe.py"
    target.write_text(source)
    result = subprocess.run(["ruff", "check", "--config", str(REPO / "ruff.toml"), "--no-cache", str(target)],
                            capture_output=True, text=True)
    assert result.returncode == 1
    assert rule in result.stdout
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_toolchain.py -q`
Expected: FAIL, because `pyproject.toml` does not exist yet (uv error) or with `ModuleNotFoundError: No module named 'jobhunter'`.

- [ ] **Step 3: Create the toolchain files**

- `pyproject.toml`:
  - `[project] name = "jobhunter"`, `requires-python = "==3.13.*"`, the runtime pins of the Global Constraints plus `segno` (†, terminal QR for TOTP);
  - `[dependency-groups] dev =` pytest, pytest-asyncio, hypothesis, ruff, mypy, httpx, `pip-audit` (†);
  - `[project.scripts] jobhunter = "jobhunter.cli:main"`;
  - hatchling build with `packages = ["backend/src/jobhunter"]`;
  - `[tool.pytest.ini_options]`: `asyncio_mode = "auto"`, `asyncio_default_fixture_loop_scope = "function"`, `testpaths = ["tests"]`, the four markers, `addopts = "-m 'not stack'"`.
- `ruff.toml`:
  - `target-version = "py313"`, `line-length = 110`;
  - `select = ["E","F","W","I","B","BLE","UP","S","TID","ASYNC","RUF","DTZ"]`;
  - `[lint.flake8-tidy-imports.banned-api]` for `anthropic.Anthropic`, `anthropic.AsyncAnthropic` and `langchain_anthropic.ChatAnthropic`, message `"Only llm/gateway.py may reach Claude (§10.1)"`;
  - per-file-ignores: `backend/src/jobhunter/llm/**` = `TID251`; the task-boundary files `backend/src/jobhunter/worker/{tasks,reconcilers,main,watchdog,operations,runs}.py` and `backend/src/jobhunter/api/errors.py` = `BLE001`; `tests/**` = `S101,S603,S607`.
- `mypy.ini`: `strict = True`, `files = backend/src, tests`, `plugins = pydantic.mypy`, `[mypy-pyotp.*] ignore_missing_imports = True`, `[mypy-tests.*] disallow_untyped_defs = False` and `disallow_incomplete_defs = False`.
- `.python-version` = `3.13.16`.
- `.gitignore`: `.venv/`, `__pycache__/`, `.secrets/`, `frontend/node_modules/`, `frontend/dist/`, `frontend/openapi.json`.
- `tests/conftest.py`: `pytest_plugins = ["tests.support.fixtures"]`. Create `tests/support/__init__.py` and an empty `tests/support/fixtures.py`.
- Then run `uv lock`. If `uv --version` is not 0.13.1, run `uv self update 0.13.1`; if that fails, keep the installed uv and note its version in the commit message.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv sync && uv run pytest tests/unit/test_toolchain.py -q && uv run mypy && uv run ruff check .`
Expected: PASS (5 passed); mypy `Success: no issues found`; ruff `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock ruff.toml mypy.ini .python-version .gitignore backend tests
git commit -m "feat(m0): repository scaffold, pinned toolchain, lint and type rules"
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
  - `@dataclass(frozen=True, slots=True) class AppConfig`: `role: Role`, `env: EnvName`, `db: DbSettings`, `public_origin: str | None`, `log_level: str`, `fault: str | None`, `secrets: Mapping[str, bytes]`, `environ: Mapping[str, str]`; methods `secret(name: str) -> bytes`, `secret_b64(name: str) -> bytes`, `dsn(driver: Literal["sqlalchemy", "psycopg"]) -> str`
  - `ALL_SECRETS: Final[frozenset[str]]` (the 21 names of §19.2); `ROLE_SECRETS: Final[Mapping[Role, RoleSecrets]]`; `@dataclass(frozen=True) class RoleSecrets: required: frozenset[str]; optional: frozenset[str]`
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
from jobhunter.config import StartupCheckFailed, load_config, run_startup_assertions

def b64(n: int, fill: bytes = b"k") -> bytes: return base64.b64encode(fill * n)

def write_secrets(d: Path, **files: bytes) -> Path:
    d.mkdir(exist_ok=True)
    for name, value in files.items():
        p = d / name; p.write_bytes(value); p.chmod(0o440)
    return d

BASE_ENV = {"JOBHUNTER_ENV": "test", "JOBHUNTER_DB_HOST": "h", "JOBHUNTER_DB_PORT": "5432",
            "JOBHUNTER_DB_NAME": "jobhunter", "JOBHUNTER_PUBLIC_ORIGIN": "https://testserver"}
API_SECRETS = {"session_secret": b64(32, b"s"), "data_key": b64(32, b"d"), "db_app": b"pw"}

def check_of(role, env, secrets_dir) -> str:
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
    ("worker", {}, {"data_key": b64(32), "db_app": b"pw", "anthropic_key": b"lsv2_abc"}, "anthropic_key_is_langsmith"),
    ("worker", {"JOBHUNTER_ENV": "prod", "JOBHUNTER_FAKE_LLM": "1"}, {"data_key": b64(32), "db_app": b"pw"}, "fake_mode_in_prod"),
    ("worker", {"JOBHUNTER_ENV": "prod", "JOBHUNTER_FAULT": "x"}, {"data_key": b64(32), "db_app": b"pw"}, "fake_mode_in_prod"),
    ("worker", {"LANGSMITH_TRACING": "true", "LANGSMITH_HIDE_INPUTS": "true", "LANGSMITH_HIDE_OUTPUTS": "true",
                "LANGSMITH_ENDPOINT": "https://api.smith.langchain.com"}, {"data_key": b64(32), "db_app": b"pw"}, "langsmith_config"),
    ("api", {}, {**API_SECRETS, "session_secret": b64(31, b"s")}, "session_secret_length"),
    ("api", {}, {**API_SECRETS, "data_key": b64(33, b"d")}, "data_key_length"),
    ("api", {}, {**API_SECRETS, "data_key_previous": b64(16)}, "data_key_previous_length"),
    ("api", {}, {**API_SECRETS, "data_key": b64(32, b"s")}, "keys_identical"),
    ("worker", {"JOBHUNTER_ENV": "prod"}, {"data_key": b64(32), "db_app": b"pw"}, "healthchecks_in_prod"),
    ("api", {"JOBHUNTER_ENV": "prod", "ANTHROPIC_BASE_URL": "http://proxy"}, API_SECRETS, "anthropic_env_in_prod"),
])
def test_assertion_failures(tmp_path: Path, role, env_extra, secrets, expected) -> None:
    assert check_of(role, {**BASE_ENV, **env_extra}, write_secrets(tmp_path / "s", **secrets)) == expected

def test_world_readable_secret_fails(tmp_path: Path) -> None:
    d = write_secrets(tmp_path / "s", **API_SECRETS); (d / "db_app").chmod(0o444)
    assert check_of("api", BASE_ENV, d) == "secrets_not_world_readable"

def test_cli_exits_78_and_never_logs_the_value(tmp_path: Path) -> None:
    secret = b64(20, b"Z")                     # too short → session_secret_length
    d = write_secrets(tmp_path / "s", **{**API_SECRETS, "session_secret": secret})
    env = {**os.environ, **BASE_ENV, "JOBHUNTER_SECRETS_DIR": str(d)}
    r = subprocess.run([sys.executable, "-m", "jobhunter.cli", "startup-check", "--role", "api"],
                       env=env, capture_output=True, text=True)
    assert r.returncode == 78
    assert "session_secret_length" in r.stdout
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
- The secrets directory is `JOBHUNTER_SECRETS_DIR` (default `/run/secrets`).
- It reads every file of that directory whose name is in `ALL_SECRETS`, keeping the bytes with one trailing newline stripped and recording the file mode.
- `environ` keeps only names that start with `JOBHUNTER_`, `LANGSMITH_` or `ANTHROPIC_`.
- It raises `StartupCheckFailed("env_valid")` when `JOBHUNTER_ENV ∉ {prod, dev, test}`.

`ROLE_SECRETS`:

| Role | Required | Optional |
|---|---|---|
| `api` | `session_secret`, `data_key`, `db_app` | `data_key_previous` |
| `worker` | `data_key`, `db_app` | `data_key_previous`, `anthropic_key`, `rapidapi_key`, `serpapi_key`, `adzuna_app_id`, `adzuna_app_key`, `langsmith_key`, `ntfy_token`, `healthchecks_ping_url`, `healthchecks_api_key` |
| `migrate` | `db_migrator` | — |

`run_startup_assertions` checks run in this order, and the first failure raises with its name:
1. `secrets_present`, `secrets_readable`, `secrets_not_world_readable` (`mode & 0o004`), `foreign_secret_mounted` (a file in `ALL_SECRETS` that is neither required nor optional);
2. `langsmith_gateway_set`, `anthropic_key_is_langsmith` (prefix `lsv2_`);
3. `fake_mode_in_prod` (`JOBHUNTER_FAKE_SOURCES`, `JOBHUNTER_FAKE_LLM` or `JOBHUNTER_FAULT` set in prod);
4. `langsmith_config`: when `LANGSMITH_TRACING == "true"`, `HIDE_INPUTS` and `HIDE_OUTPUTS` must be `"true"` and `ENDPOINT` must equal `https://eu.api.smith.langchain.com`;
5. `session_secret_length` (base64 decodes to ≥ 32 bytes), `data_key_length` (exactly 32), `data_key_previous_length` (exactly 32 when present), `keys_identical`;
6. `healthchecks_in_prod`: a prod worker has both `healthchecks_ping_url` and `healthchecks_api_key`;
7. `anthropic_env_in_prod`: `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_URL` or `ANTHROPIC_API_KEY` set in prod.

`dsn`:
- The user is `jobhunter_app` for `api` and `worker`, and `jobhunter_migrator` for `migrate`. The password is the secret `db_app` or `db_migrator`, URL-quoted with `urllib.parse.quote(..., safe="")`.
- `"sqlalchemy"` gives the prefix `postgresql+psycopg://`; `"psycopg"` gives `postgresql://`.

`faults`:
- `JOBHUNTER_FAULT` has the form `<point>[:pause|:sleep=<seconds>]`.
- In the test env, a match runs `os._exit(86)` by default, or `os.kill(os.getpid(), SIGSTOP)` for `pause`. `sleep` applies only to `afault_point`, which uses `await asyncio.sleep(s)`.

`cli.main`: an argparse subparser `startup-check` that calls `startup_or_exit(role)` and returns 0. Logging is plain JSON on stdout for now (`{"event": "startup_check_failed", "check": ...}`); Task 3 replaces it. `if __name__ == "__main__": raise SystemExit(main())`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_config.py tests/unit/test_faults.py -q`
Expected: PASS (21 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/src/jobhunter/config.py backend/src/jobhunter/faults.py backend/src/jobhunter/cli.py tests/unit/test_config.py tests/unit/test_faults.py
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
  - `metrics.REGISTRY: CollectorRegistry`, `RUNS` (`Counter("jobhunter_runs", …, ["status"])`), `RUN_DURATION` (`Histogram("jobhunter_run_duration_seconds")`), `HTTP_REQUESTS` (`Counter("jobhunter_http_requests", …, ["route", "status"])`), `TABLE_BYTES` (`Gauge("jobhunter_table_bytes", …, ["table"])`), `ALERTS_OPEN` (`Gauge("jobhunter_alerts_open", …, ["severity"])`); `metrics.start_metrics_server(port: int) -> None`

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
    rid = str(uuid7())
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
    assert [u.int >> 80 for u in ids] == sorted(u.int >> 80 for u in ids)
    assert len(set(ids)) == 1000
```

```python
# tests/unit/test_metrics.py
from prometheus_client import generate_latest
from jobhunter.observability import metrics
def test_series_names() -> None:
    metrics.RUNS.labels(status="succeeded").inc()
    metrics.HTTP_REQUESTS.labels(route="/healthz", status="200").inc()
    text = generate_latest(metrics.REGISTRY).decode()
    for name in ("jobhunter_runs_total", "jobhunter_run_duration_seconds", "jobhunter_http_requests_total",
                 "jobhunter_table_bytes", "jobhunter_alerts_open"):
        assert name in text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_redact.py tests/unit/test_ids.py tests/unit/test_metrics.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'jobhunter.ids'`

- [ ] **Step 3: Implement the modules**

- **`REDACT_KEYS`** (matched case-insensitively): `authorization`, `cookie`, `set-cookie`, `x-rapidapi-key`, `api_key`, `app_key`, `app_id`, `password`, `token`, `secret`, `redirect_url`, `outbound_url_private`, `stripped_text`, `reviewed_text`.
- **`REDACT_PREFIXES`**: `description`, `profile`, `cv`, `raw_text`.
- **`redact_event`** recurses into dicts and lists. It applies `mask_text` to every string value, except under the keys `ts` and `level`.
- **`mask_text` patterns**, applied in this order to the text **outside** canonical UUID spans (`[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}`), which are never masked:

```python
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE = re.compile(r"(?<![\w+\-])(?:\+\d{1,3}[ \d/()\-]{6,}\d|0\d{2,5}[ /\-]\d{3,}(?:[ \-]\d{2,})*)(?!\w)")
KEY   = re.compile(r"\b(?:sk-[A-Za-z0-9_\-]{20,}|lsv2_[A-Za-z0-9_]{20,}|[A-Za-z0-9+/_\-]{40,}={0,2})")
```

- **`configure_logging` processor chain**: `merge_contextvars`, `add_log_level`, `add_logger_name`, `TimeStamper(fmt="iso", utc=True, key="ts")`, `format_exc_info`, `redact_event`, `JSONRenderer()`. `redact_event` is the last processor before the renderer.
- **`uuid7`** follows RFC 9562: 48-bit Unix milliseconds, version `0b0111`, 12 random bits, variant `0b10`, 62 random bits. Within one millisecond, increment the 12-bit counter so ids stay ordered; draw new random bits when the counter overflows.
- **`start_metrics_server`** wraps `prometheus_client.start_http_server(port, registry=REGISTRY)`.
- **`startup_or_exit`** now logs `startup_check_failed` with `check=` through `get_logger`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit -q`
Expected: PASS (all unit tests, including those of Task 2)

- [ ] **Step 5: Commit**

```bash
git add backend/src/jobhunter tests/unit
git commit -m "feat(m0): structlog JSON logging with last-stage redaction, uuid7, metrics registry"
```

---

### Task 4: PostgreSQL container harness, roles and server settings

**Files:**
- Create: `deploy/postgres/init/10-init.sh`, `deploy/postgres/postgresql.conf`, `tests/support/pg.py`, `tests/support/fixtures.py`, `tests/integration/__init__.py`, `tests/integration/test_pg_init.py`
- Test: `tests/integration/test_pg_init.py`

**Interfaces:**
- Consumes: `config.load_config`.
- Produces:
  - `pg.PG_IMAGE: Final = "pgvector/pgvector:0.8.7-pg17-bookworm"`
  - `@dataclass(frozen=True) class PgServer`: `container: str`, `host: str`, `port: int`, `passwords: Mapping[str, str]`, `workdir: Path`; methods:
    - `dsn(role: Literal["superuser","migrator","app","backup","restore"], db: str = "jobhunter", *, driver: Literal["psycopg","sqlalchemy"] = "psycopg") -> str`;
    - `secrets_dir(role: Role) -> Path` (a per-role directory: `api` = `session_secret, data_key, db_app`; `worker` = `data_key, db_app`; `migrate` = `db_migrator`);
    - `logs() -> str`.
  - `config_for(server: PgServer, role: Role, **env: str) -> AppConfig` (used by session fixtures, which cannot depend on the function-scoped `make_config`)
  - `start_pg_server(workdir: Path) -> PgServer`; `remove_pg_server(server: PgServer) -> None` (`docker rm -f`); `pg_stop(server: PgServer) -> None` (`docker stop`); `pg_start(server: PgServer) -> None` (`docker start` and wait); `restart_pg_server(server: PgServer) -> None` (`docker restart` and wait)
  - Fixtures:
    - `pg_server` (session) and `fresh_pg_server` (function);
    - `make_config(role: Role, server: PgServer | None = None, **env: str) -> AppConfig`. It defaults to the session server, and its env has `JOBHUNTER_ENV=test`, the server's host and port, `JOBHUNTER_PUBLIC_ORIGIN=https://testserver` and `JOBHUNTER_SECRETS_DIR=server.secrets_dir(role)`;
    - `proc_env(role: Role, server: PgServer | None = None, **extra: str) -> dict[str, str]`, which returns `{**make_config(role, server).environ, "JOBHUNTER_SECRETS_DIR": …, **extra}` for subprocesses.

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_pg_init.py
import psycopg, pytest
from tests.support.pg import PgServer
pytestmark = pytest.mark.integration

def q(server: PgServer, role: str, sql: str, db: str = "jobhunter") -> list[tuple[object, ...]]:
    with psycopg.connect(server.dsn(role, db), autocommit=True) as c:
        return c.execute(sql).fetchall()

def test_roles(pg_server: PgServer) -> None:
    rows = dict(q(pg_server, "superuser", "SELECT rolname, rolsuper OR rolcreaterole FROM pg_roles "
                  "WHERE rolname LIKE 'jobhunter_%' AND rolname <> 'jobhunter_bootstrap'"))
    assert rows == {"jobhunter_migrator": False, "jobhunter_app": False,
                    "jobhunter_backup": False, "jobhunter_restore": False}
    assert q(pg_server, "superuser", "SELECT rolcreatedb FROM pg_roles WHERE rolname='jobhunter_restore'") == [(True,)]
    assert q(pg_server, "superuser", "SELECT pg_has_role('jobhunter_backup','pg_read_all_data','member')") == [(True,)]
    assert q(pg_server, "superuser",
             "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname='jobhunter'") == [("jobhunter_migrator",)]

@pytest.mark.parametrize("db", ["jobhunter", "jobhunter_template"])
def test_extensions(pg_server: PgServer, db: str) -> None:
    ext = dict(q(pg_server, "superuser", "SELECT extname, extversion FROM pg_extension", db))
    assert ext["vector"] == "0.8.7" and "pg_trgm" in ext and "unaccent" in ext
    assert q(pg_server, "superuser", "SELECT datistemplate FROM pg_database WHERE datname='jobhunter_template'") == [(True,)]

def test_default_privileges_cover_later_tables(pg_server: PgServer) -> None:
    q(pg_server, "migrator", "CREATE TABLE probe_dp (id int PRIMARY KEY, v text)")
    try:
        q(pg_server, "app", "INSERT INTO probe_dp VALUES (1,'a')")
        q(pg_server, "app", "UPDATE probe_dp SET v='b'")
        assert q(pg_server, "app", "SELECT v FROM probe_dp") == [("b",)]
        q(pg_server, "app", "DELETE FROM probe_dp")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            q(pg_server, "app", "CREATE TABLE app_ddl (x int)")
    finally:
        q(pg_server, "migrator", "DROP TABLE probe_dp")

def test_restore_role_scratch_db_and_no_prod_read(pg_server: PgServer) -> None:
    q(pg_server, "restore", "CREATE DATABASE jh_scratch TEMPLATE jobhunter_template", db="postgres")
    q(pg_server, "restore", "DROP DATABASE jh_scratch", db="postgres")
    q(pg_server, "migrator", "CREATE TABLE IF NOT EXISTS probe_ro (x int)")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        q(pg_server, "restore", "SELECT * FROM probe_ro")
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
            with pytest.raises(psycopg.errors.UniqueViolation):
                c.execute("INSERT INTO l1_probe VALUES (%s, %s)", (email, phone))
        logs = pg_server.logs()
        assert "duplicate key" in logs
        assert email not in logs and "170 1234567" not in logs
    finally:
        q(pg_server, "migrator", "DROP TABLE l1_probe")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `dockerd >/tmp/dockerd.log 2>&1 & sleep 3; uv run pytest tests/integration/test_pg_init.py -q`
Expected: FAIL with `fixture 'pg_server' not found`

- [ ] **Step 3: Implement the init script, the server config and the harness**

`deploy/postgres/postgresql.conf`:
- the values of §19.1 verbatim (`shared_buffers=2GB`, `effective_cache_size=6GB`, `work_mem=32MB`, `maintenance_work_mem=512MB`, `max_connections=60`, `idle_in_transaction_session_timeout=60s`, `tcp_keepalives_idle=30`, `tcp_keepalives_interval=10`, `tcp_keepalives_count=3`, and the six logging settings);
- plus `listen_addresses='*'` and `logging_collector=off`, so the logs go to stderr and `docker logs` shows them.

`deploy/postgres/init/10-init.sh` (bash, `set -euo pipefail`):
- It runs `psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB"`, with every password passed as a psql variable read from `/run/secrets/<name>` (`-v app_pw="$(cat /run/secrets/db_app)"`) and used as `PASSWORD :'app_pw'` in heredoc SQL. Passwords are never interpolated into shell strings.
- Statements, in order:
  1. `CREATE ROLE jobhunter_migrator LOGIN PASSWORD …`, the same for `jobhunter_app` and `jobhunter_backup`, and `CREATE ROLE jobhunter_restore LOGIN CREATEDB PASSWORD …`;
  2. `ALTER DATABASE :"POSTGRES_DB" OWNER TO jobhunter_migrator`;
  3. `CREATE EXTENSION vector; CREATE EXTENSION pg_trgm; CREATE EXTENSION unaccent;`;
  4. `CREATE DATABASE jobhunter_template`; `\c jobhunter_template` and the same three extensions; `ALTER DATABASE jobhunter_template IS_TEMPLATE true`; `\c :"POSTGRES_DB"`;
  5. the two `ALTER DEFAULT PRIVILEGES FOR ROLE jobhunter_migrator …` statements of §19.3;
  6. `GRANT pg_read_all_data TO jobhunter_backup`;
  7. `REVOKE CREATE ON SCHEMA public FROM PUBLIC`.

`tests/support/pg.py`:
- `start_pg_server` writes random hex passwords for `pg_bootstrap`, `db_app`, `db_migrator`, `db_backup` and `db_restore` into `workdir/pgsecrets` (mode 0444, readable by the container's uid 999).
- It writes the per-role app secret directories `workdir/secrets/{api,worker,migrate}` (files mode 0440): `session_secret` (base64 of 32 random bytes) and `data_key` (base64 of 32 random bytes), shared by the roles that receive them, plus copies of `db_app` and `db_migrator`.
- It picks a free host port by binding `127.0.0.1:0` and closing the socket, so `docker stop`, `start` and `restart` keep the port.
- It then runs (without `--rm`, so a stopped container keeps its data):

```
docker run -d --name jh-test-pg-<hex> -p 127.0.0.1:<port>:5432 \
  -e POSTGRES_USER=jobhunter_bootstrap -e POSTGRES_DB=jobhunter -e POSTGRES_PASSWORD_FILE=/run/secrets/pg_bootstrap \
  -v <pgsecrets>:/run/secrets:ro -v <repo>/deploy/postgres/init:/docker-entrypoint-initdb.d:ro \
  -v <repo>/deploy/postgres/postgresql.conf:/etc/postgresql/postgresql.conf:ro \
  pgvector/pgvector:0.8.7-pg17-bookworm postgres -c config_file=/etc/postgresql/postgresql.conf \
  -c shared_buffers=128MB -c effective_cache_size=512MB -c max_connections=150
```

- It waits until a `psycopg.connect` as `jobhunter_app` succeeds **and** `docker logs` shows `database system is ready to accept connections` twice (the entrypoint restarts the server after init). The timeout is 60 s. `pg_start` and `restart_pg_server` wait until the app role connects again.
- If `docker info` fails, `pytest.fail("start dockerd first: dockerd &")`.
- `restart_pg_server` runs `docker restart <container>` and waits as above (the port is preserved).

`tests/support/fixtures.py`:
- `pg_server` (session; starts, then `remove_pg_server`), `fresh_pg_server` (function), `make_config`, which returns `load_config(role, env, server.secrets_dir(role))`, and `proc_env`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/integration/test_pg_init.py -q`
Expected: PASS (13 passed)

- [ ] **Step 5: Commit**

```bash
git add deploy/postgres tests/support tests/integration
git commit -m "feat(m0): postgres init (roles, extensions, template, default privileges), server log settings, container test harness"
```

---

### Task 5: Baseline schema migration

**Files:**
- Create: `backend/src/jobhunter/db/__init__.py`, `db/migrations/env.py`, `db/migrations/script.py.mako`, `db/migrations/versions/0001_m0_baseline.py`, `backend/src/jobhunter/db/alembic_runner.py`
- Modify: `tests/support/fixtures.py` (fixture `migrated`), `tests/support/pg.py` (`ensure_config_rows`)
- Test: `tests/integration/test_schema.py`

**Interfaces:**
- Consumes: `PgServer`, `make_config`.
- Produces:
  - `alembic_runner.alembic_config(dsn: str) -> alembic.config.Config` (`script_location` = the `migrations` directory, `sqlalchemy.url` = the dsn)
  - `alembic_runner.upgrade_head(dsn: str) -> None`; `alembic_runner.code_head() -> str`
  - Fixture `migrated` (session): `pg_server` after `upgrade_head(server.dsn("migrator", driver="sqlalchemy"))`. Task 6 replaces this with the full `run_migrate`.
  - `tests.support.pg.ensure_config_rows(conn: psycopg.Connection) -> tuple[int, int]`: when `settings_versions` is empty, it inserts one row each into `scope_versions`, `model_policies`, `execution_configs`, `evaluation_policies` and `settings_versions` (random hashes, payload `{}`, version 1). It returns `(settings_version_id, scope_version_id)` of the lowest settings version.

The migration `0001_m0_baseline` contains this DDL **verbatim from the spec** (`op.execute` of static SQL):
- §7.1: the three `CREATE EXTENSION IF NOT EXISTS` statements, `immutable_unaccent`, `german_unaccent`, `ranking_state`;
- §7.2: `users`, `sessions` (with `sessions_expiry`), `login_attempts` (two indexes), `login_lockouts`, `api_idempotency`;
- §7.3: `scope_versions`, `model_policies`, `execution_configs`, `evaluation_policies`, `rubric_versions`, `prompt_schema_versions`, `settings_versions`, `schedule_state`, `operations` (with `operations_live_dedup` and `operations_pending`);
- §7.6: `runs` (all four indexes), `run_attempts`, `worker_heartbeat`;
- §7.14: `alerts` (with `alerts_open_dedup`) and `purge_fences` (with both indexes).

Three FKs are omitted because their referenced tables arrive later. Each gets a `-- FK added in <M>` comment:
- `runs.llm_tuple_id` (FK in M2);
- `purge_fences.source_id` (M1);
- `purge_fences.manifest_id` (M5).

`is_fenced(p_job bigint, p_tuple bigint DEFAULT NULL)` is created with the `global` and `jobs` branches only; M1 and M2 `CREATE OR REPLACE` it to the full §7.14 body.

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_schema.py
import uuid
import psycopg, pytest
from tests.support.pg import PgServer
pytestmark = pytest.mark.integration

M0_TABLES = {"ranking_state", "users", "sessions", "login_attempts", "login_lockouts", "api_idempotency",
    "scope_versions", "model_policies", "execution_configs", "evaluation_policies", "rubric_versions",
    "prompt_schema_versions", "settings_versions", "schedule_state", "operations", "runs", "run_attempts",
    "worker_heartbeat", "alerts", "purge_fences", "alembic_version"}

def sql(server: PgServer, statement: str, role: str = "migrator") -> list[tuple[object, ...]]:
    with psycopg.connect(server.dsn(role)) as c:
        cur = c.execute(statement); rows = cur.fetchall() if cur.description else []
        c.rollback(); return rows

LATER = {"active_config", "llm_tuples", "score_cycles", "sources", "postings", "deletion_manifests"}

def test_tables(migrated: PgServer) -> None:
    tables = {r[0] for r in sql(migrated, "SELECT tablename FROM pg_tables WHERE schemaname='public'")}
    assert M0_TABLES <= tables and not (tables & LATER)

def test_text_search_and_unaccent(migrated: PgServer) -> None:
    assert sql(migrated, "SELECT immutable_unaccent('Ärger Straße')") == [("Arger Strasse",)]
    assert sql(migrated, "SELECT to_tsvector('german_unaccent','Müller') = to_tsvector('german_unaccent','Muller')") == [(True,)]

# ensure_config_rows (tests/support/pg.py): inserts a minimal scope/model/execution/evaluation/settings chain
# when settings_versions is empty and returns (settings_version_id, scope_version_id); works before and after seeding.
RUN = ("INSERT INTO runs (trigger,scheduled_slot,status,include_paid,settings_version_id,scope_version_id"
       "{extra_cols}) VALUES ({trigger},{slot},'{status}',true,{sv},{sc}{extra_vals})")

def run_sql(status: str, trigger: str = "'schedule'", slot: str = "'2026-10-16T06:00Z'", extra_cols: str = "", extra_vals: str = "") -> str:
    return RUN.format(trigger=trigger, slot=slot, status=status, extra_cols=extra_cols, extra_vals=extra_vals,
                      sv="{sv}", sc="{sc}")

@pytest.mark.parametrize(("statements", "sqlstate"), [
    ([run_sql("queued"), run_sql("queued", slot="'2026-10-16T12:00Z'")], "23505"),        # runs_one_active_collect
    ([run_sql("missed"), run_sql("missed")], "23505"),                                      # runs_slot_uq
    ([run_sql("queued", trigger="'manual'")], "23514"),                                     # manual ⇔ slot NULL
    ([run_sql("missed", extra_cols=",attempt_no", extra_vals=",1")], "23514"),
    ([run_sql("running", extra_cols=",attempt_no,started_at", extra_vals=",1,now()")], "23514"),   # no heartbeat
    (["INSERT INTO operations (kind,payload,dedup_key,status) VALUES ('export','{}','e','running')"], "23514"),
    (["INSERT INTO operations (kind,payload,dedup_key,status) VALUES ('export','{}','e','pending')"] * 2, "23505"),
    (["INSERT INTO api_idempotency (route,idem_key,request_sha256,status,expires_at) VALUES ('r','short','\\x00','in_progress',now())"], "23514"),
    (["INSERT INTO users (id,username,password_hash,password_changed_at) VALUES (2,'abc','h',now())"], "23514"),
    (["INSERT INTO login_lockouts VALUES ('user:x', now(), 'r')"], "23514"),
    (["INSERT INTO purge_fences (kind,manifest_id) VALUES ('source',1)"], "23514"),
    (["INSERT INTO purge_fences (kind,manifest_id) VALUES ('jobs',1)"], "23514"),
    (["INSERT INTO schedule_state VALUES (1, now(), 3599, true, NULL, {sv}, now()) ON CONFLICT (id) DO UPDATE SET interval_s = 3599"], "23514"),
    (["INSERT INTO alerts (code,severity,dedup_key,message) VALUES ('a','warn','k','m')"] * 2, "23505"),
])
def test_constraints_reject(migrated: PgServer, statements: list[str], sqlstate: str) -> None:
    from tests.support.pg import ensure_config_rows
    with psycopg.connect(migrated.dsn("migrator")) as c:
        sv, sc = ensure_config_rows(c)
        with pytest.raises(psycopg.Error) as exc:
            for s in statements: c.execute(s.replace("{sv}", str(sv)).replace("{sc}", str(sc)))
        assert exc.value.sqlstate == sqlstate
        c.rollback()

def test_dedup_allows_new_operation_after_finish(migrated: PgServer) -> None:
    with psycopg.connect(migrated.dsn("migrator")) as c:
        c.execute("INSERT INTO operations (kind,payload,dedup_key,status,finished_at) VALUES ('export','{}','e','succeeded',now())")
        c.execute("INSERT INTO operations (kind,payload,dedup_key,status) VALUES ('export','{}','e','pending')")
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
    assert sql(migrated, "SELECT version_num FROM alembic_version") == [(code_head(),)]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/integration/test_schema.py -q`
Expected: FAIL with `fixture 'migrated' not found`

- [ ] **Step 3: Implement the Alembic environment and `0001_m0_baseline`**

- `env.py` uses `sqlalchemy.create_engine(url, poolclass=NullPool)` and online mode only, with `transaction_per_migration=True`.
- Revision ids: `0001_m0_baseline` (no `down_revision`). `downgrade()` raises `NotImplementedError("expand/contract: no downgrades")`.
- The DDL is copied from the spec and must not be retyped; the migration file's header comment names the spec sections.
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
Expected: PASS (19 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/src/jobhunter/db tests/integration/test_schema.py tests/support/fixtures.py
git commit -m "feat(m0): alembic baseline with the M0 tables from §7 verbatim"
```

---

### Task 6: `jobhunter migrate`: step order, grants, queue and checkpointer setup, seeds

**Files:**
- Create: `backend/src/jobhunter/db/migrate.py`, `backend/src/jobhunter/db/seed.py`, `backend/src/jobhunter/db/seed_data/{model_policy_b.json,execution_config_v1.json,evaluation_policy_v1.json,settings_bootstrap.json}`, `backend/src/jobhunter/graphs/__init__.py`, `backend/src/jobhunter/graphs/checkpointer.py`
- Modify: `backend/src/jobhunter/cli.py` (`migrate`), `tests/support/fixtures.py` (`migrated` now calls `run_migrate(config_for(server, "migrate"))`)
- Test: `tests/unit/test_seed_math.py`, `tests/integration/test_migrate.py`

**Interfaces:**
- Consumes: `AppConfig`, `alembic_runner.upgrade_head`, `config.load_config("migrate")`.
- Produces:
  - `MigrateStep = Literal["reconstruct_and_advance", "alembic", "advance_again", "grants", "queue_and_checkpointer", "seed", "replay"]`; `MIGRATE_ORDER: Final[tuple[MigrateStep, ...]]` (in that order)
  - `run_migrate(cfg: AppConfig, *, now: datetime | None = None) -> list[MigrateStep]`
  - `BACKUP_GRANT_TABLES: Final = ("backup_sets", "backup_runs", "manifest_shipments", "manifest_shipment_items")`; `apply_explicit_grants(conn: sqlalchemy.Connection) -> list[str]` (the tables granted)
  - `seed.canonical_json(value: JSONValue) -> bytes` (sorted keys, separators `(",", ":")`, `ensure_ascii=False`, UTF-8); `seed.content_hash(value: JSONValue) -> bytes` (sha256)
  - `seed.local_anchor_to_utc(day: date, hhmm: str, tz: str) -> datetime`; `seed.latest_slot_at_or_before(anchor: datetime, interval_s: int, t: datetime) -> datetime`
  - `seed.seed_defaults(conn: sqlalchemy.Connection, *, now: datetime) -> None` (idempotent)
  - `checkpointer.setup_checkpointer(dsn: str) -> None` (sync wrapper; `autocommit=True, prepare_threshold=0, row_factory=dict_row`)
  - `checkpointer.open_saver(dsn: str) -> AbstractAsyncContextManager[AsyncPostgresSaver]` (pool `min_size=1, max_size=4`, same connection kwargs)
  - CLI `jobhunter migrate` (exit 0 on success)

Seed payloads (exact):
- `model_policy_b.json`: `{"preset":"B","nodes":{"generate":{"model":"claude-sonnet-5-5","effort":"medium"},"revise":{"model":"claude-sonnet-5-5","effort":"medium"},"critic":{"model":"claude-opus-5-5","effort":"medium"},"extract_profile":{"model":"claude-opus-5-5","effort":"medium"},"propose_queries":{"model":"claude-opus-5-5","effort":"medium"}},"critic_light_effort":"low"}`
- `execution_config_v1.json`: `{"generate":{"max_tokens":6000,"max_tokens_retry":12000,"deadline_s":600},"revise":{"max_tokens":6000,"max_tokens_retry":12000,"deadline_s":600},"critic":{"max_tokens":6000,"max_tokens_retry":12000,"deadline_s":600},"extract_profile":{"max_tokens":16000,"max_tokens_retry":32000,"deadline_s":900},"propose_queries":{"max_tokens":8000,"max_tokens_retry":16000,"deadline_s":300}}`
- `evaluation_policy_v1.json`: `{"relevance_threshold":60,"t2_band":5,"t2_straddle_max_coverage":0.7,"t3_min_mid":80,"t8_sample_pct":5,"critic_light_triggers":["T2","T8"]}`, with `version_no=1` and `projected_critic_rate=0.450`
- `settings_bootstrap.json`: `{"bootstrap":true,"schedule":{"enabled":true,"interval_hours":6,"anchor_local":"06:00","include_paid":true},"display":{"timezone":"Europe/Berlin","ui_language":"en"}}`, stored as `settings_versions(version_no=1, schema_version=0, effect_classes='{none}')`
- `scope_versions`: one bootstrap row with payload `{"bootstrap":true}`
- `schedule_state(1)`:
  - `anchor_utc = local_anchor_to_utc(now in Europe/Berlin .date(), "06:00", "Europe/Berlin")`;
  - `interval_s = 21600`;
  - `enabled = true`;
  - `last_evaluated_slot = latest_slot_at_or_before(anchor_utc, 21600, now)`;
  - `settings_version_id` = version 1.
- `ranking_state(1, generation 0)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_seed_math.py
from datetime import UTC, date, datetime
from jobhunter.db.seed import canonical_json, latest_slot_at_or_before, local_anchor_to_utc
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
# tests/integration/test_migrate.py
from datetime import UTC, datetime
import psycopg, pytest
from jobhunter.db.alembic_runner import code_head
from jobhunter.db.migrate import MIGRATE_ORDER, apply_explicit_grants, run_migrate
from jobhunter.graphs.checkpointer import setup_checkpointer
pytestmark = pytest.mark.integration
NOW = datetime(2026, 10, 16, 10, 0, tzinfo=UTC)

def one(server, statement, role="migrator"):
    with psycopg.connect(server.dsn(role)) as c: return c.execute(statement).fetchone()

def test_first_install_order_and_artifacts(fresh_pg_server, make_config) -> None:
    cfg = make_config("migrate", fresh_pg_server)
    assert run_migrate(cfg, now=NOW) == list(MIGRATE_ORDER)
    assert list(MIGRATE_ORDER) == ["reconstruct_and_advance", "alembic", "advance_again", "grants",
                                   "queue_and_checkpointer", "seed", "replay"]
    assert one(fresh_pg_server, "SELECT version_num FROM alembic_version") == (code_head(),)
    for t in ("procrastinate_jobs", "checkpoints", "checkpoint_writes", "checkpoint_blobs"):
        assert one(fresh_pg_server, f"SELECT to_regclass('{t}') IS NOT NULL") == (True,)
    assert one(fresh_pg_server, "SELECT count(*) FROM procrastinate_jobs", role="app") == (0,)

def test_seed_values(fresh_pg_server, make_config) -> None:
    run_migrate(make_config("migrate", fresh_pg_server), now=NOW)
    assert one(fresh_pg_server, "SELECT payload->'nodes'->'critic'->>'model', payload->>'critic_light_effort' FROM model_policies") == ("claude-opus-5-5", "low")
    assert one(fresh_pg_server, "SELECT payload->'extract_profile' FROM execution_configs") == ({"max_tokens": 16000, "max_tokens_retry": 32000, "deadline_s": 900},)
    assert one(fresh_pg_server, "SELECT version_no, projected_critic_rate::text, payload->'critic_light_triggers' FROM evaluation_policies") == (1, "0.450", ["T2", "T8"])
    assert one(fresh_pg_server, "SELECT version_no, schema_version, effect_classes FROM settings_versions") == (1, 0, ["none"])
    assert one(fresh_pg_server, "SELECT anchor_utc, interval_s, enabled, last_evaluated_slot FROM schedule_state") == (
        datetime(2026, 10, 16, 4, tzinfo=UTC), 21600, True, datetime(2026, 10, 16, 10, tzinfo=UTC))
    assert one(fresh_pg_server, "SELECT generation FROM ranking_state") == (0,)

def test_migrate_is_idempotent(fresh_pg_server, make_config) -> None:
    cfg = make_config("migrate", fresh_pg_server); run_migrate(cfg, now=NOW); run_migrate(cfg, now=datetime(2026, 10, 17, tzinfo=UTC))
    counts = one(fresh_pg_server, "SELECT (SELECT count(*) FROM model_policies), (SELECT count(*) FROM settings_versions), "
                 "(SELECT count(*) FROM scope_versions), (SELECT anchor_utc FROM schedule_state)")
    assert counts == (1, 1, 1, datetime(2026, 10, 16, 4, tzinfo=UTC))

def test_grants_step_skips_absent_tables(migrated) -> None:
    import sqlalchemy as sa
    with sa.create_engine(migrated.dsn("migrator", driver="sqlalchemy")).begin() as conn:
        assert apply_explicit_grants(conn) == []

def test_checkpointer_setup_is_idempotent(migrated) -> None:
    setup_checkpointer(migrated.dsn("migrator")); setup_checkpointer(migrated.dsn("migrator"))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_seed_math.py tests/integration/test_migrate.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'jobhunter.db.migrate'`

- [ ] **Step 3: Implement `run_migrate`, the seeds and the checkpointer setup**

- **`run_migrate`** executes `MIGRATE_ORDER` as the migrator, logs `migrate_step` with `step=` for each step, and returns the executed list.
- **Steps 1, 3 and 7** (`reconstruct_and_advance`, `advance_again`, `replay`) log `migrate_step_noop` with `reason="manifest_stream_arrives_in_M5"`.
- **`grants`** runs `GRANT INSERT, UPDATE ON <t> TO jobhunter_backup` only for each table of `BACKUP_GRANT_TABLES` where `to_regclass` is not NULL.
- **`queue_and_checkpointer`**:
  - opens the procrastinate app of the migrator DSN and calls `await app.schema_manager.apply_schema_async()` only when `to_regclass('procrastinate_jobs') IS NULL` (upgrades through procrastinate migration scripts start in a later milestone);
  - then calls `setup_checkpointer(cfg.dsn("psycopg"))`.
- **`seed`** inserts with `ON CONFLICT DO NOTHING` (on the hash, `version_no` or `id` key), then inserts `schedule_state` only when absent.
- **`setup_checkpointer`** runs `asyncio.run` of `AsyncPostgresSaver(conn).setup()` on a single `psycopg.AsyncConnection.connect(dsn, autocommit=True, prepare_threshold=0, row_factory=dict_row)`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_seed_math.py tests/integration -q`
Expected: PASS (every test so far)

- [ ] **Step 5: Commit**

```bash
git add backend/src/jobhunter/db backend/src/jobhunter/graphs backend/src/jobhunter/cli.py tests
git commit -m "feat(m0): migrate command in the §19.3 order with grants, procrastinate schema, checkpointer setup and seeds"
```

---

### Task 7: Transition helper, transition matrix, alerts and engines

**Files:**
- Create: `backend/src/jobhunter/errors.py`, `backend/src/jobhunter/db/session.py`, `backend/src/jobhunter/db/transitions.py`, `backend/src/jobhunter/db/migrations/versions/0002_m0_transition_matrix.py`, `backend/src/jobhunter/observability/alerts.py`
- Modify: `backend/src/jobhunter/db/seed.py` (`sync_transition_matrix` runs in the `seed` step), `tests/support/fixtures.py` (fixtures `engine`, `migrator_engine` and truncation)
- Test: `tests/unit/test_transition_registry.py`, `tests/property/test_state_machines.py`, `tests/integration/test_transitions.py`, `tests/integration/test_alerts.py`

**Interfaces:**
- Consumes: the `migrated` fixture, `AppConfig.dsn`.
- Produces:
  - `errors.DomainError(Exception)`, `errors.TransitionConflict(DomainError)` (attributes `table`, `row_id`, `to_state`), `errors.FencedOut(DomainError)` (attribute `owner: str`), `errors.InvariantViolation(DomainError)`
  - `session.make_engine(cfg: AppConfig, *, pool_size: int = 10) -> AsyncEngine` (`pool_pre_ping=True`, `max_overflow=0`); `session.make_sync_engine(dsn: str) -> Engine`
  - `@dataclass(frozen=True) class TransitionSpec`: `table: str`, `state_col: str`, `reason_col: str | None`, `changed_at: Mapping[str, str]`, `owner_cols: frozenset[str]`, `allowed: frozenset[tuple[str, str]]`
  - `TRANSITIONS: Final[Mapping[str, TransitionSpec]]` (`runs`, `operations` in M0)
  - `async def transition(conn: AsyncConnection, table: str, row_id: int, from_states: Sequence[str], to_state: str, *, reason: str | None = None, owner: Mapping[str, object] | None = None, set_: Mapping[str, object] | None = None) -> None` (raises `TransitionConflict` on 0 rows; `ValueError` for an edge not in `allowed` or an owner key outside `owner_cols`)
  - `seed.sync_transition_matrix(conn: sqlalchemy.Connection) -> int`
  - `alerts.Severity = Literal["info", "warn", "critical"]`; `async def raise_alert(conn: AsyncConnection, *, code: str, severity: Severity, subject: str, message: str, context: Mapping[str, JSONValue] | None = None) -> int` (dedup key `f"{code}:{subject}"`); `async def resolve_alert(conn: AsyncConnection, *, code: str, subject: str) -> bool`
  - Fixtures:
    - `engine`: an async app-role engine. After each test it runs, as migrator, `TRUNCATE runs, run_attempts, operations, sessions, login_attempts, login_lockouts, api_idempotency, alerts, purge_fences, worker_heartbeat, users, procrastinate_jobs, procrastinate_periodic_defers, procrastinate_workers CASCADE`, and resets `schedule_state` to its seeded anchor and interval with `last_evaluated_slot = latest slot ≤ now()` and `enabled = true`;
    - `migrator_engine`.

The allowed edges (§7.16.4 and §7.16.8). Each to-state maps to its changed-at column:

| Table | Edges | Changed-at column |
|---|---|---|
| `runs` | `queued→running`, `running→running`, `running→succeeded`, `running→partial`, `running→failed_invariant`, `running→failed`, `queued→failed`, `queued→cancelled`, `running→cancelled` | `running → started_at` (skipped on a self-edge); the terminal states → `finished_at` |
| `operations` | `pending→running`, `running→succeeded`, `running→failed`, `running→pending`, `pending→cancelled`, `running→cancelled` | `running → started_at`; `succeeded`, `failed`, `cancelled` → `finished_at` |

Owner columns: `runs: {attempt_no}`, `operations: {claim_id}`. The reason column is `runs.status_reason` or `operations.last_error_code`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_transition_registry.py
from jobhunter.db.transitions import TRANSITIONS
def test_run_and_operation_edges() -> None:
    assert ("succeeded", "running") not in TRANSITIONS["runs"].allowed
    assert ("running", "running") in TRANSITIONS["runs"].allowed
    assert TRANSITIONS["operations"].allowed == {("pending", "running"), ("running", "succeeded"), ("running", "failed"),
        ("running", "pending"), ("pending", "cancelled"), ("running", "cancelled")}
    assert TRANSITIONS["runs"].owner_cols == {"attempt_no"} and TRANSITIONS["operations"].owner_cols == {"claim_id"}
```

```python
# tests/property/test_state_machines.py
from hypothesis import given, strategies as st
from jobhunter.db.transitions import TRANSITIONS
RUN_STATES = ["queued", "running", "succeeded", "partial", "failed", "failed_invariant", "cancelled"]
@given(st.lists(st.sampled_from(RUN_STATES), max_size=30))
def test_simulated_run_never_takes_a_disallowed_edge(targets: list[str]) -> None:
    spec, state = TRANSITIONS["runs"], "queued"
    for to in targets:
        if (state, to) in spec.allowed: state = to      # the helper refuses everything else
    terminal = {"succeeded", "partial", "failed", "failed_invariant", "cancelled"}
    assert state in RUN_STATES
    assert not any((t, s) in spec.allowed for t in terminal for s in RUN_STATES)
```

```python
# tests/integration/test_transitions.py
import pytest, sqlalchemy as sa
from jobhunter.db.transitions import transition
from jobhunter.errors import TransitionConflict
pytestmark = pytest.mark.integration
INS = sa.text("INSERT INTO runs (trigger,scheduled_slot,status,include_paid,settings_version_id,scope_version_id) "
              "SELECT 'manual',NULL,'queued',false,id,scope_version_id FROM settings_versions WHERE version_no = 1 RETURNING id")

async def test_cas_success_and_conflict(engine) -> None:
    async with engine.begin() as c:
        rid = (await c.execute(INS)).scalar_one()
        await transition(c, "runs", rid, ["queued"], "running", set_={"attempt_no": 1, "heartbeat_at": sa.func.now(),
                         "current_thread_id": f"run-{rid}-a1"})
        row = (await c.execute(sa.text("SELECT status, started_at IS NOT NULL FROM runs WHERE id=:i"), {"i": rid})).one()
        assert tuple(row) == ("running", True)
        with pytest.raises(TransitionConflict):
            await transition(c, "runs", rid, ["queued"], "running")
        with pytest.raises(TransitionConflict):
            await transition(c, "runs", rid, ["running"], "succeeded", owner={"attempt_no": 2})
        await transition(c, "runs", rid, ["running"], "succeeded", owner={"attempt_no": 1}, reason="done")
        assert (await c.execute(sa.text("SELECT status_reason, finished_at IS NOT NULL FROM runs WHERE id=:i"), {"i": rid})).one() == ("done", True)

async def test_helper_refuses_unknown_edge_and_owner_key(engine) -> None:
    async with engine.begin() as c:
        with pytest.raises(ValueError):
            await transition(c, "runs", 1, ["succeeded"], "running")
        with pytest.raises(ValueError):
            await transition(c, "runs", 1, ["queued"], "running", owner={"claim_id": "x"})

async def test_trigger_rejects_disallowed_direct_update(engine) -> None:
    async with engine.begin() as c:
        rid = (await c.execute(INS)).scalar_one()
        await c.execute(sa.text("UPDATE runs SET status='cancelled', finished_at=now() WHERE id=:i"), {"i": rid})
        with pytest.raises(sa.exc.DBAPIError, match="disallowed transition runs: cancelled -> queued"):
            await c.execute(sa.text("UPDATE runs SET status='queued', finished_at=NULL WHERE id=:i"), {"i": rid})
```

```python
# tests/integration/test_alerts.py
import pytest, sqlalchemy as sa
from jobhunter.observability.alerts import raise_alert, resolve_alert
pytestmark = pytest.mark.integration
async def test_dedup_and_resolve(engine) -> None:
    async with engine.begin() as c:
        a = await raise_alert(c, code="run.orphaned_queue", severity="warn", subject="run:5", message="m")
        b = await raise_alert(c, code="run.orphaned_queue", severity="warn", subject="run:5", message="m")
        assert a == b
        assert (await c.execute(sa.text("SELECT occurrences FROM alerts WHERE id=:i"), {"i": a})).scalar_one() == 2
        assert await resolve_alert(c, code="run.orphaned_queue", subject="run:5") is True
        assert await raise_alert(c, code="run.orphaned_queue", severity="warn", subject="run:5", message="m") != a
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_transition_registry.py tests/property tests/integration/test_transitions.py tests/integration/test_alerts.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'jobhunter.db.transitions'`

- [ ] **Step 3: Implement the helper, the migration, the matrix sync and the alerts**

- **`0002_m0_transition_matrix`** creates:

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

- **`sync_transition_matrix`** replaces the rows of every registered table from `TRANSITIONS` inside the seed transaction and returns the row count.
- **`transition`**:
  - It validates every `(from, to)` pair against `allowed` before touching the DB.
  - It builds `UPDATE <table> SET <state>=:to[, <reason>=:reason][, <changed_at>=now()][, <set_ cols>] WHERE id=:id AND <state> = ANY(:from)[ AND <owner col> = :owner_<col>] RETURNING id`. Identifiers come only from the registry, and every value is a bound parameter.
  - The changed-at column is written only when `to_state` maps to one and `to_state ∉ from_states`. `set_` values that are SQLAlchemy expressions (`sa.func.now()`) are rendered as such.
- **`raise_alert`** runs `INSERT … ON CONFLICT (dedup_key) WHERE resolved_at IS NULL DO UPDATE SET occurrences = alerts.occurrences + 1, last_at = now() RETURNING id`. **`resolve_alert`** sets `resolved_at = now()` on the open row.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit tests/property tests/integration -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/src/jobhunter tests
git commit -m "feat(m0): CAS transition helper with ownership predicates, DB transition matrix trigger, alerts with dedup"
```

---

### Task 8: Worker singleton: lock session, standby, takeover, watchdog, epoch

**Files:**
- Create: `backend/src/jobhunter/worker/__init__.py`, `worker/lock.py`, `worker/watchdog.py`, `worker/epoch.py`, `tests/support/procs.py`, `tests/crash/__init__.py`, `tests/crash/lock_holder.py`
- Modify: `backend/src/jobhunter/cli.py` (`worker-health`)
- Test: `tests/crash/test_singleton.py`, `tests/integration/test_epoch.py`

**Interfaces:**
- Consumes: `AppConfig`, `make_engine`, `transition`, the `migrated` fixture.
- Produces:
  - `lock.ADVISORY_LOCK_KEY: Final = 7261001`, `HEARTBEAT_PERIOD_S: Final = 10`, `HEARTBEAT_STALE_S: Final = 60`, `STANDBY_SLEEP_S: Final = 10`, `WATCHDOG_EXIT_CODE: Final = 70`
  - `class LockSession`: `__init__(self, dsn: str)`; `connect(self) -> int` (backend pid; a sync `psycopg.Connection` with `autocommit=True, keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3, application_name="jobhunter-worker-lock"`, and `SET statement_timeout = '5s'`); `try_lock(self) -> bool`; `terminate_stale_holder(self) -> bool`; `heartbeat(self, instance_id: UUID, epoch: int) -> bool` (the §3.3.1 statement); `close(self) -> None`
  - `lock.acquire_lock(session: LockSession, *, sleep: Callable[[float], None] = time.sleep) -> None` (logs `standby` per wait)
  - `watchdog.Watchdog(threading.Thread)`: `__init__(self, session: LockSession, instance_id: UUID, epoch: int, *, period_s: float = HEARTBEAT_PERIOD_S, exit: Callable[[int], NoReturn] = os._exit)`
  - `@dataclass(frozen=True) class WorkerIdentity`: `instance_id: UUID`, `hostname: str`, `pid: int`, `lock_backend_pid: int`, `app_version: str`
  - `epoch.TakeoverSweep = Callable[[AsyncConnection, int], Awaitable[int]]`; `epoch.register_takeover_sweep(name: str) -> Callable[[TakeoverSweep], TakeoverSweep]`
  - `async def epoch.claim_epoch_and_sweep(engine: AsyncEngine, ident: WorkerIdentity) -> tuple[int, dict[str, int]]` (one transaction: the upsert plus every registered sweep)
  - CLI `jobhunter worker-health` (exit 0 when `heartbeat_at` is ≤ 60 s old, else 1)
  - `procs.spawn(args: list[str], env: Mapping[str, str]) -> subprocess.Popen[str]` (env merged over `os.environ`; stdout and stderr piped as text); `procs.wait_until(pred: Callable[[], bool], timeout_s: float, step_s: float = 0.5) -> None`; `async def procs.await_until(pred: Callable[[], Awaitable[bool]], timeout_s: float, step_s: float = 0.5) -> None`; `procs.exit_within(p: Popen[str], timeout_s: float) -> int` (each raises `AssertionError` on timeout)
  - `tests/crash/lock_holder.py`: connects, acquires, claims the epoch, starts the watchdog, prints `{"event":"holder_ready","epoch":N}` and sleeps

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_epoch.py
import uuid
import pytest, sqlalchemy as sa
from jobhunter.worker.epoch import WorkerIdentity, claim_epoch_and_sweep
pytestmark = pytest.mark.integration
async def test_epoch_increments_and_records_holder(engine) -> None:
    ident = WorkerIdentity(uuid.uuid4(), "h", 1, 4242, "0.1.0")
    assert (await claim_epoch_and_sweep(engine, ident))[0] == 1
    assert (await claim_epoch_and_sweep(engine, WorkerIdentity(uuid.uuid4(), "h", 2, 4343, "0.1.0")))[0] == 2
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT worker_epoch, lock_backend_pid FROM worker_heartbeat"))).one() == (2, 4343)
```

```python
# tests/crash/test_singleton.py
import json, signal, subprocess, sys, time
import psycopg, pytest
from tests.support.procs import exit_within, spawn, wait_until
pytestmark = [pytest.mark.crash, pytest.mark.slow]

def holder(server, proc_env):
    return spawn([sys.executable, "-m", "tests.crash.lock_holder"], proc_env("worker", server))

def hb(server):
    with psycopg.connect(server.dsn("superuser")) as c:
        return c.execute("SELECT worker_instance_id, worker_epoch, lock_backend_pid FROM worker_heartbeat").fetchone()

def test_second_holder_stays_standby_while_fresh(migrated, proc_env) -> None:
    a = holder(migrated, proc_env); wait_until(lambda: hb(migrated) is not None, 20)
    first = hb(migrated); b = holder(migrated, proc_env)
    time.sleep(25)
    assert a.poll() is None and b.poll() is None and hb(migrated)[0] == first[0]
    b.terminate(); a.terminate()
    assert '"event": "standby"' in b.stdout.read()

def test_takeover_terminates_stale_holder(migrated, proc_env) -> None:
    a = holder(migrated, proc_env); wait_until(lambda: hb(migrated) is not None, 20)
    a.send_signal(signal.SIGSTOP)
    with psycopg.connect(migrated.dsn("superuser"), autocommit=True) as c:
        c.execute("UPDATE worker_heartbeat SET heartbeat_at = now() - interval '2 minutes'")
    b = holder(migrated, proc_env)
    wait_until(lambda: hb(migrated)[1] == 2, 30)
    with psycopg.connect(migrated.dsn("superuser")) as c:
        pid = c.execute("SELECT pid FROM pg_locks WHERE locktype='advisory' AND objid=7261001 AND granted").fetchone()
    assert pid == (hb(migrated)[2],)
    a.send_signal(signal.SIGCONT)
    assert exit_within(a, 15) == 70
    b.terminate()

def test_c18_watchdog_exits_70_when_lock_session_dies(migrated, proc_env) -> None:
    a = holder(migrated, proc_env); wait_until(lambda: hb(migrated) is not None, 20)
    with psycopg.connect(migrated.dsn("superuser"), autocommit=True) as c:
        c.execute("SELECT pg_terminate_backend(%s)", (hb(migrated)[2],))
    started = time.monotonic()
    assert exit_within(a, 12) == 70 and time.monotonic() - started <= 12

def test_watchdog_exits_on_db_restart(fresh_pg_server, make_config, proc_env) -> None:      # Review Focus 1
    from jobhunter.db.migrate import run_migrate
    from tests.support.pg import restart_pg_server
    run_migrate(make_config("migrate", fresh_pg_server))
    a = holder(fresh_pg_server, proc_env); wait_until(lambda: hb(fresh_pg_server) is not None, 20)
    restart_pg_server(fresh_pg_server)
    assert exit_within(a, 15) == 70

def test_no_termination_when_pid_mismatch(migrated, proc_env) -> None:
    a = holder(migrated, proc_env); wait_until(lambda: hb(migrated) is not None, 20)
    with psycopg.connect(migrated.dsn("superuser"), autocommit=True) as c:
        c.execute("UPDATE worker_heartbeat SET heartbeat_at = now() - interval '2 minutes', lock_backend_pid = 1")
    b = holder(migrated, proc_env); time.sleep(15)
    assert a.poll() is None and hb(migrated)[1] == 1
    b.terminate(); a.terminate()

def test_worker_health_cli(migrated, proc_env) -> None:
    env = proc_env("worker", migrated)
    a = holder(migrated, proc_env); wait_until(lambda: hb(migrated) is not None, 20)
    assert subprocess.run([sys.executable, "-m", "jobhunter.cli", "worker-health"], env=env).returncode == 0
    a.send_signal(signal.SIGSTOP)
    with psycopg.connect(migrated.dsn("superuser"), autocommit=True) as c:
        c.execute("UPDATE worker_heartbeat SET heartbeat_at = now() - interval '61 seconds'")
    assert subprocess.run([sys.executable, "-m", "jobhunter.cli", "worker-health"], env=env).returncode == 1
    a.kill()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/integration/test_epoch.py tests/crash/test_singleton.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'jobhunter.worker'`

- [ ] **Step 3: Implement the lock session, `acquire_lock`, `Watchdog`, `claim_epoch_and_sweep` and `worker-health`**

- **`try_lock`** runs `SELECT pg_try_advisory_lock(7261001)`.
- **`terminate_stale_holder`** reads `worker_heartbeat.lock_backend_pid` and `heartbeat_at`, plus the granted `pg_locks` pid for `objid = 7261001`. It calls `pg_terminate_backend(pid)` only when the row exists, `heartbeat_at < now() - interval '60 seconds'`, **and** the two pids are equal. It returns whether it terminated.
- **`acquire_lock`** loops: `try_lock`; if not acquired, `terminate_stale_holder()` (immediate retry when `True`), otherwise log `standby` and `sleep(10)`.
- **`Watchdog.run`** loops every `period_s`. It calls `session.heartbeat(...)`, and on `False` or any exception it calls `exit(70)` (BLE001 is allowed in `worker/watchdog.py`).
- **`claim_epoch_and_sweep`** runs `INSERT INTO worker_heartbeat (id, worker_instance_id, worker_epoch, hostname, pid, lock_backend_pid, app_version, started_at, heartbeat_at) VALUES (1, …, 1, …, now(), now()) ON CONFLICT (id) DO UPDATE SET worker_epoch = worker_heartbeat.worker_epoch + 1, worker_instance_id = EXCLUDED.worker_instance_id, hostname = EXCLUDED.hostname, pid = EXCLUDED.pid, lock_backend_pid = EXCLUDED.lock_backend_pid, app_version = EXCLUDED.app_version, started_at = now(), heartbeat_at = now() RETURNING worker_epoch`. It then runs every registered sweep in registration order inside the same transaction and returns `(epoch, {name: count})`.
- **`lock_holder.py`** uses `load_config("worker")`, `LockSession(cfg.dsn("psycopg"))`, `acquire_lock`, `claim_epoch_and_sweep`, and `Watchdog(...).start()`, then sleeps.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/integration/test_epoch.py tests/crash/test_singleton.py -q`
Expected: PASS (7 passed, about 3 min)

- [ ] **Step 5: Commit**

```bash
git add backend/src/jobhunter/worker backend/src/jobhunter/cli.py tests
git commit -m "feat(m0): worker singleton with lock session, standby, takeover, watchdog and epochs"
```

---

### Task 9: procrastinate app, in-transaction defer (V1), stalled jobs (V2), reconciler framework

**Files:**
- Create: `backend/src/jobhunter/worker/tasks.py`, `backend/src/jobhunter/worker/reconcilers.py`, `docs/spikes/V1.md`, `docs/spikes/V2.md`, `tests/crash/stall_worker.py`
- Modify: `tests/support/fixtures.py` (fixture `queue_app`)
- Test: `tests/integration/test_queue.py`, `tests/integration/test_reconcilers.py`, `tests/crash/test_v2_stalled.py`

**Interfaces:**
- Consumes: `AppConfig`, `make_engine`.
- Produces:
  - `TASK_NAMESPACE: Final = "jh"`; `TaskName = Literal["run_collect", "run_operation", "tick", "reconcile_heavy", "retention_daily"]`; `job_task_name(name: TaskName) -> str` (returns `"jh:<name>"`)
  - `blueprint: procrastinate.Blueprint` with tasks registered by later tasks:
    - `run_collect` (queue `collect`, `lock="collect"`);
    - `run_operation` (queue `aux`);
    - `tick` (queue `tick`, periodic `* * * * *`, `queueing_lock="tick"`);
    - `reconcile_heavy` (queue `tick`, periodic `*/10 * * * *`);
    - `retention_daily` (queue `tick`, periodic `17 3 * * *`).

    Every task takes `pass_context=True`. Task 9 registers all five with stub bodies that log `task_stub` and return; Tasks 10–13 fill them in.
  - `QUEUES: Final[Mapping[str, int]] = {"collect": 1, "score": 1, "aux": 2, "tick": 1}` (concurrency per worker loop)
  - `make_queue_app(dsn: str, *, max_size: int = 4) -> procrastinate.App` (`PsycopgConnector(conninfo=dsn, min_size=1, max_size=max_size)`; `add_tasks_from(blueprint, namespace="jh")`). The first call in a process creates the **task app**, the only `App` that carries the blueprint's tasks. Later calls with the same dsn return it; a call with another dsn returns a new `App` without tasks, which serves only job-manager queries (stalled jobs, cleanup) on that database. `task_app() -> procrastinate.App` returns the task app, or raises `RuntimeError("make_queue_app() not called")`.
  - Fixture `queue_app` (function, async): `app = make_queue_app(make_config("worker").dsn("psycopg"))`, then `async with app.open_async(): yield app`
  - `class Deferrer(Protocol)`: `async def __call__(self, conn: AsyncConnection, task: TaskName, *, lock: str | None = None, queueing_lock: str | None = None, **kwargs: JSONValue) -> int`
  - `async def defer_in_tx(conn: AsyncConnection, task: TaskName, *, lock: str | None = None, queueing_lock: str | None = None, **kwargs: JSONValue) -> int` (conforms to `Deferrer`)
  - `async def live_job_exists(conn: AsyncConnection, task: TaskName, *, args_match: Mapping[str, JSONValue]) -> bool`
  - `async def release_stalled_jobs(app: procrastinate.App, *, seconds_since_heartbeat: float = 60.0) -> int`
  - `async def delete_finished_jobs(app: procrastinate.App, *, older_than_hours: int = 168) -> None`
  - `Cadence = Literal["start", "tick", "heavy", "daily"]`; `@dataclass(frozen=True) class ReconcileContext`: `engine: AsyncEngine`, `queue: procrastinate.App`, `defer: Deferrer`, `now: datetime`, `epoch: int`
  - `ReconcilerFn = Callable[[ReconcileContext], Awaitable[int]]`; `REGISTRY: dict[str, tuple[frozenset[Cadence], ReconcilerFn]]` (insertion-ordered); `reconciler(name: str, *cadences: Cadence) -> Callable[[ReconcilerFn], ReconcilerFn]`; `async def run_reconcilers(ctx: ReconcileContext, cadence: Cadence) -> dict[str, int]`
  - The `stalled_jobs` reconciler, registered with cadences `start` and `tick`, calls `release_stalled_jobs`.

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
        await defer_in_tx(c, "run_collect", lock="collect", queueing_lock="run:1:1", run_id=1, attempt_no=1)
        await tx.rollback()
        assert (await c.execute(COUNT, {"t": job_task_name("run_collect")})).scalar_one() == 0

async def test_v1_commit_leaves_exactly_one_job(engine, queue_app) -> None:
    async with engine.begin() as c:
        await defer_in_tx(c, "run_collect", lock="collect", queueing_lock="run:1:1", run_id=1, attempt_no=1)
    async with engine.connect() as c:
        assert (await c.execute(COUNT, {"t": "jh:run_collect"})).scalar_one() == 1
        assert await live_job_exists(c, "run_collect", args_match={"run_id": 1}) is True
        assert await live_job_exists(c, "run_collect", args_match={"run_id": 2}) is False

async def test_delete_finished_jobs_keeps_recent(engine, queue_app) -> None:
    async with engine.begin() as c:     # retention_daily has no queueing lock, so two jobs can be deferred
        await defer_in_tx(c, "retention_daily", timestamp=1); await defer_in_tx(c, "retention_daily", timestamp=2)
        await c.execute(sa.text("UPDATE procrastinate_jobs SET status='doing'"))
        await c.execute(sa.text("UPDATE procrastinate_jobs SET status='succeeded'"))
    async with engine.begin() as c:   # age one job's terminal event by 8 days
        await c.execute(sa.text("UPDATE procrastinate_events SET at = now() - interval '8 days' "
                                "WHERE job_id = (SELECT min(id) FROM procrastinate_jobs)"))
    await delete_finished_jobs(queue_app, older_than_hours=168)
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT count(*) FROM procrastinate_jobs"))).scalar_one() == 1
```

```python
# tests/integration/test_reconcilers.py
import pytest
from jobhunter.clock import utcnow
from jobhunter.worker import reconcilers as r
from jobhunter.worker.tasks import defer_in_tx
pytestmark = pytest.mark.integration
async def test_runner_isolates_failures_and_filters_cadence(engine, queue_app, monkeypatch) -> None:
    calls: list[str] = []
    async def ok(ctx: r.ReconcileContext) -> int: calls.append("ok"); return 3
    async def boom(ctx: r.ReconcileContext) -> int: raise RuntimeError("x")
    monkeypatch.setattr(r, "REGISTRY", {})
    r.reconciler("ok", "tick")(ok); r.reconciler("boom", "tick")(boom); r.reconciler("daily_only", "daily")(ok)
    ctx = r.ReconcileContext(engine=engine, queue=queue_app, defer=defer_in_tx, now=utcnow(), epoch=1)
    assert await r.run_reconcilers(ctx, "tick") == {"ok": 3, "boom": -1}
    assert calls == ["ok"]
```

```python
# tests/crash/test_v2_stalled.py
import signal, sys
import pytest, sqlalchemy as sa
from jobhunter.worker.tasks import release_stalled_jobs
from tests.support.procs import await_until, spawn
pytestmark = [pytest.mark.crash, pytest.mark.slow]
async def test_v2_killed_worker_job_is_released(engine, queue_app, migrated, proc_env) -> None:
    p = spawn([sys.executable, "-m", "tests.crash.stall_worker"], proc_env("worker", migrated))   # defers + runs a sleeping job
    async def doing() -> bool:
        async with engine.connect() as c:
            return (await c.execute(sa.text("SELECT count(*) FROM procrastinate_jobs WHERE status='doing'"))).scalar_one() == 1
    await await_until(doing, 30)
    p.send_signal(signal.SIGKILL); p.wait()
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
  - It takes the raw driver connection: `raw = (await conn.get_raw_connection()).driver_connection`.
  - It then calls `await task_app().tasks[job_task_name(task)].configure(connection=raw, lock=lock, queueing_lock=queueing_lock).defer_async(**kwargs)`, so the job row is inserted in the caller's transaction (procrastinate 3.10 `ConfigureTaskOptions.connection`). An external connection bypasses the connector's pool, so this works against any database without opening the app.
- **`live_job_exists`**: `SELECT EXISTS (SELECT 1 FROM procrastinate_jobs WHERE task_name = :t AND status IN ('todo','doing') AND args @> CAST(:m AS jsonb))`.
- **`release_stalled_jobs`** calls `await app.job_manager.get_stalled_jobs(seconds_since_heartbeat=…)`, then `await app.job_manager.retry_job(job)` for each job (every M0 task is idempotent), and returns the count.
- **`delete_finished_jobs`** calls `await app.job_manager.delete_old_jobs(nb_hours=…, include_failed=True, include_cancelled=True, include_aborted=True)`.
- **`run_reconcilers`** runs the registered functions whose cadence matches, in registration order. Each failure is logged `reconciler_failed` with `name` and `error_class` (never the message), and recorded as `-1` (BLE001 is allowed in this file).
- **`stall_worker.py`** registers a test task `jh:stall` (`await asyncio.sleep(600)`) on a separate `App`, defers it, and runs `run_worker_async(queues=["aux"], wait=True, update_heartbeat_interval=1)`.
- **Spike docs.** `V1.md` and `V2.md` record the assumption, the method (these tests), the observed result and the decision. For V1 the decision is in-transaction defer (correctness rests on the reconcilers anyway, §3.3.2). For V2 it is `get_stalled_jobs(seconds_since_heartbeat=60)` plus `retry_job`. If the observed API differs, record the differing names and adapt `release_stalled_jobs` without changing its signature.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/integration/test_queue.py tests/integration/test_reconcilers.py tests/crash/test_v2_stalled.py -q`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/src/jobhunter/worker docs/spikes/V1.md docs/spikes/V2.md tests
git commit -m "feat(m0): procrastinate blueprint with in-transaction defer, stalled-job release, reconciler framework (V1, V2)"
```

---

### Task 10: Durable operations with claim tokens

**Files:**
- Create: `backend/src/jobhunter/worker/operations.py`, `backend/src/jobhunter/worker/heartbeat.py`
- Modify: `backend/src/jobhunter/worker/tasks.py` (`run_operation` task body calls `execute_operation`)
- Test: `tests/integration/test_operations.py`

**Interfaces:**
- Consumes: `transition`, `raise_alert`, `defer_in_tx`, `live_job_exists`, `reconciler`, `register_takeover_sweep`, `FencedOut`.
- Produces:
  - `OperationKind = Literal["extract_profile_text", "extract_profile", "embed_profile", "propose_queries", "apply_scope", "create_targets_for_tuple", "batch_submit", "export", "purge", "eval_run", "watchlist_validate"]`
  - `MAX_OPERATION_ATTEMPTS: Final = 10`; `OPERATION_STALE: Final = timedelta(minutes=3)`; `REDEFER_AFTER: Final = timedelta(minutes=2)`
  - `@dataclass(frozen=True) class OperationClaim`: `operation_id: int`, `claim_id: UUID`, `kind: OperationKind`, `payload: Mapping[str, JSONValue]`
  - `async def create_operation(conn, *, kind: OperationKind, payload: Mapping[str, JSONValue], dedup_key: str, defer: Deferrer | None) -> int`
  - `async def claim_operation(conn, operation_id: int, *, epoch: int) -> OperationClaim | None`
  - `async def heartbeat_operation(conn, claim: OperationClaim) -> bool`
  - `async def lock_operation_owner(conn, claim: OperationClaim) -> None` (raises `FencedOut`)
  - `async def finish_operation(conn, claim: OperationClaim, *, status: Literal["succeeded", "failed"], error_code: str | None = None) -> None` (raises `FencedOut`)
  - `async def requeue_operation(conn, claim: OperationClaim, *, error_code: str, not_before: datetime) -> Literal["pending", "failed"]`
  - `async def reclaim_stale_operations(conn, *, now: datetime) -> int`
  - Takeover sweep `operations` (registered via `register_takeover_sweep`); reconciler `operations` (cadence `tick`)
  - `@dataclass(frozen=True) class OperationContext`: `claim: OperationClaim`, `engine: AsyncEngine`, `lost: asyncio.Event`
  - `OperationHandler = Callable[[OperationContext], Awaitable[None]]`; `HANDLERS: dict[OperationKind, OperationHandler]`; `register_operation_handler(kind: OperationKind) -> Callable[[OperationHandler], OperationHandler]`
  - `async def execute_operation(engine: AsyncEngine, operation_id: int, *, epoch: int, heartbeat_period_s: float = 30.0) -> None`
  - `async def heartbeat.heartbeat_loop(beat: Callable[[], Awaitable[bool]], *, period_s: float, on_lost: Callable[[], None]) -> None` (calls `beat()` every `period_s`; on `False` or an exception it calls `on_lost()` once and returns)

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_operations.py
import asyncio, uuid
from datetime import timedelta
import pytest, sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.errors import FencedOut
from jobhunter.worker import operations as ops
from jobhunter.worker.tasks import defer_in_tx, live_job_exists
from jobhunter.worker.epoch import WorkerIdentity, claim_epoch_and_sweep
pytestmark = pytest.mark.integration

async def new_op(engine, defer=defer_in_tx) -> int:
    async with engine.begin() as c:
        return await ops.create_operation(c, kind="export", payload={"export_id": 1}, dedup_key="export:1", defer=defer)

async def age_heartbeat(engine, op_id: int, minutes: int) -> None:
    async with engine.begin() as c:
        await c.execute(sa.text("UPDATE operations SET heartbeat_at = now() - make_interval(mins => :m) WHERE id=:i"), {"m": minutes, "i": op_id})

async def test_create_dedups_live_operations(engine, queue_app) -> None:
    assert await new_op(engine) == await new_op(engine)

async def test_operation_aba_old_execution_is_fenced(engine, queue_app) -> None:     # §18.2 Operation ABA, C35 (M0 part)
    op = await new_op(engine)
    async with engine.begin() as c: a = await ops.claim_operation(c, op, epoch=1)
    assert a is not None
    await age_heartbeat(engine, op, 4)
    async with engine.begin() as c: assert await ops.reclaim_stale_operations(c, now=utcnow()) == 1
    async with engine.begin() as c: b = await ops.claim_operation(c, op, epoch=1)
    assert b is not None and b.claim_id != a.claim_id
    async with engine.begin() as c:
        assert await ops.heartbeat_operation(c, a) is False
        with pytest.raises(FencedOut): await ops.lock_operation_owner(c, a)
    async with engine.begin() as c:
        with pytest.raises(FencedOut): await ops.finish_operation(c, a, status="succeeded")
    async with engine.begin() as c:
        await ops.lock_operation_owner(c, b); await ops.finish_operation(c, b, status="succeeded")
        assert (await c.execute(sa.text("SELECT status, attempts FROM operations WHERE id=:i"), {"i": op})).one() == ("succeeded", 1)

async def test_requeue_fails_permanently_at_ten_attempts(engine, queue_app) -> None:
    op = await new_op(engine)
    async with engine.begin() as c: await c.execute(sa.text("UPDATE operations SET attempts = 9 WHERE id=:i"), {"i": op})
    async with engine.begin() as c: claim = await ops.claim_operation(c, op, epoch=1)
    async with engine.begin() as c:
        assert await ops.requeue_operation(c, claim, error_code="transient", not_before=utcnow()) == "failed"
        row = (await c.execute(sa.text("SELECT status, attempts, last_error_code FROM operations WHERE id=:i"), {"i": op})).one()
        assert tuple(row) == ("failed", 10, "attempts_exhausted")
        assert (await c.execute(sa.text("SELECT severity FROM alerts WHERE code='operation.failed'"))).scalar_one() == "warn"

async def test_takeover_sweep_returns_older_epoch_operations(engine, queue_app) -> None:
    op = await new_op(engine)
    await claim_epoch_and_sweep(engine, WorkerIdentity(uuid.uuid4(), "h", 1, 1, "v"))
    async with engine.begin() as c: assert await ops.claim_operation(c, op, epoch=1)
    epoch, counts = await claim_epoch_and_sweep(engine, WorkerIdentity(uuid.uuid4(), "h", 2, 2, "v"))
    assert epoch == 2 and counts["operations"] == 1
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT status, claim_id FROM operations WHERE id=:i"), {"i": op})).one() == ("pending", None)

async def test_c30_reconciler_redefers_lost_job_after_two_minutes(engine, queue_app) -> None:
    from jobhunter.worker.reconcilers import ReconcileContext, run_reconcilers
    op = await new_op(engine, defer=None)
    ctx = lambda dt: ReconcileContext(engine=engine, queue=queue_app, defer=defer_in_tx, now=utcnow() + dt, epoch=1)
    await run_reconcilers(ctx(timedelta(minutes=1)), "tick")
    async with engine.connect() as c: assert not await live_job_exists(c, "run_operation", args_match={"operation_id": op})
    await run_reconcilers(ctx(timedelta(minutes=2, seconds=1)), "tick")
    async with engine.connect() as c: assert await live_job_exists(c, "run_operation", args_match={"operation_id": op})

async def test_execute_signals_lost_claim_and_unknown_kind_fails(engine, queue_app, monkeypatch) -> None:
    seen: list[bool] = []
    async def handler(ctx: ops.OperationContext) -> None:
        await age_heartbeat(engine, ctx.claim.operation_id, 4)
        async with engine.begin() as c: await ops.reclaim_stale_operations(c, now=utcnow())
        await asyncio.wait_for(ctx.lost.wait(), 5); seen.append(True)
    monkeypatch.setitem(ops.HANDLERS, "export", handler)
    await ops.execute_operation(engine, await new_op(engine), epoch=1, heartbeat_period_s=0.2)
    assert seen == [True]
    async with engine.begin() as c:
        other = await ops.create_operation(c, kind="purge", payload={}, dedup_key="p", defer=None)
    monkeypatch.delitem(ops.HANDLERS, "purge", raising=False)
    await ops.execute_operation(engine, other, epoch=1)
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT status, last_error_code FROM operations WHERE id=:i"), {"i": other})).one() == ("failed", "no_handler")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/integration/test_operations.py -q`
Expected: FAIL with `ImportError: cannot import name 'operations' from 'jobhunter.worker'`

- [ ] **Step 3: Implement `operations.py` and `heartbeat.py`**

Every state change goes through `transition()`:
- **claim**: `pending→running` with `set_={"claim_id": uuid4(), "worker_epoch": epoch, "heartbeat_at": now()}`, only when `not_before IS NULL OR not_before <= now()`;
- **finish**: `running→succeeded|failed` with `owner={"claim_id": …}`;
- **requeue and reclaim**: `running→pending` with `set_={"claim_id": None, "worker_epoch": None, "heartbeat_at": None, "attempts": attempts + 1, "not_before": …}`. When `attempts + 1 >= 10`, it is `running→failed` with `last_error_code='attempts_exhausted'` instead, plus `raise_alert(code="operation.failed", severity="warn", subject=f"operation:{id}")`.

Other bodies:
- **`create_operation`**: `INSERT … ON CONFLICT (kind, dedup_key) WHERE status IN ('pending','running') DO NOTHING RETURNING id`. On a conflict it selects the live row's id. When `defer` is given, it defers `run_operation(operation_id=…)` with `queueing_lock=f"op:{id}"` in the same transaction.
- **`heartbeat_operation`**: `UPDATE operations SET heartbeat_at = now() WHERE id = :i AND claim_id = :c AND status = 'running'`, returning whether a row matched.
- **`lock_operation_owner`**: `SELECT 1 … WHERE id = :i AND claim_id = :c AND status = 'running' FOR SHARE`. Zero rows raise `FencedOut("operation")`.
- **Sweeps and reconcilers** select candidate ids `FOR UPDATE SKIP LOCKED` and transition row by row:
  - the takeover sweep takes `running` operations with `worker_epoch < :epoch`;
  - the reconciler takes `pending` operations older than `REDEFER_AFTER` (by `created_at`, or by `not_before` when later) without a live job, and re-defers them, then reclaims `running` operations whose heartbeat is older than 3 min.
- **`execute_operation`**:
  1. claim (return when `None`);
  2. a missing handler finishes `failed('no_handler')` with an `operation.failed` alert;
  3. otherwise run the handler next to `heartbeat_loop(period_s=heartbeat_period_s)`. A lost heartbeat sets `ctx.lost` and cancels nothing else, because handlers check `lost` at their own safe points.
  4. On normal return, `finish(succeeded)`. A `FencedOut` from the handler ends silently. Any other exception is logged as `operation_bug`, then `requeue_operation(error_code="bug", not_before=now + 1 min)`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/integration/test_operations.py -q`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/src/jobhunter/worker tests/integration/test_operations.py
git commit -m "feat(m0): durable operations with fresh claim tokens, reclaim, takeover sweep and the operations reconciler"
```

---

### Task 11: Tick and slots

**Files:**
- Create: `backend/src/jobhunter/worker/tick.py`
- Modify: `backend/src/jobhunter/worker/tasks.py` (`tick` task body)
- Test: `tests/unit/test_slots.py`, `tests/property/test_slots_property.py`, `tests/integration/test_tick.py`

**Interfaces:**
- Consumes: `seed.latest_slot_at_or_before`, `defer_in_tx`, `Deferrer`, `release_stalled_jobs`, `run_reconcilers`.
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
def test_tick_with_clock_behind_cursor_is_noop() -> None:      # Review Focus 5 (pure part)
    assert compute_due_slots(A, H6, A + timedelta(hours=12), A + timedelta(hours=1)) == []
def test_dst_never_shifts_utc_slots() -> None:
    a = datetime(2026, 10, 24, 4, tzinfo=UTC)                    # 06:00 CEST; DST ends 2026-10-25
    slots = compute_due_slots(a, 86400, a, a + timedelta(days=3))
    assert [s.hour for s in slots] == [4, 4, 4]
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
def test_every_slot_exactly_once(interval: int, offsets: list[int]) -> None:
    cursor, seen = A, []
    for t in sorted(offsets):
        now = A + timedelta(seconds=t); due = compute_due_slots(A, interval, cursor, now)
        seen += due; cursor = due[-1] if due else cursor
    assert len(seen) == len(set(seen)) and all(s <= A + timedelta(seconds=max(offsets)) for s in seen)
```

```python
# tests/integration/test_tick.py
import time
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

async def runs(engine) -> list[tuple[str, str]]:
    async with engine.connect() as c:
        return [tuple(r) for r in await c.execute(sa.text("SELECT status, trigger FROM runs ORDER BY scheduled_slot"))]

async def tick(engine, now: datetime):
    async with engine.begin() as c: return await run_tick(c, now=now, defer=defer_in_tx)

async def test_due_slot_queues_one_scheduled_run(engine, queue_app) -> None:
    await schedule(engine, interval=21600, cursor=A)
    rep = await tick(engine, A + timedelta(hours=6, minutes=1))
    assert rep.missed == 0 and rep.queued_run_id is not None and await runs(engine) == [("queued", "schedule")]
    async with engine.connect() as c:
        assert await live_job_exists(c, "run_collect", args_match={"run_id": rep.queued_run_id, "attempt_no": 1})
    assert (await tick(engine, A + timedelta(hours=6, minutes=2))).queued_run_id is None
    assert len(await runs(engine)) == 1

async def test_overlap_records_skipped_overlap(engine, queue_app) -> None:
    await schedule(engine, interval=21600, cursor=A)
    await tick(engine, A + timedelta(hours=6))
    rep = await tick(engine, A + timedelta(hours=12))
    assert rep.skipped_overlap and await runs(engine) == [("queued", "schedule"), ("skipped_overlap", "schedule")]

async def test_downtime_records_missed_and_catch_up(engine, queue_app) -> None:
    await schedule(engine, interval=21600, cursor=A)
    rep = await tick(engine, A + timedelta(hours=18, minutes=30))
    assert rep.missed == 2 and await runs(engine) == [("missed", "schedule"), ("missed", "schedule"), ("queued", "catch_up")]

async def test_long_downtime_is_bounded_and_once_only(engine, queue_app) -> None:      # Review Focus 2
    await schedule(engine, interval=3600, cursor=A)
    started = time.monotonic(); rep = await tick(engine, A + timedelta(days=9, minutes=30))
    assert time.monotonic() - started < 2.0
    assert rep.missed == 215 and len(await runs(engine)) == 216 and (await runs(engine))[-1] == ("queued", "catch_up")
    assert (await tick(engine, A + timedelta(days=9, minutes=31))).missed == 0 and len(await runs(engine)) == 216

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

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_slots.py tests/property/test_slots_property.py tests/integration/test_tick.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'jobhunter.worker.tick'`

- [ ] **Step 3: Implement `compute_due_slots` and `run_tick`**

- **`compute_due_slots`**: with `k(t) = floor((t − anchor) / interval)` (Python floor division on whole seconds, so negative values work), return `anchor + k·interval` for every `k` in `(k(last), k(now)]`. With `last = None`, return only the latest slot `≤ now`. When `now < last`, return `[]`.
- **`run_tick`** locks `schedule_state` with `SELECT … FOR UPDATE`, then:
  - When `enabled = false`, it sets `last_evaluated_slot := latest slot ≤ now` (never backwards) and returns.
  - It reads `include_paid` from `settings_versions.payload->'schedule'->>'include_paid'` and `scope_version_id` from that settings version.
  - It inserts every slot but the latest as `runs(status='missed', trigger='schedule')`, using `ON CONFLICT (kind, scheduled_slot) WHERE scheduled_slot IS NOT NULL DO NOTHING`, as one multi-row `INSERT … SELECT unnest(:slots)`.
  - For the latest slot `s`, inside a SAVEPOINT (`conn.begin_nested()`), it inserts `status='queued'` with `trigger='schedule'` if `now − s ≤ 5 min`, else `'catch_up'`.
    - A `UniqueViolation` on `runs_one_active_collect` rolls back the savepoint and inserts `skipped_overlap` (same slot and trigger) in a new savepoint.
    - A `UniqueViolation` on `runs_slot_uq` is a no-op.
    - The constraint is told apart by `exc.orig.diag.constraint_name`.
  - It defers `run_collect(run_id=…, attempt_no=1)` with `lock="collect"` and `queueing_lock=f"run:{id}:1"` in the same transaction.
  - It sets `last_evaluated_slot := max(cursor, s)`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_slots.py tests/property/test_slots_property.py tests/integration/test_tick.py -q`
Expected: PASS (11 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/src/jobhunter/worker tests
git commit -m "feat(m0): UTC slot tick with missed, skipped_overlap and catch_up outcomes"
```

---

### Task 12: Collection run lifecycle, no-op graph and checkpointer (V6)

**Files:**
- Create: `backend/src/jobhunter/worker/runs.py`, `backend/src/jobhunter/graphs/collect/__init__.py`, `backend/src/jobhunter/graphs/collect/graph.py`, `docs/spikes/V6.md`
- Modify: `backend/src/jobhunter/worker/tasks.py` (`run_collect` task body), `backend/src/jobhunter/graphs/checkpointer.py` (`open_saver` used at runtime)
- Test: `tests/integration/test_checkpointer_v6.py`, `tests/integration/test_runs.py`

**Interfaces:**
- Consumes: `transition`, `raise_alert`, `Deferrer`, `register_takeover_sweep`, `reconciler`, `heartbeat_loop`, `open_saver`, `metrics.RUNS`, `metrics.RUN_DURATION`, `FencedOut`.
- Produces:
  - `graph.CollectState(TypedDict)`: `run_id: int`, `attempt_no: int`, `steps: Annotated[list[str], operator.add]`
  - `graph.build_noop_collect_graph(checkpointer: BaseCheckpointSaver[str]) -> CompiledStateGraph` (nodes `start` → `finalize`; `finalize` awaits `afault_point("collect.before_finalize")`)
  - `runs.thread_id(run_id: int, attempt_no: int) -> str` (returns `f"run-{run_id}-a{attempt_no}"`)
  - `@dataclass(frozen=True) class RunOwner`: `run_id: int`, `attempt_no: int`
  - `@dataclass(frozen=True) class RunRow`: `id: int`, `kind: str`, `trigger: str`, `status: str`, `scheduled_slot: datetime | None`, `include_paid: bool`, `attempt_no: int`, `queued_at: datetime`, `started_at: datetime | None`, `finished_at: datetime | None`, `status_reason: str | None`
  - `async def start_attempt(conn, run_id: int, attempt_no: int, *, epoch: int) -> RunOwner | None`
  - `async def heartbeat_run(conn, owner: RunOwner) -> bool`
  - `async def finalize_run(conn, owner: RunOwner, *, status: Literal["succeeded", "partial", "failed_invariant", "failed"], reason: str | None, report: Mapping[str, JSONValue]) -> None` (raises `FencedOut`)
  - `async def begin_new_attempt(conn, run_id: int, *, epoch: int, end_reason: Literal["worker_takeover", "heartbeat_stale"], defer: Deferrer) -> int | None`
  - `async def request_manual_run(conn, *, include_paid: bool, defer: Deferrer) -> tuple[RunRow, bool]`
  - `async def execute_run(engine: AsyncEngine, graph: CompiledStateGraph, run_id: int, attempt_no: int, *, epoch: int, heartbeat_period_s: float = 30.0) -> None` (the `run_collect` body)
  - Takeover sweep `runs`; reconcilers `stale_runs` and `queued_runs` (cadence `tick`)
  - `RUN_STALE: Final = timedelta(minutes=3)`; `QUEUED_REDEFER_AFTER: Final = timedelta(minutes=2)`; `MAX_RESUMES: Final = 3`

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_checkpointer_v6.py
import pytest
from langgraph.graph import END, START, StateGraph
from typing import Annotated, TypedDict
import operator
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
        app = g.compile(checkpointer=saver)
        cfg = {"configurable": {"thread_id": "run-1-a1"}}
        with pytest.raises(RuntimeError): await app.ainvoke({"steps": []}, cfg, durability="sync")
        out = await app.ainvoke(None, cfg, durability="sync")
        assert calls == {"a": 1, "b": 2} and out["steps"] == ["a", "b"]
        await app.ainvoke({"steps": []}, {"configurable": {"thread_id": "run-2-a1"}}, durability="sync")
        await saver.adelete_thread("run-1-a1")
        assert await saver.aget_tuple({"configurable": {"thread_id": "run-1-a1"}}) is None
        assert await saver.aget_tuple({"configurable": {"thread_id": "run-2-a1"}}) is not None
```

```python
# tests/integration/test_runs.py
import pytest, sqlalchemy as sa
from jobhunter.errors import FencedOut
from jobhunter.graphs.checkpointer import open_saver
from jobhunter.graphs.collect.graph import build_noop_collect_graph
from jobhunter.worker import runs
from jobhunter.worker.tasks import defer_in_tx, live_job_exists
pytestmark = pytest.mark.integration

async def queued(engine) -> int:
    async with engine.begin() as c:
        row, created = await runs.request_manual_run(c, include_paid=False, defer=defer_in_tx)
        assert created; return row.id

async def fetch(engine, rid: int):
    async with engine.connect() as c:
        return (await c.execute(sa.text("SELECT status, attempt_no, resume_count, current_thread_id, report, status_reason "
                                        "FROM runs WHERE id=:i"), {"i": rid})).one()

async def test_happy_path_noop_run(engine, queue_app, migrated) -> None:
    rid = await queued(engine)
    async with open_saver(migrated.dsn("app")) as saver:
        await runs.execute_run(engine, build_noop_collect_graph(saver), rid, 1, epoch=1)
        assert await saver.aget_tuple({"configurable": {"thread_id": f"run-{rid}-a1"}}) is not None
    status, attempt, resumes, thread, report, _ = await fetch(engine, rid)
    assert (status, attempt, resumes, thread, report) == ("succeeded", 1, 0, f"run-{rid}-a1", {"noop": True})
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT worker_epoch, end_reason FROM run_attempts WHERE run_id=:i"), {"i": rid})).one() == (1, "completed")

async def test_job_for_superseded_attempt_is_noop(engine, queue_app) -> None:
    rid = await queued(engine)
    async with engine.begin() as c: assert await runs.start_attempt(c, rid, 1, epoch=1)
    async with engine.begin() as c: assert await runs.start_attempt(c, rid, 1, epoch=1) is None

async def test_new_attempt_then_old_owner_is_fenced(engine, queue_app) -> None:
    rid = await queued(engine)
    async with engine.begin() as c: old = await runs.start_attempt(c, rid, 1, epoch=1)
    async with engine.begin() as c:
        assert await runs.begin_new_attempt(c, rid, epoch=1, end_reason="heartbeat_stale", defer=defer_in_tx) == 2
    assert (await fetch(engine, rid))[:4] == ("running", 2, 1, f"run-{rid}-a2")
    async with engine.connect() as c:
        assert await live_job_exists(c, "run_collect", args_match={"run_id": rid, "attempt_no": 2})
        assert (await c.execute(sa.text("SELECT end_reason FROM run_attempts WHERE run_id=:i AND attempt_no=1"), {"i": rid})).scalar_one() == "heartbeat_stale"
    async with engine.begin() as c:
        with pytest.raises(FencedOut): await runs.finalize_run(c, old, status="succeeded", reason=None, report={})
        assert await runs.heartbeat_run(c, old) is False

async def test_resume_exhausted_after_three_resumes(engine, queue_app) -> None:
    rid = await queued(engine)
    async with engine.begin() as c: await runs.start_attempt(c, rid, 1, epoch=1)
    for expected in (2, 3, 4, None):
        async with engine.begin() as c:
            assert await runs.begin_new_attempt(c, rid, epoch=1, end_reason="heartbeat_stale", defer=defer_in_tx) == expected
    status, _, resumes, _, _, reason = await fetch(engine, rid)
    assert (status, resumes, reason) == ("failed", 4, "resume_exhausted")
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT severity FROM alerts WHERE code='run.resume_exhausted'"))).scalar_one() == "critical"

async def test_queued_runs_reconciler_redefers_once_then_fails(engine, queue_app) -> None:
    from datetime import timedelta
    from jobhunter.clock import utcnow
    from jobhunter.worker.reconcilers import ReconcileContext, run_reconcilers
    rid = await queued(engine)
    async with engine.begin() as c: await c.execute(sa.text("DELETE FROM procrastinate_jobs"))
    ctx = lambda m: ReconcileContext(engine=engine, queue=queue_app, defer=defer_in_tx, now=utcnow() + timedelta(minutes=m), epoch=1)
    await run_reconcilers(ctx(2.1), "tick")
    assert (await fetch(engine, rid))[5] == "redeferred"
    async with engine.begin() as c: await c.execute(sa.text("DELETE FROM procrastinate_jobs"))
    await run_reconcilers(ctx(4.2), "tick")
    assert (await fetch(engine, rid))[0::5] == ("failed", "orphaned_queue")

async def test_takeover_sweep_moves_older_epoch_runs(engine, queue_app) -> None:
    import uuid
    from jobhunter.worker.epoch import WorkerIdentity, claim_epoch_and_sweep
    rid = await queued(engine)
    await claim_epoch_and_sweep(engine, WorkerIdentity(uuid.uuid4(), "h", 1, 1, "v"))
    async with engine.begin() as c: await runs.start_attempt(c, rid, 1, epoch=1)
    epoch, counts = await claim_epoch_and_sweep(engine, WorkerIdentity(uuid.uuid4(), "h", 2, 2, "v"))
    assert counts["runs"] == 1 and (await fetch(engine, rid))[1] == 2
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT worker_epoch FROM run_attempts WHERE run_id=:i AND attempt_no=2"), {"i": rid})).scalar_one() == 2
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/integration/test_checkpointer_v6.py tests/integration/test_runs.py -q`
Expected: FAIL with `ImportError: cannot import name 'runs' from 'jobhunter.worker'` (V6 may already pass, since it only exercises the library and `open_saver`).

- [ ] **Step 3: Implement `runs.py`, the no-op graph and `execute_run`**

- **`start_attempt`**: `SELECT … FROM runs WHERE id = :i FOR UPDATE`.
  - When `status = 'queued' AND attempt_no = attempt_no_arg − 1`, it transitions `queued→running` with `set_={"attempt_no": n, "heartbeat_at": now(), "current_thread_id": thread_id(id, n)}` and inserts `run_attempts(run_id, n, epoch, thread_id)`.
  - When `status = 'running' AND attempt_no = n` (the attempt was created by a sweep or reconciler), it returns the owner.
  - Otherwise it returns `None`.
- **`heartbeat_run`**: `UPDATE runs SET heartbeat_at = now() WHERE id = :i AND attempt_no = :a AND status = 'running'`.
- **`finalize_run`**: `transition(conn, "runs", id, ["running"], status, reason=reason, owner={"attempt_no": a}, set_={"report": report})`. A `TransitionConflict` maps to `FencedOut("run")`. It then sets `run_attempts.ended_at = now(), end_reason = 'completed'`, increments `RUNS.labels(status)` and observes `RUN_DURATION`.
- **`begin_new_attempt`**: row `FOR UPDATE`; the run must be `running`, else it returns `None`.
  - `next = resume_count + 1`. When `next > MAX_RESUMES`: `running→failed` with `reason='resume_exhausted'` and `set_={"resume_count": next}`, plus `raise_alert(code="run.resume_exhausted", severity="critical", subject=f"run:{id}")`; return `None`.
  - Otherwise: `running→running` with `set_={"attempt_no": a + 1, "resume_count": next, "current_thread_id": …, "heartbeat_at": now()}`; end the old `run_attempts` row (`ended_at`, `end_reason`); insert the new row with `epoch`; defer `run_collect(run_id, attempt_no = a + 1)` with `lock="collect"` and `queueing_lock=f"run:{id}:{a + 1}"`; return `a + 1`.
- **Sweep `runs`**: every `running` run whose current `run_attempts.worker_epoch < :epoch` → `begin_new_attempt(..., end_reason="worker_takeover")`.
- **Reconciler `stale_runs`**: `running` and `heartbeat_at < now − 3 min` → `end_reason="heartbeat_stale"`.
- **Reconciler `queued_runs`**: a `queued` run with no live job, where `now − queued_at ≥ 2 min`.
  - When `status_reason` is NULL: defer again and set `status_reason = 'redeferred'` (a plain UPDATE; the status does not change).
  - When `status_reason = 'redeferred'` and `now − queued_at ≥ 4 min`: `queued→failed('orphaned_queue')` plus `raise_alert("run.orphaned_queue", "warn", f"run:{id}")`.
- **`request_manual_run`**:
  - Return the `queued` or `running` collection as `(row, False)` when one exists.
  - Otherwise insert `runs(trigger='manual', scheduled_slot=NULL, status='queued', include_paid, settings_version_id, scope_version_id)` from `schedule_state` and its settings version, inside a SAVEPOINT, and defer the task.
  - A `UniqueViolation` on `runs_one_active_collect` re-reads the active run and returns `(row, False)`.
- **`execute_run`**:
  1. `start_attempt` (return when `None`);
  2. run `graph.ainvoke({"run_id": …, "attempt_no": …, "steps": []}, {"configurable": {"thread_id": …}, "recursion_limit": 200}, durability="sync")` next to `heartbeat_loop(period_s=heartbeat_period_s)` (a lost heartbeat cancels the graph task);
  3. `finalize_run(status="succeeded", report={"noop": True})`.

  A `FencedOut` is logged `run_fenced_out` and ends quietly. Any other exception means `finalize_run(status="failed", reason="bug")`, plus `raise_alert("run.failed", "critical", f"run:{id}")`.
- **`V6.md`** records the results of `test_checkpointer_v6.py` and of `test_checkpointer_setup_is_idempotent` (Task 6).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/integration/test_checkpointer_v6.py tests/integration/test_runs.py -q`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/src/jobhunter docs/spikes/V6.md tests
git commit -m "feat(m0): run attempts with ownership fencing, no-op collect graph on PostgresSaver, run reconcilers (V6)"
```

---

### Task 13: Worker process: composition, graceful shutdown, retention, crash cases

**Files:**
- Create: `backend/src/jobhunter/worker/main.py`, `backend/src/jobhunter/worker/shutdown.py`, `backend/src/jobhunter/privacy/__init__.py`, `backend/src/jobhunter/privacy/retention.py`
- Modify: `backend/src/jobhunter/worker/tasks.py` (`retention_daily`, `reconcile_heavy` bodies; `WorkerRuntime` in the job context), `backend/src/jobhunter/cli.py` (`worker`)
- Test: `tests/integration/test_retention.py`, `tests/crash/test_worker_process.py`

**Interfaces:**
- Consumes: everything from Tasks 7–12.
- Produces:
  - `@dataclass class WorkerRuntime`: `cfg: AppConfig`, `engine: AsyncEngine`, `queue: procrastinate.App`, `saver: AsyncPostgresSaver`, `collect_graph: CompiledStateGraph`, `epoch: int`, `shutdown: ShutdownState`. Tasks read it from `context.additional_context["runtime"]`.
  - `@dataclass class ShutdownState`: `admission_stopped: bool = False`, `requested: asyncio.Event`
  - `GRACE_S: Final = 100`
  - `async def worker_main(cfg: AppConfig) -> int`
  - `@dataclass(frozen=True) class RetentionRule`: `name: str`, `sql: str`, `window: timedelta`; `RULES: Final[tuple[RetentionRule, ...]]`
  - `async def run_retention_daily(engine: AsyncEngine, queue: procrastinate.App, saver: AsyncPostgresSaver, *, now: datetime) -> dict[str, int]`, which returns deleted counts keyed by rule name: `api_idempotency`, `sessions`, `login_attempts`, `alerts`, `operations`, `runs`, `procrastinate_jobs` (−1, because procrastinate reports no count), `checkpoints` (threads deleted)
  - CLI `jobhunter worker`

Retention windows for the M0 tables (§20.1):
- `api_idempotency`: `expires_at < now`;
- `sessions`: `absolute_expires_at < now − 1 day` **or** `idle_expires_at < now − 1 day`;
- `login_attempts`: `at < now − 30 days`;
- `alerts`: `resolved_at < now − 90 days`;
- `operations`: `finished_at < now − 30 days`;
- `run_attempts` and `runs`: terminal and `finished_at < now − 13 months` (runs `missed` and `skipped_overlap` by `queued_at`);
- procrastinate jobs: `delete_finished_jobs(older_than_hours=168)`;
- LangGraph threads: `saver.adelete_thread(thread_id)` for every `run_attempts.thread_id` of a run finished more than 14 days ago.

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
        for ago, ok in ((31, False), (29, True)):
            await c.execute(sa.text("INSERT INTO login_attempts (at, ip, username_hmac, success) VALUES (:t, '198.51.100.1', '\\x00', false)"),
                            {"t": now - timedelta(days=ago)})
        await c.execute(sa.text("INSERT INTO alerts (code,severity,dedup_key,message,resolved_at) VALUES ('a','info','k1','m',:r), ('a','info','k2','m',:s)"),
                        {"r": now - timedelta(days=91), "s": now - timedelta(days=89)})
        await c.execute(sa.text("INSERT INTO api_idempotency (route,idem_key,request_sha256,status,expires_at) "
                                "VALUES ('r','aaaaaaaaaaaaaaaa','\\x00','in_progress',:e), ('r','bbbbbbbbbbbbbbbb','\\x00','in_progress',:f)"),
                                {"e": now - timedelta(seconds=1), "f": now + timedelta(days=1)})
    async with open_saver(migrated.dsn("app")) as saver:
        report = await run_retention_daily(engine, queue_app, saver, now=now)
    assert report["login_attempts"] == 1 and report["alerts"] == 1 and report["api_idempotency"] == 1
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT count(*) FROM login_attempts"))).scalar_one() == 1
        assert (await c.execute(sa.text("SELECT dedup_key FROM alerts"))).scalar_one() == "k2"
```

```python
# tests/crash/test_worker_process.py
import signal, sys, time
import psycopg, pytest
from tests.support.procs import exit_within, spawn, wait_until
pytestmark = [pytest.mark.crash, pytest.mark.slow]

def worker(server, proc_env, **env):
    return spawn([sys.executable, "-m", "jobhunter.cli", "worker"], proc_env("worker", server, **env))

def one(server, statement):
    with psycopg.connect(server.dsn("superuser"), autocommit=True) as c:
        cur = c.execute(statement); return cur.fetchone() if cur.description else None

def due_slot_now(server) -> None:
    one(server, "UPDATE schedule_state SET anchor_utc = date_trunc('minute', now()), interval_s = 3600, "
                "last_evaluated_slot = date_trunc('minute', now()) - interval '1 hour'")

def test_tick_runs_noop_collection_end_to_end(migrated, proc_env) -> None:
    w = worker(migrated, proc_env); due_slot_now(migrated)
    wait_until(lambda: one(migrated, "SELECT count(*) FROM runs WHERE status='succeeded'") == (1,), 120)
    w.send_signal(signal.SIGTERM); assert exit_within(w, 30) == 0
    assert one(migrated, "SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND objid=7261001") == (0,)

def test_graceful_shutdown_waits_for_inflight_run(migrated, proc_env) -> None:
    w = worker(migrated, proc_env, JOBHUNTER_FAULT="collect.before_finalize:sleep=5"); due_slot_now(migrated)
    wait_until(lambda: one(migrated, "SELECT count(*) FROM runs WHERE status='running'") == (1,), 120)
    w.send_signal(signal.SIGTERM); assert exit_within(w, 60) == 0
    assert one(migrated, "SELECT status FROM runs") == ("succeeded",)

def test_c17_zombie_worker_writes_nothing(migrated, proc_env) -> None:
    a = worker(migrated, proc_env, JOBHUNTER_FAULT="collect.before_finalize:pause"); due_slot_now(migrated)
    wait_until(lambda: one(migrated, "SELECT count(*) FROM runs WHERE status='running' AND attempt_no=1") == (1,), 120)
    time.sleep(1)                                             # a has stopped itself at the fault point
    one(migrated, "UPDATE worker_heartbeat SET heartbeat_at = now() - interval '2 minutes'")
    one(migrated, "UPDATE procrastinate_workers SET last_heartbeat = now() - interval '5 minutes'")
    b = worker(migrated, proc_env)
    wait_until(lambda: one(migrated, "SELECT status, attempt_no FROM runs") == ("succeeded", 2), 120)
    a.send_signal(signal.SIGCONT); assert exit_within(a, 20) == 70
    assert one(migrated, "SELECT end_reason FROM run_attempts WHERE attempt_no=1") == ("worker_takeover",)
    assert one(migrated, "SELECT status, attempt_no FROM runs") == ("succeeded", 2)
    b.send_signal(signal.SIGTERM); exit_within(b, 30)

def test_c27_killed_worker_job_released_and_run_resumes(migrated, proc_env) -> None:
    a = worker(migrated, proc_env, JOBHUNTER_FAULT="collect.before_finalize:pause"); due_slot_now(migrated)
    wait_until(lambda: one(migrated, "SELECT count(*) FROM runs WHERE status='running'") == (1,), 120)
    a.send_signal(signal.SIGKILL); a.wait()
    one(migrated, "UPDATE worker_heartbeat SET heartbeat_at = now() - interval '2 minutes'")
    one(migrated, "UPDATE procrastinate_workers SET last_heartbeat = now() - interval '5 minutes'")
    b = worker(migrated, proc_env)
    wait_until(lambda: one(migrated, "SELECT status, attempt_no FROM runs") == ("succeeded", 2), 120)
    assert one(migrated, "SELECT count(*) FROM procrastinate_jobs WHERE status='doing'") == (0,)
    b.send_signal(signal.SIGTERM); exit_within(b, 30)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/integration/test_retention.py tests/crash/test_worker_process.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'jobhunter.privacy'`

- [ ] **Step 3: Implement `worker_main`, shutdown and retention**

`worker_main` order (§3.3.1, §3.3.5):
1. `configure_logging(cfg.log_level)`;
2. `LockSession.connect` and `acquire_lock` (via `asyncio.to_thread`);
3. `make_engine(cfg, pool_size=10)`, then `claim_epoch_and_sweep`; log `startup_check_passed` with `check="advisory_lock_acquired"` (assertion 6);
4. start `Watchdog`;
5. `make_queue_app(cfg.dsn("psycopg"), max_size=4)` and `open_async`; `open_saver(cfg.dsn("psycopg"))`; `build_noop_collect_graph(saver)`;
6. `run_reconcilers(ctx, "start")`;
7. outside the test env, `start_metrics_server(9100)`;
8. install SIGTERM and SIGINT handlers with `loop.add_signal_handler`, which set `shutdown.requested`;
9. `asyncio.gather` one `queue.run_worker_async(queues=[q], concurrency=n, install_signal_handlers=False, shutdown_graceful_timeout=GRACE_S, additional_context={"runtime": runtime}, name=f"jh-{q}")` per entry of `QUEUES`, plus a waiter on `shutdown.requested`.

`shutdown.graceful_shutdown(runtime, worker_tasks)`:
1. set `admission_stopped = True`;
2. cancel the procrastinate worker tasks (procrastinate stops fetching and waits up to `GRACE_S` for running jobs, then cancels them);
3. await them with a timeout of `GRACE_S + 10`;
4. close the saver, engine and lock session;
5. return 0.

No state is rewritten on shutdown.

The `reconcile_heavy` task body runs the `heavy` reconcilers and updates `TABLE_BYTES` with `pg_total_relation_size` for every table of `public`. `retention_daily` calls `run_retention_daily`. The CLI `worker` is `asyncio.run(worker_main(startup_or_exit("worker")))`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/integration/test_retention.py tests/crash/test_worker_process.py -q`
Expected: PASS (5 passed, about 6 min)

- [ ] **Step 5: Commit**

```bash
git add backend/src/jobhunter tests
git commit -m "feat(m0): worker composition with graceful shutdown, M0 retention, crash cases C17/C27"
```

---

### Task 14: API core: app factory, RFC 9457, request ids, headers, health, OpenAPI

**Files:**
- Create: `backend/src/jobhunter/api/__init__.py`, `api/app.py`, `api/errors.py`, `api/deps.py`, `api/openapi_export.py`, `api/routes/__init__.py`, `api/routes/health.py`, `backend/src/jobhunter/observability/health.py`
- Modify: `backend/src/jobhunter/cli.py` (`http-check`), `tests/support/fixtures.py` (fixture `api_client`)
- Test: `tests/integration/test_api_core.py`

**Interfaces:**
- Consumes: `make_engine`, `code_head`, `uuid7`, `HTTP_REQUESTS`, `AppConfig`.
- Produces:
  - `ProblemCode = Literal["unauthenticated", "csrf_failed", "throttled", "invalid_cursor", "stale_base_version", "validation_failed", "budget_exhausted", "filtered_requires_force", "run_active", "not_found", "conflict_state", "upload_rejected", "idempotency_key_required", "idempotency_key_reused", "idempotency_in_progress", "eval_gate_failed", "internal"]`
  - `class Problem(Exception)`: `__init__(self, status: int, code: ProblemCode, title: str, *, detail: str | None = None, errors: list[FieldError] | None = None, headers: Mapping[str, str] | None = None, extra: Mapping[str, JSONValue] | None = None)`; `@dataclass class FieldError: field: str; code: str; message: str`
  - `install_error_handlers(app: FastAPI) -> None`
  - `create_app(*, cfg: AppConfig | None = None, engine: AsyncEngine | None = None, extra_routers: Sequence[APIRouter] = ()) -> FastAPI` (module-level `app = create_app()` loads `startup_or_exit("api")` in its lifespan when `cfg` is None)
  - `deps.get_engine(request: Request) -> AsyncEngine`; `deps.get_config(request: Request) -> AppConfig`
  - `ReadinessCode = Literal["db", "migrations", "worker_heartbeat"]`; `@dataclass(frozen=True) class ReadinessReport: ok: bool; failing: list[ReadinessCode]`
  - `async def check_readiness(engine: AsyncEngine, *, code_head: str, now: datetime) -> ReadinessReport`
  - `SECURITY_HEADERS: Final[Mapping[str, str]]` (§16.6 exactly)
  - `openapi_export.main(argv: Sequence[str] | None = None) -> int` (writes OpenAPI JSON to `argv[0]`)
  - Fixture `api_client`: an `httpx.AsyncClient(transport=ASGITransport(app=create_app(cfg=…, engine=engine), client=("198.51.100.10", 50000)), base_url="https://testserver", headers={"Origin": "https://testserver"})`
  - CLI `jobhunter http-check <url>` (exit 0 on 2xx within 3 s, else 1)

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

async def fresh_heartbeat(engine) -> None:
    async with engine.begin() as c:
        await c.execute(sa.text("INSERT INTO worker_heartbeat VALUES (1, gen_random_uuid(), 1, 'h', 1, 1, 'v', now(), now()) "
                                "ON CONFLICT (id) DO UPDATE SET heartbeat_at = now()"))

async def test_healthz_and_headers(api_client) -> None:
    r = await api_client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    assert all(r.headers[k] == v for k, v in HEADERS.items())
    assert uuid.UUID(r.headers["x-request-id"]).version == 7

async def test_readyz(api_client, engine) -> None:
    assert (await api_client.get("/readyz")).json() == {"failing": ["worker_heartbeat"]}
    await fresh_heartbeat(engine)
    assert (await api_client.get("/readyz")).status_code == 200

async def test_problem_for_unknown_route_and_internal_error(engine, make_config) -> None:
    from jobhunter.api.app import create_app
    router = APIRouter()
    class Body(BaseModel): n: int = Field(ge=1, le=5)
    @router.post("/api/_probe")
    async def probe(b: Body) -> dict[str, int]: return {"n": b.n}
    @router.get("/api/_boom")
    async def boom() -> None: raise RuntimeError("secret detail")
    app = create_app(cfg=make_config("api"), engine=engine, extra_routers=[router])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="https://testserver") as c:
        r = await c.get("/nope")
        body = r.json()
        assert r.status_code == 404 and r.headers["content-type"] == "application/problem+json"
        assert body["code"] == "not_found" and body["type"] == "https://jobhunter.local/problems/not_found"
        assert body["request_id"] == r.headers["x-request-id"] and body["instance"] == "/nope"
        r = await c.post("/api/_probe", json={"n": 9}, headers={"Origin": "https://testserver", "X-JH-Request": "1"})
        assert r.status_code == 422 and r.json()["code"] == "validation_failed"
        assert r.json()["errors"][0]["field"] == "body.n"
        r = await c.get("/api/_boom")
        assert r.status_code == 500 and r.json()["code"] == "internal"
        assert "secret detail" not in r.text and "Traceback" not in r.text

def test_openapi_export(tmp_path) -> None:
    from jobhunter.api.openapi_export import main
    out = tmp_path / "openapi.json"; assert main([str(out)]) == 0
    assert json.loads(out.read_text())["openapi"].startswith("3.1")

@pytest.mark.slow
async def test_api_recovers_after_db_restart(fresh_pg_server, make_config) -> None:     # Review Focus 1
    from jobhunter.api.app import create_app
    from jobhunter.db.migrate import run_migrate
    from jobhunter.db.session import make_engine
    from tests.support.pg import pg_start, pg_stop
    run_migrate(make_config("migrate", fresh_pg_server)); cfg = make_config("api", fresh_pg_server); engine = make_engine(cfg)
    await fresh_heartbeat(engine)
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

- [ ] **Step 3: Implement the app factory, errors, middleware and health**

- **Middleware**, as one pure-ASGI middleware in `app.py`, in this order:
  1. a request id (`uuid7()`), bound into structlog contextvars and set as `X-Request-Id`;
  2. `SECURITY_HEADERS` on every response;
  3. `HTTP_REQUESTS.labels(route=<route template or "unmatched">, status).inc()`.
- **Error handlers** (`api/errors.py`, BLE001 allowed):
  - `Problem` renders as the §14.4 JSON;
  - `RequestValidationError` → 422 `validation_failed`, with `errors[]` whose `field` joins `loc` with `.` and whose `code` is the pydantic error type;
  - Starlette 404 → 404 `not_found`; 405 → 405 with code `not_found` (no such method on this path);
  - anything else → 500 `internal`, logged as `unhandled_exception` with `error_class` only.

  Every problem carries `instance = request.url.path` and `request_id`.
- **`check_readiness`**:
  - `SELECT 1` under `asyncio.wait_for(…, 2.0)`; a failure or timeout means `db`, and the other checks are skipped;
  - `SELECT version_num FROM alembic_version` must equal `code_head` (`migrations`);
  - `SELECT now() - heartbeat_at FROM worker_heartbeat` must be ≤ 60 s (`worker_heartbeat`).
- **Routes**: `/healthz` → `{"status":"ok"}`; `/readyz` → 200 `{}` or 503 `{"failing":[…]}`.
- **State**: when `cfg` and `engine` are injected, `create_app` sets `app.state.cfg`, `app.state.engine` and `app.state.code_head` immediately, because tests use `httpx.ASGITransport`, which does not run the lifespan. Otherwise the lifespan loads `startup_or_exit("api")`, creates the engine (pool size 10), computes `code_head()` once and starts `start_metrics_server(8001)` when `cfg.env != "test"`.
- **`openapi_export.main`** writes `json.dumps(create_app(cfg=None).openapi(), indent=2, sort_keys=True)` without starting the lifespan.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/integration/test_api_core.py -q`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/src/jobhunter tests
git commit -m "feat(m0): API core with RFC 9457 problems, request ids, security headers, readiness and OpenAPI export"
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
  - `crypto.hkdf_sha256(root: bytes, info: bytes, length: int = 32) -> bytes`; `crypto.key_id(key: bytes) -> bytes` (the first 8 bytes of `sha256(key)`)
  - `crypto.encrypt(key: bytes, plaintext: bytes, *, aad: bytes) -> bytes` (format `b"\x01" ‖ key_id ‖ nonce(12) ‖ ciphertext+tag`); `crypto.decrypt(keys: Sequence[bytes], blob: bytes, *, aad: bytes) -> bytes` (picks the key by key id; raises `crypto.DecryptError`)
  - `passwords.ARGON2: Final = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4, hash_len=32, salt_len=16)`; `normalize_password(p: str) -> str` (NFC); `class WeakPassword(ValueError)` with `reason: Literal["too_short", "common"]`; `check_password_policy(p: str) -> None`; `hash_password(p: str) -> str`; `verify_password(stored: str, p: str) -> tuple[bool, str | None]` (a new hash when parameters changed); `dummy_verify(p: str) -> None`
  - `users.normalize_username(u: str) -> str` (NFC, stripped); `@dataclass(frozen=True) class UserRow: username: str; password_hash: str; totp_secret_enc: bytes | None; totp_enabled: bool`; `class UserExists(Exception)`
  - `async def users.create_user(conn, username: str, password: str, *, now: datetime) -> None`; `async def users.find_user(conn, username: str) -> UserRow | None` (casefold match against the single user); `async def users.set_password(conn, password: str, *, now: datetime) -> int` (also deletes every session; returns the number deleted); `async def users.update_hash(conn, new_hash: str) -> None`; `async def users.enable_totp(conn, secret_b32: str, *, data_key: bytes) -> None`; `async def users.disable_totp(conn) -> None`; `users.totp_secret(row: UserRow, *, keys: Sequence[bytes]) -> str | None`
  - `totp.new_secret() -> str`; `totp.provisioning_uri(secret: str, username: str) -> str` (issuer `Job Hunter`); `totp.verify(secret: str, code: str, *, at: datetime) -> bool` (`valid_window=1`)
  - CLI:
    - `user create --username <u> [--password-stdin]` prompts twice with `getpass`. `--password-stdin` reads one line and is refused in prod with exit 2.
    - `user totp enable` prints the URI and a `segno` terminal QR code, prompts for a current code, and stores the secret only when the code verifies.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_crypto.py
import os, pytest
from jobhunter.crypto import DecryptError, decrypt, encrypt, hkdf_sha256, key_id
def test_roundtrip_key_id_and_rotation() -> None:
    old, new = os.urandom(32), os.urandom(32)
    blob = encrypt(old, b"JBSWY3DPEHPK3PXP", aad=b"totp")
    assert blob[0] == 1 and blob[1:9] == key_id(old)
    assert decrypt([new, old], blob, aad=b"totp") == b"JBSWY3DPEHPK3PXP"
    with pytest.raises(DecryptError): decrypt([old], blob, aad=b"other")
    with pytest.raises(DecryptError): decrypt([old], blob[:-1] + bytes([blob[-1] ^ 1]), aad=b"totp")
    with pytest.raises(DecryptError): decrypt([new], blob, aad=b"totp")
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
    words = files("jobhunter.auth").joinpath("common_passwords.txt").read_text().split()
    common = next(w for w in words if len(w) >= 12 and not w.startswith("#"))
    with pytest.raises(WeakPassword) as e2:
        check_password_policy(common.upper())
    assert e2.value.reason == "common"
    check_password_policy("Tannenbaum-Ölfass-7")
def test_nfc_and_nfd_passwords_verify_alike() -> None:          # Review Focus 3
    nfc = unicodedata.normalize("NFC", "Müllerstraße-2026!")
    ok, _ = verify_password(hash_password(nfc), unicodedata.normalize("NFD", nfc))
    assert ok
def test_rehash_when_parameters_change() -> None:
    old = PasswordHasher(time_cost=2, memory_cost=65536, parallelism=4).hash("Tannenbaum-Ölfass-7")
    ok, new = verify_password(old, "Tannenbaum-Ölfass-7")
    assert ok and new is not None and new.startswith("$argon2id$v=19$m=65536,t=3,p=4$")
```

```python
# tests/integration/test_users.py
import os, subprocess, sys
from datetime import UTC, datetime, timedelta
import pytest, sqlalchemy as sa
from jobhunter.auth import totp, users
from jobhunter.clock import utcnow
pytestmark = pytest.mark.integration
PW = "Tannenbaum-Ölfass-7"

async def test_single_user_and_username_rules(engine) -> None:
    async with engine.begin() as c:
        await users.create_user(c, "  Ada Lovelace ", PW, now=utcnow())
        with pytest.raises(users.UserExists): await users.create_user(c, "other", PW, now=utcnow())
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT username FROM users"))).scalar_one() == "Ada Lovelace"

async def test_username_match_ignores_case_and_outer_spaces(engine) -> None:      # Review Focus 3
    async with engine.begin() as c: await users.create_user(c, "Straße", PW, now=utcnow())
    async with engine.connect() as c:
        assert await users.find_user(c, " STRASSE ") is not None
        assert await users.find_user(c, "strasse2") is None

async def test_set_password_revokes_sessions(engine) -> None:
    async with engine.begin() as c:
        await users.create_user(c, "ada", PW, now=utcnow())
        await c.execute(sa.text("INSERT INTO sessions (token_hmac,user_id,idle_expires_at,absolute_expires_at) VALUES ('\\x01',1,now(),now())"))
        assert await users.set_password(c, "Another-Long-Pass-9", now=utcnow()) == 1

async def test_totp_window_and_encrypted_storage(engine) -> None:
    key = os.urandom(32); secret = totp.new_secret(); now = datetime(2026, 10, 16, 10, tzinfo=UTC)
    import pyotp
    assert totp.verify(secret, pyotp.TOTP(secret).at(now - timedelta(seconds=30)), at=now)
    assert not totp.verify(secret, pyotp.TOTP(secret).at(now - timedelta(seconds=90)), at=now)
    async with engine.begin() as c:
        await users.create_user(c, "ada", PW, now=utcnow()); await users.enable_totp(c, secret, data_key=key)
        row = await users.find_user(c, "ada")
    assert row is not None and row.totp_enabled and secret.encode() not in (row.totp_secret_enc or b"")
    assert users.totp_secret(row, keys=[key]) == secret

def test_cli_create_with_stdin_never_echoes(migrated, proc_env, engine) -> None:   # engine: truncation afterwards
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

- **`common_passwords.txt`**: the SecLists file `Passwords/Common-Credentials/10k-most-common.txt` (MIT), one password per line. Its first line is a `#` comment naming the source and the upstream commit SHA. Membership is checked case-insensitively against a frozenset loaded once.
- **Passwords**: `hash_password`, `verify_password` and `check_password_policy` apply `normalize_password` first. The policy is ≥ 12 characters and not in the list.
- **`verify_password`** uses `ARGON2.verify` and `ARGON2.check_needs_rehash`; a `VerifyMismatchError` returns `(False, None)`.
- **`dummy_verify`** verifies against a hash constant computed at import from a random password.
- **`create_user`**:
  - the username is 3–64 characters after `normalize_username`, else `ValueError("username_length")`;
  - `INSERT INTO users (id, username, password_hash, password_changed_at) VALUES (1, …)`; a `UniqueViolation` on the primary key raises `UserExists`.
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

### Task 16: Login, sessions, throttling, CSRF and rate limit

**Files:**
- Create: `backend/src/jobhunter/auth/sessions.py`, `backend/src/jobhunter/auth/throttle.py`, `backend/src/jobhunter/api/security.py`, `backend/src/jobhunter/api/routes/auth.py`, `docs/spikes/V20.md`
- Modify: `backend/src/jobhunter/api/app.py` (CSRF middleware, the auth router), `backend/src/jobhunter/api/deps.py` (`require_session`), `backend/src/jobhunter/cli.py` (`sessions revoke-all|unlock`), `tests/support/fixtures.py` (`make_user`, `logged_in_client`)
- Test: `tests/integration/test_auth.py`

**Interfaces:**
- Consumes: `users.*`, `passwords.*`, `totp.verify`, `crypto.hkdf_sha256`, `raise_alert`, `resolve_alert`, `Problem`.
- Produces:
  - `sessions.SESSION_COOKIE: Final = "__Host-jh_session"`; `IDLE: Final = timedelta(hours=12)`; `ABSOLUTE: Final = timedelta(days=30)`; `SLIDE_EVERY: Final = timedelta(minutes=1)`; `COOKIE_MAX_AGE_S: Final = 2_592_000`
  - `sessions.session_pepper(session_secret: bytes) -> bytes` (`hkdf_sha256(secret, b"session")`); `sessions.token_hmac(token: str, pepper: bytes) -> bytes`
  - `@dataclass(frozen=True) class SessionInfo`: `token_hmac: bytes`, `username: str`, `totp_enabled: bool`, `idle_expires_at: datetime`, `absolute_expires_at: datetime`
  - `async def sessions.create_session(conn, *, pepper: bytes, ip: str | None, user_agent: str | None, now: datetime) -> str`; `async def sessions.validate_session(conn, token: str, *, pepper: bytes, now: datetime) -> SessionInfo | None`; `async def sessions.delete_session(conn, token: str, *, pepper: bytes) -> None`; `async def sessions.revoke_all(conn) -> int`
  - `throttle.IP_MAX_FAILURES: Final = 5`, `IP_WINDOW = timedelta(minutes=15)`, `IP_LOCK = timedelta(minutes=15)`, `GLOBAL_MAX_FAILURES: Final = 20`, `GLOBAL_WINDOW = timedelta(hours=1)`, `GLOBAL_LOCK = timedelta(hours=1)`
  - `throttle.username_hmac(username: str, session_secret: bytes) -> bytes` (HMAC-SHA256 of the casefolded NFC username with `hkdf_sha256(secret, b"login-username")`)
  - `async def throttle.active_lockout(conn, ip: str, *, now: datetime) -> timedelta | None`; `async def throttle.record_attempt(conn, *, ip: str, username_hmac: bytes, success: bool, now: datetime) -> None`; `async def throttle.unlock_all(conn) -> int`
  - `security.SessionRateLimiter(limit: int = 600, window_s: float = 60.0)` with `hit(key: bytes, now: float) -> bool`
  - `deps.require_session(request: Request) -> SessionInfo` (401 `unauthenticated` and cookie cleared; 429 `throttled` past 600/min)
  - Routes:
    - `POST /api/auth/login`: `LoginIn{username: str(1..256), password: str(1..1024), totp: str | None (^\d{6}$)}` → 204 with `Set-Cookie`;
    - `POST /api/auth/logout` → 204;
    - `GET /api/auth/session` → `SessionOut{username, totp_enabled, idle_expires_at, absolute_expires_at}`.
  - Fixtures: `make_user(username: str = "ada", password: str = "Tannenbaum-Ölfass-7") -> Awaitable[None]` (async factory); `logged_in_client`, a **separate** `httpx.AsyncClient` on the same app as `api_client`, which calls `make_user()`, logs in, and keeps the session cookie and the default headers `Origin: https://testserver` and `X-JH-Request: 1`

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_auth.py
from datetime import timedelta
import httpx, pytest, sqlalchemy as sa
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
pytestmark = pytest.mark.integration
PW = "Tannenbaum-Ölfass-7"
def login(c, password=PW, ip=None, **extra):
    return c.post("/api/auth/login", json={"username": "ada", "password": password, **extra})

async def test_login_sets_host_cookie_and_stores_only_hmac(api_client, make_user, engine) -> None:
    await make_user()
    r = await login(api_client)
    assert r.status_code == 204
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("__Host-jh_session=") and "Domain" not in cookie
    for attr in ("Secure", "HttpOnly", "SameSite=Strict", "Path=/", "Max-Age=2592000"): assert attr in cookie
    token = cookie.split(";")[0].split("=", 1)[1]
    async with engine.connect() as c:
        stored = (await c.execute(sa.text("SELECT token_hmac FROM sessions"))).scalar_one()
    assert token.encode() not in stored and len(stored) == 32

async def test_failures_are_generic_and_constant_path(api_client, make_user, monkeypatch) -> None:
    await make_user(); called = []
    import jobhunter.api.routes.auth as auth_routes
    monkeypatch.setattr(auth_routes, "dummy_verify", lambda p: called.append(1))
    a = await login(api_client, password="wrong-password-123")
    b = await api_client.post("/api/auth/login", json={"username": "nobody", "password": "wrong-password-123"})
    assert a.status_code == b.status_code == 401 and a.json()["title"] == b.json()["title"] and called == [1]

async def test_login_checks_origin_only(api_client, make_user) -> None:
    await make_user()
    r = await api_client.post("/api/auth/login", json={"username": "ada", "password": PW}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["code"] == "csrf_failed"
    assert (await login(api_client)).status_code == 204        # no X-JH-Request needed for login

async def test_ip_lockout_after_five_failures(api_client, make_user, engine) -> None:
    await make_user()
    for _ in range(5): assert (await login(api_client, password="x" * 12)).status_code == 401
    r = await login(api_client)
    assert r.status_code == 429 and r.json()["code"] == "throttled" and 880 <= r.json()["retry_after_s"] <= 900
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT count(*) FROM login_attempts WHERE username_hmac = convert_to('ada','UTF8')"))).scalar_one() == 0

async def test_global_lockout_after_twenty_failures(engine, make_config, make_user) -> None:
    from jobhunter.api.app import create_app
    await make_user(); app = create_app(cfg=make_config("api"), engine=engine)
    for i in range(20):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(f"203.0.113.{i + 1}", 1)),
                                     base_url="https://testserver", headers={"Origin": "https://testserver"}) as c:
            await login(c, password="y" * 12)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("192.0.2.200", 1)),
                                 base_url="https://testserver", headers={"Origin": "https://testserver"}) as c:
        assert (await login(c)).status_code == 429
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT severity FROM alerts WHERE code='auth.lockout' AND resolved_at IS NULL"))).scalar_one() == "critical"

async def test_session_idle_absolute_and_slide(logged_in_client, engine) -> None:
    assert (await logged_in_client.get("/api/auth/session")).status_code == 200
    async with engine.begin() as c:
        await c.execute(sa.text("UPDATE sessions SET last_seen_at = now() - interval '30 seconds'"))
        before = (await c.execute(sa.text("SELECT idle_expires_at FROM sessions"))).scalar_one()
    await logged_in_client.get("/api/auth/session")
    async with engine.begin() as c:
        assert (await c.execute(sa.text("SELECT idle_expires_at FROM sessions"))).scalar_one() == before   # < 1 min: no slide
        await c.execute(sa.text("UPDATE sessions SET idle_expires_at = now() - interval '1 second'"))
    r = await logged_in_client.get("/api/auth/session")
    assert r.status_code == 401 and r.json()["code"] == "unauthenticated" and "Max-Age=0" in r.headers["set-cookie"]

async def test_csrf_on_state_changing_routes(logged_in_client) -> None:
    r = await logged_in_client.post("/api/auth/logout", headers={"X-JH-Request": ""})
    assert r.status_code == 403 and r.json()["code"] == "csrf_failed"
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
        ips = [str(r[0]) for r in await c.execute(sa.text("SELECT host(ip) FROM login_attempts ORDER BY id"))]
    assert ips == ["203.0.113.7", "198.51.100.9"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/integration/test_auth.py -q`
Expected: FAIL with `fixture 'make_user' not found`

- [ ] **Step 3: Implement sessions, throttling, CSRF, the rate limit and the auth routes**

**Login order** (`routes/auth.py`, which imports `dummy_verify` by name so tests can observe it):
1. The Origin check is done by the middleware.
2. `active_lockout(ip)` → 429 `throttled` with `extra={"retry_after_s": ceil(seconds)}`.
3. `find_user`; when none, `dummy_verify`.
4. `verify_password`, then the TOTP check when enabled (a missing or wrong code is a failure).
5. `record_attempt(success=...)`.
6. On failure, 401 `unauthenticated` with the title `"Invalid credentials"`.
7. On success: `update_hash` when rehashed, `create_session`, then `Set-Cookie: __Host-jh_session=<token>; Max-Age=2592000; Path=/; Secure; HttpOnly; SameSite=Strict`.

The IP is `request.client.host`.

**`record_attempt`** inserts the attempt, then:
- when the failures of the IP in the last 15 min reach 5, it upserts `login_lockouts('ip:<ip>', now + 15 min, 'ip_failures')`;
- when all failures in the last hour reach 20, it upserts `login_lockouts('global', now + 1 h, 'global_failures')` and raises `auth.lockout` (critical, subject `global`).

**`active_lockout`** returns the larger remaining time of `global` and `ip:<ip>` rows still in force. When the global row has expired, it resolves the `auth.lockout` alert.

**Sessions**:
- The token is `secrets.token_urlsafe(32)`; the stored key is `HMAC-SHA256(token, pepper)`.
- On create: `idle_expires_at = now + 12 h`, `absolute_expires_at = now + 30 d`, `user_agent_sha256 = sha256(ua)`.
- **`validate_session`** rejects rows that are revoked, past their idle expiry or past their absolute expiry. It slides (`last_seen_at = now`, `idle_expires_at = least(now + 12 h, absolute)`) only when `now − last_seen_at ≥ 1 min`.

**CSRF middleware**: for POST, PUT, PATCH and DELETE under `/api/`:
- `Origin` must equal `cfg.public_origin`;
- `X-JH-Request: 1` is also required, except on `/api/auth/login`;
- otherwise 403 `csrf_failed`.

**Rate limit**: `require_session` validates the cookie, then calls `SessionRateLimiter.hit(token_hmac, time.monotonic())`, an in-memory sliding window over the last 60 s (the api runs one replica).

**CLI**: `sessions revoke-all` calls `revoke_all`; `sessions unlock` calls `unlock_all` and resolves `auth.lockout`.

**`V20.md`** records the dev part of V20 (this test, plus the devproxy check of Task 18). The Caddy part lands in M5.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/integration/test_auth.py -q`
Expected: PASS (10 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/src/jobhunter docs/spikes/V20.md tests
git commit -m "feat(m0): login with DB-backed throttling, __Host- sessions, CSRF, per-session rate limit, optional TOTP"
```

---

### Task 17: Idempotency store, Run now and system health

**Files:**
- Create: `backend/src/jobhunter/api_idempotency.py`, `backend/src/jobhunter/api/routes/runs.py`, `backend/src/jobhunter/api/routes/system.py`
- Modify: `backend/src/jobhunter/observability/health.py` (`system_health`), `backend/src/jobhunter/api/app.py` (routers; procrastinate app bound at startup through `make_queue_app`)
- Test: `tests/unit/test_idempotency_fingerprint.py`, `tests/integration/test_runs_api.py`

**Interfaces:**
- Consumes: `request_manual_run`, `defer_in_tx`, `make_queue_app`, `run_tick`, `check_readiness`, `require_session`, `Problem`.
- Produces:
  - `IDEMPOTENCY_TTL: Final = timedelta(days=7)`; `IDEM_KEY_RE: Final = re.compile(r"^[A-Za-z0-9_-]{16,128}$")`; `IN_PROGRESS_LOCK_TIMEOUT: Final = "5s"`
  - `request_fingerprint(body: JSONValue, path_params: Mapping[str, JSONValue]) -> bytes` (`sha256(canonical_json({"body": body, "path": path_params}))`)
  - `@dataclass(frozen=True) class StoredResponse`: `status: int`, `body: dict[str, JSONValue]`
  - `async def run_idempotent(conn: AsyncConnection, *, route: str, key: str | None, fingerprint: bytes, now: datetime, handler: Callable[[AsyncConnection], Awaitable[StoredResponse]]) -> StoredResponse`
  - `POST /api/runs`: `RunNowIn{include_paid: bool}` → 202 `RunNowOut{run: RunOut, created: bool}`, where `RunOut{id, kind, trigger, status, scheduled_slot, include_paid, attempt_no, queued_at, started_at, finished_at}`
  - `GET /api/system/health` → `SystemHealthOut`:
    - `ready: {ok, failing}`;
    - `last_run: RunOut | null`, `last_run_age_s: float | null`;
    - `last_successful_run_age_s: float | null` (counting `succeeded` and `partial`);
    - `last_fully_successful_run_age_s: float | null`;
    - `queue_depths: dict[str, dict[str, int]]` (procrastinate jobs by queue and status);
    - `alerts_open: {info, warn, critical}`;
    - `table_bytes: dict[str, int]`;
    - `versions: {app, python, fastapi, sqlalchemy, psycopg, procrastinate, langgraph}`.
  - `async def system_health(conn: AsyncConnection, *, engine: AsyncEngine, code_head: str, now: datetime) -> SystemHealthOut`

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
pytestmark = pytest.mark.integration
KEY = "run-now-key-000001"

async def post(c, key: str | None = KEY, include_paid: bool = False):
    return await c.post("/api/runs", json={"include_paid": include_paid}, headers={"Idempotency-Key": key} if key else {})

async def test_key_required_and_format(logged_in_client) -> None:
    for key in (None, "short", "has space in it!!!"):
        r = await post(logged_in_client, key)
        assert r.status_code == 400 and r.json()["code"] == "idempotency_key_required"

async def test_same_key_returns_stored_response_and_one_run(logged_in_client, engine, queue_app) -> None:
    a = await post(logged_in_client); b = await post(logged_in_client)
    assert a.status_code == b.status_code == 202 and a.json() == b.json() and a.json()["created"] is True
    assert a.json()["run"]["trigger"] == "manual" and a.json()["run"]["scheduled_slot"] is None
    async with engine.begin() as c:
        await c.execute(sa.text("UPDATE runs SET status='cancelled', finished_at=now()"))
    assert (await post(logged_in_client)).json() == a.json()          # even after the run finished
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT count(*) FROM runs"))).scalar_one() == 1

async def test_reused_key_with_other_body_is_422(logged_in_client, queue_app) -> None:
    await post(logged_in_client)
    r = await post(logged_in_client, include_paid=True)
    assert r.status_code == 422 and r.json()["code"] == "idempotency_key_reused"

async def test_new_key_returns_active_run(logged_in_client, queue_app) -> None:
    a = await post(logged_in_client); b = await post(logged_in_client, key="run-now-key-000002")
    assert b.json()["created"] is False and b.json()["run"]["id"] == a.json()["run"]["id"]

async def test_expired_key_executes_again(logged_in_client, engine, queue_app) -> None:
    await post(logged_in_client)
    async with engine.begin() as c:
        await c.execute(sa.text("UPDATE api_idempotency SET expires_at = now() - interval '1 second'"))
        await c.execute(sa.text("UPDATE runs SET status='cancelled', finished_at=now()"))
    assert (await post(logged_in_client)).json()["created"] is True

async def test_run_now_races_tick(logged_in_client, engine, queue_app) -> None:        # Review Focus 4
    from jobhunter.worker.tasks import defer_in_tx
    from jobhunter.worker.tick import run_tick
    now = utcnow()
    async with engine.begin() as c:
        await c.execute(sa.text("UPDATE schedule_state SET anchor_utc=:a, interval_s=3600, last_evaluated_slot=:l"),
                        {"a": now - timedelta(minutes=1), "l": now - timedelta(hours=1, minutes=1)})
    async def tick() -> None:
        async with engine.begin() as c: await run_tick(c, now=now, defer=defer_in_tx)
    results = await asyncio.gather(tick(), post(logged_in_client), return_exceptions=True)
    assert not any(isinstance(x, Exception) for x in results)
    assert results[1].status_code == 202
    async with engine.connect() as c:
        active = (await c.execute(sa.text("SELECT id FROM runs WHERE status IN ('queued','running')"))).all()
    assert len(active) == 1 and results[1].json()["run"]["id"] == active[0][0]

async def test_system_health_requires_session_and_reports(api_client, logged_in_client, queue_app) -> None:
    assert (await api_client.get("/api/system/health")).status_code == 401
    await post(logged_in_client)
    body = (await logged_in_client.get("/api/system/health")).json()
    assert body["last_run"]["status"] == "queued" and body["alerts_open"] == {"info": 0, "warn": 0, "critical": 0}
    assert body["queue_depths"]["collect"]["todo"] == 1 and body["versions"]["procrastinate"] == "3.10.0"
    assert "worker_heartbeat" in body["ready"]["failing"] and body["table_bytes"]["runs"] > 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_idempotency_fingerprint.py tests/integration/test_runs_api.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'jobhunter.api_idempotency'`

- [ ] **Step 3: Implement the store and the two routes**

**`run_idempotent`** runs inside the caller's transaction:
1. A missing key, or one that does not match `IDEM_KEY_RE`, → 400 `idempotency_key_required`.
2. `SET LOCAL lock_timeout = '5s'`, then `INSERT … (status='in_progress', expires_at = now + 7 d) ON CONFLICT (route, idem_key) DO NOTHING RETURNING 1`.
   - A `LockNotAvailable` (55P03) means a concurrent first request holds the row → 409 `idempotency_in_progress`.
   - When inserted: `resp = await handler(conn)`, then `UPDATE … SET status='completed', response_status, response_body`, and return `resp`.
3. Otherwise, select the row:
   - expired (`expires_at ≤ now`): delete it and retry the insert once;
   - a different `request_sha256` → 422 `idempotency_key_reused`;
   - `status = 'in_progress'` → 409 `idempotency_in_progress`;
   - else return the stored response.

**`POST /api/runs`** (route `"POST /api/runs"`) wraps `request_manual_run(include_paid=…, defer=defer_in_tx)` in `run_idempotent`, inside one `engine.begin()` transaction.

**`system_health`** = `check_readiness` plus aggregate queries (`runs` ordered by `queued_at DESC`; `procrastinate_jobs` grouped by queue and status; open alerts by severity; `pg_total_relation_size` per public table) and `importlib.metadata.version` for the libraries.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_idempotency_fingerprint.py tests/integration/test_runs_api.py -q`
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/src/jobhunter tests
git commit -m "feat(m0): Idempotency-Key store, Run now endpoint and system health"
```

---

### Task 18: Dev stack and the dev part of V21

**Files:**
- Create: `Dockerfile`, `.dockerignore`, `deploy/compose.dev.yaml`, `deploy/Caddyfile.dev`, `scripts/dev-secrets.sh`, `docs/spikes/V21.md`
- Modify: `docs/spikes/V20.md` (devproxy result)
- Test: `tests/stack/__init__.py`, `tests/stack/test_dev_stack.py`

**Interfaces:**
- Consumes: the CLI commands `migrate`, `worker`, `worker-health` and `http-check`, and the module `jobhunter.api.app:app`.
- Produces:
  - Image `jobhunter-app:dev`: `python:3.13.16-slim-bookworm`, `uv sync --frozen --no-dev` into `/opt/venv`, `USER 10001:10001`.
  - Compose project `jobhunter-dev` with services:
    - `postgres` (the pinned image, `deploy/postgres` mounted, secrets `pg_bootstrap, db_app, db_migrator, db_backup, db_restore`);
    - `migrate`;
    - `api` (`uvicorn jobhunter.api.app:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips=172.30.0.2`);
    - `worker` (`stop_grace_period: 120s`, `deploy.replicas: 1`);
    - `devproxy` (`caddy:2.11.7-alpine`; `devedge` and `proxy` with `ipv4_address: 172.30.0.2`; port `127.0.0.1:8080:8080`).
  - Networks: `proxy` (`internal: true`, subnet `172.30.0.0/24`), `internal` (`internal: true`), `egress`, `devedge`.
  - The app services use the `x-app` anchor of §19.1 (`read_only`, `tmpfs: [/tmp]`, `cap_drop: [ALL]`, `no-new-privileges`, `user: "10001:10001"`, `group_add: ["10010"]`, local logging driver). `JOBHUNTER_PUBLIC_ORIGIN=http://localhost:8080`.
  - `scripts/dev-secrets.sh [dir]` (default `.secrets/dev`) creates any missing secret with `umask 0227`, mode 0440, `chgrp 10010` (when permitted). It never overwrites. `session_secret` and `data_key` are `openssl rand -base64 32`; the passwords are `openssl rand -hex 24`.

- [ ] **Step 1: Write the failing test**

```python
# tests/stack/test_dev_stack.py
import json, subprocess, time
from pathlib import Path
import httpx, pytest
pytestmark = pytest.mark.stack
REPO = Path(__file__).resolve().parents[2]
DC = ["docker", "compose", "-f", str(REPO / "deploy/compose.dev.yaml")]

PROBE_NAME = "jh-egress-probe"

@pytest.fixture(scope="module")
def stack():
    subprocess.run([str(REPO / "scripts/dev-secrets.sh")], check=True)
    subprocess.run(DC + ["up", "-d", "--build", "--wait"], check=True, timeout=900)
    subprocess.run(["docker", "run", "-d", "--rm", "--name", PROBE_NAME, "--network", "jobhunter-dev_egress",
                    "python:3.13.16-slim-bookworm", "python", "-m", "http.server", "8099"], check=True)
    yield
    subprocess.run(["docker", "rm", "-f", PROBE_NAME], check=False)
    subprocess.run(DC + ["down", "-v"], check=True)

def exec_(svc: str, code: str) -> int:
    return subprocess.run(DC + ["exec", "-T", svc, "python", "-c", code]).returncode

def test_dev_stack_serves_through_devproxy(stack) -> None:
    r = httpx.get("http://127.0.0.1:8080/healthz", timeout=5)
    assert r.status_code == 200 and r.headers["x-content-type-options"] == "nosniff"
    for _ in range(30):
        if httpx.get("http://127.0.0.1:8080/readyz", timeout=5).status_code == 200: break
        time.sleep(2)
    else: pytest.fail("readyz never 200")

PROBE = "import socket,sys\ntry:\n socket.create_connection(({host!r},{port}),timeout=3)\nexcept OSError:\n sys.exit(1)"

def test_v21_dev_api_has_no_egress_but_worker_does(stack) -> None:
    time.sleep(2)                                                          # the probe server starts
    assert exec_("api", PROBE.format(host="1.1.1.1", port=443)) == 1      # no route out of internal networks
    assert exec_("api", PROBE.format(host=PROBE_NAME, port=8099)) == 1    # api is not on the egress network
    assert exec_("worker", PROBE.format(host=PROBE_NAME, port=8099)) == 0 # the worker is
    assert exec_("worker", PROBE.format(host="postgres", port=5432)) == 0

def test_secrets_are_0440_and_not_overwritten() -> None:
    d = REPO / ".secrets/dev"; before = (d / "db_app").read_bytes()
    subprocess.run([str(REPO / "scripts/dev-secrets.sh")], check=True)
    assert (d / "db_app").read_bytes() == before and oct((d / "db_app").stat().st_mode & 0o777) == "0o440"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/stack/test_dev_stack.py -m stack -q`
Expected: FAIL with `No such file or directory: '…/scripts/dev-secrets.sh'`

- [ ] **Step 3: Write the Dockerfile, the compose file, the Caddyfile and the secrets script**

- **`Dockerfile`**:
  - stage `build`: `COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /usr/local/bin/uv` (†), then `uv sync --frozen --no-dev --no-editable`;
  - stage `runtime`: copy `/opt/venv`, `ENV PATH=/opt/venv/bin:$PATH`, `USER 10001:10001`.
- **`Caddyfile.dev`**:

```
:8080 {
  reverse_proxy api:8000 {
    header_up X-Forwarded-For {remote_host}
  }
}
```

- **`compose.dev.yaml`**:
  - `migrate` depends on postgres being healthy;
  - `api` and `worker` depend on `migrate: service_completed_successfully`;
  - api healthcheck `jobhunter http-check http://127.0.0.1:8000/healthz`; worker healthcheck `jobhunter worker-health`;
  - postgres healthcheck `pg_isready -U $$POSTGRES_USER`.
- **`V21.md`** records the observed results of `test_v21_dev_api_has_no_egress_but_worker_does`. It notes that internet reachability from the worker is not asserted in the dev container (outbound traffic there goes through the agent proxy), and that DNS behaviour plus the `DOCKER-USER` rule are verified in M5 on the VPS image.
- **`V20.md`** adds the result of one manual login through `http://127.0.0.1:8080` with a spoofed `X-Forwarded-For`: `login_attempts.ip` is the Docker gateway address, not the spoofed value.

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/stack/test_dev_stack.py -m stack -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add Dockerfile .dockerignore deploy/compose.dev.yaml deploy/Caddyfile.dev scripts/dev-secrets.sh docs/spikes tests/stack
git commit -m "feat(m0): dev stack (postgres, migrate, api, worker, devproxy) with internal-only api network (V21 dev part)"
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
  - npm scripts: `dev`, `build`, `typecheck` (`tsc --noEmit`), `lint` (`eslint .`), `test` (`vitest run`), `gen:api` (`openapi-typescript openapi.json -o src/api/schema.d.ts`)
  - `scripts/check-openapi-drift.sh` (exit 1 when the regenerated `schema.d.ts` differs from the committed one)

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

- Exact pins in `package.json`: `vite@8.3.4`, `react@19.3.1`, `react-dom@19.3.1`, `openapi-typescript@7.13.0`, `vitest@5.0.5`, `@vitejs/plugin-react` (†, the version compatible with Vite 8 at install), `eslint` with `typescript-eslint` (†), and `typescript@7.0.2`.
- **V16**: run `npm run build && npm run lint && npm run typecheck`.
  - If any of them fails under TypeScript 7.0.2, pin `typescript@5.9.3` and re-run.
  - `V16.md` records which one passed and why.
- `main.tsx` renders `<h1>Job Hunter</h1>` with no inline script or style (§16.6 CSP).
- Generate the types with `uv run python -m jobhunter.api.openapi_export frontend/openapi.json && npm --prefix frontend run gen:api`, and commit `schema.d.ts`.

- [ ] **Step 4: Run the checks to verify they pass**

Run: `cd frontend && npx vitest run && npm run build && npm run lint && npm run typecheck && cd .. && scripts/check-openapi-drift.sh`
Expected: PASS (2 tests passed, build, lint and typecheck clean, the drift script exits 0)

- [ ] **Step 5: Commit**

```bash
git add frontend scripts/check-openapi-drift.sh docs/spikes/V16.md
git commit -m "feat(m0): SPA skeleton with typed API client and generated OpenAPI types (V16)"
```

---

### Task 20: CI workflow

**Files:**
- Create: `.github/workflows/ci.yml`
- Test: `actionlint` over the workflow, then a pushed run

**Interfaces:**
- Consumes: every command of Tasks 1–19.
- Produces: the CI jobs `lint`, `unit`, `integration`, `frontend` and `audit` (§18.8 subset for M0).

- [ ] **Step 1: Write the workflow**

| Job | Steps |
|---|---|
| `lint` | `actions/checkout@v5`; `astral-sh/setup-uv@v6` with `version: "0.13.1"`; `uv sync --frozen`; `uv run ruff check .`; `uv run ruff format --check .`; `uv run mypy`; `actions/setup-node@v5` with `node-version: "22.22.3"`; `npm ci --prefix frontend`; `npm run --prefix frontend lint`; `npm run --prefix frontend typecheck`; `gitleaks/gitleaks-action@v2` |
| `unit` | uv setup; `uv run pytest tests/unit tests/property -q` |
| `integration` | uv setup; `docker pull pgvector/pgvector:0.8.7-pg17-bookworm`; `uv run pytest tests/integration tests/crash -q` (the session fixture starts the container through the runner's Docker) |
| `frontend` | Node setup; `npm ci --prefix frontend`; `npm --prefix frontend test`; `npm --prefix frontend run build`; uv setup; `scripts/check-openapi-drift.sh` |
| `audit` | `uv run pip-audit --strict`; `npm audit --prefix frontend --omit=dev --audit-level=high` |

The workflow triggers on `push` and `pull_request` and has `permissions: contents: read`. No job uses a provider secret.

- [ ] **Step 2: Lint the workflow**

Run: `docker run --rm -v "$PWD:/repo" -w /repo rhysd/actionlint:1.7.7 -color`
Expected: no output, exit 0 (if actionlint reports a finding, fix it and re-run)

- [ ] **Step 3: Run the workflow commands locally**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest tests/unit tests/property tests/integration tests/crash -q`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci(m0): lint, unit, integration (real pgvector container, crash cases), frontend and audit jobs"
```

---

### Task 21: M0 acceptance

**Files:**
- Create: `scripts/smoke_m0.py`
- Modify: `docs/superpowers/plans/2026-10-16-job-hunter-roadmap.md` (the M0 status row)

**Interfaces:**
- Consumes: the dev stack, the CLI and the API.
- Produces: `scripts/smoke_m0.py` (exit 0 when every check passes, and it prints one JSON line per check).

- [ ] **Step 1: Write the smoke script**

`scripts/smoke_m0.py` uses `subprocess` and `httpx` against `deploy/compose.dev.yaml`:
1. `docker compose … run --rm -T api jobhunter user create --username smoke --password-stdin` with stdin `Tannenbaum-Ölfass-7\n`; expect exit 0.
2. `POST http://127.0.0.1:8080/api/auth/login` with `Origin: http://localhost:8080`; expect 204. Take the token from `Set-Cookie` and send it back as an explicit `Cookie` header (httpx does not send `Secure` cookies over plain HTTP).
3. `GET /api/system/health`; expect 200 and `ready.ok is true` (wait up to 60 s for the worker heartbeat).
4. Make a slot due in dev only: `docker compose … exec -T postgres psql -U jobhunter_bootstrap -d jobhunter -c "UPDATE schedule_state SET anchor_utc = date_trunc('minute', now()), interval_s = 3600, last_evaluated_slot = date_trunc('minute', now()) - interval '2 hours'"`.
5. Poll `GET /api/system/health` for up to 150 s until `last_run.status == "succeeded"`. Then the same psql shows `SELECT count(*) FROM runs WHERE status='missed'` = 1.
6. `POST /api/runs` twice with the same `Idempotency-Key: smoke-run-now-0001` and headers `Origin` plus `X-JH-Request: 1`; expect two identical 202 bodies.

- [ ] **Step 2: Bring up the stack and run the whole suite**

Run:

```bash
dockerd >/tmp/dockerd.log 2>&1 & sleep 3
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run pytest -q -m "not stack" && uv run pytest tests/stack -m stack -q
(cd frontend && npm test && npm run build && npm run lint && npm run typecheck) && scripts/check-openapi-drift.sh
docker compose -f deploy/compose.dev.yaml up -d --build --wait
uv run python scripts/smoke_m0.py
```

Expected: every command exits 0. The smoke script prints six `{"check": …, "ok": true}` lines.

- [ ] **Step 3: Update the roadmap status**

In `docs/superpowers/plans/2026-10-16-job-hunter-roadmap.md`, change the M0 status cell from `planned` to `done (<short git SHA>)`. Leave every other row unchanged.

- [ ] **Step 4: Commit**

```bash
git add scripts/smoke_m0.py docs/superpowers/plans/2026-10-16-job-hunter-roadmap.md
git commit -m "test(m0): acceptance smoke test; mark M0 done in the roadmap"
```

---

## Deviations

Each deviation either changes the order in which the spec arrives across milestones, or adds something the spec leaves open. None changes user-visible behaviour of the final system.

1. **Tables and FKs deferred to their owning milestone.**
   - `active_config` and `llm_tuples` are created in M2. Their NOT NULL references need the rubric and prompt-schema seeds, and `profile_versions`, all of which arrive in M2/M3.
   - `score_cycles` is created in M3.
   - The columns `runs.llm_tuple_id`, `purge_fences.source_id` and `purge_fences.manifest_id` exist in M0, but their FKs are added by the M2, M1 and M5 migrations.
   - M0 runs take their settings and scope from `schedule_state.settings_version_id`.
2. **`is_fenced()` in M0** covers the `global` and `jobs` branches. The M1 and M2 migrations `CREATE OR REPLACE` it to the full §7.14 body once `postings`, `job_revision_sources` and `llm_tuples` exist.
3. **Bootstrap settings.** M0 seeds `settings_versions` version 1 with `schema_version = 0` and only the `schedule` and `display` groups, plus a bootstrap scope version. M2 inserts the first full settings version. The model policy, execution config and evaluation policy seeds are complete spec values. The evaluation policy's `projected_critic_rate` is seeded as `0.450`, the §21 central first-round critic rate, which M3 recomputes.
4. **New table `transition_matrix`.** It backs the `assert_transition` trigger that §7.16 describes but whose DDL the spec does not give. It is synced from the Python registry in the `seed` step.
5. **`migrate` steps 1, 3 and 7** (identity advancement and manifest replay) are no-ops until M5. Step 4 grants only on the backup tables that exist.
6. **Later pieces of the takeover sweep, readiness and health.**
   - Takeover sweep steps 3–4 (targets, cycles) arrive in M3.
   - The embedder readiness check arrives in M1.
   - The `/api/system/health` fields for backups, quota, LLM spend, embedder and LangSmith arrive with their subsystems.
7. **Layout differences from §4.2.**
   - New: the `auth/` package, `worker/runs.py`, `db/migrate.py`, `db/seed.py`, `db/alembic_runner.py`, `faults.py`, `crypto.py`, `clock.py`, `ids.py` and `types.py`.
   - No `db/models/`: the code uses SQLAlchemy Core `text()` statements, and the DDL stays verbatim in the migrations.
8. **New dependencies.** `segno` (†, the terminal QR code that §16.2 requires) and `pip-audit` (†, §16.11) are not in §4.1.
9. **`JOBHUNTER_SECRETS_DIR`** (default `/run/secrets`) lets tests and dev point at another secrets directory. It is not in the §19.2 env table.
10. **`user create --password-stdin`** exists for scripted bootstrap (the smoke test). It is refused in `prod`, where §16.2's interactive `getpass` prompt stays the only path.
11. **The integration Postgres** is started by a docker-CLI pytest fixture, locally and in CI, instead of a GitHub Actions `services:` container. The init scripts, secrets and `postgresql.conf` must be mounted, and services cannot mount repository files.
12. **The dev stack uses an HTTP-only Caddy (`devproxy`)** at the trusted forwarder address `172.30.0.2`, so `api` can stay on internal-only networks in dev (V20/V21 dev parts). The production Caddy with TLS arrives in M5.
13. **Toolchain versions.** The dev container ships uv 0.11.32 and Node 22.22.0. CI pins uv 0.13.1 and Node 22.22.3 as in §4.1. Task 1 tries `uv self update 0.13.1` and records the outcome.
14. **The CI job set for M0** is `lint`, `unit`, `integration`, `frontend` and `audit`. The `contract`, `payload`, `images` and `e2e` jobs of §18.8 arrive with M1, M2 and M5.
15. **The crash matrix in M0** covers C17, C18, C27, C30 and the ownership part of C35 (the stale execution's heartbeat, owner lock and writes are fenced). Its LLM dispatch-marker part, C15 (stalled stream) and the other cases arrive with their subsystems in M1–M5.
