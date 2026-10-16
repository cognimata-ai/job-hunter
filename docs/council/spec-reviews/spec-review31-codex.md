**Verdict: APPROVE WITH CHANGES.** Must fix **R3-6** and new **R4-1** below. The two previous blockers are resolved; I found no new blocker in the 3.1 diff.

Reviewed `git diff adc1335 HEAD` (+106/−69), with surrounding specification text for context. This was a specification review plus executable admission-arithmetic checks; database migrations and services were not executed. No files were modified.

| Prior item | Status | Evidence |
|---|---|---|
| **R3-1 — bootstrap grants** | **RESOLVED** | [§19.3](/home/user/job-hunter/docs/superpowers/specs/2026-10-16-job-hunter-design.md:6106) advances existing identities, creates the schema, advances newly created identities, then grants permissions. The empty-schema path is explicit, and migrations cannot allocate identity rows before advancement. |
| **R3-5 — typed manifest replay** | **RESOLVED** | [§19.5](/home/user/job-hunter/docs/superpowers/specs/2026-10-16-job-hunter-design.md:6153) distinguishes `row_delete`, `field_redact`, `file_remove` and `fence`, with operation-specific verification. A redacted profile remains a valid referenced row; verification no longer demands its deletion. |
| **N8 — canceled items rebatched** | **RESOLVED** | [§10.10](/home/user/job-hunter/docs/superpowers/specs/2026-10-16-job-hunter-design.md:3935) excludes `batch_canceled`; the canceled-result mapping assigns that reason. Historical batch-result exclusion preserves the restriction after `state_reason` changes. |
| **R3-2 — released-batch singleton conflict** | **RESOLVED** | [The partial index](/home/user/job-hunter/docs/superpowers/specs/2026-10-16-job-hunter-design.md:2547) excludes every row with `released_at` set. [The lifecycle](/home/user/job-hunter/docs/superpowers/specs/2026-10-16-job-hunter-design.md:3996) preserves that timestamp through `ended → reconciled`, settles spend without changing targets, and prevents a second follow-on submission. |
| **R3-3 — admission exceeds S** | **RESOLVED** | [§9.6.2](/home/user/job-hunter/docs/superpowers/specs/2026-10-16-job-hunter-design.md:3616) trims excess whole-credit slots before distributing remainders. The original `8/2/1` case now allocates `7/2/1`. Exact-arithmetic checks passed 100,000 changing-size/changing-take cycles and rolling ten-cycle shares under permanent backlog for `S=1…30`. |
| **R3-4 — golden snapshot provenance** | **RESOLVED** | [§7.13](/home/user/job-hunter/docs/superpowers/specs/2026-10-16-job-hunter-design.md:2614) preserves fact provenance and `det_input_sources`; [§20.5](/home/user/job-hunter/docs/superpowers/specs/2026-10-16-job-hunter-design.md:6460) deletes salary-only-tainted cases, their results and evaluation calls, including manifest coverage. |
| **R3-6 — rebuild before fence release** | **NOT RESOLVED** | Ordering is corrected, but the new replay contract does not durably preserve the affected-job set. The original mixed-source salary scenario can still escape rebuilding after a crash, as detailed below. |

**R3-6 remaining — MAJOR: replay can lose the jobs it must rebuild.**

The new [`fence` operation](/home/user/job-hunter/docs/superpowers/specs/2026-10-16-job-hunter-design.md:6157) stores the source and rebuilds jobs matching its fence after the other operations. However, [source-fence matching](/home/user/job-hunter/docs/superpowers/specs/2026-10-16-job-hunter-design.md:2725) depends on surviving source postings or revision lineage.

Concrete scenario:

1. A job has JSearch-only text lineage and an Adzuna-derived canonical salary.
2. Replay deletes its Adzuna posting, affected deterministic results and golden cases.
3. Replay crashes before rebuilding the job.
4. On restart, the job has neither an Adzuna posting nor Adzuna revision lineage. It no longer matches the reconstructed source fence.
5. Replay can release the fence without clearing the canonical salary. The listed postcondition does not independently check canonical-field provenance.

**Must fix:** persist the affected job IDs before destructive operations, retain them across replay retries, and verify canonical fields/provenance for those jobs before release. Add a crash test specifically between source-data deletion and rebuild.

**R4-1 — NEW MAJOR: old source manifests also govern newly collected data.**

The new [fence postcondition](/home/user/job-hunter/docs/superpowers/specs/2026-10-16-job-hunter-design.md:6157) prohibits source postings and derivatives across matching jobs without the `max_ids` restriction applied to `row_delete`. Completed/shipped manifests are checked again on [every migration](/home/user/job-hunter/docs/superpowers/specs/2026-10-16-job-hunter-design.md:6114).

Concrete scenario: an Adzuna purge completes; the user later [re-enables Adzuna through new settings](/home/user/job-hunter/docs/superpowers/specs/2026-10-16-job-hunter-design.md:6466); collection creates a new Adzuna job above the manifest’s identity bounds. On the next migration, the old fence postcondition fails. Bounded deletes correctly preserve the new posting, but rebuilding from that posting still produces Adzuna-derived content. Replay cannot satisfy its postcondition without deleting permitted new data, so migration can fail and prevent startup.

**Must fix:** bound rebuild selection and verification to the historical purge’s affected identities and provenance generation. Explicitly preserve data collected after re-enablement, including fresh postings attached to an existing job. Add a purge → re-enable → collect → migrate/replay regression test.