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
    Role,
    StartupCheckFailed,
    load_config,
    run_startup_assertions,
)


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
    run_startup_assertions(load_config("worker", env, write_secrets(tmp_path / "s", **WORKER_SECRETS)))
    # a legacy endpoint alias that is the EU endpoint too is fine
    env["LANGCHAIN_ENDPOINT"] = "https://eu.api.smith.langchain.com"
    run_startup_assertions(load_config("worker", env, write_secrets(tmp_path / "t", **WORKER_SECRETS)))


def test_a_prod_worker_with_both_healthchecks_secrets_passes(tmp_path: Path) -> None:
    hc = {"healthchecks_ping_url": b"https://hc.example/ping/x", "healthchecks_api_key": b"k"}
    cfg = load_config(
        "worker", {**BASE_ENV, "JOBHUNTER_ENV": "prod"}, write_secrets(tmp_path / "s", **WORKER_SECRETS, **hc)
    )
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
    cfg = load_config("api", {**BASE_ENV, "JOBHUNTER_ENV": env_name, "JOBHUNTER_PUBLIC_ORIGIN": origin}, d)
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
    env = {**BASE_ENV, "JOBHUNTER_ENV": env_name, "ANTHROPIC_LOG": value}
    run_startup_assertions(load_config("api", env, write_secrets(tmp_path / "s", **API_SECRETS)))
