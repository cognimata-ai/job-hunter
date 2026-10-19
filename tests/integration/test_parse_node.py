import hashlib
import json
from collections.abc import Sequence
from datetime import datetime, timedelta

import httpx
import pytest
import respx
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db.seed import canonical_json
from jobhunter.db.transitions import transition
from jobhunter.errors import FencedOut, InvariantViolation
from jobhunter.graphs.collect.nodes import parse_normalize as parse_node
from jobhunter.graphs.collect.state import initial_state
from jobhunter.observability.metrics import REGISTRY
from jobhunter.sources.arbeitnow import FEED_URL, ArbeitnowAdapter
from jobhunter.sources.base import (
    ContractViolation,
    ItemOutcome,
    NormalizedPosting,
    Parsed,
    RawPage,
    SourceAdapter,
)
from jobhunter.sources.instances import InstanceRow, list_instances
from jobhunter.sources.items import ParseCounts, parse_response, pending_items_of_run, replay_rejected
from jobhunter.sources.jsearch import JSearchAdapter
from jobhunter.types import JSONValue, get_str
from jobhunter.worker.runs import finalize_run
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.collect import make_deps, queued_run, supersede
from tests.support.sources import (
    Adapters,
    arbeitnow_spec,
    crafted_jsearch_jobs,
    fixture_page,
    load_fixture,
    served_response,
)

pytestmark = pytest.mark.integration
DISPOSITIONS = sa.text(
    "SELECT disposition, count(*) FROM raw_items WHERE raw_response_id=:r GROUP BY disposition"
)
ITEMS = sa.text(
    "SELECT id, item_index, native_id, item_ts, item_sha256, disposition, reason_code, normalized, "
    "disposed_at FROM raw_items WHERE raw_response_id = :r ORDER BY item_index"
)
RESPONSE = sa.text("SELECT item_count, parsed_at FROM raw_responses WHERE id = :r")
RUN_ITEMS = sa.text(
    "SELECT count(*) FROM raw_items i JOIN raw_responses r ON r.id = i.raw_response_id "
    "JOIN fetch_requests f ON f.id = r.fetch_request_id WHERE f.run_id = :run"
)
ITEM = sa.text(
    "SELECT disposition, reason_code, native_id, normalized, disposed_at FROM raw_items WHERE id = :i"
)


async def items_of(engine: AsyncEngine, raw_response_id: int) -> list[sa.Row[tuple[object, ...]]]:
    async with engine.connect() as c:
        return list((await c.execute(ITEMS, {"r": raw_response_id})).all())


async def response_of(engine: AsyncEngine, raw_response_id: int) -> tuple[object, ...]:
    async with engine.connect() as c:
        return tuple((await c.execute(RESPONSE, {"r": raw_response_id})).one())


async def item(engine: AsyncEngine, raw_item_id: int) -> sa.Row[tuple[object, ...]]:
    async with engine.connect() as c:
        return (await c.execute(ITEM, {"i": raw_item_id})).one()


async def jsearch_instance(engine: AsyncEngine) -> InstanceRow:
    async with engine.connect() as c:
        return next(i for i in await list_instances(c) if i.source_id == "jsearch")


def sha256_of(element: JSONValue) -> bytes:
    return hashlib.sha256(canonical_json(element)).digest()


def raw_items_written(source: str, disposition: str) -> float:
    labels = {"source": source, "disposition": disposition}
    return REGISTRY.get_sample_value("jobhunter_raw_items_total", labels) or 0.0


class UnreadableArray(ArbeitnowAdapter):
    """Arbeitnow whose `parse` finds the item array unreadable as a whole."""

    def parse(self, page: RawPage) -> list[ItemOutcome]:
        raise ContractViolation("item_array", status=page.status)


class Losing(ArbeitnowAdapter):
    """Arbeitnow whose `parse` loses the last element."""

    def parse(self, page: RawPage) -> list[ItemOutcome]:
        return super().parse(page)[:-1]


class Reordered(ArbeitnowAdapter):
    """Arbeitnow whose `parse` returns one outcome per element, in the wrong order."""

    def parse(self, page: RawPage) -> list[ItemOutcome]:
        return super().parse(page)[::-1]


class TolerantJSearch(JSearchAdapter):
    """JSearch as a later version might read it: a job without `job_uid` is identified by its `job_id`."""

    def parse(self, page: RawPage) -> list[ItemOutcome]:
        doc = json.loads(page.body)
        for job in doc["data"]["jobs"]:
            job.setdefault("job_uid", f"tolerated-{job['job_id']}")
        return super().parse(
            RawPage(page.spec, page.status, page.headers, json.dumps(doc).encode(), page.fetched_at)
        )


async def test_one_row_per_element_and_conservation(
    engine, current_epoch, arbeitnow_adapter, stored_arbeitnow_page
) -> None:
    owner, rr = stored_arbeitnow_page  # a served Arbeitnow page1 (30 elements) of an admitted run
    async with engine.begin() as c:
        counts = await parse_response(c, rr, adapter=arbeitnow_adapter, owner=owner)
    async with engine.connect() as c:
        by = dict((await c.execute(DISPOSITIONS, {"r": rr})).all())
        item_count, parsed_at = (
            await c.execute(sa.text("SELECT item_count, parsed_at FROM raw_responses WHERE id=:r"), {"r": rr})
        ).one()
        pending = (
            await c.execute(
                sa.text(
                    "SELECT normalized IS NOT NULL, native_id IS NOT NULL, item_ts IS NOT NULL "
                    "FROM raw_items WHERE raw_response_id=:r AND disposition='pending'"
                ),
                {"r": rr},
            )
        ).all()
    assert (
        sum(by.values()) == item_count == 30 == counts.total
        and by["out_of_scope"] >= 1
        and all(tuple(p) == (True, True, True) for p in pending)
        and parsed_at is not None
    )
    async with engine.begin() as c:
        again = await parse_response(c, rr, adapter=arbeitnow_adapter, owner=owner)
    async with engine.connect() as c:
        assert (
            await c.execute(sa.text("SELECT count(*) FROM raw_items WHERE raw_response_id=:r"), {"r": rr})
        ).scalar_one() == 30 and again == counts
    assert await response_of(engine, rr) == (30, parsed_at)  # the second call wrote nothing

    # each row is its element: its index, id and time, and the sha256 of its canonical JSON; a pending row
    # holds the parse's NormalizedPosting, a disposed one its reason and its disposal time instead
    spec = await arbeitnow_spec(engine, arbeitnow_adapter)
    outcomes = arbeitnow_adapter.parse(fixture_page("arbeitnow", "page1", spec, fetched_at=utcnow()))
    jobs = json.loads(load_fixture("arbeitnow", "page1")[2])["data"]
    rows = await items_of(engine, rr)
    assert counts == ParseCounts(pending=27, out_of_scope=3, duplicate_in_page=0, rejected=0)
    assert [r.item_index for r in rows] == list(range(30))
    assert [bytes(r.item_sha256) for r in rows] == [sha256_of(job) for job in jobs]
    assert [(r.native_id, r.item_ts) for r in rows] == [(o.native_id, o.item_ts) for o in outcomes]
    for row, outcome in zip(rows, outcomes, strict=True):
        if isinstance(outcome, Parsed):
            assert (row.disposition, row.reason_code, row.disposed_at) == ("pending", None, None)
            assert NormalizedPosting.model_validate(row.normalized) == outcome.normalized
        else:
            assert (row.disposition, row.reason_code, row.normalized) == ("out_of_scope", "not_germany", None)
            assert row.disposed_at is not None

    # the stored counts are the parse's: an item that left `pending` since (§7.16.1) still counts as pending
    exits = [
        ("linked", None, {"normalized": None}),
        ("deferred", "job_fenced", {}),  # normalized kept
        ("out_of_scope", "source_purged", {"normalized": None}),
        ("rejected", "identity_error", {"normalized": None}),
    ]
    left = [r.id for r in rows if r.disposition == "pending"][: len(exits)]
    async with engine.begin() as c:
        for raw_item_id, (to_state, reason, fields) in zip(left, exits, strict=True):
            await transition(c, "raw_items", raw_item_id, ["pending"], to_state, reason=reason, set_=fields)
    async with engine.connect() as c:
        assert dict((await c.execute(DISPOSITIONS, {"r": rr})).all()) == {
            "pending": 23,
            "linked": 1,
            "deferred": 1,
            "out_of_scope": 4,
            "rejected": 1,
        }
    async with engine.begin() as c:
        assert await parse_response(c, rr, adapter=arbeitnow_adapter, owner=owner) == counts


async def test_duplicate_in_page_and_rejected_items(engine, current_epoch, crafted_page) -> None:
    owner, rr = crafted_page  # element 3 repeats element 1's job_uid, element 4 (the fifth) has none
    instance = await jsearch_instance(engine)
    async with httpx.AsyncClient() as client:
        adapter = JSearchAdapter(client, api_key=b"fake", instance_id=instance.id, clock=utcnow)
        async with engine.begin() as c:
            counts = await parse_response(c, rr, adapter=adapter, owner=owner)
    async with engine.connect() as c:
        by = dict((await c.execute(DISPOSITIONS, {"r": rr})).all())
    item_count, parsed_at = await response_of(engine, rr)
    rows = await items_of(engine, rr)
    jobs = crafted_jsearch_jobs()
    assert counts == ParseCounts(pending=3, out_of_scope=0, duplicate_in_page=1, rejected=1)
    assert by == {"pending": 3, "duplicate_in_page": 1, "rejected": 1}
    assert sum(by.values()) == item_count == counts.total == 5 and parsed_at is not None
    assert [(r.item_index, r.disposition, r.reason_code) for r in rows] == [
        (0, "pending", None),
        (1, "pending", None),
        (2, "pending", None),
        (3, "duplicate_in_page", "first_index=1"),
        (4, "rejected", "item_contract"),
    ]
    assert rows[3].native_id == rows[1].native_id and rows[4].native_id is None
    assert [r.normalized is not None for r in rows] == [True, True, True, False, False]
    assert [bytes(r.item_sha256) for r in rows] == [sha256_of(job) for job in jobs]
    assert [r.disposed_at is not None for r in rows] == [False, False, False, True, True]


async def test_replay_rejected_reparses_from_the_stored_body(
    engine, current_epoch, crafted_page, adapters: Adapters
) -> None:
    owner, rr = crafted_page
    instance = await jsearch_instance(engine)
    async with engine.begin() as c:
        await parse_response(c, rr, adapter=adapters(instance), owner=owner)
    rows = await items_of(engine, rr)
    rejected = rows[4].id
    pending = [r.id for r in rows if r.disposition == "pending"]
    fetched_at = utcnow()
    async with httpx.AsyncClient() as client:
        tolerant = TolerantJSearch(client, api_key=b"fake", instance_id=instance.id, clock=utcnow)

        async def replay(ids: Sequence[int], *, adapter: SourceAdapter, now: datetime | None = None) -> int:
            async with engine.begin() as c:
                return await replay_rejected(
                    c, raw_item_ids=ids, now=now or utcnow(), adapters=lambda instance: adapter
                )

        # the adapter of today still rejects it: nothing moves; a pending item is no replay at all
        assert await replay([rejected, *pending], adapter=adapters(instance)) == 0
        assert (await item(engine, rejected)).disposition == "rejected"
        # a body older than 30 days is no longer replayed
        assert await replay([rejected], adapter=tolerant, now=fetched_at + timedelta(days=31)) == 0
        # the adapter now tolerates the missing id: rejected -> pending, normalized rebuilt from the body
        assert await replay([rejected], adapter=tolerant) == 1
        moved = await item(engine, rejected)
        expected = f"tolerated-{get_str(crafted_jsearch_jobs()[4], 'job_id')}"
        assert (moved.disposition, moved.reason_code, moved.native_id, moved.disposed_at) == (
            "pending",
            None,
            expected,
            None,
        )
        assert NormalizedPosting.model_validate(moved.normalized).native_id == expected
        assert await replay([rejected], adapter=tolerant) == 0  # pending now: nothing to replay

        # a purged body: nothing moves
        async with engine.begin() as c:
            await transition(
                c,
                "raw_items",
                rejected,
                ["pending"],
                "rejected",
                reason="identity_error",
                set_={"normalized": None},
            )
            await c.execute(
                sa.text("UPDATE raw_responses SET body_zstd = NULL, purged_at = now() WHERE id = :r"),
                {"r": rr},
            )
        assert await replay([rejected], adapter=tolerant) == 0
        assert (await item(engine, rejected)).disposition == "rejected"


async def test_superseded_owner_writes_nothing(
    engine, current_epoch, queue_app, arbeitnow_adapter, stored_arbeitnow_page
) -> None:
    owner, rr = stored_arbeitnow_page
    await supersede(engine, owner)  # begin_new_attempt: the run's next attempt owns it
    with pytest.raises(FencedOut):
        async with engine.begin() as c:
            await parse_response(c, rr, adapter=arbeitnow_adapter, owner=owner)
    assert await items_of(engine, rr) == [] and await response_of(engine, rr) == (30, None)


async def test_a_response_of_another_run_is_refused(
    engine, current_epoch, arbeitnow_adapter, stored_arbeitnow_page
) -> None:
    owner, rr = stored_arbeitnow_page
    async with engine.begin() as c:
        await finalize_run(c, owner, status="succeeded", reason=None, report={})
    other = await queued_run(engine)
    with pytest.raises(LookupError, match="not a response of run"):
        async with engine.begin() as c:
            await parse_response(c, rr, adapter=arbeitnow_adapter, owner=other)
    assert await items_of(engine, rr) == []


async def test_an_unreadable_item_array_rejects_every_element(
    engine, current_epoch, stored_arbeitnow_page
) -> None:
    owner, rr = stored_arbeitnow_page
    async with httpx.AsyncClient() as client:
        adapter = UnreadableArray(client, instance_id=1, clock=utcnow)
        async with engine.begin() as c:
            counts = await parse_response(c, rr, adapter=adapter, owner=owner)
    rows = await items_of(engine, rr)
    jobs = json.loads(load_fixture("arbeitnow", "page1")[2])["data"]
    assert counts == ParseCounts(pending=0, out_of_scope=0, duplicate_in_page=0, rejected=30)
    assert {(r.disposition, r.reason_code, r.native_id) for r in rows} == {
        ("rejected", "item_contract", None)
    }
    assert [bytes(r.item_sha256) for r in rows] == [sha256_of(job) for job in jobs]
    assert (await response_of(engine, rr))[0] == 30


@pytest.mark.parametrize("adapter_type", [Losing, Reordered])
async def test_a_parse_that_is_not_one_outcome_per_element_is_an_invariant_violation(
    engine, current_epoch, stored_arbeitnow_page, adapter_type: type[ArbeitnowAdapter]
) -> None:
    owner, rr = stored_arbeitnow_page
    async with httpx.AsyncClient() as client:
        adapter = adapter_type(client, instance_id=1, clock=utcnow)
        with pytest.raises(InvariantViolation, match="one outcome per element"):
            async with engine.begin() as c:
                await parse_response(c, rr, adapter=adapter, owner=owner)
    assert await items_of(engine, rr) == [] and (await response_of(engine, rr))[1] is None


async def test_the_node_parses_every_unparsed_response_of_the_run(
    engine, current_epoch, arbeitnow_adapter, respx_mock: respx.MockRouter, monkeypatch, adapters: Adapters
) -> None:
    owner = await queued_run(engine)
    feed = respx_mock.get(FEED_URL)
    responses = []
    for case in ("page1", "page2", "page_end", "page_truncated"):
        status, headers, body = load_fixture("arbeitnow", case)
        responses.append(httpx.Response(status, headers=headers, content=body))
    feed.side_effect = responses
    stored = [
        await served_response(
            engine,
            owner,
            arbeitnow_adapter,
            await arbeitnow_spec(engine, arbeitnow_adapter, n),
            billing="free",
            provider=None,
        )
        for n in (1, 2, 3, 4)
    ]
    first, second, end, truncated = stored
    assert (await response_of(engine, truncated))[
        0
    ] == 0  # a failed('contract') 200: parsed_at set when stored
    points: list[str] = []

    async def reached(name: str) -> None:
        points.append(name)

    monkeypatch.setattr(parse_node, "afault_point", reached)
    before = {d: raw_items_written("arbeitnow", d) for d in ("pending", "out_of_scope", "rejected")}
    node = parse_node.make_parse_normalize(make_deps(engine, adapters))
    update = await node(initial_state(owner.run_id, owner.attempt_no))
    first_rows, second_rows = await items_of(engine, first), await items_of(engine, second)
    pending = sum(r.disposition == "pending" for r in first_rows + second_rows)
    out_of_scope = sum(r.disposition == "out_of_scope" for r in first_rows + second_rows)
    assert len(first_rows) == len(second_rows) == 30 and pending + out_of_scope == 60
    assert update == {
        "counters": {
            "raw_items_pending": pending,
            "raw_items_out_of_scope": out_of_scope,
            "raw_items_duplicate_in_page": 0,
            "raw_items_rejected": 0,
        },
        "steps": ["parse_normalize"],
    }
    # the empty end of the feed is parsed too (no row); the failed page is never selected
    assert (await response_of(engine, end))[0] == 0 and (await response_of(engine, end))[1] is not None
    assert await items_of(engine, end) == [] and await items_of(engine, truncated) == []
    assert points == ["collect.before_parse"] + ["collect.parse.between_responses"] * 3
    assert raw_items_written("arbeitnow", "pending") == before["pending"] + pending
    assert raw_items_written("arbeitnow", "out_of_scope") == before["out_of_scope"] + out_of_scope
    assert raw_items_written("arbeitnow", "rejected") == before["rejected"]
    async with engine.connect() as c:
        assert await pending_items_of_run(c, owner.run_id) == sorted(
            r.id for r in first_rows + second_rows if r.disposition == "pending"
        )
        assert (await c.execute(RUN_ITEMS, {"run": owner.run_id})).scalar_one() == 60

    # a second call finds every response parsed: it writes and counts nothing
    points.clear()
    again = await node(initial_state(owner.run_id, owner.attempt_no))
    assert again == {
        "counters": {
            "raw_items_pending": 0,
            "raw_items_out_of_scope": 0,
            "raw_items_duplicate_in_page": 0,
            "raw_items_rejected": 0,
        },
        "steps": ["parse_normalize"],
    }
    assert (
        points == ["collect.before_parse"]
        and raw_items_written("arbeitnow", "pending") == before["pending"] + pending
    )
    async with engine.connect() as c:
        assert (await c.execute(RUN_ITEMS, {"run": owner.run_id})).scalar_one() == 60
