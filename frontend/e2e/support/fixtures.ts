// What every spec imports (plan M4 Task 32): `test` with the CSP guard on every test (`csp.ts`) and the password
// fields emptied before a failure's page snapshot, `expect`, the UI's English strings (`en`, from the app's own
// catalogue: a spec never copies a string), the login form as the user fills it, and the E2E's CV files
// (`scripts/e2e_fixtures.py` writes them).
import { expect, type Page, type Response } from "@playwright/test";
import { fileURLToPath } from "node:url";
import en from "../../src/i18n/en.json" with { type: "json" };
import { test as guarded } from "./csp";
import type { SmokeUser } from "./stack";

/** `csp.ts`'s `test` with one more automatic fixture (spec §16 L5486, Ruling F4-a): when a test's context closes,
 * Playwright writes a failed test's page snapshot (error-context.md, which e2e.yml uploads) with every field's
 * value, so the fixture's teardown, which runs before the context closes, first empties every password field of
 * its pages. A page that is gone or busy is passed over. */
const test = guarded.extend<{ passwordFieldsEmptied: undefined }>({
  passwordFieldsEmptied: [
    async ({ context }, use) => {
      await use(undefined);
      for (const page of context.pages()) {
        await page
          .locator('input[type="password"]')
          .evaluateAll((fields) => {
            for (const field of fields) {
              (field as HTMLInputElement).value = "";
            }
          })
          .catch(() => undefined);
      }
    },
    { auto: true },
  ],
});

export { en, expect, test };

/** E3's DOCX: `SYNTHETIC_CV` with one planted phone line. */
export const CV_DOCX = fileURLToPath(new URL("../fixtures/cv.docx", import.meta.url));
/** E4's PDF: fewer characters than the low-text threshold. */
export const LOW_TEXT_PDF = fileURLToPath(new URL("../fixtures/low-text.pdf", import.meta.url));
/** The phone number of `cv.docx` that the contact stripper misses (`scripts/e2e_fixtures.PLANTED_PHONE`). */
export const PLANTED_PHONE = "+49 30 123";

/** `text` with every RegExp metacharacter escaped, to match it as written. */
export function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** `template` (an `en` string) as a pattern that matches it with each `{{name}}` replaced by digits. */
export function textPattern(template: string): RegExp {
  return new RegExp(`^${escapeRegExp(template).replace(/\\\{\\\{\w+\\\}\\\}/g, "\\d+")}$`);
}

/** `template` (an `en` string) with each `{{name}}` replaced by `values[name]`: the text the UI shows. */
export function filled(template: string, values: Readonly<Record<string, string>>): string {
  return template.replace(/\{\{(\w+)\}\}/g, (_, name: string) => values[name] ?? "");
}

/** `template` (an `en` string) as an anchored pattern: each `{{name}}` is `values[name]`, a string matched as
 * written or a RegExp inserted as it is (a capture group, say), and any text for a name `values` does not give. */
export function templateMatch(template: string, values: Readonly<Partial<Record<string, string | RegExp>>> = {}): RegExp {
  const body = template
    .split(/(\{\{\w+\}\})/)
    .map((part) => {
      const name = /^\{\{(\w+)\}\}$/.exec(part)?.[1];
      if (name === undefined) {
        return escapeRegExp(part);
      }
      const value = values[name];
      return value === undefined ? ".+?" : value instanceof RegExp ? value.source : escapeRegExp(value);
    })
    .join("");
  return new RegExp(`^${body}$`);
}

/** The login form's password field of the page filled with `password`, as `fill` fills it (the value set, an
 * `input` event sent) but through the page (spec §16 L5486, Ruling F4-a): Playwright names a fill's value in its
 * step title (`Fill "<value>"`), which the HTML report keeps, and an evaluation's argument in no title. */
export async function fillPassword(page: Page, password: string): Promise<void> {
  const field = page.getByLabel(en["login.password"], { exact: true });
  await expect(field).toBeEditable();
  await field.evaluate((input, value) => {
    const element = input as HTMLInputElement;
    // the prototype's setter: React's value tracker then sees a change, as it does for typing
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(element, value);
    element.dispatchEvent(new Event("input", { bubbles: true }));
  }, password);
}

/** The login form of the page filled with `user`'s name and `password` (the user's own by default) and sent,
 * as a user signs in; the login request's answer. */
export async function signInThroughUi(page: Page, user: SmokeUser, password = user.password): Promise<Response> {
  await page.getByLabel(en["login.username"], { exact: true }).fill(user.username);
  await fillPassword(page, password);
  const answered = page.waitForResponse(
    (response) => new URL(response.url()).pathname === "/api/auth/login" && response.request().method() === "POST",
  );
  await page.getByRole("button", { name: en["login.submit"] }).click();
  return answered;
}
