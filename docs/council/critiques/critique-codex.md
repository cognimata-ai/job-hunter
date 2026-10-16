# Round 2 cross-critique — Codex

I endorse a merged design built around immutable observations, versioned scoring targets, PostgreSQL transactions, and bounded LangGraph workflows. All three proposals agree on most infrastructure choices; the remaining differences concern correctness, recall, and operational guarantees.

References below use **C** = `proposal-codex.md`, **O** = `proposal-opus.md`, **F** = `proposal-fable.md`. The brief’s settled decisions override older research recommendations; `facts-addendum.md` supersedes caching assumptions.

## 1. ERRORS

### Codex: errors and specification gaps in my proposal

1. **Fallback pacing can defeat fallback availability.**  
   **C, “Quota budget algorithm”:** `floor(normal_allocation × elapsed_fraction)` gives zero allowance at a renewal boundary, even with a full balance. If JSearch fails immediately after SerpApi renews, SerpApi cannot serve the first request until sufficient allowance accrues. Use bounded initial credit and fractional carry; permit explicitly budgeted emergency borrowing.

2. **A signed ranking watermark does not itself stabilize pagination.**  
   **C, D10:** a cursor containing a generation number works only if subsequent queries can reconstruct that generation. With mutable current rankings, an ad moving across the cursor boundary can disappear or repeat. Specify immutable ranking membership/sort values for the cursor’s lifetime, or disclose live-pagination semantics.

3. **The scoring state machine omits promised outcomes and rollback behavior.**  
   **C, “Pipeline state machines” and “Rubric and uncertainty”:** “expiring before scoring” has no defined transition. Moreover, changing `ACCEPTED` to `SUPERSEDED` mixes historical validation with current applicability: reactivating an earlier configuration can encounter an existing unique target with no restoration transition. Keep acceptance immutable, derive currentness, and define explicit expiry/cancellation outcomes.

4. **Full-text equality is insufficient identity evidence by itself.**  
   **C, D5:** matching employer/title/location/full-description fingerprints can still collapse two genuine requisitions using identical boilerplate. Distinct authoritative requisition IDs must block automatic merging unless an explicit alias relationship is established.

5. **Important lifecycle behavior remains prose-only.**  
   **C, graph specification and data model:** runtime JSearch failure has no explicit fallback-planning node; batches have no specified submission/item state tables; resumption around profile activation lacks an explicit idempotent activation receipt. Implementers could miss fallback, lose batch items, or enqueue duplicate rescoring. These require concrete states, keys, and edges.

6. **Uncertainty handling introduces a ranking bias.**  
   **C, scoring rubric and D10:** sorting primarily by the lower bound systematically favors well-documented ads over promising snippets. This is a defensible conservative view, but not a calibrated estimate of fit. Expose fit and evidence coverage separately and evaluate the default ordering.

### Opus: errors and failure scenarios

1. **Scope and extraction configuration are incompletely versioned.**  
   **O, D7 and §4:** `scoring_config_hash` omits scope; `scores` has one row per ad version/configuration despite storing scope-dependent deterministic criteria. Changing commute radius or salary preference either overwrites historical results or leaves stale values. `ad_facts` uniqueness also omits an explicit model/effort/prompt identity. Version all behavior-changing inputs.

2. **“IDs-only checkpoints” contradict the graph schema.**  
   **O, D6/D16 versus §3:** `AdScoreState.draft` contains `ScoreDraft`, whose schema includes profile/ad evidence quotes; critiques can also contain personal information. Checkpoints therefore can contain PII. Store artifact IDs or apply the corresponding encryption, retention, and deletion policy.

3. **Scanned-PDF processing bypasses the promised contact stripping.**  
   **O, D8/D16:** regex removal from extracted text cannot remove contact information inside an original scanned PDF sent as a native document. The original-document path needs an accurate disclosure and distinct handling.

4. **Convergence can override a valid rejection.**  
   **O, §5:** two successive drafts within three points are accepted despite a critic still requesting revision. Two consistently unsupported ratings satisfy that test. Numerical stability is not semantic correctness; required critique must accept, otherwise the result remains contested.

5. **Grounding checks protect high scores more than false negatives.**  
   **O, §5 V3:** an ungrounded quote supporting level ≥3 becomes an issue, but unsupported low ratings can pass. An invented deficiency may suppress a good ad without mandatory critique. Validate evidence for every asserted rating and mandatory mismatch.

6. **Unknown-weight renormalization can inflate ranking.**  
   **O, §5:** an ad with only role fit known and rated 4 can receive 100, despite almost everything else being unknown. Low confidence does not repair a list sorted by that score. Also, “required language missing” must distinguish absent CV evidence from confirmed insufficient proficiency.

7. **Retry billing is not fully covered by the ledger design.**  
   **O, D1/D12 and §4:** SDK retries occur inside one gateway invocation, while ledger identity is a logical call key. A timed-out, already-billed request followed by an SDK retry can exceed a single-call reservation. Disable hidden retries or reserve and reconcile every possible transport attempt.

8. **Source freshness and identity rules have unsafe shortcuts.**  
   **O, D4/D5:** SmartRecruiters details fetched only for new postings never refresh changed descriptions under existing IDs. Two “successful” ATS responses are insufficient closure evidence without complete pagination and freshness checks. Automatically merging clusters when an exact key bridges them also needs conflict handling, not unconditional lower-ID ownership.

9. **Quota reconciliation is ambiguous where it must be conservative.**  
   **O, §6:** `max(local, remote)` for *remaining* allowance can discard evidence of external consumption. JSearch billing cannot always be attributed from header deltas under concurrent calls. Preserve local outstanding reservations, distinguish remaining balance from usage counters, and never refund without demonstrated non-billing.

10. **Cache pricing is factually wrong.**  
    **O, §8:** Opus cache reads are **$0.20/MTok**, not $0.40. Low/medium/high effort configurations require separate cache accounting; write costs are not universally negligible. The addendum establishes both corrections.

### Fable: errors and failure scenarios

1. **The active-run index does not enforce the stated invariant.**  
   **F, D2/§4:** `UNIQUE(status) WHERE status IN ('queued','running')` permits one queued row and one running row. Concurrent admission can therefore succeed twice and later fail during transition. Use a unique constant/singleton key over active collection states, plus transactional admission.

2. **Scoring identity omits the job revision and scope.**  
   **F, D7/§3.2/§4:** `(job_id, score_version)` remains unchanged when the description or user scope changes. Upsert can overwrite historical evidence; an older in-flight result can replace a newer result. Introduce immutable job revisions and versioned targets, and publish only against the matching current tuple.

3. **Fingerprint dedup explicitly accepts lost opportunities.**  
   **F, D5:** title/company/city equality merges different teams’ requisitions before text confirmation. Calling that “acceptable, reversible” does not preserve independent scores or application state. URL stripping also applies `se`/`v` globally although the research identifies those as Adzuna-specific tokens.

4. **Freshness windows ignore query rotation and downtime.**  
   **F, D4/§6:** `date_posted` follows schedule interval rather than the query’s last covered window. A query revisited every four days with `today` permanently misses intervening ads. Use per-query coverage watermarks, overlap, and explicit incomplete traversal.

5. **Arbeitnow disappearance cannot establish closure.**  
   **F, D5/§4:** the incremental, capped, cached feed is not a complete authoritative inventory. An older ad disappears from fetched pages merely because newer ads displace it. `first_seen + max_ad_age` also describes an application policy, not verified closure. Research sources §6 supports neither inference.

6. **Quota admission and fallback are incomplete.**  
   **F, §6:** reservation is explicit for page two, but not uniformly before page one and every retry; events are recorded after calls. A crash after billing loses accounting. Adzuna weekly/monthly limits are not fully enforced. `floor(remaining/runs_left)` can starve an hourly schedule for weeks without carry. Fallback is planned before JSearch executes, so newly failed requests have no same-run fallback edge.

7. **Unknowns cannot be represented faithfully in scoring.**  
   **F, §5:** every criterion requires an integer, forcing fabricated ratings where evidence is absent. Knockouts lack profile evidence and a deterministic confirmation protocol; duplicate soft knockouts can repeatedly subtract ten points. Ad-text language is also used as a proxy for working-language eligibility. A German advertisement for an English-speaking role can be wrongly excluded.

8. **Subgraph checkpoint semantics are misstated.**  
   **F, §3.2:** `checkpointer=False` disables subgraph checkpointing; it does not inherit the parent checkpointer. A crash can replay the subgraph’s calls. Use inherited persistence and operation receipts; tech §A3 distinguishes inheritance from disabling persistence.

9. **The stated reconciliation invariant counts the wrong entities.**  
   **F, D12:** fetched items cannot equal current posting rows: the same posting can appear in multiple queries and runs, while malformed items may have no native ID. Persist per-response raw-item dispositions. Actual quota spending also need not equal planned spending after skips, retries, or cached responses.

10. **Model defaults and settled policy contradict the brief.**  
    **F, §8:** recommending B *as default* conflicts with default Opus until the user chooses; the $60 cap in D9 is also unchosen. **F, §9 Q1** reopens Adzuna personal use, already settled. The addendum corrects Opus cache-read pricing; mixed model/effort configurations cannot assume one cache write per run.

11. **Operational recovery has material gaps.**  
    **F, D13–D16:** a dead scheduler cannot run its own alert checks; optional offsite backup leaves VPS loss unrecoverable. Deletion omits an explicit Adzuna-derived-data purge and protection against restoring deleted data. Raw responses also persist `redirect_url`, contradicting D5’s “never persisted” claim; the correct policy is private retention for compliant outbound navigation, not using it as identity.

## 2. COMPARISON

### D1–D16

| Decision | Endorsed position and reason |
|---|---|
| **D1** | **Consensus, with Opus’s gateway:** Python/FastAPI/Pydantic/LangGraph; one tested Claude gateway, explicit native structured output, pinned dependencies and base URL. |
| **D2** | **Codex plus simpler admission:** independent scheduler, PostgreSQL queue, transactional run creation, fencing and coalesced catch-up. Collection ownership must not be held by long scoring/batch work. |
| **D3** | **Codex:** PostgreSQL/pgvector, no Redis, separate direct-ONNX embedder. Exact vector search first; HNSW only after measurement. |
| **D4** | **Merged:** Codex coverage/accounting semantics; Opus adapter contracts and yield statistics; Fable fair query rotation. Enforce an exploration floor and runtime fallback replanning. |
| **D5** | **Codex, strengthened:** observations → listings → opportunities → immutable revisions. Compatible strong identities may merge; fuzzy matches remain suggestions initially. |
| **D6** | **Codex/Opus:** separate collection, scoring and profile workflows; bounded `Send`; durable receipts outside checkpoints. Use Fable’s ordinary API action for draft activation. |
| **D7** | **Codex with Opus anchors:** embeddings prioritize without permanent exclusion; unknown ratings remain nullable; explicit evidence and deterministic aggregation; mandatory critique cannot be bypassed by convergence. |
| **D8** | **Merged:** local parsing and reviewed draft by default; explicit native-PDF alternative. Activation atomically changes profile and creates targets; budget shortage defers work rather than blocking profile correction. |
| **D9** | **Codex:** immutable settings, broad defaults, no invented language proficiency or employment exclusions. Opus remains default; user chooses spend cap. |
| **D10** | **Merged:** Codex independent user-action fields and provenance; Opus/Fable details and feedback. Separate fit, confidence, availability and pipeline status; specify pagination snapshots. |
| **D11** | **Consensus plus Codex:** same-origin React/Vite, REST/OpenAPI, Argon2id, hashed server sessions, CSRF protection, throttling, safe uploads and SSRF-resistant ATS discovery. |
| **D12** | **Codex:** explicit observations and target outcomes, ambiguity states, reconciler and replay. Adopt Opus’s typed error taxonomy. |
| **D13** | **Codex/Opus:** DB-backed reports, private metrics, external dead-man monitoring, LangSmith off by default. Dependency failure must not make the diagnostic UI inaccessible. |
| **D14** | **Merged:** PostgreSQL concurrency/crash tests, scrubbed fixtures, payload contracts, human-labelled bilingual evaluations and Playwright CLI. Evaluate false exclusions and merges, not merely correlation. |
| **D15** | **Codex/Opus:** 4 vCPU/16 GB baseline, separate embedder, build-time model packaging, mandatory encrypted offsite backups and restore drills. Eight GB remains a measured optimization. |
| **D16** | **Codex:** minimized immutable evidence, explicit retention, source-aware deletion and restore-time deletion replay. Adopt Fable’s export and feedback facilities. |

### Cross-cutting artifacts

**LangGraph specification.** Use ID-only state and outcomes keyed by work ID. Collection explicitly routes primary results through `plan_fallback`, then reconciliation. Scoring uses immutable target IDs, inherited subgraph persistence, one optimizer pass, and compare-and-set publication. Every external attempt passes through admission. Batch submission/polling is a separate durable workflow keyed by submission identity and `custom_id`; result order is irrelevant.

**Data model and unique indexes.** Adopt C’s richer lineage model. Required uniqueness includes:

- Active collection: unique singleton expression under the active-state predicate.
- Listing: `(source_instance, native_id)`, including ATS board/region.
- Raw item: `(response_envelope, item_index)`.
- Target: `(job_revision, profile, scope, rubric, prompt/schema, extraction_config, model_policy)`.
- Attempt: `(target, attempt_number)`; at most one accepted result per target.
- Request attempt and batch item: durable unique operation keys.

Identity conflicts require review; neither a unique constraint nor an upsert constitutes a merge policy.

**Pipeline state machine.** Separate acquisition disposition, identity resolution, target processing, availability and user actions. Keep historical acceptance immutable. Budget deferral is resumable; expiry, cancellation, refusal, quarantine and batch expiration have explicit outcomes and reasons.

**Rubric/schema/evaluator.** Combine C’s nullable evidence-linked criteria with O’s concrete anchors and F’s fixed criterion-object shape. Distinguish unknown, confirmed mismatch and unconstrained preference. Version weights and anchors; show coverage separately. Critique partial/conflicted inputs, mandatory mismatch claims, high/borderline scores and reproducible samples. After one repair, unresolved semantic objections remain reviewable and cannot become accepted through numerical convergence.

**Quota algorithm.** Combine C’s reserve-before-dispatch safety with O’s fractional carry. Atomically check provider/account renewal windows, hourly/weekly limits, outstanding reservations and a bounded pacing balance. Every retry gets a new reservation; ambiguous dispatch remains chargeable. Provider observations may tighten availability but cannot erase unsettled local consumption. Reserve exhaustion is not automatically contractual exhaustion; define fallback against actual exhaustion or classified failure.

**Cost estimate.** Compare identical workloads. Using C’s 100 generators, 30 critiques and 10 repairs per workload:

| Configuration | Uncached/run | Illustrative cached/run | Cached, 30 runs/month |
|---|---:|---:|---:|
| Opus throughout | $6.40 | $5.36 | $160.66 |
| Sonnet generation/repair; Opus critique | $3.74 | $3.13 | $93.91 |
| Haiku generation/repair; Opus critique | $1.213 | $0.975 | $29.25 |

Cached figures assume a shared **2,000-token prefix**, separate generator/critic cache populations, one initial 5-minute write per population, and subsequent hits before expiry. They retain C’s output-token assumptions, including thinking. They exclude retries, extraction, evaluation and hosting.

Per the addendum: minimum prefix **512 tokens**; reads Opus/Sonnet **$0.20/MTok**, Haiku approximately **$0.01**; writes **1.25×/2×** for five minutes/one hour. Effort/model changes separate caches. Batch pricing halves eligible token charges, but does not guarantee these cache-hit assumptions, and dependent review stages add latency. Forecast from observed usage rather than assuming a universal savings multiplier.

## 3. Ideas I ADOPT and REJECT

**Adopt:**

- **Opus’s centralized LLM gateway and payload tests:** eliminate integration mistakes at one boundary.
- **Opus’s CV-independent facts extraction:** useful when reused across profiles, provided extraction configuration is versioned and evidence remains auditable.
- **Opus’s fractional budget carry and canaries:** improve utilization and detect scoring drift.
- **Fable’s ordinary API confirmation:** a reviewed draft needs no long-lived interrupted graph.
- **Fable’s explicit scoring feedback/export:** support calibration and user ownership.
- **Both proposals’ detailed criterion anchors:** stronger than numerical scales alone.

**Reject:**

- **Fable’s fingerprint merges and Arbeitnow closure inference:** both can hide real opportunities.
- **Opus’s convergence acceptance and unknown-weight renormalization:** stability and missing evidence must not manufacture confidence.
- **Both proposals’ terminal embedding rejection:** sampling exposes some misses but provides no aging guarantee for the remainder.
- **Automatic cheaper defaults, SDK-hidden retries and optional offsite recovery:** conflict with user control or stated operational guarantees.
- **My own lower-bound-first ordering as an unquestioned default:** it needs user-facing clarity and evaluation.

## 4. REVISED POSITION TABLE

“Disputed” describes remaining council differences, not established consensus.

| Item | Your revised decision (1–2 lines) | Confidence (H/M/L) | Still disputed? |
|---|---|---|---|
| D1 | Python/FastAPI/LangGraph; centralized Claude gateway. | H | No |
| D2 | Separate scheduler; transactional admission, fencing, coalesced catch-up. | H | Yes |
| D3 | PostgreSQL only; direct-ONNX service; exact vector search initially. | H | Yes |
| D4 | Coverage-aware fair planning; per-attempt reservations; runtime fallback. | H | Yes |
| D5 | Immutable observations/revisions; conservative, reversible identity resolution. | H | Yes |
| D6 | Separate bounded graphs; durable operation receipts. | H | Yes |
| D7 | Nullable evidence ratings; aging queue; mandatory semantic acceptance. | M | Yes |
| D8 | Reviewed draft; explicit PDF alternative; atomic API activation. | H | Yes |
| D9 | Broad defaults; immutable settings; user-selected spend cap. | H | Yes |
| D10 | Independent user actions; separate fit/coverage; reproducible pagination. | M | Yes |
| D11 | Same-origin SPA, server sessions, CSRF and SSRF controls. | H | No |
| D12 | Per-item/per-target outcomes with reconciliation and explicit ambiguity. | H | Yes |
| D13 | DB reports plus external heartbeat; private tracing by default. | H | Yes |
| D14 | Concurrency/crash tests and evidence/recall evaluations. | H | No |
| D15 | 16 GB baseline; packaged embedder; mandatory offsite recovery. | M | Yes |
| D16 | Source-aware deletion and restore-time deletion replay. | H | Yes |
| Graph artifact | Explicit fallback/batch edges; inherited checkpoints; ID-only state. | H | Yes |
| Data/index artifact | Complete target version tuple and correctly scoped unique indexes. | H | Yes |
| State artifact | Orthogonal states; historical acceptance immutable. | H | Yes |
| Scoring artifact | Anchored nullable rubric; bounded repair; unresolved critique stays visible. | M | Yes |
| Quota artifact | Atomic attempt reservations, fractional carry and bounded borrowing. | H | Yes |
| Cost artifact | Common workload; model/effort-specific caching; measured token forecasts. | M | Yes |

## 5. TOP 5 disagreements still requiring resolution

1. **Can embeddings permanently exclude an otherwise eligible ad?**  
   I argue no. Retain a priority queue with aging and exploration allocations. If volume exceeds budget, show backlog and explicit expiry outcomes. A small random audit cannot guarantee coverage.

2. **What evidence permits automatic merging or confirmed closure?**  
   Distinct requisitions must survive identical titles and boilerplate. Closure requires authoritative, complete, fresh snapshots. Reversibility helps recovery but does not justify avoidable false merges.

3. **What constitutes an acceptable score under missing evidence or critic disagreement?**  
   Unknowns require explicit representation; criticism requires resolution. Neither denominator renormalization, forced integer ratings nor numerical convergence establishes correctness. The default ranking treatment of uncertainty still needs evaluation.

4. **What is the unit of durable work and billing?**  
   It must be an immutable target and an individual dispatched attempt. Logical-call IDs, current job rows and checkpoint replay alone cannot protect history or spending. This determines schema, retries, fencing and batch reconciliation.

5. **How should pacing interact with strict fallback and unused reserves?**  
   Routine SerpApi supplementation contradicts its fallback role; zero-credit pacing can make fallback ineffective. Use bounded initial credit/emergency borrowing, preserve hard account limits, and define separately contractual exhaustion, local pacing, reserve policy and provider failure.