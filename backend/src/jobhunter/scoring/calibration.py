"""The calibration metrics and the eval gate (spec §11.13 L4610-4632; plan Task 20, Codex r1 #33, r2 B22,
r4 B10, A3 G20, G21, Deviation 29).

`metrics(results, cases=…, …)` measures an eval run's case results (`CaseResult`, one per case and
repetition, read from `eval_case_results` by `case_result`) against the golden cases' labels (`CaseRow`); the
labels are judged as `ORDINAL` (`good` 2, `ok` 1, `poor` 0, L4614). It is pure: `Fraction` for every count
and rank, `Decimal` (28 significant digits) for what it reports, so a bound such as 0.15 or 0.6 is compared
exactly.

**Three observation sets, declared per metric** (Codex r4 B10):
- the **primary** set (`primary`): one observation per case, its first repetition. Every gate metric is
  computed over it, so which cases got a second repetition can change no gate;
- the **paired** set (`test_retest_sample`): the cases that have two numeric `score_mid` results, in id
  order, at most `RETEST_MAX_CASES`. Which cases get a second repetition is the runner's adaptive procedure
  (Task 19), so the sample is read from the results, never inferred from a schedule (Codex r2 B22). It is the
  test-retest SD's only;
- the **cost** set: every row, the mean cost's only.

**The metrics** (over the primary set unless named otherwise):
- `schema_validity`: the share that is `accepted`, `contested`, `refused` or `filtered_out` (a `failed`
  outcome is a schema or transport failure);
- `grounding_rate`: the share of the `accepted` ratings whose every evidence quote grounded (no grounding
  failure in the final validation; `None` without an accepted rating);
- `spearman_rho`: Spearman's rho of `(score_mid, ordinal(label))` over the published cases with a score,
  each side ranked with average ranks for ties (A3 G20), i.e. Pearson's r of the ranks; `None` for fewer than
  two such cases or a side whose ranks do not vary;
- `pairwise_agreement`: of the pairs of those cases with different labels, the share whose `score_mid`
  order is the label order (a tie in `score_mid` agrees with nothing);
- `false_exclusions`: the `good` cases that were hard-filtered or carry a binding hard knockout;
- `knockout_recall`: of the expected knockouts (the cases without expectations left out), the share among
  the case's applied binding knockouts; 1 when no case expects one (vacuous: the week-1 cases, A3 G21);
- `contested_rate`: the contested share of the published cases;
- `test_retest_sd` (the paired set): the mean of each case's SD of `score_mid` over its two repetitions (the
  sample SD, n - 1: `|a - b| / sqrt(2)`); `None` below `RETEST_MIN_CASES` cases, which is missing evidence
  and never a pass (Codex r1 #33);
- `per_profile`: for each profile version with at least `PROFILE_MIN_CASES` cases, its rho and contested
  rate, and `below_pooled` when either misses the pooled gate (reported, L4628);
- reported only: `critic_rate` (of the cases that reached the LLM, those whose critic ran) beside the
  candidate's `projected_critic_rate`; `admission_recall`, the share of the `good` cases with a known
  similarity that reaches `similarity_cutoff` (`None` without a cut-off or such a case); `mean_cost_usd`
  (the cost set) beside `central_cost_usd`.

**The gate** (`gate`, L4616-4631): `uncalibrated` while fewer than `GATE_MIN_CASES` of the user's cases are
active (`active_cases`: the caller's count, `validate.gate_cases`, which leaves the adversarial ones out);
`passed` iff every gate holds (`failed_gates` is empty): schema validity and grounding rate 1, rho at least
0.6, pairwise agreement at least 0.75, no false exclusion, knockout recall 1, contested rate at most 0.15 and
a measured test-retest SD of at most 5 (a missing one fails as `test_retest_missing`); `failed` otherwise.
`metrics_json` is what the run's terminal transaction merges into `eval_runs.metrics`: ids, counts and decimal
strings, the gate's state and, when it failed, `failed_gates` naming every gate that did (`failed` stays a
failed run's reason, Task 19's string).

**Canaries** (L4632): `canary_drift` compares each canary of a canary run with the first canary score of the
same case under the same key `(golden_case_id, rubric_version_id, prompt_schema_version_id, model_policy_id,
evaluation_policy_id)` (A1 issue 25, Deviation 29); `drifted` is `|score_mid - baseline| > 8`.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext
from fractions import Fraction
from itertools import combinations
from types import MappingProxyType
from typing import Final, Literal

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q
from jobhunter.settings.validate import GATE_MIN_CASES, GateState  # one threshold for both gate reads
from jobhunter.types import JSONValue

Label = Literal["good", "ok", "poor"]
Outcome = Literal["accepted", "contested", "refused", "failed", "filtered_out"]

ORDINAL: Final[Mapping[str, int]] = MappingProxyType({"good": 2, "ok": 1, "poor": 0})
PUBLISHED: Final[frozenset[str]] = frozenset({"accepted", "contested"})
SCHEMA_VALID: Final[frozenset[str]] = frozenset({"accepted", "contested", "refused", "filtered_out"})

RHO_MIN: Final = Decimal("0.6")
PAIRWISE_MIN: Final = Decimal("0.75")
CONTESTED_MAX: Final = Decimal("0.15")
RETEST_SD_MAX: Final = Decimal(5)
RETEST_MIN_CASES: Final = 5  # fewer cases scored twice: no test-retest SD (Codex r1 #33)
RETEST_MAX_CASES: Final = 10  # L4613: 10 cases x 2 runs
PROFILE_MIN_CASES: Final = 10  # L4614: per profile from 10 cases
CANARY_DRIFT_MAX: Final = Decimal(8)  # L4632: |ΔM| above 8
TEST_RETEST_MISSING: Final = "test_retest_missing"
HARD: Final = "hard"

_PRECISION: Final = 28
_JSON_QUANTUM: Final = Decimal("0.000001")


@dataclass(frozen=True)
class CaseRow:
    """What the metrics read of a golden case: its label, its expected knockouts (fact ids), its profile
    version, and the similarity of the target its label was taken from (`None` when unknown)."""

    label: Label
    expected_knockouts: tuple[str, ...]
    profile_version_id: int
    similarity: Decimal | None = None


@dataclass(frozen=True)
class CaseResult:
    """One case repetition of a run (`case_result`): its outcome, whether the critic ran, its score, the
    criteria the contested null-out nulled, whether every quote of a published rating grounded and its
    outcome is schema-valid, the fact ids of its binding knockouts (`hard_knockouts` those of severity
    `hard`), whether the hard filters excluded it, and the cost of its calls."""

    golden_case_id: int
    repetition: int
    outcome: Outcome
    critic_ran: bool
    score_mid: Decimal | None
    nulled_criteria: list[str]
    grounding_ok: bool
    schema_ok: bool
    knockouts_applied: list[str]
    hard_knockouts: list[str]
    hard_filtered: bool
    cost_usd: Decimal


@dataclass(frozen=True)
class ProfileMetrics:
    """One profile version's figures (reported; `below_pooled` flags a miss of the pooled gate)."""

    cases: int
    rho: Decimal | None
    contested_rate: Decimal | None
    below_pooled: bool


@dataclass(frozen=True)
class Metrics:
    """A run's calibration (module docstring); `observations` counts each observation set."""

    schema_validity: Decimal | None
    grounding_rate: Decimal | None
    spearman_rho: Decimal | None
    pairwise_agreement: Decimal | None
    false_exclusions: int
    knockout_recall: Decimal | None
    contested_rate: Decimal | None
    test_retest_sd: Decimal | None
    test_retest_sample: list[int]
    per_profile: dict[int, ProfileMetrics]
    critic_rate: Decimal | None
    projected_critic_rate: Decimal
    admission_recall: Decimal | None
    similarity_cutoff: Decimal | None
    mean_cost_usd: Decimal | None
    central_cost_usd: Decimal
    observations: dict[str, int]


# ---- the arithmetic ----------------------------------------------------------------------------------------


def _decimal(value: Fraction) -> Decimal:
    with localcontext(prec=_PRECISION):
        return Decimal(value.numerator) / Decimal(value.denominator)


def _ratio(numerator: int, denominator: int) -> Decimal | None:
    return None if denominator == 0 else _decimal(Fraction(numerator, denominator))


def _share[T](items: Sequence[T], keep: Callable[[T], bool]) -> Decimal | None:
    return _ratio(sum(1 for item in items if keep(item)), len(items))


def _ranks(values: Sequence[Fraction]) -> list[Fraction]:
    """The 1-based ranks of `values`, tied values sharing the average of their positions."""
    order = sorted(range(len(values)), key=values.__getitem__)
    ranks = [Fraction(0)] * len(values)
    start = 0
    while start < len(order):
        end = start
        while end + 1 < len(order) and values[order[end + 1]] == values[order[start]]:
            end += 1
        average = Fraction(start + end + 2, 2)
        for position in range(start, end + 1):
            ranks[order[position]] = average
        start = end + 1
    return ranks


def _spearman(pairs: Sequence[tuple[Decimal, int]]) -> Decimal | None:
    """Pearson's r of the average ranks of both sides (A3 G20); `None` when it is undefined."""
    if len(pairs) < 2:
        return None
    xs = _ranks([Fraction(score) for score, _ in pairs])
    ys = _ranks([Fraction(ordinal) for _, ordinal in pairs])
    zero = Fraction(0)
    mean_x, mean_y = sum(xs, zero) / len(xs), sum(ys, zero) / len(ys)
    dxs, dys = [x - mean_x for x in xs], [y - mean_y for y in ys]
    covariance = sum((dx * dy for dx, dy in zip(dxs, dys, strict=True)), zero)
    var_x, var_y = sum((dx * dx for dx in dxs), zero), sum((dy * dy for dy in dys), zero)
    if var_x == 0 or var_y == 0:
        return None
    with localcontext(prec=_PRECISION):
        return _decimal(covariance) / _decimal(var_x * var_y).sqrt()


def _pairwise(pairs: Sequence[tuple[Decimal, int]]) -> Decimal | None:
    ordered = [(a, b) for a, b in combinations(pairs, 2) if a[1] != b[1]]
    agreeing = sum(1 for a, b in ordered if (a[0] - b[0]) * (a[1] - b[1]) > 0)
    return _ratio(agreeing, len(ordered))


def _sd_of_two(a: Decimal, b: Decimal) -> Decimal:
    """The sample SD (n - 1) of two values: `sqrt((a - b)^2 / 2)`."""
    difference = Fraction(a) - Fraction(b)
    with localcontext(prec=_PRECISION):
        return _decimal(difference * difference / 2).sqrt()


# ---- the observation sets ----------------------------------------------------------------------------------


def primary(results: Sequence[CaseResult]) -> list[CaseResult]:
    """The primary set: each case's first repetition (module docstring)."""
    return [r for r in results if r.repetition == 1]


def _numeric(results: Sequence[CaseResult], repetition: int) -> dict[int, Decimal]:
    return {
        r.golden_case_id: r.score_mid
        for r in results
        if r.repetition == repetition and r.score_mid is not None
    }


def test_retest_sample(results: Sequence[CaseResult]) -> list[int]:
    """The paired set: the cases with a numeric `score_mid` in both repetitions, in id order, at most
    `RETEST_MAX_CASES` (Codex r2 B22)."""
    first, second = _numeric(results, 1), _numeric(results, 2)
    return sorted(set(first) & set(second))[:RETEST_MAX_CASES]


def _test_retest_sd(results: Sequence[CaseResult], sample: Sequence[int]) -> Decimal | None:
    if len(sample) < RETEST_MIN_CASES:
        return None
    first, second = _numeric(results, 1), _numeric(results, 2)
    with localcontext(prec=_PRECISION):
        total = sum((_sd_of_two(first[case], second[case]) for case in sample), Decimal(0))
        return total / len(sample)


# ---- the metrics -------------------------------------------------------------------------------------------


def _ranked(observed: Sequence[CaseResult], cases: Mapping[int, CaseRow]) -> list[tuple[Decimal, int]]:
    """`(score_mid, ordinal(label))` of the published observations with a score."""
    return [
        (r.score_mid, ORDINAL[cases[r.golden_case_id].label])
        for r in observed
        if r.outcome in PUBLISHED and r.score_mid is not None
    ]


def _contested_rate(observed: Sequence[CaseResult]) -> Decimal | None:
    published = [r for r in observed if r.outcome in PUBLISHED]
    return _share(published, lambda r: r.outcome == "contested")


def _rho_passes(rho: Decimal | None) -> bool:
    return rho is not None and rho >= RHO_MIN


def _contested_passes(rate: Decimal | None) -> bool:
    return rate is not None and rate <= CONTESTED_MAX


def _knockout_recall(observed: Sequence[CaseResult], cases: Mapping[int, CaseRow]) -> Decimal:
    expected = found = 0
    for r in observed:
        wanted = cases[r.golden_case_id].expected_knockouts
        expected += len(wanted)
        found += sum(1 for fact_id in wanted if fact_id in r.knockouts_applied)
    return Decimal(1) if expected == 0 else _decimal(Fraction(found, expected))


def _per_profile(observed: Sequence[CaseResult], cases: Mapping[int, CaseRow]) -> dict[int, ProfileMetrics]:
    by_profile: dict[int, list[CaseResult]] = {}
    for r in observed:
        by_profile.setdefault(cases[r.golden_case_id].profile_version_id, []).append(r)
    figures: dict[int, ProfileMetrics] = {}
    for profile_version_id, own in sorted(by_profile.items()):
        if len(own) < PROFILE_MIN_CASES:
            continue
        rho, contested = _spearman(_ranked(own, cases)), _contested_rate(own)
        below = not (_rho_passes(rho) and _contested_passes(contested))
        figures[profile_version_id] = ProfileMetrics(len(own), rho, contested, below)
    return figures


def _admission_recall(
    observed: Sequence[CaseResult], cases: Mapping[int, CaseRow], cutoff: Decimal | None
) -> Decimal | None:
    if cutoff is None:
        return None
    similarities = [
        similarity
        for r in observed
        if cases[r.golden_case_id].label == "good"
        and (similarity := cases[r.golden_case_id].similarity) is not None
    ]
    return _share(similarities, lambda similarity: similarity >= cutoff)


def metrics(
    results: Sequence[CaseResult],
    *,
    cases: Mapping[int, CaseRow],
    projected_critic_rate: Decimal,
    central_cost_usd: Decimal,
    similarity_cutoff: Decimal | None,
) -> Metrics:
    """The calibration of `results` (module docstring); `cases` holds the case of every result."""
    observed = primary(results)
    ranked = _ranked(observed, cases)
    sample = test_retest_sample(results)
    accepted = [r for r in observed if r.outcome == "accepted"]
    reached = [r for r in observed if not r.hard_filtered]
    false_exclusions = sum(
        1
        for r in observed
        if cases[r.golden_case_id].label == "good" and (r.hard_filtered or bool(r.hard_knockouts))
    )
    mean_cost = None
    if results:
        with localcontext(prec=_PRECISION):
            mean_cost = sum((r.cost_usd for r in results), Decimal(0)) / len(results)
    return Metrics(
        schema_validity=_share(observed, lambda r: r.schema_ok),
        grounding_rate=_share(accepted, lambda r: r.grounding_ok),
        spearman_rho=_spearman(ranked),
        pairwise_agreement=_pairwise(ranked),
        false_exclusions=false_exclusions,
        knockout_recall=_knockout_recall(observed, cases),
        contested_rate=_contested_rate(observed),
        test_retest_sd=_test_retest_sd(results, sample),
        test_retest_sample=sample,
        per_profile=_per_profile(observed, cases),
        critic_rate=_share(reached, lambda r: r.critic_ran),
        projected_critic_rate=projected_critic_rate,
        admission_recall=_admission_recall(observed, cases, similarity_cutoff),
        similarity_cutoff=similarity_cutoff,
        mean_cost_usd=mean_cost,
        central_cost_usd=central_cost_usd,
        observations={"primary": len(observed), "paired": len(sample), "cost": len(results)},
    )


# ---- the gate ----------------------------------------------------------------------------------------------


def failed_gates(m: Metrics) -> list[str]:
    """The gates `m` misses (module docstring), in the table's order; an unmeasured figure misses its gate."""
    checks: tuple[tuple[str, bool], ...] = (
        ("schema_validity", m.schema_validity == 1),
        ("grounding_rate", m.grounding_rate == 1),
        ("spearman_rho", _rho_passes(m.spearman_rho)),
        ("pairwise_agreement", m.pairwise_agreement is not None and m.pairwise_agreement >= PAIRWISE_MIN),
        ("false_exclusions", m.false_exclusions == 0),
        ("knockout_recall", m.knockout_recall == 1),
        ("contested_rate", _contested_passes(m.contested_rate)),
    )
    failed = [name for name, holds in checks if not holds]
    if m.test_retest_sd is None:
        failed.append(TEST_RETEST_MISSING)
    elif m.test_retest_sd > RETEST_SD_MAX:
        failed.append("test_retest_sd")
    return failed


def gate(m: Metrics, *, active_cases: int) -> GateState:
    """The gate's state (module docstring): `active_cases` is the number of the user's active golden cases."""
    if active_cases < GATE_MIN_CASES:
        return "uncalibrated"
    return "failed" if failed_gates(m) else "passed"


def _json(value: Decimal | None) -> JSONValue:
    return None if value is None else format(value.quantize(_JSON_QUANTUM), "f")


def metrics_json(m: Metrics, *, state: GateState) -> dict[str, JSONValue]:
    """The calibration as the run's `metrics` holds it beside Task 19's keys (module docstring): decimal
    strings at 6 places (the stored rates as they are), the sample's case ids, the gate's `state` and, when it
    failed, `failed_gates`."""
    doc: dict[str, JSONValue] = {
        "schema_validity": _json(m.schema_validity),
        "grounding_rate": _json(m.grounding_rate),
        "spearman_rho": _json(m.spearman_rho),
        "pairwise_agreement": _json(m.pairwise_agreement),
        "false_exclusions": m.false_exclusions,
        "knockout_recall": _json(m.knockout_recall),
        "contested_rate": _json(m.contested_rate),
        "test_retest_sd": _json(m.test_retest_sd),
        "test_retest_sample": list(m.test_retest_sample),
        "per_profile": {
            str(profile_version_id): {
                "cases": p.cases,
                "rho": _json(p.rho),
                "contested_rate": _json(p.contested_rate),
                "below_pooled": p.below_pooled,
            }
            for profile_version_id, p in m.per_profile.items()
        },
        "critic_rate": _json(m.critic_rate),
        "projected_critic_rate": format(m.projected_critic_rate, "f"),
        "admission_recall": _json(m.admission_recall),
        "similarity_cutoff": _json(m.similarity_cutoff),
        "mean_cost_usd": _json(m.mean_cost_usd),
        "central_cost_usd": format(m.central_cost_usd, "f"),
        "observations": dict(m.observations),
        "gate": state,
    }
    if state == "failed":
        doc["failed_gates"] = list(failed_gates(m))
    return doc


# ---- a stored row ------------------------------------------------------------------------------------------


def _object(value: JSONValue, key: str) -> dict[str, JSONValue]:
    found = value.get(key) if isinstance(value, dict) else None
    return found if isinstance(found, dict) else {}


def _list(value: JSONValue, key: str) -> list[JSONValue]:
    found = value.get(key) if isinstance(value, dict) else None
    return found if isinstance(found, list) else []


def case_result(
    *,
    golden_case_id: int,
    repetition: int,
    outcome: Outcome,
    critic_ran: bool,
    score_mid: Decimal | None,
    output: JSONValue,
    cost_usd: Decimal,
) -> CaseResult:
    """The `CaseResult` of an `eval_case_results` row (Task 19's shapes): a published case's nulled criteria
    (`output.publication.nulled_criteria`), its binding knockouts (`output.aggregation.knockouts` with
    `binding`, by fact id; `hard_knockouts` those of severity `hard`) and its final validation's grounding
    failures (`output.validation.grounding_failures`: none means every quote grounded); any other outcome has
    none of them."""
    published = outcome in PUBLISHED
    knockouts = [k for k in _list(_object(output, "aggregation"), "knockouts") if isinstance(k, dict)]
    binding = [k for k in knockouts if published and k.get("binding") is True]
    return CaseResult(
        golden_case_id=golden_case_id,
        repetition=repetition,
        outcome=outcome,
        critic_ran=critic_ran,
        score_mid=score_mid,
        nulled_criteria=[
            str(c) for c in _list(_object(output, "publication"), "nulled_criteria") if published
        ],
        grounding_ok=not (published and _list(_object(output, "validation"), "grounding_failures")),
        schema_ok=outcome in SCHEMA_VALID,
        knockouts_applied=[str(k.get("fact_id")) for k in binding],
        hard_knockouts=[str(k.get("fact_id")) for k in binding if k.get("severity") == HARD],
        hard_filtered=outcome == "filtered_out",
        cost_usd=cost_usd,
    )


# ---- canaries ----------------------------------------------------------------------------------------------


def drifted(score_mid: Decimal, baseline: Decimal) -> bool:
    """L4632: a canary drifts when its score is more than `CANARY_DRIFT_MAX` from its baseline."""
    return abs(score_mid - baseline) > CANARY_DRIFT_MAX


@dataclass(frozen=True)
class Drift:
    """A canary of a run that drifted: its case, its score, its baseline and the difference."""

    golden_case_id: int
    score_mid: Decimal
    baseline: Decimal
    delta: Decimal


# Each canary of the run with its baseline: the first numeric score of the same case in a completed canary
# run under the same key (the run's rubric, prompt-schema version, model policy and evaluation policy).
_CANARY_BASELINES: Final = q(
    "SELECT c.golden_case_id, c.score_mid, ("
    "SELECT b.score_mid FROM eval_case_results b JOIN eval_runs f ON f.id = b.eval_run_id "
    "WHERE f.kind = 'canary' AND f.status = 'completed' AND b.golden_case_id = c.golden_case_id "
    "AND f.rubric_version_id = r.rubric_version_id "
    "AND f.prompt_schema_version_id = r.prompt_schema_version_id "
    "AND f.model_policy_id = r.model_policy_id AND f.evaluation_policy_id = r.evaluation_policy_id "
    "AND b.score_mid IS NOT NULL ORDER BY f.id, b.repetition LIMIT 1) AS baseline "
    "FROM eval_case_results c JOIN eval_runs r ON r.id = c.eval_run_id "
    "WHERE c.eval_run_id = :r AND r.kind = 'canary' AND c.score_mid IS NOT NULL ORDER BY c.golden_case_id"
)


async def canary_drift(conn: AsyncConnection, *, eval_run_id: int) -> list[Drift]:
    """The drifted canaries of canary run `eval_run_id` (module docstring), plain reads in the caller's
    transaction: the run's terminal transaction, after its `completed`, so a first run is its own baseline (a
    canary with no completed score under its key yet has no baseline and cannot drift)."""
    rows = await conn.execute(_CANARY_BASELINES, {"r": eval_run_id})
    drifts: list[Drift] = []
    for row in rows:
        if row.baseline is None:
            continue
        score, baseline = Decimal(row.score_mid), Decimal(row.baseline)
        if drifted(score, baseline):
            drifts.append(Drift(int(row.golden_case_id), score, baseline, score - baseline))
    return drifts
