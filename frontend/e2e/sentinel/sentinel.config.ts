// The sentinel run's configuration (`check.mjs`, `npm run e2e:sentinel`): the E2E configuration as it is, so its
// trace, screenshot, retry and reporter settings are the ones under test, with one project that runs the sentinel
// spec from the session `check.mjs` saved, and no global setup (the sentinel needs no stack). `check.mjs` runs it
// with `CI` set, the test output in its own directory (`--output`) and the HTML report beside it
// (`PLAYWRIGHT_HTML_OUTPUT_DIR`), never in frontend/test-results or frontend/playwright-report.
import { defineConfig } from "@playwright/test";
import process from "node:process";
import base from "../playwright.config";

export default defineConfig({
  ...base,
  testDir: ".",
  globalSetup: undefined,
  projects: [
    {
      name: "sentinel",
      testMatch: /[\\/]sentinel[\\/]artefacts\.sentinel\.ts$/,
      use: { storageState: process.env.JH_SENTINEL_STATE },
    },
  ],
});
