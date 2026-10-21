"""The cache monitor (spec §10.8; plan Task 5): the zero-read streak per `(model_served, effort, purpose,
prefix_sha256)` and the rolling-1 h `jobhunter_llm_cache_read_ratio{model,effort,role}` gauge."""

from datetime import UTC, datetime, timedelta

from jobhunter.llm.cache_monitor import STREAK_ALERT, CacheAlert, CacheMonitor
from jobhunter.observability.metrics import REGISTRY

T0 = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)
PREFIX_A = bytes.fromhex("aa" * 32)
PREFIX_B = bytes.fromhex("bb" * 32)


def observe(
    monitor: CacheMonitor,
    seconds: float,
    read: int,
    *,
    prefix: bytes | None = PREFIX_A,
    model: str = "claude-sonnet-5-5",
    effort: str = "medium",
    purpose: str = "generate",
    input_tokens: int = 100,
    written: int = 0,
) -> list[CacheAlert]:
    monitor.observe(
        model,
        effort,
        purpose,
        prefix,
        T0 + timedelta(seconds=seconds),
        read,
        input_tokens=input_tokens,
        cache_creation_tokens=written,
    )
    return monitor.drain()


def raised(alerts: list[CacheAlert]) -> list[CacheAlert]:
    return [a for a in alerts if not a.resolve]


def test_three_zero_reads_after_a_first_call_raise_the_streak_alert() -> None:
    monitor = CacheMonitor()
    assert observe(monitor, 0, 0, written=2000) == []  # the first call writes the prefix: no predecessor
    assert observe(monitor, 10, 0) == []  # streak 1
    assert observe(monitor, 20, 0) == []  # streak 2
    (alert,) = observe(monitor, 30, 0)  # streak 3
    assert (alert.code, alert.severity, alert.resolve) == (STREAK_ALERT, "warn", False)
    assert alert.code == "llm.cache_miss_streak"
    assert alert.subject == f"claude-sonnet-5-5/medium/generate/{PREFIX_A.hex()}"
    assert alert.context == {
        "model": "claude-sonnet-5-5",
        "effort": "medium",
        "role": "generate",
        "zero_read_streak": 3,
        "prefix_sha256": [PREFIX_A.hex()],
    }
    assert monitor.streak("claude-sonnet-5-5", "medium", "generate", PREFIX_A) == 3
    assert len(raised(observe(monitor, 40, 0))) == 1  # each further miss is another occurrence


def test_a_cache_read_resets_the_streak_and_resolves_the_alert() -> None:
    monitor = CacheMonitor()
    for second in (0, 10, 20, 30):
        observe(monitor, second, 0)
    (resolved,) = observe(monitor, 40, 500)
    assert resolved.resolve and resolved.subject == f"claude-sonnet-5-5/medium/generate/{PREFIX_A.hex()}"
    assert monitor.streak("claude-sonnet-5-5", "medium", "generate", PREFIX_A) == 0
    assert observe(monitor, 50, 0) == [] and observe(monitor, 60, 0) == []  # streak 1, 2: below 3
    assert len(raised(observe(monitor, 70, 0))) == 1  # 3 again


def test_only_calls_less_than_240_s_apart_count() -> None:
    monitor = CacheMonitor()
    observe(monitor, 0, 0)
    for second in (300, 600, 900, 1200):  # each one more than 240 s after the last: no expectation of a hit
        assert observe(monitor, second, 0) == []
    assert monitor.streak("claude-sonnet-5-5", "medium", "generate", PREFIX_A) == 0
    observe(monitor, 1439, 0)  # 239 s later: counts
    assert monitor.streak("claude-sonnet-5-5", "medium", "generate", PREFIX_A) == 1


def test_keys_are_independent() -> None:
    monitor = CacheMonitor()
    for second in (0, 10, 20):
        observe(monitor, second, 0, prefix=PREFIX_A)
        observe(monitor, second + 1, 0, prefix=PREFIX_B)
        observe(monitor, second + 2, 0, effort="low")
        observe(monitor, second + 3, 0, purpose="revise")
    (alert,) = raised(observe(monitor, 30, 0, prefix=PREFIX_A))
    assert alert.subject.endswith(PREFIX_A.hex())
    assert monitor.streak("claude-sonnet-5-5", "medium", "generate", PREFIX_B) == 2
    assert monitor.streak("claude-sonnet-5-5", "low", "generate", PREFIX_A) == 2
    assert monitor.streak("claude-sonnet-5-5", "medium", "revise", PREFIX_A) == 2
    observe(monitor, 31, 900, prefix=PREFIX_B)  # a hit on B leaves A's streak alone
    assert monitor.streak("claude-sonnet-5-5", "medium", "generate", PREFIX_A) == 3
    # the context names the two most recent prefixes of the model, effort and role
    (again,) = raised(observe(monitor, 40, 0, prefix=PREFIX_A))
    assert again.context["prefix_sha256"] == [PREFIX_A.hex(), PREFIX_B.hex()]


def test_a_call_without_a_cached_prefix_never_streaks() -> None:
    monitor = CacheMonitor()
    for second in range(0, 100, 10):
        assert observe(monitor, second, 0, prefix=None, purpose="extract_profile") == []


def gauge(model: str = "claude-sonnet-5-5", effort: str = "medium", role: str = "generate") -> float | None:
    labels = {"model": model, "effort": effort, "role": role}
    return REGISTRY.get_sample_value("jobhunter_llm_cache_read_ratio", labels)


def test_the_ratio_gauge_follows_the_rolling_hour() -> None:
    monitor = CacheMonitor()
    observe(monitor, 0, 300, input_tokens=100, model="claude-haiku-5-5")  # 300 / 400
    assert gauge("claude-haiku-5-5") == 0.75
    observe(monitor, 60, 0, input_tokens=100, written=200, model="claude-haiku-5-5")  # 300 / 700
    assert gauge("claude-haiku-5-5") == 300 / 700
    # an hour after the first, only the second is in the window
    observe(monitor, 3601, 100, input_tokens=100, model="claude-haiku-5-5")  # (0 + 100) / (300 + 200)
    assert gauge("claude-haiku-5-5") == 100 / 500
    assert monitor.ratio("claude-haiku-5-5", "medium", "generate") == 100 / 500
    # another role has its own series; a call without a cached prefix counts towards its role's ratio
    observe(
        monitor, 3602, 0, input_tokens=1000, model="claude-haiku-5-5", purpose="extract_profile", prefix=None
    )
    assert gauge("claude-haiku-5-5", role="extract_profile") == 0.0
    assert gauge("claude-haiku-5-5") == 100 / 500


def miss(monitor: CacheMonitor, seconds: float) -> None:
    """A zero-read call of key A at `seconds`, its alerts left queued."""
    started = T0 + timedelta(seconds=seconds)
    monitor.observe(
        "claude-sonnet-5-5",
        "medium",
        "generate",
        PREFIX_A,
        started,
        0,
        input_tokens=100,
        cache_creation_tokens=0,
    )


def test_requeued_alerts_are_drained_again_first() -> None:
    monitor = CacheMonitor()
    for second in (0, 10, 20, 30, 40):
        miss(monitor, second)  # streak 3 at 30 s and 4 at 40 s
    first, second_alert = monitor.drain()
    monitor.requeue([first])  # its write failed
    miss(monitor, 50)  # streak 5
    again = monitor.drain()
    assert again[0] is first and len(again) == 2 and monitor.drain() == []
    assert [a.context["zero_read_streak"] for a in (first, second_alert, again[1])] == [3, 4, 5]
