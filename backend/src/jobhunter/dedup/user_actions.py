"""The user's actions across a merge and an unmerge (spec §8.6.1 step 3, §8.6.2 step 4, §7.13; plan M3 Task
22, R1, Rulings T22-c, T22-d, T22-d'): `user_job_actions` coalesced into the into-job and restored from the
merge snapshot, in the caller's transaction (`dedup.merge`, which holds both job rows `FOR UPDATE`; the rows
here are other domain rows, locked `FOR UPDATE` in job id order, `locked_rows`).

**`coalesce_user_actions(conn, *, from_job, into_job)`** (§8.6.1 step 3): each of `seen_at`, `saved_at`,
`applied_at` and `hidden_at` of the into-job becomes the earliest non-null value of the two rows; the notes
are concatenated (`concatenate_notes`: the into-job's note whole, a line break, then the appended part, the
separator line `note_separator(from_job)` and the from-job's note; without an into-job note the separator
leads, without a from-job note nothing is appended). Only the appended part is cut (L3206): beyond
`NOTE_LIMIT` (20,000 characters, the column's CHECK) it is cut so the whole ends at the limit with
`NOTE_CUT_MARKER` (`[…]`), and when no room is left for the marker nothing is appended; the from-job's full
note stays in the snapshot either way. The from-job's row is deleted and the into-job's written with
`updated_at = now()`. Nothing changes when the from-job has no row.

**`restore_user_actions(conn, *, event)`** (§8.6.2 step 4): the from-job's snapshot row restored; on the
into-job a timestamp that equals the from-job's snapshot value (not null) and differs from the into-job's came
only from this merge and is reset (Rulings T22-d, T22-d'). The reset value is the earliest non-null of the
into-job's own value and the from-jobs' snapshot values of the other merges into the same job still in force
(`kind <> 'unmerge'`, not reversed); the own value is the event's into-snapshot value with every earlier merge
peeled off (`_peeled`): walking the earlier merges into the job newest first, whatever their state, one whose
from-job's value is the value and whose into snapshot held another gave it, and the value becomes that other
one, so a merge reversed before this one leaves nothing behind through this event's snapshot. A value changed
since is kept. The note loses exactly the segment this merge appended (`appended_segment`, recomputed from the
event's snapshot: the separator line and the from-job's note, or its cut form) when that segment is still in
it whole (`without_segment`), and is kept as it is when the user edited it (Ruling T22-c), so several merges
into one job unmerge in any order. The into-job's row is deleted when the job had none before any merge (the
earliest merge into it found none), no merge still in force brought a value or a note, and nothing is left on
it; an event whose snapshot holds no actions (recorded before M3) restores none
(`unmerge_without_user_actions_snapshot`, its id alone).

`UserActions` is one row (its note out of the repr) and the form the snapshot holds it in (`to_json`, the
timestamps as ISO 8601, `to_iso`); `MergeEvent` is a merge event as `unmerge` reverses it. Notes are the
user's own text: they reach the snapshot and the row, never a log line.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q
from jobhunter.errors import InvariantViolation
from jobhunter.observability.logging import get_logger
from jobhunter.types import JSONValue, get_list, get_object

# The four timestamps (§7.13) and the note's bound (its CHECK) and cut marker (§8.6.1).
ACTION_TIMESTAMPS: Final = ("seen_at", "saved_at", "applied_at", "hidden_at")
NOTE_LIMIT: Final = 20_000
NOTE_CUT_MARKER: Final = "[…]"

_ACTIONS: Final = q(
    "SELECT job_id, seen_at, saved_at, applied_at, hidden_at, note, updated_at FROM user_job_actions "
    "WHERE job_id = ANY(CAST(:jobs AS bigint[])) ORDER BY job_id FOR UPDATE"
)
_DELETE_ACTIONS: Final = q("DELETE FROM user_job_actions WHERE job_id = :job")
_PUT_ACTIONS: Final = q(
    "INSERT INTO user_job_actions (job_id, seen_at, saved_at, applied_at, hidden_at, note) "
    "VALUES (:job, :seen_at, :saved_at, :applied_at, :hidden_at, :note) ON CONFLICT (job_id) DO UPDATE SET "
    "seen_at = EXCLUDED.seen_at, saved_at = EXCLUDED.saved_at, applied_at = EXCLUDED.applied_at, "
    "hidden_at = EXCLUDED.hidden_at, note = EXCLUDED.note, updated_at = now()"
)
# Every other merge into the job, in any state, with whether it is still in force: its snapshot rows of the
# from-job and the into-job (Rulings T22-d, T22-d').
_OTHER_MERGES_INTO: Final = q(
    "SELECT id, from_job_id, reversed_by_event_id IS NULL AS in_force, "
    "evidence -> 'snapshot' -> 'user_job_actions' AS actions FROM merge_events "
    "WHERE into_job_id = :into AND kind <> 'unmerge' AND id <> :event ORDER BY id"
)

log = get_logger("jobhunter.dedup.user_actions")


@dataclass(frozen=True)
class UserActions:
    """A job's `user_job_actions` row: the four timestamps, the user's note (never logged) and the time of
    the row's last write."""

    job_id: int
    seen_at: datetime | None
    saved_at: datetime | None
    applied_at: datetime | None
    hidden_at: datetime | None
    note: str | None = field(repr=False)  # the user's own text: never in a repr, so never in a traceback
    updated_at: datetime

    def timestamp(self, name: str) -> datetime | None:
        """The timestamp `name` of `ACTION_TIMESTAMPS`."""
        value: datetime | None = getattr(self, name)
        return value

    def to_json(self) -> dict[str, JSONValue]:
        """The row as the merge snapshot holds it (step 1): the timestamps as ISO 8601, the note in full."""
        return {
            "job_id": self.job_id,
            **{name: to_iso(self.timestamp(name)) for name in ACTION_TIMESTAMPS},
            "note": self.note,
            "updated_at": to_iso(self.updated_at),
        }

    @classmethod
    def from_json(cls, value: JSONValue) -> "UserActions":
        """A row of a merge snapshot (`to_json`) read back."""
        row = get_object(value, "")
        job_id, note, updated_at = row["job_id"], row["note"], row["updated_at"]
        if not isinstance(job_id, int) or not (note is None or isinstance(note, str)):
            raise InvariantViolation("a merge snapshot's user_job_actions row is malformed")
        stamps = {name: _from_iso(row[name]) for name in ACTION_TIMESTAMPS}
        written = _from_iso(updated_at)
        if written is None:
            raise InvariantViolation("a merge snapshot's user_job_actions row has no updated_at")
        return cls(job_id, **stamps, note=note, updated_at=written)


@dataclass(frozen=True)
class MergeEvent:
    """A merge event as `unmerge` reverses it: its jobs, postings and snapshot (step 1)."""

    id: int
    kind: str
    from_job_id: int
    into_job_id: int
    posting_ids: tuple[int, ...]
    snapshot: Mapping[str, JSONValue]


def to_iso(value: object) -> JSONValue:
    """A datetime as ISO 8601 for a merge snapshot; anything else as `None`."""
    return value.isoformat() if isinstance(value, datetime) else None


def _from_iso(value: JSONValue) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvariantViolation("a merge snapshot's timestamp is not an ISO 8601 string")
    return datetime.fromisoformat(value)


async def locked_rows(conn: AsyncConnection, job_ids: Iterable[int]) -> dict[int, UserActions]:
    """The jobs' `user_job_actions` rows, locked `FOR UPDATE` in job id order, by job id."""
    rows = await conn.execute(_ACTIONS, {"jobs": sorted(set(job_ids))})
    return {
        int(r.job_id): UserActions(
            int(r.job_id), r.seen_at, r.saved_at, r.applied_at, r.hidden_at, r.note, r.updated_at
        )
        for r in rows
    }


def note_separator(from_job_id: int) -> str:
    """The line between the into-job's note and the merged job's (step 3)."""
    return f"--- merged from job {from_job_id} ---"


def concatenate_notes(into_note: str | None, from_note: str | None, *, from_job_id: int) -> str | None:
    """The into-job's note after the merge of `from_job_id` (step 3, module docstring): the into-job's note
    whole, a line break, then the appended part (the separator line and the from-job's note), cut so the
    whole stays within `NOTE_LIMIT` characters with `NOTE_CUT_MARKER` last; the into-job's note alone when the
    from-job has none, or when no room is left for the marker (the from-job's note stays in the snapshot)."""
    if not from_note:
        return into_note
    appended = f"{note_separator(from_job_id)}\n{from_note}"
    head = f"{into_note}\n" if into_note else ""
    room = NOTE_LIMIT - len(head)
    if len(appended) <= room:
        return head + appended
    if room < len(NOTE_CUT_MARKER):
        return into_note
    return head + appended[: room - len(NOTE_CUT_MARKER)] + NOTE_CUT_MARKER


def appended_segment(into_note: str | None, from_note: str | None, *, from_job_id: int) -> str:
    """The text the merge of `from_job_id` appended to `into_note` (`concatenate_notes`), its leading line
    break included when the into-job had a note; empty when it appended nothing."""
    merged = concatenate_notes(into_note, from_note, from_job_id=from_job_id) or ""
    return merged[len(into_note or "") :]


def without_segment(note: str | None, segment: str) -> str | None:
    """`note` without the `segment` a merge appended (Ruling T22-c), found whole: followed by the note's end
    or a line break (the next merge's segment, or a line the user added after it). The first such
    occurrence goes with the line break that joined it to the text before it or, at the start of the note
    (the text before it gone with an earlier unmerge), with the one that joined it to the text after it. A
    note in which the segment is no longer present so (the user edited it) is kept as it is."""
    if not segment or note is None:
        return note
    core = segment.removeprefix("\n")
    joined = f"\n{core}"
    at = note.find(joined)
    while at != -1:
        end = at + len(joined)
        if end == len(note) or note[end] == "\n":
            return note[:at] + note[end:] or None
        at = note.find(joined, at + 1)
    if note.startswith(core) and (len(note) == len(core) or note[len(core)] == "\n"):
        return note[len(core) :].removeprefix("\n") or None
    return note


def _earliest(x: datetime | None, y: datetime | None) -> datetime | None:
    present = [value for value in (x, y) if value is not None]
    return min(present) if present else None


def _put_params(job_id: int, stamps: Mapping[str, datetime | None], note: str | None) -> dict[str, object]:
    return {"job": job_id, **{name: stamps[name] for name in ACTION_TIMESTAMPS}, "note": note}


async def coalesce_user_actions(conn: AsyncConnection, *, from_job: int, into_job: int) -> None:
    """Step 3 (module docstring): the from-job's `user_job_actions` coalesced into the into-job's, its own row
    deleted; nothing when the from-job has none."""
    rows = await locked_rows(conn, (from_job, into_job))
    source = rows.get(from_job)
    if source is None:
        return
    target = rows.get(into_job)
    stamps = {
        name: _earliest(source.timestamp(name), None if target is None else target.timestamp(name))
        for name in ACTION_TIMESTAMPS
    }
    note = concatenate_notes(None if target is None else target.note, source.note, from_job_id=from_job)
    await conn.execute(_DELETE_ACTIONS, {"job": from_job})
    await conn.execute(_PUT_ACTIONS, _put_params(into_job, stamps, note))


@dataclass(frozen=True)
class _OtherMerge:
    """Another merge event into the same job, by its snapshot: the from-job's row and the into-job's (`None`
    for a job that had none), and whether it is still in force (not reversed)."""

    id: int
    in_force: bool
    from_row: UserActions | None
    into_row: UserActions | None


async def _other_merges(conn: AsyncConnection, event: MergeEvent) -> list[_OtherMerge]:
    """The other merge events into the event's into-job (not an unmerge), oldest first; an event recorded
    without the actions snapshot is left out (it tells nothing of the rows)."""
    found: list[_OtherMerge] = []
    params = {"into": event.into_job_id, "event": event.id}
    for other in await conn.execute(_OTHER_MERGES_INTO, params):
        if other.actions is None:
            continue
        rows = {
            row.job_id: row for row in (UserActions.from_json(item) for item in get_list(other.actions, ""))
        }
        from_row, into_row = rows.get(int(other.from_job_id)), rows.get(event.into_job_id)
        found.append(_OtherMerge(int(other.id), bool(other.in_force), from_row, into_row))
    return found


def _peeled(value: datetime | None, name: str, earlier: Sequence[_OtherMerge]) -> datetime | None:
    """Ruling T22-d': the into-job's own value of `name`, from `value` (the reversed event's into snapshot)
    with what each earlier merge contributed peeled off, walking them newest first: a merge whose from-job's
    value is the value, and whose into snapshot held another, gave it, and the value becomes that other one.
    A null value was given by no merge (each keeps the earliest non-null), so the walk ends there."""
    for merge in earlier:
        if value is None:
            break
        came = None if merge.from_row is None else merge.from_row.timestamp(name)
        before = None if merge.into_row is None else merge.into_row.timestamp(name)
        if came == value and before != value:
            value = before
    return value


def _contributes(row: UserActions) -> bool:
    """Whether a merged from-job's row brought anything: a timestamp or a note."""
    return bool(row.note) or any(row.timestamp(name) is not None for name in ACTION_TIMESTAMPS)


async def restore_user_actions(conn: AsyncConnection, *, event: MergeEvent) -> None:
    """§8.6.2 step 4 (module docstring, Rulings T22-c, T22-d and T22-d'): the from-job's snapshot row
    restored; on the into-job each value that came only from this merge reset to the earliest of the
    into-job's own (earlier merges peeled off) and those of the merges into it still in force, the note
    without the segment this merge appended, and the row deleted when nothing of the job's own or of a merge
    in force is left."""
    raw = event.snapshot.get("user_job_actions")
    if raw is None:  # an event recorded before the snapshot held the user's actions
        log.info("unmerge_without_user_actions_snapshot", event_id=event.id)
        return
    snapshot = {row.job_id: row for row in (UserActions.from_json(item) for item in get_list(raw, ""))}
    before_from, before_into = snapshot.get(event.from_job_id), snapshot.get(event.into_job_id)
    current = (await locked_rows(conn, (event.from_job_id, event.into_job_id))).get(event.into_job_id)
    if before_from is not None:
        stamps = {name: before_from.timestamp(name) for name in ACTION_TIMESTAMPS}
        await conn.execute(_PUT_ACTIONS, _put_params(event.from_job_id, stamps, before_from.note))
    if current is None:  # nothing of the merge is left on the into-job
        return
    others = await _other_merges(conn, event)
    earlier = [merge for merge in reversed(others) if merge.id < event.id]  # newest first
    in_force = [merge.from_row for merge in others if merge.in_force and merge.from_row is not None]
    restored: dict[str, datetime | None] = {}
    for name in ACTION_TIMESTAMPS:
        value = current.timestamp(name)
        merged = None if before_from is None else before_from.timestamp(name)
        own = _peeled(None if before_into is None else before_into.timestamp(name), name, earlier)
        if merged is not None and value == merged and own != merged:  # the gate on the peeled value
            fallback = [own, *(row.timestamp(name) for row in in_force)]
            restored[name] = min((v for v in fallback if v is not None), default=None)
        else:
            restored[name] = value
    segment = appended_segment(
        None if before_into is None else before_into.note,
        None if before_from is None else before_from.note,
        from_job_id=event.from_job_id,
    )
    note = without_segment(current.note, segment)
    # the into-job's own state before any merge: the earliest merge into it found no row of it (Ruling T22-d')
    had_no_row = (earlier[-1].into_row if earlier else before_into) is None
    left = note is not None or any(value is not None for value in restored.values())
    if had_no_row and not any(_contributes(row) for row in in_force) and not left:
        await conn.execute(_DELETE_ACTIONS, {"job": event.into_job_id})
    else:
        await conn.execute(_PUT_ACTIONS, _put_params(event.into_job_id, restored, note))
