"""The scoring-state buckets of the ranking generation (spec §7.1 L1255; plan M4 Task 3, B30).

A target state change bumps the ranking generation only when it moves the target to another bucket of the
Jobs list (`ranking.crosses_bucket`): the unscored section holds every state that waits for a score, so a
claim (`pending → in_progress`) or a deferral (`pending → budget_deferred`) changes nothing a page shows.
`None` is a job without a target of the tuple, which the unscored section lists too.
"""

import pytest
from jobhunter.db import ranking
from jobhunter.db.transitions import TRANSITIONS

# The sixteen states of the `score_targets.state` CHECK (migration 0008), in its order.
CHECK_STATES = (
    "pending",
    "filtered_out",
    "deferred_duplicate",
    "budget_deferred",
    "in_progress",
    "in_batch",
    "published_accepted",
    "published_contested",
    "refused",
    "failed_max_tokens",
    "failed_invalid_output",
    "failed_request",
    "failed_transient",
    "failed_attempts_exhausted",
    "expired_unscored",
    "cancelled",
)


def test_every_target_state_has_a_bucket() -> None:
    assert set(ranking.STATE_BUCKET) == {None, *CHECK_STATES}
    registry = {state for edge in TRANSITIONS["score_targets"].allowed for state in edge}
    assert registry == set(CHECK_STATES)  # the registry's states are the CHECK's: none is left without one
    assert dict(ranking.STATE_BUCKET) == {
        None: "unscored",
        "pending": "unscored",
        "budget_deferred": "unscored",
        "in_progress": "unscored",
        "in_batch": "unscored",
        "deferred_duplicate": "grouped",
        "published_accepted": "scored",
        "published_contested": "scored",
        "filtered_out": "filtered",
        "refused": "failed",
        "failed_max_tokens": "failed",
        "failed_invalid_output": "failed",
        "failed_request": "failed",
        "failed_transient": "failed",
        "failed_attempts_exhausted": "failed",
        "expired_unscored": "expired",
        "cancelled": "hidden",
    }


def test_the_admission_states_share_a_bucket() -> None:
    for one, other in (
        ("pending", "in_progress"),
        ("pending", "budget_deferred"),
        ("in_progress", "in_batch"),
    ):
        assert not ranking.crosses_bucket(one, other), (one, other)
        assert not ranking.crosses_bucket(other, one), (other, one)
    assert ranking.crosses_bucket("pending", "deferred_duplicate")  # the row leaves the top level
    assert ranking.crosses_bucket("in_progress", "failed_attempts_exhausted")


@pytest.mark.parametrize(
    ("from_state", "to_state"),
    [
        (None, "pending"),
        ("failed_transient", "failed_attempts_exhausted"),
        ("published_accepted", "published_contested"),
    ],
)
def test_a_move_within_its_bucket_does_not_cross(from_state: str | None, to_state: str) -> None:
    assert not ranking.crosses_bucket(from_state, to_state)


@pytest.mark.parametrize(
    ("from_state", "to_state"),
    [
        ("pending", "deferred_duplicate"),
        ("deferred_duplicate", "pending"),
        ("in_progress", "failed_attempts_exhausted"),
        ("in_progress", "published_accepted"),
        ("pending", "filtered_out"),
        ("pending", "expired_unscored"),
        ("pending", "cancelled"),
        ("in_batch", "refused"),
        (None, "filtered_out"),
    ],
)
def test_a_move_out_of_its_bucket_crosses(from_state: str | None, to_state: str) -> None:
    assert ranking.crosses_bucket(from_state, to_state)


def test_a_state_outside_the_check_is_refused() -> None:
    with pytest.raises(KeyError):
        ranking.crosses_bucket("pending", "scored")
