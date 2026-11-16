# M5 final re-review, round 4 — Claude Fable

Range `5e2bc85..ac20fd3` (E2 `52e2fbb`, E1 `d932ccd`, E3 `7fefe5b`, harness `ac20fd3`). Read-only; no Docker,
no stack, no network. I did not read the other reviewer's round-4 file.

### Verdict

**APPROVE.** Fix E closes both blocking findings of round 3. A receipt now exists only if the session that
took lock 7261007 held it without a break from the stream listing to the receipt's COMMIT, and the prune's
read, decision and removal have the same property; I could not build an interleaving in which a restore omits
a manifest a committed receipt named. The backup role holds no UPDATE on `deletion_manifests`, and the
`SECURITY DEFINER` function does one thing, `shipped → completed` for one row, with a pinned path and a
qualified table. Migration 0012 is a sound expand step with floor 0011. N-1 to N-5 are fixed. I raise five
Minors, none of which loses data or blocks.

### Findings mapped

| Finding | Status | Evidence |
|---|---|---|
| **R3-C1** (receipt written off the lock session) | **Fixed** | The receipt is one transaction on the lock's own connection: `_receipt` uses `held.transaction()` (`backend/src/jobhunter/backup/shipper.py:246-258`), which wraps `self.conn.transaction()` and turns an `OperationalError` on a broken or closed connection into `ShipLockLost` (`shipper.py:131-144`). The prune reads the latest receipt (`backend/src/jobhunter/backup/prune.py:173-177`) and removes false ones (`prune.py:152-170`) the same way; it lists restic once (`prune.py:190`), walks only `id <= latest` (`prune.py:90-92, 161`), and nothing re-acquires the lock. Argument below. |
| **R3-I1** (backup role's UPDATE grant) | **Fixed** | `apply_explicit_grants` revokes every UPDATE of the role on the table, columns included (`backend/src/jobhunter/db/migrate.py:167-168`), and grants only EXECUTE on the function, revoked from PUBLIC (`migrate.py:169-171`). The function: `backend/src/jobhunter/db/migrations/versions/0012_m5_unship_manifest.py:50-61`. The prune calls it (`prune.py:96-100, 166`). Privilege regressions as the backup role, each 42501: `tests/integration/test_migration_0012.py:133-147` (read, not run). |
| **N-1** (receipt without a second confirmation) | **Fixed**, by E1 | A shipper whose session ended cannot BEGIN or COMMIT on it (`shipper.py:137-144`), so B's receipt stays the latest. Regression `tests/integration/test_shipper.py`, `…_writes_no_receipt[another_shipper_ships]` (read, not run). |
| **N-2** (`LOCK_ORDER` sentence) | **Fixed** | `migrate.py:116-122` now states the worker's pairs (7261001 with 7261005 and 7261004) and scopes `LOCK_ORDER` to the backup container's keys. True as written; one pair on another database is unmentioned (M4-4). |
| **N-3** (`no_receipt` "clears by itself") | **Fixed** | `docs/RUNBOOK.md:558-562`: it stays until the first deletion has shipped; `receipt_not_listed` clears at the next shipment. Matches `shipper.py:281-283` and `prune.py:186-188`. |
| **N-4** (stale receipts after a restore) | **Fixed** | `docs/RUNBOOK.md:574-582` describes the case, the three signs and that nothing is lost; section 9 step 5 points to it (`RUNBOOK.md:705-707`). The narrow condition (a set older than a removed false receipt) is the right one: after any other older restore the stream differs from the latest receipt's items and the next ship writes a fresh receipt at once. |
| **N-5** (three guard forms) | **Fixed** | `content_names`, `given_content` and `content_uses` (`tests/unit/test_kind_bounds_declared.py:372-468`) replace `computed_keys`; the three plants fail the check (`:800-822`); green here. The widened rule (identity tests, conditional branches) is sound in my probe; one form stays open (M4-2). |

**Question 1: can a restore still omit a manifest a successful receipt named?** No. The argument:

- *A receipt implies an unbroken hold.* A session advisory lock leaves a live session only by an explicit
  unlock, and the code unlocks only in `ship_lock`'s `finally` (`shipper.py:213-218`). So a COMMIT accepted on
  that session proves the lock was held from `_stream` (`shipper.py:279`) through the upload to the commit.
  A session that died cannot commit; the open transaction dies with it. A COMMIT that the server applied but
  whose reply was lost leaves a receipt that was still written under the lock, which is consistent.
- *Lock lost during an upload.* The shipper's upload finishes, `_receipt` fails, `ShipLockLost`, no row
  (`shipper.py:284-285`). Its snapshot never gains a receipt, so forgetting it, or keeping it, changes nothing
  a receipt claims.
- *Lock lost after the prune's confirmation.* The forget passes `older`, built only from the listing of
  `prune.py:190` (`prune.py:208-211`). A shipment that lands afterwards is not in that listing. Every listed
  snapshot older than the anchor is either receipted with a lower id (the anchor holds its files, since
  shipments are serialised and the stream only grows) or has no receipt and, by the first point, never gets
  one. The anchor itself is never in `older` (strict `<`).
- *Lock lost before the listing.* A shipment can then be in the listing. On the forget path `confirm()` fails
  (`prune.py:210`); on the removal path the transaction fails (`prune.py:160`). Nothing is forgotten or
  removed.
- *Clock steps.* Time is used only to pick `older` among listed snapshots against a listed, receipted anchor.
  A wrong time can keep too much, or forget a snapshot the anchor covers. It cannot forget the anchor.
- *Partial snapshots.* They have no receipt. This range changes nothing for them.
- *Concurrent ships and prunes.* Two live lock sessions cannot coexist; a dead one writes nothing. A second
  prune needs the job lock as well.
- *The pre-upload read on the engine (concern 6/7).* Safe. While the lock session lives no one else writes
  or removes a receipt, so the read (`shipper.py:223-226, 281`) is exact. Once it has died the shipper either
  proceeds to `confirm()` and stops (`shipper.py:266`), or returns "unchanged" on a receipt that was committed
  when read. That last path writes nothing; see M4-3 for its one cosmetic effect.

**Does a forget ever pass an id that was not in the listing taken under the lock?** No: `prune.py:208` is the
only source of ids, and `snapshots` is assigned once at `prune.py:190`.

**Question 2: the function.**

- *Owner.* The migrator in production (it runs the migration, and `restore run` loads the dump as the
  migrator with `--no-owner`, `backend/src/jobhunter/backup/restore.py:633-634`). The catalogue test checks
  owner, `prosecdef`, `proconfig` and ACL.
- *`search_path`.* `public, pg_temp` (`0012…py:52`): `pg_catalog` is searched first implicitly, `pg_temp`
  last, and functions and operators are never resolved in `pg_temp`. The path must include `public` because
  `assert_transition` reads `transition_matrix` unqualified
  (`backend/src/jobhunter/db/migrations/versions/0002_m0_transition_matrix.py:34`). The trigger still runs and
  still checks the edge.
- *Arguments.* One `bigint`, used only as `t.id = unship_manifest.manifest_id`, qualified against a column
  clash (`0012…py:57`). No dynamic SQL. NULL matches no row and returns false.
- *Other states.* `AND t.status = 'shipped'` (`0012…py:57`): `executing`, `completed` and an unknown id change
  nothing and return false. `completed_at` is kept by the `coalesce`.
- *Least privilege.* Yes for manifests: the role's only write is this one edge. It can un-ship a manifest
  whose receipt is true; the reconciler ships it again on its next tick while the receipt stands
  (`backend/src/jobhunter/privacy/reconcilers.py:95-106`), so the effect is at most a flapping alert.
- *Forgery.* Through the function nothing is lost or hidden. The role's remaining powers are unchanged by
  this range; see M4-5.
- *Expand step, floor 0011.* Sound. 0012 adds one function; 0011's code ignores it. The unit test pins head
  and floor (`tests/unit/test_compat_floors.py:68-77`, green here).
- *Concern (1), the rollback re-grant.* Acceptable. 0011's prune needs the column grant, so 0011's `migrate`
  must restore it; that is what a floor of 0011 means. No release ever ran 0011's code, the next upgrade
  revokes it (`migrate.py:168`), and the spec and the migration say so (spec L6336, `0012…py:28-31`).

**Question 3: E3.** Covered in the table. On the widened guard rule (concern 7/8): the identity-test and
conditional-expression allowances do not open a path. In my probe an alias through `x = content if c else
None`, a walrus inside `is not None`, a renamed parameter, a tuple unpack and a chained alias are all followed,
and a content placed in a list, tuple, `or`, lambda, nested function, `with` or `for` fails as unhandled.

**Question 4: drift and head references.** Spec L1268, L2808-2822, L3157, L6336, L6374, L6403 and L6448 agree
with `migrate.py`, `prune.py`, `shipper.py` and 0012. The spec's function text equals the migration's apart
from comments. Head references to 0012: `scripts/build-prev-image.sh:24`, `tests/stack/test_prod_drills.py:87-89`,
`.github/workflows/prodstack.yml:10, 52`, `docs/RUNBOOK.md:825`, `tests/unit/test_compat_floors.py:75`. No
stale "0011 is the head" remains in `backend/`, `tests/`, `scripts/` or the RUNBOOK. PRIVACY.md names none of
this and needs no change.

### New findings

**Critical:** none.

**Important:** none.

**Minor**

**M4-1. A database error on the lock session prints `unexpected`, where the CLI promises `database`; and
`ShipLockLost` has no operator note.** `backend/src/jobhunter/cli_backup.py:23-27, 182, 191-192`;
`backend/src/jobhunter/backup/shipper.py:140-144, 251-257`.
- *Path.* The receipt write moved from the SQLAlchemy engine to the psycopg lock connection. A psycopg error
  there (not a `SQLAlchemyError`) falls through to the generic handler, so `backup ship` prints
  `{"error": "unexpected", "error_class": "…"}`; a lost lock prints `error_class: ShipLockLost`. The module
  docstring still says a database error prints `database`. The RUNBOOK never mentions `ShipLockLost` or
  `prune:ShipLockLost`, which an operator will now see after any PostgreSQL restart during a job.
- *Effect.* None on data; the exit status is 1 either way.
- *Fix.* Catch `ShipLockLost` (`ship_lock_lost`) and `psycopg.Error` (`database`) in `cli_backup`, and add
  one RUNBOOK sentence in section 8: the job wrote nothing and the next run repeats it.

**M4-2. A content passed to a `*args` parameter of a module function is not followed by the guard.**
`tests/unit/test_kind_bounds_declared.py:401-409, 458-463`.
- *Path.* `_helper(content)` with `def _helper(*a): k = "max_ids"; a[0][k]["planted"]` returns no violation
  in my probe. The call counts as "an argument of a function of the module" and is allowed, but
  `given_content` maps arguments only to named positional parameters, so nothing holds the content in the
  callee. A read through `manifest.__dict__["scope"][k]` passes too.
- *Effect.* None today; no kind uses either form.
- *Fix, if wanted.* Treat an argument that binds to no named parameter of the callee as an unhandled use.

**M4-3. The "unchanged" return does not confirm the lock.** `backend/src/jobhunter/backup/shipper.py:281-283`,
docstring `shipper.py:9-10`.
- *Path.* A's lock session ends before its read. A prune holds the lock and is about to remove the latest
  receipt as false. A reads that receipt on the engine, finds the stream equal, and exits 0 "unchanged". The
  prune then removes the receipt.
- *Effect.* One `backup ship` reports the stream shipped a moment before it is not. Nothing is written or
  lost, and the next ship, within 5 minutes, uploads. The sentence "a `backup ship` that exits 0 shipped, or
  found shipped, every file" is the only thing affected.
- *Fix, if wanted.* Read `_LATEST_ITEMS` in `held.transaction()`; the read then fails with the session.

**M4-4. The lock note omits one pair, on another database.** `backend/src/jobhunter/db/migrate.py:116-122`;
`backend/src/jobhunter/backup/restore_test.py:188, 286`.
- The restore test holds 7261006 on the production database while its in-process `migrate` takes 7261003 on
  the scratch database (`migrate.py:280`). Advisory locks are per database, and nothing else connects to the
  scratch one, so no cycle is possible. The note is true as scoped; a half sentence would make it complete.

**M4-5. The spec's forgery sentence covers removals only.** Spec L1268.
- The role still holds INSERT and UPDATE on `manifest_shipments` and `manifest_shipment_items`
  (`migrate.py:94`), as it must to write receipts. A compromised backup container can therefore insert a
  receipt that names a `completed` manifest, and the reconciler marks it `shipped` with no offsite copy.
  That is unchanged since 0010, outside this range, and inherent to the shipper (which also holds the
  repository's credentials). But L1268 now reads as a full account of what a compromised container can forge.
- *Fix.* One clause: a forged receipt can make a deletion look confirmed; the container is trusted for that.

### What I ran

- `git log`, `git show` and `git diff` over `5e2bc85..ac20fd3`; the working tree equals `ac20fd3` for every
  file in the range.
- Read in full: `backup/shipper.py`, `backup/prune.py`, migration 0012, the diffs of `db/migrate.py`,
  `db/transitions.py`, the spec, the RUNBOOK, the scripts, the workflow and every test in the range; and, for
  context, `privacy/reconcilers.py:60-120`, `backup/restore.py` (list filter, `load_dump`), `cli_backup.py`,
  migration 0002's trigger, 0010's tables.
- `uv run --group docparse pytest -W error -q -p no:cacheprovider tests/unit/test_kind_bounds_declared.py
  tests/unit/test_compat_floors.py tests/unit/test_migrate_lock_key.py tests/unit/test_workflows_m5.py
  tests/unit/test_spec_revision.py tests/unit/test_transition_registry.py`: 137 passed.
- A scratch probe outside the repository, 23 forms planted in `JobTombstone.execute` through the guard's own
  `planted` and `bound_reads`: 21 caught or correctly accepted, 2 open (M4-2).
- **Not run:** every integration, crash and stack test, and `tests/unit/test_restore_run.py`. Their fixtures
  start a PostgreSQL container (`tests/support/pg.py:121-139`). The E1 and E2 regressions are therefore
  verified by reading only; the implementer's report gives their red and green runs.
