"""The ledger's pure cores (spec §6.3, §6.7): the outcome of a probe as `settle` records it."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from jobhunter.quota import ledger
from jobhunter.quota.pacing import Admit, Deny, decide
from jobhunter.quota.windows import PROBE_RETRY, Window, bootstrap_window
from jobhunter.sources.base import Observation

from tests.support.quota import SETTINGS

T0 = datetime(2026, 1, 1, tzinfo=UTC)
SIX_H = 21600


def exhausted_window() -> Window:
    """A JSearch window a 429 has exhausted, whose re-probe is due 12 h later."""
    w = bootstrap_window("jsearch", T0, SETTINGS, interval_s=SIX_H, account=None)
    assert w is not None
    return replace(w, state="exhausted_until_reset", next_probe_at=T0 + PROBE_RETRY, last_observed_at=T0)


def normal_decision(w: Window, at: datetime) -> Admit | Deny:
    return decide(
        w,
        provider="jsearch",
        request_class="normal",
        units=1,
        used=6,
        run_normal_count=0,
        hour_ok=True,
        adzuna_ok=True,
        now=at,
        per_run_cap=10,
    )


def test_a_served_reprobe_that_validates_nothing_is_a_failed_probe() -> None:
    w = exhausted_window()
    due = T0 + PROBE_RETRY
    d = ledger.admission(
        w,
        provider="jsearch",
        request_class="normal",
        units=1,
        used=5,
        run_normal_count=0,
        hour_ok=True,
        adzuna_ok=True,
        now=due,
        settings=SETTINGS,
        interval_s=SIX_H,
    )
    assert d == Admit("probe", tokens_used=0, starts_probe=True)
    # the re-probe is served (2xx) but its headers validate nothing: the window stays exhausted
    settled_at = due + timedelta(seconds=1)
    after, changes, _ = ledger.settle_window(
        w,
        Observation(settled_at, 200),
        SETTINGS,
        provider="jsearch",
        state="billed",
        http_status=200,
        probe=True,
        used_units=6,
        entry_units=1,
        interval_s=SIX_H,
        now=settled_at,
    )
    assert ledger.probe_outcome("billed", 200, after) == "failed"
    assert (changes, after.state, after.probe_failed, after.next_probe_at) == (
        [],
        "exhausted_until_reset",
        True,
        settled_at + PROBE_RETRY,
    )
    # exhausted until the next probe, which is admitted 12 h later (not only when the window ends)
    retry_at = settled_at + PROBE_RETRY
    assert normal_decision(after, retry_at - timedelta(microseconds=1)) == Deny("exhausted")
    assert normal_decision(after, retry_at) == Admit("probe", tokens_used=0, starts_probe=True)


def test_a_reprobe_whose_headers_validate_the_window_ends_the_exhaustion() -> None:
    w = exhausted_window()
    due = T0 + PROBE_RETRY
    w.probe_used, w.next_probe_at = True, due + PROBE_RETRY  # the re-probe was admitted at `due`
    reset = due + timedelta(days=10)
    after, _, _ = ledger.settle_window(
        w,
        Observation(due, 200, reset_at=reset, limit=200, remaining=150),
        SETTINGS,
        provider="jsearch",
        state="billed",
        http_status=200,
        probe=True,
        used_units=6,
        entry_units=1,
        interval_s=SIX_H,
        now=due,
    )
    assert (after.state, after.window_end, after.probe_used, after.probe_failed, after.next_probe_at) == (
        "validated",
        reset,
        False,
        False,
        None,
    )
    assert ledger.probe_outcome("billed", 200, after) == "validated"
    assert normal_decision(after, due) == Admit("normal", tokens_used=1, starts_probe=False)
