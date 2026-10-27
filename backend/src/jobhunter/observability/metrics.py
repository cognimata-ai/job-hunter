"""Prometheus metrics on a dedicated registry, so nothing but these series is exposed.

`refresh_table_bytes` is the one collector of the table sizes: the worker's `metrics_refresh` reconciler sets
`TABLE_BYTES` with it, and system health reports what it returns (ruling T13-c).
"""

from typing import Final

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, start_http_server
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.db.sql import q

REGISTRY = CollectorRegistry()

RUNS = Counter("jobhunter_runs", "Runs that reached a terminal status.", ["status"], registry=REGISTRY)
# A run takes seconds to an hour; `+Inf` is implicit.
RUN_BUCKETS = (1, 5, 15, 30, 60, 120, 300, 600, 1200, 1800, 3600)
RUN_DURATION = Histogram(
    "jobhunter_run_duration_seconds", "Duration of a run.", buckets=RUN_BUCKETS, registry=REGISTRY
)
HTTP_REQUESTS = Counter(
    "jobhunter_http_requests", "HTTP requests served.", ["route", "status"], registry=REGISTRY
)
TABLE_BYTES = Gauge(
    "jobhunter_table_bytes", "Size of a database table in bytes.", ["table"], registry=REGISTRY
)
ALERTS_OPEN = Gauge("jobhunter_alerts_open", "Open alerts by severity.", ["severity"], registry=REGISTRY)
# The fetch requests by source and receipt status (§17.5): one per receipt an attempt settled itself
# (`succeeded`, `failed`, `ambiguous` or `skipped`, by the fetch nodes) and one per `skipped` receipt a
# planning transaction wrote (the `plan` and `plan_fallback` nodes, after their commit); a receipt a resumed
# attempt finds again is never counted twice.
FETCH_REQUESTS = Counter(
    "jobhunter_fetch_requests",
    "Fetch requests by receipt status: sent, denied or skipped at planning.",
    ["source", "status"],
    registry=REGISTRY,
)
# The raw items `parse_normalize` wrote (one per element of a response it parsed), by source and disposition.
RAW_ITEMS = Counter(
    "jobhunter_raw_items",
    "Raw items written by parse_normalize.",
    ["source", "disposition"],
    registry=REGISTRY,
)
# The provider's current quota window (§6.3); the ledger's reservation and settlement set them (M1 Task 8).
QUOTA_USED_UNITS = Gauge(
    "jobhunter_quota_used_units",
    "Counted units of the provider's current quota window.",
    ["provider"],
    registry=REGISTRY,
)
QUOTA_REMAINING_UNITS = Gauge(
    "jobhunter_quota_remaining_units",
    "Units left in the provider's current quota window (limit minus counted units).",
    ["provider"],
    registry=REGISTRY,
)

# The duration of every embed batch's `/v1/embed` call (§9.2 node 7, the `awaiting_embedding` reconciler):
# about 28 s for 16 texts on the V7 CPU, at most `embeddings.BATCH_TIMEOUT_S` (120 s). `outcome` is `ok`, the
# `EmbedderError` kind (`unavailable` for a timeout, `bad_response`, `checksum`) or `error` (anything else).
EMBED_BUCKETS = (0.5, 1, 2, 5, 10, 20, 30, 45, 60, 90, 120)
EMBED_SECONDS = Histogram(
    "jobhunter_embed_seconds",
    "Duration of one embed batch call, by outcome.",
    ["outcome"],
    buckets=EMBED_BUCKETS,
    registry=REGISTRY,
)

# The duplicate groups the `group_representatives` reconciler repaired (§8.7, §17.4: one election per group
# per pass), counted once the repair committed.
DEDUP_GROUP_REPAIRS = Counter(
    "jobhunter_dedup_group_repairs",
    "Duplicate groups repaired by the group_representatives reconciler.",
    registry=REGISTRY,
)


# The LLM spend of the month (§17.5), set from a settlement's `spend_ledger.SettleFigures` once its
# transaction has committed: `state` is `counted` (the month row's counter), `billed`, `ambiguous` or
# `reserved` (in flight).
LLM_SPEND_USD = Gauge(
    "jobhunter_llm_spend_usd", "LLM spend of the month by ledger state, in USD.", ["state"], registry=REGISTRY
)
LLM_BUCKET_USD = Gauge(
    "jobhunter_llm_bucket_usd", "The USD bucket that paces scoring (spec 6.9.2), in USD.", registry=REGISTRY
)
LLM_HEADROOM_USD = Gauge(
    "jobhunter_llm_headroom_usd", "Scoring headroom under the monthly LLM cap, in USD.", registry=REGISTRY
)
# Settled cost / reservation of each settled try, by purpose: about 0.1-0.5 for text, 1.0 for an ambiguous try
# (it keeps its reservation), above 1.0 only for a bound violation (§10.7).
RESERVATION_RATIO_BUCKETS = (0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.5, 2.0)
LLM_RESERVATION_RATIO = Histogram(
    "jobhunter_llm_reservation_ratio",
    "Settled cost divided by the reservation of an LLM try, by purpose.",
    ["purpose"],
    buckets=RESERVATION_RATIO_BUCKETS,
    registry=REGISTRY,
)


# Every HTTP try of the gateway (§17.5), counted once its settlement has committed. `model` is the requested
# model, `purpose` the node; `outcome` is `ok` or the try's `llm_calls.error_code`: `invalid_output`,
# `refused`, `max_tokens_first_stage`, `max_tokens`, `unexpected_stop_reason`, `model_mismatch`,
# `http_<status>`, `connect_failed`, `stream_interrupted`, `deadline` or `cancelled`. A try fenced before its
# dispatch marker sent nothing and is not counted.
LLM_CALLS = Counter(
    "jobhunter_llm_calls",
    "HTTP tries of LLM calls, by model, effort, purpose and outcome.",
    ["model", "effort", "purpose", "outcome"],
    registry=REGISTRY,
)
# The prompt-cache read share of the completed calls of the last hour (§10.8): cache_read / (input +
# cache_read + cache_write), by served model, effort and role (the call's purpose); set by `CacheMonitor`.
LLM_CACHE_READ_RATIO = Gauge(
    "jobhunter_llm_cache_read_ratio",
    "Share of prompt tokens read from the cache over a rolling hour, by model, effort and role.",
    ["model", "effort", "role"],
    registry=REGISTRY,
)

# Every critic review whose returned verdict is not its derived outcome (§11.7.2 L4429): the `critic` node of
# the score subgraph counts one after the note's transaction has committed (M3 Task 9; Task 18 pins it).
CRITIC_VERDICT_MISMATCH = Counter(
    "jobhunter_critic_verdict_mismatch",
    "Critic reviews whose returned verdict differs from the derived outcome.",
    registry=REGISTRY,
)


def start_metrics_server(port: int) -> None:
    """Serve `REGISTRY` over HTTP on `port` from a daemon thread."""
    start_http_server(port, registry=REGISTRY)


# Every ordinary table of the application schema (the procrastinate and checkpoint tables live there too),
# with its indexes and TOAST data. A partitioned parent (`relkind = 'p'`) has no storage of its own; its
# partitions are ordinary tables and are listed themselves.
_TABLE_SIZES: Final = q(
    "SELECT c.relname AS table_name, pg_total_relation_size(c.oid) AS bytes FROM pg_class c "
    "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relkind = 'r' "
    "ORDER BY c.relname"
)


async def refresh_table_bytes(conn: AsyncConnection) -> dict[str, int]:
    """Set `TABLE_BYTES` for every table of the `public` schema; returns the sizes in bytes by table name."""
    sizes = {str(row.table_name): int(row.bytes) for row in await conn.execute(_TABLE_SIZES)}
    for table, size in sizes.items():
        TABLE_BYTES.labels(table=table).set(size)
    return sizes
