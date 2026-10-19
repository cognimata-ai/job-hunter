import json
import sys
from typing import cast

import pytest

from tests.support.collect import manual_paid_run
from tests.support.pg import PgServer, pg_one, pg_rows
from tests.support.procs import Proc, ProcessGroup, wait_until
from tests.support.sources import load_fixture, served_requests

pytestmark = [pytest.mark.crash, pytest.mark.slow]
WORKER = [sys.executable, "-m", "jobhunter.cli", "worker"]
# Page 1 of the run's only cell, every attempt of it, with its ledger entry (none for a free source).
PAGE1 = (
    "SELECT f.attempt_seq, f.status, l.state, l.request_class FROM fetch_requests f "
    "LEFT JOIN quota_ledger l ON l.id = f.quota_ledger_id WHERE f.run_id = %s AND f.page_no = 1 "
    "ORDER BY f.attempt_seq"
)


def one(server: PgServer, statement: str, *params: object) -> tuple[object, ...] | None:
    return pg_one(server, "superuser", statement, *params)


def die_at(server: PgServer, proc_env, procs: ProcessGroup, fixture_log, point: str) -> Proc:
    """Worker A runs the queued run up to `point` and dies there (exit 86, no cleanup at all)."""
    a = procs.spawn(
        WORKER, proc_env("worker", server, JOBHUNTER_FAULT=point, JOBHUNTER_FIXTURE_LOG=str(fixture_log))
    )
    a.wait_event("fault_point_reached", 180, point=point)
    assert a.exit_within(30) == 86
    # A's job is stalled, as in M0's C27
    one(server, "UPDATE procrastinate_workers SET last_heartbeat = now() - interval '5 minutes'")
    return a


def resume(server: PgServer, proc_env, procs: ProcessGroup, fixture_log, run_id: int) -> Proc:
    """Worker B takes the run over and returns once it ended at attempt 2, `succeeded` or `partial`."""
    b = procs.spawn(WORKER, proc_env("worker", server, JOBHUNTER_FIXTURE_LOG=str(fixture_log)))
    wait_until(
        lambda: (
            one(
                server,
                "SELECT status IN ('succeeded', 'partial'), attempt_no FROM runs WHERE id = %s",
                run_id,
            )
            == (True, 2)
        ),
        180,
    )
    return b


def test_c1_after_reservation_before_marker(migrated, proc_env, procs, fixture_log) -> None:
    run_id = manual_paid_run(migrated, only="jsearch")  # only the source under test (Ruling P5): page 1 first
    die_at(migrated, proc_env, procs, fixture_log, "collect.after_reserve")
    assert pg_rows(migrated, "superuser", PAGE1, run_id) == [
        (1, "reserved", "reserved", "probe")
    ]  # no marker
    resume(migrated, proc_env, procs, fixture_log, run_id)
    assert pg_rows(migrated, "superuser", PAGE1, run_id) == [
        (1, "skipped", "not_billed", "probe"),
        (2, "succeeded", "billed", "probe"),
    ]
    assert one(
        migrated,
        "SELECT f.skip_reason, l.evidence FROM fetch_requests f "
        "JOIN quota_ledger l ON l.id = f.quota_ledger_id "
        "WHERE f.run_id = %s AND f.page_no = 1 AND f.attempt_seq = 1",
        run_id,
    ) == ("superseded", "never_dispatched")
    # B's entry is the probe again: the release restored probe_used, or B's reserve would have been denied
    # awaiting_window_validation
    page1 = [r["case"] for r in served_requests(fixture_log) if r["case"] == "search_v2_page1"]
    assert page1 == ["search_v2_page1"]  # one send across both processes
    assert one(
        migrated,
        "SELECT count(*) FROM raw_responses r JOIN fetch_requests f ON f.id = r.fetch_request_id "
        "WHERE f.run_id = %s AND f.page_no = 1",
        run_id,
    ) == (1,)


def test_c2_after_marker_before_send_returns(migrated, proc_env, procs, fixture_log) -> None:
    run_id = manual_paid_run(migrated, only="jsearch")
    die_at(migrated, proc_env, procs, fixture_log, "collect.after_dispatch_marker")
    resume(migrated, proc_env, procs, fixture_log, run_id)
    # counted, and the paid spec is never re-dispatched
    assert pg_rows(migrated, "superuser", PAGE1, run_id) == [(1, "ambiguous", "ambiguous", "probe")]
    assert one(
        migrated,
        "SELECT i.kind, i.gap_status, i.reason FROM coverage_intervals i JOIN fetch_requests f "
        "ON f.cell_id = i.cell_id WHERE f.run_id = %s AND f.page_no = 1",
        run_id,
    ) == ("gap", "open", "ambiguous_attempt")
    assert one(
        migrated,
        "SELECT severity FROM alerts WHERE dedup_key = 'quota.ambiguous_attempt:provider:jsearch' "
        "AND resolved_at IS NULL",
    ) == ("warn",)
    assert one(
        migrated,
        "SELECT probe_failed, "
        "next_probe_at BETWEEN now() + interval '11 hours' AND now() + interval '12 hours' "
        "FROM quota_windows WHERE provider = 'jsearch' AND is_current",
    ) == (True, True)  # the probe failed: the next one in 12 h
    # A died before its send, B never sent
    assert not [r for r in served_requests(fixture_log) if r["case"] == "search_v2_page1"]


def test_c2b_free_page_after_marker_is_redispatched(migrated, proc_env, procs, fixture_log) -> None:
    """C2's free half, a case of its own (Ruling P5)."""
    run_id = manual_paid_run(migrated, only="arbeitnow")
    die_at(migrated, proc_env, procs, fixture_log, "collect.after_dispatch_marker")
    assert served_requests(fixture_log) == []
    b = resume(migrated, proc_env, procs, fixture_log, run_id)
    # no ledger entry: a free page
    assert pg_rows(migrated, "superuser", PAGE1, run_id) == [
        (1, "ambiguous", None, None),
        (2, "succeeded", None, None),
    ]
    # Ruling T14-a: page 1 re-dispatched once, by B, then the rest of the feed read to its end (page 3 is the
    # fixture routes' `page_end`); A sent nothing
    served = served_requests(fixture_log)
    assert [r["case"] for r in served] == ["page1", "page2", "page_end"]
    assert {int(r["pid"]) for r in served} == {b.pid}


def test_c3_after_response_before_settle(migrated, proc_env, procs, fixture_log) -> None:
    run_id = manual_paid_run(migrated, only="jsearch")
    a = die_at(migrated, proc_env, procs, fixture_log, "collect.after_response")
    resume(migrated, proc_env, procs, fixture_log, run_id)
    # the response is lost, the attempt charged
    assert pg_rows(migrated, "superuser", PAGE1, run_id) == [(1, "ambiguous", "ambiguous", "probe")]
    assert one(
        migrated,
        "SELECT count(*) FROM raw_responses r JOIN fetch_requests f ON f.id = r.fetch_request_id "
        "WHERE f.run_id = %s",
        run_id,
    ) == (0,)
    # A's one send, none by B
    assert [int(r["pid"]) for r in served_requests(fixture_log) if r["case"] == "search_v2_page1"] == [a.pid]


def test_c4_after_settle_before_parse(migrated, proc_env, procs, fixture_log) -> None:
    run_id = manual_paid_run(migrated, only="jsearch")
    a = die_at(migrated, proc_env, procs, fixture_log, "collect.before_parse")
    sent = served_requests(fixture_log)
    b = resume(migrated, proc_env, procs, fixture_log, run_id)
    b.wait_event("page_replayed", 5)
    # B replays the receipts: no new HTTP call
    assert sent and {int(r["pid"]) for r in sent} == {a.pid} and served_requests(fixture_log) == sent
    assert pg_rows(
        migrated,
        "superuser",
        "SELECT f.page_no, l.state, count(DISTINCT r.id) FROM fetch_requests f "
        "JOIN quota_ledger l ON l.id = f.quota_ledger_id JOIN raw_responses r ON r.fetch_request_id = f.id "
        "WHERE f.run_id = %s GROUP BY 1, 2 ORDER BY 1",
        run_id,
    ) == [(1, "billed", 1), (2, "billed", 1)]
    assert one(
        migrated,
        "SELECT count(*) > 0 FROM raw_items i JOIN raw_responses r ON r.id = i.raw_response_id "
        "JOIN fetch_requests f ON f.id = r.fetch_request_id WHERE f.run_id = %s",
        run_id,
    ) == (True,)  # and parses them (Task 15)


def test_c5_between_responses_resumes_without_duplicates(migrated, proc_env, procs, fixture_log) -> None:
    run_id = manual_paid_run(migrated, only="jsearch")  # two pages: A dies after parsing the first
    die_at(migrated, proc_env, procs, fixture_log, "collect.parse.between_responses")
    assert one(
        migrated,
        "SELECT count(*) FILTER (WHERE r.parsed_at IS NULL) FROM raw_responses r "
        "JOIN fetch_requests f ON f.id = r.fetch_request_id WHERE f.run_id = %s AND f.status = 'succeeded'",
        run_id,
    ) == (1,)  # one response left unparsed
    resume(migrated, proc_env, procs, fixture_log, run_id)
    rows = pg_rows(
        migrated,
        "superuser",
        "SELECT r.parsed_at IS NOT NULL, r.item_count, count(i.id), count(DISTINCT i.item_index) "
        "FROM raw_responses r JOIN fetch_requests f ON f.id = r.fetch_request_id "
        "LEFT JOIN raw_items i ON i.raw_response_id = r.id WHERE f.run_id = %s AND f.status = 'succeeded' "
        "GROUP BY r.id",
        run_id,
    )
    # parsed once, no duplicate row, dispositions conserve
    assert len(rows) == 2 and all(parsed and n == items == distinct for parsed, n, items, distinct in rows)


# The dispositions of the run's raw items, by disposition.
ITEMS = (
    "SELECT i.disposition, count(*) FROM raw_items i JOIN raw_responses r ON r.id = i.raw_response_id "
    "JOIN fetch_requests f ON f.id = r.fetch_request_id WHERE f.run_id = %s GROUP BY 1"
)


def dispositions(server: PgServer, run_id: int) -> dict[str, int]:
    """`ITEMS` of the run as `{disposition: count}`."""
    return {str(disposition): cast(int, n) for disposition, n in pg_rows(server, "superuser", ITEMS, run_id)}


def test_c5b_pending_items_are_all_resumed(migrated, proc_env, procs, fixture_log) -> None:
    run_id = manual_paid_run(migrated, only="jsearch")
    die_at(migrated, proc_env, procs, fixture_log, "collect.before_resolve_identity")
    left, sent = dispositions(migrated, run_id), served_requests(fixture_log)
    assert left.get("pending", 0) > 0 and "linked" not in left  # A parsed every page and resolved nothing
    resume(migrated, proc_env, procs, fixture_log, run_id)
    after = dispositions(migrated, run_id)
    # resumed from `normalized`: no re-fetch, no re-parse
    assert (
        "pending" not in after
        and sum(after.values()) == sum(left.values())
        and served_requests(fixture_log) == sent
    )
    # none skipped
    assert (
        sum(after.get(d, 0) - left.get(d, 0) for d in ("linked", "out_of_scope", "rejected", "deferred"))
        == left["pending"]
    )


def test_c6_item_transaction_rolls_back_without_a_partial_posting(
    migrated, proc_env, procs, fixture_log
) -> None:
    run_id = manual_paid_run(migrated, only="jsearch")
    die_at(migrated, proc_env, procs, fixture_log, "collect.identity.in_item_tx")
    # the item's transaction rolled back
    assert one(migrated, "SELECT count(*) FROM postings") == (0,) and "linked" not in dispositions(
        migrated, run_id
    )
    resume(migrated, proc_env, procs, fixture_log, run_id)
    assert "pending" not in dispositions(migrated, run_id)
    # every item linked once, no partial or second posting
    assert one(
        migrated,
        "SELECT (SELECT count(*) FROM postings) = (SELECT count(DISTINCT native_id) FROM raw_items "
        "WHERE disposition = 'linked'), (SELECT count(*) FROM postings) > 0",
    ) == (True, True)


def test_c6b_grouping_resumes_from_occurrences(migrated, proc_env, procs, fixture_log) -> None:
    results = json.loads(load_fixture("adzuna", "adzuna_reingest_pair")[2])["results"]
    pair = (str(results[0]["id"]), str(results[-1]["id"]))  # one ad under two Adzuna ids
    run_id = manual_paid_run(migrated, only="adzuna")
    die_at(migrated, proc_env, procs, fixture_log, "collect.identity.before_grouping")
    # A linked its items and grouped nothing
    assert one(migrated, "SELECT count(*) FROM duplicate_groups") == (0,)
    distinct_jobs = "SELECT count(DISTINCT job_id) FROM postings WHERE native_id IN (%s, %s)"
    assert one(migrated, distinct_jobs, *pair) == (2,)
    resume(migrated, proc_env, procs, fixture_log, run_id)
    groups = pg_rows(
        migrated,
        "superuser",
        "SELECT m.group_id FROM postings p JOIN duplicate_group_members m ON m.job_id = p.job_id "
        "JOIN duplicate_groups g ON g.id = m.group_id WHERE g.status = 'active' AND p.native_id IN (%s, %s)",
        *pair,
    )
    # B grouped them from the occurrences: a linked-but-ungrouped revision is never lost
    assert len(groups) == 2 and len(set(groups)) == 1
