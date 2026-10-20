import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest
import sqlalchemy as sa
from jobhunter.sources.base import RawPage, RequestSpec, request_key
from jobhunter.sources.http import ERROR_BODY_BYTES
from jobhunter.sources.receipts import (
    RAW_BODY_TTL,
    fetch_attempts,
    insert_fetch_request,
    load_response,
    store_response,
)
from jobhunter.worker.runs import RunOwner
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.collect import jsearch_cell, page1_spec, queued_run

pytestmark = pytest.mark.integration
T0 = datetime(2026, 10, 18, 21, 0, tzinfo=UTC)
ROWS = sa.text(
    "SELECT id, body_zstd, body_ref_id, body_sha256, expires_at, fetched_at, headers, item_count, parsed_at, "
    "http_status FROM raw_responses"
)


async def planned(
    engine: AsyncEngine,
    owner: RunOwner,
    spec: RequestSpec,
    *,
    attempt_seq: int = 1,
    status: Literal["planned", "skipped"] = "planned",
    skip_reason: str | None = None,
) -> int:
    async with engine.begin() as c:
        return await insert_fetch_request(
            c,
            owner=owner,
            cell_id=spec.cell_id,
            source_instance_id=spec.source_instance_id,
            phase="primary",
            spec=spec,
            request_key=request_key(spec, owner.run_id),
            attempt_seq=attempt_seq,
            status=status,
            skip_reason=skip_reason,
        )


async def store(engine: AsyncEngine, fetch_request_id: int, page: RawPage, *, succeeded: bool) -> int:
    async with engine.begin() as c:
        return await store_response(
            c,
            fetch_request_id,
            page,
            source_instance_id=page.spec.source_instance_id,
            ledger_entry_id=None,
            succeeded=succeeded,
            items=10 if succeeded else None,
        )


async def test_store_and_load_roundtrip_with_dedup_and_error_truncation(engine, current_epoch) -> None:
    owner, cell = await queued_run(engine), await jsearch_cell(engine)
    spec = await page1_spec(engine, cell)
    specs = [replace(spec, page_no=n) for n in (1, 2, 3, 4)]  # four logical requests, four keys
    first_id, second_id, error_id, late_id = [await planned(engine, owner, s) for s in specs]
    body = bytes(range(256)) * 4096  # 1 MiB
    headers = {
        "content-type": "application/json",
        "x-ratelimit-requests-remaining": "198",
        "set-cookie": "session=1",  # never stored (§5.2)
        "x-rapidapi-key": "not-a-real-key",
    }
    first_at, second_at = T0, T0 + timedelta(hours=1)
    first = await store(engine, first_id, RawPage(specs[0], 200, headers, body, first_at), succeeded=True)
    second = await store(engine, second_id, RawPage(specs[1], 200, headers, body, second_at), succeeded=True)
    error_body = b"<html>" + b"x" * (20 * 1024)  # a 503 page of 20 KiB
    error_page = RawPage(
        specs[2], 503, {"content-type": "text/html", "retry-after": "5"}, error_body, second_at
    )
    error = await store(engine, error_id, error_page, succeeded=False)
    async with engine.connect() as c:
        rows = {r.id: r for r in (await c.execute(ROWS)).all()}
        linked = dict((await c.execute(sa.text("SELECT id, raw_response_id FROM fetch_requests"))).all())
        loaded = await load_response(c, second, specs[1])
        loaded_error = await load_response(c, error, specs[2])
    # the second body is the first one's: referenced, and the first's expiry raised to the second's
    assert rows[second].body_ref_id == first and rows[second].body_zstd is None
    assert rows[first].expires_at == second_at + RAW_BODY_TTL == rows[second].expires_at
    assert rows[first].body_sha256 == rows[second].body_sha256 == hashlib.sha256(body).digest()
    assert len(rows[first].body_zstd) < len(body) // 10  # zstd
    assert (loaded.spec, loaded.status, loaded.body, loaded.fetched_at) == (specs[1], 200, body, second_at)
    assert loaded.headers == {"content-type": "application/json", "x-ratelimit-requests-remaining": "198"}
    assert rows[first].headers == loaded.headers  # the allowlist only: no cookie, no key
    assert (rows[first].item_count, rows[first].parsed_at) == (10, None)  # parsed later (Task 15)
    assert len(loaded_error.body) == ERROR_BODY_BYTES == 4096 and loaded_error.body == error_body[:4096]
    assert (rows[error].http_status, rows[error].item_count, rows[error].parsed_at) == (503, 0, second_at)
    assert rows[error].headers == {"content-type": "text/html", "retry-after": "5"}
    assert linked == {first_id: first, second_id: second, error_id: error, late_id: None}

    # an expired body is never referenced: the same bytes after the first's expiry are stored again
    late_at = rows[first].expires_at + timedelta(seconds=1)
    late = await store(engine, late_id, RawPage(specs[3], 200, headers, body, late_at), succeeded=True)
    async with engine.connect() as c:
        row = (await c.execute(sa.text(f"{ROWS.text} WHERE id = :i"), {"i": late})).one()
    assert row.body_ref_id is None and row.body_zstd is not None and row.expires_at == late_at + RAW_BODY_TTL

    # a purged body is gone, also for the row that referenced it
    async with engine.begin() as c:
        await c.execute(
            sa.text("UPDATE raw_responses SET body_zstd = NULL, purged_at = now() WHERE id = :i"),
            {"i": first},
        )
    async with engine.connect() as c:
        for raw_response_id, page_spec in ((first, specs[0]), (second, specs[1])):
            with pytest.raises(LookupError):
                await load_response(c, raw_response_id, page_spec)
        assert (await load_response(c, late, specs[3])).body == body


async def test_fetch_attempts_are_a_key_s_attempts_in_attempt_seq_order(engine, current_epoch) -> None:
    owner, cell = await queued_run(engine), await jsearch_cell(engine)
    spec = await page1_spec(engine, cell)
    other = replace(spec, page_no=2)
    key = request_key(spec, owner.run_id)
    async with engine.connect() as c:
        assert await fetch_attempts(c, key) == []
    ids = {
        2: await planned(engine, owner, spec, attempt_seq=2, status="skipped", skip_reason="superseded"),
        3: await planned(engine, owner, spec, attempt_seq=3),
        1: await planned(engine, owner, spec, attempt_seq=1, status="skipped", skip_reason="shutdown"),
    }
    other_id = await planned(engine, owner, other, attempt_seq=5)
    async with engine.connect() as c:
        found = await fetch_attempts(c, key)
        (latest_other,) = await fetch_attempts(c, request_key(other, owner.run_id))
    # in attempt_seq order, whatever the insert order
    assert [(a.id, a.attempt_seq) for a in found] == [(ids[n], n) for n in (1, 2, 3)]
    latest = found[-1]  # the latest attempt, which the acquisition resolves (§6.2 rule 2)
    assert (latest.id, latest.attempt_seq, latest.status) == (ids[3], 3, "planned")
    assert (latest.run_id, latest.run_attempt_no) == (owner.run_id, owner.attempt_no)
    assert (latest.raw_response_id, latest.quota_ledger_id, latest.http_status) == (None, None, None)
    assert (latest_other.id, latest_other.attempt_seq) == (other_id, 5)  # keys never mix
