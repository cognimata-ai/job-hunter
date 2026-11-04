// E11 (spec §18.7, §9.6.4, §15.3, §10.8; plan M4 Task 34, resolution f): Score now on a pending card answers
// 202, and after the cycle the card shows a score. Then the cap: the active `llm.cap_usd` is read, and
// `llm.cap_usd = "1.00"` saved through the API (the model's minimum; with the default overrun buffer of $2 the
// scoring headroom is 0 whatever the counted spend, below one attempt's first reservation, so the lane's pause
// reason is `cap_reached`): 201 with `budget` among its effect classes. The status then says `cap_reached`, a
// pending card's Score now is disabled with its explanation, and score-now through the API answers 409
// `budget_exhausted`. At the end the cap read at the start is saved again (201), and the pause is gone.
import type { components } from "../../src/api/schema";
import { expectNoSeriousA11yViolations } from "../support/a11y";
import { answerTo, type ApiContext, apiContext, getJson } from "../support/api";
import { en, expect, test } from "../support/fixtures";
import { cardOf, pendingJob, publishedDetail, SCORE_LABEL, SCORE_NOW, searchFor } from "../support/jobs";
import { readSettings, type SaveOut, type Saved, saveSettings, withRestore } from "../support/settings";

type SpendOut = components["schemas"]["SpendOut"];
type StatusOut = components["schemas"]["StatusOut"];

const MIN_CAP_USD = "1.00";

function spend(api: ApiContext): Promise<SpendOut> {
  return getJson<SpendOut>(api, "/api/system/spend");
}

async function savedVersion(saved: Saved, what: string): Promise<SaveOut> {
  expect(saved.response.status(), what).toBe(201);
  return (await saved.response.json()) as SaveOut;
}

test("E11: Score now scores a pending card; under the minimum cap it is disabled and refused, and the cap comes back", async ({
  page,
}) => {
  test.setTimeout(360_000);
  const api = await apiContext(page);
  try {
    await test.step("Score now on a pending card, and its score after the cycle", async () => {
      const pending = await pendingJob(api);
      await page.goto("/");
      await searchFor(page, pending.title);
      const card = cardOf(page, pending.job_id);
      const answered = answerTo(page, "POST", `/api/jobs/${String(pending.job_id)}/score-now`);
      await card.getByRole("button", { name: SCORE_NOW }).click();
      expect((await answered).status(), `Score now of job ${String(pending.job_id)}`).toBe(202);
      await publishedDetail(api, pending.job_id, "E11's Score now");
      await page.reload();
      await expect(card.getByRole("img", { name: SCORE_LABEL })).toBeVisible();
      await expectNoSeriousA11yViolations(page);
    });

    const cap = (await readSettings(api)).payload.llm.cap_usd;
    const restore = async () => {
      const what = `the save of the cap read at the start (${cap})`;
      await savedVersion(await saveSettings(api, what, (p) => {
        p.llm.cap_usd = cap;
      }), what);
    };
    await withRestore(async () => {
      await test.step(`the cap saved at ${MIN_CAP_USD} (from ${cap})`, async () => {
        const saved = await savedVersion(await saveSettings(api, `llm.cap_usd = ${MIN_CAP_USD}`, (p) => {
          p.llm.cap_usd = MIN_CAP_USD;
        }), "the save of the minimum cap");
        expect(saved.version.effect_classes).toContain("budget");
        expect((await spend(api)).pause_reason).toBe("cap_reached");
        const status = await getJson<StatusOut>(api, "/api/system/status");
        expect(status.banners.map((banner) => banner.code)).toContain("cap_reached");
      });

      await test.step("Score now disabled with its explanation, and refused through the API", async () => {
        const pending = await pendingJob(api);
        await page.goto("/");
        await expect(page.getByText(en["banner.cap_reached"], { exact: true })).toBeVisible();
        await searchFor(page, pending.title);
        const button = cardOf(page, pending.job_id).getByRole("button", { name: SCORE_NOW });
        await expect(button).toBeDisabled();
        await expect(button).toHaveAccessibleDescription(en["card.score_now_cap"]);
        await expectNoSeriousA11yViolations(page);
        const refused = await api.post(`/api/jobs/${String(pending.job_id)}/score-now`, {
          data: {},
          headers: { "Idempotency-Key": api.key() },
        });
        expect(refused.status()).toBe(409);
        expect(((await refused.json()) as { code: string }).code).toBe("budget_exhausted");
      });
    }, restore);
    expect((await readSettings(api)).payload.llm.cap_usd).toBe(cap);
    expect((await spend(api)).pause_reason).not.toBe("cap_reached");
  } finally {
    await api.dispose();
  }
});
