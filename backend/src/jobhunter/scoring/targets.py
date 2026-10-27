"""Score targets (spec §6.9.4, §7.11, §7.16.3, §8.7, §9.4, §9.6.3, §9.6.4, §9.7, §12.6, §13.3; plan Tasks 9,
13 and 14, Deviations 7, 24 and 38, R4, R5, R9, Codex r1 #19-#21, #24, #32, #35, #36, r2 B7, r4 B8, r5 B4,
Rulings P8, P9, P14, P28, P47, P52, Controller rulings and carries T10-f, T11-c, T12-a, T12-c, T13-a, T13-b,
T13-c): the SQL that more than one module of the score lane shares, the sweeps that move targets without a
user, the moves a user asks for, `budget_release`, and (Task 14) the creation, revival and cancellation of
targets, the tuple activation, the `create_targets_for_tuple` operation and the `missing_targets` reconciler.

`RESUMABLE_DRAFT_SQL` (Codex r2 B23, r4 B9, r5 B1) is the one predicate of "this target has a draft to resume
from": a `score_drafts` row on any attempt of the target under the evaluation policy `:policy`, whatever the
attempt's mode (an online attempt's draft or a batch draft). It is an SQL predicate correlated to the outer
alias `t` of `score_targets` and binds `:policy` only:
- `load_context` (the `score_target` subgraph, Task 9) reads it as a column of its own target row, under the
  claim predicate, and resumes the target's attempt chain when it holds;
- Task 16's `ELIGIBLE_SQL` negates it per candidate row, so a target that `load_context` would resume is
  never batched again.

**The sweeps**, each in the caller's transaction, each direction `transition_where` statements with a static
predicate over the tuple's targets (the expiry's forward move one per reason; a move that sets no reason of
its own one for the rows carrying `budget_exhausted` and one for the others, below). No sweep ever
moves a target whose `override` is not `none` (§7.16.3), the exhaustion sweep excepted (an override buys no
fifth attempt). Each statement locks the rows it moves in id order, `FOR UPDATE OF s SKIP LOCKED` (other
domain rows, step 10 of the lock schedule; a row another transaction holds is skipped this time and taken by
the next sweep, so a sweep never waits for a target row). No sweep bumps the ranking: its caller does, once,
last.
- `expiry_sweep(conn, *, llm_tuple_id, max_ad_age_days, now, held=None) -> ExpirySwept(expired, revived)`
  (§9.6.3):
  `pending | budget_deferred` → `expired_unscored('closed')` when the job is `closed`, else
  `expired_unscored('too_old')` when `coalesce(posted_at_effective, first_seen_at) < now - max_ad_age_days`;
  `stale` and `unknown` never expire; `expired_unscored` → `pending` with the reason cleared when neither
  holds (A2 A6). Every target the forward move expires has its follow-up hold released
  (`bulk.release_target_hold`, Ruling P9);
- `refilter_sweep(conn, *, llm_tuple_id, scope_version_id) -> RefilterSwept(filtered, unfiltered)`: `pending
  | budget_deferred | deferred_duplicate` whose job's `hard_filter_results` row of the scope failed →
  `filtered_out` with `filter_scope_version_id` (the group column cleared on leaving `deferred_duplicate`);
  `filtered_out` whose row passed → `pending` (A2 R7, Deviation 24). A job without a row moves nowhere: the
  move needs positive evidence. The scope apply's flip runs it with the new scope (Task 15);
  `refilter_targets_of_jobs(conn, job_ids, scope)` runs its two statements over the targets of `job_ids`
  alone, with the active tuple and scope of `scope` (`filters.ActiveScopes`): the det-facts change hook's
  move for its job and the completeness reconciler's for the jobs it recomputed (Task 15, Ruling P37);
- `regroup_sweep(conn, *, llm_tuple_id) -> RegroupSwept(deferred, undeferred)`: `pending | budget_deferred`
  members for which §8.7's deferral predicate holds (`DEFERRAL_ELIGIBLE_SQL`: a member of an active group
  whose representative is another job, its membership's `score_individually` false, with no conflicting fact
  against the representative, `CONFLICTING_FACTS_SQL`, Task 15; `groups.deferral_eligible` is the same
  predicate in Python) → `deferred_duplicate` with the group; `deferred_duplicate` targets for which it no
  longer holds for their group (dissolved, left, elected, `score_individually`, a conflict) → `pending`. The
  entering states guard the entry only, so an unchanged deferred member stays deferred across every sweep
  (Codex r1 #36);
- `exhaustion_sweep(conn, *, llm_tuple_id, held=None) -> int` (Deviation 7, Codex r1 #21): `pending |
  budget_deferred`
  with `charged_attempts >= MAX_SCORE_ATTEMPTS` → `failed_attempts_exhausted('attempts_exhausted')`, its
  follow-up hold released.
**The reasons** (Controller ruling T13-c). The refilter and regroup moves set no reason (§7.16.3 names none
for them) and keep the target's own (a batch retry's, L2898, which `load_context` and the batch eligibility
read), except the denial's `budget_exhausted`, which belongs to `budget_deferred`: a row carrying it has it
cleared. The reverse expiry and `budget_release` clear the reason; the forward expiry and the exhaustion set
their own. No sweep touches an `in_progress` target or its attempt (Controller carry T10-f).

**The follow-up holds** (Ruling P52). The two sweeps that release holds move only candidates whose bulk rows
(`bulk.lock_for_targets`, step 7) were locked before their transaction's first target row; a candidate a
move skips keeps its hold. Alone (the reconciler, a save), each sweep reads its candidates without a lock
and locks their bulk rows itself first. `admit` runs the four sweeps in one transaction with exhaustion
fourth, after the others locked target rows, so its pre-lock phase (`lock_hold_candidates`, Controller
carries T12-c and T13-a) reads both sweeps' candidates and locks their bulk rows once, in id order, before
the first sweep; the expiry and exhaustion sweeps then take those ids (`held`) as their candidates and lock
nothing more. A target that becomes a candidate after the pre-lock waits for the next cycle.

**The user's moves**, in the API's transaction (`active_config FOR SHARE`, step 2; the route's idempotency
row, step 3; then the target row `FOR UPDATE`, step 10; `ranking_state` last; never the month row, so none of
them can wait on `budget_release`, which holds the month row and skips a target row another transaction
holds: Codex r1 #19, #20):
- `score_now(conn, *, job_id, force, now, defer) -> ScoreNow | ScoreNowRefused` (§9.6.4): the job's
  current-revision target of the active tuple (`not_found` without one). `pending | budget_deferred |
  expired_unscored | deferred_duplicate` → `pending` with `override = 'score_now'`; `refused | failed_*` take
  the Retry edge (`charged_attempts := 0`, L2880); `filtered_out` only with `force` (`override =
  'score_anyway'`, else `filtered_requires_force`); any other state (`in_progress`, `in_batch`, published,
  `cancelled`) is `conflict_state`. Before the target moves, the 409 pre-check: the scoring headroom of
  `now`'s month (`spend_ledger.read_month_or_new`, no lock: a fresh month answers its zero row and writes
  none) below `r_first` is `budget_exhausted` and nothing changes (L3678; racy by design, the attempt's own
  reservation is the authority). Every move sets `override_at = now` and `lane = 'score_now'` and clears a
  transient backoff (`not_before`, its count kept: Controller ruling T11-c); a move out of `budget_deferred`,
  `expired_unscored` or a Retry state clears the reason of the state it leaves, and so does a move of a
  target that still carries `budget_exhausted` (Controller ruling T13-c); then
  `cycles.request_cycle("score_now")` (a running cycle's next claim round, or the waiting job, serves it) and
  `ranking.bump`;
- `retry(conn, *, target_id, now, defer)`: the Retry edge alone for `refused | failed_*` (`conflict_state`
  otherwise, `not_found` for no target), as above;
- `score_individually(conn, *, group_id, job_id, now) -> IndividualMember | ScoreNowRefused` (§8.7, L2878,
  Controller ruling T13-b): the membership's `score_individually := true` whether or not the member has an
  active-tuple target (the flag carries into its later revisions, L3271; `not_found` without the
  membership), and a `deferred_duplicate` target → `pending` with `override = 'individual'` and
  `override_at`; its lane is left alone and no cycle is requested: the member waits in normal admission
  (L3599, L5241), and its card offers Score now. A target in another state keeps it. The result names the
  group, which the route answers (200 `Group`, L4983), the member, and the target with its state after the
  move.

**`budget_release`** (§6.9.4 L1201, tick, `release_deferred`): `fence_epoch`; `active_config FOR SHARE`;
`ensure_month` and the month row `FOR UPDATE` (`lock_month`: the first tick of a fresh month creates the row
it releases against, Codex r4 B8); every open `llm.cap_reached` of an earlier month resolved
(`resolve_stale_cap_alerts`, Codex r5 B4); then `k = floor(scoring headroom / r_first)` and, when `k >= 1`,
the `k` oldest `budget_deferred` targets of the active tuple (`state_changed_at`) → `pending` with the reason
cleared (locked `FOR UPDATE SKIP LOCKED` by a statement of their own first, so a target another transaction
holds, a Score now's, is skipped and the release never waits for it), the current month's `llm.cap_reached`
resolved, and `ranking.bump` when a target moved. A tick that resolved an alert or released a target logs it,
with at most the first `LOGGED_IDS` ids.

**`expiry_sweep`** (heavy, L5513): `fence_epoch`, `active_config FOR SHARE`, the expiry sweep of the active
tuple with the active `max_ad_age_days`, and `ranking.bump` when it moved a target.

**Creation and revival** (§9.7, §7.16.3; Task 14). `create_or_revive(conn, *, job_id, job_revision_id,
llm_tuple_id, profile_version_id, scope_version_id, bulk_operation_id, now)`, in the caller's transaction,
which holds `active_config FOR SHARE` (or `FOR UPDATE`) and passes the active tuple, scope and profile:
1. the job row `FOR SHARE` (step 4) with `is_fenced(job_id, llm_tuple_id)`: `Skipped("tuple_inactive")` when
   the tuple is not the active one, `Skipped("fenced")` under a purge fence (L6358), `Skipped("merged")` for a
   merged job, `Skipped("not_current")` when the revision is not the job's current one (or the job is gone);
2. the upsert of L3694-3702 (`UNIQUE (job_revision_id, llm_tuple_id)`), `similarity` from
   `similarity.similarity_for`: a new row is `pending`; a conflicting row is revived only from `cancelled`
   with a reason of `REVIVABLE` (`tuple_superseded`, `revision_superseded`, `job_merged`), its `DO UPDATE
   SET` the edge's `transition_set_clause` with the fresh-life resets of R4 (Deviation 7: `charged_attempts`,
   `transient_failures`, `not_before`, `resume_draft_id`, `refusal_category`, the reason, `lane`,
   `override`) and the new `bulk_operation_id` and similarity; `attempt_no` is kept (the CAS counter). Any
   other existing row is `Unchanged` (`cancelled('purged')` never revives, L2905; a published target is
   terminal);
3. the initial-state correction of a created or revived row (L3704, L2867-2869): `filtered_out` with
   `filter_scope_version_id` when the job's `hard_filter_results` row of the scope failed (computed inline by
   `filters.refilter_job` when the row is missing, A1 issue 21), else `deferred_duplicate` with the group when
   §8.7's deferral predicate holds (`DEFERRAL_ELIGIBLE_SQL` with the entering state, `groups.deferrable`: a
   member flagged `score_individually` is created `pending`, L2906), else `pending`.
`ranking.bump` is the caller's, once, last. `RETAINED_JOB_SQL` (a current revision of an unmerged,
non-synthetic job, Codex r1 #32) and `NEEDS_TARGET_SQL` (no target of `:tuple` for it but a revivable one) are
the predicates over `jobs j` the creators share with node 9 and I5.

**Cancellation** (§7.16.3, Rulings P9, P52, T10-f). `cancel_targets(conn, *, llm_tuple_id=None, job_ids=None,
revision_ids=None, reason, now) -> Cancelled(targets, attempts)` moves every target the filters match (all of
them that are given) in `CANCELLABLE` or `in_progress` to `cancelled(reason)` (the claim, group and filter
columns cleared), ends their running attempts `cancelled` with `outcome_detail = reason` (a non-owner writes
no other outcome), and releases the follow-up hold of each (`bulk.release_target_hold`). An `in_batch` target
is the batch cancel's on a tuple supersession (`batches.request_cancel`, below) and cancelled individually
for a revision supersession, a merge or a purge (L3979). Never the targets' owner: the candidates are read
without a lock, their bulk rows locked (`bulk.lock_for_targets`, step 7), then the target rows `FOR UPDATE`
in id order (step 10), the attempts and the items after. `cancel_batched(conn, *, submission_ids, reason)` is
the same move for the `in_batch` targets of the given submissions.

**The tuple activation** (§12.6 (d)-(f) and (h), §13.3 `llm_tuple`; Deviation 38, Codex r1 #24, #35).
`activate_tuple(conn, *, old_tuple_id, new_tuple_id, settings, now, defer) -> Activated(bulk_operation_id,
create_operation_id, cancelled)` runs in the confirm's transaction after `_ACTIVATE`, and in the save's after
`_FLIP`, both holding `active_config FOR UPDATE` with the pointer already on the new tuple; a caller whose
tuple is unchanged (the same profile and policy, L4786) does not call it (every target exists). In the
schedule's order: (f) `ensure_month` and the month row (step 6), the active bulk operation cancelled (the old
tuple's; the singleton holds one) and the new tuple's opened (`bulk.open`, step 7); (d) `cancel_targets` of
the old tuple, reason `tuple_superseded`; (e) `batches.request_cancel` of the old tuple (the recorded request:
the worker calls `messages.batches.cancel` after the commit, A1 issue 17); the fault point
`activation.after_cancel`; (h) `create_operation(create_targets_for_tuple, {"llm_tuple_id",
"bulk_operation_id"}, dedup_key create_targets_for_tuple:<tuple>)`. A live operation of that key from an
earlier activation of the same tuple names a bulk operation cancelled since: it is cancelled (`superseded`)
and a new one recorded.

**`create_targets_for_tuple`** (§12.6 step 4, Codex r1 #32, A2 R4), payload
`{"llm_tuple_id", "bulk_operation_id"}`: the current revisions of retained jobs that need a target of the
tuple, in chunks of `CHUNK`, the jobs of `availability = 'active'` first, then those saved or applied
(`user_job_actions`), then the rest (`CREATION_ORDER`), each group walked by job id (a keyset, so every chunk
moves on and none sorts every retained job). Each chunk is one transaction: `active_config FOR SHARE` (a tuple
no longer active ends the operation `succeeded` with `progress = {"skipped": "tuple_superseded"}`), the
chunk's job rows `FOR SHARE` in id order, the owner row (`lock_operation_owner`), `create_or_revive` per job
into the operation's bulk operation (`create_chunk`, in job id order), `set_progress({"done", "total"})`,
`ranking.bump` when it created or revived one; then the fault point `create_targets.after_chunk`, after the
commit, so a crash resumes from what is durable. `total` (the retained current revisions) and the `done` it
starts from (those with a live target of the tuple already) are counted once per execution, in its first
transaction, and `done` then moves on by each chunk's creations and revivals. When every chunk is done, with
`scoring.batch_bulk_rescore` on and at least `batch_min_targets` targets of the bulk operation `pending` with
`override = 'none'` (L3708's "≥ 200"), the `batch_submit` operation `{"bulk_operation_id", "llm_tuple_id"}` is
recorded (dedup key `batch_submit:<bulk>:1`; its job takes the `batch` lock, `operations.OPERATION_LOCKS`),
and the operation ends `succeeded` in that transaction.

**`missing_targets`** (heavy, §17.4, §7.17 #2, L3718): `fence_epoch`, `active_config FOR SHARE`, then the
current revisions of retained jobs that need a target of the active tuple, by job id in chunks of `CHUNK`,
each chunk one transaction with its job rows `FOR SHARE` (a purge chunk holds a job `FOR UPDATE`, so it waits
and then sees the fence or the deleted job, C36), `create_or_revive` without a bulk operation and
`ranking.bump`. While the tuple's `create_targets_for_tuple` operation is live it leaves the creation to it.

Ids, counts, codes and amounts only reach a log line.
"""

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_FLOOR
from typing import Any, Final, Literal

from sqlalchemy import TextClause
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.clock import utcnow
from jobhunter.db import ranking
from jobhunter.db.sql import q
from jobhunter.db.transitions import (
    CLEAR_REASON,
    TRANSITIONS,
    transition,
    transition_set_clause,
    transition_where,
)
from jobhunter.errors import FencedOut
from jobhunter.faults import afault_point
from jobhunter.llm import budget, bulk, spend_ledger
from jobhunter.llm.budget import BudgetParams
from jobhunter.observability.logging import get_logger
from jobhunter.run_settings import scope_from_payload
from jobhunter.scoring import cycles, filters, similarity
from jobhunter.scoring.admission import MAX_SCORE_ATTEMPTS
from jobhunter.settings.model import Settings
from jobhunter.types import JSONValue, get_bool, get_int, get_object
from jobhunter.worker.epoch import fence_epoch
from jobhunter.worker.operations import (
    OperationContext,
    cancel_operation,
    create_operation,
    lock_operation_owner,
    register_operation_handler,
    set_progress,
)
from jobhunter.worker.reconcilers import ReconcileContext, reconciler
from jobhunter.worker.tasks import Deferrer, defer_in_tx

RESUMABLE_DRAFT_SQL: Final = (
    "EXISTS (SELECT 1 FROM score_attempts a JOIN score_drafts d ON d.attempt_id = a.id "
    "WHERE a.target_id = t.id AND a.evaluation_policy_id = :policy)"
)

ScoreNowCode = Literal["not_found", "filtered_requires_force", "conflict_state", "budget_exhausted"]

CLOSED: Final = "closed"
TOO_OLD: Final = "too_old"
# The fault point after `budget_release` moved its targets, before its alert and ranking writes (the tests
# hold it there with the month row and the released target rows locked).
RELEASED_POINT: Final = "budget_release.after_release"
ACCEPTED_STATES: Final = ("pending", "budget_deferred", "expired_unscored", "deferred_duplicate")
RETRYABLE_STATES: Final = (
    "refused",
    "failed_max_tokens",
    "failed_invalid_output",
    "failed_request",
    "failed_transient",
    "failed_attempts_exhausted",
)
# A user's move out of these states clears the reason of the state it leaves; a `pending`,
# `deferred_duplicate` or `filtered_out` target keeps the reason it carries (a batch retry's, L2898),
# except a carried `budget_exhausted`, which `_clears` drops (Ruling T13-c).
_REASON_CLEARED: Final = frozenset({"budget_deferred", "expired_unscored", *RETRYABLE_STATES})
_ENTERING: Final = ("pending", "budget_deferred")
_SCORE_NOW: Final = "score_now"
# The denial's reason (`budget_deferred('budget_exhausted')`), which no other state keeps (Ruling T13-c).
DENIED: Final = "budget_exhausted"
LOGGED_IDS: Final = 20  # the ids a `budget_release` log line names, at most

log = get_logger("jobhunter.scoring.targets")


@dataclass(frozen=True)
class ExpirySwept:
    """What the expiry sweep moved: expired, and returned to `pending`."""

    expired: int
    revived: int

    @property
    def moved(self) -> int:
        return self.expired + self.revived


@dataclass(frozen=True)
class RefilterSwept:
    """What the refilter sweep moved: out to `filtered_out`, and back to `pending`."""

    filtered: int
    unfiltered: int

    @property
    def moved(self) -> int:
        return self.filtered + self.unfiltered


@dataclass(frozen=True)
class RegroupSwept:
    """What the regroup sweep moved: deferred, and returned to `pending`."""

    deferred: int
    undeferred: int

    @property
    def moved(self) -> int:
        return self.deferred + self.undeferred


@dataclass(frozen=True)
class ScoreNow:
    """A user's move done: the target and its state after it."""

    target_id: int
    state: str


@dataclass(frozen=True)
class IndividualMember:
    """Score individually done: the group (whose `Group` the route answers), the member, and its
    active-tuple target with its state after the move (`None` for a member without one)."""

    group_id: int
    job_id: int
    target_id: int | None
    state: str | None


@dataclass(frozen=True)
class ScoreNowRefused:
    """A user's move refused, with nothing written: the API's error code."""

    code: ScoreNowCode


# ---- the sweeps ----------------------------------------------------------------------------------------


def _locked(predicate: str, *, with_job: bool = False) -> str:
    """The `where` of a sweep's move: the rows of `score_targets s` (with its job `j`) that `predicate`
    selects, locked in id order, skipping a row another transaction holds (module docstring); `FOR UPDATE OF
    s` locks the target rows only, never a job row."""
    source = "score_targets s JOIN jobs j ON j.id = s.job_id" if with_job else "score_targets s"
    # The caller's static predicate over the registry's table: no input reaches the statement.
    return f"t.id IN (SELECT s.id FROM {source} WHERE {predicate} ORDER BY s.id FOR UPDATE OF s SKIP LOCKED)"  # noqa: S608


_ENTERING_SQL: Final = "s.state IN ('pending', 'budget_deferred')"
_CANDIDATE: Final = "s.id = ANY(CAST(:ids AS bigint[]))"
# §9.6.3: the job is closed, or older than the cutoff by its posted date, else by its first sighting.
_EXPIRING: Final = "(j.availability = 'closed' OR coalesce(j.posted_at_effective, j.first_seen_at) < :cutoff)"
_EXPIRY_CANDIDATES: Final = q(
    "SELECT s.id FROM score_targets s JOIN jobs j ON j.id = s.job_id "  # noqa: S608 (the constants above)
    f"WHERE s.llm_tuple_id = :tuple AND {_ENTERING_SQL} AND s.override = 'none' AND {_EXPIRING} ORDER BY s.id"
)
_EXPIRE_CLOSED: Final = _locked(
    f"{_CANDIDATE} AND {_ENTERING_SQL} AND s.override = 'none' AND j.availability = 'closed'", with_job=True
)
_EXPIRE_OLD: Final = _locked(
    f"{_CANDIDATE} AND {_ENTERING_SQL} AND s.override = 'none' "
    "AND coalesce(j.posted_at_effective, j.first_seen_at) < :cutoff",
    with_job=True,
)
_REVIVE: Final = _locked(
    f"s.llm_tuple_id = :tuple AND s.state = 'expired_unscored' AND s.override = 'none' AND NOT {_EXPIRING}",
    with_job=True,
)
_RESULT: Final = (
    "EXISTS (SELECT 1 FROM hard_filter_results h WHERE h.job_id = s.job_id AND h.scope_version_id = :scope "
    "AND {passed}h.passed)"
)
_CARRIES_DENIAL: Final = f"s.state_reason IS NOT DISTINCT FROM '{DENIED}'"


@dataclass(frozen=True)
class _Split:
    """The `where` pair of a move that sets no reason of its own (module docstring, Ruling T13-c): the rows
    that carry the denial's reason (cleared), and the others (their reason kept)."""

    denied: str
    kept: str


def _split(predicate: str, *, suffix: str = "") -> _Split:
    return _Split(
        denied=_locked(f"{predicate} AND {_CARRIES_DENIAL}") + suffix,
        kept=_locked(f"{predicate} AND NOT {_CARRIES_DENIAL}") + suffix,
    )


@dataclass(frozen=True)
class _Refilter:
    """The refilter's two moves (module docstring): out to `filtered_out`, and back to `pending`."""

    out: _Split
    back: _Split


def _refilter(also: str = "") -> _Refilter:
    """The refilter's statements over the tuple's targets, those `also` selects besides (`AND …`)."""
    return _Refilter(
        out=_split(
            "s.llm_tuple_id = :tuple AND s.state IN ('pending', 'budget_deferred', 'deferred_duplicate') "
            f"AND s.override = 'none' AND {_RESULT.format(passed='NOT ')}{also}"
        ),
        back=_split(
            "s.llm_tuple_id = :tuple AND s.state = 'filtered_out' AND s.override = 'none' "
            f"AND {_RESULT.format(passed='')}{also}"
        ),
    )


_REFILTER: Final = _refilter()
# The same two statements limited to the targets of `:jobs` (`refilter_targets_of_jobs`, Task 15).
_REFILTER_OF_JOBS: Final = _refilter(" AND s.job_id = ANY(CAST(:jobs AS bigint[]))")


# §8.7's conflicting scoring-relevant facts of the member's job `mj` against the representative's `rj`
# (L3261, Task 15): different structured work-mode sets, different structured employment types (a set that is
# not structured on both sides says nothing), or different seniority tokens; sets compared as sets.
CONFLICTING_FACTS_SQL: Final = (
    "((mj.work_mode_source = 'structured' AND rj.work_mode_source = 'structured' "
    "AND NOT (mj.work_mode_possible @> rj.work_mode_possible "
    "AND rj.work_mode_possible @> mj.work_mode_possible)) "
    "OR (mj.employment_source = 'structured' AND rj.employment_source = 'structured' "
    "AND NOT (mj.employment_types @> rj.employment_types AND rj.employment_types @> mj.employment_types)) "
    "OR NOT (mj.seniority_tokens @> rj.seniority_tokens AND rj.seniority_tokens @> mj.seniority_tokens))"
)


def _deferral_eligible(*, own_group: bool) -> str:
    """§8.7's deferral predicate over `s`, without the entering states (`groups.deferral_eligible`, the same
    predicate in Python): a member of an active group whose representative is another job, its membership's
    `score_individually` false, with no conflicting fact against the representative (Task 15); with
    `own_group`, for the group the target is deferred to."""
    group = " AND dm.group_id = s.deferred_group_id" if own_group else ""
    return (
        "EXISTS (SELECT 1 FROM duplicate_group_members dm JOIN duplicate_groups dg ON dg.id = dm.group_id "  # noqa: S608
        "JOIN jobs mj ON mj.id = dm.job_id JOIN jobs rj ON rj.id = dg.representative_job_id "
        "WHERE dm.job_id = s.job_id AND dg.status = 'active' "
        f"AND dg.representative_job_id <> s.job_id AND NOT dm.score_individually{group} "
        f"AND NOT {CONFLICTING_FACTS_SQL})"
    )


DEFERRAL_ELIGIBLE_SQL: Final = _deferral_eligible(own_group=False)
_DEFER: Final = _split(
    f"s.llm_tuple_id = :tuple AND {_ENTERING_SQL} AND s.override = 'none' AND {DEFERRAL_ELIGIBLE_SQL}",
    suffix=" AND m.job_id = t.job_id",
)
_UNDEFER: Final = _split(
    "s.llm_tuple_id = :tuple AND s.state = 'deferred_duplicate' AND s.override = 'none' "
    f"AND NOT {_deferral_eligible(own_group=True)}"
)
_EXHAUSTED_SQL: Final = f"{_ENTERING_SQL} AND s.charged_attempts >= :max_attempts"
_EXHAUSTION_CANDIDATES: Final = q(
    f"SELECT s.id FROM score_targets s WHERE s.llm_tuple_id = :tuple AND {_EXHAUSTED_SQL} ORDER BY s.id"  # noqa: S608
)
_EXHAUST: Final = _locked(f"{_CANDIDATE} AND {_EXHAUSTED_SQL}")
_ACTIVE_SETTINGS: Final = q(
    "SELECT v.payload FROM active_config a JOIN settings_versions v ON v.id = a.settings_version_id "
    "WHERE a.id = 1"
)


def _cutoff(now: datetime, max_ad_age_days: int) -> datetime:
    return now - timedelta(days=max_ad_age_days)


async def _ids(conn: AsyncConnection, statement: TextClause, params: dict[str, object]) -> list[int]:
    return [int(i) for i in (await conn.execute(statement, params)).scalars()]


async def active_max_ad_age_days(conn: AsyncConnection) -> int:
    """The active settings' `scope.max_ad_age_days`, read without a lock: the caller holds `active_config`."""
    payload: JSONValue = (await conn.execute(_ACTIVE_SETTINGS)).scalar_one()
    return get_int(payload, "scope.max_ad_age_days")


def _logged(sweep: str, llm_tuple_id: int, **moved: int) -> None:
    if any(moved.values()):
        log.info("score_targets_swept", sweep=sweep, llm_tuple_id=llm_tuple_id, **moved)


async def _move(
    conn: AsyncConnection,
    from_states: Sequence[str],
    to_state: str,
    split: _Split,
    params: dict[str, object],
    **options: Any,
) -> list[RowMapping]:
    """A sweep's move that sets no reason of its own (module docstring, Ruling T13-c): the rows carrying the
    denial's reason have it cleared, the others keep theirs; every row it moved."""
    denied = await transition_where(
        conn,
        "score_targets",
        from_states,
        to_state,
        reason=CLEAR_REASON,
        where=split.denied,
        params=params,
        **options,
    )
    kept = await transition_where(
        conn, "score_targets", from_states, to_state, where=split.kept, params=params, **options
    )
    return [*denied, *kept]


# The targets of `:ids` with an unreleased follow-up item: the only ones a release changes.
_HOLDING: Final = q(
    "SELECT DISTINCT target_id FROM batch_items WHERE target_id = ANY(CAST(:ids AS bigint[])) "
    "AND decision = 'followup' AND hold_released_at IS NULL ORDER BY target_id"
)


async def _release_holds(conn: AsyncConnection, moved: Sequence[RowMapping]) -> None:
    """The follow-up hold of every target a terminal move (a sweep, a cancellation) moved, once each
    (`bulk.release_target_hold`, Ruling P9); the bulk rows are held already (P52)."""
    held = await _ids(conn, _HOLDING, {"ids": sorted(int(row["id"]) for row in moved)})
    for target_id in held:
        await bulk.release_target_hold(conn, target_id)


async def lock_hold_candidates(
    conn: AsyncConnection, *, llm_tuple_id: int, max_ad_age_days: int, now: datetime
) -> frozenset[int]:
    """`admit`'s pre-lock phase (module docstring, Controller carries T12-c and T13-a): the candidates of the
    expiry's forward move and of the exhaustion sweep, read without a lock, and the bulk rows of their
    unreleased follow-up items locked in one statement, in id order; the ids it covered (the two sweeps'
    `held`)."""
    expiring = await _ids(
        conn, _EXPIRY_CANDIDATES, {"tuple": llm_tuple_id, "cutoff": _cutoff(now, max_ad_age_days)}
    )
    exhausted = await _ids(
        conn, _EXHAUSTION_CANDIDATES, {"tuple": llm_tuple_id, "max_attempts": MAX_SCORE_ATTEMPTS}
    )
    candidates = sorted({*expiring, *exhausted})
    await bulk.lock_for_targets(conn, candidates)
    return frozenset(candidates)


async def expiry_sweep(
    conn: AsyncConnection,
    *,
    llm_tuple_id: int,
    max_ad_age_days: int,
    now: datetime,
    held: frozenset[int] | None = None,
) -> ExpirySwept:
    """§9.6.3's sweep in both directions (module docstring); the forward move takes `held`, the ids whose bulk
    rows the transaction's pre-lock locked, as its candidates, else reads and locks its own."""
    cutoff = _cutoff(now, max_ad_age_days)
    if held is None:
        candidates = await _ids(conn, _EXPIRY_CANDIDATES, {"tuple": llm_tuple_id, "cutoff": cutoff})
        await bulk.lock_for_targets(conn, candidates)  # step 7, before the first target row (P52)
    else:
        candidates = sorted(held)
    expired: list[RowMapping] = []
    if candidates:
        for reason, where, params in (
            (CLOSED, _EXPIRE_CLOSED, {"ids": candidates}),
            (TOO_OLD, _EXPIRE_OLD, {"ids": candidates, "cutoff": cutoff}),
        ):
            expired += await transition_where(
                conn,
                "score_targets",
                _ENTERING,
                "expired_unscored",
                reason=reason,
                where=where,
                params=params,
            )
        await _release_holds(conn, expired)
    revived = await transition_where(
        conn,
        "score_targets",
        ["expired_unscored"],
        "pending",
        reason=CLEAR_REASON,
        where=_REVIVE,
        params={"tuple": llm_tuple_id, "cutoff": cutoff},
    )
    swept = ExpirySwept(expired=len(expired), revived=len(revived))
    _logged("expiry", llm_tuple_id, expired=swept.expired, revived=swept.revived)
    return swept


async def _refiltered(
    conn: AsyncConnection, statements: _Refilter, params: dict[str, object], *, scope_version_id: int
) -> RefilterSwept:
    out = await _move(
        conn,
        ["pending", "budget_deferred", "deferred_duplicate"],
        "filtered_out",
        statements.out,
        params,
        set_={"filter_scope_version_id": scope_version_id},
    )
    back = await _move(conn, ["filtered_out"], "pending", statements.back, params)
    return RefilterSwept(filtered=len(out), unfiltered=len(back))


async def refilter_sweep(conn: AsyncConnection, *, llm_tuple_id: int, scope_version_id: int) -> RefilterSwept:
    """The refilter against the scope's `hard_filter_results`, both directions (module docstring)."""
    params: dict[str, object] = {"tuple": llm_tuple_id, "scope": scope_version_id}
    swept = await _refiltered(conn, _REFILTER, params, scope_version_id=scope_version_id)
    _logged("refilter", llm_tuple_id, filtered=swept.filtered, unfiltered=swept.unfiltered)
    return swept


async def refilter_targets_of_jobs(
    conn: AsyncConnection, job_ids: Sequence[int], scope: filters.ActiveScopes
) -> RefilterSwept:
    """The refilter sweep's two statements (`filtered_out ⇄ pending`, `override = 'none'`) limited to the
    unpublished targets of `job_ids` of the active tuple, by the active scope's `hard_filter_results`, both
    as `scope` names them (Task 15, Ruling P37): the live hook's for its job, and the completeness
    reconciler's for the jobs it recomputed. Nothing without an active tuple or jobs."""
    if scope.llm_tuple_id is None or not job_ids:
        return RefilterSwept(filtered=0, unfiltered=0)
    params: dict[str, object] = {
        "tuple": scope.llm_tuple_id,
        "scope": scope.scope_version_id,
        "jobs": sorted(set(job_ids)),
    }
    swept = await _refiltered(conn, _REFILTER_OF_JOBS, params, scope_version_id=scope.scope_version_id)
    _logged("refilter_jobs", scope.llm_tuple_id, filtered=swept.filtered, unfiltered=swept.unfiltered)
    return swept


async def regroup_sweep(conn: AsyncConnection, *, llm_tuple_id: int) -> RegroupSwept:
    """§8.7's deferral, both directions (module docstring)."""
    params: dict[str, object] = {"tuple": llm_tuple_id}
    deferred = await _move(
        conn,
        _ENTERING,
        "deferred_duplicate",
        _DEFER,
        params,
        from_sql="duplicate_group_members m",
        set_sql={"deferred_group_id": "m.group_id"},
    )
    undeferred = await _move(conn, ["deferred_duplicate"], "pending", _UNDEFER, params)
    swept = RegroupSwept(deferred=len(deferred), undeferred=len(undeferred))
    _logged("regroup", llm_tuple_id, deferred=swept.deferred, undeferred=swept.undeferred)
    return swept


async def exhaustion_sweep(
    conn: AsyncConnection, *, llm_tuple_id: int, held: frozenset[int] | None = None
) -> int:
    """The targets whose charged attempts are used up outside `in_progress`, ended (module docstring); how
    many. Its candidates are `held`, the ids whose bulk rows the transaction's pre-lock locked, else it reads
    and locks its own."""
    if held is None:
        candidates = await _ids(
            conn, _EXHAUSTION_CANDIDATES, {"tuple": llm_tuple_id, "max_attempts": MAX_SCORE_ATTEMPTS}
        )
        await bulk.lock_for_targets(conn, candidates)  # step 7, before the first target row (P52)
    else:
        candidates = sorted(held)
    if not candidates:
        return 0
    moved = await transition_where(
        conn,
        "score_targets",
        _ENTERING,
        "failed_attempts_exhausted",
        reason=cycles.ATTEMPTS_EXHAUSTED,
        where=_EXHAUST,
        params={"ids": candidates, "max_attempts": MAX_SCORE_ATTEMPTS},
    )
    await _release_holds(conn, moved)
    _logged("exhaustion", llm_tuple_id, exhausted=len(moved))
    return len(moved)


@reconciler("expiry_sweep", "heavy")
async def reconcile_expiry(ctx: ReconcileContext) -> int:
    """The expiry sweep of the active tuple with the active `max_ad_age_days` at `ctx.now` (module
    docstring); how many targets it moved. Fenced by the worker epoch first: the worker's own `FencedOut`
    propagates (Ruling FR-w), another owner's ends the pass with 0, having written nothing."""
    try:
        async with ctx.engine.begin() as conn:
            await fence_epoch(conn, ctx.epoch)
            active = await cycles.lock_active_scoring(conn)
            if active.llm_tuple_id is None:
                return 0
            swept = await expiry_sweep(
                conn,
                llm_tuple_id=active.llm_tuple_id,
                max_ad_age_days=await active_max_ad_age_days(conn),
                now=ctx.now,
            )
            if swept.moved:
                await ranking.bump(conn)  # step 11, last
    except FencedOut as fenced:
        if fenced.worker_superseded:
            raise  # `fence_epoch` logged it
        return 0
    return swept.moved


# ---- budget_release (§6.9.4) -------------------------------------------------------------------------------

# The `k` oldest, locked by a statement of their own: inside the move, a `LIMIT` that the plan evaluates again
# per row skips the rows the statement has moved already and takes the next ones.
_OLDEST_DEFERRED: Final = q(
    "SELECT id FROM score_targets WHERE llm_tuple_id = :tuple AND state = 'budget_deferred' "
    "ORDER BY state_changed_at, id LIMIT :k FOR UPDATE SKIP LOCKED"
)


async def release_deferred(conn: AsyncConnection, *, now: datetime) -> int:
    """`budget_release`'s work after its fence, in the caller's transaction (module docstring); how many
    targets it returned to `pending`."""
    config = await cycles.scoring_config(conn)  # step 2
    p = config.settings.budget
    month = budget.month_of(now)
    await spend_ledger.ensure_month(conn, month, p, now)
    row = await spend_ledger.lock_month(conn, month, p, now=now)  # step 6
    stale = await spend_ledger.resolve_stale_cap_alerts(conn, current=month)
    r_first = budget.r_first(config.model_policy, config.execution)
    headroom = budget.headroom(row, "scoring", p)
    k = int((headroom / r_first).to_integral_value(rounding=ROUND_FLOOR))
    released: list[int] = []
    resolved = False
    tuple_id = config.active.llm_tuple_id
    if k >= 1:
        if tuple_id is not None:
            # step 10: the k oldest locked, the rows another transaction holds skipped
            oldest = await _ids(conn, _OLDEST_DEFERRED, {"tuple": tuple_id, "k": k})
            moved = await transition_where(
                conn,
                "score_targets",
                ["budget_deferred"],
                "pending",
                reason=CLEAR_REASON,
                where="t.id = ANY(CAST(:ids AS bigint[]))",
                params={"ids": oldest},
            )
            released = sorted(int(r["id"]) for r in moved)
            await afault_point(RELEASED_POINT)
        resolved = await spend_ledger.resolve_cap_reached(conn, month)
        if released:
            await ranking.bump(conn)  # step 11, last
    if released or resolved or stale:
        log.info(
            "score_budget_released",
            month=month.isoformat(),
            k=k,
            released=len(released),
            cap_alert_resolved=resolved,
            stale_cap_alerts_resolved=stale,
            target_ids=released[:LOGGED_IDS],
        )
    return len(released)


@reconciler("budget_release", "tick")
async def reconcile_budget_release(ctx: ReconcileContext) -> int:
    """`release_deferred` at `ctx.now` in a transaction fenced by the worker epoch (module docstring); how
    many targets it released. The worker's own `FencedOut` propagates, another owner's ends the pass with
    0."""
    try:
        async with ctx.engine.begin() as conn:
            await fence_epoch(conn, ctx.epoch)
            return await release_deferred(conn, now=ctx.now)
    except FencedOut as fenced:
        if fenced.worker_superseded:
            raise
        return 0


# ---- the user's moves (§9.6.4, §7.16.3, §8.7) -------------------------------------------------------------

_JOB_TARGET: Final = q(
    "SELECT t.id, t.state, t.state_reason FROM score_targets t "
    "JOIN jobs j ON j.current_revision_id = t.job_revision_id "
    "WHERE j.id = :job AND j.merged_into_job_id IS NULL AND t.llm_tuple_id = :tuple FOR UPDATE OF t"
)
_TARGET: Final = q("SELECT id, state, state_reason FROM score_targets WHERE id = :t FOR UPDATE")
_MEMBER: Final = q(
    "SELECT job_id FROM duplicate_group_members WHERE group_id = :g AND job_id = :j FOR UPDATE"
)
_FLAG_MEMBER: Final = q(
    "UPDATE duplicate_group_members SET score_individually = true WHERE group_id = :g AND job_id = :j"
)


def _refused(code: ScoreNowCode, action: str, **ids: int) -> ScoreNowRefused:
    log.info("score_target_action_refused", action=action, code=code, **ids)
    return ScoreNowRefused(code)


async def _headroom_left(conn: AsyncConnection, config: cycles.ScoringConfig, *, now: datetime) -> bool:
    """The 409 pre-check (module docstring): whether `now`'s month still admits `r_first` for scoring."""
    p = config.settings.budget
    row = await spend_ledger.read_month_or_new(conn, budget.month_of(now), p, now=now)
    return budget.headroom(row, "scoring", p) >= budget.r_first(config.model_policy, config.execution)


def _clears(state: str, reason: str | None) -> bool:
    """Whether a user's move out of `state` clears the target's `reason`: when it belongs to the state the
    target leaves, or is the denial's (module docstring, Ruling T13-c); it is kept otherwise."""
    return state in _REASON_CLEARED or reason == DENIED


async def _to_pending(
    conn: AsyncConnection, target_id: int, state: str, reason: str | None, *, override: str, now: datetime
) -> None:
    """The locked target from `state` (with `reason`) to `pending` with `override`, as Score now and Retry set
    it (module docstring): the Retry edge resets the charged attempts."""
    set_: dict[str, object] = {
        "override": override,
        "override_at": now,
        "lane": _SCORE_NOW,
        "not_before": None,
    }
    if state in RETRYABLE_STATES:
        set_["charged_attempts"] = 0
    await transition(
        conn,
        "score_targets",
        target_id,
        [state],
        "pending",
        reason=CLEAR_REASON if _clears(state, reason) else None,
        set_=set_,
    )


async def _requested(
    conn: AsyncConnection, target_id: int, *, action: str, state: str, defer: Deferrer
) -> ScoreNow:
    """A move done: the `score_now` cycle requested and the ranking bumped (step 11, last)."""
    await cycles.request_cycle(conn, trigger="score_now", defer=defer)
    await ranking.bump(conn)
    log.info("score_target_action", action=action, target_id=target_id, from_state=state)
    return ScoreNow(target_id, "pending")


async def score_now(
    conn: AsyncConnection, *, job_id: int, force: bool, now: datetime, defer: Deferrer
) -> ScoreNow | ScoreNowRefused:
    """§9.6.4's Score now of the job's current-revision target of the active tuple (module docstring)."""
    config = await cycles.scoring_config(conn)  # step 2
    tuple_id = config.active.llm_tuple_id
    row = None
    if tuple_id is not None:
        row = (await conn.execute(_JOB_TARGET, {"job": job_id, "tuple": tuple_id})).first()
    if row is None:
        return _refused("not_found", "score_now", job_id=job_id)
    target_id, state, reason = int(row.id), str(row.state), row.state_reason
    if state in ACCEPTED_STATES or state in RETRYABLE_STATES:
        override = _SCORE_NOW
    elif state == "filtered_out" and force:
        override = "score_anyway"
    elif state == "filtered_out":
        return _refused("filtered_requires_force", "score_now", target_id=target_id)
    else:
        return _refused("conflict_state", "score_now", target_id=target_id)
    if not await _headroom_left(conn, config, now=now):
        return _refused("budget_exhausted", "score_now", target_id=target_id)
    await _to_pending(conn, target_id, state, reason, override=override, now=now)
    return await _requested(conn, target_id, action="score_now", state=state, defer=defer)


async def retry(
    conn: AsyncConnection, *, target_id: int, now: datetime, defer: Deferrer
) -> ScoreNow | ScoreNowRefused:
    """The user's Retry of a `refused` or `failed_*` target (L2880; module docstring)."""
    await cycles.lock_active_scoring(conn)  # step 2
    row = (await conn.execute(_TARGET, {"t": target_id})).first()
    if row is None:
        return _refused("not_found", "retry", target_id=target_id)
    state = str(row.state)
    if state not in RETRYABLE_STATES:
        return _refused("conflict_state", "retry", target_id=target_id)
    await _to_pending(conn, target_id, state, row.state_reason, override=_SCORE_NOW, now=now)
    return await _requested(conn, target_id, action="retry", state=state, defer=defer)


async def score_individually(
    conn: AsyncConnection, *, group_id: int, job_id: int, now: datetime
) -> IndividualMember | ScoreNowRefused:
    """§8.7's Score individually of the group member `job_id` (module docstring, Controller ruling T13-b)."""
    active = await cycles.lock_active_scoring(conn)  # step 2
    if (await conn.execute(_MEMBER, {"g": group_id, "j": job_id})).first() is None:
        return _refused("not_found", "score_individually", group_id=group_id, job_id=job_id)
    await conn.execute(_FLAG_MEMBER, {"g": group_id, "j": job_id})  # with or without a target (L3271)
    row = None
    if active.llm_tuple_id is not None:
        row = (await conn.execute(_JOB_TARGET, {"job": job_id, "tuple": active.llm_tuple_id})).first()
    if row is None or row.state != "deferred_duplicate":
        target_id = None if row is None else int(row.id)
        state = None if row is None else str(row.state)
        log.info("score_target_action", action="score_individually", target_id=target_id, from_state=state)
        return IndividualMember(group_id, job_id, target_id, state)
    target_id = int(row.id)
    await transition(
        conn,
        "score_targets",
        target_id,
        ["deferred_duplicate"],
        "pending",
        reason=CLEAR_REASON if _clears("deferred_duplicate", row.state_reason) else None,
        set_={"override": "individual", "override_at": now},
    )
    await ranking.bump(conn)  # step 11, last
    log.info("score_target_action", action="score_individually", target_id=target_id, from_state=row.state)
    return IndividualMember(group_id, job_id, target_id, "pending")


# ---- creation and revival (§9.7, §7.16.3) ------------------------------------------------------------------

SkipReason = Literal["fenced", "merged", "not_current", "tuple_inactive"]
CancelReason = Literal["tuple_superseded", "revision_superseded", "job_merged", "purged"]

TUPLE_SUPERSEDED: Final = "tuple_superseded"
REVISION_SUPERSEDED: Final = "revision_superseded"
# The reasons a cancelled target revives from (§7.16.3); `purged` never does (L2905).
REVIVABLE: Final = ("tuple_superseded", "revision_superseded", "job_merged")
# The states a supersession, a merge or a purge cancels besides `in_progress` and `in_batch` (§7.16.3): the
# registry's edges into `cancelled`.
CANCELLABLE: Final = tuple(
    sorted(
        source
        for source, to in TRANSITIONS["score_targets"].allowed
        if to == "cancelled" and source not in ("in_progress", "in_batch")
    )
)
CHUNK: Final = 500  # revisions per transaction of node 9, `create_targets_for_tuple` and `missing_targets`
AFTER_CANCEL_POINT: Final = "activation.after_cancel"
AFTER_CHUNK_POINT: Final = "create_targets.after_chunk"
CREATE_TARGETS: Final = "create_targets_for_tuple"
_SUPERSEDED_OPERATION: Final = "superseded"

_REVIVABLE_SQL: Final = "(" + ", ".join(f"'{reason}'" for reason in REVIVABLE) + ")"
# A current revision of a retained job (`jobs j`): unmerged, not an adversarial golden case (Codex r1 #32).
RETAINED_JOB_SQL: Final = (
    "j.merged_into_job_id IS NULL AND j.current_revision_id IS NOT NULL AND NOT j.synthetic_eval"
)
# `jobs j`'s current revision has no target of `:tuple`, or only one a creation revives.
NEEDS_TARGET_SQL: Final = (
    "NOT EXISTS (SELECT 1 FROM score_targets n WHERE n.job_revision_id = j.current_revision_id "  # noqa: S608
    f"AND n.llm_tuple_id = :tuple AND NOT (n.state = 'cancelled' AND n.state_reason IN {_REVIVABLE_SQL}))"
)


@dataclass(frozen=True)
class Created:
    """A new target, in its initial state."""

    target_id: int
    state: str


@dataclass(frozen=True)
class Revived:
    """A cancelled target given a fresh life, in its corrected state."""

    target_id: int
    state: str


@dataclass(frozen=True)
class Unchanged:
    """An existing target the creation left as it is (live, published, or purged)."""

    target_id: int
    state: str


@dataclass(frozen=True)
class Skipped:
    """No target touched: the job is fenced or merged, the revision is not its current one, or the tuple is
    not the active one."""

    reason: SkipReason


Creation = Created | Revived | Unchanged | Skipped


@dataclass(frozen=True)
class Cancelled:
    """What a cancellation moved: targets, and the running attempts it ended."""

    targets: int
    attempts: int


@dataclass(frozen=True)
class Activated:
    """A tuple activation: the new bulk operation, the `create_targets_for_tuple` operation and how many
    targets of the old tuple it cancelled."""

    bulk_operation_id: int
    create_operation_id: int
    cancelled: int


_LOCK_JOB: Final = q(
    "SELECT j.merged_into_job_id, j.current_revision_id, is_fenced(j.id, :tuple) AS fenced, "
    "(SELECT a.llm_tuple_id FROM active_config a WHERE a.id = 1) IS NOT DISTINCT FROM :tuple AS active "
    "FROM jobs j WHERE j.id = :job FOR SHARE OF j"
)
_REVIVE_SET: Final = transition_set_clause(
    "score_targets",
    "pending",
    reason=CLEAR_REASON,
    set_sql={
        "charged_attempts": "0",
        "transient_failures": "0",
        "not_before": "NULL",
        "resume_draft_id": "NULL",
        "refusal_category": "NULL",
        "lane": "NULL",
        "override": "'none'",
        "override_at": "NULL",
        "bulk_operation_id": "EXCLUDED.bulk_operation_id",
        "updated_at": "EXCLUDED.updated_at",
    },
)
# `similarity` is no column of the edge (`transition_set_clause` sets only settable ones): its new value
# follows the edge's list. `xmax = 0` holds for the row the statement inserted, not for the one it updated.
_UPSERT: Final = q(
    "INSERT INTO score_targets AS t (job_revision_id, llm_tuple_id, job_id, state, similarity, "  # noqa: S608
    "bulk_operation_id, updated_at) VALUES (:revision, :tuple, :job, 'pending', :similarity, :bulk, :now) "
    f"ON CONFLICT (job_revision_id, llm_tuple_id) DO UPDATE SET {_REVIVE_SET}, "
    "similarity = EXCLUDED.similarity "
    f"WHERE t.state = 'cancelled' AND t.state_reason IN {_REVIVABLE_SQL} "
    "RETURNING t.id, t.xmax = 0 AS inserted"
)
_EXISTING: Final = q(
    "SELECT id, state FROM score_targets WHERE job_revision_id = :revision AND llm_tuple_id = :tuple"
)
_FILTER_RESULT: Final = q(
    "SELECT passed FROM hard_filter_results WHERE job_id = :job AND scope_version_id = :scope"
)
_SCOPE_PAYLOAD: Final = q("SELECT payload FROM scope_versions WHERE id = :scope")
_DEFER_ONE: Final = (
    "t.id = :target AND m.job_id = t.job_id AND EXISTS (SELECT 1 FROM score_targets s "  # noqa: S608
    f"WHERE s.id = t.id AND {DEFERRAL_ELIGIBLE_SQL})"
)


async def _filter_passed(conn: AsyncConnection, *, job_id: int, scope_version_id: int) -> bool:
    """Whether the job passes the scope's hard filters: its stored result, else computed and written now
    (`filters.refilter_job`, the job row held `FOR SHARE`; A1 issue 21)."""
    found = (
        await conn.execute(_FILTER_RESULT, {"job": job_id, "scope": scope_version_id})
    ).scalar_one_or_none()
    if found is not None:
        return bool(found)
    payload: JSONValue = (await conn.execute(_SCOPE_PAYLOAD, {"scope": scope_version_id})).scalar_one()
    scope = scope_from_payload(get_object(payload, ""))
    return (await filters.refilter_job(conn, job_id, {scope_version_id: scope}))[scope_version_id].passed


async def _corrected(conn: AsyncConnection, target_id: int, *, job_id: int, scope_version_id: int) -> str:
    """The initial state of a target just made `pending` (module docstring); the state it is left in."""
    if not await _filter_passed(conn, job_id=job_id, scope_version_id=scope_version_id):
        await transition(
            conn,
            "score_targets",
            target_id,
            ["pending"],
            "filtered_out",
            set_={"filter_scope_version_id": scope_version_id},
        )
        return "filtered_out"
    deferred = await transition_where(
        conn,
        "score_targets",
        ["pending"],
        "deferred_duplicate",
        where=_DEFER_ONE,
        params={"target": target_id},
        from_sql="duplicate_group_members m",
        set_sql={"deferred_group_id": "m.group_id"},
    )
    return "deferred_duplicate" if deferred else "pending"


async def create_or_revive(
    conn: AsyncConnection,
    *,
    job_id: int,
    job_revision_id: int,
    llm_tuple_id: int,
    profile_version_id: int,
    scope_version_id: int,
    bulk_operation_id: int | None,
    now: datetime,
) -> Creation:
    """The target of `(job_revision_id, llm_tuple_id)` created, revived or left as it is (module docstring),
    in the caller's transaction."""
    job = (await conn.execute(_LOCK_JOB, {"job": job_id, "tuple": llm_tuple_id})).first()  # step 4
    if job is None:
        return Skipped("not_current")  # deleted since it was read
    if not job.active:
        return Skipped("tuple_inactive")
    if job.fenced:
        return Skipped("fenced")
    if job.merged_into_job_id is not None:
        return Skipped("merged")
    if job.current_revision_id != job_revision_id:
        return Skipped("not_current")
    params = {
        "revision": job_revision_id,
        "tuple": llm_tuple_id,
        "job": job_id,
        "similarity": await similarity.similarity_for(
            conn, job_revision_id=job_revision_id, profile_version_id=profile_version_id
        ),
        "bulk": bulk_operation_id,
        "now": now,
    }
    upserted = (await conn.execute(_UPSERT, params)).first()
    if upserted is None:
        existing = (await conn.execute(_EXISTING, {"revision": job_revision_id, "tuple": llm_tuple_id})).one()
        return Unchanged(int(existing.id), str(existing.state))
    target_id = int(upserted.id)
    state = await _corrected(conn, target_id, job_id=job_id, scope_version_id=scope_version_id)
    return Created(target_id, state) if upserted.inserted else Revived(target_id, state)


# ---- cancellation (§7.16.3, Rulings P9, P52) ---------------------------------------------------------------

_MATCH: Final = {
    "llm_tuple_id": "s.llm_tuple_id = :tuple",
    "job_ids": "s.job_id = ANY(CAST(:jobs AS bigint[]))",
    "revision_ids": "s.job_revision_id = ANY(CAST(:revisions AS bigint[]))",
    "submission_ids": "s.batch_submission_id = ANY(CAST(:submissions AS bigint[]))",
}
_IN_STATES: Final = "s.state = ANY(CAST(:states AS text[]))"
_LOCKED_IDS: Final = (
    "t.id IN (SELECT s.id FROM score_targets s WHERE s.id = ANY(CAST(:ids AS bigint[])) "  # noqa: S608
    f"AND {_IN_STATES} ORDER BY s.id FOR UPDATE)"
)
_ATTEMPTS_OF: Final = (
    "(t.target_id, t.attempt_no) IN (SELECT * FROM unnest(CAST(:targets AS bigint[]), "
    "CAST(:numbers AS integer[])))"
)


async def _cancel(
    conn: AsyncConnection,
    match: Mapping[str, tuple[str, object]],
    states: Sequence[str],
    reason: CancelReason,
) -> Cancelled:
    """Cancel the targets of `match` (each bind's filter fragment and value) in `states` (module
    docstring).

    The waiting `FOR UPDATE` of the target rows after their bulk rows (P52) meets the owner of an
    `in_progress` follow-up target in the other order: its publish or fail takes the target row (step 5) and
    then its bulk row (step 7). Under `active_config FOR UPDATE` (an activation) no owner transaction runs,
    since each takes `active_config` first; under `FOR SHARE` (node 9) the pair can deadlock. PostgreSQL then
    aborts one of them: node 9 retries it (`DB_RETRY`), the owner's attempt retries its `db_transient`
    (`escaped_db_transient`), and the outcome is that of the two in either serial order."""
    predicate = " AND ".join(fragment for fragment, _ in match.values())
    params: dict[str, object] = {name: value for name, (_, value) in match.items()}
    candidates = await _ids(
        conn,
        q(f"SELECT s.id FROM score_targets s WHERE {predicate} AND {_IN_STATES} ORDER BY s.id"),  # noqa: S608
        {**params, "states": list(states)},
    )
    if not candidates:
        return Cancelled(0, 0)
    await bulk.lock_for_targets(conn, candidates)  # step 7, before the first target row (P52)
    moved = await transition_where(
        conn,
        "score_targets",
        states,
        "cancelled",
        reason=reason,
        where=_LOCKED_IDS,
        params={"ids": candidates, "states": list(states)},
        returning=("id", "attempt_no"),
    )
    if not moved:
        return Cancelled(0, 0)
    moved_ids = sorted(int(row["id"]) for row in moved)
    ended = await transition_where(
        conn,
        "score_attempts",
        ["running"],
        "cancelled",
        reason=reason,
        where=_ATTEMPTS_OF,
        params={
            "targets": [int(row["id"]) for row in moved],
            "numbers": [int(row["attempt_no"]) for row in moved],
        },
    )
    await _release_holds(conn, moved)  # the bulk rows are held (P52)
    log.info(
        "score_targets_cancelled",
        reason=reason,
        targets=len(moved),
        attempts=len(ended),
        target_ids=moved_ids[:LOGGED_IDS],
    )
    return Cancelled(len(moved), len(ended))


async def cancel_targets(
    conn: AsyncConnection,
    *,
    llm_tuple_id: int | None = None,
    job_ids: Sequence[int] | None = None,
    revision_ids: Sequence[int] | None = None,
    reason: CancelReason,
    now: datetime,
) -> Cancelled:
    """Cancel every non-published, non-cancelled target that all the given filters match (module
    docstring), in the caller's transaction; `ValueError` when none is given (it would match every target).
    `now` is the caller's clock (the transitions stamp the database's)."""
    match: dict[str, tuple[str, object]] = {}
    if llm_tuple_id is not None:
        match["tuple"] = (_MATCH["llm_tuple_id"], llm_tuple_id)
    if job_ids is not None:
        match["jobs"] = (_MATCH["job_ids"], list(job_ids))
    if revision_ids is not None:
        match["revisions"] = (_MATCH["revision_ids"], list(revision_ids))
    if not match:
        raise ValueError("cancel_targets: no filter given; it matches every target")
    # an in_batch target of a tuple supersession is the batch cancel's (`batches.request_cancel`)
    states = [*CANCELLABLE, "in_progress", *(() if reason == TUPLE_SUPERSEDED else ("in_batch",))]
    del now
    return await _cancel(conn, match, states, reason)


async def cancel_batched(
    conn: AsyncConnection, *, submission_ids: Sequence[int], reason: CancelReason
) -> Cancelled:
    """Cancel the `in_batch` targets of the submissions (`batches.request_cancel`), as `cancel_targets`
    cancels: their attempts ended, their holds released, the bulk rows first."""
    match = {"submissions": (_MATCH["submission_ids"], list(submission_ids))}
    return await _cancel(conn, match, ["in_batch"], reason)


# ---- the tuple activation (§12.6 (d)-(f) and (h), §13.3) --------------------------------------------------


async def _request_creation(
    conn: AsyncConnection, *, llm_tuple_id: int, bulk_operation_id: int, defer: Deferrer | None
) -> int:
    """(h): `create_targets_for_tuple` of the tuple into the new bulk operation (module docstring); its id."""
    kwargs: dict[str, Any] = {
        "kind": CREATE_TARGETS,
        "payload": {"llm_tuple_id": llm_tuple_id, "bulk_operation_id": bulk_operation_id},
        "dedup_key": f"{CREATE_TARGETS}:{llm_tuple_id}",
        "defer": defer,
    }
    operation_id, created = await create_operation(conn, **kwargs)
    if created:
        return operation_id
    # a live one of an earlier activation of this tuple names a bulk operation cancelled since
    await cancel_operation(conn, operation_id, reason=_SUPERSEDED_OPERATION)
    log.info("create_targets_superseded", operation_id=operation_id, llm_tuple_id=llm_tuple_id)
    operation_id, _ = await create_operation(conn, **kwargs)
    return operation_id


async def activate_tuple(
    conn: AsyncConnection,
    *,
    old_tuple_id: int | None,
    new_tuple_id: int,
    settings: Settings,
    now: datetime,
    defer: Deferrer | None,
) -> Activated:
    """§12.6 (f), (d), (e) and (h) for a changed tuple, in the caller's transaction (module docstring);
    `ValueError` for an unchanged one."""
    # Imported here: `batches` imports this module (`cancel_batched`).
    from jobhunter.scoring import batches

    if old_tuple_id == new_tuple_id:
        raise ValueError(f"tuple {new_tuple_id} is the active one already: nothing to activate")
    p = BudgetParams.from_payload(settings.to_payload())
    month = budget.month_of(now)
    await spend_ledger.ensure_month(conn, month, p, now)
    await spend_ledger.lock_month(conn, month, p, now=now)  # step 6: (f) under the month row
    superseded = await bulk.active_operation(conn)
    if superseded is not None:
        await bulk.cancel(conn, superseded, now=now)
    bulk_operation_id = await bulk.open(conn, llm_tuple_id=new_tuple_id, settings=settings, now=now)
    cancelled = 0
    if old_tuple_id is not None:
        cancelled += (
            await cancel_targets(conn, llm_tuple_id=old_tuple_id, reason=TUPLE_SUPERSEDED, now=now)
        ).targets
        cancelled += (await batches.request_cancel(conn, llm_tuple_id=old_tuple_id, now=now)).targets
    await afault_point(AFTER_CANCEL_POINT)
    operation_id = await _request_creation(
        conn, llm_tuple_id=new_tuple_id, bulk_operation_id=bulk_operation_id, defer=defer
    )
    log.info(
        "llm_tuple_activated",
        old_tuple_id=old_tuple_id,
        llm_tuple_id=new_tuple_id,
        bulk_operation_id=bulk_operation_id,
        superseded_bulk_operation_id=superseded,
        create_operation_id=operation_id,
        cancelled=cancelled,
    )
    return Activated(bulk_operation_id, operation_id, cancelled)


# ---- create_targets_for_tuple (§12.6 step 4) and missing_targets (§17.4) -----------------------------------

# The operation's order (module docstring): one predicate over `jobs j` per group, each walked by job id, so a
# page scans from its keyset on and stops at its limit (no sort of every retained job per chunk).
_SAVED_OR_APPLIED: Final = (
    "EXISTS (SELECT 1 FROM user_job_actions u WHERE u.job_id = j.id "
    "AND (u.saved_at IS NOT NULL OR u.applied_at IS NOT NULL))"
)
CREATION_ORDER: Final = (
    "j.availability = 'active'",
    f"j.availability <> 'active' AND {_SAVED_OR_APPLIED}",
    f"j.availability <> 'active' AND NOT {_SAVED_OR_APPLIED}",
)
# The jobs of group `group` that need a target of `:tuple`, after job `:after`.
_CREATION_PAGES: Final = tuple(
    q(
        "SELECT j.id, j.current_revision_id FROM jobs j "  # noqa: S608 (the constants above)
        f"WHERE {group} AND {RETAINED_JOB_SQL} AND {NEEDS_TARGET_SQL} AND j.id > :after "
        "ORDER BY j.id LIMIT :limit"
    )
    for group in CREATION_ORDER
)
_MISSING_PAGE: Final = q(
    "SELECT j.id, j.current_revision_id FROM jobs j "  # noqa: S608
    f"WHERE {RETAINED_JOB_SQL} AND {NEEDS_TARGET_SQL} AND j.id > :after ORDER BY j.id LIMIT :limit"
)
_PROGRESS: Final = q(
    f"SELECT count(*) AS total, count(*) FILTER (WHERE NOT {NEEDS_TARGET_SQL}) AS done "  # noqa: S608
    f"FROM jobs j WHERE {RETAINED_JOB_SQL}"
)
_LOCK_JOBS: Final = q("SELECT id FROM jobs WHERE id = ANY(CAST(:ids AS bigint[])) ORDER BY id FOR SHARE")
_PROFILE_OF: Final = q("SELECT profile_version_id FROM llm_tuples WHERE id = :tuple")
_BULK_PENDING: Final = q(
    "SELECT count(*) FROM score_targets WHERE bulk_operation_id = :bulk AND state = 'pending' "
    "AND override = 'none'"
)
_CREATION_LIVE: Final = q(
    "SELECT EXISTS (SELECT 1 FROM operations WHERE kind = 'create_targets_for_tuple' "
    "AND payload ->> 'llm_tuple_id' = CAST(:tuple AS text) AND status IN ('pending', 'running'))"
)


async def create_chunk(
    conn: AsyncConnection,
    jobs: Sequence[tuple[int, int]],
    *,
    llm_tuple_id: int,
    scope_version_id: int,
    bulk_operation_id: int | None,
    now: datetime,
) -> Counter[str]:
    """`create_or_revive` of each `(job_id, revision_id)` of `jobs` in job id order, whatever the caller's
    (the caller locked their job rows): two chunks that overlap then meet each other's unique keys in one
    order. The results counted: `created`, `revived`, `unchanged`, `skipped_<reason>`, and `state_<state>`
    for the state of each created or revived target (Ruling T14-a)."""
    profile = int((await conn.execute(_PROFILE_OF, {"tuple": llm_tuple_id})).scalar_one())
    counts: Counter[str] = Counter()
    for job_id, revision_id in sorted(jobs):
        made = await create_or_revive(
            conn,
            job_id=job_id,
            job_revision_id=revision_id,
            llm_tuple_id=llm_tuple_id,
            profile_version_id=profile,
            scope_version_id=scope_version_id,
            bulk_operation_id=bulk_operation_id,
            now=now,
        )
        match made:
            case Created(state=state):
                counts["created"] += 1
                counts[f"state_{state}"] += 1
            case Revived(state=state):
                counts["revived"] += 1
                counts[f"state_{state}"] += 1
            case Unchanged():
                counts["unchanged"] += 1
            case Skipped(reason=reason):
                counts[f"skipped_{reason}"] += 1
    return counts


async def lock_jobs(conn: AsyncConnection, job_ids: Sequence[int]) -> None:
    """The job rows `FOR SHARE` in id order, in one statement (step 4); a deleted one is simply absent."""
    if job_ids:
        await conn.execute(_LOCK_JOBS, {"ids": sorted(job_ids)})


def moved(counts: Counter[str]) -> bool:
    """Whether a chunk created or revived a target (its ranking bump)."""
    return counts["created"] + counts["revived"] > 0


async def _end_obsolete(conn: AsyncConnection, ctx: OperationContext, llm_tuple_id: int) -> None:
    await set_progress(conn, ctx.claim, {"skipped": TUPLE_SUPERSEDED})
    await ctx.finish_in(conn, status="succeeded")
    log.info("create_targets_obsolete", operation_id=ctx.claim.operation_id, llm_tuple_id=llm_tuple_id)


async def _request_batch(conn: AsyncConnection, *, llm_tuple_id: int, bulk_operation_id: int) -> int | None:
    """The `batch_submit` operation once every chunk is done (module docstring); its id, or `None`."""
    payload: JSONValue = (await conn.execute(_ACTIVE_SETTINGS)).scalar_one()
    if not get_bool(payload, "scoring.batch_bulk_rescore"):
        return None
    pending = int((await conn.execute(_BULK_PENDING, {"bulk": bulk_operation_id})).scalar_one())
    if pending < get_int(payload, "scoring.batch_min_targets"):
        return None
    operation_id, _ = await create_operation(
        conn,
        kind="batch_submit",
        payload={"bulk_operation_id": bulk_operation_id, "llm_tuple_id": llm_tuple_id},
        dedup_key=f"batch_submit:{bulk_operation_id}:1",
        defer=defer_in_tx,
    )
    return operation_id


@dataclass
class _Progress:
    """The operation's `{"done", "total"}`: counted once when an execution starts (the retained current
    revisions, and those of them with a live target of the tuple), then moved on by each chunk's creations."""

    done: int
    total: int

    def as_json(self) -> dict[str, JSONValue]:
        return {"done": self.done, "total": self.total}


@register_operation_handler(CREATE_TARGETS)
async def create_targets_for_tuple(ctx: OperationContext) -> None:
    """The `create_targets_for_tuple` operation (module docstring)."""
    tuple_id = get_int(dict(ctx.claim.payload), "llm_tuple_id")
    bulk_operation_id = get_int(dict(ctx.claim.payload), "bulk_operation_id")
    progress: _Progress | None = None
    totals: Counter[str] = Counter()
    for page_of_group in _CREATION_PAGES:
        after = 0
        while True:
            async with ctx.engine.begin() as conn:
                active = await cycles.lock_active_scoring(conn)  # step 2
                if active.llm_tuple_id != tuple_id:
                    await _end_obsolete(conn, ctx, tuple_id)
                    return
                if progress is None:
                    counted = (await conn.execute(_PROGRESS, {"tuple": tuple_id})).one()
                    progress = _Progress(done=int(counted.done), total=int(counted.total))
                params = {"tuple": tuple_id, "after": after, "limit": CHUNK}
                page = (await conn.execute(page_of_group, params)).all()
                if not page:
                    break
                await lock_jobs(conn, [int(row.id) for row in page])  # step 4
                await lock_operation_owner(conn, ctx.claim)  # step 5
                counts = await create_chunk(
                    conn,
                    [(int(row.id), int(row.current_revision_id)) for row in page],
                    llm_tuple_id=tuple_id,
                    scope_version_id=active.scope_version_id,
                    bulk_operation_id=bulk_operation_id,
                    now=_clock(ctx),
                )
                progress.done = min(progress.total, progress.done + counts["created"] + counts["revived"])
                await set_progress(conn, ctx.claim, progress.as_json())
                if moved(counts):
                    await ranking.bump(conn)  # step 11, last
            totals.update(counts)
            await afault_point(AFTER_CHUNK_POINT)
            if len(page) < CHUNK:
                break
            after = int(page[-1].id)
    async with ctx.engine.begin() as conn:
        active = await cycles.lock_active_scoring(conn)
        if active.llm_tuple_id != tuple_id:
            await _end_obsolete(conn, ctx, tuple_id)
            return
        await lock_operation_owner(conn, ctx.claim)
        if progress is not None and not totals:  # no chunk had a job to create a target for
            await set_progress(conn, ctx.claim, progress.as_json())
        batch = await _request_batch(conn, llm_tuple_id=tuple_id, bulk_operation_id=bulk_operation_id)
        await ctx.finish_in(conn, status="succeeded")
    log.info(
        "create_targets_done",
        operation_id=ctx.claim.operation_id,
        llm_tuple_id=tuple_id,
        bulk_operation_id=bulk_operation_id,
        batch_submit_operation_id=batch,
        **dict(sorted(totals.items())),
    )


def _clock(ctx: OperationContext) -> datetime:
    """The handler's clock: the worker's injected one, `utcnow` for an execution without deps."""
    return utcnow() if ctx.deps is None else ctx.deps.clock()


@reconciler("missing_targets", "heavy")
async def reconcile_missing_targets(ctx: ReconcileContext) -> int:
    """The missing targets of the active tuple, created or revived (module docstring); how many. The
    worker's own `FencedOut` propagates (Ruling FR-w), another owner's ends the pass."""
    after = 0
    totals: Counter[str] = Counter()
    try:
        while True:
            async with ctx.engine.begin() as conn:
                await fence_epoch(conn, ctx.epoch)
                active = await cycles.lock_active_scoring(conn)  # step 2
                tuple_id = active.llm_tuple_id
                if tuple_id is None or (await conn.execute(_CREATION_LIVE, {"tuple": tuple_id})).scalar_one():
                    break
                params = {"tuple": tuple_id, "after": after, "limit": CHUNK}
                page = (await conn.execute(_MISSING_PAGE, params)).all()
                await lock_jobs(conn, [int(row.id) for row in page])
                counts = await create_chunk(
                    conn,
                    [(int(row.id), int(row.current_revision_id)) for row in page],
                    llm_tuple_id=tuple_id,
                    scope_version_id=active.scope_version_id,
                    bulk_operation_id=None,
                    now=ctx.now,
                )
                if moved(counts):
                    await ranking.bump(conn)  # step 11, last
            totals.update(counts)
            if len(page) < CHUNK:
                break
            after = int(page[-1].id)
    except FencedOut as fenced:
        if fenced.worker_superseded:
            raise
        return 0
    made = totals["created"] + totals["revived"]
    if made or any(key.startswith("skipped_") for key in totals):
        log.info("missing_targets_created", **dict(sorted(totals.items())))
    return made
