"""The default settings put back (spec §20.4 step 8; plan M5 Task 16, P4, Deviation 24): `settings.reset`.

`reset_to_defaults` is delete-everything's re-pointing of `active_config`: the shipped default document, its
derived rows found by hash (or inserted), one new settings version and `schedule_state` reset onto it, with no
activation gate and no scoring work, so it creates no tuple and no operation (a save would create a tuple of
the active profile, a row above the purge's bound). Each test runs it as the purge does: in a transaction that
holds `active_config FOR UPDATE`. Every datum is invented.
"""

import asyncio
import copy
from datetime import datetime
from typing import Any, Final, cast

import psycopg
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db import seed
from jobhunter.db.seed import anchor_utc_for, ensure_hashed, latest_slot_at_or_before, load_settings_v1
from jobhunter.observability.deadman import DEADMAN_LOCK_KEY
from jobhunter.privacy.kinds import delete_everything
from jobhunter.settings import validate
from jobhunter.settings.derive import derived, ensure_derived
from jobhunter.settings.model import Settings
from jobhunter.settings.reset import reset_to_defaults
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tests.support.pg import PgServer, blocked_or_done
from tests.support.procs import await_until
from tests.support.purges import Purger, completed
from tests.support.scoring import activate_score_tuple

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("current_epoch")]

_LOCK_CONFIG: Final = sa.text("SELECT 1 FROM active_config WHERE id = 1 FOR UPDATE")
_ACTIVE: Final = sa.text(
    "SELECT settings_version_id, scope_version_id, pending_scope_version_id, model_policy_id, "
    "execution_config_id, evaluation_policy_id, rubric_version_id, prompt_schema_version_id, "
    "profile_version_id, llm_tuple_id FROM active_config WHERE id = 1"
)
_VERSION: Final = sa.text(
    "SELECT version_no, payload, scope_version_id, model_policy_id, execution_config_id, "
    "evaluation_policy_id, base_version_id, effect_classes, created_at FROM settings_versions WHERE id = :v"
)
_SCHEDULE: Final = sa.text(
    "SELECT anchor_utc, interval_s, enabled, last_evaluated_slot, settings_version_id FROM schedule_state"
)
_COUNTS: Final = sa.text(
    "SELECT (SELECT count(*) FROM llm_tuples) AS tuples, (SELECT count(*) FROM operations) AS operations, "
    "(SELECT count(*) FROM bulk_operations) AS bulk, (SELECT max(version_no) FROM settings_versions) AS top, "
    "(SELECT count(*) FROM scope_versions) AS scopes, (SELECT count(*) FROM model_policies) AS policies, "
    "(SELECT count(*) FROM execution_configs) AS executions, "
    "(SELECT count(*) FROM evaluation_policies) AS evaluations"
)
_POINT_MODEL_POLICY: Final = sa.text("UPDATE active_config SET model_policy_id = :p WHERE id = 1")
_IDEMPOTENCY_ROW: Final = sa.text(
    "INSERT INTO api_idempotency (route, idem_key, request_sha256, status, created_at, expires_at) "
    "VALUES ('POST /api/privacy/delete-everything', 'reset-test-key-0001', '\\x01', 'in_progress', now(), "
    "now() + interval '1 day') RETURNING id"
)


async def one(engine: AsyncEngine, statement: sa.TextClause, **params: object) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(statement, params)).one()


async def reset(engine: AsyncEngine, now: datetime) -> int:
    """The reset as the purge's step 8 runs it: under `active_config FOR UPDATE`."""
    async with engine.begin() as conn:
        await conn.execute(_LOCK_CONFIG)
        return await reset_to_defaults(conn, now=now)


def default_settings() -> Settings:
    return Settings.model_validate(load_settings_v1())


async def default_rows(conn: AsyncConnection) -> Any:
    """The derived rows of the default document (found by hash; `migrate` seeded them)."""
    return await ensure_derived(conn, derived(default_settings(), profile=None))


@pytest.mark.parametrize("enabled", [False, True], ids=["a_disabled_default", "the_enabled_default"])
async def test_a_reset_to_a_disabled_schedule_waits_for_a_dead_man_send(
    engine: AsyncEngine, migrated: PgServer, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    """Final re-review N8 (final fix C9): the reset writes `schedule_state` as the save's `schedule` action
    does, so a default that disables the schedule first takes the dead-man lock as a transaction lock: it
    waits for a send in progress (here, a session holding the lock), and no ping follows a disable it commits.
    The enabled default, the one shipped, takes no lock."""
    shipped = load_settings_v1()

    def default() -> dict[str, Any]:
        document = cast(dict[str, Any], copy.deepcopy(shipped))
        document["schedule"]["enabled"] = enabled
        return document

    monkeypatch.setattr(seed, "load_settings_v1", default)
    with psycopg.connect(migrated.dsn("app"), autocommit=True) as sender:
        sender.execute("SELECT pg_advisory_lock(%s::bigint)", (DEADMAN_LOCK_KEY,))
        task = asyncio.create_task(reset(engine, utcnow()))
        try:
            await await_until(lambda: blocked_or_done(migrated, task), 10)
            assert task.done() is enabled
        finally:
            sender.execute("SELECT pg_advisory_unlock(%s::bigint)", (DEADMAN_LOCK_KEY,))
        await asyncio.wait_for(task, 10)


async def test_the_reset_needs_no_gate_and_creates_no_tuple_or_operation(engine: AsyncEngine) -> None:
    """An active profile with its tuple, an active model policy other than the default one, and a
    prompt-schema version that no live schema dry-run verified: a save of the default document would meet
    the gate and create a tuple of the profile. The reset succeeds, adds no tuple, no operation and no bulk
    operation, and points `active_config` at the defaults with the profile, tuple and pending scope NULL;
    the rubric and prompt-schema versions are left as they are."""
    async with engine.begin() as conn:
        await activate_score_tuple(conn)
        policy = cast(dict[str, Any], copy.deepcopy(load_settings_v1()["scoring"]))["model_policy"]
        policy["nodes"]["critic"]["model"] = "claude-sonnet-5-5"  # the shipped policy's critic is Opus
        changed = await ensure_hashed(conn, "model_policies", policy)
        await conn.execute(_POINT_MODEL_POLICY, {"p": changed})
    before = await one(engine, _ACTIVE)
    assert before.profile_version_id is not None and before.llm_tuple_id is not None
    async with engine.connect() as conn:
        defaults = await default_rows(conn)
        verified = await validate.schema_verified(
            conn,
            prompt_schema_version_id=before.prompt_schema_version_id,
            model_policy_id=defaults.model_policy_id,
        )
        assert not verified  # a save that changed the tuple would be refused here
    assert before.model_policy_id == changed != defaults.model_policy_id
    counts = await one(engine, _COUNTS)
    now = utcnow()

    version_id = await reset(engine, now)

    after = await one(engine, _ACTIVE)
    assert (after.settings_version_id, after.scope_version_id, after.model_policy_id) == (
        version_id,
        defaults.scope_version_id,
        defaults.model_policy_id,
    )
    assert (after.execution_config_id, after.evaluation_policy_id) == (
        defaults.execution_config_id,
        defaults.evaluation_policy_id,
    )
    assert (after.profile_version_id, after.llm_tuple_id, after.pending_scope_version_id) == (
        None,
        None,
        None,
    )
    assert (after.rubric_version_id, after.prompt_schema_version_id) == (
        before.rubric_version_id,
        before.prompt_schema_version_id,
    )
    now_counts = await one(engine, _COUNTS)
    assert (now_counts.tuples, now_counts.operations, now_counts.bulk) == (
        counts.tuples,
        counts.operations,
        counts.bulk,
    )
    version = await one(engine, _VERSION, v=version_id)
    assert version.version_no == counts.top + 1 and version.base_version_id is None
    assert version.effect_classes == ["none"] and version.created_at == now
    assert version.payload == default_settings().to_payload()


async def test_the_reset_reuses_existing_derived_rows(engine: AsyncEngine, purger: Purger) -> None:
    """The default document's derived rows exist (settings version 1's): the reset finds each by hash and
    inserts none, and `schedule_state` follows the new version as a save's `schedule` action resets it. A
    delete-everything that ran it is satisfied straight after its completion: every row the reset reused lies
    in its manifest's `retained` set, and its step-8 postcondition holds."""
    async with engine.connect() as conn:
        defaults = await default_rows(conn)
    counts = await one(engine, _COUNTS)
    now = utcnow()
    version_id = await reset(engine, now)
    after = await one(engine, _COUNTS)
    assert (after.scopes, after.policies, after.executions, after.evaluations) == (
        counts.scopes,
        counts.policies,
        counts.executions,
        counts.evaluations,
    )
    version = await one(engine, _VERSION, v=version_id)
    assert (
        version.scope_version_id,
        version.model_policy_id,
        version.execution_config_id,
        version.evaluation_policy_id,
    ) == (
        defaults.scope_version_id,
        defaults.model_policy_id,
        defaults.execution_config_id,
        defaults.evaluation_policy_id,
    )
    schedule = await one(engine, _SCHEDULE)
    settings = default_settings()
    anchor = anchor_utc_for(now, settings.schedule.anchor_local, settings.display.timezone)
    interval_s = settings.schedule.interval_hours * 3600
    assert tuple(schedule) == (
        anchor,
        interval_s,
        settings.schedule.enabled,
        latest_slot_at_or_before(anchor, interval_s, now),
        version_id,
    )

    # the delete-everything that runs it, on this database
    async with engine.begin() as conn:
        idempotency_row_id = int((await conn.execute(_IDEMPOTENCY_ROW)).scalar_one())
        opened = await delete_everything.request_delete_everything(
            conn, now=utcnow(), idempotency_row_id=idempotency_row_id, defer=None
        )
    await purger.run(opened.operation_id)
    manifest = await completed(purger, opened.manifest_id, opened.operation_id)
    assert manifest.scope is not None
    retained = manifest.scope["retained"]
    active = await one(engine, _ACTIVE)
    assert active.settings_version_id > manifest.scope["max_ids"]["settings_versions"]  # a new version
    for table, row_id in (
        ("scope_versions", defaults.scope_version_id),
        ("model_policies", defaults.model_policy_id),
        ("execution_configs", defaults.execution_config_id),
        ("evaluation_policies", defaults.evaluation_policy_id),
        ("rubric_versions", active.rubric_version_id),
        ("prompt_schema_versions", active.prompt_schema_version_id),
    ):
        assert row_id in retained[table], table
    assert (active.scope_version_id, active.model_policy_id) == (
        defaults.scope_version_id,
        defaults.model_policy_id,
    )
    assert retained["settings_versions"] == retained["profile_versions"] == retained["llm_tuples"] == []
    async with engine.connect() as conn:
        assert await delete_everything.DeleteEverything().postconditions(conn, manifest.scope) == []
