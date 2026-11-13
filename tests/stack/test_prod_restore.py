"""The disaster restore on the compose test stack (plan M5 Task 26; spec §19.5, §22 V22; scenarios C32, C37
and C39; Deviations 46, 55 and 56, Ruling T26-1): secret delivery, a backup set and a later manifest, a VPS
lost and restored from the S3 stand-in, a newer manifest after the restore, a pending retry completed by the
restore's `migrate`, Fable's sequence, and the monthly restore test.

Run explicitly with `-m stack`, with no other stack on 172.30.0.0/24 (the dev stack: `$DC down` first):

    JOBHUNTER_STACK_PREBUILT=embedder uv run pytest -m stack -q -p no:cacheprovider \
      tests/stack/test_prod_restore.py

The scenarios share one sequence, in this module's order. The fixture is
`tests.support.prodstack.prod_session` (the dummy secrets, a fresh S3 stand-in first, then site A, `PT`); site
B (`PB`) comes up only after `$PT down`, since both projects pin the same subnets, and `$PT down` keeps A's
volumes while A's data is needed again. At the end, after a failure too, `PT`, `PB` and the stand-in are
removed with their volumes. One restic repository (`s3:http://s3stub:7070/jobhunter-test/restic`) serves both
sites, as offsite storage outlives a lost VPS: the manifest stream a restore reads is the union of every
`manifests` snapshot any site shipped.

The sites are driven as a user drives them: through Caddy (`https://localhost:8443`, verified against the
site's own internal root), and by the CLI in the containers (`jobhunter backup …`, `jobhunter restore run`,
`jobhunter restore verify`, `migrate`). Where the API has no route for a step (a tombstoning pass, which the
worker's daily schedule opens; a second profile version), a short script runs the app's own function in the
worker container; the fixtures hold no job that mixes Adzuna with another source, so Fable's sequence
makes one by moving an Adzuna posting onto a Greenhouse job before the set (`MIX`). The browser (`Site`), the
dummy user and the readers of a command's output are `tests.support.prodsite`'s, which the drills share. No
real S3 and no paid call: the worker is on the fixtures and the recorded Claude streams. No secret value is
printed: the dummy user's password is no secret, and key ids are hex ids.
"""

import json
import os
from collections.abc import Iterator
from typing import Any, Final

import pytest

from tests.support.prodsite import (
    PASSWORD,
    SEED_INPUTS,
    USERNAME,
    Site,
    backup,
    events,
    in_worker,
    last_json,
    named,
    one,
    wait_for,
)
from tests.support.prodstack import (
    PB,
    PT,
    STACK_SERVICES,
    create_user,
    down_v,
    other_stack_up,
    prod_session,
    psql,
    run,
    s3_down,
    up,
)
from tests.support.profile import SYNTHETIC_PROFILE

pytestmark = pytest.mark.stack

ROLES: Final = ("api", "worker", "migrate", "backup", "restore")
MARKER: Final = "--- startup-check passed ---"
SECRETS_CHECK: Final = (
    f'jobhunter startup-check --role "$1" && echo "{MARKER}" && id -u && id -G && '
    'for f in /run/secrets/*; do stat -c "%n %u:%g %a" "$f"; done'
)
RESTORE_TIMEOUT_S: Final = 1200
FAULT: Final = "privacy.purge.before_file"
# The mixed job the fixtures lack (no Adzuna ad of theirs is another source's too): the first Adzuna posting
# moved onto the first Greenhouse job, which then holds postings of both sources; that job's id.
MIX: Final = """
WITH g AS (SELECT p.job_id FROM postings p JOIN source_instances s ON s.id = p.source_instance_id
  WHERE s.source_id = 'greenhouse' ORDER BY p.id LIMIT 1),
a AS (SELECT p.id FROM postings p JOIN source_instances s ON s.id = p.source_instance_id
  WHERE s.source_id = 'adzuna' ORDER BY p.id LIMIT 1)
UPDATE postings SET job_id = (SELECT job_id FROM g) WHERE id = (SELECT id FROM a) RETURNING job_id
"""
# A second board on the fixtures (Ashby `n8n`), for jobs no earlier run collected.
NEW_BOARD: Final = """
WITH c AS (INSERT INTO watchlist_companies (display_name, company_key, ats_type, board_slug, detection,
  validation_status, validated_at) VALUES ('n8n', 'n8n', 'ashby', 'n8n', 'manual', 'ok', now()) RETURNING id),
i AS (INSERT INTO source_instances (source_id, instance_key, watchlist_company_id)
  SELECT 'ashby', 'n8n', c.id FROM c RETURNING id),
s AS (UPDATE settings_versions v SET payload = jsonb_set(v.payload, '{watchlist}', v.payload->'watchlist'
  || jsonb_build_array(jsonb_build_object('watchlist_company_id', (SELECT id FROM c), 'enabled', true)))
  FROM active_config a WHERE a.id = 1 AND v.id = a.settings_version_id RETURNING v.id)
SELECT (SELECT id FROM i), (SELECT id FROM s)
"""
# A tombstoning pass, as the worker's daily schedule opens one (`job_tombstone.open_batch`): the purge of
# every due root, deferred to the worker; its manifest's id, or null.
TOMBSTONE: Final = """
import asyncio, json
from jobhunter.clock import utcnow
from jobhunter.config import load_config
from jobhunter.db.session import make_engine
from jobhunter.privacy.kinds import job_tombstone
from jobhunter.worker.tasks import defer_in_tx, make_queue_app

async def main():
    cfg = load_config("worker")
    make_queue_app(cfg.dsn("psycopg"))
    engine = make_engine(cfg, pool_size=1)
    now = utcnow()
    try:
        async with engine.begin() as conn:
            batch = await job_tombstone.open_batch(
                conn, cutoff=now - job_tombstone.TOMBSTONE_AFTER, after=0, now=now, defer=defer_in_tx
            )
    finally:
        await engine.dispose()
    print(json.dumps(None if batch is None else batch.purge.manifest_id))

asyncio.run(main())
"""
# A second profile version (the synthetic profile on stdin, another seniority): a ready draft confirmed and
# activated as the route does it; its id.
PROFILE_VERSION: Final = """
import asyncio, json, sys
from sqlalchemy import text
from jobhunter.clock import utcnow
from jobhunter.config import load_config
from jobhunter.db.seed import content_hash
from jobhunter.db.session import make_engine
from jobhunter.llm.schemas.profile import ConfirmedProfile
from jobhunter.profile import activation
from jobhunter.worker.tasks import defer_in_tx, make_queue_app

async def main():
    cfg = load_config("worker")
    make_queue_app(cfg.dsn("psycopg"))
    engine = make_engine(cfg, pool_size=1)
    profile = ConfirmedProfile.model_validate(json.load(sys.stdin)).model_dump_json()
    digest = content_hash(profile)
    try:
        async with engine.begin() as conn:
            cv = (await conn.execute(text(
                "INSERT INTO cv_files (sha256, mime, size_bytes) VALUES (:h, 'text/plain', 1) RETURNING id"
            ), {"h": digest})).scalar_one()
            draft = (await conn.execute(text(
                "INSERT INTO profile_drafts (cv_file_id, status, reviewed_text_sha256, draft_json) "
                "VALUES (:cv, 'ready', :h, CAST(:p AS jsonb)) RETURNING id"
            ), {"cv": cv, "h": digest, "p": profile})).scalar_one()
        async with engine.begin() as conn:
            await conn.execute(text("SELECT 1 FROM active_config WHERE id = 1 FOR UPDATE"))
            status, body = await activation.confirm_draft(
                conn, int(draft), expected_rescore_ack=True, now=utcnow(), defer=defer_in_tx
            )
    finally:
        await engine.dispose()
    print(json.dumps({"status": status, "version": body.get("profile_version_id")}))

asyncio.run(main())
"""
# Every CV file a row names, decrypted with the api's data keys: how many opened (no content is printed).
DECRYPT_CVS: Final = """
import json
import psycopg
from jobhunter import crypto
from jobhunter.config import load_config
from jobhunter.profile.storage import CvStore
cfg = load_config("api")
with psycopg.connect(cfg.dsn("psycopg")) as conn:
    names = [r[0] for r in conn.execute("SELECT storage_path FROM cv_files WHERE storage_path IS NOT NULL")]
store, keys = CvStore(cfg.cv_store_dir), crypto.data_keys(cfg)
print(json.dumps({"named": len(names), "opened": sum(1 for n in names if store.read(n, keys))}))
"""
# The tables whose rows `migrate` must never delete again (C37), counted.
COUNTED: Final = (
    "jobs",
    "postings",
    "posting_observations",
    "job_revisions",
    "profile_versions",
    "cv_files",
    "deletion_manifests",
    "job_tombstones",
    "settings_versions",
    "exports",
)
STATE: dict[str, Any] = {}


# ---- helpers -----------------------------------------------------------------------------------------------


def lose_and_restore(set_id: int | None) -> tuple[Any, list[dict[str, Any]]]:
    """Site A gone; site B's postgres up on an empty `pgdata`, then `$PB --profile restore run --rm restore`:
    what it printed and the events it logged."""
    up(PB, "postgres", build=False)
    command = [*PB, "--profile", "restore", "run", "--rm", "-T", "restore", "jobhunter", "restore", "run"]
    if set_id is not None:
        command += ["--set", str(set_id)]
    done = run(command, timeout=RESTORE_TIMEOUT_S, check=False)
    found = events(done.stderr)
    if done.returncode != 0:
        pytest.fail(f"restore run exited {done.returncode}: {done.stdout}\n{named(found, 'restore_failed')}")
    return last_json(done.stdout), found


def verified(found: list[dict[str, Any]]) -> list[tuple[str, bool]]:
    return [(event["phase"], event["ok"]) for event in named(found, "restore_verified")]


def verify_replayed(argv: list[str], set_id: int) -> Any:
    """`jobhunter restore verify --phase replayed` in a restore container of the site; what it printed."""
    command = [
        *argv,
        "--profile",
        "restore",
        "run",
        "--rm",
        "-T",
        "restore",
        "jobhunter",
        "restore",
        "verify",
    ]
    done = run([*command, "--set", str(set_id), "--phase", "replayed"], timeout=600, check=False)
    return last_json(done.stdout)


def counts(argv: list[str]) -> dict[str, int]:
    sql = "SELECT " + ", ".join(f"(SELECT count(*) FROM {table})" for table in COUNTED)  # noqa: S608
    return dict(zip(COUNTED, (int(n) for n in one(argv, sql).split("|")), strict=True))


def bounds(argv: list[str]) -> dict[str, int]:
    """Per table, the greatest `max_ids` value of every manifest the site records."""
    sql = (
        "SELECT e.key, max(e.value::bigint) FROM deletion_manifests m "
        "CROSS JOIN LATERAL jsonb_each_text(m.scope -> 'max_ids') e GROUP BY e.key"
    )
    return {table: int(n) for table, n in (line.split("|") for line in psql(argv, sql).split())}


def manifest_status(argv: list[str], manifest_id: int) -> str:
    return one(argv, f"SELECT status FROM deletion_manifests WHERE id = {manifest_id}")  # noqa: S608


def status_of(argv: list[str], manifest_uuid: str) -> str:
    """A manifest's status by its stream identity (a restore's replay inserts its row under a new id)."""
    return one(argv, f"SELECT status FROM deletion_manifests WHERE manifest_uuid = '{manifest_uuid}'")  # noqa: S608


def completions_of(argv: list[str], manifest_uuid: str) -> int:
    """How many completed replays a manifest's `replay_log` records (entries with their counts)."""
    return int(
        one(
            argv,
            "SELECT count(*) FROM deletion_manifests m "  # noqa: S608 - a uuid the database wrote
            "CROSS JOIN LATERAL jsonb_array_elements(m.replay_log) e "
            f"WHERE m.manifest_uuid = '{manifest_uuid}' AND e ? 'counts'",
        )
    )


def replay_logs(argv: list[str]) -> str:
    """Every manifest's `replay_log`, by uuid, as the database holds it."""
    return one(
        argv,
        "SELECT coalesce(string_agg(manifest_uuid::text || '=' || replay_log::text, ' ' "
        "ORDER BY manifest_uuid), '') FROM deletion_manifests",
    )


def uuid_of(argv: list[str], manifest_id: int) -> str:
    return one(argv, f"SELECT manifest_uuid FROM deletion_manifests WHERE id = {manifest_id}")  # noqa: S608


# ---- the sequence ------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def stack() -> Iterator[None]:
    if other_stack_up() is None:
        s3_down()  # a stand-in a killed run left: the repository starts empty
    with prod_session():
        backup(PT, "init")
        create_user(PT, USERNAME, PASSWORD)
        yield


def test_v22_secret_delivery_and_startup(stack: None) -> None:
    """V22's first criterion: every service, backup and restore included, passes `startup-check` as 10001 in
    group 10010, reading its secrets from 0440 `root:10010` files."""
    for role in ROLES:
        done = run([*PT, "run", "--rm", "--no-deps", "-T", role, "sh", "-c", SECRETS_CHECK, "check", role])
        lines = done.stdout.split(f"{MARKER}\n", 1)[1].splitlines()
        assert lines[0] == "10001" and "10010" in lines[1].split(), (role, lines[:2])
        modes = {line.split(" ", 1)[1] for line in lines[2:]}
        assert modes == {"0:10010 440"}, (role, modes)


def test_a_backup_before_any_manifest_restores(stack: None) -> None:
    """A set taken before any purge, with a `ready` export, restored into B: no `manifests` snapshot exists
    (an empty stream), both verify phases pass, the export is `expired` and its download answers 410; B is
    removed again and A comes back with its volumes."""
    site = Site(PT)
    export_id = site.ready_export()
    set_id = int(backup(PT, "set")["set_id"])
    run([*PT, "down"], timeout=300)  # A's volumes are kept: A is needed again
    try:
        printed, found = lose_and_restore(None)
        assert (printed["restored"], printed["set_id"]) == (True, set_id)
        assert len(named(found, "manifest_stream_empty")) == 1
        assert verified(found) == [("restored", True), ("replayed", True)]
        [staged] = named(found, "restore_set_staged")
        assert staged["set_id"] == set_id and all(len(key) == 16 for key in staged["data_key_ids"])
        up(PB, *STACK_SERVICES, build=False)
        b = Site(PB)
        assert b.get(f"/api/privacy/exports/{export_id}")["status"] == "expired"
        assert b.export_download(export_id) == (410, "gone")
    finally:
        down_v(PB)
        up(PT, *STACK_SERVICES, build=False)


def test_v22_backup_then_a_later_manifest(stack: None) -> None:
    """On A: a collection on the fixtures, a confirmed profile, an export made `ready` and not yet expired,
    and
    a backup set; then an Adzuna purge, shipped; then (Fable's sequence) the tombstone of a job the purge
    rebuilt, aged through `availability_changed_at`, shipped too."""
    site = Site(PT)
    assert one(PT, SEED_INPUTS).count("|") == 2
    site.collect(include_paid=True)
    assert (
        int(
            one(
                PT,
                "SELECT count(*) FROM postings p JOIN source_instances s ON s.id = "
                "p.source_instance_id WHERE s.source_id = 'adzuna'",
            )
        )
        > 0
    )
    mixed = int(one(PT, MIX).splitlines()[0])  # psql adds the UPDATE tag
    STATE["version"] = site.confirmed_profile()
    STATE["export"] = site.ready_export()
    STATE["set"] = int(backup(PT, "set")["set_id"])

    body = {"source": "adzuna", "confirm": "PURGE ADZUNA", "password": PASSWORD}
    purge = int(site.post("/api/privacy/purge-source", 202, json=body)["manifest_id"])
    wait_for(lambda: manifest_status(PT, purge) == "shipped", within_s=600, what="the Adzuna purge shipped")
    STATE["purge"] = purge
    STATE["purge_uuid"] = uuid_of(PT, purge)

    # Fable's sequence: the mixed job is in the purge's stored affected set and survives it, rebuilt from its
    # Greenhouse posting with no Adzuna posting left; it is then made due for tombstoning.
    affected = psql(
        PT,
        "SELECT jsonb_array_elements_text(scope -> 'affected' -> 'job_ids') FROM deletion_manifests "  # noqa: S608
        f"WHERE id = {purge}",
    ).split()
    assert str(mixed) in affected
    left = one(
        PT,
        "SELECT count(*) FILTER (WHERE s.source_id = 'adzuna'), count(*) FROM postings p "  # noqa: S608
        f"JOIN source_instances s ON s.id = p.source_instance_id WHERE p.job_id = {mixed}",
    )
    assert left.split("|")[0] == "0" and int(left.split("|")[1]) >= 1, left
    STATE["tombstoned"] = mixed
    psql(
        PT,
        "UPDATE jobs SET availability = 'closed', availability_changed_at = now() - interval '200 days' "  # noqa: S608
        f"WHERE id = {mixed}",
    )
    tombstone = in_worker(PT, TOMBSTONE)
    assert isinstance(tombstone, int)
    wait_for(lambda: manifest_status(PT, tombstone) == "shipped", within_s=600, what="the tombstone shipped")
    STATE["tombstone"] = tombstone
    STATE["tombstone_uuid"] = uuid_of(PT, tombstone)


def test_c32_vps_loss_after_a_shipped_deletion(stack: None) -> None:
    """A is lost (`down -v`). B restores the set, taken before the purge, and the stream (every `manifests`
    snapshot): the manifest files, the dump and the CV files land at their exact paths, both verify phases
    pass, and the purge's postconditions hold with Adzuna disabled and `sources.purge_fence_at` set among
    them, re-applied from the stream. B starts; the user logs in, a restored CV decrypts, and the restored
    export, which the Adzuna manifest names, is `purged` by the replay and answers 410 `gone`."""
    down_v(PT)
    printed, found = lose_and_restore(None)
    # Review M3: the set and the data key ids its ciphertexts name (the CV file's), hex ids only, on stdout.
    assert (printed["restored"], printed["set_id"]) == (True, STATE["set"])
    assert printed["data_key_ids"] and all(len(key) == 16 for key in printed["data_key_ids"])
    assert verified(found) == [("restored", True), ("replayed", True)]
    [stream] = named(found, "manifest_stream_restored")
    assert stream["manifests"] >= 2
    [staged] = named(found, "restore_set_staged")
    assert staged["set_id"] == STATE["set"]
    [cvs] = named(found, "restore_cv_files_restored")
    assert cvs["files"] >= 1
    assert named(found, "restore_dump_loaded")[0]["set_id"] == STATE["set"]

    up(PB, *STACK_SERVICES, build=False)
    files = run([*PB, "exec", "-T", "worker", "ls", "/data/manifests", "/data/cv"]).stdout
    assert f"{STATE['purge_uuid']}.json" in files.split()
    stored = one(PB, "SELECT storage_path FROM cv_files WHERE storage_path IS NOT NULL ORDER BY id LIMIT 1")
    assert stored and stored in files.split()
    assert verify_replayed(PB, STATE["set"]) == {"phase": "replayed", "ok": True, "failures": []}
    enabled = one(
        PB,
        "SELECT v.payload -> 'sources' -> 'enabled' ->> 'adzuna' FROM active_config a "
        "JOIN settings_versions v ON v.id = a.settings_version_id WHERE a.id = 1",
    )
    assert enabled == "false"
    assert one(PB, "SELECT purge_fence_at IS NOT NULL FROM sources WHERE id = 'adzuna'") == "t"
    assert status_of(PB, STATE["purge_uuid"]) in ("completed", "shipped")

    b = Site(PB)
    assert in_worker_api(PB, DECRYPT_CVS) == {"named": 1, "opened": 1}
    assert b.get(f"/api/privacy/exports/{STATE['export']}")["status"] == "purged"
    assert b.export_download(STATE["export"]) == (410, "gone")


def in_worker_api(argv: list[str], script: str) -> Any:
    """`script` in the site's api container (the data keys are the api's)."""
    return last_json(run([*argv, "exec", "-T", "api", "python", "-I", "-c", script]).stdout)


def test_purge_then_tombstone_then_restore_on_the_stack(stack: None) -> None:
    """Fable's sequence (backup, source purge with rebuild, tombstone of the rebuilt job, restore): on B both
    manifests are completed by the replay, the tombstoned job is gone with its tombstone recorded, and the
    restore's `migrate` completed (the replayed phase held, above)."""
    job = STATE["tombstoned"]
    assert one(PB, f"SELECT count(*) FROM jobs WHERE id = {job}") == "0"  # noqa: S608
    assert int(one(PB, f"SELECT count(*) FROM job_tombstones WHERE former_job_id = {job}")) >= 1  # noqa: S608
    # Both manifests are newer than the set: the restored database had no row of either, so the replay
    # inserted, replayed and completed each, once.
    for manifest_uuid in (STATE["purge_uuid"], STATE["tombstone_uuid"]):
        assert status_of(PB, manifest_uuid) in ("completed", "shipped")
        assert completions_of(PB, manifest_uuid) == 1, manifest_uuid


def test_c37_newer_manifest_replays_once(stack: None) -> None:
    """On B: new jobs (a board no run collected), a new profile version and a new manifest (a tombstoning
    pass). `$PB run --rm migrate` again deletes nothing; every new id lies above every `max_ids` of the
    restored stream; the new manifest's uuid collides with none."""
    restored = bounds(PB)
    jobs_before = int(one(PB, "SELECT coalesce(max(id), 0) FROM jobs"))
    earlier = set(one(PB, "SELECT string_agg(manifest_uuid::text, ' ') FROM deletion_manifests").split())
    b = Site(PB)
    assert one(PB, NEW_BOARD).count("|") == 1
    b.collect(include_paid=False)
    new_jobs = [int(n) for n in psql(PB, f"SELECT id FROM jobs WHERE id > {jobs_before}").split()]  # noqa: S608
    assert new_jobs
    profile = {**SYNTHETIC_PROFILE, "seniority": "lead"}
    version = in_worker(PB, PROFILE_VERSION, stdin=json.dumps(profile))
    assert version["status"] == 201
    psql(
        PB,
        "UPDATE jobs SET availability = 'closed', availability_changed_at = now() - interval '200 days' "  # noqa: S608
        f"WHERE id = {new_jobs[-1]}",
    )
    manifest = in_worker(PB, TOMBSTONE)
    assert isinstance(manifest, int)
    wait_for(
        lambda: manifest_status(PB, manifest) in ("completed", "shipped"),
        within_s=300,
        what="the tombstone on B",
        argv=PB,
    )
    new_uuid = one(PB, f"SELECT manifest_uuid FROM deletion_manifests WHERE id = {manifest}")  # noqa: S608

    before, logs_before = counts(PB), replay_logs(PB)
    run([*PB, "run", "--rm", "-T", "migrate"], timeout=600)
    assert counts(PB) == before  # it deleted nothing
    # Review M6: nothing was replayed again: every manifest's `replay_log` is as it was, and the two restored
    # manifests still hold exactly one completion each (C37's "replayed once").
    assert replay_logs(PB) == logs_before
    for manifest_uuid in (STATE["purge_uuid"], STATE["tombstone_uuid"]):
        assert completions_of(PB, manifest_uuid) == 1, manifest_uuid
    assert min(new_jobs) > restored.get("jobs", 0)
    assert int(version["version"]) > restored.get("profile_versions", 0)
    assert manifest > restored.get("deletion_manifests", 0)
    assert new_uuid not in earlier
    assert verify_replayed(PB, STATE["set"]) == {"phase": "replayed", "ok": True, "failures": []}


def test_c39_pending_retry_completes_before_verify(stack: None) -> None:
    """On A (new), an Adzuna purge started with `JOBHUNTER_FAULT=privacy.purge.before_file` stops after A0; a
    backup set captures the `executing` row without a file. Restored into B, it is a pending retry: the
    restore's `migrate` completes it and writes its file, and the replayed phase passes only after that."""
    down_v(PB)
    os.environ["JOBHUNTER_FAULT"] = FAULT  # compose.test.yaml passes it to the worker
    try:
        up(PT, *STACK_SERVICES, build=False)
    finally:
        del os.environ["JOBHUNTER_FAULT"]
    create_user(PT, USERNAME, PASSWORD)
    site = Site(PT)
    body = {"source": "adzuna", "confirm": "PURGE ADZUNA", "password": PASSWORD}
    purge = int(site.post("/api/privacy/purge-source", 202, json=body)["manifest_id"])
    wait_for(
        lambda: int(one(PT, "SELECT coalesce(max(attempts), 0) FROM operations WHERE kind = 'purge'")) >= 1,
        within_s=300,
        what="the purge's first attempt",
    )
    row = one(
        PT,
        f"SELECT status, file_sha256 IS NULL, manifest_uuid FROM deletion_manifests WHERE id = {purge}",  # noqa: S608
    )
    status, fileless, manifest_uuid = row.split("|")
    assert (status, fileless) == ("executing", "t")
    set_id = int(backup(PT, "set")["set_id"])
    down_v(PT)

    printed, found = lose_and_restore(set_id)
    assert (printed["restored"], printed["set_id"]) == (True, set_id)
    classified = named(found, "replay_classified")
    assert classified and classified[0]["outcomes"].get("pending_retry") == 1
    assert len(named(found, "replay_pending_completed")) == 1
    assert verified(found) == [("restored", True), ("replayed", True)]
    files = run([*PB, "--profile", "restore", "run", "--rm", "-T", "restore", "ls", "/data/manifests"]).stdout
    assert f"{manifest_uuid}.json" in files.split()
    assert verify_replayed(PB, set_id) == {"phase": "replayed", "ok": True, "failures": []}
    STATE["c39_set"] = set_id


def test_the_monthly_restore_test_succeeds_as_jobhunter_restore(stack: None) -> None:
    """The restore test in B's backup container: the latest set and the stream (every `manifests` snapshot)
    restored into `jobhunter_restore_test` as `jobhunter_restore`, replayed and verified, and dropped; the run
    `succeeded`."""
    up(PB, *STACK_SERVICES, build=False)
    run_id = int(backup(PB, "restore-test")["run_id"])
    row = one(PB, f"SELECT kind, status, error, backup_set_id, report FROM backup_runs WHERE id = {run_id}")  # noqa: S608
    kind, status, error, set_id, report = row.split("|", 4)
    assert (kind, status, error, int(set_id)) == ("restore_test", "succeeded", "", STATE["c39_set"])
    parsed = json.loads(report)
    assert parsed["restored"] == parsed["replayed"] == {"ok": True, "failures": []}
    assert one(PB, "SELECT count(*) FROM pg_database WHERE datname = 'jobhunter_restore_test'") == "0"
    staging = run([*PB, "exec", "-T", "backup", "ls", "-A", "/data/staging"]).stdout.split()
    assert "restore_test" not in staging and "set" not in staging
