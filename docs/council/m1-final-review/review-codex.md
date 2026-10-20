### Verdict

REJECT — the branch still contains personal names, permits unfenced worker writes, and has cross-task defects in availability, duplicate grouping, and SmartRecruiters refreshes.

### Focus questions 1–8

1. **Crash safety end to end.** The main paid-page protocol is sound: reservation precedes dispatch marking; settlement and response persistence share a transaction; parsing, identity and embeddings have replay guards; finalization commits coverage, report and terminal state together. A crash after sending but before settlement can lose the response and leave a charged ambiguous attempt—an intentional limitation, not evidence for refunding or resending JSearch/SerpApi. I found no new stranded-run path. Known informational-counter limitations remain. See [acquire.py:661](/home/user/job-hunter/backend/src/jobhunter/sources/acquire.py:661), [identity.py:593](/home/user/job-hunter/backend/src/jobhunter/dedup/identity.py:593), [embeddings.py:365](/home/user/job-hunter/backend/src/jobhunter/embeddings.py:365), [finalize.py:618](/home/user/job-hunter/backend/src/jobhunter/graphs/collect/nodes/finalize.py:618).

2. **Lock order across modules.** I found no concrete deadlock cycle in the reviewed planner/acquisition, identity/merge, grouping, availability, filtering, embedding and finalization paths. Grouping takes its company advisory lock before locking the affected jobs; availability locks jobs before its owner guard and posting/snapshot writes. However, startup synchronization and retention bypass epoch fencing, so the overall ownership guarantee fails. See [groups.py:517](/home/user/job-hunter/backend/src/jobhunter/dedup/groups.py:517), [snapshots.py:329](/home/user/job-hunter/backend/src/jobhunter/availability/snapshots.py:329), and Critical C2.

3. **Coverage truthfulness.** No new false-covered-span defect found. Arbeitnow requires a fresh traversal reaching END; capped reads prove no span. JSearch cursor reuse expires at 24 hours. Search timestamps require persisted coverage evidence, while structurally complete ATS snapshots establish board coverage. Abandonment remains distinguishable from successful reads and permits contiguity to advance as specified. The ledger’s known END-without-coverage counter issue remains excluded. See [arbeitnow.py:229](/home/user/job-hunter/backend/src/jobhunter/sources/arbeitnow.py:229), [planner.py:401](/home/user/job-hunter/backend/src/jobhunter/quota/planner.py:401), [coverage.py:652](/home/user/job-hunter/backend/src/jobhunter/sources/coverage.py:652).

4. **Availability safety.** **Not fully safe.** Normal absence counting is snapshot-idempotent; incomplete traversals do not count absences; the stale sweep affects non-ATS postings and makes them stale rather than closed. But an incomplete snapshot can erase the population used to detect a subsequent mass disappearance, permitting ordinary closure without mass-event confirmation. This challenges T19-b as unsafe; see Important I1. Evidence: [snapshots.py:231](/home/user/job-hunter/backend/src/jobhunter/availability/snapshots.py:231), [rules.py:198](/home/user/job-hunter/backend/src/jobhunter/availability/rules.py:198).

5. **Identity.** I found no new posting-loss or duplicate-posting path in strong-key attachment, exclusive-conflict handling or unmerge. However, attaching or merging an ATS key into an existing group member can violate G1 permanently: grouping is driven by revision changes, and representative revalidation checks only G2–G5. Distinct vacancies can therefore remain grouped. See [merge.py:348](/home/user/job-hunter/backend/src/jobhunter/dedup/merge.py:348), [groups.py:195](/home/user/job-hunter/backend/src/jobhunter/dedup/groups.py:195), and Important I2.

6. **Quota.** I found no new I3 overspend path: authoritative admission locks the window and counts reservations; dispatch verifies ownership; refunds require explicit evidence. SerpApi’s lag-aware hourly calculation and renewal handling avoid a new permanent-lockout finding. The ledger already records the rollover race that can falsely exhaust the new SerpApi window; I am not re-reporting it. Separately, missing-key safety is vulnerable through unfenced startup synchronization. See [ledger.py:614](/home/user/job-hunter/backend/src/jobhunter/quota/ledger.py:614), [ledger.py:304](/home/user/job-hunter/backend/src/jobhunter/quota/ledger.py:304), [ledger.py:721](/home/user/job-hunter/backend/src/jobhunter/quota/ledger.py:721), Critical C2.

7. **Reports, logs and alerts.** Reports contain counts, identifiers and codes rather than ad text or company names. **Company names can appear in HTTP logs through ATS hostnames and board-slug paths**, consistent with the spec’s request-URL logging requirement; credentials are excluded. Known unresolved-alert cases remain as recorded in the ledger. The fixture privacy gate nevertheless fails on surviving personal names, despite all 131 existing fixture checks passing. See [finalize.py:519](/home/user/job-hunter/backend/src/jobhunter/graphs/collect/nodes/finalize.py:519), [http.py:241](/home/user/job-hunter/backend/src/jobhunter/sources/http.py:241), Critical C1.

8. **Plan drift.** SmartRecruiters’ promised new/changed/seven-day detail refresh policy is not integrated: its persisted markers have readers but no writers. This is missing M1 functionality, not a documented deferral. I found no additional unjustified expansion into deferred LLM, UI or purge functionality; the documented embedding and reporting deviations remain accepted. See [traverse.py:98](/home/user/job-hunter/backend/src/jobhunter/sources/traverse.py:98), [smartrecruiters.py:232](/home/user/job-hunter/backend/src/jobhunter/sources/ats/smartrecruiters.py:232), Important I3.

### Strengths

- Durable receipts connect quota settlement, parsing, identity and finalization well.
- Conservative handling of ambiguous paid requests, malformed envelopes and incomplete ATS traversals protects spend and availability.
- Explicit lock helpers, transition enforcement and replay guards make the implementation auditable.
- The ledger clearly distinguishes decisions, accepted limitations and unfinished work.

### Issues

#### Critical (Must Fix)

**C1 — Real personal names remain in committed fixtures.**

Evidence: [personio_everphone.xml:62](/home/user/job-hunter/tests/contract/fixtures/ats/personio_everphone.xml:62), [personio_entity.xml:65](/home/user/job-hunter/tests/contract/fixtures/ats/personio_entity.xml:65), [test_fixture_pii.py:69](/home/user/job-hunter/tests/contract/test_fixture_pii.py:69).

The recorded description contains the founder’s full name and subsequent first-name references. The derived entity fixture preserves them. These files pass the existing privacy checks because those checks cover emails, phones, credentials and tracking parameters, but not names. This directly violates the review’s binding fixture constraint.

**Fix:** Replace all identifying references with consistent placeholders in both fixtures, extend scrubbing and regression coverage for founder/CEO narrative names, and audit the affected reachable branch history before merge. Do not limit the correction to contact-marker names.

**C2 — Unfenced worker writes can undo missing-secret protection after takeover.**

Evidence: [main.py:245](/home/user/job-hunter/backend/src/jobhunter/worker/main.py:245), [instances.py:103](/home/user/job-hunter/backend/src/jobhunter/sources/instances.py:103), [registry.py:86](/home/user/job-hunter/backend/src/jobhunter/sources/registry.py:86), [jsearch.py:283](/home/user/job-hunter/backend/src/jobhunter/sources/jsearch.py:283). Retention has the same fence omission at [retention.py:184](/home/user/job-hunter/backend/src/jobhunter/privacy/retention.py:184) and [retention.py:221](/home/user/job-hunter/backend/src/jobhunter/privacy/retention.py:221).

Concrete sequence: worker A loads a present key and pauses before startup synchronization; its lock session is lost; worker B takes over with that secret absent and disables the source. Before its watchdog terminates it, A resumes and restores the instance to `ok` using its stale configuration. B can now reserve and dispatch using its empty key: adapter construction relies on the database disable having held.

Retention likewise continues domain deletions and alert writes without checking its epoch. The watchdog’s eventual exit is not a transactional fence.

**Fix:** Fence startup synchronization and every retention domain-write transaction with the worker epoch; abort maintenance on `FencedOut`. Add a local nonempty-credential check before paid dispatch as defence in depth. Test the stale-startup interleaving explicitly.

#### Important (Should Fix)

**I1 — An incomplete snapshot bypasses mass-disappearance protection.**

Evidence: [snapshots.py:231](/home/user/job-hunter/backend/src/jobhunter/availability/snapshots.py:231), [snapshots.py:345](/home/user/job-hunter/backend/src/jobhunter/availability/snapshots.py:345), [snapshots.py:366](/home/user/job-hunter/backend/src/jobhunter/availability/snapshots.py:366).

Start with a complete board and baseline 20. An incomplete traversal misses those postings and makes them `unknown`. The next complete empty snapshot counts **zero** disappeared postings because no mass event is open and none is currently active. It takes the ordinary-absence path and resets the baseline to zero. A second empty complete snapshot 24 hours later closes all 20 through `ats_absent`, without the opening event plus two later confirmations or its warning.

An offline probe confirmed the classification failure. **This explicitly challenges accepted Ruling T19-b and its addendum:** current-state filtering loses historical baseline evidence.

**Fix:** Preserve enough baseline-population evidence that an incomplete traversal cannot remove those postings from subsequent mass-disappearance detection. Add the complete → incomplete → empty-complete sequence as an integration regression.

**I2 — ATS-key changes can leave distinct same-board jobs grouped.**

Evidence: [groups.py:195](/home/user/job-hunter/backend/src/jobhunter/dedup/groups.py:195), [groups.py:457](/home/user/job-hunter/backend/src/jobhunter/dedup/groups.py:457), [groups.py:621](/home/user/job-hunter/backend/src/jobhunter/dedup/groups.py:621), [merge.py:350](/home/user/job-hunter/backend/src/jobhunter/dedup/merge.py:350).

An aggregator job A represents a group containing ATS-board-X job B. A later legitimately acquires a different posting from board X through a strong key. A and B now violate G1. If material content is unchanged, no revision occurrence schedules regrouping. Even when A is regrouped, representative revalidation checks only G2–G5 and retains B. An offline probe confirmed that this revalidation accepts a pair rejected by G1.

The result conflates distinct vacancies and can later suppress a job through duplicate handling.

**Fix:** Schedule group validation on key/posting membership changes as well as revision changes. Validate G1 against all members, including representative and merge-target changes, while preserving the existing job-lock order.

**I3 — SmartRecruiters detail refresh markers are never persisted.**

Evidence: [traverse.py:98](/home/user/job-hunter/backend/src/jobhunter/sources/traverse.py:98), [traverse.py:399](/home/user/job-hunter/backend/src/jobhunter/sources/traverse.py:399), [smartrecruiters.py:253](/home/user/job-hunter/backend/src/jobhunter/sources/ats/smartrecruiters.py:253), [identity.py:158](/home/user/job-hunter/backend/src/jobhunter/dedup/identity.py:158).

After the first successful collection, both `postings.list_item_sha256` and `detail_fetched_at` remain NULL: production code never writes either field. The next run therefore supplies an empty `known` map and fetches every in-scope detail again. The implemented cache predicate never becomes effective, increasing request volume and collection time on every run.

**Fix:** Persist the originating list-item hash and successful detail-fetch timestamp under the owner fence, with replay-safe updates. Verify two unchanged successive collections skip details, while changed hashes and details older than seven days trigger refreshes.

#### Minor (Nice to Have)

No additional minor findings; ledger-deferred items are not duplicated here.

### Declined to judge

- Live provider behaviour and billing: no external API or live tests were run.
- Full PostgreSQL/crash-suite execution: not rerun; verification used static tracing, offline probes and the existing fixture checks invoked directly.
- VPS embedding throughput and documented later-milestone work: accepted deployment prerequisites and deferrals were not reopened.
- Remote copies of previously rewritten history: not inspected; this review covered the local range and working tree.

The working tree, index and HEAD remain unchanged at `0cd6711`.