"""The backup service on the compose test stack against the S3 stand-in (plan M5 Task 24; spec §19.5;
Deviation 29, R13, Ruling T24-1 (f)): `jobhunter backup init` against versitygw, a backup set with the real
`pg_dump`, `pg_restore` and restic, the service's scheduler shipping a new manifest within 90 seconds, and the
prune, which forgets the set snapshots beyond its counts and the `manifests` snapshots strictly older than
the latest receipt's (final fix C1), by id on the real restic, the receipted one holding every file a
forgotten one held.

Run explicitly with `-m stack`, with no other stack on 172.30.0.0/24 (the dev stack: `$DC down` first):

    JOBHUNTER_STACK_PREBUILT=embedder uv run pytest -m stack -q -p no:cacheprovider \
      tests/stack/test_prod_backup.py

The module's fixture is `tests.support.prodstack.prod_session`: the dummy secrets, the S3 stand-in first
(its volume `s3data` made fresh: a stand-in a killed run left is removed first, with its repository), then
`PT` with `STACK_SERVICES`, `backup` among them (its command is `jobhunter backup scheduler`;
compose.test.yaml ships every minute); at the end, after a failure too, `PT`, `PB` and the stand-in removed
with their volumes. No real S3 and no paid call: the repository is
`s3:http://s3stub:7070/jobhunter-test/restic`. Restic's snapshots are read in the backup container through the
app's own wrapper (`Restic.snapshots`), which reads the password and the S3 keys from the container's secret
files; ids, tags and paths only are printed.
"""

import json
import time
from collections.abc import Iterator
from typing import Any, Final

import pytest

from tests.support.manifest_file import WRITE_MANIFEST, written_manifest
from tests.support.prodstack import PT, logs_tail, other_stack_up, prod_session, psql, run, s3_down

pytestmark = pytest.mark.stack
SHIP_WITHIN_S: Final = 90.0
POLL_S: Final = 2.0
# The snapshots carrying every tag of argv, as `Restic.snapshots` lists them: ids, tags and paths.
SNAPSHOTS: Final = """
import asyncio, json, sys
from jobhunter.backup.restic import Restic
from jobhunter.backup.runner import SubprocessRunner
from jobhunter.config import load_config
infos = asyncio.run(Restic(load_config("backup"), SubprocessRunner()).snapshots(tags=sys.argv[1:]))
print(json.dumps([{"id": i.id, "tags": list(i.tags), "paths": list(i.paths)} for i in infos]))
"""


def last_json(stdout: str) -> Any:
    """The JSON line a command printed last on stdout."""
    return json.loads([line for line in stdout.splitlines() if line.strip()][-1])


def backup(command: str, **env: str) -> Any:
    """`jobhunter backup <command>` in the running backup container (beside its scheduler), with `env` added;
    what it printed."""
    extra = [arg for name, value in env.items() for arg in ("-e", f"{name}={value}")]
    return last_json(run([*PT, "exec", "-T", *extra, "backup", "jobhunter", "backup", command]).stdout)


def snapshots(*tags: str) -> list[dict[str, Any]]:
    """The repository's snapshots carrying every one of `tags`, oldest first."""
    listed: list[dict[str, Any]] = last_json(
        run([*PT, "exec", "-T", "backup", "python", "-I", "-c", SNAPSHOTS, *tags]).stdout
    )
    return listed


def new_manifest() -> str:
    """A manifest's file in the `manifests` volume, written by the worker as a purge writes it
    (`tests.support.manifest_file`); its uuid."""
    writer = run([*PT, "exec", "-T", "worker", "python", "-I", "-c", WRITE_MANIFEST, "worker"])
    manifest_uuid, _, _ = written_manifest(writer.stdout)
    return manifest_uuid


@pytest.fixture(scope="module")
def stack() -> Iterator[None]:
    if other_stack_up() is None:
        s3_down()  # a stand-in a killed run left: the repository starts empty, on a fresh `s3data`
    with prod_session():
        yield


@pytest.fixture(scope="module")
def initialised(stack: None) -> list[Any]:
    """`jobhunter backup init`, twice: what each printed. The repository exists for the other tests."""
    return [backup("init") for _ in range(2)]


def test_restic_init_against_the_s3_stand_in(initialised: list[Any]) -> None:
    # Created on the fresh stand-in, then found: an existing repository is never initialised again.
    assert initialised == [{"created": True}, {"created": False}]
    assert snapshots() == []


def test_a_backup_set_succeeds_against_s3(initialised: list[Any]) -> None:
    printed = backup("set")
    set_id = int(printed["set_id"])
    row = psql(
        PT,
        "SELECT status, restic_snapshot_id, dump_bytes, manifest_count, row_counts -> 'backup_sets', "  # noqa: S608 - an int
        f"error FROM backup_sets WHERE id = {set_id}",
    )
    status, snapshot_id, dump_bytes, manifest_count, own_rows, error = row.strip().split("|")
    assert (status, error) == ("succeeded", ""), row
    assert int(dump_bytes) > 0 and int(manifest_count) >= 0 and int(own_rows) >= 1
    [listed] = snapshots(f"set:{set_id}")
    assert listed["id"] == snapshot_id
    assert sorted(listed["tags"]) == ["set", f"set:{set_id}"]
    # The staged set, the CV files and the manifests; restic's cache in the staging volume stays out.
    assert sorted(listed["paths"]) == ["/data/cv", "/data/manifests", "/data/staging/set"]
    staging = run([*PT, "exec", "-T", "backup", "ls", "-A", "/data/staging"]).stdout.split()
    assert ".restic-cache" in staging and "set" not in staging, staging


def test_the_scheduler_ships_within_90_seconds(initialised: list[Any]) -> None:
    manifest_uuid = new_manifest()
    deadline = time.monotonic() + SHIP_WITHIN_S
    query = (
        "SELECT s.restic_snapshot_id FROM manifest_shipment_items i "  # noqa: S608 - a uuid in canonical form
        "JOIN manifest_shipments s ON s.id = i.shipment_id "
        f"WHERE i.manifest_uuid = '{manifest_uuid}' ORDER BY s.id LIMIT 1"
    )
    while not (shipped := psql(PT, query).strip()):
        if time.monotonic() >= deadline:
            pytest.fail(
                f"no shipment of {manifest_uuid} within {SHIP_WITHIN_S:g} s:\n{logs_tail(PT, 'backup')}"
            )
        time.sleep(POLL_S)
    assert shipped in [snapshot["id"] for snapshot in snapshots("manifests")]


def test_prune_runs(initialised: list[Any]) -> None:
    # Two more receipted `manifests` snapshots: one new manifest a shipment, whether this ships it or the
    # scheduler already did (a second shipper finds the stream shipped and makes no snapshot).
    for _ in range(2):
        new_manifest()
        backup("ship")
    manifests = snapshots("manifests")  # oldest first
    latest = "SELECT restic_snapshot_id FROM manifest_shipments ORDER BY id DESC LIMIT 1"
    receipted = psql(PT, latest).strip()
    at = [snapshot["id"] for snapshot in manifests].index(receipted)
    older = manifests[:at]
    assert older, manifests
    for _ in range(2):
        backup("set")
    sets = snapshots("set")
    assert len(sets) >= 2

    keep_one = {name: "1" for name in ("KEEP_DAILY", "KEEP_WEEKLY", "KEEP_MONTHLY")}
    printed = backup("prune", **{f"JOBHUNTER_BACKUP_{name}": value for name, value in keep_one.items()})

    run_id = int(printed["run_id"])
    row = psql(PT, f"SELECT kind, status, report FROM backup_runs WHERE id = {run_id}")  # noqa: S608 - an int
    kind, status, report = row.strip().split("|")
    assert (kind, status) == ("prune", "succeeded"), row
    keeps = {"keep_daily": 1, "keep_weekly": 1, "keep_monthly": 1}
    assert json.loads(report) == {**keeps, "manifests_forgotten": len(older)}
    # One of each period kept: the newest set; every older one forgotten.
    assert [snapshot["id"] for snapshot in snapshots("set")] == [sets[-1]["id"]]
    # The latest receipt's `manifests` snapshot and every newer one kept, every older one forgotten by id, and
    # the receipted one holds every file a forgotten one held.
    kept = snapshots("manifests")
    assert [snapshot["id"] for snapshot in kept] == [snapshot["id"] for snapshot in manifests[at:]]
    assert all(set(old["paths"]) <= set(kept[0]["paths"]) for old in older)
