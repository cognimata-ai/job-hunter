"""The inputs of one score attempt (spec §9.4.1; plan Tasks 9 and 17, Ruling T17-c): what the `score_target`
subgraph's nodes (`graphs.score.nodes.load_inputs`) and a batch item's decision (`scoring.batch_items`) read
of an attempt named by its ids, without a lock.

`load_attempt_inputs(conn, target_id=…, attempt_id=…, prompt_schema_version_id=…)` returns the attempt's
`scoring.steps.ScoreInputs` and its `AttemptRow`: the prompt-schema version (the renderer's cache serves it),
the tuple's rubric and profile version, the revision's `<job_ad>` block (the material projection
`job_revisions.llm_input` and the revision's description, which every evidence offset refers to), the job's
committed `DetFacts`, the attempt's pinned evaluation policy and execution config, and the model policy's
critic efforts. The ids every prompt records are the revision's, the profile version's and the rubric
version's. No draft, ad or profile text reaches a log line.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, get_args

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q
from jobhunter.llm.pricing import Effort
from jobhunter.llm.prompts.render import load_prompt_version
from jobhunter.llm.schemas.profile import ConfirmedProfile
from jobhunter.scoring import evaluation_policy
from jobhunter.scoring.filters import read_det_facts
from jobhunter.scoring.steps import ScoreInputs, material_strings, revision_ad, rubric_of, score_refs
from jobhunter.scoring.triggers import sample_key
from jobhunter.types import JSONValue, get_str

_EFFORTS: Final[Mapping[str, Effort]] = {effort: effort for effort in get_args(Effort)}
_STRUCTURED: Final = "structured"


@dataclass(frozen=True)
class AttemptRow:
    """What a node reads of its target, tuple and attempt (ids only)."""

    job_id: int
    job_revision_id: int
    llm_tuple_id: int
    profile_version_id: int
    rubric_version_id: int
    model_policy_id: int
    execution_config_id: int
    evaluation_policy_id: int


_INPUTS: Final = q(
    "SELECT t.job_id, t.job_revision_id, t.llm_tuple_id, u.profile_version_id, u.rubric_version_id, "
    "u.model_policy_id, a.execution_config_id, a.evaluation_policy_id, "
    "r.llm_input, r.description_text, r.description_completeness, v.profile_json, w.weights, w.anchors, "
    "m.payload AS model_policy "
    "FROM score_targets t JOIN llm_tuples u ON u.id = t.llm_tuple_id "
    "JOIN score_attempts a ON a.id = :attempt AND a.target_id = t.id "
    "JOIN job_revisions r ON r.id = t.job_revision_id "
    "JOIN profile_versions v ON v.id = u.profile_version_id "
    "JOIN rubric_versions w ON w.id = u.rubric_version_id "
    "JOIN model_policies m ON m.id = u.model_policy_id WHERE t.id = :target"
)


def _effort(value: str) -> Effort:
    effort = _EFFORTS.get(value)
    if effort is None:
        raise ValueError("a model policy names an unknown effort")
    return effort


async def load_attempt_inputs(
    conn: AsyncConnection, *, target_id: int, attempt_id: int, prompt_schema_version_id: int
) -> tuple[ScoreInputs, AttemptRow]:
    """The inputs and ids of attempt `attempt_id` of target `target_id` (module docstring), on the caller's
    connection; `LookupError` when the target, its attempt or the profile version's profile is gone."""
    row = (await conn.execute(_INPUTS, {"target": target_id, "attempt": attempt_id})).first()
    if row is None:
        raise LookupError(f"score target {target_id} or its attempt {attempt_id} does not exist")
    if row.profile_json is None:
        raise LookupError(f"the profile version of score target {target_id} is purged")
    attempt = AttemptRow(
        job_id=int(row.job_id),
        job_revision_id=int(row.job_revision_id),
        llm_tuple_id=int(row.llm_tuple_id),
        profile_version_id=int(row.profile_version_id),
        rubric_version_id=int(row.rubric_version_id),
        model_policy_id=int(row.model_policy_id),
        execution_config_id=int(row.execution_config_id),
        evaluation_policy_id=int(row.evaluation_policy_id),
    )
    material: JSONValue = row.llm_input
    work_modes = material_strings(material, "work_mode_possible")
    work_mode_source = get_str(material, "work_mode_source")
    ad = revision_ad(
        material, completeness=row.description_completeness, description_text=row.description_text
    )
    inputs = ScoreInputs(
        version=await load_prompt_version(conn, prompt_schema_version_id),
        rubric=rubric_of(row.weights, row.anchors),
        profile=ConfirmedProfile.model_validate(row.profile_json),
        ad=ad,
        det=await read_det_facts(conn, attempt.job_id),
        structured_work_modes=tuple(work_modes) if work_mode_source == _STRUCTURED else (),
        completeness=row.description_completeness,
        policy=await evaluation_policy.load(conn, attempt.evaluation_policy_id),
        critic_effort=_effort(get_str(row.model_policy, "nodes.critic.effort")),
        critic_light_effort=_effort(get_str(row.model_policy, "critic_light_effort")),
        sample_key=sample_key(target_id=target_id, llm_tuple_id=attempt.llm_tuple_id),
        refs=score_refs(
            job_revision_id=attempt.job_revision_id,
            profile_version_id=attempt.profile_version_id,
            rubric_version_id=attempt.rubric_version_id,
        ),
    )
    return inputs, attempt
