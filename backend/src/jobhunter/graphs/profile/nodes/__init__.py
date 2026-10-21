"""The node modules of `profile_graph` (spec §9.5) and what they share; each `make_*(deps)` builds its node.

A node is called with the graph's state and the invoke config (`Node`: LangGraph passes the config to a node
whose parameter is named `config`, and `mypy --strict` matches the names). It reads its execution from
`config["configurable"]["op"]` (`execution`): the handler's `OperationContext`, with the claim (for the
owner lock and `OperationOwner`), `finish_in`, `requeue_in` and the worker's `OperationDeps`. Never from the
state: the state is checkpointed, `configurable` is not.

What the nodes share, each write in the caller's transaction after the owner lock (§9.1: the owner row, then
the domain rows):
- `read_cv(engine, draft_id)`: the draft's `cv_files` row, read without a lock;
- `lock_draft(conn, draft_id)`: the draft's status, its row `FOR UPDATE` (`None` when it is gone);
- `fail_draft(conn, draft_id, code, *, from_state)`: `→ failed(code)`, with `failure_code` and the reason
  `code` (the CHECK `status <> 'failed' OR failure_code IS NOT NULL`);
- `end_obsolete(conn, op, draft_id, status)`: a draft that left the state the node works on ends the
  operation `succeeded` as obsolete, untouched.

The extract phase (Task 13) adds:
- `read_extract_draft(engine, draft_id)`: the draft as its nodes read it (`ExtractDraft`), without a lock
  (`fetch_extract_draft(conn, draft_id)` on the caller's connection; `lock_extract_draft(conn, draft_id)`
  with its row `FOR UPDATE`);
- `generation_owner(native_pdf=…, reviewed_text_sha256=…)`: the owner predicate of every write of the phase,
  on the `owner_cols` of `profile_drafts`. A text draft's generation is the text the user approved,
  `{"reviewed_text_sha256": <digest>, "native_pdf": False}`; a native draft's is `{"native_pdf": True}`
  alone, since its `reviewed_text_sha256` may be NULL, which SQL's `=` never matches (the ruling carried from
  Task 1). A write for another generation matches no row: no reordering lets a stale result land on newer
  text;
- `end_obsolete_in_own_tx(engine, op, draft_id)` and `fail_extraction(engine, op, draft_id, code, owner=…)`:
  one transaction each in the §9.1 order (the owner row, then the draft `FOR UPDATE`), the second moving the
  draft `extracting_profile → failed(code)` for `owner`'s generation and finishing the operation
  `failed(code)` in that transaction, or ending it obsolete when the draft is in another state or generation;
- `end_off_generation(conn, op, draft_id, status)`: the end of an execution whose draft is no longer its own.
  A draft in another state ends the operation obsolete, quietly. A draft still in `extracting_profile` but in
  another generation is a state no route makes (a new generation needs `text_review` or `failed` first, and
  each ends the old operation): it is logged as a warning and raises the warn alert
  `profile.extract_generation_mismatch` (ids only), since nothing would drive that draft any further; then the
  operation ends obsolete as well, and nothing of the stale result is written.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal, Protocol

from langchain_core.runnables import RunnableConfig
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from jobhunter.db.sql import q
from jobhunter.db.transitions import transition
from jobhunter.errors import InvariantViolation, TransitionConflict
from jobhunter.graphs.profile.state import FAILED, OBSOLETE, ProfileState, ProfileUpdate
from jobhunter.observability.alerts import raise_alert
from jobhunter.observability.logging import get_logger
from jobhunter.worker.operations import OperationContext, OperationDeps, lock_operation_owner

# A stored CV that cannot be read: no file (`FileNotFoundError`), or a file or text that does not open with
# the data keys (`crypto.DecryptError`: a wrong key, or another row's blob).
CV_FILE_UNREADABLE: Final = "cv_file_unreadable"
EXTRACTING_PROFILE: Final = "extracting_profile"
GENERATION_MISMATCH_ALERT: Final = "profile.extract_generation_mismatch"
_TABLE: Final = "profile_drafts"

_CV = q(
    "SELECT c.id, c.mime, c.storage_path, c.raw_text_enc FROM profile_drafts d "
    "JOIN cv_files c ON c.id = d.cv_file_id WHERE d.id = :d"
)
_LOCK_DRAFT = q("SELECT status FROM profile_drafts WHERE id = :d FOR UPDATE")
_EXTRACT_DRAFT_SQL: Final = (
    "SELECT d.status, d.cv_file_id, d.reviewed_text, d.reviewed_text_sha256, d.native_pdf, c.storage_path "
    "FROM profile_drafts d JOIN cv_files c ON c.id = d.cv_file_id WHERE d.id = :d"
)
_EXTRACT_DRAFT = q(_EXTRACT_DRAFT_SQL)
_LOCK_EXTRACT_DRAFT = q(_EXTRACT_DRAFT_SQL + " FOR UPDATE OF d")

log = get_logger("jobhunter.graphs.profile")


class Node(Protocol):
    async def __call__(self, state: ProfileState, config: RunnableConfig) -> ProfileUpdate: ...


def execution(config: RunnableConfig) -> tuple[OperationContext, OperationDeps]:
    """The handler's `OperationContext` and its `OperationDeps`, from `config["configurable"]["op"]`."""
    op = config.get("configurable", {}).get("op")
    if not isinstance(op, OperationContext) or op.deps is None:
        raise TypeError(
            "a profile node runs with its handler's OperationContext and deps in configurable['op']"
        )
    return op, op.deps


@dataclass(frozen=True)
class CvRow:
    """A draft's `cv_files` row, as the text-phase nodes read it (the sealed text stays sealed here)."""

    id: int
    mime: str
    storage_path: str | None
    raw_text_enc: bytes | None


async def read_cv(engine: AsyncEngine, draft_id: int) -> CvRow | None:
    """The `cv_files` row of draft `draft_id`, or `None` when the draft is gone."""
    async with engine.connect() as conn:
        row = (await conn.execute(_CV, {"d": draft_id})).first()
    if row is None:
        return None
    sealed = None if row.raw_text_enc is None else bytes(row.raw_text_enc)
    return CvRow(id=int(row.id), mime=row.mime, storage_path=row.storage_path, raw_text_enc=sealed)


async def lock_draft(conn: AsyncConnection, draft_id: int) -> str | None:
    """The status of draft `draft_id`, its row held `FOR UPDATE` for the transaction; `None` when gone."""
    status = (await conn.execute(_LOCK_DRAFT, {"d": draft_id})).scalar_one_or_none()
    return None if status is None else str(status)


async def fail_draft(
    conn: AsyncConnection,
    draft_id: int,
    code: str,
    *,
    from_state: Literal["extracting_text", "extracting_profile"],
    owner: Mapping[str, object] | None = None,
) -> None:
    """Move the draft `from_state → failed(code)`: `failure_code` and the reason are `code`. With `owner`,
    only a draft of that generation moves (`generation_owner`); `TransitionConflict` otherwise."""
    await transition(
        conn, _TABLE, draft_id, [from_state], "failed", reason=code, owner=owner, set_={"failure_code": code}
    )


async def end_obsolete(
    conn: AsyncConnection, op: OperationContext, draft_id: int, status: str | None
) -> None:
    """End the operation `succeeded` as obsolete: its draft (in `status`, or gone) is no longer its own."""
    await op.finish_in(conn, status="succeeded")
    log.info(
        "profile_operation_obsolete", operation_id=op.claim.operation_id, draft_id=draft_id, status=status
    )


# --- the extract phase (Task 13) ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExtractDraft:
    """A draft as the extract-phase nodes read it, without a lock: its status, its generation (the approved
    text, or the native PDF) and its CV file. The reviewed text stays out of every `repr`."""

    status: str
    cv_file_id: int
    storage_path: str | None
    native_pdf: bool
    reviewed_text_sha256: bytes | None
    reviewed_text: str | None = field(repr=False)

    def owner(self) -> dict[str, object]:
        """The owner predicate of this generation (`generation_owner`)."""
        return generation_owner(native_pdf=self.native_pdf, reviewed_text_sha256=self.reviewed_text_sha256)


async def read_extract_draft(engine: AsyncEngine, draft_id: int) -> ExtractDraft | None:
    """Draft `draft_id` with its CV file's storage path, or `None` when it is gone."""
    async with engine.connect() as conn:
        return await fetch_extract_draft(conn, draft_id)


async def fetch_extract_draft(conn: AsyncConnection, draft_id: int) -> ExtractDraft | None:
    """`read_extract_draft` on the caller's connection (in its transaction, after its locks)."""
    return _extract_draft((await conn.execute(_EXTRACT_DRAFT, {"d": draft_id})).first())


async def lock_extract_draft(conn: AsyncConnection, draft_id: int) -> ExtractDraft | None:
    """`fetch_extract_draft` with the draft's row held `FOR UPDATE` for the transaction."""
    return _extract_draft((await conn.execute(_LOCK_EXTRACT_DRAFT, {"d": draft_id})).first())


def _extract_draft(row: Row[Any] | None) -> ExtractDraft | None:
    if row is None:
        return None
    digest = None if row.reviewed_text_sha256 is None else bytes(row.reviewed_text_sha256)
    return ExtractDraft(
        status=str(row.status),
        cv_file_id=int(row.cv_file_id),
        storage_path=row.storage_path,
        native_pdf=bool(row.native_pdf),
        reviewed_text_sha256=digest,
        reviewed_text=row.reviewed_text,
    )


def generation_owner(*, native_pdf: bool, reviewed_text_sha256: bytes | None) -> dict[str, object]:
    """The owner predicate of the extract phase's writes for one generation (module docstring)."""
    if native_pdf:
        return {"native_pdf": True}
    if reviewed_text_sha256 is None:  # the §7.4 CHECK refuses such a draft in `extracting_profile`
        raise InvariantViolation("a reviewed-text generation without its reviewed_text_sha256")
    return {"reviewed_text_sha256": reviewed_text_sha256, "native_pdf": False}


async def end_off_generation(
    conn: AsyncConnection, op: OperationContext, draft_id: int, status: str | None
) -> str:
    """End the operation obsolete, loudly when the draft is still `extracting_profile` (module docstring); in
    the caller's transaction, after its owner and draft locks. The state's `outcome`."""
    if status == EXTRACTING_PROFILE:
        operation_id = op.claim.operation_id
        log.warning("profile_extract_generation_mismatch", operation_id=operation_id, draft_id=draft_id)
        await raise_alert(
            conn,
            code=GENERATION_MISMATCH_ALERT,
            severity="warn",
            subject=f"profile_draft:{draft_id}",
            message="an extraction met its draft in another generation; the draft stays extracting_profile",
            context={"operation_id": operation_id, "draft_id": draft_id},
        )
    await end_obsolete(conn, op, draft_id, status)
    return OBSOLETE


async def end_obsolete_in_own_tx(engine: AsyncEngine, op: OperationContext, draft_id: int) -> str:
    """The operation `succeeded` as obsolete in a transaction of its own, after the owner lock (a lost claim
    is `FencedOut` and writes nothing) and the draft's; the state's `outcome`."""
    async with engine.begin() as conn:
        await lock_operation_owner(conn, op.claim)
        await end_obsolete(conn, op, draft_id, await lock_draft(conn, draft_id))
    return OBSOLETE


async def fail_extraction(
    engine: AsyncEngine, op: OperationContext, draft_id: int, code: str, *, owner: Mapping[str, object]
) -> str:
    """`extracting_profile → failed(code)` for `owner`'s generation and the operation `failed(code)`, in one
    transaction (module docstring); the state's `outcome`, `failed` or `obsolete`."""
    async with engine.begin() as conn:
        await lock_operation_owner(conn, op.claim)
        status = await lock_draft(conn, draft_id)
        if status == EXTRACTING_PROFILE:
            try:
                await fail_draft(conn, draft_id, code, from_state=EXTRACTING_PROFILE, owner=owner)
            except TransitionConflict:
                pass  # another generation: the update matched no row and wrote nothing
            else:
                await op.finish_in(conn, status="failed", error_code=code)
                log.info(
                    "profile_extract_failed",
                    operation_id=op.claim.operation_id,
                    draft_id=draft_id,
                    code=code,
                )
                return FAILED
        return await end_off_generation(conn, op, draft_id, status)
