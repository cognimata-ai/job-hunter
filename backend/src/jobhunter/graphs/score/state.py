"""The state of the `score_target` subgraph (spec §9.4 L3504-3516; plan Task 9): ids, counters and enums only.

`TargetState` is §9.4's state verbatim (`target_id`, `claim_id`, `attempt_no`, `attempt_id`, `round`,
`critic_rounds`, `repair_used`, `current_draft_id`, `last_critic_note_id`, `triggers`, `det_failures`,
`outcome`), plus:
- `start_stage`: the stage the attempt's generator call starts at (`enlarged` after a previous attempt
  stopped at `max_tokens`, M2 Carry-forward J2; `load_context`);
- `error_code`: the code `fail` maps (`llm_step.outcome_for`), an enum, never text;
- `resumed`: the attempt continues from the drafts of earlier attempts (`load_context`);
- `publish_mode`: how `publish` publishes, `accepted` or `contested` (the routing's);
- `prompt_schema_version_id`: the tuple's prompt-schema version (`load_context`); every node fetches its
  `PromptVersion` from the renderer's cache by it;
- `not_a_job_ad_verified`: the last critic round confirmed the draft's `not_a_job_ad` with a grounded quote
  (V6; restored by `load_context`, set by `critic`);
- `funded_bulk_operation_id`: the bulk operation that funds this attempt's calls, computed once by
  `load_context` (`spend_ledger.funded_bulk_operation_id`), `None` for every attempt that is not a funded
  follow-up;
- `last_critic_outcome` and `last_critic_draft_id`: the derived outcome of the newest critic note and the
  draft it reviewed (restored by `load_context`, set by `critic`), so `validate` routes a resumed draft that
  already has its review by that review (Codex r2 B23) instead of calling the critic again.

**No content.** The checkpoints of the cycle's thread hold this state, so it holds no ad text, draft, note or
profile value (§9.1, §16): the drafts and notes are read from their tables by id. `det_failures` holds the
keys of the current draft's unresolved deterministic failures (criterion keys, fact ids and the id-less
facts' pseudo keys), except that a fact id whose subject is a name folded from the ad (`certification:<name>`,
`mandatory_language:other:<name>`) is held by its code alone (`certification`, `mandatory_language:other`):
the exact keys are the draft's `deterministic_failures` column. The routing reads only whether the list is
empty.

`ClaimRef` is the cycle's claim of one target (§9.3 L3459-3462). `TargetOutcome` is what Task 11's wrapper
node projects the subgraph's final state into, `{"results": {work_id: outcome}}` with `work_id =
f"t:{target_id}:a{attempt_no}"` (L3453): the attempt's outcome, the target's state, its lane and the attempt's
`cost_usd` as a decimal string (A2 G2, Deviation 16).
"""

from typing import Final, TypedDict

from jobhunter.llm.gateway import Stage
from jobhunter.scoring.routing import DerivedOutcome, PublishMode

_NAMED_FACT_PREFIXES: Final = ("certification:", "mandatory_language:other:")


class ClaimRef(TypedDict):
    target_id: int
    claim_id: str
    attempt_no: int


class TargetState(TypedDict):
    target_id: int
    claim_id: str
    attempt_no: int
    attempt_id: int
    round: int  # 0 = generate, 1-2 = revisions
    critic_rounds: int  # 0-3
    repair_used: bool  # the one structural-repair call of the attempt (§11.6)
    current_draft_id: int | None
    last_critic_note_id: int | None
    triggers: list[str]  # T0-T8 codes of the current draft (§11.7.1)
    det_failures: list[str]  # unresolved deterministic failures of the current draft (module docstring)
    outcome: str | None
    start_stage: Stage
    error_code: str | None
    resumed: bool
    publish_mode: PublishMode | None
    prompt_schema_version_id: int
    not_a_job_ad_verified: bool
    funded_bulk_operation_id: int | None
    last_critic_outcome: DerivedOutcome | None
    last_critic_draft_id: int | None


class TargetUpdate(TypedDict, total=False):
    """A node's partial update of `TargetState`."""

    target_id: int
    claim_id: str
    attempt_no: int
    attempt_id: int
    round: int
    critic_rounds: int
    repair_used: bool
    current_draft_id: int | None
    last_critic_note_id: int | None
    triggers: list[str]
    det_failures: list[str]
    outcome: str | None
    start_stage: Stage
    error_code: str | None
    resumed: bool
    publish_mode: PublishMode | None
    prompt_schema_version_id: int
    not_a_job_ad_verified: bool
    funded_bulk_operation_id: int | None
    last_critic_outcome: DerivedOutcome | None
    last_critic_draft_id: int | None


class TargetOutcome(TypedDict):
    target_id: int
    attempt_no: int
    outcome: str  # the attempt's outcome
    state: str  # the target's state
    lane: str | None
    cost_usd: str


def work_id(target_id: int, attempt_no: int) -> str:
    """The key of an attempt's outcome in `CycleState.results` (L3453)."""
    return f"t:{target_id}:a{attempt_no}"


def initial_state(ref: ClaimRef, attempt_id: int) -> TargetState:
    """The state the subgraph starts an attempt from: the claim and its attempt row; `load_context` fills the
    rest (`prompt_schema_version_id` is 0 until it does)."""
    return {
        "target_id": ref["target_id"],
        "claim_id": ref["claim_id"],
        "attempt_no": ref["attempt_no"],
        "attempt_id": attempt_id,
        "round": 0,
        "critic_rounds": 0,
        "repair_used": False,
        "current_draft_id": None,
        "last_critic_note_id": None,
        "triggers": [],
        "det_failures": [],
        "outcome": None,
        "start_stage": "first",
        "error_code": None,
        "resumed": False,
        "publish_mode": None,
        "prompt_schema_version_id": 0,
        "not_a_job_ad_verified": False,
        "funded_bulk_operation_id": None,
        "last_critic_outcome": None,
        "last_critic_draft_id": None,
    }


def state_failure_keys(keys: list[str]) -> list[str]:
    """`keys` as the state holds them (module docstring): each name-bearing fact id by its code, in order,
    distinct."""
    held = []
    for key in keys:
        named = next((p for p in _NAMED_FACT_PREFIXES if key.startswith(p)), None)
        held.append(key if named is None else named.removesuffix(":"))
    return list(dict.fromkeys(held))
