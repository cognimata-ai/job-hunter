"""`GET /api/watchlist` (spec §14.2, §15.7; plan M4 Task 13, D1): every watchlist company by display name,
then id, with its board, its validation, its flag in the active settings (false for a company they do not
list), its source instance's health, last error, last success and last complete snapshot (null without an
instance), and its postings by availability, every bucket present.

The companies are added through the api (company, instance and settings entry) unless a test needs one
without an instance; their validation, health, snapshots and postings are written directly. Every company and
board is invented; no test sends a request to a network.
"""

import copy
from datetime import datetime, timedelta
from typing import Any, Final

import httpx
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.sources import watchlist_listing
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.collect import make_posting
from tests.support.settings import save, settings_body

pytestmark = pytest.mark.integration

URL: Final = "/api/watchlist"
NO_POSTINGS: Final = {"active": 0, "stale": 0, "unknown": 0, "closed": 0}
INSTANCE_OF: Final = sa.text("SELECT id FROM source_instances WHERE watchlist_company_id = :w")
VALIDATED: Final = sa.text(
    "UPDATE watchlist_companies SET validation_status = :status, validated_at = :at WHERE id = :w"
)
HEALTH: Final = sa.text(
    "UPDATE source_instances SET health = :health, last_error_code = :code, last_success_at = :at "
    "WHERE id = :i"
)
SNAPSHOT: Final = sa.text(
    "INSERT INTO ats_snapshots (source_instance_id, status, incomplete_reason, member_count, taken_at) "
    "VALUES (:i, :status, :reason, 3, :at) RETURNING id"
)
LAST_COMPLETE: Final = sa.text("UPDATE source_instances SET last_complete_snapshot_id = :s WHERE id = :i")
COMPANY_ALONE: Final = sa.text(
    "INSERT INTO watchlist_companies (display_name, company_key, ats_type, board_slug, detection) "
    "VALUES ('Beta AG', 'beta', 'ashby', 'beta', 'manual') RETURNING id"
)


async def listed(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    r = await client.get(URL)
    assert r.status_code == 200, (r.status_code, r.text)
    body: list[dict[str, Any]] = r.json()
    return body


async def added(client: httpx.AsyncClient, **body: Any) -> int:
    r = await client.post(URL, json=body)
    assert r.status_code == 201, (r.status_code, r.text)
    return int(r.json()["id"])


async def instance_of(engine: AsyncEngine, company_id: int) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(INSTANCE_OF, {"w": company_id})).scalar_one())


def at(text: str | None) -> datetime | None:
    return None if text is None else datetime.fromisoformat(text)


def timed(company: dict[str, Any]) -> dict[str, Any]:
    """A listed company with its times parsed."""
    out = company | {"validated_at": at(company["validated_at"])}
    if (instance := company["instance"]) is not None:
        out["instance"] = instance | {
            "last_success_at": at(instance["last_success_at"]),
            "last_complete_snapshot_at": at(instance["last_complete_snapshot_at"]),
        }
    return out


async def test_watchlist_lists_validation_health_last_snapshot_and_posting_counts(
    engine: AsyncEngine, logged_in_client: httpx.AsyncClient
) -> None:
    now = utcnow()
    zeta = await added(logged_in_client, display_name="Zeta GmbH", ats_type="greenhouse", board_slug="zeta")
    acme_lever = await added(
        logged_in_client, display_name="Acme GmbH", ats_type="lever", board_slug="acme", lever_region="eu"
    )
    acme_greenhouse = await added(
        logged_in_client, display_name="Acme GmbH", ats_type="greenhouse", board_slug="acme"
    )
    # Zeta disabled in the settings; both Acme boards stay enabled, as their adds listed them
    payload = copy.deepcopy((await settings_body(logged_in_client))["payload"])
    payload["watchlist"] = [
        entry | {"enabled": entry["watchlist_company_id"] != zeta} for entry in payload["watchlist"]
    ]
    await save(logged_in_client, payload)

    lever = await instance_of(engine, acme_lever)
    snapshot_at, success_at = now - timedelta(hours=2), now - timedelta(hours=1)
    async with engine.begin() as conn:
        await conn.execute(VALIDATED, {"status": "ok", "at": now - timedelta(days=1), "w": acme_lever})
        await conn.execute(VALIDATED, {"status": "not_found", "at": now - timedelta(days=2), "w": zeta})
        await conn.execute(HEALTH, {"health": "degraded", "code": "http_5xx", "at": success_at, "i": lever})
        complete = {"i": lever, "status": "complete", "reason": None, "at": snapshot_at}
        snapshot = int((await conn.execute(SNAPSHOT, complete)).scalar_one())
        await conn.execute(LAST_COMPLETE, {"s": snapshot, "i": lever})
        # a later incomplete snapshot is no complete one
        later = {"i": lever, "status": "incomplete", "reason": "non_2xx", "at": now - timedelta(minutes=30)}
        await conn.execute(SNAPSHOT, later)
    availabilities = ["active", "active", "stale", "unknown", "closed", "closed", "closed"]
    for n, availability in enumerate(availabilities):
        await make_posting(
            engine,
            source_id="lever",
            instance_key="eu:acme",
            native_id=f"acme-{n}",
            availability=availability,
            last_seen_at=now - timedelta(days=1),
            availability_changed_at=now - timedelta(days=1),
        )
    # a posting of another source is no posting of a watchlist board
    await make_posting(
        engine,
        source_id="jsearch",
        native_id="jsearch-1",
        availability="active",
        last_seen_at=now,
        availability_changed_at=now,
    )

    body = [timed(company) for company in await listed(logged_in_client)]

    assert body == [
        {
            "id": acme_lever,
            "display_name": "Acme GmbH",
            "ats_type": "lever",
            "board_slug": "acme",
            "lever_region": "eu",
            "validation_status": "ok",
            "validated_at": now - timedelta(days=1),
            "enabled": True,
            "instance": {
                "health": "degraded",
                "last_error_code": "http_5xx",
                "last_success_at": success_at,
                "last_complete_snapshot_at": snapshot_at,
            },
            "postings": {"active": 2, "stale": 1, "unknown": 1, "closed": 3},
        },
        {  # the same name: by id
            "id": acme_greenhouse,
            "display_name": "Acme GmbH",
            "ats_type": "greenhouse",
            "board_slug": "acme",
            "lever_region": None,
            "validation_status": "unvalidated",
            "validated_at": None,
            "enabled": True,
            "instance": {
                "health": "ok",
                "last_error_code": None,
                "last_success_at": None,
                "last_complete_snapshot_at": None,
            },
            "postings": NO_POSTINGS,
        },
        {
            "id": zeta,
            "display_name": "Zeta GmbH",
            "ats_type": "greenhouse",
            "board_slug": "zeta",
            "lever_region": None,
            "validation_status": "not_found",
            "validated_at": now - timedelta(days=2),
            "enabled": False,
            "instance": {
                "health": "ok",
                "last_error_code": None,
                "last_success_at": None,
                "last_complete_snapshot_at": None,
            },
            "postings": NO_POSTINGS,
        },
    ]


async def test_an_unvalidated_company_without_an_instance(
    engine: AsyncEngine, logged_in_client: httpx.AsyncClient
) -> None:
    async with engine.begin() as conn:
        company_id = int((await conn.execute(COMPANY_ALONE)).scalar_one())  # no instance, no settings entry

    assert await listed(logged_in_client) == [
        {
            "id": company_id,
            "display_name": "Beta AG",
            "ats_type": "ashby",
            "board_slug": "beta",
            "lever_region": None,
            "validation_status": "unvalidated",
            "validated_at": None,
            "enabled": False,
            "instance": None,
            "postings": NO_POSTINGS,
        }
    ]


async def test_without_settings_a_company_is_listed_disabled(
    engine: AsyncEngine, logged_in_client: httpx.AsyncClient
) -> None:
    company_id = await added(
        logged_in_client, display_name="Acme GmbH", ats_type="greenhouse", board_slug="acme"
    )
    async with engine.connect() as conn:  # nothing seeded, in a transaction rolled back
        await conn.execute(sa.text("DELETE FROM active_config"))
        companies = await watchlist_listing.watchlist(conn)
        await conn.rollback()

    assert [(c.id, c.enabled, c.instance is not None) for c in companies] == [(company_id, False, True)]
    assert (await listed(logged_in_client))[0]["enabled"] is True  # the settings list it, enabled
