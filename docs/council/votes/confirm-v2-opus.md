# Confirmation of consensus-v2: Claude Opus 5.5

## (1) My round-3 objections and MISSING items

| Item | Status | Where in v2 |
|---|---|---|
| A: stalled stream with no timeout | RESOLVED | A2 explicit httpx timeout + `TimeoutPolicy`; A4 a fresh reservation for every outer retry |
| D: Arbeitnow page cap and watermark | RESOLVED | D6 cap formula and contiguous-page watermark; D2 coverage accounting |
| G: expiry for admission | RESOLVED | G6 |
| G: claim lease for detached scoring | RESOLVED (but see new defect N1) | G2, M1 |
| H: `scope_hash` forcing full LLM re-scores | RESOLVED | H9 `llm_result` / `det_result` split; G8 |
| M: `/readyz` gated on run and backup age | RESOLVED | M3 |
| O: profile pruning vs foreign keys and traceability | RESOLVED | O3 dependency-aware purge |
| MISSING 1: duplicate groups (window, availability, scored member, split) | RESOLVED (but see new defect N2) | F4, F2 |
| MISSING 2: stale TTL ≥ 2× refresh cadence | RESOLVED | F5, E5 |
| MISSING 3: JSearch window larger than the `month` bucket | RESOLVED | D3 |
| MISSING 4: `unconstrained` semantics, and which number drives the critic | RESOLVED | H1, H7 |
| MISSING 5: bilingual SerpApi parsers | RESOLVED | D4 |
| MISSING 6: procrastinate cleanup | RESOLVED | B10 |

## (2) New defects introduced by v2

### N1 (blocking): a long LLM call makes the claim lease expire mid-call

v2 sets a 600 s deadline per LLM node (A2) but reclaims a target after 180 s without a heartbeat (G2: "Nodes refresh the claim heartbeat … reclaimed only when its heartbeat is older than 3 min"). The heartbeat is only refreshed at node boundaries, so a node stuck in a single streaming call cannot refresh it. Long calls are plausible here: a high-effort critic, SDK `Retry-After` waits on 429 (A4), or a 2× `max_tokens` retry.

Failure scenario for a call that runs longer than 3 minutes:
1. The target is reclaimed while the original call is still running, and the LLM is called again.
2. The original's publish fails the compare-and-swap check (`attempt_no` has moved on), so its spend is wasted.
3. M1 ("no target `in_progress` with a claim heartbeat older than 3 min") raises `failed_invariant` falsely.
4. With bounded attempts, a target that always needs long calls ends `failed` for the wrong reason.

**Minimal fix:** in G2, refresh the claim heartbeat from a **background task every 30 s for the lifetime of the claim**, not at node boundaries. Stop the task when the claim is released or cancelled. With that in place, the 3-minute reclaim threshold is safe whatever the 600 s node deadline.

### N2 (blocking): duplicate-group members are orphaned when the representative isn't scored

The new `deferred_duplicate(group_id)` state (F4) moves back to `pending` only on "a split, or conflicting scoring-relevant facts". If the representative leaves `active`, ends `expired_unscored` (G6), or ends `failed(reason)` before it is scored, the live members stay `deferred_duplicate` forever. Admission ignores them (G5 only admits `pending`), so a live ad is silently never scored. That is exactly the brief §2.8 failure mode: an ad dropped silently.

**Minimal fix:** in F4, when the representative becomes non-`active` or ends in `expired_unscored`, `failed` or `refused` without a published result, **re-elect the most complete active member as representative and move it to `pending`**. Add a reconciler invariant: "a group with active members has a representative that is pending, in progress or published".

### Non-blocking notes (implementer guidance only)

- **H9, `det_result` timing.** Compute `det_result` for the new scope **before** the active scope pointer moves, in the same transaction or by pre-computing and then swapping. Otherwise the current-score view briefly shows every ad as unscored.
- **A7, `custom_id` format.** `"<target_id>:<attempt_no>"` may violate the Batches `custom_id` character pattern. From memory the pattern is `[A-Za-z0-9_-]{1,64}`; this is UNVERIFIED. If so, every batch submit fails with a 400 (loud, not silent). Use `_` as the separator.

## (3) Vote

**BLOCK on N1 and N2 only.** Both fixes are single sentences. Once the chair applies them as written above, I change my vote to **CONFIRM** without another round. Everything else in v2 has my approval.
