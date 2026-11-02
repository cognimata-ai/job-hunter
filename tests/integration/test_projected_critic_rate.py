"""The projected critic rate of a new evaluation policy (spec §11.7.4 L4480; plan Task 20, M2 Deviation 16
closed, B R9, Deviation 29): `evaluation_policy.replay_projected_critic_rate`, stored by
`seed.ensure_evaluation_policy` when the row is created.

The replay reads the round-0 drafts of the last 30 days that were validated (their `provisional` holds the
figures), at least 50 of them, and counts those whose stored figures would fire any trigger under the new
thresholds with `critic_rounds = 0`: the policy-free triggers as stored (T1, T4, T5, T6, T7), T2 and T3 from
the stored provisional, T8 from the target's sample key; never T0. Every datum is invented.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

import pytest
import sqlalchemy as sa
from jobhunter.db.seed import DEFAULT_PROJECTED_CRITIC_RATE, ensure_evaluation_policy, load_settings_v1
from jobhunter.scoring import eval_runner
from jobhunter.scoring.evaluation_policy import EvaluationPolicy, replay_projected_critic_rate
from jobhunter.settings import dryrun
from jobhunter.settings.derive import derived, ensure_derived
from jobhunter.settings.model import Settings
from jobhunter.settings.save import SaveRequest, save_settings
from jobhunter.types import JSONValue
from jobhunter.worker.tasks import defer_in_tx
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.m3 import insert_attempt, insert_id, insert_job, insert_scoring_call, insert_target
from tests.support.scoring import score_llm_tuple

pytestmark = pytest.mark.integration

NOW: Final = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
# 60 validated round-0 drafts of the last 30 days: 15 near tau (T2 under any band), 15 at 72 that an older,
# wider policy stored with T2, 10 with the policy-free T6, 19 with nothing and one with only T0 (a resumed
# validation after a critic round: never replayed)
SIXTY: Final = [
    *[("62", ["T2"])] * 15,
    *[("72", ["T2"])] * 15,
    *[("30", ["T6"])] * 10,
    *[("30", [])] * 19,
    ("30", ["T0"]),
]


def evaluation(**fields: JSONValue) -> dict[str, JSONValue]:
    """The seeded evaluation block with `fields`; T8 off unless a test sets it."""
    block = load_settings_v1()["evaluation"]
    assert isinstance(block, dict)
    return {**block, "t8_sample_pct": 0, **fields}


async def drafts(
    engine: AsyncEngine,
    specs: list[tuple[str, list[str]]],
    *,
    round_: int = 0,
    age: timedelta = timedelta(days=1),
    validated: bool = True,
) -> None:
    """One draft per `(mid, stored triggers)` of `specs`, each on an attempt of its own of one new target,
    created `age` before `NOW`: `[mid - 5, mid + 5]` at coverage 0.9 (no straddle); an unvalidated draft keeps
    the insert's empty `provisional` (`scoring.drafts.INSERT_DRAFT`)."""
    async with engine.begin() as conn:
        target = await insert_target(conn, await insert_job(conn), await score_llm_tuple(conn))
        for attempt_no, (mid, triggers) in enumerate(specs, start=1):
            attempt = await insert_attempt(conn, target, attempt_no=attempt_no, outcome="published_accepted")
            _, call = await insert_scoring_call(conn, attempt_id=attempt, target_id=target)
            provisional: dict[str, Any] = {}
            if validated:
                provisional = {
                    "low": str(Decimal(mid) - 5),
                    "mid": mid,
                    "high": str(Decimal(mid) + 5),
                    "coverage": "0.9",
                    "confidence": "high",
                    "caps_applied": [],
                    "knockouts": [],
                }
            await insert_id(
                conn,
                "score_drafts",
                {
                    "attempt_id": attempt,
                    "round": round_,
                    "llm_call_id": call,
                    "output_json": {},
                    "validation": {"current": {}} if validated else {},
                    "provisional": provisional,
                    "triggers": triggers,
                    "created_at": NOW - age,
                },
            )


async def rate_of(engine: AsyncEngine, policy_id: int) -> Decimal:
    async with engine.connect() as conn:
        return Decimal(
            (
                await conn.execute(
                    sa.text("SELECT projected_critic_rate FROM evaluation_policies WHERE id = :i"),
                    {"i": policy_id},
                )
            ).scalar_one()
        )


async def created(engine: AsyncEngine, payload: dict[str, JSONValue]) -> int:
    async with engine.begin() as conn:
        return await ensure_evaluation_policy(conn, payload, now=NOW)


async def replayed(engine: AsyncEngine, payload: dict[str, JSONValue]) -> Decimal | None:
    async with engine.connect() as conn:
        return await replay_projected_critic_rate(conn, EvaluationPolicy.from_payload(payload), now=NOW)


async def test_sixty_round_0_drafts_give_a_replayed_rate_for_a_new_policy(engine: AsyncEngine) -> None:
    await drafts(engine, SIXTY)
    await drafts(engine, [("62", ["T2"])] * 5, age=timedelta(days=31))  # older than 30 days
    await drafts(engine, [("62", ["T2"])] * 5, round_=1)  # a revision
    await drafts(engine, [("62", ["T2"])] * 3, validated=False)  # never validated: no figures
    wide = evaluation(t2_band=15)  # 62 and 72 both within 15 of tau = 60
    assert await replayed(engine, wide) == Decimal("0.667")  # (15 + 15 + 10) / 60
    # the stored T2 of the drafts at 72 is not replayed: under the seeded band of 5 only 62 is near tau
    assert await replayed(engine, evaluation()) == Decimal("0.417")  # (15 + 10) / 60
    assert await replayed(engine, evaluation(t8_sample_pct=100)) == Decimal("1.000")  # every key sampled
    assert await replayed(engine, evaluation(t3_min_mid=70)) == Decimal("0.667")  # T3 from the stored mid
    policy_id = await created(engine, wide)
    assert await rate_of(engine, policy_id) == Decimal("0.667")  # stored at the row's creation


@pytest.mark.parametrize(("n", "rate"), [(49, DEFAULT_PROJECTED_CRITIC_RATE), (50, Decimal("1.000"))])
async def test_fewer_than_fifty_drafts_give_the_central_estimate(
    engine: AsyncEngine, n: int, rate: Decimal
) -> None:
    await drafts(engine, [("62", ["T2"])] * n)
    payload = evaluation(t2_band=9)
    assert await replayed(engine, payload) == (None if n < 50 else rate)
    assert await rate_of(engine, await created(engine, payload)) == rate
    assert DEFAULT_PROJECTED_CRITIC_RATE == Decimal("0.450")


async def test_an_identical_payload_reuses_its_row_and_its_rate(engine: AsyncEngine) -> None:
    """B R9, L4480 "computed when a policy row is created": the rate is fixed at the creation."""
    await drafts(engine, SIXTY)
    payload = evaluation(t2_band=15)
    first = await created(engine, payload)
    await drafts(engine, [("30", [])] * 60)  # the history changes: a replay now would give 0.333
    assert await replayed(engine, payload) == Decimal("0.333")
    assert await created(engine, payload) == first
    assert await rate_of(engine, first) == Decimal("0.667")
    async with engine.begin() as conn:  # the migrate seed's call (no clock): finds the row as well
        assert await ensure_evaluation_policy(conn, payload, now=None) == first


async def test_the_save_the_staging_and_the_dry_run_replay_a_new_policy(engine: AsyncEngine) -> None:
    await drafts(engine, SIXTY)
    doc = load_settings_v1()
    doc["evaluation"] = evaluation(t2_band=15)
    settings = Settings.model_validate(doc)
    async with engine.connect() as conn:  # the preview projects the rate the new row would get
        assert await dryrun.projected_critic_rate(conn, settings, now=NOW) == Decimal("0.667")
        result = await dryrun.dry_run(conn, doc, active=Settings.model_validate(load_settings_v1()), now=NOW)
    assert result.projections["critic_rate"] == "0.667"
    async with engine.begin() as conn:
        staged = await eval_runner.stage_candidate(conn, doc, now=NOW)
    assert await rate_of(engine, staged.evaluation_policy_id) == Decimal("0.667")

    other = load_settings_v1()
    other["evaluation"] = evaluation(t2_band=5, t3_min_mid=70)
    async with engine.begin() as conn:
        base = (await conn.execute(sa.text("SELECT settings_version_id FROM active_config"))).scalar_one()
        await save_settings(
            conn,
            SaveRequest(payload=other, base_version_id=int(base), acknowledged_warnings=frozenset()),
            now=NOW,
            defer=defer_in_tx,
            internal=True,
        )
        active = (await conn.execute(sa.text("SELECT evaluation_policy_id FROM active_config"))).scalar_one()
        assert (
            await ensure_derived(conn, derived(Settings.model_validate(other)), now=NOW)
        ).evaluation_policy_id == active
    assert await rate_of(engine, int(active)) == Decimal("0.667")  # (15 near tau + 15 at T3 + 10) / 60
