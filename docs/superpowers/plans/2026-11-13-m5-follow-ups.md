# M5 Follow-ups Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close what M5's acceptance and final review left open:
- the operator details of the privacy statement, served from a file on the server (the user's decision, option A);
- a base-image refresh;
- the review's non-blocking findings;
- the known operational limits;
- three UI gaps.

**Architecture:** Each task is a small, self-contained change to M5's code. None of them changes M5's data model, except F4's optional retention and rotation commands.

**Tech Stack:** As M5: Python 3.13, FastAPI, SQLAlchemy Core, PostgreSQL 17, procrastinate, React/Vite, Docker Compose, restic.

**Spec:** `docs/superpowers/specs/2026-10-16-job-hunter-design.md`, revision 3.7. Each task names the spec text it changes; the spec stays binding.

## Global Constraints

The M5 plan's Global Constraints (`docs/superpowers/plans/2026-11-09-m5-operations-privacy-production.md`, L32–57) apply unchanged. In addition:

- **No secret in the repository.** No real name, e-mail, address, host or IP goes into the repository.
- **Personal operator details** live only on the server and are never logged.
- **Pins.** Every new image or action pin is a digest or SHA. Choose the newest release at least 14 days old.
- **No paid calls.** Run with the real-key variables unset: `ADZUNA_APP_ID`, `ADZUNA_APP_KEY`, `JOBHUNTER_ANTHROPIC_API_KEY`, `LANGSMITH_API_KEY`, `RAPIDAPI_KEY`, `SERPAPI_API_KEY`.

## Review Focus

1. **The operator file is missing, malformed or oversized in production.**
   - Expected: the api refuses to start, naming the file and the problem, never its content.
   - In dev and test the statement shows the placeholders.
2. **A refreshed base image breaks the runtime.** A dropped package or a changed Python patch level must be caught by the stack tests, the smoke test and the E2E suite, not in production.
3. **A hardened path changes behaviour it should keep.**
   - The export hash repair must still repair a genuine hash.
   - The restore test's report must still name the step that failed.
4. **A new operational command runs while the worker runs.** The manual retention run and the two new secret rotations take the same locks as their scheduled or scripted counterparts, and never print a secret.

---

### Task F1: The operator file (option A)

**Files**
- Create:
  - `backend/src/jobhunter/privacy/operator.py`
  - `tests/unit/test_operator_file.py`
  - `tests/integration/test_api_operator.py`
- Modify:
  - `backend/src/jobhunter/config.py`: the path setting
  - the startup checks (`startup-check --role api`)
  - the api's privacy routes
  - `deploy/compose.yaml`: the api's read-only bind mount
  - `deploy/compose.test.yaml`, with `deploy/test/operator.json`: fictional values
  - `frontend/src/system/PrivacyStatement.tsx` and its test
  - `docs/PRIVACY.md`: the Operator section's text
  - `docs/RUNBOOK.md`: the deploy checklist row and the first-start step
  - spec §20.6 and §19.2
  - `tests/unit/test_privacy_md.py`

**Produces**

- **The file.** `/etc/jobhunter/operator.json` on the host, mode 0644, owner root. It is mounted read-only into the api only, at `/run/config/operator.json`. The setting `JOBHUNTER_OPERATOR_FILE` defaults to that path.
- **Its content.** A JSON object with exactly these five keys: `operator`, `contact`, `controller`, `vps_provider` and `s3_provider`. Each is a non-empty string of at most 200 characters, with no control characters. Any other key, or any other type, is refused.
- **Loading.** `privacy.operator.load(path) -> OperatorDetails | None` reads the file once, at api start. A changed file applies after `restart api`.
- **Production.** The startup check `operator_file` refuses to start the api when the file is missing, unreadable, not valid JSON, or fails validation. Its error names the path and the reason, never a value. This is fail-closed, Ruling FU-1.
- **Dev and test.** The file is optional. Without it, the app shows the placeholders.
- **The route.** `GET /api/privacy/operator` requires a session. It answers 200 with `{operator, contact, controller, vps_provider, s3_provider}`, or 200 with all five as null when the file is absent outside production. It is cacheable for the session.
- **The UI.** The SPA's Operator section replaces each `[set at deployment: …]` marker with the matching value, rendered as text only, with no HTML.
- **Unchanged.** `docs/PRIVACY.md` keeps its markers, so the existing marker test stays green. Its Operator line says that the values come from the deployment's operator file.
- **No leaks.** No log line, metric, export or LangSmith trace carries a value.

**Steps**

- [ ] **Step 1: Write the failing tests.**
  - The loader: valid, missing, malformed, extra key, overlong value, control character.
  - The startup check refuses in production and passes in test.
  - The route: authenticated, values or nulls.
  - The UI substitution, in vitest.
  - The compose mount: api only, read-only.
  - No value in any log line. Use a caplog probe.
- [ ] **Step 2: Implement.**
- [ ] **Step 3: Run the gates.** Then run `tests/stack/test_prod_stack.py` and the smoke test on PT, with the test operator file mounted.
- [ ] **Step 4: Commit:** `feat(m5): the privacy statement's operator details come from a file on the server`.

### Task F2: Base-image refresh

**Files**
- `deploy/images.lock`: the `python` and `restic` digests, and others only if the scan needs them.
- `Dockerfile`, `docparse/Dockerfile`, `embedder/Dockerfile`: pip removed from the embedder's runtime and `fake` stages, as T29-3 did for the others.
- `.python-version` and CI, only if the Python patch changes.
- `docs/spikes/` or the report: scan results.

**Produces**

- **New pins.**
  - Re-resolve the newest digest of `python:3.13.<patch>-slim-bookworm` that is at least 14 days old, where it clears the fixable HIGH and CRITICAL findings in openssl and apt.
  - Re-resolve the same for `restic/restic`, for the Go stdlib HIGHs, at the newest release at least 14 days old.
- **Patch bumps.** If a Python patch bump is needed, `.python-version`, the uv-managed interpreter and CI follow it.
- **The scan.** Trivy at `--scanners vuln --severity HIGH,CRITICAL --ignore-unfixed` over app, backup, docparse and the fake embedder. Report every remaining fixable HIGH or CRITICAL finding with its reason. The CI gate stays CRITICAL-only.

**Steps**

- [ ] Resolve and pin the new digests.
- [ ] Remove pip from the embedder stages.
- [ ] Rebuild, scan, then run the stack tests, the smoke test and the E2E suite.
- [ ] Commit: `build(m5): refreshed base images and no pip in the embedder images`.

### Task F3: Hardening batch

Sources: `docs/council/m5-final-review/rereview4-*.md` and the earlier reviews.

**Produces**

- **(a) M4-1:** `ShipLockLost`, and a psycopg error on the lock session, print `database` from the CLI. The RUNBOOK says what to do.
- **(b) M4-3:** the shipper's unchanged-stream return confirms the lock before it exits 0.
- **(c) M4-4:** the `LOCK_ORDER` note covers the restore test holding 7261006 while its migrate takes 7261003 on the scratch database.
- **(d) M4-5:** the spec's forgery sentence covers receipt insertion as well as removal. It states what a forged receipt could hide, and why the restore's union still holds.
- **(e) R4-M1, R4-M2:** the static bounds guard follows content into variadic parameters, or refuses them, and accepts keyword calls. Add planted cases.
- **(f) T28-7:** the export hash repair first authenticates the file under the data key, and refuses with a named error when authentication fails.
- **(g) T26-5 (2):** a cleanup error in `run_restore_test` is logged and reported beside the failed step's code. It never replaces that code.
- **(h) T19-4:** a retention rule deletes `login_lockouts` rows whose lock expired longer ago than the login-attempt window. Update spec §20.1 and PRIVACY.md if it states the period.
- **(i) T22:** `scripts/host-setup.sh` refuses with a clear message when `swapon` is missing.

**Steps**

- [ ] Write failing tests per item.
- [ ] Fix.
- [ ] Run the gates.
- [ ] Commit, one commit per item or group.

### Task F4: Operational commands

**Produces**

- **(a) Manual retention.** `jobhunter retention run`, in the worker role, runs `retention_daily` once, under the same lock and transaction rules as the scheduled job. It prints counts only.
  - Spec §19.8 and RUNBOOK §11 say how and when to use it.
- **(b) Release tag.** `release.yml`'s `pr` job tags the digest PR's head commit (`release-<YYYYMMDD>-<short sha>`) and pushes the tag. It needs no new permission beyond `contents: write`.
  - The RUNBOOK's step 0 checks out that tag.
  - Test it in `test_workflows_m5.py` and with actionlint.
- **(c) Secret rotation for `restic_password` and `pg_bootstrap`.**
  - For `restic_password`: `restic key add` with the new password, verify it, `restic key remove` the old key, install the secret, then `restart backup`. Use a script beside `rotate-db-role.sh` that follows its argument and `--dry-run` conventions.
  - For `pg_bootstrap`: extend `rotate-db-role.sh` (or a sibling script) to `ALTER ROLE` the bootstrap superuser, connecting with the old password and setting the new one, through psql variables only.
  - RUNBOOK section 7 and the recovery-kit note: a rotated `restic_password` replaces the kit's copy.
  - **Tests:** dry-run unit tests that no value reaches a command line, plus a stack case on PT: rotate both, then a backup set and a restore still work.

**Steps**

- [ ] Write failing tests.
- [ ] Implement.
- [ ] Run the gates and the stack case.
- [ ] Commit, one commit per item.

### Task F5: UI gaps

**Produces**

- **(a) Exports after a reload.**
  - Add `GET /api/privacy/exports?latest=1`, or a short list.
  - The Privacy & data export card shows a ready, unexpired export and its download link after a reload, and a running export's status.
- **(b) Deletions list.** It gains Load more, keyset-paged like the other lists, beyond the newest 20.
- **(c) Queries & sources.** No horizontal scroll at 761 and 961 px wide.
- **Visual check.** `playwright-cli` at 1280×800, 390×844 and 200 %, in English and German, for the changed views. Screenshots are not committed.

**Steps**

- [ ] Write failing tests: vitest, plus an integration test for the route.
- [ ] Implement.
- [ ] Run the visual check and the frontend gates.
- [ ] Commit.

### Task F6: Acceptance and final review

**Gates.** Every gate of M5's Task 33:
- the full suite, in chunks;
- the stack modules and the drills;
- the smoke test;
- the full E2E suite;
- gitleaks.

**Final review.** The milestone 4-eyes review of the follow-up range, Codex GPT-6 Astra at high effort and Claude Fable, repeated until both approve. Record it in `docs/council/m5-follow-ups-review/`.

**Pull request.** Open a pull request for M5 and these follow-ups to `main`, as for M0–M4.

**Roadmap.** The M4 and M5 rows stay "in progress" until CI runs green.

## Rulings made with this plan

- **FU-1.** A missing or invalid operator file stops the api in production. This is fail-closed, so a deployment cannot go live with a blank statement. The user approved option A on 2026-11-13; the blocking behaviour is the controller's choice, and the user may switch it to a warning.
  - Cost if wrong: one changed startup check.
- **FU-2.** `jobhunter-app:prev` stays tagged until the user decides otherwise.
- **FU-3.** The pull request opens after F6's final review. The user's "proceed with the remaining open items" covers it.
