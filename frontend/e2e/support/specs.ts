// The files each Playwright project must discover (plan M4 Task 32): `check-list.mjs` (`npm run e2e:check`)
// compares them with what `playwright test --list` finds, so a `testMatch` that matches nothing fails loudly.
// Tasks 33 and 34 add their scenario files to `scenarios` (five and eleven), and M5 Task 30 E18's, the last. Plain
// data only: `check-list.mjs` imports this file with Node's own type stripping.
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
    "09-e07-filters.spec.ts",
    "10-e08-pagination.spec.ts",
    "11-e09-drawer.spec.ts",
    "12-e10-group.spec.ts",
    "13-e11-score-now-cap.spec.ts",
    "14-e12-labelling.spec.ts",
    "15-e20-keyboard.spec.ts",
    "16-e13-radius-apply.spec.ts",
    "17-e14-warnings.spec.ts",
    "18-e15-preset.spec.ts",
    "19-e17-needs-attention.spec.ts",
    "20-e18-privacy.spec.ts",
  ],
};
