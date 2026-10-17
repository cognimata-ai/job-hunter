"""The scheduling tick (spec §3.4): record every due UTC slot exactly once and queue the latest collection.

Slots are `anchor_utc + k·interval_s` in elapsed seconds, so a DST change never shifts or doubles one.
`compute_due_slots` is the pure arithmetic. `run_tick` applies it inside the caller's transaction, under the
global lock order (`active_config` before `schedule_state`, §3.3.3), and decides only from the `now` it is
given, never from the database's clock:

- every due slot but the latest is recorded as a `missed` run, in one statement;
- the latest slot becomes a `queued` collection (`trigger='schedule'`, or `'catch_up'` when it is more than
  `CATCH_UP_AFTER` old), whose `run_collect` job is deferred in the same transaction;
- when a collection is already active (`runs_one_active_collect`), that slot is recorded as
  `skipped_overlap` instead; when the slot already has a row (`runs_slot_uq`), nothing is written.

There is no check before an insert: the two unique indexes decide, and a violation is an outcome, never an
error loop. `insert_active_collect` is the one place that inserts an active collection, shared with the
manual run (`request_manual_run`).

`missed` and `skipped_overlap` are terminal statuses: the tick task counts the rows a tick inserted with them
in `RUNS` once its transaction has committed (`observe_tick`, ruling T12-e). They are never timed: such a run
never started.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Literal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.seed import latest_slot_at_or_before
from jobhunter.db.sql import is_unique_violation, pg_error, q
from jobhunter.observability.logging import get_logger
from jobhunter.observability.metrics import RUNS
from jobhunter.types import get_bool
from jobhunter.worker.tasks import Deferrer

CATCH_UP_AFTER: Final = timedelta(minutes=5)

ACTIVE_COLLECT_INDEX: Final = "runs_one_active_collect"
SLOT_INDEX: Final = "runs_slot_uq"

log = get_logger("jobhunter.worker.tick")

_ACTIVE_CONFIG = q("SELECT settings_version_id, scope_version_id FROM active_config WHERE id = 1 FOR SHARE")
_SCHEDULE_STATE = q(
    "SELECT anchor_utc, interval_s, enabled, last_evaluated_slot FROM schedule_state WHERE id = 1 FOR UPDATE"
)
_SCHEDULE_SETTINGS = q("SELECT payload -> 'schedule' FROM settings_versions WHERE id = :id")
_STORE_CURSOR = q("UPDATE schedule_state SET last_evaluated_slot = :slot, updated_at = :now WHERE id = 1")

# The conflict target is the partial unique index `runs_slot_uq`, exactly as its DDL defines it (§7.6).
_INSERT_MISSED = q(
    "INSERT INTO runs (trigger, scheduled_slot, status, include_paid, settings_version_id, scope_version_id) "
    "SELECT 'schedule', u.slot, 'missed', :include_paid, :settings_version_id, :scope_version_id "
    "FROM unnest(CAST(:slots AS timestamptz[])) AS u(slot) ORDER BY u.slot "
    "ON CONFLICT (kind, scheduled_slot) WHERE scheduled_slot IS NOT NULL DO NOTHING"
)
_INSERT_QUEUED = q(
    "INSERT INTO runs (trigger, scheduled_slot, status, include_paid, settings_version_id, scope_version_id) "
    "VALUES (:trigger, :slot, 'queued', :include_paid, :settings_version_id, :scope_version_id) RETURNING id"
)
_INSERT_SKIPPED_OVERLAP = q(
    "INSERT INTO runs (trigger, scheduled_slot, status, include_paid, settings_version_id, scope_version_id) "
    "VALUES (:trigger, :slot, 'skipped_overlap', :include_paid, :settings_version_id, :scope_version_id)"
)


@dataclass(frozen=True)
class TickReport:
    """What a tick did.

    `missed` counts the `missed` rows this tick inserted. `queued_run_id` is the collection it queued, if
    any. `skipped_overlap` is true when it inserted a `skipped_overlap` row. `cursor` is the stored
    `last_evaluated_slot` after the tick.
    """

    missed: int
    queued_run_id: int | None
    skipped_overlap: bool
    cursor: datetime | None


def observe_tick(report: TickReport) -> None:
    """Count the terminal runs the tick of `report` inserted in `RUNS`; call it only after the tick's commit.

    Only the rows this tick inserted: `missed` counts them, and `skipped_overlap` is true only for an insert.
    """
    if report.missed:
        RUNS.labels(status="missed").inc(report.missed)
    if report.skipped_overlap:
        RUNS.labels(status="skipped_overlap").inc()


def _utc(value: datetime, name: str) -> datetime:
    """`value` as a UTC datetime.

    Slot arithmetic must run on UTC values: Python adds a `timedelta` to an aware datetime on its wall clock,
    so a value in a zone with DST (what a database session with such a time zone returns) would drift by an
    hour across a change.
    """
    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def compute_due_slots(
    anchor_utc: datetime, interval_s: int, last_evaluated: datetime | None, now: datetime
) -> list[datetime]:
    """The slots in `(last_evaluated, now]`, oldest first, as UTC datetimes.

    With `k(t) = floor((t - anchor) / interval)` in whole seconds (floor division, so times before the
    anchor work), these are `anchor + k·interval` for every `k` in `(k(last_evaluated), k(now)]`. Without a
    cursor only the latest slot at or before `now` is due. A `now` before the cursor yields no slot.
    """
    if interval_s <= 0:
        raise ValueError(f"interval_s must be positive, got {interval_s}")
    anchor = _utc(anchor_utc, "anchor_utc")
    step = timedelta(seconds=interval_s)
    k_now = (_utc(now, "now") - anchor) // step
    if last_evaluated is None:
        return [anchor + k_now * step]
    k_last = (_utc(last_evaluated, "last_evaluated") - anchor) // step
    return [anchor + k * step for k in range(k_last + 1, k_now + 1)]


def trigger_for(now: datetime, slot: datetime) -> Literal["schedule", "catch_up"]:
    """The trigger of the run for `slot` evaluated at `now`: `'schedule'` up to and including
    `CATCH_UP_AFTER` after the slot, `'catch_up'` after that (§3.4)."""
    return "schedule" if _utc(now, "now") - _utc(slot, "slot") <= CATCH_UP_AFTER else "catch_up"


async def insert_active_collect(
    conn: AsyncConnection,
    *,
    trigger: Literal["schedule", "catch_up", "manual"],
    scheduled_slot: datetime | None,
    include_paid: bool,
    settings_version_id: int,
    scope_version_id: int,
    path: Literal["tick", "manual"],
) -> int | Literal["active_conflict", "slot_conflict"]:
    """Insert a `queued` collection and return its id, or the unique violation that stopped it.

    This is the only insert of an active collection (tick and manual run). It runs in a savepoint, so the
    caller's transaction stays usable after a violation. The error is caught outside the savepoint block and
    classified by index: `runs_one_active_collect` means a collection is already active (logged as
    `active_collect_conflict`, returns `"active_conflict"`), `runs_slot_uq` means the slot already has a row
    (returns `"slot_conflict"`). Anything else propagates.
    """
    try:
        async with conn.begin_nested():
            result = await conn.execute(
                _INSERT_QUEUED,
                {
                    "trigger": trigger,
                    "slot": scheduled_slot,
                    "include_paid": include_paid,
                    "settings_version_id": settings_version_id,
                    "scope_version_id": scope_version_id,
                },
            )
            return int(result.scalar_one())
    except IntegrityError as exc:
        if is_unique_violation(exc, ACTIVE_COLLECT_INDEX):
            log.info("active_collect_conflict", path=path, sqlstate=pg_error(exc).sqlstate)
            return "active_conflict"
        if is_unique_violation(exc, SLOT_INDEX):
            return "slot_conflict"
        raise


async def _insert_skipped_overlap(
    conn: AsyncConnection,
    *,
    trigger: Literal["schedule", "catch_up"],
    slot: datetime,
    include_paid: bool,
    settings_version_id: int,
    scope_version_id: int,
) -> bool:
    """Record `slot` as `skipped_overlap`; false when the slot already has a row (a writer was first)."""
    try:
        async with conn.begin_nested():
            await conn.execute(
                _INSERT_SKIPPED_OVERLAP,
                {
                    "trigger": trigger,
                    "slot": slot,
                    "include_paid": include_paid,
                    "settings_version_id": settings_version_id,
                    "scope_version_id": scope_version_id,
                },
            )
    except IntegrityError as exc:
        if not is_unique_violation(exc, SLOT_INDEX):
            raise
        return False
    return True


async def _store_cursor(
    conn: AsyncConnection, stored: datetime | None, cursor: datetime, *, now: datetime
) -> None:
    """Move `last_evaluated_slot` to `cursor`; an unchanged cursor writes nothing."""
    if cursor != stored:
        await conn.execute(_STORE_CURSOR, {"slot": cursor, "now": now})


async def run_tick(conn: AsyncConnection, *, now: datetime, defer: Deferrer) -> TickReport:
    """Evaluate the slots due at `now` on `conn`'s transaction; the caller commits.

    Locks `active_config` (shared) and then `schedule_state` (exclusive), so a tick never overtakes a
    configuration change that holds the former.
    """
    now = _utc(now, "now")
    config = (await conn.execute(_ACTIVE_CONFIG)).one()
    state = (await conn.execute(_SCHEDULE_STATE)).one()
    anchor = _utc(state.anchor_utc, "anchor_utc")
    interval_s: int = state.interval_s
    stored: datetime | None = state.last_evaluated_slot
    cursor = None if stored is None else _utc(stored, "last_evaluated_slot")

    if not state.enabled:
        latest = latest_slot_at_or_before(anchor, interval_s, now)
        cursor = latest if cursor is None else max(cursor, latest)
        await _store_cursor(conn, stored, cursor, now=now)
        return TickReport(missed=0, queued_run_id=None, skipped_overlap=False, cursor=cursor)

    slots = compute_due_slots(anchor, interval_s, cursor, now)
    if not slots:
        return TickReport(missed=0, queued_run_id=None, skipped_overlap=False, cursor=cursor)

    settings = (await conn.execute(_SCHEDULE_SETTINGS, {"id": config.settings_version_id})).scalar_one()
    include_paid = get_bool(settings, "include_paid")
    versions = {
        "settings_version_id": config.settings_version_id,
        "scope_version_id": config.scope_version_id,
    }

    *earlier, latest = slots
    missed = 0
    if earlier:
        missed = (
            await conn.execute(_INSERT_MISSED, {"slots": earlier, "include_paid": include_paid, **versions})
        ).rowcount
    if missed > 0:
        # Slots that passed without a run mean the worker (or the schedule) was down: worth an operator's eye.
        log.warning(
            "slots_missed", count=missed, first_slot=earlier[0].isoformat(), last_slot=earlier[-1].isoformat()
        )

    trigger = trigger_for(now, latest)
    outcome = await insert_active_collect(
        conn, trigger=trigger, scheduled_slot=latest, include_paid=include_paid, path="tick", **versions
    )
    queued_run_id: int | None = None
    skipped_overlap = False
    if isinstance(outcome, int):
        queued_run_id = outcome
        await defer(
            conn,
            "run_collect",
            lock="collect",
            queueing_lock=f"run:{outcome}:1",
            run_id=outcome,
            attempt_no=1,
        )
    elif outcome == "active_conflict":
        skipped_overlap = await _insert_skipped_overlap(
            conn, trigger=trigger, slot=latest, include_paid=include_paid, **versions
        )

    cursor = latest if cursor is None else max(cursor, latest)
    await _store_cursor(conn, stored, cursor, now=now)
    log.info(
        "tick_slots_recorded",
        missed=missed,
        queued_run_id=queued_run_id,
        skipped_overlap=skipped_overlap,
        trigger=trigger,
        slot=latest.isoformat(),
    )
    return TickReport(
        missed=missed, queued_run_id=queued_run_id, skipped_overlap=skipped_overlap, cursor=cursor
    )
