# M5 final re-review, round 3 — Codex GPT-6 Astra

### Verdict

**REQUEST CHANGES.** D1 closes the original interleave while the shipment-lock session survives, but D2 does not prevent a shipper from successfully receipting a snapshot that a prune has already forgotten after lock loss. An in-memory probe reproduced missing restore coverage and the subsequent unchanged-stream skip. The new manifest UPDATE grants also let a compromised backup container falsely complete an unfinished purge. These block approval; neither finding challenges F-5’s chosen receipt-removal mechanism. [shipper.py:224](/home/user/job-hunter/backend/src/jobhunter/backup/shipper.py:224), [shipper.py:242](/home/user/job-hunter/backend/src/jobhunter/backup/shipper.py:242), [migrate.py:156](/home/user/job-hunter/backend/src/jobhunter/db/migrate.py:156).

### Findings mapped

“Fixed” below means supported by inspection and the probes described under “What I ran,” not an independently executed integration suite.

| Finding | Status | Evidence |
|---|---|---|
| **R2-C1 → D1** | **Partly fixed** | Prune now nests shipment lock 7261007 inside job lock 7261006 and holds it across receipt selection, listing and forgetting. Missing receipts are removed transactionally, with matching shipped manifests returned to completed. Lock loss still permits the failure under R3-C1 below. [prune.py:99](/home/user/job-hunter/backend/src/jobhunter/backup/prune.py:99), [prune.py:125](/home/user/job-hunter/backend/src/jobhunter/backup/prune.py:125), [prune.py:154](/home/user/job-hunter/backend/src/jobhunter/backup/prune.py:154). |
| **M-1 → D4** | **Fixed** | The added-key rule now requires a table created empty; backfills require a format change or bounded cascading deletion. The spec and both requested docstrings agree. [spec:6449](/home/user/job-hunter/docs/superpowers/specs/2026-10-16-job-hunter-design.md:6449), [compat.py:48](/home/user/job-hunter/backend/src/jobhunter/db/compat.py:48), [purge.py:24](/home/user/job-hunter/backend/src/jobhunter/privacy/purge.py:24). |
| **M-2 → D5** | **Fixed for the reported forms** | `.get("max_ids", …)`, computed content keys and subscripted assignment expressions are now recognized or refused. My probe detected all four planted variants and accepted the added-key `.get(key, 0)` control. [test_kind_bounds_declared.py:342](/home/user/job-hunter/tests/unit/test_kind_bounds_declared.py:342), [test_kind_bounds_declared.py:356](/home/user/job-hunter/tests/unit/test_kind_bounds_declared.py:356), [test_kind_bounds_declared.py:421](/home/user/job-hunter/tests/unit/test_kind_bounds_declared.py:421). |
| **M-3 → D3** | **Fixed** | The report distinguishes `no_receipt`, `receipt_not_listed` and no skip; the RUNBOOK gives the query and explains the fields. [prune.py:159](/home/user/job-hunter/backend/src/jobhunter/backup/prune.py:159), [prune.py:175](/home/user/job-hunter/backend/src/jobhunter/backup/prune.py:175), [prune.py:206](/home/user/job-hunter/backend/src/jobhunter/backup/prune.py:206), [RUNBOOK.md:552](/home/user/job-hunter/docs/RUNBOOK.md:552). |
| **M-4 → D2** | **Partly fixed** | `confirm()` checks the owning session, database and advisory key in `pg_locks`. Both callers check before their specified operation. This detects an already-lost session, but does not fence later receipt publication or receipt removal. [shipper.py:72](/home/user/job-hunter/backend/src/jobhunter/backup/shipper.py:72), [shipper.py:107](/home/user/job-hunter/backend/src/jobhunter/backup/shipper.py:107), [prune.py:163](/home/user/job-hunter/backend/src/jobhunter/backup/prune.py:163). |

**The three mechanism choices**

- **`shipped → completed`: sound for receipt invalidation.** It clears shipment linkage while retaining the manifest. However, “the prune’s alone” describes the intended caller, not an enforced authorization boundary: the generic transition registry and database trigger check edges without identifying the caller. The broader consequence of granting the backup role status updates is R3-I1. [transitions.py:723](/home/user/job-hunter/backend/src/jobhunter/db/transitions.py:723), [0002_m0_transition_matrix.py:31](/home/user/job-hunter/backend/src/jobhunter/db/migrations/versions/0002_m0_transition_matrix.py:31).
- **`first_entry_kept`: sound for this use.** The generated assignment is `coalesce(t.completed_at, now())`; it preserves the original overdue-alert clock. Registration rejects a preserved state without a timestamp column. [transitions.py:67](/home/user/job-hunter/backend/src/jobhunter/db/transitions.py:67), [transitions.py:894](/home/user/job-hunter/backend/src/jobhunter/db/transitions.py:894), [reconcilers.py:63](/home/user/job-hunter/backend/src/jobhunter/privacy/reconcilers.py:63).
- **Grants: the receipt DELETE grants support F-5, but the UPDATE grants are not confined to unshipping.** A compromised backup container can erase receipt history and change completion/shipment metadata. These grants do not authorize deleting manifest rows or changing their instructions/hash, and the production manifest mount remains read-only. Nevertheless, they permit false purge completion; see R3-I1. [migrate.py:94](/home/user/job-hunter/backend/src/jobhunter/db/migrate.py:94), [migrate.py:151](/home/user/job-hunter/backend/src/jobhunter/db/migrate.py:151), [test_migration_0010.py:699](/home/user/job-hunter/tests/integration/test_migration_0010.py:699), [compose.yaml:276](/home/user/job-hunter/deploy/compose.yaml:276).

**Lock order and coverage**

I found no acquisition cycle among 7261005/6/7: prune takes 6 then 7; shipment takes only 7; backup sets and restore tests take 6; dead-man handling and disabling settings use 5 without acquiring 6 or 7. [prune.py:99](/home/user/job-hunter/backend/src/jobhunter/backup/prune.py:99), [shipper.py:235](/home/user/job-hunter/backend/src/jobhunter/backup/shipper.py:235), [backup_set.py:428](/home/user/job-hunter/backend/src/jobhunter/backup/backup_set.py:428), [restore_test.py:286](/home/user/job-hunter/backend/src/jobhunter/backup/restore_test.py:286), [deadman.py:387](/home/user/job-hunter/backend/src/jobhunter/observability/deadman.py:387), [save.py:352](/home/user/job-hunter/backend/src/jobhunter/settings/save.py:352).

With surviving lock sessions and a complete repository listing, the selected complete anchor protects previously receipted manifests despite clock changes or partial snapshots. Receipt removal itself forgets no manifest snapshot. A short listing can nevertheless remove a genuine receipt; additionally, R3-C1 demonstrates genuine-receipt removal through a stale listing after lock loss. These metadata removals do not themselves remove snapshot contents. [prune.py:125](/home/user/job-hunter/backend/src/jobhunter/backup/prune.py:125), [prune.py:160](/home/user/job-hunter/backend/src/jobhunter/backup/prune.py:160), [shipper.py:35](/home/user/job-hunter/backend/src/jobhunter/backup/shipper.py:35).

### New findings

**Critical**

**R3-C1 — Lock loss after confirmation still allows successful publication of a forgotten snapshot.**

Location: [shipper.py:224](/home/user/job-hunter/backend/src/jobhunter/backup/shipper.py:224), [shipper.py:242](/home/user/job-hunter/backend/src/jobhunter/backup/shipper.py:242), [prune.py:178](/home/user/job-hunter/backend/src/jobhunter/backup/prune.py:178).

Failure path:

1. Receipt R0 names snapshot A containing `{M1}`, timestamped 12:00.
2. After a backwards clock step, a shipper lists `{M1, M2}`, passes `held.confirm()`, and uploads snapshot B timestamped 11:59.
3. Its dedicated lock connection dies during the upload. B becomes visible, but the shipper has not yet written its receipt.
4. Prune acquires the now-free shipment lock, selects R0, lists A and B, and forgets B because it predates A.
5. The shipper resumes and writes B’s receipt through `engine.begin()`, independently of the dead lock connection. Cleanup suppresses the lost-connection error.
6. The next shipment sees matching receipt items and skips repair. A disaster before a later repairing prune leaves an older backup set and a restore stream containing only M1. [shipper.py:188](/home/user/job-hunter/backend/src/jobhunter/backup/shipper.py:188), [shipper.py:238](/home/user/job-hunter/backend/src/jobhunter/backup/shipper.py:238), [restore.py:313](/home/user/job-hunter/backend/src/jobhunter/backup/restore.py:313).

My probe executed the repository’s orchestration and restore-selection bodies with in-memory boundaries:

```text
successful_receipt_for_missing_snapshot: True
next_ship_skipped: True
restore_union: ['M1']
lock_checks_passed: 3
```

The shipping reconciler trusts the receipt, so M2 can be acknowledged despite missing coverage. This violates S9 and the published confirmation guarantee. [reconcilers.py:57](/home/user/job-hunter/backend/src/jobhunter/privacy/reconcilers.py:57), [spec:68](/home/user/job-hunter/docs/superpowers/specs/2026-10-16-job-hunter-design.md:68), [PRIVACY.md:129](/home/user/job-hunter/docs/PRIVACY.md:129).

The removal path has the same separation: after confirmation, `_remove_false_receipts` opens another connection and repeatedly reads the **current** latest receipt against the **earlier** snapshot listing. Losing the lock and admitting a shipment between those operations deletes the new shipment’s genuine receipt. A second probe reproduced removal of receipts `(2, 1)` while snapshot 2 still existed. [prune.py:129](/home/user/job-hunter/backend/src/jobhunter/backup/prune.py:129), [prune.py:163](/home/user/job-hunter/backend/src/jobhunter/backup/prune.py:163).

**Fix:** make receipt publication and receipt removal fail atomically with loss of the session that owns shipment serialization—for example, execute their transactions on that same session. Do not reacquire a lock and continue with an old listing. A further standalone confirmation only narrows the race. Add regressions for lock loss during upload and after prune confirmation; the current tests terminate sessions before confirmation. [test_shipper.py:319](/home/user/job-hunter/tests/integration/test_shipper.py:319), [test_backup_set.py:747](/home/user/job-hunter/tests/integration/test_backup_set.py:747).

**Important**

**R3-I1 — The backup role can falsely complete an unfinished purge.**

Location: [migrate.py:98](/home/user/job-hunter/backend/src/jobhunter/db/migrate.py:98), [migrate.py:156](/home/user/job-hunter/backend/src/jobhunter/db/migrate.py:156).

The new grants permit this update by a compromised backup container:

```sql
UPDATE deletion_manifests
SET status = 'completed', completed_at = now()
WHERE status = 'executing' AND file_sha256 IS NOT NULL;
```

This uses only granted columns. `executing → completed` is an allowed edge, the trigger has no role-specific restriction, and both manifest CHECK constraints accept the resulting row. [transitions.py:730](/home/user/job-hunter/backend/src/jobhunter/db/transitions.py:730), [0002_m0_transition_matrix.py:34](/home/user/job-hunter/backend/src/jobhunter/db/migrations/versions/0002_m0_transition_matrix.py:34), [0010_m5_operations.py:92](/home/user/job-hunter/backend/src/jobhunter/db/migrations/versions/0010_m5_operations.py:92).

A purge interrupted after A3 has its hash recorded before deletion finishes. If the backup container applies the update before retry, the worker sees a non-executing manifest and ends successfully without performing the remaining deletion. A shipment receipt can subsequently make the manifest appear shipped. [purge.py:667](/home/user/job-hunter/backend/src/jobhunter/privacy/purge.py:667), [purge.py:570](/home/user/job-hunter/backend/src/jobhunter/privacy/purge.py:570), [reconcilers.py:97](/home/user/job-hunter/backend/src/jobhunter/privacy/reconcilers.py:97).

**Fix:** enforce the backup role’s permitted operation at the database boundary: only `shipped → completed`, clear shipment fields, and preserve completion time. A restricted function or role-aware trigger can retain F-5’s mechanism while denying premature completion and arbitrary timestamp edits. Add privilege regressions for those forbidden operations, beyond the existing content-update and row-deletion refusals. [test_migration_0010.py:699](/home/user/job-hunter/tests/integration/test_migration_0010.py:699).

**Minor**

None additional.

### What I ran

- Read the brief, both round-2 reviews, F-1–F-5, the implementer’s report, relevant binding spec sections, and all seven commits in `1de9493..821d21d`.
- Ran no-I/O Python probes of the actual shipment, prune, receipt-removal and restore-selection bodies. They reproduced both R3-C1 paths.
- Probed the static guard’s four reported variants and added-key control; inspected the actual generated transition assignments.
- Checked R3-I1 against the actual transition registry and schema constraints without executing database mutations.
- Attempted the permitted focused pytest command for the bounds guard, M5 transition registry and lock-order test, with offline operation, synchronization disabled and bytecode writes disabled. **No tests executed:** uv failed creating its cache lock on the read-only filesystem.
- Inspected fixtures before selecting tests. Did not run `test_restore_run.py`: its module requests `clean_db`, and the PostgreSQL fixture starts a container. [test_restore_run.py:83](/home/user/job-hunter/tests/unit/test_restore_run.py:83), [fixtures.py:148](/home/user/job-hunter/tests/support/fixtures.py:148).
- `git diff --check 1de9493..821d21d` passed; final git status was clean. HEAD was `7601c4a509b3da03179b7a8d4f5d1c2adf273e08`; its only addition beyond the reviewed range was the other reviewer’s round-3 document.
- No network, paid calls, Docker, stacks, Playwright, whole-suite execution, sub-agents, credential reads, or reads of `.secrets/` or `.fixtures-raw/`.

**Not saved** to `docs/council/m5-final-review/rereview3-codex.md`: this environment enforces read-only filesystem access.