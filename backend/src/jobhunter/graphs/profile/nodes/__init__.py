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
"""

from dataclasses import dataclass
from typing import Final, Literal, Protocol

from langchain_core.runnables import RunnableConfig
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from jobhunter.db.sql import q
from jobhunter.db.transitions import transition
from jobhunter.graphs.profile.state import ProfileState, ProfileUpdate
from jobhunter.observability.logging import get_logger
from jobhunter.worker.operations import OperationContext, OperationDeps

# A stored CV that cannot be read: no file (`FileNotFoundError`), or a file or text that does not open with
# the data keys (`crypto.DecryptError`: a wrong key, or another row's blob).
CV_FILE_UNREADABLE: Final = "cv_file_unreadable"
_TABLE: Final = "profile_drafts"

_CV = q(
    "SELECT c.id, c.mime, c.storage_path, c.raw_text_enc FROM profile_drafts d "
    "JOIN cv_files c ON c.id = d.cv_file_id WHERE d.id = :d"
)
_LOCK_DRAFT = q("SELECT status FROM profile_drafts WHERE id = :d FOR UPDATE")

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
) -> None:
    """Move the draft `from_state → failed(code)`: `failure_code` and the reason are `code`."""
    await transition(conn, _TABLE, draft_id, [from_state], "failed", reason=code, set_={"failure_code": code})


async def end_obsolete(
    conn: AsyncConnection, op: OperationContext, draft_id: int, status: str | None
) -> None:
    """End the operation `succeeded` as obsolete: its draft (in `status`, or gone) is no longer its own."""
    await op.finish_in(conn, status="succeeded")
    log.info(
        "profile_operation_obsolete", operation_id=op.claim.operation_id, draft_id=draft_id, status=status
    )
