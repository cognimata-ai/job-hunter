// E15 (spec §18.7, §13.3, §10.3, §15.7; plan M4 Task 34): in Scoring & models, preset B (the active one) to C. The
// dry-run warns `llm_rescore` with the re-score's ads and cost, Save (each warning ticked) asks first in the
// re-score confirmation, which states the same ads and cost; Cancel sends nothing, and B stays active.
import type { Request } from "@playwright/test";
import type { components } from "../../src/api/schema";
import { expectNoSeriousA11yViolations } from "../support/a11y";
import { apiContext } from "../support/api";
import { en, expect, filled, test } from "../support/fixtures";
import { pageUsd, readSettings } from "../support/settings";

type DryRunOut = components["schemas"]["DryRunOut"];
type ModelPolicy = components["schemas"]["Settings"]["scoring"]["model_policy"];

const NODES_OF_B = {
  generate: { model: "claude-sonnet-5-5", effort: "medium" },
  revise: { model: "claude-sonnet-5-5", effort: "medium" },
  critic: { model: "claude-opus-5-5", effort: "medium" },
} as const; // `settings/presets.json`'s B, the nodes it changes against A and C

function isSave(request: Request): boolean {
  return request.method() === "POST" && new URL(request.url()).pathname === "/api/settings";
}

function isB(policy: ModelPolicy): boolean {
  return Object.entries(NODES_OF_B).every(([node, cell]) => {
    const set = policy.nodes[node as keyof typeof NODES_OF_B];
    return set.model === cell.model && set.effort === cell.effort;
  });
}

test("E15: preset C warns of the LLM re-score and asks first; Cancel leaves B active", async ({ page }) => {
  const api = await apiContext(page);
  try {
    const before = await readSettings(api);
    expect(isB(before.payload.scoring.model_policy), "preset B active (the default)").toBe(true);
    const saves: Request[] = [];
    page.on("request", (request) => {
      if (isSave(request)) {
        saves.push(request);
      }
    });

    await page.goto("/settings#scoring");
    const scoring = page.getByRole("region", { name: en["settings.section.scoring"] });
    const presets = scoring.getByRole("radiogroup", { name: en["settings_scoring.presets"] });
    await expect(presets.getByRole("radio", { name: en["model_preset.B"], exact: true })).toBeChecked();
    const dryRun = page.waitForResponse((response) => {
      if (response.request().method() !== "POST" || new URL(response.url()).pathname !== "/api/settings/dry-run") {
        return false;
      }
      const sent = response.request().postDataJSON() as { payload: { scoring: { model_policy: ModelPolicy } } };
      return sent.payload.scoring.model_policy.nodes.generate.model === "claude-haiku-5-5";
    });
    await presets.getByRole("radio", { name: en["model_preset.C"], exact: true }).check();
    const preview = (await (await dryRun).json()) as DryRunOut;
    const rescore = preview.warnings.find((warning) => warning.code === "llm_rescore");
    expect(rescore, `the dry-run's warnings: ${preview.warnings.map((w) => w.code).join(", ")}`).toBeDefined();
    expect(preview.effects).toContain("llm_tuple");
    const targets = Number(rescore?.detail.targets);
    const cost = await pageUsd(page, String(rescore?.detail.est_usd));
    const count = await page.evaluate((n) => new Intl.NumberFormat("en-GB").format(n), targets); // `format.number`

    const warnings = scoring.getByRole("list", { name: en["settings_form.warnings"] });
    const warning = warnings.getByRole("listitem").filter({ hasText: en["warning.llm_rescore"] });
    const detail = filled(targets === 1 ? en["warning_detail.llm_rescore_one"] : en["warning_detail.llm_rescore_other"], {
      value: count,
      est_usd: cost,
      bulk_budget_usd: await pageUsd(page, String(rescore?.detail.bulk_budget_usd)),
    });
    await expect(warning.getByText(detail, { exact: true })).toBeVisible();
    await expect(scoring.getByRole("list", { name: en["settings_form.effects"] }).getByText(filled(en["settings_form.effect_llm_cost"], { usd: cost }), { exact: true })).toBeVisible();
    const save = scoring.getByRole("button", { name: en["settings_form.save"], exact: true });
    await expect(save).toBeDisabled();
    for (const tick of await warnings.getByRole("checkbox").all()) {
      await tick.check();
    }
    await expect(save).toBeEnabled();
    await expectNoSeriousA11yViolations(page);

    await save.click();
    const confirm = page.getByRole("alertdialog", { name: en["settings_form.rescore_title"] });
    const body = filled(targets === 1 ? en["settings_form.rescore_body_one"] : en["settings_form.rescore_body_other"], {
      value: count,
      est_usd: cost,
    });
    await expect(confirm).toHaveAccessibleDescription(body);
    await expectNoSeriousA11yViolations(page);
    await confirm.getByRole("button", { name: en["action.cancel"], exact: true }).click();
    await expect(confirm).toBeHidden();

    expect(saves.map((request) => request.url()), "settings saves sent").toEqual([]);
    const after = await readSettings(api);
    expect(after.version.id, "the active settings version").toBe(before.version.id);
    expect(isB(after.payload.scoring.model_policy), "preset B still active").toBe(true);
    await expectNoSeriousA11yViolations(page);
  } finally {
    await api.dispose();
  }
});
