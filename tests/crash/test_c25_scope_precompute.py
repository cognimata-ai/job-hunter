"""Crash case C25 (spec §18.3; plan Task 18): a real worker dies inside the scope precompute.

The save commits the pending pointer and the `apply_scope` operation (the save path, in its own transaction);
worker A claims it and is killed at `scope_apply.after_chunk`, right after its first chunk committed (exit 86,
no cleanup). The pointer has not flipped and the active scope's view stays complete. Worker B's takeover sweep
gives the operation back (`pending`, one more attempt) and its start releases A's stalled job (`stalled_jobs`,
as in C27, once A's procrastinate heartbeat is stale); the retry computes the rest and flips.
"""

import signal
import sys
from typing import Final

import pytest

from tests.support.pg import pg_one
from tests.support.procs import wait_until
from tests.support.settings import (
    active_scope_sync,
    incomplete_jobs_sync,
    operation_status_sync,
    pending_scope_sync,
    save_scope_sync,
)

pytestmark = [pytest.mark.crash, pytest.mark.slow]
WORKER: Final = [sys.executable, "-m", "jobhunter.cli", "worker"]


def test_crash_during_precompute_never_flips_and_the_retry_completes(
    migrated, proc_env, procs, seeded_jobs_sync
) -> None:
    sid, op_id = save_scope_sync(migrated, radius_km=60)  # the save path, in its own transaction
    first = procs.spawn(WORKER, proc_env("worker", migrated, JOBHUNTER_FAULT="scope_apply.after_chunk"))
    assert first.exit_within(120) == 86  # killed after one committed chunk
    assert active_scope_sync(migrated) != sid and pending_scope_sync(migrated) == sid
    assert incomplete_jobs_sync(migrated, active_scope_sync(migrated)) == 0  # no blank interval in the view
    assert 0 < incomplete_jobs_sync(migrated, sid) < len(seeded_jobs_sync)  # one chunk of three is done
    # A's job is stalled, as in M0's C27
    pg_one(
        migrated,
        "superuser",
        "UPDATE procrastinate_workers SET last_heartbeat = now() - interval '5 minutes'",
    )
    second = procs.spawn(WORKER, proc_env("worker", migrated))  # the takeover sweep re-queues the operation
    wait_until(lambda: active_scope_sync(migrated) == sid, 120)
    assert pending_scope_sync(migrated) is None and operation_status_sync(migrated, op_id) == "succeeded"
    assert incomplete_jobs_sync(migrated, sid) == 0
    second.signal(signal.SIGTERM)
    assert second.exit_within(60) == 0
