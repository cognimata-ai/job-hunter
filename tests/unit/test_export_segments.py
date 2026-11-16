"""The export archive's segment format (spec §20.3; plan M5 Task 17, Deviation 19): 1 MiB segments, each
sealed with the data key under the associated data `export:<id>:<n>:<last>` and framed as a 4-byte big-endian
length and the blob, so a reader streams it segment by segment and a truncated, reordered or foreign file
fails."""

import hashlib
import io
import os
from typing import Final

import pytest
from jobhunter import crypto
from jobhunter.privacy.export import SEGMENT_BYTES, ExportCorrupt, iter_decrypted, segment_aad, write_segments

KEY: Final = os.urandom(32)
EXPORT_ID: Final = 7


def sealed(data: bytes, *, export_id: int = EXPORT_ID, key: bytes = KEY) -> bytes:
    """`data` sealed as an export's stored file."""
    sink = io.BytesIO()
    write_segments(io.BytesIO(data), sink, key, export_id=export_id)
    return sink.getvalue()


def frames(stored: bytes) -> list[bytes]:
    """The stored file's frames (each a 4-byte length and its blob), in file order."""
    found, at = [], 0
    while at < len(stored):
        length = int.from_bytes(stored[at : at + 4], "big")
        found.append(stored[at : at + 4 + length])
        at += 4 + length
    return found


def opened(stored: bytes, *, export_id: int = EXPORT_ID, keys: tuple[bytes, ...] = (KEY,)) -> bytes:
    return b"".join(iter_decrypted(io.BytesIO(stored), keys, export_id=export_id))


@pytest.mark.parametrize(
    ("size", "segments"),
    [(0, 1), (1, 1), (SEGMENT_BYTES, 1), (SEGMENT_BYTES + 1, 2), (2 * SEGMENT_BYTES + 12_345, 3)],
)
def test_segments_round_trip(size: int, segments: int) -> None:
    """Every segment holds at most `SEGMENT_BYTES`, an exact multiple ends without an empty trailer, an empty
    archive is one final segment; the reader returns the bytes, and `write_segments` the stored file's sha256
    and the plaintext's size."""
    data = os.urandom(size)
    sink = io.BytesIO()
    written = write_segments(io.BytesIO(data), sink, KEY, export_id=EXPORT_ID)
    stored = sink.getvalue()
    assert (written.sha256, written.size_bytes) == (hashlib.sha256(stored).digest(), size)
    assert len(frames(stored)) == segments
    assert [len(chunk) for chunk in iter_decrypted(io.BytesIO(stored), [KEY], export_id=EXPORT_ID)] == [
        min(SEGMENT_BYTES, size - n * SEGMENT_BYTES) for n in range(segments)
    ]
    assert opened(stored) == data
    # each segment is sealed under its own associated data: the index and whether it is the last
    blobs = [frame[4:] for frame in frames(stored)]
    for n, blob in enumerate(blobs):
        aad = segment_aad(EXPORT_ID, n, n == len(blobs) - 1)
        assert aad == f"export:{EXPORT_ID}:{n}:{int(n == len(blobs) - 1)}".encode()
        assert crypto.decrypt([KEY], blob, aad=aad) == data[n * SEGMENT_BYTES : (n + 1) * SEGMENT_BYTES]


def test_a_rotated_key_still_opens_the_file() -> None:
    """The reader takes every key of `data_keys` (a rotation: the previous key still opens)."""
    data = os.urandom(SEGMENT_BYTES + 5)
    assert opened(sealed(data), keys=(os.urandom(32), KEY)) == data


def test_a_truncated_file_raises() -> None:
    """A file cut at a frame boundary (its last segment was sealed as not last), inside a frame, inside a
    length, or empty, and a frame that claims more than a segment, each raise `ExportCorrupt`."""
    stored = sealed(os.urandom(2 * SEGMENT_BYTES + 100))
    first, second, _ = frames(stored)
    for cut in (
        first + second,  # the last segment gone
        first,
        stored[:-1],  # inside the last blob
        stored[: len(first) + 2],  # inside the second length
        b"",
    ):
        with pytest.raises(ExportCorrupt):
            opened(cut)
    oversized = (SEGMENT_BYTES + 1_000).to_bytes(4, "big") + first[4:]
    with pytest.raises(ExportCorrupt):
        opened(oversized)
    with pytest.raises(ExportCorrupt):  # trailing bytes after the final segment
        opened(stored + b"\x00\x00\x00\x00")


def test_reordered_segments_raise() -> None:
    """Swapped, repeated or dropped middle segments fail their index; the bytes before the failure may have
    been returned, never a segment out of place."""
    stored = sealed(os.urandom(3 * SEGMENT_BYTES))
    first, second, third = frames(stored)
    for reordered in (second + first + third, first + first + third, first + third, first + third + second):
        with pytest.raises(ExportCorrupt):
            opened(reordered)


def test_the_aad_binds_the_export_id() -> None:
    """Another export's file (or this one read as another's) does not open, nor one sealed with another
    key."""
    data = os.urandom(SEGMENT_BYTES + 3)
    with pytest.raises(ExportCorrupt):
        opened(sealed(data, export_id=EXPORT_ID), export_id=EXPORT_ID + 1)
    with pytest.raises(ExportCorrupt):
        opened(sealed(b"short", export_id=70), export_id=7)
    with pytest.raises(ExportCorrupt):
        opened(sealed(data), keys=(os.urandom(32),))
