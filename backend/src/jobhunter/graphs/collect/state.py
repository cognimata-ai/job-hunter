"""The state of the collection graph (spec §9.2): the one definition of `CollectState` and its update.

`CollectState` is the full state of a run attempt's graph: §9.2 exactly, plus `steps` (the nodes that ran,
in order), `node_durations` (seconds per node, for the run report) and `final_status` (the status `finalize`
gave the run). It holds ids, counters and small enums only, never content (§9.1), so checkpoints carry no PII.
A node returns a `CollectUpdate`, the partial update LangGraph merges into the state through the reducers of
the annotated keys (`merge_outcomes`, `sum_counters`, `operator.add`).

Every attempt starts from `initial_state` on a thread of its own (`worker.runs.thread_id`), so no attempt
reads another's progress; the ids of the run's settings are `0` until `plan` sets them from the run row.
`FetchCellInput` is what the fan-out `Send`s to `fetch_cell`: one planned fetch request.
"""

import operator
from typing import Annotated, Literal, TypedDict

from jobhunter.graphs.outcomes import merge_outcomes, sum_counters


class FetchOutcome(TypedDict):
    work_id: str  # f"fr:{fetch_request_id}"
    status: Literal["succeeded", "failed", "ambiguous", "skipped"]
    items: int
    new_postings: int


class CollectState(TypedDict):
    run_id: int
    attempt_no: int
    settings_version_id: int
    scope_version_id: int
    llm_tuple_id: int | None
    plan_done: bool
    primary_fetch_request_ids: list[int]
    fallback_fetch_request_ids: list[int]
    outcomes: Annotated[dict[str, FetchOutcome], merge_outcomes]
    counters: Annotated[dict[str, int], sum_counters]  # e.g. raw_items_linked, revisions_new
    steps: Annotated[list[str], operator.add]
    node_durations: Annotated[dict[str, float], sum_counters]
    final_status: str | None


class CollectUpdate(TypedDict, total=False):
    run_id: int
    attempt_no: int
    settings_version_id: int
    scope_version_id: int
    llm_tuple_id: int | None
    plan_done: bool
    primary_fetch_request_ids: list[int]
    fallback_fetch_request_ids: list[int]
    outcomes: Annotated[dict[str, FetchOutcome], merge_outcomes]
    counters: Annotated[dict[str, int], sum_counters]
    steps: Annotated[list[str], operator.add]
    node_durations: Annotated[dict[str, float], sum_counters]
    final_status: str | None


class FetchCellInput(TypedDict):
    fetch_request_id: int


def initial_state(run_id: int, attempt_no: int) -> CollectState:
    """The state attempt `attempt_no` of run `run_id` starts from: nothing planned, nothing counted."""
    return {
        "run_id": run_id,
        "attempt_no": attempt_no,
        "settings_version_id": 0,
        "scope_version_id": 0,
        "llm_tuple_id": None,
        "plan_done": False,
        "primary_fetch_request_ids": [],
        "fallback_fetch_request_ids": [],
        "outcomes": {},
        "counters": {},
        "steps": [],
        "node_durations": {},
        "final_status": None,
    }
