import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import sqlalchemy as sa
from jobhunter.api_idempotency import StoredResponse, run_idempotent
from jobhunter.clock import utcnow
from jobhunter.observability.health import RunAgeState, run_age_state
from jobhunter.worker import runs
from jobhunter.worker.tasks import defer_in_tx
from jobhunter.worker.tick import TickReport, run_tick
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.support.procs import await_until

pytestmark = pytest.mark.integration
KEY = "run-now-key-000001"
# What another request's claim of a key inserts (uncommitted while that request runs).
CLAIM_KEY = sa.text(
    "INSERT INTO api_idempotency (route, idem_key, request_sha256, status, expires_at) VALUES "
    "('POST /api/runs', :k, decode(repeat('00', 32), 'hex'), 'in_progress', now() + interval '7 days')"
)


async def post(c: httpx.AsyncClient, key: str | None = KEY, include_paid: bool = False) -> httpx.Response:
    return await c.post(
        "/api/runs", json={"include_paid": include_paid}, headers={"Idempotency-Key": key} if key else {}
    )


async def waiting_on_lock(engine: AsyncEngine) -> bool:
    async with engine.connect() as c:
        waiting = await c.execute(
            sa.text("SELECT count(*) FROM pg_locks WHERE NOT granted AND locktype = 'transactionid'")
        )
        return bool(waiting.scalar_one() >= 1)


async def lock_waited_longer_than(engine: AsyncEngine, seconds: float) -> bool:
    """Whether a statement has been waiting for a lock for more than `seconds`, on the database clock."""
    async with engine.connect() as c:
        waiting = await c.execute(
            sa.text(
                "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                "AND clock_timestamp() - query_start > make_interval(secs => :s)"
            ),
            {"s": seconds},
        )
        return bool(waiting.scalar_one() >= 1)


async def due_slot(engine: AsyncEngine) -> datetime:
    now = utcnow()
    async with engine.begin() as c:
        await c.execute(
            sa.text("UPDATE schedule_state SET anchor_utc=:a, interval_s=3600, last_evaluated_slot=:l"),
            {"a": now - timedelta(minutes=1), "l": now - timedelta(hours=1, minutes=1)},
        )
    return now


async def test_key_required_and_format(logged_in_client) -> None:
    for key in (None, "short", "has space in it!!!"):
        r = await post(logged_in_client, key)
        assert r.status_code == 400 and r.json()["code"] == "idempotency_key_required"


async def test_same_key_returns_stored_response_and_one_run(logged_in_client, engine, queue_app) -> None:
    a = await post(logged_in_client)
    b = await post(logged_in_client)
    assert a.status_code == b.status_code == 202 and a.json() == b.json() and a.json()["created"] is True
    assert a.json()["run"]["trigger"] == "manual" and a.json()["run"]["scheduled_slot"] is None
    async with engine.begin() as c:
        await c.execute(sa.text("UPDATE runs SET status='cancelled', finished_at=now()"))
    assert (await post(logged_in_client)).json() == a.json()
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT count(*) FROM runs"))).scalar_one() == 1


async def test_reused_key_with_other_body_is_422(logged_in_client, queue_app) -> None:
    await post(logged_in_client)
    r = await post(logged_in_client, include_paid=True)
    assert r.status_code == 422 and r.json()["code"] == "idempotency_key_reused"


async def test_new_key_returns_active_run_and_expired_key_runs_again(
    logged_in_client, engine, queue_app
) -> None:
    a = await post(logged_in_client)
    b = await post(logged_in_client, key="run-now-key-000002")
    assert b.json()["created"] is False and b.json()["run"]["id"] == a.json()["run"]["id"]
    async with engine.begin() as c:
        await c.execute(sa.text("UPDATE api_idempotency SET expires_at = now() - interval '1 second'"))
        await c.execute(sa.text("UPDATE runs SET status='cancelled', finished_at=now()"))
    assert (await post(logged_in_client)).json()["created"] is True


async def test_run_now_blocked_behind_tick_returns_tick_run(
    logged_in_client, engine, queue_app, log_events
) -> None:  # Review Focus 4
    now = await due_slot(engine)
    async with engine.connect() as conn:
        tx = await conn.begin()
        rep = await run_tick(conn, now=now, defer=defer_in_tx)  # tick's run inserted, not committed
        req = asyncio.create_task(post(logged_in_client))
        try:
            await await_until(lambda: waiting_on_lock(engine), 5)  # Run now provably blocked on the index
        finally:  # also when the wait fails: the request must not wait for ever
            await tx.commit()
            await asyncio.wait([req], timeout=5)
    r = await asyncio.wait_for(req, 5)
    assert (
        r.status_code == 202
        and r.json() == {"run": r.json()["run"], "created": False}
        and r.json()["run"]["id"] == rep.queued_run_id
    )
    # the IntegrityError branch
    assert [e["path"] for e in log_events() if e["event"] == "active_collect_conflict"] == ["manual"]


async def test_tick_blocked_behind_run_now_records_overlap(
    engine, queue_app, log_events
) -> None:  # Review Focus 4
    now = await due_slot(engine)

    async def tick() -> TickReport:
        async with engine.begin() as c:
            return await run_tick(c, now=now, defer=defer_in_tx)

    async with engine.connect() as conn:
        tx = await conn.begin()
        manual, created = await runs.request_manual_run(conn, include_paid=False, defer=defer_in_tx)
        t = asyncio.create_task(tick())
        try:
            await await_until(lambda: waiting_on_lock(engine), 5)
        finally:  # also when the wait fails: the tick must not wait for ever
            await tx.commit()
            await asyncio.wait([t], timeout=5)
    rep = await asyncio.wait_for(t, 5)
    assert created and rep.skipped_overlap and rep.queued_run_id is None
    assert [e["path"] for e in log_events() if e["event"] == "active_collect_conflict"] == ["tick"]
    async with engine.connect() as c:
        assert [
            r[0] for r in await c.execute(sa.text("SELECT id FROM runs WHERE status IN ('queued','running')"))
        ] == [manual.id]


async def test_same_key_in_flight_elsewhere_is_409(logged_in_client, engine, queue_app, monkeypatch) -> None:
    import jobhunter.api_idempotency as idem

    monkeypatch.setattr(idem, "IN_PROGRESS_LOCK_TIMEOUT", "200ms")
    async with engine.connect() as conn:
        tx = await conn.begin()  # another request holds the key, uncommitted
        try:
            await conn.execute(CLAIM_KEY, {"k": KEY})
            r = await post(logged_in_client)  # waits on the key, then lock_timeout: SQLSTATE 55P03
            assert r.status_code == 409 and r.json()["code"] == "idempotency_in_progress"
        finally:
            await tx.rollback()
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT count(*) FROM runs"))).scalar_one() == 0


async def test_system_health_requires_session_and_reports(
    api_client, logged_in_client, engine, queue_app, monkeypatch
) -> None:
    assert (await api_client.get("/api/system/health")).status_code == 401
    await due_slot(engine)
    async with engine.begin() as c:
        await run_tick(c, now=utcnow() + timedelta(hours=2), defer=defer_in_tx)
        seeded = (await c.execute(sa.text("SELECT min(created_at) FROM settings_versions"))).scalar_one()
    # injected clock, inside the 2 h grace
    monkeypatch.setattr("jobhunter.api.routes.system.utcnow", lambda: seeded + timedelta(hours=1))
    body = (await logged_in_client.get("/api/system/health")).json()
    # anchor and anchor+1h are missed; anchor+2h is about 1 min old at this tick, within CATCH_UP_AFTER,
    # so 'schedule' (Task 11); the newest run by (queued_at, id)
    assert body["last_run"]["status"] == "queued" and body["last_run"]["trigger"] == "schedule"
    assert body["alerts_open"] == {"info": 0, "warn": 0, "critical": 0} and body["run_age_state"] in (
        "grace",
        "none",
    )
    assert body["queue_depths"]["collect"]["todo"] == 1 and body["versions"]["procrastinate"] == "3.10.0"
    assert "worker_heartbeat" in body["ready"]["failing"] and body["table_bytes"]["runs"] > 0
    assert body["disk"]["total_bytes"] > body["disk"]["free_bytes"] > 0 and body["database_bytes"] > 0


async def test_the_handler_waits_without_the_key_lock_timeout(
    logged_in_client, engine, queue_app, monkeypatch
) -> None:
    import jobhunter.api_idempotency as idem

    monkeypatch.setattr(idem, "IN_PROGRESS_LOCK_TIMEOUT", "50ms")
    now = await due_slot(engine)
    async with engine.connect() as conn:
        tx = await conn.begin()
        rep = await run_tick(conn, now=now, defer=defer_in_tx)  # tick's run inserted, not committed
        req = asyncio.create_task(post(logged_in_client))
        try:
            # the manual run's insert outwaits the key's lock timeout tenfold: that timeout is off again
            await await_until(lambda: lock_waited_longer_than(engine, 0.5), 5)
            assert not req.done()
        finally:
            await tx.commit()
            await asyncio.wait([req], timeout=5)
    r = await asyncio.wait_for(req, 5)
    assert r.status_code == 202 and r.json()["run"]["id"] == rep.queued_run_id


async def test_a_handler_error_stores_nothing_so_a_retry_runs_again(engine) -> None:  # ruling T17-e
    calls: list[str] = []

    async def failing(conn: AsyncConnection) -> StoredResponse:
        calls.append("failing")
        raise RuntimeError("the handler failed")

    async def succeeding(conn: AsyncConnection) -> StoredResponse:
        calls.append("succeeding")
        return StoredResponse(202, {"run": {"id": 1}, "created": True})

    async def call(handler: Callable[[AsyncConnection], Awaitable[StoredResponse]]) -> StoredResponse:
        async with engine.begin() as c:
            return await run_idempotent(
                c, route="POST /api/test", key=KEY, fingerprint=bytes(32), now=utcnow(), handler=handler
            )

    with pytest.raises(RuntimeError, match="the handler failed"):
        await call(failing)
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT count(*) FROM api_idempotency"))).scalar_one() == 0
    first = await call(succeeding)
    assert await call(succeeding) == first == StoredResponse(202, {"run": {"id": 1}, "created": True})
    assert calls == ["failing", "succeeding"]  # the replay did not run the handler


async def test_run_now_takes_active_config_before_it_claims_the_key(
    logged_in_client, engine, queue_app
) -> None:
    # ruling P18: while Run now waits for `active_config`, no uncommitted claim of it holds the key yet
    async with engine.connect() as holder:
        tx = await holder.begin()
        await holder.execute(sa.text("SELECT 1 FROM active_config WHERE id = 1 FOR UPDATE"))
        req = asyncio.create_task(post(logged_in_client))
        try:
            await await_until(lambda: waiting_on_lock(engine), 5)
            async with engine.connect() as probe:  # 55P03 if the waiting request had claimed the key
                probe_tx = await probe.begin()
                await probe.execute(sa.text("SELECT set_config('lock_timeout', '200ms', true)"))
                await probe.execute(CLAIM_KEY, {"k": KEY})
                await probe_tx.rollback()
        finally:
            await tx.commit()
            await asyncio.wait([req], timeout=5)
    r = await asyncio.wait_for(req, 5)
    assert r.status_code == 202 and r.json()["created"] is True


_FIRST = datetime(2026, 10, 1, tzinfo=UTC)
_HOUR = timedelta(hours=1)


@pytest.mark.parametrize(
    ("now_h", "success_h", "any_run", "state"),
    [
        (1.0, None, False, "none"),  # no run recorded yet
        (5.0, None, False, "none"),
        (1.0, None, True, "grace"),  # within 2 intervals of the first settings version
        (2.0, 1.0, True, "grace"),  # the grace includes its end
        (5.0, 4.0, True, "ok"),
        (5.0, 3.5, True, "ok"),  # exactly 1.5 intervals: not yet warn
        (5.0, 3.4, True, "warn"),
        (5.0, 3.0, True, "warn"),  # exactly 2 intervals: not yet critical
        (5.0, 2.9, True, "critical"),
        (5.0, None, True, "critical"),  # nothing succeeded: aged from the first settings version
    ],
)
def test_run_age_state(now_h: float, success_h: float | None, any_run: bool, state: RunAgeState) -> None:
    assert (
        run_age_state(
            _FIRST + now_h * _HOUR,
            any_run=any_run,
            last_success_at=None if success_h is None else _FIRST + success_h * _HOUR,
            first_settings_at=_FIRST,
            interval_s=3600,
        )
        == state
    )
