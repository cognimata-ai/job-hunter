"""scripts/e2e_fixtures.py (plan M4 Task 32): the E2E's CV files are deterministic and invented. Two builds
give equal bytes; the committed PDF is those bytes and the committed DOCX holds the same members with the same
contents and metadata (a zlib build of another platform may deflate a member into other bytes); the DOCX holds
`SYNTHETIC_CV` (an invented candidate) and one planted phone line, which the fixture PII scan's phone
patterns find (and nothing else) and the contact stripper misses, so E3 has a span to mark; its only e-mail
address is the invented one of `SYNTHETIC_CV` (`example.org`). The PDF's text is below the low-text
threshold. Needs the `docparse` group (python-docx, pypdf)."""

import importlib.util
import io
import zipfile
from pathlib import Path
from types import ModuleType
from typing import Final

import docx
from jobhunter.graphs.profile.nodes.parse import LOW_TEXT_CHARS
from jobhunter.observability.redact import EMAIL, PHONE
from jobhunter.profile.contacts import strip_contacts
from pypdf import PdfReader

from tests.support.fixture_scan import PHONE_DE, PHONE_MORE
from tests.support.profile import SYNTHETIC_CV

SCRIPT: Final = Path(__file__).resolve().parents[2] / "scripts/e2e_fixtures.py"


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("e2e_fixtures", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def members(package: zipfile.ZipFile) -> list[tuple[str, int, int, bytes]]:
    """Each member's name, attributes, system and contents, in order."""
    return [(i.filename, i.external_attr, i.create_system, package.read(i)) for i in package.infolist()]


def test_the_fixtures_are_deterministic_and_invented() -> None:
    fixtures = load()
    built = {fixtures.CV_DOCX: fixtures.cv_docx(), fixtures.LOW_TEXT_PDF: fixtures.low_text_pdf()}
    assert built == {fixtures.CV_DOCX: fixtures.cv_docx(), fixtures.LOW_TEXT_PDF: fixtures.low_text_pdf()}
    assert fixtures.LOW_TEXT_PDF.read_bytes() == built[fixtures.LOW_TEXT_PDF]
    package = zipfile.ZipFile(io.BytesIO(built[fixtures.CV_DOCX]))
    assert members(zipfile.ZipFile(fixtures.CV_DOCX)) == members(package), (
        "cv.docx is not what the script writes"
    )
    assert {(i.compress_type, i.date_time) for i in package.infolist()} == {
        (zipfile.ZIP_DEFLATED, (1980, 1, 1, 0, 0, 0))
    }
    document = docx.Document(io.BytesIO(built[fixtures.CV_DOCX]))
    assert document.core_properties.author == "Job Hunter E2E"
    text = "\n".join(p.text for p in document.paragraphs)
    planted = f"Telefon: {fixtures.PLANTED_PHONE}"
    assert text.replace(f"{planted}\n", "") == SYNTHETIC_CV
    phones = [m.group(0) for pattern in (PHONE, PHONE_DE, PHONE_MORE) for m in pattern.finditer(text)]
    assert phones == [fixtures.PLANTED_PHONE]
    assert [m.group(0) for m in EMAIL.finditer(text)] == ["erika.musterfrau@example.org"]
    stripped = strip_contacts(text).stripped_text
    assert planted in stripped and "[EMAIL]" in stripped  # the stripper misses the planted number only

    pdf = PdfReader(io.BytesIO(built[fixtures.LOW_TEXT_PDF]))
    extracted = "".join(page.extract_text() for page in pdf.pages)
    assert all(line in extracted for line in fixtures.LOW_TEXT_LINES)
    assert 0 < len(extracted.strip()) < LOW_TEXT_CHARS
