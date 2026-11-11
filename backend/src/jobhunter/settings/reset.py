"""The default settings put back (spec §20.4 step 8; plan M5 Task 16, P4, Deviation 24): delete-everything's
re-pointing of `active_config`.

`reset_to_defaults(conn, *, now) -> int` is a dedicated path, not `settings.save.save_settings`: even an
internal save runs the eval gate, and its `llm_tuple` action would create a tuple of the still-active profile,
a row above the manifest's bound that would keep the profile from being deleted. Here there is no activation
gate and no scoring work, in the caller's transaction, which holds `active_config FOR UPDATE` and bumps the
ranking after it (the last lock):

1. the shipped default document (`seed.load_settings_v1()`), validated as `Settings`;
2. its derived rows without a profile (`derive.derived(settings, profile=None)`, `ensure_derived` at `now`):
   the scope version, model policy, execution config and evaluation policy, each found by its content hash
   or inserted (above the purge's bound);
3. one `settings_versions` row: the next `version_no`, the stored payload `Settings.to_payload()`, the
   derived ids, no base version, `effect_classes = '{none}'`, no acknowledged warning, `created_at = now`;
4. one `UPDATE active_config` to its ids, with `profile_version_id`, `llm_tuple_id` and
   `pending_scope_version_id` NULL; the rubric and prompt-schema versions are left as they are;
5. `schedule_state` reset by the save's own `schedule` action (`settings.save.reset_schedule`, §3.4): the
   anchor on `now`'s date in the display zone, the interval, `enabled`, the cursor at the latest slot at or
   before `now`, and the new version.

It creates no tuple, no operation and no bulk operation, and returns the new version's id. Ids only reach a
log line.
"""

from datetime import datetime
from typing import Final

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db import seed
from jobhunter.db.seed import SETTINGS_SCHEMA_VERSION
from jobhunter.db.sql import q
from jobhunter.observability.logging import get_logger
from jobhunter.settings.derive import derived, ensure_derived
from jobhunter.settings.model import Settings
from jobhunter.settings.save import reset_schedule

_INSERT_VERSION: Final = q(
    "INSERT INTO settings_versions (version_no, schema_version, payload, scope_version_id, model_policy_id, "
    "execution_config_id, evaluation_policy_id, base_version_id, effect_classes, acknowledged_warnings, "
    "eval_override_ack, created_at) "
    "SELECT coalesce(max(version_no), 0) + 1, :schema_version, :payload, :scope_version_id, "
    ":model_policy_id, :execution_config_id, :evaluation_policy_id, NULL, '{none}', '{}', false, :now "
    "FROM settings_versions RETURNING id",
    jsonb=["payload"],
)
_POINT: Final = q(
    "UPDATE active_config SET settings_version_id = :settings_version_id, "
    "scope_version_id = :scope_version_id, pending_scope_version_id = NULL, "
    "model_policy_id = :model_policy_id, "
    "execution_config_id = :execution_config_id, evaluation_policy_id = :evaluation_policy_id, "
    "profile_version_id = NULL, llm_tuple_id = NULL, updated_at = :now WHERE id = 1"
)

log = get_logger("jobhunter.settings.reset")


async def reset_to_defaults(conn: AsyncConnection, *, now: datetime) -> int:
    """Install the default settings version and point `active_config` and `schedule_state` at it, in the
    caller's transaction, which holds `active_config FOR UPDATE` (module docstring); the new version's id.
    `LookupError` when `active_config` or `schedule_state` has no row (`migrate` seeds both)."""
    settings = Settings.model_validate(seed.load_settings_v1())
    ids = await ensure_derived(conn, derived(settings, profile=None), now=now)
    params = {
        "scope_version_id": ids.scope_version_id,
        "model_policy_id": ids.model_policy_id,
        "execution_config_id": ids.execution_config_id,
        "evaluation_policy_id": ids.evaluation_policy_id,
        "now": now,
    }
    version_id = int(
        (
            await conn.execute(
                _INSERT_VERSION,
                {"schema_version": SETTINGS_SCHEMA_VERSION, "payload": settings.to_payload(), **params},
            )
        ).scalar_one()
    )
    if (await conn.execute(_POINT, {"settings_version_id": version_id, **params})).rowcount != 1:
        raise LookupError("active_config has no row 1 (migrate seeds it)")
    await reset_schedule(conn, settings, version_id=version_id, now=now)
    log.info(
        "settings_reset_to_defaults",
        settings_version_id=version_id,
        scope_version_id=ids.scope_version_id,
        model_policy_id=ids.model_policy_id,
        execution_config_id=ids.execution_config_id,
        evaluation_policy_id=ids.evaluation_policy_id,
    )
    return version_id
