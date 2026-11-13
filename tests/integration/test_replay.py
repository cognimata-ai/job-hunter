"""Migrate step 7, the two-phase replay of the deletion-manifest stream (spec §19.5 "Replay rules", §19.3 step
7, §19.4 `manifests replay`; plan M5 Task 25, Deviations 16, 53, 55 and 56): `privacy.replay`.

`classify_stream` reads the stream as step 1 reads it (one strict reader) beside the rows the database holds,
and gives each manifest its row of §19.5's table in `manifest_uuid` order; a completed or shipped manifest is
`satisfied` while every postcondition holds (and no fence of it stands), `checked` (replayed again)
otherwise. `replay_stream` runs phase A (fences made active, the kinds' admissions re-applied, pending
retries completed, every destructive operation in stream order), phase B (one rebuild over the stored
affected jobs) and phase C (one transaction: fences released, every postcondition checked, the manifests
completed and their `replay_log` appended).

The database a replay works on here is the shared one, as a backup set would have captured it: a purge
admitted and recorded by the worker's handler, then stopped by an injected crash (`tests.support.faults`),
so its manifest is `executing` with its file in the stream. Each `migrate` runs on the test's own stream and
volumes (`tests.support.purges`' directories), as `jobhunter manifests replay` does (steps 1, 3 and 7). The
typed scenarios after a real restore are `test_typed_replay.py`'s. Every manifest and row is invented.
"""

import asyncio
import hashlib
import os
import uuid
from collections.abc import AsyncIterator, Iterable, Mapping
from pathlib import Path
from typing import Any, Final, cast

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db.migrate import StepGroup, run_migrate_async
from jobhunter.db.session import make_null_engine
from jobhunter.db.transitions import transition
from jobhunter.ids import uuid7
from jobhunter.privacy import purge, replay
from jobhunter.privacy.fences import FenceSpec, insert_fence
from jobhunter.privacy.identities import rewrite_missing_files
from jobhunter.privacy.kinds import source_purge
from jobhunter.privacy.kinds.golden_case import request_golden_case_delete
from jobhunter.privacy.manifests import (
    FileRoots,
    ManifestBoundMissing,
    ManifestConflict,
    ManifestContent,
    ManifestInvalid,
    ManifestKind,
    ManifestRow,
    Operation,
    canonical_bytes,
    check_postconditions,
    complete_manifest,
    insert_manifest,
    manifest_file_name,
    max_identity_ids,
    read_manifest,
    record_manifest,
    write_manifest_file,
)
from jobhunter.privacy.replay import Classified, ReplayFailed, classify_stream
from jobhunter.settings.active import lock_active
from jobhunter.settings.save import SaveRequest, save_settings
from jobhunter.worker.tasks import defer_in_tx
from sqlalchemy.ext.asyncio import AsyncEngine
from structlog.testing import capture_logs

from tests.support.collect import JobWithPostings, add_posting
from tests.support.faults import PausePoints, SimulatedCrash, no_fault, raising_at
from tests.support.fixtures import ConfigFactory
from tests.support.observations import stated
from tests.support.pg import PgServer, table_counts
from tests.support.purges import (
    ALERT,
    EXPORT,
    IS_FENCED,
    JOB,
    JOB_EXISTS,
    PROVENANCE_SOURCES,
    Purger,
    completed,
    completions,
    one,
    recomputed,
    request_source_purge,
    scalar,
    seeded,
    stopped_at,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("current_epoch")]

ADZUNA: Final = "adzuna"
# `jobhunter manifests replay`: steps 1, 3 and 7 (§19.4).
REPLAY_STEPS: Final[frozenset[StepGroup]] = frozenset({"sequences", "replay"})
_FENCES: Final = sa.text(
    "SELECT kind, job_ids, released_at FROM purge_fences WHERE manifest_id = :m ORDER BY id"
)
_MANIFEST: Final = sa.text(
    "SELECT status, completed_at, replay_log, file_sha256, scope FROM deletion_manifests WHERE id = :m"
)
_LINKS: Final = sa.text(
    "SELECT (SELECT count(*) FROM postings p JOIN source_instances si ON si.id = p.source_instance_id "
    "WHERE p.job_id = :j AND si.source_id = 'adzuna') AS postings, (SELECT count(*) FROM job_revisions r "
    "JOIN job_revision_sources s ON s.job_revision_id = r.id WHERE r.job_id = :j AND s.source_id = 'adzuna') "
    "AS lineage"
)
POSTING_EXISTS: Final = sa.text("SELECT EXISTS (SELECT 1 FROM postings WHERE id = :p)")
OBSERVATION_EXISTS: Final = sa.text("SELECT EXISTS (SELECT 1 FROM posting_observations WHERE id = :o)")
ACTIVE_PAYLOAD: Final = sa.text(
    "SELECT v.payload FROM active_config a JOIN settings_versions v ON v.id = a.settings_version_id "
    "WHERE a.id = 1"
)
_SHIPMENT: Final = sa.text("INSERT INTO manifest_shipments (restic_snapshot_id) VALUES ('snap') RETURNING id")
_MANIFEST_ROWS: Final = sa.text("SELECT count(*) FROM deletion_manifests")
_HASH_ONLY: Final = sa.text("UPDATE deletion_manifests SET file_sha256 = :sha WHERE id = :m")


# ---- helpers -----------------------------------------------------------------------------------------------


def content(
    manifest_uuid: str | None = None,
    *,
    kind: ManifestKind = "golden_case_delete",
    operations: Iterable[Operation] = (),
    max_ids: Mapping[str, int] | None = None,
) -> ManifestContent:
    """An invented manifest's content of format 1."""
    stamp = utcnow().isoformat()
    built: dict[str, Any] = {
        "format": 1,
        "manifest_uuid": manifest_uuid or str(uuid7()),
        "kind": kind,
        "admitted_at": stamp,
        "fenced_at": stamp,
        "max_ids": dict(max_ids or {}),
        "operations": list(operations),
    }
    return built  # type: ignore[return-value]  # the TypedDict's keys are built above


def alerts_named(*alert_ids: int) -> list[Operation]:
    """A `row_delete` of the alerts named: its postcondition holds while none of them exists."""
    return [{"op": "row_delete", "table": "alerts", "ids": list(alert_ids), "bound": {"by": "id"}}]


def cv_file_named(key: str) -> list[Operation]:
    return [{"op": "file_remove", "volume": "cv_store", "keys": [key]}]


async def recorded_row(
    engine: AsyncEngine, operations: list[Operation] | None, *, status: str = "executing"
) -> ManifestRow:
    """A golden-case deletion's manifest row: `executing` with no content when `operations` is `None`, else
    its content of `operations` recorded with its file's hash (A3), then completed or shipped as `status`
    says; its file is written nowhere."""
    async with engine.begin() as conn:
        row = await insert_manifest(conn, kind="golden_case_delete", request={}, now=utcnow())
        if operations is not None:
            built = content(str(row.manifest_uuid), operations=operations)
            await record_manifest(conn, row, built, hashlib.sha256(canonical_bytes(built)).digest())
        if status in ("completed", "shipped"):
            await complete_manifest(conn, row.id)
        if status == "shipped":
            shipment = (await conn.execute(_SHIPMENT)).scalar_one()
            await transition(
                conn, "deletion_manifests", row.id, ["completed"], "shipped", set_={"shipment_id": shipment}
            )
        found = await read_manifest(conn, row.id)
    assert found is not None
    return found


async def alert(engine: AsyncEngine) -> int:
    async with engine.begin() as conn:
        return int((await conn.execute(ALERT, {"key": f"replay:{uuid7()}"})).scalar_one())


async def gone_alert(engine: AsyncEngine) -> int:
    """The id of an alert that existed and is deleted: a `row_delete` naming it holds."""
    alert_id = await alert(engine)
    async with engine.begin() as conn:
        await conn.execute(sa.text("DELETE FROM alerts WHERE id = :a"), {"a": alert_id})
    return alert_id


async def fences(engine: AsyncEngine, manifest_id: int) -> list[Any]:
    async with engine.connect() as conn:
        return list((await conn.execute(_FENCES, {"m": manifest_id})).all())


async def migrate(make_config: ConfigFactory, purger: Purger, **flags: Any) -> list[str]:
    """`jobhunter manifests replay` on the shared database, with this test's stream and volumes."""
    return list(
        await run_migrate_async(
            make_config("migrate"),
            now=utcnow(),
            steps=flags.pop("steps", REPLAY_STEPS),
            manifest_dir=flags.pop("manifest_dir", purger.cfg.manifests_dir),
            cv_dir=purger.cfg.cv_store_dir,
            exports_dir=purger.cfg.exports_dir,
            **flags,
        )
    )


async def assert_replayed(engine: AsyncEngine, purger: Purger, manifest_id: int, *, entries: int = 1) -> None:
    """The manifest `completed`, every fence of it released in the completing transaction, every
    postcondition holding, and `entries` replays logged."""
    row = await one(engine, _MANIFEST, m=manifest_id)
    assert row.status == "completed" and len(completions(row.replay_log)) == entries
    released = {fence.released_at for fence in await fences(engine, manifest_id)}
    assert released == {row.completed_at}  # one transaction: one now()
    async with engine.connect() as conn:
        manifest = await read_manifest(conn, manifest_id)
        assert manifest is not None and manifest.scope is not None
        assert await check_postconditions(conn, manifest.scope, files=purger.file_roots) == []
        assert await source_purge.SourcePurge().postconditions(conn, manifest.scope) == []
    entry = completions(row.replay_log)[-1]
    assert set(entry) == {"restore_id", "at", "counts"} and uuid.UUID(entry["restore_id"])


@pytest.fixture
async def migrator(migrated: PgServer) -> AsyncIterator[AsyncEngine]:
    engine = make_null_engine(migrated.dsn("migrator", driver="sqlalchemy"))
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def stream(tmp_path: Path) -> Path:
    directory = tmp_path / "stream"
    directory.mkdir(mode=0o700)
    return directory


# ---- the classification ------------------------------------------------------------------------------------


async def test_the_classification_table(
    engine: AsyncEngine, migrator: AsyncEngine, stream: Path, tmp_path: Path
) -> None:
    """Each row of §19.5's table, in `manifest_uuid` order: no row (newer than the dump) and an `executing`
    row with its file are `replayed`; an `executing` row without its file is a `pending_retry`, with its hash
    (its content in `scope`) or without; a `completed` or `shipped` row whose postconditions hold is
    `satisfied`, its file in the stream or only in `scope` (step 1 then rewrites it, and it stays satisfied);
    one whose postcondition fails, one with a fence still standing and one with a file the volume holds again
    are `checked`; without the volumes no file removal can be verified, so that one is `checked` too."""
    gone, present = await gone_alert(engine), await alert(engine)
    roots = FileRoots(cv_store=tmp_path / "cv", exports=tmp_path / "exports")
    roots.cv_store.mkdir()
    roots.exports.mkdir()
    (roots.cv_store / "back-again.bin").write_bytes(b"restored")

    newer = content(operations=alerts_named(gone))
    write_manifest_file(stream, newer)
    executing = await recorded_row(engine, alerts_named(gone))
    assert executing.scope is not None
    write_manifest_file(stream, executing.scope)
    no_hash = await recorded_row(engine, None)
    hash_only = await recorded_row(engine, alerts_named(gone))
    holds = await recorded_row(engine, alerts_named(gone), status="completed")
    assert holds.scope is not None
    write_manifest_file(stream, holds.scope)
    unshipped_file = await recorded_row(engine, alerts_named(gone), status="shipped")
    fails = await recorded_row(engine, alerts_named(present), status="completed")
    fenced = await recorded_row(engine, alerts_named(gone), status="completed")
    async with engine.begin() as conn:
        await insert_fence(conn, manifest_id=fenced.id, spec=FenceSpec(kind="jobs", job_ids=(1,)))
    file_back = await recorded_row(engine, cv_file_named("back-again.bin"), status="completed")
    file_gone = await recorded_row(engine, cv_file_named("gone.bin"), status="completed")

    expected = [
        Classified(uuid.UUID(newer["manifest_uuid"]), "replayed"),
        Classified(executing.manifest_uuid, "replayed"),
        Classified(no_hash.manifest_uuid, "pending_retry"),
        Classified(hash_only.manifest_uuid, "pending_retry"),
        Classified(holds.manifest_uuid, "satisfied"),
        Classified(unshipped_file.manifest_uuid, "satisfied"),
        Classified(fails.manifest_uuid, "checked"),
        Classified(fenced.manifest_uuid, "checked"),
        Classified(file_back.manifest_uuid, "checked"),
        Classified(file_gone.manifest_uuid, "satisfied"),
    ]
    async with migrator.connect() as conn:
        assert await classify_stream(conn, stream, files=roots) == expected
        # Without the volumes, a file removal cannot be verified: replayed again, never assumed done.
        unverified = await classify_stream(conn, stream)
    assert unverified[-1] == Classified(file_gone.manifest_uuid, "checked")
    assert unverified[:-1] == expected[:-1]

    # Step 1 writes the files the stream lacks (the hash-only executing one and the completed ones): the
    # executing one is then an executing row with its file, `replayed`; the completed ones are unchanged.
    async with migrator.begin() as conn:
        await rewrite_missing_files(conn, stream)
    async with migrator.connect() as conn:
        after_step_1 = await classify_stream(conn, stream, files=roots)
    assert after_step_1 == [
        Classified(c.manifest_uuid, "replayed" if c.manifest_uuid == hash_only.manifest_uuid else c.outcome)
        for c in expected
    ]


@pytest.mark.parametrize("defect", ["not_json", "unknown_format", "name_mismatch", "missing_directory"])
async def test_a_bad_file_stops_migrate(
    engine: AsyncEngine, make_config: ConfigFactory, purger: Purger, tmp_path: Path, defect: str
) -> None:
    """A file of the stream that does not parse, or whose name differs from its uuid, stops step 7 before
    anything is replayed (`ManifestInvalid`, `migrate_failed` naming the step), and so does a stream
    directory that does not exist: the valid manifest beside it gets no row and no fence."""
    stream = purger.cfg.manifests_dir
    gone = await gone_alert(engine)
    valid = content(operations=alerts_named(gone))
    write_manifest_file(stream, valid)
    bad = content(operations=alerts_named(gone))
    if defect == "not_json":
        (stream / manifest_file_name(bad["manifest_uuid"])).write_bytes(b"{not json")
    elif defect == "unknown_format":
        other = canonical_bytes(bad).replace(b'"format":1', b'"format":2')
        (stream / manifest_file_name(bad["manifest_uuid"])).write_bytes(other)
    elif defect == "name_mismatch":
        (stream / manifest_file_name(uuid7())).write_bytes(canonical_bytes(bad))
    files = sorted(os.listdir(stream))
    manifest_dir = tmp_path / "no-such-stream" if defect == "missing_directory" else stream

    with capture_logs() as logs, pytest.raises((ManifestInvalid, FileNotFoundError)) as stopped:
        await migrate(make_config, purger, steps=frozenset({"replay"}), manifest_dir=manifest_dir)
    if defect == "missing_directory":
        assert isinstance(stopped.value, FileNotFoundError)
    else:
        assert isinstance(stopped.value, ManifestInvalid) and stopped.value.reason == defect
    (failed,) = [e for e in logs if e["event"] == "migrate_failed"]
    assert (failed["step"], failed["error_type"]) == ("replay", type(stopped.value).__name__)
    assert await scalar(engine, _MANIFEST_ROWS) == 0  # nothing replayed: no row inserted, no fence
    assert await scalar(engine, sa.text("SELECT count(*) FROM purge_fences")) == 0
    assert sorted(os.listdir(stream)) == files


_NONE: Final = 2**40  # an id no row has


def bounded_by(kind: ManifestKind) -> list[Operation]:
    """Operations of `kind` that make its procedure read its bounds when replayed (each names rows no
    database holds)."""
    if kind == "job_tombstone":
        return [
            {"op": "fence", "kind": "jobs", "key": "jobs", "job_ids": [_NONE]},
            {"op": "row_delete", "table": "jobs", "ids": [_NONE], "bound": {"by": "id"}},
        ]
    if kind == "profile_purge":
        return [
            {"op": "fence", "kind": "profile", "key": str(_NONE), "job_ids": []},
            {
                "op": "row_delete",
                "table": "score_targets",
                "ids": [_NONE],
                "bound": {"by": "parent", "table": "llm_tuples", "column": "llm_tuple_id"},
            },
        ]
    if kind == "source_purge":
        return [
            {"op": "row_delete", "table": "postings", "selector": "source:adzuna", "bound": {"by": "id"}},
            {"op": "fence", "kind": "source", "key": ADZUNA, "job_ids": []},
        ]
    if kind == "delete_everything":
        return [
            {"op": "row_delete", "table": "llm_calls", "selector": "all", "bound": {"by": "id"}},
            {"op": "row_delete", "table": "settings_versions", "selector": "all", "bound": {"by": "id"}},
        ]
    return [{"op": "row_delete", "table": "golden_cases", "ids": [_NONE], "bound": {"by": "id"}}]


@pytest.mark.parametrize(
    ("kind", "missing"),
    [
        ("job_tombstone", "score_targets"),
        ("profile_purge", "score_targets"),
        ("source_purge", "posting_observations"),
        ("delete_everything", "llm_spend_ledger"),
        ("golden_case_delete", None),
    ],
)
async def test_a_manifest_whose_max_ids_lacks_a_key_its_kind_reads_stops_migrate(
    engine: AsyncEngine, make_config: ConfigFactory, purger: Purger, kind: ManifestKind, missing: str | None
) -> None:
    """Final review M3, M8 (parked item 6): a manifest of the stream whose `max_ids` lacks a key its kind
    reads (a release reading a table an older manifest never bounded) stops step 7 before any replay step,
    with `ManifestBoundMissing` naming the manifest, its kind and the key, logged by the replay and as
    `migrate_failed` by `migrate`: no row, no fence, nothing deleted. The keys are each kind's own
    (`KindProcedure.max_ids_keys`: what its procedure reads, every one an identity table any manifest bounds)
    and those its typed operations' selectors read. A golden-case deletion reads none: its content with an
    empty `max_ids` replays."""
    async with engine.connect() as conn:
        bounds = await max_identity_ids(conn)
    assert purge.KINDS[kind].max_ids_keys <= bounds.keys()  # what a manifest the app wrote always holds
    built = content(
        kind=kind,
        operations=bounded_by(kind),
        max_ids={} if missing is None else {t: v for t, v in bounds.items() if t != missing},
    )
    write_manifest_file(purger.cfg.manifests_dir, built)

    if missing is None:
        await migrate(make_config, purger, steps=frozenset({"replay"}))
        assert await scalar(engine, _MANIFEST_ROWS) == 1
        return
    with capture_logs() as logs, pytest.raises(ManifestBoundMissing) as stopped:
        await migrate(make_config, purger, steps=frozenset({"replay"}))

    refused = stopped.value
    assert (refused.manifest_uuid, refused.kind, refused.key) == (built["manifest_uuid"], kind, missing)
    assert refused.reason == "bound_missing" and isinstance(refused, ManifestInvalid)
    assert [
        (e["manifest_uuid"], e["kind"], e["key"]) for e in logs if e["event"] == "replay_bound_missing"
    ] == [(built["manifest_uuid"], kind, missing)]
    (failed,) = [e for e in logs if e["event"] == "migrate_failed"]
    assert (failed["step"], failed["error_type"]) == ("replay", "ManifestBoundMissing")
    assert await scalar(engine, _MANIFEST_ROWS) == 0
    assert await scalar(engine, sa.text("SELECT count(*) FROM purge_fences")) == 0


class LaterBound:
    """The golden-case deletion as a later release might write it (final fix C3): it also reads the bound of a
    table that release created, `later_table`, declared added (or, for the refusal, required), and records
    what each execution read. Everything else is the real procedure's."""

    def __init__(self, inner: Any, *, added: bool) -> None:
        self.inner = inner
        self.kind = inner.kind
        self.deletes_exports = inner.deletes_exports
        self.max_ids_keys = inner.max_ids_keys | (frozenset() if added else frozenset({"later_table"}))
        self.max_ids_keys_added = frozenset({"later_table"}) if added else frozenset()
        self.read: list[int] = []

    async def compute(self, *args: Any, **kwargs: Any) -> Any:
        return await self.inner.compute(*args, **kwargs)

    async def execute(self, run: Any, content: Any) -> None:
        self.read.append(content["max_ids"].get("later_table", 0))
        await self.inner.execute(run, content)

    async def postconditions(self, conn: Any, content: Any) -> Any:
        return await self.inner.postconditions(conn, content)

    async def reapply_admission(self, *args: Any, **kwargs: Any) -> None:
        await self.inner.reapply_admission(*args, **kwargs)


@pytest.mark.parametrize("added", [True, False], ids=["declared_added", "declared_required"])
async def test_a_bound_a_later_release_added_reads_as_0_where_an_older_manifest_lacks_it(
    engine: AsyncEngine,
    make_config: ConfigFactory,
    purger: Purger,
    monkeypatch: pytest.MonkeyPatch,
    added: bool,
) -> None:
    """Final fix C3 (final re-review N3): a manifest written before a release created a table has no row of it
    within its bound. So a kind may declare the table's key added (`max_ids_keys_added`): the bound check
    requires it of no manifest, and an older manifest that lacks it replays with the bound read as 0. The same
    key declared required stops `migrate` with `ManifestBoundMissing` before any replay step."""
    later = LaterBound(purge.KINDS["golden_case_delete"], added=added)
    monkeypatch.setitem(purge.KINDS, "golden_case_delete", later)
    built = content(kind="golden_case_delete", operations=bounded_by("golden_case_delete"), max_ids={})
    write_manifest_file(purger.cfg.manifests_dir, built)

    if added:
        await migrate(make_config, purger, steps=frozenset({"replay"}))
        assert await scalar(engine, _MANIFEST_ROWS) == 1
        assert later.read == [0]
        return
    with pytest.raises(ManifestBoundMissing) as stopped:
        await migrate(make_config, purger, steps=frozenset({"replay"}))
    assert (stopped.value.kind, stopped.value.key) == ("golden_case_delete", "later_table")
    assert (await scalar(engine, _MANIFEST_ROWS), later.read) == (0, [])


@pytest.mark.parametrize("defect", ["another_kind", "another_hash", "completed_without_content"])
async def test_a_file_or_row_that_is_not_its_manifest_stops_migrate(
    engine: AsyncEngine,
    migrator: AsyncEngine,
    make_config: ConfigFactory,
    purger: Purger,
    defect: str,
) -> None:
    """The classification refuses a stream file of another kind than its row, one whose bytes do not hash to
    the row's `file_sha256`, and a `completed` row without its content (no file, an empty `scope`): each is
    `ManifestConflict`, which stops step 7 before anything is replayed."""
    stream = purger.cfg.manifests_dir
    gone = await gone_alert(engine)
    if defect == "completed_without_content":
        async with engine.begin() as conn:
            row = await insert_manifest(conn, kind="golden_case_delete", request={}, now=utcnow())
            await conn.execute(_HASH_ONLY, {"m": row.id, "sha": hashlib.sha256(b"a shipped file").digest()})
            await complete_manifest(conn, row.id)
    else:
        row = await recorded_row(engine, alerts_named(gone), status="completed")
        other = content(
            str(row.manifest_uuid),
            kind="job_tombstone" if defect == "another_kind" else "golden_case_delete",
            operations=alerts_named(gone, gone + 1),
        )
        write_manifest_file(stream, other)
    newer = content(operations=alerts_named(gone))  # a manifest the replay would otherwise insert
    write_manifest_file(stream, newer)

    async with migrator.connect() as conn:
        with pytest.raises(ManifestConflict):
            await classify_stream(conn, stream, files=purger.file_roots)
    with capture_logs() as logs, pytest.raises(ManifestConflict):
        await migrate(make_config, purger, steps=frozenset({"replay"}))
    (failed,) = [e for e in logs if e["event"] == "migrate_failed"]
    assert (failed["step"], failed["error_type"]) == ("replay", "ManifestConflict")
    assert await scalar(engine, _MANIFEST_ROWS) == 1  # the newer manifest got no row: nothing replayed
    assert await scalar(engine, sa.text("SELECT count(*) FROM purge_fences")) == 0


async def test_nothing_to_replay_ends_step_7(
    migrated: PgServer, engine: AsyncEngine, make_config: ConfigFactory, purger: Purger
) -> None:
    """Every manifest `satisfied` (and none pending): step 7 ends after the classification, with no fence,
    no write and no `replay_log` entry."""
    gone = await gone_alert(engine)
    done = await recorded_row(engine, alerts_named(gone), status="completed")
    assert done.scope is not None
    write_manifest_file(purger.cfg.manifests_dir, done.scope)
    await recorded_row(engine, alerts_named(gone), status="shipped")
    counts = table_counts(migrated)

    with capture_logs() as logs:
        await migrate(make_config, purger, steps=frozenset({"replay"}))
    (classified,) = [e for e in logs if e["event"] == "replay_classified"]
    assert classified["outcomes"] == {"satisfied": 2}
    (report,) = [e for e in logs if e["event"] == "migrate_replayed"]
    assert (report["replayed"], report["pending_completed"], report["rebuilt_jobs"]) == (0, 0, 0)
    assert not [e for e in logs if e["event"].startswith("replay_phase")]
    assert table_counts(migrated) == counts
    assert (await one(engine, _MANIFEST, m=done.id)).replay_log == []


# ---- the phases --------------------------------------------------------------------------------------------


async def test_fences_are_released_only_in_phase_c(
    engine: AsyncEngine,
    make_config: ConfigFactory,
    purger: Purger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source purge captured `executing` with its file (recorded, nothing deleted yet): its fence stands,
    over the stored affected set, through phase A (the mixed job's tainted current revision nulled, the
    job still fenced) and phase B (the job rebuilt, still fenced), and is released only by phase C's
    transaction, which completes the manifest."""
    seed = await seeded(engine)
    opened = await request_source_purge(engine)
    await stopped_at(purger, opened, purge.AFTER_RECORD, monkeypatch)
    pauses = PausePoints(replay.AFTER_A, replay.AFTER_B)
    monkeypatch.setattr(replay, "afault_point", pauses)
    closed = seed.closed_job_id

    task = asyncio.create_task(migrate(make_config, purger))
    await asyncio.wait_for(pauses[replay.AFTER_A].reached.wait(), 60)
    (fence,) = await fences(engine, opened.manifest_id)
    assert fence.kind == "source" and closed in fence.job_ids and fence.released_at is None
    assert await scalar(engine, IS_FENCED, j=closed) is True
    assert (await one(engine, JOB, j=closed)).current_revision_id is None  # tainted: nulled in phase A
    pauses[replay.AFTER_A].resume()

    await asyncio.wait_for(pauses[replay.AFTER_B].reached.wait(), 60)
    assert (await one(engine, JOB, j=closed)).current_revision_id is not None  # rebuilt in phase B
    assert [f.released_at for f in await fences(engine, opened.manifest_id)] == [None]
    assert await scalar(engine, IS_FENCED, j=closed) is True
    assert (await one(engine, _MANIFEST, m=opened.manifest_id)).status == "executing"
    pauses[replay.AFTER_B].resume()

    assert await asyncio.wait_for(task, 120) == ["reconstruct_and_advance", "advance_again", "replay"]
    await assert_replayed(engine, purger, opened.manifest_id)
    assert await scalar(engine, IS_FENCED, j=closed) is False


async def test_a_failed_postcondition_rolls_back_and_keeps_the_fences(
    engine: AsyncEngine,
    make_config: ConfigFactory,
    purger: Purger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A purged export's archive back on the exports volume after phase B: phase C finds its `file_remove`
    violated, rolls back (the fence still active, the manifest `executing`, no `replay_log` entry) and
    `migrate` stops (`ReplayFailed`). Once the file is gone, the next `migrate` replays the stream again from
    the stored set and completes."""
    seed = await seeded(engine)
    opened = await request_source_purge(engine)
    await stopped_at(purger, opened, purge.AFTER_RECORD, monkeypatch)
    pauses = PausePoints(replay.AFTER_B)
    monkeypatch.setattr(replay, "afault_point", pauses)
    late = purger.cfg.exports_dir / f"{seed.ready_export_id}-late.jhx"

    task = asyncio.create_task(migrate(make_config, purger))
    await asyncio.wait_for(pauses[replay.AFTER_B].reached.wait(), 60)
    assert (await one(engine, EXPORT, e=seed.ready_export_id)).status == "purged"
    late.write_bytes(b"an archive written again")
    with capture_logs() as logs:
        pauses[replay.AFTER_B].resume()
        with pytest.raises(ReplayFailed):
            await asyncio.wait_for(task, 120)
    (failed,) = [e for e in logs if e["event"] == "replay_postcondition_failed"]
    assert [(v["manifest_id"], v["op"], v["table"], v["detail"]) for v in failed["violations"]] == [
        (opened.manifest_id, "file_remove", None, "1 file on exports remains")
    ]
    (stopped,) = [e for e in logs if e["event"] == "migrate_failed"]
    assert (stopped["step"], stopped["error_type"]) == ("replay", "ReplayFailed")
    row = await one(engine, _MANIFEST, m=opened.manifest_id)
    assert row.status == "executing" and completions(row.replay_log) == []
    (started,) = [e for e in row.replay_log if e.get("phase") == replay.STARTED]
    assert [f.released_at for f in await fences(engine, opened.manifest_id)] == [None]
    assert await scalar(engine, IS_FENCED, j=seed.closed_job_id) is True

    late.unlink()
    monkeypatch.setattr(replay, "afault_point", no_fault)
    await migrate(make_config, purger)
    await assert_replayed(engine, purger, opened.manifest_id)
    # The second `migrate` resumed the replay the first one started: its restore id and its high water.
    log_ = (await one(engine, _MANIFEST, m=opened.manifest_id)).replay_log
    assert [e.get("phase") for e in log_] == [replay.STARTED, replay.ADMITTED, None]
    assert completions(log_)[0]["restore_id"] == started["restore_id"]


async def test_a_checked_manifest_is_replayed_again(
    engine: AsyncEngine,
    make_config: ConfigFactory,
    purger: Purger,
) -> None:
    """A completed golden-case deletion whose export archive is back on the exports volume (a restore put it
    there): its `file_remove` fails, so it is `checked` and replayed again. Its procedure finds its case gone,
    its export ops remove the archive, and phase C keeps it `completed` with one `replay_log` entry."""
    seed = await seeded(engine)
    async with engine.begin() as conn:
        opened = await request_golden_case_delete(conn, seed.closed_case_id, now=utcnow(), defer=None)
    await purger.run(opened.operation_id)
    done = await completed(purger, opened.manifest_id, opened.operation_id)
    again = purger.cfg.exports_dir / f"{seed.ready_export_id}-restored.jhx"
    again.write_bytes(b"an archive a restore brought back")

    with capture_logs() as logs:
        await migrate(make_config, purger)
    (classified,) = [e for e in logs if e["event"] == "replay_classified"]
    assert classified["outcomes"] == {"checked": 1, "satisfied": 1}  # and the seed's shipped manifest
    (report,) = [e for e in logs if e["event"] == "migrate_replayed"]
    assert (report["replayed"], report["pending_completed"]) == (1, 0)
    assert not again.exists()
    row = await one(engine, _MANIFEST, m=done.id)
    assert row.status == "completed" and row.completed_at == done.completed_at
    assert [entry["counts"]["file_remove"] for entry in completions(row.replay_log)] == [1]
    assert done.scope is not None
    async with engine.connect() as conn:
        assert await check_postconditions(conn, done.scope, files=purger.file_roots) == []


async def test_a_merged_members_adzuna_data_above_the_bound_survives_a_replay(
    engine: AsyncEngine,
    make_config: ConfigFactory,
    purger: Purger,
) -> None:
    """The T15 carry in the replay's phase B: the Adzuna purge completed, Adzuna re-enabled by a new settings
    version and collected again on a job merged into the mixed job (a posting and an observation above the
    bound, which the root's provenance names). The manifest is then `checked` (an export archive back on the
    volume) and replayed: phase B rebuilds the root within the manifest's bound, the member's posting and
    observation survive, the root's provenance still names Adzuna (above the bound), and Adzuna stays
    enabled (the user's later version)."""
    seed = await seeded(engine)
    closed, member = seed.closed_job_id, seed.merged_job_ids[-1]
    opened = await request_source_purge(engine)
    await purger.run(opened.operation_id)
    manifest = await completed(purger, opened.manifest_id, opened.operation_id)
    async with engine.begin() as conn:
        active = await lock_active(conn)
        payload = cast(dict[str, Any], active.payload)
        sources = payload["sources"]
        enabled = {**payload, "sources": {**sources, "enabled": {**sources["enabled"], ADZUNA: True}}}
        request = SaveRequest(
            payload=enabled, base_version_id=active.settings_version_id, acknowledged_warnings=frozenset()
        )
        await save_settings(conn, request, now=utcnow(), defer=defer_in_tx, internal=True)
        posting, observation = await add_posting(
            conn, member, ADZUNA, observed_at=utcnow(), salary=stated(1, 2)
        )
    await recomputed(engine, closed)
    assert ADZUNA in await scalar(engine, PROVENANCE_SOURCES, j=closed)  # collected above the bound
    assert manifest.scope is not None and posting > manifest.scope["max_ids"]["postings"]
    archive = purger.cfg.exports_dir / f"{seed.ready_export_id}-restored.jhx"
    archive.write_bytes(b"an archive a restore brought back")

    with capture_logs() as logs:
        await migrate(make_config, purger)
    (classified,) = [e for e in logs if e["event"] == "replay_classified"]
    assert classified["outcomes"] == {"checked": 1, "satisfied": 1}  # and the seed's shipped manifest
    assert [e["job_id"] for e in logs if e["event"] == "source_purge_job_rebuilt"] == [closed]
    assert await scalar(engine, POSTING_EXISTS, p=posting) is True
    assert await scalar(engine, OBSERVATION_EXISTS, o=observation) is True
    assert ADZUNA in await scalar(engine, PROVENANCE_SOURCES, j=closed)
    assert (await one(engine, ACTIVE_PAYLOAD)).payload["sources"]["enabled"][ADZUNA] is True
    assert not archive.exists()
    row = await one(engine, _MANIFEST, m=opened.manifest_id)  # still the live completion, now replayed once
    assert row.completed_at == manifest.completed_at and len(completions(row.replay_log)) == 1
    assert [f.released_at is not None for f in await fences(engine, opened.manifest_id)] == [True, True]
    assert manifest.scope is not None
    async with engine.connect() as conn:
        assert await check_postconditions(conn, manifest.scope, files=purger.file_roots) == []
        assert await source_purge.SourcePurge().postconditions(conn, manifest.scope) == []


@pytest.mark.parametrize("point", [replay.AFTER_A, replay.AFTER_B])
async def test_a_crash_in_each_phase_restarts_cleanly(
    engine: AsyncEngine,
    make_config: ConfigFactory,
    purger: Purger,
    job_with_postings: JobWithPostings,
    monkeypatch: pytest.MonkeyPatch,
    point: str,
) -> None:
    """A crash after phase A or after phase B leaves the fence over the stored set standing and the manifest
    `executing`; the next `migrate` restarts step 7 from the stored sets and completes. C38's replay half:
    a mixed job whose only link to Adzuna was its posting (its salary; its lineage never named Adzuna) no
    longer matches the source once phase A deleted that posting, and a re-derivation would miss it; it stays
    fenced through the stored `job_ids`, and the restart rebuilds it from them."""
    mixed = await job_with_postings(
        {
            "greenhouse": {},
            ADZUNA: {
                "description_text": "Kurz.",
                "description_completeness": "partial",
                "salary": stated(60_000, 70_000),
            },
        }
    )
    revision = await recomputed(engine, mixed.job_id)
    only = await job_with_postings({ADZUNA: {}})
    await recomputed(engine, only.job_id)
    assert (await one(engine, JOB, j=mixed.job_id)).salary is not None
    opened = await request_source_purge(engine)
    manifest = await stopped_at(purger, opened, purge.AFTER_RECORD, monkeypatch)
    assert manifest.scope is not None and mixed.job_id in manifest.scope["affected"]["job_ids"]

    monkeypatch.setattr(replay, "afault_point", raising_at(point))
    with capture_logs() as logs, pytest.raises(SimulatedCrash):
        await migrate(make_config, purger)
    assert [e["error_type"] for e in logs if e["event"] == "migrate_failed"] == ["SimulatedCrash"]
    assert (await one(engine, _MANIFEST, m=opened.manifest_id)).status == "executing"
    (fence,) = await fences(engine, opened.manifest_id)
    assert mixed.job_id in fence.job_ids and fence.released_at is None
    assert await scalar(engine, IS_FENCED, j=mixed.job_id) is True  # through the stored job_ids
    links = await one(engine, _LINKS, j=mixed.job_id)
    assert (links.postings, links.lineage) == (0, 0)  # phase A ran: nothing names Adzuna but its values
    if point == replay.AFTER_A:
        assert ADZUNA in await scalar(engine, PROVENANCE_SOURCES, j=mixed.job_id)  # not rebuilt yet
        async with engine.connect() as conn:
            derived = await source_purge.SourcePurge().compute(conn, manifest, now=utcnow())
        assert mixed.job_id not in derived["affected"]["job_ids"]  # a re-derivation would miss it
    else:
        assert (await one(engine, JOB, j=mixed.job_id)).salary is None  # rebuilt, the fence still standing

    monkeypatch.setattr(replay, "afault_point", no_fault)
    await migrate(make_config, purger)
    await assert_replayed(engine, purger, opened.manifest_id)
    row = await one(engine, JOB, j=mixed.job_id)
    assert row.current_revision_id == revision and row.salary is None  # rebuilt, its revision reused
    assert await scalar(engine, PROVENANCE_SOURCES, j=mixed.job_id) == ["greenhouse"]
    assert await scalar(engine, JOB_EXISTS, j=only.job_id) is False


# ---- files and repeats -------------------------------------------------------------------------------------


async def test_replay_never_overwrites_a_valid_file(
    engine: AsyncEngine,
    make_config: ConfigFactory,
    purger: Purger,
    job_with_postings: JobWithPostings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A purge whose file was written, but whose A3 never recorded it (a crash after the rename): replay
    adopts that file (its hash and content recorded on the row) and never computes another content, though
    one computed now would differ (a posting collected since lies above the file's bounds); the file's bytes
    and modification time stay, and so do those of a completed manifest's file."""
    job = await job_with_postings({"greenhouse": {}, ADZUNA: {}})
    await recomputed(engine, job.job_id)
    gone = await gone_alert(engine)
    done = await recorded_row(engine, alerts_named(gone), status="completed")
    assert done.scope is not None
    write_manifest_file(purger.cfg.manifests_dir, done.scope)
    opened = await request_source_purge(engine)
    manifest = await stopped_at(purger, opened, purge.AFTER_FILE, monkeypatch)
    assert manifest.file_sha256 is None
    path = purger.manifest_file(manifest.manifest_uuid)
    done_path = purger.manifest_file(done.manifest_uuid)
    written, done_written = path.read_bytes(), done_path.read_bytes()
    stamps = (path.stat().st_mtime_ns, done_path.stat().st_mtime_ns)
    other = await job_with_postings({ADZUNA: {}})  # collected after A2: above every bound of the file
    await recomputed(engine, other.job_id)

    await migrate(make_config, purger)
    assert path.read_bytes() == written and done_path.read_bytes() == done_written
    assert (path.stat().st_mtime_ns, done_path.stat().st_mtime_ns) == stamps
    row = await one(engine, _MANIFEST, m=opened.manifest_id)
    assert bytes(row.file_sha256) == hashlib.sha256(written).digest()
    assert canonical_bytes(row.scope) == written
    await assert_replayed(engine, purger, opened.manifest_id)
    assert await scalar(engine, JOB_EXISTS, j=other.job_id) is True  # above the bound: never touched


async def test_a_second_replay_deletes_nothing(
    migrated: PgServer,
    engine: AsyncEngine,
    make_config: ConfigFactory,
    purger: Purger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once a replay completed, the next `migrate` finds every manifest `satisfied`: data created since
    (an Adzuna posting on a rebuilt job, an alert) survives, no row of any table changes, and the manifest
    keeps its one `replay_log` entry."""
    seed = await seeded(engine)
    opened = await request_source_purge(engine)
    await stopped_at(purger, opened, purge.AFTER_RECORD, monkeypatch)
    await migrate(make_config, purger)
    await assert_replayed(engine, purger, opened.manifest_id)
    async with engine.begin() as conn:
        posting, _ = await add_posting(conn, seed.closed_job_id, ADZUNA, observed_at=utcnow())
    await alert(engine)
    counts = table_counts(migrated)

    with capture_logs() as logs:
        await migrate(make_config, purger)
    (classified,) = [e for e in logs if e["event"] == "replay_classified"]
    assert set(classified["outcomes"]) == {"satisfied"}
    assert table_counts(migrated) == counts
    assert await scalar(engine, POSTING_EXISTS, p=posting) is True
    await assert_replayed(engine, purger, opened.manifest_id)  # still one replay_log entry
