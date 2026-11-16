# M5 plan, fix round 1: the plan author's response to Codex round 1

Review: `docs/council/plan-reviews/m5-round1-codex.md` (REJECT; 1 BLOCKER, 17 MAJOR). Plan: `docs/superpowers/plans/2026-11-09-m5-operations-privacy-production.md`, edited in place on top of 471fcd5 (not committed). `P:n` is a line of the edited plan.

I checked every finding against the code. All 18 hold, so I fixed all 18 and rebutted none. M5-U1–U4 and M5-R1–R8 stay binding: no fix changes a user decision or a controller ruling. Where a fix needed a new decision, I chose one; the list under "New decisions to flag" names each one and where it is recorded.

## Findings

**1. BLOCKER — the backup Dockerfile cannot build. Fixed.**
- Verified: `Dockerfile:9` says that BuildKit does not expand variables in `COPY --from`.
- There was a second hazard. The `backup` stage becomes the Dockerfile's last stage, and `compose.dev.yaml`'s `x-app-build` names no target (`deploy/compose.dev.yaml:23-31`). The dev app image would therefore have become the backup image.
- The fix:
  - named source stages `FROM ${RESTIC_IMAGE} AS restic` and `FROM ${PGVECTOR_IMAGE} AS pgclient`, with `COPY --from=restic` / `--from=pgclient` (P:359);
  - every app build names `target: runtime`: `x-app-build` in Task 10 (P:360), `compose.test.yaml` (P:620), and the `images` and release jobs (P:784);
  - a test pinning it, `test_the_app_image_is_still_the_runtime_stage` (P:367).

**2. MAJOR — Task 21 starts `jobhunter backup scheduler` before Task 24 creates it. Fixed (staged).**
- `tests/support/prodstack.py` gains `STACK_SERVICES = ("caddy", "api", "worker", "docparse", "stubsvc")`. `up()` starts only these, so `backup` is not among them before Task 24 (P:624).
- Task 24 adds `backup` to `STACK_SERVICES` (P:677).
- Task 21 still checks the backup and restore roles with `run --rm --no-deps … startup-check --role`. That command exists from Task 10 and does not need the scheduler (P:628).

**3. MAJOR — the manifest FK breaks the existing fence fixtures. Fixed.**
- Verified: these files insert `purge_fences` with a literal `manifest_id` (1, 2 or 3):
  - `tests/support/scoring.py:1241, 1245`;
  - `tests/integration/test_is_fenced_profile.py:9`;
  - 17 more integration files: `test_admission_candidates`, `api_job_detail`, `api_labelling`, `atomic_claim`, `batch_submit`, `cycles`, `embed_profile`, `fenced_items`, `fences_m1`, `finalize`, `merge_unmerge`, `profile_confirm`, `publish`, `quota_ledger`, `replay_operation`, `schema`, `spend_ledger`.
- Task 1 now:
  - creates `tests/support/fences.py` with `insert_test_manifest`, `insert_test_fence` and `TEST_MANIFEST_SQL` for the synchronous schema tests (P:125);
  - converts every listed fixture (P:112). `test_schema.py`'s CHECK cases keep their literal ids, because a CHECK fails before the FK trigger runs;
  - adds `test_purge_fences_manifest_fk_is_enforced` (23503);
  - runs all converted files in its Step 4.

**4. MAJOR — `query_proposals` has no edge to `purged`. Fixed.**
- Verified: `db/transitions.py:513-523` allows `running → ready | failed` only, with the comment "The `purged` status … has no edge in this milestone", and the trigger is at `0006_m2_llm_profile_settings.py:354`.
- Task 1 adds `running | ready | failed → purged` with `items` settable. The matrix syncs through `seed.sync_transition_matrix` (P:123).
- Test: `test_a_proposal_moves_to_purged_with_its_items_nulled_in_one_update`.
- Task 14 cancels a live `propose_queries` operation of the version first, then moves each proposal through `transition` (P:461). It tests a `ready`, a `failed` and a `running` proposal.
- New Deviation 49 (P:991); spec 3.7 item 4.

**5. MAJOR — the completion check requires the fences released before they are released. Fixed.**
- `check_postconditions`' "no active fence" is now explicitly a final-state condition (P:335).
- Phase C is one transaction: `release_fences`, then the postconditions, then `complete_manifest`. A violation rolls it back, so the fences stay active, and a second transaction marks the operation failed (P:411).
- Replay's phase C uses the same order (P:704).
- Test: `test_completion_releases_fences_and_resolves_purge_failed` now names the fenced case.
- Deviation 17; spec 3.7 item 19.

**6. MAJOR — C31 recovery recomputes a file it may not overwrite. Fixed.**
- When `<uuid>.json` exists and `file_sha256` is NULL, the handler adopts the file and does not recompute it:
  - `parse_manifest` must accept it with the row's uuid, kind and format;
  - then `record_manifest` stores the file's own content and hash;
  - a file that does not parse fails the operation `manifest_invalid` and the fence is kept (P:409).
- The C31 test now inserts unrelated rows before the restart and asserts that the adopted `scope` equals the file (P:416).
- Deviation 17.

**7. MAJOR — `PurgeRun.chunk()` reverses the lock order. Fixed.**
- Verified: §3.3.3 L314's order (epoch, `active_config`, idempotency, job rows, owner), and `lock_operation_owner` at `worker/operations.py:465`.
- `chunk(*, config="share"|"update", job_ids=())` now takes:
  1. `fence_epoch`;
  2. `active_config` `FOR SHARE` (`FOR UPDATE` for delete-everything's step 8);
  3. the job rows `FOR UPDATE` in id order;
  4. then the owner.
- `ranking.bump` stays last, and no kind locks anything before `chunk` returns. Replay uses the same order without the epoch and the owner (P:405).
- Test: `test_chunk_takes_config_then_jobs_then_the_owner` (P:416).

**8. MAJOR — P1 strands ambiguous adoption and weakens foreign-line detection. Fixed.**
- Verified both problems:
  - `batch_adoption.py:298-313` compares the provider's ids with `items_of(...)`;
  - `tests/integration/test_batch_poll.py:438` expects `t999999_a9` to stay foreign.
- The fix uses positive evidence:
  - a new table, `purged_batch_items (submission_id, custom_id, manifest_id, purged_at)` (P:118);
  - `purge_target_set(…, manifest_id, …)` writes one row per deleted item (P:383);
  - the result reader treats a line as `purged` only when such a row exists, so every other missing item stays foreign (P:384);
  - adoption compares the provider's ids with the items plus the purged items (P:385).
- New tests: `test_an_ambiguous_submission_with_a_purged_item_is_still_adopted` (P:388), and the `t999999_a9` foreign case.
- Deviation 13; spec 3.7 items 4 and 5.

**9. MAJOR — the shared job-deletion helper omits required cleanup. Fixed.**
- Verified:
  - `representative.leave` only removes the membership (`dedup/representative.py:220-228`), and `reelect` re-elects or dissolves (`:268`);
  - `merge_events.from_job_id`/`into_job_id`, `jobs.merged_into_job_id` and `duplicate_groups.representative_job_id` restrict deletion (`0005_m1_collection.py:589-590, 459, 619`).
- `delete_job_graph` is now the one complete cleanup for tombstoning steps 4–7 (P:435). It covers:
  - inactive golden cases;
  - merge events, clearing `reversed_by_event_id` first;
  - conflicts;
  - `leave` followed by `reelect`, which dissolves a group left with fewer than two members;
  - revisions, postings and keys;
  - the tombstone;
  - merged jobs, deepest first.
- Task 13 calls only `purge_target_set` and this helper.
- New tests:
  - tombstoning: `test_a_representative_is_replaced_before_it_is_deleted` (P:443) and `test_merge_events_and_conflicts_naming_the_job_go_first`;
  - the Adzuna purge: Adzuna-only jobs that represent a group, have a merged job, or are named by a merge event (P:483).

**10. MAJOR — resetting to defaults through `save_settings` can block delete-everything. Fixed.**
- Verified:
  - the eval gate runs for gated effects (`settings/save.py:444`);
  - `_apply_llm_tuple` creates a tuple of the active profile (`:299`).
- Step 8 now calls a dedicated reset, `settings/reset.reset_to_defaults` (P:501). It:
  - takes the shipped defaults;
  - runs `ensure_derived(…, profile=None)`;
  - writes one settings version;
  - sets `active_config` to the new ids, with profile, tuple and pending scope NULL;
  - resets `schedule_state` (P4).
- It runs no gate and creates no tuple and no operation. The plan says why `save_settings` is not used (P:496).
- Test: `test_the_reset_needs_no_gate_and_creates_no_tuple_or_operation` (P:504).
- Deviation 24; spec 3.7 item 21.

**11. MAJOR — `login_lockouts` cannot be bounded. Fixed.**
- Verified: `0001_m0_baseline.py:114-118` has a `scope` primary key (an IP address), `locked_until` and `reason`, and nothing else.
- Migration 0010 adds `login_lockouts.generation bigint GENERATED BY DEFAULT AS IDENTITY`, and `_LOCK_OUT`'s `ON CONFLICT` sets `generation = DEFAULT`, so every lock or renewal takes a new value (P:119; `auth/throttle.py:64`).
- `Bound` gains `column` for an identity column other than `id` (P:331), and delete-everything bounds the table by `generation` (P:500). No IP address enters a manifest.
- Every manifest also gains a top-level `fenced_at`, so the `created_at` bound works for delete-everything's sessions and idempotency records (P:331).
- Tests:
  - `test_a_renewed_lockout_takes_a_new_generation` (Task 1);
  - `test_a_lockout_taken_or_renewed_after_the_manifest_survives_a_reexecution` (P:504).
- Deviation 16.

**12. MAJOR — the edge refuses the retry of a failed delete-everything. Fixed.**
- `FENCE_EXEMPT_ROUTES` gains `("POST", "/api/system/purges/{operation_id}/retry")`, matched by route template (`api/edge.py:86`) (P:289).
- The test goes through the real middleware with the global fence active: `test_a_failed_delete_everything_is_retried_through_the_edge_under_its_global_fence` (P:557).
- The test name in Task 8 now covers this exemption.
- Deviation 15; spec 3.7 item 19.

**13. MAJOR — re-authentication blocks idempotent retries when TOTP is on. Fixed.**
- Verified:
  - `claim_totp_step` consumes the step (`auth/users.py:192`);
  - `request_fingerprint` hashes the body (`api_idempotency.py:69`).
- The new order:
  - the fingerprint leaves out `password` and `totp`;
  - a new `api_idempotency.stored_response(...)` returns a completed request's stored response before any re-authentication, and answers 422 for another action under the key and 409 while the first request is in progress;
  - only then come the re-authentication and `run_idempotent`, with the same fingerprint (P:542).
- Tests (P:557):
  - `test_a_lost_response_retry_with_totp_returns_the_stored_202`;
  - `test_a_retry_with_a_new_code_is_the_same_action`;
  - `test_a_different_action_under_the_key_is_422_reused`.
- Deviation 21; spec 3.7 item 6.

**14. MAJOR — an export can outlive a purge. Fixed.**
- The export's snapshot first reads the purge generation, `g = max(deletion_manifests.id)`. If any manifest is `executing`, the export requeues `purge_in_progress` and reads nothing (P:521).
- The final transaction, under `active_config FOR SHARE`, ends the export `purged` when a fence is active or a manifest above `g` exists. Every A0 inserts its manifest under `active_config FOR UPDATE`, and that includes the fence-free golden-case delete (P:521).
- `exports.purge_generation` stores `g` (Task 1).
- Tests cover both schedules: `test_an_export_waits_while_a_purge_is_executing` and `test_a_purge_opened_after_the_snapshot_purges_the_export_at_publication` (P:526).
- Deviation 19; spec 3.7 item 20.

**15. MAJOR — restore ignores restic's stored path hierarchy. Fixed.**
- `Restic.restore(snapshot, subfolder, target)` runs `restic restore <snapshot>:<subfolder> --target <target>` (P:362). The `snapshotID:subfolder` form is to be read from the pinned 0.19.1's `restore --help` at execution.
- A set's dump and json now live in `/data/staging/set` and are backed up from there, which also keeps the restic cache out of the snapshot (Task 24, P:682).
- `run_restore` and the restore test restore each subfolder to an explicit target and check the exact paths before verification (P:725).
- Tests:
  - `test_a_subfolder_restore_lands_at_the_exact_path` (backup image);
  - `test_each_restore_names_its_subfolder_and_target`;
  - the stack C32 test asserts the exact paths.
- Deviation 30; spec 3.7 item 15.

**16. MAJOR — restore assumes a manifest snapshot exists. Fixed.**
- An empty `snapshots --tag manifests` list is an empty stream: nothing is restored and `manifest_stream_empty` is logged.
- A restic failure stops the restore with `restic_failed`.
- When the stream is empty, a restored `shipped` manifest stops the restore with `manifest_stream_missing` (P:725).
- The restore test follows the same rule.
- Tests:
  - `test_a_backup_before_any_manifest_restores` (stack, P:729);
  - `test_a_restore_test_before_any_manifest_succeeds`;
  - `test_an_empty_stream_is_restored_as_empty`;
  - `test_a_restic_failure_stops_the_restore`.
- Deviation 30; spec 3.7 item 15.

**17. MAJOR — rotation leaves retained backups depending on discarded keys. Fixed.**
- Every `backup_set.json` lists the `data_key_ids` of its ciphertexts: the raw texts, the TOTP secret and the CV file headers. Test: `test_the_set_names_the_data_key_ids_of_its_ciphertexts` (P:682).
- The recovery kit keeps every earlier data key a retained set names, labelled by key id, for about 3 months after the rotation (P:832).
- A restore prints the set's key ids. An older key is mounted as `data_key_previous`, followed by `crypto rotate` (RUNBOOK item 9).
- The rotation drill restores a set taken before the rotation, with the kit's old key (P:767).
- Deviation 31; spec 3.7 items 8, 15 and 17.

**18. MAJOR — a finishing run can resume a paused dead-man check. Fixed.**
- `DEADMAN_LOCK_KEY = 7261005` is a session advisory lock on an autocommit connection, so no transaction stays open during the HTTP call. Every ping decision and every `deadman_sync` action runs under it.
- A ping first reads `schedule_state.enabled` and `deadman_state.applied_paused`. A disabled schedule or a paused check gets no ping, and the watermark still moves (P:221).
- Test: `test_a_run_finishing_after_the_schedule_was_disabled_does_not_resume_the_check` (P:228).
- Deviation 9; spec 3.7 item 10.

## Other changes

- `JobGraphDeleted.job_id` became `job_ids: tuple[int, ...]`, because the helper now deletes merged jobs too (P:435).
- Delete-everything's final transaction deletes the sessions created at or before the manifest's `fenced_at`, not its `created_at`. This matches finding 11's bound (Task 16).
- Task 26's Files and Step 4 gain `tests/unit/test_restore_run.py`.
- Task 18's Files gain `api_idempotency.py`.
- Task 5's Files gain `db/migrate.py`, whose advisory-key comment lists 7261005.
- The plan now has 49 Deviations; there were 48. `.superpowers/sdd/m5-research/plan-author-report.md` still lists 48 and the round-0 spec items, and the controller may want it refreshed.

## New decisions to flag

Each choice below is recorded in the plan as a Deviation or a 3.7 spec item. None changes a user decision.

- **Finding 8:** a new content-free table, `purged_batch_items`.
- **Finding 11:**
  - a `generation` identity on `login_lockouts`, renewed by every lock;
  - a top-level `fenced_at` in every manifest.
- **Finding 10:** a dedicated `settings/reset.py` instead of `save_settings` for delete-everything's step 8.
- **Finding 13:** a credential-free idempotency fingerprint, with a stored response returned before re-authentication.
- **Finding 14:** export admission waits while any manifest is executing, and a purge generation is checked at publication.
- **Finding 18:** advisory key 7261005, and no ping while the schedule is disabled or the check paused.
- **Finding 17:** the recovery kit keeps historical data keys, and backup sets record `data_key_ids`.
- **Finding 15:**
  - subfolder restores (`snapshot:subfolder`), whose syntax is to be checked against restic 0.19.1 at execution;
  - the `/data/staging/set` layout.
- **Finding 16:** an empty manifest stream is valid unless a restored row is `shipped`.
- **Finding 4:** the `query_proposals` purge edges (Deviation 49).
- **Finding 6:** C31 adopts the written file.
- **Finding 5:** phase C releases the fences, then checks, inside one transaction.
- **Finding 1:** named Dockerfile source stages, and `target: runtime` on every app build.
- **Finding 2:** `STACK_SERVICES` stages the prod-test stack without `backup` until Task 24.
