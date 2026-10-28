"""The score lane's remaining reconcilers (spec §7.17 #3, #5, §9.6.5, §11.13, §17.4, §17.6; plan M3 Task 18,
A2 S4, A2 A8, Deviation 28, Codex r3 B15, Rulings P10, P29, P32, Carries T6-d, T15-e).

Every reconciler here opens its transactions on the reconcile context's engine, each starting with
`fence_epoch(ctx.epoch)` (step 1 of the lock schedule), and lets the worker's own `FencedOut` propagate
(Ruling FR-w); another owner's ends the pass with what it counted so far. Alerts carry ids, counts and
amounts, never ad text or model output; `raise_alert` keeps the first raise's context.

**`stuck_targets`** (tick, §17.4 L5504, §7.17 #3): in one transaction, the claims whose heartbeat is older
than `STUCK_AFTER` at `ctx.now` and whose current attempt is still `running` are reclaimed by the statements
every reclaim shares (`cycles.reclaim`, kind `stuck`, with the takeover sweep `score_targets` and the orphaned
cycle's): their bulk rows first (step 7), the target rows `FOR UPDATE` in id order (step 10), `pending` while
charged attempts are left, else `failed_attempts_exhausted('stuck')` with the follow-up hold released, the
attempts `claim_lost('stale_heartbeat')` (the detail of Task 9's `reclaim_now`). A target whose attempt a
fenced publish ended `cancelled` is left for the purge's chunk (A2 S4); `in_batch` is never `in_progress`.
`scoring.stuck_target_repaired` (warn, subject the hour of `ctx.now`, `%Y-%m-%dT%H`, L5594 "aggregated
hourly") is raised in the same transaction by every pass with reclaims; a pass without one resolves the open
ones whose last raise is older than an hour (`resolve_quiet_alerts`: an hour without reclaims). After the
commit, `jobhunter_claim_reclaims_total` counts the reclaimed claims.

**`det_completeness`** (heavy, §17.4 L5511, §7.17 #5, L4568): for the active scope, then for the pending scope
while one exists, the active tuple's published results are paged by id (`det_results.published_page`), each
chunk of `DET_CHUNK` one transaction in the schedule's order: `fence_epoch`, `active_config FOR SHARE` (the
tuple and the scope pointers, read again each chunk: a changed tuple or scope starts that scope's paging over,
a cleared one ends it), the page's job rows `FOR SHARE` in id order (step 4, carry T6-d: before any result
lock), then every result of the page without a current instance for the scope whose hash and algorithm version
are current (`det_results.stale_of`, the staleness the scope apply and the live hook read) recomputed by
`det_results.recompute_instance` (the result rows `FOR UPDATE` in id order, step 10). A changed
`DET_ALGORITHM_VERSION` makes every current instance stale, so the next pass recomputes each once. It counts
the instances written.

**`admission_alerts`** (heavy, §9.6.5 L3686-3688; A2 A8) and **`scoring_drift`** (heavy, §11.13 L4633-4635,
§17.6 L5595), one transaction each: `fence_epoch`, a plain read of the active tuple, scope and evaluation
policy (they write alerts only), then each alert of the active tuple at `ctx.now`
(`scoring.feedback_disagreement` whatever the tuple). Each counts the alerts it opened or resolved. An alert
is opened once while it is open (`open_alert`), with ids and counts as its context, and every comparison is
exact on the counts:
- `admission.exploration_hits` (warn, subject `tuple:<id>`): of the tuple's results published in the 14 days
  before `now` by an attempt of the `exploration` lane (`score_attempts.lane`, the lane snapshot; never the
  target's, which a retry overwrites: Codex r3 B15), more than 25% whose current instance under the active
  scope has `score_mid` at the relevance threshold or above, with at least 10 of them; resolved at 25% or
  less;
- `admission.high_sim_expired` (warn, subject `tuple:<id>`): at least 3 targets entered `expired_unscored` in
  the 24 h before `now` with a similarity at the 70th percentile (`percentile_cont`) of the similarity of the
  targets with an attempt of the `similarity` lane started in the 7 days before `now`, or above; resolved once
  no such target expired in the last 24 h (24 h without recurrence);
- `admission.backlog_drain` (info, subject the UTC date of `now`): the tuple's `pending` targets would take
  more than the active `max_ad_age_days` to drain at the rate of the attempts started in the last 7 days
  (`pending x 7 > max_ad_age_days x admitted`; nothing admitted never drains); an earlier day's open alert is
  resolved, and the day's is opened unless one was raised that day already (`ever_raised`: at most once a
  day); resolved below the line;
- `scoring.feedback_disagreement` (warn, subject `labels`): of the `score_feedback` rows created in the 14
  days before `now`, more than 30% `poor` on a snapshot with `M` at the relevance threshold `τ` or above, or
  `good` on one with `M < τ - 20`, with at least 10 of them; resolved at 30% or less;
- `scoring.distribution_shift` (info, subject `tuple:<id>`): the mean `score_mid` of the current instances
  under the active scope of the tuple's results published in the last 7 days differs by more than 10 from that
  of the 28 days before; resolved at 10 or less; left as it is while a window has no result;
- `scoring.critic_rate_high` (warn): `cycles.evaluate_critic_rate` of the active tuple and evaluation policy,
  which every `finalize_cycle` evaluates too (Ruling P32).

**The score lane's gauges** (§17.5 L5538-5539): `metrics_refresh` (heavy) runs `refresh_score_lane_gauges`
(registered with `gauge_refresh`), a read without a lock: the active tuple's targets in each state
(`jobhunter_score_targets{state}`, every state, 0 included), the age of its oldest `pending` one by
`created_at` (`jobhunter_oldest_pending_target_age_seconds`, 0 with none) and `critic_rate_7d` under the
active evaluation policy (`jobhunter_critic_rate_7d`, NaN below 50 attempts or without a tuple).
"""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Final, Literal

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q
from jobhunter.errors import FencedOut
from jobhunter.observability.alerts import (
    ever_raised,
    open_alert,
    raise_alert,
    resolve_alerts,
    resolve_quiet_alerts,
)
from jobhunter.observability.logging import get_logger
from jobhunter.observability.metrics import (
    CLAIM_RECLAIMS,
    CRITIC_RATE_7D,
    OLDEST_PENDING_TARGET_AGE,
    SCORE_TARGET_STATES,
    SCORE_TARGETS,
)
from jobhunter.run_settings import scope_from_payload
from jobhunter.scoring import cycles, det_results, targets
from jobhunter.scoring.det_results import JobRow, PublishedInstance, ScopeRow
from jobhunter.scoring.evaluation_policy import EvaluationPolicy
from jobhunter.types import JSONValue, get_object
from jobhunter.worker.epoch import fence_epoch
from jobhunter.worker.reconcilers import ReconcileContext, gauge_refresh, reconciler

STUCK_AFTER: Final = timedelta(minutes=3)
STALE_HEARTBEAT: Final = "stale_heartbeat"
STUCK: Final = "stuck"
STUCK_ALERT: Final = "scoring.stuck_target_repaired"
STUCK_QUIET: Final = timedelta(hours=1)
HOUR_SUBJECT: Final = "%Y-%m-%dT%H"
DET_CHUNK: Final = 500  # published results per `det_completeness` transaction

# Step 2: the tuple and the two scope pointers, under `FOR SHARE`.
_DET_POINTERS: Final = q(
    "SELECT llm_tuple_id, scope_version_id, pending_scope_version_id FROM active_config WHERE id = 1 "
    "FOR SHARE"
)
_SCOPE: Final = q("SELECT payload FROM scope_versions WHERE id = :id")

log = get_logger("jobhunter.scoring.reconcilers")


@reconciler("stuck_targets", "tick")
async def reconcile_stuck_targets(ctx: ReconcileContext) -> int:
    """Reclaim the stuck claims at `ctx.now` (module docstring); how many were reclaimed."""
    try:
        async with ctx.engine.begin() as conn:
            await fence_epoch(conn, ctx.epoch)
            moved = await cycles.reclaim(
                conn,
                "stuck",
                {"cutoff": ctx.now - STUCK_AFTER},
                detail=STALE_HEARTBEAT,
                exhausted_reason=STUCK,
            )
            if moved:
                pending = sum(1 for m in moved if m.state == "pending")
                await raise_alert(
                    conn,
                    code=STUCK_ALERT,
                    severity="warn",
                    subject=ctx.now.strftime(HOUR_SUBJECT),
                    message="score target claims with a stale heartbeat were reclaimed",
                    context={"reclaimed": len(moved), "pending": pending, "exhausted": len(moved) - pending},
                )
            else:
                await resolve_quiet_alerts(conn, code=STUCK_ALERT, since=ctx.now - STUCK_QUIET)
    except FencedOut as fenced:
        if fenced.worker_superseded:
            raise  # `fence_epoch` logged it
        return 0
    if moved:
        CLAIM_RECLAIMS.inc(len(moved))
        log.warning("stuck_targets_reclaimed", reclaimed=len(moved))  # `reclaim` logged the ids
    return len(moved)


# ---- det_completeness -------------------------------------------------------------------------------------


async def _scope_row(conn: AsyncConnection, scope_version_id: int) -> ScopeRow:
    payload = (await conn.execute(_SCOPE, {"id": scope_version_id})).scalar_one()
    return ScopeRow(scope_version_id, scope_from_payload(get_object(payload, "")))


async def _det_chunk(
    conn: AsyncConnection,
    *,
    llm_tuple_id: int,
    scope: ScopeRow,
    scope_part: dict[str, JSONValue],
    after: int,
) -> tuple[list[PublishedInstance], int]:
    """One chunk after `active_config` (module docstring): the page it read and the instances it wrote."""
    page = await det_results.published_page(
        conn, llm_tuple_id=llm_tuple_id, scope_version_id=scope.id, after=after, limit=DET_CHUNK
    )
    if not page:
        return page, 0
    await targets.lock_jobs(conn, [item.job_id for item in page])  # step 4, before any result (carry T6-d)
    stale = await det_results.stale_of(conn, page, scope, scope_part=scope_part)
    written = 0
    for item in stale.results:  # step 10: the results in id order, with the facts `stale_of` read
        if item.job_id not in stale.facts:
            continue  # deleted since the page was read: its results went with it
        instance = await det_results.recompute_instance(
            conn,
            llm_result_id=item.llm_result_id,
            scope=scope,
            det=stale.facts[item.job_id],
            sources=stale.sources[item.job_id],
            job=JobRow(item.job_id),
        )
        written += instance is not None
    return page, written


@reconciler("det_completeness", "heavy")
async def reconcile_det_completeness(ctx: ReconcileContext) -> int:
    """Write the missing and stale score instances of the active tuple (module docstring); how many."""
    written = 0
    scopes: dict[int, tuple[ScopeRow, dict[str, JSONValue]]] = {}  # immutable versions, read once a pass
    pointer: Literal["active", "pending"]
    try:
        for pointer in ("active", "pending"):
            after, seen = 0, None
            while True:
                async with ctx.engine.begin() as conn:
                    await fence_epoch(conn, ctx.epoch)
                    pointers = (await conn.execute(_DET_POINTERS)).one()  # step 2
                    tuple_id = pointers.llm_tuple_id
                    scope_id = (
                        pointers.scope_version_id
                        if pointer == "active"
                        else pointers.pending_scope_version_id
                    )
                    if tuple_id is None or scope_id is None:
                        break
                    if (tuple_id, scope_id) != seen:  # another tuple or scope: its results from the first
                        after, seen = 0, (tuple_id, scope_id)
                    if scope_id not in scopes:
                        row = await _scope_row(conn, int(scope_id))
                        scopes[row.id] = (row, det_results.scope_inputs(row.settings))
                    scope, part = scopes[int(scope_id)]
                    page, count = await _det_chunk(
                        conn, llm_tuple_id=int(tuple_id), scope=scope, scope_part=part, after=after
                    )
                    written += count
                if len(page) < DET_CHUNK:
                    break
                after = page[-1].llm_result_id
    except FencedOut as fenced:
        if fenced.worker_superseded:
            raise  # `fence_epoch` logged it
        return written
    if written:
        log.info("det_completeness_recomputed", instances=written)
    return written


# ---- the admission and scoring alerts ---------------------------------------------------------------------

EXPLORATION_ALERT: Final = "admission.exploration_hits"
HIGH_SIM_ALERT: Final = "admission.high_sim_expired"
BACKLOG_ALERT: Final = "admission.backlog_drain"
FEEDBACK_ALERT: Final = "scoring.feedback_disagreement"
SHIFT_ALERT: Final = "scoring.distribution_shift"
FEEDBACK_SUBJECT: Final = "labels"
EXPLORATION_WINDOW: Final = timedelta(days=14)
EXPLORATION_MIN: Final = 10
EXPLORATION_MAX_PCT: Final = 25
HIGH_SIM_WINDOW: Final = timedelta(hours=24)
HIGH_SIM_ADMISSIONS: Final = timedelta(days=7)
HIGH_SIM_PERCENTILE: Final = 0.7
HIGH_SIM_MIN: Final = 3
BACKLOG_ADMISSIONS: Final = timedelta(days=7)
FEEDBACK_WINDOW: Final = timedelta(days=14)
FEEDBACK_MIN: Final = 10
FEEDBACK_MAX_PCT: Final = 30
FEEDBACK_GOOD_MARGIN: Final = 20
SHIFT_RECENT: Final = timedelta(days=7)
SHIFT_PRIOR: Final = timedelta(days=28)
SHIFT_MAX: Final = Decimal(10)
_MEAN_QUANTUM: Final = Decimal("0.01")
_P70_DIGITS: Final = 4

# The active configuration, read without a lock (these passes write alerts only).
_ACTIVE: Final = q(
    "SELECT a.llm_tuple_id, a.scope_version_id, a.evaluation_policy_id, e.payload AS policy "
    "FROM active_config a JOIN evaluation_policies e ON e.id = a.evaluation_policy_id WHERE a.id = 1"
)
# The tuple's results published in the window by an attempt of the `exploration` lane (the attempt's lane,
# never the target's, which a retry overwrites: Codex r3 B15), and those whose current instance under the
# scope reaches `:tau`.
_EXPLORATION: Final = q(
    "SELECT count(*) AS published, count(*) FILTER (WHERE d.score_mid >= :tau) AS hits FROM llm_results r "
    "JOIN score_targets t ON t.id = r.target_id JOIN score_attempts a ON a.id = r.attempt_id "
    "LEFT JOIN det_results d ON d.llm_result_id = r.id AND d.scope_version_id = :scope AND d.is_current "
    "WHERE t.llm_tuple_id = :tuple AND a.lane = 'exploration' AND r.published_at >= :since "
    "AND r.published_at <= :now"
)
# The percentile of the similarity of the tuple's targets with an attempt of the `similarity` lane started in
# the window.
_SIMILARITY_CUT: Final = q(
    "SELECT percentile_cont(:p) WITHIN GROUP (ORDER BY t.similarity) FROM score_targets t "
    "WHERE t.llm_tuple_id = :tuple AND t.similarity IS NOT NULL AND EXISTS (SELECT 1 FROM score_attempts a "
    "WHERE a.target_id = t.id AND a.lane = 'similarity' AND a.started_at >= :since AND a.started_at <= :now)"
)
_HIGH_SIM_EXPIRED: Final = q(
    "SELECT count(*) FROM score_targets WHERE llm_tuple_id = :tuple AND state = 'expired_unscored' "
    "AND state_changed_at >= :since AND state_changed_at <= :now AND similarity >= :cut"
)
_BACKLOG: Final = q(
    "SELECT (SELECT count(*) FROM score_targets WHERE llm_tuple_id = :tuple AND state = 'pending') "
    "AS pending, "
    "(SELECT count(*) FROM score_attempts a JOIN score_targets t ON t.id = a.target_id "
    "WHERE t.llm_tuple_id = :tuple AND a.started_at >= :since AND a.started_at <= :now) AS admitted"
)
# The labels of the window and those that disagree with their snapshot's `M` (a snapshot without a numeric
# `M` agrees: there is nothing to disagree with).
_FEEDBACK: Final = q(
    "SELECT count(*) AS labels, count(*) FILTER (WHERE (label = 'poor' AND m >= :tau) "
    "OR (label = 'good' AND m < :tau - :margin)) AS disagreeing FROM (SELECT label, CASE "
    "WHEN jsonb_typeof(score_snapshot -> 'M') = 'number' THEN CAST(score_snapshot ->> 'M' AS numeric) "
    "END AS m "
    "FROM score_feedback WHERE created_at >= :since AND created_at <= :now) f"
)
# The mean `score_mid` of the current instances under the scope of the tuple's results published in the last
# 7 days and in the 28 days before them.
_SHIFT: Final = q(
    "SELECT avg(d.score_mid) FILTER (WHERE r.published_at >= :recent) AS recent, "
    "avg(d.score_mid) FILTER (WHERE r.published_at < :recent) AS prior FROM llm_results r "
    "JOIN score_targets t ON t.id = r.target_id JOIN det_results d ON d.llm_result_id = r.id "
    "AND d.scope_version_id = :scope AND d.is_current WHERE t.llm_tuple_id = :tuple "
    "AND r.published_at >= :since AND r.published_at <= :now"
)


@dataclass(frozen=True)
class _Active:
    llm_tuple_id: int | None
    scope_version_id: int
    evaluation_policy_id: int
    relevance_threshold: int


async def _active(conn: AsyncConnection) -> _Active:
    row = (await conn.execute(_ACTIVE)).one()
    return _Active(
        llm_tuple_id=None if row.llm_tuple_id is None else int(row.llm_tuple_id),
        scope_version_id=int(row.scope_version_id),
        evaluation_policy_id=int(row.evaluation_policy_id),
        relevance_threshold=EvaluationPolicy.from_payload(get_object(row.policy, "")).relevance_threshold,
    )


async def _opened(
    conn: AsyncConnection,
    *,
    code: str,
    severity: Literal["info", "warn"],
    subject: str,
    message: str,
    context: dict[str, JSONValue],
) -> int:
    """`open_alert` (once while it is open); 1 when it opened one, logged with its context (ids, counts)."""
    opened = await open_alert(
        conn, code=code, severity=severity, subject=subject, message=message, context=context
    )
    if opened is None:
        return 0
    log.warning("scoring_alert_opened", code=code, subject=subject, **context)
    return 1


async def _exploration_hits(conn: AsyncConnection, active: _Active, tuple_id: int, *, now: datetime) -> int:
    params = {
        "tuple": tuple_id,
        "scope": active.scope_version_id,
        "tau": active.relevance_threshold,
        "since": now - EXPLORATION_WINDOW,
        "now": now,
    }
    row = (await conn.execute(_EXPLORATION, params)).one()
    published, hits = int(row.published), int(row.hits)
    above = hits * 100 > EXPLORATION_MAX_PCT * published  # exact; 0 of 0 is not above
    if not above:
        return await resolve_alerts(conn, code=EXPLORATION_ALERT)
    if published < EXPLORATION_MIN:
        return 0  # too few to raise on, nothing to resolve
    return await _opened(
        conn,
        code=EXPLORATION_ALERT,
        severity="warn",
        subject=f"tuple:{tuple_id}",
        message="more than a quarter of the exploration lane's published targets reach the threshold",
        context={"llm_tuple_id": tuple_id, "published": published, "hits": hits},
    )


async def _high_sim_expired(conn: AsyncConnection, tuple_id: int, *, now: datetime) -> int:
    params = {"tuple": tuple_id, "p": HIGH_SIM_PERCENTILE, "since": now - HIGH_SIM_ADMISSIONS, "now": now}
    found = (await conn.execute(_SIMILARITY_CUT, params)).scalar_one_or_none()
    cut = None if found is None else float(found)
    expired = 0
    if cut is not None:
        counted = await conn.execute(
            _HIGH_SIM_EXPIRED, {"tuple": tuple_id, "since": now - HIGH_SIM_WINDOW, "now": now, "cut": cut}
        )
        expired = int(counted.scalar_one())
    if cut is None or expired == 0:  # no high-similarity expiry in 24 h: 24 h without a recurrence
        return await resolve_alerts(conn, code=HIGH_SIM_ALERT)
    if expired < HIGH_SIM_MIN:
        return 0
    return await _opened(
        conn,
        code=HIGH_SIM_ALERT,
        severity="warn",
        subject=f"tuple:{tuple_id}",
        message="targets of high similarity expired unscored in the last 24 hours",
        context={
            "llm_tuple_id": tuple_id,
            "expired": expired,
            "similarity_p70": round(cut, _P70_DIGITS),
        },
    )


async def _backlog_drain(conn: AsyncConnection, tuple_id: int, *, now: datetime) -> int:
    max_ad_age_days = await targets.active_max_ad_age_days(conn)
    params = {"tuple": tuple_id, "since": now - BACKLOG_ADMISSIONS, "now": now}
    row = (await conn.execute(_BACKLOG, params)).one()
    pending, admitted = int(row.pending), int(row.admitted)
    # pending / (admitted / 7) > max_ad_age_days, exact; nothing admitted drains nothing
    above = pending > 0 and pending * BACKLOG_ADMISSIONS.days > max_ad_age_days * admitted
    if not above:
        return await resolve_alerts(conn, code=BACKLOG_ALERT)
    today = now.date().isoformat()
    changed = await resolve_alerts(conn, code=BACKLOG_ALERT, subject_before=today)  # an earlier day's
    if await ever_raised(conn, code=BACKLOG_ALERT, subject=today):
        return changed  # at most once a day
    return changed + await _opened(
        conn,
        code=BACKLOG_ALERT,
        severity="info",
        subject=today,
        message="the pending targets would take longer than the maximum ad age to drain",
        context={
            "llm_tuple_id": tuple_id,
            "pending": pending,
            "admitted_7d": admitted,
            "max_ad_age_days": max_ad_age_days,
        },
    )


@reconciler("admission_alerts", "heavy")
async def reconcile_admission_alerts(ctx: ReconcileContext) -> int:
    """§9.6.5's three admission alerts over the active tuple at `ctx.now` (module docstring); how many alerts
    it opened or resolved."""
    try:
        async with ctx.engine.begin() as conn:
            await fence_epoch(conn, ctx.epoch)
            active = await _active(conn)
            if active.llm_tuple_id is None:
                return 0
            tuple_id = active.llm_tuple_id
            changed = await _exploration_hits(conn, active, tuple_id, now=ctx.now)
            changed += await _high_sim_expired(conn, tuple_id, now=ctx.now)
            changed += await _backlog_drain(conn, tuple_id, now=ctx.now)
    except FencedOut as fenced:
        if fenced.worker_superseded:
            raise  # `fence_epoch` logged it
        return 0
    return changed


async def _feedback_disagreement(conn: AsyncConnection, active: _Active, *, now: datetime) -> int:
    params = {
        "tau": active.relevance_threshold,
        "margin": FEEDBACK_GOOD_MARGIN,
        "since": now - FEEDBACK_WINDOW,
        "now": now,
    }
    row = (await conn.execute(_FEEDBACK, params)).one()
    labels, disagreeing = int(row.labels), int(row.disagreeing)
    above = disagreeing * 100 > FEEDBACK_MAX_PCT * labels
    if not above:
        return await resolve_alerts(conn, code=FEEDBACK_ALERT)
    if labels < FEEDBACK_MIN:
        return 0
    return await _opened(
        conn,
        code=FEEDBACK_ALERT,
        severity="warn",
        subject=FEEDBACK_SUBJECT,
        message="more than 30% of the labels of the last 14 days disagree with their score",
        context={"labels": labels, "disagreeing": disagreeing},
    )


async def _distribution_shift(conn: AsyncConnection, active: _Active, tuple_id: int, *, now: datetime) -> int:
    recent_since = now - SHIFT_RECENT
    params = {
        "tuple": tuple_id,
        "scope": active.scope_version_id,
        "recent": recent_since,
        "since": recent_since - SHIFT_PRIOR,
        "now": now,
    }
    row = (await conn.execute(_SHIFT, params)).one()
    if row.recent is None or row.prior is None:
        return 0  # a window without a result: nothing to compare
    recent, prior = Decimal(row.recent), Decimal(row.prior)
    if abs(recent - prior) <= SHIFT_MAX:
        return await resolve_alerts(conn, code=SHIFT_ALERT)
    return await _opened(
        conn,
        code=SHIFT_ALERT,
        severity="info",
        subject=f"tuple:{tuple_id}",
        message="the mean score of the last 7 days differs by more than 10 from the 28 days before",
        context={
            "llm_tuple_id": tuple_id,
            "mean_7d": str(recent.quantize(_MEAN_QUANTUM, rounding=ROUND_HALF_UP)),
            "mean_prior_28d": str(prior.quantize(_MEAN_QUANTUM, rounding=ROUND_HALF_UP)),
        },
    )


@reconciler("scoring_drift", "heavy")
async def reconcile_scoring_drift(ctx: ReconcileContext) -> int:
    """§11.13's drift alerts and §17.6's critic rate at `ctx.now` (module docstring); how many alerts it
    opened or resolved."""
    try:
        async with ctx.engine.begin() as conn:
            await fence_epoch(conn, ctx.epoch)
            active = await _active(conn)
            changed = await _feedback_disagreement(conn, active, now=ctx.now)
            if active.llm_tuple_id is not None:
                tuple_id = active.llm_tuple_id
                changed += await _distribution_shift(conn, active, tuple_id, now=ctx.now)
                evaluated = await cycles.evaluate_critic_rate(
                    conn,
                    llm_tuple_id=tuple_id,
                    evaluation_policy_id=active.evaluation_policy_id,
                    now=ctx.now,
                )
                changed += evaluated is not None
    except FencedOut as fenced:
        if fenced.worker_superseded:
            raise  # `fence_epoch` logged it
        return 0
    return changed


# ---- the score lane's gauges ------------------------------------------------------------------------------


@gauge_refresh("score_lane")
async def refresh_score_lane_gauges(conn: AsyncConnection, now: datetime) -> None:
    """The three gauges of §17.5 L5538-5539 at `now`, read without a lock (module docstring)."""
    figures = await cycles.target_figures(conn)
    for state in SCORE_TARGET_STATES:
        SCORE_TARGETS.labels(state=state).set(figures.by_state.get(state, 0))
    oldest = figures.oldest_pending_at
    OLDEST_PENDING_TARGET_AGE.set(0.0 if oldest is None else max(0.0, (now - oldest).total_seconds()))
    active = await _active(conn)
    rate = None
    if active.llm_tuple_id is not None:
        rate, _ = await cycles.critic_rate_7d(
            conn, llm_tuple_id=active.llm_tuple_id, evaluation_policy_id=active.evaluation_policy_id, now=now
        )
    CRITIC_RATE_7D.set(math.nan if rate is None else float(rate))
