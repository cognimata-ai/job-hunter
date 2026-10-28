"""Feedback, golden cases and the adversarial seed on PostgreSQL (spec §11.13 L4610-4632, §14.2 L4977-4979;
plan Task 19, R2, Deviation 21, Codex r1 #32): `scoring.golden`.

- `record_feedback` writes `score_feedback` with the score snapshot of the job's current published result
  of the active tuple (through `current_scores`) and, with a profile active, upserts the golden case of
  (revision, profile) with the active scope, the instance's `det_inputs` and their source ids: a `label` is
  a `week1` case, a `wrong_score` a `feedback` one; a second label updates the case; a job without a
  published result is 409 `conflict_state`.
- `list_cases` pages by id (keyset), `delete_case` takes the case's eval rows and calls with it,
  `set_flags` refuses a third canary (422).
- `seed_adversarial` inserts the ten fixtures' marked jobs, profiles, scopes and cases once; a synthetic job
  gets no target from `missing_targets`, no embedding (the `awaiting_embedding` scan selects none of its
  revisions) and no row in either current-score view.

Every datum is invented.
"""

import hashlib
from pathlib import Path
from typing import Any, Final

import pytest
import sqlalchemy as sa
from jobhunter.api.errors import Problem
from jobhunter.clock import utcnow
from jobhunter.embeddings import _COUNT_AWAITING, _NEXT_BATCH, _NO_TIME_BOUND, _START
from jobhunter.scoring import golden
from jobhunter.scoring.eval_fixtures import adversarial_dir
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.eval import cases, scored_job, seed_adversarial
from tests.support.llm import FakeLLM
from tests.support.m2 import insert_eval_run, insert_llm_call
from tests.support.m3 import NOW, insert_job, insert_row
from tests.support.reconcile import run_reconciler
from tests.support.scoring import activate_score_tuple

pytestmark = pytest.mark.integration

_FEEDBACK: Final = sa.text("SELECT * FROM score_feedback WHERE id = :i")
_CASE: Final = sa.text("SELECT * FROM golden_cases WHERE id = :i")
_INSTANCE: Final = sa.text("SELECT * FROM det_results WHERE id = :i")
_RESULT: Final = sa.text("SELECT * FROM llm_results WHERE id = :i")
_ACTIVE: Final = sa.text("SELECT profile_version_id, scope_version_id FROM active_config WHERE id = 1")


async def one(engine: AsyncEngine, statement: sa.TextClause, row_id: int) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(statement, {"i": row_id})).one()


async def feedback(engine: AsyncEngine, job_id: int, **kwargs: Any) -> golden.Feedback:
    values: dict[str, Any] = {"kind": "label", "label": "good", "comment": "Passt gut.", **kwargs}
    async with engine.begin() as conn:
        return await golden.record_feedback(conn, job_id=job_id, now=NOW, **values)


async def test_a_label_writes_the_score_snapshot_and_upserts_the_case(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        scored = await scored_job(conn)
    made = await feedback(engine, scored.job.job_id)
    assert made.golden_case_id is not None
    row = await one(engine, _FEEDBACK, made.feedback_id)
    instance = await one(engine, _INSTANCE, scored.det_result_id)
    result = await one(engine, _RESULT, scored.result_id)
    assert (row.job_id, row.job_revision_id, row.llm_result_id, row.det_result_id) == (
        scored.job.job_id,
        scored.job.revision_id,
        scored.result_id,
        scored.det_result_id,
    )
    assert row.profile_version_id == result.profile_version_id
    assert (row.kind, row.label, row.comment) == ("label", "good", "Passt gut.")
    assert row.score_snapshot == {
        "L": str(instance.score_low),
        "M": str(instance.score_mid),
        "U": str(instance.score_high),
        "coverage": str(instance.coverage),
        "confidence": instance.confidence,
        "llm_tuple_id": scored.llm_tuple_id,
        "rubric_version_id": result.rubric_version_id,
        "prompt_schema_version_id": result.prompt_schema_version_id,
        "model_policy_id": result.model_policy_id,
        "scope_version_id": scored.scope_version_id,
        "det_result_id": scored.det_result_id,
    }
    case = await one(engine, _CASE, made.golden_case_id)
    async with engine.connect() as conn:
        active = (await conn.execute(_ACTIVE)).one()
    assert (case.job_revision_id, case.profile_version_id, case.scope_version_id) == (
        scored.job.revision_id,
        active.profile_version_id,
        active.scope_version_id,
    )
    assert (case.label, case.comment, case.source, case.labelled_from_result_id) == (
        "good",
        "Passt gut.",
        "week1",
        scored.result_id,
    )
    assert case.det_inputs_snapshot == instance.det_inputs  # the instance's frozen inputs
    assert case.det_input_sources == sorted(scored.sources)  # every source the snapshot draws on
    assert (case.active, case.is_canary, case.expected_knockouts) == (True, False, [])


async def test_a_second_label_updates_the_case(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        scored = await scored_job(conn)
    first = await feedback(engine, scored.job.job_id)
    second = await feedback(engine, scored.job.job_id, label="poor", comment=None)
    assert second.golden_case_id == first.golden_case_id and second.feedback_id != first.feedback_id
    assert first.golden_case_id is not None
    case = await one(engine, _CASE, first.golden_case_id)
    assert (case.label, case.comment, case.source) == ("poor", None, "week1")
    assert len(await cases(engine)) == 1  # one case per (revision, profile)


async def test_wrong_score_feedback_makes_a_feedback_case(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        scored = await scored_job(conn)
    made = await feedback(engine, scored.job.job_id, kind="wrong_score", label="poor")
    assert made.golden_case_id is not None
    assert (await one(engine, _FEEDBACK, made.feedback_id)).kind == "wrong_score"
    assert (await one(engine, _CASE, made.golden_case_id)).source == "feedback"


async def test_feedback_without_a_published_result_is_conflict_state(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await activate_score_tuple(conn)
        job = await insert_job(conn)
    with pytest.raises(Problem) as refused:
        await feedback(engine, job.job_id)
    assert (refused.value.status, refused.value.code) == (409, "conflict_state")
    async with engine.connect() as conn:
        written = (await conn.execute(sa.text("SELECT count(*) FROM score_feedback"))).scalar_one()
    assert written == 0 and await cases(engine) == []


@pytest.mark.parametrize("comment", ["mit \x00 darin", "mit \ud800 darin", "x" * 2001])
async def test_an_unstorable_comment_is_refused_before_any_write(engine: AsyncEngine, comment: str) -> None:
    async with engine.begin() as conn:
        scored = await scored_job(conn)
    with pytest.raises(Problem) as refused:
        await feedback(engine, scored.job.job_id, comment=comment)
    assert (refused.value.status, refused.value.code) == (422, "validation_failed")


async def test_list_cases_pages_by_id(engine: AsyncEngine) -> None:
    await seed_adversarial(engine)
    seeded = await cases(engine)
    async with engine.begin() as conn:
        await golden.set_flags(conn, seeded[3].id, active=False, is_canary=None)
    async with engine.connect() as conn:
        first = await golden.list_cases(conn, after_id=None, limit=4, active=None)
        rest = await golden.list_cases(conn, after_id=first[-1].id, limit=20, active=None)
        inactive = await golden.list_cases(conn, after_id=None, limit=20, active=False)
        active = await golden.list_cases(conn, after_id=None, limit=20, active=True)
    assert [c.id for c in first + rest] == [c.id for c in seeded]
    assert len(first) == 4 and len(rest) == 6
    assert [c.id for c in inactive] == [seeded[3].id] and len(active) == 9
    row = first[0]
    assert (row.job_id, row.source, row.active, row.is_canary) == (
        seeded[0].job_id,
        "adversarial",
        True,
        False,
    )
    assert row.scope_version is not None and row.profile_version >= 1 and row.label == seeded[0].label


async def test_delete_case_takes_its_eval_rows_with_it(engine: AsyncEngine) -> None:
    await seed_adversarial(engine)
    kept, deleted = (await cases(engine))[:2]
    async with engine.begin() as conn:
        run = await insert_eval_run(conn)
        _, call = await insert_llm_call(conn, purpose="generate", eval_run_id=run)
        await conn.execute(
            sa.text("UPDATE llm_calls SET golden_case_id = :g WHERE id = :c"), {"g": deleted.id, "c": call}
        )
        for case in (kept, deleted):
            await insert_row(
                conn,
                "eval_case_results",
                {
                    "eval_run_id": run,
                    "golden_case_id": case.id,
                    "outcome": "failed",
                    "critic_ran": False,
                    "llm_call_ids": [],
                    "output": {},
                },
            )
    async with engine.begin() as conn:
        assert await golden.delete_case(conn, deleted.id) is True
    async with engine.begin() as conn:
        assert await golden.delete_case(conn, deleted.id) is False  # already gone
    async with engine.connect() as conn:
        results = (await conn.execute(sa.text("SELECT golden_case_id FROM eval_case_results"))).scalars()
        assert list(results) == [kept.id]
        calls = await conn.execute(sa.text("SELECT count(*) FROM llm_calls WHERE id = :c"), {"c": call})
        assert calls.scalar_one() == 0  # the eval call of the case goes with it
    left = {c.id for c in await cases(engine)}
    assert kept.id in left and deleted.id not in left and len(left) == 9


async def test_set_flags_allows_two_canaries_and_refuses_a_third(engine: AsyncEngine) -> None:
    await seed_adversarial(engine)
    first, second, third = (await cases(engine))[:3]
    async with engine.begin() as conn:
        assert (await golden.set_flags(conn, first.id, active=None, is_canary=True)).is_canary
        made = await golden.set_flags(conn, second.id, active=None, is_canary=True)
    assert (made.id, made.is_canary, made.active) == (second.id, True, True)
    with pytest.raises(Problem) as refused:
        async with engine.begin() as conn:
            await golden.set_flags(conn, third.id, active=None, is_canary=True)
    assert (refused.value.status, refused.value.code) == (422, "validation_failed")
    async with engine.begin() as conn:
        again = await golden.set_flags(conn, first.id, active=False, is_canary=True)  # already one: allowed
        assert (again.is_canary, again.active) == (True, False)
        await golden.set_flags(conn, first.id, active=None, is_canary=False)
        assert (await golden.set_flags(conn, third.id, active=None, is_canary=True)).is_canary
    with pytest.raises(Problem) as missing:
        async with engine.begin() as conn:
            await golden.set_flags(conn, 10**12, active=True, is_canary=None)
    assert missing.value.status == 404


def test_tuple_identity_is_the_hex_sha256_of_the_four_ids() -> None:
    canonical = (
        b'{"model_policy_id":4,"profile_version_id":1,"prompt_schema_version_id":3,"rubric_version_id":2}'
    )
    identity = golden.tuple_identity(
        profile_version_id=1, rubric_version_id=2, prompt_schema_version_id=3, model_policy_id=4
    )
    assert identity == hashlib.sha256(canonical).hexdigest()
    other = golden.tuple_identity(
        profile_version_id=1, rubric_version_id=2, prompt_schema_version_id=3, model_policy_id=5
    )
    assert other != identity and len(other) == 64


_SEEDED_JOBS: Final = sa.text(
    "SELECT j.id, j.company_key, j.synthetic_eval, j.availability, j.current_revision_id, "
    "r.job_id AS rev_job, r.embedding_status FROM jobs j "
    "JOIN job_revisions r ON r.id = j.current_revision_id ORDER BY j.id"
)
_SEEDED_PROFILES: Final = sa.text(
    "SELECT v.id, v.synthetic_eval, d.status, c.stripped_text IS NOT NULL AS has_text, c.strip_report "
    "FROM profile_versions v JOIN profile_drafts d ON d.id = v.draft_id "
    "JOIN cv_files c ON c.id = v.cv_file_id ORDER BY v.id"
)


async def test_the_seed_inserts_ten_marked_cases_once(engine: AsyncEngine) -> None:
    assert await seed_adversarial(engine) == golden.Seeded(cases=10, jobs=10, profiles=10)
    async with engine.connect() as conn:
        jobs = (await conn.execute(_SEEDED_JOBS)).all()
        profiles = (await conn.execute(_SEEDED_PROFILES)).all()
    assert len(jobs) == 10 and all(j.synthetic_eval and j.availability == "active" for j in jobs)
    assert {j.company_key for j in jobs} == {f"eval-{key}" for key in _KEYS}
    assert all(j.rev_job == j.id and j.embedding_status == "awaiting_embedding" for j in jobs)
    assert len(profiles) == 10 and all(p.synthetic_eval and p.status == "confirmed" for p in profiles)
    assert all(p.has_text and isinstance(p.strip_report, dict) for p in profiles)
    seeded = await cases(engine)
    assert len(seeded) == 10 and {c.source for c in seeded} == {"adversarial"}
    by_job = {c.job_id: c for c in seeded}
    clearance = by_job[next(j.id for j in jobs if j.company_key == "eval-clearance_required")]
    row = await one(engine, _CASE, clearance.id)
    assert (row.label, row.expected_knockouts, row.det_input_sources) == (
        "poor",
        ["security_clearance:clearance"],
        [],
    )
    fixture = golden.load_fixture(golden_fixture("clearance_required"))
    assert row.det_inputs_snapshot == fixture.snapshot()
    scope = await one(
        engine, sa.text("SELECT payload FROM scope_versions WHERE id = :i"), row.scope_version_id
    )
    assert scope.payload == fixture.scope_payload()
    # idempotent by the fixture's key: a second seed inserts nothing
    assert await seed_adversarial(engine) == golden.Seeded(cases=0, jobs=0, profiles=0)
    assert [c.id for c in await cases(engine)] == [c.id for c in seeded]


async def test_a_deleted_adversarial_case_is_seeded_again(engine: AsyncEngine) -> None:
    await seed_adversarial(engine)
    first = (await cases(engine))[0]
    async with engine.begin() as conn:
        assert await golden.delete_case(conn, first.id)
    assert await seed_adversarial(engine) == golden.Seeded(cases=1, jobs=0, profiles=0)
    again = [c for c in await cases(engine) if c.job_id == first.job_id]
    assert len(again) == 1 and again[0].profile_version_id == first.profile_version_id


async def test_a_synthetic_job_gets_no_target_embedding_or_score_row(
    engine: AsyncEngine, current_epoch: int, queue_app: object, fake_llm: FakeLLM
) -> None:
    await seed_adversarial(engine)
    async with engine.begin() as conn:
        tuple_id = await activate_score_tuple(conn)
        regular = await insert_job(conn)  # the control: an ordinary job of the same tables
    assert await run_reconciler("missing_targets", engine, now=utcnow(), epoch=current_epoch) == 1
    async with engine.connect() as conn:
        targeted = (
            await conn.execute(
                sa.text("SELECT job_id FROM score_targets WHERE llm_tuple_id = :t"), {"t": tuple_id}
            )
        ).scalars()
        assert list(targeted) == [regular.job_id]
        params = {"before": _NO_TIME_BOUND, "after_at": _START[0], "after_id": _START[1], "n": 100}
        awaiting = [int(row.id) for row in await conn.execute(_NEXT_BATCH, params)]
        assert awaiting == [regular.revision_id]
        assert (await conn.execute(_COUNT_AWAITING, {"before": _NO_TIME_BOUND})).scalar_one() == 1
        for view in ("current_scores", "display_scores"):
            shown = (await conn.execute(sa.text(f"SELECT job_id FROM {view}"))).scalars()  # noqa: S608
            assert list(shown) == [regular.job_id], view
    assert fake_llm.served == 0


_KEYS: Final = (
    "injection_de",
    "injection_en",
    "adzuna_snippet",
    "english_ad_german_c1",
    "german_ad_english_only",
    "overqualified",
    "underqualified",
    "not_a_job_ad",
    "clearance_required",
    "degree_or_equivalent",
)


def golden_fixture(key: str) -> Path:
    return adversarial_dir() / f"{key}.json"
