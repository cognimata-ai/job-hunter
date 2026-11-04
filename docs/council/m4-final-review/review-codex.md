# M4 final whole-branch review — Codex GPT-6 Astra

Reviewed `833312e..13eb853` module by module against revision 3.6, the plan, rulings and parked list. The prescribed `uv` invocation could not create its cache lock; direct execution with the installed Python environment passed 82 focused cases covering query normalization, cursor isolation and backend-derived translation codes. In-memory probes reproduced the CV confirmation race, custom-window cursor drift and Playwright credential retention. Both document fixtures matched their deterministic generators; translation keys matched across en/de. A read-only OpenAPI generation and comparison matched the committed types across 67 paths. Nothing was changed; the final working tree and index were clean. No prohibited data or credentials were read, and no network, Docker, Playwright suite or subagents were used.

### Verdict

**REQUEST CHANGES** — fix C1, I1 and I2; the recorded fresh-stack acceptance work also remains outstanding.

### Focus questions 1–9

**1. The jobs list engine and cursors.** Section transitions and NULL branches correctly restart the next section without carrying an incompatible key; group predicates require one member to satisfy the combined non-score filters. User values are bound, while SQL fragments come from closed mappings. The defect is **I2**: changing the display timezone changes a custom window under an accepted cursor. (`backend/src/jobhunter/jobs/listing.py:167`, `:273`, `:352`, `:375`; `backend/src/jobhunter/api/routes/jobs.py:276`.) The new indexes match the pinned walks, but the EXPLAIN tests disable sequential scans; they establish index usability, not the production latency gate. I did not execute them. (`backend/src/jobhunter/db/migrations/versions/0009_m4_ui.py:64`; `tests/integration/test_jobs_list_plans.py:91`, `:123`.)

**2. The ranking generation.** No additional writer defect found: publication, active deterministic recomputation and missing-similarity completion bump; admission avoids an unconditional bump every cycle, and reclaim distinguishes exhausted attempts. The page reads its generation and rows in one repeatable-read snapshot. (`backend/src/jobhunter/scoring/publish.py:432`; `backend/src/jobhunter/scoring/reconcilers.py:221`; `backend/src/jobhunter/worker/embedding_reconcile.py:72`; `backend/src/jobhunter/scoring/admission.py:678`; `backend/src/jobhunter/scoring/cycles.py:798`; `backend/src/jobhunter/db/snapshot.py:23`.) **I2** nevertheless produces undisclosed membership drift: a timezone-only settings save does not bump ranking. (`backend/src/jobhunter/settings/effects.py:74`; `backend/src/jobhunter/settings/save.py:507`.)

**3. Concurrency and lock order.** I constructed no additional database deadlock cycle or lost update in the reviewed paths. Merge-family retries use fresh transactions only after database-confirmed aborts. Actions lock jobs in order before updating action rows, preserve unrelated fields and set `updated_at`. Replay has its dedicated lock and transactional replay receipt; batch release checks eligibility under the owner lock, retains ambiguous spend and bumps last. Scope apply checks completeness before its atomic flip. (`backend/src/jobhunter/api/retry.py:34`; `backend/src/jobhunter/jobs/actions.py:69`, `:177`; `backend/src/jobhunter/worker/operations.py:119`; `backend/src/jobhunter/sources/replay.py:213`; `backend/src/jobhunter/scoring/batches.py:1326`; `backend/src/jobhunter/settings/scope_apply.py:394`.) **I1** is a separate browser save/confirmation race.

**4. API contract and security.** No additional defect found in session enforcement, unsafe-method Origin/header checks, keyed paid actions, TOTP prompting or background expiry behavior. (`backend/src/jobhunter/api/deps.py:128`; `backend/src/jobhunter/api/edge.py:104`; `backend/src/jobhunter/api/routes/targets.py:61`, `:259`; `backend/src/jobhunter/api/routes/auth.py:269`.) Gone-job checks cover synthetic, fenced and merged jobs; error handlers preserve RFC problem responses and `Allow`, while SPA fallback excludes `/api` and distinguishes immutable assets from uncached HTML. (`backend/src/jobhunter/jobs/detail.py:604`; `backend/src/jobhunter/api/errors.py:247`, `:292`; `backend/src/jobhunter/api/static.py:123`.) The drift script writes files, so I used an in-memory equivalent instead; generated types matched exactly. (`scripts/check-openapi-drift.sh:23`; `backend/src/jobhunter/api/openapi_export.py:18`.)

**5. The browser boundary.** No executable server-string path found. Evidence becomes escaped text and marks using code-point offsets; outbound URLs require absolute HTTP(S). The catalog’s markup is the fixed `<models>` component placeholder, whose model values render as text. (`frontend/src/jobs/EvidenceHighlighter.tsx:23`, `:43`; `frontend/src/components/OutboundLink.tsx:19`; `frontend/src/settings/ActivationDialog.tsx:190`, `:268`.) Modal styling uses classes. The build guard checks static output, and the E2E guard detects runtime violations on exercised pages; neither alone proves every runtime path. (`frontend/src/components/ModalLayer.tsx:31`; `frontend/scripts/check-dist-csp.mjs:26`; `frontend/e2e/support/csp.ts:15`.)

**6. Privacy.** **C1** exposes authentication material through failed E2E traces. The reviewed application log/error paths otherwise record identifiers, counts and error classes without the submitted content. (`frontend/e2e/playwright.config.ts:24`; `.github/workflows/e2e.yml:61`; `backend/src/jobhunter/jobs/actions.py:192`; `backend/src/jobhunter/api/routes/profile_review.py:610`; `backend/src/jobhunter/api/errors.py:311`.) The committed CV/PDF fixtures matched their invented generators byte for byte, including fixed document authorship; they are not evidence of real personal-data leakage. Setup diagnostics are deliberately restricted and scrubbed. (`scripts/e2e_fixtures.py:31`, `:46`, `:69`; `scripts/e2e_setup.py:187`, `:734`.)

**7. SPA state correctness.** **I1** lets confirmation activate an older persisted CV profile while newer edits remain visible. Settings have stronger reconciliation: keyed overlays, preservation of edits made during saving, stale-base handling and rejection of obsolete dry-run responses. No additional defect found there. (`frontend/src/settings/cv/ProfileReview.tsx:530`; `frontend/src/settings/useSettingsForm.ts:288`, `:417`, `:546`, `:583`.) Keyed mutations retain keys after transient failures and disable automatic retries; activation dialogs require a fresh preview on opening. (`frontend/src/api/idempotency.ts:61`; `frontend/src/settings/ActivationDialog.tsx:82`.)

**8. Accessibility and i18n.** No new defect found in the reviewed focus and translation paths. Modal focus is trapped and restored; list shortcuts avoid typing/dialog contexts; Label mode advances focus after labeling. Progress banners use status/alert roles. (`frontend/src/components/ModalLayer.tsx:38`; `frontend/src/jobs/useListKeyboard.ts:44`; `frontend/src/jobs/LabelMode.tsx:136`; `frontend/src/system/Banners.tsx:93`.) The backend-code comparison passed, and the catalog probe found matching en/de keys with no missing catalogued code translations. Unknown-code fallback remains explicit. (`tests/unit/test_i18n_codes.py:644`; `frontend/src/i18n/index.ts:70`.) Keyboard E2E assertions are substantive, but were not rerun here. (`frontend/e2e/specs/15-e20-keyboard.spec.ts:28`.)

**9. Plan drift and acceptance.** I found no additional unauthorized feature or silent omission beyond the findings. Privacy/E18 and production-network checks are explicitly staged to M5. (`docs/superpowers/plans/2026-11-02-m4-api-and-web-ui.md:1108`, `:1113`.) The M4 gate is **not yet demonstrated**: the smoke harness requires a fresh stack, and the controller explicitly leaves fresh smoke, ordered E2E, stack tests and performance testing to Part B. Docparse now has recorded evidence of 53 passes. (`scripts/smoke_m4.py:9`; `.superpowers/sdd/2026-11-02-m4-api-and-web-ui/progress.md:547`, `:550`.) See parked items 4–5.

### Findings

#### Critical

**C1 — Failed E2E traces retain and upload authentication secrets** — `frontend/e2e/playwright.config.ts:24`; `.github/workflows/e2e.yml:61`.

A login/cookie test signs in through the browser and subsequently fails. `retain-on-failure` keeps its trace, and CI uploads the report and trace directories unchanged. The installed recorder attaches request bodies and retains Cookie headers; an in-memory probe with invented sentinels confirmed both paths. Login fills also enter the traced action stream. (`frontend/e2e/support/fixtures.ts:53`; `frontend/node_modules/playwright-core/lib/server/trace/recorder/tracing.js:65`; `frontend/node_modules/playwright-core/lib/server/har/harTracer.js:244`, `:496`.)

These are test-stack credentials and sessions; I found no production-secret exposure. They nevertheless violate the no-secrets-in-traces requirement. (`docs/superpowers/specs/2026-10-16-job-hunter-design.md:5486`.)

**Fix:** retain the agreed diagnostic-upload workflow, but exclude authentication secrets from retained/uploaded traces, including action parameters, cookies, headers and login bodies. Add a sentinel-based artifact check that fails if any credential survives.

#### Important

**I1 — Confirm profile can discard edits made during its save** — `frontend/src/settings/cv/ProfileReview.tsx:530`, `:546`.

Edit the profile to A, click **Confirm profile**, then edit a still-enabled field to B while the PUT is pending. The save completion preserves B locally but unconditionally opens confirmation. The probe returned `persisted=A`, `visible=B`, `dirty=true`, `confirmationOpened=true`. Confirming posts only the acknowledgement, so the server activates A and the review disappears with B unsaved. (`frontend/src/settings/cv/ProfileReview.tsx:565`, `:738`; `frontend/src/settings/cv/api.ts:238`.)

**Fix:** either disable editing during save-and-confirm, or compare the current edit generation with the saved snapshot before opening confirmation. Require another save when they differ, and test a delayed save followed by further editing.

**I2 — A timezone change silently changes a custom date window during pagination** — `backend/src/jobhunter/api/routes/jobs.py:276`; `backend/src/jobhunter/jobs/query.py:194`.

Request November 1–4 in `Europe/Berlin`, then change the display timezone to UTC in another tab before loading more. The continuation preserves the cursor’s old lower bound but recomputes the upper bound: November 4 at 23:00 UTC becomes November 5 at 00:00 UTC. The hash remains accepted because it contains neither that upper bound nor the timezone. A newly eligible 23:30 posting ahead of the current key is skipped, without a ranking-change warning. The in-memory probe confirmed the changed bounds and unchanged hash. (`backend/src/jobhunter/settings/save.py:507`.)

**Fix:** freeze both resolved bounds in the authenticated cursor, or include the timezone/window interpretation in validation and reject the old cursor. Test continuation across a timezone-only settings change.

#### Minor

None newly raised.

### Parked items 1–5

1. **No action — activation dry-run errors still stop activation.** `activate()` calls `save_settings()` in the same transaction; that function checks errors before its `internal` warning branch. A refusal rolls back the preceding pointer flip. (`backend/src/jobhunter/settings/activate.py:266`; `backend/src/jobhunter/settings/save.py:426`.)

2. **No action — the release-time constant is sufficiently guarded.** The server supplies authoritative `releasable`, the UI uses its computed timestamp only for explanation, and a test pins the hour to the backend constant. (`backend/src/jobhunter/observability/attention.py:131`; `frontend/src/system/NeedsAttention.tsx:329`; `tests/unit/test_attention_release_after.py:21`.)

3. **Carry to M5 — structured node/model diagnostics would improve recovery guidance.** Model-only preview data loses diagnostic precision, but does not bypass verification: activation checks the actual pairs and refuses unverified ones. The UI retains a server-title fallback. (`backend/src/jobhunter/settings/activate.py:249`, `:353`; `frontend/src/settings/ActivationDialog.tsx:263`.)

4. **Fix now, before accepting M4 — obtain the fresh ordered run.** The project dependency chain is coherent, and one-shot scenarios disable retries; E15 cancels its preset change before E17 needs its starting configuration. I found no new deterministic ordering contradiction. (`frontend/e2e/playwright.config.ts:30`; `frontend/e2e/specs/06-e03-cv-confirm.spec.ts:48`; `frontend/e2e/specs/19-e17-needs-attention.spec.ts:35`; `frontend/e2e/specs/18-e15-preset.spec.ts:94`.) The known E6 waiting-cycle hazard remains relevant, without being re-raised as a new finding. Individual runs cannot establish the combined result, especially for final E3/E9/E10/E17. (`.superpowers/sdd/2026-11-02-m4-api-and-web-ui/progress.md:520`, `:535`.)

5. **Fix now, before accepting M4 — restore CI or obtain equivalent clean-environment evidence.** Chunked pytest does not validate the new workflow’s clean installation, browser installation and image-to-suite integration. Those are concrete remaining integration surfaces, not an identified YAML failure. (`.github/workflows/e2e.yml:44`, `:54`, `:58`; `Dockerfile:19`.) The split contract/payload environments and docparse image build also differ from the shared local environment; the ledger’s 53 docparse passes should be credited, while stack/performance evidence remains pending. (`.github/workflows/ci.yml:110`, `:125`, `:143`; `.superpowers/sdd/2026-11-02-m4-api-and-web-ui/progress.md:550`.)

### Challenges to rulings

None. C1 requires safe artifact contents while preserving the approved failure-upload decision.

### Strengths

- Clear SQL section boundaries and snapshot-based generation labeling make pagination auditable. (`backend/src/jobhunter/jobs/listing.py:352`; `backend/src/jobhunter/db/snapshot.py:23`.)
- Settings reconciliation explicitly preserves concurrent edits and rejects stale validation responses. (`frontend/src/settings/useSettingsForm.ts:417`, `:546`.)
- Generated API types matched exactly, and the translation checks derive their expectations from backend definitions. (`backend/src/jobhunter/api/openapi_export.py:18`; `tests/unit/test_i18n_codes.py:644`.)