"""The golden-case deletion (spec §14.2 L4979, §19.5; plan M5 Task 12, Deviations 20, 21 and 53, M4
Carry-forward 3 in part): manifest kind `golden_case_delete`, no fence.

**The request** (`request_golden_case_delete(conn, case_id, *, now, defer) -> OpenedPurge`), in the caller's
transaction: `active_config FOR UPDATE` (A0's lock, held for `purge.open_purge`), then
`fences.refuse_under_global_fence` (409 `delete_everything_in_progress` while delete-everything's fence
stands, M5 Task 8), then the case's row `FOR UPDATE` (`golden.lock_case`): a `CaseMissing` (`LookupError`)
when there is none, `fences.purge_in_progress()` (409 `conflict_state`, detail `purge_in_progress`) while the
case's deletion is pending (`golden.pending_deletion`: an `executing` manifest of this kind names it, so a
second request opens no second manifest, as a second source purge or delete-everything is refused; plan M5
Task 33, Ruling T33-15), `golden.eval_run_running()` (409 `conflict_state`, detail `eval_run_running`) while a
running eval run includes it. Then A0 with the request `{"golden_case_id": <id>}` and no fence. The api's
route keeps its synchronous deletion until Task 18 rewires it to answer 202 (U8, Deviation 20).

**The content** (A2): `row_delete golden_cases ids=[<id>]` (bounded by its id), and the export ops, which
`purge.compute_content` adds (a golden case's job may be in an export).

**Phase B** (`execute`), one chunk per named case over its job's row (`PurgeRun.chunk(job_ids=[job])`: the
fence, `active_config FOR SHARE`, the job `FOR UPDATE`, the owner), then
`deletion_graph.settle_and_delete_cases` (the sequence the job-graph deletion shares, M5 Task 13): the
accounts of the case's calls' entries still `reserved` or `dispatched` (`spend_ledger.lock_for_purge`: the
month rows, step 6, and the funded bulk rows, step 7), then the case `FOR UPDATE` (step 10). While a running
eval run includes it (the helper's check) the chunk rolls back and the operation waits `EVAL_WAIT` (5 min)
without spending an attempt (`purge.PurgeWait`, error code `eval_run_running`: an eval run may take hours, and
it has an end of its own; a replay never waits, below). Otherwise those entries are read again under the
case's lock, an entry reserved since by a run that has ended meanwhile has its accounts taken now (after the
case row: the one place this chunk leaves the schedule, as `deletion_graph`'s step 7), and they are settled
(`spend_ledger.settle_for_purge`: a `reserved` one released, a `dispatched` one `ambiguous`), since every
later reader of an entry joins its call. Then the case is deleted, its `eval_case_results` and its eval calls
by cascade (`golden.delete_case`); the ledger rows stay. A case gone already is skipped: the chunk is
idempotent. The kind has no postcondition of its own and nothing to re-apply in a replay.

**In a replay** (`migrate` step 7, M5 Task 25, Ruling T25-2 (8)) a running eval run that includes the case is
one the restored dump holds `running`, and nothing can end it: `api` and `worker` are stopped. The chunk ends
each such run as the `eval_run` operation's failure hook ends an interrupted one
(`worker.operations.fail_eval_run`: `running → failed`, `metrics.failed = "interrupted"`, found by
`golden.running_runs_including`), logged `golden_case_replay_eval_runs_ended` with their ids, then deletes the
case as the live purge does once the run has ended.

Ids only reach a log line.
"""

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q
from jobhunter.observability.logging import get_logger
from jobhunter.privacy.deletion_graph import settle_and_delete_cases
from jobhunter.privacy.fences import purge_in_progress, refuse_under_global_fence
from jobhunter.privacy.manifests import (
    ManifestContent,
    ManifestKind,
    ManifestRow,
    Operation,
    Violation,
    max_identity_ids,
)
from jobhunter.privacy.purge import (
    OpenedPurge,
    PurgeRun,
    PurgeWait,
    new_content,
    open_purge,
    register_kind,
)
from jobhunter.scoring import golden
from jobhunter.types import get_int
from jobhunter.worker.operations import EVAL_RUN_INTERRUPTED, fail_eval_run
from jobhunter.worker.tasks import Deferrer

KIND: Final = "golden_case_delete"
REQUEST_KEY: Final = golden.DELETION_REQUEST_KEY  # `golden_case_id`
# How long a purge waits for a running eval run that includes its case (module docstring).
EVAL_WAIT: Final = timedelta(minutes=5)
LOGGED_IDS: Final = 20

_LOCK_CONFIG: Final = q("SELECT 1 FROM active_config WHERE id = 1 FOR UPDATE")

log = get_logger("jobhunter.privacy.kinds.golden_case")


async def request_golden_case_delete(
    conn: AsyncConnection, case_id: int, *, now: datetime, defer: Deferrer | None
) -> OpenedPurge:
    """Open the purge of golden case `case_id` in the caller's transaction (module docstring)."""
    await conn.execute(_LOCK_CONFIG)
    await refuse_under_global_fence(conn)
    flags = await golden.lock_case(conn, case_id)
    if flags is None:
        raise golden.CaseMissing(case_id)
    if (pending := await golden.pending_deletion(conn, case_id)) is not None:
        log.info(
            "golden_case_delete_refused",
            golden_case_id=case_id,
            reason="purge_in_progress",
            manifest_id=pending,
        )
        raise purge_in_progress()
    if await golden.in_running_run(conn, case_id, flags):
        log.info("golden_case_delete_refused", golden_case_id=case_id, reason=golden.EVAL_RUN_RUNNING)
        raise golden.eval_run_running()
    opened = await open_purge(
        conn, kind=KIND, request={REQUEST_KEY: case_id}, fences=(), now=now, defer=defer
    )
    log.info("golden_case_delete_requested", golden_case_id=case_id, manifest_id=opened.manifest_id)
    return opened


async def _delete(run: PurgeRun, case_id: int) -> None:
    """One case's chunk (module docstring)."""
    async with run.engine.connect() as conn:
        job_id = await golden.case_job(conn, case_id)
    if job_id is None:
        return  # deleted already

    async def unless_running(conn: AsyncConnection, cases: Mapping[int, golden.CaseFlags]) -> None:
        flags = cases.get(case_id)
        if flags is None or not await golden.in_running_run(conn, case_id, flags):
            return
        if run.mode == "replay":  # a run the dump holds: ended as an interrupted one (module docstring)
            ended = [
                eval_run_id
                for eval_run_id in await golden.running_runs_including(conn, case_id, flags)
                if await fail_eval_run(conn, eval_run_id, EVAL_RUN_INTERRUPTED)
            ]
            log.warning(
                "golden_case_replay_eval_runs_ended",
                golden_case_id=case_id,
                manifest_id=run.manifest.id,
                eval_run_ids=ended[:LOGGED_IDS],
            )
            return
        log.info("golden_case_purge_waits", golden_case_id=case_id, manifest_id=run.manifest.id)
        raise PurgeWait(golden.EVAL_RUN_RUNNING, EVAL_WAIT)

    async with run.chunk(job_ids=[job_id]) as chunk:
        await settle_and_delete_cases(chunk.conn, [case_id], now=run.now, check=unless_running)


class GoldenCaseDelete:
    """The `golden_case_delete` procedure (module docstring)."""

    kind: ManifestKind = KIND
    deletes_exports: bool = True

    async def compute(
        self, conn: AsyncConnection, manifest: ManifestRow, *, now: datetime
    ) -> ManifestContent:
        case_id = get_int(dict(manifest.request), REQUEST_KEY)
        operations: list[Operation] = [
            {"op": "row_delete", "table": "golden_cases", "ids": [case_id], "bound": {"by": "id"}}
        ]
        return new_content(manifest, max_ids=await max_identity_ids(conn), operations=operations)

    async def execute(self, run: PurgeRun, content: ManifestContent) -> None:
        for op in content["operations"]:
            if op["op"] == "row_delete" and op["table"] == "golden_cases":
                for case_id in op.get("ids", []):
                    await _delete(run, case_id)

    async def postconditions(self, conn: AsyncConnection, content: ManifestContent) -> list[Violation]:
        return []

    async def reapply_admission(
        self, conn: AsyncConnection, manifest: ManifestRow, *, now: datetime, high_water: Mapping[str, int]
    ) -> None:
        return None


register_kind(GoldenCaseDelete())
