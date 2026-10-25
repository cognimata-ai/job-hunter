"""The `strip_contacts` node (spec §9.5, §12.3; M2 plan Task 11): the contact-stripped text, then the review.

It opens the stored raw text (`cv_files.raw_text_enc`, `CvStore.decrypt_text`) and runs
`profile.contacts.strip_contacts` over it in a worker thread (it is pure, and a long CV costs its regexes a
noticeable fraction of a second that the event loop, which runs the heartbeat, must not wait for). Then one
transaction, in the §9.1 order: the owner row (`lock_operation_owner`: a lost claim is `FencedOut` and
writes nothing), the draft `FOR UPDATE`, which must still be `extracting_text` (else the operation ends
`succeeded` as obsolete), then the `cv_files` row:
- `stripped_text` and `strip_report` (the counts per category, never a value);
- the draft `extracting_text → text_review`;
- the operation `succeeded` (`finish_in`), so the review state and the operation's end commit together; the
  fault point `profile.text.after_text_review_commit` follows that commit.
A raw text that is missing or does not open (`DecryptError`: a text sealed for another row, a wrong key) ends
the draft `failed('cv_file_unreadable')` and the operation `failed` in that transaction instead.

Nothing leaves the machine here: the user reviews the stripped text first (§12.3). Logs carry ids and counts
only.
"""

import asyncio
from datetime import timedelta
from typing import Final

from langchain_core.runnables import RunnableConfig

from jobhunter.crypto import DecryptError, data_keys
from jobhunter.db.sql import q
from jobhunter.db.transitions import transition
from jobhunter.faults import afault_point
from jobhunter.graphs.profile.deps import ProfileDeps
from jobhunter.graphs.profile.nodes import (
    CV_FILE_UNREADABLE,
    Node,
    end_obsolete,
    execution,
    fail_draft,
    lock_draft,
    log,
    read_cv,
)
from jobhunter.graphs.profile.state import FAILED, OBSOLETE, TEXT_REVIEW, ProfileState, ProfileUpdate
from jobhunter.profile.contacts import StripResult, strip_contacts
from jobhunter.worker.operations import lock_operation_owner

NAME: Final = "strip_contacts"
NODE_TIMEOUT: Final = timedelta(seconds=30)

_STORE_STRIPPED = q(
    "UPDATE cv_files SET stripped_text = :stripped, strip_report = :report WHERE id = :id", jsonb=["report"]
)


def make_strip_contacts(deps: ProfileDeps) -> Node:
    async def strip(state: ProfileState, config: RunnableConfig) -> ProfileUpdate:
        op, od = execution(config)
        draft_id = state["draft_id"]
        cv = await read_cv(deps.engine, draft_id)
        result: StripResult | None = None
        if cv is not None and cv.raw_text_enc is not None:
            try:
                raw = od.cv_store.decrypt_text(cv.raw_text_enc, data_keys(od.cfg), cv_file_id=cv.id)
            except DecryptError as exc:
                log.warning("profile_cv_unreadable", draft_id=draft_id, error_class=type(exc).__name__)
            else:
                result = await asyncio.to_thread(strip_contacts, raw)
                del raw
        async with deps.engine.begin() as conn:
            await lock_operation_owner(conn, op.claim)
            status = await lock_draft(conn, draft_id)
            if cv is None or status != "extracting_text":
                await end_obsolete(conn, op, draft_id, status)
                return {"steps": [NAME], "outcome": OBSOLETE}
            if result is None:
                await fail_draft(conn, draft_id, CV_FILE_UNREADABLE, from_state="extracting_text")
                await op.finish_in(conn, status="failed", error_code=CV_FILE_UNREADABLE)
                return {"steps": [NAME], "outcome": FAILED}
            report = dict(result.report)
            await conn.execute(
                _STORE_STRIPPED, {"id": cv.id, "stripped": result.stripped_text, "report": report}
            )
            await transition(conn, "profile_drafts", draft_id, ["extracting_text"], TEXT_REVIEW)
            await op.finish_in(conn, status="succeeded")
        log.info(
            "profile_text_review",
            operation_id=op.claim.operation_id,
            draft_id=draft_id,
            replacements=len(result.replacements),
        )
        await afault_point("profile.text.after_text_review_commit")
        return {"steps": [NAME], "outcome": TEXT_REVIEW}

    return strip
