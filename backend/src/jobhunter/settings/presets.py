"""The model-policy presets A, B and C, and the label of a grid (spec §10.3, §13.1 L3898; plan M4 Task 14,
D20).

`PRESETS` is each preset's grid, the model and effort of every node with the critic's light effort:
- **A**: the generator, the reviser and the critic on `claude-opus-5-5` at `medium`;
- **B**: the settings model's default grid (`ModelPolicyNodes()` and `ModelPolicy`'s default light effort),
  the one definition of the default: Sonnet generator and reviser, Opus critic, all at `medium`;
- **C**: the generator and the reviser on `claude-haiku-5-5`, the critic on `claude-sonnet-5-5`, at `medium`.
All three run `extract_profile` and `propose_queries` on `claude-opus-5-5` at `medium`, and the critic's light
rounds at `low`.

`label_for(nodes, critic_light_effort)` is the letter of the preset whose grid that is, `custom` when none
matches. It is `ModelPolicy`'s label normal form: the model stores this label as its `preset`, whatever label
the request carried, so a label-only change has the active normal form, no effect (§13.3) and no new
`model_policies` row (its hash covers the label of the grid only).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal, cast

from jobhunter.llm.pricing import Effort
from jobhunter.settings.model import ModelPolicy, ModelPolicyNodes, NodeChoice, PolicyLabel

PresetLabel = Literal["A", "B", "C"]

_OPUS: Final = NodeChoice(model="claude-opus-5-5", effort="medium")
_SONNET: Final = NodeChoice(model="claude-sonnet-5-5", effort="medium")
_HAIKU: Final = NodeChoice(model="claude-haiku-5-5", effort="medium")
_LIGHT: Final[Effort] = "low"
# B's light effort is `ModelPolicy`'s default, read from the field: an instance would label itself with
# `label_for`, which needs `PRESETS`.
_DEFAULT_LIGHT: Final = cast(Effort, ModelPolicy.model_fields["critic_light_effort"].default)


@dataclass(frozen=True)
class Preset:
    """A preset's grid: the model and effort of each node, and the critic's effort for a light round."""

    nodes: ModelPolicyNodes
    critic_light_effort: Effort

    def policy(self) -> ModelPolicy:
        """The model policy of this grid (labelled with its letter)."""
        return ModelPolicy(nodes=self.nodes, critic_light_effort=self.critic_light_effort)


PRESETS: Final[Mapping[PresetLabel, Preset]] = MappingProxyType(
    {
        "A": Preset(
            ModelPolicyNodes(
                generate=_OPUS, revise=_OPUS, critic=_OPUS, extract_profile=_OPUS, propose_queries=_OPUS
            ),
            _LIGHT,
        ),
        "B": Preset(ModelPolicyNodes(), _DEFAULT_LIGHT),
        "C": Preset(
            ModelPolicyNodes(
                generate=_HAIKU, revise=_HAIKU, critic=_SONNET, extract_profile=_OPUS, propose_queries=_OPUS
            ),
            _LIGHT,
        ),
    }
)


def label_for(nodes: ModelPolicyNodes, critic_light_effort: str) -> PolicyLabel:
    """The letter of the preset whose grid `nodes` and `critic_light_effort` are, else `custom`."""
    for label, preset in PRESETS.items():
        if preset.nodes == nodes and preset.critic_light_effort == critic_light_effort:
            return label
    return "custom"
