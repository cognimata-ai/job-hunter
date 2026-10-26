**Verdict: REJECT**

P refers to the [plan at `c5e5e22`](/home/user/job-hunter/docs/superpowers/plans/2026-10-25-m3-agentic-scoring.md); S refers to the [revision 3.4 spec](/home/user/job-hunter/docs/superpowers/specs/2026-10-16-job-hunter-design.md), including settled rulings and the prescribed amendments. Code paths below are relative to `backend/src/jobhunter/`.

The three authorised simplifications are accepted. “RESOLVED” means corrected in the plan, not implemented or tested. Inspection and probes were offline and read-only; no paid calls or restricted directories were accessed. The workspace remains unchanged.

## A. Round-4 dispositions

The first table preserves identifiers from **round 4’s section A**. Grouped identifiers each receive the stated disposition.

| Round-4 A identifiers | Disposition | Evidence |
|---|---|---|
| A3; A(r2 B3); B11 | **RESOLVED** | The structural canonicaliser is withdrawn. The seed supplies `parser_family`, and rendering uses stored wire schemas separately from parser mirrors. P:159,308,357,1396. Interface leftovers are new B6–B7 below. |
| A12; A(r2 B5); B18 | **RESOLVED** | `start(ref)` follows claim commit; `attach(ref, child)` belongs to the wrapper. P:567,621,623,1241. |
| A18; A(r2 B7); B2 | **RESOLVED** | The shared takeover transaction is removed. Each sweep acquires its configuration prefix independently; batch recovery is not a failure hook. P:86,868,1284,1290,1394. |
| A23; A(r2 B12); B3 | **RESOLVED** | Recovery recognises an older operation epoch before or after the operations sweep. P:868,912. |
| A30; A(r2 B19); B4; B5 | **RESOLVED** | Receipt replay and durable intermediate eval steps are withdrawn. A touched, interrupted run fails without further calls. P:517,1004,1246,1385. The completion boundary and obsolete test remain separate defects below. |
| A42; A(r2 B26); B16 | **RESOLVED** | Fixtures distinguish actual reservation `r` from release denominator `r_first`, account for cent rounding, and assert stored headroom. P:723–752. |
| A(r2 B18); B17 | **RESOLVED** | The persistence caller list now explicitly excludes eval. Eval calls pure `publication.build`. P:558,565,602,1004. |
| A(r2 B20); B6 | **RESOLVED** | Transport mismatch uses settable `metrics`, not a nonexistent reason column. P:1004,1006; code: `db/transitions.py:429–437`. |
| A(r2 B21); B7 | **RESOLVED** | Both candidate-staging paths hold `active_config FOR UPDATE` around `ensure_derived`. P:1004,1023. |
| A(r2 B23); B8; B9 | **RESOLVED** | Online recovery restores attempt ancestry and critic progress; a final-round rejected review publishes contested without another call. P:525–526,599,623,1242. |
| B1 | **RESOLVED** | Preparation locks month and bulk before inserting the submission with its immediate bulk FK. P:865,912,915. |
| B10 | **RESOLVED** | Immutable correction history reaches publication and is combined with publication-time repairs. P:358,528,565,599. |
| B12 | **RESOLVED** | Quotes longer than 300 characters fail grounding before routing and contested null-out. P:361,387. |
| B13 | **RESOLVED** | The manual workflow starts a live worker, awaits readiness and cleans it up unconditionally. P:1026. |
| B14 | **RESOLVED** | Representative changes use persistent `deferral_eligible`, not the entering-state predicate. P:824,844. |
| B15 | **RESOLVED** | `always_include` is admitted by both lane CHECKs, with a real claim test. P:159,639–644,664,1234. |
| B19 | **RESOLVED** | The competing reservation waits for preparation’s locks; insufficient allocation is tested separately. P:912. |
| B20 | **RESOLVED** | `OPERATION_LOCKS` applies to initial enqueue and lost-job re-enqueue. P:766,912,1284. |

Disposition of **round 4’s section B**:

| Item | Disposition | Evidence |
|---|---|---|
| B1 — shared takeover transaction | **RESOLVED** | Independent fenced sweep transactions, timeout preservation and failure semantics are specified. P:86,626,868,1284,1294. |
| B2 — missing admission lane | **RESOLVED** | Seven-value lane contract includes `always_include`. P:159,639–644,664. |
| B3 — recursive metadata stripping | **RESOLVED** | Stored parser-family constant replaces inference entirely. P:308,1396. |
| B4 — incorrect recovered-error routing | **RESOLVED** | No recovery routing remains; the sole execution uses production routing. P:1004. |
| B5 — missing receipt states | **RESOLVED** | Any persisted call or case result triggers interruption failure; no receipt-state classification is needed. P:1004. |
| B6 — retrying definitive HTTP failures | **RESOLVED** | Reclaimed executions issue no calls for touched runs. P:1004,1006. |
| B7 — lost stage/logical-call progress | **RESOLVED** | Interrupted runs are terminated rather than reconstructed. P:1004,1246. |
| B8 — missing month initialization | **RESOLVED** | Entry points ensure the month; score-now uses `read_month_or_new`. P:619–620,710–711,775,865. A separate alert-lifecycle problem remains in new B4. |
| B9 — batching an existing online draft | **PARTIALLY RESOLVED** | The intended exclusion and test are correct, but the shared SQL fragment is not correlated when embedded in batch eligibility. P:526,864,912. See new B1. |
| B10 — retests dilute primary metrics | **RESOLVED** | Primary, paired and cost observation sets are explicit, with a dilution regression test. P:1021,1044–1048,1246. |
| B11 — retention destroys unresolved settlement mapping | **RESOLVED** | Unresolved submissions protect their targets, items and calls. P:1144,1247. The released-window test has a separate inconsistency, new B9. |
| B12 — ceil/floor contradiction | **RESOLVED** | The global constraint now uses the settled floor. P:72,664,1232. |

## B. New defects and regressions

1. **MAJOR — The shared resumable-draft predicate cannot be embedded as specified.**

   **P:**526,864–865,912. **S:**1192,3523,3939. **Code:** proposed `scoring/targets.py::RESUMABLE_DRAFT_SQL` and `scoring/batches.py::ELIGIBLE_SQL`.

   The shared fragment contains `a.target_id = :t`. Batch eligibility embeds it in a set query over outer alias `t`, specifying only how `:policy` is supplied. It never changes `:t` into `t.id`; `prepare` has no scalar target-id argument. The offline extraction confirms that the fragment requires both `policy` and `t` binds and contains no outer-row correlation.

   **Failure scenario:** preparation either fails on the missing bind or tests every candidate against one supplied target’s history. The latter can exclude fresh targets or batch targets that already have drafts.

   **Fix:** define an explicitly correlated predicate using `a.target_id = t.id`, with a compatible single-target wrapper for `load_context`. Test one candidate set containing both resumable and fresh targets.

2. **MAJOR — Eval recovery does not handle a run whose terminal commit already succeeded.**

   **P:**1004,1006,1290. **S:**2640, §3.3.3, amended §11.13. **Code:** [worker/operations.py:182](/home/user/job-hunter/backend/src/jobhunter/worker/operations.py:182), `:692–701`; `db/transitions.py:429–437`.

   The run completes in a domain transaction, but the plan does not require its operation to finish atomically with that transaction. The default framework finishes the operation separately. The restarted handler’s touched-run branch then prescribes `running → failed` without first handling an already `completed` or `failed` run.

   **Failure scenario:** the worker dies after committing the completed run but before finishing its operation. Re-execution finds call/result rows and attempts an illegal transition. Depending on the run-lock predicate, it raises `TransitionConflict` or `FencedOut`, leaving the operation to repeated recovery. The failure hook also needs terminal-state tolerance.

   **Fix:** commit the run’s terminal state and `op.finish_in(..., succeeded)` together. Additionally, make handler entry and the failure hook preserve already-terminal runs. Test crashes immediately before and after that commit.

3. **MAJOR — The expanded refilter hook is unsafe for scope precomputation’s existing caller.**

   **P:**821–822,847. **S:**312,4908–4920. **Code:** [settings/scope_apply.py:229](/home/user/job-hunter/backend/src/jobhunter/settings/scope_apply.py:229), [scoring/filters.py:466](/home/user/job-hunter/backend/src/jobhunter/scoring/filters.py:466).

   Task 15 makes `refilter_job` perform active-tuple deterministic recomputation and live target moves. Existing scope precomputation calls that same function through `recompute_stale`, with **only the pending scope**, after locking jobs and the operation, and explicitly without `active_config`. P:821 adds the configuration lock to the new det-result chunks, but does not repair this existing hard-filter path.

   **Failure scenario:** implementing the new live hook with its required configuration lock acquires configuration after jobs/operation, recreating an inversion against takeover or configuration writers. Applying the supplied pending-scope filters directly instead can move live targets before the final scope flip.

   **Fix:** separate pending-scope precomputation from live mutation hooks. Keep target moves at the final flip; make any active-state recomputation acquire its configuration prefix before jobs and owners. Add coverage for the existing hard-filter chunk during a concurrent save/takeover.

4. **MAJOR — Month-specific cap alerts can keep the next month paused.**

   **P:**457,617,710,1375. **S:**1199–1201. **Code:** proposed `spend_ledger.resolve_cap_reached`; [observability/alerts.py:108](/home/user/job-hunter/backend/src/jobhunter/observability/alerts.py:108).

   Raising is keyed by month, and `resolve_cap_reached(conn, month)` clears that month’s marker and resolves its alert. Admission, however, pauses for any open `llm.cap_reached`, without a current-month restriction.

   **Failure scenario:** October’s alert remains open at rollover. November’s release pass initializes November and resolves November’s subject, while October still blocks every cycle and batch. Acknowledgement does not lift this pause. Separately, clearing the timestamp on resolution allows a second alert in the same month, contradicting the once-per-month contract.

   **Fix:** make pause evaluation explicitly current-month scoped, define resolution of obsolete monthly alerts, and retain a separate “already alerted this month” marker. Test rollover with the previous month’s alert open, plus denial → recovery → denial within one month.

5. **MAJOR — Ambiguous-batch adoption omits the required completion marker.**

   **P:**227,928,930. **S:**2543,3960,3987–3999. **Code:** proposed `scoring/batches.py::reconcile_ambiguous`; Task 1’s copied submission CHECK.

   The ordinary poll path explicitly writes `results_complete_at`. The adoption path downloads the complete stream, adopts the batch, processes items and transitions `ended → reconciled`, but never specifies that write. The copied DDL rejects `reconciled` without it.

   **Failure scenario:** a successfully matched ambiguous or released submission reaches its final transition and rolls it back on the CHECK. Recovery reports an error despite complete provider evidence.

   **Fix:** use one completion finalizer for polling and adoption: verify complete coverage, write `results_complete_at`, then reconcile. Test the adoption path through its final commit, without relying on a later ordinary poll to repair it.

6. **MINOR — Parser-family replacement left incompatible interface names.**

   **P:**304,308,357,929. **S:**4355 and the parser-family amendment. **Code:** proposed `schemas.mirror_for` and `scoring/batches.py::process_item`.

   `mirror_for("generator_v2_items")` names `GeneratorC`, while the declared class is `GeneratorWire`. Batch processing still calls `parse_generator(..., kind=...)`, although the new signature accepts keyword-only `family`.

   **Failure scenario:** following these references produces an undefined class or a `TypeError` on valid batch outputs. The keyword mismatch reproduces in a pure Python probe.

   **Fix:** use `GeneratorWire` and `family=version.parser_family` consistently; assert the batch path uses the stored family.

7. **MINOR — The parser-family mutation test contradicts the cache contract.**

   **P:**308. **S:**4565 and Deviation 40. **Code:** proposed `render.load_prompt_version`.

   Versions are cached by `(id, hash)`, but the test updates only `parser_family` and expects the same row to become unscorable. Neither cache-key component changes.

   **Failure scenario:** the test reloads the cached v2 object and fails, despite the production contract that only the seed writes immutable version rows.

   **Fix:** explicitly clear the cache for this database-mutation test, or use separately inserted rows to test both families. Do not imply that an unchanged cache key refreshes itself.

8. **MINOR — One eval test still requires the withdrawn resume design.**

   **P:**1006 versus 1004,1246,1385. **S:**amended §11.13. **Code:** proposed `test_second_repetitions_follow_case_id_order`.

   The test still says that a resume after the sixth second repetition continues at the seventh.

   **Failure scenario:** an implementation following the authorised interruption rule cannot satisfy this assertion; implementing the assertion would restore forbidden resume behaviour.

   **Fix:** keep the ordering assertion for uninterrupted execution. After interruption, assert `failed("interrupted")`, no additional calls, and a new run starting afresh.

9. **MINOR — Released-submission retention uses two different clocks and an impossible test expectation.**

   **P:**930,1144,1147,1383. **S:**3984–3999, amended §20.1. **Code:** proposed `UNRESOLVED_SUBMISSION_SQL` and `test_retention_m3.py`.

   Reconciliation’s settled 29-day window starts at `submit_started_at`; the new retention predicate starts at `released_at`. P:1147 nevertheless expects the next retention pass to delete targets immediately after releasing a 14-month-old submission, which the new predicate protects for another 29 days. Its wording that release “credits every item” also conflicts with release retaining counted ambiguous spend.

   **Failure scenario:** the prescribed regression test fails against the prescribed SQL and can encourage an incorrect financial assertion.

   **Fix:** choose and document the retention horizon explicitly, align the test with it, and assert `dispatched → ambiguous` with counted spend unchanged.

10. **MINOR — The takeover regression test expects the wrong submission state for its stated pause point.**

    **P:**868,1294. **S:**3956–3957. **Code:** proposed `test_each_takeover_sweep_is_its_own_fenced_transaction`.

    The fixture describes an old handler paused inside `prepare`, but expects its lost submission to become `submit_ambiguous`. A lost `preparing` submission must become `abandoned`; ambiguity requires a committed `submit_started`.

    **Failure scenario:** the test either asserts the wrong recovery state or becomes dependent on an uncontrolled race in which the old handler reaches `submit_start`.

    **Fix:** separate the lock-order fixture from the lifecycle assertion. Explicitly establish `preparing` and `submit_started` cases and assert `abandoned` and `submit_ambiguous`, respectively.