from datetime import UTC, datetime, timedelta

import pytest
from jobhunter.observability.health import RunAgeState, RunFreshness, run_age_state

FIRST = datetime(2026, 10, 1, tzinfo=UTC)
HOUR = timedelta(hours=1)


def freshness(*, any_run: bool, success_h: float | None) -> RunFreshness:
    success = None if success_h is None else FIRST + success_h * HOUR
    return RunFreshness(
        any_run=any_run,
        last_success_at=success,
        last_full_success_at=success,
        first_settings_at=FIRST,
        interval_s=3600,
    )


@pytest.mark.parametrize(
    ("now_h", "success_h", "any_run", "state"),
    [
        (1.0, None, False, "none"),  # no run recorded yet, within the grace
        (2.0, None, False, "none"),  # the grace includes its end
        (2.1, None, False, "critical"),  # no run at all after the grace: the stale_data alert is open (T17-g)
        (1.0, None, True, "grace"),  # within 2 intervals of the first settings version
        (2.0, 1.0, True, "grace"),
        (5.0, 4.0, True, "ok"),
        (5.0, 3.5, True, "ok"),  # exactly 1.5 intervals: not yet warn
        (5.0, 3.4, True, "warn"),
        (5.0, 3.0, True, "warn"),  # exactly 2 intervals: not yet critical
        (5.0, 2.9, True, "critical"),
        (5.0, None, True, "critical"),  # nothing succeeded: aged from the first settings version
    ],
)
def test_run_age_state(now_h: float, success_h: float | None, any_run: bool, state: RunAgeState) -> None:
    assert run_age_state(FIRST + now_h * HOUR, freshness(any_run=any_run, success_h=success_h)) == state


def test_run_age_state_is_none_without_a_seeded_schedule() -> None:
    unseeded = RunFreshness(
        any_run=True, last_success_at=None, last_full_success_at=None, first_settings_at=None, interval_s=None
    )
    assert run_age_state(FIRST, unseeded) == "none"
