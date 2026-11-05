import js from "@eslint/js";
import { defineConfig } from "eslint/config";
import tseslint from "typescript-eslint";

const REQUEST_MESSAGE =
  "Make a request context with support/api.ts's newApiContext: its failures are redacted (Ruling F4-c).";
const REQUEST_IMPORT = { name: "@playwright/test", importNames: ["request"], message: REQUEST_MESSAGE };

export default defineConfig(
  { ignores: ["dist", "src/api/schema.d.ts", "test-results", "playwright-report"] },
  js.configs.recommended,
  {
    // src/ and e2e/: the project service finds each file's tsconfig.json (e2e/ has its own).
    files: ["**/*.{ts,tsx}"],
    extends: [tseslint.configs.strictTypeChecked],
    languageOptions: {
      parserOptions: { projectService: true, tsconfigRootDir: import.meta.dirname },
    },
  },
  {
    // Every request context of the E2E harness is support/api.ts's `newApiContext`, whose failures are rethrown
    // with the session cookie and the password redacted (Ruling F4-c): no `request` of @playwright/test, no
    // `page.request` or `context.request`, and no `request` fixture anywhere else.
    files: ["e2e/**/*.ts"],
    ignores: ["e2e/support/api.ts"],
    rules: {
      "no-restricted-imports": ["error", { paths: [REQUEST_IMPORT] }],
      "no-restricted-syntax": [
        "error",
        // `page.request` or `context.request` (an object), never `response.request()` (a method called)
        {
          selector: "MemberExpression[property.name='request']:not(CallExpression > MemberExpression.callee)",
          message: REQUEST_MESSAGE,
        },
        { selector: ":function > ObjectPattern > Property[key.name='request']", message: REQUEST_MESSAGE },
      ],
    },
  },
  {
    // The E2E specs and the setup project take `test` from support/fixtures, which carries the CSP guard (R1);
    // `test` of @playwright/test has none (plan M4 Task 32). Its types and `expect` may come from either.
    files: ["e2e/specs/**/*.ts", "e2e/setup/**/*.ts"],
    rules: {
      "no-restricted-imports": [
        "error",
        {
          paths: [
            {
              name: "@playwright/test",
              importNames: ["test"],
              message: "Import `test` from support/fixtures: it carries the CSP guard (R1).",
            },
            REQUEST_IMPORT,
          ],
        },
      ],
    },
  },
  {
    // The build's Node scripts (plain ESM): the Node globals they use, without @types/node.
    files: ["scripts/**/*.mjs"],
    languageOptions: { globals: { process: "readonly", console: "readonly", URL: "readonly" } },
  },
);
