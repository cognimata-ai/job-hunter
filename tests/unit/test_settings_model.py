"""The Pydantic settings model (spec §13.1; plan Task 15): one typed model of `settings_versions.payload`.

- The defaults are the model's: `Settings()` is the seeded document `settings_v1.json`, and validating that
  document gives it back, byte-equal under `canonical_json` (decimals as strings).
- `extra="forbid"` at every level, strict JSON types, set-valued lists in their declared order.
- `test_seed_defaults_validate` is the standing guard of the seeded overrun buffer (pre-flight F30): the
  seeded settings validate with no issue under the constants of `llm/bounds.py` (V19's 2,640-token overhead
  and the 500 batch items of spec revision 3.4).
"""

import copy
from decimal import Decimal
from typing import Any, get_args

import pytest
from jobhunter.db.seed import canonical_json, load_settings_v1
from jobhunter.llm import bounds
from jobhunter.normalize.titles import SENIORITY_LADDER
from jobhunter.settings.model import (
    ModelPolicy,
    Scope,
    ScopeVersionPayload,
    SeniorityLevel,
    Settings,
)
from jobhunter.settings.validate import overrun_buffer_bound, validate_fields
from pydantic import ValidationError


def defaults() -> dict[str, Any]:
    return copy.deepcopy(load_settings_v1())


def test_the_seeded_document_round_trips_byte_equal() -> None:
    seeded = load_settings_v1()
    settings = Settings.model_validate(seeded)
    assert canonical_json(settings.to_payload()) == canonical_json(seeded)


def test_the_model_defaults_are_the_seeded_document() -> None:
    # Every default lives in the model; the packaged seed is that default document as JSON.
    assert canonical_json(Settings().to_payload()) == canonical_json(load_settings_v1())
    assert Settings.model_validate(load_settings_v1()) == Settings()


def test_decimals_are_strings_in_json_and_decimals_in_the_model() -> None:
    settings = Settings()
    assert (settings.llm.cap_usd, settings.llm.overrun_buffer_usd, settings.llm.min_bucket_capacity_usd) == (
        Decimal("100.00"),
        Decimal("2.00"),
        Decimal("1.00"),
    )
    assert settings.evaluation.t2_straddle_max_coverage == Decimal("0.7")
    assert settings.scoring.bulk_rescore_max_budget_share == Decimal("0.4")
    payload = settings.to_payload()
    llm = payload["llm"]
    assert isinstance(llm, dict) and llm["cap_usd"] == "100.00" and llm["overrun_buffer_usd"] == "2.00"
    evaluation = payload["evaluation"]
    assert isinstance(evaluation, dict) and evaluation["t2_straddle_max_coverage"] == "0.7"


def test_a_decimal_keeps_its_written_scale() -> None:
    doc = defaults()
    doc["llm"]["cap_usd"] = "250.5"
    payload = Settings.model_validate(doc).to_payload()
    llm = payload["llm"]
    assert isinstance(llm, dict) and llm["cap_usd"] == "250.5"


@pytest.mark.parametrize(
    ("where", "key"),
    [
        ((), "colour"),
        (("schedule",), "cron"),
        (("scope",), "max_age"),
        (("scope", "seniority_range"), "exact"),
        (("scope", "knockout_severity"), "mandatory_language"),
        (("scoring", "model_policy"), "fallbacks"),
        (("scoring", "model_policy", "nodes"), "embed"),
        (("scoring", "model_policy", "nodes", "generate"), "thinking"),
        (("scoring", "execution", "critic"), "timeout_s"),
        (("llm",), "currency"),
    ],
)
def test_extra_keys_are_rejected_at_every_level(where: tuple[str, ...], key: str) -> None:
    doc = defaults()
    node = doc
    for part in where:
        node = node[part]
    node[key] = "x"
    with pytest.raises(ValidationError):
        Settings.model_validate(doc)
    settings, issues = validate_fields(doc)
    path = ".".join(("payload", *where, key))
    assert settings is None
    assert [(i.field, i.code) for i in issues] == [(path, "invalid")]


def test_an_extra_key_in_a_location_is_rejected() -> None:
    doc = defaults()
    doc["scope"]["locations"] = [
        {"label": "Musterstadt", "gazetteer_id": "g:1", "lat": 50.0, "lon": 8.0, "radius_km": 10, "zip": "1"}
    ]
    _, issues = validate_fields(doc)
    assert [(i.field, i.code) for i in issues] == [("payload.scope.locations[0].zip", "invalid")]


def test_seed_defaults_validate() -> None:
    # The standing guard (pre-flight F30): with the constants of `bounds.py`, the seeded settings need no
    # more than their own $2.00 overrun buffer. 5 x 2,640 x $5.00 + 500 x 2,640 x $1.25 = $1.716 -> $1.72.
    assert set(bounds.PROMPT_OVERHEAD_TOKENS.values()) == {2_640}
    settings, issues = validate_fields(load_settings_v1())
    assert issues == []
    assert settings == Settings()
    assert overrun_buffer_bound(settings) == Decimal("1.72") <= settings.llm.overrun_buffer_usd


def test_json_types_are_strict() -> None:
    for path, value in [
        (("schedule", "interval_hours"), "6"),
        (("schedule", "interval_hours"), 6.0),
        (("schedule", "interval_hours"), True),
        (("schedule", "enabled"), 1),
        (("llm", "cap_usd"), 100),
        (("llm", "cap_usd"), 100.0),
    ]:
        doc = defaults()
        doc[path[0]][path[1]] = value
        _, issues = validate_fields(doc)
        assert [(i.field, i.code) for i in issues] == [(".".join(("payload", *path)), "invalid")], value


def test_a_coordinate_may_be_a_json_integer() -> None:
    doc = defaults()
    doc["scope"]["locations"] = [
        {"label": "Musterstadt", "gazetteer_id": "g:1", "lat": 50, "lon": 8, "radius_km": 10}
    ]
    settings, issues = validate_fields(doc)
    assert issues == [] and settings is not None
    assert (settings.scope.locations[0].lat, settings.scope.locations[0].lon) == (50.0, 8.0)


def test_set_valued_lists_take_their_declared_order() -> None:
    doc = defaults()
    doc["scope"]["work_modes"] = ["onsite", "remote"]
    doc["scope"]["employment_types"] = ["freelance", "full_time"]
    doc["scope"]["accepted_ad_languages"] = ["other", "de"]
    doc["evaluation"]["critic_light_triggers"] = ["T8", "T3"]
    settings = Settings.model_validate(doc)
    assert settings.scope.work_modes == ["remote", "onsite"]
    assert settings.scope.employment_types == ["full_time", "freelance"]
    assert settings.scope.accepted_ad_languages == ["de", "other"]
    assert settings.evaluation.critic_light_triggers == ["T3", "T8"]


def test_a_left_out_key_takes_its_default_and_the_payload_is_complete() -> None:
    doc = defaults()
    del doc["retention"]
    del doc["scope"]["max_ad_age_days"]
    payload = Settings.model_validate(doc).to_payload()
    assert canonical_json(payload) == canonical_json(load_settings_v1())


def test_the_seniority_levels_are_the_ladder() -> None:
    assert get_args(SeniorityLevel) == SENIORITY_LADDER


def test_a_scope_version_payload_has_no_admission_field() -> None:
    scope = Settings().scope
    assert isinstance(scope, Scope) and scope.max_ad_age_days == 30
    stored = ScopeVersionPayload.model_validate(
        {k: v for k, v in Settings().to_payload()["scope"].items() if k != "max_ad_age_days"}  # type: ignore[union-attr]
    )
    assert not hasattr(stored, "max_ad_age_days")
    with pytest.raises(ValidationError):
        ScopeVersionPayload.model_validate({"max_ad_age_days": 30})


def test_the_default_model_policy_is_preset_b() -> None:
    policy = Settings().scoring.model_policy
    assert isinstance(policy, ModelPolicy) and policy.preset == "B" and policy.critic_light_effort == "low"
    assert {node: (c.model, c.effort) for node, c in policy.nodes.by_node().items()} == {
        "generate": ("claude-sonnet-5-5", "medium"),
        "revise": ("claude-sonnet-5-5", "medium"),
        "critic": ("claude-opus-5-5", "medium"),
        "extract_profile": ("claude-opus-5-5", "medium"),
        "propose_queries": ("claude-opus-5-5", "medium"),
    }
    execution = Settings().scoring.execution.by_node()
    assert {node: (e.max_tokens, e.max_tokens_retry, e.deadline_s) for node, e in execution.items()} == {
        "generate": (6000, 12000, 600),
        "revise": (6000, 12000, 600),
        "critic": (6000, 12000, 600),
        "extract_profile": (16000, 32000, 900),
        "propose_queries": (8000, 16000, 300),
    }


def test_the_model_is_immutable() -> None:
    settings = Settings()
    with pytest.raises(ValidationError):
        settings.schedule.interval_hours = 3  # type: ignore[misc]
