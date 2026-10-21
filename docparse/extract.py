"""The docparse extractors (spec §12.1, §12.2): the text of a CV file. `docparse.worker.run_job` calls them in
its rlimited child only, never in the loop's own process.

- `extract_pdf`: pypdf, page by page; pdfplumber instead when pypdf yields fewer than 300 characters or more
  than 5% U+FFFD replacement characters (`needs_fallback`). The page count is pypdf's.
- `validate_pdf`: the checks alone, without reading any text. Both refuse an encrypted PDF as `encrypted`
  (any `/Encrypt`, also one an empty password opens: §12.1 accepts only unencrypted files) and one of more
  than `MAX_PAGES` pages as `too_many_pages`, before a page is read.
- `extract_docx`: python-docx, the body's paragraphs and table cells in document order (a merged cell once, a
  nested table in its cell's place); blank paragraphs carry no text and are left out.
- `extract_document`: the one for the MIME type; any other type is refused as `unsupported`.

A refusal is `ExtractionRefused(code)`; every other exception is the parser's own and means `parse_error`.
pypdf, pdfplumber and python-docx (the `docparse` dependency group) are imported inside the functions, so
`docparse.worker` imports in the base environment too (tests/support/profile.py serves a docparse loop there).
"""

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

if TYPE_CHECKING:
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    from pypdf import PdfReader

Method = Literal["pypdf", "pdfplumber", "docx"]
RefusalCode = Literal["encrypted", "too_many_pages", "unsupported"]

PDF_MIME: Final = "application/pdf"
DOCX_MIME: Final = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MAX_PAGES: Final = 20
FALLBACK_BELOW_CHARS: Final = 300
FALLBACK_ABOVE_REPLACEMENT_SHARE: Final = 0.05
_PAGE_SEPARATOR: Final = "\n"


@dataclass(frozen=True)
class Extracted:
    text: str
    method: Method
    page_count: int | None  # None for a DOCX: it has no fixed pages


@dataclass(frozen=True)
class Validated:
    page_count: int


class ExtractionRefused(Exception):
    """The file is of a kind §12.1 does not accept; `code` is the result code."""

    def __init__(self, code: RefusalCode) -> None:
        super().__init__(code)
        self.code: RefusalCode = code


def needs_fallback(text: str) -> bool:
    """Whether pypdf's `text` is too short or too garbled to stand, so pdfplumber reads the file instead."""
    replaced = text.count("\ufffd")
    return len(text) < FALLBACK_BELOW_CHARS or replaced > FALLBACK_ABOVE_REPLACEMENT_SHARE * len(text)


def _checked_reader(path: Path) -> "tuple[PdfReader, int]":
    from pypdf import PdfReader

    reader = PdfReader(path)
    if reader.is_encrypted:
        raise ExtractionRefused("encrypted")
    page_count: int = len(reader.pages)
    if page_count > MAX_PAGES:
        raise ExtractionRefused("too_many_pages")
    return reader, page_count


def validate_pdf(path: Path) -> Validated:
    _, page_count = _checked_reader(path)
    return Validated(page_count)


def extract_pdf(path: Path) -> Extracted:
    reader, page_count = _checked_reader(path)
    text = _PAGE_SEPARATOR.join(page.extract_text() for page in reader.pages)
    if not needs_fallback(text):
        return Extracted(text, "pypdf", page_count)
    import pdfplumber

    with pdfplumber.open(path) as pdf:
        text = _PAGE_SEPARATOR.join(page.extract_text() for page in pdf.pages)
    return Extracted(text, "pdfplumber", page_count)


def _block_lines(blocks: "Iterator[Paragraph | Table]") -> Iterator[str]:
    from docx.text.paragraph import Paragraph

    for block in blocks:
        if not isinstance(block, Paragraph):
            yield from _table_lines(block)
        elif block.text.strip():
            yield block.text


def _table_lines(table: "Table") -> Iterator[str]:
    # `row.cells` repeats a merged cell once per grid column it spans (and per row of a vertical merge); the
    # set holds the cell elements themselves, so each one is read once.
    seen: set[object] = set()
    for row in table.rows:
        for cell in row.cells:
            if cell._tc not in seen:
                seen.add(cell._tc)
                yield from _block_lines(cell.iter_inner_content())


def extract_docx(path: Path) -> Extracted:
    import docx

    document = docx.Document(str(path))
    return Extracted("\n".join(_block_lines(document.iter_inner_content())), "docx", None)


def extract_document(path: Path, mime: str) -> Extracted:
    if mime == PDF_MIME:
        return extract_pdf(path)
    if mime == DOCX_MIME:
        return extract_docx(path)
    raise ExtractionRefused("unsupported")
