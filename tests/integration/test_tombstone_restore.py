"""The tombstone restore of identity resolution (spec §8.6 step 3 L3188, §7.9, §20.2 L6406-6409; plan M3 Task
22, M1 carry-forward): a new job whose keys match a tombstone's records `restored_from_tombstone_id` and
starts its `user_job_actions` row from the tombstone's timestamps, so a hidden ad that is re-posted stays
hidden and an applied one stays applied. The tombstone holds timestamps only, never a note.

Every datum is invented.
"""

import json
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa
from jobhunter.dedup.identity import tombstone_key_hash
from jobhunter.normalize.keys import AdKey
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.collect import pending_item, queued_run, resolved, user_actions

pytestmark = pytest.mark.integration

SQL_NULL = object()  # a tombstone whose `user_actions` column is SQL NULL
HIDDEN = datetime(2026, 9, 1, 8, 30, tzinfo=UTC)
APPLIED = datetime(2026, 8, 20, 17, 5, 12, 345678, tzinfo=UTC)
SEEN = datetime(2026, 8, 19, 9, 0, tzinfo=UTC)
# `:actions` is JSON text, or `None` for SQL NULL (not JSON `null`)
_TOMBSTONE = sa.text(
    "INSERT INTO job_tombstones (former_job_id, user_actions) VALUES (:former, CAST(:actions AS jsonb)) "
    "RETURNING id"
)
_ACTIONS_IS_NULL = sa.text("SELECT user_actions IS NULL FROM job_tombstones WHERE id = :t")
_KEY = sa.text("INSERT INTO tombstone_keys (key_hash, tombstone_id) VALUES (:h, :t)")
_JOB_OF_ITEM = sa.text(
    "SELECT j.id, j.restored_from_tombstone_id FROM posting_observations o "
    "JOIN postings p ON p.id = o.posting_id JOIN jobs j ON j.id = p.job_id WHERE o.raw_item_id = :i"
)


async def tombstone(engine: AsyncEngine, former_job_id: int, actions: Any, *keys: AdKey) -> int:
    """A tombstone of a purged job with these user-action timestamps (a JSON document; `SQL_NULL` stores
    none) and these keys hashed."""
    text = None if actions is SQL_NULL else json.dumps(actions)
    async with engine.begin() as conn:
        found = await conn.execute(_TOMBSTONE, {"former": former_job_id, "actions": text})
        tombstone_id = int(found.scalar_one())
        for key in keys:
            await conn.execute(_KEY, {"h": tombstone_key_hash(key), "t": tombstone_id})
    return tombstone_id


async def job_of(engine: AsyncEngine, raw_item_id: int) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(_JOB_OF_ITEM, {"i": raw_item_id})).one()


def timestamps(row: Any) -> tuple[Any, ...]:
    return (row.seen_at, row.saved_at, row.applied_at, row.hidden_at, row.note)


async def test_a_reposted_ad_keeps_its_hidden_and_applied_state(
    engine: AsyncEngine, current_epoch: int
) -> None:
    owner = await queued_run(engine)
    actions = {
        "seen_at": SEEN.isoformat(),
        "applied_at": APPLIED.isoformat(),
        "hidden_at": HIDDEN.isoformat(),
    }
    restored_from = await tombstone(engine, 424242, actions, AdKey("adzuna", "9"))
    item = await pending_item(engine, owner, "adzuna", {"native_id": "9"})
    assert await resolved(engine, owner, item) == "linked"
    job = await job_of(engine, item)
    assert job.restored_from_tombstone_id == restored_from
    row = await user_actions(engine, job.id)
    assert timestamps(row) == (SEEN, None, APPLIED, HIDDEN, None)  # timestamps only, never a note

    # a new job no tombstone names starts without a row
    other = await pending_item(engine, owner, "adzuna", {"native_id": "10"})
    assert await resolved(engine, owner, other) == "linked"
    fresh = await job_of(engine, other)
    assert fresh.restored_from_tombstone_id is None and await user_actions(engine, fresh.id) is None


async def test_a_tombstone_without_timestamps_restores_the_flag_and_no_row(
    engine: AsyncEngine, current_epoch: int
) -> None:
    owner = await queued_run(engine)
    sql_null = await tombstone(engine, 515151, SQL_NULL, AdKey("adzuna", "21"))
    json_null = await tombstone(engine, 525252, None, AdKey("adzuna", "22"))
    blank = await tombstone(engine, 535353, {"saved_at": None}, AdKey("adzuna", "23"))
    async with engine.connect() as conn:
        assert (await conn.execute(_ACTIONS_IS_NULL, {"t": sql_null})).scalar_one() is True
        assert (await conn.execute(_ACTIONS_IS_NULL, {"t": json_null})).scalar_one() is False
    for native_id, restored_from in (("21", sql_null), ("22", json_null), ("23", blank)):
        item = await pending_item(engine, owner, "adzuna", {"native_id": native_id})
        assert await resolved(engine, owner, item) == "linked"
        job = await job_of(engine, item)
        assert job.restored_from_tombstone_id == restored_from
        assert await user_actions(engine, job.id) is None


async def test_an_unreadable_timestamp_is_skipped_and_the_others_restored(
    engine: AsyncEngine, current_epoch: int, log_events: Any
) -> None:
    owner = await queued_run(engine)
    actions = {"hidden_at": HIDDEN.isoformat(), "saved_at": "not a time", "seen_at": "2026-08-19T09:00:00"}
    restored_from = await tombstone(engine, 545454, actions, AdKey("adzuna", "31"))
    item = await pending_item(engine, owner, "adzuna", {"native_id": "31"})
    assert await resolved(engine, owner, item) == "linked"
    row = await user_actions(engine, (await job_of(engine, item)).id)
    # a value that is no ISO time with its offset is dropped, never guessed
    assert timestamps(row) == (None, None, None, HIDDEN, None)
    # the warning names the tombstone and the fields, never a value
    (warning,) = [e for e in log_events() if e["event"] == "tombstone_actions_unreadable"]
    assert (warning["level"], warning["tombstone_id"], warning["fields"]) == (
        "warning",
        restored_from,
        ["seen_at", "saved_at"],
    )
    assert "not a time" not in json.dumps(log_events()) and "2026-08-19T09" not in json.dumps(log_events())
