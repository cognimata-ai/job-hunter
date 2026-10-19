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
# The pages the fetch nodes acquired (one per receipt they ended on), by source and receipt status.
FETCH_REQUESTS = Counter(
    "jobhunter_fetch_requests",
    "Fetch request pages acquired by the fetch nodes.",
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
