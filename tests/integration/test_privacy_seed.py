"""The two-year-old seed of the deletion-graph tests (plan M5 Task 13; `tests.support.privacy_seed`).

The seed commits on the real schema with every CHECK, unique and partial-unique index, foreign key and trigger
enabled, so a commit is the proof that its rows satisfy the schema as it stands (the plan's Global
Constraints, Fixtures). Every table of `public` holds a row of it, read from the live catalog, so a table a
later task adds fails the first test until the seed fills it: no table escapes the deletion-graph tests.
"""

import re
from datetime import timedelta
from typing import Final, get_args

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.privacy_seed import SEED_AGE, InFlight, seed_two_year_old_graph

pytestmark = pytest.mark.integration

# The tables no seed fills, each with its reason.
NOT_SEEDED: Final = {
    "alembic_version": "the migrator's record of the schema revision, no data of the application",
    "schema_compat": "`migrate`'s record of the migration chain, which `reset_database` resets",
    # LangGraph's saver: its tables hold checkpoint threads, which the saver's API writes and deletes
    "checkpoints": "the checkpointer's own table",
    "checkpoint_blobs": "the checkpointer's own table",
    "checkpoint_writes": "the checkpointer's own table",
    "checkpoint_migrations": "the checkpointer's record of its schema",
}
# procrastinate's tables (its own prefix): the queue's jobs, events, periodic defers and workers.
QUEUE_PREFIX: Final = "procrastinate_"
_TABLES: Final = sa.text(
    "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') ORDER BY c.relname"
)
# The live submissions and the singletons and partial-unique indexes of the seed rule (Global Constraints).
_SINGLETONS: Final = sa.text(
    "SELECT (SELECT count(*) FROM batch_submissions WHERE state IN ('preparing', 'submit_started', "
    "'submitted', 'submit_ambiguous', 'ended') AND released_at IS NULL) AS live_submissions, "
    "(SELECT count(*) FROM exports WHERE status = 'running') AS running_exports, "
    "(SELECT count(*) FROM runs WHERE status IN ('queued', 'running')) AS active_runs, "
    "(SELECT count(*) FROM score_cycles WHERE status = 'running') AS running_cycles, "
    "(SELECT count(*) FROM bulk_operations WHERE state = 'active') AS active_bulk, "
    "(SELECT count(*) FROM profile_drafts WHERE status IN ('uploaded', 'extracting_text', 'text_review', "
    "'extracting_profile', 'ready')) AS open_drafts, "
    "(SELECT count(*) FROM quota_windows WHERE is_current) AS current_windows"
)
_LIVE: Final = sa.text(
    "SELECT s.state, s.released_at, (SELECT array_agg(t.state ORDER BY t.id) FROM score_targets t "
    "WHERE t.batch_submission_id = s.id) AS targets, (SELECT array_agg(l.state ORDER BY l.id) "
    "FROM batch_items i JOIN llm_calls c ON c.id = i.llm_call_id JOIN llm_spend_ledger l "
    "ON l.id = c.spend_ledger_id WHERE i.submission_id = s.id) AS entries "
    "FROM batch_submissions s WHERE s.id = :s"
)
_IN_BATCH: Final = sa.text(
    "SELECT count(*) FROM score_targets WHERE state = 'in_batch' AND batch_submission_id IS DISTINCT FROM :s"
)
_TARGET_STATES: Final = sa.text("SELECT DISTINCT state FROM score_targets ORDER BY state")
_TERMINAL: Final = sa.text(
    "SELECT state, released_at IS NOT NULL AS released FROM batch_submissions WHERE id <> :s ORDER BY state"
)
_STATE_DOMAIN: Final = sa.text(
    "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = 'score_targets'::regclass "
    "AND contype = 'c' AND pg_get_constraintdef(oid) LIKE '%state = ANY%pending%'"
)
# Every foreign key into the job graph's tables, with the referencing table and column(s), by name.
_GRAPH_KEYS: Final = sa.text(
    "SELECT c.conname, CAST(c.conrelid AS regclass)::text AS child, CAST(c.confrelid AS regclass)::text "
    "AS parent, (SELECT array_agg(a.attname ORDER BY k.n) FROM unnest(c.conkey) WITH ORDINALITY AS k(num, n) "
    "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.num) AS columns FROM pg_constraint c "
    "WHERE c.contype = 'f' AND CAST(c.confrelid AS regclass)::text IN ('jobs', 'job_revisions', 'postings') "
    "ORDER BY c.conname"
)
_GRAPH_ROWS: Final = {
    "job_revisions": sa.text("SELECT id FROM job_revisions WHERE job_id = ANY(CAST(:jobs AS bigint[]))"),
    "postings": sa.text("SELECT id FROM postings WHERE job_id = ANY(CAST(:jobs AS bigint[]))"),
}
_DATES: Final = sa.text(
    "SELECT min(first_seen_at) AS first_seen, max(availability_changed_at) AS changed FROM jobs"
)


async def public_tables(engine: AsyncEngine) -> list[str]:
    async with engine.connect() as conn:
        return [str(name) for name in (await conn.execute(_TABLES)).scalars()]


async def test_the_seed_fills_every_table(engine: AsyncEngine, current_epoch: int) -> None:
    now = utcnow()
    async with engine.begin() as conn:
        await seed_two_year_old_graph(conn, now=now)
    tables = await public_tables(engine)
    assert set(NOT_SEEDED) <= set(tables)  # each exclusion names a table that exists
    seeded = [t for t in tables if t not in NOT_SEEDED and not t.startswith(QUEUE_PREFIX)]
    assert len(seeded) >= 87  # the 87 tables of 2026-11-10, and every table added since
    async with engine.connect() as conn:
        empty = [
            table
            for table in seeded
            if not (await conn.execute(sa.text(f'SELECT EXISTS (SELECT 1 FROM "{table}")'))).scalar_one()  # noqa: S608
        ]
        dates = (await conn.execute(_DATES)).one()
        states = {str(state) for state in (await conn.execute(_TARGET_STATES)).scalars()}
        domain = str((await conn.execute(_STATE_DOMAIN)).scalar_one())
    assert empty == []
    # about two years back: every job first seen then, its availability changed a day later
    assert dates.first_seen == now - SEED_AGE
    assert dates.changed <= now - SEED_AGE + timedelta(days=1)
    assert states == set(re.findall(r"'([a-z_]+)'::text", domain))  # targets in every state of the CHECK


@pytest.mark.parametrize("live_submission", get_args(InFlight))
async def test_the_seed_holds_for_every_live_submission_state(
    engine: AsyncEngine, current_epoch: int, live_submission: InFlight
) -> None:
    """The seed commits with every constraint enabled in each live state (deferred keys included), holding
    each singleton and partial-unique index of the seed rule: its one live submission holds every `in_batch`
    target, its entries `reserved` before the submit starts and `dispatched` after, and the other
    submissions are terminal."""
    async with engine.begin() as conn:
        ids = await seed_two_year_old_graph(conn, now=utcnow(), live_submission=live_submission)
    async with engine.connect() as conn:
        singletons = (await conn.execute(_SINGLETONS)).one()
        live = (await conn.execute(_LIVE, {"s": ids.live_submission_id})).one()
        elsewhere = (await conn.execute(_IN_BATCH, {"s": ids.live_submission_id})).scalar_one()
        terminal = [tuple(row) for row in await conn.execute(_TERMINAL, {"s": ids.live_submission_id})]
    assert tuple(singletons) == (1, 0, 0, 0, 1, 0, 0)
    assert (live.state, live.released_at) == (live_submission, None)
    assert live.targets == ["in_batch", "in_batch"] and elsewhere == 0
    entry = "reserved" if live_submission == "preparing" else "dispatched"
    assert live.entries == [entry, entry]
    assert terminal == [("abandoned", False), ("reconciled", False), ("released", True)]


async def test_every_foreign_key_into_the_job_graph_has_a_seed_row_on_the_due_graph(
    engine: AsyncEngine, current_epoch: int
) -> None:
    """Every foreign key (from the live catalog) into `jobs`, `job_revisions` or `postings` has a seed row
    that references the graph a tombstoning pass deletes: a due or merged job, or one of their revisions or
    postings. A table a later task adds with a key into the job graph fails here until the seed links a row
    of it to the deleted graph, so the seed-backed tombstone test proves the graph deletion covers it."""
    async with engine.begin() as conn:
        ids = await seed_two_year_old_graph(conn, now=utcnow())
    jobs = sorted({*ids.due_job_ids, *ids.merged_job_ids})
    async with engine.connect() as conn:
        graph = {"jobs": jobs}
        for table, statement in _GRAPH_ROWS.items():
            graph[table] = [int(i) for i in (await conn.execute(statement, {"jobs": jobs})).scalars()]
        keys = (await conn.execute(_GRAPH_KEYS)).all()
        unlinked = []
        for key in keys:
            assert len(key.columns) == 1, key.conname  # the graph's keys are single-column
            linked = f'SELECT EXISTS (SELECT 1 FROM "{key.child}" WHERE "{key.columns[0]}" = ANY(:ids))'  # noqa: S608
            if not (await conn.execute(sa.text(linked), {"ids": graph[key.parent]})).scalar_one():
                unlinked.append(key.conname)
    assert len(keys) >= 28  # the keys into the job graph of 2026-11-10, and every key added since
    assert all(graph.values())
    assert unlinked == []
