**Overall verdict: REJECT.**

Reviewed revision 2 at `7c06f2a`, the changes from `dea0ffb`, both round-1 reviews, and consensus-v3 including §R’s cost-lever decision. Revision 2 makes substantial repairs, but spending guarantees, deletion, several database operations, and scoring correctness still have blocking defects.

All line references below refer to the [revision-2 specification](/home/user/job-hunter/docs/superpowers/specs/2026-10-16-job-hunter-design.md). This was a static specification review with independent arithmetic checks; the proposed migrations and services were not executed. No files were modified.

The 49 round-1 findings resolve as follows.

| # | Status | Revision-2 evidence | Remaining defect and minimal fix |
|---|---|---|---|
| 1 | **RESOLVED** | §6.9.1, L1084–1105; `llm_budget`, L2222–2236 | Every reservation, settlement and cap edit shares the monthly lock. Cap reductions have an explicit historical allowance. The separate reservation-bound defect remains under #2. |
| 2 | **PARTIALLY** | §10.7, L3742–3755; V19, L6307 | **BLOCKER:** `1.10 × estimated count` and twice the largest *observed* overhead are safety margins, not demonstrated upper bounds. This is especially material for native PDFs. A request admitted against the remaining cap can still exceed it before the alert fires. Use a provider-guaranteed bound or reserve against a documented maximum accepted billable input size; disable paths without such a bound. |
| 3 | **PARTIALLY** | §3.3.3, L285–298; §9.4.3, L3436–3439 | Run and target reservation/dispatch fencing is repaired. The new operation owner predicate lacks an attempt generation, so a retry within the same worker epoch makes a stale owner valid again. Add a per-operation claim token to every ownership check; see **N1**. |
| 4 | **PARTIALLY** | §6.5, L850–859; Q-SH1, L5510 | The original baseline-plus-local-usage error is repaired. **The shared-account guarantee remains impossible as stated:** an observation reports 40, another engine makes 10 searches during the allowed reporting lag, and this app admits another search. Require exclusive account use or shared admission coordination. Otherwise explicitly downgrade the guarantee and the impossible Q-SH1 assertion. |
| 5 | **RESOLVED** | §9.4.1, L3396–3405; L2395–2397 | Publication now locks the target, inserts the result before attaching it, and explicitly defers the cyclic FKs. Batch publication uses the same sequence. |
| 6 | **RESOLVED** | L247–252, L1652, L3313; §17.4, L5322 | Takeover and heartbeat recovery terminate orphaned cycles, close their reports and release the one-running-cycle index. |
| 7 | **PARTIALLY** | §13.4, L4730–4747; §9.2, L3294–3297 | Publication and replacement saves are coordinated, and final checks include hashes. **Collection/canonical-fact writers are not required to take the same `active_config` lock.** A new job or changed det-facts can commit after the final completeness query without pending-scope rows. Require all relevant job/input writers to take `active_config FOR SHARE`, and commit their required projections atomically or participate in a generation-based finalization protocol. |
| 8 | **PARTIALLY** | §20.2, L6048–6064; §20.4, L6098–6104; L1503–1510, L2457 | The target deletion graph is much improved, but remains non-executable in several cases. Profile purge sets a ready query proposal’s `items=NULL`, violating its CHECK. Delete-everything deletes `bulk_operations` before referencing `batch_submissions`, and deletes tuples/profiles before clearing `active_config` references. Fix those orders/states and explicitly permit the required batch cancellation transitions. |
| 9 | **PARTIALLY** | §19.5, L5922–5937; `manifest_shipments`, L2618–2622 | Offsite shipping before acknowledgement and the migration mount are repaired. **A maximum manifest ID does not prove every lower ID was shipped.** Task A can allocate ID 10 and pause before writing its file; task B ships file 11; A later completes and is incorrectly considered covered by high-water 11. Record exact shipped IDs and hashes, or serialize a genuinely contiguous manifest stream. Restore identity reuse is additionally covered by **N11**. |
| 10 | **RESOLVED** | §6.9.2–6.9.4, L1109–1140 | Persistent accrual, counted-spend debit/refund, explicit manual exceptions and headroom-based reconsideration replace the repeated fresh cycle allowance. The unsupported “never stops early” promise is removed. |
| 11 | **PARTIALLY** | §9.6.2, L3476–3508; setting range, L4684 | Spill arithmetic is repaired and lane credits persist. But `S=1` is allowed, and `ceil(0.5×S)=1` lets a continuing always-include backlog consume every slot; `R=0`, so safeguard lanes earn no credit forever. Give always-include a persistent fractional allocation too, or reserve safeguard slots across cycles. |
| 12 | **RESOLVED** | L2143–2145; §7.16.3, L2754–2785; §9.6.4 | Overrides and individual-scoring intent are durable, sweeps honor them, and `score_now` is explicitly admitted outside ordinary slots. |
| 13 | **RESOLVED** | §5.1, L521–543; L1664–1689; L2821 | Request identity now includes operation, detail ID and parameters. Physical attempt numbering spans run attempts, and exhaustion of the total bound explicitly becomes a gap. |
| 14 | **RESOLVED** | §5.3.1, L596–597; §5.6, L735–745 | Explicit continuation overrides page length; the new-ID peek occurs before normalization; initialization, contiguity and abandonment are specified. |
| 15 | **RESOLVED** | §5.3.4, L645–649 | The gap is chronologically ordered, and moving-page seeking, overlap, deduplication, closure and abandonment are explicitly described. |
| 16 | **RESOLVED** | §6.2, L787–798; L1733–1742; §9.1, L3246–3256 | Pending normalized intermediates are durable. Response persistence and settlement are atomic, and recovery checks persisted response evidence before treating an attempt as lost. |
| 17 | **PARTIALLY** | §7.16.2, L2728–2735; §8.11, L3205–3210; L5337 | Saturating counters, `unknown→closed`, and independent membership are repaired. **Mass-event confirmation still refers to two later `complete` snapshots, while persistent shrinkage makes those snapshots `mass_disappearance`.** Define confirmation over structurally complete snapshots, explicitly including that status, and distinguish structural completeness from closure classification. |
| 18 | **RESOLVED** | §8.6, L3050–3058; §7.16.2, L2720–2735 | Observation application is idempotent, sighting time comes from acquisition, timestamps/pointers are monotonic, and old sightings cannot reopen newer closures. |
| 19 | **RESOLVED** | §6.6, L864–879; §5.1, L545–550 | Local reserve denial is separate from provider exhaustion. Breaker/auth/contract failures have explicit eligibility, and envelope failures are classified before fallback planning. |
| 20 | **RESOLVED** | §6.3, L802–825; §6.7, L988–1019 | Historical windows, bootstrap, ordered limit-before-remaining reconciliation and reset attribution replace the original mutable-window calculation. The specific 200→100 counterexample is corrected. |
| 21 | **PARTIALLY** | §8.6, L3053–3065; L1848, L1991–1998 | Merge/unmerge provenance and action handling are substantially specified. **New identity bootstrap is ordered incorrectly:** it inserts a posting before deciding its required `job_id`, and inserts `ad_key_sources` before missing parent `ad_keys` exist. Allocate/select the job first; insert the posting and resolved key rows before their provenance rows, or explicitly defer suitable FKs and preallocate all required IDs. |
| 22 | **RESOLVED** | §8.7, L3113–3116, L3136, L3145–3149; §7.18 | Exclusions are checked against all members, representative changes revalidate membership, and independently scored members receive their own ranked cards. |
| 23 | **RESOLVED** | §8.7, L3120–3128; invariant 4, L2891 | Election excludes failed/refused/expired/filtered targets, recognizes budget-deferred targets, and has explicit unscorable outcomes without resetting retries. |
| 24 | **RESOLVED** | §8.8, L3172; §9.7, L3540–3566; L6015 | A→B→A reuses revisions with occurrence history; eligible cancelled targets revive; active-tuple target history is protected from age-based recreation. Purged-profile reuse introduces a separate defect, **N12**. |
| 25 | **RESOLVED** | §11.4, L4023–4042 | Requirement, comparison and confirmation are separated. Caps require a proven unmet condition; accepted on-site work and uncomparable language requirements no longer automatically cap. Future-scope review is separately defective under **N5**. |
| 26 | **PARTIALLY** | §8.12, L3223–3234; §11.8, L4321–4334; L4648 | Ambiguity and remote eligibility are substantially repaired. However, the new distance formula divides by a radius that Settings explicitly allows to be zero. Define zero-radius semantics or require positive radii; see **N13**. |
| 27 | **RESOLVED** | §11.4, L4003–4041; §11.7.2, L4245–4259 | Stable fact IDs, exactly one grounded review per required fact and derived acceptance address the original missing/duplicate/unconfirmed-fact acceptance defect. The slimmer critic introduces different gaps under **N6–N7**. |
| 28 | **RESOLVED** | §11.6 V3, L4195; L4174 | Empty/ellipsis evidence is rejected. Quotes must be contiguous, meaningful matches with mapped offsets and explicit source kinds. |
| 29 | **PARTIALLY** | §11.6, L4186–4223; requirement limit, L4176 | Structural parsing and semantic validation are separated, with final strict validation. But the repair table omits overlong requirement `text` fields, which remain structurally valid and can survive both revisions. Publication then becomes `Bug` instead of valid contested output. Specify repair/null-out/failure handling for **every** limited field, including requirement text. |
| 30 | **RESOLVED** | §11.8, L4349–4354; DDL, L2435–2436 | Endpoints are transformed first, and the displayed midpoint is recomputed from the final interval. |
| 31 | **RESOLVED** | §11.3, L3931–3953 | The two sub-rating tables are disjoint and their combination and unknown-input behavior are explicit. |
| 32 | **PARTIALLY** | L1265–1270, L2204, L2294; L3356 versus L3560 and L4720 | Separate immutable execution configs fix hash collisions. However, one contract pins the execution config for the entire attempt, while two others require the next call to read the newly active config. Choose one rule and align attempt records, gateway selection and resume behavior. |
| 33 | **PARTIALLY** | §11.11, L4393–4405; §17.4, L5331–5332 | Prospective evaluation-policy provenance and immutable deterministic instances are acceptable improvements. **Hard-filter algorithm upgrades are not fully reconciled:** the hard-filter reconciler tests only missing rows/input hashes, not `det_algorithm_version`; an unscored job can remain filtered by obsolete code indefinitely. Include algorithm-version mismatch and refilter afterward. Instance replacement also needs **N2**. |
| 34 | **PARTIALLY** | L2211–2219; §10.2, L3615–3624; §10.4, L3677–3680 | Logical/physical bounds and viable first-call deadlines are repaired. But `logical_calls <= http_calls` conflicts with incrementing the logical count before reservation and dispatch, especially when the reservation is denied. See **N3**. |
| 35 | **PARTIALLY** | §6.9.3, L1125–1131; §10.10, L3784–3789 | Eligibility sweeps and a cumulative batch-operation counter are added. **The 40% envelope does not include all batched-target follow-ups:** overflow explicitly moves to the ordinary bucket. Follow-up holds are expected values and do not reserve hard-cap headroom. Either cap the complete batched branch and defer overflow, or accurately define 40% as a batch allocation rather than a total bulk-spend limit. |
| 36 | **PARTIALLY** | §10.10, L3804–3825; §7.16.3, L2772–2780; L6049 | Per-item fencing, stop reasons and complete-stream handling are repaired. `in_batch` still lacks cancellation transitions for **revision supersession, job merge and purge**, although those paths require them. Add those transitions and their cancellation/settlement behavior. Also address **N8–N9**. |
| 37 | **PARTIALLY** | §10.10, L3829–3848; V12, L6300 | The observation window and never-created settlement path are now explicit. But a successful small visibility-lag experiment does not establish a maximum server creation/visibility delay. A client timeout does not prove the server stopped creating the batch. Without an authoritative bound or provider confirmation, an empty listing must remain ambiguous. |
| 38 | **PARTIALLY** | §20.1, L6010–6029; §20.2, L6059–6066 | Raw metadata/body dependencies and golden-job exemptions are repaired. **Non-target extraction/query `llm_calls.output_json` have no matching purge path**, so purging a profile/draft can leave its extracted content in call output indefinitely. Include auxiliary calls and eval calls in retention ownership. Also honor the consensus requirement to retain profile JSON while feedback still requires it. |
| 39 | **PARTIALLY** | §20.5, L6119–6127; L2567–2578; §8.8, L3172–3173 | Revision lineage and broader derivative deletion are added. Deleting tainted `eval_case_results` does **not** delete their calls: those calls belong to the surviving `eval_runs`, and call IDs are only an array on the case result. Delete all tainted eval-call outputs explicitly. Also ensure a rebuilt identical hash does not reuse a revision scheduled for deletion, and close the purge race in **N14**. |
| 40 | **RESOLVED** | §9.5, L3447–3452; §12.3, L4505–4511 | Text review precedes transmission, approved-text hashes are recorded, later text edits invalidate extraction, and evidence is checked against transmitted text. |
| 41 | **PARTIALLY** | §19.5, L5914–5943; roles, L1165; initialization, L5878–5886 | One tagged backup set and explicit error handling improve recovery. The restore-only role still cannot perform default ownership restoration or install untrusted extensions merely because it has `CREATEDB`; specify `--no-owner`/ACL handling and privileged extension provisioning. `backup_set.json` also lacks the row-count inventory that restore verification expects. |
| 42 | **RESOLVED** | §16.9, L5244–5251; Compose, L5708–5738 | Internal-only API networks, separate Caddy egress, per-service secrets and a networkless parser repair the original boundary defects. Secret file readability is a new deployment defect, **N10**. |
| 43 | **RESOLVED** | L5236; PostgreSQL settings, L5803–5807 | Parameter, statement and error-detail logging are explicitly restricted, with real-driver verification specified. |
| 44 | **PARTIALLY** | §11.13, L4438–4459; `golden_cases`, L2533–2547 | Case scope, mixed-profile evaluation and production routing are repaired. **Cases still lack immutable deterministic input snapshots.** Salary can change without a new material revision, so rerunning the same case can compare a different job state against its old label. Freeze det-inputs and label revision per case/run. Also define the production T8 sampling identity for evaluations that deliberately run outside the target table. |
| 45 | **RESOLVED** | §3.3.2, L275; `operations`, L1352–1383; L5323 | Auxiliary work now has same-transaction durable intents and reconciliation independently of transactional queue support. Operation execution fencing still needs **N1**, but the original lost-enqueue gap is repaired. |
| 46 | **PARTIALLY** | L1188; §14.3, L4863–4882 | A committed monotonic generation replaces `max(result generation)`, and cursor sections are defined. The enumerated bump events omit new jobs, saved/applied/seen filter changes and several unpublished-state membership changes. Also require list rows and their reported generation to come from the **same snapshot**, preventing an old page from receiving a newer generation and suppressing the change banner. |
| 47 | **RESOLVED** | §14.1, L4786; §12.6, L4611–4620; C34, L5580 | Persisted request hashes/outcomes cover delayed retries, and repeated confirmation returns the original result without reactivating an old profile. |
| 48 | **PARTIALLY** | §7.16 metadata; added endpoints, L4814–4829; L4593; L1915; status, L5 | Transition metadata, endpoint and language-field omissions are largely repaired. The document still calls itself approved, and delete-everything says to “cancel” operations/cycles without defining those states or their legal terminal mapping. Mark review status pending and give exact cancellation mappings. |
| 49 | **PARTIALLY** | §21, L6164–6280 | Revision-1 corrections are accurately recorded and the new central B calculation is sound. Several new counterfactual, cache and batch calculations remain inconsistent; exact corrections follow below. |

The following are new defects introduced or exposed by the changed material. References to them above avoid duplicating their full explanations.

| # | Severity | Section | Problem | Fix |
|---|---|---|---|---|
| **N1** | **BLOCKER** | §§3.3.3, 7.3, 17.4; L292, L1359–1369, L5323 | **Operation ownership has an ABA race.** Operation attempt A stalls; reconciliation changes it from running to pending; attempt B starts in the same `worker_epoch`. A resumes and again satisfies `worker_epoch=:epoch AND status='running'`. Both can dispatch paid extraction calls or write the same draft/export/purge results. | Give every operation execution a fresh claim UUID or monotonic attempt generation. Reservation, dispatch, heartbeat and all writes must compare it. Reclaim must invalidate it atomically. |
| **N2** | **BLOCKER** | §7.11; L2439, L2445 | **Deterministic-instance replacement violates its immediate unique index.** The specified order inserts a new `is_current=true` row and then clears the previous row. Any recomputation of an existing `(result, scope)` fails on `det_results_current` before reaching the second step. PostgreSQL checks immediate uniqueness during insertion. [Constraint rules](https://www.postgresql.org/docs/17/sql-createtable.html) | Serialize replacement for the pair, clear the old current flag first, then insert the replacement in the same transaction. Rollback preserves the previous instance on failure. |
| **N3** | **MAJOR** | §§7.11, 10.2; L2219, L3615–3624 | **The new counter CHECK rejects a legitimate call lifecycle.** The gateway increments `logical_calls` before reserving or incrementing `http_calls`. The first call therefore reaches 1/0; if budget admission fails, it never reaches 1/1. Persisting the stated counters violates the CHECK. | Define whether a denied step counts as a logical call. Remove the invalid inequality or update counters atomically at the first physical attempt, with separate attempted/admitted counters if needed. |
| **N4** | **MAJOR** | §6.7; L908–923, L955, L1022 | **The dry-run path is not read-only executable.** `reserve(dry=True)` unconditionally calls the owner’s `FOR SHARE` locking function inside a read-only transaction. Bootstrap and rollover also call row-creating/closing helpers before reaching the `dry` return. Planning a fresh or expired window can fail instead of producing a projection. | Separate pure projection from mutation. Load ordinary snapshots without row locks, then simulate bootstrap, rollover and reservations in memory. Only authoritative reservation performs writes and ownership locking. |
| **N5** | **MAJOR** | §§11.4, 11.7, 11.11; L4027, L4033, L4238, L4405 | **The consensus requirement to review scope-dependent on-site facts has disappeared.** An ad’s text-only on-site requirement is initially met, so T5 does not fire. The user later changes to remote-only. Scope recomputation cannot call the critic, so the now-unmet requirement remains unconfirmed and cannot cap. The mandatory review guarantee depends on which scope happened to be active during generation. | Restore the consensus rule: review scope-dependent binding-capable facts whenever any supported scope can make them binding, including on-site-only assertions even when currently met. |
| **N6** | **MAJOR** | §§11.6–11.7; L4198, L4245–4254, L4292 | **The slimmer critic cannot reject `not_a_job_ad`.** Generator returns `not_a_job_ad=true` with null criteria. Critic returns `verdict=revise`, explaining that it is a real job, but has no legal review target for that boolean. With no required facts or criterion disputes, the derived outcome becomes `accept`, and the real job is hidden as verified non-job content. | Add an explicit, mandatory review of the non-job assertion, with grounded confirmation/rejection/uncertainty. Only positively confirmed non-job results may receive that label and default exclusion. |
| **N7** | **MAJOR** | §§11.6–11.7; L4203, L4247–4252 | **Derived acceptance ignores unresolved V4 issues.** Suppose the deterministic seniority calculation is 0 but the generator gives 4. T1 invokes the critic; an empty review array has no required fact omissions and no V2/V3 failures, so it derives `accept`. The contradictory rating publishes as accepted. | Make acceptance require resolution of mandatory consistency issues as well as evidence failures. Deterministically repair computable ratings or retain contested status after bounded revision. |
| **N8** | **MAJOR** | §§6.9.3, 10.10; L3784, L3790, L3812–3815 | **Batch follow-ups and online-only retries can be batched again.** A successful item requiring critique becomes `pending(resume_draft_id)`. Reconciliation schedules another batch whose eligibility accepts all pending targets in the bulk operation. If it wins the target CAS before the follow-up cycle, it buys another generator instead of reviewing the existing draft. Max-token retries can similarly repeat the original batch stage. | Restrict batch eligibility to targets requiring an initial generator. Exclude `resume_draft_id`, follow-up lanes and online-retry dispositions; enforce charged-attempt limits at preparation too. |
| **N9** | **MAJOR** | §§7.16.7, 10.10; L2848–2852, L3817–3820, L3846–3848 | **Released ambiguous items cannot settle valid unbilled outcomes.** After user release, ledger items are ambiguous. The batch is later found, but an item is `errored`, `canceled` or `expired`. The decision table requires `not_billed`; the ledger only permits ambiguous→not_billed when the *entire batch was never created*. Processing either fails or permanently overcounts spend. | Permit the specific provider-proven per-item unbilled transitions for ambiguous batch entries, with matching CHECK evidence codes and atomic budget updates. |
| **N10** | **BLOCKER** | §§19.1–19.2; L5682, L5700–5720, L5824 | **The new secret delivery cannot be read by the application.** Services run as UID 10001, while file-backed secrets are root-owned mode 0400. Compose bind-mounts those files and does not remap file-secret ownership. Startup cannot read database/provider credentials. [Docker secret behavior](https://docs.docker.com/reference/compose-file/services/) | Provision host files with ownership/group permissions readable by the intended container UID/GID, or use a documented privileged initialization step that copies them into appropriately owned private storage before dropping privileges. |
| **N11** | **MAJOR** | §§7.14, 19.5; L2602–2622, L5929–5934 | **Restore reuses identities referenced by the newer manifest stream.** The dump restores older sequence positions, while offsite manifests contain later IDs. Subsequent jobs or manifests can reuse those IDs; a later replay can delete legitimate new data or a new manifest file can collide with an old filename. | Use immutable deployment/epoch-qualified UUID identities for the stream, and prevent reuse of purged object identities. Alternatively persist and restore sequence high-water marks covering all post-backup manifest references before admitting writes. |
| **N12** | **MAJOR** | §§7.4, 12.6, 20.2; L1441–1444, L4612, L6063 | **Re-uploading a purged profile activates an empty historical version.** Purge retains the unique `profile_hash` but nulls `profile_json`. Confirming identical uploaded content reuses that row, making the active profile lack its required content. | Deduplicate only against usable, unpurged profile versions. Create a new version/identity for reintroduced content and ensure old deletion manifests cannot purge the new instance. |
| **N13** | **MAJOR** | §§11.8, 13.1; L4321–4325, L4648 | **Valid Settings can cause division by zero.** `radius_km=0` is allowed, but distance bands compute `d/r_i`. Scope application or scoring fails on a user-valid city-only search. | Require radius ≥1, or define a zero-radius branch before division with explicit exact-location semantics. |
| **N14** | **MAJOR** | §§9.7, 17.4, 20.2, 20.5; L3566, L5329, L6046–6057, L6119–6127 | **Chunked purge can recreate the work it just deleted.** Source purge deletes an active-tuple target before deleting/rebuilding its job revision. The concurrently running missing-target reconciler sees the retained current revision and recreates it; scoring can start again. The source fence only blocks ingestion. An in-memory admission-stop flag also does not survive restart. | Persist a purge fence on affected input identities and make target creation, admission, publication, eval and export honor it under a shared locking protocol. Keep it until the complete deletion/rebuild operation commits. |

For #2, the [provider’s token-counting documentation](https://platform.claude.com/docs/en/build-with-claude/token-counting) describes an estimate; it does not establish the specification’s universal 10% error bound. Passing 30 fixtures establishes empirical coverage, not the hard guarantee required by S3.

For #41, ordinary `pg_restore` ownership commands require suitable ownership privileges; `CREATEDB` alone is insufficient. Extension provisioning also needs an explicit privileged path for pgvector. See [PostgreSQL restore rules](https://www.postgresql.org/docs/17/app-pgrestore.html), [extension rules](https://www.postgresql.org/docs/17/sql-createextension.html), and [pgvector’s control file](https://raw.githubusercontent.com/pgvector/pgvector/v0.8.7/vector.control).

The writer’s four “not fully adopted” decisions receive these verdicts.

| Decision | Verdict | Assessment |
|---|---|---|
| **Evaluation policy recorded, but excluded from score identity** | **ACCEPT** | This is a defensible prospective policy: existing immutable results retain their actual evaluation-policy provenance, and Settings explicitly discloses no automatic re-review. Consensus’s five-part result identity can remain. However, “explicit re-score creates a new tuple” is not an implemented same-input re-review mechanism: immutable hash deduplication reuses that tuple. Remove that promise or define a separate explicit score generation. |
| **Previously rejected judgment calls retained as “repaired”** | **REJECT as a blanket claim** | Some are now acceptable; several remain incomplete. Individual verdicts follow. |
| **True-upper-bound reservations** | **REJECT as specified** | The requirement is correct, but the implementation still substitutes measured margins for proven bounds. A post-spend pause cannot enforce the preceding hard cap. |
| **Bulk re-score limited by the 40% share** | **REJECT as claimed** | The cumulative counter bounds the amount charged to the bulk allocation, not all spending caused by that branch. Follow-up overflow explicitly escapes to the bucket, while other retained targets also score online. Define the 40% boundary accurately and distinguish an expected follow-up allocation from guaranteed funding. |

The previously rejected judgment calls should be treated individually:

| Round-1 judgment call | Round-2 verdict | Reason |
|---|---|---|
| Extra target states | **REJECT** | Batch purge/merge/revision cancellation remains outside the closed matrix; #36. |
| Knockout severities and caps | **REJECT as a complete contract** | The comparison semantics are repaired, and 25/10 can remain provisional. Scope-dependent review still fails; N5. |
| LLM budget pacing | **ACCEPT** | Persistent elapsed-time accrual now implements pacing; #10. |
| Batch eligibility, sizing and budgets | **REJECT** | Follow-up overflow, rebatching and ambiguous unbilled settlement remain; #35, N8–N9. |
| TTL and mass-disappearance policy | **REJECT as a complete contract** | The TTL explanation and saturating counter are repaired; mass-event confirmation still has a status-selection gap; #17. |
| Offline gazetteer | **ACCEPT architectural choice** | Keeping ambiguity is the needed correction. Fix the zero-radius implementation defect before implementation; N13. |
| Settings effect classes | **REJECT as a complete contract** | Execution-config selection conflicts, and input writers do not fully participate in scope activation coordination; #7, #32. |
| Evaluation gate | **REJECT** | Production routing is repaired, but deterministic case inputs and sampling identity are not reproducibly frozen; #44. |
| Retention exemptions | **REJECT** | Auxiliary/eval call content remains outside complete purge ownership; #38–39. |
| API no-egress boundary | **ACCEPT** | Networks and role-scoped secret distribution now express the intended boundary; #42. |
| `pg_dump` + restic recovery procedure | **REJECT as a complete contract** | Shipment coverage, restore privileges and identity reuse remain unresolved; #9, #41, N11. |

The §21 arithmetic checks are below. These use the specification’s prices and token assumptions, not independently substituted pricing.

The exact central funnel is:

```text
revision calls/target = 0.45 × 0.35 × (1 + 0.30)
                      = 0.20475

critic calls/target  = 0.45 + 0.20475
                      = 0.65475

first critic cost    = 0.35 × 0.0305 + 0.65 × 0.0405
                      = $0.037
```

Including the stated 1% extra generator-call repair assumption:

```text
B central = 0.0223
          + 0.45 × 0.037
          + 0.20475 × 0.0405
          + 0.20475 × 0.0253
          + 0.01 × 0.0223
          = $0.05264555 per target
```

| §21 item | Verified result | Assessment |
|---|---:|---|
| B central | **$0.05264555/target; $157.93665/3,000** | Stated $0.0526 and approximately $158 are correct. |
| B low | **$0.03456175/target; $103.68525/3,000** | Stated $0.0346 is correct under the stated low token/funnel assumptions and unchanged light-critic share. |
| B high | **$0.08521558/target; $255.64674/3,000** | Stated $0.0852 is correct. |
| Same routing, unslimmed | **$0.07090005/target** | Stated $0.0709 is correct. |
| Slimming saving | **$0.01825450/target, 25.75%** | Approximately $0.0183 and 26% are correct. |
| Revision-1 routing counterfactual, 55% first critique, all medium | **$0.08254995/target** | Rounds to **$0.0825**, not $0.0826, using unrounded inputs as promised. |
| A, literally “all Opus/medium” | **$0.08083045/target** | The stated **$0.0793** instead matches retaining low-effort discretionary critiques: **$0.07925545**. Correct the label or the number. |
| C, stated funnel and 30% Haiku uplift | **$0.019352351/target** | Assuming the same 35% light share, rounds to **$0.0194**, not $0.0195. State any different effort mix explicitly. |
| B with 30% first-round critique | **$0.04260470/target** | The stated $0.0424 excludes the repair term. Capacity is approximately **2,230** without cache overhead, or **2,009** after $9.40. |
| B capacities using the table’s fixed cache allowances | **1,626 central; 2,332 low; 1,047 high** | Approximately 1,630 / 2,330 / 1,050 is reasonable. These are conditional on the assumed cache allowances. |
| Profile extraction | **$0.140** | Correct: 5,000 input at $4/MTok plus 6,000 output at $20/MTok. |
| Query proposal | **$0.094** | Approximately $0.10 is reasonable. |

Three larger accounting corrections are needed.

1. **Cache writes are partly double-counted, and cycle counts are not self-consistent.**  
   Per-target costs already charge every prefix as a cache read. Replacing a read with a write adds the **write-minus-read** difference, not the full write price.

   With the stated 80% low-effort-prefix frequency:

   ```text
   Gross writes/cycle = 0.01125 + 0.0225 + 0.8 × 0.0225
                      = $0.05175

   Increment above already-counted reads
                      = 0.01035 + 0.0216 + 0.8 × 0.0216
                      = $0.04923
   ```

   Under a simplified ten-target-cycle model, central capacity is:

   ```text
   95 / (0.05264555 + 0.04923 / 10) ≈ 1,650 targets
   ```

   That implies approximately 165 cycles, not 180. An **uncapped 3,000-target** calculation needs its own cycle count—approximately 300 under that model—and approximately **$14.77** incremental cache overhead, rather than reusing the capped month’s $9.40. Actual cache expirations within long cycles remain a measured assumption.

2. **The cold batch generator again omits the cache-write premium.**  
   Batch parameters retain the prefix cache marker. Assuming a cold write and no reads:

   ```text
   batch generator = 2,200 × $1/MTok
                   + 4,500 × $1.25/MTok
                   + 1,700 × $5/MTok
                   = $0.016325

   online follow-ups = $0.024942375 + $0.005180175
                     = $0.03012255

   total, excluding structural repairs
                     = $0.04644755/target
                     = $46.44755/1,000
   ```

   Thus **$45.30/1,000 is not the cold-cache result for the specified payload**. Add the chosen structural-repair allowance explicitly.

   The stated reservation is correct:

   ```text
   22,750 × $1.25/MTok + 8,000 × $5/MTok = $0.0684375
   total planning hold/item = 0.0684375 + 0.03012255
                            = $0.09856005
   ```

   A $38 allocation fits **385** initial items. After their expected cold-cache cost and follow-ups settle, the next batch fits approximately **204**, not 208, before repairs. A $23.20 allocation fits **235** initial items and leaves approximately **$12.28**, not $12.50.

3. **The 2,250-target comparison uses the wrong percentage description.**  
   Compared with the table’s approximately 1,630 central capacity, 2,250 is approximately **38% higher**. Equivalently, central capacity is approximately **28% below** the 2,250 target. “Optimistic by 28%” conflates those denominators.

The user-approved B models, $100 cap and U14 defaults are preserved. The corrected arithmetic supports reporting a lower measured capacity when critic rates are higher; it does not justify presenting the approximate 2,250-target aspiration as guaranteed.