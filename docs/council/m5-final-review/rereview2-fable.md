# M5 final re-review, round 2 — Claude Fable

Fix range `ef54794..887c6f6` (fix C, ten commits), read commit by commit against both first re-reviews
(`rereview-codex.md`: R-C1, R-M1, R-M2; `rereview-fable.md`: N1 to N8), Rulings F-1 to F-4 and
`final-fix-c-report.md`. Paths under `backend/src/jobhunter/` are written `jobhunter/…`.

### Verdict

**APPROVE.** Every finding of both first re-reviews is fixed as Ruling F-4 chose, and I found no Critical or
Important defect in the fix range. The two that blocked are closed. The prune now anchors on the latest
receipt, and the anchor is never forgotten, so no count of incomplete snapshots can evict proven coverage
(R-C1, N2). A label records its sources when it is written, and the source purge freezes every row that
names the source, so retention can no longer detach a snapshot out of the purge's reach (N1). Four Minor
findings follow; none blocks, and none is a failure on today's code paths.

Limits of this verdict:

- I could not re-run the integration, crash, stack or E2E tests, which need PostgreSQL in Docker or a stack.
  Their RED and GREEN outputs are the implementer's evidence, checked here by reading each test against the
  code.
- One deviation from my instructions, stated under "What I ran": one unit file I ran starts a throwaway
  PostgreSQL container through the test harness.

### Findings mapped

| Finding | State | Evidence |
|---|---|---|
| **R-C1** (Codex, Critical) and **N2** (Fable): count-based prune can evict the last complete snapshot | **Fixed** | `jobhunter/backup/prune.py:72-88` reads the newest `manifest_shipments` row (`:49`), finds its snapshot in `restic snapshots --tag manifests` and forgets by id only those with `time <` the anchor's (`:85`); none without a receipt (`:78`) or when the receipted snapshot is not listed (`:83`). `Restic.forget_snapshots` refuses anything but 64-hex ids (`jobhunter/backup/restic.py:221-225`). Codex's exact sequence is `tests/unit/test_restore_run.py:542-585`; I ran it, green. It fails on the count-based prune by construction: ten partials would be the newest ten, and both receipted snapshots would go. "Newest 10" is gone from the spec (L6412, L6432, L6545), `docs/PRIVACY.md:83`, `docs/RUNBOOK.md:528-530` and both UI strings (`frontend/src/i18n/en.json:1760`, `de.json:1760`); my grep for `newest 10`, `keep-last`, `forget_last`, `MANIFESTS_KEPT` and `keep_manifests` over source, tests, docs and deploy finds none. See "C1 in detail". |
| **N1** (Fable, Important): detached feedback keeps Adzuna-derived scores | **Fixed** | Writer: `jobhunter/scoring/golden.py:339-341` records the sorted union of the result's revision lineage (`:233`) and the instance's `det_inputs` sources. Purge: fourth clause `score_snapshot -> 'sources' @> [source]` (`jobhunter/privacy/kinds/source_purge.py:246`), in the same `_FEEDBACK` freeze, so the live redaction, the replay and the typed postcondition cover the row through the existing `field_redact`. Export reads the column (`jobhunter/privacy/export.py:398`). See "C2 in detail". |
| **N3** (Fable): no release rule for a new `max_ids` key | **Fixed**, one gap in the rule's wording (M-1) | `KindProcedure.max_ids_keys_added` (`jobhunter/privacy/purge.py:388`); `register_kind` refuses a key declared both ways (`:431-432`); `required_bounds` requires only `max_ids_keys` (`jobhunter/privacy/replay.py:315-320`). The rule is written beside T31-2's: spec §19.6 (L6449) and `jobhunter/db/compat.py:46-51`. Enforced statically: an added key read other than as `.get(key, 0)` is a violation (`tests/unit/test_kind_bounds_declared.py:483-490`), with planted cases both ways (`:580-593, 625-631`). Run, green. |
| **R-M1** (Codex): static guard misses aliases | **Fixed** as asked; other entry forms remain (M-2) | `bindings` yields `=`, annotated `=` and `:=` (`test_kind_bounds_declared.py:274-287`); `bounds_names` runs to a fixed point (`:290-303`); `unhandled_uses` fails every use but a subscript, a `.get`, a call argument or an alias (`:306-330`). Both of Codex's planted forms now fail (`:595-609`). My own probe confirms those and eleven more forms; three still pass (M-2). |
| **R-M2** (Codex) and **N7** (Fable): UI Logs row | **Fixed** | `en.json:1752` "Rotated by size; about 14 days at normal volume", `de.json:1752`; pinned by `PrivacySection.test.tsx` and `tests/unit/test_privacy_retention_rows.py` (both run, green). Agrees with `docs/PRIVACY.md:77`. |
| **N4** (Fable): repository reused by a new deployment | **Fixed** | `docs/RUNBOOK.md:249-253`: a database not restored from a repository gets a new, empty one; `backup init` must print `{"created": true}`. C1 also shortens the exposure: the first receipt of the new lifetime makes every earlier snapshot "older", so the next prune forgets them. |
| **N5** (Fable): killed manual restore test | **Fixed** | `run_restore_test` calls `remove_leftovers(cfg, restore_staging=False)` under the backup jobs' lock, before its run row (`jobhunter/backup/restore_test.py:286-287`). RUNBOOK section 8 (`:544-548`) and the `restore verify` note (section 9). See question 2 for what it removes. |
| **N6** (Fable): pause can freeze the worker inside a transaction | **Fixed** | `pauseWorkerOutsideTransactions` (`frontend/e2e/support/stack.ts:220-238`). See "C8 in detail". |
| **N8** (Fable): stale sentences, and the reset's lock | **Fixed** | Lock moved into the shared `reset_schedule` (`jobhunter/settings/save.py:352-353`), so delete everything's reset takes it when its default disables. `cli.py` help, `api/routes/privacy.py:67-68` and the three RUNBOOK lines (`:534, 577, 762`) corrected. |

**C1 in detail.** The safety argument has one load-bearing step: the anchor is never forgotten, and the
anchor holds every manifest any receipt named. I tried each sequence the brief lists.

- *Partial snapshots after a receipt, any number, then prunes.* They are newer than the anchor and kept.
- *Partial snapshots before the anchor.* Forgotten. Each is a subset of the stream at its listing, which the
  anchor's complete listing contains, because the stream only grows.
- *Concurrent shippers.* Serialised by lock 7261007 (`jobhunter/backup/shipper.py:193-207`), so the newest
  receipt by id is the last list taken.
- *A prune beside a ship.* The prune holds the job lock, not the shipment lock. Whichever receipt it reads,
  that snapshot is complete for every earlier receipt, and anything made after it is newer and kept.
- *A receipt whose snapshot restic no longer lists* (forgotten by hand, a swapped repository, or a restored
  older set whose receipt a later prune already passed). Nothing is forgotten, which is the safe direction.
  It heals at the next receipted shipment.
- *A restore from an older set.* The restored database's newest receipt is an older one. Snapshots of the
  lost lifetime are newer than it and kept; the first shipment after the restore lists the restored union
  and becomes the new anchor.
- *Clock skew.* Skew can only make the prune keep more, or forget an unreceipted partial whose files are
  still on the manifests volume. The anchor itself is chosen by receipt id, never by time, so no skew
  removes it. Equal times are kept (`<`, `prune.py:85`).
- *A manifests volume lost while the database survives.* `migrate` rewrites every recorded manifest's file
  from its `scope` (`jobhunter/privacy/identities.py:78-94`), and `migrate` runs on every `up`, so the
  stream is whole again before the next shipment in the ordinary path.

One sequence remains in theory, M-4 below. It needs the shipment lock lost in the instant between a
shipper's listing and restic's start, and then no successful shipment for the rest of the week.

*Fix C's concern (1): is a warning enough for `receipt_not_listed`?* Yes, to ship. The skip loses nothing;
it only stops the bound on a restore's work, and only until the next receipt. Persistent shipment failure,
the case where snapshots really pile up (one incomplete snapshot per five-minute run), already pages
through `privacy.manifest_unshipped`. I would still put the reason where the operator looks; see M-3.

**C2 in detail.**

- *Is the recorded set the same set the key clauses test?* Yes. The recorded revision is the result's own
  (`_CURRENT` selects `r.job_revision_id`, `golden.py:224-230`), so its lineage is what clauses 1 and 2
  test, and `snapshot_sources` reads `job.*.sources[*]`, the path clause 3's `_NAMES_SOURCE` tests
  (`source_purge.py:183-186`).
- *Is any feedback path left that writes a snapshot without sources?* No. `record_feedback` is the only
  writer: my grep for `INSERT INTO score_feedback` and for any write of `score_snapshot` over `jobhunter/`
  finds that insert and the purge's own `SET score_snapshot = NULL` (`source_purge.py:270`). The one other
  reader, `jobhunter/scoring/reconcilers.py:303`, reads `M` only.
- *Live, replay, postcondition, export.* All four go through the frozen ids of the one `field_redact`
  operation (`source_purge.py:554, 573, 939`). Tests: `tests/integration/test_source_purge.py:594-610`,
  `test_typed_replay.py:484-515`, `test_export.py` (parametrized), and the writer through the real
  `record_feedback` (`test_golden_cases.py:68`).
- *Concern (2), tests write the detached end state by hand.* Acceptable. The clause under test reads only
  the row's own column, so how the keys came to be NULL does not matter to it.
- A snapshot that records no `sources` (written before this rule) makes the clause NULL, so it is not
  matched. The spec says no deployment holds one (L6714).

**C8 in detail.** The helper pauses, then counts the worker's sessions with `xact_start IS NOT NULL`, found
by the container's addresses, through `psql` as `jobhunter_bootstrap`, which may read every session's
columns (`stack.ts:88-90, 211-214`). While any is open it unpauses, waits one second and tries again. A
frozen process starts no new transaction, so a clean check stays clean. It cannot hang: 30 attempts at
most, then it throws with the worker running (`:237`); a failed check unpauses before it rethrows
(`:227-230`); the spec sets `workerPaused` only after a successful pause, and its `afterEach` unpauses
(`20-e18-privacy.spec.ts:58-61, 69-73`). Session advisory locks are not checked (concern 5); the api's
delete-everything admission takes none of the worker's, so I accept that.

### New findings

#### Critical

None.

#### Important

None.

#### Minor

**M-1. The new `max_ids` rule is false for a table a migration fills from existing rows.** Spec §19.6
(L6449), `jobhunter/db/compat.py:46-51`, `jobhunter/privacy/purge.py:23-28`.

- *The rule's premise:* "a table created after a manifest was written has no row within its bound".
- *Failure path, in a later release only.* Release N+1 creates table T and its migration fills T from
  table S. A set taken before a delete-everything manifest M is restored under N+1. `migrate` runs `alembic`
  before `replay` (`jobhunter/db/migrate.py:75-83`), so T is filled from rows M was written to delete. The
  replay then reads T's bound as 0 and deletes nothing from T, unless T cascades from S.
- No kind has an added key today, so nothing fails now.
- *Fix:* one clause in the three places: the rule holds only for a table the migration creates empty; a
  migration that fills a new table from existing rows must make it cascade from a bounded table, or the
  release counts as a manifest-format change.

**M-2. Three forms still pass the static bounds guard.** `tests/unit/test_kind_bounds_declared.py:333-339,
388-389, 435-441`. Planted in `JobTombstone.execute`, each returns no violation in my probe:

```python
limits = content.get("max_ids"); limits["planted"]      # also content.get("max_ids", {})["planted"]
k = "max_ids"; content[k]["planted"]
(limits := content["max_ids"])["planted"]
```

`is_max_ids` knows only a literal `["max_ids"]` subscript, and `reads_in` does not look through a
`NamedExpr`. The same probe confirms the guard fails eleven other forms (loop target, conditional, `dict()`
copy, star unpacking, `|=`, `__getitem__`, lambda, `setdefault`, `pop`, tuple binding, a `scope` path). The
five real kinds use none of the three. *Fix:* treat `.get("max_ids", …)` as a bounds expression, treat a
`NamedExpr` whose value holds bounds as holding them, and fail a non-literal subscript of `content` or
`scope`. R-M1 itself is fixed as Codex wrote it; this is what is left of the same class.

**M-3. A skipped manifests forget is invisible outside the log.** `jobhunter/backup/prune.py:78, 83, 112`.
The run succeeds with `manifests_forgotten: 0` whether there was nothing older, no receipt, or a receipt
restic does not list, and the RUNBOOK names neither skip. *Fix:* add the reason to the run's report (for
example `manifests_skipped: "receipt_not_listed"`), which the health page already shows, and one RUNBOOK
sentence in section 8 saying it clears at the next shipment. No alert is needed.

**M-4. A lost shipment lock can still, in theory, anchor the prune on a stale list.**
`jobhunter/backup/shipper.py:143-151, 193-204`, `jobhunter/backup/prune.py:85`. Not in the fix range; it is
the residual of C1's argument, so I state it.

- *Path.* Shipper A lists `{M1}`. Its lock connection dies before restic starts (a PostgreSQL restart).
  M2 is written; shipper B takes the lock, ships `{M1, M2}` and writes its receipt. A's restic starts after
  B's, so A's snapshot is the later in time, and A then writes its receipt, the newest by id. The prune
  anchors on A's `{M1}` and forgets B's snapshot as older.
- *Why it is Minor.* If A loses the lock at any point after restic has started, A's snapshot is the earlier
  one and B's is kept, so the window is the few tens of milliseconds between the listing and restic's
  start. The next five-minute run then sees the stream differ from A's items and ships a complete snapshot,
  so the prune must also find a week without one successful shipment.
- *Fix, cheap:* before writing the receipt, check on the lock connection that the lock is still held (any
  statement on it fails if the session is gone), and raise without a receipt otherwise.

### Question 2: new defects in the fix range

- **Lock order: none found.**
  - C9: the reset's chunk holds `active_config FOR UPDATE`, then waits for 7261005
    (`jobhunter/privacy/kinds/delete_everything.py:766-774`, `settings/save.py:352-353`), the same order as
    a disabling save. Under 7261005 the pinger reads `schedule_state` plainly and writes `deadman_state`,
    which delete everything keeps and that chunk does not write (`delete_everything.py:186-190`), so there
    is no cycle. The shipped default is enabled, so the lock is not taken today.
  - C1: the prune takes no new lock; its forget waits on restic's repository lock as `forget_last` did.
  - C7: the pre-job check takes no advisory lock of its own and runs under the job lock.
- **Privacy regressions: none found.** `manifests_forget_skipped` logs a run id and a reason. `sources` in a
  snapshot holds source ids only, and reaches the user's own export.
- **Drift: none found.** Spec §19.5, §19.6, §20.1 and §20.5, the RUNBOOK, PRIVACY.md, both UI languages and
  the docstrings agree with the code on the prune, the Logs row, the `max_ids` rule and the feedback rule.
  `test_privacy_md`, `test_runbook` and `test_spec_revision` pass.
- **C7 removes nothing it should not.** It takes `jobhunter_restore_test` and `<staging>/restore_test`
  only, and leaves `<staging>/restore` (`restore_test.py:239-252`). It runs inside the job lock, so it can
  never take a running restore test's copy. It cannot fail the job before its run row: a refused drop is
  caught and logged (`:246-247`), and the directory removal ignores errors (`:232-236`).

### Question 3: the parked list, section 2

1. **T28-7.** Unchanged; acceptable as parked.
2. **T31-1 (5), operator fields.** Unchanged; still a deployment carry.
3. **T30-3 (2), E18.** Improved by C8: the pause no longer has a known way to stall the 202. Acceptable as
   a required gate.
4. **T27-4 M2.** Unchanged since fix B; closed.
5. **Fail-open egress rule.** Unchanged; acceptable with the RUNBOOK's check.
6. **The bare `KeyError`.** Closed in fix A and B; C3 adds the release rule I asked for and C4 tightens the
   guard. M-1 and M-2 are what remains, both Minor.

Fix C changes no other judgement.

### What I ran

No stack, no sudo, no network or paid call, no sub-agent. `.secrets/` and `.fixtures-raw/` were not read,
listed or grepped. `git status --short` was empty before this file was written; this file is the only
change I made (`rereview2-codex.md`, the other reviewer's file, appeared beside it during my review).

- **Deviation, disclosed.** My instructions said no Docker. `tests/unit/test_restore_run.py` uses the
  `migrated` PostgreSQL fixture, which starts a throwaway `jh-test-pg-*` container
  (`tests/support/pg.py:325-331`); I ran the file before I saw that. It touched no product container or
  volume. `docker ps -a --filter name=jh-test-pg` afterwards listed nothing. I ran no other test that needs
  PostgreSQL.
- `uv run --group docparse pytest -W error -q -p no:cacheprovider`, with the six provider keys unset, on
  `tests/unit/test_kind_bounds_declared.py`, `test_restic_wrapper.py`, `test_restore_run.py`,
  `test_privacy_retention_rows.py`, `test_privacy_md.py`, `test_runbook.py`, `test_spec_revision.py` and
  `test_migrate_lock_key.py`: 113 passed in 56 s.
- `npm --prefix frontend test -- PrivacySection`: 1 file, 3 tests passed.
- An in-process probe, kept outside the repository, that plants sixteen read forms in
  `JobTombstone.execute` and calls the test module's own `bound_reads` and `violations`: thirteen fail the
  check as they should, three pass (M-2).
- `git show` for each of the ten commits, and greps over `backend/src/jobhunter`, `frontend/src`,
  `frontend/e2e`, `tests`, `deploy` and `docs` for the dropped "newest 10" names, every writer and reader
  of `score_snapshot`, the lock keys and the new log events.
- Not run: the integration, crash and stack tests, Playwright, and any RED run against the pre-fix code.
