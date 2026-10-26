# M3 plan — response to Codex review round 6

Plan: `docs/superpowers/plans/2026-10-25-m3-agentic-scoring.md` (working tree on 769d5cd / plan at 1dd85e1). Review: `docs/council/plan-reviews/m3-round6-codex.md`. P: lines are the revised plan's. Every finding was verified against the code and the spec before the fix; none is rebutted.

## A. The one partially resolved round-5 item

| Item | Severity | Disposition | New P: lines | Note |
|---|---|---|---|---|
| r5 B10 (takeover fixture's lifecycle expectation) | MINOR | fixed | 1295 | Closed by B5 below: the concurrency fixture asserts the `preparing` row's fate only after the `batch_submissions` tick reconciler has run once; the committed-row lifecycle tests stay separate. |

## B. New findings

| # | Severity | Disposition | New P: lines | Note |
|---|---|---|---|---|
| B1 | MAJOR | fixed | 929, 931, 960, 963, 1384 | Verified: `process_item` takes the job row before the submission and ends on `ranking_state`, and the round-5 finalizer processed missing items on one connection after the submission lock. `finalize_results(engine, …)` is now an orchestration of three kinds of transactions: (1) completion — `active_config` → owner (`ended`, marker NULL) → `results_complete_at`, the `missing` count and `batch.items_missing`; (2) each missing item through `process_item` in a transaction of its own in the schedule's order; (3) reconcile — `active_config` → owner (`ended`, marker set) → no unprocessed item → `ended → reconciled`, `complete_if_done`, `follow_on`. Recovery is the rows: marker NULL → stream again (idempotent on `processed_at`) then complete; marker set → skip the stream, run (2) and (3); a cut stream never sets the marker so nothing is decided `missing` early (L3961). Step 3 and Deviation 27 say the same. Test: three missing items against a concurrent `merge` holding a job row and waiting for `ranking_state`, `lock_waits`, crashes after the completion commit and after the second missing item, and before the completion commit. |
| B2 | MINOR | fixed | 931, 960 | Verified rule 3/4 of §10.10 (L3987–3990): confirmation needs exact `custom_id` equality; an omitted item is a partial intersection. The adoption test now uses complete matching sets for both origins, asserts quarantine (`batch.custom_id_anomaly`, nothing adopted) for the incomplete candidate, and the missing-item finalization is tested on an already-identified submission in `test_batch_poll.py` (B1's test). The adoption prose states that no item can be `missing` there. |
| B3 | MINOR | fixed | 896, 903–904 | The lost-execution test now scripts the fake with `polls_until_ended=0` (the created batch is ended with a line per `custom_id` when listed) and expects `reconciled` with `results_complete_at` set after `reconcile_ambiguous_once` for the `after_create` point, `submit_ambiguous` with the marker NULL for the other. |
| B4 | MINOR | fixed | 815, 819, 822, 847 | Verified `recompute_stale` returns `len(job_ids)` with the ids local (`filters.py:466–500`). New shared chunk helper `filters.recompute_stale_ids(conn, scopes, *, guard, since) -> list[int]` is the body; `recompute_stale` becomes `len(await recompute_stale_ids(...))`, signature and count unchanged; the completeness reconciler calls the helper and then `targets.refilter_targets_of_jobs` over exactly those ids in the same transaction under the job rows it holds, no second stale query. |
| B5 | MINOR | fixed | 1295 | Verified against P:865/868: the old prepare is paused at step 5, before its submission insert, so with `batch_submissions` registered first the sweep can scan before the row exists. The fixture now stays about lock ordering (reclaim, no deadlock or timeout, the old attempt's target by commit order) and asserts `abandoned` only after one run of the `batch_submissions` tick reconciler, with the old handler's `submit_start` `FencedOut`; the committed `preparing`/`submit_started` lifecycle cases remain their own assertions. |

**Counts:** 6 fixed (1 partial, 5 new), 0 rebutted. Plan: 1,445 → 1,446 lines (one assertion line added to the lost-execution test). Only the plan is modified in the working tree.
