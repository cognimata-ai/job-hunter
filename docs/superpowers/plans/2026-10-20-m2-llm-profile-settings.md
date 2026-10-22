# M2 LLM Gateway, CV Profile & Settings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the non-scoring LLM half of the platform on the M0/M1 foundation. That covers:
- `LLMGateway`, the only code path to Claude, with:
  - byte-bounded reservations;
  - the serialised monthly budget row and the USD bucket;
  - the spend ledger and `llm_calls`;
  - retries, deadlines and `stop_reason` handling;
  - the cache monitor;
  - offline payload contract tests and the live schema dry-run.
- The CV pipeline:
  - upload checks and encrypted storage;
  - the network-less `docparse` container;
  - contact stripping and the pre-transmission text review;
  - `profile_graph` with the `extract_profile` call;
  - validation and grounding;
  - review, confirm and activation with the LLM tuple;
  - `embed_profile`.
- `propose_queries` with query authoring.
- The settings editor:
  - the Pydantic settings model replacing `run_settings.load_run_settings`;
  - versioned saves with optimistic concurrency;
  - the effect classes;
  - scope precompute-then-flip;
  - the §6.8 dry-run with the §13.5 preview.
- The M2 HTTP routes, executed by the real worker.

**Architecture:**
- **Every Claude call** goes through one `LLMGateway.call` loop. It runs at most 3 transport tries per `max_tokens` stage and 2 stages. Each HTTP try:
  1. reserves its worst-case cost under the `llm_budget` month-row lock in the owner's transaction;
  2. commits a dispatch marker under the owner predicate;
  3. streams once;
  4. settles on its own.
  A crash, a takeover or a stall can therefore never double-spend, spend past the cap, or refund without evidence.
- **Non-scoring work runs as durable `operations`** on the `aux` queue: `extract_profile_text`, `extract_profile`, `embed_profile`, `propose_queries` and `apply_scope`. The operation's `claim_id` is the owner predicate of every reservation and domain write. The profile graphs are LangGraph graphs on the M0 checkpointer, with ids-only state and one thread per execution, `profile-<draft_id>-t<operation_id>-a<attempts>` / `-p<operation_id>-a<attempts>` (Deviation 27), built from the claim's `attempts`.
- **Settings activation** only moves `active_config` pointers inside a transaction that holds it `FOR UPDATE`, creates immutable hashed rows, or records durable operations.
- **Testing:** pure functions (pricing, bounds, bucket accrual, the budget admission rule, contact stripping, profile validation, settings validation, `classify_effects`, the dry-run arithmetic) are tested without a database. The authoritative paths are integration-tested on the real PostgreSQL container. The LLM runs on a recorded-SSE fake transport behind the real gateway code (`JOBHUNTER_FAKE_LLM`). Crash cases C15, C24, C25, C30, C34 and C35 run on the real worker.

**Tech Stack:**
- The M1 stack.
- Runtime dependencies: `anthropic` 1.15.0 (`httpx2` already locked), `langchain-anthropic` 1.7.6 and `python-multipart` 0.0.32.
- A `docparse` dependency group and image: `pypdf` 6.20.1, `pdfplumber` 0.11.10 and `python-docx` 1.2.0, with no network (`network_mode: none`) and its own non-root user.
- AES-256-GCM (`crypto.py`) for CV files and raw texts on the `cv_store` volume.
- A tmpfs `parse_jobs` volume shared by the worker and `docparse` only.

**Spec:** docs/superpowers/specs/2026-10-16-job-hunter-design.md (revision 3.3). M2 covers this roadmap row of `docs/superpowers/plans/2026-10-16-job-hunter-roadmap.md`:
- spec sections §6.9, §9.5, §10.1–10.9, §10.11, §11.12, §12 and §13;
- spikes V4, V5, V11, V13, V14 and V19, split with M3 by Deviation 9.

It also uses:
- the §7.3/§7.4/§7.10/§7.11 DDL that those sections need;
- the §14.2 routes of their features;
- the §17.4 reconcilers `budget_counters` and `ledger_unsettled` (LLM entries);
- the §17.6 `llm.*`, `operation.failed` and `scope.apply_failed` alerts;
- the §18 tests and crash cases that name them.

These earlier decisions bind M2 where they name it:
- the M0 plan's Deviations 1–3, 15 and 16 and Carry-forward items 1–2;
- the M1 plan's Deviations 1, 3, 8, 10 and 11 and Carry-forward items 1, 9, 13 and 17.

Facts verified live on 2026-10-16 are in `docs/council/research/tech.md` §B (langchain-anthropic 1.7.6 source facts and the live call table).

## Global Constraints

Every Global Constraint of the M0 and M1 plans still applies:
- pins, `mypy --strict`, the ruff banned APIs and the `BLE001` allowlists;
- `transition()` for every state change;
- JSONB binds through `q(..., jsonb=[...])`;
- the §3.3.3 lock order;
- no PII or secret in logs;
- `pytest -W error`;
- injected clocks;
- Step-2/Step-4 messages are indicative;
- Conventional Commits.

M2 adds:

- New pins (§4.1), exact: `anthropic==1.15.0`, `langchain-anthropic==1.7.6` and `python-multipart==0.0.32` (marked †: re-confirm at `uv lock`) are runtime dependencies. The dependency group `docparse` holds `pypdf==6.20.1`, `pdfplumber==0.11.10` and `python-docx==1.2.0`. Each task adds the pins its deliverable needs, and a differing resolution at `uv lock` is recorded in the commit message. `httpx2` (already locked, 2.13.1) is the SDK's transport. `httpx` 0.28.1 stays the adapters' transport, and objects of one are never passed to the other.
- **Boundary (§10.1).**
  - `llm/gateway.py` is the only module that constructs a Claude client. ruff `TID251` already bans `anthropic.Anthropic`, `anthropic.AsyncAnthropic` and `langchain_anthropic.ChatAnthropic` outside `backend/src/jobhunter/llm/**`.
  - Task 5 adds `scripts/check_llm_boundary.py`, an AST check in the `lint` job. It fails on:
    - `bind_tools`, `tool_choice`, `temperature`, `top_p` or `top_k` anywhere as a name, an attribute or a keyword argument (`ast.Name`, `ast.Attribute`, `ast.keyword`), never as a string constant, so tests that assert these keys are absent pass;
    - `with_structured_output` without `method="json_schema"`;
    - an `AIMessage` as the last element of a message list literal.
- **Client (§10.1 G1–G7).**
  - `base_url="https://api.anthropic.com"` and `api_key` come from the worker's `anthropic_key` secret, always explicitly. `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_URL` and `ANTHROPIC_API_KEY` are ignored. In this dev container `ANTHROPIC_BASE_URL` points at Claude Code's own proxy, which makes the explicit argument load-bearing.
  - `streaming=True`, `max_retries=0`.
  - `timeout=anthropic.Timeout(connect=10, read=120, write=30, pool=10)`.
  - `with_structured_output(Schema, method="json_schema", include_raw=True)`.
  - `reasoning_effort=<policy effort>`. No tools, `tool_choice`, prefill, `temperature`/`top_p`/`top_k`, `thinking.budget_tokens`, betas or `fallbacks`.
  - Thinking content is never stored.
- **Models and efforts (§10.3).** Models are `claude-opus-5-5`, `claude-sonnet-5-5`, `claude-haiku-5-5` and `claude-fable-5-1`. Efforts are `low | medium | high | xhigh | max`, constrained per model by `llm/pricing.py` (`claude-haiku-5-5` supports `low…max`). Per-node defaults:

  | Node | Model / effort | `max_tokens` first / enlarged | Deadline |
  |---|---|---|---|
  | `extract_profile` | `claude-opus-5-5` / `medium` | 16,000 / 32,000 | 900 s |
  | `propose_queries` | `claude-opus-5-5` / `medium` | 8,000 / 16,000 | 300 s |
  | `schema_dry_run` | each configured model / `low` | 2,000 | 300 s |
  | `generate`, `revise`, `critic` | as `settings_v1.json` | 6,000 / 12,000 | 600 s |

  The `generate`, `revise` and `critic` nodes are only configured and validated in M2; M3 calls them.
- **Retries (§10.4).**
  - Retryable statuses: 408, 409, 429, 500, 502, 503, 504 and 529, plus timeouts and connection errors. Every other status is a non-retryable `LLMRequestError`.
  - `Retry-After` is honoured. Otherwise the jittered backoff is 2 s then 8 s, each ± 25%.
  - The first try needs ≥ 30 s (`FIRST_TRY_MIN_REMAINING_S`) of the node deadline left; any later try needs ≥ 120 s (`RETRY_MIN_REMAINING_S`).
  - At most 3 transport tries per stage and 2 stages, so at most 6 HTTP tries per logical call.
- **Pricing (§10.7, USD per MTok)**, in `llm/pricing.py`:

  | Model | Input | Output | Cache write | Cache read |
  |---|---|---|---|---|
  | `claude-opus-5-5` | 4.00 | 20.00 | 5.00 | 0.20 |
  | `claude-sonnet-5-5` | 2.00 | 10.00 | 2.50 | 0.20 |
  | `claude-haiku-5-5`, prompt ≤ 100K | 0.10 | 0.50 | 1.25× input | 0.1× input |
  | `claude-haiku-5-5`, prompt > 100K | 0.50 | 2.50 | 1.25× input | 0.1× input |
  | `claude-fable-5-1` | 10.00 | 50.00 | 12.50 | 0.25 |

  Batch items cost 50% (M3).
- **Input bound (§10.7, chair decision C1).**
  - `input_tokens_bound = Σ utf8_bytes(every rendered component, including the output_config schema) + PROMPT_OVERHEAD_TOKENS[model]`.
  - `PROMPT_OVERHEAD_TOKENS` starts at the floor of 1,000 for every model until V19 sets it (Task 7).
  - The worst-case reservation is `bound × max(input, cache-write price) + max_tokens(stage) × output price`, in the price tier selected by the bound, rounded **up** to $0.000001.
  - Settlement is `in×p_in + cache_write×p_cw + cache_read×p_cr + out×p_out`, rounded **half-up** to $0.000001.
  - Native PDF reserves `ceil(1.25 × count_tokens) + PROMPT_OVERHEAD_TOKENS[model]` input tokens, plus `native_pdf_extra_usd = 0.25 × overrun_buffer_usd`.
- **Budget (§6.9).**
  - The month is the UTC calendar month. `A = cap_usd − overrun_buffer_usd` and `reserve_frac = nonscoring_reserve_pct / 100`.
  - Non-scoring is admitted iff `counted_usd + r ≤ A`. Scoring is admitted iff `counted_usd + r ≤ A − max(0, A × reserve_frac − nonscoring_counted_usd)`.
  - Bucket: `rate = A × (1 − reserve_frac) / month_length_s` and `B_max = max(2 × rate × collection_interval_s, min_bucket_capacity_usd)`. Accrual is `bucket_usd := min(B_max, bucket_usd + rate × (now − bucket_at))` at every reservation, settlement and tick, and a new month starts full.
  - Every reservation, settlement, cap edit and bucket accrual takes `llm_budget … FOR UPDATE` after the owner row and before any ledger row.
  - Defaults: `cap_usd` 100.00, `nonscoring_reserve_pct` 5, `overrun_buffer_usd` 2.00 and `min_bucket_capacity_usd` 1.00, giving `A = $98.00` and a scoring share of $93.10.
- **Lock order (§3.3.3, §9.1):** `active_config` → job rows → owner row (an `operations` row in M2) → `llm_budget` → `quota_windows` → ledger rows → other domain rows → `ranking_state`.
- **Uploads (§12.1).**
  - Size ≤ 10,485,760 bytes, enforced while streaming (413 `upload_rejected`, nothing buffered past the limit).
  - PDF: magic `%PDF-` within the first 1,024 bytes, not encrypted, ≤ 20 pages.
  - DOCX: `PK\x03\x04`, with `[Content_Types].xml` and `word/document.xml`, no `vbaProject.bin`, ≤ 50 MiB expanded and ≤ 1,000 entries.
  - The extension agrees with the detected type.
  - Pasted text is 300–50,000 characters.
  - Files are AES-256-GCM with the `data_key` and a fresh nonce per file, on the `cv_store` volume, and never served back.
- **docparse (§12.2).**
  - `network_mode: none`, read-only root, `cap_drop: [ALL]`, `no-new-privileges`, user `10002:10002` with group 10011, no secret.
  - `parse_jobs` is a tmpfs, root mode `2770`, group 10011. Per-job directories are 0770 and files 0660, unlinked in `finally`, and leftovers are removed at worker start.
  - Each job runs in a subprocess with `RLIMIT_CPU` 30 s and `RLIMIT_AS` 768 MiB.
  - pypdf falls back to pdfplumber when it yields < 300 characters or > 5% U+FFFD. python-docx reads paragraphs plus table cells in document order.
  - Low text (< 300 characters) is `failed('low_text')`. There is no OCR.
- **Contact stripping (§12.3)**, with the categories and replacements verbatim: `[EMAIL]`, `[PHONE]`, `[URL]`, `[ADDRESS]`, `[PERSONAL]` and `[NAME]`. `strip_report` holds counts per category and never a value.
- **Profile (§12.4–12.5)**: the JSON schema verbatim, the `user_entered` block, the semantic limits table verbatim, month dates stored as the 1st, `start ≤ end ≤ today`, `total_years_experience` recomputed with overlaps merged (> 1 year disagreement is an issue), no protected attribute, and every `evidence` a `fold` substring of the transmitted text.
- **Queries (§11.12):** 6–16 queries; `text` 2–120 characters with no location words; at least one `exploration = true`; rationale ≤ 300 characters; folded texts unique.
- **Settings (§13.1):** every field, type, range, default and effect class verbatim, plus the cross-field rules. The payload shape is exactly `backend/src/jobhunter/db/seed_data/settings_v1.json`, and `SETTINGS_SCHEMA_VERSION` stays `1`. A save applies the effect classes in this order in one transaction: `llm_tuple`, then `scope`, then the others. `apply_scope` works in chunks of 500.
- **Idempotency (§14.1):** `Idempotency-Key` is required on every listed M2 POST:
  - `/profile/drafts/{id}/text-review`, `/retry` and `/confirm`;
  - `/profile/query-proposals`;
  - `/settings` (save).
  A confirm of an already confirmed draft returns its original body even with a new key.
- **Privacy, binding:**
  - CV text, profile JSON, reviewed text and LLM output never reach a log, a metric label, a trace, an alert message or a checkpoint.
  - Checkpoint state holds ids, counters and small enums only.
  - Fixtures are **synthetic CVs with invented people** (Max Mustermann, Erika Musterfrau, Jane Doe) and never a real CV.
  - Recorded Anthropic responses are generated from synthetic inputs only, and the M1 PII scan (`tests/contract/test_fixture_pii.py`) gates `tests/contract/fixtures/anthropic/**` too.
- **Paid API (Anthropic), hard limits for the whole of M2:**
  - at most 8 paid messages;
  - a running sum of **worst-case reservations ≤ $0.80**, each the exact `pricing.worst_case_usd` of the request about to be sent, the native-PDF extra share included;
  - an expected actual spend of about $0.15, with the settled total recorded in `docs/spikes/V19.md`;
  - the roadmap's "Anthropic < $1 in total" counts actual spend over V4, V5, V12, V13, V14 and V19, so M3's V5, V12 and V14 keep at least $0.80 of it.

  Planned use:
  - V13: 2 calls (Sonnet, Opus, `max_tokens` 1,024);
  - V4: 4 schema calls plus 1 informational `minimum` probe (`max_tokens` 2,000, effort `low`);
  - V19: `messages.count_tokens`, which is free, plus **1** native-PDF extraction of a synthetic 1-page PDF (`max_tokens` 1,024). It is reserved with the native-PDF extra share computed from the spike's own `overrun_buffer_usd = 0.50`, the §13.1 minimum, so the extra is $0.125. §22 V19 says "no new paid calls", but its native-PDF criterion compares recorded usage with `count_tokens`, and no native-PDF response is recorded yet (Deviation 9).

  The worst cases at the floor overhead are about $0.08 for V13, $0.37 for V4 and $0.16 for V19.

  The spike recorder `scripts/spikes/record_anthropic.py` (Task 4) counts each send **before** it happens, under a file lock in `.fixtures-raw/anthropic-budget.json`. It refuses any send that would pass the spike's call count, the 8-message total, or the $0.80 running sum of worst-case reservations (`pricing.worst_case_usd`, with the request's `extra_usd`). It records the actual settled cost next to each entry. The key is the environment variable `JOBHUNTER_ANTHROPIC_API_KEY`, which the spike maps to the secret file `anthropic_key`. Never print, log or record the key. No paid call runs in CI. The payload tests are offline.
- No LangSmith tracing in M2 (`LANGSMITH_TRACING` unset; §17.8 is M5).
- Commits use the scope `m2`, for example `feat(m2): …`.
- **Test bodies (Ruling P4):** the Review Focus pins and the crash cases are given here as concrete code. Every other test given as `...` or comments is written by its implementer from the step's prose and its named assertions. A vacuous or tautological body is an Important review finding.

## Review Focus

1. **A scanned CV with no text layer** (a photographed PDF), the commonest real upload.
   - Expected: the draft ends `failed('low_text')`, with no LLM call made and no reservation written.
   - The draft response offers the two §12.7 repairs, "paste text" and "send the original PDF", and the latter is honoured only with the disclosure acknowledgement.
   - Pinned by Task 11 (`test_scanned_pdf_ends_low_text_without_any_llm_call`) and Task 14 (`test_native_pdf_retry_requires_the_disclosure_ack`).
2. **The candidate's own contact data in places the regexes see late:**
   - a footer phone split over a line break;
   - an e-mail inside a table cell;
   - the name repeated in the closing signature.
   Expected: the review text shows every one replaced and `strip_report` counts them. If the model still echoes a contact value into the profile JSON, the post-extraction scan removes it and records an issue. Pinned by Task 10 (`test_contacts_in_footer_table_and_signature_are_stripped`) and Task 12 (`test_contact_value_in_profile_json_is_removed_with_an_issue`).
3. **A hostile or oversized upload:**
   - an 11 MiB PDF;
   - a DOCX ZIP bomb;
   - a macro-enabled `.docx`;
   - an executable renamed `.pdf`.
   Expected: 413 or 422 `upload_rejected`, nothing stored or extracted, and the memory never holding more than the limit. A file that passes the checks but explodes inside docparse is killed by the rlimits, and the draft ends `failed('parse_error')`. Pinned by Task 9 (`test_hostile_uploads_are_rejected_before_storage`) and Task 8 (`test_rlimit_kills_a_runaway_parse`).
4. **The month boundary or a lowered cap during an in-flight extraction.**
   - The reservation is admitted against its own month, and its later settlement adjusts that month even after the rollover.
   - A cap lowered below the counted spend admits nothing for the rest of the month, while the in-flight call settles.
   - The counters always equal the ledger sums.
   Pinned by Task 3 (`test_settlement_after_rollover_adjusts_the_reservation_month`, `test_lowered_cap_admits_nothing_but_settles_in_flight`) and the property test of Task 3.
5. **Two browser tabs saving settings, and a scope save during a running `apply_scope`.**
   - The second save from a stale base gets 409 `stale_base_version`.
   - A newer scope save supersedes the pending one: the old operation ends `succeeded` as obsolete without flipping, and the newer one flips.
   - The Jobs view never sees a scope without its `hard_filter_results`.
   Pinned by Task 17 (`test_stale_base_version_is_409`) and Task 18 (`test_newer_scope_save_supersedes_the_pending_apply`).

---

## File Structure

The paths follow §4.2 for the M2 subset. Modules that §4.2 does not list are marked *(new)*; see Deviation 4.

| Path | Responsibility |
|---|---|
| `backend/src/jobhunter/db/migrations/versions/0006_m2_llm_profile_settings.py` | The M2 tables of §7.3 (`llm_tuples`), §7.4, §7.5 (`query_proposals`), §7.10 (`profile_vectors`), §7.11 (`llm_budget`, `llm_spend_ledger`, `llm_calls`) and §7.13 (`eval_runs`). The staged FKs, the `profile` branch of `is_fenced()`, and the transition triggers |
| `backend/src/jobhunter/db/migrations/versions/0007_m2_rubric_prompt_seed.py` | Rubric v1 and prompt-schema version 1 rows, the `active_config` pointers, and the restored NOT NULL (M0 Deviation 1) |
| `backend/src/jobhunter/db/{seed,transitions}.py` | Seeds for the rubric, prompt-schema, budget-month bootstrap; state machines `profile_drafts`, `llm_spend_ledger`, `llm_calls`, `query_proposals`, `eval_runs` |
| `backend/src/jobhunter/llm/{__init__,pricing,bounds,budget,spend_ledger,gateway,transport,errors,cache_monitor,fake}.py` | §10, §6.9.1–6.9.2. `transport.py` *(new)* builds the httpx2 client and the recorded-SSE fake transport; `fake.py` *(new)* serves `JOBHUNTER_FAKE_LLM` |
| `backend/src/jobhunter/llm/prompts/{profile_extract.md,propose_queries.md,render.py}` | The two M2 prompt templates and the renderer (`RENDERER_VERSION`) |
| `backend/src/jobhunter/llm/schemas/{__init__,generator,critic,profile,queries}.py` | Structural Pydantic mirrors of §11.5, §11.7.3, §12.4 and §11.12, plus their JSON-schema export (the generator and critic schemas only as data in M2) |
| `backend/src/jobhunter/scoring/rubric/v1.yaml`, `scoring/rubric/__init__.py` | Rubric v1 (§11.2 weights, §11.3 anchors) and its loader and validator |
| `backend/src/jobhunter/llm/schema_dry_run.py` *(new)*, CLI `jobhunter schema-dry-run --live` | §10.11 |
| `backend/src/jobhunter/profile/{__init__,upload,storage,parse_client,contacts,schema,validate,activation,estimate,vectors}.py` | §12. `storage.py` *(new)* handles encrypted files on `cv_store`; `estimate.py` *(new)* computes the re-score preview; `vectors.py` *(new)* runs `embed_profile` |
| `docparse/{Dockerfile,worker.py,extract.py}` | The sandboxed extractor (§12.2) and its file protocol |
| `backend/src/jobhunter/graphs/profile/{__init__,graph,state,deps}.py`, `graphs/profile/nodes/{parse,strip_contacts,check_pdf,extract_profile,validate,draft,llm_outcome,propose_queries}.py` | §9.5. `llm_outcome.py` *(new)* holds the LLM failure mapping and the receipts walk shared by the two LLM nodes |
| `backend/src/jobhunter/profile/operations.py` *(new)* | Handlers `extract_profile_text`, `extract_profile`, `embed_profile`, `propose_queries` |
| `backend/src/jobhunter/settings/{__init__,model,derive,validate,effects,dryrun,save,scope_apply,llm_projection}.py` *(new package; §4.2 puts settings logic in `api/routes/settings.py`, see Deviation 4)* | §13, §6.8 |
| `backend/src/jobhunter/queries/{__init__,proposals,authoring}.py` *(new)* | §5.5.1 and §11.12 |
| `backend/src/jobhunter/api/routes/{profile,settings,queries,watchlist,geo,spend}.py`, `api/pagination.py` *(new)* | The M2 routes of §14.2 (watchlist and geo taken from M4, Deviation 3); the §14.3 signed cursor for id-ordered lists |
| `backend/src/jobhunter/quota/cells.py`, `backend/src/jobhunter/sources/spec_adapters.py` *(new)* | M1 Carry-forward 13: `CellRow`, `ensure_cells` and `location_keys` leave `planner.py`, and so does the planning-time adapter factory |
| `backend/src/jobhunter/graphs/collect/nodes/{enqueue_targets,finalize}.py`, `backend/src/jobhunter/privacy/retention.py`, `backend/src/jobhunter/worker/runs.py` | `SCORE_TARGETS_ENABLED` (Deviation 10); the M2 retention rules and batched deletes; `schedule.stale_data` exempt while the schedule is disabled |
| `backend/src/jobhunter/worker/{runtime,operations,main,tasks}.py` | `OperationDeps`; `OperationContext` gains `deps`, `finish_in` and `requeue_in`; `WorkerRuntime` gains `gateway`, `parse_client`, `cv_store`, `profile_graphs` and `operation_deps`. Task 11 is the definition |
| `deploy/compose.dev.yaml`, `deploy/compose.fakellm.yaml` *(new)*, `deploy/images.lock`, `scripts/dev-secrets.sh` | `docparse`, the `cv_store` and `parse_jobs` volumes, the worker groups, the fake-LLM overlay, an optional `anthropic_key` |
| `.github/workflows/{ci.yml,schema-dry-run-manual.yml}` | The `payload` job, the `docparse` image build, and the manual dry-run |
| `scripts/spikes/record_anthropic.py`, `scripts/smoke_m2.py`, `scripts/check_llm_boundary.py` | The budgeted recorder, the M2 smoke and the AST boundary check |
| `docs/spikes/{V4,V5,V11,V13,V19}.md` | Spike results (V14 and the paid part of V5 move to M3, Deviation 9) |
| `tests/support/{llm,profile,settings}.py` | `FakeAnthropic` (recorded SSE), synthetic CV builders, settings helpers |

---

### Task 1: M2 schema, staged foreign keys, the `profile` fence branch and the M2 state machines

**Files:**
- Create: `backend/src/jobhunter/db/migrations/versions/0006_m2_llm_profile_settings.py`
- Modify: `backend/src/jobhunter/db/transitions.py`, `backend/src/jobhunter/db/migrate.py` (the backup-grant table list, if it names M1 tables), `tests/support/fixtures.py` (the M2 tables are emptied with `DELETE`, never by `_TRUNCATE`: `TRUNCATE … CASCADE` of any table that `active_config` reaches through a FK chain (`cv_files` → `profile_drafts` → `profile_versions`, `llm_calls`, `eval_runs`, `llm_tuples`) would truncate `active_config` itself. `reset_database` runs `_ACTIVE_CONFIG_RESET` first, which NULLs the profile, tuple and pending-scope pointers and restores the seeded ids, then `_M2_DELETE`, every child before its parent under the immediate FKs, in one transaction with `SET CONSTRAINTS ALL DEFERRED` (only the `profile_drafts` ↔ `profile_versions` pair is deferrable):
  1. `UPDATE profile_drafts SET llm_call_id = NULL` and `UPDATE query_proposals SET llm_call_id = NULL`;
  2. `profile_vectors`, then `llm_calls` (it references `llm_spend_ledger`, `profile_drafts` and `eval_runs`), then `llm_spend_ledger` (it references `llm_budget`);
  3. `query_proposals`, `eval_runs` and `llm_tuples` (the M1 `_TRUNCATE` has already emptied `runs`, which references it);
  4. `profile_drafts`, `profile_versions` and `cv_files`;
  5. `llm_budget`.
  `_CONFIGURATION_RESET` then also deletes the `model_policies`, `execution_configs` and `evaluation_policies` rows that no remaining `settings_versions` row or `active_config` names, which settings saves of earlier tests created, and the `rubric_versions` and `prompt_schema_versions` rows that `active_config` does not name, which tests insert for their tuples. The `_M2_DELETE` runs between `_ACTIVE_CONFIG_RESET` and `_CONFIGURATION_RESET`, after the M1 `_TRUNCATE`)
- Test: `tests/integration/test_schema_m2.py`, `tests/integration/test_is_fenced_profile.py`, `tests/integration/test_clean_db.py` (`test_two_resets_keep_active_config`: with a cv file, a confirmed draft, a profile version, a tuple (of a test rubric and prompt-schema row it inserts), a proposal, a vector, an eval run and an LLM call with its ledger row, all inserted and pointed at, two consecutive resets succeed and leave the one `active_config` row with its seeded pointers, every M2 table empty and the test rubric and prompt-schema rows gone); update `tests/integration/test_schema.py` (`LATER` loses `llm_tuples` and `profile_versions`), `tests/integration/test_schema_m1.py` (`profile_vectors` now exists; `DIRTY_CONFIG` no longer sets `profile_version_id = 1, llm_tuple_id = 1`, which the new FKs refuse: it inserts a real `cv_files` → `profile_drafts` → `profile_versions` → `llm_tuples` chain and points `active_config` at it, and `_M2_DELETE` deletes the chain), `tests/unit/test_transition_registry.py`

**Interfaces:**
- Consumes: the M0 and M1 DDL (inventory: `active_config`, `runs.llm_tuple_id`, `queries.proposal_id`, `purge_fences.kind='profile'`, `is_fenced` from 0005, the `assert_transition` trigger function).
- Produces:
  - Tables, copied verbatim from the spec:
    - `llm_tuples` (§7.3);
    - `cv_files`, `profile_drafts` with the `profile_drafts_one_open` index, and `profile_versions` with the `profile_versions_live_hash` partial unique index and the deferrable FKs (§7.4);
    - one column beyond the DDL (Deviation 2): `profile_drafts.confirm_response jsonb` with `CHECK (confirm_response IS NULL OR status = 'confirmed')`;
    - `query_proposals` (§7.5);
    - `profile_vectors` (§7.10);
    - `llm_budget`, `llm_spend_ledger` with its two indexes, and `llm_calls` with its three indexes (§7.11);
    - `eval_runs` (§7.13).
  - Staged FKs:
    - `profile_drafts.llm_call_id` and `query_proposals.llm_call_id → llm_calls(id) ON DELETE SET NULL`;
    - `llm_calls.eval_run_id → eval_runs(id) ON DELETE CASCADE`;
    - `active_config.profile_version_id → profile_versions(id)`;
    - `active_config.llm_tuple_id → llm_tuples(id)`;
    - `runs.llm_tuple_id → llm_tuples(id)`;
    - `queries.proposal_id → query_proposals(id) ON DELETE SET NULL`.
  - Columns created **without** their spec FK until M3 creates the referenced table (Deviation 1): `llm_calls.attempt_id`, `target_id`, `golden_case_id`, `batch_item_id`, and `llm_spend_ledger.bulk_operation_id`. Every spec CHECK on `llm_calls` and the ledger stays verbatim.
  - `is_fenced(...)` through `CREATE OR REPLACE` with the §7.14 body including the `profile` branch (M0 Deviation 2; the `p_tuple` parameter now resolves to the tuple's profile version).
  - Transition triggers `profile_drafts_assert_transition` (`status`), `llm_spend_ledger_assert_transition` (`state`), `llm_calls_assert_transition` (`status`), `query_proposals_assert_transition` (`status`) and `eval_runs_assert_transition` (`status`).
  - The `TRANSITIONS` entries:
    - `profile_drafts`:
      - the §7.16.8 edges, verbatim, every one: `uploaded→extracting_text`, `uploaded→extracting_profile`, `extracting_text→text_review`, `extracting_text→failed`, `text_review→extracting_profile`, `extracting_profile→ready`, `extracting_profile→failed`, `ready→text_review`, `ready→confirmed`, `text_review→discarded`, `ready→discarded`, `failed→discarded`, `failed→extracting_profile`;
      - `reason_col="status_reason"`, `changed_at` `status_changed_at` on every edge;
      - `owner_cols=frozenset({"reviewed_text_sha256", "native_pdf"})`, so Task 13's `draft` node can guard its write with `transition(owner={…})`;
      - settable: `reviewed_text`, `reviewed_text_sha256`, `native_pdf`, `transmitted_text_sha256`, `draft_json`, `validation_issues`, `failure_code`, `llm_call_id`, `confirmed_profile_version_id`, `confirm_response`, `content_purged_at`, `updated_at`.
      - "ready ──field edits──► ready" is a plain `UPDATE` of `draft_json`, not a transition.
    - `llm_spend_ledger`: `reserved→dispatched`, `reserved→not_billed`, `dispatched→billed`, `dispatched→not_billed`, `dispatched→ambiguous`, `ambiguous→billed`, `ambiguous→not_billed` (§7.16.7; M3's batches use the last two, and M2's late online settlement uses them too, Deviation 24); `reason_col="evidence"`; `dispatched→dispatched_at`; every settled state `→settled_at`; settable `settled_usd`.
    - `llm_calls`: `reserved→dispatched`, `reserved→cancelled`, `dispatched→completed`, `dispatched→error`, `dispatched→ambiguous`, plus `ambiguous→completed` and `ambiguous→error` for a late online settlement (Deviation 24); settable: every usage, response and `output_json` column of §7.11.
    - `query_proposals`: `running→ready`, `running→failed`; settable `items`, `llm_call_id`.
    - `eval_runs` (a fifth spec; §7.16 has none, as for `llm_calls`): `running→completed`, `running→failed`; `changed_at` `finished_at`; settable `passed`, `metrics`, `cost_usd`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_schema_m2.py
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
pytestmark = pytest.mark.integration
M2_TABLES = {"llm_tuples", "cv_files", "profile_drafts", "profile_versions", "query_proposals", "profile_vectors",
             "llm_budget", "llm_spend_ledger", "llm_calls", "eval_runs"}
async def test_m2_tables_and_staged_fks(engine) -> None:
    async with engine.connect() as conn:
        names = set((await conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))).scalars())
        assert M2_TABLES <= names and not {"score_targets", "score_attempts", "bulk_operations", "golden_cases"} & names
        fks = set((await conn.execute(text(
            "SELECT conrelid::regclass || '.' || a.attname FROM pg_constraint c "
            "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey) WHERE c.contype = 'f'"))).scalars())
        assert {"active_config.profile_version_id", "active_config.llm_tuple_id", "runs.llm_tuple_id",
                "queries.proposal_id", "profile_drafts.llm_call_id", "query_proposals.llm_call_id",
                "llm_calls.eval_run_id"} <= fks
        assert not {"llm_calls.attempt_id", "llm_calls.target_id", "llm_spend_ledger.bulk_operation_id"} & fks  # Deviation 1
async def test_one_open_draft_and_purged_hash_reuse(engine) -> None:
    ...  # a second open draft violates profile_drafts_one_open; a purged profile_versions row with the same
         # profile_hash does not block a new live row (Codex N12); ledger CHECKs: not_billed needs a listed evidence,
         # counted_usd = scoring + nonscoring, purpose 'extract_profile' cannot be 'scoring'
```

`tests/integration/test_is_fenced_profile.py`: an active `profile` purge fence on profile version `p` fences the tuple of `p` and no other tuple. Without a fence, `is_fenced` is false. `tests/unit/test_transition_registry.py` pins the five new specs' `allowed` sets exactly, and the `profile_drafts` spec's `owner_cols`.

- [ ] **Step 2: Run the tests and see them fail.** `uv run pytest -W error tests/integration/test_schema_m2.py tests/integration/test_is_fenced_profile.py tests/unit/test_transition_registry.py -q`. Expected: FAIL on the missing tables, the missing specs and the absent fence branch.
- [ ] **Step 3: Write migration 0006 and the `TRANSITIONS` entries.**
  - Copy the DDL verbatim, statement per `op.execute`, as 0005 does.
  - The module docstring lists the staged FKs and the columns that are left FK-less, with Deviation 1.
  - Create the tables in dependency order: `cv_files`, `profile_drafts`, `profile_versions` and the deferrable FKs, `llm_tuples`, `query_proposals`, `profile_vectors`, `llm_budget`, `eval_runs`, `llm_spend_ledger`, `llm_calls`, and then the `ALTER TABLE` FKs.
  - Add the triggers with `assert_transition('<col>')`.
  - Register the specs.
  - `seed.sync_transition_matrix` already rewrites the matrix at `migrate`.
- [ ] **Step 4: Run the tests and the whole M0/M1 schema suite.** `uv run pytest -W error tests/integration/test_schema*.py tests/integration/test_migrate.py tests/integration/test_clean_db.py tests/unit/test_transition_registry.py -q`. Expected: PASS.
- [ ] **Step 5: Commit** `feat(m2): M2 schema, staged foreign keys, the profile fence branch and the M2 state machines`.

### Task 2: Pricing and input bounds (pure)

**Files:**
- Create: `backend/src/jobhunter/llm/__init__.py`, `llm/pricing.py`, `llm/bounds.py`
- Test: `tests/unit/test_llm_pricing.py`, `tests/unit/test_llm_bounds.py`, `tests/property/test_llm_bounds_property.py`

**Interfaces:**
- Produces:
  - `pricing.Model = Literal["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1"]` and `pricing.MODELS: Final[tuple[Model, ...]]`.
  - `pricing.Effort = Literal["low", "medium", "high", "xhigh", "max"]` and `pricing.SUPPORTED_EFFORTS: Final[Mapping[Model, frozenset[Effort]]]`. All five efforts apply to every model unless `docs/council/research/tech.md` §B says otherwise, in which case cite it in the docstring.
  - `@dataclass(frozen=True) class pricing.Price: input: Decimal; output: Decimal; cache_write: Decimal; cache_read: Decimal`, all USD per MTok.
  - `pricing.price_for(model, *, prompt_tokens: int, batch: bool = False) -> Price`. Haiku switches tier above 100,000 prompt tokens; `batch` halves every price.
  - `@dataclass(frozen=True) class pricing.Usage: input_tokens: int; output_tokens: int; cache_creation_input_tokens: int; cache_read_input_tokens: int`.
  - `pricing.worst_case_usd(model, *, input_tokens_bound: int, max_tokens: int, batch: bool = False, extra_usd: Decimal = Decimal(0)) -> Decimal`, rounded **up** to 6 dp, with the tier picked by the bound.
  - `pricing.settled_usd(model, usage: Usage, *, batch: bool = False) -> Decimal`, rounded **half-up** to 6 dp, with the tier picked by the actual prompt tokens (input plus both cache counts).
  - `pricing.bound_input_price(model, *, batch: bool) -> Decimal`: the price per MTok at which a reservation bounds input, `max(input, cache_write)` of the model's highest tier, halved for `batch`. Task 15's overrun-buffer validation uses it.
  - `pricing.CONTEXT_WINDOW: Final[Mapping[Model, int]]`, the input context window per model in tokens, with its source cited in the docstring (`docs/council/research/tech.md` §B). Task 5's native-PDF count check uses it.
  - `bounds.PROMPT_OVERHEAD_TOKENS: Final[Mapping[Model, int]]`, 1,000 per model until Task 7 sets the V19 values.
  - `@dataclass(frozen=True) class bounds.RenderedRequest: system: tuple[str, ...]; messages: tuple[str, ...]; schema_json: str`.
  - `bounds.text_input_bound(model, request: RenderedRequest) -> int`, which is `Σ len(c.encode("utf-8")) + PROMPT_OVERHEAD_TOKENS[model]`.
  - `bounds.native_pdf_bound(model, counted_tokens: int) -> int`, which is `ceil(1.25 × counted_tokens) + PROMPT_OVERHEAD_TOKENS[model]`.
  - `bounds.native_pdf_extra_usd(overrun_buffer_usd: Decimal) -> Decimal`, which is `0.25 × buffer`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_llm_pricing.py
from decimal import Decimal
from jobhunter.llm.pricing import Usage, price_for, settled_usd, worst_case_usd
def test_worst_case_uses_the_cache_write_price_and_rounds_up() -> None:
    assert worst_case_usd("claude-opus-5-5", input_tokens_bound=10_000, max_tokens=16_000) == Decimal("0.370000")  # 10k×5.00 + 16k×20.00
    assert worst_case_usd("claude-sonnet-5-5", input_tokens_bound=1, max_tokens=1) == Decimal("0.000013")  # 2.5e-6 + 1e-5 → up
def test_settlement_rounds_half_up_and_charges_each_token_class() -> None:
    u = Usage(input_tokens=1000, output_tokens=500, cache_creation_input_tokens=100, cache_read_input_tokens=2000)
    assert settled_usd("claude-opus-5-5", u) == Decimal("0.014900")  # 0.004 + 0.010 + 0.0005 + 0.0004
def test_haiku_tiers_follow_the_bound_and_the_actual_prompt() -> None:
    assert price_for("claude-haiku-5-5", prompt_tokens=100_000).input == Decimal("0.10")
    assert price_for("claude-haiku-5-5", prompt_tokens=100_001).input == Decimal("0.50")
    assert price_for("claude-opus-5-5", prompt_tokens=1, batch=True).output == Decimal("10.00")
```

`tests/unit/test_llm_bounds.py`:
- German text with `ä`, `ß`, `„“` and `–` counts bytes, not characters;
- the schema JSON counts;
- the overhead is added once;
- the native-PDF bound for 1,001 counted tokens is `ceil(1251.25) + 1000`.

Property test: `worst_case_usd ≥ settled_usd(Usage(i, o, 0, 0))` for every `i ≤ bound − overhead` and `o ≤ max_tokens`, for every model, and for both the batch and the non-batch price.
- [ ] **Step 2: Run the tests and see them fail.** `uv run pytest -W error tests/unit/test_llm_pricing.py tests/unit/test_llm_bounds.py tests/property/test_llm_bounds_property.py -q`. Expected: `ModuleNotFoundError: jobhunter.llm`.
- [ ] **Step 3: Implement the two modules** with `Decimal` arithmetic only: no float anywhere in money, `ROUND_CEILING` for the reservation and `ROUND_HALF_UP` for the settlement.
- [ ] **Step 4: Run the tests and see them pass.**
- [ ] **Step 5: Commit** `feat(m2): LLM pricing and byte-bounded input reservations`.

### Task 3: The monthly budget row, the USD bucket and the spend ledger

**Files:**
- Create: `backend/src/jobhunter/llm/budget.py` (pure), `llm/spend_ledger.py` (DB), `llm/errors.py`, `tests/support/llm.py` (this task: `op_owner`, `reserve_kw`, `completed_call` and `budget_rows`; Tasks 4 and 5 extend it)
- Modify: `backend/src/jobhunter/observability/metrics.py` (the §17.5 names: the gauges `jobhunter_llm_spend_usd{state}` (`counted`, `billed`, `ambiguous`, `reserved`), `jobhunter_llm_bucket_usd` and `jobhunter_llm_headroom_usd`, and the histogram `jobhunter_llm_reservation_ratio{purpose}` of settled ÷ reserved. `settle` cannot observe its caller's commit, so it only returns their figures (`SettleFigures`, below), and Task 5's gateway updates the metrics after its transaction commits), `backend/src/jobhunter/worker/main.py` (`_REGISTERING_MODULES` += `jobhunter.llm.spend_ledger`), `tests/unit/test_worker_main.py` (pinned reconcilers), `tests/conftest.py` (`pytest_plugins` += `tests.support.llm`, placed after the modules it imports, per the file's ordering comment)
- Test: `tests/unit/test_llm_budget.py`, `tests/property/test_llm_spend_property.py`, `tests/integration/test_spend_ledger.py`

**Interfaces:**
- Consumes:
  - `pricing.*` and `bounds.native_pdf_extra_usd` (Task 2);
  - `OperationClaim` and `lock_operation_owner(conn, claim)` (M0);
  - `transition()`;
  - `raise_alert`, `open_alert`, `resolve_alert`;
  - `reconciler(name, *cadences)` and `fence_epoch` (M0).
- Produces:
  - Error types:
    - `errors.LLMError(DomainError)`;
    - `errors.BudgetExhausted(LLMError)`, with `purpose_class` and `headroom_usd`;
    - the `errors.LLMTransient`, `LLMRequestError(status)`, `DeadlineExhausted`, `MaxTokensExhausted`, `InvalidOutput(code)`, `Refused(category)` and `ModelMismatch(requested, served)` subclasses, used by Task 5.
  - Literals: `budget.Purpose = Literal["generate", "revise", "critic", "extract_profile", "propose_queries", "schema_dry_run"]` and `budget.PurposeClass = Literal["scoring", "nonscoring"]`.
  - `budget.month_of(t: datetime) -> date`, the UTC first-of-month, and `budget.month_length_s(month: date) -> int`.
  - `@dataclass(frozen=True) class budget.BudgetParams: cap_usd: Decimal; overrun_buffer_usd: Decimal; nonscoring_reserve_pct: int; min_bucket_capacity_usd: Decimal; collection_interval_s: int`, built from settings by `BudgetParams.from_payload(payload)`.
  - **Which value wins.** Inside the database, `cap_usd` and `overrun_buffer_usd` come from the locked month row only. The row is created from the active settings, and every save of class `budget` writes the new values to it under its lock (Task 17). `BudgetParams.cap_usd`/`overrun_buffer_usd` are used only by the pure layer and by `ensure_month` for a new month. `nonscoring_reserve_pct`, `min_bucket_capacity_usd` and `collection_interval_s` are always the active settings' values, read in the same transaction (`spend_ledger.current_params`, below). No caller carries `BudgetParams` across transactions.
  - Pure budget arithmetic:
    - `budget.admissible(p) -> Decimal`, which is `A`;
    - `budget.rate_per_s(p, month) -> Decimal`;
    - `budget.bucket_capacity(p, month) -> Decimal`, which is `B_max`;
    - `budget.accrue(bucket_usd: Decimal, bucket_at: datetime, now: datetime, p, month) -> Decimal`.
  - `@dataclass class budget.BudgetRow`, with every `llm_budget` column.
  - `budget.admits(row: BudgetRow, r: Decimal, purpose_class: PurposeClass, p: BudgetParams) -> bool`. It applies the §6.9.1 scoring and non-scoring rules with `A = row.cap_usd − row.overrun_buffer_usd` and `p.nonscoring_reserve_pct`, and admits nothing after `cap_lowered_at` while `counted_usd ≥ row.cap_usd − row.overrun_buffer_usd`.
  - Spend owners:
    - `SpendOwner = OperationOwner | EvalRunOwner`;
    - `@dataclass(frozen=True) class OperationOwner: claim: OperationClaim`;
    - `@dataclass(frozen=True) class EvalRunOwner: eval_run_id: int`, created here with its `lock`, its `durable` and its `owner_holds` branch; Task 7 only uses it.
    - Each has `async lock(conn) -> None`, which takes `FOR SHARE` on its row and raises `FencedOut` when the predicate is lost.
    - Each has `durable() -> dict[str, JSONValue]`, written into `llm_calls.input_refs["owner"]` by the same insert as the reservation: `{"kind": "operation", "id": <operation id>, "claim_id": "<uuid>"}` or `{"kind": "eval_run", "id": <eval run id>}`. This is the reconciler's only way to test the original owner after the in-memory owner is gone.
    - `async def spend_ledger.owner_holds(conn, ref: Mapping[str, JSONValue], *, now) -> bool` locks the owner row `FOR UPDATE` and tests the predicate. For an operation, the row exists, `status = 'running'` and `claim_id` equals the recorded one. For an eval run, the row is `running`. A missing row never holds.
    - The `llm_ledger_unsettled` pass first fails every `eval_runs` row that has been `running` for longer than `EVAL_RUN_ABANDONED = timedelta(hours=1)` (a CLI that died, whether between cases or inside a call), through `transition()` on Task 1's `eval_runs` spec (`running→failed`), so its unsettled entries then fail `owner_holds`.
    - M3 adds a target owner.
  - `@dataclass(frozen=True) class spend_ledger.Reservation: ledger_id: int; llm_call_id: int; month: date; reserved_usd: Decimal; purpose_class; model; batch: bool`.
  - Ledger operations:
    - `async def spend_ledger.current_params(conn) -> BudgetParams`. It reads the active settings payload through `active_config … FOR SHARE`, so it runs first in the transaction (§9.1), and waits for a settings save in progress, which holds `active_config FOR UPDATE`.
    - `async def spend_ledger.ensure_month(conn, month, p: BudgetParams, now) -> None`, an `INSERT … ON CONFLICT DO NOTHING` that copies the cap and buffer from `p` (the current params) and starts the bucket full.
    - `async def spend_ledger.lock_month(conn, month, p: BudgetParams, *, now) -> BudgetRow`, a `FOR UPDATE` that accrues the bucket to `now` and stores it. The accrual uses `p`'s `nonscoring_reserve_pct`, `min_bucket_capacity_usd` and `collection_interval_s`, and the row's own `cap_usd` and `overrun_buffer_usd`.
    - `async def spend_ledger.read_month(conn, month, p: BudgetParams, *, now) -> BudgetRow | None`, the read-only variant: a plain `SELECT` without `FOR UPDATE`, with the same accrual done in memory and nothing stored; `None` when the month has no row. Task 16's dry-run, `/api/system/spend` and the health `llm` section use it.
    - `async def spend_ledger.reserve(conn, *, owner, purpose, purpose_class, model, effort, max_tokens, max_tokens_stage, transport_try, logical_call_no, deadline_s, input_tokens_bound, native_pdf_extra: bool, execution_config_id, prompt_template_version, rendered_prompt_sha256, prefix_sha256, input_refs, profile_draft_id, eval_run_id, transport, now) -> Reservation`. It runs one transaction body in the caller's transaction, in the §3.3.3 order (owner row before `llm_budget`):
      0. `p = current_params(conn)` (`active_config FOR SHARE`);
      1. the owner lock;
      2. `ensure_month(conn, month_of(now), p, now)`;
      3. the month lock, `lock_month(conn, month, p, now=now)`;
      4. the worst case, `pricing.worst_case_usd(model, input_tokens_bound=…, max_tokens=…)`, plus `bounds.native_pdf_extra_usd(row.overrun_buffer_usd)` when `native_pdf_extra` is true: the extra share comes from the month row just locked, never from a value the caller carried;
      5. admission, or `BudgetExhausted`;
      6. the ledger insert, with `admitted_cap_usd` and `counted_before_usd`;
      7. the `llm_calls` insert (`reserved`), with `input_refs["owner"] = owner.durable()` and `input_refs["deadline_s"] = deadline_s`, the deadline the call runs under, and `logical_call_no` (1 for a node's first logical call, 2 for its repair; Deviation 26);
      8. the counters and the bucket (scoring outside bulk operations only);
      9. the `llm.cap_80pct` check.
    - `async def spend_ledger.mark_dispatched(conn, res, owner, *, now) -> None`. It takes the owner lock again, then `reserved→dispatched` on both rows. It raises `FencedOut` when the owner is lost; the caller then calls `release_never_dispatched`.
    - `async def spend_ledger.release_never_dispatched(conn, res, *, now) -> None`, which sets `not_billed('never_dispatched')` and moves the call to `cancelled`.
    - `async def spend_ledger.settle(conn, res, *, outcome: Literal["billed", "not_billed", "ambiguous"], evidence: str | None, usage: Usage | None, call: CallOutcome, now) -> SettleFigures`. It takes the month lock, applies the §6.9.1 counter table, and charges a bound violation in full with `evidence='reservation_bound_violated'` and the critical `llm.reservation_bound_violated`. It updates no metric: it returns the figures, and the gateway updates the metrics after its transaction commits (Task 5), so a rolled-back settlement is never counted.
    - `@dataclass(frozen=True) class spend_ledger.SettleFigures: settled_usd: Decimal; reserved_usd: Decimal; purpose: Purpose; spend_by_state: Mapping[str, Decimal]; bucket_usd: Decimal; headroom_usd: Decimal`, the inputs of the metrics above.
    - `@dataclass(frozen=True) class spend_ledger.CallOutcome`, with the `llm_calls` response fields: `model_served`, `http_status`, `error_code`, `stop_reason`, `refusal_category`, `provider_request_id`, `output_json`, `first_event_at`, and the status.
    - `async def spend_ledger.change_cap(conn, *, month, cap_usd, overrun_buffer_usd, now) -> None`. Lowering the cap below `counted_usd` records `cap_lowered_at` and `counted_at_cap_change_usd`; raising it resolves `llm.cap_80pct` when the share drops below 80%.
    - `async def spend_ledger.recompute(conn, month) -> Drift | None`, the counters recomputed from the ledger with the §7.11 comment's formula.
  - Reconcilers:
    - `@reconciler("budget_counters", "heavy")`: corrects drift under the month lock and raises `llm.budget_counter_drift` (critical); resolves it after a clean pass.
    - `@reconciler("llm_ledger_unsettled", "tick")` (Deviation 5), per entry in its own transaction, in the §9.1 order (owner row, month row, ledger row):
      - a `reserved` entry older than 15 min for which `owner_holds(conn, input_refs["owner"])` is false becomes `not_billed('never_dispatched')`, and its call `cancelled`;
      - an online `dispatched` entry becomes `ambiguous`, and its call too, once it is older than `max(15 min, input_refs["deadline_s"] + 5 min)`: the deadline recorded with the call, which covers every purpose, the schema dry-run's 300 s included. §17.4's flat 15 minutes would mark a legitimate 900 s or 1,800 s call ambiguous while its stream is live (Deviation 24);
      - the counters change in the same transaction.
    - **Late settlement** (Deviation 24). `settle` on a ledger row the reconciler already moved to `ambiguous` applies `ambiguous → billed` (with usage) or `ambiguous → not_billed` (with a listed evidence), and `ambiguous → completed`/`error` on the call, with the same counter adjustments. A settle that finds the row already `billed` or `not_billed` writes nothing and logs `llm_settle_duplicate`.
  - The alert codes are module constants: `CAP_80PCT_ALERT = "llm.cap_80pct"`, `COUNTER_DRIFT_ALERT`, `BOUND_VIOLATED_ALERT` and `CAP_REACHED_ALERT = "llm.cap_reached"` (raised by M3's scoring denial).

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_llm_budget.py
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from jobhunter.llm.budget import BudgetParams, admissible, admits, bucket_capacity, month_of, rate_per_s
P = BudgetParams(cap_usd=Decimal("100.00"), overrun_buffer_usd=Decimal("2.00"), nonscoring_reserve_pct=5,
                 min_bucket_capacity_usd=Decimal("1.00"), collection_interval_s=6 * 3600)
OCT = date(2026, 10, 1)
def test_defaults_give_the_spec_shares() -> None:
    assert admissible(P) == Decimal("98.00")
    assert round(rate_per_s(P, OCT) * 3600, 3) == Decimal("0.125")  # 93.10 / (31 d × 24 h); §6.9.2 quotes ≈ 0.127 for a 30.5-day month
    assert round(bucket_capacity(P, OCT), 2) == Decimal("1.50")       # 2 × rate × 6 h; ≈ 1.53 in §6.9.2's 30.5-day month
    assert bucket_capacity(BudgetParams(**{**P.__dict__, "collection_interval_s": 3600}), OCT) == Decimal("1.00")
    assert month_of(datetime(2026, 10, 31, 23, 59, tzinfo=UTC)) == OCT
def test_scoring_never_uses_the_unspent_nonscoring_reserve(budget_row) -> None:
    row = budget_row(counted=Decimal("93.00"), scoring=Decimal("93.00"), nonscoring=Decimal(0))
    assert admits(row, Decimal("0.10"), "scoring", P) is True and admits(row, Decimal("0.11"), "scoring", P) is False
    assert admits(row, Decimal("5.00"), "nonscoring", P) is True and admits(row, Decimal("5.01"), "nonscoring", P) is False
```

(`budget_row` is a small factory fixture in the test module.)

`tests/property/test_llm_spend_property.py` (§18.1) is a `RuleBasedStateMachine` over the pure layer. Its rules: reserve, settle billed / not_billed / ambiguous, cap edits up and down, and a month boundary. Its invariants:
- `counted ≤ max(cap, counted_at_cap_change)` in every month;
- every ledger row has `counted_before + reserved ≤ admitted_cap − buffer`;
- the counters equal the ledger sums.

A second machine covers the bucket. Over a simulated month with an unbounded scoring backlog and any tick pattern, scoring spend up to day `d` ≤ `d/month × scoring share + B_max + one cycle's in-flight reservations`, and a 1,180-target backlog does not exhaust the month in its first days (the Fable B1 scenario).

`tests/integration/test_spend_ledger.py`:
- `test_eight_concurrent_sessions_never_exceed_the_cap` (§18.2): 8 connections mixing scoring and non-scoring reservations and a cap edit serialise on the month row;
- `test_settlement_after_rollover_adjusts_the_reservation_month` (Review Focus 4);
- `test_lowered_cap_admits_nothing_but_settles_in_flight` (Review Focus 4);
- `test_bound_violation_is_charged_in_full_and_alerts`;
- `test_drift_is_corrected_and_alerted`;
- `test_unsettled_entries_are_resolved_after_15_minutes`, for both edges, with the counters updated: a reservation whose operation was reclaimed (a new `claim_id`) is released, while the replacement execution's own reservation, under the same operation id, is kept;
- `test_dispatched_extraction_is_not_marked_ambiguous_inside_its_deadline`: an `extract_profile` call dispatched 16 min ago (deadline 900 s) is untouched, and at 21 min it is `ambiguous`;
- `test_late_settlement_after_the_reconciler`: the reconciler marks a dispatched call `ambiguous`, then the gateway settles it `billed`; the counters equal the ledger;
- `test_abandoned_eval_run_releases_its_reservation`: a `running` schema dry-run older than an hour with a `reserved` entry ends `failed`, and the entry becomes `not_billed('never_dispatched')`; one that died between two completed cases, with no unsettled entry, ends `failed` too; a schema dry-run call dispatched 16 minutes ago is `ambiguous` (its recorded deadline is 300 s);
- `test_old_context_cap_cut_and_rollover`, the three conditions in one scenario: an extraction reserves on 31 October with the cap at $100; a save lowers the cap to $10 (the October row is changed); the extraction's retry after midnight creates November from the **current** settings ($10), not from any value it held;
- `test_superseded_operation_owner_cannot_reserve_or_dispatch`: `FencedOut`, and nothing is written;
- `test_native_pdf_extra_comes_from_the_locked_month_row`: with `native_pdf_extra=True` the reserved amount is the worst case plus `0.25 ×` the row's `overrun_buffer_usd`, after a save changed the buffer;
- `read_month` returns the accrued bucket, takes no row lock and writes nothing.

```python
# tests/integration/test_spend_ledger.py (the Review Focus 4 pins)
async def test_settlement_after_rollover_adjusts_the_reservation_month(engine, op_owner) -> None:
    t = datetime(2026, 10, 31, 23, 59, 30, tzinfo=UTC)
    async with engine.begin() as conn:
        res = await reserve(conn, **reserve_kw(op_owner, now=t, bound=4000, max_tokens=1000))
        await mark_dispatched(conn, res, op_owner, now=t)
    async with engine.begin() as conn:  # settled after midnight UTC
        await settle(conn, res, outcome="billed", evidence=None, usage=Usage(1000, 200, 0, 0),
                     call=completed_call(), now=t + timedelta(minutes=2))
    oct_row, nov_row = await budget_rows(engine, date(2026, 10, 1), date(2026, 11, 1))
    assert oct_row.counted_usd == Decimal("0.008000") and nov_row is None   # 1000×4 + 200×20 per MTok
async def test_lowered_cap_admits_nothing_but_settles_in_flight(engine, op_owner) -> None:
    ...  # reserve r1 (dispatched), change_cap to below counted → cap_lowered_at set; a new reserve raises
         # BudgetExhausted; settle r1 billed succeeds and counted ≤ max(cap, counted_at_cap_change_usd)
```

(`op_owner`, `reserve_kw`, `completed_call` and `budget_rows` are in `tests/support/llm.py`, which this task creates and registers in `tests/conftest.py`.)

- [ ] **Step 2: Run the tests and see them fail.** `uv run pytest -W error tests/unit/test_llm_budget.py tests/property/test_llm_spend_property.py tests/integration/test_spend_ledger.py -q`
- [ ] **Step 3: Implement** `budget.py` (pure) first, then `spend_ledger.py`.
  - Every function takes the caller's `conn`, as `quota/ledger.py` does.
  - The lock order is owner row → `llm_budget` → ledger rows. Assert it in `test_lock_order_owner_then_month` with the M1 lock-wait helpers (`tests/support/pg.lock_waits`).
  - Both reconcilers start with `fence_epoch(conn, ctx.epoch)` and let the worker's own `FencedOut` propagate (M1 Ruling FR-w). Pin this with `assert_fence_rule`.
- [ ] **Step 4: Run the tests and see them pass.** Then run `tests/unit/test_worker_main.py` with the reconciler pin updated.
- [ ] **Step 5: Commit** `feat(m2): the serialised LLM budget month row, the USD bucket and the spend ledger`.

### Task 4: The Anthropic SDK, the gateway client factory, the fake transport, the budgeted recorder and spikes V13 and V5 (local part)

**Files:**
- Modify: `pyproject.toml` (`anthropic==1.15.0`, `langchain-anthropic==1.7.6`), `uv.lock`, `backend/src/jobhunter/config.py` (`AppConfig.fake_llm: bool` from `JOBHUNTER_FAKE_LLM`, `AppConfig.fake_llm_dir: Path` from `JOBHUNTER_FAKE_LLM_DIR`, default `tests/contract/fixtures/anthropic`), `.gitignore` (`.fixtures-raw/` already ignored; verify it), `tests/contract/test_fixture_pii.py` (gate `anthropic/**`)
- Create: `backend/src/jobhunter/llm/gateway.py` (this task: `GatewayChatAnthropic` and `_chat` only), `llm/transport.py`, `llm/fake.py`, `scripts/spikes/record_anthropic.py`, `scripts/spikes/v13_structured_stream.py`, `tests/contract/fixtures/anthropic/` (recorded synthetic streams plus scenario streams), `docs/spikes/V13.md`, `docs/spikes/V5.md` (local part)
- Modify: `tests/support/llm.py` (created and registered by Task 3; this task adds `FakeAnthropic` and `LocalSSEServer`)
- Test: `tests/integration/test_llm_transport.py`, `tests/unit/test_fake_llm.py`, `tests/unit/test_recorder_budget.py`

**Interfaces:**
- Consumes: `AppConfig.secret("anthropic_key")`.
- Produces:
  - `transport.ANTHROPIC_BASE_URL: Final = "https://api.anthropic.com"`.
  - `transport.TIMEOUT: Final = anthropic.Timeout(connect=10, read=120, write=30, pool=10)`.
  - `transport.make_http_client(cfg: AppConfig, *, read_timeout_s: float = 120.0) -> httpx2.AsyncClient`. Its timeout is built from `TIMEOUT` with only the read value overridable, so the client object carries the one timeout of every request. In fake mode it mounts `fake.RecordedSSETransport(cfg.fake_llm_dir)`, and the read timeout can be injected for tests only.
  - `fake.RecordedSSETransport(root: Path)`. It serves a recorded SSE body chosen by `(model, purpose header X-JH-Purpose, schema sha256[:12])` from `root/<purpose>/<model>/<schema12>.sse`. **The real gateway code runs above it.**
    - `fake.scenario(*names)` sets an ordered per-test sequence through a context variable: each HTTP try takes the next name, the last name repeats, and `ok` is the stored stream. The per-try names are `ok`, `rate_limited`, `overloaded_529`, `stall_after_first_event`, `max_tokens`, `refusal`, `model_mismatch`, `http_401`, `connect_refused` and `parse_error`, plus the content variants `enum_stripped` (the stored body with one enum field holding a value outside its enum, as if the API had stripped the constraint) and `set_invalid` (a structurally valid queries body with five items). So `fake.scenario("rate_limited", "ok")` is a 429 and then the stored stream, and `fake.scenario("overloaded_529")` is a 529 on every try.
    - `fake.count_tokens(result: int | Literal["error"])` sets the per-test answer of `/v1/messages/count_tokens`, a JSON endpoint: `{"input_tokens": result}`, or an error response for `"error"`.
    - Later tasks add `llm/fake.py` to their Files only if they add a variant.
  - `gateway._chat(model, effort, max_tokens, schema, *, api_key: SecretStr, http_client: httpx2.AsyncClient) -> Runnable`, built per §10.2. It passes `timeout=http_client.timeout`, so the client's per-request timeout (which Stainless SDK clients send with each request) is the injected one.
  - `gateway.GatewayChatAnthropic`. It subclasses `ChatAnthropic` only if V5/V13 show that `ChatAnthropic` cannot take `anthropic.Timeout`, or cannot use the injected `http_client`. If so, it overrides the async client factory to build `anthropic.AsyncAnthropic(base_url=…, api_key=…, timeout=…, max_retries=0, http_client=…)` (G3). The decision and its evidence go in `docs/spikes/V5.md`.
  - The recorder: `scripts/spikes/record_anthropic.py --spike {v13,v4,v19} --dry-run|--live`.
    - It is the only place that maps `JOBHUNTER_ANTHROPIC_API_KEY` to a temporary `anthropic_key` secret file.
    - It counts and bounds every send before it happens (Global Constraints).
    - It writes raw request and response captures to `.fixtures-raw/anthropic/`.
    - It writes the synthetic-input SSE recordings to `tests/contract/fixtures/anthropic/` with the `request-id`, `anthropic-organization-id` and `cf-ray` headers removed.
    - Next to each stream it stores the byte size of each rendered component of the request (system blocks, messages, the `output_config` schema) in a sidecar file, so Task 6's payload tests can compute the recording's input bound without the request capture.

- [ ] **Step 1: Write the failing tests.**
  - `test_fake_llm.py` serves each per-try name and the two content variants, a sequence (each try takes the next name and the last repeats), and both answers of `fake.count_tokens` on `/v1/messages/count_tokens`. `stall_after_first_event` sends one `message_start` and then nothing.
  - `test_recorder_budget.py` checks that the 9th message, or one whose worst case would pass the $0.80 running sum, is refused **before** any socket opens (no transport is created). The count survives a crash, because it is written before the send.
  - `test_llm_transport.py` (integration, with a local SSE server on `127.0.0.1:0` in `tests/support/llm.py`, `LocalSSEServer`):
    - `_chat(...)` reaches `LocalSSEServer` through a transport mount on `https://api.anthropic.com` (the base URL stays the constant), and the request host is `api.anthropic.com`, with `stream: true`, `output_config.format.type == "json_schema"`, `output_config.effort`, no `tool_choice`, `tools` or `temperature`, and the exact `max_tokens`;
    - an injected 1 s read timeout fires on a stalled stream within 1 s ± 0.5. This is the mechanism of V5; the 120 s value is measured once in the spike, marked `slow`.
- [ ] **Step 2: Run the tests and see them fail** on the missing modules.
- [ ] **Step 3: Implement** the transport, the fake, the client factory and the recorder, in that order.
- [ ] **Step 4: Run the spikes.**
  - **V5 local:** run `LocalSSEServer` with the real 120 s idle read timeout once. Record the observed timeout, whether `ChatAnthropic` accepted `anthropic.Timeout`, and which variant the SDK honours (the client `timeout` sent with each request, or the http client's own), in `docs/spikes/V5.md`. The 20 real streamed generator and critic calls are M3's (Deviation 9).
  - **V13 (2 paid calls):** `uv run python scripts/spikes/record_anthropic.py --spike v13 --live`. It makes one Sonnet and one Opus call with the §11.12 schema literal (the `schemas.queries` module is Task 6's), effort `low` and `max_tokens` 1,024, on a synthetic profile. Its recordings serve V13 and `test_fake_llm.py` only; Tasks 13 and 20 hand-author the streams their tests replay. Assert that `raw.response_metadata` / `usage_metadata` expose `stop_reason`, `stop_details`, the served `model` and the cache usage fields, and that the captured request carries `output_config.format` and `effort` and no `tool_choice`. Record the field paths in `docs/spikes/V13.md`, with no content and no key. If a field is missing, the gateway reads it from the raw `anthropic` event stream through the subclass; note the decision.
  - Run `uv run pytest tests/contract/test_fixture_pii.py -q` over the new fixtures.
- [ ] **Step 5: Run the tests and see them pass**; then run `ruff`, `mypy` and `tests/unit/test_toolchain.py`, since the TID251 ban still fires outside `llm/**`.
- [ ] **Step 6: Commit** `feat(m2): Anthropic client factory, recorded-SSE fake transport, budgeted spike recorder; spikes V13 and V5 (local)`.

### Task 5: The gateway call loop, errors, `stop_reason` handling, the cache monitor, the boundary lint, spike V11 and crash cases C15 and C35 (dispatch marker)

**Files:**
- Modify: `backend/src/jobhunter/llm/gateway.py`, `llm/errors.py` (`InputUnbounded`), `backend/src/jobhunter/worker/operations.py` (the `registry` keyword of `execute_operation`), `worker/main.py` (`_REGISTERING_MODULES` += `jobhunter.llm.gateway`, the module of the `llm_refusal_rate` reconciler), `observability/metrics.py` (the §17.5 names `jobhunter_llm_calls_total{model,effort,purpose,outcome}`, counted per HTTP try, and `jobhunter_llm_cache_read_ratio{model,effort,role}`), `.github/workflows/ci.yml` (`lint`: `uv run python scripts/check_llm_boundary.py`), `tests/unit/test_worker_main.py` (the reconciler pin gains `llm_refusal_rate: ["heavy"]`), `tests/support/llm.py` (`fake_llm` with its `.served` request count, `create_llm_call_operation`, `reclaim_stale_now`, `ledger_states`)
- Create: `backend/src/jobhunter/llm/cache_monitor.py`, `scripts/check_llm_boundary.py`, `docs/spikes/V11.md`
- Test: `tests/unit/test_gateway_policy.py`, `tests/integration/test_gateway.py`, `tests/integration/test_gateway_timeout_policy.py` (V11), `tests/integration/test_c15_stalled_stream.py`, `tests/integration/test_c35_operation_stall.py` (dispatch-marker part), `tests/unit/test_llm_boundary_check.py`, `tests/unit/test_cache_monitor.py`, `tests/integration/test_llm_refusal_rate.py`

**Interfaces:**
- Consumes: `spend_ledger.*` (with `reserve(…, native_pdf_extra=…)` and `SettleFigures`) and `errors.*` (Task 3); `pricing` (with `CONTEXT_WINDOW`)/`bounds` (Task 2); `transport`/`_chat` and `fake.scenario`/`fake.count_tokens` (Task 4).
- Produces:
  - `gateway.NodePurpose = budget.Purpose`.
  - `@dataclass(frozen=True) class gateway.NodeConfig: purpose; model: Model; effort: Effort; max_tokens: int; max_tokens_retry: int; deadline_s: int; schema: type[BaseModel]`.
  - `gateway.node_config(purpose, *, model_policy: Mapping[str, JSONValue], execution: Mapping[str, JSONValue], schema) -> NodeConfig`. It reads `nodes.<purpose>` and `<purpose>` of the stored payloads (inventory 1a), and a missing effort is `ValueError` (G5).
  - `@dataclass(frozen=True) class gateway.RenderedPrompt`:
    - fields: `system: tuple[SystemBlock, ...]`, `messages: tuple[HumanMessage, ...]`, `template_version: str`, `input_refs: Mapping[str, JSONValue]`;
    - `SystemBlock(text, cache: bool)`;
    - `rendered_sha256() -> bytes`;
    - `prefix_sha256() -> bytes | None`;
    - `as_request(schema) -> bounds.RenderedRequest`.
  - `@dataclass class gateway.CallContext`: `owner: SpendOwner`, `purpose_class`, `execution_config_id`, `deadline: float` (monotonic; the node's active `deadline_s` from now, enforced per call, §10.2 `deadline = ctx.deadline`), `profile_draft_id: int | None = None`, `eval_run_id: int | None = None`, `logical_call_no: int = 1` (2 for a node's one repair; written to every try's reservation and `llm_calls` row), `native_pdf: bool = False`.
  - `errors.InputUnbounded(LLMError)`: the native-PDF input could not be bounded, because `count_tokens` failed or counted more than `pricing.CONTEXT_WINDOW[model]`. Nothing is reserved or sent.
  - `@dataclass(frozen=True) class gateway.GatewayResult: parsed: BaseModel; llm_call_id: int; usage: Usage; model_served: str; stop_reason: str; cost_usd: Decimal`.
  - `class gateway.LLMGateway(engine, cfg, *, http_client: httpx2.AsyncClient, clock=utcnow, monotonic=time.monotonic, sleep=asyncio.sleep, cache: CacheMonitor)`. Its methods:
    - `async def call(self, node: NodeConfig, prompt: RenderedPrompt, ctx: CallContext) -> GatewayResult` is the §10.2 loop, verbatim in its order:
      - each try runs reserve, dispatch marker, stream and settle, each as its own transaction on `engine`;
      - `FIRST_TRY_MIN_REMAINING_S = 30` and `RETRY_MIN_REMAINING_S = 120`;
      - `is_retryable_status` covers 408, 409, 429, 500, 502, 503, 504 and 529;
      - the jittered backoff is 2 s then 8 s ± 25%, and `Retry-After` is honoured.
      - The input bound is `bounds.text_input_bound(model, prompt.as_request(schema))`, with `reserve(…, native_pdf_extra=False)`. With `ctx.native_pdf` true, the gateway first runs `count_tokens` on the exact request: a failed count, or one above `pricing.CONTEXT_WINDOW[model]`, raises `InputUnbounded` before any reservation; otherwise the bound is `bounds.native_pdf_bound(model, count)` and every try reserves with `native_pdf_extra=True`, so `reserve` adds the extra share from the month row it locks.
      - Every try passes `logical_call_no=ctx.logical_call_no` to `reserve`.
      - The metrics of Task 3 (`jobhunter_llm_spend_usd{state}`, `jobhunter_llm_bucket_usd`, `jobhunter_llm_headroom_usd`, `jobhunter_llm_reservation_ratio{purpose}`) are updated from `settle`'s `SettleFigures` after the settlement transaction commits, never inside it.
    - `async def count_tokens(self, model, body) -> int` (native PDF and V19).
  - The `stop_reason` and status mapping (§10.5, §10.4):
    - `end_turn` with `parsed` returns the result. `parsing_error` raises `InvalidOutput("structural")`; the try settles `billed`, its call `completed` with `output_json = NULL` and `error_code = 'invalid_output'`, and the caller owns its one structural repair.
    - `refusal` is billed and raises `Refused(category or "unspecified")`.
    - `max_tokens` moves to the enlarged stage, then `MaxTokensExhausted`.
    - Any other stop reason raises `InvalidOutput("unexpected_stop_reason")` with a warn alert, `llm.unexpected_stop_reason` (Deviation 6).
    - `model_served ≠ model_requested` raises `ModelMismatch` with the critical `llm.model_mismatch`.
    - Each of these billed terminal outcomes settles its call `completed` with `output_json = NULL` and an `error_code`: `refused`, `max_tokens`, `unexpected_stop_reason` or `model_mismatch`. A receipt then tells every completed call apart (Task 13): a newest completed call with one of these codes ends the draft or proposal with that code and sends nothing.
    - 401/403 raise `LLMRequestError` with the critical `llm.auth_failed`, which a later successful call resolves.
  - `cache_monitor.CacheMonitor`, per §10.8:
    - `observe(model_served, effort, purpose, prefix_sha256, started_at, cache_read_tokens) -> None`;
    - the key is `(model_served, effort, purpose, prefix_sha256)`, with `zero_read_streak` (≥ 3 raises `llm.cache_miss_streak`, warn) and the rolling-1 h ratio gauge.
    - It is in-process, and M2's single calls carry no cache marker, so it mostly observes M3.
  - `@reconciler("llm_refusal_rate", "heavy")`: more than 5% refusals over 24 h with n ≥ 20 raises `llm.refusal_rate`, and the rate falling below resolves it.

- [ ] **Step 1: Write the failing tests.**
  - `test_gateway_policy.py`:
    - the backoff bounds;
    - `Retry-After` wins;
    - a first try starts only with ≥ 30 s left, and a retry or the enlarged stage only with ≥ 120 s left (`DeadlineExhausted`);
    - `is_retryable_status`.
  - `test_gateway.py`, on the fake transport with `fake.scenario(*names)` sequences (each case asserts the ledger rows, the `llm_calls` rows and the counters):
    - `scenario("rate_limited", "ok")` gives 2 tries: `not_billed(error_response_no_content)` and then `billed`;
    - `scenario("overloaded_529")` (repeated on every try) gives `LLMTransient`, with 3 `not_billed` rows;
    - `scenario("max_tokens", "ok")` gives `billed`, `billed`, the enlarged `max_tokens` on try 2, and `max_tokens_stage='enlarged'`;
    - `refusal` gives `billed`, `Refused`, and the call `completed` with `error_code='refused'`;
    - `model_mismatch` gives `billed`, `ModelMismatch`, the alert and `error_code='model_mismatch'`;
    - `http_401` gives `not_billed`, `LLMRequestError(401)` and `llm.auth_failed`;
    - `scenario("connect_refused", "ok")` gives `not_billed(connect_failed_before_send)` and a retry;
    - `parse_error` gives `InvalidOutput("structural")` and `error_code='invalid_output'`;
    - `logical_call_no=2` in the `CallContext` is recorded on the reservation and the call;
    - native PDF: with `fake.count_tokens(1800)` the reservation's bound is `native_pdf_bound(model, 1800)` and its amount includes `0.25 ×` the month row's buffer; `fake.count_tokens("error")`, and a count above `pricing.CONTEXT_WINDOW[model]`, raise `InputUnbounded` with no reservation and `fake_llm.served == 0`;
    - the spend metrics change only after the settlement commits: a settlement rolled back by an injected error leaves them unchanged.
  - **The owner check:** a reclaimed operation (its `claim_id` cleared) makes `mark_dispatched` fail. The try is then `not_billed(never_dispatched)`, and **no request reaches the transport** (the transport counts requests).
  - `test_gateway_timeout_policy.py` (V11): a LangGraph node with `TimeoutPolicy(run_timeout=2)` calls the gateway on `stall_after_first_event`. Cancellation lands within 1 s of the deadline, the try is `ambiguous`, the `llm_calls` row is `ambiguous`, and `asyncio.all_tasks()` holds no task created by the call afterwards.
  - C15 (`tests/integration/test_c15_stalled_stream.py`; the stall is in the stream, not in a process, so it runs in-process): a gateway call on `fake.scenario("stall_after_first_event", "ok")` meets the stall on try 1 and the stored stream on try 2, with a read timeout of 2 s injected through `make_http_client(..., read_timeout_s=2)`. Try 1 settles `ambiguous` and keeps its reservation; try 2 makes a new reservation and is `billed`.
  - C35, dispatch-marker part: this runs in one process, because a paused worker still holds the singleton lock, so a second worker process could never start. A real zombie is another process with its own execution registry, so each execution gets its own `ExecutionRegistry` through a new keyword `execute_operation(..., registry: ExecutionRegistry = EXECUTIONS)`, passed to `run_owned`. The default keeps the singleton's local-exclusion guarantee (`AlreadyExecuting`).
    1. Execution 1 of an operation, on registry R1, pauses at `gateway.after_reserve` (`tests/support/faults.PausePoint`).
    2. The reclaim statement of the `operations` reconciler runs on its own (`operations._reclaim_stale(conn, now=…)` with `now` past `OPERATION_STALE`): `claim_id := NULL`, attempts + 1. The reconciler's cancel step only cancels executions in `EXECUTIONS`, which R1 is not.
    3. Execution 2, on registry R2, claims the operation and completes the call.
    4. Execution 1 resumes. Its `mark_dispatched` raises `FencedOut`, its try becomes `not_billed(never_dispatched)`, and **exactly one** HTTP request was served.
    A second test, `test_reconciler_cancels_a_local_stalled_execution`, runs execution 1 on `EXECUTIONS` and the real `reconcile_operations`. The local execution is cancelled and awaited, nothing is sent, and its `reserved` entry is released by `llm_ledger_unsettled` 15 minutes later, because its recorded claim no longer holds.
    Task 13 adds the full extraction flow.
  - `test_llm_boundary_check.py` gives `check_llm_boundary.py` small source snippets: each banned construct fails as a name, an attribute and a keyword argument; a string literal naming a banned key (`assert "temperature" not in body`) passes; and the gateway's own `with_structured_output(..., method="json_schema", ...)` passes.
  - `test_cache_monitor.py`: a streak of 3 zero-read observations on one `(model_served, effort, purpose, prefix_sha256)` key raises `llm.cache_miss_streak`, a cache read resets the streak, other keys are independent, and the rolling-1 h ratio gauge follows the observations.
  - `test_llm_refusal_rate.py`: over 24 h, 19 calls with 2 refusals raise nothing (n < 20); 20 calls with 2 refusals (10%) raise `llm.refusal_rate`; one refusal in 20 (5%, not above) raises nothing; a rate falling to 5% or below resolves the open alert; and the reconciler's fence rule holds (`assert_fence_rule`).

```python
# tests/integration/test_c35_operation_stall.py (pinned; in-process, see the step above)
async def test_reclaimed_operation_never_sends(engine, current_epoch, fake_llm, monkeypatch) -> None:
    op_id = await create_llm_call_operation(engine)                      # tests/support/llm.py: registers a gateway-calling handler for kind 'eval_run' for this test only; it closes over its gateway and runs with deps=None
    pause = PausePoint("gateway.after_reserve"); monkeypatch.setattr(gateway_module, "afault_point", pause)
    first = asyncio.create_task(execute_operation(engine, op_id, epoch=current_epoch, registry=ExecutionRegistry()))
    await asyncio.wait_for(pause.reached.wait(), 10)
    await reclaim_stale_now(engine, op_id)                               # the reconciler's reclaim statement only: claim_id := NULL, attempts + 1
    await execute_operation(engine, op_id, epoch=current_epoch, registry=ExecutionRegistry())
    pause.resume(); await first
    assert fake_llm.served == 1
    assert await ledger_states(engine, op_id) == ["not_billed:never_dispatched", "billed"]
```

(`PausePoint` holds only the first arrival at its point, so the second execution passes it at once. `reclaim_stale_now(engine, op_id)` in `tests/support/llm.py` runs `operations._reclaim_stale` with `now` past `OPERATION_STALE`, and asserts it reclaimed `op_id`. Task 11 adds a `deps` keyword to `execute_operation`, defaulting to `None`; this test's handler needs none.)

- [ ] **Step 2: Run the tests and see them fail.**
- [ ] **Step 3: Implement** the loop, the error mapping, `InputUnbounded`, the cache monitor, the refusal-rate reconciler and the AST checker. The checker visits `ast.Name`, `ast.Attribute` and `ast.keyword` nodes only, never string constants. Add `gateway.after_reserve` and `gateway.after_dispatch` to the fault points. Register `jobhunter.llm.gateway` in `_REGISTERING_MODULES` and update the reconciler pin. Write `docs/spikes/V11.md` from the test's measurements.
- [ ] **Step 4: Run the tests and see them pass**, plus `tests/unit/test_worker_main.py` with the `llm_refusal_rate` pin. Run `uv run python scripts/check_llm_boundary.py` over the repo: clean.
- [ ] **Step 5: Commit** `feat(m2): the LLM gateway call loop with per-try reservations, stop-reason handling, cache monitor; boundary lint; V11; crash C15 and C35 (dispatch marker)`.

### Task 6: Structured-output schemas, the M2 prompts, rubric v1, prompt-schema version 1 and the payload contract tests

**Files:**
- Create:
  - `backend/src/jobhunter/llm/schemas/{__init__,generator,critic,profile,queries}.py`
  - `backend/src/jobhunter/llm/prompts/{profile_extract.md,propose_queries.md,render.py}`
  - `backend/src/jobhunter/scoring/rubric/{__init__.py,v1.yaml}`
  - `backend/src/jobhunter/db/migrations/versions/0007_m2_rubric_prompt_seed.py`
  - `tests/payload/` with its `conftest.py` (marker `payload`)
- Modify: `backend/src/jobhunter/db/seed.py` (fresh installs set the rubric and prompt pointers), `tests/support/fixtures.py` (`_ACTIVE_CONFIG_RESET` restores the seeded rubric and prompt ids), `pyproject.toml` (marker `payload`; the runtime pin `pyyaml==6.0.3`, today only a transitive lock entry through langchain-core, for `rubric.load_rubric`; and the dev-group pin `types-PyYAML==<the version resolved at uv lock>`, recorded in the commit message, because PyYAML ships no inline types and `mypy --strict` needs them), `uv.lock`, `.github/workflows/ci.yml` (a `payload` job, `uv run pytest -W error tests/payload -q`, M0 Deviation 15)
- Test: `tests/unit/test_llm_schemas.py`, `tests/unit/test_rubric_v1.py`, `tests/payload/test_profile_payload.py`, `tests/payload/test_queries_payload.py`, `tests/integration/test_seed_m2.py`; update the M0/M1 tests that pin the NULL pointers:
  - `tests/integration/test_schema.py`: `test_active_config_m2_columns_nullable` expects `NO` for `rubric_version_id` and `prompt_schema_version_id`;
  - `tests/integration/test_schema_m1.py`: `SEEDED_CONFIG` expects the seeded rubric and prompt-schema ids instead of NULL;
  - `tests/integration/test_migrate.py`: the fresh-install assertion compares `rubric_version_id` and `prompt_schema_version_id` with the seeded rows' ids instead of `IS NULL`.

**Interfaces:**
- Consumes: `gateway.RenderedPrompt`/`SystemBlock` (Task 5); `content_hash`/`canonical_json` (`db/seed.py`).
- Produces:
  - `schemas.profile.ProfileOut`, the structural mirror of the §12.4 schema: types, enums, required keys and `extra="forbid"`, with **no** limits.
  - `schemas.profile.UserEntered`: `work_authorization_de` defaults to `"unspecified"`, `security_clearances` defaults to `[]`.
  - `schemas.profile.ConfirmedProfile(ProfileOut)`, with `user_entered: UserEntered = Field(default_factory=UserEntered)`. The LLM never produces `user_entered` (§12.4), so a `draft_json` stored from the model validates as `ConfirmedProfile` with the defaults.
  - `schemas.queries.QueryProposalOut` (§11.12), and `schemas.generator.GeneratorOut` (§11.5) and `schemas.critic.CriticOut` (§11.7.3). The generator and critic mirrors are structural only, and their semantic validators are M3's.
  - `schemas.json_schema(model: type[BaseModel]) -> dict[str, JSONValue]` is the spec's JSON schema. A test pins each model to the spec text: generator §11.5, critic §11.7.3, profile §12.4 and queries §11.12, compared as canonical JSON.
  - `schemas.transformed(model) -> dict[str, JSONValue]`, which is `anthropic.transform_schema(json_schema(model))`, and `schemas.SCHEMAS: Final[Mapping[str, type[BaseModel]]]`, which is `{"generator", "critic", "profile", "queries"}`.
  - `render.RENDERER_VERSION: Final = "1"` and `render.template_version(name: Literal["profile_extract", "propose_queries"]) -> str`, which is `"<name>@<sha256(template)[:12]>"`.
  - `render.extract_profile_prompt(reviewed_text: str) -> RenderedPrompt`. The text goes inside `<cv_text>` in the user message; there is no cache marker and no dynamic content in the system block.
  - `render.extract_profile_native_pdf_prompt(pdf: bytes) -> RenderedPrompt`, which uses a `document` block.
  - `render.propose_queries_prompt(profile: ConfirmedProfile, locations: Sequence[str]) -> RenderedPrompt`.
  - `profile_extract.md` instructs the model:
    - to extract only from the CV text;
    - to copy every `evidence` verbatim;
    - to take languages only from explicit statements;
    - to use month-precision dates;
    - **never** to infer or output the §12.5 attributes;
    - never to output contact data.
  - `propose_queries.md` asks for 6–16 queries in DE/EN, each with a rationale and its target title, at least one `exploration`, and no location words.
  - `rubric.load_rubric("v1") -> Rubric(weights: Mapping[str, int], anchors: Mapping[str, JSONValue])`, which reads `scoring/rubric/v1.yaml` with `yaml.safe_load` (the pinned `pyyaml`). Validation requires exactly the 8 §11.2 keys, integers, and a sum of 100. The anchors are the §11.3 tables verbatim: the level anchors, the `experience_seniority` sub-rating tables and the `unknown`/`unconstrained` rules. `Rubric.content_hash` is the content hash of `{weights, anchors}`.
  - `render.prompt_schema_v1() -> tuple[templates, schemas]`: `templates = {"profile_extract", "propose_queries"}`, `schemas = transformed(each of SCHEMAS)`, and `prompt_schema_hash = sha256(canonical_json(templates) ‖ canonical_json(schemas) ‖ RENDERER_VERSION)`.
  - Migration 0007 has two halves:
    - **Rows:** it inserts rubric `v1` and the prompt-schema row from these pure functions (`ON CONFLICT DO NOTHING` by hash).
    - **Pointers:** it sets `active_config.rubric_version_id` and `prompt_schema_version_id` where the row exists and they are NULL, then `ALTER … SET NOT NULL` on both (M0 Deviation 1). On a fresh install the row does not exist yet, and `seed_defaults` inserts it with both pointers.

- [ ] **Step 1: Write the failing tests.**
  - `test_llm_schemas.py`:
    - each `json_schema(model)` equals the spec JSON (a `SPEC_SCHEMAS` literal in the test module, copied from the spec sections);
    - `ProfileOut` accepts a 200-skill output (no limits at this stage) but rejects an extra key and an unknown enum;
    - `transformed` keeps `enum`, `anyOf` and `$ref`.
  - `test_rubric_v1.py` checks the weights `{role_fit: 20, skills: 25, experience_seniority: 15, domain: 10, languages: 10, education_certifications: 5, location_work_mode: 10, employment_salary: 5}` and their sum, and that a broken YAML (a sum of 99, or a 9th key) is refused.
  - `test_seed_m2.py`:
    - after `migrate`, `active_config` points at the v1 rows and both columns are NOT NULL;
    - a second `migrate` is a no-op;
    - an upgrade from a database at 0006 with an `active_config` row sets the pointers.
  - Payload tests (§10.9, offline): they drive `gateway._chat` and `bounds` with a capturing transport, never `LLMGateway.call`, so they need no database (the `payload` CI job has no Postgres). For `extract_profile` and `propose_queries`:
    - the host is `api.anthropic.com`;
    - `output_config.format.type == "json_schema"` with exactly `transformed(schema)`;
    - `output_config.effort` equals the policy;
    - `max_tokens` equals the execution config's first stage;
    - `stream` is true;
    - there is no `tool_choice`, `tools`, `temperature`, `top_p`, `top_k` or `fallbacks`;
    - the last message has role `user`;
    - there is **no** `cache_control` anywhere, since these are single calls;
    - for every real recording of Tasks 4 and 7, the input bound computed from the component byte sizes the recorder stored next to the stream (Task 4), plus `PROMPT_OVERHEAD_TOKENS[model]`, is `≥` the recorded `usage.input_tokens + cache tokens`. The hand-authored synthetic streams of Tasks 13 and 20 are not recordings and are excluded.
- [ ] **Step 2: Run the tests and see them fail.** `uv run pytest -W error tests/unit/test_llm_schemas.py tests/unit/test_rubric_v1.py tests/payload tests/integration/test_seed_m2.py -q`
- [ ] **Step 3: Implement** the schemas, the templates, the renderer, the rubric, migration 0007 and the seed change.
- [ ] **Step 4: Run the tests and see them pass.** Then run the schema and seed suites of Task 1, with the three updated M0/M1 files (`tests/integration/test_schema.py`, `test_schema_m1.py`, `test_migrate.py`), and `uv run mypy`.
- [ ] **Step 5: Commit** `feat(m2): structured-output schemas, M2 prompts, rubric v1 and prompt-schema version 1 with restored NOT NULL pointers; payload contract tests`. The message records the `types-PyYAML` version resolved at `uv lock`.

### Task 7: The schema dry-run (§10.11), spike V4 and spike V19

**Files:**
- Create: `backend/src/jobhunter/llm/schema_dry_run.py`, `scripts/spikes/v19_token_bound.py`, `.github/workflows/schema-dry-run-manual.yml`, `docs/spikes/V4.md`, `docs/spikes/V19.md`
- Modify: `backend/src/jobhunter/cli.py` (`schema-dry-run [--live]`), `llm/bounds.py` (`PROMPT_OVERHEAD_TOKENS` from V19)
- Test: `tests/integration/test_schema_dry_run.py`, `tests/unit/test_v19_analysis.py`

**Interfaces:**
- Consumes: `LLMGateway`, `schemas.SCHEMAS` and `transformed`, `gateway.node_config`, the active `model_policies` and `execution_configs` rows, `spend_ledger.EvalRunOwner` (Task 3, used as it is), the `eval_runs` transitions (`running→completed`, `running→failed`, Task 1), and `fake.scenario("enum_stripped")` (Task 4).
- Produces:
  - `schema_dry_run.DRY_RUN_EFFORT: Final = "low"`, `DRY_RUN_MAX_TOKENS: Final = 2000` and `DRY_RUN_DEADLINE_S: Final = 300`.
  - `schema_dry_run.cases(model_policy: Mapping[str, JSONValue]) -> list[DryRunCase]`. A `DryRunCase` is `(schema_name, model)`: one case per schema, for each distinct model configured for its nodes (generator → the `generate` and `revise` models, critic → `critic`, profile → `extract_profile`, queries → `propose_queries`), plus the informational `minimum` probe on the generator model.
  - `async def schema_dry_run.run(engine, gateway, *, prompt_schema_version_id: int, model_policy_id: int, execution_config_id: int, now) -> DryRunReport`. For each case it sends one minimal synthetic request (a synthetic profile and ad, effort `low`) and asserts:
    - HTTP 200 and `stop_reason='end_turn'`;
    - structural validation;
    - the sent `output_config.format.schema` byte-equal to `transformed`;
    - every enum field allowed.
    The probe records 400 or accepted. Results go to an `eval_runs` row (`kind='schema_dry_run'`, `route='production'`, `passed`, `metrics`, `cost_usd`), which ends `completed` or `failed` through `transition()`, and the calls carry `eval_run_id` and purpose `schema_dry_run` (non-scoring). The owner is `EvalRunOwner`, so the `eval_runs` row must be `running`.
  - CLI `jobhunter schema-dry-run [--live]`. It uses the worker-role config; `--live` requires `anthropic_key`, and the default runs on the fake transport. It prints the case table (schema, model, result) and never content.
  - `.github/workflows/schema-dry-run-manual.yml`:
    - triggered by `workflow_dispatch` only;
    - starts a throwaway Postgres from `images.lock`, runs `jobhunter migrate`, then runs the CLI with the repository secret `ANTHROPIC_API_KEY_SCHEMA_DRY_RUN` written to the secret file;
    - prints only the table.
  - V19:
    - `scripts/spikes/v19_token_bound.py` takes ≥ 30 synthetic rendered requests per model (German and English, with `ä ö ü ß`, `„“`, `–`, `…`), covering the profile, queries, generator-shaped and critic-shaped bodies.
    - For each it calls `messages.count_tokens` (free) per component and for the whole request, and checks `tokens ≤ bytes` per component.
    - It checks every recorded `usage` of Tasks 4 and 7 against the computed bound.
    - It writes `PROMPT_OVERHEAD_TOKENS[model] = 2 × max(whole − Σ components)` into `bounds.py`.
    - It makes **1** paid native-PDF extraction of a synthetic 1-page PDF through the recorder, to check `usage ≤ 1.25 × count_tokens`. The recorder budgets it at `pricing.worst_case_usd(model, input_tokens_bound=native_pdf_bound(model, count), max_tokens=1024, extra_usd=native_pdf_extra_usd(Decimal("0.50")))`, the extra share at the §13.1 minimum buffer. When the check fails, or the call is refused by the budget, `bounds.NATIVE_PDF_ENABLED = False` and §12.7's native path stays disabled (Task 9's upload, Task 13's node and Task 14's `repairs` and retry honour it).
    - It computes the §6.9.1 overrun-buffer bound of the seeded `settings_v1.json` inline, with the new constants (Task 15's `overrun_buffer_bound` does not exist yet): the 3 score and 2 auxiliary online slots, each at `max over the policy's configured models of PROMPT_OVERHEAD_TOKENS[m] × bound_input_price(m, batch=False)`, plus `batch_max_items` batch slots at `PROMPT_OVERHEAD_TOKENS[g] × bound_input_price(g, batch=True)` of the generator model `g`, all divided by 1e6 and rounded up to the cent. Task 15's `test_seed_defaults_validate` is the standing guard afterwards. If the seeded `overrun_buffer_usd` ($2.00) falls below it, the task stops and reports to the user: the spec's default and §6.9.1's bound then disagree, and only a spec change (a larger default buffer or a smaller `batch_max_items`) resolves it. Validation is never weakened to make the seed pass.

- [ ] **Step 1: Write the failing tests.**
  - `test_schema_dry_run.py`, on the fake transport with the V4 recordings of Step 4, checks that:
    - the default policy yields 5 calls: generator/Sonnet, critic/Opus, profile/Opus, queries/Opus, and the probe;
    - the `eval_runs` row is `completed` and `passed`;
    - a stripped enum in a recorded response (`fake.scenario("enum_stripped")`) fails the run;
    - a sent schema differing by one byte fails;
    - every call has `max_tokens` 2,000 and effort `low`.
  - `test_v19_analysis.py` pins the pure analysis function: tokens over bytes fails the check, and the overhead doubling.
- [ ] **Step 2: Run the tests and see them fail.**
- [ ] **Step 3: Implement** the dry-run module, the CLI and the workflow.
- [ ] **Step 4: Run the spikes.**
  - **V4 (5 paid calls):** `uv run python scripts/spikes/record_anthropic.py --spike v4 --live`. It runs the dry-run cases through the recorder and stores the recordings for the fake. `docs/spikes/V4.md` records the case table, the probe result (`minimum` rejected or accepted) and the actual cost.
  - **V19 (count_tokens plus 1 paid call):** `uv run python scripts/spikes/v19_token_bound.py --live`. Record the per-model maxima, the new constants and the native-PDF verdict in `docs/spikes/V19.md`.
  - A violation of `tokens ≤ bytes` applies the §10.7 factor rule (`BYTE_FACTOR` in `bounds.py`); restate S3 in V19.md and flag it in the task report.
- [ ] **Step 5: Run the tests and see them pass**, including `tests/payload`, since the bound now uses the V19 constants.
- [ ] **Step 6: Commit** `feat(m2): schema dry-run with eval_runs records and the manual workflow; spikes V4 and V19 set the prompt overhead`.

### Task 8: The `docparse` container, its file protocol and the worker's parse client

**Files:**
- Create: `docparse/{__init__.py,Dockerfile,worker.py,extract.py}` (`__init__.py` as `embedder/` has one), `backend/src/jobhunter/profile/__init__.py`, `profile/parse_client.py`, `tests/docparse/{conftest.py,test_extract.py,test_worker.py}`, `tests/support/profile.py` (synthetic CV builders: `minimal_pdf(lines) -> bytes`, a hand-written PDF 1.4 with a Helvetica text stream; `image_only_pdf() -> bytes`; `docx_bytes(paragraphs, table=None, *, macro=False) -> bytes` built with `zipfile`; every name in them is invented)
- Modify: `tests/conftest.py` (`pytest_plugins` += `tests.support.profile`, after the modules it imports, per the file's ordering comment), `pyproject.toml` (group `docparse`: `pypdf==6.20.1`, `pdfplumber==0.11.10`, `python-docx==1.2.0`; marker `docparse`; `addopts` excludes `docparse` as it excludes `embedder_model`), `deploy/compose.dev.yaml` (service `docparse`, volumes `cv_store` and `parse_jobs`, the worker's `group_add: ["10010", "10011"]`: a service-level `group_add` replaces the `x-app` anchor's list, so both the secrets group and the parse group are named, as §19.1 has it; `api` and `worker` mount `cv_store`), `Dockerfile` (the runtime stage creates `/data/cv` owned by 10001:10001 with mode 0700 before `USER`, so a fresh named volume takes that ownership), `.github/workflows/ci.yml` (job `docparse`: `uv sync --frozen --group docparse` then `uv run pytest -W error -m docparse tests/docparse -q`; the `integration` job also syncs `--group docparse`, because Task 11's `op_runner` serves parse jobs with an in-process `docparse.worker` loop; the image build in the `integration` job's stack step or a dedicated build step), `.dockerignore`
- Test: `tests/integration/test_parse_client.py`, `tests/stack/test_docparse_no_network.py` (marker `stack`)

**Interfaces:**
- Produces:
  - The protocol, version 1, on `/data/parse/<job_id>/`:
    - the worker writes `input.<pdf|docx>` (mode 0660), then `request.json` atomically (tmp then rename): `{"v": 1, "mime": …, "input": "input.pdf", "mode": "extract" | "validate"}`. `validate` checks a PDF (encryption, page count) and extracts no text; `text.txt` is then not written.
    - `docparse` claims it by renaming `request.json` to `request.claimed`, extracts, and writes `text.txt` plus `result.json` atomically: `{"v": 1, "status": "ok" | "error", "code": null | "encrypted" | "too_many_pages" | "parse_error" | "parse_limit" | "unsupported", "method": "pypdf" | "pdfplumber" | "docx", "page_count": int | null, "chars": int}`.
  - `docparse.extract.validate_pdf(path) -> Validated(page_count)`, which gives `encrypted` or `too_many_pages` as `extract_pdf` does, and `ParseClient.validate(data) -> ParseOutcome` for it.
  - `docparse.extract.extract_pdf(path) -> Extracted` (pypdf, then pdfplumber when the text is < 300 characters or > 5% U+FFFD; `page_count`; encrypted → `encrypted`; > 20 pages → `too_many_pages`), `extract_docx(path) -> Extracted` (paragraphs and table cells in document order) and `@dataclass(frozen=True) Extracted(text, method, page_count)`.
  - `docparse.worker.run_job(job_dir: Path, *, extractor=extract_document, limits: Limits = Limits(cpu_s=30, as_bytes=768 * 2**20)) -> None` runs the extraction in a child of `multiprocessing.get_context("spawn")` (never a fork: a fork from a multi-threaded process warns on Python 3.12+, which `-W error` turns into a failure, for example in Task 11's `op_runner`), with `resource.setrlimit` for `RLIMIT_CPU` and `RLIMIT_AS`. A child killed by a limit gives `parse_limit`, and any other child failure gives `parse_error`.
  - `docparse.worker.serve(root: Path, *, stop: threading.Event | None = None) -> None` loops on `root`: it scans every 0.2 s until `stop` is set, never opens a socket, and logs only job ids and codes. `docparse.worker.main()` calls `serve` on the root named by `JOBHUNTER_PARSE_JOBS_DIR` (default `/data/parse`). Tests run `serve(tmp_root, stop=event)` in a thread.
  - `profile.parse_client.ParseClient(root: Path, *, timeout_s: float = 60.0, poll_s: float = 0.2)`. Its methods:
    - `async def extract(self, data: bytes, mime: Literal["application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"]) -> ParseOutcome`. `ParseOutcome = ParseOk(text, method, page_count) | ParseFailed(code)`. It creates `<uuid>/` with mode 0770 and the group inherited from the setgid root, and removes the directory in `finally`. A timeout gives `ParseFailed("timeout")`.
    - `def cleanup_leftovers(self) -> int`, run at worker start. A missing root removes nothing and returns 0.
  - The `cv_store` volume (named) is mounted at `/data/cv` on `api` and `worker`.
  - The `parse_jobs` volume is a tmpfs (`driver_opts: {type: tmpfs, device: tmpfs, o: "mode=2770,uid=0,gid=10011"}`) at `/data/parse` on `worker` and `docparse`.
  - `docparse` runs `network_mode: none`, `read_only: true`, `tmpfs: [/tmp]`, `cap_drop: [ALL]`, `security_opt: ["no-new-privileges:true"]`, `user: "10002:10002"` and `group_add: ["10011"]`, with no secrets and `mem_limit: 1g`.

- [ ] **Step 1: Write the failing tests.** The `docparse` tests need the `docparse` group.
  - `test_extract.py`:
    - a synthetic 2-page text PDF yields its text with method `pypdf`;
    - `image_only_pdf()` yields < 300 characters, and `run_job`'s result is `ok` with `chars < 300` (the low-text decision is the worker's, Task 11);
    - an encrypted PDF gives `encrypted`;
    - a 21-page PDF gives `too_many_pages`;
    - a DOCX with a table keeps the cell order.
  - `test_worker.py`:
    - `test_rlimit_kills_a_runaway_parse` (Review Focus 3) passes an extractor that burns CPU, with `Limits(cpu_s=1, as_bytes=…)`, and the result is `parse_limit` within 5 s;
    - the claim is a rename, so a second worker cannot claim the same job;
    - `result.json` appears only after `text.txt` is complete.
  - `test_parse_client.py` runs a docparse loop thread (`serve(tmp_root, stop=event)`) on a tmp directory: a round trip, a timeout, the directory always removed, `cleanup_leftovers` removing stale directories, and `cleanup_leftovers` on a missing root returning 0.
  - `test_docparse_no_network.py` (stack): `docker compose exec docparse python -c "import socket; socket.create_connection(('1.1.1.1', 53), 2)"` fails, and so does DNS resolution. That is test N1's docparse half (§18.2).

```python
# tests/docparse/test_worker.py (Review Focus 3 pin)
def burn(_path, _mime):  # never returns: CPU-bound
    while True:
        pass
def test_rlimit_kills_a_runaway_parse(tmp_path) -> None:
    job = make_job(tmp_path, minimal_pdf(["Max Mustermann"]), "application/pdf")
    started = time.monotonic()
    run_job(job, extractor=burn, limits=Limits(cpu_s=1, as_bytes=256 * 2**20))
    result = json.loads((job / "result.json").read_text())
    assert result["status"] == "error" and result["code"] == "parse_limit" and time.monotonic() - started < 5
```

- [ ] **Step 2: Run the tests and see them fail.** `uv run --group docparse pytest -W error -m docparse tests/docparse -q`, then `uv run pytest -W error tests/integration/test_parse_client.py -q`
- [ ] **Step 3: Implement** the extractor, the loop, the Dockerfile and the client.
  - The Dockerfile follows `embedder/Dockerfile`: `uv sync --locked --no-dev --no-install-project --only-group docparse`, copying `docparse/` only, and user 10002.
  - The compose service and volumes follow the Interfaces block.
- [ ] **Step 4: Run the tests and see them pass**: the docparse group, the parse client, then `docker compose … up -d --build docparse` and the stack test.
- [ ] **Step 5: Commit** `feat(m2): network-less docparse container with rlimited extraction, its file protocol and the worker parse client`.

### Task 9: CV upload and paste: checks, encrypted storage and the draft with its operation

**Files:**
- Create: `backend/src/jobhunter/profile/upload.py`, `profile/storage.py`, `backend/src/jobhunter/api/routes/profile.py` (this task: the two upload routes)
- Modify: `pyproject.toml` (`python-multipart==0.0.32`), `backend/src/jobhunter/api/app.py` (include the router), `backend/src/jobhunter/config.py` (`AppConfig.cv_store_dir: Path` from `JOBHUNTER_CV_STORE_DIR`, default `/data/cv`), `backend/src/jobhunter/crypto.py` (`data_keys`), `backend/src/jobhunter/api/routes/auth.py` (uses `crypto.data_keys`), `backend/src/jobhunter/api/deps.py` (`get_data_keys`), `frontend/src/api/schema.d.ts` (regenerated, M1 pre-flight P8), `tests/support/profile.py` (`docx_bytes(…, declared_expansion=…)`, which declares an expanded size in the ZIP central directory), `tests/support/fixtures.py` (`count_rows(engine, table, **filters)`, where a tuple value means `IN`, and the `cv_store_dir` fixture, a tmp directory that the `api_app` fixture passes as `cv_store_dir`)
- Test: `tests/unit/test_upload_checks.py`, `tests/integration/test_profile_upload_api.py`

**Interfaces:**
- Consumes: `crypto.encrypt/decrypt`, `create_operation`, `transition()`, `require_session`, `SESSION_RESPONSES`/`CSRF_RESPONSES`, `Problem`, `bounds.NATIVE_PDF_ENABLED` (Task 7).
- Produces:
  - `crypto.data_keys(cfg: AppConfig) -> list[bytes]`, moved from `api/routes/auth._data_keys` so that the worker reads the keys without importing FastAPI; `api/deps.get_data_keys` wraps it, and `auth.py` calls it.
  - `upload.MAX_UPLOAD_BYTES: Final = 10_485_760`, `upload.MAX_DOCX_EXPANDED: Final = 50 * 2**20`, `upload.MAX_DOCX_ENTRIES: Final = 1000`, `upload.PASTE_MIN, PASTE_MAX: Final = 300, 50_000`.
  - `upload.Detected = Literal["application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"]`.
  - `upload.check_file(head: bytes, data: bytes, filename: str) -> Detected`. It raises `UploadRejected(reason)` with these reasons:
    - `too_large`;
    - `unknown_type`;
    - `extension_mismatch`;
    - `docx_structure`, for a missing `[Content_Types].xml` or `word/document.xml`;
    - `docx_macro`, for any `vbaProject.bin`;
    - `docx_expansion`, for a declared total over 50 MiB or more than 1,000 entries.
    It reads the ZIP central directory through the stdlib `zipfile` on the bytes and **never parses PDF structure**; encryption and page count are docparse's (Deviation 7).
  - `upload.check_paste(text: str) -> str`, which enforces 300–50,000 characters.
  - `upload.read_multipart_limited(request: Request, *, limit: int) -> UploadedFile(filename, data, native_pdf_opt_in: bool)`. It streams `request.stream()` into `python_multipart.MultipartParser` with a byte counter, and stops reading with 413 as soon as the file part passes `limit`, so nothing past the limit is ever held.
  - The upload writes the encrypted file before its database transaction. When that transaction fails, the route deletes the file in its exception path. Task 23's `cv_store_orphans` rule catches a crash between the two.
  - `storage.CvStore(root: Path)`:
    - `write(data: bytes, keys) -> StoredFile(storage_path: str, sha256: bytes)` writes `<uuid4>.bin`, mode 0600, with AES-256-GCM and `aad=b"cv_file:" + name`;
    - `read(storage_path, keys) -> bytes`;
    - `delete(storage_path) -> None`;
    - `encrypt_text(text, keys, *, cv_file_id) -> bytes` and `decrypt_text(blob, keys, *, cv_file_id) -> str` use `aad=b"cv_text:%d" % id`.
  - `async def upload.request_draft_operation(conn, kind: Literal["extract_profile_text", "extract_profile"], draft_id: int, *, defer) -> tuple[int, bool]`, the one `create_operation` wrapper for both draft kinds: payload `{"draft_id": <id>}` and dedup key `extract_profile_text:<id>` or `extract_profile:<id>`. Task 14's approve and retry routes reuse it, and Tasks 11 and 13 read `payload["draft_id"]`.
  - `upload.open_new_draft(conn, *, cv: NewCvFile, native_pdf: bool, defer) -> int`. In one transaction it:
    1. locks the open draft `FOR UPDATE`, if one exists;
    2. raises `Problem(409, "conflict_state")` when the open draft is `uploaded`, `extracting_text` or `extracting_profile`;
    3. discards the open draft when it is in `text_review` or `ready`, then discards every `failed` draft (`profile_drafts_one_open` does not cover `failed`, so there may be several);
    4. inserts `cv_files` and `profile_drafts(status='uploaded')`; on a PDF native opt-in the `cv_files` row has `native_pdf_opt_in = true` and `extraction_method = 'native_pdf'` (the §7.4 CHECK `NOT native_pdf_opt_in OR extraction_method = 'native_pdf'`);
    5. inserts the operation through `request_draft_operation`: `extract_profile_text`, or `extract_profile` (`uploaded→extracting_profile`, `native_pdf := true` on the draft) on a PDF native opt-in. That path still validates the PDF in docparse before any token count or LLM call (Task 13's `check_pdf`).
  - The routes:
    - `POST /api/profile/cv` takes multipart `file` and `native_pdf_opt_in?`, and returns 202 `{draft_id}`. While `bounds.NATIVE_PDF_ENABLED` is false, a native opt-in answers 422 `validation_failed` with `errors=[{field: "native_pdf_opt_in", …}]` and stores nothing (Deviation 9).
    - `POST /api/profile/cv-text` takes `{text}` and returns 202 `{draft_id}`. Pasted text is stored as `cv_files(mime='text/plain', extraction_method='pasted', raw_text_enc=…)` with no file on disk.
    - The problems are 413 `upload_rejected` and 422 `upload_rejected` with `errors=[{field: "file", code: <reason>}]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_profile_upload_api.py (Review Focus 3 pin)
async def test_hostile_uploads_are_rejected_before_storage(logged_in_client, engine, cv_store_dir) -> None:
    cases = [
        ("big.pdf", b"%PDF-1.4\n" + b"0" * (10_485_760 + 1), 413, "too_large"),
        ("bomb.docx", docx_bytes(["x"], declared_expansion=60 * 2**20), 422, "docx_expansion"),
        ("macro.docx", docx_bytes(["x"], macro=True), 422, "docx_macro"),
        ("setup.pdf", b"MZ\x90\x00" + b"\x00" * 2048, 422, "unknown_type"),
        ("cv.docx", minimal_pdf(["Erika Musterfrau"]), 422, "extension_mismatch"),
    ]
    for name, body, status, reason in cases:
        r = await logged_in_client.post("/api/profile/cv", files={"file": (name, body)})
        assert r.status_code == status and r.json()["code"] == "upload_rejected", name
        assert r.json()["errors"][0]["code"] == reason
    assert await count_rows(engine, "cv_files") == 0 and list(cv_store_dir.iterdir()) == []
```

Also cover:
- a valid PDF gives 202, one `cv_files` row with an encrypted file whose decryption round-trips, a draft `uploaded`, and one pending `extract_profile_text` operation;
- an upload during `extracting_text` gives 409;
- an upload over a `ready` draft discards it in the same transaction, and also discards every older `failed` draft;
- the operation's payload is `{"draft_id": <id>}` with dedup key `extract_profile_text:<id>`;
- a native opt-in while `NATIVE_PDF_ENABLED` is false gives 422 `validation_failed` on `native_pdf_opt_in` and stores nothing; with it patched true, the `cv_files` row has `native_pdf_opt_in = true` and `extraction_method = 'native_pdf'`, and the operation is `extract_profile:<id>`;
- the paste route enforces its limits and stores no file;
- `test_streaming_limit_stops_reading`: a counting ASGI receive proves that at most `limit + one chunk` bytes are read;
- the route rejects a missing session (401) and a missing CSRF header (403).
- [ ] **Step 2: Run the tests and see them fail.**
- [ ] **Step 3: Implement** `crypto.data_keys`, the upload checks, the store, the operation helper, the draft opener and the routes. Regenerate `frontend/src/api/schema.d.ts` with `scripts/check-openapi-drift.sh`.
- [ ] **Step 4: Run the tests and see them pass**, plus `tests/unit/test_openapi_contract.py`.
- [ ] **Step 5: Commit** `feat(m2): CV upload and paste with streaming limits, ZIP checks, encrypted storage and the draft operation`.

### Task 10: Contact stripping (pure)

**Files:**
- Create: `backend/src/jobhunter/profile/contacts.py`
- Test: `tests/unit/test_contacts.py`, `tests/property/test_contacts_property.py`

**Interfaces:**
- Produces:
  - `contacts.Category = Literal["email", "phone", "url", "address", "personal", "name"]` and `contacts.REPLACEMENT: Final[Mapping[Category, str]]`, which maps to `[EMAIL]`, `[PHONE]`, `[URL]`, `[ADDRESS]`, `[PERSONAL]` and `[NAME]`.
  - `@dataclass(frozen=True) class contacts.Replacement: category: Category; start: int; end: int`, offsets into the **original** text.
  - `@dataclass(frozen=True) class contacts.StripResult: stripped_text: str; report: Mapping[Category, int]; replacements: tuple[Replacement, ...]`. `report` holds counts only.
  - `contacts.strip_contacts(text: str) -> StripResult`, applying the §12.3 table rule by rule, verbatim:
    - an RFC-5322-lite e-mail;
    - a phone: `+49`, `0049`, a leading `0` or another `+<cc>`, then ≥ 7 digits with space, `/`, `-`, `.` or `()` separators, also across one line break;
    - URLs: `https?://…`, `www.…`, `linkedin.com/in/…`, `xing.com/profile/…` and `github.com/<user>`;
    - the street suffix list followed by a house number, and the postal code plus city;
    - the date and place of birth labels with the rest of the line;
    - the protected-attribute labels with the rest of the line;
    - the header name: the first non-empty lines that are 2–4 capitalised tokens with no digit and none of `ROLE_KEYWORDS`. Later full-name occurrences are replaced case-insensitively.
  - `contacts.ROLE_KEYWORDS: Final[frozenset[str]]`: `lebenslauf`, `curriculum`, `vitae`, `resume`, `cv`, `profil`, `profile`, plus common DE/EN job-title words. Keep the list in one constant and document it.
  - Edits are deterministic functions of the original text, so a complete edit set can be re-applied from scratch (Task 14 rebuilds `stripped_text` from `strip_contacts(original)` on every edit request):
    - `contacts.restore(original: str, result: StripResult, index: int) -> StripResult` restores replacement `index` of `strip_contacts(original)`, for the user's false positive;
    - `contacts.remove_span(original: str, result: StripResult, start: int, end: int) -> StripResult` marks an extra span `[REMOVED]`, with `start` and `end` offsets into the **original** text, never into the stripped string.
  - `contacts.scan_value(value: str) -> list[Category]`, the same patterns without the name rule, used by Task 12.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_contacts.py (Review Focus 2 pin; every person is invented)
CV = (
    "Max Mustermann\nSenior Backend Engineer\n\n"
    "Erfahrung\n| Firma | Kontakt |\n| Beispiel GmbH | max.mustermann@example.org |\n"
    "Tel. +49 30\n1234 5678\nMusterstraße 12, 10115 Berlin\nGeburtsdatum: 01.02.1990\n"
    "Familienstand: ledig\n\nMit freundlichen Grüßen\nmax mustermann\n"
)
def test_contacts_in_footer_table_and_signature_are_stripped() -> None:
    r = strip_contacts(CV)
    for value in ("max.mustermann@example.org", "1234 5678", "Musterstraße 12", "10115 Berlin", "01.02.1990", "ledig", "Mustermann"):
        assert value.casefold() not in r.stripped_text.casefold(), value
    assert dict(r.report) == {"email": 1, "phone": 1, "address": 2, "personal": 2, "name": 2, "url": 0}
    assert "Senior Backend Engineer" in r.stripped_text            # a role line is not a name
```

Also cover:
- `restore` round-trips one replacement;
- a removal and a restore that do not overlap, applied to `strip_contacts(original)`, give the same `stripped_text` whichever is applied first, and re-applying the same edit set gives the same text;
- `scan_value` finds each category;
- the property test: `stripped_text` never contains any of the generated e-mail or phone values, and the stripping is idempotent (stripping twice changes nothing).
- [ ] **Step 2: Run the tests and see them fail.**
- [ ] **Step 3: Implement** the stripping and its helpers, with compiled, linear patterns and bounded quantifiers (the M1 F1 lesson).
- [ ] **Step 4: Run the tests and see them pass.**
- [ ] **Step 5: Commit** `feat(m2): contact stripping with per-category counts and restorable replacements`.

### Task 11: The operation runtime and the `profile_graph` text phase (`extract_profile_text`)

**Files:**
- Create: `backend/src/jobhunter/graphs/profile/{__init__,graph,state,deps}.py`, `graphs/profile/nodes/{__init__,parse,strip_contacts}.py`, `backend/src/jobhunter/profile/operations.py`
- Modify:
  - `backend/src/jobhunter/worker/operations.py` (`OperationContext` gains `deps: OperationDeps | None = None`, its outcome holder, `finish_in` and `requeue_in`; `execute_operation` gains `deps`; `OperationClaim.attempts`, read by `_CLAIMABLE`; `_STALE`, `_OLDER_EPOCH` and `_OWNED_ATTEMPTS` also read `kind, payload` for `_release`; `FAILURE_HOOKS`);
  - `worker/runtime.py` (`WorkerRuntime` gains `gateway`, `parse_client`, `cv_store`, `profile_graphs` and `operation_deps`);
  - `worker/main.py` (build them after `open_saver`, the `ParseClient` on `cfg.parse_jobs_dir`; `parse_client.cleanup_leftovers()` at start; `_REGISTERING_MODULES` += `jobhunter.profile.operations`);
  - `worker/tasks.py` (`run_operation` passes `deps=rt.operation_deps`);
  - `backend/src/jobhunter/config.py` (`AppConfig.parse_jobs_dir: Path` from `JOBHUNTER_PARSE_JOBS_DIR`, default `/data/parse`, so tests and spawned workers never touch `/data/parse`);
  - `tests/unit/test_worker_main.py`;
  - the M0/M1 tests that build or fake the operation runtime: `tests/unit/test_operation_wiring.py` (its fake `execute_operation(engine, operation_id, *, epoch, on_superseded)` takes `deps`; its `HANDLERS ==` pin is unchanged, since hooks live in `FAILURE_HOOKS`), `tests/integration/test_watchlist_validate.py` (`OperationContext(claim=…, engine=engine)` still builds with `deps` defaulted; its claims carry `attempts`), `tests/integration/test_fenced_admission_redefers.py` (`WorkerRuntime(...)` gains the new fields);
  - `tests/support/profile.py` (`op_runner`, `upload`, `draft_row`).
- Test: `tests/integration/test_profile_text_phase.py`, `tests/unit/test_profile_graph_shape.py`

**Interfaces:**
- Consumes:
  - `ParseClient` (Task 8), `CvStore`, `crypto.data_keys(cfg)` and the draft payload `{"draft_id": <id>}` (Task 9), and `strip_contacts` (Task 10);
  - `open_saver` and `DB_RETRY` (M1);
  - `execute_operation`, `register_operation_handler`, `lock_operation_owner`, `requeue_operation`, `finish_operation` (M0).
- Produces (this task is the definition; the File Structure row only summarises it):
  - `@dataclass(frozen=True) class worker.operations.OperationDeps: cfg: AppConfig; gateway: LLMGateway; parse_client: ParseClient; cv_store: CvStore; profile_graphs: ProfileGraphs; embedder: EmbedderClient | None; clock: Callable[[], datetime]`. `embedder` is `None` when the worker has none, as for `WorkerRuntime.embedder` and `CollectDeps.embedder`.
  - `@dataclass(frozen=True) class worker.operations.OperationContext: claim: OperationClaim; engine: AsyncEngine; deps: OperationDeps | None = None`, still frozen, with a private mutable outcome holder (`field(default_factory=…)`, not an init argument). The M1 handler `watchlist_validate`, and Task 5's test handler, which closes over its gateway, run with `deps=None`.
  - `OperationClaim.attempts: int`: `claim_operation` reads `attempts` in `_CLAIMABLE`, and the value is constant for the claim.
  - `execute_operation(engine, operation_id, *, epoch, deps: OperationDeps | None = None, registry: ExecutionRegistry = EXECUTIONS, heartbeat_period_s: float = 30.0, on_superseded=…)`. It builds the context with `deps`, and after the handler returns it writes nothing when the outcome holder is set. `tasks.run_operation` passes `deps=rt.operation_deps`; `WorkerRuntime.operation_deps` is built in `worker/main.py`.
  - Failure hooks (Deviation 13):
    - `type FailureHook = Callable[[AsyncConnection, int, Mapping[str, JSONValue], str | None], Awaitable[None]]`, called with the operation id, its payload and the last error code (the requeue's code, or `None` after a reclaim).
    - `register_operation_handler(kind, *, on_failed: FailureHook | None = None)` registers the hook in a separate `FAILURE_HOOKS: dict[OperationKind, FailureHook]`; `HANDLERS` keeps the handlers only.
    - `finish_operation(status='failed')` and `_release` (exhaustion by a requeue, the stale reclaim or the takeover sweep) call the hook right after the `failed` transition, in the same transaction. `_STALE`, `_OLDER_EPOCH` and `_OWNED_ATTEMPTS` also select `kind, payload`, and `_release` receives them, so it can find the hook and pass the payload.
    - A hook may lock only rows after the owner row in the §9.1 order.
    - **One owner of the failure transition.** A hook first reads its domain row `FOR UPDATE` and moves it only while it is still live: a draft in `uploaded` (text phase only), `extracting_text` or `extracting_profile`, a proposal in `running`. A row the handler's own write already ended in the same transaction (a node's `failed(code)` followed by `finish_in(status='failed')`) is left as it is. So the direct failure and the operation's end commit together, and `transition()` never meets a row that is already terminal.
    - The text phase's hook moves the draft `extracting_text → failed(<code or 'attempts_exhausted'>)`. A draft still `uploaded` (the operation failed before its first transition, for example by `no_handler` or ten `bug` requeues) takes the two §7.16.8 edges `uploaded → extracting_text → failed(code)` in the same transaction, with the same `failure_code`, so it never stays `uploaded` and blocks every later upload with 409. Task 13 adds the extract phase's hook, and Task 20 the proposal's.
  - `class graphs.profile.state.ProfileState(TypedDict)`: `draft_id: int`, `operation_id: int`, `llm_call_id: int | None`, `steps: list[str]` and `outcome: str | None`, ids only. The validated LLM output between nodes lives in `llm_calls.output_json`, and the state holds only its id.
  - **How a node reaches its execution.** A profile handler invokes its graph with `config={"configurable": {"thread_id": thread_id(…), "op": ctx}}`, the §9.1 invoke shape; `configurable` is never checkpointed. A node reads the claim (`ctx.claim`, for the owner lock and `OperationOwner`), `ctx.finish_in`, `ctx.requeue_in` and `ctx.deps` from `config["configurable"]["op"]`, never from the state.
  - `graphs.profile.graph.sanitized(name, node) -> Node` wraps every profile node (Deviation 25). LangGraph persists an exception that escapes a node into `checkpoint_writes`, and an SQLAlchemy or Pydantic error can carry bound values (CV text, profile fields) in its message.
    - `FencedOut`, `asyncio.CancelledError`, LangGraph's `GraphBubbleUp` and the M2 domain errors whose `str()` is a code only (`LLMError` and its subclasses, `UploadRejected`, `TransitionConflict`) pass through unchanged.
    - Any other exception is logged with its class only and replaced by `NodeFailed(node=name, error_class=type(exc).__name__)`. The replacement is raised **after** the `except` block, so neither `__cause__` nor `__context__` refers to the original.
  - `graphs.profile.graph.ProfileGraphs`, holding `text`, `extract` and `propose`, each a compiled graph.
  - `build_profile_graphs(checkpointer, deps: ProfileDeps) -> ProfileGraphs`, compiled once per process. `ProfileDeps` holds only process-wide objects (such as the engine and the clock); everything per execution, the claim and `OperationDeps` included, comes through `configurable["op"]`.
    - `text`: `parse → strip_contacts → END`;
    - `extract`: `check_pdf → extract_profile → validate → draft → END` (Task 13; `check_pdf` passes at once unless the draft is native PDF);
    - `propose`: `propose_queries → END` (Task 20).
    The non-LLM nodes take `DB_RETRY` and `TimeoutPolicy(run_timeout=…)` (`parse` 120 s, `strip_contacts` 30 s). The LLM nodes take no `RetryPolicy`, `TimeoutPolicy(run_timeout=1800 + 60)` (the §13.1 maximum `deadline_s` plus a margin, because a graph compiled once cannot take a deadline that an `execution` save changes at runtime; Deviation 28), and `TracePolicy(process_inputs=omit, process_outputs=omit)` (§9.1). The active `deadline_s` is enforced per call through `CallContext.deadline` (§10.2 `deadline = ctx.deadline`).
  - `graphs.profile.graph.thread_id(kind: Literal["t", "p", "q"], subject_id: int, operation_id: int, attempts: int) -> str` (Deviation 27), called with `claim.operation_id` and `claim.attempts`:
    - `profile-<draft_id>-t<operation_id>-a<attempts>` and `-p<operation_id>-a<attempts>`;
    - `queries-<proposal_id>-q<operation_id>-a<attempts>`.
    Every execution of every operation gets a thread of its own, so no state of an earlier operation or attempt leaks into a later one through omitted keys or reducers. Work done before a crash is found through the receipts (Task 13), not through the thread.
  - `OperationContext.finish_in(conn, *, status: Literal["succeeded", "failed"], error_code: str | None = None) -> None` finishes the claimed operation inside the caller's transaction, so a node's last domain write and the operation's end commit together. `OperationContext.requeue_in(conn, *, error_code: str, not_before: datetime) -> None` is the handler-side requeue, in the caller's transaction, under the same attempt limit and failure hook as `requeue_operation`. Both set the context's outcome holder, and `execute_operation` then writes nothing more for that execution: it reads the holder after the handler returns, so it never tries its own `_finish(succeeded)` over a requeue and never logs `operation_fenced` for it. Every M2 handler ends this way: the profile nodes' `ready`/`text_review`/`failed` writes, the proposal publication, the scope flip and `embed_profile` through `finish_in`, and every transient wait (Tasks 13, 18, 19, 20) through `requeue_in`.
  - The `parse` node:
    - skips when `cv_files.raw_text_enc` is set (receipt);
    - pasted text skips docparse;
    - otherwise it decrypts the file, calls `parse_client.extract`, and stores `raw_text_enc`, `extraction_method` and `page_count` under the owner lock.
    - A `ParseFailed(code)` moves the draft `extracting_text → failed(code)`. Text under 300 characters moves it to `failed('low_text')` (`LOW_TEXT_CHARS: Final = 300`).
  - The `strip_contacts` node writes `stripped_text` and `strip_report`, then `extracting_text → text_review`.
  - The `extract_profile_text` handler reads `payload["draft_id"]`, moves the draft `uploaded → extracting_text` under the owner lock, then invokes `profile_graphs.text` with `config={"configurable": {"thread_id": thread_id("t", draft_id, claim.operation_id, claim.attempts), "op": ctx}}`. The `strip_contacts` node's `text_review` write, and every `failed(code)` write of the text phase, finish the operation in the same transaction (`finish_in`). A draft that is no longer `uploaded` or `extracting_text` makes the operation succeed as obsolete.

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_profile_text_phase.py (Review Focus 1 pin)
async def test_scanned_pdf_ends_low_text_without_any_llm_call(engine, op_runner, upload) -> None:
    draft_id = await upload(image_only_pdf())                       # Task 9 route through the test client
    await op_runner.run_pending()                                   # runs extract_profile_text with an in-process docparse loop
    draft = await draft_row(engine, draft_id)
    assert (draft.status, draft.failure_code) == ("failed", "low_text")
    assert await count_rows(engine, "llm_calls") == 0 and await count_rows(engine, "llm_spend_ledger") == 0
```

(`op_runner`, `upload` and `draft_row` are in `tests/support/profile.py`, this task; `count_rows` is Task 9's. `op_runner.run_pending()` claims and executes pending operations with a real `OperationDeps`: the fake LLM, a `ParseClient` on a tmp root served by a `docparse.worker.serve(tmp_root, stop=…)` thread, and a tmp `CvStore`. `op_runner.run_one(op_id, *, pause: PausePoint | PausePoints | None = None, registry: ExecutionRegistry | None = None)` runs one operation through `execute_operation(…, deps=…, registry=…)`. A `registry` of `None` means a fresh `ExecutionRegistry` when a pause is given, so a paused execution and a second execution of the same operation never meet `AlreadyExecuting`, and `EXECUTIONS` otherwise. A pause replaces `afault_point` in every module that binds it and exists at that point of the plan: `jobhunter.llm.gateway`, `jobhunter.graphs.profile.nodes.*`, `jobhunter.settings.scope_apply` and `jobhunter.profile.activation`.)

Also cover:
- a two-page text PDF reaches `text_review` with `[EMAIL]` in `stripped_text` and the counts in `strip_report`;
- pasted text reaches `text_review` without docparse;
- `test_resume_skips_a_finished_parse`: a reclaim after `parse` committed reaches `text_review` with **one** docparse job (the loop counts jobs);
- an `encrypted` PDF gives `failed('encrypted')`;
- the checkpoint rows of the thread contain no CV text: scan `checkpoints`, `checkpoint_blobs` **and** `checkpoint_writes` for a marker word of the synthetic CV, including after a node raises an `sqlalchemy.exc.StatementError` whose parameters hold the marker (`test_node_errors_reach_no_checkpoint_write`);
- a text-phase operation exhausted by ten reclaims leaves the draft `failed('attempts_exhausted')` in the same transaction (the failure hook);
- `test_failure_before_the_first_transition_frees_the_upload_slot`: an `extract_profile_text` operation that fails while its draft is still `uploaded` (no handler registered, or ten `bug` requeues) leaves the draft `failed(code)` through `uploaded → extracting_text → failed`, and a new upload is then accepted with 202;
- a handler-side `ctx.requeue_in(...)` leaves the operation `pending` with its `not_before`, and `execute_operation` writes nothing after it (no `operation_fenced` log);
- `test_direct_failures_commit_with_the_operation_end`: `low_text` here, and in Tasks 13 and 20 `refused` and an exhausted repair, each commit the domain row's `failed(code)` and the operation's `failed` in one transaction, with no rollback and no requeue;
- `test_text_review_and_the_operation_end_commit_together`: a fault at `profile.text.after_text_review_commit`, right after the `text_review` write's commit point, finds the operation already `succeeded`;
- two successive `extract_profile_text` operations of one draft history use two threads, and neither sees the other's state.
- [ ] **Step 2: Run the tests and see them fail.** `uv run --group docparse pytest -W error tests/integration/test_profile_text_phase.py tests/unit/test_profile_graph_shape.py -q`
- [ ] **Step 3: Implement** `OperationDeps`, the context's outcome holder with `finish_in` and `requeue_in`, `execute_operation(…, deps=…)`, `OperationClaim.attempts`, `FAILURE_HOOKS` and the wiring, `AppConfig.parse_jobs_dir`, the graphs module (text phase), the two nodes and the handler. Add the fault point `profile.text.after_text_review_commit`.
- [ ] **Step 4: Run the tests and see them pass**, plus `tests/unit/test_worker_main.py`, `tests/unit/test_operation_wiring.py`, `tests/integration/test_watchlist_validate.py` and `tests/integration/test_fenced_admission_redefers.py`, all with `uv run --group docparse pytest -W error … -q`.
- [ ] **Step 5: Commit** `feat(m2): operation dependencies and the profile_graph text phase with docparse, low-text handling and contact stripping`.

### Task 12: Profile validation: semantic limits, dates, years, contacts, protected attributes and grounding (pure)

**Files:**
- Create: `backend/src/jobhunter/profile/validate.py`, `backend/src/jobhunter/profile/schema.py` (the limits table as constants)
- Test: `tests/unit/test_profile_validate.py`

**Interfaces:**
- Consumes: `schemas.profile.ProfileOut`/`ConfirmedProfile` (Task 6); `contacts.scan_value` (Task 10); `normalize.text.fold` (M1).
- Produces:
  - `schema.LIMITS`, the §12.4 semantic limits table verbatim as typed constants:
    - `SUMMARY_MAX = 1200` and `TARGET_TITLES = (1, 12)`;
    - `ROLES_MAX = 30`, `HIGHLIGHTS_MAX = 8` and `HIGHLIGHT_CHARS = 300`;
    - `SKILLS_MAX = 120` and `SKILL_YEARS = (0, 50)`;
    - `LANGUAGES_MAX = 10` and `CODE_OTHER_CHARS = 40`;
    - `EDUCATION_MAX = 10` and `EDUCATION_YEAR = (1950, None)`, where the upper bound is the current year;
    - `CERTIFICATIONS_MAX = 30`, `DOMAINS_MAX = 20` and `TOTAL_YEARS = (0, 60)`.
  - `@dataclass(frozen=True) class validate.Issue: path: str; code: Literal["limit", "date_invalid", "date_order", "years_mismatch", "contact_removed", "ungrounded", "language_without_evidence", "code_other", "unverifiable"]; message: str; proposal: JSONValue = None`.
  - `validate.validate_profile(out: ProfileOut, *, transmitted_text: str | None, today: date) -> tuple[ProfileOut, list[Issue]]`. It **never** raises; it returns the repaired profile and the issues:
    - Month dates (`YYYY-MM`) become `YYYY-MM-01`. An unparsable date becomes `null` with `date_invalid`, and `start > end` or a date after today gives `date_order`.
    - `total_years_experience` is recomputed from the roles' intervals with overlaps merged (`present` = today). A difference of more than 1 year gives `years_mismatch`, with the recomputed value as `proposal`.
    - Every string field is scanned with `contacts.scan_value`. A hit removes the value: a string becomes `""`, a list item is dropped. It gives `contact_removed` and never quotes the value.
    - Every `evidence` must be a substring of `fold(transmitted_text)` after `fold` and whitespace collapsing; otherwise `ungrounded`. With `transmitted_text=None` (native PDF) every evidence gives `unverifiable` (Deviation 8).
    - A language without evidence gives `language_without_evidence`.
    - `code_other` is required exactly when `code = other`, otherwise `code_other`.
    - The limits give `limit` issues; the excess is kept for the user to fix, never truncated silently.
  - `validate.validate_user_entered(u: UserEntered) -> list[Issue]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_profile_validate.py (Review Focus 2 pin)
def test_contact_value_in_profile_json_is_removed_with_an_issue() -> None:
    out = profile_out(summary="Backend engineer, reachable at max.mustermann@example.org",
                      highlights=["Led the team (Tel. +49 30 1234 5678)", "Built the billing service"])
    fixed, issues = validate_profile(out, transmitted_text=REVIEWED, today=date(2026, 10, 20))
    assert "example.org" not in fixed.summary and fixed.roles[0].highlights == ["Built the billing service"]
    assert {i.code for i in issues} >= {"contact_removed"} and all("example.org" not in i.message for i in issues)
```

Also cover:
- grounding through umlauts, case and line breaks (`"Größe"` vs `"GRÖSSE"` per `fold`);
- an overlapping-roles recomputation (2018-01 to 2021-12 and 2020-01 to present → about 8.8 years at the test date);
- `present` dates;
- every limit at its edge and one past it;
- `code_other` both ways;
- `unverifiable` for `transmitted_text=None`.
- [ ] **Step 2: Run the tests and see them fail.**
- [ ] **Step 3: Implement** `schema.py` and `validate.py`.
- [ ] **Step 4: Run the tests and see them pass.**
- [ ] **Step 5: Commit** `feat(m2): profile validation with repairs, grounding and contact removal`.

### Task 13: The `extract_profile` call, the extract phase of `profile_graph`, native PDF and crash case C35

**Files:**
- Create: `backend/src/jobhunter/graphs/profile/nodes/{check_pdf,extract_profile,validate,draft,llm_outcome}.py`, `tests/contract/fixtures/anthropic/extract_profile/claude-opus-5-5/<schema12>.sse` (hand-authored, synthetic; see Step 1)
- Modify: `backend/src/jobhunter/profile/operations.py` (handler `extract_profile`), `graphs/profile/graph.py`, `backend/src/jobhunter/llm/spend_ledger.py` (`receipts`, `Receipt`), `backend/src/jobhunter/llm/gateway.py` (the native-PDF request it counts and sends, `CallContext.native_pdf`), `tests/support/profile.py` (`SYNTHETIC_CV`), `tests/support/llm.py` (`synthetic_sse(body, *, model) -> bytes`, the fixture builder), `tests/unit/test_worker_main.py` (the `extract_profile` handler pin), `tests/unit/test_profile_graph_shape.py` (the `extract` graph)
- Test: `tests/integration/test_profile_extract_phase.py`, `tests/integration/test_c35_profile_extraction.py`

**Interfaces:**
- Consumes:
  - `LLMGateway.call` and `count_tokens`, `CallContext(logical_call_no=…, native_pdf=…, deadline=…)`, `errors.InputUnbounded` and the `error_code`s of completed calls (Task 5); `node_config`; `render.extract_profile_prompt` and its native-PDF variant; `validate_profile`; `bounds.native_pdf_bound` and `NATIVE_PDF_ENABLED`; `pricing.CONTEXT_WINDOW` (Task 2, through the gateway). The buffer for the native-PDF extra share is the current month row's `overrun_buffer_usd`, which `reserve(…, native_pdf_extra=True)` reads after its month lock (Task 3); the node carries no buffer value.
  - `OperationContext` from `config["configurable"]["op"]`, `finish_in`, `requeue_in`, `OperationClaim.attempts`, `thread_id` and the failure hooks (Task 11); the payload `{"draft_id": <id>}` (Task 9); `fake.scenario(*names)` and `fake.count_tokens` (Task 4).
- Produces:
  - `graphs/profile/nodes/llm_outcome.py` holds the two pieces both LLM nodes share, so Task 20's `propose_queries` node reuses them instead of copying them: the receipts walk (below, by `logical_call_no` over `spend_ledger.receipts`) and the LLM failure mapping (below, from the gateway's exceptions to a `failed(code)` or a `ctx.requeue_in`).
  - The `extract_profile` node:
    - **Receipts first** (§9.1; Deviation 26). `async def spend_ledger.receipts(conn, *, operation_id: int, purpose) -> dict[int, Receipt]` returns, per `logical_call_no`, the newest `completed` call of the operation: `Receipt(llm_call_id, output_json: JSONValue | None, error_code: str | None)`. It matches `input_refs->'owner'->>'kind' = 'operation'` and `(input_refs->'owner'->>'id')::bigint = :operation_id`: the operation id is the one `OperationOwner.durable()` writes, never a second key. The node adds `profile_draft_id`, `cv_file_id`, `reviewed_text_sha256` and `native_pdf` to `input_refs`, every hash as a hex string.
      - Logical call 1 with an `output_json` is used without any HTTP request.
      - Logical call 1 without one and `error_code = 'invalid_output'` (billed, structurally invalid) means the repair is due. Logical call 2 with an `output_json` is used; logical call 2 without one ends the draft `failed('invalid_output')`. The one structural repair is therefore never repeated or skipped across a reclaim.
      - A newest completed call with `error_code` `refused`, `model_mismatch`, `max_tokens` or `unexpected_stop_reason` (Task 5) ends the draft `failed(<that code>)` with no request.
      - Receipts belong to one operation. While it is live the draft stays in its generation: approval needs `text_review`, and discard needs `text_review`, `ready` or `failed`. Every way out of `extracting_profile` finishes the operation in the same transaction (`finish_in`), so a later generation never meets a live older operation.
    - It reads `reviewed_text` and checks `sha256(reviewed_text) == reviewed_text_sha256` before rendering; a mismatch is a `Bug`.
    - It renders, and records `transmitted_text_sha256 = sha256(the exact string inside <cv_text>)`, which must equal `reviewed_text_sha256` (§9.5).
    - It calls the gateway with `purpose='extract_profile'`, `purpose_class='nonscoring'`, the **active** model policy and execution config (non-score calls use the active ones, §13.3), `ctx.owner = OperationOwner(claim)` (the claim from `configurable["op"]`), `profile_draft_id`, `deadline = monotonic() + <the active execution config's deadline_s>` (enforced per call, Deviation 28) and `logical_call_no` 1, or 2 for the repair.
    - The error mapping, in `llm_outcome.py`:
      - `InvalidOutput("structural")` is retried **once** as logical call 2 (`CallContext(logical_call_no=2)`, the structural repair); a second one gives `failed('invalid_output')`. The gateway settles a structurally invalid response `billed`, with its call `completed`, `output_json = NULL` and `error_code = 'invalid_output'`, so the receipts can tell it apart.
      - `InvalidOutput("unexpected_stop_reason")` gives `failed('unexpected_stop_reason')` without a repair; only `"structural"` is repaired.
      - `Refused` gives `failed('refused')`.
      - `BudgetExhausted` gives `failed('budget_exhausted')`, with no HTTP request (the reservation is refused first).
      - `InputUnbounded` gives `failed('input_unbounded')`, with nothing reserved or sent.
      - `LLMRequestError(s)` gives `failed('llm_request_<s>')`, `ModelMismatch` gives `failed('model_mismatch')` and `MaxTokensExhausted` gives `failed('max_tokens')`.
      - `LLMTransient` and `DeadlineExhausted` call `ctx.requeue_in(conn, error_code='llm_transient', not_before=now + 5 min)` (Task 11). The operation's 10-attempt limit then fails it, and the kind's `on_failed` hook (Task 11) moves the draft `extracting_profile → failed('llm_transient')` in that transaction (`failed('attempts_exhausted')` after reclaims).
    - The `llm_calls.output_json` holds the parsed output, and the draft's `llm_call_id` is set.
  - The `validate` node reads the output from `llm_calls.output_json` by the state's `llm_call_id`, and runs `validate_profile` with `transmitted_text = reviewed_text`, or `None` for native PDF.
  - The `draft` node moves `extracting_profile → ready` with `draft_json` and `validation_issues`, and finishes the operation in the same transaction (`finish_in`). Its write is `transition(…, owner={"reviewed_text_sha256": <the value the call was made for>, "native_pdf": <the call's native_pdf>})`, on the `owner_cols` that Task 1 sets for `profile_drafts`, a defence that no reordering can defeat. Every `failed(code)` of the extract phase finishes the operation the same way.
  - The native-PDF path:
    - `NATIVE_PDF_ENABLED` must be true (V19). A native draft that meets a false flag (an upload or retry admitted before the flag changed) ends `failed('native_pdf_disabled')` defensively, with nothing sent.
    - The `check_pdf` node (`graphs/profile/nodes/check_pdf.py`) runs first, before any token count. It decrypts the file and calls `ParseClient.validate`. `encrypted` or `too_many_pages` ends the draft `failed(code)` with no outbound request of any kind (§12.1; Deviation 7). `ok` records `page_count`.
    - The node calls the gateway with `CallContext(native_pdf=True)`. The gateway counts tokens for the exact request and reserves `native_pdf_bound(model, count)` input tokens with `reserve(…, native_pdf_extra=True)`, which adds `native_pdf_extra_usd` of the locked month row's buffer (Tasks 3 and 5).
    - A count failure, or a count over the model's context window (`pricing.CONTEXT_WINDOW[model]`), raises `errors.InputUnbounded` in the gateway, which the node maps to `failed('input_unbounded')` with nothing sent.
  - The `extract_profile` handler reads `payload["draft_id"]` and requires the draft in `extracting_profile`, which only the API's approve or retry (Task 14) or the native opt-in upload (Task 9) sets, in the transaction that creates the operation. It then invokes `profile_graphs.extract` with `config={"configurable": {"thread_id": thread_id("p", draft_id, claim.operation_id, claim.attempts), "op": ctx}}`. A draft in any other state, `text_review` and `failed` included, finishes the operation `succeeded` as obsolete without touching the draft, so no extraction ever starts on text the user has not approved (§12.3).
  - The extract phase's failure hook (Task 11's mechanism) moves a draft still in `extracting_profile` to `failed(<code or 'attempts_exhausted'>)`.

- [ ] **Step 1: Write the failing tests.**
  - **The fixture.** The paid budget records no `extract_profile` call, so this task adds `SYNTHETIC_CV` (invented people only) to `tests/support/profile.py` and a hand-authored synthetic SSE stream at `tests/contract/fixtures/anthropic/extract_profile/claude-opus-5-5/<schema12>.sse`, under the `extract_profile` purpose and the Task 6 schema hash. `tests/support/llm.synthetic_sse(body, *, model)` builds it from a JSON profile body whose every `evidence` string is a substring of `SYNTHETIC_CV`, with the event shapes V13 recorded. The fixture is labelled synthetic (not a recording, so Task 6's bound check skips it) and passes `tests/contract/test_fixture_pii.py`.
  - `test_profile_extract_phase.py`, on the fake LLM with that synthetic stream:
    - a draft in `extracting_profile` with `reviewed_text` reaches `ready`, with a grounded profile and no issue except the ones the fixture plants;
    - the `llm_calls` row has `purpose='extract_profile'` and `max_tokens` 16,000, and the ledger row is non-scoring and `billed`;
    - `fake.scenario("parse_error", "ok")` gives one repair (logical call 2) and `ready`;
    - `fake.scenario("parse_error")` (repeated) gives `failed('invalid_output')`;
    - `refusal` gives `failed('refused')`;
    - a cap below the worst case gives `failed('budget_exhausted')` and `fake_llm.served == 0`;
    - `fake.scenario("overloaded_529")` (three 529 tries) requeues the operation through `requeue_in` with `not_before`;
    - native PDF with `fake.count_tokens(1800)` reserves `ceil(2250) + overhead` plus $0.50 (`0.25 ×` the $2.00 buffer of the month row);
    - `fake.count_tokens("error")` gives `failed('input_unbounded')` and `served == 0`;
    - a native draft with `NATIVE_PDF_ENABLED` patched false ends `failed('native_pdf_disabled')` with `served == 0`;
    - a native opt-in upload of an encrypted PDF, and one of a 21-page PDF, end `failed('encrypted')` and `failed('too_many_pages')` with zero requests to the fake transport, `count_tokens` included;
    - an `extract_profile` operation whose draft is in `text_review` or `failed` finishes `succeeded` as obsolete and leaves the draft as it is;
    - receipts: a crash after logical call 1 settled invalid, then a reclaim, makes exactly one more request (the repair); a crash after the repair settled valid makes none; a crash after a call settled `completed` with `error_code='refused'` ends the draft `failed('refused')` on resume with none;
    - `test_crash_after_settlement_reuses_the_paid_output`: a fault at `profile.extract.after_call`, after the call settles and before the `draft` node commits, then a reclaim. The second execution reaches `ready` with `fake_llm.served == 1` and one billed ledger row;
    - a retry after `failed('invalid_output')` is a new operation, so it makes a new call;
    - `test_old_extraction_cannot_publish_into_a_new_approval`: execution 1 of operation A pauses at `profile.draft.before_tx`, before its `draft` node's transaction (`op_runner.run_one(A, pause=…)`, which gives it a fresh registry, as in Task 5's C35); A is reclaimed, and execution 2 reaches `ready` and finishes A; the user edits and approves new text (operation B), and B reaches `ready`. Execution 1 then resumes: its `draft` write fails the owner predicate and writes nothing. The draft's `draft_json` comes from B's call, made with the new text.
  - C35, full: as Task 5's pin, but through the real `extract_profile` handler. Exactly one extraction is billed, the draft is `ready` once, and the first execution's late write updates no draft row (`TransitionConflict`/`FencedOut` swallowed by the obsolete-execution path).
- [ ] **Step 2: Run the tests and see them fail.** `uv run --group docparse pytest -W error tests/integration/test_profile_extract_phase.py tests/integration/test_c35_profile_extraction.py -q`
- [ ] **Step 3: Implement** `spend_ledger.receipts`, `llm_outcome.py`, the four nodes (`check_pdf`, `extract_profile`, `validate`, `draft`), the graph wiring, the handler and its failure hook, the synthetic fixture and its builder. Add the fault points `profile.extract.after_call` and `profile.draft.before_tx`. Update the handler pin in `tests/unit/test_worker_main.py` and the `extract` graph in `tests/unit/test_profile_graph_shape.py`.
- [ ] **Step 4: Run the tests and see them pass**, with `uv run --group docparse pytest -W error … -q`, plus `tests/unit/test_worker_main.py`, `tests/unit/test_profile_graph_shape.py` and `tests/contract/test_fixture_pii.py`.
- [ ] **Step 5: Commit** `feat(m2): extract_profile through the gateway with one structural repair, the native-PDF bound and crash case C35`.

### Task 14: The profile review API

**Files:**
- Modify: `backend/src/jobhunter/api/routes/profile.py`, `frontend/src/api/schema.d.ts`, `tests/support/profile.py` (`idem`, `failed_low_text_draft`, `draft_row_api`)
- Test: `tests/integration/test_profile_review_api.py`

**Interfaces:**
- Consumes: `run_idempotent`, `check_idempotency_key`, `validate_profile` (Task 12), `contacts.restore`/`remove_span`/`strip_contacts` (Task 10), `CvStore.decrypt_text`, `upload.request_draft_operation` (Task 9), `transition()`, `bounds.NATIVE_PDF_ENABLED` (Task 7).
- Produces the §14.2 routes, each documented with `SESSION_RESPONSES`, `CSRF_RESPONSES` and the problem codes it can return:
  - `GET /api/profile/drafts/{id}` → `DraftOut{status, draft, validation_issues, strip_report, stripped_text_preview, reviewed_text_sha256, failure_code, repairs}`.
    - `repairs` of a `failed` draft comes from the stored state, through the same predicates as the retry route below, never from the failure code alone (Deviation 3):
      - `"paste_text"` for every text-phase failure (a draft that failed in the text phase: `low_text`, `encrypted`, `too_many_pages`, `parse_error`, `parse_limit`, `timeout`, `unsupported`, or the text hook's `attempts_exhausted`), since §12.7 offers "paste text" after any failed extraction;
      - `"native_pdf"` exactly when `mode="native_pdf"` would be admitted: `low_text` or an LLM failure code, a PDF file not purged, and `bounds.NATIVE_PDF_ENABLED` (the pin below runs with it patched true). `encrypted` and `too_many_pages` fail §12.1's PDF checks, which the native path must pass too, so they never offer it;
      - `"retry_llm"` exactly when `mode="llm"` would be admitted: `reviewed_text IS NOT NULL AND content_purged_at IS NULL`, or `native_pdf` with an unpurged PDF file;
      - `[]` otherwise. Including `repairs` goes beyond the spec's summary and is documented.
    - `stripped_text_preview` is the stripped text, never the raw text.
  - `PUT /api/profile/drafts/{id}` with `{draft: ConfirmedProfile}`, allowed in `ready` only: it revalidates against `reviewed_text` (for native PDF, against nothing), stores `draft_json` and `validation_issues` with a plain `UPDATE`, and returns `{validation_issues}`.
  - `PUT /api/profile/drafts/{id}/text` with `{span_edits: [{op: "remove" | "restore", start?, end?, replacement_index?}]}`, the **complete** edit set of the draft, never a delta (Deviation 29):
    - `replacement_index` names a replacement of `strip_contacts(raw)`, and `start`/`end` of a removal are offsets into the original (raw) text;
    - the server decrypts `raw_text_enc`, runs `strip_contacts` on it, and applies the set with `restore` and `remove_span` to that rebuild, deterministically; the result replaces `stripped_text`. So edits compose across requests: a second request that keeps an earlier removal and adds a restore gives the same text as one request with both;
    - in `text_review` it applies the set;
    - in `ready` it first moves `ready → text_review` and sets `draft_json := NULL` (§12.3);
    - it returns `{stripped_text_preview}`.
  - `POST /api/profile/drafts/{id}/text-review` (Idempotency-Key): in `text_review` only, it stores `reviewed_text` (the current stripped text, edits applied) and its sha256, moves `text_review → extracting_profile`, and inserts the operation through `upload.request_draft_operation(conn, "extract_profile", draft_id, defer=…)` (payload `{"draft_id": <id>}`, dedup key `extract_profile:<id>`). It returns 202 `{status: "extracting_profile"}`.
  - `POST /api/profile/drafts/{id}/retry` (Idempotency-Key) with `{mode: "llm" | "native_pdf", ack_native_pdf?: bool}`, from `failed` only:
    - `native_pdf` requires `ack_native_pdf: true` (else 422 `validation_failed`), `bounds.NATIVE_PDF_ENABLED` (else 409 `conflict_state`, Deviation 9), a PDF file, and a draft failed with `low_text` or an LLM failure code (else 409 `conflict_state`);
    - it sets `cv_files.native_pdf_opt_in := true`, `extraction_method := 'native_pdf'` and the draft's `native_pdf := true`.
    - The reopening transition (`failed → extracting_profile`) also sets `content_purged_at := NULL` when it is set. A native retry is admitted after the content purge as long as the file remains, and it generates new content, which must then purge again 30 days after the draft next ends.
    - **Input still there** (§20.1 purges a failed draft's file and content 30 days after it failed). The route takes the draft `FOR UPDATE`, then its `cv_files` row `FOR SHARE`, the order the retention rules use. `mode="llm"` requires `reviewed_text IS NOT NULL AND content_purged_at IS NULL`, or `native_pdf` with a PDF file not purged; otherwise 409 `conflict_state`. A text-phase failure has no `reviewed_text`, so it is refused with 409 here instead of reaching the §7.4 CHECK and a 500; its repair is "paste text". `mode="native_pdf"` needs the file. Without its input, the route gives 409 `conflict_state` (for purged input with the detail "The CV was deleted after 30 days; upload or paste it again"), and nothing changes: no transition and no operation.
    - The operation is inserted through `upload.request_draft_operation(conn, "extract_profile", draft_id, defer=…)`.
    - It returns 202.
  - `POST /api/profile/drafts/{id}/discard` → 204, from `text_review`, `ready` or `failed`.
  - `GET /api/profile` → `{active: ProfileVersionOut | null, versions: [...]}`, empty until Task 21's confirm.
  - A wrong state is 409 `conflict_state`, and an unknown id is 404 `not_found`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_profile_review_api.py (Review Focus 1 pin)
async def test_native_pdf_retry_requires_the_disclosure_ack(logged_in_client, failed_low_text_draft) -> None:
    did = failed_low_text_draft
    body = (await logged_in_client.get(f"/api/profile/drafts/{did}")).json()
    assert body["status"] == "failed" and body["failure_code"] == "low_text" and body["repairs"] == ["paste_text", "native_pdf"]
    r = await logged_in_client.post(f"/api/profile/drafts/{did}/retry", json={"mode": "native_pdf"}, headers=idem("k" * 16))
    assert r.status_code == 422
    r = await logged_in_client.post(f"/api/profile/drafts/{did}/retry", json={"mode": "native_pdf", "ack_native_pdf": True}, headers=idem("k2" * 8))
    assert r.status_code == 202 and (await draft_row_api(logged_in_client, did))["status"] == "extracting_profile"
```

(`idem(key) -> dict[str, str]`, the `Idempotency-Key` header, is a new helper in `tests/support/profile.py`; later tasks import it.)

Also cover:
- the text edits remove and restore;
- the edit set composes across requests: a removal in one `PUT`, then a `PUT` with that removal and a restore, gives the text of one `PUT` with both, and the restored value is back;
- `ready → text` editing discards `draft_json`;
- `repairs`: `["paste_text"]` for `parse_error` and for `encrypted`; `["retry_llm"]` for a pasted draft failed `refused` with its `reviewed_text`, and `[]` once its content is purged; no `"native_pdf"` while `NATIVE_PDF_ENABLED` is false;
- `mode="llm"` on a text-phase failure (no `reviewed_text`) gives 409 `conflict_state`, not 500, and creates no operation;
- `mode="native_pdf"` while `NATIVE_PDF_ENABLED` is false gives 409 `conflict_state`;
- a retry of a draft whose content or file was purged gives 409 `conflict_state`, and creates no operation;
- a native retry of a draft whose content was purged but whose file remains reopens it with `content_purged_at` NULL;
- approve stores the sha256 and creates exactly one operation, and a repeat with the same key returns the stored 202;
- `PUT` of the draft revalidates;
- discard;
- a wrong-state 409;
- `stripped_text_preview` never contains a stripped value.
- [ ] **Step 2: Run the tests and see them fail.**
- [ ] **Step 3: Implement** the routes. Regenerate `schema.d.ts`.
- [ ] **Step 4: Run the tests and see them pass**, plus the OpenAPI contract test.
- [ ] **Step 5: Commit** `feat(m2): profile review routes: draft status, field and text edits, approve, retry, discard`.

### Task 15: The Pydantic settings model, field and cross-field validation, the derived rows, the anchor DST rule, and the replacement of `load_run_settings`

**Files:**
- Create: `backend/src/jobhunter/settings/{__init__,model,validate,derive}.py`
- Modify:
  - `backend/src/jobhunter/run_settings.py` (`load_run_settings` and `scope_from_payload` build from `settings.model.Settings`, M1 Carry-forward 1; the frozen dataclasses stay as the collection view);
  - `backend/src/jobhunter/db/seed.py` (promote `_ensure_hashed`/`_ensure_id` to public `ensure_hashed`/`ensure_id`; `local_anchor_to_utc` DST rule, M0 Carry-forward 1);
  - `tests/unit/test_seed_math.py`
- Test: `tests/unit/test_settings_model.py`, `tests/unit/test_settings_validate.py`, `tests/unit/test_anchor_dst.py`, `tests/unit/test_seniority_derivation.py`, `tests/integration/test_settings_derive.py`, plus the existing `run_settings` tests unchanged

**Interfaces:**
- Consumes: `settings_v1.json`, `SETTINGS_SCHEMA_VERSION`, `seed.scope_payload`, `content_hash`, `pricing.SUPPORTED_EFFORTS`/`bound_input_price`, `bounds.PROMPT_OVERHEAD_TOKENS`, `DEFAULT_PROJECTED_CRITIC_RATE`.
- Produces:
  - `model.Settings`, a Pydantic v2 model with `extra="forbid"`. It mirrors §13.1 group by group, with every type, range and default verbatim, and decimals as strings in JSON. Its sub-models are `Schedule`, `Display`, `Scope` (with `ScopeLocation`, `SeniorityRange`, `KnockoutSeverity`), `Evaluation`, `Sources`, `QueryEntry`, `WatchlistEntry`, `Collection`, `Quota`, `Scoring` (with `ModelPolicy`, `NodeChoice`, `ExecutionNode`), `Llm`, `Retention` and `Notifications`. `Settings.model_validate(load_settings_v1())` succeeds, and `Settings.to_payload()` reproduces `settings_v1.json` as canonical JSON exactly.
  - `validate.FieldIssue(field: str, code: str, message: str)`. Its paths look like `payload.scope.locations[0].radius_km`, and its codes are `out_of_range`, `invalid`, `required`, `conflict` and `unsupported`.
  - `validate.validate_fields(payload: JSONValue) -> tuple[Settings | None, list[FieldIssue]]`. It wraps the Pydantic errors into `FieldIssue`s, then applies:
    - the cross-field rules of §13.1;
    - model and effort support (G5);
    - the execution ranges;
    - the overrun-buffer bound of §6.9.1, `validate.overrun_buffer_bound(settings) -> Decimal`: `max_concurrent_llm_calls × PROMPT_OVERHEAD_TOKENS × the highest input price per token`, summed per slot:
      - the 3 score and 2 auxiliary online slots, each at `max over the policy's configured models of PROMPT_OVERHEAD_TOKENS[m] × bound_input_price(m, batch=False)`;
      - plus `batch_max_items` batch slots at `PROMPT_OVERHEAD_TOKENS[g] × bound_input_price(g, batch=True)` of the generator model `g` (batch items are generator items, §10.10);
      - all divided by 1e6 and rounded up to the cent.
      With the defaults (Sonnet generator, Opus elsewhere, 1,000 items, the floor overhead) that is 5 × 1,000 × $5.00 + 1,000 × 1,000 × $1.25, over 1e6, which is $1.28 and matches §6.9.1's "about $1.28". The $2.00 default holds;
    - the quota upper bounds (M1 Carry-forward 9, already in the §13.1 ranges);
    - generator effort `low`: an error unless the eval gate passes or is uncalibrated. `validate_fields` takes `gate: GateState = "uncalibrated"`, with `GateState = Literal["uncalibrated", "passed", "failed"]`. `async def validate.eval_gate_state(conn) -> GateState` returns `"uncalibrated"` while fewer than 30 golden cases exist (§11.13), which before M3's `golden_cases` table is always, without a query. So `low` is a warning `eval_uncalibrated` in M2, which `warnings_only` returns: `validate_fields` returns `FieldIssue`s only.
  - `validate.warnings_only(settings: Settings, *, gate: GateState) -> list[quota.dryrun.Warning]`: the field-level warnings, `eval_uncalibrated` (generator effort `low` while `gate` is `"uncalibrated"`) and `serpapi_disabled_no_fallback`. Task 16's `dry_run` calls it right after a clean `validate_fields`.
  - `@dataclass(frozen=True) class derive.DerivedPayloads: scope: dict; model_policy: dict; execution: dict; evaluation: dict`, and `derive.derived(settings: Settings, *, profile: ConfirmedProfile | None = None) -> DerivedPayloads`. The scope payload is `seed.scope_payload` (without `max_ad_age_days`, as M0 stores it). With a profile, a `profile_relative` seniority range is resolved into `{mode: "explicit", min, max}`; without one it is kept as is (Deviation 14).
  - `derive.derived_seniority(profile_seniority: str, *, below: int, above: int) -> tuple[str, str]` gives that explicit `(min, max)` on `normalize.titles.SENIORITY_LADDER` (the §12.4 `seniority` enum in ladder order): the profile's level minus `below` and plus `above`, clamped to the ladder.
  - `async def derive.ensure_derived(conn, d: DerivedPayloads) -> DerivedIds(scope_version_id, model_policy_id, execution_config_id, evaluation_policy_id)`. It deduplicates by content hash. A new evaluation policy takes `version_no = max + 1` and `projected_critic_rate = DEFAULT_PROJECTED_CRITIC_RATE` (0.450, the §21.2 central estimate; §11.7.4's replay needs M3's drafts).
  - `seed.local_anchor_to_utc(day, hhmm, tz)`, M0 Carry-forward 1: a time in the spring-forward gap maps to the first valid instant after the gap, and a time in the autumn fold maps to its **first** occurrence (`fold=0`).
  - `run_settings.load_run_settings(conn, settings_version_id) -> RunSettings`, now `Settings.model_validate(row.payload)` and then `RunSettings.from_settings(...)`, with an unchanged signature and result.

- [ ] **Step 1: Write the failing tests.**
  - `test_settings_model.py`: the v1 defaults round-trip byte-equal, `extra` keys are rejected, and `validate_fields(load_settings_v1())` returns no issue under the constants in `bounds.py` (`test_seed_defaults_validate`; it must stay green after V19).
  - `test_settings_validate.py` is one parametrised table covering:
    - every §13.1 range edge and one past it, with the exact `FieldIssue` path and code;
    - each cross-field rule: no location without remote, include ∩ exclude, `min > max`, `t3_min_mid ≤ relevance_threshold`, and every source disabled;
    - an unsupported model or effort;
    - an execution `max_tokens_retry < max_tokens`;
    - an overrun buffer below the bound at `batch_max_items = 10000` ($12.53 at the defaults), and exactly $1.28 accepted at the defaults;
    - `radius_km = 0`.
  - `test_anchor_dst.py`: 02:30 on 2026-03-29 in Europe/Berlin → 01:00 UTC; 02:30 on 2026-10-25 → 00:30 UTC (the first occurrence).
  - `test_seniority_derivation.py`: `mid` ±1 gives `(junior, senior)`; the range clamps at both ends of the ladder; `intern` with `below = 3` gives `intern` as the minimum.
  - `test_settings_derive.py`: an identical payload reuses every id, a changed critic threshold creates exactly one new evaluation policy, reverting reuses the old row, and the same `profile_relative` settings derive two different scope rows for a `junior` and a `senior` profile.
- [ ] **Step 2: Run the tests and see them fail.**
- [ ] **Step 3: Implement** the model, the validation, the derived rows, the DST rule and the `load_run_settings` replacement. Keep `ScopeSettings`/`RunSettings` field-for-field, because the collection pipeline consumes them.
- [ ] **Step 4: Run the tests and see them pass, plus the whole M1 suite** (`uv run pytest -W error tests/unit tests/integration -q -k "plan or run_settings or filters or dryrun"`), since the collection view must not change.
- [ ] **Step 5: Commit** `feat(m2): Pydantic settings model with field and cross-field validation, derived immutable rows and the anchor DST rule; run settings built from it`.

### Task 16: The settings dry-run (§6.8, §13.5), the LLM projection and `GET /api/system/spend`

**Files:**
- Create: `backend/src/jobhunter/settings/{effects,dryrun,llm_projection}.py`, `backend/src/jobhunter/api/routes/{settings,spend}.py` (this task: the dry-run route and the spend route)
- Modify: `backend/src/jobhunter/api/app.py`, `backend/src/jobhunter/observability/health.py` (the `llm` section), `frontend/src/api/schema.d.ts`
- Test: `tests/unit/test_classify_effects.py`, `tests/unit/test_llm_projection.py`, `tests/integration/test_settings_dry_run.py`, `tests/integration/test_spend_route.py`

**Interfaces:**
- Consumes:
  - `quota.dryrun.quota_projections` and `Warning`, `windows.current_snapshot` and `ledger.used_units` (M1);
  - `quota.planner.location_keys` (moving to `quota/cells.py` in Task 23);
  - `validate_fields`, `warnings_only`, `eval_gate_state` and `Settings` (Task 15);
  - `budget.*` and `spend_ledger.read_month(conn, month, p, *, now)` (Task 3), the read-only month read: a plain `SELECT` with the accrual done in memory. This task never calls `lock_month`.
- Produces:
  - `effects.EffectClass = Literal["none", "schedule", "collection", "admission", "budget", "scope", "execution", "evaluation_policy", "llm_tuple"]`.
  - `effects.classify_effects(active: Settings, proposed: Settings) -> frozenset[EffectClass]`. It follows the §13.1 effect column field by field: the `scope.*` fields give `scope`, and `locations` and `remote_within_germany` also give `collection`. An unchanged payload gives `{"none"}`.
  - `llm_projection.central_cost_per_target(model_policy: ModelPolicy) -> Decimal`. It is the §21.1/§21.2 formula: the token assumptions × `pricing`, under the default critic funnel, with the 0.45 first-round rate, the 35% light share, `q1 = 0.35`, `q2 = 0.30` and the 1% repairs. Preset B gives $0.0526 ± $0.0001; A and C match §21.2 within $0.0001.
  - `@dataclass(frozen=True) class llm_projection.LlmProjection: bucket_rate_per_h: Decimal; bucket_capacity: Decimal; c_hat: Decimal; projected_critic_rate: Decimal; targets_per_month: int; arrival_per_month: int; projected_month_spend: Decimal`.
  - `llm_projection.llm_projection(settings: Settings, *, now: datetime, c_hat: Decimal, projected_critic_rate: Decimal, arrival_per_month: int) -> LlmProjection`. It uses `targets_per_month = floor(scoring_share / (c_hat + 0.04923 / 10))` (§21.3), and `arrival_per_month = 3000` when there is no target history, which is always the case before M3.
  - `async def dryrun.count_retained_current_revisions(conn) -> int`: unmerged jobs with a current revision (tombstones are M5's). `async def dryrun.count_published_results_active_tuple(conn) -> int` returns 0 before M3, without a query.
  - `@dataclass(frozen=True) class dryrun.DryRunResult: errors: list[FieldIssue]; warnings: list[Warning]; projections: dict[str, JSONValue]; effects: frozenset[EffectClass]`.
  - `async def dryrun.dry_run(conn, payload: JSONValue, *, active: Settings, now: datetime) -> DryRunResult` is the §6.8 pseudo-code composed in its order:
    - `validate_fields`, and on errors it returns the errors only;
    - right after a clean `validate_fields`, `warnings_only(settings, gate=await eval_gate_state(conn))`, whose warnings join the result;
    - `paid_cells`, the enabled queries × (locations + `remote-de`);
    - `quota_projections`, with `RunSettings` built from the proposed settings;
    - the LLM projection and `llm_cap_binding`, on the month figures of `read_month`;
    - the effect-dependent items:
      - `scope` gives `det_recompute = count_published_results_active_tuple()`, which returns 0 before M3 and is documented;
      - `llm_tuple` gives the **warning** `W("llm_rescore", …)`, as §6.8 appends it, with `targets = count_retained_current_revisions()`, `est_usd = targets × c_hat` and `bulk_budget_usd = scoring.bulk_rescore_max_budget_share × scoring headroom` (the same share as Task 21's confirm preview; no literal 0.4);
      - `evaluation_policy` gives `critic_rate`;
      - `schedule` gives `next_slot`.
    It runs in a read-only transaction and writes nothing.
  - `POST /api/settings/dry-run` takes `{payload}` and returns `{errors, warnings, projections, effects}`. Errors are returned with 200 here; they are the preview. The save route answers 422.
  - `GET /api/system/spend` returns `{month, counted, billed, ambiguous, reserved, cap, headroom, bucket: {usd, capacity, rate_per_h}, c_hat, critic_rate_7d: null, projected_critic_rate, bulk_operation: null, quarantined_batch_usd: "0", by_model_effort_role}`, read without locking: the month row through `spend_ledger.read_month`, the totals from the ledger.
  - `observability.health.SystemHealthOut` gains `llm: LlmSpendOut{month, counted, cap, headroom, bucket_usd}` (§17.5 "LLM spend", M0 Deviation 6), from the same `read_month`; a month without a row (`None`) reports the settings' cap and zero spend.

- [ ] **Step 1: Write the failing tests.**
  - `test_classify_effects.py`: one row per §13.1 field group gives its classes; several changes give their union; no change gives `{"none"}`.
  - `test_llm_projection.py`: `central_cost_per_target` for presets A, B and C; `targets_per_month` for B ≈ 1,620 (§21.3); the bucket rate and capacity.
  - `test_settings_dry_run.py`:
    - the defaults with one location and 4 queries return the expected warning codes and the projection keys `jsearch`, `adzuna`, `arbeitnow`, `serpapi_fallback` and `llm`;
    - each warning appears exactly when its condition holds (paid run cap binding, refresh slow, coverage gap, Adzuna rotation, Arbeitnow risk, stale TTL short, LLM cap binding, no fallback from `warnings_only`, `eval_uncalibrated` for a generator effort `low`, and `llm_rescore` for an `llm_tuple` change, with its `bulk_budget_usd` from `scoring.bulk_rescore_max_budget_share`);
    - the dry-run takes no row lock on `llm_budget` (`read_month`);
    - invalid input returns the errors with their paths;
    - `test_dry_run_writes_nothing` (§18.2 N4): row counts of every table are unchanged, and a `quota_windows` table with no row stays empty.
  - `test_spend_route.py`: after a billed and an ambiguous call, the totals and the headroom match the ledger, and `/api/system/health` shows the same `llm` figures.
- [ ] **Step 2: Run the tests and see them fail.**
- [ ] **Step 3: Implement** the effects classifier, the projection, the dry-run and the two routes. Regenerate `schema.d.ts`.
- [ ] **Step 4: Run the tests and see them pass.**
- [ ] **Step 5: Commit** `feat(m2): settings dry-run with quota and LLM projections and effect classes; the spend summary route`.

### Task 17: Settings save, the effect-class actions and the settings read routes

**Files:**
- Create: `backend/src/jobhunter/settings/save.py`, `backend/src/jobhunter/settings/scope_apply.py` (this task: `KIND`, `dedup_key`, `request_apply` and `set_desired_scope` only; Task 18 adds the handler), `backend/src/jobhunter/api/pagination.py` *(new; Deviation 3)*, `tests/support/settings.py` (`warning_codes`, `save_scope`)
- Modify:
  - `backend/src/jobhunter/api/routes/settings.py` (`POST /api/settings`, `GET /api/settings`, `GET /api/settings/versions`, `GET /api/settings/versions/{id}`);
  - `backend/src/jobhunter/settings/effects.py` (`diff_payloads`);
  - `backend/src/jobhunter/worker/runs.py` (`reconcile_stale_data`, M0 Carry-forward 2);
  - `frontend/src/api/schema.d.ts`;
  - `tests/support/faults.py` (`raising_at(point)`, which raises `SimulatedCrash` at that point, `no_fault`, and `SimulatedCrash`; Task 21 reuses them);
  - `tests/conftest.py` (`pytest_plugins` += `tests.support.settings`, after the modules it imports, per the file's ordering comment)
- Test: `tests/integration/test_settings_save.py`, `tests/integration/test_settings_routes.py`, `tests/unit/test_pagination.py`, `tests/integration/test_stale_data_disabled_schedule.py`

**Interfaces:**
- Consumes:
  - `dry_run`, `classify_effects` (Task 16) and `validate_fields`, `derived`, `ensure_derived`, `Settings` (Task 15);
  - `spend_ledger.ensure_month`/`change_cap` and `budget.month_of`/`BudgetParams.from_payload` (Task 3);
  - `seed.anchor_utc_for`, `seed.latest_slot_at_or_before`, `run_settings.load_run_settings`;
  - `sources.instances.instance_for_company`/`list_instances` and `quota.planner.ensure_cells` (M1);
  - `db.ranking.bump`, `run_idempotent`, `check_idempotency_key`, `request_fingerprint`;
  - `create_operation` (M0), for `request_apply`;
  - `crypto.hkdf_sha256`.
- Produces:
  - The first part of `settings/scope_apply.py`, so that the `scope` class action below is importable now. Task 18 adds the handler, `incomplete_jobs`, the reconciler and the route, and registers the module; until then an `apply_scope` operation waits `pending` without a handler.
    - `scope_apply.KIND: Final = "apply_scope"` and `scope_apply.dedup_key(scope_version_id: int) -> str` (`"apply_scope:<id>"`).
    - `async def scope_apply.request_apply(conn, scope_version_id: int, *, defer: Deferrer) -> tuple[int, bool]`, the `create_operation` wrapper with payload `{"scope_version_id": id}`.
    - `async def scope_apply.set_desired_scope(conn, desired_id: int, *, defer: Deferrer) -> int | None`, the one place that moves the pending pointer towards a desired scope. The caller holds `active_config FOR UPDATE`. It returns the `apply_scope` operation id, or `None` when no apply is needed:
      - `desired = active`: `pending := NULL`;
      - `desired = pending`: no change, and it returns the live operation's id;
      - otherwise: `pending := desired` and `request_apply`.
      The settings save (this task), the profile confirm (Task 21) and retry-apply (Task 18) use it.
  - `async def save.ensure_tuple(conn, *, profile_version_id: int, model_policy_id: int) -> int`: selects or inserts the `llm_tuples` row `(profile_version_id, active rubric, active prompt-schema, model_policy_id)` and returns its id. It is the one tuple select-or-insert; Task 21's activation reuses it.
  - `@dataclass(frozen=True) class save.SaveRequest: payload: JSONValue; base_version_id: int; acknowledged_warnings: frozenset[str]; eval_override_ack: bool = False`.
  - `@dataclass(frozen=True) class save.SaveOutcome: version_id: int; version_no: int; effects: frozenset[EffectClass]; apply_scope_operation_id: int | None`.
  - `async def save.save_settings(conn, req: SaveRequest, *, now: datetime, defer: Deferrer, internal: bool = False) -> SaveOutcome`. It runs in the caller's transaction, in this order:
    1. `SELECT … FROM active_config WHERE id = 1 FOR UPDATE` (the first lock, §9.1). A caller that already holds it (the save route, `edit_query`, the watchlist routes) makes this a no-op.
    2. `req.base_version_id ≠ active_config.settings_version_id` raises `Problem(409, "stale_base_version")`.
    3. `dry_run(conn, req.payload, active=…, now=now)` again (§13.2). Errors raise `Problem(422, "validation_failed", errors=[FieldError(issue.field, issue.code, issue.message)])`. Warning codes not in `acknowledged_warnings` raise `Problem(422, "validation_failed", errors=[FieldError("acknowledged_warnings", "warning_not_acknowledged", <code>)])`, one per code. With `internal=True` (Deviation 15) the acknowledgement check is skipped and the active version's `acknowledged_warnings` are copied.
    4. `ensure_derived(conn, derived(proposed, profile=<active profile or None>))` (Deviation 14).
    5. Inserts the `settings_versions` row: `version_no = max + 1`, `schema_version = SETTINGS_SCHEMA_VERSION`, the derived ids, `base_version_id`, `effect_classes` sorted, `acknowledged_warnings` sorted, `eval_override_ack`.
    6. Applies the effect classes in the §13.3 order, `llm_tuple`, then `scope`, then the others, through one private function per class:
       - **`llm_tuple`**: `eval_gate_state(conn)` is `"uncalibrated"` in M2 (fewer than 30 golden cases), so the gate is skipped (§11.13). `model_policy_id := new`. When `profile_version_id` is set, `ensure_tuple(conn, profile_version_id=…, model_policy_id=new)` gives the tuple and `llm_tuple_id` flips; the ranking bump is not done here (below). §12.6 (d)–(f) and `create_targets_for_tuple` are M3's (Deviation 10).
       - **`scope`**: `scope_apply.set_desired_scope(conn, derived_scope_id, defer=defer)` (this task's `scope_apply.py`). It clears the pending pointer when the desired scope is already the active one (a revert; the CHECK forbids pending = active), keeps it when it already names the desired scope, and otherwise points it at the desired scope and requests its apply. A pending apply of another scope is left to end obsolete at its own final check (§13.4 step 3).
       - **`schedule`**: `schedule_state` is reset in one `UPDATE`: `anchor_utc := anchor_utc_for(now, hhmm, tz)`, `interval_s`, `enabled`, `last_evaluated_slot := latest_slot_at_or_before(anchor, interval_s, now)` (its signature is `(anchor, interval_s, t)`, `db/seed.py`), `settings_version_id := new` (§3.4). The dead-man period is M5's.
       - **`collection`**: `instance_for_company` for every `watchlist[]` entry of the new payload (its key is `watchlist_company_id`, §13.1, as `run_settings` reads it), then `ensure_cells(conn, await load_run_settings(conn, new_id), await list_instances(conn))`. New cells start never-covered. This runs after `schedule`, because `load_run_settings` joins `schedule_state`.
       - **`admission`**: the flip only. The `max_ad_age_days` expiry sweep needs targets (M3, Deviation 10).
       - **`budget`**: `ensure_month(conn, month_of(now), BudgetParams.from_payload(<new payload>), now)` then `change_cap(conn, month=month_of(now), cap_usd=…, overrun_buffer_usd=…, now=now)` with the new values, under the month-row lock after `active_config` (lock order).
       - **`execution`**: `execution_config_id := new`.
       - **`evaluation_policy`**: the new row from `ensure_derived` (gate skipped as above), then `evaluation_policy_id := new`.
       After every class action and just before step 7, `ranking.bump` runs once when the `llm_tuple` action flipped `llm_tuple_id`. `ranking_state` is the last lock of §3.3.3, so the `budget` action's `llm_budget` lock always comes before it. §13.3's class order is unchanged, and the bump stays in the save's transaction.
    7. Sets `active_config.settings_version_id := new` and `updated_at` in the same statement as the pointer flips of step 6, where it can.
  - `effects.diff_payloads(before: JSONValue, after: JSONValue) -> list[DiffEntry]`, where `DiffEntry(path: str, before: JSONValue, after: JSONValue)` uses the `FieldIssue` path syntax. Lists of objects with an id key (`query_id`, `watchlist_company_id`, `gazetteer_id`) are matched by that key; other lists compare by position.
  - `pagination.cursor_key(cfg: AppConfig) -> bytes`, which is `hkdf_sha256(session_secret, b"cursor")`.
  - `pagination.encode_cursor(body: Mapping[str, JSONValue], *, key: bytes) -> str` and `decode_cursor(cursor: str, *, key: bytes, sort: str, filters_hash: str) -> Mapping[str, JSONValue]`. The cursor is `base64url(JSON) + "." + base64url(HMAC-SHA256)` (§14.3). A bad signature, a different `sort` or a different `filters_hash` raises `Problem(400, "invalid_cursor")`. M2 uses only the `"v": 2`, `"section": "all"` form for id-ordered lists; M4 adds the job sorts.
  - The routes, each with `SESSION_RESPONSES`, `CSRF_RESPONSES` where state-changing, and its problem codes:
    - `GET /api/settings` → `{version: {id, version_no, created_at, effect_classes}, payload, pending_scope: {version_id, progress: {done, total}} | null}`. The progress comes from the live `apply_scope` operation's `progress`.
    - `POST /api/settings` (Idempotency-Key) with `{payload, base_version_id, acknowledged_warnings: [], eval_override_ack?}` → 201 `{version: {id, version_no, effect_classes}, apply_scope_operation_id}`. The route first runs `SELECT 1 FROM active_config WHERE id = 1 FOR UPDATE`, then `run_idempotent(route="settings.save", …)` around `save_settings`, whose own `FOR UPDATE` is then a no-op. Unlike `api/routes/runs.py`, it never takes `FOR SHARE` first: SHARE then UPDATE in one transaction would make two tabs deadlock on the lock upgrade instead of one getting 409 `stale_base_version`.
    - `GET /api/settings/versions?cursor&limit` (limit ≤ 100, default 50) → `{items: [{id, version_no, created_at, effect_classes}], next_cursor}`, ordered `version_no DESC`.
    - `GET /api/settings/versions/{id}` → `{version, payload, diff}`, where `diff` is `diff_payloads(<version_no − 1>, this)`, empty for version 1.
  - `worker.runs.reconcile_stale_data` (M0 Carry-forward 2): while `schedule_state.enabled` is false it never raises `schedule.stale_data`, and it resolves an open one.

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_settings_save.py (Review Focus 5 pin)
async def test_stale_base_version_is_409(logged_in_client, engine) -> None:
    active = (await logged_in_client.get("/api/settings")).json()
    base, payload = active["version"]["id"], active["payload"]
    first = {**payload, "display": {**payload["display"], "ui_language": "de"}}
    second = {**payload, "schedule": {**payload["schedule"], "interval_hours": 12}}
    r1 = await logged_in_client.post("/api/settings", json={"payload": first, "base_version_id": base,
                                     "acknowledged_warnings": await warning_codes(logged_in_client, first)},
                                     headers=idem("tab-one-key-0001"))
    assert r1.status_code == 201
    r2 = await logged_in_client.post("/api/settings", json={"payload": second, "base_version_id": base,
                                     "acknowledged_warnings": await warning_codes(logged_in_client, second)},
                                     headers=idem("tab-two-key-0001"))
    assert r2.status_code == 409 and r2.json()["code"] == "stale_base_version"
    async with engine.connect() as conn:
        versions = (await conn.execute(text("SELECT count(*) FROM settings_versions"))).scalar_one()
        interval = (await conn.execute(text("SELECT interval_s FROM schedule_state"))).scalar_one()
    assert versions == 2 and interval == 6 * 3600   # the stale save wrote nothing
```

(`warning_codes(client, payload)` in `tests/support/settings.py` returns the codes of `POST /api/settings/dry-run` for the payload, so both saves acknowledge exactly their own warnings and the test isolates the stale base. `save_scope(client, radius_km=…)` in the same module saves the active payload with one scope location, Berlin, at that radius, acknowledging the dry-run's warnings, and returns the 201 body.)

Also cover, in `test_settings_save.py`:
- an unchanged payload gives a new version with `effect_classes = {none}` and only the settings pointer moves;
- a warning code missing from `acknowledged_warnings` gives 422 with `warning_not_acknowledged`, and acknowledging it gives 201;
- an invalid payload gives 422 with the field paths of `validate_fields`;
- **schedule:** an interval change resets `last_evaluated_slot` to the latest slot ≤ now under the new schedule, and the next `tick` inserts no `missed` row;
- **collection:** enabling a new query creates its cells for every location key and `remote-de`, never covered, and a `watchlist[]` entry `{watchlist_company_id, enabled: true}` creates its instance and cell;
- **budget:** a cap change writes the current month's `llm_budget` row (a lowered cap below the counted spend sets `cap_lowered_at`);
- **execution** and **evaluation_policy:** each flips its pointer, and the evaluation policy row is new with `projected_critic_rate = 0.450`;
- **llm_tuple without a profile:** only `model_policy_id` flips and `llm_tuple_id` stays NULL;
- **llm_tuple with a profile:** a new tuple row, the flip, and `ranking_state.generation` + 1;
- **scope:** `pending_scope_version_id` is set and exactly one `apply_scope` operation exists, `pending` (its handler is Task 18's); a save that reverts the scope sets it back to NULL;
- **llm_tuple + scope in one save:** both happen in one transaction (a fault point between them, `settings.after_llm_tuple`, patched with `raising_at("settings.after_llm_tuple")`, rolls back both);
- **llm_tuple + budget in one save:** the bump runs once, after the budget action, and `ranking_state.generation` rises by exactly 1;
- two concurrent saves from the same base never deadlock: one gets 201 and the other 409 `stale_base_version`;
- an idempotent replay with the same key returns the stored 201 body.

`test_settings_routes.py`: `GET /api/settings` shows `pending_scope` with its progress while an apply is live; the versions list pages with a signed cursor and a tampered cursor gives 400 `invalid_cursor`; a version's `diff` names exactly the changed paths. `test_pagination.py`: round trip, a bad signature, a different `filters_hash`. `test_stale_data_disabled_schedule.py`: with the schedule disabled and no run for three intervals the reconciler raises nothing and resolves an open alert; re-enabled, it raises again under the old rule.
- [ ] **Step 2: Run the tests and see them fail.** `uv run pytest -W error tests/integration/test_settings_save.py tests/integration/test_settings_routes.py tests/unit/test_pagination.py tests/integration/test_stale_data_disabled_schedule.py -q`
- [ ] **Step 3: Implement** the save with `ensure_tuple`, the effect actions, the first part of `scope_apply.py`, the pagination helper, the routes and the stale-data exemption, and the test helpers. Add the fault point `settings.after_llm_tuple`. Regenerate `schema.d.ts`.
- [ ] **Step 4: Run the tests and see them pass**, then the M1 suites that read settings (`-k "run_settings or plan or tick or schedule"`), since every save now moves the pointers they read.
- [ ] **Step 5: Commit** `feat(m2): versioned settings save with optimistic concurrency, the effect-class actions in their order and the settings routes`.

### Task 18: Scope apply: precompute, then flip (§13.4), retry-apply, and crash cases C25 and C30

**Files:**
- Create: `tests/crash/test_c25_scope_precompute.py`
- Modify:
  - `backend/src/jobhunter/settings/scope_apply.py` (created by Task 17 with `KIND`, `dedup_key`, `request_apply` and `set_desired_scope`; this task adds the handler, `incomplete_jobs`, the reconciler and the alert constants);
  - `backend/src/jobhunter/scoring/filters.py` (the `_STALE` predicate becomes a shared SQL fragment, `STALE_PREDICATE`, which `_STALE` and `incomplete_jobs` both use);
  - `backend/src/jobhunter/worker/operations.py` (`set_progress`);
  - `backend/src/jobhunter/api/routes/settings.py` (`POST /api/settings/scope/retry-apply`);
  - `backend/src/jobhunter/worker/main.py` (`_REGISTERING_MODULES` += `jobhunter.settings.scope_apply`);
  - `tests/unit/test_worker_main.py`, `frontend/src/api/schema.d.ts`;
  - `tests/support/settings.py` (`active_scope_id`, `pending_scope_id`, `scope_ids_of`, `operation_end`, `incomplete_jobs_for`, `delete_procrastinate_job_of`, `procrastinate_job_exists_for`, and the sync helpers `save_scope_sync`, `active_scope_sync`, `pending_scope_sync`, `incomplete_jobs_sync`, `operation_status_sync` and the `seeded_jobs_sync` fixture, on `pg_one`);
  - `tests/support/reconcile.py` (`run_reconciler(name, engine, *, now, epoch)`, which runs one registered reconciler pass);
  - `tests/support/collect.py` (`JobFactory.many(n)`, which also writes the active scope's `hard_filter_results` for those jobs)
- Test: `tests/integration/test_scope_apply.py`, `tests/integration/test_c30_scope_save_redefer.py`

**Interfaces:**
- Consumes: `scoring.filters.recompute_stale`, `BATCH` (500), `DET_ALGORITHM_VERSION` and `run_settings.scope_from_payload` (M1); `scope_apply.KIND`, `dedup_key`, `request_apply` and `set_desired_scope` (Task 17); `lock_operation_owner`, `finish_operation` (M0); `OperationContext.finish_in` and `requeue_in` (Task 11); `op_runner.run_one(op_id, *, pause, registry)` (Task 11); `raise_alert`/`resolve_alert`; `db.ranking.bump`; `derive.derived`/`ensure_derived` (Task 15).
- Produces:
  - `scope_apply.APPLY_FAILED_ALERT: Final = "scope.apply_failed"` and `scope_apply.FINAL_CHECK_ROUNDS: Final = 5`.
  - `filters.STALE_PREDICATE`, the SQL fragment of the stale-result condition (a missing `hard_filter_results` row for the scope, `inputs_hash ≠ jobs.det_facts_hash`, or another `det_algorithm_version`), shared by `filters._STALE` and `incomplete_jobs` so the predicate is written once.
  - `async def worker.operations.set_progress(conn, claim: OperationClaim, progress: Mapping[str, JSONValue]) -> None`: an `UPDATE … SET progress` under the claim predicate, `FencedOut` on 0 rows.
  - `async def scope_apply.incomplete_jobs(conn, scope_version_id: int) -> int`: the count of unmerged jobs matching `filters.STALE_PREDICATE` for the scope, with no limit and no lock.
  - The handler `@register_operation_handler("apply_scope") async def run_apply_scope(ctx)`:
    1. **Precompute**, outside any `active_config` lock (§13.4 step 2). Each chunk is one transaction: a plain read of `active_config.pending_scope_version_id`, and when it is no longer this scope the operation finishes `succeeded` with progress `{"obsolete": true}`; otherwise `recompute_stale(conn, {sid: scope}, guard=<owner lock>)`, then `set_progress(conn, claim, {"done": …, "total": …})`. `total` is the count of unmerged jobs at the start. It loops until a chunk returns fewer than `BATCH`.
    2. **The final transaction**, in the §9.1 order: `active_config FOR UPDATE`, then the owner row. A pending pointer that differs from `sid` gives the obsolete end. When `incomplete_jobs(conn, sid) > 0` the transaction rolls back and step 1 runs again, at most `FINAL_CHECK_ROUNDS` times; after that the handler requeues the operation with `ctx.requeue_in(conn, error_code='scope_incomplete', not_before=now + 1 min)`. Otherwise it runs `UPDATE active_config SET scope_version_id = pending_scope_version_id, pending_scope_version_id = NULL, updated_at = :now`, then `ranking.bump`, resolves `scope.apply_failed`, and finishes `succeeded` in that transaction (`finish_in`).
    3. The `det_results` part of the final check and the `filtered_out ⇄ pending` target moves are M3's (Deviation 12).
  - `@reconciler("scope_apply_failed", "tick")` (§13.4 step 5; Deviation 12):
    1. `fence_epoch`, then `active_config FOR UPDATE`.
    2. When `pending_scope_version_id = P` and the newest `apply_scope` operation of `apply_scope:P` is `failed` with no live one, it sets `pending_scope_version_id := NULL` and raises `scope.apply_failed` (critical, subject `scope:<P>`), in one transaction.
  - `POST /api/settings/scope/retry-apply` → 202 `{operation_id}`. The scope is `derived(active settings payload, profile=<active profile>)`, through `ensure_derived`.
    - It gives 409 `conflict_state` when that scope is already the active one, or when an `apply_scope` operation is live.
    - Otherwise, holding `active_config FOR UPDATE`, it calls `set_desired_scope` (Task 17).

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_scope_apply.py (Review Focus 5 pin)
async def test_newer_scope_save_supersedes_the_pending_apply(logged_in_client, engine, op_runner, job_factory) -> None:
    await job_factory.many(20)                                          # retained jobs with det-facts
    s0 = await active_scope_id(engine)
    pause = PausePoint("scope_apply.after_chunk")                       # holds the first apply after its first chunk
    first = await save_scope(logged_in_client, radius_km=30)            # pending = A, apply_scope(A)
    run_a = asyncio.create_task(op_runner.run_one(first["apply_scope_operation_id"], pause=pause))
    await asyncio.wait_for(pause.reached.wait(), 10)
    assert await active_scope_id(engine) == s0 and await incomplete_jobs_for(engine, s0) == 0   # the view keeps a complete scope
    second = await save_scope(logged_in_client, radius_km=50)           # pending = B, apply_scope(B)
    pause.resume(); await run_a
    await op_runner.run_one(second["apply_scope_operation_id"])
    a, b = await scope_ids_of(engine, first, second)
    assert await operation_end(engine, first["apply_scope_operation_id"]) == ("succeeded", {"obsolete": True})
    assert await active_scope_id(engine) == b and await pending_scope_id(engine) is None
    assert await incomplete_jobs_for(engine, b) == 0 and a != b
```

(`save_scope` (Task 17), `active_scope_id`, `pending_scope_id`, `scope_ids_of`, `operation_end` and `incomplete_jobs_for` are helpers in `tests/support/settings.py`. `job_factory.many(n)` is `JobFactory.many` in `tests/support/collect.py`, and writes the active scope's `hard_filter_results` for its jobs. `op_runner.run_one(op_id, *, pause: PausePoint | PausePoints | None = None, registry: ExecutionRegistry | None = None)` is Task 11's runner on one operation, with `afault_point` replaced by the given pause points in every module that binds it (`jobhunter.settings.scope_apply` here), and a fresh registry when a pause is given.)

```python
# tests/integration/test_c30_scope_save_redefer.py (crash case C30, in process: the lost job is simulated)
async def test_scope_save_without_its_job_is_redeferred_and_flips(logged_in_client, engine, current_epoch, op_runner) -> None:
    saved = await save_scope(logged_in_client, radius_km=40)
    op_id = saved["apply_scope_operation_id"]
    await delete_procrastinate_job_of(engine, op_id)                    # the commit happened, its job did not
    await run_reconciler("operations", engine, now=utcnow() + timedelta(minutes=2, seconds=1), epoch=current_epoch)  # tests/support/reconcile.py
    assert await procrastinate_job_exists_for(engine, op_id)            # re-deferred under its `op:<id>` queueing lock
    await op_runner.run_one(op_id)
    assert await active_scope_id(engine) == (await scope_ids_of(engine, saved))[0]
```

```python
# tests/crash/test_c25_scope_precompute.py (crash case C25: a real worker dies inside the precompute)
WORKER: Final = [sys.executable, "-m", "jobhunter.cli", "worker"]
def test_crash_during_precompute_never_flips_and_the_retry_completes(migrated, proc_env, procs, seeded_jobs_sync) -> None:
    sid, op_id = save_scope_sync(migrated, radius_km=60)                 # the save path, in its own transaction
    first = procs.spawn(WORKER, proc_env("worker", migrated, JOBHUNTER_FAULT="scope_apply.after_chunk"))
    assert first.exit_within(120) == 86                                 # killed after one committed chunk
    assert active_scope_sync(migrated) != sid and pending_scope_sync(migrated) == sid
    assert incomplete_jobs_sync(migrated, active_scope_sync(migrated)) == 0   # no blank interval in the view
    second = procs.spawn(WORKER, proc_env("worker", migrated))          # the takeover sweep re-queues the operation
    wait_until(lambda: active_scope_sync(migrated) == sid, 120)
    assert pending_scope_sync(migrated) is None and operation_status_sync(migrated, op_id) == "succeeded"
    second.signal(signal.SIGTERM); assert second.exit_within(60) == 0
```

(`seeded_jobs_sync` inserts 1,200 jobs, so the precompute takes three chunks. The `_sync` helpers are in `tests/support/settings.py` and use `pg_one`. `Proc.signal` is the existing `tests/support/procs.py` API. The spawned workers' start-up `cleanup_leftovers` runs on `AppConfig.parse_jobs_dir` (`JOBHUNTER_PARSE_JOBS_DIR`, Task 11) and removes nothing when that root is missing (Task 8).)

Also cover, in `test_scope_apply.py`:
- 1,201 jobs give three chunks, `progress` reaches `{done: 1201, total: 1201}`, the flip, `ranking_state` + 1 and `pending` NULL;
- a job whose `det_facts_hash` changes between the last chunk and the final transaction is recomputed in a second round before the flip;
- **permanent failure:** an apply whose handler always raises reaches `failed('attempts_exhausted')`; the reconciler then clears `pending` and raises `scope.apply_failed`. `POST /api/settings/scope/retry-apply` creates a new operation, and its success flips the scope and resolves the alert;
- retry-apply gives 409 while an apply is live, and 409 when the active settings version's scope is already active;
- the reconciler's fence rule (`assert_fence_rule`).
- [ ] **Step 2: Run the tests and see them fail.** `uv run pytest -W error tests/integration/test_scope_apply.py tests/integration/test_c30_scope_save_redefer.py tests/crash/test_c25_scope_precompute.py -q`
- [ ] **Step 3: Implement** the rest of `scope_apply.py` (the handler, `incomplete_jobs`, the reconciler), `filters.STALE_PREDICATE`, `set_progress` and the route, and the test helpers. Add the fault point `scope_apply.after_chunk`, reached after each chunk's transaction commits. Register the module in `_REGISTERING_MODULES` and update the worker pins. Regenerate `schema.d.ts`.
- [ ] **Step 4: Run the tests and see them pass**, plus `tests/integration/test_hard_filters*.py`.
- [ ] **Step 5: Commit** `feat(m2): scope apply with chunked precompute and a checked flip, permanent-failure recovery and retry-apply; crash cases C25 and C30`.

### Task 19: `embed_profile` and the profile vectors

**Files:**
- Create: `backend/src/jobhunter/profile/vectors.py` *(new)*
- Modify: `backend/src/jobhunter/profile/operations.py` (handler `embed_profile`), `backend/src/jobhunter/privacy/retention.py` (the `embeddings` rule also keeps rows that `profile_vectors` references), `tests/unit/test_worker_main.py`
- Test: `tests/integration/test_embed_profile.py`, `tests/integration/test_retention_m2.py` (this task: the embeddings rule)

**Interfaces:**
- Consumes: `EmbedderClient.embed`, `upsert_embedding`, `text_sha256`, `EMBEDDING_MODEL_ID` (M1); `OperationDeps.embedder: EmbedderClient | None`, `OperationContext.finish_in` and `requeue_in` (Task 11); `ConfirmedProfile` (Task 6).
- Produces:
  - `vectors.vector_texts(profile: ConfirmedProfile) -> list[tuple[str, str]]`: `("summary", summary)` and `("title:<i>", target_titles[i].title)`, with `i` 0-based; `target_titles` items are `{title, lang}` objects, and the text is the `title`. Empty strings are skipped.
  - `vectors.KIND: Final = "embed_profile"` and `vectors.dedup_key(profile_version_id) -> str`.
  - `async def vectors.request_embedding(conn, profile_version_id, *, defer) -> tuple[int, bool]`.
  - The handler `embed_profile`. Payload `{"profile_version_id": id}`.
    - It reads the version.
    - A purged version, or one under an active `profile` purge fence, finishes `succeeded` with progress `{"skipped": "purged" | "fenced"}`.
    - It embeds the texts in one batch with `input_kind="query"`. The embedder adds the `query: ` prefix (`embedder/app.py`).
    - In one owner-fenced transaction it upserts the embeddings, then `INSERT INTO profile_vectors … ON CONFLICT (profile_version_id, kind) DO UPDATE SET embedding_id = EXCLUDED.embedding_id`, deletes kinds no longer present for the version, and finishes the operation (`finish_in`).
    - `EmbedderError`, and an `OperationDeps.embedder` of `None` (a worker without an embedder), requeue through `ctx.requeue_in(conn, error_code='embedder_unavailable', not_before=now + 10 min)`.
  - `@reconciler("profile_vectors", "heavy")` (§12.6 step 3, "recomputed when it returns"; Deviation 23). When the embedder reports healthy, an active profile version with fewer vectors than `vector_texts` and no live `embed_profile` operation gets one. M3's targets read the vectors.

- [ ] **Step 1: Write the failing tests.**
  - `test_embed_profile.py`, on the fake embedder:
    - a version with three target titles yields four `profile_vectors` rows (`summary`, `title:0`, `title:1`, `title:2`) whose embeddings are `input_kind='query'`, and the embedder was sent the exact texts;
    - a second run is a no-op (the same `embedding_id`s);
    - the embedder `down` requeues with `not_before` and writes nothing, and so does a run with `OperationDeps.embedder = None`;
    - an exhausted operation is re-created by the reconciler once the embedder is up;
    - a purged version is skipped.
  - `test_retention_m2.py::test_embeddings_referenced_by_profile_vectors_are_kept`: unreferenced embeddings are deleted, and those of `profile_vectors` stay.
- [ ] **Step 2: Run the tests and see them fail.** `uv run pytest -W error tests/integration/test_embed_profile.py tests/integration/test_retention_m2.py -q`
- [ ] **Step 3: Implement** `vectors.py`, the handler, the reconciler and the retention change.
- [ ] **Step 4: Run the tests and see them pass**, plus the M1 embeddings and retention suites.
- [ ] **Step 5: Commit** `feat(m2): embed_profile writes the profile vectors; embeddings retention keeps profile references`.

### Task 20: `propose_queries`, query proposals and query authoring

**Files:**
- Create: `backend/src/jobhunter/graphs/profile/nodes/propose_queries.py`, `backend/src/jobhunter/queries/{__init__,proposals,authoring}.py`, `backend/src/jobhunter/api/routes/queries.py`, `tests/contract/fixtures/anthropic/propose_queries/claude-opus-5-5/<schema12>.sse` (hand-authored, synthetic; see Step 1)
- Modify: `backend/src/jobhunter/profile/operations.py` (handler `propose_queries`; the handler lives here, so `jobhunter.queries.proposals` registers nothing), `backend/src/jobhunter/graphs/profile/graph.py` (the `propose` graph), `backend/src/jobhunter/api/routes/profile.py` (the two proposal routes), `backend/src/jobhunter/api/app.py`, `frontend/src/api/schema.d.ts`, `tests/unit/test_worker_main.py` (the `propose_queries` handler pin), `tests/unit/test_profile_graph_shape.py` (the `propose` graph)
- Test: `tests/unit/test_query_proposal_validate.py`, `tests/integration/test_propose_queries.py`, `tests/integration/test_queries_api.py`

**Interfaces:**
- Consumes:
  - `LLMGateway.call`, `CallContext(logical_call_no=…, deadline=…)`, the `error_code`s of completed calls (Task 5), `node_config`, `render.propose_queries_prompt` and `QueryProposalOut` (Tasks 5–6);
  - `spend_ledger.receipts` and `graphs/profile/nodes/llm_outcome.py`, the receipts walk and the LLM failure mapping (Task 13), reused unchanged;
  - `tests/support/llm.synthetic_sse` (Task 13), `fake.scenario(*names)` with the `set_invalid` variant (Task 4);
  - `normalize.text.fold` and `Gazetteer` (M1);
  - `save_settings(..., internal=True)` (Task 17);
  - the failure-hook mechanism, `OperationContext` from `config["configurable"]["op"]`, `finish_in`, `requeue_in` and `thread_id` (Task 11).
- Produces:
  - `proposals.LOCATION_WORDS_EXTRA: Final[frozenset[str]]`: `remote`, `homeoffice`, `home office`, `deutschland`, `germany`, `bundesweit`, `deutschlandweit`, `europa`, `europe` and the 16 federal states with their common short forms. The constant is documented in place.
  - `proposals.location_words(scope_labels: Sequence[str], gazetteer: Gazetteer) -> frozenset[str]`: the folded tokens of the scope's location labels, the folded names of gazetteer places with a population of at least 100,000, and `LOCATION_WORDS_EXTRA`.
  - `@dataclass(frozen=True) class proposals.ProposedQuery: text: str; lang: Literal["de", "en"]; target_title: str; exploration: bool; rationale: str`.
  - `proposals.validate_proposal(out: QueryProposalOut, *, location_words: frozenset[str]) -> tuple[list[ProposedQuery], list[str]]`, which checks the §11.12 limits in two levels (Deviation 20):
    - **Items:** an item with text outside 2–120 characters, a whole-word location word, a rationale over 300 characters, or a folded text equal to an earlier item's is dropped. Each drop adds an issue code: `text_length`, `location_word`, `rationale_length` or `duplicate`.
    - **The set:** the kept set must have 6–16 items and at least one `exploration`; otherwise the result is `([], ["set_invalid"])`. A kept set over 16 items is cut to the first 16 in model order, with the issue `truncated`.
  - `proposals.KIND: Final = "propose_queries"`.
  - `async def proposals.request_proposal(conn, profile_version_id: int, *, defer) -> tuple[int, int, bool]`. It returns `(proposal_id, operation_id, created)`. A `running` proposal of the same version is returned with `created=False`; otherwise it inserts `query_proposals(status='running')` and the operation with payload `{"proposal_id": id}` and dedup key `propose_queries:<proposal_id>`.
  - The handler `propose_queries` reads `payload["proposal_id"]` and invokes `profile_graphs.propose` with `config={"configurable": {"thread_id": thread_id("q", proposal_id, claim.operation_id, claim.attempts), "op": ctx}}`.
  - The `propose_queries` node (graph `propose`, thread `queries-<proposal_id>-q<operation_id>-a<attempts>`) walks the receipts first with Task 13's `llm_outcome.py` over `spend_ledger.receipts(conn, operation_id=…, purpose='propose_queries')`, which match `input_refs->'owner'->>'kind' = 'operation'` and `(input_refs->'owner'->>'id')::bigint = :operation_id` (the id `OperationOwner.durable()` writes). Its `input_refs` add `proposal_id` and `profile_version_id`, and any hash as a hex string.
    - Logical call 1 with an `output_json` is validated again (validation is pure and deterministic). A usable set is published without a request; `set_invalid`, or a structurally invalid call 1 (`error_code = 'invalid_output'`), makes logical call 2 due.
    - Logical call 2 with an `output_json` is validated and published, or ends the proposal `failed`. Logical call 2 without one ends it `failed`. So the one repair is consumed exactly once across reclaims.
    - A newest completed call with `error_code` `refused`, `model_mismatch`, `max_tokens` or `unexpected_stop_reason` ends the proposal `failed` and the operation `failed(<that code>)` with no request.
    Then:
    - It renders `propose_queries_prompt(profile_json, scope location labels)` and calls the gateway with `purpose='propose_queries'`, non-scoring, the active model policy and execution config, `OperationOwner(claim)` (the claim from `configurable["op"]`), `deadline = monotonic() + <the active execution config's deadline_s>` (Deviation 28), and `logical_call_no` 1, or 2 for the repair.
    - It validates the output. `set_invalid`, or `InvalidOutput("structural")`, is retried once as logical call 2 (`CallContext(logical_call_no=2)`); a second one ends the proposal `failed` and the operation `failed('invalid_output')`.
    - The error mapping is `llm_outcome.py`'s, the one of Task 13, not a copy: `refused`, `budget_exhausted`, `unexpected_stop_reason` (no repair), the request errors, and the transient requeue through `ctx.requeue_in`.
    - On success it moves the proposal `running → ready` with `items = {"queries": [kept items as dicts]}` and `llm_call_id`, and finishes the operation in the same transaction (`finish_in`). Its failures end the proposal and the operation together the same way.
    - The failure hook moves the proposal `running → failed`.
  - The routes:
    - `POST /api/profile/query-proposals` (Idempotency-Key) → 202 `{proposal_id}`. It needs an active profile (otherwise 409 `conflict_state`), and the call counts against the non-scoring reserve.
    - `GET /api/profile/query-proposals/{id}` → `{status, items}`.
  - `authoring.create_query(conn, *, text, lang, proposal_id: int | None = None, exploration: bool = False, now) -> QueryRow`. It trims the text and checks 2–120 characters. `origin` is `'llm_proposal'` iff `proposal_id` is set, and that proposal must be `ready`. A live query with the same `lower(text)` raises `Problem(409, "conflict_state", extra={"query_id": <existing>})`. Creating a query does **not** enable it: enabling is a settings save, with its quota preview (§13.5).
  - `authoring.edit_query(conn, query_id, *, text=None, lang=None, now, defer) -> QueryRow` (§5.5.1, "retire + create"). In one transaction it:
    1. takes `active_config FOR UPDATE`;
    2. retires the old row and creates the new one with the old row's `origin`, `exploration` and `proposal_id`;
    3. swaps the id in the active payload's `queries[]`, keeping its `enabled` flag, through `save_settings(..., internal=True)` with effect class `collection` (Deviation 15).
  - The routes:
    - `POST /api/queries` with `{text, lang, proposal_id?, exploration?}` → 201 `QueryOut{id, text, lang, origin, exploration, proposal_id}`. The two optional members go beyond the spec's body (Deviation 3).
    - `PATCH /api/queries/{id}` with `{text?, lang?}` → 200, the new `QueryOut`. A retired id gives 404.

- [ ] **Step 1: Write the failing tests.**
  - `test_query_proposal_validate.py`, one table:
    - each item rule drops exactly that item with its code (`Software Engineer Berlin`, `Backend Entwickler remote` and `Data Engineer Bayern` are location-word drops);
    - five valid items give `set_invalid`;
    - no exploration item gives `set_invalid`;
    - `Größe` and `GRÖSSE` collide after `fold`;
    - 17 valid items keep 16, with `truncated`;
    - `Java Entwickler` and `Platform Engineer` are kept (no false location hit).
  - **The fixture.** The paid budget records no `propose_queries` call, so this task hand-authors a synthetic SSE stream at `tests/contract/fixtures/anthropic/propose_queries/claude-opus-5-5/<schema12>.sse`, under the `propose_queries` purpose and the Task 6 schema hash, built with Task 13's `tests/support/llm.synthetic_sse` from a JSON body of 6–16 valid queries for the profile of Task 13's `SYNTHETIC_CV`, with the event shapes V13 recorded. It is labelled synthetic and passes `tests/contract/test_fixture_pii.py`.
  - `test_propose_queries.py`, on the fake LLM with that synthetic stream:
    - a confirmed profile's proposal reaches `ready` with 6–16 items, its `llm_calls` row has `purpose='propose_queries'` and `max_tokens` 8,000, and the ledger row is non-scoring;
    - `fake.scenario("set_invalid", "ok")` retries once (logical call 2) and gives `ready`;
    - `fake.scenario("set_invalid")` (repeated) gives the proposal `failed` and the operation `failed('invalid_output')`;
    - `refusal` gives `failed`;
    - a repeat request while one is running returns it;
    - a fault at `queries.propose.after_call`, after settlement and before `running → ready`, then a reclaim, publishes with `fake_llm.served == 1`;
    - a crash at `queries.propose.after_call` after a `set_invalid` logical call 1, and another after a settled valid logical call 2: the first resumes with exactly one request (the repair), and the second with none;
    - two successive proposals of one profile version use two threads;
    - no profile gives 409.
  - `test_queries_api.py`:
    - `POST` creates an unenabled user query, and the active settings are unchanged;
    - accepting a proposal item gives `origin='llm_proposal'` with `proposal_id`;
    - a duplicate live text gives 409 with the existing id;
    - `PATCH` retires the old row, creates the new one and moves the `queries[]` entry with its `enabled` flag in one new settings version of class `collection`, whose cells exist for the new id;
    - `PATCH` of a retired id gives 404.
- [ ] **Step 2: Run the tests and see them fail.** `uv run pytest -W error tests/unit/test_query_proposal_validate.py tests/integration/test_propose_queries.py tests/integration/test_queries_api.py -q`
- [ ] **Step 3: Implement** the validation, the node, the `propose` graph, the handler and its failure hook, the synthetic fixture, the authoring functions and the routes. Add the fault point `queries.propose.after_call`. Update the handler pin in `tests/unit/test_worker_main.py` and the `propose` graph in `tests/unit/test_profile_graph_shape.py`. Regenerate `schema.d.ts`.
- [ ] **Step 4: Run the tests and see them pass**, plus `tests/unit/test_worker_main.py`, `tests/unit/test_profile_graph_shape.py`, `tests/contract/test_fixture_pii.py` and the M1 planner tests (a retired query is no longer in `queries[]`).
- [ ] **Step 5: Commit** `feat(m2): propose_queries with semantic validation, query proposals and query authoring with retire-and-create`.

### Task 21: Profile confirm and activation (§12.6), the re-score estimate, and crash cases C24 and C34

**Files:**
- Create: `backend/src/jobhunter/profile/activation.py`, `backend/src/jobhunter/profile/estimate.py`, `tests/integration/test_c24_c34_profile_confirm.py`
- Modify:
  - `backend/src/jobhunter/api/routes/profile.py` (`POST /api/profile/drafts/{id}/confirm`, `GET /api/profile/drafts/{id}/rescore-estimate`, `GET /api/profile` filled);
  - `backend/src/jobhunter/api/errors.py` (`ProblemCode` += `profile_purge_in_progress`);
  - `backend/src/jobhunter/graphs/collect/nodes/enqueue_targets.py` and `graphs/collect/nodes/finalize.py` (`SCORE_TARGETS_ENABLED`, Deviation 10);
  - `frontend/src/api/schema.d.ts`;
  - `tests/support/profile.py` (`ready_draft`, `active_profile`, `ranking_generation`)
- Test: `tests/integration/test_profile_confirm.py`, `tests/unit/test_rescore_estimate.py`

**Interfaces:**
- Consumes:
  - `validate_profile`/`validate_user_entered` (Task 12), `ConfirmedProfile` (Task 6);
  - `derive.derived` (with `derived_seniority` inside it)/`ensure_derived` (Task 15), `scope_apply.set_desired_scope` and `save.ensure_tuple` (Task 17), `vectors.request_embedding` (Task 19), `proposals.request_proposal` (Task 20);
  - `raising_at`, `no_fault` and `SimulatedCrash` in `tests/support/faults.py` (Task 17), `op_runner` (Task 11), `count_rows` (Task 9);
  - `llm_projection.central_cost_per_target` and `dryrun.count_retained_current_revisions` (Task 16), `budget.*`;
  - `content_hash`/`canonical_json`, `db.ranking.bump`, `run_idempotent`.
- Produces:
  - `activation.BLOCKING_ISSUES: Final = frozenset({"limit", "date_invalid", "date_order", "code_other"})` (Deviation 19).
  - `@dataclass(frozen=True) class activation.ConfirmOutcome: profile_version_id: int; targets_to_create: int; est_usd: Decimal; bulk_budget_usd: Decimal`, serialised as the §14.2 201 body, with decimals as strings.
  - `async def activation.confirm_draft(conn, draft_id: int, *, expected_rescore_ack: bool, now, defer) -> tuple[int, dict[str, JSONValue]]` returns `(status, body)` and implements §12.6 step 2 in **one transaction**, in this order:
    1. `active_config FOR UPDATE`, then the draft `FOR UPDATE`.
    2. **Already confirmed** returns `(201, draft.confirm_response)`: the original body, whatever key the request used and whichever profile is active now (Deviation 2).
    3. A draft that is not `ready` gives 409 `conflict_state`.
    4. It revalidates `draft_json` as `ConfirmedProfile`, with `validate_profile` and `validate_user_entered`. A `BLOCKING_ISSUES` issue gives 422 `validation_failed` with the issue paths.
    5. `estimate = rescore_estimate(...)`. When `estimate.targets > 0` and not `expected_rescore_ack`, it gives 422 `validation_failed` on `expected_rescore_ack`.
    6. **(a)** `profile_hash = content_hash(profile_json)`. A usable version with the hash (`purged_at IS NULL`) is reused; under an active `profile` purge fence it gives 409 `profile_purge_in_progress`. Otherwise it inserts `profile_versions(version_no = max + 1, draft_id, cv_file_id, profile_json, profile_hash, confirmed_at = now)`.
    7. **(b)** `save.ensure_tuple(conn, profile_version_id=…, model_policy_id=<active model policy>)` selects or inserts the `llm_tuples` row (profile, active rubric, active prompt-schema, active model policy); the select-or-insert is not written again here.
    8. **(c)** It sets `profile_version_id` and `llm_tuple_id`, then `ranking.bump`.
    9. **(d)–(f)** are M3's (Deviation 10).
    10. **(g)** `ensure_derived(derived(active settings, profile=new))` gives the desired scope; with `seniority_range.mode = explicit` it is the settings' own scope. `set_desired_scope(conn, desired, defer=defer)` then reconciles the pending pointer on **every** confirm. So confirming A, then B (B's apply pending), then A again, clears the obsolete pending B instead of letting B's apply flip the filters to B's seniority range.
    11. **(h)** `request_embedding(version)` and `request_proposal(version)`. `create_targets_for_tuple` is M3's.
    12. **(i)** The draft moves `ready → confirmed` with `confirmed_profile_version_id` and `confirm_response = body`.
  - `@dataclass(frozen=True) class estimate.RescoreEstimate: targets: int; est_usd_online: Decimal; est_usd_batch: Decimal; month_spend: Decimal; cap: Decimal; bulk_budget_usd: Decimal`.
  - `estimate.central_batch_cost_per_target(model_policy) -> Decimal`: the §21.4 batch path with the 1% repairs, $0.0467 for preset B.
  - `async def estimate.rescore_estimate(conn, settings: Settings, *, now) -> RescoreEstimate`, with `targets = count_retained_current_revisions(conn)`:
    - `est_usd_online = targets × central_cost_per_target`;
    - `est_usd_batch = targets × central_batch_cost_per_target`;
    - `bulk_budget_usd = bulk_rescore_max_budget_share × (A × (1 − reserve_frac) − scoring_counted_usd)` (§6.9.3);
    - `month_spend` is the month's `counted_usd`, read without a lock.
  - The routes:
    - `POST /api/profile/drafts/{id}/confirm` (Idempotency-Key) with `{expected_rescore_ack: bool}` → 201 `{profile_version_id, targets_to_create, est_usd, bulk_budget_usd}`. The route first runs `SELECT 1 FROM active_config WHERE id = 1 FOR UPDATE`, then `run_idempotent(route="profile.confirm")` around `confirm_draft`, whose own step-1 `FOR UPDATE` is then a no-op; it never takes `FOR SHARE` first, which would deadlock two confirms on the lock upgrade.
    - `GET /api/profile/drafts/{id}/rescore-estimate` → `{targets, est_usd_online, est_usd_batch, month_spend, cap}`.
    - `GET /api/profile` → `{active, versions}`, each `ProfileVersionOut{id, version_no, confirmed_at, purged, profile}`. `profile` is null when purged.
  - `enqueue_targets.SCORE_TARGETS_ENABLED: Final = False`. While it is false, node 9 is a no-op with a tuple set, and `finalize`'s I5 passes with `{"llm_tuple_id": <id>, "targets": "m3"}`. This replaces the `NotImplementedError`; M3 sets it true and fills both (Deviation 10, superseding M1 Carry-forward 9).

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/test_c24_c34_profile_confirm.py (crash cases C24 and C34, in process: an injected exception inside the transaction rolls it back, as a killed process would)
@pytest.mark.parametrize("point", ["confirm.after_version", "confirm.after_flip", "confirm.before_commit"])
async def test_c24_crash_inside_activation_is_all_or_nothing(logged_in_client, engine, ready_draft, point, monkeypatch) -> None:
    monkeypatch.setattr(activation_module, "afault_point", raising_at(point))   # raises SimulatedCrash at the point
    r = await logged_in_client.post(f"/api/profile/drafts/{ready_draft}/confirm", json={"expected_rescore_ack": True},
                                    headers=idem("c24-key-000000001"))
    assert r.status_code == 500
    assert await active_profile(engine) == (None, None)                          # profile_version_id, llm_tuple_id
    assert await count_rows(engine, "profile_versions") == 0 and await count_rows(engine, "llm_tuples") == 0
    assert await count_rows(engine, "operations", kind=("embed_profile", "propose_queries", "apply_scope")) == 0
    assert (await draft_row(engine, ready_draft)).status == "ready"
async def test_c34_retry_with_the_same_key_activates_once(logged_in_client, engine, ready_draft, monkeypatch) -> None:
    key, generation_before = idem("c34-key-000000001"), await ranking_generation(engine)
    monkeypatch.setattr(activation_module, "afault_point", raising_at("confirm.before_commit"))
    assert (await logged_in_client.post(f"/api/profile/drafts/{ready_draft}/confirm", json={"expected_rescore_ack": True}, headers=key)).status_code == 500
    monkeypatch.setattr(activation_module, "afault_point", no_fault)
    first = await logged_in_client.post(f"/api/profile/drafts/{ready_draft}/confirm", json={"expected_rescore_ack": True}, headers=key)
    again = await logged_in_client.post(f"/api/profile/drafts/{ready_draft}/confirm", json={"expected_rescore_ack": True}, headers=key)
    other = await logged_in_client.post(f"/api/profile/drafts/{ready_draft}/confirm", json={"expected_rescore_ack": True}, headers=idem("c34-key-000000002"))
    assert first.status_code == again.status_code == other.status_code == 201
    assert first.json() == again.json() == other.json()
    assert await count_rows(engine, "profile_versions") == 1 and await count_rows(engine, "llm_tuples") == 1
    assert await ranking_generation(engine) == generation_before + 1 and await count_rows(engine, "operations", kind=("embed_profile",)) == 1
```

(The SPA-facing error of an exception inside the transaction is the M0 500 `internal`; the client's retry is the test's own. `raising_at`, `no_fault` and `SimulatedCrash` are in `tests/support/faults.py` (Task 17); `ready_draft`, `active_profile` and `ranking_generation` are in `tests/support/profile.py` (this task). The fault points `confirm.after_version`, `confirm.after_flip` and `confirm.before_commit` are added in `activation.py`.)

Also cover, in `test_profile_confirm.py`:
- confirm of a ready draft creates version 1, a tuple of the seeded rubric, prompt-schema and model policy, the flip, ranking + 1, the `embed_profile` and `propose_queries` operations, and the draft `confirmed`;
- confirming a second draft with identical content reuses the version and the tuple;
- a purged version with that hash is not reused (a new id);
- an active `profile` fence on the matching version gives 409 `profile_purge_in_progress`;
- a `limit` issue gives 422, and an `ungrounded` issue does not block;
- retained jobs without `expected_rescore_ack` give 422;
- `profile_relative` seniority creates the derived scope and one `apply_scope` operation, and `explicit` creates neither;
- `test_confirm_a_b_a_clears_the_obsolete_pending_scope`: confirm profile A (a `junior`) and let its apply flip; confirm B (a `senior`) and pause B's apply after its first chunk; confirm A again. `pending` is NULL, B's apply ends obsolete when resumed, and the active scope is still A's;
- after confirm, a collect run on the fake sources ends `succeeded`, with node 9 a no-op and I5 `ok` (Deviation 10);
- `GET /api/profile` lists the version.

`test_rescore_estimate.py`: 1,000 targets on preset B give $52.60 ± $0.10 online and $46.70 ± $0.10 batch (§21.4); the bulk budget at the start of a month is 40% × $93.10 = $37.24.
- [ ] **Step 2: Run the tests and see them fail.** `uv run pytest -W error tests/integration/test_profile_confirm.py tests/integration/test_c24_c34_profile_confirm.py tests/unit/test_rescore_estimate.py -q`
- [ ] **Step 3: Implement** `activation.py`, `estimate.py`, the routes, the problem code and the `SCORE_TARGETS_ENABLED` switch. Regenerate `schema.d.ts`.
- [ ] **Step 4: Run the tests and see them pass**, plus the M1 end-to-end test and `tests/unit/test_openapi_contract.py` (the new `ProblemCode`).
- [ ] **Step 5: Commit** `feat(m2): profile confirm and activation in one transaction with the derived seniority scope, re-score estimate; crash cases C24 and C34`.

### Task 22: The watchlist and gazetteer routes (taken early from M4; Deviation 3)

**Files:**
- Create: `backend/src/jobhunter/api/routes/{watchlist,geo}.py`
- Modify: `backend/src/jobhunter/api/app.py` (the routers; the gazetteer loaded once, lazily, on `api.state`), `frontend/src/api/schema.d.ts`
- Test: `tests/integration/test_watchlist_api.py`, `tests/integration/test_geo_api.py`

**Interfaces:**
- Consumes: `watchlist_patterns.detect` (`Detected | Unsupported`), `watchlist_validate.request_validation`, `instance_for_company`, `normalize.companies.company_key`, `Gazetteer.search` (M1); `save_settings(..., internal=True)` (Task 17).
- Produces the §14.2 rows:
  - `POST /api/watchlist/parse-url` with `{url}` → `{ats, slug, region} | {unsupported: reason}`. It makes no network request.
  - `POST /api/watchlist` with `{display_name, ats_type, board_slug, lever_region?, input_url?}` → 201 `{id, validation_status: "unvalidated"}`. In one transaction it:
    1. takes `active_config FOR UPDATE`;
    2. inserts `watchlist_companies` with `company_key`, and `detection` `'pattern'` when `input_url` is given, else `'manual'`;
    3. calls `instance_for_company`;
    4. calls `request_validation` (the M1 operation);
    5. adds `{watchlist_company_id, enabled: true}` to `watchlist[]` through `save_settings(..., internal=True)`, which gives a `collection` version.
    A board already on the list gives 409 `conflict_state` with the existing id.
  - `DELETE /api/watchlist/{id}` → 204. In one transaction it removes the `watchlist[]` entry through an internal save ("the settings version updates") and deletes the company row; `source_instances.watchlist_company_id` is set to NULL by its FK. An unknown id gives 404.
  - `GET /api/geo/places?q=<name or postal code>&limit=10` (limit 1–25) → `[{gazetteer_id, label, postal_codes, lat, lon, population}]`, served from the bundled gazetteer.

- [ ] **Step 1: Write the failing tests.**
  - `test_watchlist_api.py`:
    - parse-url for one URL of each of the six ATS gives its slug, Lever's region included, and an unsupported host gives the reason;
    - `POST` creates the company, its instance, one `watchlist_validate` operation and one new settings version whose `watchlist[]` holds the entry, with cells for its instance;
    - a duplicate gives 409;
    - `DELETE` removes the entry in a new version and the company row, and the instance keeps its postings;
    - every route needs a session, and the state-changing ones need CSRF.
  - `test_geo_api.py`:
    - `q=münch` and `q=Munich` both list München first;
    - `q=10115` gives Berlin;
    - `limit` is honoured;
    - an empty `q` gives the most populous places;
    - `limit=26` gives 422.
- [ ] **Step 2: Run the tests and see them fail.** `uv run pytest -W error tests/integration/test_watchlist_api.py tests/integration/test_geo_api.py -q`
- [ ] **Step 3: Implement** the routes. Regenerate `schema.d.ts`.
- [ ] **Step 4: Run the tests and see them pass**, plus the M1 watchlist-validation tests.
- [ ] **Step 5: Commit** `feat(m2): watchlist and gazetteer routes for settings authoring`.

### Task 23: Worker wiring, the M2 retention rules, the planner split and the dev stack

**Files:**
- Create: `backend/src/jobhunter/quota/cells.py`, `backend/src/jobhunter/sources/spec_adapters.py` *(new; M1 Carry-forward 13)*, `deploy/compose.fakellm.yaml`, `tests/unit/test_quota_imports.py`
- Modify:
  - `backend/src/jobhunter/quota/planner.py` (it imports from the two new modules);
  - the other importers of the moved names: `backend/src/jobhunter/settings/dryrun.py`, `backend/src/jobhunter/settings/save.py`, `backend/src/jobhunter/sources/jsearch.py`, `tests/unit/test_planner.py`, `tests/integration/test_plan_node.py`;
  - `backend/src/jobhunter/sources/coverage.py` (`CURSOR_REUSE_TTL` moves here from `sources/jsearch.py`, which imports it back);
  - `backend/src/jobhunter/worker/{main,runtime}.py`;
  - `backend/src/jobhunter/worker/tasks.py` (`retention_daily` passes `cv_store=rt.cv_store`);
  - `backend/src/jobhunter/llm/{gateway,errors}.py` (`LLMNotConfigured`);
  - `backend/src/jobhunter/graphs/profile/nodes/llm_outcome.py` (the mapping `LLMNotConfigured → failed('llm_not_configured')`), used by `graphs/profile/nodes/{extract_profile,propose_queries}.py`;
  - `backend/src/jobhunter/privacy/retention.py` (the M2 rules, M1 Carry-forward 17, and `run_retention_daily(engine, queue, saver, *, cv_store: CvStore, now, epoch)`, since the `cv_files` and `cv_store_orphans` rules delete files);
  - `tests/integration/test_retention.py` and `tests/integration/test_retention_m1.py` (their `run_retention_daily` calls pass a tmp `CvStore`);
  - `backend/src/jobhunter/config.py` (an empty optional secret reads as absent);
  - `deploy/compose.dev.yaml` (the worker's optional `anthropic_key` secret);
  - `scripts/dev-secrets.sh`;
  - `tests/unit/test_worker_main.py`, `tests/unit/test_config.py`
- Test: `tests/integration/test_retention_m2.py` (the remaining rules), `tests/integration/test_retention_batches.py`, `tests/unit/test_worker_main.py`, `tests/stack/test_dev_stack.py` (the docparse and fake-LLM services)

**Interfaces:**
- Consumes: every M2 handler and reconciler; `privacy.retention.RULES`, `_DELETES` and `run_retention_daily`; `CvStore.delete`.
- Produces:
  - **Worker registration.** `_REGISTERING_MODULES` holds every module that registers an M2 handler or reconciler: `jobhunter.llm.spend_ledger`, `jobhunter.llm.gateway`, `jobhunter.profile.operations`, `jobhunter.profile.vectors` and `jobhunter.settings.scope_apply`. The `propose_queries` handler is in `jobhunter.profile.operations` (Task 20), and the `queries` package registers nothing, so no `queries` module is listed.
  - `test_worker_main.py` pins:
    - the handlers: `apply_scope`, `embed_profile`, `extract_profile`, `extract_profile_text`, `propose_queries` and `watchlist_validate`;
    - the M2 reconcilers: `budget_counters` (heavy), `llm_ledger_unsettled` (tick), `llm_refusal_rate` (heavy), `scope_apply_failed` (tick) and `profile_vectors` (heavy).
  - **The worker's LLM.** `WorkerRuntime.gateway` is built with the `anthropic_key` secret, or in fake mode with the recorded transport.
    - With neither, `LLMGateway.call` raises `errors.LLMNotConfigured` before any reservation, and the nodes map it to `failed('llm_not_configured')` through `llm_outcome.py`'s mapping.
    - The worker logs `llm_not_configured` once at start.
    - `config.py` treats an empty optional secret file as absent, because compose needs the file to exist.
  - **The M2 rows of `privacy.retention.RULES` (§20.1)**, each in bounded, fenced transactions (M1 Carry-forward 17):
    - `RetentionRule("draft_content", timedelta(days=30))`: for drafts `confirmed`, `discarded` or `failed` for 30 days with `content_purged_at IS NULL`, it sets `draft_json := NULL` and `reviewed_text := NULL`, sets `content_purged_at`, and nulls the `output_json` of the draft's `extract_profile` calls. Each batch locks its drafts `FOR UPDATE` and re-checks status and age under the lock, so a draft that a retry reopened (`failed → extracting_profile`) in the meantime is skipped.
    - `RetentionRule("cv_files", timedelta(days=30))` (§20.1).
      - **Kept:** the `cv_file_id` of the active profile version and of the previous one, and the file of any open draft. "Previous" follows the activation order, not `version_no`: it is the `confirmed_profile_version_id` of the newest confirmed draft before the newest one that names a different version. A reactivated older version therefore keeps the version it replaced as its previous.
      - **Purged at once:** every other file of a confirmed draft. That covers both the files of versions that are neither active nor previous, and an upload whose confirm reused another version with the same `profile_hash` (that version keeps its own `cv_file_id`).
      - **Purged after 30 days:** the files of drafts `discarded` or `failed` for 30 days (`status_changed_at`), and abandoned uploads (a row no draft references) 30 days after `uploaded_at`.
      - **Serialised with activation.** Each batch of at most 100 files is one transaction, in the §9.1 order: `fence_epoch`, `active_config FOR SHARE`, the protection set computed under it, the candidates' drafts `FOR UPDATE` with their status and age re-checked under that lock, the candidate `cv_files` rows `FOR UPDATE`, the unlinks, the row updates, then the commit. A confirm, which takes `active_config FOR UPDATE`, waits for the batch, and so does a retry, which takes the draft `FOR UPDATE` (Task 14). So no file is ever deleted under a protection set that a committed reactivation or reopening has changed.
      It deletes the file from `cv_store` first (missing is fine), then sets `storage_path`, `raw_text_enc` and `stripped_text` to NULL and sets `purged_at` (Deviation 21).
    - `RetentionRule("cv_store_orphans", timedelta(days=1))`: a file in `cv_store` that no `cv_files.storage_path` names and that is older than a day is deleted. That is an upload whose transaction rolled back after the file was written.
    - `RetentionRule("eval_runs", THIRTEEN_MONTHS)`: it keeps the latest completed run of each kind, and the calls cascade.
    - `RetentionRule("profile_checkpoints", timedelta(days=14))`: a `profile-…` or `queries-…` thread is deleted when the operation whose id its name carries (Deviation 27) finished 14 days ago or no longer exists. Each thread is tied to its own execution, so a newer or running operation's thread is never touched.
  - **Carry-forward 17.** `_purge_threads` and `_delete_finished_jobs` work in batches of 500 threads and 1,000 jobs, each batch its own fenced transaction.
  - **Carry-forward 13.**
    - `quota/cells.py` holds `CellRow`, `ensure_cells`, `_cell_rows`, `location_keys`, `_places` and the cell SQL.
    - `sources/spec_adapters.py` holds `_spec_adapters`, `_refuse` and `_no_secret` (the planning-time adapter factory).
    - `planner.py` re-exports nothing, and every importer is updated.
    - `test_quota_imports.py` (an AST scan, `forbidden_imports(source: str) -> list[str]`):
      - It fails when any module under `quota/` imports `jobhunter.sources.registry` (the adapter factory, `make_adapter`) or a concrete adapter module: `jobhunter.sources.{jsearch,serpapi,adzuna,arbeitnow}` and `jobhunter.sources.ats.*`.
      - `CURSOR_REUSE_TTL` moves to `sources/coverage.py`, which `quota/` may import.
      - The test also feeds `forbidden_imports` today's `planner.py` import lines (`from jobhunter.sources.registry import make_adapter` and `from jobhunter.sources.jsearch import CURSOR_REUSE_TTL`) and asserts both are rejected, so the check is proven against the pre-refactor imports.
  - **The dev stack.**
    - `deploy/compose.fakellm.yaml` sets `JOBHUNTER_FAKE_LLM=1` and `JOBHUNTER_FAKE_LLM_DIR=/data/fixtures/anthropic` on the worker, mounting `tests/contract/fixtures/anthropic` read-only.
    - `scripts/dev-secrets.sh` writes `anthropic_key` from `JOBHUNTER_ANTHROPIC_API_KEY` when it is set, and an empty file otherwise, with the script's existing convention: group 10010, mode 0440, set before the file appears under its name. It never echoes the value, and it never overwrites an existing file.
    - The worker lists `anthropic_key` among its secrets.

- [ ] **Step 1: Write the failing tests.**
  - `test_retention_m2.py`:
    - each new rule at its edge (29 and 31 days);
    - the active and the previous version's files are kept, and an older version's file is deleted on disk and nulled in the row;
    - a second upload whose confirm reused the active version (identical content, a different `cv_files` id) is purged, while the version's own file stays;
    - after V1, V2 and V3, a confirm reusing V1 keeps V1's and V3's files and purges V2's;
    - `test_retention_and_retry_are_serialised`, both orders, for a native-PDF draft `failed('refused')` 31 days ago:
      - a retry with `mode="llm"` that commits first reopens the draft, and the batch then skips its file and content;
      - a batch paused at `retention.cv_files.after_select` holds the draft lock, the concurrent retry waits, and after the purge commits the retry gives 409 `conflict_state` with no operation created;
    - `test_regenerated_content_is_purged_again`: a native-PDF draft `failed` 31 days ago; the `draft_content` batch commits, and a successful native retry follows before the file batch runs. The draft reaches `ready` and is discarded, and 30 days later the new `draft_json` and the new call's `output_json` are purged;
    - `test_retention_and_reactivation_are_serialised`, both orders:
      - a confirm reusing V1 that commits before the batch starts keeps V1's file;
      - a batch paused at `retention.cv_files.after_select`, holding V1 as a candidate, makes a concurrent confirm reusing V1 wait on `active_config` (`tests/support/pg.lock_waits`); after the batch commits and the confirm proceeds, no unlink happens after the confirm's commit;
    - a proposal thread of a finished operation goes after 14 days, while a running proposal's thread of the same profile version stays;
    - the `content_purged_at` CHECKs hold;
    - the latest completed `eval_runs` row of a kind survives 14 months;
    - profile threads are deleted with their checkpoints, and run threads are untouched.
  - `test_retention_batches.py`: 1,200 threads take three transactions; a takeover fence between batches stops the rule after the batch in flight, and the next run resumes.
  - `test_worker_main.py`: the pins above, and a worker without a key that is not in fake mode starts and logs `llm_not_configured`.
  - `test_config.py`: an empty `anthropic_key` reads as absent.
  - `test_dev_stack.py`, on a fresh stack (`down -v`) with a **non-empty dummy** `anthropic_key` and the fakellm overlay:
    - `docparse` is running;
    - the worker reads every secret, its startup assertions pass, and it holds groups 10010 and 10011;
    - a PDF upload stores its file on `cv_store` (owned by 10001, mode 0600);
    - the worker reaches docparse through `parse_jobs`, and the draft reaches `text_review`.
- [ ] **Step 2: Run the tests and see them fail.** `uv run pytest -W error tests/integration/test_retention_m2.py tests/integration/test_retention_batches.py tests/unit/test_worker_main.py tests/unit/test_config.py tests/unit/test_quota_imports.py -q`
- [ ] **Step 3: Implement** the wiring, the rules, the batching, the split and the stack files. Add the fault point `retention.cv_files.after_select`, reached after a `cv_files` batch has selected and locked its candidates.
- [ ] **Step 4: Run the whole suite**: `uv run --group docparse pytest -W error -q`, `uv run ruff check .`, `uv run mypy`, `uv run python scripts/check_llm_boundary.py`, and the stack tests on `-f deploy/compose.dev.yaml -f deploy/compose.fakellm.yaml`.
- [ ] **Step 5: Commit** `feat(m2): worker wiring of the M2 handlers and reconcilers, M2 retention rules with batched deletes, the planner split and the fake-LLM dev overlay`.

### Task 24: M2 acceptance

**Files:**
- Create: `tests/integration/test_m2_end_to_end.py`, `tests/crash/test_profile_worker_end_to_end.py`, `scripts/smoke_m2.py`, `tests/unit/test_smoke_m2_script.py`
- Modify: `docs/superpowers/plans/2026-10-16-job-hunter-roadmap.md` (the M2 status row), `docs/spikes/{V4,V5,V11,V13,V19}.md` (complete)

**Interfaces:**
- Consumes: the dev stack, the CLI, the API and every M2 module.
- Produces: `scripts/smoke_m2.py`, which exits 0 when every check passes and prints one JSON line per check, like `smoke_m1.py`, importing its helpers. It never prints a key, a cookie, a password or any CV content.

- [ ] **Step 1: Write the end-to-end tests.**
  - `tests/integration/test_m2_end_to_end.py::test_profile_and_settings_flow_on_the_fake_llm`, in process with `op_runner` and the hand-authored synthetic `extract_profile` and `propose_queries` streams of Tasks 13 and 20:
    1. paste `SYNTHETIC_CV` (Task 13, `tests/support/profile.py`) → `text_review`, with the strip report's counts;
    2. remove one span → approve → `extract_profile` → `ready`;
    3. `PUT` one field edit → confirm with `expected_rescore_ack` → version 1, a tuple, ranking + 1;
    4. `embed_profile` writes four vectors, and `propose_queries` gives a `ready` proposal;
    5. accept 6 items through `POST /api/queries`, then a settings save that enables them, acknowledging the dry-run's warnings → cells exist;
    6. a scope save (the radius) → `apply_scope` → the flip;
    7. a manual collect run on the fake sources ends `succeeded` with I5 `ok`;
    8. `GET /api/system/spend` shows two `billed` non-scoring calls, `extract_profile` and `propose_queries`, and the counters equal the ledger (`spend_ledger.recompute` returns `None`);
    9. no log line, checkpoint blob or `operations.payload` contains a marker word of the CV.
  - `tests/crash/test_profile_worker_end_to_end.py::test_pdf_draft_through_the_worker_process`:
    - a real `jobhunter worker` child with `JOBHUNTER_FAKE_LLM=1`, a `docparse.worker` child on a tmp `parse_jobs` root (both given it as `JOBHUNTER_PARSE_JOBS_DIR`) and the fake embedder;
    - a synthetic 2-page PDF is uploaded through the API test client;
    - the draft reaches `text_review` within 60 s, and after the approval `ready` within 60 s;
    - SIGTERM gives `worker_shutdown_complete` with exit 0.
- [ ] **Step 2: Write the smoke script.** `scripts/smoke_m2.py` drives the dev stack with `-f deploy/compose.dev.yaml -f deploy/compose.fixtures.yaml -f deploy/compose.fakellm.yaml` (always with `--env-file deploy/images.lock`). Its checks, in order:
  1. `user_create`, 2. `login` and 3. `health_ready`, as in M1;
  4. `cv_upload`: `POST /api/profile/cv` with a synthetic PDF built by `tests/support/profile.minimal_pdf` → 202;
  5. `text_review`: poll the draft until `text_review` (90 s); `strip_report.email ≥ 1`;
  6. `extract_ready`: approve, then poll until `ready` (90 s);
  7. `confirm`: confirm → 201, and `GET /api/profile` shows `active.version_no == 1`;
  8. `proposal_ready`: poll the proposal of `propose_queries` until `ready`, with 6–16 items;
  9. `settings_save`: a dry-run, then a save that enables two accepted queries with the warnings acknowledged → 201;
  10. `spend`: `GET /api/system/spend` gives `counted > 0` and `counted ≤ cap − buffer`.
  `test_smoke_m2_script.py` checks the script's check list and that it never prints the cookie (the M1 test's pattern).
- [ ] **Step 3: Bring up the stack and run everything**

```bash
timeout 60 sh -c 'until docker info >/dev/null 2>&1; do sleep 1; done'
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run python scripts/check_llm_boundary.py
uv run --group docparse pytest -W error -q && uv run pytest -W error tests/payload -q && uv run pytest -W error tests/contract -q
uv run --group docparse pytest -W error -m docparse tests/docparse -q
uv run pytest tests/stack -m stack -q && uv run pytest tests/perf -m perf -q
(cd frontend && npm test && npm run build && npm run lint && npm run typecheck) && scripts/check-openapi-drift.sh
docker compose --env-file deploy/images.lock -f deploy/compose.dev.yaml -f deploy/compose.fixtures.yaml -f deploy/compose.fakellm.yaml down -v
docker compose --env-file deploy/images.lock -f deploy/compose.dev.yaml -f deploy/compose.fixtures.yaml -f deploy/compose.fakellm.yaml up -d --build --wait
uv run python scripts/smoke_m2.py
uv run jobhunter schema-dry-run || true   # the fake transport: the case table of V4.md (generator rejected, Deviation 33); exits non-zero while a gating case fails
```

Expected: every command exits 0 except `schema-dry-run`, whose case table must equal V4.md's (generator rejected with the grammar error, the other gating cases as V4 recorded them, the probe answered by `count_tokens`), and the smoke script prints ten `{"check": …, "ok": true}` lines. No paid call runs in this step.
- [ ] **Step 4: Close the spikes and the roadmap.** This step is a gate:
  - `docs/spikes/V4.md`, `V5.md` (local part), `V11.md`, `V13.md` and `V19.md` hold their results;
  - `.fixtures-raw/anthropic-budget.json` shows ≤ 8 paid messages and ≤ $0.80 of worst-case reservations, and the actual total is copied into `V19.md`, with no key and no content;
  - `bounds.PROMPT_OVERHEAD_TOKENS` holds the V19 values;
  - `NATIVE_PDF_ENABLED` matches V19's verdict.
  Then set the M2 status cell of the roadmap to `complete`, and leave the other rows unchanged.
- [ ] **Step 5: Commit, then the final 4-eyes review**

```bash
git add scripts/smoke_m2.py tests docs/spikes docs/superpowers/plans/2026-10-16-job-hunter-roadmap.md
git commit -m "test(m2): profile and settings end to end on the fake LLM through the graphs and the worker; acceptance smoke; mark M2 complete"
```

Then comes the milestone 4-eyes review of the roadmap's quality gate 3: a whole-branch review by Codex GPT-6 Astra (`model_reasoning_effort="high"`, never `xhigh`) and Claude Fable 5.1. Findings are fixed and the review repeated until both approve. Record it in `docs/council/m2-final-review/`, as M1's was.

---

## Deviations

Each deviation stages a spec item across milestones, or settles something the spec leaves open. Entries that change user-visible behaviour say so.

1. **Tables and FKs staged across milestones** (continuing M0 Deviation 1 and M1 Deviation 1).
   - M2 creates every table of §7.3 (`llm_tuples`), §7.4, §7.5 (`query_proposals`), §7.10 (`profile_vectors`), §7.11 (`llm_budget`, `llm_spend_ledger`, `llm_calls`) and §7.13 (`eval_runs`). It closes the FKs of `active_config`, `runs.llm_tuple_id` and `queries.proposal_id`.
   - Five columns keep their type without their spec FK until M3 creates the referenced tables: `llm_calls.attempt_id`, `target_id`, `golden_case_id`, `batch_item_id` and `llm_spend_ledger.bulk_operation_id`. Every CHECK stays verbatim.
2. **`profile_drafts.confirm_response jsonb`** (a column beyond the §7.4 DDL), with `CHECK (confirm_response IS NULL OR status = 'confirmed')`. §12.6 requires a repeated confirm to return the original outcome "even if a newer profile is active by then", and a new `Idempotency-Key` finds no `api_idempotency` row, which also expires after 7 days. The column stores the 201 body: ids and decimals only, no content. The draft-content purge leaves it. No other behaviour change.
3. **Routes and request members beyond the §14.2 rows of M2.**
   - `/api/watchlist/parse-url`, `/api/watchlist`, `/api/watchlist/{id}` and `/api/geo/places` are taken from M4 (M1 Carry-forward 3), because the settings editor cannot author `watchlist[]` entries or scope locations without them.
   - `api/pagination.py` implements §14.3's signed cursor for id-ordered lists only.
   - `POST /api/queries` also takes `proposal_id` and `exploration`, so an accepted proposal keeps its origin (§5.5.1).
   - `POST /api/watchlist` also takes `input_url?`, beyond its §14.2 body, so a company added from a parsed URL records `detection = 'pattern'` instead of `'manual'` (Task 22).
   - `DraftOut` also carries `repairs` (the §12.7 repairs), `edits` (the stored text edit set, Deviation 37), `segments` (the map between the stripped and the raw text, offsets in Unicode code points, never raw text) and `recomputed_total_years` (§15.7's "recomputed from roles"); `PUT /text` also restores by raw span, and the retry answers 202 with the draft's status.
   - `repairs` follows the stored state, through the retry route's own predicates, not the failure code: every text-phase failure offers `paste_text` (§12.7's "paste text is the alternative"); `retry_llm` appears only while `reviewed_text` is stored and unpurged, or for a native draft whose PDF remains, `NATIVE_PDF_ENABLED` holds and `check_pdf` did not fail it with a deterministic document code; `native_pdf` appears only after `low_text` or a model-outcome code (`invalid_output`, `refused`, `max_tokens`, `unexpected_stop_reason`, `model_mismatch`); and `mode="llm"` without its input answers 409 `conflict_state` (Task 14).
   - Approve sets the draft's `native_pdf := false`, so text approved after a native retry is sent as text; `PUT /text` from `ready` also empties `validation_issues` and rewrites `strip_report`; `PUT /drafts/{id}` also runs `validate_user_entered` and stores the repaired profile; an edit set naming a replacement or a span twice answers 422 `duplicate`.
   - `POST /api/settings/dry-run` returns validation errors with 200, because they are its preview; the save answers 422.
   User-visible: the additions and the `repairs` rule only.
4. **Layout differences from §4.2.**
   - New modules: `llm/{transport,fake,schema_dry_run}.py`, `profile/{storage,estimate,operations,vectors}.py`, the `settings/` package (§4.2 puts the settings logic in `api/routes/settings.py`; the save and the scope apply are worker- and API-shared domain code), `queries/`, `quota/cells.py`, `sources/spec_adapters.py`, `api/pagination.py` and `api/routes/{queries,watchlist,geo,spend}.py`.
   - Also new: `scripts/spikes/{record_anthropic,v13_structured_stream,v19_token_bound}.py`, `scripts/check_llm_boundary.py` and `tests/{payload,docparse}/`.
5. **`llm_ledger_unsettled` is its own reconciler.** §17.4's `ledger_unsettled` row covers quota and spend entries, and M1 built the quota half. The LLM half has the same conditions and actions, and updates the `llm_budget` counters in the same transaction. It runs on the same cadence (tick) under its own name, so a failure of one half never hides the other.
6. **`llm.unexpected_stop_reason`** (warn) is a code §17.6 lacks. §10.5 asks for "a warn alert" on an unexpected `stop_reason` without naming it. M5's catalogue adds it.
7. **Encryption and page count are checked by `docparse`, not by the upload** (§12.1 lists them as upload checks). Parsing PDF structure inside the API would put a PDF parser in the networked process that §12.2 keeps sandboxed. The API checks size, magic, extension and the ZIP structure; an encrypted PDF or one with more than 20 pages fails the draft with `failed('encrypted')` or `failed('too_many_pages')` within seconds, before any LLM call. The native-PDF path validates in docparse too (`check_pdf`), and such a draft offers only "paste text", since a native send would bypass §12.1. User-visible: the rejection arrives on the draft, not on the upload response.
8. **Native-PDF evidence is `unverifiable`.** §9.5's grounding needs a transmitted text, and a native PDF has none the worker can read without the parser it was sent around. Every evidence of a native-PDF profile is reported `unverifiable`, which does not block confirm. The contact scan of §12.7 still runs.
9. **Spikes split with M3, and V19's one paid call.**
   - V5's 20 real streamed generator and critic calls and V14 (the cache) need M3's prompts.
   - M2 runs V5's local timeout part, V4, V11, V13 and V19 within ≤ 8 paid messages and ≤ $0.80 of worst-case reservations, at an expected actual spend of about $0.15. The roadmap's "< $1" counts actual spend, and the rest is M3's.
   - §22 says V19 makes "no new paid calls". Its native-PDF criterion needs a recorded native-PDF usage, and none exists, so M2 makes exactly one native-PDF extraction of a synthetic one-page PDF. Without it the native path stays disabled (§12.7).
   - While `bounds.NATIVE_PDF_ENABLED` is false, a native opt-in upload answers 422 `validation_failed` on `native_pdf_opt_in` and stores nothing, a native retry answers 409 `conflict_state`, and `repairs` never offers `native_pdf` (Tasks 9 and 14). User-visible.
10. **Activation without targets** (supersedes M1 Carry-forward 9).
    - §12.6 (b), (c), (g), (h: `embed_profile`, `propose_queries`) and (i) are complete. (d) cancelling the old tuple's targets, (e) the batch cancellation, (f) the bulk operation and `create_targets_for_tuple` need M3's tables.
    - The `llm_tuple` effect class of a settings save does the same subset.
    - The collect graph's node 9 is a no-op while `enqueue_targets.SCORE_TARGETS_ENABLED` is false, and `finalize`'s I5 passes with a tuple set and records `"targets": "m3"`. M3 sets the switch and fills both.
    - The `admission` class's expiry sweep needs targets (M3).
    User-visible: a confirmed profile shows no re-scoring before M3.
11. **No score lane to pause.** `llm.reservation_bound_violated`, `llm.auth_failed` and `llm.cap_reached` "pause the score lane" (§10.4, §10.7, §6.9.4), and M2 has no score lane. M2 raises the first two where they arise. M3's admission reads them as its pause condition, and raises the third with `budget_release`.
12. **Scope apply without `det_results` and targets**, and the step-5 reconciler.
    - The final check of §13.4 step 3 covers `hard_filter_results`. The `det_results` clause is vacuous until M3 creates the table, and the `filtered_out ⇄ pending` target moves are M3's.
    - §13.4 step 5's "one transaction" runs in the tick reconciler `scope_apply_failed`. The permanent failure can happen in the stale reclaim or the takeover sweep, which hold the operation row, and `active_config` must be locked first (§9.1).
    - The clearing therefore lands within a minute of the failure.
13. **Operation failure hooks.** `register_operation_handler(kind, *, on_failed=…)` (Task 11) runs a kind's hook in the transaction that ends its operation `failed`, whether by its own finish, by a requeue or reclaim that exhausts the attempts, or by the takeover sweep. A hook may lock only rows after the owner row in the §9.1 order. The profile draft and the query proposal use it; the scope apply cannot (Deviation 12).
14. **Effective scope with `profile_relative` seniority.** `derive.derived(settings, profile=…)` resolves `profile_relative` into the explicit range with the active profile, at every save and at confirm (§12.6 g).
    - `settings_versions.scope_version_id` records the scope derived at save time. After a later confirm, `active_config.scope_version_id` may differ from it, and the active pointer is the truth.
    - Retry-apply derives again with the active profile.
    - Without a profile, the derived scope keeps `profile_relative` and the M1 filter passes (M1 Deviation 8).
15. **Internal settings saves.** Editing a query, adding a watchlist company and removing one write a new settings version of class `collection`, in the route's own transaction (entries keyed `query_id` and `watchlist_company_id`, §13.1). Each changes no quota quantity (a swapped id, or a free ATS source), so it copies the active version's `acknowledged_warnings` instead of asking again; field validation still runs.
16. **`projected_critic_rate` of new evaluation policies** is the §21.2 central 0.450 until M3's replay of §11.7.4 has drafts to replay.
17. **Prompt-schema version 1** holds the M2 templates, `profile_extract` and `propose_queries`, and the four schemas. M3 ships its generator and critic templates as version 2, activated through §13.3's shipped-version flow.
18. **Supersession of M1 Carry-forward 9.** M2 does activate a tuple, because §12.6 requires it; Deviation 10 keeps the collect graph safe.
19. **Blocking validation issues at confirm.** §12.6 shows the issues inline and says nothing about which ones block. The issues that make the stored profile wrong block confirm: `limit`, `date_invalid`, `date_order` and `code_other`. The rest are warnings the user may accept: `ungrounded`, `unverifiable`, `years_mismatch`, `language_without_evidence` and `contact_removed`, whose value is already gone. User-visible.
20. **Query proposal limits in two levels.** §11.12's limits are applied per item (an item that breaks one is dropped and counted) and per set (fewer than 6 items or no exploration item retries the call once). A set over 16 keeps the first 16. User-visible: a proposal may show fewer items than the model returned.
21. **CV file retention runs daily only.** §20.1 says "`retention_daily` / on confirm". A file deletion cannot join the confirm transaction, so the daily rule removes the files of versions older than the previous one.
22. **`eval_runs` in M2** has only `kind='schema_dry_run'` rows. The golden and canary runs are M3's.
23. **The `profile_vectors` reconciler** (heavy) is not in §17.4's table. It implements §12.6 step 3's "recomputed when it returns" for the profile side, as `awaiting_embedding` does for revisions.
24. **Online `dispatched` entries and late settlement.**
    - §17.4 and §7.16.7 turn an online `dispatched` entry into `ambiguous` after 15 minutes, while §13.1 allows node deadlines up to 1,800 s: `extract_profile`'s default is 900 s. M2 waits `max(15 min, the node's deadline + 5 min)`.
    - A settlement that arrives after the reconciler moved the entry to `ambiguous` uses §7.16.7's `ambiguous → billed | not_billed` edges for online calls too, and two new `llm_calls` edges, `ambiguous → completed | error`. The counters change in the same transaction.
    - No reservation is ever dropped or charged twice.
25. **Profile-graph exceptions are sanitised before LangGraph sees them.** LangGraph stores a node's exception in `checkpoint_writes`, so §16's "no CV text in a checkpoint" needs every profile node wrapped (`sanitized`). Only exceptions whose text is a code pass through; any other becomes `NodeFailed(node, error_class)`. Diagnosis then relies on the logged error class and the operation's `last_error_code`.
26. **Receipts of paid profile calls.** §9.1's receipt rule is applied to the `extract_profile` and `propose_queries` calls.
    - The receipts are the completed calls recorded under the operation's id in `input_refs`, per `logical_call_no` (1 the call, 2 its one repair; `llm_calls.logical_call_no` is otherwise a scoring column).
    - A resumed or reclaimed execution reuses them instead of paying again, and never repeats or skips the repair.
    - Every handler's last domain write finishes its operation in the same transaction (`OperationContext.finish_in`), so a draft or proposal never moves to a later generation while an older operation is still live.
    - A new approval or retry is a new operation.
27. **Thread names carry the operation.** §9.5 names the threads `profile-<draft_id>-t<n>` and `-p<n>` without defining `n`. M2 uses `n = <operation_id>-a<attempts>`, and `queries-<proposal_id>-q<operation_id>-a<attempts>` for proposals. Successive operations of one draft or profile then never share a thread, and retention deletes a thread by its own operation's end.
28. **The LLM nodes' graph timeout is the maximum, and the deadline is enforced per call.** §9.1 compiles the graphs once per process, while an `execution` save (Task 17) changes a node's `deadline_s` at runtime, so a compiled `TimeoutPolicy` cannot carry the active deadline. The LLM nodes compile with `TimeoutPolicy(run_timeout=1800 + 60)`, the §13.1 maximum `deadline_s` plus a margin, and the active `deadline_s` is enforced per call through `CallContext.deadline` (§10.2 `deadline = ctx.deadline`), from the execution config the node reads (Tasks 11, 13 and 20). No behaviour change within the configured deadlines.
29. **Text edits are sent as one complete set.** §14.2's `PUT /api/profile/drafts/{id}/text` takes `{span_edits[]}` without saying whether the list is a delta. Only `cv_files.stripped_text` and the counts persist, so after a removal or a second restore the server could not tell which replacements were already restored. The request therefore carries the draft's complete edit set, with replacement indexes into `strip_contacts(raw)` and removal spans in original-text offsets, and the server rebuilds `stripped_text` deterministically from `strip_contacts(decrypted raw)` with `restore` and `remove_span` (Tasks 10 and 14). User-visible: a client resends every edit it wants kept; an edit left out of the set is undone.
30. **Two ledger CHECKs refuse NULL evidence** (Task 1 review). Under SQL's three-valued logic the verbatim §7.11 CHECKs `state <> 'not_billed' OR (settled_usd = 0 AND evidence IN (…))` and `state <> 'billed' OR settled_usd <= reserved_usd OR evidence = 'reservation_bound_violated'` both pass a row whose `evidence` is NULL, so an unexplained `not_billed` or a silent bound violation would be stored. Migration 0006 adds `evidence IS NOT NULL` to the first and uses `IS NOT DISTINCT FROM` in the second. The CHECKs refuse only rows the spec already calls invalid; no behaviour change.
31. **The SDK's environment is shut out of the gateway client** (Task 4 review). §10.1 G1 names `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_URL` and `ANTHROPIC_API_KEY`, but the pinned SDK also reads `ANTHROPIC_CUSTOM_HEADERS` (merged into every request; an `x-api-key` there replaced the explicit key), the `ANTHROPIC_OPEN_TELEMETRY*` variables (spans that can carry content) and `ANTHROPIC_LOG` (debug logging of whole request bodies). The gateway client therefore sends exactly its own headers, turns SDK tracing and trace propagation off, and the `anthropic`, `httpx2` and `httpcore2` loggers are held at WARNING with no handlers of their own. A new startup assertion, `anthropic_log_in_prod`, refuses `ANTHROPIC_LOG=debug` or `info` in prod, beyond §16.8's list. No behaviour change for a correctly configured deployment.
32. **Rubric v1 and prompt-schema version 1 are seeded, not migrated; the pointers stay nullable until M3** (Task 6). Task 6's text had migration 0007 insert both rows and restore the NOT NULL of `active_config.rubric_version_id` and `prompt_schema_version_id` (M0 Deviation 1). §19.3 step 2 forbids migrations from inserting rows into identity tables (seeding is step 6, after the identity advancement). So `seed_defaults` inserts both rows and sets both pointers on every `migrate`, on a fresh install and on an upgrade alike, and a test pins that every `migrate` leaves both pointers set. The NOT NULL moves to M3's first migration, one release after the seed that populates it (expand/contract). No behaviour change.
33. **V4: the API rejects the §11.5 generator schema** (Task 7). The first V4 case, the generator schema on `claude-sonnet-5-5`, got HTTP 400 `invalid_request_error`, "The compiled grammar is too large…"; `messages.count_tokens` does not catch it. The `minimum` probe was answered by `count_tokens` for free (400, `minimum` not supported for integers), so no paid probe is sent, and the dry-run's probe case uses `count_tokens` and never gates `passed`. The schema dry-run keeps the generator case and reports it failed (the fake replays the recorded 400), so prompt-schema version 1's dry-run is `passed = false`, and `jobhunter schema-dry-run` exits non-zero until M3 ships a compilable generator schema as prompt-schema version 2. M2 makes no generator call, so no M2 behaviour changes. Making §11.5's output compilable is a spec change for M3's plan, put to the user with it.
34. **The default `scoring.batch_max_items` is 500** (Task 7, the user's decision on 2026-10-21; spec revision 3.4). V19 measured `PROMPT_OVERHEAD_TOKENS` at 2,640 tokens for every model (the doubled largest framing, from the generator-shaped requests). With that, the §6.9.1 overrun-buffer bound for the seeded settings was $3.37 at 1,000 batch items, above the $2.00 default. The plan's stop rule halted Task 7, and the user chose to lower the batch limit, keeping the $2.00 buffer and the $93.10 scoring share. The bound is now about $1.72. `settings_v1.json` and spec §6.9.1, §10.10 and §13.1 say 500. §21.4's sizing (about 350 ads in a first batch) is below the limit, so batch behaviour is unchanged in practice. A database seeded before this change keeps 1,000 in its settings version 1, and its first settings save must lower it (no production database exists yet).
35. **Contact stripping goes past §12.3's table where docparse's output or the review edits need it** (Task 10). A birth or protected-attribute label that stands alone on its line (only whitespace, punctuation, label words or a format mask such as `(TT.MM.JJJJ)` or `(M / W / D)` after it) also takes the next non-empty line, because docparse emits DOCX table cells one per line. The header-name rule skips headings (`Name`, `Persönliche Daten`), reads `Name:`/`Vorname/Nachname:` labels, and may strip more than one header name; title lines (role keywords, `Leiter`/`Kaufmann` as a first token or after an inflected adjective, `-in` forms with a stem of four letters or more) are never names. Words that are never streets are listed explicitly (`ENGLISH_RING_WORDS`, a few abstract `-weg` nouns), so any other `-ring`/`-weg` word with a house number is stripped. Edits are `Edits(restored, removed)` spans into the original text, `apply_edits(original, edits)` rebuilds `stripped_text`, and `restore`'s index refers to a fresh `strip_contacts(original)`; restoring a `[PERSONAL]` span keeps the e-mails, phones, URLs, addresses and names inside it replaced, so Task 14's `PUT /text` takes restore spans as well as indexes, and offers no restore control for an inner replacement whose span equals a personal span. All of this errs toward stripping; the user's text review remains the guarantee (§12.3).
36. **Profile validation adds limits §12.4 leaves open** (Task 12). `validate_user_entered` reports `limit` for more than 10 clearances, a blank clearance name, and a name or level over 60 characters (the ad-side `security_clearance.level` bound, spec §11.5), so these block confirm in Task 21. Profile evidence shorter than §11.6 V3's quote floor (fewer than 4 characters or fewer than 3 letters or digits), and blank skill evidence, is `ungrounded`, a warning that never blocks confirm. A list item dropped for contact data is reported at its list's path, so every issue path indexes the returned profile. Task 21's confirm also scans `user_entered` with `contacts.scan_value` and refuses a hit with 422 `validation_failed` on the field, so `profile_versions.profile_json` stays contact-stripped (§7).
37. **The draft stores its text edit set** (Task 14). Pre-flight F29 made `PUT /text` take the complete edit set and rebuild `stripped_text` from the raw text, but left the set unstored, so a reloaded review page would resend it without the earlier removals and a span the user removed would reappear. Migration 0007 adds `profile_drafts.text_edits jsonb NOT NULL DEFAULT '{"restored": [], "removed": []}'` (offset pairs into the raw text, no content; a CHECK keeps both lists). `PUT /text` validates the complete set and stores it with the rebuilt `stripped_text` in one transaction; `GET` returns it with `segments`. A new draft starts empty, `ready → text_review` keeps the set, and the §20.1 purges leave it (offsets only). If a later change to the stripping rules leaves a stored restore span without a replacement, `GET` returns `segments: []` and the stored set, and a resend answers 422 naming the stale entries for the client to drop. User-visible: no.
38. **The settings model stores one normal form and checks what the run view needs** (Task 15). `Settings.to_payload()` is the stored form and the form every diff compares: excluded companies become company keys (§13.1 "stored as company keys"), industries are case-folded, USD amounts keep 2 decimal places, and the two rates (`t2_straddle_max_coverage`, `bulk_rescore_max_budget_share`) take at most 2 places with trailing zeros dropped, so a formatting change never derives a new row. Beyond §13.1, a location label must give a location key, location keys (with `remote-de` reserved), query ids and watchlist ids are unique, a company or industry repeated after normalising is a `conflict`, and Adzuna caps are at least 1. A resolved `profile_relative` scope keeps `below` and `above` next to `min` and `max`. The overrun-buffer bound is one function (`validate.overrun_buffer_bound`) that the V19 spike also uses: $1.72 at the defaults, $33.07 at 10,000 batch items. User-visible: the normal forms and the extra checks.

## Carry-forward to later milestones

1. **M3:**
   - a compilable generator output schema (Deviation 33: the API rejects the §11.5 schema's grammar as too large), as a spec change approved with M3's plan, shipped as prompt-schema version 2 and verified by the live schema dry-run before any scoring call; activation requires a `schema_dry_run` eval run with `passed = true` and `metrics.transport = "live"` for that version, and `PROMPT_OVERHEAD_TOKENS` is re-measured against it (V19's free `--count`/`--analyse`), because 2,640 came from the rejected generator schema;
   - restore `NOT NULL` on `active_config.rubric_version_id` and `prompt_schema_version_id` in M3's first migration (Deviation 32);
   - the FKs of Deviation 1;
   - V5's paid part and V14;
   - §12.6 (d)–(f) and `create_targets_for_tuple`, with `SCORE_TARGETS_ENABLED`, node 9 and I5 (Deviation 10);
   - the score-lane pause on the three alerts and `budget_release` (Deviation 11);
   - the bulk budgets (§6.9.3) on the ledger's `bulk_operation_id`;
   - the eval gate with golden cases (`eval_gate_state` stops returning `"uncalibrated"`);
   - the generator and critic templates as prompt-schema version 2, and the shipped-version activation routes `/api/settings/{rubric,prompt-schema}-versions/{id}/activate`;
   - `det_results` in the scope apply's final check and the target moves (Deviation 12);
   - `count_published_results_active_tuple`, and `ĉ` and `critic_rate_7d` from target history in the LLM projection and `/api/system/spend`;
   - the `admission` expiry sweep;
   - the cache monitor's first prefix-cached calls.
2. **M4:**
   - the Profile & Settings page (§15.7): the text review, the native-PDF disclosure, the dry-run panel with its acknowledgements and the "Applying settings… (n of N)" banner;
   - `/api/queries/stats`;
   - the job sorts of `api/pagination.py`;
   - the "Re-scoring pending" display.
3. **M5:**
   - the alert-catalogue dispatch for the new `llm.*`, `scope.apply_failed` and `operation.failed` codes, `llm.unexpected_stop_reason` included (Deviation 6), and `profile.extract_generation_mismatch` (warn, Task 13: an extraction met a draft still `extracting_profile` in another generation; the draft holds the one open-draft slot until it is repaired by hand, so the runbook entry says how to fail it);
   - the dead-man period update on `schedule` saves;
   - the profile purge (§20.2), which nulls `profile_json`, `query_proposals.items` and the vectors;
   - LangSmith with hidden I/O (§17.8);
   - `docs/PRIVACY.md`'s Anthropic and native-PDF statements.

## Pre-flight corrections

The binding pre-flight rulings F1–F39 (`.superpowers/sdd/2026-10-20-m2-llm-profile-settings/preflight.md` §3, every ruling accepted as written; F33 as clarified: keep `v1.yaml`, pin `pyyaml==6.0.3` and a dev-group `types-PyYAML`) are applied in the task text above. Each line names where.

- **F1** (Task 17 creates `scope_apply.py`): Task 17 Files (Create `settings/scope_apply.py`), Consumes (`create_operation`), Produces (`KIND`, `dedup_key`, `request_apply`, `set_desired_scope`), the `scope` class action and the scope test; Task 18 Files (Modify `scope_apply.py`), Consumes (the four from Task 17), Produces (the handler, `incomplete_jobs`, the reconciler, the alert constants) and Step 3 (`_REGISTERING_MODULES`).
- **F2** (the operation-context plumbing): File Structure (the `worker/…` row defers to Task 11); Task 11 Files (with `test_operation_wiring.py`, `test_watchlist_validate.py`, `test_fenced_admission_redefers.py`), Produces (`OperationContext(claim, engine, deps=None)` with its outcome holder, `finish_in`, `requeue_in`, `execute_operation(…, deps, registry, heartbeat_period_s, on_superseded)`, `deps=rt.operation_deps`, the `configurable["op"]` invoke shape, `ProfileDeps` process-wide only), the handler and Steps 3–4; Task 5 C35 (the test handler runs with `deps=None`); Tasks 13, 18, 19 and 20 (`ctx.requeue_in`, the claim from `configurable["op"]`).
- **F3** (`CallContext` and `reserve`): Task 3 `reserve(…, native_pdf_extra: bool, …)` replacing `extra_usd`, its step 4 and test; Task 5 Files (`llm/errors.py`), `CallContext.logical_call_no`, `CallContext.native_pdf: bool`, `errors.InputUnbounded`, the count path and its tests; Task 13 Consumes and the `InputUnbounded → failed('input_unbounded')` mapping. The recorder keeps `pricing.worst_case_usd(…, extra_usd=…)` (Global Constraints, Task 7).
- **F4** (synthetic streams): Task 13 Files and Step 1 (`SYNTHETIC_CV`, the hand-authored `extract_profile/claude-opus-5-5/<schema12>.sse`, `tests/support/llm.synthetic_sse`); Task 20 Files and Step 1 (the `propose_queries` stream); Task 6 bound check (real recordings only); Task 4 V13 (the §11.12 schema literal, recordings for V13 and `test_fake_llm.py` only); Task 7 `test_schema_dry_run.py` (the V4 recordings of Step 4); Task 24 Step 1.
- **F5** (`fake.scenario(*names)`): Task 4 `RecordedSSETransport` (per-try names, `ok`, the sequence rule, `fake.count_tokens`, `enum_stripped`, `set_invalid`) and `test_fake_llm.py`; Task 5 `test_gateway.py` and C15; Task 7 Consumes; Tasks 13 and 20 Consumes and tests. No later task adds a variant, so none lists `llm/fake.py`.
- **F6** (`run_one` and the pause points): Task 11 `op_runner.run_one(op_id, *, pause, registry)` with the patched modules, and `profile.text.after_text_review_commit`; Task 13 `profile.extract.after_call`, `profile.draft.before_tx` and the fresh registry; Task 18 Consumes and the helper note; Task 20 `queries.propose.after_call`.
- **F7** (failure-hook inputs): Task 11 Files and the failure hooks (`FAILURE_HOOKS`, `kind, payload` in `_STALE`, `_OLDER_EPOCH` and `_OWNED_ATTEMPTS` for `_release`, the text hook's `uploaded → extracting_text → failed(code)`) and `test_failure_before_the_first_transition_frees_the_upload_slot`.
- **F8** (registration pins): Task 5 Files and Step 3 (`jobhunter.llm.gateway`, `llm_refusal_rate: ["heavy"]`); Tasks 13 and 20 Files and Steps 3–4 (`test_worker_main.py`, `test_profile_graph_shape.py`); Task 23 worker registration (no `queries` module).
- **F9** (M0/M1 tests): Task 1 Files (`_CONFIGURATION_RESET` also deletes unnamed `rubric_versions` and `prompt_schema_versions` rows) and Test (`DIRTY_CONFIG` inserts a real `cv_files` → `profile_drafts` → `profile_versions` → `llm_tuples` chain; `test_two_resets_keep_active_config`); Task 6 Test (`test_schema.py`, `test_schema_m1.py`, `test_migrate.py` flipped to the seeded ids) and Step 4.
- **F10** (support helpers): Task 3 creates `tests/support/llm.py` (`op_owner`, `reserve_kw`, `completed_call`, `budget_rows`) and registers `tests.support.llm` in `tests/conftest.py`, because it is the first task that needs the module, and Task 4 then modifies it; Task 5 (`fake_llm`, `create_llm_call_operation`, `reclaim_stale_now`, `ledger_states`); Task 8 (registers `tests.support.profile`); Task 9 (`docx_bytes(…, declared_expansion=)`, `count_rows(engine, table, **filters)`, `cv_store_dir` used by `api_app`); Task 11 (`op_runner`, `upload`, `draft_row`); Task 14 (`idem`, `failed_low_text_draft`, `draft_row_api`); Task 17 (creates and registers `tests/support/settings.py`; `raising_at`, `no_fault`, `SimulatedCrash` in `tests/support/faults.py`); Task 18 (the settings and sync helpers, `run_reconciler(name, engine, *, now, epoch)` in `tests/support/reconcile.py`, `JobFactory.many(n)` in `tests/support/collect.py`, `utcnow()` for `clock.now()`, `proc.signal(signal.SIGTERM)`); Task 21 (`ready_draft`, `active_profile`, `ranking_generation`).
- **F11** (`EvalRunOwner` and `eval_runs`): File Structure; Task 1 (the fifth spec `eval_runs`, its trigger, the five-spec pin); Task 3 (`EvalRunOwner` created there, the abandoned run failed through `transition()`); Task 7 Files (no `spend_ledger.py`), Consumes and the `eval_runs` end.
- **F12** (`lock_month` parameters, `read_month`): Task 3 `lock_month(conn, month, p, *, now)`, `read_month(conn, month, p, *, now)` and its test; Task 16 Consumes, the dry-run's LLM figures, `/api/system/spend`, the health `llm` section and the no-lock test.
- **F13** (lock order in `reserve`): Task 3 `reserve` steps 0–3 (`current_params` → owner lock → `ensure_month` → `lock_month`).
- **F14** (`active_config FOR UPDATE` before `run_idempotent`): Task 17 `POST /api/settings`, `save_settings` step 1 and the concurrent-saves test; Task 21 `POST /api/profile/drafts/{id}/confirm`.
- **F15** (one bump after the class actions): Task 17 the `llm_tuple` action, the paragraph before step 7 and the `llm_tuple + budget` test.
- **F16** (`warnings_only`, `llm_rescore`): Task 15 `warnings_only(settings, *, gate)` with `eval_uncalibrated` and `serpapi_disabled_no_fallback`; Task 16 Consumes, the `dry_run` composition (`llm_rescore` as a warning, `scoring.bulk_rescore_max_budget_share`) and the warning test. Task 21's estimate already used the share.
- **F17** (no extraction without approval): Task 13 the `extract_profile` handler and the obsolete-state test.
- **F18** (retry predicate and repairs): Task 14 `repairs`, the `mode="llm"` predicate and their tests; Deviation 3.
- **F19** (native-PDF CHECK and V19 gate): Task 9 Consumes, `open_new_draft` step 4, the 422 on `native_pdf_opt_in` and its test; Task 14 the retry's 409 and its test; Task 13 `failed('native_pdf_disabled')`; Task 7 V19; Deviation 9.
- **F20** (receipts of billed terminal calls): Task 5 the `error_code`s of completed calls and the tests; Task 13 the receipts match on `input_refs->'owner'`, hex hashes, the terminal-code receipt and `unexpected_stop_reason` without a repair; Task 20 the same.
- **F21** (`owner_cols`): Task 1 `TRANSITIONS["profile_drafts"]` and the registry pin; Task 13 the `draft` node's `transition(owner={…})`.
- **F22** (compile-once deadline): Task 11 `build_profile_graphs` (`TimeoutPolicy(run_timeout=1800 + 60)`); Task 5 `CallContext.deadline`; Tasks 13 and 20 the per-call deadline; Deviation 28.
- **F23** (timeout injection, base-URL test): Task 4 `make_http_client`, `_chat(…)` with `timeout=http_client.timeout`, the transport mount on `https://api.anthropic.com` in `test_llm_transport.py`, and V5's record of the honoured variant.
- **F24** (boundary check): Global Constraints (Boundary); Task 5 `test_llm_boundary_check.py` and Step 3.
- **F25** (docparse root, spawn, group): Task 8 (`docparse/__init__.py`, `serve(root, *, stop)`, `main()` on `JOBHUNTER_PARSE_JOBS_DIR`, `get_context("spawn")`, `cleanup_leftovers` on a missing root); Task 11 (`AppConfig.parse_jobs_dir`, Steps 2 and 4 with `uv run --group docparse`); Task 13 Steps 2 and 4; Task 18 the C25 note; Task 23 Step 4; Task 24 the crash test's root and Step 3's suite command.
- **F26** (`OperationClaim.attempts`): Task 11 Files, `OperationClaim.attempts` read in `_CLAIMABLE`, `thread_id` from the claim; Architecture (the thread names).
- **F27** (Task 13's interfaces): Task 2 `pricing.CONTEXT_WINDOW`; Task 13 Files (`llm/spend_ledger.py`, `llm/gateway.py`, `graphs/profile/nodes/check_pdf.py`, `tests/unit/test_worker_main.py`, `tests/unit/test_profile_graph_shape.py`, `tests/support/profile.py`); File Structure.
- **F28** (`ConfirmedProfile` shapes): Task 6 `user_entered: UserEntered = Field(default_factory=UserEntered)`; Task 19 `vector_texts` (`target_titles[i].title`).
- **F29** (complete edit set): Task 10 `restore` and `remove_span` on original-text offsets, and the composition test; Task 14 `PUT /api/profile/drafts/{id}/text` and its test; Deviation 29.
- **F30** (V19 without Task 15): Task 7 V19 (the §6.9.1 sum inline; Task 15's `test_seed_defaults_validate` the standing guard; the stop-and-report rule kept).
- **F31** (monitor and refusal-rate tests): Task 5 Test files and Step 1 (`tests/unit/test_cache_monitor.py`, `tests/integration/test_llm_refusal_rate.py` with `assert_fence_rule`).
- **F32** (metrics after commit): Task 3 Files, `settle(…) -> SettleFigures`; Task 5 the metric update after the settlement commits, and its test.
- **F33** (PyYAML, as clarified): Task 6 keeps `scoring/rubric/v1.yaml`; Files (`pyproject.toml`: runtime `pyyaml==6.0.3`, dev-group `types-PyYAML` at the version resolved at `uv lock`; `uv.lock`), `load_rubric`, and Steps 4–5 (the version recorded in the commit message).
- **F34** (Task 23 Files): Task 23 Files (the importers of the moved names, `worker/tasks.py` passing `cv_store=rt.cv_store`, `llm_outcome.py` with the two nodes, `run_retention_daily(…, *, cv_store, now, epoch)` and its two M1 test callers) and Step 3 (`retention.cv_files.after_select`).
- **F35** (dependency drift): Task 11 `OperationDeps.embedder: EmbedderClient | None` and "this task is the definition"; File Structure; Task 19 the `None` requeue; Task 9 `crypto.data_keys(cfg)`, wrapped by `api/deps.get_data_keys`; Task 11 Consumes.
- **F36** (payloads, the opener): Task 9 `upload.request_draft_operation` (payload `{"draft_id": <id>}`, dedup keys `extract_profile_text:<id>` and `extract_profile:<id>`), `upload.open_new_draft` (it discards every `failed` draft) and the tests; Task 14 approve and retry; Tasks 11 and 13 the handlers read `payload["draft_id"]`.
- **F37** (documentation drift): Task 21 Consumes (`scope_apply.set_desired_scope`, Task 17); Deviation 3 (`input_url`); Task 23 worker registration.
- **F38** (payload bound check): Task 4 the recorder (component byte sizes next to each stream); Task 6 the payload tests (`_chat` and `bounds` with a capturing transport, no database).
- **F39** (shared logic): Task 17 `save.ensure_tuple`, reused in Task 21 step 7; Task 18 `filters.STALE_PREDICATE` for `incomplete_jobs`; Task 13 `graphs/profile/nodes/llm_outcome.py`, reused by Task 20; File Structure.

Nothing else in the plan changed. The Architecture, Global Constraints and File Structure lines named above restate the corrected definitions; Deviation 3 gained two lines (`input_url`, `repairs`), Deviation 9 one (the disabled native path), and Deviations 28 and 29 are new.

## Closing note: spec coverage of the M2 sections

Section by section, the task that implements it; the items deliberately left to later milestones are listed with their reason.

- **§5.5.1:** Task 20.
- **§6.8:** Task 16. **§6.9.1–6.9.2:** Task 3, and the cap edit in Task 17. §6.9.3–6.9.4 are M3's (Deviations 10 and 11), except the bulk budget figure of the confirm preview (Task 21).
- **§7.3 (`llm_tuples`), §7.4, §7.5 (`query_proposals`), §7.10 (`profile_vectors`), §7.11 (the LLM tables), §7.13 (`eval_runs`), §7.14 (the `profile` branch) and §7.16.7–7.16.8 (the M2 machines):** Task 1.
- **§9.5:** Tasks 11, 13 and 20.
- **§10.1–10.2:** Tasks 4 and 5. **§10.3:** Tasks 5 and 15. **§10.4–10.5:** Task 5. **§10.6:** Tasks 3 and 5. **§10.7:** Tasks 2 and 7. **§10.8:** Task 5. **§10.9:** Task 6. **§10.11:** Task 7. §10.10 (Batches) is M3's.
- **§11.2–11.3 (rubric v1 as data):** Task 6. **§11.12:** Task 20. **§11.13:** the uncalibrated gate in Tasks 15 and 17; the gate itself is M3's.
- **§12.1:** Task 9 (Deviation 7). **§12.2:** Task 8. **§12.3:** Tasks 10, 11 and 14. **§12.4–12.5:** Tasks 6 and 12. **§12.6:** Tasks 19 and 21 (Deviation 10). **§12.7:** Tasks 13 and 14.
- **§13.1:** Task 15. **§13.2–13.3:** Task 17. **§13.4:** Task 18. **§13.5:** Task 16. **§13.6:** Task 15 (the fields; the scoring use is M3's).
- **§14.1–14.2 (the M2 rows):** Tasks 9, 14, 16–18 and 20–22.
- **§17.4 (the M2 reconcilers):** `budget_counters` and `llm_ledger_unsettled` (Task 3), `llm_refusal_rate` (Task 5), `scope_apply_failed` (Task 18) and `profile_vectors` (Task 19). **§17.6 (the M2 codes):** raised where their condition arises (Tasks 3, 5, 18); the ntfy dispatch is M5's.
- **§18.1–18.2 (the M2 items):** Tasks 2, 3, 10, 12, 15 and 16, plus N1's docparse half in Task 8 and N4 in Task 16. **§18.3:** C15 and C35 (Tasks 5 and 13), C24 and C34 (Task 21), C25 and C30 (Task 18). **§18.5:** Tasks 6 and 7.
- **§20.1 (the M2 rows):** Tasks 19 and 23.
- **§21:** Tasks 16 and 21 (the projection and the estimate).
- **§22:** V4, V13 and V19 (Tasks 4 and 7), V5's local part (Task 4) and V11 (Task 5); the rest per Deviation 9.
- **Deferred beyond the M2 row on purpose:** the score lane and targets (M3), the UI (M4), purges, alert dispatch and LangSmith (M5).
