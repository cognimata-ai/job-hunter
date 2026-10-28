"""The four steps of one scoring (spec §9.4.1, §11.6-§11.9; plan Task 9): `generate_step`, `validate_step`,
`critic_step` and `revise_step`, over a `DraftStore` and an LLM `Caller`.

The `score_target` subgraph's nodes are thin wrappers over them, with the store of the `score_drafts` and
`critic_notes` tables and the gateway calls of a score attempt; Task 19's eval runner drives the same steps
with an in-memory store and its own calls, and writes one terminal row per case outside the target table
(L4611). The steps read every artifact from the store by id and write every artifact to it, so a resumed
attempt finds exactly what an earlier one stored; nothing of a draft or a review goes anywhere else.

**The inputs** (`ScoreInputs`): the prompt-schema version, the rubric, the confirmed profile with its
canonical sections, the rendered `<job_ad>` block, the job's `DetFacts`, its structured work modes and its
description completeness, the attempt's evaluation policy, the critic's normal and light efforts (the model
policy's), the T8 sample key and the ids every prompt records (`input_refs`). The active scope is not one of
them: `validate_step` takes it from its caller, which reads it under `active_config FOR SHARE` (L4400).
`rubric_of` (a `rubric_versions` row), `revision_ad` (a revision's material projection and description),
`score_refs` and `role_prompt` build them the one way the `score_target` subgraph and the batch preparation
(plan Task 16) share, so a batch item's params are the online generator's request for the same target.

**The steps.**
- `generate_step`: the generator call (`round` 0), the stage-1 parse with the version's parser family
  (`validator.parse_generator`) and, for a `ParsedGenerator`, the draft stored with its normalisation
  (`DraftStore.add_draft`). A structurally invalid output (the gateway's `InvalidOutput("structural")`, or
  a `StructuralFailure` of the parse) is never stored (§11.6): `Drafted` without a draft, which the routing
  repairs once. Every other error of the call propagates to the caller, which maps it.
- `validate_step`: stage 2 of the stored draft (`validate_draft`, with the stored normalisation and, after a
  first validation, the stored corrections as its history, so a re-validation of the corrected draft repairs
  nothing new); the knockout candidates under the given scope and the facts the critic must review
  (`required_facts`); the provisional aggregation under that scope with the fact confirmations and the
  injection assessment and the `not_a_job_ad_verified` of the last critic note; the triggers under
  the policy (`triggers.evaluate`). It stores the corrected draft with its validation, failures, provisional
  aggregation and triggers (`DraftStore.save_validation`, `ValidationRecord`). When that note reviewed this
  very draft (a resumed attempt), `Validated` carries the note's derived outcome and the failures without
  those of the facts it rejected (`validator.unresolved_failures`), so the routing goes where the live critic
  round went (Codex r2 B23).
- `critic_step`: the stored draft validated again (pure: the same result as the stored one), the round's
  effort (`critic_effort`), the critic call with `<draft>`, `<required_facts>` and `<validator_issues>`
  (`llm_calls.round` is the critic round, 1-3), `parse_critic`, `validate_review`, the fact confirmations of
  this round over the last note's (`apply_fact_confirmations`), and the note stored (`DraftStore.add_note`).
  `Reviewed.det_failures` are the draft's failure keys without those of the facts the review rejected (a
  rejected fact is dropped, L4555; `validator.unresolved_failures`, the rule of the derived outcome's
  condition 5 too). An invalid output is `Reviewed` without a note.
- `revise_step`: the reviser call (`round` the new draft's, 1-2) with `<previous_draft>` (the validated
  draft) and `<issues>` (L4544: the validator's failures and issues, the critic's grounded disputes with
  their suggested levels and quotes, the `fact_rejected`, `fact_uncertain` and unreviewed required facts, and
  the injection assessment), then as `generate_step` with the new round.

**The issues** are `render.Issue`s (codes, targets and grounded quotes, never free text): a target is an LLM
criterion key or a fact id of the draft; a validator pseudo key (`must_have:0`, `certification_item:2`, a C
code) is no target, so its issue has none (Controller carry T5-b).

**The stored artifacts** are read back by `StoredDraft.from_row` and `StoredNote.from_row` (the node store's
and the publish transaction's one reader of a `score_drafts` and a `critic_notes` row); `stored_validation`
is a validated draft's last validation as a publication reads it (Task 10): stage 2 again over the stored
draft, normalisation and history, with the row's stored failures (Controller carry T9-a); `stored_review`
the last valid critic review as a publication reads it, validated again against the draft it reviewed.

**The stored validation** (`ValidationRecord.current`): `issues`, `failures` and `groundings` (codes, keys,
paths and offsets) and `required` (the fact ids the critic reviews); a note's (`NoteRecord.validation`): its
completeness, the verdict mismatch, `not_a_job_ad_verified`, the required facts, each one's review and the
confirmations after the round, the unreviewed facts, the grounded disputes with their quotes, the discarded
reviews and the other issues. The provisional aggregation's scores are decimal strings.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import cached_property
from types import MappingProxyType
from typing import Any, Final, Protocol, get_args

from sqlalchemy.engine import Row

from jobhunter.llm.errors import InvalidOutput
from jobhunter.llm.gateway import GatewayResult, RenderedPrompt
from jobhunter.llm.pricing import Effort, effort_of
from jobhunter.llm.prompts.render import (
    Issue,
    JobAdBlock,
    PromptVersion,
    ScoreRole,
    draft_block,
    issues_block,
    job_ad_block,
    required_facts_block,
    score_prompt,
)
from jobhunter.llm.schemas.critic import CriticOut
from jobhunter.llm.schemas.generator import GeneratorDraft
from jobhunter.llm.schemas.profile import ConfirmedProfile
from jobhunter.run_settings import ScopeSettings
from jobhunter.scoring.aggregate import Aggregation, aggregate, industry_match
from jobhunter.scoring.det_criteria import employment_salary, location_work_mode
from jobhunter.scoring.evaluation_policy import EvaluationPolicy
from jobhunter.scoring.facts import FactRef
from jobhunter.scoring.filters import DetFacts
from jobhunter.scoring.knockouts import Confirmation, derive_knockouts, required_facts
from jobhunter.scoring.publication import Review
from jobhunter.scoring.routing import DerivedOutcome
from jobhunter.scoring.rubric import LLM_CRITERIA, RUBRIC_KEYS, Rubric
from jobhunter.scoring.triggers import Trigger, critic_effort, evaluate
from jobhunter.scoring.validator import (
    QUOTES_PER_REVIEW,
    CorrectionHistory,
    CriticValidation,
    Dispute,
    DraftValidation,
    Failure,
    Grounding,
    Normalisation,
    ParsedGenerator,
    StructuralFailure,
    ValidationContext,
    apply_fact_confirmations,
    ground_quote,
    parse_critic,
    parse_generator,
    profile_sections,
    unresolved_failures,
    validate_draft,
    validate_review,
)
from jobhunter.scoring.validator import Issue as ValidatorIssue
from jobhunter.types import JSONValue, get_list, get_object, get_str

_STRUCTURAL: Final = "structural"
_CONFIRMATIONS: Final[Mapping[str, Confirmation]] = {c: c for c in get_args(Confirmation)}
_OUTCOMES: Final[Mapping[str, DerivedOutcome]] = {o: o for o in get_args(DerivedOutcome)}
_CRITERIA_PREFIX: Final = "criteria."
_REGION_EVIDENCE: Final = "job_facts.remote_region.evidence"
_INDUSTRY_EVIDENCE: Final = "job_facts.industry.evidence"
FACT_REJECTED: Final = "fact_rejected"
FACT_UNCERTAIN: Final = "fact_uncertain"
FACT_UNREVIEWED: Final = "fact_unreviewed"
_REVIEW_MISSING: Final = "review_missing"
_INJECTION_NONE: Final = "none"
_STRUCTURED: Final = "structured"


# ---- the inputs, the stored artifacts and the store --------------------------------------------------------


@dataclass(frozen=True)
class ScoreInputs:
    """What the steps of one scoring read (module docstring). The content fields stay out of every `repr`."""

    version: PromptVersion = field(repr=False)
    rubric: Rubric = field(repr=False)
    profile: ConfirmedProfile = field(repr=False)
    ad: JobAdBlock = field(repr=False)
    det: DetFacts = field(repr=False)
    structured_work_modes: tuple[str, ...]
    completeness: str
    policy: EvaluationPolicy
    critic_effort: Effort
    critic_light_effort: Effort
    sample_key: str
    refs: Mapping[str, JSONValue]

    @cached_property
    def ctx(self) -> ValidationContext:
        """The stage-2 context of every validation of this scoring."""
        return ValidationContext(
            ad=self.ad,
            profile=self.profile,
            profile_sections=profile_sections(self.profile),
            det=self.det,
            structured_work_modes=self.structured_work_modes,
            completeness=self.completeness,
        )


@dataclass(frozen=True)
class StoredDraft:
    """A stored draft: the internal (corrected, once validated) draft, its stage-1 normalisation, its
    correction history (the normalisation and the first validation's corrections; `None` before that
    validation), the facts the critic reviews (empty before it) and the failures of its last validation
    (`validation.current.failures`, the exact keys and codes whose keys `deterministic_failures` holds;
    `None` before it)."""

    id: int
    round: int
    draft: GeneratorDraft = field(repr=False)
    normalisation: Normalisation
    history: CorrectionHistory | None
    required: tuple[str, ...]
    failures: tuple[Failure, ...] | None = field(default=None, repr=False)

    @classmethod
    def from_row(cls, *, id: int, round: int, output_json: JSONValue, validation: JSONValue) -> "StoredDraft":
        """The stored draft of a `score_drafts` row (its `id`, `round`, `output_json` and `validation`)."""
        doc = get_object(validation, "")
        normalisation = get_object(doc, "normalisation")
        corrections = doc.get("corrections")
        history = (
            None
            if corrections is None
            else CorrectionHistory.from_json({"normalisation": normalisation, "corrections": corrections})
        )
        current = doc.get("current")
        return cls(
            id=id,
            round=round,
            draft=GeneratorDraft.model_validate(output_json),
            normalisation=Normalisation.from_json(normalisation),
            history=history,
            required=tuple(_strings(current.get("required"))) if isinstance(current, dict) else (),
            failures=(
                tuple(Failure(get_str(f, "key"), get_str(f, "code")) for f in get_list(current, "failures"))
                if isinstance(current, dict)
                else None
            ),
        )


@dataclass(frozen=True)
class ValidationRecord:
    """What `validate_step` stores of a draft (module docstring): the corrected draft, the validation's
    `current` part and the history's corrections (kept as stored when the draft has them already), the
    failure keys, the provisional aggregation and the triggers."""

    draft: GeneratorDraft = field(repr=False)
    current: dict[str, JSONValue] = field(repr=False)
    corrections: list[JSONValue]
    failures: list[str] = field(repr=False)
    provisional: dict[str, JSONValue] = field(repr=False)
    triggers: list[str]


@dataclass(frozen=True)
class NoteRecord:
    """A critic note to store (`critic_notes`)."""

    round: int
    draft_id: int
    llm_call_id: int
    effort: Effort
    triggers: list[str]
    verdict_returned: str
    derived_outcome: DerivedOutcome
    review_json: dict[str, JSONValue] = field(repr=False)
    validation: dict[str, JSONValue] = field(repr=False)


@dataclass(frozen=True)
class StoredNote:
    """A stored critic note, as the later steps read it."""

    id: int
    draft_id: int
    derived_outcome: DerivedOutcome
    review: CriticOut = field(repr=False)
    validation: Mapping[str, JSONValue] = field(repr=False)

    @property
    def confirmations(self) -> dict[str, Confirmation]:
        """The fact confirmations after this note's round (the chain's, in order)."""
        stored = self.validation.get("fact_confirmations")
        if not isinstance(stored, dict):
            raise ValueError(f"critic note {self.id}: no stored fact confirmations")
        found: dict[str, Confirmation] = {}
        for name, value in stored.items():
            confirmation = _CONFIRMATIONS.get(value) if isinstance(value, str) else None
            if confirmation is None:
                raise ValueError(f"critic note {self.id}: a stored confirmation is no confirmation")
            found[name] = confirmation
        return found

    @property
    def not_a_job_ad_verified(self) -> bool:
        return self.validation.get("not_a_job_ad_verified") is True

    @property
    def fact_reviews(self) -> dict[str, JSONValue]:
        """Each required fact's review in this round (`critic_confirmed`, `critic_rejected`, `uncertain`)."""
        stored = self.validation.get("fact_reviews")
        if not isinstance(stored, dict):
            raise ValueError(f"critic note {self.id}: no stored fact reviews")
        return dict(stored)

    @classmethod
    def from_row(
        cls, *, id: int, draft_id: int, derived_outcome: str, review_json: JSONValue, validation: JSONValue
    ) -> "StoredNote":
        """The stored note of a `critic_notes` row; `ValueError` for an unknown derived outcome."""
        outcome = _OUTCOMES.get(derived_outcome)
        if outcome is None:
            raise ValueError(f"critic note {id}: an unknown derived outcome")
        return cls(
            id=id,
            draft_id=draft_id,
            derived_outcome=outcome,
            review=CriticOut.model_validate(review_json),
            validation=get_object(validation, ""),
        )


class DraftStore(Protocol):
    """Where the steps keep the drafts and notes of one scoring."""

    async def add_draft(self, *, round: int, llm_call_id: int, parsed: ParsedGenerator) -> int:
        """Store a structurally valid output as the draft of `round`; its id."""
        ...

    async def draft(self, draft_id: int) -> StoredDraft: ...

    async def save_validation(self, draft_id: int, record: ValidationRecord) -> None: ...

    async def add_note(self, note: NoteRecord) -> int:
        """Store a critic note; its id."""
        ...

    async def note(self, note_id: int) -> StoredNote: ...


class Caller(Protocol):
    """One logical call of a score step: `role`'s node with `prompt`, the call's `round`, and the critic's
    `effort` for the round (`None`: the node's own)."""

    async def __call__(
        self, role: ScoreRole, prompt: RenderedPrompt, *, round: int, effort: Effort | None = None
    ) -> GatewayResult: ...


# ---- the results -------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Drafted:
    """The result of `generate_step` or `revise_step`: the stored draft of `round`, or `draft_id` `None` for a
    structurally invalid output. `failure` is the parse's `StructuralFailure` (pydantic's error type and a
    path of schema keys) when the gateway passed the output and the parse refused it; `None` when the gateway
    refused it itself (`InvalidOutput("structural")`, its call has no output)."""

    round: int
    draft_id: int | None
    llm_call_id: int | None
    failure: StructuralFailure | None = None


@dataclass(frozen=True)
class Validated:
    """The result of `validate_step`: the triggers and the draft's failure keys (`failures`, as its row
    stores them), and, when the attempt's last critic note reviewed this very draft (a resumed attempt),
    that note's derived outcome (`review`, else `None`). `det_failures` are the failures the routing and
    the state go by: the draft's, without those of the facts that review rejected (`unresolved_failures`,
    as after a live critic round)."""

    triggers: list[Trigger]
    failures: list[str] = field(repr=False)
    det_failures: list[str] = field(repr=False)
    review: DerivedOutcome | None = None


@dataclass(frozen=True)
class Reviewed:
    """The result of `critic_step`: the stored note and what the routing reads of it, or `note_id` `None`
    for a structurally invalid output."""

    effort: Effort
    note_id: int | None
    llm_call_id: int | None = None
    derived_outcome: DerivedOutcome | None = None
    verdict_mismatch: bool = False
    not_a_job_ad_verified: bool = False
    det_failures: list[str] = field(default_factory=list, repr=False)


# ---- the shared inputs of a score request ------------------------------------------------------------


def rubric_of(weights: JSONValue, anchors: JSONValue) -> Rubric:
    """A `rubric_versions` row as the renderer and the aggregation read it: the eight weights in §11.2's order
    and the anchors (whose rendering orders their keys itself, `render.anchors_markdown`)."""
    stored = get_object(weights, "")
    if set(stored) != set(RUBRIC_KEYS):
        raise ValueError("a stored rubric has other weights than the eight criteria")
    ordered: dict[str, int] = {}
    for key in RUBRIC_KEYS:
        weight = stored[key]
        if isinstance(weight, bool) or not isinstance(weight, int):
            raise ValueError(f"the stored weight of {key} is no integer")
        ordered[key] = weight
    return Rubric(weights=MappingProxyType(ordered), anchors=MappingProxyType(get_object(anchors, "")))


def material_strings(material: JSONValue, path: str) -> list[str]:
    """The strings of the list at `path` of a revision's material projection (`job_revisions.llm_input`)."""
    return [get_str(item, "") for item in get_list(material, path)]


def revision_ad(material: JSONValue, *, completeness: str, description_text: str) -> JobAdBlock:
    """The `<job_ad>` block of a job revision: its material projection (`llm_input`: the title, company,
    locations, work modes and employment types with their sources) and its description."""
    work_mode_source = get_str(material, "work_mode_source")
    employment_source = get_str(material, "employment_source")
    company = get_object(material, "").get("company_display")
    return job_ad_block(
        title=get_str(material, "title"),
        company=company if isinstance(company, str) else None,
        locations=material_strings(material, "locations"),
        work_modes=[(mode, work_mode_source) for mode in material_strings(material, "work_mode_possible")],
        employment_types=[
            (kind, employment_source) for kind in material_strings(material, "employment_types")
        ],
        completeness=completeness,
        description_text=description_text,
    )


def material_ad(row: Row[Any]) -> JobAdBlock:
    """`revision_ad` of a row holding a revision's material as `job_revisions` names it (`llm_input`,
    `description_text`, `description_completeness`)."""
    return revision_ad(
        row.llm_input,
        completeness=str(row.description_completeness),
        description_text=str(row.description_text),
    )


def critic_efforts(model_policy: JSONValue) -> tuple[Effort, Effort]:
    """A model policy's critic efforts: its normal one (`nodes.critic.effort`) and its light one."""
    return (
        effort_of(get_str(model_policy, "nodes.critic.effort")),
        effort_of(get_str(model_policy, "critic_light_effort")),
    )


def material_inputs(
    row: Row[Any],
    *,
    version: PromptVersion,
    rubric: Rubric,
    policy: EvaluationPolicy,
    efforts: tuple[Effort, Effort],
    det: DetFacts,
    sample_key: str,
    refs: Mapping[str, JSONValue],
) -> ScoreInputs:
    """A scoring's inputs from a row holding a revision's material (`material_ad`) and a profile version's
    `profile_json` (a score attempt's, `attempt_inputs.load_attempt_inputs`, or an eval case's): the
    structured work modes are the material's when its source is structured; the critic efforts are
    `critic_efforts` of the model policy."""
    material: JSONValue = row.llm_input
    work_modes = material_strings(material, "work_mode_possible")
    structured = get_str(material, "work_mode_source") == _STRUCTURED
    critic, light = efforts
    return ScoreInputs(
        version=version,
        rubric=rubric,
        profile=ConfirmedProfile.model_validate(row.profile_json),
        ad=material_ad(row),
        det=det,
        structured_work_modes=tuple(work_modes) if structured else (),
        completeness=str(row.description_completeness),
        policy=policy,
        critic_effort=critic,
        critic_light_effort=light,
        sample_key=sample_key,
        refs=refs,
    )


def score_refs(
    *, job_revision_id: int, profile_version_id: int, rubric_version_id: int
) -> dict[str, JSONValue]:
    """The ids every score request of a target records besides its prompt's own (`llm_calls.input_refs`)."""
    return {
        "job_revision_id": job_revision_id,
        "profile_version_id": profile_version_id,
        "rubric_version_id": rubric_version_id,
    }


def role_prompt(
    role: ScoreRole,
    *,
    version: PromptVersion,
    rubric: Rubric,
    profile: ConfirmedProfile,
    ad: JobAdBlock,
    refs: Mapping[str, JSONValue],
    **blocks: str,
) -> RenderedPrompt:
    """The request of a score `role` (`render.score_prompt`) with the target's `refs` added to its
    `input_refs`: the one rendering of every step and of a batch item (module docstring)."""
    prompt = score_prompt(role, version=version, rubric=rubric, profile=profile, ad=ad, **blocks)
    return replace(prompt, input_refs={**prompt.input_refs, **refs})


# ---- the steps ---------------------------------------------------------------------------------------------


def _prompt(inputs: ScoreInputs, role: ScoreRole, **blocks: str) -> RenderedPrompt:
    return role_prompt(
        role,
        version=inputs.version,
        rubric=inputs.rubric,
        profile=inputs.profile,
        ad=inputs.ad,
        refs=inputs.refs,
        **blocks,
    )


async def _drafted(
    inputs: ScoreInputs, store: DraftStore, call: Caller, role: ScoreRole, prompt: RenderedPrompt, round: int
) -> Drafted:
    """A generator call of `round` and its draft stored, or `Drafted` without one (module docstring)."""
    try:
        result = await call(role, prompt, round=round)
    except InvalidOutput as error:
        if error.code != _STRUCTURAL:
            raise
        return Drafted(round, None, None)
    parsed = parse_generator(result.parsed.model_dump(mode="json"), family=inputs.version.parser_family)
    if isinstance(parsed, StructuralFailure):
        return Drafted(round, None, result.llm_call_id, parsed)
    draft_id = await store.add_draft(round=round, llm_call_id=result.llm_call_id, parsed=parsed)
    return Drafted(round, draft_id, result.llm_call_id)


async def generate_step(inputs: ScoreInputs, store: DraftStore, call: Caller) -> Drafted:
    """The generator's draft (module docstring)."""
    return await _drafted(inputs, store, call, "generate", _prompt(inputs, "generate"), 0)


def _validated(inputs: ScoreInputs, stored: StoredDraft) -> DraftValidation:
    return validate_draft(
        stored.draft, inputs.ctx, normalisation=stored.normalisation, history=stored.history
    )


def stored_validation(ctx: ValidationContext, stored: StoredDraft) -> DraftValidation:
    """The last validation of a validated stored draft, as a publication reads it (Task 10): stage 2 again
    over the stored (corrected) draft with its stored normalisation and history (pure: the validation it
    stored), with the stored failures, the draft row's exact keys and codes (Controller carry T9-a).
    `ValueError` for a draft never validated."""
    if stored.failures is None or stored.history is None:
        raise ValueError(f"score draft {stored.id} was never validated")
    validation = validate_draft(stored.draft, ctx, normalisation=stored.normalisation, history=stored.history)
    return replace(validation, failures=list(stored.failures))


def stored_review(ctx: ValidationContext, note: StoredNote, reviewed: StoredDraft) -> Review:
    """The last valid critic review as a publication reads it (Task 10; Task 19's eval runner too): the note's
    review validated again (`validate_review`) against the draft it reviewed, that draft's stored validation
    (`stored_validation`) and required facts, with the chain's confirmations the note stored."""
    validation = stored_validation(ctx, reviewed)
    required = [validation.fact_ids[name] for name in reviewed.required]
    checked = validate_review(note.review, validation=validation, required=required, ctx=ctx)
    return Review(validation=checked, injection=note.review.injection, confirmations=note.confirmations)


def failure_keys(validation: DraftValidation) -> list[str]:
    """The keys of the draft's deterministic failures, in order, distinct (`score_drafts`'
    `deterministic_failures`)."""
    return list(dict.fromkeys(failure.key for failure in validation.failures))


def _decimal(value: object) -> JSONValue:
    return None if value is None else str(value)


def _provisional(aggregation: Aggregation) -> dict[str, JSONValue]:
    return {
        "low": _decimal(aggregation.low),
        "mid": _decimal(aggregation.mid),
        "high": _decimal(aggregation.high),
        "coverage": _decimal(aggregation.coverage),
        "confidence": aggregation.confidence,
        "caps_applied": list(aggregation.caps_applied),
        "knockouts": list(aggregation.knockouts),
    }


def _issue_json(issue: ValidatorIssue) -> dict[str, JSONValue]:
    return {"code": issue.code, "path": issue.path, "detail": dict(issue.detail)}


def _grounding_json(grounding: Grounding) -> dict[str, JSONValue]:
    return {
        "source_kind": grounding.source_kind,
        "field_name": grounding.field_name,
        "span_start": grounding.span_start,
        "span_end": grounding.span_end,
    }


def _current(validation: DraftValidation, required: Sequence[FactRef]) -> dict[str, JSONValue]:
    return {
        "issues": [_issue_json(issue) for issue in validation.issues],
        "failures": [{"key": f.key, "code": f.code} for f in validation.failures],
        "groundings": {
            path: [_grounding_json(g) for g in found] for path, found in validation.groundings.items()
        },
        "required": [ref.fact_id for ref in required],
    }


async def validate_step(
    inputs: ScoreInputs,
    store: DraftStore,
    *,
    draft_id: int,
    scope: ScopeSettings,
    note: StoredNote | None,
    critic_rounds: int,
) -> Validated:
    """Stage 2 of draft `draft_id`, its provisional aggregation under `scope` and its triggers, stored
    (module docstring); `note` is the attempt's last critic note (its confirmations, its injection assessment
    and its `not_a_job_ad_verified`), `None` before any."""
    stored = await store.draft(draft_id)
    validation = _validated(inputs, stored)
    draft = validation.draft
    facts = draft.job_facts
    candidates = derive_knockouts(facts, inputs.det, inputs.profile, scope)
    required = required_facts(facts, candidates, not_a_job_ad=draft.not_a_job_ad)
    groundings = validation.groundings
    region = facts.remote_region if groundings.get(_REGION_EVIDENCE) else None
    industry = facts.industry.value if groundings.get(_INDUSTRY_EVIDENCE) else None
    provisional = aggregate(
        criteria={key: getattr(draft.criteria, key) for key in LLM_CRITERIA},
        det_location=location_work_mode(inputs.det, scope, region),
        det_employment=employment_salary(inputs.det, scope),
        weights=inputs.rubric.weights,
        knockouts=candidates,
        confirmations={} if note is None else note.confirmations,
        industry=industry_match(industry, inputs.det, scope),
        completeness=inputs.completeness,
        quality=None,
        contested=False,
        injection_suspected=validation.injection_suspected,
        critic_injection=None if note is None else note.review.injection,
        not_a_job_ad_verified=note is not None and note.not_a_job_ad_verified,
    )
    triggers = evaluate(
        validation=validation,
        provisional=provisional,
        policy=inputs.policy,
        completeness=inputs.completeness,
        critic_rounds=critic_rounds,
        required=required,
        sample_key=inputs.sample_key,
    )
    failures = failure_keys(validation)
    record = ValidationRecord(
        draft=draft,
        current=_current(validation, required),
        corrections=get_list(validation.history.to_json(), "corrections"),
        failures=failures,
        provisional=_provisional(provisional),
        triggers=list(triggers),
    )
    await store.save_validation(draft_id, record)
    if note is not None and note.draft_id == draft_id:  # the draft has its review already (a resume)
        return Validated(
            triggers, failures, unresolved_failures(failures, note.fact_reviews), note.derived_outcome
        )
    return Validated(triggers, failures, failures)


def _target(key: object, validation: DraftValidation) -> str | None:
    """`key` as an issue's target: an LLM criterion key or a fact id of the draft, else `None` (a pseudo
    key, Controller carry T5-b)."""
    if isinstance(key, str) and (key in LLM_CRITERIA or key in validation.fact_ids):
        return key
    return None


def _issue_target(issue: ValidatorIssue, validation: DraftValidation) -> str | None:
    """An issue's target: its detail's key, else the criterion its path names."""
    if (target := _target(issue.detail.get("key"), validation)) is not None:
        return target
    if issue.path.startswith(_CRITERIA_PREFIX):
        return _target(issue.path.removeprefix(_CRITERIA_PREFIX).split(".", 1)[0], validation)
    return None


def validator_issues(validation: DraftValidation) -> list[Issue]:
    """The validator's failures and issues of a draft as prompt issues, in order, distinct (module
    docstring)."""
    found = [Issue(f.code, _target(f.key, validation)) for f in validation.failures]
    found += [Issue(i.code, _issue_target(i, validation)) for i in validation.issues]
    return list(dict.fromkeys(found))


def _dispute_quotes(review: CriticOut, dispute: Dispute, inputs: ScoreInputs) -> list[str]:
    """The first `QUOTES_PER_REVIEW` quotes of the dispute's review that ground in the ad: the first review of
    its target with its assessment (a later one of that target is a duplicate, `validate_review`)."""
    ctx = inputs.ctx
    for item in review.reviews:
        if (item.target, item.assessment) == (dispute.target, dispute.assessment):
            grounded = [
                quote
                for quote in item.ad_evidence
                if ground_quote(quote, ctx.ad, ctx.profile_sections) is not None
            ]
            return grounded[:QUOTES_PER_REVIEW]
    return []


def _note_validation(
    review: CriticOut,
    validation: CriticValidation,
    required: Sequence[FactRef],
    confirmations: Mapping[str, Confirmation],
    inputs: ScoreInputs,
) -> dict[str, JSONValue]:
    unreviewed: list[JSONValue] = [
        issue.detail["target"]
        for issue in validation.issues
        if issue.code == _REVIEW_MISSING and isinstance(issue.detail.get("target"), str)
    ]
    disputes: list[JSONValue] = [
        {
            "target": dispute.target,
            "assessment": dispute.assessment,
            "suggested_level": dispute.suggested_level,
            "quotes": list(_dispute_quotes(review, dispute, inputs)),
            "groundings": [_grounding_json(g) for g in dispute.groundings],
        }
        for dispute in validation.grounded_disputes
    ]
    return {
        "complete": validation.complete,
        "verdict_mismatch": validation.verdict_mismatch,
        "not_a_job_ad_verified": validation.not_a_job_ad_verified,
        "required": [ref.fact_id for ref in required],
        "fact_reviews": dict(validation.fact_reviews),
        "fact_confirmations": dict(confirmations),
        "unreviewed": unreviewed,
        "disputes": disputes,
        "discarded": [_issue_json(issue) for issue in validation.discarded],
        "issues": [_issue_json(issue) for issue in validation.issues],
    }


async def critic_step(
    inputs: ScoreInputs,
    store: DraftStore,
    call: Caller,
    *,
    draft_id: int,
    note: StoredNote | None,
    critic_round: int,
    triggers: Sequence[Trigger],
) -> Reviewed:
    """The critic round `critic_round` of draft `draft_id` (module docstring); `note` is the attempt's last
    critic note, `None` before any, and `triggers` the draft's."""
    stored = await store.draft(draft_id)
    validation = _validated(inputs, stored)
    required = [validation.fact_ids[name] for name in stored.required]
    effort = critic_effort(
        triggers, inputs.policy, normal=inputs.critic_effort, light=inputs.critic_light_effort
    )
    prompt = _prompt(
        inputs,
        "critic",
        draft=draft_block(validation.draft, validation.fact_ids),
        required_facts=required_facts_block(required),
        validator_issues=issues_block(validator_issues(validation)),
    )
    try:
        result = await call("critic", prompt, round=critic_round, effort=effort)
    except InvalidOutput as error:
        if error.code != _STRUCTURAL:
            raise
        return Reviewed(effort, None)
    review = parse_critic(result.parsed.model_dump(mode="json"))
    if isinstance(review, StructuralFailure):
        return Reviewed(effort, None, result.llm_call_id)
    checked = validate_review(review, validation=validation, required=required, ctx=inputs.ctx)
    previous = {} if note is None else note.confirmations
    confirmations = apply_fact_confirmations(previous, checked, required)
    note_id = await store.add_note(
        NoteRecord(
            round=critic_round,
            draft_id=draft_id,
            llm_call_id=result.llm_call_id,
            effort=effort,
            triggers=list(triggers),
            verdict_returned=review.verdict,
            derived_outcome=checked.derived_outcome,
            review_json=review.model_dump(mode="json"),
            validation=_note_validation(review, checked, required, confirmations, inputs),
        )
    )
    return Reviewed(
        effort=effort,
        note_id=note_id,
        llm_call_id=result.llm_call_id,
        derived_outcome=checked.derived_outcome,
        verdict_mismatch=checked.verdict_mismatch,
        not_a_job_ad_verified=checked.not_a_job_ad_verified,
        det_failures=unresolved_failures(failure_keys(validation), checked.fact_reviews),
    )


def _strings(value: JSONValue) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def note_issues(note: StoredNote) -> list[Issue]:
    """The critic's part of the reviser's `<issues>` (module docstring): the grounded disputes, the rejected,
    uncertain and unreviewed required facts, and the injection assessment."""
    found: list[Issue] = []
    disputes = note.validation.get("disputes")
    for dispute in disputes if isinstance(disputes, list) else []:
        if not isinstance(dispute, dict):
            raise ValueError(f"critic note {note.id}: a stored dispute is no object")
        level = dispute.get("suggested_level")
        found.append(
            Issue(
                str(dispute["assessment"]),
                str(dispute["target"]),
                tuple(_strings(dispute.get("quotes"))),
                level if isinstance(level, int) and not isinstance(level, bool) else None,
            )
        )
    reviews = note.validation.get("fact_reviews")
    unreviewed = set(_strings(note.validation.get("unreviewed")))
    for name in _strings(note.validation.get("required")):
        status = reviews.get(name) if isinstance(reviews, dict) else None
        if status == "critic_rejected":
            found.append(Issue(FACT_REJECTED, name))
        elif status != "critic_confirmed":
            found.append(Issue(FACT_UNREVIEWED if name in unreviewed else FACT_UNCERTAIN, name))
    if note.review.injection != _INJECTION_NONE:
        found.append(Issue(f"injection_{note.review.injection}"))
    return found


async def revise_step(
    inputs: ScoreInputs, store: DraftStore, call: Caller, *, draft_id: int, note: StoredNote, round: int
) -> Drafted:
    """The revision of draft `draft_id` (of `round`) after the critic's `note` on it: the draft of `round + 1`
    (module docstring)."""
    stored = await store.draft(draft_id)
    validation = _validated(inputs, stored)
    issues = list(dict.fromkeys([*validator_issues(validation), *note_issues(note)]))
    prompt = _prompt(
        inputs,
        "revise",
        previous_draft=draft_block(validation.draft, validation.fact_ids),
        issues=issues_block(issues),
    )
    return await _drafted(inputs, store, call, "revise", prompt, round + 1)
