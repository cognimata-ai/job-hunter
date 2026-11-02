"""The `score_drafts` statements that the `score_target` subgraph's store
(`graphs.score.nodes.TargetDraftStore`) and a batch item's store (`scoring.batch_items`) both write (plan
Tasks 9 and 17, Ruling T17-c), and their binds: the insert of a generated draft (`validation =
{"normalisation": …}`, codes and keys only, the other NOT NULL columns placeholders until the validation
fills them), the read, and the validation's update (`validation = validation || {"current": …}`,
`corrections` added only while the draft has none, so the first validation's corrections are kept
verbatim).
"""

from typing import Final

from jobhunter.db.sql import q
from jobhunter.scoring.steps import ValidationRecord
from jobhunter.scoring.validator import ParsedGenerator

INSERT_DRAFT: Final = q(
    "INSERT INTO score_drafts (attempt_id, round, llm_call_id, output_json, validation, "
    "deterministic_failures, provisional, triggers) "
    "VALUES (:attempt, :round, :call, :draft, :validation, '{}', '{}', '{}') "
    "RETURNING id",
    jsonb=["draft", "validation"],
)
READ_DRAFT: Final = q("SELECT id, round, output_json, validation FROM score_drafts WHERE id = :d")
SAVE_VALIDATION: Final = q(
    "UPDATE score_drafts SET output_json = :draft, "
    "validation = validation || jsonb_build_object('current', CAST(:current AS jsonb)) || "
    "CASE WHEN validation -> 'corrections' IS NULL "
    "THEN jsonb_build_object('corrections', CAST(:corrections AS jsonb)) ELSE CAST('{}' AS jsonb) END, "
    "deterministic_failures = CAST(:failures AS text[]), provisional = :provisional, "
    "triggers = CAST(:triggers AS text[]) WHERE id = :d",
    jsonb=["draft", "current", "corrections", "provisional"],
)


def draft_params(
    *, attempt_id: int, round: int, llm_call_id: int, parsed: ParsedGenerator
) -> dict[str, object]:
    """The binds of `INSERT_DRAFT` for a structurally valid output of the attempt's `round`."""
    return {
        "attempt": attempt_id,
        "round": round,
        "call": llm_call_id,
        "draft": parsed.draft.model_dump(mode="json"),
        "validation": {"normalisation": parsed.normalisation.to_json()},
    }


def validation_params(draft_id: int, record: ValidationRecord) -> dict[str, object]:
    """The binds of `SAVE_VALIDATION` for `record` of draft `draft_id`."""
    return {
        "d": draft_id,
        "draft": record.draft.model_dump(mode="json"),
        "current": record.current,
        "corrections": record.corrections,
        "failures": record.failures,
        "provisional": record.provisional,
        "triggers": record.triggers,
    }
