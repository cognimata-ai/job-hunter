"""Settings test helpers (M2 Task 17; Task 18 extends it): the save and read routes as a client uses them.

- `settings_body(client)`: `GET /api/settings` (200 required);
- `warning_codes(client, payload)`: the warning codes `POST /api/settings/dry-run` gives for `payload`, so a
  save acknowledges exactly its own warnings;
- `save(client, payload, *, base=None, key=None)`: `POST /api/settings` over the active version (or `base`),
  acknowledging the dry-run's warnings, with a fresh `Idempotency-Key` unless one is given; 201 required, its
  body returned;
- `save_scope(client, radius_km=…)`: the active payload with one scope location, Berlin, at that radius,
  saved as `save` does (the 201 body);
- `BERLIN`, `berlin(radius_km)`: that location (the GeoNames id of Berlin; coordinates of the city centre);
- `activate_profile(engine, seniority=…)`: a confirmed synthetic profile (an invented candidate,
  `SYNTHETIC_PROFILE`) made active with its tuple over the active model policy, as Task 21's confirm leaves
  the profile and tuple pointers (its scope part is not done here). Returns `(profile_version_id,
  llm_tuple_id)`;
- `active_row(engine)` and `generation(engine)`: the `active_config` row and the ranking generation.

Task 18 adds the scope apply's side:
- `active_scope_id(engine)`, `pending_scope_id(engine)`: the two scope pointers of `active_config`;
- `scope_ids_of(engine, *saved)`: the scope version of each saved version (a save's 201 body), in order;
- `operation_end(engine, op_id)`: the operation's `(status, progress)`;
- `incomplete_jobs_for(engine, scope_id)`: how many unmerged jobs lack a current `hard_filter_results` row for
  the scope (none, another `inputs_hash` than `jobs.det_facts_hash`, or another algorithm version). It states
  §13.4 step 3's check in a statement of its own (`INCOMPLETE_SQL`), independent of `filters.STALE_PREDICATE`,
  so a test of the apply does not grade the apply's predicate with itself (`incomplete_jobs_sync` runs it on
  an engine of its own);
- `delete_procrastinate_job_of(engine, op_id)` and `procrastinate_job_exists_for(engine, op_id)`: the
  operation's `run_operation` job removed (a save whose job was lost, C30), and whether one waits or runs;
- the sync helpers of the crash tests, each on a connection of its own: `save_scope_sync(server,
  radius_km=…)` saves the active payload with one Berlin location (the save path, `save_settings`, in its own
  transaction, acknowledging the dry-run's warnings; its job deferred through the process's task app) and
  returns `(scope_version_id, apply_scope_operation_id)`; `active_scope_sync`, `pending_scope_sync` and
  `operation_status_sync` read through `pg_one`;
- the `seeded_jobs_sync` fixture: `SEEDED_JOBS` (1,200) jobs with the active scope's results
  (`collect.insert_jobs` on an engine of its own), so the precompute takes three chunks; their ids.

Task 22 (fix round 1) adds the save that has not committed yet:
- `held_save(engine, client, edit)`: an async context manager that runs `save_settings` (internal, `edit`
  applied to a copy of the active payload) in a transaction of its own and leaves it open, holding
  `active_config` `FOR UPDATE` and the pointer flipped inside it. It yields a `HeldSave` (`version_id`,
  `version_no`, `payload`, `commit()`); a request queued behind it waits for `commit()` and then meets the
  save's version. Leaving the block without `commit()` rolls the save back;
- `change_interval(payload)`: an `edit` for it that changes `schedule.interval_hours` (class `schedule`, so
  no scope apply and no job).
"""

import asyncio
import copy
import hashlib
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Final

import httpx
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db.session import make_null_engine
from jobhunter.db.transitions import transition
from jobhunter.llm.schemas.profile import ConfirmedProfile
from jobhunter.scoring.versioning import DET_ALGORITHM_VERSION
from jobhunter.settings.dryrun import dry_run
from jobhunter.settings.model import Settings
from jobhunter.settings.save import SaveRequest, ensure_tuple, save_settings
from jobhunter.worker.tasks import defer_in_tx, job_task_name, make_queue_app
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.support.collect import insert_jobs
from tests.support.pg import PgServer, config_for, pg_one
from tests.support.profile import SYNTHETIC_PROFILE, idem

SETTINGS_URL: Final = "/api/settings"
DRY_RUN_URL: Final = "/api/settings/dry-run"
BERLIN: Final[dict[str, Any]] = {
    "label": "Berlin",
    "gazetteer_id": "geonames:2950159",
    "lat": 52.52437,
    "lon": 13.41053,
    "radius_km": 25,
}

_ACTIVE = sa.text("SELECT * FROM active_config WHERE id = 1")
_GENERATION = sa.text("SELECT generation FROM ranking_state WHERE id = 1")
_LOCK_ACTIVE = sa.text("SELECT 1 FROM active_config WHERE id = 1 FOR UPDATE")
_CV_FILE = sa.text(
    "INSERT INTO cv_files (sha256, mime, size_bytes) VALUES (:h, 'text/plain', 1) RETURNING id"
)
_DRAFT = sa.text(
    "INSERT INTO profile_drafts (cv_file_id, status, reviewed_text_sha256, draft_json) "
    "VALUES (:cv, 'ready', :h, CAST(:profile AS jsonb)) RETURNING id"
)
_VERSION = sa.text(
    "INSERT INTO profile_versions (version_no, draft_id, cv_file_id, profile_json, profile_hash) "
    "VALUES ((SELECT coalesce(max(version_no), 0) + 1 FROM profile_versions), :d, :cv, "
    "CAST(:profile AS jsonb), :h) RETURNING id"
)
_ACTIVATE = sa.text(
    "UPDATE active_config SET profile_version_id = :pv, llm_tuple_id = :t, updated_at = now() WHERE id = 1"
)


def berlin(radius_km: int) -> dict[str, Any]:
    return {**BERLIN, "radius_km": radius_km}


async def settings_body(client: httpx.AsyncClient) -> dict[str, Any]:
    """`GET /api/settings` as the client sees it."""
    r = await client.get(SETTINGS_URL)
    assert r.status_code == 200, (r.status_code, r.text)
    body: dict[str, Any] = r.json()
    return body


async def warning_codes(client: httpx.AsyncClient, payload: Any) -> list[str]:
    """The codes of the warnings `POST /api/settings/dry-run` gives for `payload`, in its order."""
    r = await client.post(DRY_RUN_URL, json={"payload": payload})
    assert r.status_code == 200, (r.status_code, r.text)
    return [str(w["code"]) for w in r.json()["warnings"]]


def fresh_key(prefix: str = "settings-save") -> str:
    """A new `Idempotency-Key` (16 to 128 characters of `[A-Za-z0-9_-]`)."""
    return f"{prefix}-{uuid.uuid4().hex}"


async def save(
    client: httpx.AsyncClient, payload: Any, *, base: int | None = None, key: str | None = None
) -> dict[str, Any]:
    """`POST /api/settings` of `payload` over `base` (the active version by default), acknowledging the
    dry-run's warnings; 201 required, its body returned."""
    if base is None:
        base = int((await settings_body(client))["version"]["id"])
    r = await client.post(
        SETTINGS_URL,
        json={
            "payload": payload,
            "base_version_id": base,
            "acknowledged_warnings": await warning_codes(client, payload),
        },
        headers=idem(key or fresh_key()),
    )
    assert r.status_code == 201, (r.status_code, r.text)
    body: dict[str, Any] = r.json()
    return body


async def save_scope(client: httpx.AsyncClient, *, radius_km: int) -> dict[str, Any]:
    """The active payload with one scope location, Berlin, at `radius_km`, saved (the 201 body)."""
    active = await settings_body(client)
    payload = copy.deepcopy(active["payload"])
    payload["scope"]["locations"] = [berlin(radius_km)]
    return await save(client, payload, base=int(active["version"]["id"]))


def change_interval(payload: dict[str, Any]) -> None:
    """An `edit` for `held_save`: another `schedule.interval_hours` (class `schedule`)."""
    payload["schedule"]["interval_hours"] = 7 if payload["schedule"]["interval_hours"] != 7 else 8


@dataclass(frozen=True)
class HeldSave:
    """A settings save whose transaction is still open (`held_save`)."""

    version_id: int
    version_no: int
    payload: dict[str, Any]
    _conn: AsyncConnection

    async def commit(self) -> None:
        await self._conn.commit()


@asynccontextmanager
async def held_save(
    engine: AsyncEngine, client: httpx.AsyncClient, edit: Callable[[dict[str, Any]], None]
) -> AsyncIterator[HeldSave]:
    """`save_settings` (internal) of the active payload changed by `edit`, in a transaction of its own that
    stays open: `active_config` is locked and its pointer flipped to the new version, none of it visible to
    anyone else until `commit()`. A request that needs the lock queues behind it. A block that ends without
    `commit()` rolls the save back."""
    active = await settings_body(client)
    payload = copy.deepcopy(active["payload"])
    edit(payload)
    conn = await engine.connect()
    try:
        outcome = await save_settings(
            conn,
            SaveRequest(
                payload=payload,
                base_version_id=int(active["version"]["id"]),
                acknowledged_warnings=frozenset(),
            ),
            now=utcnow(),
            defer=defer_in_tx,
            internal=True,
        )
        yield HeldSave(outcome.version_id, outcome.version_no, payload, conn)
    finally:
        await conn.close()


async def active_row(engine: AsyncEngine) -> sa.Row[Any]:
    async with engine.connect() as conn:
        return (await conn.execute(_ACTIVE)).one()


async def generation(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(_GENERATION)).scalar_one())


async def activate_profile(engine: AsyncEngine, *, seniority: str = "senior") -> tuple[int, int]:
    """A confirmed synthetic profile and its tuple, made active (module docstring)."""
    profile = ConfirmedProfile.model_validate({**SYNTHETIC_PROFILE, "seniority": seniority})
    profile_json = profile.model_dump_json()
    digest = hashlib.sha256(profile_json.encode("utf-8")).digest()
    async with engine.begin() as conn:
        await conn.execute(_LOCK_ACTIVE)  # the first lock, as the confirm takes it (§9.1)
        cv = (await conn.execute(_CV_FILE, {"h": digest})).scalar_one()
        draft = (await conn.execute(_DRAFT, {"cv": cv, "h": digest, "profile": profile_json})).scalar_one()
        version = int(
            (
                await conn.execute(_VERSION, {"d": draft, "cv": cv, "profile": profile_json, "h": digest})
            ).scalar_one()
        )
        await transition(
            conn,
            "profile_drafts",
            draft,
            ["ready"],
            "confirmed",
            set_={
                "confirmed_profile_version_id": version,
                "confirm_response": {"profile_version_id": version},
            },
        )
        model_policy_id = int((await conn.execute(_ACTIVE)).one().model_policy_id)
        tuple_id = await ensure_tuple(conn, profile_version_id=version, model_policy_id=model_policy_id)
        await conn.execute(_ACTIVATE, {"pv": version, "t": tuple_id})
    return version, tuple_id


# --- Task 18: the scope apply -------------------------------------------------------------------------------

SEEDED_JOBS: Final = 1200
# §13.4 step 3's completeness check as a count: the unmerged jobs without a `hard_filter_results` row for the
# scope `:s` whose `inputs_hash` is the job's `det_facts_hash` and whose algorithm version is `:v`.
INCOMPLETE_SQL: Final = (
    "SELECT count(*) FROM jobs j WHERE j.merged_into_job_id IS NULL AND NOT EXISTS (SELECT 1 FROM "
    "hard_filter_results h WHERE h.job_id = j.id AND h.scope_version_id = :s "
    "AND h.inputs_hash = j.det_facts_hash AND h.det_algorithm_version = :v)"
)
_INCOMPLETE = sa.text(INCOMPLETE_SQL)
_SCOPE_OF_VERSION = sa.text("SELECT scope_version_id FROM settings_versions WHERE id = :v")
_OPERATION_END = sa.text("SELECT status, progress FROM operations WHERE id = :i")
_RUN_OPERATION_JOB: Final = (
    "FROM procrastinate_jobs WHERE task_name = :task "
    "AND args @> jsonb_build_object('operation_id', CAST(:i AS bigint))"
)
_DELETE_JOB = sa.text("DELETE " + _RUN_OPERATION_JOB)
_LIVE_JOB = sa.text("SELECT count(*) " + _RUN_OPERATION_JOB + " AND status IN ('todo', 'doing')")
_ACTIVE_VERSION_FOR_UPDATE = sa.text(
    "SELECT v.id, v.payload FROM active_config a JOIN settings_versions v ON v.id = a.settings_version_id "
    "WHERE a.id = 1 FOR UPDATE OF a"
)


async def active_scope_id(engine: AsyncEngine) -> int:
    return int((await active_row(engine)).scope_version_id)


async def pending_scope_id(engine: AsyncEngine) -> int | None:
    pending = (await active_row(engine)).pending_scope_version_id
    return None if pending is None else int(pending)


async def scope_ids_of(engine: AsyncEngine, *saved: dict[str, Any]) -> list[int]:
    """The scope version of each saved settings version (`saved`: the 201 bodies of the saves), in order."""
    async with engine.connect() as conn:
        return [
            int((await conn.execute(_SCOPE_OF_VERSION, {"v": body["version"]["id"]})).scalar_one())
            for body in saved
        ]


async def operation_end(engine: AsyncEngine, op_id: int) -> tuple[str, Any]:
    """The operation's `(status, progress)`."""
    async with engine.connect() as conn:
        row = (await conn.execute(_OPERATION_END, {"i": op_id})).one()
    return str(row.status), row.progress


async def incomplete_jobs_for(engine: AsyncEngine, scope_id: int) -> int:
    """How many unmerged jobs lack a current hard-filter result for the scope (module docstring)."""
    async with engine.connect() as conn:
        return int(
            (await conn.execute(_INCOMPLETE, {"s": scope_id, "v": DET_ALGORITHM_VERSION})).scalar_one()
        )


async def delete_procrastinate_job_of(engine: AsyncEngine, op_id: int) -> None:
    """Remove the operation's `run_operation` job (exactly one): a save whose job never reached the queue."""
    async with engine.begin() as conn:
        deleted = await conn.execute(_DELETE_JOB, {"task": job_task_name("run_operation"), "i": op_id})
    assert deleted.rowcount == 1, deleted.rowcount


async def procrastinate_job_exists_for(engine: AsyncEngine, op_id: int) -> bool:
    """Whether a `run_operation` job of the operation waits (`todo`) or runs (`doing`)."""
    async with engine.connect() as conn:
        count = (
            await conn.execute(_LIVE_JOB, {"task": job_task_name("run_operation"), "i": op_id})
        ).scalar_one()
    return int(count) > 0


def _on_own_engine[T](server: PgServer, work: Callable[[AsyncEngine], Awaitable[T]]) -> T:
    """Run `work` on an engine of its own against `server` (the app role), for a sync test."""

    async def run() -> T:
        engine = make_null_engine(server.dsn("app", driver="sqlalchemy"))
        try:
            return await work(engine)
        finally:
            await engine.dispose()

    return asyncio.run(run())


def save_scope_sync(server: PgServer, *, radius_km: int) -> tuple[int, int]:
    """The active payload with one Berlin location at `radius_km`, saved through `save_settings` in its own
    transaction (the route's lock first), its job deferred; `(scope_version_id, apply_scope_operation_id)`."""
    make_queue_app(config_for(server, "worker").dsn("psycopg"))  # the task app `defer_in_tx` defers on

    async def save_it(engine: AsyncEngine) -> tuple[int, int]:
        now = utcnow()
        async with engine.begin() as conn:
            active = (await conn.execute(_ACTIVE_VERSION_FOR_UPDATE)).one()
            payload = copy.deepcopy(active.payload)
            payload["scope"]["locations"] = [berlin(radius_km)]
            preview = await dry_run(conn, payload, active=Settings.model_validate(active.payload), now=now)
            request = SaveRequest(
                payload=payload,
                base_version_id=int(active.id),
                acknowledged_warnings=frozenset(w.code for w in preview.warnings),
            )
            outcome = await save_settings(conn, request, now=now, defer=defer_in_tx)
            scope_id = (await conn.execute(_SCOPE_OF_VERSION, {"v": outcome.version_id})).scalar_one()
        assert outcome.apply_scope_operation_id is not None
        return int(scope_id), outcome.apply_scope_operation_id

    return _on_own_engine(server, save_it)


def _scalar_sync(server: PgServer, statement: str, *params: object) -> Any:
    row = pg_one(server, "app", statement, *params)
    assert row is not None
    return row[0]


def active_scope_sync(server: PgServer) -> int:
    return int(_scalar_sync(server, "SELECT scope_version_id FROM active_config WHERE id = 1"))


def pending_scope_sync(server: PgServer) -> int | None:
    pending = _scalar_sync(server, "SELECT pending_scope_version_id FROM active_config WHERE id = 1")
    return None if pending is None else int(pending)


def incomplete_jobs_sync(server: PgServer, scope_id: int) -> int:
    """`incomplete_jobs_for` on an engine of its own (the one statement of the check)."""
    return _on_own_engine(server, lambda engine: incomplete_jobs_for(engine, scope_id))


def operation_status_sync(server: PgServer, op_id: int) -> str:
    return str(_scalar_sync(server, "SELECT status FROM operations WHERE id = %s", op_id))


@pytest.fixture
def seeded_jobs_sync(migrated: PgServer) -> list[int]:
    """`SEEDED_JOBS` jobs with the active scope's results, committed (module docstring); their ids."""
    return _on_own_engine(migrated, lambda engine: insert_jobs(engine, SEEDED_JOBS))
