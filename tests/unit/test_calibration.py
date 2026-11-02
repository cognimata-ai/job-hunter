"""The calibration metrics and the eval gate (spec §11.13 L4610-4631; plan Task 20, Codex r1 #33, r2 B22,
r4 B10, A3 G20, G21, Deviation 29): `scoring.calibration`, pure.

Three observation sets, declared per metric: the primary set (one result per case, its first repetition) for
every gate metric, the paired set (the cases with two numeric scores) for the test-retest SD only, and every
row for the mean cost only. Every datum is invented.
"""

import dataclasses
from dataclasses import replace
from decimal import Decimal
from typing import Any, Final

import pytest
from jobhunter.scoring import calibration
from jobhunter.scoring.calibration import CaseResult, CaseRow, Label, gate, metrics

PV_A: Final = 101  # two profile versions of 15 cases each: both reach the per-profile minimum of 10
PV_B: Final = 102
CLEARANCE: Final = "security_clearance:clearance"
GERMAN: Final = "mandatory_language:de"


def result(case_id: int, score: str | None = None, **fields: Any) -> CaseResult:
    """An accepted, grounded, schema-valid first repetition of `case_id` at `score`, costing $0.05."""
    values: dict[str, Any] = {
        "golden_case_id": case_id,
        "repetition": 1,
        "outcome": "accepted",
        "critic_ran": False,
        "score_mid": None if score is None else Decimal(score),
        "nulled_criteria": [],
        "grounding_ok": True,
        "schema_ok": True,
        "knockouts_applied": [],
        "hard_knockouts": [],
        "hard_filtered": False,
        "cost_usd": Decimal("0.05"),
        **fields,
    }
    return CaseResult(**values)


def label_of(case_id: int) -> Label:
    return "poor" if case_id <= 10 else "ok" if case_id <= 20 else "good"


def score_of(case_id: int) -> str:
    """Poor cases 20-29, ok cases 40-49, good cases 70-79: the scores follow the labels."""
    base = {"poor": 10, "ok": 30, "good": 50}[label_of(case_id)]
    return str(base + case_id)


CASES: Final = {
    i: CaseRow(
        label=label_of(i),
        expected_knockouts=(CLEARANCE,) if i == 1 else (),
        profile_version_id=PV_A if i <= 15 else PV_B,
    )
    for i in range(1, 31)
}
FIRST: Final = [
    result(i, score_of(i), knockouts_applied=[CLEARANCE], hard_knockouts=[CLEARANCE])
    if i == 1
    else result(i, score_of(i))
    for i in range(1, 31)
]
RETESTS: Final = [replace(result(i, score_of(i)), repetition=2) for i in range(1, 11)]  # SD 0 over ten cases
PERFECT_RESULTS: Final = [*FIRST, *RETESTS]
RATE: Final = Decimal("0.45")
CENTRAL: Final = Decimal("0.0526")


def measured(results: list[CaseResult], cases: dict[int, CaseRow] = CASES, **kw: Any) -> calibration.Metrics:
    values: dict[str, Any] = {
        "cases": cases,
        "projected_critic_rate": RATE,
        "central_cost_usd": CENTRAL,
        "similarity_cutoff": None,
        **kw,
    }
    return metrics(results, **values)


def test_the_gate_thresholds_each_fail_alone() -> None:
    base = metrics(
        PERFECT_RESULTS,
        cases=CASES,
        projected_critic_rate=Decimal("0.45"),
        central_cost_usd=Decimal("0.0526"),
        similarity_cutoff=None,
    )
    assert gate(base, active_cases=30) == "passed" and gate(base, active_cases=29) == "uncalibrated"
    assert calibration.failed_gates(base) == []
    for field, bad in [
        ("spearman_rho", Decimal("0.59")),
        ("pairwise_agreement", Decimal("0.74")),
        ("false_exclusions", 1),
        ("knockout_recall", Decimal("0.99")),
        ("contested_rate", Decimal("0.151")),
        ("test_retest_sd", Decimal("5.01")),
        ("test_retest_sd", None),  # missing evidence never passes (Codex r1 #33)
        ("schema_validity", Decimal("0.99")),
        ("grounding_rate", Decimal("0.99")),
    ]:
        changed: dict[str, Any] = {field: bad}
        assert gate(replace(base, **changed), active_cases=30) == "failed", field
    # each bound itself passes: >= 0.6, >= 0.75, <= 0.15 and <= 5
    at_bounds = replace(
        base,
        spearman_rho=Decimal("0.6"),
        pairwise_agreement=Decimal("0.75"),
        contested_rate=Decimal("0.15"),
        test_retest_sd=Decimal(5),
    )
    assert gate(at_bounds, active_cases=30) == "passed"


def test_the_failed_gates_name_every_gate_that_failed() -> None:
    base = measured(PERFECT_RESULTS)
    both = replace(base, spearman_rho=Decimal("0.5"), contested_rate=Decimal("0.2"), test_retest_sd=None)
    assert calibration.failed_gates(both) == ["spearman_rho", "contested_rate", "test_retest_missing"]
    assert calibration.failed_gates(replace(base, test_retest_sd=Decimal(6))) == ["test_retest_sd"]
    # an unmeasured rate (no published case, no accepted rating) is a failed gate, never a pass
    unmeasured = replace(base, spearman_rho=None, grounding_rate=None, contested_rate=None)
    assert calibration.failed_gates(unmeasured) == ["grounding_rate", "spearman_rho", "contested_rate"]


def test_the_perfect_results_measure_as_perfect() -> None:
    m = measured(PERFECT_RESULTS)
    one, zero = Decimal(1), Decimal(0)
    assert (m.schema_validity, m.grounding_rate, m.pairwise_agreement) == (one, one, one)
    # rho of the ranks 1-30 against the labels' average ranks 5.5, 15.5 and 25.5: sqrt(2000 / 2247.5)
    assert m.spearman_rho is not None
    assert m.spearman_rho.quantize(Decimal("1e-12")) == (Decimal(2000) / Decimal("2247.5")).sqrt().quantize(
        Decimal("1e-12")
    )
    assert (m.false_exclusions, m.knockout_recall, m.contested_rate, m.test_retest_sd) == (0, one, zero, zero)
    assert m.test_retest_sample == list(range(1, 11))
    assert set(m.per_profile) == {PV_A, PV_B}
    assert all(not p.below_pooled and p.cases == 15 for p in m.per_profile.values())
    assert (m.critic_rate, m.projected_critic_rate, m.mean_cost_usd, m.central_cost_usd) == (
        zero,
        RATE,
        Decimal("0.05"),
        CENTRAL,
    )
    assert m.admission_recall is None  # no cut-off


def test_the_test_retest_sample_is_read_from_the_results() -> None:
    """Codex r2 B22: the sample is the cases that have two numeric scores (the runner's adaptive second
    repetitions, Task 19), in id order, at most ten; never a schedule inferred from the case ids."""
    firsts = [result(i, "50") for i in range(1, 15)]
    deltas = {3: 2, 4: 2, 5: 2, 6: 2}  # four cases 2 points apart, six identical
    seconds = [
        replace(result(i, str(50 + deltas.get(i, 0))), repetition=2) for i in range(3, 14)
    ]  # cases 3-13 scored twice: 13 is past the ten
    refused = replace(result(14, None, outcome="refused"), repetition=2)  # case 14's second has no score
    results = [*firsts, *seconds, refused]
    assert calibration.test_retest_sample(results) == list(range(3, 13))
    cases = {i: CaseRow(label="ok", expected_knockouts=(), profile_version_id=PV_A) for i in range(1, 15)}
    m = measured(results, cases)
    # the mean of each case's SD (n - 1) over cases 3-12: four of sqrt(2^2 / 2) and six of 0
    assert m.test_retest_sd is not None
    assert m.test_retest_sd.quantize(Decimal("0.000001")) == Decimal("0.565685")
    assert m.test_retest_sample == list(range(3, 13))

    # only four cases scored twice: the SD is missing evidence, and the gate fails with it
    four = [*FIRST, *RETESTS[:4]]
    assert calibration.test_retest_sample(four) == [1, 2, 3, 4]
    missing = measured(four)
    assert missing.test_retest_sd is None
    assert gate(missing, active_cases=30) == "failed"
    assert calibration.failed_gates(missing) == [calibration.TEST_RETEST_MISSING]
    five = measured([*FIRST, *RETESTS[:5]])  # five is enough
    assert five.test_retest_sd == 0 and gate(five, active_cases=30) == "passed"


def test_retests_cannot_dilute_a_primary_gate() -> None:
    """Codex r4 B10: 30 first repetitions, five contested (5 of 29 published, a failed contested rate), one
    failed and one accepted rating with an ungrounded quote, plus ten accepted second repetitions at other
    scores and costs (counted, they would make it 5 of 39, a pass): every gate metric is the first
    repetitions' alone; only the test-retest SD and the mean cost see the extra rows."""
    first = [
        result(
            i,
            None if i == 6 else score_of(i),
            outcome="contested" if i <= 5 else "failed" if i == 6 else "accepted",
            schema_ok=i != 6,
            grounding_ok=i != 7,
            critic_ran=i <= 5,
        )
        for i in range(1, 31)
    ]
    # the good cases' second scores fall below the ok ones: counted, they would lower rho and the pairs
    second = [replace(result(i, "35", cost_usd=Decimal("0.25")), repetition=2) for i in range(21, 31)]
    alone = measured(first)
    pooled = measured([*first, *second])
    assert pooled.contested_rate == alone.contested_rate
    assert pooled.contested_rate is not None
    assert pooled.contested_rate.quantize(Decimal("0.0001")) == Decimal("0.1724")  # 5 of 29 published
    for name in (
        "schema_validity",
        "grounding_rate",
        "spearman_rho",
        "pairwise_agreement",
        "false_exclusions",
        "knockout_recall",
        "contested_rate",
        "per_profile",
        "critic_rate",
        "admission_recall",
    ):
        assert getattr(pooled, name) == getattr(alone, name), name
    assert (alone.schema_validity, alone.grounding_rate) == (Decimal(29) / 30, Decimal(23) / 24)
    assert "contested_rate" in calibration.failed_gates(pooled)
    assert alone.test_retest_sd is None and pooled.test_retest_sd is not None  # the paired set
    assert (alone.mean_cost_usd, pooled.mean_cost_usd) == (Decimal("0.05"), Decimal("0.1"))  # every row


def test_spearman_uses_average_ranks_for_ties() -> None:
    """A3 G20: three `ok` cases with distinct M and two `good` with equal M. With average ranks M ranks
    1, 2, 3, 4.5, 4.5 and the labels 2, 2, 2, 4.5, 4.5, so rho = 7.5 / sqrt(9.5 x 7.5) = sqrt(15 / 19)."""
    cases = {
        i: CaseRow(label="ok" if i <= 3 else "good", expected_knockouts=(), profile_version_id=PV_A)
        for i in range(1, 6)
    }
    results = [result(1, "50"), result(2, "60"), result(3, "70"), result(4, "80"), result(5, "80")]
    m = measured(results, cases)
    hand = (Decimal(15) / Decimal(19)).sqrt()
    assert m.spearman_rho is not None
    assert m.spearman_rho.quantize(Decimal("1e-20")) == hand.quantize(Decimal("1e-20"))
    assert m.pairwise_agreement == 1  # each of the six ok-good pairs is ordered as its labels


def test_pairwise_agreement_counts_only_pairs_with_different_labels() -> None:
    cases = {
        1: CaseRow(label="poor", expected_knockouts=(), profile_version_id=PV_A),
        2: CaseRow(label="ok", expected_knockouts=(), profile_version_id=PV_A),
        3: CaseRow(label="ok", expected_knockouts=(), profile_version_id=PV_A),
        4: CaseRow(label="good", expected_knockouts=(), profile_version_id=PV_A),
    }
    # poor 50 ties ok 50 (no agreement), ok 40 is below poor (none); ok-ok is no pair; good 90 tops all
    results = [result(1, "50"), result(2, "50"), result(3, "40"), result(4, "90")]
    m = measured(results, cases)
    assert m.pairwise_agreement == Decimal(3) / 5  # of (1,2), (1,3), (1,4), (2,4), (3,4): the last three


def test_only_published_numeric_scores_are_ranked_and_contested_rate_counts_published() -> None:
    cases = {
        i: CaseRow(label=label_of(i * 10), expected_knockouts=(), profile_version_id=PV_A)
        for i in range(1, 4)
    }
    results = [
        result(1, "20"),
        result(2, None, outcome="filtered_out", hard_filtered=True),
        result(3, "80", outcome="contested", critic_ran=True),
    ]
    m = measured(results, cases)
    assert m.contested_rate == Decimal(1) / 2  # of the two published
    assert m.critic_rate == Decimal(1) / 2  # of the two that reached the LLM: the filtered one never did
    assert m.schema_validity == 1  # filtered_out is a valid outcome


def test_false_exclusions_are_good_cases_filtered_or_with_a_binding_hard_knockout() -> None:
    cases = {
        1: CaseRow(label="good", expected_knockouts=(), profile_version_id=PV_A),
        2: CaseRow(label="good", expected_knockouts=(), profile_version_id=PV_A),
        3: CaseRow(label="good", expected_knockouts=(), profile_version_id=PV_A),
        4: CaseRow(label="poor", expected_knockouts=(), profile_version_id=PV_A),
    }
    results = [
        result(1, None, outcome="filtered_out", hard_filtered=True),
        result(2, "30", knockouts_applied=[GERMAN], hard_knockouts=[GERMAN]),
        result(3, "70", knockouts_applied=["salary_floor:salary"]),  # a soft knockout excludes nothing
        result(4, None, outcome="filtered_out", hard_filtered=True),  # a poor case may be filtered
    ]
    assert measured(results, cases).false_exclusions == 2


def test_knockout_recall_counts_the_expected_knockouts_and_is_vacuous_without_any() -> None:
    """A3 G21: the cases without expectations are left out; with none at all (week 1) the recall is 1."""
    cases = {
        1: CaseRow(label="poor", expected_knockouts=(CLEARANCE,), profile_version_id=PV_A),
        2: CaseRow(label="poor", expected_knockouts=(GERMAN, CLEARANCE), profile_version_id=PV_A),
        3: CaseRow(label="ok", expected_knockouts=(), profile_version_id=PV_A),
    }
    results = [
        result(1, "20", knockouts_applied=[CLEARANCE], hard_knockouts=[CLEARANCE]),
        result(2, "25", knockouts_applied=[GERMAN]),  # the clearance did not bind
        result(3, "40", knockouts_applied=[GERMAN]),  # not expected: no part of the recall
    ]
    assert measured(results, cases).knockout_recall == Decimal(2) / 3
    week1 = {i: replace(c, expected_knockouts=()) for i, c in cases.items()}
    assert measured(results, week1).knockout_recall == 1


def test_a_profile_below_the_pooled_gate_is_flagged() -> None:
    """Profile A's ten cases score against their labels (the poor ones highest); profile B's nine are too
    few for a figure of their own."""
    cases = {
        i: CaseRow(label="poor" if i <= 5 else "good", expected_knockouts=(), profile_version_id=PV_A)
        for i in range(1, 11)
    }
    cases.update(
        {i: CaseRow(label=label_of(i), expected_knockouts=(), profile_version_id=PV_B) for i in range(21, 30)}
    )
    results = [result(i, str(100 - i)) for i in range(1, 11)] + [
        result(i, score_of(i)) for i in range(21, 30)
    ]
    m = measured(results, cases)
    assert set(m.per_profile) == {PV_A}
    profile = m.per_profile[PV_A]
    assert (profile.cases, profile.below_pooled, profile.contested_rate) == (10, True, Decimal(0))
    assert profile.rho is not None and profile.rho < 0
    fine = measured([result(i, str(i)) for i in range(1, 11)], cases)  # in label order: not flagged
    assert not fine.per_profile[PV_A].below_pooled


def test_admission_recall_reads_each_good_cases_similarity_against_the_cut_off() -> None:
    cases = {
        1: CaseRow(label="good", expected_knockouts=(), profile_version_id=PV_A, similarity=Decimal("0.71")),
        2: CaseRow(label="good", expected_knockouts=(), profile_version_id=PV_A, similarity=Decimal("0.69")),
        3: CaseRow(label="good", expected_knockouts=(), profile_version_id=PV_A, similarity=Decimal("0.70")),
        4: CaseRow(label="poor", expected_knockouts=(), profile_version_id=PV_A, similarity=Decimal("0.9")),
        5: CaseRow(label="good", expected_knockouts=(), profile_version_id=PV_A),  # no similarity: left out
    }
    results = [result(i, "70") for i in range(1, 6)]
    assert measured(results, cases, similarity_cutoff=Decimal("0.70")).admission_recall == Decimal(2) / 3
    assert measured(results, cases).admission_recall is None  # no cut-off


def test_the_metrics_json_holds_numbers_and_the_gate() -> None:
    m = measured(PERFECT_RESULTS)
    doc = calibration.metrics_json(m, state="passed")
    assert doc["gate"] == "passed" and "failed" not in doc
    assert doc["schema_validity"] == "1.000000" and doc["false_exclusions"] == 0
    assert doc["test_retest_sample"] == list(range(1, 11))
    per_profile = doc["per_profile"]
    assert isinstance(per_profile, dict) and set(per_profile) == {str(PV_A), str(PV_B)}
    profile = per_profile[str(PV_A)]
    assert isinstance(profile, dict) and isinstance(profile["rho"], str)
    assert {k: v for k, v in profile.items() if k != "rho"} == {
        "cases": 15,
        "contested_rate": "0.000000",
        "below_pooled": False,
    }
    assert doc["admission_recall"] is None and doc["projected_critic_rate"] == "0.45"
    assert doc["observations"] == {"primary": 30, "paired": 10, "cost": 40}
    failed = calibration.metrics_json(replace(m, test_retest_sd=None), state="failed")
    # the gates under their own key: `metrics.failed` stays a failed run's reason (fix round 1, item 6)
    assert failed["failed_gates"] == ["test_retest_missing"] and failed["test_retest_sd"] is None
    assert "failed" not in failed and "failed_gates" not in doc


def test_a_case_result_is_read_from_its_stored_row() -> None:
    """The shapes of Task 19: a published case's `output.publication.nulled_criteria`, its aggregation's
    knockout records (binding or not, by severity) and its final validation's grounding failures."""
    output: Any = {
        "publication": {"nulled_criteria": ["skills"]},
        "aggregation": {
            "knockouts": [
                {"fact_id": GERMAN, "binding": True, "severity": "hard"},
                {"fact_id": "salary_floor:salary", "binding": True, "severity": "soft"},
                {"fact_id": CLEARANCE, "binding": False, "severity": "hard"},
            ]
        },
        "validation": {
            "repairs": [],
            "grounding_failures": [{"path": "criteria.skills", "code": "quote_ungrounded"}],
        },
    }
    made = calibration.case_result(
        golden_case_id=4,
        repetition=2,
        outcome="contested",
        critic_ran=True,
        score_mid=Decimal("41.50"),
        output=output,
        cost_usd=Decimal("0.1"),
    )
    assert dataclasses.asdict(made) == {
        "golden_case_id": 4,
        "repetition": 2,
        "outcome": "contested",
        "critic_ran": True,
        "score_mid": Decimal("41.50"),
        "nulled_criteria": ["skills"],
        "grounding_ok": False,
        "schema_ok": True,
        "knockouts_applied": [GERMAN, "salary_floor:salary"],
        "hard_knockouts": [GERMAN],
        "hard_filtered": False,
        "cost_usd": Decimal("0.1"),
    }
    filtered = calibration.case_result(
        golden_case_id=5,
        repetition=1,
        outcome="filtered_out",
        critic_ran=False,
        score_mid=None,
        output={"filters": ["employment_type_excluded"]},
        cost_usd=Decimal(0),
    )
    assert (
        filtered.hard_filtered,
        filtered.schema_ok,
        filtered.grounding_ok,
        filtered.knockouts_applied,
    ) == (
        True,
        True,
        True,
        [],
    )
    failed = calibration.case_result(
        golden_case_id=6,
        repetition=1,
        outcome="failed",
        critic_ran=False,
        score_mid=None,
        output={"error": "InvalidOutput", "rounds": []},
        cost_usd=Decimal("0.2"),
    )
    assert not failed.schema_ok


@pytest.mark.parametrize(
    ("score", "baseline", "drifts"),
    [("68", "60", False), ("52", "60", False), ("68.01", "60", True), ("69", "60", True), ("51", "60", True)],
)
def test_a_canary_drifts_beyond_eight_points(score: str, baseline: str, drifts: bool) -> None:
    assert calibration.drifted(Decimal(score), Decimal(baseline)) is drifts
