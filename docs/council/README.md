# Architecture council record

This directory is the audit trail behind the design specification [`../superpowers/specs/2026-10-16-job-hunter-design.md`](../superpowers/specs/2026-10-16-job-hunter-design.md).

Three models took part, Claude Opus 5.5, Claude Fable 5.1 and Codex GPT‑6 Astra (Codex always at `high` reasoning effort), and they worked under the 4-eyes principle. The record is kept here so decisions can be traced back to their arguments.

## Process
1. **Research** ([`research/`](research/)): verified facts about the job-source APIs and the technology stack, gathered live on 2026-10-16.
   - `sources.md` covers JSearch, SerpApi, Adzuna, Arbeitnow, ATS boards and BA.
   - `tech.md` covers LangGraph, langchain-anthropic, local embedding benchmarks, Postgres and schedulers.
   - Raw API samples are **not** committed, because they contain recruiter personal data.
2. **Brief** ([`brief.md`](brief.md), [`facts-addendum.md`](facts-addendum.md)): requirements, user decisions and decision points D1–D16.
3. **Round 1, independent proposals** ([`proposals/`](proposals/)): one proposal per model, each written without seeing the others.
4. **Round 2, cross-critique** ([`critiques/`](critiques/)): each model critiqued all three proposals.
5. **Consensus drafts and votes** ([`consensus-v1.md`](consensus-v1.md) → [`consensus-v2.md`](consensus-v2.md) → [`consensus-v3.md`](consensus-v3.md), [`votes/`](votes/)): the chair synthesised a draft and members voted on it until all three confirmed. Section R of `consensus-v3.md` records the user's decisions from the design review.
6. **Specification 4-eyes review** ([`spec-reviews/`](spec-reviews/)): six rounds of review of the written spec by Codex and Fable, until both approved revision 3.3.
7. **M0 final 4-eyes review** ([`m0-final-review/`](m0-final-review/)): a whole-branch review of the M0 implementation by Codex and Fable. Codex rejected it and Fable approved with changes. One fix wave followed, recorded as Deviations 17–28 and the carry-forward list in the M0 plan, and both approved the re-review. The reviews refer to the controller ledger (`.superpowers/sdd/…/progress.md`), which was a git-ignored working file and was deleted once M0 was complete.

8. **M1 plan 4-eyes review** ([`plan-reviews/`](plan-reviews/)): three Codex rounds on the M1 collection-pipeline plan. Round 1 rejected it (2 blockers, 15 major findings), round 2 rejected it on a narrower list, and round 3 approved.
9. **M1 final 4-eyes review** ([`m1-final-review/`](m1-final-review/)): a whole-branch review of the M1 implementation by Codex and Fable.
   - First round: Codex rejected it (2 critical, 3 important). The critical findings were a real personal name left in two committed fixtures, and unfenced worker writes at startup and in retention. Fable approved with changes (3 important, 7 minor).
   - Fixes: seven fix dispatches, each with its own scoped review and re-review. They are recorded as Deviations 51–63, as amendments to earlier Deviations, and in the carry-forward list of the M1 plan.
   - Re-review: both approved, with all 15 findings fixed. Their remaining minors were applied in the last dispatch or carried forward.
   - The scrubbed name remains in the feature branch's pre-fix history, and it reached `main` with the merge commit of PR #2. Removing it needs a history rewrite of `main`, which is the user's decision.
   - The reviews refer to the controller ledger (`.superpowers/sdd/…/progress.md`), a git-ignored working file that is deleted once M1 is complete.
10. **M2 plan 4-eyes review** ([`plan-reviews/`](plan-reviews/), `m2-round1-codex.md` to `m2-round6-codex.md`): six Codex rounds on the M2 plan (LLM gateway, CV profile and settings).
    - Round 1 rejected it with 2 blockers and 12 major findings: the test reset, a stale cap at month rollover, the reservation owner, long calls, paid-work receipts, the overrun-buffer formula, the spike budget, checkpoint error writes, CV retention, and others.
    - Rounds 2 to 5 each confirmed the earlier fixes and found narrower issues: the reset's delete order, approval generations and repair identity, native-PDF checks, thread names, failure hooks, and retention racing activation and retry.
    - Round 6 approved, with no blocker or major finding left.

## Standing rules
See [`notes.md`](notes.md): Codex GPT‑6 Astra is used only at `high` effort in council and review rounds.
