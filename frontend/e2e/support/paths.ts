// The paths of the E2E harness (plan M4 Task 32). The frontend is an ES module package, so every path is derived
// from `import.meta.url`, never from `__dirname`.
import { fileURLToPath } from "node:url";

/** The repository root (no trailing slash): the scripts and the compose files are under it. */
export const REPO_ROOT = fileURLToPath(new URL("../../../", import.meta.url)).replace(/\/$/, "");
/** The saved session of the user `smoke` (`global-setup.ts`; `stack-setup` rewrites it), git-ignored. */
export const AUTH_STATE = fileURLToPath(new URL("../.auth/state.json", import.meta.url));
