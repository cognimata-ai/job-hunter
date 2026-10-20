"""Task 25: M1 acceptance through the real collection graph (`execute_run` on `build_collect_graph`), on the
fixtures (fake mode) and the fake embedder: two manual runs over one query and two watchlist boards.

The inputs: the query `software engineer` (the default scope plans its `remote-de` cell), the watchlist
companies `getyourguide` (Greenhouse) and `bunq` (Recruitee), and every source switch on (`SOURCE_SWITCHES`).
The first run includes the paid sources, the second does not.
"""

import pytest
import sqlalchemy as sa
from jobhunter.graphs.collect.deps import CollectDeps
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.collect import SOURCE_SWITCHES, run_to_the_end

pytestmark = pytest.mark.integration

# The Rejected items of the fixtures this run is served, as tests/contract/README.md records them (Task 6
# step 8; Ruling P7): 0, so the first run ends `succeeded`.
FIXTURE_REJECTED = 0
QUERIES = ("software engineer",)
COMPANIES = (("greenhouse", "getyourguide"), ("recruitee", "bunq"))


async def run(engine: AsyncEngine, deps: CollectDeps, *, include_paid: bool) -> int:
    """A manual run over the end-to-end inputs, run to its end on the real graph (`run_to_the_end`)."""
    return await run_to_the_end(
        engine, deps, include_paid=include_paid, queries=QUERIES, companies=COMPANIES, enabled=SOURCE_SWITCHES
    )


async def test_two_runs_on_fixtures_through_the_real_graph(
    engine, current_epoch, fake_http, fake_embedder, collect_deps, migrated
) -> None:
    rid = await run(engine, collect_deps, include_paid=True)
    async with engine.connect() as c:
        status, reason, report = (
            await c.execute(sa.text("SELECT status, status_reason, report FROM runs WHERE id=:i"), {"i": rid})
        ).one()
        rejected = (
            await c.execute(sa.text("SELECT count(*) FROM raw_items WHERE disposition='rejected'"))
        ).scalar_one()
        revisions = (await c.execute(sa.text("SELECT count(*) FROM job_revisions"))).scalar_one()
        pending = (
            await c.execute(sa.text("SELECT count(*) FROM raw_items WHERE disposition='pending'"))
        ).scalar_one()
        conserved = (
            await c.execute(
                sa.text(
                    "SELECT bool_and(r.item_count = (SELECT count(*) FROM raw_items i "
                    "WHERE i.raw_response_id = r.id)) FROM raw_responses r"
                )
            )
        ).scalar_one()
        jobs, postings, linked = [
            (await c.execute(sa.text(q))).scalar_one()
            for q in (
                "SELECT count(*) FROM jobs",
                "SELECT count(*) FROM postings",
                "SELECT count(*) FROM raw_items WHERE disposition='linked'",
            )
        ]
        ledger = dict(
            (
                await c.execute(
                    sa.text(
                        "SELECT provider || ':' || coalesce(state, kind), count(*) FROM quota_ledger "
                        "GROUP BY 1"
                    )
                )
            ).all()
        )
        snapshots = (
            await c.execute(sa.text("SELECT status, member_count FROM ats_snapshots ORDER BY id"))
        ).all()
        embedded = (
            await c.execute(sa.text("SELECT count(*) FROM job_revisions WHERE embedding_status='embedded'"))
        ).scalar_one()
        groups = (
            await c.execute(sa.text("SELECT count(*) FROM duplicate_groups WHERE status='active'"))
        ).scalar_one()
        hf = (
            await c.execute(
                sa.text(
                    "SELECT count(*) FROM jobs j WHERE NOT EXISTS "
                    "(SELECT 1 FROM hard_filter_results h WHERE h.job_id = j.id)"
                )
            )
        ).scalar_one()
    assert rejected == FIXTURE_REJECTED and (status, reason) == (
        ("succeeded", None) if FIXTURE_REJECTED == 0 else ("partial", "raw_item_rejected")
    )
    assert pending == 0 and conserved and jobs > 0 and postings == linked and hf == 0
    # JSearch: page 1 (the probe) plus page 2 (10 new ids ≥ 7); Adzuna: page 1 then the empty page 2. The
    # two JSearch adjustments (state NULL) are §6.3 step 3's tighten-only reconciliation: the recordings'
    # remaining counts (198 after page 1, 196 after page 2) show one more used unit than the ledger each time.
    # The SerpApi adjustment is the same reconciliation of plan_fallback's account read, which is persisted
    # (final review F3, Ruling T7-b): the recorded account has 248 of its 250 searches left, 2 the ledger
    # never counted.
    assert ledger == {
        "jsearch:billed": 2,
        "adzuna:billed": 2,
        "jsearch:adjustment": 2,
        "serpapi:adjustment": 1,
    }
    assert [s.status for s in snapshots] == ["complete", "complete"] and all(
        s.member_count > 0 for s in snapshots
    )
    # the Adzuna re-ingest pair of the fixtures forms a group (adzuna_reingest_pair, Task 6)
    assert embedded == revisions > 0 and groups >= 1
    assert all(v["ok"] for v in report["execution"]["invariants"].values())
    assert report["quota"]["jsearch"]["used"] == 2

    second = await run(engine, collect_deps, include_paid=False)
    async with engine.connect() as c:
        second_status = (
            await c.execute(sa.text("SELECT status, status_reason FROM runs WHERE id=:i"), {"i": second})
        ).one()
        billed = dict(
            (
                await c.execute(
                    sa.text("SELECT provider, count(*) FROM quota_ledger WHERE state = 'billed' GROUP BY 1")
                )
            ).all()
        )
        paid_entries = (
            await c.execute(
                sa.text(
                    "SELECT count(*) FROM quota_ledger WHERE run_id = :i "
                    "AND provider IN ('jsearch', 'serpapi')"
                ),
                {"i": second},
            )
        ).scalar_one()
        again = (await c.execute(sa.text("SELECT count(*) FROM jobs"))).scalar_one()
        seen = (
            await c.execute(
                sa.text(
                    "SELECT min(seen_count) FROM postings WHERE source_instance_id IN (SELECT id FROM "
                    "source_instances WHERE source_id IN ('arbeitnow','greenhouse','recruitee'))"
                )
            )
        ).scalar_one()
        gaps = (
            await c.execute(sa.text("SELECT kind, gap_status, reason FROM coverage_intervals ORDER BY id"))
        ).all()
    # the JSearch page-1-with-cursor rule of §5.6 (Carry-forward 5): the JSearch cell keeps an open gap
    assert again == jobs and seen == 2 and ("gap", "open", "page_cap") in {tuple(g) for g in gaps}
    # Task 25 review, Minor 5: the free run succeeds and sends no paid request: the paid billed entries stay
    # the first run's 2 (JSearch), and the run has no JSearch or SerpApi ledger entry at all. Adzuna, a quota
    # source (`quota_search`, §7.5) and not a paid one, is read by a free run as well: its two pages are
    # billed again.
    assert tuple(second_status) == ("succeeded", None) and paid_entries == 0
    assert billed == {"jsearch": 2, "adzuna": 4}
