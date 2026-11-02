"""The publish transaction (spec §9.4.1 L3531-3541, §11.10, §20.2; plan Task 10, Codex r1 #18, r2 B7, B18, r3
B17, r4 A, A2 S4, S6, Deviation 18): `publish_result`, the persistence of a publication, shared by the online
`publish` node and the batch `published` decision of Task 17 only. The eval runner of Task 19 calls
`publication.build` alone and never this transaction, so no eval touches a target, a result or the ranking.

**The caller's steps** (1 and 1a, `lock_config_and_job`): every caller has taken `active_config FOR SHARE` and
the job row `FOR SHARE` before its owner row; `publish_result` re-reads both under the locks it holds.

**The transaction**, in the lock schedule's order (§3.3.3, Deviation 38: `active_config` → job → target → the
bulk row → `llm_results` with its `det_results` → the other rows → `ranking_state`), in the caller's
transaction:
1. `active_config`: its `scope_version_id` and `pending_scope_version_id`, the scopes read in a statement of
   their own (`settings.active`: a joined row is read in the snapshot the locking statement started with);
1a. the job, and `is_fenced(job_id, llm_tuple_id)`: a fenced job publishes nothing, the attempt ends
   `cancelled('purge_fenced')` and the target is left for the purge's chunk (L3533; A2 S4) → `fenced`;
2. the target `FOR UPDATE` under the predicate: the claim's (`ClaimPredicate`: `in_progress` under `claim_id`
   and `attempt_no`) or the batch's (`BatchPredicate`: `in_batch` under `batch_submission_id` and
   `attempt_no`); no row → `claim_lost`, nothing written (the draft stays);
2a. `bulk.release_target_hold`: the target's unreleased follow-up hold, under its bulk row (step 7, before any
   domain insert; Codex r2 B7): every end of this transaction that writes is terminal;
3. the publication object, `publication.build` over the final draft's stored validation (re-run purely over
   its stored draft, normalisation and history, with the row's exact failures: carry T9-a) and the last valid
   critic review (`last_critic_note_id`: the note's review validated again against the draft it reviewed,
   with the chain's confirmations it stored), under the active scope, with the job's `DetFacts` read under the
   job lock. A `PublicationInvalid` is a `Bug`: the target and the attempt end
   `failed_invalid_output('publication_invalid')`, the critical `scoring.publication_invalid` is raised
   (subject: the target id; context: the error types and field paths only), the ranking is bumped →
   `publication_invalid`;
4. `INSERT llm_results` (the publication's columns, `critic_injection` beside them, the five version ids of
   the tuple and the attempt's evaluation policy, `published_at` from the caller's clock) and its
   `result_evidence` rows (its `target_id` key is deferred);
5. `det_results.compute_and_write` for the active scope and, when `pending_scope_version_id` is set, for the
   pending one (the result row's lock is the instances' lock), with the job's sources
   (`det_results.job_sources`);
6. the target → `published_<mode>` under the predicate, `published_result_id` set (the claim or batch columns
   cleared by `clear_on_exit`, `claimed_at` kept);
7. the duplicate group (`groups.representative_published`: deferred members stay deferred), the attempt
   `running → published_<mode>`, and `ranking.bump` last. The fault point `score.publish.before_commit`
   (`BEFORE_COMMIT`) is the transaction's last statement; `score.publish.after_commit` (`AFTER_COMMIT`) is the
   caller's, after its commit. The deferred foreign keys check at commit (L3541).

**An already-published 5-tuple** (`link_result`, L3523: defensive, Task 9's `load_context` finds it): the
same steps 1-2a, then the stored result is linked (the target `published_<its status>` naming it, the attempt
ended so, the ranking bumped) and nothing is inserted: no second `llm_results` row, no instance.

Nothing here logs or alerts with content: ids, counts, modes and codes only.
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import ClassVar, Final, Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db import ranking
from jobhunter.db.sql import q
from jobhunter.db.transitions import transition
from jobhunter.dedup import groups
from jobhunter.errors import InvariantViolation, TransitionConflict
from jobhunter.faults import afault_point
from jobhunter.llm import bulk
from jobhunter.observability.alerts import raise_alert
from jobhunter.observability.logging import get_logger
from jobhunter.run_settings import scope_from_payload
from jobhunter.scoring import publication
from jobhunter.scoring.det_results import JobRow, ScopeRow, compute_and_write, job_sources
from jobhunter.scoring.filters import read_det_facts
from jobhunter.scoring.publication import Publication, PublicationInvalid, PublishMode, Review
from jobhunter.scoring.steps import ScoreInputs, StoredDraft, StoredNote, stored_review, stored_validation
from jobhunter.scoring.validator import ValidationContext
from jobhunter.types import JSONValue, get_object

BEFORE_COMMIT: Final = "score.publish.before_commit"
AFTER_COMMIT: Final = "score.publish.after_commit"
PUBLICATION_INVALID_ALERT: Final = "scoring.publication_invalid"
PUBLICATION_INVALID: Final = "publication_invalid"
PURGE_FENCED: Final = "purge_fenced"

Reason = Literal["published", "claim_lost", "fenced", "publication_invalid"]

_LOCK_CONFIG: Final = q(
    "SELECT scope_version_id, pending_scope_version_id FROM active_config WHERE id = 1 FOR SHARE"
)
_LOCK_JOB: Final = q("SELECT is_fenced(j.id, :tuple) AS fenced FROM jobs j WHERE j.id = :job FOR SHARE OF j")
_LOCK_JOB_ROW: Final = q("SELECT id FROM jobs WHERE id = :j FOR SHARE")
_SCOPES: Final = q("SELECT id, payload FROM scope_versions WHERE id = ANY(CAST(:ids AS bigint[]))")
_LOCK_CLAIMED: Final = q(
    "SELECT id FROM score_targets WHERE id = :t AND claim_id = :c AND attempt_no = :a "
    "AND state = 'in_progress' FOR UPDATE"
)
_LOCK_BATCHED: Final = q(
    "SELECT id FROM score_targets WHERE id = :t AND batch_submission_id = :s AND attempt_no = :a "
    "AND state = 'in_batch' FOR UPDATE"
)
_IDS: Final = q(
    "SELECT u.profile_version_id, u.rubric_version_id, u.prompt_schema_version_id, u.model_policy_id, "
    "a.evaluation_policy_id FROM llm_tuples u JOIN score_attempts a ON a.id = :a AND a.target_id = :t "
    "WHERE u.id = :tuple"
)
_DRAFT: Final = q(
    "SELECT d.id, d.round, d.output_json, d.validation FROM score_drafts d "
    "JOIN score_attempts a ON a.id = d.attempt_id WHERE d.id = :d AND a.target_id = :t"
)
_NOTE: Final = q(
    "SELECT n.id, n.draft_id, n.derived_outcome, n.review_json, n.validation FROM critic_notes n "
    "JOIN score_attempts a ON a.id = n.attempt_id WHERE n.id = :n AND a.target_id = :t"
)
_INSERT_RESULT: Final = q(
    "INSERT INTO llm_results (target_id, attempt_id, final_draft_id, job_revision_id, profile_version_id, "
    "rubric_version_id, prompt_schema_version_id, model_policy_id, evaluation_policy_id, status, criteria, "
    "job_facts, knockout_candidates, fact_confirmations, not_a_job_ad, injection_suspected, nulled_criteria, "
    "truncated_fields, critic_outcome, critic_injection, revisions_used, summary_en, summary_de, "
    "published_at) VALUES (:target, :attempt, :draft, :revision, :profile, :rubric, :prompt, :model_policy, "
    ":evaluation_policy, :status, :criteria, :job_facts, :knockouts, :confirmations, :not_a_job_ad, "
    ":injection_suspected, CAST(:nulled AS text[]), CAST(:truncated AS text[]), :critic_outcome, "
    ":critic_injection, :revisions_used, :summary_en, :summary_de, :now) RETURNING id",
    jsonb=["criteria", "job_facts", "knockouts", "confirmations"],
)
_INSERT_EVIDENCE: Final = q(
    "INSERT INTO result_evidence (llm_result_id, subject, source_kind, field_name, quote, span_start, "
    "span_end) VALUES (:result, :subject, :source_kind, :field_name, :quote, :span_start, :span_end)"
)
_STORED_RESULT: Final = q(
    "SELECT r.id, r.status, r.target_id FROM llm_results r JOIN llm_tuples u ON u.id = :tuple "
    "WHERE r.job_revision_id = :revision AND r.profile_version_id = u.profile_version_id "
    "AND r.rubric_version_id = u.rubric_version_id "
    "AND r.prompt_schema_version_id = u.prompt_schema_version_id AND r.model_policy_id = u.model_policy_id"
)

log = get_logger("jobhunter.scoring.publish")


@dataclass(frozen=True)
class TargetRow:
    """The target a publication is of: its id, its job, its revision and its LLM tuple."""

    id: int
    job_id: int
    job_revision_id: int
    llm_tuple_id: int


@dataclass(frozen=True)
class ClaimPredicate:
    """The online owner of a target (§9.4.3): `in_progress` under `claim_id` and `attempt_no`."""

    claim_id: UUID
    attempt_no: int
    from_state: ClassVar[str] = "in_progress"

    def owner(self) -> dict[str, object]:
        return {"claim_id": self.claim_id, "attempt_no": self.attempt_no}


@dataclass(frozen=True)
class BatchPredicate:
    """The batch owner of a target (§10.10, Task 17): `in_batch` under `batch_submission_id` and
    `attempt_no`."""

    batch_submission_id: int
    attempt_no: int
    from_state: ClassVar[str] = "in_batch"

    def owner(self) -> dict[str, object]:
        return {"batch_submission_id": self.batch_submission_id, "attempt_no": self.attempt_no}


Predicate = ClaimPredicate | BatchPredicate


@dataclass(frozen=True)
class PublishOutcome:
    """How a publish transaction ended (module docstring): the result it published or linked (its status), the
    instances it wrote, and why."""

    result_id: int | None
    det_result_ids: list[int]
    reason: Reason
    status: PublishMode | None = None


async def lock_config_and_job(conn: AsyncConnection, job_id: int) -> None:
    """The caller's steps 1 and 1a (module docstring): `active_config FOR SHARE`, then the job `FOR SHARE`."""
    await conn.execute(_LOCK_CONFIG)
    if (await conn.execute(_LOCK_JOB_ROW, {"j": job_id})).first() is None:
        raise LookupError(f"job {job_id} does not exist")


async def _scopes(conn: AsyncConnection) -> list[ScopeRow]:
    """Step 1: the active scope, then the pending one when set, under `active_config FOR SHARE`."""
    pointers = (await conn.execute(_LOCK_CONFIG)).one()
    ids = [int(pointers.scope_version_id)]
    if pointers.pending_scope_version_id is not None:
        ids.append(int(pointers.pending_scope_version_id))
    payloads = {int(row.id): row.payload for row in await conn.execute(_SCOPES, {"ids": ids})}
    return [ScopeRow(i, scope_from_payload(get_object(payloads[i], ""))) for i in ids]


async def _fenced(conn: AsyncConnection, target: TargetRow, attempt_id: int) -> bool:
    """Step 1a: whether a purge fence holds the job; when one does, the attempt ends `cancelled`."""
    row = (await conn.execute(_LOCK_JOB, {"job": target.job_id, "tuple": target.llm_tuple_id})).first()
    if row is None:
        raise LookupError(f"job {target.job_id} does not exist")
    if not row.fenced:
        return False
    try:
        await transition(conn, "score_attempts", attempt_id, ["running"], "cancelled", reason=PURGE_FENCED)
    except TransitionConflict:
        log.info("score_publish_attempt_ended", target_id=target.id, attempt_id=attempt_id)
    log.warning("score_publish_fenced", target_id=target.id, attempt_id=attempt_id, job_id=target.job_id)
    return True


async def _owned(conn: AsyncConnection, target: TargetRow, predicate: Predicate) -> bool:
    """Step 2: the target `FOR UPDATE` under the predicate."""
    if isinstance(predicate, ClaimPredicate):
        params = {"t": target.id, "c": predicate.claim_id, "a": predicate.attempt_no}
        found = await conn.execute(_LOCK_CLAIMED, params)
    else:
        params = {"t": target.id, "s": predicate.batch_submission_id, "a": predicate.attempt_no}
        found = await conn.execute(_LOCK_BATCHED, params)
    return found.first() is not None


async def _stored_draft(conn: AsyncConnection, target_id: int, draft_id: int) -> StoredDraft:
    row = (await conn.execute(_DRAFT, {"d": draft_id, "t": target_id})).first()
    if row is None:
        raise InvariantViolation(f"score target {target_id} has no draft {draft_id}")
    return StoredDraft.from_row(
        id=int(row.id), round=int(row.round), output_json=row.output_json, validation=row.validation
    )


async def _last_review(
    conn: AsyncConnection, target_id: int, note_id: int | None, ctx: ValidationContext
) -> Review | None:
    """Step 3: the last valid critic review, validated again against the draft it reviewed."""
    if note_id is None:
        return None
    row = (await conn.execute(_NOTE, {"n": note_id, "t": target_id})).first()
    if row is None:
        raise InvariantViolation(f"score target {target_id} has no critic note {note_id}")
    note = StoredNote.from_row(
        id=int(row.id),
        draft_id=int(row.draft_id),
        derived_outcome=str(row.derived_outcome),
        review_json=row.review_json,
        validation=row.validation,
    )
    return stored_review(ctx, note, await _stored_draft(conn, target_id, note.draft_id))


async def _invalid(
    conn: AsyncConnection, target: TargetRow, attempt_id: int, predicate: Predicate, error: PublicationInvalid
) -> PublishOutcome:
    """Step 3's `Bug` (module docstring)."""
    reason = PUBLICATION_INVALID
    await transition(
        conn,
        "score_targets",
        target.id,
        [predicate.from_state],
        "failed_invalid_output",
        reason=reason,
        owner=predicate.owner(),
    )
    await transition(conn, "score_attempts", attempt_id, ["running"], "failed_invalid_output", reason=reason)
    context: dict[str, JSONValue] = {
        "target_id": target.id,
        "attempt_id": attempt_id,
        "error_types": list(error.error_types),
        "locations": list(error.locations),
    }
    await raise_alert(
        conn,
        code=PUBLICATION_INVALID_ALERT,
        severity="critical",
        subject=str(target.id),
        message="a score target's publication object failed its validation (a bug); the target failed",
        context=context,
    )
    await ranking.bump(conn)
    log.error(
        "score_publication_invalid",
        target_id=target.id,
        attempt_id=attempt_id,
        error_types=list(error.error_types),
        locations=list(error.locations),
    )
    return PublishOutcome(None, [], "publication_invalid")


async def _insert(
    conn: AsyncConnection,
    target: TargetRow,
    attempt_id: int,
    draft_id: int,
    made: Publication,
    now: datetime,
) -> int:
    """Step 4: the result row and its evidence."""
    ids = (await conn.execute(_IDS, {"a": attempt_id, "t": target.id, "tuple": target.llm_tuple_id})).first()
    if ids is None:
        raise InvariantViolation(f"score target {target.id} has no attempt {attempt_id}")
    result = made.result
    doc = result.model_dump(mode="json")
    params = {
        "target": target.id,
        "attempt": attempt_id,
        "draft": draft_id,
        "revision": target.job_revision_id,
        "profile": ids.profile_version_id,
        "rubric": ids.rubric_version_id,
        "prompt": ids.prompt_schema_version_id,
        "model_policy": ids.model_policy_id,
        "evaluation_policy": ids.evaluation_policy_id,
        "status": result.status,
        "criteria": doc["criteria"],
        "job_facts": doc["job_facts"],
        "knockouts": doc["knockout_candidates"],
        "confirmations": doc["fact_confirmations"],
        "not_a_job_ad": result.not_a_job_ad,
        "injection_suspected": result.injection_suspected,
        "nulled": list(result.nulled_criteria),
        "truncated": list(result.truncated_fields),
        "critic_outcome": result.critic_outcome,
        "critic_injection": made.critic_injection,
        "revisions_used": result.revisions_used,
        "summary_en": result.summary_en,
        "summary_de": result.summary_de,
        "now": now,
    }
    result_id = int((await conn.execute(_INSERT_RESULT, params)).scalar_one())
    rows = [
        {
            "result": result_id,
            "subject": e.subject,
            "source_kind": e.source_kind,
            "field_name": e.field_name,
            "quote": e.quote,
            "span_start": e.span_start,
            "span_end": e.span_end,
        }
        for e in made.evidence
    ]
    if rows:
        await conn.execute(_INSERT_EVIDENCE, rows)
    return result_id


async def _instances(
    conn: AsyncConnection, target: TargetRow, result_id: int, scopes: Sequence[ScopeRow], inputs: ScoreInputs
) -> list[int]:
    """Step 5: the instances for the active and the pending scope."""
    sources = await job_sources(conn, target.job_id)
    written: list[int] = []
    for scope in scopes:
        instance = await compute_and_write(
            conn,
            llm_result_id=result_id,
            scope=scope,
            det=inputs.det,
            sources=sources,
            job=JobRow(target.job_id),
        )
        if instance is not None:
            written.append(instance)
    return written


async def publish_result(
    conn: AsyncConnection,
    *,
    target: TargetRow,
    attempt_id: int,
    final_draft_id: int,
    mode: PublishMode,
    predicate: Predicate,
    inputs: ScoreInputs,
    last_critic_note_id: int | None,
    now: datetime,
) -> PublishOutcome:
    """Publish draft `final_draft_id` of `target`'s attempt `attempt_id` in `mode`, in the caller's
    transaction, which holds `active_config` and the job `FOR SHARE` (module docstring). `inputs` are the
    attempt's (its ad, profile and rubric); the job's `DetFacts` are read again under the job lock, and
    `last_critic_note_id` is the last valid critic note of the attempt chain (`None` when no review was
    valid). `now` stamps `published_at`."""
    scopes = await _scopes(conn)
    if await _fenced(conn, target, attempt_id):
        return PublishOutcome(None, [], "fenced")
    if not await _owned(conn, target, predicate):
        log.info("score_publish_claim_lost", target_id=target.id, attempt_id=attempt_id)
        return PublishOutcome(None, [], "claim_lost")
    released = await bulk.release_target_hold(conn, target.id)
    inputs = replace(inputs, det=await read_det_facts(conn, target.job_id))
    ctx = inputs.ctx
    stored = await _stored_draft(conn, target.id, final_draft_id)
    invalid: PublicationInvalid | None
    try:
        made = publication.build(
            draft=stored.draft,
            validation=stored_validation(ctx, stored),
            last_review=await _last_review(conn, target.id, last_critic_note_id, ctx),
            mode=mode,
            scope=scopes[0].settings,
            ctx=ctx,
            weights=inputs.rubric.weights,
            revisions_used=stored.round,
        )
    except PublicationInvalid as error:
        invalid = error
    else:
        invalid = None
    if invalid is not None:
        return await _invalid(conn, target, attempt_id, predicate, invalid)
    result_id = await _insert(conn, target, attempt_id, final_draft_id, made, now)
    instances = await _instances(conn, target, result_id, scopes, inputs)
    published = f"published_{mode}"
    await transition(
        conn,
        "score_targets",
        target.id,
        [predicate.from_state],
        published,
        owner=predicate.owner(),
        set_={"published_result_id": result_id},
    )
    await groups.representative_published(conn, target.job_id)
    await transition(conn, "score_attempts", attempt_id, ["running"], published)
    await ranking.bump(conn)
    log.info(
        "score_published",
        target_id=target.id,
        attempt_id=attempt_id,
        llm_result_id=result_id,
        draft_id=final_draft_id,
        mode=mode,
        nulled=len(made.result.nulled_criteria),
        truncated=len(made.truncated_fields),
        evidence=len(made.evidence),
        det_result_ids=instances,
        hold_released_usd=str(released),
    )
    await afault_point(BEFORE_COMMIT)
    return PublishOutcome(result_id, instances, "published", mode)


async def link_result(
    conn: AsyncConnection, *, target: TargetRow, attempt_id: int, predicate: Predicate
) -> PublishOutcome:
    """Link the stored result of `target`'s 5-tuple and end the attempt with it, inserting nothing (module
    docstring), in the caller's transaction, which holds `active_config` and the job `FOR SHARE`."""
    await _scopes(conn)
    if await _fenced(conn, target, attempt_id):
        return PublishOutcome(None, [], "fenced")
    if not await _owned(conn, target, predicate):
        log.info("score_publish_claim_lost", target_id=target.id, attempt_id=attempt_id)
        return PublishOutcome(None, [], "claim_lost")
    stored = (
        await conn.execute(_STORED_RESULT, {"tuple": target.llm_tuple_id, "revision": target.job_revision_id})
    ).first()
    if stored is None or int(stored.target_id) != target.id:
        raise InvariantViolation(f"score target {target.id}: no stored result of its 5-tuple to link")
    status: PublishMode = "contested" if stored.status == "contested" else "accepted"
    await bulk.release_target_hold(conn, target.id)
    published = f"published_{status}"
    await transition(
        conn,
        "score_targets",
        target.id,
        [predicate.from_state],
        published,
        owner=predicate.owner(),
        set_={"published_result_id": int(stored.id)},
    )
    await transition(conn, "score_attempts", attempt_id, ["running"], published)
    await ranking.bump(conn)
    log.info("score_result_linked", target_id=target.id, attempt_id=attempt_id, llm_result_id=int(stored.id))
    return PublishOutcome(int(stored.id), [], "published", status)
