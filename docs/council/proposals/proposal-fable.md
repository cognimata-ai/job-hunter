# Job Hunter — Architecture Proposal (Claude Fable 5.1, round 1, 2026-10-16)

Scope: single user, Germany, scheduled collection → dedup → CV-fit ranking, LangGraph agents on Claude, one VPS. All versions/quotas below come from `research/sources.md` and `research/tech/tech.md` (2026-10-16). Cache-price multipliers (§8) are the one assumption not in the brief and are labelled as such.

Guiding rule for every decision: **a record may only leave the pipeline through a named terminal state that the UI can show.** Nothing is dropped by returning early, catching broadly, or filtering silently.

---

## 1. Architecture diagram

```
                 HTTPS (Let's Encrypt)                  Internet APIs
  Browser ──────► caddy ─────────────────┐     ┌────────────────────────────────┐
  (Vite SPA)       │ reverse proxy       │     │ JSearch  SerpApi  Adzuna       │
                   ▼                     │     │ Arbeitnow  ATS boards (GH/Lever│
             ┌──────────┐  REST/OpenAPI  │     │ /Ashby/Personio/Recruitee/SR)  │
             │   api    │ (FastAPI,      │     │ Anthropic Messages API         │
             │ uvicorn  │  serves SPA)   │     └───────▲────────────────────────┘
             └────┬─────┘                │             │ httpx (token buckets,
                  │ SQL (psycopg3)       │             │ circuit breakers, ledger)
                  ▼                      │     ┌───────┴────────────────────────┐
   ┌──────────────────────────────┐      │     │  worker (one container)        │
   │ db: pgvector/pgvector:0.8.7- │◄─────┼─────┤  procrastinate worker          │
   │ pg17  (+pg_trgm, unaccent)   │ SQL  │     │  ├ tick(1 min) → due-run check │
   │  app tables · quota ledger   │      │     │  ├ LangGraph collect_graph     │
   │  LangGraph checkpoints       │      │     │  │   (PostgresSaver, thread=run)│
   │  procrastinate queue         │      │     │  ├ LangGraph score_subgraph    │
   │  sessions · run reports      │      │     │  ├ ingest_cv / rescore tasks   │
   └──────────────────────────────┘      │     │  └ embeddings: onnxruntime int8│
                  ▲                      │     │     arctic-embed-l-v2.0 (in-proc)│
   ┌──────────────┴───────────────┐      │     └────────────────────────────────┘
   │ migrate (one-shot): alembic  │      │     ┌────────────────────────────────┐
   │ + PostgresSaver.setup()      │      │     │ model-init (one-shot): pinned  │
   │ backup (nightly pg_dump →vol)│      │     │ HF revision + sha256 → volume  │
   └──────────────────────────────┘      │     └────────────────────────────────┘
   No Redis. One stateful service (Postgres). Optional: LangSmith (off by default, EU endpoint, inputs/outputs hidden).
```

---

## 2. Decisions D1–D16

### D1 Language & frameworks
- **Decision:** Python 3.13 + `uv`; `langgraph` 1.2.15, `langgraph-checkpoint-postgres` 3.2.0, `langchain-anthropic` 1.7.6, `anthropic` 1.15.0; FastAPI 0.143.2 + Pydantic 2.14; SQLAlchemy 2.1.4 + Alembic 1.20 on `postgresql+psycopg` (psycopg 3.3.6 — one driver shared with the LangGraph checkpointer); `httpx` for adapters; `procrastinate` 3.10 for the queue; `structlog`. All versions pinned exactly (`uv.lock`), upgraded deliberately.
- **Rationale:** the Python `langchain-anthropic` has the forced-`tool_choice` guard, knows the 5.5 model profiles, exposes `reasoning_effort`, typed `fallbacks`, `TracePolicy` and a current SDK; the JS package pins SDK 0.122.x, lacks 5.5 profiles, and its default `withStructuredOutput` sends a forced tool choice that 400s on Opus/Sonnet 5.5 (tech.md B2 #4, B3). Pydantic models double as the structured-output schemas and the DB-boundary validators.
- **Rejected:** TypeScript LangGraph + pg-boss (fewer Claude safeguards, duplicated types across two languages); Django (admin/ORM we don't need); LangGraph Server/`langgraph-api` (Elastic-2.0, extra runtime; the library is enough).
- **Risks:** fast release cadence (patches every 1–3 weeks) → pin exactly, contract tests against recorded fixtures, upgrade in a dedicated PR with eval run.

### D2 Process topology, scheduling, single-run guarantees
- **Decision:** containers `caddy`, `api`, `worker`, `db`, one-shot `migrate`, `model-init`, `backup`. **Exactly one `worker` replica**; it runs the procrastinate worker (Postgres `LISTEN/NOTIFY`, no Redis) with concurrency 2. Scheduling is **not** cron-driven: a `@periodic(cron="* * * * *")` tick task calls `maybe_start_run()`, which computes due-ness from DB state (`settings.interval_minutes`, `MAX(run.started_at)`, `settings.schedule_enabled`). If due, it inserts a `run` row and defers `run_pipeline(run_id)` with `lock="pipeline"`. Guarantees: (1) **no overlapping runs** — partial unique index `run(status) WHERE status IN ('queued','running')` (the DB is the arbiter; procrastinate's lock is belt-and-braces); (2) **missed runs** — after downtime the next tick sees `now − last_start ≥ interval` and starts **one** catch-up run (`trigger='catch_up'`, shown in the report), never N stacked runs; (3) **Run now** — API inserts a `run(trigger='manual')`; 409 if one is active; (4) **graceful shutdown** — worker handles SIGTERM, LangGraph runs with `durability="sync"`, run rows carry `heartbeat_at` (updated by every node); on worker start a reaper finds `running` runs with stale heartbeat (> 5 min) and resumes them once via `graph.invoke(None, {"configurable": {"thread_id": run_id}})` (verified resume semantics in tech.md A3); a second stale detection marks the run `aborted` with reason. Embeddings run in-process in the worker (onnxruntime int8).
- **Rationale:** procrastinate is MIT, Postgres-only, psycopg3 (same driver), DB-enforced once-per-period; its own cron catch-up window (10 min) is too weak for a 6–24 h interval, hence the tick + DB due-ness computation. One worker removes the whole class of distributed-scheduler bugs.
- **Rejected:** APScheduler 3 (fine single-process, but no durable queue for ad-hoc tasks like CV re-score); APScheduler 4 (alpha); Celery/arq/BullMQ (Redis); Temporal (server + DB on one VPS is overkill); cron in the container (no visibility, no lock).
- **Risks:** one worker = throughput ceiling (acceptable: minutes of work per run); worker crash mid-run → covered by checkpoint resume + reaper; procrastinate `LISTEN` connection drops → it polls as fallback; monitored via worker heartbeat in `/readyz`.

### D3 Database, vector store, cache
- **Decision:** PostgreSQL 17 via image `pgvector/pgvector:0.8.7-pg17` (pinned full tag), extensions `vector` 0.8.7, `pg_trgm`, `unaccent`. Vectors as `halfvec(1024)` with HNSW cosine (`hnsw.iterative_scan = relaxed_order` for filtered kNN). **No Redis**: sessions, login-rate-limit counters, quota ledger, queue, checkpoints and LangGraph node cache (not needed) all live in Postgres. HTTP responses from free sources are not cached beyond the raw-payload table.
- **Rationale:** one stateful service → one backup, one restore drill. Ubuntu's `postgresql-16-pgvector` 0.6.0 has no `halfvec` and no iterative scans (tech.md D1) → use the pgvector image. At ≤100k ads the whole corpus is ~200 MB of vectors; a dedicated vector DB adds nothing.
- **Rejected:** Postgres 18 (fine, but 17 has a year more of production mileage; `uuidv7()` not needed — we use app-generated UUIDv7); Qdrant/Weaviate; Redis for anything.
- **Risks:** HNSW build cost on re-embed → rebuild with `CREATE INDEX CONCURRENTLY`; vector dim is fixed per embedding model → `embedding_model` column + re-embed migration path (D5).

### D4 Source adapters, query planning, quota ledger, fallback
- **Decision:** one protocol, ten adapters:
  ```python
  class SourceAdapter(Protocol):
      provider: Provider; billing: Billing  # units per page, window kind, hourly cap
      def plan(self, ctx: PlanContext) -> list[FetchPlan]       # pure, deterministic
      def fetch(self, plan: FetchPlan, cursor: str|None) -> FetchResult  # raw items, next_cursor, cost_units, quota_hint
      def normalize(self, raw: dict) -> NormalizedPosting | NormalizeError   # strict pydantic
  ```
  Adapters: `jsearch` (`GET /search-v2`, `country=de`, location **inside `query`**, `date_posted` from interval: `today` ≤24 h, `3days` ≤72 h, else `week`; `num_pages=1`; cursor for page 2), `serpapi` (`engine=google_jobs`, `gl=de`, `hl=de`, location **in `q`, never the `location` param** — it returned an empty, billed page; no `uds` date tokens in v1, reusability unverified), `adzuna` (`results_per_page=50`, `max_days_old`, `sort_by=date`, ≤3 pages/query), `arbeitnow` (firehose: pages until `created_at < watermark`, max 5 pages/run, ≥60 min between fetches because the CDN caches ~1 h), ATS: `greenhouse`, `lever` (global **and** `api.eu.lever.co`), `ashby`, `personio` (XML), `recruitee`, `smartrecruiters` (list + per-posting detail, ≤4 concurrent, 5 req/s — half the generic 10 req/s limit). Workable: not an adapter (unofficial, 429 on first call).
  **Query planning** (deterministic): `profile.target_titles` (LLM-extracted, user-confirmed, DE+EN variants) × `scope.locations` (+ "Remote" if `remote` in work modes) → `source_query` rows with priority = title rank × location rank. Each run selects paid queries by **round-robin on `last_paid_at`** so every query is refreshed within a budget cycle; free adapters run every query (ATS boards ignore queries — whole board).
  **Pagination rule (paid):** page 2 only if ≥8/10 results on page 1 were new to us and allowance remains.
  **Quota ledger:** `provider_quota` (one row per provider: window start/end anchored to the provider's reset — JSearch `x-ratelimit-requests-reset`, SerpApi `plan_renewal_date` — `limit_units`, `used_local`, `reported_remaining`, hourly counters, circuit state) + append-only `quota_event` per billed request. After every JSearch call the ledger reconciles with `x-ratelimit-requests-remaining`; before and after every run it reads SerpApi `account.json` (free) and uses `this_hour_searches` for the hourly cap (month usage lags). Effective remaining = `min(local, reported)`; divergence > 2 units → warning "another client is using this key". SerpApi **empty results are billed** and recorded as such. Budget planner: §6.
  **Fallback policy:** SerpApi fetches only the JSearch-planned queries that JSearch could not serve in this run, and only when JSearch is (a) exhausted (effective remaining ≤ reserve) or (b) circuit-open (429, or ≥3 consecutive 5xx/timeouts). Never both providers for the same query in one run.
  **Rate limiting/retries/breakers:** in-process token buckets (Adzuna 10/min of its 25; SerpApi 40/h; SmartRecruiters 5/s; Arbeitnow 1/h); adapters do **not** retry internally — the single retry layer is the LangGraph node `RetryPolicy` (3 attempts, exp backoff + jitter, `retry_on=TransientError` = 5xx/timeouts/connection errors only); 429 → honour `Retry-After` once for free sources, for paid providers 429 = circuit open for the run; other 4xx → no retry, `fetch_failed(error_class)`. Circuit state persisted; half-open probe = one request next run.
- **Rationale:** mirrors verified billing (1 unit/page, both Google providers read the same index); freshness filters keep one page per query meaningful at 6 h.
- **Rejected:** Google-Jobs listing engine (ratings only); fetching Adzuna detail pages (ToS); treating SerpApi as co-primary (double-spends on the same index).
- **Risks:** `htidocid` stability across days unverified → it's one key among several (D5); JSearch per-second limit unverified → bucket 1 req/s; schema drift → strict normalizers + field-presence monitors (§7).

### D5 Normalised schema, deduplication, merge, expiry
- **Decision:** two-level model: `job_posting` (one row per `(provider, native_id)`, immutable provenance, idempotent upsert) → `job` (canonical entity, what the user sees and what gets scored). Native ids: JSearch `job_uid` (never `job_id`), SerpApi `htidocid` decoded from `job_id`, Adzuna `id`, Arbeitnow `slug`, ATS ids. **Dedup is deterministic and staged**, each stage recorded in `job_merge_event` with evidence:
  1. **Exact keys** (`job_key(key_type, key_value)` unique): `htidocid`; canonical primary apply URL (lower-case host, `www.` dropped, `utm_*`/`gclid`/`fbclid`/`se`/`v` stripped, fragment/trailing slash dropped); publisher ids parsed from that URL (`stepstone:14607704`, `linkedin:4480111992`, `indeed:jk`, `xing`, `adzuna:id`, `arbeitnow:id`, `join:id`, `ba_refnr` from it-jobs.de). **Only the primary link** (`job_apply_link` / `via` option) — never `apply_options[]` (contaminated, ≥7 foreign links observed). Adzuna `redirect_url` is never persisted (contains `app_id`).
  2. **Fingerprint** = `norm(title) | norm(company) | norm(city)` with normalisation: NFKC, soft-hyphen/NBSP removal, `unaccent`, lower-case, gender tags (`(m/w/d)`, `(w/m/d)`, `(all genders)`, `(f/m/x)`, `(w_m_d)`, `(m|w|d)`, `(*)`), legal forms (`GmbH & Co. KG`, `GmbH`, `SE`, `AG`, `PLC`), `Sr.`→`senior`, whitespace/punctuation collapse. Exact fingerprint match within 45 days → merge.
  3. **Fuzzy** (candidates via `pg_trgm`: `similarity(title_norm) ≥ 0.85 AND similarity(company_norm) ≥ 0.80 AND same city`, 45-day window) → merge **only if confirmed** by description SimHash (64-bit, Hamming ≤ 6) when both have full text, or by embedding cosine ≥ 0.97 (same model). If one side is partial (Adzuna) the fuzzy stage only **links** (`possible_duplicate_of`), never merges.
  Intra-source duplicates (Adzuna re-ingests) go through the same stages. **Merge policy:** `job.primary_posting_id` = best posting by rank (ATS > JSearch > SerpApi > Arbeitnow > Adzuna), tie-break longest non-partial description; merging a partial job with a full-text posting flips `description_is_partial` → triggers re-score. **Reposts/updates:** posting `content_hash` change → new `posting_version` row, `last_seen_at` update; title/description change → `score_state='stale'`. **Expiry:** for *enumerating* sources (ATS boards, Arbeitnow) absence across 2 consecutive successful fetches ⇒ `lifecycle='expired_confirmed'`; for query sources absence is not informative ⇒ `expired_assumed` at `first_seen_at + max_ad_age_days`. Both shown distinctly.
- **Rationale:** matches the linking evidence in sources.md §11.1; reversible merges make wrong merges a visible, fixable event instead of a lost ad.
- **Rejected:** embeddings as the primary dedup signal (cosine bands are model-specific); union-merging on all `apply_options`; LLM-based dedup (cost, non-determinism).
- **Risks:** fingerprint false merges for generic titles at large employers ("Werkstudent" at Siemens in two cities differ by city, so city is in the key; same city, different teams → shown as one job with N postings — acceptable, reversible).

### D6 Deterministic vs agentic split; LangGraph graphs
- **Decision:** **Deterministic:** fetching, normalisation, dedup, hard filters, embedding pre-rank, score aggregation, validation of LLM outputs, budgeting, reporting. **LLM:** CV→profile structuring (once per CV), per-job rubric rating with evidence (generator), critique (evaluator), optional revision. Graph spec in §3. Fan-out via `Send` with `max_concurrency` (4 for fetches, 3 for LLM); per-node `RetryPolicy` with **explicit `retry_on`** (`anthropic.RateLimitError`, `InternalServerError`, `APIConnectionError`, `NodeTimeoutError`, adapter `TransientError`) because the default retries 400s (tech.md A3); per-node `timeout`; per-node `error_handler` that converts an exception into a persisted failure state instead of aborting the superstep; `PostgresSaver` checkpointing, `thread_id = run_id`, `durability="sync"`, `recursion_limit=500` explicit. Idempotency: every DB write keyed on natural keys (`(provider,native_id)`, `(job_id, score_version)`), so a resumed node re-executes safely. Dead-letter: `score_failed` jobs retry in the next 3 runs, then `quarantined` (UI list with error class and raw error id).
- **Rationale:** verified semantics: `Send` + `max_concurrency`, `error_handler` updates, durable resume re-running only the failed sibling (tech.md A3).
- **Rejected:** `create_agent`/ReAct tool loops for scoring (unbounded calls, forced-tool pitfalls); functional API (`@task`) — fine, but StateGraph gives us explicit edges to review; LangGraph node cache (adds nothing with DB idempotency).
- **Risks:** the SDK's own `max_retries` multiplies with graph retries → SDK `max_retries=2` (honours `Retry-After`), graph `max_attempts=2`, bounded 4–6 attempts, all logged in `llm_call`.

### D7 Scoring design
- **Decision:** three stages (details §5). **A. Hard filters** (deterministic, from scope): excluded companies/keywords, employment type, max ad age, language *known* to be incompatible, location/work-mode *known* to be incompatible. Unknown ≠ fail: unknowns pass through flagged. Result `filtered_out(reason)` is a visible state with an "unfilter & score" action. **B. Embedding pre-rank:** cosine(profile_doc, job_doc) with `Snowflake/snowflake-arctic-embed-l-v2.0` int8 ONNX (`query: ` prefix on the profile side); **rank cut-off, not threshold** (model-specific bands): score the top `scoring_cap_per_run` (default 60) **plus** always: watchlist-company jobs, jobs whose `title_norm` trigram-matches any target title ≥ 0.6, and jobs a merge just completed with full text. Below the cut → `prefiltered_low` (cosine shown, "score anyway" button). **Recall safeguard:** each run also LLM-scores a random 5 of the `prefiltered_low` set (cheap, bounded); if any scores ≥ `min_score_display`, the run report raises a *recall warning* and the cap auto-raises by 20 next run (bounded by budget). **C. LLM rubric** (generator) → deterministic validation → aggregation in code → **evaluator** (critic) on: fit ≥ 65, watchlist jobs, validation warnings, and a 10 % random sample; max 2 generator iterations. **Score versioning:** `score_version = (profile_version, rubric_version, generator_model, effort, critic_model, prompt_template_sha)`; `job_score` unique on `(job_id, score_version)`; UI always shows the version. Re-score triggers: new accepted profile (confirmation dialog with cost estimate), rubric/model change (same), partial→full description, material posting change, manual. **Consistency check:** every 7 days re-score 5 random already-scored jobs under the same version; mean |Δ| > 8 → "scorer drift" warning. **Calibration:** 30–50-item golden set (D14). **Cost controls:** prompt caching of the stable prefix (rubric + profile JSON, ~3.7k tokens, `cache_control` on the last system block; `usage.cache_read_input_tokens` stored per call; run-level hit-rate < 50 % → warning), model/effort per node in Settings, `scoring_cap_per_run`, monthly USD cap (scoring stops at the cap; jobs stay `score_pending`, banner shown), Message Batches as a v1.1 transport (same request objects; −50 %; run ends `awaiting_batch`, a poller completes it).
- **Rationale:** code-side aggregation makes scores reproducible and auditable; the critic is targeted where errors cost the user (high scores) and sampled elsewhere for calibration; recall sampling makes the pre-filter's main failure mode (dropping good ads) observable.
- **Rejected:** LLM-produced overall score (opaque, drifts); fixed cosine threshold (model-dependent); critic on everything (2× cost; keep as a Settings option).
- **Risks:** German/English mixed ads → rubric instructs bilingual evidence; partial descriptions → confidence `low`, badge, excluded from "top picks" by default filter.

### D8 CV ingestion
- **Decision:** accept PDF/DOCX ≤ 10 MB. Text layer via `pypdf` 6.20 (fallback `pdfplumber` 0.11) / `python-docx` 1.2 → `profile_text` (for embedding and display). Structuring: one Claude call (`claude-opus-5-5`, `reasoning_effort="medium"`, PDF sent as native `document` block, DOCX as extracted text) with `with_structured_output(CandidateProfile, method="json_schema", include_raw=True)`. Fields: `summary`, `target_titles[]` (DE+EN), `skills[{name, level, years, evidence}]`, `experience[{title, company, start, end, highlights}]`, `education`, `languages[{code, cefr}]`, `seniority`, `domains[]`, `total_years`, `location_hints`. The result is a **draft**; the user reviews/edits every field in Settings; acceptance creates `candidate_profile` version N (`status='active'`, partial unique index on `status='active'`), embeds `profile_doc` (summary + target titles + skills), and enqueues `rescore_all` automatically (the acceptance screen shows the estimated cost from `llm_call` averages; it blocks only if the estimate would breach `llm_monthly_usd_cap`, asking the user to raise the cap). Text-less PDFs (scans) → explicit error "no text layer"; no OCR in v1.
- **Rationale:** single document → cost negligible (~$0.10); native PDF input avoids AGPL PyMuPDF and multi-GB docling images; user review removes the "plausible but wrong profile" failure.
- **Rejected:** docling/unstructured (torch-sized images), PyMuPDF (AGPL), fully automatic profile activation.
- **Risks:** extraction quality on multi-column CVs → the LLM gets the PDF itself, pypdf text is only the embedding fallback; refusals/`max_tokens` → surfaced as ingestion error.

### D9 Settings model
- **Decision:** single `settings` row (JSONB validated by Pydantic `Settings` v-schema, `version` int, `settings_history` append-only). Groups: **schedule** (`enabled`, `interval_minutes` ∈ [60, 10080], `quiet_hours` optional), **scope** (`locations[{name, lat, lon, radius_km}]`, `remote_ok`, `work_modes ⊆ {remote, hybrid, onsite}`, `languages[{code, min_cefr}]` the user can work in, `employment_types`, `seniority_levels`, `industries[]` optional, `max_ad_age_days` default 14, `excluded_companies[]`, `excluded_keywords[]` default `["Praktikum","Werkstudent","Ausbildung","Freelance"]` editable, `min_score_display` default 50), **watchlist** (`companies[{name, ats_type, board_token, enabled}]` with "detect ATS" helper that probes the six URL patterns once and stores the result + last-OK time), **budgets** (`jsearch_monthly_cap` default 180 / reserve 20, `serpapi_monthly_cap` 225 / reserve 25, `adzuna_daily_cap` 150, `llm_monthly_usd_cap` default 60), **scoring** (`generator_model`, `generator_effort`, `critic_model`, `critic_effort`, `critic_policy` ∈ {targeted, all, off-except-high}, `scoring_cap_per_run`, `recall_sample_size`). Defaults for Germany: locations seeded from the profile's `location_hints` at first setup (user confirms), `remote_ok=true`, `work_modes=all`, `languages=[de:B2, en:C1]` pre-filled from the CV. **Validation:** field-level (ranges, enums) + cross-field by the budget planner in dry-run: the UI shows "at 6 h and 12 paid queries, each query is refreshed every ~4 runs; JSearch 120/180 units/month" and a red warning if a cap would be exceeded or coverage < 1 query/run. Schedule semantics: interval measured from the last run **start**; `next_run_at` displayed; saving settings re-evaluates due-ness on the next tick. Secrets never in settings (env only).
- **Rationale:** one versioned document is easy to diff, audit and roll back; every run stores `settings_version` for traceability.
- **Rejected:** per-field tables; env-var-only configuration (not editable in UI).
- **Risks:** schema evolution → Pydantic migration function per version, tested.

### D10 Jobs page
- **Decision:** list with filters: fit score range, confidence, posted/first-seen window, location (city/radius), work mode, source/provider, user status (`new/seen/saved/applied/hidden`), company, partial-description toggle, lifecycle (active / expired, expired hidden by default), score state (scored / pending / failed / filtered / prefiltered). Text search: German FTS (`to_tsvector('german', unaccent_immutable(title||' '||description))` generated column, GIN) **plus** trigram `ILIKE` on `title_norm` for compounds ("Entwickler" inside "Softwareentwickler"). Sorting: fit desc (default), posted_at_best, first_seen, score confidence. **Keyset pagination** on `(sort_key, job_id)` with an opaque cursor. Detail view: score breakdown per criterion (rating, weight, evidence quotes from ad and CV), knockouts, confidence + reasons, critic verdict/issues, iterations, `score_version`, model/effort, token usage, timestamps (`posted_at_best` with precision label, `first_seen_at`, `last_seen_at`), all postings/providers with links (primary apply URL + source pages, Adzuna attribution label), merge history, actions: status, note, re-score, "report wrong score" (→ `score_feedback`, feeds the eval set), un-merge, unfilter.
- **Rationale:** the brief's traceability criterion is met only if every number on screen links to its inputs.
- **Rejected:** offset pagination; Elasticsearch; LLM-generated "why this job" prose beyond the stored rubric summary.
- **Risks:** FTS cannot decompound → trigram fallback; large descriptions in list → list returns snippets only.

### D11 Frontend, API, auth, security
- **Decision:** Vite 8.3 + React 19.3 + TypeScript + TanStack Query 5.104 / Router 1.170, built to static files served by FastAPI behind Caddy; REST + OpenAPI (FastAPI), TS client generated from the spec in CI (drift = build failure). **Auth:** single account; Argon2id (`argon2-cffi`); server-side sessions in Postgres (`session.id` = 256-bit random, cookie `HttpOnly; Secure; SameSite=Strict; Path=/`, 14-day idle / 90-day absolute expiry, id rotation on login); CSRF: `SameSite=Strict` + `Origin`/`Sec-Fetch-Site` check + required `X-Requested-With` header on mutating requests; login rate limit 5 failures / 15 min per IP and per account in `login_attempt` (Postgres); bootstrap via one-shot CLI `docker compose run --rm api jobhunter set-password` (no default credentials, no password in env). Security headers (Caddy + Starlette middleware): strict CSP (`default-src 'self'`), HSTS, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `X-Content-Type-Options`. Uploads: MIME sniffing, size limit, stored outside web root. API errors are RFC 9457 problem details; no stack traces to clients.
- **Rationale:** boring, auditable; a SPA avoids a second Node runtime; one user ⇒ no RBAC.
- **Rejected:** JWT in localStorage (XSS exposure), Next.js (extra runtime), OAuth providers (external dependency for one user).
- **Risks:** password loss → CLI reset; optional TOTP second factor (open question).

### D12 Error handling & fault tolerance
- **Decision:** every error is classified (`error_class` enum: `provider_4xx`, `provider_5xx`, `provider_timeout`, `quota_exhausted`, `circuit_open`, `normalize_error`, `llm_rate_limit`, `llm_refusal`, `llm_invalid_output`, `llm_max_tokens`, `llm_timeout`, `budget_cap`, `internal`) and persisted on the entity (`fetch`, `job`, `job_score`, `run`). Run outcome: `completed` (all planned fetches OK, all scoring tasks terminal-success), `partial` (any fetch failed/skipped or any score failed) with a per-item list, `failed` (graph exception), `aborted`. **End-of-run invariants** (fail the run loudly if violated): `Σ postings by state = Σ fetched items`; every job touched by the run has a terminal `score_state`; `Σ quota_event.units = planned spend`; no job has `score_state='scoring'`. Provider outages → partial run, circuit open, next run probes. Budget exhaustion → paid fetches `skipped(quota)`, free sources still run, banner "paid search paused until <reset date>". LLM refusal (`stop_reason='refusal'`) → `score_refused`, retried once with a neutral re-phrase, then terminal; invalid output → one regeneration with validator errors, then `score_invalid`; `max_tokens` → `llm_max_tokens` (never silently truncated: `include_raw=True` and stop-reason check on every call). Run reports (counts per state, per provider units, warnings, cost) are stored and shown on both pages (banner on Jobs when the last run is not `completed`).
- **Rationale:** states + invariants make "silently dropped" structurally impossible to miss.
- **Rejected:** retry-until-success loops; best-effort `except Exception: pass` anywhere (lint rule `BLE001` + review checklist).
- **Risks:** too many warnings → severity levels (`info/warn/error`), only `error` and budget warnings produce banners.

### D13 Observability
- **Decision:** `structlog` JSON logs with `run_id`, `job_id`, `provider`, `node`, `error_class`; **never** CV text, descriptions, prompts or recruiter PII (log ids, lengths, hashes). Metrics: `/metrics` (Prometheus text) on the internal network only + the same counters persisted in `run.report` (so the UI shows them without Prometheus): fetch units, items, new/merged, state counts, LLM tokens/cost, cache hit-rate, latencies, provider health. Health: `/healthz` (process up), `/readyz` (DB, migrations current, worker heartbeat < 2 min, embedding model checksum OK, last successful run age < 2× interval, last backup < 36 h). Alerting for one user: in-app banners + optional push via `ntfy` topic or SMTP (Settings) on `run.status ∈ {partial, failed}`, quota < reserve, LLM cap reached, readiness failing (checked by the tick). LangSmith: **off by default**; if enabled: `LANGSMITH_TRACING=true`, `LANGSMITH_HIDE_INPUTS=true`, `LANGSMITH_HIDE_OUTPUTS=true`, `LANGSMITH_ENDPOINT=https://eu.api.smith.langchain.com` when the key is EU, `TracePolicy(process_inputs=omit_payload)` on LLM nodes, metadata = ids only; `LANGSMITH_GATEWAY` must be unset (startup assertion).
- **Rejected:** full-payload traces, log shipping to a SaaS (PII).
- **Risks:** silence ≠ health → readiness includes "last run age" so a dead scheduler is visible.

### D14 Testing
- **Decision:** (1) unit: normalisers (German dates/salaries/locations, gender tags), canonical URL/ID parsing, dedup stages, aggregation, budget planner (property tests: never exceeds caps), state transitions. (2) Contract tests per adapter against **scrubbed fixtures** from `research/samples` (recruiter names/emails/phones replaced before commit, assert zero `@`/phone patterns) via `respx`; field-presence assertions = drift detectors. (3) Graph tests with `GenericFakeChatModel` + real Postgres (compose service): resume after injected crash, fan-out failure isolation, invariants. (4) **LLM eval set**: 30–50 real ads × active profile labelled by the user (fit bucket + knockouts); run manually/nightly with a $2 cap; gates: Spearman ≥ 0.7 vs labels, zero missed hard knockouts, evidence-verbatim rate 100 %; results stored per `score_version`. (5) E2E with `playwright-cli` against compose in `JOBHUNTER_FAKE_SOURCES=1` mode (fixture adapters, fake LLM): login, CV upload → review → accept, settings, Run now, jobs list/filters/detail. (6) CI (GitHub Actions): ruff, mypy `--strict`, pytest + Postgres service, image build, E2E; the real-API eval is a manual workflow.
- **Rejected:** mocking the DB; snapshot tests of prompts.
- **Risks:** eval labels age with the CV → eval set versioned with `profile_version`.

### D15 Deployment
- **Decision:** Compose services: `caddy` (TLS via Let's Encrypt, HTTP→HTTPS, headers), `api`, `worker`, `db`, `migrate` (alembic upgrade + `PostgresSaver.setup()`; `api`/`worker` `depends_on: condition: service_completed_successfully`), `model-init` (downloads the `snowflake-arctic-embed-l-v2.0` repo's `model_int8.onnx` export + tokenizer at a pinned HF revision, verifies sha256, writes to the `models` volume; worker refuses to start on checksum mismatch), `backup` (nightly `pg_dump -Fc` to a volume, 14 daily + 4 weekly, optional `restic` off-site; restore drill documented and tested quarterly). Images pinned by digest; `.env` with the six secrets; `ChatAnthropic(base_url="https://api.anthropic.com", api_key=JOBHUNTER_ANTHROPIC_API_KEY, ...)` explicit (dev proxy gotcha). **VPS: 4 vCPU / 8 GB RAM / 80 GB NVMe** — worker peak ≈ 2.6 GB (int8 embedding, measured) + 0.5 GB Python, Postgres 1.5 GB (`shared_buffers=1GB`), api 0.3 GB, caddy 0.05 GB ⇒ ~5 GB with headroom; 4 vCPU keeps embedding (2–4 ads/s) off the API's path. Choose 16 GB only if switching to torch/Qwen3 (5.6 GB peak).
- **Rejected:** Kubernetes; managed Postgres (cost, data residency); TEI container (throughput unverified, extra container; revisit if CPU contention appears).
- **Risks:** Let's Encrypt rate limits on re-deploys → persistent Caddy data volume; disk growth → raw-payload retention (D16) + size metric.

### D16 Retention & GDPR
- **Decision:** CV files: only the active and the previous document kept, older deleted on acceptance; profile JSON versions kept (needed to interpret historical scores) but `profile_text` of retired versions purged after 30 days. Raw provider payloads: 90 days (reprocessing window). Jobs: `expired` jobs and their scores deleted 180 days after expiry unless `saved/applied`; `llm_call` rows (no content, only counts/hashes) 13 months. Recruiter PII inside ad text is public content, displayed but excluded from logs/traces. "Delete everything" (CV, profile, scores, sessions) and "Export my data" (JSON) in Settings. Backups encrypted at rest (restic) and bound by the same retention. LangSmith off by default (D13). Processing record: a `PRIVACY.md` listing data categories, purposes, retention and third parties (Anthropic, SerpApi, RapidAPI, Adzuna).
- **Rejected:** indefinite raw retention; sending CV text to LangSmith.
- **Risks:** Anthropic data handling for API inputs → documented; zero-retention terms are the user's call (open question).

---

## 3. LangGraph graph spec

### 3.1 `collect_graph` (one invocation per run; `thread_id=run_id`; `durability="sync"`)

```python
class FetchOutcome(TypedDict):  plan_id: str; status: Literal["ok","failed","skipped"]; items: int; new_posting_ids: list[int]; units: int; error_class: str|None
class ScoreOutcome(TypedDict):  job_id: int; status: Literal["scored","score_failed","score_refused","score_invalid"]; score_id: int|None; error_class: str|None; usage: dict

class RunState(TypedDict):
    run_id: str; settings_version: int; profile_version: int; score_version_id: int
    budget: BudgetSnapshot                       # per-provider allowance for this run
    plans: list[FetchPlan]                       # set by plan_queries
    fetch_outcomes: Annotated[list[FetchOutcome], operator.add]   # fan-in reducer
    new_job_ids: list[int]; filtered: dict[int, str]; scoring_tasks: list[ScoreTask]
    score_outcomes: Annotated[list[ScoreOutcome], operator.add]
    warnings: Annotated[list[RunWarning], operator.add]
```

| Node | Kind | What it does | Policy |
|---|---|---|---|
| `load_context` | det | loads settings, active profile, score_version, reconciles ledger (JSearch header snapshot, SerpApi `account.json`), writes `run.heartbeat` | timeout 30 s, no retry |
| `plan_queries` | det | adapter `plan()` for every provider; applies §6 allowances; writes `run.plan` (auditable); `0 plans` ⇒ warning `error` level | — |
| `fetch` | det, **`Send` per FetchPlan**, `max_concurrency=4` | adapter `fetch()` → raw payload row → `quota_event` → strict `normalize()` → upsert `job_posting`; returns `FetchOutcome` | `RetryPolicy(retry_on=TransientError, max_attempts=3)`, timeout 90 s; `error_handler` → `FetchOutcome(status="failed")` |
| `ingest` | det (sequential) | dedup stages 1–3, merges, `job` upserts, embeddings for new/changed jobs (batch 16), lifecycle/expiry updates | timeout 20 min, `TimeoutPolicy(refresh_on="heartbeat")` |
| `select_candidates` | det | hard filters → `filtered`; pre-rank → `scoring_tasks` (cap + always-include + recall sample + retries of `score_failed` ≤ 3 runs) | — |
| `score` | **LLM subgraph, `Send` per ScoreTask**, `max_concurrency=3` | §3.2 | per-call timeout 120 s, node timeout 240 s; `retry_on` = Anthropic transient; `error_handler` → `ScoreOutcome(status="score_failed")` |
| `finalize` | det | invariants (D12), counts, cost, cache hit-rate, recall warning, `run.status`, notifications | no retry; failure ⇒ `run.status='failed'` |

Edges: `START → load_context → plan_queries → [Send fetch…] → ingest → select_candidates → [Send score…] → finalize → END`. Conditional: `plan_queries` with zero plans still proceeds (ATS/Arbeitnow may be disabled) but records an `error`-level warning; `select_candidates` with zero tasks skips `score`. LLM nodes carry `trace_policy=TracePolicy(process_inputs=omit_payload, process_outputs=omit_payload)`.

### 3.2 `score_subgraph` (per job; state = `ScoreTask` + intermediates; own checkpointer = `False`, inherits the parent's)

```
generate(LLM, generator_model/effort) → validate(det) ─fail→ regenerate_once(LLM) → validate ─fail→ [score_invalid]
        └ok→ aggregate(det) → route(det) ─no critic→ persist(det)
                                         └critic→ critique(LLM, critic_model/effort) ─accept→ persist
                                                   └revise & iter<2→ revise(LLM gen) → validate → aggregate → critique → persist (flag `disputed` if still revise)
```
- Model calls: `ChatAnthropic(model=…, base_url=…, api_key=…, max_tokens=2048, timeout=120, max_retries=2, reasoning_effort=…)` (no `temperature`/`top_p`), `.with_structured_output(Schema, method="json_schema", include_raw=True)`; stop-reason branch: `refusal → score_refused`, `max_tokens → llm_max_tokens`. System prompt = `[rubric_block, profile_block(cache_control=ephemeral)]`, human = ad block (title, company, location, work-mode facts from the deterministic layer, `description_is_partial` flag, text ≤ 6k tokens, truncated with marker and `truncated=true` recorded).
- `validate`: Pydantic parse; every `evidence_ad` quote must occur verbatim (whitespace-normalised) in the ad text and every `evidence_cv` quote in `profile_text`/profile JSON; ratings 0–5; knockout codes from the enum; unknown-language/location contradictions with deterministic facts → warning (routes to critic).
- `persist`: `job_score` upsert on `(job_id, score_version)`, `llm_call` rows (tokens incl. `cache_read_input_tokens`, cost, latency, stop reason, request hash), `job.score_state`.
- Bounded: ≤ 5 LLM calls per job.

### 3.3 `ingest_cv_graph`
`extract_text(det) → structure_profile(LLM Opus 5.5, medium, PDF document block, json_schema) → validate(det) → save_draft(det) → END`. Acceptance is an ordinary API action (no `interrupt`: nothing is gained by holding a thread open for days). `rescore_all` is a procrastinate task that invokes `collect_graph` in `mode="rescore"` (skips `fetch`, selects all active jobs under the new `score_version`, respects `llm_monthly_usd_cap`, chunked 200 jobs per invocation so each chunk checkpoints).

---

## 4. Data model & state machines

Key tables (PK `id` UUIDv7 unless noted; timestamps `timestamptz`):

| Table | Key columns | Constraints / indexes |
|---|---|---|
| `settings` | `id=1`, `data jsonb`, `version int`, `updated_at` | `CHECK (id=1)`; `settings_history(version, data, changed_at)` |
| `user_account` / `session` / `login_attempt` | `username`, `password_hash`; `session.id bytea`, `user_id`, `expires_at`, `last_seen_at`; `login_attempt(ip, username, at, ok)` | unique `username`; index `login_attempt(ip, at)` |
| `cv_document` | `sha256`, `filename`, `mime`, `path`, `uploaded_at`, `status` | unique `sha256` |
| `candidate_profile` | `version int`, `cv_document_id`, `profile_json`, `profile_text`, `embedding halfvec(1024)`, `embedding_model`, `status (draft/active/retired)`, `accepted_at` | unique `version`; **partial unique `(status) WHERE status='active'`** |
| `company` | `name_norm`, `display_name`, `watchlist bool`, `ats_type`, `ats_token`, `ats_status`, `ats_last_ok_at`, `excluded bool` | unique `name_norm` |
| `source_query` | `provider`, `query_key`, `query_text`, `params jsonb`, `priority`, `enabled`, `last_run_at`, `last_paid_at`, `last_count`, `zero_streak int` | unique `(provider, query_key)` |
| `run` | `trigger (schedule/manual/catch_up/rescore)`, `status`, `started_at`, `finished_at`, `heartbeat_at`, `settings_version`, `profile_version`, `score_version_id`, `plan jsonb`, `report jsonb`, `error` | **partial unique `(status) WHERE status IN ('queued','running')`** |
| `fetch` | `run_id`, `provider`, `source_query_id`, `page`, `units int`, `http_status`, `items`, `new_items`, `duration_ms`, `status`, `error_class`, `reported_remaining`, `raw_payload_id` | index `(run_id)`, `(provider, created_at)` |
| `raw_payload` | `sha256`, `body jsonb`, `fetched_at` | retention 90 d |
| `job_posting` | `provider`, `native_id`, `job_id`, `first_run_id`, `first_seen_at`, `last_seen_at`, `seen_count`, `content_hash`, `title`, `company_raw`, `location_raw`, `city_norm`, `description_text`, `description_is_partial`, `posted_at_source`, `posted_at_precision (exact/day/hour/relative/ingest/unknown)`, `posted_raw`, `apply_url_canonical`, `source_url`, `publisher`, `remote_flag`, `work_mode`, `employment_types text[]`, `salary_min/max/currency/period`, `salary_predicted`, `language`, `normalize_warnings jsonb` | **unique `(provider, native_id)`** (idempotent upsert); index `(job_id)`; `posting_version(posting_id, content_hash, seen_at, diff jsonb)` |
| `job` | `primary_posting_id`, `company_id`, `title`, `title_norm`, `company_norm`, `city_norm`, `fingerprint`, `simhash bigint`, `description_text`, `description_is_partial`, `posted_at_best`, `posted_at_precision`, `first_seen_at`, `last_seen_at`, `lifecycle`, `expired_at`, `embedding halfvec(1024)`, `embedding_model`, `score_state`, `score_attempts int`, `current_score_id`, `tsv tsvector GENERATED`, `possible_duplicate_of` | index `fingerprint`; GIN `tsv`; GIN `title_norm gin_trgm_ops`, `company_norm gin_trgm_ops`; HNSW `embedding halfvec_cosine_ops`; `CHECK (score_state IN (...))` |
| `job_key` | `key_type`, `key_value`, `job_id`, `posting_id` | **PK `(key_type, key_value)`** — the cross-source exact-dedup guarantee |
| `job_merge_event` | `kept_job_id`, `merged_posting_id`, `method (exact_key/fingerprint/fuzzy_simhash/fuzzy_embedding/manual)`, `evidence jsonb`, `created_at`, `reverted_at` | index `(kept_job_id)` |
| `score_version` | `profile_version`, `rubric_version`, `generator_model`, `generator_effort`, `critic_model`, `critic_effort`, `prompt_template_sha`, `embedding_model` | unique on the tuple |
| `job_score` | `job_id`, `score_version_id`, `run_id`, `status`, `fit_score int`, `confidence`, `criteria jsonb`, `knockouts jsonb`, `summary_de`, `summary_en`, `critic jsonb`, `iterations int`, `disputed bool`, `usage jsonb`, `cost_usd numeric`, `error_class`, `created_at` | **unique `(job_id, score_version_id)`**; `CHECK (fit_score BETWEEN 0 AND 100)` |
| `llm_call` | `run_id`, `job_id`, `role`, `model`, `effort`, `request_sha`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `cost_usd`, `stop_reason`, `latency_ms`, `error_class` | index `(created_at)`, `(run_id)` |
| `provider_quota` | `provider PK`, `window_start`, `window_end`, `limit_units`, `reserve_units`, `used_local`, `reported_remaining`, `reported_at`, `hour_used`, `hour_started`, `day_used`, `circuit_state`, `circuit_open_until`, `consecutive_failures` | — |
| `quota_event` | `provider`, `run_id`, `fetch_id`, `units`, `reported_remaining`, `at` | index `(provider, at)` |
| `job_user_state` | `job_id PK`, `status (new/seen/saved/applied/hidden)`, `note`, `updated_at` | — |
| `score_feedback` / `eval_case` | `job_score_id`, `user_rating`, `comment`; `eval_case(job_id, profile_version, label_bucket, knockouts, notes)` | — |
| LangGraph `checkpoints*`, procrastinate `procrastinate_jobs*` | managed by the libraries | created by `migrate` |

**Job lifecycle:** `active → expired_confirmed` (enumerating source: absent in 2 consecutive OK fetches) | `expired_assumed` (query source: `first_seen + max_ad_age_days`) → `active` again if re-seen (`seen_count++`).

**Job score state (per `score_version`):**
```
new ──hard filter──► filtered_out(reason)          (unfilter → score_pending)
 │──pre-rank cut──► prefiltered_low                  (score anyway / recall sample → score_pending)
 └───────────────► score_pending ──► scoring ──► scored
                       ▲               │──► score_failed(error_class)   (auto-retry ≤3 runs → quarantined)
                       │               │──► score_refused | score_invalid (terminal, visible)
                       │               └──► budget_held (LLM cap)  (auto → score_pending at cap reset)
            stale (profile/rubric/material change) ◄── scored
```
Invariant checked at `finalize`: no job of the run remains in `scoring`.

**Run:** `queued → running → completed | partial | failed | aborted`.

---

## 5. Scoring rubric, JSON schema, evaluator–optimizer protocol

**Hard filters (deterministic, before any LLM call):** excluded company / keyword hit; employment type not in scope; `posted_at_best`/`first_seen` older than `max_ad_age_days`; structured `work_mode` known and not in scope **and** location outside all radii; ad language detected (fasttext-free heuristic: stop-word ratio) not in the user's languages at any level. Unknown values never filter.

**Soft criteria (LLM rates 0–5 each with evidence; weights fixed by `rubric_version`):**

| Criterion | Weight | What 5 means |
|---|---|---|
| `skills` | 0.25 | all must-have skills in the ad are evidenced in the CV at the required depth |
| `role` | 0.20 | responsibilities match what the candidate has demonstrably done |
| `experience` | 0.15 | years/seniority match in both directions (over-qualification is a mismatch) |
| `domain` | 0.10 | industry/product domain familiarity |
| `language` | 0.10 | required working languages vs CEFR levels in the profile |
| `location_mode` | 0.10 | nuance beyond the hard filter (office days, relocation, travel) vs scope |
| `terms` | 0.10 | contract type, hours, salary signal vs preferences |

**Aggregation (code, not LLM):** `fit = round(100 · Σ wᵢ·rᵢ / 5)`; each `hard` knockout caps `fit` at 25; each `soft` knockout subtracts 10 (floor 0). `confidence = low` if `description_is_partial` or `len(missing_information) ≥ 3` or `truncated`; `medium` if any criterion has empty evidence; else `high`.

**Generator output schema (native structured outputs; length limits enforced in Pydantic, not in the schema, to stay in the supported subset):**
```json
{"type":"object","additionalProperties":false,
 "required":["criteria","knockouts","missing_information","summary_de","summary_en","ad_language"],
 "properties":{
  "criteria":{"type":"object","additionalProperties":false,
   "required":["skills","role","experience","domain","language","location_mode","terms"],
   "properties":{"skills":{"$ref":"#/$defs/c"},"role":{"$ref":"#/$defs/c"},"experience":{"$ref":"#/$defs/c"},
     "domain":{"$ref":"#/$defs/c"},"language":{"$ref":"#/$defs/c"},"location_mode":{"$ref":"#/$defs/c"},"terms":{"$ref":"#/$defs/c"}}},
  "knockouts":{"type":"array","items":{"type":"object","additionalProperties":false,
   "required":["code","severity","evidence_ad","reason"],
   "properties":{"code":{"type":"string","enum":["language_level","must_have_skill","certification","degree","clearance","work_permit","seniority_gap","onsite_required","contract_type","other"]},
     "severity":{"type":"string","enum":["hard","soft"]},"evidence_ad":{"type":"string"},"reason":{"type":"string"}}}},
  "missing_information":{"type":"array","items":{"type":"string"}},
  "ad_language":{"type":"string","enum":["de","en","other"]},
  "summary_de":{"type":"string"},"summary_en":{"type":"string"}},
 "$defs":{"c":{"type":"object","additionalProperties":false,
   "required":["rating","evidence_ad","evidence_cv","note"],
   "properties":{"rating":{"type":"integer","minimum":0,"maximum":5},
     "evidence_ad":{"type":"array","items":{"type":"string"}},
     "evidence_cv":{"type":"array","items":{"type":"string"}},
     "note":{"type":"string"}}}}}
```
**Critic output schema:** `{"verdict":"accept"|"revise","issues":[{"criterion":<enum|"knockouts">,"direction":"over"|"under"|"missing","reason":str,"evidence_ad":str}],"confidence_in_verdict":"high"|"medium"|"low"}`.

**Protocol (per job, bounded ≤ 5 calls):**
1. `gen₁ = generator(system=[rubric, profile(cached)], user=ad)`; `validate`: schema, verbatim evidence, enum codes, deterministic-fact contradictions (e.g. ad says "100 % remote" structured flag but LLM rated `location_mode` 1 → warning).
2. Validation failure → `gen₂ = generator(..., validator_errors)`; failure again → `score_invalid`.
3. `fit, confidence = aggregate(gen)`.
4. Critic runs iff `fit ≥ 65` ∨ watchlist ∨ validation warnings ∨ `random() < 0.10` (policy `targeted`; `all` and `off-except-high` selectable). Critic sees rubric, profile, ad, and `gen` (not the numeric fit — it judges ratings vs evidence) and must cite ad text for each issue (validated verbatim too).
5. `revise` → `gen′ = generator(..., critic_issues)` (same model), re-validate, re-aggregate, critic again; after 2 iterations without `accept` → persist `gen′` with `disputed=true` (UI badge, sorted normally, filterable).
6. Persist everything (both generations, critic outputs, usage); the UI shows the chain.

Golden-set and weekly consistency checks (D7) are the calibration loop; user "wrong score" feedback appends to `eval_case`.

---

## 6. Quota budget algorithm

```python
def plan_allowance(p: Provider, now, s: Settings, L: Ledger) -> int:
    q = L.quota(p)                                   # window anchored to provider reset/renewal date
    if q.window_end <= now: q = L.roll_window(p)     # new window: used_local=0, reported_remaining=None
    remaining = min(q.limit_units - q.used_local,
                    q.reported_remaining if q.reported_remaining is not None else inf) - q.reserve_units
    if remaining <= 0 or q.circuit_state == "open" and q.circuit_open_until > now:
        return 0                                     # recorded as skipped(quota) / skipped(circuit)
    runs_left = max(1, ceil((q.window_end - now) / s.interval))        # includes this run
    fair_share = (q.limit_units - q.reserve_units) * s.interval / (q.window_end - q.window_start)
    allowance = min(remaining, floor(remaining / runs_left), ceil(2 * fair_share))   # spread + bounded carry-over
    if p.hourly_cap:  allowance = min(allowance, p.hourly_cap - q.hour_used - 1)    # SerpApi: this_hour_searches from account.json
    if p.daily_cap:   allowance = min(allowance, p.daily_cap - q.day_used)          # Adzuna 250/day
    return max(allowance, 0)

def plan_paid_fetches(s, L, queries) -> list[FetchPlan]:
    plans = []
    a_js = plan_allowance(JSEARCH, now, s, L)
    todo = sorted(queries.paid, key=lambda q: (q.last_paid_at or EPOCH, -q.priority))   # stalest first
    served, unserved = todo[:a_js], todo[a_js:]
    plans += [FetchPlan(JSEARCH, q, pages=1, date_posted=freshness(s.interval)) for q in served]
    if unserved and (L.exhausted(JSEARCH) or L.circuit_open(JSEARCH)):                 # fallback policy
        a_sp = plan_allowance(SERPAPI, now, s, L)
        plans += [FetchPlan(SERPAPI, q, pages=1, location_in_q=True) for q in unserved[:a_sp]]
    for q in queries.adzuna:  plans += [FetchPlan(ADZUNA, q, pages=min(3, plan_allowance(ADZUNA, …)//len(queries.adzuna)), max_days_old=freshness_days(s.interval))]
    plans += free_plans(ARBEITNOW, ATS…)                                              # no units, own rate buckets
    return plans

# page-2 rule (inside fetch): request page 2 only if new_items >= 8 and L.try_spend(p, 1) succeeds (atomic UPDATE … WHERE used_local + 1 <= limit - reserve).
# after each billed call: L.record(p, units, reported_remaining) ; if |local_remaining - reported| > 2: warn("key used elsewhere").
# settings dry-run: coverage = a_js / len(queries.paid); if coverage < 1: UI warning "each paid query refreshes every ceil(1/coverage) runs".
```
`try_spend` is a single-row conditional `UPDATE` returning the new value — the ledger is the mutex, so a concurrent manual run (impossible by the run index, but defence in depth) cannot double-spend.

---

## 7. Silent-failure risks and mitigations

| # | Risk | Prevention / surfacing |
|---|---|---|
| 1 | Provider schema drift returns 200 with missing/renamed fields (JSearch `/search`→`/search-v2`, BA v6 precedent) | strict Pydantic normalizers (`extra="forbid"` on required paths); `normalize_error` is a posting state; per-fetch field-presence ratios vs fixture baseline → `warn` on drop > 30 %; raw payload kept 90 d for replay |
| 2 | Query silently returns 0 results (SerpApi `location` param case) | `source_query.zero_streak`; 3 consecutive zero results on a query that had results → warning; SerpApi empty pages recorded as spent units |
| 3 | Quota overrun / `429` | ledger pre-check + atomic `try_spend` + header/account reconciliation after each call; 429 opens circuit; `/readyz` and banner at < reserve |
| 4 | Wrong merge hides an ad | fuzzy merge needs text confirmation; partial-text sides only link; every merge has evidence and `reverted_at`; UI "merged from N postings" + un-merge |
| 5 | Missed duplicate shows twice | `possible_duplicate_of` link; weekly intra-DB fingerprint scan listed in report |
| 6 | Hallucinated evidence / plausible wrong score | verbatim evidence validation; aggregation in code; critic on high scores; 10 % sampling; golden set; weekly consistency re-score; `score_version` on every score |
| 7 | Good ads cut by the embedding pre-filter | rank cut + always-include rules + 5-sample recall probe per run with auto cap raise + "score anyway" |
| 8 | Stale data shown as fresh | `posted_at_precision` label, `first_seen`/`last_seen`, lifecycle badges, Jobs banner "last successful run X h ago" red when > 2× interval |
| 9 | Partial (Adzuna) text scored as full | `description_is_partial` → confidence `low`, badge, excluded from default "top picks" filter; merge with full text re-scores |
| 10 | Crash mid-run loses work or double-processes | PostgresSaver `durability="sync"`, natural-key upserts, resume via reaper, end-of-run count invariants |
| 11 | Exceptions swallowed inside fan-out | `error_handler` per node writes a failure outcome; invariant "every task has a terminal state"; lint forbids bare/broad except |
| 12 | Prompt cache silently inactive (cost ×5) | `cache_read_input_tokens` stored per call; run hit-rate < 50 % → warning; monthly USD cap halts scoring visibly (`budget_held`) |
| 13 | LangGraph default `RetryPolicy` retries 400s; SDK/graph retries multiply | explicit `retry_on`; bounded attempts; `llm_call` records every attempt |
| 14 | Wrong Anthropic base URL / key (dev proxy) | explicit `base_url`+`api_key`; startup assertion host == `api.anthropic.com` unless `JOBHUNTER_ANTHROPIC_BASE_URL` override; `LANGSMITH_GATEWAY` must be unset |
| 15 | Embedding model change makes cosines incomparable | `embedding_model` on job and profile; candidate selection refuses mismatched models and enqueues re-embed; vectors never compared across models |
| 16 | German relative dates mis-parsed ("vor 2 Tagen", "Gestern") | raw string stored, parser unit-tested, unknown → `posted_at=null`, precision `unknown`, UI uses `first_seen` |
| 17 | `apply_options` contamination creates false keys | keys only from the primary link; contract test with the KORSCH/Stolzberger fixtures |
| 18 | Hard filter removes jobs on unknown data | unknown never filters; `filtered_out(reason)` visible with un-filter action; reason counts in the report |
| 19 | Overlapping/duplicate runs | partial unique index on active runs; manual run → 409 |
| 20 | Settings make the run a no-op (0 queries, all disabled) | planner dry-run in UI; run report `error`-level warning "0 plans" |
| 21 | Scheduler dead, nobody notices | `/readyz` includes last-run age; tick writes heartbeat; optional ntfy/SMTP alert |
| 22 | Backups silently failing | `backup` writes a `backup_run` row with size; readiness fails when last OK backup > 36 h |
| 23 | LLM refusal or `max_tokens` truncation parsed as a score | `include_raw=True`, stop-reason branch before parsing; terminal visible states |
| 24 | Re-score surprises the user with cost | cost estimate shown at CV acceptance; `rescore_all` is chunked and stops at the monthly cap (`budget_held`, banner) |

---

## 8. LLM cost estimates

**Token assumptions (per job):** stable cached prefix (rubric + profile JSON + instructions) = **3,700 tokens**; uncached ad block = **1,200 tokens** (2.4–4.4k-char descriptions + facts); generator output = JSON **600** + reasoning **600** at Opus medium ⇒ 1,200 output tokens (Sonnet at effort low: 900; Haiku, no auto-thinking: 700). Critic: uncached input 1,800 (ad + generator JSON) + cached prefix; output 800 (Opus/Sonnet) / 500 (Haiku). Policy `targeted` ⇒ critic on **30 %** of jobs, revisions on **5 %**. Cache pricing assumption (not in the brief, standard Anthropic ratios): read = 0.1× input, 5-min write = 1.25× input; one cache write per run. Volume: **6 h schedule, 120 runs/month, 20 scored jobs/run ⇒ 2,400 jobs/month**; cap 60/run ⇒ 7,200 max.

| Config | Generator | Critic | $/job (gen) | $/job (critic × 0.3) | $/job total | $/run (20) | $/month (2,400) | $/month at cap (7,200) |
|---|---|---|---|---|---|---|---|---|
| **A "Anthropic default"** | Opus 5.5, medium | Opus 5.5, medium | 0.0048 + 0.0015 + 0.0240 = **0.0303** | (0.0072 + 0.0015 + 0.0160) × 0.3 = **0.0074** | + revisions 0.0015 = **0.039** | 0.78 + 0.02 cache write | **≈ 96** | ≈ 283 |
| **B "balanced"** | Sonnet 5.5, low | Opus 5.5, medium | 0.0024 + 0.0007 + 0.0090 = **0.0121** | **0.0074** | + 0.0006 = **0.020** | 0.41 | **≈ 48** | ≈ 145 |
| **C "budget"** | Haiku 5.5 | Sonnet 5.5, low | 0.00012 + 0.00004 + 0.00035 = **0.0005** | (0.0036 + 0.0007 + 0.0070) × 0.3 = **0.0034** | **0.004** | 0.08 | **≈ 10** | ≈ 29 |

Without prompt caching, each call pays the 3,700-token prefix at full input price: A +$0.019/job (+49 %), B +$0.012 (+59 %), C +$0.0026 (+65 %) — which is why the cache hit-rate is monitored. Message Batches (v1.1) halves every figure. One-offs: CV structuring ≈ $0.10 (Opus, PDF); `rescore_all` of 2,000 active jobs ≈ A $78 / B $40 / C $8; golden-set eval run (40 jobs, critic on all) ≈ A $2.2 / B $1.5 / C $0.5. Fable 5.1 ($10/$50) is not proposed for any node (≈ 2.5× Opus for no identified need). Recommendation to present to the user: **B** as default (Sonnet evidence extraction is the easy half; Opus judgement where it matters), with A and C one setting away.

---

## 9. Open questions for the user

1. **Adzuna terms:** operate as "personal research" (individual) or as a company (`cognimata.io` domain ⇒ 14-day trial then licence)? This decides whether Adzuna is in v1 at all.
2. **Model configuration & spend:** choose A/B/C (or a custom per-node mix) and the `llm_monthly_usd_cap` default; accept Message Batches (up to 24 h delay) as an option later?
3. **Freshness vs quota:** default interval 6 h (1 JSearch page/run) or 12 h (3 pages/run)? Which target cities/radii and is remote-only Germany in scope?
4. **Watchlist seed:** the initial company list and whether SmartRecruiters' per-posting detail calls (hundreds per board) are acceptable at your interval.
5. **Critic policy default:** `targeted` (proposed) or `all` (≈ +70 % cost, maximal scrutiny)?
6. **Retention:** keep expired jobs/scores for 180 days (proposed) or shorter; keep the previous CV file for rollback or delete on replace?
7. **Second factor:** add TOTP to the single login (recommended for an internet-exposed app holding a CV)?
8. **Notifications:** ntfy, e-mail (SMTP credentials) or in-app only?
9. **LangSmith:** leave off, or enable with hidden inputs/outputs (region: US or EU account)?
10. **Anthropic data handling:** is standard API retention acceptable for CV content, or do you require a zero-data-retention arrangement before go-live?
