"""Canaries (spec §11.13 L4632, §3.3.2 L285; plan Task 20, Ruling P33, A1 issue 25, Deviation 29): the
`canary_daily` task starts a canary run of the active configuration (`eval_runner.start_canary_run`), the
runner of Task 19 scores the canaries under their own profile and scope, and the run's terminal transaction
raises `scoring.canary_drift` (warn, subject the case) for each canary more than 8 points from its baseline,
the first canary score under `(golden_case_id, rubric, prompt schema, model policy, evaluation policy)`.

The fake answers each canary by its revision (Ruling P26): every criterion at level 2, or two criteria a level
higher. A level of `skills` (25) and `domain` (10) moves M by 35 / 4 = 8.75 points, one of `role_fit` (20) and
`domain` by 30 / 4 = 7.5: the rubric's weights are multiples of 5, so no answer lands on 8 or 9 exactly (the
boundary itself is `tests/unit/test_calibration.py`'s). Every datum is invented.
"""

import copy
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Final, cast

import procrastinate
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.scoring import eval_runner, golden
from jobhunter.worker import tasks
from jobhunter.worker.tasks import _reset_task_app, job_task_name, make_queue_app
from procrastinate.job_context import JobContext
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.eval import (
    CaseRef,
    case_rows,
    eval_deps,
    eval_generator_body,
    execute,
    fake_eval_answers,
    insert_cases,
    run_row,
)
from tests.support.faults import no_fault
from tests.support.llm import FakeLLM, score_profile
from tests.support.scoring import activate_score_tuple, activate_test_policy
from tests.support.settings import procrastinate_job_exists_for
from tests.support.validation import synthetic_ad

pytestmark = pytest.mark.integration

DRIFT: Final = "scoring.canary_drift"
BY_8_75: Final = ("skills", "domain")  # +1 level: 25 / 4 + 10 / 4
BY_7_5: Final = ("role_fit", "domain")  # +1 level: 20 / 4 + 10 / 4


@pytest.fixture(autouse=True)
async def _policy(engine: AsyncEngine, clean_db: None) -> None:
    """The seeded thresholds with `t8_sample_pct = 0` (Ruling P48)."""
    async with engine.begin() as conn:
        await activate_test_policy(conn)


@pytest.fixture(autouse=True)
def _unfaulted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(eval_runner, "afault_point", no_fault)


def context(engine: AsyncEngine, fake_llm: FakeLLM) -> JobContext:
    """The task's context: a runtime with the engine and the worker's config (on the fake)."""
    runtime = SimpleNamespace(engine=engine, cfg=fake_llm.cfg)
    return cast(JobContext, SimpleNamespace(additional_context={"runtime": runtime}))


async def canary_runs(engine: AsyncEngine) -> list[Any]:
    async with engine.connect() as conn:
        return list((await conn.execute(sa.text("SELECT * FROM eval_runs ORDER BY id"))).all())


async def operation_of(engine: AsyncEngine, eval_run_id: int) -> Any:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                sa.text(
                    "SELECT id, kind, payload, dedup_key, status FROM operations "
                    "WHERE payload ->> 'eval_run_id' = :r"
                ),
                {"r": str(eval_run_id)},
            )
        ).one()


async def canaries(engine: AsyncEngine, *made: CaseRef) -> None:
    async with engine.begin() as conn:
        for case in made:
            await golden.set_flags(conn, case.id, active=None, is_canary=True, now=utcnow())


def answer(fake_llm: FakeLLM, case: CaseRef, raised: tuple[str, ...] = ()) -> None:
    """The case's generator answer: every criterion at level 2, those of `raised` at 3."""
    body: dict[str, Any] = copy.deepcopy(
        eval_generator_body("canary", synthetic_ad().description_text, score_profile(), level=2)
    )
    for criterion in body["criteria"]:
        if criterion["criterion"] in raised:
            criterion["level"] = 3
    fake_llm.answer("generate", case.job_revision_id, body)
    for round_ in (1, 2):
        fake_llm.answer("revise", case.job_revision_id, body, round=round_)


async def canary_run(engine: AsyncEngine, fake_llm: FakeLLM, epoch: int) -> Any:
    """One `canary_daily`, then its operation executed; the ended run."""
    before = {run.id for run in await canary_runs(engine)}
    await tasks.canary_daily(context(engine, fake_llm), timestamp=0)
    [run] = [run for run in await canary_runs(engine) if run.id not in before]
    await execute(engine, (await operation_of(engine, run.id)).id, eval_deps(engine, fake_llm), epoch=epoch)
    return await run_row(engine, run.id)


async def drift_alerts(engine: AsyncEngine) -> list[Any]:
    async with engine.connect() as conn:
        return list(
            (
                await conn.execute(sa.text("SELECT * FROM alerts WHERE code = :c ORDER BY id"), {"c": DRIFT})
            ).all()
        )


async def scores(engine: AsyncEngine, eval_run_id: int) -> dict[int, Decimal]:
    return {r.golden_case_id: Decimal(r.score_mid) for r in await case_rows(engine, eval_run_id)}


async def test_canary_daily_starts_a_canary_run_of_the_active_configuration(
    engine: AsyncEngine, fake_llm: FakeLLM, queue_app: procrastinate.App
) -> None:
    """L4632: with a tuple active and an active canary, one `eval_runs` row (`kind = 'canary'`, production,
    the active rubric, prompt schema, model policy, evaluation policy and execution config, on the worker's
    transport) and its `eval_run` operation, deferred in the same transaction; nothing otherwise."""
    made = await insert_cases(engine, 3)
    await tasks.canary_daily(context(engine, fake_llm), timestamp=0)  # no tuple active
    await canaries(engine, made[0])
    await tasks.canary_daily(context(engine, fake_llm), timestamp=0)  # still none
    async with engine.begin() as conn:
        await activate_score_tuple(conn)
        await golden.set_flags(conn, made[0].id, active=None, is_canary=False, now=utcnow())
    await tasks.canary_daily(context(engine, fake_llm), timestamp=0)  # a tuple, but no canary
    async with engine.begin() as conn:
        await conn.execute(
            sa.text("UPDATE golden_cases SET is_canary = true, active = false WHERE id = :g"),
            {"g": made[1].id},
        )
    await tasks.canary_daily(context(engine, fake_llm), timestamp=0)  # an inactive canary is none
    assert await canary_runs(engine) == []

    await canaries(engine, made[2])
    await tasks.canary_daily(context(engine, fake_llm), timestamp=0)
    [run] = await canary_runs(engine)
    async with engine.connect() as conn:
        active = (
            await conn.execute(
                sa.text(
                    "SELECT rubric_version_id, prompt_schema_version_id, model_policy_id, "
                    "evaluation_policy_id, execution_config_id FROM active_config"
                )
            )
        ).one()
    assert (run.kind, run.route, run.status, run.repetitions, run.metrics) == (
        "canary",
        "production",
        "running",
        1,
        {"transport_requested": "fake"},
    )
    assert (
        run.rubric_version_id,
        run.prompt_schema_version_id,
        run.model_policy_id,
        run.evaluation_policy_id,
        run.execution_config_id,
    ) == tuple(active)
    operation = await operation_of(engine, run.id)
    assert (operation.kind, operation.payload, operation.dedup_key, operation.status) == (
        "eval_run",
        {"eval_run_id": run.id},
        f"eval_run:{run.id}",
        "pending",
    )
    assert await procrastinate_job_exists_for(engine, operation.id)  # deferred with the insert


def test_canary_daily_is_periodic_on_the_tick_queue_with_its_queueing_lock() -> None:
    """L285: `41 6 * * *` on the `tick` queue, one waiting job (`queueing_lock = "canary_daily"`)."""
    _reset_task_app()
    try:
        periodic = make_queue_app("dbname=unused").periodic_registry.periodic_tasks
        found = [
            (task.cron, task.task.queue, task.task.queueing_lock)
            for (name, _), task in periodic.items()
            if name == job_task_name("canary_daily")
        ]
        assert found == [("41 6 * * *", "tick", "canary_daily")]
    finally:
        _reset_task_app()


async def test_a_canary_beyond_eight_points_from_its_first_score_raises_canary_drift(
    engine: AsyncEngine, fake_llm: FakeLLM, current_epoch: int, queue_app: procrastinate.App
) -> None:
    made = await insert_cases(engine, 3)
    async with engine.begin() as conn:
        await activate_score_tuple(conn)
    await canaries(engine, made[0], made[1])
    await fake_eval_answers(engine, fake_llm)  # the critic's answers
    drifting, steady = made[0], made[1]
    for case in (drifting, steady):
        answer(fake_llm, case)

    first = await canary_run(engine, fake_llm, current_epoch)  # sets the baselines
    assert (first.status, first.passed, first.metrics["gate"]) == ("completed", None, "uncalibrated")
    assert sorted(await scores(engine, first.id)) == [drifting.id, steady.id]  # the canaries, once each
    baseline = (await scores(engine, first.id))[drifting.id]
    assert await drift_alerts(engine) == []

    answer(fake_llm, drifting, BY_8_75)
    second = await canary_run(engine, fake_llm, current_epoch)
    assert (await scores(engine, second.id))[drifting.id] - baseline == Decimal("8.75")
    [alert] = await drift_alerts(engine)
    assert (alert.severity, alert.dedup_key, alert.occurrences) == ("warn", f"{DRIFT}:{drifting.id}", 1)
    assert alert.context == {
        "eval_run_id": second.id,
        "golden_case_id": drifting.id,
        "score_mid": str(baseline + Decimal("8.75")),
        "baseline": str(baseline),
        "delta": "8.75",
    }
    # the baseline is the first score, not the previous one: the same answer again drifts again
    await canary_run(engine, fake_llm, current_epoch)
    assert [a.occurrences for a in await drift_alerts(engine)] == [2]
    answer(fake_llm, drifting, BY_7_5)  # 7.5 from the first score: no drift
    fourth = await canary_run(engine, fake_llm, current_epoch)
    assert (await scores(engine, fourth.id))[drifting.id] - baseline == Decimal("7.5")
    assert [a.occurrences for a in await drift_alerts(engine)] == [2]

    # another evaluation policy is another key: its first canary score is its own baseline
    answer(fake_llm, drifting, BY_8_75)
    async with engine.begin() as conn:
        await activate_test_policy(conn, t2_band=4)
    await canary_run(engine, fake_llm, current_epoch)
    assert [a.occurrences for a in await drift_alerts(engine)] == [2]
