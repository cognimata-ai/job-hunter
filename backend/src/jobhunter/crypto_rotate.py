"""The data-key rotation (spec §16.8 L5490, §19.8 "Secret rotation"; plan M5 Task 28, Deviations 31 and 39):
`jobhunter crypto rotate`.

**The procedure** (the RUNBOOK's): the old key is mounted as `data_key_previous` and the new one as
`data_key`, api and worker are restarted (Compose does not see a secret file's new content), then
`docker compose run --rm --no-deps worker jobhunter crypto rotate`. Once it exits 0, `data_key_previous` is
emptied and both restarted again. The old key stays in the offline recovery kit while a retained backup set
names it (Deviation 31).

**`rotate(cfg, engine, *, batch)`** re-encrypts every ciphertext under the previous key's id (`crypto.key_id`,
the 8 bytes after the version byte of `crypto.encrypt`'s blob) with the data key, then counts what is left:

1. **The CV store** (`cv_files`, `batch` rows a transaction, in id order, locked `FOR UPDATE`): the file a
   live row names (`storage_path`) and its `raw_text_enc`. A file is re-sealed with its own associated data
   (its name, `profile.storage`) into `<name>.rotate`, created exclusively with mode 0600 and flushed to the
   disk, then renamed over the file, and the directory flushed; the text is updated in place. The row stays
   locked until both are done. A purge and the retention rule lock the row before they clear it and unlink
   its file (the rule under that lock, a purge after its commit), so neither meets a half-rotated row, and a
   file is never re-created after its unlink.
2. **The TOTP secret** (`users`, the one row `FOR UPDATE`), under `auth.users.TOTP_AAD`.
3. **The exports** (`exports`, `batch` `ready` rows a transaction, in id order, `FOR UPDATE`): the archive is
   read segment by segment (`privacy.export.iter_decrypted`) and sealed again by `write_segments` under the
   data key into `<storage_path>.rotate`, which takes the same segments, each with its own index and final
   flag; then the rename, and the row's `sha256` becomes the new file's (its name and `size_bytes` stay). The
   name is the export's `<id>-*`, made while the row is held `ready` (`privacy.export`'s guard), so a purge's
   unlink of `<id>-*` follows it. The plaintext is never on a disk: the segments stream through memory.

Each kind is decided per ciphertext by its key id, so a run is **resumable and idempotent**: a ciphertext
under the data key is skipped (each row and file is rewritten once), and a run stopped anywhere (the fault
point `crypto.rotate.after_batch` follows each batch's commit in the test env) leaves only finished
ciphertexts and, at most, a `.rotate` file, which the next run removes first. One rotation runs at a time.

**The verification pass** then counts every ciphertext of the application by its key id: each CV file a live
row names, each `raw_text_enc`, the TOTP secret and each `ready` export (by its first segment's key id:
`write_segments` seals a whole archive under one key). The data key's are left out, so
`RotationReport.remaining_by_key_id` is empty once only the data key remains. A file of the CV store that no
row names (an upload that failed, a purged CV whose unlink follows its commit) is no ciphertext of the
application: it is neither rewritten (that could re-create a purged file) nor counted; the retention rule
`cv_store_orphans` removes it after a day, and a restore removes it. A ciphertext under any key other
than the two mounted ones is left as it is and counted: `crypto.decrypt` has no key for it (a set taken
during one rotation and restored after the next holds two keys, neither current).

**The command** prints the report on one JSON line, `{"rewritten": {kind: count}, "remaining_by_key_id":
{hex key id: count}}` (key ids and counts, nothing else), and exits by `outcome`: 0 when only the data key
remains; 3 with `"error": "other_keys_remain"` when ciphertexts under keys it was not given remain (the next
pass mounts one of them as `data_key_previous`; two key slots recover a set under any number of old keys, one
pass per key, Deviation 31); 2 with `no_previous_key` when no `data_key_previous` is mounted (an empty file is
none) and a ciphertext under another key exists, having rewritten nothing; 1 with `previous_key_remains` when
a ciphertext under the mounted previous key is still found (one sealed meanwhile by a service that was not
restarted): run it again once they are. A failure prints `{"error": "database"}` or `{"error": "unexpected",
"error_class": ...}` (exit 1); a ciphertext under the previous key that does not open (`crypto.DecryptError`)
is one, and nothing after it is rewritten. Log lines carry kinds, counts, row ids and key ids, never content.
"""

import argparse
import asyncio
import io
import json
import os
import sys
from collections import Counter
from collections.abc import AsyncIterator, Buffer, Callable, Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import BinaryIO, Final, Literal

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
from jobhunter.privacy.export import iter_decrypted, write_segments
from jobhunter.profile.storage import FILE_AAD_PREFIX, CvStore, fsync_directory
from jobhunter.types import JSONValue

ROTATION_BATCH: Final = 100
AFTER_BATCH: Final = "crypto.rotate.after_batch"
ROTATE_SUFFIX: Final = ".rotate"
FILE_MODE: Final = 0o600
OTHER_KEYS_REMAIN: Final = "other_keys_remain"
NO_PREVIOUS_KEY: Final = "no_previous_key"
PREVIOUS_KEY_REMAINS: Final = "previous_key_remains"

Kind = Literal["cv_files", "cv_texts", "totp", "exports"]
KINDS: Final[tuple[Kind, ...]] = ("cv_files", "cv_texts", "totp", "exports")

_HEADER_BYTES: Final = 1 + crypto.KEY_ID_BYTES
_FRAME_HEADER: Final = 4  # an export segment's length (`privacy.export.write_segments`)
_CREATE_FLAGS: Final = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
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
_SEALED_KEY_IDS: Final = q(
    "SELECT substring(s.sealed FROM 2 FOR :id_bytes) AS key_id, count(*) AS n FROM ("
    "SELECT raw_text_enc AS sealed FROM cv_files UNION ALL SELECT totp_secret_enc FROM users) AS s "
    "WHERE length(s.sealed) >= :header_bytes AND get_byte(s.sealed, 0) = :version GROUP BY 1"
)
_EXPORT_ROWS: Final = q(
    "SELECT id, storage_path, size_bytes FROM exports "
    "WHERE id > :after AND status = 'ready' AND storage_path IS NOT NULL ORDER BY id LIMIT :batch"
)
_LOCK_EXPORT_ROWS: Final = q(
    "SELECT id, storage_path, size_bytes FROM exports "
    "WHERE id > :after AND status = 'ready' AND storage_path IS NOT NULL ORDER BY id LIMIT :batch FOR UPDATE"
)
_SET_EXPORT_SHA256: Final = q("UPDATE exports SET sha256 = :sha256 WHERE id = :id")

log = get_logger("jobhunter.crypto_rotate")


@dataclass(frozen=True)
class RotationReport:
    """What a rotation did: per kind, the ciphertexts it re-encrypted; per hex key id, the ciphertexts still
    under it after the verification pass, the data key's left out."""

    rewritten: dict[str, int]
    remaining_by_key_id: dict[str, int]


@dataclass(frozen=True)
class _Keys:
    """The data key, which seals, and the previous one, which opens what this rotation re-encrypts."""

    current: bytes
    previous: bytes

    @property
    def previous_id(self) -> bytes:
        return crypto.key_id(self.previous)

    @property
    def both(self) -> list[bytes]:
        return [self.current, self.previous]


def _blob_key_id(header: bytes) -> bytes | None:
    """The key id of a blob that begins with `header`, or `None` for one too short or of another version."""
    if len(header) < _HEADER_BYTES or header[0] != crypto.FORMAT_VERSION:
        return None
    return header[1:_HEADER_BYTES]


def _read_head(path: Path, size: int) -> bytes | None:
    """The first `size` bytes of the file `path` (fewer for a shorter one), or `None` when it is gone."""
    try:
        fd = os.open(path, _READ_FLAGS)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as file:
        return file.read(size)


def _file_key_id(path: Path) -> bytes | None:
    head = _read_head(path, _HEADER_BYTES)
    return None if head is None else _blob_key_id(head)


def _export_key_id(path: Path) -> bytes | None:
    """The key id of an export archive's first segment (after its 4-byte length)."""
    head = _read_head(path, _FRAME_HEADER + _HEADER_BYTES)
    return None if head is None else _blob_key_id(head[_FRAME_HEADER:])


def _replace(path: Path, write: Callable[[BinaryIO], None]) -> None:
    """`path` replaced by what `write` writes: `<path>.rotate` created exclusively (mode 0600), flushed,
    renamed over `path`, the directory flushed. A failure removes the `.rotate` file."""
    temporary = path.with_name(path.name + ROTATE_SUFFIX)
    fd = os.open(temporary, _CREATE_FLAGS, FILE_MODE)
    try:
        with os.fdopen(fd, "wb") as out:
            os.fchmod(out.fileno(), FILE_MODE)  # whatever the umask
            write(out)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    fsync_directory(path.parent)


def remove_leftovers(directory: Path) -> int:
    """Remove the `.rotate` files a stopped rotation left in `directory` (a missing directory has none)."""
    removed = 0
    try:
        entries = list(os.scandir(directory))
    except FileNotFoundError:
        return 0
    for entry in entries:
        if entry.name.endswith(ROTATE_SUFFIX) and entry.is_file(follow_symlinks=False):
            Path(entry.path).unlink(missing_ok=True)
            removed += 1
    if removed:
        fsync_directory(directory)
    return removed


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
            stored = {name.storage_path for name in await asyncio.to_thread(store.stored)}
            for row in rows:
                name = row.storage_path
                if name is not None and name in stored:
                    path = cfg.cv_store_dir / name
                    if await asyncio.to_thread(_file_key_id, path) == keys.previous_id:
                        await asyncio.to_thread(_reseal_cv_file, store, name, keys)
                        files += 1
                sealed = None if row.raw_text_enc is None else bytes(row.raw_text_enc)
                if sealed is not None and _blob_key_id(sealed) == keys.previous_id:
                    text = store.decrypt_text(sealed, keys.both, cv_file_id=row.id)
                    resealed = store.encrypt_text(text, [keys.current], cv_file_id=row.id)
                    await conn.execute(_SET_TEXT, {"sealed": resealed, "id": row.id})
                    texts += 1
            after = int(rows[-1].id)
        rewritten["cv_files"] += files
        rewritten["cv_texts"] += texts
        log.info("crypto_rotate_batch", kind="cv_store", after_id=after, files=files, texts=texts)
        await afault_point(AFTER_BATCH)


def _reseal_cv_file(store: CvStore, name: str, keys: _Keys) -> None:
    """The stored file `name` sealed again under the data key, with its own associated data, in place."""
    data = store.read(name, keys.both)  # it checks the name is one `CvStore.write` makes
    blob = crypto.encrypt(keys.current, data, aad=FILE_AAD_PREFIX + name.encode("ascii"))

    def write(out: BinaryIO) -> None:
        out.write(blob)

    _replace(store.root / name, write)


async def _rotate_totp(engine: AsyncEngine, keys: _Keys, rewritten: Counter[str]) -> None:
    """Step 2: the user's TOTP secret."""
    async with engine.begin() as conn:
        found = (await conn.execute(_LOCK_TOTP)).scalar_one_or_none()
        sealed = None if found is None else bytes(found)
        if sealed is None or _blob_key_id(sealed) != keys.previous_id:
            return
        secret = crypto.decrypt(keys.both, sealed, aad=TOTP_AAD)
        await conn.execute(_SET_TOTP, {"sealed": crypto.encrypt(keys.current, secret, aad=TOTP_AAD)})
    rewritten["totp"] += 1
    log.info("crypto_rotate_batch", kind="totp", totp=1)
    await afault_point(AFTER_BATCH)


async def _rotate_exports(
    cfg: AppConfig, engine: AsyncEngine, keys: _Keys, batch: int, rewritten: Counter[str]
) -> None:
    """Step 3: the archives of the `ready` exports, `batch` rows a transaction."""
    after = 0
    while True:
        done = 0
        async with engine.begin() as conn:
            rows = (await conn.execute(_LOCK_EXPORT_ROWS, {"after": after, "batch": batch})).all()
            if not rows:
                return
            for row in rows:
                path = cfg.exports_dir / str(row.storage_path)
                if path.name != row.storage_path:
                    raise RuntimeError(f"export {row.id}: its storage path is no file name")
                if await asyncio.to_thread(_export_key_id, path) != keys.previous_id:
                    continue
                sha256 = await asyncio.to_thread(_reseal_export, path, int(row.id), row.size_bytes, keys)
                await conn.execute(_SET_EXPORT_SHA256, {"sha256": sha256, "id": row.id})
                done += 1
            after = int(rows[-1].id)
        rewritten["exports"] += done
        log.info("crypto_rotate_batch", kind="exports", after_id=after, exports=done)
        await afault_point(AFTER_BATCH)


def _reseal_export(path: Path, export_id: int, size_bytes: int | None, keys: _Keys) -> bytes:
    """The archive `path` sealed again under the data key, in place; the sha256 of the new file."""
    digests: list[bytes] = []

    def write(out: BinaryIO) -> None:
        with path.open("rb") as source:
            plaintext = io.BufferedReader(_Plaintext(iter_decrypted(source, keys.both, export_id=export_id)))
            sealed = write_segments(plaintext, out, keys.current, export_id=export_id)
        if size_bytes is not None and sealed.size_bytes != size_bytes:
            raise RuntimeError(f"export {export_id}: the archive holds another size than its row")
        digests.append(sealed.sha256)

    _replace(path, write)
    return digests[0]


# ---- the verification --------------------------------------------------------------------------------------


async def _remaining(cfg: AppConfig, engine: AsyncEngine, current_id: bytes, batch: int) -> dict[str, int]:
    """The verification pass (module docstring): per hex key id other than the data key's, the ciphertexts
    under it."""
    found: Counter[bytes] = Counter()
    params = {
        "id_bytes": crypto.KEY_ID_BYTES,
        "header_bytes": _HEADER_BYTES,
        "version": crypto.FORMAT_VERSION,
    }
    async with engine.connect() as conn:
        for row in (await conn.execute(_SEALED_KEY_IDS, params)).all():
            found[bytes(row.key_id)] += int(row.n)
    store = CvStore(cfg.cv_store_dir)
    stored = {name.storage_path for name in await asyncio.to_thread(store.stored)}
    async for name in _storage_paths(engine, _CV_ROWS, batch):
        if name in stored and (key := await asyncio.to_thread(_file_key_id, cfg.cv_store_dir / name)):
            found[key] += 1
    async for name in _storage_paths(engine, _EXPORT_ROWS, batch):
        if Path(name).name == name and (
            key := await asyncio.to_thread(_export_key_id, cfg.exports_dir / name)
        ):
            found[key] += 1
    found.pop(current_id, None)
    return {key.hex(): n for key, n in sorted(found.items())}


async def _storage_paths(engine: AsyncEngine, query: TextClause, batch: int) -> AsyncIterator[str]:
    """The `storage_path` of every row `query` pages through (`after`, `batch`), in id order, unlocked."""
    after = 0
    while True:
        async with engine.connect() as conn:
            rows = (await conn.execute(query, {"after": after, "batch": batch})).all()
        if not rows:
            return
        for row in rows:
            if row.storage_path is not None:
                yield str(row.storage_path)
        after = int(rows[-1].id)


# ---- the rotation ------------------------------------------------------------------------------------------


async def rotate(cfg: AppConfig, engine: AsyncEngine, *, batch: int = ROTATION_BATCH) -> RotationReport:
    """Re-encrypt every ciphertext under the mounted `data_key_previous`'s key id with `data_key`, then count
    what remains by key id (module docstring). Without a previous key it only counts."""
    data_keys = crypto.data_keys(cfg)
    current_id = crypto.key_id(data_keys[0])
    rewritten: Counter[str] = Counter(dict.fromkeys(KINDS, 0))
    keys = _Keys(data_keys[0], data_keys[1]) if len(data_keys) > 1 else None
    if keys is not None and keys.previous_id != current_id:
        log.info("crypto_rotate_started", key_id=current_id.hex(), previous_key_id=keys.previous_id.hex())
        for directory in (cfg.cv_store_dir, cfg.exports_dir):
            await asyncio.to_thread(remove_leftovers, directory)
        await _rotate_cv_store(cfg, engine, keys, batch, rewritten)
        await _rotate_totp(engine, keys, rewritten)
        await _rotate_exports(cfg, engine, keys, batch, rewritten)
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
    except SQLAlchemyError as exc:
        log.error("crypto_rotate_failed", error_class=type(exc).__name__)
        code, printed = 1, {"error": "database"}
    except Exception as exc:  # noqa: BLE001 - one JSON line names the class, never the message
        log.error("crypto_rotate_failed", error_class=type(exc).__name__)
        code, printed = 1, {"error": "unexpected", "error_class": type(exc).__name__}
    print(json.dumps(printed))
    return code
