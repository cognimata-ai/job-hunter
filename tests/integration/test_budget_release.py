"""The cap reached mid-cycle and `budget_release` on PostgreSQL (spec §6.9.1, §6.9.4, L1198-1201, L5506; plan
Task 13, Review Focus 1, Codex r3 B16, r4 B8).

Five identical synthetic ads render one bound, so each first generator call reserves the same `r`, computed
before the cycle from the exact prompt (`gateway.reservation_for`) and checked against the bound formula. A
cap whose stored scoring share lies in `[3r, 4r)` admits three reservations and denies two (dispatch in id
order; every call settles at exactly its reservation, so the order of settlement and reservation does not
matter): three targets publish, two are `budget_deferred`, `llm.cap_reached` is the one open alert, the
counters agree with the ledger, and Score now answers `budget_exhausted`. A raised cap whose stored headroom
lies in `[2 r_first, 3 r_first)` lets `budget_release` return both deferred targets and resolve the alert. A
late `not_billed` settlement of a cut stream's `ambiguous` entry frees its reservation, and `budget_release`
returns exactly the `k = h // r_first` oldest deferred targets of the stored headroom `h`, with no cap
change.

Every datum is invented.
"""

import asyncio
from collections.abc import Sequence
from datetime import date
from decimal import ROUND_CEILING, Decimal
from typing import Any

import procrastinate
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db.sql import q
from jobhunter.graphs.score.nodes import rubric_of
from jobhunter.llm import budget, fake, spend_ledger
from jobhunter.llm import gateway as gateway_mod
from jobhunter.llm.bounds import text_input_bound
from jobhunter.llm.budget import month_of
from jobhunter.llm.gateway import LLMGateway, NodeConfig, RenderedPrompt, pinned_node_config
from jobhunter.llm.pricing import Model, Usage, price_for, settled_usd, worst_case_usd
from jobhunter.llm.prompts.render import (
    job_ad_block,
    load_prompt_version,
    prompt_schema_v2,
    score_prompt,
    score_schema,
)
from jobhunter.llm.schemas.profile import ConfirmedProfile
from jobhunter.llm.spend_ledger import CAP_REACHED_ALERT, CallOutcome, Reservation
from jobhunter.scoring import cycles, targets
from jobhunter.scoring.targets import ScoreNowRefused
from jobhunter.types import JSONValue, get_list, get_object, get_str
from jobhunter.worker.tasks import defer_in_tx
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.llm import FakeLLM, budget_rows, save_budget, tries
from tests.support.reconcile import run_reconciler
from tests.support.scoring import PendingTarget, cycle_of, cycle_runner, insert_targets, synthetic_c_draft

pytestmark = pytest.mark.integration

SONNET: Model = "claude-sonnet-5-5"
BUFFER = Decimal("2.00")


# ---- the exact first-stage reservation of a target's generator prompt -----------------------------------

_INPUTS = q(
    "SELECT r.llm_input, r.description_text, r.description_completeness, v.profile_json, w.weights, "
    "w.anchors, u.prompt_schema_version_id, u.model_policy_id, a.execution_config_id "
    "FROM score_targets t JOIN llm_tuples u ON u.id = t.llm_tuple_id "
    "JOIN job_revisions r ON r.id = t.job_revision_id JOIN profile_versions v ON v.id = u.profile_version_id "
    "JOIN rubric_versions w ON w.id = u.rubric_version_id CROSS JOIN active_config a "
    "WHERE t.id = :t AND a.id = 1"
)


def _strings(doc: JSONValue, path: str) -> list[str]:
    return [get_str(item, "") for item in get_list(doc, path)]


async def generate_request(engine: AsyncEngine, target: PendingTarget) -> tuple[NodeConfig, RenderedPrompt]:
    """The `generate` node a claim of `target` pins now and its first prompt, rendered as `load_inputs` and
    `generate_step` render it (the tuple's prompt-schema version, rubric and profile, the revision's ad)."""
    async with engine.connect() as conn:
        row = (await conn.execute(_INPUTS, {"t": target.target_id})).one()
        version = await load_prompt_version(conn, int(row.prompt_schema_version_id))
        node = await pinned_node_config(
            conn,
            "generate",
            model_policy_id=int(row.model_policy_id),
            execution_config_id=int(row.execution_config_id),
            schema=score_schema("generate", version),
        )
    material: JSONValue = row.llm_input
    company = get_object(material, "").get("company_display")
    ad = job_ad_block(
        title=get_str(material, "title"),
        company=company if isinstance(company, str) else None,
        locations=_strings(material, "locations"),
        work_modes=[
            (m, get_str(material, "work_mode_source")) for m in _strings(material, "work_mode_possible")
        ],
        employment_types=[
            (kind, get_str(material, "employment_source")) for kind in _strings(material, "employment_types")
        ],
        completeness=row.description_completeness,
        description_text=row.description_text,
    )
    prompt = score_prompt(
        "generate",
        version=version,
        rubric=rubric_of(row.weights, row.anchors),
        profile=ConfirmedProfile.model_validate(row.profile_json),
        ad=ad,
    )
    return node, prompt


def usage_costing(model: Model, usd: Decimal) -> dict[str, int]:
    """A response usage that settles at exactly `usd` on `model` (`pricing.settled_usd`): a few output
    tokens, the rest uncached input, the odd micro-dollar as cache reads."""
    price = price_for(model, prompt_tokens=0)
    micro = usd * 1_000_000
    output = min(200, int(micro // price.output))
    rest = micro - output * price.output
    uncached = int(rest // price.input)
    cache_read = (rest - uncached * price.input) / price.cache_read
    if cache_read != cache_read.to_integral_value():
        raise ValueError(f"{usd} cannot be settled exactly on {model}")
    usage = Usage(uncached, output, 0, int(cache_read))
    if settled_usd(model, usage) != usd:
        raise ValueError(f"{usage} settles at {settled_usd(model, usage)}, not {usd}")
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": usage.cache_read_input_tokens,
    }


def in_order(monkeypatch: pytest.MonkeyPatch, made: Sequence[PendingTarget]) -> None:
    """Each target's first reservation waits until every earlier target's first reservation was admitted or
    denied (`LLMGateway._reserve`), so the dispatch order is the targets' order whatever the scheduling."""
    turns = [t.target_id for t in made]
    passed: set[int] = set()
    changed = asyncio.Condition()
    original = gateway_mod.LLMGateway._reserve

    async def reserve(self: LLMGateway, *args: Any) -> spend_ledger.Reservation:
        ctx: gateway_mod.CallContext = args[2]
        target = ctx.target_id
        if target is None or target not in turns or target in passed:
            return await original(self, *args)
        earlier = set(turns[: turns.index(target)])
        async with changed:
            await asyncio.wait_for(changed.wait_for(lambda: earlier <= passed), 20)
        try:
            return await original(self, *args)
        finally:
            async with changed:
                passed.add(target)
                changed.notify_all()

    monkeypatch.setattr(gateway_mod.LLMGateway, "_reserve", reserve)


# ---- the stored figures ---------------------------------------------------------------------------------


def ceil_cents(usd: Decimal) -> Decimal:
    """A cap of `numeric(10,2)`: the next cent at or above `usd` (Codex r3 B16)."""
    return usd.quantize(Decimal("0.01"), rounding=ROUND_CEILING)


async def stored(engine: AsyncEngine, month: date) -> tuple[budget.BudgetRow, budget.BudgetParams]:
    (row,) = await budget_rows(engine, month)
    assert row is not None
    async with engine.connect() as conn:
        return row, await spend_ledger.current_params(conn)


async def scoring_share(engine: AsyncEngine, month: date) -> Decimal:
    """The scoring share of the stored month row: the most `counted_usd + r` may reach for scoring."""
    row, p = await stored(engine, month)
    return budget.admission_limit(row, "scoring", p)


async def scoring_headroom(engine: AsyncEngine, month: date) -> Decimal:
    row, p = await stored(engine, month)
    return budget.headroom(row, "scoring", p)


async def reserve_frac(engine: AsyncEngine) -> Decimal:
    async with engine.connect() as conn:
        return budget.reserve_frac(await spend_ledger.current_params(conn))


async def active_r_first(engine: AsyncEngine) -> Decimal:
    async with engine.connect() as conn:
        config = await cycles.scoring_config(conn)
    return budget.r_first(config.model_policy, config.execution)


async def ledger_reserved(engine: AsyncEngine, *, purpose: str) -> list[Decimal]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            sa.text("SELECT reserved_usd FROM llm_spend_ledger WHERE purpose = :p ORDER BY id"),
            {"p": purpose},
        )
        return [Decimal(r.reserved_usd) for r in rows]


async def target_states(engine: AsyncEngine, made: Sequence[PendingTarget]) -> list[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            sa.text("SELECT id, state FROM score_targets WHERE id = ANY(CAST(:ids AS bigint[]))"),
            {"ids": [t.target_id for t in made]},
        )
        found = {int(r.id): str(r.state) for r in rows}
    return [found[t.target_id] for t in made]


async def open_alerts(engine: AsyncEngine) -> list[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(sa.text("SELECT code FROM alerts WHERE resolved_at IS NULL ORDER BY id"))
        return [str(r.code) for r in rows]


async def set_cap(engine: AsyncEngine, cap: Decimal) -> None:
    await save_budget(engine, now=utcnow(), cap_usd=str(cap), overrun_buffer_usd=str(BUFFER))


async def exact_r(
    fake_llm: FakeLLM, engine: AsyncEngine, gateway: LLMGateway, made: Sequence[PendingTarget]
) -> Decimal:
    """The one first-stage reservation of the identical ads, checked against the bound formula; every target's
    generator answer settles at exactly it."""
    node, prompt = await generate_request(engine, made[0])
    r = await gateway.reservation_for(node, prompt)
    bound = text_input_bound(node.model, prompt.as_request(node.schema))
    assert r == worst_case_usd(node.model, input_tokens_bound=bound, max_tokens=node.max_tokens)
    for target in made[1:]:  # identical ads: one rendered bound for all
        assert await gateway.reservation_for(*await generate_request(engine, target)) == r
    for target in made:
        fake_llm.answer(
            "generate", target.job_revision_id, synthetic_c_draft(), usage=usage_costing(node.model, r)
        )
    return r


# ---- Review Focus 1 -------------------------------------------------------------------------------------


async def test_cap_hit_mid_cycle_defers_the_rest_and_the_counters_stay_consistent(
    engine: AsyncEngine,
    fake_llm: FakeLLM,
    current_epoch: int,
    queue_app: procrastinate.App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    made = await insert_targets(engine, 5)  # five identical synthetic ads
    gateway = fake_llm.gateway(engine)
    runner = await cycle_runner(engine, gateway)
    r = await exact_r(fake_llm, engine, gateway, made)
    r_first = await active_r_first(engine)  # the policy-level figure budget_release divides by (Task 7)
    month, frac = month_of(utcnow()), await reserve_frac(engine)
    await set_cap(engine, ceil_cents(BUFFER + (Decimal("3.5") * r) / (1 - frac)))
    share = await scoring_share(engine, month)  # read back from the stored row, not recomputed
    assert 3 * r <= share < 4 * r  # the inequality the "3 published, 2 deferred" claim rests on
    in_order(monkeypatch, made)  # dispatch in id order: three admitted, two denied
    cycle_id = await runner.start("periodic")
    assert (await cycle_of(engine, cycle_id)).admission_size >= 5  # S >= 5: all five claimed together
    await runner.run(cycle_id)
    assert sorted(await ledger_reserved(engine, purpose="generate")) == [r] * 3  # the real reservations
    states = await target_states(engine, made)
    assert states[:3] == ["published_accepted"] * 3 and states[3:] == ["budget_deferred"] * 2
    assert await open_alerts(engine) == [CAP_REACHED_ALERT]
    async with engine.connect() as conn:
        assert await spend_ledger.recompute(conn, month) is None  # the counters agree with the ledger
    async with engine.begin() as conn:
        refused = await targets.score_now(
            conn, job_id=made[4].job_id, force=False, now=utcnow(), defer=defer_in_tx
        )
    assert refused == ScoreNowRefused("budget_exhausted")
    # counted = 3r; the release divides by r_first
    await set_cap(engine, ceil_cents(BUFFER + (3 * r + Decimal("2.5") * r_first) / (1 - frac)))
    headroom = await scoring_headroom(engine, month)  # from the stored row after the three settlements
    assert 2 * r_first <= headroom < 3 * r_first and headroom // r_first == 2  # k = 2 by the stored figures
    assert await run_reconciler("budget_release", engine, now=utcnow(), epoch=current_epoch) == 2
    assert "budget_deferred" not in await target_states(engine, made) and await open_alerts(engine) == []


async def test_a_late_settlement_releases_the_oldest_deferred_target(
    engine: AsyncEngine,
    fake_llm: FakeLLM,
    current_epoch: int,
    queue_app: procrastinate.App,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Codex r3 B16: three reservations in flight, one of them a cut stream (`ambiguous`, its retry denied),
    # so two targets are `budget_deferred`; a late `not_billed` of the ambiguous entry frees its r
    made = await insert_targets(engine, 4)
    gateway = fake_llm.gateway(engine, read_timeout_s=0.5)
    runner = await cycle_runner(engine, gateway)
    r = await exact_r(fake_llm, engine, gateway, made)
    _, schemas = prompt_schema_v2()
    generator = schemas["generator"]
    assert isinstance(generator, dict)
    fake_llm.record("generate", SONNET, synthetic_c_draft(), schema=generator)  # the cut stream's recording
    cut = fake.answer_key("generate", {"job_revision_id": made[2].job_revision_id}, 0)
    fake.answer(cut, "stall_after_first_event", consume=True)  # its first try only
    r_first = await active_r_first(engine)
    month, frac = month_of(utcnow()), await reserve_frac(engine)
    cap = ceil_cents(BUFFER + (Decimal("3.5") * r) / (1 - frac))
    await set_cap(engine, cap)
    in_order(monkeypatch, made)
    await runner.run(await runner.start("periodic"))
    assert await target_states(engine, made) == ["published_accepted"] * 2 + ["budget_deferred"] * 2
    (lost,) = [t for t in await tries(engine) if t.state == "ambiguous"]
    assert lost.reserved_usd == r
    async with engine.begin() as conn:  # the provider's evidence turns up: it was never billed
        await spend_ledger.settle(
            conn,
            Reservation(lost.ledger_id, lost.call_id, lost.budget_month, r, "scoring", SONNET, False),
            outcome="not_billed",
            evidence="error_response_no_content",
            usage=None,
            call=CallOutcome(status="error", error_code="stream_interrupted"),
            now=utcnow(),
        )
    h = await scoring_headroom(engine, month)  # the stored figure after the late settlement
    assert h >= r_first  # asserted from the stored row (the fixture's r is sized so that it holds)
    k = int(h // r_first)
    async with engine.connect() as conn:
        rows = await conn.execute(
            sa.text(
                "SELECT id FROM score_targets WHERE id = ANY(CAST(:ids AS bigint[])) "
                "AND state = 'budget_deferred' ORDER BY state_changed_at, id"
            ),
            {"ids": [t.target_id for t in made]},
        )
        deferred = [int(row.id) for row in rows]
    assert 1 <= k < len(deferred)
    assert await run_reconciler("budget_release", engine, now=utcnow(), epoch=current_epoch) == k
    states = dict(zip([t.target_id for t in made], await target_states(engine, made), strict=True))
    assert [states[t] for t in deferred] == ["pending"] * k + ["budget_deferred"] * (len(deferred) - k)
    (row,) = await budget_rows(engine, month)
    assert row.cap_usd == cap  # without any cap change
