"""CV test helpers (M2 Task 8; Tasks 9 and 11 extend it): synthetic CV files and an in-process docparse loop.

Every name, company and datum in these files is invented (Max Mustermann, Erika Musterfrau, Musterfirma GmbH);
no real CV and no real person's data is ever used (§12, privacy rules).

- `minimal_pdf(lines)`: a hand-written PDF 1.4 whose pages show `lines` in Helvetica (WinAnsiEncoding), at
  most `lines_per_page` lines per page, so `len(lines)` and `lines_per_page` set the page count;
- `replacement_char_pdf(lines)`: as `minimal_pdf`, with a font whose ToUnicode map reads every "€" of `lines`
  as U+FFFD, as a PDF with a broken text map reads;
- `image_only_pdf()`: a one-page PDF 1.4 holding a single grey image and no text at all (a scanned CV);
- `docx_bytes(paragraphs, table=None, *, macro=False)`: a minimal DOCX built with `zipfile`; the `table` rows
  follow the first paragraph, so a reader that keeps document order puts its cells between the paragraphs.
  `macro=True` makes it a macro-enabled document (`word/vbaProject.bin`, the macroEnabled main part type);
- `docx_of_body(body)`: the same package around a hand-written `<w:body>` content (merged cells, nested
  tables), with `paragraph_xml(text)` for its paragraphs.

`docparse_root` is a parse_jobs root (mode 2770, as the tmpfs of §12.2) served by a `docparse.worker.serve`
loop thread for the length of a test; it needs the `docparse` group (`uv run --group docparse pytest`).
"""

import importlib.util
import io
import threading
import zipfile
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Final
from xml.sax.saxutils import escape

import pytest

from docparse.worker import serve

PDF_MIME: Final = "application/pdf"
DOCX_MIME: Final = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
# A4 in points; the text starts at the top margin and moves down by the leading.
_PAGE_BOX: Final = b"[0 0 595 842]"
_LEADING: Final = 14
_TOP: Final = 786
_LEFT: Final = 56
_LOOP_JOIN_S: Final = 10.0


def _pdf_string(text: str) -> bytes:
    """`text` as a PDF literal string in WinAnsiEncoding (cp1252): `\\`, `(` and `)` escaped, bytes above
    0x7E as octal escapes. A character outside cp1252 raises `UnicodeEncodeError`."""
    out = bytearray(b"(")
    for byte in text.encode("cp1252"):
        if byte in b"\\()":
            out += b"\\" + bytes([byte])
        elif 0x20 <= byte <= 0x7E:
            out.append(byte)
        else:
            out += b"\\%03o" % byte
    return bytes(out + b")")


def _stream(entries: bytes, data: bytes) -> bytes:
    return b"<< %s /Length %d >>\nstream\n%s\nendstream" % (entries, len(data), data)


def _pdf(objects: Sequence[bytes]) -> bytes:
    """A PDF 1.4 file whose object `n` (from 1) is `objects[n - 1]` and whose root is object 1, with an exact
    cross-reference table."""
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def _pages_pdf(contents: Sequence[bytes], resources: bytes, extra: Sequence[bytes] = ()) -> bytes:
    """Catalog (1), page tree (2), the `extra` objects (3…), then a page and its content stream per entry of
    `contents`."""
    first_page = 3 + len(extra)
    kids = b" ".join(b"%d 0 R" % (first_page + 2 * i) for i in range(len(contents)))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(contents)),
        *extra,
    ]
    for i, content in enumerate(contents):
        page = first_page + 2 * i
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox %s /Resources %s /Contents %d 0 R >>"
            % (_PAGE_BOX, resources, page + 1)
        )
        objects.append(_stream(b"", content))
    return _pdf(objects)


_HELVETICA: Final = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding"
# Printable ASCII as itself, and the code of "€" in WinAnsiEncoding (0x80) as U+FFFD.
_FFFD_CMAP: Final = b"""/CIDInit /ProcSet findresource begin 12 dict begin begincmap /CMapName /Synthetic def
/CMapType 2 def 1 begincodespacerange <00> <FF> endcodespacerange
1 beginbfrange <20> <7E> <0020> endbfrange 1 beginbfchar <80> <FFFD> endbfchar
endcmap CMapName currentdict /CMap defineresource pop end end"""


def _text_pdf(lines: Sequence[str], lines_per_page: int, fonts: Sequence[bytes]) -> bytes:
    chunks = [lines[i : i + lines_per_page] for i in range(0, len(lines), lines_per_page)] or [[]]
    contents = []
    for chunk in chunks:
        shown = b" T* ".join(_pdf_string(line) + b" Tj" for line in chunk)
        contents.append(b"BT /F1 11 Tf %d TL %d %d Td %s ET" % (_LEADING, _LEFT, _TOP, shown))
    return _pages_pdf(contents, b"<< /Font << /F1 3 0 R >> >>", fonts)


def minimal_pdf(lines: Sequence[str], *, lines_per_page: int = 50) -> bytes:
    """A text PDF of `lines` (cp1252 text), `lines_per_page` to a page; no lines give one empty page."""
    return _text_pdf(lines, lines_per_page, [_HELVETICA + b" >>"])


def replacement_char_pdf(lines: Sequence[str]) -> bytes:
    """A one-page text PDF of `lines` (ASCII and "€") whose ToUnicode map (object 4) reads "€" as U+FFFD."""
    return _text_pdf(lines, len(lines) or 1, [_HELVETICA + b" /ToUnicode 4 0 R >>", _stream(b"", _FFFD_CMAP)])


def image_only_pdf() -> bytes:
    """One page with a 16x16 grey gradient image drawn over most of it, and no text object at all."""
    pixels = bytes((x * 16 + y) % 256 for y in range(16) for x in range(16))
    entries = (
        b"/Type /XObject /Subtype /Image /Width 16 /Height 16 /ColorSpace /DeviceGray /BitsPerComponent 8"
    )
    image = _stream(entries, pixels)
    return _pages_pdf([b"q 480 0 0 480 57 300 cm /Im1 Do Q"], b"<< /XObject << /Im1 3 0 R >> >>", [image])


_W: Final = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_REL: Final = "http://schemas.openxmlformats.org/package/2006/relationships"
_MAIN_TYPE: Final = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
_MACRO_MAIN_TYPE: Final = "application/vnd.ms-word.document.macroEnabled.main+xml"


def paragraph_xml(text: str) -> str:
    """A `<w:p>` of `text`, for `docx_of_body`."""
    return f'<w:p><w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>'


def _table(rows: Sequence[Sequence[str]]) -> str:
    columns = max((len(row) for row in rows), default=0)
    grid = "".join("<w:gridCol/>" for _ in range(columns))
    body = "".join(
        "<w:tr>" + "".join(f"<w:tc>{paragraph_xml(cell)}</w:tc>" for cell in row) + "</w:tr>" for row in rows
    )
    return f"<w:tbl><w:tblPr/><w:tblGrid>{grid}</w:tblGrid>{body}</w:tbl>"


def docx_bytes(
    paragraphs: Sequence[str], table: Sequence[Sequence[str]] | None = None, *, macro: bool = False
) -> bytes:
    """A DOCX of `paragraphs`, with `table` (rows of cell texts) after the first paragraph."""
    blocks = [paragraph_xml(text) for text in paragraphs]
    if table is not None:
        blocks.insert(min(1, len(blocks)), _table(table))
    return docx_of_body("".join(blocks), macro=macro)


def docx_of_body(body: str, *, macro: bool = False) -> bytes:
    """A DOCX whose `<w:body>` holds `body` (WordprocessingML, prefix `w`), then the section properties."""
    document = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:document xmlns:w="{_W}"><w:body>'
        + body
        + "<w:sectPr/></w:body></w:document>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        + ('<Default Extension="bin" ContentType="application/vnd.ms-office.vbaProject"/>' if macro else "")
        + '<Override PartName="/word/document.xml" '
        f'ContentType="{_MACRO_MAIN_TYPE if macro else _MAIN_TYPE}"/></Types>'
    )
    package_rels = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="{_REL}">'
        '<Relationship Id="rId1" Target="word/document.xml" Type="http://schemas.openxmlformats.org/'
        'officeDocument/2006/relationships/officeDocument"/></Relationships>'
    )
    document_rels = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Relationships xmlns="{_REL}">'
        + (
            '<Relationship Id="rId1" Target="vbaProject.bin" '
            'Type="http://schemas.microsoft.com/office/2006/relationships/vbaProject"/>'
            if macro
            else ""
        )
        + "</Relationships>"
    )
    parts = {
        "[Content_Types].xml": content_types.encode(),
        "_rels/.rels": package_rels.encode(),
        "word/document.xml": document.encode(),
        "word/_rels/document.xml.rels": document_rels.encode(),
    }
    if macro:
        parts["word/vbaProject.bin"] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(504)  # an OLE header
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in parts.items():
            archive.writestr(zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0)), data)
    return buffer.getvalue()


@pytest.fixture
def docparse_root(tmp_path: Path) -> Iterator[Path]:
    """A parse_jobs root (mode 2770: per-job directories inherit its group) served by a docparse loop
    thread; the loop stops, and its thread is joined, after the test."""
    assert importlib.util.find_spec("pypdf") is not None, (
        "the docparse loop extracts with the `docparse` group: run `uv run --group docparse pytest`"
    )
    root = tmp_path / "parse"
    root.mkdir()
    root.chmod(0o2770)
    stop = threading.Event()
    loop = threading.Thread(
        target=serve, args=(root,), kwargs={"stop": stop}, name="docparse-loop", daemon=True
    )
    loop.start()
    try:
        yield root
    finally:
        stop.set()
        loop.join(_LOOP_JOIN_S)
    assert not loop.is_alive(), "the docparse loop did not stop"
