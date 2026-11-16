# M5 plan, fix round 6: the plan author's response to Codex round 6

- **Review:** `docs/council/plan-reviews/m5-round6-codex.md` (REJECT; 2 MAJOR findings, and round-5 finding 1 only partly resolved).
- **Plan:** `docs/superpowers/plans/2026-11-09-m5-operations-privacy-production.md`, edited in place on top of ca16cd1 (HEAD f03b8de adds only the review). Not committed.
- **References:** `P:n` is a line of the edited plan.

Both findings hold against the code at HEAD: 2 fixed, 0 rebutted.

M5-U1–U4 and M5-R1–R8 stay binding. The new rule for finding 1 is folded into Deviation 54. The sweep of every creator of an operation that reads a profile version found no further race, so it made 0 fixes.

## Findings

**1. MAJOR — a racing proposal request could reopen the closed call set. Fixed at its root.**

What I verified:
- `request_query_proposal` reads the active version with `_ACTIVE_VERSION` and takes no lock (`api/routes/profile.py:146, 457-461`).
- `request_proposal` then locks only the version row and checks that it exists. For a `running` proposal without a live operation, it creates a new one (`queries/proposals.py:327-345`).
- A0's cancel ran before A0 locked the version, so a request that created an operation just before A0 committed escaped the cancel, and one that came after A0 could still create one.

The rule I chose, the smallest that makes "an operation for a purgeable version created after A0" impossible: every proposal operation is created under `active_config` (P:464).
- `request_proposal`'s callers must hold `active_config`, `FOR SHARE` at least (the schedule's step 2), from before they read which version to propose for. They must pass the active version or the one their transaction activates.
- The confirm and activation already hold it `FOR UPDATE` (`profile/activation.py:6-8`).
- The route now takes `active_config FOR SHARE` before `run_idempotent`, which is step 2 before the idempotency row and the domain rows, as `run_idempotent`'s contract asks (Files P:457).

Why it holds:
- A0 runs under `active_config FOR UPDATE`, and the eligibility is now evaluated in A0's own transaction, after that lock (P:463).
- A route holding `FOR SHARE` either finishes first, and then A0's cancel, which runs after A0's lock, sees its operation and cancels it; or it waits for A0, and then reads an active version that cannot be the fenced one, because activating a fenced version answers 409 (P:465).
- While the route holds `FOR SHARE`, no activation can move the active version (P:466).
- As a guard for a future caller that forgets the rule, `request_proposal` also raises `InvariantViolation` for a purged or fenced version, read after its version lock (P:467). The rule makes it unreachable today.
- The admission bullet's claim now rests on this rule (P:468). Consumes names `request_proposal` and its callers (P:460). Deviation 54 records the rule (P:1034).

The race test, `test_a_paused_proposal_request_cannot_reopen_the_call_set` (P:473), runs through the route and its real middleware. The request holds `active_config FOR SHARE`, has read version V, and is paused at the new test-only fault point `profile.proposal.before_request`. Both orders are covered:
- the request finishes first, the activations follow, and A0 cancels V's operation;
- A0 finishes first, and the request then proposes for the new active version.

Either way, no operation of V is live after A0, no call of V is reserved after A2, and a response settled after completion stores nothing.

`test_request_proposal_refuses_a_fenced_or_purged_version` pins the guard (P:473). Step 4 adds the route suites `test_queries_api.py` and `test_api_profile_m4.py` (P:476).

**2. MAJOR — the restore-state test required an impossible backup. Fixed.**
- **Verified:** `UNIQUE exports_one_running ((true)) WHERE status = 'running'` (Task 1, P:120) allows one `running` export per database. The test asked for two in one set.
- **The test moves** to a new integration file, `tests/integration/test_restore_exports.py` (Files P:754). It runs on the test server's real schema, unique index included: each set is a real `pg_dump` of a seeded database, restored with `dump_and_restore` and run through `run_restore`'s steps 7, 7b and 8 (P:766).
- **`test_restored_exports_follow_the_state_table` is parametrized over two valid sets:**
  - (a) the `running` export is named by a newer manifest, and a `ready` one is named by none;
  - (b) the `running` export is named by none, and a `ready` one is named.
- **Together they cover all four cases.** The named one ends `purged`; the unnamed `ready` one ends `expired`; the unnamed `running` one ends `failed`. In each, the postconditions hold and the directory is emptied.
- **`test_the_unique_index_survives_the_restore`:** after either restore no export is `running`, a new export is accepted, and a second answers 409 `export_running`.
- The unit file `test_restore_run.py` no longer carries the case (P:766), and Steps 2 and 4 run the new file (P:769).

## Sweep: every operation creator that reads a profile version

| Creator | What it reads | Result |
|---|---|---|
| `propose_queries`, from the route | the active version | **Finding 1, fixed.** |
| `propose_queries`, from the confirm and activation | the version they confirm or activate | Safe: they hold `active_config FOR UPDATE` (`profile/activation.py:6-8`) and pass that version. |
| `embed_profile`, from the confirm and activation | the version being confirmed or activated | Safe: they hold `active_config FOR UPDATE`. |
| `embed_profile`, from the `profile_vectors` reconciler | the usable active version, in one transaction; the operation is created in a second one (`profile/vectors.py:218-246`) | Safe: an operation created for a version purged or fenced meanwhile writes nothing. Its handler re-reads the version under its row lock (`FOR SHARE`) and skips it (`profile/vectors.py:20-31`). It makes no LLM call, so there is no call set to escape. |
| `extract_profile` (upload, paste, text edit) | a draft, never a version | Safe: `request_draft_operation` runs only for a new draft (an upload or paste, `profile/upload.py:510`) or for a draft in `text_review` or `ready` (`api/routes/profile_review.py:670-701`). A purgeable version's drafts are all `confirmed`, a state no edge leaves. |
| `eval_run` | the golden cases of versions | Safe: the eval checks the profile fence under the version's row lock and skips a fenced version's cases (M3 ledger L535; Task 14's `test_the_eval_skips_cases_of_the_fenced_profile`). The purgeable version's inactive cases are deleted with their calls (round 3's settlement covers them), and an active case makes the version ineligible. |
| `create_targets_for_tuple` and `missing_targets` | the active tuple | Safe: they serve only the active tuple, and a purgeable version's tuples are never active. |

## Other changes

None. The plan still has 55 Deviations.

## New decisions to flag

- **Finding 1:**
  - every proposal operation is created under `active_config`; the route takes it `FOR SHARE` before reading the active version;
  - the profile purge's eligibility is evaluated in A0's own transaction;
  - `request_proposal` raises `InvariantViolation` for a purged or fenced version;
  - the test fault point `profile.proposal.before_request` (Deviation 54).
- **Finding 2:** the export-state restore test lives in a new integration file with two parametrized real sets (no new decision).
