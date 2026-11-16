**Verdict: REJECT**

I read the brief, round-2 review and response, revised plan, relevant revision-3.6 spec sections, research digests and M4 conventions. I checked the ten fixes and nine sweep fixes against HEAD’s code and task interfaces. Two read-only, in-memory probes checked SQLAlchemy statement ordering and E18 discovery. No network, Docker or sub-agents were used; no `.secrets/`, `.fixtures-raw/`, keys or credentials were read; nothing changed.

**Round-2 findings — disposition**

| # | Finding | Resolved? |
|---|---|---|
| 1 | Missing evidence for released batch submissions | **Yes.** Evidence now covers deleted items independently of target state, including released submissions (P:384, P:389). |
| 2 | Incomplete group settlement and member locking | **Yes.** Chunks lock and revalidate co-members; graph deletion uses `leave_group` with `held` (P:406, P:438). |
| 3 | Retry exemption unavailable before routing | **Yes.** Exemptions now match method/path, with full-middleware tests (P:289, P:301). |
| 4 | CV upload crosses the admission fence | **Yes for the original race.** The transaction guard covers delayed upload transactions; it introduces new finding 1 below (P:290). |
| 5 | Source replay omits admission effects | **Yes for replay coverage.** `reapply_admission` restores those effects; its new postcondition exposes finding 2 below (P:480, P:706). |
| 6 | Migration API breaks synchronous callers | **Yes.** Both entry points retain their synchronous/asynchronous contracts (P:665). |
| 7 | Tombstoning exceeds 200 targets | **Yes.** Bounded target transactions precede graph deletion, with a 260-target test (P:435, P:444). |
| 8 | Startup checks lack the external S3 network | **Yes.** `prod_session` starts S3 first and removes it last (P:626). |
| 9 | Playwright excludes E18 | **Yes.** Task 30 updates discovery; the regex probe confirms inclusion (P:807). |
| 10 | Mixed-generation backup cannot load every key | **Yes.** Staged rotation processes each historical key, with an A/B-to-C test (P:767, P:771, P:836). |

**New findings**

1. **MAJOR — The transaction guard breaks group-write responses after committing their changes.** P:290; S:5029, S:5030; `backend/src/jobhunter/api/routes/groups.py:129`, `:169`, `:196`; `backend/src/jobhunter/db/snapshot.py:19` — Both group split and score-individually commit their mutation, then call `read_snapshot` while `WRITE_GUARD` remains set. The new `begin` listener executes its SELECT before `SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY`. PostgreSQL refuses that isolation change after a query, producing a 500 after the mutation committed. The in-memory probe confirmed the prescribed statement order. — Make the guard compatible with transaction isolation setup, or suspend it specifically around enforced read-only snapshots while retaining protection for writes. Test both routes through the real middleware.

2. **MAJOR — A live source purge cannot satisfy its new timestamp postcondition.** P:332, P:337, P:479, P:483; S:6560, S:6571; `backend/src/jobhunter/db/migrations/versions/0005_m1_collection.py:98` — A0 sets `sources.purge_fence_at := now` before committing. The manifest’s `fenced_at` is defined as the commit cutoff, obtained by a later `clock_timestamp()` read. Completion now requires `purge_fence_at ≥ fenced_at`, which the live path cannot satisfy. Only replay updates the audit timestamp to that later value. Ordinary source purges therefore fail their postconditions and retain their fences. — Define consistent durable audit and manifest timestamps, keeping the later quiescence cutoff separate where necessary. Test live completion with distinct admission and post-commit times.

3. **MAJOR — Purges still strand online spend entries after deleting their calls.** P:384, P:502, P:1041; S:5578, S:6458, S:6529; `backend/src/jobhunter/scoring/targets.py:1324`; `backend/src/jobhunter/llm/spend_ledger.py:583`, `:1881` — Task 11 settles batch entries only. Purging an online `in_progress` target deletes its `llm_calls` while its ledger entry can remain `reserved` or `dispatched`. Both ordinary settlement and the unsettled reconciler require an inner join to that deleted call, so neither can finish the entry. Delete-everything’s reserved-entry mapping does not cover its dispatched online calls. This is carry G04, which the closing note assigns to these tasks without implementing it. — Specify online settlement/recovery before removing the required call metadata, including late-response handling. Add reserved and dispatched online-call tests for source purge and delete-everything.

4. **MAJOR — Identity advancement ignores an uncalled sequence at the bound.** P:666, P:669; S:6199, S:6279 — The algorithm calls `setval(..., target, true)` only when the sequence position is *below* the target. A fresh identity sequence has `last_value = 1` and `is_called = false`. With a manifest bound of 1 and an empty table, the algorithm skips advancement, and the next insert receives 1—an ID the manifest can delete. This breaks the explicitly supported first-installation-with-manifests path. — Account for `is_called` and the next allocatable value. Add an exact-equality test with an uncalled sequence and `max_ids[table] = 1`.

5. **MAJOR — The rotation drill invokes a script hardcoded to production.** P:623, P:768, P:769; S:6362 — The drill runs on `jobhunter-prodtest` using `.secrets/test` and `compose.test.env`, but `rotate-db-role.sh` installs into `/etc/jobhunter/secrets` and invokes project `jobhunter` with `deploy/release.lock` and `/etc/jobhunter/jobhunter.env`. Those deployment files are not prerequisites of the test fixture, and the command does not target PT’s database or services. The specified drill cannot perform its rotation as written. — Parameterize the compose invocation and secret destination, retaining production defaults, and have the drill explicitly pass PT’s configuration. Assert that the real rotation and restarts affect only the test project.

6. **MAJOR — An exhausted export operation permanently blocks later exports.** P:120, P:522, P:523, P:526; S:2860, S:5085; `backend/src/jobhunter/worker/operations.py:549` — An export encountering an executing purge requeues every minute. After ten attempts, the operation framework marks its operation `failed`; Task 17 specifies no failure hook to move the export out of `running`. The unique running-export index then rejects every subsequent export, and retention never removes the stranded running row. A sufficiently long source purge triggers this without any exceptional I/O failure. — Add an idempotent export failure hook covering exhaustion and reclaim, with appropriate file cleanup. Test exhaustion followed by a successful new export, and define whether waiting for a purge should consume attempts.

### Challenges to rulings

None.