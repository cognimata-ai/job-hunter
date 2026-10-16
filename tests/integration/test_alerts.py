import pytest
import sqlalchemy as sa
from jobhunter.observability.alerts import raise_alert, refresh_alert_gauge, resolve_alert
from jobhunter.observability.metrics import REGISTRY

pytestmark = pytest.mark.integration


async def test_dedup_resolve_and_gauge(engine) -> None:
    async with engine.begin() as c:
        a = await raise_alert(
            c, code="run.orphaned_queue", severity="warn", subject="run:5", message="m", context={"run_id": 5}
        )
        assert (
            await raise_alert(c, code="run.orphaned_queue", severity="warn", subject="run:5", message="m")
            == a
        )
        assert (
            await c.execute(sa.text("SELECT occurrences, context FROM alerts WHERE id=:i"), {"i": a})
        ).one() == (
            2,
            {"run_id": 5},
        )
        assert await refresh_alert_gauge(c) == {"info": 0, "warn": 1, "critical": 0}
        assert await resolve_alert(c, code="run.orphaned_queue", subject="run:5") is True
        assert (
            await raise_alert(c, code="run.orphaned_queue", severity="warn", subject="run:5", message="m")
            != a
        )


async def test_gauge_is_set_for_every_severity_and_resolving_nothing_is_false(engine) -> None:
    async with engine.begin() as c:
        await raise_alert(c, code="a.one", severity="critical", subject="x", message="m")
        await raise_alert(c, code="a.two", severity="critical", subject="x", message="m")
        await raise_alert(c, code="a.three", severity="info", subject="x", message="m")
        # The same code with another subject is another alert; the first raise's severity stays on a repeat.
        await raise_alert(c, code="a.one", severity="info", subject="y", message="m")
        await raise_alert(c, code="a.one", severity="info", subject="x", message="other")
        assert await refresh_alert_gauge(c) == {"info": 2, "warn": 0, "critical": 2}
        assert {
            severity: REGISTRY.get_sample_value("jobhunter_alerts_open", {"severity": severity})
            for severity in ("info", "warn", "critical")
        } == {"info": 2, "warn": 0, "critical": 2}
        assert await resolve_alert(c, code="a.one", subject="x") is True
        assert await resolve_alert(c, code="a.one", subject="x") is False
        assert await resolve_alert(c, code="a.unknown", subject="x") is False
        assert await refresh_alert_gauge(c) == {"info": 2, "warn": 0, "critical": 1}
        assert REGISTRY.get_sample_value("jobhunter_alerts_open", {"severity": "critical"}) == 1
