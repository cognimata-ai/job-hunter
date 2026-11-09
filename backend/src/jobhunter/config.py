"""Configuration loading and startup assertions (spec §16.8, §19.2).

`load_config` reads the non-secret environment and the secret files into an immutable `AppConfig`
and raises `StartupCheckFailed("env_valid")` for an unusable environment. `run_startup_assertions`
then checks the loaded configuration in the order of §16.8 and raises `StartupCheckFailed` naming
the first check that fails. A failure names a check, and a secret file when one is to blame. It
never contains a secret value. `startup_or_exit` is the process entry point: it logs the failing
check and exits with code 78.

An optional secret of the role whose file is empty reads as absent (plan Task 23): compose needs a mounted
secret's file to exist, so the dev stack mounts an empty `anthropic_key` when no key was given. Its mode is
still checked (it is mounted); an empty required secret counts as missing, and an empty secret of another role
is still foreign.
"""

import base64
import binascii
import ipaddress
import os
import re
import stat
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal, cast, get_args
from urllib.parse import quote, urlsplit

from jobhunter.observability.logging import configure_logging, get_logger
from jobhunter.observability.redact import register_secret_values
from jobhunter.secret_names import SECRET_NAMES

Role = Literal["api", "worker", "migrate"]
EnvName = Literal["prod", "dev", "test"]

STARTUP_FAILED_EXIT_CODE: Final = 78
DEFAULT_SECRETS_DIR: Final = Path("/run/secrets")
LANGSMITH_EU_ENDPOINT: Final = "https://eu.api.smith.langchain.com"
# M1 (spec §5.2, §19.2; plan Deviation 5): the fixtures served in fake mode, the embedder and the User-Agent.
DEFAULT_FIXTURES_DIR: Final = Path("tests/contract/fixtures")
DEFAULT_EMBEDDER_URL: Final = "http://embedder:8080"
DEFAULT_HTTP_USER_AGENT: Final = "JobHunter/1.0 (personal job search)"
# M2 (plan Task 4): the recorded Anthropic streams served in fake LLM mode.
DEFAULT_FAKE_LLM_DIR: Final = Path("tests/contract/fixtures/anthropic")
# M2 (plan Task 9): the `cv_store` volume of the encrypted CV files (§12.1), on the api and the worker.
DEFAULT_CV_STORE_DIR: Final = Path("/data/cv")
# M2 (plan Task 11): the `parse_jobs` tmpfs the worker shares with docparse (§12.2); docparse's own
# `docparse.worker.main` reads the same variable.
DEFAULT_PARSE_JOBS_DIR: Final = Path("/data/parse")
# M4 (plan Task 19): the built SPA in the app image (the Dockerfile's `spa` stage), which the api serves.
DEFAULT_SPA_DIR: Final = Path("/opt/spa")

# LANGCHAIN_* are the legacy names the langsmith SDK still honours, so `langsmith_config` must see them.
ENV_PREFIXES: Final = ("JOBHUNTER_", "LANGSMITH_", "LANGCHAIN_", "ANTHROPIC_")
# M5 (plan Task 4, spec §19.2 L6167-6168): the two names without a prefix that the worker reads as well, the
# ntfy topic of the alert dispatcher and the healthchecks Management API base (§17.7).
ENV_NAMES: Final = ("NTFY_TOPIC_URL", "HEALTHCHECKS_API_URL")
DEFAULT_HEALTHCHECKS_API_URL: Final = "https://healthchecks.io/api/v3"
_TRACING_VARS: Final = (
    "LANGSMITH_TRACING",
    "LANGSMITH_TRACING_V2",
    "LANGCHAIN_TRACING_V2",
    "LANGCHAIN_TRACING",
)
# Every fake mode and test control, refused in prod (`fake_mode_in_prod`, §16.8 check 3), M4 Task 18's two
# stack controls of the fake LLM included.
_FAKE_MODE_VARS: Final = (
    "JOBHUNTER_FAKE_SOURCES",
    "JOBHUNTER_FAKE_LLM",
    "JOBHUNTER_FAULT",
    "JOBHUNTER_FAKE_BATCH_SCRIPT",
    "JOBHUNTER_FAKE_LLM_KEYED_DIR",
)
_ANTHROPIC_ENV_VARS: Final = ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_URL", "ANTHROPIC_API_KEY")
# The values with which the Anthropic SDK configures its own logging at its import, and at `debug` logs each
# request's options, the prompt included (anthropic 1.15.0 `_utils/_logs.py`). The SDK compares exactly; the
# check is case- and space-insensitive, so no near miss passes. Outside prod `configure_logging` holds the
# SDK's loggers at WARNING anyway (`observability/logging.quiet_library_loggers`).
_ANTHROPIC_LOG_VALUES: Final = ("debug", "info")
_WORLD_READABLE: Final = 0o004
# The levels `configure_logging` accepts (`JOBHUNTER_LOG_LEVEL`, compared upper-cased).
LOG_LEVELS: Final = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
# The schemes a public origin may have, with the port a browser leaves out of `Origin` for each.
_ORIGIN_DEFAULT_PORTS: Final = MappingProxyType({"http": 80, "https": 443})
# A host name as a browser serialises it (lower-case ASCII, punycode for anything else), and the last label
# that makes a browser read the whole host as an IPv4 address (decimal or hex).
_ORIGIN_HOST_NAME: Final = re.compile(r"[a-z0-9.-]+")
_IPV4_LAST_LABEL: Final = re.compile(r"[0-9]+|0x[0-9a-f]*")

ALL_SECRETS: Final = SECRET_NAMES


@dataclass(frozen=True)
class RoleSecrets:
    required: frozenset[str]
    optional: frozenset[str]


ROLE_SECRETS: Final[Mapping[Role, RoleSecrets]] = MappingProxyType(
    {
        "api": RoleSecrets(
            required=frozenset({"session_secret", "data_key", "db_app"}),
            optional=frozenset({"data_key_previous"}),
        ),
        "worker": RoleSecrets(
            required=frozenset({"data_key", "db_app"}),
            optional=frozenset(
                {
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
            ),
        ),
        "migrate": RoleSecrets(required=frozenset({"db_migrator"}), optional=frozenset()),
    }
)


class StartupCheckFailed(Exception):
    """A startup assertion failed. `check` is its name; `detail` names variables or files, never values."""

    def __init__(self, check: str, detail: str = "") -> None:
        super().__init__(check, detail)
        self.check = check
        self.detail = detail

    def __str__(self) -> str:
        return f"{self.check}: {self.detail}" if self.detail else self.check


@dataclass(frozen=True, slots=True)
class DbSettings:
    host: str
    port: int
    name: str


@dataclass(frozen=True, slots=True)
class AppConfig:
    role: Role
    env: EnvName
    db: DbSettings
    public_origin: str | None
    log_level: str
    fault: str | None
    # `JOBHUNTER_FAKE_SOURCES` set to anything (the empty string included): every adapter is served from
    # `fixtures_dir` (dev and test only; `fake_mode_in_prod` refuses it in prod).
    fake_sources: bool
    fixtures_dir: Path
    # `JOBHUNTER_FAKE_LLM` set to anything (the empty string included): every Claude call is served by the
    # recorded-SSE fake transport from `fake_llm_dir` (`llm/fake.py`; dev and test only, refused in prod).
    fake_llm: bool
    fake_llm_dir: Path
    # M4 Task 18, the fake LLM's two stack controls (dev and test only; `None` when unset or empty):
    # `JOBHUNTER_FAKE_BATCH_SCRIPT`, a JSON file of `BatchScript` fields the fake Batches API reads at each
    # create, and `JOBHUNTER_FAKE_LLM_KEYED_DIR`, a directory of standing keyed answers read at each try.
    fake_batch_script: Path | None
    fake_llm_keyed_dir: Path | None
    embedder_url: str
    http_user_agent: str
    # `JOBHUNTER_CV_STORE_DIR`: where `profile.storage.CvStore` keeps the encrypted CV files.
    cv_store_dir: Path
    # `JOBHUNTER_PARSE_JOBS_DIR`: the root of the docparse jobs (`profile.parse_client.ParseClient`).
    parse_jobs_dir: Path
    # `JOBHUNTER_SPA_DIR`: the built SPA the api serves (`api.static.SpaFiles`); absent outside the image.
    spa_dir: Path
    # M5 Task 4: `NTFY_TOPIC_URL`, the topic the worker's alert dispatcher pushes to (`None` when unset or
    # empty: no push; a prod worker needs an https one, `ntfy_in_prod`), registered for log masking by
    # `startup_or_exit`; and `HEALTHCHECKS_API_URL`, the Management API base of §17.7.
    ntfy_topic_url: str | None
    healthchecks_api_url: str
    # Neither mapping appears in repr(): a config that is logged or shown in a traceback must not leak.
    secrets: Mapping[str, bytes] = field(repr=False)
    environ: Mapping[str, str] = field(repr=False)
    secrets_dir: Path
    # What `load_config` recorded about the secret files, for `run_startup_assertions`: the permission
    # bits of every file found, and the files that exist but could not be read.
    secret_modes: Mapping[str, int] = field(default_factory=dict)
    unreadable_secrets: frozenset[str] = frozenset()

    def secret(self, name: str) -> bytes:
        return self.secrets[name]

    def secret_b64(self, name: str) -> bytes:
        decoded = _decode_b64(self.secrets[name])
        if decoded is None:
            raise ValueError(f"secret {name!r} is not valid base64")
        return decoded

    def dsn(self, driver: Literal["sqlalchemy", "psycopg"]) -> str:
        user, secret_name = (
            ("jobhunter_migrator", "db_migrator") if self.role == "migrate" else ("jobhunter_app", "db_app")
        )
        password = quote(self.secret(secret_name), safe="")
        scheme = "postgresql+psycopg" if driver == "sqlalchemy" else "postgresql"
        return f"{scheme}://{user}:{password}@{self.db.host}:{self.db.port}/{quote(self.db.name, safe='')}"


def _decode_b64(raw: bytes) -> bytes | None:
    try:
        return base64.b64decode(raw, validate=True)
    except binascii.Error:
        return None


def _read_secret_files(
    secrets_dir: Path,
) -> tuple[dict[str, bytes], dict[str, int], frozenset[str]]:
    """Record every known secret file in `secrets_dir`: its content, its mode, or that it is unreadable."""
    contents: dict[str, bytes] = {}
    modes: dict[str, int] = {}
    unreadable: set[str] = set()
    for name in sorted(ALL_SECRETS):
        path = secrets_dir / name
        try:
            st = path.stat()
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError:
            unreadable.add(name)
            continue
        modes[name] = stat.S_IMODE(st.st_mode)
        if not stat.S_ISREG(st.st_mode):
            unreadable.add(name)
            continue
        try:
            contents[name] = path.read_bytes().removesuffix(b"\n")
        except OSError:
            unreadable.add(name)
    return contents, modes, frozenset(unreadable)


def _optional_path(value: str | None) -> Path | None:
    return Path(value) if value else None


def _parse_db_settings(environ: Mapping[str, str]) -> DbSettings:
    name = environ.get("JOBHUNTER_DB_NAME")
    if not name:
        raise StartupCheckFailed("env_valid", "JOBHUNTER_DB_NAME is required")
    port_text = environ.get("JOBHUNTER_DB_PORT") or "5432"
    if not (port_text.isascii() and port_text.isdigit() and 1 <= int(port_text) <= 65535):
        raise StartupCheckFailed("env_valid", "JOBHUNTER_DB_PORT must be a port number")
    return DbSettings(host=environ.get("JOBHUNTER_DB_HOST") or "postgres", port=int(port_text), name=name)


def load_config(
    role: Role,
    environ: Mapping[str, str] | None = None,
    secrets_dir: Path | None = None,
) -> AppConfig:
    """Build the `AppConfig` of `role` from `environ` (default: the process environment) and the secret files.

    The secrets directory is `secrets_dir`, else `JOBHUNTER_SECRETS_DIR`, else `/run/secrets`; the same
    validation applies to every directory. Of the environment it keeps the names with one of `ENV_PREFIXES`,
    and the two of `ENV_NAMES`.
    """
    source = os.environ if environ is None else environ
    env = {k: v for k, v in source.items() if k.startswith(ENV_PREFIXES) or k in ENV_NAMES}

    env_name = env.get("JOBHUNTER_ENV")
    if env_name not in get_args(EnvName):
        raise StartupCheckFailed("env_valid", "JOBHUNTER_ENV must be one of prod, dev, test")
    db = _parse_db_settings(env)

    if secrets_dir is None:
        secrets_dir = Path(env.get("JOBHUNTER_SECRETS_DIR") or DEFAULT_SECRETS_DIR)
    contents, modes, unreadable = _read_secret_files(secrets_dir)
    optional = ROLE_SECRETS[role].optional
    contents = {name: value for name, value in contents.items() if value or name not in optional}

    return AppConfig(
        role=role,
        env=cast(EnvName, env_name),
        db=db,
        public_origin=env.get("JOBHUNTER_PUBLIC_ORIGIN") or None,
        log_level=(env.get("JOBHUNTER_LOG_LEVEL") or "INFO").upper(),
        fault=env.get("JOBHUNTER_FAULT") or None,
        fake_sources="JOBHUNTER_FAKE_SOURCES" in env,
        # A relative directory (the default included) is resolved from the current directory.
        fixtures_dir=Path(env.get("JOBHUNTER_FIXTURES_DIR") or DEFAULT_FIXTURES_DIR).resolve(),
        fake_llm="JOBHUNTER_FAKE_LLM" in env,
        fake_llm_dir=Path(env.get("JOBHUNTER_FAKE_LLM_DIR") or DEFAULT_FAKE_LLM_DIR).resolve(),
        fake_batch_script=_optional_path(env.get("JOBHUNTER_FAKE_BATCH_SCRIPT")),
        fake_llm_keyed_dir=_optional_path(env.get("JOBHUNTER_FAKE_LLM_KEYED_DIR")),
        embedder_url=env.get("JOBHUNTER_EMBEDDER_URL") or DEFAULT_EMBEDDER_URL,
        http_user_agent=env.get("JOBHUNTER_HTTP_USER_AGENT") or DEFAULT_HTTP_USER_AGENT,
        cv_store_dir=Path(env.get("JOBHUNTER_CV_STORE_DIR") or DEFAULT_CV_STORE_DIR),
        parse_jobs_dir=Path(env.get("JOBHUNTER_PARSE_JOBS_DIR") or DEFAULT_PARSE_JOBS_DIR),
        spa_dir=Path(env.get("JOBHUNTER_SPA_DIR") or DEFAULT_SPA_DIR),
        ntfy_topic_url=env.get("NTFY_TOPIC_URL") or None,
        healthchecks_api_url=env.get("HEALTHCHECKS_API_URL") or DEFAULT_HEALTHCHECKS_API_URL,
        secrets=MappingProxyType(contents),
        environ=MappingProxyType(env),
        secrets_dir=secrets_dir,
        secret_modes=MappingProxyType(modes),
        unreadable_secrets=unreadable,
    )


def _names(names: set[str] | frozenset[str]) -> str:
    return ", ".join(sorted(names))


def _check_secret_files(cfg: AppConfig) -> None:
    """Assertion 1: required secrets are present and readable, none is world-readable, no foreign one."""
    allowed = ROLE_SECRETS[cfg.role].required | ROLE_SECRETS[cfg.role].optional
    # A present file counts even when it cannot be read; `secrets_readable` then reports it by name.
    usable = {name for name, value in cfg.secrets.items() if value} | cfg.unreadable_secrets
    if missing := ROLE_SECRETS[cfg.role].required - usable:
        raise StartupCheckFailed("secrets_present", _names(missing))
    if cfg.unreadable_secrets:
        raise StartupCheckFailed("secrets_readable", _names(cfg.unreadable_secrets))
    if world_readable := {name for name, mode in cfg.secret_modes.items() if mode & _WORLD_READABLE}:
        raise StartupCheckFailed("secrets_not_world_readable", _names(world_readable))
    if foreign := (set(cfg.secrets) | cfg.unreadable_secrets) - allowed:
        raise StartupCheckFailed("foreign_secret_mounted", _names(foreign))


def _check_key_material(cfg: AppConfig) -> None:
    """Assertion 5: key lengths, and that the session secret and the data key differ."""
    decoded: dict[str, bytes] = {}
    for name, check, exact in (
        ("session_secret", "session_secret_length", None),
        ("data_key", "data_key_length", 32),
        ("data_key_previous", "data_key_previous_length", 32),
    ):
        if name not in cfg.secrets:
            continue
        key = _decode_b64(cfg.secrets[name])
        if key is None:
            raise StartupCheckFailed(check, f"{name} is not valid base64")
        if len(key) < 32 or (exact is not None and len(key) != exact):
            expected = "at least 32 bytes" if exact is None else f"exactly {exact} bytes"
            raise StartupCheckFailed(check, f"{name} must decode to {expected}")
        decoded[name] = key
    if (session_key := decoded.get("session_secret")) is not None and session_key == decoded.get("data_key"):
        raise StartupCheckFailed("keys_identical", "session_secret and data_key must differ")


def _is_serialised_host(host: str) -> bool:
    """`host` (without brackets) is a host in the form a browser serialises it (WHATWG URL host serialiser).

    That is an IPv6 address in its compressed form and without a zone; a dotted-quad IPv4 address in canonical
    form (a browser reads `127.1` or `0x7f.0.0.1` as `127.0.0.1`); or a name of `[a-z0-9.-]`. An IPv4-mapped
    IPv6 address is refused: Python prints it dotted (`::ffff:1.2.3.4`), browsers in hex
    (`::ffff:102:304`), and the plain IPv4 address serves the same purpose.
    """
    if ":" in host:
        try:
            address = ipaddress.IPv6Address(host)
        except ValueError:
            return False
        return address.scope_id is None and address.ipv4_mapped is None and address.compressed == host
    if not _ORIGIN_HOST_NAME.fullmatch(host):
        return False
    if _IPV4_LAST_LABEL.fullmatch(host.removesuffix(".").rpartition(".")[2]):
        try:
            return str(ipaddress.IPv4Address(host)) == host
        except ValueError:
            return False
    return True


def _is_serialised_origin(origin: str) -> bool:
    """`origin` is `scheme://host[:port]` exactly as a browser serialises it in `Origin` (RFC 6454 §6.2).

    That is: scheme `http` or `https`, lower-case; a host as `_is_serialised_host` describes it, IPv6 in
    brackets; a port only when it is not the scheme's default, without leading zeros; no userinfo, path,
    query or fragment, not even an empty one or a trailing slash. The edge compares `Origin` with the
    configured value byte for byte, so any other spelling of the same origin would refuse every unsafe
    request.
    """
    if not (origin.isascii() and origin.isprintable()) or " " in origin:
        return False
    try:
        parts = urlsplit(origin)
        port = parts.port
    except ValueError:  # a port that is no number in 0..65535, or a malformed IPv6 literal
        return False
    host = parts.hostname
    default_port = _ORIGIN_DEFAULT_PORTS.get(parts.scheme)
    if default_port is None or not host or port in (0, default_port) or not _is_serialised_host(host):
        return False
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc = f"{netloc}:{port}"
    return origin == f"{parts.scheme}://{netloc}"


def _is_https_url(url: str | None) -> bool:
    """`url` is an `https` URL with a host (`ntfy_in_prod`)."""
    if url is None:
        return False
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:  # a malformed IPv6 literal or port
        return False
    return parts.scheme == "https" and bool(host)


def _check_public_origin(cfg: AppConfig) -> None:
    """Rulings T18-c, T18-i: the api and the worker have a public origin (§19.2: the CSRF origin, the ntfy
    click link); a configured one has the exact form of `Origin`, and https in prod."""
    if cfg.public_origin is None:
        if cfg.role in ("api", "worker"):
            raise StartupCheckFailed("public_origin", "JOBHUNTER_PUBLIC_ORIGIN is required")
        return
    if not _is_serialised_origin(cfg.public_origin):
        raise StartupCheckFailed(
            "public_origin",
            "JOBHUNTER_PUBLIC_ORIGIN must be exactly scheme://host[:port], as a browser sends it in Origin",
        )
    if cfg.env == "prod" and not cfg.public_origin.startswith("https://"):
        raise StartupCheckFailed("public_origin", "JOBHUNTER_PUBLIC_ORIGIN must use https in prod")


def run_startup_assertions(cfg: AppConfig) -> None:
    """Run the startup assertions of §16.8 in order; the first failure raises `StartupCheckFailed`."""
    env = cfg.environ

    _check_secret_files(cfg)

    if "LANGSMITH_GATEWAY" in env:
        raise StartupCheckFailed("langsmith_gateway_set", "LANGSMITH_GATEWAY must not be set")
    if cfg.secrets.get("anthropic_key", b"").startswith(b"lsv2_"):
        raise StartupCheckFailed("anthropic_key_is_langsmith", "anthropic_key holds a LangSmith key")

    if cfg.env == "prod" and (fake := [name for name in _FAKE_MODE_VARS if name in env]):
        raise StartupCheckFailed("fake_mode_in_prod", f"{', '.join(fake)} must not be set in prod")

    # The langsmith SDK enables tracing from any of these variables, so each one counts here.
    if any(env.get(name, "").lower() == "true" for name in _TRACING_VARS):
        wrong = [
            name for name in ("LANGSMITH_HIDE_INPUTS", "LANGSMITH_HIDE_OUTPUTS") if env.get(name) != "true"
        ]
        if env.get("LANGSMITH_ENDPOINT") != LANGSMITH_EU_ENDPOINT:
            wrong.append("LANGSMITH_ENDPOINT")
        # The SDK falls back to the legacy endpoint variable, so a non-EU value must not be left behind.
        if env.get("LANGCHAIN_ENDPOINT", "") not in ("", LANGSMITH_EU_ENDPOINT):
            wrong.append("LANGCHAIN_ENDPOINT")
        if wrong:
            raise StartupCheckFailed("langsmith_config", f"tracing needs a correct {', '.join(wrong)}")

    _check_key_material(cfg)

    if cfg.env == "prod" and cfg.role == "worker":
        absent = [n for n in ("healthchecks_ping_url", "healthchecks_api_key") if not cfg.secrets.get(n)]
        if absent:
            raise StartupCheckFailed("healthchecks_in_prod", f"a prod worker needs {', '.join(absent)}")
        # Right after it (M5 plan O4, Deviation 11): a prod worker pushes its alerts to this topic, over TLS.
        if not _is_https_url(cfg.ntfy_topic_url):
            raise StartupCheckFailed("ntfy_in_prod", "a prod worker needs an https NTFY_TOPIC_URL")

    if cfg.env == "prod" and (anthropic := [name for name in _ANTHROPIC_ENV_VARS if name in env]):
        raise StartupCheckFailed("anthropic_env_in_prod", f"{', '.join(anthropic)} must not be set in prod")

    # The privacy rule (CV text and LLM output never reach a log): fail fast rather than rely on the logging
    # setup alone.
    if cfg.env == "prod" and env.get("ANTHROPIC_LOG", "").strip().lower() in _ANTHROPIC_LOG_VALUES:
        raise StartupCheckFailed("anthropic_log_in_prod", "ANTHROPIC_LOG must not be debug or info in prod")

    # After the §16.8 checks (ruling T13-a): the process applies this level when it configures its logging.
    if cfg.log_level not in LOG_LEVELS:
        raise StartupCheckFailed("log_level", f"JOBHUNTER_LOG_LEVEL must be one of {', '.join(LOG_LEVELS)}")

    # Also after the §16.8 checks (ruling T18-c): the edge's CSRF check compares `Origin` with this value.
    _check_public_origin(cfg)

    # §10.6 L3880 (M3 Task 4): the cached score prefix rests on a rubric block of at least 600 tokens.
    _check_rubric_block()


def _check_rubric_block() -> None:
    """L3880: every shipped rubric rendered into the packaged `shared_rubric` template is at least
    `RUBRIC_BLOCK_MIN_TOKENS` tokens at chars/4 (`rubric_block_too_short`). Imported here: the renderer
    imports the gateway, which imports this module."""
    from jobhunter.llm.prompts import render
    from jobhunter.scoring.rubric import RUBRIC_V1, RUBRIC_V2, load_rubric

    template = render.template_text("shared_rubric")
    for version in (RUBRIC_V1, RUBRIC_V2):
        block = render.rubric_block(template, load_rubric(version))
        if render.estimated_tokens(block) < render.RUBRIC_BLOCK_MIN_TOKENS:
            raise StartupCheckFailed(
                "rubric_block_too_short",
                f"the rubric block of {version} is under {render.RUBRIC_BLOCK_MIN_TOKENS} tokens",
            )


def _log_startup_failure(role: Role, exc: StartupCheckFailed) -> None:
    fields = {"check": exc.check, "role": role}
    if exc.detail:
        fields["detail"] = exc.detail
    get_logger("jobhunter.startup").critical("startup_check_failed", **fields)


def startup_or_exit(role: Role) -> AppConfig:
    """Load the process configuration and run the startup assertions; on failure log the check and exit 78."""
    configure_logging()
    try:
        cfg = load_config(role)
        # From here on a secret value that reaches a log line is masked, whatever shape it has; so is the ntfy
        # topic URL, whose topic name is all it takes to read the pushes (M5 Task 4).
        register_secret_values(value.decode("utf-8", errors="ignore") for value in cfg.secrets.values())
        if cfg.ntfy_topic_url is not None:
            register_secret_values([cfg.ntfy_topic_url])
        run_startup_assertions(cfg)
    except StartupCheckFailed as exc:
        _log_startup_failure(role, exc)
        sys.exit(STARTUP_FAILED_EXIT_CODE)
    return cfg
