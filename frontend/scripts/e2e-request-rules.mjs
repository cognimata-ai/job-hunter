// The E2E harness's request rules (plan M4 Task 32, Ruling F4-c; plan M5 Task 30, M4 Ruling F4-d R2-M2). Every
// request context of the harness is support/api.ts's `newApiContext`, whose failures are rethrown with the
// session cookie and the password redacted, so e2e/ (support/api.ts aside) may not reach Playwright's own:
// - no `request` imported from @playwright/test, playwright or playwright-core;
// - no `page.request`, `context.request` or `page["request"]` (an object), never `response.request()` (a method
//   called);
// - no `request` destructured, from a page or a context (`const { request } = page`) or as the `request` fixture
//   of a test's parameter.
// eslint.config.js applies them to e2e/**, and src/test/e2eLintRule.test.ts lints each form from a string, so no
// file of violations exists for `eslint .` or the e2e typecheck to find. Plain data: no Node API.

export const REQUEST_MESSAGE =
  "Make a request context with support/api.ts's newApiContext: its failures are redacted (Ruling F4-c).";

/** `request` of each Playwright package, as `no-restricted-imports` paths (the specs' rule adds its own). */
export const REQUEST_IMPORTS = ["@playwright/test", "playwright", "playwright-core"].map((name) => ({
  name,
  importNames: ["request"],
  message: REQUEST_MESSAGE,
}));

/** @type {import("eslint").Linter.RulesRecord} */
export const E2E_REQUEST_RULES = {
  "no-restricted-imports": ["error", { paths: REQUEST_IMPORTS }],
  "no-restricted-syntax": [
    "error",
    {
      // `page.request`, `context.request`, `page["request"]`: the member read, but not called as a method
      selector:
        "MemberExpression:matches([property.name='request'], [property.value='request'])" +
        ":not(CallExpression > MemberExpression.callee)",
      message: REQUEST_MESSAGE,
    },
    {
      // `const { request } = page`, `{ request: api }`, and the `request` fixture of a test's parameter
      selector: "ObjectPattern > Property:matches([key.name='request'], [key.value='request'])",
      message: REQUEST_MESSAGE,
    },
  ],
};
