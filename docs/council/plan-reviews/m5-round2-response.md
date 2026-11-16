# M5 plan, fix round 2: the plan author's response to Codex round 2

- **Review:** `docs/council/plan-reviews/m5-round2-codex.md` (REJECT; 10 MAJOR: the three that remain from round 1 and seven new ones).
- **Plan:** `docs/superpowers/plans/2026-11-09-m5-operations-privacy-production.md`, edited in place on top of d987c8e (HEAD cface63 adds only the review). Not committed.
- **References:** `P:n` is a line of the edited plan.

I checked every finding against the code at HEAD. All ten hold, so I fixed all ten and rebutted none.

M5-U1–U4 and M5-R1–R8 stay binding: no fix changes a user decision or a controller ruling. Where a fix needed a new decision, I made it and recorded it in the plan; "New decisions to flag" lists them. The sweep you asked for (the commands and paths the local test steps rely on) found nine more defects; "Sweep" lists them, all fixed.

## Findings

**1. MAJOR — purge evidence misses released batch submissions. Fixed.**

What I verified:
- The release settles the entries `ambiguous`, releases the holds, moves the targets out of `in_batch` and ends the attempts `released` (`scoring/batches.py:1340-1350`).
- The poll still reads a released submission that has a provider id (`_POLLED`, `scoring/batch_results.py:140-144`). The adoption still matches a released submission that has none (`scoring/batch_adoption.py:82`).
- So evidence keyed on "a submission that holds an `in_batch` target" missed these submissions. It also missed a target that §10.10 cancelled individually while its batch was still running.

The fix:
- `purge_target_set` keeps `record_cancel` and the `ambiguous` settlement for the submissions of `in_batch` targets. Independently of the target's state, it now writes a `purged_batch_items` row for every deleted item whose submission can still return results: `submit_started`, `submit_ambiguous`, `submitted`, `ended` or `released` (P:384). The release, the poll and the adoption are added to Consumes (P:381).
- New tests (P:389):
  - `test_a_released_lost_batch_with_a_purged_item_reconciles_on_late_retrieval`;
  - `test_a_released_ambiguous_submission_with_a_purged_item_is_still_adopted`;
  - `test_a_target_cancelled_out_of_a_running_batch_leaves_evidence_when_purged`.
- The genuinely foreign line, `t999999_a9`, still raises the anomaly.
- Deviation 13 (P:958) and spec 3.7 item 5 (P:862) now say the same.

**2. MAJOR — group cleanup neither settles deferred targets nor locks every affected member. Fixed.**

What I verified:
- `representative.reelect` dissolves or elects but never calls `settle_deferred` (`dedup/representative.py:268-300`).
- `dissolve` removes the surviving members' memberships (`:231-245`).
- `groups.leave_group` is the complete contract (`dedup/groups.py:835-853`): it raises `InvariantViolation` unless every member is in `held`, dissolves or re-elects, then calls `settle_deferred` (`:371`).
- `merge.lock_jobs(conn, ids, with_groups_of=…)` locks the jobs and their co-members in one ascending-id statement (`dedup/merge.py:292`).

The fix:
- `PurgeRun.chunk` gains `with_groups_of` and yields `Chunk(conn, held)` (P:406). Its job lock is `merge.lock_jobs` with the co-members, taken before the owner.
- A member that joins between the statement's snapshot and its lock makes the chunk start again, at most three rounds. After that, `CoMemberChanged` requeues the purge with `co_member_changed`.
- `delete_job_graph` takes `held` (P:436). It calls `groups.leave_group(conn, job, reason=groups.MEMBER_LEFT, held=held)`, as merge step 5 does (`dedup/merge.py:548`), instead of `leave` + `reelect` (P:438). The ranking bump stays last (P:435).
- Task 13's Consumes names the new contract (P:430).
- New tests:
  - `test_a_chunk_starts_again_when_a_co_member_joins_under_its_lock` (P:417);
  - `test_a_co_member_joining_during_the_graph_lock_starts_the_chunk_again` (P:444).
- The two-member test now asserts that the survivor's deferred target is `pending` and that the survivor's row was locked before the owner (P:444).
- Tasks 15 and 16 use the same graph chunk (P:482, P:502).
- New Deviation 50 (P:995); spec 3.7 item 19 (P:876).

**3. MAJOR — the retry exemption cannot work before routing. Fixed.**

What I verified:
- `route_label` reads `scope["route"]` (`api/edge.py:86-98`). Only the router sets it.
- The edge's `__call__` runs the CSRF check and the body bound before `self.app` (`api/edge.py:213-228`).

The fix (P:289):
- `fence_exempt(method, path)` matches on `scope["path"]` before routing:
  - `POST /api/auth/login`;
  - `POST /api/auth/logout`;
  - a `POST` whose path matches `^/api/system/purges/[0-9]+/retry$`;
  - every path under `/api/privacy/`.
- New tests (P:301):
  - `test_fence_exempt_matches_method_and_path`, which also checks near-misses: another method, a longer path, `/api/privacyx`;
  - `test_login_logout_privacy_and_purge_retry_are_exempt_before_routing`, through `create_app` with the real middleware: none of these requests gets the edge's 409. They get 404 before Task 18 adds the routes. Task 18's middleware test then pins the retry's 202.
- Deviation 15 (P:960).

**4. MAJOR — a CV upload can cross the global admission fence. Fixed.**

What I verified:
- `upload_cv` opens its own `engine.begin()` after reading the body (`api/routes/profile.py:349-357`).
- `open_new_draft` (`profile/upload.py:457`) takes no `active_config`.
- The quiesce only waits for transactions with `xact_start < since` (Task 9). An upload transaction that starts after the fence commits is therefore not waited for.
- Other api write routes have the same gap: 13 route modules open their own transactions, at 36 call sites of `engine.begin()` or `with_deadlock_retry`.

The fix covers every write route at once instead of auditing them one by one: `api/fence_guard.py` (P:290).
- **The flag.** The edge sets a context variable for a non-exempt state-changing request. Its token is reset in a `finally`, so an `ASGITransport` test cannot leak it.
- **The check.** `install_fence_guard` registers a SQLAlchemy `begin` listener on `engine.sync_engine`. While the flag is set, the transaction's first statement is the global-fence read, and an active fence raises 409 `delete_everything_in_progress` before anything is written.
- **Why it is complete:**
  - A transaction that began before the fence committed is one the quiesce waits for.
  - A transaction that begins after the fence committed reads it.
- **Checked in the installed SQLAlchemy 2.1.4:**
  - A statement inside the `begin` event is allowed: `Connection._begin_impl` guards autobegin re-entry with `__in_begin`.
  - The greenlet shares the request's context: `sqlalchemy/util/concurrency.py:99` sets `gr_context = driver.gr_context`.
- `persist_unhandled_alert` runs its write under `suspended()`.
- Round 1's per-route re-checks are dropped: the guard covers them. `refuse_under_global_fence` stays for the exempt privacy requests (P:288).
- New tests (P:301):
  - `test_an_upload_whose_transaction_begins_after_the_fence_commit_is_refused`: the body is held until delete-everything's A0 commits. The request gets 409, no row is written, and the stored file is removed.
  - The settings-save race.
  - `test_the_guard_ignores_unguarded_transactions`.
  - `test_a_500_under_the_guard_still_persists_its_alert`.
- Step 4 adds the upload, body-limit, auth and alert suites.
- Deviation 15 (P:960); spec 3.7 item 19 (P:876).

**5. MAJOR — source-purge replay omits the source-disable and audit effects. Fixed.**

What I verified:
- `execute` is §20.5 steps 3–5.
- The disabling settings version, `sources.purge_fence_at` (`0005_m1_collection.py:98`) and the run cancels happen only in A0.

The fix:
- **The hook.** `KindProcedure.reapply_admission` is defined in Task 12 and is a no-op for every kind but the source purge (P:407).
- **The source purge's version (P:480).** It re-applies all three A0 effects, idempotently, each under a rule that keeps a later explicit choice:
  - `purge_fence_at := fenced_at` when it is NULL or earlier;
  - a new internal disabling version, but only when the active version enables Adzuna and was created at or before `fenced_at` (`settings_versions.created_at`);
  - `cancelled('source_purge')` for `queued` and `running` runs with `queued_at ≤ fenced_at` (`runs` has `queued_at`, not `created_at`).
- **Replay.** Task 25's phase A calls it under `active_config FOR UPDATE` (P:706). Task 25 also consumes it (P:703).
- **The kind's postconditions** now check `purge_fence_at ≥ fenced_at` and the disabled setting, unless a newer version is active (P:483).
- **Tests:**
  - `test_reapply_admission_restores_the_a0_effects_once` and `test_reapply_admission_keeps_a_later_reenablement` (P:485);
  - `test_a_source_purge_replay_reapplies_its_admission_effects` (P:710);
  - the C32 backup-before-purge restore now asserts the disabled setting and `purge_fence_at` (P:731).
- Deviation 23 (P:968); spec 3.7 item 22 (P:879).

**6. MAJOR — Task 23 changes the synchronous migration API without converting its callers. Fixed (no conversion needed).**

What I verified:
- `run_migrate_async` is at `db/migrate.py:150`. `run_migrate`, the synchronous wrapper that refuses a running loop, is at `:176`.
- Its synchronous callers are:
  - `cli.py:206-208`;
  - `tests/support/fixtures.py:149` (the `migrated` fixture);
  - `tests/crash/test_singleton.py:58`;
  - `tests/integration/test_api_labelling.py:440`;
  - `tests/integration/test_jobs_list_plans.py:49`;
  - `tests/integration/test_migrate.py:60`.

The fix:
- Both functions keep their names and kinds, and both gain the flags (P:665). The wrapper passes them through, so no existing caller changes.
- The restore code awaits `run_migrate_async`: Task 25 (P:703) and Task 26 (P:727, P:728).
- New test `test_the_sync_wrapper_passes_the_flags_through`; `test_sync_wrapper_refuses_a_running_loop` stays (P:669).
- Deviation 38 (P:983).

**7. MAJOR — tombstoning can exceed the target-set deletion limit. Fixed.**

What I verified: `delete_target_set` raises `ValueError` above `TARGET_SET_CHUNK` (200) (`scoring/targets.py:1302, 1330`), and spec L6451 chunks the deletion by 200.

The fix:
- Task 13's `execute` deletes a root's and its merged jobs' targets in chunks of at most 200, each its own transaction, until none is left; the fence keeps new targets from being created. Then one graph chunk runs `delete_job_graph`, which raises `InvariantViolation` if a target is left. A restart resumes with the targets left (P:435, P:436).
- Task 15 (P:482) and Task 16 (P:502) follow the same rule.
- New tests:
  - `test_a_graph_with_more_than_200_targets_is_deleted_in_bounded_chunks`: 260 targets across a root and a merged job (P:444);
  - `test_an_adzuna_only_job_with_more_than_200_targets_is_deleted_in_bounded_chunks` (P:485).
- Deviation 50 (P:995).

**8. MAJOR — Task 21's startup checks need a network that its procedure never creates. Fixed.**
- `tests/support/prodstack.py` gains `s3_up()`, `s3_down()` and `prod_session`, which every stack module's fixture wraps (P:626):
  - it skips when another stack holds the subnets;
  - otherwise it starts `S3` (which creates `jobhunter-s3net`) before any `PT` command, `run --no-deps` included;
  - at the end, also after a failure, it removes `PT`, `PB` and then `S3`.
- `test_every_service_passes_startup_check` starts after `s3_up()` (P:630).
- The versitygw image cost moves from Task 24 to Task 21 (P:633, P:693).
- Tasks 24 and 26 drop their hand-written `$S3 up` / `$S3 down` and leave both to the fixture (P:690, P:693, P:734).
- In Task 29, `prodstack.yml` no longer starts `S3` by hand but still tears it down (P:788).
- The Global Constraints state the order (P:44).

**9. MAJOR — Playwright's discovery pattern excludes E18. Fixed.**
- Verified with the regex in Node:
  - the old pattern `(0[4-9]|1[0-9])` rejects `20-e18-privacy.spec.ts`;
  - the new `/[\\/]specs[\\/](0[4-9]|1[0-9]|20)-[^\\/]+\.spec\.ts$/` accepts it, keeps `19-…`, and still rejects `03-…` and `21-…`.
- Task 30 modifies `frontend/e2e/playwright.config.ts` (P:802, P:807). The config has `workers: 1` and `fullyParallel: false`, so the files run in name order and E18 stays last.
- Step 2 now expects `e2e:check` to report `scenarios: missing 20-e18-privacy.spec.ts` until the pattern changes (P:814).

**10. MAJOR — recovery cannot load all the keys of a mixed-generation backup. Fixed (a staged rotation).**

What I verified: `data_keys` loads exactly `data_key` and `data_key_previous` (`crypto.py:93-99`), and `decrypt` raises when no key matches (`:83`).

The fix:
- `crypto rotate` re-encrypts only what the mounted previous key opens. It leaves every other old key's ciphertexts as they are and reports them.
- `RotationReport.remaining_by_key_id` holds key ids and counts only.
- Exit codes: 0 when only the current key remains; 3 `other_keys_remain` while another key remains; 2 `no_previous_key` (P:767).
- So the two key slots recover any number of old keys, one pass per key.
- The RUNBOOK's restore runs, before `up -d`, one `docker compose run --rm --no-deps worker jobhunter crypto rotate` pass per key id the set names other than the current key, mounting each in turn as `data_key_previous` (P:836).
- Task 26 no longer tells the operator to mount keys into `restore`. That container holds no data key and decrypts nothing (P:727).
- New test `test_a_set_under_keys_a_and_b_is_recovered_under_c_in_two_passes` (P:771). The drill now checks exit codes instead of `remaining_old` (P:769).
- Deviation 31 (P:976); spec 3.7 item 17 (P:874); a RUNBOOK test (P:843).

## Sweep: commands and paths the local test steps rely on

I checked mechanically that every `tests/…`, `frontend/…`, `scripts/…`, `deploy/…` and `docs/…` path the plan names either exists or is created by a task at or before its first use. One exception is by design: `deploy/release.lock` is written by `release.yml`. I then checked the commands and fixtures by hand.

1. **`scripts/smoke_m0.py` was to be modified (Task 33).** `smoke_m1`–`smoke_m4` import `ORIGIN` and `DC` by name (`scripts/smoke_m1.py:48-62`), so rebinding them in `smoke_m0` would not reach those scripts.
   - `smoke_m5` is now self-contained: its own `httpx.Client` against `https://localhost:8443` with Caddy's root CA, `Origin` and `X-JH-Request`.
   - It imports only `emit`, `CheckFailed`, `Context`, `USERNAME`, `PASSWORD`, `stderr_tail` and `smoke_m1.SEED_INPUTS`, and puts the repository root on `sys.path` as `scripts/m4_seeds.py:251-252` does.
   - `smoke_m0.py` leaves Files and the `git add` (P:891, P:894, P:895, P:934).
   - `user create` now names `--username` (required, `cli.py:90`) and `--password-stdin`, which is refused only in prod (`cli_user.py:115`) (P:898).
   - The RUNBOOK's prod `user create` names `--username` and has no stdin (P:830).
2. **Task 21 modified `.gitignore`.** `.gitignore:3` already ignores `.secrets/`. The entry is dropped (P:613).
3. **Task 30's lint fixture and Python lint test.**
   - The fixture file under `e2e/` would have failed `eslint .` and the e2e typecheck (`frontend/e2e/tsconfig.json` includes `.`).
   - The pytest needed Node.
   - The fix: the rules move to `frontend/scripts/e2e-request-rules.mjs`, and the vitest `src/test/e2eLintRule.test.ts` runs ESLint's `Linter` over code strings, as `check-dist-csp.test.ts` imports `scripts/check-dist-csp.mjs` (P:810, P:813).
   - It runs in the default jsdom environment, because `src/test/setup.ts` touches `window`, so a `node` environment would break the setup file.
   - `ready()` is split. `support/ready.ts` exports the pure `isNotUpYet`, which the root `tsconfig.json` compiles when the vitest test imports it, and `ready()` rethrows any other error (P:810).
4. **Task 29's `${actionlint}` and `${trivy}` were undefined shell variables.** They are replaced by the lock-file lookup `ci.yml:58` uses for gitleaks (P:786, P:795).
   - The claim that `ci.yml`'s `lint` job runs actionlint was wrong (`ci.yml:23-59`). The jobs are linted locally with M0's command (P:790).
   - `prodstack.yml`'s job `env` now defines `PT`, `PB` and `S3`, as `e2e.yml` defines `DC` (P:788).
5. **`uv run python scripts/e2e_fixtures.py` in Tasks 30 and 33.**
   - It needs `--group docparse` (`python-docx`, `pyproject.toml:61-64`), which a plain `uv run` does not install, and `e2e.yml` never runs it: the fixtures are committed.
   - It is removed from both E2E command lines (P:816 and Task 33's Step 2).
6. **The test role configurations for `backup` and `restore` (Task 10).**
   - `tests/support/pg.py:57` writes secret directories for `api`, `worker` and `migrate` only, so `config_for(server, "backup" | "restore")` could not load.
   - Task 10 now modifies `pg.py`: the two roles and their secrets, `db_backup`, `db_restore` and `restic_password` (P:353).
7. **The worker's manifest and export directories in tests (Task 9).**
   - `/data/manifests` and `/data/exports` do not exist in the dev container, and the in-process and subprocess workers of Tasks 12–17 and 25 write there.
   - `server_env` now points both at the server's workdir, as it already does for `JOBHUNTER_PARSE_JOBS_DIR` (P:311).
8. **The per-test reset (Task 1).**
   - `reset_database` empties a fixed table list (`tests/support/fixtures.py:60`, `:319`). Manifests, exports and backup rows would have leaked from one test into the next, for example an `executing` manifest that makes the next test's export requeue.
   - `TRUNCATED_TABLES` gains the seven M5 tables, `deadman_state` is reset to its seeded row, and `schema_compat` is kept.
   - New test `test_reset_empties_the_m5_tables_and_restores_deadman_state` (P:112, P:128).
9. **Network names, subnets and command lines.** Each of these would fail as written:
   - **N2 (Task 22).** The probes' `https://caddy` would fail the TLS handshake, because `tls internal` certifies `localhost` only. They now connect to `172.30.1.2:443` with SNI and `Host` set to `localhost`, and with the `Origin` the CSRF check requires (P:650).
   - **PT and PB pin the same subnets.** `PB` now comes up only after `$PT down`: in the Global Constraints (P:44), the restore tests (P:731) and the rotation drill (P:769).
   - **The rollback drill (Task 27).** `up -d --build` on `app_image=jobhunter-app:prev` would rebuild the current code under that tag. `prodstack.up` gains `build=False` (P:626), and the `prev` steps use it (P:750).
   - **The prod command line.** A bare `docker compose` in `scripts/host-setup.sh` (P:647), `scripts/rotate-db-role.sh` (P:768) and the RUNBOOK (P:827) reads neither lock file, so `${app_image:?}` stops it. They now use Task 21's prod command line; the RUNBOOK names it `$JH` and gains `test_every_compose_command_uses_jh` (P:843).
   - **`rotate-db-role.sh`.** `$POSTGRES_USER` and the password file now expand inside the container through `sh -c`, not on the host (P:768).

## Other changes

- The File Structure table names `api/fence_guard.py` and the Task 30 frontend files. Deviation 45 lists `api/fence_guard.py`.
- The plan now has 50 Deviations; there were 49. `.superpowers/sdd/m5-research/plan-author-report.md` is still out of date, as round 1 noted.
- Process note: one name-only `find /` I started while looking for SQLAlchemy's source did not exclude `.secrets/` and `.fixtures-raw/`. I stopped it at once. It matched nothing there and read no file content. Every later search was limited to `.venv` or to named directories.

## New decisions to flag

Each choice below is recorded in the plan as a Deviation or a 3.7 spec item. None changes a user decision.

- **Finding 4:**
  - a transaction-level fence guard, an engine `begin` hook keyed by a context variable the edge sets, instead of per-route re-checks;
  - round 1's re-checks in `save_settings`, the profile confirm and activation and `activate_shipped_version` are removed (Deviation 15).
- **Finding 3:** exemptions matched by method and path before routing; the retry by the pattern `^/api/system/purges/[0-9]+/retry$` (Deviation 15).
- **Finding 5:**
  - `KindProcedure.reapply_admission`;
  - the generation rules: `purge_fence_at` never lowered; the disabling version only over an active version created at or before `fenced_at`; the runs queued up to `fenced_at` cancelled (Deviation 23).
- **Findings 2 and 7:** `Chunk(conn, held)`, the `with_groups_of` lock with three rounds and the `co_member_changed` requeue, targets before graph, and `leave_group` instead of `reelect` (new Deviation 50).
- **Finding 10:** `crypto rotate` exit 3 `other_keys_remain` and one rotation pass per extra key before `up -d` (Deviation 31).
- **Finding 1:** evidence for every deleted item of a submission that can still return results, whatever its target's state (Deviation 13).
- **Finding 6:** both migrate entry points keep their names; only their signatures grow (Deviation 38).
- **Sweep 3:** the E2E request rules live in `frontend/scripts/e2e-request-rules.mjs`, shared by ESLint and a vitest test.
