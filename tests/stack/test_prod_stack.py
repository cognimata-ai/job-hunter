"""The production compose on the compose test stack (plan M5 Task 21; V22's first criterion; spec §16.3,
§16.6, §16.8, §19.1, §19.2): every role passes its startup assertions under its real uid with the 0440
`root:10010` secret files, the stack comes up healthy, Caddy serves TLS with §16.6's headers and the `__Host-`
session cookie, readiness answers through it and the metrics do not, its logs hold no client address, and the
api's edge limits logins per client address, which it reads from Caddy's X-Forwarded-For. The api serves the
test stack's fictional operator file, mounted read-only into it alone (M5 follow-ups Task F1).

Run explicitly with `-m stack`, with no other stack on 172.30.0.0/24 (the dev stack: `$DC down` first):

    JOBHUNTER_STACK_PREBUILT=embedder uv run pytest -m stack -q -p no:cacheprovider \
      tests/stack/test_prod_stack.py

The module's fixture is `tests.support.prodstack.prod_session`: the dummy secrets
(`sudo scripts/test-secrets.sh`), the S3 stand-in first, then `PT` with `STACK_SERVICES`; at the end, after a
failure too, `PT`, `PB` and the stand-in removed with their volumes. The images it builds are the dev stack's
tags. Clients at known addresses are the probes on `edge` (`probe1` at 172.30.1.11, `probe2` at 172.30.1.12),
which reach Caddy as the internet would; this process reaches it on 127.0.0.1:8443, and both verify its TLS
against Caddy's internal root.
"""

import json
import re
import ssl
import time
from collections.abc import Iterator
from typing import Any, Final

import httpx
import pytest
from jobhunter.api.edge import SECURITY_HEADERS

from tests.support.prodstack import (
    DEPLOY,
    ORIGIN,
    PT,
    caddy_root_pem,
    create_user,
    probe,
    prod_session,
    psql,
    run,
)

pytestmark = pytest.mark.stack
ROLES: Final = {
    "api": "api",
    "worker": "worker",
    "migrate": "migrate",
    "backup": "backup",
    "restore": "restore",
}
USERNAME: Final = "prodtest"
PASSWORD: Final = "Messingglocke-Lindenblatt-7"  # the fixed credential of this module's own user
SESSION_COOKIE: Final = "__Host-jh_session"
PROBES: Final = {"probe1": "172.30.1.11", "probe2": "172.30.1.12"}
WITHIN_S: Final = 60.0
TMP: Final = "/tmp"  # noqa: S108 - the containers' /tmp
GIB: Final = 1024**3
OPERATOR_PATH: Final = "/api/privacy/operator"
OPERATOR_FILE: Final = "/run/config/operator.json"


@pytest.fixture(scope="module")
def stack() -> Iterator[None]:
    with prod_session():
        yield


@pytest.fixture(scope="module")
def ca_pem(stack: None) -> str:
    return caddy_root_pem(PT)


@pytest.fixture(scope="module")
def tls(ca_pem: str) -> ssl.SSLContext:
    """A context that trusts Caddy's internal root only, once Caddy serves `https://localhost:8443`."""
    context = ssl.create_default_context(cadata=ca_pem)
    deadline = time.monotonic() + WITHIN_S
    while True:
        try:
            httpx.get(f"{ORIGIN}/healthz", verify=context, timeout=5)
            return context
        except httpx.TransportError:
            assert time.monotonic() < deadline, "Caddy never answered on 127.0.0.1:8443"
            time.sleep(0.5)


@pytest.fixture(scope="module")
def user(stack: None) -> None:
    create_user(PT, USERNAME, PASSWORD)


def client(context: ssl.SSLContext) -> httpx.Client:
    return httpx.Client(base_url=ORIGIN, verify=context, timeout=15)


def login_body(password: str = PASSWORD) -> dict[str, str]:
    return {"username": USERNAME, "password": password}


def compose_services() -> dict[str, Any]:
    services: dict[str, Any] = json.loads(run([*PT, "--profile", "*", "config", "--format", "json"]).stdout)[
        "services"
    ]
    return services


# The role's startup assertions, then (after a marker, since the check logs to stdout) the uid, the groups and
# each mounted secret file's owner, group and mode.
MARKER: Final = "--- startup-check passed ---"
SECRETS_CHECK: Final = (
    f'jobhunter startup-check --role "$1" && echo "{MARKER}" && id -u && id -G && '
    'for f in /run/secrets/*; do stat -c "%n %u:%g %a" "$f"; done'
)


def test_every_service_passes_startup_check(stack: None) -> None:
    """V22's first criterion: each role's startup assertions pass in its own container, as uid 10001 in group
    10010, reading its secrets, exactly its own, from 0440 `root:10010` files (`backup` and `restore` too,
    without running their commands). The worker's http stand-in URLs pass in the test env (Ruling T21-1
    (a))."""
    services = compose_services()
    for service, role in ROLES.items():
        done = run([*PT, "run", "--rm", "--no-deps", "-T", service, "sh", "-c", SECRETS_CHECK, "check", role])
        lines = done.stdout.split(f"{MARKER}\n", 1)[1].splitlines()
        assert lines[0] == "10001" and "10010" in lines[1].split(), (service, lines[:2])
        mounted = dict(line.split(" ", 1) for line in lines[2:])
        wanted = {f"/run/secrets/{entry['source']}" for entry in services[service]["secrets"]}
        assert set(mounted) == wanted, service
        assert set(mounted.values()) == {"0:10010 440"}, (service, mounted)


def test_the_stack_comes_up_healthy(stack: None) -> None:
    listed = run([*PT, "ps", "--all", "--format", "json"]).stdout.strip()
    # One JSON object per line (or, from an older compose, one array).
    entries = (
        json.loads(listed) if listed.startswith("[") else [json.loads(line) for line in listed.splitlines()]
    )
    states = {entry["Service"]: (entry["State"], entry["Health"], entry["ExitCode"]) for entry in entries}
    assert states == {
        "caddy": ("running", "", 0),
        "api": ("running", "healthy", 0),
        "worker": ("running", "healthy", 0),
        "docparse": ("running", "", 0),
        "stubsvc": ("running", "healthy", 0),
        "backup": ("running", "", 0),  # its scheduler (Task 24); no healthcheck
        "postgres": ("running", "healthy", 0),
        "embedder": ("running", "healthy", 0),
        "migrate": ("exited", "", 0),
    }
    # The worker's /tmp, where the plaintext export zip is written: a tmpfs of 1 GiB (Ruling T21-1 (a)). The
    # exports volume: read-only in the api, writable in the worker (Deviation 55).
    tmp = "import os; s = os.statvfs('/tmp'); print(s.f_blocks * s.f_frsize)"
    assert int(run([*PT, "exec", "-T", "worker", "python", "-c", tmp]).stdout) == GIB
    for service, mode in (("worker", "rw"), ("api", "ro")):
        mounts = run([*PT, "exec", "-T", service, "cat", "/proc/mounts"]).stdout.splitlines()
        by_target = {fields[1]: fields for fields in (line.split() for line in mounts)}
        assert by_target[TMP][2] == "tmpfs", service
        assert by_target["/data/exports"][3].split(",")[0] == mode, service


def test_tls_headers_and_the_session_cookie_through_caddy(tls: ssl.SSLContext, user: None) -> None:
    with client(tls) as http:
        r = http.get("/healthz")
        assert r.status_code == 200
        for name, value in SECURITY_HEADERS.items():  # §16.6: each once, Caddy's copy over the api's
            assert r.headers.get_list(name) == [value], name
        assert "server" not in r.headers and "alt-svc" not in r.headers  # no Server, no HTTP/3 advertised
        login = http.post("/api/auth/login", json=login_body(), headers={"Origin": ORIGIN})
        assert login.status_code == 204, login.text
        [cookie] = login.headers.get_list("set-cookie")
        name, _, rest = cookie.partition("=")
        attributes = {part.strip() for part in rest.split(";")[1:]}
        assert name == SESSION_COOKIE
        assert {"Secure", "HttpOnly", "SameSite=Strict", "Path=/"} <= attributes
        assert not any(part.lower().startswith("domain") for part in attributes)
        session = http.get("/api/auth/session")
        assert session.status_code == 200 and session.json()["username"] == USERNAME


def test_the_api_serves_the_test_operator_file(tls: ssl.SSLContext, user: None) -> None:
    """F1: `deploy/test/operator.json`, mounted read-only at the setting's default in the api and in no other
    service, is what `GET /api/privacy/operator` answers a session through Caddy, and nothing without one."""
    expected = json.loads((DEPLOY / "test/operator.json").read_text(encoding="utf-8"))
    with client(tls) as http:
        assert http.get(OPERATOR_PATH).status_code == 401
        login = http.post("/api/auth/login", json=login_body(), headers={"Origin": ORIGIN})
        assert login.status_code == 204, login.text
        r = http.get(OPERATOR_PATH)
        assert (r.status_code, r.json()) == (200, expected)
    mounts = run([*PT, "exec", "-T", "api", "cat", "/proc/mounts"]).stdout.splitlines()
    by_target = {fields[1]: fields for fields in (line.split() for line in mounts)}
    assert by_target[OPERATOR_FILE][3].split(",")[0] == "ro"
    for service in ("worker", "caddy", "docparse", "backup"):
        found = run([*PT, "exec", "-T", service, "cat", "/proc/mounts"]).stdout
        assert OPERATOR_FILE not in found, service


def test_readyz_through_caddy(tls: ssl.SSLContext) -> None:
    deadline = time.monotonic() + WITHIN_S
    with client(tls) as http:
        while (r := http.get("/readyz")).status_code != 200:
            assert r.status_code == 503, (r.status_code, r.text)
            assert time.monotonic() < deadline, f"readyz never 200 (last: {r.text})"
            time.sleep(1)
    assert r.json() == {}


def test_metrics_are_not_reachable_through_caddy(tls: ssl.SSLContext) -> None:
    """Caddy proxies to the api's port 8000 only; the Prometheus exposition (port 8001 outside the test env)
    is never served on it, whatever the client asks for."""
    with client(tls) as http:
        for path in ("/metrics", "/metrics/", "/api/metrics"):
            for accept in ("*/*", "text/plain", "text/html,application/xhtml+xml"):
                r = http.get(path, headers={"Accept": accept})
                assert "# HELP" not in r.text and "# TYPE" not in r.text, (path, accept)
                assert r.status_code in (200, 404), (
                    path,
                    accept,
                )  # the SPA's page for a navigation, else 404
                if r.status_code == 200:
                    assert r.headers["content-type"] == "text/html; charset=utf-8", (path, accept)


def test_the_login_limit_through_caddy(ca_pem: str, user: None) -> None:
    """Deviation 34: the api's edge counts the logins of each client address it reads from Caddy's header,
    10 a minute: `probe2`'s eleventh is refused, before any Argon2 work; `probe1` is a client of its own."""
    headers = {"Origin": ORIGIN, "Content-Type": "application/json"}
    answers = probe(PT, "probe2", ca_pem, [("POST", "/api/auth/login", headers, login_body())] * 11)
    assert [a["status"] for a in answers] == [204] * 10 + [429]
    refused = answers[-1]
    problem = json.loads(refused["body"])
    assert (problem["code"], problem["title"]) == ("throttled", "Too many logins")
    assert 1 <= problem["retry_after_s"] <= 60
    assert {k.lower(): v for k, v in refused["headers"].items()}["retry-after"] == str(
        problem["retry_after_s"]
    )
    [other] = probe(PT, "probe1", ca_pem, [("POST", "/api/auth/login", headers, login_body())])
    assert other["status"] == 204


def address_pattern(address: str) -> re.Pattern[str]:
    """`address` as a whole address in a text (172.30.1.1 is not found in 172.30.1.11)."""
    return re.compile(rf"(?<![\d.]){re.escape(address)}(?![\d])")


def test_caddy_logs_hold_no_client_address(ca_pem: str, tls: ssl.SSLContext, user: None) -> None:
    """Caddy keeps no access log, and the error lines it does write (here: a 502 while the api is down) carry
    no client address, port or request header (the Caddyfiles' log filter). The addresses checked are those
    the api recorded for this module's logins through Caddy, the probes' among them."""
    headers = {"Origin": ORIGIN, "Content-Type": "application/json"}
    probe(
        PT,
        "probe1",
        ca_pem,
        [
            ("GET", "/healthz", {}, None),
            ("GET", "/no-such-page", {}, None),
            ("POST", "/api/auth/login", headers, login_body("not-the-password-1")),
        ],
    )
    run([*PT, "stop", "api"])
    try:
        [down] = probe(PT, "probe1", ca_pem, [("GET", "/healthz", {"X-Forwarded-For": "203.0.113.77"}, None)])
        assert down["status"] == 502
        with client(tls) as http:
            assert http.get("/healthz").status_code == 502
    finally:
        run([*PT, "up", "-d", "--no-deps", "--no-build", "--wait", "api"], timeout=300)
    logs = run([*PT, "logs", "--no-color", "caddy"]).stdout
    assert '"logger":"http.log.error"' in logs and '"status":502' in logs  # the error path was logged
    recorded = set(psql(PT, "SELECT DISTINCT host(ip) FROM login_attempts").split())
    assert set(PROBES.values()) <= recorded  # the api saw each probe's own address, through Caddy
    for address in {*recorded, "203.0.113.77"}:
        assert not address_pattern(address).search(logs), address
    for field in ('"remote_ip"', '"remote_port"', '"client_ip"', '"headers"'):  # the filter's four
        assert field not in logs, field
