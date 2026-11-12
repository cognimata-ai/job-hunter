// The E2E harness's request rules (plan M5 Task 30, M4 Rulings F4-c and F4-d R2-M2): `eslint.config.js` applies
// `scripts/e2e-request-rules.mjs` to e2e/ (support/api.ts aside), so every request context of the harness is
// `newApiContext`'s, whose failures are redacted. Each form is linted from a string here, with no type
// information, so no file of violations exists for `eslint .` or the e2e typecheck to find.
import { Linter } from "eslint";
import tseslint from "typescript-eslint";
import { describe, expect, it } from "vitest";
import { E2E_REQUEST_RULES, REQUEST_MESSAGE } from "../../scripts/e2e-request-rules.mjs";

const linter = new Linter();
const CONFIG: Linter.Config[] = [
  {
    files: ["**/*.ts"],
    languageOptions: { parser: tseslint.parser, sourceType: "module" },
    rules: E2E_REQUEST_RULES,
  },
];

/** The rule and message of each problem `code` has as an E2E spec; a parse error fails the test instead. */
function problems(code: string): { rule: string | null; message: string }[] {
  const found = linter.verify(code, CONFIG, { filename: "e2e/specs/99-example.spec.ts" });
  expect(found.filter((message) => message.fatal === true), "parse errors").toEqual([]);
  return found.map((message) => ({ rule: message.ruleId, message: message.message }));
}

describe("the E2E request rules", () => {
  it.each([
    ["the request of @playwright/test", 'import { request } from "@playwright/test";', "no-restricted-imports"],
    ["the request of playwright", 'import { request } from "playwright";', "no-restricted-imports"],
    ["the request of playwright-core", 'import { request as api } from "playwright-core";', "no-restricted-imports"],
    ["page.request", "export const api = page.request;", "no-restricted-syntax"],
    ["context.request", "await context.request.get('/api/settings');", "no-restricted-syntax"],
    ['page["request"]', 'export const api = page["request"];', "no-restricted-syntax"],
    ["a destructured page", "const { request } = page;", "no-restricted-syntax"],
    ["a destructured and renamed page", "const { request: api } = page;", "no-restricted-syntax"],
    ["the request fixture", 'test("x", async ({ page, request }) => { await page.goto("/"); });', "no-restricted-syntax"],
  ])("flags each request form: %s", (_name, code, rule) => {
    const found = problems(`declare const page: any, context: any, test: any;\n${code}`);
    expect(found).toHaveLength(1);
    expect(found[0]?.rule).toBe(rule);
    expect(found[0]?.message).toContain(REQUEST_MESSAGE);
  });

  it("leaves response.request() alone", () => {
    expect(
      problems(
        "declare const response: any, page: any;\n" +
          'export const method = response.request().method();\n' +
          'export const same = response["request"]().method();\n' +
          "export const waited = page.waitForResponse((answer: any) => answer.request().method() === 'POST');\n" +
          'import { expect, test } from "@playwright/test";\n' +
          "export const used = [expect, test];",
      ),
    ).toEqual([]);
  });
});
