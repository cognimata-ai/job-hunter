# M5 plan, fix round 3: the plan author's response to Codex round 3

- **Review:** `docs/council/plan-reviews/m5-round3-codex.md` (REJECT; it resolves all 10 round-2 findings and adds 6 MAJOR ones).
- **Plan:** `docs/superpowers/plans/2026-11-09-m5-operations-privacy-production.md`, edited in place on top of 9e2bdd4 (HEAD 1117f0f adds only the review). Not committed.
- **References:** `P:n` is a line of the edited plan.

All six findings hold against the code at HEAD: 6 fixed, 0 rebutted.

M5-U1–U4 and M5-R1–R8 stay binding. Where a fix needed a new decision, I recorded it: two new Deviations (51, 52) and updates to Deviations 15, 16, 19, 23, 29, 38 and 39. "New decisions to flag" lists them.

The sweep over the four finding classes you named found 9 more defects, all fixed; the items below marked "Fixed" that only restate a finding are not counted. It is listed by class after the findings.

## Findings

**1. MAJOR — the transaction guard breaks the group-write responses after their commit. Fixed.**

What I verified:
- `read_snapshot` opens with `engine.connect()` and sends `SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY` as the transaction's first statement (`db/snapshot.py:19-27`).
- The guard's `begin` listener would run its read before that statement, and PostgreSQL then refuses the isolation change.
- `split_member` and `score_individually` commit through `with_deadlock_retry` and then answer from `_group`'s `read_snapshot` (`api/routes/groups.py:129-133, 169, 196`).

I checked the whole surface with an AST scan of `api/routes/`:
- The only state-changing routes that open a second transaction are login (exempt) and these two.
- `read_snapshot` is the only code that sets an isolation level.
- `POST /api/settings/dry-run` sets only `READ ONLY` (`api/routes/settings.py:481-482`). PostgreSQL accepts that after a query, because it refuses only a later isolation change or a return to read-write.

The fix:
- `read_snapshot` runs under `fence_guard.suspended()` (P:290, Files P:283). A read-only transaction refuses every write anyway (SQLSTATE 25006), so the guard loses nothing there.
- The guard module moves to `db/fence_guard.py` (P:282, P:84, Deviation 45), so `db/snapshot.py` uses it without importing the api.
- New tests (P:302):
  - `test_group_split_and_score_individually_answer_after_their_commit`: through `create_app` and the real middleware, with no fence and again with delete-everything's A0 committed between the write's commit and the snapshot;
  - `test_a_read_snapshot_outside_the_guard_still_refuses_a_write`;
  - `test_settings_dry_run_answers_under_the_guard`.
- Step 4 adds `test_api_groups.py`, `test_read_snapshot.py` and `test_settings_dry_run.py`.
- Deviation 15 (P:964).

**2. MAJOR — a live source purge cannot satisfy its own timestamp postcondition. Fixed.**

What I verified:
- A0 writes `purge_fence_at := now`.
- `fenced_at` is the cutoff read after A0 commits (`fence_committed_at`, P:337), so on the live path `purge_fence_at < fenced_at` always held, and `purge_fence_at ≥ fenced_at` could never pass.

The fix keeps two timestamps apart.
- **`admitted_at` (new).** Every manifest gains a top-level `admitted_at`: the `now` of `open_purge` (P:330, P:333).
  - A0 writes it as `deletion_manifests.created_at` (`insert_manifest(now=)`, P:336; `open_purge(now=)`, P:408).
  - A0 also writes it as every audit value it records: the source purge's `purge_fence_at` and its disabling settings version's `created_at` (`save_settings(now=)`, P:482).
- **`fenced_at`** stays the later cutoff that bounds rows other transactions wrote.
- **What changed to `admitted_at`:**
  - the source purge's postconditions: `purge_fence_at ≥ admitted_at`, equal on the live path (P:486);
  - the three replay generation rules (P:483);
  - the replay test (P:714);
  - `request_golden_case_delete` gains `now` (P:418).
- New test `test_a_live_purge_completes_when_its_cutoff_is_later_than_its_admission`, with an injected clock: A0 at t0, the cutoff read at t0 + 5 s (P:488).
- Deviations 16 (P:965) and 23 (P:972); spec 3.7 item 15 (P:876).

**3. MAJOR — purges strand online spend entries after deleting their calls. Fixed (carry G04).**

What I verified:
- `_ENTRY` inner-joins the ledger row to its call (`llm/spend_ledger.py:583`). So do `_UNSETTLED` (`:1881`) and `_recover` (`:1939`).
- `settle` and `release_never_dispatched` read the entry through `_ENTRY` and raise `LookupError` without the call. `mark_dispatched` reads it after its owner lock and raises `FencedOut` (`:1160-1166`).
- `delete_target_set` deletes an `in_progress` target's attempt calls (`scoring/targets.py:1324`).
- Delete-everything's mapping released `reserved` entries but left online `dispatched` ones.

The fix (P:385):
- `spend_ledger.lock_for_purge` takes the entries' month rows, then their funded bulk rows: schedule steps 6 and 7, before `delete_target_set` locks the targets at step 10.
- `settle_for_purge` then moves `reserved` to `not_billed('never_dispatched')` through `_release`, and online `dispatched` to `ambiguous` through `_mark_ambiguous`.
- It runs through `delete_target_set`'s new `on_cancelled` hook, after step 1 has cancelled the targets and before step 3 deletes the calls (P:386, P:387). A cancelled target's coroutine can no longer dispatch, because `mark_dispatched` re-checks its owner under the owner lock.
- `settle` and `release_never_dispatched` of a purged call log `llm_settle_call_purged` and do nothing. That covers a late response and a fenced dispatch marker.
- Every purge that deletes calls uses this:
  - target-set deletion (P:387);
  - the golden-case delete (P:418);
  - the Adzuna purge's eval calls (P:484);
  - delete-everything's step 2 (P:505).
- Task 9 adds a generic postcondition: no `reserved` or `dispatched` entry without its call (P:337).
- New tests:
  - Task 11: the reserved release, the dispatched entry ending `ambiguous` with a no-op late response, and `ledger_unsettled` finding nothing stranded (P:392);
  - Task 15: `test_reserved_and_dispatched_online_calls_are_settled_before_they_go` (P:488);
  - Task 16: `test_online_calls_in_flight_are_settled_before_their_calls_go` (P:509).
- Step 4 of Task 11 adds the spend-ledger suites (P:395).
- New Deviation 51 (P:1000); spec 3.7 items 5 (P:866) and 21 (P:882).

**4. MAJOR — identity advancement ignores an uncalled sequence at the bound. Fixed.**
- Verified: a fresh sequence has `last_value = 1` and `is_called = false`, so its next value is 1, and "below the target" skipped it.
- `advance_identities` now reads the next allocatable value from the sequence: `last_value + 1` when it was called, else `last_value`. It calls `setval(…, target, true)` whenever that value is at or below the target (P:670).
- New tests (P:673):
  - `test_an_uncalled_sequence_at_the_bound_is_advanced`: an empty table, a fresh sequence and `max_ids = 1`, so the next insert gets 2;
  - `test_a_called_sequence_at_the_bound_is_left_alone`.
- Deviation 38 (P:987); spec 3.7 item 14 (P:875).

**5. MAJOR — the rotation drill invokes a script hardcoded to production. Fixed.**
- `scripts/rotate-db-role.sh` gains `--compose`, `--secrets-dir` and `--dry-run` (P:772).
  - The defaults are Task 21's prod command line and `/etc/jobhunter/secrets`, so the RUNBOOK's call is unchanged.
  - Every step uses the two parameters.
- The drill:
  - writes every secret file into `.secrets/test`;
  - restarts with `$PT restart`;
  - runs `sudo scripts/rotate-db-role.sh --compose '<PT command line>' --secrets-dir .secrets/test db_app <file>`;
  - asserts that PT's api and worker restarted, that the `S3` project's container did not, that PT's postgres refuses the old password and accepts the new one, then `/readyz` and login (P:773).
- New tests `test_the_defaults_are_the_prod_command_line_and_secrets_directory` and `test_compose_and_secrets_dir_replace_both` (P:775).
- Deviation 39 (P:988).

**6. MAJOR — an exhausted export operation permanently blocks later exports. Fixed.**

What I verified:
- `MAX_OPERATION_ATTEMPTS = 10` (`worker/operations.py:121`).
- `_release` ends the tenth requeue, and an exhausted reclaim or takeover, `failed`, and runs the kind's failure hook (`:549-563`). The export had no hook.
- `exports_one_running` (P:120) then refuses every new export.

The fix:
- **Waiting spends no attempt.** Task 8 adds `wait_operation` / `OperationContext.wait_in`: back to `pending` with `attempts` unchanged, never `failed` (P:292).
- **The export waits for at most 24 hours.** It waits out a purge with `wait_in`; after `EXPORT_WAIT_LIMIT` (24 h) it ends `failed` through `finish_in` (P:526). A failed purge keeps its manifest `executing` until the user retries it.
- **The failure hook.** `export_failed` moves the row `running → failed` idempotently and unlinks its `.tmp` file and any unpublished one (P:527). It covers the failed finish, exhaustion and reclaim.
- **A retention backstop.** Retention fails a `running` export that has no live operation (P:530).
- New tests (P:302, P:532):
  - `test_a_wait_spends_no_attempt`;
  - `test_an_export_waiting_past_24_hours_fails`;
  - `test_an_exhausted_export_fails_and_a_new_export_succeeds`;
  - `test_an_export_reclaimed_on_its_last_attempt_fails_through_the_hook`;
  - `test_retention_fails_a_running_export_without_a_live_operation`.
- The existing wait test now also asserts that no attempt is spent.
- New Deviation 52 (P:1001); spec 3.7 item 20 (P:881).

## Sweep

### (a) Response building that could fail after the data changed

1. **Fixed: the export download (Task 18).** The route read the status, then opened the file. A purge's export op or the expiry could unlink the file in between and turn a `gone` into a 500.
   - The route now opens the file and decrypts its first segment before the 200 starts. A file found missing re-reads the row: `purged` or `expired` answer 410.
   - Once the 200 has started, the stream reads from the open descriptor: `iter_decrypted` now takes an open file (P:528), so an unlink cannot truncate a download already under way (P:551).
   - New tests (P:563); Deviation 19 (P:968).
2. **Checked, no defect:**
   - every other M5 route builds its response inside its own transaction: the privacy POSTs through `run_idempotent`'s stored response, the purge retry, the golden-case delete, and the alert resolve;
   - the CV upload removes its file when the guard refuses its transaction before the commit;
   - `require_session` validates before the handler.

### (b) Ledger and spend entries a deletion could strand

3. **Fixed: the golden-case delete (P:418).** It deletes its calls by cascade and is now settled through finding 3's `settle_for_purge` too. (The Adzuna purge's eval calls, P:484, and delete-everything's dispatched calls, P:505, were named in finding 3 itself.) The generic postcondition covers every kind (P:337).
4. **Checked, no defect:**
   - `quota_ledger` has no inner join to `fetch_requests`: its `fetch_request_id` and `run_id` are `ON DELETE SET NULL` (`0005_m1_collection.py:231-233`).
   - `llm_spend_ledger.bulk_operation_id` is `SET NULL` (`0008_m3_scoring.py:306`).
   - The profile purge only redacts `llm_calls.output_json`, and its versions have no live target.
   - Retention deletes only the calls of terminal targets.

### (c) Operation lifecycles that can block their own kind

5. **Fixed: the export (finding 6).**
6. **Fixed: the golden-case delete waiting out a running eval run.** It requeued every 5 minutes and spent an attempt each time, so an eval of about an hour failed the purge. It now uses `wait_in`, since the eval run has its own end (P:418).
7. **Fixed: backup rows left `running` by a dead container (Task 24).** Nothing ever ended them. `jobhunter backup scheduler` now ends them `failed('interrupted')` at its start, because one backup container runs one job at a time (P:691). New test (P:694); Deviation 29 (P:978).
8. **Checked, kept by design:**
   - a purge's `quiesce_timeout` and `co_member_changed` still spend attempts. Both are anomalies, they raise `privacy.purge_failed`, and the retry route is their recovery;
   - a failed operation never blocks its `dedup_key`, because the index covers only `pending` and `running` (`worker/operations.py:327-331`);
   - `OPERATION_LOCKS` is a procrastinate execution lock, not a failed-state block;
   - `deadman_sync`'s queueing lock holds only a job in `todo`.

### (d) Scripts and commands that hardcode prod paths while tests call them

9. **Fixed: `rotate-db-role.sh` (finding 5).**
10. **Fixed (two defects): the backup and restore staging paths (Tasks 10, 24, 26), and the restore-test runner's binary path.** `run_backup_set`, `run_restore` and `run_restore_test` wrote literal `/data/staging`, `/data/cv` and `/data/manifests` paths, which their in-process tests cannot write in the dev container.
    - `AppConfig.staging_dir` (`JOBHUNTER_STAGING_DIR`) is new (P:360), and the three functions use `cfg.staging_dir`, `cfg.cv_store_dir` and `cfg.manifests_dir` (P:688, P:731, P:732).
    - `server_env` points the staging directory at the test server's workdir (P:354).
    - The restore-test runner runs the basename of `argv[0]` in the test server's container, because `/opt/pgclient/bin` exists only in the backup image (P:735).
11. **Fixed (two defects): `scripts/build-prev-image.sh` (Tasks 27, 29).**
    - Its worktree was "in the scratch directory", a path no CI runner has. It now uses `$(mktemp -d)` (P:753).
    - The parent of 0011's commit is missing from a shallow clone, so `prodstack.yml` checks out with `fetch-depth: 0` (P:792).
12. **Fixed: `scripts/host-setup.sh`'s tests (Task 22).** They now state that every case runs with `--dry-run` and a temporary `--secrets-from`, so no test touches `/etc/jobhunter` or the prod project (P:654).

## Other changes

- The plan now has 52 Deviations; there were 50. `.superpowers/sdd/m5-research/plan-author-report.md` is still out of date, as earlier rounds noted.

## New decisions to flag

Each choice below is recorded as a Deviation or a 3.7 spec item. None changes a user decision.

- **Finding 1:** read snapshots run outside the fence guard, and the guard moves to `db/fence_guard.py` (Deviation 15).
- **Finding 2:** a manifest's `admitted_at`, A0's own clock value, is kept apart from the cutoff `fenced_at`. Audit postconditions and replay rules compare with `admitted_at` (Deviations 16, 23).
- **Finding 3:** `lock_for_purge` / `settle_for_purge` before any call deletion, a no-op late settlement for a purged call, and a generic postcondition (new Deviation 51).
- **Finding 6, sweep 6:**
  - `OperationContext.wait_in`, an attempt-free wait;
  - the 24-hour export wait limit;
  - the export failure hook and its retention backstop (new Deviation 52).
- **Finding 4:** the advancement reads the next allocatable value (Deviation 38).
- **Finding 5:** `rotate-db-role.sh --compose/--secrets-dir/--dry-run`, with prod defaults (Deviation 39).
- **Sweep 1:** a download opens its file before the 200 (Deviation 19).
- **Sweep 7:** the backup scheduler fails rows a dead container left `running` (Deviation 29).
- **Sweep 10:** `JOBHUNTER_STAGING_DIR` (Deviation 29).
