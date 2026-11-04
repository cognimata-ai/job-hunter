// `npm --prefix frontend run e2e:check` (plan M4 Task 32): the E2E configuration loads, and every Playwright
// project discovers exactly the files `support/specs.ts` names, so a `testMatch` that matches nothing fails
// loudly. It runs `playwright test -c e2e/playwright.config.ts --list --reporter=json` (no browser, no stack, no
// global setup), collects each (project, file) pair of the report's suites, and exits 1 naming every missing
// and every unexpected file per project. `support/specs.ts` is plain data, imported with Node's own type
// stripping (Node 22.18 and later; CI and the dev container run 22.22).
import { spawnSync } from "node:child_process";
import { basename } from "node:path";
import process from "node:process";
import { URL, fileURLToPath } from "node:url";

const FRONTEND = fileURLToPath(new URL("..", import.meta.url));
const CLI = fileURLToPath(new URL("../node_modules/@playwright/test/cli.js", import.meta.url));
const { EXPECTED_SPECS } = await import("./support/specs.ts");

const listed = spawnSync(
  process.execPath,
  [CLI, "test", "-c", "e2e/playwright.config.ts", "--list", "--reporter=json"],
  { cwd: FRONTEND, encoding: "utf8", maxBuffer: 64 * 1024 * 1024 },
);
if (listed.error !== undefined || listed.status !== 0) {
  process.stderr.write(listed.stderr || String(listed.error));
  process.stderr.write(`e2e:check: playwright --list exited ${String(listed.status)}\n`);
  process.exit(1);
}

/** Every (project, file) pair of the suites, as `project\u0000file`. */
function pairs(suites, found = new Set()) {
  for (const suite of suites ?? []) {
    for (const spec of suite.specs ?? []) {
      for (const test of spec.tests ?? []) {
        found.add(`${test.projectName}\u0000${basename(spec.file)}`);
      }
    }
    pairs(suite.suites, found);
  }
  return found;
}

const report = JSON.parse(listed.stdout);
const found = pairs(report.suites);
const projects = new Set([...Object.keys(EXPECTED_SPECS), ...[...found].map((pair) => pair.split("\u0000")[0])]);
const problems = [];
for (const project of [...projects].sort()) {
  const expected = new Set(EXPECTED_SPECS[project] ?? []);
  const discovered = new Set([...found].filter((pair) => pair.startsWith(`${project}\u0000`)).map((pair) => pair.split("\u0000")[1]));
  for (const file of [...expected].filter((name) => !discovered.has(name)).sort()) {
    problems.push(`${project}: missing ${file}`);
  }
  for (const file of [...discovered].filter((name) => !expected.has(name)).sort()) {
    problems.push(`${project}: unexpected ${file}`);
  }
  process.stdout.write(`${project}: ${String(discovered.size)} file(s) ${[...discovered].sort().join(", ")}\n`);
}
if (problems.length > 0) {
  process.stderr.write(`${problems.join("\n")}\n`);
  process.exit(1);
}
process.stdout.write("e2e:check: every project discovers exactly its specs\n");
