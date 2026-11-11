"""The purge test harness (M5 Tasks 12-16): a `purge` operation run as the worker runs it.

- `purge_cfg`: the worker's configuration with a manifests and an exports directory of the test's own (each
  mode 0700, under `tmp_path`) and the api's CV store (`cv_store_dir`): the worker and the api share the
  volume, so a test that runs a purge and the api together (Task 14's race tests) has one store;
- `purger`: a `Purger` over the shared engine and `purge_cfg`, with the worker's operation dependencies (the
  purge reads none of the profile, score or LLM collaborators, so those are left unset). `run(operation_id)`
  executes the operation as `run_operation` does (`operations.execute_operation`, epoch 1);
  `again(operation_id)` makes a requeued operation due at once, then runs it; `manifest_file(manifest_uuid)`
  is the manifest's canonical file in `cfg.manifests_dir`; `file_roots` the volumes a `file_remove` names
  (`FileRoots`);
- `manifest_of(engine, manifest_id)`: the manifest's row (it must exist);
- `completed(purger, manifest_id, operation_id)`: the manifest, asserted `completed` with its content, its
  operation `succeeded`, every fence of it released and every postcondition holding.

M5 Task 25 adds what the replay tests (`test_replay.py`, `test_typed_replay.py`) share:
- `seeded(engine, **kwargs)`: the two-year-old seed (`tests.support.privacy_seed`) at `utcnow()`, committed;
- `request_source_purge(engine, *, now=None)`: the Adzuna purge's A0 in a transaction of its own (no
  deferrer: the purger runs the operation);
- `stopped_at(purger, opened, point, monkeypatch, module=purge)`: the purge run by the handler until a crash
  at `module`'s fault point `point` (`tests.support.faults.raising_at`); its manifest, still `executing`;
- `recomputed(engine, job_id)`: the job's canonical selection and det-facts hook, as identity resolution runs
  them; its current revision;
- `one`, `scalar` and `rows`: a statement's one row, one value or rows, on a connection of its own;
- `completions(replay_log)`: a manifest's completed replays (`replay_log`'s entries with their counts);
- the statements `JOB`, `JOB_EXISTS`, `IS_FENCED`, `PROVENANCE_SOURCES` (the sources a job's
  `field_provenance` names), `ALERT` (an invented alert, `:key` its dedup key) and `EXPORT`.

Shared with the restore test since M5 Task 26 (moved from `test_typed_replay.py`):
- `request_delete_everything(engine)`: delete-everything's A0 as the route runs it, its idempotency row first;
- `live`: the purger with the worker's Batches client on M3's fake (`fake_llm`), so a live delete-everything
  can cancel a recorded batch; the waits between its cancels are recorded (`Sleeps`), never slept.

It is a plugin module of the root conftest (`pytest_plugins`), so its fixtures reach every test and its
assertions are rewritten.
"""

import dataclasses
import itertools
import secrets
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.config import AppConfig
from jobhunter.dedup import revisions
from jobhunter.llm.batches import BatchesClient
from jobhunter.privacy import purge
from jobhunter.privacy.kinds import delete_everything, source_purge
from jobhunter.privacy.manifests import (
    FileRoots,
    ManifestRow,
    check_postconditions,
    manifest_file_name,
    read_manifest,
)
from jobhunter.privacy.purge import OpenedPurge
from jobhunter.profile.storage import CvStore
from jobhunter.scoring import filters
from jobhunter.worker import operations as ops
from jobhunter.worker.operations import OperationDeps
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.faults import no_fault, raising_at
from tests.support.llm import FakeLLM
from tests.support.pg import PgServer
from tests.support.privacy_seed import SeedIds, seed_two_year_old_graph

_DUE: Final = sa.text("UPDATE operations SET not_before = NULL WHERE id = :i")
_STATUS: Final = sa.text("SELECT status FROM operations WHERE id = :i")
_FENCES_RELEASED: Final = sa.text(
    "SELECT bool_and(released_at IS NOT NULL) FROM purge_fences WHERE manifest_id = :m"
)


@pytest.fixture
def purge_cfg(make_config: Any, migrated: PgServer, tmp_path: Path, cv_store_dir: Path) -> AppConfig:
    """The worker's configuration with a manifests and an exports directory of this test's own and the api's
    CV store."""
    dirs = {name: tmp_path / name for name in ("manifests", "exports")}
    for directory in dirs.values():
        directory.mkdir(mode=0o700)
    cfg: AppConfig = make_config(
        "worker",
        migrated,
        JOBHUNTER_MANIFESTS_DIR=str(dirs["manifests"]),
        JOBHUNTER_EXPORTS_DIR=str(dirs["exports"]),
        JOBHUNTER_CV_STORE_DIR=str(cv_store_dir),
    )
    return cfg


@dataclass(frozen=True)
class Purger:
    """Runs a purge operation as the worker's `run_operation` does, with the worker's dependencies."""

    engine: AsyncEngine
    cfg: AppConfig
    deps: OperationDeps

    async def run(self, operation_id: int) -> None:
        await ops.execute_operation(self.engine, operation_id, epoch=1, deps=self.deps)

    async def again(self, operation_id: int) -> None:
        """The requeued operation made due at once, then run."""
        async with self.engine.begin() as conn:
            await conn.execute(_DUE, {"i": operation_id})
        await self.run(operation_id)

    def manifest_file(self, manifest_uuid: uuid.UUID) -> Path:
        """The manifest's canonical file in the manifests directory."""
        return self.cfg.manifests_dir / manifest_file_name(manifest_uuid)

    @property
    def file_roots(self) -> FileRoots:
        """The volumes a `file_remove` names: the CV store and the exports directory."""
        return FileRoots(cv_store=self.cfg.cv_store_dir, exports=self.cfg.exports_dir)


@pytest.fixture
def purger(engine: AsyncEngine, purge_cfg: AppConfig) -> Purger:
    unused: Any = None  # the purge reads none of the profile, score or LLM collaborators
    deps = OperationDeps(
        cfg=purge_cfg,
        gateway=unused,
        parse_client=unused,
        cv_store=CvStore(purge_cfg.cv_store_dir),
        profile_graphs=unused,
        embedder=None,
        clock=utcnow,
        score_steps=unused,
        batches_client=unused,
    )
    return Purger(engine, purge_cfg, deps)


async def manifest_of(engine: AsyncEngine, manifest_id: int) -> ManifestRow:
    async with engine.connect() as conn:
        manifest = await read_manifest(conn, manifest_id)
    assert manifest is not None
    return manifest


async def completed(purger: Purger, manifest_id: int, operation_id: int) -> ManifestRow:
    """The purge's manifest, asserted `completed` (with its content) with its operation `succeeded`, every
    fence of it released and every postcondition holding."""
    manifest = await manifest_of(purger.engine, manifest_id)
    assert manifest.status == "completed" and manifest.scope is not None
    async with purger.engine.connect() as conn:
        assert (await conn.execute(_STATUS, {"i": operation_id})).scalar_one() == "succeeded"
        assert (await conn.execute(_FENCES_RELEASED, {"m": manifest_id})).scalar_one() is not False
        assert await check_postconditions(conn, manifest.scope, files=purger.file_roots) == []
    return manifest


# ---- M5 Task 25: the replay tests' shared helpers ----------------------------------------------------------

JOB: Final = sa.text("SELECT current_revision_id, salary FROM jobs WHERE id = :j")
JOB_EXISTS: Final = sa.text("SELECT EXISTS (SELECT 1 FROM jobs WHERE id = :j)")
IS_FENCED: Final = sa.text("SELECT is_fenced(:j)")
PROVENANCE_SOURCES: Final = sa.text(
    "SELECT coalesce(array_agg(DISTINCT si.source_id ORDER BY si.source_id), '{}') FROM jobs j "
    "CROSS JOIN LATERAL jsonb_each(j.field_provenance) e JOIN source_instances si "
    "ON si.id = CAST(e.value ->> 'source_instance_id' AS bigint) WHERE j.id = :j"
)
ALERT: Final = sa.text(
    "INSERT INTO alerts (code, severity, dedup_key, message) "
    "VALUES ('test.replay', 'info', :key, 'an invented alert') RETURNING id"
)
EXPORT: Final = sa.text("SELECT status, storage_path FROM exports WHERE id = :e")


async def seeded(engine: AsyncEngine, **kwargs: Any) -> SeedIds:
    async with engine.begin() as conn:
        return await seed_two_year_old_graph(conn, now=utcnow(), **kwargs)


async def request_source_purge(engine: AsyncEngine, *, now: datetime | None = None) -> OpenedPurge:
    async with engine.begin() as conn:
        return await source_purge.request_source_purge(
            conn, source_id="adzuna", now=utcnow() if now is None else now, defer=None
        )


async def stopped_at(
    purger: Purger,
    opened: OpenedPurge,
    point: str,
    monkeypatch: pytest.MonkeyPatch,
    module: Any = purge,
) -> ManifestRow:
    monkeypatch.setattr(module, "afault_point", raising_at(point))
    await purger.run(opened.operation_id)
    monkeypatch.setattr(module, "afault_point", no_fault)
    manifest = await manifest_of(purger.engine, opened.manifest_id)
    assert manifest.status == "executing"
    return manifest


async def recomputed(engine: AsyncEngine, job_id: int) -> int:
    async with engine.begin() as conn:
        active = await filters.lock_active_scopes(conn)
        result = await revisions.recompute_job(conn, job_id)
        await filters.refilter_job_live(conn, job_id, active=active)
    return result.revision_id


async def one(engine: AsyncEngine, statement: sa.TextClause, **params: object) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(statement, params)).one()


async def scalar(engine: AsyncEngine, statement: sa.TextClause, **params: object) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(statement, params)).scalar_one()


async def rows(engine: AsyncEngine, statement: sa.TextClause, **params: object) -> list[Any]:
    async with engine.connect() as conn:
        return list((await conn.execute(statement, params)).all())


def completions(replay_log: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [entry for entry in replay_log if "counts" in entry]


# ---- M5 Task 26: delete-everything's live run (M3's fake Batches API, as Task 16's tests run it) ----------

_IDEMPOTENCY_ROW: Final = sa.text(
    "INSERT INTO api_idempotency (route, idem_key, request_sha256, status, created_at, expires_at) "
    "VALUES ('POST /api/privacy/delete-everything', :key, '\\x01', 'in_progress', now(), "
    "now() + interval '1 day') RETURNING id"
)
_KEYS = itertools.count(1)


async def request_delete_everything(engine: AsyncEngine) -> OpenedPurge:
    """A0 as the route runs it: the request's idempotency row, then the request, in one transaction."""
    async with engine.begin() as conn:
        key = f"replay-delete-everything-{next(_KEYS):06d}-{secrets.token_hex(3)}"
        row_id = int((await conn.execute(_IDEMPOTENCY_ROW, {"key": key})).scalar_one())
        return await delete_everything.request_delete_everything(
            conn, now=utcnow(), idempotency_row_id=row_id, defer=None
        )


@dataclass
class Sleeps:
    waited: list[float] = dataclasses.field(default_factory=list)

    async def __call__(self, seconds: float) -> None:
        self.waited.append(seconds)


@pytest.fixture
async def live(purger: Purger, fake_llm: FakeLLM, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Purger]:
    """The purger with the worker's Batches client on the fake; the waits between cancels recorded."""
    monkeypatch.setattr(delete_everything, "CANCEL_SLEEP", Sleeps())
    async with fake_llm.client() as http:
        client = BatchesClient(fake_llm.cfg, http_client=http)
        yield Purger(purger.engine, purger.cfg, dataclasses.replace(purger.deps, batches_client=client))
