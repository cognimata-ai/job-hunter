"""Task 24: the M1 retention rules (spec §20.1), after `operations` in `RULES`.

`raw_items` 90 days; `raw_bodies` nulled once `expires_at` (already `fetched_at + 30 d` and the deduplication
extension) has passed; `ats_snapshots` 30 days except the ones the live pipeline still uses; unreferenced
`embeddings`; `coverage_intervals` and terminal `fetch_requests` 13 months. A row whose body a newer,
unpurged response still references (§7.7 deduplication) is kept with its request and run until that body
expires: deleting it would leave the newer row without a body (its CHECK refuses that).
"""

from datetime import timedelta

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db.sql import q
from jobhunter.graphs.checkpointer import open_saver
from jobhunter.privacy import retention
from jobhunter.privacy.retention import RULES, run_retention_daily

from tests.support.collect import (
    RETENTION_SHIFT,
    aged_request,
    aged_response,
    free_cell,
    insert_finished_run,
)
from tests.support.logs import logged

pytestmark = pytest.mark.integration

M1_RULES = ("raw_items", "raw_bodies", "ats_snapshots", "embeddings", "coverage_intervals", "fetch_requests")
DAY = timedelta(days=1)


async def retention_at(engine, queue_app, migrated, now) -> dict[str, int]:
    async with open_saver(migrated.dsn("app")) as saver:
        return await run_retention_daily(engine, queue_app, saver, now=now)


async def ids(engine, table: str) -> set[int]:
    async with engine.connect() as c:
        return set((await c.execute(sa.text(f"SELECT id FROM {table}"))).scalars())  # noqa: S608


async def body(engine, response_id: int) -> tuple[object, ...]:
    """`(has a body, purged_at, body_ref_id)` of a stored response."""
    async with engine.connect() as c:
        row = await c.execute(
            sa.text("SELECT body_zstd IS NOT NULL, purged_at, body_ref_id FROM raw_responses WHERE id = :i"),
            {"i": response_id},
        )
        return tuple(row.one())


def test_the_m1_rules_follow_operations_in_order_with_their_windows() -> None:
    names = [rule.name for rule in RULES]
    at = names.index("operations") + 1
    assert tuple(names[at : at + len(M1_RULES)]) == M1_RULES
    windows = {rule.name: rule.window for rule in RULES}
    assert [windows[name] for name in M1_RULES] == [
        timedelta(days=90),
        timedelta(0),  # cutoff = now: `expires_at` already holds the 30 days and the extension
        timedelta(days=30),
        timedelta(0),
        timedelta(days=397),
        timedelta(days=397),
    ]
    assert {rule.name: rule.depends_on for rule in RULES if rule.depends_on} == {
        "fetch_requests": ("raw_items",),
        "runs": ("checkpoints",),
    }


async def test_m1_windows(engine, queue_app, migrated, m1_rows, monkeypatch, caplog) -> None:
    now, rows = m1_rows.now, m1_rows
    # A pass in which `raw_items` fails skips `fetch_requests` (its cascade would take the items); the rules
    # that do not depend on it still run.
    monkeypatch.setitem(retention._DELETES, "raw_items", q("DELETE FROM no_such_table WHERE at < :cutoff"))
    failed = await retention_at(engine, queue_app, migrated, now)
    assert (failed["raw_items"], failed["fetch_requests"]) == (-1, -1)
    assert [(e["rule"], e["blocked_by"]) for e in logged(caplog, "retention_rule_skipped")] == [
        ("fetch_requests", ["raw_items"])
    ]
    assert {rows["fr_old"], rows["item_old"]} <= await ids(engine, "fetch_requests") | await ids(
        engine, "raw_items"
    )
    assert (failed["raw_bodies"], failed["ats_snapshots"], failed["embeddings"]) == (1, 2, 1)
    assert failed["coverage_intervals"] == 2
    monkeypatch.undo()

    report = await retention_at(engine, queue_app, migrated, now)
    assert {name: report[name] for name in M1_RULES} == {
        "raw_items": 1,
        "raw_bodies": 0,  # the first pass purged it
        "ats_snapshots": 0,
        "embeddings": 0,
        "coverage_intervals": 0,
        "fetch_requests": 1,
    }
    # raw_items: 91 days old goes, its observation keeps its provenance copy without the item
    assert await ids(engine, "raw_items") == {rows["item_recent"], rows["item_deferred"]}
    async with engine.connect() as c:
        links = dict(
            (
                await c.execute(
                    sa.text("SELECT id, raw_item_id FROM posting_observations WHERE id = ANY(:ids)"),
                    {"ids": [rows["obs_old"], rows["obs_recent"]]},
                )
            ).all()
        )
    assert links == {rows["obs_old"]: None, rows["obs_recent"]: rows["item_recent"]}
    # raw_bodies: nulled with purged_at = now once expired, kept 1 s before; the shared body until its
    # extension
    assert await body(engine, rows["body_expired"]) == (False, now, None)
    assert await body(engine, rows["body_live"]) == (True, None, None)
    assert await body(engine, rows["body_shared"]) == (True, None, None)
    assert await body(engine, rows["body_ref"]) == (False, None, rows["body_shared"])
    # fetch_requests: the 398-day-old terminal one goes with its response; the open one and the one whose body
    # a newer row still references stay
    requests = await ids(engine, "fetch_requests")
    assert rows["fr_old"] not in requests and rows["response_old"] not in await ids(engine, "raw_responses")
    assert {rows["fr_recent"], rows["fr_open"], rows["fr_shared"]} <= requests
    # ats_snapshots: 31 days old goes; the last complete one and the open mass event's stay
    assert await ids(engine, "ats_snapshots") == {
        rows["snap_recent"],
        rows["last_complete"],
        rows["mass_open"],
        rows["mass_followup"],
    }
    # embeddings: only the referenced one stays
    assert await ids(engine, "embeddings") == {rows["emb_used"]}
    # coverage_intervals: by coalesce(closed_at, created_at)
    assert await ids(engine, "coverage_intervals") == {
        rows["gap_closed_recent"],
        rows["covered_recent"],
        rows["gap_open"],
    }
    assert await ids(engine, "runs") == {rows["run"]}

    # Once the extension has passed, both rows of the shared body are purged in the same pass, and the request
    # of the original goes: its response's row with it, the referencing row's `body_ref_id` set to NULL.
    later = await retention_at(engine, queue_app, migrated, now + 10 * DAY + timedelta(seconds=1))
    assert later["raw_bodies"] >= 2 and later["fetch_requests"] >= 1
    assert rows["fr_shared"] not in await ids(engine, "fetch_requests")
    purged = await body(engine, rows["body_ref"])
    assert purged[0] is False and purged[1] is not None and purged[2] is None


async def test_a_body_still_referenced_keeps_its_request_and_its_run(engine, queue_app, migrated) -> None:
    """A 13-month-old run whose response body a newer, unpurged row references (an unchanged board answers the
    same bytes every day) is kept, its request too, until that row's body expires; then both go."""
    now = utcnow() + RETENTION_SHIFT
    async with engine.begin() as c:
        cell = await free_cell(c, "arbeitnow")
        old_run = await insert_finished_run(c, at=now - 398 * DAY)
        new_run = await insert_finished_run(c, at=now - DAY)
        old_request = await aged_request(c, run_id=old_run, cell_id=cell, created_at=now - 398 * DAY)
        original = await aged_response(c, old_request, fetched_at=now - 398 * DAY, expires_at=now + 5 * DAY)
        new_request = await aged_request(c, run_id=new_run, cell_id=cell, created_at=now - 25 * DAY)
        referencing = await aged_response(
            c, new_request, fetched_at=now - 25 * DAY, expires_at=now + 5 * DAY, body=False, ref=original
        )
    first = await retention_at(engine, queue_app, migrated, now)
    assert (first["runs"], first["fetch_requests"]) == (0, 0)
    assert await ids(engine, "runs") == {old_run, new_run} and old_request in await ids(
        engine, "fetch_requests"
    )
    second = await retention_at(engine, queue_app, migrated, now + 6 * DAY)
    assert (second["raw_bodies"], second["fetch_requests"], second["runs"]) == (2, 1, 1)
    assert await ids(engine, "runs") == {new_run} and original not in await ids(engine, "raw_responses")
    purged = await body(engine, referencing)
    assert purged[0] is False and purged[1] == now + 6 * DAY and purged[2] is None
