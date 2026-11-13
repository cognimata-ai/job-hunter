"""The Adzuna purge (spec §20.5, §19.5 steps 1 and 3, §20.2, §3.3.3; plan M5 Task 15, D10, D22, Deviations 13,
23, 50, 51 and 53): manifest kind `source_purge`, a `source` fence.

**A0** (`request_source_purge`, the caller's transaction; Task 18's route answers it): `active_config FOR
UPDATE`, then `fences.refuse_under_global_fence` (409 `delete_everything_in_progress` while
delete-everything's fence stands); while a source purge is `executing`, 409 `conflict_state` with `detail =
"purge_in_progress"`. Then, at A0's one `now` (the manifest's
`admitted_at`): a new internal settings version, the active document with `sources.enabled.<source> = false`,
when the active one enables it (`settings.save.save_settings(..., internal=True)`, D10);
`sources.purge_fence_at := now`; the `queued` and `running` collections `cancelled('source_purge')`
(`worker.runs.cancel_active_runs`, §7.16.4: owner rows, after `active_config`); then `purge.open_purge` with
the request `{"source_id": <source>}` and one `source` fence. `SOURCE_PURGE_SOURCES` is the one source that
can be purged so: `adzuna`. The runs it ended are returned (`OpenedSourcePurge.run_ends`) for the caller to
observe after its commit. The source switch's save defers no job (its one class is `collection`), so a
caller without a deferrer (`defer=None`, the tests' and replay's) gives it one that refuses any job
(`InvariantViolation`).

**The re-application** (`reapply_admission`, Task 25's replay, phase A, at the manifest's place in the stream,
under `active_config FOR UPDATE`): a dump from before the purge holds none of A0's durable effects beyond the
manifest, its fence and its operation, so each is applied again, idempotent and keeping a later explicit
choice: `purge_fence_at := admitted_at` when it is NULL or earlier (never lowered); a new internal version
disabling the source when the active one enables it and either was created at or before `admitted_at` or is
the replay's own (its id above the replay's `high_water`: an earlier manifest of the stream re-created it,
standing where a version made before this admission stood, such as delete-everything's reset; Ruling T25-3);
an active version of the dump created after `admitted_at` is the user's later choice and stays; the
collections queued at or before `admitted_at` and still `queued` or `running` cancelled.

**The content** (A2, `compute`, under the fence after the quiesce; never re-derived, §19.5 step 3), every id
within `max_ids`:
- the tainted revisions: every revision with a `job_revision_sources` row of the source (lineage survives a
  posting's deletion);
- the affected jobs (`affected.job_ids`, also added to the fence at A3): every job with a posting of the
  source, a tainted revision, a `det_results` instance whose `det_inputs` name the source, or a golden case on
  a tainted revision or with the source among its `det_input_sources`, each with every job it is merged into
  (the root is the one rebuilt), and the watermark (`fenced_at` and `max_ids`, `purge.compute_content` stamps
  the cutoff);
- the operations, in execution order: `row_delete score_targets` of the tainted revisions (a parent bound on
  `job_revisions`); `row_delete det_results` (the instances naming the source whose result is on an untainted
  revision: a tainted revision's go with its targets); `row_delete golden_cases` (active or not: the agreement
  overrides the exemption); `row_delete llm_calls` (the other eval calls whose `input_refs` name a tainted
  revision, no case's deletion taking them); `field_redact score_feedback score_snapshot` (final review I1:
  the feedback rows within the bound whose snapshot of L, M, U, coverage and confidence draws on the source:
  their scored revision tainted, their result on a tainted revision or their det instance naming the source,
  frozen here by id while those keys still name them, or their snapshot's recorded `sources` naming it,
  whatever keys retention nulled before (final fix C2; a snapshot written before that rule records none);
  their kind, label and comment stay);
  `row_delete postings`, `ad_keys` and `fetch_requests` with the
  selector `source:<source>` (bounded by id, by `created_at <= fenced_at` for the keys); `field_redact jobs
  current_revision_id` of the affected jobs whose current revision is tainted; `row_delete job_revisions` of
  the tainted ones; `row_delete embeddings` of the embeddings only tainted revisions reference (D22, frozen
  here); the `fence` (kind `source`, the affected job ids). The export ops come last
  (`purge.compute_content`; `deletes_exports`). An operation with nothing to name is left out (the selectors
  always stay: their postcondition holds the bound).

**Phase B** (`execute`, §20.5 steps 3-5), in `PurgeRun.chunk` transactions only, each statement over the rows
the content names or selects within its bound:
1. (3) the derivatives. The tainted revisions' targets, `TARGET_SET_CHUNK` a page: the page read, its jobs
   locked (`chunk(job_ids=...)`), the page read again under the lock (another deleter may have taken some),
   `deletion_graph.purge_target_set` (in-flight batches cancelled the P1 way, online entries settled); the
   instances, `ROW_CHUNK` a chunk over their jobs; the golden cases in one chunk over their jobs
   (`deletion_graph.settle_and_delete_cases(check=None)`: active or not, before any `delete_job_graph`, whose
   active-case refusal must never fire here); the eval calls, `ROW_CHUNK` a chunk
   (`deletion_graph.settle_and_delete_calls`: their entries settled first, G04); the frozen feedback rows'
   `score_snapshot` set NULL, `ROW_CHUNK` a chunk (live and in a replay alike; the deletions after it null
   their keys, never their snapshots);
2. (4) the source data, `ROW_CHUNK` a page by selector within the bound: the postings over their jobs
   (observations, key sources and the conflicts on them cascade; each deleted conflict's
   `dedup.identity_conflict` alert resolved), then the keys over their owners, then the fetch requests (raw
   responses and items cascade, the quota ledger keeps its rows with `fetch_request_id := NULL`); the fault
   point `privacy.source_purge.before_rebuild` (`BEFORE_REBUILD`) after their commits;
3. (5) `rebuild_jobs(run, affected)` (Task 25's phase B calls it too), within the bound of the run's
   manifest (its recorded `max_ids`: the source's postings and observations at or below it are the purged
   ones; data above it, collected after a re-enablement, is used and kept, §19.5 phase B): per root (every
   affected job followed up `merged_into_job_id`) that still exists, its family being it and every job merged
   into it. A synthetic golden-case root is left as it is (step 3 took its cases and instances naming the
   source; it has no posting to rebuild from and is never deleted). A family that keeps no posting (none of
   another source, none of the source above the bound) goes through `deletion_graph.purge_job_graph(...,
   write_tombstone=False)` with the bound `max_ids["score_targets"]`, as Task 13 runs the graph deletion; any
   other is rebuilt in one chunk over the family: the bulk rows of its targets' holds
   (`merge.lock_target_holds`, step 7), the family's tainted revisions (lineage naming a posting or
   observation of the source at or below the bound) found, `current_revision_id := NULL` where it names one,
   those revisions deleted (the fault point `privacy.source_purge.mid_rebuild`, `MID_REBUILD`, inside the
   uncommitted chunk), the root's availability from its postings (`merge.derive_from_postings`),
   `revisions.recompute_job(fenced_bound=...)` (the source's postings up to the bound left out, the ones above
   it used; an untainted revision of the same `material_hash` is reused, else a new one is inserted and
   pointed at), a merged member whose `field_provenance` still names a posting or observation of the source at
   or below the bound given the root's rebuilt canonical values (its own row held the values its moved posting
   gave it; unmerging recomputes it anyway), the previous current revision's unpublished targets cancelled
   `revision_superseded` when an untainted one stopped being current (as `merge.supersede_revision`, without
   the creation), then the det-facts hook (`filters.refilter_job_live`: the hard filters and the current
   `det_results` instances of the active tuple's published results for the active and pending scopes), the
   ranking bumped last. No target is created: `missing_targets` creates them after the release. It returns
   the number of roots (families) rebuilt. **In a replay** (`run.mode == "replay"`, `migrate` step 7's phase
   A, M5 Task 25) `execute` leaves the rebuild to the replay's phase B, which runs `rebuild_jobs` once over
   the whole stream (§19.5): here, the content's tainted revisions (`row_delete job_revisions`) are deleted
   instead, `ROW_CHUNK` a chunk over their jobs, `current_revision_id := NULL` first wherever one names them
   (the `field_redact`, as tombstoning step 6 does), so each chunk commits with no dangling deferred key
   (Fable N6) and no later manifest of the stream meets a revision a rebuild would have created. A golden case
   on one of them that the content does not name goes first (`deletion_graph.settle_and_delete_cases`): a
   later manifest of the stream deleted it before this one's A2 (completions out of stream order, after a
   requeued first run), and a dump from before both still holds it, whose key would refuse the revision's
   deletion;
4. the frozen embeddings: under the jobs of the revisions that still reference one (a revision outside the
   deleted set that took it since A2, same text), each such revision `embedding_status = 'awaiting_embedding'`
   with `embedding_id` NULL in one update (the CHECK allows nothing else), then every frozen embedding nobody
   references deleted; a revision that took one meanwhile is reset in the next round (at most
   `EMBEDDING_ROUNDS`, then the operation waits `EMBEDDING_REUSED` a minute). `awaiting_embedding` gives such
   a revision a new row, above the bound, so "no named row" holds for good.
Every chunk is idempotent: a re-execution after any commit, or a phase re-run after a lost deadlock (Ruling
T12-1), finds what is left. No file of a `file_remove` is the kind's own.

**Postconditions** (`postconditions`, phase C, beside the typed ones and the `fence` op's bounded checks over
the stored affected jobs): `sources.purge_fence_at >= admitted_at` (equal on the live path, where A0 wrote
both), and the active settings version disables the source unless it was created after `admitted_at` (only a
new settings version re-enables it). Data collected after a re-enablement lies above the watermark and is
never touched.

Ids, counts and codes only reach a log line.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal, cast

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.sql.elements import TextClause

from jobhunter.db import ranking
from jobhunter.db.sql import q
from jobhunter.dedup import merge, revisions
from jobhunter.dedup.merge import IDENTITY_CONFLICT_ALERT
from jobhunter.dedup.revisions import SourceBound
from jobhunter.errors import InvariantViolation
from jobhunter.faults import afault_point
from jobhunter.observability.alerts import resolve_alert
from jobhunter.observability.logging import get_logger
from jobhunter.privacy import deletion_graph
from jobhunter.privacy.fences import FenceSpec, purge_in_progress, refuse_under_global_fence
from jobhunter.privacy.manifests import (
    ManifestContent,
    ManifestKind,
    ManifestRow,
    Operation,
    Violation,
    max_identity_ids,
    read_manifest,
)
from jobhunter.privacy.purge import (
    RETRY_AFTER,
    OpenedPurge,
    PurgeRun,
    PurgeWait,
    new_content,
    open_purge,
    register_kind,
)
from jobhunter.scoring import filters, targets
from jobhunter.scoring.targets import TARGET_SET_CHUNK
from jobhunter.settings.active import lock_active
from jobhunter.settings.save import SaveRequest, save_settings
from jobhunter.types import JSONValue, get_str
from jobhunter.worker.runs import SOURCE_PURGE_CANCEL, RunEnd, cancel_active_runs
from jobhunter.worker.tasks import Deferrer, TaskName

KIND: Final = "source_purge"
REQUEST_KEY: Final = "source_id"
SOURCE_PURGE_SOURCES: Final = frozenset({"adzuna"})
# The fault points (module docstring): after the source data's commits, and inside a family's rebuild chunk.
BEFORE_REBUILD: Final = "privacy.source_purge.before_rebuild"
MID_REBUILD: Final = "privacy.source_purge.mid_rebuild"
ROW_CHUNK: Final = 500  # instances, calls and source rows per chunk
EMBEDDING_ROUNDS: Final = 3
EMBEDDING_REUSED: Final = "embedding_reused"  # the wait's error code (module docstring, phase B step 4)
LOGGED_IDS: Final = 20
# The `max_ids` keys the procedure reads (`max_ids_keys`, which a replay checks every manifest for): phase B's
# targets, postings and fetch requests, and the rebuild's bound (`_recorded_bound`).
BOUNDS_READ: Final = frozenset({"score_targets", "postings", "posting_observations", "fetch_requests"})

_IDS: Final = "CAST(:ids AS bigint[])"
_TAINTED_IDS: Final = "CAST(:tainted AS bigint[])"
# An instance whose `det_inputs` name the source (`manifests._SOURCE_DRAWS` reads it so).
_NAMES_SOURCE: Final = (
    "jsonb_path_exists(d.det_inputs, '$.job.*.sources[*] ? (@ == $source)', "
    "jsonb_build_object('source', CAST(:source AS text)))"
)
_POSTING_OF_SOURCE: Final = (
    "EXISTS (SELECT 1 FROM source_instances si WHERE si.id = p.source_instance_id AND si.source_id = :source)"
)

# ---- A0 ----------------------------------------------------------------------------------------------------

_LOCK_CONFIG: Final = q("SELECT 1 FROM active_config WHERE id = 1 FOR UPDATE")
_EXECUTING: Final = q(
    "SELECT EXISTS (SELECT 1 FROM deletion_manifests WHERE kind = 'source_purge' AND status = 'executing')"
)
_SET_FENCE_AT: Final = q("UPDATE sources SET purge_fence_at = :now WHERE id = :source")
_RAISE_FENCE_AT: Final = q(
    "UPDATE sources SET purge_fence_at = :at WHERE id = :source "
    "AND (purge_fence_at IS NULL OR purge_fence_at < :at) RETURNING id"
)
_VERSION_CREATED: Final = q("SELECT created_at FROM settings_versions WHERE id = :v")

# ---- A2 ----------------------------------------------------------------------------------------------------

_TAINTED: Final = q(
    "SELECT DISTINCT job_revision_id FROM job_revision_sources WHERE source_id = :source "
    "AND job_revision_id <= :bound ORDER BY job_revision_id"
)
_CASES: Final = q(
    f"SELECT id FROM golden_cases WHERE id <= :bound AND (job_revision_id = ANY({_TAINTED_IDS}) "  # noqa: S608
    "OR CAST(:source AS text) = ANY(det_input_sources)) ORDER BY id"
)
# Every job with a posting of the source, a tainted revision, an instance naming the source or one of the
# cases, with every job it is merged into.
_AFFECTED: Final = q(
    "WITH RECURSIVE direct(id) AS ("  # noqa: S608
    f"SELECT p.job_id FROM postings p WHERE p.id <= :postings AND {_POSTING_OF_SOURCE} "
    f"UNION SELECT r.job_id FROM job_revisions r WHERE r.id = ANY({_TAINTED_IDS}) "
    f"UNION SELECT d.job_id FROM det_results d WHERE d.id <= :det_results AND {_NAMES_SOURCE} "
    "UNION SELECT r.job_id FROM golden_cases g JOIN job_revisions r ON r.id = g.job_revision_id "
    "WHERE g.id = ANY(CAST(:cases AS bigint[]))), "
    "up(id, parent) AS (SELECT j.id, j.merged_into_job_id FROM jobs j WHERE j.id IN (SELECT id FROM direct) "
    "UNION SELECT p.id, p.merged_into_job_id FROM jobs p JOIN up ON p.id = up.parent) "
    "SELECT DISTINCT id FROM up ORDER BY id"
)
_INSTANCES: Final = q(
    "SELECT d.id FROM det_results d JOIN llm_results r ON r.id = d.llm_result_id "  # noqa: S608
    f"WHERE d.id <= :bound AND NOT (r.job_revision_id = ANY({_TAINTED_IDS})) AND {_NAMES_SOURCE} "
    "ORDER BY d.id"
)
_EVAL_CALLS: Final = q(
    "SELECT c.id FROM llm_calls c WHERE c.id <= :bound AND c.eval_run_id IS NOT NULL "
    "AND c.input_refs ->> 'job_revision_id' = ANY(CAST(:tainted_keys AS text[])) "
    "AND (c.golden_case_id IS NULL OR NOT (c.golden_case_id = ANY(CAST(:cases AS bigint[])))) "
    "ORDER BY c.id"
)
# The feedback within the bound whose score snapshot draws on the source (module docstring, final review I1):
# by its foreign keys, or by the sources its snapshot records, which retention cannot null (final fix C2).
_FEEDBACK: Final = q(
    "SELECT f.id FROM score_feedback f WHERE f.id <= :bound AND f.score_snapshot IS NOT NULL AND ("  # noqa: S608
    f"f.job_revision_id = ANY({_TAINTED_IDS}) "
    "OR EXISTS (SELECT 1 FROM llm_results r WHERE r.id = f.llm_result_id "
    f"AND r.job_revision_id = ANY({_TAINTED_IDS})) "
    f"OR EXISTS (SELECT 1 FROM det_results d WHERE d.id = f.det_result_id AND {_NAMES_SOURCE}) "
    "OR f.score_snapshot -> 'sources' @> jsonb_build_array(CAST(:source AS text))) ORDER BY f.id"
)
_CURRENT_TAINTED: Final = q(
    "SELECT id FROM jobs WHERE id = ANY(CAST(:jobs AS bigint[])) "  # noqa: S608
    f"AND current_revision_id = ANY({_TAINTED_IDS}) ORDER BY id"
)
_FROZEN_EMBEDDINGS: Final = q(
    "SELECT e.id FROM embeddings e WHERE e.id <= :bound "  # noqa: S608
    f"AND e.id IN (SELECT r.embedding_id FROM job_revisions r WHERE r.id = ANY({_TAINTED_IDS})) "
    "AND NOT EXISTS (SELECT 1 FROM job_revisions o WHERE o.embedding_id = e.id "
    f"AND NOT (o.id = ANY({_TAINTED_IDS}))) "
    "AND NOT EXISTS (SELECT 1 FROM profile_vectors v WHERE v.embedding_id = e.id) ORDER BY e.id"
)

# ---- phase B -----------------------------------------------------------------------------------------------

_TARGET_PAGE: Final = q(
    "SELECT id, job_id FROM score_targets WHERE job_revision_id = ANY(CAST(:revisions AS bigint[])) "
    "AND id <= :bound ORDER BY id LIMIT :limit"
)
_TARGETS_AGAIN: Final = q(f"SELECT id, job_id FROM score_targets WHERE id = ANY({_IDS}) ORDER BY id")  # noqa: S608
_JOBS_OF_INSTANCES: Final = q(f"SELECT DISTINCT job_id FROM det_results WHERE id = ANY({_IDS})")  # noqa: S608
_DELETE_INSTANCES: Final = q(f"DELETE FROM det_results WHERE id = ANY({_IDS})")  # noqa: S608
_REDACT_FEEDBACK: Final = q(
    f"UPDATE score_feedback SET score_snapshot = NULL WHERE id = ANY({_IDS}) "  # noqa: S608
    "AND score_snapshot IS NOT NULL"
)
_JOBS_OF_CASES: Final = q(
    "SELECT DISTINCT r.job_id FROM golden_cases g JOIN job_revisions r ON r.id = g.job_revision_id "  # noqa: S608
    f"WHERE g.id = ANY({_IDS})"
)
_POSTING_PAGE: Final = q(
    "SELECT p.id, p.job_id FROM postings p "  # noqa: S608
    f"WHERE p.id <= :bound AND {_POSTING_OF_SOURCE} ORDER BY p.id LIMIT :limit"
)
_CONFLICTS_ON: Final = q(
    f"SELECT id FROM identity_conflicts WHERE posting_id = ANY({_IDS}) ORDER BY id FOR UPDATE"  # noqa: S608
)
_DELETE_POSTINGS: Final = q(f"DELETE FROM postings WHERE id = ANY({_IDS}) RETURNING id")  # noqa: S608
_KEY_PAGE: Final = q(
    "SELECT key_value, job_id FROM ad_keys WHERE key_ns = :source AND created_at <= :fenced_at "
    "ORDER BY key_value LIMIT :limit"
)
_DELETE_KEYS: Final = q(
    "DELETE FROM ad_keys WHERE key_ns = :source AND key_value = ANY(CAST(:values AS text[])) "
    "AND created_at <= :fenced_at"
)
_REQUEST_PAGE: Final = q(
    "SELECT f.id FROM fetch_requests f WHERE f.id <= :bound AND EXISTS (SELECT 1 FROM source_instances si "
    "WHERE si.id = f.source_instance_id AND si.source_id = :source) ORDER BY f.id LIMIT :limit"
)
_DELETE_REQUESTS: Final = q(f"DELETE FROM fetch_requests WHERE id = ANY({_IDS})")  # noqa: S608

# The rebuild (step 5): each given job's root (with whether it is a synthetic golden-case job), a root's
# family, whether the family keeps a posting (one of another source, or of the source above the bound), and
# the family's revisions tainted within the bound (lineage naming a posting or observation of the source at or
# below it, as the `fence` postcondition reads lineage).
_ROOTS: Final = q(
    "WITH RECURSIVE up(id, parent) AS (SELECT id, merged_into_job_id FROM jobs "
    "WHERE id = ANY(CAST(:jobs AS bigint[])) "
    "UNION SELECT p.id, p.merged_into_job_id FROM jobs p JOIN up ON p.id = up.parent) "
    "SELECT DISTINCT up.id, j.synthetic_eval FROM up JOIN jobs j ON j.id = up.id WHERE up.parent IS NULL "
    "ORDER BY up.id"
)
_FAMILY: Final = q(
    "WITH RECURSIVE g(id) AS (SELECT id FROM jobs WHERE id = :root "
    "UNION SELECT j.id FROM jobs j JOIN g ON j.merged_into_job_id = g.id) SELECT id FROM g ORDER BY id"
)
_POSTINGS_LEFT: Final = q(
    "SELECT EXISTS (SELECT 1 FROM postings p WHERE p.job_id = ANY(CAST(:jobs AS bigint[])) "  # noqa: S608
    f"AND NOT (p.id <= :postings AND {_POSTING_OF_SOURCE}))"
)
_TAINTED_OF_FAMILY: Final = q(
    "SELECT r.id FROM job_revisions r WHERE r.job_id = ANY(CAST(:jobs AS bigint[])) AND EXISTS "
    "(SELECT 1 FROM job_revision_sources s WHERE s.job_revision_id = r.id AND s.source_id = :source "
    "AND (coalesce(s.posting_id <= :postings, false) OR coalesce(s.observation_id <= :observations, false))) "
    "ORDER BY r.id"
)
_UNPOINT: Final = q(
    "UPDATE jobs SET current_revision_id = NULL WHERE id = ANY(CAST(:jobs AS bigint[])) "  # noqa: S608
    f"AND current_revision_id = ANY({_TAINTED_IDS})"
)
_DELETE_REVISIONS: Final = q(f"DELETE FROM job_revisions WHERE id = ANY({_TAINTED_IDS})")  # noqa: S608
# A replay's phase A (module docstring, phase B step 3): the jobs of the content's tainted revisions left, and
# the golden cases still on them.
_JOBS_OF_REVISIONS: Final = q(
    f"SELECT DISTINCT job_id FROM job_revisions WHERE id = ANY({_TAINTED_IDS}) ORDER BY job_id"  # noqa: S608
)
_CASES_ON: Final = q(
    f"SELECT id FROM golden_cases WHERE job_revision_id = ANY({_TAINTED_IDS}) ORDER BY id"  # noqa: S608
)
_CURRENT: Final = q("SELECT current_revision_id FROM jobs WHERE id = :job")
_REVISION_EXISTS: Final = q("SELECT EXISTS (SELECT 1 FROM job_revisions WHERE id = :r)")
# A merged member whose `field_provenance` names the source takes the root's rebuilt canonical values.
_COPIED: Final = (
    *(column for column in revisions.LIST_COLUMNS if column != "current_revision_id"),
    "field_provenance",
    "det_facts_hash",
)
# (Its provenance test is the `fence` postcondition's: an entry naming a posting or observation of the source
# at or below the bound.)
_TAKE_ROOTS_VALUES: Final = q(
    f"UPDATE jobs m SET {', '.join(f'{c} = r.{c}' for c in _COPIED)}, updated_at = now() "  # noqa: S608
    "FROM jobs r WHERE r.id = :root AND m.id = ANY(CAST(:members AS bigint[])) AND m.id <> r.id "
    "AND EXISTS (SELECT 1 FROM jsonb_each(m.field_provenance) e JOIN source_instances si "
    "ON si.id = CAST(e.value ->> 'source_instance_id' AS bigint) WHERE si.source_id = :source "
    "AND (coalesce(CAST(e.value ->> 'posting_id' AS bigint) <= :postings, false) "
    "OR coalesce(CAST(e.value ->> 'observation_id' AS bigint) <= :observations, false))) RETURNING m.id"
)

# The embeddings (step 4 of phase B).
_EMBEDDINGS_LEFT: Final = q(f"SELECT id FROM embeddings WHERE id = ANY({_IDS}) ORDER BY id")  # noqa: S608
_REUSERS_JOBS: Final = q(
    f"SELECT DISTINCT job_id FROM job_revisions WHERE embedding_id = ANY({_IDS}) ORDER BY job_id"  # noqa: S608
)
_LOCK_EMBEDDINGS: Final = q(
    f"SELECT id FROM embeddings WHERE id = ANY({_IDS}) ORDER BY id FOR UPDATE"  # noqa: S608
)
_RESET_REUSERS: Final = q(
    "UPDATE job_revisions SET embedding_status = 'awaiting_embedding', embedding_id = NULL "  # noqa: S608
    f"WHERE embedding_id = ANY({_IDS}) AND job_id = ANY(CAST(:held AS bigint[])) RETURNING id"
)
_DELETE_EMBEDDINGS: Final = q(
    f"DELETE FROM embeddings e WHERE e.id = ANY({_IDS}) "  # noqa: S608
    "AND NOT EXISTS (SELECT 1 FROM job_revisions r WHERE r.embedding_id = e.id) RETURNING e.id"
)

# ---- the postconditions ------------------------------------------------------------------------------------

_FENCE_AT: Final = q("SELECT purge_fence_at FROM sources WHERE id = :source")
_ACTIVE_VERSION: Final = q(
    "SELECT v.payload, v.created_at FROM active_config a JOIN settings_versions v "
    "ON v.id = a.settings_version_id WHERE a.id = 1"
)

log = get_logger("jobhunter.privacy.kinds.source_purge")


# ---- A0 ----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class OpenedSourcePurge(OpenedPurge):
    """What A0 committed: the manifest and its `purge` operation, with the collections it cancelled and their
    ends, which the caller observes after its commit (`worker.runs.observe_run_ends`)."""

    cancelled_run_ids: tuple[int, ...] = ()
    run_ends: tuple[RunEnd, ...] = ()


async def _defer_nothing(
    conn: AsyncConnection,
    task: TaskName,
    *,
    lock: str | None = None,
    queueing_lock: str | None = None,
    **kwargs: JSONValue,
) -> int | None:
    """The deferrer of a caller that has none (module docstring): the source switch's save defers nothing."""
    raise InvariantViolation(f"a source purge's settings save deferred {task}: it has no deferrer")


def _enables(payload: JSONValue, source: str) -> bool:
    if not isinstance(payload, dict):
        raise TypeError("a settings payload is an object")
    sources = cast(Mapping[str, Any], payload.get("sources", {}))
    return bool(sources.get("enabled", {}).get(source, True))


def _disabled(payload: JSONValue, source: str) -> dict[str, JSONValue]:
    """The settings document `payload` with the source switched off, and nothing else changed."""
    if not isinstance(payload, dict):
        raise TypeError("a settings payload is an object")
    sources = cast(dict[str, Any], payload.get("sources", {}))
    enabled = {**cast(dict[str, JSONValue], sources.get("enabled", {})), source: False}
    return {**payload, "sources": {**sources, "enabled": enabled}}


async def _disable_source(
    conn: AsyncConnection,
    source: str,
    *,
    now: datetime,
    defer: Deferrer | None,
    created_by: datetime | None,
    replay_own_above: int | None = None,
) -> int | None:
    """A new internal settings version disabling the source, when the active one enables it (and, with
    `created_by`, was created at or before it, or has an id above `replay_own_above`: the replay's own); the
    new version's id, `None` when none was needed. The caller holds `active_config FOR UPDATE` (`lock_active`
    is then a no-op lock and a fresh read)."""
    active = await lock_active(conn)
    if not _enables(active.payload, source):
        return None
    replays_own = replay_own_above is not None and active.settings_version_id > replay_own_above
    if created_by is not None and not replays_own:
        created = (await conn.execute(_VERSION_CREATED, {"v": active.settings_version_id})).scalar_one()
        if created > created_by:
            return None  # the user's later choice
    request = SaveRequest(
        payload=_disabled(active.payload, source),
        base_version_id=active.settings_version_id,
        acknowledged_warnings=frozenset(),
    )
    saved = await save_settings(
        conn, request, now=now, defer=_defer_nothing if defer is None else defer, internal=True
    )
    return saved.version_id


def _checked_source(source_id: str) -> str:
    if source_id not in SOURCE_PURGE_SOURCES:
        raise ValueError(f"no source purge of {source_id!r}: only {sorted(SOURCE_PURGE_SOURCES)}")
    return source_id


async def request_source_purge(
    conn: AsyncConnection, *, source_id: Literal["adzuna"], now: datetime, defer: Deferrer | None
) -> OpenedSourcePurge:
    """A0 of the purge of `source_id` in the caller's transaction (module docstring); the opened purge.
    `Refusal(409)` under delete-everything's fence or while a source purge is executing."""
    source = _checked_source(source_id)
    await conn.execute(_LOCK_CONFIG)  # A0's lock
    await refuse_under_global_fence(conn)
    if (await conn.execute(_EXECUTING)).scalar_one():
        log.info("source_purge_refused", source_id=source, reason="purge_in_progress")
        raise purge_in_progress()
    version = await _disable_source(conn, source, now=now, defer=defer, created_by=None)
    await conn.execute(_SET_FENCE_AT, {"now": now, "source": source})
    cancelled = await cancel_active_runs(conn, reason=SOURCE_PURGE_CANCEL)
    opened = await open_purge(
        conn,
        kind=KIND,
        request={REQUEST_KEY: source},
        fences=[FenceSpec(kind="source", source_id=source)],
        now=now,
        defer=defer,
    )
    log.info(
        "source_purge_requested",
        source_id=source,
        manifest_id=opened.manifest_id,
        settings_version_id=version,
        runs_cancelled=[c.run_id for c in cancelled],
    )
    return OpenedSourcePurge(
        opened.manifest_id,
        opened.manifest_uuid,
        opened.operation_id,
        cancelled_run_ids=tuple(c.run_id for c in cancelled),
        run_ends=tuple(c.end for c in cancelled),
    )


# ---- the content -------------------------------------------------------------------------------------------


async def _ids(conn: AsyncConnection, statement: TextClause, params: Mapping[str, object]) -> list[int]:
    return [int(i) for i in (await conn.execute(statement, dict(params))).scalars()]


def _source_of(manifest: ManifestRow) -> str:
    return _checked_source(get_str(dict(manifest.request), REQUEST_KEY))


def _named(content: ManifestContent, op: str, table: str) -> list[int]:
    """The ids the content's `op` on `table` names (a parent-bound row delete's are its parents')."""
    named: set[int] = set()
    for each in content["operations"]:
        if each["op"] == op and each.get("table") == table:
            named.update(cast(list[int], each.get("ids", [])))
    return sorted(named)


def _by_ids(table: str, ids: Sequence[int]) -> list[Operation]:
    return [{"op": "row_delete", "table": table, "ids": list(ids), "bound": {"by": "id"}}] if ids else []


def _by_source(table: str, source: str, *, by: Literal["id", "created_at"] = "id") -> Operation:
    return {"op": "row_delete", "table": table, "selector": f"source:{source}", "bound": {"by": by}}


async def _operations(
    conn: AsyncConnection, source: str, max_ids: Mapping[str, int]
) -> tuple[list[Operation], list[int]]:
    """The content's operations (module docstring) and the affected jobs."""
    tainted = await _ids(conn, _TAINTED, {"source": source, "bound": max_ids["job_revisions"]})
    cases = await _ids(conn, _CASES, {"source": source, "tainted": tainted, "bound": max_ids["golden_cases"]})
    jobs = await _ids(
        conn,
        _AFFECTED,
        {
            "source": source,
            "tainted": tainted,
            "cases": cases,
            "postings": max_ids["postings"],
            "det_results": max_ids["det_results"],
        },
    )
    instances = await _ids(
        conn, _INSTANCES, {"source": source, "tainted": tainted, "bound": max_ids["det_results"]}
    )
    calls = await _ids(
        conn,
        _EVAL_CALLS,
        {"bound": max_ids["llm_calls"], "tainted_keys": [str(r) for r in tainted], "cases": cases},
    )
    feedback = await _ids(
        conn, _FEEDBACK, {"source": source, "tainted": tainted, "bound": max_ids["score_feedback"]}
    )
    unpointed = await _ids(conn, _CURRENT_TAINTED, {"jobs": jobs, "tainted": tainted})
    frozen = await _ids(conn, _FROZEN_EMBEDDINGS, {"tainted": tainted, "bound": max_ids["embeddings"]})
    operations: list[Operation] = []
    if tainted:
        operations.append(
            {
                "op": "row_delete",
                "table": "score_targets",
                "ids": tainted,
                "bound": {"by": "parent", "table": "job_revisions", "column": "job_revision_id"},
            }
        )
    operations += _by_ids("det_results", instances)
    operations += _by_ids("golden_cases", cases)
    operations += _by_ids("llm_calls", calls)
    if feedback:
        operations.append(
            {"op": "field_redact", "table": "score_feedback", "ids": feedback, "columns": ["score_snapshot"]}
        )
    operations += [
        _by_source("postings", source),
        _by_source("ad_keys", source, by="created_at"),
        _by_source("fetch_requests", source),
    ]
    if unpointed:
        operations.append(
            {"op": "field_redact", "table": "jobs", "ids": unpointed, "columns": ["current_revision_id"]}
        )
    operations += _by_ids("job_revisions", tainted)
    operations += _by_ids("embeddings", frozen)
    operations.append({"op": "fence", "kind": "source", "key": source, "job_ids": jobs})
    return operations, jobs


# ---- phase B: the derivatives ------------------------------------------------------------------------------


def _slices(ids: Sequence[int], size: int = ROW_CHUNK) -> list[list[int]]:
    return [list(ids[i : i + size]) for i in range(0, len(ids), size)]


async def _delete_targets(run: PurgeRun, tainted: Sequence[int], bound: int) -> int:
    """Step 3's targets (module docstring): the tainted revisions', `TARGET_SET_CHUNK` per chunk; how many."""
    deleted = 0
    while True:
        params = {"revisions": list(tainted), "bound": bound, "limit": TARGET_SET_CHUNK}
        async with run.engine.connect() as conn:
            page = (await conn.execute(_TARGET_PAGE, params)).all()
        if not page:
            return deleted
        async with run.chunk(job_ids=sorted({int(r.job_id) for r in page})) as chunk:
            left = (await chunk.conn.execute(_TARGETS_AGAIN, {"ids": [int(r.id) for r in page]})).all()
            if unheld := sorted({int(r.job_id) for r in left} - chunk.held):
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


async def _delete_instances(run: PurgeRun, instances: Sequence[int]) -> int:
    """Step 3's instances naming the source, `ROW_CHUNK` per chunk over their jobs; how many."""
    deleted = 0
    for ids in _slices(instances):
        async with run.engine.connect() as conn:
            jobs = await _ids(conn, _JOBS_OF_INSTANCES, {"ids": ids})
        if not jobs:
            continue  # deleted already
        async with run.chunk(job_ids=jobs) as chunk:
            deleted += (await chunk.conn.execute(_DELETE_INSTANCES, {"ids": ids})).rowcount
            await ranking.bump(chunk.conn)
    return deleted


async def _delete_cases(run: PurgeRun, cases: Sequence[int]) -> int:
    """Step 3's golden cases, active or not, `ROW_CHUNK` per chunk over their jobs; how many were deleted."""
    deleted = 0
    for ids in _slices(cases):
        async with run.engine.connect() as conn:
            jobs = await _ids(conn, _JOBS_OF_CASES, {"ids": ids})
        if not jobs:
            continue  # deleted already
        async with run.chunk(job_ids=jobs) as chunk:
            deleted += len(
                await deletion_graph.settle_and_delete_cases(chunk.conn, ids, now=run.now, check=None)
            )
    return deleted


async def _redact_feedback(run: PurgeRun, feedback: Sequence[int]) -> int:
    """Step 3's feedback snapshots drawing on the source, `ROW_CHUNK` per chunk: `score_snapshot` NULL, the
    row with its kind, label and comment kept; how many were redacted."""
    redacted = 0
    for ids in _slices(feedback):
        async with run.chunk() as chunk:
            redacted += (await chunk.conn.execute(_REDACT_FEEDBACK, {"ids": ids})).rowcount
    return redacted


async def _delete_calls(run: PurgeRun, calls: Sequence[int]) -> int:
    """Step 3's other eval calls, `ROW_CHUNK` per chunk, their entries settled first; how many."""
    deleted = 0
    for ids in _slices(calls):
        async with run.chunk() as chunk:
            deleted += len(await deletion_graph.settle_and_delete_calls(chunk.conn, ids, now=run.now))
    return deleted


# ---- phase B: the source data ------------------------------------------------------------------------------


async def _delete_postings(run: PurgeRun, source: str, bound: int) -> int:
    """Step 4's postings of the source within the bound, `ROW_CHUNK` a chunk over their jobs; how many."""
    deleted = 0
    while True:
        params = {"source": source, "bound": bound, "limit": ROW_CHUNK}
        async with run.engine.connect() as conn:
            page = (await conn.execute(_POSTING_PAGE, params)).all()
        if not page:
            return deleted
        ids = [int(r.id) for r in page]
        async with run.chunk(job_ids=sorted({int(r.job_id) for r in page})) as chunk:
            conflicts = await _ids(chunk.conn, _CONFLICTS_ON, {"ids": ids})
            deleted += len((await chunk.conn.execute(_DELETE_POSTINGS, {"ids": ids})).all())
            for conflict in conflicts:  # gone with their posting (cascade)
                await resolve_alert(chunk.conn, code=IDENTITY_CONFLICT_ALERT, subject=f"conflict:{conflict}")
        if len(page) < ROW_CHUNK:
            return deleted


async def _delete_keys(run: PurgeRun, source: str, fenced_at: datetime) -> int:
    """Step 4's keys of the source's namespace created by the cutoff, `ROW_CHUNK` a chunk over their
    owners; how many."""
    deleted = 0
    while True:
        params = {"source": source, "fenced_at": fenced_at, "limit": ROW_CHUNK}
        async with run.engine.connect() as conn:
            page = (await conn.execute(_KEY_PAGE, params)).all()
        if not page:
            return deleted
        async with run.chunk(job_ids=sorted({int(r.job_id) for r in page})) as chunk:
            values = [str(r.key_value) for r in page]
            params = {"source": source, "values": values, "fenced_at": fenced_at}
            deleted += (await chunk.conn.execute(_DELETE_KEYS, params)).rowcount
        if len(page) < ROW_CHUNK:
            return deleted


async def _delete_requests(run: PurgeRun, source: str, bound: int) -> int:
    """Step 4's fetch requests of the source within the bound, `ROW_CHUNK` a chunk; how many."""
    deleted = 0
    while True:
        params = {"source": source, "bound": bound, "limit": ROW_CHUNK}
        async with run.engine.connect() as conn:
            page = await _ids(conn, _REQUEST_PAGE, params)
        if not page:
            return deleted
        async with run.chunk() as chunk:
            deleted += (await chunk.conn.execute(_DELETE_REQUESTS, {"ids": page})).rowcount
        if len(page) < ROW_CHUNK:
            return deleted


# ---- phase B: the rebuild ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Bound:
    """The manifest's bound the rebuild works within (its `max_ids`): the source's rows at or below it are the
    purged ones; those above it were collected after a re-enablement and are used and kept."""

    source: SourceBound
    score_targets: int


async def _recorded_bound(run: PurgeRun, source: str) -> _Bound:
    """The bound of the run's manifest, from its recorded content (read again when the run began before A3
    recorded it)."""
    scope = run.manifest.scope
    if scope is None:
        async with run.engine.connect() as conn:
            manifest = await read_manifest(conn, run.manifest.id)
        scope = None if manifest is None else manifest.scope
    if scope is None:
        raise InvariantViolation(
            f"deletion manifest {run.manifest.id}: no recorded content to rebuild within"
        )
    max_ids = scope["max_ids"]
    bound = SourceBound(source, max_ids["postings"], max_ids["posting_observations"])
    return _Bound(bound, max_ids["score_targets"])


def _bound_params(bound: _Bound) -> dict[str, object]:
    return {
        "source": bound.source.source_id,
        "postings": bound.source.postings,
        "observations": bound.source.observations,
    }


async def _families(engine: AsyncEngine, job_ids: Sequence[int]) -> tuple[dict[int, list[int]], list[int]]:
    """Each existing root of `job_ids` (followed up `merged_into_job_id`) with its family, in id order, and
    the synthetic golden-case roots apart (never rebuilt nor deleted)."""
    families: dict[int, list[int]] = {}
    synthetic: list[int] = []
    async with engine.connect() as conn:
        for row in (await conn.execute(_ROOTS, {"jobs": sorted(set(job_ids))})).all():
            if row.synthetic_eval:
                synthetic.append(int(row.id))
            else:
                families[int(row.id)] = await _ids(conn, _FAMILY, {"root": int(row.id)})
    return families, synthetic


async def _postings_left(engine: AsyncEngine, family: Sequence[int], bound: _Bound) -> bool:
    """Whether the family keeps a posting: one of another source, or of the source above the bound."""
    params = {"jobs": list(family), **_bound_params(bound)}
    async with engine.connect() as conn:
        return bool((await conn.execute(_POSTINGS_LEFT, params)).scalar_one())


async def _rebuild(run: PurgeRun, root: int, family: list[int], bound: _Bound) -> bool:
    """One family's rebuild transaction (module docstring, phase B step 3); `False` when the root is gone."""
    params = _bound_params(bound)
    async with run.chunk(job_ids=family) as chunk:
        conn = chunk.conn
        if root not in chunk.held:
            return False
        await merge.lock_target_holds(conn, [root])  # step 7, before any target row
        tainted = await _ids(conn, _TAINTED_OF_FAMILY, {"jobs": family, **params})
        previous = (await conn.execute(_CURRENT, {"job": root})).scalar_one()
        if tainted:
            await conn.execute(_UNPOINT, {"jobs": family, "tainted": tainted})
            await conn.execute(_DELETE_REVISIONS, {"tainted": tainted})
        await afault_point(MID_REBUILD)
        await merge.derive_from_postings(conn, root)
        result = await revisions.recompute_job(conn, root, fenced_bound=bound.source)
        rewrite = {"root": root, "members": family, **params}
        members = [int(m) for m in (await conn.execute(_TAKE_ROOTS_VALUES, rewrite)).scalars()]
        superseded = 0
        if previous is not None and int(previous) not in tainted and int(previous) != result.revision_id:
            if (await conn.execute(_REVISION_EXISTS, {"r": int(previous)})).scalar_one():
                cancelled = await targets.cancel_targets(
                    conn, revision_ids=[int(previous)], reason=targets.REVISION_SUPERSEDED, now=run.now
                )
                superseded = cancelled.targets
        active = await filters.lock_active_scopes(conn)  # held already (step 2): its pointers read again
        live = await filters.refilter_job_live(conn, root, active=active)
        await ranking.bump(conn)
    log.info(
        "source_purge_job_rebuilt",
        manifest_id=run.manifest.id,
        job_id=root,
        revision_id=result.revision_id,
        revision_created=result.revision_created,
        tainted_revisions=tainted[:LOGGED_IDS],
        members_rewritten=members[:LOGGED_IDS],
        targets_superseded=superseded,
        instances=len(live.instances),
    )
    return True


async def rebuild_jobs(run: PurgeRun, job_ids: Sequence[int]) -> int:
    """§20.5 step 5 for the affected `job_ids` that still exist (module docstring, phase B step 3), within the
    bound of the run's manifest: each root's family rebuilt in one transaction, or deleted whole without a
    tombstone when no posting is left; a synthetic golden-case root is left as it is. Returns how many roots
    (families) were rebuilt. The live purge's phase B and Task 25's replay (phase B) call it."""
    source = _source_of(run.manifest)
    bound = await _recorded_bound(run, source)
    families, synthetic = await _families(run.engine, job_ids)
    rebuilt = deleted = 0
    for root, family in families.items():
        if not await _postings_left(run.engine, family, bound):
            purged = await deletion_graph.purge_job_graph(
                run, root, family, write_tombstone=False, target_bound=bound.score_targets
            )
            deleted += len(purged.graph.job_ids)
        elif await _rebuild(run, root, family, bound):
            rebuilt += 1
    log.info(
        "source_purge_rebuilt",
        manifest_id=run.manifest.id,
        roots_rebuilt=rebuilt,
        jobs_deleted=deleted,
        synthetic_left=synthetic[:LOGGED_IDS],
    )
    return rebuilt


async def _delete_tainted_revisions(run: PurgeRun, tainted: Sequence[int]) -> int:
    """A replay's phase A in place of the rebuild (module docstring, phase B step 3): the content's tainted
    revisions, `ROW_CHUNK` a chunk over their jobs, the golden cases still on them deleted and the revisions
    unpointed first; how many were deleted."""
    deleted = 0
    for ids in _slices(tainted):
        async with run.engine.connect() as conn:
            jobs = await _ids(conn, _JOBS_OF_REVISIONS, {"tainted": ids})
        if not jobs:
            continue  # deleted already
        async with run.chunk(job_ids=jobs) as chunk:
            if cases := await _ids(chunk.conn, _CASES_ON, {"tainted": ids}):
                await deletion_graph.settle_and_delete_cases(chunk.conn, cases, now=run.now, check=None)
                log.info(
                    "source_purge_replay_cases_deleted",
                    manifest_id=run.manifest.id,
                    cases=len(cases),
                    case_ids=cases[:LOGGED_IDS],
                )
            await chunk.conn.execute(_UNPOINT, {"jobs": sorted(chunk.held), "tainted": ids})
            deleted += (await chunk.conn.execute(_DELETE_REVISIONS, {"tainted": ids})).rowcount
            await ranking.bump(chunk.conn)
    return deleted


# ---- phase B: the embeddings -------------------------------------------------------------------------------


async def _delete_embeddings(run: PurgeRun, frozen: Sequence[int]) -> int:
    """Step 4 of phase B (module docstring): every frozen embedding deleted, a revision that took one since A2
    set back to `awaiting_embedding` first; how many were deleted. `PurgeWait(EMBEDDING_REUSED)` when
    revisions keep taking them."""
    deleted = 0
    for _ in range(EMBEDDING_ROUNDS):
        async with run.engine.connect() as conn:
            left = await _ids(conn, _EMBEDDINGS_LEFT, {"ids": list(frozen)})
            jobs = await _ids(conn, _REUSERS_JOBS, {"ids": left}) if left else []
        if not left:
            return deleted
        async with run.chunk(job_ids=jobs) as chunk:
            await chunk.conn.execute(_LOCK_EMBEDDINGS, {"ids": left})
            params = {"ids": left, "held": sorted(chunk.held)}
            reset = [int(r) for r in (await chunk.conn.execute(_RESET_REUSERS, params)).scalars()]
            deleted += len((await chunk.conn.execute(_DELETE_EMBEDDINGS, {"ids": left})).all())
        if reset:
            log.info("source_purge_embedding_reuse_reset", manifest_id=run.manifest.id, revision_ids=reset)
    async with run.engine.connect() as conn:
        if not await _ids(conn, _EMBEDDINGS_LEFT, {"ids": list(frozen)}):
            return deleted
    log.warning("source_purge_embeddings_reused", manifest_id=run.manifest.id)
    raise PurgeWait(EMBEDDING_REUSED, RETRY_AFTER)


# ---- the procedure -----------------------------------------------------------------------------------------


class SourcePurge:
    """The `source_purge` procedure (module docstring)."""

    kind: ManifestKind = KIND
    deletes_exports: bool = True
    max_ids_keys: frozenset[str] = BOUNDS_READ
    max_ids_keys_added: frozenset[str] = frozenset()  # no key a later release added (final fix C3)

    async def compute(
        self, conn: AsyncConnection, manifest: ManifestRow, *, now: datetime
    ) -> ManifestContent:
        source = _source_of(manifest)
        max_ids = await max_identity_ids(conn)
        operations, jobs = await _operations(conn, source, max_ids)
        content = new_content(manifest, max_ids=max_ids, operations=operations)
        content["affected"] = {"job_ids": jobs, "profile_version_ids": [], "tuple_ids": []}
        content["watermark"] = {"fenced_at": content["fenced_at"], "max_ids": dict(max_ids)}
        log.info("source_purge_computed", manifest_id=manifest.id, jobs=len(jobs), job_ids=jobs[:LOGGED_IDS])
        return content

    async def execute(self, run: PurgeRun, content: ManifestContent) -> None:
        source = _source_of(run.manifest)
        bounds = content["max_ids"]
        tainted = _named(content, "row_delete", "score_targets")
        counts = {
            "targets": await _delete_targets(run, tainted, bounds["score_targets"]) if tainted else 0,
            "instances": await _delete_instances(run, _named(content, "row_delete", "det_results")),
            "golden_cases": await _delete_cases(run, _named(content, "row_delete", "golden_cases")),
            "calls": await _delete_calls(run, _named(content, "row_delete", "llm_calls")),
            "feedback_redacted": await _redact_feedback(
                run, _named(content, "field_redact", "score_feedback")
            ),
            "postings": await _delete_postings(run, source, bounds["postings"]),
            "keys": await _delete_keys(run, source, datetime.fromisoformat(content["fenced_at"])),
            "fetch_requests": await _delete_requests(run, source, bounds["fetch_requests"]),
        }
        await afault_point(BEFORE_REBUILD)
        if run.mode == "replay":  # the rebuild is the replay's phase B (module docstring)
            tainted_revisions = _named(content, "row_delete", "job_revisions")
            counts["revisions"] = await _delete_tainted_revisions(run, tainted_revisions)
        else:
            affected = content.get("affected")
            counts["jobs_rebuilt"] = await rebuild_jobs(run, [] if affected is None else affected["job_ids"])
        counts["embeddings"] = await _delete_embeddings(run, _named(content, "row_delete", "embeddings"))
        log.info("source_purge_executed", manifest_id=run.manifest.id, source_id=source, **counts)

    async def postconditions(self, conn: AsyncConnection, content: ManifestContent) -> list[Violation]:
        source = _source_from_content(content)
        index = next(i for i, op in enumerate(content["operations"]) if op["op"] == "fence")
        admitted_at = datetime.fromisoformat(content["admitted_at"])
        found: list[Violation] = []
        fence_at = (await conn.execute(_FENCE_AT, {"source": source})).scalar_one()
        if fence_at is None or fence_at < admitted_at:
            found.append(Violation(index, "fence", "sources", "purge_fence_at lies before the admission"))
        active = (await conn.execute(_ACTIVE_VERSION)).one()
        if _enables(active.payload, source) and active.created_at <= admitted_at:
            detail = "the active settings version, created by the admission, enables the purged source"
            found.append(Violation(index, "fence", "settings_versions", detail))
        return found

    async def reapply_admission(
        self, conn: AsyncConnection, manifest: ManifestRow, *, now: datetime, high_water: Mapping[str, int]
    ) -> None:
        source = _source_of(manifest)
        admitted_at = manifest.created_at
        await conn.execute(_LOCK_CONFIG)  # held by the caller (Task 25's phase A): a no-op lock
        raised = (await conn.execute(_RAISE_FENCE_AT, {"at": admitted_at, "source": source})).first()
        version = await _disable_source(
            conn,
            source,
            now=now,
            defer=None,
            created_by=admitted_at,
            replay_own_above=high_water.get("settings_versions"),
        )
        cancelled = await cancel_active_runs(
            conn, reason=SOURCE_PURGE_CANCEL, queued_at_or_before=admitted_at
        )
        log.info(
            "source_purge_admission_reapplied",
            manifest_id=manifest.id,
            source_id=source,
            purge_fence_at_set=raised is not None,
            settings_version_id=version,
            runs_cancelled=[c.run_id for c in cancelled],
        )


def _source_from_content(content: ManifestContent) -> str:
    (key,) = {op["key"] for op in content["operations"] if op["op"] == "fence" and op["kind"] == "source"}
    return _checked_source(key)


register_kind(SourcePurge())
