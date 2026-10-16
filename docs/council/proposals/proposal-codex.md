# Independent architecture proposal — Codex

This design uses Python, LangGraph, PostgreSQL, and a small React application on one VPS. Its central rule is that **collection, identity, eligibility, scoring, and freshness are separate facts**. A successful HTTP request cannot imply a complete collection, a missing attribute cannot become a mismatch, and an old score cannot appear current.

Provider behavior, package versions, and prices below use the supplied research dated 2026-10-16. Unverified behavior remains a deployment test or an explicit limitation. No paid API calls or file changes were made.

## D1 — Backend and agent frameworks

**Decision:** Python 3.13, FastAPI, Pydantic 2, SQLAlchemy, psycopg 3, Alembic, and Python LangGraph. Start from the researched versions, including LangGraph 1.2.15 and langchain-anthropic 1.7.6; lock the complete dependency set.

**Rationale:** Python has the better verified Claude integration, document parsing ecosystem, and local embedding support. LangGraph runs as a library inside the worker.

**Rejected alternatives:** TypeScript agents introduce documented integration gaps; LangGraph Server adds unnecessary infrastructure.

**Risks:** Rapid dependency changes. Upgrade through contract tests, request-payload tests, and scoring evaluations; never automatically float production versions.

## D2 — Processes, scheduling, and ownership

**Decision:** Separate API, scheduler, workflow worker, and embedding service. Use Procrastinate with PostgreSQL for durable task delivery. A small scheduler polls a database `next_due_at` every 30 seconds; it does not depend on Procrastinate’s default missed-period behavior.

A transaction creates the run, advances the schedule, and writes an outbox event. One active collection orchestration is enforced by a database constraint and renewable ownership lease with a fencing generation.

**Rationale:** Database scheduling makes configurable intervals and missed-run semantics explicit.

**Rejected alternatives:** Redis/Celery adds infrastructure; APScheduler’s shared-store limitations are unnecessary; Temporal is excessive.

**Risks:** Delivery is at least once. Every task has an idempotency key. Lease expiry permits takeover only with a new fencing generation; stale workers cannot commit results.

Semantics:

- Default: every 24 elapsed hours, anchored in UTC.
- Downtime: coalesce missed ticks into one run; record the missed count.
- “Run now”: return the active run if present; otherwise enqueue one idempotently.
- Shutdown: stop admission, finish bounded requests, checkpoint, release ownership.
- Batch scoring is detached from collection ownership, allowing later collection while earlier scores remain explicitly pending.

## D3 — Storage, vectors, and caching

**Decision:** PostgreSQL 17 with pgvector 0.8.7, `pg_trgm`, and German/English full-text indexes. No Redis. Use durable content-addressed artifacts for normalized text, embeddings, and accepted scoring results.

Local embeddings: `snowflake-arctic-embed-l-v2.0`, pinned model revision, direct ONNX Runtime int8, 1,024 dimensions. Store normalized `halfvec(1024)` vectors.

**Rationale:** One transactional database covers identity, scheduling, quotas, audit history, and retrieval. The supplied CPU benchmark supports this embedding choice.

**Rejected alternatives:** Separate vector databases, Elasticsearch, hosted embeddings, and generic graph caching for side-effecting nodes.

**Risks:** ONNX output names and CPU performance vary. Package and test the exact export; use its documented query prefix. Start with exact vector search; introduce HNSW only after measuring latency and filtered-search recall.

## D4 — Sources, planning, and budgets

**Decision:** Implement JSearch primary; SerpApi only upon actual JSearch exhaustion or failure; Adzuna, Arbeitnow, and watchlist ATS polling. No BA adapter or other excluded sources.

Adapter interface:

```text
capabilities()
plan(query, window) -> RequestSpec[]
fetch(RequestSpec) -> RawPage
parse(RawPage) -> ItemOutcome[]
continuation(RawPage) -> Cursor | END | UNKNOWN
```

Capabilities declare billing units, pagination, date semantics, description completeness, and whether absence can indicate closure.

**Rationale:** Provider-specific behavior belongs in adapters; the scheduler and quota gate remain deterministic.

**Rejected alternatives:** Autonomous web browsing, unrestricted scraping, routine dual-aggregator searching, and quota pooling.

**Risks:** Provider coverage is incomplete and schemas drift. Preserve response envelopes and distinguish valid empty responses from malformed results.

Planning and source specifics:

- Generate bilingual role families once per profile/scope change using Claude; validate against profile evidence and expose editable queries in Settings.
- Deterministically rotate role/location cells, retaining broad exploration cells. Yield statistics improve allocation but cannot starve a cell indefinitely.
- JSearch: `/search-v2`, `country=de`, `num_pages=1`, opaque cursors. Never use legacy pagination.
- SerpApi: `gl=de`, `hl=de`, location in query text. Use verified response filter tokens where available; otherwise record that freshness filtering is local.
- Adzuna: country `de`, 50 results/page, date ordering, explicit partial-description flag, attribution, original outbound redirect retained privately. No detail-page scraping.
- Arbeitnow: traverse the European feed, filter locally, inspect cache age. Resume overlapping windows; pagination caps produce coverage warnings.
- Watchlist: discover supported ATS links from a bounded fetch of the supplied public careers page. Validate board ownership and show detection status. Support Greenhouse, Lever global/EU, Ashby, Personio XML, Recruitee, and SmartRecruiters.
- SmartRecruiters: filter clearly irrelevant locations before detail calls; unknown locations remain candidates.
- Workable: retain watchlist entries with an explicit unsupported/blocked status until an authorized endpoint works; no aggressive retries.

Use a 48-hour overlap around successful query watermarks, rounded outward to supported date windows. Never advance a coverage watermark past an incomplete traversal. Search-result ranking limits still prevent a completeness guarantee; report them.

## D5 — Normalization and deduplication

**Decision:** Separate source listings, immutable observations, canonical opportunities, and opportunity revisions. Preserve field-level provenance.

Exact identity uses provider/board/native ID, Google `htidocid`, and verified primary publisher IDs. JSearch uses `job_uid`, never its session-bearing `job_id`.

**Rationale:** Multiple observations can describe one opportunity without discarding conflicting evidence.

**Rejected alternatives:** Title/company uniqueness, embedding-only merging, and unioning every SerpApi application link.

**Risks:** False merges are more harmful than visible duplicates. Conflicting strong identities enter identity review; merging is reversible.

Canonicalization strips known tracking parameters while preserving identity parameters such as Indeed `jk`. Adzuna redirect URLs are not canonical identities.

Automatically merge compatible strong identities or matching full-description fingerprints with matching employer/title/location. Trigram, SimHash, and embedding similarity generate duplicate candidates within a 45-day window; fuzzy-only matches remain separate, visibly grouped in v1.

Prefer verified employer ATS fields, then complete aggregator descriptions, then snippets. Never overwrite complete text with partial text. Preserve salary currency, period, prediction status, multiple locations, work-mode uncertainty, and date precision.

A reappearance updates observations, not original posting dates. Changed content creates a revision; reopening creates an occurrence event. Aggregator disappearance never proves closure.

## D6 — Deterministic and agentic workflow

**Decision:** LangGraph coordinates bounded deterministic acquisition and a separate scoring graph. LLMs structure CVs, propose query families, interpret job/profile evidence, critique scores, and repair rejected outputs. They cannot issue network requests, spend quotas, merge identities, or write arbitrary database records.

**Rationale:** Agentic judgment is useful for semantic fit, while collection and integrity require predictable rules.

**Rejected alternatives:** A general tool-using agent controlling the entire application.

**Risks:** Checkpoint replay can repeat external effects. Database operation receipts and request ledgers, rather than checkpointing alone, provide idempotency.

Use synchronous PostgreSQL checkpoints, bounded `Send` fan-out, explicit errors, immutable input references, and the graph specification below.

## D7 — Scoring and calibration

**Decision:** Hard-filter only confirmed facts against explicit user rules. Missing or ambiguous facts produce `UNKNOWN`, not rejection. Embeddings prioritize work; they never permanently remove an otherwise eligible ad.

Use an evidence-linked rubric, deterministic aggregation, uncertainty intervals, and selective evaluator–optimizer review. Default every LLM node to Opus 5.5; cheaper configurations require the user’s choice.

**Rationale:** A plausible numerical score is insufficient without evidence and version identity.

**Rejected alternatives:** Single holistic scores, fixed cosine rejection thresholds, and treating missing skills as demonstrated deficiencies.

**Risks:** Critics can share generator biases. Maintain human-labeled calibration cases, stratified audits, and visible confidence.

Rescore after activated CV changes, relevant scope changes, material job revisions, or rubric/prompt/model configuration changes. Existing retained ads receive explicit new targets, including saved/applied/closed ads; active ads receive priority. Old scores remain labeled with their historical profile and never qualify as current.

## D8 — CV ingestion

**Decision:** Accept PDF/DOCX with size/page limits and sandboxed parsing. Use pypdf/pdfplumber and python-docx, followed by Claude structuring into evidence-linked profile fields. Review extracted text and profile in Settings before activation.

**Rationale:** Column ordering, scans, and ambiguous dates can otherwise corrupt all downstream scores.

**Rejected alternatives:** Silent automatic activation; heavyweight OCR frameworks in the initial deployment.

**Risks:** Low-text PDFs require a visible extraction failure and a repair path: paste corrected text or explicitly use Claude’s native PDF input. Native PDF processing is not silently substituted because it sends the original document.

Upload creates a draft; activation atomically replaces the one active profile and enqueues rescoring. Do not infer language proficiency, citizenship, age, or work authorization. Preserve overlapping employment dates without double-counting experience.

## D9 — Settings and defaults

**Decision:** Immutable settings/scope versions with one active pointer. Validate bounds and show quota and scoring-backlog forecasts before saving.

Defaults: Germany; 24-hour schedule; German/English ad text; all industries, employment types, and work modes; no salary minimum; 30-day discovery horizon; minimum displayed score zero.

**Rationale:** Broad defaults avoid inventing personal preferences.

**Rejected alternatives:** Automatically treating all CV attributes as hard requirements.

**Risks:** Broad scope can exceed scoring capacity. Warn using observed arrival rates; collection still preserves deferred ads.

Support locations/radius, Germany-eligible remote work, industry, seniority, required working languages, employment types, salary, exclusions, maximum age, scoring budget, model per node, and watchlist. Distinguish ad language from required working language. Unknown dates/location remain visible for review. A display threshold does not alter collection or eligibility.

## D10 — Jobs page

**Decision:** One searchable Jobs page with detail drawer, filters, and persistent run-health banner. Separate `seen_at`, `saved_at`, `applied_at`, and `hidden_at`; these are independent user actions.

**Rationale:** Application progress should survive deduplication, rescoring, and closure.

**Rejected alternatives:** A single mutually exclusive status enum and pagination by offset.

**Risks:** Ranking changes between page requests. Use a signed keyset cursor containing a ranking-generation watermark, sort key, and opportunity ID.

Filters include current/provisional/pending scores, score range, confidence, posting-age basis, first-seen age, location, mode, title, source, application state, and closure. Default ordering uses conservative score bound, then fit score, then first seen. Details show evidence, missing information, source dates, last checked, provenance, and historical scores.

German FTS is supplemented with English/simple search and trigram matching for compounds. Closed ads remain accessible; stale ads are labeled separately.

## D11 — Frontend, API, authentication

**Decision:** React/Vite/TypeScript SPA, TanStack Query, REST/OpenAPI, same-origin HTTPS. Serve static assets through the reverse proxy. Login is a gate; the authenticated application has two pages.

Single-user authentication uses Argon2id and random server-side sessions stored as hashed tokens. Cookies are Secure, HttpOnly, and SameSite; mutations require CSRF tokens and Origin validation.

**Rationale:** No second application server or external identity dependency is necessary.

**Rejected alternatives:** Browser-stored bearer tokens and public registration.

**Risks:** Internet exposure requires bounded login throttling, generic failures, session rotation, and recovery via a VPS-local administrator command. Bootstrap credentials through an interactive command, never a default password or logged token.

Apply CSP, HSTS, frame restrictions, upload limits, escaped descriptions, and `noopener noreferrer`. Watchlist discovery blocks private/link-local addresses, rechecks DNS and redirects, and cannot access metadata endpoints.

## D12 — Fault tolerance

**Decision:** Every fetched item and scoring target has an explicit state, reason, attempt history, and next action. Run reports distinguish complete, empty, partial, deferred, failed, and cancelled outcomes.

**Rationale:** Retrying alone does not prevent silent loss.

**Rejected alternatives:** Catch-and-continue without persisted errors; mapping invalid responses to empty arrays.

**Risks:** Permanent errors need user attention. Quarantine includes replay controls after a parser/configuration fix.

429/5xx/timeouts use bounded backoff; 400/auth/schema errors do not loop. Source breakers are independent. Refusals, truncated outputs, semantic failures, and ambiguous paid-request outcomes are separate categories. Budget exhaustion defers work visibly.

A reconciler checks expired leases, unprocessed observations, missing target outcomes, stuck batches, and counter inconsistencies.

## D13 — Observability

**Decision:** Structured logs with run/request/item/target IDs; database run history; Prometheus-compatible metrics; external heartbeat monitoring. LangSmith is optional and disabled by default.

**Rationale:** A dead VPS cannot alert through its own web UI.

**Rejected alternatives:** LangSmith as the sole operational record, or logs containing complete prompts/CVs.

**Risks:** Metadata can leak personal information. Trace only allowlisted IDs, timings, model identifiers, usage, and error classes; redact child calls as well.

Alert on missed schedules, source outages, stale cached feeds, schema changes, oldest unscored age, quarantine growth, quota discrepancies, disk pressure, and failed backups. Run dashboards reconcile fetched counts with recorded dispositions.

## D14 — Testing

**Decision:** Use unit/property tests for identity, state transitions, dates, and quota arithmetic; PostgreSQL integration tests; scrubbed adapter fixtures; crash-injection tests; human-labeled scoring evaluations; Playwright CLI E2E.

**Rationale:** Integrity failures often survive happy-path tests.

**Rejected alternatives:** Mock-only database tests or checking only JSON validity for scoring.

**Risks:** Recorded responses contain recruiter PII. Produce separately scrubbed fixtures, verify them with automated scanning and manual review, then commit them. Never copy raw research fixtures directly.

CI includes concurrent quota reservations, crashes before/after dispatch and persistence, contaminated application links, partial pagination, CV changes during scoring, duplicate deliveries, refusals, DST display, and backup restoration. Model changes require calibration checks before becoming active.

## D15 — Deployment

**Decision:** Docker Compose services: Caddy, API, scheduler, worker, embedding service, PostgreSQL, and backup/monitoring jobs. Start with 4 vCPU, 16 GB RAM, 100 GB SSD on an EU VPS.

**Rationale:** The measured embedding process uses roughly 2.6 GB; 16 GB leaves room for PostgreSQL, parsing, backups, and workload spikes.

**Rejected alternatives:** Kubernetes, GPU hosting, and runtime model downloads.

**Risks:** One VPS is a failure domain. Define initial objectives of 24-hour backup RPO and four-hour recovery RTO, and verify restoration.

Pin image digests and embedding artifacts; validate checksums and model outputs at build/startup. Keep PostgreSQL and embedding ports private. Use encrypted off-host backups, daily checks, monthly restore drills, resource limits, and disk-watermark admission stops.

Run Alembic and checkpointer setup as deployment jobs before traffic. Checkpointer setup uses the required autocommit connection settings.

## D16 — Retention and privacy

**Decision:** Minimize CV contact data before LLM scoring; retain only job-relevant profile facts. Encrypt originals, sensitive artifacts, and backups. Keep secrets outside images and logs.

Proposed retention: raw responses 14 days; inactive unsaved ads 180 days; operational metadata 12 months; backups 30 days. Retain saved/applied jobs until deletion. Retain minimized scoring inputs while their scores remain available.

**Rationale:** Auditability needs evidence, but not indefinite original CVs and recruiter contact details.

**Rejected alternatives:** Full prompts in hosted traces and irreversible identity merges.

**Risks:** Deletion must cover source text, profiles, embeddings, checkpoints, scores, caches, and exports. Record deletion manifests and replay them before serving a restored backup. Backups expire on schedule; deletion is not falsely presented as immediate physical erasure everywhere.

Adzuna termination triggers removal of Adzuna-derived material, including dependent merged fields and scores. Rebuild affected opportunities from remaining sources.

## Architecture diagram

```text
                         Internet
                            |
                     Caddy / HTTPS
                      /          \
             React static UI    FastAPI
                                  |
                   PostgreSQL: authoritative state
                 /        |         |          \
          schedules    outbox/    identities/   quotas/
                       queue      revisions    scores
              |           |
         Scheduler    Workflow worker
                          |
                LangGraph + checkpoints
                /          |           \
       Source adapters  Local ONNX    Claude API
          |             embeddings    explicit URL/key
          |
   +------+------+-------------------------+
   |             |                         |
JSearch       Adzuna / Arbeitnow      Watchlist ATS
   |
SerpApi only if JSearch exhausted/failing

 PostgreSQL + encrypted files --> encrypted off-host backups
 Metrics + external heartbeat --> configured user alert channel
```

## LangGraph specifications

### Typed state

Large payloads stay in application tables. Graph state contains immutable IDs; every referenced artifact has a content hash.

```python
from typing import Annotated, Literal, TypedDict

class Outcome(TypedDict):
    work_id: str
    disposition: Literal[
        "done", "empty", "partial", "deferred", "quarantined", "failed"
    ]
    artifact_id: str | None
    error_id: str | None

# Deterministic reducer: same work_id + same result is idempotent;
# conflicting results raise an integrity error.
def merge_outcomes(a: dict[str, Outcome],
                   b: dict[str, Outcome]) -> dict[str, Outcome]: ...

class CollectionState(TypedDict):
    run_id: str
    fence: int
    settings_version: str
    profile_version: str
    scope_version: str
    plan_id: str
    work_ids: list[str]
    outcomes: Annotated[dict[str, Outcome], merge_outcomes]

class ScoreState(TypedDict):
    target_id: str
    job_revision: str
    profile_version: str
    scope_version: str
    policy_version: str
    generator_config: str
    critic_config: str
    attempt: int
    draft_id: str | None
    verdict_id: str | None
    status: Literal[
        "ready", "deferred", "generated", "repair", "accepted", "review"
    ]

class ProfileState(TypedDict):
    upload_id: str
    extracted_text_id: str | None
    draft_profile_id: str | None
    status: Literal["parse", "structure", "review", "activated", "failed"]
```

### Nodes and routing

`D` means deterministic; `L` means LLM.

```text
Collection:
START -> snapshot[D] -> plan[D] -> Send(fetch_page[D], bounded work)
fetch_page -> persist_envelope[D] -> parse_items[D]
parse_items -> normalize[D] -> resolve_identity[D]
resolve_identity -> create_revisions_and_targets[D] -> record_outcome[D]
all outcomes -> reconcile[D] -> report[D] -> END

Scoring:
START -> load_target[D] -> eligibility[D]
eligibility -> confirmed_exclusion[D] -> END
eligibility -> embed_and_prioritize[D] -> admission[D]
admission -> deferred[D] -> END
admission -> generate[L] -> validate[D]
validate -> critique[L] when selected
validate -> accept[D] when valid and not selected
critique -> accept[D] | optimize[L] | review[D]
optimize -> validate[D] -> critique[L] -> accept[D] | review[D]
accept -> publish_if_current[D] -> END

Profile:
START -> parse[D] -> quality_gate[D] -> structure[L]
structure -> validate[D] -> interrupt_for_review[D]
review -> activate_and_enqueue_rescoring[D] -> END
```

Pagination is persisted as separate bounded work, not an unbounded graph loop. Expected item errors become outcomes; unexpected integrity errors fail the branch and run visibly.

Default models: Opus 5.5, low effort for structuring/query generation/scoring/repair; medium for critique. Every node is configurable and versioned. Native structured output uses:

```python
model.with_structured_output(
    Schema, method="json_schema", include_raw=True
)
```

Explicit Anthropic base URL and `JOBHUNTER_ANTHROPIC_API_KEY`; no forced tool choice, assistant prefill, sampling controls, or automatic model fallback. Record the actual returned model.

Limits:

- Four concurrent source requests globally; one per host initially.
- Three concurrent Claude calls globally, including repairs.
- One embedding batch at a time; bounded text chunks, no silent truncation.
- HTTP total timeout 45 seconds; Claude HTTP timeout 90 seconds and node timeout 120 seconds.
- Generator/repair output cap 4,096 tokens; critic 2,048; bounded larger retry requires another budget reservation.
- SDK automatic retries disabled. Explicit transient retry policy: at most three attempts, jittered backoff, respecting `Retry-After`. Paid acquisition retries always need fresh reservations.
- One optimizer pass; graph recursion limit 32.
- `AsyncPostgresSaver`, `durability="sync"`, stable thread IDs, deployment-time setup. Resume with `None` on the same thread.

Completed external-call receipts are reused after replay. A crash after dispatch but before receipt remains `AMBIGUOUS`; checkpointing cannot make remote billing exactly once.

Optional Message Batches use the Anthropic SDK directly. Persist batch IDs and unique target `custom_id`s, reconcile every terminal item, and expose pending/expired results. Ambiguous batch submissions are not blindly resubmitted.

## Data model and constraints

All times use `timestamptz`; IDs use UUIDs. Versioned artifacts are immutable. Foreign keys enforce lineage.

| Table | Key columns and constraints |
|---|---|
| `user_account`, `session` | Singleton user; unique username; unique session-token hash; expiry/revocation |
| `profile_version` | Upload/text references, structured facts, evidence, content hash, status |
| `scope_version`, `settings_version`, `model_config`, `rubric_version` | Immutable JSON plus schema/version hashes |
| `active_configuration` | Singleton row; FKs to exactly one active profile/scope/settings |
| `schedule` | Anchor, interval, next due, enabled; positive bounded interval |
| `run` | Trigger, schedule occurrence, captured versions, status, fence, lease; unique scheduled occurrence; partial unique index for active collection |
| `outbox`, queue tables | Unique event/idempotency key; delivery history |
| `source_instance` | Provider, ATS host/board, status; unique provider/host/board tuple |
| `query_cell`, `request_work` | Query fingerprint, coverage window, cursor lineage; unique logical page work key |
| `quota_window`, `request_attempt` | Provider/account/window caps; reservation, dispatch, settlement, request hash, billing outcome; unique attempt UUID |
| `response_envelope` | Attempt FK, encrypted payload/hash, status, cache age, parsing state; unique response receipt |
| `raw_item` | Envelope FK, array index, raw hash, disposition/error; unique `(envelope_id, item_index)` |
| `source_listing` | Source instance/native ID, first/last seen; unique `(source_instance_id, native_id)` |
| `listing_observation` | Listing/raw-item FK, normalized fields, quality, source dates; unique raw-item FK |
| `opportunity`, `membership` | Canonical identity; listing membership history; partial unique active membership per listing |
| `identity_claim`, `merge_event` | Namespace/value/evidence/status; unique active verified claim; conflicts cannot overwrite ownership |
| `job_revision`, `field_evidence` | Immutable selected fields, selected observation references, material hash; unique opportunity/revision number |
| `embedding` | Text hash, model revision, preprocessing version, chunk index, vector; composite unique key |
| `score_target` | Job/profile/scope/rubric/prompt/model-policy versions, state, next retry; unique complete version tuple |
| `score_attempt`, `score_result` | Target, attempt number, input hash, actual model, token/cost usage, JSON, validation/critic verdict; unique target/attempt; at most one accepted result per target |
| `user_job_state`, `pipeline_event`, `incident` | Unique user/opportunity state; append-only transitions; deduplicated active incidents |

Application procedures atomically enforce transitions, ownership fencing, and accepted-result publication. Checks constrain scores to 0–100, lower ≤ score ≤ upper, and nonnegative quota amounts. Accepted results require validated evidence and a valid required critic verdict.

The current-score view joins the active profile/scope/policy and current job revision. It cannot select the newest score merely by timestamp.

Indexes: GIN German/English text vectors; trigram title/company indexes; optional vector HNSW; B-tree score/date/ID pagination indexes; partial indexes on runnable targets, unresolved incidents, and active leases.

### Pipeline state machines

These are separate dimensions, avoiding one contradictory “job status.”

```text
Raw item:
RECEIVED -> PARSED -> NORMALIZED -> LINKED
    \          \          \          \
     +----------+----------+-----------> QUARANTINED
QUARANTINED -> explicit replay -> PARSED

Score target:
PENDING -> ELIGIBILITY_CHECK
  -> EXCLUDED_CONFIRMED
  -> READY -> SCORING -> VALIDATING -> ACCEPTED
                  |          |
                  |          +-> CRITIQUE -> REPAIR -> VALIDATING
                  |                       \-> NEEDS_REVIEW
                  +-> RETRY_WAIT | REFUSED | NEEDS_REVIEW
READY -> DEFERRED_BUDGET -> READY
ACCEPTED -> SUPERSEDED when its version tuple stops being current

Availability:
UNKNOWN <-> OBSERVED_OPEN -> STALE
OBSERVED_OPEN/STALE -> CONFIRMED_CLOSED
CONFIRMED_CLOSED -> OBSERVED_OPEN + reopening event
```

Only complete successful ATS snapshots can support absence-based closure: require two absences at least 24 hours apart. Authentication failures, truncated pagination, and suspicious mass disappearance suspend closure inference. Aggregator-only records become stale, not confirmed closed.

## Scoring rubric, schema, and evaluator–optimizer

### Rubric and uncertainty

| Criterion | Weight |
|---|---:|
| Core skills and tools | 30 |
| Relevant responsibilities and experience | 25 |
| Seniority and scope of ownership | 15 |
| Required working languages | 10 |
| Domain knowledge | 10 |
| Location/work-mode compatibility | 10 |

Observed ratings: 0 confirmed incompatibility, 1 substantial gap, 2 partial fit, 3 strong fit, 4 direct fit. Each criterion has versioned anchors; “more senior” does not automatically mean better.

Unknown is `null`, never zero. Explicitly unconstrained preferences receive 4 with a scope evidence reference.

For weights summing to 100:

```text
L = sum(weight × rating / 4 for resolved criteria)
U = L + sum(weight for unknown criteria)
score = (L + U) / 2
coverage = 1 - sum(unknown weights) / 100
```

The UI shows score, interval, coverage, and confidence. Partial descriptions force low confidence unless replaced by verified full text. These intervals describe missing evidence, not statistically calibrated probabilities.

Hard exclusions are deterministic confirmed country/location constraints, user exclusions, and trusted structured employment constraints. Uncertain Germany eligibility goes to visible location review. LLM-interpreted mandatory mismatches trigger critique and a warning; they do not silently hide an ad.

Embeddings allocate 70% of available scoring slots by relevance, 20% to oldest pending targets, and 10% to stratified exploration. Unused allocations spill over. All remaining targets stay queued. Arrival rate above capacity produces a backlog alert; expiring before scoring is an explicit outcome.

### Generator JSON schema

The application binds versions and target IDs outside model output.

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["criteria", "mandatory_concerns", "summary"],
  "properties": {
    "criteria": {
      "type": "array",
      "minItems": 6,
      "maxItems": 6,
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "criterion", "basis", "rating",
          "job_evidence", "profile_evidence", "explanation"
        ],
        "properties": {
          "criterion": {
            "enum": [
              "skills", "experience", "seniority",
              "languages", "domain", "location_mode"
            ]
          },
          "basis": {
            "enum": ["observed", "unknown", "unconstrained"]
          },
          "rating": {
            "type": ["integer", "null"],
            "minimum": 0,
            "maximum": 4
          },
          "job_evidence": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 8
          },
          "profile_evidence": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 8
          },
          "explanation": {"type": "string", "maxLength": 500}
        }
      }
    },
    "mandatory_concerns": {
      "type": "array",
      "maxItems": 12,
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["job_evidence", "profile_evidence", "explanation"],
        "properties": {
          "job_evidence": {"type": "string"},
          "profile_evidence": {
            "type": "array",
            "items": {"type": "string"}
          },
          "explanation": {"type": "string", "maxLength": 500}
        }
      }
    },
    "summary": {"type": "string", "maxLength": 800}
  }
}
```

Application validation requires each criterion exactly once; checks rating/basis consistency, evidence existence and immutable text spans, and recomputes aggregation. Evidence references identify numbered job sentences, profile facts, or scope rules. Quote existence proves attribution, not semantic correctness; the critic evaluates interpretation.

### Evaluator–optimizer protocol

1. Generate from complete bounded evidence, treating CV/job text as untrusted data.
2. Reject refusals, truncation, malformed JSON, invalid references, and impossible dates before publication.
3. Critique all partial/conflicted records, mandatory concerns, high scores ≥85, and threshold-borderline scores; also audit a reproducible 10% sample.
4. Critic examines original evidence and outputs a typed verdict: `accept`, `repair`, or `insufficient_evidence`, plus criterion-specific defects. It checks unsupported claims, omitted requirements, language interpretation, and rating anchors.
5. One repair receives evidence and concrete defects. Revalidate and recritique.
6. Publish only after all required checks pass. Otherwise retain `NEEDS_REVIEW`; do not convert failure into a neutral score.

Maintain at least 100 human-labeled bilingual examples, including snippets, career transitions, and contradictions. Track pairwise ranking agreement, criterion error, false exclusions, duplicate mistakes, and exploration yield. Proposed release gate: zero unsupported hard exclusions in the suite and ≥90% of labeled ratings within one point. Thresholds are engineering targets, not claimed current performance.

## Quota budget algorithm

Budgets belong to provider accounts and actual renewal windows, not calendar months. The nominal 450 searches are **not** a routine combined allowance: healthy JSearch means SerpApi stays unused.

```python
def select_google_provider(now):
    if jsearch.confirmed_exhausted(now) or jsearch.breaker_allows_fallback(now):
        return SERPAPI
    return JSEARCH  # local pacing delay is not provider exhaustion

def admit(request, now):
    with transaction():
        lock_provider_account(request.provider)
        verify_current_ownership_fence()
        verify_renewal_window_or_fail_closed()

        units = worst_case_billable_units(request)
        # JSearch: one page; details, if explicitly enabled: one per ID.
        # SerpApi: reserve one even for expected empty/cached responses.

        remaining = min(
            configured_limit_minus_all_chargeable_and_reserved(),
            conservative_provider_snapshot_remaining()
        )

        elapsed = elapsed_fraction_of_provider_window(now)
        paced_total = floor(normal_allocation * elapsed)
        pacing_room = paced_total - normal_spent_or_reserved()

        # Normal allocation defaults to 90%; final 10% protects retries.
        # Retry class may use reserve, but never exceed hard remaining.
        if request.is_normal and pacing_room < units:
            return DEFERRED_PACING

        for window in applicable_windows(request.provider):
            # SerpApi: monthly + conservative rolling 50/hour.
            # Adzuna: minute/day/week/month; all must pass.
            if window.remaining < units:
                return DEFERRED_QUOTA

        if remaining < units:
            return DEFERRED_QUOTA

        attempt = insert_unique_reserved_attempt(request, units)
        increment_all_relevant_reservations(units)

    mark_dispatched_durably(attempt)
    response = send_once(request)

    with transaction():
        lock_provider_account(request.provider)
        persist_receipt_and_response(response)
        settle_or_mark_ambiguous(attempt, response)
        tighten_limits_from_provider_headers(response)
```

Additional rules:

- Every retry is separately admitted. Lost responses remain conservatively charged.
- A reserved request is refunded only when non-dispatch/non-billing is demonstrable.
- SerpApi account observations may lag. Never use a larger reported balance to erase local unsettled usage; external-account usage reduces remaining allowance.
- Shared SerpApi use cannot be guaranteed safe against uncontrolled concurrent callers. Use a dedicated account allocation or route every consumer through this ledger; otherwise pause when reconciliation is unsafe.
- Reset only from validated renewal information; uncertainty blocks paid requests.
- Valid empty results spend budget and do not trigger fallback.
- Breakers open after repeated transient failures; authentication/configuration failures open immediately and alert. Recovery probes are budgeted.
- Free-source limits remain explicit: conservative host pacing, adaptive 429 handling, request caps, and visible incomplete coverage.
- Manual runs do not bypass pacing or hard limits.

A parallel USD ledger reserves worst-case Claude input plus output-cap cost, including repairs and ambiguous requests. Batch work reserves its complete submitted cost before dispatch.

## Silent-failure risks and mitigations

| Risk | Prevention or visible outcome |
|---|---|
| HTTP 200 changes schema | Envelope validation; quarantine; schema-presence metrics |
| Parsed records disappear | Every response item gets a unique row; fetched-count reconciliation |
| API pagination cap looks complete | Persist continuation state and coverage gaps |
| Old cached feed looks freshly collected | Store cache age, fetch time, source timestamp separately |
| Ingestion timestamp becomes posting date | Typed date basis and precision; no implicit substitution |
| Repeated ads become “new” | Canonical first-seen preserved across aliases and occurrences |
| Unrelated application links merge jobs | Only corroborated primary identities; conflict quarantine |
| Distinct jobs merge fuzzily | Suggestions only for fuzzy-only matches; reversible membership |
| Missing attributes become rejection | Three-valued eligibility; unknowns visible |
| Partial snippets receive confident scores | Mandatory completeness flag and confidence cap |
| Embeddings hide relevant ads | No permanent embedding rejection; aging/exploration queues |
| CV update leaves old scores current | Version-tuple join and atomic activation |
| LLM emits plausible unsupported claims | Evidence validation, mandatory critique classes, audit evaluation |
| Prompt injection changes workflow | No tools or side effects available to scoring models |
| Retry exceeds quota | Durable reservation before every dispatch |
| Crash duplicates remote costs | Persist dispatch intent; ambiguous outcomes charged and surfaced |
| Source outage closes all jobs | Closure requires complete healthy ATS snapshots |
| Score backlog quietly grows | Oldest-target alert and predicted drain time |
| Successful run masks one failed source | Per-source outcomes and partial-run status |
| Entire VPS dies silently | External heartbeat and tested restoration |
| Secrets/PII leak through fixtures or traces | Scrubbing, allowlisted tracing, log URL redaction |
| Deleted data returns after restore | Deletion manifests replayed before service activation |

## LLM cost estimates

Illustrative workload per run:

- 100 generated scores: 4,000 input + 1,600 output tokens each.
- 20 initial critiques plus 10 post-repair critiques: 5,000 input + 800 output each.
- 10 repairs: 5,000 input + 1,600 output each.
- Total: 600,000 input and 200,000 output tokens.

Output assumptions include billed thinking. Actual critique rates may exceed 20%, especially with Adzuna-heavy intake.

| Configuration | Per run | 30 runs/month | 120 runs/month |
|---|---:|---:|---:|
| A: Opus throughout — default | $6.40 | $192.00 | $768.00 |
| B: Sonnet generation/repair; Opus critique | $3.74 | $112.20 | $448.80 |
| C: Haiku generation/repair; Opus critique | $1.21 | $36.39 | $145.56 |

These comparisons hold **100 newly scored targets per run** constant. At 100 targets per day, a six-hour schedule need not quadruple scoring cost; it can distribute that daily workload.

A 1,000-ad CV refresh costs approximately $64/$37.40/$12.13 under the same review proportions. One CV extraction/query refresh is roughly $0.10 on Opus under modest text-token assumptions.

The table excludes transport retries, VPS costs, and caching savings. Budget forecasts add 20% operational headroom and report observed token distributions.

Use a stable profile/rubric prefix and verify cache savings through usage counters; do not assume the prefix meets the model’s minimum. Message Batches can halve eligible model charges: fully batched configuration A would be approximately $3.20 per equivalent workload, but dependent critique/repair rounds add latency. Default online operation avoids up-to-24-hour batch delays; batch mode remains a user choice.

## Genuinely open questions

1. What locations, commuting radius, work modes, working-language requirements, employment types, and exclusions are actual preferences—and which are hard requirements?
2. Which companies or careers-page URLs should seed the watchlist?
3. What monthly Claude budget is acceptable, and should the application use default Opus or one of the cheaper configurations?
4. Is next-day scoring acceptable for savings, or should newly collected jobs normally be ranked within the collection cycle?
5. Are the JSearch/SerpApi subscriptions used elsewhere, and what are their actual renewal dates and available balances?
6. Which off-VPS alert destination should receive missed-run and failure notifications?
7. Should saved/applied history be retained indefinitely, and are the proposed CV/history/backup retention periods acceptable?