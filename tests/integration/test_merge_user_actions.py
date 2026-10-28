"""The user's actions across a merge and an unmerge (spec §8.6.1 steps 1 and 3, §8.6.2 step 4; plan M3 Task
22, R1, M1 Deviation 7): `user_actions.coalesce_user_actions` and `user_actions.restore_user_actions`, run
inside the merge and the unmerge.

- A merge snapshots both jobs' `user_job_actions` rows into `merge_events.evidence`, then each of `seen_at`,
  `saved_at`, `applied_at` and `hidden_at` of the into-job becomes the earliest non-null of the two; the notes
  are concatenated (the into-job's, a separator line naming the merged job, the from-job's), cut at 20,000
  characters with `[…]` (both full notes stay in the snapshot); the from-job's row is deleted.
- An unmerge restores the from-job's row from the snapshot; on the into-job a timestamp that came only from
  the merge (equal to the from-job's snapshot value, different from the into-job's own value: its snapshot
  value with what earlier merges gave peeled off, Ruling T22-d') is reset to the earliest of that own value
  and those of the merges still in force (Ruling T22-d); the note loses exactly the segment this merge
  appended, unless the user edited it (Ruling T22-c); the row is deleted when the job had none before its
  merges and nothing is left, so several merges into one job unmerge in any order.
- The cut keeps the into-job's note whole and cuts only the appended part, which is left out when no room is
  left; an event recorded without the actions snapshot restores none.

Every note and datum is invented.
"""

from datetime import datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.dedup.merge import unmerge
from jobhunter.dedup.user_actions import coalesce_user_actions
from jobhunter.normalize.keys import AdKey
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.collect import (
    STEPSTONE_1,
    KeyPair,
    key_pair,
    pending_item,
    put_user_actions,
    queued_run,
    resolved,
    user_actions,
)
from tests.support.m3 import insert_job

pytestmark = pytest.mark.integration

TIMESTAMPS = ("seen_at", "saved_at", "applied_at", "hidden_at")
EVENT = sa.text("SELECT id, from_job_id, into_job_id, evidence, created_at FROM merge_events ORDER BY id")


def separator(job_id: int) -> str:
    """The separator line the merge writes between the two notes."""
    return f"--- merged from job {job_id} ---"


async def merge_events(engine: AsyncEngine) -> list[Any]:
    async with engine.connect() as conn:
        return list((await conn.execute(EVENT)).all())


async def strong_key_merge(engine: AsyncEngine, owner: Any, pair: KeyPair, at: datetime) -> Any:
    """An Adzuna sighting of B carrying A's StepStone key: identity merges B into A (the strong-key merge)."""
    merging = await pending_item(
        engine,
        owner,
        "adzuna",
        {"native_id": "9", "primary_link": STEPSTONE_1},
        fetched_at=at + timedelta(hours=2),
    )
    assert await resolved(engine, owner, merging) == "linked"
    [event] = await merge_events(engine)
    assert (event.from_job_id, event.into_job_id) == (pair.b, pair.a)
    return event


def snapshot_actions(event: Any) -> dict[int, dict[str, Any]]:
    """The event's snapshot of the `user_job_actions` rows by job id, their timestamps parsed."""
    rows: dict[int, dict[str, Any]] = {}
    for row in event.evidence["snapshot"]["user_job_actions"]:
        parsed = {
            name: None if row[name] is None else datetime.fromisoformat(row[name])
            for name in (*TIMESTAMPS, "updated_at")
        }
        rows[int(row["job_id"])] = {**parsed, "note": row["note"]}
    return rows


def values(row: Any) -> dict[str, Any]:
    return {name: getattr(row, name) for name in (*TIMESTAMPS, "note")}


@pytest.fixture
def at() -> datetime:
    return utcnow() - timedelta(days=1)


async def seeded_pair(engine: AsyncEngine, owner: Any, at: datetime) -> KeyPair:
    """A and B with actions of their own: A seen later, saved never, applied earlier, hidden never; B seen
    first, saved, applied later, hidden."""
    pair = await key_pair(engine, owner, at=at)
    async with engine.begin() as conn:
        await put_user_actions(
            conn,
            pair.a,
            seen_at=at + timedelta(hours=2),
            applied_at=at + timedelta(hours=5),
            note="Rückruf am Montag.",
        )
        await put_user_actions(
            conn,
            pair.b,
            seen_at=at + timedelta(hours=1),
            saved_at=at + timedelta(hours=3),
            applied_at=at + timedelta(hours=6),
            hidden_at=at + timedelta(hours=4),
            note="Gehalt erfragen.",
        )
    return pair


async def test_a_merge_coalesces_the_actions_into_the_into_job_and_snapshots_both_rows(
    engine: AsyncEngine, current_epoch: int, at: datetime
) -> None:
    owner = await queued_run(engine)
    pair = await seeded_pair(engine, owner, at)
    before = {job: await user_actions(engine, job) for job in (pair.a, pair.b)}
    event = await strong_key_merge(engine, owner, pair, at)

    into = await user_actions(engine, pair.a)
    assert values(into) == {
        "seen_at": at + timedelta(hours=1),  # B's, the earlier
        "saved_at": at + timedelta(hours=3),  # B's alone
        "applied_at": at + timedelta(hours=5),  # A's, the earlier
        "hidden_at": at + timedelta(hours=4),  # B's alone
        "note": f"Rückruf am Montag.\n{separator(pair.b)}\nGehalt erfragen.",
    }
    assert into.updated_at == event.created_at  # written by the merge's transaction
    assert await user_actions(engine, pair.b) is None  # the from-job's row is deleted
    # step 1: both rows as they were, notes in full
    snapshot = snapshot_actions(event)
    assert set(snapshot) == {pair.a, pair.b}
    for job in (pair.a, pair.b):
        assert {name: snapshot[job][name] for name in (*TIMESTAMPS, "note")} == values(before[job])
        assert snapshot[job]["updated_at"] == before[job].updated_at


async def test_the_concatenated_note_is_cut_at_20000_characters_and_the_snapshot_keeps_both(
    engine: AsyncEngine, current_epoch: int, at: datetime
) -> None:
    owner = await queued_run(engine)
    pair = await key_pair(engine, owner, at=at)
    into_note, from_note = "a" * 15_000, "b" * 10_000
    async with engine.begin() as conn:
        await put_user_actions(conn, pair.a, note=into_note)
        await put_user_actions(conn, pair.b, note=from_note)
    event = await strong_key_merge(engine, owner, pair, at)

    note = (await user_actions(engine, pair.a)).note
    head = f"{into_note}\n{separator(pair.b)}\n"
    assert len(note) == 20_000 and note.endswith("[…]")
    assert note == head + "b" * (20_000 - len(head) - len("[…]")) + "[…]"  # the appended part is cut
    snapshot = snapshot_actions(event)
    assert (snapshot[pair.a]["note"], snapshot[pair.b]["note"]) == (into_note, from_note)


async def test_coalesce_user_actions_without_a_cut_with_one_row_and_with_none(engine: AsyncEngine) -> None:
    t = utcnow() - timedelta(days=2)
    async with engine.begin() as conn:
        into, source, lonely, empty_into, empty_from = [await insert_job(conn) for _ in range(5)]
        # a concatenation of exactly 20,000 characters is not cut
        into_note = "x" * 100
        from_note = "y" * (20_000 - 100 - 2 - len(separator(source.job_id)))
        await put_user_actions(conn, into.job_id, note=into_note)
        await put_user_actions(conn, source.job_id, hidden_at=t, note=from_note)
        # only the from-job has a row: the into-job gets its values, the note after the separator
        await put_user_actions(conn, lonely.job_id, applied_at=t, note="Unterlagen nachreichen.")
    async with engine.begin() as conn:
        await coalesce_user_actions(conn, from_job=source.job_id, into_job=into.job_id)
        await coalesce_user_actions(conn, from_job=lonely.job_id, into_job=empty_into.job_id)
        await coalesce_user_actions(conn, from_job=empty_from.job_id, into_job=empty_into.job_id)
    exact = await user_actions(engine, into.job_id)
    assert exact.note == f"{into_note}\n{separator(source.job_id)}\n{from_note}" and len(exact.note) == 20_000
    assert exact.hidden_at == t
    gained = await user_actions(engine, empty_into.job_id)
    assert values(gained) == {
        "seen_at": None,
        "saved_at": None,
        "applied_at": t,
        "hidden_at": None,
        "note": f"{separator(lonely.job_id)}\nUnterlagen nachreichen.",
    }
    for gone in (source, lonely, empty_from):
        assert await user_actions(engine, gone.job_id) is None


async def test_an_unmerge_restores_both_rows_when_nothing_was_edited_after_the_merge(
    engine: AsyncEngine, current_epoch: int, at: datetime
) -> None:
    owner = await queued_run(engine)
    pair = await seeded_pair(engine, owner, at)
    before = {job: values(await user_actions(engine, job)) for job in (pair.a, pair.b)}
    event = await strong_key_merge(engine, owner, pair, at)
    assert await user_actions(engine, pair.b) is None  # the merge took B's row into A's
    assert values(await user_actions(engine, pair.a)) != before[pair.a]
    async with engine.begin() as conn:
        await unmerge(conn, event.id)
    # every timestamp that came only from B is reset, A's own kept; A's note restored; B's row restored
    assert values(await user_actions(engine, pair.a)) == before[pair.a]
    assert values(await user_actions(engine, pair.b)) == before[pair.b]


async def test_an_unmerge_keeps_what_was_edited_after_the_merge(
    engine: AsyncEngine, current_epoch: int, at: datetime
) -> None:
    owner = await queued_run(engine)
    pair = await seeded_pair(engine, owner, at)
    b_before = values(await user_actions(engine, pair.b))
    event = await strong_key_merge(engine, owner, pair, at)
    async with engine.begin() as conn:  # the user edits the merged job later: saved again, a new note
        await put_user_actions(
            conn,
            pair.a,
            seen_at=at + timedelta(hours=1),
            saved_at=at + timedelta(hours=10),
            applied_at=at + timedelta(hours=5),
            hidden_at=at + timedelta(hours=4),
            note="Nach dem Zusammenführen bearbeitet.",
        )
    async with engine.begin() as conn:
        await unmerge(conn, event.id)
    assert values(await user_actions(engine, pair.a)) == {
        "seen_at": at + timedelta(hours=2),  # A's own: B's earlier value came only from the merge, reset
        "saved_at": at + timedelta(hours=10),  # the user's own, not B's: kept
        "applied_at": at + timedelta(hours=5),  # A's own: kept
        "hidden_at": None,  # B's alone: reset
        "note": "Nach dem Zusammenführen bearbeitet.",  # B's segment is gone from it: kept as it is
    }
    assert values(await user_actions(engine, pair.b)) == b_before


async def test_a_row_that_came_only_from_the_merge_is_gone_after_the_unmerge(
    engine: AsyncEngine, current_epoch: int, at: datetime
) -> None:
    owner = await queued_run(engine)
    pair = await key_pair(engine, owner, at=at)
    async with engine.begin() as conn:  # A has no row of its own
        await put_user_actions(conn, pair.b, hidden_at=at, note="Nicht passend.")
    b_before = values(await user_actions(engine, pair.b))
    event = await strong_key_merge(engine, owner, pair, at)
    assert snapshot_actions(event).keys() == {pair.b}
    merged = await user_actions(engine, pair.a)
    assert (merged.hidden_at, merged.note) == (at, f"{separator(pair.b)}\nNicht passend.")
    async with engine.begin() as conn:
        await unmerge(conn, event.id)
    assert await user_actions(engine, pair.a) is None
    assert values(await user_actions(engine, pair.b)) == b_before


async def test_a_merge_of_jobs_without_actions_writes_no_row(
    engine: AsyncEngine, current_epoch: int, at: datetime
) -> None:
    owner = await queued_run(engine)
    pair = await key_pair(engine, owner, at=at)
    event = await strong_key_merge(engine, owner, pair, at)
    assert event.evidence["snapshot"]["user_job_actions"] == []
    async with engine.begin() as conn:
        await unmerge(conn, event.id)
    for job in (pair.a, pair.b):
        assert await user_actions(engine, job) is None


# ---- fix round 1: several merges into one job (Rulings T22-c, T22-d), the cut's boundary, an old event -----

_JOB_OF_ITEM = sa.text(
    "SELECT p.job_id FROM posting_observations o JOIN postings p ON p.id = o.posting_id "
    "WHERE o.raw_item_id = :i"
)
_EVENTS_BY_FROM = sa.text("SELECT from_job_id, id FROM merge_events WHERE kind <> 'unmerge' ORDER BY id")


async def jsearch_job(engine: AsyncEngine, owner: Any, native_id: str, at: datetime) -> int:
    item = await pending_item(engine, owner, "jsearch", {"native_id": native_id}, fetched_at=at)
    assert await resolved(engine, owner, item) == "linked"
    async with engine.connect() as conn:
        return int((await conn.execute(_JOB_OF_ITEM, {"i": item})).scalar_one())


async def three_merged(
    engine: AsyncEngine, owner: Any, at: datetime, *, a_row: bool = True, **actions: dict[str, Any]
) -> tuple[int, int, int, dict[int, int]]:
    """A, B and C, each with a note of its own (and `actions[name]` beside it; A with no row at all unless
    `a_row`), then one sighting of all three keys: identity merges B and C into A in one transaction, B first.
    The merge event of each by its from-job."""
    a, b, c = [await jsearch_job(engine, owner, f"H{n}", at + timedelta(hours=n)) for n in range(3)]
    async with engine.begin() as conn:
        for name, job in (("a", a), ("b", b), ("c", c)):
            if name == "a" and not a_row:
                continue
            await put_user_actions(conn, job, note=f"{name.upper()}-Notiz", **actions.get(name, {}))
    keys = tuple(AdKey("htidocid", f"H{n}") for n in range(3))
    linking = await pending_item(
        engine, owner, "arbeitnow", {"raw_keys": keys}, fetched_at=at + timedelta(hours=4)
    )
    assert await resolved(engine, owner, linking) == "linked"
    async with engine.connect() as conn:
        events = {int(r.from_job_id): int(r.id) for r in await conn.execute(_EVENTS_BY_FROM)}
    assert set(events) == {b, c}
    return a, b, c, events


async def unmerged(engine: AsyncEngine, event_id: int) -> None:
    async with engine.begin() as conn:
        await unmerge(conn, event_id)


async def test_unmerging_one_of_two_merges_removes_only_its_own_note_segment(
    engine: AsyncEngine, current_epoch: int, at: datetime
) -> None:
    """Ruling T22-c (a): B and C merged into A in one transaction; B unmerged while C stays merged."""
    owner = await queued_run(engine)
    a, b, c, events = await three_merged(engine, owner, at)
    merged_note = f"A-Notiz\n{separator(b)}\nB-Notiz\n{separator(c)}\nC-Notiz"
    assert (await user_actions(engine, a)).note == merged_note
    await unmerged(engine, events[b])
    assert (await user_actions(engine, a)).note == f"A-Notiz\n{separator(c)}\nC-Notiz"  # A's and C's text
    assert (await user_actions(engine, b)).note == "B-Notiz"
    await unmerged(engine, events[c])
    assert (await user_actions(engine, a)).note == "A-Notiz"


async def test_unmerging_both_in_reverse_order_leaves_the_into_jobs_own_note(
    engine: AsyncEngine, current_epoch: int, at: datetime
) -> None:
    """Ruling T22-c (b): C unmerged first, then B: nothing of either is left in A's note, nor duplicated."""
    owner = await queued_run(engine)
    a, b, c, events = await three_merged(engine, owner, at)
    await unmerged(engine, events[c])
    assert (await user_actions(engine, a)).note == f"A-Notiz\n{separator(b)}\nB-Notiz"
    await unmerged(engine, events[b])
    assert (await user_actions(engine, a)).note == "A-Notiz"
    assert [(await user_actions(engine, job)).note for job in (b, c)] == ["B-Notiz", "C-Notiz"]


async def test_a_note_edited_inside_the_unmerged_segment_is_kept(
    engine: AsyncEngine, current_epoch: int, at: datetime
) -> None:
    """Ruling T22-c (c): the user changed the text B's merge appended: the note is kept as it is."""
    owner = await queued_run(engine)
    a, b, c, events = await three_merged(engine, owner, at)
    edited = f"A-Notiz\n{separator(b)}\nB-Notiz, nachgefragt\n{separator(c)}\nC-Notiz"
    row = await user_actions(engine, a)
    async with engine.begin() as conn:
        await put_user_actions(conn, a, note=edited)
    await unmerged(engine, events[b])
    after = await user_actions(engine, a)
    assert after.note == edited and (row.seen_at, row.hidden_at) == (after.seen_at, after.hidden_at)


async def test_an_unmerged_timestamp_falls_back_to_the_merges_still_in_force(
    engine: AsyncEngine, current_epoch: int, at: datetime
) -> None:
    """Ruling T22-d: B hidden and applied at T0, C at T0 + 1 h, A at neither; B unmerged while C stays merged:
    A stays hidden and applied, at C's time, and its note keeps C's segment."""
    owner = await queued_run(engine)
    t0 = at - timedelta(days=2)
    b_actions = {"hidden_at": t0, "applied_at": t0}
    c_actions = {"hidden_at": t0 + timedelta(hours=1), "applied_at": t0 + timedelta(hours=1)}
    a, b, c, events = await three_merged(engine, owner, at, b=b_actions, c=c_actions)
    merged = await user_actions(engine, a)
    assert (merged.hidden_at, merged.applied_at) == (t0, t0)
    await unmerged(engine, events[b])
    kept = await user_actions(engine, a)
    assert (kept.hidden_at, kept.applied_at) == (t0 + timedelta(hours=1), t0 + timedelta(hours=1))
    assert kept.note == f"A-Notiz\n{separator(c)}\nC-Notiz"


def hidden_applied_note(row: Any) -> tuple[Any, Any, Any]:
    return (row.hidden_at, row.applied_at, row.note)


@pytest.mark.parametrize("order", ["merge_order", "reverse_order"])
@pytest.mark.parametrize("a_row", [True, False], ids=["a_with_a_note", "a_without_a_row"])
async def test_unmerging_both_merges_in_either_order_peels_both_out(
    engine: AsyncEngine, current_epoch: int, at: datetime, order: str, a_row: bool
) -> None:
    """Ruling T22-d': B hidden and applied at T0, C at T0 + 1 h, A at neither; B and C merged into A, B first,
    then both unmerged. C's snapshot of A already holds B's T0, which the reset peels off B's merge, so A ends
    at neither, whichever order; A's row is deleted when A had none before the merges."""
    owner = await queued_run(engine)
    t0 = at - timedelta(days=2)
    b_actions = {"hidden_at": t0, "applied_at": t0}
    c_actions = {"hidden_at": t0 + timedelta(hours=1), "applied_at": t0 + timedelta(hours=1)}
    a, b, c, events = await three_merged(engine, owner, at, a_row=a_row, b=b_actions, c=c_actions)
    first, second = (b, c) if order == "merge_order" else (c, b)
    await unmerged(engine, events[first])
    await unmerged(engine, events[second])
    end = await user_actions(engine, a)
    if a_row:
        assert hidden_applied_note(end) == (None, None, "A-Notiz")
    else:
        assert end is None  # A had no row before either merge, and nothing of either is left
    assert hidden_applied_note(await user_actions(engine, b)) == (t0, t0, "B-Notiz")
    c_end = t0 + timedelta(hours=1)
    assert hidden_applied_note(await user_actions(engine, c)) == (c_end, c_end, "C-Notiz")


@pytest.mark.parametrize("order", ["merge_order", "reverse_order"])
async def test_the_into_jobs_own_earlier_value_survives_both_unmerges(
    engine: AsyncEngine, current_epoch: int, at: datetime, order: str
) -> None:
    """Ruling T22-d': A hidden at T0 - 1 h of its own, B and C as above: A's own value is kept through both
    unmerges, and the applied time that came from B and C is gone."""
    owner = await queued_run(engine)
    t0 = at - timedelta(days=2)
    a_actions = {"hidden_at": t0 - timedelta(hours=1)}
    b_actions = {"hidden_at": t0, "applied_at": t0}
    c_actions = {"hidden_at": t0 + timedelta(hours=1), "applied_at": t0 + timedelta(hours=1)}
    a, b, c, events = await three_merged(engine, owner, at, a=a_actions, b=b_actions, c=c_actions)
    assert hidden_applied_note(await user_actions(engine, a))[:2] == (t0 - timedelta(hours=1), t0)
    first, second = (b, c) if order == "merge_order" else (c, b)
    await unmerged(engine, events[first])
    await unmerged(engine, events[second])
    assert hidden_applied_note(await user_actions(engine, a)) == (t0 - timedelta(hours=1), None, "A-Notiz")


@pytest.mark.parametrize("order", ["merge_order", "reverse_order"])
async def test_two_merged_jobs_hidden_at_the_same_instant_leave_nothing_behind(
    engine: AsyncEngine, current_epoch: int, at: datetime, order: str
) -> None:
    """Ruling T22-d' (the gate on the peeled value): B and C hidden and applied at the same instant (one bulk
    hide), A at neither and without a row; both merged into A, then both unmerged. C's snapshot of A holds B's
    instant, which is C's too, so the reset's gate compares the value with A's own (peeled) value, not the raw
    snapshot: A's row is deleted in either order."""
    owner = await queued_run(engine)
    t0 = at - timedelta(days=2)
    same = {"hidden_at": t0, "applied_at": t0}
    a, b, c, events = await three_merged(engine, owner, at, a_row=False, b=dict(same), c=dict(same))
    first, second = (b, c) if order == "merge_order" else (c, b)
    await unmerged(engine, events[first])
    assert hidden_applied_note(await user_actions(engine, a))[:2] == (t0, t0)  # the other is still merged
    await unmerged(engine, events[second])
    assert await user_actions(engine, a) is None


async def test_the_cut_keeps_the_into_notes_text_whole_at_the_boundary(engine: AsyncEngine) -> None:
    """Minor 4 (L3206: the appended part is the one cut): an into note of 19,996 characters leaves room for
    the line break and the marker alone; one of 19,997 leaves none, and stays as it is."""
    async with engine.begin() as conn:
        tight, full, source_a, source_b = [await insert_job(conn) for _ in range(4)]
        await put_user_actions(conn, tight.job_id, note="t" * 19_996)
        await put_user_actions(conn, full.job_id, note="f" * 19_997)
        await put_user_actions(conn, source_a.job_id, note="Anhang")
        await put_user_actions(conn, source_b.job_id, note="Anhang")
    async with engine.begin() as conn:
        await coalesce_user_actions(conn, from_job=source_a.job_id, into_job=tight.job_id)
        await coalesce_user_actions(conn, from_job=source_b.job_id, into_job=full.job_id)
    assert (await user_actions(engine, tight.job_id)).note == "t" * 19_996 + "\n[…]"
    assert (await user_actions(engine, full.job_id)).note == "f" * 19_997


async def test_an_event_recorded_without_the_actions_snapshot_restores_none(
    engine: AsyncEngine, current_epoch: int, at: datetime, log_events: Any
) -> None:
    """Minor 5: an event whose snapshot holds no `user_job_actions` (recorded before M3) leaves the rows as
    they are and says so, with the event id alone."""
    owner = await queued_run(engine)
    pair = await seeded_pair(engine, owner, at)
    event = await strong_key_merge(engine, owner, pair, at)
    async with engine.begin() as conn:
        await conn.execute(
            sa.text(
                "UPDATE merge_events SET evidence = evidence #- '{snapshot,user_job_actions}' WHERE id = :e"
            ),
            {"e": event.id},
        )
    merged = values(await user_actions(engine, pair.a))
    await unmerged(engine, event.id)
    assert values(await user_actions(engine, pair.a)) == merged
    assert await user_actions(engine, pair.b) is None
    found = [e for e in log_events() if e["event"] == "unmerge_without_user_actions_snapshot"]
    assert [e["event_id"] for e in found] == [event.id]
