// E2 (spec §18.7, §15.3; plan M4 Task 32): on the empty stack, before any setup, the Jobs page prompts for a CV
// upload, and the prompt opens the CV section of Profile & Settings. On the saved session.
import { expectNoSeriousA11yViolations } from "../support/a11y";
import { en, expect, test } from "../support/fixtures";

test("E2: the empty Jobs page prompts a CV upload, and the prompt opens the CV section", async ({ page }) => {
  await page.goto("/");
  const prompt = page.getByRole("link", { name: en["jobs.no_profile"] });
  await expect(prompt).toBeVisible();
  await expectNoSeriousA11yViolations(page);
  await prompt.click();
  await expect(page).toHaveURL(/\/settings#cv$/);
  await expect(page.getByRole("region", { name: en["settings.section.cv"] })).toBeInViewport();
  await expectNoSeriousA11yViolations(page);
});
