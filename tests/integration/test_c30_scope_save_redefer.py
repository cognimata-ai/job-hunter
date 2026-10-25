"""Crash case C30 (spec §18.3; plan Task 18), in process: a scope save committed, but the procrastinate job of
its `apply_scope` operation was lost (the job row is deleted to simulate it). The `operations` reconciler
re-defers it once the operation is `REDEFER_AFTER` (2 min) old, under its queueing lock `op:<id>`, and the
apply then flips the scope."""

from datetime import timedelta

import pytest
from jobhunter.clock import utcnow

from tests.support.reconcile import run_reconciler
from tests.support.settings import (
    active_scope_id,
    delete_procrastinate_job_of,
    procrastinate_job_exists_for,
    save_scope,
    scope_ids_of,
)

pytestmark = pytest.mark.integration


async def test_scope_save_without_its_job_is_redeferred_and_flips(
    logged_in_client, engine, current_epoch, op_runner
) -> None:
    saved = await save_scope(logged_in_client, radius_km=40)
    op_id = saved["apply_scope_operation_id"]
    await delete_procrastinate_job_of(engine, op_id)  # the commit happened, its job did not
    await run_reconciler(  # tests/support/reconcile.py
        "operations", engine, now=utcnow() + timedelta(minutes=2, seconds=1), epoch=current_epoch
    )
    assert await procrastinate_job_exists_for(engine, op_id)  # re-deferred under its `op:<id>` queueing lock
    await op_runner.run_one(op_id)
    assert await active_scope_id(engine) == (await scope_ids_of(engine, saved))[0]
