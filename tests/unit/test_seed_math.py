from datetime import UTC, date, datetime

import pytest
from jobhunter.db.seed import (
    anchor_utc_for,
    canonical_json,
    latest_slot_at_or_before,
    load_settings_v1,
    local_anchor_to_utc,
    scope_payload,
)
from jobhunter.settings.model import Settings
from jobhunter.types import get_bool, get_str
from pydantic import BaseModel

SPEC_FIELDS = [
    "schedule.enabled",
    "schedule.interval_hours",
    "schedule.anchor_local",
    "schedule.include_paid",
    "display.timezone",
    "display.ui_language",
    "scope.locations",
    "scope.remote_within_germany",
    "scope.work_modes",
    "scope.employment_types",
    "scope.seniority_range",
    "scope.industries_include",
    "scope.industries_exclude",
    "scope.accepted_ad_languages",
    "scope.language_policy_default",
    "scope.language_policy",
    "scope.excluded_companies",
    "scope.excluded_keywords",
    "scope.exclude_keywords_in_description",
    "scope.min_salary_eur_year",
    "scope.knockout_severity",
    "scope.max_ad_age_days",
    "evaluation.relevance_threshold",
    "evaluation.t2_band",
    "evaluation.t2_straddle_max_coverage",
    "evaluation.t3_min_mid",
    "evaluation.t8_sample_pct",
    "evaluation.critic_light_triggers",
    "sources.enabled",
    "queries",
    "watchlist",
    "collection.stale_ttl_days",
    "quota.jsearch_monthly_limit",
    "quota.serpapi_monthly_limit",
    "quota.reserve_pct",
    "quota.serpapi_hourly_cap",
    "quota.adzuna_cap_minute",
    "quota.adzuna_cap_day",
    "quota.adzuna_cap_week",
    "quota.adzuna_cap_month",
    "quota.paid_max_requests_per_run",
    "scoring.model_policy",
    "scoring.execution",
    "llm.cap_usd",
    "llm.nonscoring_reserve_pct",
    "llm.overrun_buffer_usd",
    "llm.min_bucket_capacity_usd",
    "scoring.max_targets_per_cycle",
    "scoring.min_cycle_targets",
    "scoring.batch_bulk_rescore",
    "scoring.batch_min_targets",
    "scoring.batch_max_items",
    "scoring.bulk_rescore_max_budget_share",
    "retention.score_history_months",
    "notifications.push_min_severity",
]


def test_settings_v1_has_every_spec_field() -> None:
    s = load_settings_v1()
    for path in SPEC_FIELDS:
        node: object = s
        for part in path.split("."):
            assert isinstance(node, dict) and part in node, path
            node = node[part]
    assert get_bool(s, "scope.remote_within_germany") is True and get_str(s, "llm.cap_usd") == "100.00"
    assert "max_ad_age_days" not in scope_payload(s) and scope_payload(s)["work_modes"] == [
        "remote",
        "hybrid",
        "onsite",
    ]


def _model_fields(model: type[BaseModel]) -> set[str]:
    """The dotted §13.1 field names of the settings model: a group's fields, a top-level list as itself."""
    names = set()
    for name, field in model.model_fields.items():
        group = field.annotation
        if isinstance(group, type) and issubclass(group, BaseModel):
            names |= {f"{name}.{inner}" for inner in group.model_fields}
        else:
            names.add(name)
    return names


def test_the_settings_model_has_exactly_the_spec_fields() -> None:
    # Every §13.1 field is a field of the model (Task 15), and the model has no field §13.1 lacks.
    assert _model_fields(Settings) == set(SPEC_FIELDS)
    assert len(SPEC_FIELDS) == len(set(SPEC_FIELDS))


def test_anchor_summer_and_winter() -> None:
    assert local_anchor_to_utc(date(2026, 10, 16), "06:00", "Europe/Berlin") == datetime(
        2026, 10, 16, 4, 0, tzinfo=UTC
    )
    assert local_anchor_to_utc(date(2026, 12, 1), "06:00", "Europe/Berlin") == datetime(
        2026, 12, 1, 5, 0, tzinfo=UTC
    )


def test_latest_slot() -> None:
    a = datetime(2026, 10, 16, 4, 0, tzinfo=UTC)
    assert latest_slot_at_or_before(a, 21600, datetime(2026, 10, 16, 10, 0, tzinfo=UTC)) == datetime(
        2026, 10, 16, 10, 0, tzinfo=UTC
    )
    assert latest_slot_at_or_before(a, 21600, datetime(2026, 10, 16, 9, 59, tzinfo=UTC)) == a
    assert latest_slot_at_or_before(
        datetime(2026, 12, 1, 5, 0, tzinfo=UTC), 21600, datetime(2026, 12, 1, 3, 0, tzinfo=UTC)
    ) == datetime(2026, 11, 30, 23, 0, tzinfo=UTC)


def test_canonical_json() -> None:
    assert canonical_json({"b": 1, "a": "ü"}) == '{"a":"ü","b":1}'.encode()


@pytest.mark.parametrize("hhmm", ["6:00", "24:00", "06:60", "06:00:00", "", "ab:cd"])
def test_anchor_rejects_a_malformed_time(hhmm: str) -> None:
    with pytest.raises(ValueError, match="HH:MM"):
        local_anchor_to_utc(date(2026, 10, 16), hhmm, "Europe/Berlin")


def test_latest_slot_rejects_a_non_positive_interval() -> None:
    now = datetime(2026, 10, 16, 4, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="interval_s"):
        latest_slot_at_or_before(now, 0, now)


def test_the_anchor_follows_the_local_date_of_now_not_the_utc_date() -> None:
    # 23:30 UTC on the 16th is 01:30 on the 17th in Berlin (CEST): the anchor is 06:00 CEST of the 17th, and
    # the latest slot at or before `now` is the previous one.
    now = datetime(2026, 10, 16, 23, 30, tzinfo=UTC)
    anchor = anchor_utc_for(now, "06:00", "Europe/Berlin")
    assert anchor == datetime(2026, 10, 17, 4, 0, tzinfo=UTC)
    assert latest_slot_at_or_before(anchor, 21600, now) == datetime(2026, 10, 16, 22, 0, tzinfo=UTC)
    # The local midnight is the boundary: 00:00 CET on 2 December is 23:00 UTC on the 1st.
    winter = "06:00", "Europe/Berlin"
    assert anchor_utc_for(datetime(2026, 12, 1, 22, 59, tzinfo=UTC), *winter) == datetime(
        2026, 12, 1, 5, 0, tzinfo=UTC
    )
    assert anchor_utc_for(datetime(2026, 12, 1, 23, 0, tzinfo=UTC), *winter) == datetime(
        2026, 12, 2, 5, 0, tzinfo=UTC
    )
