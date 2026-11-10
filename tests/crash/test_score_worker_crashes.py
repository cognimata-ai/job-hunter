"""The score lane's crash cases on the real worker (spec §18.3 C9, C14, C17, C29 and §3.3.5's drain; M3
Task 28): `jobhunter worker` children on the fake LLM (`JOBHUNTER_FAKE_LLM=1`, the committed recordings of
`tests/contract/fixtures/anthropic`) and the source fixtures, and `tests.crash.score_worker` where a call must
stall (C14, SIGTERM).

Every case scores pending targets of new synthetic-ad jobs over the score tuple
(`tests.support.scoring.insert_target`). The recordings answer them deterministically: six calls (one
generation, three critic rounds, two revisions) and `published_contested`. The targets exist before the
worker starts, but their tuple is made active only once its start's tick has ended (a tick of an active
tuple with admissible targets starts a `periodic` cycle), at a chosen second of a minute away from the next
tick; then the test asks for a `manual` cycle through the api (`POST /api/system/score-cycle`), so the first
cycle is always that one. A worker that dies leaves its `score_cycle` job `doing`: its procrastinate heartbeat
is made stale, so the next worker's start releases it (`stalled_jobs`, as in C27). The next worker's takeover
sweeps reclaim the dead epoch's claims (`worker_takeover`) and fail its running cycle `orphaned`, each in a
transaction of its own after the epoch claim (Task 27), and its start's tick starts a `periodic` cycle.

- **C9** (`gateway.after_dispatch`): the worker dies with its first try dispatched. The entry stays
  `dispatched` until `llm_ledger_unsettled` finds it past its deadline (the test moves `dispatched_at` back
  past `max(15 min, deadline_s + 5 min)`: the injected deadline), then `ambiguous` (`unsettled`). The second
  worker's takeover reclaims the target; the periodic cycle scores it in a new attempt, which builds its
  context from the rows (the dead attempt left no draft: it generates again); the zombie's publish, replayed
  by hand under the old claim, affects 0 rows.
- **C14** (`claims.beat:sleep=200`, a 400 s stalling generator call): no beat lands while the call stalls,
  so `stuck_targets` reclaims the claim at 3 minutes (`claim_lost('stale_heartbeat')`). The beat comes back
  30 + 200 s after the claim, finds the claim gone and cancels the stalled call (`ambiguous`, `cancelled`,
  `claims.ClaimHeartbeats`): the original attempt never reaches its publish, so its publish is replayed by
  hand under the old claim, and affects 0 rows. The next periodic cycle scores the target in a new attempt;
  both cycles end `completed`, never `failed`. The reclaim must fall between 180 s and 230 s after the claim:
  the claim is made at second 20 to 35 of a minute, and the tick that reclaims comes 205 s to 220 s later.
- **C17** (M0's zombie harness, `score.claim.after_claim:pause`): the worker stops itself right after its
  claim committed, the cycle in flight. Its worker heartbeat is made stale, the second worker takes over,
  reclaims the claim, fails the cycle `orphaned` and its periodic cycle publishes the target. Then the zombie
  is continued: no domain row of the score lane changes (targets, attempts, drafts, critic notes, results
  and their instances, cycles), its checkpoint writes stay on its own cycle's thread, and it exits
  superseded (70) or after its own graceful stop (0), whichever of its watchdog and its fenced epoch sees the
  takeover first. Stopped inside a transaction of its own tick, the zombie would hold rows the takeover
  sweeps wait for until their lock timeout (`takeover_blocked`, M0 ruling T13-b), so the claim is made away
  from the tick (second 10 to 40).
- **C29** (`score.claim.after_claim`): the worker dies after the claim; the takeover fails the cycle
  `orphaned` (its warn alert, which the next completed cycle resolves) and reclaims the targets; the next
  tick's periodic cycle scores them.
- **SIGTERM** during a cycle whose generator call stalls: the stop waits out its grace, cancels what
  remains and settles it (`LLMGateway.settle_in_flight` at the teardown, §3.3.5 step 4), and the worker
  exits 0 with `worker_shutdown_complete`; the in-flight entry is `ambiguous` (`cancelled`), and no state is
  rewritten (the cycle stays `running` and the attempt with it, for the next takeover).

Every datum is invented; no paid call.
"""

import re
import signal
import sys
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Final

import httpx
import pytest
from jobhunter.llm import fake
from jobhunter.scoring import publish
from jobhunter.worker.shutdown import STOP_BUDGET_S
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.pg import PgServer, pg_one, pg_rows
from tests.support.procs import Proc, ProcessGroup, wait_until
from tests.support.profile import idem
from tests.support.scoring import PendingTarget, activate_score_tuple, insert_target
from tests.support.settings import fresh_key

pytestmark = [pytest.mark.crash, pytest.mark.slow]
WORKER: Final = [sys.executable, "-m", "jobhunter.cli", "worker"]
STALLING_WORKER: Final = [sys.executable, "-m", "tests.crash.score_worker"]
FAULT_EXIT: Final = 86
SUPERSEDED_EXIT: Final = 70
READY_WITHIN_S: Final = 60
# A periodic cycle needs the next start's tick, and its attempts the fake's answers: three minutes at most.
SCORED_WITHIN_S: Final = 180
AWAY_FROM_THE_TICK: Final = range(5, 50)  # seconds of a minute
# C14: the stall outlasts the reclaim (3 min) and the beat's return (30 s + 200 s).
C14_STALL_S: Final = 400
C14_BEAT_STALL_S: Final = 200
C14_CLAIM_SECONDS: Final = range(20, 36)  # the reclaiming tick comes 205 s to 220 s after the claim
C17_CLAIM_SECONDS: Final = range(10, 41)
SIGTERM_STALL_S: Final = 600
PUBLISHED: Final = ("published_accepted", "published_contested")
# The score lane's domain rows a zombie must leave as they are (C17), each with every column.
DOMAIN_TABLES: Final = (
    "score_targets",
    "score_attempts",
    "score_drafts",
    "critic_notes",
    "llm_results",
    "det_results",
    "score_cycles",
)
# What a failed wait reports of the lane.
REPORTED: Final = (
    ("cycles", "SELECT id, trigger, worker_epoch, status, status_reason FROM score_cycles ORDER BY id"),
    ("targets", "SELECT id, state, state_reason, attempt_no, claim_epoch FROM score_targets ORDER BY id"),
    (
        "attempts",
        "SELECT target_id, attempt_no, claim_epoch, cycle_id, outcome, outcome_detail "
        "FROM score_attempts ORDER BY id",
    ),
    (
        "calls",
        "SELECT c.target_id, c.purpose, l.state, c.status, c.error_code FROM llm_calls c "
        "JOIN llm_spend_ledger l ON l.id = c.spend_ledger_id ORDER BY c.id",
    ),
    ("jobs", "SELECT task_name, status FROM procrastinate_jobs WHERE task_name <> 'jh:tick' ORDER BY id"),
)


def one(server: PgServer, statement: str, *params: object) -> tuple[object, ...] | None:
    return pg_one(server, "superuser", statement, *params)


def rows(server: PgServer, statement: str, *params: object) -> list[tuple[object, ...]]:
    return pg_rows(server, "superuser", statement, *params)


async def targets_of_an_inactive_tuple(engine: AsyncEngine, n: int) -> list[PendingTarget]:
    """`n` pending targets of new synthetic-ad jobs over the score tuple, which is not active yet."""
    return [await insert_target(engine) for _ in range(n)]


def tick_done(server: PgServer) -> bool:
    """A tick job has succeeded and none is running."""
    found = one(
        server,
        "SELECT count(*) FILTER (WHERE status = 'succeeded'), count(*) FILTER (WHERE status = 'doing') "
        "FROM procrastinate_jobs WHERE task_name = 'jh:tick'",
    )
    return found is not None and int(str(found[0])) > 0 and int(str(found[1])) == 0


def at_second_of_minute(seconds: range) -> None:
    """Wait until the wall clock is at one of `seconds` of a minute."""
    while datetime.now(UTC).second not in seconds:
        time.sleep(0.2)


async def request_cycle(
    engine: AsyncEngine, server: PgServer, client: httpx.AsyncClient, *, at: range = AWAY_FROM_THE_TICK
) -> None:
    """Once the worker's start's tick has ended, at a second of `at`: the score tuple made active, then a
    manual cycle requested through the api."""
    wait_until(lambda: tick_done(server), 60)
    at_second_of_minute(at)
    async with engine.begin() as conn:
        await activate_score_tuple(conn)
    r = await client.post("/api/system/score-cycle", headers=idem(fresh_key("score-cycle")))
    assert r.status_code == 202 and r.json()["queued"] is True, (r.status_code, r.text)


def spawn_ready(procs: ProcessGroup, args: list[str], env: dict[str, str]) -> Proc:
    worker = procs.spawn(args, env)
    worker.wait_event("worker_ready", READY_WITHIN_S)
    return worker


def stalling_env(
    proc_env: Callable[..., dict[str, str]],
    server: PgServer,
    target: PendingTarget,
    stall_s: int,
    **extra: str,
) -> dict[str, str]:
    """A `tests.crash.score_worker` child whose first generator try for `target` stalls `stall_s` seconds."""
    key = fake.answer_key("generate", {"job_revision_id": target.job_revision_id}, 0)
    return proc_env("worker", server, JH_TEST_STALL_KEY=key, JH_TEST_STALL_S=str(stall_s), **extra)


def stale_jobs(server: PgServer) -> None:
    """A dead worker's procrastinate heartbeat made stale: the next worker's start releases its job."""
    one(server, "UPDATE procrastinate_workers SET last_heartbeat = now() - interval '5 minutes'")


def target_of(server: PgServer, target_id: int) -> tuple[object, ...]:
    """`(state, attempt_no, claim_id, published_result_id)` of the target."""
    found = one(
        server,
        "SELECT state, attempt_no, claim_id, published_result_id FROM score_targets WHERE id = %s",
        target_id,
    )
    assert found is not None
    return found


def attempts_of(server: PgServer, target_id: int) -> list[tuple[object, ...]]:
    """`(attempt_no, claim_epoch, outcome, outcome_detail, its cycle's trigger)` of each attempt, oldest
    first."""
    return rows(
        server,
        "SELECT a.attempt_no, a.claim_epoch, a.outcome, a.outcome_detail, c.trigger FROM score_attempts a "
        "LEFT JOIN score_cycles c ON c.id = a.cycle_id WHERE a.target_id = %s ORDER BY a.attempt_no",
        target_id,
    )


def cycles_of(server: PgServer) -> list[tuple[object, ...]]:
    """`(trigger, worker_epoch, status, status_reason)` of each cycle, oldest first."""
    return rows(server, "SELECT trigger, worker_epoch, status, status_reason FROM score_cycles ORDER BY id")


def no_cycle_running(server: PgServer) -> bool:
    """Whether every cycle has ended (none is `running`)."""
    return all(cycle[2] != "running" for cycle in cycles_of(server))


def calls_of(server: PgServer, target_id: int) -> list[tuple[object, ...]]:
    """`(attempt_no, purpose, ledger state, call status, error_code)` of each of the target's calls."""
    return rows(
        server,
        "SELECT a.attempt_no, c.purpose, l.state, c.status, c.error_code FROM llm_calls c "
        "JOIN llm_spend_ledger l ON l.id = c.spend_ledger_id JOIN score_attempts a ON a.id = c.attempt_id "
        "WHERE c.target_id = %s ORDER BY c.id",
        target_id,
    )


def epoch(server: PgServer) -> int:
    found = one(server, "SELECT worker_epoch FROM worker_heartbeat")
    assert found is not None
    return int(str(found[0]))


def published_by_periodic(server: PgServer, targets: list[PendingTarget]) -> bool:
    """Every target published by an attempt of a periodic cycle."""
    for target in targets:
        attempts = attempts_of(server, target.target_id)
        if target_of(server, target.target_id)[0] not in PUBLISHED or attempts[-1][4] != "periodic":
            return False
    return True


def old_claim(server: PgServer, target_id: int) -> tuple[str, int]:
    """The claim and attempt number of the target's first attempt (the claim a crash left behind)."""
    found = one(
        server,
        "SELECT claim_id::text, attempt_no FROM score_attempts WHERE target_id = %s AND attempt_no = 1",
        target_id,
    )
    assert found is not None and found[0] is not None
    return str(found[0]), int(str(found[1]))


# Step 2 of `scoring.publish`, the target locked under the claim: the production statement itself
# (`publish._LOCK_CLAIMED`), its binds `:t`, `:c`, `:a` made positional in that order (the next test pins
# the order), wrapped to count the rows it locks (Task 28 fix round 1, item 8).
REPLAYED_LOCK: Final = (
    "SELECT count(*) FROM (" + re.sub(r":[a-z]\b", "%s", publish._LOCK_CLAIMED.text) + ") s"  # noqa: S608
)


def test_the_replayed_lock_is_the_publishs_own() -> None:
    assert re.findall(r":[a-z]\b", publish._LOCK_CLAIMED.text) == [":t", ":c", ":a"]
    assert REPLAYED_LOCK.count("%s") == 3 and "FOR UPDATE" in REPLAYED_LOCK


def replay_publish(server: PgServer, target_id: int, claim_id: str, attempt_no: int) -> list[int]:
    """The zombie's publish under its old claim, replayed by hand (Ruling T28-b; `scoring.publish`: step 2's
    lock of the target under the claim, step 6's move of the target, and its attempt's end): the rows each
    affected. The lock is `publish._LOCK_CLAIMED` itself (`REPLAYED_LOCK`). The two moves have no statement
    to reuse: `db.transitions.transition` renders them at run time, so they are written here with its
    predicate (the row, its from-state and the owner columns the publish passes)."""
    claim = uuid.UUID(claim_id)
    statements = (
        REPLAYED_LOCK,
        "WITH s AS (UPDATE score_targets SET state = 'published_accepted' WHERE id = %s "
        "AND claim_id = %s::uuid AND attempt_no = %s AND state = 'in_progress' RETURNING 1) "
        "SELECT count(*) FROM s",
        "WITH s AS (UPDATE score_attempts SET outcome = 'published_accepted', ended_at = now() "
        "WHERE target_id = %s AND claim_id = %s::uuid AND attempt_no = %s AND outcome = 'running' "
        "RETURNING 1) SELECT count(*) FROM s",
    )
    return [int(str(rows(server, statement, target_id, claim, attempt_no)[0][0])) for statement in statements]


def domain_rows(server: PgServer) -> dict[str, list[tuple[object, ...]]]:
    """Every row of `DOMAIN_TABLES`, ordered by id."""
    return {
        table: rows(server, f"SELECT * FROM {table} ORDER BY id")  # noqa: S608 - constant names
        for table in DOMAIN_TABLES
    }


def threads(server: PgServer) -> dict[object, tuple[object, ...]]:
    """The checkpoint rows and the pending writes stored for each thread (M0's C17 harness)."""
    found = rows(
        server,
        "SELECT t.thread_id, (SELECT count(*) FROM checkpoints c WHERE c.thread_id = t.thread_id), "
        "(SELECT count(*) FROM checkpoint_writes w WHERE w.thread_id = t.thread_id) FROM "
        "(SELECT thread_id FROM checkpoints UNION SELECT thread_id FROM checkpoint_writes) t",
    )
    return {row[0]: row[1:] for row in found}


def wait_for(server: PgServer, pred: Callable[[], bool], timeout_s: float, *workers: Proc) -> None:
    """`wait_until`, whose timeout reports the lane's rows (`REPORTED`) and each worker's last output
    lines."""
    try:
        wait_until(pred, timeout_s)
    except TimeoutError as exc:
        state = "\n".join(f"{name}: {rows(server, statement)}" for name, statement in REPORTED)
        tails = "\n".join(f"worker {w.pid}:\n{''.join(w.output().splitlines(True)[-25:])}" for w in workers)
        raise TimeoutError(f"{exc}\n{state}\n{tails}") from None


async def test_c29_a_worker_killed_after_its_claim_orphans_the_cycle_and_the_next_tick_scores(
    migrated, proc_env, procs, engine, logged_in_client
) -> None:
    targets = await targets_of_an_inactive_tuple(engine, 2)
    a = spawn_ready(procs, WORKER, proc_env("worker", migrated, JOBHUNTER_FAULT="score.claim.after_claim"))
    await request_cycle(engine, migrated, logged_in_client)
    a.wait_event("fault_point_reached", 60, point="score.claim.after_claim")
    assert a.exit_within(30) == FAULT_EXIT
    epoch_a = epoch(migrated)
    assert cycles_of(migrated) == [("manual", epoch_a, "running", None)]
    assert [target_of(migrated, t.target_id)[:2] for t in targets] == [("in_progress", 1)] * 2
    stale_jobs(migrated)

    b = spawn_ready(procs, WORKER, proc_env("worker", migrated))
    epoch_b = epoch(migrated)
    assert epoch_b > epoch_a
    # the takeover's sweeps ran before `worker_ready`: the cycle orphaned, its claims reclaimed
    assert cycles_of(migrated)[0] == ("manual", epoch_a, "failed", "orphaned")
    for target in targets:
        assert attempts_of(migrated, target.target_id)[0] == (
            1,
            epoch_a,
            "claim_lost",
            "worker_takeover",
            "manual",
        )
    assert one(migrated, "SELECT severity FROM alerts WHERE code = 'score_cycle.orphaned'") == ("warn",)
    wait_for(migrated, lambda: published_by_periodic(migrated, targets), SCORED_WITHIN_S, b)
    assert cycles_of(migrated) == [
        ("manual", epoch_a, "failed", "orphaned"),
        ("periodic", epoch_b, "completed", None),
    ]
    for target in targets:
        published = target_of(migrated, target.target_id)
        assert attempts_of(migrated, target.target_id)[1] == (2, epoch_b, published[0], None, "periodic")
    # the next completed cycle resolves the orphaned cycle's alert (Carry T25-b)
    resolved = one(migrated, "SELECT resolved_at IS NOT NULL FROM alerts WHERE code = 'score_cycle.orphaned'")
    assert resolved == (True,)
    b.signal(signal.SIGTERM)
    assert b.exit_within(60) == 0


async def test_c9_a_worker_killed_mid_stream_leaves_an_ambiguous_entry_and_the_takeover_rescores(
    migrated, proc_env, procs, engine, logged_in_client
) -> None:
    (target,) = await targets_of_an_inactive_tuple(engine, 1)
    a = spawn_ready(procs, WORKER, proc_env("worker", migrated, JOBHUNTER_FAULT="gateway.after_dispatch"))
    await request_cycle(engine, migrated, logged_in_client)
    a.wait_event("fault_point_reached", 60, point="gateway.after_dispatch")
    assert a.exit_within(30) == FAULT_EXIT
    epoch_a = epoch(migrated)
    assert calls_of(migrated, target.target_id) == [(1, "generate", "dispatched", "dispatched", None)]
    claim_id, attempt_no = old_claim(migrated, target.target_id)
    # the injected deadline: dispatched longer ago than max(15 min, deadline_s 600 s + 5 min)
    one(migrated, "UPDATE llm_spend_ledger SET dispatched_at = dispatched_at - interval '16 minutes'")
    stale_jobs(migrated)

    b = spawn_ready(procs, WORKER, proc_env("worker", migrated))
    epoch_b = epoch(migrated)
    assert attempts_of(migrated, target.target_id) == [
        (1, epoch_a, "claim_lost", "worker_takeover", "manual")
    ]
    assert target_of(migrated, target.target_id)[:2] == ("pending", 1)
    wait_for(migrated, lambda: published_by_periodic(migrated, [target]), SCORED_WITHIN_S, b)
    published = target_of(migrated, target.target_id)
    assert attempts_of(migrated, target.target_id) == [
        (1, epoch_a, "claim_lost", "worker_takeover", "manual"),
        (2, epoch_b, published[0], None, "periodic"),
    ]
    calls = calls_of(migrated, target.target_id)
    assert calls[0] == (1, "generate", "ambiguous", "ambiguous", "unsettled")  # `llm_ledger_unsettled`
    # the new attempt built its context from the rows: attempt 1 left no draft, so it generated again
    assert calls[1][:2] == (2, "generate") and {c[0] for c in calls[1:]} == {2}
    no_draft = one(
        migrated,
        "SELECT count(*) FROM score_drafts d JOIN score_attempts a ON a.id = d.attempt_id "
        "WHERE a.target_id = %s AND a.attempt_no = 1",
        target.target_id,
    )
    assert no_draft == (0,)
    assert replay_publish(migrated, target.target_id, claim_id, attempt_no) == [0, 0, 0]
    assert target_of(migrated, target.target_id) == published
    b.signal(signal.SIGTERM)
    assert b.exit_within(60) == 0


async def test_sigterm_during_a_cycle_drains_and_leaves_the_in_flight_entry_ambiguous(
    migrated, proc_env, procs, engine, logged_in_client
) -> None:
    (target,) = await targets_of_an_inactive_tuple(engine, 1)
    w = spawn_ready(procs, STALLING_WORKER, stalling_env(proc_env, migrated, target, SIGTERM_STALL_S))
    await request_cycle(engine, migrated, logged_in_client)
    w.wait_event("fake_stall_started", 60)
    assert calls_of(migrated, target.target_id) == [(1, "generate", "dispatched", "dispatched", None)]
    w.signal(signal.SIGTERM)
    # the stop waits out its grace for the stalled call, then cancels it (`worker.shutdown`)
    assert w.exit_within(STOP_BUDGET_S + 20) == 0
    assert w.wait_event("worker_shutdown_complete", 1)["exit_code"] == 0
    assert calls_of(migrated, target.target_id) == [(1, "generate", "ambiguous", "ambiguous", "cancelled")]
    # no state is rewritten on shutdown: the next worker's takeover sweep resumes the work (§3.3.5)
    assert cycles_of(migrated) == [("manual", epoch(migrated), "running", None)]
    assert [a[2] for a in attempts_of(migrated, target.target_id)] == ["running"]


async def test_c14_a_stalled_heartbeat_is_reclaimed_at_three_minutes_and_the_new_attempt_publishes(
    migrated, proc_env, procs, engine, logged_in_client
) -> None:
    (target,) = await targets_of_an_inactive_tuple(engine, 1)
    env = stalling_env(
        proc_env, migrated, target, C14_STALL_S, JOBHUNTER_FAULT=f"claims.beat:sleep={C14_BEAT_STALL_S}"
    )
    w = spawn_ready(procs, STALLING_WORKER, env)
    await request_cycle(engine, migrated, logged_in_client, at=C14_CLAIM_SECONDS)
    w.wait_event("fake_stall_started", 60)
    claimed = time.monotonic()
    claim_id, attempt_no = old_claim(migrated, target.target_id)
    w.wait_event("fault_point_reached", 60, point="claims.beat")  # the first beat, 30 s on, stalls 200 s
    wait_for(migrated, lambda: attempts_of(migrated, target.target_id)[0][2] != "running", 300, w)
    reclaimed_after_s = time.monotonic() - claimed
    epoch_w = epoch(migrated)
    assert attempts_of(migrated, target.target_id)[0] == (
        1,
        epoch_w,
        "claim_lost",
        "stale_heartbeat",
        "manual",
    )
    assert 180 <= reclaimed_after_s < 30 + C14_BEAT_STALL_S  # by `stuck_targets`, before the beat is back
    # the beat's return finds the claim gone: the stalled call is cancelled, never published
    wait_for(migrated, lambda: calls_of(migrated, target.target_id)[0][2] != "dispatched", 120, w)
    assert calls_of(migrated, target.target_id)[0] == (1, "generate", "ambiguous", "ambiguous", "cancelled")
    w.wait_event("score_claim_heartbeat_lost", 1, target_id=target.target_id, attempt_no=1)
    wait_for(migrated, lambda: published_by_periodic(migrated, [target]), SCORED_WITHIN_S, w)
    # the periodic cycle ends after it has published its target: wait for its end, then read it
    wait_for(migrated, lambda: no_cycle_running(migrated), SCORED_WITHIN_S, w)
    published = target_of(migrated, target.target_id)
    assert attempts_of(migrated, target.target_id) == [
        (1, epoch_w, "claim_lost", "stale_heartbeat", "manual"),
        (2, epoch_w, published[0], None, "periodic"),
    ]
    assert cycles_of(migrated) == [
        ("manual", epoch_w, "completed", None),
        ("periodic", epoch_w, "completed", None),
    ]
    assert replay_publish(migrated, target.target_id, claim_id, attempt_no) == [0, 0, 0]
    assert target_of(migrated, target.target_id) == published
    w.signal(signal.SIGTERM)
    assert w.exit_within(60) == 0


async def test_c17_a_zombie_worker_with_a_cycle_in_flight_writes_no_domain_row(
    migrated, proc_env, procs, engine, logged_in_client
) -> None:
    (target,) = await targets_of_an_inactive_tuple(engine, 1)
    paused = proc_env("worker", migrated, JOBHUNTER_FAULT="score.claim.after_claim:pause")
    a = spawn_ready(procs, WORKER, paused)
    await request_cycle(engine, migrated, logged_in_client, at=C17_CLAIM_SECONDS)
    a.wait_event("fault_point_reached", 60, point="score.claim.after_claim")
    wait_until(a.is_stopped, 10)
    epoch_a = epoch(migrated)
    one(migrated, "UPDATE worker_heartbeat SET heartbeat_at = now() - interval '2 minutes'")
    stale_jobs(migrated)
    b = spawn_ready(procs, WORKER, proc_env("worker", migrated))
    epoch_b = epoch(migrated)
    wait_for(migrated, lambda: published_by_periodic(migrated, [target]), SCORED_WITHIN_S, a, b)
    published = target_of(migrated, target.target_id)
    assert attempts_of(migrated, target.target_id) == [
        (1, epoch_a, "claim_lost", "worker_takeover", "manual"),
        (2, epoch_b, published[0], None, "periodic"),
    ]
    assert cycles_of(migrated) == [
        ("manual", epoch_a, "failed", "orphaned"),
        ("periodic", epoch_b, "completed", None),
    ]
    zombie_cycle, new_cycle = (str(r[0]) for r in rows(migrated, "SELECT id FROM score_cycles ORDER BY id"))
    replaced = domain_rows(migrated)
    before = threads(migrated)
    assert set(before) == {f"cycle-{zombie_cycle}", f"cycle-{new_cycle}"}
    a.signal(signal.SIGCONT)
    assert a.exit_within(60) in (SUPERSEDED_EXIT, 0)
    # §18.3 C17: the zombie changed no domain row, and its checkpoint writes stayed on its own thread
    assert domain_rows(migrated) == replaced
    after = threads(migrated)
    assert set(after) == set(before) and after[f"cycle-{new_cycle}"] == before[f"cycle-{new_cycle}"]
    assert one(
        migrated,
        "SELECT h.pid, h.lock_backend_pid = l.pid FROM worker_heartbeat h JOIN pg_locks l "
        "ON l.locktype = 'advisory' AND l.objid = 7261001 AND l.granted",
    ) == (b.pid, True)  # the replacement holds the lock
    b.signal(signal.SIGTERM)
    assert b.exit_within(60) == 0
