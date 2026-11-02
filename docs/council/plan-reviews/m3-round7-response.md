# M3 plan — response to Codex review round 7

Plan: `docs/superpowers/plans/2026-10-25-m3-agentic-scoring.md` (working tree on c17c337 / plan at 86dd118). Review: `docs/council/plan-reviews/m3-round7-codex.md` (APPROVE WITH CHANGES). P: lines are the revised plan's. Every finding was verified against the code and the spec before the fix; none is rebutted.

## A. The three partially resolved round-6 items

| Item | Disposition | Note |
|---|---|---|
| r5 B10 carry-forward (takeover fixture) | fixed | Closed by B4 below. |
| r6 B3 (adoption's expected terminal state) | fixed | Closed by B2 below. |
| r6 B5 (submission invisible to the takeover sweep) | fixed | Closed by B4 below. |

## B. New findings

| # | Severity | Disposition | New P: lines | Note |
|---|---|---|---|---|
| B1 | MINOR | fixed | 929, 931, 932, 960 | Verified `follow_on(conn, *, submission_id, defer)` (P:869) against the finalizer's declaration. `finalize_results(engine, submission_id, *, now, defer)` now takes the calling task's deferrer; `batch_poll` passes its own and `reconcile_ambiguous(engine, client, *, now, epoch, defer)` passes the task's; the release route passes the route's. `follow_on` stays inside the reconcile transaction (marker and operation insert commit with it). `test_each_entry_path_enqueues_the_follow_on_once`: the poll's and the adoption's reconcile each insert exactly one follow-on operation with the reconcile, a second run inserts none. |
| B2 | MINOR | fixed | 503 | Verified P:503: creation returned `in_progress` and only retrievals advanced the fake, while the lost-execution test (P:895–904) retrieves nothing before `reconcile_ambiguous_once`. The fake's semantics are amended: `polls_until_ended = 0` means `ended` from creation, in `retrieve` and in the listing alike (the listing reports what `retrieve` would), pinned by `tests/unit/test_fake_batches.py::test_zero_polls_means_ended_from_creation_in_retrieve_and_listing`; the lost-execution fixture's `polls_until_ended=0` therefore establishes its stated precondition. |
| B3 | MINOR | fixed | 776, 931, 960, 1384 | Verified: adoption confirms only ended candidates (P:931, rule 3) while the test demanded a `cancel` call. The adoption now carries `cancel_requested_at` forward as a record only (nothing to cancel on an ended batch; the targets were cancelled by the request and every item is `ignored_fenced`); the test asserts exactly that, and the real cancel is exercised through `record_created` (a request recorded during the send) and `batch_poll` on a `submitted` batch still `in_progress`, each calling `cancel` once right after its commit; Task 14's and Deviation 27's lists of the places that cancel no longer name the adoption. |
| B4 | MINOR | fixed | 870, 1295 | Verified `_LOCK_OWNER` checks claim and status only (`operations.py:267`), so an old execution could reach `submit_start` before reclamation. A named fault point `batch.after_prepare_commit` (after the prepare committed, before `submit_start`, P:870) is the barrier: the fixture holds the old execution there until the takeover has finished, then runs the `batch_submissions` tick reconciler once and asserts the `preparing` row `abandoned` and the released execution's `submit_start` `FencedOut`; the committed-row lifecycle cases stay separate. |
| B5 | MINOR | fixed | 617, 647, 1376 | Verified P:984, Task 21 and Deviation 19 (acknowledgement lifts the auth pause) against P:617 (any open alert). The guard now requires `acknowledged_at IS NULL` for `llm.auth_failed`, exactly as for the bound violation; Deviation 19 says the same; `test_acknowledgement_lifts_the_auth_and_bound_pauses` pins that the ack lifts both pauses while the alerts stay open, and that the guard reads neither the cap alert's ack nor its resolution (the headroom). |

**Counts:** 8 fixed (3 partials, 5 new), 0 rebutted. Plan: 1,446 lines (unchanged count; every fix is inline). Only the plan is modified in the working tree.
