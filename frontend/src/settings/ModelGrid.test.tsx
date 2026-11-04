// The model grid's effort options follow each row's model (plan M4 Task 30, resolution c). Every model of §10.3
// supports every effort today (`llm/pricing.py`), so this file gives Haiku two efforts in the catalogue's
// `order.effort` (the list `test_i18n_codes.py` derives from the backend) to see the options follow the model.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import en from "../i18n/en.json";
import { fakeApi } from "../test/api";
import { dryRunPayloads, renderSettings } from "../test/settings";

vi.mock("../i18n/codes.json", async (importOriginal) => {
  const actual = await importOriginal<{ default: typeof import("../i18n/codes.json") }>();
  const effort = { ...actual.default.order.effort, "claude-haiku-5-5": ["low", "medium"] };
  return { default: { ...actual.default, order: { ...actual.default.order, effort } } };
});

afterEach(() => {
  vi.restoreAllMocks();
});

async function row(name: string): Promise<HTMLElement> {
  const grid = await screen.findByRole("group", { name: en["settings_scoring.models"] });
  return within(grid).getByRole("group", { name });
}

function options(select: HTMLSelectElement): string[] {
  return [...select.options].map((option) => option.value);
}

function control(element: HTMLElement, name: string): HTMLSelectElement {
  return within(element).getByRole<HTMLSelectElement>("combobox", { name });
}

describe("ModelGrid", () => {
  it("the effort options follow the model", async () => {
    const api = fakeApi({ signedIn: true });
    renderSettings(["scoring"]);
    const generator = await row(en["llm_node.generate"]);
    const effort = control(generator, en["settings_scoring.effort"]);
    expect(options(effort)).toEqual(["low", "medium", "high", "xhigh", "max"]);
    const user = userEvent.setup();
    await user.selectOptions(effort, "high");
    await user.selectOptions(control(generator, en["settings_scoring.model"]), "claude-haiku-5-5");
    expect(options(control(generator, en["settings_scoring.effort"]))).toEqual(["low", "medium"]);
    // `high` is not Haiku's: the effort moves to the default one, `medium`
    expect(control(generator, en["settings_scoring.effort"]).value).toBe("medium");
    await waitFor(() => {
      expect(dryRunPayloads(api).at(-1)?.scoring.model_policy.nodes.generate).toEqual({ model: "claude-haiku-5-5", effort: "medium" });
    });
  });

  it("the light effort follows the critic's model", async () => {
    fakeApi({ signedIn: true });
    renderSettings(["scoring"]);
    const light = await row(en["settings_scoring.critic_light"]);
    expect(options(control(light, en["settings_scoring.effort"]))).toHaveLength(5);
    await userEvent.setup().selectOptions(control(await row(en["llm_node.critic"]), en["settings_scoring.model"]), "claude-haiku-5-5");
    expect(options(control(light, en["settings_scoring.effort"]))).toEqual(["low", "medium"]);
    expect(control(light, en["settings_scoring.effort"]).value).toBe("low");
    expect(light.textContent).toContain("claude-haiku-5-5");
  });

  it("a critic model whose light options lack the light effort moves it", async () => {
    const api = fakeApi({ signedIn: true });
    renderSettings(["scoring"]);
    const light = await row(en["settings_scoring.critic_light"]);
    const user = userEvent.setup();
    await user.selectOptions(control(light, en["settings_scoring.effort"]), "high");
    await user.selectOptions(control(await row(en["llm_node.critic"]), en["settings_scoring.model"]), "claude-haiku-5-5");
    // `high` is not among Haiku's light options: the default effort, `medium`
    expect(control(light, en["settings_scoring.effort"]).value).toBe("medium");
    await waitFor(() => {
      const policy = dryRunPayloads(api).at(-1)?.scoring.model_policy;
      expect(policy?.critic_light_effort).toBe("medium");
      expect(policy?.nodes.critic).toEqual({ model: "claude-haiku-5-5", effort: "medium" });
    });
  });
});
