"""docparse test helpers (Task 8): the worker's half of a job, and test extractors.

`run_job` hands its extractor to a `spawn` child, which imports it by module path; the extractors are
therefore module-level functions of this importable module (`tests.docparse.conftest`), never closures or
functions of a test module. Every text in them is invented.
"""

import contextlib
import errno
import fcntl
import json
import os
import resource
import stat
import threading
import time
from multiprocessing import resource_tracker
from pathlib import Path
from typing import Final, NoReturn

from docparse.extract import Extracted
from tests.support.profile import encrypted_pdf as encrypted_pdf  # moved there (Task 11)

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


def stall_mid_answer(_path: Path, _mime: str) -> NoReturn:
    """Writes the start of an answer to the loop's pipe (the child's write-only pipe above the standard
    streams that is not the resource tracker's, which a spawn child inherits too), then blocks: a child stuck
    mid-message."""
    tracker_fd = resource_tracker.getfd()
    for name in os.listdir("/proc/self/fd"):
        fd = int(name)
        with contextlib.suppress(OSError):  # the descriptor listdir itself used is gone
            write_only = fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_WRONLY
            if fd > 2 and fd != tracker_fd and write_only and stat.S_ISFIFO(os.fstat(fd).st_mode):
                os.write(fd, b'{"code": null, "method": "pypdf", "text": "Max Muster')
    time.sleep(120)
    os._exit(0)


def close_all_and_linger(_path: Path, _mime: str) -> NoReturn:
    """Closes every descriptor it inherited above the standard streams (the answer pipe, the end of the
    process sentinel, the resource tracker's pipe), so the loop sees EOF everywhere, then stays alive without
    using CPU: only a bounded reap that kills it ends the job."""
    os.closerange(3, resource.getrlimit(resource.RLIMIT_NOFILE)[0])
    time.sleep(120)
    os._exit(0)


def mark(path: Path, _mime: str) -> Extracted:
    """Leaves a marker named after its process in the job directory, so a test counts the extractions."""
    (path.parent / f"ran-{os.getpid()}").touch()
    return Extracted(MARKED_TEXT, "pypdf", 1)


def report_inherited(_path: Path, _mime: str) -> Extracted:
    """Answers whether the child sees the loop process's `LOOP_PROCESS_STATE`, as only a fork would."""
    return Extracted(f"inherited={bool(LOOP_PROCESS_STATE)}", "pypdf", 1)


# --- a compromised parse (final review FR-11) --------------------------------------------------------------

GRANDCHILD_PID: Final = "grandchild.pid"  # where `fork_and_overrun` leaves the pid of the process it started


def _linger_detached() -> NoReturn:
    """In a forked process: let go of every inherited descriptor (the answer pipe, the sentinel), so the loop
    sees the child's EOF and exit, and stay alive without using CPU."""
    os.closerange(3, resource.getrlimit(resource.RLIMIT_NOFILE)[0])
    time.sleep(120)
    os._exit(0)


def fork_and_linger(_path: Path, _mime: str) -> Extracted:
    """Starts a process that outlives the parse, then answers at once with its pid."""
    pid = os.fork()
    if pid == 0:
        _linger_detached()
    return Extracted(f"grandchild={pid}", "pypdf", 1)


def fork_and_overrun(path: Path, _mime: str) -> NoReturn:
    """Starts a process that outlives the parse, leaves its pid beside the input, then blocks until the loop
    kills the child at its deadline."""
    pid = os.fork()
    if pid == 0:
        _linger_detached()
    (path.parent / GRANDCHILD_PID).write_text(str(pid))
    time.sleep(120)
    os._exit(0)


def report_groups(_path: Path, _mime: str) -> Extracted:
    """Answers the child's pid, process group and session."""
    return Extracted(f"{os.getpid()} {os.getpgid(0)} {os.getsid(0)}", "pypdf", 1)


def try_to_start(_path: Path, _mime: str) -> Extracted:
    """Answers whether the child could start a process and a thread."""
    try:
        pid = os.fork()
    except OSError as exc:
        forked = f"refused:{errno.errorcode.get(exc.errno or 0, exc.errno)}"
    else:
        if pid == 0:
            os._exit(0)
        os.waitpid(pid, 0)
        forked = "started"
    try:
        thread = threading.Thread(target=lambda: None)
        thread.start()
        thread.join()
    except RuntimeError:
        threaded = "refused"
    else:
        threaded = "started"
    return Extracted(f"fork={forked} thread={threaded}", "pypdf", 1)


def litter(path: Path, _mime: str) -> Extracted:
    """Leaves copies of the decrypted input where a compromised parser can: in the job directory (a
    subdirectory that only its owner may enter, a FIFO) and beside it on the parse root (a hard link, a
    symlink, a directory of its own, one shaped like a job the worker is still writing)."""
    job, root = path.parent, path.parent.parent
    data = path.read_bytes()
    stash = job / "stash"
    stash.mkdir(mode=0o700)
    (stash / "copy.pdf").write_bytes(data)
    stash.chmod(0)  # only its owner may open it up again
    os.mkfifo(job / "pipe")
    os.link(path, root / "beside.pdf")
    (root / "link").symlink_to(path)
    (root / "hidden").mkdir()
    (root / "hidden" / "copy.txt").write_bytes(data)
    (root / "mimic").mkdir()
    (root / "mimic" / "input.pdf").write_bytes(data)
    return Extracted(MARKED_TEXT, "pypdf", 1)


def plant_stuck(path: Path, _mime: str) -> Extracted:
    """Leaves the file `stuck` beside the job, which a test makes impossible to remove."""
    (path.parent.parent / "stuck").write_bytes(path.read_bytes())
    return Extracted(MARKED_TEXT, "pypdf", 1)
