### Verdict

**REQUEST CHANGES.** Reviewed `012a451..8812840` as `codex`. Two Important findings block approval: an interrupted restic secret installation can defeat the documented recovery procedure, and the prebuilt-stack helper can rebuild a protected image through another service sharing its tag. There is also one Minor overstatement in the restic success message. The blocking paths are at [rotate-restic-password.sh:238](/home/user/job-hunter/scripts/rotate-restic-password.sh:238) and [prebuilt.py:92](/home/user/job-hunter/tests/support/prebuilt.py:92).

### Focus questions 1–6

**1. Operator file, end to end**

No production-value leak path found. The loader bounds reads, suppresses parser exception context, reports validation rules rather than values, and excludes values from `repr`. The route returns the five fields under session authentication. The committed deployment fixture is explicitly fictional. See `backend/src/jobhunter/privacy/operator.py:67`, `:93`, `:132`; `backend/src/jobhunter/api/routes/privacy.py:132`, `:721`; `deploy/test/operator.json:1`.

Production startup is fail-closed through the startup assertions and the API’s subsequent load. User/session commands use the API role’s assertions too. Host setup checks file presence before changes; Compose mounts it read-only without creating a missing source. See `backend/src/jobhunter/config.py:734`, `:831`; `backend/src/jobhunter/api/app.py:181`, `:199`; `backend/src/jobhunter/cli_user.py:111`; `backend/src/jobhunter/cli_sessions.py:51`; `scripts/host-setup.sh:172`; `deploy/compose.yaml:125`.

SPA substitution happens after document parsing and produces React text nodes, including markup-looking values. Loading and failure states substitute explanatory notes. The operator-file instructions agree across the spec, RUNBOOK and privacy statement. See `frontend/src/system/privacyDoc.ts:207`; `frontend/src/system/PrivacyStatement.tsx:31`, `:87`; `docs/RUNBOOK.md:92`; `docs/PRIVACY.md:171`; spec `docs/superpowers/specs/2026-10-16-job-hunter-design.md:6324`, `:6778`.

**2. Rotations**

The restic sequence is add → verify → install → restart backup → remove. Before installation, reruns reuse an existing new key; after successful installation, the instructions require restarting backup before manual removal. Failed removal retains a recoverable state and names the old key. **Installation itself has the uncovered failure path in I1.** See `scripts/rotate-restic-password.sh:93`, `:220`, `:237`, `:248`; `docs/RUNBOOK.md:602`.

The bootstrap rotation probes the new password first, so an interruption after `ALTER ROLE` can resume without authenticating with the retired password. Its RUNBOOK also gives manual installation recovery. See `scripts/rotate-db-role.sh:75`, `:175`; `docs/RUNBOOK.md:573`.

Passwords are passed through stdin/files and psql variables, rather than expanded into host command arguments. Restic passwords stay in files; PostgreSQL and S3 authentication values enter container-local environments. Scratch files use restrictive permissions on `/dev/shm`; PostgreSQL disables statement/error-statement logging. See `scripts/rotate-restic-password.sh:63`, `:76`, `:104`; `scripts/rotate-db-role.sh:73`; `deploy/postgres/postgresql.conf:20`.

The rotation drill’s call sites use the four approved rotation/install shapes with PT and `.secrets/test`; the two added shapes are covered by M5-U11. See `tests/stack/test_prod_drills.py:372`, `:513`, `:693`, `:720`; `.superpowers/sdd/2026-11-13-m5-follow-ups/progress.md:36`, `:39`.

**3. Hardening and interactions**

The export hash repair authenticates the exact bytes it hashes, including export identity and segment ordering/finality, before updating the row. The RUNBOOK’s recovery advice keeps the previous key mounted until retention removes the damaged export. See `backend/src/jobhunter/crypto_rotate.py:259`, `:363`; `backend/src/jobhunter/privacy/export.py:284`; `docs/RUNBOOK.md:526`.

Restore cleanup records database and directory-removal failures. A cleanup failure makes otherwise successful steps fail; an earlier step failure retains its code with `cleanup_error` alongside it. Failed restore runs feed the alert query. Hard-kill leftovers remain an explicitly documented recovery case. See `backend/src/jobhunter/backup/restore_test.py:246`, `:285`, `:330`; `backend/src/jobhunter/observability/backup_health.py:102`; `docs/RUNBOOK.md:656`.

Lockout retention deletes rows by their expiry plus the shared 30-day window and resolves `auth.lockout` when it deletes the global row, in the same fenced transaction. The unchanged-stream shipper return now confirms its lock; CLI failures distinguish `ship_lock_lost` from `database`. See `backend/src/jobhunter/privacy/retention.py:252`, `:390`, `:562`; `backend/src/jobhunter/backup/shipper.py:286`; `backend/src/jobhunter/cli_backup.py:183`.

**4. Base images**

The Python reference retains its version tag and a complete SHA-256 digest; the recorded resolution evidence identifies its date and remaining vulnerabilities. I did not independently query registries or rescan images. See `deploy/images.lock:2`; `.superpowers/sdd/2026-11-13-m5-follow-ups/task-F2-report.md:23`, `:118`.

Static inspection confirms pip removal in the Python roots of every shipped stage, including both embedder stages. The browser fallback is conditional in the E2E configuration, and the visual configuration uses the agreed symlink. See `Dockerfile:111`; `docparse/Dockerfile:59`; `embedder/Dockerfile:60`, `:76`; `frontend/e2e/playwright.config.ts:23`, `:37`; `frontend/e2e/visual/cli.config.json:4`.

FU-7 is a temporary predeployment deferral, not vulnerability clearance: it requires the Python/uv refresh on or after November 19 before deployment and separately carries the restic findings pending upstream changes. See `.superpowers/sdd/2026-11-13-m5-follow-ups/progress.md:19`.

**5. UI**

The exports list is session-authenticated, capped at five, newest first, and exposes only IDs, states and timestamps. Its running/unexpired-ready filter matches the updated spec; download-by-ID retains the separately documented retention window. See `backend/src/jobhunter/api/routes/privacy.py:132`, `:159`, `:227`, `:481`; spec `docs/superpowers/specs/2026-10-16-job-hunter-design.md:5155`, `:6692`.

The export card follows a locally started export or discovers one from the list. It uses the listed running state before the detail response arrives, and successful starts invalidate the list independently of the component’s callback. See `frontend/src/system/PrivacyActions.tsx:72`; `frontend/src/system/privacyApi.ts:137`.

Load more forwards the server’s keyset cursor; polling considers all loaded pages. The shared table scroll boxes are positioned, and the container query applies the card layout at the intended widths. E14 checks page/table overflow and requires the four expected tables, preventing a vacuous pass. See `frontend/src/system/systemApi.ts:101`; `frontend/src/system/privacyApi.ts:74`; `frontend/src/settings/tables.module.css:10`, `:137`; `frontend/e2e/specs/17-e14-warnings.spec.ts:38`, `:96`.

**6. Drift**

The changed contracts agree apart from the rotation recovery and wording findings below:

| Spec area | Corresponding implementation/documentation |
|---|---|
| §14.2 routes | `backend/src/jobhunter/api/routes/privacy.py:481`, `:721` |
| §16.8 startup | `backend/src/jobhunter/config.py:734`, `:801` |
| §19.2 operator file | `deploy/compose.yaml:125`; `docs/RUNBOOK.md:166` |
| §19.5 restore cleanup | `backend/src/jobhunter/backup/restore_test.py:339`; `docs/RUNBOOK.md:660` |
| §19.8 operational commands | `backend/src/jobhunter/cli_retention.py:65`; `docs/RUNBOOK.md:561`, `:580`, `:961` |
| §20.1 retention | `backend/src/jobhunter/privacy/retention.py:252`, `:562`; `docs/PRIVACY.md:80`; `frontend/src/i18n/en.json:1756`; `frontend/src/i18n/de.json:1756` |
| §20.3 exports | `backend/src/jobhunter/api/routes/privacy.py:159`, `:455`; `frontend/src/system/PrivacyActions.tsx:75` |
| §20.6 statement | `frontend/src/system/privacyDoc.ts:207`; `frontend/src/system/PrivacyStatement.tsx:31` |

The release workflow tags the digest commit it just created and atomically pushes its branch and tag, matching the RUNBOOK’s checkout procedure. See `.github/workflows/release.yml:159`; `docs/RUNBOOK.md:865`.

F6 closes the missing-embedder build path, but its broader promise that a prebuilt image is never rebuilt still fails for shared image tags: I2 below (`tests/support/prebuilt.py:92`; `deploy/compose.dev.yaml:39`).

### Findings

#### Critical

None.

#### Important

**I1. An interrupted restic secret installation can make “run it again” unrecoverable.**

**Where:** [rotate-restic-password.sh:238](/home/user/job-hunter/scripts/rotate-restic-password.sh:238), [rotate-restic-password.sh:93](/home/user/job-hunter/scripts/rotate-restic-password.sh:93), [RUNBOOK.md:602](/home/user/job-hunter/docs/RUNBOOK.md:602).

**Failure path:** After adding and verifying the new key, the script copies directly over the installed secret using `install`. This is not an atomic staged replacement. An interruption or write failure after replacing the destination can leave it missing, empty or incomplete. The failure message nevertheless claims it still holds the old password and recommends rerunning.

A rerun must open the repository with that installed secret before it tries the new password. It therefore fails again—even though the new source file and its verified repository key remain available. A missing mount source can stop container creation earlier still. After backup restarts, its unusable secret also prevents backup and shipment jobs from authenticating.

The shell decision probe confirmed that an unusable installed password is refused before a usable new password is considered. The existing installation-failure fake explicitly exits **without modifying the destination**, so its successful-rerun test misses this state (`tests/unit/test_rotate_restic_password_script.py:126`, `:542`).

**Fix:** Install into a temporary file in the secret directory, establish its ownership/mode, then atomically rename it over the live secret. Also document recovery for an already damaged/missing installed file: verify and install the retained new password, restart backup, then remove the recorded old key. Add failure cases that remove or truncate the destination, rather than only failing before modification.

**I2. A prebuilt image is rebuilt through sibling services sharing its tag.**

**Where:** [prebuilt.py:92](/home/user/job-hunter/tests/support/prebuilt.py:92), [compose.dev.yaml:39](/home/user/job-hunter/deploy/compose.dev.yaml:39), [compose.dev.yaml:92](/home/user/job-hunter/deploy/compose.dev.yaml:92), [compose.dev.yaml:136](/home/user/job-hunter/deploy/compose.dev.yaml:136).

**Failure path:** With `JOBHUNTER_STACK_PREBUILT=api`, the helper verifies that `jobhunter-app:dev` exists, then builds `migrate worker embedder docparse`. Both `migrate` and `worker` inherit the same `jobhunter-app:dev` image/build definition as `api`. Their build can therefore replace the supposedly protected image and perform the downloads that selecting it as prebuilt was intended to avoid. `up --no-build` cannot undo that preceding explicit build.

An in-memory probe using the actual Compose YAML and mocked subprocess calls produced exactly this sequence. The current test models only the distinct embedder image, so it does not exercise aliases (`tests/unit/test_prebuilt_stack.py:20`, `:60`).

**Fix:** Resolve the image references of all buildable services, then exclude every service whose image reference belongs to the prebuilt set. Alternatively, reject partial selections of a shared-image group before any build. Add a shared-tag regression case and apply the same rule to the analogous filter in `tests/support/prodstack.py:150`.

#### Minor

**M1. Restic success messages overstate which passwords remain valid.**

**Where:** [rotate-restic-password.sh:129](/home/user/job-hunter/scripts/rotate-restic-password.sh:129), [rotate-restic-password.sh:256](/home/user/job-hunter/scripts/rotate-restic-password.sh:256), [RUNBOOK.md:582](/home/user/job-hunter/docs/RUNBOOK.md:582).

**Failure path:** A repository with a third key retains that key after rotation, but the script says the repository opens “with the new password only.” The operational test deliberately creates that state and confirms two keys remain after rotation (`tests/stack/test_prod_drills.py:714`, `:737`). An operator could mistake successful rotation for revocation of every other repository password.

**Fix:** Say that the new password works and the selected old key was removed. State that other repository keys remain unchanged; keep their removal a separate explicit operation.

### Challenges to rulings

None. I1 concerns installation failure recovery within FU-10’s accepted order, and I2 concerns enforcement of F6-2’s no-build contract. Neither requires changing those decisions. FU-7 remains sound as a temporary deferral for the stated undeployed product, with its predeployment refresh requirement intact (`.superpowers/sdd/2026-11-13-m5-follow-ups/progress.md:19`, `:32`, `:59`).

### What I ran

- Read `git log --oneline 012a451..8812840`, per-module diffs, the spec changes, plan, rulings, and task reviews/reports.
- `git diff --check 012a451..8812840`: passed.
- `bash -n` on both rotation scripts and `host-setup.sh`: passed.
- Read-only Python in-memory probes, using the existing interpreter with bytecode writes disabled:
  - 40 operator invalid-input cases and four missing-file role/environment checks passed.
  - Five Dockerfile pip-removal checks passed.
  - Five export-authentication cases passed: valid multisegment archive, truncation, foreign export ID, wrong key and trailing bytes.
  - 35 bounds-analysis checks passed.
  - Two mocked restore outcome checks passed: cleanup failure fails a passing run and preserves a preceding step failure.
  - Shared-image build selection reproduced I2.
- An in-memory shell probe of the restic decision logic reproduced I1’s rerun refusal.
- Attempted focused `uv run --group docparse pytest -W error -q -p no:cacheprovider …`; **no tests executed** because uv could not create its cache-lock temporary file on the read-only filesystem.
- An initial combined probe was interrupted after it stalled; the relevant checks were rerun separately to completion.

No Docker, network or paid calls, sudo, sub-agents, repository writes, or reads of `.secrets/` or `.fixtures-raw/`. Frontend, integration, stack and E2E acceptance results were reviewed as reported evidence, not independently rerun.