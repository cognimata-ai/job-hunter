"""Score instances (spec §7.11 L2479-2512, §11.11 L4573-4581; M3 Task 6).

- `write_instance` locks the `llm_results` row, supersedes the previous current instance of the (result,
  scope) first and inserts the new one second, in one transaction: the immediate partial unique index
  `det_results_current` refuses the other order, so both rows are visible in the transaction and it never
  fires. An instance whose hash and algorithm version equal the current one's is a no-op; two concurrent
  recomputes of one result serialise on the result's lock; a superseded instance keeps its scores.
- `compute_and_write` rebuilds the aggregation from the stored result with no LLM call and derives the
  knockouts under the given scope, so a scope that makes a confirmed on-site requirement unmet caps the
  instance, and the confidence of a scope-only recomputation reads the stored `critic_injection`
  (`test_scope_only_recompute_keeps_confidence`, Codex r1 #11).

Every datum is invented.
"""

import asyncio
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, Final

import pytest
import sqlalchemy as sa
from jobhunter.db.sql import q
from jobhunter.scoring.aggregate import Aggregation
from jobhunter.scoring.det_results import JobRow, ScopeRow, compute_and_write, det_inputs, write_instance
from jobhunter.scoring.rubric import LLM_CRITERIA, load_rubric
from jobhunter.scoring.versioning import DET_ALGORITHM_VERSION
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.support.facts import facts, scope
from tests.support.m2 import insert_profile_chain
from tests.support.m3 import active_ids, insert_id, insert_job, insert_published
from tests.support.pg import PgServer, blocked_or_done
from tests.support.procs import await_until
from tests.support.scoring import job_facts

pytestmark = pytest.mark.integration

ONSITE: Final = {"asserted": True, "evidence": ["Arbeit vor Ort in Berlin"]}
SCORED: Final = Aggregation(
    low=Decimal("60.25"),
    mid=Decimal("70.13"),
    high=Decimal("80.00"),
    coverage=Decimal("0.85"),
    confidence="high",
    caps_applied=[],
    det_criteria={"location_work_mode": {"level": 4, "basis": "observed", "inputs": {}}},
    knockouts=[],
    industry_match="neutral",
)
CAPPED: Final = replace(
    SCORED,
    low=Decimal(20),
    mid=Decimal("22.50"),
    high=Decimal(25),
    caps_applied=[{"code": "onsite_required", "kind": "hard"}],
)
_WEIGHTS: Final = q("UPDATE rubric_versions SET weights = :w WHERE id = :r", jsonb=["w"])
_RESULT: Final = q(
    "UPDATE llm_results SET criteria = :criteria, job_facts = :job_facts, "
    "fact_confirmations = :confirmations, status = :status, injection_suspected = :injection, "
    "critic_outcome = :critic, critic_injection = :critic_injection, not_a_job_ad = :not_a_job_ad "
    "WHERE id = :id",
    jsonb=["criteria", "job_facts", "confirmations"],
)
_INSTANCES: Final = sa.text(
    "SELECT id, is_current, superseded_at IS NOT NULL AS superseded, score_low, score_mid, score_high, "
    "coverage, confidence, det_inputs_hash, det_algorithm_version, caps_applied, knockouts "
    "FROM det_results WHERE llm_result_id = :r ORDER BY id"
)


@dataclass(frozen=True)
class Stored:
    result_id: int
    job: JobRow
    scope: ScopeRow


async def stored_result(
    c: AsyncConnection,
    n: int = 1,
    *,
    levels: dict[str, int | None] | None = None,
    facts_doc: dict[str, Any] | None = None,
    confirmations: dict[str, str] | None = None,
    status: str = "accepted",
    injection_suspected: bool = False,
    critic: str = "not_run",
    critic_injection: str | None = None,
    not_a_job_ad: bool = False,
) -> Stored:
    """A published result of a new job whose stored columns are the given ones, its rubric with v2's weights,
    and the active scope version with the seeded scope."""
    chain = await insert_profile_chain(c, n)
    await c.execute(_WEIGHTS, {"w": dict(load_rubric("v2").weights), "r": chain.rubric_version_id})
    job = await insert_job(c)
    published = await insert_published(c, job, chain.llm_tuple_id)
    chosen = dict.fromkeys(LLM_CRITERIA, 4) if levels is None else levels
    criteria = {
        key: {"level": level, "basis": "unknown" if level is None else "observed", "rationale": "Erfunden."}
        for key, level in chosen.items()
    }
    await c.execute(
        _RESULT,
        {
            "id": published.result_id,
            "criteria": criteria,
            "job_facts": (job_facts() if facts_doc is None else job_facts(**facts_doc)).model_dump(
                mode="json"
            ),
            "confirmations": confirmations or {},
            "status": status,
            "injection": injection_suspected,
            "critic": critic,
            "critic_injection": critic_injection,
            "not_a_job_ad": not_a_job_ad,
        },
    )
    active = await active_ids(c)
    return Stored(published.result_id, JobRow(job.job_id), ScopeRow(active.scope_version_id, scope()))


async def instances(c: AsyncConnection, result_id: int) -> list[Any]:
    return list((await c.execute(_INSTANCES, {"r": result_id})).all())


async def write(
    c: AsyncConnection, stored: Stored, aggregation: Aggregation, digest: bytes, **more: Any
) -> int | None:
    return await write_instance(
        c,
        llm_result_id=stored.result_id,
        scope_version_id=more.pop("scope_version_id", stored.scope.id),
        job_id=stored.job.id,
        aggregation=aggregation,
        det_inputs={"job": {}, "scope": {"n": digest.hex()}},
        det_inputs_hash=digest,
        **more,
    )


async def other_scope(c: AsyncConnection, name: bytes) -> int:
    return await insert_id(c, "scope_versions", {"scope_hash": name, "payload": {}})


# -- write_instance ------------------------------------------------------------------------------------------


async def test_write_instance_supersedes_the_current_row_first_and_inserts_second(
    engine: AsyncEngine,
) -> None:
    async with engine.connect() as c:
        stored = await stored_result(c)
        first = await write(c, stored, SCORED, b"a")
        second = await write(c, stored, CAPPED, b"b")
        assert first is not None and second is not None and second > first
        rows = await instances(c, stored.result_id)  # both visible in the one transaction
        assert [(r.id, r.is_current, r.superseded) for r in rows] == [
            (first, False, True),
            (second, True, False),
        ]
        assert rows[1].det_algorithm_version == DET_ALGORITHM_VERSION
        assert rows[1].caps_applied == [{"code": "onsite_required", "kind": "hard"}]
        # the index is immediate: an insert before the supersede is refused, so the order above is the one
        with pytest.raises(IntegrityError, match="det_results_current"):
            async with c.begin_nested():
                await c.execute(
                    sa.text(
                        "INSERT INTO det_results (llm_result_id, scope_version_id, job_id, "
                        "det_algorithm_version, det_criteria, knockouts, industry_match, caps_applied, "
                        "det_inputs, det_inputs_hash) "
                        "VALUES (:r, :s, :j, 'x', '{}', '[]', 'neutral', '[]', '{}', '\\x00')"
                    ),
                    {"r": stored.result_id, "s": stored.scope.id, "j": stored.job.id},
                )
        await c.rollback()


async def test_an_equal_hash_and_version_is_a_no_op(engine: AsyncEngine) -> None:
    async with engine.connect() as c:
        stored = await stored_result(c)
        first = await write(c, stored, SCORED, b"a")
        assert await write(c, stored, CAPPED, b"a") is None
        assert [(r.id, r.is_current) for r in await instances(c, stored.result_id)] == [(first, True)]
        # another algorithm version, or another scope, is a new instance
        second = await write(c, stored, SCORED, b"a", algorithm_version="det-next")
        elsewhere = await write(c, stored, SCORED, b"a", scope_version_id=await other_scope(c, b"t6-other"))
        rows = await instances(c, stored.result_id)
        assert [(r.id, r.is_current) for r in rows] == [(first, False), (second, True), (elsewhere, True)]
        assert await write(c, stored, CAPPED, b"a", algorithm_version="det-next") is None
        await c.rollback()


async def test_a_superseded_instance_keeps_its_scores(engine: AsyncEngine) -> None:
    async with engine.connect() as c:
        stored = await stored_result(c)
        first = await write(c, stored, SCORED, b"a")
        await write(c, stored, CAPPED, b"b")
        old = next(r for r in await instances(c, stored.result_id) if r.id == first)
        assert (old.score_low, old.score_mid, old.score_high, old.coverage, old.confidence) == (
            Decimal("60.25"),
            Decimal("70.13"),
            Decimal("80.00"),
            Decimal("0.850"),
            "high",
        )
        assert not old.is_current and old.superseded
        await c.rollback()


async def test_two_concurrent_recomputes_serialise_on_the_result_lock(
    engine: AsyncEngine, migrated: PgServer
) -> None:
    async with engine.begin() as c:
        stored = await stored_result(c)
    held, release = asyncio.Event(), asyncio.Event()

    async def first() -> int | None:
        async with engine.begin() as c:
            written = await write(c, stored, SCORED, b"same")
            held.set()
            await release.wait()  # the result row stays locked until this transaction commits
            return written

    async def second() -> int | None:
        async with engine.begin() as c:
            return await write(c, stored, SCORED, b"same")

    one = asyncio.create_task(first())
    await asyncio.wait_for(held.wait(), 10)
    two = asyncio.create_task(second())
    await await_until(lambda: blocked_or_done(migrated, two), 10, 0.02)
    assert not two.done()  # it waits at the `llm_results` row lock
    release.set()
    written, again = await asyncio.gather(one, two)
    assert written is not None and again is None  # the second saw the first's committed instance
    async with engine.connect() as c:
        assert [(r.id, r.is_current) for r in await instances(c, stored.result_id)] == [(written, True)]


async def test_write_instance_of_an_unknown_result_is_a_lookup_error(engine: AsyncEngine) -> None:
    async with engine.connect() as c:
        stored = await stored_result(c)
        with pytest.raises(LookupError):
            await write(c, replace(stored, result_id=stored.result_id + 1000), SCORED, b"a")
        await c.rollback()


# -- compute_and_write ---------------------------------------------------------------------------------------


async def test_compute_and_write_rebuilds_the_stored_result_under_a_scope(engine: AsyncEngine) -> None:
    async with engine.connect() as c:
        stored = await stored_result(
            c, facts_doc={"onsite_only": ONSITE}, confirmations={"onsite_required:onsite": "critic_confirmed"}
        )
        det, sources = facts(), {"locations": ["jsearch"]}
        first = await compute_and_write(
            c, llm_result_id=stored.result_id, scope=stored.scope, det=det, sources=sources, job=stored.job
        )
        assert first is not None
        again = await compute_and_write(
            c, llm_result_id=stored.result_id, scope=stored.scope, det=det, sources=sources, job=stored.job
        )
        assert again is None  # the same inputs: a no-op
        remote_only = ScopeRow(await other_scope(c, b"t6-remote"), scope(work_modes=frozenset({"remote"})))
        capped = await compute_and_write(
            c, llm_result_id=stored.result_id, scope=remote_only, det=det, sources=sources, job=stored.job
        )
        rows = {r.id: r for r in await instances(c, stored.result_id)}
        # all six LLM criteria at 4 (85) and employment 4 (5); the location is unknown (10): 90..100
        plain = rows[first]
        assert (plain.score_low, plain.score_mid, plain.score_high, plain.coverage, plain.confidence) == (
            Decimal(90),
            Decimal(95),
            Decimal(100),
            Decimal("0.900"),
            "high",
        )
        assert plain.caps_applied == []
        onsite = {
            "code": "onsite_required",
            "fact_id": "onsite_required:onsite",
            "severity": "hard",
            "source": "critic_confirmed",
        }
        assert plain.knockouts == [{**onsite, "comparison": "uncomparable", "binding": False}]
        assert plain.det_inputs_hash == det_inputs(det, sources, stored.scope.settings)[1]
        # the remote-only scope makes the confirmed on-site requirement unmet: the hard cap
        assert capped is not None
        cap = rows[capped]
        assert (cap.score_low, cap.score_mid, cap.score_high) == (Decimal(25), Decimal(25), Decimal(25))
        assert cap.caps_applied == [{"code": "onsite_required", "kind": "hard"}]
        assert cap.knockouts == [{**onsite, "comparison": "unmet", "binding": True}]
        assert rows[first].is_current and cap.is_current  # one current instance per scope
        await c.rollback()


@pytest.mark.parametrize(
    ("critic_injection", "expected"), [("present_no_influence", "low"), ("none", "high")]
)
async def test_scope_only_recompute_keeps_confidence(
    engine: AsyncEngine, critic_injection: str, expected: str
) -> None:
    async with engine.connect() as c:
        stored = await stored_result(
            c, injection_suspected=True, critic="accepted", critic_injection=critic_injection
        )
        det, sources = facts(), dict[str, list[str]]()
        published = await compute_and_write(
            c, llm_result_id=stored.result_id, scope=stored.scope, det=det, sources=sources, job=stored.job
        )
        # a scope that changes an input of the instance, but no input of its confidence
        later = ScopeRow(await other_scope(c, b"t6-later"), scope(industries_exclude=frozenset({"tabak"})))
        recomputed = await compute_and_write(
            c, llm_result_id=stored.result_id, scope=later, det=det, sources=sources, job=stored.job
        )
        rows = {r.id: r for r in await instances(c, stored.result_id)}
        assert published is not None and recomputed is not None
        assert rows[published].det_inputs_hash != rows[recomputed].det_inputs_hash
        assert (rows[published].confidence, rows[recomputed].confidence) == (expected, expected)
        assert rows[published].score_mid == rows[recomputed].score_mid == Decimal(95)
        await c.rollback()


async def test_a_contested_partial_result_keeps_its_downgrade(engine: AsyncEngine) -> None:
    async with engine.connect() as c:
        levels: dict[str, int | None] = {**dict.fromkeys(LLM_CRITERIA, 4), "skills": None}
        stored = await stored_result(
            c, levels=levels, status="contested", critic="contested", critic_injection="none"
        )
        written = await compute_and_write(
            c, llm_result_id=stored.result_id, scope=stored.scope, det=facts(), sources={}, job=stored.job
        )
        (row,) = [r for r in await instances(c, stored.result_id) if r.id == written]
        # coverage 0.65 is medium; contested downgrades it once
        assert (row.coverage, row.confidence) == (Decimal("0.650"), "low")
        await c.rollback()


async def test_a_verified_not_a_job_ad_instance_has_no_score(engine: AsyncEngine) -> None:
    async with engine.connect() as c:
        stored = await stored_result(
            c,
            levels=dict.fromkeys(LLM_CRITERIA),
            not_a_job_ad=True,
            critic="accepted",
            critic_injection="none",
        )
        written = await compute_and_write(
            c, llm_result_id=stored.result_id, scope=stored.scope, det=facts(), sources={}, job=stored.job
        )
        (row,) = [r for r in await instances(c, stored.result_id) if r.id == written]
        assert (row.score_low, row.score_mid, row.score_high, row.confidence) == (None, None, None, None)
        assert row.coverage == Decimal(0)
        await c.rollback()
