# Confirmation of consensus-v2 — Claude Fable 5.1 (final round, 2026-10-16)

## (1) My round-3 objections and MISSING items

| Item | Status | Where in v2 |
|---|---|---|
| F(a) per-member score targets, `deferred_duplicate(group_id)`, split → `pending` | RESOLVED | F4 "Scoring", F2 re-enqueue |
| F(b) ATS identity = `(ats_type, board, posting_id)`, requisition id only a signal | RESOLVED | F1 |
| G node order (fallback before normalisation) | RESOLVED | G1 steps 1–10 |
| G embedder unavailable → `awaiting_embedding`, targets with `similarity = null` | RESOLVED | G1 |
| H LLM result without scope; `det_result` keyed on scope; generator sees profile + ad only | RESOLVED | H9, G8 |
| O keep contact-stripped profile JSON while referenced | RESOLVED | O3 |
| M1 checkpointer connection contract (autocommit, `prepare_threshold=0`, `dict_row`; `setup()` on autocommit) | RESOLVED | B1, B2 |
| M3 rubric v1 weights (sum = 100 ✓) | RESOLVED | H1 |
| M4 non-2xx paid statuses → `ambiguous` | RESOLVED | D3, D4 |
| M5 `hl=en` must preserve `htidocid` | RESOLVED | D4 |
| M7 cache monitoring per (model, effort, role) | RESOLVED | A5 |
| Notes: UTC-instant anchor, dead-man period follows interval, session lifetimes | RESOLVED | B4, M4, L |

## (2) New defects introduced by v2 (concrete scenarios)

**N1 — Batch targets are reclaimed by the online lane (A7 × G2).** Batches take up to 24 h (addendum); G2 reclaims any `in_progress` target with a claim heartbeat older than 3 min, and M1 makes that an invariant. Scenario: 1,000-ad CV refresh → after 3 min every target is reclaimable → online cycles re-score them at full price → batch results arrive with a stale `attempt_no` and the CAS publish rejects them. Cost ≈ 1.5× online instead of 0.5×. *Minimal fix:* a submitted target enters `in_batch(batch_id)` (not `in_progress`), excluded from reclaim, admission and the 3-min invariant until the batch ends or `expires_at`; expired items return to `pending`.

**N2 — Critic trigger "any asserted hard fact" (H3/H7) roughly doubles critic volume.** Most German ads assert a mandatory language ("Deutsch fließend"). H7 triggers the critic on every asserted hard fact, so the critic rate approaches the share of ads with any language/degree statement (likely > 70 %), not the ~35 % behind P's envelope. *Minimal fix:* trigger only when the asserted hard fact is **not satisfied** by the confirmed profile or structured data under a deterministic comparison (e.g. required `de ≥ C1` vs profile `de = C2` → no trigger); unmet or uncomparable facts still trigger.

**N3 — Singleton advisory lock survives an OOM-killed worker (B3).** A session-level lock is released only when Postgres notices the dead client — with default Linux keepalives, up to ~2 h. Scenario: worker OOM-killed mid-run → the new worker cannot take the lock → no resumes, runs or scoring for up to 2 h (visible via `/readyz`, but B7's resume path is defeated by its own fence). *Minimal fix:* open the lock session with `keepalives_idle=30, keepalives_interval=10, keepalives_count=3`; if the lock is held while the run heartbeat is stale > 3 min and the holder's backend is idle, the new worker runs `pg_terminate_backend` and takes over with `attempt_no + 1`.

**N4 — `unknown` availability vs admission expiry (F5 × G6).** F5 lists `unknown` with no entry rule; G6 expires a pending target whenever availability is "not `active`". If an implementer initialises aggregator sightings as `unknown`, every new target expires as `expired_unscored` before admission. *Minimal fix:* every sighting sets `active`; G6 expires only on `stale | closed` or age.

**N5 — Double spend on checkpoint resume of the scoring lane (B7 × G2).** A crashed `score_graph` resumed after its targets were reclaimed re-runs `generate` with the old claim and fails only at `publish` (bounded by `max_concurrency`). *Minimal fix:* scoring recovery is by reclaim only (no checkpoint resume), or each LLM node verifies `claim_id`/`attempt_no` before calling.

Minor: A4 dropped v1's `× (max_retries + 1)` from the worst-case reservation; SDK-internal retries can bill twice on a lost response, so keep the multiplier or settle SDK retries as `ambiguous`.

## (3) Vote

**CONFIRM.** Consensus is reached; every ruling stands and all my round-3 items are resolved. N1–N5 are state/config rules to fold into v3 before implementation; none changes a decision and each is bounded by the spend ledger or surfaced by health checks, so none is blocking.
