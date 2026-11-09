# M4 final review — re-review of fix round 1 — Codex GPT-6 Astra

Read the brief, both first reviews, the fixer’s instructions and report, Task 36’s review, rulings F4-a/F4-b, the relevant specification and plan, and per-file diffs for all four commits in `13eb853..d9c9bda`. Ran six focused Python unit modules: **212 passed**, using the installed environment with bytecode/cache writes disabled and in-memory capture after `uv` and default pytest capture encountered read-only filesystem restrictions. Four focused Vitest modules could not collect because their temporary directory was unwritable. In-memory probes checked old/new cursors, moved test helpers, credential logging snippets and profile repair. No integration tests, Playwright runs, network calls, Docker operations or sub-agents were used. I changed nothing and read no prohibited directories or actual credentials.

### Verdict

**REQUEST CHANGES** — item 1 still permits session credentials in CI failure reports; the other reported fixes are addressed, subject to the confirmation limitation distinguished below.

### 1. Each finding

1. **Codex C1 / Fable M5 → item 1: not fully fixed.** CI traces are disabled, password fills avoid credential-bearing step titles, and teardown empties password fields before page snapshots (`frontend/e2e/playwright.config.ts:28`; `frontend/e2e/support/fixtures.ts:15`, `:78`). The sentinel would detect the original retained traces and password-bearing snapshots/report steps without these changes. However, it exercises successful authenticated requests followed by an assertion failure, leaving authenticated **transport failures** uncovered (`frontend/e2e/sentinel/artefacts.sentinel.ts:31`, `:49`). Those still leak cookies through error call logs; see **R-C1**.

2. **Codex I1 → item 2: fixed for the reported edit-during-save race.** Confirmation opens only after the next render compares the current form with the submitted save snapshot (`frontend/src/settings/cv/ProfileReview.tsx:506`, `:553`, `:563`). The delayed-save test preserves the later edit, requires the modal to remain closed, then verifies a second Confirm saves that edit and opens the modal (`frontend/src/settings/cv/ProfileReview.test.tsx:264`). Without the fix, its first modal-absence assertion fails. This does not establish equality with server-repaired content; see §2.

3. **Codex I2 / Fable M8 → item 3: fixed.** Both bounds enter the cursor and hash and are restored on continuation (`backend/src/jobhunter/api/pagination.py:299`, `:333`; `backend/src/jobhunter/jobs/query.py:205`; `backend/src/jobhunter/api/routes/jobs.py:291`). The timezone-change integration test excludes the posting admitted only by UTC’s upper bound and verifies fresh-page behavior (`tests/integration/test_api_jobs_list.py:219`). Without the fix, that continuation includes the extra posting. The upper-bound hash test also detects alteration or removal (`tests/unit/test_pagination.py:510`).

4. **Fable M1 → item 4: fixed.** Preparation only moves `pending → in_batch`; rejection, abandonment and release use the guarded `in_batch → pending` helper (`backend/src/jobhunter/scoring/batches.py:688`, `:941`). The four generation-equality tests fail with their former unconditional bumps (`tests/integration/test_batch_bumps.py:82`, `:91`, `:105`, `:117`). The exhaustion test preserves the existing crossing-bucket bump and already passes before this fix (`:135`).

5. **Fable M2 → item 5: fixed under F4-b.** `useServerMessage` suppresses English supplemental text for translated codes in German, preserves unknown-code fallback, and leaves English behavior intact (`frontend/src/i18n/index.ts:89`). Its callers cover ProblemBox, quiet problems, dry-run issues, eval-gate details, profile issues and the activation fallback (`frontend/src/components/ProblemBox.tsx:21`; `frontend/src/settings/DryRunPanel.tsx:256`, `:276`, `:383`; `frontend/src/settings/cv/ProfileReview.tsx:151`; `frontend/src/settings/ActivationDialog.tsx:273`). The new German known-code assertions fail without suppression; unknown-code and English assertions protect behavior that already worked.

6. **Fable M6 → item 6: fixed.** `split_member` takes `active_config FOR SHARE` before locking jobs or the group (`backend/src/jobhunter/dedup/groups.py:886`). Its test blocks a job row, starts the split, and requires a configuration writer to queue behind that split (`tests/integration/test_group_targets.py:616`). Without the added lock, the configuration writer completes and the assertion fails.

7. **Task 36 Minors 1–6 → item 7: fixed.**
   - **M1:** the conservative any-draft predicate is present (`scripts/smoke_m4.py:136`). The SQL predicate test fails without it (`tests/unit/test_smoke_m4_script.py:762`); the additional stub test exercises existing candidate-skipping behavior.
   - **M2:** reordered-page IDs are compared only under equal generations (`scripts/smoke_m4.py:309`). The changed-generation case fails before the fix (`tests/unit/test_smoke_m4_script.py:626`).
   - **M3:** shared row/poll helpers and contextual data-error handling replace the copies (`scripts/smoke_m4.py:422`, `:569`, `:645`; `scripts/e2e_setup.py:733`). Both malformed-answer cases previously escape as exceptions (`tests/unit/test_smoke_m4_script.py:1048`).
   - **M4:** shared helpers moved to `tests/support/smoke_stack.py:63`. AST comparison confirmed `Database`, `Stack`, `template` and `detail_body` are unchanged. No behavioral red test is expected for this extraction.
   - **M5:** missing asset/cursor, overlapping pages and missing save-bump cases now have assertions (`tests/unit/test_smoke_m4_script.py:492`, `:589`, `:861`). These pin existing behavior and therefore pass before the round; the incorrect docstring is corrected.
   - **M6:** both RUNBOOK statements now match the implementation (`docs/RUNBOOK.md:47`, `:75`); verified by reading, not a regression test.

### 2. Regressions

- **Item 1 — artefacts:** normal CI failures no longer retain traces. Password screenshots remain masked, teardown clears snapshot values, saved authentication state is outside uploaded directories, and `e2e_setup.jsonl` still uses scrubbed output (`scripts/e2e_setup.py:187`). Nevertheless, list and HTML reports retain credential-bearing API error messages: **R-C1**. The scanner’s plain/base64 and recursive ZIP checks do fail on matching credential bytes, with useful canary controls (`frontend/e2e/sentinel/check.mjs:143`, `:192`, `:199`). It does not scan captured stdout/stderr for credentials, and scanning PNG bytes does not inspect visible pixels. Its successful-request scenario therefore cannot prove the broader claim. Local retained traces are consistent with F4-a’s expressly permitted local-only option and CI-artefact scope.

- **Item 2 — confirmation:** unchanged drafts still open confirmation immediately; successfully saved edits open it after reconciliation; edits during saving remain unsaved and require another Confirm. No new failure in those paths was found. **Equality with stored content is not absolute:** `saveNow` records the submitted form as its base, while the backend stores a repaired profile (`frontend/src/settings/cv/ProfileReview.tsx:553`; `backend/src/jobhunter/api/routes/profile_review.py:605`). For example, a year-only role start `"2018"` is stored as `null`; the dirty-form reconciliation can preserve the submitted value locally before confirmation opens. A pure validator probe confirmed that repair. This behavior also exists at `13eb853`, so it is a pre-existing limitation, not a new fix-range finding.

- **Item 3 — pagination:** every sort, scored/unscored transition and NULL branch uses the same restored window filter (`backend/src/jobhunter/jobs/listing.py:167`, `:352`). Twelve old-cursor cases returned **400 `invalid_cursor`**, and twelve corresponding new cursors round-tripped. The Jobs hook resets the query to page 1 and suppresses the Load more error box (`frontend/src/jobs/useJobsList.ts:87`, `:117`); the existing page test explicitly checks that path (`frontend/src/pages/JobsPage.test.tsx:55`). The revised §14.3 accurately describes both frozen bounds and their hash coverage (`docs/superpowers/specs/2026-10-16-job-hunter-design.md:5103`).

- **Item 4 — ranking:** none of the four moves crosses a bucket; cancelled targets remain untouched. Follow-on scheduling does not itself change list membership or order. The exhaustion sweep still bumps last (`backend/src/jobhunter/scoring/batches.py:1263`). The revised lock test retains both contention directions, blocker identity and final-state assertions, and additionally checks that `ranking_state` is free (`tests/integration/test_lock_schedule.py:770`). The lost-batch test retains its accounting, hold, alert and follow-on assertions; only the obsolete bump expectation changes (`tests/integration/test_lost_batch_release.py:334`).

- **Item 5 — messages:** grepping `detail`, `title` and `message` rendering found no omitted supplemental-message path. Existing upload, watchlist, schema-mismatch and toast paths already translate with fallback. Unknown codes retain English fallback; separately rendered request IDs, field paths and diagnostic codes remain visible. Loss of node/model specifics embedded in the activation title is F4-b’s recorded accepted cost.

- **Item 6 — locks:** the split now takes configuration, ordered jobs, group/domain rows, targets, then ranking. It takes no company advisory lock and does not upgrade its configuration lock. Activation and final scope application acquire configuration exclusively first; scope recomputation takes configuration before ordered job locks (`backend/src/jobhunter/settings/scope_apply.py:364`, `:398`). I found no new lock cycle against either operation.

- **Item 7 — shared stubs:** the extraction preserves the existing assertions in the M3 smoke and E2E setup tests. The M4 test changes only one existing assertion’s expected wording, from “resumes a batch draft” to “resumes a draft”; it adds the requested cases without weakening existing predicates. All **87** tests across those three modules passed.

### 3. New findings

#### Critical

**R-C1 — Authenticated API failures still leak session cookies into CI reports — `frontend/e2e/playwright.config.ts:28`, `:32`; `frontend/e2e/sentinel/artefacts.sentinel.ts:31`.**

**Scenario:** after signing in, an E2E helper makes an authenticated `api.get()` and the connection resets or times out. The installed Playwright implementation logs every request header, including its automatically supplied cookie (`frontend/node_modules/playwright-core/lib/server/fetch.js:177`, `:426`). On failure, those logs are appended to the thrown error (`frontend/node_modules/playwright-core/lib/client/connection.js:146`). The list reporter prints that error, and the HTML reporter stores it (`frontend/node_modules/playwright/lib/reporters/base.js:316`; `frontend/node_modules/playwright/lib/reporters/html.js:470`). CI uploads the report unchanged (`.github/workflows/e2e.yml:66`).

An in-memory execution of the exact header-logging and call-log-formatting snippets retained an invented session value. No browser or network was involved. Disabling traces does not affect this path, and the sentinel’s requests all succeed before its deliberate failure. This is a remaining instance of original C1, not a separate expansion of scope.

**Fix:** sanitize authenticated API failure diagnostics before reporters or artefacts capture them, including global-setup failures. Extend the sentinel with an authenticated transport failure and check captured reporter output as well as retained files.

#### Important

None newly introduced in the fix range.

#### Minor

None newly raised.

### Challenges to rulings

None. R-C1 violates F4-a’s required outcome; it does not challenge the permitted choice to retain traces locally.