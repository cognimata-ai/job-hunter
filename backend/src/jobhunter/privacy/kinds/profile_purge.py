"""The profile version purge (spec §20.1, §20.2's profile purge steps 1-7, §3.3.3; plan M5 Task 14, D11,
Deviations 18, 25, 53 and 54): manifest kind `profile_purge`, a `profile` fence.

**Eligible** (`retention_daily`'s rule `profile_purges`, `open_due`; D11): a version that is not synthetic
(`profile_versions.synthetic_eval`, unconditionally, whatever its cases: the adversarial seed reuses a live
synthetic version and makes its cases active again; Deviation 25), not purged, neither the active version nor
the newest confirmed one nor the previous one (`privacy.retained.NEWEST_AND_PREVIOUS`: the newest confirmed
draft before the newest one that names another version, in activation order; §20.1 keeps both versions' files,
as `retention`'s `cv_files` rule does), with no target of its tuples non-terminal or ended within
`retention.score_history_months` calendar months (`retained.ENDED_TARGET_STATES`, `retained.months_before`,
which retention's `score_history` rule shares), no active golden case and no `score_feedback` row naming it,
and under no unreleased `profile` or `global` fence (an earlier pass's purge of it still running;
`fences.version_fenced_sql`). One purge per eligible version, in id order, each admitted in a transaction of
its own that starts with the epoch fence (retention's convention).

**A0** (`open_next`): `active_config FOR UPDATE` (`settings.active.lock_active`, with the active settings read
under it), the next eligible version, read under that lock; every live `propose_queries` operation of a
proposal of the version cancelled (`worker.operations.cancel_operation`, reason `profile_purged`: the owner
rows before the domain rows, §9.1); the version's row and its tuples' rows `FOR UPDATE`, in id order; then
`purge.open_purge` with the request `{"profile_version_id": <id>}` and one `profile` fence. Every writer that
can make a version ineligible holds `active_config` (the confirm and activation `FOR UPDATE`; the score lane,
the user's target moves, `golden.record_feedback` and `golden.set_flags` `FOR SHARE`; the adversarial seed
`FOR UPDATE`), so eligibility cannot change between A0's read and A2; deleting a case or a feedback only makes
a version more eligible.

**The call set is closed at A0** (Deviation 54). Every reservation and dispatch marker of a proposal call
takes its operation's row `FOR SHARE` and requires it `running` under its claim, so none follows A0's commit,
and one that committed before is visible to A2 (A0 waits for its locks). Every proposal operation is created
under `active_config` for the active version or the one being activated (the caller contract of
`queries.proposals.request_proposal`), which a fenced version can be neither (activation answers 409
`profile_purge_in_progress`), and the version's drafts are all `confirmed`, so no extraction of them starts. A
call already sent settles in the gateway's own transaction later; `spend_ledger.settle` writes its output only
while the call's `content_purged_at` is NULL, so its content is never stored.

**The content** (A2, `compute`, under the fence after the quiesce): `affected = {job_ids: [],
profile_version_ids: [v], tuple_ids: [...]}`, the watermark, and §20.2's steps 2-6 as typed operations:
1. the `fence` (kind `profile`, key the version's id);
2. `row_delete score_targets` of the tuples (a parent bound on `llm_tuples`: no target of them is left; the
   rows each target owns go with it);
3. `row_delete profile_vectors` of the version (a parent bound); `field_redact query_proposals items` (marker
   `status = 'purged'`) of its proposals; `field_redact llm_calls output_json` (marker `content_purged_at`)
   over every call of the version, published or not: a `propose_queries` call whose `input_refs ->>
   'profile_version_id'` is the version (every attempt and logical call of every proposal) or that one of its
   proposals names, and an `extract_profile` call of one of its drafts (`input_refs ->> 'profile_draft_id'`,
   or the call's `profile_draft_id`);
4. `row_delete golden_cases` by id: the version's cases at A2, all inactive by eligibility, frozen;
5. `field_redact profile_versions profile_json` (marker `purged_at`);
6. when no other unpurged version uses its CV file, `field_redact cv_files raw_text_enc, stripped_text`
   (marker `purged_at`; the CHECK makes `storage_path` go with them) and, while the row names its file,
   `file_remove cv_store <storage_path>`; then `field_redact profile_drafts draft_json, reviewed_text`
   (marker `content_purged_at`) of its drafts: its own and every draft a confirm of identical content pointed
   at it;
and the export ops, which `purge.compute_content` appends (`deletes_exports`; Deviation 18). An operation with
no row to name is left out; every id is at or below `max_ids`.

**Phase B** (`execute`), in `PurgeRun.chunk` transactions only, each statement over rows the content names or
selects within its bound: (2) the tuples' targets within `max_ids["score_targets"]`, `TARGET_SET_CHUNK` at a
time: a page read, the fault point `privacy.profile_purge.before_target_chunk`, its jobs locked `FOR UPDATE`
(`chunk(job_ids=...)`), the page read again under the lock, `deletion_graph.purge_target_set` over what is
left of it, the ranking bump last, until a page is short. Retention's `score_history` rule needs no fence (the
tuples are inactive), so targets of a page may be gone by its chunk: they never come back, and the next page
is read; (3) one chunk: the calls (ledger rows, step 9, in id order) `output_json := NULL` with
`content_purged_at`, each proposal `running | ready | failed → purged` with `items := NULL`
(`transition_where`), the vectors deleted, then the fault point `privacy.profile_purge.after_redaction` before
its commit; (4) one chunk over the cases' jobs: `deletion_graph.settle_and_delete_cases` (their in-flight eval
entries settled before their calls cascade, G04), an active case an `InvariantViolation`; (5, 6) one chunk:
the version's `profile_json := NULL` with `purged_at`, its drafts' content NULL with `content_purged_at`, the
CV row's file and texts NULL with `purged_at`; after its commit, the CV file deleted (`CvStore.delete`; one
gone already is fine). A redaction keeps a time an earlier one set. The ids and states of the proposals,
drafts and calls stay, and the version's row and tuples, so runs, cycles, submissions, bulk operations and the
ledgers keep valid references. Every chunk is idempotent: a re-execution after any commit, or a phase re-run
after a lost deadlock (Ruling T12-1), finds what is left. The kind has no postcondition of its own and nothing
to re-apply in a replay: a restored live operation of a purged proposal finds it no longer `running` and ends
obsolete.

Ids and counts only reach a log line.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.sql.elements import TextClause

from jobhunter.db import ranking
from jobhunter.db.sql import q
from jobhunter.db.transitions import transition_where
from jobhunter.errors import InvariantViolation
from jobhunter.faults import afault_point
from jobhunter.observability.logging import get_logger
from jobhunter.privacy import deletion_graph
from jobhunter.privacy.fences import FenceSpec, version_fenced_sql
from jobhunter.privacy.manifests import (
    ManifestContent,
    ManifestKind,
    ManifestRow,
    Operation,
    Violation,
    max_identity_ids,
)
from jobhunter.privacy.purge import OpenedPurge, PurgeRun, new_content, open_purge, register_kind
from jobhunter.privacy.retained import ENDED_TARGET_STATES, NEWEST_AND_PREVIOUS, months_before
from jobhunter.queries import proposals
from jobhunter.scoring import golden
from jobhunter.scoring.targets import TARGET_SET_CHUNK
from jobhunter.settings.active import lock_active
from jobhunter.settings.model import Settings
from jobhunter.types import get_int
from jobhunter.worker.epoch import fence_epoch
from jobhunter.worker.operations import cancel_operation
from jobhunter.worker.tasks import Deferrer

KIND: Final = "profile_purge"
REQUEST_KEY: Final = "profile_version_id"
CANCELLED: Final = "profile_purged"  # the reason of a proposal operation A0 cancels
AFTER_REDACTION: Final = "privacy.profile_purge.after_redaction"  # the fault points (module docstring)
BEFORE_TARGET_CHUNK: Final = "privacy.profile_purge.before_target_chunk"
LOGGED_IDS: Final = 20
# The next eligible version after `:after` (module docstring), read under A0's `active_config` lock.
_ELIGIBLE: Final = q(
    f"WITH {NEWEST_AND_PREVIOUS} SELECT v.id FROM profile_versions v "  # noqa: S608 (the module's constants)
    "WHERE v.id > :after AND v.purged_at IS NULL AND NOT v.synthetic_eval "
    "AND v.id IS DISTINCT FROM CAST(:active AS bigint) "
    "AND NOT EXISTS (SELECT 1 FROM newest n WHERE n.version = v.id) "
    "AND NOT EXISTS (SELECT 1 FROM previous p WHERE p.version = v.id) "
    "AND NOT EXISTS (SELECT 1 FROM llm_tuples u JOIN score_targets t ON t.llm_tuple_id = u.id "
    "WHERE u.profile_version_id = v.id AND (t.state <> ALL(CAST(:ended AS text[])) "
    "OR t.state_changed_at >= :cutoff)) "
    "AND NOT EXISTS (SELECT 1 FROM golden_cases g WHERE g.profile_version_id = v.id AND g.active) "
    "AND NOT EXISTS (SELECT 1 FROM score_feedback f WHERE f.profile_version_id = v.id) "
    f"AND NOT {version_fenced_sql('v.id')} ORDER BY v.id LIMIT 1"
)
# The live operations of the version's proposals (`proposals.dedup_key`), owner rows in id order.
_LIVE_PROPOSAL_OPERATIONS: Final = q(
    "SELECT o.id FROM operations o JOIN query_proposals p "  # noqa: S608 (the module's constants)
    f"ON o.kind = '{proposals.KIND}' AND o.dedup_key = '{proposals.KIND}:' || p.id "
    "WHERE p.profile_version_id = :v AND o.status IN ('pending', 'running') ORDER BY o.id"
)
_LOCK_VERSION: Final = q("SELECT id FROM profile_versions WHERE id = :v FOR UPDATE")
_LOCK_TUPLES: Final = q("SELECT id FROM llm_tuples WHERE profile_version_id = :v ORDER BY id FOR UPDATE")

# A2's reads (plain: the fence and the eligibility keep them closed), each within its table's `max_ids`.
_VERSION_ROW: Final = q("SELECT draft_id, cv_file_id FROM profile_versions WHERE id = :v")
_TUPLES: Final = q("SELECT id FROM llm_tuples WHERE profile_version_id = :v AND id <= :bound ORDER BY id")
_PROPOSALS: Final = q(
    "SELECT id FROM query_proposals WHERE profile_version_id = :v AND id <= :bound ORDER BY id"
)
_DRAFTS: Final = q(
    "SELECT id FROM profile_drafts WHERE (confirmed_profile_version_id = :v OR id = :draft) AND id <= :bound "
    "ORDER BY id"
)
_CALLS: Final = q(
    "SELECT c.id FROM llm_calls c WHERE c.id <= :bound AND ((c.purpose = 'propose_queries' "
    "AND (c.input_refs ->> 'profile_version_id' = :version_key OR c.id IN "
    "(SELECT p.llm_call_id FROM query_proposals p WHERE p.profile_version_id = :v))) "
    "OR (c.purpose = 'extract_profile' AND (c.profile_draft_id = ANY(CAST(:drafts AS bigint[])) "
    "OR c.input_refs ->> 'profile_draft_id' = ANY(CAST(:draft_keys AS text[]))))) ORDER BY c.id"
)
_CASES: Final = q("SELECT id FROM golden_cases WHERE profile_version_id = :v AND id <= :bound ORDER BY id")
# The version's CV file unless another unpurged version uses it.
_OWN_CV_FILE: Final = q(
    "SELECT c.id, c.storage_path FROM cv_files c WHERE c.id = :cv AND NOT EXISTS (SELECT 1 FROM "
    "profile_versions o WHERE o.cv_file_id = c.id AND o.id <> :v AND o.purged_at IS NULL)"
)

# Phase B.
_IDS: Final = "CAST(:ids AS bigint[])"
_TARGET_PAGE: Final = q(
    "SELECT id, job_id FROM score_targets WHERE llm_tuple_id = ANY(CAST(:tuples AS bigint[])) "
    "AND id <= :bound ORDER BY id LIMIT :limit"
)
# The page's targets that are still there, read again under the chunk's job locks: another deleter
# (retention's `score_history`, which needs no fence) may have deleted some since the page was read, for good.
_PAGE_AGAIN: Final = q(f"SELECT id, job_id FROM score_targets WHERE id = ANY({_IDS}) ORDER BY id")  # noqa: S608
_REDACT_CALLS: Final = q(
    "UPDATE llm_calls SET output_json = NULL, content_purged_at = coalesce(content_purged_at, :now) "  # noqa: S608
    f"WHERE id IN (SELECT id FROM llm_calls WHERE id = ANY({_IDS}) ORDER BY id FOR NO KEY UPDATE)"
)
_DELETE_VECTORS: Final = q(f"DELETE FROM profile_vectors WHERE profile_version_id = ANY({_IDS})")  # noqa: S608
_CASE_JOBS: Final = q(
    "SELECT g.id, r.job_id FROM golden_cases g JOIN job_revisions r ON r.id = g.job_revision_id "  # noqa: S608
    f"WHERE g.id = ANY({_IDS}) ORDER BY g.id"
)
_REDACT_VERSIONS: Final = q(
    "UPDATE profile_versions SET profile_json = NULL, purged_at = coalesce(purged_at, :now) "  # noqa: S608
    f"WHERE id = ANY({_IDS})"
)
_REDACT_DRAFTS: Final = q(
    "UPDATE profile_drafts SET draft_json = NULL, reviewed_text = NULL, "  # noqa: S608
    f"content_purged_at = coalesce(content_purged_at, :now) WHERE id = ANY({_IDS})"
)
_REDACT_CV_FILES: Final = q(
    "UPDATE cv_files SET storage_path = NULL, raw_text_enc = NULL, stripped_text = NULL, "  # noqa: S608
    f"purged_at = coalesce(purged_at, :now) WHERE id = ANY({_IDS})"
)
_PURGEABLE_PROPOSALS: Final = ("running", "ready", "failed")

log = get_logger("jobhunter.privacy.kinds.profile_purge")


# ---- A0 ----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class OpenedVersion:
    """A version's A0: the version, the proposal operations it cancelled and the purge opened for it."""

    version_id: int
    cancelled: tuple[int, ...]
    purge: OpenedPurge


async def _ids(conn: AsyncConnection, statement: TextClause, params: Mapping[str, object]) -> list[int]:
    return [int(i) for i in (await conn.execute(statement, dict(params))).scalars()]


async def open_next(
    conn: AsyncConnection, *, after: int, now: datetime, defer: Deferrer | None
) -> OpenedVersion | None:
    """The next eligible version's A0 in the caller's transaction (module docstring): `active_config FOR
    UPDATE`, the eligible version after `after` read under it, its live proposal operations cancelled, its
    row and tuples locked, and its purge opened with a `profile` fence; `None` when none is eligible."""
    active = await lock_active(conn)  # step 2, a statement of its own (`settings.active`)
    months = Settings.model_validate(active.payload).retention.score_history_months
    params = {
        "after": after,
        "active": active.profile_version_id,
        "ended": list(ENDED_TARGET_STATES),
        "cutoff": months_before(now, months),
    }
    version = (await conn.execute(_ELIGIBLE, params)).scalar_one_or_none()
    if version is None:
        return None
    version = int(version)
    cancelled: list[int] = []
    for operation in await _ids(conn, _LIVE_PROPOSAL_OPERATIONS, {"v": version}):  # the owner rows first
        if await cancel_operation(conn, operation, reason=CANCELLED):
            cancelled.append(operation)
    await conn.execute(_LOCK_VERSION, {"v": version})
    tuples = await _ids(conn, _LOCK_TUPLES, {"v": version})
    opened = await open_purge(
        conn,
        kind=KIND,
        request={REQUEST_KEY: version},
        fences=[FenceSpec(kind="profile", profile_version_id=version)],
        now=now,
        defer=defer,
    )
    log.info(
        "profile_purge_opened",
        manifest_id=opened.manifest_id,
        version_id=version,
        tuple_ids=tuples[:LOGGED_IDS],
        operations_cancelled=cancelled,
    )
    return OpenedVersion(version, tuple(cancelled), opened)


async def open_due(engine: AsyncEngine, *, now: datetime, epoch: int, defer: Deferrer | None) -> int:
    """The retention rule `profile_purges` (module docstring): one A0 per eligible version, in id order, each
    in a transaction of its own fenced by worker `epoch`; how many purges it opened."""
    opened, after = 0, 0
    while True:
        async with engine.begin() as conn:
            await fence_epoch(conn, epoch)
            made = await open_next(conn, after=after, now=now, defer=defer)
        if made is None:
            return opened
        opened += 1
        after = made.version_id


# ---- the content -------------------------------------------------------------------------------------------


async def _operations(
    conn: AsyncConnection, version: int, tuples: Sequence[int], max_ids: Mapping[str, int]
) -> list[Operation]:
    """§20.2's steps 2-6 of `version`, whose tuples are `tuples`, as typed operations (module docstring)."""
    row = (await conn.execute(_VERSION_ROW, {"v": version})).one()
    proposal_ids = await _ids(conn, _PROPOSALS, {"v": version, "bound": max_ids["query_proposals"]})
    drafts = await _ids(
        conn, _DRAFTS, {"v": version, "draft": int(row.draft_id), "bound": max_ids["profile_drafts"]}
    )
    calls = await _ids(
        conn,
        _CALLS,
        {
            "bound": max_ids["llm_calls"],
            "v": version,
            "version_key": str(version),
            "drafts": drafts,
            "draft_keys": [str(draft) for draft in drafts],
        },
    )
    cases = await _ids(conn, _CASES, {"v": version, "bound": max_ids["golden_cases"]})
    cv = (
        None
        if row.cv_file_id is None
        else (await conn.execute(_OWN_CV_FILE, {"cv": int(row.cv_file_id), "v": version})).first()
    )
    operations: list[Operation] = [{"op": "fence", "kind": "profile", "key": str(version), "job_ids": []}]
    if tuples:
        operations.append(
            {
                "op": "row_delete",
                "table": "score_targets",
                "ids": list(tuples),
                "bound": {"by": "parent", "table": "llm_tuples", "column": "llm_tuple_id"},
            }
        )
    operations.append(
        {
            "op": "row_delete",
            "table": "profile_vectors",
            "ids": [version],
            "bound": {"by": "parent", "table": "profile_versions", "column": "profile_version_id"},
        }
    )
    if proposal_ids:
        operations.append(
            {
                "op": "field_redact",
                "table": "query_proposals",
                "ids": proposal_ids,
                "columns": ["items"],
                "marker": {"status": "purged"},
            }
        )
    if calls:
        operations.append(
            {
                "op": "field_redact",
                "table": "llm_calls",
                "ids": calls,
                "columns": ["output_json"],
                "marker": {"content_purged_at": "set"},
            }
        )
    if cases:
        operations.append({"op": "row_delete", "table": "golden_cases", "ids": cases, "bound": {"by": "id"}})
    operations.append(
        {
            "op": "field_redact",
            "table": "profile_versions",
            "ids": [version],
            "columns": ["profile_json"],
            "marker": {"purged_at": "set"},
        }
    )
    if cv is not None:
        operations.append(
            {
                "op": "field_redact",
                "table": "cv_files",
                "ids": [int(cv.id)],
                "columns": ["raw_text_enc", "stripped_text"],
                "marker": {"purged_at": "set"},
            }
        )
        if cv.storage_path is not None:
            operations.append({"op": "file_remove", "volume": "cv_store", "keys": [str(cv.storage_path)]})
    if drafts:
        operations.append(
            {
                "op": "field_redact",
                "table": "profile_drafts",
                "ids": drafts,
                "columns": ["draft_json", "reviewed_text"],
                "marker": {"content_purged_at": "set"},
            }
        )
    return operations


def _named(content: ManifestContent, op: Literal["row_delete", "field_redact"], table: str) -> list[int]:
    """The ids the content's `op` on `table` names (a parent-bound row delete's are its parents')."""
    named: set[int] = set()
    for each in content["operations"]:
        if each["op"] == "row_delete" and op == "row_delete" and each["table"] == table:
            named.update(each.get("ids", []))
        elif each["op"] == "field_redact" and op == "field_redact" and each["table"] == table:
            named.update(each["ids"])
    return sorted(named)


def _files(content: ManifestContent) -> list[str]:
    return [
        key
        for op in content["operations"]
        if op["op"] == "file_remove" and op["volume"] == "cv_store"
        for key in op["keys"]
    ]


# ---- phase B -----------------------------------------------------------------------------------------------


async def _delete_targets(run: PurgeRun, tuples: Sequence[int], bound: int) -> int:
    """Step 2 (module docstring): the tuples' targets, `TARGET_SET_CHUNK` per chunk; how many."""
    deleted = 0
    while True:
        params = {"tuples": list(tuples), "bound": bound, "limit": TARGET_SET_CHUNK}
        async with run.engine.connect() as conn:
            page = (await conn.execute(_TARGET_PAGE, params)).all()
        if not page:
            return deleted
        await afault_point(BEFORE_TARGET_CHUNK)
        async with run.chunk(job_ids=sorted({int(r.job_id) for r in page})) as chunk:
            left = (await chunk.conn.execute(_PAGE_AGAIN, {"ids": [int(r.id) for r in page]})).all()
            # a target's job never changes, so each one left is held; one that went meanwhile stays gone
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
        if len(page) < TARGET_SET_CHUNK:  # else the next page: every target of this one is gone now
            return deleted


async def _redact_content(run: PurgeRun, content: ManifestContent) -> None:
    """Step 3 (module docstring): the calls, then the proposals, then the vectors, in one chunk."""
    calls = _named(content, "field_redact", "llm_calls")
    proposal_ids = _named(content, "field_redact", "query_proposals")
    vectors = _named(content, "row_delete", "profile_vectors")
    async with run.chunk() as chunk:
        if calls:  # ledger rows (step 9), in id order
            await chunk.conn.execute(_REDACT_CALLS, {"ids": calls, "now": run.now})
        if proposal_ids:
            await transition_where(
                chunk.conn,
                "query_proposals",
                list(_PURGEABLE_PROPOSALS),
                "purged",
                where="t.id = ANY(CAST(:ids AS bigint[]))",
                params={"ids": proposal_ids},
                set_={"items": None},
            )
        if vectors:
            await chunk.conn.execute(_DELETE_VECTORS, {"ids": vectors})
        await afault_point(AFTER_REDACTION)


async def _refuse_active(conn: AsyncConnection, cases: Mapping[int, golden.CaseFlags]) -> None:
    if active := sorted(case for case, flags in cases.items() if flags.active):
        raise InvariantViolation(f"active golden cases {active} on a profile version under its purge's fence")


async def _delete_cases(run: PurgeRun, case_ids: Sequence[int]) -> int:
    """Step 4 (module docstring): the inactive cases, one chunk over their jobs; how many were deleted."""
    async with run.engine.connect() as conn:
        rows = (await conn.execute(_CASE_JOBS, {"ids": list(case_ids)})).all()
    if not rows:
        return 0  # deleted already
    async with run.chunk(job_ids=sorted({int(r.job_id) for r in rows})) as chunk:
        deleted = await deletion_graph.settle_and_delete_cases(
            chunk.conn, [int(r.id) for r in rows], now=run.now, check=_refuse_active
        )
    return len(deleted)


async def _redact_profile(run: PurgeRun, content: ManifestContent) -> None:
    """Steps 5 and 6's rows (module docstring): the version, its drafts, its CV row, in one chunk."""
    versions = _named(content, "field_redact", "profile_versions")
    drafts = _named(content, "field_redact", "profile_drafts")
    cv_files = _named(content, "field_redact", "cv_files")
    async with run.chunk() as chunk:
        for statement, ids in (
            (_REDACT_VERSIONS, versions),
            (_REDACT_DRAFTS, drafts),
            (_REDACT_CV_FILES, cv_files),
        ):
            if ids:
                await chunk.conn.execute(statement, {"ids": ids, "now": run.now})


class ProfilePurge:
    """The `profile_purge` procedure (module docstring)."""

    kind: ManifestKind = KIND
    deletes_exports: bool = True

    async def compute(
        self, conn: AsyncConnection, manifest: ManifestRow, *, now: datetime
    ) -> ManifestContent:
        version = get_int(dict(manifest.request), REQUEST_KEY)
        max_ids = await max_identity_ids(conn)
        tuples = await _ids(conn, _TUPLES, {"v": version, "bound": max_ids["llm_tuples"]})
        operations = await _operations(conn, version, tuples, max_ids)
        content = new_content(manifest, max_ids=max_ids, operations=operations)
        content["affected"] = {"job_ids": [], "profile_version_ids": [version], "tuple_ids": tuples}
        content["watermark"] = {"fenced_at": content["fenced_at"], "max_ids": dict(max_ids)}
        return content

    async def execute(self, run: PurgeRun, content: ManifestContent) -> None:
        tuples = _named(content, "row_delete", "score_targets")
        targets_deleted = (
            await _delete_targets(run, tuples, content["max_ids"]["score_targets"]) if tuples else 0
        )
        await _redact_content(run, content)
        cases = _named(content, "row_delete", "golden_cases")
        cases_deleted = await _delete_cases(run, cases) if cases else 0
        await _redact_profile(run, content)
        files = _files(content)
        for key in files:  # after the commit of the rows that named them
            run.cv_store.delete(key)
        affected = content.get("affected")
        log.info(
            "profile_purge_executed",
            manifest_id=run.manifest.id,
            version_ids=[] if affected is None else affected["profile_version_ids"],
            targets=targets_deleted,
            golden_cases=cases_deleted,
            calls_redacted=len(_named(content, "field_redact", "llm_calls")),
            files=len(files),
        )

    async def postconditions(self, conn: AsyncConnection, content: ManifestContent) -> list[Violation]:
        return []

    async def reapply_admission(self, conn: AsyncConnection, manifest: ManifestRow, *, now: datetime) -> None:
        return None


register_kind(ProfilePurge())
