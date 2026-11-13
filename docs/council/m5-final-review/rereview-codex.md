# M5 final re-review — Codex GPT-6 Astra

### Verdict

**REQUEST CHANGES.** Codex C1 remains partly fixed: shipment serialization and the restore union address the original interleavings, but pruning the newest ten snapshots without checking completeness can remove the last copy of an acknowledged deletion. I reproduced the stream omission with the actual shipper, prune and restore orchestration using in-memory boundaries. This violates the recovery invariant in [the spec:68](/home/user/job-hunter/docs/superpowers/specs/2026-10-16-job-hunter-design.md:68). The blocking location is [prune.py:78](/home/user/job-hunter/backend/src/jobhunter/backup/prune.py:78).

### Findings mapped

“Fixed” below means supported by code inspection. **Reported red→green** identifies implementer evidence, not an independently repeated test run: both permitted test commands stopped before collection/startup on the read-only filesystem.

| First-round finding | Status | Evidence and regression-test assessment |
|---|---|---|
| **Codex C1 — shipped manifests omitted by restore** | **Partly fixed** | Lock 7261007 covers enumeration through receipt, including the receipt timestamp: [shipper.py:193](/home/user/job-hunter/backend/src/jobhunter/backup/shipper.py:193). The restore merges retained snapshots and refuses conflicting bytes: [restore.py:262](/home/user/job-hunter/backend/src/jobhunter/backup/restore.py:262). The concurrency, older-snapshot and conflict tests target the original failures; red→green is reported in [fix A:87](/home/user/job-hunter/.superpowers/sdd/2026-11-09-m5-operations-privacy-production/final-fix-a-report.md:87). The pruning interaction remains unsafe; see R-C1 below. |
| **Codex I1 — Adzuna scores retained in feedback** | **Fixed** | A2 freezes bounded feedback IDs through revision, LLM-result or deterministic-result provenance, then records `field_redact`: [source_purge.py:237](/home/user/job-hunter/backend/src/jobhunter/privacy/kinds/source_purge.py:237), [source_purge.py:549](/home/user/job-hunter/backend/src/jobhunter/privacy/kinds/source_purge.py:549). Live execution and replay redact those IDs before their paths diverge: [source_purge.py:933](/home/user/job-hunter/backend/src/jobhunter/privacy/kinds/source_purge.py:933). Phase C checks the typed postcondition; export reads the resulting nullable column: [purge.py:732](/home/user/job-hunter/backend/src/jobhunter/privacy/purge.py:732), [export.py:394](/home/user/job-hunter/backend/src/jobhunter/privacy/export.py:394). All four named regression tests exercise relevant behavior; red→green is reported in [fix A:166](/home/user/job-hunter/.superpowers/sdd/2026-11-09-m5-operations-privacy-production/final-fix-a-report.md:166). The no-earlier-deployment assumption is explicit in [the spec:6713](/home/user/job-hunter/docs/superpowers/specs/2026-10-16-job-hunter-design.md:6713). |
| **Codex M1 — disable during dead-man pass** | **Fixed** | Each run rechecks state under the lock; a disabling save takes the same transaction lock: [deadman.py:484](/home/user/job-hunter/backend/src/jobhunter/observability/deadman.py:484), [save.py:368](/home/user/job-hunter/backend/src/jobhunter/settings/save.py:368). My cached-decision probe produced two sends before the change and one afterwards. The real-lock regression is [test_deadman_sync.py:268](/home/user/job-hunter/tests/integration/test_deadman_sync.py:268); red→green is reported in [fix A:354](/home/user/job-hunter/.superpowers/sdd/2026-11-09-m5-operations-privacy-production/final-fix-a-report.md:354). |
| **Codex M2 — E18 transient-state race** | **Fixed** | Worker pause precedes admission; executing-state and axe assertions precede unpause; `afterEach` provides cleanup: [20-e18-privacy.spec.ts:196](/home/user/job-hunter/frontend/e2e/specs/20-e18-privacy.spec.ts:196), [20-e18-privacy.spec.ts:66](/home/user/job-hunter/frontend/e2e/specs/20-e18-privacy.spec.ts:66). No failing baseline was demonstrated; the report explicitly provides a successful E2E run instead: [fix B:32](/home/user/job-hunter/.superpowers/sdd/2026-11-09-m5-operations-privacy-production/final-fix-b-report.md:32). |
| **Codex M3 — missing manifest bounds** | **Fixed** | Classification validates all available contents before evaluating postconditions or starting replay, with UUID, kind and missing key: [replay.py:320](/home/user/job-hunter/backend/src/jobhunter/privacy/replay.py:320), [replay.py:339](/home/user/job-hunter/backend/src/jobhunter/privacy/replay.py:339). The four missing-key cases and bound-free golden-case control are appropriate: [test_replay.py:373](/home/user/job-hunter/tests/integration/test_replay.py:373). Red→green is reported in [fix A:303](/home/user/job-hunter/.superpowers/sdd/2026-11-09-m5-operations-privacy-production/final-fix-a-report.md:303). My probe confirmed the four named refusals. The added static guard has a separate minor gap below. |
| **Fable I1 — interrupted restore-test copies survive** | **Fixed** | Startup invokes cleanup of the scratch database and both staging directories: [scheduler.py:201](/home/user/job-hunter/backend/src/jobhunter/backup/scheduler.py:201), [restore_test.py:234](/home/user/job-hunter/backend/src/jobhunter/backup/restore_test.py:234). The regression checks database removal, directory removal, cache preservation and idempotence: [test_backup_scheduler.py:224](/home/user/job-hunter/tests/unit/test_backup_scheduler.py:224); red→green is reported in [fix A:217](/home/user/job-hunter/.superpowers/sdd/2026-11-09-m5-operations-privacy-production/final-fix-a-report.md:217). Cleanup remains best-effort if PostgreSQL refuses the drop: [restore_test.py:240](/home/user/job-hunter/backend/src/jobhunter/backup/restore_test.py:240). |
| **Fable M1 — client addresses in logs** | **Fixed** | Authentication, re-authentication and edge refusal logs drop the address: [auth.py:332](/home/user/job-hunter/backend/src/jobhunter/api/routes/auth.py:332), [reauth.py:69](/home/user/job-hunter/backend/src/jobhunter/api/reauth.py:69), [edge.py:358](/home/user/job-hunter/backend/src/jobhunter/api/edge.py:358). The three named tests assert absence, rather than merely masking display; red→green is reported in [fix B:70](/home/user/job-hunter/.superpowers/sdd/2026-11-09-m5-operations-privacy-production/final-fix-b-report.md:70). The statement now discloses size rotation: [PRIVACY.md:15](/home/user/job-hunter/docs/PRIVACY.md:15). See R-M2 for remaining UI drift. |
| **Fable M2 — incomplete newest snapshot** | **Partly fixed** | A retained older snapshot now contributes its missing manifests, and zero-file snapshots are skipped: [restore.py:294](/home/user/job-hunter/backend/src/jobhunter/backup/restore.py:294), [restore.py:323](/home/user/job-hunter/backend/src/jobhunter/backup/restore.py:323). Relevant tests are [test_restore_run.py:361](/home/user/job-hunter/tests/unit/test_restore_run.py:361) and [test_restore_run.py:412](/home/user/job-hunter/tests/unit/test_restore_run.py:412), with reported red→green in [fix B:280](/home/user/job-hunter/.superpowers/sdd/2026-11-09-m5-operations-privacy-production/final-fix-b-report.md:280). Pruning can still remove the required older snapshot; R-C1. |
| **Fable M3 — backup stop grace** | **Fixed** | `backup` has ten minutes: [compose.yaml:265](/home/user/job-hunter/deploy/compose.yaml:265). The test pins production and test-stack configuration: [test_compose_prod.py:477](/home/user/job-hunter/tests/unit/test_compose_prod.py:477); reported red→green is in [fix A:217](/home/user/job-hunter/.superpowers/sdd/2026-11-09-m5-operations-privacy-production/final-fix-a-report.md:217). |
| **Fable M4 — missing nftables prerequisite** | **Fixed** | Preflight refuses missing `nft` before installation steps: [host-setup.sh:166](/home/user/job-hunter/scripts/host-setup.sh:166). The missing-tool test checks refusal with no changes/probes: [test_host_setup_script.py:214](/home/user/job-hunter/tests/unit/test_host_setup_script.py:214); reported red→green: [fix B:110](/home/user/job-hunter/.superpowers/sdd/2026-11-09-m5-operations-privacy-production/final-fix-b-report.md:110). |
| **Fable M5 — misleading egress health check** | **Fixed** | The prescribed check now inspects the actual table after boot and every firewall change, and explains why unit status is insufficient: [RUNBOOK.md:321](/home/user/job-hunter/docs/RUNBOOK.md:321). This is a documentation correction; no behavioral red→green proof is supplied. |
| **Fable M6 — incorrect Vitest invocation** | **Fixed** | The brief specifies the package script: [final-rereview-prompt.md:32](/home/user/job-hunter/.superpowers/sdd/2026-11-09-m5-operations-privacy-production/final-rereview-prompt.md:32). My invocation reached the frontend configuration, then failed on its read-only temporary-file write. |
| **Fable M7 — healthchecks disclosure** | **Fixed** | The agreed wording appears at [PRIVACY.md:40](/home/user/job-hunter/docs/PRIVACY.md:40). The named parity test checks agreement with the spec: [test_privacy_md.py:189](/home/user/job-hunter/tests/unit/test_privacy_md.py:189). That test is not discriminating red→green evidence: the old tables also agreed. |
| **Fable M8 — bare `KeyError`** | **Fixed** | Same runtime validation and regression evidence as Codex M3: [replay.py:320](/home/user/job-hunter/backend/src/jobhunter/privacy/replay.py:320), [test_replay.py:383](/home/user/job-hunter/tests/integration/test_replay.py:383). |
| **Fable M9 — declarative 0010 floor** | **Fixed** | The docstring now explicitly denies relying on this declaration for rollback to an unreadable 0009 release: [0010_m5_operations.py:46](/home/user/job-hunter/backend/src/jobhunter/db/migrations/versions/0010_m5_operations.py:46). The reported compatibility-test pass does not establish red→green for this prose-only correction: [fix B:130](/home/user/job-hunter/.superpowers/sdd/2026-11-09-m5-operations-privacy-production/final-fix-b-report.md:130). |

**Parked list, section 2**

1. **Export hash repair:** unchanged; acceptable as previously parked. Repair remains at [crypto_rotate.py:323](/home/user/job-hunter/backend/src/jobhunter/crypto_rotate.py:323); the first-round rationale remains applicable: [review-codex.md:121](/home/user/job-hunter/docs/council/m5-final-review/review-codex.md:121).
2. **Operator fields:** unchanged deployment carry. A safe completion mechanism still needs deciding before public deployment; [RUNBOOK.md:92](/home/user/job-hunter/docs/RUNBOOK.md:92) explicitly records the unresolved build/markers problem.
3. **E18 race:** addressed; my previous objection to retaining the race in a required gate no longer applies. [20-e18-privacy.spec.ts:196](/home/user/job-hunter/frontend/e2e/specs/20-e18-privacy.spec.ts:196).
4. **Generic-plan eligibility:** addressed by literal predicates in both readers: [reconcilers.py:72](/home/user/job-hunter/backend/src/jobhunter/privacy/reconcilers.py:72), [delete_everything.py:306](/home/user/job-hunter/backend/src/jobhunter/privacy/kinds/delete_everything.py:306). The revised test explicitly prepares both readers under custom and generic plans: [test_migration_0011.py:135](/home/user/job-hunter/tests/integration/test_migration_0011.py:135).
5. **Fail-open egress:** remains acceptable under the previous topology-dependent judgment. The operational check is improved as requested: [RUNBOOK.md:321](/home/user/job-hunter/docs/RUNBOOK.md:321).
6. **Malformed bounds:** runtime diagnostic issue addressed. The future-regression guard needs the minor improvement R-M1 below: [replay.py:339](/home/user/job-hunter/backend/src/jobhunter/privacy/replay.py:339), [test_kind_bounds_declared.py:262](/home/user/job-hunter/tests/unit/test_kind_bounds_declared.py:262).

### New findings

**Critical**

**R-C1 — Count-based pruning can discard a successfully receipted manifest.**

Location: [prune.py:78](/home/user/job-hunter/backend/src/jobhunter/backup/prune.py:78), introduced by `5017311`.

Failure path:

1. Take a backup set before M2’s deletion.
2. Successfully ship `{M1, M2}` and commit its receipt.
3. Add M3. Ten subsequent attempts hash the stream successfully but encounter M2 unreadable during restic’s read. Each leaves an incomplete `{M1, M3}` snapshot and writes no receipt.
4. Prune keeps those ten snapshots and forgets the complete snapshot.
5. Lose the host and restore the older set. The union contains M1 and M3, but no M2.

The shipper permits the repeated attempts because the current stream still differs from the latest successful receipt: [shipper.py:194](/home/user/job-hunter/backend/src/jobhunter/backup/shipper.py:194). Pruning distinguishes neither receipts nor completeness: [restic.py:225](/home/user/job-hunter/backend/src/jobhunter/backup/restic.py:225). Restore reads only retained snapshots: [restore.py:313](/home/user/job-hunter/backend/src/jobhunter/backup/restore.py:313). An older dump has no M2 record from which replay can reconstruct it: [replay.py:329](/home/user/job-hunter/backend/src/jobhunter/privacy/replay.py:329).

My in-memory probe produced:

```text
before_prune: 11 snapshots; receipts=1; confirmed_M2=True
after_prune: 10 snapshots; confirmed_snapshot_retained=False
actual_restore_stream: 2 manifests; ids=[1, 3]; M2 omitted, no refusal
```

This does not challenge choosing a bounded union. It demonstrates that its safety precondition is unenforced. The code itself acknowledges the ten-incomplete-snapshot exception at [prune.py:40](/home/user/job-hunter/backend/src/jobhunter/backup/prune.py:40). The new test covers twelve **successful** shipments only, so it cannot detect this failure: [test_restore_run.py:505](/home/user/job-hunter/tests/unit/test_restore_run.py:505).

**Fix:** retain a verified complete covering snapshot, or refuse destructive pruning until such coverage is established. Incomplete snapshots must not evict the last proven coverage. Add the sequence above, including an older dump, and assert either complete replay or refusal before coverage is lost. Reconcile the unconditional preservation promises in [PRIVACY.md:83](/home/user/job-hunter/docs/PRIVACY.md:83) and [RUNBOOK.md:526](/home/user/job-hunter/docs/RUNBOOK.md:526).

**Important**

No additional Important finding.

**Minor**

**R-M1 — The static bounds guard silently misses ordinary aliases.**

Location: [test_kind_bounds_declared.py:268](/home/user/job-hunter/tests/unit/test_kind_bounds_declared.py:268).

`bounds_names` handles direct `ast.Assign` bindings only. Planting either of these in `JobTombstone.execute` leaves the check passing:

```python
limits: dict[str, int] = content["max_ids"]
limits["planted"]
```

```python
limits = content["max_ids"]
other = limits
other["planted"]
```

My probe returned no undeclared key and no unresolved read for either, while a direct subscript correctly reported `planted`. These forms bypass the promised refusal of unrecognised reads: [test_kind_bounds_declared.py:335](/home/user/job-hunter/tests/unit/test_kind_bounds_declared.py:335), [test_kind_bounds_declared.py:433](/home/user/job-hunter/tests/unit/test_kind_bounds_declared.py:433).

**Fix:** propagate aliases to a fixed point, include annotated assignments, and reject unhandled uses of tracked bounds. Add both planted cases. Today’s five procedures match their declarations; this is a future-regression coverage defect.

**R-M2 — The UI still implies time-bounded log retention.**

Locations: [en.json:1752](/home/user/job-hunter/frontend/src/i18n/en.json:1752), [de.json:1752](/home/user/job-hunter/frontend/src/i18n/de.json:1752).

Both retention rows still say approximately fourteen days, while the corrected statement explains size rotation and potentially much longer retention: [PRIVACY.md:15](/home/user/job-hunter/docs/PRIVACY.md:15). Removing addresses fixes the privacy leakage but does not make that retention description accurate.

**Fix:** describe size-based rotation in both UI strings, including that quiet services retain logs longer.

**Other requested checks**

I found no acquisition cycle among 7261007, 7261006 and 7261005 in the inspected paths. Shipping holds its own lock; backup jobs hold theirs; disabling settings share only the dead-man lock with ping/sync work. The pinger does not acquire the settings save’s `active_config` lock: [shipper.py:193](/home/user/job-hunter/backend/src/jobhunter/backup/shipper.py:193), [backup_set.py:316](/home/user/job-hunter/backend/src/jobhunter/backup/backup_set.py:316), [save.py:368](/home/user/job-hunter/backend/src/jobhunter/settings/save.py:368), [deadman.py:447](/home/user/job-hunter/backend/src/jobhunter/observability/deadman.py:447).

The weekly prune reduces accumulated restore work, but ten is not a continuous ceiling: snapshots accumulate between prunes, and restore reads all retained snapshots. I found no demonstrated four-hour breach; I did not measure the deployment RTO. [RUNBOOK.md:523](/home/user/job-hunter/docs/RUNBOOK.md:523), [restore.py:323](/home/user/job-hunter/backend/src/jobhunter/backup/restore.py:323), [spec:6434](/home/user/job-hunter/docs/superpowers/specs/2026-10-16-job-hunter-design.md:6434).

### What I ran

- Read both reviews, both fix reports, the rulings and parked list; inspected the fix history and commits in `0f5a731..9864297`.
- In-memory probes using `.venv/bin/python -B`:
  - Actual shipper/prune/restore orchestration with fake database, filesystem and repository boundaries: reproduced R-C1.
  - Old versus current dead-man cached-decision behavior: two sends versus one.
  - Four named missing-bound refusals.
  - Snapshot-count parsing/filtering: zero excluded; positive and unknown counts retained.
  - Bounds analysis over all five current kinds, plus direct, typed-alias and alias-chain planted reads.
- Attempted the prescribed focused pytest command for `test_every_bound_a_kind_reads_is_one_it_declares`, with offline mode and bytecode writes disabled. **Stopped before collection:** `uv` could not create its cache-lock temporary file.
- Ran `npm --prefix frontend test -- PrivacySection.test.tsx` with npm offline. **Stopped before tests:** Vite could not write its temporary configuration file.
- Final `git status --porcelain=v1` was empty; HEAD remained `9864297e78322bb5068b1b7ea54a6975d75086f2`.
- No Docker, network or paid calls, Playwright, stacks, whole suites, sub-agents, or reads of `.secrets/`, `.fixtures-raw/` or credentials.

The review was **not saved** to `docs/council/m5-final-review/rereview-codex.md`: this environment grants read-only filesystem access.