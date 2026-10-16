# Technical facts for the job-hunter architecture (verified 2026-10-16)

Scope: single-user job matcher. It collects German job ads on a schedule, deduplicates them, pre-filters them with local multilingual embeddings, and ranks each ad against one CV with LangChain/LangGraph agents on Claude. Postgres, a two-page web UI, one VPS, Docker Compose.

**Evidence labels used below**
- **[RUN]**: I ran it in this container (scripts in `bench/`).
- **[SRC]**: I read it in the installed package source or tarball. File and line are given where useful.
- **[REG]**: taken live from the PyPI JSON API, the npm registry, the HF Hub API, Docker Hub or the ghcr.io registry on 2026-10-16.
- **[DOC]**: from official docs fetched today (URL given).
- **UNVERIFIED**: from memory, or a claim not checked today.

Environment for [RUN]: 4 vCPU "Intel Xeon Processor" with AVX-512, AVX512-VNNI and AMX flags, 15 GB RAM, no GPU, Python 3.13.16, Node 22.22. A typical VPS CPU (for example AMD EPYC without AMX) may be slower, especially for int8 or bf16 paths.

---

## A. LangGraph / LangChain (Python vs JS)

### A1. Versions and cadence [REG]

| Package | Latest | Released | License | Notes |
|---|---|---|---|---|
| `langgraph` (PyPI) | **1.2.15** | 2026-10-12 | MIT | 1.0.0 on 2025-10-17, 1.1.0 on 2026-03-10, 1.2.0 on 2026-05-12. Patch releases every 1–3 weeks (1.2.8–1.2.15 between Jul and Oct). 280 releases since 2024-01. |
| `langgraph-checkpoint` | 4.3.0 | 2026-10-12 | MIT | base saver, InMemorySaver, serde |
| `langgraph-checkpoint-postgres` | **3.2.0** | 2026-10-14 | MIT | needs psycopg ≥ 3.2 and psycopg-pool (not asyncpg) |
| `langgraph-checkpoint-sqlite` | 3.1.2 | 2026-10-12 | MIT | |
| `langgraph-prebuilt` | 1.1.1 | 2026-10-15 | MIT | |
| `langgraph-sdk` / `langgraph-cli` | 0.4.7 / 0.4.35 | 2026-10 | MIT | only needed for the Server/Platform |
| `langgraph-api` (the server) | 0.16.1 | 2026-10-15 | **Elastic-2.0** | not needed for library use |
| `langchain-core` | **1.6.11** | 2026-10-15 | MIT | 1.0.0 on 2025-10-17. Patches every few days. |
| `langchain` | 1.4.6 | 2026-10-14 | MIT | `create_agent`, middleware |
| `langchain-anthropic` | **1.7.6** | 2026-10-12 | MIT | requires `anthropic>=0.120,<2` |
| `anthropic` (Python SDK) | **1.15.0** | 2026-10-15 | MIT | |
| `langsmith` | 0.14.11 | 2026-10-16 | MIT | |
| `@langchain/langgraph` (npm) | **1.5.0** | 2026-10-12 | MIT | 1.0.0 on 2025-10-18 |
| `@langchain/langgraph-checkpoint-postgres` | 1.1.0 | 2026-10-12 | MIT | uses `pg` ^8.12 |
| `@langchain/anthropic` | **1.5.12** | 2026-10-01 | MIT | depends on `@anthropic-ai/sdk` **^0.122.0**. On 0.x a caret means 0.122.x only, while the latest SDK is 0.135.0. |
| `@langchain/core` | 1.2.18 | 2026-10-15 | MIT | |
| `@anthropic-ai/sdk` | 0.135.0 | 2026-10-15 | MIT | |

Maturity: both LangGraph lines have been past 1.0 for a year. They release very often (pin exact versions and upgrade on purpose). The Python side gets features and Claude-model awareness first (see B3).

### A2. Python vs JS feature parity [SRC]

| Feature | Python `langgraph` 1.2.15 | JS `@langchain/langgraph` 1.5.0 |
|---|---|---|
| `StateGraph`, reducers (`Annotated[list, operator.add]`, `add_messages`) | yes | yes (Annotation / zod state) |
| `Send` map-reduce fan-out | yes [RUN] | yes (`Send` in constants.d.ts) |
| `Command` (update + goto), `interrupt()` / `Command(resume=)` | yes [RUN] | yes |
| Per-node `RetryPolicy` | yes. **The default `retry_on` retries Anthropic 400s** (see below) | yes. The default does **not** retry 400/401/402/403/404/405/406/407/409 (`DEFAULT_STATUS_NO_RETRY`, dist/pregel/retry.js) |
| Per-node `CachePolicy` (+ `compile(cache=...)`) | yes, backends `langgraph.cache.memory` and `langgraph.cache.redis` [RUN] | yes |
| Per-node `timeout` / `TimeoutPolicy(run_timeout, idle_timeout, refresh_on)` | yes (present in 1.2.15; the release where it was added was not checked) | yes (`timeout?: number \| TimeoutPolicy`) |
| Per-node `error_handler(state, error: NodeError)` and `set_node_defaults(...)` | yes [RUN] | yes (`errorHandler`, `setNodeDefaults`) |
| `TracePolicy(process_inputs/outputs)`, per-node trace redaction | yes | **not found** in 1.5.0 typings |
| Durability modes `"sync" \| "async" \| "exit"` | yes (default `"async"`) | yes |
| Functional API (`@entrypoint`, `@task`) | yes (`langgraph.func`) | yes (`entrypoint`, `task`) |
| Postgres checkpointer | `PostgresSaver`, `AsyncPostgresSaver`, `ShallowPostgresSaver`, plus `PostgresStore` | `PostgresSaver` (pg) |

### A3. Key APIs and semantics (Python, verified in `bench/langgraph_smoke.py` [RUN] and source [SRC])

- **Map-reduce:** return `[Send("score_ad", {"ad": a}) for a in ads]` from a conditional edge, and merge the results through a reducer field. [RUN] 10 `Send`s produced 10 results. With `config={"max_concurrency": 3}`, at most 3 ran in parallel (`max_concurrency` is read in `pregel/_executor.py`).
- **RetryPolicy** (`types.py` L427): `RetryPolicy(initial_interval=0.5, backoff_factor=2.0, max_interval=128.0, max_attempts=3, jitter=True, retry_on=default_retry_on)`. You can pass one policy or a sequence; the first one that matches applies.
  - **Gotcha [RUN]:** `default_retry_on` returns False only for ValueError, TypeError, LookupError, RuntimeError, OSError and similar, and for HTTP 4xx raised as `httpx`/`requests` errors. `anthropic.BadRequestError` is none of these, so a 400 **is retried** (3 attempts measured).
  - A custom `retry_on=lambda e: isinstance(e, (anthropic.RateLimitError, anthropic.InternalServerError, anthropic.APIConnectionError))` worked [RUN].
  - The SDK also retries on its own (`max_retries=2` default for 408/409/429/5xx), so the two layers multiply.
- **Timeouts:** `add_node(..., timeout=60)` or `TimeoutPolicy(run_timeout=..., idle_timeout=..., refresh_on="auto"|"heartbeat")`. Cancellation is cooperative (asyncio), so a sync node that blocks the GIL is not interrupted until it releases it [SRC docstring]. `NodeTimeoutError` is retryable by default.
- **Node error handling:** `add_node("x", fn, error_handler=handler)`, where `def handler(state, error: NodeError) -> dict | Command`. [RUN] The handler's update was applied and the run continued. `StateGraph.set_node_defaults(retry_policy=, cache_policy=, error_handler=, timeout=)` sets graph-wide defaults; subgraphs do not inherit them.
- **Node cache:** `add_node(..., cache_policy=CachePolicy(ttl=60))` plus `compile(cache=InMemoryCache())`. The key defaults to a pickle hash of the node input. [RUN] Two identical invokes ran the node once. Only memory and Redis cache backends ship in 1.2.15 (no Postgres cache).
- **interrupt / human-in-the-loop:** `interrupt(payload)` inside a node needs a checkpointer and a `thread_id`. The result carries `__interrupt__`, and you resume with `graph.invoke(Command(resume=value), cfg)` [RUN].
- **Durable resume [RUN]:** node `c` crashed in a superstep where `b` succeeded. `graph.invoke(None, same_thread_cfg)` re-ran only `c` (counts a=1, b=1, c=2), because pending writes of the successful sibling were kept.
- **Durability** (`types.py` L98): `"sync"` persists before the next step, `"async"` (default) persists while the next step runs, `"exit"` persists only at exit. Pass it per call: `graph.invoke(..., durability="sync")`.
- **Recursion limit:** `DEFAULT_RECURSION_LIMIT = int(getenv("LANGGRAPH_DEFAULT_RECURSION_LIMIT", "10007"))` in `langgraph/_internal/_config.py` L32. Note that `langchain_core` still defaults to 25. Override per call with `{"recursion_limit": N}`. [RUN] A limit of 50 raised `GraphRecursionError`.
- **Subgraphs:** add a compiled graph as a node. The per-subgraph `checkpointer` type is `None | bool | BaseCheckpointSaver`: inherit, own persistence, or none (`types.py`).
- **PostgresSaver** (`langgraph/checkpoint/postgres/__init__.py` L69–88):
  - `from_conn_string()` opens psycopg 3 with **`autocommit=True, prepare_threshold=0, row_factory=dict_row`**. Your own `Connection`/`ConnectionPool` must use the same settings.
  - Call `.setup()` once. It creates `checkpoint_migrations`, `checkpoints`, `checkpoint_blobs` and `checkpoint_writes`, and uses `CREATE INDEX CONCURRENTLY`, which needs autocommit.
  - `AsyncPostgresSaver` is in `...postgres.aio`. `pipeline=True` is available for a single connection.
- **Library-only use: yes.** `langgraph` (MIT) is a plain in-process library: `graph.invoke()` / `ainvoke()` / `astream()` inside your own worker, with no LangGraph Server and no LangSmith. Tracing turns on only when `LANGSMITH_TRACING=true` (see A4).
  - The server runtime (`langgraph-api`, **Elastic-2.0**) is a separate commercial product. "LangGraph Platform" was renamed **"LangSmith Deployment"** in October 2025 ([LangChain changelog](https://changelog.langchain.com/announcements/ann_LCRL3XWIdoDdb), [blog](https://www.langchain.com/blog/langgraph-platform-ga)).

### A4. LangSmith tracing and PII [SRC langsmith 0.14.11]

- Tracing is on only if `LANGSMITH_TRACING=true` (or `LANGSMITH_TRACING_V2`, or legacy `LANGCHAIN_TRACING_V2`) (`utils.py` `tracing_is_enabled`). **`LANGSMITH_API_KEY` alone does not enable tracing.** This container already has `LANGSMITH_API_KEY` set, which is harmless unless tracing is switched on.
- Other variables:
  - `LANGSMITH_API_KEY`
  - `LANGSMITH_PROJECT`
  - `LANGSMITH_ENDPOINT`: US default `https://api.smith.langchain.com`; **EU `https://eu.api.smith.langchain.com`**. The endpoint is not detected automatically, so EU keys fail without it ([LangSmith KB](https://kb.langchain.com/articles/6271700631-pointing-the-langsmith-sdk-and-cli-at-a-non-us-regional-endpoint), [quickstart](https://docs.langchain.com/langsmith/observability-quickstart)).
- **Keeping CV text out of traces:**
  - `LANGSMITH_HIDE_INPUTS=true`, `LANGSMITH_HIDE_OUTPUTS=true` and `LANGSMITH_HIDE_METADATA=true` are read in `client.py` L1399–1404 via `get_env_var("HIDE_INPUTS")` with the `LANGSMITH_`/`LANGCHAIN_` prefixes.
  - For finer control, use `Client(hide_inputs=callable, hide_outputs=callable, anonymizer=callable)`.
  - Per node: LangGraph `add_node(..., trace_policy=TracePolicy(process_inputs=omit_payload))`. Its docstring says it is "not intended to redact secrets" and does not cover child runs.
- `langchain-anthropic` also contains LangSmith-gateway logic. If `LANGSMITH_GATEWAY` is set, or the API key starts with `lsv2_`, requests route to the gateway. Don't set these by accident.

---

## B. langchain-anthropic with current Claude models (critical)

### B1. Source facts, `langchain-anthropic` 1.7.6 (`chat_models.py`) [SRC]

1. **`with_structured_output()` default is `method="function_calling"`** (L2871; L2965 has `# TODO: make method="json_schema" the default`). It works like this:
   - If `thinking` is enabled/adaptive **or** `not _supports_forced_tool_choice(model)` (L1155: the model starts with `claude-fable-5-1`, `claude-opus-5-5` or `claude-sonnet-5-5`), it binds the schema as a tool **without `tool_choice`** (that is, `auto`). It warns: "You are attempting to use structured output via forced tool calling, which is not supported when `thinking` is enabled or on claude-opus-5-5…". It then raises `OutputParserException` if the model does not call the tool.
   - Otherwise (for example `claude-haiku-5-5`) it **forces `tool_choice={"type":"tool","name":<Schema>}`**.
2. **`method="json_schema"` = native structured outputs.** It does `self.bind(output_config={"format": {"type": "json_schema", "schema": anthropic.transform_schema(schema)}})` and parses with `PydanticOutputParser`/`JsonOutputParser`. It **does send `output_config.format`** (verified in a dry-run payload; `transform_schema` adds `additionalProperties: false`). `method="json_mode"` is remapped to `json_schema` with a warning. The deprecated `output_format` kwarg is moved into `output_config.format` with a DeprecationWarning.
3. **`bind_tools(tools, tool_choice="any"|"<name>")` has no model guard.** It drops a forced `tool_choice` only if `thinking` is set on the instance. On Opus 5.5 / Sonnet 5.5 / Fable 5.1 without `thinking` set, the forced choice is sent and fails with a 400 ([RUN], B2). This matters for `langchain.agents.create_agent(response_format=...)`:
   - `ToolStrategy` calls `bind_tools(..., tool_choice="any")` (factory.py L1475), so it 400s on those models.
   - When the model profile has `structured_output: True`, `create_agent` picks `ProviderStrategy`, which langchain-anthropic turns into `output_config.format` (L2009–2025). The profiles say True for Opus 5.5, Sonnet 5.5 and Fable 5.1, but **False for Haiku 5.5**, which therefore gets `ToolStrategy` with forced `any`. That works on Haiku 5.5 (B2), and native JSON schema also works on Haiku 5.5 despite the profile flag.
4. **Thinking and effort:**
   - `ChatAnthropic(thinking={"type": "adaptive", "display": "summarized"})`.
   - `reasoning_effort="low"` (alias `effort=`) maps to `output_config.effort`. It can also be passed at call time.
   - **If `reasoning_effort` is set and `thinking` is not, langchain sets `thinking={"type":"adaptive","display":"summarized"}`**, but only when the model profile lists an `xhigh` level. The Haiku 5.5 profile lists no effort levels, so it gets no auto-thinking (L1993–2003).
   - `output_config={...}` is also a field. Setting `task_budget` auto-adds the `task-budgets-2026-03-13` beta.
   - Don't pass `temperature`, `top_k` or `top_p` to the 5.x models. The profiles mark `temperature: False`, and langchain strips them for `claude-fable-5*`.
5. **Betas:** `ChatAnthropic(betas=[...])`. Any non-empty `betas` routes the call to `client.beta.messages.create` (L2172–2179). Some betas are added automatically (tool types, MCP, task budgets, `thinking.display="updates"`, user profiles).
6. **Prompt caching:**
   - Call-time kwarg `model.invoke(msgs, cache_control={"type": "ephemeral"})` is sent as the top-level automatic-caching param on the direct API (L1912–1932). Bedrock subclasses expand it to block level.
   - Block-level `cache_control` on content blocks or tool_result blocks is preserved.
   - For agents there is `langchain_anthropic.middleware.AnthropicPromptCachingMiddleware`.
7. **Arbitrary extra request params:** use `model_kwargs={...}` or call-time kwargs; they are merged into the payload and passed to `client.messages.create(**payload)`. Use `extra_body={...}` for fields the SDK does not type. Sampling params the SDK rejects are moved into `extra_body` automatically (`_sdk_compat.py`). [RUN] `betas=["server-side-fallback-2026-07-01"], model_kwargs={"fallbacks": "default"}` was accepted (`anthropic` 1.15.0 types `fallbacks` on beta create).
8. **Message Batches API: not supported.** There are no references to `batches` anywhere in the package. LangChain's `.batch()` just runs concurrent single requests at full price. For the 50% batch discount, call `anthropic.Anthropic().messages.batches.create(...)` directly with the same `output_config.format` request shape.
9. **Base URL resolution** (field `anthropic_api_url`, alias `base_url`, L1420–1430 and `_resolve_gateway` L1708–1730):
   - explicit `base_url` → env **`ANTHROPIC_API_URL`** → env **`ANTHROPIC_BASE_URL`** → `LANGSMITH_GATEWAY` → `https://api.anthropic.com`.
   - The key comes from `api_key=` or `ANTHROPIC_API_KEY`.
   - `ANTHROPIC_PROXY` sets an HTTP proxy.
   - The raw `anthropic` SDK reads only `ANTHROPIC_BASE_URL` (`_client.py` L245). JS `@langchain/anthropic` reads `ANTHROPIC_API_URL || ANTHROPIC_BASE_URL` (chat_models.js L689).
10. **Defaults that bite:**
    - `max_tokens` defaults to the profile's `max_output_tokens`, which is **128000** for all 5.5 models.
    - The client is built with `timeout=None` ([RUN]: `ChatAnthropic(...)._client.timeout` → `None`). That skips the SDK's guard ("Streaming is required for operations that may take longer than 10 minutes", `_base_client.py` L854–863), so a non-streaming call has **no HTTP timeout**.
    - Set `max_tokens` and `timeout` explicitly, or stream.
11. **Refusals:** there is no special handling. `stop_reason` appears in `AIMessage.response_metadata["stop_reason"]`, and `stop_details` is captured when streaming (L2494). With `json_schema` parsing, a refusal will probably surface as an `OutputParserException`. That is inferred from the code and was not triggered empirically. Use `include_raw=True` and check `raw.response_metadata["stop_reason"] == "refusal"`.
12. **Errors:** 400s are re-raised as `AnthropicInvalidRequestError(anthropic.BadRequestError, langchain_core.exceptions.ModelInvalidRequestError)` (L1230).
13. **PDF input:** LangChain standard `{"type": "file", ...}` blocks (base64, URL or file_id) are converted to Anthropic `document` blocks (L442–494). Native `document` blocks pass through.
14. **Assistant prefill returns 400 on the 5.x models.** If a message list ends with an `AIMessage`, langchain sends it as a prefill. Never end the input with an AI message.

### B2. Empirical calls (6 of 6 allowed) [RUN]

All calls used `ChatAnthropic(..., base_url="https://api.anthropic.com", api_key=<JOBHUNTER key>, max_tokens=512, max_retries=0)`, a subprocess with `ANTHROPIC_BASE_URL` and `LANGSMITH_API_KEY` unset, and `LANGSMITH_TRACING=false`. The schema was `FitScore{score:int, reason:str}` and the prompt was a 3-line CV/job snippet. Script: `bench/lc_anthropic_probe.py`; raw results: `bench/api_results.jsonl`.

| # | Case | Request langchain built | Result | Usage in / out |
|---|---|---|---|---|
| 1 | `claude-haiku-5-5`, `with_structured_output()` default | forced `tool_choice={"type":"tool","name":"FitScore"}`, no thinking | **OK**, `stop_reason=tool_use`, parsed, thinking_tokens=0 | 621 / 103 |
| 2 | `claude-opus-5-5`, `with_structured_output()` default | tool bound, **no tool_choice** (langchain guard), no thinking param | **OK** (the model chose to call the tool, which is not guaranteed), `stop_reason=tool_use` | 495 / 112 |
| 3 | `claude-opus-5-5`, `method="json_schema"`, `reasoning_effort="low"` | `output_config={"effort":"low","format":{json_schema}}`, auto `thinking={"type":"adaptive","display":"summarized"}` | **OK**, `stop_reason=end_turn`, valid JSON parsed into Pydantic | 380 / 82 |
| 4 | `claude-sonnet-5-5`, `bind_tools([FitScore], tool_choice="any")` | forced `{"type":"any"}` | **HTTP 400** `invalid_request_error`: **`tool_choice: type "tool" and "any" are not supported for this model.`** (raised as `AnthropicInvalidRequestError`) | 0 / 0 |
| 5 | `claude-haiku-5-5`, `method="json_schema"` | `output_config.format` | **OK**, so Haiku 5.5 supports native structured output even though the langchain profile says `structured_output: False` | 380 / 70 |
| 6 | `claude-opus-5-5`, `json_schema` + effort low + `betas=["server-side-fallback-2026-07-01"]` + `model_kwargs={"fallbacks":"default"}` | beta endpoint, `fallbacks:"default"` | **OK**. The response `usage` has new `fallback_credit: null` and `iterations[]` fields | 380 / 70 |

**Total: 6 calls, 2,256 input + 437 output = 2,693 tokens.** That is roughly $0.011 at list prices (Opus 1,255 in / 264 out, Haiku 1,001 in / 173 out). No call hit refusal or `max_tokens`. Observation: the native JSON-schema route used fewer input tokens (380) than the tool route (495 on Opus, 621 with forced tool on Haiku).

### B3. JS `@langchain/anthropic` 1.5.12 (tarball `dist/chat_models.js`, `dist/utils/params.js`) [SRC]

- `withStructuredOutput(schema, {method})` defaults to **`"functionCalling"`** and **forces `tool_choice: {type: "tool", name}` unless `this.thinking.type` is `enabled`/`adaptive`** (L1132–1160). **There is no model-based guard** (no equivalent of Python's `_supports_forced_tool_choice`). So on `claude-opus-5-5` / `claude-sonnet-5-5` / `claude-fable-5-1`, the default `withStructuredOutput` sends a forced tool_choice, and per B2 #4 that returns a 400 (not called from JS). Workarounds:
  - `{method: "jsonSchema"}`, which sends `outputConfig.format = {type: "json_schema", schema}` (L1099–1112); or
  - construct with `thinking: {type: "adaptive"}`, which binds the tool without tool_choice and throws if no tool call.
- **The package does not know the 5.5 models:** no `claude-opus-5-5`, `claude-sonnet-5-5` or `claude-haiku-5-5` entries in `profiles.js`. Prefix matching still applies some rules (`claude-opus-5` / `claude-sonnet-5` prefixes are treated as adaptive-only, which rejects `budget_tokens`/`topK`), and the default `maxTokens` is 16384 by prefix.
- Thinking is not sent unless explicitly set (`thinkingExplicitlySet`). Effort is set via `outputConfig: {effort}`; there is no `reasoning_effort` shortcut. Pass `betas`, `cache_control` (call option, top-level) and `inferenceGeo`. Pass arbitrary body fields via `invocationKwargs`.
- No Message Batches support (the `batchClient` name is just the non-streaming client).
- It pins `@anthropic-ai/sdk` 0.122.x, so newer typed params such as `fallbacks` may be missing from the types. Pass them through `invocationKwargs`. That fallbacks would be sent this way is UNVERIFIED.

---

## C. Local multilingual embeddings (CPU, German + English)

### C1. Candidates [REG HF Hub API + config files; scores from model cards]

| Model | License (commercial?) | Params / weights on disk | Dim (MRL) | Max seq | Prefixes | ST / ONNX / TEI / fastembed | Published quality |
|---|---|---|---|---|---|---|---|
| **BAAI/bge-m3** | MIT ✅ | 568M / 2.27 GB fp32 | 1024 (no MRL; also sparse + ColBERT outputs) | 8192 | none | ST ✅, ONNX fp32 in repo, TEI ✅, fastembed ✗ (dense) | MMTEB mean(task) 59.56, retrieval 54.60 (leaderboard 2025-05-24, per Qwen3 card); MIRACL 56.8, CLEF 40.8/41.3 (Arctic card) |
| **intfloat/multilingual-e5-large-instruct** | MIT ✅ | 560M / 1.12 GB fp16 | 1024 | **512** | query: `Instruct: {task}\nQuery: {q}`; docs: none | ST ✅, ONNX ✅, TEI ✅ | MMTEB 63.22, retrieval 57.12 |
| intfloat/multilingual-e5-base | MIT ✅ | 278M / 1.11 GB fp32 (qint8 ONNX 0.28 GB) | 768 | 512 | `query: ` / `passage: ` | ST, ONNX (fp32 / O4 / qint8-avx512-vnni), TEI | (older; lower than large) |
| **Alibaba-NLP/gte-multilingual-base** | Apache-2.0 ✅ | 305M / 0.61 GB fp16 | 768 (elastic 128–768) + sparse | 8192 | none | ST ✅ (**trust_remote_code**, pulls code from `Alibaba-NLP/new-impl`), TEI ✅, no ONNX in repo | MIRACL 52.3, CLEF 47.7/53.1 (Arctic card) |
| **Snowflake/snowflake-arctic-embed-l-v2.0** | Apache-2.0 ✅ | 568M (303M non-emb) / 2.27 GB fp32; ONNX int8 0.57 GB, fp16 1.13 GB | 1024 (**MRL→256**, <3% loss) | 8192 | `query: ` for queries only | ST ✅, ONNX (int8 / fp16 / q4 / bnb4), TEI ✅, transformers.js | BEIR 55.6, MIRACL 55.8, **CLEF 52.9/54.3** (CLEF covers de/fr/it/en; best of table) |
| Snowflake/snowflake-arctic-embed-m-v2.0 | Apache-2.0 ✅ | 305M / 1.22 GB | 768 (MRL 256) | 8192 | `query: ` | ST (trust_remote_code), ONNX, no TEI tag | – |
| **Qwen/Qwen3-Embedding-0.6B** | Apache-2.0 ✅ | 596M / 1.19 GB bf16 | 1024 (MRL 32–1024) | 32k | query `Instruct: …\nQuery:`; docs none | ST ✅, TEI ✅, fastembed ✅ (+`-Q` quantized) | **MMTEB 64.33, retrieval 64.64** (best open model in this size class on that snapshot) |
| voyageai/voyage-4-nano | Apache-2.0 ✅ | 346M (180M non-emb) / 0.69 GB bf16 | 2048 (MRL 1024/512/256) | 32k | `Represent the query for retrieving supporting documents: ` / `Represent the document for retrieval: ` | ST (trust_remote_code), TEI tag | card points to a Voyage blog/spreadsheet. Shares an embedding space with Voyage API models. MMTEB UNVERIFIED |
| jinaai/jina-embeddings-v3 | **CC-BY-NC-4.0 ❌** (no commercial use without a license) | 572M / 2.29 GB | 1024 (MRL) | 8192 | task LoRA prompts | ST (trust_remote_code), ONNX, fastembed | excluded on license (v5-text-small is also CC-BY-NC-4.0) |
| google/embeddinggemma-300m | **Gemma Terms of Use** (commercial allowed, but the Prohibited Use Policy is incorporated, redistribution must pass the terms on, and "Google reserves the right to restrict (remotely or otherwise) usage", [terms](https://ai.google.dev/gemma/terms), last modified 2026-04-01). HF repo is **gated** (401 without token) | 308M / 1.21 GB | 768 (MRL 512/256/128) | 2048 | `task: search result \| query: ` / `title: none \| text: ` (UNVERIFIED exact strings, card gated) | ST, TEI, fastembed | MMTEB v2 61.15 @768d (60.71 @512, 59.68 @256) ([HF blog](https://huggingface.co/blog/embeddinggemma), [card](https://huggingface.co/google/embeddinggemma-300m)) |
| sentence-transformers/paraphrase-multilingual-mpnet-base-v2 | Apache-2.0 ✅ | 278M / 1.11 GB | 768 | **128** (trained) | none | ST, ONNX, TEI, fastembed | too short for job ads. Paraphrase/STS model, not retrieval |
| ibm-granite/granite-embedding-278m-multilingual | Apache-2.0 ✅ | 278M | 768 | 512 | none | ST, ONNX, TEI | – (extra candidate, not benchmarked) |
| nomic-ai/nomic-embed-text-v2-moe | Apache-2.0 ✅ | 475M MoE | 768 | 512 | `search_query: ` / `search_document: ` | ST, TEI | – (extra candidate) |

The MMTEB numbers come from [Qwen3-Embedding-0.6B card](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B) (leaderboard snapshot 2025-05-24) and the Arctic numbers from the [arctic-embed-l-v2.0 card](https://huggingface.co/Snowflake/snowflake-arctic-embed-l-v2.0). None of these is a German-only job-ads benchmark. Run a small domain eval on real ads before committing.

### C2. CPU benchmark [RUN] (`bench/embed_bench.py`, `bench/ort_direct_bench.py`, raw: `bench/embed_results.jsonl`)

Setup:
- Each model ran in a fresh process with torch 2.14.1+cpu and 4 threads, sentence-transformers 5.7.0, transformers 4.57.6 (optimum-onnx pinned these), and onnxruntime 1.31.0.
- Corpus: 50 synthetic job ads (30 German, 20 English) of about 407 words each, which is **about 652 XLM-R tokens per ad**. That means the "1024" runs already cover the whole ad, and the "512" runs truncate it.
- "Long" set: 25 ads of about 814 words, about 1,300 tokens each. Batch size 8 (4 for the long set).
- Load times exclude download, except for the ONNX e5-base rows, which include it. Peak RSS is `ru_maxrss` of the whole process.

| Model / backend | docs/s @512 (ms/doc) | docs/s @1024 | long ≈1300 tok @2048 | Peak RSS | Load |
|---|---|---|---|---|---|
| bge-m3, torch fp32 | 1.06 (947) | 0.89 (1124) | 0.43 (2314 ms) | 2.9 GB | 2.7 s |
| snowflake-arctic-embed-l-v2.0, torch fp32 | 1.13 (883) | 1.01 (985) | – (same arch as bge-m3, expect about 0.45) | 2.9 GB | 0.6 s |
| **arctic-embed-l-v2.0, ONNX int8 (direct onnxruntime)** | **3.92 (255)** | **2.18 (460)** | – | 2.6 GB | 3.8 s |
| multilingual-e5-large-instruct, torch (fp16 weights → fp32) | 1.28 (780) | n/a (max 512) | n/a | 3.7 GB | 5.3 s |
| gte-multilingual-base, torch | 3.12 (321) | 2.28 (439) | 1.22 (820 ms) | 2.5 GB | 5.9 s |
| Qwen3-Embedding-0.6B, torch (bf16 → fp32) | 0.53 (1905) | 0.44 (2250) | – | **5.6 GB** | 3.6 s |
| multilingual-e5-base, torch | 3.98 (251) | n/a | n/a | 1.75 GB | 1.3 s |
| multilingual-e5-base, ONNX fp32 (ST backend) | 5.46 (183) | n/a | n/a | 2.3 GB | – |
| multilingual-e5-base, **ONNX qint8 avx512-vnni** (ST backend) | **11.52 (87)** | n/a | n/a | 1.4 GB | – |

Takeaways:
- On 4 vCPU, the large XLM-R models (bge-m3, arctic-l) run at about 1 ad per second with PyTorch. Int8 ONNX gives about 3.5× at 512 and 2.2× at 1024 (on a CPU with AVX512-VNNI).
- For an estimated 200–1,000 new ads a day, even the slowest option finishes in minutes. Throughput is not the deciding factor. RAM (about 2.5–3 GB peak for the large models) and quality are.
- Qwen3-0.6B is about 2× slower than the XLM-R-large models and peaked at 5.6 GB in fp32.

### C3. Cross-lingual sanity check [RUN]

Query: an **English** CV snippet (senior Python backend, FastAPI, Postgres, K8s, AWS). It was compared with 6 short **German** ads: 2 relevant (backend, platform), 1 partial (data engineer), 3 irrelevant (nurse, bookkeeper, truck driver). Cosine similarity:

| Model | rel. backend | rel. platform | partial data | max irrelevant | margin (min rel − max irr) |
|---|---|---|---|---|---|
| arctic-embed-l-v2.0 (torch) | 0.765 | 0.518 | 0.370 | 0.144 | **0.374** |
| arctic-embed-l-v2.0 (ONNX int8) | 0.732 | 0.500 | 0.354 | 0.141 | 0.359 |
| Qwen3-Embedding-0.6B | 0.817 | 0.594 | 0.489 | 0.289 | 0.305 |
| gte-multilingual-base | 0.905 | 0.726 | 0.603 | 0.524 | 0.202 |
| bge-m3 | 0.853 | 0.647 | 0.572 | 0.497 | 0.150 |
| multilingual-e5-large-instruct | 0.927 | 0.869 | 0.830 | 0.773 | 0.096 |
| multilingual-e5-base | 0.897 | 0.827 | 0.793 | 0.786 | 0.041 |

All 7 runs ordered the ads as relevant > partial > irrelevant. Absolute cosines are not comparable across models: the e5 family packs everything into a 0.75–0.93 band, so a fixed similarity threshold is model-specific. Use top-k or rank cut-offs, or calibrate per model. This is a 6-pair sanity check, not a quality benchmark.

### C4. Serving options

| Option | Footprint | Pros | Cons |
|---|---|---|---|
| **TEI container** `ghcr.io/huggingface/text-embeddings-inference:cpu-1.9.4` (latest CPU tag [REG]) | **about 239 MB compressed** (amd64) + model volume | Separate service with an HTTP `/embed` API and dynamic batching. No torch in the app image. Supports models tagged `text-embeddings-inference` on HF: bge-m3, mE5, gte-mb, arctic-l-v2, Qwen3-0.6B, embeddinggemma, voyage-4-nano tag | One more container. Throughput not measured (no Docker daemon here), so UNVERIFIED. Prefixes are your job. arctic-m-v2 and jina-v3 are not tagged |
| In-process **sentence-transformers + torch CPU** | torch CPU wheel about 740 MB installed (my venv was 1.3 GB with ONNX extras). Use the `download.pytorch.org/whl/cpu` index, because the default PyPI torch on Linux pulls CUDA wheels | Simplest. Every model works (including trust_remote_code ones). `prompt_name`/`truncate_dim` built in | Competes with the worker for CPU/RAM (2–5.6 GB peak). Large image. Current ST 6.1.0 needs transformers ≥ 5 |
| **ONNX Runtime** (ST `backend="onnx"`, fastembed 0.9.0, or direct `onnxruntime` + `tokenizers`) | onnxruntime about 63 MB installed | 2–3.5× faster with int8 (measured), lower RAM, no torch needed if used directly | Model-specific exports. ST's onnx backend failed on arctic's `model_int8.onnx` (`KeyError: 'last_hidden_state'`; the export only outputs `sentence_embedding`), so direct ORT was needed. `optimum-onnx` 0.1.0 pins older transformers. fastembed has mE5-large, Qwen3-0.6B (+Q), embeddinggemma, paraphrase-mpnet and jina-v3, but **not bge-m3 dense, arctic-l-v2 or gte-mb**. Rerankers in fastembed: `BAAI/bge-reranker-v2-m3` (+int8), `jina-reranker-v2-base-multilingual` |

---

## D. Storage / infra facts

### D1. pgvector and Postgres

- **Ubuntu noble ships `postgresql-16-pgvector` 0.6.0-1** [RUN `dpkg -l`; `/usr/share/postgresql/16/extension/vector.control`: `default_version = '0.6.0'`]. Its SQL has **no `halfvec`** (0 matches). `halfvec`, `sparsevec` and `binary_quantize` arrived in 0.7.0, and iterative index scans in 0.8.0 ([pgvector README](https://github.com/pgvector/pgvector)). Don't use the distro package.
- **Latest pgvector: 0.8.7** (README install branch `v0.8.7`. Docker tags `pgvector/pgvector:0.8.7-pg18`, `-pg17`, `-pg16`, …, and plain `pg18`/`pg17`/`pg16`, plus `-bookworm`/`-trixie` variants, all updated 2026-10-01, about 160–168 MB compressed [REG Docker Hub]).
- **Index limits** (README, confirmed in PGlite):
  - HNSW and IVFFlat index up to **2,000 dims for `vector`** and **4,000 for `halfvec`**, 64,000 for `bit`, 1,000 non-zero for `sparsevec`.
  - Storage limit is 16,000 dims.
  - [RUN] `vector(2001)` with HNSW raised "column cannot have more than 2000 dimensions for hnsw index", and `halfvec(4001)` raised the 4000 error.
  - HNSW defaults: `m=16`, `ef_construction=64`, `hnsw.ef_search=40`.
  - Iterative scans: `hnsw.iterative_scan = strict_order | relaxed_order` [RUN], `hnsw.max_scan_tuples` 20,000. These fix "filtered kNN returns too few rows".
  - Operators: `<->` L2, `<#>` negative inner product, `<=>` cosine, `<+>` L1, `<~>` Hamming, `<%>` Jaccard.
- Size math: 1024-dim `vector` is about 4 KB per row, `halfvec` about 2 KB. 100k ads × 1024-d halfvec is about 200 MB plus the HNSW index.
- **Postgres versions** [REG Docker Hub `library/postgres`]:
  - Latest stable minors: **18.6**, 17.11, 16.15.
  - **19 is still in beta (`19beta4`)**.
  - PGlite 0.5.10 reports "PostgreSQL 18.6" with pgvector 0.8.7 [RUN].
  - Both 17 and 18 are fully supported by the pgvector image. 18 adds built-in `uuidv7()` and async I/O (from memory, UNVERIFIED today).

### D2. German full-text search and fuzzy dedup (all [RUN] on PGlite = PG 18.6 + pg_trgm 1.6 + unaccent 1.1 + vector 0.8.7; `bench/pg_fts_probe.mjs`)

- The built-in `german` config exists (snowball stemmer + `german.stop`; the file also ships in the Ubuntu PG16 `tsearch_data`).
  - `to_tsvector('german','Wir suchen eine erfahrene Softwareentwicklerin (m/w/d) für unsere Backend-Systeme in München')` → `'backend':10 'backend-system':9 'erfahr':4 'm/w/d':6 'munch':13 'softwareentwickl':5 'such':2 'system':11 'uns':8`.
  - Inflection works: "Entwicklerinnen und Entwicklern" matches `plainto_tsquery('german','Entwickler')` → true.
  - Hyphenated compounds are split ("backend-system" plus its parts).
  - **Closed compounds are not split:** `to_tsvector('german','Softwareentwickler') @@ to_tsquery('german','Entwickler')` → **false**. Use prefix queries, pg_trgm/ILIKE, embeddings, or an ispell/hunspell German dictionary with compound support (not bundled) to decompound.
  - `websearch_to_tsquery('german','"python entwickler" -praktikum or django')` → `'python' <-> 'entwickl' & !'praktikum' | 'django'`.
- `unaccent('Müller Straße Café São Paulo')` → `Muller Strasse Cafe Sao Paulo`. **`unaccent()` is STABLE**, so `CREATE INDEX … (unaccent(x))` fails with "functions in index expression must be marked IMMUTABLE". Wrap it in an IMMUTABLE SQL function with the explicit dictionary (`unaccent('unaccent'::regdictionary, x)`), or build a custom text search config (`COPY = german`, then map word tokens `WITH unaccent, german_stem`). That second pattern is from the docs and was not run. Note that unaccent maps ä→a, while German users may type "ae".
- **pg_trgm:**
  - `similarity('Senior Python Entwickler (m/w/d) Berlin', 'Python Entwickler Senior (w/m/d) - Berlin')` = **1.0**. Trigrams ignore word order and punctuation, which is good for title dedup but can match texts that are actually different.
  - `show_limit()` default = 0.3 (the `%` operator threshold).
  - `word_similarity('Python Entwickler', 'Senior Python Backend Entwickler (m/w/d)')` = 0.69.
  - Index with GIN `gin_trgm_ops` or GiST `gist_trgm_ops`; GiST supports `<->` kNN.

### D3. Scheduling / job queues

| Option | Latest [REG] | License | Needs | Schedule persistence | Missed runs (downtime) | Single run with multiple processes |
|---|---|---|---|---|---|---|
| APScheduler 3.x | 3.11.3 (2026-06-28) | MIT | nothing (in-process). Optional SQLAlchemy/Redis/Mongo job store | Persistent job stores supported | `misfire_grace_time` per job or global. Missed runs within grace execute, possibly **several times in a row** unless `coalesce=True` ([3.x guide](https://apscheduler.readthedocs.io/en/3.x/userguide.html)) | **No.** "Sharing a persistent job store among two or more processes will lead to incorrect scheduler behavior like duplicate execution… You can't" ([FAQ](https://apscheduler.readthedocs.io/en/3.x/faq.html)). Run exactly one scheduler process |
| APScheduler 4 | **4.0.0a6 (2025-04-27), still alpha** | MIT | data store (Postgres/SQLAlchemy) + event broker | yes | (designed for it) | designed for shared stores, but PyPI says: "do NOT use this release in production!" |
| Celery + beat | 5.6.3 (5.7.0b1 2026-10-06) | BSD-3 | **Redis/RabbitMQ broker** (+ result backend) | `celerybeat-schedule` local file, or a DB scheduler (django-celery-beat) | runs an overdue entry once after restart (UNVERIFIED) | "You have to ensure only a single scheduler is running for a schedule at a time, otherwise you'd end up with duplicate tasks" ([docs](https://docs.celeryq.dev/en/stable/userguide/periodic-tasks.html)) |
| arq | 0.28.0 (2026-04-16) | MIT | **Redis** | cron in code (`WorkerSettings.cron_jobs`) | no catch-up (UNVERIFIED) | custom job IDs guarantee a job "cannot be enqueued again until its execution has finished and its result has cleared" ([docs](https://arq-docs.helpmanual.io/)) |
| Dramatiq | 2.2.1 (2026-09-02) | **LGPL-3.0+** | Redis or RabbitMQ | no built-in scheduler (pair with APScheduler or periodiq; UNVERIFIED) | – | – |
| **procrastinate** | 3.10.0 (2026-09-23) | MIT | **Postgres only** (psycopg3), no Redis | `@app.periodic(cron="…")` in code. Deferral state in DB | "When a worker starts, it will defer periodic tasks that have not been deferred yet but a task that is **more than 10 minutes late will not be deferred**" (configurable). No worker running means nothing is scheduled ([docs](https://procrastinate.readthedocs.io/en/stable/howto/advanced/cron.html)) | "the database is responsible for making sure deferring only happens once per period, even with multiple workers" |
| **PgQueuer** | 1.6.0 (2026-10-11) | MIT | Postgres 14+ (LISTEN/NOTIFY + `FOR UPDATE SKIP LOCKED`) | `@pgq.schedule("name","cron")` stored in table `pgqueuer_schedules` | not documented on the page I read (UNVERIFIED) | "schedules survive process restarts and only one worker runs each due task" ([docs](https://janbjorge.github.io/pgqueuer/guides/scheduling/)) |
| Temporal | SDK `temporalio` 1.34.0 | MIT (SDK) | **Temporal server + its DB** (heavy for one VPS) | server-side Schedules | Catchup Window **default one year** (min 10 s); overlap policy default `Skip` ([docs](https://docs.temporal.io/schedule)) | yes (server-managed) |
| Node: **pg-boss** | 12.38.0 (2026-10-13) | MIT | Postgres (SKIP LOCKED) | `boss.schedule(name, cron \| RRULE, data, opts)` stored in DB, checked every 30 s | option **`missed: 'skip'` (default) or `'once'`** (one job for the most recent missed occurrence) | "job throttling options are then used to make sure only 1 job is sent even if multiple instances are running". Also clock-skew correction against the DB clock ([docs](https://timgit.github.io/pg-boss/api/scheduling.html)) |
| Node: BullMQ | 6.3.15 (2026-10-14) | MIT | **Redis** by default. **New optional PostgreSQL backend** (`createPostgresBackend`, PG 13+; "Redis backend remains the default and the most battle-tested") ([docs](https://docs.bullmq.io/guide/postgresql)) | `queue.upsertJobScheduler(id, {pattern \| every, tz})` | the scheduler "will only generate new jobs when the last job begins processing". No catch-up documented (UNVERIFIED) | upsert by scheduler id prevents duplicates ([docs](https://docs.bullmq.io/guide/job-schedulers)) |
| Node: graphile-worker | 0.18.0 | MIT | Postgres | crontab | backfill option (UNVERIFIED) | yes (UNVERIFIED) |

### D4. CV / resume parsing

| Library | Latest [REG] | License | Footprint (uv dry-run) | Notes |
|---|---|---|---|---|
| pypdf | 6.20.1 (2026-10-12) | BSD-3 | 1 package, pure Python | fine for text PDFs. Reading order in multi-column CVs can be poor |
| pdfplumber | 0.11.10 | MIT | 8 packages (pdfminer.six 20260107, pypdfium2) | better layout/word positions and tables. Slower |
| PyMuPDF / pymupdf4llm | 1.28.2 | **AGPL-3.0 or commercial (Artifex)** | 1 wheel | fastest and best text/Markdown. The AGPL network clause applies to network services. Avoid it, or accept AGPL for the app |
| docling (IBM) | 2.140.0 (2026-10-16) | MIT | **121 packages incl. torch, torchvision, transformers 5.20, opencv, rapidocr, scipy** (multi-GB; default PyPI torch pulls CUDA) | strong layout/table/OCR. `docling-slim` gives about 50 MB base with opt-in extras (`[format-pdf,models-local]`, `[format-office]`) |
| unstructured | 0.27.27 | Apache-2.0 | `[pdf]`: 142 packages incl. torch, onnxruntime, opencv, tesseract wrapper. **Requires Python <3.14** | heavy |
| python-docx | 1.2.0 (2025-06-16) | MIT | small | DOCX CVs |
| markitdown (Microsoft) | 0.1.8 (2026-09-21) | MIT | `[pdf,docx]`: 30 packages (magika → onnxruntime, pdfminer.six, pdfplumber, mammoth) | quick "anything → Markdown". Python <3.15 |
| **Claude native PDF input** | – | – | none | `{"type":"document","source":{"type":"base64","media_type":"application/pdf","data":…}}` or a Files API `file_id`. Limits: 32 MB request, 600 pages (100 on 200k-context models) (claude-api skill docs). Citations cannot be combined with `output_config.format` (400). One-shot "CV PDF → structured profile JSON" with `output_config.format` is viable, and the CV is a single document, so cost is negligible. LangChain `file` blocks map to it (B1.13) |

### D5. Web stack versions [REG]

- **FastAPI 0.143.2** (2026-10-15) requires `pydantic>=2.9` (Pydantic v2 only), `starlette>=0.46` and Python ≥3.10. Pydantic 2.14.1. uvicorn 0.54.0.
- **SQLAlchemy 2.1.4** (2026-10-07; **2.1 requires Python ≥3.11**). Alembic 1.20.0.
- psycopg 3.3.6 (LGPL-3.0, dynamic use fine) + psycopg-pool 3.3.3; asyncpg 0.32.0. LangGraph's Postgres checkpointer needs psycopg3, so using `postgresql+psycopg` (sync and async) for SQLAlchemy too keeps it to one driver.
- Frontend: **Vite 8.3.4**, **React 19.3.1**, **@tanstack/react-query 5.104.3**, **@tanstack/react-router 1.170.44**, Next.js 16.4.2, TypeScript **7.0.2** (2026-07-08; a major version, so check that the toolchain/ESLint plugins support it, UNVERIFIED). For a two-page UI served by FastAPI, a Vite SPA avoids running a second Node server, while Next.js adds a Node runtime to the Compose stack.
- PGlite 0.5.10 (embedded WASM Postgres 18.6 with pgvector via `@electric-sql/pglite-pgvector`) is handy for JS tests. pg-boss lists PGlite support.

---

## Design implications / gotchas

1. **Forced `tool_choice` is gone on Opus 5.5 / Sonnet 5.5 / Fable 5.1.** The exact 400 text is `tool_choice: type "tool" and "any" are not supported for this model.` [RUN]. What to do in LangChain:
   - Use **`model.with_structured_output(Schema, method="json_schema")`** for every structured output (scores, extraction). It sends `output_config.format`, worked on Opus 5.5 and Haiku 5.5, and used fewer input tokens than the tool route.
   - Never call `bind_tools(..., tool_choice="any"/"<name>")` and never use `create_agent(..., response_format=ToolStrategy(...))` on these models.
   - With `create_agent`, pass `response_format=ProviderStrategy(Schema)` explicitly instead of relying on profile auto-detection.
   - Python's default `with_structured_output()` silently downgrades to "auto" tool choice on these models. That can raise `OutputParserException` when the model doesn't call the tool.
   - JS `@langchain/anthropic` 1.5.12 has no such guard. Its default *will* 400 on these models, so always pass `{method: "jsonSchema"}`.
2. **`ANTHROPIC_BASE_URL` gotcha.** ChatAnthropic resolves `base_url` → `ANTHROPIC_API_URL` → `ANTHROPIC_BASE_URL` → LangSmith gateway → api.anthropic.com. The raw SDK uses `ANTHROPIC_BASE_URL`. In any environment where these variables point elsewhere (as in this container), always pass `base_url="https://api.anthropic.com"` and `api_key=...` explicitly. In production, use an app-specific variable such as `JOBHUNTER_ANTHROPIC_API_KEY` and wire it into the constructor. Also make sure `LANGSMITH_GATEWAY` is unset and the key doesn't start with `lsv2_`.
3. **Set `max_tokens` and `timeout` on ChatAnthropic.** The defaults are 128,000 and *no timeout*, which bypasses the SDK's 10-minute streaming guard. Use streaming for long agent turns.
4. **Opus 5.5 thinking cannot be disabled.** Control cost with `reasoning_effort` (the default is `medium`; set it explicitly). Setting `reasoning_effort` in langchain also turns on `thinking={"type":"adaptive","display":"summarized"}` for Opus/Sonnet/Fable 5.x (not Haiku 5.5). Don't send `temperature`/`top_p`/`top_k`, and never end the input with an AIMessage (that is a prefill, which returns 400).
5. **Refusals:** use `include_raw=True` with structured output and branch on `raw.response_metadata["stop_reason"] == "refusal"`. Optionally enable server-side fallbacks with `betas=["server-side-fallback-2026-07-01"], model_kwargs={"fallbacks": "default"}`, which worked [RUN]. Neither langchain nor LangGraph handles refusals for you.
6. **LangGraph's default `RetryPolicy` retries Anthropic 400s** [RUN]. Always pass `retry_on` that is limited to `RateLimitError`/`InternalServerError`/`APIConnectionError`/timeouts. Remember the SDK's own `max_retries`, and use `timeout=` / `TimeoutPolicy` per LLM node. In JS the default already skips 4xx.
7. **For batch scoring with no latency requirement, use the Message Batches API** (50% off) through the `anthropic` SDK directly. langchain-anthropic has no Batches support. A LangGraph `Send` fan-out with `max_concurrency` is the simpler online alternative.
8. **LangGraph as a library inside one worker process is fine** (MIT, no server, no LangSmith). Use `PostgresSaver` with an autocommit, `prepare_threshold=0`, `dict_row` psycopg pool. Call `.setup()` at deploy time, before app traffic, because it uses `CREATE INDEX CONCURRENTLY`. Use `durability="sync"` for long, expensive runs. Re-invoke with `None` on the same `thread_id` to resume after a crash [RUN].
9. **LangSmith with CV PII:** leave `LANGSMITH_TRACING` unset (default off), or set `LANGSMITH_HIDE_INPUTS=true`/`LANGSMITH_HIDE_OUTPUTS=true` (or a `Client(hide_inputs=fn)` redactor). EU accounts need `LANGSMITH_ENDPOINT=https://eu.api.smith.langchain.com`.
10. **Python over JS for the agent layer:** Python has the forced-tool guard, 5.5 model profiles, `reasoning_effort`, fallbacks typing and `TracePolicy`, and its SDK is current. JS `@langchain/anthropic` pins SDK 0.122.x and doesn't know the 5.5 models.
11. **Embeddings:**
    - Arctic-embed-l-v2.0 (Apache-2.0; 8k context; MRL 1024→256; best CLEF numbers and best separation in my cross-lingual check) or bge-m3 (MIT) are the solid large choices. Qwen3-Embedding-0.6B has the best published MMTEB but is about 2× slower on CPU and peaked at 5.6 GB RAM. gte-multilingual-base is the fast 8k-context small option (needs `trust_remote_code`).
    - **Avoid jina-v3/v5 (CC-BY-NC)**. EmbeddingGemma comes with the Gemma terms (gated, use policy, remote restriction clause). paraphrase-mpnet is limited to 128 tokens and mE5 to 512.
    - Use int8 ONNX (2–3.5× faster) or TEI to keep torch out of the app image. Store embeddings as `halfvec(1024)` (or MRL-256), and remember each model's query prefix.
    - Thresholds are model-specific (the e5 cosine band is narrow), so rank rather than use fixed cut-offs.
12. **pgvector:** don't use Ubuntu's `postgresql-16-pgvector` 0.6.0 (no `halfvec`, no iterative scans). Use `pgvector/pgvector:0.8.7-pg17` or `-pg18` (pin the full tag). HNSW indexes `vector` only up to 2,000 dims and `halfvec` up to 4,000. Enable `hnsw.iterative_scan` for filtered searches.
13. **German keyword search:**
    - The `german` config stems inflections but does **not decompound** ("Softwareentwickler" ≠ "Entwickler"). Combine FTS with pg_trgm/ILIKE or embeddings.
    - `unaccent` needs an IMMUTABLE wrapper before you can index it.
    - pg_trgm similarity is word-order-insensitive (reordered title = 1.0). For dedup, combine trigram similarity on title + company + location with an embedding cosine and a normalized URL/ID key.
14. **Scheduling on one VPS without Redis:** procrastinate (Python, Postgres-only, DB-enforced once-per-period, 10-minute catch-up window) or PgQueuer fit Postgres-only. pg-boss (`missed: 'once'`) is the Node equivalent. APScheduler 3.x works only as a single in-process scheduler (no shared job store), and APScheduler 4 is still alpha. Celery, arq, Dramatiq and BullMQ (default) add Redis. Temporal is overkill.
15. **CV parsing:** prefer Claude's native PDF `document` block with `output_config.format` for the single CV, plus pypdf/pdfplumber (permissive) as a text fallback for embedding the CV. Avoid PyMuPDF (AGPL) and full docling/unstructured (torch/CUDA-sized images) unless OCR is required.

---

### Files

- `bench/lc_anthropic_probe.py`: the 6 Anthropic probes (and `--dry` payload inspection). Results in `bench/api_results.jsonl`.
- `bench/langgraph_smoke.py`: offline LangGraph semantics checks (Send, max_concurrency, RetryPolicy, error_handler, CachePolicy, interrupt, recursion limit, durable resume).
- `bench/make_docs.py`, `bench/embed_bench.py`, `bench/ort_direct_bench.py`, `bench/download.py`, `bench/run_rest*.sh`: embedding benchmark. Results in `bench/embed_results.jsonl`.
- `bench/pg_fts_probe.mjs`: PGlite probe for German FTS, unaccent, pg_trgm and pgvector limits.
- `bench/hf_model_info.py`, `bench/hf_configs.py`, `bench/pypi_versions.py`, `bench/html2txt.py`: registry and doc helpers. Data in `hf_models.jsonl`, `hf_configs.jsonl`, `pypi_versions.txt`, `npm_versions.txt`.
