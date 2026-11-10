"""The backup alerts the worker derives from the backup tables (spec §17.5 L5625, §17.6; plan M5 Task 7, O1,
O20, Deviation 12, Rulings T7-1 to T7-3).

The backup role cannot write `alerts`, so the heavy `backup_health` reconciler judges the backup tables:

- `backup.failed` (critical, subject `set:<id>`) for every `failed` set with no `succeeded` set of a greater
  id, resolved once one exists;
- `backup.stale` (warn, subject `backup`) past 36 h since the newest succeeded set's `finished_at`, or, with
  none, past the 48 h grace after install (the first settings version), escalated to critical past 72 h, and
  resolved once the newest succeeded set finished within 36 h;
- `backup.restore_test_failed` (critical, subject `restore_test:<id>`) for every failed restore test with no
  succeeded one of a greater id; the restore test's age is a health state, never an alert (O20);
- the gauge `jobhunter_backup_age_seconds`, on the same age reference.

In `dev`, where no backups run, it does nothing, the gauge included (Ruling T7-1). Every row is invented: the
snapshot ids and hashes are stand-ins.
"""

import math
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, Final, get_args

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.config import AppConfig
from jobhunter.db.transitions import TRANSITIONS
from jobhunter.errors import FencedOut
from jobhunter.observability import backup_health
from jobhunter.observability.backup_health import (
    BACKUP_CRITICAL_AFTER,
    BACKUP_INSTALL_GRACE,
    BACKUP_WARN_AFTER,
    RESTORE_TEST_WARN_AFTER,
    BackupAge,
    BackupStatus,
    backup_age_s,
    backup_state,
)
from jobhunter.observability.metrics import BACKUP_AGE, REGISTRY
from jobhunter.worker import reconcilers
from jobhunter.worker.main import _REGISTERING_MODULES
from jobhunter.worker.reconcilers import ReconcileContext
from jobhunter.worker.tasks import defer_in_tx, task_app
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.support.backups import backup_run, backup_set

pytestmark = pytest.mark.integration

FAILED: Final = "backup.failed"
STALE: Final = "backup.stale"
RESTORE_FAILED: Final = "backup.restore_test_failed"
SECOND: Final = timedelta(seconds=1)
_ALERTS: Final = sa.text(
    "SELECT id, severity, dedup_key, message, context, occurrences, last_notified_at, acknowledged_at, "
    "resolved_at FROM alerts WHERE code = :code ORDER BY id"
)
_INSTALLED_AT: Final = sa.text("SELECT min(created_at) FROM settings_versions")


async def alerts(engine: AsyncEngine, code: str) -> Sequence[Any]:
    async with engine.connect() as conn:
        return (await conn.execute(_ALERTS, {"code": code})).all()


async def open_keys(engine: AsyncEngine, code: str) -> set[str]:
    return {a.dedup_key for a in await alerts(engine, code) if a.resolved_at is None}


async def installed_at(engine: AsyncEngine) -> datetime:
    """The install anchor: the first settings version's `created_at`."""
    async with engine.connect() as conn:
        first: datetime = (await conn.execute(_INSTALLED_AT)).scalar_one()
    return first


async def judge(engine: AsyncEngine, now: datetime, *, epoch: int = 1, cfg: AppConfig | None = None) -> int:
    """One pass of the registered `backup_health` reconciler at `now` (the test's `queue_app` is the task
    app; the reconciler defers nothing)."""
    cadences, fn = reconcilers.REGISTRY["backup_health"]
    assert cadences == {"heavy"}
    ctx = ReconcileContext(engine=engine, queue=task_app(), defer=defer_in_tx, now=now, epoch=epoch, cfg=cfg)
    return await fn(ctx)


async def age_state(engine: AsyncEngine, now: datetime) -> str:
    """The health's `backup_age_state` at `now`: the state the alert follows."""
    async with engine.connect() as conn:
        return (await backup_state(conn, now=now)).backup_age_state


def gauge() -> float | None:
    return REGISTRY.get_sample_value("jobhunter_backup_age_seconds")


def unknown(value: float | None) -> bool:
    """Whether the gauge reads NaN: no age to report."""
    return value is not None and math.isnan(value)


def test_the_thresholds_and_the_worker_s_module() -> None:
    assert BACKUP_WARN_AFTER == timedelta(hours=36)
    assert BACKUP_CRITICAL_AFTER == timedelta(hours=72)
    assert BACKUP_INSTALL_GRACE == timedelta(hours=48)
    assert RESTORE_TEST_WARN_AFTER == timedelta(days=35)
    assert "jobhunter.observability.backup_health" in _REGISTERING_MODULES
    # the statuses the health reports are the two state machines' (the `status` CHECKs of migration 0010)
    for table in ("backup_sets", "backup_runs"):
        states = {state for edge in TRANSITIONS[table].allowed for state in edge}
        assert states == set(get_args(BackupStatus)), table


async def test_a_failed_set_raises_backup_failed_until_a_later_success(
    engine, current_epoch, queue_app
) -> None:
    now = utcnow()
    await backup_set(engine, "failed", now - timedelta(hours=4))  # healed by the success after it
    await backup_set(engine, "succeeded", now - timedelta(hours=3))
    first = await backup_set(engine, "failed", now - timedelta(hours=2))
    assert await judge(engine, now) == 1
    # the failure before a success raises nothing; the one after it raises, with its id only
    [raised] = await alerts(engine, FAILED)
    assert raised.dedup_key == f"{FAILED}:set:{first}"
    assert (raised.severity, raised.context) == ("critical", {"backup_set_id": first})
    assert raised.message == f"backup set {first} failed"

    # a later pass finds the same failure: no new occurrence of it
    assert await judge(engine, now) == 0
    [again] = await alerts(engine, FAILED)
    assert (again.id, again.occurrences, again.resolved_at) == (raised.id, 1, None)

    # another failure opens its own alert; a set still running is no success
    second = await backup_set(engine, "failed", now - timedelta(hours=1))
    await backup_set(engine, "running")
    assert await judge(engine, now) == 1
    assert await open_keys(engine, FAILED) == {f"{FAILED}:set:{first}", f"{FAILED}:set:{second}"}

    # a later success resolves every open one
    await backup_set(engine, "succeeded", now)
    assert await judge(engine, now) == 2
    assert await open_keys(engine, FAILED) == set()
    assert len(await alerts(engine, FAILED)) == 2
    assert await alerts(engine, STALE) == []  # every success was fresh


async def test_stale_warns_at_36h_escalates_at_72h_and_resolves(engine, current_epoch, queue_app) -> None:
    finished = utcnow()
    await backup_set(engine, "succeeded", finished)
    at_warn = finished + BACKUP_WARN_AFTER
    assert await judge(engine, at_warn) == 0
    assert await alerts(engine, STALE) == []
    assert await age_state(engine, at_warn) == "ok"

    past_warn = at_warn + SECOND
    assert await judge(engine, past_warn) == 1
    [warn] = await alerts(engine, STALE)
    assert (warn.dedup_key, warn.severity) == (f"{STALE}:backup", "warn")
    assert warn.context == {"age_s": int((past_warn - finished).total_seconds())}
    assert await age_state(engine, past_warn) == "warn"

    # a failed set is no fresh one; pushed and acknowledged meanwhile
    await backup_set(engine, "failed", finished + timedelta(hours=40))
    async with engine.begin() as conn:
        await conn.execute(
            sa.text("UPDATE alerts SET last_notified_at = now(), acknowledged_at = now() WHERE id = :a"),
            {"a": warn.id},
        )
    at_critical = finished + BACKUP_CRITICAL_AFTER
    await judge(engine, at_critical)
    [still] = await alerts(engine, STALE)
    assert (still.id, still.severity, still.resolved_at) == (warn.id, "warn", None)

    past_critical = at_critical + SECOND
    await judge(engine, past_critical)
    [critical] = await alerts(engine, STALE)
    assert (critical.id, critical.severity) == (warn.id, "critical")  # the open alert, escalated
    assert (critical.last_notified_at, critical.acknowledged_at) == (None, None)  # pushed again at once
    assert await age_state(engine, past_critical) == "critical"

    # a fresh set resolves it (and the failed set's alert)
    fresh = finished + timedelta(hours=73)
    await backup_set(engine, "succeeded", fresh)
    assert await judge(engine, fresh + BACKUP_WARN_AFTER) == 2
    assert await open_keys(engine, FAILED) == set()
    [resolved] = await alerts(engine, STALE)
    assert resolved.id == warn.id and resolved.resolved_at is not None
    assert await age_state(engine, fresh + BACKUP_WARN_AFTER) == "ok"


async def test_no_set_within_48h_of_install_is_grace(engine, current_epoch, queue_app) -> None:
    installed = await installed_at(engine)
    at_grace_end = installed + BACKUP_INSTALL_GRACE
    assert await judge(engine, at_grace_end) == 0
    assert await alerts(engine, STALE) == []
    assert await age_state(engine, at_grace_end) == "grace"
    # a failed set is no backup: the grace still holds, and it raises its own alert
    failed = await backup_set(engine, "failed", installed + timedelta(hours=1))
    assert await judge(engine, at_grace_end) == 1
    assert await open_keys(engine, FAILED) == {f"{FAILED}:set:{failed}"}
    assert await alerts(engine, STALE) == []

    # past the grace, the age counts from install
    past_grace = at_grace_end + SECOND
    await judge(engine, past_grace)
    [warn] = await alerts(engine, STALE)
    assert warn.severity == "warn"
    assert warn.context == {"age_s": int((past_grace - installed).total_seconds())}
    assert await age_state(engine, past_grace) == "warn"
    # exactly 72 h after install is still warn: critical is past it, as with a set
    at_critical = installed + BACKUP_CRITICAL_AFTER
    await judge(engine, at_critical)
    [still] = await alerts(engine, STALE)
    assert (still.id, still.severity, still.resolved_at) == (warn.id, "warn", None)
    assert await age_state(engine, at_critical) == "warn"
    past_critical = at_critical + SECOND
    await judge(engine, past_critical)
    [critical] = await alerts(engine, STALE)
    assert (critical.id, critical.severity) == (warn.id, "critical")
    assert await age_state(engine, past_critical) == "critical"


async def test_no_install_anchor_and_no_set_is_grace_with_an_unknown_age(
    engine, current_epoch, queue_app, monkeypatch
) -> None:
    """A database without a settings version has no install anchor: with no set either there is nothing to
    measure from, so nothing is raised and the age is unknown. Migrate always seeds settings version 1, so the
    read of the anchor is made to find none; the sets are read from the database."""
    read = backup_health.read_backup_age

    async def unseeded(conn: AsyncConnection) -> BackupAge:
        return BackupAge(last_success_at=(await read(conn)).last_success_at, installed_at=None)

    monkeypatch.setattr(backup_health, "read_backup_age", unseeded)
    late = await installed_at(engine) + BACKUP_CRITICAL_AFTER * 10
    BACKUP_AGE.set(-1.0)
    assert await judge(engine, late) == 0
    assert await alerts(engine, STALE) == []
    assert unknown(gauge())
    assert await age_state(engine, late) == "grace"
    # a failed set is still no backup: its own alert, and still nothing to measure the age from
    failed = await backup_set(engine, "failed", late)
    assert await judge(engine, late) == 1
    assert await open_keys(engine, FAILED) == {f"{FAILED}:set:{failed}"}
    assert await alerts(engine, STALE) == []
    assert unknown(gauge())


async def test_a_failed_restore_test_raises_and_a_later_success_resolves(
    engine, current_epoch, queue_app
) -> None:
    now = utcnow()
    used = await backup_set(engine, "succeeded", now - timedelta(hours=2))
    # a restore test that succeeded long ago raises nothing: its age is a health state only (O20)
    await backup_run(engine, "succeeded", now - RESTORE_TEST_WARN_AFTER * 2, set_id=used)
    assert await judge(engine, now) == 0
    failed = await backup_run(engine, "failed", now - timedelta(hours=1), set_id=used)
    await backup_run(engine, "failed", now - timedelta(hours=1), kind="prune")  # a prune raises nothing
    assert await judge(engine, now) == 1
    [raised] = await alerts(engine, RESTORE_FAILED)
    assert raised.dedup_key == f"{RESTORE_FAILED}:restore_test:{failed}"
    assert (raised.severity, raised.context) == ("critical", {"backup_run_id": failed})
    assert raised.message == f"restore test {failed} failed"
    assert await alerts(engine, FAILED) == []  # a restore test's failure is not the set's

    await backup_run(engine, "running", set_id=used)  # a restore test still running is no success
    assert await judge(engine, now) == 0
    assert await open_keys(engine, RESTORE_FAILED) == {raised.dedup_key}

    await backup_run(engine, "succeeded", now, set_id=used)
    assert await judge(engine, now) == 1
    assert await open_keys(engine, RESTORE_FAILED) == set()

    # a failure after that success raises again, its own alert
    later = await backup_run(engine, "failed", now, set_id=used)
    await judge(engine, now)
    assert await open_keys(engine, RESTORE_FAILED) == {f"{RESTORE_FAILED}:restore_test:{later}"}


async def test_the_backup_age_gauge(engine, current_epoch, queue_app) -> None:
    installed = await installed_at(engine)
    # no set yet: measured from install, the grace's reference
    await judge(engine, installed + timedelta(hours=10))
    assert gauge() == 36_000.0
    finished = utcnow()
    await backup_set(engine, "succeeded", finished)
    await backup_set(engine, "failed", finished + timedelta(hours=1))  # a failure does not reset it
    await judge(engine, finished + timedelta(hours=5))
    assert gauge() == 18_000.0
    assert backup_age_s(finished + timedelta(hours=5), last_success_at=finished, installed_at=installed) == (
        18_000.0
    )
    # with neither a set nor an install anchor the age is unknown
    assert math.isnan(backup_age_s(finished, last_success_at=None, installed_at=None))


@pytest.mark.parametrize("env", ["dev", "test", "prod"])
async def test_dev_runs_nothing(engine, current_epoch, queue_app, make_config, env: str) -> None:
    finished = utcnow() - timedelta(days=5)
    await backup_set(engine, "succeeded", finished)
    failed = await backup_set(engine, "failed", finished + timedelta(hours=1))
    test = await backup_run(engine, "failed", finished + timedelta(hours=2))
    now = utcnow()
    BACKUP_AGE.set(-1.0)
    count = await judge(engine, now, cfg=make_config("worker", JOBHUNTER_ENV=env))
    if env == "dev":
        assert count == 0
        assert [await alerts(engine, code) for code in (FAILED, STALE, RESTORE_FAILED)] == [[], [], []]
        assert gauge() == -1.0  # untouched
        return
    assert count == 3
    assert await open_keys(engine, FAILED) == {f"{FAILED}:set:{failed}"}
    assert [a.severity for a in await alerts(engine, STALE)] == ["critical"]
    assert await open_keys(engine, RESTORE_FAILED) == {f"{RESTORE_FAILED}:restore_test:{test}"}
    assert gauge() == pytest.approx((now - finished).total_seconds())


async def test_a_superseded_worker_writes_nothing(engine, current_epoch, queue_app) -> None:
    await backup_set(engine, "failed", utcnow())
    BACKUP_AGE.set(-1.0)
    with pytest.raises(FencedOut) as fenced:
        await judge(engine, utcnow(), epoch=current_epoch + 1)
    assert fenced.value.worker_superseded
    assert await alerts(engine, FAILED) == []
    assert gauge() == -1.0
