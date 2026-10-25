"""The docparse loop (spec §3.2, §12.2): CV extraction jobs on the parse_jobs volume, each in an rlimited
child.

**The file protocol, version 1**, on `<root>/<job_id>/`. The root is `/data/parse`, a tmpfs of mode 2770 and
group 10011 that only the worker and docparse mount (§19.1), so the plaintext never reaches a disk.

1. The worker (`jobhunter.profile.parse_client`) creates the job directory (0770; the setgid root gives it
   the group 10011), writes `input.pdf` or `input.docx` (0660), then publishes `request.json` atomically
   (a temporary name, then a rename): `{"v": 1, "mime": …, "input": "input.pdf", "mode": "extract" |
   "validate"}`.
2. docparse claims the job by renaming `request.json` to `request.claimed`. A rename succeeds once, so a
   second loop never takes the same job (`claim`).
3. It extracts in a child, then writes `text.txt` (an `ok` extraction only) and `result.json`, each to a
   temporary name and renamed, 0660: `{"v": 1, "status": "ok" | "error", "code": null | "encrypted" |
   "too_many_pages" | "parse_error" | "parse_limit" | "unsupported", "method": "pypdf" | "pdfplumber" |
   "docx", "page_count": int | null, "chars": int}`. `result.json` appears only once `text.txt` is complete,
   and `chars` is the length of its text. `validate` checks a PDF (encryption, page count) and writes no text
   (`chars` 0). An error names the method of its type (`pypdf` for a PDF), and `null` only for a request that
   names no supported type.
4. The worker reads the result and removes the directory. docparse removes only what no live job holds
   (`sweep`, below).

**The child.** `run_job` runs each extraction in a `multiprocessing` child of the `spawn` context, never a
fork (a fork from a multi-threaded process warns on Python 3.12+, and the worker's tests serve this loop
from a thread). The child imports its extractor by module path. Its first statement makes it the leader of a
session and process group of its own (`os.setsid`); then it lowers RLIMIT_CPU, RLIMIT_AS, RLIMIT_CORE and
RLIMIT_NPROC (0: under docparse's uid 10002 it can start no process and no thread, so nothing can leave its
group; root is exempt) before it opens the file, silences logging and warnings (a parser's message can
quote the document), and answers over a one-way `os.pipe` with JSON, never a pickle (it has read a hostile
file): the answer is everything it writes before its end of the pipe closes, and it counts only when the
child then exits with status 0. The loop reads it against one deadline, `SPAWN_MARGIN_S` plus
`Limits.wall_s` after the start, however the bytes arrive, and kills the child when it overruns. Once the
child has ended, however it ended, the loop kills its process group (`_reap`), so no process a parse started
outlives its job. A child ended by a limit gives `parse_limit`: SIGXCPU or SIGKILL, a MemoryError, the
deadline (a parse that blocks without using CPU, or one that stops mid-answer), or an answer over
`MAX_ANSWER_BYTES`. Any other failure gives `parse_error`, also a child that cannot be started at all
(EMFILE, ENOMEM): the job is never left without a result.

**The loop.** `serve` scans the root every 0.2 s, the oldest request first, one job at a time (a child may
take 768 MiB of the container's 1 GiB), until `stop` is set; one job's failure never ends it. It opens no
socket (the container has no network at all, `network_mode: none`) and logs only job ids, statuses, codes and
error classes: never a file name, a text or an exception message. `main()` serves the root named by
`JOBHUNTER_PARSE_JOBS_DIR` (default `/data/parse`) until SIGTERM or SIGINT.

**The sweep** (final review FR-11). A compromised parse can leave copies of a CV on the shared tmpfs: beside
its job, or in a directory only its uid may enter, which the worker (10001) cannot remove. docparse (10002)
can, so `serve` sweeps the root before its first job, after every job and every `IDLE_SWEEP_S` while idle:
it removes everything that no live job holds (`sweep` names what is live), following no symlink, and opens up
a directory of its own uid first. A sweep that cannot remove an entry ends the loop before any further job,
and `main()` exits with `SWEEP_FAILED_EXIT`: compose restarts the container, which ends every process in it,
and a sandbox whose sweep still fails is recreated with its volume.
"""

import json
import logging
import multiprocessing
import os
import resource
import signal
import stat
import sys
import threading
import time
import warnings
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from multiprocessing.connection import Connection
from pathlib import Path
from types import FrameType
from typing import Final, Literal, get_args

from docparse.extract import DOCX_MIME, PDF_MIME, Extracted, ExtractionRefused, extract_document, validate_pdf

log = logging.getLogger("docparse")

Extractor = Callable[[Path, str], Extracted]
Mode = Literal["extract", "validate"]
Code = Literal["encrypted", "too_many_pages", "parse_error", "parse_limit", "unsupported"]
ResultMethod = Literal["pypdf", "pdfplumber", "docx"]

ROOT_ENV: Final = "JOBHUNTER_PARSE_JOBS_DIR"
DEFAULT_ROOT: Final = Path("/data/parse")
SCAN_INTERVAL_S: Final = 0.2
PROTOCOL_VERSION: Final = 1
REQUEST: Final = "request.json"
CLAIMED: Final = "request.claimed"
TEXT: Final = "text.txt"
RESULT: Final = "result.json"
FILE_MODE: Final = 0o660
INPUT_NAMES: Final[dict[str, str]] = {PDF_MIME: "input.pdf", DOCX_MIME: "input.docx"}
TYPE_METHODS: Final[dict[str, ResultMethod]] = {PDF_MIME: "pypdf", DOCX_MIME: "docx"}
# The child's answer, as JSON with ASCII escapes: far above any CV's text (≤ 20 pages, §12.1), far below the
# container's memory.
MAX_ANSWER_BYTES: Final = 16 * 2**20
_LIMIT_SIGNALS: Final = frozenset({-signal.SIGXCPU, -signal.SIGKILL})
_READ_CHUNK: Final = 2**16
# The child's start (a fresh interpreter importing docparse) before `Limits.wall_s` begins, and the wait for
# its exit after an answer or a kill. With the default wall_s they stay below the client's 60 s timeout
# (tests/integration/test_parse_client.py pins it), so a stuck parse answers parse_limit, not a timeout.
SPAWN_MARGIN_S: Final = 2.0
EXIT_JOIN_S: Final = 5.0
_REAP_POLL_S: Final = 0.01
# The sweep (final review FR-11). An entry of the root unchanged for this long is no live job's: the worker
# gives up on a job after its 60 s timeout and removes it (tests/integration/test_parse_client.py pins the
# order). Only these regular files are a job's; the worker writes the first four, docparse the rest.
LEFTOVER_AFTER_S: Final = 120.0
PROTOCOL_FILES: Final = frozenset({*INPUT_NAMES.values(), REQUEST, f"{REQUEST}.tmp", CLAIMED, TEXT, RESULT})
IDLE_SWEEP_S: Final = 60.0
SWEEP_FAILED_EXIT: Final = 70  # EX_SOFTWARE: compose restarts the container
_DIR_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


@dataclass(frozen=True)
class Limits:
    """The child's limits: CPU seconds (RLIMIT_CPU), address space in bytes (RLIMIT_AS), the wall-clock
    seconds, after `SPAWN_MARGIN_S` for its start, within which its whole answer must arrive before the loop
    kills it, and RLIMIT_NPROC: 0, so that under a non-root uid the child starts no process and no thread
    (`None` leaves the limit as it is: a test of the group kill)."""

    cpu_s: int
    as_bytes: int
    wall_s: float = 45.0
    processes: int | None = 0


DEFAULT_LIMITS: Final = Limits(cpu_s=30, as_bytes=768 * 2**20)


@dataclass(frozen=True)
class _Request:
    mime: str
    path: Path
    mode: Mode


@dataclass(frozen=True)
class _Outcome:
    code: Code | None
    method: ResultMethod | None
    page_count: int | None = None
    text: str | None = None


def claim(job_dir: Path) -> bool:
    """Take the job: rename its published request. False when it has none (unpublished or claimed)."""
    try:
        os.rename(job_dir / REQUEST, job_dir / CLAIMED)
    except FileNotFoundError:
        return False
    return True


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _read_request(path: Path) -> _Request | _Outcome:
    """The claimed request, or the `unsupported` outcome of one docparse does not take."""
    try:
        raw = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return _Outcome("unsupported", None)
    if not isinstance(raw, dict):
        return _Outcome("unsupported", None)
    mime, mode = raw.get("mime"), raw.get("mode")
    method = TYPE_METHODS.get(mime) if isinstance(mime, str) else None
    if (
        method is None
        or not (_is_count(raw.get("v")) and raw.get("v") == PROTOCOL_VERSION)
        or raw.get("input") != INPUT_NAMES[str(mime)]
        or mode not in get_args(Mode)
        or (mode == "validate" and mime != PDF_MIME)
    ):
        return _Outcome("unsupported", method)
    return _Request(
        str(mime), path.parent / INPUT_NAMES[str(mime)], "validate" if mode == "validate" else "extract"
    )


def _limit(kind: int, soft: int, hard: int) -> None:
    _, current_hard = resource.getrlimit(kind)
    if current_hard != resource.RLIM_INFINITY:
        hard = min(hard, current_hard)
    resource.setrlimit(kind, (min(soft, hard), hard))


def _child(sender: Connection, extractor: Extractor, request: _Request, limits: Limits) -> None:
    """The extraction, in the spawn child: a process group of its own, the limits, then the parse, then one
    JSON answer. Nothing escapes it, so the child never prints a traceback."""
    answer: dict[str, object]
    try:
        os.setsid()  # first: the group `_reap` kills, before anything reads the document
        logging.disable(logging.CRITICAL)
        warnings.simplefilter("ignore")
        signal.signal(signal.SIGXCPU, signal.SIG_DFL)  # the soft CPU limit ends the child
        _limit(resource.RLIMIT_CORE, 0, 0)
        _limit(resource.RLIMIT_AS, limits.as_bytes, limits.as_bytes)
        _limit(resource.RLIMIT_CPU, limits.cpu_s, limits.cpu_s + 1)  # the hard limit: SIGKILL a second later
        if limits.processes is not None:  # root is exempt: under 10002 nothing can leave the group
            _limit(resource.RLIMIT_NPROC, limits.processes, limits.processes)
        if request.mode == "validate":
            validated = validate_pdf(request.path)
            answer = {"code": None, "method": "pypdf", "page_count": validated.page_count, "text": None}
        else:
            extracted = extractor(request.path, request.mime)
            answer = {
                "code": None,
                "method": extracted.method,
                "page_count": extracted.page_count,
                "text": extracted.text,
            }
    except MemoryError:
        answer = {"code": "parse_limit"}
    except ExtractionRefused as refused:
        answer = {"code": refused.code}
    except BaseException:  # noqa: BLE001 - a hostile parse's failure is its code; its message may quote the CV
        answer = {"code": "parse_error"}
    with suppress(BaseException):  # no answer is an answer too: the loop reads the exit status
        view = memoryview(json.dumps(answer).encode("ascii"))
        while view:
            view = view[os.write(sender.fileno(), view) :]
    sender.close()


def _receive(receiver: Connection, deadline: float) -> bytes | Literal["timeout", "oversize"]:
    """Everything the child writes until its end of the pipe closes, read against `deadline` (monotonic):
    a child that stops mid-answer is a timeout, not a loop that waits for ever."""
    fd = receiver.fileno()
    answer = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not receiver.poll(remaining):
            return "timeout"
        chunk = os.read(fd, _READ_CHUNK)  # readable: it does not block
        if not chunk:
            return bytes(answer)  # the child's end closed
        answer += chunk
        if len(answer) > MAX_ANSWER_BYTES:
            return "oversize"


def _kill_group(child: multiprocessing.process.BaseProcess) -> None:
    """SIGKILL to the child's process group: the child, which leads it from its first statement
    (`os.setsid`), and every process it started. A group that does not exist (the child has not reached
    `setsid` yet, or the group has ended) leaves the child to `Process.kill`, a no-op once it is reaped."""
    if child.pid is not None:  # a started child (never 0, which would name the loop's own group)
        with suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)
    child.kill()


def _reap(child: multiprocessing.process.BaseProcess, *, kill: bool) -> int | None:
    """The child's exit status, within `EXIT_JOIN_S`: a child still alive then is killed and reaped. Then
    its process group is killed, so nothing the parse started outlives the job.

    `Process.join(timeout)` is no bound: it waits for the process sentinel, then calls a blocking
    `waitpid`, and a child can close its end of the sentinel (and of the answer pipe) and stay alive.
    `exitcode` polls with WNOHANG, so the wait here is bounded whatever the child does. The group's id is the
    child's pid, which the kernel does not reuse while any member of the group lives."""
    if kill:
        _kill_group(child)
    deadline = time.monotonic() + EXIT_JOIN_S
    while child.exitcode is None and time.monotonic() < deadline:
        time.sleep(_REAP_POLL_S)
    if child.exitcode is None:  # alive after its answer or its EOF: an overrun, killed
        _kill_group(child)
        child.join()  # after SIGKILL, waitpid returns as soon as the kernel has ended the process
    exitcode = child.exitcode
    _kill_group(child)  # what the child left in its group, after its exit
    child.close()
    return exitcode


def _answer_outcome(answer: bytes, method: ResultMethod, mode: Mode) -> _Outcome:
    """The child's answer, checked as anything that crossed from the sandbox is: a malformed one is
    `parse_error`."""
    try:
        payload = json.loads(answer)
    except (ValueError, RecursionError):
        return _Outcome("parse_error", method)
    if not isinstance(payload, dict):
        return _Outcome("parse_error", method)
    code = payload.get("code")
    if code is not None:
        return _Outcome(code if code in get_args(Code) else "parse_error", method)
    got_method, page_count, text = payload.get("method"), payload.get("page_count"), payload.get("text")
    if (
        got_method not in get_args(ResultMethod)
        or not (page_count is None or _is_count(page_count))
        or not (text is None if mode == "validate" else isinstance(text, str))
    ):
        return _Outcome("parse_error", method)
    return _Outcome(None, got_method, page_count, text)


def _run_child(request: _Request, extractor: Extractor, limits: Limits) -> _Outcome:
    method = TYPE_METHODS[request.mime]
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)  # an os.pipe: no socket
    child = context.Process(target=_child, args=(sender, extractor, request, limits), daemon=True)
    with receiver:
        with sender:  # the child holds its own copy: the receiver sees EOF once the child has ended
            child.start()
        answer: bytes | Literal["timeout", "oversize"] = "timeout"  # if the read fails, the child is killed
        try:
            answer = _receive(receiver, time.monotonic() + SPAWN_MARGIN_S + limits.wall_s)
        finally:
            exitcode = _reap(child, kill=not isinstance(answer, bytes))
    if not isinstance(answer, bytes):
        return _Outcome("parse_limit", method)  # killed at the deadline or for the size of its answer
    if exitcode != 0:  # ended without its whole answer: what ended it decides
        return _Outcome("parse_limit" if exitcode in _LIMIT_SIGNALS else "parse_error", method)
    return _answer_outcome(answer, method, request.mode)


def _write_atomic(path: Path, data: bytes) -> None:
    """`data` at `path` (0660, whatever the umask: the worker reads it through the group), written to a
    temporary name and renamed, so no reader ever sees part of it."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, FILE_MODE)
    with os.fdopen(fd, "wb") as out:
        os.fchmod(out.fileno(), FILE_MODE)
        out.write(data)
    os.replace(tmp, path)


def _publish(job_dir: Path, outcome: _Outcome) -> None:
    chars = 0
    if outcome.code is None and outcome.text is not None:
        # Valid UTF-8 whatever the parser produced (a lone surrogate becomes "?"); `chars` counts what is
        # read back.
        text = outcome.text.encode("utf-8", "replace").decode("utf-8")
        _write_atomic(job_dir / TEXT, text.encode("utf-8"))
        chars = len(text)
    result = {
        "v": PROTOCOL_VERSION,
        "status": "ok" if outcome.code is None else "error",
        "code": outcome.code,
        "method": outcome.method,
        "page_count": outcome.page_count,
        "chars": chars,
    }
    _write_atomic(job_dir / RESULT, json.dumps(result).encode("ascii"))


def run_job(
    job_dir: Path, *, extractor: Extractor = extract_document, limits: Limits = DEFAULT_LIMITS
) -> None:
    """Claim the job in `job_dir`, extract in an rlimited spawn child, and publish its result. A job another
    loop has claimed is left alone."""
    if not claim(job_dir):
        return
    request = _read_request(job_dir / CLAIMED)
    if isinstance(request, _Outcome):
        outcome = request
    else:
        try:
            outcome = _run_child(request, extractor, limits)
        except Exception as exc:  # noqa: BLE001 - a child that cannot run is the job's parse_error; class only
            log.error("docparse.child_failed job_id=%s error=%s", job_dir.name, type(exc).__name__)
            outcome = _Outcome("parse_error", TYPE_METHODS[request.mime])
    _publish(job_dir, outcome)
    status = "ok" if outcome.code is None else "error"
    log.info("docparse.job_done job_id=%s status=%s code=%s", job_dir.name, status, outcome.code or "-")


def _pending(root: Path) -> list[Path]:
    """The job directories with a published request, the oldest request first."""
    found = []
    for request in root.glob(f"*/{REQUEST}"):
        with suppress(FileNotFoundError):  # claimed or removed meanwhile
            found.append((request.stat().st_mtime_ns, request.parent))
    return [job_dir for _, job_dir in sorted(found)]


class SweepFailed(Exception):
    """A leftover under the root could not be removed (`sweep`): the loop takes no further job."""

    def __init__(self, failures: int) -> None:
        super().__init__(f"{failures} leftover(s) not removed")  # a count: never a name of the root
        self.failures = failures


def _handle(dir_fd: int, name: str) -> int | None:
    """An `O_PATH` descriptor of the entry `name` itself (no symlink is followed); None when it is gone."""
    try:
        return os.open(name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
    except FileNotFoundError:
        return None


def _listing(handle: int, st: os.stat_result) -> int:
    """A descriptor that lists and changes the directory of `handle`, reopened through it, so no name is
    resolved twice. A directory of this process's uid that it may not enter or change (a parse can chmod
    what it made) gets its owner's rwx back first."""
    via = f"/proc/self/fd/{handle}"
    if st.st_uid == os.geteuid() and stat.S_IMODE(st.st_mode) & 0o700 != 0o700:
        os.chmod(via, stat.S_IMODE(st.st_mode) | 0o700)
    return os.open(via, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)


def _open_for_removal(dir_fd: int, name: str) -> int | None:
    """Unlink the entry `name` of `dir_fd` when it is no directory (a file, a symlink itself, a FIFO) and
    return None, as for one that is gone; a directory's `_listing` descriptor otherwise."""
    handle = _handle(dir_fd, name)
    if handle is None:
        return None
    try:
        st = os.fstat(handle)
        if not stat.S_ISDIR(st.st_mode):
            with suppress(FileNotFoundError):
                os.unlink(name, dir_fd=dir_fd)
            return None
        return _listing(handle, st)
    finally:
        os.close(handle)


def _remove_tree(dir_fd: int, name: str) -> None:
    """Remove the entry `name` of `dir_fd` with everything under it, depth first without recursion, following
    no symlink. An entry that is gone meanwhile is no error; anything else that cannot go raises OSError."""
    top = _open_for_removal(dir_fd, name)
    if top is None:
        return
    # (the parent's descriptor, the directory's name in it, its own descriptor, its entries not yet removed);
    # a descriptor is on the stack before its listing, so the `finally` closes it whatever fails
    stack: list[tuple[int, str, int, list[str]]] = [(dir_fd, name, top, [])]
    try:
        stack[0][3].extend(os.listdir(top))
        while stack:
            parent, entry, fd, left = stack[-1]
            if left:
                inner_name = left.pop()
                inner = _open_for_removal(fd, inner_name)
                if inner is not None:  # a directory: emptied before it is removed
                    stack.append((fd, inner_name, inner, []))
                    stack[-1][3].extend(os.listdir(inner))
                continue
            stack.pop()
            os.close(fd)
            with suppress(FileNotFoundError):
                os.rmdir(entry, dir_fd=parent)
    finally:
        for _, _, fd, _ in stack:
            os.close(fd)


def sweep(root: Path, *, live: str | None = None, now: float | None = None) -> int:
    """Remove every leftover under `root` that no live job holds; the number of entries removed. Raises
    `SweepFailed`, once it has tried every entry, when one could not be removed (the class of each error is
    logged, never a name: a parse chooses the names).

    Live, and kept: the job `live` (the one this loop just ran, whose answer the worker is about to read),
    and every directory of the root younger (its ctime, `now` on the wall clock) than `LEFTOVER_AFTER_S`,
    the longest a worker waits for a job: one it is writing, one that is queued, one whose answer it has not
    read yet. A kept directory keeps only its protocol files, each a regular file of a protocol name
    (`PROTOCOL_FILES`). Everything else goes: an entry of the root that is no directory (the worker creates
    directories only), a directory unchanged for longer than a worker waits, and every other entry of a kept
    directory (a parse's copies, a subdirectory only its owner may enter, a FIFO)."""
    now = time.time() if now is None else now
    try:
        root_fd = os.open(root, _DIR_FLAGS)
    except FileNotFoundError:
        return 0
    except OSError as exc:  # a root it may not read is a sweep it cannot do
        log.error("docparse.root_not_swept error=%s", type(exc).__name__)
        raise SweepFailed(1) from None
    removed = failures = 0
    try:
        try:
            entries = os.listdir(root_fd)
        except OSError as exc:
            log.error("docparse.root_not_swept error=%s", type(exc).__name__)
            raise SweepFailed(1) from None
        for name in entries:
            try:
                removed += _sweep_entry(root_fd, name, live=name == live, now=now)
            except OSError as exc:  # this entry stays; the others are still tried
                failures += 1
                log.error("docparse.leftover_not_removed error=%s", type(exc).__name__)
    finally:
        os.close(root_fd)
    if removed:
        log.info("docparse.leftovers_removed count=%d", removed)
    if failures:
        raise SweepFailed(failures)
    return removed


def _sweep_entry(root_fd: int, name: str, *, live: bool, now: float) -> int:
    """`sweep` of one entry of the root; the number of entries removed."""
    handle = _handle(root_fd, name)
    if handle is None:
        return 0
    try:
        st = os.fstat(handle)
        kept = stat.S_ISDIR(st.st_mode) and (live or now - st.st_ctime <= LEFTOVER_AFTER_S)
        job_fd = _listing(handle, st) if kept else None
    finally:
        os.close(handle)
    if job_fd is None:
        _remove_tree(root_fd, name)
        return 1
    removed = 0
    try:
        for inner in os.listdir(job_fd):
            try:
                inner_st = os.stat(inner, dir_fd=job_fd, follow_symlinks=False)
            except FileNotFoundError:  # the worker removed it meanwhile
                continue
            if inner not in PROTOCOL_FILES or not stat.S_ISREG(inner_st.st_mode):
                _remove_tree(job_fd, inner)
                removed += 1
    finally:
        os.close(job_fd)
    return removed


def serve(root: Path, *, stop: threading.Event | None = None) -> None:
    """Run every published job under `root`, scanning every `SCAN_INTERVAL_S`, until `stop` is set.

    The root is swept (`sweep`) before the first job, after every job (that job live), and every
    `IDLE_SWEEP_S` while no job comes. A sweep that fails raises `SweepFailed`: the loop ends at once and
    takes no further job."""
    stop = stop if stop is not None else threading.Event()
    sweep(root)
    swept = time.monotonic()
    log.info("docparse.serving")
    while not stop.is_set():
        for job_dir in _pending(root):
            if stop.is_set():
                break
            try:
                run_job(job_dir)
            except Exception as exc:  # noqa: BLE001 - one job never ends the loop; its class only is logged
                log.error("docparse.job_failed job_id=%s error=%s", job_dir.name, type(exc).__name__)
            sweep(root, live=job_dir.name)
            swept = time.monotonic()
        if time.monotonic() - swept >= IDLE_SWEEP_S:
            sweep(root)
            swept = time.monotonic()
        stop.wait(SCAN_INTERVAL_S)
    log.info("docparse.stopped")


def main() -> None:
    """Serve the root of `JOBHUNTER_PARSE_JOBS_DIR` until SIGTERM or SIGINT; a running job finishes first. A
    failed sweep exits with `SWEEP_FAILED_EXIT`, so compose restarts the container, which ends every process
    in it; one whose sweep still fails at its start is recreated with its volume (the remedy)."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s", stream=sys.stderr
    )
    stop = threading.Event()

    def request_stop(_signum: int, _frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        serve(Path(os.environ.get(ROOT_ENV, str(DEFAULT_ROOT))), stop=stop)
    except SweepFailed as failed:
        log.critical("docparse.sweep_failed failures=%d", failed.failures)
        raise SystemExit(SWEEP_FAILED_EXIT) from None


if __name__ == "__main__":
    main()
