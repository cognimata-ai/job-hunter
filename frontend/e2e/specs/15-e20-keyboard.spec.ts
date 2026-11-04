// E20 (spec §18.7, §15.9; plan M4 Task 34): the Jobs page by the keyboard alone. `j` and `k` move focus between
// the cards, Enter opens the focused card's drawer, Tab cycles inside the drawer (focus never leaves it, and comes
// round to where it started), Esc closes it with focus back on the same card; in Label mode `j` reaches the
// batch and `1`, `2` and `3` label three ads, focus moving on to the next ad to label after each.
import type { components } from "../../src/api/schema";
import { expectNoSeriousA11yViolations } from "../support/a11y";
import { en, expect, filled, test } from "../support/fixtures";
import { cardJobId, cards, firstPageRead, labelledJobs, shownPage } from "../support/jobs";

type LabellingQueueOut = components["schemas"]["LabellingQueueOut"];

const TAB_CAP = 400; // more stops than any drawer has: a trap that leaks or never comes round fails
const KEYS = [
  ["1", "good"],
  ["2", "ok"],
  ["3", "poor"],
] as const;

// Labels are written once per job: a retry would label other ads, hiding the first failure (Ruling T33-b).
test.describe.configure({ retries: 0 });

declare global {
  interface Window {
    e2eFirstStop?: Element | null;
  }
}

test("E20: j/k, Enter, Tab, Esc and 1/2/3 work the list, the drawer and Label mode", async ({ page }) => {
  const first = firstPageRead(page);
  await page.goto("/");
  const listed = await shownPage(page, first);
  expect(listed.items.length, "cards listed").toBeGreaterThanOrEqual(2);
  const [one, two] = [cards(page).nth(0), cards(page).nth(1)];

  await page.keyboard.press("j");
  await expect(one).toBeFocused();
  await page.keyboard.press("j");
  await expect(two).toBeFocused();
  await page.keyboard.press("k");
  await expect(one).toBeFocused();
  await page.keyboard.press("j");
  await expect(two).toBeFocused();

  const jobId = await cardJobId(two);
  const title = listed.items.find((card) => card.job_id === jobId)?.title ?? "";
  await page.keyboard.press("Enter");
  const drawer = page.getByRole("dialog", { name: title, exact: true });
  await expect(drawer).toBeVisible();
  expect(new URL(page.url()).searchParams.get("job")).toBe(String(jobId));
  await expect(drawer.getByRole("heading", { level: 2, name: title })).toBeVisible();
  await expectNoSeriousA11yViolations(page);

  await page.keyboard.press("Tab");
  await page.evaluate(() => {
    window.e2eFirstStop = document.activeElement;
  });
  let stops = 1;
  for (; stops <= TAB_CAP; stops++) {
    await page.keyboard.press("Tab");
    const where = await drawer.evaluate((dialog) => ({
      inside: dialog.contains(document.activeElement),
      back: document.activeElement === window.e2eFirstStop,
    }));
    expect(where.inside, `Tab stop ${String(stops)} stays in the drawer`).toBe(true);
    if (where.back) {
      break;
    }
  }
  expect(stops, `Tab comes back to the first stop within ${String(TAB_CAP)} presses`).toBeLessThanOrEqual(TAB_CAP);
  test.info().annotations.push({ type: "tab cycle", description: `${String(stops)} stops in job ${String(jobId)}'s drawer` });

  await page.keyboard.press("Escape");
  await expect(drawer).toBeHidden();
  await expect(two).toBeFocused();

  const queueRead = page.waitForResponse(
    (response) => response.request().method() === "GET" && new URL(response.url()).pathname === "/api/labelling/queue",
  );
  await page.getByRole("button", { name: en["filters.label_mode"], exact: true }).press("Enter");
  const queue = (await (await queueRead).json()) as LabellingQueueOut;
  const ids = queue.items.map((item) => item.job_id);
  expect(ids.length, "ads in the labelling queue's batch").toBeGreaterThanOrEqual(KEYS.length);
  expect(await labelledJobs(ids), "labelled ads in the batch: E20 needs a batch of unlabelled ads").toEqual([]);
  const mode = page.getByRole("region", { name: en["label_mode.title"] });
  const batch = mode.getByRole("list", { name: en["label_mode.queue"] }).getByRole("article");
  await expect(batch).toHaveCount(ids.length);
  const progress = mode.getByRole("progressbar", { name: en["label_mode.progress_label"] });
  await expectNoSeriousA11yViolations(page);

  await page.keyboard.press("j");
  await expect(batch.nth(0)).toBeFocused();
  for (const [i, [key, label]] of KEYS.entries()) {
    await expect(batch.nth(i), `the ad to label next (${key})`).toBeFocused();
    const saved = page.waitForResponse(
      (response) => response.request().method() === "POST" && /^\/api\/jobs\/\d+\/feedback$/.test(new URL(response.url()).pathname),
    );
    await page.keyboard.press(key);
    expect((await saved).status(), `the label of key ${key}`).toBe(201);
    await expect(batch.nth(i).getByText(filled(en["label_mode.labelled"], { label: en[`feedback_label.${label}`] }), { exact: true })).toBeVisible();
  }
  // focus moves on to the next ad to label, else (a batch of three) to Next batch
  const next = ids.length > KEYS.length ? batch.nth(KEYS.length) : mode.getByRole("button", { name: en["label_mode.next_batch"] });
  await expect(next).toBeFocused();
  await expect(progress).toHaveAttribute("aria-valuenow", String(queue.progress.labelled + KEYS.length));
  await expectNoSeriousA11yViolations(page);
});
