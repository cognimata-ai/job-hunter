"""A score cycle on the worker's pools (spec §9.1 L3375, §3.3.4, §9.4.3; plan M3 Task 27, B R12).

A 60-target cycle on the fake LLM, through `execute_cycle` as the `score_cycle` task runs it: three attempts
in flight (`max_concurrency=3`), the claim heartbeats and the cycle heartbeat at their real periods (30 s),
the checkpointer on `open_saver` (a pool of 1 to 4) and the engine at the worker's size (`ENGINE_POOL_SIZE`,
10).
Each message call of the fake waits `CALL_LATENCY_S` first, a stand-in for the provider's time, so the cycle
runs long enough for every claim's beat to come due while the claims queue for the three slots.

A measurement, never a correctness assertion beyond the cycle's own end: the cycle completes, every target
published, without a pool timeout and within 5 minutes. The cycle time, the checkpointer pool's peak and total
wait, its requests and timeouts, the engine pool's peak checkout, its peak wait and timeouts, and the beats
are recorded (`record_property`) and printed. A measured need for larger pools is reported, never patched
here: the pools keep the spec's sizes.
"""

import asyncio
import dataclasses
import time
from collections.abc import Awaitable, Callable
from typing import Any, Final

import httpx2
import procrastinate
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db.session import make_engine
from jobhunter.graphs.checkpointer import open_saver
from jobhunter.graphs.score.graph import MAX_CONCURRENCY, build_score_graphs, execute_cycle
from jobhunter.llm import fake
from jobhunter.llm.cache_monitor import CacheMonitor
from jobhunter.llm.gateway import LLMGateway
from jobhunter.llm.transport import make_http_client
from jobhunter.worker.main import ENGINE_POOL_SIZE, QUEUE_POOL_MAX_SIZE

from tests.support.fixtures import ConfigFactory
from tests.support.llm import FakeLLM
from tests.support.pg import PgServer
from tests.support.scoring import cycle_of, insert_targets, score_deps, synthetic_c_draft, target_of

pytestmark = [pytest.mark.perf, pytest.mark.integration]

TARGETS: Final = 60
LIMIT_S: Final = 300.0
CALL_LATENCY_S: Final = 3.0  # each fake message call's stand-in provider time
CHECKPOINTER_POOL: Final = (1, 4)  # `open_saver` (spec §9.1 L3375)
_CLAIM_BEAT: Final = "UPDATE score_targets SET claim_heartbeat_at = now()"
_CYCLE_BEAT: Final = "UPDATE score_cycles SET heartbeat_at = now()"


class Latent(httpx2.AsyncBaseTransport):
    """The fake's transport, each message request held `seconds` first (the provider's time)."""

    def __init__(self, inner: httpx2.AsyncBaseTransport, seconds: float) -> None:
        self.inner, self.seconds = inner, seconds

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/messages"):
            await asyncio.sleep(self.seconds)
        return await self.inner.handle_async_request(request)


class PoolWaits:
    """Every connection request of the checkpointer's pool, timed from the request to the connection."""

    def __init__(self, pool: Any) -> None:
        self.waits: list[float] = []
        real: Callable[..., Awaitable[Any]] = pool.getconn

        async def timed(*args: Any, **kwargs: Any) -> Any:
            started = time.perf_counter()
            try:
                return await real(*args, **kwargs)
            finally:
                self.waits.append(time.perf_counter() - started)

        pool.getconn = timed


class EngineUse:
    """The engine pool's peak checkout, every wait for one of its connections (its queue's `_do_get`, timed
    from the request to the connection) and its timeouts, and the claim and cycle beats its connections
    ran."""

    def __init__(self, engine: Any) -> None:
        self.out = self.peak = self.claim_beats = self.cycle_beats = self.timeouts = 0
        self.waits: list[float] = []
        sync = engine.sync_engine
        sa.event.listen(sync.pool, "checkout", self._checkout)
        sa.event.listen(sync.pool, "checkin", self._checkin)
        sa.event.listen(sync, "before_cursor_execute", self._statement)
        real: Callable[[], Any] = sync.pool._do_get

        def timed() -> Any:
            started = time.perf_counter()
            try:
                return real()
            except sa.exc.TimeoutError:
                self.timeouts += 1
                raise
            finally:
                self.waits.append(time.perf_counter() - started)

        sync.pool._do_get = timed

    def _checkout(self, *args: object) -> None:
        self.out += 1
        self.peak = max(self.peak, self.out)

    def _checkin(self, *args: object) -> None:
        self.out -= 1

    def _statement(self, conn: object, cursor: object, statement: str, *args: object) -> None:
        self.claim_beats += statement.startswith(_CLAIM_BEAT)
        self.cycle_beats += statement.startswith(_CYCLE_BEAT)


async def test_a_60_target_cycle_runs_on_the_spec_s_pool_sizes(
    migrated: PgServer,
    make_config: ConfigFactory,
    queue_app: procrastinate.App,
    fake_llm: FakeLLM,
    current_epoch: int,
    record_property: Callable[[str, object], None],
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = make_config("worker", migrated)
    engine = make_engine(cfg, pool_size=ENGINE_POOL_SIZE)
    use = EngineUse(engine)
    client = make_http_client(
        fake_llm.cfg, mount=Latent(fake.RecordedSSETransport(fake_llm.cfg.fake_llm_dir), CALL_LATENCY_S)
    )
    try:
        gateway = LLMGateway(engine, fake_llm.cfg, http_client=client, cache=CacheMonitor())
        targets = await insert_targets(engine, TARGETS, lane="score_now")  # Score now: all admitted at once
        for target in targets:
            fake_llm.answer("generate", target.job_revision_id, synthetic_c_draft())
        async with open_saver(cfg.dsn("psycopg")) as saver:
            pool: Any = saver.conn
            assert (pool.min_size, pool.max_size) == CHECKPOINTER_POOL
            waits = PoolWaits(pool)
            deps = dataclasses.replace(await score_deps(engine, gateway), saver=saver, clock=utcnow)
            graphs = build_score_graphs(deps)
            started = time.perf_counter()
            cycle_id = await asyncio.wait_for(
                execute_cycle(
                    graphs,
                    deps,
                    trigger="score_now",
                    epoch=current_epoch,
                    fake_llm=True,
                    admission_stopped=lambda: False,
                ),
                LIMIT_S,
            )
            elapsed = time.perf_counter() - started
            stats = pool.get_stats()
        assert cycle_id is not None
        row = await cycle_of(engine, cycle_id)
        published = [(await target_of(engine, t.target_id)).state for t in targets]
    finally:
        await client.aclose()
        await engine.dispose()
    measured = {
        "cycle_s": round(elapsed, 1),
        "targets": TARGETS,
        "max_concurrency": MAX_CONCURRENCY,
        "call_latency_s": CALL_LATENCY_S,
        "checkpointer_pool": f"{CHECKPOINTER_POOL[0]}-{CHECKPOINTER_POOL[1]}",
        "checkpointer_requests": len(waits.waits),
        "checkpointer_peak_wait_ms": round(1000 * max(waits.waits, default=0.0), 1),
        "checkpointer_total_wait_ms": stats.get("requests_wait_ms", 0),
        "checkpointer_requests_queued": stats.get("requests_queued", 0),
        "checkpointer_timeouts": stats.get("requests_errors", 0),
        "engine_pool": ENGINE_POOL_SIZE,
        "engine_peak_checkout": use.peak,
        "engine_checkouts": len(use.waits),
        "engine_peak_wait_ms": round(1000 * max(use.waits, default=0.0), 1),
        "engine_waits_over_1s": sum(wait > 1.0 for wait in use.waits),
        "engine_timeouts": use.timeouts,
        "queue_pool_max": QUEUE_POOL_MAX_SIZE,
        "claim_beats": use.claim_beats,
        "cycle_beats": use.cycle_beats,
    }
    for name, value in measured.items():
        record_property(name, value)
    with capsys.disabled():
        print(f"\nscore_cycle_perf {measured}")
    assert (row.status, row.report["outcomes"]) == ("completed", {"published_accepted": TARGETS})
    assert published == ["published_accepted"] * TARGETS
    assert measured["checkpointer_timeouts"] == 0 and use.timeouts == 0 and elapsed < LIMIT_S
