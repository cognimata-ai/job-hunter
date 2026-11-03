"""The run history and the run report over HTTP (spec §14.2 L5034-5035, §17.3; plan M4 Task 10, D5).

- `GET /api/runs?cursor&limit&status` → `{items, next_cursor}`: the runs newest first by `(queued_at, id)`,
  keyset-paged with a signed cursor whose filters hash the `status` filter, so a cursor of another filter or
  of another list is 400 `invalid_cursor`. Every run carries its `status_reason`, as `/api/system/health`'s
  `last_run` does.
- `GET /api/runs/{run_id}` → `{run, report, partial_causes}`: the stored report as it is, and the partial
  causes `build_report` wrote in `execution.partial`, an entry that is no partial cause dropped with a
  content-free warning; a no-op, an empty, a missing report or one without that list has none. An unknown run
  is 404.

Every datum is invented.
"""

import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Final, get_args

import httpx
import pytest
import sqlalchemy as sa
from jobhunter.api.app import JobHunterApp
from jobhunter.api.pagination import GOLDEN_CASES_SORT, RUNS_SORT, cursor_key, encode_list_cursor
from jobhunter.clock import utcnow
from jobhunter.graphs.collect.nodes.finalize import PartialCause, decide_status, run_revisions
from jobhunter.graphs.collect.report import FallbackRequests, build_report
from jobhunter.observability.lists import RunStatus
from jobhunter.types import JSONValue
from jobhunter.worker import runs
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.collect import queued_run
from tests.support.logs import logged
from tests.support.m3 import insert_id

pytestmark = pytest.mark.integration

URL: Final = "/api/runs"
RUN_FIELDS: Final = {
    "id",
    "kind",
    "trigger",
    "status",
    "status_reason",
    "scheduled_slot",
    "include_paid",
    "attempt_no",
    "queued_at",
    "started_at",
    "finished_at",
}
# The statuses that need a `finished_at` (the `runs` CHECK); `missed` and `skipped_overlap` never started.
ENDED: Final = frozenset({"succeeded", "partial", "failed", "failed_invariant", "cancelled"})
UNKNOWN_CAUSE_EVENT: Final = "run_report_unknown_partial_cause"


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


async def insert_run(
    engine: AsyncEngine,
    *,
    status: str,
    queued_at: datetime,
    status_reason: str | None = None,
    report: Mapping[str, JSONValue] | None = None,
) -> int:
    """A manual run of the active configuration in `status`, queued at `queued_at`; its id."""
    async with engine.begin() as c:
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
        if status in ENDED:
            values |= {
                "attempt_no": 1,
                "started_at": queued_at,
                "finished_at": queued_at + timedelta(minutes=5),
            }
        if report is not None:
            values["report"] = dict(report)
        return await insert_id(c, "runs", values)


async def page(client: httpx.AsyncClient, url: str = URL, **params: Any) -> dict[str, Any]:
    r = await client.get(url, params=params)
    assert r.status_code == 200, (r.status_code, r.text)
    body: dict[str, Any] = r.json()
    return body


def ids(body: dict[str, Any]) -> list[int]:
    return [item["id"] for item in body["items"]]


async def refused(client: httpx.AsyncClient, url: str, **params: Any) -> tuple[int, str]:
    r = await client.get(url, params=params)
    return r.status_code, r.json()["code"]


async def detail(client: httpx.AsyncClient, run_id: int) -> dict[str, Any]:
    return await page(client, f"{URL}/{run_id}")


# ---- the list ----------------------------------------------------------------------------------------------


async def test_runs_page_newest_first_with_status_reason(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, current_epoch: int
) -> None:
    now = utcnow()
    oldest = await insert_run(engine, status="succeeded", queued_at=now - timedelta(hours=3))
    failed = await insert_run(
        engine, status="failed", queued_at=now - timedelta(hours=2), status_reason="bug"
    )
    missed = await insert_run(engine, status="missed", queued_at=now - timedelta(hours=1))
    # the same `queued_at`: the higher id is the newer
    partial = await insert_run(
        engine, status="partial", queued_at=now - timedelta(hours=1), status_reason="fetch_failed"
    )
    body = await page(logged_in_client)
    assert (ids(body), body["next_cursor"]) == ([partial, missed, failed, oldest], None)
    assert [item["status_reason"] for item in body["items"]] == ["fetch_failed", None, "bug", None]
    first = body["items"][0]
    assert set(first) == RUN_FIELDS  # no report in the list
    assert first == {
        "id": partial,
        "kind": "collect",
        "trigger": "manual",
        "status": "partial",
        "status_reason": "fetch_failed",
        "scheduled_slot": None,
        "include_paid": False,
        "attempt_no": 1,
        "queued_at": iso(now - timedelta(hours=1)),
        "started_at": iso(now - timedelta(hours=1)),
        "finished_at": iso(now - timedelta(hours=1) + timedelta(minutes=5)),
    }
    r = await logged_in_client.get("/api/system/health")
    assert r.status_code == 200, r.text
    assert r.json()["last_run"] == first  # the health's run carries its reason too (D5)


async def test_runs_cursor_pages_and_a_golden_case_cursor_is_400(
    logged_in_client: httpx.AsyncClient, api_app: JobHunterApp, engine: AsyncEngine
) -> None:
    now = utcnow()
    made = [
        await insert_run(engine, status="succeeded", queued_at=now - timedelta(minutes=10 * n))
        for n in range(5)
    ]
    first = await page(logged_in_client, limit=2)
    assert ids(first) == made[:2] and first["next_cursor"] is not None
    second = await page(logged_in_client, limit=2, cursor=first["next_cursor"])
    assert ids(second) == made[2:4] and second["next_cursor"] is not None
    third = await page(logged_in_client, limit=2, cursor=second["next_cursor"])
    assert (ids(third), third["next_cursor"]) == (made[4:], None)
    assert ids(await page(logged_in_client)) == made  # the default limit takes them all

    key = cursor_key(api_app.api.state.cfg)
    golden = encode_list_cursor(sort=GOLDEN_CASES_SORT, dir="asc", last_key=[1], filters={}, key=key)
    assert (await page(logged_in_client, "/api/golden-cases", cursor=golden))["items"] == []  # its own list's
    assert await refused(logged_in_client, URL, cursor=golden) == (400, "invalid_cursor")
    # this list's sort, but not its key's shape: an id alone, a time without its zone, a bool for the id
    forgeries: list[list[JSONValue]] = [
        [made[0]],
        [now.replace(tzinfo=None).isoformat(), made[0]],
        [iso(now), True],
    ]
    for last_key in forgeries:
        forged = encode_list_cursor(sort=RUNS_SORT, dir="desc", last_key=last_key, filters={}, key=key)
        assert await refused(logged_in_client, URL, cursor=forged) == (400, "invalid_cursor"), last_key
    assert await refused(logged_in_client, URL, cursor="not-a-cursor") == (400, "invalid_cursor")
    for limit in (0, 101):
        assert await refused(logged_in_client, URL, limit=limit) == (422, "validation_failed")


async def test_the_status_filter_takes_exactly_the_statuses_of_the_check(engine: AsyncEngine) -> None:
    async with engine.connect() as c:
        definition = (
            await c.execute(
                sa.text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid = 'runs'::regclass AND conname = 'runs_status_check'"
                )
            )
        ).scalar_one()
    assert re.findall(r"'([a-z_]+)'::text", definition) == list(get_args(RunStatus))


async def test_status_filter_is_hashed(logged_in_client: httpx.AsyncClient, engine: AsyncEngine) -> None:
    now = utcnow()
    succeeded = [
        await insert_run(engine, status="succeeded", queued_at=now - timedelta(minutes=10 * n))
        for n in range(3)
    ]
    failed = await insert_run(
        engine, status="failed", queued_at=now - timedelta(hours=1), status_reason="bug"
    )
    first = await page(logged_in_client, status="succeeded", limit=2)
    assert ids(first) == succeeded[:2] and first["next_cursor"] is not None
    rest = await page(logged_in_client, status="succeeded", limit=2, cursor=first["next_cursor"])
    assert (ids(rest), rest["next_cursor"]) == (succeeded[2:], None)
    assert ids(await page(logged_in_client, status="failed")) == [failed]
    cursor = first["next_cursor"]
    assert await refused(logged_in_client, URL, status="failed", cursor=cursor) == (400, "invalid_cursor")
    assert await refused(logged_in_client, URL, cursor=cursor) == (400, "invalid_cursor")  # no filter
    assert await refused(logged_in_client, URL, status="done") == (422, "validation_failed")


# ---- the report --------------------------------------------------------------------------------------------


async def test_run_detail_has_report_and_partial_causes(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, current_epoch: int
) -> None:
    owner = await queued_run(engine)
    partial: list[PartialCause] = ["fetch_failed", "raw_item_rejected"]
    status, reason = decide_status([], partial)
    async with engine.begin() as c:
        report = await build_report(
            c,
            owner.run_id,
            invariants=[],
            partial=partial,
            revisions=await run_revisions(c, owner.run_id),
            fallback=FallbackRequests(planned=0, served=0),
            node_durations={"fetch": 1.25},
            counters={},
            alerts_raised=[],
        )
        await runs.finalize_run(c, owner, status=status, reason=reason, report=report)
    body = await detail(logged_in_client, owner.run_id)
    assert set(body) == {"run", "report", "partial_causes"}
    assert body["partial_causes"] == ["fetch_failed", "raw_item_rejected"]
    assert body["report"] == report  # as `build_report` wrote it
    assert set(body["run"]) == RUN_FIELDS
    assert (body["run"]["id"], body["run"]["status"], body["run"]["status_reason"]) == (
        owner.run_id,
        "partial",
        "fetch_failed",
    )


async def test_an_unknown_partial_cause_is_dropped(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    report: dict[str, JSONValue] = {
        "execution": {"partial": ["fetch_failed", "cause_from_the_future", 7, None, "raw_item_rejected"]}
    }
    run_id = await insert_run(
        engine, status="partial", queued_at=utcnow(), status_reason="fetch_failed", report=report
    )
    body = await detail(logged_in_client, run_id)
    assert body["partial_causes"] == ["fetch_failed", "raw_item_rejected"]
    assert body["report"] == report  # the report itself is as stored
    events = logged(caplog, UNKNOWN_CAUSE_EVENT)
    assert [(e["run_id"], e["count"], e["level"]) for e in events] == [(run_id, 3, "warning")]
    assert "cause_from_the_future" not in repr(events)  # the count, never the value


@pytest.mark.parametrize(
    "report",
    [
        {"noop": True},
        {},
        None,
        {"execution": {}},
        {"execution": {"partial": "fetch_failed"}},
        {"execution": ["fetch_failed"]},
    ],
    ids=["noop", "empty", "null", "no-partial", "partial-not-a-list", "execution-not-an-object"],
)
async def test_a_noop_an_empty_and_a_null_report_have_no_causes(
    logged_in_client: httpx.AsyncClient,
    engine: AsyncEngine,
    caplog: pytest.LogCaptureFixture,
    report: dict[str, JSONValue] | None,
) -> None:
    run_id = await insert_run(engine, status="succeeded", queued_at=utcnow(), report=report)
    body = await detail(logged_in_client, run_id)
    assert (body["run"]["id"], body["report"], body["partial_causes"]) == (run_id, report, [])
    assert logged(caplog, UNKNOWN_CAUSE_EVENT) == []


async def test_an_unknown_run_is_404(logged_in_client: httpx.AsyncClient, engine: AsyncEngine) -> None:
    run_id = await insert_run(engine, status="succeeded", queued_at=utcnow())
    assert await refused(logged_in_client, f"{URL}/{run_id + 1}") == (404, "not_found")
    for bad in ("0", "abc", str(2**63)):
        assert await refused(logged_in_client, f"{URL}/{bad}") == (422, "validation_failed"), bad
