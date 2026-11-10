"""Delete everything (spec §20.4, §19.5, §20.1; plan M5 Task 16, M5-U3, P4, P19, D14, Deviations 2, 13, 15,
16, 17, 18, 22, 24, 50, 51, 53, 54 and 56): manifest kind `delete_everything`, a `global` fence.

**A0** (`request_delete_everything`, the caller's transaction; Task 18's route re-authenticates, holds its
idempotency row in the same transaction and passes that row's `id`): `active_config FOR UPDATE`; while a
delete-everything is `executing`, 409 `conflict_state` with `detail = "purge_in_progress"`; then
`purge.open_purge` with the request `{"idempotency_row_id": <id>}` (ids only, immutable) and one `global`
fence, the persisted admission stop (Task 8): from its commit no run, cycle or operation but a purge starts.

**What it keeps** (`DELETE_EVERYTHING_KEEPS`, every table that still holds rows when it completes, each with
its kept rows): the user account with its TOTP enrolment (M5-U3); the quota and spend ledgers, `llm_budget`
and `quota_windows`; the policy and config tables of §20.1 without personal content (model policies,
execution configs, evaluation policies, rubric and prompt-schema versions, the sources and the default source
instances); the singletons; `backup_sets` and `backup_runs`; the manifests, shipments, shipment items and
fences; the alerts open at A2; the new default settings version and its scope version (the scope versions
step 10 does not list); this purge's operation and this request's idempotency row; the procrastinate jobs
`doing` at A2 with their events; the exports, every one of them content-free (`purged`, `expired` or
`failed`: §20.3's deletion of an export is its `purged` state, the export ops'); and the schema records
(`alembic_version`, `schema_compat`, the transition matrix, the checkpointer's `checkpoint_migrations`) and
procrastinate's own records of its workers and periodic defers. Every other table of `public` is empty.

**The content** (A2, `compute`, under the fence after the quiesce), every selector `all` bounded by
`max_ids` (an identity column, `login_lockouts.generation`, a parent's id, `created_at <= fenced_at` or a
checkpoint thread's owner), the memberships that can change frozen as ids (Deviations 16, 24):
- the alerts resolved at A2 (`row_delete alerts ids=[...]`: one open at A2 is kept, also once resolved);
- the procrastinate jobs not `doing` at A2 (`row_delete procrastinate_jobs ids=[...]`, their events with
  them): a job `doing` at A2 is kept, also once it has finished;
- step 10's scope versions: those within the bound but the one `settings.reset.reset_to_defaults` reuses by
  hash (the default document's, `derive.derived(..., profile=None)`, computed here). No row that survives
  references any other: the rows that name a scope version are deleted, and `active_config` is re-pointed;
- `retained`, the rows within the bound a kept pointer may name (step 8's postcondition): every scope version
  step 10 does not list, every row of the policy tables kept whole (model policies, execution configs,
  evaluation policies, rubric and prompt-schema versions, which the reset and later saves reuse by hash), and
  no settings version, profile version or tuple;
- the fixed `keep` ids: this manifest's purge operations, the request's idempotency row (by its identity `id`,
  Task 1), the default source instances;
- the CV files' storage keys (`file_remove cv_store`).
The operations, in §20.4's order: (1) the score targets and every row they own; (2) `eval_case_results`,
`eval_runs`, `golden_cases`, `score_feedback`; (3) `llm_calls`; (4) `batch_items`, `batch_submissions` (their
`purged_batch_items`), `admission_lane_credits`, `bulk_operations`; (5) the jobs' graph and every tombstone,
`duplicate_groups` included; (6) the fetch requests (raw responses and items), ATS snapshots, coverage
intervals and cells, queries, the source instances other than the defaults and the watchlist; (7) the score
cycles and the runs, those within the bound and those of a settings version within it (the tick may queue a
run under the fence: it never starts, and it names a version step 10 deletes); (9) the profile vectors,
query proposals, tuples, drafts and versions, CV files and embeddings; (10) the settings versions and the
listed scope versions; (11) the alerts, the checkpoint threads, the idempotency records, every operation but
this purge's, the login attempts and lockouts and the procrastinate jobs; (12) the sessions created at or
before `fenced_at` (deleted by the completing transaction). The export ops come last
(`purge.compute_content`). Step 8 has no typed operation: its postcondition is the kind's.

**Phase B** (`execute`), each step in `PurgeRun.chunk` transactions, every statement within the bound:
- step 2, one chunk with `active_config FOR UPDATE`: the `queued` and `running` runs
  `cancelled('delete_everything')` (`worker.runs.cancel_active_runs`); the `running` score cycles
  `failed('cancelled')`; every `pending` or `running` operation within the bound but this manifest's
  `cancelled` (`claim_id := NULL`; a running coroutine fails its next claim check), and the `running` proposal
  of each cancelled `propose_queries` operation `failed` in the same transaction; then
  `deletion_graph.settle_live_spend` (the live submissions mapped, the active bulk operation cancelled, every
  other unsettled entry settled). Then, with no transaction open, the fault point
  `privacy.delete_everything.after_cancel_recorded` (`AFTER_CANCEL_RECORDED`), and every submission within
  the bound that carries `cancel_requested_at` and a provider id (and so a crash between the recording and the
  send loses no cancel) has its cancel sent by `batch_results.send_purge_cancel` with the run's Batches client
  and `CANCEL_SLEEP`; a repeat finds the batch `canceling` and sends nothing. A replay (`run.effects is None`)
  sends none: each is logged `batch_cancel_skipped_replay` (ids only) and goes with its row (Deviation 56);
- step 3.1, `TARGET_SET_CHUNK` targets a chunk over their jobs (`purge_target_set`); 3.2, the eval runs'
  calls (`settle_and_delete_calls`), case results and runs, the golden cases over their jobs
  (`settle_and_delete_cases(check=None)`) and the feedback; 3.3, every other call (`settle_and_delete_calls`);
  3.4, the batch rows and the bulk operations; 3.5, per root job and its merged graph,
  `deletion_graph.purge_job_graph(write_tombstone=False)` (Deviation 50), then the duplicate groups and the
  tombstones; 3.6, the fetch requests newest first (a response's `body_ref_id` names an older response's
  body, so none of them loses its only body to a deletion and fails its CHECK), then the collection's other
  rows;
- before 3.7, in a live purge with a pinger, one `run.effects.deadman.ping_pending()` with no transaction
  open: a run that ended before the purge and was not pinged yet would otherwise lose its ping with its row;
- 3.8, one chunk with `active_config FOR UPDATE`: the runs of a settings version within the bound (queued by
  the tick since 3.7), then, unless every pointer already holds step 8's postcondition (a re-execution, or a
  replay of a dump taken after the reset), `settings.reset.reset_to_defaults`, the ranking bumped last;
- 3.9 to 3.11, the rest, the CV files unlinked after the commit of their rows; 3.12 is the completing
  transaction's (`complete`).
Every chunk is idempotent: a re-execution after any commit, or a phase re-run after a lost deadlock (Ruling
T12-1), finds what is left.

**The completing transaction** (`complete`, `purge.CompletingKind`, then Task 12's phase C): every session
created at or before `fenced_at` deleted (the requester's too: the user is logged out), then the global fence
released, the postconditions checked and the manifest `completed`. There is no rebuild.

**The kind's postcondition** (step 8, relative to the bounds, so later activity keeps it true): every pointer
of `active_config`, and `schedule_state.settings_version_id`, is NULL, above its table's bound, or in the
manifest's `retained` ids of its table. A dump from before the purge fails it (its pointers name deleted
rows), which is what a replay must fix. Nothing is re-applied in a replay (`reapply_admission`).

Ids, counts and codes only reach a log line.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime
from typing import Any, Final, Literal

from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import TextClause

from jobhunter.db import ranking
from jobhunter.db.seed import DEFAULT_INSTANCES, content_hash, load_settings_v1
from jobhunter.db.sql import q
from jobhunter.db.transitions import transition_where
from jobhunter.errors import InvariantViolation
from jobhunter.faults import afault_point
from jobhunter.observability.logging import get_logger
from jobhunter.privacy import deletion_graph
from jobhunter.privacy.fences import FenceSpec, purge_in_progress
from jobhunter.privacy.manifests import (
    SELECTOR_ALL,
    THREAD_FORMS,
    Bound,
    ManifestContent,
    ManifestKind,
    ManifestRow,
    Operation,
    RowDeleteOp,
    Violation,
    max_identity_ids,
)
from jobhunter.privacy.purge import OpenedPurge, PurgeRun, new_content, open_purge, register_kind
from jobhunter.queries import proposals
from jobhunter.scoring import batch_results, batches
from jobhunter.scoring.targets import TARGET_SET_CHUNK
from jobhunter.settings.derive import derived
from jobhunter.settings.model import Settings
from jobhunter.settings.reset import reset_to_defaults
from jobhunter.types import get_int
from jobhunter.worker.operations import PURGE_KIND, cancel_operation
from jobhunter.worker.runs import DELETE_EVERYTHING_CANCEL, cancel_active_runs, observe_run_ends
from jobhunter.worker.tasks import Deferrer

KIND: Final = "delete_everything"
REQUEST_KEY: Final = "idempotency_row_id"
# The fault point between step 2's commit and the cancels' send (module docstring).
AFTER_CANCEL_RECORDED: Final = "privacy.delete_everything.after_cancel_recorded"
CANCELLED_CYCLE: Final = "cancelled"  # a running score cycle's reason (§7.16.8)
CANCELLED_OPERATION: Final = "delete_everything"  # a cancelled operation's reason
ROW_CHUNK: Final = 500  # rows per chunk of the steps that delete by id
LOGGED_IDS: Final = 20
# The waits between a purge cancel's tries (`batch_results.send_purge_cancel`); a test replaces it.
CANCEL_SLEEP: Callable[[float], Awaitable[None]] = asyncio.sleep

DELETE_EVERYTHING_KEEPS: Final[frozenset[str]] = frozenset(
    {
        # the user account with its TOTP enrolment (M5-U3)
        "users",
        # the ledgers (§20.1: so a restart can never exceed a window or the cap)
        "llm_spend_ledger",
        "quota_ledger",
        "llm_budget",
        "quota_windows",
        # the policy and config tables of §20.1 without personal content
        "model_policies",
        "execution_configs",
        "evaluation_policies",
        "rubric_versions",
        "prompt_schema_versions",
        "sources",
        "source_instances",  # the default instances
        # the singletons
        "active_config",
        "schedule_state",
        "ranking_state",
        "worker_heartbeat",
        "deadman_state",
        "schema_compat",
        "transition_matrix",
        # the backups, the manifests, their shipments and the fences (ids and hashes only)
        "backup_sets",
        "backup_runs",
        "deletion_manifests",
        "manifest_shipments",
        "manifest_shipment_items",
        "purge_fences",
        "alerts",  # the alerts open at A2
        "settings_versions",  # the new default version
        "scope_versions",  # its scope version, and the others step 10 does not list
        "operations",  # this purge's
        "api_idempotency",  # this request's
        "procrastinate_jobs",  # the jobs `doing` at A2
        "procrastinate_events",  # their events
        "exports",  # every one content-free (`purged`, `expired`, `failed`)
        "alembic_version",
        # schema and queue records without content that no step of §20.4 names
        "checkpoint_migrations",
        "procrastinate_periodic_defers",
        "procrastinate_workers",
    }
)

# The tables kept whole whose rows a kept pointer may name (`retained`).
_KEPT_WHOLE: Final = (
    "model_policies",
    "execution_configs",
    "evaluation_policies",
    "rubric_versions",
    "prompt_schema_versions",
)
# Each pointer of step 8's postcondition: its table, its column and the table it names.
_POINTERS: Final = (
    ("active_config", "settings_version_id", "settings_versions"),
    ("active_config", "scope_version_id", "scope_versions"),
    ("active_config", "pending_scope_version_id", "scope_versions"),
    ("active_config", "model_policy_id", "model_policies"),
    ("active_config", "execution_config_id", "execution_configs"),
    ("active_config", "evaluation_policy_id", "evaluation_policies"),
    ("active_config", "rubric_version_id", "rubric_versions"),
    ("active_config", "prompt_schema_version_id", "prompt_schema_versions"),
    ("active_config", "profile_version_id", "profile_versions"),
    ("active_config", "llm_tuple_id", "llm_tuples"),
    ("schedule_state", "settings_version_id", "settings_versions"),
)

_IDS: Final = "CAST(:ids AS bigint[])"

# ---- A0 ----------------------------------------------------------------------------------------------------

_LOCK_CONFIG: Final = q("SELECT 1 FROM active_config WHERE id = 1 FOR UPDATE")
_EXECUTING: Final = q(
    "SELECT EXISTS (SELECT 1 FROM deletion_manifests WHERE kind = 'delete_everything' "
    "AND status = 'executing')"
)

# ---- A2 ----------------------------------------------------------------------------------------------------

_OWN_OPERATIONS: Final = q(
    "SELECT id FROM operations WHERE kind = :kind "
    "AND payload -> 'manifest_id' = to_jsonb(CAST(:m AS bigint)) AND id <= :bound ORDER BY id"
)
_DEFAULT_INSTANCES: Final = q(
    "SELECT id FROM source_instances WHERE instance_key = 'default' "
    "AND source_id = ANY(CAST(:sources AS text[])) AND id <= :bound ORDER BY id"
)
_RESOLVED_ALERTS: Final = q(
    "SELECT id FROM alerts WHERE resolved_at IS NOT NULL AND id <= :bound ORDER BY id"
)
_FINISHED_JOBS: Final = q(
    "SELECT id FROM procrastinate_jobs WHERE status <> 'doing' AND id <= :bound ORDER BY id"
)
_LISTED_SCOPES: Final = q(
    "SELECT id FROM scope_versions WHERE id <= :bound AND scope_hash <> :default_hash ORDER BY id"
)
_ROWS_WITHIN: Final = "SELECT id FROM {table} WHERE id <= :bound ORDER BY id"  # a table of this module
_CV_KEYS: Final = q(
    "SELECT storage_path FROM cv_files WHERE id <= :bound AND storage_path IS NOT NULL ORDER BY id"
)

# ---- phase B: step 2 ---------------------------------------------------------------------------------------

# Every live operation within the bound but this manifest's purges, owner rows in id order.
_LIVE_OPERATIONS: Final = q(
    "SELECT id, kind, payload FROM operations WHERE status IN ('pending', 'running') AND id <= :bound "
    "AND NOT (kind = :kind AND payload -> 'manifest_id' = to_jsonb(CAST(:m AS bigint))) "
    "ORDER BY id FOR UPDATE"
)
_RECORDED_CANCELS: Final = q(
    "SELECT id, provider_batch_id FROM batch_submissions WHERE cancel_requested_at IS NOT NULL "
    "AND provider_batch_id IS NOT NULL AND state = ANY(CAST(:states AS text[])) AND id <= :bound ORDER BY id"
)

# ---- phase B: the deletes ----------------------------------------------------------------------------------

_TARGET_PAGE: Final = q("SELECT id, job_id FROM score_targets WHERE id <= :bound ORDER BY id LIMIT :limit")
_TARGETS_AGAIN: Final = q(f"SELECT id, job_id FROM score_targets WHERE id = ANY({_IDS}) ORDER BY id")  # noqa: S608
_EVAL_CALL_PAGE: Final = q(
    "SELECT id FROM llm_calls WHERE eval_run_id IS NOT NULL AND id <= :bound ORDER BY id LIMIT :limit"
)
_CALL_PAGE: Final = q("SELECT id FROM llm_calls WHERE id <= :bound ORDER BY id LIMIT :limit")
_CASE_PAGE: Final = q(
    "SELECT g.id, r.job_id FROM golden_cases g JOIN job_revisions r ON r.id = g.job_revision_id "
    "WHERE g.id <= :bound ORDER BY g.id LIMIT :limit"
)
# The root jobs within the bound after `:after`, a page in id order, and a root's graph.
_ROOT_PAGE: Final = q(
    "SELECT id FROM jobs WHERE merged_into_job_id IS NULL AND id <= :bound AND id > :after "
    "ORDER BY id LIMIT :limit"
)
_GRAPH: Final = q(
    "WITH RECURSIVE g(id) AS (SELECT id FROM jobs WHERE id = :root "
    "UNION SELECT j.id FROM jobs j JOIN g ON j.merged_into_job_id = g.id) SELECT id FROM g ORDER BY id"
)
_JOBS_LEFT: Final = q("SELECT count(*) FROM jobs WHERE id <= :bound")
# The runs of a settings version within the bound, whatever their id (module docstring, step 3.8).
_SETTINGS_RUNS: Final = q("DELETE FROM runs WHERE settings_version_id <= :settings")
_POINTER_VALUES: Final = q(
    "SELECT a.settings_version_id, a.scope_version_id, a.pending_scope_version_id, a.model_policy_id, "
    "a.execution_config_id, a.evaluation_policy_id, a.rubric_version_id, a.prompt_schema_version_id, "
    "a.profile_version_id, a.llm_tuple_id, s.settings_version_id AS schedule_settings_version_id "
    "FROM active_config a CROSS JOIN schedule_state s WHERE a.id = 1 AND s.id = 1"
)
_DELETE_VECTORS: Final = q("DELETE FROM profile_vectors WHERE profile_version_id <= :bound")
_DELETE_DRAFTS: Final = q("DELETE FROM profile_drafts WHERE id <= :bound")
_DELETE_VERSIONS: Final = q("DELETE FROM profile_versions WHERE id <= :bound")
_DELETE_CASE_RESULTS: Final = q("DELETE FROM eval_case_results WHERE eval_run_id <= :bound")
_DELETE_LANE_CREDITS: Final = q("DELETE FROM admission_lane_credits WHERE llm_tuple_id <= :bound")
# The runs within the bound and those of a settings version within it, a page (step 3.7).
_RUNS_PAGE: Final = (
    "DELETE FROM runs WHERE id IN (SELECT id FROM runs "
    "WHERE id <= :bound OR settings_version_id <= :settings ORDER BY id LIMIT :limit)"
)
_LOCKOUTS_PAGE: Final = (
    "DELETE FROM login_lockouts WHERE generation IN (SELECT generation FROM login_lockouts "
    "WHERE generation <= :bound ORDER BY generation LIMIT :limit)"
)
_DELETE_SESSIONS: Final = q("DELETE FROM sessions WHERE created_at <= :fenced_at")

log = get_logger("jobhunter.privacy.kinds.delete_everything")


# ---- A0 ----------------------------------------------------------------------------------------------------


async def request_delete_everything(
    conn: AsyncConnection, *, now: datetime, idempotency_row_id: int, defer: Deferrer | None
) -> OpenedPurge:
    """A0 in the caller's transaction (module docstring): the manifest with the request
    `{"idempotency_row_id": <id>}`, its `global` fence and its `purge` operation; the opened purge.
    `Refusal(409)` while a delete-everything is executing; `ValueError` for an id that names no row."""
    if isinstance(idempotency_row_id, bool) or idempotency_row_id <= 0:
        raise ValueError("the request names its idempotency row by its positive id")
    await conn.execute(_LOCK_CONFIG)  # A0's lock
    if (await conn.execute(_EXECUTING)).scalar_one():
        log.info("delete_everything_refused", reason="purge_in_progress")
        raise purge_in_progress()
    opened = await open_purge(
        conn,
        kind=KIND,
        request={REQUEST_KEY: idempotency_row_id},
        fences=[FenceSpec(kind="global")],
        now=now,
        defer=defer,
    )
    log.info("delete_everything_requested", manifest_id=opened.manifest_id, operation_id=opened.operation_id)
    return opened


# ---- the content -------------------------------------------------------------------------------------------


async def _ids(conn: AsyncConnection, statement: TextClause, params: Mapping[str, object]) -> list[int]:
    return [int(i) for i in (await conn.execute(statement, dict(params))).scalars()]


_BY_ID: Final[Bound] = {"by": "id"}


def _all(table: str, bound: Bound = _BY_ID, *, keep: Sequence[int] | None = None) -> RowDeleteOp:
    """A `row_delete` of `table` with the `all` selector within `bound`, apart from the `keep` ids."""
    op: RowDeleteOp = {"op": "row_delete", "table": table, "selector": SELECTOR_ALL, "bound": bound.copy()}
    if keep is not None:
        op["keep"] = list(keep)
    return op


def _parent(table: str, parent: str, column: str) -> RowDeleteOp:
    """A `row_delete` of `table` with the `all` selector within its parent's bound (`column` holds its id)."""
    return _all(table, {"by": "parent", "table": parent, "column": column})


def _listed(table: str, ids: Sequence[int], bound: Bound = _BY_ID) -> list[Operation]:
    """A `row_delete` of `table` naming `ids` within `bound`; none without an id."""
    if not ids:
        return []
    return [{"op": "row_delete", "table": table, "ids": list(ids), "bound": bound.copy()}]


def default_scope_hash() -> bytes:
    """The content hash of the scope version `reset_to_defaults` installs: the default document's, without a
    profile (`derive.derived`)."""
    return content_hash(derived(Settings.model_validate(load_settings_v1()), profile=None).scope)


async def _operations(
    conn: AsyncConnection, manifest: ManifestRow, max_ids: Mapping[str, int]
) -> tuple[list[Operation], dict[str, list[int]]]:
    """The content's operations, in §20.4's order (module docstring), and its `retained` ids."""
    idempotency_row_id = get_int(dict(manifest.request), REQUEST_KEY)
    own = await _ids(
        conn, _OWN_OPERATIONS, {"kind": PURGE_KIND, "m": manifest.id, "bound": max_ids["operations"]}
    )
    defaults = await _ids(
        conn, _DEFAULT_INSTANCES, {"sources": list(DEFAULT_INSTANCES), "bound": max_ids["source_instances"]}
    )
    resolved = await _ids(conn, _RESOLVED_ALERTS, {"bound": max_ids["alerts"]})
    finished = await _ids(conn, _FINISHED_JOBS, {"bound": max_ids["procrastinate_jobs"]})
    listed_scopes = await _ids(
        conn, _LISTED_SCOPES, {"bound": max_ids["scope_versions"], "default_hash": default_scope_hash()}
    )
    keys = [str(k) for k in (await conn.execute(_CV_KEYS, {"bound": max_ids["cv_files"]})).scalars()]
    retained: dict[str, list[int]] = {}
    for table in (*_KEPT_WHOLE, "scope_versions"):
        statement = q(_ROWS_WITHIN.format(table=table))  # a constant table name of this module
        retained[table] = await _ids(conn, statement, {"bound": max_ids[table]})
    retained["scope_versions"] = sorted(set(retained["scope_versions"]) - set(listed_scopes))
    retained.update({"settings_versions": [], "profile_versions": [], "llm_tuples": []})
    files: list[Operation] = [{"op": "file_remove", "volume": "cv_store", "keys": keys}] if keys else []
    operations: list[Operation] = [
        # (1) the score targets and the rows they own
        _all("score_targets"),
        _all("score_attempts"),
        _all("score_drafts"),
        _all("critic_notes"),
        _all("llm_results"),
        _all("result_evidence"),
        _all("det_results"),
        # (2)
        _parent("eval_case_results", "eval_runs", "eval_run_id"),
        _all("eval_runs"),
        _all("golden_cases"),
        _all("score_feedback"),
        # (3)
        _all("llm_calls"),
        # (4)
        _all("batch_items"),
        _parent("purged_batch_items", "batch_submissions", "submission_id"),
        _all("batch_submissions"),
        _parent("admission_lane_credits", "llm_tuples", "llm_tuple_id"),
        _all("bulk_operations"),
        # (5) the jobs' graph, without tombstones, and every tombstone
        _parent("user_job_actions", "jobs", "job_id"),
        _parent("hard_filter_results", "jobs", "job_id"),
        _parent("job_revision_occurrences", "jobs", "job_id"),
        _parent("dedup_decisions", "jobs", "job_a"),  # `job_a < job_b`: either job within the bound
        _all("merge_events"),
        _all("identity_conflicts"),
        _parent("duplicate_group_members", "duplicate_groups", "group_id"),
        _all("duplicate_groups"),
        _parent("ad_key_sources", "postings", "posting_id"),
        _all("ad_keys", {"by": "created_at"}),
        _all("posting_observations"),
        _all("postings"),
        _parent("job_revision_sources", "job_revisions", "job_revision_id"),
        _all("job_revisions"),
        _all("jobs"),
        _parent("tombstone_keys", "job_tombstones", "tombstone_id"),
        _all("job_tombstones"),
        # (6)
        _all("raw_items"),
        _all("raw_responses"),
        _all("fetch_requests"),
        _parent("ats_snapshot_members", "ats_snapshots", "snapshot_id"),
        _parent("ats_snapshot_absences", "ats_snapshots", "snapshot_id"),
        _all("ats_snapshots"),
        _all("coverage_intervals"),
        _all("coverage_cells"),
        _all("queries"),
        _all("source_instances", keep=defaults),
        _all("watchlist_companies"),
        # (7) the runs within the bound, and those of a settings version within it
        _all("score_cycles"),
        _parent("run_attempts", "runs", "run_id"),
        _all("runs"),
        _parent("runs", "settings_versions", "settings_version_id"),
        # (9)
        _parent("profile_vectors", "profile_versions", "profile_version_id"),
        _all("query_proposals"),
        _all("llm_tuples"),
        _all("profile_drafts"),
        _all("profile_versions"),
        _all("cv_files"),
        *files,
        _all("embeddings"),
        # (10)
        _all("settings_versions"),
        *_listed("scope_versions", listed_scopes),
        # (11)
        *_listed("alerts", resolved),
        _all("checkpoint_writes", {"by": "thread"}),
        _all("checkpoint_blobs", {"by": "thread"}),
        _all("checkpoints", {"by": "thread"}),
        _all("api_idempotency", keep=[idempotency_row_id]),
        _all("operations", keep=own),
        _all("login_attempts"),
        _all("login_lockouts", {"by": "id", "column": "generation"}),
        *_listed(
            "procrastinate_events",
            finished,
            {"by": "parent", "table": "procrastinate_jobs", "column": "job_id"},
        ),
        *_listed("procrastinate_jobs", finished),
        # (12) the sessions, in the completing transaction
        _all("sessions", {"by": "created_at"}),
    ]
    return operations, retained


# ---- phase B: step 2 ---------------------------------------------------------------------------------------


async def _cancel_live_work(run: PurgeRun, bounds: Mapping[str, int]) -> None:
    """Step 2 (module docstring) in one chunk holding `active_config FOR UPDATE`."""
    async with run.chunk(config="update") as chunk:
        conn = chunk.conn
        runs = await cancel_active_runs(conn, reason=DELETE_EVERYTHING_CANCEL)
        cycles = await transition_where(
            conn, "score_cycles", ["running"], "failed", reason=CANCELLED_CYCLE, where="true", params={}
        )
        params = {"kind": PURGE_KIND, "m": run.manifest.id, "bound": bounds["operations"]}
        cancelled: list[int] = []
        proposal_ids: list[int] = []
        for row in (await conn.execute(_LIVE_OPERATIONS, params)).all():
            if await cancel_operation(conn, int(row.id), reason=CANCELLED_OPERATION):
                cancelled.append(int(row.id))
                proposal = row.payload.get("proposal_id") if row.kind == proposals.KIND else None
                if isinstance(proposal, int) and not isinstance(proposal, bool):
                    proposal_ids.append(proposal)
        failed = []
        if proposal_ids:  # the domain rows after their owners'
            failed = await transition_where(
                conn,
                "query_proposals",
                [proposals.RUNNING],
                proposals.FAILED,
                where=f"t.id = ANY({_IDS})",
                params={"ids": sorted(proposal_ids)},
            )
        spend = await deletion_graph.settle_live_spend(
            conn,
            submission_bound=bounds["batch_submissions"],
            ledger_bound=bounds["llm_spend_ledger"],
            bulk_bound=bounds["bulk_operations"],
            now=run.now,
        )
    observe_run_ends(c.end for c in runs)
    log.info(
        "delete_everything_live_work_cancelled",
        manifest_id=run.manifest.id,
        runs=[c.run_id for c in runs][:LOGGED_IDS],
        cycles=[int(c["id"]) for c in cycles][:LOGGED_IDS],
        operations=cancelled[:LOGGED_IDS],
        proposals_failed=[int(p["id"]) for p in failed][:LOGGED_IDS],
        cancel_recorded=list(spend.cancel_recorded),
    )


async def _send_recorded_cancels(run: PurgeRun, bounds: Mapping[str, int]) -> int:
    """Every recorded cancel sent after step 2's commit, with no transaction open, or skipped in a replay
    (module docstring); how many were accepted or found needless."""
    params = {"states": list(batches.IN_FLIGHT), "bound": bounds["batch_submissions"]}
    async with run.engine.connect() as conn:
        rows = (await conn.execute(_RECORDED_CANCELS, params)).all()
    sent = 0
    for row in rows:
        if run.effects is None:
            log.info(
                "batch_cancel_skipped_replay",
                submission_id=int(row.id),
                provider_batch_id=row.provider_batch_id,
            )
            continue
        sent += await batch_results.send_purge_cancel(
            run.effects.batches_client,
            str(row.provider_batch_id),
            submission_id=int(row.id),
            sleep=CANCEL_SLEEP,
        )
    return sent


# ---- phase B: the deletes ----------------------------------------------------------------------------------


async def _delete_paged(
    run: PurgeRun, statement: str, params: Mapping[str, object], *, limit: int = ROW_CHUNK
) -> int:
    """`statement` (a `DELETE ... WHERE id IN (SELECT ... LIMIT :limit)` of this module), one chunk a page,
    until a page is short; how many rows it deleted."""
    deleted = 0
    clause = q(statement)
    while True:
        async with run.chunk() as chunk:
            count = (await chunk.conn.execute(clause, {**params, "limit": limit})).rowcount
        deleted += count
        if count < limit:
            return deleted


def _within(table: str, *, order: Literal["ASC", "DESC"] = "ASC", extra: str = "") -> str:
    """The paged delete of `table`'s rows within its bound `:bound` (`extra` more of its predicate)."""
    return (
        f"DELETE FROM {table} WHERE id IN (SELECT id FROM {table} WHERE id <= :bound{extra} "  # noqa: S608
        f"ORDER BY id {order} LIMIT :limit)"
    )


async def _delete_targets(run: PurgeRun, bound: int) -> int:
    """Step 3.1: every target within the bound, `TARGET_SET_CHUNK` a chunk over their jobs; how many."""
    deleted = 0
    while True:
        async with run.engine.connect() as conn:
            page = (await conn.execute(_TARGET_PAGE, {"bound": bound, "limit": TARGET_SET_CHUNK})).all()
        if not page:
            return deleted
        async with run.chunk(job_ids=sorted({int(r.job_id) for r in page})) as chunk:
            left = (await chunk.conn.execute(_TARGETS_AGAIN, {"ids": [int(r.id) for r in page]})).all()
            if unheld := sorted({int(r.job_id) for r in left} - chunk.held):  # a target's job never changes
                raise InvariantViolation(
                    f"deletion manifest {run.manifest.id}: the jobs {unheld[:LOGGED_IDS]} of its targets are "
                    "not locked"
                )
            if left:
                purged = await deletion_graph.purge_target_set(
                    chunk.conn, [int(r.id) for r in left], manifest_id=run.manifest.id, now=run.now
                )
                deleted += purged.targets
                await ranking.bump(chunk.conn)
        if len(page) < TARGET_SET_CHUNK:
            return deleted


async def _settle_and_delete_calls(run: PurgeRun, page_of: TextClause, bound: int) -> int:
    """The calls `page_of` pages within the bound, `ROW_CHUNK` a chunk, their entries settled first
    (`deletion_graph.settle_and_delete_calls`, G04); how many."""
    deleted = 0
    while True:
        async with run.engine.connect() as conn:
            page = await _ids(conn, page_of, {"bound": bound, "limit": ROW_CHUNK})
        if not page:
            return deleted
        async with run.chunk() as chunk:
            deleted += len(await deletion_graph.settle_and_delete_calls(chunk.conn, page, now=run.now))
        if len(page) < ROW_CHUNK:
            return deleted


async def _delete_cases(run: PurgeRun, bound: int) -> int:
    """Step 3.2's golden cases, active or not, `ROW_CHUNK` a chunk over their jobs; how many."""
    deleted = 0
    while True:
        async with run.engine.connect() as conn:
            page = (await conn.execute(_CASE_PAGE, {"bound": bound, "limit": ROW_CHUNK})).all()
        if not page:
            return deleted
        async with run.chunk(job_ids=sorted({int(r.job_id) for r in page})) as chunk:
            cases = [int(r.id) for r in page]
            deleted += len(await deletion_graph.settle_and_delete_cases(chunk.conn, cases, now=run.now))
        if len(page) < ROW_CHUNK:
            return deleted


async def _delete_jobs(run: PurgeRun, bounds: Mapping[str, int]) -> int:
    """Step 3.5's graphs: per root within the bound, the root and every job merged into it through
    `deletion_graph.purge_job_graph(write_tombstone=False)`; how many jobs it deleted.
    `InvariantViolation` when a job within the bound is left."""
    deleted, after = 0, 0
    while True:
        async with run.engine.connect() as conn:
            params = {"bound": bounds["jobs"], "after": after, "limit": ROW_CHUNK}
            roots = await _ids(conn, _ROOT_PAGE, params)
        for root in roots:
            async with run.engine.connect() as conn:
                graph = await _ids(conn, _GRAPH, {"root": root})
            if graph:
                purged = await deletion_graph.purge_job_graph(
                    run, root, graph, write_tombstone=False, target_bound=bounds["score_targets"]
                )
                deleted += len(purged.graph.job_ids)
        if len(roots) < ROW_CHUNK:
            break
        after = roots[-1]
    async with run.engine.connect() as conn:
        if left := int((await conn.execute(_JOBS_LEFT, {"bound": bounds["jobs"]})).scalar_one()):
            raise InvariantViolation(
                f"deletion manifest {run.manifest.id}: {left} jobs within its bound left"
            )
    return deleted


async def _delete_rows(run: PurgeRun, bounds: Mapping[str, int], *tables: str) -> dict[str, int]:
    """Each of `tables`' rows within its bound, `ROW_CHUNK` a chunk; how many of each."""
    return {table: await _delete_paged(run, _within(table), {"bound": bounds[table]}) for table in tables}


async def _step_8(run: PurgeRun, content: ManifestContent) -> int | None:
    """Step 8 (module docstring) in one chunk holding `active_config FOR UPDATE`: the runs of a settings
    version within the bound, then the reset unless every pointer holds already; the new version's id."""
    bounds = content["max_ids"]
    async with run.chunk(config="update") as chunk:
        conn = chunk.conn
        stale = (await conn.execute(_SETTINGS_RUNS, {"settings": bounds["settings_versions"]})).rowcount
        if not await _pointer_violations(conn, content):
            log.info("delete_everything_reset_skipped", manifest_id=run.manifest.id, runs_deleted=stale)
            return None
        version_id = await reset_to_defaults(conn, now=run.now)
        await ranking.bump(conn)  # the last lock
    log.info("delete_everything_reset", manifest_id=run.manifest.id, settings_version_id=version_id)
    return version_id


async def _step_9(run: PurgeRun, content: ManifestContent) -> dict[str, int]:
    """Step 9 (module docstring): the vectors, proposals and tuples, then the drafts and versions (a deferred
    pair) in one chunk, then the CV rows; their files after that commit; then the embeddings."""
    bounds = content["max_ids"]
    async with run.chunk() as chunk:
        vectors = await chunk.conn.execute(_DELETE_VECTORS, {"bound": bounds["profile_versions"]})
    counts = {"profile_vectors": vectors.rowcount}
    counts.update(await _delete_rows(run, bounds, "query_proposals", "llm_tuples"))
    async with run.chunk() as chunk:  # the deferred pair, in one transaction
        drafts = await chunk.conn.execute(_DELETE_DRAFTS, {"bound": bounds["profile_drafts"]})
        versions = await chunk.conn.execute(_DELETE_VERSIONS, {"bound": bounds["profile_versions"]})
    counts.update({"profile_drafts": drafts.rowcount, "profile_versions": versions.rowcount})
    counts.update(await _delete_rows(run, bounds, "cv_files"))
    files = [
        key
        for op in content["operations"]
        if op["op"] == "file_remove" and op["volume"] == "cv_store"
        for key in op["keys"]
    ]
    for key in files:  # after the commit of the rows that named them
        run.cv_store.delete(key)
    counts["cv_store_files"] = len(files)
    counts.update(await _delete_rows(run, bounds, "embeddings"))
    return counts


def _thread_predicate(max_ids: Mapping[str, int]) -> tuple[str, dict[str, object]]:
    """A checkpoint row's thread within the bound (`manifests.THREAD_FORMS`: the owner its name carries,
    within its table's bound), over `thread_id`, with its binds."""
    params: dict[str, object] = {}
    cases: list[str] = []
    for i, form in enumerate(THREAD_FORMS):
        params[f"form_{i}"] = f"^(?:{form.pattern})$"
        params[f"owner_{i}"] = max_ids[form.owner]
        cases.append(
            f"WHEN thread_id ~ :form_{i} "
            f"THEN CAST((regexp_match(thread_id, :form_{i}))[1] AS numeric) <= :owner_{i}"
        )
    return f"CASE {' '.join(cases)} ELSE false END", params


async def _delete_threads(run: PurgeRun, max_ids: Mapping[str, int]) -> int:
    """Step 11's checkpoint threads within the bound, `ROW_CHUNK` threads a chunk, from the three tables;
    how many threads."""
    predicate, params = _thread_predicate(max_ids)
    tables = ("checkpoint_writes", "checkpoint_blobs", "checkpoints")
    page_of = q(
        " UNION ".join(f"SELECT thread_id FROM {table} WHERE {predicate}" for table in tables)  # noqa: S608
        + " ORDER BY thread_id LIMIT :limit"
    )
    deleted = 0
    while True:
        async with run.engine.connect() as conn:
            threads = [
                str(t) for t in (await conn.execute(page_of, {**params, "limit": ROW_CHUNK})).scalars()
            ]
        if not threads:
            return deleted
        async with run.chunk() as chunk:
            for table in tables:
                await chunk.conn.execute(
                    q(f"DELETE FROM {table} WHERE thread_id = ANY(CAST(:threads AS text[]))"),  # noqa: S608
                    {"threads": threads},
                )
        deleted += len(threads)
        if len(threads) < ROW_CHUNK:
            return deleted


def _row_deletes(content: ManifestContent, table: str) -> list[RowDeleteOp]:
    return [op for op in content["operations"] if op["op"] == "row_delete" and op["table"] == table]


def _named(content: ManifestContent, table: str) -> list[int]:
    """The ids the content's `row_delete` of `table` names by its own id."""
    return sorted(
        {i for op in _row_deletes(content, table) if op["bound"]["by"] == "id" for i in op.get("ids", [])}
    )


def _kept(content: ManifestContent, table: str) -> list[int]:
    """The `keep` ids of the content's `row_delete` of `table`."""
    return sorted({i for op in _row_deletes(content, table) for i in op.get("keep", [])})


async def _delete_listed(run: PurgeRun, table: str, ids: Sequence[int]) -> int:
    """The named rows of `table`, `ROW_CHUNK` a chunk; how many (a row gone already is skipped)."""
    deleted = 0
    for start in range(0, len(ids), ROW_CHUNK):
        async with run.chunk() as chunk:
            deleted += (
                await chunk.conn.execute(
                    q(f"DELETE FROM {table} WHERE id = ANY({_IDS})"),  # noqa: S608
                    {"ids": list(ids[start : start + ROW_CHUNK])},
                )
            ).rowcount
    return deleted


async def _pointer_violations(conn: AsyncConnection, content: ManifestContent) -> list[str]:
    """The pointers of step 8's postcondition that name a row within their table's bound outside `retained`:
    `<table>.<column>` each."""
    row: Any = (await conn.execute(_POINTER_VALUES)).one()
    max_ids, retained = content["max_ids"], content.get("retained", {})
    found: list[str] = []
    for table, column, names in _POINTERS:
        value = row.schedule_settings_version_id if table == "schedule_state" else getattr(row, column)
        if value is None or int(value) > max_ids[names] or int(value) in retained.get(names, []):
            continue
        found.append(f"{table}.{column}")
    return found


# ---- the procedure -----------------------------------------------------------------------------------------


class DeleteEverything:
    """The `delete_everything` procedure (module docstring)."""

    kind: ManifestKind = KIND
    deletes_exports: bool = True

    async def compute(
        self, conn: AsyncConnection, manifest: ManifestRow, *, now: datetime
    ) -> ManifestContent:
        max_ids = await max_identity_ids(conn)
        operations, retained = await _operations(conn, manifest, max_ids)
        content = new_content(manifest, max_ids=max_ids, operations=operations)
        content["retained"] = retained
        log.info("delete_everything_computed", manifest_id=manifest.id, operations=len(operations))
        return content

    async def execute(self, run: PurgeRun, content: ManifestContent) -> None:
        bounds = content["max_ids"]
        await _cancel_live_work(run, bounds)  # 2
        await afault_point(AFTER_CANCEL_RECORDED)
        counts: dict[str, int] = {"cancels_sent": await _send_recorded_cancels(run, bounds)}
        counts["score_targets"] = await _delete_targets(run, bounds["score_targets"])  # 3.1
        counts["eval_calls"] = await _settle_and_delete_calls(
            run, _EVAL_CALL_PAGE, bounds["llm_calls"]
        )  # 3.2
        async with run.chunk() as chunk:
            results = await chunk.conn.execute(_DELETE_CASE_RESULTS, {"bound": bounds["eval_runs"]})
        counts["eval_case_results"] = results.rowcount
        counts.update(await _delete_rows(run, bounds, "eval_runs"))
        counts["golden_cases"] = await _delete_cases(run, bounds["golden_cases"])
        counts.update(await _delete_rows(run, bounds, "score_feedback"))
        counts["llm_calls"] = await _settle_and_delete_calls(run, _CALL_PAGE, bounds["llm_calls"])  # 3.3
        counts.update(await _delete_rows(run, bounds, "batch_items", "batch_submissions"))  # 3.4
        async with run.chunk() as chunk:
            credits = await chunk.conn.execute(_DELETE_LANE_CREDITS, {"bound": bounds["llm_tuples"]})
        counts["admission_lane_credits"] = credits.rowcount
        counts.update(await _delete_rows(run, bounds, "bulk_operations"))
        counts["jobs"] = await _delete_jobs(run, bounds)  # 3.5
        counts.update(await _delete_rows(run, bounds, "duplicate_groups", "job_tombstones"))
        for table in ("fetch_requests", "raw_responses", "raw_items"):  # 3.6, newest first (module docstring)
            counts[table] = await _delete_paged(run, _within(table, order="DESC"), {"bound": bounds[table]})
        counts.update(
            await _delete_rows(
                run, bounds, "ats_snapshots", "coverage_intervals", "coverage_cells", "queries"
            )
        )
        defaults = _kept(content, "source_instances")
        counts["source_instances"] = await _delete_paged(
            run,
            _within("source_instances", extra=f" AND NOT (id = ANY({_IDS}))"),
            {"bound": bounds["source_instances"], "ids": defaults},
        )
        counts.update(await _delete_rows(run, bounds, "watchlist_companies"))
        if run.effects is not None and run.effects.deadman is not None:  # before 3.7, no transaction open
            counts["runs_pinged"] = await run.effects.deadman.ping_pending()
        counts.update(await _delete_rows(run, bounds, "score_cycles"))  # 3.7
        counts["runs"] = await _delete_paged(
            run, _RUNS_PAGE, {"bound": bounds["runs"], "settings": bounds["settings_versions"]}
        )
        await _step_8(run, content)  # 3.8
        counts.update(await _step_9(run, content))  # 3.9
        counts.update(await _delete_rows(run, bounds, "settings_versions"))  # 3.10
        counts["scope_versions"] = await _delete_listed(
            run, "scope_versions", _named(content, "scope_versions")
        )
        counts["alerts"] = await _delete_listed(run, "alerts", _named(content, "alerts"))  # 3.11
        counts["checkpoint_threads"] = await _delete_threads(run, bounds)
        for table in ("api_idempotency", "operations"):
            counts[table] = await _delete_paged(
                run,
                _within(table, extra=f" AND NOT (id = ANY({_IDS}))"),
                {"bound": bounds[table], "ids": _kept(content, table)},
            )
        counts.update(await _delete_rows(run, bounds, "login_attempts"))
        counts["login_lockouts"] = await _delete_paged(
            run, _LOCKOUTS_PAGE, {"bound": bounds["login_lockouts"]}
        )
        counts["procrastinate_jobs"] = await _delete_listed(
            run, "procrastinate_jobs", _named(content, "procrastinate_jobs")
        )
        log.info("delete_everything_executed", manifest_id=run.manifest.id, **counts)

    async def complete(self, conn: AsyncConnection, content: ManifestContent) -> None:
        """Step 12 in the completing transaction: every session created at or before `fenced_at`."""
        fenced_at = datetime.fromisoformat(content["fenced_at"])
        ended = (await conn.execute(_DELETE_SESSIONS, {"fenced_at": fenced_at})).rowcount
        log.info("delete_everything_sessions_ended", sessions=ended)

    async def postconditions(self, conn: AsyncConnection, content: ManifestContent) -> list[Violation]:
        index = next(
            i
            for i, op in enumerate(content["operations"])
            if op["op"] == "row_delete" and op["table"] == "settings_versions"
        )
        return [
            Violation(index, "row_delete", pointer.split(".")[0], f"{pointer} names a row the purge deletes")
            for pointer in await _pointer_violations(conn, content)
        ]

    async def reapply_admission(self, conn: AsyncConnection, manifest: ManifestRow, *, now: datetime) -> None:
        return None


register_kind(DeleteEverything())
