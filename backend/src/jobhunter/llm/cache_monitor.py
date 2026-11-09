"""The prompt-cache monitor (spec §10.8; plan Task 5): in-process, fed by the gateway after every completed
call.

- **The zero-read streak.** The key is `(model_served, effort, purpose, prefix_sha256)`. A completed call that
  read nothing from the cache (`cache_read_input_tokens = 0`) while another completed call of the same key
  started less than 240 s before it increments the key's `zero_read_streak`; any read above 0 resets it. A
  streak of 3 or more raises `llm.cache_miss_streak` (warn) on every further miss: so the first call of a
  prefix (it writes the entry) and then three misses in a row raise it. Its context names the model, effort
  and role and the two most recent prefix hashes of that model, effort and role, never content. A read on a
  key whose alert may be open (a streak of 3 or more, or a key this process has not seen) resolves it.
- **The ratio.** `jobhunter_llm_cache_read_ratio{model,effort,role}` is `cache_read / (input + cache_read +
  cache_write)` over the calls of the role that started within the hour before its newest one.

A call without a cached prefix (`prefix_sha256` None, every M2 call: §10.6 "single calls with no cache
marker") counts towards the ratio but never streaks. The monitor writes nothing itself: the gateway calls
`observe` once the call's settlement has committed (plan F32: no gauge or streak moves for a rolled-back
settlement), drains the alerts it queued (`drain`, with no await in between) and writes them in a
transaction of their own (`write_alerts`); a failed write gives them back (`requeue`) for the next call. A key
unseen for an hour is forgotten.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.observability.alerts import raise_alert, resolve_alert
from jobhunter.observability.catalogue import Severity
from jobhunter.observability.metrics import LLM_CACHE_READ_RATIO
from jobhunter.types import JSONValue

STREAK_ALERT: Final = "llm.cache_miss_streak"
STREAK_WINDOW: Final = timedelta(seconds=240)
STREAK_THRESHOLD: Final = 3
RATIO_WINDOW: Final = timedelta(hours=1)
_KEY_IDLE: Final = timedelta(hours=1)
_RECENT_PREFIXES: Final = 2

_Role = tuple[str, str, str]  # (model_served, effort, purpose)
_Key = tuple[str, str, str, bytes]


@dataclass(frozen=True)
class CacheAlert:
    """An alert write the monitor asks for: open (or count) the alert, or `resolve` it."""

    code: str
    severity: Severity
    subject: str
    message: str
    context: Mapping[str, JSONValue]
    resolve: bool = False


@dataclass
class _KeyState:
    last_started_at: datetime
    streak: int = 0


@dataclass(frozen=True)
class _Sample:
    started_at: datetime
    input_tokens: int
    cache_read: int
    cache_write: int


@dataclass
class _RoleState:
    prefixes: list[bytes] = field(default_factory=list)  # most recent first, distinct
    samples: list[_Sample] = field(default_factory=list)


def _subject(key: _Key) -> str:
    model, effort, purpose, prefix = key
    return f"{model}/{effort}/{purpose}/{prefix.hex()}"


class CacheMonitor:
    """The §10.8 state of one process (module docstring)."""

    def __init__(self) -> None:
        self._keys: dict[_Key, _KeyState] = {}
        self._roles: dict[_Role, _RoleState] = {}
        self._pending: list[CacheAlert] = []

    def observe(
        self,
        model_served: str,
        effort: str,
        purpose: str,
        prefix_sha256: bytes | None,
        started_at: datetime,
        cache_read_tokens: int,
        *,
        input_tokens: int,
        cache_creation_tokens: int,
    ) -> None:
        """Record one completed call: its role's ratio, and the streak of its key (with a cached prefix)."""
        role: _Role = (model_served, effort, purpose)
        state = self._roles.setdefault(role, _RoleState())
        self._sample(role, state, _Sample(started_at, input_tokens, cache_read_tokens, cache_creation_tokens))
        if prefix_sha256 is None:
            return
        if prefix_sha256 in state.prefixes:
            state.prefixes.remove(prefix_sha256)
        state.prefixes.insert(0, prefix_sha256)
        del state.prefixes[_RECENT_PREFIXES:]
        self._forget_idle(started_at)
        key: _Key = (*role, prefix_sha256)
        known = self._keys.get(key)
        if cache_read_tokens > 0:
            if known is None or known.streak >= STREAK_THRESHOLD:
                self._pending.append(self._alert(key, 0, state, resolve=True))
            last = started_at if known is None else max(known.last_started_at, started_at)
            self._keys[key] = _KeyState(last)
            return
        if known is None:
            self._keys[key] = _KeyState(started_at)
            return
        if timedelta(0) <= started_at - known.last_started_at < STREAK_WINDOW:
            known.streak += 1
            if known.streak >= STREAK_THRESHOLD:
                self._pending.append(self._alert(key, known.streak, state))
        known.last_started_at = max(known.last_started_at, started_at)

    def drain(self) -> list[CacheAlert]:
        """The alert writes queued since the last drain, oldest first."""
        alerts, self._pending = self._pending, []
        return alerts

    def requeue(self, alerts: Sequence[CacheAlert]) -> None:
        """Give back drained alerts that could not be written, ahead of any queued since."""
        self._pending[:0] = alerts

    def streak(self, model_served: str, effort: str, purpose: str, prefix_sha256: bytes) -> int:
        """The key's current `zero_read_streak` (0 for a key not seen)."""
        known = self._keys.get((model_served, effort, purpose, prefix_sha256))
        return 0 if known is None else known.streak

    def ratio(self, model_served: str, effort: str, purpose: str) -> float:
        """The role's cache read ratio over its rolling hour (0 without prompt tokens)."""
        state = self._roles.get((model_served, effort, purpose))
        return 0.0 if state is None else _ratio(state.samples)

    def _sample(self, role: _Role, state: _RoleState, sample: _Sample) -> None:
        newest = max([sample.started_at, *(s.started_at for s in state.samples)])
        state.samples = [s for s in (*state.samples, sample) if s.started_at > newest - RATIO_WINDOW]
        model, effort, purpose = role
        LLM_CACHE_READ_RATIO.labels(model=model, effort=effort, role=purpose).set(_ratio(state.samples))

    def _forget_idle(self, now: datetime) -> None:
        for key in [k for k, s in self._keys.items() if s.last_started_at < now - _KEY_IDLE]:
            del self._keys[key]

    @staticmethod
    def _alert(key: _Key, streak: int, state: _RoleState, *, resolve: bool = False) -> CacheAlert:
        model, effort, purpose, _ = key
        context: dict[str, JSONValue] = {
            "model": model,
            "effort": effort,
            "role": purpose,
            "zero_read_streak": streak,
            "prefix_sha256": [prefix.hex() for prefix in state.prefixes],
        }
        return CacheAlert(
            code=STREAK_ALERT,
            severity="warn",
            subject=_subject(key),
            message="prompt-cache reads stayed at zero for consecutive calls of one cached prefix",
            context=context,
            resolve=resolve,
        )


def _ratio(samples: Sequence[_Sample]) -> float:
    read = sum(s.cache_read for s in samples)
    total = sum(s.input_tokens + s.cache_read + s.cache_write for s in samples)
    return read / total if total > 0 else 0.0


async def write_alerts(conn: AsyncConnection, alerts: Sequence[CacheAlert]) -> None:
    """Persist `alerts` in the caller's transaction: open or count each, or resolve it."""
    for alert in alerts:
        if alert.resolve:
            await resolve_alert(conn, code=alert.code, subject=alert.subject)
        else:
            await raise_alert(
                conn,
                code=alert.code,
                severity=alert.severity,
                subject=alert.subject,
                message=alert.message,
                context=alert.context,
            )
