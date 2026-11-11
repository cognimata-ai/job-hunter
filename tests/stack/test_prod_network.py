"""Network tests N1 and N2 on the compose test stack (spec §16.9, §16.3, §18.2, §22 V20 and V21; plan M5
Task 22, Deviations 35 and 36; Ruling T22-1).

Run explicitly with `-m stack`, with no other stack on 172.30.0.0/24 (the dev stack: `$DC down` first), as a
user who may run `sudo -n nft`:

    JOBHUNTER_STACK_PREBUILT=embedder uv run pytest -m stack -q -p no:cacheprovider \
      tests/stack/test_prod_network.py

N1, no egress:
- `api` (on the internal networks `proxy` and `internal` only) and `docparse` (no network) can neither
  resolve nor connect to an outside address, with the egress rule `deploy/firewall/docker-user.nft` loaded and
  without;
- the rule drops a new outbound connection from a fenced subnet and counts it, and leaves `egress` alone: a
  probe on a routable test bridge, which a test copy of the rule fences, against the worker on `egress`;
- with the rule loaded, the worker reaches `stubsvc` on `egress`, and the outside where
  `JOBHUNTER_N1_OUTSIDE=1` (CI, the VPS). In the dev container outbound traffic goes through the agent proxy,
  so a connection out proves nothing there (V21's dev record);
- a reload of the rule is idempotent.

N2, the real client address: `probe1` (172.30.1.11) and `probe2` (172.30.1.12) on `edge` log in through
Caddy's `edge` address with the rule loaded. The api records each probe's own address in `login_attempts` and
`sessions`, and a spoofed `X-Forwarded-For` changes nothing.

Host changes (M5-U2, Ruling T22-1): the module loads (`sudo -n nft -f`) and deletes the table
`inet jobhunter_egress` and nothing else. A fixture finalizer deletes it after each test, after a failure too,
and the module is skipped where that table exists before it starts (a host with the production rule, which it
must not remove). The test bridge `jobhunter-prodtest-fenced` is removed after its test. The stack is
`prod_session`'s, removed with its volumes at the end.
"""

import json
import os
import re
import time
from collections.abc import Callable, Iterator
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from typing import Any, Final

import pytest

from tests.support.prodstack import CADDY_ROOT, DEPLOY, ORIGIN, PT, caddy_root_ca, prod_session, psql, run

pytestmark = pytest.mark.stack
RULE: Final = DEPLOY / "firewall/docker-user.nft"
NFT: Final = ("sudo", "-n", "nft")
TABLE: Final = "inet jobhunter_egress"
# A routable bridge of this module's own (not `internal`), outside the stack's four subnets, which only the
# test copy of the rule fences.
FENCED_NETWORK: Final = "jobhunter-prodtest-fenced"
FENCED_SUBNET: Final = "172.30.250.0/24"
EGRESS_SUBNET: Final = IPv4Network("172.30.3.0/24")
OUTSIDE_NAME: Final = "example.com"
OUTSIDE: Final = (("192.0.2.1", 443), ("1.1.1.1", 443))  # TEST-NET-1, and a public resolver
NO_ROUTE: Final = "OSError 101"  # ENETUNREACH: the container has no route out at all
STUB_URL: Final = "http://stubsvc:8099/_log"
CONNECT_TIMEOUT_S: Final = 3
WITHIN_S: Final = 60.0
CADDY_EDGE: Final = "172.30.1.2"
PROBES: Final = {"probe1": "172.30.1.11", "probe2": "172.30.1.12"}
SPOOFED: Final = "203.0.113.9"
USERNAME: Final = "prodnet"
PASSWORD: Final = "Kupferkessel-Ahornzweig-4"  # the fixed credential of this module's own user
# Run in a container (`python -I -c NET_SCRIPT '<job>'`): its interfaces; each name of `resolve` resolved; a
# TCP connection to each of `connect`; a GET of each of `fetch`, with no proxy. Prints what it saw as JSON:
# the addresses, `connected` or the status, else the error's class and errno.
NET_SCRIPT: Final = r"""
import json, socket, sys, urllib.request
job = json.loads(sys.argv[1])
seen = {"resolve": {}, "connect": {}, "fetch": {}}
seen["interfaces"] = sorted(name for _, name in socket.if_nameindex())
def failed(error):
    return f"{type(error).__name__} {error.errno}"
for name in job.get("resolve", []):
    try:
        found = socket.getaddrinfo(name, 443, type=socket.SOCK_STREAM)
        seen["resolve"][name] = sorted({info[4][0] for info in found})
    except OSError as error:
        seen["resolve"][name] = failed(error)
for host, port in job.get("connect", []):
    try:
        socket.create_connection((host, port), timeout=job["timeout"]).close()
        seen["connect"][f"{host}:{port}"] = "connected"
    except OSError as error:
        seen["connect"][f"{host}:{port}"] = failed(error)
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
for url in job.get("fetch", []):
    try:
        with opener.open(url, timeout=job["timeout"]) as response:
            seen["fetch"][url] = response.status
    except OSError as error:
        seen["fetch"][url] = failed(error)
print(json.dumps(seen))
"""
# Run in a probe on `edge`: stdin's logins, each on a TLS connection of its own to Caddy's `edge` address as
# `localhost` (the only name `tls internal` certifies), verified against the root given; prints the statuses.
EDGE_LOGINS: Final = r"""
import http.client, json, socket, ssl, sys
job = json.load(sys.stdin)
context = ssl.create_default_context(cadata=job["ca"])
statuses = []
for headers, body in job["logins"]:
    connection = http.client.HTTPSConnection("localhost", 443, context=context, timeout=15)
    connection.sock = context.wrap_socket(
        socket.create_connection((job["caddy"], 443), timeout=15), server_hostname="localhost"
    )
    connection.request(
        "POST", "/api/auth/login", body=json.dumps(body).encode(),
        headers={"Host": "localhost", "Content-Type": "application/json", **headers},
    )
    response = connection.getresponse()
    response.read()
    statuses.append(response.status)
    connection.close()
print(json.dumps(statuses))
"""


def tables() -> set[str]:
    """The host's nft tables, as `<family> <name>`."""
    listed = run([*NFT, "list", "tables"]).stdout.splitlines()
    return {line.removeprefix("table ").strip() for line in listed if line.startswith("table ")}


def load(path: Path) -> None:
    run([*NFT, "-f", str(path)])


def delete_table() -> None:
    """The rule's table deleted, when it is loaded; no other table is touched."""
    if TABLE in tables():
        run([*NFT, "delete", "table", *TABLE.split()])


def without_handles(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: without_handles(item) for key, item in value.items() if key != "handle"}
    if isinstance(value, list):
        return [without_handles(item) for item in value]
    return value


def loaded_rule() -> list[Any]:
    """The table as nft lists it (JSON, without counter values and handles)."""
    listed = json.loads(run([*NFT, "-j", "-s", "list", "table", *TABLE.split()]).stdout)["nftables"]
    return [without_handles(item) for item in listed if "metainfo" not in item]


def dropped() -> int:
    """The packets the rule's drop statement has counted since the table was loaded."""
    listed = json.loads(run([*NFT, "-j", "list", "chain", *TABLE.split(), "forward"]).stdout)["nftables"]
    counts = []
    for item in listed:
        expressions = item.get("rule", {}).get("expr", [])
        if {"drop": None} in expressions:
            counts += [
                expression["counter"]["packets"] for expression in expressions if "counter" in expression
            ]
    [packets] = counts
    return int(packets)


def fenced_copy(directory: Path) -> Path:
    """The rule with `FENCED_SUBNET` added to the subnets its drop statement fences."""
    text, replaced = re.subn(
        r"(ip saddr \{ [^}]*)( \} ip daddr != )", rf"\1, {FENCED_SUBNET}\2", RULE.read_text(encoding="utf-8")
    )
    assert replaced == 1
    copy = directory / "docker-user.fenced.nft"
    copy.write_text(text, encoding="utf-8")
    return copy


def locked_image(name: str) -> str:
    """`name`'s pinned reference in deploy/images.lock."""
    for line in (DEPLOY / "images.lock").read_text(encoding="utf-8").splitlines():
        key, _, reference = line.partition("=")
        if key == name:
            return reference
    pytest.fail(f"deploy/images.lock has no {name} line")


def observed(argv: list[str]) -> dict[str, Any]:
    seen: dict[str, Any] = json.loads(run(argv).stdout.strip().splitlines()[-1])
    return seen


def in_service(service: str, job: dict[str, Any]) -> dict[str, Any]:
    """`NET_SCRIPT` run with `job` in the running container of `service`."""
    return observed([*PT, "exec", "-T", service, "python", "-I", "-c", NET_SCRIPT, json.dumps(job)])


def on_network(network: str, job: dict[str, Any]) -> dict[str, Any]:
    """`NET_SCRIPT` run with `job` in a new, unprivileged python container on `network`."""
    argv = [
        *("docker", "run", "--rm", "--network", network, "--user", "65534:65534", "--read-only"),
        *("--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", locked_image("python")),
        *("python", "-I", "-c", NET_SCRIPT, json.dumps(job)),
    ]
    return observed(argv)


@pytest.fixture(scope="module")
def stack() -> Iterator[None]:
    if TABLE in tables():
        pytest.skip(
            f"the nft table {TABLE} is loaded on this host: this module loads and deletes that table, so it "
            "never runs where it exists (a host with the production rule). Left by a killed run of this "
            f"module: `sudo nft delete table {TABLE}`"
        )
    try:
        with prod_session():
            yield
    finally:
        delete_table()


@pytest.fixture
def rule(stack: None) -> Iterator[Callable[[Path], None]]:
    """Loads a rule file into the host (`sudo -n nft -f`); the table is deleted after the test, after a
    failure too."""
    try:
        yield load
    finally:
        delete_table()


@pytest.fixture
def fenced_network(stack: None) -> Iterator[str]:
    """The routable test bridge on `FENCED_SUBNET`, removed after the test."""
    run(["docker", "network", "rm", FENCED_NETWORK], check=False)  # what a killed run left behind
    run(["docker", "network", "create", "--driver", "bridge", "--subnet", FENCED_SUBNET, FENCED_NETWORK])
    try:
        yield FENCED_NETWORK
    finally:
        run(["docker", "network", "rm", FENCED_NETWORK])


@pytest.fixture(scope="module")
def ca_pem(stack: None) -> str:
    """Caddy's internal root (public), once Caddy has made it at its start."""
    deadline = time.monotonic() + WITHIN_S
    while run([*PT, "exec", "-T", "caddy", "test", "-f", CADDY_ROOT], check=False).returncode != 0:
        assert time.monotonic() < deadline, "Caddy never made its internal root"
        time.sleep(0.5)
    copy = caddy_root_ca(PT)
    try:
        return copy.read_text(encoding="ascii")
    finally:
        copy.unlink()


@pytest.fixture(scope="module")
def user(stack: None) -> None:
    """The single user, made in the running api container by the user CLI (the password on its stdin)."""
    create = [
        *PT,
        "exec",
        "-T",
        "api",
        "jobhunter",
        "user",
        "create",
        "--username",
        USERNAME,
        "--password-stdin",
    ]
    run(create, stdin=f"{PASSWORD}\n")


@pytest.mark.parametrize("loaded", [False, True], ids=["without-the-rule", "with-the-rule"])
def test_api_and_docparse_cannot_resolve_or_connect_out(rule: Callable[[Path], None], loaded: bool) -> None:
    if loaded:
        rule(RULE)
    assert (TABLE in tables()) is loaded
    outside = [f"{host}:{port}" for host, port in OUTSIDE]
    api = in_service(
        "api",
        {
            "resolve": [OUTSIDE_NAME, "stubsvc", "postgres"],
            "connect": [*OUTSIDE, ("postgres", 5432)],
            "timeout": CONNECT_TIMEOUT_S,
        },
    )
    # The control: the script ran in the api, which reaches its own database on `internal`.
    assert isinstance(api["resolve"]["postgres"], list) and api["connect"]["postgres:5432"] == "connected", (
        api
    )
    # No name outside its networks resolves (neither the internet's nor `egress`'s), and nothing outside is
    # routed: the connection fails at once, with no packet sent.
    assert api["resolve"][OUTSIDE_NAME].startswith("gaierror"), api
    assert api["resolve"]["stubsvc"].startswith("gaierror"), api
    assert [api["connect"][target] for target in outside] == [NO_ROUTE] * len(OUTSIDE), api
    docparse = in_service(
        "docparse", {"resolve": [OUTSIDE_NAME], "connect": list(OUTSIDE), "timeout": CONNECT_TIMEOUT_S}
    )
    assert docparse["interfaces"] == ["lo"], docparse
    assert docparse["resolve"][OUTSIDE_NAME].startswith("gaierror"), docparse
    assert [docparse["connect"][target] for target in outside] == [NO_ROUTE] * len(OUTSIDE), docparse


def test_the_rule_drops_new_outbound_from_fenced_subnets(
    rule: Callable[[Path], None], fenced_network: str, tmp_path: Path
) -> None:
    rule(fenced_copy(tmp_path))
    target = {"connect": [OUTSIDE[0]], "timeout": CONNECT_TIMEOUT_S}
    before = dropped()
    probe = on_network(fenced_network, target)
    # Dropped in silence: the connection times out, and the drop statement counted the attempt.
    assert probe["connect"]["192.0.2.1:443"] == "TimeoutError None", probe
    after = dropped()
    assert after > before
    # The same attempt from the worker's `egress` address is not the rule's to drop.
    in_service("worker", target)
    assert dropped() == after


def test_worker_reaches_the_egress_stub(rule: Callable[[Path], None]) -> None:
    rule(RULE)
    worker = in_service("worker", {"resolve": ["stubsvc"], "fetch": [STUB_URL], "timeout": CONNECT_TIMEOUT_S})
    addresses = worker["resolve"]["stubsvc"]
    assert isinstance(addresses, list) and addresses, worker
    assert all(IPv4Address(address) in EGRESS_SUBNET for address in addresses), worker
    assert worker["fetch"][STUB_URL] == 200, worker


@pytest.mark.skipif(
    os.environ.get("JOBHUNTER_N1_OUTSIDE") != "1",
    reason="JOBHUNTER_N1_OUTSIDE=1 (CI, the VPS) only: the dev container's outbound traffic goes through the "
    "agent proxy, so a connection out proves nothing here (V21's dev record)",
)
def test_worker_reaches_outside(rule: Callable[[Path], None]) -> None:
    rule(RULE)
    worker = in_service(
        "worker", {"resolve": [OUTSIDE_NAME], "connect": [("1.1.1.1", 443)], "timeout": CONNECT_TIMEOUT_S}
    )
    assert isinstance(worker["resolve"][OUTSIDE_NAME], list), worker
    assert worker["connect"]["1.1.1.1:443"] == "connected", worker


def test_the_rule_reloads_idempotently(rule: Callable[[Path], None]) -> None:
    others = tables()
    assert TABLE not in others
    rule(RULE)
    first = loaded_rule()
    rule(RULE)  # over the loaded table
    assert loaded_rule() == first
    assert [next(iter(item)) for item in first] == ["table", "chain", "rule", "rule", "rule"]
    assert tables() == {*others, TABLE}  # no other table touched


def test_client_ip_through_caddy(rule: Callable[[Path], None], ca_pem: str, user: None) -> None:
    """V20 on the compose test stack: Caddy replaces the client's X-Forwarded-For with the address it sees,
    and the api trusts that header from Caddy alone (§16.3). A few logins per probe address, well inside the
    edge's limit (10 a minute) and the throttle's lockout (5 failures)."""
    rule(RULE)
    wrong = {"username": USERNAME, "password": "not-the-password"}
    for name in PROBES:
        job = {
            "ca": ca_pem,
            "caddy": CADDY_EDGE,
            "logins": [
                ({"Origin": ORIGIN}, wrong),
                ({"Origin": ORIGIN, "X-Forwarded-For": SPOOFED}, wrong),
                ({"Origin": ORIGIN}, {"username": USERNAME, "password": PASSWORD}),
            ],
        }
        done = run([*PT, "run", "--rm", "-T", name, "python", "-I", "-c", EDGE_LOGINS], stdin=json.dumps(job))
        assert json.loads(done.stdout.strip().splitlines()[-1]) == [401, 401, 204], name
    attempts = psql(PT, "SELECT host(ip), success FROM login_attempts ORDER BY id").split()
    assert attempts == [f"{address}|{flag}" for address in PROBES.values() for flag in ("f", "f", "t")]
    sessions = psql(PT, "SELECT host(ip) FROM sessions ORDER BY created_at").split()
    assert sessions == list(PROBES.values())
