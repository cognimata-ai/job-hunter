"""The eval runner (spec §11.13 L4610-4632, §17.6 L5618, §20.2 L6362; plan Tasks 19 and 20, R2, Rulings P17,
P26, P33, round-4 ruling (a), Deviation 29, Codex r1 #29-#31, #33, #34, r2 B18-B22, r3 B4-B7, r4 B4-B7, B10,
r5 B2, B8): the `eval_run` operation, which scores a candidate configuration on the golden cases outside the
target table, and calibrates it.

**A run** (`eval_runs`, `kind` `golden` or `canary`) is inserted with its operation in one transaction
(`start_run`: the CLI's `jobhunter eval run` and `canary_daily`'s `start_canary_run`), payload
`{"eval_run_id"}`, dedup key `eval_run:<id>`, `metrics = {"transport_requested": "live" | "fake"}`,
`repetitions` 2 for a golden run and 1 for a canary run. It is refused before its row, so before any paid
call (Codex r1 #34): `NoCandidate` when an id of its candidate names no row, `NoActiveCases`
(`eval_no_cases`) when its kind has no active case. Its candidate is the five ids of the row (`Candidate`):
the active configuration's (`active_candidate`) or a staged one (`stage_candidate`: a settings document
validated as the settings dry-run validates it, the gate's own refusal of generator effort `low` aside, since
the run measures that gate; its derived rows inserted by content hash under `active_config FOR UPDATE`, the
lock `derive.ensure_derived` relies on to serialise `evaluation_policies.version_no`, a new evaluation
policy's projected critic rate replayed; no settings version, no activation; a later save of the document
finds the same rows, so the gate's run is the candidate's). `start_canary_run` (the `canary_daily` task,
L4632) starts a canary run of the active configuration when a tuple is active and an active canary exists,
under `active_config FOR SHARE`, and nothing otherwise.

**The handler** (`eval_run`, `run_eval`) builds the score steps' dependencies from today's `OperationDeps`
fields (`cfg`, `gateway`, `clock`; Ruling P33, `worker/operations.py`; Task 27 moves them). **The owner is the
operation's claim** (Codex r1 #29): every paid call carries `CallContext(owner=EvalRunOwner(eval_run_id,
claim=op.claim), purpose_class="nonscoring", eval_run_id, golden_case_id, round, logical_call_no, deadline)`
with `input_refs["repetition"]` for the ledger's audit; every domain write (a case row, the run's end) runs
under that owner's lock (the operation row under its claim, then the run row `FOR SHARE` while `running`), so
a reclaimed execution can neither reserve nor write (`FencedOut`). The lock order of a case's write:
`active_config FOR SHARE` → the case's job `FOR SHARE` → the operation row → the run row → the case row
`FOR KEY SHARE` → its `eval_case_results` row (a call takes the month and the ledger rows between,
`spend_ledger.reserve`).

**One execution, no resume** (round-4 ruling (a)). The first transaction, under the owner's operation row and
the run row `FOR UPDATE`: a run already `completed` or `failed` is left as it is (a crash between a terminal
commit and the operation's end, Codex r5 B2) and the operation ends `succeeded`; a run an earlier execution
has touched (a call of the run, or a case row) is failed `metrics.failed = "interrupted"` with the `cost_usd`
its calls count (`operations.fail_eval_run`) and the operation ends `succeeded`, in the same transaction; a
run whose requested transport is not the worker's (`cfg.fake_llm`) is failed `"transport_mismatch"` before
any dispatch (Codex r2 B20). Otherwise this execution is the run's first, and it scores the run from its first
call to its completion: the steps of a case run over a `MemoryDraftStore` (Task 9's `DraftStore` in the
handler's memory: nothing is written between calls), and the case's only durable write is its terminal row.

**A case** (in case-id order: every active case of a golden run, every active canary of a canary run):
1. under `active_config FOR SHARE` and its job `FOR SHARE`, a fenced case (its job's fence, `is_fenced`, or
   an unreleased `profile` fence of its profile version, Ruling T19-b) is skipped and reported
   (`metrics.cases.fenced`), with no row (L6362); so is a case deleted meanwhile (`metrics.cases.deleted`:
   here, before its write, or a call naming it failing because it is gone), never requeued; the fault point
   `eval.before_case`;
2. its hard filters under its scope on its frozen facts (`golden.case_det_facts`): a filtered case is
   `filtered_out` (`output = {"filters": [...]}`, no call);
3. otherwise the production route of Task 9 (`steps` and `routing`) with the candidate's rubric and
   prompt-schema version, the case's profile, ad and scope, the candidate's evaluation policy with the case's
   T8 key `golden:<case>:<tuple_identity>` (L4611), and `route = 'forced_critic'` forcing one critic round
   where the routing would publish without one (L4612); after every logical call the fault point
   `eval.after_call`;
4. a route that publishes builds the publication object through Task 10's pure `publication.build` (no
   target, result, instance or ranking row); a gateway error is `refused` (a refusal) or `failed`, a
   publication bug `failed`, and the run goes on;
5. its row `eval_case_results(outcome, critic_ran, llm_call_ids, output, score_mid)` in one transaction after
   its last call (the fences checked again, the case row `FOR KEY SHARE` after the owner's), then the fault
   point `eval.after_case_commit`. A published case's `output` holds the publication object, its
   aggregation and badge (`not_a_job_ad:ad` kept unconfirmed, carry T10-e), its evidence places, its final
   validation's repairs and grounding failures (`validation: {"repairs", "grounding_failures"}`, each a path
   and a code, never a quote), and the route's events (each validation's triggers, each critic round's
   effort and derived outcome).

**Repetitions** (§11.13 L4613, Codex r2 B22): a golden run scores every case once, then attempts a second
repetition in case-id order over the cases whose first has a numeric `score_mid`, until `RETEST_CASES` have
two numeric results, `MAX_RETEST_TRIES` were attempted or the cases are exhausted; the execution's own list
of results is the progress. A canary run scores once.

**The end**, one transaction under the owner's lock: `running → completed` with `metrics = {"transport",
"transport_requested", "cases", "repetitions"}` (`cases = {"active", "fenced", "deleted", "outcomes"}`, the
outcomes counted by repetition: `{"1": {outcome: n}, "2": …}`) and, merged beside them (Ruling P17, Task 20),
the calibration of the run's case rows (`run_cases`, `calibration.metrics` against the candidate's projected
critic rate, its model policy's §21.2 central cost and the trailing 7-day similarity-lane cut-off;
`calibration.metrics_json`, with the gate's state and, when it failed, `failed_gates`), `passed` from
`calibration.gate` over the user's active cases (`validate.gate_cases`: the adversarial ones are pooled in
the metrics but never calibrate the gate, Ruling T20-b; a golden run only: `NULL` while uncalibrated and for
a canary run) and the `cost_usd` its calls count; then its alerts (`eval.gate_failed`, warn, for a production
golden run that failed, the candidate its subject; `scoring.canary_drift`, warn, for each canary of a canary
run more than 8 points from its first score under its key, the case its subject: `calibration.canary_drift`,
after the `completed`, so a first run is its own baseline); and the operation `succeeded` through
`op.finish_in` (the fault points `eval.before_terminal_commit`, its last statement, and
`eval.after_terminal_commit`).

Nothing here logs content: ids, counts, codes, outcomes and amounts only.
"""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final, Literal, get_args

from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from jobhunter.db.sql import q
from jobhunter.db.transitions import transition
from jobhunter.errors import FencedOut
from jobhunter.faults import afault_point
from jobhunter.llm.errors import LLMError, Refused
from jobhunter.llm.gateway import CallContext, GatewayResult, LLMGateway, RenderedPrompt, pinned_node_config
from jobhunter.llm.pricing import Effort
from jobhunter.llm.prompts.render import PromptVersion, ScoreRole, load_prompt_version, score_schema
from jobhunter.llm.prompts.render import PromptVersionUnscorable as PromptVersionUnscorableError
from jobhunter.llm.spend_ledger import EvalRunOwner
from jobhunter.observability.alerts import raise_alert
from jobhunter.observability.logging import get_logger
from jobhunter.run_settings import ScopeSettings, scope_from_payload
from jobhunter.scoring import calibration, evaluation_policy, filters, golden, publication, routing, steps
from jobhunter.scoring.publication import Publication, PublicationInvalid, PublishMode
from jobhunter.scoring.routing import Next
from jobhunter.scoring.rubric import Rubric
from jobhunter.scoring.steps import (
    NoteRecord,
    ScoreInputs,
    StoredDraft,
    StoredNote,
    ValidationRecord,
    critic_efforts,
    material_inputs,
    rubric_of,
    score_refs,
)
from jobhunter.scoring.triggers import TRIGGERS, Trigger, golden_sample_key
from jobhunter.scoring.validator import QUOTE_FAILURE_CODES, DraftValidation, ParsedGenerator
from jobhunter.settings.derive import active_profile, derived, ensure_derived
from jobhunter.settings.dryrun import unknown_references
from jobhunter.settings.llm_projection import central_cost_per_target
from jobhunter.settings.model import ModelPolicy
from jobhunter.settings.validate import FieldIssue, GateState, gate_cases, validate_fields
from jobhunter.types import JSONValue, get_int, get_object
from jobhunter.worker.operations import (
    EVAL_RUN_COST_SQL,
    EVAL_RUN_INTERRUPTED,
    OperationContext,
    create_operation,
    eval_run_failed,
    fail_eval_run,
    lock_operation_owner,
    register_operation_handler,
)
from jobhunter.worker.tasks import Deferrer

KIND: Final = "eval_run"
RunKind = Literal["golden", "canary"]
Route = Literal["production", "forced_critic"]
Transport = Literal["live", "fake"]
Outcome = calibration.Outcome
Skip = Literal["fenced", "deleted"]  # a case repetition with no row (module docstring, step 1)

RETEST_CASES: Final = 10  # §11.13 L4613: test-retest runs 10 cases twice
MAX_RETEST_TRIES: Final = 20  # the call bound of the second repetitions (Codex r2 B22)
GOLDEN_REPETITIONS: Final = 2
INTERRUPTED: Final = EVAL_RUN_INTERRUPTED
TRANSPORT_MISMATCH: Final = "transport_mismatch"
RUN_MISSING: Final = "eval_run_missing"  # the error code of an operation whose run does not exist
BEFORE_CASE: Final = "eval.before_case"
AFTER_CALL: Final = "eval.after_call"
AFTER_CASE_COMMIT: Final = "eval.after_case_commit"
BEFORE_TERMINAL_COMMIT: Final = "eval.before_terminal_commit"
AFTER_TERMINAL_COMMIT: Final = "eval.after_terminal_commit"

log = get_logger("jobhunter.scoring.eval_runner")


# ---- the candidate and the run's start ---------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """The configuration an eval run evaluates (§11.13 "Candidate configuration")."""

    rubric_version_id: int
    prompt_schema_version_id: int
    model_policy_id: int
    evaluation_policy_id: int
    execution_config_id: int


@dataclass(frozen=True)
class StartedRun:
    eval_run_id: int
    operation_id: int


class NoCandidate(LookupError):
    """No candidate to run (carry T19-e): an id of the candidate names no row, or the active configuration has
    no rubric or prompt-schema version. Its message names the kind and the id only. A `LookupError`, so a
    caller that catches it by name lets any other lookup bug keep its traceback."""


class NoActiveCases(Exception):
    """A run of a kind with no active case to score (`eval_no_cases`, Codex r1 #34), refused before its
    row."""

    code: Final = "eval_no_cases"

    def __init__(self, kind: str) -> None:
        self.kind = kind
        super().__init__(f"no active {kind} case")


class CandidateInvalid(Exception):
    """A candidate settings document the settings dry-run refuses: its field issues (paths and codes; the
    messages never repeat a value)."""

    def __init__(self, issues: Sequence[FieldIssue]) -> None:
        self.issues = tuple(issues)
        super().__init__(f"the candidate settings are invalid at {', '.join(i.field for i in self.issues)}")


_ACTIVE: Final = q(
    "SELECT rubric_version_id, prompt_schema_version_id, model_policy_id, evaluation_policy_id, "
    "execution_config_id, profile_version_id FROM active_config WHERE id = 1"
)
_LOCK_ACTIVE: Final = q(
    "SELECT rubric_version_id, prompt_schema_version_id, profile_version_id FROM active_config WHERE id = 1 "
    "FOR UPDATE"
)
# Whether each id of a candidate names a row, and whether the run's kind has an active case to score.
_RESOLVES: Final = q(
    "SELECT EXISTS (SELECT 1 FROM rubric_versions WHERE id = :rubric) AS rubric, "
    "EXISTS (SELECT 1 FROM prompt_schema_versions WHERE id = :prompt) AS prompt, "
    "EXISTS (SELECT 1 FROM model_policies WHERE id = :model_policy) AS model_policy, "
    "EXISTS (SELECT 1 FROM evaluation_policies WHERE id = :evaluation_policy) AS evaluation_policy, "
    "EXISTS (SELECT 1 FROM execution_configs WHERE id = :execution) AS execution, "
    "EXISTS (SELECT 1 FROM golden_cases WHERE active AND (is_canary OR NOT :canaries)) AS cases"
)
_CANDIDATE_KINDS: Final = (
    ("rubric", "rubric version"),
    ("prompt", "prompt-schema version"),
    ("model_policy", "model policy"),
    ("evaluation_policy", "evaluation policy"),
    ("execution", "execution config"),
)
# The candidate's validation in a staging: the gate is what the run measures (`stage_candidate`).
_STAGING_GATE: Final[GateState] = "uncalibrated"
_INSERT_RUN: Final = q(
    "INSERT INTO eval_runs (kind, route, rubric_version_id, prompt_schema_version_id, model_policy_id, "
    "evaluation_policy_id, execution_config_id, repetitions, status, metrics, started_at) VALUES (:kind, "
    ":route, :rubric, :prompt, :model_policy, :evaluation_policy, :execution, :repetitions, 'running', "
    ":metrics, :now) RETURNING id",
    jsonb=["metrics"],
)


async def active_candidate(conn: AsyncConnection) -> Candidate:
    """The active configuration as a candidate (`--candidate active`)."""
    row = (await conn.execute(_ACTIVE)).one()
    if row.rubric_version_id is None or row.prompt_schema_version_id is None:
        raise NoCandidate("active_config has no rubric or prompt-schema version (migrate seeds both)")
    return Candidate(
        int(row.rubric_version_id),
        int(row.prompt_schema_version_id),
        int(row.model_policy_id),
        int(row.evaluation_policy_id),
        int(row.execution_config_id),
    )


async def stage_candidate(conn: AsyncConnection, payload: JSONValue, *, now: datetime) -> Candidate:
    """The candidate of a settings document that may not exist as rows yet (`--candidate-payload`; Codex r2
    B21, r3 B7), in the caller's transaction: `active_config FOR UPDATE` first (it serialises the new
    evaluation policy's `version_no = max + 1` with a save and with another staging), the document validated
    as the settings dry-run validates it (`CandidateInvalid`, before any row), then its derived rows found or
    inserted by content hash (`derive.ensure_derived`, the scope resolved with the active profile as a save
    resolves it; a new evaluation policy's projected critic rate replayed at `now`, M3 Task 20). No settings
    version is written and no pointer moves: the rubric and prompt-schema version are the active ones. The
    gate's own refusal (generator effort `low` while the gate failed) does not apply: the run measures the
    candidate's gate, so it cannot wait for it."""
    pointers = (await conn.execute(_LOCK_ACTIVE)).one()
    settings, issues = validate_fields(payload, gate=_STAGING_GATE)
    if settings is not None:
        issues = await unknown_references(conn, settings)
    if settings is None or issues:
        raise CandidateInvalid(issues)
    if pointers.rubric_version_id is None or pointers.prompt_schema_version_id is None:
        raise NoCandidate("active_config has no rubric or prompt-schema version (migrate seeds both)")
    profile = await active_profile(conn, pointers.profile_version_id)
    ids = await ensure_derived(conn, derived(settings, profile=profile), now=now)
    log.info(
        "eval_candidate_staged",
        model_policy_id=ids.model_policy_id,
        evaluation_policy_id=ids.evaluation_policy_id,
        execution_config_id=ids.execution_config_id,
    )
    return Candidate(
        int(pointers.rubric_version_id),
        int(pointers.prompt_schema_version_id),
        ids.model_policy_id,
        ids.evaluation_policy_id,
        ids.execution_config_id,
    )


async def start_run(
    conn: AsyncConnection,
    *,
    kind: RunKind,
    route: Route,
    candidate: Candidate,
    transport_requested: Transport,
    now: datetime,
    defer: Deferrer | None,
) -> StartedRun:
    """Insert an eval run of `candidate` and its `eval_run` operation in the caller's transaction (module
    docstring); `defer` defers the operation's job (`None`: the `operations` reconciler re-defers it). Refused
    before the row, so before any paid call (Codex r1 #34): `NoCandidate` when an id of the candidate names no
    row, `NoActiveCases` when the kind has no active case (a canary run: no active canary)."""
    ids = {
        "rubric": candidate.rubric_version_id,
        "prompt": candidate.prompt_schema_version_id,
        "model_policy": candidate.model_policy_id,
        "evaluation_policy": candidate.evaluation_policy_id,
        "execution": candidate.execution_config_id,
    }
    resolves = (await conn.execute(_RESOLVES, {**ids, "canaries": kind == "canary"})).one()
    for key, name in _CANDIDATE_KINDS:
        if not getattr(resolves, key):
            raise NoCandidate(f"{name} {ids[key]} does not exist")
    if not resolves.cases:
        raise NoActiveCases(kind)
    params = {
        "kind": kind,
        "route": route,
        "rubric": candidate.rubric_version_id,
        "prompt": candidate.prompt_schema_version_id,
        "model_policy": candidate.model_policy_id,
        "evaluation_policy": candidate.evaluation_policy_id,
        "execution": candidate.execution_config_id,
        "repetitions": GOLDEN_REPETITIONS if kind == "golden" else 1,
        "metrics": {"transport_requested": transport_requested},
        "now": now,
    }
    eval_run_id = int((await conn.execute(_INSERT_RUN, params)).scalar_one())
    operation_id, _ = await create_operation(
        conn, kind=KIND, payload={"eval_run_id": eval_run_id}, dedup_key=f"{KIND}:{eval_run_id}", defer=defer
    )
    log.info(
        "eval_run_started",
        eval_run_id=eval_run_id,
        operation_id=operation_id,
        kind=kind,
        route=route,
        transport_requested=transport_requested,
    )
    return StartedRun(eval_run_id, operation_id)


# The canary start's first lock (§9.1, step 2): the active configuration it runs, read under `FOR SHARE`.
_ACTIVE_FOR_CANARY: Final = q(
    "SELECT llm_tuple_id, rubric_version_id, prompt_schema_version_id, model_policy_id, "
    "evaluation_policy_id, execution_config_id FROM active_config WHERE id = 1 FOR SHARE"
)
_ACTIVE_CANARY: Final = q("SELECT EXISTS (SELECT 1 FROM golden_cases WHERE active AND is_canary)")


async def start_canary_run(
    conn: AsyncConnection, *, transport: Transport, now: datetime, defer: Deferrer | None
) -> StartedRun | None:
    """The `canary_daily` task's work (§11.13 L4632, M3 Task 20), in the caller's transaction: with a tuple
    active and at least one active canary, a canary run of the active configuration on the production route
    and its operation (`start_run`, the worker's `transport`); `None` otherwise, writing nothing. The runner
    scores each canary under its own profile and scope, and its terminal transaction raises
    `scoring.canary_drift` (`calibration.canary_drift`)."""
    row = (await conn.execute(_ACTIVE_FOR_CANARY)).one()
    if row.llm_tuple_id is None or not (await conn.execute(_ACTIVE_CANARY)).scalar_one():
        log.info("eval_canary_skipped", tuple_active=row.llm_tuple_id is not None)
        return None
    if row.rubric_version_id is None or row.prompt_schema_version_id is None:
        raise NoCandidate("active_config has no rubric or prompt-schema version (migrate seeds both)")
    candidate = Candidate(
        int(row.rubric_version_id),
        int(row.prompt_schema_version_id),
        int(row.model_policy_id),
        int(row.evaluation_policy_id),
        int(row.execution_config_id),
    )
    return await start_run(
        conn,
        kind="canary",
        route="production",
        candidate=candidate,
        transport_requested=transport,
        now=now,
        defer=defer,
    )


# ---- the store of one scoring ------------------------------------------------------------------------------


class MemoryDraftStore:
    """Task 9's `DraftStore` in memory, for the one execution of a case's scoring (round-4 ruling (a)):
    the drafts, validations and notes live in the handler's memory and are written nowhere. Each artifact is
    kept as the JSON its table would hold and read back through `StoredDraft.from_row` and
    `StoredNote.from_row`, the readers of the subgraph's store, so the steps see exactly what they would see
    there: a draft's `validation` starts as `{"normalisation": …}` and a validation adds `current` and keeps
    the first `corrections` (`scoring.drafts`' statements)."""

    def __init__(self) -> None:
        self._drafts: dict[int, tuple[int, JSONValue, dict[str, JSONValue]]] = {}
        self._notes: dict[int, NoteRecord] = {}

    async def add_draft(self, *, round: int, llm_call_id: int, parsed: ParsedGenerator) -> int:
        draft_id = len(self._drafts) + 1
        validation: dict[str, JSONValue] = {"normalisation": parsed.normalisation.to_json()}
        self._drafts[draft_id] = (round, parsed.draft.model_dump(mode="json"), validation)
        return draft_id

    async def draft(self, draft_id: int) -> StoredDraft:
        found = self._drafts.get(draft_id)
        if found is None:
            raise LookupError(f"draft {draft_id} of this scoring does not exist")
        round_, output_json, validation = found
        return StoredDraft.from_row(id=draft_id, round=round_, output_json=output_json, validation=validation)

    async def save_validation(self, draft_id: int, record: ValidationRecord) -> None:
        round_, _, validation = self._drafts[draft_id]
        updated: dict[str, JSONValue] = {**validation, "current": record.current}
        if "corrections" not in validation:
            updated["corrections"] = list(record.corrections)
        self._drafts[draft_id] = (round_, record.draft.model_dump(mode="json"), updated)

    async def add_note(self, note: NoteRecord) -> int:
        note_id = len(self._notes) + 1
        self._notes[note_id] = note
        return note_id

    async def note(self, note_id: int) -> StoredNote:
        found = self._notes.get(note_id)
        if found is None:
            raise LookupError(f"critic note {note_id} of this scoring does not exist")
        return StoredNote.from_row(
            id=note_id,
            draft_id=found.draft_id,
            derived_outcome=found.derived_outcome,
            review_json=found.review_json,
            validation=found.validation,
        )


# ---- the run's rows ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Run:
    """The run's candidate and shape (its `eval_runs` row)."""

    id: int
    kind: RunKind
    route: Route
    repetitions: int
    candidate: Candidate
    transport_requested: str


@dataclass(frozen=True)
class EvalOutcome:
    """How an execution ended its run: `completed`, `failed` (with `reason`: `interrupted` or
    `transport_mismatch`), or `ended` (the run had ended before; nothing changed); the case rows it wrote and
    the run's `cost_usd`."""

    eval_run_id: int
    status: Literal["completed", "failed", "ended"]
    reason: str | None
    case_rows: int
    cost_usd: Decimal


_LOCK_RUN: Final = q(
    "SELECT kind, route, repetitions, status, metrics, rubric_version_id, prompt_schema_version_id, "
    "model_policy_id, evaluation_policy_id, execution_config_id FROM eval_runs WHERE id = :r FOR UPDATE"
)
_TOUCHED: Final = q(
    "SELECT EXISTS (SELECT 1 FROM llm_calls WHERE eval_run_id = :r) "
    "OR EXISTS (SELECT 1 FROM eval_case_results WHERE eval_run_id = :r)"
)
_COST: Final = q(f"SELECT {EVAL_RUN_COST_SQL} FROM eval_runs t WHERE t.id = :r")  # noqa: S608
_CASES: Final = q(
    "SELECT g.id, g.job_revision_id, r.job_id, g.profile_version_id FROM golden_cases g "
    "JOIN job_revisions r ON r.id = g.job_revision_id WHERE g.active AND (g.is_canary OR NOT :canaries) "
    "ORDER BY g.id"
)
_LOCK_CONFIG: Final = q("SELECT 1 FROM active_config WHERE id = 1 FOR SHARE")
# Whether a purge fence holds the case (L6362): its job's (`is_fenced`), or a `profile` fence of its profile
# version (Ruling T19-b, L6352); no row when the job is gone, and its case with it.
_FENCED: Final = q(
    "SELECT is_fenced(j.id) OR EXISTS (SELECT 1 FROM purge_fences f WHERE f.released_at IS NULL "
    "AND f.kind = 'profile' AND f.profile_version_id = :pv) FROM jobs j WHERE j.id = :j FOR SHARE OF j"
)
# The case row of a write, `FOR KEY SHARE`: a deletion (`golden.delete_case`) waits for the write's commit and
# then cascades to its row; none when the case was deleted meanwhile.
_HOLD_CASE: Final = q("SELECT 1 FROM golden_cases WHERE id = :g FOR KEY SHARE")
_CASE_EXISTS: Final = q("SELECT EXISTS (SELECT 1 FROM golden_cases WHERE id = :g)")
_CASE: Final = q(
    "SELECT g.det_inputs_snapshot, s.payload AS scope, r.llm_input, r.description_text, "
    "r.description_completeness, r.ad_language, v.profile_json FROM golden_cases g "
    "JOIN scope_versions s ON s.id = g.scope_version_id JOIN job_revisions r ON r.id = g.job_revision_id "
    "JOIN profile_versions v ON v.id = g.profile_version_id WHERE g.id = :g"
)
_CANDIDATE_ROWS: Final = q(
    "SELECT w.weights, w.anchors, m.payload AS model_policy, e.projected_critic_rate "
    "FROM rubric_versions w, model_policies m, evaluation_policies e "
    "WHERE w.id = :rubric AND m.id = :model_policy AND e.id = :evaluation_policy"
)
_CASE_CALLS: Final = q(
    "SELECT id FROM llm_calls WHERE eval_run_id = :r AND golden_case_id = :g "
    "AND CAST(input_refs ->> 'repetition' AS integer) = :n ORDER BY id"
)
_INSERT_CASE: Final = q(
    "INSERT INTO eval_case_results (eval_run_id, golden_case_id, repetition, outcome, critic_ran, "
    "llm_call_ids, output, score_mid) VALUES (:r, :g, :n, :outcome, :critic_ran, CAST(:calls AS bigint[]), "
    ":output, :score_mid)",
    jsonb=["output"],
)


def _run(eval_run_id: int, row: Row[tuple[object, ...]]) -> _Run:
    metrics = row.metrics if isinstance(row.metrics, dict) else {}
    requested = metrics.get("transport_requested")
    if row.kind not in ("golden", "canary"):
        raise ValueError(f"eval run {eval_run_id} is a {row.kind} run, which no eval_run operation scores")
    return _Run(
        id=eval_run_id,
        kind=row.kind,
        route=row.route,
        repetitions=int(row.repetitions),
        candidate=Candidate(
            int(row.rubric_version_id),
            int(row.prompt_schema_version_id),
            int(row.model_policy_id),
            int(row.evaluation_policy_id),
            int(row.execution_config_id),
        ),
        transport_requested=requested if isinstance(requested, str) else "",
    )


async def _cost(conn: AsyncConnection, eval_run_id: int) -> Decimal:
    return Decimal((await conn.execute(_COST, {"r": eval_run_id})).scalar_one())


async def _end_early(
    conn: AsyncConnection, op: OperationContext, eval_run_id: int, reason: str
) -> EvalOutcome:
    """The first transaction's failing end: the run `failed(reason)` and the operation `succeeded` together
    (the operation did its work: the run's state is final)."""
    await fail_eval_run(conn, eval_run_id, reason)
    cost = await _cost(conn, eval_run_id)
    await op.finish_in(conn, status="succeeded")
    log.warning("eval_run_ended_early", eval_run_id=eval_run_id, reason=reason, cost_usd=str(cost))
    return EvalOutcome(eval_run_id, "failed", reason, 0, cost)


async def _first(
    engine: AsyncEngine, op: OperationContext, eval_run_id: int, *, fake_llm: bool
) -> _Run | EvalOutcome:
    """The first transaction (module docstring): the run to score, or how it ended."""
    async with engine.begin() as conn:
        await lock_operation_owner(conn, op.claim)
        row = (await conn.execute(_LOCK_RUN, {"r": eval_run_id})).first()
        if row is None:  # nothing to score: the operation's work is gone
            await op.finish_in(conn, status="failed", error_code=RUN_MISSING)
            log.error("eval_run_missing", eval_run_id=eval_run_id)
            return EvalOutcome(eval_run_id, "ended", RUN_MISSING, 0, Decimal(0))
        if row.status != "running":
            await op.finish_in(conn, status="succeeded")
            log.info("eval_run_already_ended", eval_run_id=eval_run_id, status=row.status)
            return EvalOutcome(eval_run_id, "ended", None, 0, Decimal(0))
        if (await conn.execute(_TOUCHED, {"r": eval_run_id})).scalar_one():
            return await _end_early(conn, op, eval_run_id, INTERRUPTED)
        run = _run(eval_run_id, row)
        if run.transport_requested != ("fake" if fake_llm else "live"):
            return await _end_early(conn, op, eval_run_id, TRANSPORT_MISMATCH)
        return run


# ---- one case ----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Config:
    """The candidate's configuration as the steps read it, and what its calibration reports against: its
    evaluation policy's projected critic rate and its model policy's §21.2 central cost per target."""

    version: PromptVersion
    rubric: Rubric
    policy: evaluation_policy.EvaluationPolicy
    efforts: tuple[Effort, Effort]  # the critic's normal and light efforts (`steps.critic_efforts`)
    projected_critic_rate: Decimal
    central_cost_usd: Decimal


@dataclass(frozen=True)
class _Case:
    id: int
    job_revision_id: int
    job_id: int
    profile_version_id: int


@dataclass(frozen=True)
class _Result:
    """A case repetition's end as the execution keeps it (its progress): `outcome` `None` for a skipped case
    (fenced or deleted), which has no row."""

    case_id: int
    repetition: int
    outcome: Outcome | None
    score_mid: Decimal | None


@dataclass
class _Walk:
    """The route of one scoring (the subgraph's `TargetState` counters, in memory)."""

    round: int = 0
    critic_rounds: int = 0
    repair_used: bool = False
    draft_id: int | None = None
    note_id: int | None = None
    triggers: list[Trigger] = field(default_factory=list)
    critic_ran: bool = False
    logical_calls: int = 0
    events: list[JSONValue] = field(default_factory=list)


async def _config(engine: AsyncEngine, candidate: Candidate) -> _Config:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                _CANDIDATE_ROWS,
                {
                    "rubric": candidate.rubric_version_id,
                    "model_policy": candidate.model_policy_id,
                    "evaluation_policy": candidate.evaluation_policy_id,
                },
            )
        ).one()
        return _Config(
            version=await load_prompt_version(conn, candidate.prompt_schema_version_id),
            rubric=rubric_of(row.weights, row.anchors),
            policy=await evaluation_policy.load(conn, candidate.evaluation_policy_id),
            efforts=critic_efforts(row.model_policy),
            projected_critic_rate=Decimal(row.projected_critic_rate),
            central_cost_usd=central_cost_per_target(ModelPolicy.model_validate(row.model_policy)),
        )


def _decimal(value: Decimal | None) -> JSONValue:
    return None if value is None else str(value)


def _validation_output(validation: DraftValidation) -> dict[str, JSONValue]:
    """The final validation's repairs and grounding failures (the V3 quote issues), paths and codes only."""
    repairs: list[JSONValue] = [{"path": r.path, "code": r.code} for r in validation.repairs]
    grounding: list[JSONValue] = [
        {"path": i.path, "code": i.code} for i in validation.issues if i.code in QUOTE_FAILURE_CODES
    ]
    return {"repairs": repairs, "grounding_failures": grounding}


def _publication_output(made: Publication, validation: DraftValidation, walk: _Walk) -> dict[str, JSONValue]:
    """A published case's `output`: the publication object, its aggregation, badge and evidence places, its
    final validation's repairs and grounding failures, and the route's events."""
    aggregation = made.aggregation
    return {
        "publication": made.result.model_dump(mode="json"),
        "aggregation": {
            "low": _decimal(aggregation.low),
            "mid": _decimal(aggregation.mid),
            "high": _decimal(aggregation.high),
            "coverage": _decimal(aggregation.coverage),
            "confidence": aggregation.confidence,
            "caps_applied": list(aggregation.caps_applied),
            "knockouts": list(aggregation.knockouts),
            "det_criteria": dict(aggregation.det_criteria),
            "industry_match": aggregation.industry_match,
        },
        "critic_injection": made.critic_injection,
        "badge": made.badge_possibly_not_a_job_ad,
        "truncated_fields": list(made.truncated_fields),
        "evidence": [
            {
                "subject": e.subject,
                "source_kind": e.source_kind,
                "field_name": e.field_name,
                "span_start": e.span_start,
                "span_end": e.span_end,
            }
            for e in made.evidence
        ],
        "validation": _validation_output(validation),
        "rounds": list(walk.events),
    }


class _Execution:
    """The run's one execution (module docstring): its candidate, its owner and its results so far."""

    def __init__(
        self,
        engine: AsyncEngine,
        gateway: LLMGateway,
        run: _Run,
        config: _Config,
        op: OperationContext,
    ) -> None:
        self._engine = engine
        self._gateway = gateway
        self._run = run
        self._config = config
        self.owner = EvalRunOwner(run.id, claim=op.claim)
        self.results: list[_Result] = []
        self.skipped: dict[Skip, list[int]] = {"fenced": [], "deleted": []}

    # -- the calls

    def _caller(self, case: _Case, walk: _Walk) -> steps.Caller:
        candidate = self._run.candidate

        async def call(
            role: ScoreRole, prompt: RenderedPrompt, *, round: int, effort: Effort | None = None
        ) -> GatewayResult:
            async with self._engine.connect() as conn:
                node = await pinned_node_config(
                    conn,
                    role,
                    model_policy_id=candidate.model_policy_id,
                    execution_config_id=candidate.execution_config_id,
                    schema=score_schema(role, self._config.version),
                    effort=effort,
                )
            walk.logical_calls += 1
            ctx = CallContext(
                owner=self.owner,
                purpose_class="nonscoring",
                execution_config_id=candidate.execution_config_id,
                deadline=self._gateway.monotonic() + node.deadline_s,
                eval_run_id=self._run.id,
                logical_call_no=walk.logical_calls,
                round=round,
                golden_case_id=case.id,
            )
            try:
                result = await self._gateway.call(node, prompt, ctx)
            finally:
                await afault_point(AFTER_CALL)
            log.info(
                "eval_called",
                eval_run_id=self._run.id,
                golden_case_id=case.id,
                purpose=role,
                llm_call_id=result.llm_call_id,
                logical_call_no=ctx.logical_call_no,
                round=round,
                effort=node.effort,
                cost_usd=str(result.cost_usd),
            )
            return result

        return call

    # -- the route (Task 9's steps and routing; module docstring, step 3)

    async def _route(
        self, inputs: ScoreInputs, store: MemoryDraftStore, scope: ScopeSettings, case: _Case, walk: _Walk
    ) -> Next:
        call = self._caller(case, walk)
        nxt = Next("generate")
        while nxt.route not in ("publish", "fail"):
            if nxt.repair:
                walk.repair_used = True
            if nxt.route == "generate":
                drafted = await steps.generate_step(inputs, store, call)
                nxt = self._drafted(drafted, walk, "generate")
            elif nxt.route == "revise":
                if walk.draft_id is None or walk.note_id is None:
                    raise ValueError("a revision needs a reviewed draft")
                drafted = await steps.revise_step(
                    inputs,
                    store,
                    call,
                    draft_id=walk.draft_id,
                    note=await store.note(walk.note_id),
                    round=walk.round,
                )
                nxt = self._drafted(drafted, walk, "revise")
            elif nxt.route == "validate":
                nxt = await self._validate(inputs, store, scope, walk)
            elif nxt.route == "critic":
                nxt = await self._critic(inputs, store, call, walk)
            else:
                raise ValueError(f"an eval scoring never routes to {nxt.route!r}")
        return nxt

    def _drafted(self, drafted: steps.Drafted, walk: _Walk, step: routing.Generation) -> Next:
        if drafted.draft_id is not None:
            walk.draft_id, walk.round = drafted.draft_id, drafted.round
        return routing.after_generation(
            step, stored=drafted.draft_id is not None, repair_used=walk.repair_used
        )

    async def _validate(
        self, inputs: ScoreInputs, store: MemoryDraftStore, scope: ScopeSettings, walk: _Walk
    ) -> Next:
        if walk.draft_id is None:
            raise ValueError("a validation needs a draft")
        validated = await steps.validate_step(
            inputs,
            store,
            draft_id=walk.draft_id,
            scope=scope,
            note=None if walk.note_id is None else await store.note(walk.note_id),
            critic_rounds=walk.critic_rounds,
        )
        walk.triggers = [code for code in TRIGGERS if code in validated.triggers]
        walk.events.append(
            {
                "step": "validate",
                "round": walk.round,
                "triggers": list(walk.triggers),
                "det_failures": len(validated.det_failures),
            }
        )
        nxt = routing.after_validate(
            triggers=validated.triggers,
            det_failures=validated.det_failures,
            critic_rounds=walk.critic_rounds,
            round=walk.round,
            review=validated.review,
        )
        if self._run.route == "forced_critic" and walk.critic_rounds == 0 and nxt.route == "publish":
            return Next("critic")  # L4612: one critic round for every case
        return nxt

    async def _critic(
        self, inputs: ScoreInputs, store: MemoryDraftStore, call: steps.Caller, walk: _Walk
    ) -> Next:
        if walk.draft_id is None:
            raise ValueError("a critic round needs a draft")
        critic_round = walk.critic_rounds + 1
        walk.critic_ran = True
        reviewed = await steps.critic_step(
            inputs,
            store,
            call,
            draft_id=walk.draft_id,
            note=None if walk.note_id is None else await store.note(walk.note_id),
            critic_round=critic_round,
            triggers=walk.triggers,
        )
        walk.events.append(
            {
                "step": "critic",
                "critic_round": critic_round,
                "effort": reviewed.effort,
                "derived_outcome": reviewed.derived_outcome,
            }
        )
        if reviewed.note_id is not None:
            walk.critic_rounds, walk.note_id = critic_round, reviewed.note_id
        return routing.after_critic(
            review=reviewed.derived_outcome,
            det_failures=reviewed.det_failures,
            round=walk.round,
            repair_used=walk.repair_used,
        )

    async def _publication(
        self,
        inputs: ScoreInputs,
        store: MemoryDraftStore,
        scope: ScopeSettings,
        mode: PublishMode,
        walk: _Walk,
    ) -> tuple[Publication, DraftValidation]:
        """Task 10's pure publication of the final draft (module docstring, step 4), and that draft's final
        validation."""
        if walk.draft_id is None:
            raise ValueError("a publication needs a draft")
        ctx = inputs.ctx
        stored = await store.draft(walk.draft_id)
        review = None
        if walk.note_id is not None:
            note = await store.note(walk.note_id)
            review = steps.stored_review(ctx, note, await store.draft(note.draft_id))
        validation = steps.stored_validation(ctx, stored)
        made = publication.build(
            draft=stored.draft,
            validation=validation,
            last_review=review,
            mode=mode,
            scope=scope,
            ctx=ctx,
            weights=inputs.rubric.weights,
            revisions_used=stored.round,
        )
        return made, validation

    # -- a case

    async def _skip(self, conn: AsyncConnection, case: _Case) -> Skip | None:
        """Under `active_config FOR SHARE`, the case's job `FOR SHARE`: whether a purge fence holds the case
        (its job's or its profile version's) or its job is gone with it."""
        fenced = (
            await conn.execute(_FENCED, {"j": case.job_id, "pv": case.profile_version_id})
        ).scalar_one_or_none()
        if fenced is None:
            return "deleted"
        return "fenced" if fenced else None

    async def _load(self, case: _Case) -> Row[tuple[object, ...]] | Skip:
        """The case's inputs under `active_config FOR SHARE` and its job `FOR SHARE`, or why it is skipped: a
        purge fence holds it (L6362, Ruling T19-b) or it was deleted."""
        async with self._engine.begin() as conn:
            await conn.execute(_LOCK_CONFIG)
            skip = await self._skip(conn, case)
            if skip is not None:
                return skip
            row = (await conn.execute(_CASE, {"g": case.id})).one_or_none()
            return "deleted" if row is None else row

    def _inputs(
        self, case: _Case, row: Row[tuple[object, ...]], repetition: int, det: filters.DetFacts
    ) -> ScoreInputs:
        config, candidate = self._config, self._run.candidate
        identity = golden.tuple_identity(
            profile_version_id=case.profile_version_id,
            rubric_version_id=candidate.rubric_version_id,
            prompt_schema_version_id=candidate.prompt_schema_version_id,
            model_policy_id=candidate.model_policy_id,
        )
        refs: dict[str, JSONValue] = {
            **score_refs(
                job_revision_id=case.job_revision_id,
                profile_version_id=case.profile_version_id,
                rubric_version_id=candidate.rubric_version_id,
            ),
            "golden_case_id": case.id,
            "repetition": repetition,
        }
        return material_inputs(
            row,
            version=config.version,
            rubric=config.rubric,
            policy=config.policy,
            efforts=config.efforts,
            det=det,
            sample_key=golden_sample_key(golden_case_id=case.id, tuple_identity=identity),
            refs=refs,
        )

    async def case(self, case: _Case, repetition: int) -> _Result:
        """One repetition of one case (module docstring, steps 1-5). A case deleted meanwhile is skipped
        whatever its scoring then raised (a call's reservation naming it, say), and never requeued: an error
        is re-raised while the case exists, and a lost claim (`FencedOut`) always is."""
        try:
            return await self._score(case, repetition)
        except FencedOut:
            raise
        except Exception as error:
            async with self._engine.connect() as conn:
                exists = bool((await conn.execute(_CASE_EXISTS, {"g": case.id})).scalar_one())
            if exists:
                raise
            log.warning(
                "eval_case_deleted_meanwhile",
                eval_run_id=self._run.id,
                golden_case_id=case.id,
                repetition=repetition,
                error_class=type(error).__name__,
            )
            return self._skipped(case, repetition, "deleted")

    async def _score(self, case: _Case, repetition: int) -> _Result:
        row = await self._load(case)
        if isinstance(row, str):
            return self._skipped(case, repetition, row)
        await afault_point(BEFORE_CASE)
        scope = scope_from_payload(get_object(row.scope, ""))
        det = golden.case_det_facts(
            row.det_inputs_snapshot, material=row.llm_input, ad_language=str(row.ad_language)
        )
        filtered = filters.evaluate(det, str(row.description_text), scope)
        if not filtered.passed:
            output: dict[str, JSONValue] = {"filters": list(filtered.reasons)}
            return await self._write(case, repetition, "filtered_out", _Walk(), output, None)
        if row.profile_json is None:
            return await self._write(case, repetition, "failed", _Walk(), {"error": "profile_purged"}, None)
        inputs = self._inputs(case, row, repetition, det)
        store, walk = MemoryDraftStore(), _Walk()
        try:
            nxt = await self._route(inputs, store, scope, case, walk)
        except Refused:
            refused: dict[str, JSONValue] = {"error": "Refused", "rounds": list(walk.events)}
            return await self._write(case, repetition, "refused", walk, refused, None)
        except (LLMError, PromptVersionUnscorableError) as error:
            failed: dict[str, JSONValue] = {"error": type(error).__name__, "rounds": list(walk.events)}
            return await self._write(case, repetition, "failed", walk, failed, None)
        if nxt.route == "fail" or nxt.publish_mode is None:
            failed = {"error": nxt.error_code, "rounds": list(walk.events)}
            return await self._write(case, repetition, "failed", walk, failed, None)
        mode = nxt.publish_mode
        invalid: PublicationInvalid | None = None
        try:
            made, validation = await self._publication(inputs, store, scope, mode, walk)
        except PublicationInvalid as error:
            invalid = error
        if invalid is not None:
            failed = {
                "error": "publication_invalid",
                "error_types": list(invalid.error_types),
                "rounds": list(walk.events),
            }
            return await self._write(case, repetition, "failed", walk, failed, None)
        output = _publication_output(made, validation, walk)
        return await self._write(case, repetition, mode, walk, output, made.aggregation.mid)

    def _skipped(self, case: _Case, repetition: int, skip: Skip) -> _Result:
        skipped = self.skipped[skip]
        if case.id not in skipped:
            skipped.append(case.id)
        log.info(
            "eval_case_skipped",
            eval_run_id=self._run.id,
            golden_case_id=case.id,
            repetition=repetition,
            skip=skip,
        )
        return _Result(case.id, repetition, None, None)

    async def _write(
        self,
        case: _Case,
        repetition: int,
        outcome: Outcome,
        walk: _Walk,
        output: dict[str, JSONValue],
        score_mid: Decimal | None,
    ) -> _Result:
        """The case's terminal row (module docstring, step 5): one transaction in the lock order, under the
        owner; a case fenced or deleted meanwhile writes nothing."""
        async with self._engine.begin() as conn:
            await conn.execute(_LOCK_CONFIG)
            skip = await self._skip(conn, case)
            if skip is not None:
                return self._skipped(case, repetition, skip)
            await self.owner.lock(conn)
            if (await conn.execute(_HOLD_CASE, {"g": case.id})).first() is None:
                return self._skipped(case, repetition, "deleted")
            ids = [int(i) for i in (await conn.execute(_CASE_CALLS, self._keys(case, repetition))).scalars()]
            await conn.execute(
                _INSERT_CASE,
                {
                    **self._keys(case, repetition),
                    "outcome": outcome,
                    "critic_ran": walk.critic_ran,
                    "calls": ids,
                    "output": output,
                    "score_mid": score_mid,
                },
            )
        await afault_point(AFTER_CASE_COMMIT)
        log.info(
            "eval_case_scored",
            eval_run_id=self._run.id,
            golden_case_id=case.id,
            repetition=repetition,
            outcome=outcome,
            critic_ran=walk.critic_ran,
            llm_calls=len(ids),
            score_mid=_decimal(score_mid),
        )
        result = _Result(case.id, repetition, outcome, score_mid)
        self.results.append(result)
        return result

    def _keys(self, case: _Case, repetition: int) -> dict[str, object]:
        return {"r": self._run.id, "g": case.id, "n": repetition}


# ---- the calibration (Task 20, Ruling P17) -----------------------------------------------------------------


@dataclass(frozen=True)
class RunCase:
    """A case repetition of a run as the calibration and the CLI's case table read it: its result, its case
    and the case's source."""

    result: calibration.CaseResult
    case: calibration.CaseRow
    source: str


# Each case row of a run with its golden case, the similarity of the target its label was taken from, and the
# counted cost of its calls (as `EVAL_RUN_COST_SQL` counts a run's).
_RUN_CASES: Final = q(
    "SELECT e.golden_case_id, e.repetition, e.outcome, e.critic_ran, e.score_mid, e.output, g.label, "
    "g.expected_knockouts, g.profile_version_id, g.source, t.similarity, ("
    "SELECT coalesce(sum(CASE l.state WHEN 'billed' THEN l.settled_usd WHEN 'not_billed' THEN 0 "
    "ELSE l.reserved_usd END), 0) FROM llm_calls c JOIN llm_spend_ledger l ON l.id = c.spend_ledger_id "
    "WHERE c.id = ANY(e.llm_call_ids)) AS cost_usd "
    "FROM eval_case_results e JOIN golden_cases g ON g.id = e.golden_case_id "
    "LEFT JOIN llm_results res ON res.id = g.labelled_from_result_id "
    "LEFT JOIN score_targets t ON t.id = res.target_id "
    "WHERE e.eval_run_id = :r ORDER BY e.golden_case_id, e.repetition"
)
# The trailing similarity-lane cut-off (L4628): the lowest similarity the lane admitted in the window.
_SIMILARITY_CUTOFF: Final = q(
    "SELECT min(t.similarity) FROM score_attempts a JOIN score_targets t ON t.id = a.target_id "
    "WHERE a.lane = 'similarity' AND a.started_at > :since AND a.started_at <= :now "
    "AND t.similarity IS NOT NULL"
)
CUTOFF_WINDOW: Final = timedelta(days=7)
GATE_FAILED_ALERT: Final = "eval.gate_failed"
CANARY_DRIFT_ALERT: Final = "scoring.canary_drift"


async def run_cases(conn: AsyncConnection, eval_run_id: int) -> list[RunCase]:
    """The run's case repetitions by case and repetition (plain reads)."""
    found: list[RunCase] = []
    for row in await conn.execute(_RUN_CASES, {"r": eval_run_id}):
        if row.outcome not in get_args(calibration.Outcome) or row.label not in calibration.ORDINAL:
            raise ValueError(f"eval run {eval_run_id} holds a case row the calibration cannot read")
        result = calibration.case_result(
            golden_case_id=int(row.golden_case_id),
            repetition=int(row.repetition),
            outcome=row.outcome,
            critic_ran=bool(row.critic_ran),
            score_mid=None if row.score_mid is None else Decimal(row.score_mid),
            output=row.output,
            cost_usd=Decimal(row.cost_usd),
        )
        case = calibration.CaseRow(
            label=row.label,
            expected_knockouts=tuple(row.expected_knockouts),
            profile_version_id=int(row.profile_version_id),
            similarity=None if row.similarity is None else Decimal(str(row.similarity)),
        )
        found.append(RunCase(result, case, str(row.source)))
    return found


@dataclass(frozen=True)
class _Calibrated:
    state: GateState
    metrics: dict[str, JSONValue]
    passed: bool | None


async def _calibrate(conn: AsyncConnection, run: _Run, config: _Config, *, now: datetime) -> _Calibrated:
    """The run's calibration and gate in its terminal transaction (module docstring, **The end**): the gate
    counts the user's active cases (`validate.gate_cases`, Ruling T20-b); the metrics pool every case."""
    rows = await run_cases(conn, run.id)
    cutoff = (
        await conn.execute(_SIMILARITY_CUTOFF, {"since": now - CUTOFF_WINDOW, "now": now})
    ).scalar_one_or_none()
    m = calibration.metrics(
        [r.result for r in rows],
        cases={r.result.golden_case_id: r.case for r in rows},
        projected_critic_rate=config.projected_critic_rate,
        central_cost_usd=config.central_cost_usd,
        similarity_cutoff=None if cutoff is None else Decimal(str(cutoff)),
    )
    state = calibration.gate(m, active_cases=await gate_cases(conn))
    passed = None if run.kind != "golden" or state == "uncalibrated" else state == "passed"
    return _Calibrated(state, calibration.metrics_json(m, state=state), passed)


async def _alert(conn: AsyncConnection, run: _Run, calibrated: _Calibrated) -> None:
    """The run's alerts, in its terminal transaction: `eval.gate_failed` for a gated golden run that failed
    (L5618; a forced-critic run is a diagnostic, L4612), `scoring.canary_drift` for each drifted canary of a
    canary run (L4632). Ids and numbers only."""
    if run.kind == "golden" and run.route == "production" and calibrated.passed is False:
        c = run.candidate
        failed = calibrated.metrics.get("failed_gates")
        names = [str(name) for name in failed] if isinstance(failed, list) else []
        await raise_alert(
            conn,
            code=GATE_FAILED_ALERT,
            severity="warn",
            subject=f"{c.rubric_version_id}:{c.prompt_schema_version_id}:{c.model_policy_id}:"
            f"{c.evaluation_policy_id}",
            message=f"eval run {run.id} failed the golden-set gate: {', '.join(names)}",
            context={"eval_run_id": run.id, "failed_gates": list(names)},
        )
    if run.kind == "canary":
        for drift in await calibration.canary_drift(conn, eval_run_id=run.id):
            await raise_alert(
                conn,
                code=CANARY_DRIFT_ALERT,
                severity="warn",
                subject=str(drift.golden_case_id),
                message=f"canary {drift.golden_case_id} scored {drift.score_mid} against its baseline "
                f"{drift.baseline} in eval run {run.id}",
                context={
                    "eval_run_id": run.id,
                    "golden_case_id": drift.golden_case_id,
                    "score_mid": str(drift.score_mid),
                    "baseline": str(drift.baseline),
                    "delta": str(drift.delta),
                },
            )
            log.warning(
                "eval_canary_drift",
                eval_run_id=run.id,
                golden_case_id=drift.golden_case_id,
                delta=str(drift.delta),
            )


# ---- the run -----------------------------------------------------------------------------------------------


def retest_cases(first: Sequence[_Result]) -> list[int]:
    """The cases a second repetition may be attempted for, in case-id order (module docstring): those whose
    first result is numeric, at most `MAX_RETEST_TRIES` of them (the call bound); the caller stops early once
    `RETEST_CASES` have two numeric results."""
    numeric = sorted(r.case_id for r in first if r.score_mid is not None)
    return numeric[:MAX_RETEST_TRIES]


async def _cases(engine: AsyncEngine, kind: RunKind) -> list[_Case]:
    async with engine.connect() as conn:
        rows = await conn.execute(_CASES, {"canaries": kind == "canary"})
        return [
            _Case(int(r.id), int(r.job_revision_id), int(r.job_id), int(r.profile_version_id)) for r in rows
        ]


async def run_eval(
    engine: AsyncEngine, gateway: LLMGateway, *, eval_run_id: int, op: OperationContext, now: datetime
) -> EvalOutcome:
    """The `eval_run` operation's body (module docstring): the run's one execution, from its first
    transaction to its terminal commit, which ends the operation too. `op.deps.cfg.fake_llm` is the worker's
    transport (the flag the gateway's transport was built from); `now` is the execution's start, logged.
    `FencedOut` (a lost claim) propagates having written nothing more. A run that does not exist ends the
    operation `failed('eval_run_missing')`."""
    if op.deps is None:
        raise RuntimeError("an eval run needs the worker's operation dependencies")
    first = await _first(engine, op, eval_run_id, fake_llm=op.deps.cfg.fake_llm)
    if isinstance(first, EvalOutcome):
        return first
    run = first
    config = await _config(engine, run.candidate)
    execution = _Execution(engine, gateway, run, config, op)
    cases = await _cases(engine, run.kind)
    log.info(
        "eval_run_executing",
        eval_run_id=run.id,
        kind=run.kind,
        route=run.route,
        cases=len(cases),
        started_at=now.isoformat(),
    )
    for case in cases:
        await execution.case(case, 1)
    attempted: list[int] = []
    sample: list[int] = []
    if run.repetitions >= GOLDEN_REPETITIONS:
        by_id = {case.id: case for case in cases}
        for case_id in retest_cases(execution.results):
            if len(sample) >= RETEST_CASES:
                break
            attempted.append(case_id)
            second = await execution.case(by_id[case_id], 2)
            if second.score_mid is not None:
                sample.append(case_id)
    outcomes: dict[str, JSONValue] = {}  # by repetition (`"1"`, `"2"`): each outcome's count
    for repetition in sorted({result.repetition for result in execution.results}):
        counts = Counter(r.outcome for r in execution.results if r.repetition == repetition and r.outcome)
        outcomes[str(repetition)] = {outcome: count for outcome, count in sorted(counts.items())}
    metrics: dict[str, JSONValue] = {
        "transport": "fake" if op.deps.cfg.fake_llm else "live",
        "transport_requested": run.transport_requested,
        "cases": {
            "active": len(cases),
            "fenced": list(execution.skipped["fenced"]),
            "deleted": list(execution.skipped["deleted"]),
            "outcomes": outcomes,
        },
        "repetitions": {"attempted": list(attempted), "sample": list(sample)},
    }
    async with engine.begin() as conn:
        await execution.owner.lock(conn)
        cost = await _cost(conn, run.id)
        calibrated = await _calibrate(conn, run, config, now=now)
        await transition(
            conn,
            "eval_runs",
            run.id,
            ["running"],
            "completed",
            set_={
                "metrics": {**metrics, **calibrated.metrics},
                "passed": calibrated.passed,
                "cost_usd": cost,
            },
        )
        await _alert(conn, run, calibrated)  # after the run's `completed`: a first canary is its own baseline
        await op.finish_in(conn, status="succeeded")
        await afault_point(BEFORE_TERMINAL_COMMIT)
    await afault_point(AFTER_TERMINAL_COMMIT)
    log.info(
        "eval_run_completed",
        eval_run_id=run.id,
        case_rows=len(execution.results),
        fenced=len(execution.skipped["fenced"]),
        deleted=len(execution.skipped["deleted"]),
        sample=len(sample),
        gate=calibrated.state,
        passed=calibrated.passed,
        cost_usd=str(cost),
    )
    return EvalOutcome(run.id, "completed", None, len(execution.results), cost)


@register_operation_handler(KIND, on_failed=eval_run_failed)
async def eval_run(ctx: OperationContext) -> None:
    """The `eval_run` operation (module docstring): payload `{"eval_run_id"}`; its dependencies are today's
    `OperationDeps` fields (Ruling P33)."""
    deps = ctx.deps
    if deps is None:
        raise RuntimeError("eval_run needs the worker's operation dependencies")
    eval_run_id = get_int(dict(ctx.claim.payload), "eval_run_id")
    await run_eval(ctx.engine, deps.gateway, eval_run_id=eval_run_id, op=ctx, now=deps.clock())
