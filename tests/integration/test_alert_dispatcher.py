"""The worker's alert dispatcher (spec §17.6 L5631-5632, L5700-5704; plan M5 Task 4, Q1, Q2, Q4, Q5,
Deviation 5).

Each pass pushes the open, unacknowledged alerts of severity `notifications.push_min_severity` or above (never
`info`, never `notify.delivery_failed`) that were never pushed, or not within 6 h (warn) or 1 h (critical):
criticals first, then oldest first, ten at most. Each push is stamped in a transaction fenced by the worker's
epoch. The first alert of a pass whose four tries fail counts one `notify_failures`, raises the in-app
`notify.delivery_failed` (subject `ntfy`), ends the pass and pauses the dispatcher for 5 min, doubling up to
60 min; the next delivered push resolves it and resets the pause.

respx serves the ntfy topic (no network); the clock and the backoff's sleep are injected (no real waiting).
Every alert, code and message is invented.
"""

import asyncio
import copy
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, cast

import httpx
import pytest
import respx
import sqlalchemy as sa
from jobhunter.api.app import create_app
from jobhunter.auth.throttle import GLOBAL_MAX_FAILURES
from jobhunter.clock import utcnow
from jobhunter.errors import FencedOut
from jobhunter.observability import ntfy
from jobhunter.observability.alerts import acknowledge_alert, raise_alert, resolve_alert
from jobhunter.observability.catalogue import Severity
from jobhunter.observability.ntfy import AlertDispatcher, DispatchReport, NtfyClient
from jobhunter.settings.active import lock_active
from jobhunter.settings.save import SaveRequest, save_settings
from jobhunter.worker.tasks import defer_in_tx
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.fixtures import TEST_USERNAME, ConfigFactory, UserFactory
from tests.support.pg import PgServer, pg_one

pytestmark = pytest.mark.integration

TOPIC: Final = "https://ntfy.test/jh-alerts-4f2a9c"
ORIGIN: Final = "https://jobs.example.test"
TOKEN: Final = "tk_test_0123456789abcdef"
T0: Final = datetime(2026, 11, 9, 12, tzinfo=UTC)
NOTHING: Final = DispatchReport(sent=0, failed_alert_id=None, paused_until=None)

_ROW: Final = sa.text(
    "SELECT severity, occurrences, last_notified_at, notify_failures, acknowledged_at, resolved_at "
    "FROM alerts WHERE id = :id"
)
_DELIVERY_FAILED: Final = sa.text(
    "SELECT dedup_key, severity, occurrences, context, resolved_at FROM alerts "
    "WHERE code = 'notify.delivery_failed' ORDER BY id"
)


class Clock:
    """The dispatcher's clock: it moves only when the test moves it."""

    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


class Sleeps:
    """The backoff's sleep: it records what it was asked for and never waits."""

    def __init__(self) -> None:
        self.requested: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.requested.append(seconds)


@dataclass
class Rig:
    """A dispatcher of epoch 1 on the respx topic, with its injected clock and sleep."""

    dispatcher: AlertDispatcher
    clock: Clock
    sleeps: Sleeps
    http: httpx.AsyncClient

    def other(self, engine: AsyncEngine, *, epoch: int = 1, token: str | None = None) -> AlertDispatcher:
        """Another dispatcher on the same client, clock and sleep."""
        client = NtfyClient(self.http, topic_url=TOPIC, token=token, public_origin=ORIGIN)
        return AlertDispatcher(engine, client, epoch=epoch, clock=self.clock, sleep=self.sleeps)


@pytest.fixture
def topic() -> Iterator[respx.Route]:
    """The ntfy topic, served by respx; it answers 200 until a test says otherwise. A request to any other
    URL fails."""
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        yield router.post(TOPIC).mock(return_value=httpx.Response(200))


@pytest.fixture
async def rig(engine: AsyncEngine, current_epoch: int, topic: respx.Route) -> AsyncIterator[Rig]:
    async with ntfy.make_http_client(user_agent="JobHunter/test") as http:
        clock, sleeps = Clock(), Sleeps()
        client = NtfyClient(http, topic_url=TOPIC, token=None, public_origin=ORIGIN)
        dispatcher = AlertDispatcher(engine, client, epoch=current_epoch, clock=clock, sleep=sleeps)
        yield Rig(dispatcher, clock, sleeps, http)


async def raised(
    engine: AsyncEngine, code: str, severity: Severity, *, subject: str = "s", message: str | None = None
) -> int:
    """Raise the alert `code`/`subject` in a transaction of its own; its message names both by default."""
    async with engine.begin() as conn:
        return await raise_alert(
            conn, code=code, severity=severity, subject=subject, message=message or f"{code} {subject}"
        )


async def acknowledged(engine: AsyncEngine, alert_id: int) -> None:
    async with engine.begin() as conn:
        assert await acknowledge_alert(conn, alert_id, now=T0) is not None


async def row(engine: AsyncEngine, alert_id: int) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(_ROW, {"id": alert_id})).one()


async def delivery_failed(engine: AsyncEngine) -> list[Any]:
    async with engine.connect() as conn:
        return list((await conn.execute(_DELIVERY_FAILED)).all())


def pushed(topic: respx.Route) -> list[str]:
    """The message of every push so far, in order (the test messages name the code and the subject)."""
    return [call.request.content.decode("utf-8") for call in topic.calls]


async def set_push_min_severity(engine: AsyncEngine, value: str) -> None:
    """Save and activate the active settings with `notifications.push_min_severity` = `value` (an internal
    save: effect class `none`, so it activates at once)."""
    async with engine.begin() as conn:
        active = await lock_active(conn)
        payload = copy.deepcopy(cast(dict[str, Any], active.payload))
        payload["notifications"]["push_min_severity"] = value
        request = SaveRequest(
            payload=payload, base_version_id=active.settings_version_id, acknowledged_warnings=frozenset()
        )
        await save_settings(conn, request, now=utcnow(), defer=defer_in_tx, internal=True)


def events_named(log_events: Callable[[], list[dict[str, Any]]], name: str) -> list[dict[str, Any]]:
    return [event for event in log_events() if event["event"] == name]


async def test_info_acknowledged_and_resolved_alerts_are_never_pushed(
    engine: AsyncEngine, rig: Rig, topic: respx.Route
) -> None:
    info = await raised(engine, "dedup.identity_conflict", "info")
    acked = await raised(engine, "run.partial", "warn")
    await acknowledged(engine, acked)
    resolved = await raised(engine, "run.failed", "critical", subject="run-1")
    async with engine.begin() as conn:
        assert await resolve_alert(conn, code="run.failed", subject="run-1")
    in_app_only = await raised(engine, "notify.delivery_failed", "warn", subject="ntfy")  # NEVER_PUSHED
    breach = await raised(engine, "quota.limit_breach", "critical")  # the one alert that is due

    assert await rig.dispatcher.pass_once() == DispatchReport(sent=1, failed_alert_id=None, paused_until=None)
    assert pushed(topic) == ["quota.limit_breach s"]
    assert (await row(engine, breach)).last_notified_at == T0
    # the resolution of a pushed alert is never pushed, nor is anything else later, past every interval
    async with engine.begin() as conn:
        assert await resolve_alert(conn, code="quota.limit_breach", subject="s")
    rig.clock.now = T0 + timedelta(hours=7)
    assert await rig.dispatcher.pass_once() == NOTHING
    assert topic.call_count == 1
    for alert_id in (info, acked, resolved, in_app_only):
        assert (await row(engine, alert_id)).last_notified_at is None


async def test_push_min_severity_critical_skips_warns(
    engine: AsyncEngine, rig: Rig, topic: respx.Route
) -> None:
    await set_push_min_severity(engine, "critical")
    warn = await raised(engine, "backup.stale", "warn")
    critical = await raised(engine, "backup.failed", "critical")

    assert await rig.dispatcher.pass_once() == DispatchReport(sent=1, failed_alert_id=None, paused_until=None)
    assert pushed(topic) == ["backup.failed s"]
    assert topic.calls.last.request.headers["Priority"] == "5"
    assert ((await row(engine, warn)).last_notified_at, (await row(engine, critical)).last_notified_at) == (
        None,
        T0,
    )
    rig.clock.now = T0 + timedelta(hours=7)  # the warn is never due under this threshold
    assert (await rig.dispatcher.pass_once()).sent == 1
    assert pushed(topic) == ["backup.failed s", "backup.failed s"]


async def test_a_threshold_lowered_later_sends_the_open_warns_once(
    engine: AsyncEngine, rig: Rig, topic: respx.Route
) -> None:
    await set_push_min_severity(engine, "critical")
    await raised(engine, "run.partial", "warn", subject="first")
    await raised(engine, "source.breaker_open", "warn", subject="second")
    await raised(engine, "run.failed", "critical", subject="third")
    assert (await rig.dispatcher.pass_once()).sent == 1
    assert pushed(topic) == ["run.failed third"]

    await set_push_min_severity(engine, "warn")  # read at the next pass: the open warns are sent once
    assert await rig.dispatcher.pass_once() == DispatchReport(sent=2, failed_alert_id=None, paused_until=None)
    assert pushed(topic) == ["run.failed third", "run.partial first", "source.breaker_open second"]
    assert await rig.dispatcher.pass_once() == NOTHING
    assert topic.call_count == 3


async def test_renotify_after_6h_for_warn_and_1h_for_critical(
    engine: AsyncEngine, rig: Rig, topic: respx.Route
) -> None:
    warn = await raised(engine, "run.partial", "warn", subject="w")
    critical = await raised(engine, "run.failed", "critical", subject="c")
    clock = rig.clock

    async def pass_at(at: timedelta) -> list[str]:
        clock.now = T0 + at
        before = topic.call_count
        await rig.dispatcher.pass_once()
        return pushed(topic)[before:]

    assert await pass_at(timedelta(0)) == [
        "run.failed c",
        "run.partial w",
    ]  # never pushed: both, critical first
    assert await pass_at(timedelta(hours=1)) == []  # exactly 1 h is not more than 1 h
    assert await pass_at(timedelta(hours=1, seconds=1)) == ["run.failed c"]
    assert await pass_at(timedelta(hours=2)) == []
    assert await pass_at(timedelta(hours=6)) == ["run.failed c"]  # the warn is 6 h old, not more
    assert await pass_at(timedelta(hours=6, seconds=1)) == ["run.partial w"]
    assert ((await row(engine, warn)).last_notified_at, (await row(engine, critical)).last_notified_at) == (
        T0 + timedelta(hours=6, seconds=1),
        T0 + timedelta(hours=6),
    )
    # an acknowledged alert is never pushed again while it stays open
    await acknowledged(engine, critical)
    assert await pass_at(timedelta(hours=9)) == []
    assert await pass_at(timedelta(hours=12, seconds=2)) == ["run.partial w"]


async def test_at_most_ten_per_pass_criticals_first(
    engine: AsyncEngine, rig: Rig, topic: respx.Route
) -> None:
    for i in range(6):
        await raised(engine, "run.partial", "warn", subject=f"w{i}")
    for i in range(6):
        await raised(engine, "run.failed", "critical", subject=f"c{i}")
    # within a severity the oldest first: the warns raised last are made the oldest here
    async with engine.begin() as conn:
        await conn.execute(
            sa.text(
                "UPDATE alerts SET first_at = timestamptz '2026-11-01 00:00:00Z' - id * interval '1 minute' "
                "WHERE code = 'run.partial'"
            )
        )

    assert await rig.dispatcher.pass_once() == DispatchReport(
        sent=10, failed_alert_id=None, paused_until=None
    )
    assert pushed(topic) == [
        *(f"run.failed c{i}" for i in range(6)),
        *(f"run.partial w{i}" for i in (5, 4, 3, 2)),
    ]
    assert [call.request.headers["Priority"] for call in topic.calls] == ["5"] * 6 + ["3"] * 4
    assert await rig.dispatcher.pass_once() == DispatchReport(sent=2, failed_alert_id=None, paused_until=None)
    assert pushed(topic)[10:] == ["run.partial w1", "run.partial w0"]
    assert await rig.dispatcher.pass_once() == NOTHING


async def test_an_outage_pauses_and_backs_off_without_flooding(
    engine: AsyncEngine, rig: Rig, topic: respx.Route, log_events: Callable[[], list[dict[str, Any]]]
) -> None:
    dispatcher = rig.other(engine, token=TOKEN)
    first = await raised(engine, "backup.failed", "critical", subject="first", message="backup failed: first")
    others = [
        await raised(engine, "run.failed", "critical", subject="second", message="run failed: second"),
        await raised(engine, "run.partial", "warn", subject="third", message="run partial: third"),
    ]
    topic.return_value = httpx.Response(503)  # ntfy refuses every push

    for n, minutes in enumerate((5, 10, 20, 40, 60, 60), start=1):
        failed_at = rig.clock.now
        report = await dispatcher.pass_once()
        paused_until = failed_at + timedelta(minutes=minutes)
        assert report == DispatchReport(sent=0, failed_alert_id=first, paused_until=paused_until), n
        # one alert, four tries: nothing else of the pass is tried
        assert topic.call_count == 4 * n and set(pushed(topic)) == {"backup failed: first"}
        assert (await row(engine, first)).notify_failures == n
        assert [(await row(engine, other)).notify_failures for other in others] == [0, 0]
        [alert] = await delivery_failed(engine)
        assert (alert.dedup_key, alert.severity, alert.occurrences, alert.resolved_at) == (
            "notify.delivery_failed:ntfy",
            "warn",
            n,
            None,
        )
        assert alert.context == {"alert_id": first, "error_code": "http_503"}
        # nothing while paused
        rig.clock.now = paused_until - timedelta(seconds=1)
        assert await dispatcher.pass_once() == DispatchReport(
            sent=0, failed_alert_id=None, paused_until=paused_until
        )
        assert topic.call_count == 4 * n
        rig.clock.now = paused_until
    assert rig.sleeps.requested == [5.0, 30.0, 120.0] * 6
    for alert_id in (first, *others):
        assert (await row(engine, alert_id)).last_notified_at is None
    # the log names the alert, the error code and the pause; never the topic, the token or a message
    failures = events_named(log_events, "ntfy_delivery_failed")
    assert [(e["alert_id"], e["code"], e["error_code"], e["tries"]) for e in failures] == [
        (first, "backup.failed", "http_503", 4)
    ] * 6
    assert [e["paused_for_s"] for e in failures] == [300, 600, 1200, 2400, 3600, 3600]
    text = str(log_events())
    for secret in (TOPIC, "jh-alerts-4f2a9c", TOKEN, "backup failed: first", "run failed", "run partial"):
        assert secret not in text, secret


async def test_after_recovery_each_due_alert_is_sent_once_critical_first(
    engine: AsyncEngine, rig: Rig, topic: respx.Route, log_events: Callable[[], list[dict[str, Any]]]
) -> None:
    warn = await raised(engine, "run.partial", "warn", subject="oldest")
    first = await raised(engine, "run.failed", "critical", subject="a")
    second = await raised(engine, "backup.failed", "critical", subject="b")
    await raised(engine, "dedup.identity_conflict", "info")
    topic.return_value = httpx.Response(502)
    report = await rig.dispatcher.pass_once()
    assert (report.sent, report.failed_alert_id) == (0, first)  # the critical first
    assert report.paused_until == T0 + ntfy.PAUSE_FIRST

    topic.return_value = httpx.Response(200)  # ntfy is back
    rig.clock.now = T0 + ntfy.PAUSE_FIRST
    before = topic.call_count
    assert await rig.dispatcher.pass_once() == DispatchReport(sent=3, failed_alert_id=None, paused_until=None)
    assert pushed(topic)[before:] == ["run.failed a", "backup.failed b", "run.partial oldest"]
    [alert] = await delivery_failed(engine)
    assert alert.resolved_at is not None  # the in-app alert resolves, and was never pushed itself
    assert all("notify.delivery_failed" not in call.request.headers["Title"] for call in topic.calls)
    assert [(await row(engine, a)).last_notified_at for a in (warn, first, second)] == [rig.clock.now] * 3
    assert (await row(engine, first)).notify_failures == 1  # the failed push stays counted
    assert len(events_named(log_events, "ntfy_delivery_recovered")) == 1
    assert await rig.dispatcher.pass_once() == NOTHING  # each due alert once

    # the pause was reset: the next outage pauses 5 min again, and opens a new in-app alert
    third = await raised(engine, "backup.failed", "critical", subject="c")
    topic.return_value = httpx.Response(500)
    report = await rig.dispatcher.pass_once()
    assert report == DispatchReport(
        sent=0, failed_alert_id=third, paused_until=rig.clock.now + ntfy.PAUSE_FIRST
    )
    assert [a.resolved_at is None for a in await delivery_failed(engine)] == [False, True]


async def test_an_api_raised_alert_is_delivered_by_the_worker(
    engine: AsyncEngine,
    rig: Rig,
    topic: respx.Route,
    make_config: ConfigFactory,
    make_user: UserFactory,
    log_events: Callable[[], list[dict[str, Any]]],
) -> None:
    await make_user()
    api = create_app(cfg=make_config("api"), engine=engine)
    for i in range(GLOBAL_MAX_FAILURES):  # the global lockout: the api raises `auth.lockout` (critical)
        transport = httpx.ASGITransport(app=api, client=(f"203.0.113.{i + 1}", 1))
        headers = {"Origin": "https://testserver"}
        async with httpx.AsyncClient(
            transport=transport, base_url="https://testserver", headers=headers
        ) as c:
            r = await c.post("/api/auth/login", json={"username": TEST_USERNAME, "password": "y" * 12})
            assert r.status_code == 401
    assert topic.call_count == 0  # the api sends nothing: it has no egress

    worker = rig.other(engine, token=TOKEN)
    assert await worker.pass_once() == DispatchReport(sent=1, failed_alert_id=None, paused_until=None)
    [call] = topic.calls
    request = call.request
    assert (request.method, str(request.url), request.content) == ("POST", TOPIC, b"global login lockout")
    assert {
        name: request.headers[name] for name in ("Title", "Priority", "Tags", "Click", "Authorization")
    } == {
        "Title": "Job Hunter - auth.lockout",
        "Priority": "5",
        "Tags": "critical",
        "Click": f"{ORIGIN}/settings#alerts",
        "Authorization": f"Bearer {TOKEN}",
    }
    [event] = events_named(log_events, "alert_pushed")
    assert (event["code"], event["severity"]) == ("auth.lockout", "critical")
    assert TOKEN not in str(log_events()) and TOPIC not in str(log_events())


async def test_a_superseded_epoch_stamps_nothing(
    engine: AsyncEngine,
    migrated: PgServer,
    rig: Rig,
    topic: respx.Route,
    log_events: Callable[[], list[dict[str, Any]]],
) -> None:
    first = await raised(engine, "run.failed", "critical", subject="first")
    second = await raised(engine, "backup.failed", "critical", subject="second")

    def taken_over(request: httpx.Request) -> httpx.Response:
        """A newer worker claims the next epoch while the first push is on the wire."""
        pg_one(migrated, "app", "UPDATE worker_heartbeat SET worker_epoch = 2 WHERE id = 1")
        return httpx.Response(200)

    topic.side_effect = taken_over
    with pytest.raises(FencedOut) as refused:
        await rig.dispatcher.pass_once()
    assert refused.value.worker_superseded
    assert pushed(topic) == ["run.failed first"]  # sent, but its stamp was refused, and the pass ended
    assert [(await row(engine, a)).last_notified_at for a in (first, second)] == [None, None]

    # the superseded worker's next pass is refused before it sends anything, and its loop stops by itself
    topic.side_effect = None
    with pytest.raises(FencedOut):
        await rig.dispatcher.pass_once()
    stop = asyncio.Event()
    async with asyncio.timeout(10):
        await rig.dispatcher.run(stop)
    assert not stop.is_set() and topic.call_count == 1
    [event] = events_named(log_events, "alert_dispatcher_superseded")
    assert event["epoch"] == 1

    # the newer worker pushes the first alert again (at least once) and stamps both
    newer = rig.other(engine, epoch=2)
    assert await newer.pass_once() == DispatchReport(sent=2, failed_alert_id=None, paused_until=None)
    assert pushed(topic) == ["run.failed first", "run.failed first", "backup.failed second"]
    assert [(await row(engine, a)).last_notified_at for a in (first, second)] == [T0, T0]


async def test_an_escalated_alert_is_pushed_at_once(
    engine: AsyncEngine, rig: Rig, topic: respx.Route
) -> None:
    stale = await raised(engine, "backup.stale", "warn", subject="backup", message="backup older than 36 h")
    assert (await rig.dispatcher.pass_once()).sent == 1
    assert (topic.calls.last.request.headers["Priority"], topic.calls.last.request.headers["Tags"]) == (
        "3",
        "warn",
    )
    await acknowledged(engine, stale)
    rig.clock.now = T0 + timedelta(minutes=30)
    assert await rig.dispatcher.pass_once() == NOTHING  # acknowledged, and pushed within 6 h anyway

    # the same open alert escalates (Ruling T2-1): unacknowledged and never pushed at its new severity
    async with engine.begin() as conn:
        escalated = await raise_alert(
            conn, code="backup.stale", severity="critical", subject="backup", message="backup older than 72 h"
        )
    assert escalated == stale
    assert await rig.dispatcher.pass_once() == DispatchReport(sent=1, failed_alert_id=None, paused_until=None)
    request = topic.calls.last.request
    assert (request.headers["Priority"], request.headers["Tags"], request.content) == (
        "5",
        "critical",
        b"backup older than 72 h",
    )
    after = await row(engine, stale)
    assert (after.severity, after.last_notified_at, after.acknowledged_at) == (
        "critical",
        rig.clock.now,
        None,
    )
    assert await rig.dispatcher.pass_once() == NOTHING


async def test_the_loop_runs_its_passes_survives_a_failing_one_and_stops_on_its_event(
    engine: AsyncEngine,
    rig: Rig,
    topic: respx.Route,
    monkeypatch: pytest.MonkeyPatch,
    log_events: Callable[[], list[dict[str, Any]]],
) -> None:
    await raised(engine, "run.failed", "critical")
    stop = asyncio.Event()
    real = rig.dispatcher.pass_once
    calls: list[int] = []

    async def failing_once() -> DispatchReport:
        calls.append(len(calls))
        if len(calls) == 1:
            raise RuntimeError("detail that must not be logged")
        report = await real()
        stop.set()
        return report

    monkeypatch.setattr(rig.dispatcher, "pass_once", failing_once)
    monkeypatch.setattr(ntfy, "DISPATCH_INTERVAL_S", 0.0)  # no real wait between the passes
    async with asyncio.timeout(10):
        await rig.dispatcher.run(stop)
    assert len(calls) == 2 and pushed(topic) == ["run.failed s"]
    [event] = events_named(log_events, "alert_dispatcher_failed")
    assert event["error_class"] == "RuntimeError"
    assert "must not be logged" not in str(log_events())
    # a stop set before the loop starts runs no pass
    stopped = asyncio.Event()
    stopped.set()
    await rig.dispatcher.run(stopped)
    assert len(calls) == 2
