"""`GET /api/system/status`, the header strip's composite (spec §15.1; plan M4 Task 11, D4-D6, D11, D13-D16).

One read snapshot, no readiness probe and no table sizes: the last run with its partial causes, the active
run, the next three slots, the quota windows, the month's LLM spend and its pace, the open coverage gaps, the
pending targets, the unacknowledged alerts, the re-score's progress, the paid-search pause, the run-now units,
the scope's apply and every banner, computed on the server.

The app here runs in fake LLM mode (the dev and E2E stacks' mode), so an unverified schema pauses nothing
unless a test switches the mode off. Every datum is invented.
"""

import dataclasses
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa
from jobhunter.api.app import JobHunterApp, create_app
from jobhunter.api.deps import get_embedder
from jobhunter.clock import utcnow
from jobhunter.llm.budget import month_of
from jobhunter.llm.spend_ledger import current_params, ensure_month
from jobhunter.observability.alerts import raise_alert
from jobhunter.quota.windows import bootstrap_window
from jobhunter.settings.derive import active_profile, derived, ensure_derived
from jobhunter.settings.model import Settings
from jobhunter.types import JSONValue
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.support import m3
from tests.support.collect import free_cell, instance_id
from tests.support.embedder import FakeEmbedder
from tests.support.fixtures import ConfigFactory
from tests.support.pg import FAKE_LLM_DIR
from tests.support.quota import SETTINGS, SIX_H, insert_window
from tests.support.scoring import activate_score_tuple

pytestmark = pytest.mark.integration

URL: Final = "/api/system/status"
SYSTEM_UTCNOW: Final = "jobhunter.api.routes.system.utcnow"
NO_ALERTS: Final = {"critical_unacked": 0, "total_unacked": 0}
ACTIVE_DOCUMENT: Final = sa.text(
    "SELECT v.payload, a.profile_version_id FROM active_config a "
    "JOIN settings_versions v ON v.id = a.settings_version_id"
)
INTERVAL: Final = sa.text(
    "INSERT INTO coverage_intervals (cell_id, kind, span, gap_status, reason) VALUES (:c, :kind, "
    "tstzrange(:lower, :upper, '[)'), :status, CASE WHEN :kind = 'gap' THEN 'page_cap' END)"
)


@pytest.fixture
def api_app(engine: AsyncEngine, make_config: ConfigFactory, cv_store_dir: Path) -> JobHunterApp:
    """The api in fake LLM mode; `logged_in_client` uses it."""
    cfg = make_config(
        "api",
        JOBHUNTER_CV_STORE_DIR=str(cv_store_dir),
        JOBHUNTER_FAKE_LLM="1",
        JOBHUNTER_FAKE_LLM_DIR=str(FAKE_LLM_DIR),
    )
    return create_app(cfg=cfg, engine=engine)


@pytest.fixture
def now(monkeypatch: pytest.MonkeyPatch) -> datetime:
    """The route's clock, fixed for the test."""
    moment = utcnow()
    monkeypatch.setattr(SYSTEM_UTCNOW, lambda: moment)
    return moment


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


async def status(client: httpx.AsyncClient) -> dict[str, Any]:
    r = await client.get(URL)
    assert r.status_code == 200, (r.status_code, r.text)
    body: dict[str, Any] = r.json()
    return body


def banners(body: Mapping[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    return [(b["code"], b["severity"], b["params"]) for b in body["banners"]]


async def insert_run(
    c: AsyncConnection,
    *,
    status: str,
    queued_at: datetime,
    status_reason: str | None = None,
    report: Mapping[str, JSONValue] | None = None,
) -> int:
    """A manual run of the active configuration in `status`, queued at `queued_at` (an ended one finished 5
    minutes later)."""
    active = (
        await c.execute(sa.text("SELECT settings_version_id, scope_version_id FROM active_config"))
    ).one()
    values: dict[str, object] = {
        "trigger": "manual",
        "status": status,
        "include_paid": False,
        "settings_version_id": active.settings_version_id,
        "scope_version_id": active.scope_version_id,
        "queued_at": queued_at,
        "status_reason": status_reason,
    }
    if status not in ("queued", "running"):
        values |= {"attempt_no": 1, "started_at": queued_at, "finished_at": queued_at + timedelta(minutes=5)}
    if report is not None:
        values["report"] = dict(report)
    return await m3.insert_id(c, "runs", values)


async def activate_tuple_and_its_scope(c: AsyncConnection) -> int:
    """The score tuple made active with its profile (`activate_score_tuple`), and the scope the active
    settings derive with that profile made the active one, as a confirm's applied scope is; the tuple's id."""
    tuple_id = await activate_score_tuple(c)
    active = (await c.execute(ACTIVE_DOCUMENT)).one()
    profile = await active_profile(c, active.profile_version_id)
    ids = await ensure_derived(c, derived(Settings.model_validate(active.payload), profile=profile))
    await c.execute(sa.text("UPDATE active_config SET scope_version_id = :s"), {"s": ids.scope_version_id})
    return tuple_id


async def execute(engine: AsyncEngine, statement: str, **params: object) -> None:
    async with engine.begin() as c:
        await c.execute(sa.text(statement), params)


async def test_status_without_a_profile_or_runs(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, api_app: JobHunterApp, now: datetime
) -> None:
    body = await status(logged_in_client)
    slots = [datetime.fromisoformat(slot) for slot in body.pop("next_slots")]
    assert body == {
        "last_run": None,
        "active_run_id": None,
        "quota": {"jsearch": None, "serpapi": None, "adzuna": None},
        "llm": {"counted": "0.000000", "cap": "100.00", "pace": "ok"},  # the month's row is not created yet
        "coverage_gaps_open": 0,
        "pending_targets": 0,
        "alerts": NO_ALERTS,
        "rescore": None,
        "paid_search": {"paused": False, "until": None, "reason": None},
        # the seeded 200 JSearch units less the 10 % reserve over the month, at 6 h: under 2 units a run, up;
        # SerpApi has no window before its first account read
        "run_now": {"jsearch_units": 2, "serpapi_fallback_units": 0},
        "scope": {"pending_version_id": None, "progress": None, "applied": True},
        "banners": [],
    }
    # the schedule's next three slots: after `now`, the first within one interval, on the anchor's grid
    async with engine.connect() as c:
        schedule = (await c.execute(sa.text("SELECT anchor_utc, interval_s FROM schedule_state"))).one()
    step = timedelta(seconds=schedule.interval_s)
    assert len(slots) == 3 and now < slots[0] <= now + step
    assert [b - a for a, b in pairwise(slots)] == [step, step]
    assert (slots[0] - schedule.anchor_utc) % step == timedelta(0)

    # the same database, read by a process with the live LLM: no live schema dry-run, so scoring is paused
    api_app.api.state.cfg = dataclasses.replace(api_app.api.state.cfg, fake_llm=False)
    assert banners(await status(logged_in_client)) == [
        ("scoring_paused", "critical", {"reason": "schema_unverified"})
    ]
    # a disabled schedule has no next slot
    await execute(engine, "UPDATE schedule_state SET enabled = false")
    assert (await status(logged_in_client))["next_slots"] == []


async def test_status_reports_partial_causes_gaps_pending_and_unacked_alerts(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, now: datetime
) -> None:
    partial_report: dict[str, JSONValue] = {
        "execution": {"partial": ["fetch_failed", "source_breaker_open"], "warnings": []}
    }
    async with engine.begin() as c:
        await insert_run(c, status="succeeded", queued_at=now - timedelta(hours=8))
        run = await insert_run(
            c,
            status="partial",
            queued_at=now - timedelta(hours=2),
            status_reason="fetch_failed",
            report=partial_report,
        )
        cell = await free_cell(c, "arbeitnow")
        intervals = ((10, "gap", "open"), (8, "gap", "open"), (6, "gap", "closed"), (4, "covered", None))
        for days, kind, gap_status in intervals:
            span = {"lower": now - timedelta(days=days), "upper": now - timedelta(days=days - 1)}
            await c.execute(INTERVAL, {"c": cell, "kind": kind, "status": gap_status, **span})
        tuple_id = await activate_tuple_and_its_scope(c)
        for columns in (
            {},
            {},
            {"state": "budget_deferred", "state_reason": "budget_exhausted"},
            {"state": "refused", "refusal_category": "other"},
            {"state": "cancelled", "state_reason": "tuple_superseded"},
        ):
            await m3.insert_target(c, await m3.insert_job(c), tuple_id, **columns)
        unacked = await raise_alert(
            c, code="quota.limit_breach", severity="critical", subject="provider:jsearch", message="m"
        )
        acked = await raise_alert(
            c, code="run.failed_invariant", severity="critical", subject="run:1", message="m"
        )
        await raise_alert(c, code="run.partial", severity="warn", subject="collect:fetch_failed", message="m")
        resolved = await raise_alert(
            c, code="source.contract_broken", severity="critical", subject="instance:1", message="m"
        )
        await c.execute(sa.text("UPDATE alerts SET acknowledged_at = now() WHERE id = :a"), {"a": acked})
        await c.execute(sa.text("UPDATE alerts SET resolved_at = now() WHERE id = :a"), {"a": resolved})
    body = await status(logged_in_client)
    last = body["last_run"]
    assert last["run"]["id"] == run and last["run"]["status"] == "partial"
    assert last["run"]["status_reason"] == "fetch_failed"
    assert last["age_s"] == 2 * 3600.0  # from its `queued_at`, as the system health measures it
    assert last["partial_causes"] == ["fetch_failed", "source_breaker_open"]
    assert body["active_run_id"] is None
    assert body["coverage_gaps_open"] == 2  # the closed gap and the covered interval are not open gaps
    assert body["pending_targets"] == 3  # `pending` and `budget_deferred`
    assert body["alerts"] == {
        "critical_unacked": 1,
        "total_unacked": 2,
    }  # the acknowledged one is not counted
    assert banners(body) == [("critical_alerts", "critical", {"count": 1})]

    # an active run is the newest one; acknowledging the last critical alert ends its banner
    async with engine.begin() as c:
        queued = await insert_run(c, status="queued", queued_at=now - timedelta(minutes=1))
        await c.execute(sa.text("UPDATE alerts SET acknowledged_at = now() WHERE id = :a"), {"a": unacked})
    body = await status(logged_in_client)
    assert body["active_run_id"] == queued
    assert body["last_run"]["run"]["id"] == queued and body["last_run"]["partial_causes"] == []
    assert body["alerts"] == {"critical_unacked": 0, "total_unacked": 1}
    assert body["banners"] == []


async def test_a_quarantined_cap_pause_gives_the_cap_banner(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, now: datetime
) -> None:
    """A batch submission whose submit was ambiguous holds its reservation, counted against the cap; with the
    month's scoring share counted, the score lane is paused `cap_reached`: one banner, whatever the alert."""
    month = month_of(now)
    async with engine.begin() as c:
        tuple_id = await activate_tuple_and_its_scope(c)
        operation = await m3.insert_bulk_operation(c, tuple_id)
        await m3.insert_submission(
            c,
            tuple_id,
            operation,
            state="submit_ambiguous",
            submit_started_at=m3.NOW,
            reserved_usd=Decimal("1.250000"),
        )
        await ensure_month(c, month, await current_params(c), now)
        await c.execute(
            sa.text(
                "UPDATE llm_budget SET counted_usd = 93.10, scoring_counted_usd = 93.10 WHERE month = :m"
            ),
            {"m": month},
        )
        alert = await raise_alert(
            c,
            code="llm.cap_reached",
            severity="critical",
            subject="llm",
            message="m",
            context={"month": month.isoformat(), "quarantined_usd": "1.250000"},
        )
    body = await status(logged_in_client)
    assert body["llm"] == {"counted": "93.100000", "cap": "100.00", "pace": "ahead"}
    assert banners(body) == [("cap_reached", "critical", {}), ("critical_alerts", "critical", {"count": 1})]
    # the pause is the ledger's: acknowledging the alert ends only the alert's banner
    await execute(engine, "UPDATE alerts SET acknowledged_at = now() WHERE id = :a", a=alert)
    assert banners(await status(logged_in_client)) == [("cap_reached", "critical", {})]
    # headroom back: no pause
    await execute(engine, "UPDATE llm_budget SET counted_usd = 1.0, scoring_counted_usd = 1.0")
    body = await status(logged_in_client)
    assert body["banners"] == [] and body["llm"]["counted"] == "1.000000"


async def test_rescore_counts_the_bulk_operation_targets(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, now: datetime
) -> None:
    async with engine.begin() as c:
        tuple_id = await activate_tuple_and_its_scope(c)
        old = await m3.insert_bulk_operation(c, tuple_id, state="cancelled")
        await m3.insert_target(
            c,
            await m3.insert_job(c),
            tuple_id,
            state="refused",
            refusal_category="other",
            bulk_operation_id=old,
        )
        bulk = await m3.insert_bulk_operation(c, tuple_id)
        submission = await m3.insert_submission(
            c, tuple_id, bulk, state="submit_ambiguous", submit_started_at=m3.NOW
        )
        unscored: list[dict[str, object]] = [
            {},
            {},
            {"state": "budget_deferred", "state_reason": "budget_exhausted"},
            {
                "state": "in_progress",
                "claim_id": uuid4(),
                "claim_epoch": 1,
                "claimed_at": now,
                "claim_heartbeat_at": now,
            },
            {"state": "in_batch", "batch_submission_id": submission},
        ]
        scored: list[dict[str, object]] = [
            {"state": "refused", "refusal_category": "other"},
            {"state": "cancelled", "state_reason": "tuple_superseded"},
        ]
        for columns in (*unscored, *scored):
            await m3.insert_target(c, await m3.insert_job(c), tuple_id, bulk_operation_id=bulk, **columns)
        await m3.insert_target(c, await m3.insert_job(c), tuple_id)  # pending, of no bulk operation
        creation = await m3.insert_id(
            c,
            "operations",
            {
                "kind": "create_targets_for_tuple",
                "payload": {"llm_tuple_id": tuple_id, "bulk_operation_id": bulk},
                "dedup_key": f"create_targets_for_tuple:{tuple_id}",
                "status": "pending",
            },
        )
    body = await status(logged_in_client)
    assert body["rescore"] == {"bulk_operation_id": bulk, "done": 2, "total": 7, "creating": True}
    assert body["pending_targets"] == 4  # the bulk operation's three and the other one
    assert banners(body) == [("rescoring", "info", {"done": 2, "total": 7})]
    await execute(
        engine, "UPDATE operations SET status = 'cancelled', finished_at = now() WHERE id = :o", o=creation
    )
    body = await status(logged_in_client)
    assert body["rescore"] == {"bulk_operation_id": bulk, "done": 2, "total": 7, "creating": False}
    assert banners(body) == [("rescoring", "info", {"done": 2, "total": 7})]
    # an ended bulk operation is no re-score
    await execute(
        engine, "UPDATE bulk_operations SET state = 'completed', finished_at = now() WHERE id = :b", b=bulk
    )
    body = await status(logged_in_client)
    assert body["rescore"] is None and body["banners"] == []


async def test_status_does_not_probe_the_embedder(
    logged_in_client: httpx.AsyncClient, api_app: JobHunterApp, fake_embedder: FakeEmbedder, now: datetime
) -> None:
    fake_embedder.down = True
    health = (await logged_in_client.get("/api/system/health")).json()
    assert "embedder" in health["ready"]["failing"]  # the embedder is down indeed

    def no_embedder() -> None:
        raise AssertionError("the header status asked for the embedder")

    api_app.api.dependency_overrides[get_embedder] = no_embedder
    try:
        body = await status(logged_in_client)
    finally:
        api_app.api.dependency_overrides.pop(get_embedder)
    assert body["banners"] == [] and "embedder" not in body and "table_bytes" not in body


async def test_scope_reports_the_pending_apply_and_whether_the_active_scope_is_applied(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, now: datetime
) -> None:
    async with engine.begin() as c:
        active = (
            await c.execute(
                sa.text(
                    "SELECT a.scope_version_id, s.payload FROM active_config a "
                    "JOIN scope_versions s ON s.id = a.scope_version_id"
                )
            )
        ).one()
        other = await m3.insert_id(
            c,
            "scope_versions",
            {"scope_hash": bytes(32), "payload": {**active.payload, "variant": "test"}},
        )
        await c.execute(sa.text("UPDATE active_config SET pending_scope_version_id = :p"), {"p": other})
        apply = await m3.insert_id(
            c,
            "operations",
            {
                "kind": "apply_scope",
                "payload": {"scope_version_id": other},
                "dedup_key": f"apply_scope:{other}",
                "status": "pending",
                "progress": {"done": 3, "total": 10, "det_done": 1, "det_total": 4},
            },
        )
    body = await status(logged_in_client)
    assert body["scope"] == {
        "pending_version_id": other,
        "progress": {"done": 3, "total": 10, "det_done": 1, "det_total": 4},
        "applied": False,
    }
    assert banners(body) == [("applying_settings", "info", {"done": 4, "total": 14})]

    # its apply ended without flipping: no live apply, until the pointer is cleared
    await execute(
        engine, "UPDATE operations SET status = 'cancelled', finished_at = now() WHERE id = :o", o=apply
    )
    body = await status(logged_in_client)
    assert body["scope"] == {"pending_version_id": other, "progress": None, "applied": False}
    assert banners(body) == [("applying_settings", "info", {"failed": True})]

    # nothing pending, and the active scope is not the active settings version's: not applied
    await execute(
        engine, "UPDATE active_config SET pending_scope_version_id = NULL, scope_version_id = :s", s=other
    )
    body = await status(logged_in_client)
    assert body["scope"] == {"pending_version_id": None, "progress": None, "applied": False}
    assert banners(body) == [("scope_not_applied", "warn", {})]

    # the settings version's scope active again, but its newest ended apply failed: not applied, until a later
    # apply of it succeeds
    await execute(engine, "UPDATE active_config SET scope_version_id = :s", s=active.scope_version_id)
    assert (await status(logged_in_client))["scope"]["applied"] is True
    for state in ("failed", "succeeded"):
        await execute(
            engine,
            "INSERT INTO operations (kind, payload, dedup_key, status, last_error_code, finished_at) "
            "VALUES ('apply_scope', '{}', :k, :s, CASE WHEN :s = 'failed' THEN 'bug' END, now())",
            k=f"apply_scope:{active.scope_version_id}",
            s=state,
        )
        body = await status(logged_in_client)
        assert body["scope"]["applied"] is (state == "succeeded")
        assert body["banners"] == (
            [] if state == "succeeded" else [{"code": "scope_not_applied", "severity": "warn", "params": {}}]
        )

    # the scope is derived with the active profile: a profile whose seniority resolves the settings'
    # profile-relative range to another scope leaves the active one unapplied until that scope is applied
    async with engine.begin() as c:
        await activate_score_tuple(c)
    assert (await status(logged_in_client))["scope"]["applied"] is False
    async with engine.begin() as c:
        await activate_tuple_and_its_scope(c)
    assert (await status(logged_in_client))["scope"]["applied"] is True


async def test_paid_search_reads_the_windows_and_the_serpapi_breaker(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, now: datetime
) -> None:
    start = now - timedelta(days=10)
    jsearch = bootstrap_window("jsearch", start, SETTINGS, interval_s=SIX_H, account=None)
    serpapi = bootstrap_window("serpapi", start, SETTINGS, interval_s=SIX_H, account=None)
    assert jsearch is not None and serpapi is None  # SerpApi's first window needs an account read
    month = timedelta(days=30)
    await insert_window(
        engine,
        dataclasses.replace(
            jsearch,
            window_end=start + month,
            state="exhausted_until_reset",
            limit_units=200,
            reserve_units=20,
        ),
    )
    await insert_window(
        engine,
        dataclasses.replace(
            jsearch,
            provider="serpapi",
            window_start=start,
            window_end=start + timedelta(days=12),
            state="validated",
            limit_units=300,
            reserve_units=30,
        ),
    )
    body = await status(logged_in_client)
    assert body["paid_search"] == {"paused": False, "until": None, "reason": None}  # SerpApi can fall back
    # min(per-run cap, fair share) on each live window, rounded up: 180 x 6 h / 30 d = 1.5 units (the cap is
    # 10) and 270 x 6 h / 12 d = 5.625 units (the cap is 12)
    assert body["run_now"] == {"jsearch_units": 2, "serpapi_fallback_units": 6}
    assert body["quota"]["jsearch"] == {
        "used": 0,
        "limit": 200,
        "reset_at": iso(start + month),
        "state": "exhausted_until_reset",
    }
    async with engine.begin() as c:
        serpapi_instance = await instance_id(c, "serpapi")
        await c.execute(
            sa.text(
                "UPDATE source_instances SET health = 'breaker_open', breaker_opened_at = :at, "
                "half_open_after = :half WHERE id = :i"
            ),
            {"at": now, "half": now + timedelta(hours=6), "i": serpapi_instance},
        )
    body = await status(logged_in_client)
    until = iso(start + month)
    assert body["paid_search"] == {"paused": True, "until": until, "reason": "serpapi_unavailable"}
    assert banners(body) == [
        ("paid_search_paused", "warn", {"until": until, "reason": "serpapi_unavailable"})
    ]
    # from its half-open time the breaker admits a probe: SerpApi can serve again
    await execute(
        engine,
        "UPDATE source_instances SET half_open_after = :half WHERE id = :i",
        half=now,
        i=serpapi_instance,
    )
    assert (await status(logged_in_client))["paid_search"]["paused"] is False
