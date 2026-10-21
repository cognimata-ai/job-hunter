"""The worker's half of the docparse file protocol (spec §12.2; version 1, set out in `docparse/worker.py`).

The app image does not contain docparse, so the protocol's names are repeated here. `ParseClient(root)` works
on the parse_jobs root, `/data/parse` in compose: a tmpfs of mode 2770 and group 10011 shared with docparse
alone, so a decrypted CV never reaches a disk.

- `extract(data, mime)` creates `<root>/<uuid>/` (0770; the setgid root gives it its group, so docparse can
  enter it, and the setgid bit, so the files inside take that group too), writes `input.pdf` or
  `input.docx` (0660), publishes `request.json` atomically, then polls for `result.json` every `poll_s`
  until `timeout_s`. It answers `ParseOk(text, method, page_count)` or `ParseFailed(code)`: docparse's own
  code, `timeout` when no result came in time, or `parse_error` for a result it cannot trust (below). The
  directory is removed in a `finally`, whatever the outcome, cancellation included.
- `validate(data)` is the same exchange in `validate` mode, for a PDF: encryption and page count, no text,
  so an `ok` is `ParseOk("", "pypdf", page_count)`.
- `cleanup_leftovers()` removes every entry under the root at worker start: what a crashed worker left. A
  missing root removes nothing and returns 0.

**docparse is the sandbox of hostile documents**, and anything its parser child runs can write anything in
the job directory. Its answer is read as untrusted: every file goes through the job directory's own file
descriptor (opened when it is created, so a directory swapped for a symlink later is never followed) and
`_read_regular`, which follows no symlink, never blocks on a FIFO, takes a regular file only and caps its
size: `RESULT_MAX_BYTES` for `result.json`, and for `text.txt` four bytes a character of the result's `chars`
(UTF-8), never more than `TEXT_MAX_BYTES`. Any failure of that, any result that is not version 1 with a known
status, code and method, and a `text.txt` that is not UTF-8 of exactly `chars` characters is
`ParseFailed("parse_error")`; reading the answer never raises. Writing the job (a missing or unwritable root)
does raise: that is the worker's environment, not the document.

The file work runs on the event loop: a job is at most 10 MiB (§12.1) on a tmpfs, and work done in place
cannot outlive the `finally` that removes its directory, as a write in a thread could. Nothing here logs a
file name, a text or a document's bytes: job ids, codes and counts only.
"""

import asyncio
import errno
import json
import os
import shutil
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, TypeIs, get_args

from jobhunter.observability.logging import get_logger

log = get_logger("jobhunter.profile.parse_client")

CvMime = Literal["application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"]
ParseMethod = Literal["pypdf", "pdfplumber", "docx"]
DocparseCode = Literal["encrypted", "too_many_pages", "parse_error", "parse_limit", "unsupported"]
ParseFailureCode = DocparseCode | Literal["timeout"]

PDF_MIME: Final = "application/pdf"
PROTOCOL_VERSION: Final = 1
INPUT_NAMES: Final[dict[str, str]] = {
    "application/pdf": "input.pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "input.docx",
}
REQUEST: Final = "request.json"
TEXT: Final = "text.txt"
RESULT: Final = "result.json"
DIR_MODE: Final = 0o770
FILE_MODE: Final = 0o660
# A result is six short fields; the text is UTF-8, at most four bytes a character.
RESULT_MAX_BYTES: Final = 4096
TEXT_MAX_BYTES: Final = 16 * 2**20
UTF8_MAX_BYTES_PER_CHAR: Final = 4
_REMOVE_ATTEMPTS: Final = 3
_READ_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_DIR_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


@dataclass(frozen=True)
class ParseOk:
    text: str
    method: ParseMethod
    page_count: int | None  # None for a DOCX


@dataclass(frozen=True)
class ParseFailed:
    code: ParseFailureCode


ParseOutcome = ParseOk | ParseFailed


def _is_count(value: object) -> TypeIs[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_method(value: object) -> TypeIs[ParseMethod]:
    return value in get_args(ParseMethod)


def _is_docparse_code(value: object) -> TypeIs[DocparseCode]:
    return value in get_args(DocparseCode)


def _write_new(dir_fd: int, name: str, data: bytes) -> None:
    """A new file `name` of `data` in the job directory, 0660 whatever the umask: docparse reads it through
    the group."""
    fd = os.open(
        name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, FILE_MODE, dir_fd=dir_fd
    )
    with os.fdopen(fd, "wb") as out:
        os.fchmod(out.fileno(), FILE_MODE)
        out.write(data)


def _submit(job_dir: Path, data: bytes, mime: str, mode: str) -> int:
    """Create the job and publish its request; the job directory's descriptor, which the caller closes."""
    input_name = INPUT_NAMES[mime]
    job_dir.mkdir(mode=DIR_MODE)
    dir_fd = os.open(job_dir, _DIR_FLAGS)
    try:
        # The umask narrows mkdir's mode, so it is set again; the setgid bit inherited from the root stays.
        os.fchmod(dir_fd, DIR_MODE | (os.fstat(dir_fd).st_mode & stat.S_ISGID))
        _write_new(dir_fd, input_name, data)
        request = {"v": PROTOCOL_VERSION, "mime": mime, "input": input_name, "mode": mode}
        _write_new(dir_fd, f"{REQUEST}.tmp", json.dumps(request).encode("ascii"))
        os.replace(f"{REQUEST}.tmp", REQUEST, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        os.close(dir_fd)
        raise
    return dir_fd


def _read_regular(dir_fd: int, name: str, max_bytes: int) -> bytes:
    """The bytes of the regular file `name` in the job directory: no symlink is followed, a FIFO or a device
    never blocks, and a file of more than `max_bytes` is refused. Raises OSError for anything else."""
    fd = os.open(name, _READ_FLAGS, dir_fd=dir_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "not a regular file")
        data = bytearray()
        while len(data) <= max_bytes and (chunk := os.read(fd, max_bytes + 1 - len(data))):
            data += chunk
    finally:
        os.close(fd)
    if len(data) > max_bytes:
        raise OSError(errno.EFBIG, "file too large")
    return bytes(data)


def _read_outcome(dir_fd: int, mode: str) -> ParseOutcome | None:
    """docparse's result, or None while there is none yet. Never raises: anything it cannot trust is
    `parse_error`."""
    try:
        raw = _read_regular(dir_fd, RESULT, RESULT_MAX_BYTES)
    except FileNotFoundError:
        return None
    except OSError:
        return ParseFailed("parse_error")
    try:
        result = json.loads(raw)
    except (ValueError, RecursionError):  # not JSON, not UTF-8, or nested past the parser's depth
        return ParseFailed("parse_error")
    if not isinstance(result, dict) or not _is_count(result.get("v")) or result["v"] != PROTOCOL_VERSION:
        return ParseFailed("parse_error")
    code = result.get("code")
    if result.get("status") == "error":
        return ParseFailed(code if _is_docparse_code(code) else "parse_error")
    method, page_count, chars = result.get("method"), result.get("page_count"), result.get("chars")
    if (
        result.get("status") != "ok"
        or code is not None
        or not _is_method(method)
        or not (page_count is None or _is_count(page_count))
        or not _is_count(chars)
    ):
        return ParseFailed("parse_error")
    if mode == "validate":
        return ParseOk("", method, page_count)
    try:
        raw_text = _read_regular(dir_fd, TEXT, min(UTF8_MAX_BYTES_PER_CHAR * chars, TEXT_MAX_BYTES))
        # Bytes, then UTF-8: reading in text mode would turn "\r\n" into "\n" and change the length.
        text = raw_text.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return ParseFailed("parse_error")
    return ParseOk(text, method, page_count) if len(text) == chars else ParseFailed("parse_error")


def _remove(job_dir: Path) -> None:
    # docparse may still be writing when a timeout or a cancellation removes the directory, so a file can
    # appear after rmtree listed it; removal is tried again until the directory is gone. A job directory that
    # docparse swapped for a symlink is unlinked, never followed.
    for _ in range(_REMOVE_ATTEMPTS):
        if job_dir.is_symlink():
            job_dir.unlink(missing_ok=True)
        shutil.rmtree(job_dir, ignore_errors=True)
        if not os.path.lexists(job_dir):
            return
    log.warning("parse_job_dir_not_removed", job_id=job_dir.name)


class ParseClient:
    def __init__(self, root: Path, *, timeout_s: float = 60.0, poll_s: float = 0.2) -> None:
        self.root = root
        self.timeout_s = timeout_s
        self.poll_s = poll_s

    async def extract(self, data: bytes, mime: CvMime) -> ParseOutcome:
        """The text of the CV file `data` of type `mime`, extracted by docparse."""
        return await self._exchange(data, mime, "extract")

    async def validate(self, data: bytes) -> ParseOutcome:
        """docparse's checks of the PDF `data` (encryption, page count), without its text."""
        return await self._exchange(data, PDF_MIME, "validate")

    async def _exchange(self, data: bytes, mime: str, mode: str) -> ParseOutcome:
        job_dir = self.root / uuid.uuid4().hex
        dir_fd: int | None = None
        try:
            dir_fd = _submit(job_dir, data, mime, mode)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.timeout_s
            while (outcome := _read_outcome(dir_fd, mode)) is None:
                if loop.time() >= deadline:
                    log.warning("parse_job_timeout", job_id=job_dir.name, timeout_s=self.timeout_s)
                    return ParseFailed("timeout")
                await asyncio.sleep(self.poll_s)
            return outcome
        finally:
            if dir_fd is not None:
                os.close(dir_fd)
            _remove(job_dir)

    def cleanup_leftovers(self) -> int:
        """Remove every entry under the root (run at worker start, before any job); the number removed."""
        try:
            entries = list(self.root.iterdir())
        except FileNotFoundError:
            return 0
        removed = 0
        for entry in entries:
            try:
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
            except OSError as exc:
                log.warning("parse_leftover_not_removed", job_id=entry.name, error=type(exc).__name__)
            else:
                removed += 1
        if removed:
            log.info("parse_leftovers_removed", count=removed)
        return removed
