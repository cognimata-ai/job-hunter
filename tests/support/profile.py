"""CV test helpers (M2 Task 8; Tasks 9 and 11 extend it): synthetic CV files and an in-process docparse loop.

Every name, company and datum in these files is invented (Max Mustermann, Erika Musterfrau, Musterfirma GmbH);
no real CV and no real person's data is ever used (§12, privacy rules).

- `minimal_pdf(lines)`: a hand-written PDF 1.4 whose pages show `lines` in Helvetica (WinAnsiEncoding), at
  most `lines_per_page` lines per page, so `len(lines)` and `lines_per_page` set the page count;
- `replacement_char_pdf(lines)`: as `minimal_pdf`, with a font whose ToUnicode map reads every "€" of `lines`
  as U+FFFD, as a PDF with a broken text map reads;
- `image_only_pdf()`: a one-page PDF 1.4 holding a single grey image and no text at all (a scanned CV);
- `encrypted_pdf(data)`: a PDF encrypted with a user password (moved here from `tests/docparse/conftest.py`
  in Task 11; it needs the `docparse` group);
- `docx_bytes(paragraphs, table=None, *, macro=False, declared_expansion=None)`: a minimal DOCX built with
  `zipfile`; the `table` rows follow the first paragraph, so a reader that keeps document order puts its cells
  between the paragraphs. `macro=True` makes it a macro-enabled document (`word/vbaProject.bin`, the
  macroEnabled main part type). `declared_expansion=n` makes the ZIP central directory declare `n` expanded
  bytes in all (the declared size of `word/document.xml` grows; its data does not), as a ZIP bomb declares;
- `docx_of_body(body)`: the same package around a hand-written `<w:body>` content (merged cells, nested
  tables), with `paragraph_xml(text)` for its paragraphs.

`docparse_root` is a parse_jobs root (mode 2770, as the tmpfs of §12.2) served by a `docparse.worker.serve`
loop thread for the length of a test; it needs the `docparse` group (`uv run --group docparse pytest`).

Task 11 adds the operation side:
- `nul_char_pdf(lines)`: as `replacement_char_pdf`, with "€" read as U+0000 (a text PostgreSQL refuses);
- `operation_deps(engine, cfg)`: a real `OperationDeps` on `cfg` (the gateway on its fake LLM transport, the
  parse client on `cfg.parse_jobs_dir`, the CV store on `cfg.cv_store_dir`, the profile graphs on a
  checkpointer of their own), closed when the block ends;
- the `op_runner` fixture (`OpRunner`): claims and executes operations in process with those deps, served by
  a docparse loop that counts its jobs (`docparse_jobs`). `run_pending()` runs every due `pending` operation;
  `run_one(op_id, *, pause=None, registry=None)` runs one through `execute_operation(…, deps=…,
  registry=…)`. A pause replaces `afault_point` in every module of `PAUSABLE_MODULES` that exists and binds
  it, for the rest of the test; with a pause and no registry the execution gets a fresh `ExecutionRegistry`,
  so a paused execution and a second one of the same operation never meet `AlreadyExecuting`;
- the `upload` fixture: `await upload(pdf_bytes)` posts a file through the api (`POST /api/profile/cv`, as
  the logged-in client), `await upload("text")` pastes (`POST /api/profile/cv-text`); either answers the new
  draft's id and requires 202;
- `draft_row(engine, draft_id)` and `cv_row(engine, draft_id)`: the draft's row and its `cv_files` row.
"""

import importlib
import importlib.util
import io
import pkgutil
import struct
import threading
import zipfile
from collections.abc import AsyncIterator, Awaitable, Iterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol
from xml.sax.saxutils import escape

import httpx
import pytest
import sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.config import AppConfig
from jobhunter.graphs.checkpointer import open_saver
from jobhunter.graphs.profile.deps import ProfileDeps
from jobhunter.graphs.profile.graph import build_profile_graphs
from jobhunter.llm.cache_monitor import CacheMonitor
from jobhunter.llm.gateway import LLMGateway
from jobhunter.llm.transport import make_http_client
from jobhunter.profile.parse_client import ParseClient
from jobhunter.profile.storage import CvStore
from jobhunter.worker.executions import EXECUTIONS, ExecutionRegistry
from jobhunter.worker.operations import OperationDeps, execute_operation
from sqlalchemy.ext.asyncio import AsyncEngine

import docparse.worker as docparse_worker
from docparse.worker import serve
from tests.support.faults import PausePoint, PausePoints
from tests.support.fixtures import ConfigFactory
from tests.support.llm import FAKE_LLM_DIR

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


def _euro_cmap(code_point: bytes) -> bytes:
    """A ToUnicode map: printable ASCII as itself, and the code of "€" in WinAnsiEncoding (0x80) as the
    hex `code_point`."""
    return (
        b"""/CIDInit /ProcSet findresource begin 12 dict begin begincmap /CMapName /Synthetic def
/CMapType 2 def 1 begincodespacerange <00> <FF> endcodespacerange
1 beginbfrange <20> <7E> <0020> endbfrange 1 beginbfchar <80> <%s> endbfchar
endcmap CMapName currentdict /CMap defineresource pop end end"""
        % code_point
    )


_FFFD_CMAP: Final = _euro_cmap(b"FFFD")
_NUL_CMAP: Final = _euro_cmap(b"0000")


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


def nul_char_pdf(lines: Sequence[str]) -> bytes:
    """As `replacement_char_pdf`, with a ToUnicode map that reads "€" as U+0000: pypdf's text holds NUL."""
    return _text_pdf(lines, len(lines) or 1, [_HELVETICA + b" /ToUnicode 4 0 R >>", _stream(b"", _NUL_CMAP)])


def encrypted_pdf(data: bytes) -> bytes:
    """`data` (a PDF) encrypted with a user password, as a password-protected CV is (the `docparse` group)."""
    from pypdf import PdfWriter

    writer = PdfWriter(clone_from=io.BytesIO(data))
    writer.encrypt(user_password="geheim", algorithm="AES-256")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


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
    paragraphs: Sequence[str],
    table: Sequence[Sequence[str]] | None = None,
    *,
    macro: bool = False,
    declared_expansion: int | None = None,
) -> bytes:
    """A DOCX of `paragraphs`, with `table` (rows of cell texts) after the first paragraph; its central
    directory declares `declared_expansion` expanded bytes in all when that is given."""
    blocks = [paragraph_xml(text) for text in paragraphs]
    if table is not None:
        blocks.insert(min(1, len(blocks)), _table(table))
    data = docx_of_body("".join(blocks), macro=macro)
    return data if declared_expansion is None else _declare_expansion(data, declared_expansion)


_CENTRAL_HEADER: Final = b"PK\x01\x02"
_END_RECORD: Final = b"PK\x05\x06"


def _declare_expansion(data: bytes, total: int) -> bytes:
    """`data` (a ZIP without ZIP64 records) whose central directory declares `total` expanded bytes in all:
    the uncompressed size of `word/document.xml` (offset 24 of its central header) is raised to make up the
    rest. The local header and the data are left as they are."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        others = sum(info.file_size for info in archive.infolist() if info.filename != "word/document.xml")
    declared = total - others
    if not 0 < declared < 2**32:
        raise ValueError("the declared expansion must exceed the other entries and fit in 32 bits")
    out = bytearray(data)
    end = out.rfind(_END_RECORD)
    count, _, offset = struct.unpack_from("<HII", out, end + 10)
    for _ in range(count):
        if out[offset : offset + 4] != _CENTRAL_HEADER:
            raise ValueError("not a central directory header")
        name_len, extra_len, comment_len = struct.unpack_from("<HHH", out, offset + 28)
        if out[offset + 46 : offset + 46 + name_len] == b"word/document.xml":
            struct.pack_into("<I", out, offset + 24, declared)
            return bytes(out)
        offset += 46 + name_len + extra_len + comment_len
    raise ValueError("the DOCX has no word/document.xml")


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


# --- Task 11: the operation runtime in process -----------------------------------------------------------

# The modules whose `afault_point` a pause replaces, where they exist at this point of the plan (Tasks 18 and
# 21 create the last two); `jobhunter.graphs.profile.nodes` stands for each of its node modules.
PAUSABLE_MODULES: Final = (
    "jobhunter.llm.gateway",
    "jobhunter.graphs.profile.nodes",
    "jobhunter.settings.scope_apply",
    "jobhunter.profile.activation",
)
_PARSE_TIMEOUT_S: Final = 60.0
_PARSE_POLL_S: Final = 0.05
# A run of `run_pending` takes at most this many rounds (an operation that stays due would loop forever).
_PENDING_ROUNDS: Final = 50
_DUE = sa.text(
    "SELECT id FROM operations WHERE status = 'pending' AND (not_before IS NULL OR not_before <= now()) "
    "ORDER BY id"
)
_DRAFT = sa.text("SELECT * FROM profile_drafts WHERE id = :d")
_CV = sa.text("SELECT c.* FROM cv_files c JOIN profile_drafts d ON d.cv_file_id = c.id WHERE d.id = :d")


@asynccontextmanager
async def operation_deps(engine: AsyncEngine, cfg: AppConfig) -> AsyncIterator[OperationDeps]:
    """A real `OperationDeps` on `cfg` (module docstring); `cfg` must be in fake LLM mode or carry a key."""
    async with open_saver(cfg.dsn("psycopg")) as saver:
        http_client = make_http_client(cfg)
        try:
            yield OperationDeps(
                cfg=cfg,
                gateway=LLMGateway(engine, cfg, http_client=http_client, cache=CacheMonitor()),
                parse_client=ParseClient(
                    cfg.parse_jobs_dir, timeout_s=_PARSE_TIMEOUT_S, poll_s=_PARSE_POLL_S
                ),
                cv_store=CvStore(cfg.cv_store_dir),
                profile_graphs=build_profile_graphs(saver, ProfileDeps(engine=engine, clock=utcnow)),
                embedder=None,
                clock=utcnow,
            )
        finally:
            await http_client.aclose()


def _pausable_modules() -> list[Any]:
    modules = []
    for name in PAUSABLE_MODULES:
        try:
            module = importlib.import_module(name)
        except ModuleNotFoundError as missing:  # not yet at this point of the plan; nothing else is hidden
            if missing.name is None or not name.startswith(missing.name):
                raise
            continue
        modules.append(module)
        if hasattr(module, "__path__"):  # a package: each of its modules
            for info in pkgutil.iter_modules(module.__path__, f"{name}."):
                modules.append(importlib.import_module(info.name))
    return [module for module in modules if hasattr(module, "afault_point")]


@dataclass
class OpRunner:
    """Operations executed in process with a real `OperationDeps` (module docstring)."""

    engine: AsyncEngine
    deps: OperationDeps
    epoch: int
    monkeypatch: pytest.MonkeyPatch
    docparse_jobs: list[str] = field(default_factory=list)  # the job ids the docparse loop ran

    async def due(self) -> list[int]:
        """The `pending` operations whose `not_before` has passed, oldest first."""
        async with self.engine.connect() as conn:
            return [int(op_id) for op_id in (await conn.execute(_DUE)).scalars()]

    async def run_pending(self) -> list[int]:
        """Execute every due `pending` operation until none is left; the ids executed, in order."""
        executed: list[int] = []
        for _ in range(_PENDING_ROUNDS):
            due = await self.due()
            if not due:
                return executed
            for op_id in due:
                await self.run_one(op_id)
                executed.append(op_id)
        raise RuntimeError("operations stayed due after every round")

    async def run_one(
        self,
        op_id: int,
        *,
        pause: PausePoint | PausePoints | None = None,
        registry: ExecutionRegistry | None = None,
    ) -> None:
        """One execution of operation `op_id` (module docstring)."""
        if pause is not None:
            for module in _pausable_modules():
                self.monkeypatch.setattr(module, "afault_point", pause)
        if registry is None:
            registry = ExecutionRegistry() if pause is not None else EXECUTIONS
        await execute_operation(self.engine, op_id, epoch=self.epoch, deps=self.deps, registry=registry)


@pytest.fixture
async def op_runner(
    engine: AsyncEngine,
    current_epoch: int,
    make_config: ConfigFactory,
    cv_store_dir: Path,
    docparse_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[OpRunner]:
    """An `OpRunner` on the api's CV store, a counting docparse loop and the committed fake LLM recordings."""
    jobs: list[str] = []
    real_run_job = docparse_worker.run_job

    def counting_run_job(job_dir: Path) -> None:
        jobs.append(job_dir.name)
        real_run_job(job_dir)

    # the loop thread looks the function up at every job
    monkeypatch.setattr(docparse_worker, "run_job", counting_run_job)
    importlib.import_module("jobhunter.profile.operations")  # registers the profile handlers and hooks
    cfg = make_config(
        "worker",
        JOBHUNTER_CV_STORE_DIR=str(cv_store_dir),
        JOBHUNTER_PARSE_JOBS_DIR=str(docparse_root),
        JOBHUNTER_FAKE_LLM="1",
        JOBHUNTER_FAKE_LLM_DIR=str(FAKE_LLM_DIR),
    )
    async with operation_deps(engine, cfg) as deps:
        yield OpRunner(
            engine=engine, deps=deps, epoch=current_epoch, monkeypatch=monkeypatch, docparse_jobs=jobs
        )


class Upload(Protocol):
    def __call__(self, content: bytes | str, *, filename: str = ...) -> Awaitable[int]: ...


@pytest.fixture
def upload(logged_in_client: httpx.AsyncClient) -> Upload:
    """`await upload(file_bytes, filename="Lebenslauf.pdf")` or `await upload(pasted_text)`: the new draft."""

    async def post(content: bytes | str, *, filename: str = "Lebenslauf.pdf") -> int:
        if isinstance(content, str):
            r = await logged_in_client.post("/api/profile/cv-text", json={"text": content})
        else:
            r = await logged_in_client.post("/api/profile/cv", files={"file": (filename, content)})
        assert r.status_code == 202, (r.status_code, r.json())
        return int(r.json()["draft_id"])

    return post


async def draft_row(engine: AsyncEngine, draft_id: int) -> sa.Row[Any]:
    """The `profile_drafts` row of `draft_id`."""
    async with engine.connect() as conn:
        return (await conn.execute(_DRAFT, {"d": draft_id})).one()


async def cv_row(engine: AsyncEngine, draft_id: int) -> sa.Row[Any]:
    """The `cv_files` row of draft `draft_id`."""
    async with engine.connect() as conn:
        return (await conn.execute(_CV, {"d": draft_id})).one()
