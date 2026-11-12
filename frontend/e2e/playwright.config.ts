// The E2E suite's configuration (spec §18.7; plan M4 Task 32). One worker, three projects run in order through
// `dependencies`, each matched by a RegExp against the file's absolute path (a string `testMatch` is a glob,
// and a glob has no `(a|b)`):
// - `first-use`: the empty stack, before any setup (the session cookie, E1, E2, E19);
// - `stack-setup`: the deterministic setup (`scripts/e2e_setup.py`). A `globalSetup` runs before every spec, so
//   the setup that must follow E2's empty state is a project of its own;
// - `scenarios`: the scenario specs on the set-up stack (Tasks 33 and 34), and E18 (plan M5 Task 30), whose
//   delete-everything leaves nothing for a later spec: one worker runs the files in name order, so `20-` is last.
// `globalSetup` waits for the stack, creates the smoke user and saves its session (`AUTH_STATE`). The report
// goes to frontend/playwright-report and the test output (traces, screenshots) to frontend/test-results, both
// git-ignored; a reporter's relative `outputFolder` resolves against this file's directory.
// No credential in an artefact (spec §16 L5486, Ruling F4-a): e2e.yml uploads both directories after a failure,
// and a trace holds the requests' cookies, the login bodies and every typed value, so under CI no trace is
// recorded (a failure keeps its screenshot and page snapshot); a local run keeps a failed test's trace, which
// holds the stack's credentials and never leaves the machine. `npm run e2e:sentinel` checks what CI keeps.
import { defineConfig } from "@playwright/test";
import process from "node:process";
import { AUTH_STATE } from "./support/paths";

export default defineConfig({
  testDir: ".",
  workers: 1,
  fullyParallel: false,
  retries: process.env.CI ? 1 : 0,
  timeout: 180_000,
  expect: { timeout: 15_000 },
  use: {
    baseURL: "http://localhost:8080",
    trace: process.env.CI ? "off" : "retain-on-failure",
    screenshot: "only-on-failure",
    locale: "en-GB",
  },
  reporter: [["list"], ["html", { open: "never", outputFolder: "../playwright-report" }]],
  globalSetup: "./global-setup.ts",
  projects: [
    {
      name: "first-use",
      testMatch: /[\\/]specs[\\/]0[0-3]-[^\\/]+\.spec\.ts$/,
      use: { storageState: AUTH_STATE },
    },
    {
      name: "stack-setup",
      testMatch: /[\\/]setup[\\/]stack\.setup\.ts$/,
      dependencies: ["first-use"],
    },
    {
      name: "scenarios",
      testMatch: /[\\/]specs[\\/](0[4-9]|1[0-9]|20)-[^\\/]+\.spec\.ts$/,
      dependencies: ["stack-setup"],
      use: { storageState: AUTH_STATE },
    },
  ],
});
