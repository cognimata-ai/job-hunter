"""The score lane's batch submissions (spec §10.10, §7.12, §6.9.3, §6.9.4, §12.6 (e); plan Tasks 14, 16 and
17, Rulings P14, P16, P31, P52, T7-b, T7-c, T7-d, T16-a, T16-c, T16-d, T16-e, Carry T16-h, Codex r1 #18,
#22-#25, r2 B7-B15, r3 B1-B3, r4 B1, B8, B9, r5 B1, A1 issue 17, A2 B10, B13, B14, Deviations 12, 13, 27 and
38): Task 14 records the cancellation a tuple supersession asks for; Task 16 adds the submission lifecycle up
to the provider's answer; Task 17 the user's release of a quarantined submission and the quarantined spend.
This module is the submission state machine; its worker side is `scoring.batch_operations` (the
`batch_submit` handler, the takeover sweep and the tick reconciler), `scoring.batch_results` (the provider
cancel, the poll and the completion) and `scoring.batch_adoption` (the ambiguous-submit reconciliation;
Ruling T17-b), and the decision of one item `scoring.batch_items` (Ruling T16-e).

**The cancellation** (Task 14). `request_cancel(conn, *, llm_tuple_id, now) -> RequestedCancel`, in the
caller's transaction (the tuple activation's, which holds `active_config FOR UPDATE` and has already taken the
month and bulk rows, so no batch-lane transaction runs meanwhile):
1. `cancel_requested_at := now` on **every** non-terminal submission of the tuple (`IN_FLIGHT`: `preparing`,
   `submit_started`, `submit_ambiguous`, `submitted`, `ended`; Codex r1 #24), whatever its phase; a request
   recorded earlier keeps its time. This record is the request: the worker calls `messages.batches.cancel`
   after the commit, as soon as a provider id is known (`record_created` below for one created during the
   request, `batch_poll` for a `submitted` one still in progress: `batch_results.cancel_requested`). A
   submission's state does not change here;
2. in the same transaction, their `in_batch` targets → `cancelled('tuple_superseded')` with
   `batch_submission_id` cleared, their running attempts ended `cancelled`, each target's follow-up hold
   released (`targets.cancel_batched`: the bulk rows first, Ruling P52). An item that arrives later for such a
   target is fenced (`ignored_fenced`, `scoring.batch_items`).

**Eligibility** (`ELIGIBLE_SQL`, L3939, A2 §5.4): one static predicate over `score_targets t` binding `:b`
(the bulk operation) and `:policy` (the active evaluation policy): a `pending` target of the operation whose
backoff has passed, not purge-fenced, with no resume draft and no draft to resume from under the policy
(`targets.RESUMABLE_DRAFT_SQL`, correlated to `t`: a target that `load_context` would resume is never batched
and paid for again, Codex r4 B9, r5 B1), not in the `batch_followup` or `score_now` lane, not in an online
retry disposition (`batch_max_tokens`, ...), with no earlier item that received a provider result (no
re-batching, L5727), charged attempts left and no override. The order: `availability = 'active'` first, then
the previous tuple's `score_mid` (the job's newest current instance of another tuple), then similarity, then
the id (L3942).

**The preparation** (`prepare`, §10.10 step 1, L3951), one transaction in the schedule's order, every write
inside a savepoint that a `NotPrepared` rolls back, so nothing of a batch that is not sent is ever written:
1. `active_config FOR SHARE` with the active configuration (`cycles.scoring_config`): the tuple must still be
   the active one and the bulk operation `active` and of that tuple, else `tuple_superseded`;
2. `cycles.pause_reason` → `paused` (the score lane's one admission guard: an unverified schema, an auth
   failure, an unacknowledged bound violation, the cap or a global fence stop the batch branch exactly as
   they stop a cycle, Codex r1 #25);
3. the fault point `batch.before_owner_lock`, then the operation row under this execution's claim
   (`lock_operation_owner`, step 5): a reclaimed execution is `FencedOut` here and prepares nothing;
4. the preconditions: `scoring.batch_bulk_rescore` (`batches_disabled`) and no submission in flight
   (`ambiguous` for a `submit_ambiguous` one, `in_flight` for the others; the `batch_one_in_flight` index has
   the last word: a unique violation of it at the insert below is `in_flight` too);
5. without a lock, the candidates (`ELIGIBLE_SQL`, in order, at most `batch_max_items`), each rendered as the
   online generator request of its target (`steps.role_prompt`, `revision_ad`, `score_refs`: the attempt's
   pinned `generate` node, `gateway.batch_request_body` at its first stage, the prefix's cache marker kept,
   L6622) with its input bound, hashes, worst case and size: the expensive part of the preparation holds no
   lock (the CAS re-checks every candidate); fewer than `batch_min_targets` is `below_minimum`;
6. `ensure_month`, the month row (`lock_month`, step 6) and the bulk row `FOR NO KEY UPDATE` (step 7),
   **before** the submission row exists: its immediate foreign key takes `FOR KEY SHARE` on the bulk row,
   which would otherwise come before the month row (Codex r3 B1); then the submission inserted `preparing`
   with its operation and claim (placeholders for the rest), and the fault point `batch.after_candidates`.
   Under the month and bulk locks, the cut: to `BATCH_MAX_BYTES` of requests, then to the largest prefix
   whose worst cases (each its own bound's, at batch prices) plus `n x hold` fit the allocation
   (`bulk.Accounts.fits`, L1189) **and** whose worst cases fit the month's scoring headroom
   (`budget.headroom` of the locked row: each item is admitted against the hard cap, L1159, Ruling T16-d).
   No fit is `no_allocation`, or `budget_exhausted` when the headroom stopped it (the pause check's read was
   unlocked); fewer than `batch_min_targets` is `below_minimum`;
7. the CAS `pending → in_batch` of those targets (`transition_where`, `ELIGIBLE_SQL` again over each row,
   `FOR UPDATE SKIP LOCKED`, `attempt_no + 1`, lane `batch`, the submission): the rows are locked and their
   attempt numbers advanced before any dependent row exists, and the statement never waits for a target row
   (the schedule's one stated exception, Deviation 38: no cycle through the month row can form);
8. the survivors `m`: fewer than `batch_min_targets` (none included) is `below_minimum` (Codex r2 B9);
9. per survivor a `score_attempts` row (`mode = 'batch'`, the active execution config and evaluation policy,
   lane `batch`) and a `batch_items` row (`custom_id = 't<target>_a<attempt>'`, its hold); then
   `spend_ledger.reserve_batch` (its allocation and hard-cap checks again under the same locks, so they pass:
   a `BudgetExhausted` there is a bug and fails the operation), each item's `llm_call_id`, and the
   submission's `request_count`, `request_hash` (SHA-256 over its items sorted by `custom_id`, each
   `custom_id ‖ SHA-256(canonical params)`), `reserved_usd` and `followup_held_usd = m x hold`;
10. `bulk.add_hold(m x hold)`; 11. `ranking.bump`.
`hold` is `bulk.hold_per_item` of the active evaluation policy and model policy; the ledger's
`prompt_template_version` is the rendered prompt's own.

**Submit-start** (`submit_start`, step 2, Ruling T7-c): `active_config FOR SHARE`, the operation owner, the
submission `FOR UPDATE` under `state = 'preparing'` and its claim (else `FencedOut`); a submission whose tuple
is no longer active, whose bulk operation is no longer `active`, whose cancel was requested, or that meets an
active global purge fence (L6365) is abandoned instead (nothing obsolete is dispatched, Codex r1 #24);
otherwise the fault point `batch.before_submit_start`, `preparing → submit_started` and the batch dispatch
marker in the same transaction (`spend_ledger.mark_batch_dispatched`: every entry dispatched, every attempt
charged and its target's `charged_attempts + 1`, R6, Deviation 13; the target rows before the attempt rows).

**The recording transactions** (steps 4a-4c) share one shape in the schedule's order (Codex r2 B7, B13):
`active_config FOR SHARE`, the operation owner, the submission `FOR UPDATE` under `state = 'submit_started'`
and its claim, then the month row and the ledger entries, the bulk row, the targets, then the attempts (the
order of node 9's cancel, `targets._cancel`: no lock cycle with it, Ruling T16-c), and `ranking_state`.
Attempts move only while `running` (`transition_where`, zero rows is fine), so an attempt a supersession ended
keeps its outcome and no accounting of an item depends on it. The handler runs the recording in a transaction
of its own, retried on a retryable database error (`graphs.sanitize.is_retryable_db_error`: a deadlock, a
serialization failure, a lost connection) up to `DB_RETRY.max_attempts` tries, its interval waited with the
gateway's injected `sleep`; the owner predicate makes each try the same recording, and a row whose tries ran
out stays `submit_started`, the recovery's:
- `record_created`: `submit_started → submitted` with `provider_batch_id` and `expires_at = created_at + 24
  h` (L3954); whether a cancel was requested meanwhile (the worker then calls `messages.batches.cancel` after
  the commit, `batch_results.cancel_requested`; `batch_poll` sends it again while the batch runs);
- `record_rejected`: `submit_started → submit_rejected`; every item's entry settled `not_billed` on the
  rejection's evidence (`batch_submit_rejected`, the call `error` with that code and the rejection's status;
  `connect_failed_before_send` for a create never sent, the call `connect_failed` without a status, Ruling
  T16-a); every item's hold released (`bulk.release_item_hold`); the submission's `in_batch` targets →
  `pending` (the batch pin `batch_submission_id = :s AND attempt_no = :a`; a cancelled target is left as it
  is), their bulk rows first (P52); the attempts still `running` → `submit_rejected`; for a 401 or 403 the
  critical `llm.auth_failed`, raised as the gateway raises it (`gateway.raise_auth_failed`), so the next
  prepare is paused (`cycles.pause_reason`); the follow-on, requested at once for a definitive 4xx and
  only marked for a not-sent rejection (`Rejected.not_sent`, Ruling T16-f). The attempts stay charged (Ruling
  T7-d,
  Deviation 13): a request the provider always rejects exhausts its targets instead of resubmitting forever;
- `record_ambiguous`: `submit_started → submit_ambiguous` with `reconcile_report = {"error_class": …}`;
  nothing else moves (L3956): the targets stay `in_batch`, the spend reserved and counted, until the
  ambiguous reconciliation adopts its batch or the user releases it (below).

**Abandon** (`abandon`, step 4d), in the recording shape under `state = 'preparing'` (no operation lock: its
callers are the recovery of a lost execution and `submit_start`, which holds it): `preparing → abandoned`;
each entry released (`release_never_dispatched`: the calls `cancelled`, the counters and the bulk debit
credited); the items' holds released; the submission's `in_batch` targets → `pending`; the attempts still
`running` → `cancelled('batch_abandoned')` (A2 B10, Deviation 12; the targets first, T16-c); the follow-on;
`ranking.bump`.

**The recovery of a lost execution** (`recover_lost`, Codex r1 #23, r2 B12, r3 B2, B3): every `preparing` or
`submit_started` submission (of `operation_id`, when given) whose operation is not `running` under the claim
that prepared it in the current worker epoch or later (`NOT EXISTS ... o.status = 'running' AND o.claim_id =
s.operation_claim_id AND o.worker_epoch >= :epoch`), locked `FOR UPDATE`: a `preparing` one is abandoned, a
`submit_started` one becomes `submit_ambiguous` with `{"error_class": "execution_lost"}` and is **never
resent**, whether the execution died before or after the HTTP send (the ambiguous reconciliation,
`batch_adoption.reconcile_ambiguous`, finds the batch if it was created, or the user releases it). The
caller holds `fence_epoch` and `active_config FOR SHARE` first. It runs in the takeover sweep
`batch_submissions`, in the `batch_submit` handler's first transaction (its own operation's rows), and in the
tick reconciler `batch_submissions`, all three `scoring.batch_operations`'; `any_lost` is the reconciler's
lock-free probe. An operation's failure hook cannot do it (the hook runs after the operation row, and
`active_config` comes first), so a row left by an operation that ended `failed` waits for the next tick.

**The follow-on** (`follow_on`, L3945, Codex r2 B15, A2 B13): keyed by the originating submission, once, when
it ends `submit_rejected`, `abandoned` or `reconciled` unreleased, or at the release of a `released` one
(never at its later adoption): the marker `follow_on_at` is set once, and with the bulk operation still
`active` the next `batch_submit` is recorded (`{"bulk_operation_id", "llm_tuple_id", "after_submission_id"}`,
dedup key `batch_submit:<bulk>:after:<s>`, its job under the `batch` lock, `OPERATION_LOCKS`). Ruling T16-f:
after a not-sent rejection (a 409 or a 429 on the create's last try, or a connect failure before the send:
the provider asked for time) the marker is set but no operation is recorded; the next heavy pass requests
the batch (`rerequest`).

**The request again** (`rerequest`, Ruling T16-d): a prepare that skipped (`paused`, `budget_exhausted`, ...)
ends its operation and requests no follow-on, nor does a not-sent rejection (T16-f), so the `bulk_operations`
reconciler (`bulk.reconcile_bulk_operations`, through `bulk.register_active_step`) asks for the batch again
on each heavy pass, in its transaction (`fence_epoch`, `active_config FOR SHARE`), for the `active` bulk
operation that it did not complete: when batches are enabled, the lane is not paused (`cycles.pause_reason`;
its schema check is the prepare's, the reconciler knowing no transport), no submission is in flight
(`IN_FLIGHT`, unreleased), no `batch_submit` of the operation is `pending` or `running`, its target creation
(`create_targets_for_tuple`) is not live (`bulk.creation_live`: the first batch is the creation's to request,
once every chunk is done), and at least `batch_min_targets` of its targets are eligible, it records
`{"bulk_operation_id", "llm_tuple_id"}` under the dedup key `batch_submit:<bulk>:resume` (`create_operation`:
one live request at most). The prepare re-checks every gate. Carry T16-h: before the request, the step runs
the tuple's exhaustion sweep (`targets.exhaustion_sweep`, its bulk rows before its target rows, P52), which a
not-sent rejection's missing follow-on would otherwise leave to the next cycle; when it ended the operation's
last open targets, `bulk.complete_if_done` completes the operation in the same pass and nothing is requested;
`ranking.bump` last when it moved any.

**The release** (`release`, L3991-3997, Codex r2 B7, B13, B15), the user's "release and re-score" of a
`submit_ambiguous` submission, or (plan M4 Task 16, U3) of a lost batch: a `submitted` one whose
`batch.retrieve_failing` is open and whose `expires_at + RETRIEVE_FAILING_AFTER` (1 h) has passed. Both are
`RELEASABLE_SQL`, the one predicate that Needs attention's `releasable` evaluates too, so the button and the
route never disagree. In the route's transaction (Task 21: `active_config FOR SHARE` and the idempotency row
held), in the schedule's order: the submission `FOR UPDATE` under that predicate at the caller's `now`,
evaluated in the locking statement itself (else `TransitionConflict`: a row another transaction moved
meanwhile is re-checked on its new version, so a poll that committed `submitted → ended` first leaves nothing
to release) → `released` (`released_at`, outside the `batch_one_in_flight` singleton from now on; a lost
batch keeps its `provider_batch_id`, and no provider cancel is sent: a retrieval that keeps failing gives no
reason to expect one to succeed); every item's entry `dispatched → ambiguous` (`settle(outcome="ambiguous")`,
the call `ambiguous` with `batch_released`), counted at its reservation, which `settle` also adds to the
attempt's `cost_usd` (Carry T7-b, as the ledger reconciler's `_mark_ambiguous` does); the bulk row and each
item's hold released once (`bulk.release_item_hold`); the submission's `in_batch` targets → `pending` under
the batch pin (a cancelled one untouched; their bulk rows first, P52) before the attempts still `running` →
`released` (a superseded one keeps `cancelled`; T16-c's order); the alerts of the locked state resolved
(`batch.submit_ambiguous_unresolved` for a quarantine; `batch.retrieve_failing`, and the 25 h alert should
one be open, for a lost batch); the follow-on (it fires for a release, and never again when the submission is
reconciled later); `ranking.bump` last. A released quarantine stays the ambiguous reconciliation's to find
for 29 days (`batch_adoption`), a released lost batch the poll's (`batch_results`), by its provider id.

**The quarantined spend** (`quarantined_usd`, S:L1199, Ruling P31): Σ `reserved_usd` of the `submit_ambiguous`
submissions, by `bulk.QUARANTINED_USD_SQL` (`/api/system/spend`'s `quarantined_batch_usd`, and the context of
`fail`'s `llm.cap_reached`).

**The unresolved submissions** (Task 23, Codex r4 B11, r5 B9): `UNRESOLVED_SUBMISSION_SQL` is the one
predicate of "a submission still needs target `t`'s items": an item of `t` belongs to a submission in flight
(`IN_FLIGHT`), or to a `released` one the ambiguous reconciliation still lists, `submit_started_at` within
`RELEASED_LISTED_FOR` (29 days, L3984, Deviation 27: one clock, `submit_started_at`, the reconciler's, which
`batch_adoption` binds). Its items and their calls are the item-to-ledger mapping a release and a
reconciliation join through (`_ITEMS`, `spend_ledger.settle`), so the score-history retention keeps such a
target while it holds and deletes it in the pass after the submission is resolved; no timeout releases
anything (C3). It is an SQL predicate correlated to the outer alias `t` of `score_targets` and binds
`:listed_since`, the caller's `now` less `RELEASED_LISTED_FOR` (Ruling P14: the caller's clock, not the
database's).

Ids, counts, codes and amounts only reach a log line, a payload or a report: never a param or prompt text.
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final, Literal

import sqlalchemy as sa
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db import ranking
from jobhunter.db.guards import global_fence_active
from jobhunter.db.sql import is_unique_violation, q
from jobhunter.db.transitions import transition, transition_where
from jobhunter.errors import FencedOut, TransitionConflict
from jobhunter.faults import afault_point
from jobhunter.llm import budget, bulk, spend_ledger
from jobhunter.llm.batches import (
    CONNECT_FAILED_BEFORE_SEND,
    SUBMIT_REJECTED,
    Ambiguous,
    BatchRequest,
    Created,
    Rejected,
)
from jobhunter.llm.bounds import text_input_bound
from jobhunter.llm.gateway import (
    AUTH_FAILED_STATUSES,
    CONNECT_FAILED,
    LLMGateway,
    NodeConfig,
    pinned_node_config,
    raise_auth_failed,
)
from jobhunter.llm.pricing import worst_case_usd
from jobhunter.llm.prompts.render import PromptVersion, load_prompt_version, score_schema
from jobhunter.llm.schemas.profile import ConfirmedProfile
from jobhunter.llm.spend_ledger import BatchItemReservation, BatchSubmissionOwner, CallOutcome, Reservation
from jobhunter.observability.alerts import resolve_alert
from jobhunter.observability.logging import get_logger
from jobhunter.scoring import cycles, steps, targets
from jobhunter.scoring.admission import MAX_SCORE_ATTEMPTS
from jobhunter.settings.dryrun import active_settings
from jobhunter.settings.model import Settings
from jobhunter.types import JSONValue
from jobhunter.worker.operations import (
    OperationClaim,
    create_operation,
    lock_operation_owner,
)
from jobhunter.worker.reconcilers import ReconcileContext
from jobhunter.worker.tasks import Deferrer

# The submissions of a tuple that are not terminal (§7.16.6): `bulk.BATCH_IN_FLIGHT`, defined once in `llm`.
IN_FLIGHT: Final = bulk.BATCH_IN_FLIGHT
# L3984: how long after its submit-start the ambiguous reconciliation lists a `released` submission (the
# results' retention); `batch_adoption.RELEASED_LISTED_FOR` is this one.
RELEASED_LISTED_FOR: Final = timedelta(days=29)
# Task 23 (module docstring): a submission still needs target `t`'s items; binds `:listed_since`.
UNRESOLVED_SUBMISSION_SQL: Final = (
    "EXISTS (SELECT 1 FROM batch_items i JOIN batch_submissions s ON s.id = i.submission_id "  # noqa: S608
    f"WHERE i.target_id = t.id AND (s.state IN ({', '.join(repr(state) for state in IN_FLIGHT)}) "
    "OR (s.state = 'released' AND s.submit_started_at > :listed_since)))"
)
KIND: Final = "batch_submit"
# Deviation 27, A2 B14: the rendered requests of one submission, below the provider's 256 MB.
BATCH_MAX_BYTES: Final = 200 * 1024 * 1024
BATCH_TTL: Final = timedelta(hours=24)  # L3954: `expires_at = created_at + 24 h`
# The fault points of the state machine (the handler's are `scoring.batch_operations`').
BEFORE_OWNER_LOCK: Final = "batch.before_owner_lock"
AFTER_CANDIDATES: Final = "batch.after_candidates"
BEFORE_SUBMIT_START: Final = "batch.before_submit_start"
# The codes this module records.
EXECUTION_LOST: Final = "execution_lost"
ABANDONED_DETAIL: Final = "batch_abandoned"
RELEASED_CODE: Final = "batch_released"  # the call's code of an entry the release made ambiguous
# The critical alert of a quarantine 25 h old (L3988), resolved by the release (subject: the submission id).
UNRESOLVED_ALERT: Final = "batch.submit_ambiguous_unresolved"
# L3958: a `submitted` batch whose retrieval still fails this long past its `expires_at` (the provider has
# ended it by then) raises the critical `batch.retrieve_failing` (subject: the submission id), which the
# poll raises (`batch_results`, which names both) and the poll's move to `ended` or the release resolves.
RETRIEVE_FAILING_ALERT: Final = "batch.retrieve_failing"
RETRIEVE_FAILING_AFTER: Final = timedelta(hours=1)
# Plan M4 Task 16 (U3, the module docstring's release): the one predicate of a releasable submission over
# `batch_submissions s`, which the release's lock and Needs attention's `releasable` both evaluate
# (`observability.attention`): `submit_ambiguous`, or `submitted` with its `batch.retrieve_failing` open (by
# the alert's dedup key, `alerts_open_dedup`) and `now > expires_at + RETRIEVE_FAILING_AFTER`. It binds
# `:expired_before` (`releasable_params`: the caller's `now` less `RETRIEVE_FAILING_AFTER`, Ruling P14).
RELEASABLE_SQL: Final = (
    "(s.state = 'submit_ambiguous' OR (s.state = 'submitted' AND s.expires_at < :expired_before "  # noqa: S608
    "AND EXISTS (SELECT 1 FROM alerts rf WHERE rf.resolved_at IS NULL "
    f"AND rf.dedup_key = '{RETRIEVE_FAILING_ALERT}:' || CAST(s.id AS text))))"
)
_RELEASABLE_STATES: Final = ("submit_ambiguous", "submitted")
# The alerts a release resolves, by the state it locked (each a no-op when it is not open): a quarantine's
# 25 h alert; a lost batch's `batch.retrieve_failing`, and the 25 h alert too should one be open.
_RELEASE_RESOLVES: Final[Mapping[str, tuple[str, ...]]] = {
    "submit_ambiguous": (UNRESOLVED_ALERT,),
    "submitted": (RETRIEVE_FAILING_ALERT, UNRESOLVED_ALERT),
}
_ONLINE_RETRY_REASONS: Final = (
    "batch_max_tokens",
    "batch_invalid_output",
    "batch_errored",
    "batch_canceled",
    "batch_expired",
    "batch_missing",
)
_SUBMISSIONS: Final = "batch_submissions"
_ONE_IN_FLIGHT: Final = "batch_one_in_flight"

NotPreparedReason = Literal[
    "batches_disabled",
    "paused",
    "in_flight",
    "ambiguous",
    "below_minimum",
    "no_allocation",
    "budget_exhausted",
    "tuple_superseded",
]

# §10.10's checklist as one static predicate over `score_targets t` (module docstring), binding `:b` and
# `:policy` (the correlated draft fragment's, never a `:t`).
ELIGIBLE_SQL: Final = (
    "t.bulk_operation_id = :b AND t.state = 'pending' AND (t.not_before IS NULL OR t.not_before <= now()) "  # noqa: S608
    "AND NOT is_fenced(t.job_id, t.llm_tuple_id) AND t.resume_draft_id IS NULL "
    f"AND NOT {targets.RESUMABLE_DRAFT_SQL} "
    "AND (t.lane IS NULL OR t.lane NOT IN ('batch_followup', 'score_now')) "
    "AND (t.state_reason IS NULL OR t.state_reason NOT IN "
    f"({', '.join(repr(reason) for reason in _ONLINE_RETRY_REASONS)})) "
    "AND NOT EXISTS (SELECT 1 FROM batch_items i WHERE i.target_id = t.id AND i.result_type <> 'awaiting') "
    f"AND t.charged_attempts < {MAX_SCORE_ATTEMPTS} AND t.override = 'none'"
)
# L3942: the previous tuple's score of the job, from its newest current instance of another tuple.
_PREVIOUS_SCORE: Final = (
    "(SELECT d.score_mid FROM det_results d JOIN llm_results r ON r.id = d.llm_result_id "
    "JOIN score_targets o ON o.id = r.target_id WHERE d.job_id = t.job_id AND d.is_current "
    "AND o.llm_tuple_id <> t.llm_tuple_id ORDER BY d.id DESC LIMIT 1)"
)
_CANDIDATES: Final = q(
    "SELECT t.id, t.attempt_no, t.job_revision_id, r.llm_input, r.description_text, "  # noqa: S608
    "r.description_completeness FROM score_targets t JOIN jobs j ON j.id = t.job_id "
    f"JOIN job_revisions r ON r.id = t.job_revision_id WHERE {ELIGIBLE_SQL} "
    f"ORDER BY (j.availability = 'active') DESC, {_PREVIOUS_SCORE} DESC NULLS LAST, "
    "t.similarity DESC NULLS LAST, t.id LIMIT :limit"
)
# Step 7: the CAS over the candidates, the predicate again over each row (the inner `t` is the subquery's).
_CAS_WHERE: Final = (
    "t.id IN (SELECT t.id FROM score_targets t WHERE t.id = ANY(CAST(:ids AS bigint[])) "  # noqa: S608
    f"AND {ELIGIBLE_SQL} FOR UPDATE SKIP LOCKED)"
)
_CAS_SET: Final = {"attempt_no": "t.attempt_no + 1", "lane": "'batch'", "batch_submission_id": ":s"}
_TUPLE_INPUTS: Final = q(
    "SELECT u.profile_version_id, u.rubric_version_id, u.prompt_schema_version_id, u.model_policy_id, "
    "v.profile_json, w.weights, w.anchors FROM llm_tuples u "
    "JOIN profile_versions v ON v.id = u.profile_version_id "
    "JOIN rubric_versions w ON w.id = u.rubric_version_id WHERE u.id = :tuple"
)
_BULK_OF: Final = q("SELECT state, llm_tuple_id FROM bulk_operations WHERE id = :b")
_IN_FLIGHT_STATES: Final = q(
    "SELECT state FROM batch_submissions WHERE state = ANY(CAST(:states AS text[])) AND released_at IS NULL"
)
_INSERT_SUBMISSION: Final = q(
    "INSERT INTO batch_submissions (state, llm_tuple_id, bulk_operation_id, operation_id, "
    "operation_claim_id, "
    "request_count, request_hash, reserved_usd, followup_held_usd) "
    "VALUES ('preparing', :tuple, :b, :operation, :claim, 1, :hash, 0, 0) RETURNING id"
)
_INSERT_ATTEMPTS: Final = q(
    "INSERT INTO score_attempts (target_id, attempt_no, mode, outcome, execution_config_id, "
    "evaluation_policy_id, lane) "
    "SELECT c.target, c.attempt, 'batch', 'running', :execution, :policy, 'batch' "
    "FROM unnest(CAST(:targets AS bigint[]), CAST(:attempts AS integer[])) AS c(target, attempt) "
    "RETURNING id, target_id"
)
_INSERT_ITEMS: Final = q(
    "INSERT INTO batch_items (submission_id, target_id, attempt_no, custom_id, hold_usd) "
    "SELECT :s, c.target, c.attempt, 't' || c.target || '_a' || c.attempt, :hold "
    "FROM unnest(CAST(:targets AS bigint[]), CAST(:attempts AS integer[])) AS c(target, attempt) "
    "RETURNING id, target_id"
)
_LINK_ITEMS: Final = q(
    "UPDATE batch_items i SET llm_call_id = c.call "
    "FROM unnest(CAST(:items AS bigint[]), CAST(:calls AS bigint[])) AS c(item, call) WHERE i.id = c.item"
)
_FILL_SUBMISSION: Final = q(
    "UPDATE batch_submissions SET request_count = :n, request_hash = :hash, reserved_usd = :reserved, "
    "followup_held_usd = :held WHERE id = :s"
)
# The owner rows of the lifecycle's transactions (step 5 of the schedule).
_LOCK_PREPARING: Final = q(
    "SELECT llm_tuple_id, bulk_operation_id, cancel_requested_at FROM batch_submissions "
    "WHERE id = :s AND state = 'preparing' AND operation_claim_id = :claim FOR UPDATE"
)
_LOCK_STARTED: Final = q(
    "SELECT llm_tuple_id, bulk_operation_id, cancel_requested_at FROM batch_submissions "
    "WHERE id = :s AND state = 'submit_started' AND operation_claim_id = :claim FOR UPDATE"
)
_LOCK_RELEASABLE: Final = q(
    "SELECT s.state, s.reserved_usd FROM batch_submissions s "  # noqa: S608
    f"WHERE s.id = :s AND {RELEASABLE_SQL} FOR UPDATE OF s"
)
_QUARANTINED: Final = q(bulk.QUARANTINED_USD_SQL)
_LOCK_IN_STATE: Final = q(
    "SELECT bulk_operation_id FROM batch_submissions WHERE id = :s AND state = :state FOR UPDATE"
)
# A submission's items with their entries (an item reserves exactly one), in id order.
_ITEMS: Final = q(
    "SELECT i.id, i.target_id, i.attempt_no, c.id AS call_id, l.id AS ledger_id, l.budget_month, "
    "l.reserved_usd, l.model_requested, l.purpose_class, l.state AS entry_state FROM batch_items i "
    "JOIN llm_calls c ON c.id = i.llm_call_id JOIN llm_spend_ledger l ON l.id = c.spend_ledger_id "
    "WHERE i.submission_id = :s ORDER BY i.id"
)
_ATTEMPTS_OF_ITEMS: Final = (
    "t.mode = 'batch' AND (t.target_id, t.attempt_no) IN "
    "(SELECT * FROM unnest(CAST(:targets AS bigint[]), CAST(:numbers AS integer[])))"
)
_TARGETS_OF_ITEMS: Final = (
    "t.id IN (SELECT s.id FROM score_targets s WHERE s.batch_submission_id = :s AND (s.id, s.attempt_no) IN "
    "(SELECT * FROM unnest(CAST(:targets AS bigint[]), CAST(:numbers AS integer[]))) "
    "ORDER BY s.id FOR UPDATE)"
)
# The lost executions' submissions (module docstring): the predicate, the rows locked in id order, and the
# lock-free probe of the tick reconciler.
_LOST_WHERE: Final = (
    "s.state IN ('preparing', 'submit_started') AND NOT EXISTS (SELECT 1 FROM operations o "
    "WHERE o.id = s.operation_id AND o.status = 'running' AND o.claim_id = s.operation_claim_id "
    "AND o.worker_epoch >= :epoch)"
)
_LOST: Final = q(
    f"SELECT s.id, s.state FROM batch_submissions s WHERE {_LOST_WHERE} "  # noqa: S608
    "AND (CAST(:operation AS bigint) IS NULL OR s.operation_id = CAST(:operation AS bigint)) "
    "ORDER BY s.id FOR UPDATE OF s"
)
_ANY_LOST: Final = q(f"SELECT 1 FROM batch_submissions s WHERE {_LOST_WHERE} LIMIT 1")  # noqa: S608
_FOLLOW_ON_OF: Final = q(
    "SELECT state, released_at, bulk_operation_id, llm_tuple_id FROM batch_submissions WHERE id = :s"
)
_MARK_FOLLOW_ON: Final = q(
    "UPDATE batch_submissions SET follow_on_at = now() WHERE id = :s AND follow_on_at IS NULL RETURNING id"
)
_RECORD: Final = q(
    "UPDATE batch_submissions SET cancel_requested_at = coalesce(cancel_requested_at, :now) "
    "WHERE llm_tuple_id = :tuple AND state = ANY(CAST(:states AS text[])) RETURNING id"
)
_FOLLOW_ON_STATES: Final = frozenset({"submit_rejected", "abandoned", "reconciled"})
# Ruling T16-d: a live `batch_submit` of the bulk operation, and whether `:n` of its targets are eligible.
_LIVE_SUBMIT: Final = q(
    "SELECT EXISTS (SELECT 1 FROM operations WHERE kind = 'batch_submit' "
    "AND status IN ('pending', 'running') AND payload ->> 'bulk_operation_id' = CAST(:b AS text))"
)
_ELIGIBLE_COUNT: Final = q(
    f"SELECT count(*) FROM (SELECT 1 FROM score_targets t WHERE {ELIGIBLE_SQL} LIMIT :n) e"  # noqa: S608
)

log = get_logger("jobhunter.scoring.batches")


# ---- the cancellation (Task 14) ----------------------------------------------------------------------------


@dataclass(frozen=True)
class RequestedCancel:
    """What a cancellation request touched: the submissions it recorded the request on, the `in_batch`
    targets it cancelled and the attempts it ended."""

    submissions: int
    targets: int
    attempts: int


async def request_cancel(conn: AsyncConnection, *, llm_tuple_id: int, now: datetime) -> RequestedCancel:
    """Record the cancellation on every non-terminal submission of the tuple and cancel their `in_batch`
    targets (module docstring)."""
    params = {"now": now, "tuple": llm_tuple_id, "states": list(IN_FLIGHT)}
    submissions = sorted(int(i) for i in (await conn.execute(_RECORD, params)).scalars())
    if not submissions:
        return RequestedCancel(0, 0, 0)
    cancelled = await targets.cancel_batched(
        conn, submission_ids=submissions, reason=targets.TUPLE_SUPERSEDED
    )
    log.info(
        "batch_cancel_requested",
        llm_tuple_id=llm_tuple_id,
        submission_ids=submissions,
        targets=cancelled.targets,
        attempts=cancelled.attempts,
    )
    return RequestedCancel(len(submissions), cancelled.targets, cancelled.attempts)


# ---- the preparation ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Prepared:
    """A submission prepared: its id, its items, what they reserved, each item's follow-up hold, and the
    requests to send (in request order; never logged)."""

    submission_id: int
    request_count: int
    reserved_usd: Decimal
    hold_usd: Decimal
    requests: tuple[BatchRequest, ...] = field(default=(), repr=False, compare=False)


@dataclass(frozen=True)
class NotPrepared:
    """Nothing prepared, and nothing written: why."""

    reason: NotPreparedReason


@dataclass(frozen=True)
class _Candidate:
    target_id: int
    attempt_no: int
    params: Mapping[str, JSONValue]
    input_refs: Mapping[str, JSONValue]
    template_version: str
    bound: int
    worst_usd: Decimal
    size: int  # `request_bytes` of its request
    rendered_sha256: bytes
    prefix_sha256: bytes | None


def request_bytes(request: BatchRequest) -> int:
    """The bytes a request takes in the create's body: its `custom_id` and params as compact JSON."""
    doc = {"custom_id": request.custom_id, "params": dict(request.params)}
    return len(json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def request_hash(requests: Sequence[BatchRequest]) -> bytes:
    """`batch_submissions.request_hash`: SHA-256 over the requests sorted by `custom_id`, each its `custom_id`
    and the SHA-256 of its canonical params."""
    digest = hashlib.sha256()
    for request in sorted(requests, key=lambda r: r.custom_id):
        canonical = json.dumps(
            dict(request.params), sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        digest.update(request.custom_id.encode("utf-8"))
        digest.update(hashlib.sha256(canonical.encode("utf-8")).digest())
    return digest.digest()


def _custom_id(target_id: int, attempt_no: int) -> str:
    return f"t{target_id}_a{attempt_no}"


async def _in_flight(conn: AsyncConnection) -> Literal["in_flight", "ambiguous"] | None:
    """Why the singleton is occupied: a `submit_ambiguous` submission, or another in flight; `None`."""
    found = set((await conn.execute(_IN_FLIGHT_STATES, {"states": list(IN_FLIGHT)})).scalars())
    if "submit_ambiguous" in found:
        return "ambiguous"
    return "in_flight" if found else None


async def prepare(
    conn: AsyncConnection,
    *,
    bulk_operation_id: int,
    llm_tuple_id: int,
    operation: OperationClaim,
    settings: Settings,
    fake_llm: bool,
    gateway: LLMGateway,
    now: datetime,
) -> Prepared | NotPrepared:
    """The preparation of a submission in the caller's transaction (module docstring); every write inside a
    savepoint that a `NotPrepared` (or an exception) rolls back. `FencedOut` for a lost claim; a hard cap that
    admits no item is `NotPrepared("budget_exhausted")`; `gateway` renders the params."""
    async with conn.begin_nested() as savepoint:  # an exception rolls it back on its way out
        result = await _prepare(
            conn,
            bulk_operation_id=bulk_operation_id,
            llm_tuple_id=llm_tuple_id,
            operation=operation,
            settings=settings,
            fake_llm=fake_llm,
            gateway=gateway,
            now=now,
        )
        if isinstance(result, NotPrepared):
            await savepoint.rollback()
    if isinstance(result, NotPrepared):
        log.info(
            "batch_not_prepared",
            operation_id=operation.operation_id,
            bulk_operation_id=bulk_operation_id,
            reason=result.reason,
        )
    return result


async def _prepare(
    conn: AsyncConnection,
    *,
    bulk_operation_id: int,
    llm_tuple_id: int,
    operation: OperationClaim,
    settings: Settings,
    fake_llm: bool,
    gateway: LLMGateway,
    now: datetime,
) -> Prepared | NotPrepared:
    config = await cycles.scoring_config(conn)  # 1. active_config FOR SHARE
    active = config.active
    operation_row = (await conn.execute(_BULK_OF, {"b": bulk_operation_id})).first()
    if (
        active.llm_tuple_id != llm_tuple_id
        or operation_row is None
        or operation_row.state != "active"
        or operation_row.llm_tuple_id != llm_tuple_id
    ):
        return NotPrepared("tuple_superseded")
    if await cycles.pause_reason(conn, fake_llm=fake_llm, now=now, config=config) is not None:  # 2.
        return NotPrepared("paused")
    await afault_point(BEFORE_OWNER_LOCK)
    await lock_operation_owner(conn, operation)  # 3. step 5: a reclaimed execution is fenced here
    scoring = settings.scoring  # 4. the preconditions
    if not scoring.batch_bulk_rescore:
        return NotPrepared("batches_disabled")
    if (occupied := await _in_flight(conn)) is not None:
        return NotPrepared(occupied)
    tuple_row = (await conn.execute(_TUPLE_INPUTS, {"tuple": llm_tuple_id})).one()  # 5. no lock held
    if tuple_row.profile_json is None:
        raise LookupError(f"the profile version of tuple {llm_tuple_id} is purged")
    version = await load_prompt_version(conn, int(tuple_row.prompt_schema_version_id))
    node = await pinned_node_config(
        conn,
        "generate",
        model_policy_id=int(tuple_row.model_policy_id),
        execution_config_id=active.execution_config_id,
        schema=score_schema("generate", version),
    )
    hold = await bulk.hold_per_item(
        conn, evaluation_policy_id=active.evaluation_policy_id, model_policy=config.model_policy
    )
    candidates = await _rendered(
        conn,
        tuple_row,
        version=version,
        node=node,
        gateway=gateway,
        bulk_operation_id=bulk_operation_id,
        evaluation_policy_id=active.evaluation_policy_id,
        limit=scoring.batch_max_items,
    )
    if len(candidates) < scoring.batch_min_targets:
        return NotPrepared("below_minimum")
    p = config.settings.budget  # 6. the month row, then the bulk row, before the submission row
    month = budget.month_of(now)
    await spend_ledger.ensure_month(conn, month, p, now)
    row = await spend_ledger.lock_month(conn, month, p, now=now)
    account = await bulk.lock_accounts(conn, bulk_operation_id)
    try:  # the owner row: new and uncommitted, its key-share on the bulk row this transaction's own
        async with conn.begin_nested():
            submission_id = int(
                (
                    await conn.execute(
                        _INSERT_SUBMISSION,
                        {
                            "tuple": llm_tuple_id,
                            "b": bulk_operation_id,
                            "operation": operation.operation_id,
                            "claim": operation.claim_id,
                            "hash": b"",
                        },
                    )
                ).scalar_one()
            )
    except sa.exc.DBAPIError as exc:
        if not is_unique_violation(exc, _ONE_IN_FLIGHT):
            raise
        return NotPrepared("in_flight")
    await afault_point(AFTER_CANDIDATES)
    chosen, stop = _cut(candidates, account, hold, headroom=budget.headroom(row, "scoring", p))
    if not chosen:
        return NotPrepared("budget_exhausted" if stop == "headroom" else "no_allocation")
    if len(chosen) < scoring.batch_min_targets:
        return NotPrepared("below_minimum")
    moved = await transition_where(  # 7. the CAS
        conn,
        "score_targets",
        ["pending"],
        "in_batch",
        where=_CAS_WHERE,
        params={
            "ids": [c.target_id for c in chosen],
            "b": bulk_operation_id,
            "policy": active.evaluation_policy_id,
            "s": submission_id,
        },
        set_sql=_CAS_SET,
        returning=("id", "attempt_no"),
    )
    numbers = {int(r["id"]): int(r["attempt_no"]) for r in moved}
    survivors = [c for c in chosen if c.target_id in numbers]
    if len(survivors) < scoring.batch_min_targets:  # 8.
        return NotPrepared("below_minimum")
    return await _fill(
        conn,
        submission_id=submission_id,
        bulk_operation_id=bulk_operation_id,
        survivors=survivors,
        numbers=numbers,
        node=node,
        execution_config_id=active.execution_config_id,
        evaluation_policy_id=active.evaluation_policy_id,
        hold=hold,
        now=now,
    )


async def _rendered(
    conn: AsyncConnection,
    tuple_row: Row[tuple[object, ...]],
    *,
    version: PromptVersion,
    node: NodeConfig,
    gateway: LLMGateway,
    bulk_operation_id: int,
    evaluation_policy_id: int,
    limit: int,
) -> list[_Candidate]:
    """Step 5: the candidates in order, each rendered, bounded, hashed, priced and sized, without a lock (a
    candidate whose prompt does not encode is skipped, never logged: the error quotes it)."""
    rubric = steps.rubric_of(tuple_row.weights, tuple_row.anchors)
    profile = ConfirmedProfile.model_validate(tuple_row.profile_json)
    rows = (
        await conn.execute(
            _CANDIDATES, {"b": bulk_operation_id, "policy": evaluation_policy_id, "limit": limit}
        )
    ).all()
    candidates: list[_Candidate] = []
    for row in rows:
        prompt = steps.role_prompt(
            "generate",
            version=version,
            rubric=rubric,
            profile=profile,
            ad=steps.material_ad(row),
            refs=steps.score_refs(
                job_revision_id=int(row.job_revision_id),
                profile_version_id=int(tuple_row.profile_version_id),
                rubric_version_id=int(tuple_row.rubric_version_id),
            ),
        )
        target_id, attempt_no = int(row.id), int(row.attempt_no) + 1
        try:
            bound = text_input_bound(node.model, prompt.as_request(node.schema))
            rendered, prefix = prompt.rendered_sha256(), prompt.prefix_sha256()
        except UnicodeEncodeError:  # never logged: the error quotes the prompt
            log.warning("batch_candidate_unencodable", target_id=target_id)
            continue
        params = gateway.batch_request_body(node, prompt, max_tokens=node.max_tokens)
        candidates.append(
            _Candidate(
                target_id=target_id,
                attempt_no=attempt_no,
                params=params,
                input_refs=prompt.input_refs,
                template_version=prompt.template_version,
                bound=bound,
                worst_usd=worst_case_usd(
                    node.model, input_tokens_bound=bound, max_tokens=node.max_tokens, batch=True
                ),
                size=request_bytes(BatchRequest(_custom_id(target_id, attempt_no), params)),
                rendered_sha256=rendered,
                prefix_sha256=prefix,
            )
        )
    return candidates


def _cut(
    candidates: Sequence[_Candidate], account: bulk.Accounts, hold: Decimal, *, headroom: Decimal
) -> tuple[list[_Candidate], Literal["bytes", "allocation", "headroom"] | None]:
    """The candidates cut to `BATCH_MAX_BYTES` of requests, then to the largest prefix whose worst cases plus
    its holds fit the allocation (L1189) and whose worst cases fit the month's scoring `headroom` (L1159,
    Ruling T16-d); with what stopped the cut (`None`: every candidate fits)."""
    chosen: list[_Candidate] = []
    worst = Decimal(0)
    bytes_used = 0
    for c in candidates:
        bytes_used += c.size
        if bytes_used > BATCH_MAX_BYTES:
            return chosen, "bytes"
        if not account.fits(worst_case_usd=worst + c.worst_usd, hold_usd=(len(chosen) + 1) * hold):
            return chosen, "allocation"
        if worst + c.worst_usd > headroom:
            return chosen, "headroom"
        chosen.append(c)
        worst += c.worst_usd
    return chosen, None


async def _fill(
    conn: AsyncConnection,
    *,
    submission_id: int,
    bulk_operation_id: int,
    survivors: Sequence[_Candidate],
    numbers: Mapping[int, int],
    node: NodeConfig,
    execution_config_id: int,
    evaluation_policy_id: int,
    hold: Decimal,
    now: datetime,
) -> Prepared:
    """Steps 9-11 of the preparation (module docstring)."""
    ids = [c.target_id for c in survivors]
    attempts = [numbers[t] for t in ids]
    pairs = {"targets": ids, "attempts": attempts}
    attempt_of = {
        int(r.target_id): int(r.id)
        for r in await conn.execute(
            _INSERT_ATTEMPTS, {**pairs, "execution": execution_config_id, "policy": evaluation_policy_id}
        )
    }
    item_of = {
        int(r.target_id): int(r.id)
        for r in await conn.execute(_INSERT_ITEMS, {**pairs, "s": submission_id, "hold": hold})
    }
    reservations = await spend_ledger.reserve_batch(
        conn,
        owner=BatchSubmissionOwner(submission_id, "preparing"),
        items=[
            BatchItemReservation(
                target_id=c.target_id,
                attempt_id=attempt_of[c.target_id],
                batch_item_id=item_of[c.target_id],
                input_tokens_bound=c.bound,
                rendered_prompt_sha256=c.rendered_sha256,
                prefix_sha256=c.prefix_sha256,
                input_refs=c.input_refs,
            )
            for c in survivors
        ],
        model=node.model,
        effort=node.effort,
        max_tokens=node.max_tokens,
        execution_config_id=execution_config_id,
        prompt_template_version=survivors[0].template_version,  # the rendered prompts' one version
        bulk_operation_id=bulk_operation_id,
        hold_usd=hold,
        now=now,
    )
    await conn.execute(
        _LINK_ITEMS,
        {"items": [item_of[t] for t in ids], "calls": [r.llm_call_id for r in reservations]},
    )
    requests = tuple(
        BatchRequest(_custom_id(c.target_id, numbers[c.target_id]), c.params, c.input_refs) for c in survivors
    )
    reserved = sum((r.reserved_usd for r in reservations), Decimal(0))
    held = len(survivors) * hold
    await conn.execute(
        _FILL_SUBMISSION,
        {
            "n": len(survivors),
            "hash": request_hash(requests),
            "reserved": reserved,
            "held": held,
            "s": submission_id,
        },
    )
    await bulk.add_hold(conn, bulk_operation_id, held)  # 10.
    await ranking.bump(conn)  # 11. step 11, last
    log.info(
        "batch_prepared",
        submission_id=submission_id,
        bulk_operation_id=bulk_operation_id,
        request_count=len(survivors),
        reserved_usd=str(reserved),
        followup_held_usd=str(held),
    )
    return Prepared(submission_id, len(survivors), reserved, hold, requests)


# ---- the items of a submission -----------------------------------------------------------------------------


@dataclass(frozen=True)
class _Item:
    id: int
    target_id: int
    attempt_no: int
    entry_state: str
    reservation: Reservation


async def _items(conn: AsyncConnection, submission_id: int) -> list[_Item]:
    return [
        _Item(
            id=int(r.id),
            target_id=int(r.target_id),
            attempt_no=int(r.attempt_no),
            entry_state=str(r.entry_state),
            reservation=Reservation(
                ledger_id=int(r.ledger_id),
                llm_call_id=int(r.call_id),
                month=r.budget_month,
                reserved_usd=r.reserved_usd,
                purpose_class=r.purpose_class,
                model=r.model_requested,
                batch=True,
            ),
        )
        for r in await conn.execute(_ITEMS, {"s": submission_id})
    ]


def _pairs(items: Sequence[_Item]) -> dict[str, object]:
    return {"targets": [i.target_id for i in items], "numbers": [i.attempt_no for i in items]}


async def _end_attempts(
    conn: AsyncConnection, items: Sequence[_Item], outcome: str, detail: str | None
) -> int:
    """The items' attempts still `running` → `outcome` (zero rows is fine: a supersession ended some)."""
    if not items:
        return 0
    ended = await transition_where(
        conn,
        "score_attempts",
        ["running"],
        outcome,
        reason=detail,
        where=_ATTEMPTS_OF_ITEMS,
        params=_pairs(items),
    )
    return len(ended)


async def _release_targets(conn: AsyncConnection, submission_id: int, items: Sequence[_Item]) -> int:
    """The submission's `in_batch` targets of the items → `pending` under the batch pin, their bulk rows
    first (P52); a target moved meanwhile (cancelled) is left as it is."""
    if not items:
        return 0
    await bulk.lock_for_targets(conn, sorted(i.target_id for i in items))
    moved = await transition_where(
        conn,
        "score_targets",
        ["in_batch"],
        "pending",
        where=_TARGETS_OF_ITEMS,
        params={"s": submission_id, **_pairs(items)},
    )
    return len(moved)


# ---- submit-start, the recordings and abandon --------------------------------------------------------------


async def _locked_started(
    conn: AsyncConnection, submission_id: int, operation: OperationClaim
) -> Row[tuple[object, ...]]:
    """The recording shape's first three steps: `active_config`, the operation owner, the submission under
    `state = 'submit_started'` and the claim (else `FencedOut`)."""
    await cycles.lock_active_scoring(conn)
    await lock_operation_owner(conn, operation)
    row = (await conn.execute(_LOCK_STARTED, {"s": submission_id, "claim": operation.claim_id})).first()
    if row is None:
        raise FencedOut(spend_ledger.SUBMISSION_OWNER)
    return row


async def submit_start(
    conn: AsyncConnection,
    submission_id: int,
    *,
    operation: OperationClaim,
    now: datetime,
    defer: Deferrer | None = None,
) -> Literal["submit_started", "abandoned"]:
    """Step 2 (module docstring); what the submission became. `FencedOut` for a lost claim or a submission no
    longer `preparing` under it."""
    active = await cycles.lock_active_scoring(conn)
    await lock_operation_owner(conn, operation)
    row = (await conn.execute(_LOCK_PREPARING, {"s": submission_id, "claim": operation.claim_id})).first()
    if row is None:
        raise FencedOut(spend_ledger.SUBMISSION_OWNER)
    operation_state = (await conn.execute(_BULK_OF, {"b": row.bulk_operation_id})).one().state
    fenced = await global_fence_active(conn)
    if (
        row.cancel_requested_at is not None
        or active.llm_tuple_id != row.llm_tuple_id
        or operation_state != "active"
        or fenced
    ):
        log.info(
            "batch_submit_obsolete",
            submission_id=submission_id,
            cancel_requested=row.cancel_requested_at is not None,
            tuple_active=active.llm_tuple_id == row.llm_tuple_id,
            bulk_operation_state=operation_state,
            global_fence=fenced,
        )
        await abandon(conn, submission_id, now=now, defer=defer)
        return "abandoned"
    await afault_point(BEFORE_SUBMIT_START)
    await transition(conn, _SUBMISSIONS, submission_id, ["preparing"], "submit_started")
    entries = [i.reservation for i in await _items(conn, submission_id)]
    await spend_ledger.mark_batch_dispatched(
        conn, BatchSubmissionOwner(submission_id, "submit_started"), entries, now=now
    )
    log.info("batch_submit_started", submission_id=submission_id, request_count=len(entries))
    return "submit_started"


async def check_sendable(conn: AsyncConnection, submission_id: int, *, operation: OperationClaim) -> None:
    """Right before the send: the operation owner and the submission `submit_started` under its claim, else
    `FencedOut` (a reclaimed execution sends nothing; its row is the recovery's)."""
    await lock_operation_owner(conn, operation)
    if (await conn.execute(_LOCK_STARTED, {"s": submission_id, "claim": operation.claim_id})).first() is None:
        raise FencedOut(spend_ledger.SUBMISSION_OWNER)


async def record_created(
    conn: AsyncConnection, submission_id: int, created: Created, *, operation: OperationClaim, now: datetime
) -> bool:
    """Step 4a (module docstring); whether a cancel was requested meanwhile (the caller sends it after the
    commit)."""
    row = await _locked_started(conn, submission_id, operation)
    await transition(
        conn,
        _SUBMISSIONS,
        submission_id,
        ["submit_started"],
        "submitted",
        set_={
            "provider_batch_id": created.provider_batch_id,
            "expires_at": created.created_at + BATCH_TTL,
        },
    )
    log.info(
        "batch_submitted",
        submission_id=submission_id,
        provider_batch_id=created.provider_batch_id,
        cancel_requested=row.cancel_requested_at is not None,
        at=now.isoformat(),
    )
    return row.cancel_requested_at is not None


async def record_rejected(
    conn: AsyncConnection,
    submission_id: int,
    rejected: Rejected,
    *,
    operation: OperationClaim,
    now: datetime,
    defer: Deferrer | None = None,
) -> None:
    """Step 4b (module docstring)."""
    await _locked_started(conn, submission_id, operation)
    await transition(conn, _SUBMISSIONS, submission_id, ["submit_started"], "submit_rejected")
    items = await _items(conn, submission_id)
    never_sent = rejected.evidence == CONNECT_FAILED_BEFORE_SEND
    call = CallOutcome(
        "error", http_status=rejected.status, error_code=CONNECT_FAILED if never_sent else SUBMIT_REJECTED
    )
    for item in items:  # the month row and the ledger entries (the bulk row of each funded one between)
        await spend_ledger.settle(
            conn,
            item.reservation,
            outcome="not_billed",
            evidence=rejected.evidence,
            usage=None,
            call=call,
            now=now,
        )
    for item in items:  # the bulk row, then each item
        await bulk.release_item_hold(conn, item_id=item.id)
    moved = await _release_targets(conn, submission_id, items)  # the targets before the attempts (T16-c)
    ended = await _end_attempts(conn, items, "submit_rejected", None)
    if rejected.status in AUTH_FAILED_STATUSES:  # the key was refused: the lane pauses on it
        await raise_auth_failed(conn, http_status=rejected.status)
    # a not-sent rejection's follow-on is the reconciler's (Ruling T16-f): the provider asked for time
    requested = await follow_on(conn, submission_id=submission_id, defer=defer, request=not rejected.not_sent)
    await ranking.bump(conn)  # step 11, last
    log.warning(
        "batch_submit_rejected",
        submission_id=submission_id,
        http_status=rejected.status,
        error_type=rejected.error_type,
        evidence=rejected.evidence,
        items=len(items),
        attempts=ended,
        targets=moved,
        follow_on=requested,
    )


async def record_ambiguous(
    conn: AsyncConnection,
    submission_id: int,
    ambiguous: Ambiguous,
    *,
    operation: OperationClaim,
    now: datetime,
) -> None:
    """Step 4c (module docstring): nothing but the submission moves."""
    await _locked_started(conn, submission_id, operation)
    await _ambiguous(conn, submission_id, ambiguous.error_class)
    log.info("batch_create_unresolved", submission_id=submission_id, at=now.isoformat())


async def _ambiguous(conn: AsyncConnection, submission_id: int, error_class: str) -> None:
    await transition(
        conn,
        _SUBMISSIONS,
        submission_id,
        ["submit_started"],
        "submit_ambiguous",
        set_={"reconcile_report": {"error_class": error_class}},
    )
    log.warning("batch_submit_ambiguous", submission_id=submission_id, error_class=error_class)


async def abandon(
    conn: AsyncConnection, submission_id: int, *, now: datetime, defer: Deferrer | None = None
) -> bool:
    """Step 4d (module docstring) in the caller's transaction; whether it abandoned (`False` for a submission
    no longer `preparing`)."""
    await cycles.lock_active_scoring(conn)
    locked = (await conn.execute(_LOCK_IN_STATE, {"s": submission_id, "state": "preparing"})).first()
    if locked is None:
        return False
    await transition(conn, _SUBMISSIONS, submission_id, ["preparing"], "abandoned")
    items = await _items(conn, submission_id)
    for item in items:  # the month row, the bulk debit and the ledger entries
        await spend_ledger.release_never_dispatched(conn, item.reservation, now=now)
    for item in items:
        await bulk.release_item_hold(conn, item_id=item.id)
    moved = await _release_targets(conn, submission_id, items)  # the targets before the attempts (T16-c)
    ended = await _end_attempts(conn, items, "cancelled", ABANDONED_DETAIL)
    requested = await follow_on(conn, submission_id=submission_id, defer=defer)
    await ranking.bump(conn)  # step 11, last
    log.warning(
        "batch_abandoned",
        submission_id=submission_id,
        items=len(items),
        attempts=ended,
        targets=moved,
        follow_on=requested,
    )
    return True


# ---- the recovery of a lost execution and the follow-on ----------------------------------------------------


@dataclass(frozen=True)
class Recovered:
    """What a recovery moved: submissions abandoned, and made ambiguous."""

    abandoned: int
    ambiguous: int


async def recover_lost(
    conn: AsyncConnection,
    *,
    epoch: int,
    operation_id: int | None = None,
    now: datetime,
    defer: Deferrer | None = None,
) -> Recovered:
    """The submissions of lost executions (module docstring), in the caller's transaction, which holds
    `fence_epoch` and `active_config FOR SHARE` already."""
    rows = (await conn.execute(_LOST, {"epoch": epoch, "operation": operation_id})).all()
    abandoned = ambiguous = 0
    for row in rows:
        if row.state == "preparing":
            abandoned += await abandon(conn, int(row.id), now=now, defer=defer)
        else:
            await _ambiguous(conn, int(row.id), EXECUTION_LOST)
            ambiguous += 1
    if rows:
        log.warning(
            "batch_execution_lost",
            epoch=epoch,
            operation_id=operation_id,
            submission_ids=[int(row.id) for row in rows],
            abandoned=abandoned,
            ambiguous=ambiguous,
        )
    return Recovered(abandoned, ambiguous)


async def follow_on(
    conn: AsyncConnection, *, submission_id: int, defer: Deferrer | None, request: bool = True
) -> bool:
    """The follow-on of an ended submission, once (module docstring); whether an operation was recorded now.
    With `request=False` (a not-sent rejection, Ruling T16-f) the follow-on is marked handled, and the
    `bulk_operations` reconciler requests the batch (`rerequest`)."""
    row = (await conn.execute(_FOLLOW_ON_OF, {"s": submission_id})).first()
    if row is None:
        return False
    ends = (row.state in _FOLLOW_ON_STATES and row.released_at is None) or row.state == "released"
    if not ends:
        return False
    if (await conn.execute(_MARK_FOLLOW_ON, {"s": submission_id})).first() is None:
        return False  # requested once already
    if (await conn.execute(_BULK_OF, {"b": row.bulk_operation_id})).one().state != "active":
        log.info(
            "batch_follow_on_skipped", submission_id=submission_id, bulk_operation_id=row.bulk_operation_id
        )
        return False
    if not request:
        log.info(
            "batch_follow_on_left_to_reconciler",
            submission_id=submission_id,
            bulk_operation_id=row.bulk_operation_id,
        )
        return False
    bulk_operation_id, llm_tuple_id = int(row.bulk_operation_id), int(row.llm_tuple_id)
    operation_id, _ = await create_operation(
        conn,
        kind=KIND,
        payload={
            "bulk_operation_id": bulk_operation_id,
            "llm_tuple_id": llm_tuple_id,
            "after_submission_id": submission_id,
        },
        dedup_key=f"{KIND}:{bulk_operation_id}:after:{submission_id}",
        defer=defer,
    )
    log.info("batch_follow_on_requested", submission_id=submission_id, operation_id=operation_id)
    return True


@bulk.register_active_step
async def rerequest(conn: AsyncConnection, bulk_operation_id: int, ctx: ReconcileContext) -> bool:
    """Ruling T16-d (module docstring): the batch of the `active` bulk operation requested again, in the
    `bulk_operations` reconciler's transaction (`fence_epoch` and `active_config FOR SHARE` held), after the
    exhaustion sweep of its tuple (Carry T16-h); whether it repaired something (a target ended, the operation
    completed, or a request recorded).

    The pass has locked this operation's bulk row (`complete_if_done`) before the sweep locks, in id order,
    the bulk rows of its candidates' unreleased follow-up items (`bulk.lock_for_targets`). That is out of id
    order only if one of those rows were another operation's, and today none can be: at most one operation is
    `active` (`bulk_operations_one_active`); the completion stamps every item of the operation it completes;
    an operation is cancelled only by a tuple supersession, which cancels its tuple's targets and so releases
    their holds in that transaction; and a target is its tuple's alone, so a candidate of this tuple has no
    item of another tuple's operation. (The candidates are `pending` or `budget_deferred`, never `in_batch`;
    an `in_batch` target has no unreleased `followup` item anyway, Carry T10-b.) So every row the sweep locks
    is this one, held already. Both invariants are pinned (`test_batch_decisions`)."""
    row = (await conn.execute(_BULK_OF, {"b": bulk_operation_id})).first()
    if row is None or row.state != "active":
        return False
    config = await cycles.scoring_config(conn)
    if config.active.llm_tuple_id != row.llm_tuple_id:
        return False
    llm_tuple_id = int(row.llm_tuple_id)
    # Carry T16-h: the targets a rejection's charge used up end here, as the follow-on's handler would sweep
    # them (a not-sent rejection requests none, Ruling T16-f), so their operation can complete in this pass
    exhausted = await targets.exhaustion_sweep(conn, llm_tuple_id=llm_tuple_id)
    if exhausted and await bulk.complete_if_done(conn, bulk_operation_id, now=ctx.now):
        requested = False
    else:
        requested = await _request_again(conn, bulk_operation_id, llm_tuple_id, config, ctx)
    if exhausted:
        await ranking.bump(conn)  # step 11, last
    return requested or exhausted > 0


async def _request_again(
    conn: AsyncConnection,
    bulk_operation_id: int,
    llm_tuple_id: int,
    config: cycles.ScoringConfig,
    ctx: ReconcileContext,
) -> bool:
    """`rerequest`'s request (module docstring): none when batches are disabled, the lane is paused, a
    submission is in flight, a `batch_submit` or the target creation is live, or too few targets are
    eligible."""
    scoring = (await active_settings(conn)).scoring
    if not scoring.batch_bulk_rescore:
        return False
    # the schema check is the prepare's (`fake_llm` is the transport's, which the reconciler does not know)
    if await cycles.pause_reason(conn, fake_llm=True, now=ctx.now, config=config) is not None:
        return False
    if await _in_flight(conn) is not None:
        return False
    if (await conn.execute(_LIVE_SUBMIT, {"b": bulk_operation_id})).scalar_one():
        return False
    if await bulk.creation_live(conn, bulk_operation_id):  # the first batch is the creation's to request
        return False
    params = {
        "b": bulk_operation_id,
        "policy": config.active.evaluation_policy_id,
        "n": scoring.batch_min_targets,
    }
    if int((await conn.execute(_ELIGIBLE_COUNT, params)).scalar_one()) < scoring.batch_min_targets:
        return False
    operation_id, created = await create_operation(
        conn,
        kind=KIND,
        payload={"bulk_operation_id": bulk_operation_id, "llm_tuple_id": llm_tuple_id},
        dedup_key=f"{KIND}:{bulk_operation_id}:resume",
        defer=ctx.defer,
    )
    if created:
        log.info("batch_rerequested", bulk_operation_id=bulk_operation_id, operation_id=operation_id)
    return created


# ---- the release of a quarantined submission (§10.10 recovery step 7) --------------------------------------


@dataclass(frozen=True)
class Released:
    """A quarantined submission released: the reservation its entries keep counted as `ambiguous`."""

    reserved_usd: Decimal


def releasable_params(now: datetime) -> dict[str, datetime]:
    """The binds of `RELEASABLE_SQL` at the caller's `now`."""
    return {"expired_before": now - RETRIEVE_FAILING_AFTER}


async def release(
    conn: AsyncConnection, submission_id: int, *, now: datetime, defer: Deferrer | None
) -> Released:
    """The user's release (module docstring) of a submission releasable at `now` (`RELEASABLE_SQL`, evaluated
    in the locking statement): a `submit_ambiguous` one, or a lost `submitted` one (plan M4 Task 16), in the
    caller's transaction, which holds `active_config FOR SHARE` and its idempotency row (the route's, Task
    21); `TransitionConflict` for any other."""
    params = {"s": submission_id, **releasable_params(now)}
    locked = (await conn.execute(_LOCK_RELEASABLE, params)).first()  # the owner
    if locked is None:
        raise TransitionConflict(_SUBMISSIONS, submission_id, "released")
    await transition(conn, _SUBMISSIONS, submission_id, list(_RELEASABLE_STATES), "released")
    items = await _items(conn, submission_id)
    call = CallOutcome("ambiguous", error_code=RELEASED_CODE)
    for item in items:  # the month row, the bulk row of each funded entry, the ledger entries
        await spend_ledger.settle(
            conn, item.reservation, outcome="ambiguous", evidence=None, usage=None, call=call, now=now
        )
    for item in items:  # the bulk row, then each item
        await bulk.release_item_hold(conn, item_id=item.id)
    moved = await _release_targets(conn, submission_id, items)  # the targets before the attempts (T16-c)
    ended = await _end_attempts(conn, items, "released", None)
    resolved = [
        code
        for code in _RELEASE_RESOLVES[str(locked.state)]
        if await resolve_alert(conn, code=code, subject=str(submission_id))
    ]
    requested = await follow_on(conn, submission_id=submission_id, defer=defer)
    await ranking.bump(conn)  # step 11, last
    reserved = Decimal(locked.reserved_usd)
    log.warning(
        "batch_released",
        submission_id=submission_id,
        origin=str(locked.state),
        items=len(items),
        attempts=ended,
        targets=moved,
        reserved_usd=str(reserved),
        alerts_resolved=resolved,
        follow_on=requested,
        at=now.isoformat(),
    )
    return Released(reserved)


async def quarantined_usd(conn: AsyncConnection) -> Decimal:
    """The reserved spend of the `submit_ambiguous` submissions (S:L1199), by `bulk.QUARANTINED_USD_SQL`,
    the one statement `fail`'s `llm.cap_reached` context uses too; read without a lock."""
    return Decimal((await conn.execute(_QUARANTINED)).scalar_one())


async def any_lost(conn: AsyncConnection, *, epoch: int) -> bool:
    """Whether a submission of a lost execution exists (`recover_lost`'s predicate), read without a lock: the
    tick reconciler's probe (`scoring.batch_operations`)."""
    return (await conn.execute(_ANY_LOST, {"epoch": epoch})).first() is not None
