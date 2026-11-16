"""Deletion manifests in the database (spec §7.14, §19.5; plan M5 Task 9, D1-D3, D7, Deviations 16 and 17).

A manifest is inserted `executing` with a UUIDv7 and its request (A0, with its fences), its content is
recorded with the hash of its fsync'd file (A3, which also adds the affected jobs to the source fence), and it
is completed. `check_postconditions` checks each typed operation's postcondition, bounded by the content's
`max_ids`, `fenced_at` and watermark so that later activity never breaks it, and `wait_for_quiesce` waits for
the application transactions that began before a fence committed. Every row is invented.
"""

import hashlib
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import psycopg
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db.sql import q
from jobhunter.db.transitions import transition
from jobhunter.errors import TransitionConflict
from jobhunter.privacy.fences import (
    QUIESCE_POLL_S,
    QUIESCE_TIMEOUT,
    FenceSpec,
    fence_committed_at,
    insert_fence,
    release_fences,
    wait_for_quiesce,
)
from jobhunter.privacy.manifests import (
    FileRoots,
    ManifestConflict,
    ManifestContent,
    ManifestRow,
    Operation,
    Violation,
    canonical_bytes,
    check_postconditions,
    complete_manifest,
    insert_manifest,
    max_identity_ids,
    record_manifest,
    write_manifest_file,
)
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.support.m2 import insert_llm_call, insert_profile_chain
from tests.support.m3 import Job, insert_golden_case, insert_id, insert_job, insert_published
from tests.support.pg import PgServer

pytestmark = pytest.mark.integration

_MANIFEST: Final = sa.text(
    "SELECT status, file_sha256, scope, request, created_at, completed_at "
    "FROM deletion_manifests WHERE id = :i"
)
_FENCES: Final = sa.text(
    "SELECT manifest_id, kind, job_ids, released_at, id FROM purge_fences WHERE manifest_id = :m ORDER BY id"
)


def content_of(
    manifest: ManifestRow,
    operations: list[Operation],
    *,
    max_ids: dict[str, int] | None = None,
    fenced_at: str | None = None,
    **extra: Any,
) -> ManifestContent:
    created = manifest.created_at.isoformat()
    content: dict[str, Any] = {
        "format": 1,
        "manifest_uuid": str(manifest.manifest_uuid),
        "kind": manifest.kind,
        "admitted_at": created,
        "fenced_at": fenced_at or created,
        "max_ids": max_ids or {},
        "operations": operations,
        **extra,
    }
    return content  # type: ignore[return-value]  # the test builds the TypedDict's keys above


async def a_manifest(engine: AsyncEngine, kind: Any = "source_purge", **request: Any) -> ManifestRow:
    async with engine.begin() as conn:
        return await insert_manifest(conn, kind=kind, request=request, now=utcnow())


async def manifest_row(engine: AsyncEngine, manifest_id: int) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(_MANIFEST, {"i": manifest_id})).one()


async def checked(
    engine: AsyncEngine, content: ManifestContent, files: FileRoots
) -> set[tuple[int, str, str]]:
    """The violated operations as (index, op, table): the details carry counts only, which each test reads
    where it matters."""
    async with engine.connect() as conn:
        found = await check_postconditions(conn, content, files=files)
    return {(v.op_index, v.op, v.table or "") for v in found}


@pytest.fixture
def files(tmp_path: Path) -> FileRoots:
    roots = FileRoots(cv_store=tmp_path / "cv", exports=tmp_path / "exports")
    roots.cv_store.mkdir(mode=0o700)
    roots.exports.mkdir(mode=0o700)
    return roots


# --- the lifecycle: insert (A0), record (A3), complete ------------------------------------------------------


async def test_insert_record_complete_lifecycle(engine: AsyncEngine, tmp_path: Path) -> None:
    now = utcnow()
    async with engine.begin() as conn:
        manifest = await insert_manifest(
            conn, kind="source_purge", request={"source_id": "adzuna", "idempotency_row_id": 3}, now=now
        )
    assert (manifest.kind, manifest.status, manifest.created_at) == ("source_purge", "executing", now)
    assert manifest.manifest_uuid.version == 7
    assert manifest.request == {"source_id": "adzuna", "idempotency_row_id": 3}
    assert (manifest.scope, manifest.file_sha256, manifest.completed_at, manifest.shipped_at) == (
        None,
        None,
        None,
        None,
    )
    stored = await manifest_row(engine, manifest.id)
    assert (stored.status, stored.scope, stored.created_at) == ("executing", {}, now)

    # Without its file's hash a manifest cannot complete (the §7.14 CHECK).
    with pytest.raises(sa.exc.IntegrityError):
        async with engine.begin() as conn:
            await complete_manifest(conn, manifest.id)

    content = content_of(
        manifest,
        [{"op": "fence", "kind": "source", "key": "adzuna", "job_ids": [4, 9]}],
        max_ids={"jobs": 9},
        affected={"job_ids": [4, 9], "profile_version_ids": [], "tuple_ids": []},
        watermark={"fenced_at": manifest.created_at.isoformat(), "max_ids": {"postings": 1}},
    )
    directory = tmp_path / "manifests"
    directory.mkdir(mode=0o700)
    sha = write_manifest_file(directory, content)

    # A hash that is not the content's, or a content of another manifest, is refused before any write.
    async with engine.begin() as conn:
        with pytest.raises(ValueError, match="hash"):
            await record_manifest(conn, manifest, content, hashlib.sha256(b"other").digest())
        stranger: Any = {**content, "manifest_uuid": "00000000-0000-7000-8000-000000000000"}
        with pytest.raises(ValueError, match="manifest"):
            await record_manifest(
                conn, manifest, stranger, hashlib.sha256(canonical_bytes(stranger)).digest()
            )

    async with engine.begin() as conn:
        await record_manifest(conn, manifest, content, sha)
    stored = await manifest_row(engine, manifest.id)
    assert (stored.status, bytes(stored.file_sha256), stored.scope) == ("executing", sha, content)
    file_bytes = (directory / f"{manifest.manifest_uuid}.json").read_bytes()
    assert canonical_bytes(stored.scope) == file_bytes  # `migrate` rewrites the same file from the row

    # A retry that adopted the written file records it again unchanged; other content never replaces it.
    async with engine.begin() as conn:
        await record_manifest(conn, manifest, content, sha)
    other: Any = {**content, "max_ids": {"jobs": 10}}
    with pytest.raises(ManifestConflict):
        async with engine.begin() as conn:
            await record_manifest(conn, manifest, other, hashlib.sha256(canonical_bytes(other)).digest())
    assert (await manifest_row(engine, manifest.id)).scope == content

    async with engine.begin() as conn:
        await complete_manifest(conn, manifest.id)
    stored = await manifest_row(engine, manifest.id)
    assert stored.status == "completed" and stored.completed_at is not None
    with pytest.raises(TransitionConflict):
        async with engine.begin() as conn:
            await complete_manifest(conn, manifest.id)
    # A completed manifest's content is final.
    with pytest.raises(ManifestConflict):
        async with engine.begin() as conn:
            await record_manifest(conn, manifest, content, sha)


async def test_max_identity_ids_covers_every_identity_column(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        job = await insert_job(conn)
        await conn.execute(
            sa.text(
                "INSERT INTO login_lockouts (scope, locked_until, reason) VALUES ('global', now(), 'test')"
            )
        )
        lockout = (await conn.execute(sa.text("SELECT max(generation) FROM login_lockouts"))).scalar_one()
    async with engine.connect() as conn:
        found = await max_identity_ids(conn)
        # Derived independently of the implementation: the information schema's identity columns and the
        # columns whose default draws on a sequence (`serial`, procrastinate's tables).
        columns = (
            await conn.execute(
                sa.text(
                    "SELECT table_name, column_name FROM information_schema.columns c "
                    "JOIN information_schema.tables t USING (table_schema, table_name) "
                    "WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE' "
                    "AND (c.is_identity = 'YES' OR c.column_default LIKE 'nextval(%')"
                )
            )
        ).all()
        expected = {}
        for table, column in columns:
            maximum = await conn.execute(sa.text(f'SELECT coalesce(max("{column}"), 0) FROM "{table}"'))  # noqa: S608
            expected[table] = int(maximum.scalar_one())

    assert found == expected
    assert len(columns) == len(expected)  # one identity column per table
    assert (found["jobs"], found["job_revisions"]) == (job.job_id, job.revision_id)
    assert found["login_lockouts"] == lockout  # its `generation` (Deviation 16)
    assert found["exports"] == 0  # an empty table
    assert {"api_idempotency", "procrastinate_jobs", "deletion_manifests"} <= set(found)  # surrogate, serial
    assert not {"sessions", "ad_keys", "checkpoints", "user_job_actions", "job_revision_sources"} & set(found)


async def test_record_appends_the_affected_jobs_to_the_source_fence(
    engine: AsyncEngine, tmp_path: Path
) -> None:
    async with engine.begin() as conn:
        manifest = await insert_manifest(
            conn, kind="source_purge", request={"source_id": "adzuna"}, now=utcnow()
        )
        fence = await insert_fence(
            conn,
            manifest_id=manifest.id,
            spec=FenceSpec(kind="source", source_id="adzuna", job_ids=(5, 3, 5)),
        )
        other = await insert_manifest(conn, kind="job_tombstone", request={}, now=utcnow())
        other_fence = await insert_fence(
            conn, manifest_id=other.id, spec=FenceSpec(kind="jobs", job_ids=(3,))
        )
    async with engine.connect() as conn:
        assert [tuple(r[:3]) for r in await conn.execute(_FENCES, {"m": manifest.id})] == [
            (manifest.id, "source", [3, 5])
        ]

    content = content_of(
        manifest,
        [{"op": "fence", "kind": "source", "key": "adzuna", "job_ids": [3, 7, 9]}],
        affected={"job_ids": [9, 3, 7], "profile_version_ids": [], "tuple_ids": []},
    )
    directory = tmp_path / "manifests"
    directory.mkdir(mode=0o700)
    sha = write_manifest_file(directory, content)
    for _ in range(2):  # a retry adds nothing twice
        async with engine.begin() as conn:
            await record_manifest(conn, manifest, content, sha)
    async with engine.connect() as conn:
        fences = (await conn.execute(_FENCES, {"m": manifest.id})).all()
        others = (await conn.execute(_FENCES, {"m": other.id})).all()
    # `insert_fence` returned each fence's own id; the record changed the source fence only.
    assert [(f.id, f.kind, f.job_ids, f.released_at) for f in fences] == [
        (fence, "source", [3, 5, 7, 9], None)
    ]
    assert [(f.id, f.kind, f.job_ids) for f in others] == [(other_fence, "jobs", [3])]

    async with engine.begin() as conn:
        assert await release_fences(conn, manifest.id) == 1
        assert await release_fences(conn, manifest.id) == 0
    async with engine.connect() as conn:
        released = (await conn.execute(_FENCES, {"m": manifest.id})).one()
        still = (await conn.execute(_FENCES, {"m": other.id})).one()
    assert (released.id, still.id) == (fence, other_fence)
    assert released.released_at is not None and still.released_at is None


# --- the postconditions (§19.5's column), each violated, then satisfied ------------------------------------

_SESSION: Final = q(
    "INSERT INTO sessions (token_hmac, user_id, created_at, idle_expires_at, absolute_expires_at) "
    "VALUES (CAST(:t AS bytea), 1, :at, :at + interval '1 day', :at + interval '1 day')"
)
_ALERT: Final = q(
    "INSERT INTO alerts (code, severity, dedup_key, message) VALUES ('a', 'info', :k, 'm') RETURNING id"
)
_LINEAGE: Final = q(
    "INSERT INTO job_revision_sources (job_revision_id, field, source_id, posting_id, observation_id) "
    "VALUES (:r, :field, :source, :posting, :observation)"
)
_CHECKPOINT: Final = q(
    "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, checkpoint) "
    "VALUES (:t, '', 'c1', '{}')"
)
_AD_KEY: Final = q("INSERT INTO ad_keys (key_ns, key_value, exclusive, job_id) VALUES (:ns, :v, true, :j)")
_EXPORT: Final = q(
    "INSERT INTO exports (status, expires_at) VALUES ('running', now() + interval '7 days') RETURNING id"
)
_REVISION: Final = q(
    "INSERT INTO job_revisions (job_id, revision_no, material_hash, llm_input, description_text, "
    "description_completeness, ad_language, embedding_status) "
    "VALUES (:j, :n, '\\x01', '{}', 'Erfundene Testanzeige.', 'full', 'de', 'awaiting_embedding') "
    "RETURNING id"
)


async def a_revision(conn: AsyncConnection, job_id: int, revision_no: int) -> int:
    return int((await conn.execute(_REVISION, {"j": job_id, "n": revision_no})).scalar_one())


async def export(conn: AsyncConnection, path: str, *, end: str) -> int:
    export_id = int((await conn.execute(_EXPORT)).scalar_one())
    await transition(conn, "exports", export_id, ["running"], "ready", set_={"storage_path": path})
    if end != "ready":
        await transition(conn, "exports", export_id, ["ready"], end, set_={"storage_path": None})
    return export_id


async def test_postconditions_per_op(engine: AsyncEngine, files: FileRoots) -> None:
    manifest = await a_manifest(engine, "delete_everything")
    fenced = utcnow()
    async with engine.begin() as conn:
        await insert_fence(conn, manifest_id=manifest.id, spec=FenceSpec(kind="global"))
        await conn.execute(
            sa.text(
                "INSERT INTO users (id, username, password_hash, password_changed_at) "
                "VALUES (1, 'owner', 'x', now())"
            )
        )
        job = await insert_job(conn)
        first, kept = [int((await conn.execute(_ALERT, {"k": k})).scalar_one()) for k in ("k1", "k2")]
        await conn.execute(_SESSION, {"t": "\\x01", "at": fenced - timedelta(hours=1)})
        await conn.execute(
            _LINEAGE,
            {"r": job.revision_id, "field": "title", "source": "jsearch", "posting": 1, "observation": 1},
        )
        # Within the bounds (runs 5, cycles 2, operations 5): a run, a cycle, a profile execution whose
        # subject is above the operations' bound but whose operation is not. Above them: the others.
        within = ("run-1-a1", "cycle-1", "profile-9-t4-a1")
        for thread in (*within, "run-9-a1", "cycle-3", "queries-2-q8-a0", "odd-thread"):
            await conn.execute(_CHECKPOINT, {"t": thread})
        await conn.execute(_AD_KEY, {"ns": "adzuna", "v": "a1", "j": job.job_id})
        await conn.execute(_AD_KEY, {"ns": "arbeitnow", "v": "b1", "j": job.job_id})
        chain = await insert_profile_chain(conn)
        _, call = await insert_llm_call(conn, draft_id=chain.draft_id)
        ready, expired = (
            await export(conn, "e1.age", end="ready"),
            await export(conn, "e2.age", end="expired"),
        )
    (files.cv_store / "f.bin").write_bytes(b"sealed")

    content = content_of(
        manifest,
        [
            {"op": "fence", "kind": "global", "key": "global", "job_ids": []},
            {"op": "row_delete", "table": "alerts", "ids": [first], "bound": {"by": "id"}},
            {"op": "row_delete", "table": "alerts", "selector": "all", "keep": [kept], "bound": {"by": "id"}},
            {"op": "row_delete", "table": "sessions", "selector": "all", "bound": {"by": "created_at"}},
            {
                "op": "row_delete",
                "table": "job_revision_sources",
                "selector": "all",
                "bound": {"by": "parent", "table": "job_revisions", "column": "job_revision_id"},
            },
            {"op": "row_delete", "table": "checkpoints", "selector": "all", "bound": {"by": "thread"}},
            {
                "op": "row_delete",
                "table": "ad_keys",
                "selector": "source:adzuna",
                "bound": {"by": "parent", "table": "jobs", "column": "job_id"},
            },
            {
                "op": "field_redact",
                "table": "llm_calls",
                "ids": [call],
                "columns": ["output_json"],
                "marker": {"content_purged_at": "set"},
            },
            {
                "op": "field_redact",
                "table": "exports",
                "ids": [ready, expired],
                "columns": ["storage_path"],
                "marker": {"status": "purged"},
            },
            {"op": "row_delete", "table": "job_revisions", "ids": [job.revision_id], "bound": {"by": "id"}},
            {"op": "field_redact", "table": "jobs", "ids": [job.job_id], "columns": ["current_revision_id"]},
            {"op": "file_remove", "volume": "cv_store", "keys": ["f.bin"]},
        ],
        max_ids={
            "alerts": kept,
            "job_revisions": job.revision_id,
            "jobs": job.job_id,
            "runs": 5,
            "score_cycles": 2,
            "operations": 5,
        },
        fenced_at=fenced.isoformat(),
    )
    async with engine.connect() as conn:
        violations = await check_postconditions(conn, content, files=files)
    assert {(v.op_index, v.op, v.table) for v in violations} == {
        (0, "fence", "purge_fences"),
        (1, "row_delete", "alerts"),
        (2, "row_delete", "alerts"),
        (3, "row_delete", "sessions"),
        (4, "row_delete", "job_revision_sources"),
        (5, "row_delete", "checkpoints"),
        (6, "row_delete", "ad_keys"),
        (7, "field_redact", "llm_calls"),
        (8, "field_redact", "exports"),
        (9, "row_delete", "job_revisions"),
        (10, "field_redact", "jobs"),
        (11, "file_remove", None),
    }
    # The details are counts only; a checkpoint thread of no known form is a violation of its own.
    checkpoint = sorted(v.detail for v in violations if v.table == "checkpoints")
    assert checkpoint == ["1 row of an unknown thread form", "3 rows within the bound remain"]
    assert [v.detail for v in violations if v.op == "file_remove"] == ["1 file on cv_store remains"]
    assert all(isinstance(v, Violation) and "Erfund" not in v.detail for v in violations)

    # The purge's work, then later normal activity, which no postcondition may count against the manifest.
    async with engine.begin() as conn:
        await release_fences(conn, manifest.id)
        await conn.execute(sa.text("DELETE FROM alerts WHERE id = :i"), {"i": first})
        await conn.execute(sa.text("DELETE FROM sessions"))
        await conn.execute(
            sa.text("DELETE FROM checkpoints WHERE thread_id = ANY(CAST(:t AS text[]))"),
            {"t": [*within, "odd-thread"]},
        )
        await conn.execute(sa.text("DELETE FROM ad_keys WHERE key_ns = 'adzuna'"))
        await conn.execute(
            sa.text("UPDATE llm_calls SET content_purged_at = now() WHERE id = :i"), {"i": call}
        )
        await transition(conn, "exports", ready, ["ready"], "purged", set_={"storage_path": None})
        rebuilt = await a_revision(conn, job.job_id, 2)
        await conn.execute(
            sa.text("UPDATE jobs SET current_revision_id = :r WHERE id = :j"), {"r": rebuilt, "j": job.job_id}
        )
        await conn.execute(sa.text("DELETE FROM job_revisions WHERE id = :r"), {"r": job.revision_id})
        # later activity: rows above every bound
        await conn.execute(_ALERT, {"k": "k3"})
        await conn.execute(_SESSION, {"t": "\\x02", "at": fenced + timedelta(seconds=1)})
        await conn.execute(
            _LINEAGE, {"r": rebuilt, "field": "title", "source": "jsearch", "posting": 1, "observation": 1}
        )
        later = await insert_job(conn)
        await conn.execute(_AD_KEY, {"ns": "adzuna", "v": "a2", "j": later.job_id})
    (files.cv_store / "f.bin").unlink()
    (files.cv_store / "g.bin").write_bytes(b"another file")

    assert await checked(engine, content, files) == set()


async def test_a_manifest_that_deletes_calls_checks_their_ledger_entries(
    engine: AsyncEngine, files: FileRoots
) -> None:
    """G04: a purge that deletes a call settles its ledger entry, since no reader could settle a `reserved` or
    `dispatched` entry without its call; one reserved after the manifest is not its concern."""
    manifest = await a_manifest(engine, "profile_purge")
    async with engine.begin() as conn:
        chain = await insert_profile_chain(conn)
        ledger, call = await insert_llm_call(conn, draft_id=chain.draft_id)
    content = content_of(
        manifest,
        [{"op": "row_delete", "table": "llm_calls", "ids": [call], "bound": {"by": "id"}}],
        max_ids={"llm_calls": call, "llm_spend_ledger": ledger},
    )
    assert await checked(engine, content, files) == {(0, "row_delete", "llm_calls")}

    async with engine.begin() as conn:
        await conn.execute(sa.text("DELETE FROM llm_calls WHERE id = :i"), {"i": call})
    assert await checked(engine, content, files) == {(0, "row_delete", "llm_spend_ledger")}

    async with engine.begin() as conn:
        await transition(
            conn,
            "llm_spend_ledger",
            ledger,
            ["reserved"],
            "not_billed",
            reason="never_dispatched",
            set_={"settled_usd": Decimal(0)},
        )
        # an entry reserved after the manifest, its call not written yet: not the manifest's concern
        await conn.execute(
            sa.text(
                "INSERT INTO llm_spend_ledger (budget_month, kind, purpose, purpose_class, model_requested, "
                "state, reserved_usd, admitted_cap_usd, counted_before_usd) VALUES ('2026-10-01', 'request', "
                "'extract_profile', 'nonscoring', 'claude-opus-5-5', 'reserved', 1, 100, 0)"
            )
        )
    assert await checked(engine, content, files) == set()


async def test_a_kept_revision_is_none_the_manifest_deletes(engine: AsyncEngine, files: FileRoots) -> None:
    """A `job_revisions` delete by the `all` selector keeps the revisions it lists: a job whose pointer names
    a kept one is no violation of the `jobs.current_revision_id` redaction; one naming a deleted one is."""
    manifest = await a_manifest(engine, "job_tombstone")
    async with engine.begin() as conn:
        kept, other = await insert_job(conn), await insert_job(conn)
    content = content_of(
        manifest,
        [
            {
                "op": "row_delete",
                "table": "job_revisions",
                "selector": "all",
                "keep": [kept.revision_id],
                "bound": {"by": "id"},
            },
            {
                "op": "field_redact",
                "table": "jobs",
                "ids": [kept.job_id, other.job_id],
                "columns": ["current_revision_id"],
            },
        ],
        max_ids={"job_revisions": max(kept.revision_id, other.revision_id)},
    )
    async with engine.connect() as conn:
        violations = await check_postconditions(conn, content, files=files)
    assert [(v.op_index, v.op, v.table, v.detail) for v in violations] == [
        (0, "row_delete", "job_revisions", "1 row within the bound remains"),
        (1, "field_redact", "jobs", "1 row not redacted"),  # the other job's pointer only
    ]

    # The other job rebuilt on a revision above the bound, its old one deleted.
    async with engine.begin() as conn:
        rebuilt = await a_revision(conn, other.job_id, 2)
        await conn.execute(
            sa.text("UPDATE jobs SET current_revision_id = :r WHERE id = :j"),
            {"r": rebuilt, "j": other.job_id},
        )
        await conn.execute(sa.text("DELETE FROM job_revisions WHERE id = :r"), {"r": other.revision_id})
    assert await checked(engine, content, files) == set()


# --- the `fence` postcondition of a source purge: bounded to the affected jobs and the watermark -----------

_INSTANCE: Final = q("SELECT id FROM source_instances WHERE source_id = :s AND instance_key = 'default'")
_PROVENANCE: Final = q("UPDATE jobs SET field_provenance = :p WHERE id = :j", jsonb=["p"])


def provenance(instance: int, posting: int, observation: int) -> dict[str, Any]:
    entry = {"source_instance_id": instance, "posting_id": posting, "observation_id": observation}
    return {"title": {**entry, "observed_at": "2026-11-01T00:00:00+00:00", "rule": "single"}}


async def det_result(conn: AsyncConnection, job: Job, result_id: int, sources: list[str]) -> int:
    scope = (await conn.execute(sa.text("SELECT scope_version_id FROM active_config"))).scalar_one()
    return await insert_id(
        conn,
        "det_results",
        {
            "llm_result_id": result_id,
            "scope_version_id": scope,
            "job_id": job.job_id,
            "det_algorithm_version": "det-test",
            "det_criteria": {},
            "knockouts": {},
            "industry_match": "neutral",
            "caps_applied": {},
            "det_inputs": {"job": {"title": {"value": "Testrolle", "sources": sources}}, "scope": {}},
            "det_inputs_hash": b"\x00",
        },
    )


async def test_the_source_fence_postcondition_is_bounded_to_the_affected_jobs_and_the_watermark(
    engine: AsyncEngine, files: FileRoots
) -> None:
    manifest = await a_manifest(engine, "source_purge", source_id="adzuna")
    async with engine.begin() as conn:
        adzuna = int((await conn.execute(_INSTANCE, {"s": "adzuna"})).scalar_one())
        jsearch = int((await conn.execute(_INSTANCE, {"s": "jsearch"})).scalar_one())
        affected, bystander = await insert_job(conn), await insert_job(conn)
        await insert_fence(
            conn,
            manifest_id=manifest.id,
            spec=FenceSpec(kind="source", source_id="adzuna", job_ids=(affected.job_id,)),
        )
        await conn.execute(_PROVENANCE, {"p": provenance(adzuna, 50, 150), "j": affected.job_id})
        await conn.execute(_PROVENANCE, {"p": provenance(adzuna, 50, 150), "j": bystander.job_id})
        lineage = {"r": affected.revision_id, "source": "adzuna", "posting": 50, "observation": 150}
        await conn.execute(_LINEAGE, {**lineage, "field": "title"})
        chain = await insert_profile_chain(conn)
        published = await insert_published(conn, affected, chain.llm_tuple_id)
        instance = await det_result(conn, affected, published.result_id, ["adzuna", "jsearch"])
        golden = await insert_golden_case(
            conn, affected, chain.profile_version_id, det_input_sources=["adzuna"]
        )
    watermark = {
        "postings": 100,
        "posting_observations": 200,
        "det_results": instance,
        "golden_cases": golden,
    }
    content = content_of(
        manifest,
        [{"op": "fence", "kind": "source", "key": "adzuna", "job_ids": [affected.job_id]}],
        affected={"job_ids": [affected.job_id], "profile_version_ids": [], "tuple_ids": []},
        watermark={"fenced_at": manifest.created_at.isoformat(), "max_ids": watermark},
    )
    async with engine.connect() as conn:
        violations = await check_postconditions(conn, content, files=files)
    assert sorted((v.op_index, v.op, v.table) for v in violations) == [
        (0, "fence", "det_results"),
        (0, "fence", "golden_cases"),
        (0, "fence", "job_revision_sources"),
        (0, "fence", "jobs"),
        (0, "fence", "purge_fences"),
    ]

    # The rebuild: the affected job's provenance, current lineage, current instance and golden snapshot no
    # longer draw on the source at or below the watermark. The bystander was never in the affected set.
    async with engine.begin() as conn:
        await release_fences(conn, manifest.id)
        await conn.execute(_PROVENANCE, {"p": provenance(jsearch, 50, 150), "j": affected.job_id})
        await conn.execute(sa.text("DELETE FROM job_revision_sources"))
        await conn.execute(
            sa.text("UPDATE det_results SET is_current = false, superseded_at = now() WHERE id = :i"),
            {"i": instance},
        )
        await conn.execute(
            sa.text("UPDATE golden_cases SET det_input_sources = '{jsearch}' WHERE id = :i"), {"i": golden}
        )
    assert await checked(engine, content, files) == set()

    # Data above the watermark is out of scope and preserved: a re-enabled source's new posting and
    # observation, the lineage and current instance built on them.
    async with engine.begin() as conn:
        await conn.execute(_PROVENANCE, {"p": provenance(adzuna, 101, 201), "j": affected.job_id})
        await conn.execute(_LINEAGE, {**lineage, "field": "title", "posting": 101, "observation": 201})
        await det_result(conn, affected, published.result_id, ["adzuna"])
    assert await checked(engine, content, files) == set()

    # A source fence without its watermark cannot be verified, so it never passes.
    unbounded: Any = {key: value for key, value in content.items() if key != "watermark"}
    assert await checked(engine, unbounded, files) == {(0, "fence", "jobs")}


# --- the quiesce (D1) ---------------------------------------------------------------------------------------


async def test_quiesce_waits_for_an_older_app_transaction_and_times_out(engine: AsyncEngine) -> None:
    start = utcnow()
    async with engine.connect() as older:
        await older.execute(sa.text("SELECT 1"))  # its transaction began before the fence committed
        since = await fence_committed_at(engine)
        assert since > start
        async with engine.connect() as newer:
            await newer.execute(sa.text("SELECT 1"))  # began after: it saw the fence, so nothing waits for it
            slept: list[float] = []

            async def sleep(seconds: float) -> None:
                slept.append(seconds)
                if len(slept) == 2:
                    await older.commit()

            assert await wait_for_quiesce(engine, since=since, clock=lambda: start, sleep=sleep) is True
            assert slept == [QUIESCE_POLL_S, QUIESCE_POLL_S]

    async with engine.connect() as stuck:
        await stuck.execute(sa.text("SELECT 1"))
        since = await fence_committed_at(engine)
        now = [start]
        waited: list[float] = []

        async def tick(seconds: float) -> None:
            waited.append(seconds)
            now[0] += timedelta(minutes=4)

        assert await wait_for_quiesce(engine, since=since, clock=lambda: now[0], sleep=tick) is False
        assert now[0] - start >= QUIESCE_TIMEOUT and len(waited) == 3
        await stuck.rollback()

    # With no older transaction open, the wait ends at once.
    async def never(seconds: float) -> None:
        raise AssertionError("nothing to wait for")

    assert (
        await wait_for_quiesce(engine, since=await fence_committed_at(engine), clock=utcnow, sleep=never)
        is True
    )


async def test_quiesce_ignores_a_transaction_in_another_database(
    engine: AsyncEngine, migrated: PgServer
) -> None:
    """T9-1: a transaction of the application role in another database of the server cannot touch this one's
    rows, so the wait does not wait for it, however old."""
    async with await psycopg.AsyncConnection.connect(migrated.dsn("app", "postgres")) as elsewhere:
        await elsewhere.execute("SELECT 1")  # its transaction began before the fence committed
        assert elsewhere.info.transaction_status == psycopg.pq.TransactionStatus.INTRANS
        since = await fence_committed_at(engine)
        async with engine.connect() as conn:
            older = await conn.execute(
                sa.text(
                    "SELECT datname FROM pg_stat_activity WHERE usename = 'jobhunter_app' "
                    "AND pid <> pg_backend_pid() AND xact_start < :since"
                ),
                {"since": since},
            )
            assert "postgres" in older.scalars().all()  # visible to the poll: only its database excludes it

        async def never(seconds: float) -> None:
            raise AssertionError("a transaction in another database held the wait")

        assert await wait_for_quiesce(engine, since=since, clock=utcnow, sleep=never) is True
        await elsewhere.rollback()
