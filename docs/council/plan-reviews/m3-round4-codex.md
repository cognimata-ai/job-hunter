**Verdict: REJECT**

P refers to the [plan at `d2aa369`](/home/user/job-hunter/docs/superpowers/plans/2026-10-25-m3-agentic-scoring.md); S refers to the [revision 3.4 spec](/home/user/job-hunter/docs/superpowers/specs/2026-10-16-job-hunter-design.md), including the settled decisions and Task 25’s prescribed amendments. Code references are relative to `backend/src/jobhunter/`.

“RESOLVED” means adequately corrected in the plan, not implemented or tested. This review used offline, read-only inspection and pure local probes. No paid calls or restricted directories were accessed; the workspace remains unchanged.

## A. Disposition of all 36 round-3 items

The first sixteen identifiers retain the response document’s names. References to “new B” mean findings in section B below.

- **A3 — PARTIALLY RESOLVED:** Structural comparison replaces property-name matching, but recursive metadata removal still accepts incompatible schemas. P:308,1384; S:4355,4565. See new B3.
- **A12 — RESOLVED:** The API consistently separates `start(ref)` from `attach(ref, child)`, and Task 25 starts every heartbeat after claim commit. P:567,623,1229.
- **A18 — PARTIALLY RESOLVED:** Submission insertion and failure-hook recovery are corrected, but takeover recovery can still enter the lock schedule after operation locks. P:859,862,1278; code: `worker/epoch.py:164`. See new B1.
- **A23 — RESOLVED:** Recovery now recognizes an obsolete operation epoch independently of whether the operations sweep has cleared its claim; both orders are tested. P:862,906.
- **A30 — PARTIALLY RESOLVED:** Invocation identities and completed-error replay are added, but replay remains incomplete and differs from production routing. P:998–1000. See new B4–B7.
- **A42 — RESOLVED:** The fixtures use cent-rounded caps constrained by both actual `r` and release denominator `r_first`, and assert stored headroom and `k`. P:721–746.
- **A(r2 B3) — PARTIALLY RESOLVED:** Wire/model separation remains correct; the new structural canonicalizer still erases meaningful property names. P:308. See new B3.
- **A(r2 B5) — RESOLVED:** Claim-commit heartbeat startup is now consistent across the API, caller and spec amendment. P:567,623,1229.
- **A(r2 B7) — PARTIALLY RESOLVED:** The FK inversion and failure hook are fixed, but the global schedule still fails across takeover sweeps sharing one transaction. P:86,859,862,1239; code: `worker/epoch.py:152–164`. See new B1.
- **A(r2 B12) — RESOLVED:** The epoch predicate identifies a dead execution before or after the operations sweep. P:862,906.
- **A(r2 B18) — PARTIALLY RESOLVED:** P:558,602,998 correctly require pure eval publication, but P:565 still names the eval runner among `publish_result` callers. Remove that remaining caller-list entry.
- **A(r2 B19) — PARTIALLY RESOLVED:** Structural repairs have separate durable keys, but recovery still mishandles several receipt states and retry paths. P:998–1000. See new B4–B7.
- **A(r2 B20) — RESOLVED:** Transport mismatch is recorded in settable `metrics` while transitioning the run to `failed`, without a nonexistent reason column. P:998; code: `db/transitions.py:429–437`.
- **A(r2 B21) — RESOLVED:** Both candidate-staging paths acquire `active_config FOR UPDATE` around `ensure_derived`. P:998,1017; code: `settings/derive.py:90–91`.
- **A(r2 B23) — RESOLVED:** Resume follows attempt ancestry, restores critic progress and explicitly publishes contested after a final-round rejected review. P:525–526,599,623,1230.
- **A(r2 B26) — RESOLVED:** Release-count expectations now derive from stored headroom divided by `r_first`, with cent precision accounted for. P:721–746.
- **B1 — RESOLVED:** Preparation locks month and bulk before inserting the submission whose FK key-shares the bulk row; a real-FK concurrency test is prescribed. P:859,906,1239.
- **B2 — PARTIALLY RESOLVED:** The failure hook is removed, but the replacement takeover path retains the same configuration-after-operation inversion through the shared sweep transaction. P:862,1278; code: `worker/epoch.py:164`. P:1371 also retains the obsolete failure-hook wording. See new B1.
- **B3 — RESOLVED:** The added `worker_epoch >= :epoch` condition fixes the recovery predicate’s dependence on sweep results. P:862,906. Lock-order independence is a separate remaining defect.
- **B4 — RESOLVED:** Original and structural-repair invocations have distinct step keys, with persisted logical-call identity and deterministic receipt selection. P:998.
- **B5 — PARTIALLY RESOLVED:** Completed errors now have branches, but exhausted structural repair is routed incorrectly, and retry continuation remains incomplete. P:998. See new B4–B7.
- **B6 — RESOLVED:** The mismatch branch uses `metrics.failed`, consistent with the existing transition registry. P:998,1000; code: `db/transitions.py:429–437`.
- **B7 — RESOLVED:** Candidate staging explicitly uses the serializer lock required by evaluation-policy version allocation, with concurrent-staging coverage. P:998,1000,1017.
- **B8 — RESOLVED:** Resume reconstructs the whole linked attempt chain, including reviews preceding a newer revised draft; the two-reclaim scenario is tested. P:526,599,623.
- **B9 — RESOLVED:** A restored round-2 `revise | incomplete` review publishes contested without another call. P:525,599,1230.
- **B10 — RESOLVED:** `DraftValidation.history` reaches publication, which unions immutable correction history with publication-time repairs; the test checks the published truncation record. P:358,565,599.
- **B11 — PARTIALLY RESOLVED:** The listed type/enum/required-property mutations are rejected, but metadata-key collisions still bypass structural equality. P:308,1384. See new B3.
- **B12 — RESOLVED:** Quotes exceeding 300 characters explicitly fail grounding before routing/null-out, with accepted-review and exhausted-revision tests. P:361,387.
- **B13 — RESOLVED:** The manual workflow starts a live-configured worker, waits for readiness and stops it in unconditional cleanup. P:1020.
- **B14 — RESOLVED:** Representative changes use persistent `deferral_eligible`, with a test preserving eligible deferred members. P:818,838.
- **B15 — PARTIALLY RESOLVED:** Attempt-lane storage, writes and alert reads are specified, but its CHECK rejects the legitimate `always_include` lane. P:159,623,971,1222. See new B2.
- **B16 — RESOLVED:** Both cap fixtures now establish their expected release counts using `r_first` and stored, rounded values. P:721–746.
- **B17 — PARTIALLY RESOLVED:** The file description and implementation step are corrected, but P:565’s persistence caller list still includes the eval runner.
- **B18 — RESOLVED:** Heartbeat declarations, claim-time startup and Task 25’s lifecycle amendment agree. P:567,623,1229.
- **B19 — RESOLVED:** The concurrency test now expects the competitor to wait; insufficient allocation before preparation is tested separately. P:906.
- **B20 — RESOLVED:** `OPERATION_LOCKS` determines the lock on every enqueue/re-enqueue, including lost-job recovery. P:760,906,1278–1279.

## B. Newly identified defects and regressions

1. **BLOCKER — Takeover sweeps do not have independent transaction lock schedules.**

   **P:**86,862,906,1238–1239,1278. **S:**304–312. **Code:** [worker/epoch.py:132](/home/user/job-hunter/backend/src/jobhunter/worker/epoch.py:132), `worker/operations.py:285–287,558–576`.

   P:862 describes takeover recovery as a transaction that acquires configuration before any operation row. The existing framework instead runs **every sweep on the same connection inside one transaction**. When `operations` runs first, its `FOR UPDATE` locks remain held when batch recovery requests `active_config`.

   This recreates the inversion removed from the failure hook. An old handler can hold configuration while waiting for the operation; a queued configuration writer can block takeover’s later shared configuration request. Takeover can then fail on a lock timeout or deadlock. Testing only the final recovery predicate in both sweep orders does not establish concurrency safety.

   **Fix:** Acquire the required configuration prefix centrally before any takeover sweep takes operation/domain locks, then audit the combined transaction’s remaining lock order. Alternatively, explicitly redesign the recovery transaction boundary and guarantees. Test the complete takeover with both registration orders, an old handler and a concurrent configuration writer.

2. **MAJOR — The new attempt-lane CHECK makes valid admissions fail.**

   **P:**159,623,658–678,1222. **S:**2219–2222,3630–3635. **Code:** Task 1’s proposed migration and Task 11’s claim insertion.

   The CHECK admits `similarity`, `oldest`, `exploration`, `score_now`, `batch_followup` and `batch`, but omits `always_include`. The claim explicitly inserts `lane = a.lane`, and the admission test explicitly expects `Picked.lane == "always_include"`.

   A claim containing a watchlist, title-match or other always-include selection therefore violates the CHECK and rolls back the entire claim transaction. A read-only comparison of the declared lane sets confirms the mismatch.

   **Fix:** Include `always_include` in the migration and Task 25 amendment, preferably deriving the allowed values from one lane contract. Add a database-backed claim test containing an always-include admission.

3. **MAJOR — Recursive metadata stripping deletes meaningful schema property names.**

   **P:**308,1384. **S:**4355,4565. **Code:** `llm/schemas/_base.py:60`; proposed `schemas.structural`.

   Stripping keys named `title`, `description`, `examples` and `$comment` recursively also strips entries with those names inside `properties` and other name-to-schema maps.

   An offline probe added an optional `properties.title` string field to C while preserving its required list. The prescribed canonicalizer considered it equal to C, although the modified schema permits a field that C’s strict mirror rejects. The replacement discriminator therefore still fails its structural-equivalence promise.

   **Fix:** Traverse JSON Schema by context. Remove annotation keywords only from schema objects; preserve property/definition names and literal data. Add collision tests for these names inside `properties`, definitions and nested schemas.

4. **MAJOR — Eval recovery turns contested-publication paths into failures.**

   **P:**525,998–1000,1230. **S:**3557–3558,4551,4611. **Code:** `llm/gateway.py:1043–1044`; proposed shared routing.

   P:998 says any completed `invalid_output` receipt becomes a repair if available, otherwise the case becomes `failed`. That is correct for exhausted initial generation, but wrong for an exhausted critic repair and for the explicitly amended revision path: both publish contested using the validated draft.

   A crash after settling an invalid critic repair consequently changes a case from contested to failed solely because recovery ran. This also changes calibration results.

   **Fix:** Replay the recovered error through the shared router with the step kind, repair state and last validated draft. Add settlement-before-receipt crash tests for exhausted critic and revision repairs, asserting parity with uninterrupted execution.

5. **MAJOR — Eval recovery omits both “no receipt” and `dispatched` receipts.**

   **P:**452,456,998–1000. **S:**295,2969–2979. **Code:** `llm/spend_ledger.py:945–958,1097–1107`; `db/migrations/versions/0006_m2_llm_profile_settings.py:278–279`.

   The step store commits before dispatch, so a crash before reservation legitimately leaves `eval_receipt(...) == None`. A process kill during a request leaves the newest call `dispatched`, potentially for the ledger’s fifteen-minute/deadline grace period. Neither state has a recovery branch.

   The declared `Receipt` also lacks the status needed to distinguish the statuses P:998 discusses; Task 7 adds only `max_tokens`.

   **Fix:** Specify a complete recovery state machine and an appropriate typed receipt. Handle absent calls, never-dispatched reservations, in-flight calls and terminal receipts explicitly, with ownership and accounting rules for waiting/reconciliation/retry. Test crashes before reserve, after reserve and after the dispatch marker.

6. **MAJOR — Eval recovery retries definitive, non-retryable HTTP failures.**

   **P:**998. **S:**3832–3843,4611. **Code:** [llm/gateway.py:1072](/home/user/job-hunter/backend/src/jobhunter/llm/gateway.py:1072).

   The new rule reruns every newest receipt with `status='error'`. That includes HTTP 400/401/403/404/413/422, which the gateway settles as `error` and then raises as `LLMRequestError`.

   A crash after that settlement makes recovery issue another request where uninterrupted production execution would fail the case. Recovery is therefore neither classification-equivalent nor restricted to retryable failures.

   **Fix:** Classify error receipts by their persisted error/HTTP code. Preserve terminal request failures and apply bounded transport continuation only to retryable errors. Add crash tests for a definitive 400 and an authentication failure.

7. **MAJOR — Newest-receipt replay loses enlarged-stage progress and consumes extra logical-call numbers.**

   **P:**998–1000. **S:**3567,3832–3836. **Code:** `llm/gateway.py:741–789`; `db/migrations/versions/0006_m2_llm_profile_settings.py:259–261`.

   Consider a first-stage `max_tokens` response followed by an enlarged-stage try that becomes ambiguous. Recovery selects only the newest receipt and follows the generic rerun branch, allocating a new logical call without preserving the enlarged stage. It can pay for the first stage again, despite the plan’s explicit promise not to.

   The same rule treats transport recovery as a new semantic call. A case taking the permitted seven-call route plus one recovered ambiguous invocation needs an eighth logical-call number, which the retained database CHECK rejects. Restarting the gateway also resets transport-try accounting unless recovery supplies additional state.

   **Fix:** Persist or reconstruct stage, consumed transport tries and the applicable deadline/recovery boundary for each semantic invocation. Continue transport recovery under the correct invocation identity; define any new attempt boundary explicitly. Test an enlarged-stage failure across multiple reclaims and a maximal seven-call route with recovery.

8. **MAJOR — Month rollover can prevent scoring from creating the budget row it needs.**

   **P:**614,619–620,704–705,769–770,859. **S:**1172–1176. **Code:** [llm/spend_ledger.py:225](/home/user/job-hunter/backend/src/jobhunter/llm/spend_ledger.py:225), `:247–289,506–510`; `settings/save.py:232`.

   Admission, release and bulk preparation require the current month row, but the plan never specifies `ensure_month` for these entry points. Existing `lock_month` raises `LookupError` when the row is absent; `read_month` returns `None`. Row creation currently happens during reservation or a budget edit.

   After UTC month rollover, periodic admission can fail before any scoring call reaches reservation, leaving the system dependent on an unrelated call or settings edit to initialize its month. Score-now’s read-only precheck is also undefined for `None`.

   **Fix:** Ensure the current month before authoritative locking at these entry points, in schedule order. Use `read_month_or_new` for the read-only precheck. Test rollover with pending targets and no unrelated calls or budget edits.

9. **MAJOR — Batch eligibility admits targets that already have an online draft to resume.**

   **P:**526–530,566,623,858–859,906. **S:**1192,3523. **Code:** Proposed online draft store, transient-failure transition and `ELIGIBLE_SQL`.

   `resume_draft_id IS NULL` excludes batch follow-ups, but ordinary online resumption discovers drafts through prior attempts; it does not require that target pointer.

   An online attempt can persist generation, encounter a transient critic failure and return to pending. Once its backoff expires, it satisfies the batch predicate: no resume pointer, no earlier batch item, an ordinary lane and fewer than four charged attempts. A later batch submission then pays for generation again rather than resuming the existing draft.

   **Fix:** Express “still needs its initial generator” using the same resumable-artifact rules as `load_context`, including online attempt history. Add an eligibility test after successful online generation followed by a critic failure.

10. **MAJOR — Calibration does not separate primary case metrics from selected retest repetitions.**

    **P:**998,1015–1016. **S:**4613–4626. **Code:** Proposed `calibration.metrics` and `CaseResult.repetition`.

    The runner supplies first repetitions plus an adaptively selected subset of second repetitions. The metrics contract does not select one observation per case for primary metrics; it explicitly describes schema validity over “case results” and leaves the other pooled metrics similarly unpartitioned.

    Pooling these rows weights retested cases twice. For example, five contested cases among thirty published cases fail at 16.67%; adding ten accepted retests changes the apparent rate to 12.5% and passes. Correlation, pairwise ordering and per-profile reporting can also change merely because different cases were retested.

    **Fix:** Define the observation set for each metric. Use one declared observation per active case—normally repetition 1—for primary gates, and use paired repetitions for test–retest statistics. Specify cost reporting separately. Test that retest selection cannot dilute primary gate failures.

11. **MAJOR — Retention can destroy unresolved batch settlement data while retaining its submission.**

    **P:**452,925,1131–1135. **S:**2973–2979,6304,6369–6378. **Code:** `llm/spend_ledger.py:375–379,1097–1107`.

    Score-history retention excludes active tuples and golden-labelled results, but not targets belonging to unresolved batch submissions. Tuple supersession can make those targets terminal while their submission remains `submit_ambiguous` indefinitely.

    After the retention window, target-set deletion removes their `batch_items` and `llm_calls`. The submission survives, but release and reconciliation lose the item-to-ledger mapping. Existing settlement lookup joins through `llm_calls`; batch entries are exempt from timeout recovery. Keeping the submission metadata alone does not preserve recoverability.

    **Fix:** Exempt targets/items needed by unresolved submissions from age deletion, or explicitly design a durable, content-free settlement mapping that survives deletion. Reconcile this interaction in Task 25 without introducing a timeout-based financial release contrary to C3. Test an old, superseded, unreleased ambiguous submission through retention and subsequent release/reconciliation.

12. **MINOR — The global admission rule still contradicts the settled floor limit.**

    **P:**72 versus 658,682,1220. **S:**3630–3635, as amended by Task 25. **Code:** Proposed `ALWAYS_INCLUDE_LIMIT`.

    The global constraints still prescribe `ceil(0.5 × S)`, while the implementation contract, property test and spec amendment prescribe `S // 2`. With `S=3` and two similarity slots, the global rule permits two always-include admissions where the settled rule permits one.

    **Fix:** Replace the global formula with `min(slots["similarity"], S // 2)` so all implementation instructions state the settled rule.