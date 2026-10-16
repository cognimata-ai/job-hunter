# Spec review, round 4 (revision 3.1) — Claude Fable 5.1

Commit 6d51db2, read as the full diff adc1335→6d51db2 plus the DDL it touches.

**Verdict: APPROVE WITH CHANGES.** N4 and all five minors are RESOLVED; both writer deviations ACCEPTED. One new MAJOR (N5) in the typed-replay material: a replay rebuild creates rows that a later manifest in the stream can neither name nor select, so the later manifest's job deletion fails on `ON DELETE RESTRICT` and `migrate` stops. One minor.

## 1. My round-3 items

| Item | Status | Evidence |
|---|---|---|
| N4 fenced raw item vs S1/I1 | **RESOLVED** | §1.5 S1 and §17.2 I1 name `deferred('job_fenced')` as a recorded disposition; §7.7 DDL (`reason_code = 'job_fenced'`, `normalized` kept, `disposed_at` set); §7.16.1 re-resolution by later runs' node 6 (≤ 1,000 per run, under that run's predicate), 90-day drop counted; §8.6 step 3; §9.1, §9.2; test "Fenced raw items". |
| Lane slots > `S` | **RESOLVED** | §9.6.2 trim loop (largest excess over `share × S`, similarity first), `spare <= 0`, `assert sum(slots) == S`; worked example; property test "Admission size under changing conditions". |
| `seen_at` bumps the ranking generation | **RESOLVED** | §7.1 excludes the implicit `seen_on_open`; §14.2 `seen_on_open` flag; §15.4. |
| `fließend → C1` | **RESOLVED** | §11.4 table: `fließend / fluent` → `B2`, `verhandlungssicher` → `C1`. |
| §17.4 wording | **RESOLVED** | "structurally complete (`complete` or `mass_disappearance`)". |
| Dumped `running` backup row | **RESOLVED** | §19.5 restore step 5. |
| tmpfs per-job group (note) | **RESOLVED** | `mode=2770` on the tmpfs root; per-job dirs 0770, files 0660 (§12.2, §19.1). |

**Deviations**
- *Option (b), `deferred('job_fenced')`* — **ACCEPT.** It is more honest than blocking on the lock: the run report shows what was deferred, the item survives a worker restart, and the retry is bounded.
- *Dumped set row → `succeeded`, other mid-run rows → `failed('restored_mid_backup')`* — **ACCEPT.** The set being restored demonstrably succeeded; its row should say so, and `backup_set.json` plus the restic id are the authoritative source.

## 2. New defects in the revision-3.1 diff

**N5 — MAJOR. Replay rebuilds create rows that later manifests cannot delete (§19.5 typed operations, §19.3 step 7, §20.5).**
Scenario: backups run at 04:00. Day 1 10:00 the Adzuna purge M1 commits; its rebuild gives the mixed-source job J a new revision R₁ (text from JSearch). Day 2 03:17 `retention_daily` tombstones J as M2 (`row_delete job_revisions [R₀, R₁]`, `row_delete jobs [J]`, …). The VPS is lost at 03:30, before the day-2 backup. Restore uses the day-1 04:00 set. Replay M1: identity advancement has put every sequence above all `max_ids` of the stream, so the rebuild inserts R₁′ with a fresh id; M1 completes. Replay M2: it deletes R₀ (named) and R₁ (absent), then `row_delete jobs [J]` fails, because `job_revisions.job_id REFERENCES jobs(id)` is `ON DELETE RESTRICT` (§7.9 DDL, line 2009) and R₁′ still references J; the same holds for the rebuilt current `det_results` instances (`det_results.job_id`, RESTRICT) and, for a selector, delete-everything: `row_delete jobs all ≤ max_ids` removes J while R₁′ (id above every bound) survives. Replay stops `migrate` with an error (§19.5), `api` and `worker` do not start, and the S9 RTO is missed on a sequence the spec itself produces (any source purge followed by a tombstone, retention purge or delete-everything before the next successful backup set). Fix (either): **(a)** run every rebuild **after the whole stream has been replayed**: the `fence` operation keeps its fence active through the remaining manifests and rebuilds only when no later manifest remains, releasing the fence in its final transaction (Codex R3-6 still holds); a job a later manifest deleted needs no rebuild; or **(b)** make a `row_delete` on `jobs` graph-closing on replay: delete the job's *current* dependents regardless of id before the job row, and the same for selectors. (a) is smaller. Add the sequence to the "Typed replay" test and V22.

**Minor — §19.5 step 3 / restore verify.** A manifest row committed with its fence but lost before its file was written (crash in the few seconds before the rename, then a disaster before the retry) restores as `executing` without a file and with an active fence; replay iterates files, so nothing touches it, and `restore verify --phase replayed` fails on "no purge fence is active" although the restored `purge` operation would complete it on the first worker start. Let `--phase replayed` treat an `executing` manifest that has no file and a `pending` purge operation as "pending retry", or run that operation inside `migrate` step 7.

Checked and clean: the `migrate` order (identities before and after `alembic`, grants after every table exists, seeding before replay), the nullable `file_sha256` with its CHECK, the `pg_stat_activity` wait before `max_ids`, selector bounds, `field_redact` postconditions, the released-submission path outside `batch_one_in_flight`, the no-re-batching rule and `det_input_sources`.

## 3. Verdict

**APPROVE WITH CHANGES.** Must fix: **N5** (replay rebuilds vs later manifests: run rebuilds after the whole stream, or make job deletes graph-closing on replay). Recommended: the `executing`-without-file case in `restore verify`.
