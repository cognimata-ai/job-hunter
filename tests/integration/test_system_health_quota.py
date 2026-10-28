import asyncio
from datetime import UTC, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.observability.alerts import raise_alert
from jobhunter.quota import ledger

from tests.support import m3
from tests.support.collect import planned_request, queued_run
from tests.support.quota import SETTINGS
from tests.support.scoring import activate_score_tuple

pytestmark = pytest.mark.integration


async def test_health_reports_current_windows(logged_in_client, engine, current_epoch) -> None:
    owner, now = await queued_run(engine), utcnow()
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
    assert body["quota"]["serpapi"] is None and body["quota"]["jsearch"] is None


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
