"""The node modules of the `score_target` subgraph (spec §9.4.1; plan Task 9) and what they share; each
`make_*(deps)` builds its node.

A node is called with the state and the invoke config (`StateNode`) and returns a `Command`: its update and
the step the routing gives (`scoring.routing`, through `goto`). It reads ids from the state and artifacts
from the tables; no draft, note, ad or profile text ever enters the state, a log line or a checkpoint.

What the nodes share:
- `owner_of(state)`: the attempt's spend owner (`TargetAttemptOwner`), whose lock is the claim check of every
  write (§9.4.3: the target `FOR SHARE` under `claim_id`, `attempt_no` and `state = 'in_progress'`, else
  `ClaimLost`);
- `load_inputs(conn, state)`: the attempt's `scoring.steps.ScoreInputs` and its `AttemptRow`, read without a
  lock (`scoring.attempt_inputs.load_attempt_inputs` at the state's prompt-schema version, which
  `load_context` set from the tuple);
- `TargetDraftStore`: the `scoring.steps.DraftStore` of the `score_drafts` (`scoring.drafts`' statements) and
  `critic_notes` tables. Each
  write is one transaction after the owner lock (step 5 of the lock schedule, then the domain rows, step
  10), unless the store is bound to a caller's transaction (`validate`'s, which holds `active_config` and the
  owner lock already). A generator draft (round 0) is written between the fault points
  `score.generate.before_draft` (in its transaction, before the owner lock: a crash there loses the billed
  call's output, C10) and `score.generate.after_draft` (after the commit, C11). The draft's `validation`
  starts as `{"normalisation": …}` (codes and keys only) and its other NOT NULL columns as placeholders until
  `validate` fills them; `validate` sets `validation = validation || {"current": …}` and adds
  `corrections` only while the draft has none, so the first validation's corrections are kept verbatim;
- `score_caller(deps, state, inputs, attempt)`: the gateway call of one logical call of the attempt, at the
  attempt's pinned configuration (`pinned_node_config`, with the critic's per-round effort): the spend owner,
  `purpose_class='scoring'`, the pinned execution config, `deadline = monotonic() + deadline_s`, the attempt,
  the target, the round, `logical_call_no = score_attempts.logical_calls + 1` (read when the call is made),
  the generator's `start_stage`, and the funded follow-up's `bulk_operation_id` (`None` for every other
  attempt);
- `goto(next, update)` and `failure(error, state, node)`: the `Command` of a routing decision, and the
  route to `fail` with the error's code (`llm_step.error_code_of`); the worker's own `FencedOut` propagates.
"""

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any, Final, Protocol, get_args
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph.types import Command
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from jobhunter.db.sql import q
from jobhunter.errors import FencedOut
from jobhunter.faults import afault_point
from jobhunter.graphs.score.deps import ScoreDeps
from jobhunter.graphs.score.llm_step import error_code_of
from jobhunter.graphs.score.state import CycleState, CycleUpdate, TargetState, TargetUpdate
from jobhunter.llm.errors import LLMError
from jobhunter.llm.gateway import CallContext, GatewayResult, RenderedPrompt, pinned_node_config
from jobhunter.llm.pricing import Effort
from jobhunter.llm.prompts.render import ScoreRole, score_schema
from jobhunter.llm.spend_ledger import TargetAttemptOwner
from jobhunter.observability.logging import get_logger
from jobhunter.scoring.attempt_inputs import AttemptRow, load_attempt_inputs
from jobhunter.scoring.drafts import (
    INSERT_DRAFT,
    READ_DRAFT,
    SAVE_VALIDATION,
    draft_params,
    validation_params,
)
from jobhunter.scoring.routing import DerivedOutcome, Next, Route
from jobhunter.scoring.steps import Caller, NoteRecord, ScoreInputs, StoredDraft, StoredNote, ValidationRecord
from jobhunter.scoring.validator import ParsedGenerator

BEFORE_DRAFT: Final = "score.generate.before_draft"
AFTER_DRAFT: Final = "score.generate.after_draft"
_OUTCOMES: Final[Mapping[str, DerivedOutcome]] = {o: o for o in get_args(DerivedOutcome)}

log = get_logger("jobhunter.graphs.score")


class StateNode[U](Protocol):
    """A node of the subgraph: the state and the invoke config, to `U`."""

    async def __call__(self, state: TargetState, config: RunnableConfig) -> U: ...


type Node = StateNode[Command[Route]]


class CycleStateNode[S](Protocol):
    """A node of the cycle graph (Task 11): its input (the cycle's state, or the claim a `Send` carries) and
    the invoke config, to its partial update of the cycle's state."""

    async def __call__(self, state: S, config: RunnableConfig) -> CycleUpdate: ...


type CycleNode = CycleStateNode[CycleState]


def derived_outcome(value: object) -> DerivedOutcome:
    """A stored `critic_notes.derived_outcome`; `ValueError` for anything else."""
    outcome = _OUTCOMES.get(value) if isinstance(value, str) else None
    if outcome is None:
        raise ValueError("an unknown derived outcome")
    return outcome


def owner_of(state: TargetState) -> TargetAttemptOwner:
    """The attempt's spend owner, from the state's claim (module docstring)."""
    return TargetAttemptOwner(
        target_id=state["target_id"],
        claim_id=UUID(state["claim_id"]),
        attempt_no=state["attempt_no"],
        attempt_id=state["attempt_id"],
    )


def goto(next_step: Next, update: TargetUpdate) -> Command[Route]:
    """The `Command` of a routing decision: `update`, plus the repair, the publish mode or the error code it
    sets."""
    full: dict[str, Any] = dict(update)
    if next_step.repair:
        full["repair_used"] = True
    if next_step.publish_mode is not None:
        full["publish_mode"] = next_step.publish_mode
    if next_step.error_code is not None:
        full["error_code"] = next_step.error_code
    return Command(goto=next_step.route, update=full)


def failure(error: LLMError | FencedOut, state: TargetState, node: str) -> Command[Route]:
    """The route to `fail` with `error`'s code (`llm_step`); the worker's own `FencedOut` is raised again."""
    if isinstance(error, FencedOut) and error.worker_superseded:
        raise error
    code = error_code_of(error)
    log.info(
        "score_step_failed",
        node=node,
        target_id=state["target_id"],
        attempt_id=state["attempt_id"],
        error_code=code,
        error_class=type(error).__name__,
    )
    return Command(goto="fail", update={"error_code": code})


# ---- the inputs --------------------------------------------------------------------------------------------


async def load_inputs(conn: AsyncConnection, state: TargetState) -> tuple[ScoreInputs, AttemptRow]:
    """The attempt's inputs and ids (module docstring), on the caller's connection; `LookupError` when the
    target, its attempt or the profile version's profile is gone."""
    return await load_attempt_inputs(
        conn,
        target_id=state["target_id"],
        attempt_id=state["attempt_id"],
        prompt_schema_version_id=state["prompt_schema_version_id"],
    )


# ---- the calls ---------------------------------------------------------------------------------------------

_LOGICAL_CALLS: Final = q("SELECT logical_calls FROM score_attempts WHERE id = :a")


def score_caller(deps: ScoreDeps, state: TargetState, inputs: ScoreInputs, attempt: AttemptRow) -> Caller:
    """The gateway call of one logical call of the attempt (module docstring)."""
    owner = owner_of(state)

    async def call(
        role: ScoreRole, prompt: RenderedPrompt, *, round: int, effort: Effort | None = None
    ) -> GatewayResult:
        async with deps.engine.connect() as conn:
            node = await pinned_node_config(
                conn,
                role,
                model_policy_id=attempt.model_policy_id,
                execution_config_id=attempt.execution_config_id,
                schema=score_schema(role, inputs.version),
                effort=effort,
            )
            logical_calls = int((await conn.execute(_LOGICAL_CALLS, {"a": owner.attempt_id})).scalar_one())
        ctx = CallContext(
            owner=owner,
            purpose_class="scoring",
            execution_config_id=attempt.execution_config_id,
            deadline=deps.monotonic() + node.deadline_s,
            logical_call_no=logical_calls + 1,
            start_stage=state["start_stage"] if role == "generate" else "first",
            attempt_id=owner.attempt_id,
            target_id=owner.target_id,
            round=round,
            bulk_operation_id=state["funded_bulk_operation_id"],
        )
        result = await deps.gateway.call(node, prompt, ctx)
        log.info(
            "score_called",
            purpose=role,
            target_id=owner.target_id,
            attempt_id=owner.attempt_id,
            llm_call_id=result.llm_call_id,
            logical_call_no=ctx.logical_call_no,
            round=round,
            effort=node.effort,
            cost_usd=str(result.cost_usd),
        )
        return result

    return call


# ---- the store ---------------------------------------------------------------------------------------------

_INSERT_NOTE: Final = q(
    "INSERT INTO critic_notes (attempt_id, round, draft_id, llm_call_id, effort, triggers, verdict_returned, "
    "derived_outcome, review_json, validation) VALUES (:attempt, :round, :draft, :call, :effort, "
    "CAST(:triggers AS text[]), :verdict, :outcome, :review, :validation) RETURNING id",
    jsonb=["review", "validation"],
)
_NOTE: Final = q(
    "SELECT id, draft_id, derived_outcome, review_json, validation FROM critic_notes WHERE id = :n"
)


class TargetDraftStore:
    """The `DraftStore` of one score attempt (module docstring): over the engine, or bound to a caller's
    transaction (`conn`) that already holds the owner lock."""

    def __init__(
        self, engine: AsyncEngine, owner: TargetAttemptOwner, *, conn: AsyncConnection | None = None
    ) -> None:
        self._engine = engine
        self._owner = owner
        self._conn = conn

    @asynccontextmanager
    async def _reading(self) -> AsyncIterator[AsyncConnection]:
        if self._conn is not None:
            yield self._conn
            return
        async with self._engine.connect() as conn:
            yield conn

    @asynccontextmanager
    async def _writing(self) -> AsyncIterator[AsyncConnection]:
        if self._conn is not None:
            yield self._conn
            return
        async with self._engine.begin() as conn:
            await self._owner.lock(conn)
            yield conn

    async def add_draft(self, *, round: int, llm_call_id: int, parsed: ParsedGenerator) -> int:
        generated = round == 0
        params = draft_params(
            attempt_id=self._owner.attempt_id, round=round, llm_call_id=llm_call_id, parsed=parsed
        )
        async with self._engine.begin() as conn:
            if generated:
                await afault_point(BEFORE_DRAFT)
            await self._owner.lock(conn)
            draft_id = int((await conn.execute(INSERT_DRAFT, params)).scalar_one())
        if generated:
            await afault_point(AFTER_DRAFT)
        log.info(
            "score_draft_stored",
            target_id=self._owner.target_id,
            attempt_id=self._owner.attempt_id,
            draft_id=draft_id,
            round=round,
            llm_call_id=llm_call_id,
        )
        return draft_id

    async def draft(self, draft_id: int) -> StoredDraft:
        async with self._reading() as conn:
            row = (await conn.execute(READ_DRAFT, {"d": draft_id})).first()
        if row is None:
            raise LookupError(f"score draft {draft_id} does not exist")
        return StoredDraft.from_row(
            id=int(row.id), round=int(row.round), output_json=row.output_json, validation=row.validation
        )

    async def save_validation(self, draft_id: int, record: ValidationRecord) -> None:
        async with self._writing() as conn:
            await conn.execute(SAVE_VALIDATION, validation_params(draft_id, record))

    async def add_note(self, note: NoteRecord) -> int:
        params = {
            "attempt": self._owner.attempt_id,
            "round": note.round,
            "draft": note.draft_id,
            "call": note.llm_call_id,
            "effort": note.effort,
            "triggers": note.triggers,
            "verdict": note.verdict_returned,
            "outcome": note.derived_outcome,
            "review": note.review_json,
            "validation": note.validation,
        }
        async with self._writing() as conn:
            note_id = int((await conn.execute(_INSERT_NOTE, params)).scalar_one())
        log.info(
            "score_critic_note_stored",
            target_id=self._owner.target_id,
            attempt_id=self._owner.attempt_id,
            note_id=note_id,
            draft_id=note.draft_id,
            round=note.round,
            effort=note.effort,
            derived_outcome=note.derived_outcome,
            verdict_returned=note.verdict_returned,
        )
        return note_id

    async def note(self, note_id: int) -> StoredNote:
        async with self._reading() as conn:
            row = (await conn.execute(_NOTE, {"n": note_id})).first()
        if row is None:
            raise LookupError(f"critic note {note_id} does not exist")
        return StoredNote.from_row(
            id=int(row.id),
            draft_id=int(row.draft_id),
            derived_outcome=str(row.derived_outcome),
            review_json=row.review_json,
            validation=row.validation,
        )
