# M3 plan — response to Codex plan review, round 3

Plan: `docs/superpowers/plans/2026-10-25-m3-agentic-scoring.md` (revised from 67befef; 1,433 lines after the revision). P: = line of the revised plan. Every claim was verified against the code and the spec before it was addressed (`worker/operations.py:287`, `:330`, `:357`, `:402–431`, `:580`; `settings/scope_apply.py:65`; `db/transitions.py:429–437`; `settings/derive.py:90–91`; `db/seed.py:212–218`; `.github/workflows/schema-dry-run-manual.yml:90–106`; S:2283, S:3559–3561, S:4337, S:4387). None was rebutted. Each fix was carried through every mention of its object (Global Constraints, the Interfaces blocks, the tests, Task 25's spec items, the Deviations, the Closing note) in one pass; Deviations 2, 17, 20, 26, 27, 28, 29, 35, 38, 40, 41 are amended, none added.

| Item | Severity | Status | Plan lines changed | What changed |
|---|---|---|---|---|
| A3 (→B11) | MAJOR | fixed | P:308, P:1384 (Dev 40) | Family = canonical structural equality after stripping only `description`/`title`/`examples`/`$comment` and normalising order; type, required, enum, `anyOf` and `additionalProperties` differences are `unsupported`; test over each class. |
| A12 (→B18) | MAJOR | fixed | P:567, P:1229 (Task 25 item 16), P:1364 (Dev 20) | One API: `start(ref)`, `attach(ref, child)`, `lost(ref)`, `stop`, `stop_all`; §9.3 amendment says every heartbeat starts at the claim commit, the wrapper attaches. |
| A18 (→B1, B2) | BLOCKER | fixed | P:86, P:859, P:862, P:906, P:1232, P:1239, P:1382 | Submission row inserted after the month and bulk rows (its FK key-shares the bulk row); `batch_submit` has no failure hook — recovery runs in fenced transactions (takeover sweep, re-run handler, `batch_submissions` tick reconciler). |
| A23 (→B3) | MAJOR | fixed | P:862, P:906, P:1232, P:1373 (Dev 27) | Recovery predicate also recognises an older `operations.worker_epoch`, so a dead epoch's rows are recovered whichever sweep runs first; both orders tested. |
| A30 (→B4, B5) | MAJOR | fixed | P:998, P:1000, P:1241 (Task 25 item 21), P:1373 (Dev 29) | Step key gains an invocation ordinal (original vs structural repair) and records `logical_call_no`; `eval_receipt` takes the step's newest call row and replays it through the gateway's classification (output reused; `invalid_output` → repair; `refused`; `max_tokens_first_stage` → enlarged stage under the same call; others fail; non-completed → re-run). |
| A42 (→B16) | MAJOR | fixed | P:721–746 | Caps chosen as cent-rounded values from explicit inequalities in both `r` (actual) and `r_first` (release denominator); the stored share/headroom and `k = headroom // r_first` are asserted before the release; the late-settlement test asserts `k` from the stored row. |
| A(r2 B3) (→B11) | MAJOR | fixed | P:308, P:1384 | Same fix as A3: structural equality, not property names, decides the parser family. |
| A(r2 B5) (→B18) | MINOR | fixed | P:567, P:1229, P:1364 | Same fix as A12: the API and Task 25's §9.3 text start heartbeats at the claim commit. |
| A(r2 B7) (→B1, B2) | BLOCKER | fixed | P:86, P:859, P:862, P:906, P:1232, P:1239, P:1382 | Same fix as A18: no implicit FK lock before the month row; no cleanup in a failure hook. |
| A(r2 B12) (→B3) | MAJOR | fixed | P:862, P:906, P:1232 | Same fix as A23: the epoch clause makes recovery sweep-order independent. |
| A(r2 B18) (→B17) | MINOR | fixed | P:558, P:602 | Task 10 no longer sends evals through `publish_result` anywhere. |
| A(r2 B19) (→B4, B5) | MAJOR | fixed | P:998, P:1000, P:1241 | Same fix as A30: invocation ordinal, receipt selection, error-receipt replay. |
| A(r2 B20) (→B6) | MAJOR | fixed | P:998, P:1000 | The mismatch reason lives in `metrics`; no nonexistent column. |
| A(r2 B21) (→B7) | MAJOR | fixed | P:998, P:1000, P:1017 | Staging takes the serialising `active_config FOR UPDATE`. |
| A(r2 B23) (→B8, B9) | MAJOR | fixed | P:525–526, P:599, P:623, P:1230, P:1361 | Chain-wide resume and the round-2 contested route. |
| A(r2 B26) (→B16) | MAJOR | fixed | P:721–746 | Same fix as A42: release counts from `r_first` and cent-rounded caps, asserted from stored rows. |
| B1 | BLOCKER | fixed | P:86, P:859, P:906, P:1232, P:1382 | As A18 (month/bulk before the owner-row insert; schedule sentence for created owner rows; real-FK deadlock test). |
| B2 | BLOCKER | fixed | P:86, P:862, P:1232, P:1239, P:1267, P:1382, P:1379 (Dev 35) | Failure hook removed for `batch_submit` (hooks run after the operation row is locked); `@reconciler("batch_submissions", "tick")` with the `scope_apply_failed` lock-free-read pattern; `M3_RECONCILERS` and the Closing note updated. |
| B3 | MAJOR | fixed | P:862, P:906, P:1232 | As A23. |
| B4 | MAJOR | fixed | P:998, P:1241 | As A30 (invocation ordinal; receipt = newest row of the step). |
| B5 | MAJOR | fixed | P:998, P:1000, P:1241 | As A30 (completed error receipts replayed by class, stage continuation; crash test per class). |
| B6 | MAJOR | fixed | P:998, P:1000 | `eval_runs` has no reason column (`reason_col=None`): the mismatch is recorded as `metrics.failed = "transport_mismatch"` on the `running → failed` transition. |
| B7 | MAJOR | fixed | P:998, P:1000, P:1017 | Staging runs `ensure_derived` under `active_config FOR UPDATE`, the lock that serialises `evaluation_policies.version_no`; concurrent-staging test. |
| B8 | MAJOR | fixed | P:526, P:599, P:623, P:1230, P:1361 (Dev 17) | Resume walks the whole attempt chain (`resumed_from_attempt_id`, set by the claim to the newest earlier attempt): newest draft, critic-round count over all drafts, last valid review; two-reclaims test. |
| B9 | MAJOR | fixed | P:525, P:599, P:1230, P:1361 | A restored round-2 `revise | incomplete` review routes to `publish(contested)` with no call (L3561); crash test at that point. |
| B10 | MAJOR | fixed | P:358, P:565, P:599, P:1386 (Dev 41) | `DraftValidation.history` (immutable normalisation + first corrections) feeds `publication.build`, whose `truncated_fields`/repairs are the union with publication-time repairs; test asserts identical `llm_results.truncated_fields` after reclaim. |
| B11 | MAJOR | fixed | P:308, P:1384 | As A3. |
| B12 | MAJOR | fixed | P:361, P:387, P:1370 (Dev 26) | A quote over 300 characters fails `ground_quote` (V3 failure of its criterion/fact, L4387) before routing and null-out; tests on the accepted-review and exhausted-revision paths. |
| B13 | MAJOR | fixed | P:1020, P:1373 | The workflow starts a live-configured `jobhunter worker`, waits on `worker_heartbeat`, runs the CLI, and stops the worker in the `if: always()` cleanup. |
| B14 | MAJOR | fixed | P:818, P:838 | The representative-change handler uses `deferral_eligible`; test that an eligible deferred member stays deferred. |
| B15 | MINOR | fixed | P:159, P:623, P:859, P:971, P:1222, P:1346 (Dev 2), P:1372 (Dev 28) | New `score_attempts.lane` (snapshot at claim and batch preparation); both §9.6.5 alerts read it; spec items for §7.11 and §9.6.5. |
| B16 | MAJOR | fixed | P:721–746 | As A42. |
| B17 | MINOR | fixed | P:558, P:602 | Task 10's file description, caller list and implementation step keep evals out of `publish_result`; `scoring/publication.py` named as the pure layer. |
| B18 | MINOR | fixed | P:567, P:1229, P:1364 | As A12. |
| B19 | MINOR | fixed | P:906 | The impossible interleaving test replaced by: the competitor waits and then sees the updated allocation; an allocation short before the prepare → `NotPrepared("no_allocation")` (the `reserve_batch` check is then a `Bug` guard). |
| B20 | MAJOR | fixed | P:133, P:760, P:854, P:906, P:1263, P:1379 (Dev 35) | `OPERATION_LOCKS` by kind, read by `defer_operation_job` on the first enqueue and on the `operations` reconciler's re-enqueue; the `lock=` parameter is gone from every mention; lost-batch-job test. |

Counts: 36 fixed (the 16 partially resolved round-2 items: A3, A12, A18, A23, A30, A42 and the round-2 B3, B5, B7, B12, B18, B19, B20, B21, B23, B26; and all 20 new findings), 0 rebutted. Plan: 1,433 lines (was 1,424).
