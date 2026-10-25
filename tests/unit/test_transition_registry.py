from typing import NoReturn, cast

import pytest
from jobhunter.db import transitions
from jobhunter.db.transitions import CLEAR_REASON, TRANSITIONS, TransitionSpec, transition
from sqlalchemy.ext.asyncio import AsyncConnection


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
