"""The `parse` node (spec §9.5, §12.2; M2 plan Task 11): the CV's text, extracted by docparse, stored sealed.

1. **The receipt** (§9.1): a `cv_files.raw_text_enc` that is set means the text is there already, a paste's
   (the upload sealed it, so a pasted text never reaches docparse) or an earlier execution's. The node passes.
2. Otherwise it reads the stored file (`CvStore.read`) and hands it to docparse (`ParseClient.extract`),
   holding no transaction meanwhile, so neither the heartbeat nor an upload waits for the extraction.
3. One transaction, in the §9.1 order: the owner row (`lock_operation_owner`: a lost claim is `FencedOut`
   and writes nothing), then the draft `FOR UPDATE`, which must still be `extracting_text` (else the
   operation ends `succeeded` as obsolete, `end_obsolete`), then:
   - a CV that cannot be read (no stored file, or one that does not open: `CV_FILE_UNREADABLE`),
   - a docparse failure (`encrypted`, `too_many_pages`, `parse_error`, `parse_limit`, `unsupported`,
     `timeout`), or
   - a text of fewer than `LOW_TEXT_CHARS` characters, white space aside (`low_text`, §12.2: no OCR in v1)
   moves the draft `extracting_text → failed(code)` and finishes the operation `failed(code)` (`finish_in`):
   the draft's end and the operation's commit together, and the graph ends. Any other result stores the text
   without its NUL characters (`profile.text.drop_nul`: PostgreSQL's `text` refuses them, and every later
   text derives from this one) in `raw_text_enc`, sealed for its row, with `extraction_method` and
   `page_count`; the fault point `profile.text.after_parse_commit` follows that commit.

Logs carry ids, codes and counts only: never a file name, a text or an exception's message.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Final, TypeIs, get_args

from langchain_core.runnables import RunnableConfig

from jobhunter.crypto import DecryptError, data_keys
from jobhunter.db.sql import q
from jobhunter.faults import afault_point
from jobhunter.graphs.profile.deps import ProfileDeps
from jobhunter.graphs.profile.nodes import (
    CV_FILE_UNREADABLE,
    CvRow,
    Node,
    end_obsolete,
    execution,
    fail_draft,
    lock_draft,
    log,
    read_cv,
)
from jobhunter.graphs.profile.state import FAILED, OBSOLETE, ProfileState, ProfileUpdate
from jobhunter.profile.parse_client import CvMime, ParseFailed, ParseMethod
from jobhunter.profile.text import drop_nul
from jobhunter.worker.operations import OperationDeps, lock_operation_owner

NAME: Final = "parse"
NODE_TIMEOUT: Final = timedelta(seconds=120)
LOW_TEXT_CHARS: Final = 300
LOW_TEXT: Final = "low_text"

_STORE_TEXT = q(
    "UPDATE cv_files SET raw_text_enc = :sealed, extraction_method = :method, page_count = :pages "
    "WHERE id = :id"
)


@dataclass(frozen=True)
class _Text:
    text: str = field(repr=False)
    method: ParseMethod
    page_count: int | None


def _extractable(mime: str) -> TypeIs[CvMime]:
    return mime in get_args(CvMime)


async def _extract(od: OperationDeps, cv: CvRow, keys: Sequence[bytes], draft_id: int) -> _Text | str:
    """The CV's text, or the code of the failure that ends the draft (module docstring, step 3)."""
    if not _extractable(cv.mime) or cv.storage_path is None:  # a paste without its sealed text, or no file
        return CV_FILE_UNREADABLE
    try:
        data = od.cv_store.read(cv.storage_path, keys)
    except (FileNotFoundError, DecryptError) as exc:
        log.warning("profile_cv_unreadable", draft_id=draft_id, error_class=type(exc).__name__)
        return CV_FILE_UNREADABLE
    outcome = await od.parse_client.extract(data, cv.mime)
    if isinstance(outcome, ParseFailed):
        return outcome.code
    text = drop_nul(outcome.text)
    if len(text.strip()) < LOW_TEXT_CHARS:
        return LOW_TEXT
    return _Text(text, outcome.method, outcome.page_count)


def make_parse(deps: ProfileDeps) -> Node:
    async def parse(state: ProfileState, config: RunnableConfig) -> ProfileUpdate:
        op, od = execution(config)
        draft_id = state["draft_id"]
        cv = await read_cv(deps.engine, draft_id)
        if cv is not None and cv.raw_text_enc is not None:
            log.info("profile_parse_skipped", operation_id=op.claim.operation_id, draft_id=draft_id)
            return {"steps": [NAME]}
        keys = data_keys(od.cfg)
        extracted = None if cv is None else await _extract(od, cv, keys, draft_id)
        async with deps.engine.begin() as conn:
            await lock_operation_owner(conn, op.claim)
            status = await lock_draft(conn, draft_id)
            if cv is None or extracted is None or status != "extracting_text":
                await end_obsolete(conn, op, draft_id, status)
                return {"steps": [NAME], "outcome": OBSOLETE}
            if isinstance(extracted, str):
                await fail_draft(conn, draft_id, extracted, from_state="extracting_text")
                await op.finish_in(conn, status="failed", error_code=extracted)
                log.info(
                    "profile_text_failed",
                    operation_id=op.claim.operation_id,
                    draft_id=draft_id,
                    code=extracted,
                )
                return {"steps": [NAME], "outcome": FAILED}
            sealed = od.cv_store.encrypt_text(extracted.text, keys, cv_file_id=cv.id)
            params = {
                "id": cv.id,
                "sealed": sealed,
                "method": extracted.method,
                "pages": extracted.page_count,
            }
            await conn.execute(_STORE_TEXT, params)
        log.info(
            "profile_text_extracted",
            draft_id=draft_id,
            method=extracted.method,
            page_count=extracted.page_count,
            chars=len(extracted.text),
        )
        await afault_point("profile.text.after_parse_commit")
        return {"steps": [NAME]}

    return parse
