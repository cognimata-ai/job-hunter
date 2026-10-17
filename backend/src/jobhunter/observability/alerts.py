"""Deduplicated alerts (spec §7.14, §17.6) and the open-alerts gauge.

An alert is identified by `code` and `subject` (`dedup_key = "<code>:<subject>"`). While one is open, raising
it again only counts the occurrence; once it is resolved, the next raise opens a new row. The unique partial
index `alerts_open_dedup` makes that atomic, so concurrent raisers never create two open rows.

All three functions run on the caller's connection and leave the transaction to the caller: an alert is
written in the transaction that decides to raise it, and persists only if that transaction commits.
"""

from collections.abc import Mapping
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
_RESOLVE = q(
    "UPDATE alerts SET resolved_at = now() WHERE dedup_key = :dedup_key AND resolved_at IS NULL RETURNING id"
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


async def resolve_alert(conn: AsyncConnection, *, code: str, subject: str) -> bool:
    """Resolve the open alert `code`/`subject`; `False` when there was none."""
    result = await conn.execute(_RESOLVE, {"dedup_key": _dedup_key(code, subject)})
    return result.first() is not None


async def refresh_alert_gauge(conn: AsyncConnection) -> dict[Severity, int]:
    """Set `ALERTS_OPEN` to the number of open alerts of each severity (0 for none) and return the counts."""
    counts = {row.severity: int(row.n) for row in await conn.execute(_OPEN_BY_SEVERITY)}
    open_alerts: dict[Severity, int] = {}
    for severity in SEVERITIES:
        open_alerts[severity] = counts.get(severity, 0)
        ALERTS_OPEN.labels(severity=severity).set(open_alerts[severity])
    return open_alerts
