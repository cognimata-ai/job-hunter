"""The Adzuna purge (spec §20.5, §19.5 step 3, §18.2; plan M5 Task 15, D10, D22, Deviations 13, 23, 50, 51, 53
and 54): `privacy.kinds.source_purge`.

A0 (`request_source_purge`) runs in the test's transaction; each `purge` operation is run as the worker runs
it (`tests.support.purges`' `Purger`): the quiesce, the canonical file, phase B (the derivatives, the source
data, the rebuild, the embeddings, the exports) and phase C's verified completion. The seed-backed tests run
on the two-year-old seed (`tests.support.privacy_seed`): every table holds a row, the mixed job `closed_job`
(an Adzuna and an ATS posting) is rebuilt and the Adzuna-only jobs are deleted against the real schema with
every foreign key, CHECK and trigger enabled. The tests of one mechanism (a salary's provenance, an eval call,
an embedding reused, the fence's consumers) add the rows they name, each job built by the real canonical
selection (`dedup.revisions.recompute_job`) over its postings. Every row is invented.
"""

import asyncio
import hashlib
import json
import uuid
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Final, cast

import procrastinate
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.dedup import revisions
from jobhunter.embeddings import EmbedderClient, make_http_client
from jobhunter.errors import Refusal
from jobhunter.graphs.collect.nodes.enqueue_targets import make_enqueue_targets
from jobhunter.graphs.collect.state import initial_state
from jobhunter.graphs.score.nodes import claim as claim_node
from jobhunter.privacy import deletion_graph, purge
from jobhunter.privacy.kinds import source_purge
from jobhunter.privacy.manifests import ManifestRow, check_postconditions
from jobhunter.privacy.purge import OpenedPurge, PurgeRun
from jobhunter.scoring import filters
from jobhunter.scoring import reconcilers as scoring_reconcilers  # registers `missing_targets`
from jobhunter.scoring.admission import Picked
from jobhunter.settings.active import lock_active
from jobhunter.settings.save import SaveRequest, save_settings
from jobhunter.sources.base import SourceAdapter
from jobhunter.sources.instances import InstanceRow
from jobhunter.worker.embedding_reconcile import reconcile_awaiting_embedding
from jobhunter.worker.runs import RunOwner
from jobhunter.worker.tasks import defer_in_tx
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.support.collect import JobWithPostings, add_posting, ctx, make_deps, queued_run
from tests.support.eval import case_rows, eval_deps, execute, run_row, seed_adversarial, start_eval
from tests.support.faults import SimulatedCrash, no_fault, raising_at
from tests.support.fences import insert_test_fence
from tests.support.groups import FULL_TEXT, canonical_job
from tests.support.llm import FakeLLM
from tests.support.m2 import insert_eval_run, insert_profile_chain
from tests.support.m3 import Job, active_ids, insert_cycle, insert_golden_case, insert_job
from tests.support.observations import stated
from tests.support.privacy_seed import SEED_AGE, SeedIds, seed_two_year_old_graph
from tests.support.purges import Purger, completed, manifest_of
from tests.support.reconcile import run_reconciler
from tests.support.scoring import (
    activate_score_tuple,
    activate_test_policy,
    claimed_target,
    embed_revision,
    embedding_at,
    insert_targets,
    invoke_subgraph,
    member_group,
    profile_of_tuple,
    profile_vector_at,
    published_result,
    score_deps,
    score_llm_tuple,
    synthetic_c_draft,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("current_epoch")]

ADZUNA: Final = "adzuna"
EMBEDDING_GRACE: Final = timedelta(minutes=11)  # past the `awaiting_embedding` reconciler's grace
_OPENED: Final = sa.text(
    "SELECT m.id AS manifest_id, o.id AS operation_id FROM deletion_manifests m "
    "JOIN operations o ON o.kind = 'purge' AND o.payload -> 'manifest_id' = to_jsonb(m.id) "
    "WHERE m.kind = 'source_purge' ORDER BY m.id"
)
_ACTIVE_SETTINGS: Final = sa.text(
    "SELECT v.id, v.payload, v.created_at, v.base_version_id FROM active_config a "
    "JOIN settings_versions v ON v.id = a.settings_version_id WHERE a.id = 1"
)
_VERSIONS: Final = sa.text("SELECT count(*) FROM settings_versions")
_FENCE_AT: Final = sa.text("SELECT purge_fence_at FROM sources WHERE id = 'adzuna'")
_FENCES: Final = sa.text(
    "SELECT kind, source_id, job_ids, released_at FROM purge_fences WHERE manifest_id = :m ORDER BY id"
)
_RUN: Final = sa.text("SELECT status, status_reason, finished_at FROM runs WHERE id = :r")
_ATTEMPT: Final = sa.text(
    "SELECT ended_at, end_reason FROM run_attempts WHERE run_id = :r AND attempt_no = 1"
)
_QUEUED_RUN: Final = sa.text(
    "INSERT INTO runs (trigger, status, include_paid, settings_version_id, scope_version_id, queued_at) "
    "SELECT 'manual', 'queued', false, settings_version_id, scope_version_id, :at FROM active_config "
    "RETURNING id"
)
_EXISTING: Final = sa.text("SELECT id FROM jobs WHERE id = ANY(CAST(:ids AS bigint[])) ORDER BY id")
_JOB: Final = sa.text(
    "SELECT j.current_revision_id, j.salary, j.source_ids, j.field_provenance, j.merged_into_job_id, "
    "j.title FROM jobs j WHERE j.id = :j"
)
_LINEAGE: Final = sa.text(
    "SELECT array_agg(DISTINCT source_id ORDER BY source_id) FROM job_revision_sources "
    "WHERE job_revision_id = :r"
)
_PROVENANCE_SOURCES: Final = sa.text(
    "SELECT coalesce(array_agg(DISTINCT si.source_id ORDER BY si.source_id), '{}') FROM jobs j "
    "CROSS JOIN LATERAL jsonb_each(j.field_provenance) e JOIN source_instances si "
    "ON si.id = CAST(e.value ->> 'source_instance_id' AS bigint) WHERE j.id = :j"
)
_ADD_LINEAGE: Final = sa.text(
    "INSERT INTO job_revision_sources (job_revision_id, field, source_id, posting_id, observation_id) "
    "VALUES (:r, 'title', 'adzuna', :p, :o)"
)
_ADZUNA_ROWS: Final = sa.text(
    "SELECT (SELECT count(*) FROM postings p JOIN source_instances si ON si.id = p.source_instance_id "
    "WHERE si.source_id = 'adzuna') AS postings, (SELECT count(*) FROM ad_keys WHERE key_ns = 'adzuna') AS "
    "keys, (SELECT count(*) FROM fetch_requests f JOIN source_instances si ON si.id = f.source_instance_id "
    "WHERE si.source_id = 'adzuna') AS fetch_requests, (SELECT count(*) FROM job_revision_sources "
    "WHERE source_id = 'adzuna') AS lineage"
)
_CASE_ROWS: Final = sa.text(
    "SELECT (SELECT count(*) FROM golden_cases WHERE id = :g) + (SELECT count(*) FROM eval_case_results "
    "WHERE golden_case_id = :g) + (SELECT count(*) FROM llm_calls WHERE golden_case_id = :g)"
)
_INSTANCES: Final = sa.text(
    "SELECT id, is_current, det_inputs, scope_version_id FROM det_results WHERE llm_result_id = :r "
    "ORDER BY id"
)
_ENTRY: Final = sa.text("SELECT l.state, l.evidence FROM llm_spend_ledger l WHERE l.id = :l")
_CALL_EXISTS: Final = sa.text("SELECT EXISTS (SELECT 1 FROM llm_calls WHERE id = :c)")
_UNSETTLED_WITHOUT_CALL: Final = sa.text(
    "SELECT count(*) FROM llm_spend_ledger l WHERE l.kind = 'request' AND l.state IN ('reserved', "
    "'dispatched') AND NOT EXISTS (SELECT 1 FROM llm_calls c WHERE c.spend_ledger_id = l.id)"
)
_TARGETS_OF_REVISIONS: Final = sa.text(
    "SELECT count(*) FROM score_targets WHERE job_revision_id = ANY(CAST(:r AS bigint[]))"
)
_RESULTS_OF_REVISIONS: Final = sa.text(
    "SELECT count(*) FROM llm_results WHERE job_revision_id = ANY(CAST(:r AS bigint[]))"
)
_REVISIONS_OF: Final = sa.text("SELECT id FROM job_revisions WHERE job_id = :j ORDER BY id")
_TOMBSTONES_OF: Final = sa.text(
    "SELECT count(*) FROM job_tombstones WHERE former_job_id = ANY(CAST(:ids AS bigint[]))"
)
_GROUP: Final = sa.text(
    "SELECT g.status, g.representative_job_id, (SELECT array_agg(m.job_id ORDER BY m.job_id) "
    "FROM duplicate_group_members m WHERE m.group_id = g.id) AS members FROM duplicate_groups g "
    "WHERE g.id = :g"
)
_EVENTS_NAMING: Final = sa.text(
    "SELECT count(*) FROM merge_events WHERE from_job_id = ANY(CAST(:ids AS bigint[])) "
    "OR into_job_id = ANY(CAST(:ids AS bigint[]))"
)
_REVISION_EMBEDDING: Final = sa.text("SELECT embedding_status, embedding_id FROM job_revisions WHERE id = :r")
_EMBEDDING_EXISTS: Final = sa.text("SELECT EXISTS (SELECT 1 FROM embeddings WHERE id = :e)")
_TARGETS_OF_JOB: Final = sa.text(
    "SELECT job_revision_id, state FROM score_targets WHERE job_id = :j AND llm_tuple_id = :t ORDER BY id"
)
_ACTIVE_TUPLE: Final = sa.text("SELECT llm_tuple_id FROM active_config WHERE id = 1")
_EXPORT: Final = sa.text("SELECT status, storage_path FROM exports WHERE id = :e")
_SUBMISSION: Final = sa.text("SELECT state, cancel_requested_at FROM batch_submissions WHERE id = :s")
_TARGETS_IN: Final = sa.text(
    "SELECT t.job_id, t.state FROM score_targets t WHERE t.batch_submission_id = :s ORDER BY t.job_id"
)
_PURGED_ITEMS: Final = sa.text(
    "SELECT custom_id FROM purged_batch_items WHERE manifest_id = :m ORDER BY custom_id"
)
_ITEMS_OF_JOB: Final = sa.text(
    "SELECT i.custom_id FROM batch_items i JOIN score_targets t ON t.id = i.target_id "
    "WHERE t.job_id = :j ORDER BY i.custom_id"
)
_LOCK_CONFIG: Final = sa.text("SELECT 1 FROM active_config WHERE id = 1 FOR UPDATE")
_CONFLICT: Final = sa.text("SELECT id FROM identity_conflicts WHERE id = :c")
_LINEAGE_POSTINGS: Final = sa.text("SELECT posting_id FROM job_revision_sources WHERE job_revision_id = :r")
_SYNTHETIC_CASES: Final = sa.text(
    "SELECT g.id, r.job_id FROM golden_cases g JOIN job_revisions r ON r.id = g.job_revision_id "
    "JOIN jobs j ON j.id = r.job_id WHERE j.synthetic_eval ORDER BY g.id"
)
_DRAWS_ON_ADZUNA: Final = sa.text("UPDATE golden_cases SET det_input_sources = '{adzuna}' WHERE id = :g")
_CASE_EXISTS: Final = sa.text("SELECT EXISTS (SELECT 1 FROM golden_cases WHERE id = :g)")
# What an A0 writes: the settings versions and the active one, `purge_fence_at`, the source purges and fences.
_A0_TRACES: Final = sa.text(
    "SELECT (SELECT count(*) FROM settings_versions) AS versions, "
    "(SELECT settings_version_id FROM active_config WHERE id = 1) AS active, "
    "(SELECT purge_fence_at FROM sources WHERE id = 'adzuna') AS fence_at, "
    "(SELECT count(*) FROM deletion_manifests WHERE kind = 'source_purge') AS manifests, "
    "(SELECT count(*) FROM purge_fences) AS fences"
)
_ALERT_RESOLVED: Final = sa.text("SELECT resolved_at IS NOT NULL FROM alerts WHERE dedup_key = :k")
_POINT_SETTINGS: Final = sa.text("UPDATE active_config SET settings_version_id = :v WHERE id = 1")
_UNFENCE_SOURCE: Final = sa.text("UPDATE sources SET purge_fence_at = NULL WHERE id = 'adzuna'")
_TOUCH: Final = sa.text("UPDATE jobs SET updated_at = now() WHERE id = :j")
_FEEDBACK: Final = sa.text("SELECT score_snapshot, kind, label, comment FROM score_feedback WHERE id = :f")
_FEEDBACK_OF_JOB: Final = sa.text("SELECT id FROM score_feedback WHERE job_id = :j")
_ADD_FEEDBACK: Final = sa.text(
    "INSERT INTO score_feedback (job_id, job_revision_id, llm_result_id, det_result_id, score_snapshot, "
    "kind, label, comment) VALUES (:job, :revision, :result, :det, CAST(:snapshot AS jsonb), 'label', "
    ":label, 'Erfunden.') RETURNING id"
)
_SNAPSHOT_AGAIN: Final = sa.text(
    """UPDATE score_feedback SET score_snapshot = '{"M": "70.00"}' WHERE id = :f"""
)
_SNAPSHOT_GONE: Final = sa.text("UPDATE score_feedback SET score_snapshot = NULL WHERE id = :f")


# ---- helpers -----------------------------------------------------------------------------------------------


async def seeded(engine: AsyncEngine, now: datetime | None = None, **kwargs: Any) -> SeedIds:
    async with engine.begin() as conn:
        return await seed_two_year_old_graph(conn, now=utcnow() if now is None else now, **kwargs)


async def request(engine: AsyncEngine, *, now: datetime | None = None) -> OpenedPurge:
    """A0 as a caller runs it: `request_source_purge` in a transaction of its own (no deferrer: the purger
    runs the operation)."""
    async with engine.begin() as conn:
        return await source_purge.request_source_purge(
            conn, source_id=ADZUNA, now=utcnow() if now is None else now, defer=None
        )


async def purged(engine: AsyncEngine, purger: Purger, opened: OpenedPurge) -> ManifestRow:
    """The purge `opened` run to its completion; its manifest, asserted completed with every typed
    postcondition holding (`tests.support.purges.completed`) and the kind's own."""
    await purger.run(opened.operation_id)
    manifest = await completed(purger, opened.manifest_id, opened.operation_id)
    assert manifest.scope is not None
    async with engine.connect() as conn:
        assert await source_purge.SourcePurge().postconditions(conn, manifest.scope) == []
    return manifest


async def one(engine: AsyncEngine, statement: sa.TextClause, **params: object) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(statement, params)).one()


async def scalar(engine: AsyncEngine, statement: sa.TextClause, **params: object) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(statement, params)).scalar_one_or_none()


async def ids(engine: AsyncEngine, statement: sa.TextClause, **params: object) -> list[int]:
    async with engine.connect() as conn:
        return [int(i) for i in (await conn.execute(statement, params)).scalars()]


async def existing(engine: AsyncEngine, job_ids: list[int] | tuple[int, ...]) -> list[int]:
    return await ids(engine, _EXISTING, ids=list(job_ids))


def operations(manifest: ManifestRow, op: str, table: str) -> list[int]:
    """The ids the manifest's `op` operations on `table` name."""
    assert manifest.scope is not None
    named: list[int] = []
    for each in manifest.scope["operations"]:
        found = cast(dict[str, Any], each)
        if found["op"] == op and found.get("table") == table:
            named += found.get("ids", [])
    return sorted(named)


def affected(manifest: ManifestRow) -> list[int]:
    assert manifest.scope is not None and "affected" in manifest.scope
    return list(manifest.scope["affected"]["job_ids"])


async def adzuna_enabled(engine: AsyncEngine) -> bool:
    row = await one(engine, _ACTIVE_SETTINGS)
    return bool(row.payload["sources"]["enabled"]["adzuna"])


async def provenance_sources(engine: AsyncEngine, job_id: int) -> list[str]:
    return list(await scalar(engine, _PROVENANCE_SOURCES, j=job_id))


async def lineage(engine: AsyncEngine, revision_id: int) -> list[str]:
    return list(await scalar(engine, _LINEAGE, r=revision_id) or [])


async def recomputed(engine: AsyncEngine, job_id: int) -> int:
    """The job's canonical selection and its det-facts hook, as identity resolution runs them: the current
    revision afterwards."""
    async with engine.begin() as conn:
        active = await filters.lock_active_scopes(conn)
        result = await revisions.recompute_job(conn, job_id)
        await filters.refilter_job_live(conn, job_id, active=active)
    return result.revision_id


async def mixed_job(job_with_postings: JobWithPostings, engine: AsyncEngine, **adzuna: Any) -> Job:
    """A job with a Greenhouse posting (title, description) and an Adzuna one (`adzuna`'s overrides, with a
    short partial description, so the Greenhouse one is the job's text), selected by `recompute_job`."""
    made = await job_with_postings(
        {
            "greenhouse": {},
            ADZUNA: {"description_text": "Kurz.", "description_completeness": "partial", **adzuna},
        }
    )
    return Job(made.job_id, await recomputed(engine, made.job_id))


async def draw_on_adzuna(conn: AsyncConnection, job: Job) -> int:
    """An Adzuna posting on the job and a lineage row of its current revision naming it (the revision is
    tainted); the posting's id."""
    posting, observation = await add_posting(conn, job.job_id, ADZUNA, observed_at=utcnow())
    await conn.execute(_ADD_LINEAGE, {"r": job.revision_id, "p": posting, "o": observation})
    return posting


async def seed_revision(engine: AsyncEngine, job_id: int) -> int:
    row = await one(engine, _JOB, j=job_id)
    return int(row.current_revision_id)


# ---- A0 ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["queued", "running"])
async def test_a0_disables_adzuna_by_a_new_settings_version_and_cancels_the_active_run(
    engine: AsyncEngine, state: str
) -> None:
    """One transaction under `active_config FOR UPDATE`: a new internal settings version (the active one with
    Adzuna disabled, based on it), `purge_fence_at`, the one active collection `cancelled('source_purge')`
    (a running one's attempt closed `cancelled`), the `executing` manifest with its request and an empty
    `source` fence, and the `purge` operation; the three audit values are A0's one `now`."""
    await seeded(engine)
    if state == "running":
        run_id = (await queued_run(engine)).run_id
    else:
        async with engine.begin() as conn:
            run_id = int((await conn.execute(_QUEUED_RUN, {"at": utcnow()})).scalar_one())
    before = await one(engine, _ACTIVE_SETTINGS)
    assert before.payload["sources"]["enabled"]["adzuna"] is True
    now = utcnow()
    opened = await request(engine, now=now)

    after = await one(engine, _ACTIVE_SETTINGS)
    assert after.id != before.id and after.base_version_id == before.id and after.created_at == now
    expected = {
        **before.payload,
        "sources": {"enabled": {**before.payload["sources"]["enabled"], "adzuna": False}},
    }
    assert after.payload == expected  # the active document, Adzuna off and nothing else
    assert await scalar(engine, _FENCE_AT) == now
    run = await one(engine, _RUN, r=run_id)
    assert (run.status, run.status_reason) == ("cancelled", "source_purge") and run.finished_at is not None
    if state == "running":
        attempt = await one(engine, _ATTEMPT, r=run_id)
        assert attempt.end_reason == "cancelled" and attempt.ended_at is not None
    manifest = await manifest_of(engine, opened.manifest_id)
    assert (manifest.kind, manifest.status, manifest.created_at) == ("source_purge", "executing", now)
    assert dict(manifest.request) == {"source_id": ADZUNA} and manifest.scope is None
    async with engine.connect() as conn:
        fences = [tuple(r) for r in await conn.execute(_FENCES, {"m": opened.manifest_id})]
    assert fences == [("source", ADZUNA, [], None)]
    assert [(r.manifest_id, r.operation_id) for r in await opened_purges(engine)] == [
        (opened.manifest_id, opened.operation_id)
    ]


async def opened_purges(engine: AsyncEngine) -> list[Any]:
    async with engine.connect() as conn:
        return list((await conn.execute(_OPENED)).all())


async def test_a_second_request_while_executing_is_409(engine: AsyncEngine) -> None:
    """While a source purge is `executing`, another request is 409 `conflict_state` (`purge_in_progress`),
    with nothing written: no settings version, no manifest."""
    await seeded(engine)
    await request(engine)
    versions = await scalar(engine, _VERSIONS)
    with pytest.raises(Refusal) as refused:
        await request(engine)
    assert (refused.value.status, refused.value.code, refused.value.detail) == (
        409,
        "conflict_state",
        "purge_in_progress",
    )
    assert await scalar(engine, _VERSIONS) == versions and len(await opened_purges(engine)) == 1


async def test_request_source_purge_is_refused_under_a_global_fence(engine: AsyncEngine) -> None:
    """Delete-everything's `global` fence: 409 `delete_everything_in_progress`, Adzuna still enabled, no
    manifest of the kind."""
    await seeded(engine)
    async with engine.begin() as conn:
        await insert_test_fence(conn, kind="global")
    with pytest.raises(Refusal) as refused:
        await request(engine)
    assert (refused.value.status, refused.value.code) == (409, "delete_everything_in_progress")
    assert await adzuna_enabled(engine) and await opened_purges(engine) == []
    assert await scalar(engine, _FENCE_AT) is None


async def test_a0_is_refused_when_adzuna_is_the_only_enabled_source(engine: AsyncEngine) -> None:
    """The settings rule "at least one source must be enabled" refuses the version that would disable Adzuna:
    A0 answers 422 `validation_failed` on `payload.sources.enabled` and leaves nothing behind (no settings
    version, `purge_fence_at` as it was, the queued collection still queued, no manifest, no fence)."""
    async with engine.begin() as conn:
        active = await lock_active(conn)
        payload = cast(dict[str, Any], active.payload)
        only = {name: name == ADZUNA for name in payload["sources"]["enabled"]}
        request_ = SaveRequest(
            payload={**payload, "sources": {**payload["sources"], "enabled": only}},
            base_version_id=active.settings_version_id,
            acknowledged_warnings=frozenset(),
        )
        await save_settings(conn, request_, now=utcnow(), defer=defer_in_tx, internal=True)
        run_id = int((await conn.execute(_QUEUED_RUN, {"at": utcnow()})).scalar_one())
    before = await one(engine, _A0_TRACES)
    with pytest.raises(Refusal) as refused:
        await request(engine)
    assert (refused.value.status, refused.value.code) == (422, "validation_failed")
    assert "payload.sources.enabled" in {field.field for field in refused.value.fields or ()}
    assert tuple(await one(engine, _A0_TRACES)) == tuple(before)
    assert tuple(await one(engine, _RUN, r=run_id))[:2] == ("queued", None)


# ---- the stored affected set -------------------------------------------------------------------------------


async def test_the_affected_set_is_stored_once_and_reused(
    engine: AsyncEngine, purger: Purger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A2 stores the affected jobs (every job with an Adzuna posting, a tainted revision or a golden case
    drawing on Adzuna, with the job it is merged into) on the manifest and on the fence. A crash after the
    source data went: by then a re-derivation would miss the jobs whose Adzuna posting was their only link,
    yet the retry rebuilds exactly the stored set, the mixed job included, and every one stays fenced."""
    seed = await seeded(engine)
    opened = await request(engine)
    monkeypatch.setattr(source_purge, "afault_point", raising_at(source_purge.BEFORE_REBUILD))
    await purger.run(opened.operation_id)
    manifest = await manifest_of(engine, opened.manifest_id)
    stored = affected(manifest)
    expected = sorted(
        {
            seed.closed_job_id,  # the mixed job: a posting, its tainted revision and its cases
            *seed.adzuna_only_job_ids,
            seed.adzuna_merged_job_id,  # a tainted revision, merged into an Adzuna-only job
            seed.golden_job_id,  # a golden case drawing on Adzuna
        }
    )
    assert stored == expected
    async with engine.connect() as conn:
        (fence,) = (await conn.execute(_FENCES, {"m": opened.manifest_id})).all()
        rows = (await conn.execute(_ADZUNA_ROWS)).one()
        derived = await source_purge.SourcePurge().compute(conn, manifest, now=utcnow())
    assert fence.job_ids == expected and fence.released_at is None
    assert (rows.postings, rows.keys, rows.fetch_requests) == (0, 0, 0)  # the source data went
    missed = set(expected) - set(derived["affected"]["job_ids"])
    assert {seed.active_job_id, seed.golden_job_id} <= missed  # a re-derivation now would miss them
    assert await existing(engine, [seed.closed_job_id]) == [seed.closed_job_id]

    monkeypatch.setattr(source_purge, "afault_point", no_fault)
    await purger.again(opened.operation_id)
    done = await completed(purger, opened.manifest_id, opened.operation_id)
    assert done.scope == manifest.scope  # the stored content, never re-derived
    assert await existing(engine, expected) == [seed.closed_job_id]  # rebuilt; the others deleted
    closed = await one(engine, _JOB, j=seed.closed_job_id)
    assert await lineage(engine, int(closed.current_revision_id)) == ["greenhouse"]
    assert await provenance_sources(engine, seed.closed_job_id) == ["greenhouse"]


# ---- the rebuild -------------------------------------------------------------------------------------------


async def test_a_mixed_source_job_is_rebuilt_without_the_adzuna_salary(
    engine: AsyncEngine, purger: Purger, job_with_postings: JobWithPostings
) -> None:
    """A mixed job whose text comes from Greenhouse (its revision untainted) and whose salary from Adzuna:
    its det instance names Adzuna, so it is deleted and computed again from the remaining data; its golden
    case's frozen snapshot draws on Adzuna, so the case goes. The job keeps its revision (no Adzuna lineage),
    and its canonical fields, its new instance and its provenance hold no Adzuna value."""
    await seeded(engine)
    tuple_id = int(await scalar(engine, _ACTIVE_TUPLE))  # the seed's active tuple
    async with engine.begin() as conn:
        chain = await insert_profile_chain(conn, 91)
    job = await mixed_job(job_with_postings, engine, salary=stated(60_000, 70_000))
    assert await lineage(engine, job.revision_id) == ["greenhouse"]  # untainted
    before = await one(engine, _JOB, j=job.job_id)
    assert before.salary is not None and await provenance_sources(engine, job.job_id) == [
        ADZUNA,
        "greenhouse",
    ]
    async with engine.begin() as conn:
        result = (await published_result(conn, job, tuple_id)).result_id
    await recomputed(engine, job.job_id)  # the det-facts hook writes the result's instances
    async with engine.connect() as conn:
        (instance,) = [r for r in (await conn.execute(_INSTANCES, {"r": result})).all() if r.is_current]
    assert instance.det_inputs["job"]["salary"]["sources"] == [ADZUNA]
    async with engine.begin() as conn:
        case = await insert_golden_case(
            conn,
            job,
            chain.profile_version_id,
            det_inputs_snapshot=instance.det_inputs,
            det_input_sources=[ADZUNA],
        )

    manifest = await purged(engine, purger, await request(engine))
    assert job.job_id in affected(manifest)
    assert operations(manifest, "row_delete", "det_results") == [int(instance.id)]
    assert case in operations(manifest, "row_delete", "golden_cases")
    after = await one(engine, _JOB, j=job.job_id)
    assert after.current_revision_id == job.revision_id  # reused: its lineage names no Adzuna source
    assert after.salary is None and list(after.source_ids) == ["greenhouse"]
    assert await provenance_sources(engine, job.job_id) == ["greenhouse"]
    async with engine.connect() as conn:
        instances = (await conn.execute(_INSTANCES, {"r": result})).all()
        assert (await conn.execute(_CASE_ROWS, {"g": case})).scalar_one() == 0
    assert int(instance.id) not in {int(r.id) for r in instances}
    current = [r for r in instances if r.is_current]
    assert current and all(r.det_inputs["job"]["salary"] == {"value": None, "sources": []} for r in current)


async def add_feedback(
    conn: AsyncConnection,
    job: Job,
    *,
    result_id: int | None = None,
    det_result_id: int | None = None,
    label: str = "ok",
) -> int:
    """A label of `job`'s revision with a score snapshot as `golden.record_feedback` writes it (invented
    scores); its id."""
    snapshot = json.dumps(
        {"L": "51.00", "M": "62.00", "U": "73.00", "coverage": "0.80", "confidence": "medium"}
    )
    return int(
        (
            await conn.execute(
                _ADD_FEEDBACK,
                {
                    "job": job.job_id,
                    "revision": job.revision_id,
                    "result": result_id,
                    "det": det_result_id,
                    "snapshot": snapshot,
                    "label": label,
                },
            )
        ).scalar_one()
    )


async def test_feedback_score_snapshots_drawn_from_adzuna_are_redacted_keeping_the_labels(
    engine: AsyncEngine, purger: Purger, job_with_postings: JobWithPostings
) -> None:
    """Final review I1: `score_feedback` copies a scored result's L, M, U, coverage and confidence. The purge
    freezes, before anything is detached, the feedback whose snapshot draws on Adzuna: the seed's label of
    the mixed job's tainted revision, and a label whose det instance names Adzuna (an Adzuna salary on an
    untainted revision). Their `score_snapshot` is NULL afterwards (manifest operation `field_redact`), their
    kind, label and comment kept; a label drawing on no Adzuna value keeps its snapshot."""
    seed = await seeded(engine)
    tuple_id = int(await scalar(engine, _ACTIVE_TUPLE))
    seeded_feedback = int(await scalar(engine, _FEEDBACK_OF_JOB, j=seed.closed_job_id))
    salaried = await mixed_job(job_with_postings, engine, salary=stated(60_000, 70_000))
    async with engine.begin() as conn:
        result = (await published_result(conn, salaried, tuple_id)).result_id
    await recomputed(engine, salaried.job_id)
    async with engine.connect() as conn:
        (instance,) = [r for r in (await conn.execute(_INSTANCES, {"r": result})).all() if r.is_current]
    assert instance.det_inputs["job"]["salary"]["sources"] == [ADZUNA]
    clean = await job_with_postings({"greenhouse": {}})
    async with engine.begin() as conn:
        adzuna_salary = await add_feedback(conn, salaried, result_id=result, det_result_id=int(instance.id))
        kept = await add_feedback(
            conn, Job(clean.job_id, await recomputed(engine, clean.job_id)), label="poor"
        )
    before = {f: await one(engine, _FEEDBACK, f=f) for f in (seeded_feedback, adzuna_salary, kept)}
    assert all(row.score_snapshot is not None for row in before.values())

    manifest = await purged(engine, purger, await request(engine))

    assert operations(manifest, "field_redact", "score_feedback") == sorted([seeded_feedback, adzuna_salary])
    for feedback in (seeded_feedback, adzuna_salary):
        after = await one(engine, _FEEDBACK, f=feedback)
        assert after.score_snapshot is None
        assert (after.kind, after.label, after.comment) == tuple(before[feedback])[1:]
    assert tuple(await one(engine, _FEEDBACK, f=kept)) == tuple(before[kept])


async def test_a_feedback_snapshot_written_again_fails_the_postcondition(
    engine: AsyncEngine, purger: Purger
) -> None:
    """The redaction's postcondition (the `field_redact`'s, checked in phase C with the kind's and by every
    replay's classification): a redacted feedback row that holds a snapshot again violates it; once NULL
    again, it holds."""
    seed = await seeded(engine)
    feedback = int(await scalar(engine, _FEEDBACK_OF_JOB, j=seed.closed_job_id))
    manifest = await purged(engine, purger, await request(engine))
    assert manifest.scope is not None
    async with engine.begin() as conn:
        await conn.execute(_SNAPSHOT_AGAIN, {"f": feedback})
    async with engine.connect() as conn:
        found = await check_postconditions(conn, manifest.scope, files=purger.file_roots)
    assert [(v.op, v.table, v.detail) for v in found] == [
        ("field_redact", "score_feedback", "1 row not redacted")
    ]
    async with engine.begin() as conn:
        await conn.execute(_SNAPSHOT_GONE, {"f": feedback})
    async with engine.connect() as conn:
        assert await check_postconditions(conn, manifest.scope, files=purger.file_roots) == []


async def test_a_tainted_current_revision_is_nulled_before_its_delete(
    engine: AsyncEngine, purger: Purger, statements: list[tuple[str, Any]]
) -> None:
    """The mixed job's current revision has Adzuna lineage: in its rebuild transaction the job's
    `current_revision_id` is set NULL before the revision is deleted (the manifest's `field_redact`, so no
    deferred key dangles), then the rebuilt revision is inserted and pointed at; it never reuses the tainted
    one's id."""
    seed = await seeded(engine)
    tainted = await seed_revision(engine, seed.closed_job_id)
    assert ADZUNA in await lineage(engine, tainted)
    statements.clear()
    manifest = await purged(engine, purger, await request(engine))
    assert seed.closed_job_id in operations(manifest, "field_redact", "jobs")
    assert tainted in operations(manifest, "row_delete", "job_revisions")
    unpoint = next(
        i
        for i, (sql, params) in enumerate(statements)
        if "SET current_revision_id = NULL" in sql
        and isinstance(params, dict)
        and tainted in params.get("tainted", ())
        and seed.closed_job_id in params.get("jobs", ())
    )
    delete = next(
        i
        for i, (sql, params) in enumerate(statements)
        if sql.startswith("DELETE FROM job_revisions")
        and isinstance(params, dict)
        and tainted in params.get("tainted", ())
    )
    assert unpoint < delete
    rebuilt = await seed_revision(engine, seed.closed_job_id)
    assert manifest.scope is not None and rebuilt > manifest.scope["max_ids"]["job_revisions"]
    assert await lineage(engine, rebuilt) == ["greenhouse"]


@pytest.fixture
def statements(engine: AsyncEngine) -> Iterator[list[tuple[str, Any]]]:
    """Every statement the engine's connections send while the test runs, with its parameters, in order."""
    sent: list[tuple[str, Any]] = []

    def record(conn: Any, cursor: Any, statement: str, parameters: Any, *args: Any) -> None:
        sent.append((statement, parameters))

    sa.event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        yield sent
    finally:
        sa.event.remove(engine.sync_engine, "before_cursor_execute", record)


async def test_a_merged_job_drawing_on_adzuna_keeps_none_of_its_values(
    engine: AsyncEngine, purger: Purger, job_with_postings: JobWithPostings
) -> None:
    """An Adzuna-only job merged into a Greenhouse job (its posting moved with the merge): its own row still
    holds the canonical values its Adzuna posting gave it and its revision is tainted. The root is rebuilt
    from the Greenhouse posting, and the merged job's row takes the root's rebuilt values, so neither names
    Adzuna afterwards (the `fence` postconditions over both)."""
    await seeded(engine)
    root = await job_with_postings({"greenhouse": {}})
    merged = await job_with_postings({ADZUNA: {"title_raw": "Erfundene Adzuna-Rolle"}})
    await recomputed(engine, root.job_id)
    merged_revision = await recomputed(engine, merged.job_id)
    async with engine.begin() as conn:
        await conn.execute(
            sa.text("UPDATE postings SET job_id = :r WHERE job_id = :m"),
            {"r": root.job_id, "m": merged.job_id},
        )
        await conn.execute(
            sa.text("UPDATE jobs SET merged_into_job_id = :r WHERE id = :m"),
            {"r": root.job_id, "m": merged.job_id},
        )
    await recomputed(engine, root.job_id)
    assert ADZUNA in await provenance_sources(engine, merged.job_id)

    manifest = await purged(engine, purger, await request(engine))
    assert {root.job_id, merged.job_id} <= set(affected(manifest))
    assert merged_revision in operations(manifest, "row_delete", "job_revisions")
    for job_id in (root.job_id, merged.job_id):
        assert await provenance_sources(engine, job_id) == ["greenhouse"]
    rows = {j: await one(engine, _JOB, j=j) for j in (root.job_id, merged.job_id)}
    assert rows[merged.job_id].title == rows[root.job_id].title
    assert rows[merged.job_id].merged_into_job_id == root.job_id
    assert rows[merged.job_id].current_revision_id is None  # its tainted revision went


async def test_a_rebuild_keeps_adzuna_data_above_the_bound(
    engine: AsyncEngine, purger: Purger, job_with_postings: JobWithPostings
) -> None:
    """§19.5 phase B: data above the manifest's watermark is used and kept. After a completed purge, Adzuna is
    enabled again and a fresh Adzuna posting (above the bound) gives the mixed job a salary and a revision
    whose lineage names it; a replay's rebuild of the stored set (`rebuild_jobs` on the completed manifest)
    reuses that revision and keeps the salary, and every postcondition still holds."""
    seed = await seeded(engine)
    manifest = await purged(engine, purger, await request(engine))
    assert manifest.scope is not None
    async with engine.begin() as conn:
        await reenable(conn)
        fresh, _ = await add_posting(
            conn,
            seed.closed_job_id,
            ADZUNA,
            observed_at=utcnow(),
            salary=stated(55_000, 65_000),
            employment_types=("full_time",),  # in the revision's text: its lineage names the posting
            employment_source="structured",
        )
    assert fresh > manifest.scope["max_ids"]["postings"]
    revision = await recomputed(engine, seed.closed_job_id)
    assert await lineage(engine, revision) == [ADZUNA, "greenhouse"]
    before = await one(engine, _JOB, j=seed.closed_job_id)
    assert before.salary is not None

    run = replay_run(engine, purger, manifest)
    assert await source_purge.rebuild_jobs(run, affected(manifest)) == 1  # the mixed job's family
    after = await one(engine, _JOB, j=seed.closed_job_id)
    assert after.current_revision_id == revision and after.salary == before.salary
    assert await lineage(engine, revision) == [ADZUNA, "greenhouse"]
    assert ADZUNA in await provenance_sources(engine, seed.closed_job_id)
    async with engine.connect() as conn:
        assert await check_postconditions(conn, manifest.scope, files=purger.file_roots) == []
        assert await source_purge.SourcePurge().postconditions(conn, manifest.scope) == []


def replay_run(engine: AsyncEngine, purger: Purger, manifest: ManifestRow) -> PurgeRun:
    """A replay's run of the manifest (Task 25's phase B calls `rebuild_jobs` with one): no epoch, no
    owner."""
    return PurgeRun(
        engine=engine,
        cfg=purger.cfg,
        now=utcnow(),
        cv_store=purger.deps.cv_store,
        manifest=manifest,
        mode="replay",
        effects=None,
        owner=None,
    )


async def test_a_family_left_with_only_an_adzuna_posting_above_the_bound_is_rebuilt(
    engine: AsyncEngine, purger: Purger, monkeypatch: pytest.MonkeyPatch, job_with_postings: JobWithPostings
) -> None:
    """An Adzuna-only job gets a fresh Adzuna posting above the bound after A2, inserted directly as a
    replay after a re-enablement would find it. The purge deletes the posting within the bound and the
    revision tainted within it, keeps the fresh posting, and rebuilds the job from it (neither deleted nor a
    `LookupError`); every postcondition holds."""
    made = await job_with_postings({ADZUNA: {}})
    tainted = await recomputed(engine, made.job_id)
    opened = await request(engine)
    monkeypatch.setattr(purge, "afault_point", raising_at(purge.AFTER_RECORD))
    await purger.run(opened.operation_id)  # the bound recorded, nothing deleted yet
    async with engine.begin() as conn:
        fresh, _ = await add_posting(conn, made.job_id, ADZUNA, observed_at=utcnow())
    monkeypatch.setattr(purge, "afault_point", no_fault)
    await purger.again(opened.operation_id)
    manifest = await completed(purger, opened.manifest_id, opened.operation_id)
    assert manifest.scope is not None and fresh > manifest.scope["max_ids"]["postings"]
    assert await existing(engine, [made.job_id]) == [made.job_id]
    assert tainted in operations(manifest, "row_delete", "job_revisions")
    rebuilt = await seed_revision(engine, made.job_id)
    assert rebuilt != tainted and await lineage(engine, rebuilt) == [ADZUNA]
    async with engine.connect() as conn:
        named = await conn.execute(_LINEAGE_POSTINGS, {"r": rebuilt})
        assert set(named.scalars()) == {fresh}  # the fresh posting's, kept
        assert await source_purge.SourcePurge().postconditions(conn, manifest.scope) == []


async def test_a_synthetic_golden_case_job_is_never_deleted(engine: AsyncEngine, purger: Purger) -> None:
    """A synthetic golden-case job (no posting) whose case draws on Adzuna is affected through the case: the
    case is deleted in step 3, and the job, which the rebuild never deletes or rebuilds, survives with its
    other cases."""
    await seed_adversarial(engine)
    async with engine.begin() as conn:
        rows = (await conn.execute(_SYNTHETIC_CASES)).all()
        drawing, *others = rows
        await conn.execute(_DRAWS_ON_ADZUNA, {"g": int(drawing.id)})
    manifest = await purged(engine, purger, await request(engine))
    assert int(drawing.job_id) in affected(manifest)
    assert await scalar(engine, _CASE_ROWS, g=int(drawing.id)) == 0
    assert await existing(engine, [int(drawing.job_id)]) == [int(drawing.job_id)]
    for case in others:
        assert await scalar(engine, _CASE_EXISTS, g=int(case.id)) is True


async def test_after_completion_missing_targets_creates_the_rebuilt_targets(
    engine: AsyncEngine, purger: Purger, queue_app: procrastinate.App, current_epoch: int
) -> None:
    """No target is created by the rebuild: the mixed job's rebuilt revision has none at completion, and
    the `missing_targets` reconciler creates it once the fence is released."""
    seed = await seeded(engine)
    tuple_id = int(await scalar(engine, _ACTIVE_TUPLE))
    await purged(engine, purger, await request(engine))
    rebuilt = await seed_revision(engine, seed.closed_job_id)
    async with engine.connect() as conn:
        made = [
            tuple(r) for r in await conn.execute(_TARGETS_OF_JOB, {"j": seed.closed_job_id, "t": tuple_id})
        ]
    assert rebuilt not in {revision for revision, _ in made}
    assert scoring_reconcilers is not None  # imported for the reconciler it registers
    await run_reconciler("missing_targets", engine, now=utcnow(), epoch=current_epoch)
    async with engine.connect() as conn:
        made = [
            tuple(r) for r in await conn.execute(_TARGETS_OF_JOB, {"j": seed.closed_job_id, "t": tuple_id})
        ]
    assert rebuilt in {revision for revision, _ in made}


# ---- jobs deleted whole ------------------------------------------------------------------------------------


async def test_an_adzuna_only_job_is_deleted_without_a_tombstone(
    engine: AsyncEngine, purger: Purger, job_with_postings: JobWithPostings
) -> None:
    """The Adzuna-only jobs go through `delete_job_graph` without a tombstone: one represents a group of three
    (the group is re-elected among the two left, which confirm each other), one has a job merged into it
    (both go), one is named by merge events (they go); every restricting key holds at each commit, and the
    mixed job and the group's other members stay."""
    seed = await seeded(engine)
    representative, *members = [
        await canonical(engine, job_with_postings, source) for source in (ADZUNA, "jsearch", "jsearch")
    ]
    async with engine.begin() as conn:
        group = await member_group(conn, representative, members)
    gone = sorted({*seed.adzuna_only_job_ids, seed.adzuna_merged_job_id, representative.job_id})
    kept = sorted({seed.closed_job_id, *(m.job_id for m in members)})
    await purged(engine, purger, await request(engine))
    assert await existing(engine, gone) == []
    assert await scalar(engine, _TOMBSTONES_OF, ids=gone) == 0
    assert await scalar(engine, _EVENTS_NAMING, ids=gone) == 0
    status, elected, joined = tuple(await one(engine, _GROUP, g=group))
    assert status == "active" and joined == [m.job_id for m in members] and elected in joined
    assert await existing(engine, kept) == kept
    # the open conflict on the active job's Adzuna posting went with the posting; its alert is resolved
    assert await scalar(engine, _CONFLICT, c=seed.lone_conflict_id) is None
    assert await scalar(
        engine, _ALERT_RESOLVED, k=f"dedup.identity_conflict:conflict:{seed.lone_conflict_id}"
    )
    async with engine.connect() as conn:
        rows = (await conn.execute(_ADZUNA_ROWS)).one()
    assert tuple(rows) == (0, 0, 0, 0)  # no posting, key, request or lineage of Adzuna is left


async def canonical(engine: AsyncEngine, job_with_postings: JobWithPostings, source: str) -> Job:
    """A canonical job of one company with one posting of `source` (`tests.support.groups.canonical_job`:
    one title, place and full text), so the jobs confirm each other as a group (§8.7 G2-G5)."""
    job_id = await canonical_job(
        engine,
        job_with_postings,
        source,
        observed_at=utcnow() - timedelta(days=2),
        description_text=FULL_TEXT,
        company_raw="Initech",
    )
    return Job(job_id, await seed_revision(engine, job_id))


async def test_an_adzuna_only_job_with_more_than_200_targets_is_deleted_in_bounded_chunks(
    engine: AsyncEngine, purger: Purger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An Adzuna-only job with 260 targets on untainted revisions: target chunks of at most 200 (200, then
    60), then the graph chunk; `delete_target_set` never refuses a set and the manifest completes."""
    async with engine.begin() as conn:
        chain = await insert_profile_chain(conn, 46)
        job = await insert_job(conn)
        await add_posting(conn, job.job_id, ADZUNA, observed_at=utcnow())
        await conn.execute(
            sa.text(
                "INSERT INTO job_revisions (job_id, revision_no, material_hash, llm_input, description_text, "
                "description_completeness, ad_language, embedding_status) SELECT :j, n, int4send(n), '{}', "
                "'Erfundene Testanzeige.', 'full', 'de', 'awaiting_embedding' FROM generate_series(2, 260) n"
            ),
            {"j": job.job_id},
        )
        await conn.execute(
            sa.text(
                "INSERT INTO score_targets (job_revision_id, llm_tuple_id, job_id, state) "
                "SELECT id, :t, job_id, 'pending' FROM job_revisions WHERE job_id = :j"
            ),
            {"t": chain.llm_tuple_id, "j": job.job_id},
        )
    calls: list[tuple[str, int]] = []
    purge_set, delete_graph = deletion_graph.purge_target_set, deletion_graph.delete_job_graph

    async def recording_set(conn: AsyncConnection, target_ids: Any, **kwargs: Any) -> Any:
        calls.append(("targets", len(target_ids)))
        return await purge_set(conn, target_ids, **kwargs)

    async def recording_graph(conn: AsyncConnection, job_id: int, **kwargs: Any) -> Any:
        assert kwargs["write_tombstone"] is False
        calls.append(("graph", job_id))
        return await delete_graph(conn, job_id, **kwargs)

    monkeypatch.setattr(deletion_graph, "purge_target_set", recording_set)
    monkeypatch.setattr(deletion_graph, "delete_job_graph", recording_graph)
    await purged(engine, purger, await request(engine))
    assert calls == [("targets", 200), ("targets", 60), ("graph", job.job_id)]
    assert await existing(engine, [job.job_id]) == []


# ---- the derivatives ---------------------------------------------------------------------------------------


_BUDGET_MONTH: Final = sa.text(
    "INSERT INTO llm_budget (month, cap_usd, overrun_buffer_usd, bucket_usd, bucket_at) "
    "VALUES (:m, 100, 2, 1, now()) ON CONFLICT DO NOTHING"
)
# A request entry of `:m` in `:state`, counted in its class's counter as the budget counts it (`:cls`).
_ENTRY_ROW: Final = sa.text(
    "INSERT INTO llm_spend_ledger (budget_month, kind, purpose, eval_run, purpose_class, model_requested, "
    "state, reserved_usd, input_tokens_bound, batch, admitted_cap_usd, counted_before_usd, dispatched_at) "
    "VALUES (:m, 'request', 'generate', :eval, :cls, 'claude-sonnet-5-5', :state, :r, 1000, false, 100, 0, "
    "CASE WHEN :state = 'dispatched' THEN now() END) RETURNING id"
)
_COUNTED: Final = {
    "scoring": sa.text(
        "UPDATE llm_budget SET scoring_counted_usd = scoring_counted_usd + :r, "
        "counted_usd = counted_usd + :r WHERE month = :m"
    ),
    "nonscoring": sa.text(
        "UPDATE llm_budget SET nonscoring_counted_usd = nonscoring_counted_usd + :r, "
        "counted_usd = counted_usd + :r WHERE month = :m"
    ),
}
# An online `generate` call of the entry, of an eval run or of a target's attempt.
_CALL_ROW: Final = sa.text(
    "INSERT INTO llm_calls (purpose, round, logical_call_no, eval_run_id, attempt_id, target_id, "
    "execution_config_id, model_requested, effort, max_tokens, transport, prompt_template_version, "
    "rendered_prompt_sha256, input_refs, input_tokens_bound, status, spend_ledger_id, started_at) "
    "VALUES ('generate', 0, 1, :run, :attempt, :target, :exec, 'claude-sonnet-5-5', 'medium', 6000, "
    "'stream', 'v2', :sha, CAST(:refs AS jsonb), 1000, :state, :ledger, now()) RETURNING id"
)
_CLAIMED_TARGET: Final = sa.text(
    "INSERT INTO score_targets (job_revision_id, llm_tuple_id, job_id, state, claim_id, claim_epoch, "
    "claimed_at, claim_heartbeat_at, attempt_no, lane) VALUES (:r, :t, :j, 'in_progress', :c, 1, now(), "
    "now(), 1, 'similarity') RETURNING id"
)
_RUNNING_ATTEMPT: Final = sa.text(
    "INSERT INTO score_attempts (target_id, attempt_no, mode, claim_id, claim_epoch, execution_config_id, "
    "evaluation_policy_id, lane, outcome) VALUES (:t, 1, 'online', :c, 1, :exec, :policy, 'similarity', "
    "'running') RETURNING id"
)
RESERVED: Final = Decimal("0.1")
_CASE_OF_CALL: Final = sa.text("UPDATE llm_calls SET golden_case_id = :g WHERE id = :c")


async def counted_entry(conn: AsyncConnection, *, month: date, state: str, purpose_class: str) -> int:
    """A request entry of `month` in `state` (an eval run's when non-scoring), counted as the budget counts it
    (a purge's release subtracts it); its id."""
    await conn.execute(_BUDGET_MONTH, {"m": month})
    params = {"m": month, "eval": purpose_class == "nonscoring", "cls": purpose_class, "state": state}
    ledger = int((await conn.execute(_ENTRY_ROW, {**params, "r": RESERVED})).scalar_one())
    await conn.execute(_COUNTED[purpose_class], {"r": RESERVED, "m": month})
    return ledger


async def online_call(
    conn: AsyncConnection,
    ledger: int,
    *,
    state: str,
    refs: str = "{}",
    eval_run_id: int | None = None,
    attempt_id: int | None = None,
    target_id: int | None = None,
) -> int:
    active = await active_ids(conn)
    params = {
        "run": eval_run_id,
        "attempt": attempt_id,
        "target": target_id,
        "exec": active.execution_config_id,
        "sha": hashlib.sha256(f"call-{ledger}".encode()).digest(),
        "refs": refs,
        "state": state,
        "ledger": ledger,
    }
    return int((await conn.execute(_CALL_ROW, params)).scalar_one())


async def eval_call(
    conn: AsyncConnection,
    *,
    revision_id: int,
    state: str,
    eval_run_id: int,
    month: date,
    golden_case_id: int | None = None,
) -> tuple[int, int]:
    """An eval run's online call of `month` naming the revision in its `input_refs` (no golden case unless
    `golden_case_id`: an eval call no case deletion takes), its entry in `state`; `(ledger, call)`."""
    ledger = await counted_entry(conn, month=month, state=state, purpose_class="nonscoring")
    refs = f'{{"job_revision_id": {revision_id}, "repetition": 1}}'
    call = await online_call(conn, ledger, state=state, refs=refs, eval_run_id=eval_run_id)
    if golden_case_id is not None:
        await conn.execute(_CASE_OF_CALL, {"g": golden_case_id, "c": call})
    return ledger, call


async def online_try(conn: AsyncConnection, job: Job, tuple_id: int, *, state: str, month: date) -> int:
    """An `in_progress` target of the job's revision with its running attempt (one claim) and one online try
    of `month` whose entry is `state`; the entry's id."""
    claim = uuid.uuid4()
    params = {"r": job.revision_id, "t": tuple_id, "j": job.job_id, "c": claim}
    target = int((await conn.execute(_CLAIMED_TARGET, params)).scalar_one())
    active = await active_ids(conn)
    attempt_params = {
        "t": target,
        "c": claim,
        "exec": active.execution_config_id,
        "policy": active.evaluation_policy_id,
    }
    attempt = int((await conn.execute(_RUNNING_ATTEMPT, attempt_params)).scalar_one())
    ledger = await counted_entry(conn, month=month, state=state, purpose_class="scoring")
    await online_call(conn, ledger, state=state, attempt_id=attempt, target_id=target)
    return ledger


def seed_month(now: datetime) -> date:
    t0 = now - SEED_AGE
    return date(t0.year, t0.month, 1)


async def test_golden_cases_drawing_on_adzuna_are_deleted_active_or_not(
    engine: AsyncEngine, purger: Purger, job_with_postings: JobWithPostings
) -> None:
    """§18.2 "golden snapshot provenance": the seed's cases (active and inactive, each drawing on Adzuna) and
    a case on an untainted revision whose frozen inputs hold an Adzuna salary are deleted with their eval
    results and eval calls (the agreement overrides the exemption); a case of the same job drawing on no
    Adzuna value stays. The manifest lists the deleted cases."""
    now = utcnow()
    seed = await seeded(engine, now)
    job = await mixed_job(job_with_postings, engine, salary=stated(50_000, 55_000))
    assert await lineage(engine, job.revision_id) == ["greenhouse"]
    async with engine.begin() as conn:
        chain = await insert_profile_chain(conn, 92)
        other_chain = await insert_profile_chain(conn, 93)
        salaried = await insert_golden_case(conn, job, chain.profile_version_id, det_input_sources=[ADZUNA])
        kept = await insert_golden_case(
            conn, job, other_chain.profile_version_id, det_input_sources=["greenhouse"]
        )
        run = await insert_eval_run(conn, "completed")
        _, salaried_call = await eval_call(
            conn,
            revision_id=job.revision_id,
            state="dispatched",
            eval_run_id=run,
            month=seed_month(now),
            golden_case_id=salaried,
        )
        for case, calls in ((salaried, [salaried_call]), (kept, [])):
            await conn.execute(
                sa.text(
                    "INSERT INTO eval_case_results (eval_run_id, golden_case_id, repetition, outcome, "
                    "critic_ran, llm_call_ids, output) VALUES (:run, :g, 1, 'accepted', false, "
                    "CAST(:calls AS bigint[]), '{}')"
                ),
                {"run": run, "g": case, "calls": calls},
            )
        seeded_cases = [
            int(c)
            for c in (
                await conn.execute(
                    sa.text("SELECT id FROM golden_cases WHERE 'adzuna' = ANY(det_input_sources)")
                )
            ).scalars()
        ]
    assert len(seeded_cases) == 4  # the seed's three (one active) and the salaried one
    manifest = await purged(engine, purger, await request(engine))
    assert operations(manifest, "row_delete", "golden_cases") == sorted(seeded_cases)
    for case in seeded_cases:
        assert await scalar(engine, _CASE_ROWS, g=case) == 0
    assert await scalar(engine, _CASE_ROWS, g=kept) == 2  # the case and its eval result
    assert await scalar(engine, _CALL_EXISTS, c=salaried_call) is False  # its eval call went with it
    assert seed.closed_job_id in affected(manifest)


async def test_other_eval_calls_naming_a_tainted_revision_are_deleted(
    engine: AsyncEngine, purger: Purger
) -> None:
    """An eval call no case deletion takes, whose `input_refs` names the mixed job's tainted revision, is
    deleted explicitly (listed by the manifest); one naming an untainted revision stays."""
    now = utcnow()
    seed = await seeded(engine, now)
    tainted = await seed_revision(engine, seed.closed_job_id)
    (untainted,) = [r for r in await ids(engine, _REVISIONS_OF, j=seed.closed_job_id) if r != tainted]
    async with engine.begin() as conn:
        run = await insert_eval_run(conn, "completed")
        _, gone = await eval_call(
            conn, revision_id=tainted, state="dispatched", eval_run_id=run, month=seed_month(now)
        )
        _, stays = await eval_call(
            conn, revision_id=untainted, state="dispatched", eval_run_id=run, month=seed_month(now)
        )
    manifest = await purged(engine, purger, await request(engine))
    assert operations(manifest, "row_delete", "llm_calls") == [gone]
    assert await scalar(engine, _CALL_EXISTS, c=gone) is False
    assert await scalar(engine, _CALL_EXISTS, c=stays) is True


async def test_reserved_and_dispatched_online_calls_are_settled_before_they_go(
    engine: AsyncEngine, purger: Purger
) -> None:
    """G04: an `in_progress` target of an affected job with a `reserved` try and one with a `dispatched` try,
    and a running eval's `dispatched` call naming a tainted revision: released, `ambiguous`, `ambiguous`,
    each before its call goes, and no entry is left without its call."""
    now = utcnow()
    seed = await seeded(engine, now)
    month = seed_month(now)
    tainted = await seed_revision(engine, seed.closed_job_id)
    async with engine.begin() as conn:
        tuple_id = int((await conn.execute(_ACTIVE_TUPLE)).scalar_one())
        tries = []
        for state in ("reserved", "dispatched"):
            job = await insert_job(conn)
            await draw_on_adzuna(conn, job)
            tries.append(await online_try(conn, job, tuple_id, state=state, month=month))
        reserved, dispatched = tries
        run = await insert_eval_run(conn, "running")
        eval_entry, _ = await eval_call(
            conn, revision_id=tainted, state="dispatched", eval_run_id=run, month=month
        )
    await purged(engine, purger, await request(engine))
    states = {}
    for name, ledger in (("reserved", reserved), ("dispatched", dispatched), ("eval", eval_entry)):
        entry = await one(engine, _ENTRY, l=ledger)
        states[name] = (entry.state, entry.evidence)
    assert states == {
        "reserved": ("not_billed", "never_dispatched"),
        "dispatched": ("ambiguous", None),
        "eval": ("ambiguous", None),
    }
    assert await scalar(engine, _UNSETTLED_WITHOUT_CALL) == 0


async def test_an_in_flight_batch_is_cancelled_and_completes(engine: AsyncEngine, purger: Purger) -> None:
    """Deviation 13: an `in_batch` target of the live (`submitted`) submission whose job draws on Adzuna is
    cancelled out of the batch: the submission's cancel is requested, its item recorded in
    `purged_batch_items` (the batch can still return its line), the batch's other target stays `in_batch`,
    and the purge completes."""
    seed = await seeded(engine)
    affected_job, other_job = seed.batched_job_ids
    async with engine.begin() as conn:
        revision = int(
            (
                await conn.execute(
                    sa.text("SELECT current_revision_id FROM jobs WHERE id = :j"), {"j": affected_job}
                )
            ).scalar_one()
        )
        await draw_on_adzuna(conn, Job(affected_job, revision))
    items = await ids_text(engine, _ITEMS_OF_JOB, j=affected_job)
    manifest = await purged(engine, purger, await request(engine))
    submission = await one(engine, _SUBMISSION, s=seed.live_submission_id)
    assert submission.state == "submitted" and submission.cancel_requested_at is not None
    assert await ids_text(engine, _PURGED_ITEMS, m=manifest.id) == items
    async with engine.connect() as conn:
        left = [tuple(r) for r in await conn.execute(_TARGETS_IN, {"s": seed.live_submission_id})]
    assert left == [(other_job, "in_batch")]
    assert await existing(engine, [affected_job]) == []  # its only posting was Adzuna's


async def ids_text(engine: AsyncEngine, statement: sa.TextClause, **params: object) -> list[str]:
    async with engine.connect() as conn:
        return [str(i) for i in (await conn.execute(statement, params)).scalars()]


# ---- the embeddings ----------------------------------------------------------------------------------------


async def test_embeddings_of_deleted_tainted_revisions_are_deleted(
    engine: AsyncEngine, purger: Purger
) -> None:
    """D22: the embedding only the mixed job's tainted revision references is frozen at A2 and deleted; an
    embedding a tainted and an untainted revision share stays with the untainted one."""
    seed = await seeded(engine)
    tainted = await seed_revision(engine, seed.closed_job_id)
    frozen = (await one(engine, _REVISION_EMBEDDING, r=tainted)).embedding_id
    async with engine.begin() as conn:
        shared = await embedding_at(conn, "shared", (5, 1.0))
        root_revision = int(
            (
                await conn.execute(
                    sa.text("SELECT current_revision_id FROM jobs WHERE id = :j"),
                    {"j": seed.adzuna_merge_root_id},
                )
            ).scalar_one()
        )
        keeper = await insert_job(conn)
        for revision in (root_revision, keeper.revision_id):
            await embed_revision(conn, revision, shared)
    manifest = await purged(engine, purger, await request(engine))
    assert operations(manifest, "row_delete", "embeddings") == [frozen]
    assert await scalar(engine, _EMBEDDING_EXISTS, e=frozen) is False
    assert await scalar(engine, _EMBEDDING_EXISTS, e=shared) is True
    assert tuple(await one(engine, _REVISION_EMBEDDING, r=keeper.revision_id)) == ("embedded", shared)


async def test_a_revision_reusing_a_frozen_embedding_after_a2_is_re_embedded_above_the_bound(
    engine: AsyncEngine,
    purger: Purger,
    monkeypatch: pytest.MonkeyPatch,
    queue_app: procrastinate.App,
    fake_embedder: Any,
    current_epoch: int,
    job_with_postings: JobWithPostings,
) -> None:
    """An unrelated revision with the same text takes one of the frozen embeddings between A2 and phase B:
    the purge completes, that revision is `awaiting_embedding` without its `embedding_id`, and the
    `awaiting_embedding` reconciler later gives it a new embedding above the manifest's bound."""
    tainted = await job_with_postings({ADZUNA: {}})
    unrelated = await job_with_postings({"jsearch": {}})
    tainted_revision = await recomputed(engine, tainted.job_id)
    reuser = await recomputed(engine, unrelated.job_id)
    async with engine.begin() as conn:
        frozen = await embedding_at(conn, "tainted text", (7, 1.0))
        await embed_revision(conn, tainted_revision, frozen)
    opened = await request(engine)
    monkeypatch.setattr(purge, "afault_point", raising_at(purge.AFTER_RECORD))
    await purger.run(opened.operation_id)  # A2 and A3 committed, nothing deleted yet
    manifest = await manifest_of(engine, opened.manifest_id)
    assert operations(manifest, "row_delete", "embeddings") == [frozen]
    async with engine.begin() as conn:
        await embed_revision(conn, reuser, frozen)  # the same text, embedded after A2
    monkeypatch.setattr(purge, "afault_point", no_fault)
    await purger.again(opened.operation_id)
    done = await completed(purger, opened.manifest_id, opened.operation_id)
    assert done.scope is not None
    assert tuple(await one(engine, _REVISION_EMBEDDING, r=reuser)) == ("awaiting_embedding", None)
    assert await scalar(engine, _EMBEDDING_EXISTS, e=frozen) is False

    async with make_http_client() as http:
        client = EmbedderClient(fake_embedder.base_url, http)
        later = utcnow() + EMBEDDING_GRACE
        await reconcile_awaiting_embedding(ctx(engine, queue_app, later, current_epoch, embedder=client))
    status, embedding = tuple(await one(engine, _REVISION_EMBEDDING, r=reuser))
    assert status == "embedded" and embedding > done.scope["max_ids"]["embeddings"]


# ---- the fence's consumers ---------------------------------------------------------------------------------

CONSUMERS: Final = ("missing_targets", "enqueue_targets", "claim", "publish", "eval_run")


def no_adapters(instance: InstanceRow) -> SourceAdapter:
    raise AssertionError("node 9 fetches nothing")


@pytest.mark.parametrize("consumer", CONSUMERS)
async def test_fence_consumers_refuse_a_fenced_job(
    engine: AsyncEngine,
    purger: Purger,
    fake_llm: FakeLLM,
    current_epoch: int,
    queue_app: procrastinate.App,
    consumer: str,
) -> None:
    """§18.2 N14: while the source fence stands (A0 committed, the purge not yet run), a job with an Adzuna
    posting and a tainted revision is refused by each consumer: `missing_targets` and node 9
    (`enqueue_targets`) create no target for it, the claim leaves its target `pending`, the publish
    transaction writes no result (the attempt ends `cancelled('purge_fenced')`), and an eval run skips its
    case as `fenced` (Task 17's `test_a_fenced_job_is_absent_from_the_export` covers the export). After the
    completion no target or result of a deleted revision exists."""
    async with engine.begin() as conn:
        await activate_test_policy(conn)
    pending = (await insert_targets(engine, 1))[0] if consumer == "claim" else None
    claimed = await claimed_target(engine) if consumer == "publish" else None
    if pending is not None:
        job = Job(pending.job_id, pending.job_revision_id)
    elif claimed is not None:
        job = Job(claimed.job_id, claimed.job_revision_id)
    else:
        async with engine.begin() as conn:
            job = await insert_job(conn)
    async with engine.begin() as conn:
        tuple_id = await activate_score_tuple(conn)
        await profile_vector_at(conn, await profile_of_tuple(conn, tuple_id), "summary", (1023, 1.0))
        await draw_on_adzuna(conn, job)
        case = None
        if consumer == "eval_run":
            chain = await insert_profile_chain(conn, 94)
            case = await insert_golden_case(conn, job, chain.profile_version_id)
    opened = await request(engine)

    if consumer == "missing_targets":
        await run_reconciler("missing_targets", engine, now=utcnow(), epoch=current_epoch)
        async with engine.connect() as conn:
            assert (await conn.execute(_TARGETS_OF_JOB, {"j": job.job_id, "t": tuple_id})).all() == []
    elif consumer == "enqueue_targets":
        owner = await queued_run(engine)
        async with engine.begin() as conn:
            await conn.execute(_TOUCH, {"j": job.job_id})  # a job the run changed
        update = await enqueue(engine, owner, tuple_id)
        assert update["counters"].get("targets_skipped_fenced") == 1
        async with engine.connect() as conn:
            assert (await conn.execute(_TARGETS_OF_JOB, {"j": job.job_id, "t": tuple_id})).all() == []
    elif pending is not None:
        async with engine.begin() as conn:
            cycle = await insert_cycle(conn, await score_llm_tuple(conn))
            active = await active_ids(conn)
            claims = await claim_node.claim_round(
                conn,
                [Picked(pending.target_id, lane="similarity")],
                epoch=current_epoch,
                cycle_id=cycle,
                execution_config_id=active.execution_config_id,
                evaluation_policy_id=active.evaluation_policy_id,
            )
        assert claims == []
    elif claimed is not None:
        fake_llm.answer("generate", job.revision_id, synthetic_c_draft())
        final = await invoke_subgraph(await score_deps(engine, fake_llm.gateway(engine)), claimed)
        assert final["outcome"] == "cancelled"
        assert await scalar(engine, _RESULTS_OF_REVISIONS, r=[job.revision_id]) == 0
    else:
        started = await start_eval(engine)
        await execute(engine, started.operation_id, eval_deps(engine, fake_llm), epoch=current_epoch)
        run = await run_row(engine, started.eval_run_id)
        assert (run.status, run.metrics["cases"]["fenced"]) == ("completed", [case])
        assert await case_rows(engine, started.eval_run_id) == []

    revisions_before = await ids(engine, _REVISIONS_OF, j=job.job_id)
    await purged(engine, purger, opened)
    assert await existing(engine, [job.job_id]) == []
    assert await scalar(engine, _TARGETS_OF_REVISIONS, r=revisions_before) == 0
    assert await scalar(engine, _RESULTS_OF_REVISIONS, r=revisions_before) == 0


async def enqueue(engine: AsyncEngine, owner: RunOwner, tuple_id: int) -> Any:
    node = make_enqueue_targets(make_deps(engine, no_adapters))
    return await node({**initial_state(owner.run_id, owner.attempt_no), "llm_tuple_id": tuple_id})


# ---- the record, a later choice, re-application ------------------------------------------------------------


async def test_purge_fence_at_stays_and_reenabling_needs_a_settings_version(
    engine: AsyncEngine, purger: Purger, job_with_postings: JobWithPostings
) -> None:
    """After the completion `purge_fence_at` stays as the record (A0's `admitted_at`) and Adzuna stays off;
    only a new settings version re-enables it. Data collected afterwards (a fresh Adzuna posting on the
    rebuilt mixed job) lies above the watermark: the manifest's postconditions keep holding."""
    seed = await seeded(engine)
    now = utcnow()
    manifest = await purged(engine, purger, await request(engine, now=now))
    assert await scalar(engine, _FENCE_AT) == now and not await adzuna_enabled(engine)
    async with engine.begin() as conn:
        await reenable(conn)
    assert await adzuna_enabled(engine) and await scalar(engine, _FENCE_AT) == now
    async with engine.begin() as conn:
        await add_posting(conn, seed.closed_job_id, ADZUNA, observed_at=utcnow(), salary=stated(1, 2))
    await recomputed(engine, seed.closed_job_id)
    assert ADZUNA in await provenance_sources(engine, seed.closed_job_id)  # collected after re-enabling
    assert manifest.scope is not None
    async with engine.connect() as conn:
        assert await check_postconditions(conn, manifest.scope, files=purger.file_roots) == []
        assert await source_purge.SourcePurge().postconditions(conn, manifest.scope) == []


async def reenable(conn: AsyncConnection) -> None:
    """A user's later choice: a new settings version of the active document with Adzuna enabled again."""
    active = await lock_active(conn)
    payload = cast(dict[str, Any], active.payload)
    sources = payload["sources"]
    enabled = {**payload, "sources": {**sources, "enabled": {**sources["enabled"], ADZUNA: True}}}
    request = SaveRequest(
        payload=enabled, base_version_id=active.settings_version_id, acknowledged_warnings=frozenset()
    )
    await save_settings(conn, request, now=utcnow(), defer=defer_in_tx, internal=True)


async def revert_admission(engine: AsyncEngine, settings_version_id: int, *, queued_at: datetime) -> int:
    """A0's effects undone, as a dump taken before the purge holds them: the settings version before it
    active again, no `purge_fence_at`, and a collection queued before the admission; the run's id."""
    async with engine.begin() as conn:
        await conn.execute(_LOCK_CONFIG)
        await conn.execute(_POINT_SETTINGS, {"v": settings_version_id})
        await conn.execute(_UNFENCE_SOURCE)
        return int((await conn.execute(_QUEUED_RUN, {"at": queued_at})).scalar_one())


async def reapply(engine: AsyncEngine, opened: OpenedPurge, now: datetime) -> None:
    manifest = await manifest_of(engine, opened.manifest_id)
    async with engine.begin() as conn:
        await conn.execute(_LOCK_CONFIG)  # phase A's lock (Task 25)
        await source_purge.SourcePurge().reapply_admission(conn, manifest, now=now, high_water={})


async def test_reapply_admission_restores_the_a0_effects_once(engine: AsyncEngine) -> None:
    """Replay's re-application of A0 over a database that lacks its effects: a new internal version disables
    Adzuna, `purge_fence_at := admitted_at`, and the collection queued before the admission is cancelled;
    a second call adds nothing."""
    await seeded(engine)
    before = await one(engine, _ACTIVE_SETTINGS)
    admitted = utcnow()
    assert before.created_at <= admitted
    opened = await request(engine, now=admitted)
    run_id = await revert_admission(engine, before.id, queued_at=admitted - timedelta(minutes=1))
    assert await adzuna_enabled(engine) and await scalar(engine, _FENCE_AT) is None

    now = utcnow()
    await reapply(engine, opened, now)
    restored = await one(engine, _ACTIVE_SETTINGS)
    assert restored.id != before.id and restored.created_at == now and not await adzuna_enabled(engine)
    assert await scalar(engine, _FENCE_AT) == admitted
    assert tuple(await one(engine, _RUN, r=run_id))[:2] == ("cancelled", "source_purge")
    versions = await scalar(engine, _VERSIONS)

    await reapply(engine, opened, utcnow())
    assert await scalar(engine, _VERSIONS) == versions
    assert (await one(engine, _ACTIVE_SETTINGS)).id == restored.id
    assert await scalar(engine, _FENCE_AT) == admitted


async def test_reapply_admission_keeps_a_later_reenablement(engine: AsyncEngine) -> None:
    """An active version created after `admitted_at` that enables Adzuna is the user's later choice: it stays
    active; a collection queued after the admission stays queued; a later `purge_fence_at` is never
    lowered."""
    await seeded(engine)
    admitted = utcnow() - timedelta(minutes=5)
    opened = await request(engine, now=admitted)
    async with engine.begin() as conn:
        await reenable(conn)
        later = utcnow()
        await conn.execute(
            sa.text("UPDATE sources SET purge_fence_at = :t WHERE id = 'adzuna'"), {"t": later}
        )
        run_id = int((await conn.execute(_QUEUED_RUN, {"at": utcnow()})).scalar_one())
    chosen = (await one(engine, _ACTIVE_SETTINGS)).id
    await reapply(engine, opened, utcnow())
    assert (await one(engine, _ACTIVE_SETTINGS)).id == chosen and await adzuna_enabled(engine)
    assert tuple(await one(engine, _RUN, r=run_id))[:2] == ("queued", None)
    assert await scalar(engine, _FENCE_AT) == later


async def test_a_live_purge_completes_when_its_cutoff_is_later_than_its_admission(
    engine: AsyncEngine, purger: Purger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An injected clock: A0 at t0 writes `purge_fence_at`, the settings version and the manifest at t0; the
    fence's cutoff is read at t0 + 5 s. The purge completes and its postconditions hold, comparing A0's own
    writes with `admitted_at`: `purge_fence_at = admitted_at < fenced_at`."""
    await seeded(engine)
    t0 = utcnow() - timedelta(seconds=30)
    cutoff = t0 + timedelta(seconds=5)

    async def committed_at(engine: AsyncEngine) -> datetime:
        return cutoff

    monkeypatch.setattr(purge, "fence_committed_at", committed_at)
    manifest = await purged(engine, purger, await request(engine, now=t0))
    assert manifest.scope is not None
    assert datetime.fromisoformat(manifest.scope["admitted_at"]) == t0
    assert datetime.fromisoformat(manifest.scope["fenced_at"]) == cutoff
    assert await scalar(engine, _FENCE_AT) == t0 < cutoff


# ---- the exports -------------------------------------------------------------------------------------------


async def test_source_purge_purges_exports(engine: AsyncEngine, purger: Purger) -> None:
    """Deviation 18: a source purge deletes every export, which may hold Adzuna data: the seed's `ready`
    export ends `purged` without its path, and its file is gone."""
    seed = await seeded(engine)
    path = (await one(engine, _EXPORT, e=seed.ready_export_id)).storage_path
    exported = purger.cfg.exports_dir / path
    exported.write_bytes(b"sealed")
    await purged(engine, purger, await request(engine))
    assert tuple(await one(engine, _EXPORT, e=seed.ready_export_id)) == ("purged", None)
    assert not exported.exists()


async def test_a_crashed_chunk_is_retried_from_what_is_left(
    engine: AsyncEngine, purger: Purger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash inside phase B (the rebuild's transaction raising) rolls that chunk back; the requeued
    operation's retry finds what is left and completes, every chunk idempotent."""
    seed = await seeded(engine)
    opened = await request(engine)
    rebuild = source_purge.rebuild_jobs
    crashed = asyncio.Event()

    async def crashing(run: Any, job_ids: Any) -> int:
        if not crashed.is_set():
            crashed.set()
            raise SimulatedCrash("rebuild")
        return await rebuild(run, job_ids)

    monkeypatch.setattr(source_purge, "rebuild_jobs", crashing)
    await purger.run(opened.operation_id)
    assert (await manifest_of(engine, opened.manifest_id)).status == "executing"
    await purger.again(opened.operation_id)
    await completed(purger, opened.manifest_id, opened.operation_id)
    assert await existing(engine, [seed.closed_job_id]) == [seed.closed_job_id]
