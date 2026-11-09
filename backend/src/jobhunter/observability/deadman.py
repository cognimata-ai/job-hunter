"""The dead-man switch on healthchecks.io (spec §17.7; plan M5 Task 5, O-ops Q6, Q7, Q8, Q14, O2, O5, O10,
O18, Deviation 9).

**The check** watches collection liveness from off the box: a run that ends `succeeded` or `partial` pings it
(`GET <ping URL>`), one that ends `failed` or `failed_invariant` pings `<ping URL>/fail` (`ping_kind`), so a
missing run alarms even when the VPS is dead. Only a scheduled or catch-up run pings, never a manual one, but
for a manual run whose `[queued_at, finished_at]` holds the slot of a `skipped_overlap` run: its activity
skipped that slot, so it pings with its own status (`runs_to_ping`, O18, M5-R2). Its period follows the
schedule: `timeout = interval_s`, `grace = grace_s(interval_s)`, applied through the Management API on the
check's uuid, the last path segment of the ping URL (`check_uuid`; the startup check
`healthchecks_ping_url_form` refuses any other form in prod, O5).

**The client** (`HealthchecksClient`) takes no proxy or certificates from the environment, follows no
redirect and waits at most `REQUEST_TIMEOUT_S` a request. A ping makes three tries, each at its offset from
the first (`PING_TRY_OFFSETS_S`: 0, 2 and 10 s, on an injected monotonic clock) or when the try before it
ended, whichever is later: 0, 2 and 10 s when the tries fail at once, 0, 10 and 20 s when each waits out its
timeout. When all three fail it logs `deadman_ping_failed` with the last error's class and returns `False`: it
never raises and raises no alert (the off-box switch is the signal). The API calls (`read_check`,
`update_check`, `pause`) send the key as `X-Api-Key` and raise `DeadmanApiError` with a code: `timeout`,
`connect`, `invalid_request` (a request that cannot be built or sent as it stands), `http_<status>` or
`invalid_response` (any other request error, an undecodable body among them, and a body that is no check);
`apply_check` adds `resume_failed` and `not_applied`. No error is chained to httpx's, whose messages name the
URL; no log line names the URL, the uuid or the key.

**One lock** (`DEADMAN_LOCK_KEY`, a one-key session advisory lock, `deadman_lock`): every ping decision and
every sync action runs holding it, on a dedicated autocommit connection of an engine without a pool, so no
transaction is open during an HTTP call and the lock ends with that connection whatever happens. Every
database write made under it is a short transaction of its own, which starts with the worker's epoch fence
(`fence_epoch`) when the pinger has an epoch.

**The pings** (`DeadmanPinger`). Under the lock a ping first reads `schedule_state.enabled`,
`deadman_state.applied_paused` and the watermark `deadman_state.last_pinged_run_id`: a disabled schedule or a
paused check sends no ping, and the watermark still moves past the run, so a run that ends after the schedule
was disabled can never resume a check the sync paused (Deviation 9). The watermark moves only past a run of
`runs_to_ping` (`GREATEST`: never back), and moves whether or not healthchecks took the ping (the switch
alarms on the missing one).
- `after_run(run_id)`, the fast path, runs after the run-ending commit (never inside it) in
  `finalize_from_graph` and `runs._finalize`: it pings every run of `runs_to_ping` up to `run_id`, in id
  order, so a lower run still waiting for the tick (an end no finalize wrote) is pinged first and never passed
  over (fix round 1, M1). It never raises into its caller: a failure is logged `deadman_fast_path_failed` with
  its class, and the tick covers it.
- `ping_pending()`, the durable path, pings every run of `runs_to_ping` above the watermark, in id order:
  the ends no finalize writes (`resume_exhausted`, `orphaned_queue`, O10) and a ping lost to a crash between a
  commit and its ping. The `deadman_ping` reconciler runs it at every tick; delete-everything shares it.
- `arm()`, once at the worker's start (`worker.main` step 8, Ruling T5-2): a watermark never set (the seeded
  NULL) is set, under the lock, in one fenced transaction, to the newest collection run, or to just below the
  oldest one still `queued` or `running` (which ends after this start and pings then), sending no ping: an
  upgraded or restored database never replays its history. Logged `deadman_armed` with that run id. A
  watermark already set is left, and a database without a run keeps its NULL, which both paths read as 0.

A ping is sent at least once (fix round 1, M2): the watermark moves in a transaction after the HTTP ping, so
a cancellation between the two (the fast path is cancelled once the ended run's next heartbeat is refused)
leaves the run above the watermark, and the next tick sends that ping again. A repeat of the same kind
changes nothing on the check, and the pings keep their order.

**The period sync** (`DeadmanPinger.sync`, the `deadman_sync` task, O2). The tick compares
`schedule_state(interval_s, enabled)` with `deadman_state(applied_interval_s, applied_paused)` and, when they
differ or `synced_at` is older than `RESYNC_AFTER` (24 h), and `next_try_at` has passed, defers `deadman_sync`
(`aux`, queueing lock `deadman_sync`); the task also runs daily (`23 4 * * *`). Under the lock it reads the
check back (`apply_check`): an enabled schedule gets `timeout` and `grace` where they differ and, when the
check is paused, its resume by a `success` ping; a disabled one gets its pause; whatever changed is read back
again and must hold. It then stamps `applied_*` and `synced_at`, clears `last_error_code` and `next_try_at`,
and resolves `deadman.update_failed`. A failure raises `deadman.update_failed` (warn, subject `healthchecks`),
records `last_error_code` and sets `next_try_at` to `SYNC_BACKOFF_FIRST` (5 min) from now, doubling with each
consecutive failure (the open alert's occurrences, so the backoff survives a restart and resets once the
sync succeeds) up to `SYNC_BACKOFF_MAX` (60 min). Pings go on meanwhile.

**Without both secrets** (dev, test) the worker builds no pinger (`healthchecks_secrets` is `None`): no ping,
no sync, and one `deadman_disabled` line at start (Q8).
"""

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any, Final, Literal

import httpx
import sqlalchemy as sa
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from jobhunter.clock import utcnow
from jobhunter.config import AppConfig, ping_url_uuid
from jobhunter.db.sql import q
from jobhunter.observability.alerts import raise_alert, resolve_alert
from jobhunter.observability.catalogue import CATALOGUE
from jobhunter.observability.logging import get_logger
from jobhunter.worker.epoch import fence_epoch
from jobhunter.worker.reconcilers import ReconcileContext, reconciler

PingKind = Literal["success", "fail"]
Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]

# The project's advisory keys are listed in `db.migrate` (a one-key session lock).
DEADMAN_LOCK_KEY: Final = 7261005
LOCK_POLL_S: Final = 0.5
REQUEST_TIMEOUT_S: Final = 10.0
PING_TRY_OFFSETS_S: Final = (0.0, 2.0, 10.0)  # each try's earliest start, from the first's
PING_TRIES: Final = len(PING_TRY_OFFSETS_S)
GRACE_MIN_S: Final = 3_600
GRACE_MAX_S: Final = 43_200
RESYNC_AFTER: Final = timedelta(hours=24)
SYNC_BACKOFF_FIRST: Final = timedelta(minutes=5)
SYNC_BACKOFF_MAX: Final = timedelta(minutes=60)
SYNC_TASK: Final = "deadman_sync"
UPDATE_FAILED: Final = "deadman.update_failed"
UPDATE_FAILED_SUBJECT: Final = "healthchecks"
# The statuses that ping, and how (§17.7); every other status pings nothing.
PING_KINDS: Final[Mapping[str, PingKind]] = MappingProxyType(
    {"succeeded": "success", "partial": "success", "failed": "fail", "failed_invariant": "fail"}
)

_TRY_LOCK: Final = q("SELECT pg_try_advisory_lock(CAST(:key AS bigint))")
_UNLOCK: Final = q("SELECT pg_advisory_unlock(CAST(:key AS bigint))")
# The terminal runs above the watermark that ping (module docstring), in id order.
_RUNS_TO_PING: Final = q(
    "SELECT r.id, r.status FROM runs r "
    "WHERE r.id > :after AND r.kind = 'collect' AND r.status = ANY(CAST(:statuses AS text[])) "
    "AND (r.trigger IN ('schedule', 'catch_up') OR (r.trigger = 'manual' AND EXISTS ("
    "SELECT 1 FROM runs s WHERE s.kind = r.kind AND s.status = 'skipped_overlap' "
    "AND s.scheduled_slot BETWEEN r.queued_at AND r.finished_at))) "
    "ORDER BY r.id"
)
_GUARD: Final = q(
    "SELECT s.enabled, d.applied_paused, d.last_pinged_run_id "
    "FROM schedule_state s CROSS JOIN deadman_state d WHERE s.id = 1 AND d.id = 1"
)
_ADVANCE: Final = q(
    "UPDATE deadman_state SET last_pinged_run_id = GREATEST(last_pinged_run_id, :run_id) WHERE id = 1"
)
_SCHEDULE: Final = q("SELECT interval_s, enabled FROM schedule_state WHERE id = 1")
_SYNC_STATE: Final = q(
    "SELECT s.interval_s, s.enabled, d.applied_interval_s, d.applied_paused, d.synced_at, d.next_try_at "
    "FROM schedule_state s CROSS JOIN deadman_state d WHERE s.id = 1 AND d.id = 1"
)
_SYNCED: Final = q(
    "UPDATE deadman_state SET applied_interval_s = :interval_s, applied_timeout_s = :timeout_s, "
    "applied_grace_s = :grace_s, applied_paused = :paused, synced_at = :now, last_error_code = NULL, "
    "next_try_at = NULL WHERE id = 1"
)
_SYNC_FAILED: Final = q(
    "UPDATE deadman_state SET last_error_code = :code, next_try_at = :next_try_at WHERE id = 1"
)
_OCCURRENCES: Final = q("SELECT occurrences FROM alerts WHERE id = :id")
# `arm`: a NULL watermark set to the newest collection run, but below the oldest still active (it pings when
# it ends); no row is set without a run, and a set watermark is left (module docstring).
_ARM: Final = q(
    "UPDATE deadman_state SET last_pinged_run_id = armed.run_id FROM ("
    "SELECT LEAST(max(id), min(id) FILTER (WHERE status IN ('queued', 'running')) - 1) AS run_id "
    "FROM runs WHERE kind = 'collect') AS armed "
    "WHERE deadman_state.id = 1 AND deadman_state.last_pinged_run_id IS NULL AND armed.run_id IS NOT NULL "
    "RETURNING deadman_state.last_pinged_run_id"
)

log = get_logger("jobhunter.observability.deadman")


def grace_s(interval_s: int) -> int:
    """The check's grace for a schedule of `interval_s`: a quarter of it, within 1 h and 12 h (§17.7)."""
    return min(max(interval_s // 4, GRACE_MIN_S), GRACE_MAX_S)


def check_uuid(ping_url: str) -> str:
    """The check's uuid, the last path segment of `ping_url`, lower-cased; `ValueError` (which never names the
    URL) for any other form, the slug form `/<ping-key>/<slug>` among them (O5)."""
    uuid = ping_url_uuid(ping_url)
    if uuid is None:
        raise ValueError("the ping URL does not end in the check's uuid")
    return uuid


def ping_kind(status: str) -> PingKind | None:
    """How a run that ended `status` pings: `success`, `fail`, or `None` for no ping (§17.7)."""
    return PING_KINDS.get(status)


def healthchecks_secrets(cfg: AppConfig) -> tuple[str, str] | None:
    """The ping URL and the Management API key, each stripped of outer whitespace; `None` unless both secrets
    are set (Q8)."""
    ping_url, api_key = (
        cfg.secrets.get(name, b"").decode("utf-8", errors="replace").strip()
        for name in ("healthchecks_ping_url", "healthchecks_api_key")
    )
    return (ping_url, api_key) if ping_url and api_key else None


class DeadmanApiError(Exception):
    """A Management API call that failed: `code` names the failure (module docstring), never the URL or the
    key."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class CheckState:
    """The check's period as healthchecks reports it (a check on a cron schedule has no `timeout`: 0)."""

    timeout_s: int
    grace_s: int
    paused: bool


def make_http_client(*, user_agent: str) -> httpx.AsyncClient:
    """The HTTP client of a `HealthchecksClient`: `REQUEST_TIMEOUT_S`, the `User-Agent`, no redirects, and no
    proxy or certificates from the environment (as the alert dispatcher's)."""
    return httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT_S,
        headers={"User-Agent": user_agent},
        follow_redirects=False,
        trust_env=False,
    )


def _whole(value: Any) -> int | None:
    """`value` when it is a whole number (a JSON integer, never a boolean), else `None`."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class HealthchecksClient:
    """The one check's pings and Management API calls (module docstring). Its repr names neither the URL nor
    the key. `ValueError` when `ping_url` does not end in a uuid (`check_uuid`)."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        ping_url: str,
        api_url: str,
        api_key: str,
        sleep: Sleep = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        uuid = check_uuid(ping_url)
        self._http = http
        self._ping_url = ping_url
        parsed = httpx.URL(ping_url)
        self._fail_url = str(parsed.copy_with(path=f"{parsed.path}/fail"))
        self._api_url = api_url
        self._check_url = f"{api_url.rstrip('/')}/checks/{uuid}"
        self._api_key = api_key
        self._sleep = sleep
        self._monotonic = monotonic

    async def _ping_once(self, url: str) -> str | None:
        """One `GET` of `url`; `None` when healthchecks answered 2xx, else the error's class (`http_<status>`
        for another answer)."""
        try:
            response = await self._http.get(url, timeout=REQUEST_TIMEOUT_S, follow_redirects=False)
        except Exception as exc:  # noqa: BLE001 - a ping never raises (§17.7: the switch is the signal); its class is logged
            return type(exc).__name__
        return None if response.is_success else f"http_{response.status_code}"

    async def ping(self, kind: PingKind) -> bool:
        """Ping the check (`success`) or its `/fail` in up to `PING_TRIES` tries, each at its offset or when
        the one before it ended, whichever is later (module docstring); whether one was taken. All failing is
        logged `deadman_ping_failed`; nothing is raised."""
        url = self._ping_url if kind == "success" else self._fail_url
        start = self._monotonic()
        error: str | None = None
        for offset in PING_TRY_OFFSETS_S:
            wait = start + offset - self._monotonic()
            if wait > 0:
                await self._sleep(wait)
            error = await self._ping_once(url)
            if error is None:
                return True
        log.warning("deadman_ping_failed", kind=kind, tries=PING_TRIES, error_class=error)
        return False

    async def _api(self, method: str, url: str, *, body: dict[str, int] | None = None) -> httpx.Response:
        """One Management API call with the key; its 2xx answer, else `DeadmanApiError` (module docstring)."""
        try:
            response = await self._http.request(
                method,
                url,
                json=body,
                headers={"X-Api-Key": self._api_key},
                timeout=REQUEST_TIMEOUT_S,
                follow_redirects=False,
            )
        except httpx.TimeoutException:
            raise DeadmanApiError("timeout") from None  # not chained: httpx's errors can name the URL
        except (httpx.InvalidURL, httpx.UnsupportedProtocol, httpx.LocalProtocolError, UnicodeEncodeError):
            # The request cannot be built or sent as it stands: the API URL, or a header value (the key).
            raise DeadmanApiError("invalid_request") from None
        except httpx.TransportError:
            raise DeadmanApiError("connect") from None
        except httpx.RequestError:
            # Any other failure of the exchange (fix round 1, M5): an answer whose body cannot be decoded.
            raise DeadmanApiError("invalid_response") from None
        if not response.is_success:
            raise DeadmanApiError(f"http_{response.status_code}")
        return response

    async def read_check(self) -> CheckState:
        """`GET <api>/checks/<uuid>`: the check's timeout, grace and whether it is paused."""
        response = await self._api("GET", self._check_url)
        try:
            body = response.json()
            timeout, grace, status = _whole(body.get("timeout", 0)), _whole(body["grace"]), body["status"]
        except (ValueError, LookupError, TypeError, AttributeError):
            # No JSON, or no check in it (fix round 1, M5): the failure path, with its alert and backoff.
            raise DeadmanApiError("invalid_response") from None
        if timeout is None or grace is None or not isinstance(status, str):
            raise DeadmanApiError("invalid_response")
        return CheckState(timeout_s=timeout, grace_s=grace, paused=status == "paused")

    async def update_check(self, *, timeout_s: int, grace_s: int) -> None:
        """`POST <api>/checks/<uuid>` with `{"timeout", "grace"}`."""
        await self._api("POST", self._check_url, body={"timeout": timeout_s, "grace": grace_s})

    async def pause(self) -> None:
        """`POST <api>/checks/<uuid>/pause`."""
        await self._api("POST", f"{self._check_url}/pause")


async def apply_check(client: HealthchecksClient, *, interval_s: int, enabled: bool) -> CheckState:
    """Bring the check in line with a schedule of `interval_s`, `enabled` or not (module docstring); the check
    as read back. `DeadmanApiError` when a call fails, `resume_failed` when no resume ping was taken, and
    `not_applied` when what was read back after a change does not hold it."""
    current = await client.read_check()
    if not enabled:
        if current.paused:
            return current
        await client.pause()
        paused = await client.read_check()
        if not paused.paused:
            raise DeadmanApiError("not_applied")
        return paused
    target = CheckState(timeout_s=interval_s, grace_s=grace_s(interval_s), paused=False)
    changed = False
    if (current.timeout_s, current.grace_s) != (target.timeout_s, target.grace_s):
        await client.update_check(timeout_s=target.timeout_s, grace_s=target.grace_s)
        changed = True
    if current.paused:
        # §17.7: re-enabling resumes the check with a ping (healthchecks resumes a paused check at its next
        # ping)
        if not await client.ping("success"):
            raise DeadmanApiError("resume_failed")
        changed = True
    if not changed:
        return current
    applied = await client.read_check()
    if applied != target:
        raise DeadmanApiError("not_applied")
    return applied


@dataclass(frozen=True)
class RunToPing:
    """A run that pings, and how."""

    run_id: int
    kind: PingKind


async def runs_to_ping(conn: AsyncConnection, *, after_run_id: int) -> list[RunToPing]:
    """The terminal runs above `after_run_id` that ping (`ping_kind`), in id order: scheduled and catch-up
    runs, and a manual run whose `[queued_at, finished_at]` holds the slot of a `skipped_overlap` run
    (O18)."""
    params = {"after": after_run_id, "statuses": sorted(PING_KINDS)}
    return [
        RunToPing(int(row.id), PING_KINDS[row.status]) for row in await conn.execute(_RUNS_TO_PING, params)
    ]


async def read_schedule(conn: AsyncConnection) -> tuple[int, bool] | None:
    """`schedule_state`'s `(interval_s, enabled)`; `None` without its row."""
    row = (await conn.execute(_SCHEDULE)).one_or_none()
    return None if row is None else (int(row.interval_s), bool(row.enabled))


@asynccontextmanager
async def deadman_lock(lock_engine: AsyncEngine) -> AsyncIterator[None]:
    """Hold `DEADMAN_LOCK_KEY` for the block on a dedicated autocommit connection of `lock_engine`, an engine
    without a pool (`NullPool`): no transaction stays open, and the lock ends with the connection on every
    path. The wait polls `pg_try_advisory_lock` every `LOCK_POLL_S`, as `migrate`'s lock does: a blocked
    statement would hold a snapshot while it waits."""
    async with lock_engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        acquired = bool((await conn.execute(_TRY_LOCK, {"key": DEADMAN_LOCK_KEY})).scalar_one())
        while not acquired:  # polls the database, so there is no event to wait for
            await asyncio.sleep(LOCK_POLL_S)
            acquired = bool((await conn.execute(_TRY_LOCK, {"key": DEADMAN_LOCK_KEY})).scalar_one())
        try:
            yield
        finally:
            # A lost connection took the lock with it; do not let that hide the error that ended the block.
            with suppress(sa.exc.DBAPIError):
                await conn.execute(_UNLOCK, {"key": DEADMAN_LOCK_KEY})


def _sync_due(row: Row[Any], now: datetime) -> bool:
    """Whether the tick defers `deadman_sync` at `now` (module docstring)."""
    if row.next_try_at is not None and now < row.next_try_at:
        return False
    differs = row.applied_interval_s != row.interval_s or row.applied_paused != (not row.enabled)
    return differs or row.synced_at is None or now - row.synced_at > RESYNC_AFTER


def _backoff(occurrences: int) -> timedelta:
    """The wait after the `occurrences`-th consecutive failed sync: 5 min, doubling, at most 60 min."""
    doublings = min(max(occurrences - 1, 0), 4)  # 5 x 2^4 = 80 min: past the cap already
    return min(SYNC_BACKOFF_FIRST * (1 << doublings), SYNC_BACKOFF_MAX)


class DeadmanPinger:
    """The worker's dead-man: its pings and its period sync (module docstring). `epoch` fences every write
    (`None`: unfenced, for a process that is no worker)."""

    def __init__(
        self, engine: AsyncEngine, client: HealthchecksClient, *, epoch: int | None, clock: Clock = utcnow
    ) -> None:
        self._engine = engine
        self._client = client
        self._epoch = epoch
        self._clock = clock
        self._lock_engine: AsyncEngine | None = None  # made on first use, on the engine's database

    @asynccontextmanager
    async def _locked(self) -> AsyncIterator[None]:
        if self._lock_engine is None:
            self._lock_engine = create_async_engine(
                self._engine.url, poolclass=NullPool, hide_parameters=True
            )
        async with deadman_lock(self._lock_engine):
            yield

    async def _fence(self, conn: AsyncConnection) -> None:
        if self._epoch is not None:
            await fence_epoch(conn, self._epoch)

    async def _pending(self) -> tuple[list[RunToPing], bool]:
        """The runs to ping above the watermark, and whether the check may be pinged now (module
        docstring)."""
        async with self._engine.begin() as conn:
            await self._fence(conn)
            guard = (await conn.execute(_GUARD)).one()
            pending = await runs_to_ping(conn, after_run_id=guard.last_pinged_run_id or 0)
        return pending, bool(guard.enabled) and guard.applied_paused is not True

    async def _ping(self, run: RunToPing, *, may_ping: bool) -> None:
        """Ping for `run` when the check may be pinged, then move the watermark past it, in a short fenced
        transaction."""
        if may_ping:
            taken = await self._client.ping(run.kind)
            log.info("deadman_pinged", run_id=run.run_id, kind=run.kind, taken=taken)
        else:
            log.info("deadman_ping_withheld", run_id=run.run_id, kind=run.kind)
        async with self._engine.begin() as conn:
            await self._fence(conn)
            await conn.execute(_ADVANCE, {"run_id": run.run_id})

    async def arm(self) -> int | None:
        """At the worker's start (module docstring): a NULL watermark set past the runs that ended before it,
        under the lock, in one fenced transaction, with no ping; the run id it was set to, or `None` when it
        was set already or there is no run. Raises `FencedOut` when the fence refuses this worker's epoch.
        The gap (fix round 1, M3, parked): at the first armed start, a run that this start's step-5 takeover
        sweep ended `failed('resume_exhausted')` lies at or below the arming and is never pinged; the
        dead-man switch itself still fires on the missing ping."""
        async with self._locked():
            async with self._engine.begin() as conn:
                await self._fence(conn)
                armed = (await conn.execute(_ARM)).scalar_one_or_none()
        if armed is None:
            return None
        log.info("deadman_armed", run_id=int(armed))
        return int(armed)

    async def _ping_up_to(self, last_run_id: int | None) -> int:
        """Every run of `runs_to_ping` above the watermark, up to `last_run_id` (`None`: all), pinged and
        passed in id order under the lock; how many."""
        async with self._locked():
            pending, may_ping = await self._pending()
            due = [run for run in pending if last_run_id is None or run.run_id <= last_run_id]
            for run in due:
                await self._ping(run, may_ping=may_ping)
        return len(due)

    async def ping_pending(self) -> int:
        """The durable path: every run of `runs_to_ping` above the watermark pinged and passed, in id order,
        under the lock; how many."""
        return await self._ping_up_to(None)

    async def after_run(self, run_id: int) -> None:
        """The fast path, after the commit that ended `run_id`: every run of `runs_to_ping` up to it pinged,
        in id order (module docstring). Never raises (but a cancellation): a failure is logged with its class,
        and the tick pings."""
        try:
            await self._ping_up_to(run_id)
        except Exception as exc:  # noqa: BLE001 - the run's end committed; the durable path sends what this missed
            log.warning("deadman_fast_path_failed", run_id=run_id, error_class=type(exc).__name__)

    async def sync(self) -> CheckState | None:
        """The `deadman_sync` task (module docstring), under the lock; the check as read back, or `None` when
        the sync failed (recorded). Raises `FencedOut` when the fence refuses this worker's epoch."""
        async with self._locked():
            async with self._engine.begin() as conn:
                await self._fence(conn)
                schedule = await read_schedule(conn)
            if schedule is None:
                raise LookupError("schedule_state has no row 1 (migrate seeds it)")
            interval_s, enabled = schedule
            try:
                check = await apply_check(self._client, interval_s=interval_s, enabled=enabled)
            except DeadmanApiError as failed:
                await self._sync_failed(failed.code)
                return None
            await self._synced(interval_s, check)
            return check

    async def _synced(self, interval_s: int, check: CheckState) -> None:
        params = {
            "interval_s": interval_s,
            "timeout_s": check.timeout_s,
            "grace_s": check.grace_s,
            "paused": check.paused,
            "now": self._clock(),
        }
        async with self._engine.begin() as conn:
            await self._fence(conn)
            recovered = await resolve_alert(conn, code=UPDATE_FAILED, subject=UPDATE_FAILED_SUBJECT)
            await conn.execute(_SYNCED, params)
        log.info(
            "deadman_synced",
            interval_s=interval_s,
            timeout_s=check.timeout_s,
            grace_s=check.grace_s,
            paused=check.paused,
            recovered=recovered,
        )

    async def _sync_failed(self, code: str) -> None:
        now = self._clock()
        async with self._engine.begin() as conn:
            await self._fence(conn)
            alert_id = await raise_alert(
                conn,
                code=UPDATE_FAILED,
                severity=CATALOGUE[UPDATE_FAILED].severity,
                subject=UPDATE_FAILED_SUBJECT,
                message="the dead-man check's period could not be applied",
                context={"error_code": code},
            )
            occurrences = int((await conn.execute(_OCCURRENCES, {"id": alert_id})).scalar_one())
            delay = _backoff(occurrences)
            await conn.execute(_SYNC_FAILED, {"code": code, "next_try_at": now + delay})
        log.warning("deadman_sync_failed", error_code=code, retry_in_s=int(delay.total_seconds()))


@reconciler("deadman_ping", "tick")
async def deadman_ping(ctx: ReconcileContext) -> int:
    """The tick's dead-man (module docstring): every run of `runs_to_ping` above the watermark pinged, then
    `deadman_sync` deferred when the check's period is due a sync; the pings plus the job deferred. Nothing
    without a pinger (Q8). Fenced by `ctx.epoch` like every reconciler write."""
    pinger = ctx.deadman
    if pinger is None:
        return 0
    pinged = await pinger.ping_pending()
    async with ctx.engine.begin() as conn:
        await fence_epoch(conn, ctx.epoch)
        if not _sync_due((await conn.execute(_SYNC_STATE)).one(), ctx.now):
            return pinged
        job = await ctx.defer(conn, SYNC_TASK, queueing_lock=SYNC_TASK, timestamp=int(ctx.now.timestamp()))
    log.info("deadman_sync_deferred", deduplicated=job is None)
    return pinged + int(job is not None)
