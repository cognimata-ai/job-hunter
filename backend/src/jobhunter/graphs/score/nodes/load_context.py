"""The `load_context` node (spec §9.4.1, §9.4.3, §10.3; plan Task 9, Deviation 17): the claim check, the
already-published check, the prompt version, and the attempt's start from the database's artifacts.

In one transaction:
1. The attempt row (`score_attempts`, by id under its target and number): its pinned evaluation policy and
   execution config (A2 T5: a reclaim creates a new attempt, which pinned the active configuration at its
   claim, L3491).
2. The target under the claim predicate, `FOR SHARE OF t`, with `targets.RESUMABLE_DRAFT_SQL` as its column
   `resumable` (bound to the attempt's policy; a batch follow-up's `resume_draft_id` holds under any policy,
   spec L3589). No row: the claim is lost; the node ends the attempt with `outcome = "claim_lost"` and writes
   nothing.
3. The tuple; an `llm_results` row of the 5-tuple (revision, profile version, rubric, prompt-schema version,
   model policy) is `outcome = "already_published"`, which `publish` returns without inserting (L3523,
   defensive; Task 10).
4. The tuple's prompt-schema version (`render.load_prompt_version`): one whose family cannot score
   (`generator_v1`) routes to `fail` with `prompt_version_unscorable`, a transient error without a charge.
5. `start_stage` (M2 Carry-forward J2, A2 T17): `enlarged` when the newest call of the previous attempt's
   generator (`spend_ledger.last_call_of_attempt`) stopped at `max_tokens`, or the target's `state_reason`
   is `batch_max_tokens`, at a `max_tokens` at least the pinned first stage's; else `first`.
6. `funded_bulk_operation_id` (Task 7's rule, Codex r1 #27): `spend_ledger.funded_bulk_operation_id`, once.
7. When `resumable`, the resume over the attempt chain (Codex r2 B23, r3 B8): the newest earlier attempt and
   every attempt reachable from it through `resumed_from_attempt_id`, the attempt of the target's
   `resume_draft_id` (a batch follow-up) and this attempt, those under the attempt's evaluation policy and the
   attempt of `resume_draft_id` whatever its policy (spec L3589: the batch draft is resumed and `validate`
   re-runs the triggers under this attempt's policy, plan L942, Deviation 27; final review, Fable I1). The
   newest draft of the chain is the current one (`current_draft_id`, `round`); `critic_rounds` counts the
   critic notes on the chain's drafts (so T0 and the three-round limit survive a revision committed by one
   attempt and reviewed by another); the newest note gives `last_critic_note_id`, its derived outcome, the
   draft it reviewed and `not_a_job_ad_verified` (its stored fact confirmations are the chain's, applied in
   order: each note stores the confirmations after its round). A chain without a draft starts fresh.

`repair_used` is `False` on every attempt (A2 T6). The routing (`routing.after_load_context`) then goes to
`END`, `publish`, `fail`, `validate` (a resume) or `generate`.
"""

from dataclasses import dataclass
from typing import Final

from langchain_core.runnables import RunnableConfig
from langgraph.types import Command
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q
from jobhunter.errors import InvariantViolation
from jobhunter.graphs.score.deps import ScoreDeps
from jobhunter.graphs.score.llm_step import PROMPT_VERSION_UNSCORABLE
from jobhunter.graphs.score.nodes import Node, derived_outcome, goto, log
from jobhunter.graphs.score.state import TargetState, TargetUpdate
from jobhunter.llm import spend_ledger
from jobhunter.llm.gateway import MAX_TOKENS, MAX_TOKENS_FIRST_STAGE, Stage, pinned_node_config
from jobhunter.llm.prompts.render import SCORABLE_FAMILIES, PromptVersion, load_prompt_version, score_schema
from jobhunter.scoring import routing
from jobhunter.scoring.routing import ALREADY_PUBLISHED, CLAIM_LOST, Route
from jobhunter.scoring.targets import RESUMABLE_DRAFT_SQL

NAME: Final = "load_context"
BATCH_MAX_TOKENS: Final = "batch_max_tokens"  # a batch item stopped at max_tokens (A2 T17)
_MAX_TOKENS_CODES: Final = frozenset({MAX_TOKENS_FIRST_STAGE, MAX_TOKENS})

_ATTEMPT: Final = q(
    "SELECT evaluation_policy_id, execution_config_id FROM score_attempts "
    "WHERE id = :attempt AND target_id = :t AND attempt_no = :a"
)
_LOCK_TARGET: Final = q(
    "SELECT t.job_revision_id, t.llm_tuple_id, t.resume_draft_id, t.state_reason, "  # noqa: S608 (a constant)
    f"({RESUMABLE_DRAFT_SQL}) AS resumable FROM score_targets t "
    "WHERE t.id = :t AND t.claim_id = :c AND t.attempt_no = :a AND t.state = 'in_progress' FOR SHARE OF t"
)
_TUPLE: Final = q(
    "SELECT profile_version_id, rubric_version_id, prompt_schema_version_id, model_policy_id "
    "FROM llm_tuples WHERE id = :tuple"
)
_PUBLISHED: Final = q(
    "SELECT id FROM llm_results WHERE job_revision_id = :revision AND profile_version_id = :profile "
    "AND rubric_version_id = :rubric AND prompt_schema_version_id = :prompt AND model_policy_id = :policy"
)
_PREVIOUS_ATTEMPT: Final = q(
    "SELECT id FROM score_attempts WHERE target_id = :t AND attempt_no < :a ORDER BY attempt_no DESC LIMIT 1"
)
# The chain (module docstring, step 7): its attempts under the policy and the batch follow-up's, by id.
_CHAIN: Final = q(
    "WITH RECURSIVE chain(id) AS ("
    "SELECT s.id FROM ((SELECT id FROM score_attempts WHERE target_id = :t AND attempt_no < :a "
    "ORDER BY attempt_no DESC LIMIT 1) "
    "UNION SELECT d.attempt_id FROM score_drafts d WHERE d.id = CAST(:resume_draft AS bigint) "
    "UNION SELECT CAST(:attempt AS bigint)) s "
    "UNION SELECT a.resumed_from_attempt_id FROM score_attempts a JOIN chain c ON a.id = c.id "
    "WHERE a.resumed_from_attempt_id IS NOT NULL) "
    "SELECT a.id FROM chain c JOIN score_attempts a ON a.id = c.id WHERE a.target_id = :t "
    "AND (a.evaluation_policy_id = :policy "
    "OR a.id = (SELECT d.attempt_id FROM score_drafts d WHERE d.id = CAST(:resume_draft AS bigint))) "
    "ORDER BY a.id"
)
_CURRENT_DRAFT: Final = q(
    "SELECT id, round FROM score_drafts WHERE attempt_id = ANY(CAST(:chain AS bigint[])) "
    "ORDER BY id DESC LIMIT 1"
)
_NOTES: Final = q(
    "SELECT count(*) OVER () AS rounds, n.id, n.draft_id, n.derived_outcome, "
    "coalesce(n.validation ->> 'not_a_job_ad_verified' = 'true', false) AS verified "
    "FROM critic_notes n JOIN score_drafts d ON d.id = n.draft_id "
    "WHERE d.attempt_id = ANY(CAST(:chain AS bigint[])) ORDER BY n.id DESC LIMIT 1"
)


@dataclass(frozen=True)
class _Resumed:
    """The chain's state (module docstring, step 7)."""

    current_draft_id: int
    round: int
    critic_rounds: int
    last_note: Row[tuple[object, ...]] | None


async def _start_stage(
    conn: AsyncConnection,
    target: Row[tuple[object, ...]],
    state: TargetState,
    version: PromptVersion,
    model_policy_id: int,
    execution_config_id: int,
) -> Stage:
    """Step 5 of the module docstring."""
    previous = (
        await conn.execute(_PREVIOUS_ATTEMPT, {"t": state["target_id"], "a": state["attempt_no"]})
    ).scalar_one_or_none()
    last = (
        None
        if previous is None
        else await spend_ledger.last_call_of_attempt(conn, attempt_id=int(previous), purpose="generate")
    )
    stopped = target.state_reason == BATCH_MAX_TOKENS or (
        last is not None and last.error_code in _MAX_TOKENS_CODES
    )
    if not stopped:
        return "first"
    node = await pinned_node_config(
        conn,
        "generate",
        model_policy_id=model_policy_id,
        execution_config_id=execution_config_id,
        schema=score_schema("generate", version),
    )
    return "enlarged" if last is None or last.max_tokens >= node.max_tokens else "first"


async def _resume(
    conn: AsyncConnection, target: Row[tuple[object, ...]], state: TargetState, policy_id: int
) -> _Resumed | None:
    """Step 7 of the module docstring; `None` when the chain has no draft."""
    params = {
        "t": state["target_id"],
        "a": state["attempt_no"],
        "attempt": state["attempt_id"],
        "resume_draft": target.resume_draft_id,
        "policy": policy_id,
    }
    chain = [int(row.id) for row in await conn.execute(_CHAIN, params)]
    current = (await conn.execute(_CURRENT_DRAFT, {"chain": chain})).first()
    if current is None:
        return None
    note = (await conn.execute(_NOTES, {"chain": chain})).first()
    rounds = 0 if note is None else int(note.rounds)
    return _Resumed(int(current.id), int(current.round), rounds, note)


def _restored(resumed: _Resumed) -> TargetUpdate:
    note = resumed.last_note
    update: TargetUpdate = {
        "resumed": True,
        "current_draft_id": resumed.current_draft_id,
        "round": resumed.round,
        "critic_rounds": resumed.critic_rounds,
    }
    if note is not None:
        update |= {
            "last_critic_note_id": int(note.id),
            "last_critic_outcome": derived_outcome(note.derived_outcome),
            "last_critic_draft_id": int(note.draft_id),
            "not_a_job_ad_verified": bool(note.verified),
        }
    return update


def make_load_context(deps: ScoreDeps) -> Node:
    async def load_context(state: TargetState, config: RunnableConfig) -> Command[Route]:
        target_id, attempt_id = state["target_id"], state["attempt_id"]
        claim = {"t": target_id, "c": state["claim_id"], "a": state["attempt_no"]}
        async with deps.engine.begin() as conn:
            attempt_params = {"t": target_id, "a": state["attempt_no"], "attempt": attempt_id}
            attempt = (await conn.execute(_ATTEMPT, attempt_params)).first()
            if attempt is None:
                raise InvariantViolation(f"score target {target_id} has no attempt {attempt_id} of its claim")
            policy_id = int(attempt.evaluation_policy_id)
            target = (await conn.execute(_LOCK_TARGET, {**claim, "policy": policy_id})).first()
            if target is None:
                log.info("score_claim_lost", node=NAME, target_id=target_id, attempt_id=attempt_id)
                return goto(routing.Next(routing.END), {"outcome": CLAIM_LOST})
            ids = (await conn.execute(_TUPLE, {"tuple": target.llm_tuple_id})).one()
            update: TargetUpdate = {
                "prompt_schema_version_id": int(ids.prompt_schema_version_id),
                "repair_used": False,
                "resumed": False,
            }
            published = (
                await conn.execute(
                    _PUBLISHED,
                    {
                        "revision": target.job_revision_id,
                        "profile": ids.profile_version_id,
                        "rubric": ids.rubric_version_id,
                        "prompt": ids.prompt_schema_version_id,
                        "policy": ids.model_policy_id,
                    },
                )
            ).scalar_one_or_none()
            if published is not None:
                update["outcome"] = ALREADY_PUBLISHED
                log.info("score_already_published", target_id=target_id, llm_result_id=published)
                return goto(
                    routing.after_load_context(outcome=ALREADY_PUBLISHED, error_code=None, resumed=False),
                    update,
                )
            version = await load_prompt_version(conn, int(ids.prompt_schema_version_id))
            if version.parser_family not in SCORABLE_FAMILIES:
                log.warning(
                    "score_prompt_version_unscorable",
                    target_id=target_id,
                    prompt_schema_version_id=version.id,
                    parser_family=version.parser_family,
                )
                nxt = routing.after_load_context(
                    outcome=None, error_code=PROMPT_VERSION_UNSCORABLE, resumed=False
                )
                return goto(nxt, update)
            update["start_stage"] = await _start_stage(
                conn,
                target,
                state,
                version,
                int(ids.model_policy_id),
                int(attempt.execution_config_id),
            )
            update["funded_bulk_operation_id"] = await spend_ledger.funded_bulk_operation_id(
                conn, target_id=target_id
            )
            resumed = await _resume(conn, target, state, policy_id) if target.resumable else None
        if resumed is not None:
            update |= _restored(resumed)
        log.info(
            "score_context_loaded",
            target_id=target_id,
            attempt_id=attempt_id,
            resumed=resumed is not None,
            current_draft_id=update.get("current_draft_id"),
            round=update.get("round", 0),
            critic_rounds=update.get("critic_rounds", 0),
            start_stage=update["start_stage"],
            funded_bulk_operation_id=update["funded_bulk_operation_id"],
        )
        nxt = routing.after_load_context(outcome=None, error_code=None, resumed=resumed is not None)
        return goto(nxt, update)

    return load_context
