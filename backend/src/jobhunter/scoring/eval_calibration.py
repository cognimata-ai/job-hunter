"""An eval run's calibration I/O and the daily canary start (spec §11.13 L4614-4632, §17.6 L5618; plan Task
20, Rulings P17, P33, T20-b; fix round 1 item 5, moved out of `scoring.eval_runner`).

- `run_cases(conn, eval_run_id)`: the run's case repetitions (`RunCase`: the `calibration.CaseResult` of
  each `eval_case_results` row with the counted cost of its calls, its golden case as a `calibration.CaseRow`
  with the similarity of the target its label was taken from, and the case's source), by case and repetition;
  the calibration and the CLI's case table read it.
- `calibrate(conn, …)`: the run's calibration in its terminal transaction: `calibration.metrics` over its
  case rows against the candidate's projected critic rate, its model policy's §21.2 central cost and the
  trailing 7-day similarity-lane cut-off (`CUTOFF_WINDOW`; the lowest similarity the lane admitted),
  `calibration.gate` over the user's active cases that the run evaluated (`validate.run_gate_cases`, Ruling
  F1-a), and `passed` (a golden run only, `NULL` while uncalibrated).
- `raise_alerts(conn, …)`: after the run's `completed`, `eval.gate_failed` (warn) for a production golden
  run that failed, its candidate the subject, and `scoring.canary_drift` (warn) for each canary of a canary
  run more than 8 points from its first score under its key (`calibration.canary_drift`), its case the
  subject; the alert of every other canary the run scored is resolved, its next canary being within bounds
  (L5673, M5 Task 3, O-ops O17). A scored canary always has a baseline here, since the run counts as
  `completed` (a first run under a key is its own), so a scored canary that did not drift lies within bounds;
  one with a failed repetition and no scored one is judged neither way.
- `start_canary_run(conn, …)`: the `canary_daily` task's work, a canary run of the active configuration
  under `active_config FOR SHARE` when a tuple is active and an active canary exists, and nothing otherwise.

Plain reads and the alerts' writes, in the caller's transaction; nothing logs content.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Final, get_args

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q
from jobhunter.observability.alerts import raise_alert, resolve_alert
from jobhunter.observability.logging import get_logger
from jobhunter.scoring import calibration
from jobhunter.settings.validate import GateState, run_gate_cases
from jobhunter.types import JSONValue
from jobhunter.worker.tasks import Deferrer

if TYPE_CHECKING:
    # Only for annotations: the runner imports this module for its terminal transaction.
    from jobhunter.scoring.eval_runner import Candidate, StartedRun, Transport

log = get_logger("jobhunter.scoring.eval_calibration")


# ---- the calibration ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RunCase:
    """A case repetition of a run as the calibration and the CLI's case table read it: its result, its case
    and the case's source."""

    result: calibration.CaseResult
    case: calibration.CaseRow
    source: str


# Each case row of a run with its golden case, the similarity of the target its label was taken from, and the
# counted cost of its calls (as `EVAL_RUN_COST_SQL` counts a run's).
_RUN_CASES: Final = q(
    "SELECT e.golden_case_id, e.repetition, e.outcome, e.critic_ran, e.score_mid, e.output, g.label, "
    "g.expected_knockouts, g.profile_version_id, g.source, t.similarity, ("
    "SELECT coalesce(sum(CASE l.state WHEN 'billed' THEN l.settled_usd WHEN 'not_billed' THEN 0 "
    "ELSE l.reserved_usd END), 0) FROM llm_calls c JOIN llm_spend_ledger l ON l.id = c.spend_ledger_id "
    "WHERE c.id = ANY(e.llm_call_ids)) AS cost_usd "
    "FROM eval_case_results e JOIN golden_cases g ON g.id = e.golden_case_id "
    "LEFT JOIN llm_results res ON res.id = g.labelled_from_result_id "
    "LEFT JOIN score_targets t ON t.id = res.target_id "
    "WHERE e.eval_run_id = :r ORDER BY e.golden_case_id, e.repetition"
)
# The trailing similarity-lane cut-off (L4628): the lowest similarity the lane admitted in the window.
_SIMILARITY_CUTOFF: Final = q(
    "SELECT min(t.similarity) FROM score_attempts a JOIN score_targets t ON t.id = a.target_id "
    "WHERE a.lane = 'similarity' AND a.started_at > :since AND a.started_at <= :now "
    "AND t.similarity IS NOT NULL"
)
CUTOFF_WINDOW: Final = timedelta(days=7)
GATE_FAILED_ALERT: Final = "eval.gate_failed"
CANARY_DRIFT_ALERT: Final = "scoring.canary_drift"
# The canaries a canary run scored (a numeric score in at least one repetition).
_SCORED_CANARIES: Final = q(
    "SELECT DISTINCT c.golden_case_id FROM eval_case_results c JOIN eval_runs r ON r.id = c.eval_run_id "
    "WHERE c.eval_run_id = :r AND r.kind = 'canary' AND c.score_mid IS NOT NULL ORDER BY c.golden_case_id"
)


async def run_cases(conn: AsyncConnection, eval_run_id: int) -> list[RunCase]:
    """The run's case repetitions by case and repetition (plain reads)."""
    found: list[RunCase] = []
    for row in await conn.execute(_RUN_CASES, {"r": eval_run_id}):
        if row.outcome not in get_args(calibration.Outcome) or row.label not in calibration.ORDINAL:
            raise ValueError(f"eval run {eval_run_id} holds a case row the calibration cannot read")
        result = calibration.case_result(
            golden_case_id=int(row.golden_case_id),
            repetition=int(row.repetition),
            outcome=row.outcome,
            critic_ran=bool(row.critic_ran),
            score_mid=None if row.score_mid is None else Decimal(row.score_mid),
            output=row.output,
            cost_usd=Decimal(row.cost_usd),
        )
        case = calibration.CaseRow(
            label=row.label,
            expected_knockouts=tuple(row.expected_knockouts),
            profile_version_id=int(row.profile_version_id),
            similarity=None if row.similarity is None else Decimal(str(row.similarity)),
        )
        found.append(RunCase(result, case, str(row.source)))
    return found


@dataclass(frozen=True)
class Calibrated:
    """A run's calibration: the gate's state, the metrics its `eval_runs.metrics` merges and its `passed`."""

    state: GateState
    metrics: dict[str, JSONValue]
    passed: bool | None


async def calibrate(
    conn: AsyncConnection,
    *,
    eval_run_id: int,
    kind: str,
    projected_critic_rate: Decimal,
    central_cost_usd: Decimal,
    now: datetime,
) -> Calibrated:
    """Run `eval_run_id`'s calibration and gate in its terminal transaction (module docstring): the gate
    counts the user's cases (Ruling T20-b) that the run evaluated and that are still active
    (`validate.run_gate_cases`, Ruling F1-a); the metrics pool every case."""
    rows = await run_cases(conn, eval_run_id)
    cutoff = (
        await conn.execute(_SIMILARITY_CUTOFF, {"since": now - CUTOFF_WINDOW, "now": now})
    ).scalar_one_or_none()
    m = calibration.metrics(
        [r.result for r in rows],
        cases={r.result.golden_case_id: r.case for r in rows},
        projected_critic_rate=projected_critic_rate,
        central_cost_usd=central_cost_usd,
        similarity_cutoff=None if cutoff is None else Decimal(str(cutoff)),
    )
    state = calibration.gate(m, active_cases=await run_gate_cases(conn, eval_run_id))
    passed = None if kind != "golden" or state == "uncalibrated" else state == "passed"
    return Calibrated(state, calibration.metrics_json(m, state=state), passed)


async def raise_alerts(
    conn: AsyncConnection,
    *,
    eval_run_id: int,
    kind: str,
    route: str,
    candidate: "Candidate",
    calibrated: Calibrated,
) -> None:
    """Run `eval_run_id`'s alerts, in its terminal transaction: `eval.gate_failed` for a gated golden run that
    failed (L5618; a forced-critic run is a diagnostic, L4612), `scoring.canary_drift` for each drifted canary
    of a canary run (L4632), resolved for each other canary it scored (L5673). Ids and numbers only."""
    if kind == "golden" and route == "production" and calibrated.passed is False:
        c = candidate
        failed = calibrated.metrics.get("failed_gates")
        names = [str(name) for name in failed] if isinstance(failed, list) else []
        await raise_alert(
            conn,
            code=GATE_FAILED_ALERT,
            severity="warn",
            subject=f"{c.rubric_version_id}:{c.prompt_schema_version_id}:{c.model_policy_id}:"
            f"{c.evaluation_policy_id}",
            message=f"eval run {eval_run_id} failed the golden-set gate: {', '.join(names)}",
            context={"eval_run_id": eval_run_id, "failed_gates": list(names)},
        )
    if kind == "canary":
        drifts = await calibration.canary_drift(conn, eval_run_id=eval_run_id)
        for drift in drifts:
            await raise_alert(
                conn,
                code=CANARY_DRIFT_ALERT,
                severity="warn",
                subject=str(drift.golden_case_id),
                message=f"canary {drift.golden_case_id} scored {drift.score_mid} against its baseline "
                f"{drift.baseline} in eval run {eval_run_id}",
                context={
                    "eval_run_id": eval_run_id,
                    "golden_case_id": drift.golden_case_id,
                    "score_mid": str(drift.score_mid),
                    "baseline": str(drift.baseline),
                    "delta": str(drift.delta),
                },
            )
            log.warning(
                "eval_canary_drift",
                eval_run_id=eval_run_id,
                golden_case_id=drift.golden_case_id,
                delta=str(drift.delta),
            )
        drifted = {drift.golden_case_id for drift in drifts}
        for case_id in (await conn.execute(_SCORED_CANARIES, {"r": eval_run_id})).scalars().all():
            if int(case_id) not in drifted:  # its next canary lies within bounds
                await resolve_alert(conn, code=CANARY_DRIFT_ALERT, subject=str(case_id))


# ---- the canary start --------------------------------------------------------------------------------------


# The canary start's first lock (§9.1, step 2): the active configuration it runs, read under `FOR SHARE`.
_ACTIVE_FOR_CANARY: Final = q(
    "SELECT llm_tuple_id, rubric_version_id, prompt_schema_version_id, model_policy_id, "
    "evaluation_policy_id, execution_config_id FROM active_config WHERE id = 1 FOR SHARE"
)
_ACTIVE_CANARY: Final = q("SELECT EXISTS (SELECT 1 FROM golden_cases WHERE active AND is_canary)")


async def start_canary_run(
    conn: AsyncConnection, *, transport: "Transport", now: datetime, defer: Deferrer | None
) -> "StartedRun | None":
    """The `canary_daily` task's work (§11.13 L4632, M3 Task 20), in the caller's transaction: with a tuple
    active and at least one active canary, a canary run of the active configuration on the production route
    and its operation (`start_run`, the worker's `transport`); `None` otherwise, writing nothing. The runner
    scores each canary under its own profile and scope, and its terminal transaction raises
    `scoring.canary_drift` (`raise_alerts`)."""
    # Imported here: the runner imports this module for its terminal transaction.
    from jobhunter.scoring.eval_runner import Candidate, NoCandidate, start_run

    row = (await conn.execute(_ACTIVE_FOR_CANARY)).one()
    if row.llm_tuple_id is None or not (await conn.execute(_ACTIVE_CANARY)).scalar_one():
        log.info("eval_canary_skipped", tuple_active=row.llm_tuple_id is not None)
        return None
    if row.rubric_version_id is None or row.prompt_schema_version_id is None:
        raise NoCandidate("active_config has no rubric or prompt-schema version (migrate seeds both)")
    candidate = Candidate(
        int(row.rubric_version_id),
        int(row.prompt_schema_version_id),
        int(row.model_policy_id),
        int(row.evaluation_policy_id),
        int(row.execution_config_id),
    )
    return await start_run(
        conn,
        kind="canary",
        route="production",
        candidate=candidate,
        transport_requested=transport,
        now=now,
        defer=defer,
    )
