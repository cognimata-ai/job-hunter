// What every spec imports (plan M4 Task 32): `test` with the CSP guard on every test (`csp.ts`), `expect`, the
// UI's English strings (`en`, from the app's own catalogue: a spec never copies a string), the login form as the
// user fills it, and the E2E's CV files (`scripts/e2e_fixtures.py` writes them).
import { expect, type Page, type Response } from "@playwright/test";
import { fileURLToPath } from "node:url";
import en from "../../src/i18n/en.json" with { type: "json" };
import { test } from "./csp";
import type { SmokeUser } from "./stack";

export { en, expect, test };

/** E3's DOCX: `SYNTHETIC_CV` with one planted phone line. */
export const CV_DOCX = fileURLToPath(new URL("../fixtures/cv.docx", import.meta.url));
/** E4's PDF: fewer characters than the low-text threshold. */
export const LOW_TEXT_PDF = fileURLToPath(new URL("../fixtures/low-text.pdf", import.meta.url));
/** The phone number of `cv.docx` that the contact stripper misses (`scripts/e2e_fixtures.PLANTED_PHONE`). */
export const PLANTED_PHONE = "+49 30 123";

/** `template` (an `en` string) as a pattern that matches it with each `{{name}}` replaced by digits. */
export function textPattern(template: string): RegExp {
  const escaped = template.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`^${escaped.replace(/\\\{\\\{\w+\\\}\\\}/g, "\\d+")}$`);
}

/** The login form of the page filled with `user`'s name and `password` (the user's own by default) and sent,
 * as a user signs in; the login request's answer. */
export async function signInThroughUi(page: Page, user: SmokeUser, password = user.password): Promise<Response> {
  await page.getByLabel(en["login.username"], { exact: true }).fill(user.username);
  await page.getByLabel(en["login.password"], { exact: true }).fill(password);
  const answered = page.waitForResponse(
    (response) => new URL(response.url()).pathname === "/api/auth/login" && response.request().method() === "POST",
  );
  await page.getByRole("button", { name: en["login.submit"] }).click();
  return answered;
}
