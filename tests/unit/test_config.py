# tests/unit/test_config.py
import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from jobhunter.config import (
    ALL_SECRETS,
    ROLE_SECRETS,
    AppConfig,
    ResticSettings,
    Role,
    StartupCheckFailed,
    load_config,
    run_startup_assertions,
)
from jobhunter.privacy.operator import FIELDS


def b64(n: int, fill: bytes = b"k") -> bytes:
    return base64.b64encode(fill * n)


def write_secrets(d: Path, **files: bytes) -> Path:
    d.mkdir(exist_ok=True)
    for name, value in files.items():
        p = d / name
        p.write_bytes(value)
        p.chmod(0o440)
    return d


BASE_ENV = {
    "JOBHUNTER_ENV": "test",
    "JOBHUNTER_DB_HOST": "h",
    "JOBHUNTER_DB_PORT": "5432",
    "JOBHUNTER_DB_NAME": "jobhunter",
    "JOBHUNTER_PUBLIC_ORIGIN": "https://testserver",
}
API_SECRETS = {"session_secret": b64(32, b"s"), "data_key": b64(32, b"d"), "db_app": b"pw"}
# M5 Task 5 (O5): a prod worker's ping URL ends in the check's uuid; a low-entropy stand-in.
HC_PING_URL = "https://hc-ping.example/00000000-0000-4000-8000-000000000001"
WORKER_SECRETS = {"data_key": b64(32), "db_app": b"pw"}


def operator_env(tmp_path: Path) -> dict[str, str]:
    """M5 follow-ups Task F1: a valid operator file, which a prod api needs (`operator_file`)."""
    path = tmp_path / "operator.json"
    path.write_text(json.dumps(dict.fromkeys(FIELDS, "x")), encoding="utf-8")
    return {"JOBHUNTER_OPERATOR_FILE": str(path)}


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


@pytest.mark.parametrize(
    ("role", "env_extra", "secrets", "expected"),
    [
        ("api", {}, {k: v for k, v in API_SECRETS.items() if k != "db_app"}, "secrets_present"),
        ("api", {}, {**API_SECRETS, "anthropic_key": b"sk-x"}, "foreign_secret_mounted"),
        ("api", {"JOBHUNTER_ENV": "staging"}, API_SECRETS, "env_valid"),
        ("api", {"LANGSMITH_GATEWAY": "1"}, API_SECRETS, "langsmith_gateway_set"),
        ("worker", {}, {**WORKER_SECRETS, "anthropic_key": b"lsv2_abc"}, "anthropic_key_is_langsmith"),
        ("worker", {"JOBHUNTER_ENV": "prod", "JOBHUNTER_FAKE_LLM": "1"}, WORKER_SECRETS, "fake_mode_in_prod"),
        ("worker", {"JOBHUNTER_ENV": "prod", "JOBHUNTER_FAULT": "x"}, WORKER_SECRETS, "fake_mode_in_prod"),
        (
            "worker",
            {
                "LANGSMITH_TRACING": "true",
                "LANGSMITH_HIDE_INPUTS": "true",
                "LANGSMITH_HIDE_OUTPUTS": "true",
                "LANGSMITH_ENDPOINT": "https://api.smith.langchain.com",
            },
            WORKER_SECRETS,
            "langsmith_config",
        ),
        (
            "worker",
            {
                "LANGSMITH_TRACING_V2": "true",
                "LANGSMITH_HIDE_INPUTS": "true",
                "LANGSMITH_HIDE_OUTPUTS": "true",
                "LANGSMITH_ENDPOINT": "https://api.smith.langchain.com",
            },
            WORKER_SECRETS,
            "langsmith_config",
        ),
        (
            "worker",
            {
                "LANGSMITH_TRACING_V2": "True",  # compared case-insensitively
                "LANGSMITH_HIDE_INPUTS": "true",
                "LANGSMITH_ENDPOINT": "https://eu.api.smith.langchain.com",
            },
            WORKER_SECRETS,
            "langsmith_config",  # HIDE_OUTPUTS is missing
        ),
        (
            "worker",
            {"LANGCHAIN_TRACING_V2": "true", "LANGSMITH_ENDPOINT": "https://eu.api.smith.langchain.com"},
            WORKER_SECRETS,
            "langsmith_config",  # no HIDE settings at all
        ),
        (
            "worker",
            {
                "LANGCHAIN_TRACING": "TRUE",
                "LANGSMITH_HIDE_INPUTS": "true",
                "LANGSMITH_ENDPOINT": "https://eu.api.smith.langchain.com",
            },
            WORKER_SECRETS,
            "langsmith_config",  # HIDE_OUTPUTS is missing
        ),
        (
            "worker",
            {
                "LANGCHAIN_TRACING_V2": "true",
                "LANGSMITH_HIDE_INPUTS": "true",
                "LANGSMITH_HIDE_OUTPUTS": "true",
                "LANGSMITH_ENDPOINT": "https://eu.api.smith.langchain.com",
                "LANGCHAIN_ENDPOINT": "https://api.smith.langchain.com",  # the SDK falls back to it
            },
            WORKER_SECRETS,
            "langsmith_config",
        ),
        ("api", {}, {**API_SECRETS, "session_secret": b64(31, b"s")}, "session_secret_length"),
        ("api", {}, {**API_SECRETS, "data_key": b64(33, b"d")}, "data_key_length"),
        ("api", {}, {**API_SECRETS, "data_key_previous": b64(16)}, "data_key_previous_length"),
        ("api", {}, {**API_SECRETS, "data_key": b64(32, b"s")}, "keys_identical"),
        ("worker", {"JOBHUNTER_ENV": "prod"}, WORKER_SECRETS, "healthchecks_in_prod"),
        (
            "api",
            {"JOBHUNTER_ENV": "prod", "ANTHROPIC_BASE_URL": "http://proxy"},
            API_SECRETS,
            "anthropic_env_in_prod",
        ),
    ],
)
def test_assertion_failures(
    tmp_path: Path, role: Role, env_extra: dict[str, str], secrets: dict[str, bytes], expected: str
) -> None:
    assert check_of(role, {**BASE_ENV, **env_extra}, write_secrets(tmp_path / "s", **secrets)) == expected


def test_world_readable_secret_fails(tmp_path: Path) -> None:
    d = write_secrets(tmp_path / "s", **API_SECRETS)
    (d / "db_app").chmod(0o444)
    assert check_of("api", BASE_ENV, d) == "secrets_not_world_readable"


def test_unreadable_secret_fails_loudly(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root reads every file")
    d = write_secrets(tmp_path / "s", **API_SECRETS)
    (d / "db_app").chmod(0o000)
    assert check_of("api", BASE_ENV, d) == "secrets_readable"


def test_cli_exits_78_and_never_logs_the_value(tmp_path: Path) -> None:
    secret = b64(20, b"Z")  # too short → session_secret_length
    d = write_secrets(tmp_path / "s", **{**API_SECRETS, "session_secret": secret})
    env = {**os.environ, **BASE_ENV, "JOBHUNTER_SECRETS_DIR": str(d)}
    r = subprocess.run(
        [sys.executable, "-m", "jobhunter.cli", "startup-check", "--role", "api"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 78 and "session_secret_length" in r.stdout
    assert secret.decode() not in r.stdout + r.stderr


def test_cli_logs_the_failed_check_as_one_critical_json_event(tmp_path: Path) -> None:
    d = write_secrets(tmp_path / "s", **{**API_SECRETS, "session_secret": b64(20, b"Z")})
    env = {**os.environ, **BASE_ENV, "JOBHUNTER_SECRETS_DIR": str(d)}
    r = subprocess.run(
        [sys.executable, "-m", "jobhunter.cli", "startup-check", "--role", "api"],
        env=env,
        capture_output=True,
        text=True,
    )
    (line,) = r.stdout.splitlines()
    event = json.loads(line)
    assert event["level"] == "critical" and event["event"] == "startup_check_failed"
    assert event["check"] == "session_secret_length" and event["role"] == "api"
    assert event["logger"] == "jobhunter.startup" and "ts" in event and "detail" in event


def test_startup_or_exit_registers_the_loaded_secret_values_for_masking(tmp_path: Path, monkeypatch) -> None:
    from jobhunter.config import startup_or_exit
    from jobhunter.observability.redact import mask_text

    adzuna_key = "0123456789abcdef0123456789abcdef"
    ping_url = "https://hc-ping.com/5f3e2a1b-0000-7000-8000-c4d3e2f1a0b9"
    d = write_secrets(
        tmp_path / "s",
        **WORKER_SECRETS,
        adzuna_app_key=adzuna_key.encode(),
        healthchecks_ping_url=ping_url.encode(),
    )
    for name, value in {**BASE_ENV, "JOBHUNTER_SECRETS_DIR": str(d)}.items():
        monkeypatch.setenv(name, value)

    startup_or_exit("worker")

    assert mask_text(f"key {adzuna_key}") == "key [SECRET]"
    assert mask_text(f"ping {ping_url}") == "ping [SECRET]"


def test_all_secrets_is_the_import_free_secret_name_catalogue() -> None:
    from jobhunter import secret_names

    assert ALL_SECRETS is secret_names.SECRET_NAMES and len(ALL_SECRETS) == 20


# --- Additional coverage of behaviour the task brief specifies in prose -----------------------------------


def test_all_secrets_are_exactly_the_names_listed_in_spec_19_2() -> None:
    assert ALL_SECRETS == frozenset(
        {
            "session_secret",
            "data_key",
            "data_key_previous",
            "db_app",
            "db_migrator",
            "db_backup",
            "db_restore",
            "pg_bootstrap",
            "anthropic_key",
            "rapidapi_key",
            "serpapi_key",
            "adzuna_app_id",
            "adzuna_app_key",
            "langsmith_key",
            "ntfy_token",
            "healthchecks_ping_url",
            "healthchecks_api_key",
            "restic_password",
            "s3_access_key_id",
            "s3_secret_access_key",
        }
    )


def test_role_secrets_table() -> None:
    api, worker, migrate = ROLE_SECRETS["api"], ROLE_SECRETS["worker"], ROLE_SECRETS["migrate"]
    assert api.required == {"session_secret", "data_key", "db_app"}
    assert api.optional == {"data_key_previous"}
    assert worker.required == {"data_key", "db_app"}
    assert worker.optional == {
        "data_key_previous",
        "anthropic_key",
        "rapidapi_key",
        "serpapi_key",
        "adzuna_app_id",
        "adzuna_app_key",
        "langsmith_key",
        "ntfy_token",
        "healthchecks_ping_url",
        "healthchecks_api_key",
    }
    assert migrate.required == {"db_migrator"}
    assert migrate.optional == frozenset()
    for rs in ROLE_SECRETS.values():
        assert rs.required | rs.optional <= ALL_SECRETS
        assert not rs.required & rs.optional


def test_secret_files_lose_exactly_one_trailing_newline(tmp_path: Path) -> None:
    d = write_secrets(tmp_path / "s", **{**API_SECRETS, "db_app": b"pw\n\n"})
    cfg = load_config("api", BASE_ENV, d)
    assert cfg.secret("db_app") == b"pw\n"
    assert cfg.secret_b64("data_key") == b"d" * 32


def test_files_outside_the_known_secret_names_are_ignored(tmp_path: Path) -> None:
    d = write_secrets(tmp_path / "s", **API_SECRETS, README=b"not a secret")
    cfg = load_config("api", BASE_ENV, d)
    assert set(cfg.secrets) == set(API_SECRETS)
    run_startup_assertions(cfg)


def test_a_secret_that_cannot_be_read_is_recorded_not_skipped(tmp_path: Path) -> None:
    d = write_secrets(tmp_path / "s", **{k: v for k, v in API_SECRETS.items() if k != "db_app"})
    (d / "db_app").mkdir()  # exists but read_bytes() fails, also when the tests run as root
    cfg = load_config("api", BASE_ENV, d)
    assert cfg.unreadable_secrets == {"db_app"} and "db_app" not in cfg.secrets
    assert check_of("api", BASE_ENV, d) == "secrets_readable"


def test_an_empty_required_secret_counts_as_missing(tmp_path: Path) -> None:
    assert check_of("api", BASE_ENV, write_secrets(tmp_path / "s", **{**API_SECRETS, "db_app": b"\n"})) == (
        "secrets_present"
    )


@pytest.mark.parametrize("content", [b"", b"\n"])
def test_an_empty_optional_secret_reads_as_absent(tmp_path: Path, content: bytes) -> None:
    """Task 23: compose needs a mounted secret's file to exist, so the dev stack mounts an empty
    `anthropic_key` when no key was given; the worker then has no key, and passes its startup assertions."""
    d = write_secrets(tmp_path / "s", **WORKER_SECRETS, anthropic_key=content, rapidapi_key=content)
    cfg = load_config("worker", BASE_ENV, d)
    assert "anthropic_key" not in cfg.secrets and "rapidapi_key" not in cfg.secrets
    assert set(cfg.secrets) == set(WORKER_SECRETS)
    run_startup_assertions(cfg)
    # the file is still mounted: its mode is checked as every mounted secret's is
    (d / "anthropic_key").chmod(0o444)
    assert check_of("worker", BASE_ENV, d) == "secrets_not_world_readable"


def test_an_empty_secret_of_another_role_is_still_foreign(tmp_path: Path) -> None:
    d = write_secrets(tmp_path / "s", **API_SECRETS, anthropic_key=b"")
    assert check_of("api", BASE_ENV, d) == "foreign_secret_mounted"


@pytest.mark.parametrize(
    "tracing_var", ["LANGSMITH_TRACING", "LANGSMITH_TRACING_V2", "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING"]
)
@pytest.mark.parametrize("tracing_value", ["true", "TRUE"])
def test_tracing_with_all_langsmith_requirements_met_passes(
    tmp_path: Path, tracing_var: str, tracing_value: str
) -> None:
    env = {
        **BASE_ENV,
        tracing_var: tracing_value,
        "LANGSMITH_HIDE_INPUTS": "true",
        "LANGSMITH_HIDE_OUTPUTS": "true",
        "LANGSMITH_ENDPOINT": "https://eu.api.smith.langchain.com",
    }
    secrets = {**WORKER_SECRETS, "langsmith_key": LANGSMITH_KEY}
    run_startup_assertions(load_config("worker", env, write_secrets(tmp_path / "s", **secrets)))
    # a legacy endpoint alias that is the EU endpoint too is fine
    env["LANGCHAIN_ENDPOINT"] = "https://eu.api.smith.langchain.com"
    run_startup_assertions(load_config("worker", env, write_secrets(tmp_path / "t", **secrets)))


# --- M5 Task 6 (O15, Q15): tracing needs its key from the secret file, one endpoint and no sampling --------

TRACING_ENV = {
    "LANGSMITH_TRACING": "true",
    "LANGSMITH_HIDE_INPUTS": "true",
    "LANGSMITH_HIDE_OUTPUTS": "true",
    "LANGSMITH_ENDPOINT": "https://eu.api.smith.langchain.com",
}
LANGSMITH_KEY = b"lsv2-test-key"  # a low-entropy stand-in: no secret scanner reads it as a key


def langsmith_failure(
    tmp_path: Path, env_extra: dict[str, str], secrets: dict[str, bytes]
) -> StartupCheckFailed:
    env = {**BASE_ENV, **TRACING_ENV, **env_extra}
    tmp_path.mkdir(parents=True, exist_ok=True)
    with pytest.raises(StartupCheckFailed) as exc:
        run_startup_assertions(load_config("worker", env, write_secrets(tmp_path / "s", **secrets)))
    return exc.value


@pytest.mark.parametrize("key", [None, b"", b"\n"])
def test_tracing_without_langsmith_key_is_refused(tmp_path: Path, key: bytes | None) -> None:
    secrets = WORKER_SECRETS if key is None else {**WORKER_SECRETS, "langsmith_key": key}
    failure = langsmith_failure(tmp_path, {}, secrets)
    assert (failure.check, failure.detail) == ("langsmith_config", "tracing needs the langsmith_key secret")
    # without tracing a worker needs no key (it is optional)
    run_startup_assertions(load_config("worker", BASE_ENV, write_secrets(tmp_path / "t", **WORKER_SECRETS)))


@pytest.mark.parametrize("name", ["LANGSMITH_RUNS_ENDPOINTS", "LANGCHAIN_RUNS_ENDPOINTS"])
@pytest.mark.parametrize("value", ['[{"api_url": "https://replica.example", "api_key": "x"}]', ""])
def test_runs_endpoints_are_refused(tmp_path: Path, name: str, value: str) -> None:
    """A write replica is a second destination the endpoint check never sees: set at all, it is refused, and
    the detail names the variable, never its value."""
    secrets = {**WORKER_SECRETS, "langsmith_key": LANGSMITH_KEY}
    failure = langsmith_failure(tmp_path, {name: value}, secrets)
    assert (failure.check, failure.detail) == ("langsmith_config", f"tracing needs {name} unset")
    assert "replica" not in str(failure)


@pytest.mark.parametrize("name", ["LANGSMITH_API_KEY", "LANGCHAIN_API_KEY"])
def test_api_key_variables_are_refused(tmp_path: Path, name: str) -> None:
    """The key comes from the `langsmith_key` secret file only, never from the environment."""
    secrets = {**WORKER_SECRETS, "langsmith_key": LANGSMITH_KEY}
    failure = langsmith_failure(tmp_path, {name: "lsv2-env-key"}, secrets)
    assert (failure.check, failure.detail) == ("langsmith_config", f"tracing needs {name} unset")
    assert "lsv2-env-key" not in str(failure)
    both = langsmith_failure(tmp_path / "both", {"LANGSMITH_API_KEY": "a", "LANGCHAIN_API_KEY": "b"}, secrets)
    assert both.detail == "tracing needs LANGCHAIN_API_KEY, LANGSMITH_API_KEY unset"


@pytest.mark.parametrize("name", ["LANGSMITH_TRACING_SAMPLING_RATE", "LANGCHAIN_TRACING_SAMPLING_RATE"])
@pytest.mark.parametrize("rate", ["0.25", "1.0"])
def test_a_sampling_rate_is_refused(tmp_path: Path, name: str, rate: str) -> None:
    """No sampling (Q15): every trace is sent, so no rate may be set, not even 1.0."""
    secrets = {**WORKER_SECRETS, "langsmith_key": LANGSMITH_KEY}
    failure = langsmith_failure(tmp_path, {name: rate}, secrets)
    assert (failure.check, failure.detail) == ("langsmith_config", f"tracing needs {name} unset")


def test_the_widened_langsmith_check_keeps_its_place_in_the_order(tmp_path: Path) -> None:
    """The hide flags and the endpoint are checked first, then the key; and `langsmith_config` still comes
    before the key material's checks."""
    wrong_endpoint = {"LANGSMITH_ENDPOINT": "https://api.smith.langchain.com", "LANGSMITH_API_KEY": "x"}
    failure = langsmith_failure(tmp_path, wrong_endpoint, WORKER_SECRETS)
    assert (failure.check, failure.detail) == (
        "langsmith_config",
        "tracing needs a correct LANGSMITH_ENDPOINT",
    )
    short_key = {**WORKER_SECRETS, "data_key": b64(16)}
    assert langsmith_failure(tmp_path / "t", {}, short_key).check == "langsmith_config"


EXIT_VARS = ("LANGSMITH_USE_DAEMON", "LANGSMITH_DISABLE_RUN_COMPRESSION")


@pytest.mark.parametrize(
    ("values", "refused"),
    [
        ({}, ["LANGSMITH_USE_DAEMON", "LANGSMITH_DISABLE_RUN_COMPRESSION"]),
        ({"LANGSMITH_USE_DAEMON": "true"}, ["LANGSMITH_DISABLE_RUN_COMPRESSION"]),
        ({"LANGSMITH_DISABLE_RUN_COMPRESSION": "true"}, ["LANGSMITH_USE_DAEMON"]),
        ({"LANGSMITH_USE_DAEMON": "True", "LANGSMITH_DISABLE_RUN_COMPRESSION": "1"}, list(EXIT_VARS)),
        ({"LANGSMITH_USE_DAEMON": "", "LANGSMITH_DISABLE_RUN_COMPRESSION": "true"}, ["LANGSMITH_USE_DAEMON"]),
    ],
)
def test_a_prod_worker_that_traces_needs_daemon_threads_and_no_compression(
    tmp_path: Path, values: dict[str, str], refused: list[str]
) -> None:
    """Ruling T21-3 I3: the SDK's threads are daemons and its compression's pool is unused, so an unreachable
    LangSmith never holds a prod worker's exit. `true` exactly: the SDK compares `USE_DAEMON` with it."""
    env = {**BASE_ENV, **TRACING_ENV, "JOBHUNTER_ENV": "prod", "NTFY_TOPIC_URL": TOPIC_URL, **values}
    secrets = {**PROD_WORKER_SECRETS, "langsmith_key": LANGSMITH_KEY}
    with pytest.raises(StartupCheckFailed) as exc:
        run_startup_assertions(load_config("worker", env, write_secrets(tmp_path / "s", **secrets)))
    assert (exc.value.check, exc.value.detail) == (
        "langsmith_config",
        f"tracing in prod needs {', '.join(refused)} true",
    )
    run_startup_assertions(
        load_config(
            "worker", {**env, **dict.fromkeys(EXIT_VARS, "true")}, write_secrets(tmp_path / "t", **secrets)
        )
    )


def test_the_exit_settings_bind_a_prod_worker_that_traces_only(tmp_path: Path) -> None:
    """Without tracing, or outside prod, neither variable is needed (no other role may trace: the api, for
    one, is never given the key)."""
    prod = {**BASE_ENV, "JOBHUNTER_ENV": "prod", "NTFY_TOPIC_URL": TOPIC_URL}
    run_startup_assertions(load_config("worker", prod, write_secrets(tmp_path / "a", **PROD_WORKER_SECRETS)))
    secrets = {**WORKER_SECRETS, "langsmith_key": LANGSMITH_KEY}
    for env_name in ("dev", "test"):
        env = {**BASE_ENV, **TRACING_ENV, "JOBHUNTER_ENV": env_name}
        run_startup_assertions(load_config("worker", env, write_secrets(tmp_path / env_name, **secrets)))


def test_a_prod_worker_with_both_healthchecks_secrets_passes(tmp_path: Path) -> None:
    hc = {"healthchecks_ping_url": HC_PING_URL.encode(), "healthchecks_api_key": b"k"}
    env = {**BASE_ENV, "JOBHUNTER_ENV": "prod", "NTFY_TOPIC_URL": "https://ntfy.example/jh-topic"}
    cfg = load_config("worker", env, write_secrets(tmp_path / "s", **WORKER_SECRETS, **hc))
    run_startup_assertions(cfg)


def test_missing_secrets_dir_fails_secrets_present(tmp_path: Path) -> None:
    assert check_of("api", BASE_ENV, tmp_path / "does-not-exist") == "secrets_present"


def test_environ_keeps_only_the_known_prefixes(tmp_path: Path) -> None:
    env = {
        **BASE_ENV,
        "LANGSMITH_PROJECT": "p",
        "LANGCHAIN_TRACING_V2": "false",
        "ANTHROPIC_BASE_URL": "u",
        "PATH": "/bin",
        "HOME": "/root",
    }
    cfg = load_config("api", env, write_secrets(tmp_path / "s", **API_SECRETS))
    assert set(cfg.environ) == set(BASE_ENV) | {
        "LANGSMITH_PROJECT",
        "LANGCHAIN_TRACING_V2",
        "ANTHROPIC_BASE_URL",
    }


def test_config_fields(tmp_path: Path) -> None:
    d = write_secrets(tmp_path / "s", **API_SECRETS)
    cfg = load_config("api", {**BASE_ENV, "JOBHUNTER_LOG_LEVEL": "debug", "JOBHUNTER_FAULT": "p.x"}, d)
    assert (cfg.role, cfg.env, cfg.secrets_dir) == ("api", "test", d)
    assert (cfg.db.host, cfg.db.port, cfg.db.name) == ("h", 5432, "jobhunter")
    assert (cfg.public_origin, cfg.log_level, cfg.fault) == ("https://testserver", "DEBUG", "p.x")
    bare = load_config("api", {"JOBHUNTER_ENV": "dev", "JOBHUNTER_DB_NAME": "x"}, d)
    assert (bare.public_origin, bare.log_level, bare.fault) == (None, "INFO", None)


def test_secrets_dir_comes_from_the_environment_when_not_passed(tmp_path: Path) -> None:
    d = write_secrets(tmp_path / "s", **API_SECRETS)
    cfg = load_config("api", {**BASE_ENV, "JOBHUNTER_SECRETS_DIR": str(d)})
    assert cfg.secrets_dir == d
    run_startup_assertions(cfg)


@pytest.mark.parametrize(
    "env",
    [
        {"JOBHUNTER_ENV": "test"},  # the database name has no default
        {"JOBHUNTER_ENV": "test", "JOBHUNTER_DB_NAME": ""},
        {"JOBHUNTER_DB_NAME": "jobhunter"},  # the environment name has no default
        {**BASE_ENV, "JOBHUNTER_DB_PORT": "five"},
    ],
)
def test_bad_environment_fails_env_valid(tmp_path: Path, env: dict[str, str]) -> None:
    with pytest.raises(StartupCheckFailed) as exc:
        load_config("api", env, write_secrets(tmp_path / "s", **API_SECRETS))
    assert exc.value.check == "env_valid"


def test_failures_name_the_file_but_never_its_content(tmp_path: Path) -> None:
    d = write_secrets(tmp_path / "s", **{**API_SECRETS, "db_app": b"hunter2-pw"})
    (d / "db_app").chmod(0o444)
    with pytest.raises(StartupCheckFailed) as exc:
        run_startup_assertions(load_config("api", BASE_ENV, d))
    assert "db_app" in str(exc.value)
    assert "hunter2-pw" not in str(exc.value) and "hunter2-pw" not in repr(exc.value)


def test_repr_of_the_config_never_shows_secret_values(tmp_path: Path) -> None:
    d = write_secrets(tmp_path / "s", **{**API_SECRETS, "db_app": b"hunter2-pw"})
    cfg = load_config("api", {**BASE_ENV, "ANTHROPIC_API_KEY": "sk-leak"}, d)
    assert "hunter2-pw" not in repr(cfg) and "sk-leak" not in repr(cfg)


def test_cli_exits_0_for_a_valid_configuration(tmp_path: Path) -> None:
    d = write_secrets(tmp_path / "s", db_migrator=b"pw")
    env = {**os.environ, **BASE_ENV, "JOBHUNTER_SECRETS_DIR": str(d)}
    r = subprocess.run(
        [sys.executable, "-m", "jobhunter.cli", "startup-check", "--role", "migrate"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_cli_main_rejects_an_unknown_role() -> None:
    from jobhunter.cli import main

    with pytest.raises(SystemExit) as exc:
        main(["startup-check", "--role", "embedder"])
    assert exc.value.code == 2


# --- Ruling T13-a: the log level is a startup assertion ----------------------------------------------------


@pytest.mark.parametrize("level", ["verbose", "TRACE", "20", "info "])
def test_an_unknown_log_level_fails_startup(tmp_path: Path, level: str) -> None:
    d = write_secrets(tmp_path / "s", **WORKER_SECRETS)
    with pytest.raises(StartupCheckFailed) as exc:
        run_startup_assertions(load_config("worker", {**BASE_ENV, "JOBHUNTER_LOG_LEVEL": level}, d))
    assert exc.value.check == "log_level"
    assert exc.value.detail == "JOBHUNTER_LOG_LEVEL must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL"


@pytest.mark.parametrize("level", ["debug", "INFO", "Warning", "ERROR", "critical"])
def test_every_known_log_level_passes_in_any_case(tmp_path: Path, level: str) -> None:
    d = write_secrets(tmp_path / "s", **WORKER_SECRETS)
    cfg = load_config("worker", {**BASE_ENV, "JOBHUNTER_LOG_LEVEL": level}, d)
    run_startup_assertions(cfg)
    assert cfg.log_level == level.upper()


def test_cli_exits_78_for_an_unknown_log_level(tmp_path: Path) -> None:
    d = write_secrets(tmp_path / "s", **WORKER_SECRETS)
    env = {**os.environ, **BASE_ENV, "JOBHUNTER_SECRETS_DIR": str(d), "JOBHUNTER_LOG_LEVEL": "verbose"}
    r = subprocess.run(
        [sys.executable, "-m", "jobhunter.cli", "startup-check", "--role", "worker"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 78
    (line,) = r.stdout.splitlines()
    assert json.loads(line)["check"] == "log_level"


# --- Ruling T18-c: the public origin is exactly scheme://host[:port] --------------------------------------

ORIGIN_SHAPE = "JOBHUNTER_PUBLIC_ORIGIN must be exactly scheme://host[:port], as a browser sends it in Origin"


def origin_failure(tmp_path: Path, env_name: str, origin: str) -> StartupCheckFailed:
    d = write_secrets(tmp_path / "s", **API_SECRETS)
    env = {**BASE_ENV, "JOBHUNTER_ENV": env_name, "JOBHUNTER_PUBLIC_ORIGIN": origin}
    with pytest.raises(StartupCheckFailed) as exc:
        run_startup_assertions(load_config("api", env, d))
    return exc.value


@pytest.mark.parametrize(
    ("env_name", "origin"),
    [
        ("dev", "http://localhost:8080"),  # the one canonical dev origin
        ("test", "https://testserver"),
        ("prod", "https://jobs.example.org"),
        ("prod", "https://jobs.example.org:8443"),
        ("dev", "http://127.0.0.1:8080"),
        ("dev", "http://[::1]:8080"),
        ("dev", "http://[2001:db8::1]:8080"),
        ("dev", "http://dev-box.example.org.:8080"),  # a trailing dot stays in the serialised host
    ],
)
def test_a_public_origin_of_scheme_host_and_port_passes(tmp_path: Path, env_name: str, origin: str) -> None:
    d = write_secrets(tmp_path / "s", **API_SECRETS)
    env = {**BASE_ENV, "JOBHUNTER_ENV": env_name, "JOBHUNTER_PUBLIC_ORIGIN": origin, **operator_env(tmp_path)}
    cfg = load_config("api", env, d)
    run_startup_assertions(cfg)
    assert cfg.public_origin == origin


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost:8080/",  # trailing slash
        "http://localhost:8080/app",  # path
        "http://localhost:8080?next=1",  # query
        "http://localhost:8080?",  # empty query
        "http://localhost:8080#top",  # fragment
        "http://localhost:8080#",  # empty fragment
        "http://user@localhost:8080",  # userinfo
        "http://user:pw@localhost:8080",
        "ftp://localhost:8080",  # neither http nor https
        "localhost:8080",  # no scheme
        "//localhost:8080",
        "http://",  # no host
        "http://:8080",
        "http://localhost:",  # empty port
        "http://localhost:0",
        "http://localhost:65536",
        "http://localhost:80a",
        "http://localhost:08080",
        "HTTP://localhost:8080",  # browsers send scheme and host lower-cased
        "http://LocalHost:8080",
        "http://localhost:80",  # browsers leave out the default port
        "https://jobs.example.org:443",
        "https://bücher.example",  # browsers send the punycode form
        " http://localhost:8080",
        "http://localhost:8080\n",
        "http://local host:8080",
        # Fix round 1, item 4: the host is [a-z0-9.-]+ or a bracketed IPv6 address without a zone, both in
        # the form a browser serialises.
        "http://local_host:8080",  # no underscore in a host a browser sends
        "http://a_b:8080",
        "http://local\\host:8080",  # a backslash inside the host
        "http://localhost%2f:8080",  # a percent-escape (here of "/") inside the host
        "http://%6cocalhost:8080",  # browsers decode percent-escapes in the host
        "http://localhost\\:8080",  # a backslash is a path separator to a browser
        "http://[fe80::1%25eth0]:8080",  # a zone id never appears in Origin
        "http://[fe80::1%eth0]:8080",
        "http://a!b:8080",
        "http://exa$mple.org",
        "http://*.example.org",
        "http://a,b.example.org",
        "http://a~b.example.org",
        "http://[0:0::1]:8080",  # browsers send the compressed form, [::1]
        "http://[::FFFF:1.2.3.4]:8080",
        "http://[::ffff:1.2.3.4]:8080",  # IPv4-mapped: configure the IPv4 address instead
        "http://[::ffff:102:304]:8080",
        "http://[127.0.0.1]:8080",  # IPv4 in brackets
        "http://127.1:8080",  # browsers send 127.0.0.1
        "http://0x7f.0.0.1:8080",
        "http://010.0.0.1:8080",  # octal to a browser: 8.0.0.1
        "http://256.0.0.1:8080",  # not a valid IPv4 address
        "http://1.2.3.4.5:8080",
    ],
)
def test_a_public_origin_that_is_not_exactly_scheme_host_and_port_fails(tmp_path: Path, origin: str) -> None:
    failure = origin_failure(tmp_path, "dev", origin)
    assert (failure.check, failure.detail) == ("public_origin", ORIGIN_SHAPE)


@pytest.mark.parametrize("origin", ["http://jobs.example.org", "http://localhost:8080"])
def test_prod_requires_an_https_public_origin(tmp_path: Path, origin: str) -> None:
    failure = origin_failure(tmp_path, "prod", origin)
    assert (failure.check, failure.detail) == (
        "public_origin",
        "JOBHUNTER_PUBLIC_ORIGIN must use https in prod",
    )


# --- Fix round 1, item 0 (ruling T18-i): the api and the worker need a public origin ---------------------


@pytest.mark.parametrize(("role", "secrets"), [("api", API_SECRETS), ("worker", WORKER_SECRETS)])
@pytest.mark.parametrize("origin", [None, ""])  # an empty variable counts as missing
def test_the_api_and_the_worker_need_a_public_origin(
    tmp_path: Path, role: Role, secrets: dict[str, bytes], origin: str | None
) -> None:
    env = {name: value for name, value in BASE_ENV.items() if name != "JOBHUNTER_PUBLIC_ORIGIN"}
    if origin is not None:
        env["JOBHUNTER_PUBLIC_ORIGIN"] = origin
    with pytest.raises(StartupCheckFailed) as exc:
        run_startup_assertions(load_config(role, env, write_secrets(tmp_path / "s", **secrets)))
    assert (exc.value.check, exc.value.detail) == ("public_origin", "JOBHUNTER_PUBLIC_ORIGIN is required")


def test_migrate_needs_no_public_origin(tmp_path: Path) -> None:
    env = {name: value for name, value in BASE_ENV.items() if name != "JOBHUNTER_PUBLIC_ORIGIN"}
    run_startup_assertions(load_config("migrate", env, write_secrets(tmp_path / "s", db_migrator=b"pw")))


def test_the_cli_exits_78_for_a_public_origin_with_a_path(tmp_path: Path) -> None:
    d = write_secrets(tmp_path / "s", **WORKER_SECRETS)
    env = {
        **os.environ,
        **BASE_ENV,
        "JOBHUNTER_SECRETS_DIR": str(d),
        "JOBHUNTER_PUBLIC_ORIGIN": "https://testserver/",
    }
    r = subprocess.run(
        [sys.executable, "-m", "jobhunter.cli", "startup-check", "--role", "worker"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 78
    (line,) = r.stdout.splitlines()
    assert json.loads(line)["check"] == "public_origin"


# --- M1 Task 5: fake sources, the fixtures directory, the embedder URL and the User-Agent ------------------


@pytest.mark.parametrize("value", ["1", "yes", ""])  # set to anything, the empty string included
def test_fake_sources_is_set_by_the_variable_being_present(tmp_path: Path, value: str) -> None:
    env = {**BASE_ENV, "JOBHUNTER_FAKE_SOURCES": value}
    assert load_config("worker", env, write_secrets(tmp_path / "s", **WORKER_SECRETS)).fake_sources is True


def test_m1_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = load_config("worker", BASE_ENV, write_secrets(tmp_path / "s", **WORKER_SECRETS))
    assert cfg.fake_sources is False
    assert (
        cfg.fixtures_dir == (tmp_path / "tests/contract/fixtures").resolve()
        and cfg.fixtures_dir.is_absolute()
    )
    assert cfg.embedder_url == "http://embedder:8080"
    assert cfg.http_user_agent == "JobHunter/1.0 (personal job search)"


def test_m1_settings_from_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    env = {
        **BASE_ENV,
        "JOBHUNTER_FIXTURES_DIR": "fx",
        "JOBHUNTER_EMBEDDER_URL": "http://127.0.0.1:9000",
        "JOBHUNTER_HTTP_USER_AGENT": "JobHunter/test",
    }
    cfg = load_config("worker", env, write_secrets(tmp_path / "s", **WORKER_SECRETS))
    assert (cfg.fixtures_dir, cfg.embedder_url, cfg.http_user_agent) == (
        (tmp_path / "fx").resolve(),
        "http://127.0.0.1:9000",
        "JobHunter/test",
    )


def test_fake_sources_are_still_refused_in_prod(tmp_path: Path) -> None:
    env = {**BASE_ENV, "JOBHUNTER_ENV": "prod", "JOBHUNTER_FAKE_SOURCES": "1"}
    with pytest.raises(StartupCheckFailed) as exc:
        run_startup_assertions(load_config("worker", env, write_secrets(tmp_path / "s", **WORKER_SECRETS)))
    assert (exc.value.check, exc.value.detail) == (
        "fake_mode_in_prod",
        "JOBHUNTER_FAKE_SOURCES must not be set in prod",
    )


# --- M2 Task 4: the recorded-SSE fake LLM (`JOBHUNTER_FAKE_LLM`, `JOBHUNTER_FAKE_LLM_DIR`) ------------------


@pytest.mark.parametrize("value", ["1", "yes", ""])  # set to anything, the empty string included
def test_fake_llm_is_set_by_the_variable_being_present(tmp_path: Path, value: str) -> None:
    env = {**BASE_ENV, "JOBHUNTER_FAKE_LLM": value}
    assert load_config("worker", env, write_secrets(tmp_path / "s", **WORKER_SECRETS)).fake_llm is True


def test_fake_llm_defaults_to_off_and_the_recorded_anthropic_fixtures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    cfg = load_config("worker", BASE_ENV, write_secrets(tmp_path / "s", **WORKER_SECRETS))
    assert cfg.fake_llm is False
    assert cfg.fake_llm_dir == (tmp_path / "tests/contract/fixtures/anthropic").resolve()
    assert cfg.fake_llm_dir.is_absolute()


def test_fake_llm_dir_from_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    env = {**BASE_ENV, "JOBHUNTER_FAKE_LLM": "1", "JOBHUNTER_FAKE_LLM_DIR": "llm-fx"}
    cfg = load_config("worker", env, write_secrets(tmp_path / "s", **WORKER_SECRETS))
    assert (cfg.fake_llm, cfg.fake_llm_dir) == (True, (tmp_path / "llm-fx").resolve())


# --- M2 Task 9: the encrypted CV files' volume (`JOBHUNTER_CV_STORE_DIR`) ---------------------------------


def test_cv_store_dir_defaults_to_the_volume_and_follows_the_environment(tmp_path: Path) -> None:
    secrets = write_secrets(tmp_path / "s", **API_SECRETS)
    assert load_config("api", BASE_ENV, secrets).cv_store_dir == Path("/data/cv")
    env = {**BASE_ENV, "JOBHUNTER_CV_STORE_DIR": str(tmp_path / "cv")}
    assert load_config("api", env, secrets).cv_store_dir == tmp_path / "cv"


# --- M2 Task 11: the docparse jobs' tmpfs (`JOBHUNTER_PARSE_JOBS_DIR`) -------------------------------------


def test_parse_jobs_dir_defaults_to_the_tmpfs_and_follows_the_environment(tmp_path: Path) -> None:
    secrets = write_secrets(tmp_path / "s", **WORKER_SECRETS)
    assert load_config("worker", BASE_ENV, secrets).parse_jobs_dir == Path("/data/parse")
    env = {**BASE_ENV, "JOBHUNTER_PARSE_JOBS_DIR": str(tmp_path / "parse")}
    assert load_config("worker", env, secrets).parse_jobs_dir == tmp_path / "parse"


# --- M5 Task 9: the deletion manifests' and the exports' volumes (R18) -------------------------------------


def test_manifests_and_exports_dirs_default_to_the_volumes_and_follow_the_environment(tmp_path: Path) -> None:
    secrets = write_secrets(tmp_path / "s", **WORKER_SECRETS)
    cfg = load_config("worker", BASE_ENV, secrets)
    assert (cfg.manifests_dir, cfg.exports_dir) == (Path("/data/manifests"), Path("/data/exports"))
    env = {
        **BASE_ENV,
        "JOBHUNTER_MANIFESTS_DIR": str(tmp_path / "manifests"),
        "JOBHUNTER_EXPORTS_DIR": str(tmp_path / "exports"),
    }
    cfg = load_config("worker", env, secrets)
    assert (cfg.manifests_dir, cfg.exports_dir) == (tmp_path / "manifests", tmp_path / "exports")


# --- M5 Task 10: the backup container's staging directory and restic repository ----------------------------

BACKUP_SECRETS = {"db_backup": b"b", "db_restore": b"r", "restic_password": b"p"}


def test_staging_dir_and_restic_default_to_the_volume_and_no_repository(tmp_path: Path) -> None:
    cfg = load_config("backup", BASE_ENV, write_secrets(tmp_path / "s", **BACKUP_SECRETS))
    assert (cfg.staging_dir, cfg.restic) == (Path("/data/staging"), None)
    # An empty repository variable is no repository either.
    empty = load_config("backup", {**BASE_ENV, "RESTIC_REPOSITORY": ""}, tmp_path / "s")
    assert empty.restic is None


def test_restic_settings_follow_the_environment(tmp_path: Path) -> None:
    secrets = write_secrets(tmp_path / "s", **BACKUP_SECRETS)
    env = {
        **BASE_ENV,
        "JOBHUNTER_STAGING_DIR": str(tmp_path / "staging"),
        "RESTIC_REPOSITORY": "s3:https://s3.example.test/bucket",
        "AWS_DEFAULT_REGION": "eu-central-1",
    }
    cfg = load_config("backup", env, secrets)
    assert cfg.staging_dir == tmp_path / "staging"
    assert cfg.restic == ResticSettings(
        repository="s3:https://s3.example.test/bucket",
        region="eu-central-1",
        cache_dir=tmp_path / "staging" / ".restic-cache",  # the cache lives in the staging volume by default
    )
    elsewhere = load_config(
        "restore",
        {**env, "AWS_DEFAULT_REGION": "", "RESTIC_CACHE_DIR": str(tmp_path / "cache")},
        write_secrets(tmp_path / "r", db_migrator=b"m", restic_password=b"p"),
    )
    assert elsewhere.restic == ResticSettings("s3:https://s3.example.test/bucket", None, tmp_path / "cache")


def test_environ_keeps_restic_and_the_region_but_never_an_s3_key(tmp_path: Path) -> None:
    env = {
        **BASE_ENV,
        "RESTIC_REPOSITORY": "/data/restic-repo",
        "RESTIC_CACHE_DIR": "/var/cache/restic",
        "AWS_DEFAULT_REGION": "eu-central-1",
        # The S3 keys come from the secret files only (R13): never loaded from the environment.
        "AWS_ACCESS_KEY_ID": "AKIDNOTAKEY",
        "AWS_SECRET_ACCESS_KEY": "not-a-secret",
    }
    cfg = load_config("backup", env, write_secrets(tmp_path / "s", **BACKUP_SECRETS))
    assert set(cfg.environ) == set(BASE_ENV) | {"RESTIC_REPOSITORY", "RESTIC_CACHE_DIR", "AWS_DEFAULT_REGION"}


# --- M4 Task 19: the built SPA the api serves (`JOBHUNTER_SPA_DIR`) ---------------------------------------


def test_spa_dir_defaults_to_the_image_s_directory_and_follows_the_environment(tmp_path: Path) -> None:
    secrets = write_secrets(tmp_path / "s", **API_SECRETS)
    assert load_config("api", BASE_ENV, secrets).spa_dir == Path("/opt/spa")
    env = {**BASE_ENV, "JOBHUNTER_SPA_DIR": str(tmp_path / "dist")}
    assert load_config("api", env, secrets).spa_dir == tmp_path / "dist"


# --- M2 Task 4 (fix round 1): `ANTHROPIC_LOG` makes the Anthropic SDK log request content ------------------


@pytest.mark.parametrize("value", ["debug", "info", "DEBUG", " info "])
def test_anthropic_log_debug_or_info_is_refused_in_prod(tmp_path: Path, value: str) -> None:
    env = {**BASE_ENV, "JOBHUNTER_ENV": "prod", "ANTHROPIC_LOG": value}
    with pytest.raises(StartupCheckFailed) as exc:
        run_startup_assertions(load_config("api", env, write_secrets(tmp_path / "s", **API_SECRETS)))
    assert (exc.value.check, exc.value.detail) == (
        "anthropic_log_in_prod",
        "ANTHROPIC_LOG must not be debug or info in prod",
    )


@pytest.mark.parametrize(("env_name", "value"), [("prod", "warning"), ("prod", ""), ("dev", "debug")])
def test_anthropic_log_otherwise_passes(tmp_path: Path, env_name: str, value: str) -> None:
    # outside prod the app's logging setup holds the SDK's loggers at WARNING (tests/unit/test_redact.py)
    env = {**BASE_ENV, "JOBHUNTER_ENV": env_name, "ANTHROPIC_LOG": value, **operator_env(tmp_path)}
    run_startup_assertions(load_config("api", env, write_secrets(tmp_path / "s", **API_SECRETS)))


# --- M5 Task 4: the ntfy topic and the healthchecks Management API base (O4, Deviation 11) -----------------

TOPIC_URL = "https://ntfy.example/jh-alerts-4f2a9c"
PROD_WORKER_SECRETS = {
    **WORKER_SECRETS,
    "healthchecks_ping_url": HC_PING_URL.encode(),  # the uuid form (M5 Task 5, O5)
    "healthchecks_api_key": b"k",
}


def test_ntfy_topic_url_and_healthchecks_api_url_are_loaded(tmp_path: Path) -> None:
    from jobhunter.config import DEFAULT_HEALTHCHECKS_API_URL

    assert DEFAULT_HEALTHCHECKS_API_URL == "https://healthchecks.io/api/v3"
    secrets = write_secrets(tmp_path / "s", **WORKER_SECRETS)
    bare = load_config("worker", BASE_ENV, secrets)
    assert (bare.ntfy_topic_url, bare.healthchecks_api_url) == (None, DEFAULT_HEALTHCHECKS_API_URL)
    # read by these exact names, which carry none of the prefixes
    env = {**BASE_ENV, "NTFY_TOPIC_URL": TOPIC_URL, "HEALTHCHECKS_API_URL": "https://hc.example/api/v3"}
    cfg = load_config("worker", env, secrets)
    assert (cfg.ntfy_topic_url, cfg.healthchecks_api_url) == (TOPIC_URL, "https://hc.example/api/v3")
    # an empty variable counts as unset; another spelling is not read
    empty = load_config("worker", {**BASE_ENV, "NTFY_TOPIC_URL": "", "HEALTHCHECKS_API_URL": ""}, secrets)
    assert (empty.ntfy_topic_url, empty.healthchecks_api_url) == (None, DEFAULT_HEALTHCHECKS_API_URL)
    other = load_config("worker", {**BASE_ENV, "ntfy_topic_url": TOPIC_URL, "NTFY_TOPIC": TOPIC_URL}, secrets)
    assert other.ntfy_topic_url is None and set(other.environ) == set(BASE_ENV)


@pytest.mark.parametrize(
    ("topic", "passes"),
    [
        (None, False),  # unset
        ("http://ntfy.example/jh-alerts", False),  # not https
        ("https:///jh-alerts", False),  # no host
        ("ntfy.example/jh-alerts", False),  # no scheme
        ("https://[::1/jh-alerts", False),  # not a URL at all
        # fix round 1 (review M2): read as httpx reads it, so what passes, a push can be built from
        ("https://ntfy.example:abc/jh-alerts", False),  # httpx.InvalidURL
        ("https://ntfy.example:70000/jh-alerts", False),  # no such port
        ("https://ntfy.example:0/jh-alerts", False),
        (TOPIC_URL, True),
        ("https://ntfy.example:8443/jh-alerts", True),
        ("HTTPS://ntfy.example/jh-alerts", True),  # httpx lower-cases the scheme
    ],
)
def test_ntfy_in_prod_requires_an_https_topic(tmp_path: Path, topic: str | None, passes: bool) -> None:
    env = {**BASE_ENV, "JOBHUNTER_ENV": "prod"} | ({} if topic is None else {"NTFY_TOPIC_URL": topic})
    cfg = load_config("worker", env, write_secrets(tmp_path / "s", **PROD_WORKER_SECRETS))
    if passes:
        run_startup_assertions(cfg)
        return
    with pytest.raises(StartupCheckFailed) as exc:
        run_startup_assertions(cfg)
    assert (exc.value.check, exc.value.detail) == (
        "ntfy_in_prod",
        "a prod worker needs an https NTFY_TOPIC_URL",
    )
    assert topic is None or topic not in str(exc.value)  # names the variable, never its value


def test_ntfy_in_prod_runs_after_healthchecks_in_prod_and_only_for_a_prod_worker(tmp_path: Path) -> None:
    # a prod worker missing both: the healthchecks check comes first (O4)
    assert check_of(
        "worker", {**BASE_ENV, "JOBHUNTER_ENV": "prod"}, write_secrets(tmp_path / "s", **WORKER_SECRETS)
    ) == ("healthchecks_in_prod")
    # a worker outside prod, and the api in prod, need no topic; an http one is fine outside prod
    for env_name in ("dev", "test"):
        env = {**BASE_ENV, "JOBHUNTER_ENV": env_name}
        run_startup_assertions(load_config("worker", env, write_secrets(tmp_path / "t", **WORKER_SECRETS)))
        env["NTFY_TOPIC_URL"] = "http://127.0.0.1:8090/jh-alerts"
        run_startup_assertions(load_config("worker", env, write_secrets(tmp_path / "t", **WORKER_SECRETS)))
    run_startup_assertions(
        load_config(
            "api",
            {**BASE_ENV, "JOBHUNTER_ENV": "prod", **operator_env(tmp_path)},
            write_secrets(tmp_path / "a", **API_SECRETS),
        )
    )


def test_the_topic_url_is_masked_in_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    from jobhunter.config import startup_or_exit
    from jobhunter.observability.logging import configure_logging, get_logger
    from jobhunter.observability.redact import mask_text

    topic = "https://ntfy.example/jhalertsprivate"  # no digit: the whole URL is what is registered
    d = write_secrets(tmp_path / "s", **WORKER_SECRETS)
    for name, value in {**BASE_ENV, "JOBHUNTER_SECRETS_DIR": str(d), "NTFY_TOPIC_URL": topic}.items():
        monkeypatch.setenv(name, value)
    assert mask_text(f"POST {topic}") == f"POST {topic}"  # no pattern knows it

    cfg = startup_or_exit("worker")

    assert cfg.ntfy_topic_url == topic
    assert mask_text(f"POST {topic}") == "POST [SECRET]"
    assert mask_text(f"POST {TOPIC_URL}") == f"POST {TOPIC_URL}"  # only the configured topic
    buffer = io.StringIO()
    configure_logging("INFO", stream=buffer)
    try:
        get_logger("jobhunter.test").info("ntfy_probe", url=topic, nested={"error": f"refused {topic}/json"})
    finally:
        configure_logging("INFO")
    [line] = buffer.getvalue().splitlines()
    event = json.loads(line)
    assert (event["url"], event["nested"]) == ("[SECRET]", {"error": "refused [SECRET]/json"})
    assert topic not in line


# --- Fix round 1 (review M2): the ntfy token is sent as a header value -------------------------------------


@pytest.mark.parametrize(
    "token",
    [
        "tök-token".encode(),  # not ASCII
        b"tk test token",  # a space
        b"tk-test\x01token",  # a control character
        b"tk-test\r\nX-Injected: 1",  # a header line of its own
        b"tk-test-token\x7f",  # DEL
    ],
)
@pytest.mark.parametrize("env_name", ["prod", "dev", "test"])
def test_the_ntfy_token_must_be_a_header_value(tmp_path: Path, token: bytes, env_name: str) -> None:
    env = {**BASE_ENV, "JOBHUNTER_ENV": env_name, "NTFY_TOPIC_URL": TOPIC_URL}
    secrets = write_secrets(tmp_path / "s", **PROD_WORKER_SECRETS, ntfy_token=token)
    with pytest.raises(StartupCheckFailed) as exc:
        run_startup_assertions(load_config("worker", env, secrets))
    assert (exc.value.check, exc.value.detail) == (
        "ntfy_token_header_safe",
        "ntfy_token must be visible ASCII, without spaces or control characters",
    )
    assert token.decode("utf-8", errors="replace").strip() not in str(exc.value)  # never the value


@pytest.mark.parametrize("token", [b"tk-test-token", b"tk-test-token\n", b"  tk-test-token  ", b"\n"])
def test_a_visible_ascii_ntfy_token_or_none_passes(tmp_path: Path, token: bytes) -> None:
    env = {**BASE_ENV, "JOBHUNTER_ENV": "prod", "NTFY_TOPIC_URL": TOPIC_URL}
    secrets = write_secrets(tmp_path / "s", **PROD_WORKER_SECRETS, ntfy_token=token)
    run_startup_assertions(
        load_config("worker", env, secrets)
    )  # outer whitespace is stripped, as by the worker


# --- M5 Task 5: the ping URL's form (O5) --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ping_url", "passes"),
    [
        (HC_PING_URL, True),
        ("https://hc-ping.example:8443/ping/00000000-0000-4000-8000-000000000001", True),  # self-hosted
        ("https://hc-ping.example/pingkeyxyz/collection", False),  # the slug form: no uuid to manage
        ("https://hc-ping.example/00000000-0000-4000-8000-000000000001/fail", False),
        ("http://hc-ping.example/00000000-0000-4000-8000-000000000001", False),  # not https
        ("hc-ping.example/00000000-0000-4000-8000-000000000001", False),  # no scheme
        ("https://[::1/00000000-0000-4000-8000-000000000001", False),  # not a URL at all
    ],
)
def test_healthchecks_ping_url_form_refuses_a_slug_url_in_prod(
    tmp_path: Path, ping_url: str, passes: bool
) -> None:
    env = {**BASE_ENV, "JOBHUNTER_ENV": "prod", "NTFY_TOPIC_URL": TOPIC_URL}
    secrets = {**PROD_WORKER_SECRETS, "healthchecks_ping_url": ping_url.encode()}
    cfg = load_config("worker", env, write_secrets(tmp_path / "s", **secrets))
    if passes:
        run_startup_assertions(cfg)
        return
    with pytest.raises(StartupCheckFailed) as exc:
        run_startup_assertions(cfg)
    assert (exc.value.check, exc.value.detail) == (
        "healthchecks_ping_url_form",
        "healthchecks_ping_url must be an https URL that ends in the check's uuid",
    )
    assert ping_url not in str(exc.value) and "hc-ping" not in str(
        exc.value
    )  # the secret's name, never its value
    # outside prod a worker starts with it (and without a dead-man: `observability.deadman`)
    for env_name in ("dev", "test"):
        other = load_config(
            "worker", {**env, "JOBHUNTER_ENV": env_name}, write_secrets(tmp_path / "t", **secrets)
        )
        run_startup_assertions(other)


def test_healthchecks_ping_url_form_comes_right_after_healthchecks_in_prod(tmp_path: Path) -> None:
    """Part of §16.8 check 8 (O5): after the presence of both secrets, before `ntfy_in_prod`."""
    slug = {**PROD_WORKER_SECRETS, "healthchecks_ping_url": b"https://hc-ping.example/pingkeyxyz/collection"}
    prod = {**BASE_ENV, "JOBHUNTER_ENV": "prod"}  # no NTFY_TOPIC_URL either
    assert check_of("worker", prod, write_secrets(tmp_path / "s", **slug)) == "healthchecks_ping_url_form"
    no_key = {k: v for k, v in slug.items() if k != "healthchecks_api_key"}
    assert check_of("worker", prod, write_secrets(tmp_path / "t", **no_key)) == "healthchecks_in_prod"


# --- M5 Task 5 pre-review fix (Ruling T5-3): where the API key goes -----------------------------------------

# Refused wherever the key is set (fix round 1, Ruling T5-5: the scheme alone depends on the env).
UNSAFE_API_URLS = (
    "https:///api/v3",  # no host
    "http:///api/v3",
    "https://jh:jh@healthchecks.example/api/v3",  # user information
    "http://jh@healthchecks.example/api/v3",  # a user alone
    "https://healthchecks.example:0/api/v3",  # no such port
    "http://healthchecks.example:70000/api/v3",
    "https://healthchecks.example:abc/api/v3",  # httpx.InvalidURL
    "healthchecks.example/api/v3",  # no scheme
    "ftp://healthchecks.example/api/v3",  # no web URL
)
HTTP_API_URLS = ("http://healthchecks.example/api/v3", "http://stubsvc:8099/hc/api/v3")  # Task 21's stand-in


def api_url_refusal(env_name: str) -> str:
    scheme = "an https" if env_name == "prod" else "an http or https"
    return (
        f"HEALTHCHECKS_API_URL must be {scheme} URL with a host, a port from 1 to 65535 when one is given, "
        "and no user information"
    )


def api_url_config(tmp_path: Path, env_name: str, api_url: str | None, *, key: bool = True) -> AppConfig:
    env = {**BASE_ENV, "JOBHUNTER_ENV": env_name, "NTFY_TOPIC_URL": TOPIC_URL}
    if api_url is not None:
        env["HEALTHCHECKS_API_URL"] = api_url
    secrets = PROD_WORKER_SECRETS if key else WORKER_SECRETS
    tmp_path.mkdir(parents=True, exist_ok=True)
    return load_config("worker", env, write_secrets(tmp_path / "s", **secrets))


@pytest.mark.parametrize("api_url", UNSAFE_API_URLS)
@pytest.mark.parametrize("env_name", ["prod", "dev", "test"])
def test_healthchecks_api_url_safe_refuses_an_unsafe_url_where_the_key_goes(
    tmp_path: Path, api_url: str, env_name: str
) -> None:
    with pytest.raises(StartupCheckFailed) as exc:
        run_startup_assertions(api_url_config(tmp_path, env_name, api_url))
    assert (exc.value.check, exc.value.detail) == ("healthchecks_api_url_safe", api_url_refusal(env_name))
    assert api_url not in str(exc.value) and "healthchecks.example" not in str(exc.value)  # never the URL


@pytest.mark.parametrize("api_url", HTTP_API_URLS)
def test_an_http_api_url_passes_outside_prod_and_is_refused_in_prod(tmp_path: Path, api_url: str) -> None:
    """Fix round 1, Ruling T5-5: the compose test stack runs `JOBHUNTER_ENV=test` with an http stand-in."""
    for env_name in ("dev", "test"):
        run_startup_assertions(api_url_config(tmp_path / env_name, env_name, api_url))
    with pytest.raises(StartupCheckFailed) as exc:
        run_startup_assertions(api_url_config(tmp_path / "prod", "prod", api_url))
    assert (exc.value.check, exc.value.detail) == ("healthchecks_api_url_safe", api_url_refusal("prod"))
    assert api_url not in str(exc.value) and "stubsvc" not in str(exc.value)


@pytest.mark.parametrize("env_name", ["prod", "dev", "test"])
def test_the_default_or_an_https_api_url_passes_and_without_the_key_nothing_is_checked(
    tmp_path: Path, env_name: str
) -> None:
    run_startup_assertions(api_url_config(tmp_path / "default", env_name, None))  # healthchecks.io's v3 API
    for n, api_url in enumerate(
        ("https://healthchecks.example/api/v3", "https://healthchecks.example:8443/x")
    ):
        run_startup_assertions(api_url_config(tmp_path / f"https{n}", env_name, api_url))
    if env_name == "prod":
        return  # a prod worker needs the key (`healthchecks_in_prod`)
    for n, api_url in enumerate(UNSAFE_API_URLS):  # the API URL is unused without the key: nothing to check
        run_startup_assertions(api_url_config(tmp_path / f"nokey{n}", env_name, api_url, key=False))


def test_healthchecks_api_url_safe_comes_right_after_the_ping_url_form(tmp_path: Path) -> None:
    prod = {**BASE_ENV, "JOBHUNTER_ENV": "prod", "HEALTHCHECKS_API_URL": "http://healthchecks.example/api/v3"}
    slug = {**PROD_WORKER_SECRETS, "healthchecks_ping_url": b"https://hc-ping.example/pingkeyxyz/collection"}
    assert check_of("worker", prod, write_secrets(tmp_path / "s", **slug)) == "healthchecks_ping_url_form"
    # a right ping URL, an http API URL and no topic: the API URL is refused before the topic is checked
    assert check_of("worker", prod, write_secrets(tmp_path / "t", **PROD_WORKER_SECRETS)) == (
        "healthchecks_api_url_safe"
    )
