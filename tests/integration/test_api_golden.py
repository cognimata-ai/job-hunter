"""Feedback and the golden cases over HTTP (spec §11.13 L4610-4632, §14.2 L4977-4979; plan Task 21, R2,
Deviation 21, Carry T19-d).

- `POST /api/jobs/{job_id}/feedback` (`{kind, label, comment?}`) → 201 `{feedback_id, golden_case_id}`
  (`golden.record_feedback`); 409 `conflict_state` without a published result, 404 for no such job, 422 for a
  comment over 2,000 characters or holding a NUL or a lone surrogate;
- `GET /api/golden-cases?cursor&limit&active` → `{items, next_cursor}`, keyset by id with §14.3's signed
  cursor, bound to the `active` filter;
- `PATCH /api/golden-cases/{id}` (`{active?, is_canary?}`) → 200 the case; at most two canaries (422);
- `DELETE /api/golden-cases/{id}` → 204, cascading to its eval rows; 409 `conflict_state` while a running eval
  run includes it (Carry T19-d: the cascade would remove an in-flight call row and leave its ledger entry
  `dispatched` for good);
- `GET /api/profile/drafts/{id}` of the adversarial seed's synthetic confirmed draft is 404 (Carry T19-d).

Every datum is invented.
"""

import json
from datetime import datetime
from typing import Any, Final

import httpx
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.llm.spend_ledger import EvalRunOwner, mark_dispatched, reserve
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.eval import Scored, insert_cases, scored_job, seed_adversarial
from tests.support.fixtures import count_rows
from tests.support.llm import ledger_row, reserve_kw
from tests.support.m3 import insert_id, insert_row

pytestmark = pytest.mark.integration

CASES_URL: Final = "/api/golden-cases"
JSON: Final = {"content-type": "application/json"}
SONNET: Final = "claude-sonnet-5-5"


@pytest.fixture
async def scored(engine: AsyncEngine) -> Scored:
    async with engine.begin() as conn:
        return await scored_job(conn)


def feedback_url(job_id: int) -> str:
    return f"/api/jobs/{job_id}/feedback"


async def case_ids(engine: AsyncEngine) -> list[int]:
    async with engine.connect() as conn:
        return [
            int(i) for i in (await conn.execute(sa.text("SELECT id FROM golden_cases ORDER BY id"))).scalars()
        ]


async def eval_run(engine: AsyncEngine, kind: str, *, status: str = "running") -> int:
    async with engine.begin() as conn:
        return await insert_id(
            conn,
            "eval_runs",
            {
                "kind": kind,
                "route": "production",
                "status": status,
                "repetitions": 2 if kind == "golden" else 1,
                "metrics": {"transport_requested": "fake"},
                "finished_at": None if status == "running" else utcnow(),
            },
        )


async def dispatched_call(engine: AsyncEngine, eval_run_id: int, case_id: int, *, now: datetime) -> int:
    """An eval case's generator call of the run, dispatched (in flight); its ledger entry's id."""
    owner = EvalRunOwner(eval_run_id)
    kw = reserve_kw(
        owner,
        now=now,
        bound=4000,
        max_tokens=1000,
        purpose="generate",
        model=SONNET,
        deadline_s=600,
        golden_case_id=case_id,
    )
    async with engine.begin() as conn:
        reservation = await reserve(conn, **kw)
        await mark_dispatched(conn, reservation, owner, now=now)
    return reservation.ledger_id


# ---- feedback ----------------------------------------------------------------------------------------------


async def test_feedback_records_the_label_and_upserts_the_golden_case(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, scored: Scored
) -> None:
    r = await logged_in_client.post(
        feedback_url(scored.job.job_id), json={"kind": "label", "label": "good", "comment": "passt gut"}
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body) == {"feedback_id", "golden_case_id"}
    async with engine.connect() as conn:
        case = (
            await conn.execute(
                sa.text("SELECT label, comment, source FROM golden_cases WHERE id = :g"),
                {"g": body["golden_case_id"]},
            )
        ).one()
    assert tuple(case) == ("good", "passt gut", "week1")
    again = await logged_in_client.post(
        feedback_url(scored.job.job_id), json={"kind": "wrong_score", "label": "poor"}
    )
    assert again.status_code == 201, again.text
    assert again.json()["golden_case_id"] == body["golden_case_id"]  # one case per (revision, profile)
    assert again.json()["feedback_id"] != body["feedback_id"]
    assert await count_rows(engine, "score_feedback") == 2


async def test_feedback_without_a_published_result_or_job_is_refused(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, scored: Scored
) -> None:
    (unscored,) = await insert_cases(engine, 1)  # a job of the synthetic ad with no result
    r = await logged_in_client.post(feedback_url(unscored.job_id), json={"kind": "label", "label": "ok"})
    assert (r.status_code, r.json()["code"]) == (409, "conflict_state"), r.text
    r = await logged_in_client.post(feedback_url(10**12), json={"kind": "label", "label": "ok"})
    assert (r.status_code, r.json()["code"]) == (404, "not_found"), r.text
    assert await count_rows(engine, "score_feedback") == 0


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "label", "label": "good", "comment": "pa\u0000sst"},
        {"kind": "label", "label": "good", "comment": "pa\ud800sst"},
        {"kind": "label", "label": "good", "comment": "x" * 2001},
        {"kind": "label", "label": "great"},
        {"kind": "like", "label": "good"},
        {"kind": "label", "label": "good", "note": "x"},
        {"label": "good"},
    ],
)
async def test_an_invalid_feedback_body_is_a_422_and_writes_nothing(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, scored: Scored, body: dict[str, Any]
) -> None:
    r = await logged_in_client.post(feedback_url(scored.job.job_id), content=json.dumps(body), headers=JSON)
    assert (r.status_code, r.json()["code"]) == (422, "validation_failed"), r.text
    assert await count_rows(engine, "score_feedback") == 0


async def test_a_comment_of_2000_characters_is_accepted(
    logged_in_client: httpx.AsyncClient, scored: Scored
) -> None:
    body = {"kind": "label", "label": "good", "comment": "ä" * 2000}
    r = await logged_in_client.post(feedback_url(scored.job.job_id), json=body)
    assert r.status_code == 201, r.text


# ---- the listing and the flags -----------------------------------------------------------------------------


async def test_the_listing_pages_by_id_with_a_cursor_bound_to_its_filter(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    made = await insert_cases(engine, 3)
    ids = [case.id for case in made]
    r = await logged_in_client.get(CASES_URL, params={"limit": 2})
    assert r.status_code == 200, r.text
    first = r.json()
    assert [i["id"] for i in first["items"]] == ids[:2] and first["next_cursor"] is not None
    item = first["items"][0]
    assert set(item) == {
        "id",
        "job_id",
        "label",
        "comment",
        "profile_version",
        "scope_version",
        "source",
        "active",
        "is_canary",
    }
    assert (item["job_id"], item["source"], item["active"], item["is_canary"]) == (
        made[0].job_id,
        "week1",
        True,
        False,
    )
    second = (
        await logged_in_client.get(CASES_URL, params={"limit": 2, "cursor": first["next_cursor"]})
    ).json()
    assert ([i["id"] for i in second["items"]], second["next_cursor"]) == (ids[2:], None)
    r = await logged_in_client.patch(f"{CASES_URL}/{ids[1]}", json={"active": False})
    assert (r.status_code, r.json()["id"], r.json()["active"]) == (200, ids[1], False), r.text
    inactive = (await logged_in_client.get(CASES_URL, params={"active": "false"})).json()
    assert [i["id"] for i in inactive["items"]] == [ids[1]]
    active = (await logged_in_client.get(CASES_URL, params={"active": "true", "limit": 1})).json()
    assert [i["id"] for i in active["items"]] == [ids[0]]
    r = await logged_in_client.get(CASES_URL, params={"active": "false", "cursor": active["next_cursor"]})
    assert (r.status_code, r.json()["code"]) == (400, "invalid_cursor")  # another filter's cursor
    r = await logged_in_client.get(CASES_URL, params={"limit": 101})
    assert (r.status_code, r.json()["code"]) == (422, "validation_failed")


async def test_at_most_two_cases_are_canaries(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    ids = [case.id for case in await insert_cases(engine, 3)]
    for case_id in ids[:2]:
        r = await logged_in_client.patch(f"{CASES_URL}/{case_id}", json={"is_canary": True})
        assert (r.status_code, r.json()["is_canary"]) == (200, True), r.text
    r = await logged_in_client.patch(f"{CASES_URL}/{ids[2]}", json={"is_canary": True})
    assert (r.status_code, r.json()["code"]) == (422, "validation_failed"), r.text
    r = await logged_in_client.patch(f"{CASES_URL}/{ids[0]}", json={"is_canary": False})
    assert (r.status_code, r.json()["is_canary"]) == (200, False)
    r = await logged_in_client.patch(f"{CASES_URL}/{ids[2]}", json={"is_canary": True})
    assert r.status_code == 200, r.text
    r = await logged_in_client.patch(f"{CASES_URL}/{10**12}", json={"active": True})
    assert (r.status_code, r.json()["code"]) == (404, "not_found")
    r = await logged_in_client.patch(f"{CASES_URL}/{ids[0]}", json={"active": "maybe"})
    assert (r.status_code, r.json()["code"]) == (422, "validation_failed")


# ---- the deletion ------------------------------------------------------------------------------------------


async def test_a_deletion_cascades_to_the_case_s_eval_rows(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    case_id, kept = (case.id for case in await insert_cases(engine, 2))
    run = await eval_run(engine, "golden", status="completed")
    async with engine.begin() as conn:
        for golden_case_id in (case_id, kept):
            await insert_row(
                conn,
                "eval_case_results",
                {
                    "eval_run_id": run,
                    "golden_case_id": golden_case_id,
                    "outcome": "filtered_out",
                    "critic_ran": False,
                    "llm_call_ids": [],
                    "output": {"filters": []},
                },
            )
    r = await logged_in_client.delete(f"{CASES_URL}/{case_id}")
    assert (r.status_code, r.content) == (204, b""), r.text
    assert await case_ids(engine) == [kept]
    assert await count_rows(engine, "eval_case_results") == 1  # the other case's row stays
    r = await logged_in_client.delete(f"{CASES_URL}/{case_id}")
    assert (r.status_code, r.json()["code"]) == (404, "not_found")


async def test_a_case_of_a_running_eval_run_cannot_be_deleted(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, eval_owner: EvalRunOwner
) -> None:
    """Carry T19-d: a running golden run includes every active case, a running canary run every active
    canary, and a run with a call naming the case includes it whatever its flags now; none of those can be
    deleted until the run ends. Another case stays deletable."""
    ids = [case.id for case in await insert_cases(engine, 4)]
    canary = await eval_run(engine, "canary")
    r = await logged_in_client.patch(f"{CASES_URL}/{ids[0]}", json={"is_canary": True})
    assert r.status_code == 200, r.text
    refused = await logged_in_client.delete(f"{CASES_URL}/{ids[0]}")
    assert (refused.status_code, refused.json()["code"]) == (409, "conflict_state"), refused.text
    assert (await logged_in_client.delete(f"{CASES_URL}/{ids[3]}")).status_code == 204  # no canary
    golden = await eval_run(engine, "golden")
    refused = await logged_in_client.delete(f"{CASES_URL}/{ids[1]}")
    assert (refused.status_code, refused.json()["code"]) == (409, "conflict_state"), refused.text
    ledger_id = await dispatched_call(engine, golden, ids[2], now=utcnow())
    r = await logged_in_client.patch(f"{CASES_URL}/{ids[2]}", json={"active": False})
    assert r.status_code == 200, r.text
    async with engine.begin() as conn:  # the canary run ends; the golden run, with its call, runs on
        await conn.execute(
            sa.text("UPDATE eval_runs SET status = 'completed', finished_at = now() WHERE id = :r"),
            {"r": canary},
        )
    refused = await logged_in_client.delete(
        f"{CASES_URL}/{ids[2]}"
    )  # inactive now, but its call is in flight
    assert (refused.status_code, refused.json()["code"]) == (409, "conflict_state"), refused.text
    assert (await ledger_row(engine, ledger_id)).state == "dispatched"
    assert await count_rows(engine, "llm_calls", golden_case_id=ids[2]) == 1
    assert await case_ids(engine) == ids[:3]
    async with engine.begin() as conn:
        await conn.execute(
            sa.text("UPDATE eval_runs SET status = 'failed', finished_at = now() WHERE id = :r"),
            {"r": golden},
        )
    assert (await logged_in_client.delete(f"{CASES_URL}/{ids[2]}")).status_code == 204
    assert (await logged_in_client.delete(f"{CASES_URL}/{ids[0]}")).status_code == 204


# ---- the synthetic seed ------------------------------------------------------------------------------------


async def test_the_synthetic_confirmed_draft_of_the_seed_is_not_found(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """Carry T19-d: the adversarial seed confirms a draft per synthetic profile; none is the user's, so the
    draft route knows none of them."""
    seeded = await seed_adversarial(engine)
    assert seeded
    async with engine.connect() as conn:
        drafts = (
            (
                await conn.execute(
                    sa.text(
                        "SELECT d.id FROM profile_drafts d JOIN profile_versions v "
                        "ON v.id = d.confirmed_profile_version_id WHERE v.synthetic_eval ORDER BY d.id"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert drafts
    for draft_id in drafts:
        r = await logged_in_client.get(f"/api/profile/drafts/{draft_id}")
        assert (r.status_code, r.json()["code"]) == (404, "not_found"), r.text
