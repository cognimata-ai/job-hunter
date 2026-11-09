# M5 plan, fix round 9: the plan author's response to Codex round 9

- **Review:** `docs/council/plan-reviews/m5-round9-codex.md` (REJECT; 3 MAJOR findings). This round follows the coordinator's rulings R9-1 to R9-3.
- **Plan:** `docs/superpowers/plans/2026-11-09-m5-operations-privacy-production.md`, edited in place on top of f5bf19a (HEAD 2eaf29a adds only the review). It now has 1,111 lines. Not committed.
- **References:** `P:n` is a line of the edited plan.

All three findings hold against the code at HEAD: 3 fixed, 0 rebutted. M5-U1–U4 and M5-R1–R8 stay binding.

The new decisions are a new Deviation 56 (external effects are live only) and updates to Deviations 16 and 24, spec 3.7 items 15 and 21 (P:928, P:934). The sweeps made 3 more fixes; there are 56 Deviations now.

## Finding 1 (R9-1): the purge's cancel stopped at a failed retrieval

What I verified: `cancel_requested` (`scoring/batch_results.py:199-219`) logs `batch_cancel_retrieve_failed` and returns `False` before its cancel loop. That is right for the poller, but delete-everything deletes the row next.

What changed:
- **A new bounded sender beside the poller's helper (P:527):** `scoring/batch_results.py` gains `PURGE_CANCEL_BACKOFF_S = (1.0, 5.0)` and `async def send_purge_cancel(client, provider_batch_id, *, submission_id, tries=CANCEL_TRIES, sleep) -> bool`.
  - Each try retrieves the batch, then cancels only while it is `in_progress`.
  - A `canceling` or ended batch counts as success, with no send.
  - A failed retrieval, a rejected cancel, or an unconfigured client (`LLMNotConfigured`, `llm/batches.py:347-350`) each count as one failed try.
  - After `CANCEL_TRIES` failed tries it logs `batch_cancel_gave_up` with both ids, and the deletion goes on (§20.4: "3 tries, then logged").
- **`cancel_requested` and its poller behaviour are unchanged.** Task 16's Files names the new function (P:520), and Consumes lists `CANCEL_TRIES` and the untouched `cancel_requested` (P:523).
- **Delete-everything's step 2 calls `send_purge_cancel`** (P:528).
- **Tests (P:536):**
  - a retrieval failing once and then recovering sends exactly one cancel;
  - retrievals failing on every try: no cancel, `batch_cancel_gave_up` logged, the submission deleted;
  - a `canceling` or ended batch counts as success without a send;
  - `cancel_requested` still returns at a failed retrieval;
  - the existing rejected-cancel case stays.
- Step 4 adds `test_batch_poll.py` (P:539).

## Finding 2 (R9-2): replay needed the worker's provider client

What changed:
- **External effects are live only.**
  - Task 12 adds `LiveEffects(batches_client, deadman)` and `PurgeRun.effects: LiveEffects | None`, set exactly when `mode == "live"` (P:415, P:416).
  - `run_purge` builds it from the worker's `OperationDeps`: `batches_client`, which already exists (`worker/operations.py:178`), and a new `OperationDeps.deadman` that carries `WorkerRuntime.deadman` (Files P:408).
- **Replay builds neither.** Task 25 builds `PurgeRun(mode="replay", effects=None)`, with no Batches client, no pinger, and no Anthropic or healthchecks secret, so the migrate, restore and backup roles are unchanged (P:762).
- **In replay, delete-everything:**
  - drops every recorded cancel with `batch_cancel_skipped_replay` logged (ids only) (P:528);
  - makes no dead-man ping (P:528).
- **The cost is recorded in Deviation 56 (P:1057).** It is at most the spend of a batch whose live cancel was never sent. That spend is already reserved or `ambiguous` in the kept ledger, and the provider expires the batch within 24 h.
- **Tests:**
  - `test_delete_everything_replay_makes_no_external_call`: a dump with a `submitted` batch, and fakes that fail the test on any call. Replay completes, the submission is gone, and no call is made (P:766).
  - `test_the_restore_test_replays_a_submitted_batch_without_worker_credentials`: the monthly restore test in the backup role completes (P:787).

**R9-2 sweep: is any other external call reachable from `execute` in replay? None found, so 0 fixes.**

| Kind | What `execute` reaches | External? |
|---|---|---|
| golden-case | the case and its cascade | No: the database only. |
| tombstone, source purge, profile purge | `purge_target_set`'s `record_cancel` | No: it only records; the worker's poller sends later, live. |
| tombstone, source purge, profile purge | `settle_for_purge`, `cancel_operation` | No: database writes. |
| tombstone, source purge, profile purge | `reapply_admission`'s `save_settings(internal=True, defer=None)` | No: it reads the eval gate from the database and defers nothing. |
| tombstone, source purge, profile purge | `rebuild_jobs` | No: re-embedding is the worker's later `awaiting_embedding` work. |
| tombstone, source purge, profile purge | export and CV file removal | No: local volumes. |
| delete-everything | `reset_to_defaults`, the ranking bump | No: database writes. |
| every kind | alerts | No: raised in the database; the dispatcher pushes them later, live. |
| every kind | the manifest | No: written to local files; the shipper and restic belong to the backup container and are never called from `execute`. |
| every kind | ntfy, healthchecks, S3, restic | Only delete-everything's ping pass reached healthchecks, and it is now live only. |

## Finding 3 (R9-3): post-completion drift re-ran delete-everything

What changed:
- **Task 9's general rule** (P:336, applied to `check_postconditions` at P:343): every postcondition must stay true under later normal activity, because §19.5 (L6268) replays a completed manifest whose postcondition fails.
  - A selector names only a property a row is born with and never loses: `all`, or `source:<id>` on `postings`, `fetch_requests` and `ad_keys` (its `key_ns`). It is evaluated only within the bound.
  - Any other membership is frozen at A2 as an `ids` list.
  - Kept rows are listed in a new `keep` id field (P:325).
  - A state set on a kept row is checked relative to the bounds.
- **Delete-everything:**
  - Step 11's alerts are the ids of the alerts resolved at A2. An alert open at A2 is kept even when it is resolved later (P:528). `DELETE_EVERYTHING_KEEPS` says "the alerts open at A2 (frozen as ids)" (P:526).
  - The `procrastinate_jobs` not in `doing` at A2 are frozen as ids. A job `doing` then is kept after it finishes (P:528).
  - The request's idempotency row is a `keep` id.
  - Step 8's postcondition is relative to the bounds (P:529): `active_config.settings_version_id > max_ids.settings_versions`; the profile, tuple and pending-scope pointers each NULL or above their bound; `schedule_state`'s version above the bound.
- **Tests (P:766):**
  - `test_a_completed_delete_everything_stays_satisfied_under_later_activity`, Codex's scenario: a new profile and settings, a kept alert resolved, a `doing` job finished. `migrate` then classifies the manifest `satisfied`, nothing re-executes, and the new configuration is unchanged.
  - `test_every_kinds_postconditions_survive_later_activity`, parametrized over all five kinds and every activity the ruling lists.
  - `test_a_job_doing_at_a2_is_kept_after_it_finishes` (P:536).
  - The alert test now covers an alert resolved between A2 and phase B (P:536).
- Deviations 16 and 24 (P:1017, P:1025) and spec items 15 and 21 record the rule.

**R9-3 sweep: every op of every kind checked against later normal activity, 3 fixes.**

| Kind | Op | Result |
|---|---|---|
| delete-everything | step 10's "scope versions only they used" | **Fixed (sweep 1, P:530).** The membership depended on references. It is now listed by id at A2. The list leaves out the rows `reset_to_defaults` will reuse by hash, because the shipped default's derived rows are computed at A2 too; otherwise step 8's reuse of the seeded default scope version would meet a deleted row. Test `test_the_reset_reuses_a_default_scope_version_that_step_10_never_lists` (P:536). |
| delete-everything | step 9's "unreferenced embeddings" | **Fixed (sweep 2, P:530).** It is now every embedding within the bound (`all`): when step 9 runs nothing kept references one, and a later reused text gets a new row above the bound. |
| delete-everything | every other step | `all` within the bound with fixed `keep` ids (its operation, the idempotency row, the default source instances). Sessions are bounded by `created_at ≤ fenced_at`, login attempts by `id` and lockouts by `generation`, all immutable. Exports follow the state table (content-free states have no way out). |
| source purge | the embeddings of tainted revisions | **Fixed (sweep 3, P:506, P:507).** The test "referenced only by them" would flip when an unrelated revision with the same text was deleted later. The embeddings are now frozen as ids at A2 and all deleted. A revision that reused one since A2 is first set back to `pending` and re-embedded into a new row above the bound. |
| source purge | postings, fetch requests, `adzuna` `ad_keys` | Stable: immutable selectors (source, `key_ns`), within the bound. |
| source purge | targets, det instances, golden cases, eval calls, revisions | Stable: ids. |
| source purge | `field_redact jobs current_revision_id` | Stable: a pointer cannot return to a deleted revision. |
| source purge | the kind's `purge_fence_at` and settings checks | Stable: `purge_fence_at` never decreases, and settings move only by new versions (`settings/save.py:158-162`), which are created later. |
| profile purge | the version's golden cases | Made explicit, no defect: frozen ids (P:485). They were already stable, since `set_flags` refuses a purged version. |
| profile purge | the redactions | Stable: every marker is terminal (`purged_at`, `status = 'purged'`, `content_purged_at`, `cv_files.purged_at`). |
| profile purge | profile vectors, file removal | Stable: vectors are selected by version id; files are removed by uuid name. |
| tombstone | its rows | Stable: job ids. Tombstones are written, and no postcondition asserts on them, so a later repost's upsert cannot break one. |
| golden-case | its row | Stable: the case id. |
| every kind | the fence op | Stable: "no active fence of the manifest", and only watermark-bounded checks for the affected jobs. Data collected after a re-enablement lies above the watermark. |

## Line count and sweep fixes

The plan now has 1,111 lines. The sweeps made 3 fixes: R9-2's found none, and R9-3's made 3.
