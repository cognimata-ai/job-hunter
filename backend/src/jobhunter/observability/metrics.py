"""Prometheus metrics on a dedicated registry, so nothing but these series is exposed."""

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, start_http_server

REGISTRY = CollectorRegistry()

RUNS = Counter("jobhunter_runs", "Runs that reached a terminal status.", ["status"], registry=REGISTRY)
RUN_DURATION = Histogram("jobhunter_run_duration_seconds", "Duration of a run.", registry=REGISTRY)
HTTP_REQUESTS = Counter(
    "jobhunter_http_requests", "HTTP requests served.", ["route", "status"], registry=REGISTRY
)
TABLE_BYTES = Gauge(
    "jobhunter_table_bytes", "Size of a database table in bytes.", ["table"], registry=REGISTRY
)
ALERTS_OPEN = Gauge("jobhunter_alerts_open", "Open alerts by severity.", ["severity"], registry=REGISTRY)


def start_metrics_server(port: int) -> None:
    """Serve `REGISTRY` over HTTP on `port` from a daemon thread."""
    start_http_server(port, registry=REGISTRY)
