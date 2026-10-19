"""The SerpApi fallback trigger (spec §6.6): which JSearch outcomes of a cell let `plan_fallback` plan
SerpApi.

SerpApi is planned only for JSearch cells this run did not serve, and only when the cell's terminal JSearch
outcome means the provider cannot serve it now: provider-exhausted (including a failed probe, which counts as
exhausted until the next one), circuit-open, auth-failed or contract-broken. Local policy (`paced`,
`reserve_held`, a `window_cap`), an outcome that may still have been served (`ambiguous`), a transient
failure, a probe in flight and the run ending never are.

`plan_fallback` asks `ledger.project_allowance("serpapi", …, account=read)` for the allowance itself, with a
fresh account read applied to its window copy (Ruling T7-b), so a month without searches still plans the
fallback as soon as a read shows the renewal.
"""

from types import MappingProxyType
from typing import Final, Literal

CellOutcome = Literal[
    "served",
    "paced",
    "reserve_held",
    "exhausted",
    "window_cap",
    "awaiting_after_failed_probe",
    "awaiting_probe_in_flight",
    "ambiguous",
    "transient",
    "breaker_open",
    "auth_failed",
    "contract_broken",
    "shutdown",
    "run_cancelled",
]

# §6.6, row by row.
_ELIGIBLE: Final = MappingProxyType(
    {
        "served": False,  # the cell was served
        "paced": False,  # local pacing (consensus D4, R11)
        "reserve_held": False,  # local reserve policy, not provider exhaustion
        # used + 1 > limit, header remaining 0, a 429 with remaining 0 or absent, or exhausted_until_reset
        "exhausted": True,
        "window_cap": False,  # a local hard cap
        "awaiting_after_failed_probe": True,  # treated as exhausted until the next probe
        "awaiting_probe_in_flight": False,  # the window is validated shortly
        "ambiguous": False,  # it may have been served; the gap is `ambiguous_attempt`
        "transient": False,  # a 429 with remaining > 0, a 5xx or a gateway 404 that did not open the breaker
        "breaker_open": True,  # circuit-open, in this run or already
        "auth_failed": True,
        "contract_broken": True,
        "shutdown": False,  # the run is ending
        "run_cancelled": False,
    }
)


def eligible(outcome: CellOutcome) -> bool:
    """Whether a cell whose JSearch outcome in this run was `outcome` may be served by SerpApi (§6.6)."""
    return _ELIGIBLE[outcome]
