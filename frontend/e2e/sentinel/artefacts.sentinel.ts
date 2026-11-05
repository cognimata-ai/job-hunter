// The sentinel spec of `check.mjs` (`npm run e2e:sentinel`; spec §16 L5486, Rulings F4-a, F4-c): no credential
// may survive in a retained or uploaded E2E artefact or in the reporters' output. Its first test signs in as the
// E2E harness does, through the login form (`signInThroughUi`) and through the API (`newApiContext`, as
// `saveAuthState`), with the invented password and on the server that `check.mjs` serves; its context starts
// from a saved session (as the `scenarios` project does) and receives one at the login, and both cookies travel
// with its requests. Then it types the password again and fails on purpose. Its second test signs in, then sends
// a request of the harness's API context (`apiContext`) to a closed port: the transport failure fails it, with
// Playwright's call log, the cookie header in it, in the error. The run keeps every failure artefact the CI
// configuration keeps, which `check.mjs` scans, with the reporters' output, for the sentinels. It belongs to no
// project of `playwright.config.ts`: only `sentinel.config.ts` runs it.
import process from "node:process";
import { apiContext, newApiContext } from "../support/api";
import { expect, fillPassword, signInThroughUi, test } from "../support/fixtures";
import type { SmokeUser } from "../support/stack";

function required(name: string): string {
  const value = process.env[name];
  if (value === undefined || value === "") {
    throw new Error(`${name} is not set: the sentinel spec runs through \`npm run e2e:sentinel\``);
  }
  return value;
}

const ORIGIN = required("JH_SENTINEL_ORIGIN");
const CLOSED_ORIGIN = required("JH_SENTINEL_CLOSED_ORIGIN"); // nothing listens there
const CANARY = required("JH_SENTINEL_CANARY");
const USER: SmokeUser = { username: "sentinel-user", password: required("JH_SENTINEL_PASSWORD"), create: [], exists: "" };

test("the sentinel signs in, sends its sessions and fails on purpose", async ({ page }, testInfo) => {
  await page.goto(`${ORIGIN}/login`);
  expect((await signInThroughUi(page, USER)).status()).toBe(204);
  expect(await page.evaluate(async () => (await fetch("/api/auth/session")).status)).toBe(200);

  const api = await newApiContext({ baseURL: ORIGIN });
  try {
    const login = await api.post("/api/auth/login", {
      data: { username: USER.username, password: USER.password },
      headers: { Origin: ORIGIN },
    });
    expect(login.status()).toBe(204);
    expect((await api.get("/api/auth/session")).status()).toBe(200);
  } finally {
    await api.dispose();
  }

  // The scanner's positive control: a harmless value in a step title and an attachment, which it must find.
  await test.step(`canary ${CANARY}`, async () => {
    await testInfo.attach("canary.txt", { body: CANARY, contentType: "text/plain" });
  });

  await page.goto(`${ORIGIN}/login`);
  await fillPassword(page, USER.password); // left in its field: the failure's screenshot and snapshots see it
  expect(false, "the sentinel spec fails on purpose").toBe(true);
});

test("an authenticated request that cannot connect fails on purpose", async ({ page }) => {
  await page.goto(`${ORIGIN}/login`);
  expect((await signInThroughUi(page, USER)).status()).toBe(204);
  // the harness's API context carries both sessions (a cookie of 127.0.0.1 is sent to every port), and its
  // transport failure is thrown with Playwright's call log, the request's headers in it
  const api = await apiContext(page);
  try {
    await api.post(`${CLOSED_ORIGIN}/api/auth/login`, { data: { username: USER.username, password: USER.password } });
  } finally {
    await api.dispose();
  }
});
