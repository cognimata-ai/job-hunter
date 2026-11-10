"""What M5 adds to `GET /api/system/health` (spec §17.5 L5625; plan M5 Tasks 6 and 7, O16, O20, Q17,
Rulings T6-3, T7-1 to T7-3).

`langsmith` is the tracing state the last worker published at its start (`publish_tracing_state`, on
`worker_heartbeat.tracing`, in a transaction that starts with its epoch fence): `null` until a worker
published one, and not cleared when the heartbeat goes stale. The api has no LangSmith configuration of its
own, so this is all it can report.

`backups` reports the backup tables as they are, in every env (Ruling T7-1): the latest set (any status) with
its age, the backup age state (`observability.backup_health.backup_age_state`, the rule the worker's alert
follows), the latest manifest shipment with its item count, the completed manifests not shipped yet with the
oldest one's completion, and the latest restore test (any status) with the age state of the newest succeeded
one: `warn` past 35 days, `none` without one, never an alert (O20). Every row is invented.
"""

from datetime import UTC, datetime, timedelta
from typing import Final

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.observability.langsmith import TracingState, publish_tracing_state
from jobhunter.worker.epoch import fence_epoch

from tests.support.backups import backup_run, backup_set, manifest, shipment

pytestmark = pytest.mark.integration

SYSTEM_UTCNOW: Final = "jobhunter.api.routes.system.utcnow"
NO_BACKUPS: Final = {
    "latest_set": None,
    "backup_age_state": "grace",  # the test database was installed minutes ago
    "latest_shipment": None,
    "unshipped_manifests": 0,
    "oldest_unshipped_completed_at": None,
    "latest_restore_test": None,
    "restore_test_age_state": "none",
}


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


async def published(engine, epoch: int, state: TracingState) -> None:
    async with engine.begin() as conn:
        await fence_epoch(conn, epoch)
        await publish_tracing_state(conn, state)


async def test_health_reports_the_published_tracing_state(logged_in_client, engine, current_epoch) -> None:
    await published(engine, current_epoch, TracingState(tracing=True, region="eu"))
    async with engine.connect() as conn:
        stored = (
            await conn.execute(sa.text("SELECT tracing FROM worker_heartbeat WHERE id = 1"))
        ).scalar_one()
    assert stored == {"tracing": True, "region": "eu"}
    body = (await logged_in_client.get("/api/system/health")).json()
    assert body["langsmith"] == {"tracing": True, "region": "eu"}
    # the next worker's start publishes its own state over it
    await published(engine, current_epoch, TracingState(tracing=False, region=None))
    body = (await logged_in_client.get("/api/system/health")).json()
    assert body["langsmith"] == {"tracing": False, "region": None}
    # a stale heartbeat fails readiness but keeps the last published state
    async with engine.begin() as conn:
        await conn.execute(sa.text("UPDATE worker_heartbeat SET heartbeat_at = now() - interval '1 hour'"))
    body = (await logged_in_client.get("/api/system/health")).json()
    assert "worker_heartbeat" in body["ready"]["failing"]
    assert body["langsmith"] == {"tracing": False, "region": None}


@pytest.mark.parametrize("heartbeat", [False, True], ids=["no_heartbeat_row", "row_not_published"])
async def test_health_langsmith_is_null_before_the_worker_published(
    logged_in_client, request: pytest.FixtureRequest, heartbeat: bool
) -> None:
    if heartbeat:  # the row a worker's takeover writes, before its start published anything
        request.getfixturevalue("current_epoch")
    body = (await logged_in_client.get("/api/system/health")).json()
    assert "langsmith" in body and body["langsmith"] is None
    assert ("worker_heartbeat" in body["ready"]["failing"]) is not heartbeat


async def test_health_reports_backups_shipments_and_unshipped_manifests(
    logged_in_client, engine, monkeypatch
) -> None:
    now = utcnow()
    monkeypatch.setattr(SYSTEM_UTCNOW, lambda: now)
    assert (await logged_in_client.get("/api/system/health")).json()["backups"] == NO_BACKUPS

    good = now - timedelta(hours=30)
    used = await backup_set(engine, "succeeded", good)
    failed = await backup_set(engine, "failed", now - timedelta(hours=1))
    await shipment(engine, now - timedelta(days=2), manifests=1)
    latest = await shipment(engine, now - timedelta(hours=5), manifests=2)
    oldest = now - timedelta(hours=3)
    await manifest(engine, "completed", oldest)
    await manifest(engine, "completed", now - timedelta(minutes=5))
    await manifest(engine, "executing")  # not completed yet: not counted
    tested = now - timedelta(days=3)
    restore_test = await backup_run(engine, "succeeded", tested, set_id=used)
    body = (await logged_in_client.get("/api/system/health")).json()
    assert body["backups"] == {
        # the newest set whatever its status; the age state from the newest succeeded one
        "latest_set": {
            "id": failed,
            "status": "failed",
            "finished_at": iso(now - timedelta(hours=1)),
            "age_s": 3600.0,
        },
        "backup_age_state": "ok",
        "latest_shipment": {"id": latest, "shipped_at": iso(now - timedelta(hours=5)), "manifests": 2},
        "unshipped_manifests": 2,
        "oldest_unshipped_completed_at": iso(oldest),
        "latest_restore_test": {
            "id": restore_test,
            "status": "succeeded",
            "finished_at": iso(tested),
            "age_s": timedelta(days=3).total_seconds(),
        },
        "restore_test_age_state": "ok",
    }

    # a set still running is the latest, with no age yet; the newest succeeded one is now 37 h old
    running = await backup_set(engine, "running")
    later = good + timedelta(hours=37)
    monkeypatch.setattr(SYSTEM_UTCNOW, lambda: later)
    backups = (await logged_in_client.get("/api/system/health")).json()["backups"]
    assert backups["latest_set"] == {"id": running, "status": "running", "finished_at": None, "age_s": None}
    assert backups["backup_age_state"] == "warn"
    monkeypatch.setattr(SYSTEM_UTCNOW, lambda: good + timedelta(hours=72, seconds=1))
    backups = (await logged_in_client.get("/api/system/health")).json()["backups"]
    assert backups["backup_age_state"] == "critical"
    # the health reports; it raises nothing (the worker's reconciler does)
    async with engine.connect() as conn:
        raised = (
            await conn.execute(sa.text("SELECT count(*) FROM alerts WHERE code LIKE 'backup.%'"))
        ).scalar()
    assert raised == 0


async def test_restore_test_age_warns_after_35_days(logged_in_client, engine, monkeypatch) -> None:
    async def restore_tests(now: datetime) -> tuple[object, object]:
        monkeypatch.setattr(SYSTEM_UTCNOW, lambda: now)
        backups = (await logged_in_client.get("/api/system/health")).json()["backups"]
        return backups["latest_restore_test"], backups["restore_test_age_state"]

    # without a succeeded restore test there is no age to judge
    first_at = utcnow() - timedelta(days=60)
    first = await backup_run(engine, "failed", first_at)
    failure = {"id": first, "status": "failed", "finished_at": iso(first_at), "age_s": 86_400.0}
    assert await restore_tests(first_at + timedelta(days=1)) == (failure, "none")

    tested = first_at + timedelta(days=2)
    succeeded = await backup_run(engine, "succeeded", tested)
    at_35_days = tested + timedelta(days=35)
    latest = {"id": succeeded, "status": "succeeded", "finished_at": iso(tested), "age_s": 35 * 86_400.0}
    assert await restore_tests(at_35_days) == (latest, "ok")
    later = at_35_days + timedelta(seconds=1)
    assert await restore_tests(later) == ({**latest, "age_s": 35 * 86_400.0 + 1}, "warn")

    # a newer failed restore test is the latest; the age still counts from the newest succeeded one
    failed_at = tested + timedelta(days=34)
    failed = await backup_run(engine, "failed", failed_at)
    failure = {"id": failed, "status": "failed", "finished_at": iso(failed_at), "age_s": 86_401.0}
    assert await restore_tests(later) == (failure, "warn")
