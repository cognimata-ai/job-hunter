"""Task 25: M1 acceptance through the real worker process (`jobhunter worker` in fake mode, spec §18.4): a due
schedule slot over the end-to-end inputs runs the real collection graph on the fixtures and the fake embedder,
and the worker then stops cleanly on SIGTERM.

The inputs are those of `tests/integration/test_collect_end_to_end.py` (the query `software engineer`, the
watchlist boards `getyourguide` on Greenhouse and `bunq` on Recruitee, every source switch on); a scheduled
run includes the paid sources (`schedule.include_paid` of the seeded settings).
"""

import json
import re
import signal
import sys
from typing import Any, Final

import pytest
from jobhunter.observability.redact import EMAIL, KEY, UUID

from tests.support.collect import SOURCE_SWITCHES, seed_inputs_sync
from tests.support.pg import PgServer, pg_one
from tests.support.procs import wait_until
from tests.support.sources import served_requests

pytestmark = [pytest.mark.crash, pytest.mark.slow]
WORKER: Final = [sys.executable, "-m", "jobhunter.cli", "worker"]
REPORT_SECTIONS: Final = {"sources", "identity", "revisions", "quota", "execution"}
# The redactor's own exemption (`redact._mask_key_like`): a run of lower-case words joined by underscores is
# an identifier (an event name), not a key.
SNAKE_CASE_WORDS: Final = re.compile(r"[a-z]+(?:_[a-z]+)+")
# Every request of the run, by fixture case (tests/contract/README.md, fake-mode routes): JSearch page 1 and
# its cursor page, Adzuna page 1 and the empty page after it, the three Arbeitnow pages, the two boards, and
# `plan_fallback`'s free SerpApi `account.json` read of a paid run (§5.3.2), which plans no search here.
SERVED_CASES: Final = (
    "account_before",
    "adzuna_reingest_pair",
    "greenhouse_getyourguide",
    "page1",
    "page2",
    "page_end",
    "recruitee_bunq",
    "search_empty",
    "search_v2_page1",
    "search_v2_page2",
)


def one(server: PgServer, statement: str, *params: object) -> tuple[Any, ...] | None:
    return pg_one(server, "superuser", statement, *params)


def due_slot_now(server: PgServer) -> None:
    """The schedule's latest slot is due now, every earlier one evaluated: one scheduled run, none missed."""
    one(
        server,
        "UPDATE schedule_state SET anchor_utc = date_trunc('minute', now()), interval_s = 3600, "
        "last_evaluated_slot = date_trunc('minute', now()) - interval '1 hour'",
    )


def log_event(line: str) -> dict[str, Any] | None:
    """The JSON object a log line holds, or `None` for any other line."""
    try:
        value = json.loads(line)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def key_shapes(line: str) -> list[str]:
    """The key-like tokens of `line` that the redactor would mask (its UUID spans and identifiers exempt)."""
    return [
        token
        for part in UUID.split(line)
        for token in KEY.findall(part)
        if not SNAKE_CASE_WORDS.fullmatch(token)
    ]


def test_scheduled_run_on_fixtures_through_the_worker_process(
    migrated, proc_env, procs, fake_embedder, fixture_log
) -> None:
    seed_inputs_sync(
        migrated,
        queries=("software engineer",),
        companies=(("greenhouse", "getyourguide"), ("recruitee", "bunq")),
        enabled=SOURCE_SWITCHES,
    )
    due_slot_now(migrated)
    w = procs.spawn(WORKER, proc_env("worker", migrated, JOBHUNTER_FIXTURE_LOG=str(fixture_log)))
    w.wait_event("worker_ready", 60)
    wait_until(
        lambda: (
            one(
                migrated,
                "SELECT status IN ('succeeded', 'partial', 'failed') FROM runs "
                "WHERE scheduled_slot IS NOT NULL",
            )
            == (True,)
        ),
        180,
        step_s=1.0,
    )
    status, reason, trigger, report = one(
        migrated, "SELECT status, status_reason, trigger, report FROM runs WHERE scheduled_slot IS NOT NULL"
    ) or (None, None, None, None)
    # the fixtures reject no item (tests/contract/README.md), so the run is fully successful
    assert (status, reason, trigger) == ("succeeded", None, "schedule")
    assert set(report) == REPORT_SECTIONS
    assert all(v["ok"] for v in report["execution"]["invariants"].values())
    assert one(migrated, "SELECT count(*) FROM runs WHERE status = 'missed'") == (0,)
    jobs, pending, unembedded = one(
        migrated,
        "SELECT (SELECT count(*) FROM jobs), "
        "(SELECT count(*) FROM raw_items WHERE disposition = 'pending'), "
        "(SELECT count(*) FROM job_revisions WHERE embedding_status <> 'embedded')",
    ) or (0, None, None)
    assert jobs > 0 and pending == 0 and unembedded == 0
    assert one(migrated, "SELECT count(*) FROM quota_ledger WHERE state = 'billed'") == (4,)
    # every request went to the fixture transport, each planned page exactly once, from this worker
    served = served_requests(fixture_log)
    assert sorted(r["case"] for r in served) == list(SERVED_CASES)
    assert {int(r["pid"]) for r in served} == {w.pid}

    w.signal(signal.SIGTERM)
    assert w.exit_within(60) == 0
    assert w.wait_event("worker_shutdown_complete", 1)["exit_code"] == 0
    lines = w.output().splitlines()
    events = [event for line in lines if (event := log_event(line)) is not None]
    assert events and [e for e in events if e.get("level") == "error" and "error_class" not in e] == []
    assert [line for line in lines if EMAIL.search(line)] == []
    assert [(line, shapes) for line in lines if (shapes := key_shapes(line))] == []
