"""Task 19: `embed_profile` and the profile vectors (spec §12.6 step 3, §7.10, §20.1; plan Deviation 23).

A confirmed profile version has up to `1 + len(target_titles)` vectors: its `summary` and each target title,
embedded as queries by the fake embedder (Task 22's HTTP contract, with the service's `query: ` prefix). These
tests run the real handler through `execute_operation` (the `op_runner` fixture) on that fake, and the
`profile_vectors` reconciler on a real reconcile context.

Every profile here is invented. The write's race cases pause the handler after its embed call
(`vectors.AFTER_EMBED_POINT`) with the same in-process `PausePoint` the other operation tests use.
"""

import asyncio
import dataclasses
import json
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx
import pytest
import sqlalchemy as sa
from jobhunter import embeddings
from jobhunter.embeddings import (
    EMBEDDING_DIM,
    EMBEDDING_MODEL_ID,
    EmbedderClient,
    make_http_client,
    text_sha256,
)
from jobhunter.errors import FencedOut
from jobhunter.llm.schemas.profile import ConfirmedProfile
from jobhunter.profile import vectors
from jobhunter.worker import operations as worker_operations
from jobhunter.worker.operations import MAX_OPERATION_ATTEMPTS, cancel_operation
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.collect import ctx
from tests.support.embedder import QUERY_PREFIX, FakeEmbedder, fake_vector
from tests.support.faults import PausePoint, SimulatedCrash
from tests.support.m2 import ProfileChain, insert_embedding, insert_profile_chain
from tests.support.pg import blocked_or_done
from tests.support.procs import await_until
from tests.support.profile import SYNTHETIC_PROFILE, OpRunner

pytestmark = pytest.mark.integration

SUMMARY = "Erfahrene Backend-Entwicklerin mit Schwerpunkt Zahlungsdienste."
TITLES = ("Senior Backend Developer", "Backend-Entwicklerin", "Platform Engineer")
# A clock far ahead of the database's, so that a requeued operation is not due when the test looks at it.
NOW = datetime(2031, 3, 1, 9, 0, tzinfo=UTC)


class RecordingEmbedder(EmbedderClient):
    """An `EmbedderClient` that records every request: the texts and the input kind it was asked for."""

    def __init__(self, base_url: str, client: httpx.AsyncClient) -> None:
        super().__init__(base_url, client)
        self.requests: list[tuple[list[str], str]] = []

    async def embed(
        self,
        texts: Sequence[str],
        *,
        input_kind: embeddings.InputKind,
        timeout_s: float = embeddings.BATCH_TIMEOUT_S,
    ) -> list[embeddings.Embedded]:
        self.requests.append((list(texts), input_kind))
        return await super().embed(texts, input_kind=input_kind, timeout_s=timeout_s)


@pytest.fixture
async def embedder(fake_embedder: FakeEmbedder) -> AsyncIterator[RecordingEmbedder]:
    async with make_http_client() as http:
        yield RecordingEmbedder(fake_embedder.base_url, http)


@pytest.fixture
def runner(op_runner: OpRunner, embedder: RecordingEmbedder) -> OpRunner:
    """`op_runner` whose worker has the recording embedder (on the fake) and a clock fixed at `NOW`."""
    op_runner.deps = dataclasses.replace(op_runner.deps, embedder=embedder, clock=lambda: NOW)
    return op_runner


def profile_json(summary: str = SUMMARY, titles: Sequence[str] = TITLES) -> dict[str, Any]:
    """The stored `profile_json` of a version: the synthetic profile with `summary` and `titles`."""
    body = {**SYNTHETIC_PROFILE, "summary": summary}
    body["target_titles"] = [{"title": title, "lang": "en"} for title in titles]
    return ConfirmedProfile.model_validate(body).model_dump(mode="json")


async def make_version(engine: AsyncEngine, n: int = 1, **kwargs: Any) -> ProfileChain:
    """A confirmed profile version (and its tuple) number `n`, with `profile_json(**kwargs)`."""
    async with engine.begin() as conn:
        return await insert_profile_chain(conn, n, profile=profile_json(**kwargs))


async def activate(engine: AsyncEngine, chain: ProfileChain) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            sa.text("UPDATE active_config SET profile_version_id = :p, llm_tuple_id = :t WHERE id = 1"),
            {"p": chain.profile_version_id, "t": chain.llm_tuple_id},
        )


async def request(engine: AsyncEngine, version_id: int) -> int:
    """A new `embed_profile` operation of the version (no job deferred); its id."""
    async with engine.begin() as conn:
        op_id, created = await vectors.request_embedding(conn, version_id, defer=None)
    assert created
    return op_id


async def rows(engine: AsyncEngine, sql: str, **params: object) -> list[Any]:
    async with engine.connect() as conn:
        return list((await conn.execute(sa.text(sql), params)).all())


async def vector_ids(engine: AsyncEngine, version_id: int) -> dict[str, int]:
    """The version's `profile_vectors`: embedding id by kind."""
    found = await rows(
        engine, "SELECT kind, embedding_id FROM profile_vectors WHERE profile_version_id = :p", p=version_id
    )
    return {str(row.kind): int(row.embedding_id) for row in found}


async def count(engine: AsyncEngine, table: str) -> int:
    [row] = await rows(engine, f"SELECT count(*) AS n FROM {table}")  # noqa: S608 - constant table names
    return int(row.n)


async def operation(engine: AsyncEngine, op_id: int) -> Any:
    [row] = await rows(
        engine,
        "SELECT status, attempts, not_before, last_error_code, progress FROM operations WHERE id = :i",
        i=op_id,
    )
    return row


async def nothing_written(engine: AsyncEngine) -> None:
    assert await count(engine, "profile_vectors") == 0
    assert await count(engine, "embeddings") == 0


async def distance_to(engine: AsyncEngine, embedding_id: int, text: str) -> float:
    """The cosine distance between the stored vector and the fake embedder's vector for `query: <text>`."""
    literal = "[" + ",".join(repr(x) for x in fake_vector(QUERY_PREFIX + text, EMBEDDING_DIM)) + "]"
    [row] = await rows(
        engine,
        "SELECT vector <=> CAST(:v AS halfvec) AS d FROM embeddings WHERE id = :i",
        v=literal,
        i=embedding_id,
    )
    return float(row.d)


Purge = Literal["purged", "profile_fence", "global_fence"]


async def purge(engine: AsyncEngine, how: Purge, version_id: int) -> str:
    """What a profile purge leaves behind for a consumer, or its fence; the `skipped` value it must produce:
    `purged` is the purge's step 5 (§20.2: the content is gone, the row stays), a fence is its step 1."""
    async with engine.begin() as conn:
        match how:
            case "purged":
                await conn.execute(
                    sa.text(
                        "UPDATE profile_versions SET profile_json = NULL, purged_at = now() WHERE id = :p"
                    ),
                    {"p": version_id},
                )
                return "purged"
            case "profile_fence":
                await conn.execute(
                    sa.text(
                        "INSERT INTO purge_fences (kind, profile_version_id, manifest_id) "
                        "VALUES ('profile', :p, 1)"
                    ),
                    {"p": version_id},
                )
            case "global_fence":
                await conn.execute(
                    sa.text("INSERT INTO purge_fences (kind, manifest_id) VALUES ('global', 1)")
                )
    return "fenced"


# --- the vectors --------------------------------------------------------------------------------------------


async def test_a_version_with_three_titles_gets_four_query_vectors(engine, runner, embedder) -> None:
    chain = await make_version(engine)
    op_id = await request(engine, chain.profile_version_id)
    await runner.run_one(op_id)

    stored = await vector_ids(engine, chain.profile_version_id)
    texts = {"summary": SUMMARY, "title:0": TITLES[0], "title:1": TITLES[1], "title:2": TITLES[2]}
    assert stored.keys() == texts.keys()
    # the embedder was sent the exact texts, once, as queries (it adds the `query: ` prefix itself)
    assert embedder.requests == [([SUMMARY, *TITLES], "query")]
    for kind, embedding_id in stored.items():
        [row] = await rows(
            engine,
            "SELECT embedding_model_id, text_sha256, input_kind FROM embeddings WHERE id = :i",
            i=embedding_id,
        )
        assert (row.embedding_model_id, bytes(row.text_sha256), row.input_kind) == (
            EMBEDDING_MODEL_ID,
            text_sha256(texts[kind]),
            "query",
        )
        # and the vector is the fake service's answer for the prefixed text
        assert await distance_to(engine, embedding_id, texts[kind]) < 0.01
    done = await operation(engine, op_id)
    assert (done.status, done.attempts, done.last_error_code) == ("succeeded", 0, None)


async def test_a_second_run_is_a_no_op(engine, runner, embedder) -> None:
    chain = await make_version(engine)
    await runner.run_one(await request(engine, chain.profile_version_id))
    snapshot = "SELECT kind, embedding_id, xmin::text AS written_by FROM profile_vectors ORDER BY kind"
    first = [tuple(row) for row in await rows(engine, snapshot)]
    ids_before = await vector_ids(engine, chain.profile_version_id)

    # a complete version needs no embedder: this worker has none, and the operation still succeeds
    runner.deps = dataclasses.replace(runner.deps, embedder=None)
    second_op = await request(engine, chain.profile_version_id)
    await runner.run_one(second_op)

    assert (await operation(engine, second_op)).status == "succeeded"
    assert await vector_ids(engine, chain.profile_version_id) == ids_before
    assert [tuple(row) for row in await rows(engine, snapshot)] == first  # not one row rewritten
    assert await count(engine, "embeddings") == 4
    assert len(embedder.requests) == 1  # the first run's


async def test_a_run_restores_a_lost_vector_and_removes_a_stale_kind(engine, runner, embedder) -> None:
    chain = await make_version(engine)
    await runner.run_one(await request(engine, chain.profile_version_id))
    before = await vector_ids(engine, chain.profile_version_id)
    async with engine.begin() as conn:
        await conn.execute(
            sa.text("DELETE FROM profile_vectors WHERE profile_version_id = :p AND kind = 'title:1'"),
            {"p": chain.profile_version_id},
        )
        await conn.execute(  # a kind the version no longer has (its text is gone)
            sa.text(
                "INSERT INTO profile_vectors (profile_version_id, kind, embedding_id) "
                "VALUES (:p, 'title:7', :e)"
            ),
            {"p": chain.profile_version_id, "e": before["summary"]},
        )

    await runner.run_one(await request(engine, chain.profile_version_id))

    assert await vector_ids(engine, chain.profile_version_id) == before  # same embedding rows, same kinds
    assert len(embedder.requests) == 2
    assert await count(engine, "embeddings") == 4


# A vector over an embedding that is not the version's text embedded as a query by the current model: another
# text, another model, or a passage. Each is stored under the right kind, so only the comparison of the text
# hash, the model id and the input kind tells that the version is not complete.
FOREIGN = {
    "text": {"label": "Some other text", "model_id": EMBEDDING_MODEL_ID, "input_kind": "query"},
    "model": {"label": TITLES[1], "model_id": "some-other-model", "input_kind": "query"},
    "input_kind": {"label": TITLES[1], "model_id": EMBEDDING_MODEL_ID, "input_kind": "passage"},
}


async def point_title_at_foreign_embedding(engine: AsyncEngine, version_id: int, case: str) -> int:
    """Re-point the version's `title:1` at an embedding row of `FOREIGN[case]`; the row's id."""
    async with engine.begin() as conn:
        foreign = await insert_embedding(conn, **FOREIGN[case])
        await conn.execute(
            sa.text(
                "UPDATE profile_vectors SET embedding_id = :e "
                "WHERE profile_version_id = :p AND kind = 'title:1'"
            ),
            {"e": foreign, "p": version_id},
        )
    return foreign


@pytest.mark.parametrize("case", sorted(FOREIGN))
async def test_a_vector_over_another_text_model_or_input_kind_is_not_complete(
    engine, runner, embedder, case
) -> None:
    chain = await make_version(engine)
    await runner.run_one(await request(engine, chain.profile_version_id))
    before = await vector_ids(engine, chain.profile_version_id)
    foreign = await point_title_at_foreign_embedding(engine, chain.profile_version_id, case)
    assert (await vector_ids(engine, chain.profile_version_id))["title:1"] == foreign

    await runner.run_one(await request(engine, chain.profile_version_id))

    assert len(embedder.requests) == 2  # it embedded again: the version was not complete
    assert await vector_ids(engine, chain.profile_version_id) == before  # and the right rows are back


async def test_the_embeddings_are_upserted_in_the_order_of_their_text_hash(
    engine, runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two writes of shared texts for different versions meet the embeddings' unique key in one order."""
    seen: list[bytes] = []
    upsert = embeddings.upsert_embedding

    async def recording(conn: Any, **kwargs: Any) -> int:
        seen.append(kwargs["text_sha256"])
        return await upsert(conn, **kwargs)

    monkeypatch.setattr(
        "jobhunter.profile.vectors.upsert_embedding", recording
    )  # the name `write_vectors` calls
    chain = await make_version(engine)
    in_kind_order = [text_sha256(text) for text in (SUMMARY, *TITLES)]
    assert in_kind_order != sorted(in_kind_order)  # the test would pass vacuously otherwise

    await runner.run_one(await request(engine, chain.profile_version_id))

    assert seen == sorted(in_kind_order)
    assert sorted(await vector_ids(engine, chain.profile_version_id)) == [
        "summary",
        "title:0",
        "title:1",
        "title:2",
    ]


async def test_versions_with_the_same_texts_share_their_embedding_rows(engine, runner) -> None:
    first, second = await make_version(engine, 1), await make_version(engine, 2)
    await runner.run_one(await request(engine, first.profile_version_id))
    await runner.run_one(await request(engine, second.profile_version_id))
    assert await vector_ids(engine, first.profile_version_id) == await vector_ids(
        engine, second.profile_version_id
    )
    assert await count(engine, "profile_vectors") == 8
    assert await count(engine, "embeddings") == 4


async def test_equal_titles_share_one_embedding_row_and_keep_their_kinds(engine, runner) -> None:
    chain = await make_version(engine, titles=("Data Engineer", "Data Engineer"))
    await runner.run_one(await request(engine, chain.profile_version_id))
    stored = await vector_ids(engine, chain.profile_version_id)
    assert sorted(stored) == ["summary", "title:0", "title:1"]
    assert stored["title:0"] == stored["title:1"] != stored["summary"]
    assert await count(engine, "embeddings") == 2


async def test_a_version_without_any_text_ends_with_no_vector_and_no_request(
    engine, runner, embedder
) -> None:
    chain = await make_version(engine, summary="", titles=())
    op_id = await request(engine, chain.profile_version_id)
    await runner.run_one(op_id)
    assert (await operation(engine, op_id)).status == "succeeded"
    assert embedder.requests == []
    await nothing_written(engine)


# --- an embedder that is not there --------------------------------------------------------------------------


async def test_a_down_embedder_requeues_for_ten_minutes_and_writes_nothing(
    engine, runner, fake_embedder, embedder
) -> None:
    chain = await make_version(engine)
    op_id = await request(engine, chain.profile_version_id)
    fake_embedder.down = True

    await runner.run_one(op_id)

    waiting = await operation(engine, op_id)
    assert (waiting.status, waiting.attempts, waiting.last_error_code) == (
        "pending",
        1,
        "embedder_unavailable",
    )
    assert waiting.not_before == NOW + timedelta(minutes=10)
    await nothing_written(engine)

    # once it is back and the wait is over, the retry writes the vectors
    fake_embedder.down = False
    async with engine.begin() as conn:
        await conn.execute(sa.text("UPDATE operations SET not_before = NULL WHERE id = :i"), {"i": op_id})
    await runner.run_one(op_id)
    assert (await operation(engine, op_id)).status == "succeeded"
    assert sorted(await vector_ids(engine, chain.profile_version_id)) == [
        "summary",
        "title:0",
        "title:1",
        "title:2",
    ]


async def test_a_worker_without_an_embedder_requeues_the_same_way(engine, runner, fake_embedder) -> None:
    chain = await make_version(engine)
    op_id = await request(engine, chain.profile_version_id)
    runner.deps = dataclasses.replace(runner.deps, embedder=None)

    await runner.run_one(op_id)

    waiting = await operation(engine, op_id)
    assert (waiting.status, waiting.attempts, waiting.last_error_code) == (
        "pending",
        1,
        "embedder_unavailable",
    )
    assert waiting.not_before == NOW + timedelta(minutes=10)
    assert fake_embedder.batches == []  # nothing was asked of any embedder
    await nothing_written(engine)


async def test_a_wrong_model_is_an_embedder_error_too(engine, runner, fake_embedder) -> None:
    """Any `EmbedderError`, not only `unavailable`, gives the operation back; nothing of it is kept."""
    chain = await make_version(engine)
    op_id = await request(engine, chain.profile_version_id)
    fake_embedder.model_id = "some-other-model"

    await runner.run_one(op_id)

    waiting = await operation(engine, op_id)
    assert (waiting.status, waiting.last_error_code) == ("pending", "embedder_unavailable")
    await nothing_written(engine)


# --- the reconciler -----------------------------------------------------------------------------------------


async def test_an_exhausted_operation_is_recreated_by_the_reconciler_once_the_embedder_is_up(
    engine, queue_app, current_epoch, runner, fake_embedder, embedder
) -> None:
    chain = await make_version(engine)
    await activate(engine, chain)
    op_id = await request(engine, chain.profile_version_id)
    async with engine.begin() as conn:  # its last attempt
        await conn.execute(
            sa.text("UPDATE operations SET attempts = :a WHERE id = :i"),
            {"a": MAX_OPERATION_ATTEMPTS - 1, "i": op_id},
        )
    fake_embedder.down = True
    await runner.run_one(op_id)
    exhausted = await operation(engine, op_id)
    assert (exhausted.status, exhausted.last_error_code) == ("failed", "attempts_exhausted")
    # the alert is the only signal that a profile has no vectors (the handler's docstring relies on it)
    [alert] = await rows(
        engine,
        "SELECT severity FROM alerts WHERE dedup_key = :k AND resolved_at IS NULL",
        k=f"operation.failed:operation:{op_id}",
    )
    assert alert.severity == "warn"

    # while the embedder is down, nothing is re-created
    context = ctx(engine, queue_app, epoch=current_epoch, embedder=embedder)
    assert await vectors.reconcile_profile_vectors(context) == 0
    assert await count(engine, "operations") == 1

    fake_embedder.down = False
    assert await vectors.reconcile_profile_vectors(context) == 1
    created = await rows(engine, "SELECT id, status, payload FROM operations WHERE id <> :i", i=op_id)
    assert [(row.status, row.payload) for row in created] == [
        ("pending", {"profile_version_id": chain.profile_version_id})
    ]
    assert (await operation(engine, op_id)).status == "failed"  # the old one stays as it ended

    await runner.run_pending()
    assert sorted(await vector_ids(engine, chain.profile_version_id)) == [
        "summary",
        "title:0",
        "title:1",
        "title:2",
    ]
    # complete now: the next pass has nothing to do
    assert await vectors.reconcile_profile_vectors(context) == 0
    assert await count(engine, "operations") == 2


@pytest.mark.parametrize("damage", ["missing", *sorted(FOREIGN)])
async def test_the_reconciler_repairs_a_version_with_three_of_its_four_vectors(
    engine, queue_app, current_epoch, runner, embedder, damage
) -> None:
    """Not only a version without any vector: a lost vector, or one over another text, model or input kind."""
    chain = await make_version(engine)
    await activate(engine, chain)
    await runner.run_one(await request(engine, chain.profile_version_id))
    context = ctx(engine, queue_app, epoch=current_epoch, embedder=embedder)
    assert await vectors.reconcile_profile_vectors(context) == 0  # complete: nothing to do
    if damage == "missing":
        async with engine.begin() as conn:
            await conn.execute(
                sa.text("DELETE FROM profile_vectors WHERE profile_version_id = :p AND kind = 'title:2'"),
                {"p": chain.profile_version_id},
            )
        assert len(await vector_ids(engine, chain.profile_version_id)) == 3
    else:
        await point_title_at_foreign_embedding(engine, chain.profile_version_id, damage)

    assert await vectors.reconcile_profile_vectors(context) == 1

    created = await rows(engine, "SELECT status, payload FROM operations WHERE status = 'pending'")
    assert [(row.status, row.payload) for row in created] == [
        ("pending", {"profile_version_id": chain.profile_version_id})
    ]
    await runner.run_pending()
    assert sorted(await vector_ids(engine, chain.profile_version_id)) == [
        "summary",
        "title:0",
        "title:1",
        "title:2",
    ]
    assert await vectors.reconcile_profile_vectors(context) == 0


async def test_a_default_test_chain_has_a_profile_the_reconciler_can_read(
    engine, queue_app, current_epoch, embedder
) -> None:
    """`insert_profile_chain`'s default `profile_json` is a valid profile (it once was `{}`, which no
    `ConfirmedProfile` accepts), so an active default chain does not break a heavy reconciler pass."""
    async with engine.begin() as conn:
        chain = await insert_profile_chain(conn, 1)
    await activate(engine, chain)
    context = ctx(engine, queue_app, epoch=current_epoch, embedder=embedder)
    assert await vectors.reconcile_profile_vectors(context) == 1


async def test_the_reconciler_leaves_alone_what_does_not_need_it(
    engine, queue_app, current_epoch, embedder, fake_embedder
) -> None:
    context = ctx(engine, queue_app, epoch=current_epoch, embedder=embedder)
    # no active profile version
    assert await vectors.reconcile_profile_vectors(context) == 0
    chain = await make_version(engine)
    await activate(engine, chain)
    # no embedder in this process: nothing can be asked, nothing is created
    assert await vectors.reconcile_profile_vectors(ctx(engine, queue_app, epoch=current_epoch)) == 0
    assert await count(engine, "operations") == 0
    # a live operation of the version is enough
    live = await request(engine, chain.profile_version_id)
    assert await vectors.reconcile_profile_vectors(context) == 0
    assert await count(engine, "operations") == 1
    assert (await operation(engine, live)).status == "pending"


async def test_the_reconciler_skips_a_purged_or_fenced_active_version(
    engine, queue_app, current_epoch, embedder
) -> None:
    context = ctx(engine, queue_app, epoch=current_epoch, embedder=embedder)
    chain = await make_version(engine)
    await activate(engine, chain)
    async with engine.begin() as conn:
        await conn.execute(
            sa.text(
                "INSERT INTO purge_fences (kind, profile_version_id, manifest_id) VALUES ('profile', :p, 1)"
            ),
            {"p": chain.profile_version_id},
        )
    assert await vectors.reconcile_profile_vectors(context) == 0
    async with engine.begin() as conn:
        await conn.execute(sa.text("UPDATE purge_fences SET released_at = now()"))
        await conn.execute(sa.text("UPDATE profile_versions SET profile_json = NULL, purged_at = now()"))
    assert await vectors.reconcile_profile_vectors(context) == 0
    assert await count(engine, "operations") == 0


async def test_a_superseded_workers_pass_is_fenced_and_creates_nothing(
    engine, queue_app, current_epoch, embedder
) -> None:
    chain = await make_version(engine)
    await activate(engine, chain)
    stale = ctx(engine, queue_app, epoch=current_epoch + 1, embedder=embedder)  # another epoch is current

    with pytest.raises(FencedOut) as fenced:
        await vectors.reconcile_profile_vectors(stale)

    assert fenced.value.worker_superseded
    assert await count(engine, "operations") == 0


# --- a version that is gone ---------------------------------------------------------------------------------


async def test_a_purged_version_is_skipped(engine, runner, embedder) -> None:
    chain = await make_version(engine)
    op_id = await request(engine, chain.profile_version_id)
    await purge(engine, "purged", chain.profile_version_id)

    await runner.run_one(op_id)

    done = await operation(engine, op_id)
    assert (done.status, done.progress) == ("succeeded", {"skipped": "purged"})
    assert embedder.requests == []
    await nothing_written(engine)


async def test_a_version_that_does_not_exist_any_more_is_skipped_as_purged(engine, runner, embedder) -> None:
    chain = await make_version(engine)
    op_id = await request(engine, chain.profile_version_id)
    async with engine.begin() as conn:  # delete-everything removes the version rows (§20.4)
        await conn.execute(sa.text("SET CONSTRAINTS ALL DEFERRED"))
        await conn.execute(sa.text("UPDATE active_config SET profile_version_id = NULL, llm_tuple_id = NULL"))
        await conn.execute(sa.text("DELETE FROM llm_tuples"))
        await conn.execute(sa.text("DELETE FROM profile_drafts"))
        await conn.execute(sa.text("DELETE FROM profile_versions"))

    await runner.run_one(op_id)

    done = await operation(engine, op_id)
    assert (done.status, done.progress) == ("succeeded", {"skipped": "purged"})
    assert embedder.requests == []


@pytest.mark.parametrize("fence", ["profile_fence", "global_fence"])
async def test_a_version_under_an_active_purge_fence_is_skipped(engine, runner, embedder, fence) -> None:
    chain = await make_version(engine)
    op_id = await request(engine, chain.profile_version_id)
    assert await purge(engine, fence, chain.profile_version_id) == "fenced"

    await runner.run_one(op_id)

    done = await operation(engine, op_id)
    assert (done.status, done.progress) == ("succeeded", {"skipped": "fenced"})
    assert embedder.requests == []
    await nothing_written(engine)


async def test_a_fence_on_another_version_and_a_released_fence_skip_nothing(engine, runner) -> None:
    chain = await make_version(engine)
    other = await make_version(engine, 2)
    async with engine.begin() as conn:
        await conn.execute(
            sa.text(
                "INSERT INTO purge_fences (kind, profile_version_id, manifest_id) VALUES ('profile', :p, 1)"
            ),
            {"p": other.profile_version_id},
        )
        await conn.execute(
            sa.text(
                "INSERT INTO purge_fences (kind, profile_version_id, manifest_id, released_at) "
                "VALUES ('profile', :p, 2, now())"
            ),
            {"p": chain.profile_version_id},
        )
    op_id = await request(engine, chain.profile_version_id)
    await runner.run_one(op_id)
    done = await operation(engine, op_id)
    assert (done.status, done.progress) == ("succeeded", {})
    assert sorted(await vector_ids(engine, chain.profile_version_id)) == [
        "summary",
        "title:0",
        "title:1",
        "title:2",
    ]


# --- the write ----------------------------------------------------------------------------------------------


async def paused_after_embed(runner: OpRunner, op_id: int) -> tuple[asyncio.Task[None], PausePoint]:
    """Start the execution of `op_id` and hold it right after its embed call (outside any transaction)."""
    pause = PausePoint(vectors.AFTER_EMBED_POINT)
    task = asyncio.create_task(runner.run_one(op_id, pause=pause))
    await asyncio.wait_for(pause.reached.wait(), 60)
    return task, pause


@pytest.mark.parametrize("how", ["purged", "profile_fence", "global_fence"])
async def test_a_version_purged_after_the_embed_call_gets_no_stale_vector(
    engine, runner, embedder, how
) -> None:
    chain = await make_version(engine)
    op_id = await request(engine, chain.profile_version_id)
    task, pause = await paused_after_embed(runner, op_id)
    assert len(embedder.requests) == 1  # the vectors are in hand, nothing is written yet
    await nothing_written(engine)
    skipped = await purge(engine, how, chain.profile_version_id)
    pause.resume()
    await asyncio.wait_for(task, 30)

    done = await operation(engine, op_id)
    assert (done.status, done.progress) == ("succeeded", {"skipped": skipped})
    await nothing_written(engine)  # not a vector, and not an embedding row


async def test_the_write_waits_for_a_purge_that_holds_the_version_row(engine, runner, migrated) -> None:
    """The profile purge locks the version row `FOR UPDATE` before it deletes anything (§20.2 step 1). The
    write takes the same row `FOR SHARE`, so it waits for that transaction and then reads what the purge did,
    instead of racing a snapshot that predates it."""
    chain = await make_version(engine)
    op_id = await request(engine, chain.profile_version_id)
    task, pause = await paused_after_embed(runner, op_id)
    version = {"p": chain.profile_version_id}
    async with engine.connect() as purger:
        transaction = await purger.begin()
        await purger.execute(sa.text("SELECT id FROM profile_versions WHERE id = :p FOR UPDATE"), version)
        pause.resume()
        await await_until(lambda: blocked_or_done(migrated, task), 10, 0.02)
        assert not task.done()  # waiting for the purge's lock, with nothing written
        await purger.execute(
            sa.text("UPDATE profile_versions SET profile_json = NULL, purged_at = now() WHERE id = :p"),
            version,
        )
        await transaction.commit()
    await asyncio.wait_for(task, 30)

    done = await operation(engine, op_id)
    assert (done.status, done.progress) == ("succeeded", {"skipped": "purged"})
    await nothing_written(engine)


async def test_a_write_after_the_claim_was_lost_writes_nothing(engine, runner) -> None:
    chain = await make_version(engine)
    op_id = await request(engine, chain.profile_version_id)
    task, pause = await paused_after_embed(runner, op_id)
    async with engine.begin() as conn:  # the operation is cancelled (or reclaimed) meanwhile
        assert await cancel_operation(conn, op_id, reason="test")
    pause.resume()
    await asyncio.wait_for(task, 30)  # the execution is lost: it ends with no owner write

    assert (await operation(engine, op_id)).status == "cancelled"
    await nothing_written(engine)


async def test_the_vectors_and_the_end_of_the_operation_commit_together(
    engine, runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    chain = await make_version(engine)
    op_id = await request(engine, chain.profile_version_id)
    finish = worker_operations.finish_operation

    async def finish_then_crash(*args: Any, **kwargs: Any) -> None:
        await finish(*args, **kwargs)
        raise SimulatedCrash("after the operation's end, before the commit")

    monkeypatch.setattr(worker_operations, "finish_operation", finish_then_crash)

    await runner.run_one(op_id)

    # the transaction rolled back as a whole: no vector, no embedding row, and the operation is not finished
    await nothing_written(engine)
    retried = await operation(engine, op_id)
    assert (retried.status, retried.last_error_code) == ("pending", "bug")


# --- logs ---------------------------------------------------------------------------------------------------


async def test_logs_carry_ids_and_never_the_profile_text(
    engine, runner, fake_embedder, log_events: Callable[[], list[dict[str, Any]]]
) -> None:
    chain = await make_version(engine, 1)
    other = await make_version(
        engine, 2, summary="Zweites Profil, anderer Text.", titles=("Site Reliability Engineer",)
    )
    await runner.run_one(await request(engine, chain.profile_version_id))
    fake_embedder.down = True
    await runner.run_one(await request(engine, other.profile_version_id))  # requeued

    events = log_events()
    text = json.dumps(events, ensure_ascii=False)
    for secret in (SUMMARY, *TITLES, "Zweites Profil", "Site Reliability Engineer"):
        assert secret not in text
    [done] = [e for e in events if e["event"] == "embed_profile_done"]
    assert (done["version_id"], done["vectors"]) == (chain.profile_version_id, 4)
    [requeued] = [e for e in events if e["event"] == "embed_profile_requeued"]
    assert requeued["version_id"] == other.profile_version_id
    assert (requeued["reason"], requeued["error_kind"]) == ("embedder_error", "unavailable")
