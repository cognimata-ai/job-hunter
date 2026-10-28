"""The evaluation policy (spec §11.7.4, L4466-4477): the thresholds the critic triggers read (§11.7.1), one
immutable `evaluation_policies` row, recorded on every attempt and result and used throughout an attempt.

`EvaluationPolicy` holds the six fields of a policy payload, the settings model's `Evaluation` group (§13.1):
`from_payload` validates a payload with it, so a value outside its range, a float rate, an unknown key or a
light trigger outside `{T2, T3, T8}` is a `pydantic.ValidationError`. A light set can therefore never name a
mandatory trigger (T0, T1, T4, T5, T6, T7), which no policy can relax. `load` reads a row's payload by id.

`T4_MIN_MID` is T4's midpoint bound, the literal 60 of L4408: not a policy field, and not the relevance
threshold (A3 G14, Deviation 25).

**The projected critic rate** (§11.7.4 L4480; M3 Task 20, M2 Deviation 16 closed):
`replay_projected_critic_rate(conn, policy, now=…)` replays `policy`'s thresholds over the round-0 drafts of
the `REPLAY_DAYS` days before `now` that were validated (their `provisional` holds the figures): the share of
them whose stored figures fire any trigger (`triggers.replay`, `critic_rounds = 0`), rounded half up to the
column's 3 places; `None` below `REPLAY_MIN_DRAFTS` drafts, where the caller keeps the §21.2 central estimate
(`seed.ensure_evaluation_policy`, which stores the rate when it creates the row: an identical payload later
finds the row and its rate, B R9). Plain reads, no lock: the drafts are immutable once validated.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Final, Self

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q
from jobhunter.settings.model import Evaluation, LightTrigger
from jobhunter.types import JSONValue, get_object

T4_MIN_MID: Final = 60
REPLAY_DAYS: Final = 30
REPLAY_MIN_DRAFTS: Final = 50
_RATE_QUANTUM: Final = Decimal("0.001")  # evaluation_policies.projected_critic_rate is numeric(4,3)

_POLICY: Final = q("SELECT payload FROM evaluation_policies WHERE id = :id")
# The validated round-0 drafts of the window, with the target's T8 key: a draft the validation never reached
# keeps the insert's empty `provisional` (`scoring.drafts.INSERT_DRAFT`).
_ROUND_0_DRAFTS: Final = q(
    "SELECT d.provisional, d.triggers, a.target_id, t.llm_tuple_id FROM score_drafts d "
    "JOIN score_attempts a ON a.id = d.attempt_id JOIN score_targets t ON t.id = a.target_id "
    "WHERE d.round = 0 AND d.created_at > :since AND d.created_at <= :now "
    "AND d.provisional -> 'coverage' IS NOT NULL"
)


@dataclass(frozen=True)
class EvaluationPolicy:
    """The routing thresholds of one evaluation policy (§11.7.4)."""

    relevance_threshold: int  # τ, T2's centre
    t2_band: int
    t2_straddle_max_coverage: Decimal
    t3_min_mid: int
    t8_sample_pct: int
    critic_light_triggers: frozenset[LightTrigger]

    @classmethod
    def from_payload(cls, payload: Mapping[str, JSONValue]) -> Self:
        """The policy of an `evaluation_policies.payload` (or a settings document's `evaluation` block); a key
        left out takes its §13.1 default. `pydantic.ValidationError` for a payload the settings model
        refuses."""
        evaluation = Evaluation.model_validate(dict(payload))
        return cls(
            relevance_threshold=evaluation.relevance_threshold,
            t2_band=evaluation.t2_band,
            t2_straddle_max_coverage=evaluation.t2_straddle_max_coverage,
            t3_min_mid=evaluation.t3_min_mid,
            t8_sample_pct=evaluation.t8_sample_pct,
            critic_light_triggers=frozenset(evaluation.critic_light_triggers),
        )


async def load(conn: AsyncConnection, policy_id: int) -> EvaluationPolicy:
    """The policy of the `evaluation_policies` row `policy_id` (an immutable row: no lock); `LookupError` when
    there is none."""
    row = (await conn.execute(_POLICY, {"id": policy_id})).first()
    if row is None:
        raise LookupError(f"evaluation policy {policy_id} does not exist")
    return EvaluationPolicy.from_payload(get_object(row.payload, ""))


def _figure(provisional: Mapping[str, JSONValue], key: str) -> Decimal | None:
    value = provisional.get(key)
    return None if value is None else Decimal(str(value))


async def replay_projected_critic_rate(
    conn: AsyncConnection, policy: EvaluationPolicy, *, now: datetime
) -> Decimal | None:
    """`policy`'s projected critic rate replayed over the stored round-0 drafts (module docstring)."""
    from jobhunter.scoring import triggers  # imported here: `triggers` imports this module

    params = {"since": now - timedelta(days=REPLAY_DAYS), "now": now}
    rows = (await conn.execute(_ROUND_0_DRAFTS, params)).all()
    if len(rows) < REPLAY_MIN_DRAFTS:
        return None
    fired = 0
    for row in rows:
        provisional = get_object(row.provisional, "")
        coverage = _figure(provisional, "coverage")
        replayed = triggers.replay(
            stored=list(row.triggers),
            low=_figure(provisional, "low"),
            mid=_figure(provisional, "mid"),
            high=_figure(provisional, "high"),
            coverage=Decimal(0) if coverage is None else coverage,
            policy=policy,
            sample_key=triggers.sample_key(target_id=int(row.target_id), llm_tuple_id=int(row.llm_tuple_id)),
        )
        fired += bool(replayed)
    return (Decimal(fired) / Decimal(len(rows))).quantize(_RATE_QUANTUM, rounding=ROUND_HALF_UP)
