"""Task 19: the `embeddings` retention rule keeps what `profile_vectors` references (spec §20.1, §7.10).

§20.1: "Embeddings: while a revision or profile vector references them; `retention_daily` (unreferenced
rows)", and a profile's vectors live "with the profile content", removed by the profile purge. The schema
agrees: `profile_vectors.embedding_id` has no cascade (a delete of a referenced embedding is refused), and
`profile_vectors.profile_version_id` cascades only when the version row itself is deleted, which only
delete-everything does. A profile purge keeps the version row (it nulls `profile_json`), so it deletes the
version's `profile_vectors` explicitly (§20.2 step 3); their embeddings are then unreferenced and the next
daily pass removes them. Later M2 tasks add their retention rules to this file.
"""

from datetime import timedelta

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.embeddings import EMBEDDING_MODEL_ID, text_sha256, upsert_embedding
from jobhunter.graphs.checkpointer import open_saver
from jobhunter.privacy.retention import run_retention_daily

from tests.support.m2 import insert_embedding, insert_profile_chain, insert_vector

pytestmark = pytest.mark.integration

CURRENT_EPOCH = 1  # the worker epoch the `current_epoch` fixture records
DAY = timedelta(days=1)


async def retention_pass(engine, queue_app, migrated) -> dict[str, int]:
    """One `retention_daily` pass a day after the rows were written (the rule measures from its own `now`)."""
    async with open_saver(migrated.dsn("app")) as saver:
        return await run_retention_daily(engine, queue_app, saver, now=utcnow() + DAY, epoch=CURRENT_EPOCH)


async def embedding_ids(engine) -> set[int]:
    async with engine.connect() as c:
        return set((await c.execute(sa.text("SELECT id FROM embeddings"))).scalars())


async def test_embeddings_referenced_by_profile_vectors_are_kept(
    engine, current_epoch, queue_app, migrated
) -> None:
    async with engine.begin() as c:
        first = await insert_profile_chain(c, 1)
        second = await insert_profile_chain(c, 2)
        # both versions hold a `summary` vector: two embedding rows, one each (the helper hashes both)
        referenced = {
            await insert_vector(c, first.profile_version_id, "summary"),
            await insert_vector(c, first.profile_version_id, "title:0"),
            await insert_vector(c, second.profile_version_id, "summary"),
        }
        unreferenced = {await insert_embedding(c, "loose-1"), await insert_embedding(c, "loose-2")}
    assert len(referenced) == 3
    assert await embedding_ids(engine) == referenced | unreferenced

    report = await retention_pass(engine, queue_app, migrated)

    assert report["embeddings"] == 2
    assert await embedding_ids(engine) == referenced


async def test_the_embeddings_of_a_purged_versions_deleted_vectors_go_with_the_next_pass(
    engine, current_epoch, queue_app, migrated
) -> None:
    async with engine.begin() as c:
        purged = await insert_profile_chain(c, 1)
        kept = await insert_profile_chain(c, 2)
        gone = {await insert_vector(c, purged.profile_version_id, k) for k in ("summary", "title:0")}
        stays = {await insert_vector(c, kept.profile_version_id, "summary")}
    assert (await retention_pass(engine, queue_app, migrated))["embeddings"] == 0  # everything is referenced

    async with engine.begin() as c:  # §20.2 step 3: the purge deletes the version's vectors
        await c.execute(
            sa.text("DELETE FROM profile_vectors WHERE profile_version_id = :p"),
            {"p": purged.profile_version_id},
        )

    assert (await retention_pass(engine, queue_app, migrated))["embeddings"] == len(gone)
    assert await embedding_ids(engine) == stays


async def test_an_embedding_a_profile_write_is_about_to_reference_survives_the_pass(
    engine, current_epoch, queue_app, migrated
) -> None:
    """`embed_profile` upserts an embedding row and then references it from `profile_vectors` in one
    transaction. An existing, unreferenced row it has upserted is held `FOR KEY SHARE`
    (`upsert_embedding`), so a pass that runs in between skips it instead of deleting a row the write is
    about to reference."""
    text = "Senior Backend Developer"
    async with engine.begin() as c:
        chain = await insert_profile_chain(c, 1)
        left_over = await upsert_embedding(
            c,
            model_id=EMBEDDING_MODEL_ID,
            text_sha256=text_sha256(text),
            input_kind="query",
            vector=[0.0] * 1023 + [1.0],
        )
    async with engine.connect() as writer:
        transaction = await writer.begin()
        again = await upsert_embedding(
            writer,
            model_id=EMBEDDING_MODEL_ID,
            text_sha256=text_sha256(text),
            input_kind="query",
            vector=[0.0] * 1023 + [1.0],
        )
        assert again == left_over  # the existing row, now held

        assert (await retention_pass(engine, queue_app, migrated))["embeddings"] == 0  # skipped, not deleted

        await writer.execute(
            sa.text(
                "INSERT INTO profile_vectors (profile_version_id, kind, embedding_id) "
                "VALUES (:p, 'summary', :e)"
            ),
            {"p": chain.profile_version_id, "e": again},
        )
        await transaction.commit()

    assert (await retention_pass(engine, queue_app, migrated))["embeddings"] == 0
    assert await embedding_ids(engine) == {left_over}
