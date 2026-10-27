"""The score lane's batch submissions (spec §10.10, §7.12, §6.9.3, §12.6 (e); plan Tasks 14 and 16, Rulings
P14, P16, P52, T7-c, T7-d, T16-a, T16-c, T16-d, Codex r1 #18, #22-#25, r2 B7-B15, r3 B1-B3, r4 B1, B8, B9,
r5 B1, A1 issue 17,
A2 B10, B13, B14, Deviations 12, 13, 27 and 38): Task 14 records the cancellation a tuple supersession asks
for; Task 16 adds the submission lifecycle up to the provider's answer (Task 17 polls, decides the items,
adopts an ambiguous submission and releases a quarantined one).

**The cancellation** (Task 14). `request_cancel(conn, *, llm_tuple_id, now) -> RequestedCancel`, in the
caller's transaction (the tuple activation's, which holds `active_config FOR UPDATE` and has already taken the
month and bulk rows, so no batch-lane transaction runs meanwhile):
1. `cancel_requested_at := now` on **every** non-terminal submission of the tuple (`IN_FLIGHT`: `preparing`,
   `submit_started`, `submit_ambiguous`, `submitted`, `ended`; Codex r1 #24), whatever its phase; a request
   recorded earlier keeps its time. This record is the request: the worker calls `messages.batches.cancel`
   after the commit, as soon as a provider id is known (`record_created` below for one created during the
   request, `batch_poll` for a `submitted` one still in progress, Task 17). A submission's state does not
   change here;
2. in the same transaction, their `in_batch` targets → `cancelled('tuple_superseded')` with
   `batch_submission_id` cleared, their running attempts ended `cancelled`, each target's follow-up hold
   released (`targets.cancel_batched`: the bulk rows first, Ruling P52). An item that arrives later for such a
   target is fenced (`ignored_fenced`, Task 17).

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
serialization failure, a lost connection) up to `DB_RETRY.max_attempts` tries with its interval; the owner
predicate makes each try the same recording, and a row whose tries ran out stays `submit_started`, the
recovery's:
- `record_created`: `submit_started → submitted` with `provider_batch_id` and `expires_at = created_at + 24
  h` (L3954); whether a cancel was requested meanwhile (the worker then calls `messages.batches.cancel` after
  the commit; `batch_poll` retries it, Task 17);
- `record_rejected`: `submit_started → submit_rejected`; every item's entry settled `not_billed` on the
  rejection's evidence (`batch_submit_rejected`, the call `error` with that code and the rejection's status;
  `connect_failed_before_send` for a create never sent, the call `connect_failed` without a status, Ruling
  T16-a); every item's hold released (`bulk.release_item_hold`); the submission's `in_batch` targets →
  `pending` (the batch pin `batch_submission_id = :s AND attempt_no = :a`; a cancelled target is left as it
  is), their bulk rows first (P52); the attempts still `running` → `submit_rejected`; for a 401 or 403 the
  critical `llm.auth_failed`, raised as the gateway raises it (`gateway.raise_auth_failed`), so the next
  prepare is paused (`cycles.pause_reason`); the follow-on. The attempts stay charged (Ruling T7-d,
  Deviation 13): a request the provider always rejects exhausts its targets instead of resubmitting forever;
- `record_ambiguous`: `submit_started → submit_ambiguous` with `reconcile_report = {"error_class": …}`;
  nothing else moves (L3956): the targets stay `in_batch`, the spend reserved and counted. The release of a
  quarantined submission, and with it the ambiguous entries' cost on their attempts (Carry T7-b, as the
  ledger reconciler's `_mark_ambiguous` adds it), is Task 17's.

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
resent**, whether the execution died before or after the HTTP send (the ambiguous reconciler of Task 17 finds
the batch if it was created, or the user releases it). The caller holds `fence_epoch` and `active_config FOR
SHARE` first. It runs in the takeover sweep `batch_submissions` (today in the takeover's transaction; Task 27
gives each sweep its own), in the `batch_submit` handler's first transaction (its own operation's rows), and
in the tick reconciler `batch_submissions`, which takes no lock on a pass whose lock-free read finds no
candidate. An operation's failure hook cannot do it (the hook runs after the operation row, and
`active_config` comes first), so a row left by an operation that ended `failed` waits for the next tick.

**The follow-on** (`follow_on`, L3945, Codex r2 B15, A2 B13): keyed by the originating submission, once, when
it ends `submit_rejected`, `abandoned` or `reconciled` unreleased, or at the release of a `released` one
(never at its later adoption): the marker `follow_on_at` is set once, and with the bulk operation still
`active` the next `batch_submit` is recorded (`{"bulk_operation_id", "llm_tuple_id", "after_submission_id"}`,
dedup key `batch_submit:<bulk>:after:<s>`, its job under the `batch` lock, `OPERATION_LOCKS`).

**The request again** (`rerequest`, Ruling T16-d): a prepare that skipped (`paused`, `budget_exhausted`, ...)
ends its operation and requests no follow-on, so the `bulk_operations` reconciler (`bulk.reconcile_bulk_
operations`, through `bulk.register_active_step`) asks for the batch again on each heavy pass, in its
transaction (`fence_epoch`, `active_config FOR SHARE`), for the `active` bulk operation that it did not
complete: when batches are enabled, the lane is not paused (`cycles.pause_reason`; its schema check is the
prepare's, the reconciler knowing no transport), no submission is in flight (`IN_FLIGHT`, unreleased), no
`batch_submit` of the operation is `pending` or `running`, and at least `batch_min_targets` of its targets
are eligible, it records `{"bulk_operation_id", "llm_tuple_id"}` under the dedup key
`batch_submit:<bulk>:resume` (`create_operation`: one live request at most). The prepare re-checks every
gate.

**The `batch_submit` handler**, payload `{"bulk_operation_id", "llm_tuple_id"}`: (1) `recover_lost` of its own
operation, (2) the four sweeps of the tuple (expiry, refilter, regroup and exhaustion, L3939, Deviation 7) in
a transaction of their own, (3) `prepare` (a `NotPrepared` ends the operation `succeeded` with `progress =
{"skipped": reason}`), the fault point
`batch.after_prepare_commit`, (4) `submit_start` (an abandoned submission ends the operation), the fault
point `batch.after_submit_start`, (5) the owner and the `submit_started` row checked again (a reclaimed
execution sends nothing), (6) `BatchesClient.create` outside any transaction, the fault point
`batch.after_create`, (7) `record_created | record_rejected | record_ambiguous` (retried, above), then the
carried cancel, and (8) `finish_in(succeeded)` with `progress = {"submission_id", "state", "request_count"}`.
The client is built from today's `OperationDeps` fields (`cfg`, `gateway.http_client`, `clock`, and the
gateway's `monotonic` and `sleep` for the create's own retries; Task 27 moves it into
`OperationDeps.batches_client`).

Ids, counts, codes and amounts only reach a log line, a payload or a report: never a param or prompt text.
"""

import asyncio
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

from jobhunter.clock import utcnow
from jobhunter.db import ranking
from jobhunter.db.guards import global_fence_active
from jobhunter.db.sql import is_unique_violation, q
from jobhunter.db.transitions import transition, transition_where
from jobhunter.errors import FencedOut
from jobhunter.faults import afault_point
from jobhunter.graphs.sanitize import DB_RETRY, db_retry_interval, is_retryable_db_error
from jobhunter.llm import budget, bulk, spend_ledger
from jobhunter.llm.batches import (
    CONNECT_FAILED_BEFORE_SEND,
    SUBMIT_REJECTED,
    Ambiguous,
    BatchApiError,
    BatchesClient,
    BatchRequest,
    Created,
    CreateOutcome,
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
from jobhunter.observability.logging import get_logger
from jobhunter.scoring import cycles, steps, targets
from jobhunter.scoring.admission import MAX_SCORE_ATTEMPTS
from jobhunter.settings.dryrun import active_settings
from jobhunter.settings.model import Settings
from jobhunter.types import JSONValue, get_int
from jobhunter.worker.epoch import fence_epoch, register_takeover_sweep
from jobhunter.worker.operations import (
    OperationClaim,
    OperationContext,
    create_operation,
    lock_operation_owner,
    register_operation_handler,
    set_progress,
)
from jobhunter.worker.reconcilers import ReconcileContext, reconciler
from jobhunter.worker.tasks import Deferrer, defer_in_tx

# The submissions of a tuple that are not terminal (§7.16.6).
IN_FLIGHT: Final = ("preparing", "submit_started", "submit_ambiguous", "submitted", "ended")
KIND: Final = "batch_submit"
# Deviation 27, A2 B14: the rendered requests of one submission, below the provider's 256 MB.
BATCH_MAX_BYTES: Final = 200 * 1024 * 1024
BATCH_TTL: Final = timedelta(hours=24)  # L3954: `expires_at = created_at + 24 h`
# The fault points of the lifecycle (module docstring).
BEFORE_OWNER_LOCK: Final = "batch.before_owner_lock"
AFTER_CANDIDATES: Final = "batch.after_candidates"
AFTER_PREPARE_COMMIT: Final = "batch.after_prepare_commit"
BEFORE_SUBMIT_START: Final = "batch.before_submit_start"
AFTER_SUBMIT_START: Final = "batch.after_submit_start"
AFTER_CREATE: Final = "batch.after_create"
# The codes this module records.
EXECUTION_LOST: Final = "execution_lost"
ABANDONED_DETAIL: Final = "batch_abandoned"
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
_OWN_EPOCH: Final = q(
    "SELECT worker_epoch FROM operations WHERE id = :i AND claim_id = :c AND status = 'running'"
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
    savepoint that a `NotPrepared` (or an exception) rolls back. `FencedOut` for a lost claim;
    `BudgetExhausted` when the hard cap refuses an item; `gateway` renders the params."""
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
            ad=steps.revision_ad(
                row.llm_input,
                completeness=row.description_completeness,
                description_text=row.description_text,
            ),
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
    requested = await follow_on(conn, submission_id=submission_id, defer=defer)
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


async def follow_on(conn: AsyncConnection, *, submission_id: int, defer: Deferrer | None) -> bool:
    """The follow-on of an ended submission, once (module docstring); whether it was recorded now."""
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
    `bulk_operations` reconciler's transaction (`fence_epoch` and `active_config FOR SHARE` held); whether a
    request was recorded now."""
    row = (await conn.execute(_BULK_OF, {"b": bulk_operation_id})).first()
    if row is None or row.state != "active":
        return False
    scoring = (await active_settings(conn)).scoring
    if not scoring.batch_bulk_rescore:
        return False
    config = await cycles.scoring_config(conn)
    if config.active.llm_tuple_id != row.llm_tuple_id:
        return False
    # the schema check is the prepare's (`fake_llm` is the transport's, which the reconciler does not know)
    if await cycles.pause_reason(conn, fake_llm=True, now=ctx.now, config=config) is not None:
        return False
    if await _in_flight(conn) is not None:
        return False
    if (await conn.execute(_LIVE_SUBMIT, {"b": bulk_operation_id})).scalar_one():
        return False
    params = {
        "b": bulk_operation_id,
        "policy": config.active.evaluation_policy_id,
        "n": scoring.batch_min_targets,
    }
    if int((await conn.execute(_ELIGIBLE_COUNT, params)).scalar_one()) < scoring.batch_min_targets:
        return False
    llm_tuple_id = int(row.llm_tuple_id)
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


@register_takeover_sweep("batch_submissions")
async def sweep_lost_submissions(conn: AsyncConnection, epoch: int) -> int:
    """At takeover: the submissions of every lost execution recovered (module docstring), after
    `fence_epoch` and `active_config FOR SHARE`; how many."""
    await fence_epoch(conn, epoch)
    await cycles.lock_active_scoring(conn)
    recovered = await recover_lost(conn, epoch=epoch, now=utcnow(), defer=defer_in_tx)
    return recovered.abandoned + recovered.ambiguous


@reconciler("batch_submissions", "tick")
async def reconcile_batch_submissions(ctx: ReconcileContext) -> int:
    """The tick's recovery of lost executions (module docstring): a lock-free read first, then, only when it
    finds a candidate, `fence_epoch`, `active_config FOR SHARE` and `recover_lost`; how many it moved. The
    worker's own `FencedOut` propagates (Ruling FR-w), another owner's ends the pass with 0."""
    try:
        async with ctx.engine.connect() as conn:
            if (await conn.execute(_ANY_LOST, {"epoch": ctx.epoch})).first() is None:
                return 0
        async with ctx.engine.begin() as conn:
            await fence_epoch(conn, ctx.epoch)
            await cycles.lock_active_scoring(conn)
            recovered = await recover_lost(conn, epoch=ctx.epoch, now=ctx.now, defer=ctx.defer)
    except FencedOut as fenced:
        if fenced.worker_superseded:
            raise
        return 0
    return recovered.abandoned + recovered.ambiguous


# ---- the batch_submit handler ------------------------------------------------------------------------------


async def _sweep(conn: AsyncConnection, *, llm_tuple_id: int, now: datetime) -> None:
    """The four sweeps of the tuple, as online admission runs them (`admission.admit`'s pre-lock first)."""
    active = await cycles.lock_active_scoring(conn)
    if active.llm_tuple_id != llm_tuple_id:
        return  # the preparation ends tuple_superseded
    max_age = await targets.active_max_ad_age_days(conn)
    held = await targets.lock_hold_candidates(
        conn, llm_tuple_id=llm_tuple_id, max_ad_age_days=max_age, now=now
    )
    moved = (
        await targets.expiry_sweep(
            conn, llm_tuple_id=llm_tuple_id, max_ad_age_days=max_age, now=now, held=held
        )
    ).moved
    moved += (
        await targets.refilter_sweep(
            conn, llm_tuple_id=llm_tuple_id, scope_version_id=active.scope_version_id
        )
    ).moved
    moved += (await targets.regroup_sweep(conn, llm_tuple_id=llm_tuple_id)).moved
    moved += await targets.exhaustion_sweep(conn, llm_tuple_id=llm_tuple_id, held=held)
    if moved:
        await ranking.bump(conn)  # step 11, last


async def _end(ctx: OperationContext, conn: AsyncConnection, progress: Mapping[str, JSONValue]) -> None:
    await set_progress(conn, ctx.claim, progress)
    await ctx.finish_in(conn, status="succeeded")


@register_operation_handler(KIND)
async def batch_submit(ctx: OperationContext) -> None:
    """The `batch_submit` operation (module docstring)."""
    deps = ctx.deps
    if deps is None:
        raise RuntimeError("batch_submit needs the worker's operation dependencies")
    payload = dict(ctx.claim.payload)
    bulk_operation_id = get_int(payload, "bulk_operation_id")
    llm_tuple_id = get_int(payload, "llm_tuple_id")
    clock = deps.clock
    client = BatchesClient(
        deps.cfg,
        http_client=deps.gateway.http_client,
        clock=clock,
        monotonic=deps.gateway.monotonic,
        sleep=deps.gateway.sleep,
    )
    async with ctx.engine.begin() as conn:  # 1. this operation's lost executions
        epoch = (
            await conn.execute(_OWN_EPOCH, {"i": ctx.claim.operation_id, "c": ctx.claim.claim_id})
        ).scalar_one_or_none()
        if epoch is None:
            raise FencedOut("operation")
        await fence_epoch(conn, int(epoch))
        await cycles.lock_active_scoring(conn)
        await recover_lost(
            conn, epoch=int(epoch), operation_id=ctx.claim.operation_id, now=clock(), defer=defer_in_tx
        )
    if not client.configured:
        async with ctx.engine.begin() as conn:
            await ctx.finish_in(conn, status="failed", error_code="llm_not_configured")
        return
    async with ctx.engine.begin() as conn:  # 2. the sweeps
        await _sweep(conn, llm_tuple_id=llm_tuple_id, now=clock())
    async with ctx.engine.begin() as conn:  # 3. the preparation
        await cycles.lock_active_scoring(conn)
        prepared = await prepare(
            conn,
            bulk_operation_id=bulk_operation_id,
            llm_tuple_id=llm_tuple_id,
            operation=ctx.claim,
            settings=await active_settings(conn),
            fake_llm=deps.cfg.fake_llm,
            gateway=deps.gateway,
            now=clock(),
        )
        if isinstance(prepared, NotPrepared):
            await _end(ctx, conn, {"skipped": prepared.reason})
            return
    s = prepared.submission_id
    await afault_point(AFTER_PREPARE_COMMIT)
    async with ctx.engine.begin() as conn:  # 4. submit-start
        started = await submit_start(conn, s, operation=ctx.claim, now=clock(), defer=defer_in_tx)
    if started == "abandoned":
        async with ctx.engine.begin() as conn:
            await _end(ctx, conn, {"submission_id": s, "state": "abandoned"})
        return
    await afault_point(AFTER_SUBMIT_START)
    async with ctx.engine.begin() as conn:  # 5. still this execution's to send
        await check_sendable(conn, s, operation=ctx.claim)
    outcome = await client.create(prepared.requests)  # 6. outside any transaction
    await afault_point(AFTER_CREATE)
    state, cancel_due = await _record(ctx, s, outcome, now=clock())  # 7.
    if cancel_due and isinstance(outcome, Created):
        try:
            await client.cancel(outcome.provider_batch_id)
        except BatchApiError as failure:  # batch_poll sends it again (Task 17)
            log.warning(
                "batch_cancel_failed", submission_id=s, error_class=failure.error_class, status=failure.status
            )
    async with ctx.engine.begin() as conn:  # 8.
        await _end(ctx, conn, {"submission_id": s, "state": state, "request_count": prepared.request_count})


async def _record(
    ctx: OperationContext, submission_id: int, outcome: CreateOutcome, *, now: datetime
) -> tuple[str, bool]:
    """The recording transaction of the create's outcome, retried on a retryable database error (module
    docstring); the submission's state and whether a cancel is due."""
    tries = 0
    while True:
        tries += 1
        try:
            return await _record_once(ctx, submission_id, outcome, now=now)
        except sa.exc.DBAPIError as exc:
            if tries >= DB_RETRY.max_attempts or not is_retryable_db_error(exc):
                raise
            log.warning(
                "batch_record_retried",
                submission_id=submission_id,
                tries=tries,
                error_class=type(exc).__name__,
            )
        await asyncio.sleep(db_retry_interval(tries))


async def _record_once(
    ctx: OperationContext, submission_id: int, outcome: CreateOutcome, *, now: datetime
) -> tuple[str, bool]:
    async with ctx.engine.begin() as conn:
        match outcome:
            case Created():
                due = await record_created(conn, submission_id, outcome, operation=ctx.claim, now=now)
                return "submitted", due
            case Rejected():
                await record_rejected(
                    conn, submission_id, outcome, operation=ctx.claim, now=now, defer=defer_in_tx
                )
                return "submit_rejected", False
            case Ambiguous():
                await record_ambiguous(conn, submission_id, outcome, operation=ctx.claim, now=now)
                return "submit_ambiguous", False
