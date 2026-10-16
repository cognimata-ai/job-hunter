# Round 3 approval review — Codex

The core architecture is acceptable. The changes below address concrete integrity and recovery gaps; they do not require reversing the topology, grouping, admission, or contested-publication rulings.

**A. Stack & LLM gateway — OBJECT**

**Failure:** An SDK attempt completes and bills, but its response is lost. A retry succeeds; settling the entire reservation from that final response’s `usage` releases money already spent. Nested LangGraph retries compound this exposure. Research `tech/tech.md` §A3 explicitly documents retry multiplication; `facts-addendum.md` also establishes cache-write surcharges.

**Required change:** Specify: “Settle only attempts with known usage; retain the maximum charge for ambiguous attempts. Every outer retry and enlarged-`max_tokens` retry requires fresh admission. Reservations use model-specific input/output prices and applicable cache-write prices.” SDK retries may remain enabled.

**B. Topology & scheduling — OBJECT**

**Failure:** B2 releases a stale task’s lock while its invocation remains alive. After takeover, that invocation can still write LangGraph checkpoints unless checkpoint persistence is fenced too. Rejecting its final run-row CAS does not undo those writes. Cooperative cancellation makes this possible even with one worker process (`research/tech/tech.md` §A3).

**Required change:** Atomically increment `attempt_no` on takeover; apply ownership checks transactionally to domain writes and dispatch admission. Fence checkpoint writes too, or isolate attempts into separate checkpoint namespaces initialized from the last committed checkpoint. No additional service is required.

**C. Storage — APPROVE**

**D. Sources & query planning — OBJECT**

**Failure:** After seven days offline, Arbeitnow’s three-page cap can stop before the old watermark. Advancing it to the newest fetched timestamp permanently skips the intervening ads. Likewise, one JSearch page does not establish complete query coverage. These limits are documented in `research/sources.md` §§2, 6, 11.5.

**Required change:** Separate `last_attempted_at` from verified coverage. A capped traversal or `UNKNOWN` continuation records `coverage_incomplete`, preserves the uncovered interval, and exposes the gap. Resume traversal where supported; otherwise explicitly report limited coverage rather than advancing `last_covered_at` as though exhaustive.

**E. Quota ledger & budget planner — OBJECT**

**Failure:** At the allowed hourly schedule, JSearch’s fair share is approximately `200/720 = 0.278` requests. A carry ceiling of twice that share is `0.556`, so fractional credit can never accumulate enough for one request after initial credit is consumed. This reproduces starvation despite an available monthly balance (`brief.md` §4.1; consensus J).

**Required change:** Set the carry/burst capacity to at least one billable request: `max(1, 2 × fair_share)`. Preserve fractional accrual between ticks; all hard account limits still apply.

**F. Normalisation, dedup, availability — OBJECT**

**Failure:** Two distinct opportunities can share an Adzuna snippet but differ in omitted requirements. F4 permits grouping and one representative score. Treating that score as both members’ score defeats H9’s revision-specific identity; filtering solely on the representative can also hide a matching member. Research `sources.md` §§4 and 11.3 establishes that snippets omit substantial content.

**Required change:** Keep the single card and single initial scoring call, but bind the result exclusively to the representative revision. Other members remain explicitly “not individually scored”; any displayed representative score must be labelled as such. Evaluate non-score filters across members, retain separate user actions, and enqueue independent targets when split or when conflicting scoring-relevant facts become known.

**G. LangGraph graphs & admission — OBJECT**

**Failure:** `FOR UPDATE SKIP LOCKED` protects selection only while the transaction holds its locks. A crash after a committed claim can strand a target; a non-atomic claim can allow duplicate dispatch. B’s recovery rules describe collection-run ownership, while M assumes a live owner for detached scoring. This leaves a recovery gap against `brief.md` §2.8.

**Required change:** Define an atomic select-and-update claim persisting target status, owner, attempt number, and heartbeat/deadline. Reconcile abandoned claims through bounded retries; publication checks that attempt number. M’s invariant must reference this scoring owner, rather than require an active collection run.

**H. Scoring rubric, evaluator, versioning — OBJECT**

**Failure:** H7 permits validator failures to reach the critic; H8 publishes unresolved results. A fabricated evidence quote can therefore remain attached to a numerical rating after two unsuccessful repairs. A contested badge does not make that rating grounded (`brief.md` §§1, 2.8; consensus H6).

**Required change:** Route every revision through validation. Publish contested results in a valid schema, replacing ratings with unresolved deterministic evidence failures by `null/unknown`, recomputing the interval, and retaining rejected drafts separately. Semantic disagreements may retain validated ratings. This preserves publication of contested ads.

**I. CV ingestion — APPROVE**

The disclosed, explicit opt-in for native-PDF processing adequately distinguishes that path from contact-stripped text processing.

**J. Settings — OBJECT**

**Failure:** J exposes accepted **advertisement languages**, but omits the required-working-language configuration explicitly requested in `brief.md` §2.4. An English-written advertisement can require German proficiency; using the former setting for the latter would silently misfilter it.

**Required change:** Add a separate working-language requirement/preference setting and its hard-versus-soft policy. State explicitly that advertisement language neither establishes job language requirements nor candidate proficiency.

**K. Jobs page — APPROVE**

**L. Security — APPROVE**

**M. Errors & observability — APPROVE**

The scoring-owner correction specified under G must also be applied to M’s stuck-target invariant.

**N. Testing — APPROVE**

The required crash, concurrency, and payload tests should cover the corrected contracts above.

**O. Deployment & retention — OBJECT**

**Failure:** Activating a third profile deletes the oldest profile under O, while H retains score history and O stores input references rather than request bodies. Historical results then lose reconstructable inputs—or foreign keys prevent the promised deletion. Pending attempts may also retain references to deleted profiles (`brief.md` §1 requires traceable scores; consensus H9 and O).

**Required change:** Make profile retirement a transactional dependency-aware purge: cancel dependent pending work, prevent late publication, and remove dependent scores/evidence/call artifacts before deleting the referenced profile. Retain only non-content audit metadata explaining the purge. Apply equivalent dependency handling when job-revision inputs expire.

**P. Cost envelope — APPROVE**

These are planning ranges, with actual cap enforcement dependent on the corrected accounting in A.

**Q. Escalated to the user — APPROVE**

## Missing requirements that affect correctness

1. **Durable batch lifecycle.** H11’s `custom_id` alone cannot recover a crash after submission or distinguish expired, canceled, failed, and successful items. Moreover, A’s unconditional LangChain/streaming path cannot implement Message Batches (`facts-addendum.md`, “Message Batches”; `research/tech/tech.md` §B1.8). Add a raw-SDK batch branch inside the gateway, durable submission/item records, ambiguous-submission handling, reconciliation of every item, and publication against the original target/attempt.

2. **Complete rescoring triggers.** G3 specifies profile activation, but no equivalent backfill follows scope, rubric, prompt/schema, or model-policy changes. H9’s current-score view then excludes old scores without necessarily creating replacements. Require idempotent target creation for every changed active tuple and material job revision, plus reconciliation for missing targets (`brief.md` D7).

3. **Finite call deadlines.** Streaming is not a total deadline. A hung stream with continuing worker heartbeats can hold work indefinitely. Specify HTTP connect/read-idle limits and a bounded overall node deadline, with cancellation and ambiguous-spend handling (`research/tech/tech.md` §§A3, B1.10).

4. **Canonical field-selection contract.** F defines revisions without saying how competing observations produce their fields. A later 500-character Adzuna record could overwrite full ATS text, or a predicted salary could become an asserted fact. Specify deterministic precedence, field-level provenance, preservation of complete descriptions, and salary currency/period/prediction semantics (`research/sources.md` §§4, 11.2–11.4).

**Overall verdict: APPROVE WITH CHANGES.**