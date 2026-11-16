"""The data-key rotation (spec §16.8 L5490, §19.8 "Secret rotation"; plan M5 Task 28, Deviations 31 and 39):
`jobhunter crypto rotate`.

**The procedure** (the RUNBOOK's): the old key is mounted as `data_key_previous` and the new one as
`data_key`, api and worker are restarted (Compose does not see a secret file's new content), then
`docker compose run --rm --no-deps worker jobhunter crypto rotate`. Once it exits 0, `data_key_previous` is
emptied and both restarted again. The old key stays in the offline recovery kit while a retained backup set
names it (Deviation 31).

**`rotate(cfg, engine, *, batch)`** re-encrypts every ciphertext under the previous key's id (`crypto.key_id`,
which `crypto.blob_key_id` reads from a blob's header) with the data key, then counts what is left:

1. **The CV store** (`cv_files`, `batch` rows a transaction, in id order, locked `FOR UPDATE`): the file a
   live row names (`storage_path`), sealed again in place by `CvStore.reseal` (the store's own file protocol
   and associated data: `<name>.rotate`, renamed over the file), and its `raw_text_enc`, updated in place.
   The row stays locked until both are done. A purge and the retention rule lock the row before they clear it
   and unlink its file (the rule under that lock, a purge after its commit), so neither meets a half-rotated
   row, and a file is never re-created after its unlink.
2. **The TOTP secret** (`users`, the one row `FOR UPDATE`), under `auth.users.TOTP_AAD`.
3. **The exports** (`exports`, one `ready` row a transaction, in id order, `FOR UPDATE`): an archive under the
   previous key is read segment by segment (`privacy.export.iter_decrypted`) and sealed again by
   `write_segments` under the data key into `<storage_path>.rotate` (`profile.storage.replace_file`), which
   takes the same segments, each with its own index and final flag; then the rename, and the row's `sha256`
   becomes the new file's (its name and `size_bytes` stay), in the same transaction, committed before the
   next export. An archive already under the data key whose row holds another `sha256` than the file's (an
   earlier run stopped between the rename and its commit) has the row's `sha256` corrected, once every
   segment of the file opens under the data key as this export's (`iter_decrypted`: its id, index and final
   flag), and to the hash of the bytes so authenticated (T28-7); one that does not (a damaged file, another
   export's) is not repaired: `ExportUnauthenticated` stops the run (below), its row as it was. The name is
   the export's `<id>-*`, made while the row is held `ready` (`privacy.export`'s guard), so a purge's unlink
   of `<id>-*` follows it. The plaintext is never on a disk: the segments stream through memory.

Each kind is decided per ciphertext by its key id, so a run is **resumable and idempotent**: a ciphertext
under the data key is skipped (each row and file is rewritten once), and a run stopped anywhere (the fault
point `crypto.rotate.after_batch` follows each committed batch in the test env) leaves only finished
ciphertexts and, at most, a `.rotate` file, which the next run removes first
(`profile.storage.remove_replace_leftovers`). One rotation runs at a time.

**The verification pass** then counts every ciphertext of the application by its key id: every value of the
database's sealed columns (`crypto.SEALED_VALUES_SQL`, the inventory the backup set reads too), each CV file
a live row names and each `ready` export (by its first segment's key id: `write_segments` seals a whole
archive under one key). The data key's are left out, so `RotationReport.remaining_by_key_id` is empty once
only the data key remains. A file of the CV store that no row names (an upload that failed, a purged CV whose
unlink follows its commit) is no ciphertext of the application: it is neither rewritten (that could re-create
a purged file) nor counted; the retention rule `cv_store_orphans` removes it after a day, and a restore
removes it. A ciphertext under any key other than the two mounted ones is left as it is and counted:
`crypto.decrypt` has no key for it (a set taken during one rotation and restored after the next holds two
keys, neither current).

**The command** prints the report on one JSON line, `{"rewritten": {kind: count}, "remaining_by_key_id":
{hex key id: count}}` (key ids and counts, nothing else), and exits by `outcome`: 0 when only the data key
remains; 3 with `"error": "other_keys_remain"` when ciphertexts under keys it was not given remain (the next
pass mounts one of them as `data_key_previous`; two key slots recover a set under any number of old keys, one
pass per key, Deviation 31); 2 with `no_previous_key` when no `data_key_previous` is mounted (an empty file is
none) and a ciphertext under another key exists, having rewritten nothing; 1 with `previous_key_remains` when
a ciphertext under the mounted previous key is still found (one sealed meanwhile by a service that was not
restarted): restart api and worker and run it again.

**A failure** prints `{"error": "database"}` for a database error, else `{"error": "unexpected",
"error_class", "kind", "table", "row_id"}` (exit 1): a ciphertext that cannot be rotated or counted
(`RotationFailed`: one under the previous key that does not open, `crypto.DecryptError` or
`privacy.export.ExportCorrupt`; an export under the data key whose hash the repair refuses,
`ExportUnauthenticated`; or a row whose `storage_path` is no name of its volume) stops the run, and the
output and the log line `crypto_rotate_failed` name its kind (`cv_files`, `cv_texts`, `totp`, `exports`), its
table and its row id, never its content; every rerun stops at it until it is dealt with. Log lines carry
kinds, counts, row ids and key ids, never content.
"""

import argparse
import asyncio
import hashlib
import io
import json
import os
import sys
from collections import Counter
from collections.abc import AsyncIterator, Buffer, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, BinaryIO, Final, Literal

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.sql.elements import TextClause

from jobhunter import crypto
from jobhunter.auth.users import TOTP_AAD
from jobhunter.config import AppConfig, startup_or_exit
from jobhunter.db.sql import q
from jobhunter.faults import afault_point
from jobhunter.observability.logging import configure_logging, get_logger
from jobhunter.privacy.export import ExportCorrupt, iter_decrypted, write_segments
from jobhunter.profile.storage import CvStore, remove_replace_leftovers, replace_file
from jobhunter.types import JSONValue

ROTATION_BATCH: Final = 100
AFTER_BATCH: Final = "crypto.rotate.after_batch"
OTHER_KEYS_REMAIN: Final = "other_keys_remain"
NO_PREVIOUS_KEY: Final = "no_previous_key"
PREVIOUS_KEY_REMAINS: Final = "previous_key_remains"

Kind = Literal["cv_files", "cv_texts", "totp", "exports"]
KINDS: Final[tuple[Kind, ...]] = ("cv_files", "cv_texts", "totp", "exports")

_FRAME_HEADER: Final = 4  # an export segment's length (`privacy.export.write_segments`)
_READ_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC

_CV_ROWS: Final = q(
    "SELECT id, storage_path, raw_text_enc FROM cv_files "
    "WHERE id > :after AND (storage_path IS NOT NULL OR raw_text_enc IS NOT NULL) ORDER BY id LIMIT :batch"
)
_LOCK_CV_ROWS: Final = q(
    "SELECT id, storage_path, raw_text_enc FROM cv_files "
    "WHERE id > :after AND (storage_path IS NOT NULL OR raw_text_enc IS NOT NULL) ORDER BY id LIMIT :batch "
    "FOR UPDATE"
)
_SET_TEXT: Final = q("UPDATE cv_files SET raw_text_enc = :sealed WHERE id = :id")
_LOCK_TOTP: Final = q("SELECT totp_secret_enc FROM users WHERE id = 1 FOR UPDATE")
_SET_TOTP: Final = q("UPDATE users SET totp_secret_enc = :sealed WHERE id = 1")
# Every value of the sealed columns (`crypto.SEALED_VALUES_SQL`), counted by its key id.
_SEALED_KEY_IDS: Final = q(
    "SELECT substring(s.sealed FROM 2 FOR :id_bytes) AS key_id, count(*) AS n FROM ("  # noqa: S608 - constant SQL
    + crypto.SEALED_VALUES_SQL
    + ") AS s WHERE length(s.sealed) >= :header_bytes AND get_byte(s.sealed, 0) = :version GROUP BY 1"
)
_EXPORT_ROWS: Final = q(
    "SELECT id, storage_path FROM exports "
    "WHERE id > :after AND status = 'ready' AND storage_path IS NOT NULL ORDER BY id LIMIT :batch"
)
_LOCK_NEXT_EXPORT: Final = q(
    "SELECT id, storage_path, sha256, size_bytes FROM exports "
    "WHERE id > :after AND status = 'ready' AND storage_path IS NOT NULL ORDER BY id LIMIT 1 FOR UPDATE"
)
_SET_EXPORT_SHA256: Final = q("UPDATE exports SET sha256 = :sha256 WHERE id = :id")

log = get_logger("jobhunter.crypto_rotate")


@dataclass(frozen=True)
class RotationReport:
    """What a rotation did: per kind, the ciphertexts it re-encrypted; per hex key id, the ciphertexts still
    under it after the verification pass, the data key's left out."""

    rewritten: dict[str, int]
    remaining_by_key_id: dict[str, int]


class RotationFailed(Exception):
    """A ciphertext could not be rotated or counted (module docstring, "A failure"): where it is (its kind,
    its table and its row id) and the class of the error, never its message."""

    def __init__(self, kind: Kind, table: str, row_id: int, error_class: str) -> None:
        super().__init__(f"{kind} of {table} row {row_id}: {error_class}")
        self.kind = kind
        self.table = table
        self.row_id = row_id
        self.error_class = error_class

    def printed(self) -> dict[str, JSONValue]:
        return {
            "error": "unexpected",
            "error_class": self.error_class,
            "kind": self.kind,
            "table": self.table,
            "row_id": self.row_id,
        }


class ExportUnauthenticated(Exception):
    """An archive under the data key whose row holds another `sha256` than the file's, and which does not open
    under the data key as its export's (`privacy.export.ExportCorrupt`): the hash repair refuses it, and its
    row keeps its hash (module docstring, step 3; T28-7)."""


@contextmanager
def _at(kind: Kind, table: str, row_id: int) -> Iterator[None]:
    """A failure inside (but a database error, which is the command's own) is the ciphertext's, at its row."""
    try:
        yield
    except SQLAlchemyError:
        raise
    except Exception as exc:  # re-raised with its location: the class only, never the message
        raise RotationFailed(kind, table, row_id, type(exc).__name__) from exc


@dataclass(frozen=True)
class _Keys:
    """The data key, which seals, and the previous one, which opens what this rotation re-encrypts."""

    current: bytes
    previous: bytes

    @property
    def current_id(self) -> bytes:
        return crypto.key_id(self.current)

    @property
    def previous_id(self) -> bytes:
        return crypto.key_id(self.previous)

    @property
    def both(self) -> list[bytes]:
        """The data key first: what `CvStore.reseal` and `encrypt_text` seal under, and both open with."""
        return [self.current, self.previous]


def _cv_file_key_id(store: CvStore, storage_path: str) -> bytes | None:
    """The key id of a stored CV file; `None` when it is gone or its header names none. `ValueError` for a
    `storage_path` that is no name of the store."""
    header = store.header(storage_path)
    return None if header is None else crypto.blob_key_id(header)


def _export_path(cfg: AppConfig, export_id: int, storage_path: str) -> Path:
    """The archive's path in the exports directory; a `storage_path` that is no plain file name raises, in the
    rewrite and in the count alike."""
    if Path(storage_path).name != storage_path:
        raise ValueError(f"export {export_id}: its storage path is no file name")
    return cfg.exports_dir / storage_path


def _export_key_id(path: Path) -> bytes | None:
    """The key id of an export archive's first segment (after its 4-byte length); `None` when it is gone."""
    try:
        fd = os.open(path, _READ_FLAGS)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as file:
        head = file.read(_FRAME_HEADER + crypto.HEADER_BYTES)
    return crypto.blob_key_id(head[_FRAME_HEADER:])


def _file_sha256(path: Path) -> bytes:
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").digest()


class _Hashed(io.RawIOBase):
    """The bytes of `source`, hashed (sha256) as they are read."""

    def __init__(self, source: BinaryIO) -> None:
        self._source = source
        self.digest = hashlib.sha256()

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Buffer, /) -> int:
        view = memoryview(buffer).cast("B")
        data = self._source.read(len(view))
        view[: len(data)] = data
        self.digest.update(data)
        return len(data)


def _authenticated_sha256(path: Path, export_id: int, key: bytes) -> bytes:
    """The sha256 of the archive `path` once every segment of it opens under `key` as export `export_id`'s
    (`iter_decrypted` reads the file to its end, each segment authenticated with its index and final flag;
    the plaintext is dropped); `ExportUnauthenticated` when one does not (T28-7)."""
    with path.open("rb") as file:
        source = _Hashed(file)
        try:
            for _ in iter_decrypted(io.BufferedReader(source), [key], export_id=export_id):
                pass
        except ExportCorrupt as exc:
            raise ExportUnauthenticated(f"export {export_id}: its archive does not open") from exc
    return source.digest.digest()


class _Plaintext(io.RawIOBase):
    """The segments of `iter_decrypted` read as one stream, so `write_segments` re-chunks them as it chunked
    the zip: the same boundaries, and no plaintext on a disk."""

    def __init__(self, segments: Iterator[bytes]) -> None:
        self._segments = segments
        self._pending = b""

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Buffer, /) -> int:
        while not self._pending:
            following = next(self._segments, None)
            if following is None:
                return 0
            self._pending = following
        view = memoryview(buffer).cast("B")
        n = min(len(view), len(self._pending))
        view[:n] = self._pending[:n]
        self._pending = self._pending[n:]
        return n


# ---- the rewrite -------------------------------------------------------------------------------------------


async def _rotate_cv_store(
    cfg: AppConfig, engine: AsyncEngine, keys: _Keys, batch: int, rewritten: Counter[str]
) -> None:
    """Step 1 (module docstring): the CV files and texts, `batch` live rows a transaction."""
    store, after = CvStore(cfg.cv_store_dir), 0
    while True:
        files = texts = 0
        async with engine.begin() as conn:
            rows = (await conn.execute(_LOCK_CV_ROWS, {"after": after, "batch": batch})).all()
            if not rows:
                return
            for row in rows:
                cv_file_id = int(row.id)
                if row.storage_path is not None:
                    with _at("cv_files", "cv_files", cv_file_id):
                        name = str(row.storage_path)
                        if await asyncio.to_thread(_cv_file_key_id, store, name) == keys.previous_id:
                            await asyncio.to_thread(store.reseal, name, keys.both)
                            files += 1
                sealed = None if row.raw_text_enc is None else bytes(row.raw_text_enc)
                if sealed is not None and crypto.blob_key_id(sealed) == keys.previous_id:
                    with _at("cv_texts", "cv_files", cv_file_id):
                        text = store.decrypt_text(sealed, keys.both, cv_file_id=cv_file_id)
                        resealed = store.encrypt_text(text, keys.both, cv_file_id=cv_file_id)
                    await conn.execute(_SET_TEXT, {"sealed": resealed, "id": cv_file_id})
                    texts += 1
            after = int(rows[-1].id)
        rewritten["cv_files"] += files
        rewritten["cv_texts"] += texts
        log.info("crypto_rotate_batch", kind="cv_store", after_id=after, files=files, texts=texts)
        await afault_point(AFTER_BATCH)


async def _rotate_totp(engine: AsyncEngine, keys: _Keys, rewritten: Counter[str]) -> None:
    """Step 2: the user's TOTP secret."""
    async with engine.begin() as conn:
        found = (await conn.execute(_LOCK_TOTP)).scalar_one_or_none()
        sealed = None if found is None else bytes(found)
        if sealed is None or crypto.blob_key_id(sealed) != keys.previous_id:
            return
        with _at("totp", "users", 1):
            secret = crypto.decrypt(keys.both, sealed, aad=TOTP_AAD)
        await conn.execute(_SET_TOTP, {"sealed": crypto.encrypt(keys.current, secret, aad=TOTP_AAD)})
    rewritten["totp"] += 1
    log.info("crypto_rotate_batch", kind="totp", totp=1)
    await afault_point(AFTER_BATCH)


async def _rotate_exports(cfg: AppConfig, engine: AsyncEngine, keys: _Keys, rewritten: Counter[str]) -> None:
    """Step 3: the archives of the `ready` exports, one export a transaction."""
    after = 0
    while True:
        async with engine.begin() as conn:
            row = (await conn.execute(_LOCK_NEXT_EXPORT, {"after": after})).first()
            if row is None:
                return
            after = export_id = int(row.id)
            with _at("exports", "exports", export_id):
                path = _export_path(cfg, export_id, str(row.storage_path))
                key = await asyncio.to_thread(_export_key_id, path)
                stored = None if row.sha256 is None else bytes(row.sha256)
                if key == keys.previous_id:
                    sha256 = await asyncio.to_thread(_reseal_export, path, export_id, row.size_bytes, keys)
                    outcome = "resealed"
                elif key == keys.current_id and await asyncio.to_thread(_file_sha256, path) != stored:
                    # the bytes authenticated under the data key give the row its hash, or nothing does
                    sha256 = await asyncio.to_thread(_authenticated_sha256, path, export_id, keys.current)
                    outcome = "sha256_corrected"
                else:
                    continue
            await conn.execute(_SET_EXPORT_SHA256, {"sha256": sha256, "id": export_id})
        if outcome == "resealed":
            rewritten["exports"] += 1
        log.info("crypto_rotate_batch", kind="exports", export_id=export_id, outcome=outcome)
        await afault_point(AFTER_BATCH)


def _reseal_export(path: Path, export_id: int, size_bytes: int | None, keys: _Keys) -> bytes:
    """The archive `path` sealed again under the data key, in place; the sha256 of the new file."""
    digests: list[bytes] = []

    def write(out: BinaryIO) -> None:
        with path.open("rb") as source:
            plaintext = io.BufferedReader(_Plaintext(iter_decrypted(source, keys.both, export_id=export_id)))
            sealed = write_segments(plaintext, out, keys.current, export_id=export_id)
        if size_bytes is not None and sealed.size_bytes != size_bytes:
            raise ValueError(f"export {export_id}: the archive holds another size than its row")
        digests.append(sealed.sha256)

    replace_file(path, write)
    return digests[0]


# ---- the verification --------------------------------------------------------------------------------------


async def _remaining(cfg: AppConfig, engine: AsyncEngine, current_id: bytes, batch: int) -> dict[str, int]:
    """The verification pass (module docstring): per hex key id other than the data key's, the ciphertexts
    under it."""
    found: Counter[bytes] = Counter()
    params = {
        "id_bytes": crypto.KEY_ID_BYTES,
        "header_bytes": crypto.HEADER_BYTES,
        "version": crypto.FORMAT_VERSION,
    }
    async with engine.connect() as conn:
        for row in (await conn.execute(_SEALED_KEY_IDS, params)).all():
            found[bytes(row.key_id)] += int(row.n)
    store = CvStore(cfg.cv_store_dir)
    async for row in _pages(engine, _CV_ROWS, batch):
        if row.storage_path is not None:
            with _at("cv_files", "cv_files", int(row.id)):
                if key := await asyncio.to_thread(_cv_file_key_id, store, str(row.storage_path)):
                    found[key] += 1
    async for row in _pages(engine, _EXPORT_ROWS, batch):
        with _at("exports", "exports", int(row.id)):
            path = _export_path(cfg, int(row.id), str(row.storage_path))
            if key := await asyncio.to_thread(_export_key_id, path):
                found[key] += 1
    found.pop(current_id, None)
    return {key.hex(): n for key, n in sorted(found.items())}


async def _pages(engine: AsyncEngine, query: TextClause, batch: int) -> AsyncIterator[Any]:
    """Every row `query` pages through (`after`, `batch`), in id order, unlocked."""
    after = 0
    while True:
        async with engine.connect() as conn:
            rows = (await conn.execute(query, {"after": after, "batch": batch})).all()
        if not rows:
            return
        for row in rows:
            yield row
        after = int(rows[-1].id)


# ---- the rotation ------------------------------------------------------------------------------------------


async def rotate(cfg: AppConfig, engine: AsyncEngine, *, batch: int = ROTATION_BATCH) -> RotationReport:
    """Re-encrypt every ciphertext under the mounted `data_key_previous`'s key id with `data_key`, then count
    what remains by key id (module docstring). Without a previous key it only counts. `RotationFailed` names a
    ciphertext that cannot be rotated or counted."""
    data_keys = crypto.data_keys(cfg)
    current_id = crypto.key_id(data_keys[0])
    rewritten: Counter[str] = Counter(dict.fromkeys(KINDS, 0))
    keys = _Keys(data_keys[0], data_keys[1]) if len(data_keys) > 1 else None
    if keys is not None and keys.previous_id != current_id:
        log.info("crypto_rotate_started", key_id=current_id.hex(), previous_key_id=keys.previous_id.hex())
        for directory in (cfg.cv_store_dir, cfg.exports_dir):
            await asyncio.to_thread(remove_replace_leftovers, directory)
        await _rotate_cv_store(cfg, engine, keys, batch, rewritten)
        await _rotate_totp(engine, keys, rewritten)
        await _rotate_exports(cfg, engine, keys, rewritten)
    remaining = await _remaining(cfg, engine, current_id, batch)
    report = RotationReport(
        rewritten={kind: rewritten[kind] for kind in KINDS}, remaining_by_key_id=remaining
    )
    log.info(
        "crypto_rotate_verified",
        key_id=current_id.hex(),
        files=report.rewritten["cv_files"],
        texts=report.rewritten["cv_texts"],
        totp=report.rewritten["totp"],
        exports=report.rewritten["exports"],
        remaining=sum(remaining.values()),
        remaining_key_ids=sorted(remaining),
    )
    return report


def outcome(cfg: AppConfig, report: RotationReport) -> tuple[int, str | None]:
    """The command's exit status and error code for `report` (module docstring)."""
    if not report.remaining_by_key_id:
        return 0, None
    data_keys = crypto.data_keys(cfg)
    if len(data_keys) < 2:
        return 2, NO_PREVIOUS_KEY
    if crypto.key_id(data_keys[1]).hex() in report.remaining_by_key_id:
        return 1, PREVIOUS_KEY_REMAINS
    return 3, OTHER_KEYS_REMAIN


async def _run(cfg: AppConfig) -> tuple[int, dict[str, JSONValue]]:
    engine = create_async_engine(cfg.dsn("sqlalchemy"), poolclass=NullPool, hide_parameters=True)
    try:
        report = await rotate(cfg, engine)
    finally:
        await engine.dispose()
    code, error = outcome(cfg, report)
    printed: dict[str, JSONValue] = {} if error is None else {"error": error}
    printed.update(asdict(report))
    return code, printed


def run_rotate_command(args: argparse.Namespace) -> int:
    """`jobhunter crypto rotate`, as the worker (its data keys and its volumes): one JSON line on stdout."""
    cfg = startup_or_exit("worker")
    configure_logging(cfg.log_level, stream=sys.stderr)
    try:
        code, printed = asyncio.run(_run(cfg))
    except RotationFailed as failed:
        log.error(
            "crypto_rotate_failed",
            error_class=failed.error_class,
            kind=failed.kind,
            table=failed.table,
            row_id=failed.row_id,
        )
        code, printed = 1, failed.printed()
    except SQLAlchemyError as exc:
        log.error("crypto_rotate_failed", error_class=type(exc).__name__)
        code, printed = 1, {"error": "database"}
    except Exception as exc:  # noqa: BLE001 - one JSON line names the class, never the message
        log.error("crypto_rotate_failed", error_class=type(exc).__name__)
        code, printed = 1, {"error": "unexpected", "error_class": type(exc).__name__}
    print(json.dumps(printed))
    return code
