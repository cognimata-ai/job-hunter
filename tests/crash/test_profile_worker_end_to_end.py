"""Task 24: M2 acceptance through the real worker process (`jobhunter worker` on the fake LLM, spec §18.4) and
a real docparse process (`python -m docparse.worker`): a synthetic two-page PDF uploaded through the api
reaches the text review through docparse, and after the approval its profile is extracted on the synthetic
`extract_profile` stream; the worker then stops cleanly on SIGTERM.

The docparse child imports its parsers from the environment, so `docparse_group` checks the `docparse` group
there before and after the test (M3 carry T18-b). Both children share a tmp `parse_jobs` root of mode 2770
(`JOBHUNTER_PARSE_JOBS_DIR`), as the tmpfs of §12.2 is shared; the worker reads the file from the api's CV
store, and its embedder is the session's fake one. The CV is `SYNTHETIC_CV`, an invented candidate; no paid
call.
"""

import os
import signal
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Final

import pytest
import sqlalchemy as sa
from jobhunter.config import ENV_PREFIXES
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.procs import await_until, wait_until
from tests.support.profile import SYNTHETIC_CV, cv_row, draft_row, idem, minimal_pdf
from tests.support.settings import fresh_key

pytestmark = [pytest.mark.crash, pytest.mark.slow]
WORKER: Final = [sys.executable, "-m", "jobhunter.cli", "worker"]
DOCPARSE: Final = [sys.executable, "-m", "docparse.worker"]
TWO_PAGE_PDF: Final = minimal_pdf(SYNTHETIC_CV.splitlines(), lines_per_page=10)
PHASE_WITHIN_S: Final = 60
# Words of the CV that neither child may log (its name, employer and work), compared case-folded.
CV_MARKERS: Final = ("Musterfrau", "Musterfirma", "Zahlungsdienst", "Datenplattform")


def reached(engine: AsyncEngine, draft_id: int, status: str) -> Callable[[], Awaitable[bool]]:
    """Whether the draft is in `status`; a draft that failed on the way fails the test at once."""

    async def check() -> bool:
        draft = await draft_row(engine, draft_id)
        assert draft.status != "failed", (draft.status, draft.failure_code)
        return bool(draft.status == status)

    return check


async def test_pdf_draft_through_the_worker_process(
    migrated,
    proc_env,
    procs,
    fake_embedder,
    engine,
    logged_in_client,
    upload,
    cv_store_dir,
    docparse_group,
    tmp_path: Path,
) -> None:
    root = tmp_path / "parse"
    root.mkdir()
    root.chmod(0o2770)  # as the parse_jobs tmpfs: every job directory inherits its group
    inherited = {name: value for name, value in os.environ.items() if not name.startswith(ENV_PREFIXES)}
    parser = procs.spawn(DOCPARSE, {**inherited, "JOBHUNTER_PARSE_JOBS_DIR": str(root)})
    wait_until(lambda: "docparse.serving" in parser.output(), 30)
    w = procs.spawn(
        WORKER,
        proc_env(
            "worker",
            migrated,
            JOBHUNTER_FAKE_LLM="1",
            JOBHUNTER_PARSE_JOBS_DIR=str(root),
            JOBHUNTER_CV_STORE_DIR=str(cv_store_dir),
        ),
    )
    w.wait_event("worker_ready", 60)

    draft_id = await upload(TWO_PAGE_PDF)
    await await_until(reached(engine, draft_id, "text_review"), PHASE_WITHIN_S, step_s=0.5)
    cv = await cv_row(engine, draft_id)
    assert (cv.extraction_method, cv.page_count) == ("pypdf", 2)
    assert cv.strip_report["email"] >= 1 and cv.storage_path is not None
    r = await logged_in_client.post(
        f"/api/profile/drafts/{draft_id}/text-review", headers=idem(fresh_key("approve"))
    )
    assert r.status_code == 202, (r.status_code, r.text)
    await await_until(reached(engine, draft_id, "ready"), PHASE_WITHIN_S, step_s=0.5)
    assert (await draft_row(engine, draft_id)).draft_json is not None
    async with engine.connect() as conn:
        ledger = (await conn.execute(sa.text("SELECT purpose, state FROM llm_spend_ledger"))).all()
    assert [tuple(e) for e in ledger] == [("extract_profile", "billed")]  # the one call, on the fake
    assert list(root.iterdir()) == []  # the worker removed both job directories

    w.signal(signal.SIGTERM)
    assert w.exit_within(60) == 0
    assert w.wait_event("worker_shutdown_complete", 1)["exit_code"] == 0
    parser.signal(signal.SIGTERM)
    assert parser.exit_within(30) == 0
    for child in (w, parser):
        output = child.output().casefold()
        assert [marker for marker in CV_MARKERS if marker.casefold() in output] == []
