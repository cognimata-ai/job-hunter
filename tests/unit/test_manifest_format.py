"""The deletion manifest's file (spec §19.5; plan M5 Task 9, D2): canonical JSON bytes, the parse that refuses
what a replay must not trust, and the write that never overwrites a manifest's file with other bytes.

The bytes of a content are `canonical_bytes` (sorted keys, compact separators, UTF-8): the file `migrate`
rewrites from a row's `scope` (a `jsonb` column, which keeps no key order) hashes to the `file_sha256` the
shipment matched, so the jsonb round trip is checked against the real column. The thread forms a checkpoint
table's bound reads are pinned against the three writers' own `thread_id` functions.
"""

import copy
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Final, cast, get_args

import pytest
from jobhunter.db.sql import q
from jobhunter.graphs.profile.graph import ThreadKind
from jobhunter.graphs.profile.graph import thread_id as profile_thread_id
from jobhunter.graphs.score.graph import thread_id as cycle_thread_id
from jobhunter.ids import uuid7
from jobhunter.privacy.manifests import (
    MANIFEST_FORMAT,
    THREAD_FORMS,
    ManifestConflict,
    ManifestContent,
    ManifestInvalid,
    Operation,
    canonical_bytes,
    parse_manifest,
    write_manifest_file,
)
from jobhunter.worker.runs import thread_id as run_thread_id
from sqlalchemy.ext.asyncio import AsyncEngine

ADMITTED_AT: Final = "2026-11-10T09:00:00.000001+00:00"
FENCED_AT: Final = "2026-11-10T09:00:00.250000+00:00"


def delete_everything(manifest_uuid: str, *, idempotency_row_id: int) -> ManifestContent:
    """A delete-everything content of every operation type and bound form: the purge's own idempotency record
    kept by id (the request's `idempotency_row_id`), a `retained` map, the `global` fence."""
    return {
        "format": 1,
        "manifest_uuid": manifest_uuid,
        "kind": "delete_everything",
        "admitted_at": ADMITTED_AT,
        "fenced_at": FENCED_AT,
        "max_ids": {"jobs": 812, "api_idempotency": 44, "job_revisions": 1203, "runs": 17, "operations": 90},
        "operations": [
            {"op": "fence", "kind": "global", "key": "global", "job_ids": []},
            {
                "op": "row_delete",
                "table": "api_idempotency",
                "selector": "all",
                "keep": [idempotency_row_id],
                "bound": {"by": "id"},
            },
            {"op": "row_delete", "table": "sessions", "selector": "all", "bound": {"by": "created_at"}},
            {
                "op": "row_delete",
                "table": "user_job_actions",
                "selector": "all",
                "bound": {"by": "parent", "table": "jobs", "column": "job_id"},
            },
            {"op": "row_delete", "table": "checkpoints", "selector": "all", "bound": {"by": "thread"}},
            {
                "op": "row_delete",
                "table": "login_lockouts",
                "selector": "all",
                "bound": {"by": "id", "column": "generation"},
            },
            {
                "op": "field_redact",
                "table": "exports",
                "ids": [3, 1],
                "columns": ["storage_path", "sha256"],
                "marker": {"status": "purged"},
            },
            {"op": "file_remove", "volume": "exports", "keys": ["b.age", "a.age"]},
        ],
        "retained": {"settings_versions": [1], "sources": []},
    }


@pytest.mark.integration
async def test_canonical_bytes_are_stable_and_survive_a_jsonb_round_trip(engine: AsyncEngine) -> None:
    manifest_uuid = str(uuid7())
    request = {"idempotency_row_id": 44}
    content = delete_everything(manifest_uuid, idempotency_row_id=request["idempotency_row_id"])
    data = canonical_bytes(content)

    # Canonical: sorted keys, no whitespace, UTF-8; the content in another key order gives the same bytes.
    assert data == json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    assert data.startswith(b'{"admitted_at":"2026-11-10T09:00:00.000001+00:00","fenced_at":')
    assert b" " not in data
    reordered = cast(ManifestContent, dict(reversed(list(content.items()))))
    reordered["operations"] = [
        cast(Operation, dict(reversed(list(op.items())))) for op in content["operations"]
    ]
    assert list(reordered) != list(content)
    assert canonical_bytes(reordered) == data

    # Through the real column: jsonb keeps neither key order nor whitespace, and the bytes come back the same.
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                q(
                    "INSERT INTO deletion_manifests (manifest_uuid, kind, status, scope, request) "
                    "VALUES (CAST(:u AS uuid), 'delete_everything', 'executing', :scope, :request) "
                    "RETURNING scope, request",
                    jsonb=["scope", "request"],
                ),
                {"u": manifest_uuid, "scope": content, "request": request},
            )
        ).one()
        await conn.rollback()
    assert canonical_bytes(row.scope) == data
    assert hashlib.sha256(canonical_bytes(row.scope)).digest() == hashlib.sha256(data).digest()
    assert row.request == request
    assert row.scope["operations"][1]["keep"] == [request["idempotency_row_id"]]


def test_parse_refuses_a_name_that_is_not_its_uuid() -> None:
    manifest_uuid = str(uuid7())
    content = delete_everything(manifest_uuid, idempotency_row_id=7)
    data = canonical_bytes(content)

    assert parse_manifest(data, file_name=f"{manifest_uuid}.json") == content
    for name in (
        f"{uuid7()}.json",
        f"{manifest_uuid}.json.tmp",
        manifest_uuid,
        f"{manifest_uuid.upper()}.json",
    ):
        with pytest.raises(ManifestInvalid) as refused:
            parse_manifest(data, file_name=name)
        assert refused.value.reason == "name_mismatch", name

    # A uuid that is not in its canonical form names no file of the stream.
    upper = copy.deepcopy(content)
    upper["manifest_uuid"] = manifest_uuid.upper()
    with pytest.raises(ManifestInvalid) as refused:
        parse_manifest(canonical_bytes(upper), file_name=f"{manifest_uuid.upper()}.json")
    assert refused.value.reason == "invalid"


def test_parse_refuses_an_unknown_format_or_op() -> None:
    manifest_uuid = str(uuid7())
    name = f"{manifest_uuid}.json"
    content = delete_everything(manifest_uuid, idempotency_row_id=7)

    def refused(data: bytes) -> str:
        with pytest.raises(ManifestInvalid) as error:
            parse_manifest(data, file_name=name)
        return error.value.reason

    assert refused(b"not json") == "not_json"
    assert refused(b'{"format": 1') == "not_json"
    assert refused(b"\xff\xfe") == "not_json"
    assert refused(b'{"format": NaN}') == "not_json"
    assert refused(b"[1]") == "invalid"

    for unknown in (2, 0, "1", True):
        later: Any = copy.deepcopy(content)
        later["format"] = unknown
        assert refused(canonical_bytes(later)) == "unknown_format", unknown
    assert MANIFEST_FORMAT == 1

    truncate: Any = copy.deepcopy(content)
    truncate["operations"].append({"op": "truncate", "table": "jobs"})
    assert refused(canonical_bytes(truncate)) == "unknown_op"

    # The known ops in a shape the format does not have: a missing field, an extra one, a wrong type.
    missing: Any = copy.deepcopy(content)
    del missing["operations"][1]["bound"]
    extra: Any = copy.deepcopy(content)
    extra["operations"][7]["recursive"] = True
    wrong: Any = copy.deepcopy(content)
    wrong["max_ids"]["jobs"] = "812"
    unknown_kind: Any = copy.deepcopy(content)
    unknown_kind["kind"] = "everything"
    for invalid in (missing, extra, wrong, unknown_kind):
        assert refused(canonical_bytes(invalid)) == "invalid"


def test_write_is_tmp_fsync_rename_and_never_overwrites_other_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "manifests"
    directory.mkdir(mode=0o700)
    manifest_uuid = str(uuid7())
    content = delete_everything(manifest_uuid, idempotency_row_id=7)
    final = directory / f"{manifest_uuid}.json"
    tmp = directory / f"{manifest_uuid}.json.tmp"

    steps: list[tuple[str, ...]] = []
    real_fsync, real_rename = os.fsync, os.rename

    def fsync(fd: int) -> None:
        target = Path(os.readlink(f"/proc/self/fd/{fd}"))
        steps.append(("fsync", "<dir>" if target == directory.resolve() else target.name))
        real_fsync(fd)

    def rename(src: Any, dst: Any) -> None:
        steps.append(("rename", Path(src).name, Path(dst).name))
        real_rename(src, dst)

    # `manifests` calls them through the `os` module, so patching the module's attributes records its calls.
    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "rename", rename)

    digest = write_manifest_file(directory, content)
    assert steps == [("fsync", tmp.name), ("rename", tmp.name, final.name), ("fsync", "<dir>")]
    assert final.read_bytes() == canonical_bytes(content)
    assert digest == hashlib.sha256(canonical_bytes(content)).digest()
    assert not tmp.exists()
    assert final.stat().st_mode & 0o777 == 0o600

    # The same content again (a retry that adopts the written file): its hash, the file left as it is.
    steps.clear()
    assert write_manifest_file(directory, content) == digest
    assert ("rename", tmp.name, final.name) not in steps
    assert final.read_bytes() == canonical_bytes(content)

    # Other bytes under the same uuid are refused: a manifest's file is never overwritten.
    other: Any = copy.deepcopy(content)
    other["max_ids"]["jobs"] = 813
    with pytest.raises(ManifestConflict):
        write_manifest_file(directory, other)
    assert final.read_bytes() == canonical_bytes(content)
    assert sorted(p.name for p in directory.iterdir()) == [final.name]

    # A leftover `.tmp` of a crashed write is replaced, never shipped as the manifest.
    second = delete_everything(str(uuid7()), idempotency_row_id=8)
    leftover = directory / f"{second['manifest_uuid']}.json.tmp"
    leftover.write_bytes(b"half a file")
    write_manifest_file(directory, second)
    assert not leftover.exists()
    assert (directory / f"{second['manifest_uuid']}.json").read_bytes() == canonical_bytes(second)


def owner_ids(thread: str) -> list[tuple[str, int]]:
    """The owner table and id of every thread form that `thread` is of (anchored, as the bound reads it)."""
    found: list[tuple[str, int]] = []
    for form in THREAD_FORMS:
        matched = re.fullmatch(form.pattern, thread)
        if matched is not None:
            found.append((form.owner, int(matched[1])))
    return found


def test_the_thread_forms_are_the_writers_own() -> None:
    """Each checkpoint thread a writer names is of exactly one form, whose first group is its owner's id (the
    `thread` bound); a name of any other form matches none. Every profile `ThreadKind` is covered, so a kind
    the writer gains is either a known form or fails here."""
    assert owner_ids(run_thread_id(17, 2)) == [("runs", 17)]
    assert owner_ids(cycle_thread_id(905)) == [("score_cycles", 905)]
    kinds = get_args(ThreadKind.__value__)
    assert "t" in kinds and "q" in kinds  # read from the alias itself, not a copy of it
    for kind in kinds:
        for subject, operation, attempts in ((31, 4410, 0), (1, 12, 3), (10**12, 2**62, 10)):
            name = profile_thread_id(kind, subject, operation, attempts)
            assert owner_ids(name) == [("operations", operation)], name
    for other in (
        "run-1",
        "run-01-a1",
        "run-1-a1x",
        "cycle-x",
        "profile-1-q2-a1",
        "queries-1-t2-a1",
        "x",
        "",
    ):
        assert owner_ids(other) == [], other
