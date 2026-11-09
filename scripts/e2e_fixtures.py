"""The E2E fixture files (plan M4 Task 32): the CVs the scenario specs upload, from invented data only.

    uv run --group docparse python scripts/e2e_fixtures.py

It writes, under `frontend/e2e/fixtures/` (committed; the CI job uses them as they are):

- `cv.docx` (E3): `tests/support/profile.SYNTHETIC_CV` (an invented candidate) one paragraph per line, with
  the line `Telefon: <PLANTED_PHONE>` after the e-mail line. The planted number is a phone shape that the
  contact stripper misses (`profile.contacts`: a `+` number needs 8 digits, this one has 7) and the fixture
  PII scan finds (`observability.redact.PHONE`), so E3 has a missed span to mark. Built with python-docx,
  its core properties at fixed times, then repacked in member order at the ZIP epoch with fixed attributes,
  each member deflated at `DEFLATE_LEVEL` (the same zlib gives the same bytes; another zlib build may pick
  other deflate blocks for the same member contents, which `tests/unit/test_e2e_fixtures.py` compares);
- `low-text.pdf` (E4): `tests/support/profile.minimal_pdf` of `LOW_TEXT_LINES`, fewer characters than the
  parse's low-text threshold (`graphs.profile.nodes.parse.LOW_TEXT_CHARS`), so its draft fails `low_text`.

Both are deterministic: a second run writes the same bytes. It prints the two paths it wrote.
"""

import io
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

REPO: Final = Path(__file__).resolve().parents[1]
FIXTURES: Final = REPO / "frontend/e2e/fixtures"
CV_DOCX: Final = FIXTURES / "cv.docx"
LOW_TEXT_PDF: Final = FIXTURES / "low-text.pdf"
PLANTED_PHONE: Final = "+49 30 123"
PHONE_LINE: Final = f"Telefon: {PLANTED_PHONE}"
LOW_TEXT_LINES: Final = ("Erika Musterfrau", "Senior Backend-Entwicklerin")
FIXED_TIME: Final = datetime(2026, 11, 1, 9, 0, 0, tzinfo=UTC)
ZIP_EPOCH: Final = (1980, 1, 1, 0, 0, 0)
UNIX: Final = 3  # `ZipInfo.create_system`: fixed, whatever the platform
MEMBER_MODE: Final = 0o100644 << 16
DEFLATE_LEVEL: Final = 9


def _test_support() -> None:
    if str(REPO) not in sys.path:  # `tests` is a package of the repository root, not of scripts/
        sys.path.insert(1, str(REPO))


def cv_lines() -> list[str]:
    """`SYNTHETIC_CV`'s lines with `PHONE_LINE` after its e-mail line."""
    _test_support()
    from tests.support.profile import SYNTHETIC_CV

    lines = SYNTHETIC_CV.split("\n")
    email = next(i for i, line in enumerate(lines) if "@" in line)
    return [*lines[: email + 1], PHONE_LINE, *lines[email + 1 :]]


def _repacked(data: bytes) -> bytes:
    """`data` (a ZIP) with every member deflated at the ZIP epoch, in its order, with fixed attributes."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(out, "w") as target:
        for info in source.infolist():
            member = zipfile.ZipInfo(info.filename, date_time=ZIP_EPOCH)
            member.compress_type = zipfile.ZIP_DEFLATED
            member.create_system = UNIX
            member.external_attr = MEMBER_MODE
            target.writestr(member, source.read(info.filename), compresslevel=DEFLATE_LEVEL)
    return out.getvalue()


def cv_docx() -> bytes:
    """`cv.docx`'s bytes (module docstring)."""
    import docx  # the `docparse` group

    document = docx.Document()
    for line in cv_lines():
        document.add_paragraph(line)
    props = document.core_properties
    props.author = props.last_modified_by = "Job Hunter E2E"
    props.title = "Lebenslauf"
    props.created = props.modified = props.last_printed = FIXED_TIME
    props.revision = 1
    raw = io.BytesIO()
    document.save(raw)
    return _repacked(raw.getvalue())


def low_text_pdf() -> bytes:
    """`low-text.pdf`'s bytes (module docstring)."""
    _test_support()
    from tests.support.profile import minimal_pdf

    return minimal_pdf(list(LOW_TEXT_LINES))


def main() -> int:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for path, data in ((CV_DOCX, cv_docx()), (LOW_TEXT_PDF, low_text_pdf())):
        path.write_bytes(data)
        print(path.relative_to(REPO))
    return 0


if __name__ == "__main__":
    sys.exit(main())
