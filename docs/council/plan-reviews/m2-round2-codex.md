**Verdict: REJECT**

## A. Round-1 findings

Reviewed commit `b275fb1`, its plan changes, the current code, and specification revision 3.3. Inspection and Python checks were read-only and offline; no APIs were called.

References: **P** = [revised plan](/home/user/job-hunter/docs/superpowers/plans/2026-10-20-m2-llm-profile-settings.md); **S** = [specification](/home/user/job-hunter/docs/superpowers/specs/2026-10-16-job-hunter-design.md). Line numbers refer to the current files. “Resolved” means resolved in the plan, not implemented.

| Round-1 finding | Status | Assessment |
|---|---|---|
| 1. Test reset | **NOT RESOLVED — BLOCKER** | Removing M2 tables from `TRUNCATE CASCADE` fixes the singleton deletion, but the replacement DELETE order violates an immediate FK. Details below. P:244; S:2386; repo: [fixtures.py:166](/home/user/job-hunter/tests/support/fixtures.py:166). |
| 2. C35 interleaving | **RESOLVED** | Separate injected registries permit the stale execution to resume; the additional test exercises actual local cancellation. P:602–625; S:5782; repo: [executions.py:154](/home/user/job-hunter/backend/src/jobhunter/worker/executions.py:154), [operations.py:434](/home/user/job-hunter/backend/src/jobhunter/worker/operations.py:434). |
| 3. Schedule helper arguments | **RESOLVED** | The call now matches `(anchor, interval_s, t)` and the save-path test checks the resulting cursor. P:1270, 1316; S:§3.4; repo: [seed.py:119](/home/user/job-hunter/backend/src/jobhunter/db/seed.py:119). |
| 4. Watchlist key | **RESOLVED** | Authoring, settings saves and diffs consistently use `watchlist_company_id`. P:1271, 1277, 1640; S:4845; repo: [run_settings.py:212](/home/user/job-hunter/backend/src/jobhunter/run_settings.py:212). |
| 5. Deployment permissions | **RESOLVED** | Both supplementary groups, CV-directory ownership, secret permissions and a fresh-volume test are now prescribed. P:748, 1711, 1724–1728; S:§19.1–19.2; repo: [compose.dev.yaml:23](/home/user/job-hunter/deploy/compose.dev.yaml:23), [Dockerfile:30](/home/user/job-hunter/Dockerfile:30), [dev-secrets.sh:34](/home/user/job-hunter/scripts/dev-secrets.sh:34). |
| 6. Stale cap at rollover | **RESOLVED** | Current settings are read under `active_config FOR SHARE`; the locked month supplies cap/buffer; `CallContext` no longer carries budget parameters. The combined regression test is present. P:392, 410–420, 478, 562; S:1141–1164; repo: [operations.py:244](/home/user/job-hunter/backend/src/jobhunter/worker/operations.py:244). |
| 7. Durable reservation owner | **RESOLVED** | The original claim token is persisted, and abandoned eval owners can release their reserved entries. P:400–406, 419, 431, 477; S:§7.11, §17.4; repo: [operations.py:145](/home/user/job-hunter/backend/src/jobhunter/worker/operations.py:145). Broader eval recovery has a separate gap in B.5. |
| 8. Long calls and late settlement | **RESOLVED** | The longer threshold, online late-settlement edges and atomic counter changes explicitly resolve the original conflict. P:274–275, 432–434, 475–476; S:§7.16.7, 4854; repo: [transitions.py:31](/home/user/job-hunter/backend/src/jobhunter/db/transitions.py:31). |
| 9. Paid-work replay receipt | **NOT RESOLVED — MAJOR** | The crash-before-publication case is addressed, but an old operation can still replay its receipt into a newly approved generation of the same draft. Details below. P:1047–1063, 1099–1101; S:3379, 4681; repo: [operations.py:557](/home/user/job-hunter/backend/src/jobhunter/worker/operations.py:557). |
| 10. Default buffer validation | **RESOLVED** | The per-slot calculation produces `$1.28`; the seed-validation pin and V19 escalation prevent silently weakening validation. P:333, 725, 1158–1179; S:1156; repo: [settings_v1.json:19](/home/user/job-hunter/backend/src/jobhunter/db/seed_data/settings_v1.json:19). Arithmetic checked in memory. |
| 11. Native-PDF spike allowance | **RESOLVED** | The `$0.80` reservation ceiling includes the `$0.125` native extra at the explicitly selected `$0.50` spike buffer. V19’s paid-call deviation is recorded. P:160–173, 724, 1825–1828; S:3901, 4857, 6671. |
| 12. Checkpoint exception privacy | **RESOLVED** | Every profile node is wrapped, unexpected exception content is replaced, and the test scans pending writes as well as checkpoint state. P:941–943, 978; S:3377, §16; repo: [checkpointer.py:42](/home/user/job-hunter/backend/src/jobhunter/graphs/checkpointer.py:42). |
| 13. Superseded CV retention and orphan files | **RESOLVED** | The originally missing older-version branch and failed-upload cleanup are explicit. P:821, 1692–1697; S:6298–6300; repo: [retention.py:225](/home/user/job-hunter/backend/src/jobhunter/privacy/retention.py:225). Hash-reused confirmations expose an additional case in B.3. |
| 14. Obsolete pending scope | **RESOLVED** | Every confirm reconciles the desired scope, including clearing pending B when A is already active. P:1348–1352, 1560, 1614; S:§12.6(g), §13.4; repo: [filters.py:398](/home/user/job-hunter/backend/src/jobhunter/scoring/filters.py:398). |
| 15. Incorrect pinned expectations | **RESOLVED** | `$0.125` and `$1.50` are correct; each save now acknowledges its own warnings. P:449–450, 1296–1310; S:§6.9.2, §13.2. Arithmetic checked in memory. |
| 16. Ineffective import-boundary check | **RESOLVED** | The forbidden modules now match actual imports, with positive rejection tests and a destination for `CURSOR_REUSE_TTL`. P:1702–1708; S:§4.2; repo: [planner.py:92](/home/user/job-hunter/backend/src/jobhunter/quota/planner.py:92). |

The two unresolved findings require these fixes:

1. **BLOCKER — The replacement reset deletes the parent before its child.**  
   **P:**244 explicitly deletes `llm_spend_ledger` before `llm_calls`, after clearing draft/proposal `llm_call_id` pointers. **S:**2386 defines `llm_calls.spend_ledger_id REFERENCES llm_spend_ledger(id)` without `DEFERRABLE` or cascading deletion. **Repo:** [fixtures.py:166](/home/user/job-hunter/tests/support/fixtures.py:166).

   **Failure scenario:** After any test inserts a call and reservation, reset attempts to delete its ledger row while the call still references it. PostgreSQL rejects the DELETE. Clearing the draft/proposal pointers does not remove this reference, and `SET CONSTRAINTS ALL DEFERRED` cannot defer a non-deferrable FK.

   **Expected fix:** Delete `llm_calls` before `llm_spend_ledger`, and verify the complete ordering against all immediate FKs. Keep the two-reset test populated with an actual call/ledger pair.

2. **MAJOR — An operation-scoped receipt does not fence the draft’s approval generation.**  
   **P:**1047 looks up the receipt before checking the current reviewed text; P:1058 publishes `ready`; P:1099–1101 permits editing and reapproval. **S:**4681 requires the profile to derive from the latest approved text. **Repo:** [operations.py:557](/home/user/job-hunter/backend/src/jobhunter/worker/operations.py:557) finishes the operation separately after the handler returns.

   **Failure scenario:** Operation A publishes `ready`, then the worker crashes before finishing A. The user edits and approves new text, creating operation B and returning the draft to `extracting_profile`. Reclaimed A still owns its operation row and finds its old paid receipt. The prescribed draft write checks A’s claim, but has no approval-generation predicate preventing A from publishing its old output into B’s draft.

   **Expected fix:** Persist an immutable approval generation/current extraction operation and check it on receipt reuse, publication and failure hooks. Finish obsolete operations without touching the draft. Atomically finishing the operation with publication can also close this particular window. Add a crash-after-`ready`, edit, reapprove, then resume-A test.

## B. New findings

1. **MAJOR — Proposal receipts do not distinguish the initial response from its semantic repair.**

   **P:**1485–1487, 1513–1517. **S:**3379, §11.12; the call schema at 2355–2387 provides no prescribed non-scoring repair identity. **Repo:** [operations.py:293](/home/user/job-hunter/backend/src/jobhunter/worker/operations.py:293) requeues the same operation across executions.

   **Failure scenario:** A structurally valid five-query response is stored as a completed call with `output_json`, but fails `set_invalid`. Its repair produces another completed call under the same operation ID. After a crash, both match the receipt predicate; selection and ordering are unspecified. If both responses were invalid and the crash occurred before publication of failure, replay also lacks a durable indication that the one repair has already been consumed.

   **Expected fix:** Persist the logical call/repair ordinal and validation disposition, or define equivalent deterministic reconstruction from ordered calls. Replay a usable completed repair without another request, and preserve repair exhaustion across reclaim. Test crashes after settlement of both the initial invalid response and the repair.

2. **MAJOR — Direct native-PDF uploads bypass the mandatory PDF checks.**

   **P:**818 delegates encryption/page-count validation to docparse; P:832 routes native opt-in directly to `extract_profile`; P:1060–1062 checks token count/context size only. P:1094 additionally offers native repair for `encrypted` and `too_many_pages`. **S:**4649 requires an unencrypted PDF of at most 20 pages. **Repo:** [compose.dev.yaml:85](/home/user/job-hunter/deploy/compose.dev.yaml:85) and [compose.dev.yaml:107](/home/user/job-hunter/deploy/compose.dev.yaml:107) establish the API/worker boundary the new parser service extends.

   **Failure scenario:** A 21-page PDF uploaded with native opt-in passes magic/size checks and goes directly to token counting and potentially extraction. No planned component checks its page count. This contradicts Deviation 7’s promise that these documents fail before an LLM call.

   **Expected fix:** Require sandboxed PDF validation before either native token counting or extraction, independently of text extraction. Remove native repair for hard upload-validation failures unless a separate, explicit specification change authorizes it. Pin zero outbound calls for encrypted and over-limit native uploads.

3. **MAJOR — Confirming identical content leaves the new CV upload retained indefinitely.**

   **P:**1556 reuses an existing profile version; P:1692–1697 enumerates the file-retention predicates. **S:**1502–1504, 4785, 6298–6299. **Repo:** [retention.py:105](/home/user/job-hunter/backend/src/jobhunter/privacy/retention.py:105).

   **Failure scenario:** Upload C1 creates profile V1. Later, upload C2 produces identical confirmed profile JSON, so confirmation reuses V1, whose `cv_file_id` remains C1. C2 belongs to a confirmed draft but no profile version directly references it. It is neither an older version’s file, a discarded/failed draft’s file, nor an upload without a draft. The orphan-file rule also excludes it because its `cv_files.storage_path` exists. Its original file and raw text therefore survive indefinitely.

   **Expected fix:** Explicitly handle confirmed uploads whose confirmation reused another version. Define their retention deadline while protecting active/previous files and open drafts. Test identical-content confirmation with distinct upload IDs. Also cover reactivation of an older version, since “older than previous” does not select non-protected versions numerically newer than the reactivated version.

4. **MAJOR — Checkpoint thread IDs collide across distinct operations.**

   **P:**950 derives the suffix from the operation’s `attempts`; P:1484 permits successive proposals for one profile; P:1699 purges proposal threads by profile-version prefix. **S:**3374–3379, 3590, 6320. **Repo:** [0001_m0_baseline.py:236](/home/user/job-hunter/backend/src/jobhunter/db/migrations/versions/0001_m0_baseline.py:236) initializes every operation’s attempts to zero; [collect/state.py:9](/home/user/job-hunter/backend/src/jobhunter/graphs/collect/state.py:9) documents existing attempt isolation.

   **Failure scenario:** Two successive proposal operations for profile 7 both use `queries-7-q0`. Reapprovals of one draft similarly reuse `profile-<draft>-p0`. An in-memory check with the installed LangGraph confirmed that changing `operation_id` on the same thread does not clear an omitted old `llm_call_id`. Independently, retention for an old proposal can delete the shared thread belonging to a newer or running proposal.

   **Expected fix:** Include the operation/proposal/approval identity in the thread key, or allocate a durable monotonic suffix within the existing naming scheme. Tie cleanup to that exact execution’s terminal time. Test successive operations, reapproval, and cleanup while a newer proposal runs.

5. **MAJOR — The revised unsettled-call deadline lookup cannot handle schema dry-runs.**

   **P:**432 requires the deadline of the call’s node from its execution config. P:706 and 713 instead define `schema_dry_run` with a standalone 300-second constant. **S:**4006, 5505. **Repo:** [settings_v1.json:23](/home/user/job-hunter/backend/src/jobhunter/db/seed_data/settings_v1.json:23), [seed.py:178](/home/user/job-hunter/backend/src/jobhunter/db/seed.py:178).

   **Failure scenario:** The CLI dies after dispatching a schema-dry-run call. Its persisted execution config contains only `generate`, `revise`, `critic`, `extract_profile`, and `propose_queries`. Looking up `schema_dry_run.deadline_s` cannot produce the reconciler’s threshold, so the prescribed recovery path fails. Moreover, an eval process dying between calls leaves no unsettled reservation through which `owner_holds()` can mark the run abandoned.

   **Expected fix:** Define deadline resolution for every purpose, explicitly including schema dry-runs, or persist the effective deadline with the call. Add an independent abandoned-eval sweep rather than relying exclusively on reservation inspection. Test crashes after dispatch and between completed cases.