import { describe, expect, it } from "vitest";
import codes from "../i18n/codes.json";
import { defaultSettings } from "../test/api";
import { EFFORTS, MODELS, type ModelPolicy, PRESETS, effortsOf, labelFor } from "./presets";

describe("presets", () => {
  it("labelFor names A, B, C and custom", () => {
    expect(labelFor(PRESETS.A)).toBe("A");
    expect(labelFor(PRESETS.B)).toBe("B");
    expect(labelFor(PRESETS.C)).toBe("C");
    // the grids of test_presets_json.py's parity cases, one cell away from a preset
    const b = PRESETS.B;
    expect(labelFor({ ...b, nodes: { ...b.nodes, generate: { model: "claude-sonnet-5-5", effort: "high" } } })).toBe("custom");
    const c = PRESETS.C;
    expect(labelFor({ ...c, nodes: { ...c.nodes, critic: { model: "claude-opus-5-5", effort: "medium" } } })).toBe("custom");
    expect(labelFor({ ...PRESETS.A, critic_light_effort: "medium" })).toBe("custom");
  });

  it("labels the grid, not the label a policy carries", () => {
    const policy = defaultSettings().scoring.model_policy;
    const saysA: ModelPolicy = { ...policy, preset: "A" };
    const saysB: ModelPolicy = { ...policy, ...PRESETS.C, preset: "B" };
    expect(labelFor(saysA)).toBe("B");
    expect(labelFor(saysB)).toBe("C");
  });

  it("B is the default document's grid", () => {
    const { nodes, critic_light_effort } = defaultSettings().scoring.model_policy;
    expect(PRESETS.B).toEqual({ nodes, critic_light_effort });
  });

  it("A runs every node on Opus, C the generator and reviser on Haiku and the critic on Sonnet", () => {
    expect(Object.values(PRESETS.A.nodes).map((node) => node.model)).toEqual(Array(5).fill("claude-opus-5-5"));
    expect([PRESETS.C.nodes.generate.model, PRESETS.C.nodes.revise.model, PRESETS.C.nodes.critic.model]).toEqual([
      "claude-haiku-5-5",
      "claude-haiku-5-5",
      "claude-sonnet-5-5",
    ]);
  });

  it("each model's efforts are the catalogue's order of them", () => {
    for (const model of MODELS) {
      expect(effortsOf(model)).toEqual(codes.order.effort[model]);
      expect(effortsOf(model).every((effort) => EFFORTS.includes(effort))).toBe(true);
    }
    expect(MODELS).toEqual(["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1"]);
  });
});
