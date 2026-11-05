// E19 (spec §18.7, §15.8; plan M4 Task 32): a session that expires while a page is open sends the user to the
// login with the URL kept, and signing in returns to it: the Jobs page with its filters in the search, and the
// settings with their section in the hash. The spec signs in through the UI with its own user agent and expires
// only its own session, by that agent's hash (the server stores `sha256(user_agent)`), so the saved session of
// `AUTH_STATE` still answers afterwards.
import type { Page } from "@playwright/test";
import { expectNoSeriousA11yViolations } from "../support/a11y";
import { newApiContext, ORIGIN } from "../support/api";
import { en, expect, signInThroughUi, test } from "../support/fixtures";
import { AUTH_STATE } from "../support/paths";
import { psql, smokeUser } from "../support/stack";

const USER_AGENT = "jobhunter-e2e-03";
const EXPIRE_OWN_SESSION =
  "UPDATE sessions SET idle_expires_at = now() - interval '1 second' " +
  `WHERE user_agent_sha256 = sha256(convert_to('${USER_AGENT}', 'UTF8'))`;

test.use({ storageState: { cookies: [], origins: [] }, userAgent: USER_AGENT });

/** The search of a URL as sorted `[name, value]` pairs. */
function searchOf(url: URL): string[][] {
  return [...url.searchParams.entries()].sort(([a], [b]) => a.localeCompare(b));
}

/** The `redirect` the login page holds, resolved on the origin. */
function redirectOf(page: Page): URL {
  const redirect = new URL(page.url()).searchParams.get("redirect");
  expect(redirect, "the login page's redirect").not.toBeNull();
  return new URL(redirect ?? "", ORIGIN);
}

async function expireOwnSession(): Promise<void> {
  expect(await psql(EXPIRE_OWN_SESSION)).toMatch(/^UPDATE [1-9]\d*$/);
}

test("E19: an expired session redirects to the login and back to the same URL", async ({ page }) => {
  const user = await smokeUser();
  await page.goto("/login");
  expect((await signInThroughUi(page, user)).status()).toBe(204);
  await expect(page).toHaveURL("/");

  // The Jobs page with filters in its search; the session expires; a filter change is answered 401.
  await page.goto("/?posted=7d&work_mode=remote");
  const posted = page.getByRole("group", { name: en["filters.posted"] });
  await expect(posted.getByRole("radio", { name: en["filters.posted.7d"] })).toBeChecked();
  await expectNoSeriousA11yViolations(page);
  await expireOwnSession();
  const refused = page.waitForResponse(
    (response) => new URL(response.url()).pathname.startsWith("/api/") && response.status() === 401,
  );
  await posted.getByRole("radio", { name: en["filters.posted.14d"] }).check();
  await refused;
  await expect(page).toHaveURL(/\/login\?redirect=/);
  const kept = redirectOf(page);
  expect(kept.pathname).toBe("/");
  expect(searchOf(kept)).toEqual([
    ["posted", "14d"],
    ["work_mode", "remote"],
  ]);
  await expectNoSeriousA11yViolations(page);
  expect((await signInThroughUi(page, user)).status()).toBe(204);
  await expect(page).toHaveURL((url) => url.pathname === "/" && url.search !== "");
  expect(searchOf(new URL(page.url()))).toEqual(searchOf(kept));
  await expect(posted.getByRole("radio", { name: en["filters.posted.14d"] })).toBeChecked();

  // The settings with a section in the hash; the session expires; the reload's session read is answered 401.
  await page.goto("/settings#schedule");
  await expect(page.getByRole("region", { name: en["settings.section.schedule"] })).toBeVisible();
  await expectNoSeriousA11yViolations(page);
  await expireOwnSession();
  await page.reload();
  await expect(page).toHaveURL(/\/login\?redirect=/);
  const again = redirectOf(page);
  expect([again.pathname, again.search, again.hash]).toEqual(["/settings", "", "#schedule"]);
  expect((await signInThroughUi(page, user)).status()).toBe(204);
  await expect(page).toHaveURL((url) => url.pathname === "/settings" && url.hash === "#schedule");
  await expectNoSeriousA11yViolations(page);

  // The saved session was never presented, so it still answers.
  const saved = await newApiContext({ baseURL: ORIGIN, storageState: AUTH_STATE });
  try {
    expect((await saved.get("/api/auth/session")).status()).toBe(200);
  } finally {
    await saved.dispose();
  }
});
