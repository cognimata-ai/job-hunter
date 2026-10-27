"""Task 23: node 10, `finalize` (§9.2, §17.2, §17.3, §7.16.4, §17.6), and the run end owned by the graph."""

import asyncio
import json
from datetime import timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.db.transitions import CLEAR_REASON, transition
from jobhunter.errors import FencedOut
from jobhunter.graphs.collect import report as report_module
from jobhunter.graphs.collect.graph import build_collect_graph, build_noop_collect_graph
from jobhunter.graphs.collect.nodes import finalize as finalize_module
from jobhunter.graphs.collect.nodes.finalize import (
    InvariantCode,
    InvariantResult,
    check_invariants,
    decide_status,
    finalize_from_graph,
    partial_conditions,
    run_revisions,
)
from jobhunter.graphs.collect.report import FallbackRequests
from jobhunter.graphs.collect.state import initial_state
from jobhunter.observability.metrics import REGISTRY
from jobhunter.quota import ledger
from jobhunter.quota.pacing import fair_share, per_run_cap
from jobhunter.quota.windows import current_snapshot
from jobhunter.sources.base import Observation
from jobhunter.sources.instances import InstanceRow
from jobhunter.worker import runs
from jobhunter.worker.runs import RunCompletion, RunEnd
from langgraph.checkpoint.memory import InMemorySaver

from tests.support.collect import (
    DEFAULT_BOARD,
    TEST_QUERY,
    conflicting_graph,
    make_deps,
    make_posting,
    manual_run_id,
    pending_item,
    planned_request,
    queued_run,
    resolved,
    run_to_the_end,
    seed_inputs,
    source_cell,
    supersede,
)
from tests.support.m2 import insert_profile_chain
from tests.support.pg import pg_rows
from tests.support.quota import insert_window, jsearch_window, seeded_run_settings

pytestmark = pytest.mark.integration

TEXTS = sa.text(
    "SELECT title, description_text FROM jobs j JOIN job_revisions r ON r.id = j.current_revision_id"
)
RUN_END = sa.text("SELECT status, status_reason, report FROM runs WHERE id = :i")
OPEN_ALERT = sa.text("SELECT severity FROM alerts WHERE dedup_key = :k AND resolved_at IS NULL")
ANY_ALERT = sa.text("SELECT count(*) FROM alerts WHERE dedup_key = :k")
BOARD_INSTANCE = sa.text("SELECT id FROM source_instances WHERE source_id = :s AND instance_key = :k")
NO_FALLBACK = FallbackRequests(planned=0, served=0)  # a run without SerpApi fallback requests


async def run_end(engine, rid: int) -> tuple[str, str | None, dict[str, Any]]:
    async with engine.connect() as c:
        status, reason, report = (await c.execute(RUN_END, {"i": rid})).one()
    return status, reason, report


async def open_alert(engine, dedup_key: str) -> str | None:
    async with engine.connect() as c:
        severity = (await c.execute(OPEN_ALERT, {"k": dedup_key})).scalar()
    return None if severity is None else str(severity)


async def board_key(engine) -> str:
    """The report key of the default board: its source and instance id, never its slug (carry 7)."""
    async with engine.connect() as c:
        instance = (
            await c.execute(BOARD_INSTANCE, {"s": DEFAULT_BOARD[0], "k": DEFAULT_BOARD[1]})
        ).scalar_one()
    return f"{DEFAULT_BOARD[0]}/instance:{instance}"


async def test_happy_run_succeeds_with_a_full_report(
    engine, current_epoch, fake_http, fake_embedder, collect_deps
) -> None:
    # a watchlist company + Arbeitnow, the real graph through execute_run
    rid = await run_to_the_end(engine, collect_deps)
    async with engine.connect() as c:
        status, reason, report = (
            await c.execute(sa.text("SELECT status, status_reason, report FROM runs WHERE id=:i"), {"i": rid})
        ).one()
    assert (status, reason) == ("succeeded", None) and set(report) == {
        "sources",
        "identity",
        "revisions",
        "quota",
        "execution",
    }
    assert all(v["ok"] for v in report["execution"]["invariants"].values()) and set(
        report["execution"]["invariants"]
    ) == {"I1", "I2", "I3", "I4", "I5", "I6", "I7"}
    # Ruling T23-a: the Arbeitnow fixture run reads page 1, page 2 and the END page (T10-a)
    assert (
        report["sources"]["arbeitnow/default"]["requests"] == {"succeeded": 3}
        and report["identity"]["jobs_created"] > 0
        and "plan" in report["execution"]["nodes"]
    )
    board = report["sources"][await board_key(engine)]
    assert board["requests"] == {"succeeded": 1} and board["health"] == "ok"
    assert set(board) == {
        "requests",
        "units",
        "items",
        "items_out_of_scope",
        "postings",
        "availability",
        "gaps",
        "health",
    }
    arbeitnow = report["sources"]["arbeitnow/default"]
    # the dispositions are counted from the rows; every posting of a first run is new
    async with engine.connect() as c:
        linked = (
            await c.execute(
                sa.text(
                    "SELECT count(*) FROM raw_items i JOIN raw_responses r ON r.id = i.raw_response_id "
                    "JOIN fetch_requests f ON f.id = r.fetch_request_id JOIN source_instances s "
                    "ON s.id = f.source_instance_id WHERE f.run_id = :r AND s.source_id = 'arbeitnow' "
                    "AND i.disposition = 'linked'"
                ),
                {"r": rid},
            )
        ).scalar_one()
    assert arbeitnow["items"].get("linked") == linked > 0
    assert arbeitnow["postings"]["new"] > 0 and arbeitnow["postings"]["updated"] == 0
    assert arbeitnow["units"] == {"billed": 0, "ambiguous": 0, "not_billed": 0}
    revisions = report["revisions"]
    assert revisions["created"] == revisions["embedded"] > 0 and revisions["awaiting"] == 0
    # no tuple was active: node 9 made nothing (M3 Task 14's shape of the section)
    assert revisions["targets"] == {"created": 0, "revived": 0, "cancelled": 0, "skipped": {}}
    assert report["quota"]["fallback"]["used"] is False
    assert report["execution"]["warnings"] == [] and report["execution"]["partial"] == []
    # no ad text anywhere: no title, no 40-character slice of any description of the run, is in the report
    async with engine.connect() as c:
        texts = (await c.execute(TEXTS)).all()
        companies = (await c.execute(sa.text("SELECT DISTINCT company_display FROM jobs"))).scalars().all()
    blob = json.dumps(report, ensure_ascii=False)
    assert (
        texts
        and not [t.title for t in texts if t.title in blob]
        and not [
            t.title for t in texts if t.description_text[:40] in blob or t.description_text[-40:] in blob
        ]
    )
    # carry 7: no company names either, neither the jobs' nor the board's slug
    assert companies and not [name for name in companies if name in blob] and DEFAULT_BOARD[1] not in blob


async def test_partial_and_invariant_failures(
    engine,
    current_epoch,
    migrated,
    fake_http,
    fake_embedder,
    collect_deps,
    adapters,
    jsearch_html_adapter,
    monkeypatch,
) -> None:
    # (a) the JSearch cell served the html fixture: a failed('contract') request and a contract_broken source
    def html_jsearch(instance: InstanceRow):
        return jsearch_html_adapter if instance.source_id == "jsearch" else adapters(instance)

    html_deps = make_deps(engine, html_jsearch, embedder=collect_deps.embedder)
    rid = await run_to_the_end(
        engine, html_deps, include_paid=True, queries=(TEST_QUERY,), companies=(), enabled=("jsearch",)
    )
    status, reason, report = await run_end(engine, rid)
    assert (status, reason) == ("partial", "fetch_failed")
    assert report["execution"]["partial"] == ["fetch_failed", "source_contract_broken"]
    assert report["sources"]["jsearch/default"]["requests"] == {"failed": 1}
    assert report["sources"]["jsearch/default"]["health"] == "contract_broken"
    assert report["sources"]["jsearch/default"]["units"] == {"billed": 1, "ambiguous": 0, "not_billed": 0}
    assert "source.contract_broken:instance:" in json.dumps(report["execution"]["alerts"])
    assert await open_alert(engine, "run.partial:collect:fetch_failed") == "warn"
    assert await open_alert(engine, "run.partial:collect:source_contract_broken") == "warn"

    # (b) the embedder down: the run's revisions stay awaiting, the run is partial, I6 counts them
    fake_embedder.down = True
    rid = await run_to_the_end(engine, collect_deps)
    fake_embedder.down = False
    status, reason, report = await run_end(engine, rid)
    awaiting = report["revisions"]["awaiting"]
    assert (status, reason) == ("partial", "awaiting_embedding") and awaiting > 0
    assert report["revisions"]["created"] == awaiting and report["revisions"]["embedded"] == 0
    assert report["execution"]["invariants"]["I6"] == {"ok": False, "detail": {"awaiting": awaiting}}
    assert report["execution"]["partial"] == ["awaiting_embedding"]
    assert await open_alert(engine, "run.partial:collect:awaiting_embedding") == "warn"
    # every other cause's alert is resolved: this run had neither
    assert await open_alert(engine, "run.partial:collect:fetch_failed") is None
    assert await open_alert(engine, "run.partial:collect:source_contract_broken") is None

    # (c) a raw item forced back to `pending` after resolve (the database's own transition check is bypassed
    # by the superuser, as a bug would): I1 fails
    async def force_pending(name: str) -> None:
        if name == "collect.before_finalize":
            await asyncio.to_thread(pg_rows, migrated, "superuser", FORCE_PENDING)

    monkeypatch.setattr(finalize_module, "afault_point", force_pending)
    rid = await run_to_the_end(engine, collect_deps)
    status, reason, report = await run_end(engine, rid)
    assert (status, reason) == ("failed_invariant", "I1")
    assert report["execution"]["invariants"]["I1"]["ok"] is False
    assert report["execution"]["invariants"]["I1"]["detail"]["pending"] == 1
    assert report["execution"]["invariants"]["I1"]["detail"]["mismatched"] == 0  # Σ dispositions still holds
    assert await open_alert(engine, f"run.failed_invariant:run:{rid}") == "critical"
    assert await open_alert(engine, "run.partial:collect:awaiting_embedding") is None  # no such cause now

    # (d) an adjustment row pushing the (Adzuna) window the run touched over its limit: I3 fails
    async def breach(name: str) -> None:
        if name == "collect.before_finalize":
            async with engine.begin() as c:
                await c.execute(BREACH)

    monkeypatch.setattr(finalize_module, "afault_point", breach)
    rid = await run_to_the_end(engine, collect_deps, queries=(TEST_QUERY,), companies=(), enabled=("adzuna",))
    status, reason, report = await run_end(engine, rid)
    assert (status, reason) == ("failed_invariant", "I3")
    (breached,) = report["execution"]["invariants"]["I3"]["detail"]["breaches"]
    assert breached["provider"] == "adzuna" and breached["kind"] == "window"
    assert breached["used"] > breached["limit"]
    assert await open_alert(engine, "quota.limit_breach:provider:adzuna") == "critical"
    assert await open_alert(engine, f"run.failed_invariant:run:{rid}") == "critical"
    monkeypatch.undo()

    # (e) a `deferred('job_fenced')` item does not fail I1: a board job fenced, its posting sighted again
    async with engine.begin() as c:
        job = (await c.execute(BOARD_JOB, {"s": DEFAULT_BOARD[0], "k": DEFAULT_BOARD[1]})).scalar_one()
        await c.execute(FENCE_JOB, {"jobs": [job]})
    rid = await run_to_the_end(engine, collect_deps)
    status, reason, report = await run_end(engine, rid)
    assert report["sources"][await board_key(engine)]["items"].get("deferred") == 1
    assert report["execution"]["invariants"]["I1"]["ok"] is True and status != "failed_invariant"


FORCE_PENDING = (
    "SET session_replication_role = replica; "
    "UPDATE raw_items SET disposition = 'pending', disposed_at = NULL, normalized = '{}', "
    "observation_id = NULL WHERE id = (SELECT i.id FROM raw_items i JOIN raw_responses r "
    "ON r.id = i.raw_response_id JOIN fetch_requests f ON f.id = r.fetch_request_id JOIN runs u "
    "ON u.id = f.run_id WHERE u.status = 'running' AND i.disposition = 'linked' ORDER BY i.id LIMIT 1)"
)
BREACH = sa.text(
    "INSERT INTO quota_ledger (provider, window_start, kind, units, evidence) "
    "SELECT provider, window_start, 'adjustment', limit_units + 1, 'test_breach' FROM quota_windows "
    "WHERE provider = 'adzuna' AND is_current"
)
BOARD_JOB = sa.text(
    "SELECT p.job_id FROM postings p JOIN source_instances i ON i.id = p.source_instance_id "
    "WHERE i.source_id = :s AND i.instance_key = :k ORDER BY p.id LIMIT 1"
)
FENCE_JOB = sa.text(
    "INSERT INTO purge_fences (kind, job_ids, manifest_id) VALUES ('jobs', CAST(:jobs AS bigint[]), 1)"
)


def test_decide_status_order() -> None:
    def ok(c: InvariantCode) -> InvariantResult:
        return InvariantResult(c, True, {})

    def bad(c: InvariantCode) -> InvariantResult:
        return InvariantResult(c, False, {})

    assert decide_status([ok("I1"), bad("I3"), bad("I7")], ["fetch_failed"]) == ("failed_invariant", "I3")
    assert decide_status([ok("I1"), bad("I6")], []) == ("succeeded", None) and decide_status(
        [ok("I1")], ["awaiting_embedding", "fetch_failed"]
    ) == ("partial", "awaiting_embedding")


def runs_total(status: str) -> float:
    return REGISTRY.get_sample_value("jobhunter_runs_total", {"status": status}) or 0.0


ATTEMPTS = sa.text("SELECT attempt_no, end_reason, ended_at IS NOT NULL FROM run_attempts WHERE run_id = :r")
FINISHED = sa.text("SELECT finished_at FROM runs WHERE id = :r")


async def test_execute_run_uses_the_graph_finalize_once(
    engine, current_epoch, fake_http, fake_embedder, collect_deps, monkeypatch
) -> None:
    finalized: list[str] = []
    ends: list[RunEnd] = []
    completions: list[RunCompletion | None] = []
    real_finalize_run, real_observe, real_completion = (
        runs.finalize_run,
        runs.observe_run_ends,
        runs.run_completion,
    )

    async def counted_finalize_run(*args: Any, **kwargs: Any) -> float | None:
        finalized.append(kwargs["status"])
        return await real_finalize_run(*args, **kwargs)

    def observed(run_ends) -> None:
        ends.extend(run_ends)
        real_observe(run_ends)

    async def looked_up(conn, run_id: int, attempt_no: int) -> RunCompletion | None:
        completions.append(await real_completion(conn, run_id, attempt_no))
        return completions[-1]

    for module in (runs, finalize_module):
        monkeypatch.setattr(module, "finalize_run", counted_finalize_run)
    monkeypatch.setattr(runs, "observe_run_ends", observed)  # the node observes through `observe_graph_end`
    monkeypatch.setattr(runs, "run_completion", looked_up)
    succeeded_before = runs_total("succeeded")

    await seed_inputs(engine, companies=(DEFAULT_BOARD,), enabled=("arbeitnow", "ats"))
    rid = await manual_run_id(engine)
    graph = build_collect_graph(InMemorySaver(), collect_deps)
    assert await runs.execute_run(engine, graph, rid, 1, epoch=1) == "started"
    status, reason, report = await run_end(engine, rid)
    assert (status, reason) == ("succeeded", None) and report != {"noop": True} and "execution" in report
    assert finalized == ["succeeded"]  # the graph's finalize, once: execute_run wrote nothing more
    async with engine.connect() as c:
        finished_at = (await c.execute(FINISHED, {"r": rid})).scalar_one()
        assert [tuple(r) for r in await c.execute(ATTEMPTS, {"r": rid})] == [(1, "completed", True)]
    assert completions == [RunCompletion(status="succeeded", reason=None, finished_at=finished_at)]
    (end,) = ends
    assert end.status == "succeeded" and isinstance(end.duration_s, float) and end.duration_s > 0
    assert runs_total("succeeded") == succeeded_before + 1

    # the M0 no-op graph still ends `succeeded` with {"noop": true}: no completion was found after it returned
    noop = await manual_run_id(engine)
    assert (
        await runs.execute_run(engine, build_noop_collect_graph(InMemorySaver()), noop, 1, epoch=1)
        == "started"
    )
    assert (await run_end(engine, noop)) == ("succeeded", None, {"noop": True})
    assert completions[-1] is None and finalized == ["succeeded", "succeeded"]
    assert ends[-1].status == "succeeded" and isinstance(ends[-1].duration_s, float)
    assert runs_total("succeeded") == succeeded_before + 2


async def test_outcome_conflict_ends_failed_invariant_i7(
    engine, current_epoch, fake_http, fake_embedder, collect_deps
) -> None:
    # two primary Sends (Arbeitnow and the board) of one step, each with its own outcome for one work id
    rid = await run_to_the_end(engine, collect_deps, graph=conflicting_graph)
    status, reason, report = await run_end(engine, rid)
    assert (status, reason) == ("failed_invariant", "I7")
    assert report["execution"]["invariants"]["I7"] == {
        "ok": False,
        "detail": {"error_class": "OutcomeConflict"},
    }
    assert await open_alert(engine, f"run.failed_invariant:run:{rid}") == "critical"
    async with engine.connect() as c:
        assert (await c.execute(ANY_ALERT, {"k": f"run.failed:run:{rid}"})).scalar_one() == 0


CELLS = sa.text(
    "SELECT id, last_attempted_at, yield_ema, consecutive_incomplete FROM coverage_cells ORDER BY id"
)


async def test_a_superseded_finalize_writes_nothing(
    engine, current_epoch, queue_app, fake_http, fake_embedder, collect_deps
) -> None:
    """`FencedOut` propagates from the node's finalize: the attempt is lost, and its transaction (cells,
    alerts, the run end) wrote nothing."""
    owner = await queued_run(engine)
    await pending_item(engine, owner, "jsearch", {"native_id": "fenced-1"})
    async with engine.connect() as c:
        cells, alerts = (
            (await c.execute(CELLS)).all(),
            (await c.execute(sa.text("SELECT count(*) FROM alerts"))).scalar_one(),
        )
    newer = await supersede(engine, owner)
    with pytest.raises(FencedOut):
        await finalize_from_graph(collect_deps, owner, initial_state(owner.run_id, owner.attempt_no))
    async with engine.connect() as c:
        assert (await c.execute(CELLS)).all() == cells
        assert (await c.execute(sa.text("SELECT count(*) FROM alerts"))).scalar_one() == alerts
        assert tuple((await c.execute(RUN_END, {"i": owner.run_id})).one()) == ("running", None, None)
    assert newer.attempt_no == owner.attempt_no + 1


async def test_i5_is_evaluated_against_the_runs_own_tuple(engine, current_epoch) -> None:
    """M3 Task 14 (R9): I5 reads the run's tuple (`runs.llm_tuple_id`), never the one active at finalize: a
    run created before any profile was active has none, so I5 is vacuous even once a tuple is active (the
    active tuple's targets are `create_targets_for_tuple`'s and `missing_targets`'). The cases with a tuple
    are `test_enqueue_targets.py`'s."""
    owner = await queued_run(engine)
    async with engine.begin() as c:
        chain = await insert_profile_chain(c)
        await c.execute(
            sa.text("UPDATE active_config SET profile_version_id = :v, llm_tuple_id = :t WHERE id = 1"),
            {"v": chain.profile_version_id, "t": chain.llm_tuple_id},
        )
    async with engine.connect() as c:
        revisions = await run_revisions(c, owner.run_id)
        results = await check_invariants(c, owner.run_id, outcome_conflicts=0, revisions=revisions)
    assert {r.code: r for r in results}["I5"] == InvariantResult("I5", True, {"llm_tuple_id": None})


async def test_check_invariants_counts_a_deferred_item_as_a_disposition(engine, current_epoch) -> None:
    """(e) at the function level: a `deferred('job_fenced')` item of the run keeps I1, and the partial
    conditions of the run are read from its rows."""
    owner = await queued_run(engine)
    first = await pending_item(engine, owner, "jsearch", {"native_id": "H"})
    assert await resolved(engine, owner, first) == "linked"
    async with engine.begin() as c:
        job = (await c.execute(sa.text("SELECT job_id FROM postings WHERE native_id = 'H'"))).scalar_one()
        await c.execute(FENCE_JOB, {"jobs": [job]})
    again = await pending_item(engine, owner, "jsearch", {"native_id": "H"})
    assert await resolved(engine, owner, again) == "deferred"
    async with engine.connect() as c:
        revisions = await run_revisions(c, owner.run_id)
        results = {
            r.code: r
            for r in await check_invariants(c, owner.run_id, outcome_conflicts=0, revisions=revisions)
        }
        assert [r.code for r in results.values()] == ["I1", "I2", "I3", "I4", "I5", "I6", "I7"]
        assert results["I1"].ok and results["I1"].detail == {"responses": 2, "mismatched": 0, "pending": 0}
        assert results["I2"].ok and results["I4"].ok and results["I5"].detail == {"llm_tuple_id": None}
        # the run's one revision awaits its embedding: I6 counts it, and it is the run's one partial cause
        assert results["I6"] == InvariantResult("I6", False, {"awaiting": 1})
        assert await partial_conditions(c, owner.run_id, revisions=revisions) == ["awaiting_embedding"]
        failed = await check_invariants(c, owner.run_id, outcome_conflicts=2, revisions=revisions)
        assert failed[-1] == InvariantResult("I7", False, {"conflicts": 2})


# --- Task 24 (Task 23 review, addendum 10): `run.orphaned_queue` resolves on the next success (§17.6) ------


async def test_a_succeeded_run_resolves_every_orphaned_queue_alert_and_a_partial_one_does_not(
    engine, current_epoch, fake_http, fake_embedder, collect_deps
) -> None:
    from jobhunter.observability.alerts import raise_alert

    keys = [f"{runs.ORPHANED_QUEUE_ALERT}:run:{n}" for n in (900001, 900002)]
    async with engine.begin() as c:
        for key in keys:
            await raise_alert(
                c,
                code=runs.ORPHANED_QUEUE_ALERT,
                severity="warn",
                subject=key.removeprefix(f"{runs.ORPHANED_QUEUE_ALERT}:"),
                message="queued run orphaned",
            )
        # A partial end (or any end but `succeeded`) leaves them open, as it leaves `run.failed` open; the
        # alerts of the end of a run without requests (no such run needs to exist for them).
        await finalize_module._alerts(c, 900003, "partial", [], ["fetch_failed"], fallback=NO_FALLBACK)
    assert [await open_alert(engine, key) for key in keys] == ["warn", "warn"]
    rid = await run_to_the_end(engine, collect_deps)
    assert (await run_end(engine, rid))[0] == "succeeded"
    assert [await open_alert(engine, key) for key in keys] == [None, None]


# --- final review F3, Minor 2: I3 against the per-run cap admitted at reservation ---------------------------

REFINED_LIMIT = 1500  # a fair share of about 12 units per 6 h run: a per-run cap of 25, above the floor of 10
ADMITTED_CAPS = sa.text("SELECT per_run_cap FROM quota_ledger WHERE run_id = :r ORDER BY id")
EXTRA_NORMAL = sa.text(
    "INSERT INTO quota_ledger (provider, window_start, kind, request_class, request_key, attempt_seq, "
    "run_id, run_attempt_no, units, state, per_run_cap) SELECT 'jsearch', window_start, 'request', "
    "'normal', :key, 1, :run, :attempt, 1, 'reserved', 19 FROM quota_windows "
    "WHERE provider = 'jsearch' AND is_current"
)

# A counted normal unit of the run admitted under the per-run cap `:cap` (in id order: admission order).
ADMITTED_NORMAL = sa.text(
    "INSERT INTO quota_ledger (provider, window_start, kind, request_class, request_key, attempt_seq, "
    "run_id, run_attempt_no, units, state, per_run_cap) SELECT 'jsearch', window_start, 'request', "
    "'normal', :key, 1, :run, 1, 1, 'reserved', :cap FROM quota_windows "
    "WHERE provider = 'jsearch' AND is_current"
)


async def i3_of(engine, owner) -> InvariantResult:
    async with engine.connect() as c:
        revisions = await run_revisions(c, owner.run_id)
        results = await check_invariants(c, owner.run_id, outcome_conflicts=0, revisions=revisions)
    return next(r for r in results if r.code == "I3")


async def test_i3_holds_a_run_to_the_per_run_cap_its_reservations_were_admitted_under(
    engine, current_epoch
) -> None:
    """A run spends exactly its per-run cap (25 normal JSearch units); a response then refines the window to
    a longer period (its reset 40 days after the start), which lowers the window's cap to 19. The run kept
    to the cap in force at each reservation, so I3 holds; one normal unit beyond the admitted cap fails it."""
    settings = seeded_run_settings()  # 6 h interval, `paid_max_requests_per_run` auto
    t0 = utcnow()
    window = jsearch_window(t0, limit=REFINED_LIMIT, reserve=0, tokens=24, interval_s=settings.interval_s)
    await insert_window(engine, window)
    owner = await queued_run(engine)
    decisions: list[ledger.Reservation | ledger.Denial] = []
    for n in range(26):
        key = n.to_bytes(32, "big")
        fr = await planned_request(engine, owner, provider="jsearch", key=key, attempt_seq=1)
        async with engine.begin() as c:  # 24 tokens at t0; the 25th unit accrues within the hour
            decisions.append(
                await ledger.reserve(
                    c,
                    "jsearch",
                    owner=owner,
                    request_key=key,
                    attempt_seq=1,
                    request_class="normal",
                    fetch_request_id=fr,
                    settings=settings.quota,
                    interval_s=settings.interval_s,
                    now=t0 if n < 24 else t0 + timedelta(hours=1),
                )
            )
    assert all(isinstance(d, ledger.Reservation) for d in decisions[:25])
    assert decisions[25] == ledger.Denial("paced")  # the 26th: the per-run cap of 25
    async with engine.connect() as c:  # every reservation records the cap it was admitted under
        caps = (await c.execute(ADMITTED_CAPS, {"r": owner.run_id})).scalars().all()
    assert caps == [25] * 25
    last = decisions[24]
    assert isinstance(last, ledger.Reservation)
    refined_at = t0 + timedelta(hours=1)
    async with engine.begin() as c:  # the last response's headers refine the window to a longer period
        await ledger.mark_dispatched(c, last.entry_id, owner)
        await ledger.settle(
            c,
            last.entry_id,
            state="billed",
            evidence=None,
            http_status=200,
            provider_remaining=REFINED_LIMIT - 25,
            settings=settings.quota,
            interval_s=settings.interval_s,
            now=refined_at,
            observation=Observation(
                observed_at=refined_at,
                status=200,
                reset_at=t0 + timedelta(days=40),
                limit=REFINED_LIMIT,
                remaining=REFINED_LIMIT - 25,
            ),
        )
        refined = await current_snapshot(c, "jsearch")
    assert refined is not None and refined.window_end == t0 + timedelta(days=40)
    assert per_run_cap(settings.quota, fair_share(refined, settings.interval_s)) == 19  # below the 25 spent
    assert await i3_of(engine, owner) == InvariantResult("I3", True, {"windows": 1, "breaches": []})
    async with engine.begin() as c:  # a normal unit admitted beyond the cap (a bug): I3 must catch it
        await c.execute(EXTRA_NORMAL, {"key": (99).to_bytes(32, "big"), "run": owner.run_id, "attempt": 1})
    failed = await i3_of(engine, owner)
    assert not failed.ok and failed.detail["breaches"] == [  # the cap in force at the latest admission
        {"provider": "jsearch", "kind": "per_run_cap", "used": 26, "limit": 19}
    ]


async def test_i3_catches_units_admitted_beyond_a_cap_that_a_refinement_lowered(
    engine, current_epoch
) -> None:
    """Fix wave F4 (final review F3, Minor 1): 15 normal units under a cap of 25; a refinement lowers it to
    19; 6 more admitted under 19 (an admission bug) make 21. The largest recorded cap (25) would pass them;
    the cap of the run's latest counted admission (19) is breached."""
    settings = seeded_run_settings()
    window = jsearch_window(
        utcnow(), limit=REFINED_LIMIT, reserve=0, tokens=24, interval_s=settings.interval_s
    )
    await insert_window(engine, window)
    owner = await queued_run(engine)
    async with engine.begin() as c:
        for n in range(21):
            params = {"key": n.to_bytes(32, "big"), "run": owner.run_id, "cap": 25 if n < 15 else 19}
            await c.execute(ADMITTED_NORMAL, params)
    failed = await i3_of(engine, owner)
    assert not failed.ok and failed.detail["breaches"] == [
        {"provider": "jsearch", "kind": "per_run_cap", "used": 21, "limit": 19}
    ]
    async with engine.begin() as c:  # the six later units released (never sent): 15 are within the cap of 25
        await c.execute(
            sa.text(
                "UPDATE quota_ledger SET state = 'not_billed', evidence = 'never_dispatched', "
                "settled_at = now() WHERE run_id = :r AND per_run_cap = 19"
            ),
            {"r": owner.run_id},
        )
    assert (await i3_of(engine, owner)).ok


# --- Final wave F5 (ledger L372): the run end's alerts and the report's availability and updated postings ---

OLD_GAP = sa.text(
    "INSERT INTO coverage_intervals (cell_id, kind, span, gap_status, reason) VALUES (:c, 'gap', "
    "tstzrange(now() - interval '120 days', now() - interval '100 days'), 'open', 'source_failed')"
)


async def test_a_succeeded_end_resolves_run_failed_and_the_fallback_alert_and_abandons_old_gaps(
    engine, current_epoch
) -> None:
    """§17.6: every open `run.failed` resolves on the next success; `quota.fallback_active` resolves when the
    run served no fallback request; a gap older than the ad age is abandoned with `coverage.gap_abandoned`,
    which the run's report lists. A partial end that served a fallback request resolves neither."""
    from jobhunter.observability.alerts import raise_alert
    from jobhunter.quota.planner import FALLBACK_ACTIVE_ALERT

    failed = [f"{runs.FAILED_ALERT}:run:{n}" for n in (900001, 900002)]
    fallback_active = f"{FALLBACK_ACTIVE_ALERT}:provider:serpapi"
    cell = await source_cell(engine, "jsearch")

    async def raise_both() -> None:
        async with engine.begin() as c:
            for key in failed:
                subject = key.removeprefix(f"{runs.FAILED_ALERT}:")
                await raise_alert(
                    c, code=runs.FAILED_ALERT, severity="critical", subject=subject, message="m"
                )
            await raise_alert(
                c, code=FALLBACK_ACTIVE_ALERT, severity="info", subject="provider:serpapi", message="m"
            )

    await raise_both()
    # a partial end that served a fallback request resolves neither
    async with engine.begin() as c:
        served = FallbackRequests(planned=1, served=1)
        await finalize_module._alerts(c, 900003, "partial", [], ["fetch_failed"], fallback=served)
    assert [await open_alert(engine, key) for key in failed] == ["critical", "critical"]
    assert await open_alert(engine, fallback_active) == "info"
    async with engine.begin() as c:
        await c.execute(OLD_GAP, {"c": cell})
    owner = await queued_run(engine)
    deps = make_deps(engine, lambda instance: pytest.fail("no adapter is used"))
    assert (
        await finalize_from_graph(deps, owner, initial_state(owner.run_id, owner.attempt_no)) == "succeeded"
    )
    assert [await open_alert(engine, key) for key in failed] == [None, None]
    assert await open_alert(engine, fallback_active) is None
    abandoned = f"coverage.gap_abandoned:cell:{cell}"
    assert await open_alert(engine, abandoned) == "warn"
    async with engine.connect() as c:
        status = await c.execute(
            sa.text("SELECT gap_status FROM coverage_intervals WHERE cell_id = :c"), {"c": cell}
        )
        assert status.scalar_one() == "abandoned"
    _, _, report = await run_end(engine, owner.run_id)
    assert abandoned in report["execution"]["alerts"]


POSTING_OF = sa.text(
    "SELECT p.id FROM postings p JOIN source_instances i ON i.id = p.source_instance_id "
    "WHERE i.source_id = :s AND p.native_id = :n"
)
SNAPSHOT_OF_RUN = sa.text(
    "INSERT INTO ats_snapshots (source_instance_id, run_id, status, member_count, taken_at, applied_at) "
    "VALUES (:i, :run, 'complete', 1, now(), now()) RETURNING id"
)
MEMBER = sa.text("INSERT INTO ats_snapshot_members (snapshot_id, native_id) VALUES (:s, :n)")


async def report_sources(engine, run_id: int) -> dict[str, Any]:
    async with engine.connect() as c:
        report = await report_module.build_report(
            c,
            run_id,
            invariants=[],
            partial=[],
            revisions=await run_revisions(c, run_id),
            fallback=NO_FALLBACK,
            node_durations={},
            counters={},
            alerts_raised=[],
        )
    sources = report["sources"]
    assert isinstance(sources, dict)
    return sources


async def test_the_report_counts_the_changes_the_run_made_and_the_postings_it_saw_change(
    engine, current_epoch
) -> None:
    """Ledger L372. `availability`: a revival the run sighted, an `unknown` or `closed` posting absent from
    the run's snapshot of its board; never the stale sweep's `→ stale` that happened while the run ran.
    `postings.updated`: a posting the run saw with another content, not one it only sighted again."""
    earlier = await queued_run(engine)
    at = utcnow() - timedelta(hours=6)
    for n, native_id in enumerate(("J1", "J2", "J3")):
        item = await pending_item(engine, earlier, "jsearch", {"native_id": native_id}, fetched_at=at)
        assert await resolved(engine, earlier, item) == "linked", n
    async with engine.begin() as c:
        await runs.finalize_run(c, earlier, status="succeeded", reason=None, report={})
        j1, j2 = [(await c.execute(POSTING_OF, {"s": "jsearch", "n": n})).scalar_one() for n in ("J1", "J2")]
        await transition(c, "postings", j1, ["active"], "stale", reason=CLEAR_REASON)  # before the run
    board = {
        native_id: await make_posting(
            engine,
            source_id="greenhouse",
            native_id=native_id,
            availability="active",
            last_seen_at=at,
            availability_changed_at=at,
        )
        for native_id in ("a", "b", "c", "d")
    }
    run = await queued_run(engine)
    # the run sights J1 with a new title (a revival and an update) and J3 unchanged (neither)
    for native_id, overrides in (("J1", {"title_raw": "Senior Python Developer"}), ("J3", {})):
        item = await pending_item(engine, run, "jsearch", {"native_id": native_id, **overrides})
        assert await resolved(engine, run, item) == "linked"
    await planned_request(engine, run, provider="greenhouse", key=b"board".ljust(32, b"-"), attempt_seq=1)
    async with engine.begin() as c:
        await transition(c, "postings", j2, ["active"], "stale", reason=CLEAR_REASON)  # the sweep, meanwhile
        instance = (await c.execute(BOARD_INSTANCE, {"s": "greenhouse", "k": "default"})).scalar_one()
        snapshot = (await c.execute(SNAPSHOT_OF_RUN, {"i": instance, "run": run.run_id})).scalar_one()
        for member in ("a", "d"):
            await c.execute(MEMBER, {"s": snapshot, "n": member})
        await transition(c, "postings", board["b"], ["active"], "unknown", reason=CLEAR_REASON)
        await transition(c, "postings", board["c"], ["active"], "closed", reason="ats_absent")
        # `d` is a member of the run's snapshot: whatever made it unknown meanwhile, it was not the run
        await transition(c, "postings", board["d"], ["active"], "unknown", reason=CLEAR_REASON)
    sources = await report_sources(engine, run.run_id)
    jsearch = sources["jsearch/default"]
    assert jsearch["availability"] == {"closed|stale|unknown→active": 1}  # J1; J2's `→ stale` is the sweep's
    assert jsearch["postings"] == {"new": 0, "updated": 1}  # J1; J3 was only sighted again
    greenhouse = sources[f"greenhouse/instance:{instance}"]
    assert greenhouse["availability"] == {"active→unknown": 1, "active|unknown→closed": 1}
