"""The `awaiting_embedding` reconciler in `worker.embedding_reconcile` (spec §17.4 L5514; plan M3 Task 15, M1
Carry-forward 14, Rulings P15 and P44): it embeds the revisions awaiting their embedding and then sets the
similarity of the active tuple's targets that had none, and the api process no longer registers it.

The revision's vector is the fake embedder's (a deterministic unit vector of its text); the profiles' vectors
are written by hand (`profile_vector_at`), so the expected similarity is the one `similarity.similarity_for`
computes from the stored vectors. Every datum is invented.
"""

from datetime import timedelta
from typing import Any, Final

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.embeddings import EmbedderClient, make_http_client
from jobhunter.scoring import similarity
from jobhunter.worker import reconcilers
from jobhunter.worker.embedding_reconcile import reconcile_awaiting_embedding
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.collect import ctx
from tests.support.m2 import insert_profile_chain
from tests.support.m3 import Job
from tests.support.m3 import insert_target as insert_target_row
from tests.support.reconcile import registered_by
from tests.support.scoring import activate_score_tuple, insert_target, profile_of_tuple, profile_vector_at

pytestmark = pytest.mark.integration

LATER: Final = timedelta(minutes=11)  # past the reconciler's 10-minute grace
_TARGET: Final = sa.text("SELECT similarity, updated_at FROM score_targets WHERE id = :t")
_REVISION: Final = sa.text("SELECT embedding_status FROM job_revisions WHERE id = :r")


async def target(engine: AsyncEngine, target_id: int) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(_TARGET, {"t": target_id})).one()


async def test_a_target_created_without_similarity_gets_it_once_the_reconciler_embeds_its_revision(
    engine, current_epoch, fake_embedder, queue_app
) -> None:
    pending = await insert_target(engine)  # its revision awaits its embedding: the similarity is NULL
    async with engine.begin() as conn:
        tuple_id = await activate_score_tuple(conn)
        profile = await profile_of_tuple(conn, tuple_id)
        await profile_vector_at(conn, profile, "summary", (0, 0.6), (1, 0.8))
        other = await insert_profile_chain(conn, 7)  # a tuple that is not the active one, with vectors too
        await profile_vector_at(conn, other.profile_version_id, "summary", (0, 1.0))
        inactive = await insert_target_row(
            conn, Job(pending.job_id, pending.job_revision_id), other.llm_tuple_id
        )
    assert (await target(engine, pending.target_id)).similarity is None
    now = utcnow() + LATER
    async with make_http_client() as http:
        client = EmbedderClient(fake_embedder.base_url, http)
        assert await reconcile_awaiting_embedding(ctx(engine, queue_app, now, embedder=client)) == 1
    async with engine.connect() as conn:
        assert (await conn.execute(_REVISION, {"r": pending.job_revision_id})).scalar_one() == "embedded"
        expected = await similarity.similarity_for(
            conn, job_revision_id=pending.job_revision_id, profile_version_id=profile
        )
    assert expected is not None
    filled = await target(engine, pending.target_id)
    assert filled.similarity == pytest.approx(expected, abs=1e-6) and filled.updated_at == now
    assert (await target(engine, inactive)).similarity is None  # the active tuple's targets only


async def test_the_reconciler_sets_a_missing_similarity_of_a_revision_embedded_earlier(
    engine, current_epoch, fake_embedder, queue_app
) -> None:
    """A revision a run's `embed` node embedded after its target was created (the profile then had no
    vectors) has nothing left to embed: the pass still sets the target's similarity."""
    pending = await insert_target(engine)
    async with engine.begin() as conn:
        tuple_id = await activate_score_tuple(conn)
        await profile_vector_at(conn, await profile_of_tuple(conn, tuple_id), "summary", (0, 1.0))
        await conn.execute(
            sa.text(
                "UPDATE job_revisions SET embedding_status = 'embedded', embedding_id = "
                "(SELECT id FROM embeddings ORDER BY id LIMIT 1) WHERE id = :r"
            ),
            {"r": pending.job_revision_id},
        )
    async with make_http_client() as http:
        client = EmbedderClient(fake_embedder.base_url, http)
        assert (
            await reconcile_awaiting_embedding(ctx(engine, queue_app, utcnow() + LATER, embedder=client)) == 0
        )
    assert fake_embedder.batches == []  # nothing was embedded
    assert (await target(engine, pending.target_id)).similarity == pytest.approx(1.0, abs=1e-3)


def test_the_api_process_no_longer_registers_awaiting_embedding() -> None:
    """Ruling P15: the api's imports still register other reconcilers (harmless: only the worker runs
    `run_reconcilers`), but no longer `awaiting_embedding`, which `worker.embedding_reconcile` registers."""
    assert reconcilers.REGISTRY["awaiting_embedding"] == (frozenset({"heavy"}), reconcile_awaiting_embedding)
    registered = registered_by(("jobhunter.api.app",))["reconcilers"]
    assert isinstance(registered, dict) and registered  # the api registers others
    assert "awaiting_embedding" not in registered
