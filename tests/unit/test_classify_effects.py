"""The effect classes of a settings change (spec §13.1 effect column, §13.3; plan Task 16).

`classify_effects` compares the normal forms (`Settings.to_payload()`, plan Deviation 38), never the raw
request: a change of formatting alone is no change. Each §13.1 field below is changed alone and gives its
column's classes; several changes give their union; and every class a derived row stands for appears exactly
when that row's payload changes (`derive.derived`), so the classifier and the row hashes never disagree.
"""

import copy
from collections.abc import Callable
from typing import Any

import pytest
from jobhunter.db.seed import content_hash, load_settings_v1
from jobhunter.settings.derive import derived
from jobhunter.settings.effects import EFFECT_CLASSES, FIELD_EFFECTS, classify_effects
from jobhunter.settings.model import Settings

from tests.unit.test_seed_math import SPEC_FIELDS

LOCATION = {"label": "Musterstadt", "gazetteer_id": "gz-0001", "lat": 52.1, "lon": 9.9, "radius_km": 25}
SCHEDULE = frozenset({"schedule"})
COLLECTION = frozenset({"collection"})
SCOPE = frozenset({"scope"})
SCOPE_COLLECTION = frozenset({"scope", "collection"})
ADMISSION = frozenset({"admission"})
EVALUATION = frozenset({"evaluation_policy"})
LLM_TUPLE = frozenset({"llm_tuple"})
EXECUTION = frozenset({"execution"})
BUDGET = frozenset({"budget"})
NONE = frozenset({"none"})


def at(path: str, value: Any) -> Callable[[dict[str, Any]], None]:
    """A change that sets the dotted `path` of a payload to `value`."""

    def change(doc: dict[str, Any]) -> None:
        *parents, key = path.split(".")
        node = doc
        for part in parents:
            node = node[part]
        node[key] = value

    return change


def proposed(*changes: Callable[[dict[str, Any]], None]) -> Settings:
    doc: dict[str, Any] = copy.deepcopy(load_settings_v1())
    for change in changes:
        change(doc)
    return Settings.model_validate(doc)


KNOCKOUTS = {
    "degree": "hard",
    "certification": "soft",
    "security_clearance": "hard",
    "work_permit": "hard",
    "onsite_required": "hard",
    "contract_type": "hard",
}
ENABLED = {"jsearch": True, "serpapi": True, "adzuna": False, "arbeitnow": True, "ats": True}
NODES = {
    "generate": {"model": "claude-sonnet-5-5", "effort": "high"},
    "revise": {"model": "claude-sonnet-5-5", "effort": "medium"},
    "critic": {"model": "claude-opus-5-5", "effort": "medium"},
    "extract_profile": {"model": "claude-opus-5-5", "effort": "medium"},
    "propose_queries": {"model": "claude-opus-5-5", "effort": "medium"},
}
STAGE = {"max_tokens": 6000, "max_tokens_retry": 12000, "deadline_s": 900}

# One change per §13.1 field and the classes of its effect column (written out here, not read from the code).
CASES: list[tuple[str, Any, frozenset[str]]] = [
    ("schedule.enabled", False, SCHEDULE),
    ("schedule.interval_hours", 12, SCHEDULE),
    ("schedule.anchor_local", "07:30", SCHEDULE),
    ("schedule.include_paid", False, COLLECTION),
    ("display.timezone", "Europe/Vienna", SCHEDULE),
    ("display.ui_language", "de", NONE),
    ("scope.locations", [LOCATION], SCOPE_COLLECTION),
    ("scope.remote_within_germany", False, SCOPE_COLLECTION),
    ("scope.work_modes", ["remote"], SCOPE),
    ("scope.employment_types", ["full_time"], SCOPE),
    (
        "scope.seniority_range",
        {"mode": "explicit", "below": 1, "above": 1, "min": "junior", "max": "senior"},
        SCOPE,
    ),
    ("scope.industries_include", ["fintech"], SCOPE),
    ("scope.industries_exclude", ["gambling"], SCOPE),
    ("scope.accepted_ad_languages", ["de"], SCOPE),
    ("scope.language_policy_default", "soft", SCOPE),
    ("scope.language_policy", {"fr": "soft"}, SCOPE),
    ("scope.excluded_companies", ["Beispiel GmbH"], SCOPE),
    ("scope.excluded_keywords", ["unpaid"], SCOPE),
    ("scope.exclude_keywords_in_description", True, SCOPE),
    ("scope.min_salary_eur_year", 50_000, SCOPE),
    ("scope.knockout_severity", KNOCKOUTS, SCOPE),
    ("scope.max_ad_age_days", 14, ADMISSION),
    ("evaluation.relevance_threshold", 55, EVALUATION),
    ("evaluation.t2_band", 3, EVALUATION),
    ("evaluation.t2_straddle_max_coverage", "0.65", EVALUATION),
    ("evaluation.t3_min_mid", 85, EVALUATION),
    ("evaluation.t8_sample_pct", 0, EVALUATION),
    ("evaluation.critic_light_triggers", ["T2", "T3", "T8"], EVALUATION),
    ("sources.enabled", ENABLED, COLLECTION),
    ("queries", [{"query_id": 1, "enabled": True}], COLLECTION),
    ("watchlist", [{"watchlist_company_id": 1, "enabled": True}], COLLECTION),
    ("collection.stale_ttl_days", 10, COLLECTION),
    ("quota.jsearch_monthly_limit", 300, COLLECTION),
    ("quota.serpapi_monthly_limit", 100, COLLECTION),
    ("quota.reserve_pct", 20, COLLECTION),
    ("quota.serpapi_hourly_cap", 30, COLLECTION),
    ("quota.adzuna_cap_minute", 10, COLLECTION),
    ("quota.adzuna_cap_day", 200, COLLECTION),
    ("quota.adzuna_cap_week", 800, COLLECTION),
    ("quota.adzuna_cap_month", 2000, COLLECTION),
    ("quota.paid_max_requests_per_run", 20, COLLECTION),
    ("scoring.model_policy.nodes", NODES, LLM_TUPLE),
    ("scoring.execution.generate", STAGE, EXECUTION),
    ("llm.cap_usd", "150.00", BUDGET),
    ("llm.nonscoring_reserve_pct", 10, BUDGET),
    ("llm.overrun_buffer_usd", "3.00", BUDGET),
    ("llm.min_bucket_capacity_usd", "2.00", BUDGET),
    ("scoring.max_targets_per_cycle", 30, BUDGET),
    ("scoring.min_cycle_targets", 20, BUDGET),
    ("scoring.batch_bulk_rescore", False, BUDGET),
    ("scoring.batch_min_targets", 300, BUDGET),
    ("scoring.batch_max_items", 1000, BUDGET),
    ("scoring.bulk_rescore_max_budget_share", "0.5", BUDGET),
    ("retention.score_history_months", 6, NONE),
    ("notifications.push_min_severity", "critical", NONE),
]


def _field(path: str) -> str:
    """The §13.1 field a changed path belongs to (`scoring.model_policy.nodes` → `scoring.model_policy`)."""
    return ".".join(path.split(".")[:2]) if "." in path else path


def test_every_spec_field_is_classified_and_changed_once() -> None:
    assert set(FIELD_EFFECTS) == set(SPEC_FIELDS)
    assert sorted(_field(path) for path, _, _ in CASES) == sorted(SPEC_FIELDS)
    assert set(EFFECT_CLASSES) == {
        "none",
        "schedule",
        "collection",
        "admission",
        "budget",
        "scope",
        "execution",
        "evaluation_policy",
        "llm_tuple",
    }


@pytest.mark.parametrize(("path", "value", "classes"), CASES, ids=[case[0] for case in CASES])
def test_one_field_gives_its_effect_column(path: str, value: Any, classes: frozenset[str]) -> None:
    assert classify_effects(Settings(), proposed(at(path, value))) == classes


def test_no_change_is_none() -> None:
    assert classify_effects(Settings(), Settings()) == NONE
    assert classify_effects(Settings(), proposed()) == NONE


def test_several_changes_give_their_union() -> None:
    changes = (
        at("schedule.interval_hours", 12),
        at("scope.locations", [LOCATION]),
        at("llm.cap_usd", "150.00"),
        at("scoring.execution.critic", STAGE),
    )
    assert classify_effects(Settings(), proposed(*changes)) == {
        "schedule",
        "scope",
        "collection",
        "budget",
        "execution",
    }
    everything = [at(path, value) for path, value, _ in CASES]
    assert classify_effects(Settings(), proposed(*everything)) == set(EFFECT_CLASSES) - {"none"}


def test_none_is_kept_only_when_nothing_else_applies() -> None:
    both_none = (at("display.ui_language", "de"), at("retention.score_history_months", 6))
    assert classify_effects(Settings(), proposed(*both_none)) == NONE
    with_schedule = (at("display.ui_language", "de"), at("schedule.enabled", False))
    assert classify_effects(Settings(), proposed(*with_schedule)) == SCHEDULE


def test_the_comparison_is_symmetric() -> None:
    changed = proposed(at("scope.max_ad_age_days", 14), at("evaluation.t2_band", 3))
    assert (
        classify_effects(changed, Settings())
        == classify_effects(Settings(), changed)
        == {
            "admission",
            "evaluation_policy",
        }
    )


# Each derived row (§13.2) and the class whose activation creates or selects it (§13.3).
ROW_CLASSES = {
    "scope": "scope",
    "model_policy": "llm_tuple",
    "execution": "execution",
    "evaluation": "evaluation_policy",
}


@pytest.mark.parametrize(
    ("active", "sent"),
    [
        (at("evaluation.t2_straddle_max_coverage", "0.7"), at("evaluation.t2_straddle_max_coverage", "0.70")),
        (
            at("scoring.bulk_rescore_max_budget_share", "0.4"),
            at("scoring.bulk_rescore_max_budget_share", "0.40"),
        ),
        (at("llm.cap_usd", "100.00"), at("llm.cap_usd", "100")),
        (at("scope.excluded_companies", ["zalando"]), at("scope.excluded_companies", ["Zalando SE"])),
        (at("scope.industries_exclude", ["fintech"]), at("scope.industries_exclude", ["FinTech"])),
        (
            at("scope.work_modes", ["remote", "hybrid", "onsite"]),
            at("scope.work_modes", ["onsite", "remote", "hybrid"]),
        ),
        (
            at("evaluation.critic_light_triggers", ["T2", "T8"]),
            at("evaluation.critic_light_triggers", ["T8", "T2"]),
        ),
    ],
    ids=[
        "rate-trailing-zero",
        "share-trailing-zero",
        "usd-scale",
        "company-key",
        "industry-fold",
        "set-order",
        "trigger-order",
    ],
)
def test_formatting_alone_is_no_change(active, sent) -> None:
    """Ruling 1: the normal forms are compared, so a request that differs only in formatting gives `none`,
    and no derived row (§13.2) changes either."""
    assert classify_effects(proposed(active), proposed(sent)) == NONE
    before, after = derived(proposed(active)), derived(proposed(sent))
    for row in ROW_CLASSES:
        assert content_hash(getattr(before, row)) == content_hash(getattr(after, row)), row


def test_keys_left_out_take_their_defaults() -> None:
    assert classify_effects(Settings(), Settings.model_validate({})) == NONE
    assert classify_effects(Settings(), Settings.model_validate({"schedule": {"interval_hours": 6}})) == NONE


def test_a_label_only_preset_change_is_no_change() -> None:
    """M4 D20 (replacing Task 16 ruling 2): the stored `preset` is the label of the grid (the label normal
    form, `settings.presets`), so a label-only change has the active normal form and derives no new row."""
    custom = proposed(at("scoring.model_policy.preset", "custom"))
    assert custom.scoring.model_policy == Settings().scoring.model_policy
    assert classify_effects(Settings(), custom) == NONE
    assert content_hash(derived(custom).model_policy) == content_hash(derived(Settings()).model_policy)


@pytest.mark.parametrize(("path", "value", "classes"), CASES, ids=[case[0] for case in CASES])
def test_a_derived_row_changes_exactly_with_its_class(path: str, value: Any, classes: frozenset[str]) -> None:
    before, after = derived(Settings()), derived(proposed(at(path, value)))
    effects = classify_effects(Settings(), proposed(at(path, value)))
    for row, effect in ROW_CLASSES.items():
        row_changed = content_hash(getattr(before, row)) != content_hash(getattr(after, row))
        assert row_changed == (effect in effects), (path, row)
