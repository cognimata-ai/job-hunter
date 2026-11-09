"""The settings dry-run (spec §6.8, §13.5; plan Task 16): the preview of a proposed settings document that the
settings page shows on every change and that a save runs again (Task 17).

`dry_run` composes §6.8's pseudo-code in its order:
1. `validate_fields` (with the eval gate of the payload's candidate, `payload_gate`: the active rubric and
   prompt-schema versions with the payload's model and evaluation policies by hash, M3 Task 20), then
   `unknown_references` (Task 17): an entry of `queries[]` whose id names no live query (no row, or a
   retired one: an edit retires its row, plan M4 Task 29 Ruling T29-c) or of `watchlist[]` whose id names no
   row is an `invalid` error at the entry's id. On any issue the result holds the errors only, so the preview
   and the save (which runs this again) refuse the same documents;
2. right after a clean validation, `warnings_only` (`eval_uncalibrated`, `eval_failed`,
   `serpapi_disabled_no_fallback`); both read `active`, which tells a selection of generator effort `low` from
   a kept one (Ruling T20-d);
3. `paid_cells`: the enabled queries times the location keys (the scope's locations, plus `remote-de` with
   remote work within Germany, `quota.cells.location_keys`);
4. `quota_projections` with the run view of the proposed settings (`RunSettings.from_settings`, the proposed
   interval), on the current window snapshots (none: simulated in memory). Paid search also shows its window:
   the counted units (`ledger.used_units`), the limit and the reset (`window`);
5. the LLM projection (`llm_projection`) with `ĉ` (`c_hat`), the proposed evaluation policy's projected
   critic rate (the stored row's, or the rate a new row gets: the replay over the round-0 drafts, else
   0.450), the arrivals (`arrival_per_month`) and `llm_cap_binding`; beside it the month's figures as the
   save would leave them: the month row read without a lock (`spend_ledger.read_month_or_new`), its cap and
   buffer set to the proposed ones in memory (`budget.change_cap`), its counted spend and scoring headroom.
   With the active configuration read once (`ActivePolicies`; plan M4 Task 14, D22, spec L1137, L1178):
   - `ĉ` is §6.9.2's: `cycles.c_hat` of the active tuple and evaluation policy (the trailing 7-day mean of
     its published online attempts, from 20 of them; else the central estimate) while the proposed model and
     evaluation policies are the active ones by content hash, else `central_cost_per_target` of the proposed
     model policy, whose own history no tuple has yet. Without an active tuple it is the central estimate.
     Its source goes with it: the capacity adds a cycle's cache-write share to a central `ĉ` only
     (`llm_projection.paced_cost_per_target`), since a history mean already includes its cache writes;
   - `arrival_per_month` is §6.8's trailing 7-day rate of new targets, a month (Ruling T14-d): the active
     tuple's targets created in `[now - 7 d, now]` without a bulk operation, x 30 / 7, rounded to the nearest
     whole number. An ordinary arrival has none (the collection's `enqueue_targets`, the `missing_targets`
     reconciler and a merge's new current revision create with `bulk_operation_id = NULL`); the targets an
     activation's `create_targets_for_tuple` creates or revives carry its bulk operation, so a re-activated
     tuple's catch-up is no arrival. It is 3,000 (`DEFAULT_ARRIVAL_PER_MONTH`) when there is no such history:
     without an active tuple, while it has no target or its oldest target is younger than 7 days, and while
     its latest activation is younger than 7 days, so a window never straddles one. The latest activation is
     the newest `created_at` of the tuple's `bulk_operations`: every activation (`targets.activate_tuple`, a
     confirm's or an `llm_tuple` save's) opens one (`bulk.open`), with or without retained revisions, and the
     retention deletes only those that ended 13 months before. One statement;
6. the effect classes (`classify_effects` against the active settings) and what they add:
   - `scope`: `det_recompute`, the published results of the active tuple that the scope apply recomputes
     without any LLM call (`count_published_results_active_tuple`, M3 Task 15);
   - `llm_tuple` with an active profile: the warning `llm_rescore` with `targets`
     (`count_retained_current_revisions`), `est_usd = targets x ĉ` and `bulk_budget_usd =
     scoring.bulk_rescore_max_budget_share x (A x (1 - reserve_frac) - scoring_counted_usd)` (§6.9.3; Task
     21's confirm preview uses the same share). Without an active profile there is no tuple to re-score
     (`active_config` pairs them), so there is no warning (D22);
   - `evaluation_policy`: `critic_rate`, the projected critic rate (published results are not re-reviewed);
   - either of the gated classes (`GATED`, §11.13 "What is gated"): `eval_gate`, the state of the gate a
     save would check (`uncalibrated`, `passed` or `failed`);
   - `schedule`: `next_slot`, the first slot after `now` under the proposed schedule (`null` when disabled),
     as a save would reset it (§3.4).

**Nothing is written.** Every read is a plain `SELECT` (no row lock: neither `active_config` nor the
`llm_budget` row) and every simulation is in memory, so the route runs it in a read-only transaction. Amounts
are decimal strings at $0.000001 (a cap at its own cents), figures of the quota part as `quota.dryrun` rounds
them, times ISO-8601 UTC.
"""

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.seed import (
    DEFAULT_PROJECTED_CRITIC_RATE,
    anchor_utc_for,
    content_hash,
    latest_slot_at_or_before,
)
from jobhunter.db.sql import q
from jobhunter.llm import budget
from jobhunter.llm.budget import BudgetParams, BudgetRow
from jobhunter.llm.spend_ledger import read_month_or_new
from jobhunter.quota.cells import location_keys
from jobhunter.quota.dryrun import Warning, quota_projections
from jobhunter.quota.ledger import used_units
from jobhunter.quota.windows import Provider, Window, current_snapshot, roll_forward
from jobhunter.run_settings import RunSettings
from jobhunter.scoring.det_results import count_published
from jobhunter.scoring.evaluation_policy import EvaluationPolicy, replay_projected_critic_rate
from jobhunter.settings.derive import candidate_of, derived
from jobhunter.settings.effects import EffectClass, classify_effects
from jobhunter.settings.llm_projection import (
    DEFAULT_ARRIVAL_PER_MONTH,
    CHatSource,
    LlmProjection,
    cap_binding,
    central_cost_per_target,
    llm_projection,
    scoring_share,
)
from jobhunter.settings.model import Settings
from jobhunter.settings.validate import (
    FieldIssue,
    GateState,
    eval_gate_state,
    field_path,
    validate_fields,
    warnings_only,
)
from jobhunter.types import JSONValue

# §11.13 "What is gated": a save of either class checks the eval gate (`save._check_eval_gate`).
GATED: Final[frozenset[EffectClass]] = frozenset({"llm_tuple", "evaluation_policy"})
# A proposal has no settings version or scope version yet; the quota projections read neither id.
_NO_VERSION: Final = 0
_SECONDS_PER_HOUR: Final = 3600
_ZERO: Final = Decimal(0)

_ACTIVE_PAYLOAD: Final = q(
    "SELECT v.payload FROM active_config a JOIN settings_versions v ON v.id = a.settings_version_id "
    "WHERE a.id = 1"
)
# Retained jobs (tombstones are M5's) that a re-score would create targets for: unmerged, with a current
# revision, not an adversarial golden case (§12.6, `create_targets_for_tuple`'s `targets.RETAINED_JOB_SQL`,
# Codex r1 #32).
_RETAINED_CURRENT_REVISIONS: Final = q(
    "SELECT count(*) FROM jobs WHERE merged_into_job_id IS NULL AND current_revision_id IS NOT NULL "
    "AND NOT synthetic_eval"
)
_ACTIVE_TUPLE: Final = q("SELECT llm_tuple_id FROM active_config WHERE id = 1")
# The active configuration the LLM projection compares the proposal with (D22).
_ACTIVE_POLICIES: Final = q(
    "SELECT a.profile_version_id, a.llm_tuple_id, a.evaluation_policy_id, "
    "m.policy_hash AS model_policy_hash, e.policy_hash AS evaluation_policy_hash FROM active_config a "
    "JOIN model_policies m ON m.id = a.model_policy_id "
    "JOIN evaluation_policies e ON e.id = a.evaluation_policy_id WHERE a.id = 1"
)
# A tuple's oldest target, its new targets (no bulk operation) created in `[since, now]`, and its latest
# activation (the newest of its bulk operations).
_ARRIVALS: Final = q(
    "SELECT t.oldest, t.recent, (SELECT max(b.created_at) FROM bulk_operations b "
    "WHERE b.llm_tuple_id = :tuple) AS activated FROM (SELECT min(created_at) AS oldest, count(*) FILTER "
    "(WHERE bulk_operation_id IS NULL AND created_at >= :since AND created_at <= :now) AS recent "
    "FROM score_targets WHERE llm_tuple_id = :tuple) t"
)
ARRIVAL_WINDOW: Final = timedelta(days=7)
_DAYS_PER_MONTH: Final = 30
_EVALUATION_POLICY_RATE: Final = q(
    "SELECT projected_critic_rate FROM evaluation_policies WHERE policy_hash = :h"
)
# A query id is missing when it names no row or a retired one: the document names live queries only.
_MISSING_QUERIES: Final = q(
    "SELECT u.id FROM unnest(CAST(:ids AS bigint[])) AS u(id) "
    "WHERE NOT EXISTS (SELECT 1 FROM queries q WHERE q.id = u.id AND q.retired_at IS NULL)"
)
_MISSING_COMPANIES: Final = q(
    "SELECT u.id FROM unnest(CAST(:ids AS bigint[])) AS u(id) "
    "WHERE NOT EXISTS (SELECT 1 FROM watchlist_companies w WHERE w.id = u.id)"
)


@dataclass(frozen=True)
class DryRunResult:
    """`errors` block a save; each of `warnings` must be acknowledged on save; `projections` is §13.5's
    preview; `effects` the classes a save would apply (empty when there are errors)."""

    errors: list[FieldIssue]
    warnings: list[Warning]
    projections: dict[str, JSONValue]
    effects: frozenset[EffectClass]


@dataclass(frozen=True)
class ActivePolicies:
    """What the LLM projection compares a proposal with (module docstring, step 5): whether a profile is
    active, the active tuple and evaluation policy, and the hashes of the active model and evaluation
    policies."""

    profile_active: bool
    llm_tuple_id: int | None
    evaluation_policy_id: int
    model_policy_hash: bytes
    evaluation_policy_hash: bytes


async def active_policies(conn: AsyncConnection) -> ActivePolicies:
    """The active configuration's `ActivePolicies` (a plain read)."""
    row = (await conn.execute(_ACTIVE_POLICIES)).one()
    return ActivePolicies(
        profile_active=row.profile_version_id is not None,
        llm_tuple_id=None if row.llm_tuple_id is None else int(row.llm_tuple_id),
        evaluation_policy_id=int(row.evaluation_policy_id),
        model_policy_hash=bytes(row.model_policy_hash),
        evaluation_policy_hash=bytes(row.evaluation_policy_hash),
    )


async def c_hat(
    conn: AsyncConnection, proposed: Settings, active: ActivePolicies, *, now: datetime
) -> tuple[Decimal, CHatSource]:
    """§6.9.2's `ĉ` for `proposed` and where it came from (module docstring, step 5): the active tuple's
    history while its model and evaluation policies are the active ones (`cycles.c_hat`, itself central below
    20 attempts), else the proposed model policy's central estimate."""
    policy = proposed.scoring.model_policy
    d = derived(proposed)
    same = (
        content_hash(d.model_policy) == active.model_policy_hash
        and content_hash(d.evaluation) == active.evaluation_policy_hash
    )
    if active.llm_tuple_id is None or not same:
        return central_cost_per_target(policy), "central"
    # `cycles` imports `llm.bulk`, which imports this module: imported when the dry-run runs
    from jobhunter.scoring import cycles

    return await cycles.c_hat(
        conn,
        llm_tuple_id=active.llm_tuple_id,
        evaluation_policy_id=active.evaluation_policy_id,
        model_policy=policy,
        now=now,
    )


async def arrival_per_month(conn: AsyncConnection, llm_tuple_id: int | None, *, now: datetime) -> int:
    """§6.8's arrivals a month (module docstring, step 5): the active tuple's new targets of the 7 days up to
    `now` x 30 / 7, or `DEFAULT_ARRIVAL_PER_MONTH` without 7 days of history since its latest activation."""
    if llm_tuple_id is None:
        return DEFAULT_ARRIVAL_PER_MONTH
    since = now - ARRIVAL_WINDOW
    row = (await conn.execute(_ARRIVALS, {"tuple": llm_tuple_id, "since": since, "now": now})).one()
    young = row.oldest is None or row.oldest > since
    if young or (row.activated is not None and row.activated > since):
        return DEFAULT_ARRIVAL_PER_MONTH
    rate = Decimal(int(row.recent) * _DAYS_PER_MONTH) / ARRIVAL_WINDOW.days
    return int(rate.to_integral_value(rounding=ROUND_HALF_UP))


async def active_settings(conn: AsyncConnection) -> Settings:
    """The active settings version's document, read with a plain `SELECT` (no lock)."""
    payload = (await conn.execute(_ACTIVE_PAYLOAD)).scalar_one_or_none()
    if payload is None:
        raise LookupError("active_config has no settings version")
    return Settings.model_validate(payload)


async def unknown_references(conn: AsyncConnection, settings: Settings) -> list[FieldIssue]:
    """An `invalid` issue at each `queries[]` entry of `settings` whose id names no live query (no row, or a
    retired one) and each `watchlist[]` entry whose id names no row (plain reads; the ids are bounded by the
    model, so they fit `bigint`)."""
    query_ids = [entry.query_id for entry in settings.queries]
    company_ids = [entry.watchlist_company_id for entry in settings.watchlist]
    missing_queries = set((await conn.execute(_MISSING_QUERIES, {"ids": query_ids})).scalars())
    missing_companies = set((await conn.execute(_MISSING_COMPANIES, {"ids": company_ids})).scalars())
    return [
        FieldIssue(field_path(("queries", i, "query_id")), "invalid", "no such query")
        for i, query_id in enumerate(query_ids)
        if query_id in missing_queries
    ] + [
        FieldIssue(
            field_path(("watchlist", i, "watchlist_company_id")), "invalid", "no such watchlist company"
        )
        for i, company_id in enumerate(company_ids)
        if company_id in missing_companies
    ]


async def count_retained_current_revisions(conn: AsyncConnection) -> int:
    """The current revisions of retained jobs: unmerged, non-synthetic jobs with a current revision (the
    targets of a re-score)."""
    return int((await conn.execute(_RETAINED_CURRENT_REVISIONS)).scalar_one())


async def count_published_results_active_tuple(conn: AsyncConnection) -> int:
    """The published results of the active tuple, which a scope apply recomputes with no LLM call (§13.4;
    `det_results.count_published`), read without a lock; 0 while no tuple is active."""
    tuple_id = (await conn.execute(_ACTIVE_TUPLE)).scalar_one_or_none()
    if tuple_id is None:
        return 0
    return await count_published(conn, llm_tuple_id=int(tuple_id))


async def projected_critic_rate(conn: AsyncConnection, settings: Settings, *, now: datetime) -> Decimal:
    """The projected critic rate of the evaluation policy of `settings` (§11.7.4): the stored row's when it
    exists, else the rate a save at `now` would give the new row: the replay over the round-0 drafts
    (`evaluation_policy.replay_projected_critic_rate`, plain reads), or `DEFAULT_PROJECTED_CRITIC_RATE`, the
    §21.2 central 0.450, below 50 of them."""
    payload = derived(settings).evaluation
    found = await conn.execute(_EVALUATION_POLICY_RATE, {"h": content_hash(payload)})
    stored = found.scalar_one_or_none()
    if stored is not None:
        return Decimal(stored)
    replayed = await replay_projected_critic_rate(conn, EvaluationPolicy.from_payload(payload), now=now)
    return DEFAULT_PROJECTED_CRITIC_RATE if replayed is None else replayed


def paid_cells(settings: RunSettings) -> int:
    """§6.8's paid cells of the run view `settings`: the enabled queries times the location keys (locations,
    and `remote-de`). The query lists count the active settings' paid cells with it too."""
    queries = sum(1 for enabled in settings.queries.values() if enabled)
    return queries * len(location_keys(settings.scope))


def next_slot(settings: Settings, now: datetime) -> datetime | None:
    """The first slot after `now` under the schedule of `settings` (§3.4: a schedule save resets the cursor to
    the latest slot at or before `now`), `None` while the schedule is disabled."""
    schedule = settings.schedule
    if not schedule.enabled:
        return None
    interval_s = schedule.interval_hours * _SECONDS_PER_HOUR
    anchor = anchor_utc_for(now, schedule.anchor_local, settings.display.timezone)
    return latest_slot_at_or_before(anchor, interval_s, now) + timedelta(seconds=interval_s)


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _usd(amount: Decimal) -> str:
    return format(amount.quantize(budget.USD_QUANTUM, rounding=ROUND_HALF_UP), "f")


async def _window(
    conn: AsyncConnection, provider: Provider, snapshot: Window | None, run: RunSettings, now: datetime
) -> JSONValue:
    """The provider's window at `now` as the projection simulates it: counted units, limit and reset."""
    w, _ = roll_forward(snapshot, provider, now, run.quota, interval_s=run.interval_s)
    if w is None:
        return None
    return {
        "used_units": await used_units(conn, provider, w.window_start),
        "limit_units": w.limit_units,
        "reset_at": _iso(w.window_end),
    }


def _llm_json(projection: LlmProjection) -> dict[str, JSONValue]:
    return {
        "bucket_rate_per_h": _usd(projection.bucket_rate_per_h),
        "bucket_capacity": _usd(projection.bucket_capacity),
        "c_hat": _usd(projection.c_hat),
        "projected_critic_rate": format(projection.projected_critic_rate, "f"),
        "targets_per_month": projection.targets_per_month,
        "arrival_per_month": projection.arrival_per_month,
        "projected_month_spend": _usd(projection.projected_month_spend),
    }


def _after_save(row: BudgetRow, p: BudgetParams, now: datetime) -> BudgetRow:
    """A copy of the month row with the proposed cap and buffer, as a save of class `budget` changes it."""
    after = replace(row)
    budget.change_cap(after, p.cap_usd, p.overrun_buffer_usd, now)
    return after


def bulk_budget(settings: Settings, p: BudgetParams, row: BudgetRow) -> Decimal:
    """§6.9.3: `bulk_rescore_max_budget_share` of the scoring headroom at the start, `A x (1 - reserve_frac)
    - scoring_counted_usd`, with `A` of `p` (never below zero; not rounded). The confirm's re-score estimate
    uses it too (Task 21)."""
    headroom = max(_ZERO, scoring_share(p) - row.scoring_counted_usd)
    return settings.scoring.bulk_rescore_max_budget_share * headroom


async def payload_gate(conn: AsyncConnection, payload: JSONValue) -> GateState:
    """The eval gate of `payload`'s candidate (`derive.candidate_of`, `validate.eval_gate_state`); for a
    document the settings model refuses, `uncalibrated`: `validate_fields` then reports its model errors only,
    and no rule reads the gate."""
    try:
        settings = Settings.model_validate(payload)
    except ValidationError:
        return "uncalibrated"
    return await eval_gate_state(conn, candidate=await candidate_of(conn, settings))


async def dry_run(
    conn: AsyncConnection, payload: JSONValue, *, active: Settings, now: datetime
) -> DryRunResult:
    """The §6.8 dry-run of `payload` against the `active` settings at `now` (module docstring)."""
    gate = await payload_gate(conn, payload)
    proposed, errors = validate_fields(payload, gate=gate, active=active)
    if proposed is not None:
        errors = await unknown_references(conn, proposed)
    if proposed is None or errors:
        return DryRunResult(errors=errors, warnings=[], projections={}, effects=frozenset())
    warns = warnings_only(proposed, gate=gate, active=active)

    run = RunSettings.from_settings(
        proposed,
        settings_version_id=_NO_VERSION,
        scope_version_id=_NO_VERSION,
        interval_s=proposed.schedule.interval_hours * _SECONDS_PER_HOUR,
    )
    jsearch = await current_snapshot(conn, "jsearch")
    adzuna = await current_snapshot(conn, "adzuna")
    serpapi = await current_snapshot(conn, "serpapi")
    quota_warns, proj = quota_projections(
        run, jsearch=jsearch, adzuna=adzuna, serpapi=serpapi, cells=paid_cells(run), now=now
    )
    warns += quota_warns
    paid_search: tuple[tuple[Provider, Window | None, str], ...] = (
        ("jsearch", jsearch, "jsearch"),
        ("serpapi", serpapi, "serpapi_fallback"),
    )
    for provider, snapshot, part in paid_search:
        projection = proj[part]
        if isinstance(projection, dict):
            projection["window"] = await _window(conn, provider, snapshot, run, now)

    policies = await active_policies(conn)
    cost, source = await c_hat(conn, proposed, policies, now=now)
    critic_rate = await projected_critic_rate(conn, proposed, now=now)
    llm = llm_projection(
        proposed,
        now=now,
        c_hat=cost,
        c_hat_source=source,
        projected_critic_rate=critic_rate,
        arrival_per_month=await arrival_per_month(conn, policies.llm_tuple_id, now=now),
    )
    p = BudgetParams.from_payload(proposed.to_payload())
    month = budget.month_of(now)
    after = _after_save(await read_month_or_new(conn, month, p, now=now), p, now)
    proj["llm"] = {
        **_llm_json(llm),
        "month": month.isoformat(),
        "cap_usd": format(after.cap_usd, "f"),
        "counted_usd": _usd(after.counted_usd),
        "headroom_usd": _usd(budget.headroom(after, "scoring", p)),
    }
    if cap_binding(llm):
        warns.append(Warning("llm_cap_binding", expected_scored_per_month=llm.targets_per_month))

    effects = classify_effects(active, proposed)
    if "scope" in effects:
        proj["det_recompute"] = await count_published_results_active_tuple(conn)
    if "llm_tuple" in effects and policies.profile_active:
        targets = await count_retained_current_revisions(conn)
        warns.append(
            Warning(
                "llm_rescore",
                targets=targets,
                est_usd=_usd(targets * cost),
                bulk_budget_usd=_usd(bulk_budget(proposed, p, after)),
            )
        )
    if "evaluation_policy" in effects:
        proj["critic_rate"] = format(critic_rate, "f")
    if effects & GATED:
        proj["eval_gate"] = gate
    if "schedule" in effects:
        slot = next_slot(proposed, now)
        proj["next_slot"] = None if slot is None else _iso(slot)
    return DryRunResult(errors=[], warnings=warns, projections=proj, effects=effects)
