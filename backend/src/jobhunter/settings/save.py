"""The settings save (spec §13.2, §13.3; plan Task 17, Deviations 10, 14 and 15).

`save_settings` writes a new immutable `settings_versions` row and activates it, in the caller's transaction:

1. `active_config` `FOR UPDATE`, the first lock (§9.1). A caller that already holds it (the save route,
   Deviation 15's internal saves) makes this a no-op; a second save waits here for the first one's commit.
2. A `base_version_id` other than the active version is 409 `stale_base_version` (§13.2): of two tabs saving
   from one base, the second waits at step 1 and fails here, never deadlocks.
3. The dry-run again (`dryrun.dry_run`, §13.2), on the payload against the active settings: its errors (the
   field rules, and `queries[]`/`watchlist[]` entries that name no row) are 422 `validation_failed` with each
   issue's path, code and message; each warning code missing from `acknowledged_warnings` is 422
   `validation_failed` with `FieldError("acknowledged_warnings", "warning_not_acknowledged", <code>)`. An
   internal save (`internal=True`, Deviation 15) skips the acknowledgement check and copies the active
   version's acknowledgements. Then the eval gate (§11.13), once, when the classes hold `llm_tuple` or
   `evaluation_policy`: uncalibrated in M2, so skipped; a failed gate without `eval_override_ack` is 409
   `eval_gate_failed`. Every refusal therefore comes before the first write.
4. The derived rows (`derive.derived` with the active profile, Deviation 14; `ensure_derived`).
5. The version row: `version_no = max + 1` (the step-1 lock serialises the writers, the unique constraint
   backs it), the stored payload `Settings.to_payload()` (the normal form, never the request; Deviation 38),
   the derived ids, `base_version_id`, `effect_classes`, `acknowledged_warnings` and `eval_override_ack`.
6. The effect classes, one private function each, in `ACTION_ORDER`: `llm_tuple`, then `scope` (§13.3), then
   the others, `budget` first: its month row comes before the other domain rows in the §9.1 order
   (`active_config` → owner row → `llm_budget` → … → other domain rows → `ranking_state`), and `collection`
   after `schedule`, since `load_run_settings` joins `schedule_state`. The fault point
   `settings.after_llm_tuple` lies between `llm_tuple` and `scope`.
   - `llm_tuple`: `model_policy_id := new`, and with an active profile the tuple (`ensure_tuple`) and
     `llm_tuple_id := it`. §12.6 (d) to (f) and `create_targets_for_tuple` are M3's (Deviation 10);
   - `scope`: `scope_apply.set_desired_scope` with the derived scope. When that is the active scope (a
     revert, §13.4 step 5's second remedy), nothing is left unapplied: after every action, so after the
     `budget` action's month row, every open `scope.apply_failed` is resolved
     (`scope_apply.resolve_apply_failed`, other domain rows), before `ranking_state`;
   - `budget`: the month row is created if new (`ensure_month`) and changed under its lock (§6.9.1): a new cap
     through `change_cap` (with the new buffer), a new buffer alone through `change_buffer`, which leaves a
     lowering `change_cap` recorded earlier at its own time. The other budget fields live in the settings
     only, so a save of them leaves the row;
   - `schedule`: `schedule_state` reset in one statement (§3.4): the anchor on the save date, the interval,
     `enabled`, the cursor at the latest slot at or before `now`, and the version;
   - `collection`: the instance of every `watchlist[]` board (`instance_for_company`), then the cells of the
     new version (`ensure_cells`; new cells start never covered);
   - `admission`: the flip only (the `max_ad_age_days` expiry sweep needs targets, M3);
   - `execution`: `execution_config_id := new`;
   - `evaluation_policy`: `evaluation_policy_id := new` (the row is step 4's).
   After every action, when the tuple pointer changed, `ranking.bump` runs once: `ranking_state` is the
   last lock of §3.3.3, after the `budget` action's month row.
7. `settings_version_id := new` and `updated_at`, in the statement that flips the pointers of step 6.
   `active_config` is locked since step 1, so this takes no new lock after `ranking_state`.

Every write of the class actions that takes a row lock follows that order. The rows inserted before the month
row (the derived rows, the version, the tuple) are new rows of tables that only writers holding
`active_config` `FOR UPDATE` insert into, so no other transaction can hold or wait for them.

`ensure_tuple` is the one select-or-insert of an `llm_tuples` row (Task 21's confirm reuses it). Neither this
module nor `scope_apply` logs: the caller logs ids and codes after its commit. (`spend_ledger.change_cap` and
`change_buffer` log the month row's change in the transaction, as every month-row writer of Task 3 does.)
"""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Final

from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.api.errors import FieldError, Problem
from jobhunter.db import ranking
from jobhunter.db.seed import SETTINGS_SCHEMA_VERSION, anchor_utc_for, ensure_id, latest_slot_at_or_before
from jobhunter.db.sql import q
from jobhunter.faults import afault_point
from jobhunter.llm import budget, spend_ledger
from jobhunter.llm.budget import BudgetParams
from jobhunter.quota.planner import ensure_cells
from jobhunter.run_settings import load_run_settings
from jobhunter.settings import scope_apply
from jobhunter.settings.derive import DerivedIds, active_profile, derived, ensure_derived
from jobhunter.settings.dryrun import dry_run
from jobhunter.settings.effects import EffectClass
from jobhunter.settings.model import Settings
from jobhunter.settings.validate import eval_gate_state
from jobhunter.sources.instances import instance_for_company, list_instances
from jobhunter.types import JSONValue
from jobhunter.worker.tasks import Deferrer

AFTER_LLM_TUPLE_POINT: Final = "settings.after_llm_tuple"
ACKNOWLEDGED_FIELD: Final = "acknowledged_warnings"
NOT_ACKNOWLEDGED: Final = "warning_not_acknowledged"
_SECONDS_PER_HOUR: Final = 3600

_LOCK_ACTIVE: Final = q(
    "SELECT settings_version_id, profile_version_id, llm_tuple_id FROM active_config WHERE id = 1 FOR UPDATE"
)
_VERSION: Final = q("SELECT payload, acknowledged_warnings FROM settings_versions WHERE id = :id")
_INSERT_VERSION: Final = q(
    "INSERT INTO settings_versions (version_no, schema_version, payload, scope_version_id, model_policy_id, "
    "execution_config_id, evaluation_policy_id, base_version_id, effect_classes, acknowledged_warnings, "
    "eval_override_ack, created_at) "
    "SELECT coalesce(max(version_no), 0) + 1, :schema_version, :payload, :scope_version_id, "
    ":model_policy_id, :execution_config_id, :evaluation_policy_id, :base_version_id, "
    "CAST(:effect_classes AS text[]), CAST(:acknowledged_warnings AS text[]), :eval_override_ack, :now "
    "FROM settings_versions RETURNING id, version_no",
    jsonb=["payload"],
)
_TUPLE_POINTERS: Final = q(
    "SELECT rubric_version_id, prompt_schema_version_id FROM active_config WHERE id = 1"
)
_FIND_TUPLE: Final = q(
    "SELECT id FROM llm_tuples WHERE profile_version_id = :profile_version_id "
    "AND rubric_version_id = :rubric_version_id AND prompt_schema_version_id = :prompt_schema_version_id "
    "AND model_policy_id = :model_policy_id"
)
_INSERT_TUPLE: Final = q(
    "INSERT INTO llm_tuples (profile_version_id, rubric_version_id, prompt_schema_version_id, "
    "model_policy_id) VALUES (:profile_version_id, :rubric_version_id, :prompt_schema_version_id, "
    ":model_policy_id) ON CONFLICT DO NOTHING RETURNING id"
)
_RESET_SCHEDULE: Final = q(
    "UPDATE schedule_state SET anchor_utc = :anchor_utc, interval_s = :interval_s, enabled = :enabled, "
    "last_evaluated_slot = :slot, settings_version_id = :settings_version_id, updated_at = :now WHERE id = 1"
)
# A pointer the save does not flip keeps its value (`coalesce` over a NULL parameter).
_FLIP: Final = q(
    "UPDATE active_config SET settings_version_id = :settings_version_id, "
    "model_policy_id = coalesce(CAST(:model_policy_id AS bigint), model_policy_id), "
    "llm_tuple_id = coalesce(CAST(:llm_tuple_id AS bigint), llm_tuple_id), "
    "execution_config_id = coalesce(CAST(:execution_config_id AS bigint), execution_config_id), "
    "evaluation_policy_id = coalesce(CAST(:evaluation_policy_id AS bigint), evaluation_policy_id), "
    "updated_at = :now WHERE id = 1"
)
_FLIPPABLE: Final = ("model_policy_id", "llm_tuple_id", "execution_config_id", "evaluation_policy_id")


@dataclass(frozen=True)
class SaveRequest:
    """A save: the proposed document, the version it was edited from, the warning codes the user
    acknowledged, and the eval-gate override (§11.13)."""

    payload: JSONValue
    base_version_id: int
    acknowledged_warnings: frozenset[str]
    eval_override_ack: bool = False


@dataclass(frozen=True)
class SaveOutcome:
    """The new version, the classes it applied, and the `apply_scope` operation its scope needs, or
    `None`."""

    version_id: int
    version_no: int
    effects: frozenset[EffectClass]
    apply_scope_operation_id: int | None


async def ensure_tuple(conn: AsyncConnection, *, profile_version_id: int, model_policy_id: int) -> int:
    """The id of the `llm_tuples` row of `profile_version_id`, the active rubric and prompt-schema versions
    and `model_policy_id`, inserted when absent (the one tuple select-or-insert; §7.3). The caller holds
    `active_config` `FOR UPDATE`, which serialises the inserts; `LookupError` when either active pointer is
    unset (`migrate` seeds both)."""
    pointers = (await conn.execute(_TUPLE_POINTERS)).one()
    if pointers.rubric_version_id is None or pointers.prompt_schema_version_id is None:
        raise LookupError("active_config has no rubric or prompt-schema version (migrate seeds both)")
    params = {
        "profile_version_id": profile_version_id,
        "rubric_version_id": int(pointers.rubric_version_id),
        "prompt_schema_version_id": int(pointers.prompt_schema_version_id),
        "model_policy_id": model_policy_id,
    }
    return await ensure_id(conn, _FIND_TUPLE, _INSERT_TUPLE, params, params)


@dataclass
class _Activation:
    """What the class actions of one save share, and what they leave for step 7."""

    conn: AsyncConnection
    req: SaveRequest
    now: datetime
    defer: Deferrer
    active: Row[tuple[object, ...]]
    before: Settings
    proposed: Settings
    payload: dict[str, JSONValue]
    ids: DerivedIds
    version_id: int
    flips: dict[str, int] = field(default_factory=dict)
    tuple_flipped: bool = False
    apply_scope_operation_id: int | None = None
    scope_is_active: bool = False  # the `scope` action's desired scope is the active one


_GATED: Final[frozenset[EffectClass]] = frozenset({"llm_tuple", "evaluation_policy"})


async def _check_eval_gate(conn: AsyncConnection, req: SaveRequest) -> None:
    """§11.13: a change the gate covers must pass, or the override is acknowledged. Before 30 golden cases the
    gate is `uncalibrated` and skipped, which in M2 it always is (`eval_gate_state`)."""
    if await eval_gate_state(conn) == "failed" and not req.eval_override_ack:
        raise Problem(
            409,
            "eval_gate_failed",
            "The eval gate failed",
            detail="acknowledge the override (eval_override_ack) to activate this change",
        )


async def _apply_llm_tuple(a: _Activation) -> None:
    a.flips["model_policy_id"] = a.ids.model_policy_id
    profile_version_id = a.active.profile_version_id
    if isinstance(profile_version_id, int):
        tuple_id = await ensure_tuple(
            a.conn, profile_version_id=profile_version_id, model_policy_id=a.ids.model_policy_id
        )
        if tuple_id != a.active.llm_tuple_id:
            a.flips["llm_tuple_id"] = tuple_id
            a.tuple_flipped = True


async def _apply_scope(a: _Activation) -> None:
    a.apply_scope_operation_id = await scope_apply.set_desired_scope(
        a.conn, a.ids.scope_version_id, now=a.now, defer=a.defer
    )
    a.scope_is_active = a.apply_scope_operation_id is None


async def _apply_budget(a: _Activation) -> None:
    new, old = BudgetParams.from_payload(a.payload), BudgetParams.from_payload(a.before.to_payload())
    cap_changed = new.cap_usd != old.cap_usd
    if not cap_changed and new.overrun_buffer_usd == old.overrun_buffer_usd:
        return
    month = budget.month_of(a.now)
    await spend_ledger.ensure_month(a.conn, month, new, a.now)
    if cap_changed:
        await spend_ledger.change_cap(
            a.conn, month=month, cap_usd=new.cap_usd, overrun_buffer_usd=new.overrun_buffer_usd, now=a.now
        )
    else:
        await spend_ledger.change_buffer(
            a.conn, month=month, overrun_buffer_usd=new.overrun_buffer_usd, now=a.now
        )


async def _apply_schedule(a: _Activation) -> None:
    schedule = a.proposed.schedule
    interval_s = schedule.interval_hours * _SECONDS_PER_HOUR
    anchor = anchor_utc_for(a.now, schedule.anchor_local, a.proposed.display.timezone)
    result = await a.conn.execute(
        _RESET_SCHEDULE,
        {
            "anchor_utc": anchor,
            "interval_s": interval_s,
            "enabled": schedule.enabled,
            "slot": latest_slot_at_or_before(anchor, interval_s, a.now),
            "settings_version_id": a.version_id,
            "now": a.now,
        },
    )
    if result.rowcount != 1:
        raise LookupError("schedule_state has no row 1 (migrate seeds it)")


async def _apply_collection(a: _Activation) -> None:
    for entry in a.proposed.watchlist:
        await instance_for_company(a.conn, entry.watchlist_company_id)
    run = await load_run_settings(a.conn, a.version_id)
    await ensure_cells(a.conn, run, await list_instances(a.conn))


async def _apply_admission(a: _Activation) -> None:
    """The flip only (step 7); the expiry sweep of `max_ad_age_days` needs targets (M3, Deviation 10)."""


async def _apply_execution(a: _Activation) -> None:
    a.flips["execution_config_id"] = a.ids.execution_config_id


async def _apply_evaluation_policy(a: _Activation) -> None:
    a.flips["evaluation_policy_id"] = a.ids.evaluation_policy_id


_ACTIONS: Final[Mapping[EffectClass, Callable[[_Activation], Awaitable[None]]]] = MappingProxyType(
    {
        "llm_tuple": _apply_llm_tuple,
        "scope": _apply_scope,
        "budget": _apply_budget,
        "schedule": _apply_schedule,
        "collection": _apply_collection,
        "admission": _apply_admission,
        "execution": _apply_execution,
        "evaluation_policy": _apply_evaluation_policy,
    }
)
# §13.3: `llm_tuple`, then `scope`, then the others (module docstring, step 6).
ACTION_ORDER: Final[tuple[EffectClass, ...]] = tuple(_ACTIONS)


def _validation_failed(errors: list[FieldError]) -> Problem:
    return Problem(422, "validation_failed", "Settings validation failed", errors=errors)


async def save_settings(
    conn: AsyncConnection, req: SaveRequest, *, now: datetime, defer: Deferrer, internal: bool = False
) -> SaveOutcome:
    """Save and activate `req` in the caller's transaction (module docstring, steps 1 to 7)."""
    active = (await conn.execute(_LOCK_ACTIVE)).one()
    if req.base_version_id != active.settings_version_id:
        raise Problem(
            409,
            "stale_base_version",
            "The settings changed since this edit began",
            detail="reload the active settings and make the change again",
        )
    stored = (await conn.execute(_VERSION, {"id": active.settings_version_id})).one()
    before = Settings.model_validate(stored.payload)

    result = await dry_run(conn, req.payload, active=before, now=now)
    if result.errors:
        raise _validation_failed([FieldError(e.field, e.code, e.message) for e in result.errors])
    if internal:
        acknowledged = sorted(stored.acknowledged_warnings)
    else:
        missing = [
            code
            for code in dict.fromkeys(w.code for w in result.warnings)
            if code not in req.acknowledged_warnings
        ]
        if missing:
            raise _validation_failed(
                [FieldError(ACKNOWLEDGED_FIELD, NOT_ACKNOWLEDGED, code) for code in missing]
            )
        acknowledged = sorted(req.acknowledged_warnings)
    proposed = Settings.model_validate(req.payload)  # valid: the dry-run found no error
    effects = result.effects
    if effects & _GATED:
        await _check_eval_gate(conn, req)

    payload = proposed.to_payload()
    ids = await ensure_derived(
        conn, derived(proposed, profile=await active_profile(conn, active.profile_version_id))
    )
    version = (
        await conn.execute(
            _INSERT_VERSION,
            {
                "schema_version": SETTINGS_SCHEMA_VERSION,
                "payload": payload,
                "scope_version_id": ids.scope_version_id,
                "model_policy_id": ids.model_policy_id,
                "execution_config_id": ids.execution_config_id,
                "evaluation_policy_id": ids.evaluation_policy_id,
                "base_version_id": req.base_version_id,
                "effect_classes": sorted(effects),
                "acknowledged_warnings": acknowledged,
                "eval_override_ack": req.eval_override_ack,
                "now": now,
            },
        )
    ).one()

    a = _Activation(
        conn=conn,
        req=req,
        now=now,
        defer=defer,
        active=active,
        before=before,
        proposed=proposed,
        payload=payload,
        ids=ids,
        version_id=int(version.id),
    )
    for effect in ACTION_ORDER:
        if effect in effects:
            await _ACTIONS[effect](a)
        if effect == "llm_tuple":
            await afault_point(AFTER_LLM_TUPLE_POINT)
    if a.scope_is_active:  # other domain rows: after the month row, before `ranking_state` (step 6)
        await scope_apply.resolve_apply_failed(conn)
    if a.tuple_flipped:
        await ranking.bump(conn)
    await conn.execute(
        _FLIP,
        {
            "settings_version_id": a.version_id,
            **{column: a.flips.get(column) for column in _FLIPPABLE},
            "now": now,
        },
    )
    return SaveOutcome(
        version_id=a.version_id,
        version_no=int(version.version_no),
        effects=effects,
        apply_scope_operation_id=a.apply_scope_operation_id,
    )
