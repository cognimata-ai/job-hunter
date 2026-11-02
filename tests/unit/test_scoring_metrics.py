"""The scoring series of §17.5 (L5538-5539; plan M3 Task 18, Ruling P28) and the health section's keys.

`jobhunter_critic_rate_7d` and `jobhunter_oldest_pending_target_age_seconds` are unlabelled gauges,
`jobhunter_score_targets` a gauge per target state, `jobhunter_claim_reclaims_total` a counter, and Task 9's
`jobhunter_critic_verdict_mismatch_total` a counter: all on the dedicated registry. The system health gains
the `scoring` section (L5552 "oldest pending target age").
"""

from jobhunter.observability import health, metrics
from prometheus_client import generate_latest


def _families() -> dict[str, str]:
    """Every metric family of the dedicated registry, by name, with its type."""
    return {family.name: family.type for family in metrics.REGISTRY.collect()}


def test_the_scoring_series_exist_with_the_spec_names_and_types() -> None:
    families = _families()
    assert families["jobhunter_critic_rate_7d"] == "gauge"
    assert families["jobhunter_score_targets"] == "gauge"
    assert families["jobhunter_oldest_pending_target_age_seconds"] == "gauge"
    # a counter's family is named without `_total`; its sample carries it (the spec's series name)
    assert families["jobhunter_claim_reclaims"] == "counter"
    assert families["jobhunter_critic_verdict_mismatch"] == "counter"  # Task 9's, pinned here (Ruling P28)


def test_the_scoring_series_carry_the_spec_labels() -> None:
    metrics.CRITIC_RATE_7D.set(0.25)
    metrics.SCORE_TARGETS.labels(state="pending").set(7)
    metrics.OLDEST_PENDING_TARGET_AGE.set(90)
    reclaims = metrics.REGISTRY.get_sample_value("jobhunter_claim_reclaims_total") or 0.0
    metrics.CLAIM_RECLAIMS.inc(2)
    mismatches = metrics.REGISTRY.get_sample_value("jobhunter_critic_verdict_mismatch_total") or 0.0
    metrics.CRITIC_VERDICT_MISMATCH.inc()
    text = generate_latest(metrics.REGISTRY).decode()
    assert "jobhunter_critic_rate_7d 0.25" in text
    assert 'jobhunter_score_targets{state="pending"} 7.0' in text
    assert "jobhunter_oldest_pending_target_age_seconds 90.0" in text
    assert metrics.REGISTRY.get_sample_value("jobhunter_claim_reclaims_total") == reclaims + 2
    assert metrics.REGISTRY.get_sample_value("jobhunter_critic_verdict_mismatch_total") == mismatches + 1


def test_every_score_target_state_is_a_label_of_the_gauge() -> None:
    # the registry's sixteen states (§7.11), each reported, 0 included, by `metrics_refresh`
    assert metrics.SCORE_TARGET_STATES == (
        "budget_deferred",
        "cancelled",
        "deferred_duplicate",
        "expired_unscored",
        "failed_attempts_exhausted",
        "failed_invalid_output",
        "failed_max_tokens",
        "failed_request",
        "failed_transient",
        "filtered_out",
        "in_batch",
        "in_progress",
        "pending",
        "published_accepted",
        "published_contested",
        "refused",
    )


def test_the_health_section_of_the_score_lane_has_its_keys() -> None:
    assert set(health.ScoringHealthOut.model_fields) == {
        "running_cycle_id",
        "pause_reason",
        "oldest_pending_target_age_s",
        "pending_targets",
        "in_batch_targets",
        "quarantined_batch_usd",
    }
    assert health.SystemHealthOut.model_fields["scoring"].annotation is health.ScoringHealthOut
