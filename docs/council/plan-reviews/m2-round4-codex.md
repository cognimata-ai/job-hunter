**Verdict: REJECT**

## A. Round-3 findings

Reviewed `2bdc5d2`, its diff from `3db138f`, branch code, and specification rev 3.3. Inspection was read-only and offline. “Resolved” means resolved **in the plan**, not implemented.

1. **RESOLVED — Duplicate failure transitions.**

   **P:** [947–948](/home/user/job-hunter/docs/superpowers/plans/2026-10-20-m2-llm-profile-settings.md:947), 963, 993. **S:** §7.16.8, §9.1. **Repo:** [operations.py:253](/home/user/job-hunter/backend/src/jobhunter/worker/operations.py:253), [transitions.py:449](/home/user/job-hunter/backend/src/jobhunter/db/transitions.py:449).

   The hook now locks and checks the domain row, skipping a transition already completed by the node. This avoids the existing helper’s `TransitionConflict` while preserving atomic domain failure and operation completion. The new pin covers `low_text`, refusal, and exhausted repair.

2. **RESOLVED — CV deletion versus profile reactivation.**

   **P:** [1724–1725](/home/user/job-hunter/docs/superpowers/plans/2026-10-20-m2-llm-profile-settings.md:1724), 1579, 1749–1751. **S:** §12.6(a)–(c), §20.1. **Repo:** [retention.py:196](/home/user/job-hunter/backend/src/jobhunter/privacy/retention.py:196), [epoch.py:93](/home/user/job-hunter/backend/src/jobhunter/worker/epoch.py:93).

   Holding `active_config FOR SHARE` from protection-set selection through unlink and commit excludes confirmation’s `FOR UPDATE`. The two-order test addresses the reported stale-selection race. A separate retry interaction remains below.

## B. New findings

1. **MAJOR — CV retention still races with reopening a failed draft.**

   **P:** [1125–1128](/home/user/job-hunter/docs/superpowers/plans/2026-10-20-m2-llm-profile-settings.md:1125), [1719–1725](/home/user/job-hunter/docs/superpowers/plans/2026-10-20-m2-llm-profile-settings.md:1719). **S:** [§7.16.8, failed→extracting_profile](/home/user/job-hunter/docs/superpowers/specs/2026-10-16-job-hunter-design.md:2998), §12.7, [§20.1](/home/user/job-hunter/docs/superpowers/specs/2026-10-16-job-hunter-design.md:6294). **Repo:** [transitions.py:435](/home/user/job-hunter/backend/src/jobhunter/db/transitions.py:435), [api_idempotency.py:131](/home/user/job-hunter/backend/src/jobhunter/api_idempotency.py:131).

   **Failure scenario:** A native-PDF draft has been `failed('refused')` for over 30 days. Retention selects and locks its `cv_files` row for deletion. Before unlink, `/retry` with `mode="llm"` changes the draft to `extracting_profile` and commits its new operation. The draft already has `native_pdf=true`, so this retry need not update the file row. The plan requires neither a conflicting `active_config` lock nor a shared draft-lock protocol for retry; the existing transition and idempotency helpers supply neither automatically. Retention then deletes the original PDF of a now-open draft using its earlier protection set. The accepted extraction cannot proceed.

   There is also no prescribed rejection when retry encounters already-purged input.

   **Expected fix:** Serialize retry admission with retention eligibility—for example, have retry acquire `active_config FOR UPDATE` before domain locks, and verify the required input remains available before reopening the draft. Coordinate draft-content purging through the draft lock as well. Reject expired-input retries with a defined response directing the user to upload or paste again. Add both-order tests: retry-first preserves its input; purge-first causes a clean rejection without creating an extraction operation.