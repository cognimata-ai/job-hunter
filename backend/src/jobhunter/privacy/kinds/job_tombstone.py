"""Job tombstoning (spec §20.1 L6397, §20.2 L6481-6491; plan M5 Task 13, D12, M5-R3, Deviations 18 and 50, M3
plan Task 23's exemption contract): manifest kind `job_tombstone`, a `jobs` fence.

**Due** (`retention_daily`'s rule `job_tombstones`, `open_due`): a root job (unmerged, not a synthetic golden
job) whose `availability` is `closed` or `stale` and whose `availability_changed_at` lies `TOMBSTONE_AFTER`
(90 days) before the pass's `now`, unless it or a job merged into it is saved or applied
(`user_job_actions.saved_at` or `applied_at` set), an active golden case names a revision of it or of a job
merged into it (`EXEMPT_SQL`), or a purge fence stands over one of them already (a tombstone opened by an
earlier pass, another purge's: `fences.NO_OPEN_FENCE_SQL`, then `is_fenced`). At most
`TOMBSTONE_BATCHES_PER_RUN` (10) batches of at most `TOMBSTONE_BATCH` (200) roots per pass, in id order
(keyset), each in a transaction of its own: the epoch fence (worker-owned maintenance, `retention`'s
convention), then `active_config FOR UPDATE` (A0's lock), the batch read under it, and A0
(`purge.open_purge`): the request `{"job_ids": [<root>, ...]}` (ids only) and one `jobs` fence naming the
batch and every job merged into it. The selection takes no job row: a job saved or made golden after it read
the job is honoured by `compute` below (D12), and a writer that comes after the fence's commit meets the
fence.

**The content** (A2, `compute`, under the fence after the quiesce): the request's roots are read again with
their merged jobs (G, `merged_into_job_id` followed down), and a root no longer due by its own row at A0's
cutoff (`admitted_at - TOMBSTONE_AFTER`: re-opened, or closed again, by a writer that began before the fence,
`DUE_ROOT_SQL`) or whose graph is now exempt (saved, applied or an active golden case since A0) is left out,
its fence released at completion with the others (D12, Ruling T13-3). The
operations: the `fence` of the jobs the content deletes, then `row_delete jobs` of them bounded by id (the
whole graph's other rows go with them or by their foreign keys); no `affected` set and no watermark (no source
or profile purge), and no export op (`deletes_exports = False`: a tombstone keeps the exports, which expire
within 7 days, M5-R3, Deviation 18).

**Phase B** (`execute`), per root J in id order and its graph G (the content's jobs, J the one whose
`merged_into_job_id` is NULL): `deletion_graph.purge_job_graph(write_tombstone=True)` with the bound
`max_ids["score_targets"]` (Deviation 50, shared with the Adzuna purge): first G's targets (by job or by
revision), in chunks `PurgeRun.chunk(job_ids=G)` of at most `targets.TARGET_SET_CHUNK` (200) target ids each,
read `ORDER BY id LIMIT 200` under the lock and within the bound, deleted by `deletion_graph.purge_target_set`
(its refusal of more than 200 is never met), until a chunk finds fewer than 200 (the fence keeps new ones from
being created); then one graph chunk, `PurgeRun.chunk(job_ids=G, with_groups_of=G)`, which holds every
co-member of G's duplicate groups before the operation owner (a member that joined under the lock starts the
chunk again), and `deletion_graph.delete_job_graph(write_tombstone=True)`; each chunk bumps the ranking last.
Every chunk is idempotent: a re-execution after any commit, or a phase re-run after a lost deadlock (Ruling
T12-1), finds the targets left, or J gone (Deviation 50). The kind has no postcondition of its own (the typed
`row_delete jobs` and `fence` cover it) and nothing to re-apply in a replay.

Ids and counts only reach a log line.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from jobhunter.db.sql import q
from jobhunter.observability.logging import get_logger
from jobhunter.privacy import deletion_graph
from jobhunter.privacy.fences import NO_OPEN_FENCE_SQL, FenceSpec
from jobhunter.privacy.manifests import (
    ManifestContent,
    ManifestKind,
    ManifestRow,
    Operation,
    Violation,
    max_identity_ids,
)
from jobhunter.privacy.purge import OpenedPurge, PurgeRun, new_content, open_purge, register_kind
from jobhunter.types import JSONValue
from jobhunter.worker.epoch import fence_epoch
from jobhunter.worker.tasks import Deferrer

KIND: Final = "job_tombstone"
REQUEST_KEY: Final = "job_ids"
FENCE_KEY: Final = "jobs"  # the `fence` op's key of a `jobs` fence
TOMBSTONE_AFTER: Final = timedelta(days=90)
TOMBSTONE_BATCH: Final = 200
TOMBSTONE_BATCHES_PER_RUN: Final = 10
LOGGED_IDS: Final = 20

_LOCK_CONFIG: Final = q("SELECT 1 FROM active_config WHERE id = 1 FOR UPDATE")
# A job of a root's graph `g` (`g.id`) that makes the root exempt (module docstring).
EXEMPT_SQL: Final = (
    "(EXISTS (SELECT 1 FROM user_job_actions u WHERE u.job_id = g.id "
    "AND (u.saved_at IS NOT NULL OR u.applied_at IS NOT NULL)) "
    "OR EXISTS (SELECT 1 FROM job_revisions r JOIN golden_cases c ON c.job_revision_id = r.id "
    "WHERE r.job_id = g.id AND c.active))"
)
# A root job `j` that is due by its own row: unmerged, not synthetic, closed or stale since before `:cutoff`.
DUE_ROOT_SQL: Final = (
    "j.merged_into_job_id IS NULL AND NOT j.synthetic_eval AND j.availability IN ('closed', 'stale') "
    "AND j.availability_changed_at < :cutoff"
)
# The due roots after `:after`, at most `:limit`, each with no exempt or fenced job in its graph.
_DUE: Final = q(
    f"WITH RECURSIVE due AS (SELECT j.id FROM jobs j WHERE {DUE_ROOT_SQL} AND j.id > :after), "  # noqa: S608
    "g(root, id) AS (SELECT id, id FROM due "
    "UNION ALL SELECT g.root, m.id FROM jobs m JOIN g ON m.merged_into_job_id = g.id) "
    f"SELECT d.id FROM due d WHERE NOT EXISTS (SELECT 1 FROM g WHERE g.root = d.id AND ({EXEMPT_SQL} "
    f"OR NOT ({NO_OPEN_FENCE_SQL} OR NOT is_fenced(g.id)))) ORDER BY d.id LIMIT :limit"
)
# The roots of `:roots` that are still due by their own rows (`DUE_ROOT_SQL`), each with the jobs merged into
# it (itself included).
_GRAPHS: Final = q(
    "WITH RECURSIVE g(root, id) AS (SELECT j.id, j.id FROM jobs j "  # noqa: S608
    f"WHERE j.id = ANY(CAST(:roots AS bigint[])) AND {DUE_ROOT_SQL} "
    "UNION ALL SELECT g.root, m.id FROM jobs m JOIN g ON m.merged_into_job_id = g.id) "
    "SELECT root, id FROM g ORDER BY root, id"
)
# The roots of `:roots` whose graph is exempt now (`compute`, D12).
_EXEMPT_ROOTS: Final = q(
    "WITH RECURSIVE g(root, id) AS (SELECT id, id FROM jobs WHERE id = ANY(CAST(:roots AS bigint[])) "  # noqa: S608
    "UNION ALL SELECT g.root, m.id FROM jobs m JOIN g ON m.merged_into_job_id = g.id) "
    f"SELECT DISTINCT g.root FROM g WHERE {EXEMPT_SQL} ORDER BY g.root"
)
_JOBS_OF_CONTENT: Final = q(
    "SELECT id, merged_into_job_id FROM jobs WHERE id = ANY(CAST(:ids AS bigint[])) ORDER BY id"
)

log = get_logger("jobhunter.privacy.kinds.job_tombstone")


def _ids(values: object) -> list[int]:
    if not isinstance(values, list) or not all(type(v) is int for v in values):
        raise ValueError(f"a {KIND} request names its root jobs: a list of ids")
    return sorted({int(v) for v in values})


async def _graphs(conn: AsyncConnection, roots: Sequence[int], *, cutoff: datetime) -> dict[int, list[int]]:
    """Each root of `roots` still due by its own row at `cutoff` (`DUE_ROOT_SQL`) with its graph (itself and
    every job merged into it), in id order."""
    graphs: dict[int, list[int]] = {}
    for row in await conn.execute(_GRAPHS, {"roots": list(roots), "cutoff": cutoff}):
        graphs.setdefault(int(row.root), []).append(int(row.id))
    return graphs


@dataclass(frozen=True)
class OpenedBatch:
    """A batch's A0: its roots, every job its fence names, and the purge opened for it."""

    roots: tuple[int, ...]
    fenced: tuple[int, ...]
    purge: OpenedPurge


async def open_batch(
    conn: AsyncConnection, *, cutoff: datetime, after: int, now: datetime, defer: Deferrer | None
) -> OpenedBatch | None:
    """One batch's A0 in the caller's transaction (module docstring): `active_config FOR UPDATE`, the due
    roots after `after` (at most `TOMBSTONE_BATCH`), and the purge of them with its `jobs` fence; `None`
    when none is due."""
    await conn.execute(_LOCK_CONFIG)
    params = {"cutoff": cutoff, "after": after, "limit": TOMBSTONE_BATCH}
    roots = [int(job) for job in (await conn.execute(_DUE, params)).scalars()]
    if not roots:
        return None
    graphs = await _graphs(conn, roots, cutoff=cutoff)
    fenced = tuple(sorted({job for graph in graphs.values() for job in graph}))
    request: list[JSONValue] = [*roots]
    opened = await open_purge(
        conn,
        kind=KIND,
        request={REQUEST_KEY: request},
        fences=[FenceSpec(kind="jobs", job_ids=fenced)],
        now=now,
        defer=defer,
    )
    log.info(
        "job_tombstones_opened",
        manifest_id=opened.manifest_id,
        roots=len(roots),
        jobs=len(fenced),
        job_ids=roots[:LOGGED_IDS],
    )
    return OpenedBatch(tuple(roots), fenced, opened)


async def open_due(engine: AsyncEngine, *, now: datetime, epoch: int, defer: Deferrer | None) -> int:
    """The retention rule `job_tombstones` (module docstring): up to `TOMBSTONE_BATCHES_PER_RUN` batches,
    each opened in a transaction of its own fenced by worker `epoch`; how many roots were fenced."""
    cutoff = now - TOMBSTONE_AFTER
    opened, after = 0, 0
    for _ in range(TOMBSTONE_BATCHES_PER_RUN):
        async with engine.begin() as conn:
            await fence_epoch(conn, epoch)
            batch = await open_batch(conn, cutoff=cutoff, after=after, now=now, defer=defer)
        if batch is None:
            break
        opened += len(batch.roots)
        if len(batch.roots) < TOMBSTONE_BATCH:
            break
        after = batch.roots[-1]
    return opened


def _jobs_of(content: ManifestContent) -> list[int]:
    return sorted(
        {
            job
            for op in content["operations"]
            if op["op"] == "row_delete" and op["table"] == "jobs"
            for job in op.get("ids", [])
        }
    )


def _forest(rows: Sequence[tuple[int, int | None]]) -> dict[int, list[int]]:
    """Each root of the content's jobs that still exist (a job whose `merged_into_job_id` names none of them)
    with its graph."""
    into = dict(rows)
    root_of: dict[int, int] = {}

    def root(job: int) -> int:
        seen = [job]
        while (parent := into.get(seen[-1])) is not None and parent in into and parent not in seen:
            seen.append(parent)
        return seen[-1]

    for job in into:
        root_of[job] = root(job)
    graphs: dict[int, list[int]] = {}
    for job in sorted(into):
        graphs.setdefault(root_of[job], []).append(job)
    return dict(sorted(graphs.items()))


class JobTombstone:
    """The `job_tombstone` procedure (module docstring)."""

    kind: ManifestKind = KIND
    deletes_exports: bool = False

    async def compute(
        self, conn: AsyncConnection, manifest: ManifestRow, *, now: datetime
    ) -> ManifestContent:
        roots = _ids(dict(manifest.request).get(REQUEST_KEY))
        # A0's own cutoff: the request's roots were due at `admitted_at` (`created_at`)
        graphs = await _graphs(conn, roots, cutoff=manifest.created_at - TOMBSTONE_AFTER)
        exempt = {int(r) for r in (await conn.execute(_EXEMPT_ROOTS, {"roots": sorted(graphs)})).scalars()}
        jobs = sorted({job for root, graph in graphs.items() if root not in exempt for job in graph})
        operations: list[Operation] = [{"op": "fence", "kind": "jobs", "key": FENCE_KEY, "job_ids": jobs}]
        if jobs:
            operations.append({"op": "row_delete", "table": "jobs", "ids": jobs, "bound": {"by": "id"}})
        # re-opened, saved, applied or made golden since A0 (D12): left out, its fence ends at completion
        if left_out := sorted(set(roots) - set(graphs) | exempt):
            log.info("job_tombstones_left_out", manifest_id=manifest.id, job_ids=left_out[:LOGGED_IDS])
        return new_content(manifest, max_ids=await max_identity_ids(conn), operations=operations)

    async def execute(self, run: PurgeRun, content: ManifestContent) -> None:
        ids = _jobs_of(content)
        if not ids:
            return
        async with run.engine.connect() as conn:
            rows = [
                (int(r.id), None if r.merged_into_job_id is None else int(r.merged_into_job_id))
                for r in await conn.execute(_JOBS_OF_CONTENT, {"ids": ids})
            ]
        bound = content["max_ids"]["score_targets"]
        for root, graph in _forest(rows).items():
            purged = await deletion_graph.purge_job_graph(
                run, root, graph, write_tombstone=True, target_bound=bound
            )
            log.info(
                "job_tombstoned",
                manifest_id=run.manifest.id,
                job_id=root,
                tombstone_id=purged.graph.tombstone_id,
                jobs=len(purged.graph.job_ids),
                targets=purged.targets,
            )

    async def postconditions(self, conn: AsyncConnection, content: ManifestContent) -> list[Violation]:
        return []

    async def reapply_admission(
        self, conn: AsyncConnection, manifest: ManifestRow, *, now: datetime, high_water: Mapping[str, int]
    ) -> None:
        return None


register_kind(JobTombstone())
