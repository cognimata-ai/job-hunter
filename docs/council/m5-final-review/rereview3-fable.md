# M5 final re-review, round 3 — Claude Fable

Range `1de9493..821d21d` (fix D, seven commits), read against Ruling F-5, the two round-2 re-reviews, the
implementer's report and spec revision 3.7 as edited in the range.

### Verdict

**APPROVE.** Fix D closes R2-C1. The prune now reads the latest receipt, lists the snapshots and forgets
inside the shipment lock, so Codex's interleaving cannot occur, and a receipt whose snapshot restic does not
list is removed instead of being trusted for ever. I found no interleaving that lets a restore omit a
manifest a standing receipt names, except one residual that needs four independent events inside five
minutes (N-1, Minor). The three mechanism choices are sound. M-1 to M-4 are fixed. I raise five Minors; none
blocks.

### Findings mapped

| Finding | Status | Evidence |
|---|---|---|
| **R2-C1** (Codex, Critical): a shipment receipted between the prune's receipt read and its forget is forgotten | **Fixed** | `_prune_manifests` takes `ship_lock(cfg, holder="prune")` and holds it over the receipt read, the listing and the ids forget (`backend/src/jobhunter/backup/prune.py:154-182`), inside `backup_job_lock` (`prune.py:99`). It is released before the sets' `forget --prune` (`prune.py:197-198`). A shipper takes the same lock before it lists the stream (`backup/shipper.py:235-236`), so a ship started during the prune waits and then lists. Codex's concern (1) is answered too: a receipt restic does not list is removed (`prune.py:125-146, 162-177`), so the shipper's unchanged-stream check (`shipper.py:238`) stops trusting it. Regression tests: `tests/unit/test_restore_run.py` (the interleaving, with `FakeRepository.shift`, `tests/support/backups.py`), `tests/integration/test_backup_set.py:650, 704`. I did not run them (PostgreSQL container); the report gives red then green for each. |
| **M-1** (the added-key rule is false for a backfilled table) | **Fixed** | Spec §19.6 (`docs/superpowers/specs/2026-10-16-job-hunter-design.md:6449`), `backend/src/jobhunter/db/compat.py:46-55`, `backend/src/jobhunter/privacy/purge.py:23-31`: the rule holds only for a table created empty; a backfill makes the key a format change, or the table cascades from a bounded one. All three places agree. |
| **M-2** (three forms pass the static guard) | **Fixed** as asked | `is_max_ids` knows `.get("max_ids", …)` (`tests/unit/test_kind_bounds_declared.py:342-353`), `holding` looks through `:=` (`:421-426`), `computed_keys` fails a non-literal key of `content` or `scope` (`:356-372`). The four planted cases pass (run, green). My probe confirms the three forms and finds three further ones still open (N-5). |
| **M-3** (a skipped manifests forget is invisible) | **Fixed** | The report carries `manifests_skipped` and `receipts_removed` (`prune.py:206-211`), with values `no_receipt`, `receipt_not_listed` or `null` (`prune.py:115, 159, 175-177, 182`). RUNBOOK section 8 gives the query and the meaning (`docs/RUNBOOK.md:552-561`). The health page shows no run report (`observability/backup_health.py:39`), so the `psql` query is the right place. One sentence is slightly off (N-3). |
| **M-4** (a lock lost before restic starts) | **Fixed**, one residual | `HeldShipLock.confirm` checks `pg_locks` on the lock's own connection (`shipper.py:72-77, 107-117`); `_backup` calls it after the list file is written and before `restic.backup` (`shipper.py:222-227`). The prune does the same before its forget and before the removal (`prune.py:163, 180`). The receipt write is not re-confirmed (N-1). |

### Question 1: R2-C1, now D1

**Can a restore still omit a manifest a successful receipt named?** Not through any path I could build,
with the one exception of N-1. The argument:

- *The anchor is the newest receipt by id* (`prune.py:74`), and receipts are written under the lock in the
  order of the lists they hold (`shipper.py:235-249`). The stream only grows, so the anchor's snapshot holds
  every file any standing receipt names.
- *Nothing is receipted between the choice of the anchor and the forget.* Both are inside the lock
  (`prune.py:154-182`). A ship that starts meanwhile polls `pg_try_advisory_lock` (`shipper.py:181-185`).
- *Clock steps.* The forget is by time (`prune.py:178`), the anchor by id. A receipted snapshot with a later
  time and a lower id is kept (it is not older). A receipted snapshot with an earlier time and a lower id is
  forgotten, and the anchor covers it. No receipted snapshot has a higher id than the anchor. So a stepped
  clock only changes how many redundant snapshots are kept.
- *Partial snapshots* (restic exit 3) have no receipt (`shipper.py:241-249` runs only after `restic.backup`
  returns). If a clock step puts one before the anchor it is forgotten, and no receipt named its extra
  files. Those manifests stay `completed`, and the next ship uploads them.
- *A complete snapshot whose receipt insert failed* (connection lost after the upload) is the same case: no
  receipt, so no claim.
- *Lost lock sessions.* Before the upload, the forget and the removal, `confirm` fails the job
  (`shipper.py:224`, `prune.py:163, 180`). After the upload has started, see N-1.
- *Restore tests.* A restore test holds 7261006 (`backup/restore_test.py:286`), so it never overlaps a
  prune. It may overlap a ship, which only adds a snapshot to the union it reads.
- *Receipt removal against the reconciler.* `manifest_shipping` marks a manifest with
  `shipment_id = <receipt>` in one `UPDATE` (`privacy/reconcilers.py:97-107`). The prune locks the receipt
  row `FOR UPDATE` (`prune.py:75-77`). Three orders:
  - the reconciler committed first: the prune's `UPDATE` sees the manifest `shipped` and returns it;
  - the reconciler's foreign-key check holds its `KEY SHARE` lock first: the prune's `FOR UPDATE` waits for
    that commit, and its next statement (READ COMMITTED, a new snapshot) sees the manifest `shipped`;
  - the prune holds the row first: the reconciler's foreign-key check waits, finds the row deleted and
    fails, so its transaction rolls back and the manifest stays `completed`.

  The foreign key (`db/migrations/versions/0010_m5_operations.py:109`) makes a dangling mark impossible in
  every order. I found no deadlock: the prune never waits on a manifest row the reconciler holds, because
  that row is not yet `shipped` in the prune's snapshot.

**Can the removal itself lose coverage?** No. The removal run forgets no `manifests` snapshot
(`prune.py:162-177` returns before line 178), and removing a receipt only withdraws a claim. If restic's
listing were short and the snapshot did exist, the cost is a re-upload and alerts, as the report's concern 2
says. A failed listing cannot be mistaken for a short one: a non-zero exit or unparsable output raises
`ResticError` (`backup/restic.py:139-152`) and the run fails with nothing removed. The ids compare like for
like: both are restic's full 64-hex id (`restic.py:62, 105-116, 150`).

One consequence to know (N-4): the walk continues below the latest receipt until restic lists one
(`prune.py:130-132`). After any earlier prune, the receipts below the previous anchor name snapshots that
prune forgot on purpose. So if every snapshot since the last prune is missing, the walk removes every
receipt. That is truthful (nothing offsite confirms those manifests then), and the next ship repairs it.

**Is the lock order sound?**

- 7261006, then 7261007: only the prune (`prune.py:99, 154`). The shipper names no job lock
  (`tests/unit/test_migrate_lock_key.py`, run, green), and `set` and `restore-test` never take 7261007
  (my grep over `backend/src/jobhunter` finds `ship_lock` only in `shipper.py` and `prune.py`).
- A shipper holding 7261007 waits only for restic. The prune holds 7261007 only while no shipper does, and
  releases it before its long `forget --prune`, so a ship is never held behind that.
- 7261005 is taken by the worker's pinger and, as a transaction lock, by a settings save
  (`settings/save.py:353`). Neither process takes 7261006 or 7261007, and the backup container never takes
  7261005. No cycle exists.
- The registry's sentence claims more than is true (N-2).

One cost, accepted: a prune that waits for a hung ship holds the job lock meanwhile, so a set would report
`backup_job_busy`. A ship is bounded by restic's own timeouts and `--retry-lock 30m`.

### Question 2: the three mechanism choices

**The `shipped → completed` edge: sound.** `db/transitions.py:723-735`.
- `clear_on_exit` clears `shipped_at` and `shipment_id`, which the §7.14 CHECK requires
  (`0010_m5_operations.py:93`).
- The only caller is the prune (`prune.py:135-142`). My grep finds no other transition of
  `deletion_manifests` out of `shipped`.
- The database matrix now permits the edge for `jobhunter_app` too. Nothing in the app takes it, and taking
  it only makes a deletion look less confirmed, which alerts. It cannot hide a deletion.
- The edge widens the matrix, so it is not a destructive revision under §19.6.
- Readers follow: the reconciler's overdue query and `backup_health` read `completed` as unshipped
  (`privacy/reconcilers.py:63-66`), and the partial index covers every status but `shipped`
  (`0010_m5_operations.py:185`).

**`first_entry_kept`: sound.** `db/transitions.py:60, 67-68, 894-895`.
- `coalesce(completed_at, now())` sets the time on the first entry (`executing → completed`, column NULL)
  and keeps it on the return from `shipped`. The alert therefore judges the deletion's real age, which is
  the point: a deletion from last month whose offsite copy is gone should alert on the next tick, not in an
  hour.
- `__post_init__` refuses a kept state with no changed-at column. Unit tests cover both (run, green).
- `shipped_at` becomes the time of the second shipment. That is the truthful value.

**The backup role's new grants: sound, and as narrow as this mechanism allows.** `db/migrate.py:95-98,
151-156`.
- `DELETE` on the two receipt tables only. Column-level `UPDATE` on exactly the four columns the helper
  assigns. `completed_at` is needed because the helper writes the `coalesce` assignment.
- *Could a compromised backup container now erase deletion evidence?* No more than before.
  - It cannot delete a manifest row or change `scope`, `file_sha256`, `replay_log` or `request`
    (`tests/integration/test_migration_0010.py`, the two refusals added in `821d21d`).
  - `status` changes pass the `assert_transition` trigger (`0010_m5_operations.py:213`), so only matrix
    edges are possible. It cannot complete an `executing` manifest, since that needs `file_sha256`
    (`0010_m5_operations.py:92`), which it cannot write.
  - It can un-ship manifests and delete receipts. Both make deletions look *less* confirmed and raise
    `privacy.manifest_unshipped`.
  - It can move `completed_at` forward on a `completed` manifest and so delay that alert. This is new, but
    the role could already forge a receipt (it has `INSERT` on both receipt tables, and the reconciler then
    marks the manifest `shipped`), which is the stronger form of the same lie. The same container also
    holds the repository's credentials. So the trust boundary has not moved.
- A narrower form exists (a `SECURITY DEFINER` function owned by the migrator), but it buys nothing against
  a role that can already forge receipts. I do not ask for it.

### Question 3: D2–D5

- **D2: correct.** The query matches a one-key advisory lock exactly: `classid` high bits, `objid` low bits,
  `objsubid = 1`, this backend, this database (`shipper.py:72-77`). `pg_locks` is readable by every role. A
  closed connection raises `psycopg.OperationalError`, which becomes `ShipLockLost` (`shipper.py:110-114`).
  The list file is removed on that path (`shipper.py:226-227`). The scheduler's job boundary catches the
  error and runs the next job (`backup/scheduler.py:123`). `ShipLockLost` has no CLI code of its own
  (`unexpected` with the class name); that is readable and I accept it.
- **D3: correct.** See M-3 above. `receipt_not_listed` is reported only when a removal ran, and the removal
  always removes at least the latest receipt, since the locked re-read sees the same row under the lock.
- **D4: correct.** See M-1 above.
- **D5: correct for the three forms.** See M-2 above and N-5.

### New findings

**Critical:** none.

**Important:** none.

**Minor**

**N-1. The shipper writes its receipt without confirming the lock again.**
`backend/src/jobhunter/backup/shipper.py:241-249`.
- *Path.* Shipper A lists `{M1}`, confirms, and restic starts. A's lock session dies during the upload. The
  clock is stepped back. Shipper B takes the lock, ships `{M1, M2}` (timestamped before A's snapshot) and
  writes its receipt; the reconciler marks M2 shipped. A finishes and writes its receipt, the newest by id.
  A prune in the next five minutes anchors on A's `{M1}` and forgets B's snapshot as older. Until the next
  ship (which sees the stream differ from A's items and re-uploads), no snapshot holds M2.
- *Why Minor.* It needs a session lost mid-upload, a backward clock step, the weekly prune and a host loss,
  all inside one five-minute interval. Without the clock step B's snapshot is the later one and is kept.
- *Fix.* Call `held.confirm()` once more between `_backup` and the receipt transaction. A then writes no
  receipt, B's stays the latest, and A's snapshot is only an extra one.

**N-2. The registry's lock-order sentence claims more than holds.** `backend/src/jobhunter/db/migrate.py:116-119`.
- "A process holds two of these at once only as `LOCK_ORDER` lists them" follows the list of all seven
  keys. The worker holds 7261001 for its lifetime (`worker/lock.py:1-3`) and takes 7261005 inside it
  (`observability/deadman.py:434-441`), and takes the transaction locks 7261004 as well. No cycle results,
  because nothing waits for 7261001 while holding another key. But the sentence is false as written, and a
  later reader may trust `LOCK_ORDER` as complete.
- *Fix.* Either scope the sentence to the backup container's keys (7261006, 7261007), or add the worker's
  pairs to `LOCK_ORDER`.

**N-3. "Either clears by itself" is not true of `no_receipt` on an installation with no deletion yet.**
`docs/RUNBOOK.md:558-561`.
- A ship of an empty stream writes no receipt (`shipper.py:238-240`), so every weekly prune reports
  `no_receipt` until the first purge. That is harmless, but the sentence tells the operator to expect it to
  clear.
- *Fix.* "`no_receipt` stays until the first deletion has shipped; `receipt_not_listed` clears at the next
  shipment."

**N-4. A restore can bring back a receipt the prune had removed, and the repair then waits for the weekly
prune and alerts once per manifest.** `backend/src/jobhunter/backup/shipper.py:238`,
`backend/src/jobhunter/backup/prune.py:130-146`, `docs/RUNBOOK.md:563-571`.
- *Path.* A backup set is taken while receipt R3 stands. R3's snapshot is lost; the prune removes R3; the
  next ship uploads the same stream as R4. Later that set is restored. The database's latest receipt is R3
  again, and the restored stream equals R3's items, so every ship reports `manifests_unchanged`. The next
  prune finds R3 unlisted and walks down. The receipts below it name snapshots earlier prunes forgot, so it
  removes all of them. Every manifest returns to `completed`, and the reconciler opens one critical alert
  per manifest (`privacy/reconcilers.py:109-119`). The next ship repairs it within five minutes.
- No coverage is lost at any point: R4's snapshot holds every file throughout. The cost is a burst of
  critical alerts up to a week after a restore, with no explanation in the RUNBOOK's restore section. The
  same burst follows any loss of every snapshot since the last prune, where it is deserved.
- *Fix.* One RUNBOOK sentence in "A false receipt" and in section 9: after a restore of a set older than a
  removed receipt, expect `receipts_removed` at the next prune and one alert per deletion, resolving by
  itself. Optionally, run `backup prune` as the last step of the restore procedure so the repair happens
  while the operator is watching.

**N-5. Three further forms still pass the static bounds guard.**
`tests/unit/test_kind_bounds_declared.py:356-372`. Planted in `JobTombstone.execute`, each returns no
violation in my probe:

```python
c = content; k = "max_ids"; c[k]["planted"]     # a computed key on an alias of the content
content.__getitem__("max_ids")["planted"]
for k, v in content.items(): v["planted"]
```

`computed_keys` judges only the names `content` and `scope`, not a name bound to one. The same probe
confirms `c = content; c["max_ids"]["planted"]`, `(l := content.get("max_ids"))["planted"]`,
`content.get("max_ids").get("planted")` and `self.scope[k]` are all caught. No real kind uses the three
open forms, and a static check of this kind cannot be complete. *Fix, if wanted:* fail any use of `content`
or `scope` other than a literal subscript or a literal `.get` (the rule `unhandled_uses` already applies to
the bounds themselves).

**Drift between the spec, the RUNBOOK, PRIVACY.md and the code.** Apart from N-2 and N-3 I found none.
Checked: the role table (spec L1268 against `migrate.py:95-98, 151-156`), the state machine (spec L3141
against `transitions.py:723-735`), the two `privacy.manifest_unshipped` rows (spec L5756, L6494, RUNBOOK
L406), §19.5's backup-jobs and Retention paragraphs (spec L6358, L6432 against `prune.py` and `shipper.py`),
RUNBOOK section 8 (L530-571) and `docs/PRIVACY.md:129`. PRIVACY.md's new sentence is accurate at the level a
user reads it.

### What I ran

No Docker, no stack, no sudo, no network or paid call, no sub-agent. `.secrets/` and `.fixtures-raw/` were
not read, listed or grepped. `git status --short` was empty before this file was written, and this file is
the only change in the tree.

- `git log --oneline 1de9493..821d21d`, and the diff of the range for every non-test file, plus the test
  diffs for `tests/support/backups.py`, `tests/stack/test_prod_backup.py`,
  `tests/unit/test_migrate_lock_key.py` and `tests/integration/test_migration_0010.py`.
- Read in full at `821d21d`: `backup/prune.py`, `backup/shipper.py`; in part: `db/transitions.py`,
  `db/migrate.py`, `privacy/reconcilers.py`, `backup/restic.py`, `backup/scheduler.py`,
  `observability/deadman.py`, migration 0010.
- `uv run --offline --group docparse pytest -W error -q -p no:cacheprovider
  tests/unit/test_kind_bounds_declared.py tests/unit/test_migrate_lock_key.py
  tests/unit/test_transition_registry.py`: 90 passed. None of the three uses a database fixture; I checked
  before running.
- One in-process probe, a scratch script outside the repository, which plants seven forms in
  `JobTombstone.execute` through the test module's own `planted`, `bound_reads` and `violations`: four
  caught, three not (N-5).
- Not run: `tests/unit/test_restore_run.py` and every integration, crash and stack test (each starts a
  PostgreSQL container or a stack), Playwright, and any red run against the pre-fix code. For those I rely
  on reading the code and on the implementer's reported red and green runs. The reconciler-against-removal
  analysis in question 1 is from PostgreSQL's foreign-key locking rules, not from a run.
