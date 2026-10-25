### Verdict

**APPROVE WITH CHANGES** — the branch substantially follows the approved plan, but settlement durability, paid-call recovery, parser isolation and checkpoint privacy need fixes before approval.

### Focus questions 1–8

**1. Spend safety.** The gateway checks configuration before reserving, and each HTTP try follows reservation and fenced dispatch ([llm/gateway.py:722](/home/user/job-hunter/backend/src/jobhunter/llm/gateway.py:722)). Settlement and counter changes share a transaction; duplicate settlements are ignored, and late settlements can replace ambiguity ([llm/spend_ledger.py:801](/home/user/job-hunter/backend/src/jobhunter/llm/spend_ledger.py:801)). I found no ordinary reservation bypass or admission-cap race. However, storage-unsafe model output can roll back a completed call’s settlement (**I1**), and recovery after a first-stage token-limit response repeats already-paid work (**I2**). Ambiguous requests remain conservatively charged; this protects accounting but does not provide exactly-once provider billing.

**2. Privacy.** Text extraction checks that the transmitted text exactly matches the reviewed text; native PDF is the documented, separately acknowledged exception ([extract_profile.py:102](/home/user/job-hunter/backend/src/jobhunter/graphs/profile/nodes/extract_profile.py:102)). Confirm revalidates the profile and scans user-entered fields ([profile/activation.py:228](/home/user/job-hunter/backend/src/jobhunter/profile/activation.py:228)). Contact stripping remains heuristic, with review providing the documented guarantee. AES-GCM uses fresh nonces and authenticated context ([crypto.py:61](/home/user/job-hunter/backend/src/jobhunter/crypto.py:61)). Retention rechecks draft eligibility under locks and clears associated extraction outputs ([privacy/retention.py:435](/home/user/job-hunter/backend/src/jobhunter/privacy/retention.py:435)); its protected-file set includes active, recent and open-draft references. The material gaps are parser leftovers/descendants (**I3**), collect exceptions reaching checkpoints (**I4**) and recorder tracing (**M4**). I found no additional content-bearing operation payload, alert or report path in the reviewed M2 flows.

**3. Lock order and concurrency.** Settings save locks `active_config` before reading its referenced version ([settings/save.py:305](/home/user/job-hunter/backend/src/jobhunter/settings/save.py:305)). Scope precompute locks jobs before its operation owner, while final activation takes the configuration lock first and ranking last ([settings/scope_apply.py:229](/home/user/job-hunter/backend/src/jobhunter/settings/scope_apply.py:229)). I found no demonstrated cross-module deadlock cycle. The remaining mutable-pointer join in run admission can encounter the parked EvalPlanQual case during concurrent duplicate admission; it refuses the duplicate rather than changing ownership ([worker/runs.py:316](/home/user/job-hunter/backend/src/jobhunter/worker/runs.py:316)). The current hard-filter callers already hold the job lock before their joined revision read. Other reviewed locking joins use stable references or are protected by the takeover epoch fence.

**4. Crash safety.** Requeue, stale reclaim and takeover share claim-token and attempt-exhaustion handling, including transactional failure hooks ([worker/operations.py:444](/home/user/job-hunter/backend/src/jobhunter/worker/operations.py:444)). Scope reconciliation clears permanently failed pending scopes or recreates missing work ([settings/scope_apply.py:316](/home/user/job-hunter/backend/src/jobhunter/settings/scope_apply.py:316)). The checked-in C24/C34 tests cover activation rollback and idempotent replay; C25 covers worker death during precompute; C30 covers lost queue delivery; C35 covers reclaimed extraction and late fenced writes. I inspected those tests but did not rerun them. I found no additional permanently stuck M2 domain state; **I1** loses exact settlement evidence, and **I2** exposes a recovery case those tests do not cover.

**5. Hostile input.** Uploads have explicit file/body limits, format checks and sandboxed parsing; the parser child limits address space, CPU and core dumps ([profile/upload.py:71](/home/user/job-hunter/backend/src/jobhunter/profile/upload.py:71), [docparse/worker.py:164](/home/user/job-hunter/docparse/worker.py:164)). The sandbox does not fully contain a compromised parser across jobs (**I3**). M2 request models generally reject unknown members and oversized identifiers, but profile strings accept NUL/lone surrogates, and an industry setting accepts a lone surrogate (**I1**). Ordinary JSON routes also lack an enforced transport-size ceiling (**M7**). Offline probes confirmed these validation gaps; I did not attempt resource exhaustion or a parser exploit.

**6. Settings correctness.** Save reruns the shared dry-run under the configuration lock, checks the base version and acknowledgements, and applies effects in their declared order ([settings/save.py:301](/home/user/job-hunter/backend/src/jobhunter/settings/save.py:301)). Internal saves retain the documented acknowledgement behavior. DST conversion explicitly handles gaps and chooses the first occurrence of repeated wall times ([db/seed.py:110](/home/user/job-hunter/backend/src/jobhunter/db/seed.py:110)). The fixed-point settings representation has a cross-module mismatch with single-pass job normalization (**M2**), and Unicode validation is incomplete (**I1**). I found no further mismatch between dry-run and save beyond the documented projection assumptions.

**7. API contract.** The reviewed M2 routes implement the planned session/CSRF and idempotency structure, including the durable original confirm response. CSRF checks apply at the edge ([api/edge.py:90](/home/user/job-hunter/backend/src/jobhunter/api/edge.py:90)). The draft GET still returns 500 for schema-incompatible stored content (**M1**), while confirm returns a controlled 409. I regenerated OpenAPI types entirely in memory, using the exporter’s sorted document, and obtained an exact match with `frontend/src/api/schema.d.ts`. The surrogate and oversized-body findings remain contract-boundary gaps despite that match.

**8. Plan drift.** The early watchlist/geo work, staged foreign keys, scoring deferrals, daily-only CV retention and currently unsuccessful live generator-schema gate are documented decisions, not findings. The carry-forward section names the major M3 scoring/schema/spike work, M4 UI and proposal failure-code work, and M5 operational/privacy work ([plan:2030](/home/user/job-hunter/docs/superpowers/plans/2026-10-20-m2-llm-profile-settings.md:2030)). I found no additional unsupported early feature or silently omitted major M2 deliverable. The paid-stage replay contradicts Deviation 26’s recovery promise (**I2**). Add the explicit carry decisions below to the milestone tracking.

### Strengths

- Reservation, dispatch ownership and settlement are separated clearly, with conservative ambiguity handling.
- Profile activation combines domain writes, operations and idempotency results transactionally.
- Scope precompute preserves the active view until completeness is established.
- Profile exception sanitization and content-free validation messages provide useful privacy boundaries.
- Crash tests exercise meaningful transaction and ownership failures.
- Read-only verification confirmed OpenAPI parity and reproduced the reported validation/checkpoint defects. No files changed, prohibited data was accessed, or network calls were made.

### Issues

#### Critical

None identified.

#### Important

**I1 — Storage-unsafe strings can lose completed-call settlement and produce API 500s.**  
Evidence: [llm/schemas/profile.py:86](/home/user/job-hunter/backend/src/jobhunter/llm/schemas/profile.py:86), [llm/gateway.py:962](/home/user/job-hunter/backend/src/jobhunter/llm/gateway.py:962), [llm/spend_ledger.py:848](/home/user/job-hunter/backend/src/jobhunter/llm/spend_ledger.py:848), [profile_review.py:480](/home/user/job-hunter/backend/src/jobhunter/api/routes/profile_review.py:480), [settings/validate.py:118](/home/user/job-hunter/backend/src/jobhunter/settings/validate.py:118).

A structurally valid profile with `summary="backend\u0000engineer"` passes both request and model-output validation. PostgreSQL rejects its JSONB representation. On the gateway path, that failure rolls back the billed settlement together with the output write; recovery can retain only an ambiguous reservation, and the operation lacks a completed receipt. Lone surrogates also survive profile validation. Separately, `scope.industries_include=["Abc\ud800xyz"]` passes settings validation and raises `UnicodeEncodeError` in effect classification. These acceptance/encoding paths were reproduced offline.

**Fix:** Reject NUL and invalid Unicode scalar values recursively at API boundaries before hashing or persistence. Validate completed model output for storage safety before settlement; classify unusable output as `invalid_output`, retain its usage, settle it billed with `output_json=NULL`, and permit only the existing structural-repair policy.

**I2 — Recovery repeats the paid first token-limit stage.**  
Evidence: [llm_outcome.py:117](/home/user/job-hunter/backend/src/jobhunter/graphs/profile/nodes/llm_outcome.py:117), [llm/gateway.py:733](/home/user/job-hunter/backend/src/jobhunter/llm/gateway.py:733), [llm/gateway.py:957](/home/user/job-hunter/backend/src/jobhunter/llm/gateway.py:957).

After the first stage settles billed with `max_tokens_first_stage`, a crash before enlargement—or transient exhaustion during enlargement—leaves that completed receipt. `walk()` returns an ordinary `Call`, and the gateway starts again at the smaller first-stage limit. This buys another request for a stage already known to be insufficient. It is duplicate paid work, not duplicate settlement of one ledger entry, and conflicts with Deviation 26’s receipt-reuse promise.

**Fix:** Preserve stage progress in the resumable call state and resume directly at enlargement. Preserve the applicable retry allowance across recovery. Add a crash test between first-stage settlement and enlargement that proves the smaller request is not sent again.

**I3 — A compromised parser can survive into later jobs and retain plaintext.**  
Evidence: [docparse/worker.py:164](/home/user/job-hunter/docparse/worker.py:164), [docparse/worker.py:216](/home/user/job-hunter/docparse/worker.py:216), [compose.dev.yaml:178](/home/user/job-hunter/deploy/compose.dev.yaml:178), [parse_client.py:187](/home/user/job-hunter/backend/src/jobhunter/profile/parse_client.py:187).

Conditional on parser compromise, the child can fork descendants: there is no process-count limit, and `_reap` kills only its PID. A survivor retains access to the shared parse volume and later jobs. Copies outside the current job, or inside an inaccessible directory, also survive best-effort worker cleanup. `daemon=True` does not prevent native `fork`.

**Fix:** Deny child process creation, add a container PID limit and descendant cleanup, and isolate each job’s writable scratch space from future jobs. Destroy that scratch space after the job; if cleanup cannot be proven, stop accepting jobs until the sandbox is recreated. Process-group killing alone does not address detached descendants or retained files.

**I4 — Collect exceptions persist database DETAIL content in checkpoints.**  
Evidence: [graphs/collect/graph.py:137](/home/user/job-hunter/backend/src/jobhunter/graphs/collect/graph.py:137), [graphs/collect/graph.py:200](/home/user/job-hunter/backend/src/jobhunter/graphs/collect/graph.py:200), [graphs/profile/graph.py:157](/home/user/job-hunter/backend/src/jobhunter/graphs/profile/graph.py:157).

A collect node raising `DBAPIError` propagates the original exception to LangGraph. PostgreSQL DETAIL can contain rejected posting/company values even when SQLAlchemy hides bound parameters. An in-memory graph probe confirmed that a synthetic DETAIL marker survives in checkpoint writes.

**Fix:** Apply content-free exception sanitization to collect nodes before LangGraph receives errors, preserving retry classification and intentional control-flow exceptions. Test serialized checkpoint writes for absence of synthetic database-detail content.

#### Minor

**M1 — Draft GET has an uncontrolled schema-drift failure.**  
Evidence: [profile_review.py:429](/home/user/job-hunter/backend/src/jobhunter/api/routes/profile_review.py:429). A ready draft whose stored JSON no longer validates raises an uncaught validation error and returns 500, although confirm handles the same condition as 409. **Fix:** Return a content-free 409 or a documented degraded draft representation, and update the response contract and regression test.

**M2 — Settings exclusions and job keys use different normal forms.**  
Evidence: [settings/model.py:154](/home/user/job-hunter/backend/src/jobhunter/settings/model.py:154), [normalize/companies.py:73](/home/user/job-hunter/backend/src/jobhunter/normalize/companies.py:73), [scoring/filters.py:300](/home/user/job-hunter/backend/src/jobhunter/scoring/filters.py:300). For `Muster Germany Deutschland`, the job key becomes `muster germany`, while the stored exclusion reaches `muster`; equality misses the exclusion. **Fix:** Use the same stable comparison normalization on both sides, including industry comparisons. Avoid silently rekeying existing identity records; test repeated-country and non-idempotent folding cases.

**M3 — Tokenization defeats the proposal location stop-list.**  
Evidence: [queries/proposals.py:218](/home/user/job-hunter/backend/src/jobhunter/queries/proposals.py:218), [queries/proposals.py:235](/home/user/job-hunter/backend/src/jobhunter/queries/proposals.py:235). The alternate spelling `kil'` survives spelling-level subtraction, then tokenizes to stop-listed `kil`, causing false location rejection. **Fix:** Compare alternate spellings and stop words using the same token/phrase representation, preserving intentional primary-place names; assert against the final phrase set.

**M4 — The recorder can enable LangSmith tracing through its environment.**  
Evidence: [record_anthropic.py:461](/home/user/job-hunter/scripts/spikes/record_anthropic.py:461), [record_anthropic.py:896](/home/user/job-hunter/scripts/spikes/record_anthropic.py:896). With LangSmith tracing configured, direct `ainvoke` can export prompts through LangChain; disabling Anthropic SDK telemetry does not disable this layer. No tracing export was attempted during review. **Fix:** Refuse tracing-enabled recorder execution or explicitly disable tracing around every recorder invocation, with an offline regression test.

**M5 — Recorded fixtures are unreadable by the dev worker.**  
Evidence: [record_anthropic.py:224](/home/user/job-hunter/scripts/spikes/record_anthropic.py:224), [record_anthropic.py:784](/home/user/job-hunter/scripts/spikes/record_anthropic.py:784). Committed synthetic fixtures use the private writer’s 0700 directories and 0600 files; a different worker UID cannot read newly recorded artifacts. **Fix:** Separate private raw-artifact permissions from sanitized fixture permissions, explicitly setting readable modes only for the latter.

**M6 — Dependency auditing omits the embedder environment.**  
Evidence: [ci.yml:180](/home/user/job-hunter/.github/workflows/ci.yml:180). The audit installs the docparse group but omits embedder-only dependencies, leaving part of the deployed software unaudited even when the job runs. **Fix:** Include the embedder group or audit its separately exported locked requirements.

**M7 — Ordinary JSON routes have no enforced body-size ceiling.**  
Evidence: [api/edge.py:156](/home/user/job-hunter/backend/src/jobhunter/api/edge.py:156), [profile_review.py:480](/home/user/job-hunter/backend/src/jobhunter/api/routes/profile_review.py:480), [compose.dev.yaml:115](/home/user/job-hunter/deploy/compose.dev.yaml:115). A synthetic 2 MiB summary passes request parsing and remains intact after semantic validation; save proceeds with that content and its `limit` issue. Neither the edge nor supplied proxy bounds these JSON bodies, so progressively larger requests consume unbounded parsing/validation memory in the 1 GiB API container. Resource exhaustion itself was not exercised. **Fix:** Enforce a documented streaming byte limit before JSON parsing, covering requests without trustworthy `Content-Length`; retain the separate upload limits and return 413.

### Parked findings

1. **FIX NOW** — I3: prevent parser descendants, isolate per-job scratch space, and recreate the sandbox when cleanup fails.
2. **FIX NOW** — M4: refuse or explicitly disable LangSmith tracing in the recorder.
3. **FIX NOW** — M6: include embedder dependencies in the audit.
4. **FIX NOW** — I4: sanitize collect exceptions before checkpoint serialization while preserving retry behavior.
5. **ACCEPT** — Concurrent duplicate admission can observe the null-extended attempt, but the resulting refusal does not steal ownership; the original executor or stale-run recovery remains responsible. No corruption or permanent stall was demonstrated.
6. **FIX NOW** — M1: return a controlled schema-mismatch response from draft GET.
7. **FIX NOW** — M2: align filter comparison normalization without silently changing persisted identity keys.
8. **FIX NOW** — M3: apply stop-list matching in the final token/phrase representation.
9. **CARRY to M4** — Suppress expected disconnect alerts and improve cancellation behavior; the identified parser work is bounded, and the current failure exposes no content.
10. **CARRY to M3** — Store admission-buffer evidence and settle adjustment-row accounting before adding adjustment writers; M2 admission enforcement is present, but retrospective audit evidence is incomplete.
11. **ACCEPT** — These are documented heuristic limits; mandatory text review remains the guarantee. Do not represent stripping as exhaustive.
12. **ACCEPT** — The listed misses and false positives fall under the same documented review requirement.
13. **CARRY to M4** — Align `RunNowIn` strictness during API/UI integration; ignored unknown fields do not alter the existing M0 action.
14. **ACCEPT** — Deviation 39 documents the unreachable warning; a sweep would verify the same arithmetic without changing behavior.
15. **ACCEPT** — No demonstrated functional or privacy defect follows from the listed test/style nits; blank-URL rejection is valid, and the zero-width display name is cosmetic.
16. **FIX NOW** — M5: separate private recording modes from worker-readable synthetic fixture modes before the next recording cycle.