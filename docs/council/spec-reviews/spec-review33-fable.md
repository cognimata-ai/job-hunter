# Spec review, round 6 (revision 3.3) — Claude Fable 5.1

Commit e2c351f, read as the full diff 670d016→e2c351f.

**Verdict: APPROVE.**

## 1. N6 — RESOLVED

- §19.5 phase A: a source purge's operations `field_redact` `jobs.current_revision_id` for every affected job whose current revision is tainted, **before** the `row_delete` of that revision, so each phase-A transaction commits without a dangling deferred key; phase B sets the column again.
- §20.5 step 5 applies the same order in normal operation (one transaction per job: null, delete tainted revisions, insert, re-point), so the manifest's operation list matches what the purge actually did.
- The `field_redact` postcondition for this column is correctly special-cased ("names none of the revisions the manifest deletes") rather than "is NULL", so phase C passes after the rebuild.
- §7.17 invariant 1 exempts fenced jobs; §7.18 `current_scores` filters `current_revision_id IS NOT NULL`; the jobs list and detail treat such a job as absent; the state is reachable only inside migrate step 7 or after a failed replay, with `api`/`worker` stopped. Test "Tainted current revision" covers the sequence.

## 2. New defects

None at BLOCKER or MAJOR level. The identity-ordering fix (migrate step 1 rewrites files from `scope` before advancing; bounds include database-only manifests; an identity is never lowered; a pending-retry manifest without `file_sha256` has no bounds because nothing was deleted under it) is consistent with §19.5 and C39.

One note, not blocking: between phases A and B a published result of an untainted revision whose Adzuna-sourced `det_results` instance phase A deleted (§20.5 step 3) has no current instance. Phase B's canonical-field recompute re-creates it (§8.8); if a job's canonical fields are unchanged, the `det_completeness` reconciler (§17.4) inserts it within 10 min of start. Stating in phase B that the rebuild recomputes current det instances for every affected job would remove the dependence on the reconciler.

## 3. Verdict

**APPROVE.** No must-fix items remain from my side.
