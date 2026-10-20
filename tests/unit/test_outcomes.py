import pytest
from jobhunter.graphs.collect.state import FetchOutcome
from jobhunter.graphs.outcomes import OutcomeConflict, merge_outcomes, sum_counters

A: dict[str, FetchOutcome] = {
    "fr:1": {"work_id": "fr:1", "status": "succeeded", "items": 10, "new_postings": 3}
}


def test_reducer_is_idempotent_commutative_and_conflicting() -> None:
    B: dict[str, FetchOutcome] = {
        "fr:2": {"work_id": "fr:2", "status": "failed", "items": 0, "new_postings": 0}
    }
    assert merge_outcomes(A, A) == A and merge_outcomes(A, B) == merge_outcomes(B, A) == {**A, **B}
    with pytest.raises(OutcomeConflict):
        merge_outcomes(A, {"fr:1": {**A["fr:1"], "items": 9}})
    assert sum_counters({"a": 1}, {"a": 2, "b": 1}) == {"a": 3, "b": 1}


def test_the_reducers_leave_their_inputs_alone() -> None:
    left, right = dict(A), {"a": 1}
    merge_outcomes(left, {"fr:3": {"work_id": "fr:3", "status": "skipped", "items": 0, "new_postings": 0}})
    sum_counters(right, {"a": 1})
    assert left == A and right == {"a": 1}
