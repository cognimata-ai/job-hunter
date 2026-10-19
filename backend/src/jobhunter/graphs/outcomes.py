"""The reducers of graph state (spec §9.1, §17.2 I7): outcomes keyed by work id, and summed counters.

`merge_outcomes` merges two dicts of outcomes keyed by `work_id`. The same `work_id` twice with an equal
outcome is idempotent (a replayed node, a resumed attempt); a differing outcome for one `work_id` raises
`OutcomeConflict`, which fails the run `failed_invariant` (Task 23). The merge is commutative wherever it does
not raise. `sum_counters` adds counters by name. Neither modifies its arguments.
"""

from typing import TYPE_CHECKING

from jobhunter.errors import DomainError

if TYPE_CHECKING:
    # `state` names these reducers in its annotations: the type only, so the import is not circular.
    from jobhunter.graphs.collect.state import FetchOutcome


class OutcomeConflict(DomainError):
    """Two different outcomes for one work id (§9.1): an invariant violation (I7), never a merge."""

    def __init__(self, work_id: str) -> None:
        super().__init__(f"conflicting outcomes for {work_id}")
        self.work_id = work_id


def merge_outcomes(a: "dict[str, FetchOutcome]", b: "dict[str, FetchOutcome]") -> "dict[str, FetchOutcome]":
    """`a` and `b` together; an equal outcome for a work id in both is kept once, a different one raises."""
    merged = dict(a)
    for work_id, outcome in b.items():
        known = merged.get(work_id)
        if known is not None and known != outcome:
            raise OutcomeConflict(work_id)
        merged[work_id] = outcome
    return merged


def sum_counters(a: dict[str, int], b: dict[str, int]) -> dict[str, int]:
    """The counters of `a` and `b`, added by name."""
    total = dict(a)
    for name, value in b.items():
        total[name] = total.get(name, 0) + value
    return total
