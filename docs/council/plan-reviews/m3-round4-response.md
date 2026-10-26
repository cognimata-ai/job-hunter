# M3 plan — response to Codex review round 4

Plan: `docs/superpowers/plans/2026-10-25-m3-agentic-scoring.md` (working tree on d2aa369). Review: `docs/council/plan-reviews/m3-round4-codex.md`. P: lines are the revised plan's. Every finding was verified against the code and the spec before the fix; none is rebutted.

**Simplifications taken under the round-4 ruling** (each recorded as a Deviation where it changes behaviour):

- (a) Eval runs are scored by one execution and never resumed: an execution that finds the run already touched (a call row or a result row exists) ends it `running → failed` with `metrics.failed = "interrupted"` and succeeds its operation; the `eval_run` failure hook does the same; the user starts a new run (≈ one run's cost). The durable step store, the `in_progress` outcome, the step keys, `eval_receipt` and the whole replay are withdrawn (Deviation 29, Task 1, Task 19, Task 25 items 9 and 21).
- (b) The parser family is the explicit column `prompt_schema_versions.parser_family text NOT NULL DEFAULT 'generator_v1' CHECK (… IN ('generator_v1','generator_v2_items'))`, written by the seed (the only writer of version rows in M3) and pinned by `test_parser_family_is_the_stored_constant`; the structural canonicaliser (`generator_family`, `structural`) is withdrawn. A stored schema that does not fit its family is caught by the gateway's mirror validation of every output and by the live dry-run before activation, not by inspecting the schema (Deviation 40, Tasks 1, 4, 5, 9, 25).
- (c) Takeover: the **one-transaction-per-sweep** option. The epoch claim commits alone; each sweep then runs in a transaction of its own, in registration order, opened with `fence_epoch` and `active_config FOR SHARE`, its rows in schedule order; a failing sweep ends the takeover as today and the next start sweeps again under the next epoch. The central acquisition in the shared transaction was rejected on verification: the month row is step 6 and the operation/submission owners are step 5, so holding the month before the `operations` sweep would invert against `prepare` (operation row → month row) and every attempt's `fail`/`reserve` (target → month); and one transaction over five sweeps would hold one sweep's step-10 rows while the next takes its step-5 rows (Global Constraints, Tasks 11, 16, 27, Task 25 items 25–26, Deviations 35, 38).

## A. Partially resolved round-3 items

| Item | Severity (r3) | Disposition | New P: lines | Note |
|---|---|---|---|---|
| A3 | MAJOR | fixed | 159, 308, 1234, 1358, 1396 | Closed by ruling (b): the family is a stored constant; the structural comparison is gone. |
| A18 | MAJOR | fixed | 86, 626, 868, 1250–1251, 1284, 1290, 1294, 1391, 1394 | Closed by B1's fix: one fenced transaction per sweep, `active_config` before any operation row. |
| A30 | MAJOR | fixed | 517, 994, 1004, 1006, 1246, 1358, 1385 | Closed by ruling (a): no replay, an interrupted run is failed and re-run. |
| A(r2 B3) | MAJOR | fixed | 308, 1396 | `WireSchema` kept; the canonicaliser replaced by the stored family. |
| A(r2 B7) | MAJOR | fixed | 86, 1250–1251, 1394 | The shared-transaction inversion is gone: each sweep is its own transaction. |
| A(r2 B18) | MAJOR | fixed | 565 | The eval runner is removed from `publish_result`'s caller list (it calls `publication.build` only). |
| A(r2 B19) | MAJOR | fixed | 1004, 1006, 1385 | Receipt states no longer exist to mishandle: no resume. |
| A(r3 B2) | MAJOR | fixed | 86, 868, 1383 | Deviation 27 now names the `batch_submissions` tick reconciler, never a failure hook; the takeover path holds `active_config` first in every sweep transaction. |
| A(r3 B5) | MAJOR | fixed | 1004, 1006 | Exhausted-repair routing on replay is moot: the one execution routes through the production `routing.route` on outcomes it received. |
| A(r3 B11) | MAJOR | fixed | 308, 1396 | Metadata-key collisions cannot bypass anything: nothing inspects the schema. |
| A(r3 B15) | MAJOR | fixed | 159, 639, 664, 1234, 1358 | `always_include` admitted; both CHECKs rendered from `admission.LANE_VALUES`; a DB claim test with an always-include admission. |
| A(r3 B17) | MAJOR | fixed | 565 | Same edit as A(r2 B18). |

## B. New findings

| # | Severity | Disposition | New P: lines | Note |
|---|---|---|---|---|
| B1 | BLOCKER | fixed | 86, 626, 868, 1250–1251, 1284, 1290, 1294, 1391, 1394 | Option (i) of the ruling: epoch claim commits alone; every sweep in its own transaction opened with `fence_epoch` + `active_config FOR SHARE`; `worker/epoch.py` split in Task 27; `test_each_takeover_sweep_is_its_own_fenced_transaction` (both registration orders, an old handler holding its operation row, an old attempt holding a target, a queued configuration writer, `lock_waits`, no deadlock or timeout) and `test_a_failing_sweep_ends_the_takeover_and_the_next_start_sweeps_again`. |
| B2 | MAJOR | fixed | 159, 639, 664, 1234, 1358 | `score_attempts.lane` CHECK = the seven values of L2219–2222 incl. `always_include`; both CHECKs rendered from `admission.LANE_VALUES` and pinned by `test_lane_values_pin_both_checks` (`pg_get_constraintdef`); `test_an_always_include_admission_is_claimed_and_written_to_the_attempt`. |
| B3 | MAJOR | fixed | 159, 308, 357, 380, 383, 526, 527, 1234, 1358, 1373, 1392, 1396 | Ruling (b): `parser_family` column, `PromptVersion.parser_family`, `parse_generator(wire, *, family)`, seed-written and test-pinned; `generator_family`/`structural`/`generator_kind` withdrawn everywhere. |
| B4 | MAJOR | fixed | 1004, 1006, 1246, 1385 | Ruling (a): no recovery path exists to turn a contested case into a failure; routing is the production route on live outcomes. |
| B5 | MAJOR | fixed | 1004, 1006, 1246, 1385 | Ruling (a): no receipt states ("no receipt", `dispatched`) to recover; the `Receipt` of Task 7 is untouched (`max_tokens` for `last_call_of_attempt` only). |
| B6 | MAJOR | fixed | 1004, 1006, 1246, 1385 | Ruling (a): no error receipt is ever re-run; an interrupted run is failed. |
| B7 | MAJOR | fixed | 1004, 1006, 1246, 1385 | Ruling (a): no stage or logical-call reconstruction; the execution's own gateway calls carry the stage, and the eighth logical call cannot arise. |
| B8 | MAJOR | fixed | 86, 614, 619, 620, 647, 703, 710, 711, 755, 775, 865 | `ensure_month` before `lock_month` at `start_cycle`, `tick_score`, `budget_release`, the batch prepare and `bulk.open` (schedule step 6 says so); `read_month_or_new` for the score-now pre-check; a release locks the reservation's own month, which exists; tests `test_the_first_cycle_of_a_month_creates_its_row`, `test_score_now_on_a_fresh_month_needs_no_row`, `test_budget_release_on_a_fresh_month_creates_its_row`. |
| B9 | MAJOR | fixed | 526, 864, 912 | One predicate `targets.RESUMABLE_DRAFT_SQL` (a `score_drafts` row on any attempt of the target under the active policy) is what `load_context` resumes and what `ELIGIBLE_SQL` negates; `test_a_target_with_an_online_draft_is_resumed_not_batched`. |
| B10 | MAJOR | fixed | 1021, 1044, 1246, 1385 | Three declared observation sets: primary = repetition 1 per active case for every gate metric (`calibration.primary`), paired = the test–retest sample for `test_retest_sd`, all rows for `mean_cost_usd`; `test_retests_cannot_dilute_a_primary_gate` (5/30 contested stays 16.67 % with ten accepted retests); L4613 amended. |
| B11 | MAJOR | fixed | 1144, 1147, 1247, 1387 | `batches.UNRESOLVED_SUBMISSION_SQL` (`preparing | submit_started | submit_ambiguous | submitted | ended`, or `released` < 29 d) exempts targets and their items and calls from `score_history` until the submission is resolved; no age releases anything (C3); L6304 amended; `test_an_unresolved_submission_keeps_its_targets_through_retention` runs the release and the next pass. |
| B12 | MINOR | fixed | 72 | Global Constraints now state `min(slots["similarity"], S // 2)`, the settled floor. |

**Counts:** 24 fixed (12 partials, 12 new), 0 rebutted. Plan: 1,433 → 1,445 lines. Only the plan is modified in the working tree.
