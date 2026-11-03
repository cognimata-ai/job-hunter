"""The model-policy presets and the label normal form (spec §10.3, §13.1, §13.3, §21; plan M4 Task 14, D20).

`PRESETS` holds the three grids: A puts the generator, the reviser and the critic on Opus, B is the settings
model's default grid, C puts the generator and the reviser on Haiku and the critic on Sonnet; all three run
`extract_profile` and `propose_queries` on Opus and light critic rounds at `low`. `label_for` names a grid's
preset, `custom` for any other grid, and `ModelPolicy` stores that label whatever a request says, so a
label-only change is no change and derives no new `model_policies` row.
"""

import copy
from decimal import Decimal
from typing import Any

import pytest
from jobhunter.db.seed import content_hash, load_settings_v1
from jobhunter.settings import presets
from jobhunter.settings.derive import derived
from jobhunter.settings.effects import classify_effects
from jobhunter.settings.llm_projection import cache_writes_per_cycle, central_cost_per_target
from jobhunter.settings.model import ModelPolicy, ModelPolicyNodes, Settings

OPUS, SONNET, HAIKU = "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"


def grid(generate: str, revise: str, critic: str) -> dict[str, Any]:
    """The JSON of a grid at `medium`, `extract_profile` and `propose_queries` on Opus."""

    def node(model: str) -> dict[str, str]:
        return {"model": model, "effort": "medium"}

    return {
        "generate": node(generate),
        "revise": node(revise),
        "critic": node(critic),
        "extract_profile": node(OPUS),
        "propose_queries": node(OPUS),
    }


# §10.3 and §13.1, as the brief gives them
BRIEF_GRIDS = {"A": grid(OPUS, OPUS, OPUS), "B": grid(SONNET, SONNET, OPUS), "C": grid(HAIKU, HAIKU, SONNET)}


def policy(nodes: dict[str, Any], *, preset: str = "custom", light: str = "low") -> ModelPolicy:
    return ModelPolicy.model_validate({"preset": preset, "nodes": nodes, "critic_light_effort": light})


def test_each_preset_grid_is_labelled_by_its_letter() -> None:
    assert set(presets.PRESETS) == {"A", "B", "C"}
    for label, preset in presets.PRESETS.items():
        assert preset.nodes.model_dump(mode="json") == BRIEF_GRIDS[label], label
        assert preset.critic_light_effort == "low"
        assert presets.label_for(preset.nodes, preset.critic_light_effort) == label
        assert preset.policy().preset == label
        assert policy(BRIEF_GRIDS[label]).preset == label  # a grid sent as JSON with another label
    # B is the settings model's own default, not a second copy of it
    assert presets.PRESETS["B"].nodes == ModelPolicyNodes()
    assert presets.PRESETS["B"].policy() == ModelPolicy() == Settings().scoring.model_policy
    # a fourth grid: one effort, one model, or the light effort away from a preset
    effort = copy.deepcopy(BRIEF_GRIDS["B"])
    effort["critic"]["effort"] = "high"
    model = copy.deepcopy(BRIEF_GRIDS["C"])
    model["propose_queries"]["model"] = SONNET
    for nodes, light in ((effort, "low"), (model, "low"), (BRIEF_GRIDS["A"], "medium")):
        assert policy(nodes, preset="A", light=light).preset == "custom"
        assert presets.label_for(ModelPolicyNodes.model_validate(nodes), light) == "custom"


def test_a_request_label_is_ignored_by_the_normal_form() -> None:
    assert policy(BRIEF_GRIDS["B"], preset="C").preset == "B"
    assert ModelPolicy(preset="custom").preset == "B"
    doc: dict[str, Any] = copy.deepcopy(load_settings_v1())
    doc["scoring"]["model_policy"]["preset"] = "C"
    stored = Settings.model_validate(doc).to_payload()
    assert stored == load_settings_v1()  # the seed's own `B`, as stored
    doc["scoring"]["model_policy"] = {"preset": "B", "nodes": BRIEF_GRIDS["C"], "critic_light_effort": "low"}
    assert Settings.model_validate(doc).scoring.model_policy.preset == "C"


@pytest.mark.parametrize("label", ["A", "C", "custom"])
def test_a_label_only_change_has_no_effect(label: str) -> None:
    doc: dict[str, Any] = copy.deepcopy(load_settings_v1())
    doc["scoring"]["model_policy"]["preset"] = label
    relabelled = Settings.model_validate(doc)
    assert classify_effects(Settings(), relabelled) == {"none"}
    assert content_hash(derived(relabelled).model_policy) == content_hash(derived(Settings()).model_policy)


def test_c_preset_matches_section_21_figures() -> None:
    # §21.2: $0.0194 per target (the Haiku tokenizer uplift and the weaker generator's funnel); §21.1's
    # cache writes per cycle: Haiku/medium, Sonnet/medium and 0.8 x Sonnet/low
    c = presets.PRESETS["C"].policy()
    assert central_cost_per_target(c) == Decimal("0.019352")
    assert cache_writes_per_cycle(c) == Decimal("0.019303")
    assert central_cost_per_target(presets.PRESETS["A"].policy()) == Decimal("0.079255")
    assert central_cost_per_target(presets.PRESETS["B"].policy()) == Decimal("0.052646")
