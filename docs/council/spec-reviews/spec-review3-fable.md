# Spec review, round 3 (targeted) — Claude Fable 5.1

Spec revision 3, commit adc1335; read as the full diff 7c06f2a→adc1335 plus the cited sections in place. Chair decisions C1–C3 (§2.4) are taken as binding and not re-argued.

**Verdict: APPROVE WITH CHANGES.** One must-fix MAJOR (N4, a contradiction introduced by the purge-fence work; trivial to repair) and four minors. No BLOCKER. My round-2 MAJORs N1–N3 and all six minors are resolved; the three writer deviations on my items are accepted; Codex's #2, N1, N2 and N10 are resolved.

## 1. My round-2 items

| Item | Status | Evidence |
|---|---|---|
| N1 mass-disappearance confirmation unreachable | **RESOLVED** | §8.11: structural completeness separated from closure classification; follow-up snapshots get `mass_disappearance` + `event_snapshot_id` (§7.7 DDL with `CHECK (event_snapshot_id IS NULL OR status = 'mass_disappearance')`); confirmation evaluated over `complete` or `mass_disappearance` snapshots against the opening snapshot's absent set; ordinary absence counting suppressed for the absent set while the event is open; worked example reproduces my scenario (confirmed at day-2 12:00). |
| N2 `logical_calls <= http_calls` CHECK | **RESOLVED** | §1.6 glossary redefines a logical call as one that reached a dispatched try; §10.2 `call()` no longer counts before the loop, `mark_dispatched(res, ctx, counts=(1 if first else 0, 1))` increments both counters in the dispatch-marker transaction; §9.4.2 states a pre-dispatch denial changes neither counter and ends `budget_exhausted → budget_deferred`; a unit test denies the budget before the first dispatch. |
| N3 purged profile version reused | **RESOLVED** | §7.4 partial unique index `profile_versions_live_hash … WHERE purged_at IS NULL`; §12.6 (a) reuses only a usable version, refuses 409 under a `profile` fence, never revives a purged row; §19.5 identity advancement guarantees a new id above every manifest's `max_ids`; test "Purged profile re-upload". |
| m-golden_cases RESTRICT vs tombstoning | **RESOLVED** | §20.2 job tombstoning step 4 deletes inactive cases (with results and calls) before revisions; §20.1 row; deletion-graph test seeds a job with an inactive case. |
| m-`apply_scope` permanent failure | **RESOLVED** | §13.4 step 5: clears `pending_scope_version_id`, keeps the old scope, `scope.apply_failed` (critical), `POST /api/settings/scope/retry-apply`; runbook row. |
| m-plaintext CV on `parse_jobs` | **RESOLVED** | §12.2 tmpfs, `finally` unlink, leftover cleanup at worker start; §19.1 `parse_jobs` tmpfs `uid=0,gid=10011,mode=0770`; runbook host step 5 (no plain swap). |
| m-deterministic seniority as T1 trigger | **RESOLVED** | §11.6 V4 split into repairable checks (seniority level replaced by the deterministic value, recorded as a correction, no trigger) and consistency failures (T1). |
| m-`unspecified` CEFR → T5 on most German ads | **RESOLVED** | §11.4 German-phrase mapping table in the generator prompt; `mandatory_language` with `unspecified` level and profile ≥ B2 is `met`; §21.1 notes the net trigger effect is measured in week 1. |
| m-snapshot status for follow-ups | **RESOLVED** | See N1; writer chose `event_snapshot_id` instead of a new status. |

**Writer deviations on my items**

- *German mapping uses the lowest plausible level* — **ACCEPT.** Lowest-level mapping can only turn `uncomparable` into `met`, never create a false `unmet`, which is the right direction for a knockout. One row is not the lowest plausible level: "fließend / fluent" is routinely used for B2 candidates; map it to `B2` and keep `verhandlungssicher / business fluent` at `C1`. Non-blocking.
- *No new snapshot status; `mass_disappearance` + `event_snapshot_id`* — **ACCEPT.** Equivalent to what I asked for, and the CHECK ties the pointer to the status. One wording leftover: the §17.4 `ats_mass_events` row still says "two later complete snapshots"; add "structurally" to match §8.11.
- *tmpfs mode 0770 with group 10011* — **ACCEPT**, with one implementation note: the mount options set the mode and group of the tmpfs root only. A per-job directory the worker creates inherits the worker's primary group (10001) unless the root has the setgid bit (`mode=2770`) or the worker `chgrp`s it to 10011 and sets `0770`; otherwise `docparse` (10002, supplementary 10011) cannot enter it. Specify `mode=2770` or the explicit chgrp.

## 2. Spot-check of Codex round-2 blockers

| Codex | Status | Evidence |
|---|---|---|
| #2 reservation bound (C1) | Resolved under C1 | §10.7 byte bound over every rendered component + `PROMPT_OVERHEAD_TOKENS` = 2 × measured framing overhead (floor 1,000 until V19); the single stated assumption (≤ 1 token per byte) is structurally true of any tokenizer whose tokens cover ≥ 1 character and is verified by V19 on umlaut/`ß` fixtures; `overrun_buffer_usd` ($2, never admitted, validated against concurrency × overhead × price) in §6.9.1, `llm_budget` DDL, S3, invariant 6; native PDF reserves 1.25 × `count_tokens` + buffer share or is disabled. Consistent throughout (§6.9.2 rate on `A`, §21.3 $93.10). |
| N1 operation ABA | Resolved | §3.3.3/§7.3 fresh `claim_id` per execution, predicate `claim_id = :claim AND status = 'running'` on every reservation, dispatch marker, heartbeat and domain write; reclaim/takeover clear it in the same statement; `CHECK` ties `running` to a non-null claim; tests "Operation ABA" and C35. |
| N2 det-instance replacement | Resolved | §7.11: lock `llm_results` `FOR UPDATE`, clear the old current row first, then insert; failure rolls back to the old instance; test "Score-instance replacement". |
| N10 secrets unreadable | Resolved | §19.2 files `root:10010` mode 0440, directory 0750; `x-app` `group_add: ["10010"]`, postgres `user: "999:999"` + `group_add`; backup/restore read as container root; startup assertion 1 checks readability and world-readability; runbook host setup; V22. |

## 3. New defects in the revision-3 diff

**N4 — MAJOR. §8.6 step 3 contradicts S1 / §17.2 I1 / §7.16.1.** Step 3 says that when a job in `P` or `J` is under a purge fence, "the item waits: it stays `pending` and is retried by the next run". But §7.16.1 and §17.2 I1 require `count(pending) = 0` at finalize, and S1 turns a violation into `failed_invariant` plus an alert. Scenario: `retention_daily` (03:17) tombstones a batch of jobs under a `jobs` fence while the 03:00 run's `resolve_identity` processes a re-posted ad whose keys resolve to one of those jobs; the item stays `pending`; `finalize` fails I1, the run ends `failed_invariant` and a critical alert fires for a situation the spec itself created. There is also no mechanism for "the next run" to pick up another run's `pending` item (node 6 and the resume rule are per run). Fix (either): (a) do not defer at all — the consumer already takes the job rows `FOR SHARE`, so it simply blocks until the purge chunk's `FOR UPDATE` commits, then re-reads `P`/`J` (a tombstoned job is gone, so the item becomes a new job with the tombstone check; a fenced-but-not-yet-deleted job is treated as absent for this item); or (b) add the disposition `deferred('job_fenced')` to §7.16.1, exclude it from I1, and have the next run's node 6 include deferred items of earlier runs. (a) is simpler and needs no new state.

**Minors**

- **§9.6.2 lane slots can exceed `S`.** `slots = floor(credit)` per lane, with credits capped at `share × S + 1` and unused slots costing nothing. After two or more cycles in which no lane has candidates (every pending target has `not_before` in the future after an API outage, which still satisfies the trigger "pending targets exist"), credits sit at their caps and `sum(slots) = 13` for `S = 10`; `spare` is −3, the remainder loop checks only `spare == 0`, so each lane gets one more slot and 16 targets are admitted against an admission size of 10 (`S + 6` in the worst case). The hard cap and the bucket's negative-balance repayment still hold, so the consequence is bounded. Fix: break on `spare <= 0` and, when `sum(slots) > S`, trim the lanes with the smallest remainders; add "cycles with no candidates" to the admission property test, which currently assumes a permanent backlog in every lane.
- **§7.1 ranking generation bumps on `seen_at`.** Opening any job now increments the generation, so a user who reads a job and returns to the list gets the "Ranking changed — refresh" banner on every visit. Bump on `seen_at` only when the active filter reads it (an unseen-only filter), or suppress the banner for self-caused changes.
- **§11.4 mapping row** `fließend → C1` (see §1 above); **§17.4 wording** "complete" → "structurally complete".
- **§19.5 backup step 1** leaves a `backup_sets(status='running')` row inside the dump; after a restore it should be marked `failed('restored_mid_backup')` by `restore verify --phase restored` rather than left `running`.

## 4. Verdict

**APPROVE WITH CHANGES.** Must fix before implementation: **N4** (fenced raw items vs S1/I1). Recommended during implementation: the lane-slot cap, the tmpfs setgid/chgrp note, `fließend → B2`, the `seen_at` bump, and the two wording items. Nothing in the revision-3 diff weakens the cap, the quota ledger, the identity rules or the deletion guarantees; C1–C3 are integrated consistently across S3, §6.9, §7.16.6–7.16.7, §10.7, §10.10, §17.4, §18 and §21.
