"""The critic's structured output (spec §11.7.3): the structural mirror only.

Its limits and its derived outcome (§11.7.2) are M3's; M2 holds the schema as data for prompt-schema version 1
and the schema dry run (§10.11).
"""

from typing import Annotated, Literal

from jobhunter.llm.schemas._base import Def, SchemaModel
from jobhunter.llm.schemas.generator import Level


class Review(SchemaModel):
    target: str
    assessment: Literal[
        "level_too_high",
        "level_too_low",
        "evidence_ungrounded",
        "should_be_unknown",
        "evidence_overlooked",
        "anchor_misapplied",
        "fact_confirmed",
        "fact_rejected",
        "fact_uncertain",
    ]
    suggested_level: Level
    ad_evidence: list[str]
    explanation: str


class CriticOut(SchemaModel):
    """The critic's output (§11.7.3)."""

    verdict: Literal["accept", "revise"]
    reviews: list[Annotated[Review, Def("review")]]
    injection: Literal["none", "present_no_influence", "present_influenced"]
    summary: str
