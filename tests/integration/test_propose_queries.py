"""`propose_queries` (spec §5.5.1, §9.1, §9.5, §11.12, §14.2; plan Task 20, Deviations 20, 26, 27 and 28): the
proposal routes, the `propose` graph's one node through the real gateway on the fake LLM transport and the
hand-authored synthetic stream of `SYNTHETIC_QUERIES`, the receipts walk, the one repair and the failure hook.

Every operation runs in process (`op_runner`); the active profile is `activate_profile`'s synthetic one (an
invented candidate). No real CV, and no paid call.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any, Final

import httpx
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db.seed import canonical_json
from jobhunter.geo.gazetteer import Gazetteer
from jobhunter.graphs.profile.graph import thread_id
from jobhunter.llm import fake
from jobhunter.llm.prompts.render import propose_queries_prompt
from jobhunter.llm.schemas.profile import ConfirmedProfile
from jobhunter.llm.schemas.queries import QueryProposalOut
from jobhunter.profile.operations import proposal_failed
from jobhunter.queries import proposals
from jobhunter.queries.proposals import location_words, validate_proposal
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.faults import PausePoint
from tests.support.fixtures import count_rows
from tests.support.llm import ledger_states, reclaim_stale_now, save_budget, tries
from tests.support.profile import QUERIES_USAGE, SYNTHETIC_QUERIES, OpRunner, idem
from tests.support.settings import BERLIN, activate_profile, fresh_key

pytestmark = pytest.mark.integration

URL: Final = "/api/profile/query-proposals"
AFTER_CALL: Final = "queries.propose.after_call"
STEPS: Final = ["propose_queries"]
QUERIES: Final = SYNTHETIC_QUERIES["queries"]
# Words of the synthetic answer and of the profile: no log line or checkpoint may hold them.
CONTENT_WORDS: Final = ("Zahlungsverkehr", "FastAPI services", "Musterfirma", "Datenplattformen")
CHECKPOINT_TABLES: Final = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")


def served() -> int:
    return fake.state().served


async def propose(client: httpx.AsyncClient, key: str | None = None) -> int:
    """`POST /api/profile/query-proposals` (202 required): the proposal's id."""
    r = await client.post(URL, headers=idem(key or fresh_key("proposal")))
    assert r.status_code == 202, (r.status_code, r.text)
    assert set(r.json()) == {"proposal_id"}
    return int(r.json()["proposal_id"])


async def proposal_row(engine: AsyncEngine, proposal_id: int) -> sa.Row[Any]:
    async with engine.connect() as conn:
        statement = sa.text("SELECT *, xmin::text AS xmin FROM query_proposals WHERE id = :p")
        return (await conn.execute(statement, {"p": proposal_id})).one()


async def operations_of(engine: AsyncEngine, proposal_id: int) -> list[sa.Row[Any]]:
    async with engine.connect() as conn:
        statement = sa.text(
            "SELECT id, status, attempts, last_error_code, dedup_key, payload, xmin::text AS xmin "
            "FROM operations WHERE kind = 'propose_queries' AND payload ->> 'proposal_id' = :p ORDER BY id"
        )
        return list((await conn.execute(statement, {"p": str(proposal_id)})).all())


async def operation_of(engine: AsyncEngine, proposal_id: int) -> sa.Row[Any]:
    (op,) = await operations_of(engine, proposal_id)
    return op


async def proposed(engine: AsyncEngine, client: httpx.AsyncClient) -> tuple[int, int, int]:
    """The active synthetic profile and a proposal for it: `(profile_version_id, proposal_id, op_id)`."""
    version_id, _ = await activate_profile(engine)
    proposal_id = await propose(client)
    return version_id, proposal_id, int((await operation_of(engine, proposal_id)).id)


_ACTIVE_SCOPE: Final = sa.text(
    "SELECT a.scope_version_id, s.payload FROM active_config a "
    "JOIN scope_versions s ON s.id = a.scope_version_id"
)
_NEW_SCOPE: Final = sa.text(
    "INSERT INTO scope_versions (scope_hash, payload) VALUES (sha256(:h), CAST(:p AS jsonb)) "
    "ON CONFLICT (scope_hash) DO NOTHING"
)
_SCOPE_OF_HASH: Final = sa.text("SELECT id FROM scope_versions WHERE scope_hash = sha256(:h)")


async def active_scope_id(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(_ACTIVE_SCOPE)).one().scope_version_id)


async def activate_scope(engine: AsyncEngine, locations: list[dict[str, Any]]) -> int:
    """The scope version of the active scope with `locations` (inserted when new), made active as a finished
    apply leaves it."""
    async with engine.begin() as conn:
        payload = dict((await conn.execute(_ACTIVE_SCOPE)).one().payload)
        payload["locations"] = locations
        params = {"h": canonical_json(payload), "p": json.dumps(payload)}
        await conn.execute(_NEW_SCOPE, params)
        scope_id = int((await conn.execute(_SCOPE_OF_HASH, {"h": params["h"]})).scalar_one())
        await conn.execute(sa.text("UPDATE active_config SET scope_version_id = :s"), {"s": scope_id})
    return scope_id


async def purge_profile(engine: AsyncEngine, version_id: int) -> None:
    """The profile version's content purged, as §20.2's purge leaves it."""
    async with engine.begin() as conn:
        await conn.execute(
            sa.text("UPDATE profile_versions SET profile_json = NULL, purged_at = now() WHERE id = :v"),
            {"v": version_id},
        )


async def assert_failed_with_the_operation(engine: AsyncEngine, proposal_id: int, code: str) -> None:
    """The proposal `failed` and its operation `failed(code)`, in one transaction, without a requeue."""
    proposal, op = await proposal_row(engine, proposal_id), await operation_of(engine, proposal_id)
    assert (proposal.status, proposal.items) == ("failed", None)
    assert (op.status, op.last_error_code, op.attempts) == ("failed", code, 0)
    assert proposal.xmin == op.xmin


async def state_of(op_runner: OpRunner, proposal_id: int, op_id: int, attempts: int) -> dict[str, Any]:
    thread = thread_id("q", proposal_id, op_id, attempts)
    state = await op_runner.deps.profile_graphs.propose.aget_state({"configurable": {"thread_id": thread}})
    return dict(state.values)


# --- the request ----------------------------------------------------------------------------------------


async def test_a_request_creates_a_running_proposal_and_its_operation(engine, logged_in_client) -> None:
    version_id, _ = await activate_profile(engine)
    proposal_id = await propose(logged_in_client)
    proposal = await proposal_row(engine, proposal_id)
    assert (proposal.profile_version_id, proposal.status, proposal.items) == (version_id, "running", None)
    op = await operation_of(engine, proposal_id)
    assert (op.status, op.dedup_key, op.payload) == (
        "pending",
        f"propose_queries:{proposal_id}",
        {"proposal_id": proposal_id, "scope_version_id": await active_scope_id(engine)},  # pinned
    )
    async with engine.connect() as conn:  # its job was deferred with the request
        jobs = await conn.execute(
            sa.text(
                "SELECT count(*) FROM procrastinate_jobs WHERE task_name LIKE '%run_operation' "
                "AND args @> jsonb_build_object('operation_id', CAST(:op AS bigint))"
            ),
            {"op": op.id},
        )
        assert jobs.scalar_one() == 1


async def test_a_repeat_request_while_one_is_running_returns_it(engine, logged_in_client) -> None:
    await activate_profile(engine)
    first = await propose(logged_in_client)
    assert await propose(logged_in_client) == first  # a new key, the same running proposal
    assert await count_rows(engine, "query_proposals") == 1
    assert len(await operations_of(engine, first)) == 1


async def test_the_same_key_replays_the_stored_response(engine, logged_in_client) -> None:
    await activate_profile(engine)
    key = fresh_key("proposal")
    first = await propose(logged_in_client, key)
    async with engine.begin() as conn:  # the proposal ended: a new key would open another one
        await conn.execute(
            sa.text("UPDATE query_proposals SET status = 'failed' WHERE id = :p"), {"p": first}
        )
    assert await propose(logged_in_client, key) == first
    assert await count_rows(engine, "query_proposals") == 1


async def test_a_purged_active_profile_is_a_conflict(engine, logged_in_client) -> None:
    version_id, _ = await activate_profile(engine)
    await purge_profile(engine, version_id)
    r = await logged_in_client.post(URL, headers=idem(fresh_key("proposal")))
    assert (r.status_code, r.json()["code"]) == (409, "conflict_state")
    assert await count_rows(engine, "query_proposals") == 0
    assert await count_rows(engine, "operations", kind="propose_queries") == 0


async def test_no_active_profile_is_a_conflict(engine, logged_in_client) -> None:
    r = await logged_in_client.post(URL, headers=idem(fresh_key("proposal")))
    assert (r.status_code, r.json()["code"]) == (409, "conflict_state")
    assert await count_rows(engine, "query_proposals") == 0
    assert await count_rows(engine, "operations", kind="propose_queries") == 0


async def test_the_request_needs_an_idempotency_key(engine, logged_in_client) -> None:
    await activate_profile(engine)
    r = await logged_in_client.post(URL)
    assert (r.status_code, r.json()["code"]) == (400, "idempotency_key_required")
    assert await count_rows(engine, "query_proposals") == 0


async def test_an_unknown_proposal_is_not_found(logged_in_client) -> None:
    r = await logged_in_client.get(f"{URL}/987654")
    assert (r.status_code, r.json()["code"]) == (404, "not_found")


# --- the call ---------------------------------------------------------------------------------------------


async def test_a_confirmed_profiles_proposal_reaches_ready(engine, op_runner, logged_in_client) -> None:
    version_id, proposal_id, op_id = await proposed(engine, logged_in_client)
    running = await logged_in_client.get(f"{URL}/{proposal_id}")
    assert running.json() == {"status": "running", "items": None}
    async with engine.connect() as conn:  # the activation's own operation (M3 Task 14) runs first
        creation = (
            await conn.execute(sa.text("SELECT id FROM operations WHERE kind = 'create_targets_for_tuple'"))
        ).scalar_one()
    assert await op_runner.run_pending() == [creation, op_id]
    proposal, op = await proposal_row(engine, proposal_id), await operation_of(engine, proposal_id)
    assert proposal.status == "ready" and 6 <= len(proposal.items["queries"]) <= 16
    assert proposal.items == {"queries": QUERIES}  # every item of the synthetic set is valid
    assert (op.status, op.attempts, op.last_error_code) == ("succeeded", 0, None)
    assert proposal.xmin == op.xmin  # `ready` and the operation's end commit together
    (call,) = await tries(engine)
    assert proposal.llm_call_id == call.call_id and call.output_json == SYNTHETIC_QUERIES
    assert (call.status, call.state, call.logical_call_no, call.max_tokens) == (
        "completed",
        "billed",
        1,
        8000,
    )
    assert (call.input_tokens, call.output_tokens) == (QUERIES_USAGE["input_tokens"], 700)
    assert served() == 1
    r = await logged_in_client.get(f"{URL}/{proposal_id}")
    assert (r.status_code, r.json()) == (200, {"status": "ready", "items": QUERIES})
    assert await state_of(op_runner, proposal_id, op_id, 0) == {
        "proposal_id": proposal_id,
        "operation_id": op_id,
        "llm_call_id": call.call_id,
        "steps": STEPS,
        "outcome": "ready",
    }
    assert version_id == proposal.profile_version_id


async def test_the_call_is_a_non_scoring_propose_queries_call_with_the_active_config(
    engine, op_runner, logged_in_client
) -> None:
    version_id, proposal_id, op_id = await proposed(engine, logged_in_client)
    await op_runner.run_pending()
    (attempt,) = await tries(engine)
    async with engine.connect() as conn:
        call = (
            await conn.execute(
                sa.text(
                    "SELECT c.purpose, c.input_refs, c.effort, c.model_requested, c.execution_config_id, "
                    "c.profile_draft_id, l.purpose_class, l.purpose AS ledger_purpose FROM llm_calls c "
                    "JOIN llm_spend_ledger l ON l.id = c.spend_ledger_id WHERE c.id = :i"
                ),
                {"i": attempt.call_id},
            )
        ).one()
        active = (await conn.execute(sa.text("SELECT * FROM active_config WHERE id = 1"))).one()
        version = (
            await conn.execute(
                sa.text("SELECT profile_json, profile_hash FROM profile_versions WHERE id = :v"),
                {"v": version_id},
            )
        ).one()
    assert (call.purpose, call.ledger_purpose, call.purpose_class) == (
        "propose_queries",
        "propose_queries",
        "nonscoring",
    )
    assert (call.model_requested, call.effort, call.profile_draft_id) == ("claude-opus-5-5", "medium", None)
    assert call.execution_config_id == active.execution_config_id
    refs = dict(call.input_refs)
    owner = refs.pop("owner")
    assert (owner["kind"], owner["id"]) == ("operation", op_id)
    assert refs == {
        "proposal_id": proposal_id,
        "profile_version_id": version_id,
        "scope_version_id": active.scope_version_id,
        "profile_hash": bytes(version.profile_hash).hex(),
        "deadline_s": 300,
    }
    (request,) = fake.state().requests
    assert (request.purpose, request.model, request.max_tokens) == (
        "propose_queries",
        "claude-opus-5-5",
        8000,
    )
    assert request.body["output_config"]["effort"] == "medium"
    prompt = propose_queries_prompt(ConfirmedProfile.model_validate(version.profile_json), [])
    assert request.body["messages"] == [{"role": "user", "content": prompt.messages[0].content}]


async def test_the_request_names_the_active_scope_locations(engine, op_runner, logged_in_client) -> None:
    """§5.5.1: the locations of the scope active at the request, by label, even when another scope is active
    when the operation runs."""
    berlin_scope = await activate_scope(engine, [BERLIN])
    _, proposal_id, _ = await proposed(engine, logged_in_client)
    await activate_scope(engine, [])  # saved after the request: not this proposal's scope
    await op_runner.run_pending()
    assert (await operation_of(engine, proposal_id)).payload["scope_version_id"] == berlin_scope
    (request,) = fake.state().requests
    assert request.body["messages"][0]["content"].endswith('<search_locations>["Berlin"]</search_locations>')
    assert (await proposal_row(engine, proposal_id)).status == "ready"


async def test_an_invalid_set_is_repaired_once(engine, op_runner, logged_in_client) -> None:
    """`set_invalid` serves five items: structurally valid, semantically not (§11.12: 6 to 16)."""
    fake.scenario("set_invalid", "ok")
    _, proposal_id, _ = await proposed(engine, logged_in_client)
    await op_runner.run_pending()
    proposal = await proposal_row(engine, proposal_id)
    assert (proposal.status, proposal.items) == ("ready", {"queries": QUERIES})
    rows = await tries(engine)
    assert [(r.logical_call_no, r.status, r.error_code, r.state) for r in rows] == [
        (1, "completed", None, "billed"),
        (2, "completed", None, "billed"),
    ]
    assert len(rows[0].output_json["queries"]) == fake.SET_INVALID_ITEMS
    assert proposal.llm_call_id == rows[1].call_id
    assert served() == 2


async def test_a_second_invalid_set_fails_the_proposal(engine, op_runner, logged_in_client) -> None:
    fake.scenario("set_invalid")  # the last name repeats: the repair is invalid too
    _, proposal_id, _ = await proposed(engine, logged_in_client)
    await op_runner.run_pending()
    await assert_failed_with_the_operation(engine, proposal_id, "invalid_output")
    assert served() == 2  # one repair, never a third call
    assert [r.logical_call_no for r in await tries(engine)] == [1, 2]
    r = await logged_in_client.get(f"{URL}/{proposal_id}")
    assert r.json() == {"status": "failed", "items": None}


async def test_a_structural_failure_is_repaired_once(engine, op_runner, logged_in_client) -> None:
    fake.scenario("parse_error", "ok")
    _, proposal_id, _ = await proposed(engine, logged_in_client)
    await op_runner.run_pending()
    assert (await proposal_row(engine, proposal_id)).status == "ready"
    assert [(r.logical_call_no, r.error_code) for r in await tries(engine)] == [
        (1, "invalid_output"),
        (2, None),
    ]


async def test_an_invalid_set_after_a_structural_failure_fails_the_proposal(
    engine, op_runner, logged_in_client
) -> None:
    fake.scenario("parse_error", "set_invalid")
    _, proposal_id, _ = await proposed(engine, logged_in_client)
    await op_runner.run_pending()
    await assert_failed_with_the_operation(engine, proposal_id, "invalid_output")
    assert served() == 2


@pytest.mark.parametrize(
    ("scenario", "code", "requests"),
    [("refusal", "refused", 1), ("model_mismatch", "model_mismatch", 1), ("http_401", "llm_request_401", 1)],
)
async def test_a_terminal_answer_fails_the_proposal_without_a_repair(
    engine, op_runner, logged_in_client, scenario: fake.Scenario, code: str, requests: int
) -> None:
    fake.scenario(scenario)
    _, proposal_id, _ = await proposed(engine, logged_in_client)
    await op_runner.run_pending()
    await assert_failed_with_the_operation(engine, proposal_id, code)
    assert served() == requests
    assert await count_rows(engine, "alerts", code="operation.failed") == 1


async def test_a_cap_below_the_worst_case_fails_budget_exhausted_with_no_request(
    engine, op_runner, logged_in_client
) -> None:
    """The call counts against the non-scoring share: A = cap - buffer = $0.10 is below its worst case."""
    await save_budget(engine, now=utcnow(), cap_usd="2.10")
    _, proposal_id, _ = await proposed(engine, logged_in_client)
    await op_runner.run_pending()
    await assert_failed_with_the_operation(engine, proposal_id, "budget_exhausted")
    assert served() == 0 and await count_rows(engine, "llm_calls") == 0


async def test_an_overloaded_api_requeues_the_operation(engine, op_runner, logged_in_client) -> None:
    fake.scenario("overloaded_529")
    _, proposal_id, op_id = await proposed(engine, logged_in_client)
    before = utcnow()
    await op_runner.run_one(op_id)
    after = utcnow()
    op = await operation_of(engine, proposal_id)
    assert (op.status, op.attempts, op.last_error_code) == ("pending", 1, "llm_transient")
    async with engine.connect() as conn:
        not_before = (
            await conn.execute(sa.text("SELECT not_before FROM operations WHERE id = :i"), {"i": op_id})
        ).scalar_one()
    assert before + timedelta(minutes=5) <= not_before <= after + timedelta(minutes=5)
    assert (await proposal_row(engine, proposal_id)).status == "running"  # the next attempt retries
    assert (await state_of(op_runner, proposal_id, op_id, 0))["outcome"] == "requeued"


async def test_the_last_transient_attempt_fails_the_proposal_through_the_hook(
    engine, op_runner, logged_in_client
) -> None:
    fake.scenario("overloaded_529")
    _, proposal_id, op_id = await proposed(engine, logged_in_client)
    async with engine.begin() as conn:
        await conn.execute(sa.text("UPDATE operations SET attempts = 9 WHERE id = :i"), {"i": op_id})
    await op_runner.run_one(op_id)
    proposal, op = await proposal_row(engine, proposal_id), await operation_of(engine, proposal_id)
    assert (op.status, op.last_error_code) == ("failed", "attempts_exhausted")
    assert proposal.status == "failed" and proposal.xmin == op.xmin
    assert (await state_of(op_runner, proposal_id, op_id, 9))["outcome"] == "failed"


async def test_the_hook_fails_a_running_proposal_and_tolerates_a_bad_payload(
    engine, logged_in_client, log_events
) -> None:
    _, proposal_id, op_id = await proposed(engine, logged_in_client)
    async with engine.begin() as conn:
        await proposal_failed(conn, op_id, {"proposal_id": "seven"}, None)  # logged, nothing moved
        await proposal_failed(conn, op_id, {}, None)
        await proposal_failed(conn, op_id, {"proposal_id": 2**63}, None)
    assert (await proposal_row(engine, proposal_id)).status == "running"
    invalid = [e for e in log_events() if e["event"] == "query_proposal_hook_payload_invalid"]
    assert [e["operation_id"] for e in invalid] == [op_id, op_id, op_id]
    async with engine.begin() as conn:
        await proposal_failed(conn, op_id, {"proposal_id": proposal_id}, None)  # after a reclaim: no code
    assert (await proposal_row(engine, proposal_id)).status == "failed"
    async with engine.begin() as conn:
        await proposal_failed(conn, op_id, {"proposal_id": proposal_id}, "refused")  # already failed: left
    assert (await proposal_row(engine, proposal_id)).status == "failed"


async def test_an_operation_whose_proposal_is_no_longer_running_is_obsolete(
    engine, op_runner, logged_in_client
) -> None:
    _, proposal_id, op_id = await proposed(engine, logged_in_client)
    async with engine.begin() as conn:
        await conn.execute(
            sa.text("UPDATE query_proposals SET status = 'failed' WHERE id = :p"), {"p": proposal_id}
        )
    before = (await proposal_row(engine, proposal_id)).xmin
    await op_runner.run_one(op_id)
    op = await operation_of(engine, proposal_id)
    assert (op.status, op.last_error_code) == ("succeeded", None)
    assert (await proposal_row(engine, proposal_id)).xmin == before  # untouched
    assert served() == 0 and await count_rows(engine, "llm_calls") == 0


async def test_two_successive_proposals_of_one_version_use_two_threads(
    engine, op_runner, logged_in_client
) -> None:
    version_id, first, first_op = await proposed(engine, logged_in_client)
    await op_runner.run_pending()
    second = await propose(logged_in_client)  # the first is ready: a new proposal
    second_op = int((await operation_of(engine, second)).id)
    assert second != first and second_op != first_op
    await op_runner.run_pending()
    rows = [await proposal_row(engine, p) for p in (first, second)]
    assert [(r.profile_version_id, r.status) for r in rows] == [(version_id, "ready"), (version_id, "ready")]
    one = await state_of(op_runner, first, first_op, 0)
    two = await state_of(op_runner, second, second_op, 0)
    assert (one["proposal_id"], one["steps"]) == (first, STEPS)
    assert (two["proposal_id"], two["steps"]) == (second, STEPS)  # nothing of the first thread
    assert thread_id("q", first, first_op, 0) != thread_id("q", second, second_op, 0)
    assert served() == 2


# --- receipts: a reclaimed execution reuses what was paid ---------------------------------------------------


async def _reclaim_during(
    op_runner: OpRunner,
    engine: AsyncEngine,
    op_id: int,
    pause: PausePoint,
    meanwhile: Callable[[], Awaitable[object]] | None = None,
) -> None:
    """Execution 1 of `op_id` held at `pause`, `meanwhile` (a change while it is held), the reconciler's
    reclaim, execution 2 to its end, then execution 1 resumed: its next owner write is fenced, and it writes
    nothing."""
    first = asyncio.create_task(op_runner.run_one(op_id, pause=pause))
    await asyncio.wait_for(pause.reached.wait(), 60)
    if meanwhile is not None:
        await meanwhile()
    await reclaim_stale_now(engine, op_id)
    await op_runner.run_one(op_id)
    pause.resume()
    await asyncio.wait_for(first, 30)


async def test_a_crash_after_settlement_publishes_without_a_new_request(
    engine, op_runner, logged_in_client
) -> None:
    """The fault point lies after the settled call and before `running → ready`."""
    _, proposal_id, op_id = await proposed(engine, logged_in_client)
    pause = PausePoint(AFTER_CALL)
    first = asyncio.create_task(op_runner.run_one(op_id, pause=pause))
    await asyncio.wait_for(pause.reached.wait(), 60)
    assert (await proposal_row(engine, proposal_id)).status == "running"
    await reclaim_stale_now(engine, op_id)
    await op_runner.run_one(op_id)
    proposal = await proposal_row(engine, proposal_id)
    assert (proposal.status, proposal.items) == ("ready", {"queries": QUERIES})
    assert served() == 1 and await ledger_states(engine, op_id) == ["billed"]
    pause.resume()
    await asyncio.wait_for(first, 30)
    assert (
        await proposal_row(engine, proposal_id)
    ).xmin == proposal.xmin  # the first execution wrote nothing
    op = await operation_of(engine, proposal_id)
    assert (op.status, op.attempts) == ("succeeded", 1)
    assert (await state_of(op_runner, proposal_id, op_id, 1))["steps"] == STEPS


async def test_a_reclaim_after_an_invalid_set_makes_only_the_repair(
    engine, op_runner, logged_in_client
) -> None:
    fake.scenario("set_invalid", "ok")
    _, proposal_id, op_id = await proposed(engine, logged_in_client)
    await _reclaim_during(op_runner, engine, op_id, PausePoint(AFTER_CALL))
    assert (await proposal_row(engine, proposal_id)).status == "ready"
    assert served() == 2  # call 1 before the crash, the repair after it: exactly one request on resume
    assert [r.logical_call_no for r in await tries(engine)] == [1, 2]
    assert (await operation_of(engine, proposal_id)).attempts == 1


async def test_a_reclaim_after_a_valid_repair_makes_no_request(engine, op_runner, logged_in_client) -> None:
    fake.scenario("set_invalid", "ok")
    _, proposal_id, op_id = await proposed(engine, logged_in_client)
    await _reclaim_during(op_runner, engine, op_id, PausePoint(AFTER_CALL, arrival=2))
    proposal = await proposal_row(engine, proposal_id)
    assert proposal.status == "ready"
    assert served() == 2  # no request after the reclaim
    rows = await tries(engine)
    assert [r.logical_call_no for r in rows] == [1, 2] and proposal.llm_call_id == rows[1].call_id


async def test_a_reclaim_after_an_invalid_repair_fails_without_a_request(
    engine, op_runner, logged_in_client
) -> None:
    fake.scenario("set_invalid")
    _, proposal_id, op_id = await proposed(engine, logged_in_client)
    await _reclaim_during(op_runner, engine, op_id, PausePoint(AFTER_CALL, arrival=2))
    proposal, op = await proposal_row(engine, proposal_id), await operation_of(engine, proposal_id)
    assert (proposal.status, op.status, op.last_error_code, op.attempts) == (
        "failed",
        "failed",
        "invalid_output",
        1,
    )
    assert served() == 2


async def test_a_reclaim_after_a_refusal_fails_without_a_request(engine, op_runner, logged_in_client) -> None:
    fake.scenario("refusal")
    _, proposal_id, op_id = await proposed(engine, logged_in_client)
    await _reclaim_during(op_runner, engine, op_id, PausePoint(AFTER_CALL))
    op = await operation_of(engine, proposal_id)
    assert ((await proposal_row(engine, proposal_id)).status, op.last_error_code) == ("failed", "refused")
    assert served() == 1


async def test_a_scope_saved_between_a_crash_and_the_reclaim_leaves_a_usable_receipt_usable(
    engine, op_runner, logged_in_client
) -> None:
    """The receipt is checked against the scope pinned at the request, not the one active at the resume: under
    the new scope the paid set would be invalid, and its repair paid for."""
    _, proposal_id, op_id = await proposed(engine, logged_in_client)
    # a synthetic label whose word four of the eight stored queries hold: four kept items are too few
    backend_scope = [{**BERLIN, "label": "Backend"}]
    stored = QueryProposalOut.model_validate(SYNTHETIC_QUERIES)
    under_new_scope = location_words(["Backend"], Gazetteer.load())
    assert validate_proposal(stored, location_words=under_new_scope) == ([], ["set_invalid"])

    async def save_scope() -> None:
        await activate_scope(engine, backend_scope)

    await _reclaim_during(op_runner, engine, op_id, PausePoint(AFTER_CALL), meanwhile=save_scope)
    proposal = await proposal_row(engine, proposal_id)
    assert (proposal.status, proposal.items) == ("ready", {"queries": QUERIES})
    assert served() == 1 and [r.logical_call_no for r in await tries(engine)] == [1]  # no repair paid
    assert await ledger_states(engine, op_id) == ["billed"]
    assert (await operation_of(engine, proposal_id)).attempts == 1


# --- a purged profile ------------------------------------------------------------------------------------


async def test_a_purged_profile_fails_the_proposal_without_a_request(
    engine, op_runner, logged_in_client
) -> None:
    version_id, proposal_id, _ = await proposed(engine, logged_in_client)
    await purge_profile(engine, version_id)
    await op_runner.run_pending()
    await assert_failed_with_the_operation(engine, proposal_id, "profile_unavailable")
    assert served() == 0 and await count_rows(engine, "llm_calls") == 0


async def test_a_purge_after_an_invalid_set_fails_without_paying_the_repair(
    engine, op_runner, logged_in_client
) -> None:
    fake.scenario("set_invalid", "ok")
    version_id, proposal_id, op_id = await proposed(engine, logged_in_client)

    async def purge() -> None:
        await purge_profile(engine, version_id)

    await _reclaim_during(op_runner, engine, op_id, PausePoint(AFTER_CALL), meanwhile=purge)
    proposal, op = await proposal_row(engine, proposal_id), await operation_of(engine, proposal_id)
    assert (proposal.status, op.status, op.last_error_code, op.attempts) == (
        "failed",
        "failed",
        "profile_unavailable",
        1,
    )
    assert served() == 1 and [r.logical_call_no for r in await tries(engine)] == [1]


async def test_a_purge_after_a_usable_set_still_publishes_it(engine, op_runner, logged_in_client) -> None:
    """The paid set needs no request, so the purged profile is never read."""
    version_id, proposal_id, op_id = await proposed(engine, logged_in_client)

    async def purge() -> None:
        await purge_profile(engine, version_id)

    await _reclaim_during(op_runner, engine, op_id, PausePoint(AFTER_CALL), meanwhile=purge)
    proposal = await proposal_row(engine, proposal_id)
    assert (proposal.status, proposal.items) == ("ready", {"queries": QUERIES})
    assert served() == 1


# --- no content outside the content tables --------------------------------------------------------------


async def test_no_query_text_or_profile_value_reaches_a_log_or_a_checkpoint(
    engine, op_runner, logged_in_client, log_events
) -> None:
    fake.scenario("set_invalid", "ok")
    _, proposal_id, _ = await proposed(engine, logged_in_client)
    await op_runner.run_pending()
    assert (await proposal_row(engine, proposal_id)).status == "ready"
    async with engine.connect() as conn:
        blobs: list[str] = []
        for table in CHECKPOINT_TABLES:
            rows = await conn.execute(sa.text(f"SELECT * FROM {table}"))  # noqa: S608 - constant names
            blobs.extend(json.dumps(list(row), default=repr, ensure_ascii=False) for row in rows)
    assert blobs, "the scan must see the thread's checkpoints"
    events = log_events()
    rendered = json.dumps(events, ensure_ascii=False)
    for word in (*CONTENT_WORDS, *(q["text"] for q in QUERIES)):
        assert all(word not in blob for blob in blobs), word
        assert word not in rendered, word
    [ready] = [e for e in events if e["event"] == "query_proposal_ready"]
    assert (ready["proposal_id"], ready["items"], ready["logical_call_no"]) == (proposal_id, len(QUERIES), 2)
    assert proposals.KIND == "propose_queries"
