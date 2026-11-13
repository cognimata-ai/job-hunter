# M5 final re-review, round 2 — Codex GPT-6 Astra

### Verdict

**REQUEST CHANGES.** Fix C closes the detached-feedback defect and the remaining minor findings, but receipt-based pruning still permits loss of an acknowledged deletion. I reproduced a concurrent shipment whose timestamp precedes the prune’s cached anchor: the prune deletes that newly receipted snapshot, and subsequent shipments skip repairing it. This violates the recovery invariant at [spec:68](/home/user/job-hunter/docs/superpowers/specs/2026-10-16-job-hunter-design.md:68). The blocking selection is at [prune.py:85](/home/user/job-hunter/backend/src/jobhunter/backup/prune.py:85).

### Findings mapped

“Fixed” means supported by inspection and the probes described below. **Reported red→green** refers to the implementer’s evidence; neither permitted test command reached test execution in this read-only environment.

| Finding | Status | Evidence and regression-test assessment |
|---|---|---|
| **Codex R-C1 — pruning loses receipted manifests** | **Partly fixed** | The prune retains its selected receipt and skips manifest forgetting without a listed anchor: [prune.py:75](/home/user/job-hunter/backend/src/jobhunter/backup/prune.py:75). The ten-partial-snapshot regression directly covers the previous failure: [test_restore_run.py:542](/home/user/job-hunter/tests/unit/test_restore_run.py:542), with reported red→green at [report:86](/home/user/job-hunter/.superpowers/sdd/2026-11-09-m5-operations-privacy-production/final-fix-c-report.md:86). It does not cover a concurrent shipment with a backwards timestamp. See R2-C1. |
| **Codex R-M1 — aliases bypass static bounds guard** | **Fixed** | Assignments, annotated assignments and assignment expressions propagate aliases to a fixed point; unsupported uses are rejected: [test_kind_bounds_declared.py:274](/home/user/job-hunter/tests/unit/test_kind_bounds_declared.py:274). My baseline/current probes independently confirmed both original alias cases, assignment expressions, iteration and attribute storage change from undetected to detected. The named planted regressions have reported red→green: [report:217](/home/user/job-hunter/.superpowers/sdd/2026-11-09-m5-operations-privacy-production/final-fix-c-report.md:217). |
| **Codex R-M2 — Logs retention wording** | **Fixed** | Both languages now disclose size rotation: [en.json:1752](/home/user/job-hunter/frontend/src/i18n/en.json:1752), [de.json:1752](/home/user/job-hunter/frontend/src/i18n/de.json:1752). The focused assertions discriminate the old wording; reported red→green: [report:231](/home/user/job-hunter/.superpowers/sdd/2026-11-09-m5-operations-privacy-production/final-fix-c-report.md:231). |
| **Fable N1 — detached feedback retains source-derived scores** | **Fixed** | The shared feedback writer records lineage and deterministic-input sources before insertion: [golden.py:339](/home/user/job-hunter/backend/src/jobhunter/scoring/golden.py:339). Purge selection includes that recorded provenance independently of foreign keys: [source_purge.py:240](/home/user/job-hunter/backend/src/jobhunter/privacy/kinds/source_purge.py:240). Live, replay and export regressions exercise detached rows, with reported red→green: [report:138](/home/user/job-hunter/.superpowers/sdd/2026-11-09-m5-operations-privacy-production/final-fix-c-report.md:138). Further assessment below. |
| **Fable N2 — incomplete snapshots evict complete coverage** | **Partly fixed** | The count-based eviction is removed, and its regression is discriminating. The broader preservation guarantee remains broken by R2-C1: [prune.py:80](/home/user/job-hunter/backend/src/jobhunter/backup/prune.py:80), [test_restore_run.py:542](/home/user/job-hunter/tests/unit/test_restore_run.py:542). |
| **Fable N3 — later-added bounds break older manifests** | **Fixed** | The rule sits beside expand/contract in [spec:6449](/home/user/job-hunter/docs/superpowers/specs/2026-10-16-job-hunter-design.md:6449) and [compat.py:46](/home/user/job-hunter/backend/src/jobhunter/db/compat.py:46). Registration rejects overlapping declarations; the static guard requires added keys to use `.get(key, 0)`: [purge.py:428](/home/user/job-hunter/backend/src/jobhunter/privacy/purge.py:428), [test_kind_bounds_declared.py:483](/home/user/job-hunter/tests/unit/test_kind_bounds_declared.py:483). My probes confirmed the permitted/default-refusal distinction. The replay test **also passed before the change**, as the report correctly acknowledges; declaration/static checks supply the new regression evidence: [report:177](/home/user/job-hunter/.superpowers/sdd/2026-11-09-m5-operations-privacy-production/final-fix-c-report.md:177). |
| **Fable N4 — reuse of an earlier deployment’s repository** | **Fixed** | The RUNBOOK explicitly requires a new empty repository for a database started from nothing: [RUNBOOK.md:248](/home/user/job-hunter/docs/RUNBOOK.md:248). This implements the selected documentation fix. The reported RUNBOOK test pass is not red→green evidence for that sentence: [report:242](/home/user/job-hunter/.superpowers/sdd/2026-11-09-m5-operations-privacy-production/final-fix-c-report.md:242). |
| **Fable N5 — killed manual restore-test leftovers** | **Fixed** | Every restore test performs cleanup under the job lock before recording its run, preserving `restore/`: [restore_test.py:286](/home/user/job-hunter/backend/src/jobhunter/backup/restore_test.py:286). Manual recovery and startup’s broader deletion are documented: [RUNBOOK.md:544](/home/user/job-hunter/docs/RUNBOOK.md:544), [RUNBOOK.md:673](/home/user/job-hunter/docs/RUNBOOK.md:673). The regression checks cleanup ordering and preservation of manual staging; reported red→green: [report:265](/home/user/job-hunter/.superpowers/sdd/2026-11-09-m5-operations-privacy-production/final-fix-c-report.md:265). |
| **Fable N6 — worker paused inside a transaction** | **Fixed for the identified race** | The helper checks worker transactions while paused, retries at most 30 times, and unpauses after a failed check: [stack.ts:210](/home/user/job-hunter/frontend/e2e/support/stack.ts:210). Admission happens only after successful pause; unpause follows executing-state assertions: [20-e18-privacy.spec.ts:199](/home/user/job-hunter/frontend/e2e/specs/20-e18-privacy.spec.ts:199). My stubbed-helper probes confirmed retry/success, exhaustion and check-error cleanup. There is **no red run**; the report supplies E2E evidence instead: [report:298](/home/user/job-hunter/.superpowers/sdd/2026-11-09-m5-operations-privacy-production/final-fix-c-report.md:298). |
| **Fable N7 — UI Logs wording** | **Fixed** | Same change and evidence as R-M2: [en.json:1752](/home/user/job-hunter/frontend/src/i18n/en.json:1752), [de.json:1752](/home/user/job-hunter/frontend/src/i18n/de.json:1752). |
| **Fable N8 — stale sentences and unlocked settings reset** | **Fixed** | Restore help, privacy logging documentation and shipment retry instructions are corrected: [cli.py:316](/home/user/job-hunter/backend/src/jobhunter/cli.py:316), [privacy.py:67](/home/user/job-hunter/backend/src/jobhunter/api/routes/privacy.py:67), [RUNBOOK.md:576](/home/user/job-hunter/docs/RUNBOOK.md:576). The shared schedule reset takes the dead-man lock when disabling: [save.py:352](/home/user/job-hunter/backend/src/jobhunter/settings/save.py:352). The disabled-default regression has reported red→green; the enabled control already passed: [report:316](/home/user/job-hunter/.superpowers/sdd/2026-11-09-m5-operations-privacy-production/final-fix-c-report.md:316). |

**Additional requested checks**

For N1, I found no remaining production feedback writer that creates a snapshot without sources: labels and “wrong score” share the insertion path. Frozen IDs become a `field_redact` operation; execution redacts before the live/replay branch; the typed postcondition requires NULL; export reads the resulting column. [golden.py:289](/home/user/job-hunter/backend/src/jobhunter/scoring/golden.py:289), [source_purge.py:553](/home/user/job-hunter/backend/src/jobhunter/privacy/kinds/source_purge.py:553), [source_purge.py:938](/home/user/job-hunter/backend/src/jobhunter/privacy/kinds/source_purge.py:938), [manifests.py:737](/home/user/job-hunter/backend/src/jobhunter/privacy/manifests.py:737), [export.py:394](/home/user/job-hunter/backend/src/jobhunter/privacy/export.py:394).

The detached-row tests construct retention’s final foreign-key state rather than running retention themselves. That is adequate focused coverage when combined with the real writer assertion and existing retention tests, but is not an independently executed lifecycle test. [purges.py:289](/home/user/job-hunter/tests/support/purges.py:289), [test_golden_cases.py:68](/home/user/job-hunter/tests/integration/test_golden_cases.py:68), [test_retention_m3.py:411](/home/user/job-hunter/tests/integration/test_retention_m3.py:411), [test_job_tombstone.py:253](/home/user/job-hunter/tests/integration/test_job_tombstone.py:253).

I found no new lock-order cycle in the changed reset path: the reset holds `active_config` before the dead-man lock, while the pinger reads schedule state without acquiring that configuration lock. C7’s new cleanup targets only the scratch database and `restore_test/`; it does not remove manual `restore/` staging. [reset.py:60](/home/user/job-hunter/backend/src/jobhunter/settings/reset.py:60), [deadman.py:447](/home/user/job-hunter/backend/src/jobhunter/observability/deadman.py:447), [restore_test.py:239](/home/user/job-hunter/backend/src/jobhunter/backup/restore_test.py:239).

E18’s intended state observation is now controlled. Its retry loop is finite, commands have timeouts, and the test has an outer timeout; persistent activity can fail the gate rather than wait indefinitely. I did not establish browser-level reliability without running the prohibited stack. [stack.ts:30](/home/user/job-hunter/frontend/e2e/support/stack.ts:30), [stack.ts:220](/home/user/job-hunter/frontend/e2e/support/stack.ts:220), [20-e18-privacy.spec.ts:125](/home/user/job-hunter/frontend/e2e/specs/20-e18-privacy.spec.ts:125).

**Parked list, section 2**

1. **Export hash repair:** unchanged; acceptable as previously parked. [review-codex.md:121](/home/user/job-hunter/docs/council/m5-final-review/review-codex.md:121).
2. **Operator fields:** unchanged deployment carry; a safe completion mechanism remains necessary before public deployment. [RUNBOOK.md:92](/home/user/job-hunter/docs/RUNBOOK.md:92).
3. **E18 race:** the identified transaction-freeze residual is addressed, with the validation limits above. [stack.ts:220](/home/user/job-hunter/frontend/e2e/support/stack.ts:220).
4. **Generic-plan eligibility:** remains closed; the fixed-purpose reader retains literal predicates. [reconcilers.py:71](/home/user/job-hunter/backend/src/jobhunter/privacy/reconcilers.py:71).
5. **Fail-open egress:** unchanged from the previous topology-dependent acceptance. [rereview-codex.md:50](/home/user/job-hunter/docs/council/m5-final-review/rereview-codex.md:50).
6. **Malformed bounds:** named refusal remains, and C3 supplies the previously missing release rule. [replay.py:324](/home/user/job-hunter/backend/src/jobhunter/privacy/replay.py:324), [compat.py:46](/home/user/job-hunter/backend/src/jobhunter/db/compat.py:46).

### New findings

**Critical**

**R2-C1 — A concurrent shipment with a backwards timestamp can be successfully receipted and then forgotten.**

Location: [prune.py:75](/home/user/job-hunter/backend/src/jobhunter/backup/prune.py:75), particularly [prune.py:85](/home/user/job-hunter/backend/src/jobhunter/backup/prune.py:85).

Failure path:

1. Snapshot A contains `{M1}`, has a receipt, and has timestamp 12:00.
2. Prune reads A’s receipt and releases its database connection.
3. After a backwards clock adjustment, another ship uploads B containing `{M1, M2}`, timestamped 11:59, and commits its receipt.
4. Prune lists A and B. Its cached anchor is still A, so it forgets B because `B.time < A.time`.
5. The host is lost. A backup set predating M2 plus the surviving stream cannot replay M2.

Prune holds the backup-job lock, while shipment holds a separate shipment lock. Neither prevents this interleaving; command-level repository locking does not make the receipt query and snapshot listing atomic. [prune.py:68](/home/user/job-hunter/backend/src/jobhunter/backup/prune.py:68), [shipper.py:193](/home/user/job-hunter/backend/src/jobhunter/backup/shipper.py:193).

My in-memory probe used the actual shipment orchestration, prune selector and restore snapshot selector with fake boundaries:

```text
concurrent ship: True receipt=B True
prune forgotten: 1 B forgotten: True restore-selected union: ['M1']
next ship repairs: False
next prune forgotten: 0
```

The failure persists: shipment compares local UUID/hash pairs with the latest receipt and skips re-upload without checking whether its snapshot exists. The next prune merely warns `receipt_not_listed`. The shipping reconciler also trusts receipt rows, so `privacy.manifest_unshipped` does not diagnose lost repository coverage. [shipper.py:194](/home/user/job-hunter/backend/src/jobhunter/backup/shipper.py:194), [prune.py:82](/home/user/job-hunter/backend/src/jobhunter/backup/prune.py:82), [reconcilers.py:57](/home/user/job-hunter/backend/src/jobhunter/privacy/reconcilers.py:57).

Restore selects surviving snapshots, and verification can only check manifests present in the restored database or stream. An older set has no M2 evidence from which to detect this omission. [restore.py:313](/home/user/job-hunter/backend/src/jobhunter/backup/restore.py:313), [restore_verify.py:246](/home/user/job-hunter/backend/src/jobhunter/privacy/restore_verify.py:246).

**Fix:** serialize receipt selection, snapshot listing and manifest forgetting against shipments—for example, hold the shipment lock throughout those steps, with an explicit lock order. Add the interleaving above to the older-set restore regression.

**Concern (1): warning alone is insufficient.** Skipping forgetting is appropriately conservative, but a missing anchor can mean coverage is already lost, including through this bug. Make that condition actionable and permit verified re-upload instead of indefinitely taking the unchanged-stream fast path. This preserves F-4’s receipt-based policy; it repairs its unenforced concurrency assumption. [progress.md:637](/home/user/job-hunter/.superpowers/sdd/2026-11-09-m5-operations-privacy-production/progress.md:637).

**Important**

None additional.

**Minor**

None additional.

### What I ran

- Read both first re-reviews, first-round context, rulings F-1–F-4, fix C’s report and concerns, relevant spec sections, and the parked list. Inspected all ten commits in `ef54794..887c6f6`.
- In-memory Python probes of actual shipment/prune/restore-selection functions reproduced R2-C1. Controls confirmed no forgetting with no receipt, a missing receipt, equal timestamps, or an already-selected latest receipt with a backwards timestamp.
- Baseline/current static-analysis probes confirmed alias detection and the added-key access rule; all five current kinds’ observed bounds matched their declarations.
- In-memory JavaScript probes of the transpiled pause helper confirmed retry/success, bounded exhaustion and check-error unpause behavior. All external operations were stubbed.
- Attempted `uv run --group docparse pytest -W error -q -p no:cacheprovider tests/unit/test_kind_bounds_declared.py::test_every_bound_a_kind_reads_is_one_it_declares`, offline with bytecode writes disabled. **Stopped before collection:** read-only uv cache-lock creation.
- Attempted `npm --prefix frontend test -- PrivacySection.test.tsx`, offline. **Stopped before tests:** read-only Vite temporary-config creation.
- Final git status was clean; HEAD remained `887c6f65ecff6a0c343fd0dfbee6bb5f2fe4961b`.
- No network, paid calls, Docker, stacks, Playwright, whole suites, sub-agents, credential reads, or reads of `.secrets/` or `.fixtures-raw/`.

The review was **not saved** to `docs/council/m5-final-review/rereview2-codex.md`: the environment enforces read-only filesystem access.