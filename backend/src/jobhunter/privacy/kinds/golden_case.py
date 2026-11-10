"""The golden-case deletion (spec §14.2 L4979, §19.5; plan M5 Task 12, Deviations 20, 21 and 53, M4
Carry-forward 3 in part): manifest kind `golden_case_delete`, no fence.

**The request** (`request_golden_case_delete(conn, case_id, *, now, defer) -> OpenedPurge`), in the caller's
transaction: `active_config FOR UPDATE` (A0's lock, held for `purge.open_purge`), then
`fences.refuse_under_global_fence` (409 `delete_everything_in_progress` while delete-everything's fence
stands, M5 Task 8), then the case's row `FOR UPDATE` (`golden.lock_case`): a `CaseMissing` (`LookupError`)
when there is none, `golden.eval_run_running()` (409 `conflict_state`, detail `eval_run_running`) while a
running eval run includes it. Then A0 with the request `{"golden_case_id": <id>}` and no fence. The api's
route keeps its synchronous deletion until Task 18 rewires it to answer 202 (U8, Deviation 20).

**The content** (A2): `row_delete golden_cases ids=[<id>]` (bounded by its id), and the export ops, which
`purge.compute_content` adds (a golden case's job may be in an export).

**Phase B** (`execute`), one chunk per named case over its job's row (`PurgeRun.chunk(job_ids=[job])`: the
fence, `active_config FOR SHARE`, the job `FOR UPDATE`, the owner): the accounts of the case's calls' entries
still `reserved` or `dispatched` (`spend_ledger.lock_for_purge`: the month rows, step 6, and the funded bulk
rows, step 7), then the case `FOR UPDATE` (step 10). While a running eval run includes it the chunk rolls
back and the operation waits `EVAL_WAIT` (5 min) without spending an attempt (`purge.PurgeWait`, error code
`eval_run_running`: an eval run may take hours, and it has an end of its own). Otherwise those entries are
read again under the case's lock, an entry reserved since by a run that has ended meanwhile has its accounts
taken now (after the case row: the one place this chunk leaves the schedule, as `deletion_graph`'s step 7),
and they are settled (`spend_ledger.settle_for_purge`: a `reserved` one released, a `dispatched` one
`ambiguous`), since every later reader of an entry joins its call. Then the case is deleted, its
`eval_case_results` and its eval calls by cascade (`golden.delete_case`); the ledger rows stay. A case gone
already is skipped: the chunk is idempotent. The kind has no postcondition of its own and nothing to
re-apply in a replay.

Ids only reach a log line.
"""

from datetime import datetime, timedelta
from typing import Final

from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q
from jobhunter.llm import spend_ledger
from jobhunter.observability.logging import get_logger
from jobhunter.privacy.fences import refuse_under_global_fence
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
from jobhunter.worker.tasks import Deferrer

KIND: Final = "golden_case_delete"
REQUEST_KEY: Final = "golden_case_id"
# How long a purge waits for a running eval run that includes its case (module docstring).
EVAL_WAIT: Final = timedelta(minutes=5)

_LOCK_CONFIG: Final = q("SELECT 1 FROM active_config WHERE id = 1 FOR UPDATE")
# The entries of the case's calls that a reader could still settle: `reserved` or `dispatched`.
_ENTRIES_IN_FLIGHT: Final = q(
    "SELECT l.id FROM llm_calls c JOIN llm_spend_ledger l ON l.id = c.spend_ledger_id "
    "WHERE c.golden_case_id = :i AND l.state IN ('reserved', 'dispatched') ORDER BY l.id"
)

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
    if await golden.in_running_run(conn, case_id, flags):
        log.info("golden_case_delete_refused", golden_case_id=case_id, reason=golden.EVAL_RUN_RUNNING)
        raise golden.eval_run_running()
    opened = await open_purge(
        conn, kind=KIND, request={REQUEST_KEY: case_id}, fences=(), now=now, defer=defer
    )
    log.info("golden_case_delete_requested", golden_case_id=case_id, manifest_id=opened.manifest_id)
    return opened


async def _entries_in_flight(conn: AsyncConnection, case_id: int) -> list[int]:
    return [int(i) for i in (await conn.execute(_ENTRIES_IN_FLIGHT, {"i": case_id})).scalars()]


async def _delete(run: PurgeRun, case_id: int) -> None:
    """One case's chunk (module docstring)."""
    async with run.engine.connect() as conn:
        job_id = await golden.case_job(conn, case_id)
    if job_id is None:
        return  # deleted already
    async with run.chunk(job_ids=[job_id]) as chunk:
        conn = chunk.conn
        entries = await _entries_in_flight(conn, case_id)
        rows = await spend_ledger.lock_for_purge(conn, entries, now=run.now)  # steps 6 and 7
        flags = await golden.lock_case(conn, case_id)  # step 10
        if flags is None:
            return
        if await golden.in_running_run(conn, case_id, flags):
            log.info("golden_case_purge_waits", golden_case_id=case_id, manifest_id=run.manifest.id)
            raise PurgeWait(golden.EVAL_RUN_RUNNING, EVAL_WAIT)
        current = await _entries_in_flight(conn, case_id)
        if not set(current) <= set(entries):
            rows = await spend_ledger.lock_for_purge(conn, current, now=run.now)
        await spend_ledger.settle_for_purge(conn, current, rows, now=run.now)
        await golden.delete_case(conn, case_id)


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

    async def reapply_admission(self, conn: AsyncConnection, manifest: ManifestRow, *, now: datetime) -> None:
        return None


register_kind(GoldenCaseDelete())
