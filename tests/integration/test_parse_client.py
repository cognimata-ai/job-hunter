"""The worker's parse client against a docparse loop (spec §12.2; Task 8).

`docparse_root` (tests/support/profile.py) serves a tmp parse_jobs root with an in-process
`docparse.worker.serve` thread, so these tests need the `docparse` group: `uv run --group docparse pytest`.
No database is involved.
"""

import asyncio
import json
import os
import stat
import time
from contextlib import suppress
from pathlib import Path
from typing import Final

import pytest
from jobhunter.profile.parse_client import ParseClient, ParseFailed, ParseOk

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


def ok_result(chars: int) -> dict[str, object]:
    return {"v": 1, "status": "ok", "code": None, "method": "pypdf", "page_count": 1, "chars": chars}


@pytest.mark.parametrize(
    ("text", "result", "expected"),
    [
        ("Max Mustermann", json.dumps(ok_result(14)), ParseOk("Max Mustermann", "pypdf", 1)),
        ("Max Muster", json.dumps(ok_result(14)), ParseFailed("parse_error")),  # text.txt incomplete
        (None, json.dumps(ok_result(14)), ParseFailed("parse_error")),  # text.txt missing
        ("Max Mustermann", json.dumps(ok_result(14) | {"v": 2}), ParseFailed("parse_error")),
        ("Max Mustermann", json.dumps(ok_result(14) | {"method": "ocr"}), ParseFailed("parse_error")),
        (
            None,
            json.dumps(ok_result(0) | {"status": "error", "code": "encrypted", "page_count": None}),
            ParseFailed("encrypted"),
        ),
        (None, json.dumps(ok_result(0) | {"status": "error", "code": "melted"}), ParseFailed("parse_error")),
        (None, "{not json", ParseFailed("parse_error")),
    ],
)
async def test_an_answer_the_client_cannot_trust_is_a_parse_error(tmp_path, text, result, expected) -> None:
    root = parse_root(tmp_path)

    async def answer() -> None:  # docparse's half, by hand
        job = await first_request(root)
        (job / "request.json").rename(job / "request.claimed")
        if text is not None:
            (job / "text.txt").write_text(text, encoding="utf-8")
        (job / "result.json").write_text(result)

    responder = asyncio.create_task(answer())
    assert await ParseClient(root, timeout_s=10, poll_s=0.02).extract(b"%PDF-1.4", PDF_MIME) == expected
    await responder
    assert list(root.iterdir()) == []


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
