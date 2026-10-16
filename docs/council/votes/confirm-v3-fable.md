# Confirmation of consensus-v3 — Claude Fable 5.1 (2026-10-16)

## (1) Integration of my v2 items

| Item | Status | v3 location |
|---|---|---|
| N1 `in_batch(submission_id)` exempt from reclaim/admission/invariant | INTEGRATED | A7, M1 |
| N2 critic only for binding-capable asserted facts | INTEGRATED in H3 — **but H7's trigger list still says "any asserted hard fact"** (line 283), contradicting H3. Implementing H7 literally restores the ~2× critic rate. Fix: change the H7 bullet to "an asserted hard fact that H3 deems binding-capable". | H3 vs H7 |
| N3 keepalives + `pg_terminate_backend` takeover | INTEGRATED | B3 |
| N4 sighting → `active`; `unknown` not a default | INTEGRATED | F5 |
| N5 claim check before each LLM call; reclaim-only recovery | INTEGRATED | G2 |
| A4 minor (single retry layer, per-attempt reservation) | INTEGRATED | A4 |

## (2) New defects in v3

**B3 takeover condition (non-blocking precision fix).** Takeover fires when "the run heartbeat has been stale for more than 3 min". When no run is active, that is vacuously true, so if two live workers ever coexist (accidental `scale=2`, a manual second worker) the newcomer terminates a healthy idle holder; its watchdog exits, Compose restarts it, and the pair ping-pong. No data loss (reclaim/CAS), but no progress. *Fix:* key the takeover on the **worker** heartbeat the holder writes every 30 s regardless of runs (it already feeds `/readyz`), and on a stale run heartbeat only when a run is `running`. Not blocking: it needs a second worker, which the single-replica deployment forbids; with one replica the rule only ever terminates a genuinely dead backend.

Checked and sound: A4 settlement classes; A7 ambiguity resolution (never resubmitted while ambiguous, 25 h bound, `custom_id` set equality, `in_batch` exemption); B3 per-attempt checkpoint threads (replay via request-key conflicts, ambiguous specs become recorded coverage gaps, not silent); G2 background heartbeat; F4 re-election + reconciler invariant; F5/G6 entry rule; H9 scope precompute-then-flip.

## (3) Vote

**CONFIRM.** Required text fix: H7 bullet aligned with H3. Recommended: B3 takeover keyed on the worker heartbeat. Neither is blocking.
