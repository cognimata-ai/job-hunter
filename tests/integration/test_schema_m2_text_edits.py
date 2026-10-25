"""The draft's stored text edit set (migration 0007; M2 Task 14, ruling R14-1): `profile_drafts.text_edits`.

The column holds the complete edit set of the text review as offset pairs into the raw text, and no content:
a new draft starts with the empty set, and the CHECK keeps its two lists. Migrations are expand/contract, so
0007 has no downgrade either.
"""

import asyncio
import importlib

import pytest
import sqlalchemy as sa
from alembic import command
from jobhunter.db.alembic_runner import alembic_config, code_head
from jobhunter.db.sql import CHECK_VIOLATION, pg_error

from tests.support.m2 import insert_cv_file
from tests.support.pg import pg_one, pg_rows

pytestmark = pytest.mark.integration
M2_SCHEMA = "0006_m2_llm_profile_settings"
M2_HEAD = "0007_m2_draft_text_edits"
EMPTY: dict[str, list[object]] = {"restored": [], "removed": []}
_NEW_DRAFT = (
    "INSERT INTO profile_drafts (cv_file_id, status) VALUES (:cv, 'uploaded') RETURNING id, text_edits"
)


def test_0007_is_the_head_and_follows_0006() -> None:
    module = importlib.import_module(f"jobhunter.db.migrations.versions.{M2_HEAD}")
    assert (module.revision, module.down_revision) == (M2_HEAD, M2_SCHEMA)
    assert code_head() == M2_HEAD


def test_0007_has_no_downgrade() -> None:
    module = importlib.import_module(f"jobhunter.db.migrations.versions.{M2_HEAD}")
    with pytest.raises(NotImplementedError, match="expand/contract"):
        module.downgrade()


async def test_a_new_draft_starts_with_the_empty_edit_set(engine) -> None:
    async with engine.connect() as c:
        cv = await insert_cv_file(c)
        row = (await c.execute(sa.text(_NEW_DRAFT), {"cv": cv})).one()
        assert row.text_edits == EMPTY
        column = (
            await c.execute(
                sa.text(
                    "SELECT data_type, is_nullable FROM information_schema.columns "
                    "WHERE table_name = 'profile_drafts' AND column_name = 'text_edits'"
                )
            )
        ).one()
        assert tuple(column) == ("jsonb", "NO")
        # Offset pairs are what it holds.
        await c.execute(
            sa.text("UPDATE profile_drafts SET text_edits = CAST(:e AS jsonb) WHERE id = :d"),
            {"e": '{"restored": [[0, 14]], "removed": [[20, 31], [40, 41]]}', "d": row.id},
        )


@pytest.mark.parametrize(
    "value",
    [
        pytest.param('{"restored": []}', id="no-removed-list"),
        pytest.param('{"removed": []}', id="no-restored-list"),
        pytest.param('{"restored": {}, "removed": []}', id="restored-not-a-list"),
        pytest.param('{"restored": [], "removed": "0-3"}', id="removed-not-a-list"),
        pytest.param("[]", id="not-an-object"),
    ],
)
async def test_the_edit_set_keeps_its_two_lists(engine, value: str) -> None:
    async with engine.connect() as c:
        cv = await insert_cv_file(c)
        draft = (await c.execute(sa.text(_NEW_DRAFT), {"cv": cv})).one().id
        with pytest.raises(sa.exc.IntegrityError) as bad:
            async with c.begin_nested():
                await c.execute(
                    sa.text("UPDATE profile_drafts SET text_edits = CAST(:e AS jsonb) WHERE id = :d"),
                    {"e": value, "d": draft},
                )
        info = pg_error(bad.value)
        assert (info.sqlstate, info.constraint) == (CHECK_VIOLATION, "profile_drafts_text_edits_check")


async def test_the_edit_set_is_never_null(engine) -> None:
    async with engine.connect() as c:
        cv = await insert_cv_file(c)
        draft = (await c.execute(sa.text(_NEW_DRAFT), {"cv": cv})).one().id
        with pytest.raises(sa.exc.IntegrityError):
            async with c.begin_nested():
                await c.execute(
                    sa.text("UPDATE profile_drafts SET text_edits = NULL WHERE id = :d"), {"d": draft}
                )


async def test_an_upgrade_from_0006_gives_every_draft_the_empty_set_and_no_downgrade_runs(
    fresh_pg_server,
) -> None:
    dsn = fresh_pg_server.dsn("migrator", driver="sqlalchemy")
    await asyncio.to_thread(command.upgrade, alembic_config(dsn), M2_SCHEMA)
    pg_one(
        fresh_pg_server,
        "migrator",
        "INSERT INTO cv_files (sha256, mime, size_bytes) VALUES ('\\x01', 'text/plain', 1)",
    )
    for status in ("discarded", "text_review"):
        pg_one(
            fresh_pg_server,
            "migrator",
            "INSERT INTO profile_drafts (cv_file_id, status) SELECT id, %s FROM cv_files",
            status,
        )
    await asyncio.to_thread(command.upgrade, alembic_config(dsn), "head")
    assert pg_rows(
        fresh_pg_server, "migrator", "SELECT status, text_edits FROM profile_drafts ORDER BY id"
    ) == [
        ("discarded", EMPTY),
        ("text_review", EMPTY),
    ]
    assert pg_one(fresh_pg_server, "migrator", "SELECT version_num FROM alembic_version") == (M2_HEAD,)
    with pytest.raises(NotImplementedError, match="expand/contract"):
        await asyncio.to_thread(command.downgrade, alembic_config(dsn), M2_SCHEMA)
    assert pg_one(fresh_pg_server, "migrator", "SELECT version_num FROM alembic_version") == (M2_HEAD,)
