"""Aggregation (spec §11.8, L4512-4537): a result's score interval under a scope, its caps and penalties, its
midpoint and its confidence. Pure functions: no connection, no clock. Scores are `Decimal`s only.

- **Interval** (`interval`, L4515-4517). With `K` the criteria with a level and `Q` those without:
  `L₀ = Σ_{i∈K} w_i · level_i / 4`, `U₀ = L₀ + Σ_{i∈Q} w_i`, `coverage = Σ_{i∈K} w_i / 100`. The weights are
  integers, so every endpoint is a multiple of 0.25 and exact at two places.
- **Caps** (`caps`, L4520-4523), on both endpoints, in this order: each distinct soft code once,
  `x := max(0, x - SOFT_PENALTY)`; then each distinct hard code, `x := min(x, HARD_CAP)`; then
  `M = round((L + U) / 2, 2)`, half up (PostgreSQL's `round` of a non-negative numeric, so the `det_results`
  CHECK holds). Every step is monotone and applied to both endpoints, so `0 ≤ L ≤ M ≤ U ≤ 100`. The Codex #30
  example: `[20, 80]` with one binding hard knockout is `[20, 25]`, `M = 22.5`. `applied` records each code
  once, the soft ones first (`det_results.caps_applied`).
- **Industry preference** (`industry_match`, L4521; §13.1 `industries_include`/`industries_exclude`, stored
  case-folded): `unknown` without an LLM industry fact (or a blank one, or one whose `fold` has no fixed
  point); `excluded` when the fact's folded fixed point is excluded (the hard filter's comparison, M2
  Deviation 44), which takes precedence; `outside_include` when the include list is non-empty and lacks it;
  else `neutral` (an empty include list prefers nothing). A job with a **structured** industry is `neutral`:
  the structured value is the `excluded_industry` filter's, never penalised here (A3 G25, Deviation 25).
  `excluded` and `outside_include` are both the soft penalty code `industry` (with a badge).
- **Confidence** (`confidence`, L4534-4537 verbatim): `high` from a coverage of 0.80, `medium` from 0.60,
  else `low`; `low` for a `partial` description or a `garbage` one; one step down (`high` to `medium`,
  `medium` to `low`) for a contested result; `low` for an injection suspicion unless the critic's assessment
  is `none`. `critic_injection` is `None` when the critic did not run and `unavailable` when it ran and left
  no valid review (Task 1): both are `≠ "none"`, the conservative reading of L4537, so a suspicion without a
  usable assessment is `low` (Codex r2 B17).
- **`aggregate`** puts the six LLM criteria and the two deterministic ones (`det_criteria`) through the
  interval, applies the binding knockouts (`knockouts.binding`: an `unmet` comparison whose fact is
  structured or critic-confirmed; L4192) by their severity, and the industry penalty. Each knockout's record
  is its code, fact id, comparison, severity, binding status and `source`, the confirmation it was bound by
  (`structured` for a structured requirement, else the fact's confirmation, `unconfirmed` when it has none);
  a critic-rejected fact's knockout is dropped (§11.4). A verified not-a-job-ad (`not_a_job_ad_verified`,
  V6: a confirmed review with a grounded quote, never the draft's flag alone; Codex r1 #10) has every
  criterion `null`, no cap, no score and no confidence, as the `det_results` CHECKs require.
"""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Final, Literal, Protocol

from jobhunter.normalize.text import fixed_point, fold
from jobhunter.run_settings import ScopeSettings
from jobhunter.scoring.det_criteria import DetCriterion
from jobhunter.scoring.filters import DetFacts
from jobhunter.scoring.knockouts import Confirmation, KnockoutCandidate, binding
from jobhunter.scoring.rubric import LLM_CRITERIA
from jobhunter.types import JSONValue

HARD_CAP: Final = 25
SOFT_PENALTY: Final = 10
INDUSTRY_CODE: Final = "industry"  # the soft penalty code of an industry preference

IndustryMatch = Literal["neutral", "excluded", "outside_include", "unknown"]
Confidence = Literal["high", "medium", "low"]
CapKind = Literal["soft", "hard"]

_PENALISED: Final[frozenset[IndustryMatch]] = frozenset({"excluded", "outside_include"})
_LEVELS: Final = range(5)
_FULL_LEVEL: Final = Decimal(4)
_HUNDRED: Final = Decimal(100)
_CENT: Final = Decimal("0.01")
_HIGH_COVERAGE: Final = Decimal("0.80")
_MEDIUM_COVERAGE: Final = Decimal("0.60")
_DOWNGRADE: Final[Mapping[Confidence, Confidence]] = {"high": "medium", "medium": "low", "low": "low"}


class CriterionRating(Protocol):
    """A rated criterion as the interval reads it: its level, 0 to 4, or `None` (unknown). The draft's and
    the published criteria are ones."""

    @property
    def level(self) -> int | None: ...


@dataclass(frozen=True)
class Interval:
    low: Decimal
    high: Decimal
    coverage: Decimal


@dataclass(frozen=True)
class Cap:
    code: str
    kind: CapKind


@dataclass(frozen=True)
class Capped:
    low: Decimal
    mid: Decimal
    high: Decimal
    applied: list[Cap]


@dataclass(frozen=True)
class Aggregation:
    """A result's aggregation under one scope: the scores (`None` only for a verified not-a-job-ad), the
    coverage and confidence, and the JSON of the `det_results` columns `caps_applied`, `det_criteria` and
    `knockouts` (codes, ids, enums and the deterministic criteria's compared values, never ad text)."""

    low: Decimal | None
    mid: Decimal | None
    high: Decimal | None
    coverage: Decimal
    confidence: Confidence | None
    caps_applied: list[dict[str, JSONValue]]
    det_criteria: dict[str, JSONValue]
    knockouts: list[dict[str, JSONValue]]
    industry_match: IndustryMatch


def _level(key: str, level: object) -> Decimal | None:
    if level is None:
        return None
    if isinstance(level, bool) or not isinstance(level, int) or level not in _LEVELS:
        raise ValueError(f"the level of {key} must be 0 to 4 or None, got {level!r}")
    return Decimal(level)


def interval(levels: Mapping[str, int | None], weights: Mapping[str, int]) -> Interval:
    """`[L₀, U₀]` and the coverage of `levels` under `weights` (module docstring); the criteria of `levels`
    must be the weights' keys. `ValueError` otherwise, or for a level outside 0 to 4."""
    if set(levels) != set(weights):
        raise ValueError(f"the criteria {sorted(levels)} must be the weights' keys {sorted(weights)}")
    low = known = unknown = Decimal(0)
    for key, weight in weights.items():
        level = _level(key, levels[key])
        if level is None:
            unknown += weight
        else:
            low += weight * level / _FULL_LEVEL
            known += weight
    return Interval(low.quantize(_CENT), (low + unknown).quantize(_CENT), known / _HUNDRED)


def caps(interval: Interval, *, soft_codes: Collection[str], hard_codes: Collection[str]) -> Capped:
    """The caps and penalties applied to both endpoints of `interval`, then the midpoint (module
    docstring)."""
    low, high = interval.low, interval.high
    applied: list[Cap] = []
    for code in dict.fromkeys(soft_codes):
        low, high = max(Decimal(0), low - SOFT_PENALTY), max(Decimal(0), high - SOFT_PENALTY)
        applied.append(Cap(code, "soft"))
    for code in dict.fromkeys(hard_codes):
        low, high = min(low, Decimal(HARD_CAP)), min(high, Decimal(HARD_CAP))
        applied.append(Cap(code, "hard"))
    mid = ((low + high) / 2).quantize(_CENT, ROUND_HALF_UP)
    return Capped(low.quantize(_CENT), mid, high.quantize(_CENT), applied)


def industry_match(industry_fact: str | None, det: DetFacts, scope: ScopeSettings) -> IndustryMatch:
    """The industry preference of the job under `scope` (module docstring); `industry_fact` is the draft's
    grounded `industry` value, or `None`."""
    if det.industry_structured is not None:
        return "neutral"
    if industry_fact is None or not industry_fact.strip():
        return "unknown"
    folded = fixed_point(fold, industry_fact)
    if folded is None:
        return "unknown"
    if folded in scope.industries_exclude:
        return "excluded"
    if scope.industries_include and folded not in scope.industries_include:
        return "outside_include"
    return "neutral"


def confidence(
    *,
    coverage: Decimal,
    completeness: str,
    quality: str | None,
    contested: bool,
    injection_suspected: bool,
    critic_injection: str | None,
) -> Confidence:
    """L4534-4537 (module docstring). `quality` is the description's (§8.9), or `None` when the caller has
    only its completeness (a `garbage` description is always `partial`, §8.9)."""
    base: Confidence = (
        "high" if coverage >= _HIGH_COVERAGE else "medium" if coverage >= _MEDIUM_COVERAGE else "low"
    )
    if completeness == "partial" or quality == "garbage":
        base = "low"
    if contested:
        base = _DOWNGRADE[base]
    if injection_suspected and critic_injection != "none":
        base = "low"
    return base


def _criterion_json(criterion: DetCriterion, *, nulled: bool) -> dict[str, JSONValue]:
    level = None if nulled else criterion.level
    return {
        "level": level,
        "basis": "unknown" if level is None else criterion.basis,
        "inputs": dict(criterion.inputs),
    }


def _source(candidate: KnockoutCandidate, confirmations: Mapping[str, Confirmation]) -> Confirmation:
    if candidate.requirement_source == "structured":
        return "structured"
    return confirmations.get(candidate.fact_id, "unconfirmed")


def aggregate(
    *,
    criteria: Mapping[str, CriterionRating],
    det_location: DetCriterion,
    det_employment: DetCriterion,
    weights: Mapping[str, int],
    knockouts: Sequence[KnockoutCandidate],
    confirmations: Mapping[str, Confirmation],
    industry: IndustryMatch,
    completeness: str,
    quality: str | None,
    contested: bool,
    injection_suspected: bool,
    critic_injection: str | None,
    not_a_job_ad_verified: bool,
) -> Aggregation:
    """The aggregation of a result (module docstring): `criteria` holds the six LLM criteria (after any
    null-out), `knockouts` the candidates derived under the scope, `confirmations` the facts' confirmations.
    `ValueError` when an LLM criterion is missing or the weights name other criteria."""
    missing = [key for key in LLM_CRITERIA if key not in criteria]
    if missing:
        raise ValueError(f"the LLM criteria {missing} are missing")
    records: list[dict[str, JSONValue]] = []
    soft: list[str] = []
    hard: list[str] = []
    for candidate in knockouts:
        source = _source(candidate, confirmations)
        if source == "critic_rejected":
            continue  # the knockout is dropped (§11.4)
        binds = binding(candidate, source)
        records.append(
            {
                "code": candidate.code,
                "fact_id": candidate.fact_id,
                "comparison": candidate.comparison,
                "severity": candidate.severity,
                "binding": binds,
                "source": source,
            }
        )
        if binds:
            (hard if candidate.severity == "hard" else soft).append(candidate.code)
    if industry in _PENALISED:
        soft.append(INDUSTRY_CODE)
    nulled = not_a_job_ad_verified
    det_criteria: dict[str, JSONValue] = {
        "location_work_mode": _criterion_json(det_location, nulled=nulled),
        "employment_salary": _criterion_json(det_employment, nulled=nulled),
    }
    levels: dict[str, int | None] = {key: None if nulled else criteria[key].level for key in LLM_CRITERIA}
    levels["location_work_mode"] = None if nulled else det_location.level
    levels["employment_salary"] = None if nulled else det_employment.level
    span = interval(levels, weights)
    if not_a_job_ad_verified:
        return Aggregation(None, None, None, span.coverage, None, [], det_criteria, records, industry)
    capped = caps(span, soft_codes=soft, hard_codes=hard)
    rated = confidence(
        coverage=span.coverage,
        completeness=completeness,
        quality=quality,
        contested=contested,
        injection_suspected=injection_suspected,
        critic_injection=critic_injection,
    )
    applied: list[dict[str, JSONValue]] = [{"code": cap.code, "kind": cap.kind} for cap in capped.applied]
    return Aggregation(
        capped.low, capped.mid, capped.high, span.coverage, rated, applied, det_criteria, records, industry
    )
