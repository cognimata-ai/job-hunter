# Consensus draft v1 — chair synthesis after round 2 (2026-10-16)

Inputs: brief.md, facts-addendum.md, proposals (O = Opus, F = Fable, C = Codex), critiques. Items marked **[RULING]** were disputed in round 2; the chair decided them (majority + rationale). Items marked **[USER]** are escalated to the user and must be designed as configurable until answered. Everything else was agreed by all three members.

---

## A. Stack & LLM gateway
1. Python 3.13; FastAPI 0.143.x, Pydantic v2, SQLAlchemy 2.1 + Alembic, psycopg 3.3 as the single driver (shared with `langgraph-checkpoint-postgres`); LangGraph 1.2.15 **as a library only**; langchain-anthropic 1.7.6 + `anthropic` SDK; exact pins via uv lockfile. Frontend: React 19 + Vite 8 + TypeScript + TanStack Query/Router, built to static assets served same-origin by the API.
2. **`LLMGateway`** is the only path to Claude. It always: passes `base_url="https://api.anthropic.com"` and the key from `JOBHUNTER_ANTHROPIC_API_KEY` explicitly; uses `with_structured_output(method="json_schema", include_raw=True)`; **streams** (final-message helper) so long thinking can't trip HTTP timeouts; sets `max_tokens` per node sized for thinking + output; requires an **explicit model and effort per node** (config without effort is rejected); never sends forced `tool_choice`, prefill or `temperature` (lint rule + payload contract tests); branches on `stop_reason` (`refusal` → `refused` with category; `max_tokens` → one retry with larger cap, then `failed_max_tokens`); records `model_served` and usage.
3. Server-side refusal **fallbacks off** by default (a silently different model would break score versioning).
4. Retries: SDK `max_retries=2` (honours `Retry-After`) for 429/5xx/connection errors; LangGraph `RetryPolicy` with an explicit `retry_on` that excludes all 4xx and our validation errors. The LLM spend ledger reserves the worst case `(input + max_tokens) × (max_retries + 1)` before a call and settles from `usage`.
5. Prompt caching: explicit breakpoint at the end of the shared prefix (rubric + confirmed profile), 5-minute TTL; role-specific instructions after the breakpoint; per-run cache-hit monitoring (alert if `cache_read_input_tokens` stays 0 across consecutive same-prefix calls).
6. A one-time **schema dry-run** test per structured-output schema against the API (cheap) to confirm the JSON-schema subset is accepted and not silently stripped; range/length limits are enforced in the Pydantic validator regardless.

## B. Topology & scheduling
1. Compose services: `caddy` (TLS/HSTS), `api` (FastAPI + SPA), `worker` (**single replica**, procrastinate worker running the LangGraph graphs), `embedder` (onnxruntime, `snowflake-arctic-embed-l-v2.0` int8, model baked into the image at a pinned revision, sha256 verified at start), `postgres` (`pgvector/pgvector:0.8.7-pg17`), `migrate` (one-shot: Alembic + `PostgresSaver.setup()` + procrastinate schema), `backup`. **No Redis.**
2. **[RULING] No separate scheduler container, no lease/fencing service, no outbox** (O+F over C): with one worker replica, a DB uniqueness constraint and a procrastinate lock, concurrent orchestrations are unrepresentable. C's real concerns are covered by:
   - **Transactional admission** where procrastinate can defer on the same connection/transaction (to be verified in a spike); otherwise a **reconciler** re-defers or fails any `queued` run without a live job after 2 min.
   - **Lightweight fencing:** every run row carries `attempt_no`; every state-changing write of a run checks `WHERE run_id = :id AND attempt_no = :attempt` (CAS), so a zombie invocation cannot commit after a resume.
   - **Stalled-job release:** at worker start and on every tick, procrastinate jobs stuck in `doing` past the heartbeat window are marked failed before a resume is deferred; **resume runs as a locked task**, never a direct `graph.invoke`.
3. Schedule: a 1-minute procrastinate periodic tick computes due **slots** `anchor + k·interval` (UTC) from the active settings version and inserts `runs(kind='collect', scheduled_slot)` (unique on `(kind, scheduled_slot)`). After downtime only the latest missed slot runs; skipped slots are recorded `missed`; a slot that falls during an active run is recorded `skipped_overlap` (unique-violation handled as an outcome, never an error loop).
4. One active collection: partial unique index on a constant expression — `CREATE UNIQUE INDEX one_active_collect ON runs ((true)) WHERE kind='collect' AND status IN ('queued','running')` (fixes F's per-status index).
5. **Scoring is a detached lane** that drains score targets (section G); it is not a `runs` row competing with collection for the same index (fixes O's self-collision).
6. Worker heartbeats every 30 s on the run row; nodes that can exceed 60 s heartbeat internally; stale > 3 min ⇒ resume from the LangGraph checkpoint (`durability="sync"`); 3 resumes ⇒ `failed` + alert.
7. "Run now" is idempotent (returns the active run if one exists). Manual runs use paid sources only if the user ticks "include paid sources" (budget impact shown).
8. Graceful shutdown: stop admission, finish bounded in-flight calls, checkpoint, exit.

## C. Storage
1. Postgres 17 + pgvector 0.8.7, `pg_trgm`, `unaccent` via an IMMUTABLE wrapper, a `german_unaccent` text-search config; trigram search complements German FTS for compounds ("Softwareentwickler" vs "Entwickler").
2. Embeddings: `halfvec(1024)` keyed by `(embedding_model_id, text_sha256)`; **exact scan** until > 50k vectors, HNSW only when measured latency requires it.
3. Raw provider payloads stored compressed for **30 days**; billed paid responses are **replayed from the stored payload** on resume instead of being re-requested.

## D. Sources & query planning
1. Adapter protocol: `capabilities()`, `plan(query, window) → RequestSpec[]`, `fetch(spec) → RawPage` (exactly one HTTP attempt; the caller holds the quota reservation), `parse(page) → ItemOutcome[]` (every raw item gets a disposition: accepted / duplicate / malformed(reason) / filtered(reason)), `continuation(page) → Cursor | END | UNKNOWN`.
2. **JSearch** (primary): `/search-v2`; location in the query text ("<title> in <city>, Germany"); `country=de`; `date_posted` = smallest provider bucket ≥ `now − query.last_covered_at + 24 h` (per-query window, not per schedule interval); 1 page per query per planned slot; **concurrency 1**; status-based settlement (2xx = billed; RapidAPI gateway 404 with `x-rapidapi-proxy-response: true` = not billed); identity via `job_uid` (= Google `htidocid`), never `job_id`.
3. **SerpApi Google Jobs** (fallback): used only when JSearch is **provider-exhausted, circuit-open, auth-failed or contract-broken** — never because of local pacing; `plan_fallback` routes unserved JSearch specs to SerpApi **within the same run**; location in `q`, never the `location` param; `gl=de`, `hl=en` (keeps structured `detected_extensions`; to be confirmed with one recorded call); empty results count as billed; identity via `htidocid` decoded from `job_id`.
4. **Adzuna** (personal use): `/v1/api/jobs/de/search`, `max_days_old` from the query window, 50 results/page, all four rate windows enforced (25/min, 250/day, 1,000/week, 2,500/month); `description_completeness='partial'` (500 chars); `redirect_url` stored privately as the compliant outbound link, never used as identity; Adzuna detail pages are never fetched.
5. **Arbeitnow:** ≤ 3 pages per run with a `created_at` watermark; local filter to Germany; never used to close ads.
6. **Watchlist ATS:** Greenhouse, Lever (global **and** EU host), Ashby, Personio XML, Recruitee, SmartRecruiters (detail calls for new postings and when the list shows a change, plus a periodic refresh); Workable deferred. The user enters company + ATS + slug, or a careers URL that is **parsed by pattern only** (no arbitrary URL fetching ⇒ no SSRF surface).
7. Query planning: an LLM proposes a query set (target titles × locations) from the confirmed profile + scope; the **user reviews/edits it in Settings**; runtime planning is deterministic: rotation by staleness, yield-EMA ranking, zero-yield demotion, a small exploration cell.

## E. Quota ledger & budget planner
1. Ledger rows per provider/window: `reserved → billed | not_billed | ambiguous (counts as spent) | adjustment`. A reservation is taken in a row-locked transaction **before every attempt**; each retry takes a new reservation; `ON CONFLICT` on a request key distinguishes *new*, *existing billed with payload (replay)*, and *existing reserved/unknown (→ ambiguous, counted, surfaced)*.
2. Windows: provider-reported reset where available (JSearch `x-ratelimit-requests-reset`; SerpApi `account.json`); when stale, extrapolate (JSearch ≈ 30.96 d) and permit **one budgeted probe** to revalidate (fixes C's deadlock).
3. Reconciliation: provider-reported balances may only **tighten** local availability; nothing is refunded without positive evidence of non-billing.
4. Pacing: elapsed-fraction allowance with **bounded carry** (≤ 2× fair share per run) and a bounded **initial credit** (so fallback is usable right after a renewal); 10 % reserve for retries/fallback, released in the window's final 48 h; SerpApi hourly cap enforced below 50; Adzuna's four windows enforced.
5. Settings dry-run: projected paid units per run, query refresh cadence, and warnings when the schedule/query set cannot be served.

## F. Normalisation, dedup, availability
1. Lineage: `raw_item (response_id, item_index, disposition)` → `posting (source_instance, native_id)` **unique** → `job` (canonical opportunity) → `job_revision` (immutable; content hash of normalised description + key fields) ; `ad_keys` hold strong identities (htidocid; publisher IDs parsed from the **primary** apply link only — StepStone, LinkedIn, Indeed, XING, Join…; Adzuna id; Arbeitnow slug; ATS requisition ids).
2. Strong-key match ⇒ merge into one job with a `merge_event` (evidence, reversible). **Conflicting strong identities** (e.g., two different ATS requisition ids) are never merged ⇒ review.
3. URL canonicalisation with per-host rules (strip `utm_*`; Adzuna-specific `se`/`v` only on Adzuna hosts).
4. **[RULING] Fuzzy matches form "probable-duplicate groups", not merges, in v1** (F's compromise between O's guarded auto-merge and C's suggestions-only). Candidate = same normalised company key (legal forms, gender tags stripped), compatible location, **identical seniority-token set**, title trigram ≥ 0.85, plus text confirmation (shingle containment; a partial snippet is checked by containment in the full text). A group renders as **one card with a visible "possible duplicates (n)" badge**, is **scored once** via its most complete member (addresses O's 4× Adzuna cost concern), and can be confirmed (→ merge) or split by the user; those decisions become labelled pairs for calibrating a future auto-merge. Two distinct native IDs from the **same ATS board** are never grouped or merged; Adzuna same-source re-ingests may be grouped.
5. Availability is an orthogonal state machine: `active → stale` (aggregators: not seen for a TTL measured from `last_seen_at`) and `active → closed` **only** from complete ATS snapshots (absent in 2 complete listings ≥ 24 h apart, suspended on mass disappearance); `unknown`; source failures never age anything. "Too old to show" is a filter (max ad age), not a lifecycle state.

## G. LangGraph graphs (deterministic vs agentic) & admission
1. **`collect_graph` — deterministic, no LLM:** `plan` → `fetch` (`Send` per RequestSpec, `max_concurrency` 4, JSearch 1) → `parse_normalize` → `resolve_identity` → `embed` (embedder service) → `hard_filters` → `plan_fallback` (loops unserved JSearch specs to SerpApi) → `enqueue_targets` → `finalize` (invariants).
2. **`score_graph` — agentic:** claim admitted targets with `FOR UPDATE SKIP LOCKED` → per-target subgraph via `Send` (`max_concurrency` 3): `generate` (LLM) → `validate` (deterministic V1–V6) → `critic` (LLM, conditional) → `revise` (LLM, ≤ 2) → `publish` (CAS) | `contested` | `failed(reason)`.
3. **`profile_graph`:** parse CV → strip contacts → extract profile (LLM) → validate → **draft**. Activation is a **plain API action** (no held `interrupt`) that atomically sets the active `profile_version` and enqueues re-score targets. `propose_queries` (LLM) → user edits in Settings.
4. State holds **IDs and counters only**; artifacts (drafts, critiques, evidence) live in DB tables (so checkpoints hold no PII); subgraph `checkpointer=None` (inherit; `False` would disable it); an outcome reducer raises on conflicting results for the same work id.
5. **[RULING] Admission = budgeted allocation with no permanent embedding rejection** (C + F, with O's bounded-spend concern addressed):
   - Hard filters act on **positive evidence only** (unknown passes).
   - Each scoring cycle has a budget (count and USD); allocation ≈ **70 % by embedding-similarity rank, 20 % oldest pending, 10 % exploration** sampled from below the cut; **always-include**: watchlist companies, title trigram ≥ 0.6 against target titles, full-text twin of a scored partial ad.
   - Unadmitted ads stay `pending`, browsable with a clearly labelled **preliminary similarity**, and have a **"Score now"** action; if they expire first they end as explicit `expired_unscored`.
   - Alerts fire on **meaningful** conditions only (not "backlog exists"): exploration-lane ads scoring above the user's relevance threshold more often than a set rate (allocation miscalibrated), or high-similarity ads expiring unscored.

## H. Scoring rubric, evaluator, versioning
1. LLM-rated criteria, each `level ∈ {0..4}` with anchored descriptions **or `null` (unknown)** plus `basis ∈ {observed, unknown, unconstrained}` and verbatim evidence quotes (ad + profile): role/title fit, skills (must-have / nice-to-have), experience & seniority fit, domain/industry fit, language requirements fit, education/certification requirements. **Deterministic** criteria: location/work-mode fit (structured fields + scope), salary fit (only when both sides are known), employment type.
2. Criteria are keyed as an **object** in the schema (each exactly once); levels are enums; limits are enforced in the validator.
3. **[RULING] No separate facts-extraction LLM call** (F + C over O): job facts (work mode, required languages, must-haves, seniority, `not_a_job_ad`) are fields of the generator output. **Caps/gates bind only to structured or deterministic data** (source fields, user rules, deterministic parsers). An LLM-derived knockout or mandatory mismatch triggers the critic and is applied only if the critic confirms; otherwise the result is `contested`.
4. Knockout codes with severity (degree, certification, security clearance, work permit, on-site-required vs remote-only scope, contract type, mandatory language).
5. Aggregation in code (weighted). Unknown criteria produce an interval `[L, U]`; **coverage** = weight share of known criteria; confidence combines coverage, description completeness and critic outcome. Displayed/ranked number: **[USER] neutral midpoint vs conservative lower bound** (members split O: midpoint, F: lower bound, C: show both and evaluate). Interim design: both are computed and stored; the Jobs page offers both sorts; the default is the user's choice.
6. Validator V1–V6: schema/ranges; every non-null rating and every asserted mismatch cites evidence; quotes must be substrings of the ad/profile after normalisation; cross-field consistency; prompt-injection detector (instructions in ad text) ⇒ critic; `not_a_job_ad`.
7. Critic triggers: validator issues; score within ±8 of the user's relevance threshold; score ≥ 75; partial description with score ≥ 60; any LLM-derived knockout; injection suspicion; 10 % random sample. Critic returns `accept | revise(issues)`; ≤ 2 revisions.
8. **[RULING] Unresolved ⇒ published as `contested`, never withheld** (O + F over C): both drafts and critic notes are kept, confidence is lowered, a badge is shown and contested ads can be filtered. A score is **never** accepted by numerical convergence alone (adopts C's objection).
9. Versioning: `score_target` unique on `(job_revision_id, profile_version_id, scope_hash, rubric_version, prompt_schema_hash, model_policy_hash)` (`scope_hash` covers only behaviour-relevant settings); `score_attempt (target_id, attempt_no)`; ≤ 1 published (accepted or contested) result per target; a current-score **view** joins the active tuple (never "newest by timestamp"); history retained per retention policy.
10. Calibration: a golden set of 40–50 labelled ads (week-1), grown from "wrong score" feedback; canary ads re-scored on any config change; drift alerts.
11. Message Batches only for bulk re-scores (≥ 200 targets, e.g., after a CV change), `custom_id` = target id; incremental scoring is online.

## I. CV ingestion
PDF/DOCX upload (size + magic-byte checks) → text via pypdf/pdfplumber or python-docx → **contact stripping** (email, phone, address, URLs, date of birth, header name) before any LLM or embedding call → LLM extracts a structured profile (skills with level/years, roles, seniority, languages with CEFR, education, certifications, domains, target titles) → validator → **draft for user review/edit** → confirm creates an immutable `profile_version`. Native-PDF upload to Claude only on explicit opt-in (for scans), with disclosure. No inference of protected attributes.

## J. Settings
Immutable `settings_versions` + active pointer; server-side validation. Fields and defaults: schedule interval 1 h–7 d (**default 6 h**), timezone; locations as city + radius (required input; default radius 30 km) + "remote within Germany" toggle; work modes (default all); employment types (default all); seniority (default: profile ±1 level); industries include/exclude (default none); accepted ad languages (default de + en); max ad age (default 30 d); excluded companies/keywords; watchlist; query set; relevance threshold (default 60; also drives the critic boundary trigger); per-node model + effort; monthly LLM USD cap; paid-source budget preview.

## K. Jobs page
Filters: posted within (24 h / 3 d / 7 d / 30 d / custom), score range, minimum coverage/confidence, location + radius, work mode, title/keyword (German FTS + trigram), company, source, ad language, employment type, seniority, user status, availability, contested, unscored/pending. Sorts: score (default; variant per H5), posted date, first seen. Keyset pagination with a "ranking changed" banner. Independent user-action timestamps (`seen_at`, `saved_at`, `applied_at`, `hidden_at`). Detail view: sanitised description (nh3), per-criterion breakdown with evidence, interval + coverage, critic notes / contested drafts, provenance (sources, first/last seen, `posted_at_effective` with precision), duplicate group, outbound link, "wrong score" feedback. Header strip: last/next run, quota usage, alerts.

## L. Security
Argon2id; the single user is created via CLI (no default password); server-side sessions in Postgres with a `__Host-` cookie (Secure, HttpOnly, SameSite=Strict); CSRF via Origin check + custom header; DB-backed login throttling + lockout alert; optional TOTP; strict CSP, HSTS, `frame-ancestors 'none'`; upload limits; nh3 sanitisation; secrets only via env; no secrets or PII in logs.

## M. Errors & observability
Every raw item has a disposition; every score target ends in a named state with a reason; run invariants (dispositions reconcile; spend ≤ allowance — never `=`; no target `in_progress` > 15 min without a live run) ⇒ `failed_invariant`; typed `SourceError`/`LLMError` taxonomies; `ambiguous` paid attempts surfaced. Structured JSON logs without PII; private metrics; DB-backed run reports in the UI; `/healthz` and `/readyz` (DB, worker heartbeat, embedder, model checksum, last successful run age, last backup age — **not** vector coverage). Alerts in-app + an external channel **[USER]** + an external dead-man-switch ping per successful run. LangSmith **[USER]**: off by default; if on, inputs/outputs hidden and EU endpoint.

## N. Testing
Unit + property tests (normalisers, dedup, quota/pacing); Postgres integration tests (constraints, CAS, SKIP LOCKED, concurrency); crash-injection matrix (before/after dispatch, before/after persist); contract tests on scrubbed recorded fixtures; payload tests for every LLM call shape; LLM eval on the golden set (rank correlation, false-exclusion rate, critic agreement); E2E with playwright-cli; GitHub Actions CI (lint, type-check, tests; no live paid calls).

## O. Deployment & retention
VPS 4 vCPU / 16 GB / ≥ 100 GB NVMe, EU region; images by digest; Caddy auto-TLS; nightly encrypted **offsite** backups (restic to S3-compatible storage) **[USER: target]** + monthly restore test; deletion manifest replayed after restore. Retention: raw payloads 30 d; checkpoints 14 d after run end; logs 14 d; closed/stale ads 90 d then tombstone (hashed dedup keys kept); LLM call records = references + outputs + prompt hash + usage (no request bodies); only active + previous profile versions kept; full export; "delete everything"; Adzuna-derived data purge; PRIVACY.md.

## P. Cost envelope (≈ 3,000 ads/month reaching the LLM; addendum cache prices)
A all Opus (medium) ≈ $120–300/month · B Sonnet generator + Opus critic ≈ $60–135 · C Haiku generator + Sonnet critic ≈ $15–45. Spend ledger enforces a hard monthly cap; week 1 measures real token usage. **[USER]** chooses the configuration and cap; default `claude-opus-5-5` until chosen.

## Q. Escalated to the user
1. Model configuration + monthly USD cap.
2. Default ranking for ads with missing information: neutral midpoint vs conservative lower bound.
3. External alert channel and dead-man-switch service.
4. LangSmith tracing on/off.
5. Willingness to label ~40–50 ads in week 1 for calibration.
6. (Deployment-time) domain, VPS provider, offsite backup target.
