# M4 final review — re-review of fix round 2 — Codex GPT-6 Astra

Read all changes in `48e4d35..3899af5`, both prior re-reviews, my first review, the controller’s instructions, the round-2 report, rulings F4-a/F4-b/F4-c, relevant revision-3.6 specification and plan sections, and the installed Playwright implementation. Ran in-memory probes of request redaction, Playwright instrumentation, scanner mutations, rendering and lint restrictions. All 28 rendering cases passed. The focused Vitest invocation for ProblemBox and DryRunPanel failed before collection because Vite’s temporary config was unwritable. I read the sentinel but **did not run it**: it needs no stack, but makes loopback HTTP requests, which I avoided under the no-network rule. Nothing in the tree, index or git state changed; no prohibited directories, actual keys or credentials were read, and no network, Docker, whole-suite or sub-agent operations were used.

### Verdict

**APPROVE** — all three findings are addressed within the rulings’ scope, with no new fix-range defect requiring changes; full sentinel execution was not independently repeated here.

### 1. Each finding

- **R-C1 — fixed.** `newApiContext` suppresses Playwright’s raw API step and records a replacement step whose failure has already been scrubbed. The redactor handles Cookie/Set-Cookie values, password fields, remembered passwords and session values in both message and stack (`frontend/e2e/support/api.ts:69`, `:81`; `frontend/e2e/support/redact.ts:10`, `:22`, `:31`). The extended sentinel exercises an authenticated transport failure and scans reporter output (`frontend/e2e/sentinel/artefacts.sentinel.ts:56`; `frontend/e2e/sentinel/check.mjs:186`). **Without the fix:** my in-memory probe using the installed Playwright channel/instrumentation code and actual scanner detected credentials in both the thrown error and recorded step. A rethrow-only mutation cleaned the exception but still leaked through the step. The current wrapper cleaned both. The fixer separately records full-sentinel red/red/green runs; I did not reproduce those browser runs.

- **R-M1 — fixed.** `ProblemDetail` renders the specified code-shaped detail as escaped `<code>` in either language; prose retains the existing translation/fallback policy. ProblemBox, quiet problems and the eval-gate offer all use it (`frontend/src/components/ProblemBox.tsx:13`, `:18`, `:37`; `frontend/src/settings/DryRunPanel.tsx:258`, `:395`). **Without the fix:** the new ProblemBox assertion fails because English previously rendered plain text, and German omitted the code; the quiet-problem assertion likewise fails in German (`frontend/src/components/ProblemBox.test.tsx:61`; `frontend/src/settings/DryRunPanel.test.tsx:97`). In-memory React/i18next rendering confirmed the expected behavior and failed the code-markup assertions when the code branch was removed.

- **R-M2 — fixed.** The RUNBOOK gives the four-file stack command, E2E invocation, retained local directories, credential warning and CI behavior (`docs/RUNBOOK.md:284`). These agree with `frontend/e2e/playwright.config.ts:28`, `:32`, `.github/workflows/e2e.yml:66` and `.gitignore:8`. **No regression test was added or is necessary for this documentation change**; removing it restores the missing warning.

### 2. R-C1 in depth

**Request coverage.** Grepping `frontend/e2e/` found only one raw `request.newContext` call, inside the wrapper (`frontend/e2e/support/api.ts:70`). All harness API request contexts use it: `apiContext`, `saveAuthState`, global readiness, E19’s saved-session check and the sentinel. Stack setup calls `saveAuthState` (`frontend/e2e/setup/stack.setup.ts:22`).

Browser-originated requests remain browser requests: UI login, navigation and the cookie test’s `page.evaluate(fetch(...))` do not pass through `newApiContext`. That is appropriate to their purpose. Their error paths do not append APIRequestContext’s credential-bearing header log: navigation logs URLs and lifecycle waits, while response waits log the awaited event or URL (`frontend/node_modules/playwright-core/lib/server/frames.js:504`; `frontend/node_modules/playwright-core/lib/client/page.js:336`). The login response wait additionally uses `scrubbed` (`frontend/e2e/support/fixtures.ts:100`).

**Credential destinations under the reviewed CI configuration:**

| Destination | Assessment |
|---|---|
| Error message and stack | Wrapped request failures are scrubbed before propagation. All seven request methods passed the invented-credential probe. |
| Playwright step | The internal call avoids recording the raw failure; the replacement step receives the scrubbed exception. Existing request titles contain no credentials. |
| Reporter stdout/stderr; list and HTML reports | No remaining credential path found for the reviewed calls. The sentinel captures both output streams and scans them alongside report files and embedded archives (`check.mjs:111`, `:161`, `:186`). |
| Attachments and page snapshots | CI traces remain disabled. Password filling avoids credential-bearing fill titles, and teardown empties password fields before snapshots (`fixtures.ts:16`, `:79`). No new credential attachment was introduced. |
| `e2e_setup.jsonl` | Still originates from the Python setup’s scrubbed output, independently of the new wrapper (`scripts/e2e_setup.py:187`; `frontend/e2e/setup/stack.setup.ts:19`). |

Local retained traces still contain credentials, as expressly allowed by the rulings and now documented. These conclusions are scoped to the actual harness and CI configuration, not arbitrary future logging or attachments.

**Sentinel effectiveness.** Bypassing the wrapper restores the cookie-bearing exception and API step that the scanner detects. Removing only the internal-call suppression also restores a detectable step leak. The sentinel checks both deliberate failure markers, a screenshot and canary coverage (`frontend/e2e/sentinel/check.mjs:181`, `:198`). My probes verified the scanner/instrumentation behavior; the full browser-level result remains the fixer’s reported evidence.

**Private API.** Acceptable for this pinned test harness. Playwright 1.56.1’s `_wrapApiCall` suppresses instrumentation callbacks for internal calls, which closes the otherwise unavoidable pre-catch recording path (`frontend/node_modules/playwright-core/lib/client/channelOwner.js:143`; `frontend/package.json:53`). A behavior change restoring raw steps is covered by the sentinel. Removing the method produced a loud `_wrapApiCall is not a function` failure in my probe.

**Lint.** The restrictions block named request imports, ordinary `page.request`/`context.request` access and the request fixture, while allowing `response.request()` and wrapped contexts. All 35 current harness TypeScript files outside the wrapper passed these restrictions. The rule is **not exhaustive**: destructuring `const { request: raw } = page`, computed access and a direct request import from `playwright-core` can bypass it (`frontend/eslint.config.js:27`, `:32`, `:35`). No such bypass exists in the reviewed harness; this is a limitation of the future guard, not an outstanding authenticated call site.

### 3. Regressions

- **E19:** retains the same base URL, saved authentication state, status assertion and disposal; only the context constructor changes (`frontend/e2e/specs/03-e19-session-expiry.spec.ts:80`).
- **Readiness:** retains its five-second request timeout, non-200/error retry behavior and disposal. Outside a test, the wrapper does not invoke `test.step`; that path passed the in-memory failure probe (`frontend/e2e/global-setup.ts:11`; `frontend/e2e/support/api.ts:50`, `:83`).
- **Request-error assertions:** no spec asserts the former error prefix or concrete error subclass. Existing restoration helpers compose already-scrubbed errors. HTTP response/status assertions remain unchanged.
- **English/German rendering:** the 28 rendering cases preserved prose policy, unknown-code fallback, escaping and request IDs, while adding verbatim code details. Quiet and alert paths share the component, as does the eval-gate offer. The focused Vitest files were inspected but could not execute in this read-only environment.

### 4. New findings

#### Critical

None.

#### Important

None.

#### Minor

None requiring changes at the first review’s calibration.

### Challenges to rulings

None.