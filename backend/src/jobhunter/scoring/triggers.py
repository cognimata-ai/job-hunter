"""The critic triggers (spec §11.7.1, L4402-4412) and the critic's effort (§11.7.4, L4477). Pure functions of
a draft's validation, its provisional aggregation under the active scope, and the attempt's evaluation
policy.

| Code | Fires when | Mandatory |
|---|---|---|
| T0 | a previous critic round in this attempt (`critic_rounds > 0`) | yes |
| T1 | a V1 limit issue, a V2/V3 failure or a V4 consistency failure (`DraftValidation.t1`); no repair | yes |
| T2 | `|M - τ| ≤ t2_band`, or `L < τ ≤ U` with `coverage < t2_straddle_max_coverage` | no |
| T3 | `M ≥ t3_min_mid` | no |
| T4 | a `partial` description and `M ≥ T4_MIN_MID` (the literal 60) | yes |
| T5 | a binding-capable fact among the required facts (any but `not_a_job_ad:ad`) | yes |
| T6 | the injection suspicion (V5) | yes |
| T7 | the draft says it is no job ad | yes |
| T8 | `sampled(sample_key, t8_sample_pct)` | no |

`τ` is `relevance_threshold`. T2, T3 and T4 need a score: a provisional aggregation without one (a verified
not-a-job-ad) fires none of them. `evaluate` returns the codes in this order.

**Sampling.** `sampled(key, pct)` is `int(sha256(key)[:8 hex digits], 16) % 100 < pct`: reproducible, and
independent of anything but the key. Online the key is `sample_key(target_id, llm_tuple_id)`,
`f"{target_id}:{llm_tuple_id}"`; in an eval run `golden_sample_key(golden_case_id, tuple_identity)`,
`f"golden:{golden_case_id}:{tuple_identity}"` (Task 20 defines `tuple_identity`).

**Effort.** `critic_effort` is the light effort iff a round's triggers are non-empty and a subset of the
policy's `critic_light_triggers` (⊆ `{T2, T3, T8}`, the settings model's set, so it never names a mandatory
trigger), else the normal one. T0 is never light, so every re-review after a revision runs at the normal
effort.
"""

import hashlib
from collections.abc import Sequence
from decimal import Decimal
from typing import Final, Literal, get_args

from jobhunter.llm.pricing import Effort
from jobhunter.scoring.aggregate import Aggregation
from jobhunter.scoring.evaluation_policy import T4_MIN_MID, EvaluationPolicy
from jobhunter.scoring.facts import NOT_A_JOB_AD, FactRef
from jobhunter.scoring.validator import DraftValidation

Trigger = Literal["T0", "T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8"]
TRIGGERS: Final[tuple[Trigger, ...]] = get_args(Trigger)
MANDATORY: Final[frozenset[Trigger]] = frozenset({"T0", "T1", "T4", "T5", "T6", "T7"})

_SAMPLE_DIGITS: Final = 8


def sampled(key: str, pct: int) -> bool:
    """Whether `key` falls in the `pct` percent sample (module docstring)."""
    bucket = int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:_SAMPLE_DIGITS], 16) % 100
    return bucket < pct


def sample_key(*, target_id: int, llm_tuple_id: int) -> str:
    """T8's key of an online target."""
    return f"{target_id}:{llm_tuple_id}"


def golden_sample_key(*, golden_case_id: int, tuple_identity: str) -> str:
    """T8's key of a golden case in an eval run."""
    return f"golden:{golden_case_id}:{tuple_identity}"


def _score_triggers(provisional: Aggregation, policy: EvaluationPolicy, completeness: str) -> set[Trigger]:
    low, mid, high = provisional.low, provisional.mid, provisional.high
    if low is None or mid is None or high is None:
        return set()
    found: set[Trigger] = set()
    tau = Decimal(policy.relevance_threshold)
    straddles = low < tau <= high and provisional.coverage < policy.t2_straddle_max_coverage
    if abs(mid - tau) <= policy.t2_band or straddles:
        found.add("T2")
    if mid >= policy.t3_min_mid:
        found.add("T3")
    if completeness == "partial" and mid >= T4_MIN_MID:
        found.add("T4")
    return found


def evaluate(
    *,
    validation: DraftValidation,
    provisional: Aggregation,
    policy: EvaluationPolicy,
    completeness: str,
    critic_rounds: int,
    required: Sequence[FactRef],
    sample_key: str,
) -> list[Trigger]:
    """The triggers of a draft (the table of the module docstring), in code order. `validation` is the
    draft's stage 2, `provisional` its aggregation under the active scope, `completeness` the description's,
    `critic_rounds` the attempt's critic rounds so far, and `required` the facts the critic must review
    (`knockouts.required_facts`)."""
    found = _score_triggers(provisional, policy, completeness)
    if critic_rounds > 0:
        found.add("T0")
    if validation.t1:
        found.add("T1")
    if any(ref.fact_id != NOT_A_JOB_AD.fact_id for ref in required):
        found.add("T5")
    if validation.injection_suspected:
        found.add("T6")
    if validation.draft.not_a_job_ad:
        found.add("T7")
    if sampled(sample_key, policy.t8_sample_pct):
        found.add("T8")
    return [code for code in TRIGGERS if code in found]


def critic_effort(
    triggers: Sequence[Trigger], policy: EvaluationPolicy, *, normal: Effort, light: Effort
) -> Effort:
    """`light` iff `triggers` is non-empty and a subset of the policy's light triggers, else `normal`."""
    return light if triggers and set(triggers) <= policy.critic_light_triggers else normal
