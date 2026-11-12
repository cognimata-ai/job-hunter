"""The data-key rotation (spec §16.8 L5490, §19.8; plan M5 Task 28, Deviations 31 and 39):
`crypto_rotate.rotate` and `jobhunter crypto rotate`.

Every ciphertext kind is sealed under keys made at run time (`os.urandom`): a CV file of the store and its
row's `raw_text_enc`, a pasted CV's text, the user's TOTP secret and a `ready` export of two segments. A
rotation runs with the worker's configuration whose `data_key` and `data_key_previous` are the test's
(`rotation_cfg`), against the shared database; the CLI cases run `cli.main` with a secrets directory of their
own (`rotate_cli`). Nothing printed holds a key, only key ids. Every datum is invented.
"""

import asyncio
import base64
import dataclasses
import hashlib
import io
import json
import os
import shutil
from collections.abc import Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

import psycopg
import pytest
import sqlalchemy as sa
from jobhunter import cli, crypto, crypto_rotate
from jobhunter.auth import totp, users
from jobhunter.clock import utcnow
from jobhunter.config import AppConfig
from jobhunter.crypto_rotate import AFTER_BATCH, ROTATION_BATCH, RotationFailed, RotationReport, rotate
from jobhunter.db.transitions import transition
from jobhunter.ids import uuid7
from jobhunter.privacy.export import SEGMENT_BYTES, iter_decrypted, segment_aad, write_segments
from jobhunter.profile.storage import CvStore
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.faults import SimulatedCrash, no_fault, raising_at
from tests.support.pg import PgServer, server_env

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("clean_db")]

KINDS: Final = ("cv_files", "cv_texts", "totp", "exports")
NOTHING: Final = dict.fromkeys(KINDS, 0)
_CV_ROW: Final = sa.text(
    "INSERT INTO cv_files (sha256, mime, size_bytes, storage_path, extraction_method) "
    "VALUES (:sha, :mime, :size, :path, :method) RETURNING id"
)
_SEAL_TEXT: Final = sa.text("UPDATE cv_files SET raw_text_enc = :enc WHERE id = :i")
_CV_ROWS: Final = sa.text("SELECT id, storage_path, raw_text_enc FROM cv_files ORDER BY id")
_TOTP: Final = sa.text("SELECT totp_secret_enc FROM users WHERE id = 1")
_EXPORT_ROW: Final = sa.text(
    "INSERT INTO exports (status, expires_at) VALUES ('running', now() + interval '7 days') RETURNING id"
)
_EXPORTS: Final = sa.text("SELECT id, storage_path, sha256, size_bytes FROM exports ORDER BY id")
_STALE_SHA256: Final = sa.text("UPDATE exports SET sha256 = :stale WHERE id = :e")
_STORAGE_PATH: Final = sa.text("UPDATE exports SET storage_path = :p WHERE id = :e")
_SEAL_TEXT_PSYCOPG: Final = "UPDATE cv_files SET raw_text_enc = %(enc)s WHERE id = %(i)s"
_ENC_COLUMNS: Final = sa.text(
    "SELECT table_name, column_name FROM information_schema.columns "
    "WHERE table_schema = 'public' AND data_type = 'bytea' AND column_name LIKE '%\\_enc'"
)


@dataclasses.dataclass(frozen=True)
class Cv:
    """A seeded CV: its row, its file's name (`None` for a pasted text), its plaintexts."""

    cv_file_id: int
    storage_path: str | None
    data: bytes | None
    text: str


@dataclasses.dataclass(frozen=True)
class Export:
    export_id: int
    storage_path: str
    zip_bytes: bytes


def b64(key: bytes) -> bytes:
    return base64.b64encode(key)


@pytest.fixture
def volumes(tmp_path: Path) -> tuple[Path, Path]:
    """The worker's CV store and exports directory, both empty, mode 0700."""
    made = (tmp_path / "cv_store", tmp_path / "exports")
    for directory in made:
        directory.mkdir(mode=0o700)
    return made


def rotation_cfg(
    make_config: Any,
    migrated: PgServer,
    volumes: tuple[Path, Path],
    *,
    current: bytes,
    previous: bytes | None,
) -> AppConfig:
    """The worker's configuration with the test's volumes, `current` as `data_key` and `previous`, when given,
    as `data_key_previous`."""
    cv_store, exports = volumes
    base: AppConfig = make_config(
        "worker", migrated, JOBHUNTER_CV_STORE_DIR=str(cv_store), JOBHUNTER_EXPORTS_DIR=str(exports)
    )
    secrets = {name: value for name, value in base.secrets.items() if name != "data_key_previous"}
    secrets["data_key"] = b64(current)
    if previous is not None:
        secrets["data_key_previous"] = b64(previous)
    return dataclasses.replace(base, secrets=MappingProxyType(secrets))


# ---- seeding -----------------------------------------------------------------------------------------------


async def seed_cv(engine: AsyncEngine, store: CvStore, key: bytes, *, pasted: bool = False) -> Cv:
    """A CV row sealed under `key`: a PDF's file and its extracted text, or a pasted text (no file)."""
    text = f"Invented CV {os.urandom(4).hex()}: Python, PostgreSQL, Rust."
    data = None if pasted else b"%PDF-1.7 invented " + os.urandom(64)
    stored = None if data is None else store.write(data, [key])
    params = {
        "sha": hashlib.sha256(text.encode() if data is None else data).digest(),
        "mime": "text/plain" if pasted else "application/pdf",
        "size": len(text) if data is None else len(data),
        "path": None if stored is None else stored.storage_path,
        "method": "pasted" if pasted else "pypdf",
    }
    async with engine.begin() as conn:
        cv_file_id = int((await conn.execute(_CV_ROW, params)).scalar_one())
        sealed = store.encrypt_text(text, [key], cv_file_id=cv_file_id)
        await conn.execute(_SEAL_TEXT, {"enc": sealed, "i": cv_file_id})
    return Cv(cv_file_id, None if stored is None else stored.storage_path, data, text)


async def seed_totp(engine: AsyncEngine, key: bytes) -> str:
    """The single user with TOTP on, its secret sealed under `key`; the secret."""
    secret = totp.new_secret()
    async with engine.begin() as conn:
        await users.create_user(conn, "rotationtest", "Glockenblume-Wasserturm-4", now=utcnow())
        await users.enable_totp(conn, secret, data_key=key)
    return secret


async def seed_export(engine: AsyncEngine, volume: Path, key: bytes) -> Export:
    """A `ready` export of two segments (its "zip" is invented bytes), sealed under `key` as the handler seals
    one, its row moved `running → ready` as the publication moves it."""
    payload = os.urandom(SEGMENT_BYTES + 4096)
    async with engine.begin() as conn:
        export_id = int((await conn.execute(_EXPORT_ROW)).scalar_one())
        name = f"{export_id}-{uuid7()}.jhx"
        with (volume / name).open("wb") as out:
            sealed = write_segments(io.BytesIO(payload), out, key, export_id=export_id)
        (volume / name).chmod(0o600)
        await transition(
            conn,
            "exports",
            export_id,
            ["running"],
            "ready",
            set_={
                "storage_path": name,
                "sha256": sealed.sha256,
                "size_bytes": sealed.size_bytes,
                "purge_generation": 0,
            },
        )
    return Export(export_id, name, payload)


# ---- reading back ------------------------------------------------------------------------------------------


def _id(blob: bytes) -> str:
    return blob[1 : 1 + crypto.KEY_ID_BYTES].hex()


def _segment_ids(path: Path) -> list[str]:
    """The key id of every segment of a stored export, in order."""
    found = []
    with path.open("rb") as source:
        while header := source.read(4):
            blob = source.read(int.from_bytes(header, "big"))
            found.append(_id(blob))
    return found


async def key_ids(engine: AsyncEngine, cfg: AppConfig) -> dict[str, list[str]]:
    """Per kind, the key id (hex) of every ciphertext: each CV file a row names, each `raw_text_enc`, the TOTP
    secret, each segment of each `ready` export."""
    found: dict[str, list[str]] = {kind: [] for kind in KINDS}
    async with engine.connect() as conn:
        for row in (await conn.execute(_CV_ROWS)).all():
            if row.storage_path is not None:
                found["cv_files"].append(_id((cfg.cv_store_dir / row.storage_path).read_bytes()))
            if row.raw_text_enc is not None:
                found["cv_texts"].append(_id(bytes(row.raw_text_enc)))
        if (sealed := (await conn.execute(_TOTP)).scalar_one_or_none()) is not None:
            found["totp"].append(_id(bytes(sealed)))
        for row in (await conn.execute(_EXPORTS)).all():
            found["exports"].extend(_segment_ids(cfg.exports_dir / row.storage_path))
    return found


async def opened(
    engine: AsyncEngine, cfg: AppConfig, keys: Sequence[bytes], cvs: Sequence[Cv], exports: Sequence[Export]
) -> None:
    """Every seeded CV's file and text, and every export, opens with `keys` to its plaintext."""
    store = CvStore(cfg.cv_store_dir)
    async with engine.connect() as conn:
        texts = {int(row.id): bytes(row.raw_text_enc) for row in (await conn.execute(_CV_ROWS)).all()}
        stored = {int(row.id): row for row in (await conn.execute(_EXPORTS)).all()}
    for cv in cvs:
        if cv.storage_path is not None:
            assert store.read(cv.storage_path, keys) == cv.data
        assert store.decrypt_text(texts[cv.cv_file_id], keys, cv_file_id=cv.cv_file_id) == cv.text
    for export in exports:
        path = cfg.exports_dir / export.storage_path
        with path.open("rb") as source:
            assert b"".join(iter_decrypted(source, keys, export_id=export.export_id)) == export.zip_bytes
        row = stored[export.export_id]
        assert row.storage_path == export.storage_path
        assert bytes(row.sha256) == hashlib.sha256(path.read_bytes()).digest()
        assert row.size_bytes == len(export.zip_bytes)
        assert path.stat().st_mode & 0o777 == 0o600


async def totp_opens(engine: AsyncEngine, keys: Sequence[bytes], secret: str) -> bool:
    async with engine.connect() as conn:
        sealed = bytes((await conn.execute(_TOTP)).scalar_one())
    return crypto.decrypt(keys, sealed, aad=users.TOTP_AAD).decode() == secret


def leftovers(cfg: AppConfig) -> list[str]:
    """What a rotation may leave on a volume besides the stored files: none of its temporary files."""
    return sorted(
        name
        for root in (cfg.cv_store_dir, cfg.exports_dir)
        for name in os.listdir(root)
        if not (name.endswith(".bin") or name.endswith(".jhx"))
    )


def inodes(cfg: AppConfig) -> dict[str, int]:
    return {
        name: (root / name).stat().st_ino
        for root in (cfg.cv_store_dir, cfg.exports_dir)
        for name in os.listdir(root)
    }


# ---- rotate ------------------------------------------------------------------------------------------------


async def test_every_ciphertext_kind_is_reencrypted(
    engine: AsyncEngine, make_config: Any, migrated: PgServer, volumes: tuple[Path, Path]
) -> None:
    """Under the previous key: a CV file and its text, a pasted CV's text, the TOTP secret and a two-segment
    export. One rotation seals each under the data key, once; each opens with the data key alone to the same
    plaintext; the export keeps its name and size, its row's sha256 is the new file's; nothing else is left on
    the volumes."""
    old, new = os.urandom(32), os.urandom(32)
    cfg = rotation_cfg(make_config, migrated, volumes, current=new, previous=old)
    store = CvStore(cfg.cv_store_dir)
    cvs = [await seed_cv(engine, store, old), await seed_cv(engine, store, old, pasted=True)]
    secret = await seed_totp(engine, old)
    exports = [await seed_export(engine, cfg.exports_dir, old)]
    assert {kind: set(ids) for kind, ids in (await key_ids(engine, cfg)).items()} == {
        kind: {crypto.key_id(old).hex()} for kind in KINDS
    }

    report = await rotate(cfg, engine)

    assert report == RotationReport(
        rewritten={"cv_files": 1, "cv_texts": 2, "totp": 1, "exports": 1}, remaining_by_key_id={}
    )
    found = await key_ids(engine, cfg)
    assert found == {
        "cv_files": [crypto.key_id(new).hex()],
        "cv_texts": [crypto.key_id(new).hex()] * 2,
        "totp": [crypto.key_id(new).hex()],
        "exports": [crypto.key_id(new).hex()] * 2,
    }
    await opened(engine, cfg, [new], cvs, exports)
    assert await totp_opens(engine, [new], secret)
    assert (cfg.cv_store_dir / str(cvs[0].storage_path)).stat().st_mode & 0o777 == 0o600
    assert leftovers(cfg) == []


async def test_it_is_resumable_and_idempotent(
    engine: AsyncEngine,
    make_config: Any,
    migrated: PgServer,
    volumes: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three CVs and an export under the previous key, rotated one row per batch: a crash after the first
    batch's commit leaves that CV rewritten and the rest as they were; the next run rewrites the rest, not the
    first again; a third run rewrites nothing and touches no file."""
    old, new = os.urandom(32), os.urandom(32)
    cfg = rotation_cfg(make_config, migrated, volumes, current=new, previous=old)
    store = CvStore(cfg.cv_store_dir)
    cvs = [await seed_cv(engine, store, old) for _ in range(3)]
    exports = [await seed_export(engine, cfg.exports_dir, old)]

    monkeypatch.setattr(crypto_rotate, "afault_point", raising_at(AFTER_BATCH))
    with pytest.raises(SimulatedCrash):
        await rotate(cfg, engine, batch=1)
    first = await key_ids(engine, cfg)
    new_id, old_id = crypto.key_id(new).hex(), crypto.key_id(old).hex()
    assert (first["cv_files"], first["cv_texts"]) == ([new_id, old_id, old_id], [new_id, old_id, old_id])
    assert set(first["exports"]) == {old_id}
    rewritten_first = inodes(cfg)[str(cvs[0].storage_path)]

    monkeypatch.setattr(crypto_rotate, "afault_point", no_fault)
    again = await rotate(cfg, engine, batch=1)
    assert again == RotationReport(
        rewritten={"cv_files": 2, "cv_texts": 2, "totp": 0, "exports": 1}, remaining_by_key_id={}
    )
    assert inodes(cfg)[str(cvs[0].storage_path)] == rewritten_first  # the first CV was not rewritten again
    await opened(engine, cfg, [new], cvs, exports)

    before = inodes(cfg)
    assert await rotate(cfg, engine, batch=1) == RotationReport(rewritten=NOTHING, remaining_by_key_id={})
    assert inodes(cfg) == before
    assert leftovers(cfg) == []


async def test_the_verification_counts_remaining_ciphertexts_by_key_id(
    engine: AsyncEngine, make_config: Any, migrated: PgServer, volumes: tuple[Path, Path]
) -> None:
    """Ciphertexts under A (mounted as previous), B and D (not mounted) and C (the data key): the rotation
    rewrites A's; the verification counts B's and D's, each ciphertext once and each under its own key id, and
    leaves the data key's out. A file of the store that no row names (an upload that failed, or a purged CV's
    file before its unlink) is no ciphertext of the application: it is neither rewritten nor counted."""
    a, b, c, d = (os.urandom(32) for _ in range(4))
    cfg = rotation_cfg(make_config, migrated, volumes, current=c, previous=a)
    store = CvStore(cfg.cv_store_dir)
    for key in (a, a, b, c, d):
        await seed_cv(engine, store, key)
    await seed_cv(engine, store, b, pasted=True)
    await seed_totp(engine, b)
    await seed_export(engine, cfg.exports_dir, b)
    orphan = store.write(b"%PDF-1.7 an orphan", [a]).storage_path
    orphan_bytes = (cfg.cv_store_dir / orphan).read_bytes()

    report = await rotate(cfg, engine, batch=ROTATION_BATCH)

    # B's: a CV file and two texts, the TOTP secret, the export (one file of two segments); D's: a CV file and
    # its text.
    assert report == RotationReport(
        rewritten={"cv_files": 2, "cv_texts": 2, "totp": 0, "exports": 0},
        remaining_by_key_id={crypto.key_id(b).hex(): 5, crypto.key_id(d).hex(): 2},
    )
    assert (cfg.cv_store_dir / orphan).read_bytes() == orphan_bytes


async def test_an_export_is_committed_alone(
    migrated: PgServer,
    engine: AsyncEngine,
    tmp_path: Path,
    volumes: tuple[Path, Path],
    make_config: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Two exports under the previous key, the second's archive cut short: the first is sealed again, renamed
    and its row's sha256 committed before the second is read, so the second's `ExportCorrupt` leaves the first
    whole, its row's hash the new file's. The run stops at the second (exit 1) and names it, on stdout and in
    `crypto_rotate_failed`, by its kind, table and row id; its file is as it was, and no `.rotate` is left."""
    old, new = os.urandom(32), os.urandom(32)
    cfg = rotation_cfg(make_config, migrated, volumes, current=new, previous=old)
    first, second = [await seed_export(engine, cfg.exports_dir, old) for _ in range(2)]
    cut = cfg.exports_dir / second.storage_path
    cut.write_bytes(cut.read_bytes()[:-1])
    cut_bytes = cut.read_bytes()
    events: list[dict[str, Any]] = []

    code, printed = await rotate_cli(
        migrated, tmp_path, volumes, monkeypatch, capsys, current=new, previous=old, events=events
    )

    where = {"kind": "exports", "table": "exports", "row_id": second.export_id}
    assert (code, printed) == (1, {"error": "unexpected", "error_class": "ExportCorrupt", **where})
    [failed] = [event for event in events if event["event"] == "crypto_rotate_failed"]
    assert {name: failed[name] for name in ("error_class", *where)} == {
        "error_class": "ExportCorrupt",
        **where,
    }
    await opened(engine, cfg, [new], [], [first])  # the new file, and its row's sha256 is the new file's
    assert _segment_ids(cfg.exports_dir / first.storage_path) == [crypto.key_id(new).hex()] * 2
    assert cut.read_bytes() == cut_bytes
    assert leftovers(cfg) == []


async def test_a_stale_export_hash_under_the_data_key_is_corrected(
    engine: AsyncEngine, make_config: Any, migrated: PgServer, volumes: tuple[Path, Path]
) -> None:
    """An export already under the data key whose row holds another sha256 than its file's (a run stopped
    between the rename and its commit): the pass corrects the row's hash and leaves the file as it is; it
    re-encrypts nothing."""
    old, new = os.urandom(32), os.urandom(32)
    cfg = rotation_cfg(make_config, migrated, volumes, current=new, previous=old)
    export = await seed_export(engine, cfg.exports_dir, new)
    async with engine.begin() as conn:
        await conn.execute(_STALE_SHA256, {"stale": bytes(32), "e": export.export_id})
    files = inodes(cfg)

    report = await rotate(cfg, engine)

    assert report == RotationReport(rewritten=NOTHING, remaining_by_key_id={})
    await opened(engine, cfg, [new], [], [export])  # the row's sha256 is the file's again
    assert inodes(cfg) == files


async def test_a_run_stopped_inside_a_file_leaves_it_whole_and_the_next_clears_leftovers(
    engine: AsyncEngine,
    make_config: Any,
    migrated: PgServer,
    volumes: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash while an export's second segment is sealed: its `.rotate` file, holding the first, is removed
    and the archive and its row are as they were (the CV, in its own committed batch, is done). A run killed
    there would leave the `.rotate` file: planted in both volumes, the next run removes them first, so its own
    `.rotate` names are free, and finishes the export."""
    old, new = os.urandom(32), os.urandom(32)
    cfg = rotation_cfg(make_config, migrated, volumes, current=new, previous=old)
    store = CvStore(cfg.cv_store_dir)
    cv = await seed_cv(engine, store, old)
    export = await seed_export(engine, cfg.exports_dir, old)
    archive = cfg.exports_dir / export.storage_path
    archive_bytes = archive.read_bytes()
    second_segment = segment_aad(export.export_id, 1, True)
    sealing = crypto.encrypt

    def crash_at_the_second_segment(key: bytes, plaintext: bytes, *, aad: bytes) -> bytes:
        if aad == second_segment:
            raise SimulatedCrash("the second segment")
        return sealing(key, plaintext, aad=aad)

    monkeypatch.setattr(crypto, "encrypt", crash_at_the_second_segment)
    with pytest.raises(RotationFailed) as stopped:
        await rotate(cfg, engine)
    assert (stopped.value.kind, stopped.value.row_id, stopped.value.error_class) == (
        "exports",
        export.export_id,
        "SimulatedCrash",
    )
    assert archive.read_bytes() == archive_bytes
    assert leftovers(cfg) == []
    assert (await key_ids(engine, cfg))["cv_files"] == [crypto.key_id(new).hex()]

    monkeypatch.setattr(crypto, "encrypt", sealing)
    for planted in (
        cfg.cv_store_dir / f"{cv.storage_path}.rotate",
        archive.with_name(f"{archive.name}.rotate"),
    ):
        planted.write_bytes(b"left by a killed run")
    report = await rotate(cfg, engine)

    assert report == RotationReport(
        rewritten={"cv_files": 0, "cv_texts": 0, "totp": 0, "exports": 1}, remaining_by_key_id={}
    )
    assert leftovers(cfg) == []
    await opened(engine, cfg, [new], [cv], [export])


async def test_a_ciphertext_sealed_under_the_previous_key_meanwhile_exits_1(
    migrated: PgServer,
    engine: AsyncEngine,
    tmp_path: Path,
    volumes: tuple[Path, Path],
    make_config: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A service that was not restarted seals a CV text under the previous key after the rewrite passed it
    (here, at the last batch's fault point): the verification still finds the previous key's id, so the
    command exits 1 `previous_key_remains`, neither 0 nor 3."""
    old, new = os.urandom(32), os.urandom(32)
    cfg = rotation_cfg(make_config, migrated, volumes, current=new, previous=old)
    store = CvStore(cfg.cv_store_dir)
    cv = await seed_cv(engine, store, old)
    await seed_export(engine, cfg.exports_dir, old)
    arrivals: list[str] = []

    async def seal_under_the_previous_key_at_the_last_batch(name: str) -> None:
        arrivals.append(name)
        if len(arrivals) == 2:  # the CV batch, then the export, the last
            sealed = store.encrypt_text(cv.text, [old], cv_file_id=cv.cv_file_id)
            with psycopg.connect(migrated.dsn("app")) as conn:
                conn.execute(_SEAL_TEXT_PSYCOPG, {"enc": sealed, "i": cv.cv_file_id})

    monkeypatch.setattr(crypto_rotate, "afault_point", seal_under_the_previous_key_at_the_last_batch)
    code, printed = await rotate_cli(
        migrated, tmp_path, volumes, monkeypatch, capsys, current=new, previous=old
    )

    assert arrivals == [AFTER_BATCH, AFTER_BATCH]
    assert (code, printed) == (
        1,
        {
            "error": "previous_key_remains",
            "rewritten": {"cv_files": 1, "cv_texts": 1, "totp": 0, "exports": 1},
            "remaining_by_key_id": {crypto.key_id(old).hex(): 1},
        },
    )


async def test_a_ciphertext_that_does_not_open_is_named_by_its_kind_and_row(
    engine: AsyncEngine, make_config: Any, migrated: PgServer, volumes: tuple[Path, Path]
) -> None:
    """A CV text under the previous key that does not open (sealed for another row: its associated data
    differs) stops the rotation with `RotationFailed` naming its kind, its table and its row id, and the class
    of the error; its row is left as it was."""
    old, new = os.urandom(32), os.urandom(32)
    cfg = rotation_cfg(make_config, migrated, volumes, current=new, previous=old)
    store = CvStore(cfg.cv_store_dir)
    cv = await seed_cv(engine, store, old, pasted=True)
    foreign = store.encrypt_text("another row's text", [old], cv_file_id=cv.cv_file_id + 1)
    async with engine.begin() as conn:
        await conn.execute(_SEAL_TEXT, {"enc": foreign, "i": cv.cv_file_id})

    with pytest.raises(RotationFailed) as stopped:
        await rotate(cfg, engine)

    failed = stopped.value
    assert (failed.kind, failed.table, failed.row_id, failed.error_class) == (
        "cv_texts",
        "cv_files",
        cv.cv_file_id,
        "DecryptError",
    )
    assert str(failed) == f"cv_texts of cv_files row {cv.cv_file_id}: DecryptError"
    assert (await key_ids(engine, cfg))["cv_texts"] == [crypto.key_id(old).hex()]


async def test_the_sealed_column_inventory_is_every_enc_column_of_the_schema(engine: AsyncEngine) -> None:
    """`crypto.SEALED_COLUMNS`, which the rotation's verification and the backup set's key ids both read,
    names every `bytea` column `*_enc` of the schema: a sealed column a migration adds without it fails here,
    before a verification could pass with its ciphertexts uncounted."""
    async with engine.connect() as conn:
        found = {
            (str(row.table_name), str(row.column_name)) for row in (await conn.execute(_ENC_COLUMNS)).all()
        }
    assert found == set(crypto.SEALED_COLUMNS)


@pytest.mark.parametrize("previous", [True, False], ids=["rewrite", "count"])
async def test_an_export_path_that_is_no_file_name_stops_the_rewrite_and_the_count_alike(
    engine: AsyncEngine, make_config: Any, migrated: PgServer, volumes: tuple[Path, Path], previous: bool
) -> None:
    """A `ready` export whose `storage_path` is no plain file name of the exports directory stops the rotation
    in the rewrite (a previous key mounted) and in the count (none), alike, naming the export."""
    old, new = os.urandom(32), os.urandom(32)
    cfg = rotation_cfg(make_config, migrated, volumes, current=new, previous=old if previous else None)
    export = await seed_export(engine, cfg.exports_dir, old)
    async with engine.begin() as conn:
        await conn.execute(_STORAGE_PATH, {"p": f"../{export.storage_path}", "e": export.export_id})

    with pytest.raises(RotationFailed) as stopped:
        await rotate(cfg, engine)

    failed = stopped.value
    assert (failed.kind, failed.table, failed.row_id, failed.error_class) == (
        "exports",
        "exports",
        export.export_id,
        "ValueError",
    )


async def test_without_a_previous_key_it_refuses(
    migrated: PgServer,
    engine: AsyncEngine,
    tmp_path: Path,
    volumes: tuple[Path, Path],
    make_config: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`jobhunter crypto rotate` with an empty `data_key_previous` and a ciphertext under another key than the
    data key: exit 2 `no_previous_key`, the report printed, nothing rewritten. With only the data key's
    ciphertexts it has nothing to do: exit 0."""
    old, new = os.urandom(32), os.urandom(32)
    cfg = rotation_cfg(make_config, migrated, volumes, current=new, previous=None)
    store = CvStore(cfg.cv_store_dir)
    await seed_cv(engine, store, old)
    before = await key_ids(engine, cfg)
    files = inodes(cfg)

    code, printed = await rotate_cli(
        migrated, tmp_path, volumes, monkeypatch, capsys, current=new, previous=None
    )

    assert code == 2
    assert printed == {
        "error": "no_previous_key",
        "rewritten": NOTHING,
        "remaining_by_key_id": {crypto.key_id(old).hex(): 2},
    }
    assert await key_ids(engine, cfg) == before and inodes(cfg) == files

    nothing_to_do = await rotate_cli(
        migrated, tmp_path, volumes, monkeypatch, capsys, current=old, previous=None
    )
    assert nothing_to_do == (0, {"rewritten": NOTHING, "remaining_by_key_id": {}})


async def test_a_set_under_keys_a_and_b_is_recovered_under_c_in_two_passes(
    migrated: PgServer,
    engine: AsyncEngine,
    tmp_path: Path,
    volumes: tuple[Path, Path],
    make_config: Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A restored set holds ciphertexts under A and B, neither the data key C (Deviation 31): a CV and an
    export under A, a CV and the TOTP secret under B. The first pass, A mounted as previous, rewrites A's and
    exits 3 `other_keys_remain`, naming B only; the second, B mounted, rewrites B's and exits 0. Every
    ciphertext is then under C, and each original key's CV, the TOTP secret and the export open with C
    alone."""
    a, b, c = os.urandom(32), os.urandom(32), os.urandom(32)
    cfg = rotation_cfg(make_config, migrated, volumes, current=c, previous=None)
    store = CvStore(cfg.cv_store_dir)
    cvs = [await seed_cv(engine, store, a), await seed_cv(engine, store, b)]
    exports = [await seed_export(engine, cfg.exports_dir, a)]
    secret = await seed_totp(engine, b)

    first = await rotate_cli(migrated, tmp_path, volumes, monkeypatch, capsys, current=c, previous=a)
    assert first == (
        3,
        {
            "error": "other_keys_remain",
            "rewritten": {"cv_files": 1, "cv_texts": 1, "totp": 0, "exports": 1},
            "remaining_by_key_id": {crypto.key_id(b).hex(): 3},
        },
    )
    second = await rotate_cli(migrated, tmp_path, volumes, monkeypatch, capsys, current=c, previous=b)
    assert second == (
        0,
        {"rewritten": {"cv_files": 1, "cv_texts": 1, "totp": 1, "exports": 0}, "remaining_by_key_id": {}},
    )

    found = await key_ids(engine, cfg)
    assert {kind: set(ids) for kind, ids in found.items()} == {
        kind: {crypto.key_id(c).hex()} for kind in KINDS
    }
    await opened(engine, cfg, [c], cvs, exports)
    assert await totp_opens(engine, [c], secret)


# ---- the CLI -----------------------------------------------------------------------------------------------


async def rotate_cli(
    server: PgServer,
    tmp_path: Path,
    volumes: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    current: bytes,
    previous: bytes | None,
    events: list[dict[str, Any]] | None = None,
) -> tuple[int, dict[str, Any]]:
    """`jobhunter crypto rotate` as the worker against `server`, with `current` as its data key and
    `previous`, or an empty file, as `data_key_previous`: its exit status and its one JSON line (and, into
    `events`, the JSON log events it wrote). Nothing it prints or logs holds a key. It runs in a thread of its
    own, as its `asyncio.run` needs one without a running event loop."""
    secrets = tmp_path / f"secrets-{os.urandom(4).hex()}"
    secrets.mkdir()
    for source in server.secrets_dir("worker").iterdir():
        shutil.copyfile(source, secrets / source.name)
    (secrets / "data_key").write_bytes(b64(current))
    (secrets / "data_key_previous").write_bytes(b"" if previous is None else b64(previous))
    for path in secrets.iterdir():
        path.chmod(0o440)
    cv_store, exports = volumes
    env = {
        **server_env(server, "worker"),
        "JOBHUNTER_SECRETS_DIR": str(secrets),
        "JOBHUNTER_CV_STORE_DIR": str(cv_store),
        "JOBHUNTER_EXPORTS_DIR": str(exports),
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("NTFY_TOPIC_URL", raising=False)

    code = await asyncio.to_thread(cli.main, ["crypto", "rotate"])

    out, err = capsys.readouterr()
    for key in (current, previous):
        if key is not None:
            for shown in (key.hex(), b64(key).decode()):
                assert shown not in out and shown not in err
    [line] = out.splitlines()
    printed: dict[str, Any] = json.loads(line)
    if events is not None:
        events.extend(json.loads(logged) for logged in err.splitlines() if logged.startswith("{"))
    return code, printed
