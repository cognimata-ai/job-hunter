"""docparse test helpers (Task 8): the worker's half of a job, and test extractors.

`run_job` hands its extractor to a `spawn` child, which imports it by module path; the extractors are
therefore module-level functions of this importable module (`tests.docparse.conftest`), never closures or
functions of a test module. Every text in them is invented.
"""

import io
import json
import os
import time
from pathlib import Path
from typing import Final, NoReturn

from docparse.extract import Extracted

INPUT_NAMES: Final = {
    "application/pdf": "input.pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "input.docx",
}
# An invented applicant; the text a crashing extractor puts into its exception message.
SECRET_TEXT: Final = "Max Mustermann, Musterstraße 1, 12345 Musterstadt"
MARKED_TEXT: Final = "Erika Musterfrau\nEntwicklerin bei der Musterfirma GmbH"
# Set by a test in the loop's process: a forked child would inherit it, a spawn child imports this module
# afresh and finds it empty.
LOOP_PROCESS_STATE: dict[str, bool] = {}


def make_job(root: Path, data: bytes, mime: str, *, mode: str = "extract", name: str = "job-1") -> Path:
    """A job directory under `root` as the worker leaves it: the directory 0770, `input.<ext>` 0660, then
    `request.json` (0660) published by a rename."""
    job = root / name
    job.mkdir(mode=0o770)
    job.chmod(0o770)
    source = job / INPUT_NAMES.get(mime, "input.pdf")
    source.write_bytes(data)
    source.chmod(0o660)
    request = {"v": 1, "mime": mime, "input": source.name, "mode": mode}
    tmp = job / "request.json.tmp"
    tmp.write_text(json.dumps(request))
    tmp.chmod(0o660)
    tmp.replace(job / "request.json")
    return job


def result_of(job: Path) -> dict[str, object]:
    loaded: dict[str, object] = json.loads((job / "result.json").read_text())
    return loaded


def encrypted_pdf(data: bytes) -> bytes:
    """`data` (a PDF) encrypted with a user password, as a password-protected CV is."""
    from pypdf import PdfWriter

    writer = PdfWriter(clone_from=io.BytesIO(data))
    writer.encrypt(user_password="geheim", algorithm="AES-256")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def burn(_path: Path, _mime: str) -> NoReturn:  # never returns: CPU-bound
    while True:
        pass


def hog(_path: Path, _mime: str) -> Extracted:
    """Allocates 1 GiB at once, far over a 256 MiB address-space limit."""
    block = bytearray(2**30)
    return Extracted(str(len(block)), "pypdf", 1)


def sleeper(_path: Path, _mime: str) -> Extracted:
    """Blocks without using CPU: only the wall-clock limit ends it."""
    time.sleep(120)
    return Extracted("", "pypdf", 1)


def explode(_path: Path, _mime: str) -> Extracted:
    """Fails as a parser can, with document text in its message."""
    raise ValueError(SECRET_TEXT)


def die(_path: Path, _mime: str) -> NoReturn:
    """The child exits at once with a failure status, without an answer."""
    os._exit(3)


def flood(_path: Path, _mime: str) -> Extracted:
    """More text than the loop takes from a child (`docparse.worker.MAX_ANSWER_BYTES`, 16 MiB)."""
    return Extracted("x" * (17 * 2**20), "pypdf", 1)


def mark(path: Path, _mime: str) -> Extracted:
    """Leaves a marker named after its process in the job directory, so a test counts the extractions."""
    (path.parent / f"ran-{os.getpid()}").touch()
    return Extracted(MARKED_TEXT, "pypdf", 1)


def report_inherited(_path: Path, _mime: str) -> Extracted:
    """Answers whether the child sees the loop process's `LOOP_PROCESS_STATE`, as only a fork would."""
    return Extracted(f"inherited={bool(LOOP_PROCESS_STATE)}", "pypdf", 1)
