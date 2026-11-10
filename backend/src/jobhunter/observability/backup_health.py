"""The backups' health (spec §17.5 L5625, §17.6, §19.5; plan M5 Task 7, O1, O20, Deviation 12, Rulings T7-1
to T7-3): the backup alerts the worker derives from the backup tables, the backup age gauge, and the backups
section of the system health.

**The backup role cannot write `alerts`** (O1). The backup container records its sets (`backup_sets`), its
prune and restore-test runs (`backup_runs`) and its manifest shipments; the worker's heavy `backup_health`
reconciler derives the alerts from them, in one transaction that starts with the worker's epoch fence:

- `backup.failed` (critical, subject `set:<id>`, context `{backup_set_id}`) for every `failed` set that no
  `succeeded` set of a greater id follows, and `backup.restore_test_failed` (critical, subject
  `restore_test:<id>`, context `{backup_run_id}`) for every failed `restore_test` run that no succeeded one
  of a greater id follows (`_FAILURES`; a prune's failure has no alert in §17.6). Each failure opens its
  alert once (`open_alert`: a later pass that finds it again counts no occurrence), and every open one whose
  failure has such a later success is resolved.
- `backup.stale` (subject `backup`, context `{age_s}`), by `backup_age_state`: `warn` raises it warn,
  `critical` raises it critical, which escalates the open warn alert (`raise_alert`), `ok` resolves it, and
  `grace` writes nothing.
- the gauge `jobhunter_backup_age_seconds` (`backup_age_s`).

In `dev`, where no backups run, it does nothing, the gauge included (Ruling T7-1); a context without a
configuration (a test's) is judged. It returns the alerts it opened, raised or resolved, and raises
`FencedOut`, having written nothing, when its epoch is no longer the current one.

**One age reference** (Ruling T7-2) for the alert, the health's state and the gauge: the newest succeeded
set's `finished_at`, else the install anchor, the first settings version's `created_at` (the anchor of the run
age's grace, `health.run_age_state`). With a set: `ok` up to `BACKUP_WARN_AFTER` (36 h), `warn` past it and
`critical` past `BACKUP_CRITICAL_AFTER` (72 h). With none: `grace` until `BACKUP_INSTALL_GRACE` (48 h) after
install, then `warn`, and `critical` past 72 h after install. With neither a set nor an anchor: `grace`, and
the age is unknown (NaN). `backup.stale` is resolved once a fresh set exists: the newest succeeded set
finished within 36 h.

**The health's section** (`backup_state`, Ruling T7-3) reports the tables as they are, in every env (T7-1):
the latest set (the greatest id, any status) with its age since `finished_at` (`null` while it runs), the
backup age state, the latest manifest shipment (the greatest id) with the number of manifests it shipped (its
`manifest_shipment_items`), the manifests `completed` and not shipped yet with the oldest one's completion
time, and the latest restore test (any status) with `restore_test_age_state`: `warn` past
`RESTORE_TEST_WARN_AFTER` (35 days) since the newest succeeded restore test finished, `none` without one, else
`ok`. That age is a health state only, never an alert (O20). Ids, statuses, counts and times only: never a
set's `error` or a run's report.
"""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Literal

from pydantic import BaseModel
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import TextClause

from jobhunter.db.sql import q
from jobhunter.observability.alerts import open_alert, open_subjects, raise_alert, resolve_alert
from jobhunter.observability.catalogue import CATALOGUE, Severity
from jobhunter.observability.metrics import BACKUP_AGE
from jobhunter.wire import UtcDatetime
from jobhunter.worker.epoch import fence_epoch
from jobhunter.worker.reconcilers import ReconcileContext, reconciler

BACKUP_WARN_AFTER: Final = timedelta(hours=36)
BACKUP_CRITICAL_AFTER: Final = timedelta(hours=72)
BACKUP_INSTALL_GRACE: Final = timedelta(hours=48)
RESTORE_TEST_WARN_AFTER: Final = timedelta(days=35)

BACKUP_FAILED_ALERT: Final = "backup.failed"
BACKUP_STALE_ALERT: Final = "backup.stale"
RESTORE_TEST_FAILED_ALERT: Final = "backup.restore_test_failed"
BACKUP_STALE_SUBJECT: Final = "backup"

BackupAgeState = Literal["ok", "warn", "critical", "grace"]
RestoreTestAgeState = Literal["ok", "warn", "none"]
# The values of the `status` CHECK of `backup_sets` and of `backup_runs` (migration 0010).
BackupStatus = Literal["running", "succeeded", "failed"]

# The two candidates of the age reference: `backup_sets_latest` serves the newest success.
_AGE_REFERENCE: Final = q(
    "SELECT (SELECT max(finished_at) FROM backup_sets WHERE status = 'succeeded') AS last_success_at, "
    "(SELECT min(created_at) FROM settings_versions) AS installed_at"
)
# The figures of the health's section beside the latest rows; `deletion_manifests_open` serves the unshipped.
_SECTION_FIGURES: Final = q(
    "SELECT (SELECT max(finished_at) FROM backup_runs WHERE kind = 'restore_test' AND status = 'succeeded') "
    "AS last_restore_test_at, "
    "(SELECT count(*) FROM deletion_manifests WHERE status = 'completed') AS unshipped, "
    "(SELECT min(completed_at) FROM deletion_manifests WHERE status = 'completed') AS oldest_unshipped_at"
)
_LATEST_SET: Final = q("SELECT id, status, finished_at FROM backup_sets ORDER BY id DESC LIMIT 1")
_LATEST_RESTORE_TEST: Final = q(
    "SELECT id, status, finished_at FROM backup_runs WHERE kind = 'restore_test' ORDER BY id DESC LIMIT 1"
)
_LATEST_SHIPMENT: Final = q(
    "SELECT s.id, s.shipped_at, "
    "(SELECT count(*) FROM manifest_shipment_items i WHERE i.shipment_id = s.id) AS manifests "
    "FROM manifest_shipments s ORDER BY s.id DESC LIMIT 1"
)
# The failures no success of a greater id follows (Ruling T7-2), in id order.
_FAILED_SETS: Final = q(
    "SELECT id FROM backup_sets WHERE status = 'failed' "
    "AND id > coalesce((SELECT max(id) FROM backup_sets WHERE status = 'succeeded'), 0) ORDER BY id"
)
_FAILED_RESTORE_TESTS: Final = q(
    "SELECT id FROM backup_runs WHERE kind = 'restore_test' AND status = 'failed' "
    "AND id > coalesce((SELECT max(id) FROM backup_runs "
    "WHERE kind = 'restore_test' AND status = 'succeeded'), 0) ORDER BY id"
)


@dataclass(frozen=True)
class _Failure:
    """A failure alert raised per row: its code, its subject's prefix (`<prefix>:<id>`), the row's noun in the
    message, the context's key of the row's id, and the query of the failures no success follows."""

    code: str
    prefix: str
    noun: str
    context_key: str
    failing: TextClause


_FAILURES: Final = (
    _Failure(BACKUP_FAILED_ALERT, "set", "backup set", "backup_set_id", _FAILED_SETS),
    _Failure(
        RESTORE_TEST_FAILED_ALERT, "restore_test", "restore test", "backup_run_id", _FAILED_RESTORE_TESTS
    ),
)


@dataclass(frozen=True)
class BackupAge:
    """The age reference's two candidates: the newest succeeded set's `finished_at` (`None` before the first)
    and the install anchor, the first settings version's `created_at` (`None` when nothing is seeded)."""

    last_success_at: datetime | None
    installed_at: datetime | None


async def read_backup_age(conn: AsyncConnection) -> BackupAge:
    """The age reference's candidates, in one statement on `conn`."""
    row = (await conn.execute(_AGE_REFERENCE)).one()
    return BackupAge(last_success_at=row.last_success_at, installed_at=row.installed_at)


def backup_age_state(
    now: datetime, *, last_success_at: datetime | None, installed_at: datetime | None
) -> BackupAgeState:
    """How old the backups are at `now` (module docstring, One age reference): `grace` without a succeeded
    set until `BACKUP_INSTALL_GRACE` after install (its end included), and without an install anchor either;
    otherwise `critical` past `BACKUP_CRITICAL_AFTER`, `warn` past `BACKUP_WARN_AFTER`, else `ok`."""
    reference = last_success_at or installed_at
    if reference is None:
        return "grace"
    age = now - reference
    if last_success_at is None and age <= BACKUP_INSTALL_GRACE:
        return "grace"
    if age > BACKUP_CRITICAL_AFTER:
        return "critical"
    if age > BACKUP_WARN_AFTER:
        return "warn"
    return "ok"


def backup_age_s(now: datetime, *, last_success_at: datetime | None, installed_at: datetime | None) -> float:
    """The backup age at `now` in seconds, on the same reference as `backup_age_state`; NaN without one."""
    reference = last_success_at or installed_at
    return math.nan if reference is None else (now - reference).total_seconds()


def restore_test_age_state(now: datetime, last_success_at: datetime | None) -> RestoreTestAgeState:
    """`none` without a succeeded restore test, `warn` past `RESTORE_TEST_WARN_AFTER` since the newest one
    finished, else `ok`."""
    if last_success_at is None:
        return "none"
    return "warn" if now - last_success_at > RESTORE_TEST_WARN_AFTER else "ok"


class BackupRunOut(BaseModel):
    """A backup set or a restore test: its status, when it finished and its age in seconds (both null while it
    runs)."""

    id: int
    status: BackupStatus
    finished_at: UtcDatetime | None
    age_s: float | None

    @classmethod
    def of(cls, row: Row[Any], now: datetime) -> "BackupRunOut":
        finished_at: datetime | None = row.finished_at
        age_s = None if finished_at is None else (now - finished_at).total_seconds()
        return cls(id=row.id, status=row.status, finished_at=finished_at, age_s=age_s)


class ManifestShipmentOut(BaseModel):
    """A shipment of deletion manifests to the offsite repository, and how many manifests it shipped."""

    id: int
    shipped_at: UtcDatetime
    manifests: int


class BackupStateOut(BaseModel):
    """The backups, the offsite manifests and the restore tests. Ages are seconds at the time of the request.

    - `latest_set`: the newest backup set, whatever its status; `null` before the first.
    - `backup_age_state`: the age of the newest succeeded set: `warn` past 36 hours, `critical` past 72;
      before the first one, `grace` until 48 hours after install, then measured from install.
    - `latest_shipment`: the newest manifest shipment and the number of manifests it shipped.
    - `unshipped_manifests`: the deletion manifests completed and not shipped yet;
      `oldest_unshipped_completed_at` is when the oldest of them completed.
    - `latest_restore_test`: the newest restore test, whatever its status.
    - `restore_test_age_state`: the age of the newest succeeded restore test: `warn` past 35 days, `none`
      without one.
    """

    latest_set: BackupRunOut | None
    backup_age_state: BackupAgeState
    latest_shipment: ManifestShipmentOut | None
    unshipped_manifests: int
    oldest_unshipped_completed_at: UtcDatetime | None
    latest_restore_test: BackupRunOut | None
    restore_test_age_state: RestoreTestAgeState


async def backup_state(conn: AsyncConnection, *, now: datetime) -> BackupStateOut:
    """The health's backups section at `now` (module docstring), read without a lock, in every env."""
    age = await read_backup_age(conn)
    figures = (await conn.execute(_SECTION_FIGURES)).one()
    latest_set = (await conn.execute(_LATEST_SET)).one_or_none()
    shipment = (await conn.execute(_LATEST_SHIPMENT)).one_or_none()
    restore_test = (await conn.execute(_LATEST_RESTORE_TEST)).one_or_none()
    return BackupStateOut(
        latest_set=None if latest_set is None else BackupRunOut.of(latest_set, now),
        backup_age_state=backup_age_state(
            now, last_success_at=age.last_success_at, installed_at=age.installed_at
        ),
        latest_shipment=(
            None
            if shipment is None
            else ManifestShipmentOut(
                id=shipment.id, shipped_at=shipment.shipped_at, manifests=int(shipment.manifests)
            )
        ),
        unshipped_manifests=int(figures.unshipped),
        oldest_unshipped_completed_at=figures.oldest_unshipped_at,
        latest_restore_test=None if restore_test is None else BackupRunOut.of(restore_test, now),
        restore_test_age_state=restore_test_age_state(now, figures.last_restore_test_at),
    )


async def _judge_failures(conn: AsyncConnection) -> int:
    """Each failure alert of `_FAILURES` opened for its failures no success follows, and every other open one
    resolved; the alerts opened or resolved."""
    changed = 0
    for failure in _FAILURES:
        failing = [int(row_id) for row_id in (await conn.execute(failure.failing)).scalars()]
        for row_id in failing:
            opened = await open_alert(
                conn,
                code=failure.code,
                severity=CATALOGUE[failure.code].severity,
                subject=f"{failure.prefix}:{row_id}",
                message=f"{failure.noun} {row_id} failed",
                context={failure.context_key: row_id},
            )
            changed += opened is not None
        kept = {f"{failure.prefix}:{row_id}" for row_id in failing}
        for subject in await open_subjects(conn, code=failure.code):
            if subject not in kept:
                changed += await resolve_alert(conn, code=failure.code, subject=subject)
    return changed


async def _judge_staleness(conn: AsyncConnection, now: datetime, age: BackupAge) -> int:
    """`backup.stale` raised, escalated or resolved by `backup_age_state` at `now`; 1 when it wrote, else
    0."""
    state = backup_age_state(now, last_success_at=age.last_success_at, installed_at=age.installed_at)
    if state == "ok":
        return int(await resolve_alert(conn, code=BACKUP_STALE_ALERT, subject=BACKUP_STALE_SUBJECT))
    reference = age.last_success_at or age.installed_at
    if state == "grace" or reference is None:
        return 0
    severity: Severity = "critical" if state == "critical" else "warn"
    past = BACKUP_CRITICAL_AFTER if severity == "critical" else BACKUP_WARN_AFTER
    await raise_alert(
        conn,
        code=BACKUP_STALE_ALERT,
        severity=severity,
        subject=BACKUP_STALE_SUBJECT,
        message=f"no backup set succeeded in the last {past // timedelta(hours=1)} h",
        context={"age_s": int((now - reference).total_seconds())},
    )
    return 1


@reconciler("backup_health", "heavy")
async def backup_health(ctx: ReconcileContext) -> int:
    """The backup alerts and the backup age gauge at `ctx.now` (module docstring): nothing in `dev`; else, in
    one transaction fenced by `ctx.epoch`, the gauge set and the failure and staleness alerts judged. The
    alerts opened, raised or resolved."""
    if ctx.cfg is not None and ctx.cfg.env == "dev":
        return 0
    async with ctx.engine.begin() as conn:
        await fence_epoch(conn, ctx.epoch)
        age = await read_backup_age(conn)
        BACKUP_AGE.set(
            backup_age_s(ctx.now, last_success_at=age.last_success_at, installed_at=age.installed_at)
        )
        return await _judge_failures(conn) + await _judge_staleness(conn, ctx.now, age)
