**Verdict: REJECT**

## A. Previously open findings

Reviewed `3db138f`, the specified diff, branch code, and specification rev 3.3. Inspection and in-memory checks were read-only and offline.

**P:** [plan](/home/user/job-hunter/docs/superpowers/plans/2026-10-20-m2-llm-profile-settings.md). **S:** [specification](/home/user/job-hunter/docs/superpowers/specs/2026-10-16-job-hunter-design.md). “Resolved” means resolved in the plan, not implemented. These statuses address the specific previous findings; Section B identifies additional interactions.

| Item | Status | Assessment |
|---|---|---|
| Reset DELETE order | **RESOLVED** | P:244–251 now deletes calls before their ledger rows, detaches incoming call references, and defers the draft/version cycle. The populated two-reset test covers the original failure. Checked against S:§7.4, §7.11 and repo: [fixtures.py:166](/home/user/job-hunter/tests/support/fixtures.py:166); the immediate M2 FK ordering has no remaining inversion. |
| Receipt versus approval generation | **RESOLVED** | P:962, 1064–1075 closes the publication/completion window with `finish_in`, adds the reviewed-text predicate, and specifies obsolete-operation handling and the reclaim/reapprove test. This addresses S:4681 and the separate completion currently at repo: [operations.py:557](/home/user/job-hunter/backend/src/jobhunter/worker/operations.py:557). |
| B1 — Proposal repair identity | **RESOLVED** | P:426, 579, 1061–1068 and 1506–1508 distinguish logical calls 1 and 2 and preserve structural-invalid outcomes as receipts. Crash tests cover both settlements. Compatible with S:2355, 2394 and §9.1’s receipt rule; repo: [operations.py:293](/home/user/job-hunter/backend/src/jobhunter/worker/operations.py:293) preserves the operation across retries. |
| B2 — Native-PDF validation | **RESOLVED** | P:761–764, 1078, 1094 and 1115 require sandboxed validation before token counting and remove native repair for hard PDF failures. This satisfies S:4649 while preserving the process boundary in repo: [compose.dev.yaml:85](/home/user/job-hunter/deploy/compose.dev.yaml:85). |
| B3 — Reused-version upload retention | **RESOLVED** | P:1719–1722 explicitly includes confirmed uploads without their own version reference and selects previous versions by activation order. Tests cover duplicate uploads and older-version reactivation. This fixes the predicate omission under S:6298–6299; repo: [retention.py:105](/home/user/job-hunter/backend/src/jobhunter/privacy/retention.py:105). A separate concurrency problem remains below. |
| B4 — Checkpoint thread collisions | **RESOLVED** | P:958–961 includes operation and attempt identity, and P:1725 ties deletion to that operation. This addresses S:3374–3379 and the independently initialized operation attempts in repo: [0001_m0_baseline.py:236](/home/user/job-hunter/backend/src/jobhunter/db/migrations/versions/0001_m0_baseline.py:236). |
| B5 — Unsettled deadlines and abandoned evals | **RESOLVED** | P:413, 426, 439 and 484 persist the effective deadline and sweep abandoned eval runs independently of unsettled reservations. Schema dry-runs therefore need no nonexistent execution-config node. Checked against S:4006, §17.4 and repo: [settings_v1.json:23](/home/user/job-hunter/backend/src/jobhunter/db/seed_data/settings_v1.json:23). |

## B. New findings

1. **MAJOR — Direct failure writes and failure hooks attempt the same transition twice.**

   **P:** [945–947](/home/user/job-hunter/docs/superpowers/plans/2026-10-20-m2-llm-profile-settings.md:945), 962–969, 1075, 1511–1514. **S:** §7.16.8, §9.1. **Repo:** [transitions.py:386](/home/user/job-hunter/backend/src/jobhunter/db/transitions.py:386), [operations.py:253](/home/user/job-hunter/backend/src/jobhunter/worker/operations.py:253).

   **Failure scenario:** A proposal’s second invalid response causes the node to transition the proposal `running → failed`, then finish the operation as failed in the same transaction. Task 11 requires that finish to invoke the failure hook, which again transitions the proposal `running → failed`. The existing `transition()` raises `TransitionConflict` when the source state no longer matches; it does not silently skip. The transaction consequently rolls back instead of committing the intended terminal outcome. The same overlap exists for direct draft failures.

   I checked the repeated transition using the repository helper with an in-memory connection stub.

   **Expected fix:** Define one owner of the domain failure transition, or explicitly make hooks no-ops when the domain row is already terminal. Preserve claim fencing and atomic completion. Pin direct failures—such as `low_text`, refusal and exhausted repair—to commit both terminal states without a rollback/requeue.

2. **MAJOR — CV-file deletion is not serialized with profile reactivation.**

   **P:** [1577–1588](/home/user/job-hunter/docs/superpowers/plans/2026-10-20-m2-llm-profile-settings.md:1577), [1716–1722](/home/user/job-hunter/docs/superpowers/plans/2026-10-20-m2-llm-profile-settings.md:1716). **S:** §12.6(a)–(c), 6294, 6298–6299. **Repo:** [retention.py:196](/home/user/job-hunter/backend/src/jobhunter/privacy/retention.py:196), [epoch.py:93](/home/user/job-hunter/backend/src/jobhunter/worker/epoch.py:93).

   **Failure scenario:** With V3 active and V2 previous, retention selects V1’s file for deletion. Before the unlink, confirmation reuses V1 and commits it as active. Retention then deletes V1’s file using its earlier selection. The prescribed worker-epoch fence locks `worker_heartbeat`; it provides no exclusion against API confirmation’s `active_config` lock. Rechecking eligibility only in the subsequent database UPDATE is too late—the file is already gone.

   **Expected fix:** Specify coordination with activation: acquire `active_config FOR SHARE` before selecting/rechecking protected files and hold it through unlink and the corresponding database update, with appropriate draft/file locking. Add a pause-after-selection test racing retention against reactivation, proving that deletion cannot proceed using an obsolete protection set.