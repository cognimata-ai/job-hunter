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
4. The worker reads the result and removes the directory; docparse never removes anything.

**The child.** `run_job` runs each extraction in a `multiprocessing` child of the `spawn` context, never a
fork (a fork from a multi-threaded process warns on Python 3.12+, and the worker's tests serve this loop
from a thread). The child imports its extractor by module path. It lowers RLIMIT_CPU, RLIMIT_AS and
RLIMIT_CORE before it opens the file, silences logging and warnings (a parser's message can quote the
document), and answers over a one-way `os.pipe` with JSON, never a pickle (it has read a hostile file): the
answer is everything it writes before its end of the pipe closes, and it counts only when the child then
exits with status 0. The loop reads it against one deadline, `SPAWN_MARGIN_S` plus `Limits.wall_s` after the
start, however the bytes arrive, and kills the child when it overruns. A child ended by a limit gives
`parse_limit`: SIGXCPU or SIGKILL, a MemoryError, the deadline (a parse that blocks without using CPU, or one
that stops mid-answer), or an answer over `MAX_ANSWER_BYTES`. Any other failure gives `parse_error`, also a
child that cannot be started at all (EMFILE, ENOMEM): the job is never left without a result.

**The loop.** `serve` scans the root every 0.2 s, the oldest request first, one job at a time (a child may
take 768 MiB of the container's 1 GiB), until `stop` is set; one job's failure never ends it. It opens no
socket (the container has no network at all, `network_mode: none`) and logs only job ids, statuses, codes and
error classes: never a file name, a text or an exception message. `main()` serves the root named by
`JOBHUNTER_PARSE_JOBS_DIR` (default `/data/parse`) until SIGTERM or SIGINT.
"""

import json
import logging
import multiprocessing
import os
import resource
import signal
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


@dataclass(frozen=True)
class Limits:
    """The child's limits: CPU seconds (RLIMIT_CPU), address space in bytes (RLIMIT_AS), and the wall-clock
    seconds, after `SPAWN_MARGIN_S` for its start, within which its whole answer must arrive before the loop
    kills it."""

    cpu_s: int
    as_bytes: int
    wall_s: float = 45.0


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
    """The extraction, in the spawn child: the limits first, then the parse, then one JSON answer. Nothing
    escapes it, so the child never prints a traceback."""
    answer: dict[str, object]
    try:
        logging.disable(logging.CRITICAL)
        warnings.simplefilter("ignore")
        signal.signal(signal.SIGXCPU, signal.SIG_DFL)  # the soft CPU limit ends the child
        _limit(resource.RLIMIT_CORE, 0, 0)
        _limit(resource.RLIMIT_AS, limits.as_bytes, limits.as_bytes)
        _limit(resource.RLIMIT_CPU, limits.cpu_s, limits.cpu_s + 1)  # the hard limit: SIGKILL a second later
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


def _reap(child: multiprocessing.process.BaseProcess, *, kill: bool) -> int | None:
    """The child's exit status, within `EXIT_JOIN_S`: a child still alive then is killed and reaped.

    `Process.join(timeout)` is no bound: it waits for the process sentinel, then calls a blocking
    `waitpid`, and a child can close its end of the sentinel (and of the answer pipe) and stay alive.
    `exitcode` polls with WNOHANG, so the wait here is bounded whatever the child does."""
    if kill:
        child.kill()
    deadline = time.monotonic() + EXIT_JOIN_S
    while child.exitcode is None and time.monotonic() < deadline:
        time.sleep(_REAP_POLL_S)
    if child.exitcode is None:  # alive after its answer or its EOF: an overrun, killed
        child.kill()
        child.join()  # after SIGKILL, waitpid returns as soon as the kernel has ended the process
    exitcode = child.exitcode
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


def serve(root: Path, *, stop: threading.Event | None = None) -> None:
    """Run every published job under `root`, scanning every `SCAN_INTERVAL_S`, until `stop` is set."""
    stop = stop if stop is not None else threading.Event()
    log.info("docparse.serving")
    while not stop.is_set():
        for job_dir in _pending(root):
            if stop.is_set():
                break
            try:
                run_job(job_dir)
            except Exception as exc:  # noqa: BLE001 - one job never ends the loop; its class only is logged
                log.error("docparse.job_failed job_id=%s error=%s", job_dir.name, type(exc).__name__)
        stop.wait(SCAN_INTERVAL_S)
    log.info("docparse.stopped")


def main() -> None:
    """Serve the root of `JOBHUNTER_PARSE_JOBS_DIR` until SIGTERM or SIGINT; a running job finishes first."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s", stream=sys.stderr
    )
    stop = threading.Event()

    def request_stop(_signum: int, _frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    serve(Path(os.environ.get(ROOT_ENV, str(DEFAULT_ROOT))), stop=stop)


if __name__ == "__main__":
    main()
