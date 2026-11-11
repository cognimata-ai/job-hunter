"""The egress rule and its loader (spec §16.9; plan M5 Task 22, Deviation 35, R9):
`deploy/firewall/docker-user.nft` fences exactly the networks `deploy/compose.yaml` pins, and
`deploy/firewall/jobhunter-egress.service` loads it before Docker starts.

`test_the_rule_parses` has the kernel check the file (`nft -c -f`, which loads nothing). The check needs
CAP_NET_ADMIN, so it runs only where `nft` exists and this process is root, and is skipped elsewhere: CI runs
tests/unit unprivileged. The rule's effect on traffic is network test N1 (tests/stack/test_prod_network.py).
"""

import configparser
import os
import re
import shutil
import subprocess
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

REPO: Final = Path(__file__).resolve().parents[2]
RULE: Final = REPO / "deploy/firewall/docker-user.nft"
UNIT: Final = REPO / "deploy/firewall/jobhunter-egress.service"
APP_DIR: Final = Path("/opt/jobhunter")  # the checkout on the VPS
COMPOSE_NETWORKS: Final = IPv4Network("172.30.0.0/16")
TABLE: Final = "inet jobhunter_egress"
# The rule's two address statements: who may go out, and who may open nothing outside the compose networks.
ACCEPT: Final = re.compile(r"ip saddr \{ ([^}]*) \} accept")
DROP: Final = re.compile(r"ip saddr \{ ([^}]*) \} ip daddr != (\S+) ct state new counter drop")


def statements() -> list[str]:
    """The rule file's lines without their comments and indentation, the empty ones left out."""
    stripped = (line.split("#", 1)[0].strip() for line in RULE.read_text(encoding="utf-8").splitlines())
    return [line for line in stripped if line]


def addresses(listed: str) -> set[IPv4Network]:
    """An nft set's elements (`a/24, b`), a bare address as its /32."""
    return {IPv4Network(element.strip()) for element in listed.split(",")}


def pinned() -> tuple[dict[str, IPv4Network], IPv4Address]:
    """deploy/compose.yaml's subnet of each network, and Caddy's fixed address on `edge`."""
    compose: dict[str, Any] = yaml.safe_load((REPO / "deploy/compose.yaml").read_text(encoding="utf-8"))
    subnets = {}
    for name, network in compose["networks"].items():
        [config] = network["ipam"]["config"]
        subnets[name] = IPv4Network(config["subnet"])
    return subnets, IPv4Address(compose["services"]["caddy"]["networks"]["edge"]["ipv4_address"])


def test_the_rule_names_every_pinned_subnet_and_caddys_edge_address() -> None:
    subnets, caddy = pinned()
    assert set(subnets) == {"edge", "proxy", "internal", "egress"}
    assert caddy in subnets["edge"]
    lines = statements()
    [accept] = [found for line in lines if (found := ACCEPT.fullmatch(line))]
    [drop] = [found for line in lines if (found := DROP.fullmatch(line))]
    # `egress` and Caddy's `edge` address go out; `proxy`, `internal` and the rest of `edge` open nothing new
    # beyond the compose networks, all of which lie inside the excepted range.
    assert addresses(accept[1]) == {subnets["egress"], IPv4Network(f"{caddy}/32")}
    assert addresses(drop[1]) == {subnets["proxy"], subnets["internal"], subnets["edge"]}
    assert IPv4Network(drop[2]) == COMPOSE_NETWORKS
    assert all(subnet.subnet_of(COMPOSE_NETWORKS) for subnet in subnets.values())
    # Its own table, declared, deleted and defined anew (an idempotent reload), on the forward hook before
    # Docker's chains (priority 0); replies pass first, then who may go out, then the drop.
    assert lines == [
        f"table {TABLE}",
        f"delete table {TABLE}",
        f"table {TABLE} {{",
        "chain forward {",
        "type filter hook forward priority -10; policy accept;",
        "ct state established,related accept",
        accept[0],
        drop[0],
        "}",
        "}",
    ]


@pytest.mark.skipif(
    shutil.which("nft") is None or os.geteuid() != 0,
    reason="`nft -c` needs CAP_NET_ADMIN: it runs where nft exists and this process is root (CI runs "
    "tests/unit unprivileged)",
)
def test_the_rule_parses() -> None:
    done = subprocess.run(
        ["nft", "-c", "-f", str(RULE)], capture_output=True, text=True, timeout=30, check=False
    )
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")


class _Unit(configparser.ConfigParser):
    """A systemd unit read as INI, its keys' case kept."""

    def optionxform(self, optionstr: str) -> str:
        return optionstr


def test_the_unit_runs_before_docker() -> None:
    unit = _Unit(interpolation=None)
    unit.read_string(UNIT.read_text(encoding="utf-8"))
    assert unit.sections() == ["Unit", "Service", "Install"]
    assert set(unit["Unit"]) == {"Description", "Before"}
    assert unit["Unit"]["Before"] == "docker.service"
    assert dict(unit["Service"]) == {
        "Type": "oneshot",
        "RemainAfterExit": "yes",
        "ExecStart": f"/usr/sbin/nft -f {APP_DIR}/deploy/firewall/docker-user.nft",
        "ExecStop": f"/usr/sbin/nft delete table {TABLE}",
    }
    assert dict(unit["Install"]) == {"WantedBy": "multi-user.target"}
    # It loads this checkout's rule (at the checkout's path on the VPS) and deletes that rule's table.
    loaded = Path(unit["Service"]["ExecStart"].split()[-1])
    assert REPO / loaded.relative_to(APP_DIR) == RULE
    assert f"table {TABLE} {{" in statements()
