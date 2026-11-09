# M5 plan, fix round 4: the plan author's response to Codex round 4

- **Review:** `docs/council/plan-reviews/m5-round4-codex.md` (REJECT; it resolves all 6 round-3 findings and raises 4 new MAJOR ones).
- **Plan:** `docs/superpowers/plans/2026-11-09-m5-operations-privacy-production.md`, edited in place on top of 9824267 (HEAD a5cd9c9 adds only the review). Not committed.
- **References:** `P:n` is a line of the edited plan.

All four findings hold against the code at HEAD: 4 fixed, 0 rebutted.

M5-U1–U4 and M5-R1–R8 stay binding. The fixes needed three new decisions, recorded as Deviations 53–55. The sweep of the two classes you named found 7 more defects, all fixed.

## Findings

**1. MAJOR — a late settlement can restore content after a profile purge redacted it. Fixed.**

What I verified:
- `cancel_operation` (`worker/operations.py:605`) changes ownership only. A call already sent still gets its response.
- The gateway settles that response in its own transaction (`llm/gateway.py:1362`).
- `spend_ledger.settle` writes the call's `output_json` from `_call_set` (`llm/spend_ledger.py:1558, 1596`) whatever the operation's or the version's state.
- `settle` is the only writer of `llm_calls.output_json`; `batch_items` passes its output through it too.
- Round 3's fix covered only deleted calls.

The fix:
- **A marker.** Task 1 adds `llm_calls.content_purged_at` (P:117). It is the marker of the profile purge's `field_redact llm_calls output_json`.
- **Every call is redacted.** The redaction now selects every call of the version, published or not (P:464):
  - `propose_queries` calls whose `input_refs ->> 'profile_version_id'` names the version, every attempt and logical call, not only `query_proposals.llm_call_id`;
  - `extract_profile` calls of its drafts.
- **Settlement honours the marker (P:465, Files P:457).** `settle` writes the call row through `transition_where` with `output_json` set as `CASE WHEN t.content_purged_at IS NULL THEN … END`.
  - PostgreSQL evaluates that expression on the row the UPDATE locks, and again on the newest version after a lock wait, so a redaction that committed first wins.
  - Usage, cost and the ledger state settle as before: billing is kept.
- **Tests (P:468):**
  - a response released after the purge completed: billed, `output_json` NULL;
  - a settlement racing the redaction;
  - a late extraction response;
  - an earlier attempt's unpublished call, redacted.
- Step 4 adds `test_spend_ledger.py` and `test_gateway.py` (P:471).
- New Deviation 54 (P:1004); spec 3.7 items 4 and 19 (P:866, P:881).

**2. MAJOR — recreated purge operations lose the request needed to compute their manifest. Fixed.**

What I verified:
- `insert_manifest` wrote an empty scope, and the request lived only in the operation payload.
- The `purge_fences` reconciler creates `{manifest_id}`-only operations.
- Delete-everything cancels the other operations and deletes them (only its own is kept).
- A golden-case delete has no fence that names its case.

The fix:
- **The request moves to the manifest row.**
  - Task 1 adds `deletion_manifests.request jsonb` (ids only) (P:117).
  - `insert_manifest(request=…)` and `ManifestRow.request` (P:335, P:336); `open_purge` writes it at A0 (P:408).
  - The operation payload is `{manifest_id}` alone (P:407).
  - `compute` reads `manifest.request` (P:410).
- **Every later reader computes from the same request:** the reconciler, the retry route, replay's pending retries and a re-queued manifest whose operation delete-everything deleted.
- **Tests:**
  - `test_a_replacement_operation_computes_from_the_rows_request`: the operation is lost between A0 and A2 (P:420);
  - `test_another_purges_deleted_operation_is_recreated_from_its_row`: the delete-everything interaction, ending with an export that is not left waiting (P:510).
- New Deviation 53 (P:1003); spec 3.7 items 4 and 19.

**3. MAJOR — migration replay cannot remove files from the real exports volume. Fixed.**

What I verified:
- §19.1 gives `migrate` only `manifests` and `cv_store`, and `restore` only `cv_store`, `manifests` and staging (spec, §19.1's outline).
- Task 25 replayed `execute` only. `run_purge` runs `purge_exports` as a separate step after it (Task 12 step 3, P:414).

The fix:
- **Replay runs the export ops.** Phase A runs `purge_exports` right after `execute` for every kind with `deletes_exports`. Its file ops and postconditions use the export root migrate was given, never an unmounted image directory (P:711).
- **The volume is mounted where replay runs.**
  - `migrate` and `restore` mount `exports` read-write in the prod and test compose (P:623) and in the dev compose (P:339).
  - `migrate --exports-dir` defaults to the mounted volume (P:670), and both migrate entry points take `exports_dir`.
  - The restore test passes an empty scratch directory (P:733), so a scratch replay never touches the live archives.
- **Test (P:715):** `test_replay_removes_the_export_archive_from_the_exports_volume`. A golden-case delete stopped by `privacy.purge.mid_delete` after A3 and before its export ops; `migrate` removes the archive from the given directory and marks the row `purged` before phase C completes.
- New Deviation 55 (P:1005); spec 3.7 item 13 (P:875).

**4. MAJOR — restore leaves exports marked `ready` without their archives. Fixed (the invalidation policy).**

What I verified: a backup set holds the dump, `/data/cv` and `/data/manifests`, and no archive. So a restored `ready` row names a file that is not there.

Why invalidation and not backup: each archive is the user's full content and lives only 7 days, so I chose not to copy it offsite.

The fix:
- After `verify_restored`, `restore run`'s step 5 (P:732):
  - moves every restored `ready` export to `expired`, so its download answers 410 `gone`;
  - moves every `running` one to `failed`; its restored operation then finds it not `running` and ends without writing (P:527);
  - empties the `exports` volume, so no restored row names a file in it.
- The restore test does the same against its scratch database and directory (P:733).
- The RUNBOOK's restore section says so (P:841), and so does PRIVACY.md §6 (P:581).
- **Tests:**
  - `test_a_ready_export_in_the_set_is_expired_and_the_exports_volume_emptied` (P:736);
  - the stack scenario now puts an unexpired `ready` export into the set before the backup, and C32 asserts `expired` and 410 after the fresh restore (P:736).
- Deviation 55; spec 3.7 item 15 (P:877).

## Sweep (a): late writers that could re-create or restore purged data

For each purge kind, I checked every writer that can run after the purge's admission or completion.

| Purge kind | Writer | Result |
|---|---|---|
| profile purge | settlement of a redacted call | **Finding 1, fixed.** |
| profile purge | proposals, `items` | Safe: `transition([RUNNING], READY, set_={items})` (`queries/proposals.py:366`) cannot leave `purged`. |
| profile purge | profile vectors | Safe: the `embed_profile` handler and the `profile_vectors` reconciler skip a purged or fenced version (`profile/vectors.py:20-31`). |
| profile purge | activating a purged version | Safe: a purged version is never revived; identical content gets a new id (`profile/activation.py:27-34`). |
| profile purge | `missing_targets` | Safe: it serves only the active tuple, and the purged version's tuples are never active. |
| tombstone, source purge, delete-everything | scoring, batch results, online settlement | Safe: deleted or cancelled targets are fenced at publish; purged batch lines are skipped (Task 11); a deleted call's late settlement is a no-op (round 3). |
| tombstone, source purge | the job-scoped writers of Task 8 (embed node, `det_completeness`, similarity fill) | **Fixed (sweep 1, P:293).** They checked `is_fenced` after `FOR SHARE` on the job row, but said nothing when the row was gone. A job deleted under such a writer now counts as fenced: no `embeddings` row for a deleted revision's text, no row for a deleted job. Test `test_a_writer_meeting_a_deleted_job_writes_nothing` (P:302). |
| tombstone | identity resolution of a deferred raw item | By design: an item deferred while the job was fenced is a newer observation of the ad. It re-creates the job through `tombstone_keys`, which restores the hidden state (spec §20.2 tombstoning). |
| source purge | collection | Safe: A0 cancels the runs, and the run-owner guard fences their writers. `resolve_identity` refuses Adzuna items under the fence (spec §20.5 step 1). Raw items go with their fetch requests. |
| source purge | `rebuild_jobs` and `missing_targets` after release | Intended: they rebuild from remaining data. |
| golden-case delete | eval runs | Safe: it waits while a running eval includes the case (round 3). |
| delete-everything | runs, operations and batches | Safe: runs and operations are cancelled and their owner guards fence their writers; submissions are deleted, so nothing polls or adopts. |
| delete-everything | requests in flight | Safe: the global fence refuses them at the edge and in the guard. After completion every session is gone, so an old `Idempotency-Key` retry answers 401. |
| export | publication after a purge | Safe for the row: the purge generation decides it (round 1). The file is sweep (b). |
| every kind | restore | Safe: the two-phase replay re-applies every manifest, A0 effects included (round 2). |

## Sweep (b): artefacts that live outside the database

| Artefact | Restore | Replay | Purge | Row and file consistency |
|---|---|---|---|---|
| **Export archives** | Findings 3 and 4. | Finding 3. | **Fixed (sweeps 2 and 3).** | **Fixed (sweep 4).** |
| **Manifest files** | Restored into `manifests` (P:732). | `migrate` mounts `manifests` and rewrites missing files. | The worker writes them; no purge deletes them; delete-everything keeps them. | `file_sha256` is checked against the file (rounds 1–2). The restore test uses a scratch `--manifest-dir`. No defect. |
| **CV files** | Restored to `cv_store`; files no row names are deleted at restore step 7. | `migrate` mounts `cv_store`. | The worker mounts `cv_store` read-write. | A redacted `cv_files` row keeps `storage_path` with `purged_at` set, and readers check `purged_at` (the export reads only files that still exist). `CvStore` names files by uuid4 (`profile/storage.py:79`), so no two rows share a file. Sweep 4 applies. |
| **Staging files** | **Fixed (sweep 5).** | — | — | **Fixed (sweep 6).** |
| **Other files** | — | — | — | Checked, no defect: the docparse job directories are removed in a `finally`, cancellation included (`profile/parse_client.py:12`); the restic cache holds index and tree metadata only; the export zip sits on the worker's tmpfs. |

The fixes in detail:

- **Sweep 2 (Task 17):** a running export that a purge marks `purged` first left the handler's file behind. The handler now unlinks the file it wrote when its final transaction finds the row no longer `running` (P:527). Test `test_a_purge_that_marks_a_running_export_first_leaves_no_file` (P:533).
- **Sweep 3 (Task 17):** a crash after the rename left an archive that no row names. Each attempt now first unlinks the earlier attempts' `<id>-*` files. The `exports` retention rule removes any archive older than a day that no `ready` row names (P:531). Tests `test_an_attempt_removes_an_earlier_attempts_archive` and `test_retention_removes_an_unnamed_archive` (P:533).
- **Sweep 4 (Tasks 12 and 17):** a `file_remove` could run before the row change committed. A rollback would then leave a `ready` row naming a missing file, which answers 500 on download.
  - Task 12's `execute` (P:410), `purge_exports` (P:417) and the export expiry (P:531) now commit the row change first and unlink after.
  - A crash in between leaves only a file no row names, which the idempotent re-execution or the orphan sweep removes.
- **Sweep 5 (Task 26):** the restore left `<staging>/restore/jobhunter.dump`, a full copy of the database that no later purge would reach. `restore run` now removes it whatever the outcome (P:732). Test `test_the_restore_staging_is_removed_after_success_and_failure` (P:736).
- **Sweep 6 (Task 26):** the restore test's scratch database and directory were dropped only on success. They are now dropped after a failure too (P:733).
- **Sweep 7 (Task 26):** `verify_replayed` checked file postconditions without naming a root. It now takes `files: FileRoots`: the mounted `cv_store` and `exports` volumes for `restore verify`, and the scratch directories for the restore test (P:730).

## Other changes

The plan now has 55 Deviations; there were 52. `.superpowers/sdd/m5-research/plan-author-report.md` is still out of date, as earlier rounds noted.

## New decisions to flag

Each choice below is recorded as a Deviation or a 3.7 spec item. None changes a user decision.

- **Finding 2:** `deletion_manifests.request` (a column §7.14 lacks), and a `{manifest_id}`-only purge payload (Deviation 53).
- **Finding 1:** `llm_calls.content_purged_at` (a column §7.14 lacks), and settlement's SQL `CASE` that keeps redacted content NULL (Deviation 54).
- **Findings 3 and 4, sweeps 2–6:**
  - `migrate` and `restore` mount `exports` read-write, departing from §19.1;
  - replay runs export ops;
  - backups never hold export archives, so a restore expires `ready` exports, fails `running` ones and empties the volume;
  - restore staging is removed;
  - rows are committed before their files are removed;
  - an orphan sweep for archives (Deviation 55).
- **Sweep 1:** a job-scoped writer that finds its job deleted treats it as fenced (Deviation 14's coverage).
