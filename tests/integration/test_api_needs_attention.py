"""`GET /api/system/needs-attention` (spec §14.2, §15.7; plan M4 Task 12, D23, D26, D42, U3) and the replay
route `POST /api/system/raw-items/replay`.

One read snapshot; each list section is its full count and at most 50 items, in a stable order; the score
lane's acknowledgeable pause and a failed scope apply are an object or null. One test per section seeds its
rows (and the rows it must leave out); `test_each_section_is_bounded_to_50_with_its_count` seeds 51 of every
list section but `retrieve_failing`, which at most one submission can fill (`batch_one_in_flight`: one
`submitted` submission at a time). The route's clock is injected (`jobhunter.api.routes.system.utcnow`).
Every datum is invented, or a scrubbed fixture.
"""

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Final

import httpx
import pytest
import sqlalchemy as sa
from jobhunter.availability.snapshots import apply_snapshot
from jobhunter.clock import utcnow
from jobhunter.db.transitions import transition
from jobhunter.llm.gateway import AUTH_FAILED_ALERT
from jobhunter.llm.spend_ledger import BOUND_VIOLATED_ALERT
from jobhunter.observability.alerts import acknowledge_alert, raise_alert
from jobhunter.observability.attention import ITEMS_PER_SECTION
from jobhunter.scoring.batch_results import RETRIEVE_FAILING_ALERT
from jobhunter.settings.scope_apply import APPLY_FAILED_ALERT
from jobhunter.sources import replay
from jobhunter.sources.arbeitnow import ArbeitnowAdapter
from jobhunter.sources.items import parse_response
from jobhunter.sources.receipts import RAW_BODY_TTL
from jobhunter.worker.runs import RunOwner, finalize_run
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.support.collect import Board, pending_item, queued_run
from tests.support.jobs_list import Card, activate_tuple, add_posting, seed_cards
from tests.support.m3 import insert_id
from tests.support.sources import StoredPage

pytestmark = pytest.mark.integration

URL: Final = "/api/system/needs-attention"
REPLAY: Final = "/api/system/raw-items/replay"
SYSTEM_UTCNOW: Final = "jobhunter.api.routes.system.utcnow"
LIST_SECTIONS: Final = (
    "raw_items",
    "identity_conflicts",
    "failed_targets",
    "quarantined_batches",
    "retrieve_failing",
    "mass_disappearances",
    "unscorable_groups",
    "broken_sources",
    "failed_purges",
)
EMPTY: Final = {"count": 0, "items": []}
PENDING_OF: Final = sa.text(
    "SELECT id FROM raw_items WHERE raw_response_id = :r AND disposition = 'pending' ORDER BY id"
)
RESPONSE_OF: Final = sa.text("SELECT raw_response_id FROM raw_items WHERE id = :i")
TARGET_OF: Final = sa.text("SELECT id FROM score_targets WHERE job_id = :j")


@pytest.fixture
def now(monkeypatch: pytest.MonkeyPatch) -> datetime:
    """The route's clock, fixed for the test."""
    moment = utcnow()
    monkeypatch.setattr(SYSTEM_UTCNOW, lambda: moment)
    return moment


def at(text: str | None) -> datetime | None:
    return None if text is None else datetime.fromisoformat(text)


async def attention(client: httpx.AsyncClient) -> dict[str, Any]:
    r = await client.get(URL)
    assert r.status_code == 200, (r.status_code, r.text)
    body: dict[str, Any] = r.json()
    return body


async def execute(engine: AsyncEngine, statement: str, **params: Any) -> Any:
    async with engine.begin() as c:
        return await c.execute(sa.text(statement), params)


async def scalar(engine: AsyncEngine, statement: str, **params: Any) -> Any:
    async with engine.connect() as c:
        return (await c.execute(sa.text(statement), params)).scalar_one()


async def reject(engine: AsyncEngine, ids: Sequence[int], reason: str = "item_contract") -> None:
    async with engine.begin() as c:
        for raw_item_id in ids:
            await transition(
                c, "raw_items", raw_item_id, ["pending"], "rejected", reason=reason, set_={"normalized": None}
            )


async def rejected_alone(engine: AsyncEngine, owner: RunOwner, *, fetched_at: datetime | None = None) -> int:
    """A stored Arbeitnow response of `owner`'s run whose one item is rejected (`item_contract`); its id."""
    raw_item_id = await pending_item(engine, owner, "arbeitnow", {}, fetched_at=fetched_at)
    await reject(engine, [raw_item_id])
    return int(await scalar(engine, "SELECT raw_response_id FROM raw_items WHERE id = :i", i=raw_item_id))


async def parsed_arbeitnow(engine: AsyncEngine, adapter: ArbeitnowAdapter, page: StoredPage) -> list[int]:
    owner, rr = page
    async with engine.begin() as c:
        await parse_response(c, rr, adapter=adapter, owner=owner)
    async with engine.connect() as c:
        return [int(i) for i in (await c.execute(PENDING_OF, {"r": rr})).scalars()]


async def ended(engine: AsyncEngine, owner: RunOwner) -> None:
    """The run over (`succeeded`): no node of it resolves its items any more, so they can be replayed."""
    async with engine.begin() as c:
        await finalize_run(c, owner, status="succeeded", reason=None, report={})


async def alert(c: AsyncConnection, code: str, subject: str, **context: Any) -> int:
    return await raise_alert(
        c, code=code, severity="critical", subject=subject, message="test", context=context
    )


# --- the sections -----------------------------------------------------------------------------------------


async def test_an_empty_install_has_empty_sections(
    logged_in_client: httpx.AsyncClient, now: datetime
) -> None:
    body = await attention(logged_in_client)
    assert body == {**dict.fromkeys(LIST_SECTIONS, EMPTY), "paused_lane": None, "scope_apply_failed": None}


async def test_raw_items_are_grouped_by_response_with_their_reasons_and_replayability(
    logged_in_client: httpx.AsyncClient,
    engine: AsyncEngine,
    arbeitnow_adapter: ArbeitnowAdapter,
    stored_arbeitnow_page: StoredPage,
    now: datetime,
) -> None:
    owner, rr = stored_arbeitnow_page
    pending = await parsed_arbeitnow(engine, arbeitnow_adapter, stored_arbeitnow_page)
    await reject(engine, pending[:2], "item_contract")
    await reject(engine, pending[2:3], "identity_error")
    expired = await rejected_alone(engine, owner, fetched_at=now - RAW_BODY_TTL - timedelta(hours=1))
    purged = await rejected_alone(engine, owner, fetched_at=now - timedelta(days=2))
    await execute(
        engine, "UPDATE raw_responses SET body_zstd = NULL, purged_at = now() WHERE id = :r", r=purged
    )
    fetched = {
        r: await scalar(engine, "SELECT fetched_at FROM raw_responses WHERE id = :r", r=r)
        for r in (rr, purged)
    }
    await ended(engine, owner)

    section = (await attention(logged_in_client))["raw_items"]
    assert section["count"] == 3
    # newest fetch first; the pending items of the page are not listed
    assert [
        (i["raw_response_id"], i["rejected"], i["reasons"], i["replayable"]) for i in section["items"]
    ] == [
        (rr, 3, ["identity_error", "item_contract"], True),
        (purged, 1, ["item_contract"], False),  # its body is purged
        (expired, 1, ["item_contract"], False),  # fetched more than 30 days ago
    ]
    first = section["items"][0]
    assert (first["source_id"], first["instance_label"], at(first["fetched_at"])) == (
        "arbeitnow",
        "default",  # the default instance, which no watchlist company names: labelled by its instance key
        fetched[rr],
    )


async def test_a_running_run_s_items_are_not_replayable(
    logged_in_client: httpx.AsyncClient,
    engine: AsyncEngine,
    arbeitnow_adapter: ArbeitnowAdapter,
    stored_arbeitnow_page: StoredPage,
    now: datetime,
) -> None:
    owner, rr = stored_arbeitnow_page
    pending = await parsed_arbeitnow(engine, arbeitnow_adapter, stored_arbeitnow_page)
    await reject(engine, pending[:2])
    # the run still runs: its resolve node or its finalize may still count them (I1), so no replay
    [item] = (await attention(logged_in_client))["raw_items"]["items"]
    assert (item["raw_response_id"], item["replayable"]) == (rr, False)
    for body in ({"raw_response_id": rr}, {"raw_item_ids": pending[:2]}):
        r = await logged_in_client.post(REPLAY, json=body)
        assert (r.status_code, r.json()["code"]) == (409, "conflict_state"), body
    assert await scalar(engine, "SELECT count(*) FROM operations") == 0
    # the run over: both replayable
    await ended(engine, owner)
    [item] = (await attention(logged_in_client))["raw_items"]["items"]
    assert (item["raw_response_id"], item["replayable"]) == (rr, True)
    r = await logged_in_client.post(REPLAY, json={"raw_response_id": rr})
    assert (r.status_code, r.json()["items"]) == (202, 2)


async def test_a_body_exactly_30_days_old_is_still_replayable(
    logged_in_client: httpx.AsyncClient,
    engine: AsyncEngine,
    arbeitnow_adapter: ArbeitnowAdapter,
    stored_arbeitnow_page: StoredPage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, rr = stored_arbeitnow_page
    pending = await parsed_arbeitnow(engine, arbeitnow_adapter, stored_arbeitnow_page)
    await reject(engine, pending[:1])
    await ended(engine, owner)
    fetched_at = await scalar(engine, "SELECT fetched_at FROM raw_responses WHERE id = :r", r=rr)
    edge = fetched_at + RAW_BODY_TTL  # retention keeps the body until then, and `replay_rejected` takes it
    assert RAW_BODY_TTL == timedelta(days=30)

    monkeypatch.setattr(SYSTEM_UTCNOW, lambda: edge)
    assert [i["replayable"] for i in (await attention(logged_in_client))["raw_items"]["items"]] == [True]
    r = await logged_in_client.post(REPLAY, json={"raw_response_id": rr})
    assert (r.status_code, r.json()["items"]) == (202, 1)
    # a microsecond later it is past the TTL
    monkeypatch.setattr(SYSTEM_UTCNOW, lambda: edge + timedelta(microseconds=1))
    assert [i["replayable"] for i in (await attention(logged_in_client))["raw_items"]["items"]] == [False]
    r = await logged_in_client.post(REPLAY, json={"raw_response_id": rr})
    assert (r.status_code, r.json()["code"]) == (409, "conflict_state")


async def test_identity_conflicts_list_the_open_ones_with_their_jobs_and_namespaces(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, now: datetime
) -> None:
    a, b, c = await seed_cards(engine, [Card(), Card(), Card()])
    posting = await add_posting(engine, a, "jsearch")
    evidence = {
        "raw_item_id": 1,
        "conflicting_keys": [
            {"key_ns": "pub:stepstone.de", "observation": ["1"], "jobs": []},
            {"key_ns": "ats:lever:eu:acme", "observation": ["x"], "jobs": []},
        ],
    }
    async with engine.begin() as conn:
        older = await insert_id(
            conn,
            "identity_conflicts",
            {
                "posting_id": posting,
                "posting_job_id": a,
                "candidate_job_ids": [b],
                "evidence": {},
                "status": "open",
            },
        )
        newer = await insert_id(
            conn,
            "identity_conflicts",
            {
                "posting_id": posting,
                "posting_job_id": a,
                "candidate_job_ids": [c, b],
                "evidence": evidence,
                "status": "open",
            },
        )
        await insert_id(  # resolved: not listed
            conn,
            "identity_conflicts",
            {
                "posting_id": posting,
                "posting_job_id": a,
                "candidate_job_ids": [c],
                "evidence": {},
                "status": "resolved_separate",
                "resolved_at": now,
            },
        )
    section = (await attention(logged_in_client))["identity_conflicts"]
    assert section["count"] == 2
    assert [(i["conflict_id"], i["job_ids"], i["reason"]) for i in section["items"]] == [
        (newer, [a, c, b], "ats:lever:eu:acme, pub:stepstone.de"),  # same opening time: the newer id first
        (older, [a, b], "exclusive_conflict"),
    ]
    opened = await scalar(engine, "SELECT created_at FROM identity_conflicts WHERE id = :i", i=newer)
    assert at(section["items"][0]["opened_at"]) == opened


async def test_failed_targets_are_the_active_tuple_s_refused_and_failed_ones(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, now: datetime
) -> None:
    await activate_tuple(engine, 1)
    (earlier,) = await seed_cards(engine, [Card(state="failed_request")])
    await activate_tuple(engine, 2)  # the first tuple's failed target is no longer the active tuple's
    refused, failed, _, _ = await seed_cards(
        engine,
        [
            Card(state="refused", title="Abgelehnte Rolle"),
            Card(state="failed_attempts_exhausted"),
            Card(state="pending"),
            Card(state="expired_unscored"),
        ],
    )
    targets = {
        job: await scalar(engine, "SELECT id FROM score_targets WHERE job_id = :j", j=job)
        for job in (refused, failed)
    }
    earlier_target = await scalar(engine, "SELECT state FROM score_targets WHERE job_id = :j", j=earlier)
    assert earlier_target == "failed_request"  # still failed, under a tuple no longer active: not listed
    section = (await attention(logged_in_client))["failed_targets"]
    assert section == {
        "count": 2,
        "items": [  # changed in the same transaction: the higher id first
            {
                "target_id": targets[failed],
                "job_id": failed,
                "title": "Testrolle",
                "state": "failed_attempts_exhausted",
                "state_reason": "test",
                "refusal_category": None,
            },
            {
                "target_id": targets[refused],
                "job_id": refused,
                "title": "Abgelehnte Rolle",
                "state": "refused",
                "state_reason": None,
                "refusal_category": "test",
            },
        ],
    }


async def bulk_operation(c: AsyncConnection, llm_tuple_id: int) -> int:
    return await insert_id(
        c,
        "bulk_operations",
        {
            "kind": "tuple_rescore",
            "llm_tuple_id": llm_tuple_id,
            "state": "completed",
            "budget_usd": Decimal(1),
            "finished_at": utcnow(),
        },
    )


def submission(llm_tuple_id: int, bulk: int, state: str, started: datetime, **columns: Any) -> dict[str, Any]:
    return {
        "state": state,
        "llm_tuple_id": llm_tuple_id,
        "bulk_operation_id": bulk,
        "request_count": 3,
        "request_hash": b"\x01",
        "reserved_usd": Decimal("0.123456"),
        "followup_held_usd": Decimal(0),
        "submit_started_at": started,
        **columns,
    }


async def test_quarantined_batches_are_the_ambiguous_and_the_listed_released_ones(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, now: datetime
) -> None:
    llm_tuple = await activate_tuple(engine)
    async with engine.begin() as c:
        bulk = await bulk_operation(c, llm_tuple)
        ambiguous = await insert_id(
            c,
            "batch_submissions",
            submission(
                llm_tuple,
                bulk,
                "submit_ambiguous",
                now - timedelta(hours=2),
                last_reconcile_at=now - timedelta(minutes=10),
                reconcile_report={"candidates": 2, "listed": 5, "unread_candidates": 2},
            ),
        )
        released = await insert_id(
            c,
            "batch_submissions",
            submission(
                llm_tuple, bulk, "released", now - timedelta(days=5), released_at=now - timedelta(days=4)
            ),
        )
        await insert_id(  # released more than 29 days after its send: listed no more
            c,
            "batch_submissions",
            submission(
                llm_tuple, bulk, "released", now - timedelta(days=30), released_at=now - timedelta(days=29)
            ),
        )
    section = (await attention(logged_in_client))["quarantined_batches"]
    assert section["count"] == 2
    assert [
        (i["submission_id"], i["state"], i["reserved_usd"], i["request_count"], i["unread_candidates"])
        for i in section["items"]
    ] == [(ambiguous, "submit_ambiguous", "0.123456", 3, 2), (released, "released", "0.123456", 3, 0)]
    first, second = section["items"]
    assert (at(first["submit_started_at"]), at(first["last_reconcile_at"])) == (
        now - timedelta(hours=2),
        now - timedelta(minutes=10),
    )
    assert second["last_reconcile_at"] is None


async def test_retrieve_failing_items_carry_reserved_usd_and_releasable(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, now: datetime
) -> None:
    llm_tuple = await activate_tuple(engine)
    async with engine.begin() as c:
        bulk = await bulk_operation(c, llm_tuple)
        submitted = await insert_id(
            c,
            "batch_submissions",
            submission(
                llm_tuple,
                bulk,
                "submitted",
                now - timedelta(days=2),
                provider_batch_id="msgbatch_test_1",
                submitted_at=now - timedelta(days=2),
                expires_at=now - timedelta(hours=1, seconds=1),
                reserved_usd=Decimal("0.500000"),
            ),
        )
    # no open alert yet: a submitted batch whose retrieval works is not listed
    assert (await attention(logged_in_client))["retrieve_failing"] == EMPTY
    async with engine.begin() as c:
        alert_id = await alert(c, RETRIEVE_FAILING_ALERT, str(submitted), submission_id=submitted)
    item = {
        "submission_id": submitted,
        "provider_batch_id": "msgbatch_test_1",
        "alert_id": alert_id,
        "reserved_usd": "0.500000",  # what a release leaves counted as ambiguous
        "releasable": True,  # more than 1 h past its expiry
    }
    section = (await attention(logged_in_client))["retrieve_failing"]
    assert section["count"] == 1
    assert {k: v for k, v in section["items"][0].items() if k != "expires_at"} == item
    assert at(section["items"][0]["expires_at"]) == now - timedelta(hours=1, seconds=1)
    # at most 1 h past it: not releasable yet
    await execute(
        engine,
        "UPDATE batch_submissions SET expires_at = :e WHERE id = :s",
        e=now - timedelta(minutes=59),
        s=submitted,
    )
    assert (await attention(logged_in_client))["retrieve_failing"]["items"][0]["releasable"] is False
    await execute(engine, "UPDATE alerts SET resolved_at = now() WHERE id = :a", a=alert_id)
    assert (await attention(logged_in_client))["retrieve_failing"] == EMPTY


async def opened_event(engine: AsyncEngine, board: Board, taken_at: datetime) -> int:
    """A complete snapshot of the board without a member, applied: it opens a mass disappearance."""
    async with engine.begin() as c:
        snapshot_id = int(
            (
                await c.execute(
                    sa.text(
                        "INSERT INTO ats_snapshots (source_instance_id, status, member_count, taken_at) "
                        "VALUES (:i, 'complete', 0, :t) RETURNING id"
                    ),
                    {"i": board.instance_id, "t": taken_at},
                )
            ).scalar_one()
        )

    async def no_guard(conn: AsyncConnection) -> None:
        return None

    async with engine.begin() as c:
        effect = await apply_snapshot(c, snapshot_id, now=utcnow(), guard=no_guard)
    assert effect.mass_opened
    return snapshot_id


async def test_mass_disappearances_are_the_open_events_by_their_opening_snapshot(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, board20: Board, now: datetime
) -> None:
    taken = now - timedelta(hours=3)
    opening = await opened_event(engine, board20, taken)
    section = (await attention(logged_in_client))["mass_disappearances"]
    assert section["count"] == 1
    [item] = section["items"]
    assert {k: v for k, v in item.items() if k != "taken_at"} == {
        "snapshot_id": opening,
        "instance_label": "acme-board",  # the watchlist company's display name
        "absent_count": 20,
        "baseline_active_count": 20,
    }
    assert at(item["taken_at"]) == taken
    r = await logged_in_client.post(f"/api/ats-snapshots/{opening}/acknowledge")  # the event ends
    assert (r.status_code, r.json()) == (200, {"snapshot_id": opening, "closed": 20})
    assert (await attention(logged_in_client))["mass_disappearances"] == EMPTY


async def test_unscorable_groups_list_their_members_with_their_targets(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, now: datetime
) -> None:
    await activate_tuple(engine)
    a, b, c = await seed_cards(engine, [Card(state="failed_request"), Card(state="refused"), Card()])
    async with engine.begin() as conn:
        group = await insert_id(
            conn,
            "duplicate_groups",
            {"status": "active", "unscorable_reason": "all_failed", "election_reason": "rep_ineligible"},
        )
        await insert_id(  # one with a representative: not listed
            conn,
            "duplicate_groups",
            {"status": "active", "representative_job_id": c, "election_reason": "initial"},
        )
        for job in (b, a):
            await conn.execute(
                sa.text("UPDATE jobs SET duplicate_group_id = :g WHERE id = :j"), {"g": group, "j": job}
            )
    targets = {
        job: await scalar(engine, "SELECT id FROM score_targets WHERE job_id = :j", j=job) for job in (a, b)
    }
    assert (await attention(logged_in_client))["unscorable_groups"] == {
        "count": 1,
        "items": [
            {
                "group_id": group,
                "unscorable_reason": "all_failed",
                "members": [  # by job id
                    {
                        "job_id": a,
                        "title": "Testrolle",
                        "target_id": targets[a],
                        "target_state": "failed_request",
                    },
                    {"job_id": b, "title": "Testrolle", "target_id": targets[b], "target_state": "refused"},
                ],
            }
        ],
    }


async def test_broken_sources_are_the_four_partial_causes_and_degraded_as_a_warning(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, board: Board, now: datetime
) -> None:
    opened = now - timedelta(minutes=30)

    async def broken(health: str, opened_at: datetime, code: str, where: str, **params: Any) -> None:
        await execute(
            engine,
            f"UPDATE source_instances SET health = :health, breaker_opened_at = :o, half_open_after = :h, "  # noqa: S608
            f"last_error_code = :code WHERE {where}",
            health=health,
            o=opened_at,
            h=now + timedelta(hours=1),
            code=code,
            **params,
        )

    default = "source_id = :s AND instance_key = 'default'"
    await broken("breaker_open", opened, "http_503", default, s="jsearch")
    await broken("not_found", opened - timedelta(hours=1), "http_404", "id = :i", i=board.instance_id)
    await broken("auth_failed", opened - timedelta(hours=2), "http_401", default, s="arbeitnow")
    lever = int(
        (
            await execute(
                engine,
                "INSERT INTO source_instances (source_id, instance_key, health, breaker_opened_at, "
                "half_open_after, last_error_code) "
                "VALUES ('lever', 'global:acme', 'contract_broken', :o, :h, 'item_array') RETURNING id",
                o=opened - timedelta(hours=3),
                h=now + timedelta(hours=1),
            )
        ).scalar_one()
    )
    await execute(
        engine,
        "UPDATE source_instances SET health = 'degraded', last_error_code = 'timeout' "
        "WHERE source_id = 'adzuna' AND instance_key = 'default'",
    )
    await execute(  # not collected on purpose: no attention needed
        engine,
        "UPDATE source_instances SET health = 'disabled', last_error_code = 'no_credential' "
        "WHERE source_id = 'serpapi' AND instance_key = 'default'",
    )
    ids = {
        s: await scalar(
            engine, "SELECT id FROM source_instances WHERE source_id = :s AND instance_key = 'default'", s=s
        )
        for s in ("jsearch", "adzuna", "arbeitnow")
    }
    section = (await attention(logged_in_client))["broken_sources"]
    assert section["count"] == 5  # not the disabled instance, not the healthy ones
    # the broken ones first, the newest opened breaker first; then the degraded one
    assert [
        (i["instance_id"], i["source_id"], i["label"], i["health"], i["severity"]) for i in section["items"]
    ] == [
        (ids["jsearch"], "jsearch", "default", "breaker_open", "critical"),
        (board.instance_id, "greenhouse", "acme-board", "not_found", "critical"),
        (ids["arbeitnow"], "arbeitnow", "default", "auth_failed", "critical"),
        (lever, "lever", "global:acme", "contract_broken", "critical"),
        (ids["adzuna"], "adzuna", "default", "degraded", "warn"),
    ]
    first, *_, last = section["items"]
    assert (first["last_error_code"], at(first["breaker_opened_at"]), at(first["half_open_after"])) == (
        "http_503",
        opened,
        now + timedelta(hours=1),
    )
    assert (last["last_error_code"], last["breaker_opened_at"], last["half_open_after"]) == (
        "timeout",
        None,
        None,
    )


async def test_the_paused_lane_is_its_unacknowledged_alert(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, now: datetime
) -> None:
    async with engine.begin() as c:
        auth = await alert(c, AUTH_FAILED_ALERT, "llm")
    assert (await attention(logged_in_client))["paused_lane"] == {"reason": "auth_failed", "alert_id": auth}
    async with engine.begin() as c:
        bound = await alert(c, BOUND_VIOLATED_ALERT, "month:2026-11")
    # the bound violation first, as the pause guard orders them
    assert (await attention(logged_in_client))["paused_lane"] == {
        "reason": "reservation_bound_violated",
        "alert_id": bound,
    }
    async with engine.begin() as c:
        await acknowledge_alert(c, bound, now=now)
    assert (await attention(logged_in_client))["paused_lane"] == {"reason": "auth_failed", "alert_id": auth}
    async with engine.begin() as c:
        await acknowledge_alert(c, auth, now=now)
    assert (await attention(logged_in_client))["paused_lane"] is None


async def test_a_failed_scope_apply_is_its_scope_and_alert(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, now: datetime
) -> None:
    active = int(await scalar(engine, "SELECT scope_version_id FROM active_config WHERE id = 1"))
    inserted = await execute(
        engine,
        "INSERT INTO scope_versions (scope_hash, payload) SELECT sha256(convert_to('task-12', 'UTF8')), "
        "payload FROM scope_versions WHERE id = :s RETURNING id",
        s=active,
    )
    pending = int(inserted.scalar_one())
    key = f"apply_scope:{pending}"
    # a pending scope whose apply failed: the reconciler has not raised the alert yet
    await execute(engine, "UPDATE active_config SET pending_scope_version_id = :p WHERE id = 1", p=pending)
    await execute(
        engine,
        "INSERT INTO operations (kind, payload, dedup_key, status, last_error_code, finished_at) "
        "VALUES ('apply_scope', CAST(:payload AS jsonb), :k, 'failed', 'attempts_exhausted', now())",
        payload=f'{{"scope_version_id": {pending}}}',
        k=key,
    )
    assert (await attention(logged_in_client))["scope_apply_failed"] == {
        "version_id": pending,
        "alert_id": None,
    }
    # a live apply of it: under way, nothing to retry
    await execute(
        engine,
        "INSERT INTO operations (kind, payload, dedup_key, status) "
        "VALUES ('apply_scope', CAST(:payload AS jsonb), :k, 'pending')",
        payload=f'{{"scope_version_id": {pending}}}',
        k=key,
    )
    assert (await attention(logged_in_client))["scope_apply_failed"] is None
    # the reconciler's end: the pointer cleared, the alert raised
    await execute(
        engine, "UPDATE operations SET status = 'cancelled', finished_at = now() WHERE status = 'pending'"
    )
    await execute(engine, "UPDATE active_config SET pending_scope_version_id = NULL WHERE id = 1")
    async with engine.begin() as c:
        alert_id = await alert(
            c, APPLY_FAILED_ALERT, f"scope:{pending}", scope_version_id=pending, operation_id=1
        )
    assert (await attention(logged_in_client))["scope_apply_failed"] == {
        "version_id": pending,
        "alert_id": alert_id,
    }
    await execute(engine, "UPDATE alerts SET resolved_at = now() WHERE id = :a", a=alert_id)
    assert (await attention(logged_in_client))["scope_apply_failed"] is None


# --- the bound -------------------------------------------------------------------------------------------

N: Final = ITEMS_PER_SECTION + 1


async def test_each_section_is_bounded_to_50_with_its_count(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, current_epoch: int, now: datetime
) -> None:
    owner = await queued_run(engine)
    responses = [await rejected_alone(engine, owner, fetched_at=now - timedelta(minutes=n)) for n in range(N)]
    llm_tuple = await activate_tuple(engine)
    jobs = await seed_cards(engine, [Card(state="failed_request") for _ in range(N)])
    posting = await add_posting(engine, jobs[0], "jsearch")
    async with engine.begin() as c:
        bulk = await bulk_operation(c, llm_tuple)
        for n in range(N):
            await insert_id(
                c,
                "identity_conflicts",
                {
                    "posting_id": posting,
                    "posting_job_id": jobs[0],
                    "candidate_job_ids": [jobs[1]],
                    "evidence": {},
                    "status": "open",
                },
            )
            await insert_id(
                c,
                "batch_submissions",
                submission(
                    llm_tuple,
                    bulk,
                    "released",
                    now - timedelta(hours=n + 1),
                    released_at=now - timedelta(hours=n),
                ),
            )
            await insert_id(
                c,
                "duplicate_groups",
                {
                    "status": "active",
                    "unscorable_reason": "no_active_member",
                    "election_reason": "member_left",
                },
            )
        # boards in a mass disappearance, each degraded
        await c.execute(
            sa.text(
                "INSERT INTO source_instances (source_id, instance_key, health, last_error_code) "
                "SELECT 'greenhouse', 'board-' || n, 'degraded', 'timeout' FROM generate_series(1, :n) n"
            ),
            {"n": N},
        )
        await c.execute(
            sa.text(
                "INSERT INTO ats_snapshots (source_instance_id, status, member_count, baseline_active_count, "
                "absent_count, taken_at, applied_at) SELECT id, 'mass_disappearance', 0, 10, 10, :t, :t "
                "FROM source_instances WHERE instance_key LIKE 'board-%'"
            ),
            {"t": now - timedelta(hours=1)},
        )
        await c.execute(
            sa.text(
                "UPDATE source_instances i SET mass_event_snapshot_id = n.id FROM ats_snapshots n "
                "WHERE n.source_instance_id = i.id AND i.instance_key LIKE 'board-%'"
            )
        )
        # deletions in progress without a purge operation (failed purges, M5 Task 18)
        await c.execute(
            sa.text(
                "INSERT INTO deletion_manifests (manifest_uuid, kind, status) "
                "SELECT gen_random_uuid(), 'job_tombstone', 'executing' FROM generate_series(1, :n)"
            ),
            {"n": N},
        )
    body = await attention(logged_in_client)
    for name in LIST_SECTIONS:
        section = body[name]
        if name == "retrieve_failing":  # one submitted submission at a time: nothing seeded
            assert section == EMPTY
            continue
        assert (section["count"], len(section["items"])) == (N, ITEMS_PER_SECTION), name
    # the newest first: the response fetched last, and not the one fetched first
    listed = [i["raw_response_id"] for i in body["raw_items"]["items"]]
    assert listed == responses[:ITEMS_PER_SECTION]
    assert len({i["conflict_id"] for i in body["identity_conflicts"]["items"]}) == ITEMS_PER_SECTION


# --- the replay route -------------------------------------------------------------------------------------


async def test_replay_route_expands_a_response_id_and_refuses_both_or_neither(
    logged_in_client: httpx.AsyncClient,
    engine: AsyncEngine,
    arbeitnow_adapter: ArbeitnowAdapter,
    stored_arbeitnow_page: StoredPage,
    now: datetime,
) -> None:
    owner, rr = stored_arbeitnow_page
    pending = await parsed_arbeitnow(engine, arbeitnow_adapter, stored_arbeitnow_page)
    first, second, still_pending = pending[0], pending[1], pending[2]
    await reject(engine, [first, second])
    expired = await rejected_alone(engine, owner, fetched_at=now - RAW_BODY_TTL - timedelta(seconds=1))
    (expired_item,) = (
        await execute(engine, "SELECT id FROM raw_items WHERE raw_response_id = :r", r=expired)
    ).scalars()
    await ended(engine, owner)

    # a response id: its rejected items (its pending ones are no replay)
    r = await logged_in_client.post(REPLAY, json={"raw_response_id": rr})
    assert r.status_code == 202, r.text
    operation_id = r.json()["operation_id"]
    assert r.json() == {"operation_id": operation_id, "items": 2}
    async with engine.connect() as c:
        payload, dedup_key, status = (
            await c.execute(
                sa.text("SELECT payload, dedup_key, status FROM operations WHERE id = :o"),
                {"o": operation_id},
            )
        ).one()
    assert (payload, dedup_key, status) == (
        {"raw_item_ids": [first, second]},
        replay.dedup_key([first, second]),
        "pending",
    )
    # the same set, listed in another order, with a repeat and with ids that are no replay (a pending item, an
    # expired one, an id that names no item): the same operation, its two items
    r = await logged_in_client.post(
        REPLAY, json={"raw_item_ids": [second, first, second, still_pending, expired_item, 10**12]}
    )
    assert (r.status_code, r.json()) == (202, {"operation_id": operation_id, "items": 2})
    assert await scalar(engine, "SELECT count(*) FROM operations") == 1

    # nothing to replay: 409; an unknown response: 404
    nothing: tuple[dict[str, Any], ...] = (
        {"raw_item_ids": [still_pending, expired_item]},
        {"raw_response_id": expired},
    )
    for body in nothing:
        r = await logged_in_client.post(REPLAY, json=body)
        assert (r.status_code, r.json()["code"]) == (409, "conflict_state"), body
    r = await logged_in_client.post(REPLAY, json={"raw_response_id": 10**12})
    assert (r.status_code, r.json()["code"]) == (404, "not_found")

    # both, neither, no id, too many ids, an id below 1, an unknown member: 422
    invalid: tuple[dict[str, Any], ...] = (
        {"raw_response_id": rr, "raw_item_ids": [first]},
        {},
        {"raw_item_ids": []},
        {"raw_item_ids": list(range(1, replay.MAX_ITEMS + 2))},
        {"raw_item_ids": [0]},
        {"raw_response_id": rr, "other": 1},
    )
    for body in invalid:
        r = await logged_in_client.post(REPLAY, json=body)
        assert (r.status_code, r.json()["code"]) == (422, "validation_failed"), body
    assert await scalar(engine, "SELECT count(*) FROM operations") == 1
