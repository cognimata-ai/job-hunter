**Verdict: REJECT.** Revision 3 resolves most round-2 findings, but the revised bootstrap order and manifest replay contract introduce blockers. Four additional major defects remain.

Reviewed `adc1335` against `7c06f2a`, using the round-2 review as the checklist. References below are to the [revision-3 specification](/home/user/job-hunter/docs/superpowers/specs/2026-10-16-job-hunter-design.md). This was a static review plus an executable arithmetic check of the admission algorithm; migrations and services were not executed. No files were modified.

**C1–C3**

- **C1: accepted under the binding decision.** S3, §6.9.1, §10.7 and V19 disclose the tokenizer assumption, specify byte-based text reservations and measured overhead, and withhold the buffer from admission. This accepts the specified empirical assurance; it does not claim V19 has already passed.
- **C2: consistently implemented and disclosed.** Exclusive account use is explicit; foreign-usage detection, conservative admission and the reduced Q-SH1 assertion agree.
- **C3: consistently disclosed, with an implementation conflict below.** Empty listings no longer release spend. User release preserves ambiguous accounting. The new `released → ended` transition conflicts with the single-flight index when another submission exists.

**Disposition of every still-open round-2 item**

“RESOLVED” below addresses the original finding; separate revision-3 regressions are identified where applicable.

| Item | Status | Evidence / remaining issue |
|---|---|---|
| #2 | **RESOLVED** | Under C1: S3, §6.9.1, §10.7 and V19 replace the unsupported unconditional reservation claim. |
| #3 | **RESOLVED** | §3.3.3 and §7.3 require a fresh operation `claim_id`, checked by reservation, dispatch, heartbeat and domain writes. |
| #4 | **RESOLVED** | C2, §6.5 and Q-SH1 explicitly condition the guarantee on exclusive use. |
| #7 | **RESOLVED** | §§8.6, 9.2 and 13.4 require collection input writers to hold `active_config FOR SHARE` and commit active/pending-scope projections atomically. |
| #8 | **RESOLVED** | §20.2 gives proposals a legal `purged` state; §20.4 orders batch children before parents and clears active profile/tuple references before deletion. |
| #9 | **RESOLVED** | §7.14 and §19.5 replace high-water coverage with exact manifest UUID/hash shipment membership. |
| #11 | **RESOLVED** | §9.6.2 gives safeguard lanes credits based on all `S` slots, including `S=1`; always-include uses similarity slots. The replacement algorithm introduces **R3-3** below. |
| #17 | **RESOLVED** | §8.11 explicitly confirms mass events using structurally complete snapshots, including `mass_disappearance`. Align §17.4’s remaining “complete snapshots” shorthand during implementation. |
| #21 | **RESOLVED** | §8.6 now decides/inserts the job before the posting, and keys before provenance. |
| #26 | **RESOLVED** | §§11.8 and 13.1 require `radius_km ≥ 1`. |
| #29 | **RESOLVED** | §11.6 defines generic limit repairs and explicitly includes requirement text and the other limited strings. |
| #32 | **RESOLVED** | §§9.7, 10.3 and 13.3 consistently select per-attempt execution configuration. |
| #33 | **RESOLVED** | §17.4 includes hard-filter algorithm-version mismatch and refiltering; §7.11 corrects deterministic-instance replacement order. |
| #34 | **RESOLVED** | §§9.4.2 and 10.2 increment logical and physical counters atomically at first dispatch; denied steps increment neither. |
| #35 | **RESOLVED** | §6.9.3 explicitly defines 40% as a batch allocation, distinguishes expected follow-up holds from hard-cap reservations, and discloses overflow. |
| #36 | **RESOLVED** | §§7.16.3 and 10.10 add revision, merge and purge cancellation transitions for `in_batch` targets. |
| #37 | **RESOLVED** | C3 and §10.10 remove automatic absence settlement. See **R3-2** for the new release-recovery conflict. |
| #38 | **RESOLVED** | §§20.1–20.2 assign auxiliary/evaluation-call retention ownership and preserve profiles referenced by feedback. |
| #39 | **NOT RESOLVED** | Tainted-revision eval calls now cascade correctly, but newly frozen deterministic snapshots escape source purge when only their salary is tainted. **R3-4**. |
| #41 | **RESOLVED** | §§19.3–19.5 specify privileged extension provisioning, filtered `--no-owner --no-acl` restore and inventory from the dump’s snapshot. New bootstrap/replay blockers remain: **R3-1, R3-5**. |
| #44 | **RESOLVED** | §7.13 adds `det_inputs_snapshot`; §11.13 uses it and defines reproducible evaluation T8 identity. |
| #46 | **RESOLVED** | §7.1 expands generation bumps to list membership/filter changes; §14.3 reads generation and rows in one repeatable-read snapshot. |
| #48 | **RESOLVED** | Status is “under final review”; §§7.16.8 and 20.4 define operation cancellation and cycle failure mappings. |
| #49 | **RESOLVED** | §21 corrects counterfactuals, incremental cache costs, cycle counts, cold-batch costs and comparison denominators. Independently recalculated central capacity: approximately **1,617**, consistent with “≈1,620.” |
| N1 | **RESOLVED** | Fresh operation claim UUIDs eliminate the same-worker-epoch ABA predicate; §§3.3.3, 7.3, 17.4. |
| N2 | **RESOLVED** | §7.11 locks the result, clears the previous current flag, then inserts its replacement transactionally. |
| N3 | **RESOLVED** | First-dispatch counter updates preserve `logical_calls ≤ http_calls`; §§9.4.2, 10.2. |
| N4 | **RESOLVED** | §6.7 separates pure `roll_forward`/`project_allowance` from authoritative mutation and ownership locking. |
| N5 | **RESOLVED** | §§11.4 and 11.7 always require review of text-asserted on-site requirements, including when currently met. |
| N6 | **RESOLVED** | V6, §11.7 and §11.10 require grounded confirmation of `not_a_job_ad:ad`; rejection/uncertainty cannot silently hide the job. |
| N7 | **RESOLVED** | §11.6 repairs computable inconsistencies and makes remaining V4 failures block acceptance and null out at contested publication. |
| N8 | **NOT RESOLVED** | §10.10 excludes most online-retry reasons, but `canceled` items outside tuple supersession still become plain `pending`, with no excluded disposition. They remain eligible for another batch. |
| N9 | **RESOLVED** | Ledger CHECKs and §7.16.7 permit provider-proven per-item ambiguous→unbilled settlement. **R3-2** obstructs reaching processing in one new scenario. |
| N10 | **RESOLVED** | §§19.1–19.2 give mounted secrets group-readable permissions and intended consumers supplementary group 10010. |
| N11 | **RESOLVED** | §19.5 uses UUID stream identity and advances object identities above manifest high-water values before writes. |
| N12 | **RESOLVED** | §7.4’s partial unique index and §12.6 reuse only unpurged profiles; purged content receives a new identity. |
| N13 | **RESOLVED** | Settings reject zero radii; §§11.8, 13.1. |
| N14 | **NOT RESOLVED** | Normal purge fencing is substantially repaired, but the new restore replay explicitly releases fences before source rebuild completes. **R3-6**. |

The separate round-2 objection to promised same-input re-review is also **resolved**: §11.11 now explicitly says v1 does not offer it.

**New blocker/major defects in the revision-3 diff**

**R3-1 — BLOCKER: first installation grants permissions before creating the tables.**  
[§19.3, L6081](/home/user/job-hunter/docs/superpowers/specs/2026-10-16-job-hunter-design.md:6081)

The prescribed order first grants on `backup_sets`, `backup_runs`, `manifest_shipments` and `manifest_shipment_items`, then runs Alembic. On an empty installation, initialization creates roles/extensions but none of these tables. The first grant fails because its relation does not exist, so migrations never run and dependent services never start. An upgrade introducing `manifest_shipment_items` has the same problem.

**Must fix:** apply grants after schema creation. Preserve pre-write identity advancement for existing restored tables, with an explicit empty-schema path. Test both first installation and upgrade from the preceding schema.

**R3-2 — MAJOR: recovering a released batch violates `batch_one_in_flight`.**  
[§7.11, L2545](/home/user/job-hunter/docs/superpowers/specs/2026-10-16-job-hunter-design.md:2545), §10.10.

Scenario:

1. Submission A becomes ambiguous; the user releases it.
2. Submission B enters `submitted`, as expressly permitted.
3. Reconciliation finds A’s completed provider batch.
4. A must transition `released → ended`.

The unique index includes `ended` and `submitted`, so step 4 fails. A’s outcomes cannot be processed while B occupies the index. A long-lived ambiguous B can block recovery through A’s result-retention window.

**Must fix:** permit settlement/reconciliation of released submissions without reacquiring the submission-admission singleton—for example, exclude released-origin reconciliation from the index and specify its lifecycle.

**R3-3 — MAJOR: carried credits can allocate more than the cycle’s `S` slots.**  
[§9.6.2, L3601](/home/user/job-hunter/docs/superpowers/specs/2026-10-16-job-hunter-design.md:3601)

I reproduced this directly from the new algorithm with decimal arithmetic:

| Cycle | Available candidates | Credits after accrual | Initial slots | Result |
|---|---|---|---|---|
| 1, `S=10` | No similarity candidates; sufficient safeguard candidates | 7 / 2 / 1 | 7 / 2 / 1 | Safeguards plus spill fill 10; similarity retains 7 credits |
| 2, `S=10` | Sufficient candidates in every lane | 8 / 2 / 1 | 8 / 2 / 1 | `spare=-1`; loop increments all lanes, producing **9 / 3 / 2 = 14 admissions** |

Even changing the loop to stop on `spare <= 0` leaves eleven initial slots. This violates cycle sizing and its pacing contract.

**Must fix:** allocate at most `S` credit-backed slots before selection, including when carried whole credits exceed capacity. Test candidate availability changes and changing `S`, alongside permanent-backlog fairness.

**R3-4 — MAJOR: frozen golden-case data survives the source purge.**  
[§7.13, L2611](/home/user/job-hunter/docs/superpowers/specs/2026-10-16-job-hunter-design.md:2611), [§20.5, L6422](/home/user/job-hunter/docs/superpowers/specs/2026-10-16-job-hunter-design.md:6422).

A job’s text revision comes entirely from another source, while its canonical salary comes from Adzuna. Labelling freezes that salary into the new `golden_cases.det_inputs_snapshot`. The purge deletes tainted deterministic results, but deletes golden cases only when their **material revision** is tainted. This case and its frozen Adzuna salary survive, and future evaluations continue using them.

**Must fix:** retain source provenance for frozen deterministic inputs and purge affected cases/results even when their material revision is untainted. Include their derived evaluation outputs in the manifest.

**R3-5 — BLOCKER: replay interprets content-purge identities as rows that must disappear.**  
[§19.5, L6134](/home/user/job-hunter/docs/superpowers/specs/2026-10-16-job-hunter-design.md:6134), §20.2.

The new replay table says that for completed/shipped manifests, any named ID still present must be deleted. Verification likewise requires every named ID absent. But profile purge intentionally preserves the named `profile_versions` row, its tuples and metadata references, while nulling content. Query proposals and several retention operations likewise preserve rows while clearing fields.

After restoring a successfully purged profile, replay therefore either attempts an illegal physical deletion through retained references, or preserves the row and fails verification. The specified restore cannot finish.

**Must fix:** make manifests describe typed operations—row deletion, field redaction, file removal—and verify each operation’s postcondition. A redacted profile’s ID must remain valid while its content is absent.

**R3-6 — MAJOR: restore releases source fences before rebuilding canonical data.**  
[§19.5, L6142](/home/user/job-hunter/docs/superpowers/specs/2026-10-16-job-hunter-design.md:6142).

Replay explicitly releases fences and queues source rebuilding for later. Consider the mixed-source job above: replay removes Adzuna postings and affected deterministic results, but rebuilding the retained job’s canonical salary is deferred. Restore verification checks named IDs, then starts API/worker. Until the queued rebuild runs, the retained job can expose the old salary, and reconciliation can recompute scores from it.

This contradicts both the purge’s “no Adzuna-derived salary remains” guarantee and N14’s requirement to retain fencing until deletion/rebuild finishes.

**Must fix:** retain/reconstruct the fence through replay and rebuild. Complete rebuilding before declaring replay verified, or keep affected jobs inaccessible to reads and consumers until an atomic rebuild-and-release transaction.

The blockers requiring another revision are **R3-1 and R3-5**. The other must-fix items are **N8 and R3-2, R3-3, R3-4, R3-6**.