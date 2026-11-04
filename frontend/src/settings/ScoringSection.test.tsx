import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { components } from "../api/schema";
import en from "../i18n/en.json";
import {
  type FakeApi,
  dryRunOut,
  evalRunsPage,
  fakeApi,
  goldenRun,
  json,
  problem,
  projectionsOut,
  requestsTo,
  settingsRead,
  spendOut,
} from "../test/api";
import { dryRunPayloads, renderSettings, saveBody, saves } from "../test/settings";
import { PRESETS } from "./presets";

type Settings = components["schemas"]["Settings"];
type DryRunOut = components["schemas"]["DryRunOut"];

afterEach(() => {
  vi.restoreAllMocks();
});

const RESCORE = { code: "llm_rescore", detail: { targets: 1180, est_usd: "41.300000", bulk_budget_usd: "22.440000" } };
const CONFLICT = {
  field: "payload.scoring.model_policy.nodes.generate.effort",
  code: "conflict" as const,
  message: "selecting generator effort low needs a passing eval (§11.13)",
};

/** Answers the base document's dry-run with `base`, any other with `draft` (of the draft it is asked for). */
function dryRuns(api: FakeApi, draft: (payload: Settings) => DryRunOut, base: DryRunOut = dryRunOut(), served = settingsRead()) {
  const basePayload = JSON.stringify(served.payload);
  api.routes.set("POST /api/settings/dry-run", (request) => {
    const { payload } = JSON.parse(request.body ?? "{}") as { payload: Settings };
    return json(JSON.stringify(payload) === basePayload ? base : draft(payload));
  });
}

function section(id = "scoring"): HTMLElement {
  const element = document.getElementById(id);
  if (element === null) {
    throw new Error(`no section ${id}`);
  }
  return element;
}

/** The row of `node` in the model grid (`en` key of its name). */
async function modelRow(name: string): Promise<HTMLElement> {
  const grid = await within(section()).findByRole("group", { name: en["settings_scoring.models"] });
  return within(grid).getByRole("group", { name });
}

async function select(row: HTMLElement, label: string): Promise<HTMLSelectElement> {
  return within(row).findByRole<HTMLSelectElement>("combobox", { name: label });
}

function radio(name: string): HTMLInputElement {
  return within(section()).getByRole<HTMLInputElement>("radio", { name });
}

function saveIn(id: string): HTMLButtonElement {
  return within(section(id)).getByRole<HTMLButtonElement>("button", { name: en["settings_form.save"] });
}

/** The value beside the term `name` in the section. */
function figure(name: string): string {
  const term = within(section()).getByText(name, { selector: "dt" });
  return term.nextElementSibling?.textContent ?? "";
}

/** A draft whose model policy is not the base's answers with the re-score warning and its effect. */
function rescoring(payload: Settings): DryRunOut {
  const changed = JSON.stringify(payload.scoring.model_policy) !== JSON.stringify(settingsRead().payload.scoring.model_policy);
  return changed
    ? dryRunOut({ warnings: [RESCORE], effects: ["llm_tuple"], projections: projectionsOut({ eval_gate: "passed" }) })
    : dryRunOut({ effects: ["schedule"] });
}

/** Ticks the re-score warning in the section `id`'s panel. */
async function tickRescore(id: string): Promise<void> {
  const item = await waitFor(() => {
    const found = within(section(id)).getByText(en["warning.llm_rescore"]).closest("li");
    if (found === null) {
      throw new Error("no re-score warning");
    }
    return found;
  });
  await userEvent.setup().click(within(item).getByRole("checkbox", { name: en["settings_form.understand"] }));
}

describe("ScoringSection", () => {
  it("choosing C fills the grid", async () => {
    const api = fakeApi({ signedIn: true });
    renderSettings(["scoring"]);
    await modelRow(en["llm_node.generate"]);
    expect(radio(en["model_preset.B"]).checked).toBe(true);
    await userEvent.setup().click(radio(en["model_preset.C"]));
    expect(radio(en["model_preset.C"]).checked).toBe(true);
    const models = await Promise.all(
      (["generate", "revise", "critic", "extract_profile", "propose_queries"] as const).map(async (node) =>
        (await select(await modelRow(en[`llm_node.${node}`]), en["settings_scoring.model"])).value,
      ),
    );
    expect(models).toEqual(["claude-haiku-5-5", "claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5-5"]);
    const light = await select(await modelRow(en["settings_scoring.critic_light"]), en["settings_scoring.effort"]);
    expect(light.value).toBe("low");
    await waitFor(() => {
      const policy = dryRunPayloads(api).at(-1)?.scoring.model_policy;
      expect(policy?.nodes).toEqual(PRESETS.C.nodes);
      expect(policy?.critic_light_effort).toBe(PRESETS.C.critic_light_effort);
    });
    // back to B: the saved grid again, nothing dirty
    await userEvent.setup().click(radio(en["model_preset.B"]));
    expect(await within(section()).findByText(en["settings_form.no_changes"])).toBeTruthy();
  });

  it("editing the grid relabels custom", async () => {
    const api = fakeApi({ signedIn: true });
    renderSettings(["scoring"]);
    const user = userEvent.setup();
    await user.click(await within(section()).findByRole("radio", { name: en["model_preset.A"] }));
    await user.selectOptions(await select(await modelRow(en["llm_node.revise"]), en["settings_scoring.effort"]), "high");
    expect(radio(en["model_preset.custom"]).checked).toBe(true);
    expect(radio(en["model_preset.A"]).checked).toBe(false);
    await waitFor(() => {
      expect(dryRunPayloads(api).at(-1)?.scoring.model_policy.nodes.revise).toEqual({ model: "claude-opus-5-5", effort: "high" });
    });
    // and back to A's cell: A again
    await user.selectOptions(await select(await modelRow(en["llm_node.revise"]), en["settings_scoring.effort"]), "medium");
    expect(radio(en["model_preset.A"]).checked).toBe(true);
  });

  it("custom cannot be chosen by hand", async () => {
    fakeApi({ signedIn: true });
    renderSettings(["scoring"]);
    await modelRow(en["llm_node.generate"]);
    const custom = radio(en["model_preset.custom"]);
    expect(custom.disabled).toBe(true);
    expect(custom.checked).toBe(false);
    const user = userEvent.setup();
    await user.selectOptions(await select(await modelRow(en["llm_node.critic"]), en["settings_scoring.model"]), "claude-fable-5-1");
    expect(custom.checked).toBe(true);
    // a preset can be chosen from a custom grid: it fills the grid
    await user.click(radio(en["model_preset.B"]));
    expect(custom.checked).toBe(false);
    expect(custom.disabled).toBe(true);
    expect((await select(await modelRow(en["llm_node.critic"]), en["settings_scoring.model"])).value).toBe("claude-opus-5-5");
  });

  it("a preset chosen from a custom grid writes its light effort", async () => {
    const api = fakeApi({ signedIn: true });
    const served = settingsRead();
    served.payload.scoring.model_policy.critic_light_effort = "medium";
    served.payload.scoring.model_policy.preset = "custom";
    api.routes.set("GET /api/settings", () => json(served));
    renderSettings(["scoring"]);
    const light = await select(await modelRow(en["settings_scoring.critic_light"]), en["settings_scoring.effort"]);
    expect(light.value).toBe("medium");
    expect(radio(en["model_preset.custom"]).checked).toBe(true);
    await userEvent.setup().click(radio(en["model_preset.B"]));
    expect(light.value).toBe("low");
    expect(radio(en["model_preset.B"]).checked).toBe(true);
    await waitFor(() => {
      expect(dryRunPayloads(api).at(-1)?.scoring.model_policy.critic_light_effort).toBe("low");
    });
  });

  it("eval_failed and eval_uncalibrated copy", async () => {
    const api = fakeApi({ signedIn: true });
    // the saved generator is on `low` under a failed gate: the standing warning `eval_failed`
    const base = settingsRead();
    const low = structuredClone(base);
    low.payload.scoring.model_policy.nodes.generate.effort = "low";
    low.payload.scoring.model_policy.preset = "custom";
    api.routes.set("GET /api/settings", () => json(low));
    dryRuns(
      api,
      () => dryRunOut({ warnings: [{ code: "eval_uncalibrated", detail: { node: "generate" } }], effects: ["llm_tuple"] }),
      dryRunOut({ warnings: [{ code: "eval_failed", detail: { node: "generate" } }] }),
      low,
    );
    renderSettings(["scoring"]);
    const generator = await modelRow(en["llm_node.generate"]);
    const failed = await within(generator).findByText(en["warning.eval_failed"]);
    expect(en["warning.eval_failed"]).toBe(
      "The golden-set gate failed for this configuration. Your generator effort “low” stays active, but it cannot be selected again until a golden run passes.",
    );
    const effort = await select(generator, en["settings_scoring.effort"]);
    expect((effort.getAttribute("aria-describedby") ?? "").split(" ")).toContain(failed.id);
    // a change: the draft's own warning beside the generator row
    fireEvent.change(await select(await modelRow(en["llm_node.revise"]), en["settings_scoring.effort"]), { target: { value: "high" } });
    expect(await within(generator).findByText(en["warning.eval_uncalibrated"])).toBeTruthy();
    expect(within(generator).queryByText(en["warning.eval_failed"])).toBeNull();
  });

  it("the generator's low conflict is told at its effort", async () => {
    const api = fakeApi({ signedIn: true });
    dryRuns(api, () => dryRunOut({ errors: [CONFLICT], warnings: [], projections: null, effects: [] }));
    renderSettings(["scoring"]);
    const generator = await modelRow(en["llm_node.generate"]);
    const effort = await select(generator, en["settings_scoring.effort"]);
    await userEvent.setup().selectOptions(effort, "low");
    await waitFor(() => {
      expect(effort.getAttribute("aria-invalid")).toBe("true");
    });
    const described = (effort.getAttribute("aria-describedby") ?? "").split(" ").map((id) => document.getElementById(id)?.textContent);
    expect(described).toContain(en["settings_scoring.generator_low_conflict"]);
    // Task 28's error linking: the panel's issue too
    expect(described.some((text) => text?.includes(CONFLICT.message))).toBe(true);
    expect(saveIn("scoring").disabled).toBe(true);
  });

  it("an llm_tuple save asks with the re-score cost", async () => {
    const api = fakeApi({ signedIn: true });
    dryRuns(api, rescoring);
    api.routes.set("POST /api/settings", () =>
      json({ version: { id: 42, version_no: 13, effect_classes: ["llm_tuple"] }, apply_scope_operation_id: null }, 201),
    );
    renderSettings(["scoring"]);
    const user = userEvent.setup();
    await user.click(await within(section()).findByRole("radio", { name: en["model_preset.C"] }));
    await tickRescore("scoring");
    await user.click(saveIn("scoring"));
    const dialog = await screen.findByRole("alertdialog", { name: en["settings_form.rescore_title"] });
    expect(dialog.textContent).toContain("This change re-scores 1,180 ads with the LLM, about $41.30. Continue?");
    expect(saves(api)).toHaveLength(0); // nothing sent while it asks
    await user.click(within(dialog).getByRole("button", { name: en["settings_form.rescore_confirm"] }));
    await waitFor(() => {
      expect(saves(api)).toHaveLength(1);
    });
    const body = saveBody(saves(api)[0]);
    expect(body.payload.scoring.model_policy.nodes).toEqual(PRESETS.C.nodes);
    expect(body.acknowledged_warnings).toEqual(["llm_rescore"]);
    expect(screen.queryByRole("alertdialog")).toBeNull();
  });

  it("cancel sends no save", async () => {
    const api = fakeApi({ signedIn: true });
    dryRuns(api, rescoring);
    renderSettings(["scoring"]);
    const user = userEvent.setup();
    await user.click(await within(section()).findByRole("radio", { name: en["model_preset.A"] }));
    await tickRescore("scoring");
    await user.click(saveIn("scoring"));
    const dialog = await screen.findByRole("alertdialog", { name: en["settings_form.rescore_title"] });
    await user.click(within(dialog).getByRole("button", { name: en["action.cancel"] }));
    expect(screen.queryByRole("alertdialog")).toBeNull();
    // Esc cancels too
    await user.click(saveIn("scoring"));
    await screen.findByRole("alertdialog", { name: en["settings_form.rescore_title"] });
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(saves(api)).toHaveLength(0);
    expect(radio(en["model_preset.A"]).checked).toBe(true); // the draft is kept
  });

  it("the re-score confirmation guards a save from another section", async () => {
    const api = fakeApi({ signedIn: true });
    dryRuns(api, rescoring);
    renderSettings(["schedule", "scoring"]);
    const user = userEvent.setup();
    await user.click(await within(section()).findByRole("radio", { name: en["model_preset.C"] }));
    await tickRescore("schedule");
    await user.click(saveIn("schedule"));
    const dialog = await screen.findByRole("alertdialog", { name: en["settings_form.rescore_title"] });
    expect(dialog.textContent).toContain("1,180 ads");
    await user.click(within(dialog).getByRole("button", { name: en["action.cancel"] }));
    expect(saves(api)).toHaveLength(0);
  });

  it("a save refused by the eval gate asks again before it is sent with the override", async () => {
    const api = fakeApi({ signedIn: true });
    dryRuns(api, rescoring);
    let answers = 0;
    api.routes.set("POST /api/settings", () =>
      answers++ === 0
        ? problem(409, "eval_gate_failed")
        : json({ version: { id: 42, version_no: 13, effect_classes: ["llm_tuple"] }, apply_scope_operation_id: null }, 201),
    );
    renderSettings(["scoring"]);
    const user = userEvent.setup();
    await user.click(await within(section()).findByRole("radio", { name: en["model_preset.C"] }));
    await tickRescore("scoring");
    await user.click(saveIn("scoring"));
    await user.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: en["settings_form.rescore_confirm"] }));
    // refused: the panel offers the override
    const override = await within(section()).findByRole("checkbox", { name: en["settings_form.eval_override"] });
    expect(saves(api)).toHaveLength(1);
    expect(saveBody(saves(api)[0]).eval_override_ack).toBeUndefined();
    await user.click(override);
    // each send acknowledges its own re-score (Ruling T30-e): Save asks again
    await user.click(saveIn("scoring"));
    const again = await screen.findByRole("alertdialog", { name: en["settings_form.rescore_title"] });
    expect(again.textContent).toContain("This change re-scores 1,180 ads with the LLM, about $41.30. Continue?");
    expect(saves(api)).toHaveLength(1);
    await user.click(within(again).getByRole("button", { name: en["settings_form.rescore_confirm"] }));
    await waitFor(() => {
      expect(saves(api)).toHaveLength(2);
    });
    const [first, second] = saves(api);
    expect(saveBody(second).eval_override_ack).toBe(true);
    expect(saveBody(second).payload.scoring.model_policy.nodes).toEqual(PRESETS.C.nodes);
    expect(second.headers.get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
    expect(second.headers.get("Idempotency-Key")).not.toBe(first.headers.get("Idempotency-Key"));
  });

  it("a save without an llm_tuple effect does not ask", async () => {
    const api = fakeApi({ signedIn: true });
    dryRuns(api, () => dryRunOut({ effects: ["execution"] }));
    renderSettings(["scoring"]);
    const grid = await within(section()).findByRole("group", { name: en["settings_scoring.execution"] });
    const generate = within(grid).getByRole("group", { name: en["llm_node.generate"] });
    fireEvent.change(within(generate).getByRole("spinbutton", { name: en["settings_scoring.max_tokens"] }), { target: { value: "7000" } });
    await waitFor(() => {
      expect(saveIn("scoring").disabled).toBe(false);
    });
    await userEvent.setup().click(saveIn("scoring"));
    await waitFor(() => {
      expect(saves(api)).toHaveLength(1);
    });
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(saveBody(saves(api)[0]).payload.scoring.execution.generate).toEqual({ max_tokens: 7000, max_tokens_retry: 12000, deadline_s: 600 });
  });

  it("the execution grid has no bound of its own", async () => {
    fakeApi({ signedIn: true });
    renderSettings(["scoring"]);
    const grid = await within(section()).findByRole("group", { name: en["settings_scoring.execution"] });
    const profile = within(grid).getByRole("group", { name: en["llm_node.extract_profile"] });
    const fields = within(profile).getAllByRole("spinbutton").map((field) => [
      (field as HTMLInputElement).value,
      field.getAttribute("min"),
      field.getAttribute("max"),
    ]);
    expect(fields).toEqual([
      ["16000", null, null],
      ["32000", null, null],
      ["900", null, null],
    ]);
  });

  it("critic rates with null as a dash", async () => {
    const api = fakeApi({ signedIn: true });
    dryRuns(api, () => dryRunOut({ effects: ["evaluation_policy"], projections: projectionsOut({ critic_rate: "0.380", eval_gate: "passed" }) }));
    renderSettings(["scoring"]);
    await waitFor(() => {
      expect(figure(en["settings_scoring.projected_rate"])).toBe("45%"); // the spend read's, while nothing changed
    });
    expect(figure(en["settings_scoring.measured_rate"])).toBe("—");
    fireEvent.change(within(section()).getByRole("spinbutton", { name: en["settings_scoring.relevance_threshold"] }), {
      target: { value: "65" },
    });
    await waitFor(() => {
      expect(figure(en["settings_scoring.projected_rate"])).toBe("38%"); // the dry-run's
    });
    await waitFor(() => {
      expect(dryRunPayloads(api).at(-1)?.evaluation.relevance_threshold).toBe(65);
    });
  });

  it("the measured critic rate as a percentage", async () => {
    const api = fakeApi({ signedIn: true });
    api.spend = spendOut({ critic_rate_7d: "0.412", projected_critic_rate: "0.450" });
    renderSettings(["scoring"]);
    await waitFor(() => {
      expect(figure(en["settings_scoring.measured_rate"])).toBe("41.2%");
    });
    // the spend is read live, in the background, as Budget & quota reads it
    expect(requestsTo(api, "GET /api/system/spend")[0]?.headers.get("X-JH-Background")).toBe("1");
  });

  it("the light-effort triggers and the straddle coverage", async () => {
    const api = fakeApi({ signedIn: true });
    renderSettings(["scoring"]);
    const triggers = await within(section()).findByRole("group", { name: en["settings_scoring.light_triggers"] });
    const boxes = within(triggers).getAllByRole<HTMLInputElement>("checkbox");
    expect(boxes.map((box) => [box.labels?.[0]?.textContent, box.checked])).toEqual([
      [en["critic_trigger.T2"], true],
      [en["critic_trigger.T3"], false],
      [en["critic_trigger.T8"], true],
    ]);
    await userEvent.setup().click(boxes[1]);
    fireEvent.change(within(section()).getByRole("textbox", { name: en["settings_scoring.t2_coverage"] }), { target: { value: "0,65" } });
    await waitFor(() => {
      const evaluation = dryRunPayloads(api).at(-1)?.evaluation;
      expect(evaluation?.critic_light_triggers).toEqual(["T2", "T3", "T8"]);
      expect(evaluation?.t2_straddle_max_coverage).toBe("0.65");
    });
  });

  it("the monthly cost projection", async () => {
    fakeApi({ signedIn: true });
    renderSettings(["scoring"]);
    await waitFor(() => {
      expect(figure(en["settings_scoring.cost"])).toBe("$92.40 this month · up to 1,700 ads a month at ĉ $0.0526");
    });
  });

  it("eval status: the last golden run, the gate and the command line", async () => {
    const api = fakeApi({ signedIn: true });
    api.evalRuns = evalRunsPage([goldenRun(), goldenRun({ id: 6, gate: "failed", passed: false, spearman_rho: "0.400" })], {
      state: "passed",
      cases: 34,
    });
    renderSettings(["scoring"]);
    expect(await within(section()).findByText("Last eval: passed, ρ = 0.71")).toBeTruthy();
    expect(figure(en["settings_scoring.gate"])).toBe("passed · 34 golden cases");
    expect(within(section()).getByText(en["settings_scoring.golden_cli"])).toBeTruthy();
    const [read] = requestsTo(api, "GET /api/eval-runs");
    expect(read.search.get("kind")).toBe("golden");
  });

  it("eval status without a run", async () => {
    fakeApi({ signedIn: true });
    renderSettings(["scoring"]);
    expect(await within(section()).findByText(en["settings_scoring.no_golden_run"])).toBeTruthy();
    expect(figure(en["settings_scoring.gate"])).toBe(`${en["eval_gate.uncalibrated"]} · 12 golden cases`);
  });

  it("eval status of a running run", async () => {
    const api = fakeApi({ signedIn: true });
    const running = goldenRun({ id: 8, status: "running", passed: null, gate: null, spearman_rho: null, finished_at: null });
    api.evalRuns = evalRunsPage([running, goldenRun()]);
    renderSettings(["scoring"]);
    expect(await within(section()).findByText(`Last eval: ${en["eval_run_status.running"]}`)).toBeTruthy();
  });

  it("eval status of a crashed run", async () => {
    const api = fakeApi({ signedIn: true });
    api.evalRuns = evalRunsPage([goldenRun({ status: "failed", passed: null, gate: null, spearman_rho: null })]);
    renderSettings(["scoring"]);
    expect(await within(section()).findByText(`Last eval: ${en["eval_run_status.failed"]}`)).toBeTruthy();
  });

  it("a gated change shows the gate it would check", async () => {
    const api = fakeApi({ signedIn: true });
    dryRuns(api, rescoring);
    renderSettings(["scoring"]);
    await userEvent.setup().click(await within(section()).findByRole("radio", { name: en["model_preset.C"] }));
    await waitFor(() => {
      expect(figure(en["settings_scoring.gate_draft"])).toBe(en["eval_gate.passed"]);
    });
    expect(api.calls).toContain("POST /api/settings/dry-run");
  });
});
