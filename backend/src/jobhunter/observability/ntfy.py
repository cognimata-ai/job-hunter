"""Push notifications to ntfy: the worker's alert dispatcher (spec §17.6 L5631-5632, L5700-5704; plan M5
Task 4, Q1, Q2, Q4, Q5, O3, O22a, Deviation 5).

**What is due** (`due_alerts`): an open, unacknowledged alert whose severity is the active settings'
`notifications.push_min_severity` or above (§13.1 L4909: `warn` or `critical`; `info` is in-app only,
L5631), whose code is not in `catalogue.NEVER_PUSHED` (`notify.delivery_failed`, which reports that this very
channel failed), and that was never pushed or was last pushed more than `RENOTIFY_AFTER[severity]` ago: 6 h
for warn, 1 h for critical, while it stays unacknowledged (L5632). Criticals come first, then the oldest
(`first_at`, then `id`), at most `MAX_SENDS_PER_PASS` a pass. An alert that escalated (`alerts.raise_alert`,
Ruling T2-1) has neither `last_notified_at` nor `acknowledged_at`, so it is due at once; `notify_failures`
plays no part. The threshold is read at every pass, so a change applies at the next one.

**A push** (`NtfyClient.publish`) is `POST <topic URL>` with the alert's message as its UTF-8 body (a message
holds no PII: §7.14) and the headers of `ntfy_headers`, all ASCII: `Title: Job Hunter - <code>` (an ASCII
hyphen, O3), `Priority` 5 or 3 (`catalogue.push_priority`), `Tags: <severity>`, `Click: <public
origin>/settings#alerts` (O22a), and `Authorization: Bearer <ntfy_token>` only when that secret is set. It
waits at most `SEND_TIMEOUT_S` and follows no redirect. A non-2xx answer raises `NtfyError("http_<status>")`,
a timeout `NtfyError("timeout")`, and any other transport failure (no connection, or one lost before the
answer) `NtfyError("connect")`. `deliver` makes four tries, `NTFY_RETRY_DELAYS_S` (5, 30 and 120 s) apart
(§17.6: "retries 3 times").

**A pass** (`AlertDispatcher.pass_once`, Deviation 5):
1. While the dispatcher is paused it does nothing.
2. It reads the due alerts in a transaction that starts with `epoch.fence_epoch`, so a superseded worker
   (Ruling T21-c) pushes nothing.
3. It delivers them in turn, outside any transaction. Each delivered push is stamped `last_notified_at` (the
   clock then) in a transaction that starts with the fence and also resolves `notify.delivery_failed`
   (subject `ntfy`); the pause goes back to `PAUSE_FIRST`. A worker superseded meanwhile stamps nothing, so
   the newer worker pushes that alert again, as it does after a crash between a push and its stamp: a push is
   delivered at least once.
4. The first alert whose four tries all fail ends the pass. One fenced transaction counts one more
   `notify_failures` on its row and raises `notify.delivery_failed` (subject `ntfy`, warn, in-app only), a
   repeat of the open one when one is open. The dispatcher then pauses for `PAUSE_FIRST`, and for twice as
   long after each further failing pass, up to `PAUSE_MAX`: 5, 10, 20, 40, 60, 60 min. An outage costs one
   alert four tries a pass, never a flood.

A resolution is never pushed. The pause lives in memory: a restarted worker starts unpaused.

**The loop** (`AlertDispatcher.run`) runs a pass at its start and every `DISPATCH_INTERVAL_S` until its stop
event is set. A pass that raises is logged `alert_dispatcher_failed` with its error class only (a database
error can carry statement parameters) and the next pass runs on time (`heartbeat_watch.watch_heartbeat`'s
pattern). A fence that refuses the worker's epoch ends the loop instead (`alert_dispatcher_superseded`): a
newer worker took over, and `worker.main` then requests this worker's graceful stop (Ruling T24-a).

**The logs** name an alert's id and code, an error code and counts: never the topic URL (registered for
masking by `config.startup_or_exit` all the same), the token or a message.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Final, Literal

import httpx
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from jobhunter.db.sql import q
from jobhunter.errors import FencedOut
from jobhunter.observability.alerts import raise_alert, resolve_alert
from jobhunter.observability.catalogue import CATALOGUE, NEVER_PUSHED, SEVERITIES, push_priority
from jobhunter.observability.logging import get_logger
from jobhunter.settings.dryrun import active_settings
from jobhunter.worker.epoch import fence_epoch

PushSeverity = Literal["warn", "critical"]

NTFY_RETRY_DELAYS_S: Final = (5.0, 30.0, 120.0)  # between four tries
DISPATCH_INTERVAL_S: Final = 30.0
MAX_SENDS_PER_PASS: Final = 10
RENOTIFY_AFTER: Final[Mapping[PushSeverity, timedelta]] = MappingProxyType(
    {"warn": timedelta(hours=6), "critical": timedelta(hours=1)}
)
PAUSE_FIRST: Final = timedelta(minutes=5)
PAUSE_MAX: Final = timedelta(minutes=60)
SEND_TIMEOUT_S: Final = 10.0
DELIVERY_FAILED: Final = "notify.delivery_failed"
DELIVERY_FAILED_SUBJECT: Final = "ntfy"

TRIES: Final = len(NTFY_RETRY_DELAYS_S) + 1
TITLE_PREFIX: Final = "Job Hunter - "
CLICK_PATH: Final = "/settings#alerts"

Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]

# Open, unacknowledged, at or above the threshold (`:severities`), not in-app only, and never pushed or pushed
# longer ago than its severity's interval; criticals first, then the oldest.
_DUE: Final = q(
    "SELECT id, code, severity, message FROM alerts "
    "WHERE resolved_at IS NULL AND acknowledged_at IS NULL "
    "AND severity = ANY(CAST(:severities AS text[])) AND code <> ALL(CAST(:never_pushed AS text[])) "
    "AND (last_notified_at IS NULL "
    "OR (severity = 'warn' AND last_notified_at < :warn_before) "
    "OR (severity = 'critical' AND last_notified_at < :critical_before)) "
    "ORDER BY severity = 'critical' DESC, first_at, id LIMIT :limit"
)
_STAMP: Final = q("UPDATE alerts SET last_notified_at = :at WHERE id = :id")
_COUNT_FAILURE: Final = q("UPDATE alerts SET notify_failures = notify_failures + 1 WHERE id = :id")

log = get_logger("jobhunter.observability.ntfy")


class NtfyError(Exception):
    """A push that ntfy did not take: `code` is `timeout`, `connect` or `http_<status>` (module docstring)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class DueAlert:
    """An alert to push: its id, code, severity and message (the body; no PII, kept out of its repr)."""

    id: int
    code: str
    severity: PushSeverity
    message: str = field(repr=False)


@dataclass(frozen=True)
class DispatchReport:
    """What a pass did: the alerts it pushed, the alert whose push failed for good (which ended it), and the
    end of the pause the dispatcher is in (`None` when it is not paused)."""

    sent: int
    failed_alert_id: int | None
    paused_until: datetime | None


def _push_severity(severity: str) -> PushSeverity:
    match severity:
        case "warn" | "critical":
            return severity
        case _:
            raise ValueError(f"an alert of severity {severity!r} is never pushed")


def ntfy_headers(alert: DueAlert, *, public_origin: str, token: str | None) -> dict[str, str]:
    """The headers of `alert`'s push (module docstring): ASCII only; `Authorization` only with a `token`."""
    priority = push_priority(alert.severity)
    if priority is None:  # unreachable: a `DueAlert` is warn or critical
        raise ValueError(f"an alert of severity {alert.severity!r} is never pushed")
    headers = {
        "Title": f"{TITLE_PREFIX}{alert.code}",
        "Priority": str(priority),
        "Tags": alert.severity,
        "Click": f"{public_origin}{CLICK_PATH}",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def make_http_client(*, user_agent: str) -> httpx.AsyncClient:
    """The HTTP client of an `NtfyClient`: `SEND_TIMEOUT_S`, the `User-Agent`, no redirects."""
    return httpx.AsyncClient(
        timeout=SEND_TIMEOUT_S, headers={"User-Agent": user_agent}, follow_redirects=False
    )


class NtfyClient:
    """Pushes alerts to one ntfy topic (module docstring). Its repr shows neither the topic nor the token."""

    def __init__(
        self, http: httpx.AsyncClient, *, topic_url: str, token: str | None, public_origin: str
    ) -> None:
        self._http = http
        self._topic_url = topic_url
        self._token = token
        self._public_origin = public_origin

    async def publish(self, alert: DueAlert) -> None:
        """One try: `POST` `alert` to the topic; `NtfyError` unless ntfy answers 2xx."""
        headers = ntfy_headers(alert, public_origin=self._public_origin, token=self._token)
        try:
            response = await self._http.post(
                self._topic_url,
                content=alert.message.encode("utf-8"),
                headers=headers,
                timeout=SEND_TIMEOUT_S,
                follow_redirects=False,
            )
        except httpx.TimeoutException:
            raise NtfyError("timeout") from None  # not chained: httpx's errors can name the URL
        except httpx.TransportError:
            raise NtfyError("connect") from None
        if not response.is_success:
            raise NtfyError(f"http_{response.status_code}")


async def deliver(client: NtfyClient, alert: DueAlert, *, sleep: Sleep) -> None:
    """Push `alert` in up to four tries, `NTFY_RETRY_DELAYS_S` apart; the last try's `NtfyError` when all
    four fail. Each failed try but the last is logged `ntfy_try_failed`."""
    for delay in NTFY_RETRY_DELAYS_S:
        try:
            await client.publish(alert)
        except NtfyError as refused:
            log.warning(
                "ntfy_try_failed",
                alert_id=alert.id,
                code=alert.code,
                error_code=refused.code,
                retry_in_s=delay,
            )
            await sleep(delay)
        else:
            return
    await client.publish(alert)


async def due_alerts(conn: AsyncConnection, *, now: datetime, min_severity: PushSeverity) -> list[DueAlert]:
    """The alerts a pass at `now` pushes under the threshold `min_severity`, in their order (module
    docstring), at most `MAX_SENDS_PER_PASS`."""
    params = {
        "severities": list(SEVERITIES[SEVERITIES.index(min_severity) :]),
        "never_pushed": sorted(NEVER_PUSHED),
        "warn_before": now - RENOTIFY_AFTER["warn"],
        "critical_before": now - RENOTIFY_AFTER["critical"],
        "limit": MAX_SENDS_PER_PASS,
    }
    return [
        DueAlert(id=int(row.id), code=row.code, severity=_push_severity(row.severity), message=row.message)
        for row in await conn.execute(_DUE, params)
    ]


class AlertDispatcher:
    """The worker's alert dispatcher (module docstring): `pass_once` is one pass, `run` the loop."""

    def __init__(
        self, engine: AsyncEngine, client: NtfyClient, *, epoch: int, clock: Clock, sleep: Sleep
    ) -> None:
        self._engine = engine
        self._client = client
        self._epoch = epoch
        self._clock = clock
        self._sleep = sleep
        self._paused_until: datetime | None = None
        self._pause = PAUSE_FIRST  # the next pause's length

    async def pass_once(self) -> DispatchReport:
        """One pass (module docstring). Raises `FencedOut` when the fence refuses this worker's epoch, having
        stamped nothing since its last successful stamp."""
        now = self._clock()
        if self._paused_until is not None and now < self._paused_until:
            return DispatchReport(sent=0, failed_alert_id=None, paused_until=self._paused_until)
        self._paused_until = None
        async with self._engine.begin() as conn:
            await fence_epoch(conn, self._epoch)
            threshold = (await active_settings(conn)).notifications.push_min_severity
            due = await due_alerts(conn, now=now, min_severity=threshold)
        sent = 0
        for alert in due:
            try:
                await deliver(self._client, alert, sleep=self._sleep)
            except NtfyError as refused:
                paused_until = await self._failed(alert, refused.code)
                return DispatchReport(sent=sent, failed_alert_id=alert.id, paused_until=paused_until)
            await self._delivered(alert)
            sent += 1
        return DispatchReport(sent=sent, failed_alert_id=None, paused_until=None)

    async def _delivered(self, alert: DueAlert) -> None:
        """Stamp `alert`'s push and resolve `notify.delivery_failed`, under the fence; end the backoff."""
        async with self._engine.begin() as conn:
            await fence_epoch(conn, self._epoch)
            await conn.execute(_STAMP, {"id": alert.id, "at": self._clock()})
            recovered = await resolve_alert(conn, code=DELIVERY_FAILED, subject=DELIVERY_FAILED_SUBJECT)
        self._pause = PAUSE_FIRST
        log.info("alert_pushed", alert_id=alert.id, code=alert.code, severity=alert.severity)
        if recovered:
            log.info("ntfy_delivery_recovered", alert_id=alert.id)

    async def _failed(self, alert: DueAlert, error_code: str) -> datetime:
        """Count the failed push on `alert` and raise `notify.delivery_failed`, under the fence; pause the
        dispatcher (module docstring) and return the pause's end."""
        async with self._engine.begin() as conn:
            await fence_epoch(conn, self._epoch)
            await conn.execute(_COUNT_FAILURE, {"id": alert.id})
            await raise_alert(
                conn,
                code=DELIVERY_FAILED,
                severity=CATALOGUE[DELIVERY_FAILED].severity,
                subject=DELIVERY_FAILED_SUBJECT,
                message=f"ntfy did not take a push in {TRIES} tries",
                context={"alert_id": alert.id, "error_code": error_code},
            )
        pause = self._pause
        self._paused_until = self._clock() + pause
        self._pause = min(pause * 2, PAUSE_MAX)
        log.error(
            "ntfy_delivery_failed",
            alert_id=alert.id,
            code=alert.code,
            error_code=error_code,
            tries=TRIES,
            paused_for_s=int(pause.total_seconds()),
        )
        return self._paused_until

    async def run(self, stop: asyncio.Event) -> None:
        """Run a pass at the start and every `DISPATCH_INTERVAL_S` until `stop` is set; return earlier only
        when a fence refused this worker's epoch (module docstring)."""
        while not stop.is_set():
            try:
                await self.pass_once()
            except Exception as exc:  # noqa: BLE001 - a task boundary: the next pass runs; its class only is logged
                if isinstance(exc, FencedOut) and exc.worker_superseded:
                    log.warning("alert_dispatcher_superseded", epoch=self._epoch)
                    return
                log.warning("alert_dispatcher_failed", error_class=type(exc).__name__)
            with suppress(TimeoutError):
                async with asyncio.timeout(DISPATCH_INTERVAL_S):
                    await stop.wait()
