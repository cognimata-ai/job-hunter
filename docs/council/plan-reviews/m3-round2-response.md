# M3 plan — response to Codex plan review, round 2

Plan: `docs/superpowers/plans/2026-10-25-m3-agentic-scoring.md` (revised from efe9566; 1,424 lines after the revision). P: = line of the revised plan. Every claim was verified against the code and the spec before it was addressed (`db/sql.py:25`, `gateway.py:357–376`/`:809`/`:999`, `schema_dry_run.py:111–184`, `spend_ledger.py:608–632`, `settings/save.py:334–388`, `settings/model.py:400–407`, `fixtures.py:52`, `fold("Französisch") == "franzosisch"`, S:3521–3560, S:4534–4537). None was rebutted. Deviations 2, 11, 17, 20, 27, 29, 36, 38, 40, 41 are amended; no new deviation was needed.

| Item | Severity | Status | Plan lines changed | What changed |
|---|---|---|---|---|
| A1 (→B1) | BLOCKER | fixed | P:149, P:1371 (Dev 36) | `operations` leaves the truncate set too (its cascade reaches `active_config` through `batch_submissions.operation_id`); deleted with the M3 tables; the closure test runs on the fully migrated FK graph. |
| A3 (→B3) | MAJOR | fixed | P:308, P:1375 (Dev 40) | Family discriminator (`generator_family`: C's required keys and `$defs` shapes, v1's objects, else `unsupported`); `WireSchema(json, model)` as the gateway's two inputs (stored wire schema sent byte for byte, mirror parses); the two-version test uses two family-`c` schemas with different descriptions. |
| A12 (→B5, B6) | BLOCKER | fixed | P:567, P:599, P:621, P:623, P:1355 (Dev 20) | Heartbeats start for every claim right after the claim commit (cycle-owned); the wrapper runs the subgraph in an owned child task, attaches it, and turns ownership loss into a `claim_lost` outcome instead of a graph error. |
| A15 (→B2) | MAJOR | fixed | P:231, P:623, P:1346 (Dev 11) | All array casts written `CAST(:name AS type)`; `_validate` refuses `::` after a bind; `test_transition_where_binds` asserts the compiled bind set. |
| A18 (→B7, B8) | BLOCKER | fixed | P:86, P:565–566, P:851–853, P:897, P:900, P:916, P:1050, P:1223, P:1373 (Dev 38) | Every batch transaction takes `active_config` before the operation and submission owners; ranking last everywhere (save/confirm bump after `activate_tuple`, publish releases the hold before its inserts); prepare: CAS with `SKIP LOCKED` before dependent rows, the schedule's one stated exception. |
| A23 (→B12) | BLOCKER | fixed | P:159, P:850, P:853, P:1337 (Dev 27) | `batch_submissions.operation_claim_id` stores the preparing claim; the recovery predicate compares it with the operation's current claim; `prepare`, `submit_start` and the recordings lock the operation owner; both sweep orders tested. |
| A25 (→B24) | MAJOR | fixed | P:1008, P:1234 (Task 25 item 30) | `schema_verified` requires a passed live case for all five (schema, model) pairs of `_SCHEMA_NODES` × `ModelPolicyNodes.by_node()`, including `extract_profile` and `propose_queries`. |
| A30 (→B19) | MAJOR | fixed | P:989, P:991 (Dev 29) | Step identity (`input_refs["eval_step"]`) persisted before dispatch; a `dispatched` step is completed from its call row on resume (settled call never repeated); test at the point right after settlement. |
| A35 (→B7) | MAJOR | fixed | P:788, P:1050 | `save_settings`: `… → _FLIP → activate_tuple → ranking.bump`; the confirm likewise bumps after the activation. |
| A42 (→B26) | MAJOR | fixed | P:460, P:715–735 | Identical fixtures; `gateway.reservation_for` gives the real reservation; bucket/admission seeded; dispatch order pinned; ledger reservations asserted equal to `r`; the late-settlement release kept as a separate test. |
| B1 | BLOCKER | fixed | P:149, P:1371 | As A1. |
| B2 | MAJOR | fixed | P:231, P:623, P:1346 | As A15. |
| B3 | MAJOR | fixed | P:308, P:1375 | As A3. |
| B4 | MAJOR | fixed | P:314, P:1234 (Dev 5 via Task 25 item 30) | Probe-only schema when the generator schema has no plain integer node (C's `level` is an `anyOf` enum); the keyword question does not depend on the generator schema; offline probe-body test. |
| B5 | BLOCKER | fixed | P:567, P:599, P:623, P:1355 | Cycle-owned heartbeat per claim from the claim commit; sixty-claims-three-blocked test. |
| B6 | MAJOR | fixed | P:567, P:599, P:621, P:1355 | Owned child task with an explicit loss signal; `claim_lost` returned; cycle shutdown propagates separately; siblings and the cycle complete. |
| B7 | BLOCKER | fixed | P:86, P:565–566, P:788, P:851–853, P:916, P:1050, P:1082, P:1223, P:1373 | Recording/abandon/release/recovery transactions rewritten in schedule order; attempts moved only while `running`; ranking last; hold release at the bulk row before inserts; the API month-lock sentence corrected (batch release, budget save). |
| B8 | BLOCKER | fixed | P:86, P:897, P:900, P:1223, P:1373 | CAS (`FOR UPDATE SKIP LOCKED`) advances `attempt_no` before any attempt/item row exists; it never waits for a target, so the month→targets order forms no cycle; online-claim-between test. |
| B9 | MAJOR | fixed | P:850, P:897 | Minimum re-applied to the claimed set; `m < min` (incl. 0) rolls the whole preparation back (`NotPrepared`); zero and min−1 survivors tested. |
| B10 | MAJOR | fixed | P:846, P:850, P:897, P:1362 | Sizing under the month and bulk locks; `reserve_batch` re-checks `spent + held + Σ worst + n × hold ≤ budget` under the locks and raises; test with an intervening funded follow-up. |
| B11 | MAJOR | fixed | P:454, P:897 | `_release` credits the bulk debit (`reserved_usd`) once under the bulk row for funded entries; abandon-credits test. |
| B12 | BLOCKER | fixed | P:159, P:850, P:853, P:897, P:1337 | As A23. |
| B13 | MAJOR | fixed | P:852–853, P:916 | `transition_where(["running"], …)` for every cleanup (rejection, abandon, release, missing); an existing terminal outcome is kept; accounting independent of target state. |
| B14 | MAJOR | fixed | P:913 | Missing items go through `process_item` as a synthetic `missing` result: fence, attempt disposition, once-only hold release, ranking. |
| B15 | MAJOR | fixed | P:159, P:854, P:915, P:1223, P:1337 | `follow_on(submission_id)` keyed by the originating submission, once-only through `follow_on_at`, never for an adopted released row; dedup key per originating submission. |
| B16 | MAJOR | fixed | P:528, P:599, P:1376 (Dev 41) | `validation` = immutable `normalisation` + `corrections` (first write only) + rewritable `current`; history-survives test. |
| B17 | MAJOR | fixed | P:159, P:407, P:565, P:599, P:1337, P:1352 | `critic_injection = 'unavailable'` for "critic ran, no valid review"; confidence reads it as not `none` (`low` when injection-suspected); contested-after-two-invalid-critic-outputs test. |
| B18 | MAJOR | fixed | P:565, P:989 | Pure `publication.build` (null-out, repairs, strict model, V6 guard, aggregation) factored from `publish_result`; the eval runner uses only the pure layer. |
| B19 | MAJOR | fixed | P:989, P:991 | As A30. |
| B20 | BLOCKER | fixed | P:989, P:991, P:1008 | `metrics.transport_requested` persisted at creation; the handler fails `transport_mismatch` before any call; `metrics.transport` records the served transport; the gate requires live provenance except in a fake-mode process. |
| B21 | MAJOR | fixed | P:989, P:991, P:1008 | `--candidate-payload` for the dry-run and the eval CLI stages the candidate rows through `ensure_derived` (content-addressed, no activation); the later save finds them by hash. |
| B22 | MAJOR | fixed | P:989, P:991, P:1006, P:1017–1019 | Deterministic adaptive second repetitions in case-id order (≤ 20 attempts) after every first repetition; the sample is read from the results; resume continues the order. |
| B23 | MAJOR | fixed | P:525–526, P:599, P:1221, P:1352 (Dev 17) | `load_context` restores the last valid note, its derived outcome, the confirmations and the verification; `validate` after a resume routes by that note (no repeated critic); spec L3523/L3548 reconciled in Task 25; two crash tests. |
| B24 | MAJOR | fixed | P:1008, P:1234 | As A25. |
| B25 | MINOR | fixed | P:287, P:644, P:774, P:1270, P:1289 | `score_cycle(trigger)` in Task 27; smoke expects `queued = true`; the wrapper is the registered node; the activation test parametrises the single in-flight submission's phase; `franzosisch`. |
| B26 | MAJOR | fixed | P:460, P:715–735 | As A42. |

Counts: 36 fixed (10 partial round-1 items, 26 new findings), 0 rebutted. Plan: 1,424 lines (was 1,418).
