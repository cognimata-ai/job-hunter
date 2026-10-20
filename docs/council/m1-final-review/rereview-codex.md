### Verdict

APPROVE — all 15 original findings are fixed at `4b71f80`; no blocking regression found, with one minor documentation correction below.

### Finding verdicts

Verdicts reflect code inspection against the ledger rulings; test references were inspected, not executed.

- **Codex C1 — FIXED (FR-a/e/f/m/n):** identifying narrative text replaced; narrative/contact checks added to the gate. Evidence: [personio_entity.xml:65](/home/user/job-hunter/tests/contract/fixtures/ats/personio_entity.xml:65), [test_fixture_pii.py:81](/home/user/job-hunter/tests/contract/test_fixture_pii.py:81), [cli_fixtures.py:472](/home/user/job-hunter/backend/src/jobhunter/cli_fixtures.py:472).
- **Codex C2 — FIXED (FR-b/h/i/l/r/t):** startup synchronization and retention writes are epoch-fenced; paid adapters refuse missing credentials. Evidence: [instances.py:126](/home/user/job-hunter/backend/src/jobhunter/sources/instances.py:126), [retention.py:196](/home/user/job-hunter/backend/src/jobhunter/privacy/retention.py:196), [http.py:285](/home/user/job-hunter/backend/src/jobhunter/sources/http.py:285).
- **Codex I1 — FIXED (FR-d/j):** disappearance detection retains the complete-snapshot baseline across incomplete reads. Evidence: [snapshots.py:135](/home/user/job-hunter/backend/src/jobhunter/availability/snapshots.py:135), [snapshots.py:243](/home/user/job-hunter/backend/src/jobhunter/availability/snapshots.py:243), [snapshots.py:424](/home/user/job-hunter/backend/src/jobhunter/availability/snapshots.py:424).
- **Codex I2 — FIXED (FR-j/k):** key/merge changes schedule regrouping; revalidation checks G1 against every retained member. Evidence: [groups.py:218](/home/user/job-hunter/backend/src/jobhunter/dedup/groups.py:218), [groups.py:343](/home/user/job-hunter/backend/src/jobhunter/dedup/groups.py:343).
- **Codex I3 — FIXED:** identity persists both SmartRecruiters markers under its owner fence. Evidence: [identity.py:186](/home/user/job-hunter/backend/src/jobhunter/dedup/identity.py:186), [identity.py:543](/home/user/job-hunter/backend/src/jobhunter/dedup/identity.py:543).
- **Fable Important 1 — FIXED:** persisted markers now feed the unchanged/seven-day detail predicate. Evidence: [identity.py:543](/home/user/job-hunter/backend/src/jobhunter/dedup/identity.py:543), [smartrecruiters.py:257](/home/user/job-hunter/backend/src/jobhunter/sources/ats/smartrecruiters.py:257).
- **Fable Important 2 — FIXED:** continuation persistence and reuse enforce ≥7 new IDs and page ≤2. Evidence: [jsearch.py:83](/home/user/job-hunter/backend/src/jobhunter/sources/jsearch.py:83), [coverage.py:615](/home/user/job-hunter/backend/src/jobhunter/sources/coverage.py:615), [planner.py:410](/home/user/job-hunter/backend/src/jobhunter/quota/planner.py:410).
- **Fable Important 3 — FIXED:** fallback account observations persist before planning, making stale-renewal detection reachable without a search. Evidence: [plan_fallback.py:103](/home/user/job-hunter/backend/src/jobhunter/graphs/collect/nodes/plan_fallback.py:103), [ledger.py:573](/home/user/job-hunter/backend/src/jobhunter/quota/ledger.py:573).
- **Fable Minor 1 — FIXED:** the known-ID count runs inside the retried settlement transaction. Evidence: [acquire.py:676](/home/user/job-hunter/backend/src/jobhunter/sources/acquire.py:676), [acquire.py:713](/home/user/job-hunter/backend/src/jobhunter/sources/acquire.py:713).
- **Fable Minor 2 — FIXED:** I3 uses the latest counted admission’s persisted cap. Evidence: [ledger.py:188](/home/user/job-hunter/backend/src/jobhunter/quota/ledger.py:188), [finalize.py:253](/home/user/job-hunter/backend/src/jobhunter/graphs/collect/nodes/finalize.py:253).
- **Fable Minor 3 — FIXED:** persisted projection reads reach foreign-usage resolution without a subsequent search. Evidence: [plan_fallback.py:106](/home/user/job-hunter/backend/src/jobhunter/graphs/collect/nodes/plan_fallback.py:106), [ledger.py:391](/home/user/job-hunter/backend/src/jobhunter/quota/ledger.py:391).
- **Fable Minor 4 — FIXED:** blocked breakers prevent projection and post-search account reads. Evidence: [plan_fallback.py:68](/home/user/job-hunter/backend/src/jobhunter/graphs/collect/nodes/plan_fallback.py:68), [traverse.py:343](/home/user/job-hunter/backend/src/jobhunter/sources/traverse.py:343).
- **Fable Minor 5 — FIXED:** named dead production helpers were removed or moved to test support; no-seek wording corrected. Evidence: [plan:3176](/home/user/job-hunter/docs/superpowers/plans/2026-10-17-m1-collection-pipeline.md:3176), [plan:3191](/home/user/job-hunter/docs/superpowers/plans/2026-10-17-m1-collection-pipeline.md:3191).
- **Fable Minor 6 — FIXED:** fetch metrics exclude replayed receipts and count newly written planning skips after commit. Evidence: [traverse.py:215](/home/user/job-hunter/backend/src/jobhunter/sources/traverse.py:215), [fetch.py:211](/home/user/job-hunter/backend/src/jobhunter/graphs/collect/nodes/fetch.py:211), [plan.py:52](/home/user/job-hunter/backend/src/jobhunter/graphs/collect/nodes/plan.py:52).
- **Fable Minor 7 — FIXED:** the complete 231-word German list is present; the English list contains 127 words. Evidence: [stopwords_de.txt:201](/home/user/job-hunter/backend/src/jobhunter/normalize/data/stopwords_de.txt:201).

Privacy inspection found no surviving real personal names in the 131 tracked fixture files or 63 tracked prose documents. Narrative/contact scans reported zero findings; the 11 removed identity spellings and removed publisher identity/host also had zero remaining matches. FR-a’s historical-commit exception remains applicable.

### New issues

**Critical:** None found.

**Important:** None found.

**Minor — continuation documentation omits failed reads.** [coverage.py:643](/home/user/job-hunter/backend/src/jobhunter/sources/coverage.py:643) and [plan:3189](/home/user/job-hunter/docs/superpowers/plans/2026-10-17-m1-collection-pipeline.md:3189) describe continuation as a page the run “did not read.” Sequence: page 1 qualifies, page 2 fails or becomes ambiguous, and finalization retains page 1’s eligible cursor because it selects the last successful page. The wording can mislead maintainers about subsequent requests. Change it to “not successfully read” and mention failed/ambiguous attempts. No retry-safety defect established.

No additional defect found in worker-stop classification/exit codes, Arbeitnow fill-time/Age handling, post-semaphore timeout scope, or migration 0005 amendments.

### Declined to judge

- Test execution: after checking for concurrent pytest, the focused PII command failed before collection because `uv` could not create its cache lock on the read-only filesystem.
- Live provider behaviour, billing, deployment throughput, and full PostgreSQL/crash-suite results: not exercised; no external calls made.
- Historical/remote privacy cleanup: explicitly deferred by FR-a.

Working tree, index, and HEAD remain unchanged at `4b71f80`.