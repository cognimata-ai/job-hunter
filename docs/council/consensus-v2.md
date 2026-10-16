# Consensus draft v2 — after round-3 votes (2026-10-16)

All three members voted **APPROVE WITH CHANGES** on v1, with no ruling contested. v2 applies every objection and MISSING item. Where two members' fixes conflicted, the chair merged them (**[MERGED]**). Changes from v1 are tagged **(v2: source)**: C = Codex, O = Opus, F = Fable. **[USER]** = escalated to the user; it must stay configurable until answered.

---

## A. Stack & LLM gateway
1. Python 3.13; FastAPI 0.143.x, Pydantic v2, SQLAlchemy 2.1 + Alembic, psycopg 3.3.
   - LangGraph 1.2.15 **as a library only**; langchain-anthropic 1.7.6 + the `anthropic` SDK.
   - Exact pins via a uv lockfile.
   - Frontend: React 19 + Vite 8 + TypeScript + TanStack Query/Router, built to static assets and served same-origin by the API.
2. **`LLMGateway`** is the only path to Claude. On every call it:
   - passes `base_url="https://api.anthropic.com"` and the key from `JOBHUNTER_ANTHROPIC_API_KEY` explicitly;
   - uses `with_structured_output(method="json_schema", include_raw=True)` and **streams** (final-message helper);
   - sets an **explicit httpx timeout** of connect 10 s and read-idle 120 s (the SSE stream carries pings), plus a LangGraph `TimeoutPolicy` deadline on every LLM node. The deadline defaults to 600 s for the generator and critic, and the spike validates it (v2: O, C);
   - sets `max_tokens` per node, sized for thinking plus output;
   - requires an **explicit model and effort per node**; a config without effort is rejected;
   - never sends forced `tool_choice`, prefill or `temperature`, enforced by a lint rule and payload contract tests;
   - branches on `stop_reason`:
     - `refusal` → `refused`, with its category;
     - `max_tokens` → one retry with a larger cap, then `failed_max_tokens`;
   - records `model_served` and usage.
3. Server-side refusal **fallbacks are off** by default.
4. **Retries and spend accounting:**
   - The SDK uses `max_retries=2` for 429/5xx/connection errors and honours `Retry-After`.
   - LangGraph `RetryPolicy` uses an explicit `retry_on` that excludes all 4xx and our validation errors.
   - The LLM spend ledger reserves the worst case before each call, priced per model (input, output and cache-write prices; v2: C).
   - **Settlement:** an attempt with known `usage` settles to actual cost. An attempt whose response was lost or timed out keeps its maximum charge as `ambiguous`.
   - Every outer (LangGraph) retry and every enlarged-`max_tokens` retry takes a **fresh reservation** (v2: C).
5. **Prompt caching:**
   - An explicit breakpoint sits at the end of the shared prefix (rubric + confirmed profile), with a 5-minute TTL. Role-specific instructions go after the breakpoint.
   - Cache entries are per **(model, effort)**. Cache-hit monitoring and its "consecutive zero reads" alert are keyed per **(model, effort, role)** (v2: F).
6. **Schema dry-run test:** each structured-output schema is sent once to the API (cheap) to confirm its JSON-schema subset is accepted and not silently stripped. Ranges and lengths are enforced in the Pydantic validator regardless.
7. **Message Batches** use a separate raw-`anthropic`-SDK branch inside the gateway (LangChain can't do Batches) with a durable lifecycle (v2: C):
   - A `batch_submission` row is written **before** submit: request hash, state `preparing → submitted | submit_ambiguous → ended`, provider batch id, `expires_at`.
   - `batch_item` rows are keyed by `custom_id = "<target_id>:<attempt_no>"`, with per-item outcome `succeeded | errored | canceled | expired`.
   - An ambiguous submit is resolved by listing batches and matching metadata, never by blind resubmission.
   - Every item is reconciled. Results publish against the original target/attempt with the same CAS rules as online scoring.

## B. Topology & scheduling
1. Compose services (**no Redis**):
   - `caddy` (TLS/HSTS);
   - `api` (FastAPI + SPA);
   - `worker`: a **single replica** running procrastinate and the LangGraph graphs;
   - `embedder`: onnxruntime with `snowflake-arctic-embed-l-v2.0` int8; the model is baked into the image at a pinned revision and its sha256 verified at start;
   - `postgres` (`pgvector/pgvector:0.8.7-pg17`);
   - `migrate`: one-shot Alembic + procrastinate schema + `PostgresSaver.setup()` on its **own autocommit connection** (v2: F);
   - `backup`.
2. **Checkpointer connection contract (v2: F):** the worker gives `PostgresSaver` a dedicated psycopg pool, separate from SQLAlchemy's transactional pool, with `autocommit=True`, `prepare_threshold=0` and `row_factory=dict_row`.
3. **[RULING] No separate scheduler, lease service or outbox.** Concurrency safety comes from the following **[MERGED]** measures (v2: C, O):
   - **Worker singleton fence:** the worker holds a Postgres **session-level advisory lock** for its whole lifetime, and only the lock holder runs graphs or resumes runs. A zombie process that still has a DB session keeps the lock, so a new worker cannot start resuming. This fences checkpoint writes as well as domain writes.
   - **In-process stale tasks:** within one process, a stale task is cancelled and awaited before any resume.
   - **Takeover:** `attempt_no` is incremented atomically on takeover. Every domain write and paid-dispatch admission of a run checks `attempt_no` (CAS) as defence in depth.
   - **Transactional admission** where procrastinate can defer on the same connection (to be verified in a spike). Otherwise a **reconciler** re-defers or fails any `queued` run that has had no live job for 2 min.
   - **Stalled procrastinate jobs** are released at worker start and on every tick. A resume always runs as a **locked task**.
4. **Schedule:**
   - A 1-minute procrastinate tick computes due slots `anchor + k·interval`. The anchor is stored as a **UTC instant** and the interval is elapsed time, so DST never shifts or doubles a slot (v2: F).
   - Rows are unique on `(kind, scheduled_slot)`.
   - After downtime only the latest missed slot runs; the others are recorded `missed`.
   - A slot that falls during an active run is recorded `skipped_overlap`. The unique violation is handled as an outcome.
5. **One active collection:** a unique index over `((true)) WHERE kind='collect' AND status IN ('queued','running')`.
6. **Scoring is a detached lane** of score targets with its own claim lease (G2), not a `runs` row.
7. **Heartbeats:**
   - Run heartbeat every 30 s; nodes that can exceed 60 s heartbeat internally.
   - A heartbeat stale for more than 3 min triggers a resume from the checkpoint (`durability="sync"`).
   - After 3 resumes the run is marked `failed` and an alert is raised.
8. **"Run now"** is idempotent. Paid sources are used only if the user ticks "include paid sources" (the budget impact is shown).
9. **Graceful shutdown:** stop admission, finish bounded in-flight calls, checkpoint, exit.
10. A periodic cleanup deletes finished procrastinate jobs older than 7 days, and a table-size metric is recorded (v2: O).

## C. Storage
1. Postgres 17 + pgvector 0.8.7, `pg_trgm`, `unaccent` behind an IMMUTABLE wrapper, and a `german_unaccent` text-search config. Trigram search complements German FTS for compound words.
2. Embeddings are stored as `halfvec(1024)`, keyed by `(embedding_model_id, text_sha256)`. **Exact scan** until there are more than 50k vectors; HNSW only once measured.
3. Raw provider payloads are stored compressed for 30 days. A billed paid response is **replayed from storage** on resume.

## D. Sources & query planning
1. **Adapter protocol:**
   - `capabilities()`
   - `plan(query, window) → RequestSpec[]`
   - `fetch(spec) → RawPage`: one HTTP attempt; the caller holds the reservation.
   - `parse(page) → ItemOutcome[]`: every raw item gets a disposition.
   - `continuation(page) → Cursor | END | UNKNOWN`
2. **Coverage accounting (v2: C, O):**
   - Each query/source tracks `last_attempted_at` separately from **verified coverage intervals**.
   - A capped traversal or an `UNKNOWN` continuation records `coverage_incomplete`, keeps the uncovered interval as a gap cursor, and shows the gap in the run report and the UI.
   - `last_covered_at` only advances across fully covered ranges.
3. **JSearch** (primary):
   - Endpoint `/search-v2`. The location goes in the query text.
   - `country=de`.
   - `date_posted` is the smallest bucket ≥ `now − last_covered_at + 24 h`. If that exceeds `month`, use `month` and record a coverage gap (v2: O).
   - Concurrency 1.
   - Settlement:
     - **2xx = billed**;
     - RapidAPI gateway 404 with `x-rapidapi-proxy-response: true` = not billed;
     - **every other status or timeout = `ambiguous`**, counted as spent (v2: F).
   - Identity is `job_uid` (Google `htidocid`).
4. **SerpApi Google Jobs** (fallback):
   - Used only when JSearch is provider-exhausted, circuit-open, auth-failed or contract-broken. Never because of local pacing.
   - The location goes in `q`; `gl=de`; `hl=en` is **pending one recorded verification call**. That call must also confirm the `htidocid` is the same under `hl=en` as under `hl=de`/JSearch; if not, keep `hl=de` (v2: F).
   - Parsers accept **both English and German** relative dates and extension strings either way, e.g. "vor 4 Tagen", "Homeoffice" (v2: O).
   - Empty results count as billed.
   - Status settlement is the same as JSearch: 2xx billed, everything else ambiguous.
5. **Adzuna** (personal use):
   - `max_days_old` comes from the coverage window; 50 results per page.
   - All four rate windows are enforced: 25/min, 250/day, 1,000/week, 2,500/month.
   - Descriptions are `partial` (500 chars).
   - `redirect_url` is stored privately as the outbound link and never used as identity. Adzuna detail pages are never fetched.
6. **Arbeitnow** (v2: O):
   - Page cap = `min(10, max(3, ceil(interval_h / 30) + 1))`, within its 50-request limit.
   - The `created_at` watermark advances only across contiguous, fully read pages. A truncated traversal keeps a gap cursor for the next run.
   - Local filter to Germany; never used to close ads.
7. **Watchlist ATS:**
   - Supported: Greenhouse, Lever (global **and** EU host), Ashby, Personio XML, Recruitee, SmartRecruiters. SmartRecruiters detail calls are made for new or changed postings, plus a periodic refresh.
   - Workable is deferred.
   - The user enters company + ATS + slug, or a careers URL that is **parsed by pattern only**, so there is no SSRF surface.
8. **Query planning:**
   - An LLM proposes a query set from the confirmed profile and scope, and the user reviews it in Settings.
   - Runtime planning is deterministic: rotation by staleness, yield-EMA ranking, zero-yield demotion, and an exploration cell.

## E. Quota ledger & budget planner
1. **Ledger entries** move `reserved → billed | not_billed | ambiguous (counted as spent) | adjustment`.
   - A reservation is taken in a row-locked transaction before **every** attempt; retries take new reservations.
   - Request-key conflicts resolve three ways:
     - new → reserve;
     - existing and billed with a stored payload → replay;
     - existing, reserved or unknown → `ambiguous`, counted and surfaced.
2. **Windows:** use the provider-reported reset (JSearch header, SerpApi `account.json`). When stale, extrapolate (JSearch ≈ 30.96 d) and allow one budgeted probe.
3. **Reconciliation** only ever tightens: no refund without positive evidence that a call wasn't billed.
4. **Pacing:**
   - Allowance is an elapsed fraction with **fractional accrual** between ticks.
   - Carry capacity is `max(1, 2 × fair_share)` billable requests, so hourly schedules don't starve (v2: C).
   - A bounded initial credit is available after each renewal.
   - A 10% reserve for retries and fallback is released in the final 48 h.
   - SerpApi's hourly cap is enforced below 50, and Adzuna's four windows are enforced.
5. **Settings dry-run:** projected units per run, query refresh cadence, the stale-TTL check (F5) and coverage warnings.

## F. Normalisation, dedup, availability
1. **Lineage:** `raw_item(response_id, item_index, disposition)` → `posting(source_instance, native_id)` (unique) → `job` → immutable `job_revision`.
   - `ad_keys` hold the strong identities:
     - `htidocid`;
     - publisher ids parsed from the **primary** apply link only;
     - Adzuna id;
     - Arbeitnow slug;
     - **ATS identity = `(ats_type, board, posting_id)`**. A Greenhouse `requisition_id` is only a probable-duplicate signal, because one requisition can be posted in several offices (v2: F).
2. **Merging:**
   - A strong-key match merges into one job, with a `merge_event` (evidence, reversible).
   - Conflicting strong identities are never merged and go to review.
   - Merge reversals and group splits re-enqueue targets (F4, G8).
3. **URL canonicalisation** follows per-host rules.
4. **[RULING] Fuzzy matches form probable-duplicate groups, not merges, in v1.**
   - **Candidate criteria:**
     - same normalised company key;
     - compatible location;
     - identical seniority-token set;
     - title trigram ≥ 0.85;
     - text confirmation (shingle containment);
     - **within a 45-day window** (v2: O).
   - **Never grouped:** two distinct postings from the same ATS board. Adzuna same-source re-ingests may be grouped.
   - **Scoring (v2: F, C, O [MERGED]):**
     - Every member revision gets its **own score target**. Non-representative members are set to `deferred_duplicate(group_id)`, which costs no LLM spend.
     - The representative (the most complete member) is scored, and its result belongs to **that revision only**.
     - The card shows "scored via <member>, applies to n members".
     - A split, or conflicting scoring-relevant facts between members, moves deferred members to `pending`.
   - **Display:**
     - Non-score filters are evaluated across all members.
     - Group availability = **any member active**.
     - User actions are stored per job; an action on the group card applies to all members.
5. **Availability** is an orthogonal state machine:
   - `active → stale` for aggregators, by TTL from `last_seen_at`. The TTL must be ≥ **2× the longest per-query refresh cadence**; the dry-run warns otherwise (v2: O).
   - `active → closed` only from **complete** ATS snapshots: absent in 2 listings ≥ 24 h apart, suspended on mass disappearance.
   - `unknown`.
   - Failures never age anything.
6. **Canonical field selection (v2: C):**
   - Precedence is deterministic per field, and each field records its provenance (source, observed_at).
   - **Descriptions:** full beats partial; ATS > JSearch/SerpApi > Arbeitnow > Adzuna. A partial description never overwrites a full one.
   - **Salary** is stored as `{min, max, currency, period, kind: stated | predicted}`. Predicted salary is never presented as stated.
   - **Posted date:** `posted_at_effective` with a precision flag.
   - A new material revision is created only when the canonical scoring-relevant content changes.

## G. LangGraph graphs & admission
1. **`collect_graph` (deterministic, no LLM; v2: F order):**
   1. `plan`
   2. `fetch_primary` (`Send` per spec; `max_concurrency` 4; JSearch 1)
   3. `plan_fallback`
   4. `fetch_fallback`
   5. `parse_normalize`
   6. `resolve_identity`
   7. `embed`
   8. `hard_filters`
   9. `enqueue_targets`
   10. `finalize`
   - **Embedder unavailable (v2: F):** the revision is marked `awaiting_embedding`. Targets are still created with `similarity = null`, which makes them eligible for the oldest-pending and exploration lanes and for always-include. The run ends `partial` with the count, and embedding is retried next run.
2. **`score_graph` (agentic):**
   - **Atomic claim (v2: C, O):** one `UPDATE … WHERE id IN (SELECT … FOR UPDATE SKIP LOCKED) RETURNING` sets `state='in_progress'`, `claim_id`, `attempt_no+1` and `claim_heartbeat_at`.
   - Nodes refresh the claim heartbeat. A target is reclaimed only when its heartbeat is older than 3 min, with bounded attempts.
   - `publish` checks `claim_id` and `attempt_no` (CAS).
   - **Per-target subgraph**, run via `Send` with `max_concurrency` 3:
     - `generate` (LLM) → `validate`
     - `critic` (LLM, conditional) → `revise` (LLM, ≤ 2) → `validate` (**every revision is validated**; v2: C)
     - → `publish` | `contested` | `failed(reason)`
3. **`profile_graph`:** parse → strip contacts → extract profile (LLM) → validate → draft.
   - Activation is a plain API action. It atomically sets the active `profile_version` and enqueues targets.
   - `propose_queries` (LLM) feeds the Settings review.
4. **State and checkpoints:**
   - State holds IDs and counters only; artifacts live in the DB.
   - The subgraph checkpointer is `None` (inherits the parent's).
   - An outcome reducer raises on conflicting results for the same work id.
5. **[RULING] Admission = budgeted allocation with no permanent rejection.**
   - Hard filters act on positive evidence only.
   - Each cycle has a count + USD budget, split ≈ 70% similarity rank, 20% oldest pending, 10% exploration, plus always-include (watchlist, title trigram ≥ 0.6, full-text twin).
   - Unadmitted ads stay `pending`, are browsable with a preliminary similarity, and have a "Score now" button.
6. **Expiry for admission (v2: O):** a pending target is expired when its availability is not `active` **or** `posted_at_effective` is older than max ad age. Admission skips expired targets, and they end as `expired_unscored`. The oldest-pending lane therefore only spends on visible, live ads.
7. **Alerts** fire only on meaningful conditions:
   - the exploration lane scoring above the relevance threshold more often than a set rate;
   - high-similarity ads expiring unscored.
8. **Re-score triggers (v2: C):** target creation is idempotent.
   - It is triggered by every change of the active LLM tuple (profile version, rubric version, prompt/schema hash, model policy) and by every new material job revision.
   - Scope changes recompute only deterministic results (H9), with no LLM call.
   - A reconciler creates any missing targets for the active tuple.

## H. Scoring rubric, evaluator, versioning
1. **LLM-rated criteria.** Each has a level 0–4 with anchored descriptions, or `null`, plus `basis ∈ {observed, unknown, unconstrained}` and verbatim evidence.
   - `unconstrained` means the ad demonstrably imposes no requirement on that criterion. It **requires evidence** (a quote or a structured field), scores as level 4, and counts as known for coverage. Without evidence it is `unknown` (v2: O).
   - **Initial rubric v1 weights** (v2: F):

     | Criterion | Weight |
     |---|---|
     | Role/title fit | 20 |
     | Skills (must/nice) | 25 |
     | Experience & seniority | 15 |
     | Domain/industry experience | 10 |
     | Language requirements | 10 |
     | Education/certifications | 5 |
     | Location & work mode *(deterministic)* | 10 |
     | Employment type & salary *(deterministic, when known)* | 5 |

     The weights are versioned and calibrated on the golden set.
2. **Schema:** criteria are object-keyed and levels are enums. Limits are enforced in the validator.
3. **[RULING] No separate facts LLM call.** The generator outputs CV-independent job facts with evidence: work mode, required languages and level, must-haves, seniority, industry, `not_a_job_ad`.
   - **Caps** bind only to structured source fields or **critic-confirmed** facts.
   - Any asserted hard fact (on-site-only, mandatory language, clearance, work permit, degree, certification) triggers the critic, independently of scope. Unconfirmed facts → `contested`.
4. **Knockout codes with severity:** degree, certification, clearance, work permit, on-site-required, contract type, mandatory language.
5. **Aggregation** is in code:
   - Unknown criteria give an interval `[L, U]`.
   - **Coverage** = the weight share of known criteria.
   - Confidence = f(coverage, description completeness, critic outcome).
   - Ranking default **[USER]**: neutral midpoint vs conservative lower bound. Both are stored and both sorts are offered.
6. **Validator:** schema and ranges; evidence for every non-null rating and every asserted fact or mismatch; quotes must be substrings of the source; consistency checks; injection detector → critic; `not_a_job_ad`.
7. **Critic:**
   - **Triggers:**
     - validator issues;
     - **the interval `[L, U]` straddles the relevance threshold, or the midpoint is within ±8 of it** (v2: O);
     - midpoint ≥ 75;
     - a partial description with midpoint ≥ 60;
     - any asserted hard fact;
     - injection suspicion;
     - a 10% sample.
   - Verdict is `accept | revise(issues)`; at most 2 revisions, each re-validated.
8. **[RULING] Unresolved results publish as `contested`, never withheld (v2: C hardening):**
   - A contested result must be schema-valid. Ratings whose deterministic evidence failure is still unresolved are replaced by `null`, and the interval is recomputed.
   - Rejected drafts and critic notes are kept separately.
   - Never accepted by numerical convergence.
9. **Versioning (v2: F, O [MERGED]):**
   - The generator receives **profile + ad (+ rubric) only**, no scope.
   - `llm_result` is keyed on `(job_revision_id, profile_version_id, rubric_version, prompt_schema_hash, model_policy_hash)`, with `score_attempt(target_id, attempt_no)` and ≤ 1 published result per target.
   - `det_result` is keyed on `(llm_result_id, scope_version_id)`. It holds the deterministic criteria (location/work mode, salary, employment type, industry preference), caps from confirmed facts plus scope, `[L, U]`, coverage and the final number. It is recomputed **without an LLM call** whenever scope changes.
   - The current-score **view** joins the active LLM tuple with the active scope, never "newest by timestamp".
10. **Calibration:** a 40–50-ad golden set **[USER: week-1 labelling]**, plus "wrong score" feedback, canaries and drift alerts.
11. **Batches** are used only for bulk re-scores of ≥ 200 targets (A7).

## I. CV ingestion
1. Upload PDF or DOCX, with size and magic-byte checks.
2. Extract text with pypdf/pdfplumber or python-docx.
3. **Strip contacts** before any LLM or embedding call.
4. The LLM extracts a structured profile.
5. Validate.
6. The **user reviews and edits the draft**; confirming creates an immutable `profile_version`.

Native-PDF upload to Claude only on explicit opt-in, with disclosure. No inference of protected attributes.

## J. Settings
Settings are immutable `settings_versions` with an active pointer, validated server-side.

| Setting | Default / notes |
|---|---|
| Interval | 1 h–7 d, **default 6 h** |
| Timezone | Display only |
| Locations | City + radius (default 30 km), plus a "remote within Germany" toggle |
| Work modes | All |
| Employment types | All |
| Seniority | Profile ±1 |
| Industries | Include/exclude lists |
| Accepted **ad languages** | de + en |
| **Working-language requirements policy** (v2: C) | See below |
| Max ad age | 30 d |
| Excluded companies / keywords | — |
| Watchlist | — |
| Query set | Proposed by the LLM, reviewed by the user |
| Relevance threshold | 60 |
| Per-node model + effort | — |
| Monthly LLM USD cap | — |
| Paid-budget preview | — |

**Working-language policy:**
- The candidate's languages and CEFR levels come from the confirmed profile and can be edited.
- A per-language policy decides what an unmet mandatory language requirement does: `hard` (cap) or `soft` (penalty).
- **The language an ad is written in never implies its working-language requirements or the candidate's proficiency.**

## K. Jobs page
**Filters:**
- posted within;
- score range;
- minimum coverage/confidence;
- location + radius;
- work mode;
- title/keyword (FTS + trigram);
- company;
- source;
- ad language;
- employment type;
- seniority;
- user status;
- availability;
- contested;
- unscored/pending.

**Sorts:** score (variant per H5), posted date, first seen. Pagination is keyset, with a "ranking changed" banner.

**User actions:** independent timestamps (seen, saved, applied, hidden).

**Detail view:**
- sanitised description;
- per-criterion evidence;
- interval and coverage;
- critic notes / contested drafts;
- provenance;
- duplicate group (which member was scored);
- outbound link;
- "wrong score" feedback.

**Header strip:** last/next run, quota, coverage gaps, alerts.

## L. Security
- **Accounts and sessions:**
  - Argon2id; the user is created via CLI.
  - Server-side sessions with a `__Host-` cookie (Secure, HttpOnly, SameSite=Strict), **12 h idle / 30 d absolute lifetime** (v2: F).
- **Request protection:**
  - CSRF: Origin check + custom header.
  - DB-backed login throttling with a lockout alert; optional TOTP.
  - CSP, HSTS, `frame-ancestors 'none'`.
- **Data handling:**
  - Upload limits; nh3 sanitisation.
  - Secrets only via env; no secrets or PII in logs.

## M. Errors & observability
1. **Invariants:**
   - Every raw item has a disposition.
   - Every target ends in a named state with a reason.
   - Invariants: dispositions reconcile; spend ≤ allowance; **no target `in_progress` with a claim heartbeat older than 3 min** (v2: C, O).
   - Any violation sets `failed_invariant`.
   - Typed error taxonomies; `ambiguous` paid attempts are surfaced.
2. **Logs, metrics, reports:** structured logs without PII, private metrics, DB-backed run reports.
3. **Health endpoints (v2: O):**
   - `/healthz` is liveness.
   - `/readyz` covers serving prerequisites only: DB, migrations at head, embedder, model checksum, worker heartbeat.
   - **Last-run age and last-backup age** live on `/api/system/health`. They raise warn/critical alerts, with a first-run grace period; they are not readiness gates.
4. **Alerts:**
   - In-app, plus an external channel **[USER]**.
   - An external dead-man-switch ping on each successful run. The expected period follows the active interval: it is updated when the interval changes (v2: F).
5. **LangSmith [USER]:** off by default. If on: hidden inputs/outputs and the EU endpoint.

## N. Testing
- **Unit and property tests** for normalisers, dedup and quota/pacing (including hourly-schedule starvation).
- **Postgres integration tests:** constraints, CAS, SKIP LOCKED, concurrency, advisory-lock fence.
- **Crash-injection matrix:**
  - before/after dispatch;
  - before/after persist;
  - claim-heartbeat reclaim;
  - stalled stream;
  - batch submit-ambiguous (v2: O, C).
- **Contract tests** on scrubbed fixtures.
- **Payload tests** plus a schema dry-run.
- **LLM eval** on the golden set.
- **E2E** via playwright-cli.
- **CI:** GitHub Actions, with no live paid calls.

## O. Deployment & retention
1. **Deployment:**
   - VPS 4 vCPU / 16 GB / ≥ 100 GB NVMe in an EU region; images by digest; Caddy auto-TLS.
   - Nightly encrypted **offsite** backups (restic → S3-compatible) **[USER: target]**, with a monthly restore test.
   - The deletion manifest is replayed after any restore.
2. **Retention periods:**
   - raw payloads 30 d;
   - checkpoints 14 d after run end;
   - logs 14 d;
   - closed/stale ads 90 d, then tombstoned (hashed keys kept);
   - LLM call records hold references + outputs + prompt hash + usage, no bodies.
3. **Profile retention (v2: F, C, O [MERGED]):**
   - The **CV file and raw extracted text** are deleted for every version older than the previous one.
   - The **contact-stripped profile JSON** of a version is kept while retained scores, feedback or golden-set cases reference it.
   - Score history is kept for 12 months (configurable). Then a **dependency-aware purge** runs in one transaction: it cancels pending work, prevents late publication, deletes dependent targets, results, evidence and call records, then the profile JSON once it is unreferenced. Only non-content audit metadata remains.
   - The same dependency handling applies when job-revision inputs expire.
4. **User data controls:** full export, "delete everything", Adzuna-derived data purge, PRIVACY.md.

## P. Cost envelope (≈ 3,000 ads/month reaching the LLM; addendum cache prices; facts call removed)
| Config | Models | Per month |
|---|---|---|
| A | All Opus (medium) | ≈ $120–230 |
| B | Sonnet generator + Opus critic | ≈ $60–125 |
| C | Haiku generator + Sonnet critic | ≈ $15–45 |

- A hard monthly cap is enforced by the spend ledger.
- Week 1 measures real token usage.
- **[USER]** chooses the configuration and cap; the default is `claude-opus-5-5` until then.

## Q. Escalated to the user
1. Model configuration + monthly USD cap.
2. Default ranking for ads with missing information: neutral midpoint vs conservative lower bound.
3. External alert channel and dead-man-switch service.
4. LangSmith tracing on/off.
5. Willingness to label about 40–50 ads in week 1.
6. At deployment time: domain, VPS provider, offsite backup target.
