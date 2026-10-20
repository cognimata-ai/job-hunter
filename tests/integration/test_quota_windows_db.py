import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db.sql import CHECK_VIOLATION, PgErrorInfo, pg_error
from jobhunter.db.transitions import TRANSITIONS
from jobhunter.quota import windows
from jobhunter.quota.windows import (
    AccountObservation,
    Provider,
    Window,
    apply_changes,
    apply_observation_pure,
    current_snapshot,
    current_window_locked,
    save_window,
)
from jobhunter.sources.base import Observation
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.pg import blocked_or_done
from tests.support.procs import await_until
from tests.support.quota import SETTINGS

pytestmark = pytest.mark.integration
SIX_H = 21600
ROWS = sa.text(
    "SELECT window_start, is_current, closed_reason, state FROM quota_windows WHERE provider = :p "
    "ORDER BY window_start"
)


async def rows(engine: AsyncEngine, provider: str) -> list[sa.Row[tuple[object, ...]]]:
    async with engine.connect() as c:
        return list((await c.execute(ROWS, {"p": provider})).all())


async def locked(engine: AsyncEngine, provider: Provider, at) -> Window:
    async with engine.begin() as c:
        w = await current_window_locked(c, provider, at, SETTINGS, interval_s=SIX_H)
    assert w is not None
    return w


async def snapshot(engine: AsyncEngine, provider: Provider) -> Window:
    async with engine.connect() as c:
        w = await current_snapshot(c, provider)
    assert w is not None
    return w


async def test_bootstrap_and_rollover_are_persisted_once(engine) -> None:
    now = utcnow()
    async with engine.begin() as c:
        w = await current_window_locked(c, "adzuna", now, SETTINGS, interval_s=21600)
        assert w is not None and w.is_current
        assert await current_window_locked(c, "serpapi", now, SETTINGS, interval_s=21600) is None
    async with engine.begin() as c:
        w2 = await current_window_locked(c, "adzuna", now + timedelta(days=61), SETTINGS, interval_s=21600)
    found = await rows(engine, "adzuna")
    assert [(r.is_current, r.closed_reason) for r in found] == [
        (False, "rolled_over"),
        (False, "rolled_over"),
        (True, None),
    ]
    assert w2 is not None and w2.window_start == found[2].window_start
    assert (await snapshot(engine, "adzuna")).window_start == w2.window_start
    again = await locked(engine, "adzuna", now + timedelta(days=61))  # nothing left to roll: no new row
    assert again == w2 and len(await rows(engine, "adzuna")) == 3
    async with engine.connect() as c:
        assert await current_snapshot(c, "serpapi") is None and await current_snapshot(c, "jsearch") is None


async def test_every_column_round_trips(engine) -> None:
    now = utcnow()
    w = await locked(engine, "jsearch", now)
    assert await snapshot(engine, "jsearch") == w  # the bootstrap row is exactly the returned window
    w.tokens, w.tokens_at, w.probe_used, w.next_probe_at = (
        Decimal("1.9067"),
        now,
        True,
        now + timedelta(hours=12),
    )
    w.state, w.last_observed_at, w.observed_remaining, w.observed_limit = "validated", now, 150, 200
    w.observed_this_hour, w.observed_hourly_cap, w.hour_observed_at = 3, 250, now
    w.conservative_until, w.limit_units, w.reserve_units = now + timedelta(days=1), 150, 15
    w.window_end = now + timedelta(days=20)
    async with engine.begin() as c:
        await save_window(c, w)
    assert await snapshot(engine, "jsearch") == w


async def test_a_state_change_outside_the_machine_is_refused_before_the_statement(engine) -> None:
    w = await locked(engine, "adzuna", utcnow())  # validated
    async with engine.begin() as c:
        with pytest.raises(
            ValueError, match="disallowed transition quota_windows: validated -> extrapolated"
        ):
            await save_window(c, replace(w, state="extrapolated_unvalidated", tokens=Decimal(0)))
    assert await snapshot(engine, "adzuna") == w  # nothing was written
    exhausted = replace(w, state="exhausted_until_reset")
    async with engine.begin() as c:
        await save_window(c, exhausted)  # validated -> exhausted_until_reset is an edge
    assert (await snapshot(engine, "adzuna")).state == "exhausted_until_reset"


async def test_a_closed_window_never_changes_state(engine) -> None:
    now = utcnow()
    first = await locked(engine, "adzuna", now)
    await locked(engine, "adzuna", now + timedelta(days=31))  # closes `first` as rolled_over
    closed = replace(first, is_current=False, closed_reason="rolled_over")
    async with engine.begin() as c:
        with pytest.raises(ValueError, match="closed"):
            await save_window(c, replace(closed, state="exhausted_until_reset"))
    async with engine.begin() as c:  # its other columns may still be written (a late settlement, Task 8)
        await save_window(c, replace(closed, probe_failed=True))
    assert [(r.is_current, r.state) for r in await rows(engine, "adzuna")] == [
        (False, "validated"),
        (True, "validated"),
    ]


async def test_the_trigger_rejects_a_disallowed_edge_on_the_persist_path(engine, monkeypatch) -> None:
    w = await locked(engine, "adzuna", utcnow())  # validated
    # A registry that drifted from the database matrix and wrongly allows the edge: the pre-check passes, and
    # the `quota_windows_assert_transition` trigger (§7.16.8 edges in `transition_matrix`) still refuses it.
    spec = TRANSITIONS["quota_windows"]
    drifted = replace(spec, allowed=spec.allowed | {("validated", "extrapolated_unvalidated")})
    monkeypatch.setattr(windows, "TRANSITIONS", {"quota_windows": drifted})
    async with engine.begin() as c:
        with pytest.raises(sa.exc.IntegrityError) as bad:
            await save_window(c, replace(w, state="extrapolated_unvalidated"))
    assert pg_error(bad.value) == PgErrorInfo(CHECK_VIOLATION, None)  # the trigger names no constraint
    assert "disallowed transition quota_windows: validated -> extrapolated_unvalidated" in str(bad.value)
    assert (await snapshot(engine, "adzuna")).state == "validated"


async def test_a_provider_reset_is_persisted_as_close_and_insert(engine) -> None:
    now = utcnow()
    w = await locked(engine, "jsearch", now)
    obs = Observation(
        now + timedelta(days=20), 200, reset_at=now + timedelta(days=50), limit=100, remaining=99
    )
    async with engine.begin() as c:
        cur = await current_window_locked(c, "jsearch", now + timedelta(days=20), SETTINGS, interval_s=SIX_H)
        assert cur == w
        new, changes, fx = apply_observation_pure(
            cur, obs, SETTINGS, provider="jsearch", used_units=0, entry_units=1
        )
        await apply_changes(c, changes)
        await save_window(c, new)
    assert fx.reattribute and fx.adjustment_units == 0
    assert [
        (r.window_start, r.is_current, r.closed_reason, r.state) for r in await rows(engine, "jsearch")
    ] == [
        (w.window_start, False, "provider_reset_observed", "extrapolated_unvalidated"),
        (obs.observed_at, True, None, "validated"),
    ]
    assert await snapshot(engine, "jsearch") == new and (new.limit_units, new.reserve_units) == (100, 10)


@pytest.mark.parametrize("provider", ["adzuna", "serpapi"])
async def test_a_rollover_racing_another_waits_and_takes_the_winners_window(
    engine, migrated, provider
) -> None:
    now = utcnow()
    account = AccountObservation(now, now + timedelta(days=10), 250, 250, 0, 250)
    async with engine.begin() as c:
        assert await current_window_locked(c, provider, now, SETTINGS, interval_s=SIX_H, account=account)
    later = now + timedelta(days=31)
    async with engine.connect() as holder:
        tx = await holder.begin()
        winner = await current_window_locked(
            holder, provider, later, SETTINGS, interval_s=SIX_H
        )  # rolls over
        rival = asyncio.create_task(locked(engine, provider, later))  # SerpApi: no account to bootstrap from
        try:
            await await_until(lambda: blocked_or_done(migrated, rival), 5)
            waited = not rival.done()  # it waits for the current row instead of reading around it
        finally:
            await tx.commit()
    # After the wait, READ COMMITTED re-checks `is_current` on the row it waited for (now closed) and does not
    # see the winner's new row: the rival must look again, neither bootstrapping a second current window
    # (Adzuna) nor finding no window at all (SerpApi without an account read).
    taken = await asyncio.wait_for(rival, 5)
    assert waited and winner is not None and taken == winner
    assert [(r.is_current, r.closed_reason) for r in await rows(engine, provider)][-2:] == [
        (False, "rolled_over"),
        (True, None),
    ]


async def test_a_bootstrap_racing_another_waits_and_takes_the_winners_window(engine, migrated) -> None:
    now = utcnow()
    async with engine.connect() as holder:
        tx = await holder.begin()
        winner = await current_window_locked(holder, "jsearch", now, SETTINGS, interval_s=SIX_H)
        rival = asyncio.create_task(locked(engine, "jsearch", now + timedelta(seconds=1)))
        try:
            await await_until(lambda: blocked_or_done(migrated, rival), 5)
            waited = not rival.done()  # its insert waits for the uncommitted current row
        finally:
            await tx.commit()
    taken = await asyncio.wait_for(rival, 5)
    assert waited and winner is not None and taken == winner
    assert [(r.window_start, r.is_current) for r in await rows(engine, "jsearch")] == [(now, True)]
