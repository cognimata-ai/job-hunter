"""Export archives across a restore and its replay (spec §19.5, §20.3; plan M5 Task 26, Deviation 55, Task
17's state table, its restore column): `restore run`'s steps 7, 7b and 8 (`backup.restore.complete_restore`).

Each backup set is a real `pg_dump` of the seeded shared database (`tests.support.privacy_seed`), restored
with `tests.support.restore.dump_and_restore` into a database of its own, whose exports volume is a directory
of the test. Both sets are valid under `exports_one_running`: the seed's `ready` export and one `running`
export. After the set, a golden-case deletion runs live (its export ops name every live export at its A2): one
of the two exports is ended first in the live database, so the newer manifest names the other one only:
- (a) `running_named`: the `running` export is named by the newer manifest, the `ready` one by none;
- (b) `ready_named`: the `ready` export is named by the newer manifest, the `running` one by none.
A stale archive (the `ready` export's) lies in the restored exports directory each time. restic is not called
at these steps. Every datum is invented.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

import pytest
import sqlalchemy as sa
from jobhunter.api.errors import Problem
from jobhunter.api.routes.privacy import get_export
from jobhunter.backup.restore import complete_restore
from jobhunter.clock import utcnow
from jobhunter.db.transitions import transition
from jobhunter.errors import Refusal
from jobhunter.privacy.export import request_export
from jobhunter.privacy.kinds.golden_case import request_golden_case_delete
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.fixtures import ConfigFactory
from tests.support.pg import PgServer
from tests.support.purges import EXPORT, Purger, completed, seeded
from tests.support.restore import RestoredDatabase, dump_and_restore, restored_database

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("current_epoch")]

Named = Literal["running_named", "ready_named"]

_RUNNING_EXPORT: Final = sa.text(
    "INSERT INTO exports (status, created_at, expires_at) "
    "VALUES ('running', now(), now() + interval '7 days') RETURNING id"
)
_RUNNING: Final = sa.text("SELECT count(*) FROM exports WHERE status = 'running'")
_NAMED: Final = sa.text(
    "SELECT coalesce(array_agg(DISTINCT i ORDER BY i), '{}') FROM deletion_manifests m "
    "CROSS JOIN LATERAL jsonb_array_elements(m.scope -> 'operations') o "
    "CROSS JOIN LATERAL jsonb_array_elements_text(o -> 'ids') i "
    "WHERE m.id = :m AND o ->> 'op' = 'field_redact' AND o ->> 'table' = 'exports'"
)


@dataclass(frozen=True)
class RestoredSet:
    """A restored set after steps 7, 7b and 8: its database, the named and unnamed exports by state, and
    whether the replayed phase held."""

    db: RestoredDatabase
    named: int
    unnamed: int
    unnamed_state: str
    replayed_ok: bool


@pytest.fixture
async def restored_set(
    request: pytest.FixtureRequest,
    migrated: PgServer,
    engine: AsyncEngine,
    purger: Purger,
    make_config: ConfigFactory,
    tmp_path: Path,
) -> AsyncIterator[RestoredSet]:
    named: Named = request.param
    seed = await seeded(engine)
    ready = seed.ready_export_id
    async with engine.begin() as conn:
        running = int((await conn.execute(_RUNNING_EXPORT)).scalar_one())
    stale = (await one(engine, EXPORT, e=ready)).storage_path
    async with restored_database(migrated, tmp_path / "restored", create=False) as db:
        await dump_and_restore(migrated, source_db="jobhunter", target_db=db.name)
        # Live, after the set: the export the newer manifest must not name is ended first.
        async with engine.begin() as conn:
            if named == "running_named":
                await transition(conn, "exports", ready, ["ready"], "expired", set_={"storage_path": None})
            else:
                await transition(conn, "exports", running, ["running"], "failed")
        async with engine.begin() as conn:
            opened = await request_golden_case_delete(conn, seed.closed_case_id, now=utcnow(), defer=None)
        await purger.run(opened.operation_id)
        await completed(purger, opened.manifest_id, opened.operation_id)
        named_id, unnamed_id = (running, ready) if named == "running_named" else (ready, running)
        assert await scalar(engine, _NAMED, m=opened.manifest_id) == [str(named_id)]
        (db.files.exports / stale).write_bytes(b"an encrypted archive the set does not hold")

        cfg = make_config(
            "restore",
            JOBHUNTER_DB_NAME=db.name,
            JOBHUNTER_MANIFESTS_DIR=str(purger.cfg.manifests_dir),
            JOBHUNTER_CV_STORE_DIR=str(db.files.cv_store),
            JOBHUNTER_EXPORTS_DIR=str(db.files.exports),
        )
        report = await complete_restore(cfg, now=utcnow())
        unnamed_state = "expired" if named == "running_named" else "failed"
        yield RestoredSet(db, named_id, unnamed_id, unnamed_state, report.ok)


async def one(engine: AsyncEngine, statement: sa.TextClause, **params: object) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(statement, params)).one()


async def scalar(engine: AsyncEngine, statement: sa.TextClause, **params: object) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(statement, params)).scalar_one()


async def download(
    make_config: ConfigFactory, restored: RestoredSet, export_id: int
) -> tuple[int, str | None]:
    """The export's download as the route answers it (`GET /api/privacy/exports/{id}?download=1`): its
    problem's status and detail."""
    cfg = make_config("api", JOBHUNTER_EXPORTS_DIR=str(restored.db.files.exports))
    with pytest.raises(Problem) as answered:
        await get_export(export_id=export_id, engine=restored.db.engine, cfg=cfg, keys=[], download=True)
    return answered.value.status, answered.value.detail


BOTH_SETS: Final = pytest.mark.parametrize("restored_set", ["running_named", "ready_named"], indirect=True)


@BOTH_SETS
async def test_restored_exports_follow_the_state_table(
    restored_set: RestoredSet, make_config: ConfigFactory
) -> None:
    """The named export is `purged` by the replay; the unnamed `ready` one `expired`, the unnamed `running`
    one `failed` (step 7b); every postcondition holds (step 8); the directory is empty; each download answers
    as the table says: 410 for `purged` and `expired`, 409 for `failed`."""
    db = restored_set.db
    assert restored_set.replayed_ok
    assert tuple(await one(db.engine, EXPORT, e=restored_set.named)) == ("purged", None)
    assert tuple(await one(db.engine, EXPORT, e=restored_set.unnamed)) == (restored_set.unnamed_state, None)
    assert list(db.files.exports.iterdir()) == []
    assert await download(make_config, restored_set, restored_set.named) == (410, "purged")
    expected = (410, "expired") if restored_set.unnamed_state == "expired" else (409, "failed")
    assert await download(make_config, restored_set, restored_set.unnamed) == expected


@BOTH_SETS
async def test_the_unique_index_survives_the_restore(restored_set: RestoredSet) -> None:
    """After either restore no export is `running`, so a new export is accepted; a second one while it runs
    is refused 409 `export_running` by `exports_one_running`."""
    engine = restored_set.db.engine
    assert await scalar(engine, _RUNNING) == 0
    async with engine.begin() as conn:
        first = await request_export(conn, now=utcnow(), defer=None)
    assert tuple(await one(engine, EXPORT, e=first)) == ("running", None)
    with pytest.raises(Refusal) as refused:
        async with engine.begin() as conn:
            await request_export(conn, now=utcnow(), defer=None)
    assert (refused.value.status, refused.value.detail) == (409, "export_running")
    assert await scalar(engine, _RUNNING) == 1
