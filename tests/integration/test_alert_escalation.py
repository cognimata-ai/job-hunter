"""Alert escalation and the acknowledgement that resolves (spec §17.6 L5632, L5662; plan M5 Task 2, O-ops O6,
O7, Q12, Q13).

- `raise_alert` on an open alert with a higher severity (`info < warn < critical`) escalates it: the row takes
  the new severity, message and context, and its `last_notified_at` is cleared, so the dispatcher pushes the
  escalation at once; `occurrences` and `last_at` move as for any repeat. A lower or equal severity never
  changes the row but for those two.
- `acknowledge_alert` resolves `llm.reservation_bound_violated` (§17.6: "manual acknowledgement"): its pause
  ends, and its next occurrence opens a new row, which pauses the score lane again and is pushed again. Any
  other alert stays open when acknowledged.

Every datum is invented.
"""

from datetime import UTC, datetime, timedelta
from typing import Any, Final

import pytest
import sqlalchemy as sa
from jobhunter.llm.gateway import AUTH_FAILED_ALERT
from jobhunter.llm.spend_ledger import BOUND_VIOLATED_ALERT
from jobhunter.observability.alerts import acknowledge_alert, raise_alert
from jobhunter.observability.catalogue import Severity
from jobhunter.scoring import cycles
from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.integration

NOW: Final = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
NOTIFIED: Final = NOW - timedelta(hours=2)


async def raise_(
    engine: AsyncEngine,
    severity: Severity,
    message: str,
    context: dict[str, Any],
    *,
    code: str = "backup.stale",
    subject: str = "backup",
) -> int:
    async with engine.begin() as conn:
        return await raise_alert(
            conn, code=code, severity=severity, subject=subject, message=message, context=context
        )


async def notified(engine: AsyncEngine, alert_id: int) -> None:
    """The dispatcher's stamp on a pushed alert."""
    async with engine.begin() as conn:
        await conn.execute(
            sa.text("UPDATE alerts SET last_notified_at = :t WHERE id = :a"), {"t": NOTIFIED, "a": alert_id}
        )


async def row(engine: AsyncEngine, alert_id: int) -> sa.Row[Any]:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                sa.text(
                    "SELECT severity, message, context, occurrences, last_at, last_notified_at, "
                    "acknowledged_at, resolved_at FROM alerts WHERE id = :a"
                ),
                {"a": alert_id},
            )
        ).one()


async def pause(engine: AsyncEngine) -> str | None:
    async with engine.connect() as conn:
        return await cycles.pause_reason(conn, fake_llm=True, now=NOW)


async def test_a_higher_severity_raise_escalates_and_clears_last_notified(engine: AsyncEngine) -> None:
    warn = await raise_(engine, "warn", "backup older than 36 h", {"age_h": 40})
    await notified(engine, warn)
    before = await row(engine, warn)
    critical = await raise_(engine, "critical", "backup older than 72 h", {"age_h": 80})
    assert critical == warn  # the same open alert
    after = await row(engine, warn)
    assert (after.severity, after.message, after.context) == (
        "critical",
        "backup older than 72 h",
        {"age_h": 80},
    )
    assert after.last_notified_at is None  # pushed again at once
    assert after.occurrences == 2
    assert after.last_at > before.last_at
    assert (after.acknowledged_at, after.resolved_at) == (None, None)


async def test_a_lower_severity_raise_never_deescalates(engine: AsyncEngine) -> None:
    critical = await raise_(engine, "critical", "disk at 90 %", {"used_pct": 91}, code="system.disk_pressure")
    await notified(engine, critical)
    repeats: tuple[Severity, ...] = ("warn", "info", "critical")  # lower, lower, equal
    for n, severity in enumerate(repeats, start=2):
        before = await row(engine, critical)
        repeat = await raise_(
            engine, severity, f"repeat {n}", {"used_pct": 80 + n}, code="system.disk_pressure"
        )
        assert repeat == critical
        after = await row(engine, critical)
        assert (after.severity, after.message, after.context) == (
            "critical",
            "disk at 90 %",
            {"used_pct": 91},
        )
        assert after.last_notified_at == NOTIFIED  # not pushed again
        assert after.occurrences == n
        assert after.last_at > before.last_at


async def test_ack_resolves_a_reservation_bound_violation_and_its_next_occurrence_reopens_and_pauses(
    engine: AsyncEngine, current_epoch: int
) -> None:
    bound = await raise_(
        engine, "critical", "a settled cost exceeded its reservation", {}, code=BOUND_VIOLATED_ALERT
    )
    await notified(engine, bound)
    assert await pause(engine) == "reservation_bound_violated"
    async with engine.begin() as conn:
        assert await acknowledge_alert(conn, bound, now=NOW) == NOW
    acknowledged = await row(engine, bound)
    assert (acknowledged.acknowledged_at, acknowledged.resolved_at) == (NOW, NOW)  # resolved by the ack
    assert await pause(engine) != "reservation_bound_violated"

    again = await raise_(
        engine, "critical", "a settled cost exceeded its reservation", {}, code=BOUND_VIOLATED_ALERT
    )
    assert again != bound  # a new row
    assert await pause(engine) == "reservation_bound_violated"
    reopened = await row(engine, again)
    assert (reopened.last_notified_at, reopened.acknowledged_at, reopened.resolved_at) == (None, None, None)
    assert reopened.occurrences == 1


async def test_ack_keeps_any_other_alert_open(engine: AsyncEngine, current_epoch: int) -> None:
    auth = await raise_(
        engine, "critical", "401 from the provider", {}, code=AUTH_FAILED_ALERT, subject="llm"
    )
    async with engine.begin() as conn:
        assert await acknowledge_alert(conn, auth, now=NOW) == NOW
    acknowledged = await row(engine, auth)
    assert (acknowledged.acknowledged_at, acknowledged.resolved_at) == (NOW, None)
    assert await pause(engine) is None
    assert (await raise_(engine, "critical", "again", {}, code=AUTH_FAILED_ALERT, subject="llm")) == auth
