from datetime import UTC, datetime

import httpx
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.sources.watchlist_validate import request_validation, validate_company
from jobhunter.worker import operations as ops
from jobhunter.worker.tasks import defer_in_tx

pytestmark = pytest.mark.integration
COMPANY = sa.text(
    "INSERT INTO watchlist_companies "
    "(display_name, company_key, ats_type, board_slug, lever_region, detection) "
    "VALUES (:name, :key, :ats, :slug, :region, 'manual') RETURNING id"
)
VALIDATION = sa.text("SELECT validation_status, validated_at FROM watchlist_companies WHERE id = :i")
INSTANCES = sa.text(
    "SELECT source_id, instance_key, watchlist_company_id FROM source_instances "
    "WHERE watchlist_company_id IS NOT NULL OR instance_key <> 'default' ORDER BY id"
)
OPERATION = sa.text("SELECT status, last_error_code FROM operations WHERE id = :i")


async def company(engine, slug: str, ats: str = "greenhouse", region: str | None = None) -> int:
    async with engine.begin() as c:
        params = {"name": slug.title(), "key": slug, "ats": ats, "slug": slug, "region": region}
        return int((await c.execute(COMPANY, params)).scalar_one())


async def validation(engine, company_id: int) -> tuple[str, datetime | None]:
    async with engine.connect() as c:
        status, at = (await c.execute(VALIDATION, {"i": company_id})).one()
    return status, at


async def instances(engine) -> list[tuple[str, str, int | None]]:
    async with engine.connect() as c:
        return [tuple(r) for r in (await c.execute(INSTANCES)).all()]


async def run_validation(engine, company_id: int) -> int:
    async with engine.begin() as c:
        op, created = await ops.create_operation(
            c,
            kind="watchlist_validate",
            payload={"watchlist_company_id": company_id},
            dedup_key=f"watchlist_validate:{company_id}",
            defer=defer_in_tx,
        )
    assert created
    await ops.execute_operation(engine, op, epoch=1)
    return op


async def test_operation_validates_and_creates_the_instance(
    engine, current_epoch, queue_app, fake_http
) -> None:
    before = utcnow()
    found = await company(engine, "getyourguide")
    op = await run_validation(engine, found)
    status, at = await validation(engine, found)
    assert status == "ok" and at is not None and at >= before
    assert await instances(engine) == [("greenhouse", "getyourguide", found)]

    unknown = await company(engine, "jobhunter-nonexistent-board")  # the routes answer greenhouse_404
    op_unknown = await run_validation(engine, unknown)
    status, at = await validation(engine, unknown)
    assert status == "not_found" and at is not None
    assert await instances(engine) == [("greenhouse", "getyourguide", found)]  # no instance for it

    async with engine.connect() as c:
        for operation in (op, op_unknown):
            assert tuple((await c.execute(OPERATION, {"i": operation})).one()) == ("succeeded", None)


async def test_every_ats_validates_through_its_list_url(engine, current_epoch, queue_app, fake_http) -> None:
    boards = [
        ("lever", "leverdemo", "eu", "eu:leverdemo"),
        ("ashby", "n8n", None, "n8n"),
        ("personio", "everphone", None, "everphone"),
        ("recruitee", "bunq", None, "bunq"),
        ("smartrecruiters", "DeliveryHero", None, "DeliveryHero"),
    ]
    for ats, slug, region, key in boards:
        company_id = await company(engine, slug, ats, region)
        await run_validation(engine, company_id)
        assert (await validation(engine, company_id))[0] == "ok", ats
        assert (ats, key, company_id) in await instances(engine)
    gone = await company(engine, "gone", "lever", "global")
    await run_validation(engine, gone)
    assert (await validation(engine, gone))[0] == "not_found"


async def test_validation_errors_and_the_single_get(engine, current_epoch) -> None:
    sent: list[httpx.Request] = []

    def answer(status: int, body: bytes = b"", **headers: str):
        def handle(request: httpx.Request) -> httpx.Response:
            sent.append(request)
            return httpx.Response(status, content=body, headers=headers)

        return httpx.MockTransport(handle)

    now = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
    company_id = await company(engine, "acme")
    cases = [
        (
            answer(301, location="https://boards-api.greenhouse.io/v1/boards/other/jobs"),
            "error",
        ),  # not followed
        (answer(503), "error"),
        (answer(200, b"<html>maintenance</html>", **{"content-type": "text/html"}), "error"),  # no envelope
        (answer(200, b'{"jobs": [], "meta": {"total": 0}}'), "ok"),
    ]
    for transport, expected in cases:
        sent.clear()
        async with httpx.AsyncClient(transport=transport) as client, engine.begin() as c:
            assert await validate_company(c, company_id, client=client, now=now) == expected
        assert [(r.method, r.url.host, r.url.path, r.url.query) for r in sent] == [
            ("GET", "boards-api.greenhouse.io", "/v1/boards/acme/jobs", b"content=true")
        ]
        assert await validation(engine, company_id) == (expected, now)

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as client, engine.begin() as c:
        assert await validate_company(c, company_id, client=client, now=now) == "error"
    async with httpx.AsyncClient() as client, engine.begin() as c:
        with pytest.raises(LookupError):
            await validate_company(c, company_id + 1000, client=client, now=now)


async def test_a_request_is_deduplicated_per_company(engine, current_epoch, queue_app) -> None:
    company_id = await company(engine, "acme")
    async with engine.begin() as c:
        first = await request_validation(c, company_id, defer=defer_in_tx)
        again = await request_validation(c, company_id, defer=defer_in_tx)
    assert first[1] and again == (first[0], False)
    async with engine.connect() as c:
        kind, payload, dedup = (
            await c.execute(
                sa.text("SELECT kind, payload, dedup_key FROM operations WHERE id = :i"), {"i": first[0]}
            )
        ).one()
    assert (kind, payload, dedup) == (
        "watchlist_validate",
        {"watchlist_company_id": company_id},
        f"watchlist_validate:{company_id}",
    )


async def test_a_removed_company_ends_its_validation_quietly(
    engine, current_epoch, queue_app, fake_http
) -> None:
    company_id = await company(engine, "getyourguide")
    async with engine.begin() as c:
        op, _ = await request_validation(c, company_id, defer=defer_in_tx)
        await c.execute(sa.text("DELETE FROM watchlist_companies WHERE id = :i"), {"i": company_id})
    await ops.execute_operation(engine, op, epoch=1)
    async with engine.connect() as c:
        assert tuple((await c.execute(OPERATION, {"i": op})).one()) == ("succeeded", None)
    assert await instances(engine) == []


async def test_a_validation_whose_claim_was_lost_meanwhile_writes_nothing(
    engine, current_epoch, queue_app, fake_http
) -> None:
    """Fix wave F4 (ledger L151): the owner lock comes after the GET, in the transaction of the writes; a
    claim that lost the operation meanwhile (reclaimed, or a newer execution) is `FencedOut` there, so neither
    the validation result nor the board's instance is written."""
    from uuid import uuid4

    from jobhunter.errors import FencedOut
    from jobhunter.sources.watchlist_validate import run_watchlist_validate

    company_id = await company(engine, "getyourguide")
    async with engine.begin() as c:
        op, _ = await request_validation(c, company_id, defer=defer_in_tx)
    before = await validation(engine, company_id)
    lost = ops.OperationClaim(op, uuid4(), "watchlist_validate", {"watchlist_company_id": company_id}, 0)
    with pytest.raises(FencedOut):
        await run_watchlist_validate(ops.OperationContext(claim=lost, engine=engine))
    assert await validation(engine, company_id) == before
    assert await instances(engine) == []
    async with engine.connect() as c:  # the operation itself is untouched: still waiting for its own claim
        assert tuple((await c.execute(OPERATION, {"i": op})).one()) == ("pending", None)
