// The `stack-setup` project (plan M4 Task 32): the deterministic E2E setup, after `first-use` and before the
// scenarios. It runs `uv run python scripts/e2e_setup.py` in the repository, shows its JSON lines (one per step:
// ids, states, counts and codes, never a secret), fails with them when it exits non-zero, and then signs the
// smoke user in through the API again and rewrites `AUTH_STATE`, so `scenarios` start on a fresh, valid session
// whatever `first-use` did. Its `test` is the fixtures' (the CSP guard, as for every spec; no page opens).
import process from "node:process";
import { saveAuthState } from "../support/api";
import { expect, test as setup } from "../support/fixtures";
import { REPO_ROOT } from "../support/paths";
import { run, smokeUser } from "../support/stack";

// The collection (600 s), the grounded publish (180 s), the score lane's settle wait (600 s) and the polls.
const SETUP_WITHIN_MS = 40 * 60_000;

setup("the deterministic E2E setup", async () => {
  setup.setTimeout(SETUP_WITHIN_MS);
  const ran = await run(["uv", "run", "python", "scripts/e2e_setup.py"], { cwd: REPO_ROOT, timeoutMs: SETUP_WITHIN_MS });
  process.stdout.write(ran.stdout);
  await setup.info().attach("e2e_setup.jsonl", { body: ran.stdout, contentType: "application/jsonl" });
  const stderr = ran.stderr.trim().split("\n").slice(-3).join("\n"); // a crash's last lines (no step printed)
  expect(ran.code, `scripts/e2e_setup.py exited ${String(ran.code)}:\n${ran.stdout}${stderr}`).toBe(0);
  await saveAuthState(await smokeUser());
});
