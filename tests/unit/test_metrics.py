# tests/unit/test_metrics.py
from jobhunter.observability import metrics
from prometheus_client import REGISTRY as GLOBAL_REGISTRY
from prometheus_client import generate_latest


def test_series_names() -> None:
    metrics.RUNS.labels(status="succeeded").inc()
    metrics.HTTP_REQUESTS.labels(route="/healthz", status="200").inc()
    metrics.ALERTS_OPEN.labels(severity="warn").set(2)
    text = generate_latest(metrics.REGISTRY).decode()
    for name in (
        "jobhunter_runs_total",
        "jobhunter_run_duration_seconds",
        "jobhunter_http_requests_total",
        "jobhunter_table_bytes",
        'jobhunter_alerts_open{severity="warn"} 2.0',
    ):
        assert name in text


def test_metrics_live_on_a_dedicated_registry() -> None:
    assert metrics.REGISTRY is not GLOBAL_REGISTRY
    assert "jobhunter_" not in generate_latest(GLOBAL_REGISTRY).decode()


def test_metrics_server_serves_the_dedicated_registry(monkeypatch) -> None:
    calls: list[tuple[int, object]] = []
    monkeypatch.setattr(metrics, "start_http_server", lambda port, registry: calls.append((port, registry)))
    metrics.start_metrics_server(9100)
    assert calls == [(9100, metrics.REGISTRY)]


def test_run_duration_buckets_cover_minutes_to_an_hour() -> None:
    def bucket(le: str) -> float:
        return metrics.REGISTRY.get_sample_value("jobhunter_run_duration_seconds_bucket", {"le": le}) or 0.0

    before = {le: bucket(le) for le in ("1.0", "120.0", "3600.0")}
    metrics.RUN_DURATION.observe(90)
    assert bucket("1.0") == before["1.0"]
    assert bucket("120.0") == before["120.0"] + 1
    assert bucket("3600.0") == before["3600.0"] + 1
    text = generate_latest(metrics.REGISTRY).decode()
    assert 'jobhunter_run_duration_seconds_bucket{le="10.0"}' not in text
