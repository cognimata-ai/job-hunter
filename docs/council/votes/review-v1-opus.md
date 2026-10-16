# Review of consensus-v1: Claude Opus 5.5 (round 3 vote)

I checked consensus-v1 against `brief.md`, `research/sources.md`, `research/tech/tech.md` and `facts-addendum.md`. I am not re-arguing any [RULING], including the one against my own facts-extraction node (H3).

## Section votes

**A. Stack & LLM gateway: OBJECT**
- (a) **Failure: one stalled stream hangs a target indefinitely, and nothing detects it.**
  - Tech §B1.10 says ChatAnthropic is built with `timeout=None` and recommends: "Set `max_tokens` and `timeout` explicitly, or stream."
  - A2 chose streaming but sets no timeout, and no LLM node has a `TimeoutPolicy`.
  - With no read timeout, a half-open TCP connection mid-stream blocks the call forever.
  - The worker is still alive and heartbeating, so B6's stale detection never fires. M's "in_progress > 15 min without a live run" check also never fires, because the run *is* live.
  - The score target therefore stays `in_progress` forever, holding a concurrency slot.
- (b) **Required change** (add to A2):
  - Set an explicit httpx timeout on ChatAnthropic: connect 10 s, read/idle 120 s.
  - Add `TimeoutPolicy(idle_timeout=180 s)` on every LLM node. A timeout counts as a new attempt and takes a new spend reservation (A4).

**B. Topology & scheduling: APPROVE.** Note: B5 detaches scoring from `runs`. That affects M's invariant, which is covered under G below.

**C. Storage: APPROVE.**

**D. Sources & query planning: OBJECT**
- (a) **Failure: Arbeitnow ads are silently skipped at long intervals.**
  - D5 caps Arbeitnow at ≤3 pages per run with a `created_at` watermark. J allows schedules up to 7 days.
  - Research §6: page 1 holds 300 ads covering about 37 h. A 7-day gap is therefore about 4.5 pages.
  - With the cap at 3, the run stops before reaching the old watermark. If the watermark then advances to the newest `created_at`, the unread middle pages are never fetched.
  - D5 doesn't say what happens to the watermark in that case. "Never used to close ads" does not cover lost intake.
- (b) **Required change** (add to D5):
  - Advance the watermark only across a contiguous, fully read range.
  - If the cap is hit before reaching the old watermark, record `coverage_truncated` and keep a gap cursor for the next run.
  - Set the page cap to `min(10, max(3, ceil(interval_h/30)+1))`, within Arbeitnow's 50-request rate limit (research §6).

**E. Quota ledger & budget planner: APPROVE.**

**F. Normalisation, dedup, availability: APPROVE.** Some items are missing; see M1 and M2 below.

**G. Graphs & admission: OBJECT** (two problems, neither re-argues the G5 ruling)
- (a1) **Failure: budget is spent on ads the user can't see, and `expired_unscored` never happens for aggregator ads.**
  - F5 defines availability for aggregator ads as `active → stale` only. They can never be `closed`.
  - F5 also makes max ad age a display filter, not a lifecycle state.
  - So pending JSearch, SerpApi, Adzuna and Arbeitnow targets never "expire". G5's "20 % oldest pending" lane will keep spending LLM budget on ads that are already older than max age or stale, which the Jobs page hides by default.
  - The terminal state `expired_unscored` that G5 promises is never reached for these ads.
- (a2) **Failure: a detached scorer has no claim lease.**
  - G2 claims targets with `FOR UPDATE SKIP LOCKED`, but that row lock is released at commit.
  - B5 says scoring is not a `runs` row, so M's "in_progress without a live run" check has no run to look at.
  - Result: either a crashed scorer's targets stay `in_progress` forever, or a slow but live scorer is reset after 15 min and the target is scored twice. The CAS publish stops a double publish, but not the double spend.
- (b) **Required changes:**
  1. Define "expired" for admission as: availability is not `active`, **or** the ad's effective posting date is older than max ad age. Admission skips such targets, and they become `expired_unscored`.
  2. The claim transaction sets `state='in_progress'`, `claim_id` and `claim_heartbeat_at`, and each node refreshes the heartbeat. A target may be reclaimed only when its claim heartbeat is older than 3 min. M's invariant must use this claim heartbeat instead of a run.

**H. Scoring rubric, evaluator, versioning: OBJECT**
- (a) **Failure: any scope tweak forces a full LLM re-score.**
  - H9 makes `score_target` unique on a tuple that includes `scope_hash`, and the current-score view joins the active tuple.
  - Location/work-mode, salary and employment-type fit are **deterministic** criteria (H1), and those are exactly what scope settings change.
  - So changing the radius from 30 km to 40 km creates a new target for every retained ad, and every current score instantly drops out of the view.
  - Concretely: about 1,000 ads × ≈$0.05–0.10 on config A, or the Jobs page showing everything "pending" until the budget catches up, which can take days. No LLM input changed.
- (b) **Required change:**
  - `scope_hash` should cover only the scope fields that are actually rendered into the LLM prompt.
  - Deterministic criteria, `[L, U]`, coverage and gates are recomputed from the stored LLM result whenever scope changes, with no new target and no LLM call. Persist this as a derived row keyed by `(score_result_id, scope_version_id)`.

**I. CV ingestion: APPROVE.**

**J. Settings: APPROVE.** The 7-day maximum interval interacts with D5; see D above.

**K. Jobs page: APPROVE.**

**L. Security: APPROVE.**

**M. Errors & observability: OBJECT**
- (a) **Failure: a fresh install can come up permanently "not ready".**
  - `/readyz` includes "last successful run age" and "last backup age".
  - On first deploy neither a run nor a backup exists, and during any multi-hour source outage the run age is legitimately old.
  - If `/readyz` drives the compose healthcheck or `depends_on: service_healthy` (B1 doesn't say), the stack never becomes healthy, or flaps. That turns an upstream data problem into the app looking down.
- (b) **Required change:**
  - `/readyz` checks only what the process needs to serve: DB, migrations at head, embedder up, model checksum, worker heartbeat.
  - Last-run age and last-backup age move to `/api/system/health`. They raise warn or critical alerts, with a first-run grace period, and are not readiness gates.

**N. Testing: APPROVE.** Please add two crash-matrix cases: a claim-heartbeat reclaim (G), and a stalled stream (A).

**O. Deployment & retention: OBJECT**
- (a) **Failure: profile pruning breaks foreign keys or traceability.**
  - O keeps "only active + previous profile versions".
  - H9 has `score_target` referencing `profile_version_id`, and its history is kept "per retention policy".
  - After a third CV upload, pruning v1 either fails on the foreign key every night (with only a log line) or orphans results whose inputs can no longer be reconstructed. That breaks the brief §1 requirement that every score is traceable.
- (b) **Required change:** pruning a profile version deletes or tombstones its score targets, attempts and results in the same transaction, and the UI history states this. Alternatively, keep any profile version that a retained result still references.

**P. Cost envelope: APPROVE.**
- Minor: the upper bound for config A ($300) came from my estimate, which included the facts-extraction call (about $80/month). The H3 ruling removed that call, so A is about $120–230.

**Q. Escalations: APPROVE.**

## MISSING (each causes an integrity error or a silent failure if left out)

1. **Probable-duplicate groups (F4) are underspecified:**
   - They need a time window (45 days, as in O and F).
   - A group's availability should be "any member active", otherwise a live member hides behind a closed one.
   - The card must say which member was scored.
   - A user **split** must enqueue a score target for each resulting job. Otherwise the split-off job keeps showing the other ad's score.
2. **Stale TTL:** with paid queries on rotation, a live JSearch ad may not be re-seen for days. The TTL default must be at least 2× the longest per-query refresh cadence. The planner dry-run should warn when it isn't, so live ads don't go stale.
3. **JSearch `date_posted` beyond the largest bucket:** if `now − last_covered_at + 24 h` exceeds `month`, use `month` and record a coverage gap. Don't leave the case undefined.
4. **Semantics of `basis=unconstrained`** (H1): is it scored as level 4, or excluded? And which number, the midpoint or the lower bound, drives the ±8 critic trigger and the relevance threshold? Implementations will diverge unless this is pinned.
5. **SerpApi `hl=en` (D3) is unverified** (research §3). Parsers must accept both English and German relative dates and extension strings (`"vor 4 Tagen"`, `"Homeoffice"`). If they don't, the confirmation call flipping back to `hl=de` silently yields null dates and work modes.
6. **Procrastinate job cleanup:** the 1-minute tick creates about 525k finished jobs per year. Add a scheduled cleanup of finished jobs and a table-size metric.

## Verdict

**APPROVE WITH CHANGES.** Six objections: A, D, G, H, M and O. Each needs only a minimal change, and none re-argues a ruling. Also resolve the six MISSING items before implementation starts.
