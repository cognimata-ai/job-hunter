# Spec review, round 2 — Claude Fable 5.1

Spec: `docs/superpowers/specs/2026-10-16-job-hunter-design.md`, revision 2 (commit 7c06f2a). Authority: consensus-v3 incl. §R (cost decision). Read: full added-lines diff dea0ffb→7c06f2a plus the cited sections in place.

**Verdict: APPROVE WITH CHANGES.** No BLOCKER. Three new MAJORs (N1–N3) in changed material; all 25 of my round-1 findings are resolved; Codex's nine BLOCKERs are resolved; the five "not fully adopted" decisions are all ACCEPTED; the §21 arithmetic is correct.

---

## 1. My round-1 findings

| # | Round-1 finding | Status | Evidence in revision 2 |
|---|---|---|---|
| B1 | LLM pacing not implementable (per-run cap vs monthly cap) | **RESOLVED** | §6.9.2 time-based USD bucket: rate = cap×0.95/month_s, capacity max(2×rate×interval, $1); §3.4 step 6 and §9.3 cycle trigger when bucket ≥ min(`min_cycle_targets`×ĉ, B_max); §6.9.4 cap-reached behaviour; §6.9.3 bulk operations funded from headroom (40%). |
| M1 | `unknown` postings could never close; `absent_snapshots` CHECK violated | **RESOLVED** | §7.16.2 `unknown → closed('ats_absent')` on `absent_snapshots ≥ 2 ∧ ≥ 24 h`; §8.11 saturating `LEAST(absent_snapshots + 1, 2)`, idempotent per `last_absent_snapshot_id`; `unknown` entry rule (only incomplete/mass snapshots). |
| M2 | Merge/tombstone dropped user state | **RESOLVED** | §8.6.1 step 1 snapshot into `merge_events.evidence`, step 3 coalesce `seen/saved/applied/hidden_at` (earliest) and notes; §8.6.2 unmerge restores from the snapshot; §8.6 step 5 tombstones consulted on re-sighting. |
| M3 | `llm_call_count ≤ 6` CHECK contradicted the routing (7 logical calls) | **RESOLVED** (but see N2) | §7.11 `logical_calls BETWEEN 0 AND 7`, `http_calls BETWEEN 0 AND 42`; §9.4.2 bounds paragraph; §1.6 glossary separates logical call from HTTP try. |
| M4 | Login throttle keyed on the proxy's IP | **RESOLVED** | §16.3: Caddy fixed at 172.30.0.2, uvicorn `--proxy-headers --forwarded-allow-ips=172.30.0.2` (§19.1 compose), test N2 (§18.8). |
| M5 | Purge order violated FKs (`llm_tuples`, ledgers) | **RESOLVED** | §20.2 deletion graph; §20.4 delete-everything order; profile purge keeps the row and nulls content (§20.1 table, §20.2 step 4); ledger rows survive (§20.2 target-set step 7). |
| M6 | §21 critic rate 35% too low | **RESOLVED** | §21 funnel: central first-round critic rate 0.45 (range 0.35–0.58), 0.655 critic calls and 0.205 revisions per target; sensitivity table. |
| m1 | Takeover deviation from consensus B3 | **RESOLVED** | §2.2 R1 records the deviation; §3.3.1 takeover keyed on `worker_heartbeat` > 60 s, healthy holder never killed. |
| m2 | Ambiguous batch settled on a timeout | **RESOLVED** | §2.2 R9; §17.4 `ambiguous_batches` "never settles by timeout", alert at 25 h; §10.10 positive-evidence reconciliation. |
| m3 | Per-run paid cap meaningless at long intervals | **RESOLVED** | §6.4 `paid_max_requests_per_run = auto = max(10, ceil(2 × fair_share))`; §13 settings row. |
| m4 | Stale TTL 21 d expired targets of still-live aggregator ads | **RESOLVED** | §8.11 aggregator `stale` = "not re-confirmed", default `max_ad_age_days`, targets never expired by it; §17.4 `stale_sweep`; settings validation `stale_ttl_days ≥ max_ad_age_days` (§6.8 dry-run code). |
| m5 | Revision A→B→A violated the uniqueness | **RESOLVED** | §7.9 `UNIQUE (job_id, material_hash)` "an identical projection reuses the revision"; §8.8 lineage via `job_revision_sources`. |
| m6 | `in_batch` column resets / cancelled transition | **RESOLVED** | §7.11 `CHECK ((state = 'in_batch') = (batch_submission_id IS NOT NULL))`; §7.16.3 transition table with column effects; §9.4.3 and §7.17 invariant 3 exempt `in_batch` from reclaim. |
| m7 | Scope flip race with concurrent publishes | **RESOLVED** | §13.4 all writers coordinate on `active_config` (FOR UPDATE for flips, FOR SHARE for publishes, §9.4.1); publishes during precompute write `det_results` for both scopes; final transaction re-checks completeness. |
| m8 | `golden_cases` second label undefined | **RESOLVED** | §7.13 `UNIQUE (job_revision_id, profile_version_id)` with ON CONFLICT DO UPDATE; §15.6 upsert. |
| m9 | `remote_region` had no `other` value | **RESOLVED** | §11.4 enum `… / other_country / unspecified` with `remote_countries[]`; V4 consistency rules on `other_country`/`countries`. |
| m10 | Unverified pins (`httpx2`, versions) | **RESOLVED** (ACCEPT, §4) | §4.1 † marks on every unverified pin, re-confirmed at `uv lock`; `anthropic 1.x → httpx2` stated with its source; `httpx 0.28.1` for the adapters, coexistence noted. |
| m11 | No fetch-request machine; gap rows not unique | **RESOLVED** | §7.16.5 fetch-request machine (planned/reserved/dispatched/succeeded/failed/ambiguous/skipped, `attempt_seq ≤ 8`); §5.6 gap rows `UNIQUE (cell_id, kind, span)`. |
| m12 | `skipped(shutdown)` specs lost | **RESOLVED** | §3.3.5 step 2 and §5.6 "re-planning on a new run attempt": `planned` and `skipped(shutdown)` specs are planned again. |
| m13 | Attempt budget consumed by budget denials | **RESOLVED** | §7.11 `charged_attempts`; §7.16.3 "an attempt is charged when it dispatches its first LLM call or its batch item is submitted; denied reservations do not count"; `BudgetExhausted → budget_deferred` (§10.4). |
| m14 | Cap tolerance made the cap soft | **RESOLVED** (stricter) | §6.9.1 "reservations are true upper bounds"; §10.7 bound = counted prefix ×1.1 + UTF-8 bytes + overhead; a violation is a defect (critical alert, lane pause); §7.17 invariant 6 "no tolerance". |
| m15 | Default privileges / CREATEDB on the app role | **RESOLVED** | §7.1 role table: `ALTER DEFAULT PRIVILEGES FOR ROLE jobhunter_migrator …` for `jobhunter_app`; `CREATEDB` only on `jobhunter_restore` (§19.3 bootstrap SQL). |
| m16 | Fatal V1 drafts discarded though repairable | **RESOLVED** | §11.6 two-stage validation: only the structural parse is fatal; V1 limit issues and V2–V6 are semantic and route to the critic (T1) or publication repairs. |
| m17 | Dangling §8.6 → §8.7 reference | **RESOLVED** | §8.6 / §8.7 renumbered and cross-referenced (identity rules → probable-duplicate groups). |
| m18 | "Revoke claims" wording | **RESOLVED** | §12.6 step 2(d): `in_progress → cancelled('tuple_superseded')` clears the claim, coroutines stop at the next claim check, attempts end `cancelled`. |

## 2. Spot-check of Codex's nine BLOCKERs

| Codex # | Claim | Status | Evidence |
|---|---|---|---|
| 1 | LLM reservations not serialised | Resolved | §6.9.1 `llm_budget` month row `FOR UPDATE` is the serialisation point; counter table with `counted_usd` equal to the ledger sum (§7.17 inv. 6); lock order in §3.3.3. |
| 2 | Worst-case reservation not an upper bound | Resolved | §10.7 input bound from counted prefix ×1.10 + UTF-8 bytes + measured overhead ×2; `count_tokens` for document inputs or reject; output bounded by `max_tokens` incl. thinking; price tier chosen by the bound; round up. |
| 3 | Dispatch-fence TOCTOU | Resolved | §3.3.3 and §9.4.3: reservation **and** dispatch-marker transactions both take the owner row `FOR SHARE` with the full predicate; `ClaimLost` before any spend; heartbeat task cancels the coroutine. |
| 4 | SerpApi hourly undercount | Resolved | §6.5 formula: `account.json` before every search, `this_hour` + local attempts reserved after `observed_at − 120 s`, admit iff ≤ min(`serpapi_hourly_cap`, observed cap); deny `window_unknown` without a fresh observation. |
| 5 | Publish transaction FK order | Resolved | §9.4.1 publish takes `active_config FOR SHARE` first, inserts `llm_results` → `det_results` → target update; cyclic pairs `DEFERRABLE INITIALLY DEFERRED` (§7.1). |
| 6 | Crashed cycle blocks `score_cycles_one_running` | Resolved | §7.6 the takeover sweep sets `failed('orphaned')` for an older `worker_epoch`; `orphaned_cycles` reconciler on `heartbeat_at` > 3 min; §9.3 triggers arriving during a cycle are recorded, not lost. |
| 7 | Precompute-then-flip not atomic | Resolved | §13.4 final transaction under `active_config FOR UPDATE` re-verifies every `det_result`/`hard_filter_results` row (hash + algorithm version) before flipping; obsolete operations end `succeeded`. |
| 8 | Deletion procedure vs FKs/CHECKs | Resolved | §20.2 deletion graph per entity (target set, profile version, job tombstoning), explicit child-first order, `batch_items`/`llm_calls` deferred pair; §20.4 delete-everything order; §20.5 Adzuna purge with lineage. |
| 9 | Deletion reversible through DR | Resolved | §7.14 `deletion_manifests` (`executing/completed/shipped`), `manifest_shipments`; §17.4 `manifest_shipping` reconciler with critical alert after 1 h; §19.5 backup sets and restore-order replay of manifests after restore. |

## 3. New defects in changed material

### MAJOR

**N1 — §8.11 mass-disappearance auto-confirmation is unreachable.**
Scenario: a Greenhouse board has 20 active postings (baseline 20). The company moves to another ATS; the next complete traversal shows all 20 absent → classified `mass_disappearance`, postings → `unknown`, `mass_event_snapshot_id` set. The baseline is "the number of active postings after the last complete, **non-mass** snapshot" and is updated only "after every other complete snapshot", so it stays 20. Six hours later the next traversal again shows 20 of 20 absent: by the same rule it is classified `mass_disappearance` (status enum `complete | incomplete | mass_disappearance`, §7.7), not `complete`. The confirmation rule needs "two later **complete** snapshots ≥ 24 h apart" and the reconciler `ats_mass_events` (§17.4) looks for exactly that; neither ever occurs. The 20 dead postings sit in `unknown` indefinitely with scorable targets, and only the manual acknowledge closes them. The same happens for a partial event (12 of 20 gone): every later snapshot is 60% absent of the frozen baseline.
Fix: while `mass_event_snapshot_id` is set, classify a complete traversal as `complete` with `mass_event_snapshot_id` carried on the row (or add a status `mass_followup`), evaluate confirmation against the **first** snapshot's absent set, and state explicitly that the ordinary `absent_snapshots` counting of §7.16.2 is suppressed for postings of an open event (otherwise `ats_absent` closes them after 24 h anyway and the confirmation path is dead code). Reset the baseline on confirmation or when the absent share falls to ≤ 50%, as already written.

**N2 — §10.2 / §7.11: `CHECK (logical_calls <= http_calls)` is violated by every pre-dispatch abort.**
`call()` executes `ctx.attempt.count_logical_call()` before the stage loop. Three exits then happen before `count_http_call()`: `DeadlineExhausted` (first-try window < 30 s), `BudgetExhausted` from `spend.reserve` (§10.7) and `ClaimLost` from the `FOR SHARE` claim check inside the reservation transaction (§9.4.3). Scenario: the monthly cap is reached between admission and the generator call; the attempt must end `budget_exhausted` with `logical_calls = 1, http_calls = 0`; the UPDATE of `score_attempts` fails the CHECK; the attempt cannot leave `running` (`(outcome='running') = (ended_at IS NULL)`), the target stays `in_progress` until reclaim 3 min later, and the failure surfaces as a Bug instead of the specified `budget_deferred` transition (§10.4 table). The same path is hit at every cap-reached event, i.e. deterministically once per month under the $100 cap.
Fix: count the logical call at first dispatch (move the increment next to `count_http_call()` under a "first try of this logical call" guard), or drop the CHECK and keep `logical_calls ≤ 7` only. The §9.4.2 bound ("≤ 7 logical calls") is unaffected either way.

**N3 — §12.6 step 2(a) can activate a purged, content-less profile version.**
`profile_versions.profile_hash` is `UNIQUE` and survives a purge (`profile_json = NULL`, `purged_at` set, §20.2). Step 2(a) says "if the `profile_hash` already exists, that version is reused". Scenario: profile P1 (hash H) is confirmed; later P2 replaces it; P1's score history expires and its version is purged. The user re-uploads the same CV, the extraction is identical, the draft hashes to H → the purged row is reused: `active_config.profile_version_id` points at a row with `profile_json = NULL`; `embed_profile` has no summary to embed; `create_targets_for_tuple` creates targets whose generator prompt has no profile; the purge rule ("runs when none of the version's tuples is active") never repairs it. Fix: reuse only when `purged_at IS NULL`; if the hash matches a purged row, restore the content into that row (`profile_json := draft content, purged_at := NULL`, `confirmed_at` unchanged) in the same transaction, so existing tuple ids stay valid, or relax the UNIQUE to a partial index `WHERE purged_at IS NULL` and insert a new row.

### MINOR (fix during implementation)

- **§7.13 / §20.2** `golden_cases.job_revision_id` is `NOT NULL … ON DELETE RESTRICT`; job tombstoning deletes revisions, so a job with an *inactive* golden case cannot be tombstoned. State that tombstoning deletes inactive cases and refuses (or deactivates with an alert) active ones.
- **§13.4** step 4 covers a crash, not a permanent failure of `apply_scope` (`operations.status = 'failed'`, §7.3). Then `pending_scope_version_id` stays set forever and §9.4.1 keeps writing double `det_results` for every publish. Specify: on `failed`, clear the pointer, raise a critical alert, leave the old scope active.
- **§12.3 / §19.1** the decrypted CV sits in plaintext on the `parse_jobs` volume for the extraction's duration; a tmpfs mount with `mode=0700` and explicit unlink in a `finally` would close the at-rest gap the encryption elsewhere is meant to close.
- **§11.6 V4 / §11.3** "`experience_seniority.level` differs from the deterministic two-sub-rating value" is a T1 mandatory critic trigger, yet the deterministic value is known. Overwrite the level deterministically (as a publication repair) and record the discrepancy; sending an Opus call to fix a number the system already has costs ≈ $0.04 per occurrence for nothing.
- **§11.4 knockouts, cost risk** `mandatory_language` is `uncomparable` when the required level is `unspecified` and the profile has the language. German ads overwhelmingly phrase language requirements without CEFR ("sehr gute Deutschkenntnisse", "fließend", "verhandlungssicher"). If the generator maps those to `unspecified`, T5 fires on most German ads instead of §21's assumed 15%, and the first-round critic rate moves toward the §21 high case (0.58, $0.085/target, ≈ 1,050 targets). Add a mapping table to the generator prompt (Muttersprache → native; verhandlungssicher/fließend → C1; sehr gut → B2–C1; gut → B2; Grundkenntnisse → A2), and treat `unspecified` + profile ≥ B2 as `met`.
- **§7.7** `ats_snapshots.status` has no value for the N1 follow-up snapshots; add it with the N1 fix.

## 4. The writer's "not fully adopted" decisions

| Decision | Verdict | Reason |
|---|---|---|
| Evaluation policy recorded on the attempt but **not** part of the score identity (`llm_tuple`) | **ACCEPT** | The policy changes *which* drafts get a critic, not the rubric, prompt, schema or model; the published number has the same definition under every policy. Keeping it out of the tuple avoids a full re-score ($85+) for a threshold tweak. `score_attempts.evaluation_policy_id NOT NULL` (§7.11) preserves S4 traceability, and §11.13's eval gate on case scope catches a policy that degrades quality. |
| Codex-rejected judgment calls kept but repaired (#4 extra target states, #6 knockout severities, #8 pacing, #9 batch rules, #12 TTL/mass-disappearance, #13 gazetteer, #15 effect classes, #16 eval gate, #18 retention exemptions, #20 API no-egress, #21 backup tooling) | **ACCEPT** | Each repair addresses the concrete failure Codex named: states now have a closed transition contract (§7.16.3); severities are scoped per language with defaults (§13.6); pacing is the time-based bucket; batch rules have positive-evidence settlement; the TTL no longer expires targets; gazetteer candidates are explicit (§8.12); effect classes gained `execution`/`evaluation_policy`; the eval gate is case-scoped; retention exemptions are enumerated (§20.1); no-egress is tested (N1 in §18.2); backup sets are versioned with manifests. None of the original rejections was a correctness objection that survives the repair. |
| m10 (pins †, `httpx2`), m11 (fetch machine), m14 (no cap tolerance) handling | **ACCEPT** | m10: every unverified pin is marked and re-checked at `uv lock`; the `anthropic 1.x → httpx2` statement matches the bundled SDK-upgrade notes, and if it were wrong the adapters' `httpx` pin is unaffected. m11: the machine plus gap uniqueness is exactly what I asked for. m14: the writer went stricter than my proposal (hard upper bounds and no tolerance instead of a 1% tolerance); that is the right call given invariant 6 is now provable from request bytes. |
| B1 capacity floor $1 and `min_cycle_targets = 10` trigger | **ACCEPT** | Rate = $95 / 730 h ≈ $0.130/h. At the 6 h default the computed capacity is 2 × 0.130 × 6 ≈ $1.6 (≈ 30 targets; §6.9.2 says $1.58), so the floor is inactive; the trigger fires at min(10 × $0.0526, B_max) ≈ $0.53, i.e. after ≈ 4 h of accrual, which keeps cycles small enough to amortise the LangGraph/checkpoint overhead without letting the bucket idle. At a 1 h interval the floor ($1 ≈ 19 targets) replaces 2 × 0.130 × 1 = $0.26, which is what makes short intervals usable at all: without it a cycle could never admit more than five targets. Both constants are sensible defaults and both are settings. |
| Critic `medium` effort on revision rounds (T0), `low` only when T2/T8 alone fired in round 0 | **ACCEPT** | T0 is the convergence guard: the critic must judge whether its own earlier objections were addressed, which `low` effort does badly. Cost: revision rounds are 0.205 per target at $0.0405 vs $0.0305 → ≈ $0.002 per target. Not worth the quality risk. |

## 5. §21 arithmetic

Re-derived independently from the §10.7 price table and the §21 token assumptions (slim outputs, cache read $0.20/MTok Opus and Sonnet, cache write 1.25×):

- generate (Sonnet 5.5, medium, slim) ≈ $0.0223; critic Opus 5.5 medium ≈ $0.0405, low ≈ $0.0305; revise (Sonnet) ≈ $0.0253.
- First-round critic rate 0.45; 35% of first-round critics run at `low` (only T2/T8 fired) → first-round average 0.35 × 0.0305 + 0.65 × 0.0405 = $0.0370. q1 = 0.35, q2 = 0.30 → 0.455 follow-up rounds per first-round critic (q1 + q1·q2), hence 0.45 × 1.455 = 0.655 critic calls and 0.45 × 0.455 = 0.205 revisions per target, follow-up critics at `medium`.
- Per target: 0.0223 + 0.45 × (0.0370 + 0.455 × 0.0405) + 0.205 × 0.0253 + 0.0002 = 0.0223 + 0.0249 + 0.0052 + 0.0002 = **$0.0526**. Confirmed.
- Cache writes ≈ $9.40/month (prefix re-writes per (model, effort) after the 5-minute TTL); (95 − 9.4) / 0.0526 = **1,627 ≈ 1,630 targets/month**. Confirmed. A ($0.0793) and C ($0.0195) also reproduce; batch $0.0453/ad reproduces.
- Versus my $0.042: the spec reproduces it at a 0.30 first-round critic rate ($0.0424); my round-1 funnel also assumed far fewer revisions. The difference is entirely the funnel assumption. With the derived-outcome critic (any `revise`/`incomplete` forces a revision, T0 forces re-review) and T4/T5 firing on partial snippets, 0.45 / 0.35 / 0.30 is the better central estimate; I withdraw $0.042 as the central figure and treat it as the optimistic end. The §21 range ($0.0346 low to $0.0852 high, 2,330 to 1,050 targets) is honest, and the per-cycle cache-write accounting (≈ 180 cycles × $0.052) is new and correct. The mapping-table minor above is the cheapest lever to stay near the central figure.

## 6. Summary

- No BLOCKER remains.
- MAJOR: N1 (§8.11 confirmation unreachable), N2 (§10.2/§7.11 `logical_calls <= http_calls` CHECK), N3 (§12.6 purged-version reuse).
- Six minors for implementation.
- All five writer decisions ACCEPTED; §21 verified; my cost estimate revised toward the spec's central figure.
