# M5 plan, fix round 7: the plan author's response to Codex round 7

- **Review:** `docs/council/plan-reviews/m5-round7-codex.md` (REJECT; it resolves both round-6 findings and raises 2 new MAJOR findings).
- **Plan:** `docs/superpowers/plans/2026-11-09-m5-operations-privacy-production.md`, edited in place on top of c067f1f (HEAD 40a48f0 adds only the review). Not committed.
- **References:** `P:n` is a line of the edited plan.

Both findings hold against the code at HEAD: 2 fixed, 0 rebutted. M5-U1–U4 and M5-R1–R8 stay binding. The one new decision is folded into Deviation 25. The final fixture sweep found 2 more fixtures that the real schema would reject, both fixed, and added one general rule.

## Findings

**1. MAJOR — golden-case activation could change profile-purge eligibility after admission. Fixed with one rule over every writer of eligibility.**

What I verified:
- `set_flags` (`scoring/golden.py:494-516`, called by `api/routes/golden.py:215`) takes no `active_config` lock.
- Its only fence check is Task 8's job-fence refusal: it locks the job row and calls `is_fenced`. A profile purge's A0 locks the profile's rows, not the job's, and its fence is not committed while A0 runs.
- So an activation could pass its check and commit between A0's eligibility read and A0's commit. A2 would then leave the newly active case out while the version's content was redacted.

The rule (P:468): A0 reads eligibility under `active_config FOR UPDATE`, and every writer that can make a version ineligible holds `active_config`. So no eligibility condition can change between A0's read and A2. I checked each condition's writers:

| Eligibility condition | Its writers | Result |
|---|---|---|
| active or previous | the confirm and activation | Already safe: they hold `FOR UPDATE`. |
| a non-terminal or recent target of its tuples | the score lane and the user's target moves | Already safe: they take `active_config FOR SHARE` first (`scoring/targets.py:71`) and serve the active tuple only. |
| feedback, or a new or upserted golden case | `record_feedback`; the adversarial seed | Already safe: `record_feedback` takes `FOR SHARE` first and works on the active profile only (`scoring/golden.py:280-286`); the seed takes `FOR UPDATE` (`:778`). |
| an active golden case | `set_flags` | **The gap, fixed** (P:472). It now takes `active_config FOR SHARE` as its first statement, before the canary lock and the job row. It then refuses with 409 `conflict_state` (`purge_in_progress`) to activate a case whose profile version is purged or under an unreleased `profile` or `global` fence. |
| (deleting a case or feedback) | — | Needs no lock: it only makes a version more eligible. |

How both orders now end:
- **Activation first:** A0's eligibility read sees the active case, so no purge opens.
- **Admission first:** the activation waits for A0 and then meets the fence, or after completion the version's `purged_at`.

`test_case_activation_and_admission_serialise` covers both orders through the route, and also the refusal after completion (P:483). Elsewhere in the plan:
- Files (P:462) and Step 4 (P:486; the golden suites);
- Task 8's `set_flags` bullet points to Task 14 (P:296);
- Deviation 25 records the rule (P:1015).

**2. MAJOR — the shared privacy seed required three live submissions at once. Fixed.**
- **Verified:** `batch_one_in_flight` (`0008_m3_scoring.py:469-470`) allows one submission in `preparing`, `submit_started`, `submit_ambiguous`, `submitted` or `ended` with no `released_at`. Its CHECK allows `released_at` only in `released`, `ended` or `reconciled` (`:466-467`).
- **The seed takes the live state as a parameter:** `seed_two_year_old_graph(conn, *, now, live_submission: InFlight = "submitted")` (P:446).
  - The `in_batch` targets belong to that one live submission.
  - Every other submission is terminal: `reconciled`, `abandoned`, and `released` with `released_at`.
  - Every row is inserted directly in its state, with every CHECK, index and trigger enabled.
- **Tests:**
  - Task 16's `test_the_batch_mapping_is_exact` is parametrized over the five in-flight states, each on its own seeded database (P:525);
  - the new `test_the_seed_holds_for_every_live_submission_state` commits the seed for each state (P:452).
- The seed's default (`submitted`) serves Tasks 14, 15 and 25 unchanged.

## Sweep: every fixture and seed against the schema

The checks I ran:
- I read every CHECK, UNIQUE and partial-unique index in the migrations.
- I read every fixture the plan describes against them: the seed, each Step 1 fixture, the smoke's inserts and the stack scenarios.
- Seed rows are inserted directly in their state, and the `assert_transition` trigger fires only on `UPDATE OF status`. So the matrix matters only where a fixture moves a row afterwards, and those moves go through `transition()`.

| Fixture | Result |
|---|---|
| The privacy seed (Task 13) | **Finding 2.** The seed bullet now states the rule every M5 fixture follows (P:446-450): the singletons and partial-unique indexes M5 meets; the keys that must be distinct; the per-row CHECKs a fixture is likely to trip; and only the matrix's edges for later moves. |
| Task 16's batch-mapping test | **Finding 2.** |
| Task 15's A0 test, `…_cancels_runs` | **Fixed (sweep 1).** It invited a `queued` and a `running` collect run together, which `runs_one_active_collect` forbids. It is now `…_cancels_the_active_run`, parametrized over `queued`, then `running` (P:504). |
| The smoke's `deadman` check (Task 33) | **Fixed (sweep 2).** It inserted "a finished `schedule` run" with no slot, which the CHECK `(trigger = 'manual') = (scheduled_slot IS NULL)` refuses. A reused slot would hit `runs_slot_uq`. It now names a free past slot, `started_at` and `finished_at`, the active versions, and `succeeded` (P:946). |
| The restore-state sets (round 6) | Valid: two sets, one `running` export each. |
| Task 17's export tests | Valid: one export at a time; failed and expired exports are reached through their edges. |
| Task 14's proposals (`ready`, `failed`, `running`) | Valid: no unique index on running proposals; a `ready` one has `items`. |
| Tasks 11 and 15's batch tests | Valid: one live submission each, and a released one has `released_at`, outside the singleton. |
| Task 5's deadman runs | Valid: a `manual` run has no slot, a `skipped_overlap` run has `attempt_no = 0` and no `started_at`, and slots are distinct. |
| Task 4's ten warn and two critical alerts | Valid as long as their dedup keys are distinct (`alerts_open_dedup`); the seed rule now says so. |
| Task 28's ciphertexts (a CV, a raw text, the TOTP secret, an export) | Valid: one user, one export. |
| The stack scenarios (C32, C37, C39) | Valid: they use the real application, not hand-written rows. |
| `tests/support/fences.py` (Task 1) | Valid: each fence gets a manifest of the matching kind, and the existing CHECK cases keep their intended violation. |

**General rule:** a new Global Constraint, **Fixtures** (P:50). Every fixture and seed runs on the real schema with its constraints and triggers enabled and moves rows only along the matrix's edges. A scenario that needs two rows a singleton forbids is parametrized over separate databases.

## Other changes

None. The plan still has 55 Deviations.

## New decisions to flag

- **Finding 1:** every writer of a profile version's purge eligibility holds `active_config`. `set_flags` now takes it `FOR SHARE` first and refuses activating a case of a purged or fenced profile (Deviation 25).
- **Finding 2 and the sweep:** the seed's `live_submission` parameter, and the **Fixtures** Global Constraint. These are test rules, not spec changes.
