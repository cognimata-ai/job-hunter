"""Task 24: the worker's source wiring (carry 3: missing secrets fail closed per instance).

At its start (step 8) the worker runs `sync_instances(conn, cfg)`: a source whose provider secret is absent is
`disabled('missing_secret')` and never planned; fake mode needs no secret. A run of a worker without the
JSearch key therefore plans no JSearch spec and ends for its other sources, never `failed`.
"""

from dataclasses import replace

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.embeddings import EmbedderClient, make_http_client
from jobhunter.sources import registry
from jobhunter.sources.breaker import record_failure
from jobhunter.sources.instances import InstanceRow, disable, sync_instances
from jobhunter.worker.runs import FINAL_STATUSES

from tests.support.collect import TEST_QUERY, instance_id, make_deps, run_to_the_end
from tests.support.sources import served_requests

pytestmark = pytest.mark.integration

PROVIDER_SECRETS = {
    "rapidapi_key": b"rk-test-0001",
    "serpapi_key": b"sk-test-0002",
    "adzuna_app_id": b"aid-test-03",
    "adzuna_app_key": b"akey-test-04",
}
STATE = sa.text(
    "SELECT health, last_error_code, breaker_opened_at, half_open_after FROM source_instances "
    "WHERE source_id = :s AND instance_key = 'default'"
)


async def state(engine, source_id: str) -> tuple[object, ...]:
    async with engine.connect() as c:
        return tuple((await c.execute(STATE, {"s": source_id})).one())


async def sync(engine, cfg) -> dict[str, str]:
    async with engine.begin() as c:
        return dict(await sync_instances(c, cfg))


def live_worker(make_config, secrets: dict[str, bytes]):
    """The test worker's configuration out of fake mode, with `secrets` as its provider secrets only."""
    cfg = make_config("worker")
    kept = {name: value for name, value in cfg.secrets.items() if name not in PROVIDER_SECRETS}
    return replace(cfg, fake_sources=False, secrets={**kept, **secrets})


async def test_sync_instances_disables_sources_without_a_secret_and_fake_mode_needs_none(
    engine, make_config
) -> None:
    without = live_worker(make_config, {})
    assert await sync(engine, without) == {
        "jsearch": "disabled",
        "serpapi": "disabled",
        "adzuna": "disabled",
        "arbeitnow": "ok",
    }
    assert await state(engine, "jsearch") == ("disabled", "missing_secret", None, None)
    assert await state(engine, "adzuna") == ("disabled", "missing_secret", None, None)
    # With the secrets present again, every missing-secret disable is restored.
    assert await sync(engine, live_worker(make_config, PROVIDER_SECRETS)) == dict.fromkeys(
        ("jsearch", "serpapi", "adzuna", "arbeitnow"), "ok"
    )
    # Fake mode (the test worker's own configuration) needs no secret.
    await sync(engine, without)
    assert make_config("worker").fake_sources and not set(PROVIDER_SECRETS) & set(
        make_config("worker").secrets
    )
    assert await sync(engine, make_config("worker")) == dict.fromkeys(
        ("jsearch", "serpapi", "adzuna", "arbeitnow"), "ok"
    )
    # A breaker state is left alone, timestamps included, while the secret is present.
    async with engine.begin() as c:
        jsearch = await instance_id(c, "jsearch")
        for _ in range(2):
            await record_failure(c, jsearch, "transient", billing="paid", now=utcnow())
    opened = await state(engine, "jsearch")
    assert opened[0] == "breaker_open" and opened[2] is not None and opened[3] is not None
    assert (await sync(engine, live_worker(make_config, PROVIDER_SECRETS)))["jsearch"] == "breaker_open"
    assert await state(engine, "jsearch") == opened
    # Ruling (c): restoration only from `missing_secret`; an administrative disable keeps its code.
    async with engine.begin() as c:
        await disable(c, jsearch, code="by_user")
    assert (await sync(engine, live_worker(make_config, PROVIDER_SECRETS)))["jsearch"] == "disabled"
    assert await state(engine, "jsearch") == ("disabled", "by_user", None, None)


async def test_a_worker_without_the_jsearch_key_plans_no_jsearch_spec_and_does_not_fail(
    engine, current_epoch, make_config, fake_http, fixture_log, fake_embedder
) -> None:
    """Carry 3: the start's sync disables JSearch, the run plans no JSearch spec (its traversal is never
    reached, so no missing key can fail it as a bug), and the run ends for its other sources."""
    cfg = live_worker(make_config, {k: v for k, v in PROVIDER_SECRETS.items() if k != "rapidapi_key"})
    assert (await sync(engine, cfg))["jsearch"] == "disabled"
    clients = registry.make_clients(cfg)  # on `fake_http`'s fixture transport: nothing leaves the process

    def adapter_of(instance: InstanceRow):
        return registry.build_adapter(instance, cfg, clients)

    try:
        async with make_http_client() as embedder_http:
            deps = make_deps(
                engine, adapter_of, embedder=EmbedderClient(fake_embedder.base_url, embedder_http)
            )
            run_id = await run_to_the_end(
                engine, deps, include_paid=True, queries=[TEST_QUERY], enabled=("jsearch", "arbeitnow", "ats")
            )
    finally:
        for client in clients.values():
            await client.aclose()
    async with engine.connect() as c:
        status, reason = (
            await c.execute(sa.text("SELECT status, status_reason FROM runs WHERE id = :i"), {"i": run_id})
        ).one()
        planned = (
            await c.execute(
                sa.text(
                    "SELECT i.source_id, count(*) FROM fetch_requests f "
                    "JOIN source_instances i ON i.id = f.source_instance_id WHERE f.run_id = :i "
                    "GROUP BY i.source_id ORDER BY i.source_id"
                ),
                {"i": run_id},
            )
        ).all()
    sources = {row.source_id for row in planned}
    assert "jsearch" not in sources and {"arbeitnow", "greenhouse"} <= sources
    assert status in FINAL_STATUSES and status not in ("failed", "failed_invariant"), (status, reason)
    assert not [r for r in served_requests(fixture_log) if r["host"] == "jsearch.p.rapidapi.com"]
