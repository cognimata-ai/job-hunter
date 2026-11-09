"""The state-machine registry and `transition()`, the one way a stateful row changes state (spec §7.16).

`TRANSITIONS` holds, per table, the allowed edges and the column effects of an edge. `transition()` checks a
call against it and runs a single CAS `UPDATE`: the new state, the reason, the changed-at column and the
cleared ownership columns are all part of that statement, so a row never leaves `running` still owned.

The statement is built with SQLAlchemy Core over a table object made from the registry, aliased `t`. Every
identifier in it (table, state, reason, changed-at, owner, cleared and settable columns) comes from the
registry, never from a caller string: a caller's table, states, owner keys and `set_` keys are looked up in
the registry and refused when they are not there. Caller values are always bound parameters.

`transition_where()` is the set-based form over the same registry and the same assignments (`_assignments`):
one `UPDATE ... AS t` moves every row of the from-states that a caller's `where` selects. Its `where`, its
source relation (`from_sql`) and its SQL assignments (`set_sql`) are static SQL of the caller's module, never
input, with `:name` binds from `params`; every other identifier still comes from the registry.
`transition_set_clause()` renders the same assignments as the `SET` list of an upsert.

The database holds the same edges in `transition_matrix` (written by `seed.sync_transition_matrix` from this
registry), where the `assert_transition` trigger checks them, row by row, as defence in depth.
"""

import enum
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from types import MappingProxyType
from typing import Final

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql.base import PGDialect
from sqlalchemy.engine import RowMapping
from sqlalchemy.engine.interfaces import BindTyping
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.errors import TransitionConflict


@dataclass(frozen=True)
class TransitionSpec:
    """What the registry knows about one table's state machine.

    `changed_at` maps a to-state to the column that records entering it. `owner_cols` are the columns an
    ownership predicate may test. `clear_on_exit` maps a state to the columns set to NULL when a row leaves it
    for another state. `settable` are the other columns a caller may assign in the same update (`set_`); the
    columns the helper manages itself are not among them.
    """

    table: str
    state_col: str
    reason_col: str | None
    changed_at: Mapping[str, str]
    owner_cols: frozenset[str]
    clear_on_exit: Mapping[str, tuple[str, ...]]
    allowed: frozenset[tuple[str, str]]
    settable: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        states = {state for edge in self.allowed for state in edge}
        unknown = (set(self.changed_at) | set(self.clear_on_exit)) - states
        if unknown:
            raise ValueError(f"{self.table}: states {sorted(unknown)} are in no allowed edge")
        managed = {self.state_col, *self.changed_at.values()}
        if self.reason_col is not None:
            managed.add(self.reason_col)
        if clash := self.settable & managed:
            raise ValueError(f"{self.table}: {sorted(clash)} are managed by transition() and not settable")

    @property
    def columns(self) -> frozenset[str]:
        """Every column the helper may name in a statement on this table."""
        named = {"id", self.state_col, *self.changed_at.values(), *self.owner_cols, *self.settable}
        if self.reason_col is not None:
            named.add(self.reason_col)
        for cleared in self.clear_on_exit.values():
            named.update(cleared)
        return frozenset(named)


class _Clear(enum.Enum):
    """The type of `CLEAR_REASON`, a sentinel of one value."""

    CLEAR_REASON = "clear_reason"


# `transition(reason=CLEAR_REASON)` sets the reason column to NULL; `reason=None` leaves it as it is.
CLEAR_REASON: Final = _Clear.CLEAR_REASON

_RUN_TERMINAL: Final = ("succeeded", "partial", "failed_invariant", "failed", "cancelled")

# §7.16.3: the states a user "Retry" returns to `pending` (`refused`, `failed_*`), and those a supersession,
# a merge or a purge cancels outside `in_progress` and `in_batch`.
_TARGET_RETRYABLE: Final = (
    "refused",
    "failed_max_tokens",
    "failed_invalid_output",
    "failed_request",
    "failed_transient",
    "failed_attempts_exhausted",
)
_TARGET_CANCELLABLE: Final = (
    "pending",
    "budget_deferred",
    "deferred_duplicate",
    "filtered_out",
    "expired_unscored",
    *_TARGET_RETRYABLE,
)
# The 49 edges of §7.16.3 (L2865-2903, A1 §3.1's table), then the two of plan Deviation 7.
_TARGET_EDGES: Final = frozenset(
    {
        ("pending", "in_progress"),  # admitted + the atomic claim
        ("pending", "in_batch"),  # prepared into a bulk batch (§10.10)
        *((f, "expired_unscored") for f in ("pending", "budget_deferred")),  # expiry (§9.6.3)
        *((f, "filtered_out") for f in ("pending", "budget_deferred", "deferred_duplicate")),  # refilter
        *((f, "deferred_duplicate") for f in ("pending", "budget_deferred")),  # regroup
        ("filtered_out", "pending"),  # scope flip; "Score anyway"
        ("deferred_duplicate", "pending"),  # split, dissolved, elected; "Score individually"; "Score now"
        ("pending", "pending"),  # "Score now" (L2879)
        ("budget_deferred", "pending"),  # "Score now"; headroom returns (§6.9.4)
        ("expired_unscored", "pending"),  # "Score now"; the expiry condition no longer holds
        *((f, "pending") for f in _TARGET_RETRYABLE),  # "Retry"
        *(
            ("in_progress", t)
            for t in (
                "budget_deferred",
                "published_accepted",
                "published_contested",
                "refused",
                "failed_max_tokens",
                "failed_invalid_output",
                "failed_request",
                "pending",
                "failed_transient",
                "failed_attempts_exhausted",
                "cancelled",
            )
        ),
        *(
            ("in_batch", t)
            for t in (
                "published_accepted",
                "pending",
                "refused",
                "failed_request",
                "failed_invalid_output",
                "cancelled",
            )
        ),
        *((f, "cancelled") for f in _TARGET_CANCELLABLE),  # superseded, merged, purged
        ("cancelled", "pending"),  # the revival (§9.7)
        # Deviation 7 (Codex r1 #21): `charged_attempts` reached `max_score_attempts` outside `in_progress`
        # (R6 charges at `submit_started`, so after a rejected or ambiguous batch, or a budget denial inside
        # a fourth attempt); the exhaustion sweep (Task 13) moves it, reason `attempts_exhausted`.
        *((f, "failed_attempts_exhausted") for f in ("pending", "budget_deferred")),
    }
)
# §7.11: every outcome but `running` ends an attempt.
_ATTEMPT_OUTCOMES: Final = (
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
# §7.12: the provider's per-item result types.
_ITEM_RESULTS: Final = ("succeeded", "errored", "canceled", "expired", "missing")

TRANSITIONS: Final[Mapping[str, TransitionSpec]] = MappingProxyType(
    {
        # §7.16.4. A run keeps its owner columns on exit: its ownership predicate is `attempt_no`.
        "runs": TransitionSpec(
            table="runs",
            state_col="status",
            reason_col="status_reason",
            changed_at=MappingProxyType(
                {"running": "started_at", **dict.fromkeys(_RUN_TERMINAL, "finished_at")}
            ),
            owner_cols=frozenset({"attempt_no"}),
            clear_on_exit=MappingProxyType({}),
            allowed=frozenset(
                {
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
            ),
            settable=frozenset(
                {"attempt_no", "current_thread_id", "resume_count", "heartbeat_at", "report", "llm_tuple_id"}
            ),
        ),
        # §7.16.8. The claim, its epoch and its heartbeat go together on every exit from `running`.
        "operations": TransitionSpec(
            table="operations",
            state_col="status",
            reason_col="last_error_code",
            changed_at=MappingProxyType(
                {
                    "running": "started_at",
                    "succeeded": "finished_at",
                    "failed": "finished_at",
                    "cancelled": "finished_at",
                }
            ),
            owner_cols=frozenset({"claim_id"}),
            clear_on_exit=MappingProxyType({"running": ("claim_id", "worker_epoch", "heartbeat_at")}),
            allowed=frozenset(
                {
                    ("pending", "running"),
                    ("running", "succeeded"),
                    ("running", "failed"),
                    ("running", "pending"),
                    ("pending", "cancelled"),
                    ("running", "cancelled"),
                }
            ),
            settable=frozenset(
                {"claim_id", "worker_epoch", "heartbeat_at", "attempts", "not_before", "progress"}
            ),
        ),
        # The M1 machines have no owner columns: the caller checks the run's ownership predicate with its
        # `runs ... FOR SHARE` (§3.3.3), as the `runs` and `operations` specs check theirs.
        #
        # §7.16.5. Two reason columns, so neither is the registry's: `skip_reason` and `error_code` are
        # settable, and the §7.7 CHECKs make each mandatory in its state (plan Deviation 13).
        "fetch_requests": TransitionSpec(
            table="fetch_requests",
            state_col="status",
            reason_col=None,
            changed_at=MappingProxyType(
                {
                    "dispatched": "dispatched_at",
                    **dict.fromkeys(("succeeded", "failed", "ambiguous", "skipped"), "finished_at"),
                }
            ),
            owner_cols=frozenset(),
            clear_on_exit=MappingProxyType({}),
            allowed=frozenset(
                {
                    ("planned", "reserved"),
                    ("planned", "dispatched"),
                    ("planned", "skipped"),
                    ("reserved", "dispatched"),
                    ("reserved", "skipped"),
                    ("dispatched", "succeeded"),
                    ("dispatched", "failed"),
                    ("dispatched", "ambiguous"),
                }
            ),
            settable=frozenset(
                {
                    "skip_reason",
                    "error_code",
                    "http_status",
                    "quota_ledger_id",
                    "raw_response_id",
                    "continuation",
                    "next_cursor",
                    "items",
                    "new_native_ids",
                    "new_postings",
                }
            ),
        ),
        # §7.16.1. `disposed_at` records leaving `pending` and is cleared on the replay `rejected -> pending`
        # (the §7.7 CHECK `(disposition = 'pending') = (disposed_at IS NULL)`). The replay and
        # `deferred -> linked` pass `reason=CLEAR_REASON`: the CHECK on `reason_code` refuses a kept reason.
        "raw_items": TransitionSpec(
            table="raw_items",
            state_col="disposition",
            reason_col="reason_code",
            changed_at=MappingProxyType(
                dict.fromkeys(("linked", "out_of_scope", "rejected", "deferred"), "disposed_at")
            ),
            owner_cols=frozenset(),
            clear_on_exit=MappingProxyType({"rejected": ("disposed_at",)}),
            allowed=frozenset(
                {
                    ("pending", "linked"),
                    ("pending", "out_of_scope"),
                    ("pending", "rejected"),
                    ("pending", "deferred"),
                    ("deferred", "linked"),
                    ("deferred", "out_of_scope"),
                    ("deferred", "rejected"),
                    ("deferred", "deferred"),
                    ("rejected", "pending"),
                }
            ),
            settable=frozenset({"observation_id", "normalized"}),
        ),
        # §7.16.2. A new posting is inserted `active`; every change of availability is recorded.
        "postings": TransitionSpec(
            table="postings",
            state_col="availability",
            reason_col="availability_reason",
            changed_at=MappingProxyType(
                dict.fromkeys(("active", "stale", "unknown", "closed"), "availability_changed_at")
            ),
            owner_cols=frozenset(),
            clear_on_exit=MappingProxyType({}),
            allowed=frozenset(
                {
                    ("stale", "active"),
                    ("unknown", "active"),
                    ("closed", "active"),
                    ("active", "stale"),
                    ("active", "unknown"),
                    ("active", "closed"),
                    ("unknown", "closed"),
                }
            ),
            settable=frozenset({"absent_snapshots", "first_absent_at", "last_absent_snapshot_id"}),
        ),
        # §7.16.7, quota providers: `billed`, `not_billed` and `ambiguous` are terminal.
        "quota_ledger": TransitionSpec(
            table="quota_ledger",
            state_col="state",
            reason_col="evidence",
            changed_at=MappingProxyType(
                {
                    "dispatched": "dispatched_at",
                    **dict.fromkeys(("billed", "not_billed", "ambiguous"), "settled_at"),
                }
            ),
            owner_cols=frozenset(),
            clear_on_exit=MappingProxyType({}),
            allowed=frozenset(
                {
                    ("reserved", "dispatched"),
                    ("reserved", "not_billed"),
                    ("dispatched", "billed"),
                    ("dispatched", "not_billed"),
                    ("dispatched", "ambiguous"),
                }
            ),
            settable=frozenset({"http_status", "provider_remaining", "window_start"}),
        ),
        # §7.16.8. Representative changes stay `active` and are not transitions (§8.7).
        "duplicate_groups": TransitionSpec(
            table="duplicate_groups",
            state_col="status",
            reason_col="election_reason",
            changed_at=MappingProxyType({"dissolved": "updated_at"}),
            owner_cols=frozenset(),
            clear_on_exit=MappingProxyType({}),
            allowed=frozenset({("active", "dissolved")}),
            settable=frozenset({"representative_job_id", "unscorable_reason", "elected_at"}),
        ),
        # §7.16.8, a user action.
        "identity_conflicts": TransitionSpec(
            table="identity_conflicts",
            state_col="status",
            reason_col=None,
            changed_at=MappingProxyType(
                dict.fromkeys(("resolved_separate", "resolved_merged"), "resolved_at")
            ),
            owner_cols=frozenset(),
            clear_on_exit=MappingProxyType({}),
            allowed=frozenset({("open", "resolved_separate"), ("open", "resolved_merged")}),
        ),
        # §7.16.8, gaps only (a `covered` row has no gap status).
        "coverage_intervals": TransitionSpec(
            table="coverage_intervals",
            state_col="gap_status",
            reason_col="reason",
            changed_at=MappingProxyType(dict.fromkeys(("closed", "abandoned"), "closed_at")),
            owner_cols=frozenset(),
            clear_on_exit=MappingProxyType({}),
            allowed=frozenset({("open", "closed"), ("open", "abandoned")}),
            settable=frozenset({"gap_cursor"}),
        ),
        # §7.16.8. `ready -> ready` (field edits) is a plain UPDATE of `draft_json`, not a transition. The two
        # owner columns guard the extraction result: the `draft` node writes it only for the text (or the
        # native-PDF choice) it started with (`transition(owner={...})`, Task 13).
        "profile_drafts": TransitionSpec(
            table="profile_drafts",
            state_col="status",
            reason_col="status_reason",
            changed_at=MappingProxyType(
                dict.fromkeys(
                    (
                        "extracting_text",
                        "text_review",
                        "extracting_profile",
                        "ready",
                        "failed",
                        "confirmed",
                        "discarded",
                    ),
                    "status_changed_at",
                )
            ),
            owner_cols=frozenset({"reviewed_text_sha256", "native_pdf"}),
            clear_on_exit=MappingProxyType({}),
            allowed=frozenset(
                {
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
            ),
            settable=frozenset(
                {
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
            ),
        ),
        # §7.16.7, LLM entries: `billed` and `not_billed` are terminal. The two edges out of `ambiguous` are
        # M3's batch items found later and M2's late online settlement (plan Deviation 24).
        "llm_spend_ledger": TransitionSpec(
            table="llm_spend_ledger",
            state_col="state",
            reason_col="evidence",
            changed_at=MappingProxyType(
                {
                    "dispatched": "dispatched_at",
                    **dict.fromkeys(("billed", "not_billed", "ambiguous"), "settled_at"),
                }
            ),
            owner_cols=frozenset(),
            clear_on_exit=MappingProxyType({}),
            allowed=frozenset(
                {
                    ("reserved", "dispatched"),
                    ("reserved", "not_billed"),
                    ("dispatched", "billed"),
                    ("dispatched", "not_billed"),
                    ("dispatched", "ambiguous"),
                    ("ambiguous", "billed"),
                    ("ambiguous", "not_billed"),
                }
            ),
            settable=frozenset({"settled_usd"}),
        ),
        # §7.11. §7.16 has no `llm_calls` machine: a call follows its ledger entry. No reason column
        # (`error_code` and `refusal_category` are response columns) and no changed-at column:
        # `first_event_at` and `ended_at` are settable like the rest of the response.
        # `ambiguous -> completed | error` is a late online settlement (plan Deviation 24).
        "llm_calls": TransitionSpec(
            table="llm_calls",
            state_col="status",
            reason_col=None,
            changed_at=MappingProxyType({}),
            owner_cols=frozenset(),
            clear_on_exit=MappingProxyType({}),
            allowed=frozenset(
                {
                    ("reserved", "dispatched"),
                    ("reserved", "cancelled"),
                    ("dispatched", "completed"),
                    ("dispatched", "error"),
                    ("dispatched", "ambiguous"),
                    ("ambiguous", "completed"),
                    ("ambiguous", "error"),
                }
            ),
            settable=frozenset(
                {
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
            ),
        ),
        # §7.5. The `purged` status (items removed, §20.2) has no edge in this milestone.
        "query_proposals": TransitionSpec(
            table="query_proposals",
            state_col="status",
            reason_col=None,
            changed_at=MappingProxyType({}),
            owner_cols=frozenset(),
            clear_on_exit=MappingProxyType({}),
            allowed=frozenset({("running", "ready"), ("running", "failed")}),
            settable=frozenset({"items", "llm_call_id"}),
        ),
        # §7.13. §7.16 has no `eval_runs` machine either: a run is `running` until it finishes or fails.
        "eval_runs": TransitionSpec(
            table="eval_runs",
            state_col="status",
            reason_col=None,
            changed_at=MappingProxyType(dict.fromkeys(("completed", "failed"), "finished_at")),
            owner_cols=frozenset(),
            clear_on_exit=MappingProxyType({}),
            allowed=frozenset({("running", "completed"), ("running", "failed")}),
            settable=frozenset({"passed", "metrics", "cost_usd"}),
        ),
        # §7.16.8. Keyed by `(provider, window_start)` and without an `id`, so `transition()` cannot update
        # it: `windows.save_window` checks its edge against this entry, and the trigger checks it again. A
        # closed window never changes state again.
        "quota_windows": TransitionSpec(
            table="quota_windows",
            state_col="state",
            reason_col=None,
            changed_at=MappingProxyType({}),
            owner_cols=frozenset(),
            clear_on_exit=MappingProxyType({}),
            allowed=frozenset(
                {
                    ("extrapolated_unvalidated", "validated"),
                    ("extrapolated_unvalidated", "exhausted_until_reset"),
                    ("validated", "exhausted_until_reset"),
                    ("exhausted_until_reset", "validated"),
                }
            ),
        ),
        # §7.16.3. Every exit clears what only its state may hold (the §7.11 CHECKs pair each with its
        # state); `claimed_at` is kept as history (R8). Every edge that sets `override` sets `override_at` in
        # the same `set_` (`CHECK ((override = 'none') = (override_at IS NULL))`), and the revival sets both
        # back to `'none'` / `NULL`.
        "score_targets": TransitionSpec(
            table="score_targets",
            state_col="state",
            reason_col="state_reason",
            changed_at=MappingProxyType(
                dict.fromkeys(sorted({to for _, to in _TARGET_EDGES}), "state_changed_at")
            ),
            owner_cols=frozenset({"claim_id", "attempt_no", "batch_submission_id"}),
            clear_on_exit=MappingProxyType(
                {
                    "in_progress": ("claim_id", "claim_epoch", "claim_heartbeat_at"),
                    "in_batch": ("batch_submission_id",),
                    "deferred_duplicate": ("deferred_group_id",),
                    "filtered_out": ("filter_scope_version_id",),
                }
            ),
            allowed=_TARGET_EDGES,
            settable=frozenset(
                {
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
            ),
        ),
        # §7.11. §7.16 has no attempt machine, as it has none for `llm_calls` (A1 issue 15, plan Deviation
        # 12): an attempt is `running` until it ends in one of its outcomes.
        "score_attempts": TransitionSpec(
            table="score_attempts",
            state_col="outcome",
            reason_col="outcome_detail",
            changed_at=MappingProxyType(dict.fromkeys(_ATTEMPT_OUTCOMES, "ended_at")),
            owner_cols=frozenset({"claim_id"}),
            clear_on_exit=MappingProxyType({}),
            allowed=frozenset(("running", outcome) for outcome in _ATTEMPT_OUTCOMES),
            settable=frozenset(
                {"logical_calls", "http_calls", "charged", "cost_usd", "claim_id", "claim_epoch"}
            ),
        ),
        # §7.16.8 (L3001). A cycle's owner is its worker epoch.
        "score_cycles": TransitionSpec(
            table="score_cycles",
            state_col="status",
            reason_col="status_reason",
            changed_at=MappingProxyType(dict.fromkeys(("completed", "failed"), "finished_at")),
            owner_cols=frozenset({"worker_epoch"}),
            clear_on_exit=MappingProxyType({}),
            allowed=frozenset({("running", "completed"), ("running", "failed")}),
            settable=frozenset(
                {"admission_size", "admitted", "c_hat_usd", "spent_usd", "report", "heartbeat_at"}
            ),
        ),
        # §7.16.8 (L3002).
        "bulk_operations": TransitionSpec(
            table="bulk_operations",
            state_col="state",
            reason_col=None,
            changed_at=MappingProxyType(dict.fromkeys(("completed", "cancelled"), "finished_at")),
            owner_cols=frozenset(),
            clear_on_exit=MappingProxyType({}),
            allowed=frozenset({("active", "completed"), ("active", "cancelled")}),
            settable=frozenset({"spent_usd", "followup_held_usd"}),
        ),
        # §7.16.6 (L2948-2962). The CAS on `state` is the ownership predicate (L303), so there are no owner
        # columns. No reason column: `reconcile_report` is jsonb and written by `set_` (A1 issue 14).
        # `submit_rejected`, `submit_ambiguous` and `abandoned` record no time. The self-loops of L2955 and
        # L2958 are plain UPDATEs of `last_reconcile_at` and `reconcile_report`, not transitions. M4 adds the
        # lost-batch release (plan M4 Task 16, U3): `submitted → released` once its retrieval keeps failing
        # past `expires_at + 1 h` (`scoring.batches.RELEASABLE_SQL`).
        "batch_submissions": TransitionSpec(
            table="batch_submissions",
            state_col="state",
            reason_col=None,
            changed_at=MappingProxyType(
                {
                    "submit_started": "submit_started_at",
                    "submitted": "submitted_at",
                    "ended": "ended_at",
                    "reconciled": "reconciled_at",
                    "released": "released_at",
                }
            ),
            owner_cols=frozenset(),
            clear_on_exit=MappingProxyType({}),
            allowed=frozenset(
                {
                    ("preparing", "submit_started"),
                    ("preparing", "abandoned"),
                    ("submit_started", "submitted"),
                    ("submit_started", "submit_rejected"),
                    ("submit_started", "submit_ambiguous"),
                    ("submit_started", "released"),
                    ("submit_ambiguous", "ended"),
                    ("submit_ambiguous", "released"),
                    ("submitted", "released"),
                    ("released", "ended"),
                    ("submitted", "ended"),
                    ("ended", "reconciled"),
                }
            ),
            settable=frozenset(
                {
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
            ),
        ),
        # §7.12. The decision is the reason, and every edge passes one: the CHECK
        # `(processed_at IS NULL) = (decision IS NULL)` pairs it with the changed-at column.
        "batch_items": TransitionSpec(
            table="batch_items",
            state_col="result_type",
            reason_col="decision",
            changed_at=MappingProxyType(dict.fromkeys(_ITEM_RESULTS, "processed_at")),
            owner_cols=frozenset(),
            clear_on_exit=MappingProxyType({}),
            allowed=frozenset(("awaiting", result) for result in _ITEM_RESULTS),
            settable=frozenset({"llm_call_id", "stop_reason"}),
        ),
    }
)


# Registered machines whose rows `transition()` cannot address (no `id`), with the function that writes them.
_WRITTEN_ELSEWHERE: Final[Mapping[str, str]] = MappingProxyType(
    {"quota_windows": "jobhunter.quota.windows.save_window"}
)


@cache
def _table(name: str) -> sa.Alias:
    """A lightweight table object over the registry columns of `name`, aliased `t` as every statement of this
    module names it (only registered names get here)."""
    spec = TRANSITIONS[name]
    columns = (
        sa.column(column, sa.BigInteger) if column == "id" else sa.column(column)
        for column in sorted(spec.columns)
    )
    # `sa.alias` is typed as returning a `NamedFromClause`; a table's alias is an `Alias`, which `sa.update`
    # takes.
    return sa.alias(sa.table(spec.table, *columns), "t")  # type: ignore[return-value]


def _spec(table: str) -> TransitionSpec:
    """The registry entry of `table`, whose rows this module updates by `id`; else `ValueError`."""
    spec = TRANSITIONS.get(table)
    if spec is None:
        raise ValueError(f"unknown table {table!r}: it has no state machine")
    if (writer := _WRITTEN_ELSEWHERE.get(table)) is not None:
        raise ValueError(
            f"{table} has no id, so transition() cannot update it: its state is written by {writer}"
        )
    return spec


def _bound(value: object) -> object:
    """`value` for the SET clause: SQL expressions as they are, JSON containers as JSONB, the rest bound."""
    if isinstance(value, sa.ClauseElement):
        return value
    if isinstance(value, Mapping):
        return sa.literal(dict(value), JSONB)
    if isinstance(value, list):
        return sa.literal(value, JSONB)
    return value


def _validate(
    spec: TransitionSpec,
    from_states: Sequence[str],
    to_state: str,
    reason: str | _Clear | None,
    owner: Mapping[str, object],
    set_: Mapping[str, object],
) -> frozenset[str]:
    """Check a call against the registry; return the columns the exit from `from_states` clears."""
    if not from_states:
        raise ValueError(f"{spec.table}: from_states is empty")
    for from_state in from_states:
        # A from-state that appears in no edge is not a state of the table: it is refused like any other edge.
        if (from_state, to_state) not in spec.allowed:
            raise ValueError(f"disallowed transition {spec.table}: {from_state} -> {to_state}")
    if reason is not None and spec.reason_col is None:
        raise ValueError(f"{spec.table} has no reason column")
    if foreign := set(owner) - spec.owner_cols:
        raise ValueError(f"{spec.table}: {sorted(foreign)} are not ownership columns")
    cleared = frozenset(
        column
        for from_state in from_states
        if from_state != to_state
        for column in spec.clear_on_exit.get(from_state, ())
    )
    if overlap := set(set_) & cleared:
        raise ValueError(f"{spec.table}: {sorted(overlap)} are cleared by this transition and cannot be set")
    if unsettable := set(set_) - spec.settable:
        raise ValueError(f"{spec.table}: {sorted(unsettable)} cannot be set by a transition")
    return cleared


def _assignments(
    spec: TransitionSpec,
    from_states: Sequence[str],
    to_state: str,
    reason: str | _Clear | None,
    cleared: frozenset[str],
    set_: Mapping[str, object],
) -> dict[str, object]:
    """The SET clause of an edge that `_validate` accepted, column by column, over `_table(spec.table)`.

    - The new state, and the reason when `reason` is given: a text, or `CLEAR_REASON` for `NULL`. Without one
      (`None`) the reason column keeps whatever it held.
    - The changed-at column of `to_state`, unless the row is already in `to_state` (a self-edge keeps the
      time of the first entry).
    - `NULL` for the `cleared` columns.
    - The `set_` columns. A value that is a SQL expression is rendered as it is, a mapping or a list is bound
      as JSONB, anything else as an ordinary parameter.

    A plain value is bound under the name of its column when the statement is compiled.
    """
    target = _table(spec.table)
    state = target.c[spec.state_col]
    assignments: dict[str, object] = {spec.state_col: to_state}
    if reason is not None and spec.reason_col is not None:
        assignments[spec.reason_col] = sa.null() if reason is CLEAR_REASON else reason
    changed_at = spec.changed_at.get(to_state)
    if changed_at is not None:
        assignments[changed_at] = (
            sa.case((state == to_state, target.c[changed_at]), else_=sa.func.now())
            if to_state in from_states
            else sa.func.now()
        )
    assignments.update(dict.fromkeys(sorted(cleared), sa.null()))
    assignments.update({column: _bound(value) for column, value in set_.items()})
    return assignments


async def transition(
    conn: AsyncConnection,
    table: str,
    row_id: int,
    from_states: Sequence[str],
    to_state: str,
    *,
    reason: str | _Clear | None = None,
    owner: Mapping[str, object] | None = None,
    set_: Mapping[str, object] | None = None,
) -> None:
    """Move row `row_id` of `table` from one of `from_states` to `to_state`, or raise.

    One `UPDATE <table> AS t SET <assignments> WHERE t.id = :id AND t.<state> = ANY(:from)
    [AND t.<owner col> = :owner ...] RETURNING t.id`, with the assignments of `_assignments`: the new state,
    the reason when one is given, the changed-at column of `to_state` (kept on a self-edge), `NULL` for the
    ownership columns that `clear_on_exit` lists for every state of `from_states` other than `to_state`, and
    the `set_` columns.

    A self-edge (for example `running -> running`) is exclusive only when `owner` is given: without an owner
    predicate, two callers that both name `running` as the from-state both match, and the last one wins. A
    caller that needs exclusivity (a takeover that must beat its rival) passes the owner.

    Raises `ValueError`, before touching the database, for a table or an edge the registry does not know, a
    table whose rows have no `id` (`quota_windows`: `windows.save_window` writes its state), an owner key that
    is not an ownership column, and a `set_` key that is not settable or that the transition clears. Raises
    `TransitionConflict` when no row matched: it is absent, in another state, or owned by another attempt or
    claim. The caller decides what that means.
    """
    spec = _spec(table)
    owner = owner or {}
    set_ = set_ or {}
    cleared = _validate(spec, from_states, to_state, reason, owner, set_)

    target = _table(spec.table)
    statement = (
        sa.update(target)
        .where(
            target.c.id == row_id,
            target.c[spec.state_col] == sa.any_(sa.literal(list(from_states), ARRAY(sa.Text))),
            # The owner value keeps no type of its own, so the database reads it as the column's type.
            *(
                target.c[column] == sa.bindparam(None, value, type_=sa.types.NullType())
                for column, value in owner.items()
            ),
        )
        .values(_assignments(spec, from_states, to_state, reason, cleared, set_))
        .returning(target.c.id)
    )
    if (await conn.execute(statement)).first() is None:
        raise TransitionConflict(table, row_id, to_state)


class _TextDialect(PGDialect):
    """PostgreSQL rendering with no bind casts, for SQL that is read back by `sa.text`.

    `sa.text` does not recognise `:name::type` as a bind (`db/sql.py`), and the PostgreSQL compiler writes a
    JSONB bind as `:name::JSONB`; without the casts every bind is a plain `:name`. The types stay on the
    bind parameters, so the executing dialect still processes each value as its type.
    """

    bind_typing = BindTyping.NONE


_TEXT_DIALECT: Final = _TextDialect(paramstyle="named")  # type: ignore[no-untyped-call]  # PGDialect.__init__
# A `:name` that `sa.text` reads as a bind: not after a colon, a word character or a backslash. Unlike the
# pattern of `sa.text`, it also matches a name followed by `:`, which is how `:name::type` is found.
_BIND: Final = re.compile(r"(?<![:\w\\]):(\w+)")
_IDENTIFIER: Final = re.compile(r"[a-z_][a-z0-9_]*")
# The bind of the from-states in `transition_where`'s WHERE clause.
_FROM_BIND: Final = "from"


def _sql_binds(table: str, fragments: Sequence[str]) -> set[str]:
    """The `:name` binds of the static SQL `fragments`; `ValueError` for a `:name::type` cast."""
    names: set[str] = set()
    for fragment in fragments:
        for match in _BIND.finditer(fragment):
            name = match.group(1)
            if fragment.startswith(":", match.end()):
                raise ValueError(
                    f"{table}: write CAST(:{name} AS type), not :{name}::type, which sa.text does not bind"
                )
            names.add(name)
    return names


def _set_list(
    spec: TransitionSpec,
    assignments: Mapping[str, object],
    set_sql: Mapping[str, str],
    *,
    literal_binds: bool = False,
) -> tuple[str, list[sa.BindParameter[object]]]:
    """The SET list (`column=<expression>, ...`) of `assignments` and then of the static `set_sql`, in that
    order, as SQL for `sa.text`, and the bind parameters it names, each under the name it is rendered with
    (none with `literal_binds`, which renders every value inline).

    An assignment that reads another table is refused: SQLAlchemy would append its own `FROM <that table>`
    to the statement, inside the list taken here. A source relation is the caller's `from_sql`, and its
    columns are read in `set_sql`.
    """
    target = _table(spec.table)
    for column, value in assignments.items():
        if isinstance(value, sa.ColumnElement):
            if read := [str(source) for source in sa.select(value).get_final_froms() if source is not target]:
                raise ValueError(
                    f"{spec.table}: the value of {column} reads {read}: name a source relation in from_sql "
                    "and read its columns in set_sql"
                )
    values = {**assignments, **{column: sa.literal_column(sql) for column, sql in set_sql.items()}}
    update = sa.update(target).ordered_values(*values.items())
    compiled = update.compile(dialect=_TEXT_DIALECT, compile_kwargs={"literal_binds": literal_binds})
    binds = [
        sa.bindparam(name, compiled.params[name], type_=bind.type)
        for bind, name in compiled.bind_names.items()
    ]
    # `UPDATE <table> AS t SET <list>`: with no other table read, the list is what follows the first " SET ".
    return str(compiled).partition(" SET ")[2], binds


async def transition_where(
    conn: AsyncConnection,
    table: str,
    from_states: Sequence[str],
    to_state: str,
    *,
    where: str,
    params: Mapping[str, object],
    reason: str | _Clear | None = None,
    set_: Mapping[str, object] | None = None,
    set_sql: Mapping[str, str] | None = None,
    from_sql: str | None = None,
    returning: Sequence[str] = ("id",),
) -> list[RowMapping]:
    """Move every row of `table` in one of `from_states` that `where` selects to `to_state`; return them.

    The set-based form of `transition()` over the same registry and the same assignments (plan R3):

        UPDATE <table> AS t SET <assignments>, <set_sql> [FROM <from_sql>]
         WHERE t.<state> = ANY(:from) AND (<where>) RETURNING t.<returning>, ...

    - `where` is static SQL of the caller's module (never input) over `t`, with `:name` binds from `params`.
      A row lock is the caller's to write, as a subquery in `where`:
      `t.id IN (SELECT id FROM <table> WHERE ... FOR UPDATE SKIP LOCKED)` (the atomic claim, §9.4).
    - `from_sql` is a static source relation with its own alias, for example
      `unnest(CAST(:ids AS bigint[]), CAST(:lanes AS text[])) AS a(id, lane)`; its binds come from `params`.
    - `set_sql` maps a settable column to a static SQL expression over `t` and the source
      (`gen_random_uuid()`, `t.attempt_no + 1`, `a.lane`, `:epoch`); `set_` maps one to a value, as in
      `transition()`.
    - `returning` names columns of `t`.

    A cast is written `CAST(:name AS type)`: `sa.text` does not recognise `:name::type` as a bind. The
    assignments bind the to-state, a text reason and the `set_` values under their column names, and the
    from-states as `:from`; a key of `params` may not take one of those names.

    Raises `ValueError`, before touching the database, for everything `transition()` refuses (an unknown
    table or edge, a reason without a reason column, a column that is not settable or that the transition
    clears, whether in `set_` or in `set_sql`), a column in both `set_` and `set_sql`, an empty `where`, a
    `:name` of `where`, `from_sql` or `set_sql` that `params` does not give and a key of `params` that none of
    them binds, a `:name::type` cast, a bind name that collides with the assignments', and a `returning` name
    that is not a column name. Zero rows is not an error: the caller reads the list. The `assert_transition`
    trigger still checks every row.
    """
    spec = _spec(table)
    set_ = set_ or {}
    set_sql = set_sql or {}
    if both := set(set_) & set(set_sql):
        raise ValueError(f"{table}: {sorted(both)} are assigned by both set_ and set_sql")
    cleared = _validate(spec, from_states, to_state, reason, {}, {**set_, **set_sql})
    if not where.strip():
        raise ValueError(f"{table}: where is empty (a predicate that selects every row is written `true`)")
    if not returning:
        raise ValueError(f"{table}: returning is empty")
    if odd := [name for name in returning if not _IDENTIFIER.fullmatch(name)]:
        raise ValueError(f"{table}: {odd} in returning are not a column name")
    fragments = [where, *([from_sql] if from_sql is not None else []), *set_sql.values()]
    named = _sql_binds(table, fragments)
    if missing := named - set(params):
        raise ValueError(f"{table}: {sorted(missing)} have no value in params")
    if spare := set(params) - named:
        raise ValueError(f"{table}: {sorted(spare)} are bound by no :name of where, from_sql or set_sql")

    assignments = _assignments(spec, from_states, to_state, reason, cleared, set_)
    set_list, emitted = _set_list(spec, assignments, set_sql)
    if collide := set(params) & {_FROM_BIND, *(bind.key for bind in emitted)}:
        raise ValueError(f"{table}: {sorted(collide)} collide with the binds of the transition")
    source = f" FROM {from_sql}" if from_sql is not None else ""
    columns = ", ".join(f"t.{name}" for name in returning)
    predicate = f"t.{spec.state_col} = ANY(:{_FROM_BIND}) AND ({where})"
    # The registry's identifiers and the caller's static SQL, checked above; every value is a bind.
    statement = sa.text(
        f"UPDATE {spec.table} AS t SET {set_list}{source} WHERE {predicate} RETURNING {columns}"  # noqa: S608
    ).bindparams(
        *emitted,
        sa.bindparam(_FROM_BIND, list(from_states), type_=ARRAY(sa.Text)),
        **params,
    )
    return list((await conn.execute(statement)).mappings())


def transition_set_clause(
    table: str, to_state: str, *, reason: str | _Clear | None, set_sql: Mapping[str, str]
) -> str:
    """The `SET` list of an edge into `to_state`, for the `ON CONFLICT ... DO UPDATE` of an upsert (§9.7).

    The same assignments as `transition()` and `transition_where()` (the new state, the reason when one is
    given, the changed-at column of `to_state`), then `set_sql`, static SQL as in `transition_where()`, e.g.
    `state='pending', state_reason=NULL, state_changed_at=now(), override='none', ...`. The from-state is the
    upsert's own `WHERE` (the revival names `cancelled`), so nothing is cleared here and the changed-at column
    is always set.

    The fragment binds nothing of its own (ruling T2-a): the to-state, which the registry knows, and a text
    reason are SQL literals, and `CLEAR_REASON` is `NULL`. The registry has no list of reasons, and the
    fragment is spliced into the caller's `sa.text`, which would read a `:word` inside a quoted literal as a
    bind, so a reason must be a plain code (`[a-z_][a-z0-9_]*`). `set_sql` brings its own binds.

    Raises `ValueError` for an unknown table, a to-state no edge enters, a reason without a reason column, a
    reason that is not a plain code, a `set_sql` column that is not settable, and a `:name::type` cast.
    """
    spec = _spec(table)
    if not any(to == to_state for _, to in spec.allowed):
        raise ValueError(f"no transition of {table} enters {to_state}")
    if reason is not None and spec.reason_col is None:
        raise ValueError(f"{table} has no reason column")
    if isinstance(reason, str) and not _IDENTIFIER.fullmatch(reason):
        raise ValueError(f"{table}: the reason {reason!r} is not a plain code ([a-z_][a-z0-9_]*)")
    if unsettable := set(set_sql) - spec.settable:
        raise ValueError(f"{table}: {sorted(unsettable)} cannot be set by a transition")
    _sql_binds(table, list(set_sql.values()))
    assignments = {
        column: sa.literal(value, sa.Text) if isinstance(value, str) else value
        for column, value in _assignments(spec, (), to_state, reason, frozenset(), {}).items()
    }
    set_list, _ = _set_list(spec, assignments, set_sql, literal_binds=True)
    return set_list
