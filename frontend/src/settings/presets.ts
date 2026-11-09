// The model-policy presets A, B and C and the label of a grid (spec §10.3, §15.7; plan M4 Task 30, resolution b,
// D20), with the closed sets of the model grid.
//
// - **`PRESETS`** is `presets.json`, the backend's `settings.presets.PRESETS` written out once (each preset's
//   node grid and its critic light effort, keyed by its letter); `tests/unit/test_presets_json.py` holds the file
//   to the backend, so the one definition stays the backend's. Each model and effort of the file is checked
//   against its closed set when the module loads.
// - **`labelFor(policy)`** mirrors `settings.presets.label_for`: the letter of the preset whose grid (`nodes` and
//   `critic_light_effort`) the policy's is, else `custom`. The label a policy carries plays no part: the server
//   stores the label of the grid, whatever a request sends.
// - **The models** are §10.3's, the schema's literal (both ways, `closedSet`), in §10.3's order. **The efforts of
//   a model** are `codes.json`'s `order.effort.<model>`, which `test_i18n_codes.py` derives from
//   `llm/pricing.py`'s `SUPPORTED_EFFORTS` in `Effort`'s order; they are never listed here.
import codes from "../i18n/codes.json";
import { closedSet } from "./fields";
import presetsJson from "./presets.json";
import { type Settings, deepEqual } from "./useSettingsForm";

export type ModelPolicy = Settings["scoring"]["model_policy"];
export type ModelPolicyNodes = ModelPolicy["nodes"];
export type NodeChoice = ModelPolicyNodes["generate"];
export type Model = NodeChoice["model"];
export type Effort = NodeChoice["effort"];
/** A node of the model and execution grids (`settings.model.Node`). */
export type LlmNode = keyof ModelPolicyNodes;
export type PolicyLabel = ModelPolicy["preset"];
export type PresetLabel = Exclude<PolicyLabel, "custom">;

/** A preset's grid: each node's model and effort, and the critic's effort for a light round. */
export interface Preset {
  nodes: ModelPolicyNodes;
  critic_light_effort: Effort;
}

/** §10.3's allowed models, in its order. */
export const MODELS = closedSet<Model>({
  "claude-opus-5-5": true,
  "claude-sonnet-5-5": true,
  "claude-haiku-5-5": true,
  "claude-fable-5-1": true,
});
export const EFFORTS = closedSet<Effort>({ low: true, medium: true, high: true, xhigh: true, max: true });
/** The nodes in §10.3's table order. */
export const NODES = closedSet<LlmNode>({ generate: true, revise: true, critic: true, extract_profile: true, propose_queries: true });
export const PRESET_LABELS = closedSet<PresetLabel>({ A: true, B: true, C: true });
/** The preset selector's options: the presets, then `custom`. */
export const POLICY_LABELS = closedSet<PolicyLabel>({ A: true, B: true, C: true, custom: true });
/** The default effort (§10.3), the one a node moves to when its new model lacks its effort. */
export const DEFAULT_EFFORT: Effort = "medium";

function member<T extends string>(set: readonly T[], value: string, what: string): T {
  const found = set.find((option) => option === value);
  if (found === undefined) {
    throw new Error(`${what} ${value} is none of ${set.join(", ")}`);
  }
  return found;
}

function choice(raw: { model: string; effort: string }): NodeChoice {
  return { model: member(MODELS, raw.model, "presets.json: model"), effort: member(EFFORTS, raw.effort, "presets.json: effort") };
}

function preset(raw: (typeof presetsJson)[PresetLabel]): Preset {
  const { nodes } = raw;
  return {
    nodes: {
      generate: choice(nodes.generate),
      revise: choice(nodes.revise),
      critic: choice(nodes.critic),
      extract_profile: choice(nodes.extract_profile),
      propose_queries: choice(nodes.propose_queries),
    },
    critic_light_effort: member(EFFORTS, raw.critic_light_effort, "presets.json: effort"),
  };
}

export const PRESETS: Readonly<Record<PresetLabel, Preset>> = {
  A: preset(presetsJson.A),
  B: preset(presetsJson.B),
  C: preset(presetsJson.C),
};

/** The label of a grid: the letter of the preset it is, else `custom` (`settings.presets.label_for`). */
export function labelFor(policy: Pick<ModelPolicy, "nodes" | "critic_light_effort">): PolicyLabel {
  for (const label of PRESET_LABELS) {
    const grid = PRESETS[label];
    if (deepEqual(grid.nodes, policy.nodes) && grid.critic_light_effort === policy.critic_light_effort) {
      return label;
    }
  }
  return "custom";
}

// `Record<Model, …>`: the catalogue lists every model (a missing one fails the typecheck)
const MODEL_EFFORTS: Readonly<Record<Model, readonly string[]>> = codes.order.effort;

/** The efforts `model` supports, in their order (`codes.json`'s `order.effort`). */
export function effortsOf(model: Model): Effort[] {
  return MODEL_EFFORTS[model].flatMap((value) => EFFORTS.filter((effort) => effort === value));
}

/** `effort` when `model` supports it, else the default effort, else the model's first. */
export function effortFor(model: Model, effort: Effort): Effort {
  const supported = effortsOf(model);
  if (supported.includes(effort)) {
    return effort;
  }
  return supported.includes(DEFAULT_EFFORT) ? DEFAULT_EFFORT : (supported[0] ?? effort);
}
