// E12 (spec §18.7, §15.6, §11.13; plan M4 Task 34): Label mode shows a batch of ads without any score; `1`, `2`
// and `3` label three cards Good, OK and Poor; the progress goes up by three; Reveal score shows a labelled
// card's score. The batch must be unlabelled ads (`labelledJobs`), else the spec fails naming that precondition:
// the labelling queue gives labelled ones only once the unlabelled ones run out.
import type { components } from "../../src/api/schema";
import { expectNoSeriousA11yViolations } from "../support/a11y";
import { en, expect, filled, test } from "../support/fixtures";
import { labelledJobs, SCORE_LABEL } from "../support/jobs";

type LabellingQueueOut = components["schemas"]["LabellingQueueOut"];

const KEYS = [
  ["1", "good"],
  ["2", "ok"],
  ["3", "poor"],
] as const;

// Labels are written once per job: a retry would label other ads, hiding the first failure (Ruling T33-b).
test.describe.configure({ retries: 0 });

test("E12: Label mode hides the scores, 1/2/3 label three ads, the progress counts them and Reveal shows a score", async ({
  page,
}) => {
  const queueRead = page.waitForResponse(
    (response) => response.request().method() === "GET" && new URL(response.url()).pathname === "/api/labelling/queue",
  );
  await page.goto("/");
  await page.getByRole("button", { name: en["filters.label_mode"], exact: true }).click();
  const queue = (await (await queueRead).json()) as LabellingQueueOut;
  const ids = queue.items.map((item) => item.job_id);
  expect(ids.length, "ads in the labelling queue's batch").toBeGreaterThanOrEqual(KEYS.length);
  expect(await labelledJobs(ids), "labelled ads in the batch: E12 needs a batch of unlabelled ads").toEqual([]);

  const mode = page.getByRole("region", { name: en["label_mode.title"] });
  const batch = mode.getByRole("list", { name: en["label_mode.queue"] }).getByRole("article");
  await expect(batch).toHaveCount(ids.length);
  const progress = mode.getByRole("progressbar", { name: en["label_mode.progress_label"] });
  await expect(progress).toHaveAttribute("aria-valuenow", String(queue.progress.labelled));
  await expect(page.getByRole("img", { name: SCORE_LABEL })).toHaveCount(0);
  await expectNoSeriousA11yViolations(page);

  for (const [i, [key, label]] of KEYS.entries()) {
    const card = batch.nth(i);
    const saved = page.waitForResponse(
      (response) => response.request().method() === "POST" && /^\/api\/jobs\/\d+\/feedback$/.test(new URL(response.url()).pathname),
    );
    await card.press(key);
    const response = await saved;
    expect(response.status(), `the label of key ${key}`).toBe(201);
    expect(response.request().postDataJSON()).toMatchObject({ kind: "label", label });
    await expect(card.getByText(filled(en["label_mode.labelled"], { label: en[`feedback_label.${label}`] }), { exact: true })).toBeVisible();
  }
  await expect(progress).toHaveAttribute("aria-valuenow", String(queue.progress.labelled + KEYS.length));
  await expect(page.getByRole("img", { name: SCORE_LABEL })).toHaveCount(0);
  await expectNoSeriousA11yViolations(page);

  const first = batch.nth(0);
  await first.getByRole("button", { name: en["label_mode.reveal"] }).click();
  await expect(first.getByRole("img", { name: SCORE_LABEL })).toBeVisible();
  await expectNoSeriousA11yViolations(page);
});
