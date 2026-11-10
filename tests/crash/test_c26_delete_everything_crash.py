"""Crash case C26 (spec §18.3, §20.4, §19.5 step 3; plan M5 Task 16): a real worker dies in the middle of a
delete-everything and the next one finishes it.

Delete-everything is requested in process on the two-year-old seed (`request_delete_everything` beside the
request's idempotency row, its job deferred), with the seed's `ready` export's file on the worker's exports
volume. Worker A (`JOBHUNTER_FAULT=privacy.purge.mid_delete`) claims the purge and is killed after the kind's
chunks have committed and before the export ops (exit 86, no cleanup); its procrastinate heartbeat is made
stale, so worker B's start releases the job, and B's takeover sweep gives the operation back (as in C31 and
C36). The manifest stays `executing` with its global fence active in between. B resumes idempotently: every
chunk of the kind finds what is left (nothing) without a foreign-key or CHECK violation, the export ops run,
and the completing transaction deletes the sessions, releases the fence and marks the manifest `completed`.

Every datum is invented; no paid call (the worker is in fake LLM mode: the recorded cancel of the seed's
`submitted` batch finds no such batch on the fake, and is given up after its three tries).
"""

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import procrastinate
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.privacy import purge
from jobhunter.privacy.kinds import delete_everything
from jobhunter.privacy.manifests import FileRoots, check_postconditions, read_manifest
from jobhunter.privacy.purge import OpenedPurge
from jobhunter.worker.tasks import defer_in_tx
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.pg import PgServer, pg_one
from tests.support.privacy_seed import SeedIds, seed_two_year_old_graph
from tests.support.procs import Proc, ProcessGroup, wait_until

pytestmark = [pytest.mark.crash, pytest.mark.slow]
WORKER: Final = [sys.executable, "-m", "jobhunter.cli", "worker"]
FAULT_EXIT: Final = 86
READY_WITHIN_S: Final = 60
ENDED_WITHIN_S: Final = 180
_IDEMPOTENCY_ROW: Final = sa.text(
    "INSERT INTO api_idempotency (route, idem_key, request_sha256, status, created_at, expires_at) "
    "VALUES ('POST /api/privacy/delete-everything', 'c26-delete-everything', '\\x01', 'in_progress', now(), "
    "now() + interval '1 day') RETURNING id"
)
_TABLES: Final = sa.text(
    "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') ORDER BY c.relname"
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
    """Spawns `jobhunter worker` children on this test's volumes, with an optional fault point (C36's)."""

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


async def requested(engine: AsyncEngine) -> OpenedPurge:
    async with engine.begin() as conn:
        row_id = int((await conn.execute(_IDEMPOTENCY_ROW)).scalar_one())
        return await delete_everything.request_delete_everything(
            conn, now=utcnow(), idempotency_row_id=row_id, defer=defer_in_tx
        )


async def holding(engine: AsyncEngine) -> list[str]:
    """The tables outside `DELETE_EVERYTHING_KEEPS` that still hold a row."""
    async with engine.connect() as conn:
        tables = [str(name) for name in (await conn.execute(_TABLES)).scalars()]
        return [
            table
            for table in tables
            if table not in delete_everything.DELETE_EVERYTHING_KEEPS
            and (await conn.execute(sa.text(f'SELECT EXISTS (SELECT 1 FROM "{table}")'))).scalar_one()  # noqa: S608
        ]


async def test_a_crash_mid_delete_resumes_to_completed(
    migrated: PgServer, engine: AsyncEngine, queue_app: procrastinate.App, workers: Workers
) -> None:
    async with engine.begin() as conn:
        seed: SeedIds = await seed_two_year_old_graph(conn, now=utcnow())
    export = workers.volumes.exports / f"{seed.ready_export_id}-seed.jhx"
    export.write_bytes(b"\x00 encrypted segments")
    opened = await requested(engine)
    workers.crash_at(purge.MID_DELETE)

    # the kind's chunks committed, the export ops not yet: the manifest `executing`, its fence active
    status, released = one(
        migrated,
        "SELECT m.status, f.released_at FROM deletion_manifests m "
        "JOIN purge_fences f ON f.manifest_id = m.id WHERE m.id = %s",
        opened.manifest_id,
    )
    assert (status, released) == ("executing", None)
    assert one(migrated, "SELECT count(*) FROM jobs")[0] == 0
    assert one(migrated, "SELECT status FROM exports WHERE id = %s", seed.ready_export_id)[0] == "ready"
    assert export.exists()
    assert one(migrated, "SELECT count(*) FROM sessions")[0] >= 1  # the completing transaction's to delete

    b = workers.start()
    workers.ended(b, opened.operation_id)
    assert operation(migrated, opened.operation_id) == ("succeeded", None)
    async with engine.connect() as conn:
        manifest = await read_manifest(conn, opened.manifest_id)
        assert manifest is not None and manifest.status == "completed" and manifest.scope is not None
        assert await check_postconditions(conn, manifest.scope, files=workers.volumes.roots) == []
        assert await delete_everything.DeleteEverything().postconditions(conn, manifest.scope) == []
    _, released = one(
        migrated, "SELECT kind, released_at FROM purge_fences WHERE manifest_id = %s", opened.manifest_id
    )
    completed_at = one(
        migrated, "SELECT completed_at FROM deletion_manifests WHERE id = %s", opened.manifest_id
    )
    assert released is not None and released == completed_at[0]  # one transaction: one now()
    assert one(migrated, "SELECT status, storage_path FROM exports WHERE id = %s", seed.ready_export_id) == (
        "purged",
        None,
    )
    assert not export.exists()
    assert await holding(engine) == []
