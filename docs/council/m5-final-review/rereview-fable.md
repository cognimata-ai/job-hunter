# M5 final re-review — Claude Fable

Fix range `0f5a731..9864297` (fix A `0f5a731..d454f3d`, fix B `d454f3d..9864297`), read commit by commit
against both first-round reviews, Rulings F-1, F-2 and F-3, and the two fix reports. Paths under
`backend/src/jobhunter/` are written `jobhunter/…`.

### Verdict

**REQUEST CHANGES**, for one Important finding. Every other fix holds as ruled: the shipment is
serialised, the restore reads a conflict-checked union, a killed restore test is cleaned up at the next start,
the bound refusal is named and statically complete, the dead-man disable is ordered against each send, and
no log line carries a client address any more. The fix that does not close its finding is Codex I1's. The
Adzuna purge finds the feedback snapshots to redact through the row's foreign keys, and two ordinary
retention paths null those keys long before a purge runs (job tombstoning, and the score-history rule). A
snapshot detached that way keeps its Adzuna-derived scores through the purge and is still exported. Ruling
F-1 closed the detached case with "no deployment ran an Adzuna purge", which covers detachment by an earlier
purge only. The fix is small and needs no migration. Eight Minor findings follow; none blocks.

Limit of this verdict: every test that needs PostgreSQL starts it through Docker
(`tests/support/pg.py:331`), which this review may not use. So I did not re-run the integration, crash or
stack tests the reports name, and their RED and GREEN outputs are the implementers' evidence, checked here
by reading the tests and the code. What I ran is listed at the end.

### Findings mapped

| Finding | State | Evidence |
|---|---|---|
| **Codex C1** (newest `manifests` snapshot can omit a receipted manifest) | Fixed, with a stated residual (N2) | Listing, upload and receipt run under session lock 7261007 (`jobhunter/backup/shipper.py:193-207`); a second shipper polls and then lists the stream itself (`shipper.py:141-145`). The restore merges every `manifests` snapshot, oldest first, and stops on one name with two hashes before anything reaches the target (`jobhunter/backup/restore.py:262-279, 323-329`). See "C1 in detail" below. |
| **Codex I1** (Adzuna scores in `score_feedback.score_snapshot`) | **Partly fixed** (N1) | Live purge, replay, postcondition and export are right for a row whose keys still name its provenance: ids frozen at A2 (`jobhunter/privacy/kinds/source_purge.py:237-243, 546-548`), `field_redact` executed by the same `execute` in live and replay (`source_purge.py:929-931`), typed postcondition `IS NULL` (`jobhunter/privacy/manifests.py:737-739, 919-920`), export reads the column (`jobhunter/privacy/export.py:394-399`). A row whose keys were nulled earlier is never frozen. |
| **Codex M1** (disable during a dead-man pass) | Fixed | One lock hold per run, with schedule, pause and watermark read again (`jobhunter/observability/deadman.py:484-496`); a disabling save takes the same key as a transaction lock (`jobhunter/settings/save.py:368-371`). One unlocked writer remains, harmless today (N8). |
| **Codex M2** (E18 transient state) | Fixed, not run by me | Worker paused before the request and unpaused after the executing-state checks, with an `afterEach` (`frontend/e2e/specs/20-e18-privacy.spec.ts:66-70, 196-207`). A rarer race replaces the old one (N6). |
| **Codex M3 / Fable M8** (bare `KeyError` on a missing `max_ids` key) | Fixed | Every manifest's content is checked before any postcondition reads a bound (`jobhunter/privacy/replay.py:320-341`); the required set is the kind's declared keys plus the operations' (`replay.py:312-317`, `manifests.py:887-907`). The static test passes for all five kinds and fails on seven planted reads (run below). The remaining reads outside the kinds are guarded (`manifests.py:633-634, 721-722, 844, 876-879`). Consequence for a later release: N3. |
| **Codex challenge to T24-2(i) / T26-1** | Fixed | Superseded by the shipment lock; registered beside the other keys (`jobhunter/db/migrate.py:109-110`), uniqueness test passes. |
| **Codex challenge to T10-4** | Fixed | An exit-3 snapshot can no longer hide a manifest: the union reads it beside the others; one that holds no file is skipped (`restore.py:288-303`). |
| **Fable I1** (killed restore test leaves a full copy) | Fixed | The scheduler's start always calls `remove_leftovers` (`jobhunter/backup/scheduler.py:200-201`), which drops `jobhunter_restore_test` and removes `restore_test/` and `restore/` (`jobhunter/backup/restore_test.py:215-249`). It runs whether or not a row was `running`, so a crash between the two steps is covered at the following start. One path stays open (N5). |
| **Fable M1** (addresses in logs) | Fixed | Every `login` and `reauth` line carries the outcome only (`jobhunter/api/routes/auth.py:242-248, 271, 331-336`; `jobhunter/api/reauth.py:69-70, 78, 120, 154, 166`; `jobhunter/api/edge.py:358`). My grep over `jobhunter/` for a log call with an `ip`, `addr`, `address`, `client` or `remote` field finds none; the one `host=` is a provider's host (`jobhunter/sources/http.py:274`). PRIVACY.md §2, §5 and §6 and spec L5480, L6481, L6538, L6697 and L6724 follow. F-3's "about 14 days" question: N7. |
| **Fable M2** (newest snapshot may be incomplete) | Fixed | As Codex C1. |
| **Fable M3** (no stop grace on `backup`) | Fixed | `stop_grace_period: 10m` (`deploy/compose.yaml:265`), pinned by `test_compose_prod.py` (run below); RUNBOOK sections 4, 7 and 8 say a restart can wait. |
| **Fable M4** (host setup assumes nftables) | Fixed | Preflight `command -v nft` before anything changes (`scripts/host-setup.sh:166-168`); RUNBOOK section 2 names the package. Test run below. |
| **Fable M5** (flushed ruleset looks healthy) | Fixed | `nft list table inet jobhunter_egress` is the check, after every boot and any firewall change (`docs/RUNBOOK.md:235-236, 321-326, 720-721`). |
| **Fable M6** (the brief's vitest command) | Fixed | This brief gives `npm --prefix frontend test -- <pattern>`; it ran from `frontend/` and left the root clean. |
| **Fable M7** ("Pings only") | Fixed | `docs/PRIVACY.md:40` and the spec's table; `test_privacy_md.py` holds them equal (run below). |
| **Fable M9** (0010's floor) | Fixed | The docstring states that the floor is declarative (`jobhunter/db/migrations/versions/0010_m5_operations.py`, commit `3a4ba4c`). |
| **Fable challenge to T31-2** | Fixed as ruled | The stop is now named. The rule I asked for (what a release must do when a kind starts reading a new key) is still unwritten; see N3. |

**C1 in detail.** The brief asks whether any sequence can still make a restore omit a manifest that a
successful receipt named. I tried these:

- *Two shippers.* Serialised, so snapshots are made in the order of their lists.
- *The lock lost mid-shipment* (PostgreSQL restarts under shipper A; B then ships a longer list; A's older
  list lands last, and A's receipt becomes the latest). The union still holds B's snapshot, so nothing is
  omitted, and the next run ships the full stream again because it differs from the latest items
  (`shipper.py:195-196`).
- *An exit-3 snapshot after a complete one.* The union reads both.
- *A prune between them.* Ten snapshots are kept (`jobhunter/backup/prune.py:42, 78`). `--group-by host` is
  right: without it each snapshot is a group of its own.
- *The superset argument.* It needs a stream that only grows. I found no code that removes a manifest file:
  `migrate` rewrites missing ones from `scope` (`jobhunter/privacy/identities.py:78-94`) and the restore
  copies the union in (`restore.py:282-285`).
- *A snapshot with no folder.* Skipped by its file count; without a count it is restored and fails closed.

One sequence remains, and the code comment and the spec state it: ten incomplete snapshots in a row after
the last complete one, then a prune. That is N2, Minor.

RTO: at most ten small restic restores after a prune, and one per shipment since the last prune otherwise.
That is seconds to a few minutes, far inside 4 h.

### New findings

#### Critical

None.

#### Important

**N1. A feedback snapshot detached before the purge keeps its Adzuna-derived scores (Codex I1, partly
fixed).** `jobhunter/privacy/kinds/source_purge.py:237-243`.

- *What the fix relies on.* `_FEEDBACK` selects a row when its `job_revision_id` is tainted, its
  `llm_result_id` is a result on a tainted revision, or its `det_result_id` is an instance naming the
  source. All three keys are `ON DELETE SET NULL`
  (`jobhunter/db/migrations/versions/0008_m3_scoring.py:520-527`), and the table's own comment says the
  snapshot "survives score and job retention".
- *Failure path 1, job tombstoning.* The user labels an Adzuna job, later deactivates its golden case, and
  the job closes. Only an **active** case exempts a job (`jobhunter/privacy/kinds/job_tombstone.py:80-85`).
  After 90 days the nightly tombstone deletes the job and its revisions, and the feedback row "keeps its row
  with the job and revision NULL" (`jobhunter/privacy/deletion_graph.py:162-163`), snapshot included. The
  Adzuna purge then matches none of the three clauses.
- *Failure path 2, score history.* An Adzuna salary on a revision whose text came from another source is
  covered only by the third clause. The `score_history` rule deletes the results of an inactive tuple after
  12 months with their `det_results`, and feedback does not exempt them
  (`jobhunter/privacy/retention.py:106-115`). `det_result_id` becomes NULL and the revision is untainted,
  so the row is missed.
- *Effect.* L, M, U, coverage and confidence derived from Adzuna data stay in the database after a purge
  shown "confirmed offsite", and the export includes detached rows with their snapshot
  (`jobhunter/privacy/export.py:394-399`, `f.job_id IS NULL`). That contradicts spec L6719 and
  `docs/PRIVACY.md:140`.
- *Why the ruling does not cover it.* F-1 item 2 and spec L6713 say no row was detached "by an earlier
  purge". Tombstoning and retention detach rows in normal operation, on any deployment, before its first
  purge.
- *Fix.* Record the provenance on the row when it is written. `record_feedback` already has the revision and
  the instance in hand (`jobhunter/scoring/golden.py:319-331`): add a `sources` array to `score_snapshot`
  (the revision's lineage sources and the instance's det-input sources), and give `_FEEDBACK` a fourth
  clause on it. No migration (the column is `jsonb`) and no backfill (no deployment). Add two tests beside
  the existing ones: a label whose job was tombstoned, and one whose results the score-history rule deleted.
  Correct spec L6713's last sentence.

#### Minor

**N2. Ten incomplete snapshots, then a prune, forget the last complete one.**
`jobhunter/backup/prune.py:36-42, 78`. Documented in the code and in spec §19.5, and it needs a file that
the shipper can hash and restic cannot read ten runs in a row, so it is not a practical case. It is cheap to
close: the prune knows the latest receipt's snapshot (`manifest_shipments.restic_snapshot_id`), so skip
`forget_last` unless the newest ten include it.

**N3. The bound check runs at every `migrate`, on every manifest.** `jobhunter/privacy/replay.py:339-341`.
`_classify` runs at each deploy, and it now checks satisfied manifests too. The first release that adds a
key to a kind's `max_ids_keys` (a new table that delete everything must bound, most likely) will stop
`migrate` on every deployment that holds an older manifest of that kind, at an ordinary upgrade, not only
at a restore. Nothing fails today: a manifest holds every identity table that existed when it was computed.
*Fix:* write the rule where T31-2's is (spec §19.5, `db/compat.py`): a table created after a manifest has
no row within its bound, so its missing key reads as 0, or the release counts as a format change. This
re-states my first-round challenge; the named refusal makes the stop legible but the release rule is still
missing.

**N4. A repository reused by a new deployment is replayed into it.** `jobhunter/backup/restore.py:294-303`,
`docs/RUNBOOK.md:242-249`. `backup init` leaves an existing repository as it is. If a deployment is wiped
and started again on the same bucket, the earlier lifetime's `manifests` snapshots stay among the newest
ten until ten new shipments and a prune have passed, and a restore replays their `row_delete` operations by
id and bound against the new database. Before the fix the same happened only until the first new shipment.
*Fix:* one RUNBOOK sentence in section 3: a database that was not restored from a repository gets a new,
empty repository.

**N5. A restore test started by hand and killed hard is not cleaned up.**
`jobhunter/backup/scheduler.py:200-201`. `$JH exec backup jobhunter backup restore-test` runs beside the
scheduler. If that process is OOM-killed or killed with SIGKILL, the scheduler does not restart, so nothing
removes the scratch database and `restore_test/` until the next restart or the next monthly test. Related:
the start also removes `restore/`, including a set an operator staged there for `restore verify --phase
restored`. *Fix:* a RUNBOOK line (after a killed manual test, run it again or restart `backup`); or run the
leftovers check before each scheduled job.

**N6. Pausing the worker can freeze it inside a transaction.**
`frontend/e2e/specs/20-e18-privacy.spec.ts:196`. If `docker compose pause` lands while the worker holds
`active_config`, the api's admission waits for it and the 202 never arrives; the `afterEach` unpauses and
the spec fails. Far rarer than the race it replaces. *Fix:* if the 202 does not arrive within a few
seconds, unpause, wait and pause again.

**N7. The UI's Logs row still says "About 14 days (rotation)".** `frontend/src/i18n/en.json:1752`,
`de.json:1752`. PRIVACY.md and spec §20.1 now say rotation is by size and can be longer for a quiet
service. The row holds no personal data any more, so this is wording drift only; F-3 left it to the
reviewers. *Fix:* "Rotated by size; about 14 days at normal volume" in both languages.

**N8. Three stale sentences.**
- `jobhunter/cli.py:316`: the `restore run` help still says "the latest manifest stream".
- `jobhunter/api/routes/privacy.py:67`: the docstring still says the client address reaches a log line.
- `docs/RUNBOOK.md:566, 748`: a manual ship is told to retry on `backup_job_busy`, which section 8 now says
  it never prints.

Also under this heading: delete everything's settings reset writes `schedule_state` without the dead-man
lock (`jobhunter/settings/reset.py:82`). The seed schedule is enabled
(`jobhunter/db/seed_data/settings_v1.json:1`), so it is not a disable today; if the default ever changes,
it must take the lock as `_apply_schedule` does.

**Lock order and deadlock: none found.** A shipper holds 7261007 alone and takes neither 7261005 nor
7261006; a prune or restore test holds 7261006 and never asks for 7261007; restic's repository lock orders
a ship against a prune as before. The disabling save holds `active_config FOR UPDATE` and then waits for
7261005. Under 7261005 the pinger only reads `schedule_state` and `deadman_state` plainly, updates
`deadman_state`, and takes `worker_heartbeat FOR KEY SHARE` (`deadman.py:136-142, 449-470`); the save
touches none of them before its wait, so there is no cycle. The cost is that a save coinciding with a ping
can hold `active_config` for one send, about 30 s at worst, which F-2 accepted.

**Privacy regressions: none found.** The new log events carry ids, names and counts only
(`manifest_ship_waiting`, `manifest_stream_conflict`, `manifest_snapshot_empty`,
`restore_leftovers_removed`, `replay_bound_missing`).

### Parked list, section 2

1. **T28-7.** Unchanged; my first-round judgement stands.
2. **T31-1 (5), operator fields.** Unchanged, and still a deployment carry: the deployment step should not
   start until there is a way to fill them without committing them.
3. **T30-3 (2), E18.** Changed: fixed by B1. Acceptable for a required gate, with N6 as a known residual.
4. **T27-4 M2, index 0011.** Changed: both readers write `kind = 'purge'` as a literal
   (`jobhunter/privacy/reconcilers.py:71-81`, `jobhunter/privacy/kinds/delete_everything.py:305-308`), and
   the test now forces generic plans. Closed.
5. **Fail-open egress rule.** Judgement unchanged (acceptable); the manual check is now the right one.
6. **The bare `KeyError`.** Changed: closed by fix A item 4 and B9. N3 is what remains of my condition.

### What I ran

No Docker, no stack, no sudo, no network or paid call, no sub-agent. `.secrets/` and `.fixtures-raw/` were
not read, listed or grepped. The tree was clean before and after (`git status --short` empty); this file is
the only change.

- `uv run --group docparse pytest -W error -q -p no:cacheprovider` on `tests/unit/test_kind_bounds_declared.py`,
  `test_restic_wrapper.py`, `test_migrate_lock_key.py`, `test_compose_prod.py`, `test_host_setup_script.py`,
  `test_privacy_md.py`, `test_login_limit.py`, `test_runbook.py` and `test_spec_revision.py`: 144 passed in
  28 s.
- `npm --prefix frontend test -- PrivacySection PrivacyStatement`: 2 files, 6 tests passed.
- `git show` and `git diff` over the range for every source, deploy, script and docs file, and greps over
  `backend/src/jobhunter`, `deploy/` and `docs/` for log fields, lock keys, `max_ids` reads, schedule
  writers and stale wording.

Not run: every test that needs PostgreSQL (`test_shipper`, `test_restore_run`, `test_restore_test`,
`test_source_purge`, `test_typed_replay`, `test_export`, `test_replay`, `test_deadman_sync`,
`test_backup_scheduler`, `test_migration_0011`, `test_auth`, `test_reauth`), the crash and stack tests, the
smoke and Playwright. I did not confirm any RED run myself.
