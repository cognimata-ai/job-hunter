// E20 (spec §18.7, §15.9; plan M4 Task 34): the Jobs page by the keyboard alone. `j` and `k` move focus between
// the cards, Enter opens the focused card's drawer, Tab cycles inside the drawer (focus never leaves it, and comes
// round to where it started), Esc closes it with focus back on the same card; Shift+Tab reaches the Label mode
// toggle and Enter turns it on; there `j` reaches the batch and `1`, `2` and `3` label three ads (each card's own
// job answering), focus moving on to the next ad to label after each.
import { expectNoSeriousA11yViolations } from "../support/a11y";
import { en, expect, test } from "../support/fixtures";
import { cardJobId, cards, enterLabelMode, firstPageRead, LABEL_KEYS, labelCardJob, labelWith, shownPage } from "../support/jobs";

const TAB_CAP = 400; // more stops than any drawer (or the way back to the filters) has: a leak or no end fails

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

  // Label mode by the keyboard: Shift+Tab from the card back to the filter panel's toggle (the panel is before
  // the list), then Enter.
  const toggle = page.getByRole("button", { name: en["filters.label_mode"], exact: true });
  const { queue, mode, cards: batch, progress } = await enterLabelMode(
    page,
    async () => {
      let stops = 0;
      for (; stops < TAB_CAP && !(await toggle.evaluate((element) => element === document.activeElement)); stops++) {
        await page.keyboard.press("Shift+Tab");
      }
      expect(stops, `Shift+Tab reaches Label mode within ${String(TAB_CAP)} presses`).toBeLessThan(TAB_CAP);
      await page.keyboard.press("Enter");
    },
    "E20",
  );
  await expectNoSeriousA11yViolations(page);

  await page.keyboard.press("j");
  await expect(batch.nth(0)).toBeFocused();
  for (const [i, keyed] of LABEL_KEYS.entries()) {
    const card = batch.nth(i);
    await expect(card, `the ad to label next (${keyed[0]})`).toBeFocused();
    await labelWith(page, card, await labelCardJob(card, queue.items), keyed, () => page.keyboard.press(keyed[0]));
  }
  // focus moves on to the next ad to label, else (a batch of three) to Next batch
  const next =
    queue.items.length > LABEL_KEYS.length
      ? batch.nth(LABEL_KEYS.length)
      : mode.getByRole("button", { name: en["label_mode.next_batch"] });
  await expect(next).toBeFocused();
  await expect(progress).toHaveAttribute("aria-valuenow", String(queue.progress.labelled + LABEL_KEYS.length));
  await expectNoSeriousA11yViolations(page);
});
