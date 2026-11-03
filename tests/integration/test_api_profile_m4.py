"""The profile reads of the M4 web UI (spec §12.6 step 2, §14.2, §15.7, §21.1, §21.3, §21.4), on
PostgreSQL.

- `GET /api/profile/drafts/{id}/rescore-estimate` gives the confirm modal's figures: the bulk budget and its
  share, the ads that fit one batch with their follow-ups, the rest and the pace they are scored online at,
  and the number of the profile version a confirm creates;
- `GET /api/profile` names the newest open draft (`open_draft`), the newest query proposal
  (`latest_proposal`, with the failure code of a failed one) and what one call of each profile node costs
  under the active model policy (`estimates`);
- `GET /api/profile/query-proposals/{id}` carries a failed proposal's `failure_code`: the code its newest
  operation failed with.

Drafts go through the real pipeline on the synthetic stream, proposals through the fake LLM transport
(`op_runner`); every name and datum is invented, and no call is paid.
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

import httpx
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db.seed import load_settings_v1
from jobhunter.llm import fake
from jobhunter.llm.budget import month_of
from jobhunter.llm.spend_ledger import current_params, ensure_month
from jobhunter.profile.estimate import node_call_usd
from jobhunter.settings.model import Settings
from jobhunter.types import JSONValue, get_object
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.collect import insert_jobs
from tests.support.eval import seed_adversarial
from tests.support.llm import save_budget
from tests.support.m3 import insert_id
from tests.support.profile import SYNTHETIC_CV, OpRunner, Upload, idem, make_ready_draft
from tests.support.settings import activate_profile, fresh_key

pytestmark = pytest.mark.integration

CLOCK: Final = "jobhunter.api.routes.profile.utcnow"
# The start of a month of 30 days: the bucket accrues $93.10 / 720 h = $0.129306 an hour.
NOW: Final = datetime(2026, 11, 2, 9, 30, tzinfo=UTC)
PROPOSALS: Final = "/api/profile/query-proposals"


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> datetime:
    monkeypatch.setattr(CLOCK, lambda: NOW)
    return NOW


async def profile(client: httpx.AsyncClient) -> dict[str, Any]:
    r = await client.get("/api/profile")
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


async def retained_revisions(engine: AsyncEngine, n: int) -> None:
    """`n` unmerged jobs, each with one revision that is current: the targets of a re-score."""
    job_ids = await insert_jobs(engine, n)
    async with engine.begin() as conn:
        await conn.execute(
            sa.text(
                "INSERT INTO job_revisions (job_id, revision_no, material_hash, llm_input, description_text, "
                "description_completeness, ad_language, embedding_status) "
                "SELECT j, 1, sha256(convert_to(j::text, 'UTF8')), "
                "jsonb_build_object('title', 'Python Developer'), 'Ein erfundenes Inserat.', 'full', 'de', "
                "'awaiting_embedding' FROM unnest(CAST(:ids AS bigint[])) AS j"
            ),
            {"ids": job_ids},
        )
        await conn.execute(
            sa.text(
                "UPDATE jobs j SET current_revision_id = r.id FROM job_revisions r "
                "WHERE r.job_id = j.id AND j.id = ANY(CAST(:ids AS bigint[]))"
            ),
            {"ids": job_ids},
        )


async def counted(engine: AsyncEngine, *, scoring: str, nonscoring: str) -> None:
    """The month of `NOW` with this spend counted."""
    async with engine.begin() as conn:
        await ensure_month(conn, month_of(NOW), await current_params(conn), NOW)
        await conn.execute(
            sa.text(
                "UPDATE llm_budget SET scoring_counted_usd = CAST(:s AS numeric), "
                "nonscoring_counted_usd = CAST(:n AS numeric), "
                "counted_usd = CAST(:s AS numeric) + CAST(:n AS numeric) WHERE month = :m"
            ),
            {"s": scoring, "n": nonscoring, "m": month_of(NOW)},
        )


async def propose(client: httpx.AsyncClient) -> int:
    r = await client.post(PROPOSALS, headers=idem(fresh_key("proposal")))
    assert r.status_code == 202, r.text
    return int(r.json()["proposal_id"])


async def proposal(client: httpx.AsyncClient, proposal_id: int) -> dict[str, Any]:
    r = await client.get(f"{PROPOSALS}/{proposal_id}")
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


async def an_operation(engine: AsyncEngine, proposal_id: int, *, status: str, code: str | None) -> None:
    """One more `propose_queries` operation of the proposal, newer than any before it, ended as given."""
    async with engine.begin() as conn:
        await insert_id(
            conn,
            "operations",
            {
                "kind": "propose_queries",
                "payload": {"proposal_id": proposal_id},
                "dedup_key": f"propose_queries:{proposal_id}",
                "status": status,
                "last_error_code": code,
                "finished_at": utcnow(),
            },
        )


def with_nodes(**nodes: JSONValue) -> Settings:
    """The seeded settings with `scoring.model_policy.nodes.<node> = value` for each of `nodes`."""
    payload = load_settings_v1()
    scoring = get_object(payload, "scoring")
    policy = get_object(scoring, "model_policy")
    policy["nodes"] = {**get_object(policy, "nodes"), **nodes}
    return Settings.model_validate(payload)


# --- the confirm modal's figures ----------------------------------------------------------------------------


async def test_estimate_has_the_modal_figures(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, op_runner: OpRunner, upload: Upload
) -> None:
    """§15.7's modal at §21.4's second case: $38.20 counted, $37.00 of it scoring, so the batch allocation is
    40% x ($93.10 - $37.00) = $22.44, which holds 209 items at $0.107221 each with their follow-ups. Of 400
    retained ads the other 191 are scored online at November's pace: $3.103344 a day over the central
    $0.057569 a target with its cache writes, 53 a day. One user version exists, so a confirm creates v2; the
    adversarial seed's synthetic versions are not counted."""
    await activate_profile(engine)
    await seed_adversarial(engine)
    draft_id = await make_ready_draft(logged_in_client, op_runner, upload)
    await retained_revisions(engine, 400)
    await counted(engine, scoring="37.00", nonscoring="1.20")
    r = await logged_in_client.get(f"/api/profile/drafts/{draft_id}/rescore-estimate")
    assert r.status_code == 200, r.text
    assert r.json() == {
        "targets": 400,
        "est_usd_online": "21.058400",  # 400 x $0.052646
        "est_usd_batch": "18.668400",  # 400 x $0.046671
        "month_spend": "38.200000",
        "cap": "100.00",
        "bulk_budget_usd": "22.440000",
        "bulk_share": "0.4",
        "batch_targets": 209,
        "online_targets": 191,
        "online_per_day": 53,
        "next_version_no": 2,
    }


async def test_the_estimate_of_a_small_rescore_has_no_batch(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, ready_draft: int
) -> None:
    """Below `batch_min_targets` (200) no batch is submitted: every ad is scored online; the first confirm
    creates v1."""
    await retained_revisions(engine, 150)
    body = (await logged_in_client.get(f"/api/profile/drafts/{ready_draft}/rescore-estimate")).json()
    figures = ("bulk_budget_usd", "batch_targets", "online_targets", "online_per_day", "next_version_no")
    assert {key: body[key] for key in figures} == {
        "bulk_budget_usd": "37.240000",  # 40% x $93.10
        "batch_targets": 0,
        "online_targets": 150,
        "online_per_day": 53,
        "next_version_no": 1,
    }


# --- GET /api/profile ---------------------------------------------------------------------------------------


async def test_profile_names_the_open_draft_and_the_latest_proposal(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, op_runner: OpRunner, upload: Upload
) -> None:
    body = await profile(logged_in_client)
    assert (body["open_draft"], body["latest_proposal"]) == (None, None)
    first = await upload(SYNTHETIC_CV)
    assert (await profile(logged_in_client))["open_draft"] == {"id": first, "status": "uploaded"}
    await op_runner.run_pending()
    assert (await profile(logged_in_client))["open_draft"] == {"id": first, "status": "text_review"}
    second = await upload(SYNTHETIC_CV)  # a new CV supersedes the draft in review
    assert (await profile(logged_in_client))["open_draft"] == {"id": second, "status": "uploaded"}
    await op_runner.run_pending()
    assert (await profile(logged_in_client))["open_draft"] == {"id": second, "status": "text_review"}
    discarded = await logged_in_client.post(f"/api/profile/drafts/{second}/discard")
    assert discarded.status_code == 204, discarded.text
    assert (await profile(logged_in_client))["open_draft"] is None  # a discarded draft is not open

    await activate_profile(engine)  # a confirmed draft of its own: not open either
    proposal_id = await propose(logged_in_client)
    body = await profile(logged_in_client)
    assert body["open_draft"] is None
    assert body["latest_proposal"] == {"id": proposal_id, "status": "running", "failure_code": None}
    await op_runner.run_pending()
    assert (await profile(logged_in_client))["latest_proposal"] == {
        "id": proposal_id,
        "status": "ready",
        "failure_code": None,
    }
    newer = await propose(logged_in_client)  # none is running: a new proposal, the latest
    assert newer > proposal_id
    assert (await profile(logged_in_client))["latest_proposal"] == {
        "id": newer,
        "status": "running",
        "failure_code": None,
    }


async def test_node_call_estimates_are_the_section_21_figures(logged_in_client: httpx.AsyncClient) -> None:
    """§21.3 under preset B: extract_profile on Opus, 5,000 input x $4 + 6,000 output x $20 per MTok = $0.14;
    propose_queries 3,500 x $4 + 4,000 x $20 = $0.094 ("≈ $0.09")."""
    assert (await profile(logged_in_client))["estimates"] == {
        "extract_profile_usd": "0.140000",
        "propose_queries_usd": "0.094000",
    }
    seeded = Settings.model_validate(load_settings_v1())
    assert seeded.scoring.model_policy.preset == "B"
    assert node_call_usd(seeded, "extract_profile") == Decimal("0.140000")
    # another model: Sonnet, 5,000 x $2 + 6,000 x $10 = $0.07
    sonnet = with_nodes(extract_profile={"model": "claude-sonnet-5-5", "effort": "medium"})
    assert sonnet.scoring.model_policy.preset == "custom"
    assert node_call_usd(sonnet, "extract_profile") == Decimal("0.070000")
    assert node_call_usd(sonnet, "propose_queries") == Decimal("0.094000")
    # Haiku counts 30% more tokens: 4,550 x $0.10 + 5,200 x $0.50 = $0.003055
    haiku = with_nodes(propose_queries={"model": "claude-haiku-5-5", "effort": "low"})
    assert node_call_usd(haiku, "propose_queries") == Decimal("0.003055")


# --- failure codes ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("code", ["budget_exhausted", "invalid_output", "refused"])
async def test_a_failed_proposal_has_its_failure_code(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, op_runner: OpRunner, code: str
) -> None:
    if code == "budget_exhausted":  # A = cap - buffer = $0.10, below the call's worst case
        await save_budget(engine, now=utcnow(), cap_usd="2.10")
    elif code == "invalid_output":
        fake.scenario("set_invalid")  # the repair is invalid too
    else:
        fake.scenario("refusal")
    await activate_profile(engine)
    proposal_id = await propose(logged_in_client)
    await op_runner.run_pending()
    assert await proposal(logged_in_client, proposal_id) == {
        "status": "failed",
        "items": None,
        "failure_code": code,
    }
    assert (await profile(logged_in_client))["latest_proposal"] == {
        "id": proposal_id,
        "status": "failed",
        "failure_code": code,
    }


async def test_the_failure_code_is_the_newest_operations(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, op_runner: OpRunner
) -> None:
    fake.scenario("refusal")
    await activate_profile(engine)
    proposal_id = await propose(logged_in_client)
    await op_runner.run_pending()
    assert (await proposal(logged_in_client, proposal_id))["failure_code"] == "refused"
    await an_operation(engine, proposal_id, status="failed", code="attempts_exhausted")
    assert (await proposal(logged_in_client, proposal_id))["failure_code"] == "attempts_exhausted"
    assert (await profile(logged_in_client))["latest_proposal"]["failure_code"] == "attempts_exhausted"


async def test_a_ready_proposal_has_no_failure_code(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, op_runner: OpRunner
) -> None:
    await activate_profile(engine)
    proposal_id = await propose(logged_in_client)
    await op_runner.run_pending()
    body = await proposal(logged_in_client, proposal_id)
    assert (body["status"], body["failure_code"]) == ("ready", None)
    # a failed operation of the key is the proposal's code only while the proposal is failed
    await an_operation(engine, proposal_id, status="failed", code="refused")
    assert (await proposal(logged_in_client, proposal_id))["failure_code"] is None
    assert (await profile(logged_in_client))["latest_proposal"] == {
        "id": proposal_id,
        "status": "ready",
        "failure_code": None,
    }


async def test_a_running_proposal_has_no_failure_code_after_a_transient_error(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, op_runner: OpRunner
) -> None:
    fake.scenario("overloaded_529")
    await activate_profile(engine)
    proposal_id = await propose(logged_in_client)
    await op_runner.run_pending()  # requeued with `llm_transient`, five minutes on
    async with engine.connect() as conn:
        code = (
            await conn.execute(
                sa.text("SELECT last_error_code FROM operations WHERE dedup_key = :k"),
                {"k": f"propose_queries:{proposal_id}"},
            )
        ).scalar_one()
    assert code == "llm_transient"
    assert await proposal(logged_in_client, proposal_id) == {
        "status": "running",
        "items": None,
        "failure_code": None,
    }
