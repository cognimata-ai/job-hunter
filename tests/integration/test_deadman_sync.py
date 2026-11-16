"""The dead-man check's period sync (spec §17.7; plan M5 Task 5, O-ops Q7, Q8, O2, Deviation 9).

The tick (the `deadman_ping` reconciler) compares `schedule_state(interval_s, enabled)` with what
`deadman_state` records as applied, and defers one `deadman_sync` (`aux`, queueing lock `deadman_sync`) when
they differ or the last sync is more than 24 h old, once `next_try_at` has passed; the task also runs daily
(`23 4 * * *`). The task reads the check back and applies `timeout = interval_s` and `grace =
grace_s(interval_s)` where they differ, pauses the check of a disabled schedule and resumes a paused one with
a `success` ping, then stamps the row and resolves `deadman.update_failed`. A failure raises that alert (warn,
subject `healthchecks`), records its code and backs off 5 min, doubling to 60; pings go on. A run that ends
while the schedule is disabled never pings, so it cannot resume a check the sync paused. Without both secrets
nothing is sent.

respx serves healthchecks (`tests.support.healthchecks`; no network); the jobs run on a real procrastinate
worker of `aux`; the sync's clock is injected. Every setting and run is invented.
"""

import asyncio
import copy
import dataclasses
import time
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime, timedelta
from types import MappingProxyType, SimpleNamespace
from typing import Any, Final, cast

import httpx
import procrastinate
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.config import AppConfig
from jobhunter.graphs.collect.nodes.finalize import finalize_from_graph
from jobhunter.graphs.collect.state import initial_state
from jobhunter.observability import deadman
from jobhunter.observability.deadman import DeadmanPinger, HealthchecksClient, grace_s
from jobhunter.settings.active import lock_active
from jobhunter.settings.save import SaveRequest, save_settings
from jobhunter.worker import tasks
from jobhunter.worker.runs import RunOwner
from jobhunter.worker.tasks import defer_in_tx, job_task_name
from procrastinate.job_context import JobContext
from procrastinate.periodic import PeriodicDeferrer
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.collect import make_deps
from tests.support.deadman import (
    admitted,
    deadman_state,
    run_aux,
    runtime_of,
    scheduled_run,
    sync_jobs,
    tick,
)
from tests.support.fixtures import ConfigFactory
from tests.support.healthchecks import API_KEY, API_URL, PING_URL, FakeCheck, fake_healthchecks
from tests.support.pg import PgServer, blocked_or_done
from tests.support.procs import await_until

pytestmark = pytest.mark.integration

T0: Final = datetime(2026, 11, 9, 12, tzinfo=UTC)
SEEDED_INTERVAL_S: Final = 6 * 3600  # settings version 1: `schedule.interval_hours` 6
_ALERTS: Final = sa.text(
    "SELECT severity, occurrences, context, resolved_at FROM alerts WHERE dedup_key = :k ORDER BY id"
)
UPDATE_FAILED_KEY: Final = "deadman.update_failed:healthchecks"


class Clock:
    """The sync's clock: it moves only when the test moves it."""

    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


async def no_wait(seconds: float) -> None:
    return None


def client_of(http: httpx.AsyncClient) -> HealthchecksClient:
    """The healthchecks client of the stand-in on `http`, whose pings never wait to retry."""
    return HealthchecksClient(http, ping_url=PING_URL, api_url=API_URL, api_key=API_KEY, sleep=no_wait)


@pytest.fixture
def hc() -> Iterator[FakeCheck]:
    with fake_healthchecks() as (check, _):
        yield check


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
async def pinger(
    engine: AsyncEngine, current_epoch: int, hc: FakeCheck, clock: Clock
) -> AsyncIterator[DeadmanPinger]:
    async with deadman.make_http_client(user_agent="JobHunter/test") as http:
        yield DeadmanPinger(engine, client_of(http), epoch=current_epoch, clock=clock)


async def save_schedule(engine: AsyncEngine, **schedule: Any) -> None:
    """Save and activate the active settings with `schedule` changed (an internal save: its effect class
    `schedule` resets `schedule_state` at once, §13.3)."""
    async with engine.begin() as conn:
        active = await lock_active(conn)
        payload = copy.deepcopy(cast(dict[str, Any], active.payload))
        payload["schedule"].update(schedule)
        request = SaveRequest(
            payload=payload, base_version_id=active.settings_version_id, acknowledged_warnings=frozenset()
        )
        await save_settings(conn, request, now=utcnow(), defer=defer_in_tx, internal=True)


async def ticked(
    engine: AsyncEngine, app: procrastinate.App, pinger: DeadmanPinger | None, now: datetime
) -> int:
    """One tick at `now`; how many `deadman_sync` jobs wait after it."""
    await tick(engine, app, pinger, now=now)
    return sum(job.status == "todo" for job in await sync_jobs(engine))


async def synced(
    engine: AsyncEngine,
    app: procrastinate.App,
    pinger: DeadmanPinger,
    monkeypatch: pytest.MonkeyPatch,
    now: datetime,
) -> None:
    """A tick at `now` that defers one sync, and that sync run by the `aux` worker."""
    assert await ticked(engine, app, pinger, now) == 1
    [job] = [job for job in await sync_jobs(engine) if job.status == "todo"]
    assert (job.queue_name, job.queueing_lock) == ("aux", "deadman_sync")
    await run_aux(app, runtime_of(engine, pinger), monkeypatch)
    assert [job.status for job in await sync_jobs(engine)][-1] == "succeeded"


async def update_failed(engine: AsyncEngine) -> list[Any]:
    async with engine.connect() as conn:
        return list((await conn.execute(_ALERTS, {"k": UPDATE_FAILED_KEY})).all())


async def test_a_schedule_save_syncs_the_check_within_a_tick(
    engine: AsyncEngine,
    queue_app: procrastinate.App,
    pinger: DeadmanPinger,
    hc: FakeCheck,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # never synced: the first tick defers a sync, which applies the seeded 6 h
    assert (await deadman_state(engine)).synced_at is None
    await synced(engine, queue_app, pinger, monkeypatch, now=clock.now)
    assert (hc.timeout, hc.grace, hc.status) == (SEEDED_INTERVAL_S, 5400, "new")
    state = await deadman_state(engine)
    assert (state.applied_interval_s, state.applied_timeout_s, state.applied_grace_s) == (21600, 21600, 5400)
    assert (state.applied_paused, state.synced_at, state.last_error_code, state.next_try_at) == (
        False,
        T0,
        None,
        None,
    )
    assert await ticked(engine, queue_app, pinger, clock.now) == 0  # in sync: nothing to do

    await save_schedule(engine, interval_hours=24)
    clock.now += timedelta(minutes=1)
    await synced(engine, queue_app, pinger, monkeypatch, now=clock.now)  # the next tick
    assert (hc.timeout, hc.grace) == (86_400, grace_s(86_400)) == (86_400, 21_600)
    assert hc.updates == [{"timeout": 21_600, "grace": 5_400}, {"timeout": 86_400, "grace": 21_600}]
    state = await deadman_state(engine)
    assert (state.applied_interval_s, state.applied_timeout_s, state.applied_grace_s) == (86_400,) * 2 + (
        21_600,
    )
    assert state.synced_at == clock.now
    assert await ticked(engine, queue_app, pinger, clock.now) == 0
    assert hc.pings == [] and all(hc.api_keys_right)


async def test_disable_pauses_and_enable_resumes_with_a_ping(
    engine: AsyncEngine,
    queue_app: procrastinate.App,
    pinger: DeadmanPinger,
    hc: FakeCheck,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await synced(engine, queue_app, pinger, monkeypatch, now=clock.now)
    hc.status = "up"

    await save_schedule(engine, enabled=False)
    await synced(engine, queue_app, pinger, monkeypatch, now=clock.now)
    assert (hc.status, hc.pauses, hc.pings) == ("paused", 1, [])
    assert (await deadman_state(engine)).applied_paused is True
    assert await ticked(engine, queue_app, pinger, clock.now) == 0  # paused as the schedule says

    await save_schedule(engine, enabled=True)
    await synced(engine, queue_app, pinger, monkeypatch, now=clock.now)
    assert (hc.status, hc.pauses, hc.pings) == ("up", 1, ["success"])  # resumed by one ping
    assert len(hc.updates) == 1  # the period was right all along
    assert (await deadman_state(engine)).applied_paused is False
    assert await ticked(engine, queue_app, pinger, clock.now) == 0


async def test_a_run_finishing_after_the_schedule_was_disabled_does_not_resume_the_check(
    engine: AsyncEngine,
    queue_app: procrastinate.App,
    pinger: DeadmanPinger,
    hc: FakeCheck,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deviation 9: the ping reads `schedule_state.enabled` and `deadman_state.applied_paused` under the lock,
    so a run that ends after the schedule was disabled sends nothing, whether or not the pause is applied yet;
    the watermark still moves past it."""
    await synced(engine, queue_app, pinger, monkeypatch, now=clock.now)
    deps = make_deps(engine, lambda instance: pytest.fail("no adapter"))
    deps.deadman = pinger

    async def end(owner: RunOwner) -> None:
        state = initial_state(owner.run_id, owner.attempt_no)
        assert await finalize_from_graph(deps, owner, state) == "succeeded"

    in_flight = await admitted(engine, await scheduled_run(engine, slot=T0))
    await save_schedule(engine, enabled=False)
    await synced(engine, queue_app, pinger, monkeypatch, now=clock.now)  # the pause applied
    assert (hc.status, hc.pauses) == ("paused", 1)
    await end(in_flight)
    assert hc.pings == [] and hc.status == "paused"
    assert (await deadman_state(engine)).last_pinged_run_id == in_flight.run_id
    await tick(engine, queue_app, pinger, now=clock.now)  # the durable path sends nothing either
    assert hc.pings == [] and hc.status == "paused"

    # the other order: the run ends after the schedule was disabled but before the sync paused the check
    await save_schedule(engine, enabled=True)
    await synced(engine, queue_app, pinger, monkeypatch, now=clock.now)
    assert (hc.status, hc.pings) == ("up", ["success"])  # the re-enable's resume
    later = await admitted(engine, await scheduled_run(engine, slot=T0 + timedelta(hours=6)))
    await save_schedule(engine, enabled=False)
    await end(later)
    assert hc.pings == ["success"] and hc.status == "up"
    await synced(engine, queue_app, pinger, monkeypatch, now=clock.now)
    assert (hc.status, hc.pauses, hc.pings) == ("paused", 2, ["success"])
    assert (await deadman_state(engine)).last_pinged_run_id == later.run_id


class FirstPingHeld(HealthchecksClient):
    """The stand-in's client whose first ping waits for `release` once it has begun (`sending`)."""

    def __init__(self, http: httpx.AsyncClient) -> None:
        super().__init__(http, ping_url=PING_URL, api_url=API_URL, api_key=API_KEY, sleep=no_wait)
        self.sending, self.release = asyncio.Event(), asyncio.Event()
        self.begun = 0

    async def ping(self, kind: deadman.PingKind) -> bool:
        self.begun += 1
        if self.begun == 1:
            self.sending.set()
            await self.release.wait()
        return await super().ping(kind)


async def test_a_disable_during_a_two_run_pass_withholds_the_second_ping(
    engine: AsyncEngine, migrated: PgServer, current_epoch: int, hc: FakeCheck, clock: Clock
) -> None:
    """Final review M1: two ended runs wait for their pings, and the tick's pass sends the first. While it is
    on its way the user disables the schedule: the save takes the dead-man lock (7261005) the pass holds
    around each send, so it waits for that ping and commits before the next; the next send reads the schedule
    again under the lock and is withheld. No ping is sent after the disable commits, and the watermark moves
    past both runs."""
    deps = make_deps(engine, lambda instance: pytest.fail("no adapter"))  # no pinger: no fast path
    ended = []
    for slot in (T0, T0 + timedelta(hours=6)):
        owner = await admitted(engine, await scheduled_run(engine, slot=slot))
        assert (
            await finalize_from_graph(deps, owner, initial_state(owner.run_id, owner.attempt_no))
            == "succeeded"
        )
        ended.append(owner.run_id)
    async with deadman.make_http_client(user_agent="JobHunter/test") as http:
        client = FirstPingHeld(http)
        pinger = DeadmanPinger(engine, client, epoch=current_epoch, clock=clock)
        passing = asyncio.create_task(pinger.ping_pending())
        await asyncio.wait_for(client.sending.wait(), 10)
        disabling = asyncio.create_task(save_schedule(engine, enabled=False))
        await await_until(lambda: blocked_or_done(migrated, disabling), 10)  # the save waits on the lock
        client.release.set()
        await asyncio.wait_for(asyncio.gather(passing, disabling), 30)

    assert passing.result() == 2
    assert hc.pings == ["success"]  # the first only: the second came after the disable committed
    assert (await deadman_state(engine)).last_pinged_run_id == ended[1]
    async with engine.connect() as conn:
        assert await deadman.read_schedule(conn) == (SEEDED_INTERVAL_S, False)


async def test_an_api_failure_raises_update_failed_and_backs_off_5_to_60(
    engine: AsyncEngine,
    queue_app: procrastinate.App,
    pinger: DeadmanPinger,
    hc: FakeCheck,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
    log_events: Callable[[], list[dict[str, Any]]],
) -> None:
    hc.api_status = 500
    for occurrence, minutes in enumerate((5, 10, 20, 40, 60, 60), start=1):
        await synced(engine, queue_app, pinger, monkeypatch, now=clock.now)  # the job ends; the sync failed
        state = await deadman_state(engine)
        assert (state.last_error_code, state.next_try_at) == (
            "http_500",
            clock.now + timedelta(minutes=minutes),
        )
        assert state.synced_at is None and state.applied_interval_s is None
        [alert] = await update_failed(engine)
        assert (alert.severity, alert.occurrences, alert.context, alert.resolved_at) == (
            "warn",
            occurrence,
            {"error_code": "http_500"},
            None,
        )
        # no sync is deferred before `next_try_at`, one is once it has passed
        assert await ticked(engine, queue_app, pinger, clock.now + timedelta(minutes=minutes - 1)) == 0
        clock.now = state.next_try_at

    # pings go on meanwhile: the check stays fed by the run ends
    owner = await admitted(engine, await scheduled_run(engine, slot=T0))
    deps = make_deps(engine, lambda instance: pytest.fail("no adapter"))
    deps.deadman = pinger
    assert await finalize_from_graph(deps, owner, initial_state(owner.run_id, 1)) == "succeeded"
    assert hc.pings == ["success"]

    hc.api_status = 200
    await synced(engine, queue_app, pinger, monkeypatch, now=clock.now)
    state = await deadman_state(engine)
    assert (state.last_error_code, state.next_try_at, state.synced_at) == (None, None, clock.now)
    assert (hc.timeout, hc.grace) == (21_600, 5_400)
    [alert] = await update_failed(engine)
    assert alert.resolved_at is not None
    failures = [e for e in log_events() if e["event"] == "deadman_sync_failed"]
    assert [(e["error_code"], e["retry_in_s"]) for e in failures] == [
        ("http_500", m * 60) for m in (5, 10, 20, 40, 60, 60)
    ]
    assert API_KEY not in str(log_events()) and "hc-ping" not in str(log_events())


async def test_the_daily_sync_reapplies_a_drifted_check(
    engine: AsyncEngine,
    queue_app: procrastinate.App,
    pinger: DeadmanPinger,
    hc: FakeCheck,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await synced(engine, queue_app, pinger, monkeypatch, now=clock.now)
    hc.timeout, hc.grace = 999, 60  # changed on healthchecks itself
    clock.now += timedelta(hours=1)
    assert (
        await ticked(engine, queue_app, pinger, clock.now) == 0
    )  # the row says in sync: the tick sees nothing

    # the daily run, deferred by its cron
    periodic = {
        name: (task.cron, task.task.queue, task.task.queueing_lock)
        for (name, _), task in queue_app.periodic_registry.periodic_tasks.items()
    }
    assert periodic[job_task_name("deadman_sync")] == ("23 4 * * *", "aux", "deadman_sync")
    deferrer = PeriodicDeferrer(queue_app.periodic_registry, max_delay=2 * 86_400)
    due = [
        (task, at)
        for task, at in deferrer.get_previous_tasks(at=time.time())
        if task.task.name == job_task_name("deadman_sync")
    ]
    assert len(due) == 1
    await deferrer.defer_jobs(due)
    await run_aux(queue_app, runtime_of(engine, pinger), monkeypatch)
    assert (hc.timeout, hc.grace) == (21_600, 5_400)
    assert (await deadman_state(engine)).synced_at == clock.now

    # and the tick's own check: a last sync more than 24 h old is due again
    hc.timeout = 999
    clock.now += timedelta(hours=24, minutes=1)
    await synced(engine, queue_app, pinger, monkeypatch, now=clock.now)
    assert hc.timeout == 21_600 and (await deadman_state(engine)).synced_at == clock.now


async def test_without_secrets_nothing_is_sent(
    engine: AsyncEngine,
    queue_app: procrastinate.App,
    current_epoch: int,
    make_config: ConfigFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Q8: dev and test have neither secret: no pinger is built, so the tick defers no sync, a run's end pings
    nothing and a `deadman_sync` job does nothing."""
    cfg = make_config("worker")
    assert deadman.healthchecks_secrets(cfg) is None
    for only in ("healthchecks_ping_url", "healthchecks_api_key"):  # one of the two is not enough
        one = {**cfg.secrets, only: (PING_URL if only == "healthchecks_ping_url" else API_KEY).encode()}
        assert deadman.healthchecks_secrets(_with_secrets(cfg, one)) is None
    both = {
        **cfg.secrets,
        "healthchecks_ping_url": PING_URL.encode(),
        "healthchecks_api_key": b"hc-test-key\n",
    }
    assert deadman.healthchecks_secrets(_with_secrets(cfg, both)) == (PING_URL, API_KEY)

    with fake_healthchecks() as (_, router):
        assert await tick(engine, queue_app, None, now=utcnow()) == 0
        assert await sync_jobs(engine) == []
        owner = await admitted(engine, await scheduled_run(engine, slot=T0))
        deps = make_deps(engine, lambda instance: pytest.fail("no adapter"))  # no dead-man
        assert await finalize_from_graph(deps, owner, initial_state(owner.run_id, 1)) == "succeeded"
        context = cast(JobContext, SimpleNamespace(additional_context={"runtime": runtime_of(engine, None)}))
        await tasks.deadman_sync(context, timestamp=0)
        assert router.calls.call_count == 0
    state = await deadman_state(engine)
    assert tuple(state) == (None,) * 8  # as the seed wrote it


def _with_secrets(cfg: AppConfig, secrets: dict[str, bytes]) -> AppConfig:
    return dataclasses.replace(cfg, secrets=MappingProxyType(secrets))


# --- Fix round 1 (review M5): an answer that is no check takes the failure path -----------------------------


async def test_an_undecodable_or_malformed_answer_raises_update_failed_and_backs_off(
    engine: AsyncEngine, current_epoch: int, clock: Clock
) -> None:
    """M5: an httpx request error that is neither a timeout nor a transport error (here a body that cannot be
    decoded), and a body that holds no check, are `invalid_response`: the alert and the backoff, never a job
    that fails on its own and is deferred again at the next tick."""
    with fake_healthchecks() as (check, router):
        async with deadman.make_http_client(user_agent="JobHunter/test") as http:
            pinger = DeadmanPinger(engine, client_of(http), epoch=current_epoch, clock=clock)
            answers: list[httpx.Response | Exception] = [
                httpx.DecodingError("undecodable"),
                httpx.Response(200, json={"status": "up", "grace": [3600]}),
            ]
            for occurrence, answer in enumerate(answers, start=1):
                router.routes["read"].mock(side_effect=[answer])
                assert await pinger.sync() is None
                state = await deadman_state(engine)
                minutes = 5 * 2 ** (occurrence - 1)
                assert (state.last_error_code, state.next_try_at) == (
                    "invalid_response",
                    clock.now + timedelta(minutes=minutes),
                )
                [alert] = await update_failed(engine)
                assert (alert.severity, alert.occurrences, alert.context, alert.resolved_at) == (
                    "warn",
                    occurrence,
                    {"error_code": "invalid_response"},
                    None,
                )
                clock.now = state.next_try_at
    assert check.updates == [] and check.pauses == 0
