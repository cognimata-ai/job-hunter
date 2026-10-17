# M1 Collection Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the deterministic collection pipeline on the M0 foundation: the M1 schema, the normalisers and the gazetteer, the source adapters (JSearch, SerpApi, Adzuna, Arbeitnow, six ATS boards) running on scrubbed fixtures with live mode behind config, the quota ledger with windows, pacing, fallback and dry-run projections, the acquisition receipts, coverage and gaps, identity resolution with merges, unmerges and probable-duplicate groups, availability and ATS snapshots, the embedder service, and `collect_graph` end to end with run invariants and reports, executed by the real worker.

**Architecture:** Every node of `collect_graph` rebuilds its work list from durable receipts (`fetch_requests`, `raw_responses`, `raw_items`, `posting_observations`, `ats_snapshots.applied_at`) and writes through the M0 `transition()` helper under the run's ownership predicate, so a new attempt resumes without a second HTTP call or a second sighting. Paid HTTP goes through one acquisition protocol: resolve the latest attempt, reserve under the window lock, commit the dispatch marker, send once, persist and settle in one transaction. Pure decision functions (`roll_forward`, `decide`, `select_canonical`, the G1–G5 table, the availability rules) are tested without a database; the authoritative paths are integration-tested on the real PostgreSQL container, and the crash cases C1–C7 on the real worker with fixture adapters.

**Tech Stack:** The M0 stack plus `httpx` 0.28.1 and `respx` 0.23.1 (adapters and their mocks), `nh3` 0.3.7 (HTML), `defusedxml` 0.7.1 (Personio XML), `zstandard` 0.25.0 (stored bodies), pgvector `halfvec`, `pg_trgm`; a separate `embedder` image with `onnxruntime` 1.31.0, `tokenizers` 0.23.3, `numpy` 2.5.4 and the `Snowflake/snowflake-arctic-embed-l-v2.0` int8 ONNX export at a pinned revision; GeoNames (CC BY 4.0) for the gazetteer.

**Spec:** docs/superpowers/specs/2026-10-16-job-hunter-design.md (revision 3.3). M1 covers the roadmap row `docs/superpowers/plans/2026-10-16-job-hunter-roadmap.md`: §5, §6.1–6.8, §7.5, §7.7–7.10, §8, §9.1–9.2, §11.1, §17.2–17.3, §18.4 and spikes V3, V7, V8, V9, V10, V17, V18. Research facts verified live on 2026-10-16: `docs/council/research/sources.md`, `docs/council/research/tech.md`. The M0 plan's Deviations 1–28 and Carry-forward items 1–7 bind M1 where they name it.

## Global Constraints

Every Global Constraint of the M0 plan still applies (pins, `mypy --strict`, ruff banned APIs and `BLE001` allowlists, `transition()` for every state change, JSONB binds, the lock order, no PII or secret in logs, `pytest -W error`, injected clocks, Step-2/Step-4 messages indicative, Conventional Commits). M1 adds:

- New pins (§4.1), exact: `httpx==0.28.1` moves to the runtime dependencies; `nh3==0.3.7`, `defusedxml==0.7.1`, `zstandard==0.25.0` are runtime dependencies; `respx==0.23.1` is a dev dependency; the dependency group `embedder` holds `onnxruntime==1.31.0`, `tokenizers==0.23.3`, `numpy==2.5.4`, `fastapi==0.143.2`, `uvicorn==0.54.0`, `pydantic==2.14.1`. Each task adds the pins its deliverable needs; a differing resolution at `uv lock` is recorded in the commit message.
- Embedding model: `Snowflake/snowflake-arctic-embed-l-v2.0`, HF revision `ac6544c8a46e00af67e330e85a9028c66b8cfd9a`, files `onnx/model_int8.onnx` (sha256 `4b164a8bd09dd9806e035bdf3c34a2d81848b3db9642ba2e342b8367c00872d8`, 569,721,975 bytes) and `tokenizer.json` (sha256 `39feb9863a378165ab9c5c689047203d789422966c0c58721c5309fd039a8edc`), read from the HF Hub API on 2026-10-17 and re-verified by the build. `embedding_model_id = "snowflake-arctic-embed-l-v2.0@ac6544c8a46e00af67e330e85a9028c66b8cfd9a/int8"`. Vectors are `halfvec(1024)`, L2-normalised; passages ≤ 1,024 tokens, truncation flagged; batches of 16; 120 s per batch; query prefix `query: ` for queries only.
- HTTP (§5.2): connect 5 s, read 30 s (Arbeitnow and ATS full boards 60 s), write 10 s, pool 5 s, total deadline 90 s per attempt; `User-Agent` from `JOBHUNTER_HTTP_USER_AGENT`, default `JobHunter/1.0 (personal job search)`; redirects disabled except the Personio host family (`*.jobs.personio.de`); free-source retries at most 3 attempts per logical request and run attempt on connect errors, 5xx and 429, backoff 1 s then 4 s (jittered), `Retry-After` honoured up to 60 s, no retry on other 4xx; paid sources at most 1 retry per spec, only after a 429 or 5xx **response**, as a new attempt (`attempt_seq + 1`, class `retry`); stored response headers only `date`, `content-type`, `x-ratelimit-*`, `x-rapidapi-proxy-response`, `retry-after`, `age`, `cf-cache-status`, `cache-control`; a body larger than 64 MiB is read no further and is a contract failure (M1 bound).
- Breaker (§5.2): opens after 2 consecutive failed attempts for paid sources and 5 for free ones; immediately on 401/403 (`auth_failed`), a contract violation (`contract_broken`) and an ATS board 404 (`not_found`); half-open after 6 h with one probe request; failures below the threshold are `degraded`.
- Rate limits (§5.3): JSearch concurrency 1, 1 request/s; SerpApi concurrency 1, hourly cap `serpapi_hourly_cap` 45 (always < 50 and < `account_rate_limit_per_hour`); Adzuna concurrency 1, rolling caps 20/min, 225/day, 900/week, 2,250/month (ToS 25/250/1,000/2,500); Arbeitnow 1 request per 2 s, concurrency 1; every ATS host 2 concurrent requests and 5 requests/s; SmartRecruiters detail calls concurrency 4 and ≤ 5 requests/s.
- Quota (§6): JSearch limit `jsearch_monthly_limit` 200 over a nominal period of 2,675,242 s (30.96 d); SerpApi `serpapi_monthly_limit` 250 over the calendar month ending at `plan_renewal_date`; Adzuna synthetic 30-day window with `N = 0.9 × cap_month`; `reserve_pct` 10 → `reserve = ceil(reserve_frac × limit)`; `fair_share = N × interval_s / window_length_s`; `C = max(1, 2 × fair_share)`; a new window starts with `tokens = C`; the effective reserve is 0 in the final 48 h; `paid_max_requests_per_run` `auto` = `max(10, ceil(2 × fair_share))`; probe retry 12 h (`PROBE_RETRY`); `SERPAPI_USAGE_LAG` 120 s; foreign-usage tolerance 1 search, conservative mode `H − 5` for 24 h after the last detection; ledger recovery after 15 min; `attempt_seq` 1–8; `COUNTED = (reserved, dispatched, billed, ambiguous)`; nothing is refunded without evidence `rapidapi_gateway_404`, `connect_failed_before_send` or `never_dispatched`.
- Per-source identity and settlement (§5.3): JSearch `native_id = job_uid` (never `job_id`), 2xx `billed`, 404 with `x-rapidapi-proxy-response: true` `not_billed`, anything else after dispatch `ambiguous`, a connect error before any byte `not_billed`; page 2 only when ≥ 7 of the page's 10 `job_uid`s are new and a `normal` reservation succeeds; `cursor_reuse_ttl` set from V9 to the longest observed reuse interval (24 h, 2 h or 0). SerpApi 2xx `billed` (empty results billed), `account.json` free and read before every search; `SERPAPI_HL` set by V3. Adzuna every dispatched request `billed` whatever its status, `results_per_page=50`, per-cell page cap 3, description always `partial`, `redirect_url` never logged, exported or used as identity.
- Coverage (§5.5–5.6): `date_posted` is the smallest of `{today: 24 h, 3days: 72 h, week: 168 h, month: 720 h}` that is `≥ now − last_covered_at + 24 h`, `month` for a never-covered cell; a span over 720 h abandons `[last_covered_at, t − 720 h]` as `beyond_provider_window`; Adzuna `max_days_old = min(max_ad_age_days, ceil((now − last_covered_at) / 1 d) + 1)`; priority `staleness_h × weight × (0.5 + yield_ema)` with `staleness_h = 10,000` when never attempted, `weight = 0.25` at `zero_yield_streak ≥ 3` (still refreshed at least every 7 days), `yield_ema` EMA(α = 0.3); one exploration cell whenever ≥ 10 paid units were planned since the last exploration request; `consecutive_incomplete ≥ 3` marks a paid cell too broad; Arbeitnow page cap `min(10, max(3, ceil(interval_h / 30) + 1))`, overlap 2 h, stop at `x-ratelimit-remaining ≤ 5`, 300 items per page, gaps `[older, newer)`, seek page `max(1, floor(n_newer / 300))`; gap rows idempotent on `(cell_id, kind, span)`.
- Normalisation (§8): `norm_text` NFKC → remove U+00AD, U+200B–U+200D, U+FEFF → NBSP and U+202F to space → collapse → trim; `fold = immutable_unaccent(norm_text).casefold()`; the §8.2 gender-tag pattern and the §8.3 legal-form list verbatim; quality `short` < 200 characters, `garbage` when ≥ 3 lines equal `null`/`undefined`, > 40% non-letters or < 60 characters; language over ≥ 50 tokens with `|de_ratio − en_ratio| ≥ 0.2`; annualisation month × 12, week × 46, day × 220, hour × 1,720; nh3 allowed tags `p br ul ol li strong em b i h3 h4 a`, `href` http/https only, `rel="noopener noreferrer nofollow"` and `target="_blank"` forced.
- Grouping (§8.7): candidates share `company_key`, `|first_seen_at(A) − first_seen_at(B)| ≤ 45 days`, no `different` decision; G1 never two postings of one `<type>:<board>`; G2 a shared gazetteer id, every candidate pair within 25 km, both `remote` or both `country_wide`; G3 identical seniority-token sets; G4 `similarity(title_norm) ≥ 0.85`; G5 5-word shingles over `fold(description)`: full/full Jaccard ≥ 0.6 or containment ≥ 0.8, partial/full containment ≥ 0.7, partial/partial ≥ 0.9, under 20 shingles exact folded equality; at most 10 members per group.
- Availability (§8.11, §7.16.2): `stale_ttl_days` defaults to `max_ad_age_days` (30); ATS `closed('ats_absent')` at `absent_snapshots ≥ 2` and `taken_at − first_absent_at ≥ 24 h`; absence counting saturates at 2 and is idempotent per snapshot; mass disappearance when > 50% of the baseline and ≥ 5 postings are absent from a structurally complete snapshot, confirmed by two later complete snapshots ≥ 24 h apart; the stale sweep works in batches of 1,000; failures never age anything.
- Gazetteer (§8.12): German places with ≥ 1,000 inhabitants; European cities over 100,000 plus country names in DE and EN; postal codes `\b\d{5}\b`, several places within 10 km → `unique`; exclusion only when **all** candidates agree; `unresolved` always passes.
- Hard filters (§11.1): positive evidence only, unknown passes; `beyond_radius` at more than 2 × `radius_km` from **each** scope location; `DET_ALGORITHM_VERSION = "1"` (const, `scoring/versioning.py`); `too_old` is not stored.
- Graph (§9.1–9.2): `ainvoke(…, {"configurable": {"thread_id"}, "recursion_limit": 200, "max_concurrency": 4}, durability="sync")`; per-source semaphores JSearch 1, SerpApi 1, Adzuna 1, Arbeitnow 1, ATS host 2; node timeouts plan 60 s, fetch 15 min per traversal, plan_fallback 30 s, parse_normalize 10 min, resolve_identity 20 min, embed 20 min (120 s per batch), hard_filters 5 min, enqueue_targets 5 min, finalize 60 s; `RetryPolicy(max_attempts=3, initial_interval=0.5, backoff_factor=2, jitter=True, retry_on=is_retryable_db_error)` matching only SQLSTATE 40001, 40P01 and connection resets; subgraphs never compiled with `checkpointer=False`; state holds ids, counters and small enums only; at most 1,000 deferred items re-resolved per run.
- Receipts (§7.7): `raw_responses` keep the full body for 2xx and the first 4 KiB otherwise, zstd-compressed, deduplicated on `body_sha256` with `expires_at = fetched_at + 30 d` extended by a referencing row; `request_key = sha256(canonical_json({source_instance_id, operation, run_id, cell_id, page_no, sha256(cursor_in), detail_native_id, sha256(params)}))`; `fetch_requests.spec` never holds a secret.
- Retention (§20.1): raw bodies 30 d, `raw_items` 90 d, `fetch_requests` and `coverage_intervals` 13 months (397 d), ATS snapshots 30 d except the latest complete one and unacknowledged mass-disappearance snapshots per instance, unreferenced embeddings; job tombstoning arrives in M5.
- Fixtures (§18.4, §2.3): raw API responses contain recruiter personal data: they are NEVER committed. Raw recordings live only in the gitignored `.fixtures-raw/`; contract tests run on scrubbed fixtures: `jobhunter fixtures scrub <in> <out>` (Task 6 defines the procedure) writes `tests/contract/fixtures/`; `tests/contract/test_fixture_pii.py` fails on any e-mail or phone match, any `utm_source=` value other than `scrubbed` and any key-shaped string; live mode stays behind config (`JOBHUNTER_FAKE_SOURCES` unset **and** the secret file present) and is never run in CI; `JOBHUNTER_FAKE_SOURCES=1` serves every adapter from `JOBHUNTER_FIXTURES_DIR` (default `tests/contract/fixtures`) through the real adapter code.
- Spikes run with the user's real keys ONLY within these hard budgets for the whole milestone: JSearch ≤ 8 requests in total (V9 + V10), SerpApi 1 search (V3), Adzuna ≤ 5 requests (V18). Planned use: JSearch 7 (1 gateway-404 probe of the legacy route, which is not billed, plus 6 `search-v2` pages: V9 phases 1–2 and the day-2 phase shared by V10 and the 24-hour cursor check), SerpApi 1 search (plus free `account.json` reads), Adzuna 4, Arbeitnow ≤ 6 requests over 2 h (free, V8), ATS ≤ 12 free requests. Every spike step in Task 6 and Task 21 states its exact request count and its stop condition; the recorder counts every send *before* it happens, under a file lock, so a crash after the send or a transport failure can never spend more than the budget. The V10 day-2 phase may land in a later commit within M1; Task 25 (acceptance) stays blocked until its evidence is recorded in `docs/spikes/V10.md` and any identity instability it shows is resolved. In this dev container the keys are environment variables `RAPIDAPI_KEY` (JSearch via RapidAPI), `SERPAPI_API_KEY`, `ADZUNA_APP_ID` / `ADZUNA_APP_KEY`; the application itself reads provider keys as the spec's §19.2 secret files `rapidapi_key`, `serpapi_key`, `adzuna_app_id`, `adzuna_app_key` (the spike steps map the variables to the files); never print a key, never log one, never put one in a URL that is logged, never write one to a fixture.
- No LLM in M1 (deterministic pipeline). No paid call in CI (`contract` job: fixtures and the PII scan only; the weekly `smoke-free-sources.yml` touches free sources only).
- Commits use the scope `m1`, for example `feat(m1): …`.

## Review Focus

1. **Relative dates at the Berlin midnight and DST boundary.** SerpApi gives `Gestern` and `vor 2 Tagen` only; a user in Berlin at 00:30 local time expects "yesterday" to be the previous local day, also on the night the clocks change. Pinned by Task 3 (`test_relative_day_boundaries_follow_europe_berlin`).
2. **A provider date later than the sighting** (JSearch's hour-truncated `job_posted_at_timestamp` can lie ahead of `fetched_at`). The ad must be kept with `posted_at_effective = first_seen_at`, never rejected by the `posted_at_effective ≤ first_seen_at` CHECK. Pinned by Task 17 (`test_future_provider_date_is_clamped_to_first_seen_at`).
3. **An HTML error page or an oversized body in place of JSON** (a CDN or gateway outage). The fetch request becomes `failed('contract')` with the first 4 KiB stored and `item_count = 0`; its ledger entry settles by the billing rule alone (a JSearch 200 is `billed`: the provider charged, whatever the body), the instance is `contract_broken`, the run goes on and ends `partial`; nothing crashes, nothing is retried in the run, and a restart replays the failure from the receipt without a second send. Pinned by Task 5 (`test_html_body_and_oversized_body_are_contract_failures`), Task 12 (`test_contract_violation_fails_the_request_and_opens_the_breaker`, `test_contract_failure_is_replayed_after_restart`) and Task 14 (`test_contract_violation_is_visible_to_plan_fallback_in_the_same_run`).
4. **A restart between a paid reservation and its dispatch** (a deploy during a run). The user must not be billed: the undispatched spec ends `skipped(shutdown)` with its ledger entry `not_billed(never_dispatched)`, nothing is sent, and the next attempt re-plans it. Pinned by Task 14 (`test_shutdown_after_reservation_before_dispatch_releases_and_sends_nothing`).
5. **A watchlist company whose board disappears** (the company moved to another ATS: the board answers 404). Its postings are never aged by the failure, `source.not_found` is raised once, the instance is `not_found`, the next runs plan no request for it until the 6-hour half-open probe, and the probe's success resolves the alert. Pinned by Task 11 (`test_board_404_marks_not_found_and_skips_until_half_open`: health, alert and untouched postings) and Task 13 (`test_plan_skips_closed_instances_and_probes_half_open`: no request until the half-open probe).

---

## File Structure

The paths follow §4.2 for the M1 subset. Modules that §4.2 does not list are marked *(new)*; see Deviation 4.

| Path | Responsibility |
|---|---|
| `backend/src/jobhunter/db/migrations/versions/0005_m1_collection.py` | The M1 tables of §7.5, §7.7–7.10, `hard_filter_results` (§7.11), the `purge_fences.source_id` FK, `is_fenced` with the `source` branch, the transition triggers |
| `backend/src/jobhunter/db/{seed,transitions}.py` | `sources` and default `source_instances` seeds; the seven M1 state machines |
| `backend/src/jobhunter/run_settings.py` *(new)* | Typed, read-only view of the collection, quota and scope settings of a settings version (M2 owns editing) |
| `backend/src/jobhunter/normalize/{text,titles,companies,urls,keys,html,quality,language,fields}.py`, `normalize/data/{stopwords_de,stopwords_en}.txt` | §8.1–8.5, §8.8–8.10 pure functions |
| `backend/src/jobhunter/sources/parsing/{dates,salary,employment,extensions,locations}.py` | German/English dates, salaries, employment types, SerpApi extensions, the location parser |
| `backend/src/jobhunter/geo/gazetteer.py`, `geo/data/{de_places.tsv,non_de_places.tsv,country_names_de.tsv,ATTRIBUTION.md}`, `scripts/build_gazetteer.py` | The offline GeoNames gazetteer and its build |
| `backend/src/jobhunter/sources/{base,http,ratelimit,breaker,instances,fixtures,receipts,acquire,traverse,coverage,jsearch,serpapi,adzuna,arbeitnow,watchlist_patterns,watchlist_validate}.py`, `sources/ats/{common,greenhouse,lever,ashby,personio,recruitee,smartrecruiters}.py` | Adapter protocol and models, the HTTP layer, token buckets, the persisted breaker, instance sync, the fixture transport, receipts, the one-page acquisition protocol, cell traversal, coverage accounting, the adapters, watchlist detection and validation *(instances, fixtures, receipts, acquire, traverse, coverage, watchlist_validate new)* |
| `backend/src/jobhunter/quota/{windows,ledger,pacing,planner,fallback,dryrun}.py` | §6.1–6.8: windows and observations, the ledger, pacing and admission, the runtime planner, fallback eligibility, dry-run projections |
| `backend/src/jobhunter/dedup/{identity,merge,revisions,groups,shingles,representative}.py` | §8.6–8.8: identity resolution, merge and unmerge, revisions and lineage, probable-duplicate groups *(revisions new)* |
| `backend/src/jobhunter/availability/{rules,snapshots}.py` | §7.16.2, §8.11 |
| `backend/src/jobhunter/scoring/{filters,versioning}.py` | §11.1 hard filters, `DET_ALGORITHM_VERSION` |
| `backend/src/jobhunter/graphs/outcomes.py`, `graphs/collect/{state,deps,graph}.py`, `graphs/collect/nodes/{plan,fetch,plan_fallback,parse_normalize,resolve_identity,embed,hard_filters,enqueue_targets,finalize}.py` | The outcome reducer, the state, the dependencies and the ten nodes of `collect_graph` *(deps new)* |
| `backend/src/jobhunter/embeddings.py` *(new)*, `embedder/{Dockerfile,app.py,health.py,model.lock,download_model.sh}` | The worker's embedder client and `embeddings` writes; the embedder service |
| `backend/src/jobhunter/cli_fixtures.py` *(new)*, `scripts/spikes/{record_sources.py,derive_variants.py,v7_embedder.py}`, `scripts/smoke_m1.py` | `jobhunter fixtures scrub`; the budgeted recorder, the variant deriver, the V7 benchmark; the acceptance smoke |
| `backend/src/jobhunter/{config.py, worker/{main,runs,tasks,reconcilers}.py, observability/{health,metrics}.py, api/routes/health.py, privacy/retention.py}` | Embedder URL and fixture settings; the real graph in the worker, carry-forward 5 (Task 1) and 6 (Task 24); new metrics and health fields; M1 retention rules |
| `deploy/compose.dev.yaml`, `deploy/compose.fixtures.yaml` *(new)*, `.github/workflows/{ci.yml,smoke-free-sources.yml}` | The embedder service; the fixture-adapter override for the dev stack; the `contract` job and the weekly free-source smoke |
| `tests/support/{sources,collect,embedder}.py`, `tests/contract/{fixtures/,README.md,test_fixture_pii.py,test_*.py}`, `tests/{unit,property,integration,crash,live}/…` | Test helpers, scrubbed fixtures, contract, unit, property, integration, crash and live tests |
| `docs/spikes/{V3,V7,V8,V9,V10,V17,V18}.md` | Spike records |

---

### Task 1: M1 schema, seeds, the source fence and the M1 state machines

**Files:**
- Create: `backend/src/jobhunter/db/migrations/versions/0005_m1_collection.py`
- Modify: `backend/src/jobhunter/db/seed.py` (sources and default instances in `seed_defaults`; `DEFAULT_INSTANCES_SQL` shared with the test reset), `backend/src/jobhunter/db/transitions.py` (seven new `TransitionSpec`s), `tests/support/fixtures.py` (`_TRUNCATE`, `reset_database`), `tests/unit/test_transition_registry.py`, `backend/src/jobhunter/worker/main.py` (M0 carry-forward 5, Step 6)
- Test: `tests/integration/test_schema_m1.py`, `tests/integration/test_fences_m1.py`, `tests/unit/test_teardown.py`

**Interfaces:**
- Consumes: `transition`, `TRANSITIONS`, `seed_defaults`, `sync_transition_matrix`, `migrated`, `clean_db`, `pg_one`.
- Produces:
  - The tables of §7.5 (`sources`, `watchlist_companies`, `source_instances`, `queries`, `coverage_cells`, `coverage_intervals`), §7.7 (`fetch_requests`, `raw_responses`, `raw_items`, `ats_snapshots`, `ats_snapshot_members`), §7.8 (`quota_windows`, `quota_ledger`), §7.9 (`postings`, `posting_observations`, `jobs`, `job_revisions`, `job_revision_occurrences`, `job_revision_sources`, `ad_keys`, `ad_key_sources`, `merge_events`, `identity_conflicts`, `duplicate_groups`, `duplicate_group_members`, `dedup_decisions`, `job_tombstones`, `tombstone_keys`), §7.10 (`embeddings`) and §7.11 (`hard_filter_results`), plus `ats_snapshot_absences (snapshot_id bigint NOT NULL REFERENCES ats_snapshots(id) ON DELETE CASCADE, posting_id bigint NOT NULL REFERENCES postings(id) ON DELETE CASCADE, PRIMARY KEY (snapshot_id, posting_id))`, the frozen absent set of a mass-disappearance opening snapshot (Deviation 22), with their indexes, copied verbatim from the spec as static SQL in dependency order, the forward and cyclic FKs added afterwards with `ALTER TABLE … ADD FOREIGN KEY`.
  - `ALTER TABLE purge_fences ADD FOREIGN KEY (source_id) REFERENCES sources(id)` (M0 Deviation 1).
  - `CREATE OR REPLACE FUNCTION is_fenced(p_job bigint, p_tuple bigint DEFAULT NULL)` with the `global`, `jobs` and `source` branches of §7.14 (the `profile` branch is M2's; M0 Deviation 2).
  - Triggers `<table>_assert_transition BEFORE UPDATE OF <state column> … EXECUTE FUNCTION assert_transition('<state column>')` on `fetch_requests(status)`, `raw_items(disposition)`, `postings(availability)`, `quota_ledger(state)`, `duplicate_groups(status)`, `identity_conflicts(status)`, `coverage_intervals(gap_status)`.
  - `seed.SOURCES: Final[tuple[SourceSeed, ...]]` with `@dataclass(frozen=True) class SourceSeed: id: str; kind: str; precedence_rank: int; can_close: bool; default_completeness: str`: `jsearch` (`paid_search`, 2, false, full), `serpapi` (`paid_search`, 2, false, full), `adzuna` (`quota_search`, 4, false, partial), `arbeitnow` (`firehose`, 3, false, full), `greenhouse`, `lever`, `ashby`, `personio`, `recruitee`, `smartrecruiters` (`ats`, 1, true, full); `seed.DEFAULT_INSTANCES: Final = ("jsearch", "serpapi", "adzuna", "arbeitnow")`, each with `instance_key = 'default'`. Both inserted `ON CONFLICT DO NOTHING` by `seed_defaults`.
  - `TRANSITIONS` entries (state column, reason column, changed-at, edges, settable):

| Table | Edges | Changed-at | Reason column | Settable |
|---|---|---|---|---|
| `fetch_requests` (`status`) | `planned→reserved`, `planned→dispatched`, `planned→skipped`, `reserved→dispatched`, `reserved→skipped`, `dispatched→succeeded`, `dispatched→failed`, `dispatched→ambiguous` | `dispatched → dispatched_at`; `succeeded`, `failed`, `ambiguous`, `skipped → finished_at` | none (`skip_reason` and `error_code` are `settable`; the two CHECKs of §7.7 make them mandatory) | `skip_reason`, `error_code`, `http_status`, `quota_ledger_id`, `raw_response_id`, `continuation`, `next_cursor`, `items`, `new_native_ids`, `new_postings` |
| `raw_items` (`disposition`) | `pending→linked`, `pending→out_of_scope`, `pending→rejected`, `pending→deferred`, `deferred→linked`, `deferred→out_of_scope`, `deferred→rejected`, `deferred→deferred`, `rejected→pending` | every state but `pending → disposed_at`; `clear_on_exit = {"rejected": ("disposed_at",)}` sets it to NULL on `rejected→pending` | `reason_code` (the callers of `rejected→pending` and `deferred→linked` pass `CLEAR_REASON`) | `observation_id`, `normalized` |
| `postings` (`availability`) | `stale→active`, `unknown→active`, `closed→active`, `active→stale`, `active→unknown`, `active→closed`, `unknown→closed` | every state → `availability_changed_at` | `availability_reason` | `absent_snapshots`, `first_absent_at`, `last_absent_snapshot_id` |
| `quota_ledger` (`state`) | `reserved→dispatched`, `reserved→not_billed`, `dispatched→billed`, `dispatched→not_billed`, `dispatched→ambiguous` | `dispatched → dispatched_at`; `billed`, `not_billed`, `ambiguous → settled_at` | `evidence` | `http_status`, `provider_remaining`, `window_start` |
| `duplicate_groups` (`status`) | `active→dissolved` | `dissolved → updated_at` | `election_reason` | `representative_job_id`, `unscorable_reason`, `elected_at` |
| `identity_conflicts` (`status`) | `open→resolved_separate`, `open→resolved_merged` | both → `resolved_at` | none | — |
| `coverage_intervals` (`gap_status`) | `open→closed`, `open→abandoned` | both → `closed_at` | `reason` | `gap_cursor` |

  None of them has owner columns: the run's ownership predicate is checked by the caller's `runs … FOR SHARE` (§3.3.3), as the M0 `runs`/`operations` specs check theirs.
  - `fixtures._TRUNCATE` also names `watchlist_companies, source_instances, queries, coverage_cells, coverage_intervals, fetch_requests, raw_responses, raw_items, ats_snapshots, ats_snapshot_members, ats_snapshot_absences, quota_windows, quota_ledger, postings, posting_observations, jobs, job_revisions, job_revision_occurrences, job_revision_sources, ad_keys, ad_key_sources, merge_events, identity_conflicts, duplicate_groups, duplicate_group_members, dedup_decisions, job_tombstones, tombstone_keys, embeddings, hard_filter_results`. `source_instances` is named on purpose: `TRUNCATE … CASCADE` truncates every table that references a named one whatever its `ON DELETE` action, so the default instances vanish with `watchlist_companies` and `ats_snapshots` in any case. `reset_database` therefore re-inserts them in the same transaction with `seed.DEFAULT_INSTANCES_SQL` (the seed's own `INSERT … ON CONFLICT DO NOTHING`), and also restores the configuration state that M1 tests mutate, **in this order** (every row that references a newer settings version is re-pointed before that version is deleted; `schedule_state.settings_version_id` is an immediate FK): `UPDATE active_config SET settings_version_id = <v1>, scope_version_id = <v1.scope>, pending_scope_version_id = NULL, llm_tuple_id = NULL`; the M0 `_SCHEDULE_RESET` (which points `schedule_state` at version 1); `DELETE FROM settings_versions WHERE version_no > 1`; `DELETE FROM scope_versions WHERE id NOT IN (SELECT scope_version_id FROM settings_versions)`; `UPDATE ranking_state SET generation = 0, updated_at = now()`. Instance ids change with every reset, so no test, helper or fixture assumes an id: they resolve instances by `(source_id, instance_key)` (`tests/support/collect.instance_id(conn, source_id, instance_key="default")`). The `sources` rows are seeds and stay.

The migration's differences from the spec text, each with a SQL comment naming its owner (Deviations 1 and 2): `queries.proposal_id bigint` has no FK (M2 adds it with `query_proposals`); `profile_vectors` is not created (M2); `hard_filter_results` is created here; the `source_instances` CHECK reads `(health IN ('breaker_open','auth_failed','contract_broken','not_found')) = (breaker_opened_at IS NOT NULL AND half_open_after IS NOT NULL)`; the CHECK constraints a test asserts by name carry an explicit `CONSTRAINT <name>` (the spec leaves them unnamed, and PostgreSQL's generated `<table>_check<n>` names depend on DDL order): `fetch_requests_skipped_reason_check`, `quota_windows_tokens_check`, `source_instances_breaker_check`, `coverage_intervals_covered_reason_check`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_transition_registry.py (append)
def test_m1_machines() -> None:
    assert TRANSITIONS["fetch_requests"].allowed == {("planned", "reserved"), ("planned", "dispatched"), ("planned", "skipped"),
        ("reserved", "dispatched"), ("reserved", "skipped"), ("dispatched", "succeeded"), ("dispatched", "failed"), ("dispatched", "ambiguous")}
    assert TRANSITIONS["raw_items"].reason_col == "reason_code" and ("rejected", "pending") in TRANSITIONS["raw_items"].allowed
    assert ("active", "active") not in TRANSITIONS["postings"].allowed and ("unknown", "closed") in TRANSITIONS["postings"].allowed
    assert TRANSITIONS["quota_ledger"].changed_at == {"dispatched": "dispatched_at", "billed": "settled_at", "not_billed": "settled_at", "ambiguous": "settled_at"}
    assert TRANSITIONS["coverage_intervals"].state_col == "gap_status" and TRANSITIONS["identity_conflicts"].reason_col is None
```

```python
# tests/integration/test_schema_m1.py
import pytest, sqlalchemy as sa
from jobhunter.db.sql import CHECK_VIOLATION, UNIQUE_VIOLATION, PgErrorInfo, pg_error
from tests.support.fixtures import reset_database
pytestmark = pytest.mark.integration
M1_TABLES = {"sources", "watchlist_companies", "source_instances", "queries", "coverage_cells", "coverage_intervals", "fetch_requests",
    "raw_responses", "raw_items", "ats_snapshots", "ats_snapshot_members", "quota_windows", "quota_ledger", "postings", "posting_observations",
    "jobs", "job_revisions", "job_revision_occurrences", "job_revision_sources", "ad_keys", "ad_key_sources", "merge_events", "identity_conflicts",
    "duplicate_groups", "duplicate_group_members", "dedup_decisions", "job_tombstones", "tombstone_keys", "embeddings", "hard_filter_results"}

async def test_tables_seeds_and_default_instances(engine) -> None:
    async with engine.connect() as c:
        names = set((await c.execute(sa.text("SELECT table_name FROM information_schema.tables WHERE table_schema='public'"))).scalars())
        assert M1_TABLES <= names and "profile_vectors" not in names
        rows = (await c.execute(sa.text("SELECT id, kind, precedence_rank, can_close, default_completeness FROM sources ORDER BY id"))).all()
        assert [tuple(r) for r in rows if r.id in ("adzuna", "greenhouse", "jsearch")] == [
            ("adzuna", "quota_search", 4, False, "partial"), ("greenhouse", "ats", 1, True, "full"), ("jsearch", "paid_search", 2, False, "full")]
        assert len(rows) == 10
        assert sorted((await c.execute(sa.text("SELECT source_id FROM source_instances WHERE instance_key='default'"))).scalars()) == ["adzuna", "arbeitnow", "jsearch", "serpapi"]

RUN = "INSERT INTO runs (trigger,scheduled_slot,status,include_paid,settings_version_id,scope_version_id) SELECT 'manual',NULL,'queued',false,settings_version_id,scope_version_id FROM active_config RETURNING id"
CELL = "INSERT INTO coverage_cells (source_instance_id) SELECT id FROM source_instances WHERE source_id='arbeitnow' AND instance_key='default' RETURNING id"
@pytest.mark.parametrize(("statement", "constraint"), [
    ("INSERT INTO fetch_requests (run_id, run_attempt_no, cell_id, source_instance_id, phase, operation, request_key, page_no, request_class, spec, status) "
     "VALUES (:run,1,:cell,(SELECT source_instance_id FROM coverage_cells WHERE id=:cell),'primary','list','\\x00',1,'free','{}','skipped')", "fetch_requests_skipped_reason_check"),   # skipped without skip_reason
    ("INSERT INTO quota_windows (provider, window_start, window_end, is_current, limit_units, reserve_units, state, tokens, tokens_at, capacity) VALUES ('jsearch', now(), now() + interval '1 day', true, 200, 20, 'validated', 3, now(), 2)", "quota_windows_tokens_check"),   # tokens above capacity
    ("INSERT INTO source_instances (source_id, instance_key, health) VALUES ('jsearch', 'x', 'breaker_open')", "source_instances_breaker_check"),   # open without timestamps
    ("INSERT INTO coverage_intervals (cell_id, kind, span, reason) VALUES (:cell, 'covered', tstzrange(now() - interval '1 day', now()), 'page_cap')", "coverage_intervals_covered_reason_check"),   # covered with a reason
])
async def test_checks_reject_violations(engine, statement, constraint) -> None:
    async with engine.begin() as c:   # the dependencies are valid rows, so only the intended CHECK can fail
        run = (await c.execute(sa.text(RUN))).scalar_one(); cell = (await c.execute(sa.text(CELL))).scalar_one()
        with pytest.raises(sa.exc.IntegrityError) as bad: await c.execute(sa.text(statement), {"run": run, "cell": cell})
    assert pg_error(bad.value) == PgErrorInfo(CHECK_VIOLATION, constraint)   # the exact named constraint, through M0's helper

async def test_one_current_window_per_provider(engine) -> None:
    ins = sa.text("INSERT INTO quota_windows (provider, window_start, window_end, is_current, limit_units, reserve_units, state, tokens, tokens_at, capacity) VALUES ('adzuna', :s, :s + interval '30 days', true, 2250, 0, 'validated', 1, :s, 1)")
    async with engine.begin() as c:
        await c.execute(ins, {"s": "2026-10-01T00:00:00Z"})
        with pytest.raises(sa.exc.IntegrityError) as bad: await c.execute(ins, {"s": "2026-10-02T00:00:00Z"})
    assert (pg_error(bad.value).sqlstate, pg_error(bad.value).constraint) == (UNIQUE_VIOLATION, "quota_windows_current")

async def test_new_tables_have_transition_triggers(engine) -> None:
    async with engine.begin() as c:
        rid = (await c.execute(sa.text(RUN))).scalar_one(); cell = (await c.execute(sa.text(CELL))).scalar_one()
        fr = (await c.execute(sa.text("INSERT INTO fetch_requests (run_id, run_attempt_no, cell_id, source_instance_id, phase, operation, request_key, page_no, request_class, spec, status) "
            "VALUES (:r,1,:c,(SELECT source_instance_id FROM coverage_cells WHERE id=:c),'primary','list','\\x01',1,'free','{}','planned') RETURNING id"), {"r": rid, "c": cell})).scalar_one()
        with pytest.raises(sa.exc.IntegrityError, match="disallowed transition fetch_requests: planned -> succeeded"):
            await c.execute(sa.text("UPDATE fetch_requests SET status='succeeded' WHERE id=:i"), {"i": fr})

V2 = ("INSERT INTO settings_versions (version_no, schema_version, payload, scope_version_id, model_policy_id, execution_config_id, evaluation_policy_id, effect_classes) "
      "SELECT 2, schema_version, payload, scope_version_id, model_policy_id, execution_config_id, evaluation_policy_id, '{}' FROM settings_versions WHERE version_no = 1 RETURNING id")
async def test_reset_reseeds_the_default_instances_and_the_configuration(engine, migrated) -> None:
    async with engine.begin() as c:
        await c.execute(sa.text("INSERT INTO watchlist_companies (display_name, ats_type, board_slug, validation_status) VALUES ('X','greenhouse','x','ok')"))
        await c.execute(sa.text("UPDATE ranking_state SET generation = 7"))
        v2 = (await c.execute(sa.text(V2))).scalar_one()   # a newer version that the schedule and the active config point at, as the M1 tests do
        await c.execute(sa.text("UPDATE active_config SET settings_version_id = :v"), {"v": v2}); await c.execute(sa.text("UPDATE schedule_state SET settings_version_id = :v"), {"v": v2})
    reset_database(migrated)   # must not fail on schedule_state's FK: the schedule is re-pointed before version 2 is deleted
    async with engine.connect() as c:
        v1 = (await c.execute(sa.text("SELECT id FROM settings_versions WHERE version_no = 1"))).scalar_one()
        assert sorted((await c.execute(sa.text("SELECT source_id FROM source_instances WHERE instance_key='default'"))).scalars()) == ["adzuna", "arbeitnow", "jsearch", "serpapi"]
        assert (await c.execute(sa.text("SELECT max(version_no) FROM settings_versions"))).scalar_one() == 1
        assert (await c.execute(sa.text("SELECT settings_version_id FROM schedule_state"))).scalar_one() == v1 == (await c.execute(sa.text("SELECT settings_version_id FROM active_config"))).scalar_one()
        assert (await c.execute(sa.text("SELECT generation FROM ranking_state"))).scalar_one() == 0 and (await c.execute(sa.text("SELECT count(*) FROM watchlist_companies"))).scalar_one() == 0
```

```python
# tests/integration/test_fences_m1.py
import pytest, sqlalchemy as sa
pytestmark = pytest.mark.integration
JOB = ("INSERT INTO jobs (company_key, company_display, title, title_norm, location_kind, work_mode_possible, work_mode_source, employment_source, "
       "ad_language, completeness, posted_precision, first_seen_at, last_seen_at, availability, availability_changed_at, source_ids, field_provenance, det_facts_hash) "
       "VALUES ('acme','Acme','T','t','unknown','{remote,hybrid,onsite}','none','none','unknown','full','unknown',now(),now(),'active',now(),'{adzuna}','{}','\\x00') RETURNING id")

async def test_source_fence_matches_posting_lineage_and_stored_set(engine) -> None:
    async with engine.begin() as c:
        job_a, job_b, job_c = [(await c.execute(sa.text(JOB))).scalar_one() for _ in range(3)]
        adz = (await c.execute(sa.text("SELECT id FROM source_instances WHERE source_id='adzuna'"))).scalar_one()
        await c.execute(sa.text("INSERT INTO postings (source_instance_id, native_id, job_id, first_seen_at, last_seen_at, availability, availability_changed_at) VALUES (:i,'1',:j,now(),now(),'active',now())"), {"i": adz, "j": job_a})
        rev = (await c.execute(sa.text("INSERT INTO job_revisions (job_id, revision_no, material_hash, llm_input, description_text, description_completeness, ad_language, embedding_status) VALUES (:j,1,'\\x01','{}','d','full','de','awaiting_embedding') RETURNING id"), {"j": job_b})).scalar_one()
        await c.execute(sa.text("INSERT INTO job_revision_sources (job_revision_id, field, source_id) VALUES (:r,'description_text','adzuna')"), {"r": rev})
        fence = (await c.execute(sa.text("INSERT INTO purge_fences (kind, source_id, job_ids, manifest_id) VALUES ('source','adzuna',ARRAY[:c]::bigint[],1) RETURNING id"), {"c": job_c})).scalar_one()
        fenced = lambda j: c.execute(sa.text("SELECT is_fenced(:j)"), {"j": j})
        assert [(await fenced(j)).scalar_one() for j in (job_a, job_b, job_c)] == [True, True, True]
        other = (await c.execute(sa.text(JOB))).scalar_one()
        assert (await fenced(other)).scalar_one() is False
        await c.execute(sa.text("UPDATE purge_fences SET released_at = now() WHERE id=:f"), {"f": fence})
        assert (await fenced(job_a)).scalar_one() is False
        with pytest.raises(sa.exc.IntegrityError): await c.execute(sa.text("INSERT INTO purge_fences (kind, source_id, manifest_id) VALUES ('source','nope',1)"))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_transition_registry.py tests/integration/test_schema_m1.py tests/integration/test_fences_m1.py -q`
Expected: FAIL with `KeyError: 'fetch_requests'` and `UndefinedTable` errors (the tables do not exist).

- [ ] **Step 3: Write the migration, the seeds, the registry entries and the fixture reset**

- The migration docstring lists the spec sections it copies and the four deviations, like `0001_m0_baseline`. `op.execute` per statement; `-- ruff: noqa: E501`.
- `seed_defaults` inserts `SOURCES` and `DEFAULT_INSTANCES` after `ranking_state` and before `sync_transition_matrix` (both `ON CONFLICT DO NOTHING`).
- `transitions.py`: the seven specs as in the Interfaces table; `TransitionSpec` needs no change (`reason_col=None` for `fetch_requests` and `identity_conflicts`). `raw_items.disposed_at` is a changed-at column *and* a `clear_on_exit` column of `rejected`, never a settable one: `TransitionSpec.__post_init__` refuses `settable & managed`, and `_validate` already applies `clear_on_exit` on every exit to another state. `test_m1_machines` also asserts `TRANSITIONS["raw_items"].clear_on_exit == {"rejected": ("disposed_at",)}` and `"disposed_at" not in TRANSITIONS["raw_items"].settable`, and `tests/unit/test_transition_registry.py` gains `test_registry_imports_without_a_managed_settable_clash` (constructing a spec whose `settable` names a changed-at column raises `ValueError`, the guard this table relies on). The callers in Tasks 15 and 18 pass `reason=CLEAR_REASON` on `rejected→pending` and `deferred→linked`: `reason=None` keeps the old reason and the `raw_items` CHECK `(disposition IN (…)) = (reason_code IS NOT NULL)` refuses the row.
- `reset_database`, all in the one transaction and in this order: the truncate, `DEFAULT_INSTANCES_SQL`, the `active_config` restoration, the M0 schedule reset (`_SCHEDULE_RESET`, now **before** the deletes), then `DELETE FROM settings_versions WHERE version_no > 1`, the `scope_versions` delete and the `ranking_state` reset.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit tests/integration -q`
Expected: PASS (every M0 test still passes with the longer truncate list and the reseed).

- [ ] **Step 5: Commit**

```bash
git add backend/src/jobhunter/db tests
git commit -m "feat(m1): M1 schema (sources, receipts, quota, postings, jobs, groups, embeddings, hard filters), source fence, M1 state machines"
```

- [ ] **Step 6: M0 carry-forward 5, before any task touches the worker**

`_WorkerProcess.teardown` (`worker/main.py`) closes the lock session only when the watchdog thread is not alive after its join (`Watchdog.is_alive()`); otherwise it logs `lock_session_left_open` and lets the process exit drop the socket and the lock. Test first, `tests/unit/test_teardown.py`: `test_lock_session_is_not_closed_while_the_watchdog_is_alive` (a `_WorkerProcess` with a fake watchdog whose `is_alive()` stays `True` after `stop()`: `teardown` returns, `lock_session.close` was not called, `lock_session_left_open` logged; with `is_alive()` `False`: `close` is called last). Run `uv run pytest tests/unit/test_teardown.py -q` (FAIL: `close` called), implement, run again (PASS), then:

```bash
git add backend/src/jobhunter/worker/main.py tests/unit/test_teardown.py
git commit -m "fix(worker): keep the lock session open while the watchdog is in a heartbeat (M0 carry-forward 5)"
```

---

### Task 2: Text primitives, titles, company keys, URL canonicalisation and strong keys

**Files:**
- Create: `backend/src/jobhunter/normalize/__init__.py`, `normalize/text.py`, `normalize/titles.py`, `normalize/companies.py`, `normalize/urls.py`, `normalize/keys.py`
- Test: `tests/unit/test_normalize_text.py`, `tests/unit/test_titles.py`, `tests/unit/test_companies.py`, `tests/unit/test_urls.py`, `tests/unit/test_keys.py`, `tests/integration/test_fold_matches_db.py`

**Interfaces:**
- Consumes: nothing from M1.
- Produces:
  - `text.norm_text(s: str) -> str` (§8.1 steps 1–5); `text.unaccent(s: str) -> str` (NFKD, combining marks removed, the PostgreSQL `unaccent.rules` ligature and special-letter map: `ß→ss`, `æ→ae`, `œ→oe`, `ø→o`, `ł→l`, `đ→d`, `þ→th`, `ð→d`, `ı→i`); `text.fold(s: str) -> str` = `unaccent(norm_text(s)).casefold()`
  - `titles.SeniorityToken = Literal["intern", "working_student", "apprentice", "junior", "senior", "lead", "principal", "head"]`; `titles.SENIORITY_LADDER: Final = ("intern", "working_student", "apprentice", "junior", "mid", "senior", "lead", "principal", "head")` (the §12.4 enum order; `mid` is the level without a token); `titles.GENDER_TAG: Final[re.Pattern[str]]` (the §8.2 pattern); `titles.strip_gender_tags(s: str) -> str`; `@dataclass(frozen=True) class NormalizedTitle: title: str; title_norm: str; seniority_tokens: frozenset[SeniorityToken]`; `titles.normalize_title(raw: str) -> NormalizedTitle` (§8.2 steps 1–6); `titles.seniority_tokens(title_norm: str) -> frozenset[SeniorityToken]`
  - `companies.LEGAL_FORMS: Final[tuple[str, ...]]` (the §8.3 list, longest first); `companies.company_key(raw: str) -> str`
  - `urls.TRACKING_PARAMS: Final = ("utm_", "gclid", "fbclid", "msclkid", "_hsenc", "_hsmi", "mc_cid", "mc_eid")`; `urls.canonical_url(u: str) -> str` (§8.5 steps 1–3; a value that is not an `http(s)` URL is returned as `norm_text(u)`)
  - `@dataclass(frozen=True) class keys.AdKey: ns: str; value: str` with the property `exclusive: bool` (`ns != "htidocid"`); `keys.keys_from_link(primary_link: str) -> list[AdKey]` (the §8.4 publisher rules on the canonical link; the Adzuna `/land/ad/` form yields nothing); `keys.decode_serpapi_job_id(job_id: str) -> str | None` (the `htidocid` of the base64 JSON); `keys.jsearch_uid_key(job_uid: str) -> AdKey`; `keys.ats_key(ats_type: str, board: str, posting_id: str) -> AdKey` (`ns = f"ats:{ats_type}:{board}"`); `keys.decode_ba_refnr(segment: str) -> str | None`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_titles.py
import pytest
from jobhunter.normalize.titles import normalize_title, strip_gender_tags
@pytest.mark.parametrize("raw", ["Entwickler (m/w/d)", "Entwickler (w/m/d)", "Entwickler (f/m/x)", "Entwickler (w_m_d)", "Entwickler (m|w|d)",
    "Entwickler (*)", "Entwickler (all genders)", "Entwickler (gn)", "Entwickler m/w/d", "Entwickler*in", "Entwickler:innen", "Entwickler_in", "Entwickler/-in", "EntwicklerIn"])
def test_gender_tags_are_stripped(raw) -> None: assert strip_gender_tags(raw).strip() == "Entwickler"
def test_normalized_title_and_tokens() -> None:
    t = normalize_title("Sr. Software Engineer (Data & Platform) – Berlin")
    assert (t.title_norm, t.seniority_tokens) == ("senior software engineer (data & platform) berlin", frozenset({"senior"}))
    assert normalize_title("Werkstudent (m/w/d) Backend").seniority_tokens == {"working_student"}
    assert normalize_title("Head of Engineering").seniority_tokens == {"head"} and normalize_title("Teamleiterin Buchhaltung").seniority_tokens == {"lead"}
    assert normalize_title("Software­entwickler").title_norm == "softwareentwickler"
    assert normalize_title("Senior Python Entwickler (m/w/d) | Berlin").title_norm == "senior python entwickler berlin"
```

(The first assertion's expected `title_norm` is `"senior software engineer (data & platform) berlin"`: step 4 trims the separators `-–—|,:` at both ends after collapsing runs; a separator in the middle collapses to one space, `&` stays.)

```python
# tests/unit/test_companies.py
from jobhunter.normalize.companies import company_key
def test_legal_forms_and_fallback() -> None:
    assert company_key("Zalando SE") == "zalando" and company_key("Vitaservices GmbH & Co. KG") == "vitaservices"
    assert company_key("Delivery Hero SE") == "delivery hero" and company_key("Müller Deutschland GmbH") == "muller"
    assert company_key("Deutschland GmbH") == "deutschland" and company_key("GmbH") == "gmbh"
    assert company_key("UG (haftungsbeschränkt) Acme") == "acme" and company_key("Acme Inc.") == "acme"
```

```python
# tests/unit/test_urls.py
from jobhunter.normalize.urls import canonical_url
def test_per_host_rules() -> None:
    assert canonical_url("https://www.stepstone.de/stellenangebote--X--14607704-inline.html?utm_source=a&rltr=1#top") == "https://stepstone.de/stellenangebote--X--14607704-inline.html"
    assert canonical_url("https://de.linkedin.com/jobs/view/senior-x-4480111992?trk=a") == "https://de.linkedin.com/jobs/view/senior-x-4480111992"
    assert canonical_url("https://de.indeed.com/viewjob?jk=abc123&from=serp") == "https://de.indeed.com/viewjob?jk=abc123"
    assert canonical_url("https://www.adzuna.de/details/5926833612?se=XYZ&v=123&utm_medium=api") == "https://adzuna.de/details/5926833612"
    assert canonical_url("https://example.com/b?z=1&a=2&gclid=9") == "https://example.com/b?a=2&z=1"
    assert canonical_url("https://www.it-jobs.de/x_ers-MTIw/") == "https://it-jobs.de/x_ers-MTIw"
```

```python
# tests/unit/test_keys.py
from jobhunter.normalize.keys import AdKey, ats_key, decode_ba_refnr, decode_serpapi_job_id, keys_from_link
import base64, json
def test_publisher_keys_from_primary_links() -> None:
    assert keys_from_link("https://www.stepstone.de/stellenangebote--A--14607704-inline.html") == [AdKey("pub:stepstone.de", "14607704")]
    assert keys_from_link("https://de.linkedin.com/jobs/view/senior-x-4480111992") == [AdKey("pub:linkedin.com", "4480111992")]
    assert keys_from_link("https://de.indeed.com/viewjob?jk=abc123") == [AdKey("pub:indeed", "abc123")]
    assert keys_from_link("https://www.xing.com/jobs/berlin-software-158579394") == [AdKey("pub:xing.com", "158579394")]
    assert keys_from_link("https://join.com/companies/roboservice/16831124-robotics") == [AdKey("pub:join.com", "16831124")]
    assert keys_from_link("https://www.arbeitnow.com/jobs/companies/roboservice-gmbh/robotics-engineer-194025") == [AdKey("arbeitnow", "194025")]
    assert keys_from_link("https://www.adzuna.de/details/5926833612") == [AdKey("adzuna", "5926833612")]
    assert keys_from_link("https://www.adzuna.de/land/ad/5926833612?se=x") == []
    assert keys_from_link("https://www.it-jobs.de/job/x_ers-MTIwMTYtMTAwMDU1NDg2NDUtUyAx") == [AdKey("pub:ba_refnr", "12016-10005548645-S 1")]
    assert keys_from_link("https://jobs.eu.lever.co/acme/8b1d9f0e-1111-2222-3333-444444444444") == [AdKey("ats:lever:eu:acme", "8b1d9f0e-1111-2222-3333-444444444444")]
    assert keys_from_link("https://boards.greenhouse.io/acme/jobs/4567") == [AdKey("ats:greenhouse:acme", "4567")] and AdKey("htidocid", "x").exclusive is False
def test_serpapi_and_ba_decoders() -> None:
    job_id = base64.b64encode(json.dumps({"job_title": "x", "htidocid": "UMsYA14AEpQuse3eAAAAAA==", "fc": "session"}).encode()).decode()
    assert decode_serpapi_job_id(job_id) == "UMsYA14AEpQuse3eAAAAAA==" and decode_serpapi_job_id("not base64!") is None
    assert decode_ba_refnr("MTgyNTEtMzM3NjctUw==") == "18251-33767-S" and ats_key("personio", "everphone", "12").ns == "ats:personio:everphone"
```

```python
# tests/integration/test_fold_matches_db.py
import pytest, sqlalchemy as sa
from jobhunter.normalize.text import fold, norm_text
pytestmark = pytest.mark.integration
CORPUS = ["Müller Straße Café", "Zürich Köln Mönchengladbach", "Groß-Umstadt", "Łódź Đakovo Þór", "Ærø Œuvre ørsted", "İstanbul", "Softwareentwickler­ (m/w/d) Berlin"]
@pytest.mark.parametrize("s", CORPUS)
async def test_python_fold_equals_db_unaccent_casefolded(engine, s) -> None:
    async with engine.connect() as c:
        db = (await c.execute(sa.text("SELECT lower(immutable_unaccent(:s))"), {"s": norm_text(s)})).scalar_one()
    assert fold(s) == db.casefold()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_titles.py tests/unit/test_companies.py tests/unit/test_urls.py tests/unit/test_keys.py tests/integration/test_fold_matches_db.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'jobhunter.normalize'`

- [ ] **Step 3: Implement the five modules**

- `titles.normalize_title`: `norm_text` → `strip_gender_tags` (the three §8.2 rules, case-insensitive; the suffix rule applies after a letter and `In` only before a word boundary) → `Sr.`/`Sr`/`Snr` → `senior`, `Jr.`/`Jr` → `junior` → collapse punctuation runs and trim `-–—|,:` at both ends → `title_norm = fold(result)` → tokens by whole-word or prefix match of the §8.2 table (`teamleiter` matches by prefix `teamleit`, `praktikant` by prefix `praktik`, `werkstudent` by prefix).
- `companies.company_key`: §8.3 steps 1–4; the legal forms are matched as whole words anywhere, longest first (`ug (haftungsbeschränkt)` before `ug`).
- `urls.canonical_url`: `urllib.parse.urlsplit`; host rules keyed by the host's registrable suffix (`stepstone.de`, `linkedin.com`, `indeed.*`, `xing.com`, `join.com`, `arbeitnow.com`, `adzuna.*`, `it-jobs.de`, the ATS hosts of §8.4); otherwise generic tracking removal then sorted parameters.
- `keys.keys_from_link`: canonicalise first, then the §8.4 regexes; the `it-jobs.de` refnr is base64-decoded (standard alphabet, padding added) and must decode to printable ASCII.
- The DB comparison may show a character where PostgreSQL's `unaccent.rules` differs from the NFKD map: extend `text.unaccent`'s map rather than the test.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit tests/integration/test_fold_matches_db.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/src/jobhunter/normalize tests
git commit -m "feat(m1): text primitives, title and company normalisation, URL canonicalisation, strong keys"
```

---

### Task 3: HTML, quality gate, language detection, dates, salaries, employment types and SerpApi extensions

**Files:**
- Create: `backend/src/jobhunter/normalize/html.py`, `normalize/quality.py`, `normalize/language.py`, `normalize/data/stopwords_de.txt`, `normalize/data/stopwords_en.txt`, `backend/src/jobhunter/sources/__init__.py`, `sources/parsing/__init__.py`, `sources/parsing/dates.py`, `sources/parsing/salary.py`, `sources/parsing/employment.py`, `sources/parsing/extensions.py`
- Modify: `pyproject.toml` (`nh3==0.3.7`)
- Test: `tests/unit/test_html_quality_language.py`, `tests/unit/test_dates.py`, `tests/unit/test_salary.py`, `tests/unit/test_employment_extensions.py`

**Interfaces:**
- Consumes: `fold`, `norm_text` (Task 2).
- Produces:
  - `html.ALLOWED_TAGS: Final = frozenset({"p", "br", "ul", "ol", "li", "strong", "em", "b", "i", "h3", "h4", "a"})`; `html.sanitize_html(raw: str) -> str` (nh3, `href` http/https only, `rel` and `target` forced, everything else stripped); `html.html_to_text(sanitized: str) -> str` (block tags become line breaks, entities unescaped, then `norm_text` per line); `html.unescape_html(s: str) -> str`
  - `quality.Quality = Literal["ok", "short", "garbage"]`; `quality.description_quality(text: str) -> Quality` (§8.9 thresholds 200 / 60 / 40% / 3 `null`-or-`undefined` lines)
  - `language.AdLanguage = Literal["de", "en", "unknown"]`; `language.detect_language(text: str) -> AdLanguage` (the top-200 stop-word lists shipped as data files, ≥ 50 tokens, threshold 0.2)
  - `dates.Precision = Literal["exact", "hour", "day", "relative_day", "ingest", "unknown"]`; `@dataclass(frozen=True) class ParsedDate: at: datetime; precision: Precision; raw: str | None`; `dates.BERLIN: Final = ZoneInfo("Europe/Berlin")`; `dates.parse_relative(text: str, *, reference: datetime) -> ParsedDate | None` (DE and EN forms of §5.3.2; `hour` for hours, `relative_day` for days/`Gestern`/`Heute`/`yesterday`/`today`/`just posted`, resolved in Europe/Berlin and returned in UTC); `dates.jsearch_precision(relative: str) -> Literal["hour", "day"]`; `dates.from_epoch(seconds: int | float, *, millis: bool = False, precision: Precision) -> ParsedDate`; `dates.parse_iso(text: str, *, precision: Precision) -> ParsedDate | None`; `dates.response_date(headers: Mapping[str, str], fallback: datetime) -> datetime` (the `Date` header, else `fallback`)
  - `salary.Salary` (Pydantic, §8.10 stored form: `min`, `max`, `currency`, `period`, `kind`, `raw`, `annual_eur_min`, `annual_eur_max`); `salary.parse_salary(text: str, *, kind: Literal["stated", "predicted"] = "stated") -> Salary | None`; `salary.annualise(amount: float, period: str) -> float`; `salary.from_bounds(min_: float | None, max_: float | None, *, currency: str, period: str, kind, raw: str | None) -> Salary | None`
  - `employment.EmploymentType = Literal["full_time", "part_time", "contract", "temporary", "internship", "working_student", "apprenticeship", "freelance"]`; `employment.JSEARCH_TYPES: Final[Mapping[str, EmploymentType]]` (`FULLTIME`, `PARTTIME`, `CONTRACTOR`, `INTERN`); `employment.parse_employment(text: str) -> list[EmploymentType]` (`Vollzeit`/`Full-time`, `Teilzeit`/`Part-time`, `Vertrag`/`Contractor`, `Praktikum`/`Internship`, plus `Werkstudent`, `Ausbildung`, `befristet`/`temporary`, `freelance`/`freiberuflich`); `employment.adzuna_employment(contract_time: str | None, contract_type: str | None) -> list[EmploymentType]`
  - `@dataclass(frozen=True) class extensions.Extensions: posted: ParsedDate | None; work_modes: frozenset[str] | None; employment: tuple[EmploymentType, ...]; salary: Salary | None`; `extensions.parse_extensions(items: Sequence[str], detected: Mapping[str, object] | None, *, reference: datetime) -> Extensions` (`Homeoffice`, `Home Office`, `Work from home`, `Remote` → `{remote, hybrid}`)

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_dates.py
from datetime import UTC, datetime
import pytest
from jobhunter.sources.parsing.dates import jsearch_precision, parse_relative, response_date
REF = datetime(2026, 10, 16, 9, 47, tzinfo=UTC)
@pytest.mark.parametrize(("text", "precision", "hours_back"), [("vor 15 Stunden", "hour", 15), ("15 hours ago", "hour", 15), ("vor 1 Stunde", "hour", 1)])
def test_hours(text, precision, hours_back) -> None:
    d = parse_relative(text, reference=REF); assert d is not None and (d.precision, (REF - d.at).total_seconds() / 3600) == (precision, hours_back)
@pytest.mark.parametrize(("text", "day"), [("vor 2 Tagen", 14), ("2 days ago", 14), ("vor 1 Tag", 15), ("1 day ago", 15), ("Gestern", 15), ("yesterday", 15), ("Heute", 16), ("today", 16), ("just posted", 16)])
def test_days_resolve_to_berlin_midnight(text, day) -> None:
    d = parse_relative(text, reference=REF)  # 11:47 in Berlin (CEST): the local day starts at 22:00 UTC the evening before
    assert d is not None and d.precision == "relative_day" and d.at == datetime(2026, 10, day - 1, 22, 0, tzinfo=UTC)
def test_relative_day_boundaries_follow_europe_berlin() -> None:  # Review Focus 1
    cest_night = datetime(2026, 10, 24, 22, 30, tzinfo=UTC)  # 00:30 on 25 Oct in Berlin (CEST): "Gestern" is 24 Oct
    assert parse_relative("Gestern", reference=cest_night).at == datetime(2026, 10, 23, 22, 0, tzinfo=UTC)
    assert parse_relative("Heute", reference=cest_night).at == datetime(2026, 10, 24, 22, 0, tzinfo=UTC)
    cet_morning = datetime(2026, 10, 26, 0, 0, tzinfo=UTC)  # 01:00 on 26 Oct in Berlin (CET, after the change): "vor 2 Tagen" is 24 Oct, still a CEST day
    assert parse_relative("vor 2 Tagen", reference=cet_morning).at == datetime(2026, 10, 23, 22, 0, tzinfo=UTC)
def test_precision_and_date_header() -> None:
    assert (jsearch_precision("vor 15 Stunden"), jsearch_precision("vor 2 Tagen"), jsearch_precision("2 hours ago")) == ("hour", "day", "hour")
    assert response_date({"date": "Fri, 16 Oct 2026 09:47:00 GMT"}, REF) == REF and response_date({}, REF) == REF
    assert parse_relative("irgendwann", reference=REF) is None
```

```python
# tests/unit/test_salary.py
from jobhunter.sources.parsing.salary import annualise, parse_salary
def test_german_salary_strings() -> None:
    s = parse_salary("60.000 € bis 80.000 € pro Jahr")
    assert (s.min, s.max, s.currency, s.period, s.kind, s.annual_eur_min, s.annual_eur_max) == (60000, 80000, "EUR", "year", "stated", 60000, 80000)
    assert parse_salary("5.000 € pro Monat").annual_eur_min == 60000 and parse_salary("25 € pro Stunde").annual_eur_min == 43000
    assert parse_salary("60K – 70K EUR a year").max == 70000 and parse_salary("60 Tsd. € jährlich").min == 60000
    assert parse_salary("$80,000 per year").annual_eur_min is None and parse_salary("Gehalt nach Vereinbarung") is None
    assert (annualise(1, "week"), annualise(1, "day"), annualise(1, "hour")) == (46, 220, 1720)
```

```python
# tests/unit/test_html_quality_language.py
from jobhunter.normalize.html import html_to_text, sanitize_html
from jobhunter.normalize.language import detect_language
from jobhunter.normalize.quality import description_quality
def test_sanitiser_allowlist() -> None:
    out = sanitize_html('<p onclick="x">Hi <script>evil()</script><a href="javascript:1">j</a><a href="https://e.x">ok</a></p><h1>t</h1>')
    assert "<script" not in out and "onclick" not in out and "<h1" not in out
    assert 'href="https://e.x"' in out and 'rel="noopener noreferrer nofollow"' in out and 'target="_blank"' in out and "javascript" not in out
    assert html_to_text("<p>a&amp;b</p><ul><li>x</li></ul>") == "a&b\nx"
def test_quality_gate() -> None:
    assert description_quality("x" * 56) == "garbage" and description_quality("Wir suchen " * 15) == "short"
    assert description_quality("ok text here\nnull\nnull\nnull\n" + "word " * 60) == "garbage" and description_quality("#" * 300) == "garbage"
    assert description_quality("Wir suchen eine erfahrene Entwicklerin. " * 10) == "ok"
def test_language_detection() -> None:
    de = "Wir suchen für unser Team in Berlin eine erfahrene Entwicklerin, die mit uns die Zukunft gestaltet und nicht nur auf dem Papier gut ist. " * 3
    en = "We are looking for an experienced engineer who will join our team in Berlin and help us build the future of our product with the team. " * 3
    assert (detect_language(de), detect_language(en), detect_language("kurz"), detect_language(de + en)) == ("de", "en", "unknown", "unknown")
```

```python
# tests/unit/test_employment_extensions.py
from datetime import UTC, datetime
from jobhunter.sources.parsing.employment import adzuna_employment, parse_employment
from jobhunter.sources.parsing.extensions import parse_extensions
def test_employment_strings() -> None:
    assert parse_employment("Vollzeit und Teilzeit") == ["full_time", "part_time"] and parse_employment("Praktikum") == ["internship"]
    assert adzuna_employment("full_time", "permanent") == ["full_time"] and adzuna_employment(None, "contract") == ["contract"]
def test_serpapi_extensions_bilingual() -> None:
    ref = datetime(2026, 10, 16, 9, 47, tzinfo=UTC)
    e = parse_extensions(["vor 4 Tagen", "Vollzeit", "Homeoffice", "60.000 € bis 80.000 € pro Jahr"], None, reference=ref)
    assert (e.posted.precision, e.work_modes, e.employment, e.salary.max) == ("relative_day", frozenset({"remote", "hybrid"}), ("full_time",), 80000)
    d = parse_extensions(["3 days ago", "Full-time"], {"posted_at": "3 days ago", "schedule_type": "Full-time", "work_from_home": True}, reference=ref)
    assert d.work_modes == frozenset({"remote", "hybrid"}) and d.employment == ("full_time",)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_dates.py tests/unit/test_salary.py tests/unit/test_html_quality_language.py tests/unit/test_employment_extensions.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'jobhunter.sources'`

- [ ] **Step 3: Implement the modules**

- `dates.parse_relative`: the reference is converted to `BERLIN`; a day form takes that local date minus `n` days at local midnight, converted back to UTC; an hour form subtracts `n` hours from the reference. `response_date` parses RFC 7231 `Date` with `email.utils.parsedate_to_datetime`.
- `salary.parse_salary`: numbers accept `.` thousands and `,` decimals, `K`, `Tsd.`, NBSP; ranges joined by `bis`, `–`, `-`, `to`; periods `pro Jahr`/`jährlich`/`a year`/`per year`, `pro Monat`/`monatlich`/`a month`, `pro Stunde`/`an hour`, `pro Woche`; currency `€`/`EUR` only gives the annualised EUR values, any other currency leaves them `None`; no period → `year`.
- `language.detect_language`: tokens are `fold`ed words; the data files hold one stop word per line (top 200 each, from the stop-word lists of PostgreSQL's `german.stop` and `english.stop`, trimmed to 200 by frequency).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock backend/src/jobhunter/normalize backend/src/jobhunter/sources tests
git commit -m "feat(m1): HTML sanitisation, quality gate, language detection, bilingual dates, salaries, employment types, SerpApi extensions"
```

---

### Task 4: Gazetteer and location resolution

**Files:**
- Create: `scripts/build_gazetteer.py`, `backend/src/jobhunter/geo/__init__.py`, `geo/gazetteer.py`, `geo/data/de_places.tsv`, `geo/data/non_de_places.tsv`, `geo/data/country_names_de.tsv`, `geo/data/ATTRIBUTION.md`, `backend/src/jobhunter/sources/parsing/locations.py`
- Test: `tests/unit/test_gazetteer.py`, `tests/unit/test_locations.py`

**Interfaces:**
- Consumes: `fold` (Task 2).
- Produces:
  - `@dataclass(frozen=True) class gazetteer.Place: gazetteer_id: str` (`f"geonames:{geonameid}"`)`; name: str; folded: str; alt_folded: tuple[str, ...]; country_code: str; admin1: str; lat: float; lon: float; population: int; postal_codes: tuple[str, ...]`
  - `@dataclass(frozen=True) class gazetteer.NonDePlace: name: str; folded: str; country_code: str; kind: Literal["city", "country"]`
  - `class gazetteer.Gazetteer`: `@classmethod load() -> Gazetteer` (cached; reads the packaged TSVs through `importlib.resources`); `by_name(folded: str) -> list[Place]` (exact match on `folded` or any `alt_folded`); `by_postal_code(code: str) -> list[Place]`; `search(q: str, limit: int = 10) -> list[Place]` (prefix on folded names and postal codes, by population; the §14.2 `/api/geo/places` source, M4); `non_de(folded: str) -> NonDePlace | None`; `is_german_place_name(folded: str) -> bool`
  - `gazetteer.distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float` (haversine)
  - `locations.LocationKind = Literal["city", "multi", "country_wide", "remote", "unknown"]`; `locations.Resolution = Literal["unique", "ambiguous", "unresolved"]`; `class locations.Candidate(BaseModel): gazetteer_id: str; country_code: str; lat: float; lon: float`; `class locations.Location(BaseModel): label: str; resolution: Resolution; candidates: list[Candidate]; postal_code: str | None; source: str`; `@dataclass(frozen=True) class locations.ParsedLocations: locations: list[Location]; kind: LocationKind; positively_non_de: bool`
  - `locations.parse_location_text(text: str, *, source: str) -> ParsedLocations` (§8.12 steps 1–4 and the §5.3.4 non-German rule); `locations.parse_jsearch_location(job_location: str | None, job_city: str | None, job_country: str | None) -> ParsedLocations` (§5.3.1: split on `•`, drop `über X`/`via X`, `Beliebiger Ort`/`Anywhere` → `country_wide`); `locations.location_from_coordinates(label: str, lat: float, lon: float, *, source: str) -> Location`; `locations.positively_outside_germany(locations: Sequence[Location]) -> bool` (every location resolved with every candidate outside `DE`, at least one location)
  - `scripts/build_gazetteer.py`: downloads `https://download.geonames.org/export/dump/DE.zip`, `…/export/zip/DE.zip`, `…/export/dump/cities15000.zip` and `…/export/dump/countryInfo.txt` into a temporary directory, writes the three TSVs (DE: feature class `P`, population ≥ 1,000; a postal code maps to the places with the same folded name in the same admin1 code, else to the nearest DE place within 10 km; non-DE: European cities with population > 100,000 by `countryInfo.txt` continent `EU` excluding `DE`, plus every country name in EN from `countryInfo.txt` and in DE from the hand-maintained `country_names_de.tsv`), and prints the row counts. The TSV columns are the `Place` fields in order; `alt_folded` and `postal_codes` are `|`-joined.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_gazetteer.py
from jobhunter.geo.gazetteer import Gazetteer, distance_km
def test_lookups_and_sizes() -> None:
    g = Gazetteer.load()
    berlin = g.by_name("berlin"); assert len(berlin) == 1 and berlin[0].country_code == "DE" and berlin[0].population > 3_000_000
    assert [p.name for p in g.by_postal_code("10115")] == ["Berlin"] and len(g.by_name("neustadt")) > 3
    assert {p.admin1 for p in g.by_name("frankfurt")} >= {"05", "12"}  # am Main (Hessen) and (Oder) (Brandenburg)
    assert g.by_name("monchengladbach") == g.by_name("moenchengladbach") and g.by_name("monchengladbach")[0].name == "Mönchengladbach"
    assert g.non_de("zurich").country_code == "CH" and g.non_de("schweiz").kind == "country" and g.non_de("london").kind == "city"
    assert g.non_de("berlin") is None and 8_000 <= sum(1 for _ in g.search("", limit=100_000)) <= 20_000
    assert 3.0 < distance_km(52.52, 13.405, 52.5, 13.35) < 5.0
```

```python
# tests/unit/test_locations.py
from jobhunter.sources.parsing.locations import parse_jsearch_location, parse_location_text, positively_outside_germany
def test_resolution_keeps_every_candidate() -> None:
    p = parse_location_text("Neustadt", source="x"); assert p.locations[0].resolution == "ambiguous" and len(p.locations[0].candidates) > 3
    assert parse_location_text("10115 Berlin", source="x").locations[0].resolution == "unique"
    u = parse_location_text("Hauptsitz Nord", source="x"); assert u.locations[0].resolution == "unresolved" and not u.positively_non_de
def test_markers_and_multi() -> None:
    assert parse_location_text("Deutschlandweit", source="x").kind == "country_wide" and parse_location_text("Remote", source="x").kind == "remote"
    m = parse_location_text("Berlin; Hamburg / München", source="x"); assert m.kind == "multi" and [l.label for l in m.locations] == ["Berlin", "Hamburg", "München"]
def test_jsearch_location_strings() -> None:
    assert parse_jsearch_location("Berlin     •  über Stepstone", None, None).locations[0].label == "Berlin"
    assert parse_jsearch_location("Beliebiger Ort   •  über LinkedIn", None, None).kind == "country_wide"
    assert parse_jsearch_location(None, "München", "DE").locations[0].candidates[0].country_code == "DE"
def test_positive_non_de_evidence_only() -> None:
    assert positively_outside_germany(parse_location_text("Zürich", source="x").locations) is True
    assert positively_outside_germany(parse_location_text("London or Berlin", source="x").locations) is False
    assert positively_outside_germany(parse_location_text("", source="x").locations) is False and parse_location_text("Wien", source="x").positively_non_de
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_gazetteer.py tests/unit/test_locations.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'jobhunter.geo'`

- [ ] **Step 3: Build the data and implement the modules**

- Run `uv run python scripts/build_gazetteer.py` once; commit the TSVs and `ATTRIBUTION.md` ("Contains data from GeoNames (geonames.org), CC BY 4.0", the download date and the filters). The build is deterministic for a given download; it is not run by CI.
- `parse_location_text`: step 1 strips a trailing ` • über X`, ` via X`, ` (X)` publisher suffix; step 2 splits on `;`, `/`, ` und `, ` or `; step 3 resolves each part (postal code → coordinates → folded name unique → ambiguous → unresolved); step 4 marks `remote`/`homeoffice`/`home office` as `remote` and `deutschlandweit`/`beliebiger ort`/`anywhere` as `country_wide`; `positively_non_de` is true only when every part matches a non-DE place or country name and no part has a German candidate.
- A part that is a country name in `non_de` (`Schweiz`, `Austria`) is `unresolved` with `positively_non_de`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/build_gazetteer.py backend/src/jobhunter/geo backend/src/jobhunter/sources/parsing/locations.py tests
git commit -m "feat(m1): GeoNames gazetteer (DE places, European cities, country names) and the location parser"
```

---
### Task 5: Adapter protocol and models, HTTP layer, token buckets, the persisted breaker, instances, the fixture transport and the run settings view

**Files:**
- Create: `backend/src/jobhunter/sources/base.py`, `sources/http.py`, `sources/ratelimit.py`, `sources/breaker.py`, `sources/instances.py`, `sources/fixtures.py`, `backend/src/jobhunter/run_settings.py`
- Modify: `backend/src/jobhunter/config.py` (`fake_sources`, `fixtures_dir`, `embedder_url`, `http_user_agent`), `pyproject.toml` (`httpx==0.28.1` runtime, `respx==0.23.1` dev), `tests/support/pg.py` (`server_env` sets `JOBHUNTER_FAKE_SOURCES=1` and `JOBHUNTER_FIXTURES_DIR` for the worker role)
- Test: `tests/unit/test_sources_base.py`, `tests/unit/test_http.py`, `tests/unit/test_ratelimit.py`, `tests/unit/test_fixture_transport.py`, `tests/unit/test_config.py` (append), `tests/integration/test_breaker.py`, `tests/integration/test_run_settings.py`

**Interfaces:**
- Consumes: Tasks 2–4 models and parsers; `DomainError`, `raise_alert`, `resolve_alert`, `types.get_*`.
- Produces:
  - `AppConfig` gains `fake_sources: bool` (`JOBHUNTER_FAKE_SOURCES` set to anything), `fixtures_dir: Path` (`JOBHUNTER_FIXTURES_DIR`, default `tests/contract/fixtures` resolved from the current directory), `embedder_url: str` (`JOBHUNTER_EMBEDDER_URL`, default `http://embedder:8080`), `http_user_agent: str` (`JOBHUNTER_HTTP_USER_AGENT`, default `JobHunter/1.0 (personal job search)`).
  - `base.Billing = Literal["paid", "quota", "free"]`; `class base.Continuation(Enum): CURSOR, END, UNKNOWN`; `@dataclass(frozen=True) class base.RateLimit: key: str; per_second: float; concurrency: int`; `@dataclass(frozen=True) class base.Capabilities` (the §5.1 fields); `@dataclass(frozen=True) class base.RequestSpec` (the §5.1 fields; `params: Mapping[str, str]` sorted) with `to_json() -> dict[str, JSONValue]` and `@classmethod from_json(doc) -> RequestSpec`; `base.request_key(spec: RequestSpec, run_id: int) -> bytes` (the §5.1 formula over `seed.canonical_json`, `None` where a field is unset); `@dataclass(frozen=True) class base.RawPage: spec: RequestSpec; status: int; headers: Mapping[str, str]; body: bytes; fetched_at: datetime` with `json() -> JSONValue` (cached; raises `ContractViolation("not_json")`) and `text() -> str`
  - `base.SourceErrorKind = Literal["transient", "rate_limited", "auth", "quota_exhausted", "contract", "not_found", "client", "ambiguous"]`; `class base.SourceError(DomainError)` with `__init__(kind: SourceErrorKind, *, code: str, status: int | None = None, before_send: bool = False)` (`before_send`: a connect error raised before any byte was written); `class base.ContractViolation(SourceError)` with `__init__(code: str, *, status: int | None = None)` (`kind="contract"`, `code` names the check)
  - `class base.NormalizedPosting(BaseModel)` with the §5.1 fields (`native_id`, `title_raw`, `company_raw`, `locations: list[Location]`, `location_kind`, `work_mode_possible: frozenset[str]`, `work_mode_source: Literal["structured", "text", "none"]`, `remote_countries: tuple[str, ...]`, `employment_types: tuple[EmploymentType, ...]`, `employment_source`, `salary: Salary | None`, `posted_at: datetime | None`, `posted_precision: Precision`, `posted_raw: str | None`, `item_ts: datetime | None`, `description_text: str`, `description_html_sanitized: str | None`, `description_completeness: Literal["full", "partial"]`, `description_quality: Quality`, `ad_language_structured: str | None`, `primary_link: str | None` (canonical), `outbound_url_private: str | None`, `publisher: str | None`, `raw_keys: tuple[AdKey, ...]`, `requisition_id: str | None`, `source_structured: dict[str, JSONValue]`); `model_config = ConfigDict(frozen=True)`
  - Item outcomes: `@dataclass(frozen=True) class base.Parsed: item_index: int; native_id: str; item_ts: datetime | None; normalized: NormalizedPosting`; `OutOfScope(item_index, native_id, item_ts, reason: str)`; `DuplicateInPage(item_index, native_id, item_ts, first_index: int)`; `Rejected(item_index, native_id: str | None, item_ts, reason_code: str, detail: str)`; `base.ItemOutcome = Parsed | OutOfScope | DuplicateInPage | Rejected`
  - `@dataclass(frozen=True) class base.Observation: observed_at: datetime; status: int | None; reset_at: datetime | None = None; limit: int | None = None; remaining: int | None = None; this_hour: int | None = None; hourly_cap: int | None = None`
  - `base.Settlement = tuple[Literal["billed", "not_billed", "ambiguous"], str | None]` (state, evidence)
  - `base.CoverageWindow = tuple[datetime, datetime]`; `@dataclass(frozen=True) class base.CellPlan: cell_id: int; source_id: str; instance_id: int; query_text: str | None; query_lang: str | None; location_key: str | None; location_label: str | None; radius_km: int | None; phase: Literal["primary", "fallback"]; request_class: Literal["normal", "retry", "probe", "free"]; bucket: Literal["today", "3days", "week", "month"] | None; window: CoverageWindow; max_days_old: int | None` (what an adapter needs to build its first page; the planner of Task 13 builds it from its DB-level `CellRow`); `class base.SourceAdapter(Protocol)`: the §5.1 methods (`capabilities`, `plan(cell: CellPlan) -> RequestSpec` for the first page of a traversal, with `cell.window` copied into `RequestSpec.window` so a resumed attempt reads the window from the planned row and never recomputes it; `fetch`, `check_envelope`, `peek_native_ids`, `parse`, `continuation`), plus `settle(outcome: HttpOutcome) -> Settlement` and `observation(page: RawPage) -> Observation | None` (both no-ops for free sources) and `next_page(spec: RequestSpec, cursor: str | None) -> RequestSpec | None`
  - `http.Timeouts` consts per §5.2; `http.MAX_BODY_BYTES: Final = 64 * 1024 * 1024`; `http.ERROR_BODY_BYTES: Final = 4096`; `http.STORED_HEADERS: Final` (the §5.2 allowlist); `http.configure(cfg: AppConfig) -> None` (process-wide: the UA, and the fixture transport when `cfg.fake_sources`); `http.make_client(source_id: str, *, max_body_bytes: int = MAX_BODY_BYTES) -> httpx.AsyncClient` (timeouts by source, redirects off, the Personio family handled in `send`); `@dataclass(frozen=True) class http.HttpOutcome: page: RawPage | None; error: SourceError | None`; `async def http.send(client, spec: RequestSpec, request: httpx.Request, *, now: Callable[[], datetime], deadline_s: float = 90.0) -> HttpOutcome` (one attempt; a `ConnectError` is `SourceError(transient, before_send=True)`, a timeout or a lost response `SourceError(ambiguous)`, a body over the cap `ContractViolation("body_too_large")` with the first 4 KiB kept, a 3xx a page unless the Personio exception applies; headers filtered to the allowlist, lower-cased)
  - `class ratelimit.RateLimiter`: `__init__(limits: Iterable[RateLimit], *, clock: Callable[[], float], sleep: Callable[[float], Awaitable[None]])`; `async def acquire(key: str) -> AsyncContextManager[None]` (token bucket refilled continuously plus a semaphore of `concurrency`); `ratelimit.SOURCE_LIMITS: Final[Mapping[str, RateLimit]]` and `ratelimit.ATS_HOST_LIMIT: Final = RateLimit("ats-host", 5.0, 2)`, `ratelimit.SMARTRECRUITERS_DETAIL: Final = RateLimit("smartrecruiters-detail", 5.0, 4)`
  - `breaker.Health = Literal[...]` (the seven §1.6 states); `breaker.FailureKind = Literal["transient", "rate_limited", "client", "auth", "contract", "not_found"]`; `breaker.PAID_THRESHOLD: Final = 2`, `FREE_THRESHOLD: Final = 5`, `HALF_OPEN_AFTER: Final = timedelta(hours=6)`; `async def breaker.record_failure(conn, instance_id: int, kind: FailureKind, *, billing: Billing, now: datetime) -> Health` (raises the §17.6 alerts `source.breaker_open` warn, `source.auth_failed` critical, `source.contract_broken` critical, `source.not_found` warn, subject `instance:<id>`); `async def breaker.record_success(conn, instance_id, *, now) -> Health` (`ok`, `consecutive_failures = 0`, `last_success_at`, resolves the four alerts); `breaker.available(instance: InstanceRow, now: datetime) -> Literal["available", "half_open", "blocked"]` (`blocked` is skipped at planning; `half_open` admits one probe)
  - `@dataclass(frozen=True) class instances.InstanceRow: id: int; source_id: str; instance_key: str; watchlist_company_id: int | None; health: Health; half_open_after: datetime | None; board_slug: str | None; lever_region: str | None; display_name: str | None`; `async def instances.list_instances(conn) -> list[InstanceRow]`; `async def instances.disable(conn, instance_id: int, *, code: str) -> None` (`health = 'disabled'`, `last_error_code = code`, `breaker_opened_at = NULL`, `half_open_after = NULL`, `consecutive_failures = 0`); `async def instances.sync_instances(conn, cfg: AppConfig) -> dict[str, Health]` (Deviation 6, a behaviour change: a paid or quota default instance whose secret file is absent is `disable`d with `code = 'missing_secret'`; an instance is restored to `ok` **only** from `disabled` with `last_error_code = 'missing_secret'` once its secret is present; every other state, an administrative `disabled` without that code and every breaker state included, is left untouched; in fake mode no secret is required and nothing is disabled); `async def instances.instance_for_company(conn, company_id: int) -> int` (`instance_key = board_slug`, or `f"{lever_region}:{board_slug}"` for Lever; `ON CONFLICT (source_id, instance_key)` returns the existing id)
  - `fixtures.fixture_transport(fixtures_dir: Path, *, log_path: Path | None = None) -> httpx.MockTransport` and `fixtures.ROUTES: Final[tuple[fixtures.Route, ...]]` (`Route(host: str, path: re.Pattern[str], query: Mapping[str, str | None], file: str)`, first match wins; the response body is `<file>.json`/`.xml`, status and headers from `<file>.headers.json`; no match → 404 `{"error": "no fixture"}`); with `log_path` (from `JOBHUNTER_FIXTURE_LOG`, test only) every served request appends one JSON line `{"pid", "method", "host", "path", "query", "case", "status"}` (`O_APPEND`, one `write` per line), the instrument the crash tests use to count real sends across worker processes
  - `run_settings.ScopeLocation(label, gazetteer_id, lat, lon, radius_km, key)` where `key = slug(label)` (`fold`, non-alphanumerics to `-`, collapsed); `run_settings.SeniorityRange(mode, below, above, min, max)`; `run_settings.ScopeSettings` (`locations`, `remote_within_germany`, `work_modes`, `employment_types`, `seniority_range`, `industries_exclude`, `accepted_ad_languages`, `excluded_companies` as company keys, `excluded_keywords`, `exclude_keywords_in_description`, `max_ad_age_days`); `run_settings.SourcesEnabled(jsearch, serpapi, adzuna, arbeitnow, ats)`; `run_settings.QuotaSettings(jsearch_monthly_limit, serpapi_monthly_limit, reserve_pct, serpapi_hourly_cap, adzuna_cap_minute, adzuna_cap_day, adzuna_cap_week, adzuna_cap_month, paid_max_requests_per_run: int | Literal["auto"])`; `@dataclass(frozen=True) class run_settings.RunSettings: settings_version_id: int; scope_version_id: int; scope: ScopeSettings; sources: SourcesEnabled; quota: QuotaSettings; queries: Mapping[int, bool]; watchlist: Mapping[int, bool]; stale_ttl_days: int; interval_s: int; schedule_include_paid: bool`; `async def run_settings.load_run_settings(conn, settings_version_id: int) -> RunSettings` (`interval_s` from `schedule_state`; `stale_ttl_days` `null` → `max_ad_age_days`); `run_settings.scope_from_payload(payload: Mapping[str, JSONValue]) -> ScopeSettings`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_sources_base.py
from jobhunter.sources.base import RequestSpec, request_key
SPEC = RequestSpec(source_instance_id=1, operation="list", cell_id=7, page_no=1, params={"query": "x in Berlin, Germany", "country": "de"},
                   cursor_in=None, detail_native_id=None, parent_fetch_request_id=None, window=None, request_class="normal")
def test_request_key_is_stable_and_cursor_sensitive() -> None:
    assert request_key(SPEC, 42) == request_key(RequestSpec.from_json(SPEC.to_json()), 42) and len(request_key(SPEC, 42)) == 32
    assert request_key(SPEC, 42) != request_key(SPEC, 43) and request_key(SPEC, 42) != request_key(RequestSpec(**{**SPEC.__dict__, "cursor_in": "c"}), 42)
    assert "x-rapidapi-key" not in str(SPEC.to_json()) and SPEC.to_json()["params"] == {"country": "de", "query": "x in Berlin, Germany"}
```

```python
# tests/unit/test_http.py
import httpx, pytest, respx
from datetime import UTC, datetime
from jobhunter.sources.base import ContractViolation, SourceError
from jobhunter.sources.http import make_client, send
from tests.unit.test_sources_base import SPEC
NOW = lambda: datetime(2026, 10, 16, 9, 47, tzinfo=UTC)
def test_timeouts_user_agent_and_no_redirects() -> None:
    c, a = make_client("jsearch"), make_client("arbeitnow")
    assert (c.timeout.connect, c.timeout.read, c.timeout.write, c.timeout.pool) == (5.0, 30.0, 10.0, 5.0) and a.timeout.read == 60.0
    assert c.headers["user-agent"] == "JobHunter/1.0 (personal job search)" and c.follow_redirects is False
@respx.mock
async def test_send_classifies_transport_outcomes() -> None:
    respx.get("https://api.example/ok").mock(return_value=httpx.Response(200, json={"a": 1}, headers={"Set-Cookie": "s=1", "X-RateLimit-Requests-Remaining": "199"}))
    respx.get("https://api.example/conn").mock(side_effect=httpx.ConnectError("refused"))
    respx.get("https://api.example/slow").mock(side_effect=httpx.ReadTimeout("slow"))
    respx.get("https://api.example/moved").mock(return_value=httpx.Response(301, headers={"Location": "https://elsewhere"}))
    async with make_client("jsearch") as c:
        ok = await send(c, SPEC, c.build_request("GET", "https://api.example/ok"), now=NOW)
        assert ok.page is not None and ok.page.headers == {"x-ratelimit-requests-remaining": "199", "content-type": "application/json"} and ok.page.json() == {"a": 1}
        conn = await send(c, SPEC, c.build_request("GET", "https://api.example/conn"), now=NOW)
        assert conn.error is not None and (conn.error.kind, conn.error.before_send) == ("transient", True)
        slow = await send(c, SPEC, c.build_request("GET", "https://api.example/slow"), now=NOW)
        assert slow.error is not None and (slow.error.kind, slow.error.before_send) == ("ambiguous", False)
        assert (await send(c, SPEC, c.build_request("GET", "https://api.example/moved"), now=NOW)).page.status == 301
@respx.mock
async def test_html_body_and_oversized_body_are_contract_failures() -> None:  # Review Focus 3
    respx.get("https://api.example/html").mock(return_value=httpx.Response(200, text="<html><body>Service Unavailable</body></html>", headers={"content-type": "text/html"}))
    respx.get("https://api.example/big").mock(return_value=httpx.Response(200, content=b"x" * 10_000))
    async with make_client("jsearch", max_body_bytes=8_000) as c:
        html = await send(c, SPEC, c.build_request("GET", "https://api.example/html"), now=NOW)
        with pytest.raises(ContractViolation, match="not_json"): html.page.json()
        big = await send(c, SPEC, c.build_request("GET", "https://api.example/big"), now=NOW)
        assert isinstance(big.error, ContractViolation) and big.error.code == "body_too_large" and big.page is not None and len(big.page.body) == 4096
```

```python
# tests/unit/test_ratelimit.py
import asyncio
from jobhunter.sources.base import RateLimit
from jobhunter.sources.ratelimit import RateLimiter
from tests.support.clock import FakeClock
async def test_bucket_waits_and_concurrency_caps() -> None:
    clock, slept = FakeClock(0.0), []
    async def sleep(s: float) -> None: slept.append(s); clock.now += s
    rl = RateLimiter([RateLimit("jsearch", 1.0, 1)], clock=clock, sleep=sleep)
    async with rl.acquire("jsearch"): pass
    async with rl.acquire("jsearch"): pass
    assert slept == [1.0]
    entered, gate = 0, asyncio.Event()
    async def hold() -> None:
        nonlocal entered
        async with rl.acquire("jsearch"): entered += 1; await gate.wait()
    t1 = asyncio.create_task(hold()); await asyncio.sleep(0); t2 = asyncio.create_task(hold()); await asyncio.sleep(0)
    assert entered == 1; gate.set(); await asyncio.gather(t1, t2); assert entered == 2
```

```python
# tests/unit/test_fixture_transport.py
import httpx, json
from jobhunter.sources.fixtures import fixture_transport
async def test_routes_by_host_path_and_query(tmp_path) -> None:
    (tmp_path / "jsearch").mkdir()
    (tmp_path / "jsearch/search_v2_page1.json").write_text('{"status":"OK","data":[]}'); (tmp_path / "jsearch/search_v2_page1.headers.json").write_text(json.dumps({"status": 200, "headers": {"x-ratelimit-requests-remaining": "199"}}))
    (tmp_path / "jsearch/search_legacy_404.json").write_text('{"message":"Endpoint \'/search\' does not exist"}'); (tmp_path / "jsearch/search_legacy_404.headers.json").write_text(json.dumps({"status": 404, "headers": {"x-rapidapi-proxy-response": "true"}}))
    async with httpx.AsyncClient(transport=fixture_transport(tmp_path)) as c:
        ok = await c.get("https://jsearch.p.rapidapi.com/search-v2?query=x&country=de")
        assert (ok.status_code, ok.headers["x-ratelimit-requests-remaining"], ok.json()["status"]) == (200, "199", "OK")
        assert (await c.get("https://jsearch.p.rapidapi.com/search?query=x")).status_code == 404
        assert (await c.get("https://nowhere.example/x")).status_code == 404
```

```python
# tests/integration/test_breaker.py
from datetime import timedelta
import pytest, sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.sources.breaker import HALF_OPEN_AFTER, available, record_failure, record_success
from jobhunter.sources.instances import list_instances
pytestmark = pytest.mark.integration
async def instance(engine, source_id: str):
    async with engine.connect() as c: return next(i for i in await list_instances(c) if i.source_id == source_id)
async def open_alerts(engine) -> set[str]:
    async with engine.connect() as c: return set((await c.execute(sa.text("SELECT code FROM alerts WHERE resolved_at IS NULL"))).scalars())
async def test_thresholds_half_open_and_alerts(engine) -> None:
    now, js, an = utcnow(), await instance(engine, "jsearch"), await instance(engine, "arbeitnow")
    async with engine.begin() as c:
        assert await record_failure(c, js.id, "transient", billing="paid", now=now) == "degraded"
        assert await record_failure(c, js.id, "transient", billing="paid", now=now) == "breaker_open"
        assert [await record_failure(c, an.id, "transient", billing="free", now=now) for _ in range(5)] == ["degraded"] * 4 + ["breaker_open"]
    js = await instance(engine, "jsearch")
    assert js.half_open_after == now + HALF_OPEN_AFTER and available(js, now) == "blocked" and available(js, now + HALF_OPEN_AFTER) == "half_open"
    assert await open_alerts(engine) == {"source.breaker_open"}
    async with engine.begin() as c:
        assert await record_failure(c, js.id, "auth", billing="paid", now=now) == "auth_failed"
        assert await record_failure(c, an.id, "contract", billing="free", now=now) == "contract_broken"
    assert await open_alerts(engine) == {"source.breaker_open", "source.auth_failed", "source.contract_broken"}
    async with engine.begin() as c:
        assert await record_success(c, js.id, now=now + timedelta(hours=7)) == "ok" and await record_success(c, an.id, now=now) == "ok"
    assert await open_alerts(engine) == set() and (await instance(engine, "jsearch")).half_open_after is None
async def test_breaker_timestamps_clear_on_ok_and_disabled(engine) -> None:  # ruling (b) on Deviation 2
    now, js, az = utcnow(), await instance(engine, "jsearch"), await instance(engine, "adzuna")
    async with engine.begin() as c:
        await record_failure(c, js.id, "auth", billing="paid", now=now); await record_failure(c, az.id, "contract", billing="quota", now=now)
    async with engine.begin() as c:
        assert await record_success(c, js.id, now=now + timedelta(hours=7)) == "ok"; await disable(c, az.id, code="missing_secret")
    async with engine.connect() as c:
        rows = (await c.execute(sa.text("SELECT source_id, health, breaker_opened_at, half_open_after, consecutive_failures FROM source_instances WHERE id IN (:a, :b) ORDER BY source_id"), {"a": js.id, "b": az.id})).all()
    assert [tuple(r) for r in rows] == [("adzuna", "disabled", None, None, 0), ("jsearch", "ok", None, None, 0)]
```

```python
# tests/integration/test_run_settings.py
import pytest, sqlalchemy as sa
from jobhunter.run_settings import load_run_settings
pytestmark = pytest.mark.integration
async def test_seeded_defaults(engine) -> None:
    async with engine.connect() as c:
        sid = (await c.execute(sa.text("SELECT settings_version_id FROM active_config"))).scalar_one()
        s = await load_run_settings(c, sid)
    assert (s.scope.locations, s.scope.remote_within_germany, s.scope.max_ad_age_days, s.stale_ttl_days, s.interval_s) == ((), True, 30, 30, 21600)
    assert (s.quota.jsearch_monthly_limit, s.quota.serpapi_monthly_limit, s.quota.reserve_pct, s.quota.serpapi_hourly_cap, s.quota.paid_max_requests_per_run) == (200, 250, 10, 45, "auto")
    assert (s.quota.adzuna_cap_minute, s.quota.adzuna_cap_day, s.quota.adzuna_cap_week, s.quota.adzuna_cap_month) == (20, 225, 900, 2250)
    assert s.sources.jsearch and s.sources.ats and s.queries == {} and s.watchlist == {} and s.schedule_include_paid is True
    assert s.scope.employment_types == frozenset({"full_time", "part_time", "contract", "temporary", "internship", "working_student", "apprenticeship", "freelance"})
```

(Also appended to `tests/unit/test_config.py`: `JOBHUNTER_FAKE_SOURCES=1` gives `fake_sources is True`, the three defaults above, and `fake_mode_in_prod` still refuses it in `prod`.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_sources_base.py tests/unit/test_http.py tests/unit/test_ratelimit.py tests/unit/test_fixture_transport.py tests/integration/test_breaker.py tests/integration/test_run_settings.py -q`
Expected: FAIL with `ImportError: cannot import name 'RequestSpec' from 'jobhunter.sources.base'`

- [ ] **Step 3: Implement the modules**

- `http.send`: `client.send(request, stream=True)`; reads the body in chunks up to the cap; `response.headers` filtered to `STORED_HEADERS`; the Personio exception follows at most 3 redirects whose target host ends with `.jobs.personio.de`. The log line `source_request` carries `source_id`, the URL path and the query with the `api_key`, `app_id`, `app_key` parameters removed (the redaction processor masks the rest).
- `breaker.record_failure`: `transient`, `rate_limited` and `client` count toward the threshold (`degraded` below it); `auth`, `contract` and `not_found` open at once as their own state; every open state sets `breaker_opened_at = now` and `half_open_after = now + HALF_OPEN_AFTER` (Deviation 2). `record_success` and `instances.disable` set both timestamps to NULL (the widened CHECK requires it, and the test above pins it). `available` is `half_open` when `now ≥ half_open_after`, `blocked` before, `available` for `ok`/`degraded`, and `blocked` for `disabled`.
- `test_breaker.py` imports `disable` from `jobhunter.sources.instances`.
- `fixtures.ROUTES` is filled by Task 6 as the fixtures are recorded; this task ships the mechanism and the two JSearch routes of the test.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit tests/integration/test_breaker.py tests/integration/test_run_settings.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock backend/src/jobhunter tests
git commit -m "feat(m1): adapter protocol and models, HTTP layer with outcome classification, token buckets, persisted breaker, instance sync, fixture transport, run settings view"
```

---

### Task 6: Fixture tooling, the source spikes (V3, V8, V9, V10, V18), the scrubbed fixtures and the gazetteer spike (V17)

**Files:**
- Create: `backend/src/jobhunter/cli_fixtures.py`, `scripts/spikes/record_sources.py`, `scripts/spikes/derive_variants.py`, `tests/contract/__init__.py`, `tests/contract/conftest.py`, `tests/contract/README.md`, `tests/contract/fixtures/**`, `tests/contract/test_fixture_pii.py`, `tests/contract/test_v17_gazetteer_coverage.py`, `tests/support/sources.py`, `tests/unit/test_fixture_scrub.py`, `docs/spikes/V3.md`, `V8.md`, `V9.md`, `V10.md`, `V17.md`, `V18.md`
- Modify: `backend/src/jobhunter/cli.py` (`fixtures scrub`), `.gitignore` (`.fixtures-raw/`), `backend/src/jobhunter/sources/fixtures.py` (`ROUTES`), `pyproject.toml` (marker `contract`, `live`; `addopts = "-m 'not stack and not perf and not live'"`)

**Interfaces:**
- Consumes: `redact.EMAIL`, `redact.PHONE`, `redact.KEY`, `Gazetteer`, `parse_location_text`.
- Produces:
  - `class cli_fixtures.Pseudonyms`: deterministic per-category counters (`person`, `email`, `phone`, `user`, `token`, `key`), first appearance wins; `cli_fixtures.scrub_text(text: str, p: Pseudonyms) -> str`: e-mails → `[email:n]`, phones → `[phone:n]`, 2–3 capitalised words within 120 characters after `Ansprechpartner`, `Kontakt`, `Contact`, `Your contact` (case-insensitive) and the 2–3 capitalised words right before an e-mail → `[person:n]`, `linkedin.com/in/<x>` and `xing.com/profile/<x>` paths → `[user:n]`, every `utm_source=<x>` → `utm_source=scrubbed`, every `se=`/`v=`/`adref`/`fc`-style token and every `redact.KEY` match → `[token:n]`; `cli_fixtures.IDENTIFIER_FIELDS: Final[Mapping[str, Mapping[str, FieldRule]]]` per source, keyed by the leaf key of a JSON path, with a `FieldRule` of `keep` (`job_uid`, `htidocid`, `id`, `slug`, `requisition_id`, `internal_job_id`, numeric and UUID ids), `token` (`next_page_token`, `fc`, `se`, `v`, `adref`, `serpapi_pagination.*token*`, `search_metadata.id`, `request_id`: replaced by `[token:n]` whatever their shape), `serpapi_job_id` (base64 JSON: decode, `fc := "scrubbed"`, re-encode, `htidocid` kept) and `jsearch_job_id` (the `<job_uid>:<session blob>` form: the blob becomes `scrubbed`); `cli_fixtures.scrub_document(doc: JSONValue, source: str, p) -> JSONValue` walks the document **with its path** and is field-aware: a string under an identifier field gets its field rule and nothing else; every other string value (free text: titles, descriptions, company names, locations, links) goes through `scrub_text`, whose generic `redact.KEY` pass therefore never touches a rebuilt id; object keys are never rewritten (the wire structure stays); a URL value keeps its path and loses `utm_*`, `se`, `v`, `adref` values as `scrub_text` says; `cli_fixtures.scrub_tree(src: Path, dst: Path) -> ScrubReport` (every `.json`, `.xml` and `.headers.json`; headers keep only the §5.2 allowlist; `.meta.json` is scrubbed too); CLI `jobhunter fixtures scrub <in> <out>`
  - `scripts/spikes/record_sources.py <spike> --phase N`: reads the keys from `RAPIDAPI_KEY`, `SERPAPI_API_KEY`, `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` (never from a secret file, never printed); its accounting is crash-safe and conservative: the whole invocation holds an exclusive `fcntl.flock` on `.fixtures-raw/budget.lock` (a second invocation fails at once), and `.fixtures-raw/budget.json` (`{provider: {"sent": n, "requests": [{case, phase, state: "sending" | "done", status}]}}`) is rewritten by atomic rename **before** every send with the request counted as `sending`; after the response the entry becomes `done` with its status; a crash or a transport failure leaves it counted. It **refuses** a request when `sent + 1` would exceed `BUDGET = {"jsearch": 8, "serpapi": 1, "adzuna": 5, "arbeitnow": 6, "ats": 12}` and refuses a phase whose entries are already `done`; writes `.fixtures-raw/<source>/<case>.json` (body as received), `<case>.headers.json` (`{"status", "headers"}` allowlisted) and `<case>.meta.json` (URL without query secrets, non-secret params, `fetched_at`); logs every request as one line `source case status units_remaining`
  - `scripts/spikes/derive_variants.py <scrubbed-dir>`: writes the hand-shaped variants listed in `tests/contract/README.md` from the scrubbed recordings (JSearch `search_v2_empty` (`data: []`, no cursor), `search_v2_429` (status 429, `x-ratelimit-requests-remaining: 0`), `search_v2_drift` (`status` key removed), `search_v2_html` (an HTML body); SerpApi `google_jobs_empty` (`jobs_results` removed, `error` added), `google_jobs_drift`; Adzuna `search_empty`, `search_401`; Arbeitnow `page1_remaining5` (`x-ratelimit-remaining: 5`), `page_truncated` (the body cut mid-array); ATS `*_404`, `lever_eu_<site>` from the global recording, `greenhouse_count_mismatch` (`meta.total + 1`), `personio_entity` (a DTD with an entity, must be refused by defusedxml), `smartrecruiters_list_paged` (the trimmed list with `totalFound = 150`: the pagination rule yields `offset = 100`) and `smartrecruiters_list_offset100` (`offset = 100`, `content: []`, `totalFound = 150`: the end)), trimming Arbeitnow to its first 30 items and Greenhouse/SmartRecruiters to 15/10 items with `meta.total`/`totalFound` adjusted so a trimmed base fixture is structurally complete (ruling (e): the count-mismatch and pagination cases keep their own variants, so trimming never erases those tests; a trim removes whole items and never alters an item, so every identity relationship of the kept items survives). The JSearch variants locate the item array and the cursor through the paths recorded by V9 (`--jsearch-items-path`, `--jsearch-cursor-path`, both written into the README). The Lever full-page rule (exactly 100 items → continue with `skip`) is tested on a synthetic list of 100 minimal objects built in the test, not on a padded fixture
  - `tests/support/sources.py`: `FIXTURES: Final = Path("tests/contract/fixtures")`; `load_fixture(source: str, case: str) -> tuple[int, dict[str, str], bytes]`; `fixture_page(source, case, spec: RequestSpec, *, fetched_at) -> RawPage`; `mock_fixture(respx_mock, method, url_pattern, source, case) -> respx.Route`; `served_requests(log_path: Path) -> list[dict[str, str]]` (the fixture transport's log lines, Task 5)
  - `tests/contract/fixtures/location_labels.tsv`: one row per distinct location string of the scrubbed fixtures (`source`, `string`, `country` (ISO 3166-1 alpha-2, or `-` for remote/country-wide/unknown), `kind` (`city`, `multi`, `country_wide`, `remote`, `unknown`)), labelled by hand while the fixtures are reviewed (step 8): the independent ground truth of V17
  - `fixtures.ROUTES` for every recorded case the fake worker uses (JSearch page 1 / page 2 by `cursor` / today by `date_posted` / legacy 404; SerpApi `account.json` and `search.json`; Adzuna page 1 (`search_p1_rpp100`) and page ≥ 2 (`search_empty`); Arbeitnow `page=1`, `page=2`; the six ATS boards by host and slug, unknown slugs → `*_404`)
  - The spike records `docs/spikes/V<n>.md` in the `V1.md` form (Assumption, Method, Observed result, Decision)

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_fixture_scrub.py
import base64, json
from jobhunter.cli_fixtures import Pseudonyms, scrub_document, scrub_text
from jobhunter.observability.redact import EMAIL, KEY, PHONE
def test_scrub_rules_are_deterministic_and_leave_no_pii() -> None:
    p = Pseudonyms()
    text = ("Ansprechpartner: Max Mustermann, max.mustermann@acme.de, Tel. +49 30 1234567. Contact Jane Doe. "
            "https://www.linkedin.com/in/max-mustermann https://adzuna.de/land/ad/1?se=AbC123&utm_source=APPID99&v=XyZ "
            "key sk-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    out = scrub_text(text, p)
    assert "[person:1]" in out and "[email:1]" in out and "[phone:1]" in out and "[person:2]" in out and "[user:1]" in out
    assert "utm_source=scrubbed" in out and "se=[token:" in out and "v=[token:" in out and "sk-" not in out
    assert not EMAIL.search(out) and not PHONE.search(out) and not KEY.search(out) and scrub_text(text, Pseudonyms()) == out
    assert "[token:" in scrub_text("ref " + "a1b2c3d4" * 6, Pseudonyms())  # a 48-character hex key in free text is a key
def test_identifier_fields_are_scrubbed_by_name_not_by_shape() -> None:
    doc = {"search_metadata": {"id": "6b1e9b8c4f2a4d3e9c0b1a2f3e4d5c6b"}, "serpapi_pagination": {"next_page_token": "x" * 60},
           "jobs_results": [{"title": "Dev " + "a1b2c3d4" * 6, "job_id": "notbase64"}]}
    out = scrub_document(doc, "serpapi", Pseudonyms())
    assert out["search_metadata"]["id"].startswith("[token:") and out["serpapi_pagination"]["next_page_token"].startswith("[token:")
    assert "[token:" in out["jobs_results"][0]["title"] and set(out) == set(doc)  # keys untouched, free text scrubbed
def test_serpapi_and_jsearch_ids_keep_the_htidocid() -> None:
    job_id = base64.b64encode(json.dumps({"htidocid": "UMsYA14AEpQuse3eAAAAAA==", "fc": "SECRETSESSION"}).encode()).decode()
    doc = scrub_document({"jobs_results": [{"job_id": job_id}]}, "serpapi", Pseudonyms())
    assert json.loads(base64.b64decode(doc["jobs_results"][0]["job_id"])) == {"htidocid": "UMsYA14AEpQuse3eAAAAAA==", "fc": "scrubbed"}
    js = base64.b64encode(b"UMsYA14AEpQuse3eAAAAAA==:" + b"x" * 276).decode()
    out = scrub_document({"data": [{"job_id": js, "job_uid": "UMsYA14AEpQuse3eAAAAAA=="}]}, "jsearch", Pseudonyms())
    assert base64.b64decode(out["data"][0]["job_id"]) == b"UMsYA14AEpQuse3eAAAAAA==:scrubbed" and out["data"][0]["job_uid"] == "UMsYA14AEpQuse3eAAAAAA=="
```

```python
# tests/contract/test_fixture_pii.py
import json, os, re
from pathlib import Path
import pytest
from jobhunter.cli_fixtures import IDENTIFIER_FIELDS, validate_identifier   # validate_identifier(source, key, value) -> bool: the field's own shape check
from jobhunter.observability.redact import EMAIL, KEY, PHONE
ROOT = Path("tests/contract/fixtures")
FILES = sorted(p for p in ROOT.rglob("*") if p.suffix in (".json", ".xml", ".tsv"))   # bodies, headers, meta and the label file
UTM = re.compile(r"utm_source=([^&\"'\\s]+)")
SECRET_VARS = ("RAPIDAPI_KEY", "SERPAPI_API_KEY", "ADZUNA_APP_ID", "ADZUNA_APP_KEY")
def strings(doc, path=()):   # every (path, string value) of a JSON document
    if isinstance(doc, str): yield path, doc
    elif isinstance(doc, dict):
        for k, v in doc.items(): yield from strings(v, (*path, k))
    elif isinstance(doc, list):
        for i, v in enumerate(doc): yield from strings(v, (*path, i))
@pytest.mark.parametrize("path", FILES, ids=[str(p.relative_to(ROOT)) for p in FILES])
def test_fixture_has_no_pii_secret_or_key_shaped_value(path) -> None:
    text, source = path.read_text(encoding="utf-8"), path.relative_to(ROOT).parts[0]
    assert not EMAIL.search(text), "an e-mail address survived the scrub"
    assert not PHONE.search(text), "a phone number survived the scrub"
    assert {m.group(1) for m in UTM.finditer(text)} <= {"scrubbed"}
    for var in SECRET_VARS:   # the real keys of this machine, when present: never echoed in the message
        value = os.environ.get(var, "")
        assert len(value) < 8 or value not in text, f"the value of {var} is in the fixture"
    if path.suffix != ".json":
        assert not KEY.search(text), "a key-shaped value in a non-JSON fixture"; return
    doc = json.loads(text)
    if path.name.endswith(".headers.json"):
        assert set(doc) == {"status", "headers"} and not {k.lower() for k in doc["headers"]} & {"set-cookie", "authorization", "x-rapidapi-key", "cookie"}
    strict = [(p, v) for p, v in strings(doc) if KEY.search(v)]   # a key-shaped value is allowed only as a validated identifier field
    offending = [(p, v[:12] + "…") for p, v in strict if not (p and str(p[-1]) in IDENTIFIER_FIELDS.get(source, {}) and validate_identifier(source, str(p[-1]), v))]
    assert not offending, f"key-shaped values outside the identifier allowlist: {[p for p, _ in offending]}"
def test_fixture_tree_is_not_empty() -> None:
    assert len(FILES) >= 30 and (ROOT / "jsearch/search_v2_page1.json").exists() and (ROOT / "location_labels.tsv").exists()
```

(`validate_identifier` is strict per field: a SerpApi `job_id` must base64-decode to a JSON object whose `fc` is `scrubbed`; a JSearch `job_id` must decode to `<24-character htidocid>:scrubbed`; a `[token:n]` placeholder validates for the `token` fields; a `keep` field validates only when its value matches that field's shape (`htidocid`: 24 base64 characters, which the 40-character `KEY` floor never matches anyway). Everything else that `KEY` matches — a 48-character hexadecimal string in a description included — fails the scan.)

```python
# tests/contract/test_v17_gazetteer_coverage.py
import csv
from pathlib import Path
from jobhunter.sources.parsing.locations import parse_jsearch_location, parse_location_text
def labelled() -> list[tuple[str, str, str, object]]: ...  # (source, string, country, kind) of location_labels.tsv, each parsed with the matching parser (JSearch strings through parse_jsearch_location)
def test_labels_cover_every_fixture_location_string() -> None:
    ...  # every distinct location string found in the scrubbed fixtures has a row in location_labels.tsv (the label file cannot go stale)
def test_v17_coverage_thresholds_against_hand_labels() -> None:
    rows = labelled()
    german = [(s, p) for _, s, country, kind, p in rows if country == "DE" and kind in ("city", "multi")]   # the denominator is the label, not the parser
    resolved = [s for s, p in german if p.kind in ("city", "multi") and all(l.resolution != "unresolved" for l in p.locations)]
    assert len(german) >= 40 and len(resolved) / len(german) >= 0.95
    assert not [s for _, s, country, _, p in rows if country == "DE" and p.positively_non_de]        # no German location is ever "positively outside"
    assert not [s for _, s, country, _, p in rows if country not in ("DE", "-") and not p.positively_non_de and all(c.country_code == "DE" for l in p.locations for c in l.candidates) and p.locations]  # a foreign city resolved as only-German is a false German
    assert all(p.kind == kind for _, _, _, kind, p in rows if kind in ("remote", "country_wide"))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_fixture_scrub.py tests/contract -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'jobhunter.cli_fixtures'`; the contract tests fail on the empty fixture tree.

- [ ] **Step 3: Implement the scrubber, the recorder and the variant deriver**

`scrub_document` is one recursive walk that carries the JSON path and decides **per leaf** by the Interfaces contract: a string whose leaf key is in `IDENTIFIER_FIELDS[source]` gets that field's rule and **nothing else** — the rebuilt SerpApi `job_id` (base64 JSON, ≥ 40 characters, which M0's `redact.KEY` matches) and the rebuilt JSearch `job_id` are returned as rebuilt, never passed through `scrub_text`; every other string value goes through `scrub_text` (its generic `KEY` pass included); object keys are never touched; numbers, booleans and nulls pass through. There is no second, document-wide pass. `test_serpapi_and_jsearch_ids_keep_the_htidocid` and `test_identifier_fields_are_scrubbed_by_name_not_by_shape` pin exactly this. XML is scrubbed as text (Personio CDATA included). `scrub_tree` scrubs `.meta.json` files like any JSON document (the URL and the parameter values are free-text fields; a leaked key there would otherwise reach the repository) and `.headers.json` through the §5.2 allowlist; nothing is copied through unchanged. The recorder sends the keys as the §5.3 headers and parameters and writes nothing that contains them: the `.meta.json` URL is built from the path and the non-secret parameters only, and the PII scan's `SECRET_VARS` check is the second line of defence.

- [ ] **Step 4: Run the spikes (live, with the user's keys; each with its exact request count and stop condition)**

Each phase refuses to run twice; the budget file is the hard stop. Record the observations in `docs/spikes/V<n>.md` as they come.

1. **JSearch, V9 phase 1 (4 requests, 3 units):** `record_sources.py jsearch --phase 1`:
   (1) `GET /search?query=…` (legacy route) → expect 404 with `x-rapidapi-proxy-response: true`, case `search_legacy_404`; (2) `search-v2` `query="software engineer in Berlin, Germany"`, `country=de`, `num_pages=1`, `date_posted=week` → `search_v2_page1`, remaining `R`; (3) `date_posted=today` → `search_v2_today`; (4) the page-1 cursor, sent at once after (3) → `search_v2_page2`. Stop after request 4. Acceptance: `R` after (2) is one below the limit and the 404 did not decrement it (1 credit per page, gateway 404 not billed); every `job_posted_at_timestamp` of (3) is within 48 h of `fetched_at` (day-truncated bucket); (3) and (4) both 200 within one second (1 request/s stays as the const). The **envelope** of `search_v2_page1` is read off the recording and written into `V9.md`: the path of the item array and the path of the cursor (the spec expects `data[]` with `data.cursor`-style continuation; the recording decides). Task 9's `ITEMS_PATH`/`CURSOR_PATH` and the variant deriver's `--jsearch-items-path`/`--jsearch-cursor-path` are set from that record, so the adapter, the scrubber and the variants describe one JSON structure.
2. **JSearch, V9 phase 2 (1 request, 1 unit), at least 2 h after phase 1:** (5) the phase-1 cursor again → `search_v2_page2_reused`. Stop. Acceptance: a 200 page whose `job_uid`s overlap `search_v2_page2` means the cursor survives 2 h; an error or an unrelated page means it does not.
3. **JSearch, V10 phase 3 (2 requests, 2 units), at least 24 h after phase 1:** (6) request (2) again → `search_v2_page1_day2` (V10: ≥ 3 `job_uid` values identical with `search_v2_page1`; fewer means the identity is unstable, which is analysed and resolved before acceptance); (7) the phase-1 cursor once more → `search_v2_page2_day2` (the 24-hour reuse check). Stop after request 7. `CURSOR_REUSE_TTL` is the longest interval at which reuse was *observed* to work, never an extrapolation: `timedelta(hours=24)` when (7) is a page overlapping `search_v2_page2`, `timedelta(hours=2)` when only (5) was, `timedelta(0)` otherwise; `V9.md` records which. Total JSearch: 7 requests, 6 units; the one spare request repeats a request that failed in transport, never a new case. This phase may land in a later commit within M1; Task 25 waits for it (ruling (f)).
4. **SerpApi, V3 (1 search):** `record_sources.py serpapi`: `account.json` (free) → `account_before`; one `search.json?engine=google_jobs&q=software engineer Berlin&gl=de&hl=en` → `google_jobs_hl_en`; `account.json` → `account_after`. Stop at 1 search. Acceptance: `detected_extensions` present on the results and ≥ 3 `htidocid` identical with `search_v2_page1`; then `SERPAPI_HL = "en"`, otherwise `"de"` (and the fixture is still the contract fixture).
5. **Adzuna, V18 (4 requests, 5 allowed):** `record_sources.py adzuna`: (1) `what=software engineer`, `where=Berlin`, `results_per_page=100`, `sort_by=date` → `search_p1_rpp100`; (2) `results_per_page=50`, `max_days_old=1` → `search_p1_days1`; (3) `distance=10` → `search_p1_distance10`; (4) `distance=50` → `search_p1_distance50`. Stop at 4; the 5th only repeats a request that failed in transport. Acceptance: (1) returns exactly 50 results; every `created` of (2) is within 2 days of `fetched_at`; `count` of (4) ≥ `count` of (3).
6. **Arbeitnow, V8 (6 requests over 2 h):** `record_sources.py arbeitnow --phase k` for `k` = 1…6 at 0, 20, 40, 60, 90 and 120 min: phase 1 is `page=1` → `page1`; phase 2 is `page=2` → `page2`; phases 3–6 are `page=1` with headers only. Stop at 6. Acceptance: the sequence of `x-ratelimit-remaining` determines the window (a reset within the 2 h gives its length; none means ≥ 2 h); the page cap of §5.3.4 keeps a 7-day catch-up (`min(10, max(3, ceil(168 / 30) + 1)) = 7` pages) within the limit, otherwise the cap formula's upper bound is lowered and recorded in `V8.md`.
7. **ATS boards (≤ 10 requests, free):** `record_sources.py ats`: Greenhouse `getyourguide` and a nonexistent slug (404); Lever `leverdemo` (global) and `api.eu.lever.co/v0/postings/leverdemo` (expected 404; a working EU site may be substituted when one is known); Ashby `n8n`; Personio `everphone`; Recruitee `bunq`; SmartRecruiters `DeliveryHero` list (`limit=100&offset=0`) plus the detail of its first two postings. Stop after the list.
8. Scrub: `uv run jobhunter fixtures scrub .fixtures-raw tests/contract/fixtures`, then `uv run python scripts/spikes/derive_variants.py tests/contract/fixtures --jsearch-items-path <from V9> --jsearch-cursor-path <from V9>`, then fill `fixtures.ROUTES` and `tests/contract/README.md`: one row per file with its provenance (`recorded <spike> <date>` or `derived from <file>: <exact change>`), the trims (which items were dropped, which totals were adjusted) and the JSearch paths (ruling (e)). Review every scrubbed file by hand against the PR checklist (names near contact words, signatures, phone numbers in free text) before the commit; while reviewing, write `location_labels.tsv` by hand for every distinct location string; the raw directory is never added.
9. V17: run `tests/contract/test_v17_gazetteer_coverage.py` and record the ratios in `docs/spikes/V17.md` (resolved among hand-labelled German city strings, false-German and false-foreign counts); a ratio below 95% is fixed in the gazetteer build (missing variants) or the parser, never by relaxing the threshold or editing a label to fit the parser.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/test_fixture_scrub.py tests/contract -q`
Expected: PASS (one PII test per fixture file, and the V17 thresholds)

- [ ] **Step 6: Commit**

```bash
git add .gitignore pyproject.toml backend/src/jobhunter/cli.py backend/src/jobhunter/cli_fixtures.py backend/src/jobhunter/sources/fixtures.py scripts/spikes tests docs/spikes
git commit -m "feat(m1): fixture scrubber and PII scan, budgeted source recorder, scrubbed fixtures; spikes V3, V8, V9, V18, V17 (V10 day 2 pending)"
```

---

### Task 7: Quota windows, observations, pacing and the admission decision

**Files:**
- Create: `backend/src/jobhunter/quota/__init__.py`, `quota/windows.py`, `quota/pacing.py`, `tests/support/quota.py`
- Modify: `backend/src/jobhunter/observability/metrics.py` (`QUOTA_USED_UNITS`, `QUOTA_REMAINING_UNITS`)
- Test: `tests/unit/test_quota_windows.py`, `tests/unit/test_quota_pacing.py`, `tests/integration/test_quota_windows_db.py`

**Interfaces:**
- Consumes: `QuotaSettings` (Task 5), `Observation` (Task 5), `raise_alert`, `resolve_alert`.
- Produces:
  - `windows.Provider = Literal["jsearch", "serpapi", "adzuna"]`; `windows.WindowState = Literal["validated", "extrapolated_unvalidated", "exhausted_until_reset"]`; `@dataclass class windows.Window` with every `quota_windows` column (`tokens`, `capacity` as `Decimal`); `windows.NOMINAL_PERIOD: Final[Mapping[Provider, timedelta]] = {"jsearch": timedelta(seconds=2_675_242), "adzuna": timedelta(days=30)}` plus `windows.serpapi_period(renewal: datetime) -> tuple[datetime, datetime]` (the calendar month ending at `plan_renewal_date`); `windows.PROBE_RETRY: Final = timedelta(hours=12)`; `windows.RESERVE_RELEASE: Final = timedelta(hours=48)`
  - `@dataclass(frozen=True) class windows.AccountObservation: observed_at; plan_renewal_date: datetime; searches_per_month: int; plan_searches_left: int; this_hour_searches: int; account_rate_limit_per_hour: int`
  - `windows.WindowChange = Insert(window) | Close(window, reason)`; `windows.limit_for(provider, settings) -> int` (200 / 250 / `adzuna_cap_month`); `windows.reserve_for(provider, limit, settings) -> int` (`ceil(reserve_frac × limit)`; Adzuna `cap_month − int(0.9 × cap_month)`); `windows.bootstrap_window(provider, now, settings, *, account: AccountObservation | None) -> Window | None` (§6.3 bootstrap; `tokens = capacity` needs `interval_s`, so bootstrap takes `interval_s` too); `windows.roll_forward(w, provider, now, settings, *, interval_s, account=None) -> tuple[Window | None, list[WindowChange]]` (pure; closes `rolled_over` across long downtime)
  - `@dataclass(frozen=True) class pacing.PlannedSpec: request_class: Literal["normal", "retry", "probe"]` (what the planner and the dry-run hand to `project_allowance`); `pacing.fair_share(w: Window, interval_s: int) -> float`; `pacing.capacity(w, interval_s) -> Decimal`; `pacing.accrue(w, now, *, interval_s) -> None`; `pacing.effective_reserve(w, now) -> int`; `pacing.per_run_cap(settings, fair_share: float) -> int`; `pacing.DenyCode = Literal["paced", "reserve_held", "exhausted", "window_cap", "awaiting_window_validation"]`; `@dataclass(frozen=True) class pacing.Admit: request_class; tokens_used: int; starts_probe: bool`; `@dataclass(frozen=True) class pacing.Deny: code: DenyCode`; `pacing.decide(w, *, provider, request_class, units, used, run_normal_count, hour_ok, adzuna_ok, now, per_run_cap) -> Admit | Deny` (the §6.7 function verbatim in its order)
  - `@dataclass(frozen=True) class windows.ObservationEffects: adjustment_units: int; alerts: tuple[Literal["quota.limit_increased", "quota.unaccounted_usage"], ...]; reattribute: bool`; `windows.apply_observation_pure(w, obs: Observation, settings, *, provider, used_units: int) -> tuple[Window, list[WindowChange], ObservationEffects]` (§6.3 steps 0–4 in order; a 429 never validates)
  - `async def windows.current_window_locked(conn, provider, now, settings, *, interval_s, account=None) -> Window | None` (`FOR UPDATE`, then `roll_forward` persisted: closes and inserts); `async def windows.current_snapshot(conn, provider) -> Window | None` (plain `SELECT`); `async def windows.save_window(conn, w) -> None`; `async def windows.apply_changes(conn, changes) -> None`
  - Metrics `metrics.QUOTA_USED_UNITS` and `metrics.QUOTA_REMAINING_UNITS` (gauges, label `provider`), set by Task 8's `settle` and `reserve`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_quota_windows.py
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from jobhunter.quota.windows import NOMINAL_PERIOD, Close, Insert, apply_observation_pure, bootstrap_window, roll_forward
from jobhunter.sources.base import Observation
from tests.support.quota import SETTINGS  # the seeded QuotaSettings (Task 5 values)
T0 = datetime(2026, 10, 16, 9, 47, tzinfo=UTC); SIX_H = 21600
def test_bootstrap_per_provider() -> None:
    js = bootstrap_window("jsearch", T0, SETTINGS, interval_s=SIX_H, account=None)
    assert (js.window_start, js.window_end, js.state, js.limit_units, js.reserve_units) == (T0, T0 + NOMINAL_PERIOD["jsearch"], "extrapolated_unvalidated", 200, 20)
    assert js.tokens == js.capacity == Decimal("2.9067") and js.probe_used is False  # 2 × 180 × 21600 / 2675242, quantised to the column scale
    assert bootstrap_window("serpapi", T0, SETTINGS, interval_s=SIX_H, account=None) is None
    az = bootstrap_window("adzuna", T0, SETTINGS, interval_s=SIX_H, account=None); assert (az.state, az.limit_units, az.reserve_units, az.window_end) == ("validated", 2250, 225, T0 + timedelta(days=30))
def test_roll_forward_across_long_downtime_keeps_history() -> None:
    w = bootstrap_window("adzuna", T0, SETTINGS, interval_s=SIX_H, account=None)
    later = T0 + timedelta(days=95)
    cur, changes = roll_forward(w, "adzuna", later, SETTINGS, interval_s=SIX_H)
    assert [type(c).__name__ for c in changes] == ["Close", "Insert"] * 3 and cur.window_start == T0 + timedelta(days=90) and cur.tokens == cur.capacity
    assert all(c.reason == "rolled_over" for c in changes if isinstance(c, Close))
def test_observation_steps_in_order() -> None:
    w = bootstrap_window("jsearch", T0, SETTINGS, interval_s=SIX_H, account=None)
    obs = Observation(observed_at=T0 + timedelta(minutes=1), status=200, reset_at=T0 + timedelta(days=30), limit=100, remaining=99)
    w2, changes, fx = apply_observation_pure(w, obs, SETTINGS, provider="jsearch", used_units=1)  # Codex #20
    assert (w2.state, w2.window_end, w2.limit_units, w2.reserve_units, fx.adjustment_units, fx.alerts, changes) == ("validated", obs.reset_at, 100, 10, 0, (), [])
    w3, _, fx3 = apply_observation_pure(w2, Observation(observed_at=T0 + timedelta(minutes=2), status=200, remaining=90), SETTINGS, provider="jsearch", used_units=1)
    assert fx3.adjustment_units == 9 and fx3.alerts == ("quota.unaccounted_usage",)
    stale, _, _ = apply_observation_pure(w3, Observation(observed_at=T0, status=200, remaining=0), SETTINGS, provider="jsearch", used_units=1)
    assert stale == w3  # step 0: a late response changes nothing
    reset = Observation(observed_at=T0 + timedelta(days=31), status=200, reset_at=T0 + timedelta(days=61), limit=200, remaining=200)
    w4, changes4, fx4 = apply_observation_pure(w3, reset, SETTINGS, provider="jsearch", used_units=10)
    assert [type(c).__name__ for c in changes4] == ["Close", "Insert"] and changes4[0].reason == "provider_reset_observed" and fx4.reattribute and w4.window_start == reset.observed_at
    assert fx4.alerts == ("quota.limit_increased",)
    ex, _, _ = apply_observation_pure(w4, Observation(observed_at=T0 + timedelta(days=32), status=429, remaining=0), SETTINGS, provider="jsearch", used_units=10)
    assert ex.state == "exhausted_until_reset" and ex.window_end == w4.window_end  # a 429 never validates
```

```python
# tests/unit/test_quota_pacing.py
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from jobhunter.quota.pacing import accrue, decide, effective_reserve, fair_share, per_run_cap
from jobhunter.quota.windows import bootstrap_window
from tests.support.quota import SETTINGS
T0 = datetime(2026, 10, 16, tzinfo=UTC)
def test_fair_share_capacity_and_accrual() -> None:
    w = bootstrap_window("jsearch", T0, SETTINGS, interval_s=3600, account=None)
    assert round(fair_share(w, 3600), 3) == 0.242 and w.capacity == Decimal(1) and per_run_cap(SETTINGS, fair_share(w, 3600)) == 10
    assert per_run_cap(SETTINGS, 41.0) == 82 and per_run_cap(SETTINGS.__class__(**{**SETTINGS.__dict__, "paid_max_requests_per_run": 3}), 41.0) == 3
    w.tokens = Decimal(0); w.tokens_at = T0; accrue(w, T0 + timedelta(hours=6), interval_s=3600)
    assert w.tokens == Decimal(1) and effective_reserve(w, T0) == 20 and effective_reserve(w, w.window_end - timedelta(hours=47)) == 0
def test_decision_table() -> None:
    w = bootstrap_window("jsearch", T0, SETTINGS, interval_s=21600, account=None)
    d = lambda **k: decide(w, provider="jsearch", request_class=k.pop("cls", "normal"), units=1, used=k.pop("used", 0), run_normal_count=k.pop("run", 0), hour_ok=k.pop("hour", True), adzuna_ok=True, now=T0, per_run_cap=10, **k)
    assert d().starts_probe and d().request_class == "probe" and d().tokens_used == 0
    w.probe_used = True; assert d().code == "awaiting_window_validation"
    w.state, w.probe_used = "validated", False
    assert d(used=200).code == "exhausted" and d(used=180).code == "reserve_held" and d(used=180, cls="retry").request_class == "retry"
    assert d(hour=False).code == "window_cap" and d(run=10).code == "paced"
    w.tokens = Decimal("0.5"); assert d().code == "paced" and d(cls="probe").request_class == "probe"
    w.state, w.probe_failed, w.next_probe_at = "exhausted_until_reset", False, T0 + timedelta(hours=12)
    assert d().code == "exhausted" and decide(w, provider="jsearch", request_class="normal", units=1, used=0, run_normal_count=0, hour_ok=True, adzuna_ok=True, now=T0 + timedelta(hours=12), per_run_cap=10).starts_probe
```

```python
# tests/integration/test_quota_windows_db.py
import pytest, sqlalchemy as sa
from datetime import timedelta
from jobhunter.clock import utcnow
from jobhunter.quota.windows import current_snapshot, current_window_locked
from tests.support.quota import SETTINGS
pytestmark = pytest.mark.integration
async def test_bootstrap_and_rollover_are_persisted_once(engine) -> None:
    now = utcnow()
    async with engine.begin() as c:
        w = await current_window_locked(c, "adzuna", now, SETTINGS, interval_s=21600); assert w is not None and w.is_current
        assert await current_window_locked(c, "serpapi", now, SETTINGS, interval_s=21600) is None
    async with engine.begin() as c:
        w2 = await current_window_locked(c, "adzuna", now + timedelta(days=61), SETTINGS, interval_s=21600)
    async with engine.connect() as c:
        rows = (await c.execute(sa.text("SELECT window_start, is_current, closed_reason FROM quota_windows WHERE provider='adzuna' ORDER BY window_start"))).all()
    assert [(r.is_current, r.closed_reason) for r in rows] == [(False, "rolled_over"), (False, "rolled_over"), (True, None)] and w2.window_start == rows[2].window_start
    async with engine.connect() as c: assert (await current_snapshot(c, "adzuna")).window_start == w2.window_start
```

`tests/support/quota.py` holds `SETTINGS = QuotaSettings(200, 250, 10, 45, 20, 225, 900, 2250, "auto")`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_quota_windows.py tests/unit/test_quota_pacing.py tests/integration/test_quota_windows_db.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'jobhunter.quota'`

- [ ] **Step 3: Implement `windows.py` and `pacing.py`**

- `capacity = max(Decimal(1), Decimal(2) × fair_share)` rounded to 4 decimals (the column scale); `accrue` adds `N × Δt / length` capped at the capacity and never lowers the tokens.
- `apply_observation_pure` returns a copy; `reattribute` tells the caller (Task 8 `settle`) to move the entry to the new window. The alerts are raised by the DB caller (`settle`) with subject `provider:<name>`.
- `current_window_locked`: `SELECT … FOR UPDATE` on the current row; when the row must be rolled over, `apply_changes` closes it (`is_current = false, closed_reason`) and inserts the successors, and the new current row is locked by the insert itself.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit tests/integration/test_quota_windows_db.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/src/jobhunter/quota backend/src/jobhunter/observability/metrics.py tests
git commit -m "feat(m1): quota windows, bootstrap and rollover, ordered observation reconciliation, token-bucket pacing and the admission decision"
```

---

### Task 8: Quota ledger: reservation, dispatch marker, settlement, recovery, the hard caps, projections, fallback eligibility and dry-run projections

**Files:**
- Create: `backend/src/jobhunter/quota/ledger.py`, `quota/fallback.py`, `quota/dryrun.py`, `tests/support/quota.py` (extend)
- Modify: `backend/src/jobhunter/observability/health.py` (`quota` field of `SystemHealthOut`), `backend/src/jobhunter/worker/main.py` (`jobhunter.quota.ledger` in `_REGISTERING_MODULES`)
- Test: `tests/integration/test_quota_ledger.py`, `tests/integration/test_quota_concurrency.py`, `tests/property/test_quota_property.py`, `tests/unit/test_fallback.py`, `tests/unit/test_dryrun.py`, `tests/integration/test_system_health_quota.py`

**Interfaces:**
- Consumes: Task 7; `RunOwner`, `fence_epoch`, `transition`, `reconciler`, `ReconcileContext`, `raise_alert`.
- Produces:
  - `ledger.COUNTED: Final = ("reserved", "dispatched", "billed", "ambiguous")`; `ledger.RECOVERY_AFTER: Final = timedelta(minutes=15)`; `ledger.MAX_ATTEMPT_SEQ: Final = 8`
  - `@dataclass(frozen=True) class ledger.Reservation: entry_id: int; request_class: Literal["normal", "retry", "probe"]; window_start: datetime`; `@dataclass(frozen=True) class ledger.Denial: code: DenyCode`
  - `async def ledger.lock_run_owner(conn, owner: RunOwner) -> None` (`SELECT 1 FROM runs WHERE id = :run AND attempt_no = :attempt AND status = 'running' FOR SHARE`, else `FencedOut("run")`; also refuses while a `global` purge fence is active)
  - `async def ledger.reserve(conn, provider, *, owner: RunOwner, request_key: bytes, attempt_seq: int, request_class, fetch_request_id: int, settings: QuotaSettings, interval_s: int, now: datetime, account: AccountObservation | None = None, units: int = 1) -> Reservation | Denial` (§6.7 `reserve` verbatim in its order: owner lock, window lock and roll-forward, accrual, `decide` with `serpapi_hour_ok` / `adzuna_windows_ok`, the `reserved` row, the window saved; a `Deny` persists the accrual only)
  - `async def ledger.mark_dispatched(conn, entry_id: int, owner: RunOwner) -> None` (owner lock, `transition(quota_ledger, reserved→dispatched)`; `TransitionConflict` → `FencedOut`)
  - `async def ledger.settle(conn, entry_id: int, *, state: Literal["billed", "not_billed", "ambiguous"], evidence: str | None, http_status: int | None, provider_remaining: int | None, observation: Observation | None, settings, now) -> None` (`dispatched → state`; `apply_observation_pure` on the locked current window, changes applied, the entry re-attributed when `reattribute`, the effect alerts raised with subject `provider:<p>`; a `probe` entry's outcome goes through `windows.note_probe_outcome` below; a JSearch 429 raises `quota.exhausted` warn, resolved by a later validated observation with `remaining > 0`)
  - `windows.note_probe_outcome(w: Window, outcome: Literal["validated", "failed", "never_sent"], *, now) -> None` (pure, the one place that touches the probe columns after a reservation: `validated` → `probe_used = probe_failed = False, next_probe_at = None` (the observation's step 1 did it already when the reset was seen); `failed` (a probe settled `ambiguous` or with a non-2xx status, or recovered as ambiguous) → `probe_failed = True, next_probe_at = now + PROBE_RETRY`; `never_sent` (a probe reservation released as `never_dispatched`) → `probe_used = False, next_probe_at = None`, so the next request starts a fresh probe instead of waiting 12 h for one that never happened)
  - `@dataclass(frozen=True) class ledger.LedgerAttempt: entry_id: int; attempt_seq: int; state: str; fetch_request_id: int | None; run_id: int | None; run_attempt_no: int | None; dispatched: bool`; `async def ledger.latest_attempt(conn, provider, request_key: bytes) -> LedgerAttempt | None`
  - The two recovery writes, **centralised and under the window lock** (every caller — the replay branch of `acquire_page`, the shutdown path and the reconciler — uses them, nothing else writes these transitions): `async def ledger.release_never_dispatched(conn, entry_id: int, *, now) -> bool` (locks the entry's window `FOR UPDATE` (the window of its `window_start`, current or closed), `reserved → not_billed('never_dispatched')`, and for a `probe` entry `note_probe_outcome(w, "never_sent")`, window saved) and `async def ledger.mark_ambiguous(conn, entry_id, *, now) -> bool` (the same lock, `dispatched → ambiguous`, for a `probe` entry `note_probe_outcome(w, "failed")`); `async def ledger.used_units(conn, provider, window_start) -> int`; `async def ledger.normal_count_for_run(conn, provider, run_id: int) -> int` (normal-class counted entries of the run, across its attempts)
  - `async def ledger.serpapi_hour_ok(conn, w: Window, now, units, settings, *, local_recent: int) -> bool` and `async def ledger.detect_foreign_usage(conn, w, account: AccountObservation, now) -> int` (§6.5: `foreign = this_hour − local attempts reserved in the 60 min before observed_at`; `> 1` raises `quota.serpapi_foreign_usage` warn and sets `conservative_until = now + 24 h`); `async def ledger.adzuna_windows_ok(conn, now, units, settings) -> bool` (the four rolling counts of §6.5 over `created_at`, `COUNTED` states)
  - `async def ledger.project_allowance(conn, provider, specs: Sequence[PlannedSpec], now, settings, *, interval_s, account=None) -> list[Admit | Deny]` (§6.7, pure on a snapshot; `PlannedSpec(request_class)` is in `quota/pacing.py`)
  - Reconciler `ledger_unsettled` (`async def ledger.reconcile_ledger_unsettled(ctx: ReconcileContext) -> int`, cadence `tick`), one transaction per entry, `fence_epoch(conn, ctx.epoch)` first: `reserved` older than 15 min whose owner predicate no longer holds (`runs` row locked `FOR UPDATE`, then the window) → `release_never_dispatched` and its fetch request `skipped('superseded')`; `dispatched` older than 15 min → `mark_ambiguous` and its fetch request `ambiguous`; returns the count. Recovery therefore never leaves a bootstrap probe "in flight": the window's probe columns are restored with the entry.
  - `fallback.CellOutcome = Literal["served", "paced", "reserve_held", "exhausted", "window_cap", "awaiting_after_failed_probe", "awaiting_probe_in_flight", "ambiguous", "transient", "breaker_open", "auth_failed", "contract_broken", "shutdown", "run_cancelled"]`; `fallback.eligible(outcome: CellOutcome) -> bool` (the §6.6 table)
  - `dryrun.quota_projections(settings: RunSettings, *, jsearch: Window | None, adzuna: Window | None, serpapi: Window | None, cells: int, now) -> tuple[list[Warning], dict[str, JSONValue]]` with `Warning(code, **detail)`: the `jsearch`, `adzuna`, `arbeitnow`, `serpapi_fallback` and `stale_ttl_short` parts of §6.8 (`paid_run_cap_binding`, `paid_refresh_slow`, `coverage_gap_guaranteed`, `adzuna_rotation`, `arbeitnow_coverage_risk`, `stale_ttl_short`); M2 composes `dry_run()` around it (Deviation 10)
  - `SystemHealthOut.quota: dict[Provider, QuotaOut | None]` with `QuotaOut(window_start, window_end, state, limit_units, used_units, remaining_units, tokens)`

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_quota_ledger.py
from datetime import timedelta
import pytest, sqlalchemy as sa
from jobhunter.clock import utcnow
from jobhunter.errors import FencedOut
from jobhunter.quota import ledger
from jobhunter.quota.pacing import PlannedSpec
from jobhunter.quota.windows import AccountObservation
from jobhunter.sources.base import Observation
from jobhunter.worker import runs
from jobhunter.worker.tasks import defer_in_tx
from tests.support.collect import ctx, planned_request, queued_run   # a queued run admitted as epoch 1 (RunOwner), a planned fetch_requests row for a key, a ReconcileContext
from tests.support.quota import SETTINGS
pytestmark = pytest.mark.integration
K = bytes(32)

async def test_reserve_dispatch_settle_and_replay_order(engine, current_epoch) -> None:
    owner = await queued_run(engine); fr = await planned_request(engine, owner, provider="jsearch", key=K, attempt_seq=1); now = utcnow()
    async with engine.begin() as c:
        r = await ledger.reserve(c, "jsearch", owner=owner, request_key=K, attempt_seq=1, request_class="normal", fetch_request_id=fr, settings=SETTINGS, interval_s=21600, now=now)
    assert isinstance(r, ledger.Reservation) and r.request_class == "probe"  # the first request of an unvalidated window
    async with engine.begin() as c: await ledger.mark_dispatched(c, r.entry_id, owner)
    async with engine.begin() as c:
        await ledger.settle(c, r.entry_id, state="billed", evidence=None, http_status=200, provider_remaining=199, settings=SETTINGS, now=now,
                            observation=Observation(observed_at=now, status=200, reset_at=now + timedelta(days=30), limit=200, remaining=199))
    async with engine.connect() as c:
        state, pr = (await c.execute(sa.text("SELECT state, provider_remaining FROM quota_ledger WHERE id=:i"), {"i": r.entry_id})).one()
        w = (await c.execute(sa.text("SELECT state, probe_used, next_probe_at FROM quota_windows WHERE provider='jsearch' AND is_current"))).one()
        latest = await ledger.latest_attempt(c, "jsearch", K)
    assert (state, pr, tuple(w)) == ("billed", 199, ("validated", False, None)) and latest.attempt_seq == 1 and latest.state == "billed"
    async with engine.begin() as c:
        with pytest.raises(FencedOut): await ledger.mark_dispatched(c, r.entry_id, owner)  # already dispatched: 0 rows

async def test_superseded_owner_cannot_reserve_or_dispatch_and_is_released(engine, current_epoch, queue_app) -> None:
    owner = await queued_run(engine); fr = await planned_request(engine, owner, provider="jsearch", key=K, attempt_seq=1)
    async with engine.begin() as c:
        r = await ledger.reserve(c, "jsearch", owner=owner, request_key=K, attempt_seq=1, request_class="normal", fetch_request_id=fr, settings=SETTINGS, interval_s=21600, now=utcnow())
    async with engine.begin() as c: await runs.begin_new_attempt(c, owner.run_id, epoch=1, end_reason="heartbeat_stale", defer=defer_in_tx)  # C28: the old owner is superseded
    async with engine.begin() as c:
        with pytest.raises(FencedOut): await ledger.mark_dispatched(c, r.entry_id, owner)
    async with engine.begin() as c: await c.execute(sa.text("UPDATE quota_ledger SET created_at = created_at - interval '16 minutes'"))
    assert await ledger.reconcile_ledger_unsettled(ctx(engine, queue_app)) == 1
    async with engine.connect() as c:
        assert tuple((await c.execute(sa.text("SELECT state, evidence FROM quota_ledger WHERE id=:i"), {"i": r.entry_id})).one()) == ("not_billed", "never_dispatched")
        assert (await c.execute(sa.text("SELECT status, skip_reason FROM fetch_requests WHERE id=:i"), {"i": fr})).one() == ("skipped", "superseded")

async def test_recovery_restores_probe_bookkeeping_on_a_fresh_window(engine, current_epoch, queue_app) -> None:  # C1–C3 against an absent window
    owner, now = await queued_run(engine), utcnow()
    fr1 = await planned_request(engine, owner, provider="jsearch", key=K, attempt_seq=1)
    async with engine.begin() as c:   # the first reservation bootstraps the window and is the probe
        r1 = await ledger.reserve(c, "jsearch", owner=owner, request_key=K, attempt_seq=1, request_class="normal", fetch_request_id=fr1, settings=SETTINGS, interval_s=21600, now=now)
    assert r1.request_class == "probe"
    async with engine.begin() as c: assert await ledger.release_never_dispatched(c, r1.entry_id, now=now)   # C1: never sent
    async with engine.connect() as c: w = (await c.execute(sa.text("SELECT probe_used, probe_failed, next_probe_at FROM quota_windows WHERE provider='jsearch' AND is_current"))).one()
    assert tuple(w) == (False, False, None)
    fr2 = await planned_request(engine, owner, provider="jsearch", key=K, attempt_seq=2)
    async with engine.begin() as c:   # admission is open again: a new probe, not awaiting_window_validation
        r2 = await ledger.reserve(c, "jsearch", owner=owner, request_key=K, attempt_seq=2, request_class="normal", fetch_request_id=fr2, settings=SETTINGS, interval_s=21600, now=now)
        assert isinstance(r2, ledger.Reservation) and r2.request_class == "probe"; await ledger.mark_dispatched(c, r2.entry_id, owner)
    async with engine.begin() as c: assert await ledger.mark_ambiguous(c, r2.entry_id, now=now)   # C2/C3: sent, outcome unknown
    async with engine.connect() as c:
        w = (await c.execute(sa.text("SELECT probe_failed, next_probe_at FROM quota_windows WHERE provider='jsearch' AND is_current"))).one()
        out = await ledger.project_allowance(c, "jsearch", [PlannedSpec("normal")], now + timedelta(hours=1), SETTINGS, interval_s=21600)
    assert tuple(w) == (True, now + ledger.PROBE_RETRY) and out[0].code == "awaiting_window_validation" and eligible("awaiting_after_failed_probe")
    async with engine.connect() as c: assert (await ledger.project_allowance(c, "jsearch", [PlannedSpec("normal")], now + timedelta(hours=13), SETTINGS, interval_s=21600))[0].request_class == "probe"
    # the reconciler takes the same two paths: a stale reserved probe → never_sent; a stale dispatched probe → failed (asserted on two further keys)

async def test_serpapi_hour_rule_exclusive_and_foreign(engine, current_epoch) -> None:
    now = utcnow(); account = AccountObservation(observed_at=now, plan_renewal_date=now + timedelta(days=20), searches_per_month=250, plan_searches_left=200, this_hour_searches=12, account_rate_limit_per_hour=250)
    ... # 12 local reservations in the hour, account reports 12 → the 13th is admitted; account reports 40 with 0 local → `quota.serpapi_foreign_usage` open, conservative_until set, and 41 > 40 denies `window_cap`

async def test_adzuna_rolling_minute_cap(engine, current_epoch) -> None:
    ... # 20 reservations within the minute (not_billed ones excluded) → the 21st is Denial("window_cap"); the next minute admits again

async def test_project_allowance_writes_nothing(engine) -> None:
    async with engine.connect() as c:  # no window exists; an expired one is also tried
        out = await ledger.project_allowance(c, "jsearch", [PlannedSpec("normal")] * 3, utcnow(), SETTINGS, interval_s=21600)
    assert [type(d).__name__ for d in out] == ["Admit", "Deny", "Deny"] and out[1].code == "awaiting_window_validation"
    async with engine.connect() as c: assert (await c.execute(sa.text("SELECT count(*) FROM quota_windows"))).scalar_one() == 0
```

```python
# tests/integration/test_quota_concurrency.py
async def test_eight_concurrent_reservers_never_exceed_the_limit(engine, current_epoch) -> None:
    ... # a validated jsearch window with limit 5, reserve 0, tokens 5; 8 tasks on 8 connections reserve different keys at once
    # assert exactly 5 Reservations and 3 Denials, and SUM(units) over COUNTED states == 5
```

```python
# tests/property/test_quota_property.py
# A hypothesis RuleBasedStateMachine over the pure functions (roll_forward, accrue, decide, apply_observation_pure) with an in-memory ledger:
# rules: tick (1 h–7 d schedules), reserve_normal/retry, dispatch, settle (billed/not_billed/ambiguous), a 429, a late observation, an out-of-order
# observation, a limit decrease, a provider reset observed early or late, a manual run, a crash (reserved never dispatched → never_dispatched after 15 min).
# invariants after every rule: counted ≤ limit_units in every window; no observation ever lowered counted usage; the Adzuna 60 s / 24 h / 7 d / 30 d
# counts never exceed their caps; with an hourly schedule and tokens ≥ 1 a normal request is admitted within 1 / (N / window) hours of the last denial.
```

```python
# tests/unit/test_fallback.py
import pytest
from jobhunter.quota.fallback import eligible
@pytest.mark.parametrize(("outcome", "expected"), [("served", False), ("paced", False), ("reserve_held", False), ("exhausted", True), ("awaiting_after_failed_probe", True),
    ("awaiting_probe_in_flight", False), ("ambiguous", False), ("transient", False), ("breaker_open", True), ("auth_failed", True), ("contract_broken", True), ("shutdown", False), ("run_cancelled", False), ("window_cap", False)])
def test_fallback_table(outcome, expected) -> None: assert eligible(outcome) is expected
```

```python
# tests/unit/test_dryrun.py
def test_projections_with_defaults_and_six_hour_interval() -> None:
    warnings, proj = quota_projections(settings_6h, jsearch=js_window, adzuna=az_window, serpapi=None, cells=4, now=T0)
    assert proj["jsearch"] == {"units_per_run": 1.453, "refresh_cadence_days": 0.688, "cells": 4} and proj["arbeitnow"] == {"page_cap": 3, "expected_pages": 1.162}
    assert [w.code for w in warnings] == [] and quota_projections(settings_6h_40_cells_stale7, ...)[0] has codes {"paid_refresh_slow", "stale_ttl_short"}
```

```python
# tests/integration/test_system_health_quota.py
async def test_health_reports_current_windows(logged_in_client, engine, current_epoch) -> None:
    ... # after one adzuna reservation: body["quota"]["adzuna"] == {"state": "validated", "limit_units": 2250, "used_units": 1, "remaining_units": 2249, ...} and body["quota"]["serpapi"] is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/integration/test_quota_ledger.py tests/integration/test_quota_concurrency.py tests/property/test_quota_property.py tests/unit/test_fallback.py tests/unit/test_dryrun.py tests/integration/test_system_health_quota.py -q`
Expected: FAIL with `ImportError: cannot import name 'ledger' from 'jobhunter.quota'`

- [ ] **Step 3: Implement the ledger, the caps, the projections, the reconciler, the table and the health field**

- `reserve` takes `fence_epoch`? No: the run row `FOR SHARE` is the owner predicate (§3.3.3); the epoch fence belongs to epoch-stamping writes only.
- `settle` raises `quota.limit_increased` (info) and `quota.unaccounted_usage` (warn) from the effects, and inserts the `adjustment` row (`evidence = f"provider_remaining={obs.remaining}"`). It also sets `QUOTA_USED_UNITS` / `QUOTA_REMAINING_UNITS`.
- `reconcile_ledger_unsettled` fences the epoch, then locks each entry's run row `FOR UPDATE` before deciding, so a stale owner's dispatch marker afterwards finds 0 rows; the window lock comes after the run row (owner → `quota_windows` → ledger rows, §3.3.3). `test_quota_ledger.py` imports `eligible` from `jobhunter.quota.fallback` and `PROBE_RETRY` is re-exported by `ledger`.
- `tests/support/collect.py` (new in this task; grows later): `async def queued_run(engine, *, include_paid=True) -> RunOwner` (a manual run inserted with `MANUAL_RUN_SQL`, admitted with `runs.start_attempt(…, epoch=1)`); `async def planned_request(engine, owner, *, provider, key, attempt_seq, cell_id=None) -> int` (a `coverage_cells` row for the provider's default instance and a `planned` `fetch_requests` row); `ctx(engine, queue_app, now=None, epoch=1) -> ReconcileContext`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit tests/property tests/integration -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/src/jobhunter tests
git commit -m "feat(m1): quota ledger with reservation, dispatch marker, settlement and recovery; SerpApi hourly and Adzuna rolling caps; projections, fallback table, dry-run quota projections, quota health"
```

---

### Task 9: JSearch and SerpApi adapters

**Files:**
- Create: `backend/src/jobhunter/sources/jsearch.py`, `sources/serpapi.py`
- Test: `tests/contract/test_jsearch.py`, `tests/contract/test_serpapi.py`, `tests/unit/test_jsearch_plan.py`

**Interfaces:**
- Consumes: Tasks 2–6; `fixture_page`, `mock_fixture`.
- Produces:
  - `class jsearch.JSearchAdapter`: `__init__(client: httpx.AsyncClient, *, api_key: bytes, instance_id: int)`; `capabilities()` → `Capabilities("jsearch", "paid", 1, "cursor", 10, "day", "full", False, 1, [RateLimit("jsearch", 1.0, 1)])`; `plan(cell)` → one `RequestSpec` page 1 with `params = {"query": f"{q} in {city}, Germany"}` (or `f"{q} remote Germany"` for `remote-de`), `country=de`, `num_pages=1`, `date_posted=<bucket>` (`cell.bucket`), `request_class` from the cell; `fetch` (headers `x-rapidapi-key`, `x-rapidapi-host: jsearch.p.rapidapi.com`, GET `https://jsearch.p.rapidapi.com/search-v2`; the legacy `/search` route is never used); `jsearch.ITEMS_PATH: Final[tuple[str, ...]]` and `jsearch.CURSOR_PATH: Final[tuple[str, ...]]` (the item-array path and the cursor path of the `search-v2` envelope as recorded by V9 and written in `V9.md` and the fixtures README; `jsearch.items_of(doc) -> list[JSONValue]`, `jsearch.cursor_of(doc) -> str | None`); `check_envelope` (a JSON object with `status == "OK"`, a list at `ITEMS_PATH` and, when the cursor key exists, a non-empty string at `CURSOR_PATH`, asserted together; anything else `ContractViolation`); `peek_native_ids` (`job_uid` per item, `None` when missing); `parse` (§5.3.1: `native_id = job_uid`, keys `htidocid:<job_uid>` plus `keys_from_link(job_apply_link)`, `posted_at` from `job_posted_at_timestamp` with `jsearch_precision(job_posted_at)`, `item_ts = posted_at`, full description through the quality gate, `job_is_remote` → `{remote}` structured else all three with source `none`, `job_employment_types` mapped, location from `parse_jsearch_location`, `publisher = job_publisher`, `primary_link = canonical_url(job_apply_link)`); `continuation` (`cursor_of(doc)` present → `(CURSOR, cursor)` whatever the page length, else `(END, None)`); `next_page(spec, cursor)` (page 2 with `cursor`, `page_no + 1`, class `normal`); `settle(outcome)` (2xx `billed`; 404 with `x-rapidapi-proxy-response: true` `("not_billed", "rapidapi_gateway_404")`; a connect error before any byte `("not_billed", "connect_failed_before_send")`; everything else `ambiguous`); `observation(page)` from `x-ratelimit-requests-limit/-remaining/-reset` (`reset_at = fetched_at + reset seconds`); `jsearch.CURSOR_REUSE_TTL: Final[timedelta]` (24 h, 2 h or 0: the longest reuse interval V9 observed); `jsearch.PAGE2_MIN_NEW: Final = 7`
  - `class serpapi.SerpApiAdapter`: `__init__(client, *, api_key: bytes, instance_id)`; capabilities `("serpapi", "paid", 1, "cursor", 10, "relative", "full", False, 1, [RateLimit("serpapi", 1.0, 1)])`; `plan` → `q = f"{q} {city}"` (`f"{q} Homeoffice Deutschland"` for `remote-de`), `gl=de`, `hl=SERPAPI_HL`, `engine=google_jobs`, never `location` or `uule`; `async account() -> AccountObservation` (`GET https://serpapi.com/account.json`, free, not a ledger entry); `fetch` (`api_key` added at send time only); `check_envelope` (a JSON object with `search_metadata`; `jobs_results` may be absent when `error` says no results, which is an empty page, not a violation); `peek_native_ids` (decoded `htidocid`); `parse` (§5.3.2: primary link = the `apply_options[]` entry whose `title` equals `via` after removing a `via ` prefix, case-insensitively, else the first; no other option is stored; `extensions` and `detected_extensions` through `parse_extensions` with `reference = response_date(headers, fetched_at)`; precision `hour` or `relative_day`; quality gate); `continuation` (`serpapi_pagination.next_page_token`); `settle` (2xx `billed`, connect-before-send `not_billed`, else `ambiguous`); `observation(page)` is `None` (the account read supplies it); `serpapi.SERPAPI_HL: Final[Literal["de", "en"]]` from V3

- [ ] **Step 1: Write the failing tests**

```python
# tests/contract/test_jsearch.py
import httpx, pytest, respx
from datetime import UTC, datetime
from jobhunter.sources.base import ContractViolation, Continuation, Parsed, SourceError
from jobhunter.sources.jsearch import JSearchAdapter
from tests.support.sources import fixture_page, mock_fixture
pytestmark = pytest.mark.contract
T = datetime(2026, 10, 16, 9, 47, tzinfo=UTC)
def adapter() -> JSearchAdapter: return JSearchAdapter(httpx.AsyncClient(), api_key=b"k", instance_id=1)
def test_page_one_contract() -> None:
    a, page = adapter(), fixture_page("jsearch", "search_v2_page1", SPEC, fetched_at=T)
    a.check_envelope(page); ids = a.peek_native_ids(page); outcomes = a.parse(page)
    assert len(items_of(page.json())) == 10 and isinstance(cursor_of(page.json()), str)  # the recorded envelope: items and cursor at the V9 paths, together
    assert len(ids) == 10 and all(ids) and a.continuation(page)[0] is Continuation.CURSOR
    assert len(outcomes) == 10 and sum(isinstance(o, Parsed) for o in outcomes) >= 9 and [o.item_index for o in outcomes] == list(range(10))
    parsed = [o.normalized for o in outcomes if isinstance(o, Parsed)]
    assert all(p.native_id == uid for p, uid in zip(parsed, ids, strict=False)) and all(":" not in p.native_id for p in parsed)  # job_uid, never the session blob
    assert all(p.raw_keys[0].ns == "htidocid" and p.description_completeness == "full" for p in parsed)
    assert sum(len(p.description_text) >= 200 for p in parsed) / len(parsed) >= 0.9 and sum(p.posted_at is not None for p in parsed) / len(parsed) >= 0.9
    assert {p.posted_precision for p in parsed} <= {"hour", "day"} and any(p.locations for p in parsed)
    obs = a.observation(page); assert obs is not None and obs.limit == 200 and obs.remaining is not None and obs.reset_at > T
def test_empty_page_ends_and_variants_are_contract_failures() -> None:
    a = adapter()
    assert a.continuation(fixture_page("jsearch", "search_v2_empty", SPEC, fetched_at=T)) == (Continuation.END, None)
    for case in ("search_v2_drift", "search_v2_html"):
        with pytest.raises(ContractViolation): a.check_envelope(fixture_page("jsearch", case, SPEC, fetched_at=T))
def test_settlement_rules() -> None:
    a = adapter()
    assert a.settle(HttpOutcome(fixture_page("jsearch", "search_legacy_404", SPEC, fetched_at=T), None)) == ("not_billed", "rapidapi_gateway_404")
    assert a.settle(HttpOutcome(fixture_page("jsearch", "search_v2_429", SPEC, fetched_at=T), None)) == ("ambiguous", None)
    assert a.settle(HttpOutcome(None, SourceError("transient", code="connect", before_send=True))) == ("not_billed", "connect_failed_before_send")
    assert a.settle(HttpOutcome(None, SourceError("ambiguous", code="timeout"))) == ("ambiguous", None)
@respx.mock
async def test_fetch_sends_the_key_as_a_header_only_and_logs_no_key(log_events) -> None:
    route = mock_fixture(respx.mock, "GET", "https://jsearch.p.rapidapi.com/search-v2", "jsearch", "search_v2_page1")
    async with httpx.AsyncClient() as client:
        a = JSearchAdapter(client, api_key=b"TOPSECRETKEY-abcdefghijklmnopqrstuvwxyz0123456789", instance_id=1)
        page = await a.fetch(SPEC)
    sent = route.calls.last.request
    assert sent.headers["x-rapidapi-key"].startswith("TOPSECRET") and "TOPSECRET" not in str(sent.url) and page.status == 200
    assert "TOPSECRET" not in "".join(str(e) for e in log_events()) and "cursor" not in SPEC.to_json()["params"]
```

```python
# tests/contract/test_serpapi.py
def test_hl_en_page_contract_and_v3_overlap() -> None:
    a, page = adapter(), fixture_page("serpapi", "google_jobs_hl_en", SPEC, fetched_at=T)
    a.check_envelope(page); outcomes = a.parse(page); ids = a.peek_native_ids(page)
    assert len(outcomes) == 10 and all(len(i) >= 20 for i in ids if i) and a.continuation(page)[0] is Continuation.CURSOR
    parsed = [o.normalized for o in outcomes if isinstance(o, Parsed)]
    assert all(p.native_id == uid for p, uid in zip(parsed, ids, strict=False)) and all(p.raw_keys[0] == AdKey("htidocid", p.native_id) for p in parsed)
    via_links = [p.primary_link for p in parsed]; assert all(l is None or "utm_" not in l for l in via_links)
    jsearch_ids = set(JSearchAdapter(...).peek_native_ids(fixture_page("jsearch", "search_v2_page1", SPEC, fetched_at=T)))
    assert len(set(ids) & jsearch_ids) >= 3 or SERPAPI_HL == "de"
def test_primary_link_is_the_via_option_only() -> None:
    ... # an item whose apply_options list the `via` publisher third: primary_link is that option; no other option appears in raw_keys
def test_empty_results_are_a_page_not_a_violation_and_account_is_free() -> None:
    ... # `google_jobs_empty` passes check_envelope, parse → [], continuation END; `account()` returns AccountObservation with the recorded fields
def test_missing_date_header_falls_back_to_fetched_at() -> None:
    ... # the hl_en page with its Date header removed: every relative date resolves against fetched_at
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/contract/test_jsearch.py tests/contract/test_serpapi.py tests/unit/test_jsearch_plan.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'jobhunter.sources.jsearch'`

- [ ] **Step 3: Implement both adapters**

`tests/unit/test_jsearch_plan.py` pins the query strings (`"python developer in Berlin, Germany"`, `"python developer remote Germany"`) and the `date_posted` parameter from the cell's bucket. Item-level failures (a missing `job_uid`, a non-string title) become `Rejected(reason_code="item_contract")`, never a page failure. `ITEMS_PATH`/`CURSOR_PATH` are copied from `V9.md`; `test_empty_page_ends_and_variants_are_contract_failures` passing on `search_v2_empty` proves the variant deriver used the same paths. `test_jsearch.py` imports `cursor_of`, `items_of` from `jobhunter.sources.jsearch`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/contract tests/unit -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/src/jobhunter/sources/jsearch.py backend/src/jobhunter/sources/serpapi.py tests
git commit -m "feat(m1): JSearch and SerpApi adapters on scrubbed fixtures: envelopes, identity, continuation, settlement and observations"
```

---
### Task 10: Adzuna and Arbeitnow adapters, with the firehose coverage rules (P-AN1)

**Files:**
- Create: `backend/src/jobhunter/sources/adzuna.py`, `sources/arbeitnow.py`
- Test: `tests/contract/test_adzuna.py`, `tests/contract/test_arbeitnow.py`, `tests/unit/test_arbeitnow_coverage.py`, `tests/property/test_arbeitnow_growth.py`

**Interfaces:**
- Consumes: Tasks 2–6.
- Produces:
  - `class adzuna.AdzunaAdapter`: `__init__(client, *, app_id: bytes, app_key: bytes, instance_id)`; capabilities `("adzuna", "quota", 1, "page", 50, "ingest", "partial", False, 1, [RateLimit("adzuna", 1.0, 1)])`; `plan(cell)` → page 1 with `results_per_page=50`, `what=<q>`, `where=<city>`, `distance=<radius_km>`, `max_days_old=<cell.max_days_old>`, `sort_by=date` (`remote-de`: `what=<q> remote`, no `where`); `fetch` (`GET https://api.adzuna.com/v1/api/jobs/de/search/{page_no}`, `app_id`/`app_key` added at send time); `check_envelope` (an object with a `results` list and an integer `count`); `peek_native_ids` (`str(id)`); `parse` (§5.3.3: `native_id = id`, key `adzuna:<id>`, `posted_at = created` precision `ingest`, `item_ts = created`, description `partial` through the quality gate, `outbound_url_private = redirect_url`, `primary_link = None`, `raw_keys` never from `redirect_url`, salary from `salary_min`/`salary_max` with `kind = "predicted"` when `salary_is_predicted` is `"1"` else `stated`, currency `EUR` period `year`, coordinates → one `unique` candidate, `contract_time`/`contract_type` → employment, `category.tag` → `source_structured["industry"]` unless `unknown`, `company.display_name`); `continuation` (`END` when the page holds fewer than 50 or `page_no × 50 ≥ count`, else `(CURSOR, str(page_no + 1))`); `next_page(spec, cursor)`; `settle` (every dispatched request `billed` whatever its status; connect-before-send `not_billed`); `observation` is `None`; `adzuna.PAGE_CAP: Final = 3`
  - `class arbeitnow.ArbeitnowAdapter`: `__init__(client, *, instance_id)`; capabilities `("arbeitnow", "free", 0, "page", 300, "exact", "full", False, 1, [RateLimit("arbeitnow", 0.5, 1)])`; `plan(cell)` → page 1, always (§5.3.4: each run reads page 1 first; the seek legs are planned by the traversal, below); `fetch` (`GET https://www.arbeitnow.com/api/job-board-api?page=N`); `check_envelope` (an object with `data` list, `meta` and `links`); `peek_native_ids` (`slug`); `parse` (§5.3.4: `native_id = slug`, key `arbeitnow:<numeric suffix>`, `created_at` seconds → `exact` and `item_ts`, `remote=true` → `{remote}` structured, description HTML sanitised, `job_types` → employment (`text`), location through `parse_location_text` with `OutOfScope("not_germany")` on positive evidence only); `continuation` (`links.next` present → `(CURSOR, str(page_no + 1))`, else `END`); `remaining_low(page) -> bool` (`x-ratelimit-remaining ≤ 5`); `arbeitnow.PAGE_SIZE: Final = 300`; `arbeitnow.OVERLAP: Final = timedelta(hours=2)`; `arbeitnow.LOW_REMAINING: Final = 5`; `arbeitnow.page_cap(interval_s: int) -> int`; `arbeitnow.seek_page(n_newer: int) -> int`
  - The pure firehose coverage model, **fresh head first**: `@dataclass(frozen=True) class arbeitnow.Gap: lower: datetime; upper: datetime; status: Literal["open", "closed", "abandoned"]`; `@dataclass(frozen=True) class arbeitnow.FirehoseState: newest_covered_upper: datetime | None; gaps: tuple[Gap, ...]` (derived from the cell's `last_covered_at` and its `coverage_intervals`, never stored separately); `@dataclass(frozen=True) class arbeitnow.Leg: kind: Literal["fresh", "seek"]; gap: Gap | None; start_page: int`; `arbeitnow.plan_legs(state, *, n_newer: Callable[[Gap], int], now, max_ad_age_days) -> list[Leg]` (always `Leg("fresh", None, 1)` first; then one `seek` leg per open gap whose upper bound is newer than `now − max_ad_age_days`, newest first, at `seek_page(n_newer(gap))`); `arbeitnow.step(page_items: Sequence[datetime], leg, state, *, first_item_older_than_target: bool, last_item_newer_than_target: bool) -> Literal["continue", "step_back", "step_forward", "stop"]` (the fresh leg stops once it has passed `newest_covered_upper − OVERLAP`; a seek leg once it has passed `gap.lower`); `arbeitnow.conclude(state, legs: Sequence[tuple[Leg, Sequence[datetime]]], *, pages: int, cap: int, stopped_by: Literal["overlap", "cap", "remaining", "end"], now, max_ad_age_days) -> tuple[FirehoseState, list[Gap]]` (the §5.3.4 rules: a fresh leg that reached the overlap advances `newest_covered_upper` to its newest item; a fresh leg stopped by the cap or the remaining-low rule before the overlap records `[newest_covered_upper, oldest_read)` open; a seek leg that read past `gap.lower` closes the gap; a seek leg cut short closes the gap and opens the remainder `[gap.lower, oldest_read_of_the_leg)` (narrowing is recorded, never silent); a gap whose upper bound is older than `now − max_ad_age_days` is abandoned; every interval `[older, newer)`). Every page of every leg counts against the cap; a seek leg starts only while pages remain, so with a backlog the fresh head is still read every run and the oldest gaps wait or are abandoned and reported (P-AN1)

- [ ] **Step 1: Write the failing tests**

```python
# tests/contract/test_adzuna.py
def test_rpp100_is_capped_at_50_and_everything_is_partial() -> None:
    a, page = adapter(), fixture_page("adzuna", "search_p1_rpp100", SPEC, fetched_at=T)
    a.check_envelope(page); out = a.parse(page); parsed = [o.normalized for o in out if isinstance(o, Parsed)]
    assert len(out) == 50 and len(parsed) >= 48 and all(p.description_completeness == "partial" for p in parsed)
    assert sum(len(p.description_text) <= 500 for p in parsed) == len(parsed) and all(p.posted_precision == "ingest" and p.item_ts == p.posted_at for p in parsed)
    assert all(p.primary_link is None and p.outbound_url_private is not None and p.raw_keys == (AdKey("adzuna", p.native_id),) for p in parsed)
    assert any(p.salary is not None and p.salary.kind == "predicted" for p in parsed) and all(c.resolution == "unique" for p in parsed for c in p.locations if p.locations)
    assert sum(isinstance(o, DuplicateInPage) for o in out) == 0  # re-ingests have different ids: distinct postings, grouped later (§8.7)
    assert a.continuation(page) == (Continuation.CURSOR, "2") or a.continuation(page) == (Continuation.END, None)
def test_settlement_bills_every_status() -> None:
    a = adapter()
    assert a.settle(HttpOutcome(fixture_page("adzuna", "search_401", SPEC, fetched_at=T), None)) == ("billed", None)
    assert a.settle(HttpOutcome(None, SourceError("transient", code="connect", before_send=True))) == ("not_billed", "connect_failed_before_send")
def test_plan_params_and_remote_cell() -> None:
    ... # `where=Berlin`, `distance=25`, `max_days_old=3`, `sort_by=date`, `results_per_page=50`; the remote-de cell has `what="python remote"` and no `where`; no `app_id` in the spec
```

```python
# tests/contract/test_arbeitnow.py
def test_page_contract_scope_rule_and_sanitised_html() -> None:
    a, page = adapter(), fixture_page("arbeitnow", "page1", SPEC, fetched_at=T)
    a.check_envelope(page); out = a.parse(page)
    assert len(out) == 30 and all(o.item_ts is not None for o in out) and a.continuation(page) == (Continuation.CURSOR, "2")
    oos = [o for o in out if isinstance(o, OutOfScope)]; assert oos and all(o.reason == "not_germany" for o in oos)
    parsed = [o.normalized for o in out if isinstance(o, Parsed)]
    assert all("<script" not in (p.description_html_sanitized or "") and p.posted_precision == "exact" for p in parsed)
    assert all(p.raw_keys == (AdKey("arbeitnow", p.native_id.rsplit("-", 1)[1]),) for p in parsed) and any(not p.locations for p in parsed)  # an empty location stays in scope
    assert a.remaining_low(fixture_page("arbeitnow", "page1_remaining5", SPEC, fetched_at=T)) and not a.remaining_low(page)
    with pytest.raises(ContractViolation): a.check_envelope(fixture_page("arbeitnow", "page_truncated", SPEC, fetched_at=T))
```

```python
# tests/unit/test_arbeitnow_coverage.py
from datetime import UTC, datetime, timedelta
from jobhunter.sources.arbeitnow import FirehoseState, Gap, Leg, conclude, page_cap, plan_legs, seek_page
T = datetime(2026, 10, 16, tzinfo=UTC); H = timedelta(hours=1)
def test_caps_and_seek() -> None:
    assert (page_cap(3600), page_cap(21600), page_cap(604800)) == (3, 3, 7) and (seek_page(0), seek_page(299), seek_page(650)) == (1, 1, 2)
def test_fresh_head_is_always_the_first_leg() -> None:
    state = FirehoseState(newest_covered_upper=T - 2 * H, gaps=(Gap(T - 40 * H, T - 29 * H, "open"),))
    legs = plan_legs(state, n_newer=lambda g: 900, now=T, max_ad_age_days=30)
    assert legs[0] == Leg("fresh", None, 1) and legs[1] == Leg("seek", state.gaps[0], 3)
def test_stop_at_cap_records_the_gap_and_a_later_seek_closes_it() -> None:
    state = FirehoseState(newest_covered_upper=T - 40 * H, gaps=())
    fresh = Leg("fresh", None, 1); read = [T - n * H for n in range(0, 30)]  # 30 h of items, newest first; the overlap at T-42h was not reached
    state2, gaps = conclude(state, [(fresh, read)], pages=3, cap=3, stopped_by="cap", now=T, max_ad_age_days=30)
    assert gaps == [Gap(lower=T - 40 * H, upper=T - 29 * H, status="open")] and state2.newest_covered_upper == T
    legs = plan_legs(state2, n_newer=lambda g: 30, now=T + H, max_ad_age_days=30); assert [l.kind for l in legs] == ["fresh", "seek"]
    head = [T + H, T + H / 2]  # the fresh leg reaches the overlap at once
    seek = [T - 29 * H + 5 * H - n * H for n in range(0, 20)]  # reads down past the gap's lower bound
    state3, gaps3 = conclude(state2, [(legs[0], head), (legs[1], seek)], pages=2, cap=3, stopped_by="overlap", now=T + H, max_ad_age_days=30)
    assert [g.status for g in state3.gaps] == ["closed"] and state3.newest_covered_upper == T + H
def test_seek_cut_short_narrows_the_gap_and_never_drops_it() -> None:
    gap = Gap(T - 40 * H, T - 29 * H, "open"); state = FirehoseState(newest_covered_upper=T, gaps=(gap,))
    legs = plan_legs(state, n_newer=lambda g: 300, now=T, max_ad_age_days=30)
    state2, gaps = conclude(state, [(legs[0], [T]), (legs[1], [T - 29 * H - n * H for n in range(0, 5)])], pages=3, cap=3, stopped_by="cap", now=T, max_ad_age_days=30)
    assert [(g.lower, g.upper, g.status) for g in gaps] == [(gap.lower, gap.upper, "closed"), (gap.lower, T - 33 * H, "open")]
def test_old_gap_is_abandoned_not_skipped() -> None:
    state = FirehoseState(newest_covered_upper=T, gaps=(Gap(T - 40 * 24 * H, T - 31 * 24 * H, "open"),))
    legs = plan_legs(state, n_newer=lambda g: 0, now=T, max_ad_age_days=30); assert [l.kind for l in legs] == ["fresh"]
    _, gaps = conclude(state, [(legs[0], [T])], pages=1, cap=3, stopped_by="overlap", now=T, max_ad_age_days=30)
    assert gaps[0].status == "abandoned"
```

```python
# tests/property/test_arbeitnow_growth.py
# hypothesis: a feed that grows by g items per run (g drawn so that g > cap × 300 for some runs) with an open gap present from the start, run
# over n runs with the pure model (`plan_legs`, `step`, `conclude` on a simulated feed of timestamps): every run's first leg is `fresh` from
# page 1, so every item on the first `cap` pages at run time is read by that run (sustained growth never starves the head); every gap has
# lower < upper; the seek page lands within one page of the gap's upper bound (|seek_page(n_newer) − true page| ≤ 1); an open gap is closed,
# narrowed or abandoned (and reported), never dropped; no interval of the feed is ever marked covered without having been read (P-AN1).
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/contract/test_adzuna.py tests/contract/test_arbeitnow.py tests/unit/test_arbeitnow_coverage.py tests/property/test_arbeitnow_growth.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'jobhunter.sources.adzuna'`

- [ ] **Step 3: Implement both adapters and the firehose model**

`conclude` records a fresh-leg gap only when that leg stopped at the cap or the remaining-low rule before reaching `newest_covered_upper − OVERLAP`; a fresh leg that reached the overlap sets `newest_covered_upper` to the newest item read. Pages read while seeking count against the cap. `FirehoseState` is rebuilt by Task 14 from `coverage_cells.last_covered_at` and the cell's `coverage_intervals`; nothing else is persisted for it.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/contract tests/unit tests/property -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/src/jobhunter/sources/adzuna.py backend/src/jobhunter/sources/arbeitnow.py tests
git commit -m "feat(m1): Adzuna and Arbeitnow adapters; the firehose coverage model with seek, overlap, gap closure and abandonment (P-AN1)"
```

---

### Task 11: The six ATS adapters, watchlist detection, snapshot membership and the `watchlist_validate` operation

**Files:**
- Create: `backend/src/jobhunter/sources/watchlist_patterns.py`, `sources/watchlist_validate.py`, `sources/ats/__init__.py`, `sources/ats/common.py`, `sources/ats/greenhouse.py`, `sources/ats/lever.py`, `sources/ats/ashby.py`, `sources/ats/personio.py`, `sources/ats/recruitee.py`, `sources/ats/smartrecruiters.py`
- Modify: `pyproject.toml` (`defusedxml==0.7.1`), `backend/src/jobhunter/worker/main.py` (`jobhunter.sources.watchlist_validate` in `_REGISTERING_MODULES`)
- Test: `tests/unit/test_watchlist_patterns.py`, `tests/contract/test_ats.py`, `tests/integration/test_watchlist_validate.py`, `tests/integration/test_board_not_found.py`

**Interfaces:**
- Consumes: Tasks 2–6; `register_operation_handler`, `OperationContext`, `execute_operation`, `breaker`, `instances`.
- Produces:
  - `watchlist_patterns.AtsType = Literal["greenhouse", "lever", "ashby", "personio", "recruitee", "smartrecruiters"]`; `watchlist_patterns.SLUG: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")`; `@dataclass(frozen=True) class Detected: ats: AtsType; slug: str; lever_region: Literal["global", "eu"] | None`; `@dataclass(frozen=True) class Unsupported: reason: Literal["workable", "unsupported_pattern", "bad_slug", "not_a_url"]`; `watchlist_patterns.detect(url: str) -> Detected | Unsupported` (the §5.4 table; a trailing slash, a path tail and a query are ignored; the URL is never fetched)
  - `watchlist_patterns.API_HOSTS: Final[Mapping[AtsType, tuple[str, ...]]]` (the fixed hosts of §5.3.5; Personio `{slug}.jobs.personio.de`; Lever `api.lever.co` / `api.eu.lever.co`): the compile-time allowlist of §5.4
  - `ats/common.py`: `@dataclass(frozen=True) class SnapshotDraft: members: frozenset[str]; member_count: int; complete: bool; incomplete_reason: str | None` (reasons `non_2xx`, `envelope`, `no_end`, `count_mismatch`, `missing_id`); `common.membership(pages: Sequence[RawPage], adapter, *, reached_end: bool) -> SnapshotDraft` (rules 1–5 of §5.3.5); `class common.AtsAdapter` (shared: `board`, `instance_id`, the host from `API_HOSTS`, `capabilities()` with `billing="free"`, `can_close=True`, `max_concurrency=2`, `rate_limits=[ATS_HOST_LIMIT]`, `settle`/`observation` no-ops, `key(posting_id) -> AdKey` via `ats_key`)
  - One adapter class per ATS with the §5.3.5 request, pagination, identity, posted date, work mode and description rules: `GreenhouseAdapter` (single page; `meta.total` must equal `len(jobs)` else `incomplete(count_mismatch)`; `content` unescaped then sanitised; `requisition_id` and `language` in `source_structured`), `LeverAdapter(region)` (`skip`/`limit=100`, fewer than 100 → `END`; `workplaceType` structured; `country` → `remote_countries` for remote postings; `descriptionPlain + lists + additional`), `AshbyAdapter` (`includeCompensation=true`; `workplaceType`/`isRemote`), `PersonioAdapter` (`defusedxml` over the `workzag-jobs` XML; a DTD or entity → `ContractViolation("xml_entity")`; `seniority`, `employmentType`, `yearsOfExperience`, `occupationCategory` kept in `source_structured`), `RecruiteeAdapter` (`remote`/`hybrid`/`on_site`, `country_code`), `SmartRecruitersAdapter` (`limit=100&offset=`, `END` when `offset + len ≥ totalFound`; `detail_specs(page, known: Mapping[str, tuple[bytes, datetime | None]], *, now) -> list[RequestSpec]` for postings that are new, whose list-item sha256 changed, or whose detail is older than 7 days; a list item positively outside Germany and not remote → `OutOfScope("not_germany")` with no detail; `parse_detail(page) -> ItemOutcome`; detail `RateLimit` `SMARTRECRUITERS_DETAIL`)
  - `ats.adapter_for(instance: InstanceRow, client) -> AtsAdapter`
  - `watchlist_validate.validate_company(conn, company_id: int, *, client: httpx.AsyncClient, now) -> Literal["ok", "not_found", "error"]` (one GET of the board's list URL with redirects disabled; `ok` creates the source instance through `instance_for_company`; writes `validation_status` and `validated_at`); the handler `@register_operation_handler("watchlist_validate")` (payload `{"watchlist_company_id": id}`, dedup key `watchlist_validate:<id>`)

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_watchlist_patterns.py
import pytest
from jobhunter.sources.watchlist_patterns import Detected, Unsupported, detect
@pytest.mark.parametrize(("url", "expected"), [
    ("https://boards.greenhouse.io/acme/jobs/123", Detected("greenhouse", "acme", None)), ("https://job-boards.greenhouse.io/acme", Detected("greenhouse", "acme", None)),
    ("https://jobs.lever.co/acme/", Detected("lever", "acme", "global")), ("https://jobs.eu.lever.co/Acme?lang=de", Detected("lever", "Acme", "eu")),
    ("https://jobs.ashbyhq.com/n8n", Detected("ashby", "n8n", None)), ("https://everphone.jobs.personio.de/", Detected("personio", "everphone", None)),
    ("https://everphone.jobs.personio.com/job/1", Detected("personio", "everphone", None)), ("https://bunq.recruitee.com/o/x", Detected("recruitee", "bunq", None)),
    ("https://jobs.smartrecruiters.com/DeliveryHero/123", Detected("smartrecruiters", "DeliveryHero", None)), ("https://careers.smartrecruiters.com/DeliveryHero", Detected("smartrecruiters", "DeliveryHero", None)),
    ("https://apply.workable.com/acme", Unsupported("workable")), ("https://www.acme.com/careers", Unsupported("unsupported_pattern")),
    ("https://jobs.lever.co/../etc", Unsupported("bad_slug")), ("not a url", Unsupported("not_a_url"))])
def test_detection_table(url, expected) -> None: assert detect(url) == expected
```

```python
# tests/contract/test_ats.py
@pytest.mark.parametrize(("ats", "case", "n", "key_ns"), [("greenhouse", "greenhouse_getyourguide", 15, "ats:greenhouse:getyourguide"), ("lever", "lever_leverdemo", None, "ats:lever:global:leverdemo"),
    ("ashby", "ashby_n8n", None, "ats:ashby:n8n"), ("personio", "personio_everphone", 8, "ats:personio:everphone"), ("recruitee", "recruitee_bunq", None, "ats:recruitee:bunq")])
def test_board_contract(ats, case, n, key_ns) -> None:
    a, page = adapter_for_fixture(ats, case), fixture_page("ats", case, SPEC, fetched_at=T)
    a.check_envelope(page); ids = a.peek_native_ids(page); out = a.parse(page)
    assert all(ids) and len(out) == len(ids) and (n is None or len(out) == n) and a.continuation(page) == (Continuation.END, None)
    parsed = [o.normalized for o in out if isinstance(o, Parsed)]; assert len(parsed) == len(out)
    assert all(p.raw_keys == (AdKey(key_ns, p.native_id),) and p.posted_precision == "exact" and p.description_completeness == "full" for p in parsed)
    assert sum(len(p.description_text) >= 200 for p in parsed) / len(parsed) >= 0.9
    draft = membership([page], a, reached_end=True); assert draft.complete and draft.members == frozenset(ids)
def test_structured_fields_per_ats() -> None:
    lever = parsed_of("lever", "lever_leverdemo"); assert any(p.work_mode_source == "structured" for p in lever) and any(p.remote_countries for p in lever if "remote" in p.work_mode_possible)
    rec = parsed_of("recruitee", "recruitee_bunq"); assert all(p.work_mode_source == "structured" for p in rec) and any(p.remote_countries == ("NL",) for p in rec)
    per = parsed_of("personio", "personio_everphone"); assert all(p.work_mode_source == "none" for p in per) and any("seniority" in p.source_structured for p in per)
    gh = parsed_of("greenhouse", "greenhouse_getyourguide"); assert any(p.requisition_id for p in gh) and all(p.work_mode_source == "none" for p in gh)
def test_incomplete_snapshots_and_xml_entities() -> None:
    gh = adapter_for_fixture("greenhouse", "greenhouse_count_mismatch")
    assert membership([fixture_page("ats", "greenhouse_count_mismatch", SPEC, fetched_at=T)], gh, reached_end=True).incomplete_reason == "count_mismatch"
    with pytest.raises(ContractViolation, match="xml_entity"): adapter_for_fixture("personio", "personio_entity").check_envelope(fixture_page("ats", "personio_entity", SPEC, fetched_at=T))
    assert membership([fixture_page("ats", "lever_leverdemo", SPEC, fetched_at=T)], adapter_for_fixture("lever", "lever_leverdemo"), reached_end=False).incomplete_reason == "no_end"
def test_smartrecruiters_list_and_details() -> None:
    a = adapter_for_fixture("smartrecruiters", "smartrecruiters_deliveryhero_list"); page = fixture_page("ats", "smartrecruiters_deliveryhero_list", SPEC, fetched_at=T)
    assert a.continuation(page) == (Continuation.END, None) and len(a.peek_native_ids(page)) == 10
    known = {a.peek_native_ids(page)[0]: (a.list_item_sha256(page, 0), T)}  # unchanged and fresh: no detail call
    specs = a.detail_specs(page, known, now=T); assert len(specs) == 9 and all(s.operation == "detail" and s.detail_native_id for s in specs)
    stale = {a.peek_native_ids(page)[0]: (a.list_item_sha256(page, 0), T - timedelta(days=8))}; assert len(a.detail_specs(page, stale, now=T)) == 10
    detail = a.parse_detail(fixture_page("ats", "smartrecruiters_deliveryhero_detail_1", SPEC, fetched_at=T)); assert isinstance(detail, Parsed) and len(detail.normalized.description_text) >= 200
```

```python
# tests/integration/test_board_not_found.py
async def test_board_404_marks_not_found_and_skips_until_half_open(engine, current_epoch) -> None:  # Review Focus 5 (the planning half is Task 13)
    ... # a watchlist company with an unknown slug and an ATS instance with one active posting; the fixture transport answers 404;
    # fetching the board through `acquire_page`-less direct `send` + `record_failure("not_found")`: health not_found, `source.not_found` warn open,
    # available(now) == "blocked", available(now + 6 h) == "half_open"; the posting is still `active` with its availability_changed_at unchanged;
    # `record_success` later → ok and the alert resolved
```

```python
# tests/integration/test_watchlist_validate.py
async def test_operation_validates_and_creates_the_instance(engine, current_epoch, queue_app, fake_http) -> None:
    ... # create_operation(kind="watchlist_validate", payload={"watchlist_company_id": id}) for the greenhouse slug `getyourguide` → execute_operation
    # → validation_status 'ok', validated_at set, a source_instances row (greenhouse, 'getyourguide'); an unknown slug → 'not_found' and no instance; the operation `succeeded` both times
```

(`fake_http` is a fixture of `tests/support/sources.py` that calls `http.configure` with a config whose `fake_sources` is true.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_watchlist_patterns.py tests/contract/test_ats.py tests/integration/test_board_not_found.py tests/integration/test_watchlist_validate.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'jobhunter.sources.watchlist_patterns'`

- [ ] **Step 3: Implement the adapters, the patterns, membership and the operation**

Every ATS adapter's `check_envelope` requires the exact root shape of its API (`jobs` list and `meta.total` for Greenhouse, a list for Lever, `jobs` for Ashby, `workzag-jobs` for Personio, `offers` for Recruitee, `content` with `totalFound` for SmartRecruiters). `membership` collects `peek_native_ids` of every page; one `None` id makes the draft `incomplete(missing_id)`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit tests/contract tests/integration/test_board_not_found.py tests/integration/test_watchlist_validate.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock backend/src/jobhunter tests
git commit -m "feat(m1): six ATS adapters with snapshot membership, watchlist detection by pattern, watchlist_validate operation"
```

---

### Task 12: Acquisition receipts and the one-page protocol (reserve, dispatch marker, send, persist and settle, replay)

**Files:**
- Create: `backend/src/jobhunter/sources/receipts.py`, `sources/acquire.py`
- Modify: `pyproject.toml` (`zstandard==0.25.0`), `tests/support/collect.py`
- Test: `tests/integration/test_receipts.py`, `tests/integration/test_acquire.py`

**Interfaces:**
- Consumes: Tasks 5–11; `RunOwner`, `transition`, `ledger`, `breaker`, `afault_point`, `PausePoint`.
- Produces:
  - `receipts.RAW_BODY_TTL: Final = timedelta(days=30)`; `receipts.ERROR_BODY_BYTES: Final = 4096`
  - `async def receipts.insert_fetch_request(conn, *, owner: RunOwner, cell_id: int, source_instance_id: int, phase: Literal["primary", "fallback"], spec: RequestSpec, request_key: bytes, attempt_seq: int, status: Literal["planned", "skipped"], skip_reason: str | None = None) -> int`
  - `@dataclass(frozen=True) class receipts.FetchAttempt: id: int; attempt_seq: int; status: str; raw_response_id: int | None; quota_ledger_id: int | None; run_id: int; run_attempt_no: int; http_status: int | None`; `async def receipts.latest_fetch_attempt(conn, request_key: bytes) -> FetchAttempt | None`
  - `async def receipts.store_response(conn, fetch_request_id: int, page: RawPage, *, source_instance_id: int, ledger_entry_id: int | None, succeeded: bool, items: int | None) -> int` (zstd level 3; the full body for a `succeeded` request, the first `ERROR_BODY_BYTES` for every other outcome whatever the HTTP status, an HTML 200 included; a request that did not succeed stores `item_count = 0` and `parsed_at = fetched_at`, so I1 holds for an unparseable envelope and `parse_normalize` never selects it (Deviation 23); `body_sha256`; a stored unexpired body with the same hash is referenced through `body_ref_id` and its `expires_at` raised to `greatest(expires_at, new.expires_at)`; `expires_at = fetched_at + RAW_BODY_TTL`; returns the row id and sets `fetch_requests.raw_response_id`)
  - `async def receipts.load_response(conn, raw_response_id: int, spec: RequestSpec) -> RawPage` (follows `body_ref_id`; raises `LookupError` for a purged body)
  - `@dataclass class acquire.AcquireContext: engine: AsyncEngine; owner: RunOwner; adapter: SourceAdapter; billing: Billing; provider: Provider | None; settings: RunSettings; clock: Callable[[], datetime]; sleep: Callable[[float], Awaitable[None]]; admission_stopped: Callable[[], bool]` (no cached account: SerpApi's account is read inside the protocol, below)
  - `async def ledger.record_account(conn, obs: AccountObservation, *, settings, now) -> None` (added to Task 8's module here: the window lock, `windows.apply_account` (bootstrap or refresh of the SerpApi window from the observation), `detect_foreign_usage`; the same code `reserve` runs when it is given `account=`)
  - Results: `@dataclass(frozen=True) class acquire.Served: fetch_request_id: int; page: RawPage; replayed: bool; new_native_ids: int | None`; `Denied(fetch_request_id, skip_reason)`; `Failed(fetch_request_id, error_code: str, http_status: int | None, replayed: bool)`; `Ambiguous(fetch_request_id)`; `acquire.PageResult = Served | Denied | Failed | Ambiguous`
  - `async def acquire.acquire_page(ctx, spec: RequestSpec, *, cell_id: int, phase: Literal["primary", "fallback"], planned_id: int | None, known_native_ids: Callable[[Sequence[str | None]], Awaitable[int]] | None = None) -> PageResult`: the §6.2 rule-2 order on `latest_fetch_attempt`/`ledger.latest_attempt`: a `succeeded` row is **replayed** from its stored response without HTTP (`Served(replayed=True)`); a settled `failed` row keeps its receipt and applies the §5.2 retry rule — `rate_limited` or a 5xx `transient` *response* earns one `retry`-class attempt (`attempt_seq + 1`) for a paid source and up to three attempts for a free one, any other error code (`contract`, `not_found`, `auth`, `client`) is returned as `Failed(replayed=True)` with no send, no new ledger entry and no second breaker write; a `reserved` row of a superseded owner is released through `ledger.release_never_dispatched` and its request `skipped('superseded')` before `attempt_seq + 1`; a paid `dispatched`/`ambiguous` row becomes `Ambiguous` (`ledger.mark_ambiguous`) and is not re-dispatched; a free or Adzuna one is re-dispatched with `attempt_seq + 1`. Then per attempt: `admission_stopped()` → `skipped('shutdown')`; **SerpApi only**: `account = await adapter.account()` (free HTTP, outside any transaction; a failure or a non-2xx → `Denied("window_cap")`, the request `skipped(window_cap)`, nothing sent: §5.3.2 fails closed); quota providers: `ledger.reserve(…, account=account)` (the observation and its foreign-usage effects are persisted under the window lock *before* `decide`; a `Denial` → `skipped(<code>)`), `fetch_requests planned→reserved`; `afault_point("collect.after_reserve")`; `admission_stopped()` again → `skipped('shutdown')` and `release_never_dispatched`; `ledger.mark_dispatched` and `reserved→dispatched` in one transaction; `afault_point("collect.after_dispatch_marker")`; the rate limiter; `adapter.fetch`; `afault_point("collect.after_response")`; `check_envelope`; `new_native_ids` through `known_native_ids(adapter.peek_native_ids(page))` when given; one settlement transaction: `store_response(succeeded=…)`, `fetch_requests → succeeded | failed(error_code) | ambiguous` with `http_status`, `continuation`, `next_cursor`, `items`, `new_native_ids`, `ledger.settle` with `adapter.settle(outcome)` (billing is classified by the transport outcome and the status alone, never by the envelope: a JSearch HTML 200 is `billed` and `failed('contract')` at once) and `adapter.observation(page)`, `breaker.record_success`/`record_failure`; `afault_point("collect.after_settle")`. Free-source retries sleep through `ctx.sleep` (1 s, 4 s, jitter ±25%, `Retry-After` ≤ 60 s). Every transaction that writes a receipt first holds the run row `FOR SHARE` (`ledger.lock_run_owner`), so a superseded owner is `FencedOut` before any write; the lock order inside is owner → `quota_windows` → ledger row → `fetch_requests`/`raw_responses` → `source_instances`.
  - `acquire.error_code(err: SourceError) -> str` (`transient`, `rate_limited`, `auth`, `contract`, `not_found`, `client`, `ambiguous`)
  - `tests/support/collect.py` gains `acquire_ctx(engine, owner, adapter, *, billing, provider, clock=None, sleep=None, stopped=lambda: False) -> AcquireContext`

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_receipts.py
async def test_store_and_load_roundtrip_with_dedup_and_error_truncation(engine, current_epoch) -> None:
    ... # two succeeded pages with identical 1 MiB bodies: the second row has body_ref_id = first and the first's expires_at == second.fetched_at + 30 d;
    # load_response of the second returns the body; a 503 page of 20 KiB stores 4096 bytes; headers stored are the allowlist only
async def test_latest_fetch_attempt_picks_the_highest_attempt_seq(engine, current_epoch) -> None: ...
```

```python
# tests/integration/test_acquire.py
import httpx, pytest, respx, sqlalchemy as sa
from jobhunter.errors import FencedOut
from jobhunter.sources.acquire import Ambiguous, Denied, Failed, Served, acquire_page
from tests.support.faults import PausePoint
pytestmark = pytest.mark.integration

async def test_paid_page_reserve_marker_send_settle(engine, current_epoch, jsearch_fixture_adapter) -> None:
    owner, cell = await queued_run(engine), await jsearch_cell(engine)
    ctx = acquire_ctx(engine, owner, jsearch_fixture_adapter, billing="paid", provider="jsearch")
    r = await acquire_page(ctx, page1_spec(cell), cell_id=cell, phase="primary", planned_id=None)
    assert isinstance(r, Served) and not r.replayed and r.page.status == 200
    async with engine.connect() as c:
        fr = (await c.execute(sa.text("SELECT status, request_class, items, continuation, http_status, raw_response_id, quota_ledger_id FROM fetch_requests WHERE id=:i"), {"i": r.fetch_request_id})).one()
        led = (await c.execute(sa.text("SELECT state, request_class, http_status FROM quota_ledger WHERE id=:i"), {"i": fr.quota_ledger_id})).one()
        rr = (await c.execute(sa.text("SELECT ledger_entry_id, item_count, http_status FROM raw_responses WHERE id=:i"), {"i": fr.raw_response_id})).one()
    assert (fr.status, fr.request_class, fr.items, fr.continuation, fr.http_status) == ("succeeded", "probe", 10, "cursor", 200)
    assert tuple(led) == ("billed", "probe", 200) and tuple(rr) == (fr.quota_ledger_id, 10, 200)

async def test_intermediate_states_at_each_fault_point(engine, current_epoch, jsearch_fixture_adapter, monkeypatch) -> None:
    ... # PausePoint at collect.after_reserve: fetch_requests reserved + ledger reserved, no dispatched_at; at collect.after_dispatch_marker: both dispatched;
    # at collect.after_response: no raw_responses row yet; after resume: succeeded/billed. One PausePoint per acquisition, four acquisitions on four keys.

async def test_replay_order_of_rule_two(engine, current_epoch, jsearch_fixture_adapter, respx_mock) -> None:
    ... # (a) a settled 2xx row for the key → Served(replayed=True) and the respx route has 0 calls;
    # (b) a `dispatched` paid row of attempt 1 after `begin_new_attempt` → Ambiguous, ledger ambiguous, fetch_requests ambiguous, 0 calls;
    # (c) a `reserved` row of a superseded owner → released not_billed(never_dispatched), old request skipped(superseded), new row attempt_seq 2 served;
    # (d) a settled 429 row → one `retry`-class attempt_seq 2; a second 429 → Failed(error_code="rate_limited") and no third attempt (paid: ≤ 1 retry)

async def test_free_source_retries_and_backoff(engine, current_epoch, arbeitnow_adapter, respx_mock) -> None:
    ... # 503, 503, 200: three fetch_requests rows (attempt_seq 1..3, class free), sleeps ≈ [1.0, 4.0] (±25%), the third `succeeded`;
    # 429 with Retry-After: 30 → a 30 s sleep; Retry-After: 120 → 60 s; a 404 board → Failed(error_code="not_found") and no retry

async def test_denial_and_shutdown_send_nothing(engine, current_epoch, jsearch_fixture_adapter, respx_mock) -> None:
    ... # an exhausted window → Denied("exhausted"), 0 calls; admission_stopped() true before the reserve → Denied("shutdown"), no ledger row

async def test_stale_owner_dispatch_marker_is_fenced(engine, current_epoch, queue_app, jsearch_fixture_adapter, monkeypatch, respx_mock) -> None:  # C28, in process
    ... # PausePoint at collect.after_reserve; while paused, `begin_new_attempt` supersedes the owner; resume → FencedOut, the respx route has 0 calls,
    # and the new owner's `acquire_page` for the same key takes branch (c) above

async def test_contract_violation_fails_the_request_and_opens_the_breaker(engine, current_epoch, jsearch_html_adapter) -> None:
    ... # Failed(error_code="contract", replayed=False), the instance contract_broken, `source.contract_broken` critical open; raw_responses row with
    # body ≤ 4096 bytes, item_count 0 and parsed_at set; the ledger entry `billed` (a 200 is billed whatever the body: billing ≠ envelope validity)

async def test_contract_failure_is_replayed_after_restart(engine, current_epoch, queue_app, jsearch_html_adapter, respx_mock) -> None:  # Review Focus 3
    ... # after the test above, `begin_new_attempt` supersedes the owner; the new owner's acquire_page for the same key → Failed(error_code="contract", replayed=True),
    # the respx route has 0 calls, no second fetch_requests row, no second ledger entry, the instance's consecutive_failures unchanged

async def test_serpapi_account_is_read_before_each_search_and_after_the_last(engine, current_epoch, serpapi_fixture_adapter, respx_mock) -> None:
    ... # two searches (page 1 with a cursor, page 2): the account route is called before each search (2 calls) and once more by traverse_cell after
    # the last one (Task 14; here asserted through `ledger.record_account` on the recorded `account_after` fixture): the window's observed fields and
    # `hour_observed_at` advance each time; a 500 from account.json before the first search → Denied("window_cap"), the search route has 0 calls;
    # an account observation older than the window's `hour_observed_at` changes nothing (step 0)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/integration/test_receipts.py tests/integration/test_acquire.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'jobhunter.sources.receipts'`

- [ ] **Step 3: Implement `receipts.py` and `acquire.py`**

- The adapter fixtures of `tests/support/sources.py`: `jsearch_fixture_adapter` (a `JSearchAdapter` on a client with the fixture transport), `jsearch_html_adapter` (routes to `search_v2_html`), `serpapi_fixture_adapter` (`account.json` and `search.json` on respx, so the account calls can be counted), `arbeitnow_adapter` (respx-mocked). `tests/support/collect.py` gains `jsearch_cell(engine) -> int` (the JSearch default instance's `remote-de` cell for a test query) and `page1_spec(cell_id: int) -> RequestSpec`.
- `acquire_page` never raises `SourceError`; it raises `FencedOut` (the owner lost the run) and lets `Bug`-class errors propagate.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/integration/test_receipts.py tests/integration/test_acquire.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock backend/src/jobhunter/sources tests
git commit -m "feat(m1): acquisition receipts with zstd bodies and dedup; the one-page protocol with reservation, dispatch marker, settlement, replay order and retries"
```

---

### Task 13: Graph skeleton, outcome reducer, the planner, the `plan` node and `plan_fallback`

**Files:**
- Create: `backend/src/jobhunter/graphs/outcomes.py`, `graphs/collect/state.py`, `graphs/collect/deps.py`, `graphs/collect/nodes/__init__.py`, `graphs/collect/nodes/plan.py`, `graphs/collect/nodes/plan_fallback.py`, `backend/src/jobhunter/quota/planner.py`, `backend/src/jobhunter/sources/coverage.py` (the `freshness_bucket` and `adzuna_max_days_old` functions; the rest in Task 14)
- Modify: `backend/src/jobhunter/graphs/collect/graph.py` (`build_collect_graph`; `build_noop_collect_graph` stays for the M0 tests), `backend/src/jobhunter/worker/runs.py` (`_invoke` builds the full initial `CollectState`)
- Test: `tests/unit/test_outcomes.py`, `tests/unit/test_planner.py`, `tests/unit/test_freshness.py`, `tests/integration/test_plan_node.py`

**Interfaces:**
- Consumes: Tasks 5–12.
- Produces:
  - `class outcomes.OutcomeConflict(DomainError)`; `outcomes.merge_outcomes(a: dict[str, FetchOutcome], b: dict[str, FetchOutcome]) -> dict[str, FetchOutcome]` (idempotent on equal values, raises on a differing value for one `work_id`); `outcomes.sum_counters(a: dict[str, int], b: dict[str, int]) -> dict[str, int]`
  - `class state.FetchOutcome(TypedDict)` and `class state.CollectState(TypedDict)` exactly as §9.2, plus `steps: Annotated[list[str], operator.add]`, `node_durations: Annotated[dict[str, float], sum_counters]` and `final_status: str | None`; `class state.CollectUpdate(TypedDict, total=False)` with the same keys; `state.initial_state(run_id, attempt_no) -> CollectState`
  - `@dataclass class deps.CollectDeps: engine: AsyncEngine; adapters: Callable[[InstanceRow], SourceAdapter]; embedder: EmbedderClient | None; clock: Callable[[], datetime]; sleep: Callable[[float], Awaitable[None]]; admission_stopped: Callable[[], bool]` (`EmbedderClient` is Task 22's; `None` until then)
  - `graph.build_collect_graph(checkpointer: BaseCheckpointSaver[str], deps: CollectDeps) -> CollectGraph`: the §9.2 edges `START → plan → fetch_primary (Send fan-out to fetch_cell) → plan_fallback → [fetch_fallback | parse_normalize] → resolve_identity → embed → hard_filters → enqueue_targets → finalize → END`, each node with its §9.2 timeout, `RetryPolicy(max_attempts=3, initial_interval=0.5, backoff_factor=2, jitter=True, retry_on=graph.is_retryable_db_error)` on the non-I/O nodes, and an `error_handler` that persists typed `SourceError`/`EmbedderError` outcomes and lets `Bug`, `OutcomeConflict` and `FencedOut` propagate. In this task the nodes after `plan_fallback` are the pass-through functions of their modules (`return {"steps": [name]}`); Tasks 14–23 replace them.
  - `graph.is_retryable_db_error(exc: BaseException) -> bool` (SQLSTATE `40001`, `40P01`, or a `DBAPIError` with `connection_invalidated`)
  - `@dataclass(frozen=True) class planner.CellRow` (every `coverage_cells` column plus `source_id`, `instance_key`, `query_text`, `query_lang`, `exploration`, `location_label`, `radius_km`); `planner.cell_plan(cell: CellRow, instance: InstanceRow, *, phase, request_class, bucket, window, max_days_old) -> base.CellPlan` (the adapter-facing plan of Task 5; `CoverageWindow` is `base.CoverageWindow`)
  - `planner.location_keys(scope: ScopeSettings) -> list[tuple[str, str | None]]` (`(slug, label)` per scope location, de-duplicated on the slug, plus `("remote-de", None)` when the toggle is on); `async def planner.ensure_cells(conn, settings: RunSettings, instances: Sequence[InstanceRow]) -> list[CellRow]` (paid and quota: `(instance, query, location_key)` for every enabled query and key; Arbeitnow one cell; one cell per enabled ATS instance; `ON CONFLICT DO NOTHING`, then read back)
  - `planner.priority(cell: CellRow, now: datetime) -> float`; `planner.order_paid(cells: Sequence[CellRow], *, now, units_since_exploration: int) -> list[CellRow]` (priority order; an exploration cell first when `units_since_exploration ≥ 10`)
  - `coverage.BUCKETS: Final = {"today": timedelta(hours=24), "3days": timedelta(hours=72), "week": timedelta(hours=168), "month": timedelta(hours=720)}`; `coverage.freshness_bucket(now, last_covered_at: datetime | None) -> tuple[str, CoverageWindow, CoverageWindow | None]` (the bucket, the window `[now − span, now]`, and the unreachable part `[last_covered_at, now − 720 h]` to abandon when the span exceeds 720 h); `coverage.adzuna_max_days_old(now, last_covered_at, max_ad_age_days) -> int`
  - `@dataclass(frozen=True) class planner.PlanReport: primary_fetch_request_ids: list[int]; skipped: dict[str, int]; cells: int; replanned: int`; `async def planner.plan_run(conn, *, owner: RunOwner, include_paid: bool, settings: RunSettings, instances: Sequence[InstanceRow], now, account: AccountObservation | None) -> PlanReport` (the §9.2 node-1 behaviour: the receipt (existing `fetch_requests` of the run are reused, `skipped(shutdown|superseded)` re-planned as `planned` rows with `attempt_seq + 1`, up to 8); otherwise the free cells of enabled, `available`/`half_open` instances every run, Adzuna by `project_allowance` with rotation by staleness, JSearch by priority and `project_allowance` (`paced`, `reserve_held`, `exhausted`, `window_cap`, `awaiting_window_validation` as `skipped` rows); an instance that is `blocked` gives `skipped(breaker_open)` rows; a `half_open` instance gets exactly one spec; a disabled source (settings or health `disabled`) gives no rows; the unreachable part of a window is abandoned at once (`coverage.abandon_span`, Task 14 — here through a `coverage_intervals` insert with reason `beyond_provider_window`); SerpApi cells get no primary spec)
  - `async def fallback.cell_outcome(conn, run_id: int, cell_id: int) -> CellOutcome` (in `quota/fallback.py`: from the cell's last fetch request of the run — `succeeded` → `served`; `skipped(<code>)` → that code, except `awaiting_window_validation`, which is `awaiting_after_failed_probe` when the JSearch window has `probe_failed` and `awaiting_probe_in_flight` otherwise; `failed(<error_code>)` → `transient`, or the instance's health for `breaker_open`/`auth_failed`/`contract_broken`; `ambiguous` → `ambiguous`; `skipped(shutdown|run_cancelled)` by name)
  - `async def planner.plan_fallback(conn, *, owner: RunOwner, settings, instances, now, account) -> list[int]` (§6.6: for every JSearch cell of the run whose `cell_outcome` is eligible, one SerpApi spec `phase='fallback'` on the SerpApi cell with the same `(query_id, location_key)`, subject to `project_allowance` on `serpapi` with the account observation (a free read made by the node for the projection only; the acquisition protocol reads the account again immediately before every search, Task 12); records `quota.fallback_active` info when any is planned)
  - `nodes/plan.make_plan(deps) -> node` (loads the run and `load_run_settings`, `list_instances`, the SerpApi account when SerpApi is enabled (projection only), calls `plan_run`, returns `settings_version_id`, `scope_version_id`, `llm_tuple_id`, `plan_done`, `primary_fetch_request_ids`); `nodes/plan_fallback.make_plan_fallback(deps) -> node` and `nodes/plan_fallback.route(state) -> Literal["fetch_fallback", "parse_normalize"]`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_outcomes.py
import pytest
from jobhunter.graphs.outcomes import OutcomeConflict, merge_outcomes, sum_counters
A = {"fr:1": {"work_id": "fr:1", "status": "succeeded", "items": 10, "new_postings": 3}}
def test_reducer_is_idempotent_commutative_and_conflicting() -> None:
    B = {"fr:2": {"work_id": "fr:2", "status": "failed", "items": 0, "new_postings": 0}}
    assert merge_outcomes(A, A) == A and merge_outcomes(A, B) == merge_outcomes(B, A) == {**A, **B}
    with pytest.raises(OutcomeConflict): merge_outcomes(A, {"fr:1": {**A["fr:1"], "items": 9}})
    assert sum_counters({"a": 1}, {"a": 2, "b": 1}) == {"a": 3, "b": 1}
```

```python
# tests/unit/test_planner.py
from datetime import UTC, datetime, timedelta
from jobhunter.quota.planner import order_paid, priority
T = datetime(2026, 10, 16, tzinfo=UTC)
def cell(**k): return CellRow(**{**DEFAULT_CELL, **k})
def test_priority_formula() -> None:
    assert priority(cell(last_attempted_at=None), T) == 10_000.0
    assert priority(cell(last_attempted_at=T - timedelta(hours=48), yield_ema=0.2), T) == 48 * 1.0 * 0.7
    assert priority(cell(last_attempted_at=T - timedelta(hours=48), yield_ema=0.2, zero_yield_streak=3), T) == 48 * 0.25 * 0.7
    assert priority(cell(last_attempted_at=T - timedelta(days=8), yield_ema=0.0, zero_yield_streak=5), T) == 192 * 1.0 * 0.5  # a demoted cell is refreshed after 7 days
def test_exploration_goes_first_after_ten_units() -> None:
    normal, explore = cell(id=1, last_attempted_at=None), cell(id=2, exploration=True, last_attempted_at=T - timedelta(hours=1))
    assert [c.id for c in order_paid([normal, explore], now=T, units_since_exploration=9)] == [1, 2]
    assert [c.id for c in order_paid([normal, explore], now=T, units_since_exploration=10)] == [2, 1]
```

```python
# tests/unit/test_freshness.py
from datetime import UTC, datetime, timedelta
from jobhunter.sources.coverage import adzuna_max_days_old, freshness_bucket
T = datetime(2026, 10, 16, 6, 0, tzinfo=UTC); H = timedelta(hours=1)
def test_bucket_selection_and_abandoned_span() -> None:
    assert freshness_bucket(T, None)[0] == "month" and freshness_bucket(T, T - 1 * H)[0] == "3days"  # needs ≥ 25 h: the smallest bucket ≥ that is 3days
    assert freshness_bucket(T, T - 48 * H) == ("3days", (T - 72 * H, T), None) and freshness_bucket(T, T - 100 * H)[0] == "week"
    bucket, window, abandoned = freshness_bucket(T, T - 800 * H)
    assert (bucket, window, abandoned) == ("month", (T - 720 * H, T), (T - 800 * H, T - 720 * H))
    assert adzuna_max_days_old(T, T - timedelta(days=2, hours=1), 30) == 4 and adzuna_max_days_old(T, None, 30) == 30 and adzuna_max_days_old(T, T - timedelta(days=60), 30) == 30
```

```python
# tests/integration/test_plan_node.py
async def test_plan_creates_cells_and_specs_for_every_enabled_source(engine, current_epoch, fake_http) -> None:
    ... # a query ("python developer", remote-de only) + a greenhouse company `getyourguide`; an include_paid run → cells: jsearch×1, serpapi×1, adzuna×1, arbeitnow×1, ats×1;
    # fetch_requests: jsearch 1 planned (class probe, params date_posted=month), adzuna 1 planned (max_days_old=30), arbeitnow 1, greenhouse 1; serpapi 0; report.skipped == {}
    # with include_paid=False: no jsearch row, adzuna still planned
async def test_plan_is_a_receipt_and_replans_shutdown_skips(engine, current_epoch, fake_http) -> None:
    ... # a second plan_run returns the same ids and inserts nothing; a row set skipped(shutdown) → a new planned row attempt_seq 2 for the same request_key; replanned == 1
async def test_plan_skips_closed_instances_and_probes_half_open(engine, current_epoch, fake_http) -> None:  # Review Focus 5, the planning half
    ... # the greenhouse instance `not_found` with half_open_after in the future → its spec is skipped(breaker_open); with half_open_after in the past → exactly one planned spec
async def test_plan_fallback_follows_the_eligibility_table(engine, current_epoch, fake_http) -> None:
    ... # JSearch cell outcomes `exhausted` and `contract_broken` → one SerpApi fallback spec each on the matching serpapi cell; `paced` and `ambiguous` → none; `quota.fallback_active` info open
async def test_graph_runs_plan_and_pass_through_nodes_on_the_real_worker_protocol(engine, current_epoch, migrated, fake_http) -> None:
    ... # execute_run(engine, build_collect_graph(saver, deps), rid, 1, epoch=1) == "started"; the run `succeeded`; fetch_requests exist for the free cells; state steps include every node name
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_outcomes.py tests/unit/test_planner.py tests/unit/test_freshness.py tests/integration/test_plan_node.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'jobhunter.graphs.outcomes'`

- [ ] **Step 3: Implement the reducer, the state, the planner, the two nodes and the graph**

- `plan_run` builds every spec through `adapter.plan(cell_plan)` and `insert_fetch_request`; the coverage window is computed once here and travels in `RequestSpec.window` (persisted in `fetch_requests.spec`), so a resumed attempt reuses it (§5.5.3 re-planning, Task 14). `project_allowance` is called once per provider with the ordered specs. The JSearch `units_since_exploration` is the count of normal-class JSearch ledger entries since `max(coverage_cells.last_exploration_at)` of the JSearch cells.
- `runs._invoke` builds `state.initial_state(run_id, attempt_no)`; the M0 shape (`run_id`, `attempt_no`, `steps`) is a subset, so the M0 tests of `build_noop_collect_graph` still pass.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit tests/integration/test_plan_node.py tests/integration/test_runs.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/src/jobhunter tests
git commit -m "feat(m1): collect_graph skeleton with the outcome reducer, the runtime planner, cells, freshness windows, plan and plan_fallback nodes"
```

---

### Task 14: Cell traversal, the fetch nodes, coverage accounting and the crash cases C1–C4

**Files:**
- Create: `backend/src/jobhunter/sources/traverse.py`, `graphs/collect/nodes/fetch.py`, `tests/crash/test_collect_crashes.py`
- Modify: `backend/src/jobhunter/sources/coverage.py` (the persisted rules), `backend/src/jobhunter/graphs/collect/graph.py` (the `Send` fan-out), `backend/src/jobhunter/worker/main.py` (`CollectDeps` with the fake transport in test env; the full wiring is Task 24, this task adds what the crash tests need), `backend/src/jobhunter/observability/metrics.py` (`FETCH_REQUESTS` counter, labels `source`, `status`)
- Test: `tests/integration/test_traverse.py`, `tests/integration/test_coverage.py`, `tests/crash/test_collect_crashes.py`

**Interfaces:**
- Consumes: Tasks 10–13; `EXECUTIONS`, `afault_point`.
- Produces:
  - `@dataclass(frozen=True) class traverse.TraversalResult: outcomes: list[FetchOutcome]; end: Literal["end", "cursor", "unknown", "ambiguous", "failed", "skipped"]; reason: str | None; pages: int; items: int; cursor_out: str | None; snapshot: SnapshotDraft | None; firehose: tuple[FirehoseState, list[Gap]] | None` (no posting counts: postings exist only after node 6, so the yield is finalized from the tables, below)
  - `async def traverse.traverse_cell(ctx: AcquireContext, plan: CellPlan, first_fetch_request_id: int, *, limiter: RateLimiter) -> TraversalResult`: the window is `plan.window`, read from the planned row's spec (never recomputed on a resumed attempt); pages in a loop, each through `acquire_page` with its own receipt; JSearch page 2 only when `new_native_ids ≥ PAGE2_MIN_NEW` (the count persisted by `acquire_page`) and the reservation is admitted; Adzuna up to `PAGE_CAP` pages, a full page at the cap → `end="unknown"`; Arbeitnow through `plan_legs` (fresh head first, then the seek legs; `n_newer(gap)` counts distinct native ids with `raw_items.item_ts ≥ gap.upper`), `step`, `remaining_low` and the cap; ATS until `END` (Lever/SmartRecruiters offsets), SmartRecruiters detail specs at `SMARTRECRUITERS_DETAIL` concurrency, and membership from the list pages; SerpApi: after the last search of the traversal one more `adapter.account()` read, persisted with `ledger.record_account` (§5.3.2); `admission_stopped()` between pages ends the traversal `skipped('shutdown')`; a `Failed`/`Ambiguous`/`Denied` page ends it with that `end`; every page yields one `FetchOutcome(work_id=f"fr:{id}", new_postings=0)` (the state's outcome is a receipt of the page, the posting count lives in `fetch_requests.new_postings`, written by finalize)
  - `async def coverage.record_search_traversal(conn, cell_id: int, *, window: CoverageWindow, result: TraversalResult, run_id: int) -> CoverageEffect` with `@dataclass(frozen=True) class CoverageEffect: opened: int; closed: int; covered: int` — **intervals only**, replay-safe by the unique span: `END` inserts the `covered` row `W` and closes the open gaps `W` covers; otherwise it inserts the `open` gap `[max(W.lower, coalesce(last_covered_at, W.lower)), W.upper]` with `gap_cursor` and `reason` (`page_cap`, `allowance`, `ambiguous_attempt`, `unknown_continuation`, `source_failed`, `shutdown`); every row `ON CONFLICT (cell_id, kind, span) DO NOTHING` with `run_id`; nothing on `coverage_cells` is written here
  - `async def coverage.record_firehose_traversal(conn, cell_id, state: FirehoseState, gaps: Sequence[Gap], *, run_id) -> CoverageEffect` (the gap and covered rows on the `created_at` axis, the same idempotency); `async def coverage.record_ats_snapshot(conn, cell_id, instance_id, draft: SnapshotDraft, *, run_id, taken_at) -> int` (the `ats_snapshots` row with `status` `complete` or `incomplete` (the mass classification is Task 19's `apply_snapshot`), its members, and a gap `incomplete_snapshot` otherwise; written once per `(source_instance_id, run_id)`: an existing row is returned, nothing inserted); `async def coverage.abandon_old_gaps(conn, *, now, max_ad_age_days) -> int` (raises `coverage.gap_abandoned` warn per cell)
  - `async def coverage.finalize_cells(conn, run_id: int, *, now) -> dict[int, CellStats]` with `@dataclass(frozen=True) class CellStats: requests: int; new_postings: int; reached_end: bool; covered: bool` — the `coverage_cells` updates of §9.2 node 10, computed from durable rows only and applied **once, in the `finalize` transaction** (Task 23; atomic with `finalize_run`, so a crash keeps or loses both and no later attempt applies them twice): for every cell with a fetch request of the run: `last_attempted_at = min(created_at)` of those requests; `last_success_at` when any succeeded; `requests` = dispatched requests; `new_postings` = postings whose earliest observation belongs to the run's raw items of the cell, written also into each `fetch_requests.new_postings`; `yield_ema` EMA α 0.3 of `new_postings / requests` (unchanged when `requests = 0`), `zero_yield_streak`; `last_covered_at` advanced to `W.upper` of the run's `covered` row under the three §5.6 conditions (`NULL` before; `W.lower ≤ last_covered_at`; every gap between them `closed`/`abandoned`), `last_complete_snapshot_id` for a complete ATS snapshot; `consecutive_incomplete` reset to 0 by a covered run and `+ 1` by a run that opened a gap; `last_exploration_at` for exploration cells; `continuation` = the JSearch cursor of the last succeeded page with a cursor plus `obtained_at`; `coverage.too_broad` info at `consecutive_incomplete ≥ 3` for a paid cell, resolved when covered
  - `nodes/fetch.make_fetch_primary(deps)`, `make_fetch_fallback(deps)` (the `Send("fetch_cell", {"fetch_request_id": id})` fan-out over the planned ids of the phase, `max_concurrency=4`), `make_fetch_cell(deps)` (loads the planned row and its cell, builds the `CellPlan` from the stored spec and the adapter, holds the per-source semaphore (`deps`-level `FetchContext` with the §9.2 limits), runs `traverse_cell`, then in one transaction (owner `FOR SHARE` first) the `record_*` writes, then `afault_point("collect.after_coverage_commit")`, and returns `outcomes` and `counters`); `fetch.route_after_primary(state) -> "plan_fallback"`
  - Fault points: `collect.after_reserve`, `collect.after_dispatch_marker`, `collect.after_response`, `collect.after_settle` (Task 12), `collect.after_coverage_commit` (here) and `collect.before_parse` (the start of node 5, Task 15; the C4 test waits for it)

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_coverage.py
async def test_search_coverage_rules(engine, current_epoch) -> None:
    ... # a cell covered at day 1 06:00; a run at day 3 06:00 with window [day 0 06:00, day 3 06:00] (the window read from the planned spec):
    # (a) end="end" → one `covered` row; finalize_cells → last_covered_at == day 3 06:00, consecutive_incomplete 0;
    # (b) end="cursor", reason page_cap → gap [day 1 06:00, day 3 06:00] open; recording it again is a no-op (unique span); finalize_cells → last_covered_at unchanged, consecutive_incomplete 1;
    # (c) a later END whose window covers the gap → gap closed, closed_at set; (d) a gap whose upper bound is older than max_ad_age_days → abandoned by abandon_old_gaps and `coverage.gap_abandoned` open;
    # (e) `beyond_provider_window` abandoned at planning (Task 13) lets contiguity advance past it
async def test_cell_stats_are_finalized_from_durable_rows_once(engine, current_epoch, job_factory) -> None:
    ... # a run with one dispatched request for the cell and 3 postings whose earliest observations come from its raw items, plus 1 older posting re-sighted:
    # finalize_cells → new_postings 3 (the re-sighting is not new), fetch_requests.new_postings == 3, yield_ema from 0.5 == 0.3*3 + 0.7*0.5 == 1.4;
    # calling finalize_cells again in a new transaction for the same run is refused by the caller contract (the run is terminal) — asserted through the crash case below;
    # three runs that opened a gap → consecutive_incomplete 3 and `coverage.too_broad` info; a covered run resolves it and resets the streak
```

```python
# tests/integration/test_traverse.py
async def test_jsearch_page_two_rule(engine, current_epoch, fake_http) -> None:
    ... # page 1 with 10 new ids → page 2 requested (2 outcomes, 2 ledger entries); with 4 of the ids already in postings → no page 2, end="cursor", reason "page_cap", one open gap
async def test_adzuna_page_cap_and_arbeitnow_overlap(engine, current_epoch, fake_http) -> None:
    ... # Adzuna: 3 full pages at the cap → end="unknown" and a gap with reason `unknown_continuation`; Arbeitnow: page 1 reaches the overlap → end="end", newest_covered_upper advanced, no gap
async def test_ats_snapshot_written_once_and_complete(engine, current_epoch, fake_http) -> None:
    ... # greenhouse getyourguide: one ats_snapshots row (complete, member_count 15), 15 members; a resumed traversal (the page replayed) inserts no second snapshot
async def test_shutdown_after_reservation_before_dispatch_releases_and_sends_nothing(engine, current_epoch, fake_http, monkeypatch) -> None:  # Review Focus 4
    ... # admission_stopped flips to True while paused at collect.after_reserve: the request ends skipped(shutdown), the ledger entry not_billed(never_dispatched), the respx route has 0 calls,
    # the traversal end is "skipped" with reason "shutdown", and a new plan_run re-plans the key as attempt_seq 2
async def test_contract_violation_is_visible_to_plan_fallback_in_the_same_run(engine, current_epoch, fake_http) -> None:  # Review Focus 3
    ... # the JSearch cell is served the html fixture: fetch_requests failed('contract'), instance contract_broken; plan_fallback then plans a SerpApi spec for that cell
```

```python
# tests/crash/test_collect_crashes.py
# Each case: a query + settings entry and an include_paid manual run inserted by SQL (tests/support/collect.manual_paid_run), worker A spawned with
# JOBHUNTER_FAKE_SOURCES=1 and JOBHUNTER_FAULT=<point>, exit 86 awaited, worker B spawned, the run awaited to `succeeded` or `partial` at attempt 2.
def test_c1_after_reservation_before_marker(migrated, proc_env, procs, fixture_log) -> None:
    ... # point collect.after_reserve → A exits 86; after B: the attempt-1 ledger row is not_billed/never_dispatched, its fetch request skipped(superseded),
    # an attempt_seq-2 row succeeded with a billed entry; the JSearch window's probe_used is False between A and B and the attempt-2 entry is the probe;
    # exactly one served request for the JSearch page-1 route in `served_requests(fixture_log)` across both processes (the instrument, not a row count), and one raw_responses row
def test_c2_after_marker_before_send_returns(migrated, proc_env, procs, fixture_log) -> None:
    ... # point collect.after_dispatch_marker → the entry ends ambiguous, the request ambiguous, a coverage gap ambiguous_attempt, no attempt_seq 2 for the paid key,
    # `quota.ambiguous_attempt` warn open, the window's probe_failed True with next_probe_at 12 h later; the Arbeitnow (free) page is re-dispatched by B with attempt_seq 2 and succeeds
def test_c3_after_response_before_settle(migrated, proc_env, procs, fixture_log) -> None:
    ... # point collect.after_response → as C2 (no raw_responses row for attempt 1); the fixture log shows the one send of A and none of B for the paid key
def test_c4_after_settle_before_parse(migrated, proc_env, procs, fixture_log) -> None:
    ... # point collect.before_parse → B logs `page_replayed` for the key, exactly one raw_responses row, one billed entry, and parses it (raw_items exist, Task 15)
```

(`fixture_log` is a `tests/support/sources.py` fixture: a fresh file whose path `proc_env` passes as `JOBHUNTER_FIXTURE_LOG` to both workers. The crash case for `collect.after_coverage_commit`, C3b, asserts the finalized cell statistics and therefore lives in Task 23, where `finalize` stops being a pass-through; this task only adds the fault point.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/integration/test_coverage.py tests/integration/test_traverse.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'jobhunter.sources.traverse'` (the crash tests fail until Task 15 gives them `collect.before_parse`; they are run again there)

- [ ] **Step 3: Implement the traversal, the coverage rules, the fetch nodes and the fan-out**

- `traverse_cell` holds one `RateLimiter` for the process (in `CollectDeps`) and the per-source asyncio semaphores of `FetchContext`; the ATS host semaphore is keyed by host.
- `fetch_cell` is one `Send` per planned primary spec; `fetch_primary` returns the `Send` list from a conditional edge, and `fetch_fallback` the same for the fallback ids. The fan-out nodes carry the 15-minute `TimeoutPolicy`.
- `worker/main.py` builds `CollectDeps` (adapters from `instances` + the configured HTTP layer; `embedder=None` until Task 22) and `build_collect_graph` instead of the no-op graph.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/integration/test_coverage.py tests/integration/test_traverse.py -q && uv run pytest tests/crash/test_collect_crashes.py -k "c1 or c2 or c3" -q`
Expected: PASS for both commands (the `-k` filter applies to the crash file only; C4 passes in Task 15)

- [ ] **Step 5: Commit**

```bash
git add backend/src/jobhunter tests
git commit -m "feat(m1): cell traversal with page rules, coverage accounting and gaps, fetch_primary/fetch_fallback fan-out; crash cases C1-C3"
```

---
### Task 15: The `parse_normalize` node, raw items and dispositions, replay of rejected items, crash cases C4 and C5

**Files:**
- Create: `backend/src/jobhunter/graphs/collect/nodes/parse_normalize.py`, `backend/src/jobhunter/sources/items.py`
- Modify: `backend/src/jobhunter/observability/metrics.py` (`RAW_ITEMS` counter, labels `source`, `disposition`), `tests/crash/test_collect_crashes.py` (C4 enabled, C5 added)
- Test: `tests/integration/test_parse_node.py`

**Interfaces:**
- Consumes: Tasks 5–14; `load_response`, `transition`, `lock_run_owner`.
- Produces:
  - `@dataclass(frozen=True) class items.ParseCounts: pending: int; out_of_scope: int; duplicate_in_page: int; rejected: int` with `total` property
  - `async def items.parse_response(conn, raw_response_id: int, *, adapter: SourceAdapter, owner: RunOwner) -> ParseCounts` (one transaction on `conn`, the run row `FOR SHARE` first; a response with `parsed_at` set returns its stored counts and writes nothing; otherwise `adapter.parse(load_response(...))` gives one `raw_items` row per array element with `item_index` preserved: `Parsed` → `pending` with `normalized = NormalizedPosting.model_dump(mode="json")`, `native_id`, `item_ts`; `OutOfScope` → `out_of_scope(reason)`; `DuplicateInPage` → `duplicate_in_page` with `reason_code = f"first_index={n}"`; `Rejected` → `rejected(reason_code)`; `item_sha256 = sha256(canonical_json(element))`; `ON CONFLICT (raw_response_id, item_index) DO NOTHING` so a retried transaction never duplicates a row; then `raw_responses.parsed_at = now()` and `item_count = len(elements)`); a `SourceError`/`ContractViolation` from `parse` (the whole array unreadable) rejects every element with `reason_code = "item_contract"`
  - `async def items.pending_items_of_run(conn, run_id: int) -> list[int]`; `async def items.replay_rejected(conn, *, raw_item_ids: Sequence[int], now, adapters: Callable[[InstanceRow], SourceAdapter]) -> int` (§7.16.1 `rejected → pending`, re-parsed from the stored body when it is not purged and younger than 30 days; the count moved; M4 exposes it)
  - `nodes/parse_normalize.make_parse_normalize(deps)`: `afault_point("collect.before_parse")`; for every `raw_responses` row of the run (through `fetch_requests.run_id`) whose fetch request is `succeeded` and whose `parsed_at IS NULL` (a `failed('contract')` 200 has `parsed_at` set at store time and is never selected): `parse_response` in its own transaction, `afault_point("collect.parse.between_responses")` after each; increments `RAW_ITEMS`; returns `counters` (`raw_items_pending`, `raw_items_out_of_scope`, `raw_items_duplicate_in_page`, `raw_items_rejected`) and `steps`

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_parse_node.py
import pytest, sqlalchemy as sa
from jobhunter.sources.items import parse_response, replay_rejected
pytestmark = pytest.mark.integration
DISPOSITIONS = sa.text("SELECT disposition, count(*) FROM raw_items WHERE raw_response_id=:r GROUP BY disposition")

async def test_one_row_per_element_and_conservation(engine, current_epoch, arbeitnow_adapter, stored_arbeitnow_page) -> None:
    owner, rr = stored_arbeitnow_page  # a served Arbeitnow page1 (30 elements) of an admitted run
    async with engine.begin() as c: counts = await parse_response(c, rr, adapter=arbeitnow_adapter, owner=owner)
    async with engine.connect() as c:
        by = dict((await c.execute(DISPOSITIONS, {"r": rr})).all()); item_count, parsed_at = (await c.execute(sa.text("SELECT item_count, parsed_at FROM raw_responses WHERE id=:r"), {"r": rr})).one()
        pending = (await c.execute(sa.text("SELECT normalized IS NOT NULL, native_id IS NOT NULL, item_ts IS NOT NULL FROM raw_items WHERE raw_response_id=:r AND disposition='pending'"), {"r": rr})).all()
    assert sum(by.values()) == item_count == 30 == counts.total and by["out_of_scope"] >= 1 and all(tuple(p) == (True, True, True) for p in pending) and parsed_at is not None
    async with engine.begin() as c: again = await parse_response(c, rr, adapter=arbeitnow_adapter, owner=owner)
    async with engine.connect() as c: assert (await c.execute(sa.text("SELECT count(*) FROM raw_items WHERE raw_response_id=:r"), {"r": rr})).scalar_one() == 30 and again == counts

async def test_duplicate_in_page_and_rejected_items(engine, current_epoch, crafted_page) -> None:
    ... # a crafted JSearch page: element 3 repeats element 1's job_uid, element 5 has no job_uid: dispositions pending 3, duplicate_in_page 1 (reason_code "first_index=1"), rejected 1 ("item_contract"); the sum equals item_count

async def test_replay_rejected_reparses_from_the_stored_body(engine, current_epoch, crafted_page, adapters) -> None:
    ... # the rejected row → replay_rejected → `pending` with normalized rebuilt once the adapter accepts it (the crafted adapter now tolerates the missing id); a purged body → 0 moved

async def test_superseded_owner_writes_nothing(engine, current_epoch, queue_app, arbeitnow_adapter, stored_arbeitnow_page) -> None:
    ... # begin_new_attempt first; parse_response → FencedOut and no raw_items rows
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/integration/test_parse_node.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'jobhunter.sources.items'`

- [ ] **Step 3: Implement `items.py`, the node and the two crash cases**

The fixtures `stored_arbeitnow_page` (an admitted run with one served Arbeitnow page-1 receipt, as `(RunOwner, raw_response_id)`), `crafted_page` (a JSearch page whose element 3 repeats element 1's `job_uid` and whose element 5 has none, stored the same way) and `adapters` (the fixture-backed adapter factory) go into `tests/support/sources.py`. `tests/crash/test_collect_crashes.py` gains `test_c5_between_responses_resumes_without_duplicates` (point `collect.parse.between_responses` → after B: every 2xx response of the run has `parsed_at`, every `(raw_response_id, item_index)` once, Σ dispositions = `item_count` per response) and runs `test_c4_after_settle_before_parse` (Task 14).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/integration/test_parse_node.py -q && uv run pytest tests/crash/test_collect_crashes.py -k "c4 or c5" -q`
Expected: PASS for both commands

- [ ] **Step 5: Commit**

```bash
git add backend/src/jobhunter tests
git commit -m "feat(m1): parse_normalize node with one raw item per element, dispositions, replay of rejected items; crash cases C4 and C5"
```

---

### Task 16: Hard filters, `hard_filter_results` and the completeness reconciler

**Files:**
- Create: `backend/src/jobhunter/scoring/__init__.py`, `scoring/versioning.py`, `scoring/filters.py`, `graphs/collect/nodes/hard_filters.py`, `tests/support/facts.py`
- Modify: `backend/src/jobhunter/worker/main.py` (`jobhunter.scoring.filters` in `_REGISTERING_MODULES`)
- Test: `tests/unit/test_hard_filters.py`, `tests/integration/test_hard_filter_results.py`

**Interfaces:**
- Consumes: `ScopeSettings`, `Location`, `distance_km`, `company_key`, `SENIORITY_LADDER`, `reconciler`.
- Produces:
  - `versioning.DET_ALGORITHM_VERSION: Final = "1"`
  - `filters.FilterCode = Literal["outside_germany", "beyond_radius", "work_mode_excluded", "employment_type_excluded", "seniority_out_of_range", "excluded_company", "excluded_keyword", "ad_language_excluded", "excluded_industry"]`
  - `@dataclass(frozen=True) class filters.DetFacts: company_key: str; title_norm: str; seniority_tokens: frozenset[str]; structured_seniority: str | None; locations: tuple[Location, ...]; location_kind: str; work_mode_possible: frozenset[str]; work_mode_source: str; remote_countries: tuple[str, ...]; employment_types: frozenset[str]; employment_source: str; salary: Salary | None; industry_structured: str | None; ad_language: str; posted_at_effective: datetime | None` (the det-facts of §8.8 that filters and `det_results` read); `filters.det_facts_hash(facts: DetFacts) -> bytes` (sha256 of the canonical JSON)
  - `@dataclass(frozen=True) class filters.HardFilterResult: passed: bool; reasons: tuple[FilterCode, ...]`; `filters.evaluate(facts: DetFacts, description_text: str, scope: ScopeSettings) -> HardFilterResult` (§11.1, positive evidence only; `seniority_out_of_range` only for `seniority_range.mode == "explicit"`, mapping tokens and the structured level onto `SENIORITY_LADDER` and failing when every evidence level is outside `[min, max]`; `profile_relative` is unknown in M1, Deviation 8)
  - `async def filters.load_facts(conn, job_id: int) -> tuple[DetFacts, str]` (the facts and the current revision's `description_text`); `async def filters.write_result(conn, job_id, scope_version_id: int, result: HardFilterResult, *, inputs_hash: bytes) -> None` (upsert on the primary key, `det_algorithm_version = DET_ALGORITHM_VERSION`, `computed_at = now()`); `async def filters.active_scopes(conn) -> dict[int, ScopeSettings]` (`active_config … FOR SHARE`: the active scope and the pending one when set, from `scope_versions.payload`); `async def filters.refilter_job(conn, job_id: int, scopes: Mapping[int, ScopeSettings]) -> dict[int, HardFilterResult]`
  - Reconciler `hard_filter_completeness` (cadence `heavy`): every unmerged job lacking a row for a scope of `active_scopes`, or whose row has `inputs_hash ≠ jobs.det_facts_hash` or `det_algorithm_version ≠ DET_ALGORITHM_VERSION`, is recomputed under `active_config FOR SHARE`, 500 jobs per pass; returns the count (the target refilter arrives with `score_targets` in M3)
  - `nodes/hard_filters.make_hard_filters(deps)`: the same check restricted to jobs with `updated_at ≥ runs.started_at` of the run (node 8 verification and backfill); returns `counters["hard_filter_backfilled"]`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_hard_filters.py
import pytest
from jobhunter.scoring.filters import evaluate
from tests.support.facts import BERLIN, HAMBURG, LONDON, facts, scope   # Location fixtures with gazetteer ids; `facts(**overrides)` and `scope(**overrides)` builders over the seeded defaults
def test_unknown_passes_and_positive_evidence_fails() -> None:
    assert evaluate(facts(), "", scope()).passed and evaluate(facts(locations=()), "", scope()).passed
    assert evaluate(facts(locations=(LONDON,)), "", scope(remote_within_germany=False)).reasons == ("outside_germany",)
    assert evaluate(facts(locations=(LONDON,), work_mode_possible=frozenset({"remote"}), work_mode_source="structured"), "", scope(remote_within_germany=True)).passed
    assert evaluate(facts(locations=(LONDON,), work_mode_possible=frozenset({"remote"}), work_mode_source="structured", remote_countries=("GB",)), "", scope()).reasons == ("outside_germany",)
    assert evaluate(facts(locations=(AMBIGUOUS_WITH_ONE_DE_CANDIDATE,)), "", scope()).passed
def test_radius_bands_per_scope_location() -> None:
    berlin_25 = scope(locations=(ScopeLocation("Berlin", "geonames:2950159", 52.52, 13.405, 25, "berlin"),))
    assert evaluate(facts(locations=(HAMBURG,), work_mode_possible=frozenset({"onsite"}), work_mode_source="structured"), "", berlin_25).reasons == ("beyond_radius",)
    assert evaluate(facts(locations=(HAMBURG,)), "", berlin_25).passed  # all three work modes: unknown
    assert evaluate(facts(locations=(POTSDAM_40KM,), work_mode_possible=frozenset({"hybrid"}), work_mode_source="text"), "", berlin_25).passed  # between 1× and 2× the radius
    two = scope(locations=berlin_25.locations + (ScopeLocation("Hamburg", "geonames:2911298", 53.55, 9.99, 25, "hamburg"),))
    assert evaluate(facts(locations=(HAMBURG,), work_mode_possible=frozenset({"onsite"}), work_mode_source="structured"), "", two).passed
def test_structured_only_filters_and_keywords() -> None:
    assert evaluate(facts(work_mode_possible=frozenset({"onsite"}), work_mode_source="structured"), "", scope(work_modes=frozenset({"remote"}))).reasons == ("work_mode_excluded",)
    assert evaluate(facts(work_mode_possible=frozenset({"onsite"}), work_mode_source="text"), "", scope(work_modes=frozenset({"remote"}))).passed
    assert evaluate(facts(employment_types=frozenset({"internship"}), employment_source="structured"), "", scope(employment_types=frozenset({"full_time"}))).reasons == ("employment_type_excluded",)
    assert evaluate(facts(company_key="acme"), "", scope(excluded_companies=frozenset({"acme"}))).reasons == ("excluded_company",)
    assert evaluate(facts(title_norm="senior sap consultant"), "we use SAP daily", scope(excluded_keywords=("sap",))).reasons == ("excluded_keyword",)
    assert evaluate(facts(title_norm="senior saperlot engineer"), "we use SAP daily", scope(excluded_keywords=("sap",))).passed  # whole word only, description off by default
    assert evaluate(facts(title_norm="engineer"), "we use SAP daily", scope(excluded_keywords=("sap",), exclude_keywords_in_description=True)).reasons == ("excluded_keyword",)
    assert evaluate(facts(ad_language="other"), "", scope()).reasons == ("ad_language_excluded",) and evaluate(facts(ad_language="unknown"), "", scope()).passed
    assert evaluate(facts(industry_structured="gaming"), "", scope(industries_exclude=frozenset({"gaming"}))).reasons == ("excluded_industry",)
def test_seniority_explicit_range_only() -> None:
    explicit = scope(seniority_range=SeniorityRange("explicit", 0, 0, "junior", "senior"))
    assert evaluate(facts(seniority_tokens=frozenset({"head"})), "", explicit).reasons == ("seniority_out_of_range",)
    assert evaluate(facts(seniority_tokens=frozenset({"senior", "lead"})), "", explicit).passed and evaluate(facts(seniority_tokens=frozenset()), "", explicit).passed
    assert evaluate(facts(seniority_tokens=frozenset({"head"})), "", scope()).passed  # profile_relative: unknown in M1
```

```python
# tests/integration/test_hard_filter_results.py
async def test_write_recompute_and_reconciler(engine, current_epoch, job_factory) -> None:
    ... # a job with det_facts_hash H: write_result for the active scope → row (passed, reasons, inputs_hash H, det_algorithm_version "1");
    # change the job's det facts (hash H2) → reconciler `hard_filter_completeness` recomputes (inputs_hash H2, count 1), a second pass returns 0;
    # a row with det_algorithm_version "0" is recomputed; a job merged away is skipped
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_hard_filters.py tests/integration/test_hard_filter_results.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'jobhunter.scoring'`

- [ ] **Step 3: Implement the filters, the writes, the reconciler and the node**

`job_factory` (`tests/support/collect.py`, also used by Tasks 19 and 20 with `posting_factory`, `board` and `board20`) inserts jobs, postings and observations directly with the given facts. `outside_germany` is positive only when every location is resolved and every candidate is outside `DE`; a remote-capable job (`remote ∈ work_mode_possible`) with the toggle on passes unless structured `remote_countries` is non-empty and excludes `DE`. `beyond_radius` applies only when `work_mode_possible ⊆ {onsite, hybrid}` and the scope has locations; a location whose candidates fall into different bands is unknown.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit tests/integration/test_hard_filter_results.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/src/jobhunter tests
git commit -m "feat(m1): hard filters on positive evidence, hard_filter_results per (job, scope), completeness reconciler and node 8"
```

---

### Task 17: Canonical field selection, the material projection, revisions, occurrences and lineage

**Files:**
- Create: `backend/src/jobhunter/normalize/fields.py`, `backend/src/jobhunter/dedup/__init__.py`, `dedup/revisions.py`, `backend/src/jobhunter/db/ranking.py`, `tests/support/observations.py`
- Test: `tests/unit/test_canonical_fields.py`, `tests/integration/test_revisions.py`

**Interfaces:**
- Consumes: `NormalizedPosting`, `DetFacts`, `det_facts_hash`, `canonical_json`, `content_hash`, `normalize_title`, `company_key`, `detect_language`.
- Produces:
  - `@dataclass(frozen=True) class fields.ObservationView: posting_id: int; observation_id: int; source_instance_id: int; source_id: str; observed_at: datetime; normalized: NormalizedPosting; completeness: str; quality: str`
  - `fields.SOURCE_RANK: Final[Mapping[str, int]]` (`greenhouse`…`smartrecruiters`: 1; `jsearch`, `serpapi`: 2; `arbeitnow`: 3; `adzuna`: 4)
  - `@dataclass(frozen=True) class fields.CanonicalFields`: every `jobs` column the §8.8 table selects (`company_key`, `company_display`, `title`, `title_norm`, `seniority_tokens`, `locations`, `location_kind`, `work_mode_possible`, `work_mode_source`, `remote_countries`, `employment_types`, `employment_source`, `salary`, `industry_structured`, `ad_language`, `completeness`, `posted_at_effective`, `posted_precision`, `source_ids`), the description (`description_text`, `description_completeness`, `description_quality`), `field_provenance: dict[str, Provenance]` (`Provenance(source_instance_id, posting_id, observation_id, observed_at, rule)`), `det_facts: DetFacts`, `det_facts_hash: bytes`, `llm_input: dict[str, JSONValue]` (exactly `title`, `company_display`, `locations` (labels), `work_mode_possible` + `work_mode_source`, `employment_types` + `employment_source`, `ad_language`, `description_completeness`, `description_text`), `material_hash: bytes`, `lineage: dict[str, Provenance]` (per `llm_input` field), `conflicts: tuple[str, ...]` (fields whose structured values disagreed)
  - `fields.select_canonical(observations: Sequence[ObservationView], *, first_seen_at: datetime, watchlist_display: str | None) -> CanonicalFields` (the §8.8 precedence table; ties by most recent `observed_at`, then lowest posting id; `posted_at_effective = min(best-precision provider date, first_seen_at)`, `unknown` when none; `ad_language` structured > `detect_language(description_text)` > `unknown`; `location_kind = multi` for ≥ 2 locations; `source_ids` sorted)
  - `@dataclass(frozen=True) class revisions.RecomputeResult: job_id: int; revision_id: int; revision_created: bool; current_changed: bool; det_facts_changed: bool; tainted_reuse_refused: bool; conflicts: tuple[str, ...]`; `async def revisions.recompute_job(conn, job_id: int, *, now: datetime, fenced_source_ids: frozenset[str] = frozenset()) -> RecomputeResult` (the caller holds the job row `FOR UPDATE`; loads the latest observation of every posting of the job (and of the jobs merged into it), excluding the observations of a fenced source, `select_canonical`, writes the `jobs` columns, `field_provenance`, `det_facts_hash`, `updated_at`; reuses the revision with `(job_id, material_hash)`; when that revision's lineage names a fenced source the reuse is refused **and nothing is created either**, because `UNIQUE (job_id, material_hash)` forbids a second row while it exists: the job keeps its current revision and the result says `tainted_reuse_refused` (M5's purge deletes the tainted revision, after which the same recompute creates it anew; Deviation 24); otherwise inserts `job_revisions(revision_no = max + 1, embedding_status = 'awaiting_embedding')` with its `job_revision_sources` rows; moves `current_revision_id` and inserts a `job_revision_occurrences` row when it changed; raises `dedup.field_conflict` info per conflict)
  - `async def ranking.bump(conn) -> None` (`UPDATE ranking_state SET generation = generation + 1, updated_at = now() WHERE id = 1`, the last statement of a transaction that changes the list, §7.1)

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_canonical_fields.py
from datetime import UTC, datetime, timedelta
from jobhunter.normalize.fields import select_canonical
from tests.support.observations import obs   # obs(source_id, posting_id, observed_at, **normalized overrides) → ObservationView with sane defaults
T = datetime(2026, 10, 16, 9, tzinfo=UTC)
def test_description_and_title_precedence() -> None:
    adz = obs("adzuna", 1, T, description_text="x" * 500 + "…", description_completeness="partial", title_raw="Senior Engineer (m/w/d)")
    js = obs("jsearch", 2, T - timedelta(hours=5), description_text="full text " * 100, title_raw="Senior Engineer")
    gh = obs("greenhouse", 3, T - timedelta(days=1), description_text="ats text " * 100, title_raw="Senior Engineer - Berlin")
    f = select_canonical([adz, js, gh], first_seen_at=T, watchlist_display="Acme GmbH")
    assert f.description_text.startswith("ats text") and f.description_completeness == "full" and f.title == "Senior Engineer - Berlin"
    assert f.field_provenance["description_text"].posting_id == 3 and f.company_display == "Acme GmbH" and f.source_ids == ("adzuna", "greenhouse", "jsearch")
    assert set(f.llm_input) == {"title", "company_display", "locations", "work_mode_possible", "work_mode_source", "employment_types", "employment_source", "ad_language", "description_completeness", "description_text"}
    assert select_canonical([adz, js], first_seen_at=T, watchlist_display=None).description_text.startswith("full text")  # a partial never overwrites a full
def test_work_mode_intersection_salary_and_dates() -> None:
    a = obs("lever", 1, T, work_mode_possible=frozenset({"remote", "hybrid"}), work_mode_source="structured", salary=stated(60000, 80000))
    b = obs("recruitee", 2, T, work_mode_possible=frozenset({"hybrid"}), work_mode_source="structured", salary=predicted(50000, 70000))
    f = select_canonical([a, b], first_seen_at=T, watchlist_display=None)
    assert f.work_mode_possible == frozenset({"hybrid"}) and f.salary.kind == "stated" and f.conflicts == ()
    c = obs("ashby", 3, T, work_mode_possible=frozenset({"onsite"}), work_mode_source="structured")
    g = select_canonical([a, c], first_seen_at=T, watchlist_display=None); assert g.conflicts == ("work_mode_possible",) and g.work_mode_possible == frozenset({"remote", "hybrid"})
    assert select_canonical([obs("jsearch", 1, T, work_mode_possible=frozenset({"remote", "hybrid", "onsite"}), work_mode_source="none")], first_seen_at=T, watchlist_display=None).work_mode_source == "none"
def test_future_provider_date_is_clamped_to_first_seen_at() -> None:  # Review Focus 2
    f = select_canonical([obs("jsearch", 1, T, posted_at=T + timedelta(hours=10), posted_precision="hour")], first_seen_at=T, watchlist_display=None)
    assert (f.posted_at_effective, f.posted_precision) == (T, "hour")
    g = select_canonical([obs("adzuna", 1, T, posted_at=T - timedelta(days=3), posted_precision="ingest"), obs("greenhouse", 2, T, posted_at=T - timedelta(days=5), posted_precision="exact")], first_seen_at=T, watchlist_display=None)
    assert (g.posted_at_effective, g.posted_precision) == (T - timedelta(days=5), "exact")
    assert select_canonical([obs("serpapi", 1, T, posted_at=None, posted_precision="unknown")], first_seen_at=T, watchlist_display=None).posted_precision == "unknown"
def test_det_facts_hash_ignores_the_description() -> None:
    a = select_canonical([obs("jsearch", 1, T, description_text="A " * 100)], first_seen_at=T, watchlist_display=None)
    b = select_canonical([obs("jsearch", 1, T, description_text="B " * 100)], first_seen_at=T, watchlist_display=None)
    assert a.det_facts_hash == b.det_facts_hash and a.material_hash != b.material_hash
```

```python
# tests/integration/test_revisions.py
async def test_revision_reuse_lineage_and_occurrences(engine, current_epoch, job_with_postings) -> None:
    ... # first recompute: revision 1 created, 10 job_revision_sources rows (one per llm_input field), one occurrence, current_revision_id set, embedding_status awaiting_embedding;
    # recompute without change: revision_created False, current_changed False; a title change (new observation): revision 2 + occurrence; the title back: current_revision_id == revision 1,
    # revision_created False, 3 occurrences, 2 revisions; the ranking generation advanced once per current change
async def test_fenced_lineage_is_refused_until_the_purge_removes_it(engine, current_epoch, job_with_postings) -> None:
    ... # revision 1 has lineage `adzuna`; a recompute with fenced_source_ids={"adzuna"} whose material hash equals revision 1's → RecomputeResult(tainted_reuse_refused=True,
    # revision_created=False, current_changed=False), current_revision_id unchanged, still one revision (UNIQUE (job_id, material_hash) forbids a second);
    # DELETE FROM job_revisions WHERE id = <revision 1> (what the M5 purge does) → the same recompute creates revision 2 with that hash and moves current to it
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_canonical_fields.py tests/integration/test_revisions.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'jobhunter.normalize.fields'`

- [ ] **Step 3: Implement `fields.py`, `revisions.py` and `ranking.py`**

`job_with_postings` (`tests/support/collect.py`) inserts a job with postings and observations straight into the tables (the resolution path arrives in Task 18). The `jobs` row's `title_norm` and `seniority_tokens` come from `normalize_title(title)`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit tests/integration/test_revisions.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/src/jobhunter tests
git commit -m "feat(m1): canonical field selection with provenance, material projection, revision reuse, occurrences and lineage"
```

---

### Task 18: Identity resolution, merges, unmerges, conflicts, tombstone keys and fences; node 6 (items); crash cases C5b and C6

**Files:**
- Create: `backend/src/jobhunter/dedup/identity.py`, `dedup/merge.py`, `graphs/collect/nodes/resolve_identity.py`
- Modify: `tests/crash/test_collect_crashes.py` (C5b, C6)
- Test: `tests/integration/test_identity.py`, `tests/integration/test_merge_unmerge.py`, `tests/integration/test_fenced_items.py`

**Interfaces:**
- Consumes: Tasks 15–17; `AdKey`, `is_fenced` (SQL), `transition`, `lock_run_owner`, `refilter_job`, `active_scopes`, `ranking.bump`, `raise_alert`.
- Produces:
  - `identity.collect_keys(n: NormalizedPosting) -> list[AdKey]` (`raw_keys` de-duplicated, exclusive namespaces first)
  - `identity.Resolution = Literal["linked", "out_of_scope", "rejected", "deferred"]`; `async def identity.resolve_item(conn, *, owner: RunOwner, raw_item_id: int, now: datetime) -> Resolution`: the §8.6 transaction on the caller's connection, in the §3.3.3 lock order `active_config` → job rows → owner row → other domain rows → `ranking_state`: `active_config FOR SHARE` first; step 1 (an active `source` fence for the item's source → `out_of_scope('source_purged')`; an existing observation for the item → `linked`; both dispositions are owner writes and take `lock_run_owner` before them); step 2 (plain reads: sighting time `t = raw_responses.fetched_at`; `P` by `(source_instance_id, native_id)`; `K` through `ad_keys` following `merged_into_job_id`); step 3 (the jobs of `P ∪ J` locked `FOR UPDATE` in id order with one `SELECT … WHERE id = ANY(:ids) ORDER BY id FOR UPDATE` — the transaction writes every one of them in step 7, so the spec's `FOR SHARE` would only be upgraded (Deviation 21) — **then** `lock_run_owner`: the owner row after the job rows; a new job for an empty `J` is inserted after the owner lock, nothing can reference it yet; `is_fenced(id)` on any → `deferred('job_fenced')` keeping `normalized`; `J` empty → a new job from `select_canonical([this observation])`, `restored_from_tombstone_id` from `tombstone_keys` on `sha256(ns ‖ value)`; one job → it; several without an exclusive conflict and without a `different` decision → the lowest id, the others merged in step 6; an exclusive conflict → `P`'s job or a new one and an `identity_conflicts` row with `dedup.identity_conflict` info; a `different` decision → the posting's own job); step 4 (the posting inserted `ON CONFLICT DO NOTHING` then locked `FOR UPDATE`, or moved; the observation with `observed_at = t`, `raw_response_sha256`, `content_hash = material_hash`; the sighting effects of §7.16.2 — `seen_count + 1`, `last_seen_at = greatest`, `latest_observation_id` only when `t ≥` the latest's, revival when `t > availability_changed_at`, the absence reset — through `rules.apply_sighting` (Task 19; this task carries the same statements inline and Task 19 moves them into `apply_sighting` unchanged); this is the **only** place a sighting is counted: an ATS snapshot membership (Task 19) revives and resets but never increments `seen_count`); step 5 (`ad_keys` `ON CONFLICT DO NOTHING` owned by the decided job, then `ad_key_sources`); step 6 (`merge.merge_jobs` per merged job, or the conflict row); step 7 (`revisions.recompute_job` for every affected job and `filters.refilter_job` for the active and pending scopes; `ranking.bump` last); then the item `pending → linked` with `observation_id` and `normalized = NULL`. A `DomainError` other than `FencedOut` → `rejected('identity_error')` in a transaction of its own; `FencedOut` propagates.
  - `async def identity.resolve_run_items(engine, owner, *, now, clock) -> dict[Resolution, int]` (every `pending` item of the run, oldest first, one transaction each; `afault_point("collect.identity.in_item_tx")` inside the transaction before the item's final transition); `async def identity.resolve_deferred_items(engine, owner, *, now, limit: int = 1000) -> dict[Resolution, int]` (`deferred` items of earlier runs whose jobs are no longer fenced, oldest first)
  - `async def merge.merge_jobs(conn, *, from_job_id: int, into_job_id: int, kind: Literal["strong_key_merge", "user_confirmed_merge"], evidence: Mapping[str, JSONValue], now) -> int` (§8.6.1 steps 1, 2, 5, 6, 7 with both rows `FOR UPDATE` in id order; the snapshot records the `ad_keys` moved and the group memberships; steps 3 and 4 arrive with `user_job_actions` (M4) and `score_targets` (M3), Deviation 7); `async def merge.unmerge(conn, merge_event_id: int, *, now) -> int` (§8.6.2 steps 1, 2, 3, 5, 6; step 4 with M4); `async def merge.resolve_conflict(conn, conflict_id: int, decision: Literal["separate", "merge"], *, now) -> None` (`separate`: `dedup_decisions(different, conflict_resolution)` for every pair and `resolved_separate`; `merge`: `merge_jobs(user_confirmed_merge)` of the candidates into the posting's job and `resolved_merged`)
  - `nodes/resolve_identity.make_resolve_identity(deps)`: `afault_point("collect.before_resolve_identity")`, `resolve_run_items`, `resolve_deferred_items`, then (Task 19) the snapshots and (Task 20) the grouping; returns `counters` (`raw_items_linked`, `raw_items_out_of_scope`, `raw_items_rejected`, `raw_items_deferred`, `jobs_created`, `merges`, `conflicts`, `revisions_new`)

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_identity.py
import pytest, sqlalchemy as sa
from jobhunter.dedup.identity import resolve_item
from jobhunter.errors import FencedOut
from tests.support.collect import pending_item   # pending_item(engine, owner, source_id, normalized_overrides, *, fetched_at) → raw_item_id, via a stored page and parse_response
pytestmark = pytest.mark.integration

async def test_new_job_posting_observation_keys_revision_and_filters(engine, current_epoch) -> None:
    owner = await queued_run(engine); item = await pending_item(engine, owner, "jsearch", {"native_id": "UID1", "primary_link": "https://stepstone.de/x--14607704-inline.html"})
    async with engine.begin() as c: assert await resolve_item(c, owner=owner, raw_item_id=item, now=utcnow()) == "linked"
    async with engine.connect() as c:
        job = (await c.execute(sa.text("SELECT j.id, j.current_revision_id, j.availability, p.seen_count, p.availability FROM jobs j JOIN postings p ON p.job_id = j.id"))).one()
        keys = (await c.execute(sa.text("SELECT key_ns, key_value, exclusive FROM ad_keys ORDER BY key_ns"))).all()
        item_row = (await c.execute(sa.text("SELECT disposition, observation_id IS NOT NULL, normalized FROM raw_items WHERE id=:i"), {"i": item})).one()
        hf = (await c.execute(sa.text("SELECT count(*) FROM hard_filter_results WHERE job_id=:j"), {"j": job.id})).scalar_one()
        gen = (await c.execute(sa.text("SELECT generation FROM ranking_state"))).scalar_one()
    assert (job.current_revision_id is not None, job.availability, job.seen_count, job[4]) == (True, "active", 1, "active")
    assert [tuple(k) for k in keys] == [("htidocid", "UID1", False), ("pub:stepstone.de", "14607704", True)] and tuple(item_row) == ("linked", True, None) and hf == 1 and gen == 1

async def test_second_sighting_and_replay_are_idempotent(engine, current_epoch) -> None:
    ... # the same (instance, native_id) a day later → same posting, seen_count 2, last_seen_at advanced, 2 observations, 1 job; resolving the same raw_item_id again → "linked" and nothing changes

async def test_strong_key_merge_and_exclusive_conflict(engine, current_epoch) -> None:
    ... # job A (JSearch, htidocid H, stepstone 1) and job B (Adzuna id 9); an Adzuna item with key adzuna:9 (P = B) whose link carries htidocid H's stepstone 1 → B merged into A:
    # merge_events(strong_key_merge, from B, into A), B.merged_into_job_id == A, B's posting on A, every key owned by A, ad_key_sources untouched, A recomputed;
    # a SerpApi item with htidocid H and stepstone 2 → no merge, identity_conflicts(open, posting_job_id = new job, candidate_job_ids = [A]), `dedup.identity_conflict` info open

async def test_different_decision_blocks_the_merge(engine, current_epoch) -> None: ...
async def test_tombstone_key_restores_the_flag(engine, current_epoch) -> None:
    ... # tombstone_keys holds sha256('adzuna' ‖ '9') → the new job has restored_from_tombstone_id
async def test_late_sighting_never_revives(engine, current_epoch) -> None:
    ... # a closed posting with availability_changed_at = T; an item with fetched_at = T − 1 h → observation added, seen_count + 1, still closed, latest_observation_id unchanged
async def test_superseded_owner_is_fenced(engine, current_epoch, queue_app) -> None:
    ... # begin_new_attempt, then resolve_item → FencedOut, no jobs row
```

```python
# tests/integration/test_merge_unmerge.py
async def test_unmerge_restores_postings_keys_and_blocks_a_remerge(engine, current_epoch) -> None:
    ... # after the strong-key merge above: unmerge(event) → B.merged_into_job_id NULL, B's posting back on B, key ownership by the earliest ad_key_sources row per key,
    # dedup_decisions(A, B, different, unmerge), merge_events(unmerge) with reversed_by_event_id on the original; a new sighting carrying both keys creates no merge
async def test_resolve_conflict_separate_and_merge(engine, current_epoch) -> None: ...
```

```python
# tests/integration/test_fenced_items.py
async def test_source_fence_and_job_fence_dispositions(engine, current_epoch) -> None:  # §18.2 "Fenced raw items"
    ... # an active source fence for adzuna → an Adzuna item is out_of_scope('source_purged'); a jobs fence naming the job behind key H → a JSearch item with H is deferred('job_fenced')
    # with normalized kept; after released_at is set, resolve_deferred_items links it (counts {"linked": 1}); a still-fenced one stays deferred and is counted as such
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/integration/test_identity.py tests/integration/test_merge_unmerge.py tests/integration/test_fenced_items.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'jobhunter.dedup.identity'`

- [ ] **Step 3: Implement identity, merge and the node; add the crash cases**

- The `tombstone_keys` hash is `sha256(ns.encode() + b"\x00" + value.encode())`; `tombstone_keys` rows are written by M5's tombstoning, this task only reads them.
- `tests/crash/test_collect_crashes.py` gains `test_c5b_pending_items_are_all_resumed` (point `collect.before_resolve_identity`: after B, `count(linked) + count(out_of_scope) + count(rejected) + count(deferred)` of the run equals the pending count A left, and `count(pending) == 0`) and `test_c6_item_transaction_rolls_back_without_a_partial_posting` (point `collect.identity.in_item_tx` → after A dies the item is still `pending` and `postings` has no row for it; after B it is `linked` and `count(postings) == count(linked items of distinct native ids)`).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/integration/test_identity.py tests/integration/test_merge_unmerge.py tests/integration/test_fenced_items.py -q && uv run pytest tests/crash/test_collect_crashes.py -k "c5b or c6" -q`
Expected: PASS for both commands

- [ ] **Step 5: Commit**

```bash
git add backend/src/jobhunter tests
git commit -m "feat(m1): identity resolution with strong-key merges, exclusive conflicts, unmerge, tombstone keys and purge fences; node 6; crash cases C5b and C6"
```

---

### Task 19: Availability rules, ATS snapshot effects, mass disappearance, the stale sweep

**Files:**
- Create: `backend/src/jobhunter/availability/__init__.py`, `availability/rules.py`, `availability/snapshots.py`
- Modify: `backend/src/jobhunter/dedup/identity.py` (step 4 calls `apply_sighting`), `graphs/collect/nodes/resolve_identity.py` (the snapshot pass), `backend/src/jobhunter/worker/main.py` (`jobhunter.availability.rules`, `jobhunter.availability.snapshots` in `_REGISTERING_MODULES`)
- Test: `tests/integration/test_availability.py`, `tests/integration/test_snapshots.py`

**Interfaces:**
- Consumes: Task 18; `transition`, `raise_alert`, `resolve_alert`, `ranking.bump`, `reconciler`.
- Produces:
  - `rules.JOB_ORDER: Final = ("active", "unknown", "stale", "closed")`; `@dataclass(frozen=True) class rules.SightingEffect: revived: bool; latest_moved: bool`; `async def rules.apply_sighting(conn, posting_id: int, *, observed_at: datetime, observation_id: int) -> SightingEffect` — **observation accounting, called by identity only** (`seen_count + 1`; `last_seen_at = greatest`; `latest_observation_id` when `observed_at ≥` the latest's; `stale | unknown | closed → active` only when `observed_at > availability_changed_at`, a `closed → active` logged `posting_reopened`; `absent_snapshots = 0, first_absent_at = NULL`); `async def rules.apply_membership(conn, posting_id: int, *, taken_at: datetime) -> bool` — **membership effects, called by snapshots only**: the revival rule and the absence reset of a member (§8.11: "a member of any snapshot"), never `seen_count`, `last_seen_at` or `latest_observation_id` (those belong to the observation the item produced, or to no observation when the item failed normalisation); `async def rules.recompute_job_availability(conn, job_id: int) -> bool` (the best of its postings by `JOB_ORDER`; sets `availability_changed_at` and bumps the ranking when it changed); `async def rules.stale_sweep(conn, *, now, stale_ttl_days: int, limit: int = 1000) -> int` (lock order: the jobs of the batch `FOR UPDATE` in id order **first**, then their postings `active → stale` for non-ATS instances with `now − last_seen_at > stale_ttl_days`, then the jobs recomputed, `ranking_state` last; the batch is chosen by a plain read and re-checked after the lock); reconciler `stale_sweep` (cadences `tick`, `daily`; `fence_epoch(conn, ctx.epoch)` opens each batch transaction; `stale_ttl_days` from the active settings)
  - `@dataclass(frozen=True) class snapshots.SnapshotEffect: revived: int; absent_marked: int; closed: int; unknown: int; mass_opened: bool; mass_followup: bool; mass_cleared: bool; mass_confirmed: bool`; `async def snapshots.apply_snapshot(conn, snapshot_id: int, *, now, guard: Callable[[AsyncConnection], Awaitable[None]]) -> SnapshotEffect` (guarded by `applied_at`; lock order: the jobs of the instance's `active`/`unknown` postings and of the members `FOR UPDATE` in id order, then `guard(conn)` (the node passes `lambda c: ledger.lock_run_owner(c, owner)`, the reconciler `lambda c: fence_epoch(c, ctx.epoch)`), then the posting, snapshot and instance rows; members → `apply_membership(taken_at)` (a member without a posting, for example an item that was rejected, is ignored); the snapshot's **absent set** = the instance's `active`/`unknown` postings that are not members; from a structurally complete snapshot `absent_snapshots = LEAST(absent_snapshots + 1, 2)`, `first_absent_at = COALESCE(first_absent_at, taken_at)`, `last_absent_snapshot_id` idempotency, then `active | unknown → closed('ats_absent')` when `absent_snapshots ≥ 2` and `taken_at − first_absent_at ≥ 24 h`; from an incomplete or a mass-disappearance snapshot `active → unknown` without counting; the mass classification against the frozen baseline `last_snapshot_active_count` (> 50% and ≥ 5 absent opens an event: status `mass_disappearance`, `mass_event_snapshot_id`, the absent set written to `ats_snapshot_absences` for the opening snapshot — the event's frozen absent set, never re-derived from mutable posting columns (Deviation 22) — and `ats.mass_disappearance` warn; a follow-up keeps `event_snapshot_id`; otherwise `complete` clears the event and resets the baseline); the confirmation rule of §8.11 over structurally complete snapshots (two later ones ≥ 24 h apart whose members exclude every posting of the frozen set) → `unknown → closed('mass_disappearance_confirmed')` for the frozen set, baseline reset, alert resolved; `applied_at = now`; every touched job recomputed; ranking bumped once)
  - `async def snapshots.acknowledge(conn, snapshot_id: int, *, now) -> int` (the same closure of the frozen set at once; `acknowledged_at`; the count closed); reconciler `ats_mass_events` (cadence `tick`): the confirmation check for every open event (the "after every ATS snapshot" trigger is the node's `apply_snapshot`), with the epoch guard
  - The node's snapshot pass: every `ats_snapshots` row of the run with `applied_at IS NULL`, one transaction each with the owner guard; counters `availability_closed`, `availability_unknown`, `availability_revived`, `mass_events_opened`

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_availability.py
async def test_sighting_rules_and_job_derivation(engine, current_epoch, posting_factory) -> None:
    ... # a stale posting sighted later → active; a closed posting sighted earlier than its change → unchanged, seen_count + 1; a job with postings (closed, unknown) is unknown; (closed, stale) stale
async def test_stale_sweep_touches_aggregators_only(engine, current_epoch, posting_factory) -> None:
    ... # an Adzuna posting with last_seen 31 days ago → stale (reason NULL), its job stale, ranking bumped; a Greenhouse posting with last_seen 100 days ago stays active; 1,000-row batches (limit=2 with 3 rows: 2 then 1)
```

```python
# tests/integration/test_snapshots.py
from datetime import timedelta
H = timedelta(hours=1)
async def test_absence_counting_closes_at_two_absences_a_day_apart(engine, current_epoch, board) -> None:
    ... # board with postings {a, b, c} active, baseline 3; snapshots every 6 h without `a`: t0 (absent 1, first_absent t0), +6 h (2), +12 h (2, no close), +18 h (2), +24 h → a closed('ats_absent');
    # applying the +6 h snapshot twice counts once; an incomplete snapshot at +30 h → b and c unknown, absence not counted; a complete one at +36 h with b → b active, c closed? no: c stays unknown until its own two absences a day apart
async def test_mass_disappearance_open_followup_confirm_and_acknowledge(engine, current_epoch, board20) -> None:
    ... # §8.11 example: 20 active, baseline 20; 06:00 d1 snapshot with 0 members → mass_disappearance, every posting unknown, `ats.mass_disappearance` open, baseline still 20;
    # 12:00 d1, 18:00 d1, 00:00 d2 follow-ups (event_snapshot_id set, nothing closed); 12:00 d2 → confirmed: 20 closed('mass_disappearance_confirmed'), baseline 0, mass_event_snapshot_id NULL, alert resolved;
    # a second board: open then `acknowledge` → closed at once; a third: a follow-up where only 8 of 20 are absent → status complete, event cleared, those 8 counted absent once;
    # the opening snapshot has 20 ats_snapshot_absences rows, and a posting of the frozen set that is later re-sighted by a job item (apply_sighting) is excluded from the closure
async def test_snapshot_membership_never_counts_a_sighting(engine, current_epoch, board) -> None:
    ... # posting a was linked by an item of the run (seen_count 2) and is a member of the run's complete snapshot: after apply_snapshot seen_count is still 2 and
    # last_seen_at unchanged; a stale member not sighted by an item is revived with seen_count unchanged; a member native id without a posting (its item was rejected) is ignored
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/integration/test_availability.py tests/integration/test_snapshots.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'jobhunter.availability'`

- [ ] **Step 3: Implement the rules, the snapshot effects and the reconcilers**

`apply_snapshot` computes a snapshot's absent set as the `active`/`unknown` postings of the instance that are not members; when an event opens, that set is written to `ats_snapshot_absences` and is the event's absent set from then on. The confirmation compares the frozen set with the members of the two later structurally complete snapshots. `identity.resolve_item` keeps calling `apply_sighting` (Task 18); `apply_membership` has no other caller than `apply_snapshot`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/integration/test_availability.py tests/integration/test_snapshots.py tests/integration/test_identity.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/src/jobhunter tests
git commit -m "feat(m1): availability rules, ATS snapshot effects with absence counting and mass disappearance, stale sweep and reconcilers"
```

---

### Task 20: Probable-duplicate groups, shingles, representative election and the group reconciler

**Files:**
- Create: `backend/src/jobhunter/dedup/shingles.py`, `dedup/groups.py`, `dedup/representative.py`, `tests/support/groups.py`
- Modify: `backend/src/jobhunter/graphs/collect/nodes/resolve_identity.py` (the grouping pass), `backend/src/jobhunter/dedup/merge.py` (step 5 leaves the group), `backend/src/jobhunter/worker/main.py` (`jobhunter.dedup.groups` in `_REGISTERING_MODULES`), `backend/src/jobhunter/observability/metrics.py` (`DEDUP_GROUP_REPAIRS` counter), `tests/crash/test_collect_crashes.py` (C6b)
- Test: `tests/unit/test_shingles.py`, `tests/unit/test_group_rules.py`, `tests/integration/test_groups.py`

**Interfaces:**
- Consumes: Tasks 17–19; `fold`, `distance_km`, `merge_jobs`, `ranking.bump`, `lock_waits`.
- Produces:
  - `shingles.shingles(text: str, n: int = 5) -> frozenset[str]` (over `fold(text)` words); `shingles.jaccard(a, b) -> float`; `shingles.containment(small, big) -> float`; `@dataclass(frozen=True) class shingles.TextSide: text: str; completeness: Literal["full", "partial"]`; `shingles.text_confirms(a: TextSide, b: TextSide) -> tuple[bool, float]` (the G5 thresholds; a trailing `…` removed; under 20 shingles exact folded equality)
  - `@dataclass(frozen=True) class groups.JobView: id: int; company_key: str; title_norm: str; seniority_tokens: frozenset[str]; locations: tuple[Location, ...]; location_kind: str; completeness: str; description_text: str; first_seen_at: datetime; availability: str; source_rank: int; ats_boards: frozenset[str]` (`<type>:<board>` of its ATS keys)`; requisition_ids: frozenset[str]; merged: bool`; `groups.compatible_location(a: JobView, b: JobView) -> bool` (G2); `groups.pair_evidence(a: JobView, b: JobView, title_similarity: float) -> GroupEvidence | None` (G1–G5 with `GroupEvidence(title_similarity, text_score, location_rule, requisition_match)`; `None` when any fails); `groups.G4_TITLE: Final = 0.85`, `groups.CANDIDATE_DAYS: Final = 45`, `groups.MAX_MEMBERS: Final = 10`, `groups.G2_KM: Final = 25.0`
  - `@dataclass(frozen=True) class groups.GroupEffect: group_id: int | None; joined: bool; created: bool; left: tuple[int, ...]; reelected: bool; ambiguous: bool`; `async def groups.regroup_job(conn, job_id: int, *, now, guard: Callable[[AsyncConnection], Awaitable[None]]) -> GroupEffect` (§8.7 under `pg_advisory_xact_lock(hashtext(company_key))`, then the job and its candidates `FOR UPDATE` in id order, then `guard(conn)` (owner lock or epoch fence), then the group rows, `ranking_state` last; idempotent: a job already in the right group is confirmed and nothing is written: candidates by `company_key`, `|first_seen_at| ≤ 45 days`, unmerged, no `different` decision; `similarity(title_norm)` from pg_trgm; join the active group whose representative passes G2–G5 and every member passes G1 and has no `different` decision, the group with the higher G5 score when two qualify (`dedup.ambiguous_grouping` info), at most 10 members; else a new group `{A, B}`; a joining `full` twin of a `partial` representative is elected (`fuller_member_joined`); after every representative change members are revalidated and leave (`member_left`); ranking bumped on any change); `async def groups.split_member(conn, group_id: int, job_id: int, *, now) -> None` (`dedup_decisions(different, user_split)` against every other member; a group left with one member is `dissolved`); `async def groups.confirm_group(conn, group_id: int, *, now) -> int` (`merge_jobs(user_confirmed_merge)` of every member into the representative, `dedup_decisions(same, user_confirm)`); `async def groups.leave_group(conn, job_id: int, *, reason: str, now) -> None` (used by `merge_jobs` step 5)
  - `@dataclass(frozen=True) class representative.MemberView: job_id: int; availability: str; completeness: str; source_rank: int; description_length: int; first_seen_at: datetime; eligible: bool`; `representative.elect(members: Sequence[MemberView]) -> MemberView | None` (the six-key order of §8.7); `representative.eligible(availability: str, merged: bool) -> bool` (M1: unmerged and not `closed`; the target clause arrives in M3, Deviation 9); `async def representative.reelect(conn, group_id: int, *, reason: str, now) -> int | None` (sets `representative_job_id`, `election_reason`, `elected_at`, `updated_at`; `unscorable_reason = 'no_active_member'` and `representative_job_id = NULL` when none is eligible; `active → dissolved` under 2 members through `transition`)
  - Reconciler `group_representatives` (cadence `heavy`, `fence_epoch` first): §7.17 #4 as it reads in M1 (an active group has an eligible representative, or `unscorable_reason` and no eligible member); at most one election per group per pass, counted in `DEDUP_GROUP_REPAIRS`
  - The node's grouping pass, after the snapshot pass: `afault_point("collect.identity.before_grouping")`, then `regroup_job` for every unmerged job with a `job_revision_occurrences` row whose `became_current_at ≥ runs.started_at` of the run — the durable trace of "created or changed in this run" (`started_at` is the first attempt's start; the `running → running` self-edge keeps it) — never from an in-memory `RecomputeResult`; a resumed attempt re-runs it harmlessly

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_shingles.py
from jobhunter.dedup.shingles import TextSide, containment, jaccard, shingles, text_confirms
WORDS = [f"wort{i}" for i in range(80)]
def varied(n: int, offset: int = 0) -> str: return " ".join(WORDS[(offset + i) % 80] for i in range(n))   # n distinct words: n − 4 distinct 5-shingles
def test_scores_and_g5_thresholds() -> None:
    assert len(shingles("one two three four five six")) == 2 and jaccard(shingles(varied(40)), shingles(varied(40))) == 1.0
    full = TextSide(varied(60), "full"); partial = TextSide(varied(30) + "…", "partial")   # 56 and 26 distinct shingles, well over the 20-shingle floor
    assert text_confirms(full, full) == (True, 1.0) and text_confirms(partial, full)[0]   # partial/full: containment 26/26 ≥ 0.7
    assert not text_confirms(TextSide(varied(30), "full"), TextSide(varied(30, 40), "full"))[0]   # disjoint shingle sets: Jaccard 0
    assert text_confirms(TextSide("short text", "full"), TextSide("short text", "full")) == (True, 1.0)   # under 20 shingles: exact folded equality
    assert not text_confirms(TextSide("short text", "full"), TextSide("short texts", "full"))[0]
```

```python
# tests/unit/test_group_rules.py
from jobhunter.dedup.groups import pair_evidence
from tests.support.groups import job   # job(**overrides) → JobView with Berlin, {senior}, full text, company "acme"
def test_decision_table() -> None:
    assert pair_evidence(job(id=1), job(id=2, title_norm="senior software engineer"), 1.0) is not None  # "Sr." normalised earlier: identical title_norm
    assert pair_evidence(job(id=1, ats_boards=frozenset({"greenhouse:acme"})), job(id=2, ats_boards=frozenset({"greenhouse:acme"})), 1.0) is None  # G1
    assert pair_evidence(job(id=1, requisition_ids=frozenset({"R1"})), job(id=2, locations=(ZURICH,), requisition_ids=frozenset({"R1"})), 1.0) is None  # Berlin vs Zurich: G2
    assert pair_evidence(job(id=1, source_rank=4), job(id=2, source_rank=4), 1.0) is not None  # Adzuna re-ingests may be grouped
    assert pair_evidence(job(id=1), job(id=2, seniority_tokens=frozenset({"junior"})), 1.0) is None  # G3
    assert pair_evidence(job(id=1), job(id=2), 0.84) is None and pair_evidence(job(id=1), job(id=2), 0.85) is not None  # G4
    assert pair_evidence(job(id=1, locations=(UNRESOLVED,)), job(id=2), 1.0) is None and pair_evidence(job(id=1, location_kind="remote", locations=()), job(id=2, location_kind="remote", locations=()), 1.0) is not None
    assert pair_evidence(job(id=1, locations=(POTSDAM_24KM,)), job(id=2), 1.0) is not None and pair_evidence(job(id=1, locations=(LEIPZIG,)), job(id=2), 1.0) is None
```

```python
# tests/integration/test_groups.py
async def test_group_formation_election_twin_and_member_left(engine, current_epoch, job_factory) -> None:
    ... # A (Adzuna, partial) and B (Adzuna, partial, re-ingest) → one group {A, B}, representative by the order (earliest first_seen); C (Greenhouse, full) joins → elected (`fuller_member_joined`);
    # D with a different seniority token is not grouped; after C's election a member whose text no longer confirms against C leaves (`member_left`); the ranking generation advanced
async def test_split_confirm_dissolve_and_reconciler(engine, current_epoch, job_factory) -> None:
    ... # split_member → dedup_decisions(different) rows, regroup never re-joins; a group with one member left → dissolved; confirm_group → merge_events(user_confirmed_merge) and dedup_decisions(same);
    # a group whose representative is closed → `group_representatives` re-elects once (count 1), a second pass 0; none eligible → unscorable_reason no_active_member
async def test_regroup_serialises_per_company(engine, current_epoch, job_factory, migrated) -> None:
    ... # two transactions regroup jobs of one company: the second waits (lock_waits(migrated) > 0) until the first commits
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/test_shingles.py tests/unit/test_group_rules.py tests/integration/test_groups.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'jobhunter.dedup.shingles'`

- [ ] **Step 3: Implement the modules, the node pass and the reconciler**

`title_similarity` is `SELECT similarity(:a, :b)` (pg_trgm) per candidate pair; candidates are read with one query over `jobs_company`. `tests/crash/test_collect_crashes.py` gains `test_c6b_grouping_resumes_from_occurrences` (point `collect.identity.before_grouping`, with two Adzuna re-ingests of one ad in the fixtures: A dies after its items are linked and before any grouping; after B the two jobs form one active group, so a linked-but-ungrouped revision is never lost).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit tests/integration/test_groups.py tests/integration/test_identity.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/src/jobhunter tests
git commit -m "feat(m1): probable-duplicate groups (G1-G5), shingle confirmation, representative election, split/confirm, group reconciler"
```

---
### Task 21: The embedder service (image, checksum, `/v1/embed`, `/health`) and spike V7

**Files:**
- Create: `embedder/Dockerfile`, `embedder/app.py`, `embedder/health.py`, `embedder/model.lock`, `embedder/download_model.sh`, `scripts/spikes/v7_embedder.py`, `docs/spikes/V7.md`, `tests/embedder/__init__.py`, `tests/embedder/test_app.py`
- Modify: `pyproject.toml` (the `embedder` dependency group; marker `embedder_model`, deselected by default), `.gitignore` (`.model-cache/`)

**Interfaces:**
- Consumes: nothing from the `jobhunter` package (the embedder image does not contain it).
- Produces:
  - `embedder/model.lock` (key=value lines): `repo=Snowflake/snowflake-arctic-embed-l-v2.0`, `revision=ac6544c8a46e00af67e330e85a9028c66b8cfd9a`, `model_id=snowflake-arctic-embed-l-v2.0@ac6544c8a46e00af67e330e85a9028c66b8cfd9a/int8`, `onnx/model_int8.onnx=sha256:4b164a8bd09dd9806e035bdf3c34a2d81848b3db9642ba2e342b8367c00872d8`, `tokenizer.json=sha256:39feb9863a378165ab9c5c689047203d789422966c0c58721c5309fd039a8edc`
  - `embedder/download_model.sh <model-dir>`: `curl` of `https://huggingface.co/<repo>/resolve/<revision>/<file>` for each locked file, `sha256sum --check` against the lock; non-zero exit on a mismatch
  - `embedder/app.py`: `MAX_TOKENS: Final = 1024`, `MAX_BATCH: Final = 16`, `MAX_TEXT_BYTES: Final = 65_536`, `DIM: Final = 1024`, `QUERY_PREFIX: Final = "query: "`; `class ChecksumMismatch(Exception)`; `verify_checksums(model_dir: Path, lock: Path) -> str` (returns `model_id`; raises `ChecksumMismatch` naming the file); `class Model(Protocol): def embed(self, texts: Sequence[str], input_kind: Literal["query", "passage"]) -> tuple[list[list[float]], list[bool], list[int]]` (vectors, truncated flags, token counts); `class OnnxModel(Model)` (`tokenizers.Tokenizer.from_file`, truncation at 1024 and padding per batch, `onnxruntime.InferenceSession` with `intra_op_num_threads` from `EMBEDDER_THREADS` (default 3), the `query: ` prefix for queries only, the pooled output (`sentence_embedding`, or mean pooling over `last_hidden_state` when that is the export's output: V7 records which) L2-normalised as float32); `create_app(model: Model, *, model_id: str, checksum_ok: bool) -> FastAPI` with `GET /health` → `200 {"status": "ok", "model_id", "checksum_ok": true, "dim": 1024}` (503 when `checksum_ok` is false) and `POST /v1/embed` (`{"input_kind": "query" | "passage", "texts": [...]}` → `{"model_id", "vectors": [[1024 floats]], "truncated": [bool], "tokens": [int]}`; 413 for more than 16 texts or a text over 64 KiB; 422 for anything else); `main()` reads `EMBEDDER_MODEL_DIR` (default `/opt/model`), verifies the checksums (exit 78 on a mismatch, logged by file name) and serves on `0.0.0.0:8080`
  - `embedder/health.py` (installed as `/usr/local/bin/embedder-health`): `GET http://127.0.0.1:8080/health` within 3 s, exit 0 iff 200 and `checksum_ok`
  - `embedder/Dockerfile`: `ARG PYTHON_IMAGE`, `ARG UV_IMAGE` like the app image; `uv sync --locked --no-dev --no-install-project --only-group embedder` into `/opt/venv`; `download_model.sh /opt/model` at build; `COPY embedder/app.py embedder/health.py /app/`; `USER 10004:10004`; `EXPOSE 8080`; `CMD ["python", "/app/app.py"]`; runs read-only (nothing is written at run time)
  - `scripts/spikes/v7_embedder.py`: loads `OnnxModel` from a local model directory (downloaded with `download_model.sh` into the gitignored `.model-cache/`), embeds the descriptions of 50 fixture ads at 1,024 tokens, prints docs/s, peak RSS (`resource.getrusage`), the cosine parity against the fp32 export (`onnx/model.onnx` + `model.onnx_data`, 2.27 GB, downloaded into `.model-cache/` only for the spike) and the query-prefix check (a `query: python developer` vector is closer to a Python ad than to a nursing ad, and differs from the same text embedded as a passage)

- [ ] **Step 1: Write the failing tests**

```python
# tests/embedder/test_app.py
import hashlib, math
import pytest
from fastapi.testclient import TestClient
from embedder.app import ChecksumMismatch, create_app, verify_checksums
class FakeModel:
    def __init__(self) -> None: self.calls: list[tuple[list[str], str]] = []
    def embed(self, texts, input_kind):
        self.calls.append((list(texts), input_kind))
        vecs = [[1.0] + [0.0] * 1023 for _ in texts]
        return vecs, [len(t) > 40 for t in texts], [min(len(t.split()), 1024) for t in texts]
def test_health_and_embed_contract() -> None:
    model = FakeModel(); client = TestClient(create_app(model, model_id="m@rev/int8", checksum_ok=True))
    assert client.get("/health").json() == {"status": "ok", "model_id": "m@rev/int8", "checksum_ok": True, "dim": 1024}
    r = client.post("/v1/embed", json={"input_kind": "passage", "texts": ["short", "x" * 50]}); body = r.json()
    assert r.status_code == 200 and body["model_id"] == "m@rev/int8" and len(body["vectors"]) == 2 and len(body["vectors"][0]) == 1024 and body["truncated"] == [False, True]
    assert client.post("/v1/embed", json={"input_kind": "query", "texts": ["q"]}).status_code == 200 and model.calls[-1] == (["q"], "query")
    assert client.post("/v1/embed", json={"input_kind": "passage", "texts": ["x"] * 17}).status_code == 413
    assert client.post("/v1/embed", json={"input_kind": "document", "texts": ["x"]}).status_code == 422
    assert TestClient(create_app(model, model_id="m", checksum_ok=False)).get("/health").status_code == 503
def test_checksum_verification(tmp_path) -> None:
    (tmp_path / "onnx").mkdir(); (tmp_path / "onnx/model_int8.onnx").write_bytes(b"model"); (tmp_path / "tokenizer.json").write_bytes(b"tok")
    lock = tmp_path / "model.lock"
    lock.write_text(f"repo=r\nrevision=v\nmodel_id=r@v/int8\nonnx/model_int8.onnx=sha256:{hashlib.sha256(b'model').hexdigest()}\ntokenizer.json=sha256:{hashlib.sha256(b'tok').hexdigest()}\n")
    assert verify_checksums(tmp_path, lock) == "r@v/int8"
    (tmp_path / "tokenizer.json").write_bytes(b"changed")
    with pytest.raises(ChecksumMismatch, match="tokenizer.json"): verify_checksums(tmp_path, lock)
@pytest.mark.embedder_model
def test_real_model_vectors_are_unit_length_and_prefix_sensitive() -> None:
    ... # needs .model-cache/: OnnxModel; a passage and the same text as a query differ; every vector has L2 norm 1 ± 1e-3; a 2,000-token text is truncated
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/embedder -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'embedder'`

- [ ] **Step 3: Implement the service and run V7**

- `pyproject.toml`: the `embedder` group; `tests/embedder` is type-checked like the other tests (`embedder/` is added to `mypy.ini`'s `files`). `app.py` imports `onnxruntime` and `tokenizers` inside `OnnxModel.__init__` only, so `create_app`, `verify_checksums` and `tests/embedder/test_app.py` run in the base environment (fastapi is a runtime dependency of the api already); CI's `unit` job runs `tests/embedder` (Task 24).
- V7 (free, local): `embedder/download_model.sh .model-cache/arctic` (590 MB), then `uv run --group embedder python scripts/spikes/v7_embedder.py .model-cache/arctic --fp32 .model-cache/arctic-fp32`. Record in `docs/spikes/V7.md`: docs/s at 1,024 tokens, RSS, parity, the output name of the int8 export and the prefix check. Acceptance in this container: ≥ 1.5 docs/s, RSS ≤ 3 GB, parity ≥ 0.99. The VPS run of §22 V7 is repeated at deployment (§23: the VPS is chosen then); a parity below 0.99 switches `model.lock` to `onnx/model_fp16.onnx` (sha256 `0b6846bcf4c0d9e53fdfc70d6dc1bbe3bb40fd205ebb7f0365da5b07064ddccc`).
- `docker build -f embedder/Dockerfile --build-arg PYTHON_IMAGE=… --build-arg UV_IMAGE=… -t jobhunter-embedder:dev .` must succeed and `docker run --rm jobhunter-embedder:dev python -c "..."` must show `/opt/model` verified.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/embedder -q && uv run pytest tests/embedder -m embedder_model -q`
Expected: PASS (the second command needs `.model-cache/`)

- [ ] **Step 5: Commit**

```bash
git add .gitignore pyproject.toml uv.lock mypy.ini embedder scripts/spikes/v7_embedder.py docs/spikes/V7.md tests/embedder
git commit -m "feat(m1): embedder service with pinned arctic-embed-l-v2.0 int8, checksum verification, /v1/embed and /health; spike V7"
```

---

### Task 22: The embedder client, the `embed` node, embeddings, readiness and health, the `awaiting_embedding` reconciler, compose wiring and crash case C7

**Files:**
- Create: `backend/src/jobhunter/embeddings.py`, `graphs/collect/nodes/embed.py`, `tests/support/embedder.py`
- Modify: `backend/src/jobhunter/observability/health.py` (`ReadinessCode` gains `"embedder"`, `check_readiness(…, embedder=…)`, `SystemHealthOut.embedder`), `backend/src/jobhunter/api/{app.py,deps.py,routes/health.py,routes/system.py}` (an `EmbedderClient` in the app state), `backend/src/jobhunter/observability/metrics.py` (`EMBED_SECONDS` histogram), `backend/src/jobhunter/worker/main.py` (`jobhunter.graphs.collect.nodes.embed` in `_REGISTERING_MODULES`, the client in `CollectDeps`), `deploy/compose.dev.yaml` (the `embedder` service, `JOBHUNTER_EMBEDDER_URL`, the worker's `depends_on`), `tests/support/{fixtures,pg}.py` (the fake embedder's URL in `make_config` and `process_env`), `tests/crash/test_collect_crashes.py` (C7)
- Test: `tests/integration/test_embed_node.py`, `tests/integration/test_readiness_embedder.py`

**Interfaces:**
- Consumes: Task 21's HTTP contract; Tasks 17–18; `reconciler`, `raise_alert`, `resolve_alert`.
- Produces:
  - `embeddings.EMBEDDING_MODEL_ID: Final = "snowflake-arctic-embed-l-v2.0@ac6544c8a46e00af67e330e85a9028c66b8cfd9a/int8"`; `embeddings.BATCH: Final = 16`; `embeddings.BATCH_TIMEOUT_S: Final = 120.0`; `embeddings.HEALTH_TIMEOUT_S: Final = 2.0`
  - `class embeddings.EmbedderError(DomainError)`: `kind: Literal["unavailable", "bad_response", "checksum"]`; `@dataclass(frozen=True) class embeddings.Embedded: vector: tuple[float, ...]; truncated: bool; tokens: int`; `@dataclass(frozen=True) class embeddings.EmbedderHealth: ok: bool; model_id: str | None; checksum_ok: bool | None`
  - `class embeddings.EmbedderClient`: `__init__(base_url: str, client: httpx.AsyncClient)`; `async embed(texts: Sequence[str], *, input_kind: Literal["query", "passage"], timeout_s: float = BATCH_TIMEOUT_S) -> list[Embedded]` (`unavailable` on a connect error, a timeout or a 5xx; `bad_response` on a non-JSON body, a wrong count, a wrong dimension, a non-finite value or a `model_id` other than `EMBEDDING_MODEL_ID`; `checksum` on a 503 health); `async health(timeout_s: float = HEALTH_TIMEOUT_S) -> EmbedderHealth` (never raises)
  - `embeddings.embedding_text(llm_input: Mapping[str, JSONValue]) -> str` (`title`, `company_display` and `description_text` joined by newlines); `embeddings.text_sha256(text: str) -> bytes`; `async def embeddings.upsert_embedding(conn, *, model_id: str, text_sha256: bytes, input_kind: str, vector: Sequence[float]) -> int` (`INSERT … ON CONFLICT (embedding_model_id, text_sha256, input_kind) DO NOTHING`, then the id; the vector bound as the `halfvec` literal `[v1,…]`); `async def embeddings.mark_embedded(conn, revision_id: int, *, embedding_id: int, truncated: bool) -> None`
  - `@dataclass(frozen=True) class embeddings.EmbedReport: embedded: int; awaiting: int; truncated: int; unavailable: bool`; `async def embeddings.embed_awaiting(engine, client: EmbedderClient, *, guard: Callable[[AsyncConnection], Awaitable[None]], limit: int | None = None, batch: int = BATCH, fault: str | None = None) -> EmbedReport` (revisions `awaiting_embedding` oldest first, batches of 16; per batch one `embed` HTTP call, `afault_point("collect.embed.after_http")`, then one transaction that **first** runs `guard(conn)` — the post-I/O ownership check: the node passes `lambda c: ledger.lock_run_owner(c, owner)`, the reconciler `lambda c: fence_epoch(c, ctx.epoch)`; `FencedOut` propagates and the attempt is lost, no vector of a superseded owner is committed — then the `embeddings` upserts and `UPDATE job_revisions … WHERE id = ANY(:ids) AND embedding_status = 'awaiting_embedding'` (a revision embedded meanwhile is left alone); `EmbedderError(unavailable)` stops the loop and counts the rest as `awaiting`; `EMBED_SECONDS` observed per batch; `afault_point(fault)` after each batch when given)
  - Two entry points over that loop: `nodes/embed.make_embed(deps)` (`embed_awaiting(…, guard=<owner lock>, fault="collect.embed.between_batches")`; on `unavailable` raises `embedder.unavailable` warn (subject `embedder`) and returns `counters["revisions_awaiting"]`, which makes the run `partial` (Task 23)) and the reconciler `awaiting_embedding` (`embeddings.reconcile_awaiting_embedding(ctx)`, cadence `heavy`: when `client.health().ok`, `embed_awaiting(…, guard=<epoch fence>)` over revisions awaiting for more than 10 min, resolving the alert when none is left)
  - Readiness: `health.ReadinessCode = Literal["db", "migrations", "worker_heartbeat", "embedder"]`; `async def health.check_readiness(engine, *, code_head: str, now: datetime, embedder: EmbedderClient | None) -> ReadinessReport` (the probe `embedder` fails when `health()` is not `ok` with `checksum_ok`); `SystemHealthOut.embedder: EmbedderOut(ok: bool, model_id: str | None, checksum_ok: bool | None)`; `deps.get_embedder(request) -> EmbedderClient`; `create_app` builds the client from `cfg.embedder_url` on an `httpx.AsyncClient` closed by the lifespan
  - `tests/support/embedder.py`: `class FakeEmbedder` (a `ThreadingHTTPServer` on `127.0.0.1:0` answering `/health` and `/v1/embed` with deterministic unit vectors seeded from the text's sha256, `truncated` for texts over 4,000 characters; `down: bool` makes it refuse connections, `checksum_ok`, `dim` and `model_id` configurable; `batches: list[int]` records the size of every embed request; `base_url`); the session fixture `fake_embedder` and `make_config`/`process_env` pass its URL as `JOBHUNTER_EMBEDDER_URL`
  - `deploy/compose.dev.yaml`: service `embedder` (build `embedder/Dockerfile` with the lock's `python`/`uv` args, image `jobhunter-embedder:dev`, `read_only: true`, `user: "10004:10004"`, `cap_drop: [ALL]`, `security_opt`, `networks: [internal]`, `healthcheck: {test: ["CMD", "embedder-health"], interval: 30s, start_period: 120s, start_interval: 2s}`, `mem_limit: 3500m`, `cpus: 3`, `restart: unless-stopped`); `JOBHUNTER_EMBEDDER_URL: http://embedder:8080` for `api` and `worker`; the worker's `depends_on` gains `embedder: {condition: service_healthy}`

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_embed_node.py
import httpx, math, pytest, sqlalchemy as sa
from jobhunter.embeddings import EMBEDDING_MODEL_ID, EmbedderClient, embed_awaiting
from jobhunter.quota import ledger
from tests.support.collect import queued_run
pytestmark = pytest.mark.integration
async def test_batches_dedup_and_truncation(engine, current_epoch, fake_embedder, revisions_awaiting) -> None:
    ids = await revisions_awaiting(engine, 20, duplicate_texts=2, long_texts=1)  # 20 awaiting revisions, two with identical text, one over 4,000 characters
    owner = await queued_run(engine); guard = lambda c: ledger.lock_run_owner(c, owner)   # the node's guard; the reconciler tests pass fence_epoch
    async with httpx.AsyncClient() as http: report = await embed_awaiting(engine, EmbedderClient(fake_embedder.base_url, http), guard=guard)
    assert (report.embedded, report.awaiting, report.truncated, report.unavailable) == (20, 0, 1, False) and fake_embedder.batches == [16, 4]
    async with engine.connect() as c:
        rows = (await c.execute(sa.text("SELECT embedding_status, embedding_id, embedding_truncated FROM job_revisions WHERE id = ANY(:ids)"), {"ids": ids})).all()
        n_emb = (await c.execute(sa.text("SELECT count(*), max(embedding_model_id) FROM embeddings"))).one()
        norm = (await c.execute(sa.text("SELECT sqrt(sum(x*x)) FROM unnest(string_to_array(trim(both '[]' from (SELECT vector::text FROM embeddings LIMIT 1)), ',')::float[]) AS x"))).scalar_one()
    assert all(r.embedding_status == "embedded" and r.embedding_id for r in rows) and sum(r.embedding_truncated for r in rows) == 1
    assert tuple(n_emb) == (19, EMBEDDING_MODEL_ID) and math.isclose(norm, 1.0, abs_tol=1e-2)  # two identical texts share one embeddings row
async def test_unavailable_leaves_awaiting_and_the_reconciler_catches_up(engine, current_epoch, fake_embedder, revisions_awaiting, queue_app) -> None:
    ... # fake_embedder.down = True: report (0, 5, 0, True); the node raises `embedder.unavailable` warn; up again → reconciler `awaiting_embedding` embeds 5 (older than 10 min) and resolves the alert
async def test_bad_response_is_an_error_not_a_write(engine, current_epoch, fake_embedder, revisions_awaiting) -> None:
    ... # fake_embedder.dim = 8 → EmbedderError("bad_response"), no embeddings row, revisions still awaiting; fake_embedder.model_id = "other" → bad_response
async def test_superseded_owner_batch_is_fenced_after_the_http_call(engine, current_epoch, queue_app, fake_embedder, revisions_awaiting, monkeypatch) -> None:
    ... # PausePoint at collect.embed.after_http; while paused, begin_new_attempt supersedes the owner; resume → FencedOut from the guard, no revision of that batch
    # `embedded`, no embeddings row; the reconciler entry point with the current epoch then embeds them (fence_epoch passes)
```

```python
# tests/integration/test_readiness_embedder.py
async def test_readyz_and_health_report_the_embedder(api_client, logged_in_client, fake_embedder, worker_lock_heartbeat) -> None:
    assert (await api_client.get("/readyz")).status_code == 200
    body = (await logged_in_client.get("/api/system/health")).json(); assert body["embedder"] == {"ok": True, "model_id": EMBEDDING_MODEL_ID, "checksum_ok": True}
    fake_embedder.checksum_ok = False
    r = await api_client.get("/readyz"); assert r.status_code == 503 and r.json() == {"failing": ["embedder"]}
    fake_embedder.down = True; assert (await api_client.get("/readyz")).json() == {"failing": ["embedder"]}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/integration/test_embed_node.py tests/integration/test_readiness_embedder.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'jobhunter.embeddings'`

- [ ] **Step 3: Implement the client, the node, the readiness probe, the compose service and C7**

- `tests/crash/test_collect_crashes.py` gains `test_c7_crash_during_embed_leaves_awaiting_then_embedded` (point `collect.embed.between_batches` with ≥ 17 awaiting revisions → after A dies at least 16 are `embedded` and the rest `awaiting_embedding`; after B every revision is `embedded` and `count(embeddings) == count(distinct text_sha256)`).
- `tests/support/collect.py` gains `revisions_awaiting(engine, n, *, duplicate_texts=0, long_texts=0) -> list[int]` (jobs with revisions in `awaiting_embedding`); `tests/support/fixtures.py` gains `worker_lock_heartbeat` (a fresh `worker_heartbeat` row, so the `worker_heartbeat` probe passes). The M0 readiness tests use `api_app`, whose config carries the fake embedder's URL through the session fixture, so `/readyz` keeps answering 200 there.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/integration -q && uv run pytest tests/crash/test_collect_crashes.py -k c7 -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/src/jobhunter deploy/compose.dev.yaml tests
git commit -m "feat(m1): embedder client, embed node with batches of 16, embeddings upsert, readiness and health embedder probes, awaiting_embedding reconciler, dev embedder service; crash case C7"
```

---

### Task 23: `finalize`: run invariants, the run report, the run status and alerts; `enqueue_targets`; the run end from the graph

**Files:**
- Create: `backend/src/jobhunter/graphs/collect/nodes/finalize.py`, `graphs/collect/nodes/enqueue_targets.py`
- Modify: `backend/src/jobhunter/worker/runs.py` (`_finalize` widened to `FinalStatus`; `execute_run`'s durable completion lookup and its `OutcomeConflict` branch; `_invoke` is unchanged and `run_owned`/`ExecutionOutcome` of `worker/executions.py` keep their M0 shape, `Coroutine[…, None]` and status-only), `backend/src/jobhunter/graphs/collect/graph.py` (the real `finalize` and `enqueue_targets` nodes, `collect.before_finalize` kept at the start of `finalize`)
- Test: `tests/integration/test_finalize.py`, `tests/crash/test_collect_crashes.py` (C3b)

**Interfaces:**
- Consumes: every earlier task; `finalize_run`, `observe_run_ends`, `RunEnd` (M0's metrics record), `raise_alert`, `resolve_alert`.
- Produces:
  - `finalize.InvariantCode = Literal["I1", "I2", "I3", "I4", "I5", "I6", "I7"]`; `@dataclass(frozen=True) class finalize.InvariantResult: code: InvariantCode; ok: bool; detail: dict[str, JSONValue]`; `async def finalize.check_invariants(conn, run_id: int, *, settings: RunSettings, outcome_conflicts: int) -> list[InvariantResult]` (§17.2 as SQL: I1 per `raw_responses` of the run Σ dispositions = `item_count` and `count(pending) = 0`; I2 every `fetch_requests` row of the run terminal; I3 for every window touched by the run's ledger entries counted ≤ `limit_units`, and the run's normal units ≤ `per_run_cap`; I4 every `linked` item's observation's posting has a job; I5 vacuous while `active_config.llm_tuple_id IS NULL` (Deviation 11); I6 every revision created in the run `embedded`, else `awaiting` counted (not a failure); I7 `outcome_conflicts == 0`)
  - `finalize.PartialCause = Literal["fetch_failed", "fetch_ambiguous", "source_breaker_open", "source_auth_failed", "source_contract_broken", "source_not_found", "awaiting_embedding", "raw_item_rejected"]`; `async def finalize.partial_conditions(conn, run_id: int) -> list[PartialCause]` (the §7.16.4 list)
  - `finalize.decide_status(invariants: Sequence[InvariantResult], partial: Sequence[PartialCause]) -> tuple[FinalStatus, str | None]` (`failed_invariant(<first failed code>)` when any of I1–I5, I7 fails; `partial(<first cause>)` when any cause; else `succeeded`)
  - `async def finalize.build_report(conn, run_id: int, *, invariants, partial, node_durations: Mapping[str, float], counters: Mapping[str, int], alerts_raised: Sequence[str]) -> dict[str, JSONValue]` with exactly these top-level keys: `sources` (per `"<source_id>/<instance_key>"`: `requests` by status, `units` `{billed, ambiguous, not_billed}`, `items` by disposition, `postings` `{new, updated}`, `availability` transitions by `<from>→<to>`, `gaps` `{opened, closed, abandoned}`, `health`), `identity` (`jobs_created`, `merges`, `conflicts`, `groups_formed`, `groups_changed`, `elections`), `revisions` (`created`, `embedded`, `awaiting`, `targets`: `{}` in M1), `quota` (per provider `{projected, used, window_start}`; `fallback: {used, reason, cells}`), `execution` (`nodes` durations, `invariants` `{code: {ok, detail}}`, `alerts`, `warnings`); no ad text anywhere
  - `async def finalize.finalize_from_graph(deps: CollectDeps, owner: RunOwner, state: CollectState) -> FinalStatus`: `afault_point("collect.before_finalize")`; one transaction, owner `FOR SHARE` first (`ledger.lock_run_owner`): `coverage.finalize_cells` (Task 14: the `coverage_cells` updates of §9.2 node 10, atomic with the run end), `check_invariants`, `partial_conditions`, `decide_status`, `build_report`, the alerts (`run.failed_invariant` critical subject `run:<id>`; `run.partial` warn per cause, subject `collect:<cause>`, and every other cause's alert resolved; `quota.limit_breach` critical when I3 fails; `run.failed` resolved on `succeeded`; `quota.fallback_active` resolved when no fallback spec was dispatched; `coverage.abandon_old_gaps`), then `finalize_run(conn, owner, status=…, reason=…, report=…)` (raises `FencedOut`, which propagates: the attempt is lost and nothing was written); after the commit `observe_run_ends([RunEnd(status, duration)])` with `finalize_run`'s duration (ruling T12-e) and the `run_finished` log line; the dead-man ping arrives in M5 (Deviation 12)
  - `nodes/finalize.make_finalize(deps)` returns `{"final_status": status, "steps": ["finalize"]}`; `nodes/enqueue_targets.make_enqueue_targets(deps)` returns `{"steps": ["enqueue_targets"]}` while `state["llm_tuple_id"] is None` (M3 fills the §9.2 node-9 behaviour)
  - The typed result channel to `execute_run` is **durable, not in-memory**: `run_owned` discards the coroutine's value and `ExecutionOutcome` carries status only (M0 `executions.py`), so `execute_run` does not read graph state. M0's `runs.RunEnd(status, duration_s)` is the metrics record and stays untouched (`observe_run_ends` reads `.duration_s`); the completion lookup has its own record: `@dataclass(frozen=True) class runs.RunCompletion: status: FinalStatus; reason: str | None; finished_at: datetime`; `async def runs.run_completion(conn, run_id: int, attempt_no: int) -> RunCompletion | None` (the run's terminal row of this attempt, `None` while `running`). `execute_run`'s `returned` branch: `run_completion` not `None` → the graph finalized the run itself (its node observed the metrics and logged); only `run_admission`'s return remains; `None` → the M0 behaviour (`_finalize(status="succeeded", reason=None, report={"noop": True})`, which only the no-op graph of the M0 tests reaches). The `raised` branch gains one explicit case before the generic one: `isinstance(outcome.error, OutcomeConflict)` → `_finalize(status="failed_invariant", reason="I7", report={"execution": {"invariants": {"I7": {"ok": False, "detail": {"error_class": "OutcomeConflict"}}}}})`, which raises `run.failed_invariant` critical (the reducer conflicts outside any node, so `finalize` cannot see it; §17.2 I7 and §17.1 say `failed_invariant`, not `failed('bug')`). `_finalize`'s `status` becomes `FinalStatus` and it raises `run.failed` for `failed` and `run.failed_invariant` for `failed_invariant`. The cancellation semantics are unchanged: a caller cancelled during `finalize` writes nothing here, the run's next owner records the outcome

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_finalize.py
import pytest, sqlalchemy as sa
from jobhunter.graphs.collect.nodes.finalize import check_invariants, decide_status, partial_conditions
pytestmark = pytest.mark.integration
async def test_happy_run_succeeds_with_a_full_report(engine, current_epoch, fake_http, fake_embedder, collect_deps) -> None:
    rid = await run_to_the_end(engine, collect_deps)   # a watchlist company + Arbeitnow, the real graph through execute_run
    async with engine.connect() as c:
        status, reason, report = (await c.execute(sa.text("SELECT status, status_reason, report FROM runs WHERE id=:i"), {"i": rid})).one()
    assert (status, reason) == ("succeeded", None) and set(report) == {"sources", "identity", "revisions", "quota", "execution"}
    assert all(v["ok"] for v in report["execution"]["invariants"].values()) and set(report["execution"]["invariants"]) == {"I1", "I2", "I3", "I4", "I5", "I6", "I7"}
    assert report["sources"]["arbeitnow/default"]["requests"] == {"succeeded": 1} and report["identity"]["jobs_created"] > 0 and "plan" in report["execution"]["nodes"]
    async with engine.connect() as c:   # no ad text anywhere: no title and no 40-character slice of any description of the run appears in the report
        texts = (await c.execute(sa.text("SELECT title, description_text FROM jobs j JOIN job_revisions r ON r.id = j.current_revision_id"))).all()
    blob = json.dumps(report, ensure_ascii=False)
    assert texts and not [t.title for t in texts if t.title in blob] and not [t.title for t in texts if t.description_text[:40] in blob or t.description_text[-40:] in blob]
async def test_partial_and_invariant_failures(engine, current_epoch, fake_http, fake_embedder, collect_deps) -> None:
    ... # (a) the JSearch cell served the html fixture → `partial('fetch_failed')`, `run.partial` warn with subject collect:fetch_failed open, `source_contract_broken` also a cause;
    # (b) the embedder down → `partial('awaiting_embedding')`, I6 detail {"awaiting": n}; (c) a raw item forced back to `pending` after resolve → `failed_invariant('I1')` and `run.failed_invariant` critical;
    # (d) an adjustment row pushing the window over its limit → `failed_invariant('I3')` and `quota.limit_breach` critical; (e) a `deferred('job_fenced')` item does not fail I1
def test_decide_status_order() -> None:
    ok = lambda c: InvariantResult(c, True, {}); bad = lambda c: InvariantResult(c, False, {})
    assert decide_status([ok("I1"), bad("I3"), bad("I7")], ["fetch_failed"]) == ("failed_invariant", "I3")
    assert decide_status([ok("I1"), bad("I6")], []) == ("succeeded", None) and decide_status([ok("I1")], ["awaiting_embedding", "fetch_failed"]) == ("partial", "awaiting_embedding")
async def test_execute_run_uses_the_graph_finalize_once(engine, current_epoch, fake_http, fake_embedder, collect_deps) -> None:
    ... # execute_run returns "started"; runs.report is the graph's report (not {"noop": true}); finished_at set once; RUNS counter incremented once;
    # the M0 no-op graph still ends `succeeded` with {"noop": true} (run_completion is None after it returns); observe_run_ends still receives RunEnd(status, duration_s)
async def test_outcome_conflict_ends_failed_invariant_i7(engine, current_epoch, fake_http, fake_embedder, collect_deps) -> None:
    ... # conflicting_graph(saver, deps): a fetch_cell wrapper that returns two differing outcomes for one work_id → the reducer raises OutcomeConflict;
    # execute_run returns "started", the run is `failed_invariant` with status_reason "I7", report.execution.invariants.I7.ok is False, `run.failed_invariant`
    # critical open with subject run:<id>, and no `run.failed` alert
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/integration/test_finalize.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'jobhunter.graphs.collect.nodes.finalize'`

- [ ] **Step 3: Implement the node, the report and the `execute_run` change**

`tests/support/collect.py` gains `collect_deps` (a `CollectDeps` over the fixture adapters, the fake embedder and `utcnow`), `run_to_the_end(engine, deps, *, include_paid=False) -> int` (a manual run inserted, admitted and executed through `execute_run` with `build_collect_graph`; returns the run id) and `conflicting_graph(saver, deps)` (the real graph with `fetch_cell` wrapped to emit two differing outcomes for the same `work_id`). `test_finalize.py` imports `json`. `tests/crash/test_collect_crashes.py` gains `test_c3b_after_coverage_commit_replays_without_double_stats` (point `collect.after_coverage_commit` on the JSearch cell, Task 14 → after B: the page replayed (0 further sends for the key in the fixture log), exactly one gap row for the cell, `consecutive_incomplete == 1` and `yield_ema` equal to the single-application value: the statistics are applied once, by this task's `finalize`).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/integration/test_finalize.py tests/integration/test_runs.py tests/crash -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/src/jobhunter tests
git commit -m "feat(m1): finalize with run invariants I1-I7, the run report, partial conditions and alerts; the graph owns the run end"
```

---

### Task 24: Worker wiring, live and fake adapters, carry-forward 6, M1 retention rules, the `contract` CI job and the free-source smoke workflow

**Files:**
- Create: `backend/src/jobhunter/sources/registry.py`, `deploy/compose.fixtures.yaml`, `.github/workflows/smoke-free-sources.yml`, `tests/live/__init__.py`, `tests/live/test_free_sources_live.py`
- Modify: `backend/src/jobhunter/worker/main.py` (wiring; carry-forward 5 landed in Task 1), `backend/src/jobhunter/worker/runs.py` (carry-forward 6), `backend/src/jobhunter/worker/operations.py` (carry-forward 6; `claim_operation` raises `FencedOut` for a stale epoch), `backend/src/jobhunter/privacy/retention.py`, `.github/workflows/ci.yml` (`contract` job; `tests/embedder` in the `unit` job), `pyproject.toml` (marker `live`), `tests/integration/test_operations.py` (the fenced-claim assertion)
- Test: `tests/integration/test_worker_wiring.py`, `tests/integration/test_retention_m1.py`, `tests/integration/test_fenced_admission_redefers.py`

**Interfaces:**
- Consumes: everything.
- Produces:
  - `registry.build_adapter(instance: InstanceRow, cfg: AppConfig, clients: Mapping[str, httpx.AsyncClient]) -> SourceAdapter` (by `source_id`: `JSearchAdapter` with `cfg.secret("rapidapi_key")`, `SerpApiAdapter` with `serpapi_key`, `AdzunaAdapter` with the two Adzuna secrets, `ArbeitnowAdapter`, the ATS adapters through `ats.adapter_for`; in fake mode the keys are `b"fake"`); `registry.make_clients(cfg) -> dict[str, httpx.AsyncClient]` (one client per source family through `http.make_client`, closed by the worker teardown)
  - `worker_main` step 7 builds `http.configure(cfg)`, the clients, `EmbedderClient(cfg.embedder_url, …)`, `CollectDeps(…, admission_stopped=lambda: self.shutdown.admission_stopped)` and `build_collect_graph(saver, deps)`; step 8 runs `sync_instances(conn, cfg)` before the start reconcilers; `_REGISTERING_MODULES = ("jobhunter.worker.operations", "jobhunter.worker.runs", "jobhunter.quota.ledger", "jobhunter.availability.rules", "jobhunter.availability.snapshots", "jobhunter.dedup.groups", "jobhunter.scoring.filters", "jobhunter.graphs.collect.nodes.embed", "jobhunter.sources.watchlist_validate")`
  - Carry-forward 6 (Deviation 18, ruling (d)): `execute_run` on `foreign_epoch` re-defers `run_collect(run_id, attempt_no)` with `defer_run_attempt` in a transaction of its own (`None` when a job with that queueing lock already waits); `claim_operation` raises `FencedOut` **only** for a stale epoch and keeps returning `None` for an ordinary unclaimable operation (absent, not `queued`, already claimed); `execute_operation` catches `FencedOut` and re-defers `run_operation(operation_id)` under the queueing lock `op:<id>`, so a second re-defer of the same operation is deduplicated by the lock
  - Retention rules appended to `RULES`, in this order after `operations`: `raw_items` (90 d, `created_at`), `raw_bodies` (window `timedelta(0)`, so `cutoff = now`: `body_zstd := NULL, purged_at := :cutoff` where `expires_at < :cutoff AND purged_at IS NULL`; `expires_at` already holds `fetched_at + 30 d` and the deduplication extension, so a 30-day window compared with it would keep bodies for about 60 days), `ats_snapshots` (30 d by `taken_at`, except each instance's `last_complete_snapshot_id`, its `mass_event_snapshot_id` and every `mass_disappearance` snapshot with `acknowledged_at IS NULL` whose event is open), `embeddings` (0 d: rows referenced by no `job_revisions.embedding_id`; `profile_vectors` joins in M2), `coverage_intervals` (397 d by `coalesce(closed_at, created_at)`), `fetch_requests` (397 d by `created_at`, terminal rows; `depends_on=("raw_items",)`)
  - `.github/workflows/ci.yml`: the job `contract` (`uv sync --frozen`, `uv run pytest -W error tests/contract -q`), and the `unit` job's command becomes `uv run pytest -W error tests/unit tests/property tests/embedder -q` (the embedder app tests need no model and no `embedder` group, Task 21); `tests/live/test_free_sources_live.py` (marker `live`; Arbeitnow page 1 and the six public boards of the fixtures, envelope and ≥ 1 item each; never a paid host) run by `smoke-free-sources.yml` (`workflow_dispatch` and `schedule: "23 5 * * 1"`, `uv run pytest -m live tests/live -q`)
  - `deploy/compose.fixtures.yaml`: the `worker` override `environment: {JOBHUNTER_FAKE_SOURCES: "1", JOBHUNTER_FIXTURES_DIR: /data/fixtures}` and the read-only mount `../tests/contract/fixtures:/data/fixtures:ro` (dev only; `fake_mode_in_prod` refuses it in prod)

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_worker_wiring.py
async def test_sync_instances_disables_sources_without_a_secret_and_fake_mode_needs_none(engine, make_config) -> None:
    ... # a worker config without rapidapi_key: sync_instances → jsearch 'disabled' with last_error_code 'missing_secret' and NULL breaker timestamps, adzuna 'disabled', arbeitnow 'ok';
    # with the secrets present again → 'ok'; a config with JOBHUNTER_FAKE_SOURCES=1 and no secrets → all 'ok'; a jsearch instance in `breaker_open` is left alone with its timestamps;
    # an instance `disabled` with last_error_code 'by_user' stays disabled when its secret is present (ruling (c): restoration only from missing_secret)
```

```python
# tests/integration/test_fenced_admission_redefers.py
async def test_foreign_epoch_run_and_fenced_claim_redefer_their_job(engine, queue_app, current_epoch, migrated) -> None:
    ... # epoch 2 becomes current (claim_epoch_and_sweep with the worker_lock fixture); execute_run(…, epoch=1) == "foreign_epoch" and live_job_exists("run_collect", {run_id, attempt_no});
    # an operation claimed with epoch=1 → execute_operation returns, live_job_exists("run_operation", {operation_id}); a second execute_operation with epoch=1 defers nothing more
    # (one live job under op:<id>, the queueing lock); claim_operation for an operation that is already `running` returns None (not FencedOut) with the current epoch
```

```python
# tests/integration/test_retention_m1.py
async def test_m1_windows(engine, queue_app, migrated, m1_rows) -> None:
    ... # raw_items 91 d old deleted and their observations keep raw_item_id NULL; a body whose expires_at is 1 s before `now` is nulled with purged_at == now, one whose expires_at is 1 s after
    # `now` is kept, a deduplicated body whose referencing row raised its expires_at is kept until that later time; fetch_requests 398 d old deleted with their responses;
    # ats_snapshots 31 d old deleted except the instance's last complete one and an unacknowledged mass one; an embedding referenced by no revision deleted; `fetch_requests` skipped when `raw_items` failed
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/integration/test_worker_wiring.py tests/integration/test_fenced_admission_redefers.py tests/integration/test_retention_m1.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'jobhunter.sources.registry'` and `AttributeError` on the missing rules

- [ ] **Step 3: Implement the wiring, the two carry-forwards, the rules and the workflows**

- `actionlint` (the image of `deploy/images.lock`) passes on both workflow files.
- The M0 crash tests keep passing with the real graph: the worker in `test` env with `JOBHUNTER_FAKE_SOURCES=1` (set by `process_env`) serves the fixtures; without a query and a watchlist company a run plans the Arbeitnow cell only.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest -q && uv run pytest tests/crash -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/src/jobhunter deploy .github pyproject.toml tests
git commit -m "feat(m1): worker runs the real collect_graph with live or fixture adapters; fenced admissions re-defer; M1 retention; contract CI job; free-source smoke workflow"
```

---

### Task 25: M1 acceptance

**Files:**
- Create: `tests/integration/test_collect_end_to_end.py`, `tests/crash/test_collect_worker_end_to_end.py`, `scripts/smoke_m1.py`
- Modify: `tests/stack/test_dev_stack.py` (the embedder service is healthy and `/readyz` reports no `embedder` failure), `docs/superpowers/plans/2026-10-16-job-hunter-roadmap.md` (the M1 status row), `docs/spikes/V10.md` (complete)

**Interfaces:**
- Consumes: the dev stack, the CLI, the API, every M1 module.
- Produces: `scripts/smoke_m1.py` (exit 0 when every check passes; one JSON line per check, like `smoke_m0.py`; it never prints a key, a cookie or a password)

- [ ] **Step 1: Write the end-to-end tests**

```python
# tests/integration/test_collect_end_to_end.py
async def test_two_runs_on_fixtures_through_the_real_graph(engine, current_epoch, fake_http, fake_embedder, collect_deps, migrated) -> None:
    # inputs: the query "software engineer" (remote-de), watchlist companies getyourguide (greenhouse) and bunq (recruitee), an include_paid manual run
    rid = await run_to_the_end(engine, collect_deps, include_paid=True)
    async with engine.connect() as c:
        status, report = (await c.execute(sa.text("SELECT status, report FROM runs WHERE id=:i"), {"i": rid})).one()
        pending = (await c.execute(sa.text("SELECT count(*) FROM raw_items WHERE disposition='pending'"))).scalar_one()
        conserved = (await c.execute(sa.text("SELECT bool_and(r.item_count = (SELECT count(*) FROM raw_items i WHERE i.raw_response_id = r.id)) FROM raw_responses r"))).scalar_one()
        jobs, postings, linked = [(await c.execute(sa.text(q))).scalar_one() for q in ("SELECT count(*) FROM jobs", "SELECT count(*) FROM postings", "SELECT count(*) FROM raw_items WHERE disposition='linked'")]
        ledger = dict((await c.execute(sa.text("SELECT provider || ':' || state, count(*) FROM quota_ledger GROUP BY 1"))).all())
        snapshots = (await c.execute(sa.text("SELECT status, member_count FROM ats_snapshots ORDER BY id"))).all()
        embedded = (await c.execute(sa.text("SELECT count(*) FROM job_revisions WHERE embedding_status='embedded'"))).scalar_one()
        groups = (await c.execute(sa.text("SELECT count(*) FROM duplicate_groups WHERE status='active'"))).scalar_one()
        hf = (await c.execute(sa.text("SELECT count(*) FROM jobs j WHERE NOT EXISTS (SELECT 1 FROM hard_filter_results h WHERE h.job_id = j.id)"))).scalar_one()
    assert status == "succeeded" and pending == 0 and conserved and jobs > 0 and postings == linked and hf == 0
    # JSearch: page 1 (probe) plus page 2 (10 new ids ≥ 7); Adzuna: 50 of 113 then the empty page 2
    assert ledger == {"jsearch:billed": 2, "adzuna:billed": 2} and [s.status for s in snapshots] == ["complete", "complete"] and all(s.member_count > 0 for s in snapshots)
    assert embedded == (await count(engine, "SELECT count(*) FROM job_revisions")) and groups >= 1  # the Adzuna re-ingests of one ad form a group
    assert all(v["ok"] for v in report["execution"]["invariants"].values()) and report["quota"]["jsearch"]["used"] == 2
    second = await run_to_the_end(engine, collect_deps, include_paid=False)
    async with engine.connect() as c:
        again = (await c.execute(sa.text("SELECT count(*) FROM jobs"))).scalar_one(); seen = (await c.execute(sa.text("SELECT min(seen_count) FROM postings WHERE source_instance_id IN (SELECT id FROM source_instances WHERE source_id IN ('arbeitnow','greenhouse','recruitee'))"))).scalar_one()
        gaps = (await c.execute(sa.text("SELECT kind, gap_status, reason FROM coverage_intervals ORDER BY id"))).all()
    assert again == jobs and seen == 2 and ("gap", "open", "page_cap") in {tuple(g) for g in gaps}  # the JSearch page-1-with-cursor rule of §5.6 (Carry-forward 5)
```

```python
# tests/crash/test_collect_worker_end_to_end.py
def test_scheduled_run_on_fixtures_through_the_worker_process(migrated, proc_env, procs, fake_embedder) -> None:
    ... # the inputs inserted by SQL (tests/support/collect.seed_inputs_sql), a due slot, the worker spawned with the fake sources; wait for `succeeded` or `partial` within 180 s;
    # runs.report has the five sections; `worker_shutdown_complete` with exit 0 after SIGTERM; no `error` log line without an `error_class`, and no log line containing an e-mail or a key shape
```

- [ ] **Step 2: Write the smoke script**

`scripts/smoke_m1.py` drives the dev stack with `-f deploy/compose.dev.yaml -f deploy/compose.fixtures.yaml` (always with `--env-file deploy/images.lock`), using the M0 script's `Context`, `emit`, `psql` and `poll_health` helpers (imported from `scripts/smoke_m0.py`), with these checks in order:
1. `user_create`, 2. `login`, 3. `health_ready` as in M0 (the body must also show `embedder.ok` true and a `quota` object);
4. `seed_inputs`: `psql` inserts a `queries` row (`software engineer`, `en`, `user`), appends `{"query_id": <id>, "enabled": true}` to the active settings version's `queries[]`, inserts the watchlist company `getyourguide` (greenhouse) with its entry in `watchlist[]` and its source instance;
5. `run_now_paid`: `POST /api/runs` with `{"include_paid": true}` (an `Idempotency-Key`, `Origin`, `X-JH-Request`) → 202;
6. `collect_succeeded`: poll until `last_run.status` is `succeeded` (150 s); then `psql`: `count(*) FROM jobs > 0`, `count(*) FROM raw_items WHERE disposition='pending' = 0`, `count(*) FROM embeddings > 0`, `count(*) FROM quota_ledger WHERE state='billed' = 4` (two JSearch pages, two Adzuna pages), and `report->'execution'->'invariants'` all `ok`;
7. `second_run_idempotent`: a second `POST /api/runs` with `include_paid: false` → after it succeeds, `count(*) FROM jobs` unchanged and `min(seen_count) FROM postings WHERE source_instance_id IN (<arbeitnow, greenhouse>)` is 2.

- [ ] **Step 3: Bring up the stack and run everything**

Run:

```bash
dockerd >/tmp/dockerd.log 2>&1 & sleep 3
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run pytest -q && uv run pytest tests/contract -q && uv run pytest tests/stack -m stack -q && uv run pytest tests/perf -m perf -q
(cd frontend && npm test && npm run build && npm run lint && npm run typecheck) && scripts/check-openapi-drift.sh
docker compose --env-file deploy/images.lock -f deploy/compose.dev.yaml -f deploy/compose.fixtures.yaml down -v
docker compose --env-file deploy/images.lock -f deploy/compose.dev.yaml -f deploy/compose.fixtures.yaml up -d --build --wait
uv run python scripts/smoke_m1.py
```

Expected: every command exits 0, and the smoke script prints seven `{"check": …, "ok": true}` lines. The OpenAPI drift check sees the new `/readyz` failing code and the `embedder`/`quota` health fields (regenerate `frontend/openapi.json` and the types in this task).

- [ ] **Step 4: Close the spikes and the roadmap**

This step is a gate (ruling (f)): `docs/spikes/V10.md` must hold the day-2 result of Task 6 phase 3 (`search_v2_page1_day2` compared with `search_v2_page1`), any identity instability it showed must be resolved in the adapter or the identity rules with its own test, and `CURSOR_REUSE_TTL` must match the interval `V9.md` records; until then Task 25 does not proceed to the commit. Then set the M1 status cell of the roadmap to `complete`; leave the other rows unchanged.

- [ ] **Step 5: Commit, then the final 4-eyes review**

```bash
git add scripts/smoke_m1.py tests docs/spikes docs/superpowers/plans/2026-10-16-job-hunter-roadmap.md frontend/openapi.json frontend/src/api/schema.d.ts
git commit -m "test(m1): end-to-end collection on fixtures through the graph and the worker; acceptance smoke; mark M1 complete"
```

Then the milestone 4-eyes review of the roadmap's quality gate 3: a whole-branch review by Codex GPT-6 Astra (`model_reasoning_effort="high"`, never `xhigh`) and Claude Fable 5.1; findings are fixed and the review repeated until both approve. Record the review in `docs/council/` as the M0 review was.

---

## Deviations

Each deviation stages a spec item across milestones or settles something the spec leaves open. Entries that change user-visible behaviour say so.

1. **Tables and FKs staged across milestones** (continuing M0 Deviation 1). In M1: every table of §7.5, §7.7–7.10 except `profile_vectors` (M2, with `profile_versions`) and `query_proposals` (M2); `queries.proposal_id` keeps its type without an FK until then. `hard_filter_results` (§7.11) is created in M1 because `resolve_identity` and node 8 write it; the rest of §7.11 is M3's. The FK `purge_fences.source_id → sources` and the `source` branch of `is_fenced()` arrive here, as M0 Deviations 1 and 2 said.
2. **`source_instances` CHECK widened** (approved, ruling (b)). §5.2 says `auth_failed`, `contract_broken` and `not_found` *open* the breaker and that a half-open probe follows after 6 h, but the §7.5 CHECK allows `breaker_opened_at`/`half_open_after` only for `breaker_open`. M1 allows them for all four open states so the 6-hour half-open rule applies to each; `record_success` and `instances.disable` clear both timestamps on the way back to `ok` or `disabled` (`test_breaker_timestamps_clear_on_ok_and_disabled`, Task 5). No user-visible change.
3. **Settings lists written directly in M1.** §13.1's `queries[]` and `watchlist[]` entries are authored by M2's settings editing and M4's watchlist API. M1 reads them as the spec says (a query or company counts as enabled only with an entry `enabled: true`); the tests, the crash tests and `smoke_m1.py` write the entries with SQL. No behaviour change.
4. **Layout differences from §4.2.** New: `run_settings.py`, `embeddings.py`, `cli_fixtures.py`, `db/ranking.py`, `sources/{instances,fixtures,receipts,acquire,traverse,coverage,watchlist_validate,registry,items}.py`, `dedup/revisions.py`, `graphs/collect/{state,deps}.py`, `embedder/{health.py,download_model.sh}`, `scripts/spikes/`, `tests/{contract,embedder,live}/`. `geo/data/country_names_de.tsv` is a hand-maintained source of the build.
5. **Environment.** `JOBHUNTER_FIXTURES_DIR` (test and dev only, with `JOBHUNTER_FAKE_SOURCES`) is not in §19.2; `JOBHUNTER_EMBEDDER_URL` is read by the api as well as the worker, because `/readyz` probes the embedder (§17.5). In fake mode `sync_instances` requires no provider secret.
6. **A missing provider secret disables the instance** — a **behaviour change**, approved (ruling (c)). §16.8 lists the provider secrets as optional for the worker and says nothing about a missing one at planning time; without this rule every run would fail the source with `auth_failed`. `sync_instances` sets `health = 'disabled'`, `last_error_code = 'missing_secret'` (breaker timestamps cleared) and restores `ok` **only** from that exact state once the secret is present; an administrative `disabled` without that code and every breaker state are left untouched. The source is skipped and visibly reported as `disabled` in the run report and in `/api/system/health`.
7. **Merge and unmerge steps that need later tables.** §8.6.1 step 3 (user actions) and §8.6.2 step 4 arrive with `user_job_actions` (M4); §8.6.1 step 4 (targets `cancelled('job_merged')`) with `score_targets` (M3). The merge snapshot records what exists.
8. **Hard filters without a profile.** `seniority_range.mode = profile_relative` is unknown in M1 (the filter passes); M2's activation derives the explicit range (§12.6 step g). `too_old` is not stored (§11.1). The target refilter of the `hard_filter_completeness` reconciler arrives in M3.
9. **Group eligibility without targets.** §8.7 eligibility reads the member's active-tuple target; M1 has no tuple, so a member is eligible when its job is unmerged and not `closed`. Deferral (`deferred_duplicate`) and "Score individually" are M3's. The representative election, re-election, split and confirm are complete in M1.
10. **Dry-run.** §6.8 `dry_run()` is M2's API; M1 ships `quota_projections` (the quota, Arbeitnow and `stale_ttl_short` parts) and M2 composes the rest around it.
11. **`enqueue_targets` and I5.** Node 9 does nothing while `active_config.llm_tuple_id` is NULL, which is always the case before M2; I5 is vacuous accordingly. M3 fills both.
12. **The dead-man ping** of `finalize` (§17.7) arrives in M5 with healthchecks; `finalize` has the hook (`deps` carries no pinger in M1).
13. **The `fetch_requests` reason columns.** The M0 `transition()` has one reason column per table; `fetch_requests` has two (`skip_reason`, `error_code`), so both are `settable` and the §7.7 CHECKs enforce their presence. No behaviour change.
14. **Hand-derived fixture variants** (approved, ruling (e)). Recording a 429, a schema drift or an HTML error page from a paid provider costs quota and cannot be forced; those variants are derived from the scrubbed recordings by `scripts/spikes/derive_variants.py`, each with an explicit provenance line in `tests/contract/README.md` (`derived from <file>: <exact change>`). A derivation keeps the wire structure (no key renamed or moved) and the identity relationships of the kept items (a trim drops whole items, never edits one); the pagination and count-mismatch cases are separate variants, so trimming a base fixture never erases those tests.
15. **V7 runs in the dev container.** The VPS of §23 does not exist yet; the spike's thresholds are checked here and repeated at deployment (M5).
16. **The run end from the graph.** `finalize` writes the run end itself; `execute_run` finalizes a graph that returns without `final_status` as `succeeded` `{"noop": true}`, which only the M0 no-op graph does. A `FencedOut` from the node's finalize ends the attempt quietly as before.
17. **The embedder image** is built from the root `pyproject.toml`'s `embedder` dependency group (one lock file for the repository) and contains neither the `jobhunter` package nor any secret.
18. **Carry-forward 6 changes `claim_operation`** (approved, ruling (d)) to raise `FencedOut` for a stale epoch (M0 returned `None`), so `execute_operation` can re-defer only the fenced case; `None` stays the answer for an ordinary unclaimable operation, and the re-defer is deduplicated by the `op:<id>` queueing lock (tested in Task 24).
19. **Body cap.** A response body over 64 MiB is a contract failure (§5.2 is silent); the first 4 KiB is stored like any error body.
20. **`watchlist_validate` and `replay_rejected`** exist as the worker-side operation and function; their API routes are M4's (§14.2).
21. **Lock mode of the job rows in identity.** §8.6 step 3 locks the jobs of `P ∪ J` `FOR SHARE` and only the merge candidates `FOR UPDATE`; the same transaction updates every one of those rows in step 7 (canonical fields, `updated_at`), so a `FOR SHARE` would be upgraded and two transactions sharing a job could deadlock. M1 locks them `FOR UPDATE` in id order in one statement, before the owner row, in the §3.3.3 order. No behaviour change.
22. **`ats_snapshot_absences`.** §8.11 confirms a mass disappearance against the opening snapshot's "absent set" but gives it no durable home; deriving it from `postings.last_absent_snapshot_id` would need an update rule the spec does not define. M1 adds the table (written once when an event opens, cascade-deleted with the snapshot) and reads the frozen set from it. No user-visible change.
23. **Billing is separate from envelope validity; a failed request stores no items.** §5.3 classifies billing by the transport outcome and the HTTP status (a JSearch 2xx is `billed`), and §5.2 keeps the full body of a 2xx; a 200 whose body is not the contract (an HTML error page) is both `billed` and `failed('contract')`. M1 keeps the full body only for a `succeeded` request, stores the first 4 KiB otherwise, and sets `item_count = 0` with `parsed_at` on a request that did not succeed, so I1 holds for an unparseable envelope and `parse_normalize` selects by `fetch_requests.status = 'succeeded'`. A replayed failure keeps its receipt and is never re-sent unless the §5.2 retry rule says so. No user-visible change beyond the report's `requests` counts.
24. **A tainted revision blocks reuse without creating a twin.** `UNIQUE (job_id, material_hash)` forbids a second revision with the same hash, so when the reusable revision's lineage names a fenced source the recompute refuses the reuse and leaves the job on its current revision (`tainted_reuse_refused`) until M5's purge deletes that revision; a recompute whose material differs (the fenced source's observations are excluded from the inputs) creates a new revision as usual. No behaviour visible before M5.

## Carry-forward to later milestones

1. **M2:** `dry_run()` composition around `quota_projections` (§6.8); the Pydantic settings model replaces `run_settings.load_run_settings`; the derived explicit seniority range at profile activation; `query_proposals`, `profile_vectors` and the `queries.proposal_id` FK; the `profile` branch of `is_fenced()`; the authoring of `queries[]`/`watchlist[]` entries.
2. **M3:** `enqueue_targets` (§9.2 node 9) with `similarity` from the profile vectors; the target clause of group eligibility and deferral (§8.7); merge step 4; the target refilter in `hard_filter_completeness`; I5 with an active tuple; `score_targets` revival on unmerge (§8.6.2 step 5).
3. **M4:** the routes of §14.2 that M1's functions back: `/api/watchlist/parse-url` (`watchlist_patterns.detect`), `/api/watchlist` (+ the `watchlist_validate` operation), `/api/system/raw-items/replay` (`items.replay_rejected`), `/api/identity-conflicts/{id}/resolve` (`merge.resolve_conflict`), `/api/duplicate-groups/{id}/{split,confirm}`, `/api/jobs/{id}/unmerge`, `/api/ats-snapshots/{id}/acknowledge` (`snapshots.acknowledge`), `/api/geo/places` (`Gazetteer.search`), `/api/runs/{id}` (the report); merge step 3 and unmerge step 4 with `user_job_actions`; the gap counts in the header strip and the per-query table.
4. **M5:** job tombstoning (the writer of `tombstone_keys`) and the source purge (which relies on `recompute_job(fenced_source_ids=…)` never reusing a tainted revision); the dead-man ping in `finalize`; the alert catalogue dispatch for the new codes; the `images` CI job with the embedder checksum; V7 on the VPS; PRIVACY.md's GeoNames attribution.
5. **Steady-state consequence of §5.3.1 with §5.6 (ruling (a): implemented as written).** A steady-state JSearch cell whose page 1 carries a cursor and fewer than 7 new `job_uid`s never reaches `END`: §5.6 records an open gap on every run (`reason = page_cap`) and marks the query "too broad" after three such runs. M1 implements exactly that, and Task 25's end-to-end test pins the gap. Fewer than 7 new ids does not prove chronological overlap with the previous coverage, so treating it as the overlap would be a separate behaviour change; whether to adopt such a rule is a product decision for the user, recorded here for the spec revision that would carry it.
6. **M4/M5 runbook:** the embedder's first build downloads 590 MB from the HF Hub; a host without that route needs the model files staged by hand (`embedder/download_model.sh` accepts a local mirror URL through `HF_MIRROR`).

## Closing note: spec coverage of the M1 sections

Section by section, the task that implements it; the items deliberately left to later milestones are listed with their reason.

- **§5.1–5.2:** Tasks 5, 9–12. **§5.3.1–5.3.5:** Tasks 9, 10, 11, 14. **§5.4:** Task 11 (detection, validation operation); the save endpoint is M4. **§5.5.1:** M2 (LLM proposal). **§5.5.2–5.5.3:** Task 13. **§5.6:** Tasks 10, 13, 14 (the interval rows) and 23 (the `coverage_cells` updates, in `finalize` as §9.2 node 10 says); the three gap displays are M4.
- **§6.1–6.5:** Tasks 7, 8. **§6.6:** Tasks 8, 13. **§6.7:** Tasks 7, 8 (the pseudo-code's functions by name). **§6.8:** Task 8's projections; the endpoint and `validate_fields`, `llm_projection`, `classify_effects` are M2.
- **§7.5, §7.7–7.10:** Task 1 (DDL), the writers in Tasks 11–22; `profile_vectors` M2. **§7.11 `hard_filter_results`:** Task 16. **§7.14 source fence:** Task 1. **§7.16.1, 7.16.2, 7.16.5, 7.16.7 (quota), 7.16.8 (groups, conflicts, gaps, windows):** Tasks 1, 7, 8, 12, 15, 18, 19, 20.
- **§8.1–8.5:** Task 2. **§8.6:** Task 18 (steps 3 and 4 of the merge and step 4 of the unmerge with their tables, Deviation 7). **§8.7:** Task 20 (deferral and score-individually in M3, Deviation 9; the display in M4). **§8.8:** Task 17 (`det_results` instances in M3). **§8.9–8.10:** Task 3. **§8.11:** Task 19 (the acknowledge route in M4). **§8.12:** Task 4 (the search API in M4).
- **§9.1:** Task 13 (reducer, receipts, retry policy, error handlers, lock order, fences). **§9.2:** nodes 1–3 Task 13, 2 and 4 Task 14, 5 Task 15, 6 Tasks 18–20, 7 Task 22, 8 Task 16, 9 Task 23 (placeholder, Deviation 11), 10 Task 23.
- **§11.1:** Task 16 (`profile_relative` and `too_old` per Deviation 8).
- **§17.2–17.3:** Task 23. **§17.4 reconcilers of M1:** `ledger_unsettled` (Task 8), `hard_filter_completeness` (16), `group_representatives` (20), `awaiting_embedding` (22), `stale_sweep` and `ats_mass_events` (19). **§17.5 metrics and health of M1:** Tasks 8, 14, 15, 20, 22. **§17.6 alert codes of M1:** raised where their condition arises (Tasks 5, 8, 13, 14, 18, 19, 20, 22, 23); ntfy dispatch is M5.
- **§18.1 unit and property tests of M1:** normalisers (2, 3, 4), the dedup decision table (20), location (4, 16), quota and Q-SH1 (8), P-AN1 (10), the outcome reducer (13), state machines (1). **§18.2 items of M1:** constraints (1), concurrency and fencing (8, 12), projection (8), fenced raw items (18), ops ABA unchanged from M0. **§18.3:** C1–C7 (14, 15, 18, 22), C28 in process (12). **§18.4:** Task 6. **§18.8:** the `contract` job and the free-source smoke (24).
- **§22:** V3, V8, V9, V10, V18 (Task 6), V17 (Task 6), V7 (Task 21).
- **Deferred beyond the M1 row on purpose:** the §14.2 routes (M4), ntfy and the dead-man switch (M5), purges and tombstoning (M5), `det_results` and targets (M3), settings editing (M2).
