"""The upload checks (spec §12.1), the streaming multipart reader and the encrypted CV store (M2 Task 9).

Every name and datum here is invented (Erika Musterfrau, Max Mustermann, Musterfirma GmbH).
"""

import hashlib
import io
import json
import os
import re
import stat
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import get_args

import pytest
from jobhunter import crypto
from jobhunter.api.errors import Problem
from jobhunter.crypto import DecryptError
from jobhunter.profile import upload
from jobhunter.profile.storage import CvStore
from jobhunter.profile.upload import UploadRejected, check_file, check_paste
from starlette.requests import Request
from starlette.types import Message

from tests.support.profile import DOCX_MIME, PDF_MIME, docx_bytes, minimal_pdf

KEY, OLD_KEY = os.urandom(32), os.urandom(32)
CV_LINES = ["Erika Musterfrau", "Softwareentwicklerin bei Musterfirma GmbH", "Python, PostgreSQL"]
BOUNDARY = "jh-test-boundary-7f3a"


def reason_of(head: bytes, data: bytes, filename: str) -> str:
    with pytest.raises(UploadRejected) as exc:
        check_file(head, data, filename)
    return exc.value.reason


def checked(data: bytes, filename: str) -> str:
    return check_file(data[:1024], data, filename)


def rejected(data: bytes, filename: str) -> str:
    return reason_of(data[:1024], data, filename)


def zip_of(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def test_every_reason_has_a_fixed_message() -> None:
    assert set(upload.REJECT_MESSAGES) == set(get_args(upload.RejectReason))


def test_the_limits_are_the_spec_values() -> None:
    assert upload.MAX_UPLOAD_BYTES == 10_485_760
    assert upload.MAX_DOCX_EXPANDED == 50 * 2**20
    assert upload.MAX_DOCX_ENTRIES == 1000
    assert (upload.PASTE_MIN, upload.PASTE_MAX) == (300, 50_000)


# --- check_file ------------------------------------------------------------------------------------------


def test_a_pdf_and_a_docx_are_detected_by_their_content() -> None:
    assert checked(minimal_pdf(CV_LINES), "Lebenslauf.pdf") == PDF_MIME
    assert checked(minimal_pdf(CV_LINES), "C:\\fakepath\\CV.PDF") == PDF_MIME  # the extension in any case
    assert checked(docx_bytes(CV_LINES, [["2020", "Musterfirma GmbH"]]), "Lebenslauf.docx") == DOCX_MIME


def test_the_pdf_magic_may_start_anywhere_in_the_first_1024_bytes() -> None:
    pdf = minimal_pdf(CV_LINES)
    assert checked(b"\x00" * 1019 + pdf, "cv.pdf") == PDF_MIME  # `%PDF-` ends at byte 1024
    assert rejected(b"\x00" * 1020 + pdf, "cv.pdf") == "unknown_type"  # it would end at byte 1025


def test_a_file_over_the_limit_is_too_large_before_anything_else() -> None:
    big = b"%PDF-1.4\n" + b"0" * (upload.MAX_UPLOAD_BYTES - 9)
    assert checked(big, "big.pdf") == PDF_MIME  # exactly the limit
    assert rejected(big + b"0", "big.pdf") == "too_large"
    assert rejected(b"MZ" + bytes(upload.MAX_UPLOAD_BYTES), "setup.exe") == "too_large"


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"MZ\x90\x00" + bytes(2048),  # a Windows executable
        b"\x7fELF" + bytes(64),
        b"Erika Musterfrau\nSoftwareentwicklerin",
        b"PK\x05\x06" + bytes(18),  # an empty ZIP: no local file header
    ],
)
def test_content_without_a_pdf_or_zip_signature_is_an_unknown_type(data: bytes) -> None:
    assert rejected(data, "cv.pdf") == "unknown_type"


@pytest.mark.parametrize(
    ("data", "filename"),
    [
        (minimal_pdf(CV_LINES), "cv.docx"),
        (minimal_pdf(CV_LINES), "cv.pdf.exe"),
        (minimal_pdf(CV_LINES), "cv"),
        (minimal_pdf(CV_LINES), ""),
        (docx_bytes(CV_LINES), "cv.pdf"),
        (docx_bytes(CV_LINES), "cv.doc"),
        (docx_bytes(CV_LINES), "cv.zip"),
    ],
)
def test_the_extension_must_agree_with_the_detected_type(data: bytes, filename: str) -> None:
    assert rejected(data, filename) == "extension_mismatch"


def test_the_type_is_detected_from_head_and_the_zip_read_from_data() -> None:
    pdf = minimal_pdf(CV_LINES)
    assert check_file(pdf[:1024], pdf, "cv.pdf") == PDF_MIME
    assert reason_of(b"MZ", pdf, "cv.pdf") == "unknown_type"


@pytest.mark.parametrize(
    "missing", ["[Content_Types].xml", "word/document.xml"], ids=["content-types", "document"]
)
def test_a_docx_without_its_required_parts_is_a_structure_failure(missing: str) -> None:
    entries = {"[Content_Types].xml": b"<Types/>", "_rels/.rels": b"<R/>", "word/document.xml": b"<w:d/>"}
    del entries[missing]
    assert rejected(zip_of(entries), "cv.docx") == "docx_structure"


def test_the_required_part_names_are_compared_without_case() -> None:
    entries = {"[content_types].xml": b"<Types/>", "Word/Document.xml": b"<w:d/>"}
    assert checked(zip_of(entries), "cv.docx") == DOCX_MIME


def test_a_zip_signature_over_an_unreadable_archive_is_a_structure_failure() -> None:
    assert rejected(b"PK\x03\x04" + os.urandom(4096), "cv.docx") == "docx_structure"
    valid = docx_bytes(CV_LINES)
    assert rejected(valid[: len(valid) - 30], "cv.docx") == "docx_structure"  # the end record cut off


def test_a_macro_enabled_docx_is_refused() -> None:
    assert rejected(docx_bytes(CV_LINES, macro=True), "cv.docx") == "docx_macro"


@pytest.mark.parametrize("name", ["word/vbaProject.bin", "customXml/VBAPROJECT.BIN", "vbaProject.bin"])
def test_any_vba_project_makes_a_docx_macro_enabled(name: str) -> None:
    entries = {"[Content_Types].xml": b"<Types/>", "word/document.xml": b"<w:d/>", name: b"\xd0\xcf\x11\xe0"}
    assert rejected(zip_of(entries), "cv.docx") == "docx_macro"


def test_a_declared_expansion_over_50_mib_is_refused_from_the_central_directory() -> None:
    bomb = docx_bytes(["x"], declared_expansion=60 * 2**20)
    with zipfile.ZipFile(io.BytesIO(bomb)) as archive:  # the helper declares what it says, in a small file
        assert sum(info.file_size for info in archive.infolist()) == 60 * 2**20 and len(bomb) < 4096
    assert rejected(bomb, "bomb.docx") == "docx_expansion"
    assert rejected(docx_bytes(["x"], declared_expansion=upload.MAX_DOCX_EXPANDED + 1), "b.docx") == (
        "docx_expansion"
    )
    assert checked(docx_bytes(["x"], declared_expansion=upload.MAX_DOCX_EXPANDED), "b.docx") == DOCX_MIME


def test_more_than_1000_entries_is_an_expansion_failure() -> None:
    required = {"[Content_Types].xml": b"<Types/>", "word/document.xml": b"<w:d/>"}
    padding = {f"word/media/p{i}.bin": b"" for i in range(upload.MAX_DOCX_ENTRIES - len(required))}
    assert checked(zip_of(required | padding), "cv.docx") == DOCX_MIME  # exactly 1,000
    assert rejected(zip_of(required | padding | {"word/media/one-more.bin": b""}), "cv.docx") == (
        "docx_expansion"
    )


def test_too_many_central_records_are_refused_before_zipfile_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every record `zipfile` would build carries the central-directory signature, so counting it bounds
    that work: past `CENTRAL_RECORDS_MAX` the archive is refused without being parsed."""
    required = {"[Content_Types].xml": b"<Types/>", "word/document.xml": b"<w:d/>"}
    padding = {f"p{i}": b"" for i in range(upload.CENTRAL_RECORDS_MAX - len(required) + 1)}
    many = zip_of(required | padding)
    assert many.count(b"PK\x01\x02") == upload.CENTRAL_RECORDS_MAX + 1

    def no_parse(*args: object, **kwargs: object) -> None:
        raise AssertionError("zipfile.ZipFile was built for an archive the count refuses")

    monkeypatch.setattr(zipfile, "ZipFile", no_parse)  # the module `upload` reads it from at call time
    assert rejected(many, "cv.docx") == "docx_expansion"


def test_a_stored_embedded_package_does_not_trip_the_record_count() -> None:
    """A workbook embedded uncompressed brings its own central directory: its records count, but the four-fold
    bound leaves room for it, and the DOCX itself has four entries."""
    workbook = zip_of({f"xl/worksheets/s{i}.xml": b"<w/>" for i in range(1500)})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", b"<Types/>")
        archive.writestr("word/document.xml", b"<w:d/>")
        archive.writestr("word/embeddings/Workbook1.xlsx", workbook, compress_type=zipfile.ZIP_STORED)
    docx = buffer.getvalue()
    assert upload.MAX_DOCX_ENTRIES < docx.count(b"PK\x01\x02") <= upload.CENTRAL_RECORDS_MAX
    assert checked(docx, "cv.docx") == DOCX_MIME


# --- check_paste -----------------------------------------------------------------------------------------


def paste_reason(text: str) -> str:
    with pytest.raises(UploadRejected) as exc:
        check_paste(text)
    return exc.value.reason


def test_pasted_text_is_300_to_50000_characters() -> None:
    assert check_paste("ä" * 300) == "ä" * 300  # characters, not bytes
    assert check_paste("x" * 50_000) == "x" * 50_000
    assert paste_reason("x" * 299) == "too_short"
    assert paste_reason("") == "too_short"
    assert paste_reason("x" * 50_001) == "too_long"


def test_pasted_text_that_is_no_utf8_is_refused() -> None:
    assert paste_reason("Erika Musterfrau " * 20 + "\ud800") == "not_utf8"  # a lone surrogate


def test_a_paste_is_answered_without_its_nul_characters() -> None:
    """Task 11 (Task 9 carry): PostgreSQL's `text` refuses U+0000, so the paste is stored without it; the
    length is that of the text without them."""
    text = "Erika Musterfrau\x00 Entwicklerin " * 12
    assert check_paste(text) == text.replace("\x00", "")
    assert paste_reason("x" * 299 + "\x00") == "too_short"
    assert paste_reason("\x00" * 400) == "too_short"


# --- read_multipart_limited ------------------------------------------------------------------------------


@dataclass
class Pulled:
    """What a counting ASGI `receive` handed out."""

    bytes: int = 0
    messages: int = 0
    log: list[int] = field(default_factory=list)


def part(name: str, value: bytes, filename: str | None = None) -> bytes:
    disposition = f'form-data; name="{name}"' + ("" if filename is None else f'; filename="{filename}"')
    headers = f"Content-Disposition: {disposition}\r\n"
    if filename is not None:
        headers += "Content-Type: application/octet-stream\r\n"
    return f"--{BOUNDARY}\r\n{headers}\r\n".encode() + value + b"\r\n"


def form(*parts: bytes) -> bytes:
    return b"".join(parts) + f"--{BOUNDARY}--\r\n".encode()


def counting_request(
    messages: Sequence[bytes],
    *,
    content_type: str | None = None,
    headers: Sequence[tuple[bytes, bytes]] = (),
) -> tuple[Request, Pulled]:
    """A request whose body arrives as `messages`, one ASGI message each; `Pulled` counts what was taken."""
    pulled = Pulled()

    async def receive() -> Message:
        index = pulled.messages
        pulled.messages += 1
        if index >= len(messages):
            return {"type": "http.disconnect"}
        pulled.bytes += len(messages[index])
        pulled.log.append(len(messages[index]))
        return {"type": "http.request", "body": messages[index], "more_body": index < len(messages) - 1}

    ctype = f"multipart/form-data; boundary={BOUNDARY}" if content_type is None else content_type
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/profile/cv",
        "headers": [(b"content-type", ctype.encode("latin-1")), *headers],
    }
    return Request(scope, receive), pulled


def chunked(body: bytes, size: int = 65536) -> list[bytes]:
    return [body[i : i + size] for i in range(0, len(body), size)] or [b""]


async def read(
    body: bytes, *, limit: int = upload.MAX_UPLOAD_BYTES, content_type: str | None = None
) -> upload.UploadedFile:
    request, _ = counting_request(chunked(body), content_type=content_type)
    return await upload.read_multipart_limited(request, limit=limit)


async def problem_of(
    body: bytes, *, limit: int = upload.MAX_UPLOAD_BYTES, content_type: str | None = None
) -> Problem:
    with pytest.raises(Problem) as exc:
        await read(body, limit=limit, content_type=content_type)
    return exc.value


def summary(problem: Problem) -> tuple[int, str, list[tuple[str, str]]]:
    return problem.status, problem.code, [(e.field, e.code) for e in problem.errors or []]


async def test_streaming_limit_stops_reading() -> None:
    """A counting ASGI receive: past the limit the reader stops, having pulled at most the limit and one
    chunk of the file's bytes (the first message is the multipart framing before them)."""
    limit, chunk = 4096, 512
    framing = part("file", b"", "big.pdf").removesuffix(b"\r\n")
    data = b"%PDF-1.4\n" + b"0" * (50 * limit)
    messages = [framing, *chunked(data + f"\r\n--{BOUNDARY}--\r\n".encode(), chunk)]
    request, pulled = counting_request(messages)
    with pytest.raises(Problem) as exc:
        await upload.read_multipart_limited(request, limit=limit)
    assert summary(exc.value) == (413, "upload_rejected", [("file", "too_large")])
    assert pulled.bytes - len(framing) <= limit + chunk
    assert pulled.messages < len(messages) // 10  # it stopped early: the rest was never asked for


async def test_a_file_of_exactly_the_limit_is_read_whole() -> None:
    limit, chunk = 4096, 512
    data = b"%PDF-1.4\n" + b"0" * (limit - 9)
    request, _ = counting_request(chunked(form(part("file", data, "cv.pdf")), chunk))
    uploaded = await upload.read_multipart_limited(request, limit=limit)
    assert (uploaded.filename, uploaded.data, uploaded.native_pdf_opt_in) == ("cv.pdf", data, False)
    assert summary(await problem_of(form(part("file", data + b"0", "cv.pdf")), limit=limit)) == (
        413,
        "upload_rejected",
        [("file", "too_large")],
    )


async def test_the_whole_body_is_bounded_not_only_the_file() -> None:
    """A part the reader ignores is not kept, but neither is it read without end."""
    limit, chunk = 4096, 512
    body = form(part("other", b"y" * (upload.MULTIPART_OVERHEAD_BYTES + 2 * limit)), part("file", b"%PDF-"))
    request, pulled = counting_request(chunked(body, chunk))
    with pytest.raises(Problem) as exc:
        await upload.read_multipart_limited(request, limit=limit)
    assert summary(exc.value) == (413, "upload_rejected", [("file", "too_large")])
    assert pulled.bytes <= limit + upload.MULTIPART_OVERHEAD_BYTES + chunk


@pytest.mark.parametrize(
    ("value", "expected"), [(b"true", True), (b"1", True), (b"on", True), (b"false", False), (b"0", False)]
)
async def test_the_opt_in_field_is_read_as_a_boolean(value: bytes, expected: bool) -> None:
    pdf = minimal_pdf(CV_LINES)
    uploaded = await read(form(part("native_pdf_opt_in", value), part("file", pdf, "Lebenslauf Erika.pdf")))
    assert (uploaded.filename, uploaded.data, uploaded.native_pdf_opt_in) == (
        "Lebenslauf Erika.pdf",
        pdf,
        expected,
    )


async def test_a_utf8_file_name_and_unknown_parts_are_read() -> None:
    pdf = minimal_pdf(CV_LINES)
    body = form(part("comment", b"ignored"), part("file", pdf, "Lebenslauf-Müller.pdf"))
    uploaded = await read(body)
    assert uploaded.filename == "Lebenslauf-Müller.pdf" and uploaded.data == pdf


async def test_the_uploaded_file_hides_its_name_and_content_from_repr() -> None:
    uploaded = await read(form(part("file", b"%PDF-1.4 Erika", "Erika-Musterfrau.pdf")))
    assert "Erika" not in repr(uploaded)


@pytest.mark.parametrize(
    ("body", "content_type", "expected"),
    [
        (form(part("native_pdf_opt_in", b"true")), None, [("file", "missing")]),
        (form(), None, [("file", "missing")]),
        (
            form(part("file", b"%PDF-1", "a.pdf"), part("file", b"%PDF-2", "b.pdf")),
            None,
            [("file", "duplicate")],
        ),
        (
            form(part("native_pdf_opt_in", b"1"), part("native_pdf_opt_in", b"0"), part("file", b"%PDF-")),
            None,
            [("native_pdf_opt_in", "duplicate")],
        ),
        (
            form(part("native_pdf_opt_in", b"maybe"), part("file", b"%PDF-")),
            None,
            [("native_pdf_opt_in", "bool_parsing")],
        ),
        (
            form(part("native_pdf_opt_in", b"t" * 100), part("file", b"%PDF-")),
            None,
            [("native_pdf_opt_in", "bool_parsing")],
        ),
        (b'{"file": "x"}', "application/json", [("body", "multipart_required")]),
        (form(part("file", b"%PDF-", "a.pdf")), "", [("body", "multipart_required")]),
        (form(part("file", b"%PDF-", "a.pdf")), "multipart/form-data", [("body", "boundary_missing")]),
        (form(part("file", b"%PDF-", "a.pdf"))[:-12], None, [("body", "malformed_multipart")]),  # truncated
        (b"not multipart at all", None, [("body", "malformed_multipart")]),
        (
            f"--{BOUNDARY}\r\nContent-Disposition: form-data\r\n\r\nx\r\n--{BOUNDARY}--\r\n".encode(),
            None,
            [("body", "malformed_multipart")],
        ),  # a part without a name
        (
            form(*(part(f"f{i}", b"") for i in range(upload.MULTIPART_MAX_PARTS + 1))),
            None,
            [("body", "too_many_parts")],
        ),
    ],
    ids=[
        "no-file-part",
        "no-part-at-all",
        "two-file-parts",
        "two-opt-ins",
        "opt-in-not-a-boolean",
        "opt-in-too-long",
        "json-body",
        "no-content-type",
        "no-boundary",
        "truncated",
        "garbage",
        "part-without-name",
        "too-many-parts",
    ],
)
async def test_a_malformed_form_is_a_validation_failure(
    body: bytes, content_type: str | None, expected: list[tuple[str, str]]
) -> None:
    assert summary(await problem_of(body, content_type=content_type)) == (422, "validation_failed", expected)


# --- read_body_limited (the paste's JSON body) -----------------------------------------------------------


async def test_a_declared_length_over_the_cap_is_refused_before_any_read() -> None:
    request, pulled = counting_request(
        [b"{}"], content_type="application/json", headers=[(b"content-length", b"%d" % (4096 + 1))]
    )
    with pytest.raises(Problem) as exc:
        await upload.read_body_limited(request, limit=4096, field_name="text")
    assert summary(exc.value) == (413, "upload_rejected", [("text", "too_large")])
    assert pulled.messages == 0


async def test_a_body_without_a_length_is_counted_as_it_streams() -> None:
    limit, chunk = 4096, 512
    body = b'{"text": "' + b"x" * (50 * limit) + b'"}'
    request, pulled = counting_request(chunked(body, chunk), content_type="application/json")
    with pytest.raises(Problem) as exc:
        await upload.read_body_limited(request, limit=limit, field_name="text")
    assert summary(exc.value) == (413, "upload_rejected", [("text", "too_large")])
    assert pulled.bytes <= limit + chunk and pulled.messages < len(chunked(body, chunk)) // 10


async def test_a_body_within_the_cap_is_read_whole() -> None:
    body = b'{"text": "' + b"x" * 4000 + b'"}'
    for headers in ([], [(b"content-length", b"%d" % len(body))], [(b"content-length", b"garbage")]):
        request, _ = counting_request(chunked(body, 512), content_type="application/json", headers=headers)
        assert await upload.read_body_limited(request, limit=len(body), field_name="text") == body


def test_the_paste_body_cap_covers_the_longest_escaped_text() -> None:
    """50,000 characters outside the BMP, each two `\\uXXXX` escapes: the JSON body stays under the cap."""
    longest = json.dumps({"text": "\U0001f600" * upload.PASTE_MAX}).encode()
    assert len(longest) <= upload.PASTE_BODY_MAX_BYTES and len(longest) > upload.PASTE_BODY_MAX_BYTES // 2


# --- CvStore ---------------------------------------------------------------------------------------------


def test_a_file_is_written_encrypted_with_mode_0600_under_a_fresh_name(tmp_path: Path) -> None:
    store, data = CvStore(tmp_path), minimal_pdf(CV_LINES)
    first, second = store.write(data, [KEY]), store.write(data, [KEY])
    assert first.storage_path != second.storage_path
    for stored in (first, second):
        assert re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\.bin", stored.storage_path
        )
        path = tmp_path / stored.storage_path
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert b"Erika" not in path.read_bytes() and path.read_bytes()[1:9] == crypto.key_id(KEY)
        assert stored.sha256 == hashlib.sha256(data).digest()
        assert store.read(stored.storage_path, [KEY]) == data


def test_a_file_opens_only_under_its_own_name_and_with_a_configured_key(tmp_path: Path) -> None:
    store = CvStore(tmp_path)
    a, b = store.write(b"Erika Musterfrau", [KEY]), store.write(b"Max Mustermann", [KEY])
    (tmp_path / a.storage_path).replace(tmp_path / b.storage_path)  # a's blob under b's name
    with pytest.raises(DecryptError):
        store.read(b.storage_path, [KEY])
    c = store.write(b"Erika Musterfrau", [OLD_KEY])
    assert store.read(c.storage_path, [KEY, OLD_KEY]) == b"Erika Musterfrau"  # a rotation: the previous key
    with pytest.raises(DecryptError):
        store.read(c.storage_path, [KEY])


def test_delete_removes_a_file_and_a_missing_one_is_fine(tmp_path: Path) -> None:
    store = CvStore(tmp_path)
    stored = store.write(b"Erika Musterfrau", [KEY])
    store.delete(stored.storage_path)
    store.delete(stored.storage_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("bad", ["../secret.bin", "/etc/passwd", "a/b.bin", "x.bin", ""])
def test_only_names_the_store_made_are_read_or_deleted(tmp_path: Path, bad: str) -> None:
    store = CvStore(tmp_path)
    with pytest.raises(ValueError, match="storage path"):
        store.read(bad, [KEY])
    with pytest.raises(ValueError, match="storage path"):
        store.delete(bad)


def test_a_write_needs_a_key(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="key"):
        CvStore(tmp_path).write(b"x", [])
    assert list(tmp_path.iterdir()) == []


def test_a_text_is_sealed_for_its_cv_file_id(tmp_path: Path) -> None:
    store = CvStore(tmp_path)
    blob = store.encrypt_text("Erika Musterfrau, Köln", [KEY], cv_file_id=7)
    assert b"Erika" not in blob
    assert store.decrypt_text(blob, [OLD_KEY, KEY], cv_file_id=7) == "Erika Musterfrau, Köln"
    assert crypto.decrypt([KEY], blob, aad=b"cv_text:7") == "Erika Musterfrau, Köln".encode()
    with pytest.raises(DecryptError):
        store.decrypt_text(blob, [KEY], cv_file_id=8)
    assert list(tmp_path.iterdir()) == []  # a text never becomes a file
