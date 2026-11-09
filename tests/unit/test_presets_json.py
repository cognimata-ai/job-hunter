"""`frontend/src/settings/presets.json` (plan M4 Task 30, resolution b): the model-policy presets A, B and C
as the settings page offers them, one definition with the backend's `settings.presets.PRESETS`.

The file is `PRESETS` serialised as JSON (each preset's node grid and its `critic_light_effort`, keyed by its
label, sorted keys, a 2-space indent), so a change of a preset fails here until the JSON follows it. The
frontend labels a grid with the file (`presets.ts`'s `labelFor`): `json_label` is that lookup, and the parity
test holds it to `label_for` on each preset's grid and on an edited one.
"""

import json
from pathlib import Path
from typing import Any, Final

import pytest
from jobhunter.settings.model import ModelPolicyNodes, NodeChoice
from jobhunter.settings.presets import PRESETS, Preset, label_for

REPO: Final = Path(__file__).resolve().parents[2]
PRESETS_JSON: Final = REPO / "frontend/src/settings/presets.json"


def serialised(preset: Preset) -> dict[str, Any]:
    """A preset as the file holds it."""
    return {"nodes": preset.nodes.model_dump(mode="json"), "critic_light_effort": preset.critic_light_effort}


def the_file_text() -> str:
    """What the file must hold, byte for byte."""
    document = {label: serialised(preset) for label, preset in PRESETS.items()}
    return json.dumps(document, sort_keys=True, indent=2) + "\n"


@pytest.fixture(scope="module")
def presets_json() -> dict[str, Any]:
    loaded = json.loads(PRESETS_JSON.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def json_label(document: dict[str, Any], nodes: dict[str, Any], critic_light_effort: str) -> str:
    """The label of a grid by the file's own presets, as `presets.ts`'s `labelFor` reads them: the preset
    whose nodes and light effort are the grid's, else `custom`."""
    for label, preset in document.items():
        if preset["nodes"] == nodes and preset["critic_light_effort"] == critic_light_effort:
            return str(label)
    return "custom"


def test_the_frontend_presets_equal_the_backend_presets(presets_json: dict[str, Any]) -> None:
    assert presets_json == {label: serialised(preset) for label, preset in PRESETS.items()}
    # written once from the backend, in its one form: sorted keys, a 2-space indent and a final newline
    assert PRESETS_JSON.read_text(encoding="utf-8") == the_file_text()


def edited_grids() -> list[tuple[str, ModelPolicyNodes, str]]:
    """Each preset's grid, and grids one cell away from one: a node's effort, a node's model, the light
    effort."""
    b = PRESETS["B"]
    return [
        *((f"preset {label}", preset.nodes, preset.critic_light_effort) for label, preset in PRESETS.items()),
        (
            "B with the generator at high",
            b.nodes.model_copy(update={"generate": NodeChoice(model="claude-sonnet-5-5", effort="high")}),
            b.critic_light_effort,
        ),
        (
            "C with the critic on Opus",
            PRESETS["C"].nodes.model_copy(
                update={"critic": NodeChoice(model="claude-opus-5-5", effort="medium")}
            ),
            PRESETS["C"].critic_light_effort,
        ),
        ("A with a medium light effort", PRESETS["A"].nodes, "medium"),
    ]


@pytest.mark.parametrize(
    ("case", "nodes", "light"), edited_grids(), ids=[case for case, _, _ in edited_grids()]
)
def test_the_json_lookup_labels_a_grid_as_label_for_does(
    presets_json: dict[str, Any], case: str, nodes: ModelPolicyNodes, light: str
) -> None:
    assert json_label(presets_json, nodes.model_dump(mode="json"), light) == label_for(nodes, light), case


def test_the_edited_grids_are_custom(presets_json: dict[str, Any]) -> None:
    """The parity is not vacuous: the three presets keep their letters and the edited grids are `custom`."""
    labels = [
        json_label(presets_json, nodes.model_dump(mode="json"), light) for _, nodes, light in edited_grids()
    ]
    assert labels == ["A", "B", "C", "custom", "custom", "custom"]
