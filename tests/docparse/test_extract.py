"""The docparse extractors (spec §12.1, §12.2; Task 8), in process, on synthetic CVs.

Run with `uv run --group docparse pytest -W error -m docparse tests/docparse`.
"""

from pathlib import Path

import pytest

from docparse.extract import (
    MAX_PAGES,
    ExtractionRefused,
    extract_document,
    extract_docx,
    extract_pdf,
    needs_fallback,
    validate_pdf,
)
from docparse.worker import run_job
from tests.docparse.conftest import encrypted_pdf, make_job, result_of
from tests.support.profile import DOCX_MIME, PDF_MIME, docx_bytes, image_only_pdf, minimal_pdf

pytestmark = pytest.mark.docparse

CV_LINES = [
    "Max Mustermann",
    "Softwareentwickler",
    "Berufserfahrung",
    "2019-2024 Senior-Entwickler, Musterfirma GmbH, Musterstadt",
    "Entwicklung von Backend-Diensten in Python und PostgreSQL",
    "2015-2019 Entwickler, Beispiel AG, Beispielstadt",
    "Ausbildung",
    "2010-2015 M.Sc. Informatik, Beispiel-Universität",
    "Sprachen: Deutsch (Muttersprache), Englisch (C1), Französisch (B1)",
    "Kenntnisse: Python, SQL, Docker, Linux, Prüfstände, Größenordnungen",
]


def write(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_a_two_page_text_pdf_yields_its_text_in_page_order_with_pypdf(tmp_path) -> None:
    assert sum(len(line) for line in CV_LINES) >= 300  # enough text: pypdf's answer stands
    pdf = write(tmp_path, "cv.pdf", minimal_pdf(CV_LINES, lines_per_page=5))
    extracted = extract_pdf(pdf)
    assert extracted.method == "pypdf" and extracted.page_count == 2
    assert [line.strip() for line in extracted.text.splitlines()] == CV_LINES
    assert "\ufffd" not in extracted.text


def test_an_image_only_pdf_yields_little_text_and_its_job_is_ok_with_the_count(tmp_path) -> None:
    extracted = extract_pdf(write(tmp_path, "scan.pdf", image_only_pdf()))
    # Under 300 characters from pypdf, so pdfplumber answers instead; it finds no text either.
    assert len(extracted.text) < 300 and extracted.method == "pdfplumber" and extracted.page_count == 1
    job = make_job(tmp_path, image_only_pdf(), PDF_MIME)
    run_job(job)
    result = result_of(job)
    # The low-text decision is the worker's (Task 11): docparse reports the extraction and its size.
    assert result == {
        "v": 1,
        "status": "ok",
        "code": None,
        "method": "pdfplumber",
        "page_count": 1,
        "chars": len((job / "text.txt").read_text(encoding="utf-8")),
    }
    assert isinstance(result["chars"], int) and result["chars"] < 300


def test_an_encrypted_pdf_is_refused_as_encrypted(tmp_path) -> None:
    pdf = write(tmp_path, "locked.pdf", encrypted_pdf(minimal_pdf(CV_LINES)))
    for check in (extract_pdf, validate_pdf):
        with pytest.raises(ExtractionRefused) as refused:
            check(pdf)
        assert refused.value.code == "encrypted"
    job = make_job(tmp_path, pdf.read_bytes(), PDF_MIME)
    run_job(job)
    assert result_of(job)["code"] == "encrypted" and not (job / "text.txt").exists()


def test_a_pdf_over_20_pages_is_refused_as_too_many_pages(tmp_path) -> None:
    assert MAX_PAGES == 20
    long_pdf = write(
        tmp_path, "long.pdf", minimal_pdf([f"Seite {n}" for n in range(1, 22)], lines_per_page=1)
    )
    for check in (extract_pdf, validate_pdf):
        with pytest.raises(ExtractionRefused) as refused:
            check(long_pdf)
        assert refused.value.code == "too_many_pages"
    twenty = write(
        tmp_path, "twenty.pdf", minimal_pdf([f"Seite {n}" for n in range(1, 21)], lines_per_page=1)
    )
    assert validate_pdf(twenty).page_count == 20
    assert extract_pdf(twenty).page_count == 20


def test_validate_reads_the_page_count_without_extracting(tmp_path) -> None:
    validated = validate_pdf(write(tmp_path, "cv.pdf", minimal_pdf(CV_LINES, lines_per_page=4)))
    assert validated.page_count == 3


def test_a_docx_with_a_table_keeps_paragraphs_and_cells_in_document_order(tmp_path) -> None:
    paragraphs = ["Erika Musterfrau", "Sprachen: Deutsch, Englisch", "Hobbys: Schach & Laufen"]
    table = [
        ["2020-2024", "Projektleiterin, Musterfirma GmbH"],
        ["2016-2020", "Ingenieurin <Prüfstand>, Beispiel AG"],
    ]
    docx = write(tmp_path, "cv.docx", docx_bytes(paragraphs, table))
    extracted = extract_docx(docx)
    assert extracted.method == "docx" and extracted.page_count is None
    assert extracted.text.splitlines() == [
        "Erika Musterfrau",
        "2020-2024",
        "Projektleiterin, Musterfirma GmbH",
        "2016-2020",
        "Ingenieurin <Prüfstand>, Beispiel AG",
        "Sprachen: Deutsch, Englisch",
        "Hobbys: Schach & Laufen",
    ]
    assert extract_document(docx, DOCX_MIME) == extracted


def test_a_macro_enabled_document_or_a_broken_pdf_is_a_parse_error(tmp_path) -> None:
    jobs = [
        make_job(tmp_path, docx_bytes(["Max Mustermann"], macro=True), DOCX_MIME, name="docm"),
        make_job(tmp_path, b"%PDF-1.4\n" + b"\x00" * 64, PDF_MIME, name="broken"),
    ]
    for job in jobs:
        run_job(job)
        assert result_of(job)["code"] == "parse_error" and not (job / "text.txt").exists()


def test_extract_document_dispatches_on_the_mime_type(tmp_path) -> None:
    pdf = write(tmp_path, "cv.pdf", minimal_pdf(CV_LINES))
    assert extract_document(pdf, PDF_MIME) == extract_pdf(pdf)
    with pytest.raises(ExtractionRefused) as refused:
        extract_document(pdf, "text/plain")
    assert refused.value.code == "unsupported"


@pytest.mark.parametrize(
    ("text", "fallback"),
    [
        ("", True),
        ("a" * 299, True),
        ("a" * 300, False),
        ("a" * 285 + "\ufffd" * 15, False),  # exactly 5%: not more than 5%
        ("a" * 284 + "\ufffd" * 16, True),
        ("\ufffd" * 400, True),
    ],
)
def test_pdfplumber_takes_over_below_300_characters_or_above_5_percent_replacement_characters(
    text, fallback
) -> None:
    assert needs_fallback(text) is fallback
