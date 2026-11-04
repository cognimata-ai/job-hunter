// E12 (spec §18.7, §15.6, §11.13; plan M4 Task 34): Label mode shows a batch of ads without any score; `1`, `2`
// and `3` label three cards Good, OK and Poor, each card's own job (`labelCardJob`) answering; the progress goes
// up by three; Reveal score shows a labelled card's score. The batch must be unlabelled ads (`enterLabelMode`),
// else the spec fails naming that precondition: the queue gives labelled ones only once the unlabelled run out.
import { expectNoSeriousA11yViolations } from "../support/a11y";
import { en, expect, test } from "../support/fixtures";
import { enterLabelMode, LABEL_KEYS, labelCardJob, labelWith, SCORE_LABEL } from "../support/jobs";

// Labels are written once per job: a retry would label other ads, hiding the first failure (Ruling T33-b).
test.describe.configure({ retries: 0 });

test("E12: Label mode hides the scores, 1/2/3 label three ads, the progress counts them and Reveal shows a score", async ({
  page,
}) => {
  await page.goto("/");
  const toggle = page.getByRole("button", { name: en["filters.label_mode"], exact: true });
  const { queue, cards, progress } = await enterLabelMode(page, () => toggle.click(), "E12");
  await expect(progress).toHaveAttribute("aria-valuenow", String(queue.progress.labelled));
  await expect(page.getByRole("img", { name: SCORE_LABEL })).toHaveCount(0);
  await expectNoSeriousA11yViolations(page);

  for (const [i, keyed] of LABEL_KEYS.entries()) {
    const card = cards.nth(i);
    await labelWith(page, card, await labelCardJob(card, queue.items), keyed, () => card.press(keyed[0]));
  }
  await expect(progress).toHaveAttribute("aria-valuenow", String(queue.progress.labelled + LABEL_KEYS.length));
  await expect(page.getByRole("img", { name: SCORE_LABEL })).toHaveCount(0);
  await expectNoSeriousA11yViolations(page);

  const first = cards.nth(0);
  await first.getByRole("button", { name: en["label_mode.reveal"] }).click();
  await expect(first.getByRole("img", { name: SCORE_LABEL })).toBeVisible();
  await expectNoSeriousA11yViolations(page);
});
