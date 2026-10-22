"""The effect classes of a settings change (spec §13.1's effect column, §13.3; plan Task 16).

`FIELD_EFFECTS` is §13.1's effect column, one entry per field: the `scope.*` fields give `scope` (the
`scope_versions` row), `scope.locations` and `scope.remote_within_germany` also `collection` (their cells),
and `scope.max_ad_age_days` gives `admission` only, as it is not stored with the scope version.

`classify_effects` compares the two documents' **normal forms** (`Settings.to_payload()`, plan Deviation
38), field by field, as canonical JSON. A request that differs from the active document only in formatting
(`"0.70"` for `"0.7"`, `"Zalando SE"` for the stored key `zalando`, a reordered set, a key left out for its
default) has the same normal form and is no change. The result is the union of the changed fields' classes
without `none`; it is `{"none"}` when nothing changed, or only fields of class `none` (`display.*`,
`retention.*`, `notifications.*`).

Each derived row (§13.2) changes exactly when its class appears, because both hash the same normal form:
the scope (every `scope.*` field but `max_ad_age_days`) with `scope`, the whole `scoring.model_policy` object
with `llm_tuple`, `scoring.execution` with `execution`, and `evaluation` with `evaluation_policy`. That
includes the model policy's `preset`: §13.1 lists it inside `scoring.model_policy` (effect `llm_tuple`),
and its hash covers the label, so a label-only preset change derives a new `model_policies` row and is an
`llm_tuple` change (Task 16 ruling 2).
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, Literal, get_args

from jobhunter.db.seed import canonical_json
from jobhunter.settings.model import Settings
from jobhunter.types import json_get

EffectClass = Literal[
    "none",
    "schedule",
    "collection",
    "admission",
    "budget",
    "scope",
    "execution",
    "evaluation_policy",
    "llm_tuple",
]
EFFECT_CLASSES: Final[tuple[EffectClass, ...]] = get_args(EffectClass)

_NONE: Final[frozenset[EffectClass]] = frozenset({"none"})
_SCHEDULE: Final[frozenset[EffectClass]] = frozenset({"schedule"})
_COLLECTION: Final[frozenset[EffectClass]] = frozenset({"collection"})
_SCOPE: Final[frozenset[EffectClass]] = frozenset({"scope"})
_SCOPE_COLLECTION: Final[frozenset[EffectClass]] = frozenset({"scope", "collection"})
_BUDGET: Final[frozenset[EffectClass]] = frozenset({"budget"})
_EVALUATION: Final[frozenset[EffectClass]] = frozenset({"evaluation_policy"})

# §13.1's effect column, by the dotted path of each field in the settings document.
FIELD_EFFECTS: Final[Mapping[str, frozenset[EffectClass]]] = MappingProxyType(
    {
        "schedule.enabled": _SCHEDULE,
        "schedule.interval_hours": _SCHEDULE,
        "schedule.anchor_local": _SCHEDULE,
        "schedule.include_paid": _COLLECTION,
        "display.timezone": _NONE,
        "display.ui_language": _NONE,
        "scope.locations": _SCOPE_COLLECTION,
        "scope.remote_within_germany": _SCOPE_COLLECTION,
        "scope.work_modes": _SCOPE,
        "scope.employment_types": _SCOPE,
        "scope.seniority_range": _SCOPE,
        "scope.industries_include": _SCOPE,
        "scope.industries_exclude": _SCOPE,
        "scope.accepted_ad_languages": _SCOPE,
        "scope.language_policy_default": _SCOPE,
        "scope.language_policy": _SCOPE,
        "scope.excluded_companies": _SCOPE,
        "scope.excluded_keywords": _SCOPE,
        "scope.exclude_keywords_in_description": _SCOPE,
        "scope.min_salary_eur_year": _SCOPE,
        "scope.knockout_severity": _SCOPE,
        "scope.max_ad_age_days": frozenset({"admission"}),
        "evaluation.relevance_threshold": _EVALUATION,
        "evaluation.t2_band": _EVALUATION,
        "evaluation.t2_straddle_max_coverage": _EVALUATION,
        "evaluation.t3_min_mid": _EVALUATION,
        "evaluation.t8_sample_pct": _EVALUATION,
        "evaluation.critic_light_triggers": _EVALUATION,
        "sources.enabled": _COLLECTION,
        "queries": _COLLECTION,
        "watchlist": _COLLECTION,
        "collection.stale_ttl_days": _COLLECTION,
        "quota.jsearch_monthly_limit": _COLLECTION,
        "quota.serpapi_monthly_limit": _COLLECTION,
        "quota.reserve_pct": _COLLECTION,
        "quota.serpapi_hourly_cap": _COLLECTION,
        "quota.adzuna_cap_minute": _COLLECTION,
        "quota.adzuna_cap_day": _COLLECTION,
        "quota.adzuna_cap_week": _COLLECTION,
        "quota.adzuna_cap_month": _COLLECTION,
        "quota.paid_max_requests_per_run": _COLLECTION,
        "scoring.model_policy": frozenset({"llm_tuple"}),
        "scoring.execution": frozenset({"execution"}),
        "llm.cap_usd": _BUDGET,
        "llm.nonscoring_reserve_pct": _BUDGET,
        "llm.overrun_buffer_usd": _BUDGET,
        "llm.min_bucket_capacity_usd": _BUDGET,
        "scoring.max_targets_per_cycle": _BUDGET,
        "scoring.min_cycle_targets": _BUDGET,
        "scoring.batch_bulk_rescore": _BUDGET,
        "scoring.batch_min_targets": _BUDGET,
        "scoring.batch_max_items": _BUDGET,
        "scoring.bulk_rescore_max_budget_share": _BUDGET,
        "retention.score_history_months": _NONE,
        "notifications.push_min_severity": _NONE,
    }
)


def classify_effects(active: Settings, proposed: Settings) -> frozenset[EffectClass]:
    """The classes that activating `proposed` over `active` triggers (module docstring)."""
    before, after = active.to_payload(), proposed.to_payload()
    classes: set[EffectClass] = set()
    for path, effects in FIELD_EFFECTS.items():
        if canonical_json(json_get(before, path)) != canonical_json(json_get(after, path)):
            classes |= effects
    classes.discard("none")
    return frozenset(classes) if classes else _NONE
