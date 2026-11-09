// E1 (spec §18.7, §15.2, §16.3; plan M4 Task 32): a login lands on Jobs; a wrong password shows the one
// generic "Invalid credentials"; the sixth failure from the address is throttled (5 failures in 15 min lock it
// out). The spec ends with `jobhunter sessions unlock` (§16.3's escape hatch) so later logins work, also when it
// fails. It starts without the saved session and signs in itself (its own user agent). The throttle's attempts
// stay recorded after an unlock, so a retry of this spec would meet a lockout at its first failure: it runs once.
import { expectNoSeriousA11yViolations } from "../support/a11y";
import { en, expect, signInThroughUi, test, textPattern } from "../support/fixtures";
import { compose, smokeUser } from "../support/stack";

const FAILURES_BEFORE_LOCKOUT = 5; // throttle.IP_MAX_FAILURES
const WRONG_PASSWORD = "not-the-smoke-password-e2e";

test.use({ storageState: { cookies: [], origins: [] }, userAgent: "jobhunter-e2e-01" });
test.describe.configure({ retries: 0 });

test.afterAll(async () => {
  await compose(["run", "--rm", "-T", "api", "jobhunter", "sessions", "unlock"]);
});

test("E1: a login lands on Jobs, a wrong password is refused generically, the sixth failure is throttled", async ({
  page,
  context,
}) => {
  const user = await smokeUser();
  await page.goto("/login");
  await expectNoSeriousA11yViolations(page);
  expect((await signInThroughUi(page, user)).status()).toBe(204);
  await expect(page).toHaveURL("/");
  await expect(page.getByRole("main")).toBeVisible();
  await expectNoSeriousA11yViolations(page);

  await context.clearCookies();
  await page.goto("/login");
  const alert = page.getByRole("alert");
  for (let failure = 1; failure <= FAILURES_BEFORE_LOCKOUT; failure += 1) {
    expect((await signInThroughUi(page, user, WRONG_PASSWORD)).status(), `failure ${String(failure)}`).toBe(401);
    await expect(alert).toHaveText(en["login.invalid"]);
  }
  await expectNoSeriousA11yViolations(page);
  expect((await signInThroughUi(page, user, WRONG_PASSWORD)).status()).toBe(429);
  await expect(alert).toHaveText(textPattern(en["login.throttled"]));
  await expectNoSeriousA11yViolations(page);
  await expect(page).toHaveURL(/\/login$/);
});
