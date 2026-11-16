# M5 final whole-branch review — Claude Fable

Range `69689d1..077e7c3`, branch `claude/job-matching-agent-platform-1f720r`. Read-only review; paths under
`backend/src/jobhunter/` are written `jobhunter/…`.

### Verdict

**REQUEST CHANGES**, for one Important finding. The purge protocol, the replay, the compatibility floor, the
rotation and the deployment surface hold up under a whole-branch reading: I found no path that deletes before
the manifest's file and hash are committed, none that releases a fence before the deletes are durable, and no
way for a restore to drop a shipped deletion. The one blocker is at the edge of the privacy guarantee: a
restore test that is killed leaves a full copy of the database (a scratch database and a staged dump) that no
purge reaches, and the scheduler's start-up cleanup, which already handles the same case for a backup set,
does not remove it. The fix is a few lines. Nine Minor findings follow; none blocks.

Depth of this review, so the verdict is read correctly: I read the protocol modules' contracts and the
handler code in full (`privacy/purge.py`, `privacy/fences.py`, `privacy/reconcilers.py`, `backup/shipper.py`,
`db/compat.py`, migration 0011, `deploy/compose.yaml`, the firewall files, both scripts, `release.yml`,
`prodstack.yml`, `docs/PRIVACY.md`, RUNBOOK sections 1 to 10). For `privacy/replay.py`, `privacy/manifests.py`,
`backup/restore.py`, `crypto_rotate.py`, the kinds and the frontend I read the module contracts and the code
at the points each focus question names, not every line. Where an answer rests on a per-task test rather than
on my own reading, it says so.

### Focus questions 1–8

**1. Purge protocol end to end.** No defect found.

- *Delete before the file and hash are committed?* No. `run_purge` reaches phase B only with a content that
  came from `_admit` or from the row's stored `scope` (`jobhunter/privacy/purge.py:598-610`). `_admit` writes
  the file (tmp, fsync, rename, directory fsync) at `purge.py:650` and records the hash and scope in a
  transaction of its own at `purge.py:653-656`; `proc.execute` is first called at `purge.py:689`. A crash
  between the rename and A3 is adopted, never recomputed (`purge.py:627-634`), and an unadoptable file ends
  the operation `failed` with the fence kept.
- *A fence held forever?* Not silently. Every `failed` end runs `purge_failed` (`purge.py:478-496`), which
  raises `privacy.purge_failed` at once; a manifest with no live operation is re-queued and a stale one
  alerted on by `purge_fences` (`jobhunter/privacy/reconcilers.py:151-190`). A fence can stay only with a
  critical alert open and Retry offered.
- *A fence released before the deletes are durable?* No. `release_fences` runs in phase C's single
  transaction, with the postconditions and the completion (`purge.py:724-735`); every delete committed in an
  earlier chunk transaction. File removals fsync their directory (`jobhunter/privacy/manifests.py:793-805`),
  and the file postcondition is checked in phase C before the commit (`manifests.py:809-816`).
- *Derived rows above the watermark?* I found none by reading: writers take the job rows `FOR SHARE` and then
  read the fence in a later statement (`jobhunter/privacy/fences.py:137-143`), the chunk takes the same rows
  `FOR UPDATE` (`purge.py:345-355`), and a transaction that began before A0 is waited out
  (`fences.py:204-217`, `_OLDER_OPEN` at `fences.py:84-87`). Both api and worker log in as `jobhunter_app`
  (`jobhunter/config.py:186`), so the quiesce predicate covers every writer. Per-writer coverage I did not
  re-derive; it rests on `tests/integration/test_fence_coverage_m5.py` and `test_purge_in_batch.py`.
- *Shipment.* Coverage is exact, by uuid and hash, never inferred (`reconcilers.py:55-61`); a manifest shipped
  while still `executing` is matched once it completes. The shipper hashes write-once files, so its
  hash-then-backup order has no race (`jobhunter/backup/shipper.py:140-153`).
- *Export interaction and Review Focus 1 and 4.* Not re-derived; I rely on the tests the plan names.

**2. Restore and replay.** No defect that brings shipped deletions back; one Minor (M2).

- *A manifest never shipped.* If the dump holds its row, step 1 rewrites its file from `scope` and the replay
  covers it (`jobhunter/privacy/identities.py:20-26`). If the host is lost before both the next dump and the
  next shipment, the deletion is lost. That is inherent, and the product is honest about it: the UI reports
  done only at `shipped`, `privacy.manifest_unshipped` opens after 1 h (`reconcilers.py:44-45, 104-116`), and
  the RUNBOOK's wipe procedure checks the shipment itself (`docs/RUNBOOK.md:530-560`).
- *Older or newer format.* `parse_manifest` refuses any format but 1 (probe below: `unknown_format`), which
  stops `migrate`. Ruling T31-2's expand/contract rule covers a `format` change; it does not cover a kind that
  starts reading a new `max_ids` key without one. See "Challenges to rulings".
- *A purge executing at dump time.* With `file_sha256`, its content is the stored `scope`; without, nothing was
  deleted under it and the content is computed under the restored fence
  (`jobhunter/privacy/replay.py:16-18`). Both end in the same phase C as live.
- *Identity advancement.* `setval` only ever raises, to the larger of the column's maximum and the stream's
  bound, including every listed id (`identities.py:27-37`); `verify_replayed` re-checks it
  (`jobhunter/privacy/restore_verify.py:31-33`).
- *Exports after a restore.* Ended and the volume emptied after the replay
  (`jobhunter/backup/restore.py:57-62`); a set never holds an archive (the `backup` service does not mount
  `exports`, `deploy/compose.yaml:271`).
- *`data_key_ids`.* Both the sealed columns and the CV files' headers are counted
  (`jobhunter/backup/backup_set.py:214, 272-302`).
- *The two verify phases.* As specified (`restore_verify.py:6-33`); C32, C37 and C39 I did not run (stack
  tests).

**3. Alerts, dispatcher, dead-man.** No defect found, at lower depth than questions 1 and 2.

- The catalogue holds 72 rows and matches §17.6 by test (`tests/unit/test_alert_catalogue.py:103-135`, run
  below). A heuristic scan of mine for auto-resolving codes without a resolver flagged only the four
  `source.*` codes and the two backup codes, each a false positive: they resolve through
  `breaker.resolve_alerts` (`jobhunter/sources/breaker.py:46-49`) and the `backup_health` reconciler
  (`jobhunter/observability/backup_health.py:9-14`).
- The lock is 7261005 (`jobhunter/observability/deadman.py:101`), registered in the key list
  (`jobhunter/db/migrate.py:102`). A ping reads `schedule_state.enabled` and `applied_paused` under it and
  sends nothing when disabled (`deadman.py:31-34, 448`).
- Escalation, `notify.delivery_failed` and the outage back-off I did not re-derive; they rest on Task 4's and
  Task 5's tests.

**4. Secrets, keys, rotation.** No defect found.

- `SEALED_COLUMNS` is `cv_files.raw_text_enc` and `users.totp_secret_enc` (`jobhunter/crypto.py:49`), the only
  two `bytea *_enc` columns, pinned against the live schema by
  `tests/integration/test_crypto_rotate.py:541-549`. Every `crypto.encrypt` call site is one of: TOTP
  (`jobhunter/auth/users.py:208`), CV file and text (`jobhunter/profile/storage.py:90, 124, 156`), export
  segments (`jobhunter/privacy/export.py:274`). All four are rotated and counted.
- `CvStore.reseal` decrypts with any mounted key and replaces in place (`storage.py:119-129`).
- Exit codes 0, 1, 2, 3 match the RUNBOOK's table (`jobhunter/crypto_rotate.py:47-54`;
  `docs/RUNBOOK.md:468-473`).
- `scripts/rotate-db-role.sh`: no password reaches a command line or a log. psql reads it from the mounted
  file inside the container (`scripts/rotate-db-role.sh:45-48`), the bootstrap password is in the one-off's
  environment only (`:43-44`), `step` prints names and paths (`:67-74`), and PostgreSQL cannot log the
  `ALTER ROLE` (`log_statement = none`, `log_min_error_statement = panic` in
  `deploy/postgres/postgresql.conf`).
- Kit retention: 3 months and a week after the rotation's exit 0 (`docs/RUNBOOK.md:582-585`), consistent with
  the prune schedule.
- `startup-check --role` runs for api, worker, migrate, backup and restore (`scripts/host-setup.sh:45,
  201-205`), matching `ROLE_SECRETS` (`jobhunter/config.py:145-178`) and `deploy/secrets.example`.

**5. Production deployment surface.** One Important (I1), four Minor (M3, M4, M5, M6).

- Least privilege holds: every app service is read-only, `cap_drop: [ALL]`, `no-new-privileges`, uid 10001
  (`deploy/compose.yaml:39-51`); the api holds four secrets and no provider key (`:118`) on two internal
  networks (`:120`); docparse has `network_mode: none` and no secret (`:191-197`); only worker, backup and the
  one-off restore are on `egress` (`:176, 272, 288`).
- Workflows: actions pinned to SHAs, `contents: read` by default, write scopes only on the job that needs them
  (`.github/workflows/release.yml:27-28, 38-40, 127-129`), every script value from the environment
  (`release.yml:135-165`), `pull_request` (not `pull_request_target`) in `prodstack.yml`. No injection found.
- *Would a fresh VPS following the RUNBOOK end up as the spec describes?* Yes, with these divergences:
  1. `nft` is assumed at `/usr/sbin/nft` (M4): on an image without nftables host setup stops at step 4, after
     the secrets are installed.
  2. The operator fields of the privacy statement stay placeholders unless the pending decision is taken
     (parked item 2).
  3. The backup service gets Docker's default 10 s stop grace, so any restart during a backup job kills it
     (I1, M3).

**6. Privacy statements match behaviour.** Consistent on every point the brief lists; two understatements.

- Delete everything keeps: PRIVACY §6 (`docs/PRIVACY.md:119`) matches `DELETE_EVERYTHING_KEEPS`
  (`jobhunter/privacy/kinds/delete_everything.py:167-209`).
- Export retention: 7 days and rows 30 days (`jobhunter/privacy/export.py:169, 186`; `PRIVACY.md:82`).
- Offsite lag: "about 4 months" in PRIVACY §6, both UI strings (`frontend/src/i18n/en.json:1653`,
  `de.json:1653`) and the spec (L6700, L6741, L6920).
- Understated: the logs (M1) and healthchecks (M7). And I1 is a copy of the data that the statement's "delete
  everything removes all your content" does not cover.

**7. Frontend.** No defect found, at the lowest depth of the eight.

- en and de hold the same 1765 keys (my parity script); the new problem codes `confirmation_mismatch`,
  `delete_everything_in_progress`, `gone` and `reauth_failed` are in `frontend/src/i18n/codes.json:4-18` with
  text in `en.json:46-59`.
- The eight Privacy and System test files pass (51 tests, below). I did not read the components line by line,
  and I did not run Playwright, so the CSP and axe guards and E18 are unverified by me.

**8. Compatibility and rollback.** No defect found.

- `classify_schema` (`jobhunter/db/compat.py:182-196`) accepts an unknown head only with a non-destructive row
  whose floor the code knows; since floors only move forward (`compat.py:112-127`), no destructive revision
  can sit between. My probe confirms the six cases (below).
- 0011 adds one index, floor 0010, not destructive
  (`jobhunter/db/migrations/versions/0011_m5_purge_operation_index.py:35-45`).
- *Run on a schema it should refuse?* Only where a release mis-declares: a narrowed transition edge not
  declared destructive passes `/readyz` and fails later. The docstring states the rule (`compat.py:40-43`);
  nothing enforces it. Acceptable.
- *Refuse one it could run?* A head without a `schema_compat` row is refused, which is the safe side.
- The rollback section is sound, including "remove no volume" when the previous release cannot read the
  stream (`docs/RUNBOOK.md:704-739`). The upgrade drill I did not run.

### Findings

#### Critical

None.

#### Important

**I1. A killed restore test leaves a full copy of the database that no purge reaches.**
`backend/src/jobhunter/backup/scheduler.py:181-190`, `backend/src/jobhunter/backup/restore_test.py:191-198,
222-236`, `deploy/compose.yaml:256-273`.

- *Failure path.* The monthly restore test creates the database `jobhunter_restore_test` and stages the set,
  with `jobhunter.dump`, under `<staging>/restore_test` (`restore_test.py:104-119`). Its cleanup is a
  `finally` (`restore_test.py:231-234`), so it runs only if the process survives. The `backup` service has no
  `stop_grace_period` (only the worker has one, `compose.yaml:133`), and the scheduler lets a running job
  finish on SIGTERM (`scheduler.py:23-24`), so Docker kills it after 10 s. Any `$JH restart backup` (the
  RUNBOOK's S3-key and `db_backup` rotations), any `up -d` that recreates it (every upgrade), any `stop`, a
  host reboot or an OOM at its 1 GiB limit during the test therefore skips the cleanup.
- At the next start `fail_interrupted` marks the run `failed` and calls `clear_staging`, which removes only
  `set/` (`backup_set.py:345-352`). The scratch database and `restore_test/` stay until the next restore test
  drops them (`restore_test.py:108, 111`): up to a month, or longer if the cron is unset or the job keeps
  finding the lock busy.
- Meanwhile a delete everything, an Adzuna purge or a profile purge completes and is shown "confirmed
  offsite", while the scratch database still holds every row and the staged dump a full plaintext copy.
  Neither is in the manifest's reach, and PRIVACY.md mentions neither.
- Mitigation that exists: the interrupted run raises `backup.restore_test_failed`, and the RUNBOOK's row says
  to run the test again, which cleans up. That depends on the operator acting, and nothing says why it
  matters.
- *Fix.* In `fail_interrupted`, also drop `jobhunter_restore_test` and remove `<staging>/restore_test` (the
  existing `_clean_up`), and remove `<staging>/restore` on the same grounds (`restore.py:63-64` removes it in
  a `finally` too). Add a test beside the existing interrupted-set test. Give `backup` a `stop_grace_period`
  long enough for a set (M3).

#### Minor

**M1. The logs keep client addresses longer than the statement says, and delete everything leaves them.**
`backend/src/jobhunter/api/routes/auth.py:242, 247, 270, 332, 335`, `backend/src/jobhunter/api/reauth.py:69,
77, 119, 153, 165`, `deploy/compose.yaml:34-36`, `docs/PRIVACY.md:15, 77, 118-119`.
- The api logs the client address on every sign-in and re-authentication, accepted ones included. The `local`
  driver rotates by size only (10 × 20 MB). The api writes no access log, so it fills that very slowly: its
  lines can stay for far longer than "about 14 days", until the container is recreated.
- Delete everything removes `login_attempts` and `login_lockouts` because they hold addresses (Deviation 2),
  but the same addresses stay in the api's log, and "What it keeps" does not list logs.
- *Fix.* Either drop `ip` from these log lines (the `login_attempts` row already records it, with a 30-day
  rule and delete-everything coverage), or state in PRIVACY §2, §5 and §6 that logs rotate by size, can hold
  sign-in addresses until the service is recreated, and are not removed by delete everything. The spec's
  L6538 row changes with it.

**M2. A restore takes the newest `manifests` snapshot, which can be an incomplete one.**
`backend/src/jobhunter/backup/restore.py:203-218`, `backend/src/jobhunter/backup/shipper.py:19-22`.
- The shipper's own docstring says that after restic's exit 3 the newest `manifests` snapshot is incomplete
  until the next run. A host lost in that window restores a stream that lacks a file an earlier complete
  snapshot held. If that manifest is also newer than the restored dump, its deletion is not replayed.
- It needs a file that was readable once and unreadable later, plus a loss within minutes, so it is unlikely.
- *Fix.* Manifest files are write-once, so restore the union: every `manifests` snapshot's directory, oldest
  first, into the target. Or `restic forget` the snapshot when `backup` exits 3.

**M3. The backup service has no stop grace.** `deploy/compose.yaml:256-273`.
- A nightly set interrupted by a restart is killed after 10 s and recorded `interrupted`; the next night's set
  replaces it, and `backup.failed` fires meanwhile. Wasteful rather than unsafe, except for I1.
- *Fix.* `stop_grace_period` on `backup`, sized to a set's upload.

**M4. Host setup assumes nftables.** `deploy/firewall/jobhunter-egress.service:13`,
`scripts/host-setup.sh:196-198`, `docs/RUNBOOK.md:142-233`.
- On an image without `/usr/sbin/nft`, `systemctl enable --now` fails at step 4, after the secrets were
  installed and before the startup checks.
- *Fix.* Check `command -v nft` in the preflight, and name the package in RUNBOOK section 2.

**M5. Nothing re-loads the egress rule if the ruleset is flushed after boot.**
`deploy/firewall/jobhunter-egress.service`, `docs/RUNBOOK.md:233, 317-319`.
- A `nft flush ruleset`, or a firewall front end's reload that flushes, deletes the table. The unit stays
  `active (exited)`, so the RUNBOOK's `systemctl status` check still looks healthy; only its second command,
  `nft list table`, shows the loss, and it is asked for after a boot only.
- *Fix.* Make `nft list table inet jobhunter_egress` the check, and ask for it after any firewall change.

**M6. The brief's vitest command fails from the repository root.**
- `npm --prefix frontend exec vitest run <file>` runs vitest with the root as its working directory: 42 of 44
  tests failed for me, and it wrote a cache to a new `node_modules/` at the root. From `frontend/` the same
  files pass. Not a product defect; the review brief and any contract that copies the command should say
  `cd frontend`.

**M7. "Pings only" understates what healthchecks receives.** `docs/PRIVACY.md:40`,
`backend/src/jobhunter/observability/deadman.py:304-345`.
- The worker also calls the Management API to read and set the check's timeout, grace and pause. No personal
  data, but the row is not accurate.
- *Fix.* "Pings, and the check's period".

**M8. A manifest whose `max_ids` lacks a key its kind reads stops `migrate` with a bare `KeyError`.**
`backend/src/jobhunter/privacy/kinds/job_tombstone.py:254`,
`backend/src/jobhunter/privacy/kinds/profile_purge.py:510`. Parked item 6; judged below.

**M9. 0010 declares floor 0009, but no 0009 release can read a floor.**
`backend/src/jobhunter/db/migrations/versions/0010_m5_operations.py:66-67`.
- The reader (`schema_state`) arrives with M5, so M4's code would stop in Alembic on an unknown head whatever
  the row says. No M4 production release exists, so nothing fails; the declaration promises a rollback nobody
  can perform.
- *Fix.* None needed; a sentence in the docstring would stop a later reader relying on it.

### Challenges to rulings

**T31-2 (expand/contract for a manifest-format change).** The rule is sound for a `format` change, but the
same failure arrives without one. The format's `max_ids` is an open `dict[str, int]`
(`jobhunter/privacy/manifests.py:190`), filled from whatever identity tables exist when the manifest is
computed (`manifests.py:412-425`), and the kinds index it by table name (`job_tombstone.py:254`,
`profile_purge.py:510`).

- *Failure path.* Release N+1 adds a table to a kind's procedure and reads `content["max_ids"]["<new
  table>"]`, with `format` still 1. A manifest that release N wrote has no such key. A restore under N+1 of a
  dump older than that manifest replays it with N+1's procedure and stops with a `KeyError` in `migrate`,
  after `pg_restore`.
- It is recoverable (restore with release N, then upgrade), so this is not a blocker. But the rule as written
  tells an implementer that only a `format` bump needs care.
- *Ask.* Extend the rule in §19.6 and `db/compat.py`: a kind that reads a key older manifests lack treats the
  missing key as 0 (the table did not exist, so nothing of it is in the bound), or the change counts as a
  format change. Parked item 6's named refusal makes the stop legible in the meantime.

No other ruling allows a failure I could construct.

### Parked list, section 2

1. **T28-7, the export hash repair.** Acceptable as parked. Nothing reads `exports.sha256`: neither the api's
   models nor the frontend expose it, and the only writer and reader is the rotation
   (`jobhunter/crypto_rotate.py:129-132, 317-328`). A corrupt archive fails its own AEAD on download, each
   segment bound to its export id, index and final flag (`jobhunter/privacy/export.py:274, 302`). Fix it
   before anything starts to trust the column.
2. **T31-1 (5), the operator fields.** Acceptable for M5's acceptance, since deployment is a later step, but
   something is missing: a way to fill them without committing a contact address, and a guard against
   shipping placeholders. Today a deployment that follows the RUNBOOK serves "[set at deployment: …]" as the
   controller. I would add the five values to `/etc/jobhunter/jobhunter.env` (they are not secrets), serve
   them from the api and have the SPA substitute them, with a prod startup check that refuses empty ones. The
   markers test then stays as it is. The deployment step should not start until this is decided.
3. **T30-3 (2), the E18 race.** Acceptable while the gate does not run. Not acceptable once `e2e` is a
   required check: a known race in a per-PR gate teaches people to re-run. Make it deterministic (pause the
   worker, or accept "requested" or the next state) in the change that turns CI green.
4. **T27-4 M2, generic plans and index 0011.** Acceptable. The readers are the heavy-cadence reconciler and
   delete-everything's own lookup, on a table retention keeps small. Also, PostgreSQL keeps custom plans when
   the generic plan costs more, which it does when a bound `kind` hides a partial index, so the index is
   likely used anyway. I did not measure this.
5. **The egress rule fails open.** Acceptable as parked. The api and docparse have no route out without it
   (`deploy/compose.yaml:120, 191`, internal networks at `:297-306`), so the rule is the second layer, and
   refusing to start Docker on a broken rule file would trade a defence-in-depth gap for an outage. Tighten
   the manual check (M5).
6. **The bare `KeyError`.** Acceptable to ship M5 with it, since every manifest in existence was written by
   this release's code. It must change before the first release that alters what a kind reads or adds an
   identity table, for the reason under "Challenges to rulings". The named refusal is cheap: validate
   `max_ids` against the kind in `parse_manifest`'s caller and raise `ManifestInvalid` with the uuid and kind.
   My probe confirms the gap: a `job_tombstone` manifest with an empty `max_ids` parses.

### What I ran

No Docker, no compose stack, no network or paid call, no sub-agent. `.secrets/` and `.fixtures-raw/` were not
read.

- `uv run --group docparse pytest -W error -q -p no:cacheprovider` on `tests/unit/test_compat_floors.py`,
  `test_manifest_format.py`, `test_firewall_files.py`, `test_compose_prod.py`, `test_privacy_md.py`,
  `test_runbook.py`, `test_rotate_db_role_script.py`, `test_alert_catalogue.py`, `test_workflows_m5.py` and
  `test_spec_revision.py`: 131 passed in 30 s.
- `npx vitest run` from `frontend/` on `PrivacyActions`, `DeletionStatus`, `AlertsList`, `NeedsAttention`,
  `privacyDoc`, `PrivacyStatement`, `HealthPanel` and `PrivacySection` tests: 8 files, 51 tests passed.
- The brief's form of that command, from the repository root, first: 42 of 44 failed (M6). It created an
  untracked `node_modules/` at the root holding only vitest's caches (`.vitest-cache`, `.vite`); I removed that
  directory, and `git status --short` was empty afterwards. That is the only change I made to the tree other
  than this file.
- An in-process probe of `classify_schema`: floor 0010 on head 0011 is `ahead_compatible`; an unknown floor, a
  destructive head, a head without a row and two heads are each `ahead_incompatible`.
- An in-process probe of `parse_manifest`: formats 0 and 2 are refused `unknown_format`; a `job_tombstone`
  manifest with an empty `max_ids` parses.
- A scratch script comparing each auto-resolving catalogue code with the resolve sites in the source: six
  flags, all false positives (question 3).
- A key-parity script over `en.json` and `de.json`: 1765 keys each, none missing on either side.

Not run: integration and crash tests (they need the database), every stack test, the drills, the smoke and
Playwright.
