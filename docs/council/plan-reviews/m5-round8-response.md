# M5 plan, fix round 8: the plan author's response to Codex round 8

- **Review:** `docs/council/plan-reviews/m5-round8-codex.md` (REJECT; it resolves both round-7 findings and raises 2 new MAJOR findings).
- **Plan:** `docs/superpowers/plans/2026-11-09-m5-operations-privacy-production.md`, edited in place on top of d04bedf (HEAD a755030 adds only the review). Not committed.
- **References:** `P:n` is a line of the edited plan.

Both findings hold against the code at HEAD: 2 fixed, 0 rebutted.

The sweep of every purge kind for deletions that remove state an in-flight side effect still needs found 2 more defects, both in delete-everything, and fixed them. M5-U1–U4 and M5-R1–R8 stay binding. The decisions are folded into Deviation 24 and spec 3.7 item 21 (P:1014, P:923).

## Findings

**1. MAJOR — delete-everything could erase a batch cancellation before it was sent. Fixed: the purge sends the cancel itself, before step 3.**

What I verified:
- `record_cancel` only records `cancel_requested_at`.
- The provider cancel is sent later by `cancel_requested` (`scoring/batch_results.py:199-207`), which the poll calls for each submission it reads (`:140-143, 820-825`). Once delete-everything's step 3 deletes the row, no poll can find it.
- §20.4's table asks for "`messages.batches.cancel` after commit (3 tries, then logged)".

The fix (P:521):
- **The order.** After step 2's recording transaction commits, and before step 3 touches a submission, the purge sends every recorded cancel itself through `cancel_requested`, with no transaction open.
- **What `cancel_requested` does.** It retrieves the batch first and sends `messages.batches.cancel` only while the batch is `in_progress`. It makes `CANCEL_TRIES` tries, then logs `batch_cancel_gave_up` with the ids, as §20.4 prescribes. A cancel is attempted, so a provider outage does not hold the global fence; the spend is already bounded, because §20.4 keeps the entries at their reservation.
- **Restartability.** Every submission still carrying `cancel_requested_at` when phase B (re)starts is sent again before step 3. A crash between the recording and the send therefore loses nothing, and a repeat finds the batch `canceling` and sends nothing.
- **Live purges are unchanged.** Their submission rows stay, so the next poll still sends (Task 11).
- Task 16's Consumes names `cancel_requested`, `CANCEL_TRIES` and the client (P:517).

New tests (P:525):
- `test_recorded_cancels_are_sent_before_the_submissions_go`: the poller is withheld. One `cancel` reaches the fake API before the row goes, and a fake refusing all three tries leads to `batch_cancel_gave_up` and the deletion going on.
- `test_a_crash_after_recording_the_cancel_resends_it_on_restart`: the new fault point `privacy.delete_everything.after_cancel_recorded`.

**2. MAJOR — Task 14's synthetic-profile rule contradicted Deviation 25. Fixed: the rule is now unconditional.**
- Task 14's predicate is an unconditional `NOT synthetic_eval`, which matches Deviation 25 (P:467). The reason, now stated there:
  - `seed-adversarial` reuses an unpurged synthetic version and re-creates active cases on it (`scoring/golden.py:574-576, 615-619, 647-649, 797-813`);
  - so a synthetic version made eligible by deleting or deactivating its cases could be re-seeded after A0, leaving an active case on purged content.
- `test_synthetic_eval_versions_are_never_selected` is parametrized over a synthetic version with active cases, with only inactive cases, and with no case left. It also checks that a later `seed-adversarial` reuses the version intact (P:483).

## Sweep: does any purge delete state that an in-flight side effect still needs?

The side effects I checked, in every purge kind:
- outbound provider calls: online LLM calls, batch creates and cancels, collection fetches;
- the ntfy dispatcher;
- the dead-man pings;
- the shipper;
- the embedder, docparse and LangSmith.

| Purge kind | Side effect | Result |
|---|---|---|
| job tombstone | batch cancels | Safe: `record_cancel` keeps the submission rows, so the poll sends the cancel (Task 11). |
| job tombstone | late batch lines | Safe: `purged_batch_items` keeps the evidence that skips them. |
| job tombstone | online calls | Safe: settled before their calls go (round 3); a late response settles nothing. |
| job tombstone | the embed node | Safe: a job deleted under it writes nothing (round 4). |
| job tombstone | alerts, runs, manifests | Safe: no alert or run is deleted; the manifest file is kept for the shipper. |
| source purge | collection fetches in flight | Safe: A0 cancels the runs, and the run-owner guard fences their writes. The quota ledger needs no deleted row (`fetch_request_id`, `run_id` `SET NULL`; round 3). |
| source purge | batches, eval calls, exports | Safe: batch rows are kept, so the poll sends their cancels; eval calls are settled first (round 3); exports follow the state table (round 5). |
| profile purge | proposal calls in flight | Safe: their operations are cancelled at A0. A late response stores no content (`content_purged_at`, round 4) and keeps its billing. |
| profile purge | `embed_profile` | Safe: its handler re-checks the version under its row lock and writes nothing for a fenced or purged version. |
| golden-case purge | running evals | Safe: it waits out a running eval (round 3); the calls of finished evals are settled. |
| delete-everything | recorded batch cancels | **Finding 1, fixed.** |
| delete-everything | dead-man pings | **Fixed (sweep 1)**, below. |
| delete-everything | the ntfy dispatcher and the score-lane gates | **Fixed (sweep 2)**, below. |
| delete-everything | online LLM calls | Safe: settled before their calls go; late responses settle nothing (round 3). |
| delete-everything | the shipper | Safe: manifests, shipments and fences are kept (`DELETE_EVERYTHING_KEEPS`). |
| delete-everything | exports | Safe: their own ops, by the state table. |
| delete-everything | docparse jobs | Safe: docparse removes each job's directory in a `finally`. |
| delete-everything | LangSmith | Safe: traces carry no inputs or outputs. |
| delete-everything | `deadman_sync` | Safe: it works from the kept `deadman_state` and `schedule_state`. |

**Sweep 1: dead-man pings.** Step 7 deleted runs that ended before the purge but had not been pinged yet. The tick's durable path pings runs above the watermark, so a deleted row lost its ping, and healthchecks would see no ping for a whole interval and raise a false dead-man alarm.
- Task 5 now exposes the durable path as `DeadmanPinger.ping_pending()` (P:223).
- Delete-everything runs it once before step 7, with no transaction open (P:521).
- Test: `test_a_finished_unpinged_run_is_pinged_before_the_runs_go` (P:525).

**Sweep 2: the dispatcher's outbox and the score-lane gates.** Step 11 deleted every alert, the open ones included.
- **The harm.** An open alert is the dispatcher's outbox and may not have been pushed yet. An unacknowledged `llm.reservation_bound_violated` or `llm.auth_failed` also pauses the score lane (`scoring/cycles.py:257-265`). Deleting it dropped the push and lifted the pause without an acknowledgement.
- **The fix.** Step 11 now deletes only resolved alerts, and `DELETE_EVERYTHING_KEEPS` lists the open ones (P:520, P:521). An open alert holds a code and ids, never content.
- Test: `test_open_alerts_survive_and_are_still_pushed` (P:525).
- This departs from §20.4 step 11's "alerts", so it is recorded in Deviation 24 and spec 3.7 item 21.

**Residuals left as §20.4 specifies them, not changed:**
- A `submit_started` or `submit_ambiguous` submission has no provider batch id to cancel. §20.4 maps it to `released`, with its entries `ambiguous` at their reservation.
- A lost submission released before the purge is already terminal in §20.4's table.
- A provider batch that exists for either is never adopted after the rows go, but the cap holds because every such reservation stays counted at its maximum until the month closes (§20.4).

## Other changes

None. The plan still has 55 Deviations.

## New decisions to flag

- **Finding 1:** delete-everything sends the recorded batch cancels itself, with 3 tries then a log, before step 3 deletes the submissions. Phase B re-sends them on restart. New fault point `privacy.delete_everything.after_cancel_recorded` (Deviation 24, spec 3.7 item 21).
- **Finding 2:** synthetic profile versions are never purge-eligible, unconditionally (Task 14, now matching Deviation 25).
- **Sweep:**
  - delete-everything runs `DeadmanPinger.ping_pending()` before deleting runs;
  - step 11 keeps open alerts, departing from §20.4 step 11 (Deviation 24, spec 3.7 item 21).
