// The files each Playwright project must discover (plan M4 Task 32): `check-list.mjs` (`npm run e2e:check`)
// compares them with what `playwright test --list` finds, so a `testMatch` that matches nothing fails loudly.
// Tasks 33 and 34 add their scenario files to `scenarios`. Plain data only: `check-list.mjs` imports this file
// with Node's own type stripping.
export const EXPECTED_SPECS: Record<"first-use" | "stack-setup" | "scenarios", readonly string[]> = {
  "first-use": [
    "00-session-cookie.spec.ts",
    "01-e01-login.spec.ts",
    "02-e02-first-use.spec.ts",
    "03-e19-session-expiry.spec.ts",
  ],
  "stack-setup": ["stack.setup.ts"],
  scenarios: [
    "04-e05-run-now.spec.ts",
    "05-e16-watchlist.spec.ts",
    "06-e03-cv-confirm.spec.ts",
    "07-e04-low-text-pdf.spec.ts",
    "08-e06-scored-cards.spec.ts",
  ],
};
