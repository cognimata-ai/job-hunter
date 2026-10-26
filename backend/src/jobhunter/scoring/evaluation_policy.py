"""The evaluation policy (spec §11.7.4, L4466-4477): the thresholds the critic triggers read (§11.7.1), one
immutable `evaluation_policies` row, recorded on every attempt and result and used throughout an attempt.

`EvaluationPolicy` holds the six fields of a policy payload, the settings model's `Evaluation` group (§13.1):
`from_payload` validates a payload with it, so a value outside its range, a float rate, an unknown key or a
light trigger outside `{T2, T3, T8}` is a `pydantic.ValidationError`. A light set can therefore never name a
mandatory trigger (T0, T1, T4, T5, T6, T7), which no policy can relax. `load` reads a row's payload by id.

`T4_MIN_MID` is T4's midpoint bound, the literal 60 of L4408: not a policy field, and not the relevance
threshold (A3 G14, Deviation 25).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Self

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q
from jobhunter.settings.model import Evaluation, LightTrigger
from jobhunter.types import JSONValue, get_object

T4_MIN_MID: Final = 60

_POLICY: Final = q("SELECT payload FROM evaluation_policies WHERE id = :id")


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
