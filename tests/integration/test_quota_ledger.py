from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.errors import FencedOut
from jobhunter.quota import ledger
from jobhunter.quota.fallback import eligible
from jobhunter.quota.pacing import Admit, Deny, PlannedSpec, QuotaClass, capacity
from jobhunter.quota.windows import AccountObservation, current_snapshot, current_window_locked
from jobhunter.sources.base import Observation
from jobhunter.worker import reconcilers, runs
from jobhunter.worker.tasks import defer_in_tx
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.collect import ctx, planned_request, queued_run, request_sent
from tests.support.logs import logged
from tests.support.quota import SETTINGS, account, insert_window, jsearch_window

pytestmark = pytest.mark.integration
K = bytes(32)
SIX_H = 21600
JSEARCH_WINDOW = "FROM quota_windows WHERE provider = 'jsearch' AND is_current"


def key(n: int) -> bytes:
    return n.to_bytes(32, "big")


async def one(engine: AsyncEngine, statement: str, **params: object) -> tuple[object, ...]:
    async with engine.connect() as c:
        return tuple((await c.execute(sa.text(statement), params)).one())


async def scalar(engine: AsyncEngine, statement: str, **params: object) -> object:
    async with engine.connect() as c:
        return (await c.execute(sa.text(statement), params)).scalar_one()


async def open_alerts(engine: AsyncEngine) -> dict[str, tuple[str, int]]:
    async with engine.connect() as c:
        rows = await c.execute(
            sa.text("SELECT dedup_key, severity, occurrences FROM alerts WHERE resolved_at IS NULL")
        )
        return {r.dedup_key: (r.severity, r.occurrences) for r in rows}


def reservation(r: ledger.Reservation | ledger.Denial) -> ledger.Reservation:
    assert isinstance(r, ledger.Reservation), r
    return r


async def test_reserve_dispatch_settle_and_replay_order(engine, current_epoch) -> None:
    owner = await queued_run(engine)
    fr = await planned_request(engine, owner, provider="jsearch", key=K, attempt_seq=1)
    now = utcnow()
    async with engine.begin() as c:
        r = await ledger.reserve(
            c,
            "jsearch",
            owner=owner,
            request_key=K,
            attempt_seq=1,
            request_class="normal",
            fetch_request_id=fr,
            settings=SETTINGS,
            interval_s=21600,
            now=now,
        )
    assert (
        isinstance(r, ledger.Reservation) and r.request_class == "probe"
    )  # the first request of an unvalidated window
    async with engine.begin() as c:
        await ledger.mark_dispatched(c, r.entry_id, owner)
    async with engine.begin() as c:
        await ledger.settle(
            c,
            r.entry_id,
            state="billed",
            evidence=None,
            http_status=200,
            provider_remaining=199,
            settings=SETTINGS,
            interval_s=21600,
            now=now,
            observation=Observation(
                observed_at=now, status=200, reset_at=now + timedelta(days=30), limit=200, remaining=199
            ),
        )
    async with engine.connect() as c:
        state, pr = (
            await c.execute(
                sa.text("SELECT state, provider_remaining FROM quota_ledger WHERE id=:i"), {"i": r.entry_id}
            )
        ).one()
        w = (await c.execute(sa.text(f"SELECT state, probe_used, next_probe_at {JSEARCH_WINDOW}"))).one()
        latest = await ledger.latest_attempt(c, "jsearch", K)
    assert (state, pr, tuple(w)) == ("billed", 199, ("validated", False, None)) and latest is not None
    assert latest.attempt_seq == 1 and latest.state == "billed"
    assert (
        latest.entry_id,
        latest.fetch_request_id,
        latest.run_id,
        latest.run_attempt_no,
        latest.dispatched,
    ) == (
        r.entry_id,
        fr,
        owner.run_id,
        1,
        True,
    )
    async with engine.begin() as c:
        with pytest.raises(FencedOut):
            await ledger.mark_dispatched(c, r.entry_id, owner)  # already dispatched: 0 rows
    async with engine.begin() as c:  # a settled entry is never settled again (a late, second classification)
        with pytest.raises(FencedOut):
            await ledger.settle(
                c,
                r.entry_id,
                state="ambiguous",
                evidence=None,
                http_status=None,
                provider_remaining=None,
                observation=None,
                settings=SETTINGS,
                interval_s=21600,
                now=now,
            )
    async with engine.connect() as c:
        assert await ledger.latest_attempt(c, "jsearch", bytes(31) + b"\x01") is None
        assert await ledger.used_units(c, "jsearch", r.window_start) == 1


async def test_superseded_owner_cannot_reserve_or_dispatch_and_is_released(
    engine, current_epoch, queue_app
) -> None:
    owner = await queued_run(engine)
    fr = await planned_request(engine, owner, provider="jsearch", key=K, attempt_seq=1)
    async with engine.begin() as c:
        r = await ledger.reserve(
            c,
            "jsearch",
            owner=owner,
            request_key=K,
            attempt_seq=1,
            request_class="normal",
            fetch_request_id=fr,
            settings=SETTINGS,
            interval_s=21600,
            now=utcnow(),
        )
    assert isinstance(r, ledger.Reservation)
    async with engine.begin() as c:
        await runs.begin_new_attempt(
            c, owner.run_id, epoch=1, end_reason="heartbeat_stale", defer=defer_in_tx
        )  # C28: the old owner is superseded
    async with engine.begin() as c:
        with pytest.raises(FencedOut):
            await ledger.mark_dispatched(c, r.entry_id, owner)
    fr2 = await planned_request(engine, owner, provider="jsearch", key=key(2), attempt_seq=1)
    async with engine.begin() as c:  # nor can it reserve: nothing is written
        with pytest.raises(FencedOut):
            await ledger.reserve(
                c,
                "jsearch",
                owner=owner,
                request_key=key(2),
                attempt_seq=1,
                request_class="normal",
                fetch_request_id=fr2,
                settings=SETTINGS,
                interval_s=21600,
                now=utcnow(),
            )
    assert await scalar(engine, "SELECT count(*) FROM quota_ledger") == 1
    # younger than 15 minutes: left alone
    assert await ledger.reconcile_ledger_unsettled(ctx(engine, queue_app)) == 0
    async with engine.begin() as c:
        await c.execute(sa.text("UPDATE quota_ledger SET created_at = created_at - interval '16 minutes'"))
    assert await ledger.reconcile_ledger_unsettled(ctx(engine, queue_app)) == 1
    async with engine.connect() as c:
        assert tuple(
            (
                await c.execute(
                    sa.text("SELECT state, evidence FROM quota_ledger WHERE id=:i"), {"i": r.entry_id}
                )
            ).one()
        ) == ("not_billed", "never_dispatched")
        assert tuple(
            (
                await c.execute(
                    sa.text("SELECT status, skip_reason FROM fetch_requests WHERE id=:i"), {"i": fr}
                )
            ).one()
        ) == ("skipped", "superseded")
        assert await ledger.used_units(c, "jsearch", r.window_start) == 0
    assert await ledger.reconcile_ledger_unsettled(ctx(engine, queue_app)) == 0  # idempotent


async def test_a_superseded_workers_ledger_reconciler_raises_its_fence_and_stops_the_cadence_there(
    engine, current_epoch, queue_app, monkeypatch, caplog
) -> None:
    """Final review C2 audit, fix wave F7 (Ruling FR-r): `ledger_unsettled` under a superseded epoch writes
    nothing and lets the worker's own fence propagate, so `run_reconcilers` stops the cadence at this
    reconciler (its count is not reported, the reconcilers after it do not run, no `reconciler.failed`
    resolve is attempted) and the superseded worker requests its stop."""
    owner = await queued_run(engine)
    fr = await planned_request(engine, owner, provider="jsearch", key=K, attempt_seq=1)
    async with engine.begin() as c:
        r = await ledger.reserve(
            c,
            "jsearch",
            owner=owner,
            request_key=K,
            attempt_seq=1,
            request_class="normal",
            fetch_request_id=fr,
            settings=SETTINGS,
            interval_s=21600,
            now=utcnow(),
        )
    assert isinstance(r, ledger.Reservation)
    async with engine.begin() as c:  # the owner is superseded: the reservation is the reconciler's to release
        await runs.begin_new_attempt(
            c, owner.run_id, epoch=1, end_reason="heartbeat_stale", defer=defer_in_tx
        )
    async with engine.begin() as c:
        await c.execute(sa.text("UPDATE quota_ledger SET created_at = created_at - interval '16 minutes'"))
        await c.execute(sa.text("UPDATE worker_heartbeat SET worker_epoch = worker_epoch + 1"))  # a takeover
    superseded = ctx(engine, queue_app, epoch=current_epoch)
    with pytest.raises(FencedOut) as fenced:
        await ledger.reconcile_ledger_unsettled(superseded)
    assert fenced.value.worker_superseded
    assert await one(engine, "SELECT state FROM quota_ledger WHERE id = :i", i=r.entry_id) == ("reserved",)

    ran: list[str] = []

    async def after(_: reconcilers.ReconcileContext) -> int:
        ran.append("after")
        return 1

    async def resolve_attempted(*args: object, **kwargs: object) -> None:
        ran.append("resolve")

    stops: list[str] = []
    monkeypatch.setattr(reconcilers, "REGISTRY", {})
    monkeypatch.setattr(reconcilers, "_resolve_failure", resolve_attempted)
    reconcilers.reconciler("ledger_unsettled", "tick")(ledger.reconcile_ledger_unsettled)
    reconcilers.reconciler("after", "tick")(after)
    cadence = replace(superseded, on_superseded=lambda: stops.append("stop"))
    assert await reconcilers.run_reconcilers(cadence, "tick") == {}  # no count: it stopped at the reconciler
    assert ran == [] and stops == ["stop"]  # neither its resolve nor the next reconciler ran
    assert [(e["cadence"], e["reconciler"]) for e in logged(caplog, "reconcilers_fenced")] == [
        ("tick", "ledger_unsettled")
    ]
    assert logged(caplog, "reconciler_failed") == []
    assert await one(engine, "SELECT state FROM quota_ledger WHERE id = :i", i=r.entry_id) == ("reserved",)
    assert await scalar(engine, "SELECT count(*) FROM alerts WHERE code = 'reconciler.failed'") == 0
    # the current epoch's reconciler releases it
    assert await ledger.reconcile_ledger_unsettled(ctx(engine, queue_app, epoch=current_epoch + 1)) == 1


async def test_a_live_owners_reservation_is_kept_and_a_global_purge_fence_refuses(
    engine, current_epoch, queue_app
) -> None:
    owner = await queued_run(engine)
    fr = await planned_request(engine, owner, provider="adzuna", key=K, attempt_seq=1)
    async with engine.begin() as c:
        r = reservation(
            await ledger.reserve(
                c,
                "adzuna",
                owner=owner,
                request_key=K,
                attempt_seq=1,
                request_class="normal",
                fetch_request_id=fr,
                settings=SETTINGS,
                interval_s=SIX_H,
                now=utcnow(),
            )
        )
        await c.execute(sa.text("UPDATE quota_ledger SET created_at = created_at - interval '16 minutes'"))
    # the owner still holds the run: its reservation is not released, however old
    assert await ledger.reconcile_ledger_unsettled(ctx(engine, queue_app)) == 0
    assert await one(engine, "SELECT state FROM quota_ledger WHERE id = :i", i=r.entry_id) == ("reserved",)
    async with engine.begin() as c:
        await c.execute(sa.text("INSERT INTO purge_fences (kind, manifest_id) VALUES ('global', 1)"))
    async with engine.begin() as c:
        with pytest.raises(FencedOut):
            await ledger.mark_dispatched(c, r.entry_id, owner)
    fr2 = await planned_request(engine, owner, provider="adzuna", key=key(2), attempt_seq=1)
    async with engine.begin() as c:
        with pytest.raises(FencedOut):
            await ledger.reserve(
                c,
                "adzuna",
                owner=owner,
                request_key=key(2),
                attempt_seq=1,
                request_class="normal",
                fetch_request_id=fr2,
                settings=SETTINGS,
                interval_s=SIX_H,
                now=utcnow(),
            )
    async with engine.begin() as c:
        await c.execute(sa.text("UPDATE purge_fences SET released_at = now()"))
    async with engine.begin() as c:
        await ledger.mark_dispatched(c, r.entry_id, owner)  # the fence is released: dispatch goes ahead


async def test_recovery_restores_probe_bookkeeping_on_a_fresh_window(
    engine, current_epoch, queue_app
) -> None:  # C1-C3 against an absent window
    owner, now = await queued_run(engine), utcnow()
    fr1 = await planned_request(engine, owner, provider="jsearch", key=K, attempt_seq=1)
    async with engine.begin() as c:  # the first reservation bootstraps the window and is the probe
        r1 = await ledger.reserve(
            c,
            "jsearch",
            owner=owner,
            request_key=K,
            attempt_seq=1,
            request_class="normal",
            fetch_request_id=fr1,
            settings=SETTINGS,
            interval_s=21600,
            now=now,
        )
    assert isinstance(r1, ledger.Reservation) and r1.request_class == "probe"
    async with engine.begin() as c:
        assert await ledger.release_never_dispatched(c, r1.entry_id, now=now)  # C1: never sent
    async with engine.connect() as c:
        w = (
            await c.execute(sa.text(f"SELECT probe_used, probe_failed, next_probe_at {JSEARCH_WINDOW}"))
        ).one()
    assert tuple(w) == (False, False, None)
    fr2 = await planned_request(engine, owner, provider="jsearch", key=K, attempt_seq=2)
    async with engine.begin() as c:  # admission is open again: a new probe, not awaiting_window_validation
        r2 = await ledger.reserve(
            c,
            "jsearch",
            owner=owner,
            request_key=K,
            attempt_seq=2,
            request_class="normal",
            fetch_request_id=fr2,
            settings=SETTINGS,
            interval_s=21600,
            now=now,
        )
        assert isinstance(r2, ledger.Reservation) and r2.request_class == "probe"
        await ledger.mark_dispatched(c, r2.entry_id, owner)
    async with engine.begin() as c:
        assert await ledger.mark_ambiguous(c, r2.entry_id, now=now)  # C2/C3: sent, outcome unknown
    async with engine.connect() as c:
        w = (await c.execute(sa.text(f"SELECT probe_failed, next_probe_at {JSEARCH_WINDOW}"))).one()
        out = await ledger.project_allowance(
            c, "jsearch", [PlannedSpec("normal")], now + timedelta(hours=1), SETTINGS, interval_s=21600
        )
    assert isinstance(out[0], Deny)
    assert (
        tuple(w) == (True, now + ledger.PROBE_RETRY)
        and out[0].code == "awaiting_window_validation"
        and eligible("awaiting_after_failed_probe")
    )
    async with engine.connect() as c:
        later = await ledger.project_allowance(
            c, "jsearch", [PlannedSpec("normal")], now + timedelta(hours=13), SETTINGS, interval_s=21600
        )
    assert isinstance(later[0], Admit) and later[0].request_class == "probe"
    assert await open_alerts(engine) == {"quota.ambiguous_attempt:provider:jsearch": ("warn", 1)}
    async with engine.begin() as c:  # each recovery write happens once
        assert not await ledger.release_never_dispatched(c, r1.entry_id, now=now)
        assert not await ledger.mark_ambiguous(c, r2.entry_id, now=now)

    # The reconciler takes the same two paths: a stale reserved probe is never_sent, a dispatched one failed.
    at = now + timedelta(hours=13)
    fr3 = await planned_request(engine, owner, provider="jsearch", key=key(3), attempt_seq=1)
    async with engine.begin() as c:
        r3 = reservation(
            await ledger.reserve(
                c,
                "jsearch",
                owner=owner,
                request_key=key(3),
                attempt_seq=1,
                request_class="normal",
                fetch_request_id=fr3,
                settings=SETTINGS,
                interval_s=21600,
                now=at,
            )
        )
    assert r3.request_class == "probe"
    async with engine.begin() as c:
        await runs.begin_new_attempt(
            c, owner.run_id, epoch=1, end_reason="heartbeat_stale", defer=defer_in_tx
        )
    assert (
        await ledger.reconcile_ledger_unsettled(ctx(engine, queue_app, now=at + timedelta(minutes=16))) == 1
    )
    snap = await one(
        engine,
        f"SELECT probe_used, probe_failed, next_probe_at {JSEARCH_WINDOW}",
    )
    assert snap == (False, False, None)  # never_sent: the next request probes at once
    second = runs.RunOwner(owner.run_id, 2)
    fr4 = await planned_request(engine, second, provider="jsearch", key=key(4), attempt_seq=1)
    async with engine.begin() as c:
        r4 = reservation(
            await ledger.reserve(
                c,
                "jsearch",
                owner=second,
                request_key=key(4),
                attempt_seq=1,
                request_class="normal",
                fetch_request_id=fr4,
                settings=SETTINGS,
                interval_s=21600,
                now=at,
            )
        )
        assert r4.request_class == "probe"
        await request_sent(c, fr4, r4.entry_id)
        await ledger.mark_dispatched(c, r4.entry_id, second)
        await c.execute(
            sa.text("UPDATE quota_ledger SET dispatched_at = :t WHERE id = :i"), {"t": at, "i": r4.entry_id}
        )
    recovered_at = at + timedelta(minutes=16)
    assert (
        await ledger.reconcile_ledger_unsettled(ctx(engine, queue_app, now=recovered_at)) == 1
    )  # the live owner's too
    assert await one(engine, "SELECT state FROM quota_ledger WHERE id = :i", i=r4.entry_id) == ("ambiguous",)
    assert await one(engine, "SELECT status, error_code FROM fetch_requests WHERE id = :i", i=fr4) == (
        "ambiguous",
        "unsettled",
    )
    snap = await one(
        engine,
        f"SELECT probe_failed, next_probe_at {JSEARCH_WINDOW}",
    )
    assert snap == (True, recovered_at + ledger.PROBE_RETRY)  # failed: the next probe 12 h later
    assert (await open_alerts(engine))["quota.ambiguous_attempt:provider:jsearch"] == ("warn", 2)


async def test_settle_reconciles_the_observation_in_order(engine, current_epoch) -> None:
    owner, now = await queued_run(engine), utcnow()
    await insert_window(
        engine, jsearch_window(now - timedelta(days=1), limit=200, reserve=20, tokens=2, interval_s=SIX_H)
    )
    entries = []
    for n in (1, 2, 3):
        fr = await planned_request(engine, owner, provider="jsearch", key=key(n), attempt_seq=1)
        async with engine.begin() as c:
            r = reservation(
                await ledger.reserve(
                    c,
                    "jsearch",
                    owner=owner,
                    request_key=key(n),
                    attempt_seq=1,
                    request_class="retry",
                    fetch_request_id=fr,
                    settings=SETTINGS,
                    interval_s=SIX_H,
                    now=now,
                )
            )
            await ledger.mark_dispatched(c, r.entry_id, owner)
        entries.append(r)
    end = now + timedelta(days=20)
    async with (
        engine.begin() as c
    ):  # provider: limit 250 (higher: info only), 190 of 200 left -> 10 used, 3 counted
        await ledger.settle(
            c,
            entries[0].entry_id,
            state="billed",
            evidence=None,
            http_status=200,
            provider_remaining=190,
            observation=Observation(now, 200, reset_at=end, limit=250, remaining=190),
            settings=SETTINGS,
            interval_s=SIX_H,
            now=now,
        )
    adjustments = "SELECT units, evidence FROM quota_ledger WHERE kind = 'adjustment' ORDER BY id"
    async with engine.connect() as c:
        rows = [tuple(r) for r in (await c.execute(sa.text(adjustments))).all()]
        assert (
            await ledger.used_units(c, "jsearch", entries[0].window_start) == 3 + 7
        )  # three requests counted + 7
    assert rows == [(7, "provider_remaining=190")]
    assert await open_alerts(engine) == {
        "quota.limit_increased:provider:jsearch": ("info", 1),
        "quota.unaccounted_usage:provider:jsearch": ("warn", 1),
    }
    async with engine.begin() as c:  # a late response (observed before the last one) is ignored entirely
        await ledger.settle(
            c,
            entries[1].entry_id,
            state="billed",
            evidence=None,
            http_status=200,
            provider_remaining=100,
            observation=Observation(now - timedelta(seconds=1), 200, reset_at=end, limit=100, remaining=100),
            settings=SETTINGS,
            interval_s=SIX_H,
            now=now,
        )
    assert await one(
        engine, "SELECT limit_units, window_end FROM quota_windows WHERE provider = 'jsearch' AND is_current"
    ) == (200, end)
    later = now + timedelta(minutes=1)
    async with (
        engine.begin() as c
    ):  # a lower limit lowers limit and reserve; remaining that is larger never refunds
        await ledger.settle(
            c,
            entries[2].entry_id,
            state="not_billed",
            evidence="rapidapi_gateway_404",
            http_status=404,
            provider_remaining=150,
            observation=Observation(later, 404, reset_at=end, limit=150, remaining=150),
            settings=SETTINGS,
            interval_s=SIX_H,
            now=later,
        )
    assert await one(
        engine,
        "SELECT limit_units, reserve_units FROM quota_windows WHERE provider = 'jsearch' AND is_current",
    ) == (150, 15)
    async with engine.connect() as c:
        assert (
            await ledger.used_units(c, "jsearch", entries[0].window_start) == 2 + 7
        )  # the gateway 404 is refunded
    assert await scalar(engine, "SELECT count(*) FROM quota_ledger WHERE kind = 'adjustment'") == 1


async def test_a_provider_reset_reattributes_the_entry_with_a_full_bucket(engine, current_epoch) -> None:
    owner, now = await queued_run(engine), utcnow()
    old = jsearch_window(now - timedelta(days=29), limit=200, reserve=20, tokens=0, interval_s=SIX_H)
    await insert_window(engine, old)
    fr = await planned_request(engine, owner, provider="jsearch", key=K, attempt_seq=1)
    async with engine.begin() as c:
        r = reservation(
            await ledger.reserve(
                c,
                "jsearch",
                owner=owner,
                request_key=K,
                attempt_seq=1,
                request_class="retry",
                fetch_request_id=fr,
                settings=SETTINGS,
                interval_s=SIX_H,
                now=now,
            )
        )
        await ledger.mark_dispatched(c, r.entry_id, owner)
    reset = now + timedelta(
        days=30
    )  # at least half a period past the old end: the provider has reset already
    async with engine.begin() as c:
        await ledger.settle(
            c,
            r.entry_id,
            state="billed",
            evidence=None,
            http_status=200,
            provider_remaining=199,
            observation=Observation(now, 200, reset_at=reset, limit=200, remaining=199),
            settings=SETTINGS,
            interval_s=SIX_H,
            now=now,
        )
    async with engine.connect() as c:
        rows = (
            await c.execute(
                sa.text(
                    "SELECT window_start, is_current, closed_reason FROM quota_windows ORDER BY window_start"
                )
            )
        ).all()
        cur = await current_snapshot(c, "jsearch")
        entry_window = (
            await c.execute(sa.text("SELECT window_start FROM quota_ledger WHERE id = :i"), {"i": r.entry_id})
        ).scalar_one()
        assert cur is not None and await ledger.used_units(c, "jsearch", cur.window_start) == 1
    assert [tuple(row) for row in rows] == [
        (old.window_start, False, "provider_reset_observed"),
        (now, True, None),
    ]
    assert entry_window == now and cur.state == "validated" and cur.window_end == reset
    assert (
        cur.tokens == cur.capacity == capacity(cur, SIX_H)
    )  # the new window's full bucket at its own capacity
    assert (
        await scalar(engine, "SELECT count(*) FROM quota_ledger WHERE kind = 'adjustment'") == 0
    )  # 1 used = the entry


async def test_a_jsearch_429_exhausts_until_a_validated_observation(engine, current_epoch) -> None:
    owner, now = await queued_run(engine), utcnow()
    end = now + timedelta(days=10)
    await insert_window(
        engine,
        replace(
            jsearch_window(now - timedelta(days=1), limit=200, reserve=20, tokens=2, interval_s=SIX_H),
            window_end=end,
        ),
    )
    fr = await planned_request(engine, owner, provider="jsearch", key=K, attempt_seq=1)
    async with engine.begin() as c:
        r = reservation(
            await ledger.reserve(
                c,
                "jsearch",
                owner=owner,
                request_key=K,
                attempt_seq=1,
                request_class="normal",
                fetch_request_id=fr,
                settings=SETTINGS,
                interval_s=SIX_H,
                now=now,
            )
        )
        await ledger.mark_dispatched(c, r.entry_id, owner)
    async with (
        engine.begin() as c
    ):  # every status but 2xx and the gateway 404 is ambiguous; 429 with remaining 0
        await ledger.settle(
            c,
            r.entry_id,
            state="ambiguous",
            evidence=None,
            http_status=429,
            provider_remaining=0,
            observation=Observation(now, 429, reset_at=end, limit=200, remaining=0),
            settings=SETTINGS,
            interval_s=SIX_H,
            now=now,
        )
    assert await one(
        engine, "SELECT state, window_end FROM quota_windows WHERE provider = 'jsearch' AND is_current"
    ) == ("exhausted_until_reset", end)
    assert await open_alerts(engine) == {
        "quota.exhausted:provider:jsearch": ("warn", 1),
        "quota.ambiguous_attempt:provider:jsearch": ("warn", 1),
        "quota.unaccounted_usage:provider:jsearch": ("warn", 1),
    }
    async with engine.connect() as c:
        out = await ledger.project_allowance(
            c, "jsearch", [PlannedSpec("normal")], now, SETTINGS, interval_s=SIX_H
        )
    assert out == [Deny("exhausted")]
    # a non-probe 429 sets no probe time: the window waits for its reset, which the next window's probe sees
    fr2 = await planned_request(engine, owner, provider="jsearch", key=key(2), attempt_seq=1)
    at = end + timedelta(minutes=1)
    async with engine.begin() as c:
        r2 = reservation(
            await ledger.reserve(
                c,
                "jsearch",
                owner=owner,
                request_key=key(2),
                attempt_seq=1,
                request_class="normal",
                fetch_request_id=fr2,
                settings=SETTINGS,
                interval_s=SIX_H,
                now=at,
            )
        )
        assert r2.request_class == "probe"  # the rolled-over successor is unvalidated
        await ledger.mark_dispatched(c, r2.entry_id, owner)
    async with engine.begin() as c:
        await ledger.settle(
            c,
            r2.entry_id,
            state="billed",
            evidence=None,
            http_status=200,
            provider_remaining=199,
            observation=Observation(at, 200, reset_at=at + timedelta(days=30), limit=200, remaining=199),
            settings=SETTINGS,
            interval_s=SIX_H,
            now=at,
        )
    assert "quota.exhausted:provider:jsearch" not in await open_alerts(engine)


async def test_a_probe_that_refines_its_window_to_seconds_still_admits(engine, current_epoch) -> None:
    # Ruling T8-a: the probe's reset header ends the bootstrapped window 1.5 s after its start; the bucket's
    # capacity of such a window must fit its column (numeric(10,4)), so the next reservation is admitted.
    owner, now = await queued_run(engine), utcnow()
    fr = await planned_request(engine, owner, provider="jsearch", key=K, attempt_seq=1)
    async with engine.begin() as c:
        r = reservation(
            await ledger.reserve(
                c,
                "jsearch",
                owner=owner,
                request_key=K,
                attempt_seq=1,
                request_class="normal",
                fetch_request_id=fr,
                settings=SETTINGS,
                interval_s=SIX_H,
                now=now,
            )
        )
        assert r.request_class == "probe"
        await ledger.mark_dispatched(c, r.entry_id, owner)
    observed = now + timedelta(milliseconds=500)
    async with engine.begin() as c:
        await ledger.settle(
            c,
            r.entry_id,
            state="billed",
            evidence=None,
            http_status=200,
            provider_remaining=199,
            observation=Observation(
                observed, 200, reset_at=now + timedelta(milliseconds=1500), limit=200, remaining=199
            ),
            settings=SETTINGS,
            interval_s=SIX_H,
            now=observed,
        )
    fr2 = await planned_request(engine, owner, provider="jsearch", key=key(1), attempt_seq=1)
    async with engine.begin() as c:
        again = await ledger.reserve(
            c,
            "jsearch",
            owner=owner,
            request_key=key(1),
            attempt_seq=1,
            request_class="normal",
            fetch_request_id=fr2,
            settings=SETTINGS,
            interval_s=SIX_H,
            now=now + timedelta(seconds=1),
        )
    assert isinstance(again, ledger.Reservation) and again.request_class == "normal"
    w = await one(engine, f"SELECT state, window_end, capacity, limit_units {JSEARCH_WINDOW}")
    assert w[:2] == ("validated", now + timedelta(milliseconds=1500))
    assert isinstance(w[2], Decimal) and isinstance(w[3], int) and 1 <= w[2] <= w[3]


async def test_a_probe_that_fails_or_is_never_sent(engine, current_epoch) -> None:
    owner, now = await queued_run(engine), utcnow()
    fr = await planned_request(engine, owner, provider="jsearch", key=K, attempt_seq=1)
    async with engine.begin() as c:
        r = reservation(
            await ledger.reserve(
                c,
                "jsearch",
                owner=owner,
                request_key=K,
                attempt_seq=1,
                request_class="normal",
                fetch_request_id=fr,
                settings=SETTINGS,
                interval_s=SIX_H,
                now=now,
            )
        )
        await ledger.mark_dispatched(c, r.entry_id, owner)
    async with (
        engine.begin() as c
    ):  # a connect error before any byte: positive evidence, the probe never happened
        await ledger.settle(
            c,
            r.entry_id,
            state="not_billed",
            evidence="connect_failed_before_send",
            http_status=None,
            provider_remaining=None,
            observation=None,
            settings=SETTINGS,
            interval_s=SIX_H,
            now=now,
        )
    probe = f"SELECT probe_used, probe_failed, next_probe_at {JSEARCH_WINDOW}"
    assert await one(engine, probe) == (False, False, None)
    fr2 = await planned_request(engine, owner, provider="jsearch", key=K, attempt_seq=2)
    async with engine.begin() as c:
        r2 = reservation(
            await ledger.reserve(
                c,
                "jsearch",
                owner=owner,
                request_key=K,
                attempt_seq=2,
                request_class="normal",
                fetch_request_id=fr2,
                settings=SETTINGS,
                interval_s=SIX_H,
                now=now,
            )
        )
        assert r2.request_class == "probe"
        await ledger.mark_dispatched(c, r2.entry_id, owner)
    async with (
        engine.begin() as c
    ):  # a 2xx whose headers do not validate the window is a failed probe, not a lockout
        await ledger.settle(
            c,
            r2.entry_id,
            state="billed",
            evidence=None,
            http_status=200,
            provider_remaining=None,
            observation=Observation(now, 200),
            settings=SETTINGS,
            interval_s=SIX_H,
            now=now,
        )
    assert await one(engine, probe) == (True, True, now + ledger.PROBE_RETRY)


async def test_serpapi_hour_rule_exclusive_and_foreign(engine, current_epoch) -> None:
    now = utcnow()
    account_now = AccountObservation(
        observed_at=now,
        plan_renewal_date=now + timedelta(days=20),
        searches_per_month=250,
        plan_searches_left=200,
        this_hour_searches=12,
        account_rate_limit_per_hour=250,
    )
    owner = await queued_run(engine)
    renewal = account_now.plan_renewal_date
    # 12 local searches spread over the last hour, each after a read counting the earlier ones (exclusive use)
    for n in range(12):
        at = now - timedelta(minutes=55) + n * timedelta(minutes=4)
        fr = await planned_request(engine, owner, provider="serpapi", key=key(n), attempt_seq=1)
        async with engine.begin() as c:
            reservation(
                await ledger.reserve(
                    c,
                    "serpapi",
                    owner=owner,
                    request_key=key(n),
                    attempt_seq=1,
                    request_class="retry",
                    fetch_request_id=fr,
                    settings=SETTINGS,
                    interval_s=SIX_H,
                    now=at,
                    account=account(at, renewal, left=250 - n, this_hour=n),
                )
            )
    # account reports 12 -> the 13th is admitted (12 + 0 + 1 <= 45)
    fr = await planned_request(engine, owner, provider="serpapi", key=key(12), attempt_seq=1)
    async with engine.begin() as c:
        reservation(
            await ledger.reserve(
                c,
                "serpapi",
                owner=owner,
                request_key=key(12),
                attempt_seq=1,
                request_class="retry",
                fetch_request_id=fr,
                settings=SETTINGS,
                interval_s=SIX_H,
                now=now,
                account=account_now,
            )
        )
    # 200 left: 50 used at the provider against 12 counted here (an adjustment of 38)
    assert await open_alerts(engine) == {"quota.unaccounted_usage:provider:serpapi": ("warn", 1)}
    w = await one(
        engine, "SELECT conservative_until, observed_this_hour FROM quota_windows WHERE provider = 'serpapi'"
    )
    assert w == (None, 12)
    # the reads are free; without a fresh one (older than 60 s), no search is admitted
    fr = await planned_request(engine, owner, provider="serpapi", key=key(13), attempt_seq=1)
    async with engine.begin() as c:
        stale = await ledger.reserve(
            c,
            "serpapi",
            owner=owner,
            request_key=key(13),
            attempt_seq=1,
            request_class="retry",
            fetch_request_id=fr,
            settings=SETTINGS,
            interval_s=SIX_H,
            now=now + timedelta(seconds=61),
        )
    assert stale == ledger.Denial("window_cap")
    # two hours later: the account reports 40 while local usage is 0 -> foreign = 40
    later = now + timedelta(hours=2)
    async with engine.begin() as c:
        foreign = await ledger.reserve(
            c,
            "serpapi",
            owner=owner,
            request_key=key(13),
            attempt_seq=1,
            request_class="retry",
            fetch_request_id=fr,
            settings=SETTINGS,
            interval_s=SIX_H,
            now=later,
            account=account(later, renewal, left=150, this_hour=40),
        )
    assert foreign == ledger.Denial("window_cap")  # 40 + 0 + 1 = 41 > 45 - 5
    assert (await open_alerts(engine))["quota.serpapi_foreign_usage:provider:serpapi"] == ("warn", 1)
    assert await one(engine, "SELECT conservative_until FROM quota_windows WHERE provider = 'serpapi'") == (
        later + timedelta(hours=24),
    )
    # conservative mode admits up to H - 5: at 39 with no local search, one more is admitted (39 + 1 <= 40)
    again = later + timedelta(minutes=1)
    async with engine.begin() as c:
        reservation(
            await ledger.reserve(
                c,
                "serpapi",
                owner=owner,
                request_key=key(13),
                attempt_seq=1,
                request_class="retry",
                fetch_request_id=fr,
                settings=SETTINGS,
                interval_s=SIX_H,
                now=again,
                account=account(again, renewal, left=150, this_hour=39),
            )
        )
    # 24 h after the last detection: normal mode again, and the alert resolves at the next read without
    # foreign use
    day = later + timedelta(hours=25)
    fr = await planned_request(engine, owner, provider="serpapi", key=key(14), attempt_seq=1)
    async with engine.begin() as c:
        reservation(
            await ledger.reserve(
                c,
                "serpapi",
                owner=owner,
                request_key=key(14),
                attempt_seq=1,
                request_class="retry",
                fetch_request_id=fr,
                settings=SETTINGS,
                interval_s=SIX_H,
                now=day,
                account=account(day, renewal, left=149, this_hour=0),
            )
        )
    assert "quota.serpapi_foreign_usage:provider:serpapi" not in await open_alerts(engine)


async def test_adzuna_rolling_minute_cap(engine, current_epoch) -> None:
    owner, now = await queued_run(engine), utcnow()
    results = []
    for n in range(22):  # 22 reservations within the minute, two of which settle not_billed
        at = now + timedelta(seconds=n)
        fr = await planned_request(engine, owner, provider="adzuna", key=key(n), attempt_seq=1)
        async with engine.begin() as c:
            r = await ledger.reserve(
                c,
                "adzuna",
                owner=owner,
                request_key=key(n),
                attempt_seq=1,
                request_class="normal",
                fetch_request_id=fr,
                settings=SETTINGS,
                interval_s=SIX_H,
                now=at,
            )
            results.append(r)
            if n in (3, 7) and isinstance(r, ledger.Reservation):
                await ledger.mark_dispatched(c, r.entry_id, owner)
                await ledger.settle(
                    c,
                    r.entry_id,
                    state="not_billed",
                    evidence="connect_failed_before_send",
                    http_status=None,
                    provider_remaining=None,
                    observation=None,
                    settings=SETTINGS,
                    interval_s=SIX_H,
                    now=at,
                )
    assert all(isinstance(r, ledger.Reservation) for r in results)  # not_billed ones are excluded: 20 counted
    fr = await planned_request(engine, owner, provider="adzuna", key=key(22), attempt_seq=1)
    async with engine.begin() as c:
        denied = await ledger.reserve(
            c,
            "adzuna",
            owner=owner,
            request_key=key(22),
            attempt_seq=1,
            request_class="normal",
            fetch_request_id=fr,
            settings=SETTINGS,
            interval_s=SIX_H,
            now=now + timedelta(seconds=30),
        )
        assert not await ledger.adzuna_windows_ok(c, now + timedelta(seconds=30), 1, SETTINGS)
    assert denied == ledger.Denial("window_cap")
    async with engine.begin() as c:  # the next minute admits again: the first entries left the 60 s window
        again = await ledger.reserve(
            c,
            "adzuna",
            owner=owner,
            request_key=key(22),
            attempt_seq=1,
            request_class="normal",
            fetch_request_id=fr,
            settings=SETTINGS,
            interval_s=SIX_H,
            now=now + timedelta(seconds=61),
        )
    assert isinstance(again, ledger.Reservation)
    async with engine.connect() as c:
        assert await ledger.normal_count_for_run(c, "adzuna", owner.run_id) == 21
        assert await ledger.used_units(c, "adzuna", again.window_start) == 21


async def test_the_per_run_bound_holds_across_attempts_and_a_denial_persists_its_accrual_only(
    engine, current_epoch
) -> None:
    owner, now = await queued_run(engine), utcnow()
    week = 7 * 86400  # a bucket of 81 units: the explicit per-run bound of 2 binds first
    capped = replace(SETTINGS, paid_max_requests_per_run=2)
    await insert_window(engine, jsearch_window(now, limit=200, reserve=20, tokens=50, interval_s=week))

    async def attempt(
        o: runs.RunOwner, n: int, cls: QuotaClass, at: datetime
    ) -> ledger.Reservation | ledger.Denial:
        fr = await planned_request(engine, o, provider="jsearch", key=key(n), attempt_seq=1)
        async with engine.begin() as c:
            return await ledger.reserve(
                c,
                "jsearch",
                owner=o,
                request_key=key(n),
                attempt_seq=1,
                request_class=cls,
                fetch_request_id=fr,
                settings=capped,
                interval_s=week,
                now=at,
            )

    first = [await attempt(owner, n, "normal", now) for n in range(3)]
    assert [r.request_class if isinstance(r, ledger.Reservation) else r.code for r in first] == [
        "normal",
        "normal",
        "paced",
    ]
    async with engine.begin() as c:
        await runs.begin_new_attempt(
            c, owner.run_id, epoch=1, end_reason="heartbeat_stale", defer=defer_in_tx
        )
    second = runs.RunOwner(owner.run_id, 2)
    paced_at = now + timedelta(hours=1)
    assert await attempt(second, 3, "normal", paced_at) == ledger.Denial("paced")  # across the run's attempts
    async with engine.connect() as c:
        assert await ledger.normal_count_for_run(c, "jsearch", owner.run_id) == 2
        assert await ledger.latest_attempt(c, "jsearch", key(3)) is None  # a denial writes no ledger row
    tokens, tokens_at = await one(
        engine, "SELECT tokens, tokens_at FROM quota_windows WHERE provider = 'jsearch' AND is_current"
    )
    assert isinstance(tokens_at, datetime)
    # the denial saved its accrual: an hour's 0.2422 credited, the clock at the credited time (the 0.33 s
    # worth of the rounded-away fraction is credited by the next accrual, fix wave F4, ledger L92)
    assert tokens == Decimal("48.2422") and paced_at - timedelta(seconds=1) < tokens_at < paced_at
    retry = await attempt(second, 4, "retry", paced_at + timedelta(seconds=1))
    assert (
        isinstance(retry, ledger.Reservation) and retry.request_class == "retry"
    )  # retries bypass the bound


async def test_retries_may_use_the_reserve_and_normal_requests_may_not(engine, current_epoch) -> None:
    owner, now = await queued_run(engine), utcnow()
    await insert_window(engine, jsearch_window(now, limit=12, reserve=2, tokens=1, interval_s=SIX_H))
    outcomes = []
    retries: list[QuotaClass] = ["retry"] * 10
    tail: list[QuotaClass] = ["normal", "retry", "retry", "retry"]
    for n, cls in enumerate(retries + tail):
        fr = await planned_request(engine, owner, provider="jsearch", key=key(n), attempt_seq=1)
        async with engine.begin() as c:
            outcomes.append(
                await ledger.reserve(
                    c,
                    "jsearch",
                    owner=owner,
                    request_key=key(n),
                    attempt_seq=1,
                    request_class=cls,
                    fetch_request_id=fr,
                    settings=SETTINGS,
                    interval_s=SIX_H,
                    now=now,
                )
            )
    kinds = [r.request_class if isinstance(r, ledger.Reservation) else r.code for r in outcomes]
    assert kinds == ["retry"] * 10 + ["reserve_held", "retry", "retry", "exhausted"]
    async with engine.connect() as c:
        assert await ledger.used_units(c, "jsearch", now) == 12


async def test_project_allowance_writes_nothing(engine) -> None:
    async with engine.connect() as c:  # no window exists; an expired one is also tried
        out = await ledger.project_allowance(
            c, "jsearch", [PlannedSpec("normal")] * 3, utcnow(), SETTINGS, interval_s=21600
        )
    assert [type(d).__name__ for d in out] == ["Admit"] * 3 and all(
        isinstance(d, Admit) and d.request_class == "probe" for d in out
    )  # §6.7: the simulation never sets probe_used
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT count(*) FROM quota_windows"))).scalar_one() == 0
    expired = jsearch_window(utcnow() - timedelta(days=40), limit=200, reserve=20, tokens=1, interval_s=SIX_H)
    await insert_window(engine, expired)
    async with engine.connect() as c:
        rolled = await ledger.project_allowance(
            c, "jsearch", [PlannedSpec("normal")] * 2, utcnow(), SETTINGS, interval_s=21600
        )
        serpapi = await ledger.project_allowance(
            c, "serpapi", [PlannedSpec("normal")], utcnow(), SETTINGS, interval_s=21600
        )
    assert all(
        isinstance(d, Admit) and d.request_class == "probe" for d in rolled
    )  # the extrapolated successor
    assert serpapi == [Deny("window_cap")]  # SerpApi without an account read
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT count(*) FROM quota_windows"))).scalar_one() == 1
        assert await current_snapshot(c, "jsearch") == expired


# The SerpApi renewal gap (Rulings T7-a, T7-b). The plan renews on Nov 8, which SerpApi applies hours after
# the local rollover at 00:00 UTC; until then every read reports the old renewal date.
OCT = datetime(2026, 10, 20, tzinfo=UTC)
RENEWAL = datetime(2026, 11, 8, tzinfo=UTC)
NEXT = datetime(2026, 12, 8, tzinfo=UTC)


async def october_window(engine: AsyncEngine) -> None:
    """SerpApi bootstrapped on Oct 20 from a read with 240 of 250 left; no search in the rest of the month."""
    async with engine.begin() as c:
        w = await current_window_locked(
            c, "serpapi", OCT, SETTINGS, interval_s=SIX_H, account=account(OCT, RENEWAL, left=240)
        )
    assert w is not None and (w.window_start, w.window_end, w.state) == (
        datetime(2026, 10, 8, tzinfo=UTC),
        RENEWAL,
        "validated",
    )


async def test_a_month_without_serpapi_searches_still_plans_the_fallback_once_a_read_shows_the_renewal(
    engine,
) -> None:
    await october_window(engine)
    in_gap = RENEWAL + timedelta(hours=5)
    specs = [PlannedSpec("normal")] * 2
    async with engine.connect() as c:
        no_read = await ledger.project_allowance(c, "serpapi", specs, in_gap, SETTINGS, interval_s=SIX_H)
        lagging = await ledger.project_allowance(
            c,
            "serpapi",
            specs,
            in_gap,
            SETTINGS,
            interval_s=SIX_H,
            account=account(in_gap, RENEWAL, left=240),
        )
        renewed = await ledger.project_allowance(
            c,
            "serpapi",
            specs,
            in_gap + timedelta(hours=3),
            SETTINGS,
            interval_s=SIX_H,
            account=account(in_gap + timedelta(hours=3), NEXT, left=250),
        )
    assert (
        no_read == lagging == [Deny("window_cap")] * 2
    )  # the rolled-over window is unvalidated: fail closed
    assert renewed == [Admit("normal", tokens_used=1, starts_probe=False)] * 2  # the read validates the copy
    async with engine.connect() as c:  # planning wrote nothing: the stored window is still October's
        stored = await current_snapshot(c, "serpapi")
    assert stored is not None and stored.window_start == datetime(2026, 10, 8, tzinfo=UTC)


async def test_the_serpapi_renewal_stale_alert_is_raised_and_resolved(engine, current_epoch) -> None:
    await october_window(engine)
    owner = await queued_run(engine)
    fr = await planned_request(engine, owner, provider="serpapi", key=K, attempt_seq=1)
    stale_key = "quota.serpapi_renewal_stale:provider:serpapi"

    async def search(at: datetime, renewal: datetime, left: int) -> ledger.Reservation | ledger.Denial:
        async with engine.begin() as c:
            return await ledger.reserve(
                c,
                "serpapi",
                owner=owner,
                request_key=K,
                attempt_seq=1,
                request_class="normal",
                fetch_request_id=fr,
                settings=SETTINGS,
                interval_s=SIX_H,
                now=at,
                account=account(at, renewal, left=left),
            )

    assert await search(RENEWAL + timedelta(hours=47), RENEWAL, 240) == ledger.Denial("window_cap")
    assert stale_key not in await open_alerts(engine)  # 47 h after the window's start: not yet
    assert await search(RENEWAL + timedelta(hours=49), RENEWAL, 240) == ledger.Denial("window_cap")
    assert (await open_alerts(engine))[stale_key] == ("warn", 1)
    renewed = await search(RENEWAL + timedelta(hours=50), NEXT, 250)
    assert isinstance(renewed, ledger.Reservation) and renewed.window_start == RENEWAL
    assert stale_key not in await open_alerts(engine)
    w = await one(
        engine,
        "SELECT state, window_end, limit_units FROM quota_windows WHERE provider = 'serpapi' AND is_current",
    )
    assert w == ("validated", NEXT, 250)
    async with engine.connect() as c:  # nothing of October's usage was charged to the new month
        assert await ledger.used_units(c, "serpapi", RENEWAL) == 1


async def test_a_reservation_sets_the_quota_gauges(engine, current_epoch) -> None:
    from jobhunter.observability.metrics import REGISTRY

    owner, now = await queued_run(engine), utcnow()
    fr = await planned_request(engine, owner, provider="adzuna", key=K, attempt_seq=1)
    async with engine.begin() as c:
        r = reservation(
            await ledger.reserve(
                c,
                "adzuna",
                owner=owner,
                request_key=K,
                attempt_seq=1,
                request_class="normal",
                fetch_request_id=fr,
                settings=SETTINGS,
                interval_s=SIX_H,
                now=now,
            )
        )
    labels = {"provider": "adzuna"}
    assert REGISTRY.get_sample_value("jobhunter_quota_used_units", labels) == 1
    assert REGISTRY.get_sample_value("jobhunter_quota_remaining_units", labels) == 2249
    tokens = await scalar(engine, "SELECT tokens FROM quota_windows WHERE provider = 'adzuna'")
    assert tokens == Decimal("32.7500") and r.window_start == now


# ---- fix wave F4 -------------------------------------------------------------------------------------------

US = timedelta(microseconds=1)

PROBE_COLUMNS = f"SELECT state, probe_used, probe_failed, next_probe_at {JSEARCH_WINDOW}"
VALIDATED_MEANWHILE = sa.text(
    "UPDATE quota_windows SET state = 'validated', probe_used = false, probe_failed = false, "
    "next_probe_at = NULL WHERE provider = 'jsearch' AND is_current"
)


async def settle_one(c, entry_id: int, **values: object) -> None:
    arguments: dict[str, object] = {
        "state": "billed",
        "evidence": None,
        "http_status": 200,
        "provider_remaining": None,
        "observation": None,
        "settings": SETTINGS,
        "interval_s": SIX_H,
        "now": utcnow(),
        **values,
    }
    await ledger.settle(c, entry_id, **arguments)  # type: ignore[arg-type]


async def reserved_probe(engine, owner, now) -> ledger.Reservation:
    """The first reservation of a fresh JSearch window: its probe."""
    fr = await planned_request(engine, owner, provider="jsearch", key=K, attempt_seq=1)
    async with engine.begin() as c:
        r = reservation(
            await ledger.reserve(
                c,
                "jsearch",
                owner=owner,
                request_key=K,
                attempt_seq=1,
                request_class="normal",
                fetch_request_id=fr,
                settings=SETTINGS,
                interval_s=SIX_H,
                now=now,
            )
        )
    assert r.request_class == "probe"
    return r


@pytest.mark.parametrize("dispatched", [True, False])
async def test_recovering_a_probe_of_a_window_validated_meanwhile_leaves_its_probe_columns_alone(
    engine, current_epoch, dispatched: bool
) -> None:
    """Ledger L109: a validated window awaits no probe, so neither recovery write (`mark_ambiguous` of a
    dispatched probe, `release_never_dispatched` of a reserved one) stamps its probe columns: a stale
    `next_probe_at` would otherwise admit an early re-probe after a later 429."""
    owner, now = await queued_run(engine), utcnow()
    r = await reserved_probe(engine, owner, now)
    async with engine.begin() as c:
        if dispatched:
            await ledger.mark_dispatched(c, r.entry_id, owner)
        await c.execute(VALIDATED_MEANWHILE)  # say, by the late response of an earlier entry
    async with engine.begin() as c:
        if dispatched:
            assert await ledger.mark_ambiguous(c, r.entry_id, now=now)
        else:
            assert await ledger.release_never_dispatched(c, r.entry_id, now=now)
    assert await one(engine, PROBE_COLUMNS) == ("validated", False, False, None)
    expected = "ambiguous" if dispatched else "not_billed"
    assert await one(engine, "SELECT state FROM quota_ledger WHERE id = :i", i=r.entry_id) == (expected,)


async def dispatched_entry(engine, owner, provider: str, n: int, now: datetime, **reserve: object) -> int:
    """A reserved and dispatched attempt of `provider` (key `n`) at `now`."""
    fr = await planned_request(engine, owner, provider=provider, key=key(n), attempt_seq=1)
    async with engine.begin() as c:
        r = reservation(
            await ledger.reserve(
                c,
                provider,  # type: ignore[arg-type]
                owner=owner,
                request_key=key(n),
                attempt_seq=1,
                request_class="retry",
                fetch_request_id=fr,
                settings=SETTINGS,
                interval_s=SIX_H,
                now=now,
                **reserve,  # type: ignore[arg-type]
            )
        )
        await ledger.mark_dispatched(c, r.entry_id, owner)
    return r.entry_id


@pytest.mark.parametrize("provider", ["serpapi", "adzuna"])
async def test_a_gateway_404_is_no_refund_evidence_outside_jsearch(engine, current_epoch, provider) -> None:
    """Ledger L105: only JSearch sits behind the RapidAPI gateway; for SerpApi or Adzuna the evidence is
    refused before anything is written, and the entry stays dispatched (counted)."""
    owner, now = await queued_run(engine), utcnow()
    extra = {"account": account(now, now + timedelta(days=20))} if provider == "serpapi" else {}
    entry = await dispatched_entry(engine, owner, provider, 1, now, **extra)
    async with engine.begin() as c:
        with pytest.raises(ValueError, match="rapidapi_gateway_404"):
            await settle_one(c, entry, state="not_billed", evidence="rapidapi_gateway_404", http_status=404)
    assert await one(engine, "SELECT state FROM quota_ledger WHERE id = :i", i=entry) == ("dispatched",)


async def test_a_jsearch_gateway_404_measures_the_window_without_its_own_entry(engine, current_epoch) -> None:
    """The observation of a `not_billed` settlement is measured against the window's usage without the entry
    (computed before its transition, which now comes after the window's changes): 2 billed, and a gateway
    404 reporting 3 used is 1 unaccounted unit."""
    owner, now = await queued_run(engine), utcnow()
    await insert_window(engine, jsearch_window(now, limit=200, reserve=20, tokens=5, interval_s=SIX_H))
    for n in (1, 2):
        async with engine.begin() as c:
            await settle_one(c, await dispatched_entry(engine, owner, "jsearch", n, now), now=now)
    gateway = await dispatched_entry(engine, owner, "jsearch", 3, now)
    async with engine.begin() as c:
        await settle_one(
            c,
            gateway,
            state="not_billed",
            evidence="rapidapi_gateway_404",
            http_status=404,
            observation=Observation(now, 404, limit=200, remaining=197),
            now=now,
        )
    assert await one(engine, "SELECT state, evidence FROM quota_ledger WHERE id = :i", i=gateway) == (
        "not_billed",
        "rapidapi_gateway_404",
    )
    assert (
        await scalar(engine, "SELECT COALESCE(SUM(units), 0) FROM quota_ledger WHERE kind = 'adjustment'")
        == 1
    )


async def test_an_out_of_searches_answer_settled_after_the_rollover_leaves_the_new_month_open(
    engine, current_epoch
) -> None:
    """Ledger L175: a SerpApi search reserved in October whose "out of searches" answer settles after the
    local rollover to November describes October; the new month is neither exhausted nor charged the rest of
    its limit."""
    await october_window(engine)
    owner = await queued_run(engine)
    before = RENEWAL - timedelta(seconds=30)
    entry = await dispatched_entry(
        engine, owner, "serpapi", 1, before, account=account(before, RENEWAL, left=1)
    )
    after = RENEWAL + timedelta(seconds=30)
    async with engine.begin() as c:
        await settle_one(
            c, entry, provider_remaining=0, observation=Observation(after, 200, remaining=0), now=after
        )
        current = await current_snapshot(c, "serpapi")
    assert current is not None and current.window_start == RENEWAL
    assert current.state == "extrapolated_unvalidated"  # not exhausted_until_reset
    november = "SELECT count(*) FROM quota_ledger WHERE kind = 'adjustment' AND window_start = :ws"
    assert await scalar(engine, november, ws=RENEWAL) == 0  # nor charged the rest of its limit
    assert await one(engine, "SELECT state, window_start FROM quota_ledger WHERE id = :i", i=entry) == (
        "billed",
        datetime(2026, 10, 8, tzinfo=UTC),
    )
    # the same answer within the month it describes exhausts that month (T12-a)
    second = await dispatched_entry(engine, owner, "serpapi", 2, after, account=account(after, NEXT, left=5))
    answered = after + timedelta(seconds=1)
    async with engine.begin() as c:
        await settle_one(
            c, second, provider_remaining=0, observation=Observation(answered, 200, remaining=0), now=answered
        )
        november_window = await current_snapshot(c, "serpapi")
    assert november_window is not None and november_window.state == "exhausted_until_reset"


ADZUNA_ATTEMPT = sa.text(
    "INSERT INTO quota_ledger (provider, window_start, kind, request_class, request_key, attempt_seq, units, "
    "state, per_run_cap, created_at, dispatched_at, settled_at) VALUES ('adzuna', :ws, 'request', 'normal', "
    ":key, 1, 1, 'billed', 10, :at, :at, :at)"
)


@pytest.mark.parametrize("span", ledger.ADZUNA_SPANS)
async def test_each_adzuna_rolling_window_ends_exactly_at_its_span(engine, current_epoch, span) -> None:
    """Ledger L105: each rolling count is `created_at > now - span`: an attempt exactly `span` old has left
    the window, one a microsecond younger is in it (with that window's cap 1, the others 100)."""
    now = utcnow()
    caps = {f"adzuna_cap_{name}": 100 for name in ("minute", "day", "week", "month")}
    caps[f"adzuna_cap_{('minute', 'day', 'week', 'month')[ledger.ADZUNA_SPANS.index(span)]}"] = 1
    tight = replace(SETTINGS, **caps)
    async with engine.begin() as c:
        w = await current_window_locked(c, "adzuna", now - timedelta(days=31), SETTINGS, interval_s=SIX_H)
        assert w is not None
        await c.execute(ADZUNA_ATTEMPT, {"ws": w.window_start, "key": key(1), "at": now - span})
    async with engine.connect() as c:
        assert await ledger.adzuna_windows_ok(c, now, 1, tight)  # exactly `span` old: out of the window
    async with engine.begin() as c:
        await c.execute(sa.text("UPDATE quota_ledger SET created_at = :at"), {"at": now - span + US})
    async with engine.connect() as c:
        assert not await ledger.adzuna_windows_ok(c, now, 1, tight)  # a microsecond younger: in it
