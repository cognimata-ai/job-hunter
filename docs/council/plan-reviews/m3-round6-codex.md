**Verdict: REJECT**

Reviewed commit `1dd85e1` against round 5, the author’s response, the spec and repository interfaces. The three settled simplifications remain accepted. All inspection and probes were offline and read-only; no paid calls or restricted-directory access occurred. The workspace is unchanged.

“Resolved” below means corrected in the plan, not implemented or runtime-tested. **P** denotes the [revised plan](/home/user/job-hunter/docs/superpowers/plans/2026-10-25-m3-agentic-scoring.md).

## A. Round-5 dispositions

| Round-5 item | Disposition | Evidence |
|---|---|---|
| Carry-forward: r4 B9 — batching an existing online draft | **RESOLVED** | P:526,864,912 now correlate the shared predicate to each candidate and prescribe a mixed-candidate test. |
| B1 — uncorrelated resumable-draft predicate | **RESOLVED** | P:526 uses `a.target_id = t.id`; the single-target wrapper supplies its own outer row. Offline extraction confirms the shared fragment binds only `policy`. |
| B2 — eval terminal-commit recovery | **RESOLVED** | P:1004,1006,1290 require atomic run completion and `op.finish_in`, terminal-state tolerance at entry, and a failure hook restricted to running runs. |
| B3 — refilter hook unsafe for scope precomputation | **RESOLVED** | P:822,844,847 separate pending-scope computation from `refilter_job_live`; live callers acquire configuration before jobs. A remaining helper-interface omission is new B4 below. |
| B4 — monthly cap-alert lifecycle | **RESOLVED** | P:457,617,710,755 preserve the once-only marker, derive the pause from current-month headroom, resolve obsolete alerts, and cover rollover and repeated denial. |
| B5 — adoption omits completion marker | **RESOLVED** | P:928,930,959 explicitly require `results_complete_at` through the shared finalizer. Its transaction structure introduces new B1 below. |
| B6 — parser-family interface leftovers | **RESOLVED** | P:308 names `GeneratorWire`; P:929 uses `family=version.parser_family`; P:959 tests that keyword. |
| B7 — mutation test contradicts cache contract | **RESOLVED** | P:308 now inserts a separate inactive version; it no longer expects an unchanged cache key to refresh. |
| B8 — obsolete eval-resume assertion | **RESOLVED** | P:1006 requires interruption failure, no further calls, and a fresh run beginning at repetition 1. |
| B9 — released-submission retention clocks | **RESOLVED** | P:1144,1147,1247,1387 consistently use `submit_started_at`. Release preserves counted spend; the old-submission fixture now matches that rule. |
| B10 — takeover fixture’s lifecycle expectation | **PARTIALLY RESOLVED** | P:1294 correctly establishes separate committed `preparing` and `submit_started` cases. However, its concurrency fixture still asserts a lifecycle result without ensuring the submission exists before the relevant sweep. See new B5. |

## B. New defects and regressions

### B1. MAJOR — The shared batch finalizer violates the lock schedule

**Evidence:** [P:928](/home/user/job-hunter/docs/superpowers/plans/2026-10-25-m3-agentic-scoring.md:928), P:929,962; global schedule P:86.

`finalize_results(conn, ...)` locks the submission and writes `results_complete_at`, then processes every missing item through `process_item` on that connection. But `process_item` must acquire the item’s **job before the submission**, and it bumps `ranking_state`. Processing another missing item subsequently acquires another job lock after ranking.

This creates a concrete deadlock opportunity: finalization processes missing item A and holds ranking; a concurrent merge holds job B and waits for ranking; finalization proceeds to missing item B and waits for job B. It also contradicts P:962’s explicit one-transaction-per-item contract.

**Required change:** Make completion orchestration use separate transactions: persist verified stream completion, process missing items individually in the established lock order, then reconcile only after no awaiting items remain. Specify recovery between those steps and test multiple missing items against a concurrent job mutation.

### B2. MINOR — The new adoption test requires an impossible confirmation

**Evidence:** [P:959](/home/user/job-hunter/docs/superpowers/plans/2026-10-25-m3-agentic-scoring.md:959), P:930; spec §10.10’s exact adoption procedure.

The new test requires a “confirmed batch whose stream lacks one of our items” to mark that item missing during adoption. Confirmation still requires complete download and **exact equality** of the provider’s `custom_id` set with ours. An omitted item produces a partial intersection, which must remain quarantined with `batch.custom_id_anomaly`.

**Change:** Use complete matching sets for adoption-finalization tests. Test missing-item finalization on an already-identified submission, and assert quarantine for an incomplete candidate set. Preserve the settled exact-match rule.

### B3. MINOR — The lost-execution test expects adoption to stop before its required final state

**Evidence:** [P:903](/home/user/job-hunter/docs/superpowers/plans/2026-10-25-m3-agentic-scoring.md:903), P:930,959.

After `reconcile_ambiguous_once`, the created-batch branch expects `ended`. The revised contract requires a confirmed adoption to finish **`reconciled` in that same pass**, without a subsequent poll.

**Change:** Explicitly make the candidate ended before reconciliation and expect `reconciled` with `results_complete_at`. If testing the intermediate adoption commit, pause at a named fault point instead.

### B4. MINOR — The completeness caller cannot obtain the prescribed recomputed job IDs

**Evidence:** [P:822](/home/user/job-hunter/docs/superpowers/plans/2026-10-25-m3-agentic-scoring.md:822), P:847; [filters.py:466](/home/user/job-hunter/backend/src/jobhunter/scoring/filters.py:466).

The new completeness path calls `refilter_targets_of_jobs(conn, job_ids, scope)` after `recompute_stale` returns, while P:847 requires its signature to remain unchanged. The existing function returns only an integer count; its selected `job_ids` remain local. Offline AST inspection confirms returns of `0` or `len(job_ids)`.

**Change:** Specify how the caller receives the exact selected IDs—for example, a shared internal chunk helper returning IDs while preserving the existing count-returning wrapper. Avoid a second stale-row query after those rows have already been repaired.

### B5. MINOR — The revised takeover fixture still depends on an uncontrolled commit order

**Evidence:** [P:1294](/home/user/job-hunter/docs/superpowers/plans/2026-10-25-m3-agentic-scoring.md:1294), P:865,868.

The old preparation is paused at step 5, **before inserting its submission**. With `batch_submissions` registered first, that sweep can finish while no submission is visible. Releasing the month lock then lets preparation commit; the operations sweep subsequently reclaims its operation. The completed takeover can therefore leave a `preparing` row for the tick reconciler, contrary to the fixture’s unconditional `abandoned` assertion.

**Change:** Keep this fixture focused on lock ordering, then run the tick reconciler before asserting abandonment; alternatively, add a barrier ensuring preparation commits before the batch sweep scans. Retain the separate committed-row lifecycle tests.