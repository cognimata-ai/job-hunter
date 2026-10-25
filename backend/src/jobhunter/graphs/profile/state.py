"""The state of the profile graphs (spec §9.1, §9.5; M2 plan Task 11): ids, steps and a small enum only.

The checkpoints hold no PII: no CV text, profile JSON or LLM output is ever part of the state. The validated
LLM output between the extract phase's nodes lives in `llm_calls.output_json`, and the state holds only its
id (`llm_call_id`, Task 13). `steps` are the nodes that ran, in order (added up across the thread, so a
thread of its own per execution, `graph.thread_id`, keeps another execution's steps out). `outcome` is set by
the node that ended the draft's phase: `text_review` or `ready`, `failed`, `obsolete`, or `requeued` (the
operation was given back for a later attempt, the draft left as it is).

A node returns a `ProfileUpdate`, the partial update LangGraph merges into the state.

The `propose` graph (Task 20) runs on a query proposal, not a draft: `ProposalState` holds its
`proposal_id` where `ProfileState` holds the draft's, and the same ids, steps and outcome otherwise (`ready`,
`failed`, `obsolete` or `requeued`). The validated proposal is in `query_proposals.items` and its call in
`llm_calls.output_json`, never in the state.
"""

import operator
from typing import Annotated, Final, TypedDict

TEXT_REVIEW: Final = "text_review"
READY: Final = "ready"
FAILED: Final = "failed"
OBSOLETE: Final = "obsolete"
REQUEUED: Final = "requeued"


class ProfileState(TypedDict):
    draft_id: int
    operation_id: int
    llm_call_id: int | None
    steps: Annotated[list[str], operator.add]
    outcome: str | None


class ProfileUpdate(TypedDict, total=False):
    draft_id: int
    operation_id: int
    llm_call_id: int | None
    steps: Annotated[list[str], operator.add]
    outcome: str | None


def initial_state(draft_id: int, operation_id: int) -> ProfileState:
    """The state an execution of operation `operation_id` on draft `draft_id` starts its thread from."""
    return {
        "draft_id": draft_id,
        "operation_id": operation_id,
        "llm_call_id": None,
        "steps": [],
        "outcome": None,
    }


class ProposalState(TypedDict):
    proposal_id: int
    operation_id: int
    llm_call_id: int | None
    steps: Annotated[list[str], operator.add]
    outcome: str | None


class ProposalUpdate(TypedDict, total=False):
    proposal_id: int
    operation_id: int
    llm_call_id: int | None
    steps: Annotated[list[str], operator.add]
    outcome: str | None


def initial_proposal_state(proposal_id: int, operation_id: int) -> ProposalState:
    """The state an execution of operation `operation_id` on proposal `proposal_id` starts its thread from."""
    return {
        "proposal_id": proposal_id,
        "operation_id": operation_id,
        "llm_call_id": None,
        "steps": [],
        "outcome": None,
    }
