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
  lock: the state's prompt-schema version (`load_context` set it from the tuple; the renderer's cache serves
  it), the tuple's rubric and profile version, the revision's `<job_ad>` block (the material projection
  `job_revisions.llm_input` and the revision's description, which every evidence offset refers to), the
  job's committed `DetFacts`, the attempt's pinned evaluation policy and execution config, and the model
  policy's critic efforts. The ids every prompt records are the revision's, the profile version's and the
  rubric version's;
- `TargetDraftStore`: the `scoring.steps.DraftStore` of the `score_drafts` and `critic_notes` tables. Each
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
from dataclasses import dataclass
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
from jobhunter.llm.prompts.render import ScoreRole, load_prompt_version, score_schema
from jobhunter.llm.schemas.profile import ConfirmedProfile
from jobhunter.llm.spend_ledger import TargetAttemptOwner
from jobhunter.observability.logging import get_logger
from jobhunter.scoring import evaluation_policy
from jobhunter.scoring.filters import read_det_facts
from jobhunter.scoring.routing import DerivedOutcome, Next, Route
from jobhunter.scoring.steps import (
    Caller,
    NoteRecord,
    ScoreInputs,
    StoredDraft,
    StoredNote,
    ValidationRecord,
    material_strings,
    revision_ad,
    rubric_of,
    score_refs,
)
from jobhunter.scoring.triggers import sample_key
from jobhunter.scoring.validator import ParsedGenerator
from jobhunter.types import JSONValue, get_str

BEFORE_DRAFT: Final = "score.generate.before_draft"
AFTER_DRAFT: Final = "score.generate.after_draft"
_EFFORTS: Final[Mapping[str, Effort]] = {effort: effort for effort in get_args(Effort)}
_OUTCOMES: Final[Mapping[str, DerivedOutcome]] = {o: o for o in get_args(DerivedOutcome)}
_STRUCTURED: Final = "structured"

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


@dataclass(frozen=True)
class AttemptRow:
    """What a node reads of its target, tuple and attempt (ids only)."""

    job_id: int
    job_revision_id: int
    llm_tuple_id: int
    profile_version_id: int
    rubric_version_id: int
    model_policy_id: int
    execution_config_id: int
    evaluation_policy_id: int


_INPUTS: Final = q(
    "SELECT t.job_id, t.job_revision_id, t.llm_tuple_id, u.profile_version_id, u.rubric_version_id, "
    "u.model_policy_id, a.execution_config_id, a.evaluation_policy_id, "
    "r.llm_input, r.description_text, r.description_completeness, v.profile_json, w.weights, w.anchors, "
    "m.payload AS model_policy "
    "FROM score_targets t JOIN llm_tuples u ON u.id = t.llm_tuple_id "
    "JOIN score_attempts a ON a.id = :attempt AND a.target_id = t.id "
    "JOIN job_revisions r ON r.id = t.job_revision_id "
    "JOIN profile_versions v ON v.id = u.profile_version_id "
    "JOIN rubric_versions w ON w.id = u.rubric_version_id "
    "JOIN model_policies m ON m.id = u.model_policy_id WHERE t.id = :target"
)


def _effort(value: str) -> Effort:
    effort = _EFFORTS.get(value)
    if effort is None:
        raise ValueError("a model policy names an unknown effort")
    return effort


async def load_inputs(conn: AsyncConnection, state: TargetState) -> tuple[ScoreInputs, AttemptRow]:
    """The attempt's inputs and ids (module docstring), on the caller's connection; `LookupError` when the
    target, its attempt or the profile version's profile is gone."""
    return await load_attempt_inputs(
        conn,
        target_id=state["target_id"],
        attempt_id=state["attempt_id"],
        prompt_schema_version_id=state["prompt_schema_version_id"],
    )


async def load_attempt_inputs(
    conn: AsyncConnection, *, target_id: int, attempt_id: int, prompt_schema_version_id: int
) -> tuple[ScoreInputs, AttemptRow]:
    """`load_inputs` of an attempt named by its ids (a batch item's, `scoring.batch_items`, which has no
    subgraph state)."""
    row = (await conn.execute(_INPUTS, {"target": target_id, "attempt": attempt_id})).first()
    if row is None:
        raise LookupError(f"score target {target_id} or its attempt {attempt_id} does not exist")
    if row.profile_json is None:
        raise LookupError(f"the profile version of score target {target_id} is purged")
    attempt = AttemptRow(
        job_id=int(row.job_id),
        job_revision_id=int(row.job_revision_id),
        llm_tuple_id=int(row.llm_tuple_id),
        profile_version_id=int(row.profile_version_id),
        rubric_version_id=int(row.rubric_version_id),
        model_policy_id=int(row.model_policy_id),
        execution_config_id=int(row.execution_config_id),
        evaluation_policy_id=int(row.evaluation_policy_id),
    )
    material: JSONValue = row.llm_input
    work_modes = material_strings(material, "work_mode_possible")
    work_mode_source = get_str(material, "work_mode_source")
    ad = revision_ad(
        material, completeness=row.description_completeness, description_text=row.description_text
    )
    inputs = ScoreInputs(
        version=await load_prompt_version(conn, prompt_schema_version_id),
        rubric=rubric_of(row.weights, row.anchors),
        profile=ConfirmedProfile.model_validate(row.profile_json),
        ad=ad,
        det=await read_det_facts(conn, attempt.job_id),
        structured_work_modes=tuple(work_modes) if work_mode_source == _STRUCTURED else (),
        completeness=row.description_completeness,
        policy=await evaluation_policy.load(conn, attempt.evaluation_policy_id),
        critic_effort=_effort(get_str(row.model_policy, "nodes.critic.effort")),
        critic_light_effort=_effort(get_str(row.model_policy, "critic_light_effort")),
        sample_key=sample_key(target_id=target_id, llm_tuple_id=attempt.llm_tuple_id),
        refs=score_refs(
            job_revision_id=attempt.job_revision_id,
            profile_version_id=attempt.profile_version_id,
            rubric_version_id=attempt.rubric_version_id,
        ),
    )
    return inputs, attempt


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

# The draft statements, which a batch item's store (`scoring.batch_items`) writes too: the insert of a
# generated draft (`validation = {"normalisation": …}`, the other NOT NULL columns placeholders), the read,
# and the validation's update.
INSERT_DRAFT: Final = q(
    "INSERT INTO score_drafts (attempt_id, round, llm_call_id, output_json, validation, "
    "deterministic_failures, provisional, triggers) "
    "VALUES (:attempt, :round, :call, :draft, :validation, '{}', '{}', '{}') "
    "RETURNING id",
    jsonb=["draft", "validation"],
)
READ_DRAFT: Final = q("SELECT id, round, output_json, validation FROM score_drafts WHERE id = :d")
SAVE_VALIDATION: Final = q(
    "UPDATE score_drafts SET output_json = :draft, "
    "validation = validation || jsonb_build_object('current', CAST(:current AS jsonb)) || "
    "CASE WHEN validation -> 'corrections' IS NULL "
    "THEN jsonb_build_object('corrections', CAST(:corrections AS jsonb)) ELSE CAST('{}' AS jsonb) END, "
    "deterministic_failures = CAST(:failures AS text[]), provisional = :provisional, "
    "triggers = CAST(:triggers AS text[]) WHERE id = :d",
    jsonb=["draft", "current", "corrections", "provisional"],
)
_INSERT_NOTE: Final = q(
    "INSERT INTO critic_notes (attempt_id, round, draft_id, llm_call_id, effort, triggers, verdict_returned, "
    "derived_outcome, review_json, validation) VALUES (:attempt, :round, :draft, :call, :effort, "
    "CAST(:triggers AS text[]), :verdict, :outcome, :review, :validation) RETURNING id",
    jsonb=["review", "validation"],
)
_NOTE: Final = q(
    "SELECT id, draft_id, derived_outcome, review_json, validation FROM critic_notes WHERE id = :n"
)


def draft_params(
    *, attempt_id: int, round: int, llm_call_id: int, parsed: ParsedGenerator
) -> dict[str, object]:
    """The binds of `INSERT_DRAFT` for a structurally valid output of the attempt's `round`."""
    return {
        "attempt": attempt_id,
        "round": round,
        "call": llm_call_id,
        "draft": parsed.draft.model_dump(mode="json"),
        "validation": {"normalisation": parsed.normalisation.to_json()},
    }


def validation_params(draft_id: int, record: ValidationRecord) -> dict[str, object]:
    """The binds of `SAVE_VALIDATION` for `record` of draft `draft_id`."""
    return {
        "d": draft_id,
        "draft": record.draft.model_dump(mode="json"),
        "current": record.current,
        "corrections": record.corrections,
        "failures": record.failures,
        "provisional": record.provisional,
        "triggers": record.triggers,
    }


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
