"""A worker without the `anthropic_key` secret and not in fake mode (plan Task 23): its gateway is built "not
configured", and every call raises `LLMNotConfigured` before any reservation, so the two LLM nodes end their
row `failed('llm_not_configured')` through `llm_outcome`'s mapping, with no `llm_calls` and no ledger row.

The text phase and the confirmed profile come from the usual in-process runner (fake LLM, docparse loop);
only the gateway of the extraction and of the proposal is the keyless one, on a transport that fails the test
if anything is sent. The CV is the invented `SYNTHETIC_CV`.
"""

from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any, Final

import httpx
import httpx2
import pytest
import sqlalchemy as sa
from jobhunter.llm.cache_monitor import CacheMonitor
from jobhunter.llm.gateway import LLMGateway
from jobhunter.llm.transport import make_http_client
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.fixtures import ConfigFactory, count_rows
from tests.support.profile import SYNTHETIC_CV, OpRunner, Upload, draft_row, draft_row_api, idem
from tests.support.settings import activate_profile, fresh_key

pytestmark = pytest.mark.integration

LLM_NOT_CONFIGURED: Final = "llm_not_configured"


def _sends_nothing(request: Any) -> Any:
    raise AssertionError(f"a keyless gateway sent a request to {request.url}")


@pytest.fixture
async def keyless(
    op_runner: OpRunner, engine: AsyncEngine, make_config: ConfigFactory
) -> AsyncIterator[OpRunner]:
    """`op_runner` with a gateway that has no key and is not in fake mode, for every execution from now on."""
    cfg = make_config("worker")
    assert not cfg.fake_llm and "anthropic_key" not in cfg.secrets
    client = make_http_client(cfg, mount=httpx2.MockTransport(_sends_nothing))
    gateway = LLMGateway(engine, cfg, http_client=client, cache=CacheMonitor())
    assert gateway.configured is False
    op_runner.deps = replace(op_runner.deps, gateway=gateway)
    try:
        yield op_runner
    finally:
        await client.aclose()


async def llm_rows(engine: AsyncEngine, purpose: str) -> tuple[int, int]:
    """The `llm_calls` and the ledger rows of `purpose`."""
    return (
        await count_rows(engine, "llm_calls", purpose=purpose),
        await count_rows(engine, "llm_spend_ledger", purpose=purpose),
    )


async def operation(engine: AsyncEngine, kind: str) -> sa.Row[Any]:
    async with engine.connect() as conn:
        statement = sa.text("SELECT status, attempts, last_error_code FROM operations WHERE kind = :k")
        (row,) = (await conn.execute(statement, {"k": kind})).all()
        return row


async def test_an_extraction_without_a_key_fails_the_draft_llm_not_configured(
    engine: AsyncEngine, logged_in_client: httpx.AsyncClient, keyless: OpRunner, upload: Upload
) -> None:
    draft_id = await upload(SYNTHETIC_CV)
    await keyless.run_pending()  # the text phase needs no LLM
    approved = await logged_in_client.post(
        f"/api/profile/drafts/{draft_id}/text-review", headers=idem(fresh_key("approve"))
    )
    assert approved.status_code == 202, approved.text
    await keyless.run_pending()
    draft = await draft_row(engine, draft_id)
    assert (draft.status, draft.failure_code) == ("failed", LLM_NOT_CONFIGURED)
    op = await operation(engine, "extract_profile")
    assert (op.status, op.last_error_code, op.attempts) == ("failed", LLM_NOT_CONFIGURED, 0)
    assert await llm_rows(engine, "extract_profile") == (0, 0)
    assert await count_rows(engine, "llm_budget") == 0  # not even the month row: nothing was reserved
    # once a key is mounted, the same approved text is extracted again (§12.7)
    assert (await draft_row_api(logged_in_client, draft_id))["repairs"] == ["retry_llm"]


async def test_a_proposal_without_a_key_fails_llm_not_configured(
    engine: AsyncEngine, logged_in_client: httpx.AsyncClient, keyless: OpRunner
) -> None:
    await activate_profile(engine)
    months = await count_rows(engine, "llm_budget")  # the activation's bulk operation opened the month's row
    r = await logged_in_client.post("/api/profile/query-proposals", headers=idem(fresh_key("proposal")))
    assert r.status_code == 202, r.text
    proposal_id = int(r.json()["proposal_id"])
    await keyless.run_pending()
    async with engine.connect() as conn:
        status = await conn.execute(
            sa.text("SELECT status, items FROM query_proposals WHERE id = :p"), {"p": proposal_id}
        )
        assert tuple(status.one()) == ("failed", None)
    op = await operation(engine, "propose_queries")
    assert (op.status, op.last_error_code, op.attempts) == ("failed", LLM_NOT_CONFIGURED, 0)
    assert await llm_rows(engine, "propose_queries") == (0, 0)
    assert await count_rows(engine, "llm_budget") == months  # the refused proposal wrote none
