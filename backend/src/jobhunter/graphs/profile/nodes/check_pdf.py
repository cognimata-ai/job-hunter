"""The `check_pdf` node (spec §12.1, §12.7; M2 plan Task 13, Deviations 7 and 9): a native PDF's §12.1 checks,
before any request leaves the machine.

The extract phase's first node. A draft that is no longer `extracting_profile` (or is gone) ends the operation
`succeeded` as obsolete. A reviewed-text draft has nothing to check and passes on. For a native draft:
1. `bounds.NATIVE_PDF_ENABLED` must be true (V19). A native draft that meets a false flag (an upload or retry
   admitted before the flag changed) ends `failed('native_pdf_disabled')`, defensively, with nothing sent to
   docparse or to the API.
2. The stored file is decrypted (`CvStore.read`; a missing or undecryptable one is `cv_file_unreadable`) and
   handed to docparse (`ParseClient.validate`: encryption and page count, no text), holding no transaction.
   docparse sends nothing anywhere: it has no network.
3. One transaction in the §9.1 order (the owner row, then the draft `FOR UPDATE`): any docparse failure
   (`encrypted`, `too_many_pages`, or one that leaves §12.1 unchecked: `parse_error`, `parse_limit`,
   `unsupported`, `timeout`) ends the draft `failed(code)` with the operation (`fail_extraction`, guarded by
   the native generation's owner predicate), with no outbound request of any kind: no token count either.
   `ok` records docparse's `page_count` on the `cv_files` row.

Logs carry ids, codes and counts only.
"""

from datetime import timedelta
from typing import Final

from langchain_core.runnables import RunnableConfig

from jobhunter.crypto import DecryptError, data_keys
from jobhunter.db.sql import q
from jobhunter.graphs.profile.deps import ProfileDeps
from jobhunter.graphs.profile.nodes import (
    CV_FILE_UNREADABLE,
    EXTRACTING_PROFILE,
    ExtractDraft,
    Node,
    end_obsolete,
    end_obsolete_in_own_tx,
    execution,
    fail_extraction,
    lock_draft,
    log,
    read_extract_draft,
)
from jobhunter.graphs.profile.state import OBSOLETE, ProfileState, ProfileUpdate
from jobhunter.llm import bounds
from jobhunter.profile.parse_client import ParseFailed
from jobhunter.worker.operations import OperationDeps, lock_operation_owner

NAME: Final = "check_pdf"
# The parse client's own timeout (60 s) plus the file's decryption, as for `parse`.
NODE_TIMEOUT: Final = timedelta(seconds=120)
NATIVE_PDF_DISABLED: Final = "native_pdf_disabled"

_PAGE_COUNT = q("UPDATE cv_files SET page_count = :pages WHERE id = :id")


async def _check(od: OperationDeps, draft: ExtractDraft, draft_id: int) -> int | str:
    """docparse's page count of the native PDF, or the code that ends the draft (module docstring)."""
    if not bounds.NATIVE_PDF_ENABLED:
        return NATIVE_PDF_DISABLED
    if draft.storage_path is None:  # a paste has no file: no native path
        return CV_FILE_UNREADABLE
    try:
        data = od.cv_store.read(draft.storage_path, data_keys(od.cfg))
    except (FileNotFoundError, DecryptError) as exc:
        log.warning("profile_cv_unreadable", draft_id=draft_id, error_class=type(exc).__name__)
        return CV_FILE_UNREADABLE
    outcome = await od.parse_client.validate(data)
    if isinstance(outcome, ParseFailed):
        return outcome.code
    if outcome.page_count is None:  # docparse answers a PDF's count; a result without one is not a PDF's
        return "parse_error"
    return outcome.page_count


def make_check_pdf(deps: ProfileDeps) -> Node:
    async def check_pdf(state: ProfileState, config: RunnableConfig) -> ProfileUpdate:
        op, od = execution(config)
        draft_id = state["draft_id"]
        draft = await read_extract_draft(deps.engine, draft_id)
        if draft is None or draft.status != EXTRACTING_PROFILE:
            return {"steps": [NAME], "outcome": await end_obsolete_in_own_tx(deps.engine, op, draft_id)}
        if not draft.native_pdf:
            return {"steps": [NAME]}  # a reviewed text: nothing to check
        checked = await _check(od, draft, draft_id)
        if isinstance(checked, str):
            outcome = await fail_extraction(deps.engine, op, draft_id, checked, owner=draft.owner())
            return {"steps": [NAME], "outcome": outcome}
        async with deps.engine.begin() as conn:
            await lock_operation_owner(conn, op.claim)
            status = await lock_draft(conn, draft_id)
            if status != EXTRACTING_PROFILE:
                await end_obsolete(conn, op, draft_id, status)
                return {"steps": [NAME], "outcome": OBSOLETE}
            await conn.execute(_PAGE_COUNT, {"id": draft.cv_file_id, "pages": checked})
        log.info(
            "profile_pdf_checked", operation_id=op.claim.operation_id, draft_id=draft_id, page_count=checked
        )
        return {"steps": [NAME]}

    return check_pdf
