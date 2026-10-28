"""The settings dry-run (spec §6.8, §13.5, §18.2 N4; plan Task 16): `POST /api/settings/dry-run` and
`settings.dryrun.dry_run` on PostgreSQL.

Every payload is the seeded document with one location and four enabled queries (synthetic: an invented
town and query ids, whose rows `query_rows` inserts), changed for the case. The projections run on fresh
windows: no quota window exists, so each is simulated in memory.
"""

import asyncio
import copy
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

import httpx
import pytest
import sqlalchemy as sa
from jobhunter.db.seed import content_hash, load_settings_v1
from jobhunter.db.sql import q
from jobhunter.llm.budget import BudgetParams, month_of
from jobhunter.llm.spend_ledger import ensure_month
from jobhunter.settings import dryrun
from jobhunter.settings.derive import derived
from jobhunter.settings.model import Settings
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.collect import insert_revisions_awaiting
from tests.support.pg import PgServer, pg_rows

pytestmark = pytest.mark.integration

URL = "/api/settings/dry-run"
NOW = datetime(2026, 10, 22, 9, 30, tzinfo=UTC)
LOCATION = {"label": "Musterstadt", "gazetteer_id": "gz-0001", "lat": 52.1, "lon": 9.9, "radius_km": 25}
LOCK_MONTH = sa.text("SELECT 1 FROM llm_budget WHERE month = :m FOR UPDATE")
LOCK_ACTIVE_CONFIG = sa.text("SELECT 1 FROM active_config WHERE id = 1 FOR UPDATE")


def base(queries: int = 4) -> dict[str, Any]:
    """The seeded settings with one location and `queries` enabled queries (ids 1…n)."""
    doc: dict[str, Any] = copy.deepcopy(load_settings_v1())
    doc["scope"]["locations"] = [LOCATION]
    doc["queries"] = [{"query_id": i, "enabled": True} for i in range(1, queries + 1)]
    return doc


def with_(doc: dict[str, Any], path: str, value: Any) -> dict[str, Any]:
    *parents, key = path.split(".")
    node = doc
    for part in parents:
        node = node[part]
    node[key] = value
    return doc


# The dry-run refuses a `queries[]` entry whose id names no row (Task 17): `base` names ids 1 to 24.
_QUERY_ROWS = sa.text(
    "INSERT INTO queries (id, text, lang, origin) OVERRIDING SYSTEM VALUE "
    "SELECT i, 'Testabfrage ' || i, 'de', 'user' FROM generate_series(1, 24) AS i"
)


# Explicit ids leave the identity sequence behind the rows; it is moved past them (never back), so a later
# identity insert cannot take one of them. Only the sequence's owner may (`setval` needs UPDATE on it).
_SEQUENCE_PAST_ROWS = (
    "SELECT setval(pg_get_serial_sequence('queries', 'id'), "
    "greatest(24, (SELECT last_value FROM queries_id_seq)))"
)


@pytest.fixture(autouse=True)
async def query_rows(engine: AsyncEngine, migrated: PgServer) -> None:
    """The query rows of `base`'s ids, with invented texts; the reset after the test removes them."""
    async with engine.begin() as conn:
        await conn.execute(_QUERY_ROWS)
    pg_rows(migrated, "migrator", _SEQUENCE_PAST_ROWS)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> datetime:
    monkeypatch.setattr("jobhunter.api.routes.settings.utcnow", lambda: NOW)
    return NOW


async def post(client: httpx.AsyncClient, payload: Any) -> dict[str, Any]:
    r = await client.post(URL, json={"payload": payload})
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    assert set(body) == {"errors", "warnings", "projections", "effects"}
    return body


def codes(body: dict[str, Any]) -> set[str]:
    return {w["code"] for w in body["warnings"]}


def warning(body: dict[str, Any], code: str) -> dict[str, Any]:
    (found,) = [w for w in body["warnings"] if w["code"] == code]
    detail: dict[str, Any] = found["detail"]
    return detail


async def test_defaults_with_one_location_and_four_queries(logged_in_client, clock) -> None:
    body = await post(logged_in_client, base())
    assert body["errors"] == []
    # 3,000 arrivals a month (no target history before M3) against about 1,617 scored under the $100 cap
    assert [(w["code"], w["detail"]) for w in body["warnings"]] == [
        ("llm_cap_binding", {"expected_scored_per_month": 1617})
    ]
    projections = body["projections"]
    assert set(projections) == {"jsearch", "adzuna", "arbeitnow", "serpapi_fallback", "llm", "det_recompute"}
    # 4 queries x (Musterstadt + remote-de) = 8 paid cells; the JSearch window is bootstrapped in memory
    jsearch = projections["jsearch"]
    assert (jsearch["cells"], jsearch["units_per_run"], jsearch["refresh_cadence_days"]) == (8, 1.453, 1.376)
    assert jsearch["window"] == {
        "used_units": 0,
        "limit_units": 200,
        "reset_at": "2026-11-22T08:37:22Z",  # 30.96 days on
    }
    assert projections["adzuna"]["calls_per_day"] == 32.0 and projections["serpapi_fallback"] is None
    assert projections["arbeitnow"] == {"page_cap": 3, "expected_pages": 1.343}
    assert projections["llm"] == {
        "bucket_rate_per_h": "0.125134",
        "bucket_capacity": "1.501613",
        "c_hat": "0.052646",
        "projected_critic_rate": "0.450",
        "targets_per_month": 1617,
        "arrival_per_month": 3000,
        "projected_month_spend": "93.089073",
        "month": "2026-10-01",
        "cap_usd": "100.00",
        "counted_usd": "0.000000",
        "headroom_usd": "93.100000",
    }
    assert projections["det_recompute"] == 0  # no LLM calls for a scope change; 0 before M3's results
    assert body["effects"] == ["collection", "scope"]


Change = Callable[[dict[str, Any]], dict[str, Any]]


def change(*steps: tuple[str, Any]) -> Change:
    def apply(doc: dict[str, Any]) -> dict[str, Any]:
        for path, value in steps:
            with_(doc, path, value)
        return doc

    return apply


# Each warning where its condition holds (beside `llm_cap_binding`, which the $100 cap always gives for
# 3,000 arrivals); a baseline without it is the $200 cap below.
WARNING_CASES: list[tuple[str, Change, set[str]]] = [
    # a weekly interval: a fair share of 40.7 units against an explicit cap of 10
    # (a weekly interval also expects 10.6 Arbeitnow pages against a cap of 7: V8's 17.5 h per page)
    (
        "paid_run_cap_binding",
        change(("schedule.interval_hours", 168), ("quota.paid_max_requests_per_run", 10)),
        {"paid_run_cap_binding", "arbeitnow_coverage_risk", "llm_cap_binding"},
    ),
    # 24 queries x 2 = 48 cells at 1.453 units per run: 8.3 days per refresh; Adzuna's 192 calls a day fit
    ("paid_refresh_slow", lambda doc: base(24), {"paid_refresh_slow", "llm_cap_binding"}),
    # 5 JSearch units a month: a refresh takes longer than JSearch's 720 h date window (and is slow)
    (
        "coverage_gap_guaranteed",
        change(("quota.jsearch_monthly_limit", 5)),
        {"paid_refresh_slow", "coverage_gap_guaranteed", "llm_cap_binding"},
    ),
    # 5 queries x 2 cells every hour: 240 Adzuna calls a day against the daily cap of 225
    (
        "adzuna_rotation",
        lambda doc: with_(base(5), "schedule.interval_hours", 1),
        {"adzuna_rotation", "llm_cap_binding"},
    ),
    ("stale_ttl_short", change(("collection.stale_ttl_days", 10)), {"stale_ttl_short", "llm_cap_binding"}),
    (
        "serpapi_disabled_no_fallback",
        change(("sources.enabled.serpapi", False)),
        {"serpapi_disabled_no_fallback", "llm_cap_binding"},
    ),
    # generator effort low while the eval gate is uncalibrated; a model-policy change also re-scores
    (
        "eval_uncalibrated",
        change(("scoring.model_policy.nodes.generate.effort", "low")),
        {"eval_uncalibrated", "llm_rescore", "llm_cap_binding"},
    ),
    ("llm_rescore", change(("scoring.model_policy.preset", "custom")), {"llm_rescore", "llm_cap_binding"}),
    ("no_cap_binding", change(("llm.cap_usd", "200.00")), set()),
    # Ruling 4: a cap at or below the overrun buffer admits nothing (A ≤ 0)
    ("cap_below_buffer", change(("llm.cap_usd", "1.00")), {"llm_cap_binding"}),
    ("cap_equal_to_buffer", change(("llm.cap_usd", "2.00")), {"llm_cap_binding"}),
]


@pytest.mark.parametrize(("name", "make", "expected"), WARNING_CASES, ids=[c[0] for c in WARNING_CASES])
async def test_each_warning_appears_exactly_when_its_condition_holds(
    logged_in_client, clock, name: str, make: Change, expected: set[str]
) -> None:
    body = await post(logged_in_client, make(base()))
    assert body["errors"] == []
    assert codes(body) == expected
    assert len(body["warnings"]) == len(expected)  # each at most once
    if name == "paid_run_cap_binding":
        assert warning(body, "paid_run_cap_binding") == {"cap": 10, "fair_share": 40.7}
    if name == "stale_ttl_short":
        assert warning(body, "stale_ttl_short") == {"label_after_days": 10}
    if name == "eval_uncalibrated":
        assert warning(body, "eval_uncalibrated") == {"node": "generate"}
    if name.startswith("cap_"):
        assert warning(body, "llm_cap_binding") == {"expected_scored_per_month": 0}
        assert body["projections"]["llm"]["targets_per_month"] == 0


async def test_arbeitnow_risk_is_forwarded(logged_in_client, clock) -> None:
    """With page 1 holding about 17.5 h of postings (V8) the weekly interval expects 10.6 pages against a cap
    of 7, and the dry-run forwards the warning; the 6 h default expects 1.3 pages and gives none."""
    assert "arbeitnow_coverage_risk" not in codes(await post(logged_in_client, base()))
    body = await post(logged_in_client, with_(base(), "schedule.interval_hours", 168))
    assert body["projections"]["arbeitnow"] == {"page_cap": 7, "expected_pages": 10.6}
    assert codes(body) == {"arbeitnow_coverage_risk", "llm_cap_binding"}


async def retained_revisions(engine: AsyncEngine, n: int) -> None:
    """`n` jobs whose one revision is current, and a merged job and one without a current revision, which do
    not count."""
    await insert_revisions_awaiting(engine, n + 2)
    async with engine.begin() as conn:
        await conn.execute(
            sa.text("UPDATE jobs j SET current_revision_id = r.id FROM job_revisions r WHERE r.job_id = j.id")
        )
        ids = [int(i) for i in (await conn.execute(sa.text("SELECT id FROM jobs ORDER BY id"))).scalars()]
        await conn.execute(
            sa.text("UPDATE jobs SET merged_into_job_id = :into WHERE id = :id"),
            {"into": ids[0], "id": ids[-1]},
        )
        await conn.execute(
            sa.text("UPDATE jobs SET current_revision_id = NULL WHERE id = :id"), {"id": ids[-2]}
        )


async def test_llm_rescore_counts_the_retained_revisions_and_uses_the_bulk_share(
    logged_in_client, engine, clock
) -> None:
    await retained_revisions(engine, 3)
    p = BudgetParams.from_payload(load_settings_v1())
    async with engine.begin() as conn:  # this month has $7.00 of scoring and $3.00 of non-scoring spend
        await ensure_month(conn, month_of(NOW), p, NOW)
        await conn.execute(
            sa.text(
                "UPDATE llm_budget SET counted_usd = 10, scoring_counted_usd = 7, nonscoring_counted_usd = 3 "
                "WHERE month = :m"
            ),
            {"m": month_of(NOW)},
        )
    tuple_change = with_(base(), "scoring.model_policy.nodes.critic.effort", "high")
    body = await post(logged_in_client, tuple_change)
    assert body["effects"] == ["collection", "llm_tuple", "scope"]
    # 3 targets x ĉ $0.052646; 0.4 x ($98.00 x 0.95 - $7.00 scoring counted) = $34.44 (§6.9.3)
    assert warning(body, "llm_rescore") == {
        "targets": 3,
        "est_usd": "0.157938",
        "bulk_budget_usd": "34.440000",
    }
    llm = body["projections"]["llm"]
    assert (llm["counted_usd"], llm["headroom_usd"]) == ("10.000000", "86.100000")  # A - 10 - (4.90 - 3)
    # the share is `scoring.bulk_rescore_max_budget_share`, never a literal 0.4: 0.5 x $86.10 = $43.05
    body = await post(logged_in_client, with_(tuple_change, "scoring.bulk_rescore_max_budget_share", "0.5"))
    assert body["effects"] == ["budget", "collection", "llm_tuple", "scope"]
    assert warning(body, "llm_rescore")["bulk_budget_usd"] == "43.050000"


async def test_no_adzuna_allowance_is_null_for_never(logged_in_client, clock) -> None:
    """A monthly cap of 1 leaves Adzuna no allowance: the cadence is JSON null ("never"), in the projection
    and in the rotation warning (8 cells x 4 runs = 32 calls a day against a daily cap of 10)."""
    doc = with_(with_(base(), "quota.adzuna_cap_month", 1), "quota.adzuna_cap_day", 10)
    body = await post(logged_in_client, doc)
    assert body["projections"]["adzuna"] == {
        "calls_per_day": 32.0,
        "allowance_per_day": 0.0,
        "cadence_days": None,
        "cells": 8,
    }
    assert warning(body, "adzuna_rotation") == {"cadence_days": None}


_STORED_POLICY: Final = q(
    "INSERT INTO evaluation_policies (version_no, policy_hash, payload, projected_critic_rate) "
    "SELECT max(version_no) + 1, :hash, :payload, :rate FROM evaluation_policies",
    jsonb=["payload"],
)


async def test_a_stored_evaluation_policy_gives_its_own_critic_rate(logged_in_client, engine, clock) -> None:
    """An evaluation policy that already has a row (an earlier version, M3's replay) is projected at its
    stored rate, not at the 0.450 a new row gets (§11.7.4)."""
    doc = with_(base(), "evaluation.t2_band", 3)
    evaluation = derived(Settings.model_validate(doc)).evaluation
    async with engine.begin() as conn:
        params = {"hash": content_hash(evaluation), "payload": evaluation, "rate": Decimal("0.520")}
        await conn.execute(_STORED_POLICY, params)
    body = await post(logged_in_client, doc)
    assert body["projections"]["critic_rate"] == "0.520"
    assert body["projections"]["llm"]["projected_critic_rate"] == "0.520"
    # another evaluation, with no row yet, still gets the default
    body = await post(logged_in_client, with_(base(), "evaluation.t2_band", 4))
    assert body["projections"]["critic_rate"] == "0.450"


async def test_effect_dependent_projections(logged_in_client, clock) -> None:
    body = await post(logged_in_client, with_(base(), "evaluation.t8_sample_pct", 0))
    assert body["projections"]["critic_rate"] == "0.450"  # no re-review of existing scores
    assert "next_slot" not in body["projections"]
    # a gated class reports the gate a save would check (M3 Task 20): below 30 golden cases, uncalibrated
    assert body["projections"]["eval_gate"] == "uncalibrated"
    body = await post(logged_in_client, with_(base(), "scoring.model_policy.nodes.critic.effort", "high"))
    assert body["projections"]["eval_gate"] == "uncalibrated"
    body = await post(logged_in_client, with_(base(), "schedule.interval_hours", 12))
    # 06:00 Berlin is 04:00 UTC on 2026-10-22: slots every 12 h, the latest at or before 09:30 is 04:00
    assert body["projections"]["next_slot"] == "2026-10-22T16:00:00Z"
    assert "critic_rate" not in body["projections"] and "eval_gate" not in body["projections"]
    body = await post(logged_in_client, with_(base(), "schedule.enabled", False))
    assert body["projections"]["next_slot"] is None


async def test_an_unchanged_normal_form_is_none(logged_in_client, clock) -> None:
    """Ruling 1: the active (seeded) document sent back with formatting differences changes nothing."""
    doc: dict[str, Any] = copy.deepcopy(load_settings_v1())
    with_(doc, "evaluation.t2_straddle_max_coverage", "0.70")
    with_(doc, "llm.cap_usd", "100")
    with_(doc, "scope.work_modes", ["onsite", "hybrid", "remote"])
    del doc["notifications"]
    body = await post(logged_in_client, doc)
    assert body["effects"] == ["none"]
    assert set(body["projections"]) == {"jsearch", "adzuna", "arbeitnow", "serpapi_fallback", "llm"}


async def test_invalid_input_returns_the_errors_with_their_paths(logged_in_client, clock) -> None:
    doc = with_(base(), "schedule.interval_hours", 0)
    doc["scope"]["colour"] = "blue"
    body = await post(logged_in_client, doc)
    assert [(e["field"], e["code"]) for e in body["errors"]] == [
        ("payload.schedule.interval_hours", "out_of_range"),
        ("payload.scope.colour", "invalid"),
    ]
    assert all(e["message"] for e in body["errors"])
    assert (body["warnings"], body["projections"], body["effects"]) == ([], {}, [])
    # a cross-field rule: no location and no remote work
    doc = with_(with_(base(), "scope.locations", []), "scope.remote_within_germany", False)
    body = await post(logged_in_client, doc)
    assert [(e["field"], e["code"]) for e in body["errors"]] == [("payload.scope.locations", "conflict")]
    body = await post(logged_in_client, "not a document")
    assert [(e["field"], e["code"]) for e in body["errors"]] == [("payload", "invalid")]


async def test_the_route_needs_a_session_and_a_body(api_client, logged_in_client) -> None:
    no_session = await api_client.post(URL, json={"payload": base()}, headers={"X-JH-Request": "1"})
    assert no_session.status_code == 401
    r = await logged_in_client.post(URL, json={})
    assert r.status_code == 422 and r.json()["code"] == "validation_failed"


async def test_the_dry_run_takes_no_row_lock_on_the_month_or_the_settings(
    logged_in_client, engine, clock
) -> None:
    """`read_month` is a plain `SELECT`: a reservation (or a settings save) holding the rows does not hold the
    preview back."""
    async with engine.begin() as conn:
        await ensure_month(conn, month_of(NOW), BudgetParams.from_payload(load_settings_v1()), NOW)
    async with engine.connect() as blocker:
        await blocker.begin()
        await blocker.execute(LOCK_MONTH, {"m": month_of(NOW)})
        await blocker.execute(LOCK_ACTIVE_CONFIG)
        body = await asyncio.wait_for(post(logged_in_client, base()), 10)
        await blocker.rollback()
    assert body["projections"]["llm"]["month"] == "2026-10-01"


async def test_the_route_runs_the_dry_run_in_a_read_only_transaction(
    logged_in_client, clock, monkeypatch
) -> None:
    seen: list[str] = []

    async def probe(conn, payload, *, active, now):
        seen.append(
            (await conn.execute(sa.text("SELECT current_setting('transaction_read_only')"))).scalar_one()
        )
        return dryrun.DryRunResult(errors=[], warnings=[], projections={}, effects=frozenset({"none"}))

    monkeypatch.setattr("jobhunter.api.routes.settings.dry_run", probe)
    await post(logged_in_client, base())
    assert seen == ["on"]


def table_counts(server: PgServer) -> dict[str, int]:
    """The row count of every table of the `public` and `procrastinate` schemas."""
    tables = pg_rows(
        server,
        "superuser",
        "SELECT schemaname, tablename FROM pg_tables WHERE schemaname IN ('public', 'procrastinate') "
        "ORDER BY 1, 2",
    )
    counts: dict[str, int] = {}
    for schema, table in tables:
        row = pg_rows(server, "superuser", f'SELECT count(*) FROM "{schema}"."{table}"')  # noqa: S608
        counts[f"{schema}.{table}"] = int(str(row[0][0]))
    return counts


async def test_dry_run_writes_nothing(logged_in_client, engine, migrated: PgServer, clock) -> None:
    """§18.2 N4: no window exists, so every one is simulated in memory; no row of any table is written, by a
    change of every class at once."""
    await retained_revisions(engine, 2)
    before = table_counts(migrated)
    assert before["public.quota_windows"] == 0 and before["public.llm_budget"] == 0
    doc = base()
    for path, value in [
        ("schedule.interval_hours", 12),
        ("scope.max_ad_age_days", 14),
        ("evaluation.t2_band", 3),
        ("scoring.model_policy.preset", "custom"),
        ("scoring.execution.critic.deadline_s", 900),
        ("llm.cap_usd", "150.00"),
        ("display.ui_language", "de"),
    ]:
        with_(doc, path, value)
    body = await post(logged_in_client, doc)
    assert body["effects"] == [
        "admission",
        "budget",
        "collection",
        "evaluation_policy",
        "execution",
        "llm_tuple",
        "schedule",
        "scope",
    ]
    assert {"llm_rescore", "llm_cap_binding"} <= codes(body)
    after = table_counts(migrated)
    assert after == before
    assert after["public.quota_windows"] == 0


async def test_dry_run_runs_inside_a_read_only_transaction(engine) -> None:
    """The function itself, on a connection whose transaction refuses any write (SQLSTATE 25006)."""
    async with engine.connect() as conn:
        await conn.execute(sa.text("SET TRANSACTION READ ONLY"))
        active = await dryrun.active_settings(conn)
        assert active == Settings.model_validate(load_settings_v1())
        doc = with_(with_(base(), "scoring.model_policy.preset", "A"), "schedule.anchor_local", "07:00")
        result = await dryrun.dry_run(conn, doc, active=active, now=NOW)
        await conn.rollback()
    assert result.errors == [] and result.effects == {"collection", "llm_tuple", "schedule", "scope"}
    assert {w.code for w in result.warnings} == {"llm_rescore", "llm_cap_binding"}
    assert result.warnings[-1].detail == {"targets": 0, "est_usd": "0.000000", "bulk_budget_usd": "37.240000"}
    assert Decimal(str(result.projections["det_recompute"])) == 0
