"""The user's actions across a merge and an unmerge (spec §8.6.1 steps 1 and 3, §8.6.2 step 4; plan M3 Task
22, R1, M1 Deviation 7): `merge.coalesce_user_actions` and `merge.restore_user_actions`, run inside the merge
and the unmerge.

- A merge snapshots both jobs' `user_job_actions` rows into `merge_events.evidence`, then each of `seen_at`,
  `saved_at`, `applied_at` and `hidden_at` of the into-job becomes the earliest non-null of the two; the notes
  are concatenated (the into-job's, a separator line naming the merged job, the from-job's), cut at 20,000
  characters with `[…]` (both full notes stay in the snapshot); the from-job's row is deleted.
- An unmerge restores the from-job's row from the snapshot; on the into-job a timestamp that came only from
  the merge (equal to the from-job's snapshot value, different from the into-job's) is reset, and the note is
  restored unless it was edited after the merge.

Every note and datum is invented.
"""

from datetime import datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.dedup import merge
from jobhunter.dedup.merge import unmerge
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
        await merge.coalesce_user_actions(conn, from_job=source.job_id, into_job=into.job_id)
        await merge.coalesce_user_actions(conn, from_job=lonely.job_id, into_job=empty_into.job_id)
        await merge.coalesce_user_actions(conn, from_job=empty_from.job_id, into_job=empty_into.job_id)
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
        "seen_at": at + timedelta(hours=2),  # B's value, not A's: it came from the merge, reset
        "saved_at": at + timedelta(hours=10),  # the user's own, not B's: kept
        "applied_at": at + timedelta(hours=5),  # A's own: kept
        "hidden_at": None,  # B's alone: reset
        "note": "Nach dem Zusammenführen bearbeitet.",  # edited after the merge: kept
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
