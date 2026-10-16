# Facts addendum for round 2 (verified from Anthropic's current docs, cached 2026-10-06)

## Prompt caching
- **Minimum cacheable prefix: 512 tokens** on `claude-opus-5-5`, `claude-sonnet-5-5`, `claude-haiku-5-5`, `claude-fable-5-1` (shorter prefixes silently don't cache: `cache_creation_input_tokens: 0`).
- **Cache read price:** Opus 5.5 $0.20/MTok (0.05× input); Sonnet 5.5 $0.20/MTok (0.1×); Haiku 5.5 ≈0.1× input; Fable 5.1 $0.25/MTok (0.025×).
- **Cache write price:** 1.25× input for the 5-minute TTL (default), 2× for the 1-hour TTL. A read refreshes the TTL at no cost; lifetime counts from request *start*.
- Break-even: 5-min TTL pays off from the 2nd request; 1-h TTL from the 3rd.
- **Cache entries are model-scoped** (a multi-model cascade doesn't share cache), and **thinking/effort settings are part of the cached prefix** — changing `effort` between requests invalidates the cache. Max 4 breakpoints per request. Put the breakpoint at the end of the shared portion (rubric + profile), not at the end of the whole prompt.
- Verify with `usage.cache_read_input_tokens` / `cache_creation_input_tokens`.
- Pre-warming with `max_tokens: 0` is rejected together with `output_config.format` (structured outputs), `stream: true`, or inside Batches.

## Message Batches
- 50% of standard price; asynchronous, results may take up to 24 h; results arrive in any order (key by `custom_id`). langchain-anthropic has no Batches support → use the `anthropic` SDK directly.

## Model behaviour reminders
- `claude-opus-5-5`: effort default is **medium**; thinking can't be disabled; forced `tool_choice` 400; no prefill.
- `claude-sonnet-5-5`: effort default `high`; thinking off only via `thinking: {type: "between_tools"}` (at effort ≤ high); forced `tool_choice` 400.
- `claude-haiku-5-5`: effort default `medium`; $0.10/$0.50 up to 100K-token prompts ($0.50/$2.50 beyond).
- Refusals: `stop_reason: "refusal"` (HTTP 200) with `stop_details.category`; server-side fallback via beta `server-side-fallback-2026-07-01` + `fallbacks: "default"` (Claude API; for Sonnet 5.5 only the `"default"` form).
- The **model configuration and monthly spend cap are the user's decision**; the design must expose per-node model/effort and present cost options, defaulting to `claude-opus-5-5` until the user chooses.
