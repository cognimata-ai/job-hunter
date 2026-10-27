"""The profile confirm and activation (spec §12.6 step 2, §9.1, §14.2, §6.9.3; plan Task 21, Deviations 2,
10, 14, 19, 36 and 40; rulings R21-1 to R21-5): `POST /api/profile/drafts/{id}/confirm`,
`GET /api/profile/drafts/{id}/rescore-estimate` and `GET /api/profile`, on PostgreSQL.

Every draft is the synthetic CV through the real pipeline (`make_ready_draft`: the paste, the text phase, the
approval and the extraction on the synthetic stream), its profile edited through `PUT /drafts/{id}` where a
case needs it; the operations run in process (`op_runner`). Every name and datum is invented; no real CV and
no paid call. The route's clock is fixed (`jobhunter.api.routes.profile.utcnow`).
"""

import asyncio
import contextlib
import copy
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

import httpx
import pytest
import sqlalchemy as sa
from jobhunter import api_idempotency
from jobhunter.api.routes import profile as profile_routes
from jobhunter.db.seed import content_hash, load_settings_v1
from jobhunter.graphs.collect.graph import build_collect_graph
from jobhunter.graphs.collect.nodes.enqueue_targets import ENQUEUE_TARGETS
from jobhunter.llm import budget
from jobhunter.llm.schemas.profile import ConfirmedProfile
from jobhunter.observability.alerts import raise_alert
from jobhunter.profile import activation
from jobhunter.profile.estimate import central_batch_cost_per_target
from jobhunter.settings import scope_apply
from jobhunter.settings.derive import derived_seniority
from jobhunter.settings.llm_projection import central_cost_per_target
from jobhunter.settings.model import Settings
from jobhunter.worker import runs
from jobhunter.worker.tasks import defer_in_tx
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.collect import insert_revisions_awaiting, manual_run_id, seed_inputs
from tests.support.faults import PausePoint
from tests.support.fixtures import count_rows
from tests.support.profile import (
    SYNTHETIC_CV,
    OpRunner,
    ProfileEdit,
    active_profile,
    draft_row,
    draft_row_api,
    idem,
    make_ready_draft,
    ranking_generation,
)
from tests.support.settings import (
    active_scope_id,
    fresh_key,
    operation_end,
    pending_scope_id,
    procrastinate_job_exists_for,
    save,
    settings_body,
)

pytestmark = pytest.mark.integration

NOW: Final = datetime(2026, 10, 22, 9, 30, tzinfo=UTC)
CLOCK: Final = "jobhunter.api.routes.profile.utcnow"
# The seeded settings at the start of a month: 0.4 x ($98.00 x 0.95 - $0 scoring counted) (§6.9.3, §21.4).
BULK_BUDGET: Final = "37.240000"
SECRET_EMAIL: Final = "max.mustermann@example.org"
TUPLE_POINTERS: Final = (
    "SELECT rubric_version_id, prompt_schema_version_id, model_policy_id FROM active_config WHERE id = 1"
)


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> datetime:
    monkeypatch.setattr(CLOCK, lambda: NOW)
    return NOW


# --- helpers ------------------------------------------------------------------------------------------------


def url(draft_id: int) -> str:
    return f"/api/profile/drafts/{draft_id}/confirm"


async def confirm(
    client: httpx.AsyncClient, draft_id: int, *, ack: bool = True, key: str | None = None
) -> httpx.Response:
    return await client.post(
        url(draft_id), json={"expected_rescore_ack": ack}, headers=idem(key or fresh_key("confirm"))
    )


async def confirmed(client: httpx.AsyncClient, draft_id: int, **kwargs: Any) -> dict[str, Any]:
    """`POST …/confirm` (201 required): its body."""
    r = await confirm(client, draft_id, **kwargs)
    assert r.status_code == 201, (r.status_code, r.text)
    body: dict[str, Any] = r.json()
    return body


def problem(r: httpx.Response) -> tuple[int, str]:
    return r.status_code, str(r.json()["code"])


def errors_of(r: httpx.Response) -> list[tuple[str, str]]:
    return [(e["field"], e["code"]) for e in r.json()["errors"]]


async def rows(engine: AsyncEngine, statement: str, **params: object) -> list[sa.Row[Any]]:
    async with engine.connect() as conn:
        return list((await conn.execute(sa.text(statement), params)).all())


async def scalar(engine: AsyncEngine, statement: str, **params: object) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(sa.text(statement), params)).scalar_one()


async def operations_of(engine: AsyncEngine, kind: str) -> list[sa.Row[Any]]:
    return await rows(
        engine, "SELECT id, status, payload, dedup_key FROM operations WHERE kind = :k ORDER BY id", k=kind
    )


async def written_counts(engine: AsyncEngine) -> tuple[int, ...]:
    """What a refused confirm must leave as it was."""
    return (
        await count_rows(engine, "profile_versions"),
        await count_rows(engine, "llm_tuples"),
        await count_rows(engine, "operations", kind=("embed_profile", "propose_queries", "apply_scope")),
        await count_rows(engine, "query_proposals"),
        await count_rows(engine, "scope_versions"),
        await count_rows(engine, "api_idempotency", route="profile.confirm"),
        await ranking_generation(engine),
    )


async def retained_revisions(engine: AsyncEngine, n: int) -> None:
    """`n` unmerged jobs whose one revision is current: the targets of a re-score."""
    await insert_revisions_awaiting(engine, n)
    async with engine.begin() as conn:
        await conn.execute(
            sa.text("UPDATE jobs j SET current_revision_id = r.id FROM job_revisions r WHERE r.job_id = j.id")
        )


def with_seniority(level: str) -> ProfileEdit:
    def edit(draft: dict[str, Any]) -> dict[str, Any]:
        return {**draft, "seniority": level}

    return edit


def with_user_entered(**user_entered: Any) -> ProfileEdit:
    def edit(draft: dict[str, Any]) -> dict[str, Any]:
        return {**draft, "user_entered": {**draft["user_entered"], **user_entered}}

    return edit


# --- the activation -----------------------------------------------------------------------------------------


async def test_confirm_activates_the_draft_in_one_transaction(
    logged_in_client, engine, ready_draft, log_events
) -> None:
    generation_before = await ranking_generation(engine)
    draft_before = await draft_row(engine, ready_draft)
    body = await confirmed(logged_in_client, ready_draft, ack=False)  # no retained ads: no ack needed
    version_id = body["profile_version_id"]
    assert body == {
        "profile_version_id": version_id,
        "targets_to_create": 0,
        "est_usd": "0.000000",
        "bulk_budget_usd": BULK_BUDGET,
    }
    (version,) = await rows(engine, "SELECT * FROM profile_versions")
    assert (version.id, version.version_no, version.draft_id, version.cv_file_id, version.purged_at) == (
        version_id,
        1,
        ready_draft,
        draft_before.cv_file_id,
        None,
    )
    # the draft's profile, validated again (it was stored repaired) with the user-entered defaults, and the
    # hash of its canonical JSON
    assert version.profile_json == ConfirmedProfile.model_validate(draft_before.draft_json).model_dump(
        mode="json"
    )
    assert bytes(version.profile_hash) == content_hash(version.profile_json)
    assert version.confirmed_at == NOW
    (tuple_row,) = await rows(engine, "SELECT * FROM llm_tuples")
    (pointers,) = await rows(engine, TUPLE_POINTERS)
    assert (
        tuple_row.profile_version_id,
        tuple_row.rubric_version_id,
        tuple_row.prompt_schema_version_id,
        tuple_row.model_policy_id,
    ) == (version_id, pointers.rubric_version_id, pointers.prompt_schema_version_id, pointers.model_policy_id)
    assert await active_profile(engine) == (version_id, tuple_row.id)
    assert await ranking_generation(engine) == generation_before + 1
    # (h): the vectors and a query proposal, each with its job deferred in the transaction
    (embed,) = await operations_of(engine, "embed_profile")
    assert (embed.status, embed.payload) == ("pending", {"profile_version_id": version_id})
    (propose,) = await operations_of(engine, "propose_queries")
    (proposal,) = await rows(engine, "SELECT id, profile_version_id, status FROM query_proposals")
    assert (proposal.profile_version_id, proposal.status) == (version_id, "running")
    assert propose.payload == {"proposal_id": proposal.id, "scope_version_id": await active_scope_id(engine)}
    assert await procrastinate_job_exists_for(engine, embed.id)
    assert await procrastinate_job_exists_for(engine, propose.id)
    # (i): the draft and its stored response
    draft = await draft_row(engine, ready_draft)
    assert (draft.status, draft.confirmed_profile_version_id, draft.confirm_response) == (
        "confirmed",
        version_id,
        body,
    )
    (event,) = [e for e in log_events() if e["event"] == "profile_confirmed"]
    assert {k: v for k, v in event.items() if k not in {"level", "logger", "ts", "request_id"}} == {
        "event": "profile_confirmed",
        "draft_id": ready_draft,
        "version_id": version_id,
        "version_created": True,
        "llm_tuple_id": tuple_row.id,
        "apply_scope_operation_id": (await operations_of(engine, "apply_scope"))[0].id,
        "embed_operation_id": embed.id,
        "proposal_id": proposal.id,
        "targets_to_create": 0,
    }


async def test_the_ranking_bump_is_the_confirms_last_statement(engine, ready_draft) -> None:
    """R21-1: `ranking_state` is the last lock of §9.1, so the bump comes after the scope (g), the operations
    (h) and the draft (i), not at the flip (c)."""
    statements: list[str] = []

    def record(*args: Any) -> None:  # before_cursor_execute(conn, cursor, statement, parameters, ...)
        statements.append(" ".join(str(args[2]).split()))

    async with engine.begin() as conn:
        await conn.execute(sa.text("SELECT 1 FROM active_config WHERE id = 1 FOR UPDATE"))  # the route's lock
        sa.event.listen(conn.sync_connection, "before_cursor_execute", record)
        status, _ = await activation.confirm_draft(
            conn, ready_draft, expected_rescore_ack=True, now=NOW, defer=defer_in_tx
        )
    assert status == 201

    def first(prefix: str) -> int:
        return next(i for i, statement in enumerate(statements) if statement.startswith(prefix))

    flip = first("UPDATE active_config SET profile_version_id")
    assert flip < first("UPDATE active_config SET pending_scope_version_id")  # (c) before (g)
    assert first("INSERT INTO operations") < first("UPDATE profile_drafts")  # (h) before (i)
    assert statements[-1].startswith("UPDATE ranking_state")
    assert sum(statement.startswith("UPDATE ranking_state") for statement in statements) == 1


async def test_a_repeated_confirm_returns_the_original_body_even_with_a_newer_profile_active(
    logged_in_client, engine, op_runner, upload, log_events
) -> None:
    """Deviation 2: a confirmed draft answers its stored body to any key, and nothing is written again."""
    first_draft = await make_ready_draft(logged_in_client, op_runner, upload)
    first = await confirmed(logged_in_client, first_draft)
    second_draft = await make_ready_draft(logged_in_client, op_runner, upload, edit=with_seniority("junior"))
    second = await confirmed(logged_in_client, second_draft)
    assert second["profile_version_id"] != first["profile_version_id"]
    before = await written_counts(engine)
    again = await confirmed(logged_in_client, first_draft, key="confirm-again-key-0001")
    assert again == first
    assert await written_counts(engine) == (*before[:5], before[5] + 1, before[6])  # only the key's row
    assert (await active_profile(engine))[0] == second["profile_version_id"]
    (replayed,) = [e for e in log_events() if e["event"] == "profile_confirm_replayed"]
    assert {k: v for k, v in replayed.items() if k not in {"level", "logger", "ts", "request_id"}} == {
        "event": "profile_confirm_replayed",
        "draft_id": first_draft,
        "version_id": first["profile_version_id"],
    }
    # a replay under the same key never reaches the handler: `run_idempotent` logs it
    assert await confirmed(logged_in_client, first_draft, key="confirm-again-key-0001") == first
    assert len([e for e in log_events() if e["event"] == "profile_confirm_replayed"]) == 1
    assert [e["route"] for e in log_events() if e["event"] == "idempotent_replay"] == ["profile.confirm"]


async def test_two_concurrent_confirms_activate_once(
    logged_in_client, engine, ready_draft, monkeypatch
) -> None:
    """The route takes `active_config` `FOR UPDATE` first, never `FOR SHARE`: the second confirm waits for the
    first's commit and answers the stored body (Deviation 2). Each request is held after the route's lock
    until both have it or a second has passed: two shared locks would both be held there, and the confirms
    would deadlock on the upgrade."""
    arrived: list[int] = []
    both = asyncio.Event()

    async def gated(conn: Any, **kwargs: Any) -> Any:
        arrived.append(1)
        if len(arrived) == 2:
            both.set()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(both.wait(), 1)
        return await api_idempotency.run_idempotent(conn, **kwargs)

    monkeypatch.setattr(profile_routes, "run_idempotent", gated)
    generation_before = await ranking_generation(engine)
    first, second = await asyncio.gather(
        confirm(logged_in_client, ready_draft, key="concurrent-key-0001"),
        confirm(logged_in_client, ready_draft, key="concurrent-key-0002"),
    )
    assert len(arrived) == 2
    assert first.status_code == second.status_code == 201 and first.json() == second.json()
    assert await count_rows(engine, "profile_versions") == 1
    assert await ranking_generation(engine) == generation_before + 1


async def test_a_second_draft_with_identical_content_reuses_the_version_and_the_tuple(
    logged_in_client, engine, op_runner, upload
) -> None:
    first_draft = await make_ready_draft(logged_in_client, op_runner, upload)
    first = await confirmed(logged_in_client, first_draft)
    active_before = await active_profile(engine)
    second_draft = await make_ready_draft(logged_in_client, op_runner, upload)  # runs the first's scope apply
    generation_before = await ranking_generation(engine)
    second = await confirmed(logged_in_client, second_draft)
    assert second == first
    assert await count_rows(engine, "profile_versions") == 1 and await count_rows(engine, "llm_tuples") == 1
    assert await active_profile(engine) == active_before
    assert await ranking_generation(engine) == generation_before + 1  # every confirm bumps (R21-4)
    assert (await draft_row(engine, second_draft)).confirmed_profile_version_id == first["profile_version_id"]
    # the live operations of the version are reused, not doubled
    assert len(await operations_of(engine, "embed_profile")) == 1
    assert await count_rows(engine, "query_proposals") == 1


async def test_a_reconfirm_of_a_version_with_a_ready_proposal_requests_none(
    logged_in_client, engine, op_runner, upload
) -> None:
    """R21-5: an identical re-confirm makes no paid call for a proposal the version already has."""
    first = await confirmed(logged_in_client, await make_ready_draft(logged_in_client, op_runner, upload))
    (propose,) = await operations_of(engine, "propose_queries")
    await op_runner.run_one(propose.id)
    (proposal,) = await rows(engine, "SELECT status FROM query_proposals")
    assert proposal.status == "ready"
    second = await confirmed(logged_in_client, await make_ready_draft(logged_in_client, op_runner, upload))
    assert second["profile_version_id"] == first["profile_version_id"]
    assert await count_rows(engine, "query_proposals") == 1
    assert len(await operations_of(engine, "propose_queries")) == 1


async def test_a_purged_version_with_that_hash_is_not_reused(
    logged_in_client, engine, op_runner, upload
) -> None:
    first = await confirmed(logged_in_client, await make_ready_draft(logged_in_client, op_runner, upload))
    async with engine.begin() as conn:  # as §20.2's profile purge leaves the version
        await conn.execute(
            sa.text("UPDATE profile_versions SET profile_json = NULL, purged_at = now() WHERE id = :v"),
            {"v": first["profile_version_id"]},
        )
    second = await confirmed(logged_in_client, await make_ready_draft(logged_in_client, op_runner, upload))
    assert second["profile_version_id"] != first["profile_version_id"]
    versions = await rows(engine, "SELECT id, version_no, profile_hash FROM profile_versions ORDER BY id")
    assert [(v.id, v.version_no) for v in versions] == [
        (first["profile_version_id"], 1),
        (second["profile_version_id"], 2),
    ]
    assert versions[0].profile_hash == versions[1].profile_hash  # the partial unique index allows it
    assert await count_rows(engine, "llm_tuples") == 2
    assert (await active_profile(engine))[0] == second["profile_version_id"]


async def test_an_active_profile_fence_on_the_matching_version_is_409_until_released(
    logged_in_client, engine, op_runner, upload
) -> None:
    first = await confirmed(logged_in_client, await make_ready_draft(logged_in_client, op_runner, upload))
    async with engine.begin() as conn:
        fence = (
            await conn.execute(
                sa.text(
                    "INSERT INTO purge_fences (kind, profile_version_id, manifest_id) "
                    "VALUES ('profile', :v, 1) RETURNING id"
                ),
                {"v": first["profile_version_id"]},
            )
        ).scalar_one()
    draft_id = await make_ready_draft(logged_in_client, op_runner, upload)
    before = await written_counts(engine)
    r = await confirm(logged_in_client, draft_id, key="fenced-confirm-key-01")
    assert problem(r) == (409, "profile_purge_in_progress")
    assert await written_counts(engine) == before and (await draft_row(engine, draft_id)).status == "ready"
    async with engine.begin() as conn:
        await conn.execute(sa.text("UPDATE purge_fences SET released_at = now() WHERE id = :f"), {"f": fence})
    # a refusal stores nothing: the same key confirms once the purge has finished
    again = await confirmed(logged_in_client, draft_id, key="fenced-confirm-key-01")
    assert again["profile_version_id"] == first["profile_version_id"]


async def test_a_fence_on_another_version_does_not_refuse(
    logged_in_client, engine, op_runner, upload
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            sa.text(
                "INSERT INTO purge_fences (kind, profile_version_id, manifest_id) "
                "VALUES ('profile', 999999, 1)"
            )
        )
    await confirmed(logged_in_client, await make_ready_draft(logged_in_client, op_runner, upload))


# --- the refusals -------------------------------------------------------------------------------------------


async def test_a_limit_issue_is_422_and_an_ungrounded_issue_does_not_block(
    logged_in_client, engine, op_runner, upload
) -> None:
    clearances = [{"name": f"Freigabe {i}", "level": None} for i in range(11)]
    draft_id = await make_ready_draft(
        logged_in_client, op_runner, upload, edit=with_user_entered(security_clearances=clearances)
    )
    before = await written_counts(engine)
    r = await confirm(logged_in_client, draft_id)
    assert problem(r) == (422, "validation_failed")
    assert errors_of(r) == [("user_entered.security_clearances", "limit")]
    assert await written_counts(engine) == before and (await draft_row(engine, draft_id)).status == "ready"

    def ungrounded(draft: dict[str, Any]) -> dict[str, Any]:
        skill = {"name": "Rust", "category": "programming_language", "level": "basic", "years": 1}
        return {
            **draft,
            "skills": [*draft["skills"], {**skill, "evidence": "Rust (1 Jahr)"}],
            "user_entered": {**draft["user_entered"], "security_clearances": []},
        }

    draft = (await draft_row_api(logged_in_client, draft_id))["draft"]
    saved = await logged_in_client.put(f"/api/profile/drafts/{draft_id}", json={"draft": ungrounded(draft)})
    assert ("skills[6].evidence", "ungrounded") in [
        (i["path"], i["code"]) for i in saved.json()["validation_issues"]
    ]
    await confirmed(logged_in_client, draft_id)


async def test_a_date_issue_blocks_the_confirm(logged_in_client, engine, op_runner, upload) -> None:
    def end_before_start(draft: dict[str, Any]) -> dict[str, Any]:
        roles = copy.deepcopy(draft["roles"])
        roles[1]["end"] = "2015-01-01"  # before its start, 2016-01-01
        return {**draft, "roles": roles}

    draft_id = await make_ready_draft(logged_in_client, op_runner, upload, edit=end_before_start)
    r = await confirm(logged_in_client, draft_id)
    assert problem(r) == (422, "validation_failed") and ("roles[1].end", "date_order") in errors_of(r)


async def test_a_contact_in_user_entered_is_422_on_its_field(
    logged_in_client, engine, op_runner, upload, log_events
) -> None:
    """Deviation 36: `validate_user_entered` neither scans nor repairs the user's own values, so the confirm
    refuses a contact in them: `profile_versions.profile_json` stays contact-stripped."""
    clearance = {"name": "Sicherheitsfreigabe Ü2", "level": f"Rückfragen an {SECRET_EMAIL}"}
    draft_id = await make_ready_draft(
        logged_in_client, op_runner, upload, edit=with_user_entered(security_clearances=[clearance])
    )
    before = await written_counts(engine)
    r = await confirm(logged_in_client, draft_id)
    assert problem(r) == (422, "validation_failed")
    assert errors_of(r) == [("user_entered.security_clearances[0].level", "contact_found")]
    assert SECRET_EMAIL not in r.text and await written_counts(engine) == before
    assert not any(SECRET_EMAIL in str(e) for e in log_events())
    draft = (await draft_row_api(logged_in_client, draft_id))["draft"]
    clean = with_user_entered(security_clearances=[{"name": "Sicherheitsfreigabe Ü2", "level": None}])
    assert (
        await logged_in_client.put(f"/api/profile/drafts/{draft_id}", json={"draft": clean(draft)})
    ).status_code == 200
    await confirmed(logged_in_client, draft_id)
    (version,) = await rows(engine, "SELECT profile_json FROM profile_versions")
    assert version.profile_json["user_entered"]["security_clearances"] == [
        {"name": "Sicherheitsfreigabe Ü2", "level": None}
    ]


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param({"foo": 1}, id="unknown-member"),
        pytest.param([], id="list"),
        pytest.param("text", id="string"),
        pytest.param(None, id="invalid-seniority"),  # the extracted profile with `seniority` out of its enum
    ],
)
async def test_a_stored_profile_that_fails_the_schema_is_409_and_writes_nothing(
    logged_in_client, engine, ready_draft, log_events, stored: Any
) -> None:
    if stored is None:
        stored = {**(await draft_row(engine, ready_draft)).draft_json, "seniority": "grandmaster"}
    async with engine.begin() as conn:
        await conn.execute(
            sa.text("UPDATE profile_drafts SET draft_json = CAST(:j AS jsonb) WHERE id = :d"),
            {"j": json.dumps(stored), "d": ready_draft},
        )
    before = await written_counts(engine)
    r = await confirm(logged_in_client, ready_draft)
    assert problem(r) == (409, "conflict_state")
    assert "no longer matches the profile schema" in r.json()["detail"]
    assert await written_counts(engine) == before and (await draft_row(engine, ready_draft)).status == "ready"
    # neither the response nor a log line quotes the stored values or the validation error
    for value in ("foo", "grandmaster", "literal_error", "model_type"):
        assert value not in r.text and not any(value in str(e) for e in log_events()), value


async def test_retained_ads_without_the_ack_are_422(logged_in_client, engine, ready_draft) -> None:
    await retained_revisions(engine, 3)
    before = await written_counts(engine)
    r = await confirm(logged_in_client, ready_draft, ack=False)
    assert problem(r) == (422, "validation_failed")
    assert errors_of(r) == [("body.expected_rescore_ack", "ack_required")]
    assert await written_counts(engine) == before and (await draft_row(engine, ready_draft)).status == "ready"
    body = await confirmed(logged_in_client, ready_draft, ack=True)
    c_hat = central_cost_per_target(Settings.model_validate(load_settings_v1()).scoring.model_policy)
    assert (body["targets_to_create"], body["est_usd"]) == (3, format(3 * c_hat, "f"))  # $0.157938
    assert body["bulk_budget_usd"] == BULK_BUDGET
    # M2 creates no target (Deviation 10): the count is what M3's create_targets_for_tuple will create


async def test_an_unknown_draft_is_404_and_a_draft_not_ready_is_409(
    logged_in_client, engine, op_runner, upload
) -> None:
    assert problem(await confirm(logged_in_client, 999_999)) == (404, "not_found")
    assert problem(await logged_in_client.get("/api/profile/drafts/999999/rescore-estimate")) == (
        404,
        "not_found",
    )
    draft_id = await upload(SYNTHETIC_CV)
    await op_runner.run_pending()
    assert (await draft_row(engine, draft_id)).status == "text_review"
    r = await confirm(logged_in_client, draft_id)
    assert problem(r) == (409, "conflict_state")


async def test_the_body_forbids_unknown_members(logged_in_client, ready_draft) -> None:
    r = await logged_in_client.post(
        url(ready_draft),
        json={"expected_rescore_ack": True, "force": True},
        headers=idem("unknown-member-key-01"),
    )
    assert problem(r) == (422, "validation_failed")


# --- (g): the derived seniority scope (Deviation 14) --------------------------------------------------------


async def test_profile_relative_seniority_creates_the_derived_scope_and_one_apply(
    logged_in_client, engine, ready_draft
) -> None:
    active_before = await active_scope_id(engine)
    await confirmed(logged_in_client, ready_draft)
    pending = await pending_scope_id(engine)
    assert pending is not None and pending != active_before and await active_scope_id(engine) == active_before
    payload = await scalar(engine, "SELECT payload FROM scope_versions WHERE id = :i", i=pending)
    low, high = derived_seniority("senior", below=1, above=1)  # the seeded range around the profile's level
    assert payload["seniority_range"] == {"mode": "explicit", "below": 1, "above": 1, "min": low, "max": high}
    (apply,) = await operations_of(engine, "apply_scope")
    assert (apply.status, apply.payload) == ("pending", {"scope_version_id": pending})


async def test_explicit_seniority_creates_no_scope_and_no_apply(
    logged_in_client, engine, op_runner, upload
) -> None:
    await activate_explicit_scope(logged_in_client, op_runner)
    explicit = await active_scope_id(engine)
    draft_id = await make_ready_draft(logged_in_client, op_runner, upload)
    scopes, applies = (
        await count_rows(engine, "scope_versions"),
        len(await operations_of(engine, "apply_scope")),
    )
    await confirmed(logged_in_client, draft_id)
    assert await count_rows(engine, "scope_versions") == scopes
    assert len(await operations_of(engine, "apply_scope")) == applies
    assert (await active_scope_id(engine), await pending_scope_id(engine)) == (explicit, None)


async def test_confirm_a_b_a_clears_the_obsolete_pending_scope(
    logged_in_client, engine, op_runner, upload, job_factory
) -> None:
    """Confirming A, then B (B's apply pending), then A again must clear the obsolete pending B: B's apply
    would otherwise flip the filters to B's seniority range under profile A."""
    await job_factory.many(20)  # retained jobs with det-facts
    draft_a = await make_ready_draft(logged_in_client, op_runner, upload, edit=with_seniority("junior"))
    profile_a = await confirmed(logged_in_client, draft_a)
    (apply_a,) = await operations_of(engine, "apply_scope")
    await op_runner.run_one(apply_a.id)
    scope_a = await active_scope_id(engine)
    assert scope_a == apply_a.payload["scope_version_id"] and await pending_scope_id(engine) is None

    draft_b = await make_ready_draft(logged_in_client, op_runner, upload, edit=with_seniority("senior"))
    await confirmed(logged_in_client, draft_b)
    (_, apply_b) = await operations_of(engine, "apply_scope")
    scope_b = apply_b.payload["scope_version_id"]
    assert scope_b != scope_a and await pending_scope_id(engine) == scope_b
    pause = PausePoint("scope_apply.after_chunk")  # holds B's apply after its first chunk
    run_b = asyncio.create_task(op_runner.run_one(apply_b.id, pause=pause))
    await asyncio.wait_for(pause.reached.wait(), 10)

    again_a = await make_ready_draft(logged_in_client, op_runner, upload, edit=with_seniority("junior"))
    assert (await confirmed(logged_in_client, again_a))["profile_version_id"] == profile_a[
        "profile_version_id"
    ]
    assert await pending_scope_id(engine) is None and await active_scope_id(engine) == scope_a
    pause.resume()
    await run_b
    assert await operation_end(engine, apply_b.id) == ("succeeded", {"obsolete": True})
    assert (await active_scope_id(engine), await pending_scope_id(engine)) == (scope_a, None)
    assert len(await operations_of(engine, "apply_scope")) == 2  # A's scope is active: no apply for it


async def test_a_confirm_back_to_the_active_scope_resolves_an_open_apply_failed(
    logged_in_client, engine, op_runner, ready_draft
) -> None:
    """§13.4 step 5's revert remedy, as the save applies it: a desired scope that is the active one leaves
    nothing unapplied, so every open `scope.apply_failed` is resolved (before `ranking_state`)."""
    await activate_explicit_scope(logged_in_client, op_runner)
    async with engine.begin() as conn:
        await raise_alert(
            conn,
            code=scope_apply.APPLY_FAILED_ALERT,
            severity="critical",
            subject="scope:999999",
            message="The scope apply failed",
        )
    await confirmed(logged_in_client, ready_draft)
    assert await pending_scope_id(engine) is None
    open_alerts = "SELECT count(*) FROM alerts WHERE code = :c AND resolved_at IS NULL"
    assert await scalar(engine, open_alerts, c=scope_apply.APPLY_FAILED_ALERT) == 0


async def activate_explicit_scope(client: httpx.AsyncClient, op_runner: OpRunner) -> None:
    """The seeded settings with an explicit seniority range, saved and its scope applied (the active one)."""
    payload = copy.deepcopy((await settings_body(client))["payload"])
    payload["scope"]["seniority_range"] = {
        "mode": "explicit",
        "below": 1,
        "above": 1,
        "min": "mid",
        "max": "lead",
    }
    saved = await save(client, payload)
    await op_runner.run_one(saved["apply_scope_operation_id"])


# --- M3 Task 14 (Deviation 10's switch flipped): the collection with a tuple -------------------------------


async def test_after_confirm_a_collect_run_creates_its_targets_and_i5_holds(
    logged_in_client, engine, ready_draft, current_epoch, fake_http, fake_embedder, collect_deps
) -> None:
    await confirmed(logged_in_client, ready_draft)
    _, tuple_id = await active_profile(engine)
    await seed_inputs(engine, companies=(("greenhouse", "getyourguide"),), enabled=("arbeitnow", "ats"))
    run_id = await manual_run_id(engine)  # the run records the active tuple, as the run creation does
    compiled = build_collect_graph(InMemorySaver(), collect_deps)
    assert await runs.execute_run(engine, compiled, run_id, 1, epoch=1) == "started"
    (run,) = await rows(engine, "SELECT status, status_reason, report FROM runs WHERE id = :r", r=run_id)
    assert (run.status, run.status_reason) == ("succeeded", None)
    (made,) = await rows(
        engine, "SELECT count(*) AS n FROM score_targets WHERE llm_tuple_id = :t", t=tuple_id
    )
    assert made.n > 0  # node 9 gave the run's revisions their targets of the confirmed tuple
    assert run.report["execution"]["invariants"]["I5"] == {
        "ok": True,
        "detail": {"llm_tuple_id": tuple_id, "revisions": made.n, "missing": 0},
    }
    assert run.report["revisions"]["targets"] == {
        "created": made.n,
        "revived": 0,
        "cancelled": 0,
        "skipped": {},
    }
    assert ENQUEUE_TARGETS in run.report["execution"]["nodes"]


# --- the reads ----------------------------------------------------------------------------------------------


async def test_the_profile_list_shows_the_confirmed_version(logged_in_client, engine, ready_draft) -> None:
    body = await confirmed(logged_in_client, ready_draft)
    (version,) = await rows(engine, "SELECT * FROM profile_versions")
    listed = (await logged_in_client.get("/api/profile")).json()
    expected = {
        "id": body["profile_version_id"],
        "version_no": 1,
        "confirmed_at": "2026-10-22T09:30:00Z",
        "purged": False,
        "profile": version.profile_json,
    }
    assert listed == {"active": expected, "versions": [expected]}


async def test_the_rescore_estimate_previews_the_count_the_costs_and_the_month(
    logged_in_client, engine, ready_draft
) -> None:
    await retained_revisions(engine, 4)
    r = await logged_in_client.get(f"/api/profile/drafts/{ready_draft}/rescore-estimate")
    assert r.status_code == 200, r.text
    policy = Settings.model_validate(load_settings_v1()).scoring.model_policy
    counted = await scalar(
        engine,
        "SELECT coalesce((SELECT counted_usd FROM llm_budget WHERE month = :m), 0)",
        m=budget.month_of(NOW),
    )
    assert r.json() == {
        "targets": 4,
        "est_usd_online": format(4 * central_cost_per_target(policy), "f"),
        "est_usd_batch": format(4 * central_batch_cost_per_target(policy), "f"),
        "month_spend": format(Decimal(counted).quantize(budget.USD_QUANTUM), "f"),
        "cap": "100.00",
    }
    assert (await draft_row(engine, ready_draft)).status == "ready"  # a preview writes nothing


async def test_confirm_logs_ids_and_codes_only(logged_in_client, op_runner, upload, log_events) -> None:
    draft_id = await make_ready_draft(
        logged_in_client, op_runner, upload, edit=with_user_entered(work_authorization_de="authorized")
    )
    start = len(log_events())
    await confirmed(logged_in_client, draft_id)
    events = log_events()[start:]  # what the confirm logged
    assert [e["event"] for e in events if str(e["event"]).startswith("profile_confirm")] == [
        "profile_confirmed"
    ]
    for word in ("Musterfirma", "Backend-Entwicklerin", "Zahlungsdiensten", "authorized"):
        assert not any(word in str(e) for e in events), word


async def test_a_refused_confirm_logs_its_codes(logged_in_client, engine, ready_draft, log_events) -> None:
    await retained_revisions(engine, 1)
    assert problem(await confirm(logged_in_client, ready_draft, ack=False)) == (422, "validation_failed")
    (event,) = [e for e in log_events() if e["event"] == "profile_confirm_refused"]
    assert (event["draft_id"], event["code"], event["errors"], event["issue_codes"]) == (
        ready_draft,
        "validation_failed",
        1,
        ["ack_required"],
    )
