import asyncio
import math
from datetime import datetime, timedelta

import pytest
import sqlalchemy as sa
from jobhunter import embeddings
from jobhunter.clock import utcnow
from jobhunter.embeddings import (
    EMBEDDING_MODEL_ID,
    EmbedderClient,
    EmbedderError,
    EmbedReport,
    embed_awaiting,
    make_http_client,
    mark_embedded,
    reconcile_awaiting_embedding,
    upsert_embedding,
)
from jobhunter.errors import FencedOut
from jobhunter.graphs.collect.graph import error_handled
from jobhunter.graphs.collect.nodes.embed import BUDGET, make_embed
from jobhunter.graphs.collect.state import initial_state
from jobhunter.observability.metrics import REGISTRY
from jobhunter.quota import ledger
from jobhunter.sources.base import SourceAdapter
from jobhunter.sources.instances import InstanceRow
from jobhunter.worker import reconcilers, runs
from jobhunter.worker.runs import RunOwner

from tests.support.collect import ctx, make_deps, queued_run, supersede
from tests.support.faults import PausePoint
from tests.support.pg import blocked_or_done
from tests.support.procs import await_until

pytestmark = pytest.mark.integration

ALERT = "embedder.unavailable:embedder"
_STATUS = sa.text("SELECT embedding_status, count(*) FROM job_revisions GROUP BY embedding_status")
_OPEN_ALERT = sa.text("SELECT severity, context FROM alerts WHERE dedup_key = :k AND resolved_at IS NULL")
_ALERTS = sa.text("SELECT count(*) FROM alerts WHERE dedup_key = :k")
# Past the reconciler's 10 minutes: every revision inserted by the test is old enough.
LATER = timedelta(minutes=11)


def no_adapter(instance: InstanceRow) -> SourceAdapter:
    raise AssertionError(f"the embed node fetches nothing, asked for {instance.source_id}")


def embed_calls(outcome: str = "ok") -> float:
    """How many embed batch calls `jobhunter_embed_seconds` observed in this process with `outcome`."""
    return REGISTRY.get_sample_value("jobhunter_embed_seconds_count", {"outcome": outcome}) or 0.0


def skips(log_events) -> list[dict[str, object]]:
    """The `awaiting_embedding_skipped` lines logged so far (at INFO, as rendered)."""
    return [e for e in log_events() if e["event"] == "awaiting_embedding_skipped"]


async def finish(engine, owner: RunOwner) -> None:
    """End `owner`'s run `succeeded`: no run is `running` any more (the reconciler yields to one, T22-a)."""
    async with engine.begin() as c:
        await runs.finalize_run(c, owner, status="succeeded", reason=None, report={})


async def statuses(engine) -> dict[str, int]:
    async with engine.connect() as c:
        return {str(row[0]): int(row[1]) for row in await c.execute(_STATUS)}


async def embeddings_rows(engine) -> int:
    async with engine.connect() as c:
        return int((await c.execute(sa.text("SELECT count(*) FROM embeddings"))).scalar_one())


async def open_alert(engine) -> tuple[str, dict[str, object]] | None:
    async with engine.connect() as c:
        row = (await c.execute(_OPEN_ALERT, {"k": ALERT})).first()
    return None if row is None else (str(row.severity), dict(row.context))


async def test_batches_dedup_and_truncation(engine, current_epoch, fake_embedder, revisions_awaiting) -> None:
    # 20 awaiting revisions, two with identical text, one over 4,000 characters
    ids = await revisions_awaiting(engine, 20, duplicate_texts=2, long_texts=1)
    owner = await queued_run(engine)

    async def guard(c) -> None:  # the node's guard; the reconciler tests pass fence_epoch
        await ledger.lock_run_owner(c, owner)

    calls = embed_calls()
    async with make_http_client() as http:
        report = await embed_awaiting(engine, EmbedderClient(fake_embedder.base_url, http), guard=guard)
    assert (report.embedded, report.awaiting, report.truncated, report.unavailable) == (20, 0, 1, False)
    assert fake_embedder.batches == [16, 4] and embed_calls() == calls + 2  # `EMBED_SECONDS`, once per batch
    async with engine.connect() as c:
        rows = (
            await c.execute(
                sa.text(
                    "SELECT embedding_status, embedding_id, embedding_truncated FROM job_revisions "
                    "WHERE id = ANY(:ids) ORDER BY id"
                ),
                {"ids": ids},
            )
        ).all()
        n_emb = (await c.execute(sa.text("SELECT count(*), max(embedding_model_id) FROM embeddings"))).one()
        norm = (
            await c.execute(
                sa.text(
                    "SELECT sqrt(sum(x*x)) FROM unnest(string_to_array(trim(both '[]' from "
                    "(SELECT vector::text FROM embeddings LIMIT 1)), ',')::float[]) AS x"
                )
            )
        ).scalar_one()
        kinds = (await c.execute(sa.text("SELECT DISTINCT input_kind FROM embeddings"))).scalars().all()
    assert all(r.embedding_status == "embedded" and r.embedding_id for r in rows)
    assert [r.embedding_truncated for r in rows] == [False] * 19 + [True]  # the long one, the last
    # two identical texts share one embeddings row
    assert tuple(n_emb) == (19, EMBEDDING_MODEL_ID) and math.isclose(norm, 1.0, abs_tol=1e-2)
    assert rows[0].embedding_id == rows[1].embedding_id and len({r.embedding_id for r in rows}) == 19
    assert kinds == ["passage"]


async def test_unavailable_leaves_awaiting_and_the_reconciler_catches_up(
    engine, current_epoch, fake_embedder, revisions_awaiting, queue_app
) -> None:
    assert reconcilers.REGISTRY["awaiting_embedding"] == (frozenset({"heavy"}), reconcile_awaiting_embedding)
    await revisions_awaiting(engine, 5)
    owner = await queued_run(engine)
    fake_embedder.down = True
    unavailable = embed_calls("unavailable")

    async def guard(c) -> None:
        await ledger.lock_run_owner(c, owner)

    async with make_http_client() as http:
        client = EmbedderClient(fake_embedder.base_url, http)
        report = await embed_awaiting(engine, client, guard=guard)
        assert report == EmbedReport(embedded=0, awaiting=5, truncated=0, unavailable=True)
        assert embed_calls("unavailable") == unavailable + 1  # a failed batch is observed too
        assert await statuses(engine) == {"awaiting_embedding": 5} and await embeddings_rows(engine) == 0

        # the node: the same stop, counted, and the warn alert
        node = make_embed(make_deps(engine, no_adapter, embedder=client))
        update = await node(initial_state(owner.run_id, owner.attempt_no))
        assert update == {
            "counters": {"revisions_embedded": 0, "revisions_awaiting": 5, "revisions_truncated": 0},
            "steps": ["embed"],
        }
        assert await open_alert(engine) == ("warn", {"kind": "unavailable", "run_id": owner.run_id})
        await finish(engine, owner)  # the reconciler yields to a running run (T22-a)

        # the reconciler waits for a healthy embedder, then for the revisions to be 10 minutes old
        assert (
            await reconcile_awaiting_embedding(ctx(engine, queue_app, utcnow() + LATER, embedder=client)) == 0
        )
        fake_embedder.down = False
        assert await reconcile_awaiting_embedding(ctx(engine, queue_app, utcnow(), embedder=client)) == 0
        assert fake_embedder.batches == [] and await open_alert(engine) is not None
        assert (
            await reconcile_awaiting_embedding(ctx(engine, queue_app, utcnow() + LATER, embedder=client)) == 5
        )
    assert fake_embedder.batches == [5] and await statuses(engine) == {"embedded": 5}
    assert await open_alert(engine) is None  # resolved: healthy, and no backlog left
    async with engine.connect() as c:
        assert (await c.execute(_ALERTS, {"k": ALERT})).scalar_one() == 1
    # without a client (a context that names none) it does nothing
    assert await reconcile_awaiting_embedding(ctx(engine, queue_app, utcnow() + LATER)) == 0


async def test_the_reconciler_leaves_the_alert_open_while_a_backlog_remains(
    engine, current_epoch, fake_embedder, revisions_awaiting, queue_app, monkeypatch
) -> None:
    await revisions_awaiting(engine, 3)
    async with engine.begin() as c:
        await embeddings.raise_unavailable(c, kind="unavailable", run_id=None)
    monkeypatch.setattr(embeddings, "RECONCILE_LIMIT", 2)  # every pass embeds at most this many
    async with make_http_client() as http:
        context = ctx(
            engine, queue_app, utcnow() + LATER, embedder=EmbedderClient(fake_embedder.base_url, http)
        )
        assert await reconcile_awaiting_embedding(context) == 2
        assert fake_embedder.batches == [2] and await statuses(engine) == {
            "embedded": 2,
            "awaiting_embedding": 1,
        }
        assert await open_alert(engine) is not None
        assert await reconcile_awaiting_embedding(context) == 1
    assert fake_embedder.batches == [2, 1] and await open_alert(engine) is None


async def test_bad_response_is_an_error_not_a_write(
    engine, current_epoch, fake_embedder, revisions_awaiting
) -> None:
    await revisions_awaiting(engine, 3)
    owner = await queued_run(engine)

    async def guard(c) -> None:
        await ledger.lock_run_owner(c, owner)

    observed = {kind: embed_calls(kind) for kind in ("bad_response", "checksum")}
    async with make_http_client() as http:
        client = EmbedderClient(fake_embedder.base_url, http)
        for switch, value, kind in (
            ("dim", 8, "bad_response"),
            ("model_id", "other", "bad_response"),
            ("checksum_ok", False, "checksum"),
        ):
            fake_embedder.reset()
            setattr(fake_embedder, switch, value)
            with pytest.raises(EmbedderError) as raised:
                await embed_awaiting(engine, client, guard=guard)
            assert raised.value.kind == kind and fake_embedder.batches == [3]
            assert await embeddings_rows(engine) == 0 and await statuses(engine) == {"awaiting_embedding": 3}

        # in the graph: the node raises the alert, and the error handler persists the node's outcome
        fake_embedder.reset()
        fake_embedder.dim = 8
        node = error_handled("embed", make_embed(make_deps(engine, no_adapter, embedder=client)))
        update = await node(initial_state(owner.run_id, owner.attempt_no))
    assert update == {"steps": ["embed"], "counters": {"embedder_errors": 1}}
    assert await open_alert(engine) == ("warn", {"kind": "bad_response", "run_id": owner.run_id})
    # every failed batch is observed, by its outcome
    assert (embed_calls("bad_response"), embed_calls("checksum")) == (
        observed["bad_response"] + 3,
        observed["checksum"] + 1,
    )
    assert await embeddings_rows(engine) == 0 and await statuses(engine) == {"awaiting_embedding": 3}


async def test_superseded_owner_batch_is_fenced_after_the_http_call(
    engine, current_epoch, queue_app, fake_embedder, revisions_awaiting, monkeypatch
) -> None:
    await revisions_awaiting(engine, 3)
    owner = await queued_run(engine)
    pause = PausePoint("collect.embed.after_http")
    monkeypatch.setattr(embeddings, "afault_point", pause)

    async def guard(c) -> None:
        await ledger.lock_run_owner(c, owner)

    async with make_http_client() as http:
        client = EmbedderClient(fake_embedder.base_url, http)
        task = asyncio.create_task(embed_awaiting(engine, client, guard=guard))
        await asyncio.wait_for(pause.reached.wait(), 10)
        assert fake_embedder.batches == [3]  # the vectors are back
        taker = await supersede(engine, owner)  # a takeover: the attempt loses the run meanwhile
        pause.resume()
        with pytest.raises(FencedOut):
            await task
        # no revision of that batch embedded, no vector of the superseded owner committed
        assert await statuses(engine) == {"awaiting_embedding": 3} and await embeddings_rows(engine) == 0
        await finish(engine, taker)  # the reconciler yields to a running run (T22-a)

        # the reconciler entry point is fenced by the worker epoch: a superseded epoch writes nothing, and its
        # first transaction refuses it before any embed request
        assert (
            await reconcile_awaiting_embedding(ctx(engine, queue_app, utcnow() + LATER, 2, embedder=client))
            == 0
        )
        assert await statuses(engine) == {"awaiting_embedding": 3} and await embeddings_rows(engine) == 0
        assert fake_embedder.batches == [3]
        # the current epoch passes fence_epoch and embeds them
        n = await reconcile_awaiting_embedding(ctx(engine, queue_app, utcnow() + LATER, embedder=client))
    assert n == 3 and await statuses(engine) == {"embedded": 3} and await embeddings_rows(engine) == 3


async def test_a_revision_embedded_meanwhile_is_left_alone(
    engine, current_epoch, fake_embedder, revisions_awaiting, monkeypatch
) -> None:
    ids = await revisions_awaiting(engine, 3)
    owner = await queued_run(engine)
    pause = PausePoint("collect.embed.after_http")
    monkeypatch.setattr(embeddings, "afault_point", pause)

    async def guard(c) -> None:
        await ledger.lock_run_owner(c, owner)

    async with make_http_client() as http:
        task = asyncio.create_task(
            embed_awaiting(engine, EmbedderClient(fake_embedder.base_url, http), guard=guard)
        )
        await asyncio.wait_for(pause.reached.wait(), 10)
        # another caller embeds the first revision meanwhile, with a vector of its own
        async with engine.begin() as c:
            other = await upsert_embedding(
                c,
                model_id="test-model@0/fp32",
                text_sha256=b"x" * 32,
                input_kind="passage",
                vector=[1.0] * 1024,
            )
            assert await mark_embedded(c, ids[0], embedding_id=other, truncated=True)
            assert not await mark_embedded(
                c, ids[0], embedding_id=other, truncated=False
            )  # only an awaiting one
        pause.resume()
        report = await task
    assert (report.embedded, report.awaiting) == (2, 0)  # the batch counts only the revisions it moved
    async with engine.connect() as c:
        first = (
            await c.execute(
                sa.text("SELECT embedding_id, embedding_truncated FROM job_revisions WHERE id = :i"),
                {"i": ids[0]},
            )
        ).one()
    assert tuple(first) == (other, True)  # left alone


class SteppingClock:
    """A clock that moves `step` forward on every reading."""

    def __init__(self, start: datetime, step: timedelta) -> None:
        self.now, self.step = start, step

    def __call__(self) -> datetime:
        now = self.now
        self.now += self.step
        return now


async def test_the_node_stops_between_batches_at_its_deadline(
    engine, current_epoch, fake_embedder, revisions_awaiting
) -> None:
    assert BUDGET == timedelta(
        minutes=17
    )  # 20 min node timeout - 120 s for a batch - 60 s for its transaction
    await revisions_awaiting(engine, 40)
    owner = await queued_run(engine)
    async with make_http_client() as http:
        client = EmbedderClient(fake_embedder.base_url, http)
        deps = make_deps(engine, no_adapter, embedder=client)
        # readings: the deadline (start + 17 min), then one before each batch: 10 min in, then 20 min in
        deps.clock = SteppingClock(utcnow(), timedelta(minutes=10))
        update = await make_embed(deps)(initial_state(owner.run_id, owner.attempt_no))
        assert update == {
            "counters": {"revisions_embedded": 16, "revisions_awaiting": 24, "revisions_truncated": 0},
            "steps": ["embed"],
        }
        assert fake_embedder.batches == [16] and await statuses(engine) == {
            "embedded": 16,
            "awaiting_embedding": 24,
        }
        assert await open_alert(engine) is None  # a budget stop is no failure: the run is partial, no alert

        # a stopped admission (the worker's shutdown) starts no batch either
        stopped = make_deps(engine, no_adapter, embedder=client, admission_stopped=lambda: True)
        update = await make_embed(stopped)(initial_state(owner.run_id, owner.attempt_no))
    assert update["counters"] == {"revisions_embedded": 0, "revisions_awaiting": 24, "revisions_truncated": 0}
    assert fake_embedder.batches == [16]


async def test_the_node_without_an_embedder_counts_what_awaits(
    engine, current_epoch, revisions_awaiting
) -> None:
    await revisions_awaiting(engine, 2)
    owner = await queued_run(engine)
    update = await make_embed(make_deps(engine, no_adapter))(initial_state(owner.run_id, owner.attempt_no))
    assert update == {
        "counters": {"revisions_embedded": 0, "revisions_awaiting": 2, "revisions_truncated": 0},
        "steps": ["embed"],
    }
    assert await open_alert(engine) is None


# ---- Fix round 1 ------------------------------------------------------------------------------------------


async def test_the_reconciler_yields_to_a_running_run(
    engine, current_epoch, fake_embedder, revisions_awaiting, queue_app, log_events
) -> None:
    """Ruling T22-a: while a run is `running`, its `embed` node does the embedding; the pass does nothing."""
    await revisions_awaiting(engine, 5)
    owner = await queued_run(engine)  # admitted: `running`
    async with make_http_client() as http:
        context = ctx(
            engine, queue_app, utcnow() + LATER, embedder=EmbedderClient(fake_embedder.base_url, http)
        )
        assert await reconcile_awaiting_embedding(context) == 0
        assert fake_embedder.batches == [] and await statuses(engine) == {"awaiting_embedding": 5}
        assert [e["reason"] for e in skips(log_events)] == ["run_running"]
        await finish(engine, owner)
        assert await reconcile_awaiting_embedding(context) == 5
    assert fake_embedder.batches == [5] and await statuses(engine) == {"embedded": 5}


async def test_a_reconciler_pass_embeds_at_most_32_after_the_grace(
    engine, current_epoch, fake_embedder, revisions_awaiting, queue_app
) -> None:
    assert embeddings.RECONCILE_LIMIT == 32  # two batches: about 1 min of the shared tick queue (T22-a)
    await revisions_awaiting(engine, 40)
    async with make_http_client() as http:
        client = EmbedderClient(fake_embedder.base_url, http)
        # the grace: revisions awaiting for less than 10 minutes are left to the run that made them
        assert await reconcile_awaiting_embedding(ctx(engine, queue_app, utcnow(), embedder=client)) == 0
        assert fake_embedder.batches == []
        later = ctx(engine, queue_app, utcnow() + LATER, embedder=client)
        assert await reconcile_awaiting_embedding(later) == 32
        assert fake_embedder.batches == [16, 16]
        assert await statuses(engine) == {"embedded": 32, "awaiting_embedding": 8}
        assert await reconcile_awaiting_embedding(later) == 8
    assert fake_embedder.batches == [16, 16, 8] and await statuses(engine) == {"embedded": 40}


async def test_the_reconciler_logs_why_it_skips(
    engine, current_epoch, fake_embedder, revisions_awaiting, queue_app, log_events
) -> None:
    await revisions_awaiting(engine, 2)
    assert await reconcile_awaiting_embedding(ctx(engine, queue_app, utcnow() + LATER)) == 0
    fake_embedder.checksum_ok = False
    async with make_http_client() as http:
        client = EmbedderClient(fake_embedder.base_url, http)
        assert (
            await reconcile_awaiting_embedding(ctx(engine, queue_app, utcnow() + LATER, embedder=client)) == 0
        )
    skipped = skips(log_events)
    assert [e["reason"] for e in skipped] == ["no_embedder", "embedder_unhealthy"]
    assert skipped[1]["checksum_ok"] is False
    assert fake_embedder.batches == [] and await statuses(engine) == {"awaiting_embedding": 2}


async def test_an_embedding_another_transaction_committed_is_reused(engine, migrated) -> None:
    """M-3: the `DO NOTHING` then `SELECT` path, across transactions: both callers get the one row's id."""
    async with engine.connect() as first:
        await first.begin()
        first_id = await _upsert_unit_vector(first)  # inserted, not committed yet
        second = asyncio.create_task(_upsert_in_own_transaction(engine))
        # the second insert waits for the first transaction's row (its unique key) before it can decide
        await await_until(lambda: blocked_or_done(migrated, second), 10, 0.02)
        assert not second.done()
        await first.commit()
    second_id = await asyncio.wait_for(second, 10)  # DO NOTHING, then a SELECT that sees the committed row
    assert second_id == first_id
    # and once both committed, a third caller gets it at once
    assert await _upsert_in_own_transaction(engine) == first_id
    assert await embeddings_rows(engine) == 1


async def _upsert_unit_vector(conn) -> int:
    return await upsert_embedding(
        conn,
        model_id=EMBEDDING_MODEL_ID,
        text_sha256=b"\x07" * 32,
        input_kind="passage",
        vector=[1.0] + [0.0] * 1023,
    )


async def _upsert_in_own_transaction(engine) -> int:
    async with engine.begin() as c:
        return await _upsert_unit_vector(c)
