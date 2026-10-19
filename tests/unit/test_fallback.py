from typing import get_args

import pytest
from jobhunter.quota.fallback import CellOutcome, eligible


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        ("served", False),
        ("paced", False),
        ("reserve_held", False),
        ("exhausted", True),
        ("awaiting_after_failed_probe", True),
        ("awaiting_probe_in_flight", False),
        ("ambiguous", False),
        ("transient", False),
        ("breaker_open", True),
        ("auth_failed", True),
        ("contract_broken", True),
        ("shutdown", False),
        ("run_cancelled", False),
        ("window_cap", False),
    ],
)
def test_fallback_table(outcome: CellOutcome, expected: bool) -> None:
    assert eligible(outcome) is expected


def test_every_outcome_has_a_row() -> None:
    eligible_outcomes = {o for o in get_args(CellOutcome) if eligible(o)}
    assert eligible_outcomes == {
        "exhausted",
        "awaiting_after_failed_probe",
        "breaker_open",
        "auth_failed",
        "contract_broken",
    }
    assert len(get_args(CellOutcome)) == 14
