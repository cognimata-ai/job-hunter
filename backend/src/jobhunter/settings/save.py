"""The settings save (spec §13.2, §13.3; plan Task 17, Deviations 10, 14 and 15; M3 Task 14, Codex r1 #35,
r2 B7).

`save_settings` writes a new immutable `settings_versions` row and activates it, in the caller's transaction:

1. `active_config` `FOR UPDATE`, the first lock (§9.1). A caller that already holds it (the save route,
   Deviation 15's internal saves) makes this a no-op; a second save waits here for the first one's commit.
2. A `base_version_id` other than the active version is 409 `stale_base_version` (§13.2): of two tabs saving
   from one base, the second waits at step 1 and fails here, never deadlocks.
3. The dry-run again (`dryrun.dry_run`, §13.2), on the payload against the active settings: its errors (the
   field rules, and `queries[]`/`watchlist[]` entries that name no row) are 422 `validation_failed` with each
   issue's path, code and message; each warning code missing from `acknowledged_warnings` is 422
   `validation_failed` with `RefusalField("acknowledged_warnings", "warning_not_acknowledged", <code>)`. An
   internal save (`internal=True`, Deviation 15) skips the acknowledgement check and copies the active
   version's acknowledgements. The classes are the dry-run's with `forced_effects` merged in (M3 Task 21):
   the shipped-version activation (`settings.activate`) saves the unchanged document with `llm_tuple` forced,
   since it changes a pointer no field of the document holds, so the derived classes alone would be `none`
   (the merge drops `none`). Then, once, when the classes hold `llm_tuple` or `evaluation_policy`
   (`dryrun.GATED`; `_check_eval_gate`, M3 Task 20):
   - an `llm_tuple` save whose model policy is not verified for the active prompt-schema version is 409
     `conflict_state` with `detail = "schema_dry_run_required"` and a title naming the (schema, model) pairs
     without a pass, whatever the override (Codex r2 B24, Ruling T20-a): the policy's row is found or
     inserted by its content hash (`ensure_hashed`, under the step-1 lock), then `validate.schema_verified`
     reads it, so a new policy that reuses verified models saves with no new run, and one with a new model
     is unblocked by the schema dry-run of the payload (`jobhunter schema-dry-run --candidate-payload`). A
     process on the fake LLM skips it, as the score lane's guard does (`cycles.pause_reason`): no live
     dry-run exists there;
   - the eval gate of the payload's candidate (`derive.candidate_of`, `validate.eval_gate_state`): a failed
     gate (with 30 of the user's active golden cases, no passed run of the candidate is a failed gate)
     without `eval_override_ack` is 409 `eval_gate_failed`.
   Every refusal therefore comes before the version and its actions; the policy row a refused save inserted
   rolls back with it (the route's transaction; an internal save's caller's).
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
     `llm_tuple_id := it` (flipped in step 7; §12.6 (d) to (h) are step 8's);
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
   - `admission`: the flip and, when `scope.max_ad_age_days` changed, the expiry sweep of the active tuple in
     both directions (`scoring.targets.expiry_sweep`, M3 Task 13: the bulk rows, then the target rows);
   - `execution`: `execution_config_id := new`;
   - `evaluation_policy`: `evaluation_policy_id := new` (the row is step 4's).
7. `settings_version_id := new` and `updated_at`, in the statement that flips the pointers of step 6
   (`_FLIP`). `active_config` is locked since step 1, so this takes no new lock.
8. When the tuple pointer changed, the tuple activation (`scoring.targets.activate_tuple`, §12.6 (d) to (f)
   and (h), §13.3 `llm_tuple`; M3 Task 14, Codex r1 #35): the month row, the old tuple's bulk operation
   cancelled and the new one opened under the new settings, the old tuple's unpublished targets and in-flight
   submissions cancelled, and `create_targets_for_tuple` recorded. It runs after `_FLIP` because it needs the
   flipped pointer.
9. When the tuple pointer changed or the `admission` action's sweep moved a target, `ranking.bump` once, the
   save's last statement: `ranking_state` is the last lock of §3.3.3, after the month row of the `budget`
   action and of the activation (Codex r2 B7).

The class actions do not keep the schedule's order among themselves, and this is what actually happens. The
derived rows, the version and the tuple are new rows of tables that only writers holding `active_config` `FOR
UPDATE` insert into, so no other transaction can hold or wait for them. With `budget` the month row comes
before every other domain row of the actions. With `admission` the expiry sweep's bulk rows and target rows
and `resolve_apply_failed`'s alerts come before the activation's month row of step 8 whenever the month row
was not taken by `budget` already. That is safe because `active_config FOR UPDATE`, held from step 1,
serialises the save with every score-lane transaction (each takes `active_config` first, step 2), the only
transactions that take a month row together with bulk or target rows; and none of them, nor any other
month-row writer, waits for a `scope.apply_failed` alert. `ranking_state` is always the last lock.

Every refusal is a `jobhunter.errors.Refusal` (M4 Task 1, R6), which the api answers with its problem.

`ensure_tuple` is the one select-or-insert of an `llm_tuples` row (Task 21's confirm reuses it). Neither this
module nor `scope_apply` logs: the caller logs ids and codes after its commit. (`spend_ledger.change_cap` and
`change_buffer` log the month row's change in the transaction, as every month-row writer of Task 3 does.)
"""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Final

from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db import ranking
from jobhunter.db.seed import (
    SETTINGS_SCHEMA_VERSION,
    anchor_utc_for,
    ensure_hashed,
    ensure_id,
    latest_slot_at_or_before,
)
from jobhunter.db.sql import q
from jobhunter.errors import Refusal, RefusalField
from jobhunter.faults import afault_point
from jobhunter.llm import budget, spend_ledger
from jobhunter.llm.budget import BudgetParams
from jobhunter.quota.cells import ensure_cells
from jobhunter.run_settings import load_run_settings
from jobhunter.scoring import targets
from jobhunter.settings import scope_apply
from jobhunter.settings.derive import DerivedIds, active_profile, candidate_of, derived, ensure_derived
from jobhunter.settings.dryrun import GATED, dry_run
from jobhunter.settings.effects import EffectClass
from jobhunter.settings.model import Settings
from jobhunter.settings.validate import eval_gate_state, fake_llm_process, schema_verified, unverified_pairs
from jobhunter.sources.instances import instance_for_company, list_instances
from jobhunter.types import JSONValue
from jobhunter.worker.tasks import Deferrer

AFTER_LLM_TUPLE_POINT: Final = "settings.after_llm_tuple"
SCHEMA_DRY_RUN_REQUIRED: Final = "schema_dry_run_required"
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
_NONE: Final[frozenset[EffectClass]] = frozenset({"none"})


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
    """The new version, the classes it applied, the `apply_scope` operation its scope needs (or `None`), and
    the tuple active after it (`None` while no profile is active)."""

    version_id: int
    version_no: int
    effects: frozenset[EffectClass]
    apply_scope_operation_id: int | None
    llm_tuple_id: int | None


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
    targets_moved: bool = False  # the `admission` action's expiry sweep moved a target
    apply_scope_operation_id: int | None = None
    scope_is_active: bool = False  # the `scope` action's desired scope is the active one


async def _unverified_pairs(conn: AsyncConnection, proposed: Settings) -> list[tuple[str, str]]:
    """The (schema, model) pairs of the proposed model policy without a passed live schema dry-run of the
    active prompt-schema version, none when it is verified (module docstring, step 3; Ruling T20-a): its row
    found or inserted by hash in the save's transaction, then Task 11's `validate.schema_verified` on it."""
    model_policy_id = await ensure_hashed(conn, "model_policies", derived(proposed).model_policy)
    pointers = (await conn.execute(_TUPLE_POINTERS)).one()
    if pointers.prompt_schema_version_id is None:
        raise LookupError("active_config has no prompt-schema version (migrate seeds it)")
    version = int(pointers.prompt_schema_version_id)
    if await schema_verified(conn, prompt_schema_version_id=version, model_policy_id=model_policy_id):
        return []
    return await unverified_pairs(
        conn, prompt_schema_version_id=version, model_policy=proposed.scoring.model_policy
    )


def schema_dry_run_required(missing: Sequence[tuple[str, str]]) -> Refusal:
    """409 `conflict_state` (`schema_dry_run_required`) of a model policy without a passed live schema
    dry-run of the (schema, model) pairs `missing`, which its title names (the save, and the shipped-version
    activation's step 4, `settings.activate`)."""
    pairs = ", ".join(f"{schema} on {model}" for schema, model in missing)
    return Refusal(
        409,
        "conflict_state",
        f"The model policy has no passed live schema dry-run for {pairs}",
        detail=SCHEMA_DRY_RUN_REQUIRED,
    )


def eval_gate_failed() -> Refusal:
    """409 `eval_gate_failed` of a gated change whose candidate failed the eval gate, without the override."""
    return Refusal(
        409,
        "eval_gate_failed",
        "The eval gate failed",
        detail="acknowledge the override (eval_override_ack) to activate this change",
    )


async def _check_eval_gate(
    conn: AsyncConnection, req: SaveRequest, proposed: Settings, effects: frozenset[EffectClass]
) -> None:
    """The refusals of a gated save (module docstring, step 3): an unverified model policy, then a failed eval
    gate of the payload's candidate without the override. Below 30 active golden cases the gate is
    `uncalibrated` and does not refuse."""
    if "llm_tuple" in effects and not fake_llm_process():
        missing = await _unverified_pairs(conn, proposed)
        if missing:
            raise schema_dry_run_required(missing)
    candidate = await candidate_of(conn, proposed)
    if await eval_gate_state(conn, candidate=candidate) == "failed" and not req.eval_override_ack:
        raise eval_gate_failed()


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
    """The flip (step 7) and, when `scope.max_ad_age_days` changed, the expiry sweep of the tuple active after
    this save in both directions, under the `active_config` lock the save holds (L4893; M3 Task 13, A2 A5,
    Deviation 24): the candidates' bulk rows, then their target rows (other domain rows), after the `budget`
    action's month row. A sweep that moved a target bumps the ranking (step 6)."""
    max_age = a.proposed.scope.max_ad_age_days
    if max_age == a.before.scope.max_ad_age_days:
        return
    tuple_id = a.flips.get("llm_tuple_id", a.active.llm_tuple_id)
    if not isinstance(tuple_id, int):
        return
    swept = await targets.expiry_sweep(a.conn, llm_tuple_id=tuple_id, max_ad_age_days=max_age, now=a.now)
    a.targets_moved = a.targets_moved or swept.moved > 0


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


def _validation_failed(fields: list[RefusalField]) -> Refusal:
    return Refusal(422, "validation_failed", "Settings validation failed", fields=fields)


async def save_settings(
    conn: AsyncConnection,
    req: SaveRequest,
    *,
    now: datetime,
    defer: Deferrer,
    internal: bool = False,
    forced_effects: frozenset[EffectClass] = frozenset(),
) -> SaveOutcome:
    """Save and activate `req` in the caller's transaction (module docstring, steps 1 to 9), with
    `forced_effects` merged into the dry-run's classes (step 3)."""
    active = (await conn.execute(_LOCK_ACTIVE)).one()
    if req.base_version_id != active.settings_version_id:
        raise Refusal(
            409,
            "stale_base_version",
            "The settings changed since this edit began",
            detail="reload the active settings and make the change again",
        )
    stored = (await conn.execute(_VERSION, {"id": active.settings_version_id})).one()
    before = Settings.model_validate(stored.payload)

    result = await dry_run(conn, req.payload, active=before, now=now)
    if result.errors:
        raise _validation_failed([RefusalField(e.field, e.code, e.message) for e in result.errors])
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
                [RefusalField(ACKNOWLEDGED_FIELD, NOT_ACKNOWLEDGED, code) for code in missing]
            )
        acknowledged = sorted(req.acknowledged_warnings)
    proposed = Settings.model_validate(req.payload)  # valid: the dry-run found no error
    effects = ((result.effects | forced_effects) - _NONE) if forced_effects else result.effects
    if effects & GATED:
        await _check_eval_gate(conn, req, proposed, effects)

    payload = proposed.to_payload()
    ids = await ensure_derived(
        conn, derived(proposed, profile=await active_profile(conn, active.profile_version_id)), now=now
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
    await conn.execute(
        _FLIP,
        {
            "settings_version_id": a.version_id,
            **{column: a.flips.get(column) for column in _FLIPPABLE},
            "now": now,
        },
    )
    if a.tuple_flipped:  # step 8, on the flipped pointer
        old_tuple_id = a.active.llm_tuple_id
        await targets.activate_tuple(
            conn,
            old_tuple_id=old_tuple_id if isinstance(old_tuple_id, int) else None,
            new_tuple_id=a.flips["llm_tuple_id"],
            settings=proposed,
            now=now,
            defer=defer,
        )
    if a.tuple_flipped or a.targets_moved:  # step 9, last
        await ranking.bump(conn)
    tuple_id = a.flips.get("llm_tuple_id", a.active.llm_tuple_id)
    return SaveOutcome(
        version_id=a.version_id,
        version_no=int(version.version_no),
        effects=effects,
        apply_scope_operation_id=a.apply_scope_operation_id,
        llm_tuple_id=tuple_id if isinstance(tuple_id, int) else None,
    )
