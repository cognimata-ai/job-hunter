from datetime import UTC, timedelta

import pytest
from jobhunter.clock import utcnow
from jobhunter.quota import ledger

from tests.support.collect import planned_request, queued_run
from tests.support.quota import SETTINGS

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
