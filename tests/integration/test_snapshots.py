import hashlib
from collections.abc import Collection, Mapping
from datetime import datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from jobhunter.availability import snapshots
from jobhunter.availability.rules import apply_sighting, recompute_job_availability
from jobhunter.availability.snapshots import SnapshotEffect, acknowledge, apply_snapshot
from jobhunter.clock import utcnow
from jobhunter.db.transitions import transition
from jobhunter.errors import FencedOut
from jobhunter.graphs.collect.nodes.resolve_identity import make_resolve_identity
from jobhunter.graphs.collect.state import initial_state
from jobhunter.worker import main as worker_main
from jobhunter.worker import reconcilers as r
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.support.collect import Board, ctx, job_lockable, make_deps, queued_run
from tests.support.reconcile import assert_fence_rule

pytestmark = pytest.mark.integration

H = timedelta(hours=1)
DAY = timedelta(days=1)
SNAPSHOT = sa.text(
    "INSERT INTO ats_snapshots (source_instance_id, run_id, status, incomplete_reason, member_count, "
    "taken_at) VALUES (:i, :run, :status, :reason, :count, :taken_at) RETURNING id"
)
MEMBERS = sa.text(
    "INSERT INTO ats_snapshot_members (snapshot_id, native_id) SELECT :s, unnest(CAST(:members AS text[]))"
)
POSTINGS = sa.text(
    "SELECT p.native_id, p.id, p.job_id, p.availability, p.availability_reason, p.availability_changed_at, "
    "p.absent_snapshots, p.first_absent_at, p.seen_count, p.last_seen_at, p.latest_observation_id, "
    "j.availability AS job_availability FROM postings p JOIN jobs j ON j.id = p.job_id "
    "WHERE p.source_instance_id = :i ORDER BY p.native_id"
)
SNAPSHOT_ROW = sa.text(
    "SELECT status, event_snapshot_id, baseline_active_count, absent_count, applied_at, acknowledged_at "
    "FROM ats_snapshots WHERE id = :s"
)
INSTANCE = sa.text(
    "SELECT last_snapshot_active_count AS baseline, mass_event_snapshot_id AS event FROM source_instances "
    "WHERE id = :i"
)
ALERT = sa.text(
    "SELECT severity, resolved_at IS NULL AS open FROM alerts WHERE dedup_key = :k ORDER BY id DESC LIMIT 1"
)
ABSENCES = sa.text("SELECT posting_id FROM ats_snapshot_absences WHERE snapshot_id = :s ORDER BY posting_id")
GENERATION = sa.text("SELECT generation FROM ranking_state WHERE id = 1")
# Every posting of a board, its job and its observation sighted at :at (before the test's snapshots).
SIGHTED_AT = (
    "UPDATE posting_observations SET observed_at = :at WHERE source_instance_id = :i",
    "UPDATE postings SET first_seen_at = :at, last_seen_at = :at, availability_changed_at = :at "
    "WHERE source_instance_id = :i",
    "UPDATE jobs SET first_seen_at = :at, last_seen_at = :at, availability_changed_at = :at "
    "WHERE id IN (SELECT job_id FROM postings WHERE source_instance_id = :i)",
)
BASELINE = sa.text("UPDATE source_instances SET last_snapshot_active_count = :n WHERE id = :i")


async def no_guard(conn: AsyncConnection) -> None:
    return None


async def sighted_at(engine: AsyncEngine, instance_id: int, at: datetime) -> None:
    async with engine.begin() as c:
        for statement in SIGHTED_AT:
            await c.execute(sa.text(statement), {"at": at, "i": instance_id})


async def snapshot(
    engine: AsyncEngine,
    instance_id: int,
    members: Collection[str],
    taken_at: datetime,
    *,
    complete: bool = True,
    run_id: int | None = None,
) -> int:
    """A recorded (unapplied) snapshot of the board, as `coverage.record_ats_snapshot` writes it."""
    async with engine.begin() as c:
        params = {
            "i": instance_id,
            "run": run_id,
            "status": "complete" if complete else "incomplete",
            "reason": None if complete else "no_end",
            "count": len(members),
            "taken_at": taken_at,
        }
        snapshot_id = int((await c.execute(SNAPSHOT, params)).scalar_one())
        await c.execute(MEMBERS, {"s": snapshot_id, "members": sorted(members)})
    return snapshot_id


async def apply(engine: AsyncEngine, snapshot_id: int) -> SnapshotEffect:
    async with engine.begin() as c:
        return await apply_snapshot(c, snapshot_id, now=utcnow(), guard=no_guard)


async def taken(engine: AsyncEngine, board: Board, members: Collection[str], at: datetime, **kw: Any):
    """Record a snapshot of the board and apply it: `(snapshot_id, effect)`."""
    snapshot_id = await snapshot(engine, board.instance_id, members, at, **kw)
    return snapshot_id, await apply(engine, snapshot_id)


async def postings(engine: AsyncEngine, instance_id: int) -> Mapping[str, Any]:
    async with engine.connect() as c:
        return {row.native_id: row for row in await c.execute(POSTINGS, {"i": instance_id})}


async def one(engine: AsyncEngine, statement: sa.TextClause, **params: Any) -> Any:
    async with engine.connect() as c:
        return (await c.execute(statement, params)).one()


async def generation(engine: AsyncEngine) -> int:
    async with engine.connect() as c:
        return int((await c.execute(GENERATION)).scalar_one())


async def extra_board(engine: AsyncEngine, posting_factory, key: str, at: datetime, n: int = 20) -> Board:
    """A second Greenhouse board `key` (no watchlist entry) with `n` active postings sighted at `at`."""
    made = {
        f"p{k:02d}": await posting_factory(
            engine,
            source_id="greenhouse",
            instance_key=key,
            native_id=f"p{k:02d}",
            availability="active",
            last_seen_at=at,
            availability_changed_at=at,
        )
        for k in range(1, n + 1)
    }
    async with engine.connect() as c:
        instance_id = int(
            (
                await c.execute(
                    sa.text("SELECT source_instance_id FROM postings WHERE id = :p"), {"p": made["p01"]}
                )
            ).scalar_one()
        )
    async with engine.begin() as c:
        await c.execute(BASELINE, {"n": n, "i": instance_id})
    return Board(instance_id, made)


async def test_absence_counting_closes_at_two_absences_a_day_apart(engine, current_epoch, board) -> None:
    t0 = utcnow() - 10 * DAY
    await sighted_at(engine, board.instance_id, t0 - DAY)
    _, effect = await taken(engine, board, {"b", "c"}, t0)
    assert effect == SnapshotEffect(absent_marked=1)
    rows = await postings(engine, board.instance_id)
    assert (rows["a"].availability, rows["a"].absent_snapshots, rows["a"].first_absent_at) == (
        "active",
        1,
        t0,
    )
    assert (rows["b"].absent_snapshots, rows["c"].absent_snapshots) == (0, 0)

    six = await snapshot(engine, board.instance_id, {"b", "c"}, t0 + 6 * H)
    assert await apply(engine, six) == SnapshotEffect(absent_marked=1)
    assert await apply(engine, six) == SnapshotEffect()  # applied once: `applied_at`
    assert (await postings(engine, board.instance_id))["a"].absent_snapshots == 2
    for hours in (12, 18):  # saturated at 2, but not yet 24 h after the first absence
        _, effect = await taken(engine, board, {"b", "c"}, t0 + hours * H)
        assert effect == SnapshotEffect(absent_marked=1)
        a = (await postings(engine, board.instance_id))["a"]
        assert (a.availability, a.absent_snapshots, a.first_absent_at) == ("active", 2, t0)
    gen = await generation(engine)
    day, effect = await taken(engine, board, {"b", "c"}, t0 + 24 * H)
    assert effect == SnapshotEffect(absent_marked=1, closed=1)  # the fifth consecutive absence
    rows = await postings(engine, board.instance_id)
    assert (rows["a"].availability, rows["a"].availability_reason, rows["a"].job_availability) == (
        "closed",
        "ats_absent",
        "closed",
    )
    assert await generation(engine) == gen + 1  # bumped once, last
    applied = await one(engine, SNAPSHOT_ROW, s=day)
    assert (applied.status, applied.baseline_active_count, applied.absent_count) == ("complete", 3, 1)
    assert applied.applied_at is not None
    assert (await one(engine, INSTANCE, i=board.instance_id)).baseline == 2  # the active count after it

    # an incomplete snapshot: b and c unknown at its time, their absence not counted
    _, effect = await taken(engine, board, set(), t0 + 30 * H, complete=False)
    assert effect == SnapshotEffect(unknown=2)
    rows = await postings(engine, board.instance_id)
    for name in ("b", "c"):
        assert (rows[name].availability, rows[name].absent_snapshots, rows[name].job_availability) == (
            "unknown",
            0,
            "unknown",
        )
        assert rows[name].availability_changed_at == t0 + 30 * H
    assert rows["a"].availability == "closed"
    assert (await one(engine, INSTANCE, i=board.instance_id)).baseline == 2  # an incomplete one sets none
    # a member of a snapshot older than the change (applied late) revives nothing (carry 3)
    _, effect = await taken(engine, board, {"b"}, t0 + 29 * H, complete=False)
    assert effect == SnapshotEffect()
    assert (await postings(engine, board.instance_id))["b"].availability == "unknown"

    # a complete one with b: b active again (at its time), c counted from here
    _, effect = await taken(engine, board, {"b"}, t0 + 36 * H)
    assert effect == SnapshotEffect(revived=1, absent_marked=1)
    rows = await postings(engine, board.instance_id)
    assert (rows["b"].availability, rows["b"].availability_changed_at, rows["b"].seen_count) == (
        "active",
        t0 + 36 * H,
        1,
    )
    assert (rows["c"].availability, rows["c"].absent_snapshots, rows["c"].first_absent_at) == (
        "unknown",
        1,
        t0 + 36 * H,
    )
    for hours in (42, 48, 54):
        _, effect = await taken(engine, board, {"b"}, t0 + hours * H)
        assert effect.closed == 0
    assert (await postings(engine, board.instance_id))["c"].availability == "unknown"
    _, effect = await taken(engine, board, {"b"}, t0 + 60 * H)  # 24 h after its own first absence
    assert effect == SnapshotEffect(absent_marked=1, closed=1)
    rows = await postings(engine, board.instance_id)
    assert (rows["c"].availability, rows["c"].availability_reason) == ("closed", "ats_absent")
    assert rows["b"].availability == "active"


async def test_mass_disappearance_open_followup_confirm_and_acknowledge(
    engine, current_epoch, board20, posting_factory
) -> None:
    d1 = (utcnow() - 10 * DAY).replace(hour=0, minute=0, second=0, microsecond=0)
    await sighted_at(engine, board20.instance_id, d1 - DAY)
    alert = {"k": f"{snapshots.MASS_ALERT}:instance:{board20.instance_id}"}

    # 06:00 d1: none of the 20 is a member: the event opens, every posting unknown, the baseline frozen
    opening, effect = await taken(engine, board20, set(), d1 + 6 * H)
    assert effect == SnapshotEffect(unknown=20, mass_opened=True)
    row = await one(engine, SNAPSHOT_ROW, s=opening)
    assert (row.status, row.event_snapshot_id, row.baseline_active_count, row.absent_count) == (
        "mass_disappearance",
        None,
        20,
        20,
    )
    assert tuple(await one(engine, INSTANCE, i=board20.instance_id)) == (20, opening)
    rows = await postings(engine, board20.instance_id)
    assert {p.availability for p in rows.values()} == {"unknown"}
    assert {p.absent_snapshots for p in rows.values()} == {0}  # not counted
    assert tuple(await one(engine, ALERT, **alert)) == ("warn", True)
    async with engine.connect() as c:
        frozen = list((await c.execute(ABSENCES, {"s": opening})).scalars())
    assert frozen == sorted(board20.postings.values())  # the event's frozen absent set

    # 12:00 d1, 18:00 d1, 00:00 d2: follow-ups of the event, nothing closed
    for hours in (12, 18, 24):
        followup, effect = await taken(engine, board20, set(), d1 + hours * H)
        assert effect == SnapshotEffect(mass_followup=True)
        row = await one(engine, SNAPSHOT_ROW, s=followup)
        assert (row.status, row.event_snapshot_id, row.baseline_active_count) == (
            "mass_disappearance",
            opening,
            20,
        )
    assert tuple(await one(engine, INSTANCE, i=board20.instance_id)) == (20, opening)
    async with engine.connect() as c:
        assert (await c.execute(ABSENCES, {"s": followup})).first() is None  # only the opening's set

    # 12:00 d2: 24 h after the 12:00 d1 follow-up: confirmed, the 20 close
    gen = await generation(engine)
    confirming, effect = await taken(engine, board20, set(), d1 + 36 * H)
    assert effect == SnapshotEffect(closed=20, mass_followup=True, mass_confirmed=True)
    rows = await postings(engine, board20.instance_id)
    assert {(p.availability, p.availability_reason, p.job_availability) for p in rows.values()} == {
        ("closed", "mass_disappearance_confirmed", "closed")
    }
    assert tuple(await one(engine, INSTANCE, i=board20.instance_id)) == (0, None)
    assert tuple(await one(engine, ALERT, **alert)) == ("warn", False)
    assert await generation(engine) == gen + 1
    assert (await one(engine, SNAPSHOT_ROW, s=confirming)).event_snapshot_id == opening

    # a second board: opened, one posting of its frozen set sighted again by a job item, then acknowledged
    second = await extra_board(engine, posting_factory, "board-two", d1 - DAY)
    opened, effect = await taken(engine, second, set(), d1 + 6 * H)
    assert effect.mass_opened and effect.unknown == 20
    resighted = second.postings["p07"]
    async with engine.begin() as c:
        observation = (
            await c.execute(
                sa.text("SELECT latest_observation_id FROM postings WHERE id = :p"), {"p": resighted}
            )
        ).scalar_one()
        sighting = await apply_sighting(c, resighted, observed_at=d1 + 7 * H, observation_id=observation)
        job = (
            await c.execute(sa.text("SELECT job_id FROM postings WHERE id = :p"), {"p": resighted})
        ).scalar_one()
        await recompute_job_availability(c, job)
    assert sighting.revived
    async with engine.begin() as c:
        assert await acknowledge(c, opened, now=utcnow()) == 19
    rows = await postings(engine, second.instance_id)
    assert rows["p07"].availability == "active"  # left `unknown` by reappearing: not closed
    assert {(p.availability, p.availability_reason) for n, p in rows.items() if n != "p07"} == {
        ("closed", "mass_disappearance_confirmed")
    }
    assert (await one(engine, SNAPSHOT_ROW, s=opened)).acknowledged_at is not None
    assert tuple(await one(engine, INSTANCE, i=second.instance_id)) == (1, None)
    assert tuple(await one(engine, ALERT, k=f"{snapshots.MASS_ALERT}:instance:{second.instance_id}")) == (
        "warn",
        False,
    )
    async with engine.begin() as c:
        assert await acknowledge(c, opened, now=utcnow()) == 0  # the event is no longer open

    # a third board: a follow-up with only 8 of 20 absent is `complete` and clears the event
    third = await extra_board(engine, posting_factory, "board-three", d1 - DAY)
    third_opening, _ = await taken(engine, third, set(), d1 + 6 * H)
    present = {f"p{k:02d}" for k in range(1, 13)}
    cleared, effect = await taken(engine, third, present, d1 + 12 * H)
    assert effect == SnapshotEffect(revived=12, absent_marked=8, mass_cleared=True)
    row = await one(engine, SNAPSHOT_ROW, s=cleared)
    assert (row.status, row.event_snapshot_id, row.absent_count) == ("complete", None, 8)
    assert tuple(await one(engine, INSTANCE, i=third.instance_id)) == (12, None)
    rows = await postings(engine, third.instance_id)
    for name, p in rows.items():
        if name in present:
            assert (p.availability, p.absent_snapshots) == ("active", 0)
        else:  # counted absent once, by the ordinary rule
            assert (p.availability, p.absent_snapshots, p.first_absent_at) == ("unknown", 1, d1 + 12 * H)
    assert tuple(await one(engine, ALERT, k=f"{snapshots.MASS_ALERT}:instance:{third.instance_id}")) == (
        "warn",
        False,
    )
    assert third_opening != cleared
    async with engine.begin() as c:
        with pytest.raises(ValueError, match="not a mass disappearance"):
            await acknowledge(c, cleared, now=utcnow())


async def test_snapshot_membership_never_counts_a_sighting(engine, current_epoch, board) -> None:
    owner = await queued_run(engine, include_paid=False)
    t = utcnow() - 2 * H
    await sighted_at(engine, board.instance_id, t - 3 * DAY)
    rows = await postings(engine, board.instance_id)
    # posting a was linked by an item of the run (its sighting: seen_count 2)
    async with engine.begin() as c:
        sha = hashlib.sha256(b"a@t").digest()
        observation = (
            await c.execute(
                sa.text(
                    "INSERT INTO posting_observations (posting_id, source_instance_id, raw_response_sha256, "
                    "observed_at, normalized, content_hash, completeness, quality) VALUES (:p, :i, :sha, :t, "
                    "'{}', :sha, 'full', 'ok') RETURNING id"
                ),
                {"p": rows["a"].id, "i": board.instance_id, "sha": sha, "t": t},
            )
        ).scalar_one()
        await apply_sighting(c, rows["a"].id, observed_at=t, observation_id=observation)
        # b went stale (not re-confirmed) two days ago and no item of the run sighted it
        await transition(c, "postings", rows["b"].id, ["active"], "stale")
        await c.execute(
            sa.text("UPDATE postings SET availability_changed_at = :at WHERE id = :p"),
            {"at": t - 2 * DAY, "p": rows["b"].id},
        )
    before = await postings(engine, board.instance_id)
    assert before["a"].seen_count == 2
    # the run's complete snapshot: a, b and `zz`, whose item was rejected (a member without a posting)
    run_snapshot = await snapshot(engine, board.instance_id, {"a", "b", "zz"}, t, run_id=owner.run_id)
    node = make_resolve_identity(make_deps(engine, lambda instance: pytest.fail("no adapter is used")))
    update = await node(initial_state(owner.run_id, owner.attempt_no))
    assert {k: update["counters"][k] for k in snapshots.SNAPSHOT_COUNTERS} == {
        "availability_closed": 0,
        "availability_unknown": 0,
        "availability_revived": 1,
        "mass_events_opened": 0,
    }
    after = await postings(engine, board.instance_id)
    assert set(after) == {"a", "b", "c"}  # no posting for `zz`
    for name in ("a", "b"):  # membership counts no sighting
        assert (after[name].seen_count, after[name].last_seen_at, after[name].latest_observation_id) == (
            before[name].seen_count,
            before[name].last_seen_at,
            before[name].latest_observation_id,
        )
    assert (after["a"].seen_count, after["a"].last_seen_at) == (2, t)
    assert (after["b"].availability, after["b"].availability_changed_at, after["b"].seen_count) == (
        "active",
        t,  # revived at the snapshot's time
        1,
    )
    assert (after["c"].availability, after["c"].absent_snapshots) == ("active", 1)
    assert (await one(engine, SNAPSHOT_ROW, s=run_snapshot)).applied_at is not None
    again = await node(initial_state(owner.run_id, owner.attempt_no))
    assert {k: again["counters"][k] for k in snapshots.SNAPSHOT_COUNTERS} == dict.fromkeys(
        snapshots.SNAPSHOT_COUNTERS, 0
    )


async def test_the_guard_comes_after_the_job_rows_and_fences_the_snapshot(
    engine, current_epoch, board
) -> None:
    t0 = utcnow() - 3 * DAY
    await sighted_at(engine, board.instance_id, t0 - DAY)
    first = await snapshot(engine, board.instance_id, {"b", "c"}, t0)

    async def fenced(conn: AsyncConnection) -> None:
        raise FencedOut("run")

    with pytest.raises(FencedOut):
        async with engine.begin() as c:
            await apply_snapshot(c, first, now=utcnow(), guard=fenced)
    assert (await one(engine, SNAPSHOT_ROW, s=first)).applied_at is None
    assert (await postings(engine, board.instance_id))["a"].absent_snapshots == 0

    jobs = sorted(p.job_id for p in (await postings(engine, board.instance_id)).values())
    seen: list[list[bool]] = []

    async def probe(conn: AsyncConnection) -> None:
        seen.append([await job_lockable(engine, job) for job in jobs])

    async with engine.begin() as c:
        assert await apply_snapshot(c, first, now=utcnow(), guard=probe) == SnapshotEffect(absent_marked=1)
    assert seen == [[False, False, False]]  # every job row was held when the guard ran (Ruling T19-a)


async def test_the_mass_events_reconciler_confirms_an_open_event_and_is_fenced(
    engine, current_epoch, queue_app, board20
) -> None:
    assert r.REGISTRY["ats_mass_events"][0] == {"tick"}
    assert "jobhunter.availability.snapshots" in worker_main._REGISTERING_MODULES
    reconcile = r.REGISTRY["ats_mass_events"][1]
    d1 = (utcnow() - 10 * DAY).replace(hour=0, minute=0, second=0, microsecond=0)
    await sighted_at(engine, board20.instance_id, d1 - DAY)
    opening, _ = await taken(engine, board20, set(), d1 + 6 * H)
    await taken(engine, board20, set(), d1 + 12 * H)
    assert await reconcile(ctx(engine, queue_app)) == 0  # one follow-up: nothing to confirm yet
    # a later structurally complete snapshot of the event recorded as applied, its confirmation not yet run
    later = await snapshot(engine, board20.instance_id, {"p01"}, d1 + 40 * H)
    # and, between the two confirming snapshots, an incomplete one with p02 (a member is present, §8.11)
    between = await snapshot(engine, board20.instance_id, {"p02"}, d1 + 20 * H, complete=False)
    async with engine.begin() as c:
        await c.execute(
            sa.text(
                "UPDATE ats_snapshots SET status = 'mass_disappearance', event_snapshot_id = :e, "
                "applied_at = now() WHERE id = :s"
            ),
            {"e": opening, "s": later},
        )
        await c.execute(sa.text("UPDATE ats_snapshots SET applied_at = now() WHERE id = :s"), {"s": between})
    # a superseded worker: its own fence propagates, and the cadence stops at the reconciler (Ruling FR-w)
    await assert_fence_rule(
        "ats_mass_events",
        "tick",
        ctx(engine, queue_app, epoch=current_epoch + 1),
        ctx(engine, queue_app),
        snapshots,
    )
    assert tuple(await one(engine, INSTANCE, i=board20.instance_id)) == (20, opening)
    assert await reconcile(ctx(engine, queue_app)) == 1  # one event confirmed
    rows = await postings(engine, board20.instance_id)
    assert rows["p01"].availability == "unknown"  # a member of a confirming snapshot: not closed
    assert rows["p02"].availability == "unknown"  # present inside the window: not closed either
    assert {p.availability for n, p in rows.items() if n not in ("p01", "p02")} == {"closed"}
    assert tuple(await one(engine, INSTANCE, i=board20.instance_id)) == (0, None)
    assert await reconcile(ctx(engine, queue_app)) == 0


# ---- fix round 1 ------------------------------------------------------------------------------------------

ALERT_COUNT = sa.text("SELECT count(*) FROM alerts WHERE dedup_key = :k")


async def test_unknown_postings_absent_again_open_no_new_event(engine, current_epoch, board20) -> None:
    """Ruling T19-b, the review's trace: after an event clears (baseline 12, 8 still `unknown` and absent),
    the next complete snapshot with the same 12 members is no new mass disappearance; the 8 keep counting
    toward their ordinary close."""
    d1 = (utcnow() - 10 * DAY).replace(hour=0, minute=0, second=0, microsecond=0)
    await sighted_at(engine, board20.instance_id, d1 - DAY)
    alert = {"k": f"{snapshots.MASS_ALERT}:instance:{board20.instance_id}"}
    await taken(engine, board20, set(), d1 + 6 * H)  # opens: 20 absent
    present = {f"p{k:02d}" for k in range(1, 13)}
    _, effect = await taken(engine, board20, present, d1 + 12 * H)
    assert effect.mass_cleared and tuple(await one(engine, INSTANCE, i=board20.instance_id)) == (12, None)

    again, effect = await taken(engine, board20, present, d1 + 18 * H)
    assert effect == SnapshotEffect(absent_marked=8)  # no event: none of the 12 active postings is absent
    row = await one(engine, SNAPSHOT_ROW, s=again)
    assert (row.status, row.event_snapshot_id, row.baseline_active_count, row.absent_count) == (
        "complete",
        None,
        12,
        0,
    )
    assert tuple(await one(engine, INSTANCE, i=board20.instance_id)) == (12, None)
    assert tuple(await one(engine, ALERT, **alert)) == ("warn", False)  # the cleared one, not raised again
    async with engine.connect() as c:
        assert (await c.execute(ALERT_COUNT, alert)).scalar_one() == 1
    rows = await postings(engine, board20.instance_id)
    gone = {name for name in rows if name not in present}
    assert {(rows[n].availability, rows[n].absent_snapshots, rows[n].first_absent_at) for n in gone} == {
        ("unknown", 2, d1 + 12 * H)
    }
    _, effect = await taken(engine, board20, present, d1 + 36 * H)  # 24 h after their first absence
    assert effect == SnapshotEffect(absent_marked=8, closed=8)
    rows = await postings(engine, board20.instance_id)
    assert {(rows[n].availability, rows[n].availability_reason) for n in gone} == {("closed", "ats_absent")}


async def test_an_incomplete_reads_unknowns_count_as_disappeared_but_below_half_open_no_event(
    engine, current_epoch, board20
) -> None:
    """Rulings T19-b and FR-d: 20 postings, complete at first (baseline 20); an incomplete snapshot leaves 9
    `unknown`. The next complete snapshot without them counts the 9 as disappeared (they were active at the
    baseline's snapshot, FR-d), but 9 is not more than half of 20: no event, the ordinary rule counts them,
    and the baseline falls to 11. The one after it counts none of them (they were `unknown` at its baseline's
    snapshot) and opens no event either."""
    t0 = utcnow() - 10 * DAY
    await sighted_at(engine, board20.instance_id, t0 - DAY)
    await taken(engine, board20, set(board20.postings), t0 - 6 * H)  # complete: baseline 20
    present = {f"p{k:02d}" for k in range(10, 21)}  # 11 present, p01..p09 missing
    _, effect = await taken(engine, board20, present, t0, complete=False)
    assert effect == SnapshotEffect(unknown=9)
    for hours, disappeared in ((6, 9), (12, 0)):
        snapshot_id, effect = await taken(engine, board20, present, t0 + hours * H)
        assert effect == SnapshotEffect(absent_marked=9)
        row = await one(engine, SNAPSHOT_ROW, s=snapshot_id)
        assert (row.status, row.absent_count) == ("complete", disappeared)
        assert tuple(await one(engine, INSTANCE, i=board20.instance_id)) == (11, None)
    async with engine.connect() as c:
        alert = {"k": f"{snapshots.MASS_ALERT}:instance:{board20.instance_id}"}
        assert (await c.execute(ALERT_COUNT, alert)).scalar_one() == 0
        assert (await c.execute(sa.text("SELECT count(*) FROM ats_snapshot_absences"))).scalar_one() == 0


async def test_the_absence_count_is_once_per_snapshot_without_applied_at(
    engine, current_epoch, board
) -> None:
    """M-3: `last_absent_snapshot_id` alone keeps a snapshot's absence counted once, even when its
    `applied_at` guard is bypassed."""
    t0 = utcnow() - 3 * DAY
    await sighted_at(engine, board.instance_id, t0 - DAY)
    first, effect = await taken(engine, board, {"b", "c"}, t0)
    assert effect == SnapshotEffect(absent_marked=1)
    async with engine.begin() as c:
        await c.execute(sa.text("UPDATE ats_snapshots SET applied_at = NULL WHERE id = :s"), {"s": first})
    assert await apply(engine, first) == SnapshotEffect()  # applied again, counted nothing
    a = (await postings(engine, board.instance_id))["a"]
    assert (a.absent_snapshots, a.first_absent_at) == (1, t0)


async def test_an_unknown_never_moves_the_change_time_backwards(engine, current_epoch, board) -> None:
    """M-5: a posting whose availability changed after the snapshot was taken keeps a change time that is not
    earlier than before; the others take the snapshot's time."""
    t0 = utcnow() - 3 * DAY
    await sighted_at(engine, board.instance_id, t0 - DAY)
    rows = await postings(engine, board.instance_id)
    later = t0 + 2 * H
    async with engine.begin() as c:
        await c.execute(
            sa.text("UPDATE postings SET availability_changed_at = :at, last_seen_at = :at WHERE id = :p"),
            {"at": later, "p": rows["b"].id},
        )
    _, effect = await taken(engine, board, {"a"}, t0, complete=False)
    assert effect == SnapshotEffect(unknown=2)
    rows = await postings(engine, board.instance_id)
    assert (rows["b"].availability, rows["c"].availability) == ("unknown", "unknown")
    assert rows["c"].availability_changed_at == t0
    assert rows["b"].availability_changed_at >= later  # never moved back to `t0`


# ---- final review I1 (Ruling FR-d) ------------------------------------------------------------------------


async def test_an_incomplete_snapshot_cannot_hide_a_mass_disappearance_from_the_next_complete_one(
    engine, current_epoch, board20
) -> None:
    """Complete (20 members, baseline 20) → incomplete (none: the 20 go `unknown`, the baseline stays 20) →
    complete and empty → complete and empty. The 20 were active at the last structurally complete snapshot,
    so the first empty complete snapshot finds them disappeared: it opens a mass disappearance instead of
    counting them toward an ordinary close; the next one is its follow-up, and only the event's confirmation
    (two follow-ups 24 h apart) closes them, never `ats_absent`."""
    d1 = (utcnow() - 10 * DAY).replace(hour=0, minute=0, second=0, microsecond=0)
    await sighted_at(engine, board20.instance_id, d1 - DAY)
    every = set(board20.postings)
    alert = {"k": f"{snapshots.MASS_ALERT}:instance:{board20.instance_id}"}

    _, effect = await taken(engine, board20, every, d1)
    assert effect == SnapshotEffect()
    assert tuple(await one(engine, INSTANCE, i=board20.instance_id)) == (20, None)

    _, effect = await taken(engine, board20, set(), d1 + 6 * H, complete=False)
    assert effect == SnapshotEffect(unknown=20)
    assert tuple(await one(engine, INSTANCE, i=board20.instance_id)) == (20, None)  # never lowered

    opening, effect = await taken(engine, board20, set(), d1 + 12 * H)
    assert effect == SnapshotEffect(mass_opened=True)  # all 20 are `unknown` already: none newly so
    row = await one(engine, SNAPSHOT_ROW, s=opening)
    assert (row.status, row.event_snapshot_id, row.baseline_active_count, row.absent_count) == (
        "mass_disappearance",
        None,
        20,
        20,
    )
    assert tuple(await one(engine, INSTANCE, i=board20.instance_id)) == (20, opening)
    assert tuple(await one(engine, ALERT, **alert)) == ("warn", True)
    async with engine.connect() as c:
        frozen = list((await c.execute(ABSENCES, {"s": opening})).scalars())
    assert frozen == sorted(board20.postings.values())
    rows = await postings(engine, board20.instance_id)
    assert {(p.availability, p.absent_snapshots) for p in rows.values()} == {("unknown", 0)}  # not counted

    followup, effect = await taken(engine, board20, set(), d1 + 36 * H)
    assert effect == SnapshotEffect(mass_followup=True)  # 24 h later: no ordinary close
    row = await one(engine, SNAPSHOT_ROW, s=followup)
    assert (row.status, row.event_snapshot_id, row.baseline_active_count, row.absent_count) == (
        "mass_disappearance",
        opening,
        20,
        20,
    )
    rows = await postings(engine, board20.instance_id)
    assert {(p.availability, p.absent_snapshots) for p in rows.values()} == {("unknown", 0)}

    _, effect = await taken(engine, board20, set(), d1 + 60 * H)  # 24 h after the first follow-up
    assert effect == SnapshotEffect(closed=20, mass_followup=True, mass_confirmed=True)
    rows = await postings(engine, board20.instance_id)
    assert {(p.availability, p.availability_reason) for p in rows.values()} == {
        ("closed", "mass_disappearance_confirmed")
    }
    assert tuple(await one(engine, INSTANCE, i=board20.instance_id)) == (0, None)
    assert tuple(await one(engine, ALERT, **alert)) == ("warn", False)


async def test_an_event_opened_across_an_incomplete_read_counts_those_postings_in_its_follow_ups(
    engine, current_epoch, board20
) -> None:
    """FR-d with the T19-b addendum: 15 of 20 go `unknown` through an incomplete read, then a complete
    snapshot without any of the 20 opens the event (all 20 disappeared). Its follow-ups keep comparing the
    same population with the frozen baseline: the 15 still count, so the event does not clear at its first
    follow-up (which would reset the baseline to 0 and let the ordinary rule close them)."""
    d1 = (utcnow() - 10 * DAY).replace(hour=0, minute=0, second=0, microsecond=0)
    await sighted_at(engine, board20.instance_id, d1 - DAY)
    every = set(board20.postings)
    await taken(engine, board20, every, d1)
    kept = {f"p{k:02d}" for k in range(16, 21)}  # p01..p15 missed by the incomplete read
    _, effect = await taken(engine, board20, kept, d1 + 6 * H, complete=False)
    assert effect == SnapshotEffect(unknown=15)
    opening, effect = await taken(engine, board20, set(), d1 + 12 * H)
    assert effect == SnapshotEffect(unknown=5, mass_opened=True)
    assert (await one(engine, SNAPSHOT_ROW, s=opening)).absent_count == 20
    followup, effect = await taken(engine, board20, set(), d1 + 18 * H)
    assert effect == SnapshotEffect(mass_followup=True)
    row = await one(engine, SNAPSHOT_ROW, s=followup)
    assert (row.status, row.event_snapshot_id, row.absent_count) == ("mass_disappearance", opening, 20)
    assert tuple(await one(engine, INSTANCE, i=board20.instance_id)) == (20, opening)


async def test_postings_unknown_before_the_last_complete_snapshot_still_never_count(
    engine, current_epoch, board20
) -> None:
    """FR-d keeps T19-b's exclusion: 12 of 20 go `unknown` through an incomplete read, a complete snapshot
    with the 8 others follows (12 of 20 absent: a mass disappearance) and its event clears at a complete
    snapshot with 12 present. The `unknown` postings left over from before that clearing snapshot are not
    part of its baseline, so later complete snapshots without them open no new event."""
    d1 = (utcnow() - 10 * DAY).replace(hour=0, minute=0, second=0, microsecond=0)
    await sighted_at(engine, board20.instance_id, d1 - DAY)
    every = set(board20.postings)
    await taken(engine, board20, every, d1)
    eight = {f"p{k:02d}" for k in range(13, 21)}
    await taken(engine, board20, eight, d1 + 6 * H, complete=False)  # p01..p12 unknown
    opening, effect = await taken(engine, board20, eight, d1 + 12 * H)
    assert effect.mass_opened and (await one(engine, SNAPSHOT_ROW, s=opening)).absent_count == 12
    twelve = eight | {"p01", "p02", "p03", "p04"}
    _, effect = await taken(engine, board20, twelve, d1 + 18 * H)  # 8 of 20 absent: the event clears
    assert effect.mass_cleared
    assert tuple(await one(engine, INSTANCE, i=board20.instance_id)) == (12, None)
    again, effect = await taken(engine, board20, twelve, d1 + 24 * H)
    assert not (effect.mass_opened or effect.mass_followup)
    row = await one(engine, SNAPSHOT_ROW, s=again)
    assert (row.status, row.absent_count) == ("complete", 0)
