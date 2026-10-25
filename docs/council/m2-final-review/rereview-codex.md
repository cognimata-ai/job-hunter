### Verdict

**APPROVE WITH CHANGES** — the ruled implementation fixes are present, with no remaining blocking code issue found; Deviation 44 needs one API-status correction.

### Finding verdicts

`C` identifies my round-1 findings, `F` identifies Fable’s findings, and `P` identifies my parked-item verdicts.

- **C-I1 — FIXED.** Draft strings are checked before persistence, settings reject surrogates, and unusable structured output settles billed with usage and no output: [profile_review.py:500](/home/user/job-hunter/backend/src/jobhunter/api/routes/profile_review.py:500), [validate.py:120](/home/user/job-hunter/backend/src/jobhunter/settings/validate.py:120), [gateway.py:984](/home/user/job-hunter/backend/src/jobhunter/llm/gateway.py:984).
- **C-I2 — FIXED.** A first-stage token-limit receipt selects enlargement directly; the resumed first try uses the fresh deadline’s 30-second threshold: [llm_outcome.py:136](/home/user/job-hunter/backend/src/jobhunter/graphs/profile/nodes/llm_outcome.py:136), [gateway.py:756](/home/user/job-hunter/backend/src/jobhunter/llm/gateway.py:756).
- **C-I3 — ACCEPTED-AS-RULED.** Process limits, group termination and fail-closed cleanup implement the required M2 hardening; full per-job isolation and `/tmp` cleanup remain explicitly carried to M5: [worker.py:193](/home/user/job-hunter/docparse/worker.py:193), [worker.py:243](/home/user/job-hunter/docparse/worker.py:243), [worker.py:449](/home/user/job-hunter/docparse/worker.py:449), [plan:2061](/home/user/job-hunter/docs/superpowers/plans/2026-10-20-m2-llm-profile-settings.md:2061).
- **C-I4 — FIXED.** Every production collect node is guarded, with database retry classification and lifecycle exceptions preserved: [collect/graph.py:118](/home/user/job-hunter/backend/src/jobhunter/graphs/collect/graph.py:118), [collect/graph.py:206](/home/user/job-hunter/backend/src/jobhunter/graphs/collect/graph.py:206), [sanitize.py:54](/home/user/job-hunter/backend/src/jobhunter/graphs/sanitize.py:54).
- **C-M1 — FIXED.** Schema-incompatible drafts return a degraded representation with `schema_mismatch` and the paste repair: [profile_review.py:439](/home/user/job-hunter/backend/src/jobhunter/api/routes/profile_review.py:439).
- **C-M2 — FIXED.** Company and industry comparisons use the same fixed point as settings, without changing identity keys; algorithm version `"2"` invalidates prior results: [filters.py:305](/home/user/job-hunter/backend/src/jobhunter/scoring/filters.py:305), [versioning.py:10](/home/user/job-hunter/backend/src/jobhunter/scoring/versioning.py:10).
- **C-M3 — FIXED.** Alternate spellings and stop words are compared after tokenization, while primary place names remain included: [proposals.py:219](/home/user/job-hunter/backend/src/jobhunter/queries/proposals.py:219).
- **C-M4 — FIXED.** The recorder refuses tracing-related environment settings before loading a spike or making calls: [record_anthropic.py:968](/home/user/job-hunter/scripts/spikes/record_anthropic.py:968).
- **C-M5 — FIXED.** Committed recordings receive explicit 0644/0755 modes separately from private artifacts: [record_anthropic.py:247](/home/user/job-hunter/scripts/spikes/record_anthropic.py:247), [record_anthropic.py:277](/home/user/job-hunter/scripts/spikes/record_anthropic.py:277).
- **C-M6 — FIXED.** The audit installs both docparse and embedder dependency groups: [ci.yml:181](/home/user/job-hunter/.github/workflows/ci.yml:181).
- **C-M7 — FIXED.** The edge counts streamed bytes before parsing, rejects oversized bodies, and exempts the two independently bounded upload routes: [edge.py:80](/home/user/job-hunter/backend/src/jobhunter/api/edge.py:80), [edge.py:148](/home/user/job-hunter/backend/src/jobhunter/api/edge.py:148).

- **P1 — ACCEPTED-AS-RULED.** The required M2 sandbox controls are implemented; the explicitly carried isolation remainder remains necessary, as described under C-I3.
- **P2 — FIXED.** Recorder tracing refusal is implemented; see C-M4.
- **P3 — FIXED.** Embedder dependencies are included in auditing; see C-M6.
- **P4 — FIXED.** Collect exceptions are sanitized without changing their retry classification; see C-I4.
- **P5 — ACCEPTED-AS-RULED.** Duplicate-admission refusal remains safe; splitting the mutable-pointer join is explicitly carried as M3 hygiene: [plan:2046](/home/user/job-hunter/docs/superpowers/plans/2026-10-20-m2-llm-profile-settings.md:2046).
- **P6 — FIXED.** Draft schema mismatch has a controlled degraded response; see C-M1.
- **P7 — FIXED.** Comparison normalization is aligned without rekeying identities; see C-M2.
- **P8 — FIXED.** The final token representation excludes the `kil'` stop-list leak; see C-M3.
- **P9 — FIXED.** Both upload readers convert disconnects into content-free 400 responses and informational logging; bounded parser cancellation remains accepted: [upload.py:251](/home/user/job-hunter/backend/src/jobhunter/profile/upload.py:251), [upload.py:376](/home/user/job-hunter/backend/src/jobhunter/profile/upload.py:376), [upload.py:409](/home/user/job-hunter/backend/src/jobhunter/profile/upload.py:409).
- **P10 — ACCEPTED-AS-RULED.** Admission-buffer evidence and adjustment accounting are explicitly carried to M3; no adjustment writer was introduced: [plan:2046](/home/user/job-hunter/docs/superpowers/plans/2026-10-20-m2-llm-profile-settings.md:2046).
- **P11 — ACCEPTED-AS-RULED.** Mandatory review remains the contact-stripping guarantee; the identified shapes are carried into M4 UI tests: [plan:2052](/home/user/job-hunter/docs/superpowers/plans/2026-10-20-m2-llm-profile-settings.md:2052).
- **P12 — ACCEPTED-AS-RULED.** The additional stripping edge cases have the same documented review requirement and M4 test carry.
- **P13 — FIXED.** `RunNowIn` now forbids extra members: [runs.py:43](/home/user/job-hunter/backend/src/jobhunter/api/routes/runs.py:43).
- **P14 — FIXED.** The parameterized sweep covers all 168 allowed intervals and pins the unreachable warning: [test_dryrun.py:105](/home/user/job-hunter/tests/unit/test_dryrun.py:105).
- **P15 — ACCEPTED-AS-RULED.** The cited test/style nits still demonstrate no functional or privacy defect.
- **P16 — FIXED.** Sanitized recordings and private artifacts now use separate permission policies; see C-M5.

- **F-I1 — FIXED.** Draft NUL and surrogate values are rejected before persistence; see C-I1.
- **F-I2 — FIXED.** All three GET paths handle stored-schema incompatibility: [profile_review.py:439](/home/user/job-hunter/backend/src/jobhunter/api/routes/profile_review.py:439), [profile_review.py:818](/home/user/job-hunter/backend/src/jobhunter/api/routes/profile_review.py:818), [profile.py:468](/home/user/job-hunter/backend/src/jobhunter/api/routes/profile.py:468).
- **F-M1 — FIXED.** Paid first-stage recovery resumes at enlargement; see C-I2.
- **F-M2 — FIXED.** The graph timeout covers two maximum-duration logical calls plus the margin: [profile/graph.py:108](/home/user/job-hunter/backend/src/jobhunter/graphs/profile/graph.py:108).
- **F-M3 — FIXED.** Storage-unsafe structured output becomes billed `invalid_output`; see C-I1.
- **F-M4 — ACCEPTED-AS-RULED.** The timezone-only anchor behavior remains the identified specification inconsistency; this wave does not claim to resolve it.
- **F-M5 — ACCEPTED-AS-RULED.** Updating the roadmap status remains the controller’s review-closure action.
- **F-M6 — ACCEPTED-AS-RULED.** Confirm requests embeddings on reactivation, so active-version reconciliation remains sufficient for the active tuple.

### New issues

**Critical:** None.

**Important:** None.

**Minor — N1: Deviation 44 incorrectly promises HTTP 422 for every invalid M2 body.**  
[Plan:2028](/home/user/job-hunter/docs/superpowers/plans/2026-10-20-m2-llm-profile-settings.md:2028) omits the settings dry-run exception. A payload containing a lone surrogate in an industry returns HTTP 200 with validation errors through [settings.py:261](/home/user/job-hunter/backend/src/jobhunter/api/routes/settings.py:261), as intended and explicitly reported by the implementer. A client following the new deviation could incorrectly treat HTTP 200 as successful validation. **Fix:** Document that settings dry-run returns 200 with field errors, while save returns 422; retain the existing `/cv-text` exception.

### Notes

- Reviewed `c845cbe..66016f5`; the working tree remained unchanged. No prohibited files, keys, network services or paid APIs were accessed.
- **697 focused offline tests passed.** Database, container and crash tests were inspected but not rerun in this read-only environment.
- FR-1(d) retains the existing atomic, duplicate-resistant settlement path and usage fields. Provider-metadata validation remains explicitly carried to M3.
- FR-2 preserves existing per-execution retry behavior. Previous usage remains in durable ledger entries; the returned `cost_usd` covers this execution’s tries.
- FR-4 preserves the multipart upload’s larger limit and the paste route’s own limit. No additional supported large-body route was identified.
- FR-11 preserves young protocol directories for concurrent submissions and unread results. Cleanup failures propagate outside the per-job catch and stop processing. Full isolation, `/tmp`, and restart-loop operational handling remain documented M5 work.
- FR-12 preserves fencing, cancellation, invariant handling and database retries. FR-16 uses the shared stale predicate; its regression checks that the second reconciliation pass performs no work.
- Amended Deviation 28 and the new carry-forward lines match the implementation, subject to N1 above.