"""The purge test harness (M5 Tasks 12-16): a `purge` operation run as the worker runs it.

- `purge_cfg`: the worker's configuration with a manifests, an exports and a CV-store directory of the test's
  own (each mode 0700, under `tmp_path`);
- `purger`: a `Purger` over the shared engine and `purge_cfg`, with the worker's operation dependencies (the
  purge reads none of the profile, score or LLM collaborators, so those are left unset). `run(operation_id)`
  executes the operation as `run_operation` does (`operations.execute_operation`, epoch 1);
  `again(operation_id)` makes a requeued operation due at once, then runs it; `manifest_file(manifest_uuid)`
  is the manifest's canonical file in `cfg.manifests_dir`; `file_roots` the volumes a `file_remove` names
  (`FileRoots`);
- `manifest_of(engine, manifest_id)`: the manifest's row (it must exist);
- `completed(purger, manifest_id, operation_id)`: the manifest, asserted `completed` with its content, its
  operation `succeeded`, every fence of it released and every postcondition holding.

It is a plugin module of the root conftest (`pytest_plugins`), so its fixtures reach every test and its
assertions are rewritten.
"""

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.config import AppConfig
from jobhunter.privacy.manifests import (
    FileRoots,
    ManifestRow,
    check_postconditions,
    manifest_file_name,
    read_manifest,
)
from jobhunter.profile.storage import CvStore
from jobhunter.worker import operations as ops
from jobhunter.worker.operations import OperationDeps
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.pg import PgServer

_DUE: Final = sa.text("UPDATE operations SET not_before = NULL WHERE id = :i")
_STATUS: Final = sa.text("SELECT status FROM operations WHERE id = :i")
_FENCES_RELEASED: Final = sa.text(
    "SELECT bool_and(released_at IS NOT NULL) FROM purge_fences WHERE manifest_id = :m"
)


@pytest.fixture
def purge_cfg(make_config: Any, migrated: PgServer, tmp_path: Path) -> AppConfig:
    """The worker's configuration with a manifests, an exports and a CV directory of this test's own."""
    dirs = {name: tmp_path / name for name in ("manifests", "exports", "cv_store")}
    for directory in dirs.values():
        directory.mkdir(mode=0o700)
    cfg: AppConfig = make_config(
        "worker",
        migrated,
        JOBHUNTER_MANIFESTS_DIR=str(dirs["manifests"]),
        JOBHUNTER_EXPORTS_DIR=str(dirs["exports"]),
        JOBHUNTER_CV_STORE_DIR=str(dirs["cv_store"]),
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
