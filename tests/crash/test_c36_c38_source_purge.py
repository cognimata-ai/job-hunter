"""Crash cases C36 and C38 (spec §18.3, §20.5, §19.5 step 3; plan M5 Task 15): a real worker dies in a source
purge and the next one finishes it.

A source purge is requested in process (`request_source_purge`, its job deferred). Worker A
(`JOBHUNTER_FAULT=<point>`) claims the purge and is killed at the point (exit 86, no cleanup: the open chunk
transaction rolls back with the connection); its procrastinate heartbeat is made stale, so worker B's start
releases the job, and B's takeover sweep gives the operation back (as in C31).

- **C36** (`privacy.source_purge.mid_rebuild`): A dies inside the mixed job's rebuild chunk, after the
  derivatives and the source data committed. The job's target on its tainted current revision is gone and the
  rebuild rolled back, so the `missing_targets` reconciler would recreate it if the fence did not hold: it
  runs while the fence stands (after A's death and again once B is ready) and creates no target of a deleted
  revision; B resumes the purge and releases the fence in its completing transaction; afterwards the
  reconciler creates the rebuilt revision's target, and only that one.
- **C38** (`privacy.source_purge.before_rebuild`): A dies after the Adzuna postings went, before the rebuild.
  A mixed job whose only link to Adzuna was its posting (its salary; its revision's lineage never named
  Adzuna) then matches the fence through the stored `job_ids` alone, and a re-derivation would miss it. B
  rebuilds exactly the stored set, that job included, and the fence ends only in the transaction that commits
  the bounded postconditions. (The replay's phase B is Task 25's.)

Every datum is invented; no paid call (the worker is in fake LLM mode and the purge calls nothing).
"""

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import procrastinate
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.dedup import revisions
from jobhunter.privacy.kinds import source_purge
from jobhunter.privacy.manifests import FileRoots, check_postconditions, read_manifest
from jobhunter.privacy.purge import OpenedPurge
from jobhunter.scoring import filters
from jobhunter.scoring import reconcilers as scoring_reconcilers  # registers `missing_targets`
from jobhunter.worker.tasks import defer_in_tx
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.collect import JobWithPostings
from tests.support.observations import stated
from tests.support.pg import PgServer, pg_one
from tests.support.procs import Proc, ProcessGroup, wait_until
from tests.support.reconcile import run_reconciler
from tests.support.scoring import activate_score_tuple

pytestmark = [pytest.mark.crash, pytest.mark.slow]
WORKER: Final = [sys.executable, "-m", "jobhunter.cli", "worker"]
FAULT_EXIT: Final = 86
READY_WITHIN_S: Final = 60
ENDED_WITHIN_S: Final = 120
ADZUNA: Final = "adzuna"
_TARGET: Final = sa.text(
    "INSERT INTO score_targets (job_revision_id, llm_tuple_id, job_id, state) VALUES (:r, :t, :j, 'pending') "
    "RETURNING id"
)
_TARGETS_OF_JOB: Final = sa.text("SELECT job_revision_id FROM score_targets WHERE job_id = :j ORDER BY id")
_IS_FENCED: Final = sa.text("SELECT is_fenced(:j)")
_LINKS: Final = sa.text(
    "SELECT (SELECT count(*) FROM postings p JOIN source_instances si ON si.id = p.source_instance_id "
    "WHERE p.job_id = :j AND si.source_id = 'adzuna') AS postings, (SELECT count(*) FROM job_revisions r "
    "JOIN job_revision_sources s ON s.job_revision_id = r.id WHERE r.job_id = :j AND s.source_id = 'adzuna') "
    "AS lineage"
)
_PROVENANCE_SOURCES: Final = sa.text(
    "SELECT coalesce(array_agg(DISTINCT si.source_id ORDER BY si.source_id), '{}') FROM jobs j "
    "CROSS JOIN LATERAL jsonb_each(j.field_provenance) e JOIN source_instances si "
    "ON si.id = CAST(e.value ->> 'source_instance_id' AS bigint) WHERE j.id = :j"
)
_JOB: Final = sa.text("SELECT current_revision_id, salary FROM jobs WHERE id = :j")
_LINEAGE: Final = sa.text(
    "SELECT coalesce(array_agg(DISTINCT source_id ORDER BY source_id), '{}') FROM job_revision_sources "
    "WHERE job_revision_id = :r"
)


@dataclass(frozen=True)
class Volumes:
    """The worker's manifests and exports volumes, this test's own (0700, as the images make them)."""

    manifests: Path
    exports: Path

    @property
    def roots(self) -> FileRoots:
        return FileRoots(cv_store=self.manifests.parent / "cv", exports=self.exports)


@pytest.fixture
def volumes(tmp_path: Path) -> Volumes:
    made = Volumes(tmp_path / "manifests", tmp_path / "exports")
    for directory in (made.manifests, made.exports):
        directory.mkdir(mode=0o700)
    return made


@dataclass(frozen=True)
class Workers:
    """Spawns `jobhunter worker` children on this test's volumes, with an optional fault point (C31's)."""

    server: PgServer
    proc_env: Any
    procs: ProcessGroup
    volumes: Volumes

    def env(self, **extra: str) -> dict[str, str]:
        dirs = {
            "JOBHUNTER_MANIFESTS_DIR": str(self.volumes.manifests),
            "JOBHUNTER_EXPORTS_DIR": str(self.volumes.exports),
        }
        env: dict[str, str] = self.proc_env("worker", self.server, **dirs, **extra)
        return env

    def crash_at(self, point: str) -> None:
        """Worker A, killed at `point`; its procrastinate heartbeat then made stale, so the next worker's
        start releases its job."""
        a = self.procs.spawn(WORKER, self.env(JOBHUNTER_FAULT=point))
        a.wait_event("fault_point_reached", READY_WITHIN_S + ENDED_WITHIN_S, point=point)
        assert a.exit_within(30) == FAULT_EXIT
        pg_one(
            self.server,
            "superuser",
            "UPDATE procrastinate_workers SET last_heartbeat = now() - interval '5 minutes'",
        )

    def start(self) -> Proc:
        """Worker B, ready."""
        b = self.procs.spawn(WORKER, self.env())
        b.wait_event("worker_ready", READY_WITHIN_S)
        return b

    def ended(self, b: Proc, operation_id: int) -> None:
        """Wait until the purge operation has ended (`succeeded` or `failed`)."""
        try:
            wait_until(
                lambda: operation(self.server, operation_id)[0] in ("succeeded", "failed"), ENDED_WITHIN_S
            )
        except TimeoutError as exc:
            tail = "".join(b.output().splitlines(True)[-30:])
            raise TimeoutError(f"{exc}\noperation: {operation(self.server, operation_id)}\n{tail}") from None


@pytest.fixture
def workers(migrated: PgServer, proc_env: Any, procs: ProcessGroup, volumes: Volumes) -> Workers:
    return Workers(migrated, proc_env, procs, volumes)


def one(server: PgServer, statement: str, *params: object) -> tuple[Any, ...]:
    found = pg_one(server, "superuser", statement, *params)
    assert found is not None, statement
    return found


def operation(server: PgServer, operation_id: int) -> tuple[Any, ...]:
    """`(status, last_error_code)` of the operation."""
    return one(server, "SELECT status, last_error_code FROM operations WHERE id = %s", operation_id)


def fence(server: PgServer, manifest_id: int) -> tuple[Any, ...]:
    """`(job_ids, released_at)` of the manifest's source fence."""
    return one(server, "SELECT job_ids, released_at FROM purge_fences WHERE manifest_id = %s", manifest_id)


def epoch(server: PgServer) -> int:
    return int(one(server, "SELECT worker_epoch FROM worker_heartbeat")[0])


async def scalar(engine: AsyncEngine, statement: sa.TextClause, **params: object) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(statement, params)).scalar_one()


async def revisions_targeted(engine: AsyncEngine, job_id: int) -> list[int]:
    async with engine.connect() as conn:
        return [int(r) for r in (await conn.execute(_TARGETS_OF_JOB, {"j": job_id})).scalars()]


async def recomputed(engine: AsyncEngine, job_id: int) -> int:
    """The job's canonical selection and its det-facts hook, as identity resolution runs them: its current
    revision afterwards."""
    async with engine.begin() as conn:
        active = await filters.lock_active_scopes(conn)
        result = await revisions.recompute_job(conn, job_id)
        await filters.refilter_job_live(conn, job_id, active=active)
    return result.revision_id


async def open_purge(engine: AsyncEngine) -> OpenedPurge:
    async with engine.begin() as conn:
        return await source_purge.request_source_purge(
            conn, source_id=ADZUNA, now=utcnow(), defer=defer_in_tx
        )


async def assert_completed(
    engine: AsyncEngine, server: PgServer, volumes: Volumes, opened: OpenedPurge
) -> None:
    """The manifest `completed`, its fence released in that transaction, every postcondition holding."""
    assert operation(server, opened.operation_id) == ("succeeded", None)
    async with engine.connect() as conn:
        manifest = await read_manifest(conn, opened.manifest_id)
        assert manifest is not None and manifest.status == "completed" and manifest.scope is not None
        assert await check_postconditions(conn, manifest.scope, files=volumes.roots) == []
        assert await source_purge.SourcePurge().postconditions(conn, manifest.scope) == []
    _, released = fence(server, opened.manifest_id)
    completed_at = one(
        server, "SELECT completed_at FROM deletion_manifests WHERE id = %s", opened.manifest_id
    )
    assert released is not None and released == completed_at[0]  # one transaction: one now()


async def test_c36_crash_mid_chunk_then_restart_while_missing_targets_runs(
    migrated: PgServer,
    engine: AsyncEngine,
    queue_app: procrastinate.App,
    workers: Workers,
    job_with_postings: JobWithPostings,
) -> None:
    async with engine.begin() as conn:
        tuple_id = await activate_score_tuple(conn)
    # a mixed job whose Adzuna posting supplies its employment type: its revision is tainted
    made = await job_with_postings(
        {"greenhouse": {}, ADZUNA: {"employment_types": ("full_time",), "employment_source": "structured"}}
    )
    tainted = await recomputed(engine, made.job_id)
    async with engine.connect() as conn:
        assert ADZUNA in (await conn.execute(_LINEAGE, {"r": tainted})).scalar_one()
    async with engine.begin() as conn:
        await conn.execute(_TARGET, {"r": tainted, "t": tuple_id, "j": made.job_id})
    opened = await open_purge(engine)
    workers.crash_at(source_purge.MID_REBUILD)

    # the derivatives and the source data committed; the rebuild's chunk rolled back
    job_ids, released = fence(migrated, opened.manifest_id)
    assert made.job_id in job_ids and released is None
    async with engine.connect() as conn:
        assert (await conn.execute(_JOB, {"j": made.job_id})).one().current_revision_id == tainted
    assert await revisions_targeted(engine, made.job_id) == []  # the tainted revision's target is gone
    assert scoring_reconcilers is not None  # imported for the reconciler it registers
    await run_reconciler("missing_targets", engine, now=utcnow(), epoch=epoch(migrated))
    assert await revisions_targeted(engine, made.job_id) == []  # fenced: the deleted target is not recreated

    b = workers.start()  # the restart, with the reconciler run again while it resumes the purge
    await run_reconciler("missing_targets", engine, now=utcnow(), epoch=epoch(migrated))
    assert tainted not in await revisions_targeted(engine, made.job_id)
    workers.ended(b, opened.operation_id)
    await assert_completed(engine, migrated, workers.volumes, opened)
    async with engine.connect() as conn:
        rebuilt = int((await conn.execute(_JOB, {"j": made.job_id})).one().current_revision_id)
        assert ADZUNA not in (await conn.execute(_LINEAGE, {"r": rebuilt})).scalar_one()
    assert rebuilt != tainted
    await run_reconciler("missing_targets", engine, now=utcnow(), epoch=epoch(migrated))
    assert await revisions_targeted(engine, made.job_id) == [rebuilt]  # the rebuilt revision's, only


async def test_c38_crash_after_the_postings_before_the_rebuild(
    migrated: PgServer,
    engine: AsyncEngine,
    queue_app: procrastinate.App,
    workers: Workers,
    job_with_postings: JobWithPostings,
) -> None:
    # a mixed job whose Adzuna posting gives it its salary only: its lineage never names Adzuna
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
    async with engine.connect() as conn:
        assert (await conn.execute(_LINEAGE, {"r": revision})).scalar_one() == ["greenhouse"]
        assert (await conn.execute(_JOB, {"j": mixed.job_id})).one().salary is not None
    opened = await open_purge(engine)
    workers.crash_at(source_purge.BEFORE_REBUILD)

    job_ids, released = fence(migrated, opened.manifest_id)
    assert {mixed.job_id, only.job_id} <= set(job_ids) and released is None
    async with engine.connect() as conn:
        links = (await conn.execute(_LINKS, {"j": mixed.job_id})).one()
        assert (links.postings, links.lineage) == (0, 0)  # no posting or lineage names Adzuna any more
        assert (await conn.execute(_IS_FENCED, {"j": mixed.job_id})).scalar_one() is True  # through job_ids
        assert ADZUNA in (await conn.execute(_PROVENANCE_SOURCES, {"j": mixed.job_id})).scalar_one()
        manifest = await read_manifest(conn, opened.manifest_id)
        assert manifest is not None and manifest.scope is not None
        derived = await source_purge.SourcePurge().compute(conn, manifest, now=utcnow())
    assert mixed.job_id not in derived["affected"]["job_ids"]  # a re-derivation would miss it

    b = workers.start()
    workers.ended(b, opened.operation_id)
    await assert_completed(engine, migrated, workers.volumes, opened)
    async with engine.connect() as conn:
        row = (await conn.execute(_JOB, {"j": mixed.job_id})).one()
        assert row.current_revision_id == revision and row.salary is None  # rebuilt, its revision reused
        assert (await conn.execute(_PROVENANCE_SOURCES, {"j": mixed.job_id})).scalar_one() == ["greenhouse"]
        gone = await conn.execute(sa.text("SELECT count(*) FROM jobs WHERE id = :j"), {"j": only.job_id})
        assert gone.scalar_one() == 0
