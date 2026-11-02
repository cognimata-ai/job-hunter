from typing import NoReturn, cast

import pytest
import sqlalchemy as sa
from jobhunter.db import transitions
from jobhunter.db.transitions import CLEAR_REASON, TRANSITIONS, TransitionSpec, transition
from sqlalchemy.ext.asyncio import AsyncConnection

from tests.support.m3 import CLAIM_FROM, CLAIM_RETURNING, CLAIM_SET_SQL, CLAIM_WHERE


def test_run_and_operation_edges() -> None:
    assert ("succeeded", "running") not in TRANSITIONS["runs"].allowed
    assert ("running", "running") in TRANSITIONS["runs"].allowed
    assert TRANSITIONS["runs"].allowed == {
        ("queued", "running"),
        ("running", "running"),
        ("running", "succeeded"),
        ("running", "partial"),
        ("running", "failed_invariant"),
        ("running", "failed"),
        ("queued", "failed"),
        ("queued", "cancelled"),
        ("running", "cancelled"),
    }
    assert TRANSITIONS["operations"].allowed == {
        ("pending", "running"),
        ("running", "succeeded"),
        ("running", "failed"),
        ("running", "pending"),
        ("pending", "cancelled"),
        ("running", "cancelled"),
    }
    assert TRANSITIONS["operations"].clear_on_exit == {
        "running": ("claim_id", "worker_epoch", "heartbeat_at")
    }


class NoDatabase:
    """A connection that fails the test if the helper touches it."""

    async def execute(self, *args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("transition() reached the database for a call the registry refuses")


NO_DATABASE = cast("AsyncConnection", NoDatabase())


@pytest.mark.parametrize(
    ("table", "froms", "to", "kwargs", "message"),
    [
        ("sessions", ["queued"], "running", {}, "no state machine"),
        ("runs", [], "running", {}, "from_states is empty"),
        ("runs", ["succeeded"], "running", {}, "disallowed transition runs: succeeded -> running"),
        ("runs", ["queued", "succeeded"], "running", {}, "disallowed transition runs: succeeded -> running"),
        ("runs", ["queued"], "queued", {}, "disallowed transition runs: queued -> queued"),
        ("runs", ["zombie"], "running", {}, "disallowed transition runs: zombie -> running"),
        ("runs", ["queued"], "zombie", {}, "disallowed transition runs: queued -> zombie"),
        ("runs", ["queued"], "running", {"owner": {"claim_id": "x"}}, "not ownership columns"),
        (
            "runs",
            ["queued"],
            "running",
            {"owner": {"attempt_no = 1 OR true; --": 1}},
            "not ownership columns",
        ),
        ("operations", ["running"], "succeeded", {"set_": {"claim_id": None}}, "cleared by this transition"),
        ("operations", ["running"], "succeeded", {"set_": {"worker_epoch": 1}}, "cleared by this transition"),
        (
            "operations",
            ["pending", "running"],
            "cancelled",
            {"set_": {"heartbeat_at": None}},
            "cleared by this transition",
        ),
        ("runs", ["running"], "succeeded", {"set_": {"status": "queued"}}, "cannot be set"),
        ("runs", ["running"], "succeeded", {"set_": {"finished_at": None}}, "cannot be set"),
        ("runs", ["running"], "succeeded", {"set_": {"status_reason": "x"}}, "cannot be set"),
        ("runs", ["running"], "succeeded", {"set_": {"id": 2}}, "cannot be set"),
        ("runs", ["running"], "succeeded", {"set_": {"report = '{}', status": "queued"}}, "cannot be set"),
        # Keyed by (provider, window_start), without an id: refused here, not by the database (Ruling P11).
        ("quota_windows", ["validated"], "exhausted_until_reset", {}, r"quota\.windows\.save_window"),
    ],
)
async def test_calls_the_registry_refuses_never_reach_the_database(table, froms, to, kwargs, message) -> None:
    with pytest.raises(ValueError, match=message):
        await transition(NO_DATABASE, table, 1, froms, to, **kwargs)


def test_every_edge_state_that_has_a_column_effect_is_in_an_edge() -> None:
    for spec in TRANSITIONS.values():
        states = {state for edge in spec.allowed for state in edge}
        assert set(spec.changed_at) <= states
        assert set(spec.clear_on_exit) <= states
        assert not spec.settable & {spec.state_col, spec.reason_col, *spec.changed_at.values()}


def test_a_spec_with_a_column_effect_for_a_state_in_no_edge_is_refused() -> None:
    with pytest.raises(ValueError, match="in no allowed edge"):
        TransitionSpec("t", "s", None, {"gone": "at"}, frozenset(), {}, frozenset({("a", "b")}))
    with pytest.raises(ValueError, match="managed by transition"):
        TransitionSpec("t", "s", "r", {}, frozenset(), {}, frozenset({("a", "b")}), settable=frozenset({"r"}))


async def test_a_reason_for_a_table_without_a_reason_column_is_refused(monkeypatch) -> None:
    spec = TransitionSpec("t", "s", None, {}, frozenset(), {}, frozenset({("a", "b")}))
    monkeypatch.setattr(transitions, "TRANSITIONS", {"t": spec})
    with pytest.raises(ValueError, match="t has no reason column"):
        await transition(NO_DATABASE, "t", 1, ["a"], "b", reason="why")
    with pytest.raises(ValueError, match="t has no reason column"):
        await transition(NO_DATABASE, "t", 1, ["a"], "b", reason=CLEAR_REASON)


def test_m1_machines() -> None:
    assert TRANSITIONS["fetch_requests"].allowed == {
        ("planned", "reserved"),
        ("planned", "dispatched"),
        ("planned", "skipped"),
        ("reserved", "dispatched"),
        ("reserved", "skipped"),
        ("dispatched", "succeeded"),
        ("dispatched", "failed"),
        ("dispatched", "ambiguous"),
    }
    assert TRANSITIONS["raw_items"].reason_col == "reason_code"
    assert ("rejected", "pending") in TRANSITIONS["raw_items"].allowed
    assert TRANSITIONS["raw_items"].clear_on_exit == {"rejected": ("disposed_at",)}
    assert "disposed_at" not in TRANSITIONS["raw_items"].settable
    assert ("active", "active") not in TRANSITIONS["postings"].allowed
    assert ("unknown", "closed") in TRANSITIONS["postings"].allowed
    assert TRANSITIONS["quota_ledger"].changed_at == {
        "dispatched": "dispatched_at",
        "billed": "settled_at",
        "not_billed": "settled_at",
        "ambiguous": "settled_at",
    }
    assert TRANSITIONS["coverage_intervals"].state_col == "gap_status"
    assert TRANSITIONS["identity_conflicts"].reason_col is None
    assert TRANSITIONS["quota_windows"].allowed == {  # §7.16.8
        ("extrapolated_unvalidated", "validated"),
        ("extrapolated_unvalidated", "exhausted_until_reset"),
        ("validated", "exhausted_until_reset"),
        ("exhausted_until_reset", "validated"),
    }


def test_registry_imports_without_a_managed_settable_clash() -> None:
    # `raw_items.disposed_at` is a changed-at column (and cleared on leaving `rejected`), never a settable
    # one: the registry imported, so no registered spec names a managed column as settable, because this
    # guard refuses a changed-at column as surely as the reason column.
    assert "disposed_at" in TRANSITIONS["raw_items"].changed_at.values()
    with pytest.raises(ValueError, match=r"\['disposed_at'\] are managed by transition"):
        TransitionSpec(
            "raw_items",
            "disposition",
            "reason_code",
            {"linked": "disposed_at"},
            frozenset(),
            {},
            frozenset({("pending", "linked")}),
            settable=frozenset({"disposed_at"}),
        )


def test_m2_machines() -> None:
    # §7.16.8, verbatim; the draft's owner columns are the two the extraction result is guarded by (Task 13)
    drafts = TRANSITIONS["profile_drafts"]
    assert drafts.allowed == {
        ("uploaded", "extracting_text"),
        ("uploaded", "extracting_profile"),
        ("extracting_text", "text_review"),
        ("extracting_text", "failed"),
        ("text_review", "extracting_profile"),
        ("extracting_profile", "ready"),
        ("extracting_profile", "failed"),
        ("ready", "text_review"),
        ("ready", "confirmed"),
        ("text_review", "discarded"),
        ("ready", "discarded"),
        ("failed", "discarded"),
        ("failed", "extracting_profile"),
    }
    assert ("ready", "ready") not in drafts.allowed  # field edits are a plain UPDATE, not a transition
    assert (drafts.state_col, drafts.reason_col) == ("status", "status_reason")
    assert drafts.changed_at == dict.fromkeys(
        ("extracting_text", "text_review", "extracting_profile", "ready", "failed", "confirmed", "discarded"),
        "status_changed_at",
    )
    assert drafts.owner_cols == {"reviewed_text_sha256", "native_pdf"}
    assert drafts.settable == {
        "reviewed_text",
        "reviewed_text_sha256",
        "native_pdf",
        "transmitted_text_sha256",
        "draft_json",
        "validation_issues",
        "failure_code",
        "llm_call_id",
        "confirmed_profile_version_id",
        "confirm_response",
        "content_purged_at",
        "updated_at",
    }

    # §7.16.7, plus the two edges out of `ambiguous` that a late online settlement uses (Deviation 24)
    ledger = TRANSITIONS["llm_spend_ledger"]
    assert ledger.allowed == {
        ("reserved", "dispatched"),
        ("reserved", "not_billed"),
        ("dispatched", "billed"),
        ("dispatched", "not_billed"),
        ("dispatched", "ambiguous"),
        ("ambiguous", "billed"),
        ("ambiguous", "not_billed"),
    }
    assert (ledger.state_col, ledger.reason_col) == ("state", "evidence")
    assert ledger.changed_at == {
        "dispatched": "dispatched_at",
        "billed": "settled_at",
        "not_billed": "settled_at",
        "ambiguous": "settled_at",
    }
    assert ledger.settable == {"settled_usd"}

    # §7.16 has no `llm_calls` machine: the call follows its ledger entry
    calls = TRANSITIONS["llm_calls"]
    assert calls.allowed == {
        ("reserved", "dispatched"),
        ("reserved", "cancelled"),
        ("dispatched", "completed"),
        ("dispatched", "error"),
        ("dispatched", "ambiguous"),
        ("ambiguous", "completed"),
        ("ambiguous", "error"),
    }
    assert calls.state_col == "status"
    assert calls.settable == {
        "model_served",
        "http_status",
        "error_code",
        "stop_reason",
        "refusal_category",
        "provider_request_id",
        "input_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "output_tokens",
        "cost_usd",
        "output_json",
        "first_event_at",
        "ended_at",
    }

    proposals = TRANSITIONS["query_proposals"]
    assert proposals.allowed == {("running", "ready"), ("running", "failed")}
    assert proposals.state_col == "status"
    assert proposals.settable == {"items", "llm_call_id"}

    runs = TRANSITIONS["eval_runs"]
    assert runs.allowed == {("running", "completed"), ("running", "failed")}
    assert runs.state_col == "status"
    assert runs.changed_at == {"completed": "finished_at", "failed": "finished_at"}
    assert runs.settable == {"passed", "metrics", "cost_usd"}


TARGET_STATES = (
    "pending",
    "filtered_out",
    "deferred_duplicate",
    "budget_deferred",
    "in_progress",
    "in_batch",
    "published_accepted",
    "published_contested",
    "refused",
    "failed_max_tokens",
    "failed_invalid_output",
    "failed_request",
    "failed_transient",
    "failed_attempts_exhausted",
    "expired_unscored",
    "cancelled",
)
# §7.16.3 (L2865-2903), its 49 edges, then Deviation 7's two
TARGET_EDGES = {
    ("pending", "in_progress"),
    ("pending", "in_batch"),
    ("pending", "expired_unscored"),
    ("budget_deferred", "expired_unscored"),
    ("pending", "filtered_out"),
    ("budget_deferred", "filtered_out"),
    ("deferred_duplicate", "filtered_out"),
    ("pending", "deferred_duplicate"),
    ("budget_deferred", "deferred_duplicate"),
    ("filtered_out", "pending"),
    ("deferred_duplicate", "pending"),
    ("pending", "pending"),
    ("budget_deferred", "pending"),
    ("expired_unscored", "pending"),
    ("refused", "pending"),
    ("failed_max_tokens", "pending"),
    ("failed_invalid_output", "pending"),
    ("failed_request", "pending"),
    ("failed_transient", "pending"),
    ("failed_attempts_exhausted", "pending"),
    ("in_progress", "budget_deferred"),
    ("in_progress", "published_accepted"),
    ("in_progress", "published_contested"),
    ("in_progress", "refused"),
    ("in_progress", "failed_max_tokens"),
    ("in_progress", "failed_invalid_output"),
    ("in_progress", "failed_request"),
    ("in_progress", "pending"),
    ("in_progress", "failed_transient"),
    ("in_progress", "failed_attempts_exhausted"),
    ("in_progress", "cancelled"),
    ("in_batch", "published_accepted"),
    ("in_batch", "pending"),
    ("in_batch", "refused"),
    ("in_batch", "failed_request"),
    ("in_batch", "failed_invalid_output"),
    ("in_batch", "cancelled"),
    ("pending", "cancelled"),
    ("budget_deferred", "cancelled"),
    ("deferred_duplicate", "cancelled"),
    ("filtered_out", "cancelled"),
    ("expired_unscored", "cancelled"),
    ("refused", "cancelled"),
    ("failed_max_tokens", "cancelled"),
    ("failed_invalid_output", "cancelled"),
    ("failed_request", "cancelled"),
    ("failed_transient", "cancelled"),
    ("failed_attempts_exhausted", "cancelled"),
    ("cancelled", "pending"),
    # Deviation 7: charged_attempts reached max_score_attempts outside in_progress (the exhaustion sweep)
    ("pending", "failed_attempts_exhausted"),
    ("budget_deferred", "failed_attempts_exhausted"),
}
ATTEMPT_OUTCOMES = (
    "published_accepted",
    "published_contested",
    "refused",
    "failed_max_tokens",
    "failed_invalid_output",
    "failed_request",
    "transient_error",
    "budget_exhausted",
    "claim_lost",
    "cancelled",
    "batch_draft_ready",
    "submit_rejected",
    "released",
)
ITEM_RESULTS = ("succeeded", "errored", "canceled", "expired", "missing")


def test_m3_machines() -> None:
    targets = TRANSITIONS["score_targets"]
    assert len(TARGET_EDGES) == 51
    assert targets.allowed == TARGET_EDGES
    assert {state for edge in targets.allowed for state in edge} == set(TARGET_STATES)
    assert (targets.state_col, targets.reason_col) == ("state", "state_reason")
    assert targets.changed_at == dict.fromkeys(TARGET_STATES, "state_changed_at")
    assert targets.owner_cols == {"claim_id", "attempt_no", "batch_submission_id"}
    assert targets.clear_on_exit == {
        "in_progress": ("claim_id", "claim_epoch", "claim_heartbeat_at"),  # claimed_at is history (R8)
        "in_batch": ("batch_submission_id",),
        "deferred_duplicate": ("deferred_group_id",),
        "filtered_out": ("filter_scope_version_id",),
    }
    assert targets.settable == {
        "claim_id",
        "claim_epoch",
        "claimed_at",
        "claim_heartbeat_at",
        "attempt_no",
        "lane",
        "always_include_reason",
        "batch_submission_id",
        "filter_scope_version_id",
        "deferred_group_id",
        "override",
        "override_at",
        "charged_attempts",
        "not_before",
        "transient_failures",
        "refusal_category",
        "published_result_id",
        "resume_draft_id",
        "bulk_operation_id",
        "updated_at",
    }
    # published states are terminal, and nothing enters `in_batch` or `in_progress` but from `pending`
    assert not {edge for edge in targets.allowed if edge[0].startswith("published_")}
    assert {f for f, t in targets.allowed if t in ("in_progress", "in_batch")} == {"pending"}

    # §7.16 has no attempt machine (A1 issue 15, Deviation 12): `running` ends in one of 13 outcomes
    attempts = TRANSITIONS["score_attempts"]
    assert attempts.allowed == {("running", outcome) for outcome in ATTEMPT_OUTCOMES}
    assert (attempts.state_col, attempts.reason_col) == ("outcome", "outcome_detail")
    assert attempts.changed_at == dict.fromkeys(ATTEMPT_OUTCOMES, "ended_at")
    assert attempts.owner_cols == {"claim_id"}
    assert attempts.clear_on_exit == {}
    assert attempts.settable == {
        "logical_calls",
        "http_calls",
        "charged",
        "cost_usd",
        "claim_id",
        "claim_epoch",
    }

    cycles = TRANSITIONS["score_cycles"]  # L3001
    assert cycles.allowed == {("running", "completed"), ("running", "failed")}
    assert (cycles.state_col, cycles.reason_col) == ("status", "status_reason")
    assert cycles.changed_at == {"completed": "finished_at", "failed": "finished_at"}
    assert cycles.owner_cols == {"worker_epoch"}
    assert cycles.clear_on_exit == {}
    assert cycles.settable == {
        "admission_size",
        "admitted",
        "c_hat_usd",
        "spent_usd",
        "report",
        "heartbeat_at",
    }

    bulk = TRANSITIONS["bulk_operations"]  # L3002
    assert bulk.allowed == {("active", "completed"), ("active", "cancelled")}
    assert (bulk.state_col, bulk.reason_col) == ("state", None)
    assert bulk.changed_at == {"completed": "finished_at", "cancelled": "finished_at"}
    assert bulk.owner_cols == frozenset()
    assert bulk.clear_on_exit == {}
    assert bulk.settable == {"spent_usd", "followup_held_usd"}

    # §7.16.6 (L2948-2962): 11 edges; the self-loops of L2955 and L2958 are plain UPDATEs, not transitions
    submissions = TRANSITIONS["batch_submissions"]
    assert submissions.allowed == {
        ("preparing", "submit_started"),
        ("preparing", "abandoned"),
        ("submit_started", "submitted"),
        ("submit_started", "submit_rejected"),
        ("submit_started", "submit_ambiguous"),
        ("submit_started", "released"),
        ("submit_ambiguous", "ended"),
        ("submit_ambiguous", "released"),
        ("released", "ended"),
        ("submitted", "ended"),
        ("ended", "reconciled"),
    }
    assert ("submit_ambiguous", "submit_ambiguous") not in submissions.allowed
    assert ("released", "released") not in submissions.allowed
    assert (submissions.state_col, submissions.reason_col) == ("state", None)  # reconcile_report is jsonb
    assert submissions.changed_at == {
        "submit_started": "submit_started_at",
        "submitted": "submitted_at",
        "ended": "ended_at",
        "reconciled": "reconciled_at",
        "released": "released_at",
    }
    assert submissions.owner_cols == frozenset()  # the CAS on `state` is the predicate (L303)
    assert submissions.clear_on_exit == {}
    assert submissions.settable == {
        "provider_batch_id",
        "expires_at",
        "cancel_requested_at",
        "results_complete_at",
        "last_reconcile_at",
        "reconcile_report",
        "request_hash",
        "request_count",
        "reserved_usd",
        "followup_held_usd",
    }

    items = TRANSITIONS["batch_items"]
    assert items.allowed == {("awaiting", result) for result in ITEM_RESULTS}
    assert (items.state_col, items.reason_col) == ("result_type", "decision")
    assert items.changed_at == dict.fromkeys(ITEM_RESULTS, "processed_at")
    assert items.owner_cols == frozenset()
    assert items.clear_on_exit == {}
    assert items.settable == {"llm_call_id", "stop_reason"}


class RecordingConnection:
    """A connection that keeps every statement it is given and answers each with no rows."""

    def __init__(self) -> None:
        self.statements: list[object] = []

    async def execute(self, statement: object, *args: object, **kwargs: object) -> "NoRows":
        self.statements.append(statement)
        return NoRows()


class NoRows:
    def mappings(self) -> list[object]:
        return []


CLAIM_PARAMS = {"ids": [1, 2], "lanes": ["similarity", "oldest"], "reasons": [None, None], "epoch": 1}


async def test_transition_where_binds() -> None:
    recorder = RecordingConnection()
    rows = await transitions.transition_where(
        cast("AsyncConnection", recorder),
        "score_targets",
        ["pending"],
        "in_progress",
        where=CLAIM_WHERE,
        from_sql=CLAIM_FROM,
        params={**CLAIM_PARAMS, "max_attempts": 4},
        set_sql=CLAIM_SET_SQL,
        returning=CLAIM_RETURNING,
    )
    assert rows == []
    (statement,) = recorder.statements
    assert isinstance(statement, sa.TextClause)
    binds = set(statement.compile().params)
    # what `_assignments` emits for this edge, compiled as `transition()` compiles it: the to-state, by column
    spec = TRANSITIONS["score_targets"]
    assignments = transitions._assignments(spec, ["pending"], "in_progress", None, frozenset(), {})
    emitted = set(sa.update(transitions._table("score_targets")).values(assignments).compile().params)
    assert emitted == {"state"}
    caller = {"from", "ids", "lanes", "reasons", "epoch", "max_attempts"}
    assert caller.isdisjoint(emitted)
    assert binds == caller | emitted
    assert statement.compile().params["state"] == "in_progress"
    assert statement.compile().params["from"] == ["pending"]
    assert "::" not in statement.text  # every cast is CAST(:name AS type)
    assert statement.text.startswith("UPDATE score_targets AS t SET state=:state, state_changed_at=now(), ")
    assert f" FROM {CLAIM_FROM} WHERE t.state = ANY(:from) AND ({CLAIM_WHERE}) " in statement.text
    assert statement.text.endswith(" RETURNING t.id, t.claim_id, t.attempt_no")


WHERE_ID = {"where": "t.id = :id", "params": {"id": 1}}


@pytest.mark.parametrize(
    ("table", "froms", "to", "kwargs", "message"),
    [
        ("sessions", ["pending"], "in_progress", WHERE_ID, "no state machine"),
        ("quota_windows", ["validated"], "exhausted_until_reset", WHERE_ID, r"quota\.windows\.save_window"),
        ("score_targets", [], "in_progress", WHERE_ID, "from_states is empty"),
        (
            "score_targets",
            ["published_accepted"],
            "in_progress",
            WHERE_ID,
            "disallowed transition score_targets: published_accepted -> in_progress",
        ),
        ("bulk_operations", ["active"], "completed", {**WHERE_ID, "reason": "done"}, "has no reason column"),
        # a column the exit clears, whether set by value or by SQL
        (
            "score_targets",
            ["in_progress"],
            "pending",
            {**WHERE_ID, "set_sql": {"claim_id": "NULL"}},
            r"\['claim_id'\] are cleared by this transition",
        ),
        (
            "score_targets",
            ["pending", "in_batch"],
            "cancelled",
            {**WHERE_ID, "reason": "purged", "set_": {"batch_submission_id": None}},
            "cleared by this transition",
        ),
        ("score_targets", ["pending"], "pending", {**WHERE_ID, "set_sql": {"job_id": "1"}}, "cannot be set"),
        (
            "score_targets",
            ["pending"],
            "pending",
            {**WHERE_ID, "set_sql": {"state_changed_at": "now()"}},
            "cannot be set",
        ),
        (
            "score_targets",
            ["pending"],
            "pending",
            {**WHERE_ID, "set_": {"lane": "score_now"}, "set_sql": {"lane": "'score_now'"}},
            r"\['lane'\] are assigned by both set_ and set_sql",
        ),
        # every :name of where, from_sql and set_sql is a key of params, and every key is one of them
        ("score_targets", ["pending"], "pending", {"where": "t.id = :id", "params": {}}, r"\['id'\]"),
        (
            "score_targets",
            ["pending"],
            "in_progress",
            {"where": CLAIM_WHERE, "from_sql": CLAIM_FROM, "params": CLAIM_PARAMS, "set_sql": CLAIM_SET_SQL},
            r"\['max_attempts'\] have no value in params",
        ),
        (
            "score_targets",
            ["pending"],
            "pending",
            {**WHERE_ID, "set_sql": {"claim_epoch": ":epoch"}},
            r"\['epoch'\] have no value in params",
        ),
        (
            "score_targets",
            ["pending"],
            "pending",
            {"where": "t.id = :id", "params": {"id": 1, "spare": 2}},
            r"\['spare'\] are bound by no :name",
        ),
        # `sa.text` does not read `:ids::bigint[]` as a bind (db/sql.py:25)
        (
            "score_targets",
            ["pending"],
            "pending",
            {"where": "t.id = ANY(:ids::bigint[])", "params": {"ids": [1]}},
            r"CAST\(:ids AS type\)",
        ),
        # a caller bind may not take the name of a bind the helper emits
        (
            "score_targets",
            ["pending"],
            "pending",
            {"where": "t.id = :id AND t.state <> :state", "params": {"id": 1, "state": "x"}},
            r"\['state'\] collide with the binds of the transition",
        ),
        (
            "score_targets",
            ["pending"],
            "pending",
            {"where": "t.id = :id AND :from IS NOT NULL", "params": {"id": 1, "from": 1}},
            r"\['from'\] collide with the binds of the transition",
        ),
        ("score_targets", ["pending"], "pending", {"where": " ", "params": {}}, "where is empty"),
        (
            "score_targets",
            ["pending"],
            "pending",
            {**WHERE_ID, "returning": ("id, state",)},
            "not a column name",
        ),
        ("score_targets", ["pending"], "pending", {**WHERE_ID, "returning": ()}, "returning is empty"),
        # an expression over another table would make SQLAlchemy append its own FROM inside the SET list
        (
            "score_targets",
            ["pending"],
            "pending",
            {**WHERE_ID, "set_": {"lane": sa.table("lanes", sa.column("name")).c.name}},
            r"the value of lane reads \['lanes'\]: name a source relation in from_sql",
        ),
        (
            "score_targets",
            ["pending"],
            "pending",
            {
                **WHERE_ID,
                "set_": {"attempt_no": sa.table("score_targets", sa.column("attempt_no")).c.attempt_no},
            },
            r"the value of attempt_no reads \['score_targets'\]",
        ),
    ],
)
async def test_transition_where_calls_the_registry_refuses_never_reach_the_database(
    table, froms, to, kwargs, message
) -> None:
    with pytest.raises(ValueError, match=message):
        await transitions.transition_where(NO_DATABASE, table, froms, to, **kwargs)


REVIVE_SET_SQL = {
    "override": "'none'",
    "override_at": "NULL",
    "lane": "NULL",
    "bulk_operation_id": "EXCLUDED.bulk_operation_id",
    "updated_at": "now()",
}


def test_transition_set_clause_renders_the_assignments_inline() -> None:
    # §9.7's revive: the to-state, the reason, the changed-at column, then the caller's SQL; no bind of
    # its own
    clause = transitions.transition_set_clause(
        "score_targets", "pending", reason=CLEAR_REASON, set_sql=REVIVE_SET_SQL
    )
    assert clause == (
        "state='pending', state_reason=NULL, state_changed_at=now(), override='none', override_at=NULL, "
        "lane=NULL, bulk_operation_id=EXCLUDED.bulk_operation_id, updated_at=now()"
    )
    with_reason = transitions.transition_set_clause(
        "batch_items", "missing", reason="ignored_fenced", set_sql={}
    )
    assert with_reason == "result_type='missing', decision='ignored_fenced', processed_at=now()"
    # no reason keeps the reason column out of the list; set_sql keeps its own binds
    kept = transitions.transition_set_clause(
        "score_targets", "pending", reason=None, set_sql={"lane": ":lane"}
    )
    assert kept == "state='pending', state_changed_at=now(), lane=:lane"
    assert set(sa.text(f"UPDATE score_targets SET {clause}").compile().params) == set()  # noqa: S608


@pytest.mark.parametrize(
    ("table", "to", "kwargs", "message"),
    [
        ("sessions", "pending", {}, "no state machine"),
        ("quota_windows", "validated", {}, r"quota\.windows\.save_window"),
        ("score_targets", "zombie", {}, "no transition of score_targets enters zombie"),
        ("bulk_operations", "completed", {"reason": "done"}, "has no reason column"),
        ("score_targets", "pending", {"set_sql": {"job_id": "1"}}, "cannot be set"),
        ("score_targets", "pending", {"set_sql": {"state_reason": "NULL"}}, "cannot be set"),
        ("score_targets", "pending", {"set_sql": {"not_before": ":at::timestamptz"}}, r"CAST\(:at AS type\)"),
        # the reason is inlined: a quote would end the literal, and `sa.text` would read `:x` as a bind
        ("score_targets", "cancelled", {"reason": "it's"}, "is not a plain code"),
        ("score_targets", "cancelled", {"reason": "see :x"}, "is not a plain code"),
        ("score_targets", "cancelled", {"reason": "Purged"}, "is not a plain code"),
    ],
)
def test_transition_set_clause_refuses_what_the_registry_refuses(table, to, kwargs, message) -> None:
    arguments = {"reason": None, "set_sql": {}, **kwargs}
    with pytest.raises(ValueError, match=message):
        transitions.transition_set_clause(table, to, **arguments)
