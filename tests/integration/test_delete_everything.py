"""Delete everything (spec §20.4, §19.5, §20.1, §18.2 "deletion graph"; plan M5 Task 16, M5-U3, P4, P19, D14,
Deviations 2, 13, 15, 16, 18, 24, 50, 51, 52, 53 and 56): `privacy.kinds.delete_everything`.

A0 (`request_delete_everything`) runs in the test's transaction, beside the request's idempotency row (the
route's, Task 18); each `purge` operation is run as the worker runs it (`tests.support.purges`' `Purger`),
with a Message Batches client on M3's fake Batches API and, where a test pings, the dead-man pinger on Task
5's fake healthchecks. The waits between a cancel's tries are recorded, never slept (`CANCEL_SLEEP`). The
seed-backed tests run on the two-year-old seed (`tests.support.privacy_seed`): every table of `public` holds a
row, and the purge deletes everything it does not keep against the real schema with every foreign key, CHECK
and trigger enabled. A crash is `tests.support.faults.raising_at` in place of a module's `afault_point`; a
state between two steps is observed at a `PausePoint`. Every datum is invented.
"""

import asyncio
import dataclasses
import hashlib
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final, get_args

import httpx
import procrastinate
import pytest
import respx
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db.migrate import run_migrate_async
from jobhunter.db.seed import anchor_utc_for, latest_slot_at_or_before, load_settings_v1
from jobhunter.errors import FencedOut, Refusal
from jobhunter.llm.batches import BatchApiError, BatchesClient, BatchStatus, RequestCounts
from jobhunter.llm.errors import LLMNotConfigured
from jobhunter.llm.fake import BatchScript, CancelOutcome, StoredBatch
from jobhunter.observability import deadman, ntfy
from jobhunter.observability.alerts import acknowledge_alert, raise_alert, resolve_alert
from jobhunter.observability.deadman import DeadmanPinger, HealthchecksClient
from jobhunter.observability.ntfy import AlertDispatcher, NtfyClient
from jobhunter.privacy import (
    fences,
    purge,
    reconcilers,  # noqa: F401 (registers `purge_fences`)
)
from jobhunter.privacy.export import request_export
from jobhunter.privacy.kinds import delete_everything
from jobhunter.privacy.kinds.delete_everything import DELETE_EVERYTHING_KEEPS, DeleteEverything
from jobhunter.privacy.kinds.golden_case import request_golden_case_delete
from jobhunter.privacy.manifests import ManifestRow, check_postconditions, manifest_file_name
from jobhunter.privacy.purge import KINDS, OpenedPurge, PurgeRun
from jobhunter.quota.ledger import lock_run_owner
from jobhunter.scoring import batch_results, cycles
from jobhunter.settings.model import Settings
from jobhunter.worker.operations import claim_operation, create_operation
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.collect import queued_run
from tests.support.faults import PausePoint, no_fault, raising_at
from tests.support.fixtures import ConfigFactory
from tests.support.healthchecks import API_KEY, API_URL, PING_URL, fake_healthchecks
from tests.support.llm import FakeLLM
from tests.support.logs import logged
from tests.support.m3 import insert_job
from tests.support.privacy_seed import InFlight, SeedIds, seed_two_year_old_graph
from tests.support.purges import Purger, completed, manifest_of
from tests.support.reconcile import run_reconciler
from tests.support.scoring import activate_score_tuple

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("current_epoch")]

TOPIC: Final = "https://ntfy.test/jh-alerts-4f2a9c"
ORIGIN: Final = "https://jobs.example.test"
SEEDED_BATCH: Final = "msgbatch_seed_submitted"  # the seed's live submission's provider id when `submitted`
_IDEMPOTENCY_ROW: Final = sa.text(
    "INSERT INTO api_idempotency (route, idem_key, request_sha256, status, created_at, expires_at) "
    "VALUES ('POST /api/privacy/delete-everything', :key, '\\x01', 'in_progress', now(), "
    "now() + interval '1 day') RETURNING id"
)
_TABLES: Final = sa.text(
    "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') ORDER BY c.relname"
)
_LEDGERS: Final = sa.text(
    "SELECT (SELECT count(*) FROM llm_spend_ledger) AS spend, (SELECT count(*) FROM quota_ledger) AS quota, "
    "(SELECT count(*) FROM llm_budget) AS months, (SELECT count(*) FROM quota_windows) AS windows, "
    "(SELECT sum(reserved_usd) FROM llm_spend_ledger) AS reserved"
)
# What the month's counters count, as its entries count it (the seed's rule): reserved, dispatched and
# ambiguous at their reservation, billed at its cost, not billed nothing.
_COUNTERS: Final = sa.text(
    "SELECT b.counted_usd, (SELECT coalesce(sum(CASE WHEN l.state IN ('billed', 'not_billed') "
    "THEN l.settled_usd ELSE l.reserved_usd END), 0) FROM llm_spend_ledger l WHERE l.budget_month = b.month "
    "AND l.kind = 'request') AS entries FROM llm_budget b ORDER BY b.month"
)
_UNSETTLED: Final = sa.text(
    "SELECT count(*) FROM llm_spend_ledger WHERE kind = 'request' AND state IN ('reserved', 'dispatched')"
)
_USER: Final = sa.text(
    "SELECT username, password_hash, totp_secret_enc, totp_enabled, totp_last_step FROM users WHERE id = 1"
)
_TOTP: Final = sa.text(
    "UPDATE users SET totp_secret_enc = '\\x0123', totp_enabled = true, totp_last_step = 42 WHERE id = 1"
)
_SUBMISSION: Final = sa.text(
    "SELECT state, released_at, cancel_requested_at FROM batch_submissions WHERE id = :s"
)
_ITEM_ENTRIES: Final = sa.text(
    "SELECT l.id, l.state, l.evidence FROM batch_items i JOIN llm_calls c ON c.id = i.llm_call_id "
    "JOIN llm_spend_ledger l ON l.id = c.spend_ledger_id WHERE i.submission_id = :s ORDER BY l.id"
)
_ENTRIES: Final = sa.text(
    "SELECT id, state, evidence FROM llm_spend_ledger WHERE id = ANY(CAST(:ids AS bigint[])) ORDER BY id"
)
_CALLS_OF: Final = sa.text(
    "SELECT count(*) FROM llm_calls WHERE spend_ledger_id = ANY(CAST(:ids AS bigint[]))"
)
_ALERT: Final = sa.text("SELECT resolved_at, acknowledged_at, last_notified_at FROM alerts WHERE id = :a")
_EXISTS: Final = "SELECT EXISTS (SELECT 1 FROM {table} WHERE id = :i)"
_ACTIVE: Final = sa.text(
    "SELECT settings_version_id, scope_version_id, pending_scope_version_id, profile_version_id, "
    "llm_tuple_id FROM active_config WHERE id = 1"
)
_SCHEDULE: Final = sa.text(
    "SELECT anchor_utc, interval_s, enabled, last_evaluated_slot, settings_version_id, updated_at "
    "FROM schedule_state"
)
_VERSION: Final = sa.text("SELECT payload, version_no, effect_classes FROM settings_versions WHERE id = :v")
_PROPOSAL: Final = sa.text("SELECT status FROM query_proposals WHERE id = :p")
_OPERATION: Final = sa.text("SELECT status, claim_id, last_error_code FROM operations WHERE id = :o")
_LOCKOUTS: Final = sa.text("SELECT scope, generation FROM login_lockouts ORDER BY scope")
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
_SESSIONS: Final = sa.text("SELECT token_hmac FROM sessions ORDER BY token_hmac")
_JOB: Final = sa.text(
    "INSERT INTO procrastinate_jobs (queue_name, task_name, args, status) "
    "VALUES ('aux', 'delete_everything_test', '{}', CAST(:status AS procrastinate_job_status)) RETURNING id"
)
_QUEUE_STATES: Final = ("todo", "doing", "succeeded")  # one job in each, in id order
_FINISH_JOB: Final = sa.text("UPDATE procrastinate_jobs SET status = 'succeeded' WHERE id = :j")
_JOB_EVENTS: Final = sa.text("SELECT count(*) FROM procrastinate_events WHERE job_id = :j")
_PURGE_FENCES_LEFT: Final = sa.text(
    "SELECT (SELECT count(*) FROM purge_fences WHERE released_at IS NULL) AS fences, "
    "(SELECT count(*) FROM deletion_manifests WHERE status = 'executing') AS executing"
)
_PURGE_OPERATIONS: Final = sa.text(
    "SELECT id, status FROM operations WHERE kind = 'purge' AND payload -> 'manifest_id' = to_jsonb(:m) "
    "ORDER BY id"
)
_EXPORT: Final = sa.text("SELECT status, storage_path FROM exports WHERE id = :e")
_EXPORT_OPERATION: Final = sa.text(
    "SELECT id FROM operations WHERE kind = 'export' "
    "AND payload -> 'export_id' = to_jsonb(CAST(:e AS bigint))"
)
# The manifests the database records with their content (`migrate` rewrites a missing file from the scope).
_RECORDED_MANIFESTS: Final = sa.text(
    "SELECT manifest_uuid, kind, file_sha256 FROM deletion_manifests "
    "WHERE file_sha256 IS NOT NULL ORDER BY kind"
)
# The settings versions, `active_config`'s pointers and `schedule_state`, as one comparable listing.
_CONFIGURATION: Final = sa.text(
    "SELECT 'version' AS kind, id, version_no, NULL::timestamptz AS at FROM settings_versions "
    "UNION ALL SELECT 'active', settings_version_id, scope_version_id, updated_at FROM active_config "
    "UNION ALL SELECT 'schedule', settings_version_id, interval_s, last_evaluated_slot FROM schedule_state "
    "ORDER BY 1, 2"
)
_A0_WRITES: Final = sa.text(
    "SELECT (SELECT count(*) FROM deletion_manifests) AS manifests, "
    "(SELECT count(*) FROM purge_fences WHERE kind = 'global') AS fences, "
    "(SELECT count(*) FROM operations WHERE kind = 'purge') AS operations"
)
_QUERY: Final = sa.text(
    "INSERT INTO queries (text, lang, origin, created_at) VALUES ('nach dem bound', 'de', 'user', now()) "
    "RETURNING id"
)
_LOGIN_ATTEMPT: Final = sa.text(
    "INSERT INTO login_attempts (at, ip, username_hmac, success) "
    "VALUES (now(), CAST('198.51.100.30' AS inet), '\\x03', false) RETURNING id"
)


# ---- the harness -------------------------------------------------------------------------------------------


@dataclasses.dataclass
class Sleeps:
    """`CANCEL_SLEEP`'s stand-in: it records each wait and never waits."""

    waited: list[float] = dataclasses.field(default_factory=list)

    async def __call__(self, seconds: float) -> None:
        self.waited.append(seconds)


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> Sleeps:
    recorded = Sleeps()
    monkeypatch.setattr(delete_everything, "CANCEL_SLEEP", recorded)
    return recorded


@pytest.fixture
async def batches_client(fake_llm: FakeLLM) -> AsyncIterator[BatchesClient]:
    """A Message Batches client on M3's fake Batches API."""
    async with fake_llm.client() as http:
        yield BatchesClient(fake_llm.cfg, http_client=http)


@pytest.fixture
def live(purger: Purger, batches_client: BatchesClient, sleeps: Sleeps) -> Purger:
    """The purger with the worker's Batches client on the fake (no dead-man pinger)."""
    return Purger(purger.engine, purger.cfg, dataclasses.replace(purger.deps, batches_client=batches_client))


@dataclasses.dataclass
class Cancels:
    """The provider cancels the fake was sent, each with whether the submission's row still existed then."""

    sent: list[bool] = dataclasses.field(default_factory=list)


@pytest.fixture
def cancels(monkeypatch: pytest.MonkeyPatch, engine: AsyncEngine) -> Cancels:
    recorded = Cancels()
    real = BatchesClient.cancel

    async def counting(self: BatchesClient, batch_id: str) -> BatchStatus:
        async with engine.connect() as conn:
            row = await conn.execute(
                sa.text("SELECT EXISTS (SELECT 1 FROM batch_submissions WHERE provider_batch_id = :b)"),
                {"b": batch_id},
            )
            recorded.sent.append(bool(row.scalar_one()))
        return await real(self, batch_id)

    monkeypatch.setattr(BatchesClient, "cancel", counting)
    return recorded


def in_progress_batch(fake_llm: FakeLLM, *, cancel: CancelOutcome = "ok") -> StoredBatch:
    """The seed's live batch on the fake (`SEEDED_BATCH`), in progress until a cancel is accepted."""
    stored = fake_llm.batches(BatchScript(polls_until_ended=1000, cancel=cancel))
    batch = StoredBatch(
        id=SEEDED_BATCH,
        seq=1,
        created_at=fake_llm.state.clock(),
        purpose="generate",
        requests={"t1_a1": {}},
        keys={"t1_a1": "seed-key"},
    )
    stored[SEEDED_BATCH] = batch
    return batch


async def seeded(engine: AsyncEngine, **kwargs: Any) -> SeedIds:
    async with engine.begin() as conn:
        return await seed_two_year_old_graph(conn, now=utcnow(), **kwargs)


@dataclasses.dataclass(frozen=True)
class Requested:
    """A0's purge and the request's idempotency row."""

    opened: OpenedPurge
    idempotency_row_id: int


_KEYS = iter(range(1, 10**6))


async def request(engine: AsyncEngine, *, now: datetime | None = None) -> Requested:
    """A0 as Task 18's route runs it: the request's idempotency row, then `request_delete_everything` with
    its id, in one transaction (no deferrer: the purger runs the operation)."""
    async with engine.begin() as conn:
        row_id = int(
            (
                await conn.execute(_IDEMPOTENCY_ROW, {"key": f"delete-everything-{next(_KEYS):06d}"})
            ).scalar_one()
        )
        opened = await delete_everything.request_delete_everything(
            conn, now=utcnow() if now is None else now, idempotency_row_id=row_id, defer=None
        )
    return Requested(opened, row_id)


async def finished(purger: Purger, requested: Requested) -> ManifestRow:
    """The purge run (again) to its completion; its manifest, asserted completed with every typed
    postcondition holding (`tests.support.purges.completed`) and the kind's own."""
    await purger.again(requested.opened.operation_id)
    manifest = await completed(purger, requested.opened.manifest_id, requested.opened.operation_id)
    assert manifest.scope is not None
    async with purger.engine.connect() as conn:
        assert await DeleteEverything().postconditions(conn, manifest.scope) == []
    return manifest


async def recorded(purger: Purger, requested: Requested, monkeypatch: pytest.MonkeyPatch) -> ManifestRow:
    """The purge's content recorded (A3) and nothing deleted: a crash right after it; the manifest."""
    monkeypatch.setattr(purge, "afault_point", raising_at(purge.AFTER_RECORD))
    await purger.run(requested.opened.operation_id)
    monkeypatch.setattr(purge, "afault_point", no_fault)
    manifest = await manifest_of(purger.engine, requested.opened.manifest_id)
    assert manifest.status == "executing" and manifest.scope is not None
    return manifest


async def paused_at(purger: Purger, requested: Requested, point: PausePoint) -> "asyncio.Task[None]":
    """The purge run (again: made due) in a task until it reaches `point` (patched in by the caller); the
    task."""
    task = asyncio.create_task(purger.again(requested.opened.operation_id))
    await asyncio.wait_for(point.reached.wait(), 60)
    return task


async def scalar(engine: AsyncEngine, statement: sa.TextClause, **params: object) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(statement, params)).scalar_one()


async def one(engine: AsyncEngine, statement: sa.TextClause, **params: object) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(statement, params)).one()


def files_in(directory: Path) -> dict[str, bytes]:
    """Each file of `directory` with its bytes."""
    return {path.name: path.read_bytes() for path in sorted(directory.iterdir())}


async def rows(engine: AsyncEngine, statement: sa.TextClause, **params: object) -> list[Any]:
    async with engine.connect() as conn:
        return list((await conn.execute(statement, params)).all())


async def exists(engine: AsyncEngine, table: str, row_id: int) -> bool:
    return bool(await scalar(engine, sa.text(_EXISTS.format(table=table)), i=row_id))


def replay_run(purger: Purger, manifest: ManifestRow) -> PurgeRun:
    """A replay's run of the manifest (Task 25's, in `migrate`): no effects, no epoch, no owner."""
    return PurgeRun(
        engine=purger.engine,
        cfg=purger.cfg,
        now=utcnow(),
        cv_store=purger.deps.cv_store,
        manifest=manifest,
        mode="replay",
        effects=None,
        owner=None,
    )


def operations_of(manifest: ManifestRow, table: str) -> list[dict[str, Any]]:
    assert manifest.scope is not None
    return [dict(op) for op in manifest.scope["operations"] if op.get("table") == table]


# ---- A0 ----------------------------------------------------------------------------------------------------


async def test_a0_commits_the_manifest_its_global_fence_and_operation_and_refuses_a_second(
    engine: AsyncEngine,
) -> None:
    """A0: the `executing` manifest with the request `{"idempotency_row_id": <id>}`, a `global` fence and
    the `purge` operation; while it is executing, another request is 409 `conflict_state`
    (`purge_in_progress`) with nothing written."""
    now = utcnow()
    first = await request(engine, now=now)
    manifest = await manifest_of(engine, first.opened.manifest_id)
    assert (manifest.kind, manifest.status, manifest.created_at) == ("delete_everything", "executing", now)
    assert dict(manifest.request) == {"idempotency_row_id": first.idempotency_row_id}
    fences = await rows(
        engine, sa.text("SELECT kind, released_at FROM purge_fences WHERE manifest_id = :m"), m=manifest.id
    )
    assert [tuple(f) for f in fences] == [("global", None)]
    with pytest.raises(Refusal) as refused:
        await request(engine)
    assert (refused.value.status, refused.value.code, refused.value.detail) == (
        409,
        "conflict_state",
        "purge_in_progress",
    )
    written = await one(engine, _A0_WRITES)
    assert tuple(written) == (1, 1, 1)  # one manifest, one `global` fence, one purge operation
    # the second request's idempotency row rolled back with its transaction
    assert await rows(engine, sa.text("SELECT id FROM api_idempotency")) == [(first.idempotency_row_id,)]


async def test_a0_refuses_an_idempotency_row_that_does_not_exist(engine: AsyncEngine) -> None:
    """The request names its idempotency row by an id that must name a row (the route's own, in A0's
    transaction): an id naming none is a `ValueError`, and nothing is written."""
    async with engine.begin() as conn:
        with pytest.raises(ValueError, match="idempotency row"):
            await delete_everything.request_delete_everything(
                conn, now=utcnow(), idempotency_row_id=987_654_321, defer=None
            )
    assert tuple(await one(engine, _A0_WRITES)) == (0, 0, 0)


# ---- the whole graph ---------------------------------------------------------------------------------------


async def test_it_runs_to_completion_without_fk_or_check_violation_and_keeps_the_ledgers(
    engine: AsyncEngine, live: Purger
) -> None:
    """§18.2 "deletion graph": on the seed (every table holds a row) the purge completes with every foreign
    key, CHECK and trigger enabled and every postcondition holding. The ledgers keep every row, the budget
    month's counters still equal what its entries count, and no entry is left `reserved` or `dispatched`."""
    await seeded(engine)
    before = await one(engine, _LEDGERS)
    requested = await request(engine)
    manifest = await finished(live, requested)
    after = await one(engine, _LEDGERS)
    assert tuple(after) == tuple(before)
    for month in await rows(engine, _COUNTERS):
        assert month.counted_usd == month.entries
    assert await scalar(engine, _UNSETTLED) == 0
    assert live.manifest_file(manifest.manifest_uuid).exists()


async def test_every_table_outside_the_keep_list_is_empty(
    engine: AsyncEngine, live: Purger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every table of `public` (from the live catalog: a table a later task adds is caught) either is in
    `DELETE_EVERYTHING_KEEPS` or is empty after the purge, and the kept tables that a step deletes from hold
    only their kept rows: this purge's operation, this request's idempotency row, the default source
    instances, the new default settings version, the scope versions of `retained` and those above the bound,
    the alerts open at A2, content-free exports, and the procrastinate jobs not listed at A2 (`todo` or
    `doing` then) or above the bound."""
    seed = await seeded(engine)
    async with engine.begin() as conn:
        queue = [int((await conn.execute(_JOB, {"status": status})).scalar_one()) for status in _QUEUE_STATES]
    requested = await request(engine)
    await recorded(live, requested, monkeypatch)
    async with engine.begin() as conn:
        later_job = int((await conn.execute(_JOB, {"status": "todo"})).scalar_one())
    manifest = await finished(live, requested)
    assert manifest.scope is not None
    async with engine.connect() as conn:
        tables = [str(name) for name in (await conn.execute(_TABLES)).scalars()]
        assert DELETE_EVERYTHING_KEEPS <= set(tables)
        holding = [
            table
            for table in tables
            if table not in DELETE_EVERYTHING_KEEPS
            and (await conn.execute(sa.text(f'SELECT EXISTS (SELECT 1 FROM "{table}")'))).scalar_one()  # noqa: S608
        ]

        async def ids(statement: str) -> list[int]:
            return [int(i) for i in (await conn.execute(sa.text(statement))).scalars()]

        assert await ids("SELECT id FROM operations") == [requested.opened.operation_id]
        assert await ids("SELECT id FROM api_idempotency") == [requested.idempotency_row_id]
        instances = await conn.execute(sa.text("SELECT DISTINCT instance_key FROM source_instances"))
        assert list(instances.scalars()) == ["default"]
        active = (await conn.execute(_ACTIVE)).one()
        assert await ids("SELECT id FROM settings_versions") == [active.settings_version_id]
        alerts = await ids("SELECT id FROM alerts ORDER BY id")
        scopes = await ids("SELECT id FROM scope_versions ORDER BY id")
        jobs = await ids("SELECT id FROM procrastinate_jobs ORDER BY id")
        exports = await conn.execute(sa.text("SELECT DISTINCT status FROM exports"))
        assert set(exports.scalars()) <= {"purged", "expired", "failed"}
        content_free = await conn.execute(
            sa.text("SELECT count(*) FROM exports WHERE storage_path IS NOT NULL")
        )
        assert content_free.scalar_one() == 0
    assert holding == []
    bound = manifest.scope["max_ids"]
    retained_scopes = manifest.scope["retained"]["scope_versions"]
    assert [i for i in scopes if i <= bound["scope_versions"]] == retained_scopes and retained_scopes
    listed_jobs = [i for op in operations_of(manifest, "procrastinate_jobs") for i in op["ids"]]
    todo, doing, ended = queue
    assert listed_jobs == [ended] and jobs == [todo, doing, later_job]
    assert later_job > bound["procrastinate_jobs"]
    resolved_at_a2 = [i for op in operations_of(manifest, "alerts") for i in op["ids"]]
    assert seed.resolved_alert_id in resolved_at_a2 and not set(resolved_at_a2) & set(alerts)
    assert {seed.open_alert_id, seed.bound_violation_alert_id} <= set(alerts)


# ---- step 2: live work -------------------------------------------------------------------------------------


@pytest.mark.parametrize("live_submission", get_args(InFlight))
async def test_the_batch_mapping_is_exact(
    engine: AsyncEngine, live: Purger, monkeypatch: pytest.MonkeyPatch, live_submission: InFlight
) -> None:
    """Step 2 on a database seeded with its one live submission in each in-flight state: `preparing` →
    `abandoned`, `submit_started` and `submit_ambiguous` → `released`, `submitted` and `ended` → the cancel
    recorded (the state kept); every entry of its items `not_billed('never_dispatched')` (abandoned) or
    `ambiguous`, none left `reserved` or `dispatched`; the entries keep those states after completion, the
    submission deleted."""
    seed = await seeded(engine, live_submission=live_submission)
    requested = await request(engine)
    point = PausePoint(delete_everything.AFTER_CANCEL_RECORDED)
    monkeypatch.setattr(delete_everything, "afault_point", point)
    task = await paused_at(live, requested, point)
    submission = await one(engine, _SUBMISSION, s=seed.live_submission_id)
    entries = await rows(engine, _ITEM_ENTRIES, s=seed.live_submission_id)
    point.resume()
    await task
    expected = {
        "preparing": ("abandoned", False, False, ("not_billed", "never_dispatched")),
        "submit_started": ("released", True, False, ("ambiguous", None)),
        "submit_ambiguous": ("released", True, False, ("ambiguous", None)),
        "submitted": ("submitted", False, True, ("ambiguous", None)),
        "ended": ("ended", False, True, ("ambiguous", None)),
    }[live_submission]
    assert (
        submission.state,
        submission.released_at is not None,
        submission.cancel_requested_at is not None,
    ) == (expected[:3])
    assert len(entries) == 2 and {(e.state, e.evidence) for e in entries} == {expected[3]}
    await completed(live, requested.opened.manifest_id, requested.opened.operation_id)
    assert not await exists(engine, "batch_submissions", seed.live_submission_id)
    after = await rows(engine, _ENTRIES, ids=[int(e.id) for e in entries])
    assert {(e.state, e.evidence) for e in after} == {expected[3]}
    assert await scalar(engine, _UNSETTLED) == 0


async def test_online_calls_in_flight_are_settled_before_their_calls_go(
    engine: AsyncEngine, live: Purger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G04: a scoring call `reserved`, a scoring call and an eval call `dispatched`: step 2 settles them
    (released `not_billed('never_dispatched')`, `ambiguous`, `ambiguous`) while their calls still exist;
    the calls go later, the entries stay, and no entry is left `reserved` or `dispatched`."""
    seed = await seeded(engine)
    ids = [seed.reserved_entry_id, seed.dispatched_entry_id, seed.eval_entry_id]
    before = await rows(engine, _ENTRIES, ids=ids)
    assert [(e.id, e.state) for e in before] == sorted(
        zip(ids, ["reserved", "dispatched", "dispatched"], strict=True)
    )
    requested = await request(engine)
    point = PausePoint(delete_everything.AFTER_CANCEL_RECORDED)
    monkeypatch.setattr(delete_everything, "afault_point", point)
    task = await paused_at(live, requested, point)
    settled = {int(e.id): (e.state, e.evidence) for e in await rows(engine, _ENTRIES, ids=ids)}
    calls_then = await scalar(engine, _CALLS_OF, ids=ids)
    point.resume()
    await task
    expected = {
        seed.reserved_entry_id: ("not_billed", "never_dispatched"),
        seed.dispatched_entry_id: ("ambiguous", None),
        seed.eval_entry_id: ("ambiguous", None),
    }
    assert settled == expected and calls_then == 3
    await completed(live, requested.opened.manifest_id, requested.opened.operation_id)
    assert {int(e.id): (e.state, e.evidence) for e in await rows(engine, _ENTRIES, ids=ids)} == expected
    assert await scalar(engine, _CALLS_OF, ids=ids) == 0
    assert await scalar(engine, _UNSETTLED) == 0


async def test_a_running_proposal_of_a_cancelled_operation_fails(
    engine: AsyncEngine, live: Purger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The M2 final review: a `running` proposal whose `propose_queries` operation step 2 cancels ends
    `failed` in the same transaction (it no longer stays `running`); the operation `cancelled` without a
    claim."""
    seed = await seeded(engine)
    requested = await request(engine)
    point = PausePoint(delete_everything.AFTER_CANCEL_RECORDED)
    monkeypatch.setattr(delete_everything, "afault_point", point)
    task = await paused_at(live, requested, point)
    proposal = await scalar(engine, _PROPOSAL, p=seed.running_proposal_id)
    operation = await one(engine, _OPERATION, o=seed.running_operation_id)
    point.resume()
    await task
    assert proposal == "failed"
    assert (operation.status, operation.claim_id, operation.last_error_code) == (
        "cancelled",
        None,
        "delete_everything",
    )
    await completed(live, requested.opened.manifest_id, requested.opened.operation_id)


async def test_nothing_new_starts_while_it_runs(engine: AsyncEngine, queue_app: procrastinate.App) -> None:
    """Once A0 has committed its `global` fence (Task 8's admission stop): a run's owner write is refused
    (`FencedOut("purge_fence")`), no score cycle starts (`global_fence`), and an operation is not claimed
    (left `pending`, no attempt spent)."""
    async with engine.begin() as conn:
        await activate_score_tuple(conn)
        operation_id, _ = await create_operation(
            conn, kind="apply_scope", payload={}, dedup_key="apply_scope:fenced", defer=None
        )
    owner = await queued_run(engine)
    await request(engine)
    async with engine.begin() as conn:
        with pytest.raises(FencedOut) as fenced:
            await lock_run_owner(conn, owner)
    assert fenced.value.owner == "purge_fence"
    async with engine.begin() as conn:
        assert await cycles.pause_reason(conn, fake_llm=True, now=utcnow()) == "global_fence"
        assert await cycles.start_cycle(conn, trigger="manual", now=utcnow(), epoch=1, fake_llm=True) is None
    async with engine.begin() as conn:
        assert await claim_operation(conn, operation_id, epoch=1) is None
    status = await one(
        engine, sa.text("SELECT status, attempts FROM operations WHERE id = :o"), o=operation_id
    )
    assert tuple(status) == ("pending", 0)


# ---- step 2: the provider cancels --------------------------------------------------------------------------


@pytest.mark.parametrize("cancel", ["ok", "error_500"])
async def test_recorded_cancels_are_sent_before_the_submissions_go(
    engine: AsyncEngine,
    live: Purger,
    fake_llm: FakeLLM,
    cancels: Cancels,
    caplog: pytest.LogCaptureFixture,
    cancel: CancelOutcome,
) -> None:
    """The poller withheld (no poll runs), the seed's live submission `submitted` and its batch in progress
    on the fake: the purge itself sends the recorded cancel, while the submission's row still exists, then
    deletes the row. With the fake rejecting all three cancels, `batch_cancel_gave_up` is logged (ids only)
    and the deletion goes on."""
    seed = await seeded(engine)
    batch = in_progress_batch(fake_llm, cancel=cancel)
    requested = await request(engine)
    await finished(live, requested)
    if cancel == "ok":
        assert cancels.sent == [True] and batch.cancel_requested
        assert logged(caplog, "batch_cancel_gave_up") == []
    else:
        assert cancels.sent == [True, True, True] and not batch.cancel_requested
        (gave_up,) = logged(caplog, "batch_cancel_gave_up")
        assert (gave_up["submission_id"], gave_up["provider_batch_id"]) == (
            seed.live_submission_id,
            SEEDED_BATCH,
        )
    assert not await exists(engine, "batch_submissions", seed.live_submission_id)


async def test_a_retrieval_failing_once_then_recovering_sends_one_cancel(
    fake_llm: FakeLLM, batches_client: BatchesClient, cancels: Cancels, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`send_purge_cancel`: the first retrieval fails (one failed try, then the first backoff), the second
    sees the batch `in_progress`, and exactly one cancel is sent and accepted."""
    batch = in_progress_batch(fake_llm)
    real = BatchesClient.retrieve
    failures = [BatchApiError("retrieve", status=503, error_class="InternalServerError")]

    async def flaky(self: BatchesClient, batch_id: str) -> BatchStatus:
        if failures:
            raise failures.pop()
        return await real(self, batch_id)

    monkeypatch.setattr(BatchesClient, "retrieve", flaky)
    sleeps = Sleeps()
    sent = await batch_results.send_purge_cancel(batches_client, SEEDED_BATCH, submission_id=7, sleep=sleeps)
    assert sent is True and len(cancels.sent) == 1 and batch.cancel_requested
    assert sleeps.waited == [batch_results.PURGE_CANCEL_BACKOFF_S[0]]


async def test_a_retrieval_failing_every_try_gives_up_and_the_deletion_goes_on(
    engine: AsyncEngine,
    live: Purger,
    fake_llm: FakeLLM,
    cancels: Cancels,
    sleeps: Sleeps,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three failed retrievals: no cancel sent, the backoff's two waits, `batch_cancel_gave_up` logged with
    the submission and provider ids, and the submission deleted all the same."""
    seed = await seeded(engine)
    in_progress_batch(fake_llm)

    async def failing(self: BatchesClient, batch_id: str) -> BatchStatus:
        raise BatchApiError("retrieve", status=503, error_class="InternalServerError")

    monkeypatch.setattr(BatchesClient, "retrieve", failing)
    requested = await request(engine)
    await finished(live, requested)
    assert cancels.sent == []
    assert sleeps.waited == list(batch_results.PURGE_CANCEL_BACKOFF_S)
    (gave_up,) = logged(caplog, "batch_cancel_gave_up")
    assert (gave_up["submission_id"], gave_up["provider_batch_id"]) == (seed.live_submission_id, SEEDED_BATCH)
    assert not await exists(engine, "batch_submissions", seed.live_submission_id)


async def test_a_client_not_configured_counts_as_a_failed_try(
    batches_client: BatchesClient,
    cancels: Cancels,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Batches client without its key (`LLMNotConfigured`, before any request) is a failed try like a
    failed retrieval: after the three tries and the backoff's two waits, `batch_cancel_gave_up` and
    `False`, nothing sent."""

    async def unconfigured(self: BatchesClient, batch_id: str) -> BatchStatus:
        raise LLMNotConfigured()

    monkeypatch.setattr(BatchesClient, "retrieve", unconfigured)
    sleeps = Sleeps()
    sent = await batch_results.send_purge_cancel(batches_client, SEEDED_BATCH, submission_id=7, sleep=sleeps)
    assert sent is False and cancels.sent == []
    assert sleeps.waited == list(batch_results.PURGE_CANCEL_BACKOFF_S)
    (gave_up,) = logged(caplog, "batch_cancel_gave_up")
    assert (gave_up["submission_id"], gave_up["provider_batch_id"]) == (7, SEEDED_BATCH)


@pytest.mark.parametrize("processing", ["canceling", "ended"])
async def test_a_canceling_or_ended_batch_is_success_without_a_send(
    fake_llm: FakeLLM,
    batches_client: BatchesClient,
    cancels: Cancels,
    monkeypatch: pytest.MonkeyPatch,
    processing: str,
) -> None:
    """A batch whose cancel was accepted already (`canceling`: the fake ends such a batch at its next
    observation, so its retrieval is a stand-in here) or that has ended (on the fake): success at the first
    retrieval, nothing sent."""
    in_progress_batch(fake_llm)
    if processing == "canceling":
        status = BatchStatus("canceling", RequestCounts(1, 0, 0, 0, 0), None, utcnow() + timedelta(days=1))

        async def canceling(self: BatchesClient, batch_id: str) -> BatchStatus:
            return status

        monkeypatch.setattr(BatchesClient, "retrieve", canceling)
    else:
        fake_llm.batches(BatchScript(polls_until_ended=0))  # the next observation ends it
    sleeps = Sleeps()
    sent = await batch_results.send_purge_cancel(batches_client, SEEDED_BATCH, submission_id=7, sleep=sleeps)
    assert sent is True and cancels.sent == [] and sleeps.waited == []


async def test_cancel_requested_still_returns_at_a_failed_retrieval(
    fake_llm: FakeLLM,
    batches_client: BatchesClient,
    cancels: Cancels,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The poller's helper is unchanged: a failed retrieval sends nothing and is not retried (the next poll
    retries), so `cancel_requested` returns `False` after one retrieval."""
    in_progress_batch(fake_llm)
    calls: list[str] = []

    async def failing(self: BatchesClient, batch_id: str) -> BatchStatus:
        calls.append(batch_id)
        raise BatchApiError("retrieve", status=503, error_class="InternalServerError")

    monkeypatch.setattr(BatchesClient, "retrieve", failing)
    caplog.set_level("INFO", logger="jobhunter.scoring.batch_results")
    assert await batch_results.cancel_requested(batches_client, SEEDED_BATCH, submission_id=7) is False
    assert calls == [SEEDED_BATCH] and cancels.sent == []
    assert len(logged(caplog, "batch_cancel_retrieve_failed")) == 1
    assert logged(caplog, "batch_cancel_gave_up") == []


async def test_a_crash_after_recording_the_cancel_resends_it_on_restart(
    engine: AsyncEngine, live: Purger, fake_llm: FakeLLM, cancels: Cancels, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash between step 2's commit (the cancel recorded) and the send: nothing was sent and the
    submission is still there; the restarted purge sends the cancel once (the submission still carries it),
    then deletes the row."""
    seed = await seeded(engine)
    batch = in_progress_batch(fake_llm)
    requested = await request(engine)
    monkeypatch.setattr(
        delete_everything, "afault_point", raising_at(delete_everything.AFTER_CANCEL_RECORDED)
    )
    await live.run(requested.opened.operation_id)
    submission = await one(engine, _SUBMISSION, s=seed.live_submission_id)
    assert submission.state == "submitted" and submission.cancel_requested_at is not None
    assert cancels.sent == [] and not batch.cancel_requested
    monkeypatch.setattr(delete_everything, "afault_point", no_fault)
    await finished(live, requested)
    assert cancels.sent == [True] and batch.cancel_requested
    assert not await exists(engine, "batch_submissions", seed.live_submission_id)


# ---- steps 3 to 12 -----------------------------------------------------------------------------------------


async def test_open_alerts_survive_and_are_still_pushed(
    engine: AsyncEngine, live: Purger, monkeypatch: pytest.MonkeyPatch, current_epoch: int
) -> None:
    """The alerts open at A2 are kept (an id list frozen there): an unpushed open warn alert, which the
    dispatcher pushes afterwards, and an unacknowledged `llm.reservation_bound_violated`, which keeps the
    score lane paused until it is acknowledged. An alert resolved before A2 is deleted; one resolved between
    A2 and phase B is kept."""
    seed = await seeded(engine)
    async with engine.begin() as conn:
        later = await raise_alert(
            conn, code="operation.failed", severity="warn", subject="operation:9", message="m"
        )
    requested = await request(engine)
    await recorded(live, requested, monkeypatch)
    async with engine.begin() as conn:
        assert await resolve_alert(conn, code="operation.failed", subject="operation:9")
    await finished(live, requested)
    assert not await exists(engine, "alerts", seed.resolved_alert_id)
    resolved_later = await one(engine, _ALERT, a=later)
    assert resolved_later.resolved_at is not None
    for alert in (seed.open_alert_id, seed.bound_violation_alert_id):
        row = await one(engine, _ALERT, a=alert)
        assert (row.resolved_at, row.acknowledged_at, row.last_notified_at) == (None, None, None)
    async with engine.connect() as conn:
        assert await cycles.pause_reason(conn, fake_llm=True, now=utcnow()) == "reservation_bound_violated"
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        topic = router.post(TOPIC).mock(return_value=httpx.Response(200))
        async with ntfy.make_http_client(user_agent="JobHunter/test") as http:
            client = NtfyClient(http, topic_url=TOPIC, token=None, public_origin=ORIGIN)
            dispatcher = AlertDispatcher(engine, client, epoch=current_epoch, clock=utcnow, sleep=Sleeps())
            report = await dispatcher.pass_once()
    assert report.sent >= 1 and topic.called
    assert (await one(engine, _ALERT, a=seed.open_alert_id)).last_notified_at is not None
    async with engine.begin() as conn:
        assert await acknowledge_alert(conn, seed.bound_violation_alert_id, now=utcnow()) is not None
    async with engine.connect() as conn:
        assert await cycles.pause_reason(conn, fake_llm=True, now=utcnow()) != "reservation_bound_violated"


async def test_a_job_doing_at_a2_is_kept_after_it_finishes(
    engine: AsyncEngine, live: Purger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The procrastinate jobs that had finished at A2 (a terminal status, Deviation 24) are an id list
    frozen there: a job `doing` at A2 is kept with its events, also once it has finished before phase B, and
    so is one still `todo` at A2 (it may be fetched before step 11); a finished one is deleted with its
    events."""
    async with engine.begin() as conn:
        todo, doing, finished_job = [
            int((await conn.execute(_JOB, {"status": status})).scalar_one()) for status in _QUEUE_STATES
        ]
    requested = await request(engine)
    manifest = await recorded(live, requested, monkeypatch)
    assert [op["ids"] for op in operations_of(manifest, "procrastinate_jobs")] == [[finished_job]]
    async with engine.begin() as conn:
        await conn.execute(_FINISH_JOB, {"j": doing})
    events = await scalar(engine, _JOB_EVENTS, j=doing)
    assert events >= 1  # procrastinate's trigger recorded the job's end (`succeeded`)
    await finished(live, requested)
    assert await exists(engine, "procrastinate_jobs", doing) and await exists(
        engine, "procrastinate_jobs", todo
    )
    assert await scalar(engine, _JOB_EVENTS, j=doing) == events
    assert not await exists(engine, "procrastinate_jobs", finished_job)
    assert await scalar(engine, _JOB_EVENTS, j=finished_job) == 0


async def test_the_reset_reuses_a_default_scope_version_that_step_10_never_lists(
    engine: AsyncEngine, live: Purger
) -> None:
    """Step 10's scope versions are listed at A2, the one `reset_to_defaults` reuses by hash left out (the
    default document's, settings version 1's): the reset points `active_config` at it, it is in the
    manifest's `retained`, and it still exists; the seed's other scope version is listed and gone."""
    await seeded(engine)
    before = await one(engine, _ACTIVE)
    requested = await request(engine)
    manifest = await finished(live, requested)
    listed = [i for op in operations_of(manifest, "scope_versions") for i in op["ids"]]
    assert manifest.scope is not None
    assert before.scope_version_id not in listed and listed
    assert before.scope_version_id in manifest.scope["retained"]["scope_versions"]
    after = await one(engine, _ACTIVE)
    assert after.scope_version_id == before.scope_version_id
    for scope_id in listed:
        assert not await exists(engine, "scope_versions", scope_id)


async def test_a_finished_unpinged_run_is_pinged_before_the_runs_go(
    engine: AsyncEngine, live: Purger, current_epoch: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scheduled run that ended `succeeded` before the purge and was not pinged yet: the live purge runs
    the dead-man's durable ping pass before step 7 deletes the runs (the run still exists then), so the
    fake healthchecks receives its `success` ping and the watermark passes it; then the run is gone."""
    seed = await seeded(engine)
    present_at_ping: list[bool] = []
    with fake_healthchecks() as (check, _):
        async with deadman.make_http_client(user_agent="JobHunter/test") as http:
            client = HealthchecksClient(
                http, ping_url=PING_URL, api_url=API_URL, api_key=API_KEY, sleep=Sleeps()
            )
            pinger = DeadmanPinger(engine, client, epoch=current_epoch)
            real = pinger.ping_pending

            async def observed() -> int:
                present_at_ping.append(await exists(engine, "runs", seed.unpinged_run_id))
                return await real()

            monkeypatch.setattr(pinger, "ping_pending", observed)
            pinging = Purger(live.engine, live.cfg, dataclasses.replace(live.deps, deadman=pinger))
            requested = await request(engine)
            await finished(pinging, requested)
    assert present_at_ping == [True] and check.pings == ["success"]
    watermark = await scalar(engine, sa.text("SELECT last_pinged_run_id FROM deadman_state"))
    assert watermark == seed.unpinged_run_id
    assert not await exists(engine, "runs", seed.unpinged_run_id)


async def test_schedule_state_points_at_the_new_default_settings_version(
    engine: AsyncEngine, live: Purger
) -> None:
    """Step 8: `active_config` and `schedule_state` both name the new default settings version (above the
    bound, the shipped document), the profile, tuple and pending scope NULL; the schedule reset as a save's
    `schedule` action resets it, at the purge's clock."""
    await seeded(engine)
    requested = await request(engine)
    manifest = await finished(live, requested)
    assert manifest.scope is not None
    active = await one(engine, _ACTIVE)
    schedule = await one(engine, _SCHEDULE)
    assert schedule.settings_version_id == active.settings_version_id
    assert active.settings_version_id > manifest.scope["max_ids"]["settings_versions"]
    assert (active.pending_scope_version_id, active.profile_version_id, active.llm_tuple_id) == (
        None,
        None,
        None,
    )
    version = await one(engine, _VERSION, v=active.settings_version_id)
    settings = Settings.model_validate(load_settings_v1())
    assert version.payload == settings.to_payload() and version.effect_classes == ["none"]
    now = schedule.updated_at
    anchor = anchor_utc_for(now, settings.schedule.anchor_local, settings.display.timezone)
    interval_s = settings.schedule.interval_hours * 3600
    assert (schedule.anchor_utc, schedule.interval_s, schedule.enabled) == (anchor, interval_s, True)
    assert schedule.last_evaluated_slot == latest_slot_at_or_before(anchor, interval_s, now)


async def test_the_user_and_totp_are_kept_and_login_attempts_and_lockouts_are_gone(
    engine: AsyncEngine, live: Purger
) -> None:
    """M5-U3: the user account with its TOTP enrolment stays as it was; the login attempts and the lockouts
    (they hold IP addresses) are gone."""
    await seeded(engine)
    async with engine.begin() as conn:
        await conn.execute(_TOTP)
    user = await one(engine, _USER)
    requested = await request(engine)
    await finished(live, requested)
    assert tuple(await one(engine, _USER)) == tuple(user)
    assert user.totp_enabled is True and user.totp_secret_enc is not None
    assert await scalar(engine, sa.text("SELECT count(*) FROM login_attempts")) == 0
    assert await scalar(engine, sa.text("SELECT count(*) FROM login_lockouts")) == 0


async def test_a_lockout_taken_or_renewed_after_the_manifest_survives_a_reexecution(
    engine: AsyncEngine, live: Purger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`login_lockouts` is bounded by its `generation` (Task 1: every lock or renewal takes a new one), so no
    IP address enters the manifest: the seed's lockout renewed after A3 and a lockout taken then survive the
    purge, and again a replay's re-execution of it."""
    await seeded(engine)
    requested = await request(engine)
    manifest = await recorded(live, requested, monkeypatch)
    assert manifest.scope is not None
    assert "198.51.100" not in str(manifest.scope)
    async with engine.begin() as conn:
        for scope in ("ip:198.51.100.8", "ip:198.51.100.9"):
            await conn.execute(_RENEW_LOCKOUT, {"scope": scope})
    taken = [tuple(r) for r in await rows(engine, _LOCKOUTS)]
    assert [scope for scope, _ in taken] == ["ip:198.51.100.8", "ip:198.51.100.9"]
    assert all(generation > manifest.scope["max_ids"]["login_lockouts"] for _, generation in taken)
    done = await finished(live, requested)
    assert [tuple(r) for r in await rows(engine, _LOCKOUTS)] == taken
    assert done.scope is not None
    await KINDS["delete_everything"].execute(replay_run(live, done), done.scope)
    assert [tuple(r) for r in await rows(engine, _LOCKOUTS)] == taken


async def test_its_own_idempotency_row_survives_and_sessions_end_at_completion(
    engine: AsyncEngine, live: Purger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The request's idempotency row is kept by its `id` (the manifest's `keep`), in the live run and again
    in a replay's re-execution on the same database. The sessions created at or before `fenced_at` (the
    seed's and the requester's) last until the completing transaction, which deletes them; one created after
    the fence's cutoff stays."""
    await seeded(engine)
    async with engine.begin() as conn:
        await conn.execute(_SESSION, {"token": b"requester"})
    requested = await request(engine)
    manifest = await recorded(live, requested, monkeypatch)
    async with engine.begin() as conn:
        await conn.execute(_SESSION, {"token": b"later"})
    assert manifest.scope is not None
    keep = [op["keep"] for op in operations_of(manifest, "api_idempotency")]
    assert keep == [[requested.idempotency_row_id]]
    point = PausePoint(purge.BEFORE_COMPLETE)
    monkeypatch.setattr(purge, "afault_point", point)
    task = await paused_at(live, requested, point)
    before_completion = [bytes(r.token_hmac) for r in await rows(engine, _SESSIONS)]
    point.resume()
    await task
    done = await completed(live, requested.opened.manifest_id, requested.opened.operation_id)
    assert {b"requester", b"later"} <= set(before_completion)  # the user stays logged in until then
    assert [bytes(r.token_hmac) for r in await rows(engine, _SESSIONS)] == [b"later"]
    assert await exists(engine, "api_idempotency", requested.idempotency_row_id)
    assert done.scope is not None
    await KINDS["delete_everything"].execute(replay_run(live, done), done.scope)
    assert await exists(engine, "api_idempotency", requested.idempotency_row_id)
    async with engine.connect() as conn:
        assert await check_postconditions(conn, done.scope, files=live.file_roots) == []


async def test_a_row_created_after_max_ids_survives_a_reexecution(
    engine: AsyncEngine, live: Purger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every deletion is bounded by `max_ids`: a job, a query and a login attempt created after A2 survive
    the purge and a replay's re-execution of it."""
    await seeded(engine)
    requested = await request(engine)
    await recorded(live, requested, monkeypatch)
    async with engine.begin() as conn:
        job = await insert_job(conn)
        query = int((await conn.execute(_QUERY)).scalar_one())
        attempt = int((await conn.execute(_LOGIN_ATTEMPT)).scalar_one())
    done = await finished(live, requested)
    assert done.scope is not None
    for _ in range(2):
        assert await exists(engine, "jobs", job.job_id)
        assert await exists(engine, "queries", query)
        assert await exists(engine, "login_attempts", attempt)
        await KINDS["delete_everything"].execute(replay_run(live, done), done.scope)


async def test_another_purges_deleted_operation_is_recreated_from_its_row(
    engine: AsyncEngine, live: Purger, purger: Purger, current_epoch: int
) -> None:
    """Deviation 53: a golden-case delete opened before, its operation `pending`: delete-everything cancels
    and deletes that operation and keeps its manifest `executing`. After its completion (its global fence
    released) the `purge_fences` reconciler creates a new operation from the manifest's request, which
    completes (its case is gone already); no fence is left active and no manifest executing, so an export
    requested afterwards is accepted and ends `ready` (M5 Task 17, T16-3 (7))."""
    seed = await seeded(engine)
    async with engine.begin() as conn:
        golden = await request_golden_case_delete(conn, seed.purgeable_case_id, now=utcnow(), defer=None)
    requested = await request(engine)
    await finished(live, requested)
    assert not await exists(engine, "operations", golden.operation_id)
    assert (await manifest_of(engine, golden.manifest_id)).status == "executing"
    assert await run_reconciler("purge_fences", engine, now=utcnow(), epoch=current_epoch) >= 1
    (recreated,) = await rows(engine, _PURGE_OPERATIONS, m=golden.manifest_id)
    assert recreated.status == "pending" and recreated.id != golden.operation_id
    await purger.run(int(recreated.id))
    await completed(purger, golden.manifest_id, int(recreated.id))
    left = await one(engine, _PURGE_FENCES_LEFT)
    assert (left.fences, left.executing) == (0, 0)
    async with engine.begin() as conn:
        export_id = await request_export(conn, now=utcnow(), defer=None)
        export_operation = int((await conn.execute(_EXPORT_OPERATION, {"e": export_id})).scalar_one())
    await purger.run(export_operation)  # the worker's `run_operation`, with the worker's dependencies
    assert (await one(engine, _OPERATION, o=export_operation)).status == "succeeded"
    exported = await one(engine, _EXPORT, e=export_id)
    assert exported.status == "ready" and (purger.cfg.exports_dir / exported.storage_path).is_file()


async def test_delete_everything_purges_exports(engine: AsyncEngine, live: Purger) -> None:
    """Deviation 18: a `ready` export ends `purged` with no `storage_path`, its file gone."""
    seed = await seeded(engine)
    path = live.cfg.exports_dir / f"{seed.ready_export_id}-seed.jhx"
    path.write_bytes(b"\x00 encrypted segments")
    requested = await request(engine)
    await finished(live, requested)
    assert tuple(await one(engine, _EXPORT, e=seed.ready_export_id)) == ("purged", None)
    assert not path.exists()


async def test_the_sessions_go_before_the_fence_is_released(
    engine: AsyncEngine, live: Purger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The completing transaction deletes the sessions (`CompletingKind.complete`) before it releases the
    global fence and checks the postconditions: when `release_fences` runs, no session created at or before
    `fenced_at` is left in that transaction."""
    await seeded(engine)
    seen: list[int] = []
    real = fences.release_fences  # the function `purge` imported

    async def observed(conn: Any, manifest_id: int) -> int:
        seen.append(int((await conn.execute(sa.text("SELECT count(*) FROM sessions"))).scalar_one()))
        return await real(conn, manifest_id)

    monkeypatch.setattr(purge, "release_fences", observed)
    assert await scalar(engine, sa.text("SELECT count(*) FROM sessions")) >= 1
    requested = await request(engine)
    await finished(live, requested)
    assert seen == [0]


async def test_runs_are_deleted_within_their_bound_only(engine: AsyncEngine, live: Purger) -> None:
    """§19.5: every deletion stays within `max_ids` (Ruling T16-4: the tick queues no run under the fence,
    so no run above the bound names a deleted settings version): the content deletes `runs` by its own id
    bound only."""
    await seeded(engine)
    manifest = await finished(live, await request(engine))
    assert [op["bound"] for op in operations_of(manifest, "runs")] == [{"by": "id"}]
    assert await scalar(engine, sa.text("SELECT count(*) FROM runs")) == 0


async def test_migrate_after_delete_everything_adds_no_settings_version_and_moves_no_pointer(
    engine: AsyncEngine, live: Purger, make_config: ConfigFactory, tmp_path: Path
) -> None:
    """Delete-everything deletes settings version 1; the next `migrate` seeds no settings version again
    (only an empty `settings_versions` gets the defaults) and leaves `active_config` and `schedule_state`
    as the purge left them.

    Its manifest stream is a directory of the test's own that holds no file yet (M5 Task 23), so step 1
    rewrites there, from their scopes, the files of both manifests the database records: the
    delete-everything's, byte for byte the file its purge wrote, and the seed's shipped golden-case one. The
    shared server's manifests directory is left as it was."""
    await seeded(engine)
    manifest = await finished(live, await request(engine))
    before = [tuple(r) for r in await rows(engine, _CONFIGURATION)]
    cfg = make_config("migrate")
    shared = files_in(cfg.manifests_dir)
    stream = tmp_path / "migrate_manifests"
    stream.mkdir(mode=0o700)
    await run_migrate_async(cfg, now=utcnow(), manifest_dir=stream)
    assert [tuple(r) for r in await rows(engine, _CONFIGURATION)] == before
    recorded = await rows(engine, _RECORDED_MANIFESTS)
    assert [row.kind for row in recorded] == ["delete_everything", "golden_case_delete"]
    rewritten = files_in(stream)
    assert {name: hashlib.sha256(data).digest() for name, data in rewritten.items()} == {
        manifest_file_name(row.manifest_uuid): bytes(row.file_sha256) for row in recorded
    }
    purge_file = files_in(live.cfg.manifests_dir)[manifest_file_name(manifest.manifest_uuid)]
    assert rewritten[manifest_file_name(manifest.manifest_uuid)] == purge_file
    assert files_in(cfg.manifests_dir) == shared
