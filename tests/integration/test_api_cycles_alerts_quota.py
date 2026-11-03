"""The score cycles, the alert list and the quota summary over HTTP (spec §14.2 L5036, L5038, L5047, §17.6;
plan M4 Task 10, D27).

- `GET /api/score-cycles?cursor&limit` → `{items, next_cursor}`, by id descending, keyset-paged with a signed
  cursor that no other list's cursor passes for.
- `GET /api/alerts?status&cursor&limit` → `{items, next_cursor}`, by id descending, each alert with its status
  as SQL derives it: `resolved` once `resolved_at` is set (acknowledged or not), else `acknowledged` once
  `acknowledged_at` is, else `open`. The `status` filter is applied in SQL and hashed into the cursor.
- `GET /api/system/quota` → one entry per provider: its current window with the window's counted usage
  (`ledger.used_units`), its reserved and dispatched units, its ambiguous units and its adjustments, and its
  bucket; `null` for a provider without a current window. A closed window's entries count for nothing.

Every datum is invented.
"""

import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final, get_args

import httpx
import pytest
import sqlalchemy as sa
from jobhunter.api.app import JobHunterApp
from jobhunter.api.pagination import ALERTS_SORT, SCORE_CYCLES_SORT, cursor_key, encode_list_cursor
from jobhunter.clock import utcnow
from jobhunter.observability.alerts import acknowledge_alert, raise_alert, resolve_alert
from jobhunter.observability.lists import CycleStatus
from jobhunter.quota import ledger
from jobhunter.quota.windows import Close, apply_changes, bootstrap_window
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support import m3
from tests.support.quota import (
    SETTINGS,
    SIX_H,
    LedgerState,
    insert_ledger_entry,
    insert_window,
    jsearch_window,
)
from tests.support.scoring import activate_score_tuple

pytestmark = pytest.mark.integration

CYCLES_URL: Final = "/api/score-cycles"
ALERTS_URL: Final = "/api/alerts"
QUOTA_URL: Final = "/api/system/quota"
PROVIDERS: Final = ("jsearch", "serpapi", "adzuna")


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


async def page(client: httpx.AsyncClient, url: str, **params: Any) -> dict[str, Any]:
    r = await client.get(url, params=params)
    assert r.status_code == 200, (r.status_code, r.text)
    body: dict[str, Any] = r.json()
    return body


def ids(body: dict[str, Any]) -> list[int]:
    return [item["id"] for item in body["items"]]


async def refused(client: httpx.AsyncClient, url: str, **params: Any) -> tuple[int, str]:
    r = await client.get(url, params=params)
    return r.status_code, r.json()["code"]


# ---- score cycles ------------------------------------------------------------------------------------------


async def test_cycles_page_by_id_desc(
    logged_in_client: httpx.AsyncClient, api_app: JobHunterApp, engine: AsyncEngine
) -> None:
    async with engine.begin() as c:
        tuple_id = await activate_score_tuple(c)
        first = await m3.insert_cycle(c, tuple_id, status="completed")
        failed = await m3.insert_cycle(c, tuple_id, status="failed")
        reported = await m3.insert_cycle(c, tuple_id, status="completed")
        await c.execute(
            sa.text(
                "UPDATE score_cycles SET admission_size = 3, admitted = CAST(:admitted AS jsonb), "
                "spent_usd = :spent, report = CAST(:report AS jsonb) WHERE id = :id"
            ),
            {
                "admitted": '{"score_now": 1, "fresh": 2}',
                "spent": Decimal("0.123456"),
                "report": '{"outcomes": {"published": 3}}',
                "id": reported,
            },
        )
        running = await m3.insert_cycle(c, tuple_id)
        started = (
            await c.execute(sa.text("SELECT started_at FROM score_cycles WHERE id = :id"), {"id": reported})
        ).scalar_one()
    top = await page(logged_in_client, CYCLES_URL, limit=3)
    assert ids(top) == [running, reported, failed] and top["next_cursor"] is not None
    rest = await page(logged_in_client, CYCLES_URL, limit=3, cursor=top["next_cursor"])
    assert (ids(rest), rest["next_cursor"]) == ([first], None)
    assert top["items"][1] == {
        "id": reported,
        "trigger": "manual",
        "status": "completed",
        "status_reason": None,
        "llm_tuple_id": tuple_id,
        "admission_size": 3,
        "admitted": {"score_now": 1, "fresh": 2},
        "spent_usd": "0.123456",
        "report": {"outcomes": {"published": 3}},
        "started_at": iso(started),
        "finished_at": iso(m3.NOW),
    }
    by_id = {item["id"]: item for item in top["items"]}
    assert (by_id[failed]["status"], by_id[failed]["status_reason"], by_id[failed]["spent_usd"]) == (
        "failed",
        "bug",
        "0.000000",
    )
    assert (by_id[running]["finished_at"], by_id[running]["report"], by_id[running]["admitted"]) == (
        None,
        None,
        {},
    )

    key = cursor_key(api_app.api.state.cfg)
    # both lists sort by an id descending: only the namespaced sort tells their cursors apart (D35)
    alerts = encode_list_cursor(sort=ALERTS_SORT, dir="desc", last_key=[running], filters={}, key=key)
    assert await refused(logged_in_client, CYCLES_URL, cursor=alerts) == (400, "invalid_cursor")
    for last_key in ([0], [True], [str(running)], [running, 1]):
        forged = encode_list_cursor(
            sort=SCORE_CYCLES_SORT, dir="desc", last_key=last_key, filters={}, key=key
        )
        assert await refused(logged_in_client, CYCLES_URL, cursor=forged) == (400, "invalid_cursor"), last_key
    assert await refused(logged_in_client, CYCLES_URL, limit=101) == (422, "validation_failed")


async def test_a_cycle_s_status_is_one_of_the_check(engine: AsyncEngine) -> None:
    async with engine.connect() as c:
        definition = (
            await c.execute(
                sa.text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid = 'score_cycles'::regclass AND conname = 'score_cycles_status_check'"
                )
            )
        ).scalar_one()
    assert re.findall(r"'([a-z_]+)'::text", definition) == list(get_args(CycleStatus))


# ---- alerts ------------------------------------------------------------------------------------------------


async def test_alert_statuses_open_acknowledged_resolved(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    acked_at = utcnow() - timedelta(minutes=5)
    made: dict[str, int] = {}
    async with engine.begin() as c:
        for name in ("open", "acknowledged", "resolved", "acknowledged_then_resolved"):
            made[name] = await raise_alert(
                c,
                code="run.partial",
                severity="warn",
                subject=f"cause:{name}",
                message="a run was partial",
                context={"run_id": 7, "cause": "fetch_failed"},
            )
        await raise_alert(c, code="run.partial", severity="warn", subject="cause:open", message="again")
        for name in ("acknowledged", "acknowledged_then_resolved"):
            assert await acknowledge_alert(c, made[name], now=acked_at) is not None
        for name in ("resolved", "acknowledged_then_resolved"):
            assert await resolve_alert(c, code="run.partial", subject=f"cause:{name}")
        row = (
            await c.execute(
                sa.text("SELECT first_at, last_at FROM alerts WHERE id = :id"), {"id": made["open"]}
            )
        ).one()
    body = await page(logged_in_client, ALERTS_URL)
    newest_first = ["acknowledged_then_resolved", "resolved", "acknowledged", "open"]
    assert ids(body) == [made[name] for name in newest_first] and body["next_cursor"] is None
    assert [item["status"] for item in body["items"]] == ["resolved", "resolved", "acknowledged", "open"]
    assert body["items"][-1] == {
        "id": made["open"],
        "code": "run.partial",
        "severity": "warn",
        "message": "a run was partial",
        "context": {"run_id": 7, "cause": "fetch_failed"},
        "first_at": iso(row.first_at),
        "last_at": iso(row.last_at),
        "occurrences": 2,
        "acknowledged_at": None,
        "resolved_at": None,
        "status": "open",
    }
    assert body["items"][2]["acknowledged_at"] == iso(acked_at)
    assert all(item["resolved_at"] is not None for item in body["items"][:2])

    assert ids(await page(logged_in_client, ALERTS_URL, status="open")) == [made["open"]]
    # the oldest alert is the one open: a filter applied after the limit would find none on the first page
    only_open = await page(logged_in_client, ALERTS_URL, status="open", limit=1)
    assert (ids(only_open), only_open["next_cursor"]) == ([made["open"]], None)
    assert ids(await page(logged_in_client, ALERTS_URL, status="acknowledged")) == [made["acknowledged"]]
    first = await page(logged_in_client, ALERTS_URL, status="resolved", limit=1)
    assert ids(first) == [made["acknowledged_then_resolved"]] and first["next_cursor"] is not None
    rest = await page(logged_in_client, ALERTS_URL, status="resolved", limit=1, cursor=first["next_cursor"])
    assert (ids(rest), rest["next_cursor"]) == ([made["resolved"]], None)
    cursor = first["next_cursor"]
    assert await refused(logged_in_client, ALERTS_URL, status="open", cursor=cursor) == (
        400,
        "invalid_cursor",
    )
    assert await refused(logged_in_client, ALERTS_URL, cursor=cursor) == (400, "invalid_cursor")
    assert await refused(logged_in_client, ALERTS_URL, status="closed") == (422, "validation_failed")


# ---- quota -------------------------------------------------------------------------------------------------


async def test_quota_breaks_down_used_reserved_ambiguous_adjustments(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    now = utcnow()
    older = jsearch_window(now - timedelta(days=40), limit=200, reserve=20, tokens=5, interval_s=SIX_H)
    await insert_window(engine, older)
    for state in ("billed", None):  # the closed window's: counted there, not here
        await insert_ledger_entry(engine, "jsearch", older.window_start, units=64, state=state)
    async with engine.begin() as c:
        await apply_changes(c, [Close(older, "rolled_over")])
    current = jsearch_window(now, limit=200, reserve=20, tokens=7, interval_s=SIX_H)
    await insert_window(engine, current)
    # one power of two each, so that every sum names exactly the entries it counts
    entries: list[tuple[int, LedgerState | None]] = [
        (1, "reserved"),
        (2, "dispatched"),
        (4, "billed"),
        (8, "ambiguous"),
        (16, "not_billed"),
        (32, None),  # an adjustment
    ]
    for units, state in entries:
        await insert_ledger_entry(engine, "jsearch", current.window_start, units=units, state=state)
    adzuna = bootstrap_window("adzuna", now, SETTINGS, interval_s=SIX_H, account=None)
    assert adzuna is not None
    await insert_window(engine, replace(adzuna, state="validated"))
    await insert_ledger_entry(engine, "adzuna", adzuna.window_start, units=3, state="dispatched")

    body = await page(logged_in_client, QUOTA_URL)
    assert list(body) == list(PROVIDERS)
    assert body["jsearch"] == {
        "window_start": iso(current.window_start),
        "window_end": iso(current.window_end),
        "state": "validated",
        "limit_units": 200,
        "used": 1 + 2 + 4 + 8 + 32,  # the counted states and the adjustment; never `not_billed`
        "reserved": 1 + 2,
        "ambiguous": 8,
        "adjustments": 32,
        "tokens": 7.0,
    }
    assert body["adzuna"] == {
        "window_start": iso(adzuna.window_start),
        "window_end": iso(adzuna.window_end),
        "state": "validated",
        "limit_units": adzuna.limit_units,
        "used": 3,
        "reserved": 3,
        "ambiguous": 0,
        "adjustments": 0,
        "tokens": float(adzuna.tokens),
    }
    assert body["serpapi"] is None
    async with engine.connect() as c:  # `used` is the ledger's own counted usage
        assert body["jsearch"]["used"] == await ledger.used_units(c, "jsearch", current.window_start)
        assert body["adzuna"]["used"] == await ledger.used_units(c, "adzuna", adzuna.window_start)


async def test_a_provider_without_a_window_is_null(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    assert await page(logged_in_client, QUOTA_URL) == dict.fromkeys(PROVIDERS)
    now = utcnow()
    await insert_window(engine, jsearch_window(now, limit=200, reserve=20, tokens=3, interval_s=SIX_H))
    body = await page(logged_in_client, QUOTA_URL)
    assert (body["serpapi"], body["adzuna"]) == (None, None)
    assert (body["jsearch"]["used"], body["jsearch"]["reserved"], body["jsearch"]["tokens"]) == (0, 0, 3.0)
