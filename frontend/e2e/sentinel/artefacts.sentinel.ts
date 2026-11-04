// The sentinel spec of `check.mjs` (`npm run e2e:sentinel`; spec §16 L5486, Ruling F4-a): no credential may
// survive in a retained or uploaded E2E artefact. It signs in as the E2E harness does, through the login form
// (`signInThroughUi`) and through the API (as `saveAuthState`), with the invented password and on the server that
// `check.mjs` serves; its context starts from a saved session (as the `scenarios` project does) and receives one
// at the login, and both cookies travel with its requests. Then it types the password again and fails on purpose,
// so the run keeps every failure artefact the CI configuration keeps, which `check.mjs` scans for the sentinels.
// It belongs to no project of `playwright.config.ts`: only `sentinel.config.ts` runs it.
import { request } from "@playwright/test";
import process from "node:process";
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
const CANARY = required("JH_SENTINEL_CANARY");
const USER: SmokeUser = { username: "sentinel-user", password: required("JH_SENTINEL_PASSWORD"), create: [], exists: "" };

test("the sentinel signs in, sends its sessions and fails on purpose", async ({ page }, testInfo) => {
  await page.goto(`${ORIGIN}/login`);
  expect((await signInThroughUi(page, USER)).status()).toBe(204);
  expect(await page.evaluate(async () => (await fetch("/api/auth/session")).status)).toBe(200);

  const api = await request.newContext({ baseURL: ORIGIN });
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
