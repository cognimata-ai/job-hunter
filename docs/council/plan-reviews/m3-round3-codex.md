**Verdict: REJECT**

P refers to the [plan at `67befef`](/home/user/job-hunter/docs/superpowers/plans/2026-10-25-m3-agentic-scoring.md); S refers to the [revision 3.4 spec](/home/user/job-hunter/docs/superpowers/specs/2026-10-16-job-hunter-design.md), accounting for the settled decisions and proposed amendments. Code references are relative to `backend/src/jobhunter/`, unless otherwise stated.

“RESOLVED” means the plan adequately specifies the correction, not that M3 has been implemented or tested. Review used offline, read-only inspection and local probes. No paid calls or restricted directories were accessed; the workspace is unchanged.

## A. Disposition of round-2 findings

The A identifiers retain the original finding numbers used by the response document.

- **A1 — RESOLVED:** `operations` joins the removed truncate roots; deletion ordering and the fully migrated FK-closure test address the additional cascade. P:149; S:2524–2552; code: `tests/support/fixtures.py:52`, `db/migrations/versions/0006_m2_llm_profile_settings.py:304`.
- **A3 — PARTIALLY RESOLVED:** Stored wire schemas and validation models are separated, and description-varied C versions work; the replacement family discriminator accepts structurally incompatible schemas. P:308; S:4355,4565; code: `llm/gateway.py:984`. See B11.
- **A12 — PARTIALLY RESOLVED:** Cycle-owned heartbeats and cancellation translation solve the runtime design, but the declared API and prescribed spec amendment retain the old lifecycle. P:567,621,623,1220; S:3572–3575; code: `worker/executions.py:154`. See B18.
- **A15 — RESOLVED:** The claim uses `CAST(:… AS …[])`; the offline SQLAlchemy probe recognizes the array binds. P:229–231,623; S:3477–3488; code: `db/sql.py:25`.
- **A18 — PARTIALLY RESOLVED:** Activation, publication and cleanup ordering improve, but submission insertion acquires an implicit earlier bulk lock, and failure hooks enter recovery after locking the operation. P:86,850,853,1269; S:312,2524,3396; code: `worker/operations.py:418–431`. See B1–B2.
- **A23 — PARTIALLY RESOLVED:** Preparation identity is persisted and checked, but the takeover predicate still depends on sweep order. P:159,850,853,1269; S:304–310,3956; code: `worker/epoch.py:164`, `worker/operations.py:479–489`. See B3.
- **A25 — RESOLVED:** Verification now derives all required schema/model pairs from all five policy nodes, including profile extraction and query generation. P:1008,1234; S:4006–4014; code: `llm/schema_dry_run.py:111`, `settings/model.py:400`.
- **A30 — PARTIALLY RESOLVED:** Pre-dispatch step records close the basic settlement/receipt gap, but repair calls share identities and completed error receipts have no recovery rule. P:989,991; S:295,3552–3558,3835; code: `llm/gateway.py:977–1002`, `llm/spend_ledger.py:935–958`. See B4–B5.
- **A35 — RESOLVED:** The settings ranking bump moves after pointer flip and activation; shipped-version activation uses the same corrected sequence. P:793,1050; S:4898–4901; code: `settings/save.py:379–388`.
- **A42 — PARTIALLY RESOLVED:** Actual prompt reservations, ordering and admission setup are specified, but release assertions still confuse actual `r` with policy-level `r_first` and ignore cent precision. P:458,704,715–737; S:1201,2297; code: `llm/pricing.py:103`, `llm/budget.py`. See B16.

- **B1 — RESOLVED:** The unsafe `operations` truncate root is removed and its FK path explicitly covered by the closure test. P:149; S:2552; code: `tests/support/fixtures.py:52`.
- **B2 — RESOLVED:** All three array casts use SQLAlchemy-compatible `CAST` syntax, with bind inspection required. P:229–231,623; S:3477–3488; code: `db/sql.py:25`.
- **B3 — PARTIALLY RESOLVED:** Wire/model separation and compatible version variation are explicit, but “same property names” does not establish parser compatibility. P:308; S:4355,4565; code: `llm/gateway.py:984`. See B11.
- **B4 — RESOLVED:** C uses a separately identified synthetic minimum-keyword probe when no suitable integer exists. P:314,1234; S:4013; code: `llm/schema_dry_run.py:159–184`.
- **B5 — PARTIALLY RESOLVED:** Every claim is heartbeaten after claim commit, including queued claims, with an appropriate four-minute test; Task 25 still specifies wrapper-started heartbeats. P:567,599,623,1220; S:3572; code: `worker/heartbeat.py:33`. See B18.
- **B6 — RESOLVED:** The wrapper owns a child task, distinguishes ownership loss from shutdown, returns `claim_lost`, and preserves sibling execution. P:599,621; S:3453,3572–3575; code: `worker/executions.py:164–202`.
- **B7 — PARTIALLY RESOLVED:** The named rejection, release and activation sequences are corrected, but the global schedule remains violated through implicit FK locking and operation-framework entry points. P:850–853,916,1269; S:312,2524; code: `worker/operations.py:402–489`. See B1–B2.
- **B8 — RESOLVED:** Target CAS and attempt-number advancement precede dependent attempts/items; concurrent online claims are tested. P:850,897,900; S:2290,3939; code: `db/transitions.py:522`.
- **B9 — RESOLVED:** The surviving count is checked after CAS, with whole-transaction rollback for zero or below-minimum survivors. P:850,897; S:2525,3944; code: `db/transitions.py:522`.
- **B10 — RESOLVED:** Sizing and final allocation checks now share month/bulk locks. The new test’s interleaving is impossible, but the specified admission rule is corrected. P:846,850,897; S:1189–1191; code: `llm/spend_ledger.py:506`. See B19.
- **B11 — RESOLVED:** Never-dispatched release explicitly reverses the bulk debit once, alongside month/bucket accounting. P:454,853,897; S:1189–1191; code: `llm/spend_ledger.py:608–632`.
- **B12 — PARTIALLY RESOLVED:** Stored claim identity and preparation fencing are present; recovery before the operations sweep still misses an old-epoch execution. P:159,850,853; S:304–310,3956; code: `worker/epoch.py:164`, `worker/operations.py:558–577`. See B3.
- **B13 — RESOLVED:** Cleanup transitions only running attempts and keeps accounting independent of an already-terminal attempt. P:852–853,914,916; S:2901,3959; code: `db/transitions.py:490–519`.
- **B14 — RESOLVED:** Missing results traverse the shared item-finalization path, including ownership, attempt disposition and hold release. P:913–914; S:3959–3960,3999; code: `db/transitions.py:490–519`.
- **B15 — RESOLVED:** Follow-on is keyed and marked on the originating submission, and durable `released_at` suppresses replay after adoption. P:159,854,913,915–916; S:3945,3999; code: `worker/operations.py:301–327`.
- **B16 — RESOLVED:** Draft normalization and first-validation corrections are immutable, with repeated-validation/reclaim assertions. Publication still needs an explicit projection of that history. P:528,599,1376; S:4367; code: `llm/schemas/generator.py`. See B10.
- **B17 — RESOLVED:** `unavailable` represents an invalid critic assessment, satisfies the CHECK and receives conservative confidence. P:159,407,565,599; S:3557–3558,4534–4537; code: `llm/schemas/critic.py:36`.
- **B18 — PARTIALLY RESOLVED:** The pure publication layer solves targetless evaluation, but Task 10 still explicitly instructs eval callers to use production persistence. P:558,565,602,989; S:4611; code: `db/transitions.py:522`. See B17.
- **B19 — PARTIALLY RESOLVED:** A durable pre-dispatch identity exists, but it is insufficient for structural repairs and multiple call receipts; completed errors remain unhandled. P:989,991; S:3552–3558,3835; code: `llm/spend_ledger.py:935–958`. See B4–B5.
- **B20 — PARTIALLY RESOLVED:** Requested/actual transport and live gate provenance are specified, but the mismatch branch writes a nonexistent reason column. P:989,991,1008; S:2625–2641; code: `db/transitions.py:429–437`. See B6.
- **B21 — PARTIALLY RESOLVED:** Candidate-payload staging breaks the provisioning cycle, but invokes a serializer-dependent helper without specifying its required serialization. P:989,1008; S:4898–4901; code: `settings/derive.py:87–97`, `db/seed.py:203–218`. See B7.
- **B22 — RESOLVED:** Second repetitions are scheduled adaptively after first repetitions, ordered by case ID, bounded at twenty attempts and recoverable from result rows. P:989,1006,1025–1028; S:4613,4625; code: `settings/validate.py:59`.
- **B23 — PARTIALLY RESOLVED:** Direct accepted-review and not-ad resumes are covered, but multi-attempt draft ancestry loses critic progress, and final-round rejected reviews have no complete resume route. P:525–526,599; S:3523,3554–3568; code: `llm/schemas/critic.py`. See B8–B9.
- **B24 — RESOLVED:** The gate explicitly includes generate, revise, critic, extract-profile and propose-queries pairs. P:1008,1234; S:4006–4014; code: `llm/schema_dry_run.py:111–115`.
- **B25 — RESOLVED:** The five previously identified signature, graph, singleton-fixture and folding inconsistencies are corrected. P:287,644,774,1270,1289; S:2547–2548,3493–3499; code: `normalize/text.py:36`, `worker/tasks.py:377`.
- **B26 — PARTIALLY RESOLVED:** Matching actual reservations removes the original settlement-bound mismatch, but the amended fixture does not establish its release counts. P:458,704,715–737; S:1201,2297; code: `llm/pricing.py:103`. See B16.

## B. Newly identified defects and regressions

1. **BLOCKER — Inserting the submission acquires the bulk lock before the month lock.**  
   **P:**850,900. **S:**312,2524. **Code:** `llm/spend_ledger.py:506`; planned extension at P:453,846.  
   `batch_submissions.bulk_operation_id` is an immediate FK. Inserting the submission therefore takes a key-share lock on the referenced bulk row before step 6. A funded online reservation can hold the month row and wait for that bulk row `FOR UPDATE`, while preparation holds the FK lock and waits for the month row. Target `SKIP LOCKED` does not break this cycle.  
   **Fix:** acquire month/bulk locks before inserting the new submission, explicitly accommodating an unpublished owner row, or specify another demonstrably safe FK/locking design. Add a concurrency test using the real FK.

2. **BLOCKER — Batch failure recovery enters the lock schedule too late.**  
   **P:**853,1269. **S:**312,3396,3956. **Code:** [operations.py:402](/home/user/job-hunter/backend/src/jobhunter/worker/operations.py:402), [scope_apply.py:65](/home/user/job-hunter/backend/src/jobhunter/settings/scope_apply.py:65).  
   Failure hooks run after the framework locks/updates the operation. `recover_lost` then requests `active_config`, reversing configuration→owner. A handler can hold configuration while waiting for that operation; a queued configuration writer can then block the hook’s later shared configuration request. Existing scope-apply code explicitly documents why this kind of cleanup cannot run in its failure hook.  
   **Fix:** perform recovery in a separately fenced transaction/reconciler after failure commits, or change every framework entry point to acquire the full prefix before locking operations.

3. **MAJOR — The new recovery predicate is still not sweep-order independent.**  
   **P:**853,897,1229,1269. **S:**304–310,3956. **Code:** `worker/epoch.py:164`, `worker/operations.py:479–489,558–577`.  
   When the submission sweep runs first, a dead epoch’s operation remains `running` with the stored claim, so recovery skips it. The subsequent ordinary operation requeue invokes no failure hook, and takeover does not repeat the submission sweep. The promised recovered state therefore does not hold at takeover commit.  
   **Fix:** recognize an obsolete operation epoch independently of claim replacement, or explicitly order/depend the sweeps and revise the independence claim. Test a non-exhausted operation in both orders.

4. **MAJOR — Structural repair shares the original eval step’s identity.**  
   **P:**989,991,1225. **S:**3552–3558,3567. **Code:** `llm/gateway.py:741`, `llm/spend_ledger.py:935–958`.  
   `(case, repetition, round, kind)` identifies both an invalid generator/critic call and its structural repair. Both can have multiple HTTP receipts. Recovery’s query can consequently return several different logical calls without a defined selection rule, and the step store cannot distinguish the original invocation from its repair.  
   **Fix:** persist an invocation/repair ordinal and logical-call identity before dispatch; associate transport/stage receipts with that invocation and specify deterministic receipt selection.

5. **MAJOR — Eval recovery omits completed calls without output.**  
   **P:**989,991. **S:**3835,3840–3847,4355. **Code:** [gateway.py:977](/home/user/job-hunter/backend/src/jobhunter/llm/gateway.py:977), [spend_ledger.py:945](/home/user/job-hunter/backend/src/jobhunter/llm/spend_ledger.py:945).  
   Refusal, invalid output, model mismatch and `max_tokens` responses are billed with `status='completed'`, `output_json=NULL`, and an error code. The new recovery rule covers completed output and error/ambiguous/cancelled statuses, leaving these common receipts undefined. A first-stage `max_tokens` crash additionally needs continuation at the enlarged stage, not a fresh first-stage payment.  
   **Fix:** replay completed error receipts through the same routing/error classification as a returned gateway call, including stage continuation. Add settlement-before-receipt crash cases for each class.

6. **MAJOR — Transport mismatch writes a nonexistent column.**  
   **P:**989,991. **S:**2625–2641. **Code:** `db/migrations/versions/0006_m2_llm_profile_settings.py:197–213`, `db/transitions.py:429–437`.  
   `eval_runs` has no `status_reason`, and its transition registry has `reason_col=None`. The prescribed mismatch failure therefore raises instead of durably recording the requested terminal outcome. The offline registry probe confirms this.  
   **Fix:** store the mismatch code in `metrics` while transitioning to `failed`, or explicitly amend the DDL, registry and spec to introduce a reason column.

7. **MAJOR — Candidate staging bypasses evaluation-policy serialization.**  
   **P:**989,1008. **S:**4898–4901. **Code:** [derive.py:87](/home/user/job-hunter/backend/src/jobhunter/settings/derive.py:87), [seed.py:203](/home/user/job-hunter/backend/src/jobhunter/db/seed.py:203).  
   `ensure_derived` relies on `active_config FOR UPDATE` to serialize `max(version_no)+1`. Two staged candidates—or staging concurrent with a save—can choose the same version number. The losing `ON CONFLICT DO NOTHING` inserts no matching hash, and its lookup fails. Content addressing alone does not prevent this.  
   **Fix:** specify the same serialization lock for both staging commands, or replace version allocation with a concurrency-safe mechanism. Test simultaneous different candidate policies.

8. **MAJOR — Repeated reclaim loses critic history across draft ancestry.**  
   **P:**525–526,529–530,623. **S:**3523,3554–3568,2282. **Code:** `llm/schemas/critic.py`; the proposed `resumed_from_attempt_id` chain at P:623.  
   Attempt A writes a draft and a revise review. Attempt B resumes it, writes a revised draft, then crashes before its critic. Attempt C selects B’s draft but counts notes only on B’s drafts: zero. A clean revision can now publish accepted without the mandatory follow-up critic, losing T0 and prior critic progress.  
   **Fix:** reconstruct the applicable resume chain across attempts within the tuple/evaluation-policy boundary, preserving critic progress separately from the current draft’s own review. Test two successive reclaims around revision commit.

9. **MAJOR — Resume routing omits a final-round rejected review.**  
   **P:**525,1221. **S:**3559–3561. **Code:** the persisted review outcomes represented by `llm/schemas/critic.py`.  
   The added routes handle accepted reviews and `revise|incomplete` when `round < 2`. A crash after a round-2 revise/incomplete review matches neither branch; it also is not a draft newer than every review. Recalling the critic or failing routing violates the existing contested-publication rule.  
   **Fix:** explicitly route a restored round-2 revise/incomplete review to contested publication, with no new call, and test that exact crash point.

10. **MAJOR — Durable correction history is disconnected from publication.**  
    **P:**358,528,565,599,1376. **S:**4367,4380. **Code:** `llm/schemas/generator.py`; the proposed `DraftValidation` interface at P:358.  
    Revalidation correctly produces no fresh V4 repair. History now lives in `validation.corrections`, but `publication.build` accepts `DraftValidation`, and no rule merges the persisted corrections into its repairs or `truncated_fields`. The new test checks retained draft JSON, not the published correction marker.  
    **Fix:** define reconstruction of publication validation from immutable normalization/correction history plus current findings. Assert identical `llm_results.truncated_fields` before and after reclaim.

11. **MAJOR — The permissive C-family discriminator can select the wrong structural mirror.**  
    **P:**308,1375. **S:**4355,4565. **Code:** `llm/gateway.py:984`, `llm/schemas/_base.py`.  
    Keeping property names while changing `criterion.level` to an unrestricted string still satisfies every proposed family check; the offline structural probe confirms this. The stored wire schema can therefore allow output rejected by the chosen C mirror. A successful sample dry-run does not prove structural equivalence.  
    **Fix:** compare canonical structural schemas while ignoring only explicitly harmless metadata/order differences, or persist a validated parser-family/version contract. Test changed types, required fields, enums and additional-property rules.

12. **MAJOR — Overlong quotes are not explicitly made grounding failures before publication.**  
    **P:**359–361,366,565. **S:**4337,4387. **Code:** `llm/schemas/_base.py`; the planned structural parser intentionally omits length limits.  
    A contiguous quote longer than 300 characters passes the listed grounding checks and receives only a V1 issue. If it survives review, publication drops or truncates it without the required V3 failure having participated in contested null-out. This can preserve a rating whose evidence must be discarded.  
    **Fix:** explicitly classify over-300-character quotes as criterion/fact grounding failures before routing and null-out. Test accepted-review and exhausted-revision paths.

13. **MAJOR — The manual eval workflow queues work without starting its consumer.**  
    **P:**989,1011. **S:**4611,5818–5828. **Code:** `.github/workflows/schema-dry-run-manual.yml:90–106`, `worker/operations.py:330–333`.  
    The eval CLI now inserts an operation and waits. The throwaway workflow prescribes PostgreSQL, migration, seeding and that CLI command, but no worker. Unlike the existing direct schema dry-run, the command cannot complete against this otherwise idle database.  
    **Fix:** start a live-configured worker against the throwaway database, wait for readiness, run the CLI, and stop the worker in unconditional cleanup.

14. **MAJOR — Representative changes still use the entering-deferral predicate.**  
    **P:**809. **S:**2874–2877,3261. **Code:** `dedup/groups.py:291–444`.  
    The same paragraph correctly distinguishes persistent `deferral_eligible` from `deferrable`, then says an already-deferred member remains deferred only while “deferrable” against its new representative. That function requires `pending|budget_deferred`, so every `deferred_duplicate` member fails it and is unnecessarily released for scoring.  
    **Fix:** use `deferral_eligible` in the representative-change handler and add a test that an otherwise eligible deferred member remains deferred.

15. **MINOR — Admission alerts require an attempt lane that is not stored.**  
    **P:**155,962. **S:**2268–2293,3661,3686–3687. **Code:** the M3 schema is introduced by Task 1; no existing attempt-lane storage supplies this field.  
    The exploration alert is defined using “the attempt’s lane,” but `score_attempts` has no lane column. The adjacent similarity alert instead reads mutable `score_targets.lane`, which can change on retry. Implementations therefore either reference a nonexistent field or misclassify historical admissions.  
    **Fix:** explicitly choose target-level semantics, or snapshot lane on attempts and amend the migration/spec and claim insertion accordingly.

16. **MAJOR — The revised cap fixture still predicts the wrong release count.**  
    **P:**458,704,715–737. **S:**1201,2297. **Code:** `llm/pricing.py:103`, `llm/bounds.py`, `llm/budget.py`.  
    After three settlements, the increased cap leaves approximately `2.5r` headroom, while release divides by `r_first`, not `r`. A read-only probe using the current Sonnet prices and an 8,000-token bound gives `r=$0.080000`, `r_first=$0.156100`; cent-rounded caps are `$2.29` and `$2.46`, leaving `$0.197000`, so **k=1**, not two. The late-settlement assertion has the same denominator problem.  
    **Fix:** select cent-representable caps from explicit inequalities involving both quantities, and assert the stored headroom and computed `k`.

17. **MINOR — Task 10 still directs evals into production persistence.**  
    **P:**558,565,602 versus 989. **S:**4611. **Code:** `db/transitions.py:522`.  
    The new pure builder is correct, but the file description, caller list and implementation step still say evals call `publish_result`, even suggesting an eval owner for its target predicate. Following that implementation step recreates the original targetless-eval defect.  
    **Fix:** remove evals from every production-persistence caller list and make Task 10’s implementation step explicitly preserve the two-layer contract.

18. **MINOR — Heartbeat API and Task 25 amendment contradict the corrected lifecycle.**  
    **P:**567,623,1220,1237. **S:**3572–3575. **Code:** `worker/heartbeat.py:33–51`, `worker/executions.py:154`.  
    The declaration remains `start(ref, task)` with direct cancellation, while callers use `start(ref)` followed by `attach`. Task 25 additionally prescribes that the wrapper starts the heartbeat, reintroducing the queued-claim gap into revision 3.5.  
    **Fix:** give `start`, `attach`, loss signaling and cleanup one consistent API, and amend §9.3 to start every heartbeat after claim commit.

19. **MINOR — The allocation concurrency test cannot reach its prescribed interleaving.**  
    **P:**846,850,897. **S:**1189–1191. **Code:** `llm/spend_ledger.py:506`, extended by P:453.  
    Preparation now holds both financial locks continuously from sizing through `reserve_batch`. A competing funded reservation cannot commit between those points. A test waiting for it will block rather than make preparation raise the expected allocation error.  
    **Fix:** test that the competitor waits and subsequently observes the updated allocation; separately test an insufficient allocation already present before preparation acquires its locks.

20. **MAJOR — Re-deferred batch operation jobs lose `lock="batch"`.**  
    **P:**22,751,762,854,1370. **S:**284,3939. **Code:** [operations.py:330](/home/user/job-hunter/backend/src/jobhunter/worker/operations.py:330), [operations.py:580](/home/user/job-hunter/backend/src/jobhunter/worker/operations.py:580).  
    The plan adds an optional `lock=None` parameter and passes `"batch"` at initial creation. `_redefer_lost_jobs` recreates jobs using only the operation ID, so a recovered batch operation defaults to no execution lock. Distinct batch operations can then run concurrently and race preparation, violating the stated worker serialization contract even though the submission singleton remains a final database guard.  
    **Fix:** derive the execution lock centrally from operation kind for every enqueue/re-enqueue path, and test recovery after a lost batch job.