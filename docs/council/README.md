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

## Standing rules
See [`notes.md`](notes.md): Codex GPT‑6 Astra is used only at `high` effort in council and review rounds.
