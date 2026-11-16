// E1 (spec §18.7, §15.2, §16.3; plan M4 Task 32): a login lands on Jobs; a wrong password shows the one
// generic "Invalid credentials"; the sixth failure from the address is throttled (5 failures in 15 min lock it
// out). The spec ends with `jobhunter sessions unlock` (§16.3's escape hatch) so later logins work, also when it
// fails. It starts without the saved session and signs in itself (its own user agent). The throttle's attempts
// stay recorded after an unlock, so a retry of this spec would meet a lockout at its first failure: it runs once.
//
// The login budget (M5 Task 21, Ruling T21-3 I2): the api's edge refuses an 11th login within 60 s from one
// client address (`LOGIN_LIMIT_PER_IP` in backend/src/jobhunter/api/edge.py), and every client of the E2E stack
// reaches the api from devproxy's one address. The global setup, the session-cookie spec and this one send 9
// logins within seconds, and E19 sends 3 more right after, so this spec ends by waiting out the minute of its
// last login. A later spec that signs in must keep any 60 s to at most 10 logins in all.
import { expectNoSeriousA11yViolations } from "../support/a11y";
import { en, expect, signInThroughUi, test, textPattern } from "../support/fixtures";
import { compose, smokeUser } from "../support/stack";

const FAILURES_BEFORE_LOCKOUT = 5; // throttle.IP_MAX_FAILURES
const WRONG_PASSWORD = "not-the-smoke-password-e2e";
// LOGIN_LIMIT_PER_IP's window (api/edge.py: 10 logins in 60 s from one address), and a second to spare.
const LOGIN_WINDOW_MS = 61_000;
// When this spec's last login was answered (no earlier than the edge counted it), or `undefined` before its first.
let lastLoginAt: number | undefined;

/** The login of the page, as `signInThroughUi` sends it, with the time of its answer noted. */
async function signIn(...args: Parameters<typeof signInThroughUi>): ReturnType<typeof signInThroughUi> {
  try {
    return await signInThroughUi(...args);
  } finally {
    lastLoginAt = Date.now();
  }
}

test.use({ storageState: { cookies: [], origins: [] }, userAgent: "jobhunter-e2e-01" });
test.describe.configure({ retries: 0 });

test.afterAll(async () => {
  try {
    await compose(["run", "--rm", "-T", "api", "jobhunter", "sessions", "unlock"]);
  } finally {
    // Out of the edge's login window before E19 signs in (the budget above); nothing to wait for without a login.
    const wait = lastLoginAt === undefined ? 0 : lastLoginAt + LOGIN_WINDOW_MS - Date.now();
    if (wait > 0) {
      await new Promise((resolve) => setTimeout(resolve, wait));
    }
  }
});

test("E1: a login lands on Jobs, a wrong password is refused generically, the sixth failure is throttled", async ({
  page,
  context,
}) => {
  const user = await smokeUser();
  await page.goto("/login");
  await expectNoSeriousA11yViolations(page);
  expect((await signIn(page, user)).status()).toBe(204);
  await expect(page).toHaveURL("/");
  await expect(page.getByRole("main")).toBeVisible();
  await expectNoSeriousA11yViolations(page);

  await context.clearCookies();
  await page.goto("/login");
  const alert = page.getByRole("alert");
  for (let failure = 1; failure <= FAILURES_BEFORE_LOCKOUT; failure += 1) {
    expect((await signIn(page, user, WRONG_PASSWORD)).status(), `failure ${String(failure)}`).toBe(401);
    await expect(alert).toHaveText(en["login.invalid"]);
  }
  await expectNoSeriousA11yViolations(page);
  expect((await signIn(page, user, WRONG_PASSWORD)).status()).toBe(429);
  await expect(alert).toHaveText(textPattern(en["login.throttled"]));
  await expectNoSeriousA11yViolations(page);
  await expect(page).toHaveURL(/\/login$/);
});
