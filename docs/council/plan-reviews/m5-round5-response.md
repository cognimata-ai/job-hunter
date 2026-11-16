# M5 plan, fix round 5: the plan author's response to Codex round 5

- **Review:** `docs/council/plan-reviews/m5-round5-codex.md` (REJECT; it resolves round-4 findings 2 and 3, resolves findings 1 and 4 only in part, and raises 3 MAJOR findings).
- **Plan:** `docs/superpowers/plans/2026-11-09-m5-operations-privacy-production.md`, edited in place on top of 6b035c5 (HEAD 0e4d7f4 adds only the review). Not committed.
- **References:** `P:n` is a line of the edited plan.

All three findings hold against the code at HEAD: 3 fixed, 0 rebutted.

As asked, each fix uses one mechanism that closes the gap for good, not another special case. M5-U1–U4 and M5-R1–R8 stay binding. The new decisions are folded into Deviations 33, 54 and 55, flagged below.

The volume check found one more defect (`s3data`'s owner), and building the export table exposed a live race. That makes 2 sweep fixes.

## Findings

**1. MAJOR — calls created after the manifest's computation escape the profile redaction. Fixed by closing the call set at admission.**

What I verified:
- The profile purge cancelled the version's `propose_queries` operation only in phase B, after A2 had frozen the call ids.
- Every reservation takes `active_config FOR SHARE`, then `_lock_owner` (`llm/spend_ledger.py:288, 863`). That checks only its owner and the global fence, not the profile fence. So a proposal coroutine could reserve a new call between A2 and phase B.

The mechanism: A0 now cancels every live `propose_queries` operation of the version, in the admission transaction under `active_config FOR UPDATE`, owner rows before domain rows (P:463). This makes a call created after A2 impossible:
- **After A0, no call can be reserved or dispatched.** Every reservation and every dispatch marker takes its operation's row `FOR SHARE` and requires it `running` under its claim (`lock_operation_owner` via `_lock_owner`, `:863, 1160`), so one that comes after A0's commit is `FencedOut`.
- **A racing reservation is in A2's set.** A reservation that committed before A0 is visible to A2, because the cancel waited for its `FOR SHARE`.
- **No new operation can start for the version.**
  - `request_proposal` serves only a version being confirmed or activated, or the active one (`profile/activation.py:344`, `api/routes/profile.py:458-461`). The fenced, non-active version can be neither: activation answers 409 `profile_purge_in_progress`.
  - The version's drafts are all `confirmed`, so no new extraction call can start for it.
- So the set A2 computes is every call the version will ever have. Phase B no longer cancels anything (P:466).

I chose this over a profile-fence check at reservation. That check would have needed the version in every owner's lock path, and the ownership check already exists.

New tests (P:468):
- `test_no_call_of_the_version_is_reserved_after_admission`: a reservation after A0 is `FencedOut` and adds no row. A call dispatched before A0 is in the set, and its response, released after completion, stores nothing.
- `test_a_reservation_racing_admission_is_in_the_computed_set`.

Consumes names the owner check and the `request_proposal` callers (P:460). Deviation 54 (P:1029).

**2. MAJOR — restore invalidation put exports into states replay could not purge. Fixed with one export-state table for live purges, replay and restore.**

What I verified:
- Task 1's edges are `running → ready | failed | purged` and `ready → expired | purged`. There is no edge out of `expired` or `failed`.
- Restore moved rows to `expired` and `failed` before replay, so a newer manifest's export op could neither apply its marker nor pass its postcondition.

The same table also exposed a live race. A named export can turn `failed` (its failure hook) or `expired` (retention) between A2 and phase B. The purge's marker would then fail on the live path too.

The mechanism: Task 17's table of export states (P:528-538). For each state it gives:
- its files on the volume;
- its `storage_path`;
- its download answer;
- what a purge's export op does with it, live or replayed;
- what `restore run` does with it.

The rules it sets:
- **Content-free states.** `failed`, `expired` and `purged` have `storage_path` NULL and no file.
- **What a purge's export op does.** It moves only `ready` and `running` rows to `purged`, leaves a content-free row as it is, and removes `<id>-*`.
  - Task 12's export ops follow it (P:417).
  - Task 9's postcondition accepts any content-free state for `exports` (P:337).
  - The expiry now nulls `storage_path` (P:542).
- **Restore applies its column after replay**, as step 7b (P:757; the restore test, P:758):
  - exports that a replayed manifest names end `purged`, as their live purge left them;
  - the rest still `ready` end `expired`, and the rest still `running` end `failed`;
  - then the volume is emptied.

The C32 expectation is corrected: the export that the Adzuna manifest names comes back `purged`, with a 410 download (P:761). The `expired` case moves to `test_a_backup_before_any_manifest_restores`, with a 410 download (P:761).

New tests:
- `test_restored_exports_follow_the_state_table`: a `ready` and a `running` export named by a newer manifest, and one of each not named (P:761);
- `test_a_named_export_that_failed_or_expired_before_phase_b_satisfies_the_op`, the live race (P:544).

Deviation 55 (P:1030); spec 3.7 item 15 (P:902); the RUNBOOK's restore note (P:866).

**3. MAJOR — the local restic repository volume was unwritable by the shipper. Fixed with the mount point's owner and mode.**

What I verified:
- The runtime stage creates only `/data/cv` before `USER 10001:10001` (`Dockerfile:44-52`). The plan added `/data/manifests`, `/data/exports` and `/data/staging`, never `/data/restic-repo`.
- A fresh `restic_repo` volume therefore started root-owned.

The fix:
- The `backup` stage creates `/data/restic-repo` as `10001:10001` 0700 before switching users (P:361).
- `test_fresh_volumes_mount_10001_0700` now reads `stat -c '%u:%g %a'` = `10001:10001 700` for every M5 mount point on new named volumes, `/data/restic-repo` included (P:369).
- `test_ship_into_a_local_repository` removes `restic_repo` and `backup_staging` first, so `backup init` runs as 10001 on fresh volumes (P:369).

## Sweep: the owner and mode of every named volume

Task 21 now holds one table of every named volume across the prod, test, s3stub and dev compose files (P:640-652). For each it names the mount point, the owner and mode and the image they come from, the writers with their uids, and the read-only readers. The invariant: every image that mounts a volume carries the mount point with the same owner and mode, so the order in which services start never matters.

| Volume | Result |
|---|---|
| `cv_store`, `manifests`, `exports` | Consistent. `10001:10001` 0700 in `runtime` (`Dockerfile:47-49`, Task 9), inherited by the `backup` stage. Every writer (api, worker, migrate, restore) and every reader runs as 10001. |
| `backup_staging`, `restic_repo` | `10001:10001` 0700 in `backup`. `restic_repo` was **finding 3**. |
| `parse_jobs` | Consistent. The tmpfs options `uid=0,gid=10011,mode=2770` (`deploy/compose.dev.yaml:233-235`). |
| `pgdata` | Consistent. `999:999` from the pgvector image, run as `999:999`. |
| `caddy_data`, `caddy_config` | Consistent. The caddy image sets no `USER`, so root owns and writes them. With every capability dropped but `NET_BIND_SERVICE`, the owner still writes without `CAP_DAC_OVERRIDE` (Deviation 33's "the image's user"). |
| `s3data` | **Fixed.** Its owner depended on the versitygw image's default user, which I cannot read offline. `s3stub` now sets `user: "0:0"`, so the volume is root's whatever the image says (P:652, P:654). `test_restic_init_against_the_s3_stand_in` now runs on an `s3data` volume the fixture created fresh (P:720). |

`test_every_named_volume_matches_the_owner_table` pins the table against `config --format json` of all four files (P:660). It checks that:
- each read-write mounter runs as the table's uid;
- each reader mounts the volume `:ro`;
- `parse_jobs` keeps its tmpfs options;
- `s3stub` is root.

Deviation 33 (P:1008) records the invariant.

## Other changes

None beyond the above. The plan still has 55 Deviations.

## New decisions to flag

- **Finding 1:** the profile purge cancels the version's proposal operations at A0, not in phase B, chosen over a fence check at reservation (Deviation 54).
- **Finding 2 and the live race:**
  - one export-state table;
  - content-free states (`failed`, `expired`, `purged`) satisfy a purge's export op;
  - the expiry nulls `storage_path`;
  - restore's export step runs after replay (Deviation 55).
- **Finding 3 and the sweep:**
  - `/data/restic-repo` in the `backup` stage;
  - `s3stub` runs as `0:0`;
  - one owner and mode per named volume, carried by every image that mounts it (Deviation 33).
