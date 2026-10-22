"""A structured output that holds a NUL character through the two profile graphs' LLM nodes (final review
FR-1 (d)): the gateway finds it unstorable before the settlement writes `output_json`, so each try settles
`billed` with its usage, `output_json` NULL and `invalid_output`; the node's one repair follows, and the
second invalid output fails the row `invalid_output`. Nothing is left `dispatched` or `ambiguous`, and no
later attempt pays again.

The fake serves streams of this module's own root (`fake_llm_root`): the committed recordings, with the
`extract_profile` and `propose_queries` streams rebuilt by `write_synthetic_recording` from the Task 13 and
Task 20 bodies with a NUL in one string, labelled synthetic. Every datum is invented; no paid call.
"""

import shutil
from pathlib import Path
from typing import Any, Final

import httpx
import pytest
import sqlalchemy as sa
from jobhunter.llm import fake
from jobhunter.llm.schemas.profile import ProfileOut
from jobhunter.llm.schemas.queries import QueryProposalOut
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.fixtures import count_rows
from tests.support.llm import FAKE_LLM_DIR, ledger_states, tries, write_synthetic_recording
from tests.support.profile import (
    EXTRACT_MODEL,
    EXTRACT_PURPOSE,
    PROPOSE_MODEL,
    PROPOSE_PURPOSE,
    QUERIES_USAGE,
    SYNTHETIC_CV,
    SYNTHETIC_PROFILE,
    SYNTHETIC_QUERIES,
    SYNTHETIC_USAGE,
    OpRunner,
    Upload,
    approve_text,
    draft_row,
    idem,
)
from tests.support.settings import activate_profile, fresh_key

pytestmark = pytest.mark.integration

NUL: Final = "\u0000"
NUL_PROFILE: Final[dict[str, Any]] = {**SYNTHETIC_PROFILE, "summary": f"Backend{NUL}Entwicklerin"}
NUL_QUERIES: Final[dict[str, Any]] = {
    "queries": [
        {**SYNTHETIC_QUERIES["queries"][0], "text": f"Backend{NUL}Developer"},
        *SYNTHETIC_QUERIES["queries"][1:],
    ]
}


@pytest.fixture
def fake_llm_root(tmp_path: Path) -> Path:
    """The committed recordings, with both profile streams holding a NUL (module docstring)."""
    root = tmp_path / "anthropic"
    shutil.copytree(FAKE_LLM_DIR, root)
    for purpose, model, schema, body, usage in (
        (EXTRACT_PURPOSE, EXTRACT_MODEL, ProfileOut, NUL_PROFILE, SYNTHETIC_USAGE),
        (PROPOSE_PURPOSE, PROPOSE_MODEL, QueryProposalOut, NUL_QUERIES, QUERIES_USAGE),
    ):
        write_synthetic_recording(root, purpose, model, schema, body, case="fr1d_nul_output", usage=usage)
    return root


def billed_invalid(usage: dict[str, int]) -> list[tuple[Any, ...]]:
    """The two tries of a call and its one repair, each billed `invalid_output` with its usage."""
    return [
        (n, "completed", "invalid_output", "billed", None, usage["input_tokens"], usage["output_tokens"])
        for n in (1, 2)
    ]


async def calls(engine: AsyncEngine) -> list[tuple[Any, ...]]:
    return [
        (r.logical_call_no, r.status, r.error_code, r.state, r.output_json, r.input_tokens, r.output_tokens)
        for r in await tries(engine)
    ]


async def test_an_extraction_holding_a_nul_is_repaired_once_and_every_try_settles_billed(
    engine: AsyncEngine, op_runner: OpRunner, upload: Upload
) -> None:
    draft_id = await upload(SYNTHETIC_CV)
    await op_runner.run_pending()
    op_id = await approve_text(engine, draft_id)
    await op_runner.run_pending()
    draft = await draft_row(engine, draft_id)
    assert (draft.status, draft.failure_code, draft.draft_json) == ("failed", "invalid_output", None)
    assert await calls(engine) == billed_invalid(SYNTHETIC_USAGE)
    assert await ledger_states(engine, op_id) == ["billed", "billed"]  # nothing dispatched or ambiguous
    assert fake.state().served == 2  # the call and its one repair
    async with engine.connect() as conn:
        status, code = (
            await conn.execute(
                sa.text("SELECT status, last_error_code FROM operations WHERE id = :i"), {"i": op_id}
            )
        ).one()
    assert (status, code) == ("failed", "invalid_output")


async def test_a_proposal_holding_a_nul_is_repaired_once_and_every_try_settles_billed(
    engine: AsyncEngine, op_runner: OpRunner, logged_in_client: httpx.AsyncClient
) -> None:
    await activate_profile(engine)
    r = await logged_in_client.post("/api/profile/query-proposals", headers=idem(fresh_key("proposal")))
    assert r.status_code == 202, r.text
    proposal_id = int(r.json()["proposal_id"])
    await op_runner.run_pending()
    async with engine.connect() as conn:
        status, items = (
            await conn.execute(
                sa.text("SELECT status, items FROM query_proposals WHERE id = :p"), {"p": proposal_id}
            )
        ).one()
    assert (status, items) == ("failed", None)
    assert await calls(engine) == billed_invalid(QUERIES_USAGE)
    assert fake.state().served == 2
    assert await count_rows(engine, "llm_spend_ledger") == 2
