# Review of consensus-v1 — Claude Fable 5.1 (round 3, 2026-10-16)

Checked against brief.md, facts-addendum.md, research/sources.md, research/tech/tech.md.

## Section votes

**A. Stack & LLM gateway — APPROVE.** Consistent with tech §B1/B2 and the addendum (streaming, explicit effort, `json_schema`, fallbacks off, SDK owns 429/5xx). Note only: the cache-hit monitor must be keyed per (model, effort, role), because effort is part of the cached prefix (addendum) and generator/critic may differ.

**B. Topology & scheduling — APPROVE.** The `((true))` partial index, `attempt_no` CAS, locked resume and slot model close the races I raised (F5/F8/O10). Note: B3 computes slots in UTC while J exposes a timezone setting; define that the anchor is stored as an instant and the interval is elapsed time, so DST never shifts or doubles a slot.

**C. Storage — APPROVE.**

**D. Sources & query planning — APPROVE.** Two implementation notes (not objections): (1) D2's status rule must read "2xx = billed; gateway 404 with `x-rapidapi-proxy-response: true` = not billed; **every other status/timeout = ambiguous**" — E3 implies it, D2 should state it, otherwise a 5xx after billing (UNVERIFIED either way, sources §12.6) gets refunded and overspends at window end. (2) The one recorded `hl=en` SerpApi call must also confirm that `htidocid` for a known ad equals the `hl=de`/JSearch value (sources §11.1 only verified equality at `hl=de`); if not, keep `hl=de` or cross-source dedup via htidocid silently stops working.

**E. Quota ledger & budget planner — APPROVE.**

**F. Normalisation, dedup, availability — OBJECT (two minimal changes).**
(a) *Failure:* F4 says a probable-duplicate group is "scored once via its most complete member" and "can be split by the user". A group formed by fuzzy evidence can contain two different jobs (same company, city, seniority tokens, boilerplate-heavy text passing containment). On split, the non-representative member has no score and — if grouping suppressed its target — no `score_target` either, so it sits unscored forever and is excluded from the default score sort. *Change:* every job revision always gets its own `score_target`; non-representative group members are set to `deferred_duplicate(group_id)` (no LLM spend); split transitions them to `pending`; the group card displays the representative's score with "applies to n members".
(b) *Failure:* F1 lists "ATS requisition ids" as strong identities. Greenhouse exposes both a posting `id` and a `requisition_id`; one requisition is routinely published as several postings in different offices (the `getyourguide` fixture carries `requisition_id`, `offices`). Keying on the requisition merges Berlin and Zurich postings into one job, whose single location then drives D-criteria/hard filters and can hide the Berlin posting from a Berlin-only scope. *Change:* strong ATS identity = `(ats_type, board, posting_id)`; `requisition_id` is at most a probable-duplicate signal.

**G. LangGraph graphs & admission — OBJECT (one minimal change).**
*Failure:* G1 orders `… → embed → hard_filters → plan_fallback → enqueue_targets`. SerpApi items fetched by `plan_fallback` never pass `parse_normalize`, `resolve_identity`, `embed` or `hard_filters` unless the graph loops back, which the spec does not define; implemented literally, fallback ads either reach `enqueue_targets` un-normalised (no identity → duplicates, no similarity) or are dropped. *Change:* `plan → fetch → plan_fallback → fetch_fallback → parse_normalize → resolve_identity → embed → hard_filters → enqueue_targets → finalize` (as in proposal-opus §3). Also add the missing state for embedder failure (see MISSING 2).

**H. Scoring rubric, evaluator, versioning — OBJECT (one minimal change).**
*Failure:* H9 keys `score_target` on `scope_hash`. H1 makes location/work-mode, salary and employment type deterministic, but the key covers the whole target, so widening the radius from 30 to 50 km (or toggling "remote within Germany") invalidates every published LLM result: ≈3,000 new targets ≈ $120–300 (P) of spend for a change that alters no LLM input — or, if implementers exclude `scope_hash` to avoid that, the stored deterministic criteria go stale while the row still reads as current. Conversely, H4's "on-site-required vs remote-only scope" knockout implies the generator sees scope, which would make the LLM part scope-dependent after all. *Change:* the generator receives **profile + ad only** (no scope; it extracts facts such as `work_mode`, `required_languages`); store `llm_result` keyed on `(job_revision_id, profile_version_id, rubric_version, prompt_schema_hash, model_policy_hash)` and `det_result` keyed additionally on `scope_hash`; the current-score view joins both; a scope change recomputes only `det_result` (no LLM calls); scope-dependent knockouts are deterministic from extracted facts + scope.

**I. CV ingestion — APPROVE.**

**J. Settings — APPROVE.**

**K. Jobs page — APPROVE.**

**L. Security — APPROVE.** Note: state session idle/absolute lifetimes (O's 12 h / 30 d are fine).

**M. Errors & observability — APPROVE.** Note: the dead-man window must be derived from the active interval (a 7-day schedule against a 6-hour dead-man window alarms daily; a 6-hour window silently never fires at 7 days if configured once at install).

**N. Testing — APPROVE.**

**O. Deployment & retention — OBJECT (one minimal change).**
*Failure:* "only active + previous profile versions kept" while H9/H10 retain score history, "wrong score" feedback and the golden set, all keyed by `profile_version_id`. After the third CV upload, version 1 is deleted: either the FK cascades and the user's labelled eval cases and feedback vanish (calibration silently degrades), or the deletion fails and the retention job errors every night. *Change:* delete only the CV file and raw extracted text of retired versions; keep the contact-stripped `profile` JSON of any version still referenced by a retained score, feedback row or eval case; purge it when the last reference is purged.

**P. Cost envelope — APPROVE.** Ranges bracket the merged estimate from my critique (A ≈ $245, B ≈ $125, C ≈ $42 at 3,000 ads with addendum cache prices).

**Q. Escalated to the user — APPROVE.**

## MISSING (absence would cause an integrity error or silent failure)

1. **Checkpointer connection contract.** `PostgresSaver` requires `autocommit=True, prepare_threshold=0, row_factory=dict_row` and `.setup()` runs `CREATE INDEX CONCURRENTLY` (tech §A3). B1's `migrate` must open its own autocommit connection, and the worker must give the saver a pool with those settings, separate from SQLAlchemy's transactional pool; otherwise setup fails or checkpoints are written inside app transactions and lost on rollback.
2. **Embedder unavailable.** G1's `embed` node has no failure outcome. Define `awaiting_embedding` on the job revision; `enqueue_targets` still creates targets with `similarity = null`; null-similarity targets are eligible for the 20 % oldest-pending and 10 % exploration lanes and the always-include rules; the run finishes `partial` with the count. Without this, an embedder outage either fails the whole collect run (ads never enqueued) or leaves them invisible to admission.
3. **Rubric v1 weights.** H1 names the criteria but no weights; the golden set (H10) cannot be labelled or evaluated against an unspecified aggregation, and `rubric_version` would be undefined on day one. Fix the weight table in v1 (proposal-opus §5 or proposal-fable §5 both satisfy this).
4. **Non-2xx paid statuses → ambiguous** (see D note) stated in D2/E1 explicitly.
5. **SerpApi `hl=en` ⇒ htidocid equality check** (see D note) added to the verification step.
6. **Split/un-merge re-enqueue** (covered by the F change): any merge reversal or group split must create targets for revisions without a published result.
7. **Prompt-cache keying.** State that the shared prefix is cached per (model, effort) pair; with different effort for generator and critic there are two entries, and the "consecutive zero cache reads" alert must be evaluated per pair (addendum: effort is part of the prefix).

## Overall verdict: **APPROVE WITH CHANGES**

Required changes: F(a) per-member targets with `deferred_duplicate`; F(b) ATS identity = posting id, not requisition id; G node order (fallback before normalisation) + `awaiting_embedding`; H split of LLM result (no scope in key or prompt) from deterministic result (scope-keyed); O keep contact-stripped profile JSON while referenced; MISSING 1–7 as stated. No [RULING] is contested.
