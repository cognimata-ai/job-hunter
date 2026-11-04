// E13 (spec §18.7, §13.4, §15.7, §15.1; plan M4 Task 34, resolution f): the setup's Berlin location from 30 to
// 50 km in Search scope. The scope section says "No LLM cost — n scores will be recomputed" (the dry-run's
// `det_recompute`); Save (201) starts the scope apply, and the header says "Applying settings… n/N" ("Applying
// settings…" before the apply's first count). While it says so, every scored card on the Jobs page (reached in
// the app, so the header keeps its status) still shows its interval, checked twice 3 s apart; then the banner
// goes (the status is read every 60 s). At the end the radius goes back to 30 km through the API, and that apply
// is awaited, so a later spec starts on the setup's scope.
import type { Page } from "@playwright/test";
import { expectNoSeriousA11yViolations } from "../support/a11y";
import { answerTo, type ApiContext, apiContext } from "../support/api";
import { en, expect, templateMatch, test } from "../support/fixtures";
import { cardOf, firstPageRead, SCORE_LABEL, shownPage } from "../support/jobs";
import { readSettings, type SaveOut, saveSettings, withRestore } from "../support/settings";
import { waitFor } from "../support/stack";

const SETUP_PLACE = "Berlin";
const FROM_KM = 30;
const TO_KM = 50;
const APPLIED_WITHIN_MS = 120_000;
const BANNER_GONE_MS = 90_000; // the header's status is read again every 60 s
const RECHECK_AFTER_MS = 3_000;

// "Applying settings… n/N", or "Applying settings…" while the apply has counted nothing yet.
const APPLYING = new RegExp(
  `${templateMatch(en["banner.applying_settings"], { done: /[\d,]+/, total: /[\d,]+/ }).source}|` +
    templateMatch(en["banner.applying_settings_starting"]).source,
);

// The apply's banner is a moment the spec must catch: a retry would only re-time it (Ruling T33-b).
test.describe.configure({ retries: 0 });

/** The scope's apply finished: no scope pending, the active one applied. */
async function applied(api: ApiContext, what: string): Promise<void> {
  await waitFor(
    async () => {
      const settings = await readSettings(api);
      return settings.pending_scope === null && settings.scope_applied ? true : null;
    },
    APPLIED_WITHIN_MS,
    what,
  );
}

/** Every scored card of the Jobs page's first page shows its interval. */
async function scoredShowIntervals(page: Page, scored: readonly number[], check: string): Promise<void> {
  for (const jobId of scored) {
    await expect(cardOf(page, jobId).getByRole("img", { name: SCORE_LABEL }), `${check}: job ${String(jobId)}`).toBeVisible();
  }
}

test("E13: a radius change recomputes the scores without the LLM, and the cards keep their intervals while it applies", async ({
  page,
}) => {
  test.setTimeout(420_000);
  const api = await apiContext(page);
  try {
    const before = (await readSettings(api)).payload.scope.locations;
    const index = before.findIndex((place) => place.label === SETUP_PLACE);
    expect(before[index]?.radius_km, `the setup's ${SETUP_PLACE} in the scope, at ${String(FROM_KM)} km`).toBe(FROM_KM);
    await applied(api, "no scope apply before E13's");
    const restore = async () => {
      const what = `the radius back to ${String(FROM_KM)} km`;
      const restored = await saveSettings(api, what, (p) => {
        p.scope.locations = before;
      });
      expect(restored.response.status(), what).toBe(201);
      await applied(api, `the ${String(FROM_KM)} km apply`);
    };
    await withRestore(async () => {
      await page.goto("/settings#scope");
      const scope = page.getByRole("region", { name: en["settings.section.scope"] });
      const row = scope.getByRole("listitem").filter({ hasText: SETUP_PLACE });
      const slider = row.getByRole("slider", { name: en["settings_scope.radius"] });
      await expect(slider).toHaveAttribute("aria-valuenow", String(FROM_KM));
      await slider.focus();
      for (let km = FROM_KM; km < TO_KM; km++) {
        await page.keyboard.press("ArrowRight");
      }
      await expect(slider).toHaveAttribute("aria-valuenow", String(TO_KM));
      await expect(scope.getByText(templateMatch(en["settings_scope.recompute_other"], { value: /[\d,]+/ }))).toBeVisible();
      const ticks = scope.getByRole("list", { name: en["settings_form.warnings"] }).getByRole("checkbox");
      for (const tick of await ticks.all()) {
        await tick.check();
      }
      const save = scope.getByRole("button", { name: en["settings_form.save"], exact: true });
      await expect(save).toBeEnabled();
      await expectNoSeriousA11yViolations(page);

      const answered = answerTo(page, "POST", "/api/settings");
      await save.click();
      const response = await answered;
      expect(response.status(), "the save of the radius").toBe(201);
      const saved = (await response.json()) as SaveOut;
      expect(saved.version.effect_classes).toContain("scope");
      expect(saved.apply_scope_operation_id, "the save's scope apply").not.toBeNull();
      const banner = page.getByRole("status").filter({ hasText: APPLYING });
      await expect(banner).toBeVisible();
      await expectNoSeriousA11yViolations(page);

      const listed = firstPageRead(page);
      await page.getByRole("navigation", { name: en["nav.label"] }).getByRole("link", { name: en["nav.jobs"], exact: true }).click();
      const answer = await shownPage(page, listed);
      const scored = answer.items.filter((card) => card.score !== null).map((card) => card.job_id);
      expect(scored.length, "scored cards on the Jobs page").toBeGreaterThan(0);
      await expect(banner).toBeVisible();
      await scoredShowIntervals(page, scored, "the first check while the banner shows");
      await page.waitForTimeout(RECHECK_AFTER_MS);
      await expect(banner).toBeVisible();
      await scoredShowIntervals(page, scored, "the second check, 3 s on, while the banner shows");
      await expectNoSeriousA11yViolations(page);
      await expect(banner).toBeHidden({ timeout: BANNER_GONE_MS });
      await applied(api, "the 50 km apply");
    }, restore);
    expect((await readSettings(api)).payload.scope.locations).toEqual(before);
  } finally {
    await api.dispose();
  }
});
