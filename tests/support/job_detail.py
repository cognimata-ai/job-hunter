"""Synthetic job-detail data (plan M4 Task 6): the stored documents and rows the drawer reads beyond a card,
for the detail's tests and its p95 test. Invented texts, levels and ids only, no ad content.

- `criteria_doc(levels)` is an `llm_results.criteria` document and `draft_doc(levels)` a `score_drafts`
  `output_json` (the internal draft), each with the six LLM criteria at `levels` (`None`: `unknown`);
- `published_of(c, job_id)` names the rows behind the job's published active-tuple result: the result, its
  target, attempt and final draft;
- `write_chain(c, published, drafts=…, notes=…)` gives that result the review chain of a critic loop on its
  attempt: the existing draft (round 0) rated `drafts[0]`, a revision draft of its own `revise` call for
  each later entry (round 1, 2), the last one the result's final draft, and a critic note for each `Note`
  with its own `critic` call (the model and effort it served) and the validated round M3 stores
  (`note_documents`); `add_attempt`, `add_draft`, `add_note` and `set_final` are its steps, for a chain over
  several attempts;
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

from tests.support.m3 import insert_attempt, insert_draft, insert_id, insert_scoring_call

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
    """A critic note of the chain, stored as M3's `critic_step` stores one (`steps._note_validation`): the
    draft it reviewed (an index into the chain's drafts), the round's triggers, the returned verdict and the
    derived outcome, and what the round's validation made of the critic's reviews:
    - `disputes`: the grounded criterion disputes, `(target, assessment, suggested_level)`;
    - `facts`: the required facts in order, each with the critic's assessment of it (`fact_confirmed`,
      `fact_rejected`, `fact_uncertain`), or `None` for one it did not review (`unreviewed`);
    - `discarded`: reviews dropped for want of a grounded quote, `(target, assessment, suggested_level)`;
    - `invalid`: reviews whose target names no criterion and no required fact, `(target, assessment)`.
    The raw reviews (`review_json`) hold all of them; the model and effort are those its call served."""

    draft: int
    triggers: tuple[str, ...]
    verdict: str
    outcome: str
    disputes: tuple[tuple[str, str, int | None], ...] = ()
    facts: tuple[tuple[str, str | None], ...] = ()
    discarded: tuple[tuple[str, str, int | None], ...] = ()
    invalid: tuple[tuple[str, str], ...] = ()
    model: str = "claude-opus-5-5"
    effort: str = "high"


_DRAFT_OUTPUT: Final = q("UPDATE score_drafts SET output_json = :o WHERE id = :d", jsonb=["o"])
_CALL_SERVED: Final = q("UPDATE llm_calls SET round = :r, model_served = :m, effort = :e WHERE id = :c")
_FINAL: Final = q("UPDATE llm_results SET final_draft_id = :d, revisions_used = :n WHERE id = :r")
# What a round's review of a fact leaves in `fact_reviews` (`validator.validate_review`; `None`: unreviewed)
_FACT_STATUS: Final[Mapping[str | None, str]] = {
    "fact_confirmed": "critic_confirmed",
    "fact_rejected": "critic_rejected",
    "fact_uncertain": "uncertain",
    None: "uncertain",
}


def _review(target: str, assessment: str, suggested: int | None) -> dict[str, JSONValue]:
    return {
        "target": target,
        "assessment": assessment,
        "suggested_level": suggested,
        "ad_evidence": ["Erfundenes Zitat"],
        "explanation": "Erfundene Erklärung.",
    }


def note_documents(note: Note) -> tuple[dict[str, JSONValue], dict[str, JSONValue]]:
    """The note's `review_json` (the critic's raw reviews: the invalid, the discarded, the disputes, the
    reviewed facts) and its `validation` (module docstring, `Note`)."""
    raw: list[tuple[str, str, int | None]] = [
        *((target, assessment, None) for target, assessment in note.invalid),
        *note.discarded,
        *note.disputes,
        *((name, assessment, None) for name, assessment in note.facts if assessment is not None),
    ]
    reviews: list[JSONValue] = [_review(*review) for review in raw]
    discarded_at = len(note.invalid)
    statuses = {name: _FACT_STATUS[assessment] for name, assessment in note.facts}
    unreviewed = [name for name, assessment in note.facts if assessment is None]
    issues: list[JSONValue] = []
    for i in range(len(note.invalid)):
        issues.append({"code": "invalid_target", "path": f"reviews[{i}]", "detail": {}})
    for name in unreviewed:
        issues.append({"code": "review_missing", "path": "reviews", "detail": {"target": name}})
    validation: dict[str, JSONValue] = {
        "complete": not note.invalid and not unreviewed,
        "verdict_mismatch": False,
        "not_a_job_ad_verified": False,
        "required": [name for name, _ in note.facts],
        "fact_reviews": dict(statuses),
        "fact_confirmations": dict(statuses),
        "unreviewed": list(unreviewed),
        "disputes": [
            {
                "target": target,
                "assessment": assessment,
                "suggested_level": suggested,
                "quotes": ["Erfundenes Zitat"],
                "groundings": [],
            }
            for target, assessment, suggested in note.disputes
        ],
        "discarded": [
            {
                "code": "review_ungrounded",
                "path": f"reviews[{discarded_at + i}]",
                "detail": {"target": target},
            }
            for i, (target, _, _) in enumerate(note.discarded)
        ],
        "issues": issues,
    }
    review: dict[str, JSONValue] = {
        "verdict": note.verdict,
        "reviews": reviews,
        "injection": "none",
        "summary": "Erfunden.",
    }
    return review, validation


async def _call(
    c: AsyncConnection, p: Published, attempt_id: int, *, purpose: str, round_: int, model: str, effort: str
) -> int:
    _, call = await insert_scoring_call(c, attempt_id=attempt_id, target_id=p.target_id, purpose=purpose)
    await c.execute(_CALL_SERVED, {"r": round_, "m": model, "e": effort, "c": call})
    return call


_ATTEMPT_NO: Final = q("SELECT coalesce(max(attempt_no), 0) + 1 FROM score_attempts WHERE target_id = :t")


async def add_attempt(
    c: AsyncConnection, p: Published, *, resumed_from: int | None, **columns: object
) -> int:
    """A later ended attempt of `p`'s target (`published_accepted`, unless `columns` say otherwise), resumed
    from `resumed_from`; its id."""
    attempt_no = int((await c.execute(_ATTEMPT_NO, {"t": p.target_id})).scalar_one())
    values: dict[str, object] = {"outcome": "published_accepted", "lane": "similarity", **columns}
    return await insert_attempt(
        c, p.target_id, attempt_no=attempt_no, resumed_from_attempt_id=resumed_from, **values
    )


async def add_draft(
    c: AsyncConnection,
    p: Published,
    attempt_id: int,
    round_: int,
    levels: Mapping[str, int | None],
    *,
    generator: tuple[str, str] = ("claude-sonnet-5-5", "medium"),
) -> int:
    """A draft of `round_` on the attempt, rated `levels`, with its own call (`generate` in round 0, else
    `revise`) serving `generator` (model, effort); its id."""
    model, effort = generator
    purpose = "generate" if round_ == 0 else "revise"
    call = await _call(c, p, attempt_id, purpose=purpose, round_=round_, model=model, effort=effort)
    draft = await insert_draft(c, attempt_id, call, round_)
    await c.execute(_DRAFT_OUTPUT, {"o": draft_doc(levels), "d": draft})
    return draft


async def add_note(
    c: AsyncConnection, p: Published, attempt_id: int, round_: int, draft_id: int, note: Note
) -> int:
    """The critic note of `round_` on the attempt, reviewing `draft_id`, with its own `critic` call; its
    id."""
    call = await _call(
        c, p, attempt_id, purpose="critic", round_=round_, model=note.model, effort=note.effort
    )
    review, validation = note_documents(note)
    return await insert_id(
        c,
        "critic_notes",
        {
            "attempt_id": attempt_id,
            "round": round_,
            "draft_id": draft_id,
            "llm_call_id": call,
            "effort": note.effort,
            "triggers": list(note.triggers),
            "verdict_returned": note.verdict,
            "derived_outcome": note.outcome,
            "review_json": review,
            "validation": validation,
        },
    )


async def set_final(c: AsyncConnection, p: Published, draft_id: int, revisions_used: int) -> None:
    await c.execute(_FINAL, {"d": draft_id, "n": revisions_used, "r": p.result_id})


async def write_chain(
    c: AsyncConnection,
    p: Published,
    *,
    drafts: Sequence[Mapping[str, int | None]],
    notes: Sequence[Note] = (),
    generator: tuple[str, str] = ("claude-sonnet-5-5", "medium"),
) -> list[int]:
    """The review chain of `p`'s result on its attempt (module docstring); the chain's draft ids, round 0
    first. The generator's calls serve `generator` (model, effort)."""
    model, effort = generator
    first_call = await _draft_call(c, p)
    await c.execute(_CALL_SERVED, {"r": 0, "m": model, "e": effort, "c": first_call})
    ids = [p.final_draft_id]
    await c.execute(_DRAFT_OUTPUT, {"o": draft_doc(drafts[0]), "d": p.final_draft_id})
    for round_, levels in enumerate(drafts[1:], start=1):
        ids.append(await add_draft(c, p, p.attempt_id, round_, levels, generator=generator))
    await set_final(c, p, ids[-1], len(ids) - 1)
    for round_, note in enumerate(notes, start=1):
        await add_note(c, p, p.attempt_id, round_, ids[note.draft], note)
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
