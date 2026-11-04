// E13 (spec §18.7, §21 "intervals stay populated throughout", §13.4, §15.7, §15.1; plan M4 Task 34, Ruling T34-b):
// the setup's Berlin location from 30 to 50 km in Search scope, and the scores' intervals populated throughout the
// scope apply that the save starts. The apply takes 0.1–0.5 s on the fixtures, so the plan's "checked twice, 3 s
// apart, while the banner shows" cannot be watched (a plan error, Ruling T34-b); the spec asserts the window:
// 1. Before the save: `S`, the scored jobs of the default list's first page (each L ≤ M ≤ U), each card showing
//    its interval.
// 2. The save through the UI: "No LLM cost — n scores will be recomputed" (the dry-run's `det_recompute`), then
//    Save: 201, `scope` among its effect classes, the apply's operation id.
// 3. The header's first status read after the save: when it caught the scope pending, the banner the page shows
//    is the one built from that read's own params ("Applying settings… n/N", or "Applying settings…" before the
//    apply's first count); when the apply had flipped already, the scope is applied and no apply banner shows.
//    Either way the banner is right; the branch taken is annotated.
// 4. From the 201 until the scope is applied, the list is read through the API again and again with no pause:
//    every listed job of `S` keeps a score with its interval. The samples taken while the scope was pending are
//    counted (annotated, not required: requiring one would bring the race back).
// 5. The apply did what the dry-run said (`psql`, read only): `succeeded`, every job done, and `n` published
//    results recomputed.
// 6. After a reload (the status read again): no apply banner, and every card of `S` listed shows its interval.
// At the end the radius goes back to 30 km through the API, and that apply is awaited (`withRestore`).
import type { Page, Request, Response } from "@playwright/test";
import type { components } from "../../src/api/schema";
import { expectNoSeriousA11yViolations } from "../support/a11y";
import { answerTo, type ApiContext, apiContext, getJson } from "../support/api";
import { en, expect, filled, templateMatch, test } from "../support/fixtures";
import { cardOf, firstPageRead, type JobsPageOut, SCORE_LABEL, shownPage } from "../support/jobs";
import { readSettings, type SaveOut, saveSettings, withRestore } from "../support/settings";
import { psql, waitFor } from "../support/stack";

type StatusOut = components["schemas"]["StatusOut"];
type BannerOut = StatusOut["banners"][number];

const SETUP_PLACE = "Berlin";
const FROM_KM = 30;
const TO_KM = 50;
const APPLIED_WITHIN_MS = 120_000;
const STATUS_PATH = "/api/system/status";

// "Applying settings… n/N", or "Applying settings…" while the apply has counted nothing yet.
const APPLYING = new RegExp(
  `${templateMatch(en["banner.applying_settings"], { done: /[\d,]+/, total: /[\d,]+/ }).source}|` +
    templateMatch(en["banner.applying_settings_starting"]).source,
);
// "No LLM cost — n score(s) will be recomputed", n captured.
const RECOMPUTE = new RegExp(
  [en["settings_scope.recompute_one"], en["settings_scope.recompute_other"]]
    .map((text) => templateMatch(text, { value: /([\d,]+)/ }).source)
    .join("|"),
);

// The window is asserted whichever side of the status read the apply lands on, but the spec saves and restores
// the scope: a retry would only repeat a failure's cause (Ruling T33-b).
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

/** Whether a listed card's score has its interval, L ≤ M ≤ U. */
function hasInterval(score: JobsPageOut["items"][number]["score"]): boolean {
  return score !== null && Number(score.low) <= Number(score.mid) && Number(score.mid) <= Number(score.high);
}

/** The answer to the first status read the page sends after `trigger`'s answer (the save's): a request event
 * comes after the response event that made the page send it, so a poll sent before is never taken. */
function statusReadAfter(page: Page, trigger: (response: Response) => boolean): Promise<Response> {
  return new Promise((resolve, reject) => {
    let after = false;
    const onResponse = (response: Response) => {
      after ||= trigger(response);
    };
    const onRequest = (request: Request) => {
      if (after && request.method() === "GET" && new URL(request.url()).pathname === STATUS_PATH) {
        page.off("response", onResponse);
        page.off("request", onRequest);
        request.response().then(
          (response) => {
            if (response === null) {
              reject(new Error("the status read after the save got no answer"));
            } else {
              resolve(response);
            }
          },
          (error: unknown) => {
            reject(error instanceof Error ? error : new Error(String(error)));
          },
        );
      }
    };
    page.on("response", onResponse);
    page.on("request", onRequest);
  });
}

/** The banner text the header shows for `banner` (`Banners.tsx`), its counts in the page's own `Intl`. */
async function applyingText(page: Page, banner: BannerOut): Promise<string> {
  const { done, total, failed } = banner.params;
  if (failed === true) {
    return en["banner.applying_settings_failed"];
  }
  if (typeof total !== "number" || total === 0) {
    return en["banner.applying_settings_starting"];
  }
  const [doneText, totalText] = await page.evaluate(
    (counts) => counts.map((n) => new Intl.NumberFormat("en-GB").format(n)),
    [typeof done === "number" ? done : 0, total],
  );
  return filled(en["banner.applying_settings"], { done: doneText, total: totalText });
}

test("E13: a radius change recomputes the scores without the LLM, and the intervals stay populated throughout", async ({
  page,
}) => {
  test.setTimeout(3 * APPLIED_WITHIN_MS + 60_000); // the three bounded applies, and the pages around them
  const api = await apiContext(page);
  try {
    const before = (await readSettings(api)).payload.scope.locations;
    const berlin = before.find((place) => place.label === SETUP_PLACE);
    expect(berlin?.radius_km, `the setup's ${SETUP_PLACE} in the scope, at ${String(FROM_KM)} km`).toBe(FROM_KM);
    await applied(api, "no scope apply before E13's");
    const banner = page.getByRole("status").filter({ hasText: APPLYING });

    // 1. The baseline: the scored jobs of the first page, each card showing its interval.
    const listed = firstPageRead(page);
    await page.goto("/");
    const baseline = await shownPage(page, listed);
    const scored = baseline.items.filter((card) => card.score !== null);
    expect(scored.length, "scored cards on the Jobs page").toBeGreaterThan(0);
    expect(scored.filter((card) => !hasInterval(card.score)).map((card) => card.job_id), "L ≤ M ≤ U").toEqual([]);
    const S = new Set(scored.map((card) => card.job_id));
    for (const jobId of S) {
      await expect(cardOf(page, jobId).getByRole("img", { name: SCORE_LABEL }), `job ${String(jobId)} before`).toBeVisible();
    }

    const restore = async () => {
      const what = `the radius back to ${String(FROM_KM)} km`;
      const restored = await saveSettings(api, what, (p) => {
        p.scope.locations = before;
      });
      expect(restored.response.status(), what).toBe(201);
      await applied(api, `the ${String(FROM_KM)} km apply`);
    };
    await withRestore(async () => {
      // 2. The radius to 50 km in Search scope, and the save.
      await page.goto("/settings#scope");
      const scope = page.getByRole("region", { name: en["settings.section.scope"] });
      const slider = scope.getByRole("listitem").filter({ hasText: SETUP_PLACE }).getByRole("slider", { name: en["settings_scope.radius"] });
      await expect(slider).toHaveAttribute("aria-valuenow", String(FROM_KM));
      await slider.focus();
      for (let km = FROM_KM; km < TO_KM; km++) {
        await page.keyboard.press("ArrowRight");
      }
      await expect(slider).toHaveAttribute("aria-valuenow", String(TO_KM));
      const recompute = scope.getByText(RECOMPUTE);
      await expect(recompute).toBeVisible();
      // the capture of the form that matched (`recompute_one` or `_other`): the other group is undefined
      const captured = RECOMPUTE.exec((await recompute.textContent()) ?? "")?.slice(1).find(Boolean);
      const n = Number((captured ?? "").replace(/,/g, ""));
      expect(Number.isInteger(n) && n > 0, `the dry-run's recompute count (${String(captured)})`).toBe(true);
      for (const tick of await scope.getByRole("list", { name: en["settings_form.warnings"] }).getByRole("checkbox").all()) {
        await tick.check();
      }
      const save = scope.getByRole("button", { name: en["settings_form.save"], exact: true });
      await expect(save).toBeEnabled();
      await expectNoSeriousA11yViolations(page);

      const isSave = (response: Response) =>
        response.request().method() === "POST" && new URL(response.url()).pathname === "/api/settings";
      const statusRead = statusReadAfter(page, isSave);
      const answered = answerTo(page, "POST", "/api/settings");
      await save.click();
      const response = await answered;
      expect(response.status(), "the save of the radius").toBe(201);
      const saved = (await response.json()) as SaveOut;
      expect(saved.version.effect_classes).toContain("scope");
      const operation = saved.apply_scope_operation_id;
      expect(operation, "the save's scope apply").not.toBeNull();

      // 4. The window, sampled from the 201 on: the list read, then the settings (a sample is in the window when
      // the scope is still pending after its list read). Every listed job of S keeps its interval.
      const progress: string[] = [];
      let samples = 0;
      let inWindow = 0;
      const deadline = Date.now() + APPLIED_WITHIN_MS;
      for (;;) {
        const page1 = await getJson<JobsPageOut>(api, "/api/jobs?limit=50");
        const settings = await readSettings(api);
        samples++;
        const lost = page1.items.filter((card) => S.has(card.job_id) && !hasInterval(card.score));
        expect(lost.map((card) => card.job_id), `sample ${String(samples)}: jobs of S listed without an interval`).toEqual([]);
        if (settings.pending_scope !== null) {
          inWindow++;
          progress.push(JSON.stringify(settings.pending_scope.progress));
        } else if (settings.scope_applied) {
          break;
        }
        expect(Date.now(), `the ${String(TO_KM)} km apply within ${String(APPLIED_WITHIN_MS / 1000)} s`).toBeLessThan(deadline);
      }
      const sampled = `${String(samples)} samples, ${String(inWindow)} while the scope was pending (progress ${progress.join(", ") || "none"})`;
      test.info().annotations.push({ type: "apply window", description: sampled });
      console.log(`E13: ${sampled}`);

      // 3. The first status read after the save, and the banner the page shows from it.
      const status = (await (await statusRead).json()) as StatusOut;
      const applying = status.banners.find((item) => item.code === "applying_settings");
      let branch: string;
      if (status.scope.pending_version_id !== null) {
        expect(applying, "the status read with the scope pending names the apply").toBeDefined();
        const text = await applyingText(page, applying as BannerOut);
        await expect(page.getByRole("status").filter({ hasText: text })).toHaveText(text);
        branch = `pending at the read: "${text}"`;
      } else {
        expect([status.scope.applied, applying], "the status read after the flip").toEqual([true, undefined]);
        await expect(banner).toHaveCount(0);
        branch = "applied before the read: no apply banner";
      }
      test.info().annotations.push({ type: "status read", description: branch });
      console.log(`E13: the status read after the save: ${branch}`);
      await expectNoSeriousA11yViolations(page);

      // 5. What the apply did, read only: succeeded, every job done, the n results the dry-run named.
      const done = await psql(
        "SELECT status || '|' || (progress->>'done') || '|' || (progress->>'total') || '|' || " +
          `(progress->>'det_done') || '|' || (progress->>'det_total') FROM operations WHERE id = ${String(operation)}`,
      );
      const [state, jobsDone, jobsTotal, resultsDone, resultsTotal] = done.split("|");
      expect([state, jobsDone, resultsDone, resultsTotal], `operation ${String(operation)}: ${done}`).toEqual([
        "succeeded",
        jobsTotal,
        String(n),
        String(n),
      ]);

      // 6. After the flip: the status read again, no apply banner, and the intervals shown.
      const reread = answerTo(page, "GET", STATUS_PATH);
      const relisted = firstPageRead(page);
      await page.goto("/");
      const after = (await (await reread).json()) as StatusOut;
      expect(after.banners.map((item) => item.code), "the status after the apply").not.toContain("applying_settings");
      const shown = await shownPage(page, relisted);
      await expect(banner).toHaveCount(0);
      for (const card of shown.items.filter((item) => S.has(item.job_id))) {
        await expect(cardOf(page, card.job_id).getByRole("img", { name: SCORE_LABEL }), `job ${String(card.job_id)} after`).toBeVisible();
      }
      await expectNoSeriousA11yViolations(page);
    }, restore);
    expect((await readSettings(api)).payload.scope.locations).toEqual(before);
  } finally {
    await api.dispose();
  }
});
