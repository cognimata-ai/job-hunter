# Job Hunter — Implementation Roadmap

> **For agentic workers:** this is the index of the milestone plans. Each milestone has its own plan in this directory, written just-in-time against the code the previous milestone actually produced. Execute plans with superpowers:subagent-driven-development (recommended) or superpowers:executing-plans.

**Goal:** build the approved Job Hunter platform in six milestones. Each milestone ships working, tested software.

**Spec:** [`docs/superpowers/specs/2026-10-16-job-hunter-design.md`](../specs/2026-10-16-job-hunter-design.md) (revision 3.5, approved). Council record: [`docs/council/`](../../council/).

## Why milestones

The spec covers roughly a dozen subsystems in about 82k words. A single plan would turn into a transcript of the program, and it would freeze interfaces before any code exists. Each milestone below therefore:

- is independently testable;
- owns its own database migrations;
- produces interfaces the next milestone can consume.

A milestone's plan is written once the previous milestone is merged, so it argues from the real code and not from guesses.

## Milestones

| # | Milestone | Spec sections | Deliverable (working software) | Spikes (§22) |
|---|---|---|---|---|
| **M0** | **Foundation** | §3.2–3.4, §4, §7.1–7.3, §7.6 (runs, worker heartbeat), §7.14 (`purge_fences` + `is_fenced()` only), §7.16 metadata/transition helper, §14.1/§14.4/§14.5 basics, §16.2–16.8, §17.1/§17.4 framework/§17.5, §18.2/§18.3 harness, §18.8, §19.1–19.4 (dev stack) | Repo + toolchain (uv, ruff, mypy `--strict`, pytest), config and startup assertions, DB roles and the migrate order, core tables. Worker singleton (advisory lock, watchdog, epochs, worker heartbeat, takeover). Durable `operations` with claim tokens, the reconciler framework, and the procrastinate 1-min tick with slots (`missed` / `skipped_overlap`), plus a no-op collection run. API skeleton with login (Argon2id, sessions, CSRF, throttling, optional TOTP), `Idempotency-Key`, RFC 9457 errors, `/healthz`, `/readyz` and `/api/system/health`. Structured logging with redaction, metrics, dev compose (postgres+pgvector, api, worker), and CI. | V1, V2, V6, V20, V21 (dev part), V16 |
| **M1** | **Collection pipeline (deterministic)** | §5, §6.1–6.8, §7.5, §7.7–7.10, §8, §9.1–9.2, §11.1 (hard filters), §17.2–17.3, §18.4 | Source adapters (JSearch, SerpApi, Adzuna, Arbeitnow, six ATS) on scrubbed fixtures, with live mode behind config. Quota ledger, windows, pacing, fallback and dry-run. Receipts (`fetch_requests`, `raw_responses`, `raw_items`), coverage and gaps, normalisation, identity/merge/unmerge, probable-duplicate groups, availability and snapshots, the gazetteer, and the embedder service. `collect_graph` end to end, with run invariants and reports. | V3, V7, V8, V9, V10, V17, V18 |
| **M2** | **LLM gateway, CV profile & settings** | §6.9, §10.1–10.9, §10.11, §11.12, §12, §13, §9.5 | `LLMGateway`: bounds, pricing, the serialized budget plus USD token bucket, the spend ledger, retries, stop reasons, cache monitor, payload tests and schema dry-run. The `docparse` container (no network), upload checks, contact stripping, the pre-transmission text review, `profile_graph`, and profile activation. Settings versions, effect classes, `active_config`, scope apply, the budget/quota preview, and query proposal. | V4, V5, V11, V13, V14, V19 |
| **M3** | **Agentic scoring** | §7.11–7.13, §7.17–7.18, §9.3–9.4, §9.6–9.7, §10.10, §11.2–11.11, §11.13, §21 | Rubric v1, deterministic criteria, knockouts, validator V1–V6, triggers and the evaluation policy. Admission lanes with credits, `score_graph` and its per-target subgraph (generator, critic, reviser), publish/contested, `det_results`, scope precompute-then-flip, and re-score triggers. The Batches branch and bulk operations. Golden set, canaries and the eval gate. | V12 |
| **M4** | **HTTP API completion & web UI** | §14.2–14.3, §15, §18.7 | Every remaining endpoint, including the jobs list with keyset pagination and ranking generation, the detail view, groups, labelling, runs, alerts and privacy. The React SPA: Login, Jobs, and Profile & Settings with CV review, EN/DE i18n and accessibility. playwright-cli E2E scenarios checking for logical and visual defects. | — |
| **M5** | **Operations, privacy & production** | §16.9, §17.6–17.8, §19.1–19.8 (prod), §20, §2.4 C1–C3 runtime checks | Alert catalogue with ntfy, the healthchecks dead-man switch, and LangSmith (EU, hidden I/O). Retention, deletion graph, purge fences, typed manifests and shipping, export, delete-everything and the Adzuna purge. Backups (restic), restore with two-phase replay and verification. Caddy/TLS, the no-egress firewall, per-service secrets, `docs/RUNBOOK.md` and `docs/PRIVACY.md`. A production deploy rehearsal. | V15, V22 |

**Dependencies:**
- M0 comes before everything else.
- M1 and M2 are independent; they run sequentially by default (M1 first).
- M3 needs M1 and M2.
- M4 needs M3. Its API tests for M1–M3 routes are written in the milestone that adds each route.
- M5 needs M4.
- The `purge_fences` table and `is_fenced()` are built in M0, so every consumer added in M1–M4 honours fences from day one. The purge procedures arrive in M5.

## Paid-API budget for spikes

Spikes run only with the user's keys and only inside the limits in §22:
- JSearch: ≤ 8 requests in total (V9 + V10).
- SerpApi: 1 search (V3).
- Adzuna: ≤ 5 requests (V18).
- Anthropic spikes: $10.00 of actual spend in total, enforced by the recorder (V4, V4b, V5, V12, V13, V14, V19).

Each spike records its result in `docs/spikes/V<n>.md`.

## Quality gates (every milestone)

1. **TDD per task.** The failing test comes first, then the minimal code, then the passing test, then the commit.
2. **Per-task review.** A fresh reviewer subagent checks spec compliance and code quality before the next task starts.
3. **Milestone 4-eyes review.** A whole-branch review by Codex GPT‑6 Astra (`model_reasoning_effort="high"` only, never `xhigh`) and Claude Fable 5.1. Findings are fixed, and the review is repeated until both approve.
4. **CI green**, covering lint, mypy `--strict`, unit, property, integration (real Postgres + pgvector) and crash tests, before a milestone is declared done.
5. **UI changes (M4 onwards)** are verified with Microsoft playwright-cli against the running dev stack, for logical and visual defects.
6. **Spec deviations** are recorded in the milestone plan's "Deviations" section and approved by the user if they change behaviour.

## Status

| Milestone | Plan | Status |
|---|---|---|
| M0 | [`2026-10-16-m0-foundation.md`](2026-10-16-m0-foundation.md) | complete |
| M1 | [`2026-10-17-m1-collection-pipeline.md`](2026-10-17-m1-collection-pipeline.md) | complete |
| M2 | [`2026-10-20-m2-llm-profile-settings.md`](2026-10-20-m2-llm-profile-settings.md) | complete |
| M3 | written after M2 lands | — |
| M4 | written after M3 lands | — |
| M5 | written after M4 lands | — |
