# M5 plan, fix round 10: the plan author's response to Codex round 10

- **Review:** `docs/council/plan-reviews/m5-round10-codex.md` (REJECT; all round-9 findings resolved, 3 new MAJOR findings). This round follows the coordinator's rulings R10-1 to R10-3.
- **Plan:** `docs/superpowers/plans/2026-11-09-m5-operations-privacy-production.md`, edited in place on top of 6c59ff2 (HEAD d820b77 adds only the review). It now has 1,122 lines. Not committed.
- **References:** `P:n` is a line of the edited plan.

All three findings hold against the code at HEAD: 3 fixed, 0 rebutted.

M5-U1–U4 and M5-R1–R8 stay binding. The decisions are folded into Deviations 16 and 24 (P:1028, P:1036) and spec 3.7 item 4 (P:928). The sweeps made 2 more fixes.

## Finding 1 (R10-1): `keep=[id]` on `api_idempotency`, which has no id

What I verified: the table's primary key is `(route, idem_key)` and it has no identity column (`0001_m0_baseline.py:120-130`).

What changed:
- **Task 1's migration 0010** adds `api_idempotency.id bigint GENERATED ALWAYS AS IDENTITY` with `UNIQUE (id)`; the primary key stays (P:118). The existing rows are numbered. The table now fits `max_identity_ids`' rule of one identity column per table.
- **Task 1's schema test** asserts the identity, its uniqueness, the numbered rows and the unchanged key (P:129).
- **Task 9's canonical-serialization test** now round-trips a delete-everything content with `keep` ids, a `retained` map and a request holding `idempotency_row_id` (P:350).
- **The route** reads its own claimed row's `id` inside `run_idempotent`'s handler and passes it on (Task 18, P:597).
- **The manifest records it.** `request_delete_everything(..., idempotency_row_id: int, ...)` stores it in the manifest's request as `{"idempotency_row_id": <id>}` (P:525).
- **Step 11 keeps it.** It bounds `api_idempotency` by its new `id` with `keep=[request.idempotency_row_id]` (P:528).
- **Test:** the existing `test_its_own_idempotency_row_survives_and_sessions_end_at_completion` now checks the row is kept by its `id`, in the live run and again in a replay on the real schema (P:547).

**R10-1 sweep: every table delete-everything bounds, checked against the schema at HEAD.** I read every `CREATE TABLE` in the migrations. Task 16 now states the bound of each table (P:538).

| Bound | Tables |
|---|---|
| `id`, one identity column at HEAD | alerts, ats_snapshots, batch_items, batch_submissions, bulk_operations, coverage_cells, coverage_intervals, critic_notes, cv_files, det_results, duplicate_groups, embeddings, eval_runs, fetch_requests, golden_cases, identity_conflicts, job_revisions, job_tombstones, jobs, llm_calls, llm_results, llm_tuples, login_attempts, merge_events, operations, posting_observations, postings, profile_drafts, profile_versions, query_proposals, queries, raw_items, raw_responses, result_evidence, runs, scope_versions, score_attempts, score_cycles, score_drafts, score_feedback, score_targets, settings_versions, source_instances, watchlist_companies; M5's `exports` |
| `id`, one identity column from Task 1 | `api_idempotency` (new `id`) |
| `generation`, from Task 1 | `login_lockouts` |
| `parent`, the bound of the named parent | `ad_key_sources` (postings), `admission_lane_credits` (llm_tuples), `ats_snapshot_members` and `ats_snapshot_absences` (ats_snapshots), `dedup_decisions` (jobs, either job), `duplicate_group_members` (duplicate_groups), `eval_case_results` (eval_runs), `hard_filter_results`, `job_revision_occurrences` and `user_job_actions` (jobs), `job_revision_sources` (job_revisions), `profile_vectors` (profile_versions), `run_attempts` (runs), `tombstone_keys` (job_tombstones via `tombstone_id`), `purged_batch_items` (batch_submissions) |
| `created_at ≤ fenced_at` | `ad_keys`, `sessions` |
| **`thread`, new (sweep fix 1)** | `checkpoints`, `checkpoint_writes`, `checkpoint_blobs` |
| frozen id lists (round 9) | `alerts`, `procrastinate_jobs`, `scope_versions` |
| kept, not deleted | `users`, `llm_budget`, `quota_windows`, `quota_ledger`, `llm_spend_ledger`, the policy tables, the singletons, `backup_sets`, `backup_runs`, the manifest tables |

The two sweep fixes:
- **Sweep fix 1: the checkpoint tables had no workable bound.** They have text keys, no identity and no `created_at`, and the plan named none. Task 9 adds the `thread` bound: the owner id that a `thread_id` names must be within that owner table's bound (P:323, P:339). The thread forms are `run-<run>-a<n>` (`worker/runs.py:137-139`), a cycle's `<prefix><cycle>` (`graphs/score/graph.py:112-114`), and a profile execution's `<prefix>-<subject>-<kind><operation>-a<n>` (`graphs/profile/graph.py:137-146`). An unknown form is a violation, never a pass.
- **Sweep fix 2: listed ids could be allocated again.** `procrastinate_jobs.id` is a `bigserial`, not an identity column, and is named by an id list. After a restore its sequence could hand out a listed id again, and a replay would then delete the new job.
  - `manifest_bounds` now takes the largest id of every `ids` and `keep` list too, and `advance_identities` covers `serial` and `bigserial` columns (P:733).
  - New test `test_listed_ids_and_serial_columns_are_advanced_past` (P:736).
  - Deleting those jobs needs no extra step: procrastinate's own delete trigger unlinks a periodic defer that references one (`procrastinate_unlink_periodic_defers_v1`), and its events cascade.

## Finding 2 (R10-2): a retained default scope, reused by hash, lands below the bound

What I verified: `ensure_hashed` reuses a row by hash (`db/seed.py:186-200`), and `set_desired_scope` writes `pending_scope_version_id` (`settings/scope_apply.py:224-237`). Step 10 retains the default scope version, which can lie below the bound.

What changed:
- **Step 8's postcondition uses the manifest's immutable sets (P:529).** Each pointer below must be NULL, above its table's bound, or in the manifest's frozen `retained` set for that table:
  - every `active_config` pointer: settings, scope, pending scope, model policy, execution config, evaluation policy, rubric, prompt schema, profile and tuple;
  - `schedule_state.settings_version_id`.
- **`retained` (new in Task 9's `ManifestContent`, P:331) is frozen at A2:**
  - **scope versions:** every one within the bound that step 10 does not list. That includes each row the reset may reuse by hash and each row a kept row references (P:530).
  - **the kept policy tables:** every row of `model_policies`, `execution_configs`, `evaluation_policies`, `rubric_versions` and `prompt_schema_versions` within the bound, because `ensure_derived` reuses their rows by hash (P:531).
  - **settings versions, profile versions and tuples:** none. Settings versions are never reused, and every profile version and tuple within the bound is deleted.
- **Tests:**
  - `test_a_retained_default_scope_pending_again_keeps_the_manifest_satisfied`, Codex's scenario (P:777). After completion another scope is applied, then settings are saved back to the default, so it is pending. `migrate` classifies the manifest `satisfied`, nothing replays, and the configuration is unchanged.
  - `test_the_reset_reuses_existing_derived_rows` now also asserts that the manifest is satisfied straight after completion (P:547).

**R10-2 sweep: other pointers and every kind's postconditions for the same hash-reuse pattern. No further fix.**

| Pointer or postcondition | Result |
|---|---|
| `schedule_state.settings_version_id` | Safe: settings versions are always new rows (`settings/save.py:134`). |
| model policy, execution config, evaluation policy, rubric, prompt schema | Safe: covered by `retained`. |
| profile version and tuple | Safe: every row within the bound is deleted. A later identical profile gets a new id (`profile_versions_live_hash` covers unpurged rows only), and its tuple is new too (`UNIQUE` on the profile and its pointers). |
| `deletion_manifests` | Safe: no kind's postcondition names a hash-reusable row. |
| the source purge's settings check | Safe: compares creation times, and settings are never reused. |
| `embeddings` (`UNIQUE` on model, text hash and kind) | Safe: deleted rows cannot be reused; a later identical text gets a new row above the bound. |
| `golden_cases` (upsert on revision and profile) | Safe: a deleted case is followed by a new row. |
| `ad_keys` | Safe: a deleted key is re-inserted with a new `created_at`. |
| `tombstone_keys` | Safe: an upsert, but no postcondition reads it. |

## Finding 3 (R10-3): `embedding_status = 'pending'` is illegal

What I verified: the CHECK allows only `awaiting_embedding` and `embedded`, with `embedded` exactly when `embedding_id` is set (`0005_m1_collection.py:519-532`).

What changed:
- The source purge's cleanup now sets `embedding_status = 'awaiting_embedding'` and `embedding_id = NULL` in one update (P:507).
- New test `test_a_revision_reusing_a_frozen_embedding_after_a2_is_re_embedded_above_the_bound` (P:510). An unrelated revision takes a frozen embedding between A2 and phase B. The purge completes, the revision is `awaiting_embedding`, and it is later re-embedded with an id above `max_ids.embeddings`.

**R10-3 sweep: every literal status or state the plan writes or asserts, checked against the migrations' CHECK lists and `db/transitions.py`. No further fix.** I extracted every `column = '<value>'`, every `` `state('reason')` `` and every arrowed state pair from the plan, and checked each:

| Values | Checked against | Result |
|---|---|---|
| runs `succeeded` / `queued` / `running` / `cancelled` / `skipped_overlap` / `missed` | runs CHECK | Valid. |
| operations `kind = 'purge'` and `'export'` | the CHECK list (`0001_m0_baseline.py:228-230`, `0009_m4_ui.py:114`) | Valid. |
| operations `pending` / `running` / `failed` / `cancelled` / `succeeded` | operations CHECK | Valid. |
| query proposals `purged` | `0006…:159` | Valid. |
| llm_calls `purpose = 'propose_queries'` and `'extract_profile'` | `0006…:219-220` | Valid. |
| batch-submission edges: `submit_started → released`, `submit_ambiguous → released`, `submitted → released`, `released → ended`, `preparing → abandoned` | `db/transitions.py:659-672` | All present. |
| score cycles `failed('cancelled')` (`status_reason` is free text); runs `cancelled('source_purge' \| 'delete_everything')` (free `status_reason`) | the reason columns | Valid. |
| targets `cancelled('purged')`; ledger `not_billed('never_dispatched')`; raw items `deferred('job_fenced')` | existing M1–M3 values | Valid. |
| exports, manifests, backup sets and runs | Task 1's own new lists | Consistent. |

The only illegal value was finding 3's `pending`.

## Line count and sweep fixes

The plan now has 1,122 lines. The sweeps made 2 fixes, both from R10-1's: the checkpoint tables' `thread` bound, and advancing identities past listed ids and `serial` columns. The R10-2 and R10-3 sweeps found nothing further.
