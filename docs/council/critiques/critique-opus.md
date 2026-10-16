# Critique: Claude Opus 5.5 (council round 2)

This critique checks the three proposals against the brief (§2–§4), `research/sources.md`, `research/tech/tech.md` and `facts-addendum.md`.

Labels used throughout:
- **O** is my proposal, **F** is Fable's, **C** is Codex's.
- Sections are cited as in each document.

## 1. ERRORS

### 1.1 Opus (own proposal)

**O1. The score run can never be enqueued, because it collides with its own unique index.**
- §3 says `finalize_collect` "defers `run_score`" into the `pipeline` lane.
- At that moment the collect run is still `running`.
- §4 has `UNIQUE(lane) WHERE status IN ('queued','running')`.
- So inserting the score run violates the index on every collection, and scoring never runs.
- Fix: make scoring a detached `score_target` queue (§4), or close the collect row in the same transaction that inserts the score work.

**O2. Bulk re-score and incremental scoring race each other.**
- D2 puts bulk re-score in the `aux` lane and incremental scoring in `pipeline`. Both select `awaiting_score` ads.
- §4 `transition()` *raises* when it updates 0 rows, so whichever claimant loses fails a node instead of skipping.
- Scenario: you confirm a new CV during a scheduled run. Both lanes claim overlapping ads. You get spurious `score_failed` states, and duplicate LLM calls for any ad that was read before it was claimed.
- Fix: claim with `UPDATE … WHERE id IN (SELECT … FOR UPDATE SKIP LOCKED) RETURNING`, so each ad has exactly one claimant.

**O3. A queued run can be orphaned and stop all scheduling.**
- The diagram and D2 say the API "defers tasks in same DB tx". Nothing in the research verifies that procrastinate's `defer` joins the caller's SQLAlchemy transaction.
- If it doesn't, a crash between `INSERT runs(queued)` and `defer` leaves a queued row with no job behind it.
- The lane index then blocks everything: every tick hits a unique violation and every "Run now" returns 409. Collection stops silently until the dead-man alarm fires, hours later.
- Fix: a reconciler that re-defers or fails any queued row with no job after 2 min, or C's outbox.

**O4. Resuming a crashed run deadlocks on procrastinate's lock.**
- D2 re-defers a stale run with `lock="pipeline"`.
- After a hard kill, the dead job stays in `doing` and keeps holding that lock, so the resumed job never starts.
- My D2 "Risks" claims heartbeat recovery covers this. That claim is false.
- Fix: at worker start, and in each tick, mark stalled procrastinate jobs as failed (check procrastinate 3.10's stalled-job API) before re-deferring.
- F's D2 has the same defect.

**O5. A ledger conflict is mistaken for an exhausted budget.**
- §6 `reserve_unit` uses `ON CONFLICT DO NOTHING RETURNING id`.
- On resume, an existing reservation returns no row, and the caller reads `None` as "budget exhausted".
- It needs three outcomes:
  1. a new reservation;
  2. an existing billed row with a stored payload, which should be replayed;
  3. an existing reserved or unknown row with no payload, which should become `AMBIGUOUS` (C's term), stay counted against quota, and be surfaced.

**O6. My cache figures were wrong.**
- §8 assumed cache reads cost 0.1× input. The addendum says Opus 5.5 reads cost $0.20/MTok, which is 0.05×.
- D7 padded the prefix to about 5k tokens "above the cache minimum". The minimum is actually 512 tokens on every 5.x model.
- The cost impact is small (about −$3/month on config A), but the rationale was wrong. The worked examples stay only if the eval set shows they improve calibration.

**O7. Effort must always be set explicitly.**
- D1 says to omit effort for Haiku. The addendum says Haiku has an effort parameter (default medium) and Sonnet's default is `high`.
- If any Sonnet node falls back to its default, thinking-token cost rises silently.
- Fix: the gateway rejects any node config that lacks an explicit effort for a model that supports one.

**O8. The aggregation inflates ads with little evidence.**
- §5 drops unknown criteria from both the numerator and the denominator.
- Example: an Adzuna snippet where only `role_fit=4` and `location_workmode=4` are known scores **100**, flagged only as "low confidence".
- That puts thin evidence at the top of the ranking, which is exactly the "wrong but plausible" failure the brief warns about.
- Fix: adopt C's L/U interval and rank by its midpoint (§4).

**O9. One state column mixes three independent dimensions.**
- §4's single `job_ads.pipeline_state` combines eligibility, prefilter and per-config scoring.
- When the config changes, the ad is pushed back to `awaiting_score`, and the fact that it was scored under config N is lost.
- Fix: one score target per (job_revision, scoring_config), as C proposes. This also fixes O2.

**O10. Gaps.**
- `gate_flags` has no degree, clearance or work-permit knockout, so an ad requiring "Sicherheitsüberprüfung Ü2" is never capped.
- §8's cost rests on a single volume figure (100/day) with no range; the revised range is in §4.

### 1.2 Fable

**F1. Every Arbeitnow ad expires within about two runs.**
- D5 treats Arbeitnow as an enumerating source: an ad absent from 2 consecutive successful fetches becomes `expired_confirmed`.
- But D4 only pages "until `created_at < watermark`, max 5 pages".
- Ads older than the watermark are never listed again, so every one of them looks absent twice and is marked expired. Only full-board ATS listings actually enumerate.

**F2. Live aggregator ads are shown as expired.**
- `expired_assumed = first_seen_at + max_ad_age_days`, with a 14-day default (D9).
- A role that JSearch still returns on day 20 is displayed as expired.
- Lifecycle has to key on `last_seen_at`. "Too old" belongs in a filter.

**F3. The freshness window ignores query rotation.**
- In §6, `date_posted=freshness(s.interval)` is derived from the schedule interval.
- But queries are served round-robin, stalest first. With 12 paid queries and 1 unit per 6-hour run, each query is refreshed about every 3 days while still asking for `today`.
- So about 2 days of ads per query are never requested.
- Fix: `freshness(now − q.last_paid_at)`.

**F4. Pacing pushes paid spend to the end of the window at short intervals.**
- `floor(remaining/runs_left)` has no fractional carry.
- At a 1–4 h interval, `runs_left > remaining`, so the allowance is 0 for most of the window.
- At 1 h, JSearch sits idle for about 23 days and then spends everything at the end.

**F5. Page-1 paid calls are not reserved before they are sent.**
- `try_spend` guards page 2 only. Page-1 units are recorded after the call.
- A crash between the response and `L.record`, or the lag in SerpApi's `account.json`, under-counts billed units.
- Brief §4.1 requires a ledger that cannot overrun.

**F6. Node-level retry repeats side effects.**
- In §3.1 the `fetch` node bundles the HTTP call, payload storage, `quota_event`, normalisation and upsert. It is retried as one unit on `TransientError` (D4: "the single retry layer").
- A transient DB error *after* a billed call therefore pays for the call again, with no new reservation.
- Retries belong around the HTTP call, behind a reservation.

**F7. The subgraph checkpointer setting does the opposite of what is intended.**
- §3.2 says "own checkpointer = `False`, inherits the parent's".
- Per tech §A3, `None` means inherit and `False` means no checkpointing.
- With `False`, a crash re-runs the whole subgraph, repeating up to 5 LLM calls per job.

**F8. The active-run index does not enforce a single run.**
- §4 uses `partial unique (status) WHERE status IN ('queued','running')`.
- That is unique per status *value*, so one `queued` and one `running` row can coexist. This contradicts "409 if one is active".
- `rescore` runs (§3.3) share the same table, so they also block scheduled collection, and the tick's failed insert is unspecified.

**F9. One invariant fails on normal runs.**
- D12 checks `Σ quota_event.units = planned spend`.
- Page 2 is decided inside `fetch`, and fetches that fail before billing cost 0. So this equality is false on ordinary runs and marks them failed.
- It should be `≤ allowance`, with the sum reconciled against the ledger for that run.

**F10. Adzuna's `redirect_url` is never persisted.**
- Research §4 says the API spec requires using this URL to comply with Adzuna's terms.
- Without it, the UI has no compliant outbound link.

**F11. The whole CV PDF goes to Anthropic.**
- D8 sends the native PDF document, so name, address, phone, photo and date of birth all leave the box. That contradicts brief §4.3 (data minimisation).
- It also says scans are rejected with "no text layer", even though the native-PDF path would read them.

**F12. Fingerprint auto-merge has no confirmation step.**
- D5 stage 2 merges on an exact `title|company|city` match without checking the text.
- That merges two distinct requisitions on the same ATS board (same title, same city).
- It also merges partial Adzuna items, which contradicts stage 3's rule that partial items only link.
- Stage 3's SimHash/cosine check is weak for templated employer ads that share boilerplate, and there is no seniority guard.

**F13. The LLM must rate criteria the ad says nothing about.**
- §5 requires every criterion to be rated 0–5.
- `terms` and `location_mode` therefore get guessed when the ad has no data. Salary was filled in 0/10 of the German JSearch samples.
- `missing_information` is recorded but never affects the score.

**F14. `max_tokens=2048` is too low for Opus with thinking.**
- §3.2: thinking tokens count against `max_tokens`.
- Seven criteria with evidence arrays plus medium thinking will often exceed it, producing frequent terminal `llm_max_tokens` states.

**F15. Miscellaneous.**
- §9 Q1 re-opens Adzuna, which brief §3 already decided.
- D15 keeps backups on the same VPS; off-site is only optional.
- D9's $60 cap is below F's own ≈$96/month estimate for the mandated Opus default, so scoring would be held every month.
- Arbeitnow's "1/h" bucket contradicts "max 5 pages/run".
- Opus cache read is 0.05×, not 0.1×.

### 1.3 Codex

**C1. Partial ads may never get a score.**
- The critic must review every partial record. Results publish only once all checks pass; otherwise they sit in `NEEDS_REVIEW`. An `insufficient_evidence` verdict exists.
- A 500-character Adzuna snippet will plausibly draw `insufficient_evidence` every time, so after one repair it stays in `NEEDS_REVIEW` permanently.
- Brief §3 says partial ads must be *scored with lower confidence*, not withheld.

**C2. Adzuna re-ingests and partial/full twins are never merged.**
- D5 auto-merges only on strong identities or matching full-text fingerprints.
- The research found Adzuna re-ingesting the same ad under 4 different ids, linkable to other sources only by title and company.
- Under C, each copy is listed, scored and ranked separately, multiplying cost by about 4×.

**C3. The fail-closed reset rule can deadlock JSearch.**
- §6: "Reset only from validated renewal information."
- JSearch's reset time arrives only in the header of a *paid* response.
- Once the stored reset time has passed, JSearch is blocked, and validating the new window needs a paid call. The result is a permanent fallback to SerpApi.
- Fix: extrapolate the window (about 30.96 days, research §2) and allow one budgeted probe call.

**C4. Possibly unsupported schema keywords.**
- The schema uses `minItems: 6`, `maxItems`, `maxLength`, and `minimum`/`maximum` on `["integer","null"]`.
- The research only verifies that `transform_schema` adds `additionalProperties:false`.
- If these keywords are silently stripped, their guarantees disappear. If they are rejected, every call returns 400.
- Needs a payload test. F's approach of putting limits in Pydantic is the safe one.

**C5. The evidence model contradicts itself.**
- The text says evidence references "numbered job sentences" and rejects "invalid references".
- The schema defines evidence as free strings with no reference ids.

**C6. The cost table is distorted.**
- It ignores caching. With a 512-token minimum and Opus cache reads at 0.05×, input cost on config A is overstated by about 35%.
- Config B never states Sonnet's effort. Sonnet defaults to `high`, so 1,600 output tokens is probably an underestimate.
- The $768 row assumes 400 ads/day, which contradicts its own note.

**C7. Admission is unbounded.**
- Embeddings never exclude anything, and every target stays queued.
- The Arbeitnow firehose and Adzuna pages keep growing the backlog.
- The backlog alert then fires permanently. A permanent alarm is as good as silence.

**C8. Timeout and missing specifics.**
- A 90 s non-streaming timeout with a 4,096-token output cap plus Opus thinking will cause spurious timeouts and paid retries. Stream, or allow ≥180 s.
- No dedup thresholds, hard-filter list or location/radius defaults are given, and Adzuna's windows are absent from the pseudo-code. Reviewers can't verify the claims.

## 2. COMPARISON

The **Endorse** column is the position I support.

| Item | O | F | C | Endorse |
|---|---|---|---|---|
| D1 language/gateway | Python, LangGraph library, single gateway | same | same | All three agree. Add: reject any node config without explicit effort (O7). |
| D2 scheduler | procrastinate tick + slot logic + heartbeat | 1-min tick, due-ness measured from run start | separate scheduler, lease, fencing, outbox | O's tick, plus C's outbox/reconciler for queued rows, plus stalled-job release (O4). Fencing is redundant with `replicas:1`, the procrastinate lock and the DB index. |
| D2 embedder | separate container | in-process | separate service | Separate container: memory isolation and independent restarts. onnxruntime releases the GIL, so this is not critical (M). |
| D3 storage | PG17 + pgvector, HNSW | same | exact scan first | PG17 + pgvector 0.8.7. Exact scan below about 50k vectors (C); HNSW when measured latency requires it. |
| D4 planning | LLM proposes queries once, deterministic runtime | round-robin | rotation + exploration cells | LLM proposes queries, user approves. Rotation by staleness, freshness window per query (fixes F3), C's exploration cell. |
| D4 fallback | SerpApi only for tasks JSearch didn't serve | same | same | All three agree. |
| D5 dedup | exact keys → guarded fuzzy with text confirmation → review grey zone | exact → fingerprint auto-merge → fuzzy | exact only; fuzzy = suggestions | O's ladder, plus F's `job_merge_event` with `reverted_at`, plus C's "conflicting strong identities go to review". |
| D5 expiry | ATS-only closure; aggregators go stale after a TTL from `last_seen` | broken (F1, F2) | ATS-only, ≥24 h apart, mass-disappearance guard | O plus C's guards. |
| D6 graphs | collect / score / score_ad / profile | scoring inside collect | separate scoring | Collection graph plus **detached** scoring that drains `score_target` (C), using O's per-target subgraph. |
| D7 admission | τ + top-N + 5% audit; dropped ads stay browsable | per-run cap + always-include rules + 5-ad sample | never exclude; 70/20/10 slots | O/F, plus F's always-include rules, plus C's "oldest pending" share inside the budget. Ads not scored get an explicit `not_prioritised` state. |
| D7 facts node | separate CV-independent extraction | none | none | Keep it (disputed). Facts are reused on every CV change and drive deterministic gates. |
| D7 scores | config-hash versioning, critic on boundary/top/sample | similar | version tuple | All three converge. Use C's version tuple as the key. |
| D8 CV | text + contact stripping | native PDF | native PDF only on explicit choice | O + C. |
| D9 settings | versioned rows | single row + history | versioned rows | Immutable versions with an active pointer (O/C). |
| D10 jobs page | status enum | enum | independent timestamps | C's independent `seen/saved/applied/hidden_at`. |
| D11 auth | Argon2, server sessions, CSRF, optional TOTP | same | same + SSRF guard | All three, plus C's SSRF guard on watchlist discovery. |
| D12 errors | typed errors + invariants | invariants (but F9) | `AMBIGUOUS` outcome | O + C's `AMBIGUOUS`. |
| D13 observability | DB-backed + dead-man switch | + last-run age in readiness | external heartbeat | All three, plus F's `backup_run` row and backup-age readiness check. |
| D14 tests | contract/eval/E2E | same | + crash-injection matrix | Add C's crash matrix: crash before/after dispatch and before/after persist. |
| D15 deploy | 16 GB, offsite backup | 8 GB, local backup | 16 GB, offsite | O/C. |
| D16 privacy | short retention | export + `PRIVACY.md` | deletion manifests | Combine all three. |
| Graph spec | IDs-only state; subgraph inherits checkpointer | F7 error | idempotent outcome reducer | O + C's reducer; `checkpointer=None` (inherit). |
| Data model | single `pipeline_state` | job + `score_state` | listing / observation / opportunity / revision / score_target | C's separation, with O's concrete unique indexes. |
| State machines | one combined machine | score + lifecycle | three machines | C's three orthogonal machines, with O's named terminal states. |
| Rubric/schema | 0–4, null, deterministic criteria, array | 0–5 forced, object, knockouts | 0–4, null + basis, interval | Merge (§4 row "Rubric"). |
| Evaluator | V1–V6 checks, ≤2 revisions, publish "contested" | ≤2 revisions, publish "disputed" | 1 repair, withhold | O/F: never withhold (C1). |
| Quota | slot pacing with carry, reserve before every call | floor pacing (F4), records after the call (F5) | elapsed pacing, reservation, `AMBIGUOUS`, fail-closed | C's elapsed pacing + reservation + `AMBIGUOUS`, an extrapolated window with a probe (fixes C3), and O's reserve release in the final 48 h. |
| Cost | A/B/C ≈ $310/135/44 per month | ≈ $96/48/10 | ≈ $192–768, no caching | A range (§4), using the addendum's cache prices, measured in week 1. |

## 3. ADOPT / REJECT

**Adopt from F:**
- Criteria keyed as an object in the schema, so each criterion appears exactly once without needing `minItems`.
- Knockout codes with severity: degree, certification, clearance, work permit, on-site required, contract type.
- `job_merge_event` with evidence and revert.
- A "wrong score" button that feeds eval cases.
- Always-include admission for watchlist companies and for titles with trigram ≥ 0.6 against target titles.
- Readiness checks on backup age and on age of the last successful run.
- `PRIVACY.md` plus data export.
- `summary_de` and `summary_en`.

**Adopt from C:**
- `score_target` per version tuple, plus a separate availability state machine.
- Independent user-action timestamps.
- `AMBIGUOUS` paid outcomes.
- A reconciler for queued runs and stuck targets.
- A worst-case USD reservation (input plus `max_tokens`) before each LLM call.
- The L/U interval with a coverage measure.
- Elapsed-fraction quota pacing (90% normal, 10% for retries).
- An SSRF guard.
- Closure only after two complete snapshots at least 24 h apart, with a mass-disappearance guard.
- Deletion manifests replayed after a restore.
- Exact vector scan first.

**Reject:**
- F's in-process embeddings on 8 GB. ORT's 2.6 GB plus Postgres plus CV parsing plus `pg_dump` peaks leaves no margin and no isolation.
- F's unconfirmed fingerprint merge (F12) and its enumerating-source treatment of Arbeitnow (F1).
- F's retry wrapped around side effects (F6).
- C's withholding of unaccepted scores (C1) and its unbounded queue (C7).
- C's separate scheduler container with leases and fencing. That is three moving parts guarding against two concurrent workers, which `replicas:1`, the procrastinate lock and the DB index already rule out.
- C's ranking by lower bound **by default**. It buries every partial ad; it should be a sort option instead.
- Sending the native PDF by default (F11).

## 4. REVISED POSITION TABLE

| Item | Revised decision | Conf. | Disputed? |
|---|---|---|---|
| Language/gateway | Python, LangGraph as a library, one `LLMGateway`. Every call: `json_schema` + `include_raw`, explicit base URL/key/`max_tokens`/timeout ≥ 180 s (or streaming), **explicit effort per node**. | H | No |
| Scheduler | procrastinate 5-min tick computes due slots. Unique scheduled slot; one active collection. Reconciler for orphaned queued rows; stalled procrastinate jobs released before resume. | H | Partly (C: lease and fencing) |
| Embedder | Separate onnxruntime container running arctic-l-v2.0 int8, `query:` prefix, vectors keyed by model; exact scan first. | M | Yes (F) |
| Paid sources | JSearch primary. SerpApi only for tasks JSearch didn't serve, when it is exhausted, breaker-open, auth-failed or contract-broken. Location in the query text; per-query freshness window. | H | No |
| Quota | Elapsed-fraction pacing (90/10), reservation before every attempt, `AMBIGUOUS` state, extrapolated reset with a probe, reserve released in the last 48 h; Adzuna 4 windows; SerpApi hourly cap. | H | Minor |
| Dedup | Exact keys from the primary link → fuzzy merge only with seniority guard + text confirmation + same-source exception for Adzuna → grey zone to review. Merges reversible with evidence. | M | Yes |
| Expiry | Only complete ATS snapshots close an ad (×2, ≥24 h apart). Aggregator ads go stale from `last_seen`. Failures never age anything. | H | No |
| Data model | `source_item` → `job` (+ revisions, `ad_keys`) → `score_target(job_revision, scoring_config)` → `score_result`. Orthogonal availability and user-action timestamps. | H | No |
| Admission | Hard filters act on positive evidence only. Budgeted admission: about 70% by similarity, about 20% oldest pending, about 10% exploration/audit, plus always-include. The remainder is an explicit, browsable `not_prioritised` with a recall alarm. | M | Yes (C) |
| Facts extraction | A separate CV-independent LLM node (cached by content hash) feeds deterministic gates. Default model is Opus until the user chooses. | M | Yes |
| Rubric | 0–4 anchored levels, null for unknown, `basis` field. Object-keyed criteria. Knockout codes with severity. Location/salary computed deterministically. | M | Partly |
| Aggregation | Show midpoint = (L+U)/2, the interval, and coverage. Rank by midpoint by default; the lower bound is a sort option. Gate cap 25. | M | Yes |
| Evaluator | Deterministic V1–V6 checks. Critic on issues, the boundary (±8), scores ≥75, partial ads ≥60, knockouts and a 5–10% sample. ≤2 revisions, then publish as `contested`. **Never withhold.** | M | Yes (C) |
| CV | Extracted text with contacts stripped. Native PDF only on explicit opt-in. User confirms an immutable profile version. | H | Minor (F) |
| Settings | Immutable versions with an active pointer. Defaults: 6 h, home city with 30 km radius plus remote-DE, 30 d, de/en. `min_score` is a filter, not a cutoff. | M | Minor |
| Jobs UI | Keyset paging, German FTS + trigram, independent action timestamps, provenance and evidence view, freshness strip. | H | No |
| Auth/security | Argon2id, server-side `__Host-` session, CSRF + Origin check, throttling, optional TOTP, CSP/HSTS, nh3, SSRF guard. | H | No |
| Errors/observability | Typed errors, conservation invariants (`≤`, never `=` for spend), DB-backed run reports, dead-man switch, backup-age readiness. | H | No |
| Deploy | 4 vCPU / 16 GB, Compose, one-shot migrate, models baked in by digest, offsite encrypted backups with monthly restore test. | H | Yes (F: 8 GB) |
| Batches | Online for incremental runs. Batches only for the first pass of bulk re-scores (≥200 ads); `custom_id` = target id. | M | Minor |
| Cost (per month, about 3,000 ads/month reaching the LLM; addendum cache prices) | A (all Opus) ≈ $120–300 depending on scorer effort (low → medium) and whether facts extraction runs. B (Haiku extract, Sonnet/medium score, Opus critic) ≈ $60–135. C (Haiku + Sonnet critic) ≈ $15–45. **Week 1 measures real tokens behind a hard cap.** | M | Yes (spread) |

## 5. TOP 5 DISAGREEMENTS

1. **Admission: bounded and audited vs never-exclude.** C keeps every target queued, so the backlog grows with the Arbeitnow and Adzuna firehoses until the budget clears it, which never happens on cheap configs. A permanently firing backlog alarm is a silent failure. My position: spend a fixed LLM budget per run, guarantee some share to the oldest pending ads and to exploration, and audit recall by LLM-scoring a sample of rejected ads. Everything left over becomes an explicit `not_prioritised` state that the user can browse and score on demand. Every ad is still accounted for, and spend stays bounded.

2. **Aggregation and ranking when criteria are unknown.**
   - Renormalising over known criteria (my round 1) lets thin evidence score 100.
   - F's forced 0–5 ratings invent data.
   - C's lower-bound ranking buries every partial ad.
   
   Use C's interval for honesty, but **rank by the midpoint**, which treats unknown as neutral, and show coverage. The brief asks for lower confidence on partial ads, not exclusion.

3. **Publish contested scores vs withhold them (C).** Withholding turns critic disagreement into an ad with no rank, and partial ads will hit `insufficient_evidence` systematically. Publishing with a `contested` badge, keeping both drafts and the critic notes, and lowering confidence keeps the failure visible without hiding the ad. The user can filter contested ads out.

4. **Fuzzy dedup auto-merge.**
   - C never merges fuzzy matches, so Adzuna's 4× re-ingests stay as duplicates.
   - F merges on an exact fingerprint with no text check, which merges distinct requisitions.
   
   My position: auto-merge only when there is the same `company_key`, compatible location, an identical seniority-token set, title trigram ≥ 0.85, *and* text confirmation (Jaccard/containment; for snippets, containment). Never merge two distinct IDs from the same ATS board. The grey zone goes to review. Every merge is reversible with recorded evidence.

5. **Topology: embedder placement and scheduler machinery.**
   - F runs embeddings in-process on 8 GB. C uses a separate scheduler container with leases and fencing.
   - I'd keep a separate embedder on 16 GB. That gives memory isolation from the worker, independent restarts and headroom for `pg_dump` plus CV parsing, at a cost of about €10/month.
   - I'd reject the scheduler, lease and fencing layer: with `replicas: 1`, the procrastinate lock and the DB unique index, two concurrent workers can't happen. The real gaps are orphaned queued rows and stalled-job locks (O3, O4), and a reconciler fixes both with less machinery.
