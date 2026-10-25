"""The query proposal's structured output (spec §11.12): the structural mirror only.

The semantic limits (6 to 16 queries, text length, no location words, at least one exploration item, rationale
length, unique folded texts) are applied after the parse (Task 20, plan Deviation 20).
"""

from typing import Literal

from jobhunter.llm.schemas._base import SchemaModel


class QueryProposal(SchemaModel):
    text: str
    lang: Literal["de", "en"]
    target_title: str
    exploration: bool
    rationale: str


class QueryProposalOut(SchemaModel):
    """The `propose_queries` output (§11.12)."""

    queries: list[QueryProposal]
