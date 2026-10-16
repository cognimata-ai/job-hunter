**Verdict: APPROVE WITH CHANGES.** R3-6, R4-1 and N5 are resolved. One new MAJOR remains in recovery of manifests whose files were never shipped.

Reviewed `git diff 6d51db2 HEAD` (+48/−29) and relevant surrounding contracts. This was a specification review; no implementation tests were run and no files were modified.

| Item | Status | Evidence |
|---|---|---|
| **R3-6 — persist affected jobs; verify before release** | **RESOLVED** | [§19.5 step 3](/home/user/job-hunter/docs/superpowers/specs/2026-10-16-job-hunter-design.md:6173) persists the affected set in both file and database before deletion, copies job IDs onto the fence, and requires retries to reuse that set. [Phase C](/home/user/job-hunter/docs/superpowers/specs/2026-10-16-job-hunter-design.md:6192) makes verification and release transactional. The bounded fence postcondition explicitly checks canonical-field provenance. C38 covers the deletion-to-rebuild crash window. |
| **R4-1 — preserve post-re-enablement data** | **RESOLVED** | The [fence postcondition](/home/user/job-hunter/docs/superpowers/specs/2026-10-16-job-hunter-design.md:6167) is explicitly limited to stored affected identities and historical provenance bounds. Phase B uses and preserves surviving data above those bounds. The [regression requirement](/home/user/job-hunter/docs/superpowers/specs/2026-10-16-job-hunter-design.md:5719) includes fresh postings and observations attached to an affected existing job. |
| **N5 — rebuilds between manifests** | **RESOLVED** | [Phases A–B](/home/user/job-hunter/docs/superpowers/specs/2026-10-16-job-hunter-design.md:6190) perform all destructive operations before any rebuild, then rebuild surviving affected jobs once. Jobs deleted by later manifests are excluded. The purge→tombstone→restore sequence is explicitly required. |
| **Executing manifest without file** | **NOT RESOLVED completely** | [Pending-retry classification](/home/user/job-hunter/docs/superpowers/specs/2026-10-16-job-hunter-design.md:6185), writable manifest storage and final verification resolve the **file-never-written** case. The additional **file-written-but-never-shipped** branch has the identity-ordering gap below. |

**NEW MAJOR — recovered manifest bounds arrive after identity advancement.**

[Migrate steps 1 and 3](/home/user/job-hunter/docs/superpowers/specs/2026-10-16-job-hunter-design.md:6115) advance identities before step 7 reconstructs missing files from database `scope`. The [advancement rule](/home/user/job-hunter/docs/superpowers/specs/2026-10-16-job-hunter-design.md:6196) uses current table maxima and manifests in the file stream; it does not include bounds stored only in pending-retry database rows.

Concrete scenario:

1. A source purge persists `max_ids.job_revisions = 100`, then deletes revisions 91–100 for source-only jobs. A mixed-source job still awaits rebuilding.
2. A backup captures this `executing` manifest, including its hash and scope, but disaster occurs before its file ships. The surviving revision maximum is 90.
3. Restore loads the older shipped stream. Identity advancement restarts revisions at 91.
4. Step 7 reconstructs the missing manifest. Phase B allocates revision 91 for the mixed-source job.
5. Phase C fails: revision 91 exists, although the recovered manifest explicitly requires its deletion. Recovery cannot complete as specified.

**Required correction:** include persisted database-manifest bounds in identity advancement before allocations, or reconstruct missing files before that advancement. Never lower an existing sequence position. Add a restore test for an executing, hash-present manifest absent from the shipped stream, with high IDs already deleted and a surviving job requiring rebuild.

No additional new BLOCKER or MAJOR found in this diff.