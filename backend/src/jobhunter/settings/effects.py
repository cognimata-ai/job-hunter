"""The effect classes of a settings change (spec §13.1's effect column, §13.3; plan Task 16).

`FIELD_EFFECTS` is §13.1's effect column, one entry per field: the `scope.*` fields give `scope` (the
`scope_versions` row), `scope.locations` and `scope.remote_within_germany` also `collection` (their cells),
and `scope.max_ad_age_days` gives `admission` only, as it is not stored with the scope version.

`classify_effects` compares the two documents' **normal forms** (`Settings.to_payload()`, plan Deviation
38), field by field, as canonical JSON. A request that differs from the active document only in formatting
(`"0.70"` for `"0.7"`, `"Zalando SE"` for the stored key `zalando`, a reordered set, a key left out for its
default) has the same normal form and is no change. The result is the union of the changed fields' classes
without `none`; it is `{"none"}` when nothing changed, or only fields of class `none` (`display.ui_language`,
`retention.*`, `notifications.*`). `display.timezone` is `schedule` (spec 3.5, U3): it is the zone of
`schedule.anchor_local`, which a save converts into `anchor_utc` in that zone, so a change of zone alone runs
the schedule action and re-anchors the schedule.

Each derived row (§13.2) changes exactly when its class appears, because both hash the same normal form:
the scope (every `scope.*` field but `max_ad_age_days`) with `scope`, the whole `scoring.model_policy` object
with `llm_tuple`, `scoring.execution` with `execution`, and `evaluation` with `evaluation_policy`. The model
policy's `preset` is part of that object, but its normal form is the label of the grid (`settings.presets`,
M4 Task 14, D20, replacing Task 16 ruling 2), so a label-only change is no change: it has the active normal
form, no effect class and no new `model_policies` row.

`diff_payloads` (Task 17) lists what changed between two documents, for `GET /api/settings/versions/{id}`:
one `DiffEntry` per changed leaf, at its `FieldIssue` path (`payload.scope.locations[0].radius_km`,
`settings.validate.field_path`), in the order of the `after` document (keys only `before` has come after its
own). The callers pass normal forms, so a formatting difference is none here either. Values compare as
canonical JSON, so `true` is never `1`. `None` stands for a side where the value is absent: a list entry or a
map key that only one side has (a stored document has every other key). Lists:
- a list whose entries are all objects with one of `ID_KEYS` (`query_id`, `watchlist_company_id`,
  `gazetteer_id`; §13.1), unique on each side, is matched by that key: an entry of both sides is compared
  field by field at its index in `after`, a new entry is reported whole at its index in `after`, a removed one
  whole at its index in `before`. A move alone is no entry, as the run view keys these lists by id;
- any other list compares by position, an entry beyond the shorter list against `None`.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal, get_args

from jobhunter.db.seed import canonical_json
from jobhunter.settings.model import Settings
from jobhunter.settings.validate import ROOT
from jobhunter.types import JSONValue, json_get

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
        "display.timezone": _SCHEDULE,
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


# The id keys of §13.1's lists of objects (`queries[]`, `watchlist[]`, `scope.locations[]`), tried in order.
ID_KEYS: Final[tuple[str, ...]] = ("query_id", "watchlist_company_id", "gazetteer_id")


@dataclass(frozen=True)
class DiffEntry:
    """One changed leaf: its path and its value on each side (`None` where absent; module docstring)."""

    path: str
    before: JSONValue
    after: JSONValue


def diff_payloads(before: JSONValue, after: JSONValue) -> list[DiffEntry]:
    """What changed from `before` to `after`, one entry per changed leaf (module docstring)."""
    entries: list[DiffEntry] = []
    _diff(ROOT, before, after, entries)
    return entries


def _same(a: JSONValue, b: JSONValue) -> bool:
    return canonical_json(a) == canonical_json(b)


def _diff(path: str, before: JSONValue, after: JSONValue, out: list[DiffEntry]) -> None:
    if isinstance(before, dict) and isinstance(after, dict):
        for key in [*after, *(key for key in before if key not in after)]:
            _diff(f"{path}.{key}", before.get(key), after.get(key), out)
    elif isinstance(before, list) and isinstance(after, list):
        id_key = _id_key(before, after)
        if id_key is None:
            for i in range(max(len(before), len(after))):
                old = before[i] if i < len(before) else None
                new = after[i] if i < len(after) else None
                _diff(f"{path}[{i}]", old, new, out)
        else:
            _diff_keyed(path, before, after, id_key, out)
    elif not _same(before, after):
        out.append(DiffEntry(path, before, after))


def _ids(entries: Sequence[JSONValue], key: str) -> list[bytes] | None:
    """The canonical id of each entry, or `None` when an entry is no object with `key` or an id repeats."""
    ids = [canonical_json(entry[key]) for entry in entries if isinstance(entry, dict) and key in entry]
    return ids if len(ids) == len(entries) and len(set(ids)) == len(ids) else None


def _id_key(before: list[JSONValue], after: list[JSONValue]) -> str | None:
    if not before and not after:
        return None
    for key in ID_KEYS:
        if _ids(before, key) is not None and _ids(after, key) is not None:
            return key
    return None


def _diff_keyed(
    path: str, before: list[JSONValue], after: list[JSONValue], key: str, out: list[DiffEntry]
) -> None:
    old = {
        ident: (i, entry)
        for i, (ident, entry) in enumerate(zip(_ids(before, key) or [], before, strict=True))
    }
    seen: set[bytes] = set()
    for j, (ident, entry) in enumerate(zip(_ids(after, key) or [], after, strict=True)):
        seen.add(ident)
        if ident in old:
            _diff(f"{path}[{j}]", old[ident][1], entry, out)
        else:
            out.append(DiffEntry(f"{path}[{j}]", None, entry))
    for ident, (i, entry) in old.items():
        if ident not in seen:
            out.append(DiffEntry(f"{path}[{i}]", entry, None))
