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

`cell_outcome` reads a JSearch cell's outcome in a run from its durable receipts (Task 13, Ruling T13-a): a
cell with a `succeeded` request is `served`; otherwise its last fetch request of the run decides. A skip is
its reason, except `awaiting_window_validation`, which is `awaiting_after_failed_probe` while the current
JSearch window has `probe_failed` and `awaiting_probe_in_flight` otherwise; `superseded` (re-planned, §7.16.5)
reads as `shutdown` and `not_needed` as `served`, neither eligible. A failure is the instance's health when
that is `breaker_open`, `auth_failed` or `contract_broken` (a breaker that opened in this run included), then
`exhausted` for a `rate_limited` failure while the window is `exhausted_until_reset` (a 429 with `remaining`
0 or absent, §6.6), and `transient` otherwise; `ambiguous` is `ambiguous`.
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, Literal

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q
from jobhunter.errors import InvariantViolation
from jobhunter.quota.windows import current_snapshot

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


# The outcome of a cell whose last request was skipped, by skip reason (`awaiting_window_validation` reads
# the window, below).
_SKIP_OUTCOMES: Final[Mapping[str, CellOutcome]] = MappingProxyType(
    {
        "paced": "paced",
        "reserve_held": "reserve_held",
        "exhausted": "exhausted",
        "window_cap": "window_cap",
        "breaker_open": "breaker_open",
        "shutdown": "shutdown",
        "run_cancelled": "run_cancelled",
        "superseded": "shutdown",  # re-planned as a new row (§7.16.5): the attempt that skipped it was ending
        "not_needed": "served",  # the cell needed no further request
    }
)
# The instance states a failed request reads as: circuit-open, auth-failed, contract-broken (§6.6).
_HEALTH_OUTCOMES: Final[Mapping[str, CellOutcome]] = MappingProxyType(
    {"breaker_open": "breaker_open", "auth_failed": "auth_failed", "contract_broken": "contract_broken"}
)
_REQUESTS = q(
    "SELECT f.id, f.status, f.skip_reason, f.error_code, i.health FROM fetch_requests f "
    "JOIN source_instances i ON i.id = f.source_instance_id WHERE f.run_id = :run AND f.cell_id = :cell "
    "ORDER BY f.id"
)


async def cell_outcome(conn: AsyncConnection, run_id: int, cell_id: int) -> CellOutcome:
    """The JSearch outcome of cell `cell_id` in run `run_id`, from its fetch requests (module docstring).

    Raises `LookupError` for a cell without a fetch request in the run and `InvariantViolation` when its last
    one is not terminal: `plan_fallback` runs after every primary JSearch spec is terminal (§9.2 node 3).
    """
    rows = (await conn.execute(_REQUESTS, {"run": run_id, "cell": cell_id})).all()
    if not rows:
        raise LookupError(f"cell {cell_id} has no fetch request in run {run_id}")
    if any(row.status == "succeeded" for row in rows):
        return "served"
    last = rows[-1]
    match last.status:
        case "ambiguous":
            return "ambiguous"
        case "skipped" if last.skip_reason == "awaiting_window_validation":
            window = await current_snapshot(conn, "jsearch")
            failed = window is not None and window.probe_failed
            return "awaiting_after_failed_probe" if failed else "awaiting_probe_in_flight"
        case "skipped":
            return _SKIP_OUTCOMES[last.skip_reason]
        case "failed":
            if (health := _HEALTH_OUTCOMES.get(last.health)) is not None:
                return health
            if last.error_code == "rate_limited":
                window = await current_snapshot(conn, "jsearch")
                if window is not None and window.state == "exhausted_until_reset":
                    return "exhausted"
            return "transient"
    raise InvariantViolation(f"fetch request {last.id} of cell {cell_id} is {last.status}, not terminal")
