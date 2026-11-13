# M5 final re-review, round 4 — Codex GPT-6 Astra

### Verdict

**APPROVE.** Fix E closes both blocking round-3 findings: receipt publication and prune receipt transactions now use the shipment-lock session, and the backup role’s manifest UPDATE permission is replaced by a restricted un-ship function. I found no remaining coverage-loss interleaving in the reviewed paths. Two minor gaps remain in the widened static guard; neither affects the five current purge kinds. PostgreSQL and stack regressions were inspected, not executed. [shipper.py:131](/home/user/job-hunter/backend/src/jobhunter/backup/shipper.py:131), [prune.py:152](/home/user/job-hunter/backend/src/jobhunter/backup/prune.py:152), [migrate.py:166](/home/user/job-hunter/backend/src/jobhunter/db/migrate.py:166).

### Findings mapped

| Finding | Status | Evidence |
|---|---|---|
| **R3-C1 → E1** | **Fixed** | Receipt insertion, prune receipt selection and receipt removal transact on `held.conn`. Removal is bounded by the previously read receipt ID; forget uses IDs from the single listing. [shipper.py:246](/home/user/job-hunter/backend/src/jobhunter/backup/shipper.py:246), [prune.py:160](/home/user/job-hunter/backend/src/jobhunter/backup/prune.py:160), [prune.py:173](/home/user/job-hunter/backend/src/jobhunter/backup/prune.py:173), [prune.py:208](/home/user/job-hunter/backend/src/jobhunter/backup/prune.py:208). |
| **R3-I1 → E2** | **Fixed** | The grants step revokes UPDATE; the function changes only a matching `shipped` row, preserves completion time and clears shipment linkage. [migrate.py:166](/home/user/job-hunter/backend/src/jobhunter/db/migrate.py:166), [0012_m5_unship_manifest.py:51](/home/user/job-hunter/backend/src/jobhunter/db/migrations/versions/0012_m5_unship_manifest.py:51). |
| **N-1** | **Fixed** | A shipper losing its session during upload cannot subsequently publish its older enumeration through another connection. The regression includes another shipper completing meanwhile. [shipper.py:284](/home/user/job-hunter/backend/src/jobhunter/backup/shipper.py:284), [test_shipper.py:333](/home/user/job-hunter/tests/integration/test_shipper.py:333). |
| **N-2** | **Fixed** | The lock-order comment distinguishes the worker’s nested locks from the backup container’s `7261006 → 7261007` pair. [migrate.py:117](/home/user/job-hunter/backend/src/jobhunter/db/migrate.py:117). |
| **N-3** | **Fixed** | The RUNBOOK explains that an empty stream produces no receipt and `no_receipt` persists until the first deletion ships. [RUNBOOK.md:558](/home/user/job-hunter/docs/RUNBOOK.md:558). |
| **N-4** | **Fixed** | The older-set restore note explains stale receipts, delayed prune repair, per-deletion alerts and subsequent re-upload; the restore procedure links to it. [RUNBOOK.md:574](/home/user/job-hunter/docs/RUNBOOK.md:574), [RUNBOOK.md:705](/home/user/job-hunter/docs/RUNBOOK.md:705). |
| **N-5** | **Partly fixed** | All three reported forms are caught. Conditional content propagation and identity checks behaved correctly in my probes, but module-call argument handling has the two minor gaps below. [test_kind_bounds_declared.py:360](/home/user/job-hunter/tests/unit/test_kind_bounds_declared.py:360), [test_kind_bounds_declared.py:798](/home/user/job-hunter/tests/unit/test_kind_bounds_declared.py:798). |

**E1 interleavings.** A session lost during upload cannot write the subsequent receipt: the transaction uses that same connection without reconnection. If COMMIT succeeds before the connection fails, the receipt committed while serialization still held; an uncertain client outcome does not invalidate coverage. After a prune’s confirmation, lock loss can still allow its external forget to proceed, but its arguments remain the previously listed IDs. A newly arriving shipment is excluded, and an earlier uploader whose session died cannot later receipt a listed snapshot. [shipper.py:131](/home/user/job-hunter/backend/src/jobhunter/backup/shipper.py:131), [shipper.py:251](/home/user/job-hunter/backend/src/jobhunter/backup/shipper.py:251), [prune.py:184](/home/user/job-hunter/backend/src/jobhunter/backup/prune.py:184).

Clock steps change which redundant snapshots qualify as older; the latest receipt is selected by ID, and its complete snapshot remains the anchor. Partial uploads receive no receipt, and restore still merges every retained manifest snapshot. These properties preserve acknowledged coverage under the reviewed concurrent ship/prune paths. [prune.py:89](/home/user/job-hunter/backend/src/jobhunter/backup/prune.py:89), [prune.py:208](/home/user/job-hunter/backend/src/jobhunter/backup/prune.py:208), [restic.py:177](/home/user/job-hunter/backend/src/jobhunter/backup/restic.py:177), [restore.py:323](/home/user/job-hunter/backend/src/jobhunter/backup/restore.py:323).

The pre-upload engine read is acceptable: it only chooses whether to upload. It cannot publish a receipt after losing the lock, because publication remains on the original session. The unchanged-stream branch writes nothing. [shipper.py:223](/home/user/job-hunter/backend/src/jobhunter/backup/shipper.py:223), [shipper.py:278](/home/user/job-hunter/backend/src/jobhunter/backup/shipper.py:278).

**E2 security and compatibility.** The migrator creates the function; its pinned `public, pg_temp` path places temporary objects after the protected public schema, and the updated table is schema-qualified. The argument is a qualified bigint parameter, with no dynamic SQL. Unknown or NULL IDs and non-`shipped` states match nothing and return false. PUBLIC execution is revoked; the grants step restores that restriction after `--no-acl` restores. [0012_m5_unship_manifest.py:16](/home/user/job-hunter/backend/src/jobhunter/db/migrations/versions/0012_m5_unship_manifest.py:16), [0012_m5_unship_manifest.py:51](/home/user/job-hunter/backend/src/jobhunter/db/migrations/versions/0012_m5_unship_manifest.py:51), [10-init.sh:59](/home/user/job-hunter/deploy/postgres/init/10-init.sh:59), [migrate.py:169](/home/user/job-hunter/backend/src/jobhunter/db/migrate.py:169).

This is appropriately restricted for the chosen un-ship operation. A compromised backup role still has its **pre-existing** ability to forge receipt rows and thus offsite acknowledgement for completed manifests; E2 does not eliminate that trust boundary. It does eliminate the additional ability to complete an unfinished purge or edit its completion time directly. [migrate.py:94](/home/user/job-hunter/backend/src/jobhunter/db/migrate.py:94), [migrate.py:159](/home/user/job-hunter/backend/src/jobhunter/db/migrate.py:159), [reconcilers.py:57](/home/user/job-hunter/backend/src/jobhunter/privacy/reconcilers.py:57).

0012 is an additive function migration with floor 0011. The rollback re-grant remains a real limitation, explicitly accepted by F-7; the next upgrade revokes it again. Schema-head, previous-image and drill references consistently identify 0012. [0012_m5_unship_manifest.py:28](/home/user/job-hunter/backend/src/jobhunter/db/migrations/versions/0012_m5_unship_manifest.py:28), [progress.md:145](/home/user/job-hunter/.superpowers/sdd/2026-11-09-m5-operations-privacy-production/progress.md:145), [build-prev-image.sh:24](/home/user/job-hunter/scripts/build-prev-image.sh:24), [test_prod_drills.py:87](/home/user/job-hunter/tests/stack/test_prod_drills.py:87).

I found no additional material drift in the reviewed spec/RUNBOOK/PRIVACY guarantees. The HTTP-client fix closes clients after constructor failure and through per-test cleanup. [spec:6374](/home/user/job-hunter/docs/superpowers/specs/2026-10-16-job-hunter-design.md:6374), [PRIVACY.md:129](/home/user/job-hunter/docs/PRIVACY.md:129), [prodsite.py:117](/home/user/job-hunter/tests/support/prodsite.py:117), [test_prod_restore.py:280](/home/user/job-hunter/tests/stack/test_prod_restore.py:280), [test_prod_drills.py:170](/home/user/job-hunter/tests/stack/test_prod_drills.py:170).

### New findings

**Critical:** None.

**Important:** None.

**Minor — R4-M1: variadic helpers bypass content tracking.**

`content_uses` permits passing content to any recognized module function, but `given_content` propagates only ordinary positional parameters and named keywords. A variadic receiver therefore loses tracking. [test_kind_bounds_declared.py:401](/home/user/job-hunter/tests/unit/test_kind_bounds_declared.py:401), [test_kind_bounds_declared.py:461](/home/user/job-hunter/tests/unit/test_kind_bounds_declared.py:461).

I planted this in the actual `JobTombstone` syntax tree; the guard returned **no violations**:

```python
# In execute:
_probe_read(content)

# Module helper:
def _probe_read(*args):
    c = args[0]
    k = "max_ids"
    c[k]["planted"]
```

A future helper can consequently read an undeclared bound without failing the guard. **Fix:** reject content arguments whose receiving parameter cannot be tracked, or propagate through variadic containers; add this regression. The existing five-kind checks still passed. [test_kind_bounds_declared.py:589](/home/user/job-hunter/tests/unit/test_kind_bounds_declared.py:589), [test_kind_bounds_declared.py:640](/home/user/job-hunter/tests/unit/test_kind_bounds_declared.py:640).

**Minor — R4-M2: valid keyword helper calls are rejected.**

For `read(c=content)`, the content node’s parent is `ast.keyword`, not `ast.Call`. The new allowance therefore reports “unhandled use of the content,” even though `given_content` tracks the receiving keyword parameter. My probe reproduced this with an otherwise declared bound read. [test_kind_bounds_declared.py:409](/home/user/job-hunter/tests/unit/test_kind_bounds_declared.py:409), [test_kind_bounds_declared.py:461](/home/user/job-hunter/tests/unit/test_kind_bounds_declared.py:461).

**Fix:** resolve keyword nodes to their enclosing call before checking the allowance, and add a passing keyword-call control.

### What I ran

- Read the brief, both round-3 reviews, F-1–F-7, the implementer’s report, relevant binding spec sections, and the four commits through `git show`.
- Attempted the permitted focused pytest command with offline operation, synchronization disabled and bytecode writes disabled. **No pytest tests executed:** uv could not create its cache-lock temporary file on the read-only filesystem.
- Ran direct in-process checks using `.venv/bin/python -B`: all **five kind checks, 21 planted cases, the 0012 floor check and the backup lock-order check passed**. These invoked the existing test functions without pytest fixtures.
- Ran additional AST probes for conditional propagation, identity checks, variadic helpers and keyword calls; reproduced R4-M1 in the actual kind’s syntax tree.
- Probed the actual receipt transaction boundaries with a simulated dead connection: insertion, selection and removal each raised `ShipLockLost`. Probed prune orchestration with a shipment arriving after confirmation: only the previously listed older ID was forgotten. These were in-memory checks, not PostgreSQL integration tests.
- Inspected fixtures before selecting checks. Did not execute integration, restore or stack tests whose fixtures start containers. [fixtures.py:148](/home/user/job-hunter/tests/support/fixtures.py:148).
- `git diff --check 5e2bc85..ac20fd3` passed. HEAD was `ac20fd312f6436b378dbadfe358771957c0d3943`; git status remained unchanged.
- No network, paid calls, Docker, sub-agents, or reads of `.secrets/` or `.fixtures-raw/`.

**Not saved:** filesystem access is enforced read-only. The pre-existing untracked `docs/council/m5-final-review/rereview4-codex.md` was left untouched.