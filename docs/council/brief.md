# Architecture Council Brief — "Job Hunter" (v1, 2026-10-16)

You are one member of an architecture council (members: Claude Opus 5.5, Claude Fable 5.1, OpenAI Codex/GPT-6). Each member first writes an **independent** proposal; later rounds cross-critique until consensus. Be concrete, opinionated, production-minded, and honest about risks. Prefer boring, proven technology unless a requirement forces otherwise. Where facts are needed, use the research reports listed in §7 (they were gathered live on 2026-10-16); do not rely on stale memory for versions, quotas or API behaviour.

## 1. Mission
A **production-grade, single-user** web application that, on a configurable schedule, collects job advertisements for the **German market** from several sources, **deduplicates** them, and **ranks** them by fit against the user's **CV/resume** (experience, skills, seniority, languages, domain, location/work-mode preferences, etc.), using **LangChain/LangGraph** for the agentic workflow. The user browses ranked results in a web UI.

Implicit success criteria (to be confirmed with the user in the spec): scheduled runs keep finding and ranking new ads unattended **without exceeding free API quotas**, **without silently dropping or mis-scoring ads**, and every score is **traceable** (inputs, rubric, model, reasoning) and reproducible enough to trust.

## 2. Hard requirements (from the user)
1. Collect job ads from multiple platforms based on relevance to a given CV.
2. Rank collected ads by fitness to the CV (experience, skills, etc.).
3. Collection runs on a **configurable schedule** (e.g., every 6h, every 1d).
4. **Two-page UI**:
   - **Jobs (list/search)** with filters, e.g., time since posted, ranking score, location, job title, etc.
   - **Profile/Settings**: upload/set the CV; override/configure runtime parameters (e.g., schedule interval); define search limits/scope (locations, industry, required language, remote/on-site/hybrid, etc.).
5. **LangChain/LangGraph** for the agentic workflow. Decide explicitly which tasks are **deterministic** (e.g., fetching/scraping, normalization) vs **agentic/LLM** (e.g., relevance matching, multi-criteria scoring). Apply agentic design patterns & best practices: **parallelization**, **generator–evaluator/optimizer**, **error handling & fault tolerance**.
6. **Deduplication** of job records.
7. **Single user / single tenant**.
8. **Production design, not a prototype.** The system will undergo in-depth critical reviews for logical/integrity errors and **silent mistreatment** (silently dropped records, swallowed errors, wrong-but-plausible scores, stale data presented as fresh, quota overruns, etc.). Design to make such failures impossible or loudly visible.
9. Free design choices: sources, DB (SQL/NoSQL), vector store, caching, backend/frontend frameworks, filter/search-scope defaults.

## 3. Decisions already made by the user (do not re-litigate; design within them)
- **Market:** Germany only (UI and ads may be German or English).
- **Sources:**
  - **JSearch** (OpenWeb Ninja via RapidAPI) — primary paid aggregator (Google-for-Jobs based), **free plan: 200 requests/month**.
  - **SerpApi Google Jobs** — **fallback** used only when JSearch is exhausted or failing; **free plan: 250 searches/month, 50/hour**, shared across all SerpApi engines.
  - **Adzuna API** (free key available).
  - Additional **free/no-key** sources are welcome (e.g., Bundesagentur für Arbeit Jobsuche, Arbeitnow, company ATS boards) — see research.
  - **No direct scraping** of LinkedIn/Indeed/StepStone (ToS, anti-bot); their ads reach us via Google-for-Jobs aggregators.
- **Embeddings:** a **local** multilingual embedding model (no hosted embedding API).
- **LLM:** Anthropic Claude via API (key available as env var). No OpenAI API key for the app.
- **Deployment:** **single VPS + Docker Compose**.
- **Access:** **built-in login** (single user), internet-exposed behind HTTPS.
- **One active CV** (+ one search scope). Uploading a new CV re-scores existing ads.
- **LangSmith** API key available (optional tracing).
- **Source policy decisions (made after research, 2026-10-16):**
  - **Bundesagentur für Arbeit (BA) Jobsuche: SKIP entirely** (unofficial API; BA site terms forbid automated collection). Do not design a BA adapter.
  - **Adzuna: use under its personal-use terms** (this is the user's personal job hunt). Its descriptions are truncated to 500 chars → such records must carry an explicit "partial description" flag and the scoring must reflect lower confidence; do not scrape Adzuna's detail pages.
  - **Company "watchlist" in v1: YES.** The user maintains a list of target companies in Settings; the app detects/polls their public ATS boards (Greenhouse, Lever incl. EU host, Ashby, Personio XML, Recruitee; SmartRecruiters needs per-posting detail calls; Workable rate-limits aggressively) at no paid-quota cost.
  - **Arbeitnow: include** (free, full descriptions, Europe-wide feed without server-side filters → filter locally).
  - **Remotive, Careerjet, EURES, GermanTechJobs: skip** (terms/limits/no API — see research).
- **Recorded fixtures** contain recruiter names/emails/phones → must be scrubbed before being committed.

## 4. Constraints & known facts
### 4.1 Quota math (paid aggregators)
- Combined free budget ≈ **450 paid searches/month** (200 JSearch + 250 SerpApi fallback).
- At a 6h schedule (~120 runs/month) that is ~3.7 paid searches per run; at 24h (~30 runs) ~15 per run. The design needs a **persistent quota ledger** per provider (monthly reset, hourly cap for SerpApi), a **budget planner** that spreads searches over the month, freshness-limited queries, and a UI warning when settings would overrun the budget. Free sources should carry volume.

### 4.2 Claude API facts (from Anthropic's current docs, cached 2026-10-06)
| Model | ID | Context | Input $/MTok | Output $/MTok |
|---|---|---|---|---|
| Claude Fable 5.1 | `claude-fable-5-1` | 1M | 10.00 | 50.00 |
| Claude Opus 5.5 | `claude-opus-5-5` | 1M | 4.00 | 20.00 |
| Claude Sonnet 5.5 | `claude-sonnet-5-5` | 1M | 2.00 | 10.00 |
| Claude Haiku 5.5 | `claude-haiku-5-5` | 1M | 0.10 (≤100K prompt) | 0.50 |
- Anthropic's guidance: default to `claude-opus-5-5`; using cheaper models for sub-tasks is **the user's cost decision**, so the design must make the **model per graph node configurable** and present **cost estimates** for alternatives (the user will choose).
- `claude-opus-5-5`, `claude-sonnet-5-5`, `claude-fable-5-1`: **forced `tool_choice` (`any`/`tool`) returns HTTP 400**; assistant **prefill returns 400**; thinking can't be disabled on Opus 5.5 (control depth via `output_config.effort` low…max; default `medium`). Use **native structured outputs** (`output_config.format` with JSON schema) or `tool_choice: auto` + `strict: true`. ⚠ LangChain's `with_structured_output` may default to forced tool calling — see tech research.
- Handle `stop_reason: "refusal"` (and server-side `fallbacks` beta exists); `max_tokens` stop; typed SDK errors (429/5xx retryable vs 4xx not).
- **Prompt caching**: prefix-match caching (tools → system → messages); the CV/profile + rubric is a stable prefix reused across all job scorings → large savings; min cacheable prefix 512–4096 tokens depending on model; verify via `usage.cache_read_input_tokens`.
- **Message Batches API**: 50% cost, asynchronous (results may take up to 24h) — relevant because scheduled runs are not latency-sensitive (trade-off vs freshness).
- Dev-container gotcha: `ANTHROPIC_BASE_URL` is set to an internal proxy in the dev container; the app must configure its own base URL/key explicitly (`JOBHUNTER_ANTHROPIC_API_KEY`).

### 4.3 Environment / ops
- Dev container: 4 vCPU, 15 GB RAM, no GPU; Python 3.13/uv, Node 22, Postgres 16 (+pgvector installed), Redis 7, Docker available. Prod VPS size: propose one (e.g., 4 vCPU/8–16 GB) and justify.
- Secrets via env vars: `JOBHUNTER_ANTHROPIC_API_KEY`, `RAPIDAPI_KEY`, `SERPAPI_API_KEY`, `ADZUNA_APP_ID`, `ADZUNA_APP_KEY`, `LANGSMITH_API_KEY`.
- The CV is personal data (GDPR): data minimisation, deletion, no PII in logs/traces unless deliberate.
- UI testing will be done with Microsoft `playwright-cli`; code reviews by other top-tier models.

## 5. Decision points to resolve (each: Decision · Rationale · Rejected alternatives · Risks)
- **D1** Backend/agent language & frameworks (Python vs TypeScript LangGraph; API framework).
- **D2** Process topology (API, worker(s), scheduler, embedding runtime) and scheduler/queue technology; single-run guarantees (no overlapping runs), missed-run/catch-up semantics, manual "Run now", graceful shutdown/resume.
- **D3** Database, vector store, caching layer (is Redis needed at all?).
- **D4** Source adapters: common interface, **query planning** from CV + scope (which queries to send to which source), freshness windows, pagination, **quota ledger & budget planner**, JSearch→SerpApi fallback policy, rate limiting, retries/backoff, circuit breakers.
- **D5** Normalised job-ad schema; **deduplication** (exact keys, canonical URL, cross-source fuzzy/near-duplicate detection — trigram/simhash/embeddings), merge policy and provenance for multi-source records, reposts/updates, expiry/closed ads.
- **D6** Deterministic vs agentic split and the **LangGraph graph(s)**: nodes, edges, state schema, fan-out (`Send`), concurrency limits, retry policies, checkpointing/durability, idempotency, partial-failure handling, dead-letter/quarantine.
- **D7** **Scoring design**: deterministic hard filters vs soft criteria; embedding pre-filter (threshold/top-N, recall safeguards); LLM multi-criteria rubric and JSON schema; **evaluator–optimizer** loop (when the critic runs, acceptance criteria, max iterations); calibration/consistency across runs; **score versioning** (CV version × rubric version × model); re-scoring triggers; cost controls (prompt caching, batch, model per node); explanations shown to the user.
- **D8** CV ingestion: PDF/DOCX parsing, LLM structuring into a profile (skills, experience, languages, seniority…), user review/edit of the extracted profile, versioning.
- **D9** Settings model: configurable runtime params & validation; schedule semantics; **search-scope fields and sensible defaults for Germany** (locations + radius, industries, required languages, work mode, employment type, seniority, minimum score, max ad age, excluded companies/keywords…).
- **D10** Jobs page: filters, sorting, keyset pagination, full-text search (German), per-ad status (new/seen/saved/applied/hidden?), detail view with score breakdown & explanation, expired-ad handling.
- **D11** Frontend stack, API style (REST + OpenAPI?), auth (password hashing, sessions/cookies, CSRF, login rate limiting, initial credential bootstrap), security headers.
- **D12** Error handling & fault tolerance: provider outages/429s, partial runs, LLM refusals/timeouts/invalid output, budget exhaustion — **no silent drops**: every ad has an explicit pipeline state; run reports visible in the UI.
- **D13** Observability: structured logs, metrics, run history, LangSmith tracing (PII policy), health checks, alerting for a single user.
- **D14** Testing strategy: unit, contract tests with recorded fixtures, **LLM scoring eval set**, E2E via playwright-cli, CI.
- **D15** Deployment: compose services, TLS/reverse proxy, backups/restore, migrations, resource sizing, embedding model packaging.
- **D16** Data retention & GDPR.

## 6. Required output format (your proposal)
Markdown, ≤ ~6,000 words, containing:
1. Sections **D1–D16** in the format above.
2. An ASCII **architecture diagram**.
3. The **LangGraph graph spec(s)**: state schema (typed), nodes (deterministic vs LLM, which model/effort), edges/conditional routing, fan-out, retry/timeout policy, checkpointing.
4. **Data model**: tables with key columns, constraints, unique indexes (esp. for dedup and idempotency), and the **job-ad pipeline state machine**.
5. **Scoring rubric** (criteria, weights or aggregation, hard vs soft) + the structured-output **JSON schema**, + the evaluator-optimizer protocol.
6. **Quota budget algorithm** (pseudo-code).
7. A list of **"silent failure" risks** and the concrete mitigation for each.
8. A rough **per-run and per-month LLM cost estimate** for at least two model configurations.
9. **Open questions for the user** — only items that genuinely cannot be decided without them.

## 7. Research inputs (read these files)
- `research/sources.md` (+ `research/samples/*.json`) — job-source API facts and live samples.
- `research/tech/tech.md` — LangGraph/langchain-anthropic/embeddings/infra facts and CPU benchmarks.
(Paths are relative to this council directory.)
