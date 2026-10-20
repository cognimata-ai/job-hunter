# tests/integration/test_breaker.py
import base64
import os
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.config import AppConfig, load_config
from jobhunter.sources.breaker import HALF_OPEN_AFTER, available, record_failure, record_success
from jobhunter.sources.instances import (
    InstanceRow,
    disable,
    instance_for_company,
    list_instances,
    sync_instances,
)
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

pytestmark = pytest.mark.integration


async def instance(engine, source_id: str, instance_key: str = "default") -> InstanceRow:
    async with engine.connect() as c:
        return next(
            i for i in await list_instances(c) if (i.source_id, i.instance_key) == (source_id, instance_key)
        )


async def open_alerts(engine) -> set[str]:
    async with engine.connect() as c:
        return set((await c.execute(sa.text("SELECT code FROM alerts WHERE resolved_at IS NULL"))).scalars())


async def test_thresholds_half_open_and_alerts(engine) -> None:
    now, js, an = utcnow(), await instance(engine, "jsearch"), await instance(engine, "arbeitnow")
    async with engine.begin() as c:
        assert await record_failure(c, js.id, "transient", billing="paid", now=now) == "degraded"
        assert await record_failure(c, js.id, "transient", billing="paid", now=now) == "breaker_open"
        assert [await record_failure(c, an.id, "transient", billing="free", now=now) for _ in range(5)] == [
            "degraded"
        ] * 4 + ["breaker_open"]
    js = await instance(engine, "jsearch")
    assert js.half_open_after == now + HALF_OPEN_AFTER and available(js, now) == "blocked"
    assert available(js, now + HALF_OPEN_AFTER) == "half_open"
    assert await open_alerts(engine) == {"source.breaker_open"}
    async with engine.begin() as c:
        assert await record_failure(c, js.id, "auth", billing="paid", now=now) == "auth_failed"
        assert await record_failure(c, an.id, "contract", billing="free", now=now) == "contract_broken"
    assert await open_alerts(engine) == {
        "source.breaker_open",
        "source.auth_failed",
        "source.contract_broken",
    }
    async with engine.begin() as c:
        assert await record_success(c, js.id, now=now + timedelta(hours=7)) == "ok"
        assert await record_success(c, an.id, now=now) == "ok"
    assert await open_alerts(engine) == set() and (await instance(engine, "jsearch")).half_open_after is None


async def test_breaker_timestamps_clear_on_ok_and_disabled(engine) -> None:  # ruling (b) on Deviation 2
    now, js, az = utcnow(), await instance(engine, "jsearch"), await instance(engine, "adzuna")
    async with engine.begin() as c:
        await record_failure(c, js.id, "auth", billing="paid", now=now)
        await record_failure(c, az.id, "contract", billing="quota", now=now)
    async with engine.begin() as c:
        assert await record_success(c, js.id, now=now + timedelta(hours=7)) == "ok"
        await disable(c, az.id, code="missing_secret")
    async with engine.connect() as c:
        rows = (
            await c.execute(
                sa.text(
                    "SELECT source_id, health, breaker_opened_at, half_open_after, consecutive_failures "
                    "FROM source_instances WHERE id IN (:a, :b) ORDER BY source_id"
                ),
                {"a": js.id, "b": az.id},
            )
        ).all()
    assert [tuple(r) for r in rows] == [
        ("adzuna", "disabled", None, None, 0),
        ("jsearch", "ok", None, None, 0),
    ]
    # fix wave F4 (ledger L62): disabling the open instance resolves its alert, as a success does (a missing
    # secret keeps only `source.auth_failed` open, below; this one is `source.contract_broken`)
    assert await open_alerts(engine) == set()


INSTANCE_STATE = sa.text(
    "SELECT health, consecutive_failures, breaker_opened_at, half_open_after, last_success_at, "
    "last_error_code FROM source_instances WHERE id = :i"
)


async def state(engine, instance_id: int) -> tuple[object, ...]:
    async with engine.connect() as c:
        return tuple((await c.execute(INSTANCE_STATE, {"i": instance_id})).one())


async def test_quota_sources_open_after_two_and_open_states_open_at_once(engine) -> None:
    now, az, sp = utcnow(), await instance(engine, "adzuna"), await instance(engine, "serpapi")
    async with engine.begin() as c:
        assert [
            await record_failure(c, az.id, "rate_limited", billing="quota", now=now) for _ in range(2)
        ] == [
            "degraded",
            "breaker_open",
        ]
        assert await record_failure(c, sp.id, "not_found", billing="paid", now=now) == "not_found"
    assert await state(engine, az.id) == ("breaker_open", 2, now, now + HALF_OPEN_AFTER, None, "rate_limited")
    assert await state(engine, sp.id) == ("not_found", 1, now, now + HALF_OPEN_AFTER, None, "not_found")
    assert await open_alerts(engine) == {"source.breaker_open", "source.not_found"}
    async with engine.connect() as c:
        severities = dict(
            (await c.execute(sa.text("SELECT code, severity FROM alerts WHERE resolved_at IS NULL"))).all()
        )
    assert severities == {"source.breaker_open": "warn", "source.not_found": "warn"}


async def test_a_failed_probe_reopens_the_same_state_for_another_six_hours(engine) -> None:
    now, an = utcnow(), await instance(engine, "arbeitnow")
    later = now + HALF_OPEN_AFTER
    async with engine.begin() as c:
        assert await record_failure(c, an.id, "auth", billing="free", now=now) == "auth_failed"
        # The probe after the half-open time fails transiently: below the free threshold, still open.
        assert await record_failure(c, an.id, "transient", billing="free", now=later) == "auth_failed"
    assert await state(engine, an.id) == ("auth_failed", 2, later, later + HALF_OPEN_AFTER, None, "transient")
    async with engine.begin() as c:
        assert await record_failure(c, an.id, "client", billing="free", now=later) == "auth_failed"
        assert await record_success(c, an.id, now=later) == "ok"
    assert await state(engine, an.id) == ("ok", 0, None, None, later, None)
    async with engine.connect() as c:
        (occurrences,) = (
            await c.execute(sa.text("SELECT occurrences FROM alerts WHERE code = 'source.auth_failed'"))
        ).one()
    assert occurrences == 3  # every failure in the open state counts as an occurrence of its alert


async def test_a_disabled_instance_is_left_disabled_by_the_breaker(engine) -> None:
    now, js = utcnow(), await instance(engine, "jsearch")
    async with engine.begin() as c:
        await disable(c, js.id, code="missing_secret")
        assert await record_failure(c, js.id, "auth", billing="paid", now=now) == "disabled"
        assert await record_success(c, js.id, now=now) == "disabled"
    assert await state(engine, js.id) == ("disabled", 0, None, None, None, "missing_secret")
    assert await open_alerts(engine) == set()
    assert available(await instance(engine, "jsearch"), now) == "blocked"


async def test_available_by_state(engine) -> None:
    now, js = utcnow(), await instance(engine, "jsearch")
    assert available(js, now) == "available"
    async with engine.begin() as c:
        await record_failure(c, js.id, "transient", billing="paid", now=now)
    assert (await instance(engine, "jsearch")).health == "degraded"
    assert available(await instance(engine, "jsearch"), now) == "available"
    async with engine.begin() as c:
        await record_failure(c, js.id, "contract", billing="paid", now=now)
    js = await instance(engine, "jsearch")
    assert [available(js, now + d) for d in (timedelta(0), HALF_OPEN_AFTER - timedelta(seconds=1))] == [
        "blocked",
        "blocked",
    ]
    assert available(js, now + HALF_OPEN_AFTER) == "half_open"


# --- instances: the default instances, sync_instances (plan Deviation 6) and watchlist instances ----------

PROVIDER_SECRETS = {
    "rapidapi_key": b"rk-1234567890",
    "serpapi_key": b"sk-1234567890",
    "adzuna_app_id": b"aid-123456",
    "adzuna_app_key": b"akey-1234567",
}


def worker_config(
    tmp_path: Path, name: str, secrets: Mapping[str, bytes] | None = None, *, fake: bool = False
) -> AppConfig:
    directory = tmp_path / name
    directory.mkdir()
    for file_name, value in {
        "data_key": base64.b64encode(os.urandom(32)),
        "db_app": b"pw",
        **(secrets or {}),
    }.items():
        (directory / file_name).write_bytes(value)
        (directory / file_name).chmod(0o440)
    env = {"JOBHUNTER_ENV": "test", "JOBHUNTER_DB_NAME": "jobhunter"}
    if fake:
        env["JOBHUNTER_FAKE_SOURCES"] = "1"
    return load_config("worker", env, directory)


async def unfenced(conn: AsyncConnection) -> None:
    """The guard of a sync that no worker epoch owns (the worker passes its epoch fence)."""


async def sync(engine: AsyncEngine, cfg: AppConfig) -> dict[str, str]:
    async with engine.begin() as c:
        return dict(await sync_instances(c, cfg, guard=unfenced))


async def test_a_missing_provider_secret_disables_and_only_that_disable_is_restored(engine, tmp_path) -> None:
    without_rapidapi = {k: v for k, v in PROVIDER_SECRETS.items() if k != "rapidapi_key"}
    without_adzuna_key = {k: v for k, v in PROVIDER_SECRETS.items() if k != "adzuna_app_key"}
    # An open breaker without its secret is disabled as well: no request could be sent without the key.
    now, js = utcnow(), await instance(engine, "jsearch")
    async with engine.begin() as c:
        await record_failure(c, js.id, "auth", billing="paid", now=now)
    assert await sync(engine, worker_config(tmp_path, "a", without_rapidapi)) == {
        "jsearch": "disabled",
        "serpapi": "ok",
        "adzuna": "ok",
        "arbeitnow": "ok",
    }
    assert await state(engine, js.id) == ("disabled", 0, None, None, None, "missing_secret")
    # F4 fix round 1 (Minor 3): the missing key stays visible: `source.auth_failed` stays open (§17.6 resolves
    # it on a success only, Ruling FR-l), though the disable cleared the breaker
    assert await open_alerts(engine) == {"source.auth_failed"}
    assert (await sync(engine, worker_config(tmp_path, "b", without_adzuna_key)))["adzuna"] == "disabled"
    assert await sync(engine, worker_config(tmp_path, "c", PROVIDER_SECRETS)) == dict.fromkeys(
        ("jsearch", "serpapi", "adzuna", "arbeitnow"), "ok"
    )
    assert await state(engine, js.id) == ("ok", 0, None, None, None, None)
    assert await open_alerts(engine) == {"source.auth_failed"}  # restored, not yet used: still open
    async with engine.begin() as c:
        await record_success(c, js.id, now=utcnow())  # the restored key's first success resolves it
    assert await open_alerts(engine) == set()


async def test_sync_leaves_administrative_disables_and_breaker_states_untouched(engine, tmp_path) -> None:
    now, js, sp = utcnow(), await instance(engine, "jsearch"), await instance(engine, "serpapi")
    async with engine.begin() as c:
        await disable(c, js.id, code="by_user")
        await record_failure(c, sp.id, "transient", billing="paid", now=now)
        await record_failure(c, sp.id, "transient", billing="paid", now=now)
    opened = await state(engine, sp.id)
    assert (await sync(engine, worker_config(tmp_path, "a", PROVIDER_SECRETS)))["jsearch"] == "disabled"
    assert await state(engine, js.id) == ("disabled", 0, None, None, None, "by_user")
    assert await state(engine, sp.id) == opened and opened[0] == "breaker_open"
    # Without any secret, the administrative disable keeps its code (a restored secret must not enable it).
    assert (await sync(engine, worker_config(tmp_path, "b")))["jsearch"] == "disabled"
    assert await state(engine, js.id) == ("disabled", 0, None, None, None, "by_user")


async def test_fake_mode_needs_no_secret_and_restores_a_missing_secret_disable(engine, tmp_path) -> None:
    assert (await sync(engine, worker_config(tmp_path, "a")))["serpapi"] == "disabled"
    assert await sync(engine, worker_config(tmp_path, "b", fake=True)) == dict.fromkeys(
        ("jsearch", "serpapi", "adzuna", "arbeitnow"), "ok"
    )


WATCHLIST = sa.text(
    "INSERT INTO watchlist_companies "
    "(display_name, company_key, ats_type, board_slug, lever_region, detection) "
    "VALUES (:name, :name, :ats, :slug, :region, 'manual') RETURNING id"
)


async def add_company(c, name: str, ats: str, slug: str, region: str | None = None) -> int:
    params = {"name": name, "ats": ats, "slug": slug, "region": region}
    return int((await c.execute(WATCHLIST, params)).scalar_one())


async def test_instance_for_company_keys_by_slug_and_lever_region(engine) -> None:
    async with engine.begin() as c:
        gh = await add_company(c, "GetYourGuide", "greenhouse", "getyourguide")
        lv = await add_company(c, "Lever Demo", "lever", "leverdemo", "eu")
        gh_instance = await instance_for_company(c, gh)
        assert await instance_for_company(c, gh) == gh_instance  # the existing row, no second one
        lv_instance = await instance_for_company(c, lv)
    greenhouse, lever = (
        await instance(engine, "greenhouse", "getyourguide"),
        await instance(engine, "lever", "eu:leverdemo"),
    )
    assert (
        greenhouse.id,
        greenhouse.watchlist_company_id,
        greenhouse.board_slug,
        greenhouse.display_name,
    ) == (
        gh_instance,
        gh,
        "getyourguide",
        "GetYourGuide",
    )
    assert (lever.id, lever.watchlist_company_id, lever.lever_region, lever.health) == (
        lv_instance,
        lv,
        "eu",
        "ok",
    )
    assert (await instance(engine, "jsearch")).watchlist_company_id is None
    # A company removed and added again finds its board's instance and links it once more.
    async with engine.begin() as c:
        await c.execute(sa.text("DELETE FROM watchlist_companies WHERE id = :i"), {"i": gh})
        again = await add_company(c, "GetYourGuide", "greenhouse", "getyourguide")
        assert await instance_for_company(c, again) == gh_instance
    assert (await instance(engine, "greenhouse", "getyourguide")).watchlist_company_id == again
    async with engine.begin() as c:
        with pytest.raises(LookupError):
            await instance_for_company(c, again + 1000)


async def test_an_administrative_disable_resolves_every_open_state_alert(engine) -> None:
    """F4 fix round 1 (Minor 3): only a missing secret keeps `source.auth_failed` open; any other disable
    (the user's) resolves every open-state alert of the instance, as §17.6's "removal" does."""
    now, js = utcnow(), await instance(engine, "jsearch")
    async with engine.begin() as c:
        await record_failure(c, js.id, "auth", billing="paid", now=now)
        await disable(c, js.id, code="by_user")
    assert await open_alerts(engine) == set()
