"""System → Needs attention (spec §14.2 L5040, §15.7 L5310; plan M4 Task 12, D23, D26, D42, U1, U3,
Deviation 30): `GET /api/system/needs-attention`.

`needs_attention(conn, *, now)` reads on the caller's connection, which the route opens as one read snapshot
(`db.snapshot.read_snapshot`): plain reads, no lock. A list section is `{count, items}`: `count` is the full
count of its rows, `items` the first `ITEMS_PER_SECTION` (50) of them in the order named below (newest
first, unless the section says otherwise), each section one statement (`count(*) OVER ()` before the
`LIMIT`). The sections and what each reads (the index or bound each read uses; every growing table is read
through an index or within a bound):

- `raw_items`: the stored responses with `rejected` items, one entry per response: its source and instance
  label, its fetch time, how many of its items are rejected and their distinct reasons (sorted), and
  `replayable`, the one predicate `sources.replay` queues by (`replay.RESPONSE_REPLAYABLE_SQL`: its run has
  ended, its body is not purged and it was fetched at most `RAW_BODY_TTL` ago, inclusive).
  Newest fetch first, then the response id. The rejected items through `raw_items_attention` (a partial index
  of exactly the `pending`, `rejected` and `deferred` items), bounded by the 90-day retention of raw items;
  each response by its key.
- `identity_conflicts`: the open conflicts (`identity_conflicts_open`), `job_ids` the posting's job first,
  then the other jobs; `reason` the exclusive key namespaces in which they differ, from the conflict's
  evidence (`exclusive_conflict` when it names none). Newest first.
- `failed_targets`: the active tuple's targets in a state `targets.retry` takes back (`refused` and the
  `failed_*` ones, `targets.RETRYABLE_STATES`), with their job's title. Newest state change first. Read
  through `score_targets_unscored_rank` (the tuple's targets that are not published, deferred or
  cancelled), the states inlined as constants so that the partial index's predicate is provably implied.
- `quarantined_batches` (D26, closing Carry T17-d without a new alert): the `submit_ambiguous` submissions
  and the `released` ones whose `submit_started_at` is within `batches.RELEASED_LISTED_FOR` of `now` (the
  ambiguous reconciliation's own listing, `scoring.batch_adoption`, and a released lost batch, which the poll
  looks for by its provider id as long), with the candidates its last pass could not read
  (`reconcile_report.unread_candidates`, 0 without). Newest submit-start first. `batch_submissions` is
  scanned: one row per batch, and at most one in flight at a time.
- `retrieve_failing` (U3): the `submitted` submissions with an open `batch.retrieve_failing` (joined by the
  alert's dedup key, `alerts_open_dedup`); `releasable` by `batches.RELEASABLE_SQL` at `now`, the very
  predicate under which `batches.release` locks the submission (M4 Task 16: the alert open and `now >
  expires_at + RETRIEVE_FAILING_AFTER`, 1 h), so the Release button and the route never disagree; and
  `reserved_usd` the submission's reserved spend, which a release leaves counted as ambiguous (as
  `batches.release` reports it). Newest submit-start first.
- `mass_disappearances`: the open events, one per source instance with `mass_event_snapshot_id` set, shown by
  their opening snapshot (the id `POST /api/ats-snapshots/{id}/acknowledge` takes). Newest first.
  `source_instances` is scanned (one row per board).
- `unscorable_groups`: the active duplicate groups without a representative (`unscorable_reason` set), with
  their members (the unmerged jobs of the group, `jobs_group`, by job id), each with the id and state of its
  current revision's target of the active tuple (`null` without one). Newest election first.
  `duplicate_groups` has no index on these columns and is scanned: one row per group ever formed.
- `broken_sources` (D42): the source instances whose health is a partial cause of a run (`breaker_open`,
  `auth_failed`, `contract_broken`, `not_found`; severity `critical`) or `degraded` (severity `warn`),
  labelled by their watchlist company's display name, else their instance key. The critical ones first,
  then the newest opened breaker, then the id. `source_instances` is scanned.
- `paused_lane`: the open, unacknowledged `llm.reservation_bound_violated` or `llm.auth_failed` alert that
  pauses the score lane until it is acknowledged (`cycles.pause_reason`'s first two reasons, in its order;
  the newest of a code), for its Ack; `null` without one. The open alerts through `alerts_open_dedup`.
- `scope_apply_failed`: a pending scope whose newest apply ended `failed` (the reconciler has not cleared
  the pointer yet; `alert_id` is its open `scope.apply_failed` alert, `null` before the reconciler raises
  it), else, while nothing is pending, the newest open `scope.apply_failed` alert and its scope (from the
  alert's context); `null` otherwise, and while the pending scope's apply is live. The operations through
  `operations_dedup_newest`.

- `failed_purges` (M5 Task 18, D8, P24; U1 deferred it to M5): one item per `executing` deletion manifest
  whose newest purge operation (its dedup key `purge:<uuid>`, through `operations_dedup_newest`) is `failed`,
  or which has none, with that operation's id, error code and attempts (null, null and 0 without one), for
  its Retry (`POST /api/system/purges/{operation_id}/retry`); a manifest whose newest purge is pending,
  running, cancelled (the `purge_fences` reconciler re-queues it) or succeeded is not listed. Newest manifest
  first. `deletion_manifests` is read through `deletion_manifests_open` (the manifests not shipped).
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Final, Literal

from pydantic import BaseModel, Field
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import TextClause

from jobhunter.api.models import DecimalOut, UtcDatetime
from jobhunter.db.sql import q
from jobhunter.llm.gateway import AUTH_FAILED_ALERT
from jobhunter.llm.spend_ledger import BOUND_VIOLATED_ALERT
from jobhunter.privacy.manifests import ManifestKind
from jobhunter.scoring.batches import (
    RELEASABLE_SQL,
    RELEASED_LISTED_FOR,
    RETRIEVE_FAILING_ALERT,
    releasable_params,
)
from jobhunter.scoring.targets import RETRYABLE_STATES
from jobhunter.settings import scope_apply
from jobhunter.sources.replay import RESPONSE_REPLAYABLE_SQL, replayable_since
from jobhunter.worker.operations import PURGE_DEDUP_PREFIX, PURGE_KIND

ITEMS_PER_SECTION: Final = 50
NO_NAMESPACE_REASON: Final = "exclusive_conflict"

BrokenHealth = Literal["breaker_open", "auth_failed", "contract_broken", "not_found", "degraded"]
BrokenSeverity = Literal["critical", "warn"]
PausedReason = Literal["reservation_bound_violated", "auth_failed"]
UnscorableReason = Literal["all_failed", "all_filtered", "no_active_member"]

_PAUSING_ALERTS: Final[dict[str, PausedReason]] = {
    BOUND_VIOLATED_ALERT: "reservation_bound_violated",
    AUTH_FAILED_ALERT: "auth_failed",
}
# A constant of the code, never input: the states are inlined so the planner can prove the partial index's
# predicate (a bound array hides them from it).
_RETRYABLE_SQL: Final = ", ".join(f"'{state}'" for state in RETRYABLE_STATES)

_RAW_ITEMS: Final = q(
    "/* attention.raw_items */ SELECT g.raw_response_id, s.source_id, "  # noqa: S608
    "coalesce(w.display_name, s.instance_key) AS instance_label, r.fetched_at, g.rejected, g.reasons, "
    f"{RESPONSE_REPLAYABLE_SQL} AS replayable, count(*) OVER () AS total "
    "FROM (SELECT raw_response_id, count(*) AS rejected, "
    "array_agg(DISTINCT reason_code ORDER BY reason_code) AS reasons "
    "FROM raw_items WHERE disposition = 'rejected' GROUP BY raw_response_id) g "
    "JOIN raw_responses r ON r.id = g.raw_response_id "
    "JOIN source_instances s ON s.id = r.source_instance_id "
    "LEFT JOIN watchlist_companies w ON w.id = s.watchlist_company_id "
    "ORDER BY r.fetched_at DESC, g.raw_response_id DESC LIMIT :limit"
)
_IDENTITY_CONFLICTS: Final = q(
    "/* attention.identity_conflicts */ SELECT id, posting_job_id, candidate_job_ids, created_at, "
    "evidence -> 'conflicting_keys' AS conflicting, count(*) OVER () AS total "
    "FROM identity_conflicts WHERE status = 'open' ORDER BY created_at DESC, id DESC LIMIT :limit"
)
_FAILED_TARGETS: Final = q(
    "/* attention.failed_targets */ SELECT t.id, t.job_id, j.title, t.state, t.state_reason, "  # noqa: S608
    "t.refusal_category, count(*) OVER () AS total "
    "FROM active_config a JOIN score_targets t ON t.llm_tuple_id = a.llm_tuple_id "
    "JOIN jobs j ON j.id = t.job_id "
    f"WHERE a.id = 1 AND t.state IN ({_RETRYABLE_SQL}) "
    "ORDER BY t.state_changed_at DESC, t.id DESC LIMIT :limit"
)
_QUARANTINED: Final = q(
    "/* attention.quarantined_batches */ SELECT id, state, reserved_usd, request_count, submit_started_at, "
    "last_reconcile_at, coalesce(CAST(reconcile_report ->> 'unread_candidates' AS integer), 0) "
    "AS unread_candidates, count(*) OVER () AS total FROM batch_submissions "
    "WHERE state = 'submit_ambiguous' OR (state = 'released' AND submit_started_at > :listed_since) "
    "ORDER BY submit_started_at DESC, id DESC LIMIT :limit"
)
_RETRIEVE_FAILING: Final = q(
    "/* attention.retrieve_failing */ SELECT s.id, s.provider_batch_id, s.expires_at, a.id AS alert_id, "  # noqa: S608
    f"s.reserved_usd, {RELEASABLE_SQL} AS releasable, count(*) OVER () AS total FROM batch_submissions s "
    "JOIN alerts a ON a.dedup_key = :prefix || CAST(s.id AS text) AND a.resolved_at IS NULL "
    "WHERE s.state = 'submitted' ORDER BY s.submit_started_at DESC, s.id DESC LIMIT :limit"
)
_MASS_DISAPPEARANCES: Final = q(
    "/* attention.mass_disappearances */ SELECT n.id, coalesce(w.display_name, i.instance_key) "
    "AS instance_label, n.absent_count, n.baseline_active_count, n.taken_at, count(*) OVER () AS total "
    "FROM source_instances i JOIN ats_snapshots n ON n.id = i.mass_event_snapshot_id "
    "LEFT JOIN watchlist_companies w ON w.id = i.watchlist_company_id "
    "WHERE i.mass_event_snapshot_id IS NOT NULL ORDER BY n.taken_at DESC, n.id DESC LIMIT :limit"
)
_UNSCORABLE_GROUPS: Final = q(
    "/* attention.unscorable_groups */ SELECT id, unscorable_reason, count(*) OVER () AS total "
    "FROM duplicate_groups WHERE status = 'active' AND unscorable_reason IS NOT NULL "
    "ORDER BY elected_at DESC, id DESC LIMIT :limit"
)
_GROUP_MEMBERS: Final = q(
    "/* attention.group_members */ SELECT m.duplicate_group_id AS group_id, m.id AS job_id, m.title, "
    "t.id AS target_id, t.state AS target_state FROM jobs m "
    "LEFT JOIN score_targets t ON t.job_revision_id = m.current_revision_id "
    "AND t.llm_tuple_id = (SELECT a.llm_tuple_id FROM active_config a WHERE a.id = 1) "
    "WHERE m.duplicate_group_id = ANY(CAST(:groups AS bigint[])) AND m.merged_into_job_id IS NULL "
    "ORDER BY m.duplicate_group_id, m.id"
)
_BROKEN_SOURCES: Final = q(
    "/* attention.broken_sources */ SELECT i.id, i.source_id, coalesce(w.display_name, i.instance_key) "
    "AS label, i.health, i.last_error_code, i.breaker_opened_at, i.half_open_after, "
    "count(*) OVER () AS total "
    "FROM source_instances i LEFT JOIN watchlist_companies w ON w.id = i.watchlist_company_id "
    "WHERE i.health IN ('breaker_open', 'auth_failed', 'contract_broken', 'not_found', 'degraded') "
    "ORDER BY i.health = 'degraded', i.breaker_opened_at DESC NULLS LAST, i.id DESC LIMIT :limit"
)
_PAUSING: Final = q(
    "/* attention.paused_lane */ SELECT id, code FROM alerts WHERE code IN (:bound, :auth) "
    "AND resolved_at IS NULL AND acknowledged_at IS NULL ORDER BY code = :bound DESC, id DESC LIMIT 1"
)
_FAILED_PURGES: Final = q(
    "/* attention.failed_purges */ SELECT m.id, m.kind, m.created_at, n.id AS operation_id, "
    "n.last_error_code, coalesce(n.attempts, 0) AS attempts, count(*) OVER () AS total "
    "FROM deletion_manifests m "
    "LEFT JOIN LATERAL (SELECT o.id, o.status, o.last_error_code, o.attempts FROM operations o "
    "WHERE o.kind = :kind AND o.dedup_key = :prefix || CAST(m.manifest_uuid AS text) "
    "ORDER BY o.id DESC LIMIT 1) n ON true "
    "WHERE m.status = 'executing' AND (n.id IS NULL OR n.status = 'failed') "
    "ORDER BY m.created_at DESC, m.id DESC LIMIT :limit"
)
_PENDING_SCOPE: Final = q(
    "/* attention.pending_scope */ SELECT pending_scope_version_id FROM active_config WHERE id = 1"
)
_NEWEST_APPLY: Final = q(
    "/* attention.newest_apply */ SELECT status FROM operations WHERE kind = :kind AND dedup_key = :key "
    "ORDER BY id DESC LIMIT 1"
)
_OPEN_ALERT: Final = q(
    "/* attention.open_alert */ SELECT id FROM alerts WHERE dedup_key = :key AND resolved_at IS NULL"
)
_NEWEST_APPLY_FAILED: Final = q(
    "/* attention.apply_failed */ SELECT id, context ->> 'scope_version_id' AS scope_version_id FROM alerts "
    "WHERE code = :code AND resolved_at IS NULL ORDER BY id DESC LIMIT 1"
)


class RejectedResponseOut(BaseModel):
    """A stored response with rejected items: its source and instance, when it was fetched, how many of its
    items are rejected and why, and whether they can be replayed (the run that fetched it has ended, and its
    body is kept and at most 30 days old)."""

    raw_response_id: int
    source_id: str
    instance_label: str = Field(description="The watchlist company's name, else the instance key")
    fetched_at: UtcDatetime
    rejected: int
    reasons: list[str] = Field(description="The distinct reason codes of its rejected items, sorted")
    replayable: bool


class RawItemsSectionOut(BaseModel):
    """The responses with rejected items: how many, and the 50 fetched last."""

    count: int
    items: list[RejectedResponseOut]


class IdentityConflictOut(BaseModel):
    """An open identity conflict: the jobs whose strong keys disagree, the posting's job first."""

    conflict_id: int
    job_ids: list[int]
    reason: str = Field(
        description="The exclusive key namespaces in which the jobs carry different values, comma-separated "
        "(for example `pub:stepstone.de`); `exclusive_conflict` when none is named"
    )
    opened_at: UtcDatetime


class IdentityConflictsSectionOut(BaseModel):
    """The open identity conflicts: how many, and the 50 opened last."""

    count: int
    items: list[IdentityConflictOut]


class FailedTargetOut(BaseModel):
    """A target of the active scoring tuple that was refused or failed, which a retry can take back."""

    target_id: int
    job_id: int
    title: str
    state: str
    state_reason: str | None
    refusal_category: str | None


class FailedTargetsSectionOut(BaseModel):
    """The refused and failed targets: how many, and the 50 that changed state last."""

    count: int
    items: list[FailedTargetOut]


class QuarantinedBatchOut(BaseModel):
    """A batch submission whose send is ambiguous (`submit_ambiguous`) or that was released and is still
    reconciled (`released`): the spend it holds, its requests, when it was sent, the last reconciliation pass
    and the candidate batches that pass could not read."""

    submission_id: int
    state: Literal["submit_ambiguous", "released"]
    reserved_usd: DecimalOut
    request_count: int
    submit_started_at: UtcDatetime
    last_reconcile_at: UtcDatetime | None
    unread_candidates: int


class QuarantinedBatchesSectionOut(BaseModel):
    """The quarantined batch submissions: how many, and the 50 sent last."""

    count: int
    items: list[QuarantinedBatchOut]


class RetrieveFailingBatchOut(BaseModel):
    """A submitted batch that still cannot be retrieved past its expiry, with its open alert."""

    submission_id: int
    provider_batch_id: str
    expires_at: UtcDatetime | None
    alert_id: int
    reserved_usd: DecimalOut = Field(
        description="The spend the submission holds, which a release leaves counted as ambiguous"
    )
    releasable: bool = Field(description="Whether it can be released: more than 1 h past its expiry")


class RetrieveFailingSectionOut(BaseModel):
    """The submitted batches whose retrieval keeps failing: how many, and the 50 sent last."""

    count: int
    items: list[RetrieveFailingBatchOut]


class MassDisappearanceOut(BaseModel):
    """An unconfirmed mass disappearance of a board, by its opening snapshot: the postings absent from it
    against the board's active count before it."""

    snapshot_id: int
    instance_label: str = Field(description="The watchlist company's name, else the board's instance key")
    absent_count: int | None
    baseline_active_count: int | None
    taken_at: UtcDatetime


class MassDisappearancesSectionOut(BaseModel):
    """The open mass disappearances: how many, and the 50 opened last."""

    count: int
    items: list[MassDisappearanceOut]


class UnscorableMemberOut(BaseModel):
    """A member of an unscorable group, with its current revision's target of the active scoring tuple (null
    without one)."""

    job_id: int
    title: str
    target_id: int | None
    target_state: str | None


class UnscorableGroupOut(BaseModel):
    """An active duplicate group that no member can represent, why, and its members by job id."""

    group_id: int
    unscorable_reason: UnscorableReason
    members: list[UnscorableMemberOut]


class UnscorableGroupsSectionOut(BaseModel):
    """The unscorable duplicate groups: how many, and the 50 elected last."""

    count: int
    items: list[UnscorableGroupOut]


class BrokenSourceOut(BaseModel):
    """A source instance that does not collect (`critical`), or collects with failures (`degraded`, `warn`):
    its health, its last error and its breaker's times."""

    instance_id: int
    source_id: str
    label: str = Field(description="The watchlist company's name, else the instance key")
    health: BrokenHealth
    severity: BrokenSeverity
    last_error_code: str | None
    breaker_opened_at: UtcDatetime | None
    half_open_after: UtcDatetime | None


class BrokenSourcesSectionOut(BaseModel):
    """The broken and degraded source instances: how many, and 50 of them, the broken ones first."""

    count: int
    items: list[BrokenSourceOut]


class PausedLaneOut(BaseModel):
    """The alert that pauses the score lane until it is acknowledged, and why."""

    reason: PausedReason
    alert_id: int | None


class ScopeApplyFailedOut(BaseModel):
    """The scope whose apply failed, and its open alert (null until the alert is raised)."""

    version_id: int
    alert_id: int | None


class FailedPurgeOut(BaseModel):
    """A deletion whose purge failed or has no operation: its manifest, its kind and when it was requested,
    and its newest purge operation (null without one), which Retry takes, with its error code and attempts."""

    manifest_id: int
    kind: ManifestKind
    created_at: UtcDatetime
    operation_id: int | None
    last_error_code: str | None
    attempts: int


class FailedPurgesSectionOut(BaseModel):
    """The deletions whose purge failed: how many, and the 50 requested last."""

    count: int
    items: list[FailedPurgeOut]


class AttentionOut(BaseModel):
    """What needs the user: each list section's full count and at most 50 of its items, the score lane's
    acknowledgeable pause, a failed scope apply (each null when there is none) and the failed purges."""

    raw_items: RawItemsSectionOut
    identity_conflicts: IdentityConflictsSectionOut
    failed_targets: FailedTargetsSectionOut
    quarantined_batches: QuarantinedBatchesSectionOut
    retrieve_failing: RetrieveFailingSectionOut
    mass_disappearances: MassDisappearancesSectionOut
    unscorable_groups: UnscorableGroupsSectionOut
    broken_sources: BrokenSourcesSectionOut
    paused_lane: PausedLaneOut | None
    scope_apply_failed: ScopeApplyFailedOut | None
    failed_purges: FailedPurgesSectionOut


async def _rows(
    conn: AsyncConnection, statement: TextClause, **params: Any
) -> tuple[int, Sequence[Row[Any]]]:
    """A section's rows (at most `ITEMS_PER_SECTION`) and its full count, the rows' `total`."""
    rows = (await conn.execute(statement, {**params, "limit": ITEMS_PER_SECTION})).all()
    return (int(rows[0].total) if rows else 0), rows


def _namespaces(conflicting: object) -> str:
    """The exclusive key namespaces a conflict's evidence names, sorted and comma-separated."""
    found: set[str] = set()
    if isinstance(conflicting, list):
        found = {k["key_ns"] for k in conflicting if isinstance(k, dict) and isinstance(k.get("key_ns"), str)}
    return ", ".join(sorted(found)) or NO_NAMESPACE_REASON


async def _raw_items(conn: AsyncConnection, now: datetime) -> RawItemsSectionOut:
    count, rows = await _rows(conn, _RAW_ITEMS, since=replayable_since(now))
    return RawItemsSectionOut(
        count=count,
        items=[
            RejectedResponseOut(
                raw_response_id=r.raw_response_id,
                source_id=r.source_id,
                instance_label=r.instance_label,
                fetched_at=r.fetched_at,
                rejected=r.rejected,
                reasons=list(r.reasons),
                replayable=r.replayable,
            )
            for r in rows
        ],
    )


async def _identity_conflicts(conn: AsyncConnection) -> IdentityConflictsSectionOut:
    count, rows = await _rows(conn, _IDENTITY_CONFLICTS)
    return IdentityConflictsSectionOut(
        count=count,
        items=[
            IdentityConflictOut(
                conflict_id=r.id,
                job_ids=[r.posting_job_id, *r.candidate_job_ids],
                reason=_namespaces(r.conflicting),
                opened_at=r.created_at,
            )
            for r in rows
        ],
    )


async def _failed_targets(conn: AsyncConnection) -> FailedTargetsSectionOut:
    count, rows = await _rows(conn, _FAILED_TARGETS)
    return FailedTargetsSectionOut(
        count=count,
        items=[
            FailedTargetOut(
                target_id=r.id,
                job_id=r.job_id,
                title=r.title,
                state=r.state,
                state_reason=r.state_reason,
                refusal_category=r.refusal_category,
            )
            for r in rows
        ],
    )


async def _quarantined(conn: AsyncConnection, now: datetime) -> QuarantinedBatchesSectionOut:
    count, rows = await _rows(conn, _QUARANTINED, listed_since=now - RELEASED_LISTED_FOR)
    return QuarantinedBatchesSectionOut(
        count=count,
        items=[
            QuarantinedBatchOut(
                submission_id=r.id,
                state=r.state,
                reserved_usd=r.reserved_usd,
                request_count=r.request_count,
                submit_started_at=r.submit_started_at,
                last_reconcile_at=r.last_reconcile_at,
                unread_candidates=r.unread_candidates,
            )
            for r in rows
        ],
    )


async def _retrieve_failing(conn: AsyncConnection, now: datetime) -> RetrieveFailingSectionOut:
    count, rows = await _rows(
        conn, _RETRIEVE_FAILING, prefix=f"{RETRIEVE_FAILING_ALERT}:", **releasable_params(now)
    )
    return RetrieveFailingSectionOut(
        count=count,
        items=[
            RetrieveFailingBatchOut(
                submission_id=r.id,
                provider_batch_id=r.provider_batch_id,
                expires_at=r.expires_at,
                alert_id=r.alert_id,
                reserved_usd=r.reserved_usd,
                releasable=r.releasable,
            )
            for r in rows
        ],
    )


async def _mass_disappearances(conn: AsyncConnection) -> MassDisappearancesSectionOut:
    count, rows = await _rows(conn, _MASS_DISAPPEARANCES)
    return MassDisappearancesSectionOut(
        count=count,
        items=[
            MassDisappearanceOut(
                snapshot_id=r.id,
                instance_label=r.instance_label,
                absent_count=r.absent_count,
                baseline_active_count=r.baseline_active_count,
                taken_at=r.taken_at,
            )
            for r in rows
        ],
    )


async def _unscorable_groups(conn: AsyncConnection) -> UnscorableGroupsSectionOut:
    count, rows = await _rows(conn, _UNSCORABLE_GROUPS)
    members: dict[int, list[UnscorableMemberOut]] = {int(r.id): [] for r in rows}
    if members:
        for m in await conn.execute(_GROUP_MEMBERS, {"groups": list(members)}):
            members[int(m.group_id)].append(
                UnscorableMemberOut(
                    job_id=m.job_id, title=m.title, target_id=m.target_id, target_state=m.target_state
                )
            )
    return UnscorableGroupsSectionOut(
        count=count,
        items=[
            UnscorableGroupOut(
                group_id=r.id, unscorable_reason=r.unscorable_reason, members=members[int(r.id)]
            )
            for r in rows
        ],
    )


async def _broken_sources(conn: AsyncConnection) -> BrokenSourcesSectionOut:
    count, rows = await _rows(conn, _BROKEN_SOURCES)
    return BrokenSourcesSectionOut(
        count=count,
        items=[
            BrokenSourceOut(
                instance_id=r.id,
                source_id=r.source_id,
                label=r.label,
                health=r.health,
                severity="warn" if r.health == "degraded" else "critical",
                last_error_code=r.last_error_code,
                breaker_opened_at=r.breaker_opened_at,
                half_open_after=r.half_open_after,
            )
            for r in rows
        ],
    )


async def _paused_lane(conn: AsyncConnection) -> PausedLaneOut | None:
    params = {"bound": BOUND_VIOLATED_ALERT, "auth": AUTH_FAILED_ALERT}
    row = (await conn.execute(_PAUSING, params)).first()
    return None if row is None else PausedLaneOut(reason=_PAUSING_ALERTS[row.code], alert_id=row.id)


def _scope_alert_key(scope_version_id: int) -> str:
    return f"{scope_apply.APPLY_FAILED_ALERT}:scope:{scope_version_id}"


async def _scope_apply_failed(conn: AsyncConnection) -> ScopeApplyFailedOut | None:
    pending = (await conn.execute(_PENDING_SCOPE)).scalar_one_or_none()
    if pending is not None:
        key = scope_apply.dedup_key(int(pending))
        newest = (await conn.execute(_NEWEST_APPLY, {"kind": scope_apply.KIND, "key": key})).scalar()
        if newest == "failed":
            alert = (await conn.execute(_OPEN_ALERT, {"key": _scope_alert_key(int(pending))})).scalar()
            return ScopeApplyFailedOut(
                version_id=int(pending), alert_id=None if alert is None else int(alert)
            )
        return None  # its apply is under way, or ended another way (the reconciler requests it again)
    row = (await conn.execute(_NEWEST_APPLY_FAILED, {"code": scope_apply.APPLY_FAILED_ALERT})).first()
    if row is None or row.scope_version_id is None or not str(row.scope_version_id).isdecimal():
        return None
    return ScopeApplyFailedOut(version_id=int(row.scope_version_id), alert_id=int(row.id))


async def _failed_purges(conn: AsyncConnection) -> FailedPurgesSectionOut:
    count, rows = await _rows(conn, _FAILED_PURGES, kind=PURGE_KIND, prefix=PURGE_DEDUP_PREFIX)
    return FailedPurgesSectionOut(
        count=count,
        items=[
            FailedPurgeOut(
                manifest_id=r.id,
                kind=r.kind,
                created_at=r.created_at,
                operation_id=r.operation_id,
                last_error_code=r.last_error_code,
                attempts=r.attempts,
            )
            for r in rows
        ],
    )


async def needs_attention(conn: AsyncConnection, *, now: datetime) -> AttentionOut:
    """Every section of Needs attention at `now`, read on the caller's connection (module docstring)."""
    return AttentionOut(
        raw_items=await _raw_items(conn, now),
        identity_conflicts=await _identity_conflicts(conn),
        failed_targets=await _failed_targets(conn),
        quarantined_batches=await _quarantined(conn, now),
        retrieve_failing=await _retrieve_failing(conn, now),
        mass_disappearances=await _mass_disappearances(conn),
        unscorable_groups=await _unscorable_groups(conn),
        broken_sources=await _broken_sources(conn),
        paused_lane=await _paused_lane(conn),
        scope_apply_failed=await _scope_apply_failed(conn),
        failed_purges=await _failed_purges(conn),
    )
