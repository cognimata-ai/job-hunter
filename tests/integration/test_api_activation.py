"""The shipped-version activation and the version listings on PostgreSQL (spec §13.3 L4896-4900, §14.2
L5005-5006, §10.11, §11.13; plan Task 21, Deviation 23, Codex r1 #25, #35, r2 B7, Ruling P41).

`POST /api/settings/{rubric,prompt-schema}-versions/{id}/activate` is an `llm_tuple` change: in one
transaction `active_config FOR UPDATE`, the idempotency row, the version row (404), for a prompt-schema
version a passed live schema dry-run of every (schema, model) pair of the active model policy (409
`conflict_state`, `schema_dry_run_required`), the eval gate of the candidate (409 `eval_gate_failed` without
the override), the re-score acknowledgement while retained revisions exist (422), the pointer flip and the
internal save of the active document with the forced class `llm_tuple`, whose tuple activation runs after
`_FLIP`. The 201 body is `{version, llm_tuple_id, targets_to_create, bulk_budget_usd}` (Ruling P41). The
listings show which version is active and, for a prompt-schema version, its newest schema dry-run.

The tests' process is a live one (`_live_process`): a fake run never verifies a schema. Every datum is
invented.
"""

from decimal import Decimal
from typing import Any, Final

import httpx
import pytest
import sqlalchemy as sa
from jobhunter.scoring import targets
from jobhunter.settings import activate as activate_mod
from jobhunter.settings import save as save_mod
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.support.activation import (
    ACK,
    HAIKU,
    POLICY_B_CASES,
    PROMPTS_URL,
    RUBRICS_URL,
    SONNET,
    activate,
    case,
    dry_run_record,
    point_prompt_schema_at,
    prompt_versions,
    rubric_versions,
)
from tests.support.eval import insert_cases
from tests.support.fixtures import count_rows
from tests.support.m3 import insert_job, insert_target
from tests.support.profile import idem
from tests.support.settings import activate_profile, fresh_key, generation, settings_body

pytestmark = pytest.mark.integration

_ACTIVE: Final = sa.text(
    "SELECT rubric_version_id, prompt_schema_version_id, model_policy_id, llm_tuple_id, settings_version_id "
    "FROM active_config WHERE id = 1"
)


@pytest.fixture(autouse=True)
def _live_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts as a live process, whatever the environment the suite runs in."""
    monkeypatch.delenv("JOBHUNTER_FAKE_LLM", raising=False)


async def active(engine: AsyncEngine) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(_ACTIVE)).one()


async def save_revise_on_haiku(client: httpx.AsyncClient) -> None:
    """The active document with `revise` on Haiku, saved (its pairs verified on the active version first)."""
    active_body = await settings_body(client)
    payload = active_body["payload"]
    payload["scoring"]["model_policy"]["preset"] = "custom"
    payload["scoring"]["model_policy"]["nodes"]["revise"]["model"] = HAIKU
    dry = await client.post("/api/settings/dry-run", json={"payload": payload})
    codes = [w["code"] for w in dry.json()["warnings"]]
    r = await client.post(
        "/api/settings",
        json={
            "payload": payload,
            "base_version_id": active_body["version"]["id"],
            "acknowledged_warnings": codes,
        },
        headers=idem(fresh_key()),
    )
    assert r.status_code == 201, r.text


async def retained_job_of(engine: AsyncEngine, llm_tuple_id: int) -> int:
    """A retained job with a `pending` target of `llm_tuple_id`; the target's id."""
    async with engine.begin() as conn:
        job = await insert_job(conn)
        return await insert_target(conn, job, llm_tuple_id)


async def bulk_operations(engine: AsyncEngine) -> dict[int, str]:
    async with engine.connect() as conn:
        rows = await conn.execute(sa.text("SELECT llm_tuple_id, state FROM bulk_operations ORDER BY id"))
        return {int(r.llm_tuple_id): str(r.state) for r in rows}


class SeenAtPoints:
    """The `active_config.llm_tuple_id` and the active bulk operation's tuple that the activation's own
    transaction sees at `settings.after_llm_tuple` and `activation.after_cancel`: the connection is the one
    the activation hands to `save_settings`, read while the point holds the transaction between statements."""

    POINTS: Final = (save_mod.AFTER_LLM_TUPLE_POINT, targets.AFTER_CANCEL_POINT)
    _READ: Final = sa.text(
        "SELECT a.llm_tuple_id, (SELECT b.llm_tuple_id FROM bulk_operations b WHERE b.state = 'active') "
        "AS bulk_tuple_id FROM active_config a WHERE a.id = 1"
    )

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.seen: list[tuple[str, int | None, int | None]] = []
        self._conn: list[AsyncConnection] = []
        real = save_mod.save_settings

        async def holding(conn: AsyncConnection, *args: Any, **kwargs: Any) -> Any:
            self._conn.append(conn)
            return await real(conn, *args, **kwargs)

        monkeypatch.setattr(activate_mod, "save_settings", holding)  # the name the activation calls
        monkeypatch.setattr(save_mod, "afault_point", self)
        monkeypatch.setattr(targets, "afault_point", self)

    async def __call__(self, name: str) -> None:
        if name in self.POINTS and self._conn:
            row = (await self._conn[-1].execute(self._READ)).one()
            self.seen.append((name, row.llm_tuple_id, row.bulk_tuple_id))


# ---- the prompt-schema form --------------------------------------------------------------------------------


async def test_a_prompt_schema_activation_needs_a_live_pass_of_every_pair_of_the_active_policy(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """Step 4 (Task 11's `schema_verified`, per version and per model; Codex r1 #25): no live dry-run of the
    version, a fake one, or a live one that covers the generator schema on the `generate` model but not on
    the `revise` model is 409 `conflict_state` with detail `schema_dry_run_required`, whatever the override,
    and nothing is written."""
    v1, v2 = await prompt_versions(engine)
    await point_prompt_schema_at(engine, v1)

    async def refused(missing: str) -> None:
        versions = await count_rows(engine, "settings_versions")
        keys = await count_rows(engine, "api_idempotency")
        body = {**ACK, "eval_override_ack": True}
        r = await activate(logged_in_client, "prompt_schema", v2, body)
        problem = r.json()
        assert (r.status_code, problem["code"], problem["detail"]) == (
            409,
            "conflict_state",
            "schema_dry_run_required",
        ), r.text
        assert missing in problem["title"]
        assert (await active(engine)).prompt_schema_version_id == v1
        assert await count_rows(engine, "settings_versions") == versions
        assert await count_rows(engine, "api_idempotency") == keys  # the key's claim rolled back

    await refused(f"generator on {SONNET}")
    await dry_run_record(engine, v2, POLICY_B_CASES, transport="fake")
    await refused(f"generator on {SONNET}")  # a fake run never verifies
    await dry_run_record(engine, v1, [*POLICY_B_CASES, case("generator", HAIKU)])
    await save_revise_on_haiku(logged_in_client)  # verified on version 1, the active one
    await dry_run_record(engine, v2, POLICY_B_CASES)
    await refused(f"generator on {HAIKU}")  # the generate model's pass is no pass for the revise model
    await dry_run_record(engine, v2, [case("generator", HAIKU)])
    r = await activate(logged_in_client, "prompt_schema", v2, ACK)
    assert r.status_code == 201, r.text
    assert (await active(engine)).prompt_schema_version_id == v2


async def test_a_prompt_schema_activation_flips_the_pointer_and_activates_the_new_tuple_after_the_flip(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Steps 5 to 8 (Codex r1 #35, r2 B7): with a passed live run of every pair and the gate uncalibrated,
    201; the pointer flipped, the new tuple created and active, a settings version of the forced class
    `llm_tuple` over the unchanged document, the old tuple's target cancelled, the bulk operation opened
    after `_FLIP` (`settings.after_llm_tuple` sees the old pointer and bulk operation,
    `activation.after_cancel` the new ones), `create_targets_for_tuple` recorded and the ranking bumped; a
    replay with the same key is the stored 201."""
    v1, v2 = await prompt_versions(engine)
    await point_prompt_schema_at(engine, v1)
    _, tuple_a = await activate_profile(engine)
    old_target = await retained_job_of(engine, tuple_a)
    await dry_run_record(engine, v2, POLICY_B_CASES)
    before = await active(engine)
    generation_before = await generation(engine)

    r = await activate(logged_in_client, "prompt_schema", v2, {"expected_rescore_ack": False})
    problem = r.json()
    assert (r.status_code, problem["code"]) == (422, "validation_failed"), r.text
    assert [(e["field"], e["code"]) for e in problem["errors"]] == [
        ("body.expected_rescore_ack", "ack_required")
    ]
    assert await active(engine) == before and await count_rows(engine, "api_idempotency") == 0

    seen = SeenAtPoints(monkeypatch)
    key = fresh_key("act")
    r = await activate(logged_in_client, "prompt_schema", v2, ACK, key=key)
    assert r.status_code == 201, r.text
    after = await active(engine)
    tuple_b = after.llm_tuple_id
    assert tuple_b is not None and tuple_b != tuple_a
    body = r.json()
    assert set(body) == {"version", "llm_tuple_id", "targets_to_create", "bulk_budget_usd"}
    assert (body["version"], body["llm_tuple_id"], body["targets_to_create"]) == (2, tuple_b, 1)
    assert Decimal(body["bulk_budget_usd"]) > 0
    assert (after.prompt_schema_version_id, after.rubric_version_id) == (v2, before.rubric_version_id)
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                sa.text(
                    "SELECT t.prompt_schema_version_id, v.version_no, v.effect_classes, "
                    "v.eval_override_ack, v.payload, v.base_version_id, b.budget_usd FROM llm_tuples t, "
                    "settings_versions v, bulk_operations b "
                    "WHERE t.id = :t AND v.id = :v AND b.llm_tuple_id = :t"
                ),
                {"t": tuple_b, "v": after.settings_version_id},
            )
        ).one()
        previous = (
            await conn.execute(
                sa.text("SELECT payload FROM settings_versions WHERE id = :v"),
                {"v": before.settings_version_id},
            )
        ).scalar_one()
    assert (row.prompt_schema_version_id, row.version_no, row.effect_classes) == (v2, 2, ["llm_tuple"])
    assert (row.eval_override_ack, row.base_version_id, row.payload) == (
        False,
        before.settings_version_id,
        previous,
    )
    assert format(row.budget_usd, "f") == body["bulk_budget_usd"]
    async with engine.connect() as conn:
        target = (
            await conn.execute(
                sa.text("SELECT state, state_reason FROM score_targets WHERE id = :t"), {"t": old_target}
            )
        ).one()
    assert (target.state, target.state_reason) == ("cancelled", "tuple_superseded")
    assert await bulk_operations(engine) == {tuple_a: "cancelled", tuple_b: "active"}
    async with engine.connect() as conn:
        payloads = (
            (
                await conn.execute(
                    sa.text(
                        "SELECT payload FROM operations WHERE kind = 'create_targets_for_tuple' ORDER BY id"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert payloads[-1]["llm_tuple_id"] == tuple_b
    assert seen.seen == [
        (save_mod.AFTER_LLM_TUPLE_POINT, tuple_a, tuple_a),
        (targets.AFTER_CANCEL_POINT, tuple_b, tuple_b),
    ]
    assert await generation(engine) == generation_before + 1

    replay = await activate(logged_in_client, "prompt_schema", v2, ACK, key=key)
    assert (replay.status_code, replay.json()) == (201, body)
    assert await count_rows(engine, "settings_versions") == 2
    other = await activate(logged_in_client, "prompt_schema", v2, {**ACK, "eval_override_ack": True}, key=key)
    assert (other.status_code, other.json()["code"]) == (422, "idempotency_key_reused")


async def test_an_activation_without_an_active_profile_flips_the_pointer_and_creates_no_tuple(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """No profile, no tuple (`active_config` pairs them): the pointer flips with the settings version, nothing
    is to be re-scored, so no acknowledgement is needed and `llm_tuple_id` stays null."""
    v1, v2 = await prompt_versions(engine)
    await point_prompt_schema_at(engine, v1)
    await dry_run_record(engine, v2, POLICY_B_CASES)
    async with engine.begin() as conn:
        await insert_job(conn)  # a retained revision, but no tuple to re-score it with
    r = await activate(logged_in_client, "prompt_schema", v2, {"expected_rescore_ack": False})
    assert r.status_code == 201, r.text
    body = r.json()
    assert (body["llm_tuple_id"], body["targets_to_create"], body["bulk_budget_usd"]) == (None, 0, "0.000000")
    after = await active(engine)
    assert (after.prompt_schema_version_id, after.llm_tuple_id) == (v2, None)
    assert await count_rows(engine, "bulk_operations") == 0


# ---- the eval gate and the rubric form ---------------------------------------------------------------------


async def test_a_failed_eval_gate_refuses_the_rubric_activation_unless_the_override_is_acknowledged(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Step 5 on the candidate (Task 20: the new rubric with the active prompt-schema version and policies):
    with 30 of the user's cases and no golden run of it the gate has failed, 409 `eval_gate_failed`; the
    override activates it, recorded on the settings version; the rubric pointer flips and the tuple with
    it, its activation after `_FLIP`. Before the active pair is verified, the save's own re-check refuses it
    after the flip, which rolls back with the rest."""
    rubrics = await rubric_versions(engine)
    _, v2 = await prompt_versions(engine)
    _, tuple_a = await activate_profile(engine)
    await insert_cases(engine, 30)
    assert (await active(engine)).rubric_version_id == rubrics["v2"]
    # the save of step 8 re-checks the active policy after the flip: unverified, refused, the flip rolled back
    r = await activate(logged_in_client, "rubric", rubrics["v1"], {**ACK, "eval_override_ack": True})
    problem = r.json()
    assert (r.status_code, problem["code"], problem["detail"]) == (
        409,
        "conflict_state",
        "schema_dry_run_required",
    )
    assert (await active(engine)).rubric_version_id == rubrics["v2"]
    assert (
        await count_rows(engine, "settings_versions") == 1
        and await count_rows(engine, "api_idempotency") == 0
    )
    await dry_run_record(engine, v2, POLICY_B_CASES)  # the active pair: an `llm_tuple` save needs it verified
    r = await activate(logged_in_client, "rubric", rubrics["v1"], ACK)
    assert (r.status_code, r.json()["code"]) == (409, "eval_gate_failed"), r.text
    assert (await active(engine)).rubric_version_id == rubrics["v2"]
    seen = SeenAtPoints(monkeypatch)
    r = await activate(logged_in_client, "rubric", rubrics["v1"], {**ACK, "eval_override_ack": True})
    assert r.status_code == 201, r.text
    after = await active(engine)
    assert after.rubric_version_id == rubrics["v1"] and after.llm_tuple_id not in (None, tuple_a)
    assert seen.seen == [  # the tuple's activation after `_FLIP`, as for the prompt-schema form
        (save_mod.AFTER_LLM_TUPLE_POINT, tuple_a, tuple_a),
        (targets.AFTER_CANCEL_POINT, after.llm_tuple_id, after.llm_tuple_id),
    ]
    assert await bulk_operations(engine) == {tuple_a: "cancelled", after.llm_tuple_id: "active"}
    async with engine.connect() as conn:
        version = (
            await conn.execute(
                sa.text("SELECT effect_classes, eval_override_ack FROM settings_versions WHERE id = :v"),
                {"v": after.settings_version_id},
            )
        ).one()
        tuple_rubric = (
            await conn.execute(
                sa.text("SELECT rubric_version_id FROM llm_tuples WHERE id = :t"), {"t": after.llm_tuple_id}
            )
        ).scalar_one()
    assert (version.effect_classes, version.eval_override_ack, tuple_rubric) == (
        ["llm_tuple"],
        True,
        rubrics["v1"],
    )
    assert r.json()["targets_to_create"] == 30  # the cases' jobs are retained revisions


async def test_a_failed_eval_gate_refuses_the_prompt_schema_activation_unless_the_override_is_acknowledged(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    """Step 5 for the prompt-schema form: the candidate (version 2 with the active rubric and policies) has no
    golden run while 30 of the user's cases are active, 409 `eval_gate_failed` with nothing written; the
    override activates it and is recorded."""
    v1, v2 = await prompt_versions(engine)
    await point_prompt_schema_at(engine, v1)
    _, tuple_a = await activate_profile(engine)
    await insert_cases(engine, 30)
    await dry_run_record(engine, v2, POLICY_B_CASES)
    r = await activate(logged_in_client, "prompt_schema", v2, ACK)
    assert (r.status_code, r.json()["code"]) == (409, "eval_gate_failed"), r.text
    assert (await active(engine)).prompt_schema_version_id == v1
    assert await count_rows(engine, "settings_versions") == 1
    r = await activate(logged_in_client, "prompt_schema", v2, {**ACK, "eval_override_ack": True})
    assert r.status_code == 201, r.text
    after = await active(engine)
    assert after.prompt_schema_version_id == v2 and after.llm_tuple_id not in (None, tuple_a)
    async with engine.connect() as conn:
        override = (
            await conn.execute(
                sa.text("SELECT eval_override_ack FROM settings_versions WHERE id = :v"),
                {"v": after.settings_version_id},
            )
        ).scalar_one()
    assert override is True


async def test_the_activation_refuses_an_unknown_or_active_version_and_a_missing_key(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    rubrics = await rubric_versions(engine)
    _, v2 = await prompt_versions(engine)
    for kind in ("rubric", "prompt_schema"):
        r = await activate(logged_in_client, kind, 10**12, ACK)
        assert (r.status_code, r.json()["code"]) == (404, "not_found"), r.text
    for kind, version in (("rubric", rubrics["v2"]), ("prompt_schema", v2)):
        r = await activate(logged_in_client, kind, version, ACK)
        assert (r.status_code, r.json()["code"]) == (409, "conflict_state"), r.text
    r = await logged_in_client.post(f"{RUBRICS_URL}/{rubrics['v1']}/activate", json=ACK)
    assert (r.status_code, r.json()["code"]) == (400, "idempotency_key_required")
    r = await activate(logged_in_client, "rubric", rubrics["v1"], {**ACK, "note": "x"})
    assert (r.status_code, r.json()["code"]) == (422, "validation_failed")
    r = await activate(logged_in_client, "rubric", rubrics["v1"], {})
    assert (r.status_code, r.json()["code"]) == (422, "validation_failed")  # the acknowledgement is required
    assert await count_rows(engine, "settings_versions") == 1


# ---- the listings ------------------------------------------------------------------------------------------


async def test_the_listings_show_the_active_version_and_the_newest_dry_run(
    logged_in_client: httpx.AsyncClient, engine: AsyncEngine
) -> None:
    rubrics = await rubric_versions(engine)
    v1, v2 = await prompt_versions(engine)
    await dry_run_record(engine, v2, POLICY_B_CASES, transport="fake", started_minutes_ago=10)
    newest = await dry_run_record(engine, v2, POLICY_B_CASES, started_minutes_ago=5)
    r = await logged_in_client.get(RUBRICS_URL)
    assert r.status_code == 200, r.text
    async with engine.connect() as conn:
        hashes = {
            int(row.id): bytes(row.content_hash).hex()
            for row in await conn.execute(sa.text("SELECT id, content_hash FROM rubric_versions"))
        }
    assert [(i["id"], i["version"], i["content_hash"], i["active"]) for i in r.json()] == [
        (rubrics["v1"], "v1", hashes[rubrics["v1"]], False),
        (rubrics["v2"], "v2", hashes[rubrics["v2"]], True),
    ]
    assert all(i["created_at"].endswith("Z") or i["created_at"].endswith("+00:00") for i in r.json())
    r = await logged_in_client.get(PROMPTS_URL)
    assert r.status_code == 200, r.text
    items = {i["id"]: i for i in r.json()}
    assert list(items) == [v1, v2]
    assert (items[v1]["active"], items[v1]["dry_run"]) == (False, None)
    assert items[v2]["active"] is True and len(items[v2]["prompt_schema_hash"]) == 64
    dry = items[v2]["dry_run"]
    assert (dry["eval_run_id"], dry["passed"], dry["transport"]) == (newest, True, "live")
    assert dry["finished_at"] is not None
    running = await dry_run_record(engine, v1, [], status="running")
    r = await logged_in_client.get(PROMPTS_URL)
    v1_dry = {i["id"]: i for i in r.json()}[v1]["dry_run"]
    assert v1_dry == {"eval_run_id": running, "passed": None, "transport": None, "finished_at": None}
