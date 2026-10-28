"""Deduplicated alerts (spec §7.14, §17.6) and the open-alerts gauge.

An alert is identified by `code` and `subject` (`dedup_key = "<code>:<subject>"`). While one is open, raising
it again only counts the occurrence; once it is resolved, the next raise opens a new row. The unique partial
index `alerts_open_dedup` makes that atomic, so concurrent raisers never create two open rows.

`open_alert` opens one only when none is open, and counts no occurrence of an open one: for a state that a
later check finds still true, not a new occurrence of it (a start that finds a key still missing).

`acknowledge_alert` (M3 Task 13) stamps `acknowledged_at` on an open alert, once: the alert stays open, and an
acknowledgement ends the score lane's pause on an unacknowledged `llm.reservation_bound_violated` or
`llm.auth_failed` (`scoring.cycles.pause_reason`, Deviation 19).

`resolve_quiet_alerts` (M3 Task 18) resolves the open alerts of a code whose last occurrence is older than
a given time: the quiet period after which a condition counts as cleared. `ever_raised` tells whether an alert
was raised for a subject at all, open or resolved: an alert raised at most once per subject.

All eight functions run on the caller's connection and leave the transaction to the caller: an alert is
written in the transaction that decides to raise it, and persists only if that transaction commits.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Final, Literal

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q
from jobhunter.observability.metrics import ALERTS_OPEN
from jobhunter.types import JSONValue

Severity = Literal["info", "warn", "critical"]
SEVERITIES: Final[tuple[Severity, ...]] = ("info", "warn", "critical")

# The conflict target repeats the index `alerts_open_dedup` of §7.14 exactly:
# `(dedup_key) WHERE resolved_at IS NULL`.
_RAISE = q(
    "INSERT INTO alerts (code, severity, dedup_key, message, context) "
    "VALUES (:code, :severity, :dedup_key, :message, :context) "
    "ON CONFLICT (dedup_key) WHERE resolved_at IS NULL "
    "DO UPDATE SET occurrences = alerts.occurrences + 1, last_at = now() RETURNING id",
    jsonb=["context"],
)
_OPEN = q(
    "INSERT INTO alerts (code, severity, dedup_key, message, context) "
    "VALUES (:code, :severity, :dedup_key, :message, :context) "
    "ON CONFLICT (dedup_key) WHERE resolved_at IS NULL DO NOTHING RETURNING id",
    jsonb=["context"],
)
_RESOLVE = q(
    "UPDATE alerts SET resolved_at = now() WHERE dedup_key = :dedup_key AND resolved_at IS NULL RETURNING id"
)
_RESOLVE_CODE = q(
    "UPDATE alerts SET resolved_at = now() WHERE code = :code AND resolved_at IS NULL RETURNING id"
)
# The open alerts of one code whose subject orders before a given one: every key of one code shares the
# `<code>:` prefix, so its keys order as its subjects do, byte by byte (`COLLATE "C"`).
_RESOLVE_CODE_BEFORE = q(
    "UPDATE alerts SET resolved_at = now() WHERE code = :code AND resolved_at IS NULL "
    'AND dedup_key COLLATE "C" < :before_key RETURNING id'
)
# The open alerts of one code whose last occurrence is older than a given time.
_RESOLVE_QUIET = q(
    "UPDATE alerts SET resolved_at = now() WHERE code = :code AND resolved_at IS NULL AND last_at < :since "
    "RETURNING id"
)
_EVER_RAISED = q("SELECT EXISTS (SELECT 1 FROM alerts WHERE dedup_key = :dedup_key)")
# The first acknowledgement's time is kept: a repeated one changes nothing.
_ACKNOWLEDGE = q(
    "UPDATE alerts SET acknowledged_at = coalesce(acknowledged_at, :now) "
    "WHERE id = :id AND resolved_at IS NULL RETURNING id"
)
_OPEN_BY_SEVERITY = q(
    "SELECT severity, count(*) AS n FROM alerts WHERE resolved_at IS NULL GROUP BY severity"
)


def _dedup_key(code: str, subject: str) -> str:
    return f"{code}:{subject}"


async def raise_alert(
    conn: AsyncConnection,
    *,
    code: str,
    severity: Severity,
    subject: str,
    message: str,
    context: Mapping[str, JSONValue] | None = None,
) -> int:
    """Open the alert `code`/`subject`, or count one more occurrence of the open one; returns its id.

    The `severity`, `message` and `context` of the first raise stay: a repeat changes only `occurrences` and
    `last_at`. `message` and `context` carry no PII (ids and counts only).
    """
    result = await conn.execute(
        _RAISE,
        {
            "code": code,
            "severity": severity,
            "dedup_key": _dedup_key(code, subject),
            "message": message,
            "context": {} if context is None else dict(context),
        },
    )
    return int(result.scalar_one())


async def open_alert(
    conn: AsyncConnection,
    *,
    code: str,
    severity: Severity,
    subject: str,
    message: str,
    context: Mapping[str, JSONValue] | None = None,
) -> int | None:
    """Open the alert `code`/`subject` unless it is open; returns the new alert's id, `None` when one was
    open already (left as it is: no occurrence counted, `last_at` unchanged). Atomic, as `raise_alert` is."""
    result = await conn.execute(
        _OPEN,
        {
            "code": code,
            "severity": severity,
            "dedup_key": _dedup_key(code, subject),
            "message": message,
            "context": {} if context is None else dict(context),
        },
    )
    opened = result.scalar()
    return None if opened is None else int(opened)


async def resolve_alert(conn: AsyncConnection, *, code: str, subject: str) -> bool:
    """Resolve the open alert `code`/`subject`; `False` when there was none."""
    result = await conn.execute(_RESOLVE, {"dedup_key": _dedup_key(code, subject)})
    return result.first() is not None


async def resolve_alerts(conn: AsyncConnection, *, code: str, subject_before: str | None = None) -> int:
    """Resolve every open alert `code`, whatever its subject (`run.failed` is raised per run and resolved by
    the next succeeded one); with `subject_before`, only those whose subject orders before it, byte by byte
    (`llm.cap_reached` of the months before the current one, its subject the month's ISO date). The number
    resolved."""
    if subject_before is None:
        return len((await conn.execute(_RESOLVE_CODE, {"code": code})).all())
    params = {"code": code, "before_key": _dedup_key(code, subject_before)}
    return len((await conn.execute(_RESOLVE_CODE_BEFORE, params)).all())


async def resolve_quiet_alerts(conn: AsyncConnection, *, code: str, since: datetime) -> int:
    """Resolve every open alert `code` that has not occurred since `since` (its `last_at`, the time of its
    last raise, is older): an alert that auto-resolves after a quiet period (`scoring.stuck_target_repaired`
    after an hour without reclaims; M3 Task 18). The number resolved."""
    return len((await conn.execute(_RESOLVE_QUIET, {"code": code, "since": since})).all())


async def ever_raised(conn: AsyncConnection, *, code: str, subject: str) -> bool:
    """Whether the alert `code`/`subject` was ever raised, open or resolved: an alert raised at most once per
    subject (`admission.backlog_drain`, at most once a day, its subject the UTC date; M3 Task 18)."""
    return bool((await conn.execute(_EVER_RAISED, {"dedup_key": _dedup_key(code, subject)})).scalar_one())


async def acknowledge_alert(conn: AsyncConnection, alert_id: int, *, now: datetime) -> bool:
    """Acknowledge the open alert `alert_id` at `now` (module docstring); `False` when no open alert has that
    id (it does not exist or is resolved). An alert acknowledged before keeps its first `acknowledged_at`."""
    return (await conn.execute(_ACKNOWLEDGE, {"id": alert_id, "now": now})).first() is not None


async def refresh_alert_gauge(conn: AsyncConnection) -> dict[Severity, int]:
    """Set `ALERTS_OPEN` to the number of open alerts of each severity (0 for none) and return the counts."""
    counts = {row.severity: int(row.n) for row in await conn.execute(_OPEN_BY_SEVERITY)}
    open_alerts: dict[Severity, int] = {}
    for severity in SEVERITIES:
        open_alerts[severity] = counts.get(severity, 0)
        ALERTS_OPEN.labels(severity=severity).set(open_alerts[severity])
    return open_alerts
