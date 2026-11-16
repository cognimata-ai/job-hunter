"""The typed replay after a restore (spec §18.2 "Typed replay", §19.5 "Replay rules", §20.4, §20.5 step 6;
plan M5 Task 25, Deviations 16, 53, 55 and 56): `migrate` step 7 on a database restored from a backup set.

A backup set is the shared database dumped and restored into a database of its own on the test server
(`tests.support.restore.dump_and_restore`: `pg_dump -Fc`, then `pg_restore --no-owner --no-acl
--exit-on-error -L <filtered list>` as the migrator, into a database made from `jobhunter_template`), which
is dropped after the test. The data is the two-year-old seed (`tests.support.privacy_seed`): every table
holds a row, `closed_job` is the mixed job (an Adzuna and an ATS posting; its current revision's lineage
names both), and the Adzuna-only jobs are deleted by a source purge. The purges after the set run as the
worker runs them (`tests.support.purges`), their files in the test's own stream; `migrate` then runs on the
restored database with that stream and with the restored database's own CV and export volumes, as a
disaster restore runs it. The current-score view and the Jobs list are read on the restored database while
`migrate` is held at a phase's fault point (`tests.support.faults.PausePoints`). Every datum is invented; no
call leaves the process (delete-everything's live run cancels through M3's fake Batches API, its replay
calls nothing).
"""

import asyncio
import copy
import dataclasses
import hashlib
import secrets
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal, cast
from uuid import UUID

import procrastinate
import pytest
import sqlalchemy as sa
from jobhunter.backup.restore import restore_list
from jobhunter.clock import utcnow
from jobhunter.db.migrate import MIGRATE_ORDER, StepGroup, run_migrate_async
from jobhunter.db.seed import content_hash
from jobhunter.db.session import make_null_engine
from jobhunter.db.snapshot import read_snapshot
from jobhunter.errors import InvariantViolation
from jobhunter.jobs.listing import select_page
from jobhunter.jobs.query import JobsQuery
from jobhunter.llm.batches import BatchesClient
from jobhunter.llm.schemas.profile import ConfirmedProfile
from jobhunter.observability.deadman import DeadmanPinger
from jobhunter.privacy import deletion_graph, purge, replay
from jobhunter.privacy.kinds import job_tombstone, profile_purge, source_purge
from jobhunter.privacy.kinds.golden_case import request_golden_case_delete
from jobhunter.privacy.manifests import (
    FileRoots,
    ManifestKind,
    ManifestRow,
    check_postconditions,
    manifest_file_name,
    read_manifest,
)
from jobhunter.privacy.purge import KINDS, OpenedPurge
from jobhunter.privacy.replay import Classified, classify_stream
from jobhunter.profile import activation
from jobhunter.scoring import batch_results
from jobhunter.scoring import golden as golden_rules
from jobhunter.settings.active import lock_active
from jobhunter.settings.dryrun import dry_run
from jobhunter.settings.model import Settings
from jobhunter.settings.save import SaveRequest, save_settings
from jobhunter.worker import operations as ops
from jobhunter.worker.operations import EVAL_RUN_INTERRUPTED, fail_eval_run
from jobhunter.worker.tasks import defer_in_tx
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from structlog.testing import capture_logs

from tests.support.collect import JobWithPostings, add_posting, queued_run
from tests.support.faults import PausePoints, SimulatedCrash, no_fault, raising_at
from tests.support.fixtures import ConfigFactory
from tests.support.observations import stated
from tests.support.pg import PgServer
from tests.support.privacy_seed import SeedIds
from tests.support.profile import SYNTHETIC_PROFILE
from tests.support.purges import (
    ALERT,
    EXPORT,
    IS_FENCED,
    JOB,
    PROVENANCE_SOURCES,
    Detached,
    Purger,
    completed,
    completions,
    detached_feedback,
    one,
    recomputed,
    request_delete_everything,
    request_source_purge,
    rows,
    scalar,
    seeded,
    stopped_at,
)
from tests.support.restore import drop_database, dump_and_restore, dump_listing

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("current_epoch")]

ADZUNA: Final = "adzuna"
LIVE_DB: Final = "jobhunter"
SEEDED_BATCH: Final = "msgbatch_seed_submitted"  # the seed's live submission's provider id when `submitted`
EVERY_CARD: Final = JobsQuery(
    posted="any",
    status=["unseen", "seen", "saved", "applied", "hidden"],
    availability=["active", "unknown", "stale", "closed"],
    scoring_state=["scored", "unscored", "filtered", "failed", "expired", "not_a_job_ad"],
)
# `jobhunter manifests replay`: steps 1, 3 and 7 (§19.4).
REPLAY_STEPS: Final[frozenset[StepGroup]] = frozenset({"sequences", "replay"})

_MANIFEST_ID: Final = sa.text("SELECT id FROM deletion_manifests WHERE manifest_uuid = :u")
_MANIFEST: Final = sa.text(
    "SELECT status, completed_at, replay_log, file_sha256 FROM deletion_manifests WHERE manifest_uuid = :u"
)
_FENCES: Final = sa.text(
    "SELECT f.kind, f.job_ids, f.released_at FROM purge_fences f JOIN deletion_manifests m "
    "ON m.id = f.manifest_id WHERE m.manifest_uuid = :u ORDER BY f.id"
)
_OPENED: Final = sa.text(
    "SELECT m.id AS manifest_id, m.manifest_uuid, o.id AS operation_id FROM deletion_manifests m "
    "JOIN operations o ON o.kind = 'purge' AND o.payload -> 'manifest_id' = to_jsonb(m.id) "
    "WHERE m.kind = :kind ORDER BY m.id DESC LIMIT 1"
)
_PROFILE_PURGE_OF: Final = sa.text(
    "SELECT m.id AS manifest_id, m.manifest_uuid, o.id AS operation_id FROM deletion_manifests m "
    "JOIN operations o ON o.kind = 'purge' AND o.payload -> 'manifest_id' = to_jsonb(m.id) "
    "WHERE m.kind = 'profile_purge' AND m.request -> 'profile_version_id' = to_jsonb(CAST(:v AS bigint))"
)
_LINEAGE: Final = sa.text(
    "SELECT coalesce(array_agg(DISTINCT s.source_id ORDER BY s.source_id), '{}') FROM jobs j "
    "JOIN job_revision_sources s ON s.job_revision_id = j.current_revision_id WHERE j.id = :j"
)
_CURRENT_SCORES: Final = sa.text("SELECT count(*) FROM current_scores WHERE job_id = :j")
_VERSION: Final = sa.text(
    "SELECT profile_json IS NULL AS redacted, purged_at, profile_hash FROM profile_versions WHERE id = :v"
)
_TUPLES: Final = sa.text("SELECT id FROM llm_tuples WHERE profile_version_id = :v ORDER BY id")
_OPERATION_STATUS: Final = sa.text("SELECT status FROM operations WHERE id = :o")
_PROPOSAL_STATUS: Final = sa.text("SELECT status FROM query_proposals WHERE id = :p")
_ACTIVE_SETTINGS: Final = sa.text(
    "SELECT v.id, v.payload, v.created_at FROM active_config a JOIN settings_versions v "
    "ON v.id = a.settings_version_id WHERE a.id = 1"
)
_FENCE_AT: Final = sa.text("SELECT purge_fence_at FROM sources WHERE id = 'adzuna'")
_RUN: Final = sa.text("SELECT status, status_reason FROM runs WHERE id = :r")
_TOMBSTONES_OF: Final = sa.text("SELECT count(*) FROM job_tombstones WHERE former_job_id = :j")
_SUBMISSION_EXISTS: Final = sa.text("SELECT EXISTS (SELECT 1 FROM batch_submissions WHERE id = :s)")
_POSTINGS_MAX: Final = sa.text("SELECT coalesce(max(id), 0) FROM postings")
_JOBS: Final = sa.text("SELECT count(*) FROM jobs")
_SESSION_COUNT: Final = sa.text("SELECT count(*) FROM sessions")
_ACTIVE_CASE: Final = sa.text("SELECT id FROM golden_cases WHERE active ORDER BY id LIMIT 1")
_EVAL_RUN: Final = sa.text("SELECT status, metrics FROM eval_runs WHERE id = :r")
_ROW_EXISTS: Final = "SELECT EXISTS (SELECT 1 FROM {table} WHERE id = :i)"
_FEEDBACK: Final = sa.text("SELECT score_snapshot, kind, label, comment FROM score_feedback WHERE id = :f")
_FEEDBACK_OF_JOB: Final = sa.text("SELECT id FROM score_feedback WHERE job_id = :j")
_CONFIGURATION: Final = sa.text(
    "SELECT 'version' AS kind, id, version_no, NULL::timestamptz AS at FROM settings_versions "
    "UNION ALL SELECT 'active', settings_version_id, scope_version_id, updated_at FROM active_config "
    "UNION ALL SELECT 'pending', pending_scope_version_id, profile_version_id, NULL FROM active_config "
    "UNION ALL SELECT 'schedule', settings_version_id, interval_s, last_evaluated_slot FROM schedule_state "
    "ORDER BY 1, 2"
)
_JOB_ROW: Final = sa.text(
    "INSERT INTO procrastinate_jobs (queue_name, task_name, args, status) "
    "VALUES ('aux', 'replay_test', '{}', 'doing') RETURNING id"
)
_FINISH_JOB: Final = sa.text("UPDATE procrastinate_jobs SET status = 'succeeded' WHERE id = :j")
_RESOLVE: Final = sa.text("UPDATE alerts SET resolved_at = now() WHERE id = :a AND resolved_at IS NULL")
_LOCKOUT_SCOPES: Final = sa.text("SELECT scope FROM login_lockouts ORDER BY scope")
_RENEW_LOCKOUT: Final = sa.text(
    "INSERT INTO login_lockouts (scope, locked_until, reason) VALUES (:scope, now() + interval '15 minutes', "
    "'failures') ON CONFLICT (scope) DO UPDATE SET locked_until = GREATEST(login_lockouts.locked_until, "
    "EXCLUDED.locked_until), reason = EXCLUDED.reason, generation = DEFAULT"
)
_SESSION: Final = sa.text(
    "INSERT INTO sessions (token_hmac, user_id, created_at, last_seen_at, idle_expires_at, "
    "absolute_expires_at, ip, user_agent_sha256) VALUES (:token, 1, now(), now(), now() + interval '1 hour', "
    "now() + interval '1 day', CAST('198.51.100.20' AS inet), '\\x02')"
)
_LOGIN_ATTEMPT: Final = sa.text(
    "INSERT INTO login_attempts (at, ip, username_hmac, success) "
    "VALUES (now(), CAST('198.51.100.30' AS inet), '\\x03', false)"
)
_CV: Final = sa.text(
    "INSERT INTO cv_files (sha256, mime, size_bytes) VALUES (:h, 'text/plain', 1) RETURNING id"
)
_READY_DRAFT: Final = sa.text(
    "INSERT INTO profile_drafts (cv_file_id, status, reviewed_text_sha256, draft_json) "
    "VALUES (:cv, 'ready', :h, CAST(:profile AS jsonb)) RETURNING id"
)
_LOCK_ACTIVE: Final = sa.text("SELECT 1 FROM active_config WHERE id = 1 FOR UPDATE")
_SEQUENCES: Final = sa.text(
    "SELECT c.relname AS table_name, a.attname AS column_name FROM pg_depend d "
    "JOIN pg_class s ON s.oid = d.objid AND s.relkind = 'S' "
    "JOIN pg_class c ON c.oid = d.refobjid AND c.relkind IN ('r', 'p') "
    "JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public' "
    "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = d.refobjsubid "
    "WHERE d.classid = CAST('pg_class' AS regclass) AND d.refclassid = CAST('pg_class' AS regclass) "
    "AND d.deptype IN ('a', 'i')"
)


# ---- the restored database ---------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Restored:
    """A database restored from a backup set: its name, the migrator's engine on it, and its CV and export
    volumes (empty unless a test puts a file there)."""

    name: str
    engine: AsyncEngine
    files: FileRoots


@pytest.fixture
async def restored(migrated: PgServer, tmp_path: Path) -> AsyncIterator[Restored]:
    name = f"jh_replay_{secrets.token_hex(4)}"
    files = FileRoots(cv_store=tmp_path / "restored-cv", exports=tmp_path / "restored-exports")
    for directory in (files.cv_store, files.exports):
        directory.mkdir(mode=0o700)
    engine = make_null_engine(migrated.dsn("migrator", name, driver="sqlalchemy"))
    try:
        yield Restored(name, engine, files)
    finally:
        await engine.dispose()
        drop_database(migrated, name)


async def backup_set(server: PgServer, restored: Restored) -> None:
    """The shared database as a backup set captures it now, restored into `restored`'s database."""
    await dump_and_restore(server, source_db=LIVE_DB, target_db=restored.name)


async def migrate_restored(
    make_config: ConfigFactory, restored: Restored, stream: Path, *, steps: frozenset[StepGroup] | None = None
) -> list[str]:
    """`migrate` on the restored database (a disaster restore's: every step), with `stream` and the restored
    database's volumes."""
    return list(
        await run_migrate_async(
            make_config("migrate"),
            now=utcnow(),
            steps=steps,
            database=restored.name,
            manifest_dir=stream,
            cv_dir=restored.files.cv_store,
            exports_dir=restored.files.exports,
        )
    )


async def migrate_live(make_config: ConfigFactory, purger: Purger) -> list[str]:
    """`jobhunter manifests replay` on the shared database, with the purger's stream and volumes."""
    return list(
        await run_migrate_async(
            make_config("migrate"),
            now=utcnow(),
            steps=REPLAY_STEPS,
            manifest_dir=purger.cfg.manifests_dir,
            cv_dir=purger.cfg.cv_store_dir,
            exports_dir=purger.cfg.exports_dir,
        )
    )


# ---- reading -----------------------------------------------------------------------------------------------


async def exists(engine: AsyncEngine, table: str, row_id: int) -> bool:
    return bool(await scalar(engine, sa.text(_ROW_EXISTS.format(table=table)), i=row_id))


async def row_of(engine: AsyncEngine, manifest_uuid: UUID) -> ManifestRow:
    """The manifest row of `manifest_uuid` in the database of `engine`."""
    async with engine.connect() as conn:
        manifest_id = (await conn.execute(_MANIFEST_ID, {"u": manifest_uuid})).scalar_one()
        found = await read_manifest(conn, int(manifest_id))
    assert found is not None
    return found


async def assert_replayed(engine: AsyncEngine, files: FileRoots, *manifests: ManifestRow) -> None:
    """Each manifest `completed` in the database of `engine` with one `replay_log` entry, every fence of it
    released in the completing transaction, and every postcondition (the typed and the kind's) holding."""
    for manifest in manifests:
        row = await one(engine, _MANIFEST, u=manifest.manifest_uuid)
        assert row.status == "completed" and len(completions(row.replay_log)) == 1, manifest.kind
        fences = await rows(engine, _FENCES, u=manifest.manifest_uuid)
        assert {fence.released_at for fence in fences} <= {row.completed_at}  # one transaction: one now()
        assert manifest.scope is not None
        async with engine.connect() as conn:
            assert await check_postconditions(conn, manifest.scope, files=files) == []
            assert await KINDS[manifest.kind].postconditions(conn, manifest.scope) == []


async def listed(engine: AsyncEngine) -> set[int]:
    """The jobs of the Jobs list's every card (every status, availability and scoring state), read as the
    list route reads them: in one read snapshot (`listing`'s module docstring, Cards)."""
    async with read_snapshot(engine) as conn:
        page = await select_page(
            conn, EVERY_CARD, window=(None, None), after=None, limit=500, with_counts=False
        )
    return {row.job_id for row in page.rows}


def events(logs: Sequence[Mapping[str, Any]], name: str) -> list[Mapping[str, Any]]:
    return [e for e in logs if e["event"] == name]


# ---- the purges after the set ------------------------------------------------------------------------------


async def opened_of(engine: AsyncEngine, kind: ManifestKind) -> OpenedPurge:
    """The newest purge of `kind` opened so far."""
    row = await one(engine, _OPENED, kind=kind)
    return OpenedPurge(int(row.manifest_id), row.manifest_uuid, int(row.operation_id))


async def request_golden(engine: AsyncEngine, case_id: int) -> OpenedPurge:
    async with engine.begin() as conn:
        return await request_golden_case_delete(conn, case_id, now=utcnow(), defer=None)


async def request_profile_purge(engine: AsyncEngine, version_id: int) -> OpenedPurge:
    """The retention rule's A0s; the purge of `version_id` among them."""
    assert await profile_purge.open_due(engine, now=utcnow(), epoch=1, defer=None) >= 1
    row = await one(engine, _PROFILE_PURGE_OF, v=version_id)
    return OpenedPurge(int(row.manifest_id), row.manifest_uuid, int(row.operation_id))


async def request_tombstones(engine: AsyncEngine, *, now: datetime) -> job_tombstone.OpenedBatch:
    async with engine.begin() as conn:
        batch = await job_tombstone.open_batch(
            conn, cutoff=now - job_tombstone.TOMBSTONE_AFTER, after=0, now=now, defer=None
        )
    assert batch is not None
    return batch


async def finished(purger: Purger, opened: OpenedPurge) -> ManifestRow:
    """The purge run by the worker's handler to its completion, every postcondition holding; its
    manifest."""
    await purger.run(opened.operation_id)
    return await completed(purger, opened.manifest_id, opened.operation_id)


async def _refuse_defer(conn: AsyncConnection, task: str, **kwargs: Any) -> int | None:
    raise AssertionError(f"this save defers no job, yet it deferred {task}")


async def save(engine: AsyncEngine, edit: Any, *, defer: Any = _refuse_defer, internal: bool = False) -> int:
    """A user's settings save of the active document as `edit` changes it, acknowledging its dry-run's
    warnings; the new version's id."""
    now = utcnow()
    async with engine.begin() as conn:
        active = await lock_active(conn)
        payload = edit(copy.deepcopy(cast(dict[str, Any], active.payload)))
        preview = await dry_run(conn, payload, active=Settings.model_validate(active.payload), now=now)
        request = SaveRequest(
            payload=payload,
            base_version_id=active.settings_version_id,
            acknowledged_warnings=frozenset(w.code for w in preview.warnings),
        )
        outcome = await save_settings(conn, request, now=now, defer=defer, internal=internal)
    return outcome.version_id


def adzuna_on(payload: dict[str, Any]) -> dict[str, Any]:
    payload["sources"]["enabled"][ADZUNA] = True
    return payload


async def adzuna_enabled(engine: AsyncEngine) -> bool:
    return bool((await one(engine, _ACTIVE_SETTINGS)).payload["sources"]["enabled"][ADZUNA])


async def confirm_profile(engine: AsyncEngine, seniority: str) -> int:
    """A ready draft of the synthetic profile at `seniority`, confirmed and activated as the route does it;
    the version."""
    profile = ConfirmedProfile.model_validate({**SYNTHETIC_PROFILE, "seniority": seniority}).model_dump_json()
    digest = content_hash(profile)
    async with engine.begin() as conn:
        cv = (await conn.execute(_CV, {"h": digest})).scalar_one()
        draft = (await conn.execute(_READY_DRAFT, {"cv": cv, "h": digest, "profile": profile})).scalar_one()
    async with engine.begin() as conn:
        await conn.execute(_LOCK_ACTIVE)
        status, body = await activation.confirm_draft(
            conn, int(draft), expected_rescore_ack=True, now=utcnow(), defer=defer_in_tx
        )
    assert status == 201, body
    version = body["profile_version_id"]
    assert isinstance(version, int)
    return version


# ---- §18.2 typed replay ------------------------------------------------------------------------------------


async def test_profile_source_and_golden_purges_replay_after_a_restore(
    migrated: PgServer,
    engine: AsyncEngine,
    purger: Purger,
    restored: Restored,
    make_config: ConfigFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A backup set taken before a golden-case deletion, a profile purge and a source purge is restored and
    replayed. The purged version still exists with `profile_json` NULL and its tuples; the mixed job, found
    through the source purge's stored affected set, has its tainted current revision nulled in phase A and is
    rebuilt in phase B (no Adzuna lineage or provenance afterwards), while its fence still stands; the fence
    is released only by phase C, whose transaction also completes the three manifests."""
    seed = await seeded(engine)
    closed, version = seed.closed_job_id, seed.purgeable_version_id
    tuples = await rows(engine, _TUPLES, v=version)
    await backup_set(migrated, restored)
    golden = await finished(purger, await request_golden(engine, seed.closed_case_id))
    profile = await finished(purger, await request_profile_purge(engine, version))
    source = await finished(purger, await request_source_purge(engine))

    pauses = PausePoints(replay.AFTER_A, replay.AFTER_B)
    monkeypatch.setattr(replay, "afault_point", pauses)
    task = asyncio.create_task(migrate_restored(make_config, restored, purger.cfg.manifests_dir))
    await asyncio.wait_for(pauses[replay.AFTER_A].reached.wait(), 120)
    assert (await one(restored.engine, JOB, j=closed)).current_revision_id is None
    assert await scalar(restored.engine, IS_FENCED, j=closed) is True
    pauses[replay.AFTER_A].resume()
    await asyncio.wait_for(pauses[replay.AFTER_B].reached.wait(), 120)
    assert (await one(restored.engine, JOB, j=closed)).current_revision_id is not None
    assert [f.released_at for f in await rows(restored.engine, _FENCES, u=source.manifest_uuid)] == [None]
    assert (await one(restored.engine, _MANIFEST, u=source.manifest_uuid)).status == "executing"
    pauses[replay.AFTER_B].resume()
    assert await asyncio.wait_for(task, 300) == list(MIGRATE_ORDER)

    await assert_replayed(restored.engine, restored.files, golden, profile, source)
    purged_version = await one(restored.engine, _VERSION, v=version)
    assert purged_version.redacted and purged_version.purged_at is not None
    assert await rows(restored.engine, _TUPLES, v=version) == tuples
    assert ADZUNA not in await scalar(restored.engine, PROVENANCE_SOURCES, j=closed)
    assert ADZUNA not in await scalar(restored.engine, _LINEAGE, j=closed)
    assert not await exists(restored.engine, "golden_cases", seed.closed_case_id)
    for job in seed.adzuna_only_job_ids:
        assert not await exists(restored.engine, "jobs", job)
    # The profile kind re-applies nothing of its A0 (it cancelled the version's proposal operation live): the
    # operation the dump holds stays `pending`, its proposal `purged`, which its next execution ends as
    # obsolete (`queries.proposals.end_obsolete`); a live execution is fenced at its next reservation or
    # marker (`test_profile_purge.py::test_no_call_of_the_version_is_reserved_after_admission`).
    assert await scalar(restored.engine, _OPERATION_STATUS, o=seed.running_operation_id) == "pending"
    assert await scalar(restored.engine, _PROPOSAL_STATUS, p=seed.running_proposal_id) == "purged"


async def test_a_source_purge_replay_redacts_the_feedback_snapshots_it_froze(
    migrated: PgServer,
    engine: AsyncEngine,
    purger: Purger,
    restored: Restored,
    make_config: ConfigFactory,
) -> None:
    """Final review I1: a backup set taken before the Adzuna purge holds the seed's label of the mixed job's
    tainted revision with its Adzuna-derived score snapshot. The manifest froze that feedback's id before any
    provenance was detached, so the replay redacts it as the live purge did: `score_snapshot` NULL, the kind,
    label and comment kept, the `field_redact` postcondition holding."""
    seed = await seeded(engine)
    feedback = int(await scalar(engine, _FEEDBACK_OF_JOB, j=seed.closed_job_id))
    await backup_set(migrated, restored)
    before = await one(restored.engine, _FEEDBACK, f=feedback)
    assert before.score_snapshot is not None
    source = await finished(purger, await request_source_purge(engine))
    assert source.scope is not None
    assert [
        op["ids"]
        for op in source.scope["operations"]
        if op["op"] == "field_redact" and op["table"] == "score_feedback"
    ] == [[feedback]]

    assert await migrate_restored(make_config, restored, purger.cfg.manifests_dir) == list(MIGRATE_ORDER)

    await assert_replayed(restored.engine, restored.files, source)
    after = await one(restored.engine, _FEEDBACK, f=feedback)
    assert after.score_snapshot is None
    assert (after.kind, after.label, after.comment) == (before.kind, before.label, before.comment)


@pytest.mark.parametrize("by", ["tombstoning", "score_history"])
async def test_a_source_purge_replay_redacts_a_detached_feedback_snapshot(
    migrated: PgServer,
    engine: AsyncEngine,
    purger: Purger,
    restored: Restored,
    make_config: ConfigFactory,
    by: Detached,
) -> None:
    """Final re-review N1: a label detached from its provenance by tombstoning or the score-history rule,
    whose snapshot records Adzuna among its sources (final fix C2), is in a backup set taken before the purge.
    The manifest froze its id by those sources, so the replay redacts it as the live purge did."""
    await seeded(engine)
    feedback = await detached_feedback(engine, by)
    await backup_set(migrated, restored)
    before = await one(restored.engine, _FEEDBACK, f=feedback)
    assert before.score_snapshot is not None
    source = await finished(purger, await request_source_purge(engine))
    assert source.scope is not None
    [frozen] = [
        op["ids"]
        for op in source.scope["operations"]
        if op["op"] == "field_redact" and op["table"] == "score_feedback"
    ]
    assert feedback in frozen

    assert await migrate_restored(make_config, restored, purger.cfg.manifests_dir) == list(MIGRATE_ORDER)

    await assert_replayed(restored.engine, restored.files, source)
    after = await one(restored.engine, _FEEDBACK, f=feedback)
    assert after.score_snapshot is None
    assert (after.kind, after.label, after.comment) == (before.kind, before.label, before.comment)


async def test_an_injected_rebuild_failure_keeps_the_fence_and_fails_migrate(
    migrated: PgServer,
    engine: AsyncEngine,
    purger: Purger,
    restored: Restored,
    make_config: ConfigFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rebuild that fails inside its chunk (phase B) stops `migrate`: the manifest stays `executing` and its
    fence active over the mixed job; the next `migrate` replays the stream again and completes."""
    seed = await seeded(engine)
    await backup_set(migrated, restored)
    source = await finished(purger, await request_source_purge(engine))

    monkeypatch.setattr(source_purge, "afault_point", raising_at(source_purge.MID_REBUILD))
    with capture_logs() as logs, pytest.raises(SimulatedCrash):
        await migrate_restored(make_config, restored, purger.cfg.manifests_dir)
    (failed,) = events(logs, "migrate_failed")
    assert (failed["step"], failed["error_type"]) == ("replay", "SimulatedCrash")
    assert (await one(restored.engine, _MANIFEST, u=source.manifest_uuid)).status == "executing"
    assert [f.released_at for f in await rows(restored.engine, _FENCES, u=source.manifest_uuid)] == [None]
    assert await scalar(restored.engine, IS_FENCED, j=seed.closed_job_id) is True

    monkeypatch.setattr(source_purge, "afault_point", no_fault)
    await migrate_restored(make_config, restored, purger.cfg.manifests_dir)
    await assert_replayed(restored.engine, restored.files, source)
    assert await scalar(restored.engine, IS_FENCED, j=seed.closed_job_id) is False


async def test_data_collected_after_a_reenablement_survives(
    migrated: PgServer,
    engine: AsyncEngine,
    purger: Purger,
    restored: Restored,
    make_config: ConfigFactory,
    job_with_postings: JobWithPostings,
) -> None:
    """Purge Adzuna, re-enable it with a new settings version and collect: a new Adzuna job, and fresh Adzuna
    postings with their observations on the rebuilt mixed job and on a job merged into it. `migrate` then
    replays nothing (the manifest is satisfied) and keeps every row collected after the watermark; the
    backup set from before the purge, restored and replayed, completes with every postcondition holding, and
    the next `migrate` there replays nothing."""
    seed = await seeded(engine)
    await backup_set(migrated, restored)
    source = await finished(purger, await request_source_purge(engine))
    await save(engine, adzuna_on, internal=True)
    fresh = await job_with_postings({ADZUNA: {}})
    await recomputed(engine, fresh.job_id)
    member = seed.merged_job_ids[-1]  # merged into `closed_job`
    async with engine.begin() as conn:
        on_root = await add_posting(
            conn, seed.closed_job_id, ADZUNA, observed_at=utcnow(), salary=stated(1, 2)
        )
        on_member = await add_posting(conn, member, ADZUNA, observed_at=utcnow())
    await recomputed(engine, seed.closed_job_id)
    assert ADZUNA in await scalar(engine, PROVENANCE_SOURCES, j=seed.closed_job_id)  # above the watermark

    with capture_logs() as logs:
        await migrate_live(make_config, purger)
    assert set(events(logs, "replay_classified")[0]["outcomes"]) == {"satisfied"}
    for posting, observation in (on_root, on_member):
        assert await exists(engine, "postings", posting)
        assert await exists(engine, "posting_observations", observation)
    assert await exists(engine, "jobs", fresh.job_id) and await adzuna_enabled(engine)
    assert ADZUNA in await scalar(engine, PROVENANCE_SOURCES, j=seed.closed_job_id)
    assert source.scope is not None
    async with engine.connect() as conn:
        assert await check_postconditions(conn, source.scope, files=purger.file_roots) == []
        assert await KINDS["source_purge"].postconditions(conn, source.scope) == []

    await migrate_restored(make_config, restored, purger.cfg.manifests_dir)
    await assert_replayed(restored.engine, restored.files, source)
    with capture_logs() as again:
        await migrate_restored(make_config, restored, purger.cfg.manifests_dir)
    assert set(events(again, "replay_classified")[0]["outcomes"]) == {"satisfied"}


async def test_a_job_two_source_purges_hold_is_rebuilt_once_by_the_latest(
    migrated: PgServer,
    engine: AsyncEngine,
    purger: Purger,
    restored: Restored,
    make_config: ConfigFactory,
) -> None:
    """Two source purges after the backup set: Adzuna purged (M1), re-enabled, collected again on the mixed
    job, and purged again (M2); both stored sets hold the mixed job. Phase B rebuilds it once, with the run of
    the latest replayed source purge that holds it (M2), whose bound covers the posting collected between
    them; both manifests complete."""
    seed = await seeded(engine)
    closed = seed.closed_job_id
    await backup_set(migrated, restored)
    m1 = await finished(purger, await request_source_purge(engine))
    await save(engine, adzuna_on, internal=True)
    async with engine.begin() as conn:
        await add_posting(conn, closed, ADZUNA, observed_at=utcnow(), salary=stated(1, 2))
    await recomputed(engine, closed)
    m2 = await finished(purger, await request_source_purge(engine))
    assert m1.scope is not None and m2.scope is not None
    assert closed in m1.scope["affected"]["job_ids"] and closed in m2.scope["affected"]["job_ids"]

    with capture_logs() as logs:
        await migrate_restored(make_config, restored, purger.cfg.manifests_dir)
    await assert_replayed(restored.engine, restored.files, m1, m2)
    latest = await row_of(restored.engine, m2.manifest_uuid)
    rebuilt = [e["manifest_id"] for e in events(logs, "source_purge_job_rebuilt") if e["job_id"] == closed]
    assert rebuilt == [latest.id]
    assert ADZUNA not in await scalar(restored.engine, PROVENANCE_SOURCES, j=closed)


async def test_purge_then_tombstone_then_restore(
    migrated: PgServer,
    engine: AsyncEngine,
    purger: Purger,
    restored: Restored,
    make_config: ConfigFactory,
) -> None:
    """Fable's sequence: a backup set; a source purge M1 with its rebuild; the retention pass tombstones the
    rebuilt job as M2; the server is lost. The restore replays M1 and M2 in phase A (M2 deletes the job: no
    rebuilt revision exists to block it), phase B skips the deleted job, and phase C verifies both manifests
    and releases both fences."""
    seed = await seeded(engine)
    closed = seed.closed_job_id
    await backup_set(migrated, restored)
    m1 = await finished(purger, await request_source_purge(engine))
    batch = await request_tombstones(engine, now=utcnow())
    assert closed in batch.roots
    m2 = await finished(purger, batch.purge)

    with capture_logs() as logs:
        await migrate_restored(make_config, restored, purger.cfg.manifests_dir)
    await assert_replayed(restored.engine, restored.files, m1, m2)
    names = [e["event"] for e in logs]
    a_done = names.index("replay_phase_a_done")
    assert names.index("source_purge_executed") < a_done and names.index("job_tombstoned") < a_done
    assert closed not in [e["job_id"] for e in events(logs, "source_purge_job_rebuilt")]
    assert not await exists(restored.engine, "jobs", closed)
    assert await scalar(restored.engine, _TOMBSTONES_OF, j=closed) == 1


async def test_stream_order_replay_tolerates_completions_out_of_order(
    migrated: PgServer,
    engine: AsyncEngine,
    purger: Purger,
    restored: Restored,
    make_config: ConfigFactory,
) -> None:
    """Completions out of stream order (the purge protocol's carry): a source purge M1 is admitted first, but
    its content is computed only after a later golden-case deletion M2 completed (its first run requeued at
    the quiesce), so M1 names none of the cases M2 deleted. In a backup set from before both, M2's case
    still names a revision M1 deletes. The replay runs M1 before M2, in stream order: M1's revision deletion
    takes that case first (a later manifest of the stream deletes it anyway), M2 finds it gone, and both
    complete with every postcondition holding."""
    seed = await seeded(engine)
    await backup_set(migrated, restored)
    first = await request_source_purge(engine)
    m2 = await finished(purger, await request_golden(engine, seed.closed_case_id))
    m1 = await finished(purger, first)
    assert m1.manifest_uuid < m2.manifest_uuid and m1.completed_at is not None
    assert m2.completed_at is not None and m2.completed_at < m1.completed_at
    assert m1.scope is not None
    deleted_cases = [
        op for op in m1.scope["operations"] if op["op"] == "row_delete" and op["table"] == "golden_cases"
    ]
    assert all(seed.closed_case_id not in op.get("ids", []) for op in deleted_cases)

    with capture_logs() as logs:
        await migrate_restored(make_config, restored, purger.cfg.manifests_dir)
    assert [e["kind"] for e in events(logs, "replay_admitted")][-2:] == ["source_purge", "golden_case_delete"]
    await assert_replayed(restored.engine, restored.files, m1, m2)
    assert not await exists(restored.engine, "golden_cases", seed.closed_case_id)


async def test_executing_without_file_is_completed_before_verify(
    migrated: PgServer,
    engine: AsyncEngine,
    purger: Purger,
    restored: Restored,
    make_config: ConfigFactory,
    tmp_path: Path,
) -> None:
    """A backup set captures a source purge's manifest `executing` before its file was written (A0 alone);
    the shipped stream lacks it. The restore classifies it a pending retry; `migrate` computes its content
    under the restored fence, writes its file, records it and replays it; only then does the manifest
    stand `completed` with every postcondition holding."""
    seed = await seeded(engine)
    opened = await request_source_purge(engine)
    await backup_set(migrated, restored)
    stream = tmp_path / "shipped"
    stream.mkdir(mode=0o700)
    async with restored.engine.connect() as conn:
        classified = await classify_stream(conn, stream, files=restored.files)
    assert Classified(opened.manifest_uuid, "pending_retry") in classified
    assert (await one(restored.engine, _MANIFEST, u=opened.manifest_uuid)).status == "executing"

    with capture_logs() as logs:
        await migrate_restored(make_config, restored, stream)
    (report,) = events(logs, "migrate_replayed")
    assert (report["replayed"], report["pending_completed"]) == (1, 1)
    row = await one(restored.engine, _MANIFEST, u=opened.manifest_uuid)
    written = (stream / manifest_file_name(opened.manifest_uuid)).read_bytes()
    assert bytes(row.file_sha256) == hashlib.sha256(written).digest()
    manifest = await row_of(restored.engine, opened.manifest_uuid)
    assert manifest.scope is not None and seed.closed_job_id in manifest.scope["affected"]["job_ids"]
    await assert_replayed(restored.engine, restored.files, manifest)


async def test_hash_present_file_never_shipped(
    migrated: PgServer,
    engine: AsyncEngine,
    purger: Purger,
    restored: Restored,
    make_config: ConfigFactory,
    job_with_postings: JobWithPostings,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A backup set captures a source purge `executing` with `file_sha256` and `scope` set, after it deleted
    the source's postings (the highest-id posting among them: the surviving maximum lies below its bound)
    and before its rebuild; the file is absent from the shipped stream. Every sequence of the restored
    database is set back to its table's surviving maximum, so only the manifest's bounds can keep a new row
    off its ids. Step 1 rewrites the file from `scope` before it advances the identities; every identity then
    restarts above the manifest's bounds, no row the replay's rebuild creates takes an id at or below
    `max_ids`, and phase C passes."""
    await seeded(engine)
    newest = await job_with_postings({ADZUNA: {}})  # the highest posting id, deleted by the purge
    await recomputed(engine, newest.job_id)
    opened = await request_source_purge(engine)
    manifest = await stopped_at(purger, opened, source_purge.BEFORE_REBUILD, monkeypatch, source_purge)
    assert manifest.scope is not None and manifest.file_sha256 is not None
    bounds = manifest.scope["max_ids"]
    assert await scalar(engine, _POSTINGS_MAX) < bounds["postings"]
    await backup_set(migrated, restored)
    stream = tmp_path / "shipped"
    stream.mkdir(mode=0o700)
    surviving = await lower_sequences(restored.engine, bounds)

    with capture_logs() as logs:
        await migrate_restored(make_config, restored, stream)
    rewritten = [
        e for e in logs if e["event"] in ("migrate_manifest_files_rewritten", "migrate_identities_advanced")
    ]
    assert [(e["event"], e["step"]) for e in rewritten[:2]] == [
        ("migrate_manifest_files_rewritten", "reconstruct_and_advance"),
        ("migrate_identities_advanced", "reconstruct_and_advance"),
    ]
    assert str(manifest.manifest_uuid) in rewritten[0]["manifests"]
    path = stream / manifest_file_name(manifest.manifest_uuid)
    assert hashlib.sha256(path.read_bytes()).digest() == manifest.file_sha256
    assert await below_bounds(restored.engine, bounds, surviving) == {}
    await assert_replayed(restored.engine, restored.files, manifest)


def _maximum(table: str, column: str) -> sa.TextClause:
    # Catalogue names only.
    return sa.text(f'SELECT coalesce(max("{column}"), 0) FROM public."{table}"')  # noqa: S608


def _between(table: str, column: str) -> sa.TextClause:
    # Catalogue names only.
    return sa.text(f'SELECT count(*) FROM public."{table}" WHERE "{column}" > :low AND "{column}" <= :bound')  # noqa: S608


async def lower_sequences(engine: AsyncEngine, bounds: Mapping[str, int]) -> dict[str, int]:
    """Every sequence of a table `bounds` names set back to its table's surviving maximum (or to its start
    on an empty table); those maxima."""
    surviving: dict[str, int] = {}
    async with engine.begin() as conn:
        for row in (await conn.execute(_SEQUENCES)).all():
            table, column = str(row.table_name), str(row.column_name)
            if table not in bounds:
                continue
            top = int((await conn.execute(_maximum(table, column))).scalar_one())
            await conn.execute(
                sa.text("SELECT setval(pg_get_serial_sequence(:t, :c), :v, :called)"),
                {"t": f'public."{table}"', "c": column, "v": max(top, 1), "called": top > 0},
            )
            surviving[table] = top
    return surviving


async def below_bounds(
    engine: AsyncEngine, bounds: Mapping[str, int], surviving: Mapping[str, int]
) -> dict[str, str]:
    """Per bounded table, what breaks "no id the run allocated is at or below the bound": a row above the
    table's surviving maximum and at or below the bound, or a next value at or below it."""
    wrong: dict[str, str] = {}
    async with engine.connect() as conn:
        for row in (await conn.execute(_SEQUENCES)).all():
            table, column = str(row.table_name), str(row.column_name)
            if table not in surviving:
                continue
            params = {"low": surviving[table], "bound": bounds[table]}
            reused = (await conn.execute(_between(table, column), params)).scalar_one()
            if reused:
                wrong[table] = f"{reused} rows allocated at or below {bounds[table]}"
            sequence = (
                await conn.execute(
                    sa.text("SELECT pg_get_serial_sequence(:t, :c)"), {"t": f'public."{table}"', "c": column}
                )
            ).scalar_one()
            state = (await conn.execute(sa.text(f"SELECT last_value, is_called FROM {sequence}"))).one()  # noqa: S608
            following = state.last_value + 1 if state.is_called else state.last_value
            if following <= bounds[table]:
                wrong[table] = f"next value {following} at or below {bounds[table]}"
    return wrong


async def test_a_tainted_current_revision_is_nulled_in_phase_a(
    migrated: PgServer,
    engine: AsyncEngine,
    purger: Purger,
    restored: Restored,
    make_config: ConfigFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fable N6: the mixed job's current revision has Adzuna lineage. Phase A nulls `current_revision_id`
    before deleting the revision and commits; while the job is fenced the current-score view and the Jobs
    list omit it (the view while it has no current revision, the list while its fence stands); phase B
    points it at the rebuilt revision, phase C passes, and the list shows it again."""
    seed = await seeded(engine)
    closed = seed.closed_job_id
    await backup_set(migrated, restored)
    assert closed in await listed(restored.engine)
    assert ADZUNA in await scalar(restored.engine, _LINEAGE, j=closed)
    source = await finished(purger, await request_source_purge(engine))

    pauses = PausePoints(replay.AFTER_A, replay.AFTER_B)
    monkeypatch.setattr(replay, "afault_point", pauses)
    task = asyncio.create_task(migrate_restored(make_config, restored, purger.cfg.manifests_dir))
    await asyncio.wait_for(pauses[replay.AFTER_A].reached.wait(), 120)
    assert (await one(restored.engine, JOB, j=closed)).current_revision_id is None
    assert await scalar(restored.engine, _CURRENT_SCORES, j=closed) == 0
    assert closed not in await listed(restored.engine)
    pauses[replay.AFTER_A].resume()
    await asyncio.wait_for(pauses[replay.AFTER_B].reached.wait(), 120)
    assert (await one(restored.engine, JOB, j=closed)).current_revision_id is not None
    assert await scalar(restored.engine, _CURRENT_SCORES, j=closed) == 1
    assert closed not in await listed(restored.engine)  # still fenced
    pauses[replay.AFTER_B].resume()
    await asyncio.wait_for(task, 300)

    await assert_replayed(restored.engine, restored.files, source)
    assert ADZUNA not in await scalar(restored.engine, _LINEAGE, j=closed)
    assert closed in await listed(restored.engine)


async def test_a_purged_profile_reupload_survives_the_replay(
    migrated: PgServer,
    engine: AsyncEngine,
    purger: Purger,
    restored: Restored,
    make_config: ConfigFactory,
    queue_app: procrastinate.App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex N12: a version confirmed, superseded and purged; content identical to it confirmed again
    becomes a new version with its content, while the purge's manifest was still `executing` (phase B
    done). The backup set captured both; the replay re-runs the old profile purge on its stored ids and
    leaves the new version, its tuple and its draft intact."""
    await seeded(engine)
    old = await confirm_profile(engine, "principal")
    await confirm_profile(engine, "lead")
    await confirm_profile(engine, "head")
    opened = await request_profile_purge(engine, old)
    manifest = await stopped_at(purger, opened, purge.BEFORE_COMPLETE, monkeypatch)
    assert (await one(engine, _VERSION, v=old)).redacted
    again = await confirm_profile(engine, "principal")
    new = await one(engine, _VERSION, v=again)
    assert (
        again > old
        and not new.redacted
        and new.profile_hash == (await one(engine, _VERSION, v=old)).profile_hash
    )
    new_tuples = await rows(engine, _TUPLES, v=again)
    await backup_set(migrated, restored)

    await migrate_restored(make_config, restored, purger.cfg.manifests_dir)
    await assert_replayed(restored.engine, restored.files, manifest)
    assert (await one(restored.engine, _VERSION, v=old)).redacted
    kept = await one(restored.engine, _VERSION, v=again)
    assert not kept.redacted and kept.purged_at is None
    assert await rows(restored.engine, _TUPLES, v=again) == new_tuples


@pytest.mark.parametrize("dump", ["before_the_purge", "reenabled_after_admission"])
async def test_a_source_purge_replay_reapplies_its_admission_effects(
    migrated: PgServer,
    engine: AsyncEngine,
    purger: Purger,
    restored: Restored,
    make_config: ConfigFactory,
    monkeypatch: pytest.MonkeyPatch,
    dump: Literal["before_the_purge", "reenabled_after_admission"],
) -> None:
    """A dump from before the purge holds none of A0's effects: after the replay the active settings disable
    Adzuna, `sources.purge_fence_at` is the manifest's `admitted_at`, the collection `running` in the dump is
    `cancelled('source_purge')`, and the kind's postconditions hold. A dump taken while the purge was
    executing, after a new settings version re-enabled Adzuna, keeps it enabled: that version is the user's
    later choice."""
    await seeded(engine)
    if dump == "before_the_purge":
        run_id = (await queued_run(engine)).run_id
        await backup_set(migrated, restored)
        manifest = await finished(purger, await request_source_purge(engine))
    else:
        opened = await request_source_purge(engine)
        manifest = await stopped_at(purger, opened, purge.MID_DELETE, monkeypatch)
        reenabling = await save(engine, adzuna_on, internal=True)
        await backup_set(migrated, restored)

    await migrate_restored(make_config, restored, purger.cfg.manifests_dir)
    await assert_replayed(restored.engine, restored.files, manifest)
    assert await scalar(restored.engine, _FENCE_AT) == manifest.created_at
    if dump == "before_the_purge":
        assert not await adzuna_enabled(restored.engine)
        assert tuple(await one(restored.engine, _RUN, r=run_id)) == ("cancelled", "source_purge")
    else:
        assert await adzuna_enabled(restored.engine)
        assert (await one(restored.engine, _ACTIVE_SETTINGS)).id == reenabling


async def test_delete_everything_replays_and_the_rebuild_skips_its_deleted_jobs(
    migrated: PgServer,
    engine: AsyncEngine,
    live: Purger,
    restored: Restored,
    make_config: ConfigFactory,
) -> None:
    """A source purge, then delete-everything, after the backup set: phase A runs both (delete-everything
    deletes every job of the dump), phase B finds none of the source purge's stored jobs left and rebuilds
    nothing, and phase C verifies and completes both."""
    seed = await seeded(engine)
    await backup_set(migrated, restored)
    source = await finished(live, await request_source_purge(engine))
    everything = await finished(live, await request_delete_everything(engine))

    with capture_logs() as logs:
        await migrate_restored(make_config, restored, live.cfg.manifests_dir)
    await assert_replayed(restored.engine, restored.files, source, everything)
    (report,) = events(logs, "migrate_replayed")
    assert (report["replayed"], report["rebuilt_jobs"]) == (2, 0)
    assert events(logs, "source_purge_job_rebuilt") == []
    assert await scalar(restored.engine, _JOBS) == 0
    assert not await exists(restored.engine, "jobs", seed.closed_job_id)
    # Its completing transaction's own writes ran in phase C (`CompletingKind.complete`): the dump's session
    # (created before the fence) is gone.
    assert await scalar(restored.engine, _SESSION_COUNT) == 0


_SETTINGS_VERSIONS: Final = sa.text("SELECT version_no, payload FROM settings_versions ORDER BY version_no")


@dataclasses.dataclass(frozen=True)
class SettingsOutcome:
    """What a source purge's admission and delete-everything's reset leave: the active settings document,
    every settings version (its number and document), `sources.purge_fence_at` and Adzuna's switch."""

    active: Any
    versions: list[tuple[int, Any]]
    purge_fence_at: datetime | None
    adzuna_enabled: bool


async def settings_outcome(engine: AsyncEngine) -> SettingsOutcome:
    active = (await one(engine, _ACTIVE_SETTINGS)).payload
    return SettingsOutcome(
        active=active,
        versions=[(int(r.version_no), r.payload) for r in await rows(engine, _SETTINGS_VERSIONS)],
        purge_fence_at=await scalar(engine, _FENCE_AT),
        adzuna_enabled=bool(active["sources"]["enabled"][ADZUNA]),
    )


@pytest.mark.parametrize(
    "order",
    ["source_purge_then_delete_everything", "delete_everything_then_source_purge", "crash_after_phase_a"],
)
async def test_admissions_replay_at_their_place_in_the_stream(
    migrated: PgServer,
    engine: AsyncEngine,
    live: Purger,
    restored: Restored,
    make_config: ConfigFactory,
    monkeypatch: pytest.MonkeyPatch,
    order: str,
) -> None:
    """Ruling T25-3: a backup set from before a source purge and a delete-everything, in either order (and
    the first order with a crash after phase A, then a restart), replays to the outcome the live database
    reached: the same active settings document, the same settings versions, the same `purge_fence_at` and
    Adzuna's switch. Each manifest's admission runs at its place in the stream, and the rows the replay
    created before delete-everything's turn (the source purge's re-applied settings version) fall within its
    bound, as their live counterparts did."""
    await seeded(engine)
    await backup_set(migrated, restored)
    if order == "delete_everything_then_source_purge":
        everything = await finished(live, await request_delete_everything(engine))
        source = await finished(live, await request_source_purge(engine))
        assert everything.manifest_uuid < source.manifest_uuid
    else:
        source = await finished(live, await request_source_purge(engine))
        everything = await finished(live, await request_delete_everything(engine))
    expected = await settings_outcome(engine)
    assert expected.adzuna_enabled == (order != "delete_everything_then_source_purge")

    if order == "crash_after_phase_a":
        monkeypatch.setattr(replay, "afault_point", raising_at(replay.AFTER_A))
        with pytest.raises(SimulatedCrash):
            await migrate_restored(make_config, restored, live.cfg.manifests_dir)
        monkeypatch.setattr(replay, "afault_point", no_fault)
    with capture_logs() as logs:
        await migrate_restored(make_config, restored, live.cfg.manifests_dir)
    assert await settings_outcome(restored.engine) == expected
    await assert_replayed(restored.engine, restored.files, source, everything)
    (started,) = events(logs, "replay_started")
    assert started["resumed"] is (order == "crash_after_phase_a")


async def test_a_retained_default_scope_pending_again_keeps_the_manifest_satisfied(
    engine: AsyncEngine,
    live: Purger,
    make_config: ConfigFactory,
    queue_app: procrastinate.App,
) -> None:
    """Codex round 10: after delete-everything completed, another scope is saved and applied, then the
    settings are saved back to the default document, whose scope version the purge retained below its
    bound: it is `pending_scope_version_id` again. `migrate` classifies the manifest `satisfied`, nothing is
    re-executed and the configuration stays as it is."""
    await seeded(engine)
    everything = await finished(live, await request_delete_everything(engine))
    assert everything.scope is not None
    default_scope = (
        await one(engine, sa.text("SELECT scope_version_id FROM active_config"))
    ).scope_version_id
    assert default_scope in everything.scope["retained"]["scope_versions"]
    default = (await one(engine, _ACTIVE_SETTINGS)).payload

    def narrower(payload: dict[str, Any]) -> dict[str, Any]:
        payload["scope"]["excluded_keywords"] = ["erfundenes-stichwort"]
        return payload

    await save(engine, narrower, defer=defer_in_tx)
    await apply_pending_scope(engine, live)
    assert (
        await one(engine, sa.text("SELECT scope_version_id FROM active_config"))
    ).scope_version_id != default_scope
    await save(engine, lambda _: copy.deepcopy(default), defer=defer_in_tx)
    pending = await one(engine, sa.text("SELECT pending_scope_version_id FROM active_config"))
    assert pending.pending_scope_version_id == default_scope
    configuration = [tuple(r) for r in await rows(engine, _CONFIGURATION)]

    with capture_logs() as logs:
        await migrate_live(make_config, live)
    assert events(logs, "replay_classified")[0]["outcomes"] == {"satisfied": 2}
    assert not [e for e in logs if e["event"].startswith("replay_phase")]
    assert [tuple(r) for r in await rows(engine, _CONFIGURATION)] == configuration


async def apply_pending_scope(engine: AsyncEngine, purger: Purger) -> None:
    """The pending scope's `apply_scope` operation run by the worker's handler until it flipped it."""
    op_id = (
        await one(
            engine,
            sa.text(
                "SELECT id FROM operations WHERE kind = 'apply_scope' AND status IN ('pending', 'running') "
                "ORDER BY id DESC LIMIT 1"
            ),
        )
    ).id
    for _ in range(5):
        await ops.execute_operation(engine, int(op_id), epoch=1, deps=purger.deps)
        pending = await one(engine, sa.text("SELECT pending_scope_version_id FROM active_config"))
        if pending.pending_scope_version_id is None:
            return
        async with engine.begin() as conn:
            await conn.execute(sa.text("UPDATE operations SET not_before = NULL WHERE id = :i"), {"i": op_id})
    raise AssertionError("the scope apply never flipped")


async def later_activity(
    engine: AsyncEngine, seed: SeedIds, job_with_postings: JobWithPostings, doing_job: int
) -> None:
    """Normal activity after a purge completed: rows above every bound (an alert, a job collected from
    Adzuna, re-enabled first when a source purge disabled it), an alert the purge kept resolved, a queue
    job that was `doing` finished, a lockout renewed, a session and a login attempt, a settings save, and a
    new profile confirmed with its tuple."""
    async with engine.begin() as conn:
        await conn.execute(ALERT, {"key": f"replay:{secrets.token_hex(6)}"})
        await conn.execute(_RESOLVE, {"a": seed.open_alert_id})
        await conn.execute(_FINISH_JOB, {"j": doing_job})
        for scope in (await conn.execute(_LOCKOUT_SCOPES)).scalars():
            await conn.execute(_RENEW_LOCKOUT, {"scope": scope})
        await conn.execute(_RENEW_LOCKOUT, {"scope": "ip:198.51.100.40"})
        await conn.execute(_SESSION, {"token": hashlib.sha256(secrets.token_bytes(16)).digest()})
        await conn.execute(_LOGIN_ATTEMPT)

    def interval(payload: dict[str, Any]) -> dict[str, Any]:
        payload["schedule"]["interval_hours"] = 12 if payload["schedule"]["interval_hours"] != 12 else 24
        return payload

    await save(engine, interval, defer=defer_in_tx)
    if not await adzuna_enabled(engine):
        await save(engine, adzuna_on, internal=True)
    collected = await job_with_postings({ADZUNA: {}, "greenhouse": {}})
    await recomputed(engine, collected.job_id)
    await confirm_profile(engine, "senior")


async def run_kind(engine: AsyncEngine, live: Purger, seed: SeedIds, kind: ManifestKind) -> ManifestRow:
    """A purge of `kind` on the seed, run by the worker's handler to its completion."""
    if kind == "golden_case_delete":
        opened = await request_golden(engine, seed.closed_case_id)
    elif kind == "job_tombstone":
        opened = (await request_tombstones(engine, now=utcnow())).purge
    elif kind == "profile_purge":
        opened = await request_profile_purge(engine, seed.purgeable_version_id)
    elif kind == "source_purge":
        opened = await request_source_purge(engine)
    else:
        opened = await request_delete_everything(engine)
    return await finished(live, opened)


@pytest.mark.parametrize(
    "kind", ["golden_case_delete", "job_tombstone", "profile_purge", "source_purge", "delete_everything"]
)
async def test_every_kinds_postconditions_survive_later_activity(
    engine: AsyncEngine,
    live: Purger,
    make_config: ConfigFactory,
    job_with_postings: JobWithPostings,
    queue_app: procrastinate.App,
    kind: ManifestKind,
) -> None:
    """Deviation 16 (plan review round 9): each kind's purge completed, then normal activity; every
    postcondition still holds, so `migrate` finds the manifest `satisfied` and re-executes nothing."""
    seed = await seeded(engine)
    async with engine.begin() as conn:
        doing = int((await conn.execute(_JOB_ROW)).scalar_one())
    manifest = await run_kind(engine, live, seed, kind)
    await later_activity(engine, seed, job_with_postings, doing)
    assert manifest.scope is not None
    async with engine.connect() as conn:
        assert await check_postconditions(conn, manifest.scope, files=live.file_roots) == []
        assert await KINDS[kind].postconditions(conn, manifest.scope) == []
        classified = await classify_stream(conn, live.cfg.manifests_dir, files=live.file_roots)
    assert Classified(manifest.manifest_uuid, "satisfied") in classified
    with capture_logs() as logs:
        await migrate_live(make_config, live)
    assert set(events(logs, "replay_classified")[0]["outcomes"]) == {"satisfied"}


async def test_a_completed_delete_everything_stays_satisfied_under_later_activity(
    engine: AsyncEngine,
    live: Purger,
    make_config: ConfigFactory,
    job_with_postings: JobWithPostings,
    queue_app: procrastinate.App,
) -> None:
    """Codex round 9: delete-everything completes; then a new profile is confirmed and activated and the
    settings saved, an alert it kept is resolved and a queue job that was `doing` at A2 finishes. `migrate`
    classifies the manifest `satisfied`, nothing is re-executed and the new configuration stays as it is."""
    seed = await seeded(engine)
    async with engine.begin() as conn:
        doing = int((await conn.execute(_JOB_ROW)).scalar_one())
    everything = await finished(live, await request_delete_everything(engine))
    await later_activity(engine, seed, job_with_postings, doing)
    configuration = [tuple(r) for r in await rows(engine, _CONFIGURATION)]

    with capture_logs() as logs:
        await migrate_live(make_config, live)
    outcomes = events(logs, "replay_classified")[0]["outcomes"]
    assert outcomes == {"satisfied": 2}  # delete-everything's, and the seed's shipped golden-case deletion
    assert not [e for e in logs if e["event"].startswith("replay_phase")]
    assert [tuple(r) for r in await rows(engine, _CONFIGURATION)] == configuration
    assert (await one(engine, _MANIFEST, u=everything.manifest_uuid)).replay_log == []


async def test_delete_everything_replay_makes_no_external_call(
    migrated: PgServer,
    engine: AsyncEngine,
    live: Purger,
    restored: Restored,
    make_config: ConfigFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deviation 56: a backup set holding a `submitted` batch, then delete-everything. The replay builds no
    Batches client and no pinger and sends nothing (each would fail the test), and the migrate role holds no
    Anthropic or healthchecks secret; the replay completes, the submission is gone, and the recorded cancel
    is dropped with `batch_cancel_skipped_replay` naming its ids."""
    seed = await seeded(engine, live_submission="submitted")
    await backup_set(migrated, restored)
    everything = await finished(live, await request_delete_everything(engine))

    def no_call(*args: object, **kwargs: object) -> Any:
        raise AssertionError("a replay made an external call")

    monkeypatch.setattr(BatchesClient, "__init__", no_call)
    monkeypatch.setattr(batch_results, "send_purge_cancel", no_call)
    monkeypatch.setattr(DeadmanPinger, "__init__", no_call)
    monkeypatch.setattr(DeadmanPinger, "ping_pending", no_call)
    cfg = make_config("migrate")
    assert set(cfg.secrets) == {"db_migrator"}  # no anthropic_key, no healthchecks key
    with capture_logs() as logs:
        await run_migrate_async(
            cfg,
            now=utcnow(),
            database=restored.name,
            manifest_dir=live.cfg.manifests_dir,
            cv_dir=restored.files.cv_store,
            exports_dir=restored.files.exports,
        )
    await assert_replayed(restored.engine, restored.files, everything)
    assert not await scalar(restored.engine, _SUBMISSION_EXISTS, s=seed.live_submission_id)
    skipped = events(logs, "batch_cancel_skipped_replay")
    assert [(e["submission_id"], e["provider_batch_id"]) for e in skipped] == [
        (seed.live_submission_id, SEEDED_BATCH)
    ]


async def test_replay_removes_the_export_archive_from_the_exports_volume(
    migrated: PgServer,
    engine: AsyncEngine,
    purger: Purger,
    restored: Restored,
    make_config: ConfigFactory,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Deviation 55: a golden-case deletion stopped after A3 and before its export ops (`mid_delete`), a
    `ready` export's archive still on the exports volume. `migrate` given that volume (`--exports-dir`)
    removes the archive and moves the row `purged` in phase A, before phase C completes the manifest. Its
    `file_remove` postcondition is no evidence when read in another, empty directory: there it holds before
    anything was removed."""
    seed = await seeded(engine)
    export = await one(engine, EXPORT, e=seed.ready_export_id)
    archive = restored.files.exports / export.storage_path
    archive.write_bytes(b"an encrypted archive")
    opened = await request_golden(engine, seed.closed_case_id)
    manifest = await stopped_at(purger, opened, purge.MID_DELETE, monkeypatch)
    assert manifest.scope is not None
    await backup_set(migrated, restored)
    empty = FileRoots(cv_store=restored.files.cv_store, exports=tmp_path / "empty-exports")
    empty.exports.mkdir()
    async with restored.engine.connect() as conn:
        assert [v.op for v in await check_postconditions(conn, manifest.scope, files=empty)] == [
            "field_redact"
        ]
        assert {v.op for v in await check_postconditions(conn, manifest.scope, files=restored.files)} == {
            "field_redact",
            "file_remove",
        }
    assert make_config("migrate").exports_dir != restored.files.exports  # the flag decides, not the default

    pauses = PausePoints(replay.AFTER_B)
    monkeypatch.setattr(replay, "afault_point", pauses)
    task = asyncio.create_task(migrate_restored(make_config, restored, purger.cfg.manifests_dir))
    await asyncio.wait_for(pauses[replay.AFTER_B].reached.wait(), 120)
    assert not archive.exists()
    assert tuple(await one(restored.engine, EXPORT, e=seed.ready_export_id)) == ("purged", None)
    assert (await one(restored.engine, _MANIFEST, u=manifest.manifest_uuid)).status == "executing"
    pauses[replay.AFTER_B].resume()
    await asyncio.wait_for(task, 300)
    await assert_replayed(restored.engine, restored.files, manifest)


async def test_a_replayed_kind_never_waits_on_an_eval_run_the_dump_holds_running(
    migrated: PgServer,
    engine: AsyncEngine,
    purger: Purger,
    restored: Restored,
    make_config: ConfigFactory,
) -> None:
    """Ruling T25-2 (8): the backup set holds a `running` golden eval run, which includes every active case;
    after it ended live, an active case was deleted. In the restore nothing could ever end that run (`api`
    and `worker` are stopped), so the replay ends it as the `eval_run` failure hook ends an interrupted run
    (`failed`, `metrics.failed = "interrupted"`), logs its id, and deletes the case: `migrate` completes and
    every postcondition holds, where the live purge would have waited."""
    seed = await seeded(engine)
    case = int(await scalar(engine, _ACTIVE_CASE))
    await backup_set(migrated, restored)
    async with engine.begin() as conn:  # the run ends live (as its operation's hook ends it)
        assert await fail_eval_run(conn, seed.running_eval_run_id, EVAL_RUN_INTERRUPTED)
    golden = await finished(purger, await request_golden(engine, case))
    async with restored.engine.connect() as conn:
        flags = await golden_rules.lock_case(conn, case)
        assert flags is not None and await golden_rules.in_running_run(conn, case, flags)

    with capture_logs() as logs:
        await migrate_restored(make_config, restored, purger.cfg.manifests_dir)
    await assert_replayed(restored.engine, restored.files, golden)
    (ended,) = events(logs, "golden_case_replay_eval_runs_ended")
    assert (ended["golden_case_id"], ended["eval_run_ids"]) == (case, [seed.running_eval_run_id])
    run = await one(restored.engine, _EVAL_RUN, r=seed.running_eval_run_id)
    assert (run.status, run.metrics["failed"]) == ("failed", EVAL_RUN_INTERRUPTED)
    assert not await exists(restored.engine, "golden_cases", case)


async def activate(engine: AsyncEngine, case_id: int, active: bool) -> None:
    """The user's own flag edit of the case (`golden.set_flags`)."""
    async with engine.begin() as conn:
        await golden_rules.set_flags(conn, case_id, active=active, is_canary=None, now=utcnow())


@pytest.mark.parametrize("kind", ["profile_purge", "job_tombstone"])
async def test_an_active_case_the_dump_holds_is_deactivated_and_deleted_in_a_replay(
    migrated: PgServer,
    engine: AsyncEngine,
    purger: Purger,
    restored: Restored,
    make_config: ConfigFactory,
    kind: str,
) -> None:
    """Ruling T25-4 (3): the backup set holds a golden case active that the user deactivated before a purge
    deleted it: the profile purge of the version it was labelled for, or the tombstone of the job whose
    revision it names. Live, the purge met it inactive; in the replay the dump's case is active, which both
    guards refuse live (`profile_purge._refuse_active`, `deletion_graph._refuse_active`). The replay makes it
    inactive by the user's own flag edit (`golden.set_flags`), logs `golden_case_deactivated_replay` with its
    id, and deletes it as an inactive case: `migrate` completes and every postcondition holds."""
    seed = await seeded(engine)
    case = seed.purgeable_case_id if kind == "profile_purge" else seed.closed_case_id
    await activate(engine, case, True)
    await backup_set(migrated, restored)
    await activate(engine, case, False)
    if kind == "profile_purge":
        manifest = await finished(purger, await request_profile_purge(engine, seed.purgeable_version_id))
    else:
        batch = await request_tombstones(engine, now=utcnow())
        assert seed.closed_job_id in batch.roots
        manifest = await finished(purger, batch.purge)
    async with restored.engine.connect() as conn:
        flags = await golden_rules.lock_case(conn, case)
    assert flags is not None and flags.active  # the dump's case
    # Live, each guard still refuses this case (in a transaction rolled back).
    with pytest.raises(InvariantViolation, match="active golden cases"):
        async with restored.engine.begin() as conn:
            if kind == "profile_purge":
                await deletion_graph.settle_and_delete_cases(
                    conn, [case], now=utcnow(), check=profile_purge._refuse_active
                )
            else:
                await deletion_graph._delete_cases(conn, [seed.closed_job_id], now=utcnow(), replay=False)

    with capture_logs() as logs:
        await migrate_restored(make_config, restored, purger.cfg.manifests_dir)
    await assert_replayed(restored.engine, restored.files, manifest)
    (deactivated,) = events(logs, "golden_case_deactivated_replay")
    assert deactivated["case_ids"] == [case]
    assert not await exists(restored.engine, "golden_cases", case)


async def test_a_crash_between_two_manifests_of_phase_a_resumes_the_replay(
    migrated: PgServer,
    engine: AsyncEngine,
    live: Purger,
    restored: Restored,
    make_config: ConfigFactory,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Two backup sets from before a delete-everything and a later source purge. One replay is stopped by a
    crash after delete-everything's execution, before the source purge's admission
    (`privacy.replay.after_execute`), then `migrate` runs again: it resumes the stored replay (its restore id
    and high water), applies no admission twice (one `admitted` entry per manifest, delete-everything's turn
    unchanged, the source purge's admission re-applied once), and reaches the outcome of the other set's
    uninterrupted replay, which is the live database's."""
    await seeded(engine)
    await backup_set(migrated, restored)
    other_name = restored.name.replace("jh_replay_", "jh_other_")  # the uninterrupted replay's set
    other = Restored(
        other_name,
        make_null_engine(migrated.dsn("migrator", other_name, driver="sqlalchemy")),
        FileRoots(cv_store=tmp_path / "other-cv", exports=tmp_path / "other-exports"),
    )
    for directory in (other.files.cv_store, other.files.exports):
        directory.mkdir(mode=0o700)
    try:
        await dump_and_restore(migrated, source_db=LIVE_DB, target_db=other.name)
        everything = await finished(live, await request_delete_everything(engine))
        source = await finished(live, await request_source_purge(engine))
        expected = await settings_outcome(engine)
        assert not expected.adzuna_enabled

        monkeypatch.setattr(replay, "afault_point", raising_at(replay.AFTER_EXECUTE))
        with capture_logs() as first, pytest.raises(SimulatedCrash):
            await migrate_restored(make_config, restored, live.cfg.manifests_dir)
        monkeypatch.setattr(replay, "afault_point", no_fault)
        crashed = {
            m.manifest_uuid: (await one(restored.engine, _MANIFEST, u=m.manifest_uuid)).replay_log
            for m in (everything, source)
        }
        assert [e.get("phase") for e in crashed[everything.manifest_uuid]] == [
            replay.STARTED,
            replay.ADMITTED,
        ]
        assert [e.get("phase") for e in crashed[source.manifest_uuid]] == [replay.STARTED]  # not yet admitted
        (began,) = {
            (e["restore_id"], tuple(sorted(e["high_water"].items())))
            for log_ in crashed.values()
            for e in log_
            if e.get("phase") == replay.STARTED
        }

        with capture_logs() as second:
            await migrate_restored(make_config, restored, live.cfg.manifests_dir)
        (resumed,) = events(second, "replay_started")
        assert resumed["resumed"] is True and resumed["restore_id"] == began[0]
        for manifest in (everything, source):
            log_ = (await one(restored.engine, _MANIFEST, u=manifest.manifest_uuid)).replay_log
            assert [e.get("phase") for e in log_] == [replay.STARTED, replay.ADMITTED, None]
            assert {e["restore_id"] for e in log_} == {began[0]}
            assert tuple(sorted(log_[0]["high_water"].items())) == began[1]
            if manifest is everything:
                assert log_[1] == crashed[everything.manifest_uuid][1]  # its admission, recorded once
        reapplied = events([*first, *second], "source_purge_admission_reapplied")
        assert len(reapplied) == 1
        await assert_replayed(restored.engine, restored.files, everything, source)

        await migrate_restored(make_config, other, live.cfg.manifests_dir)
        uninterrupted = await settings_outcome(other.engine)
        assert uninterrupted == expected
        assert await settings_outcome(restored.engine) == uninterrupted
    finally:
        await other.engine.dispose()
        drop_database(migrated, other.name)


async def test_restore_list_leaves_out_what_the_superuser_owns(
    migrated: PgServer, engine: AsyncEngine, restored: Restored
) -> None:
    """The filtered list of a real dump of the seeded database (`backup.restore.restore_list`): it loses
    exactly the `EXTENSION`, `COMMENT - EXTENSION` and `SCHEMA - public` entries (the three extensions and
    their comments among them) and keeps every other entry in its order, and the restore it drives succeeds
    with `--exit-on-error`, every table's rows there."""
    await seeded(engine)
    listing = await dump_listing(migrated, source_db=LIVE_DB)
    entries = [line for line in listing.splitlines() if line.strip() and not line.startswith(";")]
    markers = (" EXTENSION - ", " COMMENT - EXTENSION ", " SCHEMA - public ")
    owned = [line for line in entries if any(marker in f"{line} " for marker in markers)]
    names = {line.split()[5] for line in owned if " EXTENSION - " in f"{line} "}
    assert names == {"pg_trgm", "unaccent", "vector"} and len(owned) >= 6
    assert restore_list(listing) == [line for line in entries if line not in owned]

    await backup_set(migrated, restored)
    for table in ("settings_versions", "jobs", "postings", "deletion_manifests", "golden_cases"):
        live = await scalar(engine, sa.text(f"SELECT count(*) FROM {table}"))  # noqa: S608 (constant names)
        assert await scalar(restored.engine, sa.text(f"SELECT count(*) FROM {table}")) == live > 0  # noqa: S608
