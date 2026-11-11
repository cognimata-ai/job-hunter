"""The user's data export (spec §20.3, §20.2 L6445, §20.5 L6560, §19.5; plan M5 Task 17, D9, Deviations 18,
19, 48, 52 and 55, Ruling T17-1): a segmented, encrypted zip that any purge ends.

**The request** (`request_export(conn, *, now, defer) -> export_id`), in the caller's transaction:
`active_config FOR SHARE` (`settings.active.lock_active`), then `fences.refuse_under_global_fence` (409
`delete_everything_in_progress` while delete-everything's fence stands, Task 8), then the `exports` row
`running` with `created_at = now` and `expires_at = now + EXPORT_TTL`, and its `export` operation, payload
`{"export_id": id}` (ids only), dedup key `export`. A second request while one export runs meets the unique
index `exports_one_running` and is refused 409 `conflict_state` (detail `export_running`). So is one while an
earlier export's operation is still live although its row was ended (a purge's phase B moves a running export
`purged` before its operation runs): that operation holds the dedup key, and the new export would otherwise be
left without an operation of its own; the old one ends `succeeded` at its next execution.

**The states** (Task 17's table, which live purges, replay and restore all follow; the edges are Task 1's:
`running → ready | failed | purged`, `ready → expired | purged`). Only `running` (at most its own
`<id>-*.jhx.tmp` or an unpublished `<id>-*.jhx`) and `ready` (`<id>-<uuid7>.jhx`, named by `storage_path`)
hold content; `failed`, `expired` and `purged` have `storage_path` NULL and no file. The purge side is
`privacy.deletion_graph`'s (`export_ops` at A2, `purge_exports` in phase B, Task 12) and `privacy.manifests`'
(`EXPORT_CONTENT_FREE` satisfies an `exports.status` marker; the `file_remove` key `<id>-*`): this module adds
none.

**The handler** (`run_export`, kind `export`), with the worker's `OperationDeps` (its configuration's exports
directory and data keys, the CV store, the clock):
1. **Start.** One transaction: the owner row (`lock_operation_owner`), the export row `FOR SHARE`. An export
   no longer `running` (a purge's phase B ended it `purged`, a restore `failed`) ends the operation
   `succeeded` at once, with nothing written but the removal of any `<id>-*` file (a content-free state has
   none; a `ready` export's archive, which no handler meets, is left as it is). Otherwise every `<id>-*` file
   of the export is removed first: what an earlier attempt left (a crash after the rename leaves an archive
   no row names).
2. **Snapshot.** One `REPEATABLE READ, READ ONLY` transaction (`db.snapshot.read_snapshot`) whose first
   statement reads the purge generation `g = max(deletion_manifests.id)` (0 when none) and whether any
   manifest is `executing`. One is: no content is read, and the operation waits
   (`OperationContext.wait_in`, `purge_in_progress`, `PURGE_WAIT`, no attempt spent: a long purge never
   exhausts it); an export still waiting `EXPORT_WAIT_LIMIT` after its `created_at` ends `failed`
   (`purge_in_progress`) instead, since a failed purge keeps its manifest `executing` until it is retried.
   So an export never snapshots data a purge is deleting, the fence-free golden-case deletion's included.
   Otherwise the snapshot's `write_archive` writes the zip (below) to an anonymous temporary file of the
   worker's `/tmp` (the plaintext: it has no name, and it is gone when it is closed or the process dies).
3. **The archive file.** The guard (below), then `<exports_dir>/<id>-<uuid7>.jhx.tmp` created with `O_EXCL`
   (mode 0600) before its transaction commits. A row found no longer `running` ends the operation
   `succeeded`, and no file is created.
4. **Encryption** (`write_segments`) into the open `.tmp`: the zip in segments of `SEGMENT_BYTES` (1 MiB),
   each `crypto.encrypt(data_key, chunk, aad=b"export:<id>:<n>:<0|1>")` (`segment_aad`: its index and
   whether it is the last) framed as a 4-byte big-endian length and the blob; the sha256 of the stored bytes
   and the zip's size are kept; then `fsync`. The plaintext file is closed (removed).
5. **The rename.** The guard, then the rename to `<id>-<uuid7>.jhx` and an `fsync` of the directory before
   its transaction commits. A row found no longer `running` ends the operation `succeeded` and its files are
   removed (no row names them).
6. **Publication**, the final transaction (§20.3 L6508): `active_config FOR SHARE` (every purge's A0 commits
   its manifest under `active_config FOR UPDATE`, so the reads below see a manifest committed before this
   lock), the owner row, the export row `FOR UPDATE`. An export no longer `running` (a purge ended it first,
   D9) → `succeeded`, its `<id>-*` files removed (no row names them). An active purge fence, or a manifest
   with an id above `g` (a purge opened after the snapshot, fenced or not) → `running → purged`, the file
   removed. Else `running → ready` with `storage_path`, `sha256` (of the stored file), `size_bytes` (the
   zip's) and `purge_generation = g`, and `succeeded`. A purge opened after this commit names the `ready`
   export at its A2 and deletes it (§20.2: the export became `ready` before the fence, or it saw it).

**The guard** (Ruling T17-1 (c): no `<id>-` name is ever made for an export a purge has ended, else that
purge's `file_remove` postcondition could fail). Every name the handler makes, the `.tmp`'s creation (step 3)
and its rename (step 5), is made inside a transaction that holds the owner row and the export row `FOR SHARE`
and found it `running`. A purge's phase B moves the row `running → purged` (`deletion_graph.purge_exports`)
and lists and unlinks `<id>-*` only after its own commit (`manifests.remove_files`). So that update either
committed first, and the guard finds the row `purged` and makes no name, or it waits for the guard's
transaction, and the name exists before the purge's commit, hence before its listing. The rename is guarded
too, not only the creation: an unguarded rename could fall between the purge's listing (which saw the
`.tmp`) and its unlink (which then misses the new `.jhx`). The handler keeps writing into its open file
whatever happens to its name. The failure hook's removal holds the operation row, which a guard takes too, and
ends the claim the guard checks; retention removes only files a day old. Every content-free row's files are
removed inside the transaction that ends it here: none of them is named, so a rolled-back transaction loses
nothing.

**The archive** (`write_archive(conn, sink, *, export_id, cv_store, keys)`, in the caller's snapshot): the
files of `EXPORT_FILES` and the `cv_files/` directory. Each JSON file is an array, in id order, streamed row
by row: `profile_versions.json` (the retained versions' contact-stripped profiles),
`cv_files/<cv_file_id>.<ext>` (the decrypted originals of the CV files that still exist, `CvStore.read`; a
pasted CV, which has no file, as its decrypted text in `.txt`; a file its row names that is gone meanwhile is
left out), `settings_versions.json`, `jobs.json` (the canonical fields, the user's actions and note),
`scores.json` (every published result, current and historical, with its evidence, its score instances, its
attempt's critic notes and the versions it was made with), `feedback.json`, `golden_cases.json`, `runs.json`
(with their reports) and `README.txt` (what each file holds, the 7 days, the pointer to PRIVACY.md). A job an
active purge fence matches (`is_fenced`) is left out of every file that names jobs (L6560: no export reads a
fenced job).

**The reader** (`iter_decrypted(source, keys, *, export_id)`): the plaintext of an open stored file,
segment by segment (an open file, so a reader that opened it before a purge's unlink reads on); each segment
opens only under its own index and final flag and this export's id, so a truncated, reordered, foreign or
tampered file raises `ExportCorrupt`.

**The failure hook** (`export_failed`, registered with the handler): in every transaction that ends the
operation `failed` (a `failed` finish, an exhausting requeue, reclaim or takeover), the payload's export
`running → failed`, a row in any other state left as it is (a purge or the publication moved it first), and
its files removed unless it is `ready` (an attempt's `.tmp` and an unpublished archive: nothing names them).
So no `running` row outlives its operation, and `exports_one_running` never blocks the next export.

**Retention** (`retain_exports`, the rule `exports` of `privacy.retention`, each step a transaction of its own
that starts with the epoch fence): a `ready` export past `expires_at` → `expired`, `storage_path` NULL, its
file removed after that commit (the row named it until then); a `running` export whose operation is neither
`pending` nor `running` (lost without its hook, e.g. cancelled) → `failed`, its files removed; a file of the
exports directory of the form `<id>-*.jhx` or `.jhx.tmp`, written more than `ORPHAN_AGE` ago, that no `ready`
row names → removed, as `cv_store_orphans` does for CV files; `expired`, `failed` and `purged` rows deleted
`EXPORT_ROWS_KEPT` after `expires_at` (Deviation 48).

**The exports volume** of the hook and the retention rule, which run without the handler's dependencies (the
operations reconciler's reclaim, the takeover sweep, the daily pass), is configured once by the worker's start
(`configure(cfg)`, step 4, before the takeover sweeps), as `sources.http.configure` is. Where it is not
configured (a process that never starts a worker), the hook and the rule change rows only, and log that they
left the files to the next configured pass.

Fault points (test env only): `privacy.export.after_snapshot` (the zip written, the snapshot closed, nothing
on the volume), `before_tmp` (in step 3's transaction, the row held, no file yet), `after_tmp` (the `.tmp`
created and committed), `before_rename` (in step 5's transaction, the row held, not renamed yet) and
`after_rename` (the archive renamed and committed, not yet published). Ids, statuses, counts and error classes
only reach a log line.
"""

import hashlib
import json
import os
import re
import stat
import tempfile
import zipfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import BinaryIO, Final

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.sql.elements import TextClause

from jobhunter import crypto
from jobhunter.config import AppConfig
from jobhunter.db.snapshot import read_snapshot
from jobhunter.db.sql import is_unique_violation, q
from jobhunter.db.transitions import transition, transition_where
from jobhunter.errors import Refusal
from jobhunter.faults import afault_point
from jobhunter.ids import uuid7
from jobhunter.observability.logging import get_logger
from jobhunter.privacy.deletion_graph import export_files_key
from jobhunter.privacy.fences import NO_OPEN_FENCE_SQL, PURGE_IN_PROGRESS, refuse_under_global_fence
from jobhunter.privacy.manifests import remove_files
from jobhunter.profile.storage import CvStore, fsync_directory
from jobhunter.settings.active import lock_active
from jobhunter.types import JSONValue
from jobhunter.worker.epoch import fence_epoch
from jobhunter.worker.operations import (
    OperationContext,
    create_operation,
    lock_operation_owner,
    register_operation_handler,
)
from jobhunter.worker.tasks import Deferrer

KIND: Final = "export"
DEDUP_KEY: Final = "export"
EXPORT_TTL: Final = timedelta(days=7)
SEGMENT_BYTES: Final = 1 << 20
EXPORT_FILES: Final = (
    "profile_versions.json",
    "settings_versions.json",
    "jobs.json",
    "scores.json",
    "feedback.json",
    "golden_cases.json",
    "runs.json",
    "README.txt",
)
CV_FILES_DIR: Final = "cv_files/"
# How long an export waits out an executing purge before it fails, and how long each wait lasts.
EXPORT_WAIT_LIMIT: Final = timedelta(hours=24)
PURGE_WAIT: Final = timedelta(minutes=1)
# The retention rule's windows: terminal rows after `expires_at`, unnamed archives after they were written.
EXPORT_ROWS_KEPT: Final = timedelta(days=30)
ORPHAN_AGE: Final = timedelta(days=1)
EXPORT_RUNNING: Final = "export_running"
INVALID_PAYLOAD: Final = "invalid_payload"
ARCHIVE_SUFFIX: Final = ".jhx"
TMP_SUFFIX: Final = ".jhx.tmp"
FILE_MODE: Final = 0o600

AFTER_SNAPSHOT: Final = "privacy.export.after_snapshot"
BEFORE_TMP: Final = "privacy.export.before_tmp"
AFTER_TMP: Final = "privacy.export.after_tmp"
BEFORE_RENAME: Final = "privacy.export.before_rename"
AFTER_RENAME: Final = "privacy.export.after_rename"

_ONE_RUNNING: Final = "exports_one_running"
_FRAME_HEADER: Final = 4
# What `crypto.encrypt` adds to a segment: the version byte, the key id, the nonce and the tag.
_SEAL_OVERHEAD: Final = 1 + crypto.KEY_ID_BYTES + crypto.NONCE_BYTES + crypto.TAG_BYTES
_MAX_BLOB: Final = SEGMENT_BYTES + _SEAL_OVERHEAD
_BIGINT_END: Final = 2**63
_CREATE_FLAGS: Final = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
# An archive name of the exports directory: `<id>-<anything>.jhx`, or its `.tmp`.
_ARCHIVE_NAME: Final = re.compile(r"[1-9][0-9]*-[^/]+\.jhx(?:\.tmp)?")
_CV_EXTENSIONS: Final = MappingProxyType(
    {
        "application/pdf": "pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
        "text/plain": "txt",
    }
)

log = get_logger("jobhunter.privacy.export")

# The exports volume of the hook and the retention rule (module docstring); `configure` sets it.
_volume: Path | None = None


def configure(cfg: AppConfig) -> None:
    """Process-wide: the exports directory of `cfg`, which the failure hook and the retention rule remove
    files from (module docstring). The worker's start calls it before the takeover sweeps."""
    global _volume
    _volume = cfg.exports_dir


# ---- the segments ------------------------------------------------------------------------------------------


class ExportCorrupt(Exception):
    """A stored archive that does not open as this export's: truncated, reordered, foreign or tampered. Its
    message is one of a few fixed texts: never content, never a key id."""


@dataclass(frozen=True)
class SealedArchive:
    """What `write_segments` wrote: the sha256 of the stored bytes and the size of the plaintext."""

    sha256: bytes
    size_bytes: int


def segment_aad(export_id: int, n: int, last: bool) -> bytes:
    """The associated data of segment `n` of export `export_id`: `export:<id>:<n>:<last>`."""
    return f"export:{export_id}:{n}:{int(last)}".encode()


def _read_up_to(source: BinaryIO, size: int) -> bytes:
    """Up to `size` bytes of `source`, fewer only at its end."""
    parts: list[bytes] = []
    wanted = size
    while wanted:
        part = source.read(wanted)
        if not part:
            break
        parts.append(part)
        wanted -= len(part)
    return b"".join(parts)


def write_segments(source: BinaryIO, sink: BinaryIO, key: bytes, *, export_id: int) -> SealedArchive:
    """Seal what `source` holds into `sink` (module docstring, step 4): segments of `SEGMENT_BYTES`, the last
    one shorter (an empty source is one empty final segment), each framed as its length and its blob."""
    digest = hashlib.sha256()
    size = 0
    n = 0
    chunk = _read_up_to(source, SEGMENT_BYTES)
    while True:
        following = _read_up_to(source, SEGMENT_BYTES)
        last = not following
        blob = crypto.encrypt(key, chunk, aad=segment_aad(export_id, n, last))
        frame = len(blob).to_bytes(_FRAME_HEADER, "big") + blob
        sink.write(frame)
        digest.update(frame)
        size += len(chunk)
        if last:
            return SealedArchive(digest.digest(), size)
        chunk, n = following, n + 1


def iter_decrypted(source: BinaryIO, keys: Sequence[bytes], *, export_id: int) -> Iterator[bytes]:
    """The plaintext of export `export_id`'s stored file `source`, one segment at a time (module docstring,
    "The reader"): `ExportCorrupt` for a truncated, reordered, foreign or tampered file, once the segments
    before the fault have been returned."""
    header = _read_up_to(source, _FRAME_HEADER)
    n = 0
    while True:
        if len(header) < _FRAME_HEADER:
            raise ExportCorrupt("the archive is truncated")
        length = int.from_bytes(header, "big")
        if not _SEAL_OVERHEAD <= length <= _MAX_BLOB:
            raise ExportCorrupt("a segment of the archive is malformed")
        blob = _read_up_to(source, length)
        if len(blob) < length:
            raise ExportCorrupt("the archive is truncated")
        header = _read_up_to(source, _FRAME_HEADER)
        last = not header
        try:
            plain = crypto.decrypt(keys, blob, aad=segment_aad(export_id, n, last))
        except crypto.DecryptError:
            raise ExportCorrupt("a segment of the archive does not open") from None
        yield plain
        if last:
            return
        n += 1


# ---- the archive -------------------------------------------------------------------------------------------

# A job no active purge fence matches (L6560); the uncorrelated `NOT EXISTS` spares every row the function
# while no fence stands (D21).
_NOT_FENCED: Final = f"({NO_OPEN_FENCE_SQL} OR NOT is_fenced(j.id))"
_PROFILE_VERSIONS: Final = q(
    "SELECT jsonb_build_object('id', v.id, 'version_no', v.version_no, 'confirmed_at', v.confirmed_at, "
    "'cv_file_id', v.cv_file_id, 'synthetic_eval', v.synthetic_eval, 'profile', v.profile_json) "
    "FROM profile_versions v WHERE v.profile_json IS NOT NULL ORDER BY v.id"
)
# The CV files that still exist: an upload's file, or a paste's sealed text.
_CV_FILES: Final = q(
    "SELECT c.id, c.mime, c.storage_path, c.raw_text_enc FROM cv_files c WHERE c.purged_at IS NULL "
    "AND (c.storage_path IS NOT NULL OR (c.extraction_method = 'pasted' AND c.raw_text_enc IS NOT NULL)) "
    "ORDER BY c.id"
)
_SETTINGS_VERSIONS: Final = q(
    "SELECT jsonb_build_object('id', s.id, 'version_no', s.version_no, 'schema_version', s.schema_version, "
    "'created_at', s.created_at, 'base_version_id', s.base_version_id, "
    "'effect_classes', to_jsonb(s.effect_classes), "
    "'acknowledged_warnings', to_jsonb(s.acknowledged_warnings), "
    "'eval_override_ack', s.eval_override_ack, 'settings', s.payload) FROM settings_versions s ORDER BY s.id"
)
_JOBS: Final = q(
    "SELECT jsonb_build_object('id', j.id, 'merged_into_job_id', j.merged_into_job_id, "  # noqa: S608
    "'current_revision_id', j.current_revision_id, 'synthetic_eval', j.synthetic_eval, "
    "'company', j.company_display, 'title', j.title, 'seniority_tokens', to_jsonb(j.seniority_tokens), "
    "'locations', j.locations, 'location_kind', j.location_kind, "
    "'work_mode_possible', to_jsonb(j.work_mode_possible), 'work_mode_source', j.work_mode_source, "
    "'remote_countries', to_jsonb(j.remote_countries), 'employment_types', to_jsonb(j.employment_types), "
    "'employment_source', j.employment_source, 'salary', j.salary, 'industry', j.industry_structured, "
    "'seniority', j.structured_seniority, 'ad_language', j.ad_language, 'completeness', j.completeness, "
    "'posted_at', j.posted_at_effective, 'posted_precision', j.posted_precision, "
    "'first_seen_at', j.first_seen_at, 'last_seen_at', j.last_seen_at, 'availability', j.availability, "
    "'availability_changed_at', j.availability_changed_at, 'sources', to_jsonb(j.source_ids), "
    "'duplicate_group_id', j.duplicate_group_id, "
    "'user_actions', CASE WHEN a.job_id IS NOT NULL THEN jsonb_build_object('seen_at', a.seen_at, "
    "'saved_at', a.saved_at, 'applied_at', a.applied_at, 'hidden_at', a.hidden_at, "
    "'updated_at', a.updated_at) END, 'note', a.note) "
    f"FROM jobs j LEFT JOIN user_job_actions a ON a.job_id = j.id WHERE {_NOT_FENCED} ORDER BY j.id"
)
_EVIDENCE: Final = (
    "(SELECT coalesce(jsonb_agg(jsonb_build_object('subject', e.subject, 'source_kind', e.source_kind, "
    "'field_name', e.field_name, 'quote', e.quote, 'span_start', e.span_start, 'span_end', e.span_end) "
    "ORDER BY e.id), CAST('[]' AS jsonb)) FROM result_evidence e WHERE e.llm_result_id = r.id)"
)
_INSTANCES: Final = (
    "(SELECT coalesce(jsonb_agg(jsonb_build_object('id', d.id, 'scope_version_id', d.scope_version_id, "
    "'det_algorithm_version', d.det_algorithm_version, 'is_current', d.is_current, "
    "'superseded_at', d.superseded_at, 'score_low', d.score_low, 'score_mid', d.score_mid, "
    "'score_high', d.score_high, 'coverage', d.coverage, 'confidence', d.confidence, "
    "'det_criteria', d.det_criteria, 'knockouts', d.knockouts, 'industry_match', d.industry_match, "
    "'caps_applied', d.caps_applied, 'det_inputs', d.det_inputs, 'computed_at', d.computed_at) "
    "ORDER BY d.id), CAST('[]' AS jsonb)) FROM det_results d WHERE d.llm_result_id = r.id)"
)
_CRITIC_NOTES: Final = (
    "(SELECT coalesce(jsonb_agg(jsonb_build_object('round', n.round, 'effort', n.effort, "
    "'triggers', to_jsonb(n.triggers), 'verdict_returned', n.verdict_returned, "
    "'derived_outcome', n.derived_outcome, 'review', n.review_json, 'validation', n.validation, "
    "'created_at', n.created_at) ORDER BY n.round), CAST('[]' AS jsonb)) "
    "FROM critic_notes n WHERE n.attempt_id = r.attempt_id)"
)
# `current`: the result of the active tuple on its job's current revision.
_SCORES: Final = q(
    "SELECT jsonb_build_object('id', r.id, 'target_id', r.target_id, 'job_id', t.job_id, "  # noqa: S608
    "'job_revision_id', r.job_revision_id, 'llm_tuple_id', t.llm_tuple_id, "
    "'current', coalesce(t.llm_tuple_id = a.llm_tuple_id "
    "AND j.current_revision_id = r.job_revision_id, false), "
    "'versions', jsonb_build_object('profile_version_id', r.profile_version_id, "
    "'rubric_version_id', r.rubric_version_id, 'prompt_schema_version_id', r.prompt_schema_version_id, "
    "'model_policy_id', r.model_policy_id, 'evaluation_policy_id', r.evaluation_policy_id), "
    "'status', r.status, 'criteria', r.criteria, 'job_facts', r.job_facts, "
    "'knockout_candidates', r.knockout_candidates, 'fact_confirmations', r.fact_confirmations, "
    "'not_a_job_ad', r.not_a_job_ad, 'injection_suspected', r.injection_suspected, "
    "'nulled_criteria', to_jsonb(r.nulled_criteria), 'truncated_fields', to_jsonb(r.truncated_fields), "
    "'critic_outcome', r.critic_outcome, 'critic_injection', r.critic_injection, "
    "'revisions_used', r.revisions_used, 'summary_en', r.summary_en, 'summary_de', r.summary_de, "
    f"'published_at', r.published_at, 'evidence', {_EVIDENCE}, 'scores', {_INSTANCES}, "
    f"'critic_notes', {_CRITIC_NOTES}) "
    "FROM llm_results r JOIN score_targets t ON t.id = r.target_id JOIN jobs j ON j.id = t.job_id "
    "CROSS JOIN (SELECT llm_tuple_id FROM active_config WHERE id = 1) a "
    f"WHERE {_NOT_FENCED} ORDER BY r.id"
)
_FEEDBACK: Final = q(
    "SELECT jsonb_build_object('id', f.id, 'job_id', f.job_id, 'job_revision_id', f.job_revision_id, "  # noqa: S608
    "'profile_version_id', f.profile_version_id, 'llm_result_id', f.llm_result_id, "
    "'det_result_id', f.det_result_id, 'kind', f.kind, 'label', f.label, 'comment', f.comment, "
    "'score_snapshot', f.score_snapshot, 'created_at', f.created_at) FROM score_feedback f "
    f"WHERE f.job_id IS NULL OR {NO_OPEN_FENCE_SQL} OR NOT is_fenced(f.job_id) ORDER BY f.id"
)
_GOLDEN_CASES: Final = q(
    "SELECT jsonb_build_object('id', g.id, 'job_id', v.job_id, 'job_revision_id', g.job_revision_id, "  # noqa: S608
    "'profile_version_id', g.profile_version_id, 'scope_version_id', g.scope_version_id, "
    "'label', g.label, 'comment', g.comment, 'expected_knockouts', to_jsonb(g.expected_knockouts), "
    "'source', g.source, 'labelled_from_result_id', g.labelled_from_result_id, "
    "'det_inputs_snapshot', g.det_inputs_snapshot, 'det_input_sources', to_jsonb(g.det_input_sources), "
    "'is_canary', g.is_canary, 'active', g.active, 'created_at', g.created_at, 'updated_at', g.updated_at) "
    "FROM golden_cases g JOIN job_revisions v ON v.id = g.job_revision_id "
    f"WHERE {NO_OPEN_FENCE_SQL} OR NOT is_fenced(v.job_id) ORDER BY g.id"
)
_RUNS: Final = q(
    "SELECT jsonb_build_object('id', r.id, 'kind', r.kind, 'trigger', r.trigger, "
    "'scheduled_slot', r.scheduled_slot, 'status', r.status, 'status_reason', r.status_reason, "
    "'include_paid', r.include_paid, 'settings_version_id', r.settings_version_id, "
    "'scope_version_id', r.scope_version_id, 'llm_tuple_id', r.llm_tuple_id, 'queued_at', r.queued_at, "
    "'started_at', r.started_at, 'finished_at', r.finished_at, 'report', r.report) FROM runs r ORDER BY r.id"
)
# The JSON files after `profile_versions.json` and the CV files, in the archive's order.
_LISTS: Final = (
    ("settings_versions.json", _SETTINGS_VERSIONS),
    ("jobs.json", _JOBS),
    ("scores.json", _SCORES),
    ("feedback.json", _FEEDBACK),
    ("golden_cases.json", _GOLDEN_CASES),
    ("runs.json", _RUNS),
)
_README: Final = """\
Job Hunter: your data export {export_id}

This archive is a copy of your data as Job Hunter held it when the export ran. Each JSON file is a list of
records in the order they were created; times are UTC (ISO 8601).

profile_versions.json   your confirmed profile versions that are still kept, without your contact details
cv_files/               the CV files you uploaded that still exist, decrypted (a pasted CV as a .txt file)
settings_versions.json  every saved version of your settings
jobs.json               the job ads collected for you: their canonical fields, your actions (seen, saved,
                        applied, hidden) and your notes
scores.json             every published score, current and historical, with its evidence, the critic's
                        notes and the versions it was made with
feedback.json           the feedback you gave on scores
golden_cases.json       the cases you labelled for the evaluation of the scoring
runs.json               the collection runs with their reports
README.txt              this file

The export is available for 7 days after you requested it and is deleted then, or at once when data is
deleted (a purge), because it may hold that data; a job that a deletion was still removing when the export
ran is left out of it. How Job Hunter keeps, deletes and exports your data is described in its privacy
statement, PRIVACY.md (Settings, Privacy & data).
"""


async def _write_items(conn: AsyncConnection, archive: zipfile.ZipFile, name: str, items: TextClause) -> None:
    """The JSON array of `items` (one JSON object per row) as the archive's file `name`, row by row."""
    with archive.open(name, "w") as entry:
        entry.write(b"[")
        separator = b"\n"
        async with conn.stream_scalars(items) as rows:
            async for item in rows:
                entry.write(separator + json.dumps(item, ensure_ascii=False).encode())
                separator = b",\n"
        entry.write(b"\n]\n" if separator == b",\n" else b"]\n")


async def _write_cv_files(
    conn: AsyncConnection, archive: zipfile.ZipFile, cv_store: CvStore, keys: Sequence[bytes]
) -> int:
    """The `cv_files/` directory with each CV file that still exists, decrypted (module docstring); how
    many."""
    archive.mkdir(CV_FILES_DIR.rstrip("/"))
    written = 0
    async with conn.stream(_CV_FILES) as rows:
        async for row in rows:
            cv_file_id = int(row.id)
            if row.storage_path is not None:
                try:
                    original = cv_store.read(row.storage_path, keys)
                except FileNotFoundError:
                    # Its row named it when the snapshot was taken; retention removed it since. The id is
                    # `file_id`: the §16.8 redaction masks every `cv*` key.
                    log.info("export_cv_file_gone", file_id=cv_file_id)
                    continue
            else:
                original = cv_store.decrypt_text(row.raw_text_enc, keys, cv_file_id=cv_file_id).encode()
            archive.writestr(f"{CV_FILES_DIR}{cv_file_id}.{_CV_EXTENSIONS[row.mime]}", original)
            written += 1
    return written


async def write_archive(
    conn: AsyncConnection, sink: BinaryIO, *, export_id: int, cv_store: CvStore, keys: Sequence[bytes]
) -> None:
    """The zip of the caller's snapshot `conn` into `sink` (module docstring, "The archive"); `keys` open the
    CV files."""
    with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        await _write_items(conn, archive, "profile_versions.json", _PROFILE_VERSIONS)
        cv_files = await _write_cv_files(conn, archive, cv_store, keys)
        for name, items in _LISTS:
            await _write_items(conn, archive, name, items)
        archive.writestr("README.txt", _README.format(export_id=export_id))
    log.info("export_archive_written", export_id=export_id, cv_files=cv_files)


# ---- the request -------------------------------------------------------------------------------------------

_INSERT_EXPORT: Final = q(
    "INSERT INTO exports (status, created_at, expires_at) VALUES ('running', :now, :expires) RETURNING id"
)


def export_running() -> Refusal:
    """The refusal of a request while an export runs (module docstring)."""
    return Refusal(409, "conflict_state", "An export is already running", detail=EXPORT_RUNNING)


async def request_export(conn: AsyncConnection, *, now: datetime, defer: Deferrer | None) -> int:
    """Request an export at `now` in the caller's transaction (module docstring, "The request"); its id."""
    await lock_active(conn, share=True)
    await refuse_under_global_fence(conn)
    try:
        inserted = await conn.execute(_INSERT_EXPORT, {"now": now, "expires": now + EXPORT_TTL})
    except sa.exc.IntegrityError as exc:
        if is_unique_violation(exc, _ONE_RUNNING):
            raise export_running() from None
        raise
    export_id = int(inserted.scalar_one())
    _, created = await create_operation(
        conn, kind=KIND, payload={"export_id": export_id}, dedup_key=DEDUP_KEY, defer=defer
    )
    if not created:  # an earlier export's operation is still live (module docstring)
        raise export_running()
    log.info("export_requested", export_id=export_id)
    return export_id


# ---- the failure hook --------------------------------------------------------------------------------------

_STATUS: Final = q("SELECT status FROM exports WHERE id = :e")


def _export_id(payload: Mapping[str, JSONValue]) -> int | None:
    value = payload.get("export_id")
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < _BIGINT_END:
        return None
    return value


def _remove_configured(export_id: int) -> int | None:
    """Remove the export's `<id>-*` files from the configured volume: how many, or `None` when no volume is
    configured (module docstring)."""
    if _volume is None:
        log.warning("export_files_left", export_id=export_id)
        return None
    return remove_files(_volume, [export_files_key(export_id)])


async def export_failed(
    conn: AsyncConnection, operation_id: int, payload: Mapping[str, JSONValue], error_code: str | None
) -> None:
    """The `export` operation's failure hook (module docstring): the export `running → failed`, its files
    removed unless it is `ready`. It writes the export row after the operation row the finishing transaction
    holds (steps 5 and 10 of the lock schedule) and takes no earlier lock."""
    export_id = _export_id(payload)
    if export_id is None:
        log.error("export_hook_payload_invalid", operation_id=operation_id)
        return  # no export a `bigint` names: the operation still ends `failed`
    moved = await transition_where(
        conn, "exports", ["running"], "failed", where="t.id = :e", params={"e": export_id}
    )
    status = "failed" if moved else (await conn.execute(_STATUS, {"e": export_id})).scalar_one_or_none()
    removed = None if status in (None, "ready") else _remove_configured(export_id)
    if moved or removed:
        log.warning(
            "export_failed",
            operation_id=operation_id,
            export_id=export_id,
            error_code=error_code,
            files_removed=removed,
        )


# ---- the handler -------------------------------------------------------------------------------------------

_SHARE_EXPORT: Final = q("SELECT status, created_at FROM exports WHERE id = :e FOR SHARE")
_LOCK_EXPORT: Final = q("SELECT status FROM exports WHERE id = :e FOR UPDATE")
# The purge generation and whether a purge is executing: the snapshot's first statement.
_GENERATION: Final = q(
    "SELECT (SELECT coalesce(max(id), 0) FROM deletion_manifests) AS generation, "
    "EXISTS (SELECT 1 FROM deletion_manifests WHERE status = 'executing') AS executing"
)
# What ends an export `purged` at its publication: an active fence, or a manifest above the generation.
_PURGED_SINCE: Final = q(
    "SELECT EXISTS (SELECT 1 FROM purge_fences WHERE released_at IS NULL) AS fenced, "
    "EXISTS (SELECT 1 FROM deletion_manifests WHERE id > :g) AS newer"
)


def _remove(volume: Path, export_id: int) -> int:
    return remove_files(volume, [export_files_key(export_id)])


def _create_tmp(volume: Path, export_id: int) -> tuple[Path, int]:
    """A new `<id>-<uuid7>.jhx.tmp` of `volume`, created exclusively, mode 0600: its path and its open
    descriptor."""
    path = volume / f"{export_id}-{uuid7()}{TMP_SUFFIX}"
    return path, os.open(path, _CREATE_FLAGS, FILE_MODE)


def _discard(path: Path, fd: int) -> None:
    os.close(fd)
    path.unlink(missing_ok=True)


def _seal(plain: BinaryIO, tmp: Path, fd: int, key: bytes, export_id: int) -> SealedArchive:
    """Step 4: the zip encrypted into the open `.tmp` and flushed to the disk; what was written. A failure
    removes the `.tmp`."""
    try:
        with os.fdopen(fd, "wb") as out:
            os.fchmod(out.fileno(), FILE_MODE)  # whatever the umask
            plain.seek(0)
            sealed = write_segments(plain, out, key, export_id=export_id)
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return sealed


def _plaintext() -> BinaryIO:
    """An anonymous temporary file of the system's temporary directory (the worker's `/tmp`) for the zip."""
    return tempfile.TemporaryFile()


async def _snapshot(
    ctx: OperationContext, plain: BinaryIO, export_id: int, cv_store: CvStore, keys: Sequence[bytes]
) -> int | None:
    """Step 2: the zip of one snapshot into `plain` and its purge generation; `None`, nothing read, while a
    purge is executing."""
    async with read_snapshot(ctx.engine) as snap:
        state = (await snap.execute(_GENERATION)).one()
        if state.executing:
            return None
        await write_archive(snap, plain, export_id=export_id, cv_store=cv_store, keys=keys)
    return int(state.generation)


async def _wait_or_fail(ctx: OperationContext, export_id: int, created_at: datetime, now: datetime) -> None:
    """Step 2's wait: `purge_in_progress` until `PURGE_WAIT` later, no attempt spent; `failed` once the export
    has waited `EXPORT_WAIT_LIMIT` since its request (its failure hook ends the row)."""
    exhausted = now - created_at >= EXPORT_WAIT_LIMIT
    async with ctx.engine.begin() as conn:
        if exhausted:
            await ctx.finish_in(conn, status="failed", error_code=PURGE_IN_PROGRESS)
        else:
            await ctx.wait_in(conn, error_code=PURGE_IN_PROGRESS, not_before=now + PURGE_WAIT)
    log.info("export_waits_for_purge", export_id=export_id, wait_exhausted=exhausted)


async def _guard(
    ctx: OperationContext, conn: AsyncConnection, export_id: int
) -> tuple[str | None, datetime | None]:
    """The guard of the start and of every name the handler gives a file (steps 1, 3 and 5), in the caller's
    transaction: the owner row, then the export row `FOR SHARE`. The export's status (`None` when it has no
    row) and, while it is `running`, its `created_at`; an export no longer `running` ends the operation
    `succeeded` in that transaction."""
    await lock_operation_owner(conn, ctx.claim)
    row = (await conn.execute(_SHARE_EXPORT, {"e": export_id})).first()
    if row is not None and row.status == "running":
        created_at: datetime = row.created_at
        return "running", created_at
    await ctx.finish_in(conn, status="succeeded")
    status = None if row is None else str(row.status)
    log.info("export_not_running", export_id=export_id, status=status)
    return status, None


def _remove_unless_ready(volume: Path, export_id: int, status: str | None) -> None:
    """The export's `<id>-*` files removed, except a `ready` export's archive, which its row names (no handler
    meets one: defensive)."""
    if status != "ready" and (removed := _remove(volume, export_id)):
        log.info("export_files_removed", export_id=export_id, files_removed=removed)


async def _open_tmp(ctx: OperationContext, export_id: int, volume: Path) -> tuple[Path, int] | None:
    """Step 3: the `.tmp`, created under the guard; `None`, with the operation `succeeded` and no file
    created, when the export is no longer `running`."""
    opened: tuple[Path, int] | None = None
    try:
        async with ctx.engine.begin() as conn:
            status, _ = await _guard(ctx, conn, export_id)
            if status != "running":
                return None
            await afault_point(BEFORE_TMP)
            opened = _create_tmp(volume, export_id)
        # committed: a purge's `running → purged` of this row comes after, and its unlink takes the `.tmp`
    except BaseException:
        if opened is not None:
            _discard(*opened)
        raise
    return opened


async def _rename(ctx: OperationContext, export_id: int, volume: Path, tmp: Path) -> str | None:
    """Step 5: the sealed `.tmp` renamed to its `.jhx` under the guard (the directory flushed); its name, or
    `None`, with the operation `succeeded` and the export's files removed (no row names them), when the export
    is no longer `running`. A `.tmp` gone while the export still runs is no state of the protocol: its
    `FileNotFoundError` propagates (the operation is retried)."""
    async with ctx.engine.begin() as conn:
        status, _ = await _guard(ctx, conn, export_id)
        if status != "running":
            _remove_unless_ready(volume, export_id, status)  # no row names them
            return None
        await afault_point(BEFORE_RENAME)
        archive = tmp.with_name(tmp.name.removesuffix(TMP_SUFFIX) + ARCHIVE_SUFFIX)
        os.rename(tmp, archive)
        fsync_directory(volume)
    return archive.name


async def _publish(
    ctx: OperationContext, export_id: int, volume: Path, generation: int, sealed: SealedArchive, archive: str
) -> None:
    """Step 6, the final transaction (module docstring)."""
    async with ctx.engine.begin() as conn:
        await lock_active(conn, share=True)  # step 2 of the lock schedule, a statement of its own
        await lock_operation_owner(conn, ctx.claim)
        status = (await conn.execute(_LOCK_EXPORT, {"e": export_id})).scalar_one_or_none()
        since = (await conn.execute(_PURGED_SINCE, {"g": generation})).one()
        if status != "running":
            _remove_unless_ready(volume, export_id, status)  # no row names them
            await ctx.finish_in(conn, status="succeeded")
            outcome = "not_running"
        elif since.fenced or since.newer:
            await transition(conn, "exports", export_id, ["running"], "purged")
            _remove(volume, export_id)
            await ctx.finish_in(conn, status="succeeded")
            outcome = "purged"
        else:
            await transition(
                conn,
                "exports",
                export_id,
                ["running"],
                "ready",
                set_={
                    "storage_path": archive,
                    "sha256": sealed.sha256,
                    "size_bytes": sealed.size_bytes,
                    "purge_generation": generation,
                },
            )
            await ctx.finish_in(conn, status="succeeded")
            outcome = "ready"
    log.info(
        "export_published",
        export_id=export_id,
        outcome=outcome,
        status=status,
        fenced=bool(since.fenced),
        newer_manifest=bool(since.newer),
        purge_generation=generation,
        size_bytes=sealed.size_bytes,
    )


@register_operation_handler(KIND, on_failed=export_failed)
async def run_export(ctx: OperationContext) -> None:
    """The `export` operation's handler (module docstring, "The handler")."""
    deps = ctx.deps
    if deps is None:
        raise RuntimeError("the export handler runs with the worker's operation dependencies")
    export_id = _export_id(ctx.claim.payload)
    if export_id is None:
        log.error("export_payload_invalid", operation_id=ctx.claim.operation_id)
        async with ctx.engine.begin() as conn:
            await ctx.finish_in(conn, status="failed", error_code=INVALID_PAYLOAD)
        return
    volume = deps.cfg.exports_dir
    keys = crypto.data_keys(deps.cfg)
    now = deps.clock()
    async with ctx.engine.begin() as conn:  # 1.
        status, created_at = await _guard(ctx, conn, export_id)
    _remove_unless_ready(volume, export_id, status)  # an earlier attempt's, or a content-free row's
    if created_at is None:  # no longer `running`: the operation ended `succeeded`
        return
    with _plaintext() as plain:
        generation = await _snapshot(ctx, plain, export_id, deps.cv_store, keys)  # 2.
        if generation is None:
            await _wait_or_fail(ctx, export_id, created_at, now)
            return
        await afault_point(AFTER_SNAPSHOT)
        opened = await _open_tmp(ctx, export_id, volume)  # 3.
        if opened is None:
            return
        tmp, fd = opened
        try:
            await afault_point(AFTER_TMP)
        except BaseException:
            _discard(tmp, fd)
            raise
        sealed = _seal(plain, tmp, fd, keys[0], export_id)  # 4.
    archive = await _rename(ctx, export_id, volume, tmp)  # 5.
    if archive is None:
        return
    await afault_point(AFTER_RENAME)
    await _publish(ctx, export_id, volume, generation, sealed, archive)  # 6.


# ---- retention ---------------------------------------------------------------------------------------------

# A running export whose operation is neither `pending` nor `running`.
_WITHOUT_LIVE_OPERATION: Final = (
    "NOT EXISTS (SELECT 1 FROM operations o WHERE o.kind = 'export' AND o.status IN ('pending', 'running') "
    "AND o.payload -> 'export_id' = to_jsonb(t.id))"
)
_NAMED_ARCHIVES: Final = q(
    "SELECT storage_path FROM exports WHERE status = 'ready' AND storage_path = ANY(CAST(:names AS text[]))"
)
_DELETE_ENDED: Final = q(
    "DELETE FROM exports WHERE status IN ('expired', 'failed', 'purged') AND expires_at < :cutoff"
)


def _archives(volume: Path) -> list[tuple[str, datetime]]:
    """The archive names of the exports directory (regular files, a symbolic link never followed) with when
    each was last written (UTC); one removed while the directory is read is left out."""
    found: list[tuple[str, datetime]] = []
    try:
        entries = os.scandir(volume)
    except FileNotFoundError:
        return found
    with entries:
        for entry in entries:
            if not _ARCHIVE_NAME.fullmatch(entry.name):
                continue
            try:
                status = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISREG(status.st_mode):
                found.append((entry.name, datetime.fromtimestamp(status.st_mtime, UTC)))
    return sorted(found)


async def _remove_orphans(engine: AsyncEngine, volume: Path, *, now: datetime, epoch: int) -> int:
    """The archives written before `now - ORPHAN_AGE` that no `ready` row names, removed under the epoch
    fence; how many."""
    written = [name for name, modified in _archives(volume) if modified < now - ORPHAN_AGE]
    if not written:
        return 0
    async with engine.begin() as conn:
        await fence_epoch(conn, epoch)  # held while the files are removed
        named = {str(name) for name in (await conn.execute(_NAMED_ARCHIVES, {"names": written})).scalars()}
        orphans = [name for name in written if name not in named]
        for name in orphans:
            (volume / name).unlink(missing_ok=True)
    if orphans:
        fsync_directory(volume)
    return len(orphans)


async def retain_exports(engine: AsyncEngine, *, now: datetime, epoch: int) -> int:
    """The `exports` retention rule at `now` for worker `epoch` (module docstring, "Retention"): the rows it
    expired, failed or deleted and the unnamed archives it removed."""
    async with engine.begin() as conn:
        await fence_epoch(conn, epoch)
        expired = await transition_where(
            conn,
            "exports",
            ["ready"],
            "expired",
            where="t.expires_at < :now",
            params={"now": now},
            set_={"storage_path": None},
        )
    async with engine.begin() as conn:
        await fence_epoch(conn, epoch)
        lost = await transition_where(
            conn, "exports", ["running"], "failed", where=_WITHOUT_LIVE_OPERATION, params={}
        )
    ended = sorted(int(row["id"]) for row in (*expired, *lost))
    removed = [_remove_configured(export_id) for export_id in ended]  # after the commits that ended them
    orphans = 0 if _volume is None else await _remove_orphans(engine, _volume, now=now, epoch=epoch)
    async with engine.begin() as conn:
        await fence_epoch(conn, epoch)
        deleted = int((await conn.execute(_DELETE_ENDED, {"cutoff": now - EXPORT_ROWS_KEPT})).rowcount)
    log.info(
        "exports_retained",
        expired=len(expired),
        failed=len(lost),
        files_removed=sum(n or 0 for n in removed),
        orphans_removed=orphans,
        rows_deleted=deleted,
        export_ids=ended[:20],
    )
    return len(ended) + orphans + deleted
