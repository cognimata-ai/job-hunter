# M5 follow-ups: final whole-range review (Fable)

Range `012a451..8812840` (33 commits). Reviewer: Claude Fable. Read-only; no Docker, no network, no paid call.

### Verdict

**APPROVE.** I found no Critical and no Important finding. The operator file is fail-closed in production at every entry I could trace, and no value reaches an error text, a log line or a repr. Both rotations keep every password off command lines and out of printed output, and each interruption point I walked through either finishes on a second run or is covered by a RUNBOOK step. The hardening items do what their rulings say, and the spec rows, the RUNBOOK, PRIVACY.md and the i18n rows agree with the code. Five Minor findings follow; none blocks the pull request. The one I would act on before the deployment step is Minor 1: the base-image refresh that Ruling FU-7 makes a condition of deployment is recorded in no tracked file.

### Focus questions 1–6

**1. The operator file (F1), end to end.**

- **Leaks: none found.**
  - `OperatorFileInvalid.reason` names a key of `FIELDS` or a rule, never a value and never an unknown key (`backend/src/jobhunter/privacy/operator.py:115-160`). A JSON error gives line and column only, and is raised `from None` (`operator.py:142-151`).
  - `OperatorDetails` hides all five fields from its repr (`operator.py:67-75`).
  - The only log line is `operator_file_invalid` with the path and the reason (`operator.py:178`).
  - My own probe fed 15 malformed files that each carried a marker value (BOM, trailing bytes, truncation, an unknown key, a duplicate unknown key, a non-string, an overlong value, a control character, a placeholder, a lone surrogate, bad UTF-8, an array, a 5000-digit integer, 100 000 nested brackets, `NaN`). None put the marker into the message, the formatted traceback or the repr, and none escaped as another exception.
  - The values live only in `api.state.operator` (`backend/src/jobhunter/api/app.py:199`, `:300`). They are not in the database, so no export, backup or trace can carry them.
  - Fixtures are fictional: `deploy/test/operator.json` uses `example.org`, and the tests use made-up names.
- **Fail-closed in production: yes.**
  - `startup-check --role api` and the api's start: `_check_operator_file` refuses a missing or invalid file for a prod api (`backend/src/jobhunter/config.py:734-747`, called at `:801`).
  - The api's read at start raises again for a file gone or broken since (`operator.py:170-182`).
  - The CLI commands that take the api role (`cli_user.py:111`, `cli_sessions.py:51`, `cli_api.py:35`) run the same assertions, so they refuse too. The RUNBOOK says so (`docs/RUNBOOK.md`, section 11).
  - Compose makes no directory in place of a missing file (`deploy/compose.yaml:125-129`), and the mount is on the api only.
  - Host setup stops a real run before any change when the file is missing (`scripts/host-setup.sh:172-177`). It checks existence only; the content is checked at its step 5, after the secrets are installed. That is documented in the script's header, so I do not raise it.
- **Text only: yes.** `fillOperator` replaces markers inside text spans after the parse, through a replacer function, so a value is never read as Markdown or as a replacement pattern (`frontend/src/system/privacyDoc.ts:207-215`). The spans render as React text (`frontend/src/system/PrivacyStatement.tsx:30-42`).
- **Documents agree.** PRIVACY.md keeps its five markers and names the operator file; spec §19.2, §16.8 item 11 and §20.6 match the code; the RUNBOOK's checklist row, section 2 item 3, the changed-file procedure and the recovery kit list all match.

**2. The rotations (F4).**

- **No password leak found.**
  - `restic_password`: the new password goes from the file to the one-off's stdin, then to a mode-0600 file on the container's `/dev/shm`, removed by a trap (`scripts/rotate-restic-password.sh:64-69`, `:90-91`). Every restic call names password files by flag, and restic's output goes to `/dev/null` (`:76`, `:104`, `:124`). The printed command lines hold the script text, paths and key ids only (`:147-160`, `:212-213`).
  - `pg_bootstrap`: the same stdin route (`scripts/rotate-db-role.sh:73-78`), and psql reads the value into a variable from the file (`:85-88`). `PGPASSWORD` is in psql's environment only. The server logs no statement (`deploy/postgres/postgresql.conf:23`, `:27`).
  - The S3 keys and `PGPASSWORD` are in the one-off's process environment, as they are for the backup jobs. I accept that.
- **Interruption points.** I walked each one:

  | Stopped after | State | A second run |
  |---|---|---|
  | restic: key added, before install | both passwords open the repository; old file installed | finds the new key, adds none, continues (`rotate-restic-password.sh:95-109`) |
  | restic: install, before or during restart | new file installed; backup may still mount the old one | says to restart backup first, then remove the old key by hand (`:220-231`); RUNBOOK section 7 agrees |
  | restic: restart, before removal | new password everywhere; old key still there | same message; the RUNBOOK's by-hand removal applies |
  | restic: removal fails | old key may linger | exit 1 naming the key id (`:249-254`) |
  | pg_bootstrap: `ALTER`, before install | role has the new password; old file installed | logs in with the new password, sets nothing, installs (`rotate-db-role.sh:79-82`) |

  One gap is left, a partly written secret file; see Minor 2.
- **Sudo shapes.** The stack case uses exactly four: `install … .secrets/test/<name>`, `rotate-db-role.sh … db_app`, `rotate-db-role.sh … pg_bootstrap` and `rotate-restic-password.sh`, each with PT's command line and `.secrets/test` (`tests/stack/test_prod_drills.py:362-376`, `:695-721`). That matches M5-U6 plus M5-U11. The module's docstring still says two; see Minor 3.

**3. Hardening (F3) and its interactions.**

- **Export hash repair.** The repair now authenticates every segment under the data key as this export's before it writes a hash, and hashes the same bytes it authenticated (`backend/src/jobhunter/crypto_rotate.py:258-269`, `:363-366`). `iter_decrypted` reads to the end of the file and refuses a trailing partial frame, so the hash covers the whole file. A file that does not open stops the run as `ExportUnauthenticated` with the row unchanged.
- **Restore-test cleanup (FU-8).** I found no silent path.
  - A cleanup error after passing steps fails the run as `cleanup:<class>` (`backend/src/jobhunter/backup/restore_test.py:339-342`); after a failed step it sits beside that step's code.
  - `_clean_up` catches every `Exception` from the drop and every `OSError` from the removal (`:285-296`).
  - A run cancelled or killed during cleanup stays `running`, and the scheduler's next start fails it as interrupted, so the alert still fires.
  - If the leftover removal at a start fails, it is only logged, but the next restore test then cannot succeed with the copy still there (its own cleanup fails again).
- **`login_lockouts` and `auth.lockout`.** The rule deletes rows whose lock ended more than 30 days ago and resolves the alert in the same fenced transaction when the global row is among them (`backend/src/jobhunter/privacy/retention.py:562-570`). The code and subject match the throttle's (`backend/src/jobhunter/auth/throttle.py:113`, `:140-144`). `scope` is the primary key, so a live global lockout cannot coexist with the expired row the rule deletes. The throttle deletes an expired row before it writes a new one, so both take the lockout row before the alert row; I see no lock-order inversion.
- **Lock confirmation and CLI codes.** The unchanged-stream return confirms the lock first (`backend/src/jobhunter/backup/shipper.py:286`). `ShipLockLost` prints `ship_lock_lost` and a psycopg error prints `database` (`backend/src/jobhunter/cli_backup.py:183-188`). The RUNBOOK's "A lost lock" paragraph and its ship step agree.

**4. The base images (F2).**

- **The python pin** has the lock's format, tag and a sha256 digest (`deploy/images.lock:2`). I could not check it against the registry (no network); the F2 review did that live.
- **pip** is removed in every stage that ships: the app's `runtime` (and `backup` through it), docparse's `runtime`, and the embedder's `fake` and `runtime` (`embedder/Dockerfile:60-62`, `:76-78`). `tests/unit/test_image_stages_no_pip.py` derives the shipped stages from `USER` lines, so a new stage cannot slip past it.
- **The E2E fallback** applies only where `/opt/pw-browsers/chromium` exists (`frontend/e2e/playwright.config.ts:37`), so CI is unchanged.
- **FU-7** is sound for a product nobody runs yet, on one condition; see "Challenges to rulings" and Minor 1.

**5. The UI (F5).**

- **`GET /api/privacy/exports`** sits behind the router's session dependency (`backend/src/jobhunter/api/routes/privacy.py:132-134`). It returns id, status and two times for the running export and unexpired ready ones, newest first, at most 5 (`:159-162`, `:481-494`). No size, path or address. `expires_at` is `NOT NULL`, so a running row cannot break the response model. Spec §14.2 and §20.3 say the same, including the expired-but-still-downloadable case.
- **The export card.** After a reload it follows the newest export in use until the user starts one (`frontend/src/system/PrivacyActions.tsx:76-83`). A successful start invalidates the list, so a card shown again later finds the new export (`frontend/src/system/privacyApi.ts:144`).
- **Load more** reuses the shared keyset hook; a poll re-reads each loaded page from the fresh cursor of the page before (`frontend/src/system/systemApi.ts:98-113`).
- **The table layout.** The card rules moved to a container query (`frontend/src/settings/tables.module.css:137-144`). Every table that uses the shared `.table` class is inside a `.scroll` box (Queries, Watchlist, Runs, Cycles, Golden set), so none lost its narrow-screen cards. `.scroll` is positioned (`:10`).
- **The E14 guard** requires the four tables to exist, so it cannot pass on an empty page (`frontend/e2e/specs/17-e14-warnings.spec.ts`).

**6. Drift.** I compared each named spec row with the code and found none: §14.2 (both new routes), §16.8 (item 11), §19.2 (the operator file), §19.5 (the lock confirmation, the cleanup outcomes), §19.8 (both rotations, `retention run`, host setup), §20.1 and PRIVACY.md and both i18n rows (30 days after the lockout ended, `retention.py:253-260`, `:391`), §20.3 and §20.6. The RUNBOOK's `retention run` outputs match `backend/src/jobhunter/cli_retention.py:73-93`. Two small wording drifts are Minor 3 and Minor 5.

### Findings

**Critical:** none.

**Important:** none.

**Minor:**

1. **The refresh FU-7 requires before deployment is in no tracked file.** `docs/RUNBOOK.md`, section 13 (the deployment step), and `deploy/images.lock:2`.
   - Failure path: Ruling FU-7 leaves a fixable openssl HIGH and a fixable apt HIGH in all four images, on the condition that the python refresh lands "before any deployment". That condition is written only in `.superpowers/…/progress.md`, which git does not track. CI's scan gate is CRITICAL-only, so nothing stops a release with those findings.
   - Fix: add an item to the RUNBOOK's deployment step (or to step 3, the release) that names the refresh and the scan at HIGH, or record the remaining findings and the due date in `docs/spikes/`, as the plan's F2 file list allowed.
2. **A partly written secret file makes the restic rotation loop on "run it again".** `scripts/rotate-restic-password.sh:238-240`; `docs/RUNBOOK.md:603`.
   - Failure path: `install` removes the destination before it copies. If it fails midway (a full root filesystem, a power loss), `restic_password` is missing or truncated. The script says the file "still holds the old one: run it again". The next run's one-off then finds that the installed password opens no key, stops with "unchanged … run it again", and every further run does the same. The running backup container keeps the old file until it restarts; after a reboot it cannot start. I reasoned this from the script; I did not run it.
   - Fix: on a failed install, tell the operator to install `<new file>` by hand (its key is already added and verified), as the `pg_bootstrap` branch already does (`scripts/rotate-db-role.sh:184-187`). Or install to a temporary name in the secrets directory and rename it.
3. **The drill module's docstring still names two root commands.** `tests/stack/test_prod_drills.py:44-47`, `:363`.
   - Failure path: the docstring is the module's statement of its permission boundary (M5-U6). The module now runs four shapes (M5-U11). A reader checking the boundary against the docstring gets the wrong answer.
   - Fix: name the four shapes and M5-U11 there.
4. **The digest PR's body can misname its head commit.** `.github/workflows/release.yml:170`; `docs/RUNBOOK.md:852`.
   - Failure path: the RUNBOOK offers "pushing a commit to its branch" as a way to start CI on the digest PR. After that push the PR's head is no longer the tagged commit, while the body says the tag is "this PR's head commit". The deploy by tag is still right (the tag's tree is the built tree plus `release.lock`), but what is merged then differs from what is deployed.
   - Fix: say "the commit this workflow made" in the body, and in the RUNBOOK prefer dispatching CI over pushing a commit, or say the extra commit must not change the tree.
5. **One RUNBOOK row names only half of the cleanup failure.** `docs/RUNBOOK.md:427`.
   - Failure path: the `backup.restore_test_failed` row says a `cleanup:<error class>` run "could not drop its scratch database". The same code covers a scratch directory that could not be removed (`restore_test.py:285-296`), as section 8 says correctly.
   - Fix: add "or remove its scratch directory" to the row.

Two observations I do not raise as findings:

- The `auth.lockout` alert stays open after a global lockout ends until the next sign-in or the 30-day rule. That was so before the range; the range puts a bound on it.
- A dry run of either rotation script by a user who cannot read the new file reports "holds no password". The RUNBOOK runs the dry run under sudo.

### Challenges to rulings

- **FU-7 (remaining HIGHs).** I do not challenge the decision: no image is deployed, the eligible python digest does not fix the findings, and an in-image `apt upgrade` would add unpinned packages. I challenge only that its condition is unrecorded (Minor 1). The failure path is a first deployment from a release cut before the refresh, which no gate would stop.
- **FU-1 (fail-closed), as it reaches the api's one-off commands.** A broken operator file also blocks `sessions unlock` and `user totp disable`, the recovery commands for a locked-out user. This is a real cost, but the RUNBOOK states it and gives the check to run before a restart, and the file changes rarely. I leave it as ruled.
- **FU-10 (1) (no lock between a manual and a nightly retention pass).** I spot-checked the rules' locking (`SKIP LOCKED`, `FOR UPDATE` with a re-check) and found no path where two passes harm each other. No challenge.

### What I ran

Every command ran with the six real-key variables unset. I used no Docker, no sudo and no network, and I read nothing under `.secrets/` or `.fixtures-raw/`.

- `git log`, `git diff` and `git show` over `012a451..8812840`, per module.
- `uv run --group docparse pytest -W error -q -p no:cacheprovider` on 16 unit files: `test_operator_file`, `test_rotate_restic_password_script`, `test_rotate_db_role_script`, `test_runbook`, `test_spec_revision`, `test_privacy_md`, `test_privacy_retention_rows`, `test_host_setup_script`, `test_image_stages_no_pip`, `test_prebuilt_stack`, `test_cli_backup`, `test_workflows_m5`, `test_openapi_m5`, `test_kind_bounds_declared`, `test_migrate_lock_key`, `test_config`. Result: 564 passed, 1 skipped.
- `npm --prefix frontend test --` on `privacyDoc.test.ts`, `PrivacyStatement.test.tsx`, `PrivacyActions.test.tsx`, `DeletionStatus.test.tsx` and `QueriesSection.test.tsx`. Result: 46 passed.
- One in-process probe, a scratch script outside the repository: 15 malformed operator files through `privacy.operator.parse`, checking the message, the formatted traceback and the repr for the planted value. Result: no leak, no escaped exception.

Not run: the integration, crash, stack, smoke and E2E suites (they need Docker), and any check of the python digest against the registry. For those I relied on the task reports and reviews, and say so where it matters.
