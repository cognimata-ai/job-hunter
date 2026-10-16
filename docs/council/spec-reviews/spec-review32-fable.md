# Spec review, round 5 (revision 3.2) — Claude Fable 5.1

Commit 670d016, read as the full diff 6d51db2→670d016 (restore-replay subsystem).

**Verdict: APPROVE WITH CHANGES** — one must-fix MAJOR (N6), a one-line consequence of separating phase A from phase B.

## 1. My items

| Item | Status | Evidence |
|---|---|---|
| N5 replay rebuild vs later manifests | **RESOLVED** | §19.5 replay rules: phase A runs every destructive operation of the whole stream in order with all replayed fences active ("no rebuild runs between manifests"); phase B rebuilds once over the union of stored affected `job_ids` minus jobs phase A deleted; phase C verifies and releases. §19.3 step 7, §20.4 note, §20.5 step 6, my exact sequence as a test ("Purge, then tombstone, then restore") and in V22. |
| `executing` manifest without file | **RESOLVED** | Classification "pending retry": completed from `scope` when `file_sha256` is set, otherwise computed under the restored fence; `purge_fences` reconciler re-queues a missing operation; restore step 8 accepts it only after completion; C39. |

**Deviations**
- *`manifests` mount read-write for `migrate`* — **ACCEPT.** Required for the pending-retry file; it is a one-shot container, and the write path is the same tmp → fsync → rename protocol. State that `migrate` never deletes a manifest file.
- *Only replayed manifests' fences active in phase A* — **ACCEPT.** Checked manifests have nothing to protect, `api`/`worker` are down for the whole of step 7, and a reconstructed fence is released only in phase C.
- *Verification from stored provenance, bounded by the affected set and watermark* — **ACCEPT.** Stable across releases, cheap to repeat, and it is what keeps re-enabled data (above the watermark) safe from replay; phase B recomputes the provenance it reads, so the check is meaningful after the rebuild.

## 2. New defect

**N6 — MAJOR. Phase A commits with a dangling `jobs.current_revision_id`.** A source purge's manifest records the deletion of each tainted revision as `row_delete job_revisions [R₀]` (§20.2 "every deletion as `row_delete`"; §20.5 step 2 finds the tainted revisions before anything is deleted). In normal operation that delete and the insert of the rebuilt revision happen in one per-job transaction (§20.5 step 5), so the deferred FK `jobs.current_revision_id → job_revisions` (§7.9, `DEFERRABLE INITIALLY DEFERRED`, no `SET NULL`) is satisfied at commit. Phase A now runs the delete in its own transaction, and phase B inserts the new revision later. Scenario: the common mixed-source job J whose current revision R₀ carries Adzuna lineage (Adzuna supplied the description); a restore replays the purge; phase A deletes R₀ while `J.current_revision_id = R₀`; the phase-A transaction fails its deferred FK check at commit; `migrate` stops with the fences active. Fix: in phase A, before deleting a job's current revision, set `jobs.current_revision_id := NULL` exactly as tombstoning step 6 does (record it as a `field_redact` on `jobs.current_revision_id` for the affected jobs, so the postcondition stays checkable); phase B sets it again. Add the case to the "Typed replay" test (a mixed job whose *current* revision is tainted).

Otherwise clean: affected set and watermark stored before any delete; `is_fenced` on the stored `job_ids`; phase C the only release point.

## 3. Verdict

**APPROVE WITH CHANGES.** Must fix: N6 (null `jobs.current_revision_id` in phase A before deleting a tainted current revision).
