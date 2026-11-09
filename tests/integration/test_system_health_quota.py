import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.observability.alerts import raise_alert
from jobhunter.quota import ledger
from jobhunter.quota.windows import NOMINAL_PERIOD, bootstrap_window

from tests.support import m3
from tests.support.collect import planned_request, queued_run
from tests.support.quota import SETTINGS, SIX_H, insert_ledger_entry, insert_window
from tests.support.scoring import activate_score_tuple

pytestmark = pytest.mark.integration
SYSTEM_UTCNOW = "jobhunter.api.routes.system.utcnow"


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


async def test_health_reports_current_windows(logged_in_client, engine, current_epoch, monkeypatch) -> None:
    owner, now = await queued_run(engine), utcnow()
    monkeypatch.setattr(SYSTEM_UTCNOW, lambda: now)
    fr = await planned_request(engine, owner, provider="adzuna", key=bytes(32), attempt_seq=1)
    async with engine.begin() as c:
        r = await ledger.reserve(
            c,
            "adzuna",
            owner=owner,
            request_key=bytes(32),
            attempt_seq=1,
            request_class="normal",
            fetch_request_id=fr,
            settings=SETTINGS,
            interval_s=21600,
            now=now,
        )
    assert isinstance(r, ledger.Reservation)
    body = (await logged_in_client.get("/api/system/health")).json()
    start = now.astimezone(UTC)
    assert body["quota"]["adzuna"] == {
        "window_start": start.isoformat().replace("+00:00", "Z"),
        "window_end": (start + timedelta(days=30)).isoformat().replace("+00:00", "Z"),
        "state": "validated",
        "limit_units": 2250,
        "used_units": 1,
        "remaining_units": 2249,
        "tokens": 32.75,  # the full bucket of a 6 h schedule (33.75) less the one normal request
    }
    # no JSearch window is stored: the one its first request would open now, unspent; SerpApi has none
    # before its first account read
    jsearch = body["quota"]["jsearch"]
    assert body["quota"]["serpapi"] is None
    assert (jsearch["window_start"], jsearch["window_end"]) == (
        iso(now),
        iso(now + NOMINAL_PERIOD["jsearch"]),
    )
    assert (jsearch["state"], jsearch["limit_units"], jsearch["used_units"], jsearch["remaining_units"]) == (
        "extrapolated_unvalidated",
        200,
        0,
        200,
    )


async def test_health_and_status_show_the_same_window_after_a_reset(
    logged_in_client, engine, current_epoch, monkeypatch
) -> None:
    """A stored JSearch window that ran out and whose reset has passed, before any ledger write rolled it: the
    detailed health and the header status both show its successor, unspent, with the same reset."""
    now = utcnow()
    monkeypatch.setattr(SYSTEM_UTCNOW, lambda: now)
    start = now - timedelta(days=40)
    boot = bootstrap_window("jsearch", start, SETTINGS, interval_s=SIX_H, account=None)
    assert boot is not None
    ended = start + timedelta(days=31)
    await insert_window(engine, replace(boot, window_end=ended, state="exhausted_until_reset"))
    await insert_ledger_entry(engine, "jsearch", start, units=200, state="billed")
    health = (await logged_in_client.get("/api/system/health")).json()["quota"]["jsearch"]
    status = (await logged_in_client.get("/api/system/status")).json()["quota"]["jsearch"]
    successor_end = iso(ended + NOMINAL_PERIOD["jsearch"])
    assert health["window_start"] == iso(ended) and health["window_end"] == successor_end
    assert (health["state"], health["used_units"], health["remaining_units"]) == (
        "extrapolated_unvalidated",
        0,
        200,
    )
    assert status == {
        "used": health["used_units"],
        "limit": health["limit_units"],
        "reset_at": health["window_end"],
        "state": health["state"],
    }


async def test_health_reports_the_score_lane_read_without_locks(
    logged_in_client, engine, current_epoch, monkeypatch
) -> None:
    """M3 Task 18 (§17.5 L5552): the `scoring` section of the active tuple: the running cycle, the pause
    guard's reason, the oldest pending target's age (by `created_at`), the pending and batched targets and
    the quarantined batch spend; read while another transaction holds `active_config FOR UPDATE`."""
    now = utcnow()
    monkeypatch.setattr("jobhunter.api.routes.system.utcnow", lambda: now)
    body = (await logged_in_client.get("/api/system/health")).json()
    # no tuple yet: nothing pending, and a real-mode lane without a verified schema is paused for it
    assert body["scoring"] == {
        "running_cycle_id": None,
        "pause_reason": "schema_unverified",
        "oldest_pending_target_age_s": None,
        "pending_targets": 0,
        "in_batch_targets": 0,
        "quarantined_batch_usd": "0.000000",
    }
    async with engine.begin() as c:
        tuple_id = await activate_score_tuple(c)
        for age in (timedelta(hours=5), timedelta(hours=1)):
            await m3.insert_target(c, await m3.insert_job(c), tuple_id, created_at=now - age)
        operation = await m3.insert_bulk_operation(c, tuple_id)
        submission = await m3.insert_submission(
            c,
            tuple_id,
            operation,
            state="submit_ambiguous",
            submit_started_at=m3.NOW,
            reserved_usd=Decimal("1.250000"),
        )
        await m3.insert_target(
            c, await m3.insert_job(c), tuple_id, state="in_batch", batch_submission_id=submission
        )
        await m3.insert_target(c, await m3.insert_job(c), tuple_id, state="refused", refusal_category="other")
        cycle = await m3.insert_cycle(c, tuple_id)
        await raise_alert(
            c, code="llm.auth_failed", severity="critical", subject="anthropic", message="m", context={}
        )
    async with engine.connect() as holder:  # a save's lock: the health page reads past it
        await holder.execute(sa.text("SELECT 1 FROM active_config WHERE id = 1 FOR UPDATE"))
        body = (await asyncio.wait_for(logged_in_client.get("/api/system/health"), 10)).json()
        await holder.rollback()
    assert body["scoring"] == {
        "running_cycle_id": cycle,
        "pause_reason": "auth_failed",
        "oldest_pending_target_age_s": 5 * 3600.0,
        "pending_targets": 2,
        "in_batch_targets": 1,
        "quarantined_batch_usd": "1.250000",
    }
