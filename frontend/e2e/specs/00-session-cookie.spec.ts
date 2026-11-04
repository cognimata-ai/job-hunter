// The session cookie (M0 Deviation 13; plan M4 Task 32): after a login through the UI on http://localhost:8080,
// the cookie `__Host-jh_session` is Secure, HttpOnly, SameSite=Strict, Path=/ and has no Domain; the page's own
// `GET /api/auth/session` answers 200; Logout removes it. The spec starts without the saved session and signs in
// itself (its own user agent), so its Logout ends only its own session. A cookie's value is never asserted on,
// so it never reaches a message.
import type { Page } from "@playwright/test";
import { expectNoSeriousA11yViolations } from "../support/a11y";
import { en, expect, signInThroughUi, test } from "../support/fixtures";
import { smokeUser } from "../support/stack";

const COOKIE = "__Host-jh_session";

test.use({ storageState: { cookies: [], origins: [] }, userAgent: "jobhunter-e2e-00" });

/** The attributes of the `Set-Cookie` lines for `name`, without the value: lower-cased, sorted. */
function cookieAttributes(setCookie: string, name: string): string[][] {
  return setCookie
    .split("\n")
    .filter((line) => line.startsWith(`${name}=`))
    .map((line) =>
      line
        .split(";")
        .slice(1)
        .map((attribute) => attribute.trim().toLowerCase())
        .sort(),
    );
}

async function sessionStatus(page: Page): Promise<number> {
  return page.evaluate(async () => (await fetch("/api/auth/session")).status);
}

test("the session cookie is a host-only Secure HttpOnly SameSite=Strict cookie, and Logout removes it", async ({
  page,
  context,
}) => {
  const user = await smokeUser();
  await page.goto("/login");
  await expectNoSeriousA11yViolations(page);
  const login = await signInThroughUi(page, user);
  expect(login.status()).toBe(204);
  await expect(page).toHaveURL("/");

  const [attributes] = cookieAttributes((await login.headerValue("set-cookie")) ?? "", COOKIE);
  expect(attributes).toEqual(expect.arrayContaining(["httponly", "path=/", "samesite=strict", "secure"]));
  expect(attributes.filter((attribute) => attribute.startsWith("domain"))).toEqual([]);
  const stored = (await context.cookies()).filter((cookie) => cookie.name === COOKIE);
  expect(
    stored.map(({ domain, path, secure, httpOnly, sameSite }) => ({ domain, path, secure, httpOnly, sameSite })),
  ).toEqual([{ domain: "localhost", path: "/", secure: true, httpOnly: true, sameSite: "Strict" }]);
  expect(await sessionStatus(page)).toBe(200);
  await expectNoSeriousA11yViolations(page);

  await page.getByRole("button", { name: en["header.logout"] }).click();
  await expect(page).toHaveURL(/\/login(\?|$)/);
  expect((await context.cookies()).map((cookie) => cookie.name)).not.toContain(COOKIE);
  expect(await sessionStatus(page)).toBe(401);
  await expectNoSeriousA11yViolations(page);
});
