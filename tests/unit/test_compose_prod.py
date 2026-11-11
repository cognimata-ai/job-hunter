"""The production compose, its test stack, the S3 stand-in and the Caddy files (spec §19.1, §19.2, §19.7,
§16.6, §17.8; plan M5 Task 21, Deviations 29, 33, 34, 55, R10, R19, R26, R28).

Offline: each compose file as `docker compose … config --format json` renders it, with `deploy/images.lock`
and `deploy/compose.test.env` (which defines the four image variables `deploy/release.lock` holds in prod, and
the rest of `/etc/jobhunter/jobhunter.env`), every profile on, in an environment of PATH, HOME and Docker's
own variables alone, so the calling shell's variables never reach the interpolation. `prod_config` is
`deploy/compose.yaml` alone, `pt_config` with `deploy/compose.test.yaml` over it (`PT`), `s3_config` the
stand-in, `dev_config` the dev stack's four files.

The last test starts the prod-configured worker's LangSmith tracing against an endpoint that never answers
(Ruling T21-1 (a)): `install_tracing` returns at once, and only the SDK's own thread waits on the network.
"""

import json
import os
import re
import subprocess
import threading
import time
from collections.abc import Mapping
from functools import cache
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from typing import Any, Final

import pytest
import requests
import yaml
from jobhunter.api.edge import SECURITY_HEADERS
from jobhunter.config import LANGSMITH_EU_ENDPOINT, ROLE_SECRETS, Role, load_config, tracing_enabled
from jobhunter.observability.langsmith import TracingState, install_tracing
from jobhunter.secret_names import SECRET_NAMES
from langsmith import Client, run_trees
from requests.adapters import BaseAdapter

REPO: Final = Path(__file__).resolve().parents[2]
DEPLOY: Final = REPO / "deploy"
ENV: Final = {
    name: value
    for name, value in os.environ.items()
    if name in ("PATH", "HOME") or name in ("DOCKER_HOST", "DOCKER_CONFIG", "DOCKER_CONTEXT")
}
# Paths and addresses inside the containers, not of this process.
TMP: Final = "/tmp"  # noqa: S108
ALL_INTERFACES: Final = "0.0.0.0"  # noqa: S104

# §19.1 and §19.2: what each service of the production compose is given.
APP_SERVICES: Final[Mapping[str, Role]] = {
    "api": "api",
    "worker": "worker",
    "migrate": "migrate",
    "backup": "backup",
    "restore": "restore",
}
SECRETS: Final[Mapping[str, frozenset[str]]] = {
    "caddy": frozenset(),
    "migrate": frozenset({"db_migrator"}),
    "api": frozenset({"session_secret", "data_key", "data_key_previous", "db_app"}),
    "worker": frozenset(
        {
            "data_key",
            "data_key_previous",
            "db_app",
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
    "docparse": frozenset(),
    "embedder": frozenset(),
    "postgres": frozenset({"pg_bootstrap", "db_app", "db_migrator", "db_backup", "db_restore"}),
    "backup": frozenset(
        {"db_backup", "db_restore", "restic_password", "s3_access_key_id", "s3_secret_access_key"}
    ),
    "restore": frozenset({"db_migrator", "restic_password", "s3_access_key_id", "s3_secret_access_key"}),
}
# §16.8 check 9: none of these is ever mounted into the api.
NOT_FOR_THE_API: Final = frozenset(
    {
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
NETWORKS: Final[Mapping[str, frozenset[str]]] = {
    "caddy": frozenset({"edge", "proxy"}),
    "migrate": frozenset({"internal"}),
    "api": frozenset({"proxy", "internal"}),
    "worker": frozenset({"internal", "egress"}),
    "embedder": frozenset({"internal"}),
    "postgres": frozenset({"internal"}),
    "backup": frozenset({"internal", "egress"}),
    "restore": frozenset({"internal", "egress"}),
}
# §19.2's environment table, by service: required names (`${NAME:?}`) and optional ones (`${NAME:-}`).
APP_REQUIRED: Final = frozenset(
    {"JOBHUNTER_ENV", "JOBHUNTER_DB_HOST", "JOBHUNTER_DB_PORT", "JOBHUNTER_DB_NAME"}
)
APP_OPTIONAL: Final = frozenset({"JOBHUNTER_LOG_LEVEL"})
RESTIC_REQUIRED: Final = frozenset({"RESTIC_REPOSITORY", "AWS_DEFAULT_REGION"})
BACKUP_OPTIONAL: Final = frozenset(
    {
        "JOBHUNTER_BACKUP_CRON",
        "JOBHUNTER_MANIFEST_SHIP_CRON",
        "JOBHUNTER_RESTORE_TEST_CRON",
        "JOBHUNTER_BACKUP_KEEP_DAILY",
        "JOBHUNTER_BACKUP_KEEP_WEEKLY",
        "JOBHUNTER_BACKUP_KEEP_MONTHLY",
    }
)
REQUIRED_ENV: Final[Mapping[str, frozenset[str]]] = {
    "caddy": frozenset({"JOBHUNTER_DOMAIN", "JOBHUNTER_ACME_EMAIL"}),
    "migrate": APP_REQUIRED,
    "api": APP_REQUIRED | {"JOBHUNTER_PUBLIC_ORIGIN"},
    "worker": APP_REQUIRED | {"JOBHUNTER_PUBLIC_ORIGIN", "JOBHUNTER_EMBEDDER_URL", "NTFY_TOPIC_URL"},
    "docparse": frozenset(),
    "embedder": frozenset(),
    "postgres": frozenset({"POSTGRES_DB", "POSTGRES_USER"}),
    "backup": APP_REQUIRED | RESTIC_REQUIRED,
    "restore": APP_REQUIRED | RESTIC_REQUIRED,
}
OPTIONAL_ENV: Final[Mapping[str, frozenset[str]]] = {
    "migrate": APP_OPTIONAL,
    "api": APP_OPTIONAL,
    "worker": APP_OPTIONAL | {"JOBHUNTER_HTTP_USER_AGENT", "HEALTHCHECKS_API_URL"},
    "backup": APP_OPTIONAL | BACKUP_OPTIONAL,
    "restore": APP_OPTIONAL,
}
# Fixed in the compose itself: §17.8's worker values, and where the bootstrap password is read from.
TRACING: Final = {
    "LANGSMITH_TRACING": "true",
    "LANGSMITH_ENDPOINT": LANGSMITH_EU_ENDPOINT,
    "LANGSMITH_PROJECT": "jobhunter-prod",
    "LANGSMITH_HIDE_INPUTS": "true",
    "LANGSMITH_HIDE_OUTPUTS": "true",
}
FIXED_ENV: Final[Mapping[str, Mapping[str, str]]] = {
    "worker": TRACING,
    "postgres": {"POSTGRES_PASSWORD_FILE": "/run/secrets/pg_bootstrap"},
}
# What the test stack adds (compose.fixtures.yaml, compose.fakellm.yaml, compose.e2e.yaml's embedder).
TEST_ONLY_ENV: Final[Mapping[str, frozenset[str]]] = {
    "worker": frozenset(
        {
            "JOBHUNTER_FAKE_SOURCES",
            "JOBHUNTER_FIXTURES_DIR",
            "JOBHUNTER_FAKE_LLM",
            "JOBHUNTER_FAKE_LLM_DIR",
            "JOBHUNTER_FAULT",
        }
    ),
    "api": frozenset({"JOBHUNTER_FAKE_LLM"}),
    "embedder": frozenset({"EMBEDDER_FAKE", "JOBHUNTER_ENV"}),
}
# §19.7 and Deviation 33 (R28), in bytes; the test stack's smaller ones (a dev container).
GIB: Final = 1024**3
MIB: Final = 1024**2
MEMORY: Final[Mapping[str, int]] = {
    "postgres": 5 * GIB,
    "embedder": 3500 * MIB,
    "worker": 2 * GIB,
    "api": 1 * GIB,
    "docparse": 1 * GIB,
    "caddy": 256 * MIB,
    "backup": 1 * GIB,
    "migrate": 2 * GIB,
    "restore": 2 * GIB,
}
TEST_MEMORY: Final[Mapping[str, int]] = {"postgres": 1 * GIB, "embedder": 1 * GIB, "worker": 1500 * MIB}
LOGGING: Final = {"driver": "local", "options": {"max-size": "20m", "max-file": "10"}}
# The test stack's own services: the ntfy and healthchecks stand-in, and the two probes on `edge`.
TEST_SERVICES: Final[Mapping[str, Mapping[str, str | None]]] = {
    "stubsvc": {"egress": None},
    "probe1": {"edge": "172.30.1.11"},
    "probe2": {"edge": "172.30.1.12"},
}
DIGEST_PINNED: Final = re.compile(r"[a-z0-9./_-]+(:[A-Za-z0-9._-]+)?@sha256:[0-9a-f]{64}")
# The named volumes (Task 21's table): mount point, the writers' uid (`None`: the image's root user) and who
# writes and who reads only, across PROD, TEST, S3 and DEV.
VOLUMES: Final[Mapping[str, tuple[str, str | None, frozenset[str], frozenset[str]]]] = {
    "pgdata": ("/var/lib/postgresql/data", "999", frozenset({"postgres"}), frozenset()),
    "cv_store": (
        "/data/cv",
        "10001",
        frozenset({"api", "worker", "migrate", "restore"}),
        frozenset({"backup"}),
    ),
    "manifests": (
        "/data/manifests",
        "10001",
        frozenset({"worker", "migrate", "restore"}),
        frozenset({"backup", "shipper"}),
    ),
    "exports": ("/data/exports", "10001", frozenset({"worker", "migrate", "restore"}), frozenset({"api"})),
    "backup_staging": ("/data/staging", "10001", frozenset({"backup", "restore", "shipper"}), frozenset()),
    "restic_repo": ("/data/restic-repo", "10001", frozenset({"shipper"}), frozenset()),
    "parse_jobs": ("/data/parse", None, frozenset({"worker", "docparse"}), frozenset()),
    "caddy_data": ("/data", None, frozenset({"caddy"}), frozenset()),
    "caddy_config": ("/config", None, frozenset({"caddy"}), frozenset()),
    "s3data": ("/data", "0", frozenset({"s3stub"}), frozenset()),
}
PARSE_JOBS_OPTS: Final = {"type": "tmpfs", "device": "tmpfs", "o": "size=256m,uid=0,gid=10011,mode=2770"}


def render(project: str, env_files: tuple[str, ...], files: tuple[str, ...]) -> dict[str, Any]:
    argv = ["docker", "compose", "-p", project]
    for name in env_files:
        argv += ["--env-file", str(name if Path(name).is_absolute() else DEPLOY / name)]
    for name in files:
        argv += ["-f", str(DEPLOY / name)]
    done = subprocess.run(
        [*argv, "--profile", "*", "config", "--format", "json"],
        capture_output=True,
        text=True,
        check=False,
        env=ENV,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    rendered: dict[str, Any] = json.loads(done.stdout)
    return rendered


@cache
def prod_config() -> dict[str, Any]:
    return render("jobhunter", ("images.lock", "compose.test.env"), ("compose.yaml",))


@cache
def pt_config() -> dict[str, Any]:
    return render(
        "jobhunter-prodtest", ("images.lock", "compose.test.env"), ("compose.yaml", "compose.test.yaml")
    )


@cache
def s3_config() -> dict[str, Any]:
    return render("jobhunter-s3stub", ("images.lock",), ("compose.s3stub.yaml",))


@cache
def dev_config() -> dict[str, Any]:
    files = ("compose.dev.yaml", "compose.fixtures.yaml", "compose.fakellm.yaml", "compose.e2e.yaml")
    return render("jobhunter-dev", ("images.lock",), files)


def secrets_of(service: Mapping[str, Any]) -> frozenset[str]:
    return frozenset(entry["source"] for entry in service.get("secrets", []))


def uid_of(service: Mapping[str, Any]) -> str | None:
    user = service.get("user")
    return None if user is None else str(user).split(":")[0]


def test_each_service_has_its_spec_networks_and_secrets() -> None:
    services = prod_config()["services"]
    assert set(services) == set(SECRETS)
    for name, service in services.items():
        assert secrets_of(service) == SECRETS[name], name
        if name == "docparse":
            assert service["network_mode"] == "none" and "networks" not in service  # §12.2
        else:
            assert set(service["networks"]) == NETWORKS[name], name
    for name, role in APP_SERVICES.items():  # what the startup assertion `secrets_present` and its kin accept
        given, wanted = secrets_of(services[name]), ROLE_SECRETS[role]
        assert wanted.required <= given <= wanted.required | wanted.optional, name
    assert not secrets_of(services["api"]) & NOT_FOR_THE_API  # §16.8 check 9
    test = pt_config()["services"]
    assert set(test) == set(SECRETS) | set(TEST_SERVICES)
    for name in SECRETS:
        assert secrets_of(test[name]) == SECRETS[name], name
        extra = {"s3net"} if name in ("backup", "restore") else set()  # the S3 stand-in's network
        if name != "docparse":
            assert set(test[name]["networks"]) == NETWORKS[name] | extra, name
    for name, networks in TEST_SERVICES.items():
        assert not secrets_of(test[name]), name
        addresses = {net: (cfg or {}).get("ipv4_address") for net, cfg in test[name]["networks"].items()}
        assert addresses == networks, name
    assert pt_config()["networks"]["s3net"] == {"name": "jobhunter-s3net", "ipam": {}, "external": True}


def test_only_caddy_publishes_ports() -> None:
    def published(config: Mapping[str, Any]) -> dict[str, list[tuple[str | None, str, int, str]]]:
        return {
            name: [(p.get("host_ip"), p["published"], p["target"], p["protocol"]) for p in service["ports"]]
            for name, service in config["services"].items()
            if service.get("ports")
        }

    assert published(prod_config()) == {"caddy": [(None, "80", 80, "tcp"), (None, "443", 443, "tcp")]}
    assert published(pt_config()) == {
        "caddy": [("127.0.0.1", "8443", 443, "tcp"), ("127.0.0.1", "8081", 80, "tcp")]  # loopback only
    }
    assert published(s3_config()) == {}


def test_subnets_and_caddy_addresses_are_pinned() -> None:
    networks = prod_config()["networks"]
    pinned = {
        "proxy": ("172.30.0.0/24", "172.30.0.128/25", True),
        "edge": ("172.30.1.0/24", "172.30.1.128/25", False),
        "internal": ("172.30.2.0/24", None, True),
        "egress": ("172.30.3.0/24", None, False),
    }
    assert set(networks) == set(pinned)
    for name, (subnet, ip_range, internal) in pinned.items():
        [config] = networks[name]["ipam"]["config"]
        assert (config["subnet"], config.get("ip_range"), networks[name].get("internal", False)) == (
            subnet,
            ip_range,
            internal,
        ), name
        if ip_range is not None:
            assert config["gateway"] == str(IPv4Network(subnet)[1]), name
    caddy = prod_config()["services"]["caddy"]["networks"]
    assert caddy == {"edge": {"ipv4_address": "172.30.1.2"}, "proxy": {"ipv4_address": "172.30.0.2"}}
    # The fixed addresses lie outside the dynamic range, so no container started earlier can take one.
    fixed = [
        ("proxy", "172.30.0.2"),
        ("edge", "172.30.1.2"),
        ("edge", "172.30.1.11"),
        ("edge", "172.30.1.12"),
    ]
    for name, address in fixed:
        [config] = pt_config()["networks"][name]["ipam"]["config"]
        assert IPv4Address(address) in IPv4Network(config["subnet"]), address
        assert IPv4Address(address) not in IPv4Network(config["ip_range"]), address
    api = prod_config()["services"]["api"]["command"]
    assert api[api.index("--forwarded-allow-ips") + 1] == "172.30.0.2"  # Caddy, the one trusted forwarder


def test_hardening_on_every_service() -> None:
    for config, memory in ((prod_config(), MEMORY), (pt_config(), {**MEMORY, **TEST_MEMORY})):
        services = config["services"]
        for name, service in services.items():
            assert service["cap_drop"] == ["ALL"], name
            assert "no-new-privileges:true" in service["security_opt"], name
            assert service.get("read_only", False) is (name != "postgres"), name
            assert service["logging"] == LOGGING, name
            assert not service.get("privileged", False), name
            assert "cap_add" not in service or name == "caddy", name
            if name in memory:
                assert int(service["mem_limit"]) == memory[name], name
            if name in TEST_SERVICES:
                assert uid_of(service) == "65534", name  # nobody
        for name in APP_SERVICES:
            service = services[name]
            assert service["init"] is True and service["user"] == "10001:10001", name
            assert "10010" in service["group_add"], (
                name
            )  # reads the 0440 secret files by group (Deviation 29)
            assert service["tmpfs"][0].split(":")[0] == TMP, name
        assert services["worker"]["tmpfs"] == [
            f"{TMP}:size=1g"
        ]  # the plaintext export zip (Ruling T21-1 (a))
        assert services["caddy"]["cap_add"] == ["NET_BIND_SERVICE"] and "user" not in services["caddy"]
        assert (services["postgres"]["user"], services["postgres"]["group_add"]) == ("999:999", ["10010"])
        assert (services["embedder"]["user"], services["docparse"]["user"]) == ("10004:10004", "10002:10002")
        assert services["docparse"]["pids_limit"] == 16 and services["docparse"]["tmpfs"] == [TMP]
        assert services["embedder"]["cpus"] == 3
    s3 = s3_config()["services"]["s3stub"]
    assert (s3["user"], s3["cap_drop"], s3["read_only"]) == ("0:0", ["ALL"], True)


def test_each_service_sees_only_its_own_env_names() -> None:
    services = prod_config()["services"]
    for name in SECRETS:
        required, optional = REQUIRED_ENV[name], OPTIONAL_ENV.get(name, frozenset())
        fixed = FIXED_ENV.get(name, {})
        environment: dict[str, str] = services[name].get("environment", {})
        assert set(environment) == required | optional | set(fixed), name
        assert {key: environment[key] for key in fixed} == fixed, name
    # In the file itself, each name is its own variable: `NAME: ${NAME:?}`, or `${NAME:-}` when optional.
    raw = yaml.safe_load((DEPLOY / "compose.yaml").read_text(encoding="utf-8"))["services"]
    for name, service in raw.items():
        environment = service.get("environment", {})
        for key in REQUIRED_ENV[name]:
            assert environment[key] == f"${{{key}:?}}", (name, key)
        for key in OPTIONAL_ENV.get(name, frozenset()):
            assert environment[key] == f"${{{key}:-}}", (name, key)
    test = pt_config()["services"]
    for name in SECRETS:
        expected = set(services[name].get("environment", {})) | TEST_ONLY_ENV.get(name, frozenset())
        assert set(test[name].get("environment", {})) == expected, name


def test_tracing_is_on_in_prod_and_off_in_test(tmp_path: Path) -> None:
    for config in (prod_config(), pt_config()):
        for name, service in config["services"].items():
            if name != "worker":  # only the worker traces (§17.8)
                assert not [
                    k for k in service.get("environment", {}) if k.startswith(("LANGSMITH_", "LANGCHAIN_"))
                ]
    worker = prod_config()["services"]["worker"]
    assert {key: worker["environment"][key] for key in TRACING} == TRACING
    assert "langsmith_key" in secrets_of(worker)
    assert pt_config()["services"]["worker"]["environment"]["LANGSMITH_TRACING"] == "false"
    on = load_config("worker", prod_worker_env(), secrets_dir(tmp_path / "on"))
    off = load_config("worker", {**prod_worker_env(), **pt_worker_tracing()}, secrets_dir(tmp_path / "off"))
    assert (tracing_enabled(on), tracing_enabled(off)) == (True, False)


def prod_worker_env() -> dict[str, str]:
    """The prod compose's worker environment, as a prod worker gets it."""
    return {**prod_config()["services"]["worker"]["environment"], "JOBHUNTER_ENV": "prod"}


def pt_worker_tracing() -> dict[str, str]:
    return {"LANGSMITH_TRACING": pt_config()["services"]["worker"]["environment"]["LANGSMITH_TRACING"]}


def secrets_dir(path: Path) -> Path:
    """The worker's secret files with dummy values (the LangSmith key a low-entropy stand-in), mode 0440."""
    path.mkdir(parents=True)
    for name, value in {"db_app": "pw", "data_key": "a" * 43 + "=", "langsmith_key": "lsv2-dummy\n"}.items():
        (path / name).write_text(value)
        (path / name).chmod(0o440)
    return path


def test_no_image_is_a_floating_tag(tmp_path: Path) -> None:
    lock = {}
    for line in (DEPLOY / "images.lock").read_text(encoding="utf-8").splitlines():
        name, _, reference = line.partition("=")
        assert DIGEST_PINNED.fullmatch(reference), line
        lock[name] = reference
    assert lock["versitygw"].startswith("versity/versitygw:v1.9.0@sha256:")
    # Every image of the files is a variable: digest-pinned in images.lock, or one of release.lock's four.
    for name in ("compose.yaml", "compose.test.yaml", "compose.s3stub.yaml"):
        for image in re.findall(r"^\s*image:\s*(.+)$", (DEPLOY / name).read_text(encoding="utf-8"), re.M):
            assert re.fullmatch(r'"\$\{[a-z_]+:\?\}"', image), (name, image)
    release = tmp_path / "release.lock"
    release.write_text(
        "".join(
            f"{var}=ghcr.io/owner/jobhunter-{kind}@sha256:{index * 64}\n"
            for var, kind, index in (
                ("app_image", "app", "1"),
                ("backup_image", "backup", "2"),
                ("docparse_image", "docparse", "3"),
                ("embedder_image", "embedder", "4"),
            )
        )
    )
    # As on the VPS: release.lock after images.lock, then the rest of the environment (compose.test.env's
    # image lines come before it here, so release.lock's override them).
    released = render("jobhunter", ("images.lock", "compose.test.env", str(release)), ("compose.yaml",))
    for config in (released, s3_config()):
        for name, service in config["services"].items():
            assert DIGEST_PINNED.fullmatch(service["image"]), (name, service["image"])
    for name in TEST_SERVICES:
        assert pt_config()["services"][name]["image"] == lock["python"], name


def test_restore_has_its_profile_and_waits_for_postgres() -> None:
    services = prod_config()["services"]
    restore, backup = services["restore"], services["backup"]
    assert restore["profiles"] == ["restore"] and restore["restart"] == "no"
    assert restore["command"] == ["jobhunter", "restore", "run"]
    assert restore["depends_on"] == {"postgres": {"condition": "service_healthy", "required": True}}
    assert backup["command"] == ["jobhunter", "backup", "scheduler"] and "profiles" not in backup
    assert restore["hostname"] == backup["hostname"] == "jobhunter-backup"  # one restic host (R13)
    assert restore["image"] == backup["image"] == "jobhunter-backup:dev"
    plain = subprocess.run(
        [
            *("docker", "compose", "-p", "jobhunter"),
            *("--env-file", str(DEPLOY / "images.lock"), "--env-file", str(DEPLOY / "compose.test.env")),
            *("-f", str(DEPLOY / "compose.yaml"), "config", "--services"),
        ],
        capture_output=True,
        text=True,
        check=True,
        env=ENV,
        timeout=60,
    )
    assert set(plain.stdout.split()) == set(SECRETS) - {"restore"}  # `up` never starts it
    api = services["api"]
    assert api["command"] == [
        *("jobhunter", "api", "--host", ALL_INTERFACES, "--port", "8000"),
        *("--proxy-headers", "--forwarded-allow-ips", "172.30.0.2"),
    ]
    assert api["healthcheck"]["test"] == ["CMD", "jobhunter", "http-check", "http://127.0.0.1:8000/healthz"]
    assert {"start_period", "start_interval"} <= set(api["healthcheck"])  # R19
    probe = services["postgres"]["healthcheck"]["test"]
    assert probe[0] == "CMD-SHELL" and "pg_isready -h 127.0.0.1" in probe[1]  # over TCP


def test_secrets_example_lists_exactly_the_secret_names(tmp_path: Path) -> None:
    lines = [
        line
        for line in (DEPLOY / "secrets.example").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    names = [line.split()[0] for line in lines]
    assert len(names) == len(set(names)) and set(names) == SECRET_NAMES
    assert all(re.fullmatch(r"[a-z0-9_]+\s+# .+", line) for line in lines), "a value in the template"
    for config in (prod_config(), pt_config()):
        declared = {name for name in config["secrets"] if name != "build_ca"}  # the test builds' optional CA
        assert declared == SECRET_NAMES
        for name in SECRET_NAMES:
            assert config["secrets"][name]["file"] == str(REPO / ".secrets/test" / name)
    # Without JOBHUNTER_SECRETS_DIR, as on the VPS: §19.2's directory.
    env = tmp_path / "jobhunter.env"
    test_env = (DEPLOY / "compose.test.env").read_text(encoding="utf-8").splitlines()
    env.write_text("".join(f"{line}\n" for line in test_env if not line.startswith("JOBHUNTER_SECRETS_DIR=")))
    vps = render("jobhunter", ("images.lock", str(env)), ("compose.yaml",))
    assert {name: s["file"] for name, s in vps["secrets"].items()} == {
        name: f"/etc/jobhunter/secrets/{name}" for name in SECRET_NAMES
    }
    script = (REPO / "scripts/test-secrets.sh").read_text(encoding="utf-8")
    looped = {
        word
        for words in re.findall(r"^for name in ([a-z0-9_ ]+); do$", script, re.M)
        for word in words.split()
    }
    created = looped | set(re.findall(r"^create ([a-z0-9_]+) ", script, re.M))
    assert created == SECRET_NAMES  # the dummies of the test stack, every one


def test_every_named_volume_matches_the_owner_table() -> None:
    seen: set[str] = set()
    for config in (prod_config(), pt_config(), s3_config(), dev_config()):
        for name, service in config["services"].items():
            for mount in service.get("volumes", []):
                if mount["type"] != "volume":
                    continue
                volume = mount["source"]
                target, uid, writers, readers = VOLUMES[volume]
                seen.add(volume)
                assert mount["target"] == target, (name, volume)
                if mount.get("read_only", False):
                    assert name in readers, (name, volume)
                    continue
                assert name in writers, (name, volume)
                if uid is not None:
                    assert uid_of(service) == uid, (name, volume)
                elif volume == "parse_jobs":  # the tmpfs's group, by the service's group or its user's
                    assert "10011" in service.get("group_add", []), name
                else:  # caddy: the image's root user owns /data and /config
                    assert "user" not in service, name
    assert seen == set(VOLUMES)
    for config in (prod_config(), dev_config()):
        assert config["volumes"]["parse_jobs"]["driver_opts"] == PARSE_JOBS_OPTS
    exports = {
        name: next(m.get("read_only", False) for m in s["volumes"] if m["source"] == "exports")
        for name, s in prod_config()["services"].items()
        if any(m.get("source") == "exports" for m in s.get("volumes", []))
    }
    assert exports == {"api": True, "worker": False, "migrate": False, "restore": False}  # Deviation 55
    assert s3_config()["services"]["s3stub"]["user"] == "0:0"


def site_headers() -> dict[str, str]:
    """The `header` block of deploy/caddy/site.caddy: its set headers, and `-Server` as `{"-Server": ""}`."""
    text = (DEPLOY / "caddy/site.caddy").read_text(encoding="utf-8")
    block = re.search(r"^header \{\n(.*?)^\}", text, re.M | re.S)
    assert block is not None
    headers = {}
    for line in block.group(1).splitlines():
        name, _, value = line.strip().partition(" ")
        headers[name] = value.strip('"')
    return headers


def test_both_caddyfiles_proxy_with_the_edge_headers_and_log_no_client_address() -> None:
    assert site_headers() == {**SECURITY_HEADERS, "-Server": ""}  # §16.6, as the api's edge mirrors them
    site = (DEPLOY / "caddy/site.caddy").read_text(encoding="utf-8")
    assert "reverse_proxy api:8000 {\n\theader_up X-Forwarded-For {remote_host}\n}" in site  # §16.3
    deletes = ("request>remote_ip", "request>remote_port", "request>client_ip", "request>headers")
    for name in ("Caddyfile", "Caddyfile.test"):
        text = (DEPLOY / name).read_text(encoding="utf-8")
        code = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
        assert "\timport site.caddy" in code, name
        assert not [line for line in code if re.match(r"\s*log\s*(\{|$)", line)], name  # no access log
        assert "\t\tprotocols h1 h2" in code, name  # no HTTP/3 over a TCP-only 443
        for field in deletes:
            assert f"\t\t\t{field} delete" in code, (name, field)
    assert "\temail {$JOBHUNTER_ACME_EMAIL}" in (DEPLOY / "Caddyfile").read_text(encoding="utf-8")
    assert "\ttls internal" in (DEPLOY / "Caddyfile.test").read_text(encoding="utf-8")


class SilentEndpoint(BaseAdapter):
    """An endpoint that never answers, as an unreachable LangSmith whose packets are dropped: a request waits
    until `release` is set, then fails. `sent` is set by the first request; `threads` names who sent each."""

    def __init__(self) -> None:
        super().__init__()
        self.release = threading.Event()
        self.sent = threading.Event()
        self.threads: list[int] = []

    def send(self, request: requests.PreparedRequest, *args: Any, **kwargs: Any) -> requests.Response:
        self.threads.append(threading.get_ident())
        self.sent.set()
        self.release.wait(timeout=60)
        raise requests.ConnectionError("unreachable")

    def close(self) -> None:
        pass


class OneAdapterSession(requests.Session):
    def __init__(self, adapter: BaseAdapter) -> None:
        super().__init__()
        self.adapter = adapter

    def get_adapter(self, url: str) -> BaseAdapter:
        return self.adapter


def test_the_prod_worker_starts_tracing_without_waiting_for_langsmith(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling T21-1 (a): with the prod compose's worker environment, `install_tracing` (worker start, step 7)
    returns at once while LangSmith never answers; the SDK's control thread is the one that waits."""
    endpoint = SilentEndpoint()
    monkeypatch.setattr(requests, "Session", lambda: OneAdapterSession(endpoint))
    configured: list[dict[str, Any]] = []
    monkeypatch.setattr(run_trees, "configure", lambda **kwargs: configured.append(kwargs))
    cfg = load_config("worker", prod_worker_env(), secrets_dir(tmp_path / "secrets"))
    try:
        started = time.monotonic()
        state = install_tracing(cfg)
        elapsed = time.monotonic() - started
        assert state == TracingState(tracing=True, region="eu")
        assert endpoint.sent.wait(timeout=10), "the client never asked LangSmith anything"
        assert elapsed < 2.0, f"install_tracing took {elapsed:.1f} s"
        assert threading.get_ident() not in endpoint.threads  # only the SDK's own thread waited
        [kwargs] = configured
        assert isinstance(kwargs["client"], Client) and kwargs["client"].api_url == LANGSMITH_EU_ENDPOINT
    finally:
        endpoint.release.set()
        for made in configured:
            made["client"].close(timeout=0)
