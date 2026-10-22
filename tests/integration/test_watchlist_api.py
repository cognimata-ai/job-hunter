"""The watchlist routes (spec §5.4, §9.1, §14.2; plan Task 22, Deviations 3 and 15): `POST
/api/watchlist/parse-url` reads a careers URL by pattern alone, `POST /api/watchlist` adds a board (company,
instance, validation operation and a `collection` settings version in one transaction) and `DELETE
/api/watchlist/{id}` removes it again.

Every company and board is invented or one of the M1 fixture boards; no test sends a request to a network.
"""

import asyncio
import copy
import json
import socket
from collections.abc import Callable
from typing import Any, Final

import httpx
import pytest
import sqlalchemy as sa
from jobhunter.sources import http
from jobhunter.worker import operations as ops
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.collect import insert_job
from tests.support.fixtures import count_rows
from tests.support.settings import procrastinate_job_exists_for, save, save_scope, settings_body

pytestmark = pytest.mark.integration

URL: Final = "/api/watchlist"
PARSE_URL: Final = "/api/watchlist/parse-url"
_LOCK_ACTIVE: Final = sa.text("SELECT 1 FROM active_config WHERE id = 1 FOR UPDATE")
_COMPANY: Final = sa.text("SELECT * FROM watchlist_companies WHERE id = :i")
_INSERT_COMPANY: Final = sa.text(
    "INSERT INTO watchlist_companies (display_name, company_key, ats_type, board_slug, lever_region, "
    "detection) VALUES (:name, :key, :ats, :slug, :region, 'manual') RETURNING id"
)
_INSTANCE: Final = sa.text(
    "SELECT id, watchlist_company_id FROM source_instances WHERE source_id = :s AND instance_key = :k"
)
_OPERATIONS: Final = sa.text(
    "SELECT id, status, payload, dedup_key, last_error_code FROM operations "
    "WHERE kind = 'watchlist_validate' ORDER BY id"
)
_CELLS: Final = sa.text("SELECT count(*) FROM coverage_cells WHERE source_instance_id = :i")
_POSTINGS: Final = sa.text("SELECT count(*) FROM postings WHERE source_instance_id = :i")
_VERSION: Final = sa.text(
    "SELECT base_version_id, effect_classes, acknowledged_warnings FROM settings_versions WHERE id = :v"
)
GREENHOUSE_OK: Final = {"jobs": [], "meta": {"total": 0}}


async def post(client: httpx.AsyncClient, path: str, body: dict[str, Any]) -> httpx.Response:
    """The body as ASCII JSON: a lone surrogate travels as its escape (`"\\ud800"`), as a client sends it."""
    return await client.post(path, content=json.dumps(body), headers={"content-type": "application/json"})


async def add(client: httpx.AsyncClient, **overrides: Any) -> httpx.Response:
    body = {"display_name": "Acme GmbH", "ats_type": "greenhouse", "board_slug": "acme", **overrides}
    return await post(client, URL, {k: v for k, v in body.items() if v is not ...})


async def added(client: httpx.AsyncClient, **overrides: Any) -> int:
    r = await add(client, **overrides)
    assert r.status_code == 201, (r.status_code, r.text)
    return int(r.json()["id"])


async def company_row(engine: AsyncEngine, company_id: int) -> sa.Row[Any] | None:
    async with engine.connect() as conn:
        return (await conn.execute(_COMPANY, {"i": company_id})).first()


async def instance_of(engine: AsyncEngine, source_id: str, key: str) -> sa.Row[Any] | None:
    async with engine.connect() as conn:
        return (await conn.execute(_INSTANCE, {"s": source_id, "k": key})).first()


async def operations(engine: AsyncEngine) -> list[sa.Row[Any]]:
    async with engine.connect() as conn:
        return list((await conn.execute(_OPERATIONS)).all())


async def scalar(engine: AsyncEngine, statement: sa.TextClause, **params: Any) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(statement, params)).scalar_one()


async def untouched(engine: AsyncEngine, client: httpx.AsyncClient, before: dict[str, Any]) -> None:
    """Nothing was written since `before` (`settings_body`): no version, company, instance or operation."""
    assert await settings_body(client) == before
    assert await count_rows(engine, "watchlist_companies") == 0
    assert await operations(engine) == []
    assert await instance_of(engine, "greenhouse", "acme") is None


# --- POST /api/watchlist/parse-url -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://boards.greenhouse.io/acme/jobs/4012",
            {"ats": "greenhouse", "slug": "acme", "region": None},
        ),
        ("https://job-boards.greenhouse.io/acme", {"ats": "greenhouse", "slug": "acme", "region": None}),
        (
            "https://boards.greenhouse.io/embed/job_board?for=acme",
            {"ats": "greenhouse", "slug": "acme", "region": None},
        ),
        (
            "https://jobs.lever.co/leverdemo/abc-123",
            {"ats": "lever", "slug": "leverdemo", "region": "global"},
        ),
        ("https://jobs.eu.lever.co/leverdemo", {"ats": "lever", "slug": "leverdemo", "region": "eu"}),
        ("https://jobs.ashbyhq.com/n8n", {"ats": "ashby", "slug": "n8n", "region": None}),
        (
            "https://everphone.jobs.personio.de/job/77",
            {"ats": "personio", "slug": "everphone", "region": None},
        ),
        ("https://everphone.jobs.personio.com/", {"ats": "personio", "slug": "everphone", "region": None}),
        ("https://bunq.recruitee.com/o/engineer", {"ats": "recruitee", "slug": "bunq", "region": None}),
        (
            "https://jobs.smartrecruiters.com/DeliveryHero/1",
            {"ats": "smartrecruiters", "slug": "DeliveryHero", "region": None},
        ),
        (
            "  HTTPS://Careers.SmartRecruiters.com/DeliveryHero  ",
            {"ats": "smartrecruiters", "slug": "DeliveryHero", "region": None},
        ),
    ],
)
async def test_parse_url_reads_the_board_of_each_ats(
    logged_in_client, url: str, expected: dict[str, Any]
) -> None:
    r = await post(logged_in_client, PARSE_URL, {"url": url})
    assert (r.status_code, r.json()) == (200, expected)


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("https://apply.workable.com/acme", "workable"),
        ("https://example.com/careers", "unsupported_pattern"),
        ("https://boards.greenhouse.io/", "unsupported_pattern"),
        ("https://www.recruitee.com/", "unsupported_pattern"),
        ("https://jobs.lever.co/../etc", "bad_slug"),
        ("careers at acme", "not_a_url"),
        ("", "not_a_url"),
    ],
)
async def test_parse_url_gives_the_reason_for_what_it_cannot_read(
    logged_in_client, url: str, reason: str
) -> None:
    r = await post(logged_in_client, PARSE_URL, {"url": url})
    assert (r.status_code, r.json()) == (200, {"unsupported": reason})


async def test_parse_url_makes_no_network_request(logged_in_client, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("parse-url must not connect to anything")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)  # the network transports;
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)  # the test client's is the app's
    r = await post(logged_in_client, PARSE_URL, {"url": "https://boards.greenhouse.io/acme"})
    assert (r.status_code, r.json()["slug"]) == (200, "acme")
    r = await post(logged_in_client, PARSE_URL, {"url": "https://example.com/jobs"})
    assert r.json() == {"unsupported": "unsupported_pattern"}


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"url": None},
        {"url": 5},
        {"url": "x" * 2049},
        {"url": "https://a.b/\x00"},
        {"url": "https://a.b/\ud800"},
        {"url": "https://boards.greenhouse.io/acme", "extra": 1},
    ],
)
async def test_parse_url_refuses_a_malformed_body(logged_in_client, body: dict[str, Any]) -> None:
    r = await post(logged_in_client, PARSE_URL, body)
    assert (r.status_code, r.json()["code"]) == (422, "validation_failed")


# --- POST /api/watchlist -----------------------------------------------------------------------------------


async def test_an_added_board_has_its_company_instance_operation_and_settings_version(
    engine, logged_in_client
) -> None:
    before = await settings_body(logged_in_client)
    r = await add(logged_in_client, display_name="  Zalando SE ", board_slug="zalando")
    assert r.status_code == 201, r.text
    company_id = r.json()["id"]
    assert r.json() == {"id": company_id, "validation_status": "unvalidated"}

    row = await company_row(engine, company_id)
    assert row is not None
    assert (row.display_name, row.company_key) == ("Zalando SE", "zalando")  # trimmed; §8.3 key
    assert (row.ats_type, row.board_slug, row.lever_region) == ("greenhouse", "zalando", None)
    assert (row.input_url, row.detection, row.enabled) == (None, "manual", True)
    assert (row.validation_status, row.validated_at) == ("unvalidated", None)

    instance = await instance_of(engine, "greenhouse", "zalando")
    assert instance is not None and instance.watchlist_company_id == company_id
    assert await scalar(engine, _CELLS, i=instance.id) == 1  # the board's one cell

    (operation,) = await operations(engine)
    assert (operation.status, operation.payload, operation.dedup_key) == (
        "pending",
        {"watchlist_company_id": company_id},
        f"watchlist_validate:{company_id}",
    )
    assert await procrastinate_job_exists_for(engine, operation.id)  # the job is deferred in the transaction

    after = await settings_body(logged_in_client)
    assert after["version"]["version_no"] == before["version"]["version_no"] + 1  # one new version
    assert after["version"]["effect_classes"] == ["collection"]
    assert after["payload"]["watchlist"] == [{"watchlist_company_id": company_id, "enabled": True}]
    expected = copy.deepcopy(before["payload"])
    expected["watchlist"] = after["payload"]["watchlist"]
    assert after["payload"] == expected  # nothing else changed
    async with engine.connect() as conn:
        version = (await conn.execute(_VERSION, {"v": after["version"]["id"]})).one()
        previous = (await conn.execute(_VERSION, {"v": before["version"]["id"]})).one()
    assert version.base_version_id == before["version"]["id"]
    assert list(version.acknowledged_warnings) == list(previous.acknowledged_warnings)  # Deviation 15


async def test_an_add_and_a_removal_copy_the_acknowledged_warnings(engine, logged_in_client) -> None:
    """Deviation 15: the internal saves ask for no acknowledgement and keep the active version's."""
    saved = await save_scope(logged_in_client, radius_km=30)
    async with engine.connect() as conn:
        acknowledged = list(
            (await conn.execute(_VERSION, {"v": saved["version"]["id"]})).one().acknowledged_warnings
        )
    assert acknowledged  # the defaults' warning, acknowledged by that save
    company_id = await added(logged_in_client)
    assert (await logged_in_client.delete(f"{URL}/{company_id}")).status_code == 204
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                sa.text("SELECT acknowledged_warnings FROM settings_versions WHERE id > :v ORDER BY id"),
                {"v": saved["version"]["id"]},
            )
        ).all()
    assert [list(r.acknowledged_warnings) for r in rows] == [acknowledged, acknowledged]  # add, remove


async def test_a_board_added_from_a_parsed_url_records_the_pattern(engine, logged_in_client) -> None:
    url = "https://jobs.eu.lever.co/leverdemo/abc"
    company_id = await added(
        logged_in_client,
        display_name="Lever Demo",
        ats_type="lever",
        board_slug="leverdemo",
        lever_region="eu",
        input_url=f"  {url} ",
    )
    row = await company_row(engine, company_id)
    assert row is not None
    assert (row.detection, row.input_url, row.lever_region) == ("pattern", url, "eu")
    instance = await instance_of(engine, "lever", "eu:leverdemo")  # Lever keys its board by region
    assert instance is not None and instance.watchlist_company_id == company_id


async def test_one_slug_on_two_lever_regions_is_two_boards(engine, logged_in_client) -> None:
    first = await added(logged_in_client, ats_type="lever", lever_region="global")
    second = await added(logged_in_client, ats_type="lever", lever_region="eu")
    third = await added(logged_in_client, ats_type="ashby")  # the slug alone is not the board
    assert len({first, second, third}) == 3
    assert (await settings_body(logged_in_client))["payload"]["watchlist"] == [
        {"watchlist_company_id": c, "enabled": True} for c in (first, second, third)
    ]


async def test_a_name_at_the_limit_is_stored_whole(engine, logged_in_client) -> None:
    name = "Ä" * 120
    row = await company_row(engine, await added(logged_in_client, display_name=name))
    assert row is not None and row.display_name == name


async def test_a_board_already_on_the_list_is_a_conflict_naming_the_company(engine, logged_in_client) -> None:
    first = await added(logged_in_client)
    before = await settings_body(logged_in_client)
    for overrides in (
        {},
        {"display_name": "Another Name"},
        {"input_url": "https://boards.greenhouse.io/acme"},
    ):
        r = await add(logged_in_client, **overrides)
        assert (r.status_code, r.json()["code"], r.json()["watchlist_company_id"]) == (
            409,
            "conflict_state",
            first,
        ), overrides
    assert await settings_body(logged_in_client) == before  # no version
    assert await count_rows(engine, "watchlist_companies") == 1
    assert len(await operations(engine)) == 1


async def test_two_requests_for_one_board_make_one_company(engine, logged_in_client) -> None:
    results = await asyncio.gather(add(logged_in_client), add(logged_in_client))
    assert sorted(r.status_code for r in results) == [201, 409]
    created = next(r for r in results if r.status_code == 201).json()["id"]
    assert next(r for r in results if r.status_code == 409).json()["watchlist_company_id"] == created
    assert await count_rows(engine, "watchlist_companies") == 1
    assert len(await operations(engine)) == 1
    assert (await settings_body(logged_in_client))["payload"]["watchlist"] == [
        {"watchlist_company_id": created, "enabled": True}
    ]


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"display_name": ...}, "body.display_name"),
        ({"display_name": ""}, "body.display_name"),
        ({"display_name": " \t "}, "body.display_name"),
        ({"display_name": "x" * 121}, "body.display_name"),
        ({"display_name": "Ac\x00me"}, "body.display_name"),
        ({"display_name": "Ac\ud800me"}, "body.display_name"),
        ({"display_name": 5}, "body.display_name"),
        ({"board_slug": ...}, "body.board_slug"),
        ({"board_slug": ""}, "body.board_slug"),
        ({"board_slug": " acme"}, "body.board_slug"),
        ({"board_slug": "acme\n"}, "body.board_slug"),
        ({"board_slug": "-acme"}, "body.board_slug"),
        ({"board_slug": "acme/jobs"}, "body.board_slug"),
        ({"board_slug": "ac me"}, "body.board_slug"),
        ({"board_slug": "a" * 101}, "body.board_slug"),
        ({"board_slug": "ac\x00me"}, "body.board_slug"),
        ({"board_slug": "ac\ud800me"}, "body.board_slug"),
        ({"board_slug": ".."}, "body.board_slug"),
        ({"ats_type": ...}, "body.ats_type"),
        ({"ats_type": "workable"}, "body.ats_type"),
        ({"ats_type": "GREENHOUSE"}, "body.ats_type"),
        ({"ats_type": "lever"}, "body.lever_region"),  # a Lever board needs its region
        ({"ats_type": "lever", "lever_region": "us"}, "body.lever_region"),
        ({"lever_region": "global"}, "body.lever_region"),  # and no other board has one
        ({"input_url": ""}, "body.input_url"),
        ({"input_url": "https://example.com/careers"}, "body.input_url"),  # not a board URL
        ({"input_url": "https://boards.greenhouse.io/other"}, "body.input_url"),  # another board
        ({"input_url": "https://jobs.lever.co/acme"}, "body.input_url"),  # another ATS
        ({"input_url": "https://boards.greenhouse.io/acme/\x00"}, "body.input_url"),
        ({"input_url": "https://boards.greenhouse.io/acme/\ud800"}, "body.input_url"),
        ({"input_url": "https://boards.greenhouse.io/acme/" + "x" * 2048}, "body.input_url"),
        ({"surprise": 1}, "body.surprise"),
        ({"enabled": False}, "body.enabled"),
    ],
)
async def test_an_invalid_body_is_refused_and_writes_nothing(
    engine, logged_in_client, overrides: dict[str, Any], field: str
) -> None:
    before = await settings_body(logged_in_client)
    r = await add(logged_in_client, **overrides)
    assert (r.status_code, r.json()["code"]) == (422, "validation_failed")
    assert field in [e["field"] for e in r.json()["errors"]]
    await untouched(engine, logged_in_client, before)


async def test_a_lever_region_must_match_the_url(engine, logged_in_client) -> None:
    before = await settings_body(logged_in_client)
    r = await add(
        logged_in_client,
        ats_type="lever",
        board_slug="leverdemo",
        lever_region="global",
        input_url="https://jobs.eu.lever.co/leverdemo",
    )
    assert (r.status_code, [e["field"] for e in r.json()["errors"]]) == (422, ["body.input_url"])
    assert await settings_body(logged_in_client) == before
    assert await count_rows(engine, "watchlist_companies") == 0


async def test_a_full_watchlist_refuses_one_more_and_keeps_nothing(engine, logged_in_client) -> None:
    """§13.1 allows 100 entries; the internal save refuses the 101st, and the company row goes with it."""
    async with engine.begin() as conn:
        ids = [
            int(
                (
                    await conn.execute(
                        _INSERT_COMPANY,
                        {
                            "name": f"Co {n}",
                            "key": f"co {n}",
                            "ats": "ashby",
                            "slug": f"co-{n}",
                            "region": None,
                        },
                    )
                ).scalar_one()
            )
            for n in range(100)
        ]
    active = await settings_body(logged_in_client)
    payload = copy.deepcopy(active["payload"])
    payload["watchlist"] = [{"watchlist_company_id": i, "enabled": False} for i in ids]
    full = await save(logged_in_client, payload)
    before = await settings_body(logged_in_client)
    assert before["version"]["id"] == full["version"]["id"]

    r = await add(logged_in_client)
    assert (r.status_code, r.json()["code"]) == (422, "validation_failed")
    assert [(e["field"], e["code"]) for e in r.json()["errors"]] == [("payload.watchlist", "out_of_range")]
    assert await settings_body(logged_in_client) == before
    assert await count_rows(engine, "watchlist_companies") == 100
    assert await operations(engine) == []
    assert await instance_of(engine, "greenhouse", "acme") is None


async def test_an_add_waits_for_active_config_before_it_touches_a_company(engine, logged_in_client) -> None:
    """§9.1: `active_config` is the first lock. A rival that holds it and then inserts the same board gets in
    first (no deadlock, no wait on the request), and the request then meets that company: 409."""
    async with engine.connect() as holder:
        await holder.execute(_LOCK_ACTIVE)
        request = asyncio.create_task(add(logged_in_client))
        await asyncio.sleep(0.4)
        assert not request.done()
        await holder.execute(sa.text("SET LOCAL lock_timeout = '500ms'"))
        rival = int(
            (
                await holder.execute(
                    _INSERT_COMPANY,
                    {"name": "Rival", "key": "rival", "ats": "greenhouse", "slug": "acme", "region": None},
                )
            ).scalar_one()
        )
        await holder.commit()
    r = await asyncio.wait_for(request, timeout=10)
    assert (r.status_code, r.json()["watchlist_company_id"]) == (409, rival)


# --- DELETE /api/watchlist/{id} ----------------------------------------------------------------------------


async def test_a_removed_board_leaves_the_settings_and_its_postings_stay(engine, logged_in_client) -> None:
    keep = await added(logged_in_client, board_slug="keeper")
    gone = await added(logged_in_client, board_slug="zalando")
    instance = await instance_of(engine, "greenhouse", "zalando")
    assert instance is not None
    await insert_job(engine, native_id="p1", source_id="greenhouse", instance_key="zalando")
    await insert_job(engine, native_id="p2", source_id="greenhouse", instance_key="zalando")
    before = await settings_body(logged_in_client)

    r = await logged_in_client.delete(f"{URL}/{gone}")
    assert (r.status_code, r.content) == (204, b"")

    after = await settings_body(logged_in_client)
    assert after["version"]["version_no"] == before["version"]["version_no"] + 1  # a version of its own
    assert after["version"]["effect_classes"] == ["collection"]
    assert after["payload"]["watchlist"] == [{"watchlist_company_id": keep, "enabled": True}]
    expected = copy.deepcopy(before["payload"])
    expected["watchlist"] = after["payload"]["watchlist"]
    assert after["payload"] == expected
    async with engine.connect() as conn:
        version = (await conn.execute(_VERSION, {"v": after["version"]["id"]})).one()
    assert version.base_version_id == before["version"]["id"]

    assert await company_row(engine, gone) is None
    assert await company_row(engine, keep) is not None
    kept = await instance_of(engine, "greenhouse", "zalando")
    assert (
        kept is not None and kept.id == instance.id and kept.watchlist_company_id is None
    )  # the FK's SET NULL
    assert await scalar(engine, _POSTINGS, i=instance.id) == 2


async def test_a_board_added_again_after_its_removal_gets_its_instance_and_postings_back(
    engine, logged_in_client
) -> None:
    first = await added(logged_in_client, board_slug="zalando")
    instance = await instance_of(engine, "greenhouse", "zalando")
    assert instance is not None
    await insert_job(engine, native_id="p1", source_id="greenhouse", instance_key="zalando")
    assert (await logged_in_client.delete(f"{URL}/{first}")).status_code == 204

    second = await added(logged_in_client, board_slug="zalando", display_name="Zalando, again")
    assert second != first
    again = await instance_of(engine, "greenhouse", "zalando")
    assert again is not None
    assert (again.id, again.watchlist_company_id) == (instance.id, second)  # re-linked, not re-created
    assert await scalar(engine, _POSTINGS, i=instance.id) == 1
    assert await count_rows(engine, "source_instances", source_id="greenhouse", instance_key="zalando") == 1
    assert await scalar(engine, _CELLS, i=instance.id) >= 1
    assert (await settings_body(logged_in_client))["payload"]["watchlist"] == [
        {"watchlist_company_id": second, "enabled": True}
    ]
    assert [op.payload["watchlist_company_id"] for op in await operations(engine)] == [first, second]


async def test_a_listed_company_removed_while_disabled_in_settings_leaves_no_entry(
    engine, logged_in_client
) -> None:
    company_id = await added(logged_in_client)
    active = await settings_body(logged_in_client)
    payload = copy.deepcopy(active["payload"])
    payload["watchlist"] = [{"watchlist_company_id": company_id, "enabled": False}]
    await save(logged_in_client, payload)
    assert (await logged_in_client.delete(f"{URL}/{company_id}")).status_code == 204
    assert (await settings_body(logged_in_client))["payload"]["watchlist"] == []


async def test_a_company_no_settings_version_names_is_removed_without_a_version(
    engine, logged_in_client
) -> None:
    async with engine.begin() as conn:
        orphan = int(
            (
                await conn.execute(
                    _INSERT_COMPANY,
                    {"name": "Orphan", "key": "orphan", "ats": "ashby", "slug": "orphan", "region": None},
                )
            ).scalar_one()
        )
    before = await settings_body(logged_in_client)
    assert (await logged_in_client.delete(f"{URL}/{orphan}")).status_code == 204
    assert await company_row(engine, orphan) is None
    assert await settings_body(logged_in_client) == before


async def test_an_unknown_company_is_not_found_and_a_removed_one_too(engine, logged_in_client) -> None:
    company_id = await added(logged_in_client)
    assert (await logged_in_client.delete(f"{URL}/{company_id}")).status_code == 204
    before = await settings_body(logged_in_client)
    for missing in (company_id, company_id + 1000, 2**63 - 1):
        r = await logged_in_client.delete(f"{URL}/{missing}")
        assert (r.status_code, r.json()["code"]) == (404, "not_found"), missing
    assert await settings_body(logged_in_client) == before


@pytest.mark.parametrize("company_id", ["0", "-1", str(2**63), "99999999999999999999", "abc", "1.5"])
async def test_a_company_id_outside_bigint_is_refused(logged_in_client, company_id: str) -> None:
    r = await logged_in_client.delete(f"{URL}/{company_id}")
    assert (r.status_code, r.json()["code"]) == (422, "validation_failed")


async def test_a_remove_waits_for_active_config_before_it_touches_the_company(
    engine, logged_in_client
) -> None:
    """§9.1: a holder of `active_config` then locks the company row; the request, which would hold that row
    first if it broke the order, would deadlock with it. It waits for the first lock instead."""
    company_id = await added(logged_in_client)
    async with engine.connect() as holder:
        await holder.execute(_LOCK_ACTIVE)
        request = asyncio.create_task(logged_in_client.delete(f"{URL}/{company_id}"))
        await asyncio.sleep(0.4)
        assert not request.done()
        await holder.execute(sa.text("SET LOCAL lock_timeout = '500ms'"))
        await holder.execute(
            sa.text("SELECT id FROM watchlist_companies WHERE id = :i FOR UPDATE"), {"i": company_id}
        )
        await holder.commit()
    r = await asyncio.wait_for(request, timeout=10)
    assert r.status_code == 204
    assert await company_row(engine, company_id) is None


# --- removal and the validation operation ------------------------------------------------------------------


async def operation_end(engine: AsyncEngine, operation_id: int) -> tuple[str, str | None]:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                sa.text("SELECT status, last_error_code FROM operations WHERE id = :i"), {"i": operation_id}
            )
        ).one()
    return row.status, row.last_error_code


async def test_a_board_removed_before_its_validation_ran_ends_the_operation_quietly(
    engine, logged_in_client, current_epoch, queue_app, fake_http
) -> None:
    company_id = await added(logged_in_client, board_slug="getyourguide")
    (operation,) = await operations(engine)
    assert (await logged_in_client.delete(f"{URL}/{company_id}")).status_code == 204

    await ops.execute_operation(engine, operation.id, epoch=1)  # the worker claims it now

    assert await operation_end(engine, operation.id) == ("succeeded", None)  # no failure, no retry
    assert await company_row(engine, company_id) is None
    instance = await instance_of(engine, "greenhouse", "getyourguide")
    assert instance is not None and instance.watchlist_company_id is None  # nothing re-linked it


@pytest.mark.parametrize("status", [200, 404, 503])
async def test_a_board_removed_while_its_validation_request_is_in_flight_ends_the_operation_quietly(
    engine, logged_in_client, current_epoch, queue_app, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    company_id = await added(logged_in_client, board_slug="getyourguide")
    (operation,) = await operations(engine)
    answered: list[int] = []

    async def board_answers(request: httpx.Request) -> httpx.Response:
        removed = await logged_in_client.delete(f"{URL}/{company_id}")  # the user removes it meanwhile
        answered.append(removed.status_code)
        return httpx.Response(status, json=GREENHOUSE_OK)

    monkeypatch.setattr(
        http, "_configuration", http._Configuration("test-agent", httpx.MockTransport(board_answers))
    )
    await ops.execute_operation(engine, operation.id, epoch=1)

    assert answered == [204]
    assert await operation_end(engine, operation.id) == ("succeeded", None)
    assert await company_row(engine, company_id) is None
    instance = await instance_of(engine, "greenhouse", "getyourguide")
    assert instance is not None and instance.watchlist_company_id is None


async def test_removing_and_adding_a_board_again_lets_only_the_new_company_be_validated(
    engine, logged_in_client, current_epoch, queue_app, fake_http
) -> None:
    first = await added(logged_in_client, board_slug="getyourguide")
    assert (await logged_in_client.delete(f"{URL}/{first}")).status_code == 204
    second = await added(logged_in_client, board_slug="getyourguide")
    old, new = await operations(engine)

    await ops.execute_operation(engine, old.id, epoch=1)
    row = await company_row(engine, second)
    assert row is not None and row.validation_status == "unvalidated"  # the old operation touched nothing
    await ops.execute_operation(engine, new.id, epoch=1)

    assert await operation_end(engine, old.id) == ("succeeded", None)
    assert await operation_end(engine, new.id) == ("succeeded", None)
    row = await company_row(engine, second)
    assert row is not None and row.validation_status == "ok"
    instance = await instance_of(engine, "greenhouse", "getyourguide")
    assert instance is not None and instance.watchlist_company_id == second


# --- session, CSRF and logs --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", PARSE_URL, {"url": "https://boards.greenhouse.io/acme"}),
        ("POST", URL, {"display_name": "Acme", "ats_type": "greenhouse", "board_slug": "acme"}),
        ("DELETE", f"{URL}/1", None),
    ],
)
async def test_the_routes_need_a_session_and_the_csrf_header(
    engine, api_client, logged_in_client, method: str, path: str, body: dict[str, Any] | None
) -> None:
    kwargs: dict[str, Any] = {} if body is None else {"json": body}
    r = await api_client.request(method, path, headers={"X-JH-Request": "1"}, **kwargs)
    assert (r.status_code, r.json()["code"]) == (401, "unauthenticated")
    r = await logged_in_client.request(method, path, headers={"X-JH-Request": "0"}, **kwargs)
    assert (r.status_code, r.json()["code"]) == (403, "csrf_failed")
    assert await count_rows(engine, "watchlist_companies") == 0
    assert await operations(engine) == []


async def test_logs_carry_ids_and_codes_never_a_name_or_slug(
    engine, logged_in_client, log_events: Callable[[], list[dict[str, Any]]]
) -> None:
    company_id = await added(
        logged_in_client,
        display_name="Needle Holdings",
        board_slug="needle-board-9",
        input_url="https://boards.greenhouse.io/needle-board-9/jobs/1",
    )
    assert (await add(logged_in_client, board_slug="needle-board-9")).status_code == 409
    assert (await logged_in_client.delete(f"{URL}/{company_id}")).status_code == 204
    events = log_events()
    names = [e["event"] for e in events]
    assert "watchlist_company_added" in names and "watchlist_company_removed" in names
    for event in events:
        if event["event"].startswith("watchlist_company"):
            assert event["watchlist_company_id"] == company_id
    assert "needle" not in json.dumps(events).lower()
