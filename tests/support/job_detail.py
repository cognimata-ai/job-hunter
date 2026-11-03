"""Synthetic job-detail data (plan M4 Task 6): the stored documents and rows the drawer reads beyond a card,
for the detail's tests and its p95 test. Invented texts, levels and ids only, no ad content.

- `criteria_doc(levels)` is an `llm_results.criteria` document and `draft_doc(levels)` a `score_drafts`
  `output_json` (the internal draft), each with the six LLM criteria at `levels` (`None`: `unknown`);
- `published_of(c, job_id)` names the rows behind the job's published active-tuple result: the result, its
  target, attempt and final draft;
- `write_chain(c, published, drafts=…, notes=…)` gives that result the review chain of a critic loop: the
  existing draft (round 0) rated `drafts[0]`, a revision draft of its own `revise` call for each later
  entry (round 1, 2), the last one the result's final draft, and a critic note for each `Note` with its own
  `critic` call (the model and effort it served);
- `add_evidence(c, result_id, rows)` inserts `result_evidence` rows;
- `set_weights(c, llm_tuple_id)` gives the tuple's test rubric the v1 weights (a test chain's rubric has
  none).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from jobhunter.db.sql import q
from jobhunter.scoring.rubric import LLM_CRITERIA, RUBRIC_V1, load_rubric
from jobhunter.types import JSONValue
from sqlalchemy.ext.asyncio import AsyncConnection

from tests.support.m3 import insert_draft, insert_id, insert_scoring_call

LEVELS: Final[Mapping[str, int | None]] = {
    "role_fit": 4,
    "skills": 3,
    "experience_seniority": 3,
    "domain": None,
    "languages": 4,
    "education_certifications": 4,
}
_NOTHING_ASSERTED: Final[dict[str, JSONValue]] = {
    "work_mode": {"value": "unspecified", "evidence": []},
    "onsite_only": {"asserted": False, "evidence": []},
    "remote_region": {"value": "unspecified", "countries": [], "evidence": []},
    "required_languages": [],
    "must_haves": [],
    "nice_to_haves": [],
    "seniority": {"value": "unspecified", "evidence": []},
    "min_years_experience": {"value": None, "evidence": []},
    "industry": {"value": None, "evidence": []},
    "employment_types": {"values": [], "evidence": []},
    "degree": {
        "level": "unspecified",
        "mandatory": False,
        "or_equivalent": False,
        "field": None,
        "evidence": [],
    },
    "certifications": [],
    "security_clearance": {"required": False, "level": None, "evidence": []},
    "work_permit": {"required": False, "evidence": []},
}


def _criterion(key: str, level: int | None) -> dict[str, JSONValue]:
    return {
        "level": level,
        "basis": "unknown" if level is None else "observed",
        "ad_evidence": [],
        "profile_evidence": [],
        "rationale": f"Erfundene Begründung {key}.",
    }


def criteria_doc(levels: Mapping[str, int | None] = LEVELS) -> dict[str, JSONValue]:
    """An `llm_results.criteria` document: each LLM criterion at its level of `levels` (module docstring)."""
    return {key: _criterion(key, {**LEVELS, **levels}[key]) for key in LLM_CRITERIA}


def draft_doc(levels: Mapping[str, int | None] = LEVELS) -> dict[str, JSONValue]:
    """A `score_drafts.output_json`: an internal draft asserting no fact, its criteria at `levels`."""
    return {
        "not_a_job_ad": False,
        "injection_observed": False,
        "job_facts": dict(_NOTHING_ASSERTED),
        "criteria": criteria_doc(levels),
        "summary_en": "Invented summary.",
        "summary_de": "Erfundene Zusammenfassung.",
    }


@dataclass(frozen=True)
class Published:
    """The rows behind a published result: the result, its target, its attempt and its final draft."""

    result_id: int
    target_id: int
    attempt_id: int
    final_draft_id: int


_PUBLISHED: Final = q(
    "SELECT r.id, r.target_id, r.attempt_id, r.final_draft_id FROM jobs j "
    "JOIN active_config a ON a.id = 1 "
    "JOIN score_targets t ON t.job_revision_id = j.current_revision_id AND t.llm_tuple_id = a.llm_tuple_id "
    "JOIN llm_results r ON r.id = t.published_result_id WHERE j.id = :j"
)


async def published_of(c: AsyncConnection, job_id: int) -> Published:
    """The published active-tuple result of the job's current revision (module docstring)."""
    row = (await c.execute(_PUBLISHED, {"j": job_id})).one()
    return Published(int(row.id), int(row.target_id), int(row.attempt_id), int(row.final_draft_id))


@dataclass(frozen=True)
class Note:
    """A critic note of the chain: the draft it reviewed (an index into the chain's drafts), the round's
    triggers, the returned verdict and the derived outcome, its reviews as `(target, assessment,
    suggested_level)`, and the model and effort its call served."""

    draft: int
    triggers: tuple[str, ...]
    verdict: str
    outcome: str
    reviews: tuple[tuple[str, str, int | None], ...]
    model: str = "claude-opus-5-5"
    effort: str = "high"


_DRAFT_OUTPUT: Final = q("UPDATE score_drafts SET output_json = :o WHERE id = :d", jsonb=["o"])
_CALL_SERVED: Final = q("UPDATE llm_calls SET round = :r, model_served = :m, effort = :e WHERE id = :c")
_FINAL: Final = q("UPDATE llm_results SET final_draft_id = :d, revisions_used = :n WHERE id = :r")


def _review_doc(note: Note) -> dict[str, JSONValue]:
    reviews: list[JSONValue] = [
        {
            "target": target,
            "assessment": assessment,
            "suggested_level": suggested,
            "ad_evidence": ["Erfundenes Zitat"],
            "explanation": "Erfundene Erklärung.",
        }
        for target, assessment, suggested in note.reviews
    ]
    return {"verdict": note.verdict, "reviews": reviews, "injection": "none", "summary": "Erfunden."}


async def _call(
    c: AsyncConnection, p: Published, *, purpose: str, round_: int, model: str, effort: str
) -> int:
    _, call = await insert_scoring_call(c, attempt_id=p.attempt_id, target_id=p.target_id, purpose=purpose)
    await c.execute(_CALL_SERVED, {"r": round_, "m": model, "e": effort, "c": call})
    return call


async def write_chain(
    c: AsyncConnection,
    p: Published,
    *,
    drafts: Sequence[Mapping[str, int | None]],
    notes: Sequence[Note] = (),
    generator: tuple[str, str] = ("claude-sonnet-5-5", "medium"),
) -> list[int]:
    """The review chain of `p`'s result (module docstring); the chain's draft ids, round 0 first. The
    generator's calls serve `generator` (model, effort)."""
    model, effort = generator
    first_call = await _draft_call(c, p)
    await c.execute(_CALL_SERVED, {"r": 0, "m": model, "e": effort, "c": first_call})
    ids = [p.final_draft_id]
    await c.execute(_DRAFT_OUTPUT, {"o": draft_doc(drafts[0]), "d": p.final_draft_id})
    for round_, levels in enumerate(drafts[1:], start=1):
        call = await _call(c, p, purpose="revise", round_=round_, model=model, effort=effort)
        draft = await insert_draft(c, p.attempt_id, call, round_)
        await c.execute(_DRAFT_OUTPUT, {"o": draft_doc(levels), "d": draft})
        ids.append(draft)
    await c.execute(_FINAL, {"d": ids[-1], "n": len(ids) - 1, "r": p.result_id})
    for round_, note in enumerate(notes, start=1):
        call = await _call(c, p, purpose="critic", round_=round_, model=note.model, effort=note.effort)
        await insert_id(
            c,
            "critic_notes",
            {
                "attempt_id": p.attempt_id,
                "round": round_,
                "draft_id": ids[note.draft],
                "llm_call_id": call,
                "effort": note.effort,
                "triggers": list(note.triggers),
                "verdict_returned": note.verdict,
                "derived_outcome": note.outcome,
                "review_json": _review_doc(note),
                "validation": {"fact_confirmations": {}, "fact_reviews": {}},
            },
        )
    return ids


_DRAFT_CALL: Final = q("SELECT llm_call_id FROM score_drafts WHERE id = :d")


async def _draft_call(c: AsyncConnection, p: Published) -> int:
    return int((await c.execute(_DRAFT_CALL, {"d": p.final_draft_id})).scalar_one())


async def add_evidence(c: AsyncConnection, result_id: int, rows: Sequence[Mapping[str, Any]]) -> list[int]:
    """`result_evidence` rows of the result, in order (each `subject`, `source_kind`, `quote` and either a
    span or a `field_name`); their ids."""
    ids = []
    for row in rows:
        values = {"llm_result_id": result_id, "field_name": None, "span_start": None, "span_end": None, **row}
        ids.append(await insert_id(c, "result_evidence", values))
    return ids


_WEIGHTS: Final = q(
    "UPDATE rubric_versions SET weights = :w "
    "WHERE id = (SELECT rubric_version_id FROM llm_tuples WHERE id = :t)",
    jsonb=["w"],
)


async def set_weights(c: AsyncConnection, llm_tuple_id: int) -> dict[str, int]:
    """The v1 rubric's weights on the tuple's rubric (module docstring); the weights."""
    weights = dict(load_rubric(RUBRIC_V1).weights)
    await c.execute(_WEIGHTS, {"w": weights, "t": llm_tuple_id})
    return weights
