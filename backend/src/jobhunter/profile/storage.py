"""Encrypted CV files and texts (spec §12.1, §12.2, §16.9; M2 Task 9).

`CvStore(root)` keeps the uploaded CV files on the `cv_store` volume (`AppConfig.cv_store_dir`, `/data/cv`),
which no web root serves:

- `write(data, keys)` seals `data` with AES-256-GCM under `keys[0]` (the data key, `crypto.data_keys`) and a
  fresh nonce (`crypto.encrypt`), and writes it as `<uuid4>.bin`, mode 0600 from its creation, flushed to
  the disk with its directory entry before it returns. The associated data is `b"cv_file:" + name`, so a
  blob renamed to another file's name does not open. It returns the name (`cv_files.storage_path`) and the
  sha256 of the plaintext (`cv_files.sha256`). A write that fails removes what it wrote;
- `read(storage_path, keys)` opens a file with any of `keys` (a rotation); `delete(storage_path)` removes one,
  and a missing file is no error (retention and a failed upload may race to it). Both accept only a name
  `write` makes, never a path, so no caller value can reach outside the root;
- `encrypt_text(text, keys, *, cv_file_id)` and `decrypt_text(blob, keys, *, cv_file_id)` seal a CV text for
  its `cv_files` row (`raw_text_enc`: a pasted text, or the text docparse extracted) with the associated data
  `b"cv_text:%d" % cv_file_id`, so a text moved to another row does not open. A text never becomes a file.

The file work is synchronous: a file is at most 10 MiB (§12.1), and a write the caller awaits cannot outlive
the exception path that removes it, as a write in a thread could. Nothing here logs: a caller logs the name's
row id and a size, never a CV's content.
"""

import hashlib
import os
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from jobhunter import crypto

FILE_MODE: Final = 0o600
FILE_AAD_PREFIX: Final = b"cv_file:"
# A name `write` makes: a UUID4 in its canonical form, then `.bin`.
_STORAGE_NAME: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\.bin")
_CREATE_FLAGS: Final = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC


@dataclass(frozen=True)
class StoredFile:
    storage_path: str
    sha256: bytes


def _file_aad(storage_path: str) -> bytes:
    return FILE_AAD_PREFIX + storage_path.encode("ascii")


def _text_aad(cv_file_id: int) -> bytes:
    return b"cv_text:%d" % cv_file_id


class CvStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, storage_path: str) -> Path:
        if not _STORAGE_NAME.fullmatch(storage_path):
            raise ValueError("not a storage path of the CV store")
        return self.root / storage_path

    def write(self, data: bytes, keys: Sequence[bytes]) -> StoredFile:
        """Seal `data` under `keys[0]` into a new file of the store; its name and the plaintext's sha256."""
        if not keys:
            raise ValueError("writing a CV file needs a data key")
        name = f"{uuid.uuid4()}.bin"
        blob = crypto.encrypt(keys[0], data, aad=_file_aad(name))
        path = self._path(name)
        fd = os.open(path, _CREATE_FLAGS, FILE_MODE)
        try:
            with os.fdopen(fd, "wb") as out:
                os.fchmod(out.fileno(), FILE_MODE)  # whatever the umask
                out.write(blob)
                out.flush()
                os.fsync(out.fileno())
            _fsync_directory(self.root)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return StoredFile(storage_path=name, sha256=hashlib.sha256(data).digest())

    def read(self, storage_path: str, keys: Sequence[bytes]) -> bytes:
        """The plaintext of a stored file; `crypto.DecryptError` when no key of `keys` opens it."""
        return crypto.decrypt(keys, self._path(storage_path).read_bytes(), aad=_file_aad(storage_path))

    def delete(self, storage_path: str) -> None:
        """Remove a stored file; one that is already gone is fine."""
        self._path(storage_path).unlink(missing_ok=True)

    def encrypt_text(self, text: str, keys: Sequence[bytes], *, cv_file_id: int) -> bytes:
        """`text` (UTF-8) sealed under `keys[0]` for the `cv_files` row `cv_file_id`."""
        if not keys:
            raise ValueError("sealing a CV text needs a data key")
        return crypto.encrypt(keys[0], text.encode("utf-8"), aad=_text_aad(cv_file_id))

    def decrypt_text(self, blob: bytes, keys: Sequence[bytes], *, cv_file_id: int) -> str:
        """The text `encrypt_text` sealed for `cv_file_id`; `crypto.DecryptError` for another row's blob."""
        return crypto.decrypt(keys, blob, aad=_text_aad(cv_file_id)).decode("utf-8")


def _fsync_directory(directory: Path) -> None:
    """Flush the directory entry of a new file, so a row that names it never outlives it in a power loss."""
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
