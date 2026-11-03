"""The typed settings document and the dry-run's history (spec §6.8 L1137, §6.9.2, §13.4, §14.2; plan M4 Task
14, D11, D17, D22).

- `GET /api/settings` and `GET /api/settings/versions/{id}` publish the document as the `Settings` schema,
  and the dry-run its projections as `ProjectionsOut`; requests keep plain JSON, so an invalid draft still
  reaches the dry-run and comes back as field issues.
- `GET /api/settings` shows the pending scope's apply in both of its phases and whether the active settings
  version's scope is the applied one.
- The dry-run's ĉ is the active tuple's 7-day history while the proposed model and evaluation policies are
  the active ones, else the proposed policy's central estimate; its arrivals are the tuple's new targets (no
  bulk operation: no activation's catch-up) of the last 7 days a month, or 3,000 without 7 days of history,
  also within 7 days of the tuple's latest activation (Ruling T14-d); it warns of no re-score while no
  profile is active.

Every datum is invented.
"""

import copy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

import httpx
import pytest
import sqlalchemy as sa
from jobhunter.api.app import create_app
from jobhunter.clock import utcnow
from jobhunter.db.seed import load_settings_v1
from jobhunter.quota.windows import bootstrap_window
from jobhunter.settings import presets
from jobhunter.settings.llm_projection import central_cost_per_target
from jobhunter.worker.operations import execute_operation
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.m3 import insert_attempt, insert_job, insert_target
from tests.support.quota import SETTINGS, SIX_H, account, insert_window
from tests.support.reconcile import run_reconciler
from tests.support.settings import (
    activate_profile,
    active_row,
    fail_for_good,
    save,
    save_scope,
    settings_body,
)

pytestmark = pytest.mark.integration

DRY_RUN_URL: Final = "/api/settings/dry-run"
NOW: Final = datetime(2026, 10, 22, 9, 30, tzinfo=UTC)
SCHEMAS: Final = "#/components/schemas/"
CENTRAL_B: Final = "0.052646"
_CREATION: Final = sa.text(
    "SELECT id FROM operations WHERE dedup_key = :key AND status = 'pending' ORDER BY id DESC LIMIT 1"
)
# The tuple's targets in its newest bulk operation: those it created in the window, and those it revived.
_CATCH_UP: Final = sa.text(
    "SELECT count(*) FILTER (WHERE t.created_at >= :since AND t.created_at <= :now) AS created, "
    "count(*) FILTER (WHERE t.created_at < :since) AS revived FROM score_targets t "
    "WHERE t.llm_tuple_id = :tuple AND t.bulk_operation_id = (SELECT max(b.id) FROM bulk_operations b "
    "WHERE b.llm_tuple_id = :tuple)"
)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> datetime:
    monkeypatch.setattr("jobhunter.api.routes.settings.utcnow", lambda: NOW)
    return NOW


def seeded() -> dict[str, Any]:
    """The active (seeded) document."""
    doc: dict[str, Any] = copy.deepcopy(load_settings_v1())
    return doc


def changed(path: str, value: Any) -> dict[str, Any]:
    doc = seeded()
    *parents, key = path.split(".")
    node = doc
    for part in parents:
        node = node[part]
    node[key] = value
    return doc


async def dry_run(client: httpx.AsyncClient, payload: Any) -> dict[str, Any]:
    r = await client.post(DRY_RUN_URL, json={"payload": payload})
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


def codes(body: dict[str, Any]) -> list[str]:
    return [w["code"] for w in body["warnings"]]


# ---- the document ------------------------------------------------------------------------------------------


async def test_settings_payload_is_typed_in_the_document(logged_in_client: httpx.AsyncClient) -> None:
    schemas = create_app().api.openapi()["components"]["schemas"]
    assert schemas["SettingsOut"]["properties"]["payload"] == {"$ref": SCHEMAS + "Settings"}
    assert schemas["VersionDetailOut"]["properties"]["payload"] == {"$ref": SCHEMAS + "Settings"}
    assert set(schemas["Settings"]["properties"]) == set(load_settings_v1())
    policy = schemas["ModelPolicy"]["properties"]
    assert policy["preset"]["enum"] == ["A", "B", "C", "custom"]
    assert schemas["Llm"]["properties"]["cap_usd"]["type"] == "string"  # a decimal leaves as a string
    projections = schemas["DryRunOut"]["properties"]["projections"]
    assert {"$ref": SCHEMAS + "ProjectionsOut"} in projections["anyOf"]
    parts = schemas["ProjectionsOut"]
    assert set(parts["required"]) == {"jsearch", "adzuna", "arbeitnow", "serpapi_fallback", "llm"}
    assert set(parts["properties"]) - set(parts["required"]) == {
        "det_recompute",
        "critic_rate",
        "eval_gate",
        "next_slot",
    }
    assert set(schemas["SettingsOut"]["required"]) == {"version", "payload", "pending_scope", "scope_applied"}
    # a request's document stays plain JSON, whatever it holds
    for request in ("DryRunIn", "SaveIn"):
        assert schemas[request]["properties"]["payload"] == {"$ref": SCHEMAS + "JsonValue"}

    body = await settings_body(logged_in_client)
    assert body["payload"] == load_settings_v1()  # the stored normal form, typed
    version = await logged_in_client.get(f"/api/settings/versions/{body['version']['id']}")
    assert version.status_code == 200 and version.json()["payload"] == load_settings_v1()
    invalid = await dry_run(logged_in_client, changed("schedule.interval_hours", "6"))
    assert [(e["field"], e["code"]) for e in invalid["errors"]] == [
        ("payload.schedule.interval_hours", "invalid")
    ]
    assert (invalid["projections"], invalid["effects"]) == (None, [])
    valid = await dry_run(logged_in_client, changed("schedule.interval_hours", 12))
    assert set(valid["projections"]) == {
        "jsearch",
        "adzuna",
        "arbeitnow",
        "serpapi_fallback",
        "llm",
        "next_slot",
    }


async def test_the_serpapi_fallback_is_typed_with_its_window(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, clock: datetime
) -> None:
    serpapi = bootstrap_window(
        "serpapi", NOW, SETTINGS, interval_s=SIX_H, account=account(NOW, NOW + timedelta(days=12))
    )
    assert serpapi is not None
    await insert_window(engine, serpapi)
    fallback = (await dry_run(logged_in_client, seeded()))["projections"]["serpapi_fallback"]
    assert set(fallback) == {"units_per_run", "hourly_cap", "state", "window"}
    assert (fallback["hourly_cap"], fallback["state"]) == (45, "validated")
    assert isinstance(fallback["units_per_run"], float) and fallback["units_per_run"] > 0
    assert fallback["window"] == {
        "used_units": 0,
        "limit_units": 250,
        "reset_at": serpapi.window_end.isoformat().replace("+00:00", "Z"),
    }


async def test_progress_has_both_phases_and_scope_applied(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, current_epoch: int
) -> None:
    body = await settings_body(logged_in_client)
    assert (body["pending_scope"], body["scope_applied"]) == (None, True)  # the seeded scope is in force
    saved = await save_scope(logged_in_client, radius_km=40)
    op_id = saved["apply_scope_operation_id"]
    pending = (await active_row(engine)).pending_scope_version_id
    body = await settings_body(logged_in_client)
    zero = {"done": 0, "total": 0, "det_done": 0, "det_total": 0}
    assert (body["pending_scope"], body["scope_applied"]) == (
        {"version_id": pending, "progress": zero},
        False,
    )
    async with engine.begin() as conn:
        await conn.execute(
            sa.text(
                "UPDATE operations SET progress = "
                '\'{"done": 1201, "total": 1201, "det_done": 40, "det_total": 350}\' WHERE id = :i'
            ),
            {"i": op_id},
        )
    progress = (await settings_body(logged_in_client))["pending_scope"]["progress"]
    assert progress == {"done": 1201, "total": 1201, "det_done": 40, "det_total": 350}

    await fail_for_good(engine, op_id)  # no live apply, until the reconciler clears the pointer
    body = await settings_body(logged_in_client)
    assert (body["pending_scope"], body["scope_applied"]) == (
        {"version_id": pending, "progress": None},
        False,
    )
    assert await run_reconciler("scope_apply_failed", engine, now=NOW, epoch=current_epoch) == 1
    body = await settings_body(logged_in_client)
    # nothing pending, and the active version's scope is still not the active one: what retry-apply acts on
    assert (body["pending_scope"], body["scope_applied"]) == (None, False)
    assert body["version"]["id"] == saved["version"]["id"]


# ---- the dry-run's history (D22) ---------------------------------------------------------------------------


async def published_attempts(engine: AsyncEngine, llm_tuple_id: int, n: int, cost: str) -> None:
    """`n` charged online attempts of the tuple that ended published a day before `NOW`, at `cost` each."""
    async with engine.begin() as conn:
        target = await insert_target(conn, await insert_job(conn), llm_tuple_id)
        for number in range(1, n + 1):
            await insert_attempt(
                conn,
                target,
                attempt_no=number,
                outcome="published_accepted",
                charged=True,
                cost_usd=Decimal(cost),
                ended_at=NOW - timedelta(days=1),
            )


async def test_dry_run_uses_history_c_hat_for_unchanged_policies_and_central_otherwise(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, clock: datetime
) -> None:
    central = (await dry_run(logged_in_client, seeded()))["projections"]["llm"]  # no tuple
    # a central ĉ with its share of a cycle's cache writes: 93.10 / (0.052646 + 0.004923) = 1617.2
    assert (central["c_hat"], central["targets_per_month"]) == (CENTRAL_B, 1617)
    _, tuple_id = await activate_profile(engine)
    await published_attempts(engine, tuple_id, 19, "0.120000")
    central = (await dry_run(logged_in_client, seeded()))["projections"]["llm"]  # 19 < 20
    assert (central["c_hat"], central["targets_per_month"]) == (CENTRAL_B, 1617)
    await published_attempts(engine, tuple_id, 1, "0.120000")
    # the active policies: the tuple's 7-day mean, also for a change that touches neither policy
    for doc in (
        seeded(),
        changed("schedule.interval_hours", 12),
        changed("scoring.model_policy.preset", "C"),
    ):
        body = await dry_run(logged_in_client, doc)
        assert body["projections"]["llm"]["c_hat"] == "0.120000", doc
        # a history ĉ already holds its cache writes: 93.10 / 0.12 = 775.8, no share added
        assert body["projections"]["llm"]["targets_per_month"] == 775
    # another model policy: its own central estimate, not the tuple's history
    c_grid = presets.PRESETS["C"].policy().model_dump(mode="json")
    body = await dry_run(logged_in_client, changed("scoring.model_policy", c_grid))
    assert body["projections"]["llm"]["c_hat"] == "0.019352"
    c_central = central_cost_per_target(presets.PRESETS["C"].policy())
    assert Decimal(body["projections"]["llm"]["c_hat"]) == c_central
    # another evaluation policy over the same model policy: central too
    body = await dry_run(logged_in_client, changed("evaluation.t2_band", 3))
    assert body["projections"]["llm"]["c_hat"] == CENTRAL_B


async def recent_targets(
    engine: AsyncEngine, llm_tuple_id: int, *ages: timedelta, at: datetime = NOW, bulk: int | None = None
) -> None:
    """A target of a new job for each of `ages`, created that long before `at`, in bulk operation `bulk`
    (an activation's) or in none (an ordinary arrival)."""
    async with engine.begin() as conn:
        for age in ages:
            job = await insert_job(conn)
            await insert_target(conn, job, llm_tuple_id, created_at=at - age, bulk_operation_id=bulk)


_ACTIVATED_AT: Final = sa.text("UPDATE bulk_operations SET created_at = :t WHERE llm_tuple_id = :tuple")


async def activated_at(engine: AsyncEngine, llm_tuple_id: int, at: datetime) -> None:
    """The tuple's activations (its bulk operations) moved to `at`, as if it was activated then."""
    async with engine.begin() as conn:
        await conn.execute(_ACTIVATED_AT, {"t": at, "tuple": llm_tuple_id})


def arrivals(body: dict[str, Any]) -> int:
    found: int = body["projections"]["llm"]["arrival_per_month"]
    return found


async def test_dry_run_arrivals_from_the_seven_day_rate(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, clock: datetime
) -> None:
    assert arrivals(await dry_run(logged_in_client, seeded())) == 3000  # no tuple
    _, tuple_id = await activate_profile(engine)
    await activated_at(engine, tuple_id, NOW - timedelta(days=30))
    assert arrivals(await dry_run(logged_in_client, seeded())) == 3000  # a tuple without targets
    days = [timedelta(days=d, hours=1) for d in range(7)][:-1] * 2 + [timedelta(hours=2), timedelta(hours=3)]
    await recent_targets(engine, tuple_id, *days)  # 14 ordinary arrivals, all within the last 7 days
    assert arrivals(await dry_run(logged_in_client, seeded())) == 3000  # the oldest is younger than 7 days
    # an old one, and one created after `now`
    await recent_targets(engine, tuple_id, timedelta(days=10), timedelta(hours=-1))
    body = await dry_run(logged_in_client, seeded())
    assert arrivals(body) == 60  # 14 x 30 / 7
    assert "llm_cap_binding" not in codes(body)  # 60 arrive, about 1,617 fit
    await recent_targets(engine, tuple_id, timedelta(days=6, hours=23))
    assert arrivals(await dry_run(logged_in_client, seeded())) == 64  # 15 x 30 / 7 = 64.29
    await recent_targets(engine, tuple_id, timedelta(days=3))
    assert (
        arrivals(await dry_run(logged_in_client, seeded())) == 69
    )  # 16 x 30 / 7 = 68.57: the nearest, not 68


async def test_the_seven_days_include_their_first_instant(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, clock: datetime
) -> None:
    """The oldest target and the activation exactly 7 days before `now` take the history branch, and a target
    created exactly at the window's start is counted: 7 x 30 / 7 = 30 (6 would give 26, a young one 3,000)."""
    _, tuple_id = await activate_profile(engine)
    await activated_at(engine, tuple_id, NOW - timedelta(days=7))
    await recent_targets(engine, tuple_id, timedelta(days=7), *(timedelta(days=d) for d in range(1, 7)))
    assert arrivals(await dry_run(logged_in_client, seeded())) == 30


async def test_a_re_activated_tuple_waits_out_its_activation_and_never_counts_its_catch_up(
    logged_in_client: httpx.AsyncClient,
    engine: AsyncEngine,
    current_epoch: int,
    fake_llm_process: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ruling T14-d: switching the model policy away and back re-activates the profile's earlier tuple; its
    `create_targets_for_tuple` revives the old targets and creates one for each revision that became current
    meanwhile, all in the activation's bulk operation. Inside 7 days of that activation the arrivals are
    3,000; after them, the ordinary arrivals' rate, the catch-up left out."""
    clock = [utcnow()]  # the activations' and the catch-up's rows take the database's clock
    monkeypatch.setattr("jobhunter.api.routes.settings.utcnow", lambda: clock[0])
    _, tuple_id = await activate_profile(engine)
    await recent_targets(engine, tuple_id, timedelta(days=40), timedelta(days=39), at=clock[0])  # history
    await save(logged_in_client, changed("scoring.model_policy.nodes.critic.effort", "high"))
    assert (await active_row(engine)).llm_tuple_id != tuple_id
    async with engine.begin() as conn:
        for _ in range(5):
            await insert_job(conn)  # five revisions become current while another tuple is active
    await save(logged_in_client, seeded())  # back: the same tuple, activated again
    assert (await active_row(engine)).llm_tuple_id == tuple_id
    key = f"create_targets_for_tuple:{tuple_id}"
    async with engine.connect() as conn:
        creation = int((await conn.execute(_CREATION, {"key": key})).scalar_one())
    await execute_operation(engine, creation, epoch=current_epoch)
    now = clock[0] = utcnow()  # the dry-run's `now`, after the catch-up
    params = {"tuple": tuple_id, "since": now - timedelta(days=7), "now": now}
    async with engine.connect() as conn:
        caught_up = (await conn.execute(_CATCH_UP, params)).one()
    # in the dry-run's window, in the activation's bulk operation
    assert (caught_up.created, caught_up.revived) == (5, 2)
    assert arrivals(await dry_run(logged_in_client, seeded())) == 3000  # the latest activation is just now

    # 8 days on from the activations, with 7 ordinary arrivals in the window: 7 x 30 / 7, no catch-up
    async with engine.begin() as conn:
        await conn.execute(sa.text("UPDATE bulk_operations SET created_at = created_at - interval '8 days'"))
    await recent_targets(engine, tuple_id, *(timedelta(days=d, hours=1) for d in range(7)), at=now)
    assert arrivals(await dry_run(logged_in_client, seeded())) == 30  # (7 + 5) x 30 / 7 would be 51


async def test_no_rescore_warning_without_a_profile(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, clock: datetime
) -> None:
    async with engine.begin() as conn:
        for _ in range(3):
            await insert_job(conn)
    tuple_change = changed("scoring.model_policy.nodes.critic.effort", "high")
    body = await dry_run(logged_in_client, tuple_change)
    assert "llm_tuple" in body["effects"] and "llm_rescore" not in codes(body)
    await activate_profile(engine)
    body = await dry_run(logged_in_client, tuple_change)
    (rescore,) = [w["detail"] for w in body["warnings"] if w["code"] == "llm_rescore"]
    assert rescore == {"targets": 3, "est_usd": "0.157938", "bulk_budget_usd": "37.240000"}  # 3 x $0.052646
