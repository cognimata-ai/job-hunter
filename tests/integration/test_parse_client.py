"""The worker's parse client against a docparse loop (spec §12.2; Task 8).

`docparse_root` (tests/support/profile.py) serves a tmp parse_jobs root with an in-process
`docparse.worker.serve` thread, so these tests need the `docparse` group: `uv run --group docparse pytest`.
No database is involved.
"""

import asyncio
import inspect
import json
import os
import stat
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Final

import pytest
from jobhunter.profile.parse_client import ParseClient, ParseFailed, ParseOk, ParseOutcome

from docparse import worker
from tests.support.profile import DOCX_MIME, PDF_MIME, docx_bytes, minimal_pdf

pytestmark = pytest.mark.integration

PARSE_GID: Final = 10011  # jobhunter-parse, the group of the parse_jobs tmpfs (§19.1)
WAIT_S: Final = 10.0
CV_LINES = [
    "Erika Musterfrau",
    "Projektleiterin",
    "2020-2024 Projektleiterin, Musterfirma GmbH, Musterstadt",
    "Leitung eines Teams von acht Entwicklerinnen und Entwicklern",
    "2016-2020 Ingenieurin, Beispiel AG, Beispielstadt",
    "2010-2016 Dipl.-Ing. Maschinenbau, Beispiel-Universität",
    "Sprachen: Deutsch (Muttersprache), Englisch (C1), Spanisch (A2)",
    "Kenntnisse: Projektmanagement, Python, Prüfstände, Qualitätssicherung",
]


def parse_root(tmp_path: Path) -> Path:
    """A root as the parse_jobs tmpfs: mode 2770, group 10011 where the test may set it (as root)."""
    root = tmp_path / "parse"
    root.mkdir()
    if os.geteuid() == 0:
        os.chown(root, -1, PARSE_GID)
    root.chmod(0o2770)
    return root


def _wait_for_request(root: Path) -> Path:
    deadline = time.monotonic() + WAIT_S
    while not (jobs := [p.parent for p in root.glob("*/request.json")]):
        assert time.monotonic() < deadline, "no request was published"
        time.sleep(0.01)
    assert len(jobs) == 1
    return jobs[0]


async def first_request(root: Path) -> Path:
    """The one job directory under `root` once its request is published."""
    return await asyncio.to_thread(_wait_for_request, root)


async def test_a_round_trip_returns_the_text_and_removes_the_job_directory(docparse_root) -> None:
    client = ParseClient(docparse_root, timeout_s=30, poll_s=0.05)
    cv_docx = docx_bytes(["Max Mustermann", "Softwareentwickler"], [["2019-2024", "Musterfirma"]])
    pdf, docx = await asyncio.gather(
        client.extract(minimal_pdf(CV_LINES), PDF_MIME), client.extract(cv_docx, DOCX_MIME)
    )
    assert isinstance(pdf, ParseOk) and pdf.method == "pypdf" and pdf.page_count == 1
    assert [line.strip() for line in pdf.text.splitlines()] == CV_LINES
    assert docx == ParseOk("Max Mustermann\n2019-2024\nMusterfirma\nSoftwareentwickler", "docx", None)
    assert list(docparse_root.iterdir()) == []


async def test_validate_answers_the_page_count_or_the_refusal_without_text(docparse_root) -> None:
    client = ParseClient(docparse_root, timeout_s=30, poll_s=0.05)
    three = minimal_pdf(["Max Mustermann"] * 3, lines_per_page=1)
    assert await client.validate(three) == ParseOk("", "pypdf", 3)
    long_pdf = minimal_pdf([f"Seite {n}" for n in range(1, 22)], lines_per_page=1)
    assert await client.validate(long_pdf) == ParseFailed("too_many_pages")
    assert list(docparse_root.iterdir()) == []


async def test_a_failed_parse_is_its_code_and_removes_the_job_directory(docparse_root) -> None:
    client = ParseClient(docparse_root, timeout_s=30, poll_s=0.05)
    macro = docx_bytes(["Max Mustermann"], macro=True)
    assert await client.extract(macro, DOCX_MIME) == ParseFailed("parse_error")
    assert list(docparse_root.iterdir()) == []


async def test_without_an_answer_the_client_times_out_and_removes_the_job_directory(tmp_path) -> None:
    root = parse_root(tmp_path)  # no docparse loop serves it
    client = ParseClient(root, timeout_s=0.3, poll_s=0.05)
    assert await client.extract(minimal_pdf(CV_LINES), PDF_MIME) == ParseFailed("timeout")
    assert list(root.iterdir()) == []


async def test_a_job_is_written_with_its_modes_and_the_root_group_and_removed_when_cancelled(
    tmp_path,
) -> None:
    root = parse_root(tmp_path)
    data = minimal_pdf(CV_LINES)
    umask = os.umask(0o077)  # the modes are set, never left to the umask
    try:
        task = asyncio.create_task(ParseClient(root, timeout_s=30, poll_s=0.05).extract(data, PDF_MIME))
        job = await first_request(root)
        assert sorted(p.name for p in job.iterdir()) == ["input.pdf", "request.json"]
        modes = {
            p.name: stat.S_IMODE(p.stat().st_mode) for p in (job, job / "input.pdf", job / "request.json")
        }
        # 0770, with the setgid bit inherited from the root, so the files inside take the root's group too
        assert modes == {job.name: 0o2770, "input.pdf": 0o660, "request.json": 0o660}
        assert {p.stat().st_gid for p in (job, job / "input.pdf", job / "request.json")} == {
            root.stat().st_gid
        }
        assert (job / "input.pdf").read_bytes() == data
        assert json.loads((job / "request.json").read_text()) == {
            "v": 1,
            "mime": PDF_MIME,
            "input": "input.pdf",
            "mode": "extract",
        }
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        assert task.cancelled() and list(root.iterdir()) == []
    finally:
        os.umask(umask)


def ok_result(chars: int) -> str:
    return json.dumps(
        {"v": 1, "status": "ok", "code": None, "method": "pypdf", "page_count": 1, "chars": chars}
    )


def error_result(code: str) -> str:
    return json.dumps(
        {"v": 1, "status": "error", "code": code, "method": "pypdf", "page_count": None, "chars": 0}
    )


def answer_when_published(root: Path, answer: Callable[[Path], None]) -> None:
    job = _wait_for_request(root)
    (job / "request.json").rename(job / "request.claimed")
    answer(job)


def exchange(root: Path, answer: Callable[[Path], None], *, timeout_s: float = 10.0) -> ParseOutcome:
    """One `extract` on `root`, answered by `answer` (docparse's half, by hand) once the request is published.
    Both run in a daemon thread with its own event loop, so a client that blocks (on a FIFO, say) fails the
    test instead of hanging it."""

    async def run() -> ParseOutcome:
        responder = asyncio.create_task(asyncio.to_thread(answer_when_published, root, answer))
        outcome = await ParseClient(root, timeout_s=timeout_s, poll_s=0.02).extract(b"%PDF-1.4", PDF_MIME)
        await responder
        return outcome

    outcomes: list[ParseOutcome] = []
    errors: list[BaseException] = []

    def target() -> None:
        try:
            outcomes.append(asyncio.run(run()))
        except BaseException as exc:  # noqa: BLE001 - re-raised in the test's own thread below
            errors.append(exc)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(WAIT_S + timeout_s)
    assert not thread.is_alive(), "the client blocked on docparse's answer"
    if errors:
        raise errors[0]
    return outcomes[0]


def files(**contents: str | bytes) -> Callable[[Path], None]:
    """An answer that writes `contents` (file name with "." spelled "_") in order, as docparse would."""

    def answer(job: Path) -> None:
        for name, data in contents.items():
            path = job / name.replace("_", ".")
            path.write_bytes(data.encode() if isinstance(data, str) else data)

    return answer


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (files(text_txt="Max Mustermann", result_json=ok_result(14)), ParseOk("Max Mustermann", "pypdf", 1)),
        (files(text_txt="Max\r\nMuster", result_json=ok_result(11)), ParseOk("Max\r\nMuster", "pypdf", 1)),
        (files(text_txt="Max Muster", result_json=ok_result(14)), ParseFailed("parse_error")),  # incomplete
        (files(result_json=ok_result(14)), ParseFailed("parse_error")),  # text.txt missing
        (files(text_txt=b"Max \xff", result_json=ok_result(5)), ParseFailed("parse_error")),  # not UTF-8
        (
            files(text_txt="x", result_json=ok_result(1).replace('"v": 1', '"v": 2')),
            ParseFailed("parse_error"),
        ),
        (files(text_txt="x", result_json=ok_result(1).replace("pypdf", "ocr")), ParseFailed("parse_error")),
        (files(result_json=error_result("encrypted")), ParseFailed("encrypted")),
        (files(result_json=error_result("melted")), ParseFailed("parse_error")),
        (files(result_json="{not json"), ParseFailed("parse_error")),
        (files(result_json="[" * 3000), ParseFailed("parse_error")),  # nested past the JSON parser's depth
        (files(result_json=ok_result(14) + " " * 5000), ParseFailed("parse_error")),  # over 4 KiB
        (
            files(text_txt="x" * 100, result_json=ok_result(5)),
            ParseFailed("parse_error"),
        ),  # over 4 bytes a char
    ],
    ids=[
        "ok",
        "ok-crlf",
        "incomplete-text",
        "missing-text",
        "text-not-utf8",
        "version-2",
        "unknown-method",
        "docparse-code",
        "unknown-code",
        "not-json",
        "deep-json",
        "result-oversized",
        "text-oversized",
    ],
)
def test_an_answer_the_client_cannot_trust_is_a_parse_error(tmp_path, answer, expected) -> None:
    root = parse_root(tmp_path)
    assert exchange(root, answer) == expected
    assert list(root.iterdir()) == []


SECRET = "the worker's data_key, as it would read it through group 10010"


def hostile(kind: str, outside: Path) -> Callable[[Path], None]:
    """docparse's half as a compromised parser writes it: files that are no regular file of the job."""
    secret = outside / "secret"
    secret.write_text(SECRET)
    (outside / "result.json").write_text(ok_result(len(SECRET)))

    def answer(job: Path) -> None:
        if kind == "text-symlink":  # text.txt names a file outside the job directory
            (job / "text.txt").symlink_to(secret)
            (job / "result.json").write_text(ok_result(len(SECRET)))
        elif kind == "result-symlink":
            (job / "text.txt").write_text(SECRET)
            (job / "result.json").symlink_to(outside / "result.json")
        elif kind == "result-fifo":  # opened for reading, a FIFO blocks until a writer comes
            os.mkfifo(job / "result.json")
        elif kind == "text-fifo":
            os.mkfifo(job / "text.txt")
            (job / "result.json").write_text(ok_result(14))
        elif kind == "result-directory":
            (job / "result.json").mkdir()
        elif kind == "text-over-16-mib":  # a `chars` that would allow any size
            (job / "text.txt").write_bytes(b"x" * (16 * 2**20 + 1))
            (job / "result.json").write_text(ok_result(2**40))

    return answer


@pytest.mark.parametrize(
    "kind",
    ["text-symlink", "result-symlink", "result-fifo", "text-fifo", "result-directory", "text-over-16-mib"],
)
def test_a_hostile_answer_is_a_parse_error_at_once_and_the_job_is_still_removed(tmp_path, kind) -> None:
    root, outside = parse_root(tmp_path), tmp_path / "outside"
    outside.mkdir()
    started = time.monotonic()
    assert exchange(root, hostile(kind, outside)) == ParseFailed("parse_error")
    assert time.monotonic() - started < WAIT_S / 2  # answered at the first look, not at the timeout
    assert list(root.iterdir()) == []
    assert (outside / "secret").read_text() == SECRET  # the removal followed no symlink


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a file whatever its mode")
def test_an_unreadable_result_is_a_parse_error(tmp_path) -> None:
    def answer(job: Path) -> None:
        (job / "text.txt").write_text("Max Mustermann")
        (job / "result.json").write_text(ok_result(14))
        (job / "result.json").chmod(0)

    root = parse_root(tmp_path)
    assert exchange(root, answer) == ParseFailed("parse_error")
    assert list(root.iterdir()) == []


def test_a_job_directory_swapped_for_a_symlink_is_never_read_through(tmp_path) -> None:
    root, elsewhere = parse_root(tmp_path), tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "text.txt").write_text(SECRET)
    (elsewhere / "result.json").write_text(ok_result(len(SECRET)))

    def answer(job: Path) -> None:
        job.rename(job.with_name(job.name + ".moved"))
        job.symlink_to(elsewhere, target_is_directory=True)

    # The client reads through the descriptor of the directory it made, which never gets a result.
    assert exchange(root, answer, timeout_s=0.5) == ParseFailed("timeout")
    assert [p.name.endswith(".moved") for p in root.iterdir()] == [True]  # only the moved directory is left
    assert (elsewhere / "text.txt").read_text() == SECRET


def test_the_client_waits_longer_than_docparse_takes_for_one_job() -> None:
    """A parse that docparse ends at its deadline answers parse_limit before the client's default timeout."""
    client_timeout = inspect.signature(ParseClient).parameters["timeout_s"].default
    assert worker.SPAWN_MARGIN_S + worker.DEFAULT_LIMITS.wall_s + worker.EXIT_JOIN_S < client_timeout


def stale_job(root: Path, name: str, files: dict[str, bytes]) -> None:
    (root / name).mkdir()
    for file, data in files.items():
        (root / name / file).write_bytes(data)


def test_cleanup_leftovers_removes_every_stale_job_directory(tmp_path) -> None:
    root = parse_root(tmp_path)
    stale_job(root, "a", {"input.pdf": b"%PDF-1.4", "request.json": b"{}"})
    stale_job(root, "b", {"input.docx": b"PK\x03\x04", "request.claimed": b"{}", "text.txt": b"Max"})
    stale_job(root, "c", {})
    assert ParseClient(root).cleanup_leftovers() == 3
    assert root.is_dir() and list(root.iterdir()) == []


def test_cleanup_leftovers_on_a_missing_root_removes_nothing(tmp_path) -> None:
    assert ParseClient(tmp_path / "missing").cleanup_leftovers() == 0
    assert not (tmp_path / "missing").exists()
