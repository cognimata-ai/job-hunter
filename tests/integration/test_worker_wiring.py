"""Task 24: the worker's source wiring (carry 3: missing secrets fail closed per instance).

At its start (step 8) the worker runs `sync_instances(conn, cfg, guard=...)` under its epoch fence: a
source whose provider secret is absent is `disabled('missing_secret')` and never planned; fake mode needs no
secret. A run of a worker without the JSearch key therefore plans no JSearch spec and ends for its other
sources, never `failed`.
"""

import uuid
from dataclasses import replace

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db.guards import Guard
from jobhunter.embeddings import EmbedderClient, make_http_client
from jobhunter.errors import FencedOut
from jobhunter.sources import registry
from jobhunter.sources.breaker import record_failure
from jobhunter.sources.instances import InstanceRow, disable, sync_instances
from jobhunter.worker.epoch import WorkerIdentity, claim_epoch_and_sweep, fence_epoch
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


async def unfenced(conn) -> None:
    """The guard of a sync that no worker epoch owns (these tests' own syncs)."""


def fenced_by(epoch: int) -> Guard:
    """The worker's guard of its startup sync: its epoch fence (`epoch.fence_epoch`)."""

    async def guard(conn) -> None:
        await fence_epoch(conn, epoch)

    return guard


async def sync(engine, cfg, guard: Guard = unfenced) -> dict[str, str]:
    async with engine.begin() as c:
        return dict(await sync_instances(c, cfg, guard=guard))


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


# --- Final review C2: the startup sync is fenced by the worker epoch; a paid adapter never sends without
# its credential ---------------------------------------------------------------------------------------------


async def test_a_superseded_workers_startup_sync_is_refused_and_leaves_the_newer_owners_state(
    engine, current_epoch, worker_lock, make_config
) -> None:
    """Worker A (epoch 1) has its secrets and pauses before its startup sync; worker B takes over (epoch 2)
    without the JSearch key and disables JSearch. A's sync, fenced by its own epoch, is refused before it
    reads or writes an instance: it cannot restore `ok` from its stale view, so B never plans JSearch."""
    stale = current_epoch
    newer, _ = await claim_epoch_and_sweep(
        engine, WorkerIdentity(uuid.uuid4(), "h", 2, worker_lock.backend_pid, "v")
    )
    without_key = live_worker(make_config, {k: v for k, v in PROVIDER_SECRETS.items() if k != "rapidapi_key"})
    assert (await sync(engine, without_key, fenced_by(newer)))["jsearch"] == "disabled"
    left = await state(engine, "jsearch")
    assert left == ("disabled", "missing_secret", None, None)
    with pytest.raises(FencedOut) as fenced:
        await sync(engine, live_worker(make_config, PROVIDER_SECRETS), fenced_by(stale))
    assert fenced.value.owner == "worker_epoch"
    assert await state(engine, "jsearch") == left  # as the newer owner left it
    # The current epoch's own sync still restores the instance once its secret is there.
    assert (await sync(engine, live_worker(make_config, PROVIDER_SECRETS), fenced_by(newer)))[
        "jsearch"
    ] == "ok"


async def test_a_paid_instance_left_ok_without_its_key_sends_nothing_and_fails_as_auth(
    engine, current_epoch, make_config, fake_http, fixture_log, fake_embedder
) -> None:
    """Defence in depth: should a JSearch instance be `ok` while the worker has no key (the stale restore
    above, before its fence), the adapter refuses locally: no request reaches the transport, the attempt is
    `failed('auth')`, its reservation is refunded `not_billed` (nothing left the process) and the breaker
    opens `auth_failed`, so no later attempt reserves anything."""
    cfg = live_worker(make_config, {k: v for k, v in PROVIDER_SECRETS.items() if k != "rapidapi_key"})
    assert await state(engine, "jsearch") == ("ok", None, None, None)  # never synced: the seed's state
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
    assert not [r for r in served_requests(fixture_log) if r["host"] == "jsearch.p.rapidapi.com"]
    assert [r for r in served_requests(fixture_log) if r["host"] == "www.arbeitnow.com"]  # the others ran
    async with engine.connect() as c:
        status, reason = (
            await c.execute(sa.text("SELECT status, status_reason FROM runs WHERE id = :i"), {"i": run_id})
        ).one()
        attempts = (
            await c.execute(
                sa.text(
                    "SELECT f.status, f.error_code, f.skip_reason, l.state, l.evidence FROM fetch_requests f "
                    "JOIN source_instances i ON i.id = f.source_instance_id "
                    "LEFT JOIN quota_ledger l ON l.id = f.quota_ledger_id "
                    "WHERE f.run_id = :i AND i.source_id = 'jsearch' ORDER BY f.id"
                ),
                {"i": run_id},
            )
        ).all()
    assert status in FINAL_STATUSES and status not in ("failed", "failed_invariant"), (status, reason)
    refused = ("failed", "auth", None, "not_billed", "connect_failed_before_send")
    assert attempts and tuple(attempts[0]) == refused
    assert all(tuple(a) in (refused, ("skipped", None, "breaker_open", None, None)) for a in attempts[1:])
    health, code, opened, _ = await state(engine, "jsearch")
    assert (health, code) == ("auth_failed", "auth") and opened is not None


async def test_a_serpapi_instance_left_ok_without_its_key_is_visible_as_auth_failed(
    engine, current_epoch, make_config, fake_http, fixture_log, fake_embedder
) -> None:
    """Ruling FR-l (the F2 review's Minor 1): SerpApi's first send is the account read, refused locally like
    the search. That refusal is counted by the breaker as an `auth` failure, so the instance is
    `auth_failed` with the critical `source.auth_failed` open, never a silent `window_cap`; no request
    reaches SerpApi or JSearch."""
    without = {k: v for k, v in PROVIDER_SECRETS.items() if k not in ("rapidapi_key", "serpapi_key")}
    cfg = live_worker(make_config, without)
    assert (await state(engine, "serpapi"))[0] == "ok"  # never synced: the seed's state
    clients = registry.make_clients(cfg)

    def adapter_of(instance: InstanceRow):
        return registry.build_adapter(instance, cfg, clients)

    try:
        async with make_http_client() as embedder_http:
            deps = make_deps(
                engine, adapter_of, embedder=EmbedderClient(fake_embedder.base_url, embedder_http)
            )
            run_id = await run_to_the_end(
                engine,
                deps,
                include_paid=True,
                queries=[TEST_QUERY],
                enabled=("jsearch", "serpapi", "arbeitnow", "ats"),
            )
    finally:
        for client in clients.values():
            await client.aclose()
    hosts = {r["host"] for r in served_requests(fixture_log)}
    assert not hosts & {"serpapi.com", "jsearch.p.rapidapi.com"} and "www.arbeitnow.com" in hosts
    health, code, opened, _ = await state(engine, "serpapi")
    assert (health, code) == ("auth_failed", "auth") and opened is not None
    async with engine.connect() as c:
        serpapi = await instance_id(c, "serpapi")
        alert = await c.execute(
            sa.text("SELECT severity FROM alerts WHERE dedup_key = :k AND resolved_at IS NULL"),
            {"k": f"source.auth_failed:instance:{serpapi}"},
        )
        assert alert.scalar_one() == "critical"
        status = (
            await c.execute(sa.text("SELECT status FROM runs WHERE id = :i"), {"i": run_id})
        ).scalar_one()
    assert status in FINAL_STATUSES and status not in ("failed", "failed_invariant")
