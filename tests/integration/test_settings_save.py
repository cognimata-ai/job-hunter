"""The settings save (spec §13.2, §13.3, §3.4, §6.9.1; plan Task 17): `POST /api/settings` and
`settings.save.save_settings` on PostgreSQL.

Every payload is the seeded document, changed for the case; the places, queries and boards are synthetic (an
invented company board, invented query texts). A test that reads the month row or the schedule fixes the
route's clock (`jobhunter.api.routes.settings.utcnow`), which the save and its dry-run both use.
"""

import asyncio
import copy
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

import httpx
import pytest
import sqlalchemy as sa
from jobhunter.api.errors import Problem
from jobhunter.api.routes import settings as settings_routes
from jobhunter.db import ranking
from jobhunter.db.seed import ensure_hashed, load_settings_v1
from jobhunter.llm import spend_ledger
from jobhunter.llm.budget import BudgetParams
from jobhunter.llm.spend_ledger import ensure_month
from jobhunter.settings import save as save_module
from jobhunter.settings import scope_apply
from jobhunter.settings.derive import ensure_derived
from jobhunter.settings.effects import EFFECT_CLASSES
from jobhunter.settings.save import ACTION_ORDER, SaveRequest, save_settings
from jobhunter.settings.validate import validate_fields
from jobhunter.worker.tasks import defer_in_tx
from jobhunter.worker.tick import run_tick
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.faults import PausePoint, no_fault, raising_at
from tests.support.fixtures import count_rows
from tests.support.pg import PgServer, blocked_or_done
from tests.support.procs import await_until
from tests.support.profile import idem
from tests.support.settings import (
    SETTINGS_URL,
    activate_profile,
    active_row,
    berlin,
    fresh_key,
    generation,
    save,
    save_scope,
    settings_body,
    warning_codes,
)

pytestmark = pytest.mark.integration

NOW: Final = datetime(2026, 10, 22, 9, 30, tzinfo=UTC)  # 11:30 CEST: Berlin is at UTC+2 until 25 October
MONTH: Final = date(2026, 10, 1)
CLOCK: Final = "jobhunter.api.routes.settings.utcnow"


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> datetime:
    monkeypatch.setattr(CLOCK, lambda: NOW)
    return NOW


async def post(client: httpx.AsyncClient, body: dict[str, Any], key: str | None = None) -> httpx.Response:
    return await client.post(SETTINGS_URL, json=body, headers=idem(key or fresh_key()))


async def request_for(client: httpx.AsyncClient, payload: dict[str, Any]) -> dict[str, Any]:
    """A save request of `payload` over the active version, acknowledging exactly its own warnings."""
    active = await settings_body(client)
    return {
        "payload": payload,
        "base_version_id": active["version"]["id"],
        "acknowledged_warnings": await warning_codes(client, payload),
    }


async def active_payload(client: httpx.AsyncClient) -> dict[str, Any]:
    payload: dict[str, Any] = copy.deepcopy((await settings_body(client))["payload"])
    return payload


async def version_row(engine: AsyncEngine, version_id: int) -> sa.Row[Any]:
    async with engine.connect() as conn:
        return (
            await conn.execute(text("SELECT * FROM settings_versions WHERE id = :i"), {"i": version_id})
        ).one()


async def scalar(engine: AsyncEngine, statement: str, **params: object) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(text(statement), params)).scalar_one()


async def apply_scope_operations(engine: AsyncEngine) -> list[tuple[str, dict[str, Any], str]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT status, payload, dedup_key FROM operations WHERE kind = 'apply_scope' ORDER BY id")
        )
        return [(r.status, r.payload, r.dedup_key) for r in rows]


# --- Review Focus 5 pin -------------------------------------------------------------------------------------


async def test_stale_base_version_is_409(logged_in_client, engine) -> None:
    active = (await logged_in_client.get("/api/settings")).json()
    base, payload = active["version"]["id"], active["payload"]
    first = {**payload, "display": {**payload["display"], "ui_language": "de"}}
    second = {**payload, "schedule": {**payload["schedule"], "interval_hours": 12}}
    r1 = await logged_in_client.post("/api/settings", json={"payload": first, "base_version_id": base,
                                     "acknowledged_warnings": await warning_codes(logged_in_client, first)},
                                     headers=idem("tab-one-key-0001"))  # fmt: skip
    assert r1.status_code == 201
    r2 = await logged_in_client.post("/api/settings", json={"payload": second, "base_version_id": base,
                                     "acknowledged_warnings": await warning_codes(logged_in_client, second)},
                                     headers=idem("tab-two-key-0001"))  # fmt: skip
    assert r2.status_code == 409 and r2.json()["code"] == "stale_base_version"
    async with engine.connect() as conn:
        versions = (await conn.execute(text("SELECT count(*) FROM settings_versions"))).scalar_one()
        interval = (await conn.execute(text("SELECT interval_s FROM schedule_state"))).scalar_one()
    assert versions == 2 and interval == 6 * 3600  # the stale save wrote nothing


# --- the version row and the settings pointer ---------------------------------------------------------------


async def test_unchanged_payload_gives_none_and_moves_only_the_settings_pointer(
    logged_in_client, engine
) -> None:
    before = await active_row(engine)
    async with engine.connect() as conn:
        schedule_before = (await conn.execute(text("SELECT * FROM schedule_state"))).one()
    generation_before = await generation(engine)
    body = await save(logged_in_client, await active_payload(logged_in_client), key="unchanged-key-0001")
    assert body["version"]["effect_classes"] == ["none"] and body["apply_scope_operation_id"] is None
    after = await active_row(engine)
    assert after.settings_version_id == body["version"]["id"] != before.settings_version_id
    unchanged = (
        "scope_version_id",
        "pending_scope_version_id",
        "profile_version_id",
        "model_policy_id",
        "execution_config_id",
        "evaluation_policy_id",
        "llm_tuple_id",
        "rubric_version_id",
        "prompt_schema_version_id",
    )
    assert [getattr(after, c) for c in unchanged] == [getattr(before, c) for c in unchanged]
    row = await version_row(engine, body["version"]["id"])
    assert (row.version_no, row.schema_version, row.base_version_id) == (2, 1, before.settings_version_id)
    assert (row.scope_version_id, row.model_policy_id, row.execution_config_id, row.evaluation_policy_id) == (
        before.scope_version_id,
        before.model_policy_id,
        before.execution_config_id,
        before.evaluation_policy_id,
    )
    assert row.effect_classes == ["none"] and row.eval_override_ack is False
    assert row.acknowledged_warnings == ["llm_cap_binding"]  # the defaults' one warning, acknowledged
    async with engine.connect() as conn:
        schedule_after = (await conn.execute(text("SELECT * FROM schedule_state"))).one()
    assert schedule_after == schedule_before  # no schedule reset without the class
    assert await generation(engine) == generation_before
    assert await count_rows(engine, "operations") == 0 and await count_rows(engine, "llm_budget") == 0


async def test_the_normal_form_is_stored_and_compared(logged_in_client, engine) -> None:
    """Ruling 1 (carried from Task 15): the stored payload is `Settings.to_payload()`, never the request."""
    payload = await active_payload(logged_in_client)
    payload["evaluation"]["t2_straddle_max_coverage"] = "0.70"
    payload["scope"]["excluded_companies"] = ["Zalando SE"]
    first = await save(logged_in_client, payload)
    assert first["version"]["effect_classes"] == ["scope"]  # "0.70" is the stored "0.7": no evaluation change
    stored = (await version_row(engine, first["version"]["id"])).payload
    assert stored["evaluation"]["t2_straddle_max_coverage"] == "0.7"
    assert stored["scope"]["excluded_companies"] == ["zalando"]
    assert (await settings_body(logged_in_client))["payload"] == stored
    again = await active_payload(logged_in_client)
    again["evaluation"]["t2_straddle_max_coverage"] = "0.700"
    again["scope"]["excluded_companies"] = ["ZALANDO"]
    second = await save(logged_in_client, again)
    assert second["version"]["effect_classes"] == ["none"]
    first_row = await version_row(engine, first["version"]["id"])
    second_row = await version_row(engine, second["version"]["id"])
    assert second_row.payload == stored and second_row.scope_version_id == first_row.scope_version_id


async def test_a_missing_key_takes_its_default_in_the_stored_payload(logged_in_client, engine) -> None:
    payload = await active_payload(logged_in_client)
    del payload["notifications"]
    payload["display"] = {"ui_language": "de"}  # timezone left out: its default
    body = await save(logged_in_client, payload)
    stored = (await version_row(engine, body["version"]["id"])).payload
    assert stored["notifications"] == {"push_min_severity": "warn"}
    assert stored["display"] == {"timezone": "Europe/Berlin", "ui_language": "de"}


# --- warnings and validation --------------------------------------------------------------------------------


async def test_a_warning_missing_from_the_acknowledgements_is_422(logged_in_client, engine) -> None:
    payload = await active_payload(logged_in_client)
    payload["sources"]["enabled"]["serpapi"] = False
    request = await request_for(logged_in_client, payload)
    assert sorted(request["acknowledged_warnings"]) == ["llm_cap_binding", "serpapi_disabled_no_fallback"]
    r = await post(logged_in_client, {**request, "acknowledged_warnings": ["llm_cap_binding"]})
    assert r.status_code == 422, r.text
    assert r.json()["code"] == "validation_failed"
    assert r.json()["errors"] == [
        {
            "field": "acknowledged_warnings",
            "code": "warning_not_acknowledged",
            "message": "serpapi_disabled_no_fallback",
        }
    ]
    assert await count_rows(engine, "settings_versions") == 1
    nothing = await post(logged_in_client, {**request, "acknowledged_warnings": []})
    assert [e["message"] for e in nothing.json()["errors"]] == request["acknowledged_warnings"]
    r = await post(logged_in_client, request)
    assert r.status_code == 201, r.text
    stored = await version_row(engine, r.json()["version"]["id"])
    assert stored.acknowledged_warnings == ["llm_cap_binding", "serpapi_disabled_no_fallback"]  # sorted


async def test_an_invalid_payload_is_422_with_the_field_paths(logged_in_client, engine) -> None:
    payload = await active_payload(logged_in_client)
    payload["schedule"]["interval_hours"] = 0
    payload["scope"]["locations"] = [berlin(0)]
    request = {"payload": payload, "base_version_id": (await active_row(engine)).settings_version_id}
    r = await post(logged_in_client, {**request, "acknowledged_warnings": []})
    assert r.status_code == 422 and r.json()["code"] == "validation_failed"
    _, issues = validate_fields(payload)
    expected = [{"field": i.field, "code": i.code, "message": i.message} for i in issues]
    assert r.json()["errors"] == expected
    assert [e["field"] for e in expected] == [
        "payload.schedule.interval_hours",
        "payload.scope.locations[0].radius_km",
    ]
    cross = await active_payload(logged_in_client)
    cross["evaluation"]["t3_min_mid"] = 60  # not above relevance_threshold 60
    r = await post(logged_in_client, {"payload": cross, "base_version_id": request["base_version_id"],
                                      "acknowledged_warnings": []})  # fmt: skip
    assert [(e["field"], e["code"]) for e in r.json()["errors"]] == [
        ("payload.evaluation.t3_min_mid", "conflict")
    ]
    assert await count_rows(engine, "settings_versions") == 1


async def test_the_request_body_is_checked(logged_in_client, engine) -> None:
    payload = await active_payload(logged_in_client)
    base = (await active_row(engine)).settings_version_id
    for body in (
        {"payload": payload, "base_version_id": base},  # acknowledged_warnings is required
        {"payload": payload, "base_version_id": base, "acknowledged_warnings": [], "extra": 1},
        {"payload": payload, "base_version_id": 0, "acknowledged_warnings": []},
        {"payload": payload, "base_version_id": base, "acknowledged_warnings": ["Not A Code"]},
    ):
        r = await post(logged_in_client, body)
        assert r.status_code == 422 and r.json()["code"] == "validation_failed", body
    r = await logged_in_client.post(
        SETTINGS_URL, json={"payload": payload, "base_version_id": base, "acknowledged_warnings": []}
    )
    assert r.status_code == 400 and r.json()["code"] == "idempotency_key_required"
    assert await count_rows(engine, "settings_versions") == 1


async def dry_run_errors(client: httpx.AsyncClient, payload: Any) -> list[tuple[str, str]]:
    r = await client.post("/api/settings/dry-run", json={"payload": payload})
    assert r.status_code == 200, r.text
    return [(e["field"], e["code"]) for e in r.json()["errors"]]


async def assert_refused_like_the_dry_run(
    client: httpx.AsyncClient, engine: AsyncEngine, payload: Any, expected: list[tuple[str, str]]
) -> None:
    """The preview shows `expected` as its errors (200), and the save refuses the same with a 422; nothing
    is written."""
    assert await dry_run_errors(client, payload) == expected
    base = (await active_row(engine)).settings_version_id
    r = await post(client, {"payload": payload, "base_version_id": base, "acknowledged_warnings": []})
    assert r.status_code == 422 and r.json()["code"] == "validation_failed", r.text
    assert [(e["field"], e["code"]) for e in r.json()["errors"]] == expected
    assert await count_rows(engine, "settings_versions") == 1 and await count_rows(engine, "operations") == 0


async def test_unknown_queries_and_watchlist_companies_are_refused(logged_in_client, engine) -> None:
    payload = await active_payload(logged_in_client)
    payload["queries"] = [{"query_id": 987654, "enabled": True}]
    payload["watchlist"] = [{"watchlist_company_id": 876543, "enabled": False}]
    expected = [
        ("payload.queries[0].query_id", "invalid"),
        ("payload.watchlist[0].watchlist_company_id", "invalid"),
    ]
    await assert_refused_like_the_dry_run(logged_in_client, engine, payload, expected)
    assert await count_rows(engine, "source_instances") == 4


@pytest.mark.parametrize(
    ("group", "entry", "path"),
    [
        ("queries", {"query_id": 2**63, "enabled": True}, "payload.queries[0].query_id"),
        (
            "watchlist",
            {"watchlist_company_id": 2**63, "enabled": True},
            "payload.watchlist[0].watchlist_company_id",
        ),
    ],
)
async def test_an_id_beyond_bigint_is_refused_not_a_500(logged_in_client, engine, group, entry, path) -> None:
    payload = await active_payload(logged_in_client)
    payload[group] = [entry]
    await assert_refused_like_the_dry_run(logged_in_client, engine, payload, [(path, "out_of_range")])


async def test_a_nul_character_in_a_string_is_refused_not_a_500(logged_in_client, engine) -> None:
    payload = await active_payload(logged_in_client)
    payload["scope"]["excluded_keywords"] = ["Prak\u0000tikum"]  # JSON "\\u0000", which no JSONB stores
    payload["scope"]["locations"] = [berlin(10) | {"label": "Ber\u0000lin"}]
    expected = [
        ("payload.scope.locations[0].label", "invalid"),
        ("payload.scope.excluded_keywords[0]", "invalid"),
    ]
    await assert_refused_like_the_dry_run(logged_in_client, engine, payload, expected)


# --- schedule -----------------------------------------------------------------------------------------------


async def test_a_schedule_save_resets_the_slot_cursor(logged_in_client, engine, clock, queue_app) -> None:
    async with engine.begin() as conn:  # an old cursor: without the reset, the next tick records missed slots
        await conn.execute(
            text("UPDATE schedule_state SET last_evaluated_slot = :t"), {"t": NOW - timedelta(days=1)}
        )
    payload = await active_payload(logged_in_client)
    payload["schedule"]["interval_hours"] = 4
    body = await save(logged_in_client, payload)
    assert body["version"]["effect_classes"] == ["schedule"]
    async with engine.connect() as conn:
        state = (await conn.execute(text("SELECT * FROM schedule_state"))).one()
    # 06:00 in Berlin on the save date (CEST) is 04:00 UTC; the slots are 04:00 and 08:00 UTC
    assert state.anchor_utc == datetime(2026, 10, 22, 4, tzinfo=UTC)
    assert (state.interval_s, state.enabled) == (4 * 3600, True)
    assert state.last_evaluated_slot == datetime(2026, 10, 22, 8, tzinfo=UTC)  # the latest slot <= now
    assert state.settings_version_id == body["version"]["id"] and state.updated_at == NOW
    async with engine.begin() as conn:
        report = await run_tick(conn, now=NOW, defer=defer_in_tx)
    assert (report.missed, report.queued_run_id) == (0, None)  # an edit never fires a run or a missed slot
    async with engine.begin() as conn:
        report = await run_tick(conn, now=datetime(2026, 10, 22, 12, 0, 30, tzinfo=UTC), defer=defer_in_tx)
    assert report.missed == 0 and report.queued_run_id is not None
    async with engine.connect() as conn:
        runs = (await conn.execute(text("SELECT scheduled_slot, status, trigger FROM runs"))).all()
    assert [tuple(r) for r in runs] == [(datetime(2026, 10, 22, 12, tzinfo=UTC), "queued", "schedule")]


async def test_disabling_the_schedule_is_a_schedule_save(logged_in_client, engine, clock) -> None:
    payload = await active_payload(logged_in_client)
    payload["schedule"]["enabled"] = False
    payload["schedule"]["anchor_local"] = "07:15"
    body = await save(logged_in_client, payload)
    assert body["version"]["effect_classes"] == ["schedule"]
    async with engine.connect() as conn:
        state = (await conn.execute(text("SELECT * FROM schedule_state"))).one()
    assert (state.enabled, state.anchor_utc) == (False, datetime(2026, 10, 22, 5, 15, tzinfo=UTC))
    assert state.last_evaluated_slot == datetime(2026, 10, 22, 5, 15, tzinfo=UTC)  # 6 h slots from 05:15 UTC


# --- collection ---------------------------------------------------------------------------------------------


async def insert_query(engine: AsyncEngine, query_text: str) -> int:
    async with engine.begin() as conn:
        return int(
            (
                await conn.execute(
                    text("INSERT INTO queries (text, lang, origin) VALUES (:t, 'en', 'user') RETURNING id"),
                    {"t": query_text},
                )
            ).scalar_one()
        )


async def insert_board(engine: AsyncEngine, slug: str) -> int:
    async with engine.begin() as conn:
        return int(
            (
                await conn.execute(
                    text(
                        "INSERT INTO watchlist_companies (display_name, company_key, ats_type, board_slug, "
                        "detection) VALUES ('Musterfirma', 'musterfirma', 'greenhouse', :s, 'manual') "
                        "RETURNING id"
                    ),
                    {"s": slug},
                )
            ).scalar_one()
        )


async def cells(engine: AsyncEngine) -> list[tuple[str, str, int | None, str | None, bool]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT i.source_id, i.instance_key, c.query_id, c.location_key, "
                "c.last_attempted_at IS NULL AND c.last_covered_at IS NULL AND c.last_success_at IS NULL "
                "FROM coverage_cells c JOIN source_instances i ON i.id = c.source_instance_id "
                "ORDER BY i.source_id, c.query_id NULLS FIRST, c.location_key NULLS FIRST"
            )
        )
        return [tuple(r) for r in rows]


async def test_a_collection_save_creates_cells_and_board_instances(logged_in_client, engine) -> None:
    query = await insert_query(engine, "Data Engineer")
    board = await insert_board(engine, "musterfirma")
    payload = await active_payload(logged_in_client)
    payload["scope"]["locations"] = [berlin(30)]
    payload["queries"] = [{"query_id": query, "enabled": True}]
    payload["watchlist"] = [{"watchlist_company_id": board, "enabled": True}]
    body = await save(logged_in_client, payload)
    assert body["version"]["effect_classes"] == ["collection", "scope"]
    async with engine.connect() as conn:
        instance = (
            await conn.execute(
                text("SELECT source_id, instance_key FROM source_instances WHERE watchlist_company_id = :w"),
                {"w": board},
            )
        ).one()
    assert tuple(instance) == ("greenhouse", "musterfirma")
    never_covered = True
    assert await cells(engine) == [
        ("adzuna", "default", query, "berlin", never_covered),
        ("adzuna", "default", query, "remote-de", never_covered),
        ("arbeitnow", "default", None, None, never_covered),
        ("greenhouse", "musterfirma", None, None, never_covered),
        ("jsearch", "default", query, "berlin", never_covered),
        ("jsearch", "default", query, "remote-de", never_covered),
        ("serpapi", "default", query, "berlin", never_covered),
        ("serpapi", "default", query, "remote-de", never_covered),
    ]
    # a second query, enabled later, gets its cells too; the first query's cells are kept as they were
    second = await insert_query(engine, "Analytics Engineer")
    payload = await active_payload(logged_in_client)
    payload["queries"].append({"query_id": second, "enabled": True})
    assert (await save(logged_in_client, payload))["version"]["effect_classes"] == ["collection"]
    assert sum(1 for c in await cells(engine) if c[2] == second) == 6 and len(await cells(engine)) == 14


async def test_a_disabled_board_gets_its_instance_but_no_cell(logged_in_client, engine) -> None:
    board = await insert_board(engine, "musterfirma-off")
    payload = await active_payload(logged_in_client)
    payload["watchlist"] = [{"watchlist_company_id": board, "enabled": False}]
    assert (await save(logged_in_client, payload))["version"]["effect_classes"] == ["collection"]
    assert await count_rows(engine, "source_instances", watchlist_company_id=board) == 1
    assert [c for c in await cells(engine) if c[0] == "greenhouse"] == []


# --- budget -------------------------------------------------------------------------------------------------


async def month_row(engine: AsyncEngine) -> sa.Row[Any]:
    async with engine.connect() as conn:
        return (await conn.execute(text("SELECT * FROM llm_budget WHERE month = :m"), {"m": MONTH})).one()


async def test_a_raised_cap_writes_the_month_row(logged_in_client, engine, clock) -> None:
    payload = await active_payload(logged_in_client)
    payload["llm"]["cap_usd"] = "200"
    body = await save(logged_in_client, payload)
    assert body["version"]["effect_classes"] == ["budget"]
    row = await month_row(engine)  # created by the save, then changed under its lock
    assert (row.cap_usd, row.overrun_buffer_usd, row.cap_lowered_at) == (
        Decimal("200.00"),
        Decimal("2.00"),
        None,
    )
    assert row.bucket_at == NOW and row.updated_at == NOW


async def test_a_cap_lowered_below_the_counted_spend_records_the_change(
    logged_in_client, engine, clock
) -> None:
    async with engine.begin() as conn:
        await ensure_month(
            conn, MONTH, BudgetParams.from_payload(load_settings_v1()), NOW - timedelta(days=3)
        )
        await conn.execute(
            text("UPDATE llm_budget SET counted_usd = 60, nonscoring_counted_usd = 60 WHERE month = :m"),
            {"m": MONTH},
        )
    payload = await active_payload(logged_in_client)
    payload["llm"]["cap_usd"] = "50.00"
    payload["llm"]["overrun_buffer_usd"] = "2.50"
    await save(logged_in_client, payload)
    row = await month_row(engine)
    assert (row.cap_usd, row.overrun_buffer_usd) == (Decimal("50.00"), Decimal("2.50"))
    assert (row.cap_lowered_at, row.counted_at_cap_change_usd) == (NOW, Decimal("60.000000"))


async def test_a_budget_save_without_a_cap_change_leaves_the_month_row(
    logged_in_client, engine, clock
) -> None:
    async with engine.begin() as conn:
        await ensure_month(
            conn, MONTH, BudgetParams.from_payload(load_settings_v1()), NOW - timedelta(days=3)
        )
        await conn.execute(
            text("UPDATE llm_budget SET counted_usd = 120, nonscoring_counted_usd = 120, "
                 "cap_lowered_at = :t, counted_at_cap_change_usd = 120 WHERE month = :m"),
            {"m": MONTH, "t": NOW - timedelta(days=1)},
        )  # fmt: skip
    before = await month_row(engine)
    payload = await active_payload(logged_in_client)
    payload["scoring"]["max_targets_per_cycle"] = 40
    assert (await save(logged_in_client, payload))["version"]["effect_classes"] == ["budget"]
    assert await month_row(engine) == before  # the lowering stays recorded at its own time


async def test_a_buffer_only_edit_writes_the_buffer_and_keeps_the_lowering(
    logged_in_client, engine, clock
) -> None:
    lowered_at = NOW - timedelta(days=1)
    async with engine.begin() as conn:
        await ensure_month(
            conn, MONTH, BudgetParams.from_payload(load_settings_v1()), NOW - timedelta(days=3)
        )
        await conn.execute(
            text("UPDATE llm_budget SET counted_usd = 120, nonscoring_counted_usd = 120, "
                 "cap_lowered_at = :t, counted_at_cap_change_usd = 120 WHERE month = :m"),
            {"m": MONTH, "t": lowered_at},
        )  # fmt: skip
    payload = await active_payload(logged_in_client)
    payload["llm"]["overrun_buffer_usd"] = "3.00"
    assert (await save(logged_in_client, payload))["version"]["effect_classes"] == ["budget"]
    row = await month_row(engine)
    assert (row.cap_usd, row.overrun_buffer_usd) == (Decimal("100.00"), Decimal("3.00"))
    assert (row.cap_lowered_at, row.counted_at_cap_change_usd) == (lowered_at, Decimal("120.000000"))
    assert row.updated_at == NOW


# --- execution and evaluation policy ------------------------------------------------------------------------


async def test_execution_and_evaluation_policy_flip_their_pointers(logged_in_client, engine) -> None:
    before = await active_row(engine)
    payload = await active_payload(logged_in_client)
    payload["scoring"]["execution"]["generate"]["deadline_s"] = 900
    body = await save(logged_in_client, payload)
    assert body["version"]["effect_classes"] == ["execution"]
    after = await active_row(engine)
    row = await version_row(engine, body["version"]["id"])
    assert after.execution_config_id == row.execution_config_id != before.execution_config_id
    assert after.evaluation_policy_id == before.evaluation_policy_id
    payload["evaluation"]["t2_band"] = 7
    body = await save(logged_in_client, payload)
    assert body["version"]["effect_classes"] == ["evaluation_policy"]
    flipped = await active_row(engine)
    assert flipped.execution_config_id == after.execution_config_id
    async with engine.connect() as conn:
        policy = (
            await conn.execute(
                text("SELECT * FROM evaluation_policies WHERE id = :i"), {"i": flipped.evaluation_policy_id}
            )
        ).one()
    assert policy.id != before.evaluation_policy_id and policy.version_no == 2
    assert policy.projected_critic_rate == Decimal("0.450") and policy.payload["t2_band"] == 7


# --- llm_tuple ----------------------------------------------------------------------------------------------


def with_generator_effort(payload: dict[str, Any], effort: str) -> dict[str, Any]:
    payload["scoring"]["model_policy"]["nodes"]["generate"]["effort"] = effort
    return payload


async def test_an_llm_tuple_save_without_a_profile_flips_the_model_policy_only(
    logged_in_client, engine
) -> None:
    before = await active_row(engine)
    generation_before = await generation(engine)
    payload = with_generator_effort(await active_payload(logged_in_client), "high")
    assert "llm_rescore" in await warning_codes(logged_in_client, payload)
    body = await save(logged_in_client, payload)
    assert body["version"]["effect_classes"] == ["llm_tuple"]
    after = await active_row(engine)
    assert after.model_policy_id == (await version_row(engine, body["version"]["id"])).model_policy_id
    assert after.model_policy_id != before.model_policy_id
    assert (after.profile_version_id, after.llm_tuple_id) == (None, None)
    assert await count_rows(engine, "llm_tuples") == 0 and await generation(engine) == generation_before


async def test_an_llm_tuple_save_with_a_profile_flips_the_tuple_and_bumps(logged_in_client, engine) -> None:
    profile_id, old_tuple = await activate_profile(engine)
    before = await active_row(engine)
    generation_before = await generation(engine)
    body = await save(logged_in_client, with_generator_effort(await active_payload(logged_in_client), "high"))
    assert body["version"]["effect_classes"] == ["llm_tuple"]
    after = await active_row(engine)
    assert after.llm_tuple_id != old_tuple and after.profile_version_id == profile_id
    async with engine.connect() as conn:
        new_tuple = (
            await conn.execute(text("SELECT * FROM llm_tuples WHERE id = :i"), {"i": after.llm_tuple_id})
        ).one()
    assert (new_tuple.profile_version_id, new_tuple.model_policy_id) == (profile_id, after.model_policy_id)
    assert (new_tuple.rubric_version_id, new_tuple.prompt_schema_version_id) == (
        before.rubric_version_id,
        before.prompt_schema_version_id,
    )
    assert await generation(engine) == generation_before + 1
    # reverting the policy reuses the first tuple (select-or-insert), and bumps again
    reverted = await save(
        logged_in_client, with_generator_effort(await active_payload(logged_in_client), "medium")
    )
    assert reverted["version"]["effect_classes"] == ["llm_tuple"]
    assert (await active_row(engine)).llm_tuple_id == old_tuple
    assert await count_rows(engine, "llm_tuples") == 2 and await generation(engine) == generation_before + 2


async def test_the_eval_gate_needs_the_override_once_it_has_failed(
    logged_in_client, engine, monkeypatch
) -> None:
    """M2's gate is always uncalibrated (fewer than 30 golden cases); a failed one needs the override
    (§11.13)."""

    async def failed(conn: object) -> str:
        return "failed"

    derived_rows: list[object] = []
    real_ensure_derived = ensure_derived

    async def spy_ensure_derived(*args: Any, **kwargs: Any) -> Any:
        derived_rows.append(args)
        return await real_ensure_derived(*args, **kwargs)

    monkeypatch.setattr(save_module, "eval_gate_state", failed)
    monkeypatch.setattr(save_module, "ensure_derived", spy_ensure_derived)
    payload = with_generator_effort(await active_payload(logged_in_client), "high")
    payload["scope"]["locations"] = [berlin(30)]  # a scope change too: refused before any write
    request = await request_for(logged_in_client, payload)
    r = await post(logged_in_client, request)
    assert r.status_code == 409 and r.json()["code"] == "eval_gate_failed"
    assert await count_rows(engine, "operations") == 0 and await count_rows(engine, "scope_versions") == 1
    assert await count_rows(engine, "settings_versions") == 1
    evaluation_only = await active_payload(logged_in_client)
    evaluation_only["evaluation"]["t2_band"] = 8
    r = await post(logged_in_client, await request_for(logged_in_client, evaluation_only))
    assert r.status_code == 409 and r.json()["code"] == "eval_gate_failed"  # evaluation policies are gated
    assert derived_rows == []  # the gate refused both before step 4's first write
    r = await post(logged_in_client, {**request, "eval_override_ack": True})
    assert r.status_code == 201, r.text
    assert (await version_row(engine, r.json()["version"]["id"])).eval_override_ack is True


# --- scope --------------------------------------------------------------------------------------------------


async def test_a_scope_save_points_pending_at_the_scope_and_requests_its_apply(
    logged_in_client, engine
) -> None:
    before = await active_row(engine)
    first = await save_scope(logged_in_client, radius_km=30)
    assert first["version"]["effect_classes"] == ["collection", "scope"]
    scope_a = (await version_row(engine, first["version"]["id"])).scope_version_id
    after = await active_row(engine)
    assert (after.scope_version_id, after.pending_scope_version_id) == (before.scope_version_id, scope_a)
    assert await apply_scope_operations(engine) == [
        ("pending", {"scope_version_id": scope_a}, f"apply_scope:{scope_a}")
    ]
    op_id = first["apply_scope_operation_id"]
    async with engine.connect() as conn:  # its job is deferred in the save's transaction
        jobs = (
            (
                await conn.execute(
                    text("SELECT queueing_lock FROM procrastinate_jobs WHERE task_name = 'jh:run_operation'")
                )
            )
            .scalars()
            .all()
        )
    assert jobs == [f"op:{op_id}"]
    # another scope (B), then scope A again while A's apply is still live: that apply is reused
    payload = await active_payload(logged_in_client)
    payload["scope"]["excluded_keywords"] = ["Praktikum"]
    other = await save(logged_in_client, payload)
    payload["scope"]["excluded_keywords"] = []
    same = await save(logged_in_client, payload)
    assert same["apply_scope_operation_id"] == op_id and other["apply_scope_operation_id"] != op_id
    assert (await active_row(engine)).pending_scope_version_id == scope_a
    assert [op[0] for op in await apply_scope_operations(engine)] == ["pending", "pending"]


async def test_set_desired_scope_stamps_the_callers_now(engine) -> None:
    """The pending pointer's `updated_at` is the caller's injected `now` (Tasks 18 and 21 do not overwrite
    it), for a new pending scope and for the clearing revert."""
    active = await active_row(engine)
    async with engine.begin() as conn:
        await conn.execute(text("SELECT 1 FROM active_config WHERE id = 1 FOR UPDATE"))
        other = await ensure_hashed(conn, "scope_versions", {"test": "another scope"})
        op_id = await scope_apply.set_desired_scope(conn, other, now=NOW, defer=defer_in_tx)
    after = await active_row(engine)
    assert (after.pending_scope_version_id, after.updated_at) == (other, NOW) and op_id is not None
    async with engine.begin() as conn:
        await conn.execute(text("SELECT 1 FROM active_config WHERE id = 1 FOR UPDATE"))
        again = await scope_apply.set_desired_scope(conn, other, now=NOW, defer=defer_in_tx)
        cleared = await scope_apply.set_desired_scope(
            conn, active.scope_version_id, now=NOW + timedelta(hours=1), defer=defer_in_tx
        )
    assert again == op_id and cleared is None  # the live apply is found through its dedup key
    after = await active_row(engine)
    assert (after.pending_scope_version_id, after.updated_at) == (None, NOW + timedelta(hours=1))


async def test_a_scope_save_that_reverts_the_scope_clears_pending(logged_in_client, engine) -> None:
    before = await active_row(engine)
    await save_scope(logged_in_client, radius_km=30)
    payload = await active_payload(logged_in_client)
    payload["scope"]["locations"] = []
    reverted = await save(logged_in_client, payload)
    assert reverted["version"]["effect_classes"] == ["collection", "scope"]
    assert reverted["apply_scope_operation_id"] is None
    after = await active_row(engine)
    assert (after.scope_version_id, after.pending_scope_version_id) == (before.scope_version_id, None)
    assert (await version_row(engine, reverted["version"]["id"])).scope_version_id == before.scope_version_id
    # the apply of the replaced scope is left to end obsolete at its own final check (Task 18)
    assert [op[0] for op in await apply_scope_operations(engine)] == ["pending"]


async def test_the_scope_is_derived_with_the_active_profile(logged_in_client, engine) -> None:
    """Deviation 14: a `profile_relative` range is resolved with the active profile at every save."""
    await activate_profile(engine, seniority="senior")
    body = await save_scope(logged_in_client, radius_km=30)
    async with engine.connect() as conn:
        scope = (
            await conn.execute(
                text("SELECT s.payload FROM scope_versions s "
                     "JOIN settings_versions v ON v.scope_version_id = s.id WHERE v.id = :i"),
                {"i": body["version"]["id"]},
            )
        ).scalar_one()  # fmt: skip
    assert scope["seniority_range"] == {
        "mode": "explicit",
        "below": 1,
        "above": 1,
        "min": "mid",
        "max": "lead",
    }
    # the same range set explicitly is another settings document but the same scope, already pending
    payload = await active_payload(logged_in_client)
    payload["scope"]["seniority_range"] = {
        "mode": "explicit",
        "below": 1,
        "above": 1,
        "min": "mid",
        "max": "lead",
    }
    explicit = await save(logged_in_client, payload)
    assert explicit["version"]["effect_classes"] == ["scope"]
    assert explicit["apply_scope_operation_id"] == body["apply_scope_operation_id"]
    pending = (await active_row(engine)).pending_scope_version_id
    assert pending == (await version_row(engine, explicit["version"]["id"])).scope_version_id
    assert [op[0] for op in await apply_scope_operations(engine)] == ["pending"]


# --- several classes in one save ----------------------------------------------------------------------------


def test_the_action_order_is_llm_tuple_then_scope_then_the_others() -> None:
    assert ACTION_ORDER[:2] == ("llm_tuple", "scope")
    assert set(ACTION_ORDER) == set(EFFECT_CLASSES) - {"none"}
    # §9.1: the month row before the other domain rows; the cells after the schedule they read
    assert ACTION_ORDER.index("budget") < ACTION_ORDER.index("schedule") < ACTION_ORDER.index("collection")


async def test_a_fault_between_llm_tuple_and_scope_rolls_both_back(
    logged_in_client, engine, monkeypatch
) -> None:
    await activate_profile(engine)
    before = await active_row(engine)
    generation_before = await generation(engine)
    payload = with_generator_effort(await active_payload(logged_in_client), "high")
    payload["scope"]["locations"] = [berlin(30)]
    request = await request_for(logged_in_client, payload)
    monkeypatch.setattr(save_module, "afault_point", raising_at("settings.after_llm_tuple"))
    r = await post(logged_in_client, request, key="fault-key-000000001")
    assert r.status_code == 500 and r.json()["code"] == "internal"
    assert await active_row(engine) == before and await generation(engine) == generation_before
    assert await count_rows(engine, "llm_tuples") == 1  # the active profile's own tuple only
    assert await count_rows(engine, "settings_versions") == 1 and await count_rows(engine, "operations") == 0
    assert await count_rows(engine, "api_idempotency") == 0  # the key's claim rolled back with the rest
    monkeypatch.setattr(save_module, "afault_point", no_fault)
    r = await post(logged_in_client, request, key="fault-key-000000001")  # the client's retry
    assert r.status_code == 201, r.text
    assert r.json()["version"]["effect_classes"] == ["collection", "llm_tuple", "scope"]
    after = await active_row(engine)
    assert after.llm_tuple_id != before.llm_tuple_id and after.pending_scope_version_id is not None
    assert await count_rows(engine, "llm_tuples") == 2 and await generation(engine) == generation_before + 1
    assert [op[0] for op in await apply_scope_operations(engine)] == ["pending"]


async def test_llm_tuple_and_scope_commit_together(logged_in_client, engine, monkeypatch) -> None:
    await activate_profile(engine)
    payload = with_generator_effort(await active_payload(logged_in_client), "high")
    payload["scope"]["locations"] = [berlin(30)]
    request = await request_for(logged_in_client, payload)
    pause = PausePoint("settings.after_llm_tuple")
    monkeypatch.setattr(save_module, "afault_point", pause)
    task = asyncio.create_task(post(logged_in_client, request))
    await asyncio.wait_for(pause.reached.wait(), 10)
    # the tuple row and the model-policy flip are written, uncommitted: another connection sees neither
    assert await count_rows(engine, "llm_tuples") == 1 and await count_rows(engine, "operations") == 0
    pause.resume()
    r = await task
    assert r.status_code == 201, r.text
    assert await count_rows(engine, "llm_tuples") == 2 and await count_rows(engine, "operations") == 1


async def test_llm_tuple_and_budget_bump_once_after_the_budget_action(
    logged_in_client, engine, monkeypatch
) -> None:
    await activate_profile(engine)
    generation_before = await generation(engine)
    calls: list[str] = []
    change_cap, bump = spend_ledger.change_cap, ranking.bump

    async def spy_change_cap(*args: Any, **kwargs: Any) -> None:
        calls.append("change_cap")
        await change_cap(*args, **kwargs)

    async def spy_bump(*args: Any, **kwargs: Any) -> None:
        calls.append("bump")
        await bump(*args, **kwargs)

    monkeypatch.setattr(spend_ledger, "change_cap", spy_change_cap)  # save.py calls both through the module
    monkeypatch.setattr(ranking, "bump", spy_bump)
    payload = with_generator_effort(await active_payload(logged_in_client), "high")
    payload["llm"]["cap_usd"] = "150.00"
    body = await save(logged_in_client, payload)
    assert body["version"]["effect_classes"] == ["budget", "llm_tuple"]
    assert calls == ["change_cap", "bump"]  # ranking_state is the last lock, after the month row (§9.1)
    assert await generation(engine) == generation_before + 1
    assert (await month_row(engine)).cap_usd == Decimal("150.00")


# --- concurrency and idempotency ----------------------------------------------------------------------------


async def test_two_concurrent_saves_from_one_base_give_201_and_409(
    logged_in_client, engine, migrated: PgServer, monkeypatch
) -> None:
    """Two tabs: the second waits for the first's `active_config` lock and finds a stale base, never a
    deadlock. The first is held between the route's first lock and the save's own one, where two shared
    locks would both have been granted and the upgrades would deadlock (40P01, a 500)."""
    payload = await active_payload(logged_in_client)
    first = {**payload, "display": {**payload["display"], "ui_language": "de"}}
    second = copy.deepcopy(payload)
    second["schedule"]["interval_hours"] = 12
    r1_body, r2_body = await request_for(logged_in_client, first), await request_for(logged_in_client, second)
    held, release, entered = asyncio.Event(), asyncio.Event(), list[int]()

    async def held_first(conn: Any, *args: Any, **kwargs: Any) -> Any:
        entered.append(len(entered) + 1)
        if len(entered) == 1:  # the first tab: inside its transaction, holding the route's lock only
            held.set()
            await release.wait()
        return await save_settings(conn, *args, **kwargs)  # the route's own, by name

    monkeypatch.setattr(settings_routes, "save_settings", held_first)
    one = asyncio.create_task(post(logged_in_client, r1_body, key="tab-one-concurrent-1"))
    await asyncio.wait_for(held.wait(), 10)
    two = asyncio.create_task(post(logged_in_client, r2_body, key="tab-two-concurrent-1"))
    await await_until(lambda: blocked_or_done(migrated, two), 10, 0.02)
    assert not two.done() and entered == [1]  # it waits at the route's first lock, on its own connection
    release.set()
    r1, r2 = await asyncio.gather(one, two)
    assert (r1.status_code, r2.status_code) == (201, 409), (r1.text, r2.text)
    assert r2.json()["code"] == "stale_base_version"
    assert await count_rows(engine, "settings_versions") == 2
    assert await scalar(engine, "SELECT interval_s FROM schedule_state") == 6 * 3600


async def test_two_direct_saves_on_two_connections_never_deadlock(engine: AsyncEngine) -> None:
    """`save_settings` itself on two connections, each in its own transaction: the second's `FOR UPDATE`
    waits, then its base is stale."""
    active = await active_row(engine)
    payload = load_settings_v1()

    async def one_save(ui_language: str) -> int | str:
        doc = copy.deepcopy(payload)
        doc["display"] = {"timezone": "Europe/Berlin", "ui_language": ui_language}
        request = SaveRequest(
            payload=doc,
            base_version_id=active.settings_version_id,
            acknowledged_warnings=frozenset({"llm_cap_binding"}),
        )
        try:
            async with engine.begin() as conn:
                outcome = await save_settings(conn, request, now=NOW, defer=defer_in_tx)
                await asyncio.sleep(0.2)  # hold the lock while the other save starts
                return outcome.version_no
        except Problem as problem:  # str() is its code
            return str(problem)

    results = await asyncio.gather(one_save("de"), one_save("en"))
    assert sorted(results, key=str) == [2, "stale_base_version"]


async def test_a_replay_with_the_same_key_returns_the_stored_201(logged_in_client, engine) -> None:
    payload = await active_payload(logged_in_client)
    payload["display"]["ui_language"] = "de"
    request = await request_for(logged_in_client, payload)
    first = await post(logged_in_client, request, key="replay-key-00000001")
    again = await post(logged_in_client, request, key="replay-key-00000001")
    assert first.status_code == again.status_code == 201 and first.json() == again.json()
    assert await count_rows(engine, "settings_versions") == 2
    assert set(first.json()) == {"version", "apply_scope_operation_id"}
    assert set(first.json()["version"]) == {"id", "version_no", "effect_classes"}
    # a reordered acknowledgement list is the same request
    reordered = {**request, "acknowledged_warnings": list(reversed(request["acknowledged_warnings"]))}
    assert (await post(logged_in_client, reordered, key="replay-key-00000001")).json() == first.json()
    other = {**request, "payload": {**payload, "display": {**payload["display"], "ui_language": "en"}}}
    r = await post(logged_in_client, other, key="replay-key-00000001")
    assert r.status_code == 422 and r.json()["code"] == "idempotency_key_reused"


async def test_an_internal_save_copies_the_active_acknowledgements(logged_in_client, engine) -> None:
    """Deviation 15: an internal save skips the acknowledgement check and copies the active version's."""
    payload = await active_payload(logged_in_client)
    payload["sources"]["enabled"]["serpapi"] = False
    first = await save(logged_in_client, payload)
    query = await insert_query(engine, "Data Engineer")
    payload["queries"] = [{"query_id": query, "enabled": True}]
    async with engine.begin() as conn:
        await conn.execute(text("SELECT 1 FROM active_config WHERE id = 1 FOR UPDATE"))
        outcome = await save_settings(
            conn,
            SaveRequest(
                payload=payload, base_version_id=first["version"]["id"], acknowledged_warnings=frozenset()
            ),
            now=NOW,
            defer=defer_in_tx,
            internal=True,
        )
    assert outcome.effects == frozenset({"collection"}) and outcome.version_no == 3
    stored = await version_row(engine, outcome.version_id)
    assert stored.acknowledged_warnings == ["llm_cap_binding", "serpapi_disabled_no_fallback"]
    with pytest.raises(Problem, match="validation_failed"):  # field validation still runs
        async with engine.begin() as conn:
            bad = copy.deepcopy(payload)
            bad["schedule"]["interval_hours"] = 0
            await save_settings(
                conn,
                SaveRequest(
                    payload=bad, base_version_id=outcome.version_id, acknowledged_warnings=frozenset()
                ),
                now=NOW,
                defer=defer_in_tx,
                internal=True,
            )


async def test_the_save_logs_ids_and_codes_only(logged_in_client, engine, log_events) -> None:
    payload = await active_payload(logged_in_client)
    payload["scope"]["excluded_companies"] = ["Musterfirma Unikat GmbH"]
    body = await save(logged_in_client, payload)
    (event,) = [e for e in log_events() if e["event"] == "settings_saved"]
    assert {k: event[k] for k in ("version_id", "version_no", "effects", "apply_scope_operation_id")} == {
        "version_id": body["version"]["id"],
        "version_no": 2,
        "effects": ["scope"],
        "apply_scope_operation_id": body["apply_scope_operation_id"],
    }
    rendered = str(log_events())
    assert "musterfirma" not in rendered.lower()


async def test_a_replay_logs_no_second_save(logged_in_client, log_events) -> None:
    payload = await active_payload(logged_in_client)
    payload["display"]["ui_language"] = "de"
    request = await request_for(logged_in_client, payload)
    for _ in range(2):
        assert (await post(logged_in_client, request, key="replay-log-key-0001")).status_code == 201
    events = [e["event"] for e in log_events()]
    assert events.count("settings_saved") == 1 and events.count("idempotent_replay") == 1


async def test_a_refusal_logs_counts_and_codes_not_paths(logged_in_client, engine, log_events) -> None:
    payload = await active_payload(logged_in_client)
    payload["geheimer_schluessel"] = 1  # an unknown key: its path names what the sender typed
    base = (await active_row(engine)).settings_version_id
    r = await post(
        logged_in_client, {"payload": payload, "base_version_id": base, "acknowledged_warnings": []}
    )
    assert r.status_code == 422
    (event,) = [e for e in log_events() if e["event"] == "settings_save_refused"]
    assert (event["code"], event["errors"], event["issue_codes"]) == ("validation_failed", 1, ["invalid"])
    assert "geheimer" not in str(log_events())
