**Verdict: APPROVE WITH CHANGES**

Reviewed `86dd118` against round 6, its response, the spec and repository interfaces. I found no remaining BLOCKER or MAJOR defect. All inspection and probes were offline and read-only; no paid calls or restricted-directory access. The workspace is unchanged.

“Resolved” means corrected in the plan, not implemented or runtime-tested. **P** denotes the implementation plan.

## A. Round-6 dispositions

| Item | Disposition | Evidence |
|---|---|---|
| Carry-forward r5 B10 — takeover fixture | **PARTIALLY RESOLVED** | [P:1295](/home/user/job-hunter/docs/superpowers/plans/2026-10-25-m3-agentic-scoring.md:1295) now waits for tick recovery before asserting abandonment. A remaining dispatch race needs an explicit barrier; see B4 below. |
| B1 — finalizer lock schedule | **RESOLVED** | [P:929](/home/user/job-hunter/docs/superpowers/plans/2026-10-25-m3-agentic-scoring.md:929), P:960,963 separate completion, each missing item, and reconciliation. Recovery distinguishes NULL/set completion markers; missing-item transactions acquire jobs before submissions and retain no locks across items. |
| B2 — impossible missing-item adoption fixture | **RESOLVED** | [P:931](/home/user/job-hunter/docs/superpowers/plans/2026-10-25-m3-agentic-scoring.md:931), P:960 require exact matching sets for adoption, quarantine partial intersections, and exercise missing items through ordinary polling. |
| B3 — adoption’s expected terminal state | **PARTIALLY RESOLVED** | [P:895](/home/user/job-hunter/docs/superpowers/plans/2026-10-25-m3-agentic-scoring.md:895), P:903–904 correctly expect `reconciled` and a completion marker. The fake’s specified lifecycle does not yet guarantee the candidate is ended; see B2 below. |
| B4 — recomputed IDs unavailable to completeness caller | **RESOLVED** | [P:822](/home/user/job-hunter/docs/superpowers/plans/2026-10-25-m3-agentic-scoring.md:822), P:847 introduce the list-returning helper, preserve the count-returning wrapper, and refilter exactly those IDs in the same transaction without another stale query. |
| B5 — submission invisible to takeover sweep | **PARTIALLY RESOLVED** | [P:1295](/home/user/job-hunter/docs/superpowers/plans/2026-10-25-m3-agentic-scoring.md:1295) fixes recovery of a late-committing preparation, but does not explicitly prevent that execution from reaching `submit_start` before reclamation. See B4 below. |

## B. New defects and remaining gaps

1. **MINOR — The new finalizer omits its enqueue dependency.**

   [P:929](/home/user/job-hunter/docs/superpowers/plans/2026-10-25-m3-agentic-scoring.md:929) declares `finalize_results(engine, submission_id, *, now)` and requires its reconciliation transaction to call `follow_on`. But [P:869](/home/user/job-hunter/docs/superpowers/plans/2026-10-25-m3-agentic-scoring.md:869) declares `follow_on(conn, *, submission_id, defer)` with a required deferrer. The adoption caller also lacks `defer`.

   Specify either propagation through both callers into the finalizer or an explicit `defer_in_tx` dependency. Keep the follow-on marker and operation creation in the reconciliation transaction, and test that both entry paths enqueue once.

2. **MINOR — `polls_until_ended=0` does not establish the adoption fixture’s stated precondition.**

   [P:503](/home/user/job-hunter/docs/superpowers/plans/2026-10-25-m3-agentic-scoring.md:503) specifies that creation returns `in_progress`, while **retrieval** advances the fake to `ended`. [P:895–904](/home/user/job-hunter/docs/superpowers/plans/2026-10-25-m3-agentic-scoring.md:895) performs no retrieval before ambiguous reconciliation, whose candidate selection only processes ended batches.

   Explicitly retrieve/end the created fake batch before reconciliation, or amend and test the fake’s zero-poll/listing semantics. The revised terminal-state assertion is correct.

3. **MINOR — Another adoption test still requires an unreachable cancellation.**

   [P:960](/home/user/job-hunter/docs/superpowers/plans/2026-10-25-m3-agentic-scoring.md:960) requires a confirmed adoption with `cancel_requested_at` to call `cancel`. Yet P:931 confirms only **ended** candidates and conditions cancellation on the provider batch **not** being ended.

   Align the test with that condition: preserve the recorded request and fenced target outcomes during ended-batch adoption. Exercise actual cancellation through `record_created` or polling an unfinished batch.

4. **MINOR — The takeover fixture needs a barrier before the old execution’s next transaction.**

   With `batch_submissions` registered first, this ordering remains possible:

   - The batch sweep sees no submission.
   - Preparation commits after the month lock is released.
   - The old execution commits `submit_start` before the operations sweep reclaims it.
   - Tick recovery correctly produces `submit_ambiguous`, contradicting P:1295’s unconditional `abandoned`.

   [P:866](/home/user/job-hunter/docs/superpowers/plans/2026-10-25-m3-agentic-scoring.md:866) uses `lock_operation_owner`; its [actual predicate](/home/user/job-hunter/backend/src/jobhunter/worker/operations.py:267) checks claim and status, not the newly claimed worker epoch.

   Pause after preparation commits, before `submit_start`, until takeover finishes. Then run tick recovery and assert abandonment plus `FencedOut`.

5. **MINOR — The admission guard’s auth-alert wording contradicts the settled acknowledgement rule.**

   [P:617](/home/user/job-hunter/docs/superpowers/plans/2026-10-25-m3-agentic-scoring.md:617) pauses for any open `llm.auth_failed`. [P:984](/home/user/job-hunter/docs/superpowers/plans/2026-10-25-m3-agentic-scoring.md:984), Task 21 and Deviation 19 explicitly require acknowledgement to lift that pause.

   Make Task 11’s predicate explicitly require `acknowledged_at IS NULL`, consistent with the later ruling, and pin acknowledgement behavior in the admission-guard test.