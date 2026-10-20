"""ATS snapshot effects (spec §8.11, §7.16.2, §5.3.5): membership, absence counting, the mass disappearance
with its follow-ups, confirmation and acknowledgement.

**`apply_snapshot`** applies one recorded snapshot (`coverage.record_ats_snapshot` wrote it `complete` or
`incomplete`, with its members) exactly once: a snapshot with `applied_at` set is left as it is. Lock order
(§3.3.3, Ruling T19-a): the job rows of the instance's `active`/`unknown` postings and of the members'
postings, with their group co-members, in one ordered `FOR UPDATE` statement (`rules.lock_posting_jobs`);
then the caller's `guard` (the node's `ledger.lock_run_owner`, a reconciler's `fence_epoch`); then those
posting rows, the snapshot row and the instance row `FOR UPDATE`; then the writes; `ranking_state` last.

- **Members** (`ats_snapshot_members`, so an item that failed normalisation still counts as present) →
  `rules.apply_membership(taken_at)`: the absence reset, and `stale | unknown | closed → active` when
  `taken_at > availability_changed_at`, changed at `taken_at` (carry 3). A member without a posting (an item
  that was rejected) is ignored, and no sighting is counted (carry 4).
- The snapshot's **absent set** `A` is the instance's `active`/`unknown` postings that are not members: the
  set that is counted, closed and frozen.
- **Structurally incomplete** (`incomplete`): `active → unknown` for `A`, nothing counted.
- **Mass classification** of a structurally complete snapshot (Ruling T19-b and its addendum, extended by
  Ruling FR-d). The baseline `B` is `source_instances.last_snapshot_active_count`, §8.11's number of active
  postings, which only a structurally complete snapshot (or the end of an event) sets, never an incomplete
  one, and which is frozen while an event is open. It is compared with the **disappeared** part `D` of `A`:
  the "previously active" postings of §5.3.5, those active at the snapshot `B` was taken at. That is the
  instance's last applied structurally complete snapshot taken before the open event's opening snapshot, or
  before this one when no event is open (its `taken_at`, `baseline_at`), so `D` is the postings of `A` that
  are `active` and those `unknown` since `baseline_at` (`availability_changed_at >` it): an incomplete read
  after the baseline (FR-d) or the open event's own snapshots (the addendum) made them unknown, and an
  incomplete read never hides them from the next complete snapshot. A posting already `unknown` at that
  snapshot (an incomplete read before it, or an event that cleared at it) is absent again, no new
  disappearance; without any such snapshot every `unknown` one counts. Mass: `|D| ≥ 5` and `|D| > B / 2`
  (no classification without a baseline). Then `active → unknown` for `A`, nothing counted, and:
  - no open event: the event **opens**: status `mass_disappearance`, `mass_event_snapshot_id` := the
    snapshot, `A` written to `ats_snapshot_absences` (the event's frozen absent set, plan Deviation 22),
    `ats.mass_disappearance` (warn, subject `instance:<id>`) raised; the baseline stays frozen;
  - an open event: a **follow-up**: status `mass_disappearance`, `event_snapshot_id` := the opening one;
    then the confirmation check (below).
- **Otherwise** (`complete`): the ordinary rule for `A`: `absent_snapshots := LEAST(absent_snapshots + 1,
  2)`, `first_absent_at := COALESCE(first_absent_at, taken_at)`, once per snapshot
  (`last_absent_snapshot_id`); then `active | unknown → closed('ats_absent')` when `absent_snapshots ≥ 2` and
  `taken_at - first_absent_at ≥ 24 h`. An open event **clears**: its alert is resolved. The baseline is reset
  to the instance's `active` count and `mass_event_snapshot_id := NULL`.
- The snapshot row records its status, `event_snapshot_id`, `baseline_active_count` (`B` before it),
  `absent_count` (`|D|`, §7.7: "previously active postings absent from it") and `applied_at := now` (the
  caller's clock).

**Confirmation** (§8.11, over the applied structurally complete snapshots taken after the opening one): `S2`
is the latest of them and `S1` the latest at least 24 h before `S2`; when `S1` exists the event is confirmed.
The postings of the frozen set that are still `unknown` and are members of no applied snapshot (complete or
not: a member of any snapshot is present) taken from `S1` to `S2` go `unknown →
closed('mass_disappearance_confirmed')`: an interpretation of "the postings absent from the opening snapshot
and from both confirming snapshots", which closes only a posting absent throughout the window, and one that
left `unknown` (it reappeared, or an item sighted it) is not closed. The baseline is reset to the `active`
count, `mass_event_snapshot_id := NULL` and the alert resolved. `apply_snapshot` checks it after every
follow-up (the "after every ATS snapshot" trigger of §17.4); the reconciler `ats_mass_events` (cadence
`tick`) runs `confirm_event` for every open event, in the same lock order with the epoch fence after the job
rows.

**`acknowledge`** is the user's confirmation (`POST /api/ats-snapshots/{id}/acknowledge`, §14.2; M4): the
same closure of the frozen set's `unknown` postings at once, without the membership condition; every snapshot
of the event gets `acknowledged_at`. An event that is no longer open closes nothing.

Every touched job's availability is recomputed (`rules.recompute_job_availability`) and the ranking bumped
once, last, when one changed. Closures and `unknown` transitions are written through `transition()`; a closure
keeps the database's `now()` as `availability_changed_at`, an `unknown` gets the snapshot's `taken_at`, the
time of the evidence, so that the revival rule's "member of a later snapshot" compares in one time domain
(carry 3), but only when it is later than the change time before the transition (M-5): out-of-order evidence
never moves `availability_changed_at` backwards. Applying the snapshots of one instance in the order they were
taken is assumed (the node applies each run's own).

**`apply_run_snapshots`** is node 6's snapshot pass (§9.2): every snapshot of the run with `applied_at IS
NULL`, in id order, one transaction each under the run's owner row; its counters `SNAPSHOT_COUNTERS`.
"""

from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from jobhunter.availability import rules
from jobhunter.db import ranking
from jobhunter.db.guards import Guard
from jobhunter.db.sql import q
from jobhunter.db.transitions import CLEAR_REASON, transition
from jobhunter.errors import FencedOut
from jobhunter.observability.alerts import raise_alert, resolve_alert
from jobhunter.observability.logging import get_logger
from jobhunter.quota import ledger
from jobhunter.worker.epoch import fence_epoch
from jobhunter.worker.reconcilers import ReconcileContext, reconciler
from jobhunter.worker.runs import RunOwner

MASS_ALERT: Final = "ats.mass_disappearance"
ATS_ABSENT: Final = "ats_absent"
MASS_CONFIRMED: Final = "mass_disappearance_confirmed"
STRUCTURALLY_COMPLETE: Final = ("complete", "mass_disappearance")
CLOSE_SPAN: Final = timedelta(hours=24)  # taken_at - first_absent_at before an absent posting closes
CONFIRM_SPAN: Final = timedelta(hours=24)  # between the two confirming snapshots of a mass disappearance
MASS_MIN_ABSENT: Final = 5  # absent postings a mass disappearance needs, beside more than half the baseline
# Node 6's counters of the snapshot pass.
SNAPSHOT_COUNTERS: Final = (
    "availability_closed",
    "availability_unknown",
    "availability_revived",
    "mass_events_opened",
)

_SNAPSHOT: Final = q("SELECT source_instance_id, applied_at FROM ats_snapshots WHERE id = :s")
# The postings a snapshot may touch: the instance's `active`/`unknown` ones and the members' postings.
_CANDIDATES: Final = q(
    "SELECT p.id FROM postings p WHERE p.source_instance_id = :i AND (p.availability IN ('active','unknown') "
    "OR p.native_id IN (SELECT m.native_id FROM ats_snapshot_members m WHERE m.snapshot_id = :s))"
)
_LOCK_CANDIDATES: Final = q(
    "SELECT p.id, p.job_id, p.availability, p.availability_changed_at, p.absent_snapshots, "
    "EXISTS (SELECT 1 FROM ats_snapshot_members m WHERE m.snapshot_id = :s AND m.native_id = p.native_id) "
    "AS member FROM postings p "
    "WHERE p.id = ANY(CAST(:ids AS bigint[])) AND p.job_id = ANY(CAST(:jobs AS bigint[])) "
    "ORDER BY p.id FOR UPDATE OF p"
)
_LOCK_HELD: Final = q(
    "SELECT id, job_id FROM postings WHERE id = ANY(CAST(:ids AS bigint[])) "
    "AND job_id = ANY(CAST(:jobs AS bigint[])) ORDER BY id FOR UPDATE"
)
_LOCK_SNAPSHOT: Final = q("SELECT status, taken_at, applied_at FROM ats_snapshots WHERE id = :s FOR UPDATE")
# The instance's baseline and open event, with the event's opening time.
_LOCK_INSTANCE: Final = q(
    "SELECT i.last_snapshot_active_count, i.mass_event_snapshot_id, (SELECT o.taken_at FROM ats_snapshots o "
    "WHERE o.id = i.mass_event_snapshot_id) AS opened_at FROM source_instances i WHERE i.id = :i "
    "FOR UPDATE OF i"
)
# When the baseline was taken (Ruling FR-d): the `taken_at` of the instance's last applied structurally
# complete snapshot before :before (the open event's opening snapshot, else the snapshot being applied).
_BASELINE_AT: Final = q(
    "SELECT max(taken_at) FROM ats_snapshots WHERE source_instance_id = :i "
    "AND status IN ('complete','mass_disappearance') AND applied_at IS NOT NULL "
    "AND taken_at < CAST(:before AS timestamptz)"
)
# The change time of the postings `transition()` just made `unknown` (at `now()`): the snapshot's `taken_at`
# when the change time before the transition (`prev`) is earlier, and never earlier than `prev` (M-5).
_UNKNOWN_AT: Final = q(
    "UPDATE postings AS p SET availability_changed_at = CASE WHEN x.prev < CAST(:taken_at AS timestamptz) "
    "THEN CAST(:taken_at AS timestamptz) ELSE greatest(p.availability_changed_at, x.prev) END "
    "FROM unnest(CAST(:ids AS bigint[]), CAST(:prev AS timestamptz[])) AS x(id, prev) "
    "WHERE p.id = x.id AND p.availability = 'unknown'"
)
_COUNT_ABSENCE: Final = q(
    "UPDATE postings SET absent_snapshots = least(absent_snapshots + 1, 2), "
    "first_absent_at = coalesce(first_absent_at, CAST(:taken_at AS timestamptz)), "
    "last_absent_snapshot_id = :s "
    "WHERE id = ANY(CAST(:ids AS bigint[])) AND last_absent_snapshot_id IS DISTINCT FROM CAST(:s AS bigint) "
    "RETURNING id"
)
_DUE_TO_CLOSE: Final = q(
    "SELECT id, job_id FROM postings WHERE id = ANY(CAST(:ids AS bigint[])) "
    "AND availability IN ('active','unknown') AND absent_snapshots >= 2 "
    "AND CAST(:taken_at AS timestamptz) - first_absent_at >= CAST(:span AS interval) ORDER BY id"
)
_FREEZE: Final = q(
    "INSERT INTO ats_snapshot_absences (snapshot_id, posting_id) "
    "SELECT :s, unnest(CAST(:ids AS bigint[])) ON CONFLICT DO NOTHING"
)
_CLASSIFY: Final = q(
    "UPDATE ats_snapshots SET status = :status, event_snapshot_id = :event, "
    "baseline_active_count = :baseline, absent_count = :absent, applied_at = :now WHERE id = :s"
)
_OPEN_EVENT: Final = q("UPDATE source_instances SET mass_event_snapshot_id = :s WHERE id = :i")
# The baseline after a structurally complete snapshot outside an event (or the end of one): the active count.
_RESET_BASELINE: Final = q(
    "UPDATE source_instances SET last_snapshot_active_count = (SELECT count(*) FROM postings "
    "WHERE source_instance_id = :i AND availability = 'active'), mass_event_snapshot_id = NULL WHERE id = :i "
    "RETURNING last_snapshot_active_count"
)
# The confirmation window: S2, the latest applied structurally complete snapshot after the opening one, and
# S1, the latest of them at least :span before S2 (NULL while there is none).
_WINDOW: Final = q(
    "WITH s AS (SELECT taken_at FROM ats_snapshots WHERE source_instance_id = :i "
    "AND status IN ('complete','mass_disappearance') AND applied_at IS NOT NULL "
    "AND taken_at > (SELECT o.taken_at FROM ats_snapshots o WHERE o.id = :event)), "
    "last AS (SELECT max(taken_at) AS at FROM s) "
    "SELECT (SELECT max(s.taken_at) FROM s WHERE s.taken_at <= last.at - CAST(:span AS interval)) AS first, "
    "last.at AS last FROM last"
)
# The frozen absent set's postings still `unknown`, among :ids.
_FROZEN_UNKNOWN: Final = (
    "SELECT p.id, p.job_id FROM postings p WHERE p.id = ANY(CAST(:ids AS bigint[])) AND p.availability = "
    "'unknown' AND p.id IN (SELECT a.posting_id FROM ats_snapshot_absences a WHERE a.snapshot_id = :event)"
)
_ACKNOWLEDGED_DUE: Final = q(f"{_FROZEN_UNKNOWN} ORDER BY p.id")
# ... and a member of no applied snapshot (complete or not: a member is present, §8.11) of the window.
_CONFIRMED_DUE: Final = q(
    f"{_FROZEN_UNKNOWN} AND NOT EXISTS (SELECT 1 FROM ats_snapshots s "  # noqa: S608
    "JOIN ats_snapshot_members m ON m.snapshot_id = s.id WHERE s.source_instance_id = p.source_instance_id "
    "AND s.applied_at IS NOT NULL AND s.taken_at BETWEEN :first AND :last AND m.native_id = p.native_id) "
    "ORDER BY p.id"
)
_FROZEN_CANDIDATES: Final = q(
    "SELECT p.id FROM ats_snapshot_absences a JOIN postings p ON p.id = a.posting_id "
    "WHERE a.snapshot_id = :event AND p.availability = 'unknown'"
)
_EVENT_OF: Final = q("SELECT mass_event_snapshot_id FROM source_instances WHERE id = :i")
_TO_ACKNOWLEDGE: Final = q(
    "SELECT source_instance_id, status, event_snapshot_id FROM ats_snapshots WHERE id = :s"
)
_ACKNOWLEDGE: Final = q(
    "UPDATE ats_snapshots SET acknowledged_at = :now WHERE (id = :event OR event_snapshot_id = :event) "
    "AND acknowledged_at IS NULL"
)
_UNAPPLIED_OF_RUN: Final = q(
    "SELECT id FROM ats_snapshots WHERE run_id = :run AND applied_at IS NULL ORDER BY id"
)
_OPEN_EVENTS: Final = q(
    "SELECT id FROM source_instances WHERE mass_event_snapshot_id IS NOT NULL ORDER BY id"
)

log = get_logger("jobhunter.availability.snapshots")


@dataclass(frozen=True)
class SnapshotEffect:
    """What applying a snapshot did (all zero for a snapshot applied already)."""

    revived: int = 0
    absent_marked: int = 0
    closed: int = 0
    unknown: int = 0
    mass_opened: bool = False
    mass_followup: bool = False
    mass_cleared: bool = False
    mass_confirmed: bool = False


def _subject(instance_id: int) -> str:
    return f"instance:{instance_id}"


def _is_mass(disappeared: int, baseline: int | None) -> bool:
    """More than half of the baseline, and at least `MASS_MIN_ABSENT` postings, disappeared (§8.11)."""
    return baseline is not None and disappeared >= MASS_MIN_ABSENT and 2 * disappeared > baseline


def _disappeared(row: Row[tuple[object, ...]], baseline_at: datetime | None) -> bool:
    """An absent posting counts against the baseline (Rulings T19-b and FR-d): it is `active`, or `unknown`
    since the snapshot the baseline was taken at (`baseline_at`, module docstring), every `unknown` one when
    there is no such snapshot."""
    if row.availability == "active":
        return True
    return row.availability == "unknown" and (
        baseline_at is None or row.availability_changed_at > baseline_at
    )


def _ids(rows: Iterable[Row[tuple[object, ...]]]) -> list[int]:
    return [int(row.id) for row in rows]


async def _close(
    conn: AsyncConnection, rows: Sequence[Row[tuple[object, ...]]], from_states: Sequence[str], reason: str
) -> set[int]:
    """Close each posting of `rows` from one of `from_states` with `reason`; the jobs of the postings."""
    for row in rows:
        await transition(conn, "postings", int(row.id), from_states, "closed", reason=reason)
    return {int(row.job_id) for row in rows}


async def _mark_unknown(
    conn: AsyncConnection, rows: Sequence[Row[tuple[object, ...]]], taken_at: datetime
) -> set[int]:
    """`active → unknown` for each posting of `rows`, changed at the snapshot's `taken_at` unless that is not
    later than its change time before (`_UNKNOWN_AT`); their jobs."""
    for row in rows:
        await transition(conn, "postings", int(row.id), ["active"], "unknown", reason=CLEAR_REASON)
    if rows:
        prev = [row.availability_changed_at for row in rows]
        await conn.execute(_UNKNOWN_AT, {"ids": _ids(rows), "prev": prev, "taken_at": taken_at})
    return {int(row.job_id) for row in rows}


async def _count_absence(
    conn: AsyncConnection, snapshot_id: int, absent: Sequence[Row[tuple[object, ...]]], taken_at: datetime
) -> tuple[int, Sequence[Row[tuple[object, ...]]]]:
    """The ordinary rule for the absent set of a complete snapshot: `(counted, the postings to close)`."""
    if not absent:
        return 0, []
    params = {"ids": _ids(absent), "s": snapshot_id, "taken_at": taken_at}
    counted = len((await conn.execute(_COUNT_ABSENCE, params)).all())
    due = (await conn.execute(_DUE_TO_CLOSE, {**params, "span": CLOSE_SPAN})).all()
    return counted, due


async def _confirmed_due(
    conn: AsyncConnection, instance_id: int, event: int, posting_ids: Collection[int]
) -> Sequence[Row[tuple[object, ...]]] | None:
    """The postings the confirmation of the open event closes, among `posting_ids` (held); `None` while the
    event is not confirmed (module docstring)."""
    window = (await conn.execute(_WINDOW, {"i": instance_id, "event": event, "span": CONFIRM_SPAN})).one()
    if window.first is None:
        return None
    params = {"ids": sorted(posting_ids), "event": event, "first": window.first, "last": window.last}
    return (await conn.execute(_CONFIRMED_DUE, params)).all()


async def _end_event(conn: AsyncConnection, instance_id: int, event: int, *, how: str) -> int:
    """The event ends: the baseline reset to the active count, `mass_event_snapshot_id := NULL`, the alert
    resolved; returns the new baseline."""
    baseline = int((await conn.execute(_RESET_BASELINE, {"i": instance_id})).scalar_one())
    await resolve_alert(conn, code=MASS_ALERT, subject=_subject(instance_id))
    log.info(
        "mass_disappearance_ended",
        source_instance_id=instance_id,
        event_snapshot_id=event,
        how=how,
        baseline=baseline,
    )
    return baseline


async def _recompute(conn: AsyncConnection, jobs: Iterable[int]) -> None:
    """Every touched job's availability, then, last, the ranking bumped once when one changed."""
    changed = False
    for job_id in sorted(set(jobs)):
        changed |= await rules.recompute_job_availability(conn, job_id)
    if changed:
        await ranking.bump(conn)


async def apply_snapshot(
    conn: AsyncConnection, snapshot_id: int, *, now: datetime, guard: Guard
) -> SnapshotEffect:
    """Apply the snapshot's availability effects (module docstring) in the caller's transaction, once.

    `guard` runs after the job rows are locked and before anything is written; it raises `FencedOut` (or
    another error) to stop, having written nothing. Raises `LookupError` for a snapshot that does not exist.
    """
    found = (await conn.execute(_SNAPSHOT, {"s": snapshot_id})).first()
    if found is None:
        raise LookupError(f"ats snapshot {snapshot_id} does not exist")
    if found.applied_at is not None:
        return SnapshotEffect()
    instance_id = int(found.source_instance_id)
    candidates = [
        int(posting_id)
        for posting_id in (await conn.execute(_CANDIDATES, {"i": instance_id, "s": snapshot_id})).scalars()
    ]
    held = await rules.lock_posting_jobs(conn, candidates)
    await guard(conn)
    rows = (
        await conn.execute(_LOCK_CANDIDATES, {"s": snapshot_id, "ids": candidates, "jobs": sorted(held)})
    ).all()
    snap = (await conn.execute(_LOCK_SNAPSHOT, {"s": snapshot_id})).one()
    if snap.applied_at is not None:  # applied by another transaction since the first read
        return SnapshotEffect()
    instance = (await conn.execute(_LOCK_INSTANCE, {"i": instance_id})).one()
    taken_at: datetime = snap.taken_at
    baseline: int | None = instance.last_snapshot_active_count
    event: int | None = instance.mass_event_snapshot_id
    before = taken_at if instance.opened_at is None else instance.opened_at
    baseline_at = (await conn.execute(_BASELINE_AT, {"i": instance_id, "before": before})).scalar()
    absent = [row for row in rows if not row.member and row.availability in ("active", "unknown")]
    disappeared = sum(_disappeared(row, baseline_at) for row in absent)
    complete = snap.status in STRUCTURALLY_COMPLETE
    mass = complete and _is_mass(disappeared, baseline)

    touched: set[int] = set()  # the jobs of the postings whose availability changed
    revived = 0
    for row in rows:
        if row.member and (row.availability != "active" or row.absent_snapshots > 0):
            if await rules.apply_membership(conn, int(row.id), taken_at=taken_at):
                revived += 1
                touched.add(int(row.job_id))

    status, event_of_row, counted = str(snap.status), None, 0
    closing: Sequence[Row[tuple[object, ...]]] = []
    newly_unknown = [row for row in absent if row.availability == "active"] if not complete or mass else []
    touched |= await _mark_unknown(conn, newly_unknown, taken_at)
    if mass:
        status, event_of_row = "mass_disappearance", event
        if event is None:  # it opens the event: its absent set is the event's, frozen
            await conn.execute(_FREEZE, {"s": snapshot_id, "ids": _ids(absent)})
    elif complete:
        counted, closing = await _count_absence(conn, snapshot_id, absent, taken_at)
        touched |= await _close(conn, closing, ("active", "unknown"), ATS_ABSENT)
    classify = {
        "s": snapshot_id,
        "status": status,
        "event": event_of_row,
        "baseline": baseline,
        "absent": disappeared,
        "now": now,
    }
    await conn.execute(_CLASSIFY, classify)

    confirmed: Sequence[Row[tuple[object, ...]]] | None = None
    if mass and event is None:
        await conn.execute(_OPEN_EVENT, {"s": snapshot_id, "i": instance_id})
        await raise_alert(
            conn,
            code=MASS_ALERT,
            severity="warn",
            subject=_subject(instance_id),
            message="most postings of an ATS board are absent from its snapshot",
            context={
                "snapshot_id": snapshot_id,
                "source_instance_id": instance_id,
                "absent": disappeared,
                "baseline": baseline,
            },
        )
        log.warning(
            "mass_disappearance_opened",
            snapshot_id=snapshot_id,
            source_instance_id=instance_id,
            absent=disappeared,
            baseline=baseline,
        )
    elif mass and event is not None:
        confirmed = await _confirmed_due(conn, instance_id, event, _ids(rows))
        if confirmed is not None:
            touched |= await _close(conn, confirmed, ("unknown",), MASS_CONFIRMED)
            await _end_event(conn, instance_id, event, how="confirmed")
    elif complete and event is not None:  # the absent share fell to half or below: the event clears
        await _end_event(conn, instance_id, event, how="cleared")
    elif complete:
        await conn.execute(_RESET_BASELINE, {"i": instance_id})

    effect = SnapshotEffect(
        revived=revived,
        absent_marked=counted,
        closed=len(closing) + (len(confirmed) if confirmed is not None else 0),
        unknown=len(newly_unknown),
        mass_opened=mass and event is None,
        mass_followup=mass and event is not None,
        mass_cleared=complete and not mass and event is not None,
        mass_confirmed=confirmed is not None,
    )
    await _recompute(conn, touched)
    log.info(
        "ats_snapshot_applied",
        snapshot_id=snapshot_id,
        source_instance_id=instance_id,
        status=status,
        revived=effect.revived,
        absent_marked=effect.absent_marked,
        closed=effect.closed,
        unknown=effect.unknown,
    )
    return effect


async def confirm_event(conn: AsyncConnection, instance_id: int, *, guard: Guard) -> int | None:
    """The confirmation check of the instance's open mass disappearance (module docstring) in the caller's
    transaction: the postings closed when it was confirmed, `None` when no event is open or it is not
    confirmed. The lock order of `apply_snapshot`, `guard` after the job rows."""
    event = (await conn.execute(_EVENT_OF, {"i": instance_id})).scalar()
    if event is None:
        return None
    event = int(event)
    window = (await conn.execute(_WINDOW, {"i": instance_id, "event": event, "span": CONFIRM_SPAN})).one()
    if window.first is None:  # nothing to confirm yet: nothing locked, nothing written
        return None
    candidates = [int(p) for p in (await conn.execute(_FROZEN_CANDIDATES, {"event": event})).scalars()]
    held = await rules.lock_posting_jobs(conn, candidates)
    await guard(conn)
    rows = (await conn.execute(_LOCK_HELD, {"ids": candidates, "jobs": sorted(held)})).all()
    instance = (await conn.execute(_LOCK_INSTANCE, {"i": instance_id})).one()
    if instance.mass_event_snapshot_id != event:  # confirmed, cleared or acknowledged meanwhile
        return None
    due = await _confirmed_due(conn, instance_id, event, _ids(rows))
    if due is None:
        return None
    touched = await _close(conn, due, ("unknown",), MASS_CONFIRMED)
    await _end_event(conn, instance_id, event, how="confirmed")
    await _recompute(conn, touched)
    return len(due)


async def acknowledge(conn: AsyncConnection, snapshot_id: int, *, now: datetime) -> int:
    """The user's acknowledgement of the mass disappearance of `snapshot_id` (its opening snapshot or a
    follow-up; module docstring) in the caller's transaction: the frozen set's `unknown` postings close at
    once. Returns how many closed (0 for an event that is no longer open). Raises `LookupError` for a snapshot
    that does not exist and `ValueError` for one that is no mass disappearance."""
    found = (await conn.execute(_TO_ACKNOWLEDGE, {"s": snapshot_id})).first()
    if found is None:
        raise LookupError(f"ats snapshot {snapshot_id} does not exist")
    if found.status != "mass_disappearance":
        raise ValueError(f"ats snapshot {snapshot_id} is {found.status}, not a mass disappearance")
    event = snapshot_id if found.event_snapshot_id is None else int(found.event_snapshot_id)
    instance_id = int(found.source_instance_id)
    is_open = (await conn.execute(_EVENT_OF, {"i": instance_id})).scalar() == event
    candidates = (
        [int(p) for p in (await conn.execute(_FROZEN_CANDIDATES, {"event": event})).scalars()]
        if is_open
        else []
    )
    held = await rules.lock_posting_jobs(conn, candidates)
    rows = (await conn.execute(_LOCK_HELD, {"ids": candidates, "jobs": sorted(held)})).all()
    await conn.execute(_ACKNOWLEDGE, {"event": event, "now": now})
    instance = (await conn.execute(_LOCK_INSTANCE, {"i": instance_id})).one()
    if instance.mass_event_snapshot_id != event:
        log.info(
            "mass_disappearance_acknowledged",
            event_snapshot_id=event,
            source_instance_id=instance_id,
            closed=0,
        )
        return 0
    due = (await conn.execute(_ACKNOWLEDGED_DUE, {"ids": _ids(rows), "event": event})).all()
    touched = await _close(conn, due, ("unknown",), MASS_CONFIRMED)
    await _end_event(conn, instance_id, event, how="acknowledged")
    await _recompute(conn, touched)
    log.info(
        "mass_disappearance_acknowledged",
        event_snapshot_id=event,
        source_instance_id=instance_id,
        closed=len(due),
    )
    return len(due)


async def apply_run_snapshots(
    engine: AsyncEngine, owner: RunOwner, *, clock: Callable[[], datetime]
) -> dict[str, int]:
    """Node 6's snapshot pass: apply every unapplied snapshot of `owner`'s run, in id order, one transaction
    each, guarded by the run's owner row after the job rows (raises `FencedOut` for a superseded owner).
    Returns `SNAPSHOT_COUNTERS`. `clock` gives each snapshot's `applied_at`."""
    async with engine.connect() as conn:
        pending = [int(s) for s in (await conn.execute(_UNAPPLIED_OF_RUN, {"run": owner.run_id})).scalars()]

    async def owned(conn: AsyncConnection) -> None:
        await ledger.lock_run_owner(conn, owner)

    counts = dict.fromkeys(SNAPSHOT_COUNTERS, 0)
    for snapshot_id in pending:
        async with engine.begin() as conn:
            effect = await apply_snapshot(conn, snapshot_id, now=clock(), guard=owned)
        counts["availability_closed"] += effect.closed
        counts["availability_unknown"] += effect.unknown
        counts["availability_revived"] += effect.revived
        counts["mass_events_opened"] += effect.mass_opened
    if pending:
        log.info("run_snapshots_applied", run_id=owner.run_id, snapshots=len(pending), **counts)
    return counts


@reconciler("ats_mass_events", "tick")
async def ats_mass_events(ctx: ReconcileContext) -> int:
    """§17.4 `ats_mass_events`: the confirmation check of every open mass disappearance, one transaction per
    instance, fenced by the worker epoch after the job rows (Ruling T19-a). Returns how many events it
    confirmed; a superseded worker stops at its first fenced transaction, which wrote nothing."""
    async with ctx.engine.connect() as conn:
        instances = [int(i) for i in (await conn.execute(_OPEN_EVENTS)).scalars()]

    async def fenced(conn: AsyncConnection) -> None:
        await fence_epoch(conn, ctx.epoch)

    confirmed = 0
    for instance_id in instances:
        try:
            async with ctx.engine.begin() as conn:
                if await confirm_event(conn, instance_id, guard=fenced) is not None:
                    confirmed += 1
        except FencedOut:
            break  # `fence_epoch` logged it
    return confirmed
