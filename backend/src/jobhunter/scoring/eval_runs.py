"""The eval-run list (spec §11.13, §14.2; plan M4 Task 14, D9): the golden runs or the schema dry-runs, newest
first, with the gate's figures each holds, for `GET /api/eval-runs`. Runs start from the CLI only (`jobhunter
eval`, `jobhunter schema-dry-run`); this module reads.

- `list_eval_runs(conn, *, kind, after_id, limit)`: the runs of `kind` by id descending, below `after_id` when
  given (the keyset of the route's signed cursor), at most `limit`, plain reads. From `metrics` each row takes
  what a golden run's calibration merges (`scoring.calibration.metrics_json`): `gate`, `spearman_rho`,
  `pairwise_agreement` (as `pairwise`), `contested_rate` and `failed_gates`. A run that has not been
  calibrated (a running or crashed one, a schema dry-run) has none of them: `None`, and no failed gate.
- `active_gate(conn)`: the eval gate of the active configuration (`validate.eval_gate_state`) and the number
  of the user's cases it counts (`validate.gate_cases`: active, not adversarial).
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Final, Literal, get_args

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q
from jobhunter.settings.validate import GateState, eval_gate_state, gate_cases
from jobhunter.types import JSONValue

EvalRunKind = Literal["golden", "schema_dry_run"]  # a canary run is not listed
EvalRoute = Literal["production", "forced_critic"]
EvalStatus = Literal["running", "completed", "failed"]
_GATE_STATES: Final[tuple[GateState, ...]] = get_args(GateState)

_RUNS: Final = q(
    "SELECT id, kind, route, status, passed, metrics ->> 'gate' AS gate, "
    "metrics ->> 'spearman_rho' AS spearman_rho, metrics ->> 'pairwise_agreement' AS pairwise, "
    "metrics ->> 'contested_rate' AS contested_rate, metrics -> 'failed_gates' AS failed_gates, cost_usd, "
    "started_at, finished_at, rubric_version_id, prompt_schema_version_id FROM eval_runs "
    "WHERE kind = :kind AND (CAST(:after AS bigint) IS NULL OR id < CAST(:after AS bigint)) "
    "ORDER BY id DESC LIMIT :limit"
)


@dataclass(frozen=True)
class EvalRunRow:
    """One run as the list shows it (module docstring)."""

    id: int
    kind: EvalRunKind
    route: EvalRoute
    status: EvalStatus
    passed: bool | None
    gate: GateState | None
    spearman_rho: Decimal | None
    pairwise: Decimal | None
    contested_rate: Decimal | None
    failed_gates: list[str]
    cost_usd: Decimal
    started_at: datetime
    finished_at: datetime | None
    rubric_version_id: int | None
    prompt_schema_version_id: int | None


@dataclass(frozen=True)
class ActiveGate:
    """The active configuration's eval gate and the cases it counts."""

    state: GateState
    cases: int


def _decimal(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _gate(value: str | None) -> GateState | None:
    for state in _GATE_STATES:
        if value == state:
            return state
    return None


def _names(value: JSONValue) -> list[str]:
    return [name for name in value if isinstance(name, str)] if isinstance(value, list) else []


async def list_eval_runs(
    conn: AsyncConnection, *, kind: EvalRunKind, after_id: int | None, limit: int
) -> list[EvalRunRow]:
    """The runs of `kind`, newest first by id, below `after_id` when given, at most `limit` (plain reads)."""
    rows = await conn.execute(_RUNS, {"kind": kind, "after": after_id, "limit": limit})
    return [
        EvalRunRow(
            id=int(row.id),
            kind=kind,
            route=row.route,
            status=row.status,
            passed=row.passed,
            gate=_gate(row.gate),
            spearman_rho=_decimal(row.spearman_rho),
            pairwise=_decimal(row.pairwise),
            contested_rate=_decimal(row.contested_rate),
            failed_gates=_names(row.failed_gates),
            cost_usd=Decimal(row.cost_usd),
            started_at=row.started_at,
            finished_at=row.finished_at,
            rubric_version_id=row.rubric_version_id,
            prompt_schema_version_id=row.prompt_schema_version_id,
        )
        for row in rows
    ]


async def active_gate(conn: AsyncConnection) -> ActiveGate:
    """The active configuration's eval gate and the number of the user's cases it counts (plain reads)."""
    return ActiveGate(state=await eval_gate_state(conn), cases=await gate_cases(conn))
