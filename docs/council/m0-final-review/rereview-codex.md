### Finding verdicts (Codex 1–7 + plan issue; Fable 1–9 + plan issues)

Reviewed `b2dc1dc..407fda3`, both reviews, requirements, reports, rulings and the relevant spec. Repository unchanged. I inspected tests and reported RED/GREEN evidence; I did not rerun pytest or Docker. `git diff --check` passed.

**Codex findings**

| Finding | Verdict and evidence |
|---|---|
| **1. Locked operations escape takeover** | **ADDRESSED.** [operations.py:152](/home/user/job-hunter/backend/src/jobhunter/worker/operations.py:152) now waits with `FOR UPDATE`; periodic recovery retains `SKIP LOCKED`. Tests hold both owner-share and heartbeat-update locks, then verify requeue, rejected old heartbeat/finalization, and complete rollback on `55P03`. |
| **2. Obsolete workers acquire fresh ownership** | **ADDRESSED.** Admission fences before locking domain rows: [runs.py:244](/home/user/job-hunter/backend/src/jobhunter/worker/runs.py:244), [operations.py:207](/home/user/job-hunter/backend/src/jobhunter/worker/operations.py:207). New attempts and stale-run reconciliation also fence, including before the reconciler’s initial row selection ([runs.py:475](/home/user/job-hunter/backend/src/jobhunter/worker/runs.py:475)). |
| **3. Delayed epoch claim evicts replacement** | **ACCEPTED-AS-RULED — sound for the reported race.** [epoch.py:152](/home/user/job-hunter/backend/src/jobhunter/worker/epoch.py:152) serializes claimants before checking advisory-lock ownership. A displaced claimant can finish before its successor, but cannot overwrite a successor that already committed. Existing-row and empty-table tests exercise termination between check and mutation. |
| **4. TOTP replacement bypasses credential recheck** | **ADDRESSED.** [users.py:50](/home/user/job-hunter/backend/src/jobhunter/auth/users.py:50) compares the verified ciphertext under the user-row lock. The regression replaces A with B after verification and checks 401, no session and a failed attempt ([test_auth.py:385](/home/user/job-hunter/tests/integration/test_auth.py:385)). |
| **5. Failed cleanup orphans checkpoints** | **ADDRESSED.** Runs explicitly depend on checkpoint cleanup; failure skips parent deletion ([retention.py:70](/home/user/job-hunter/backend/src/jobhunter/privacy/retention.py:70), [retention.py:183](/home/user/job-hunter/backend/src/jobhunter/privacy/retention.py:183)). The 398-day-old-run test proves discovery rows survive, independent cleanup continues, and the next pass deletes everything and resolves the alert. |
| **6. Unbounded Argon2 work** | **ADDRESSED.** [argon2_gate.py:35](/home/user/job-hunter/backend/src/jobhunter/api/argon2_gate.py:35) bounds workers to two and admitted requests to ten. All route hashing paths use it; slot acquisition precedes another lockout check ([auth.py:278](/home/user/job-hunter/backend/src/jobhunter/api/routes/auth.py:278)). The dedicated executor preserves the computation bound despite request cancellation. |
| **7. Missing terminal-slot metrics** | **ADDRESSED.** [tasks.py:89](/home/user/job-hunter/backend/src/jobhunter/worker/tasks.py:89) observes the report after commit; [tick.py:83](/home/user/job-hunter/backend/src/jobhunter/worker/tick.py:83) counts newly inserted missed/overlapping slots. The regression checks counts and a subsequent no-op tick. |

**Fencing walkthrough.** The new order is heartbeat row → run/operation row. Admissions hold `FOR KEY SHARE`; takeover requires conflicting `FOR UPDATE`. Thus an admission either commits first and appears in the subsequent sweep, or waits and rejects the changed epoch. Ordinary lock-session heartbeats update only non-key columns and remain compatible with admission’s lock; the run regression explicitly exercises that compatibility ([test_runs.py:507](/home/user/job-hunter/tests/integration/test_runs.py:507)).

Takeover holds the heartbeat row before `_HOLDS_LOCK`. If A loses its lock session after that check, B must wait for A’s claim transaction or time out; after A commits, B advances beyond A and sweeps it. On an empty table, the uncommitted epoch-0 insert provides the same serialization through the unique-key conflict; a failed claim rolls it back. An operation-lock timeout likewise rolls back the epoch and sweeps together. The startup `55P03` path returns failure and releases the singleton session during teardown ([main.py:167](/home/user/job-hunter/backend/src/jobhunter/worker/main.py:167)). I found no reversed acquisition order or heartbeat dependency completing a deadlock cycle in these M0 paths.

**Fable findings**

| Finding | Verdict and evidence |
|---|---|
| **1. Operations sweep skips locks** | **ADDRESSED**, as Codex 1 above. |
| **2. Argon2 concurrency** | **ADDRESSED**, as Codex 6 above. |
| **3. C17 may never attempt finalization** | **ACCEPTED-AS-RULED — sound.** T21-h separates process containment from deterministic write refusal. C17 compares replacement state ([test_worker_process.py:103](/home/user/job-hunter/tests/crash/test_worker_process.py:103)); the gated in-process test explicitly requires `run_fenced`, `write=finalize`, and unchanged replacement state ([test_runs.py:588](/home/user/job-hunter/tests/integration/test_runs.py:588)). |
| **4. Additive shutdown budgets** | **ADDRESSED.** [shutdown.py:90](/home/user/job-hunter/backend/src/jobhunter/worker/shutdown.py:90) creates the shared 110-second deadline; [main.py:247](/home/user/job-hunter/backend/src/jobhunter/worker/main.py:247) passes remaining budgets into teardown and bounds asynchronous closes. The injected-clock regression checks budget consumption and a timed-out close. |
| **5. Mixed stale-cutoff clocks** | **ACCEPTED-AS-RULED — sound for the specified single-host deployment.** Both threshold comments document the shared-kernel assumption and required change for a remote database ([runs.py:75](/home/user/job-hunter/backend/src/jobhunter/worker/runs.py:75), [operations.py:66](/home/user/job-hunter/backend/src/jobhunter/worker/operations.py:66)). |
| **6. Enrollment-code replay** | **ADDRESSED.** [cli_user.py:181](/home/user/job-hunter/backend/src/jobhunter/cli_user.py:181) passes the matched step; [users.py:200](/home/user/job-hunter/backend/src/jobhunter/auth/users.py:200) persists it. The regression exercises CLI enrollment, rejected same-step login and accepted later-step login. |
| **7. Rate-limited requests still query SQL** | **ACCEPTED-AS-RULED — sound.** [deps.py:101](/home/user/job-hunter/backend/src/jobhunter/api/deps.py:101) checks the token HMAC before SQL, counting only validated sessions afterward. T21-j’s compromise avoids storing arbitrary invalid-cookie keys; concurrent boundary requests can still perform validation. |
| **8. Username-HMAC whitespace mismatch** | **ADDRESSED.** [throttle.py:84](/home/user/job-hunter/backend/src/jobhunter/auth/throttle.py:84) uses `normalize_username`; tests check both HMAC equality and recorded login attempts. |
| **9. Duplicated run deferral** | **ADDRESSED.** [tasks.py:233](/home/user/job-hunter/backend/src/jobhunter/worker/tasks.py:233) supplies the shared helper used by tick and runs. |

**Plan/spec issues**

- **Codex: destructive stack fixture — ACCEPTED-AS-RULED, sound.** Explicit project/probe names isolate removal; existing dev containers or networks cause a skip ([test_dev_stack.py:24](/home/user/job-hunter/tests/stack/test_dev_stack.py:24), [test_dev_stack.py:92](/home/user/job-hunter/tests/stack/test_dev_stack.py:92)). Fixed ports/subnets remain, but conflicts do not authorize removing development resources.
- **Codex: zombie-admission acceptance — ADDRESSED.** Superseded by T21-c and the transactional guards above; recorded in [plan:3418](/home/user/job-hunter/docs/superpowers/plans/2026-10-16-m0-foundation.md:3418).
- **Codex: epoch check incorrectly considered sufficient — ACCEPTED-AS-RULED, sound.** T21-c/i add serialization, including first initialization; see Codex 3.
- **Codex: unconditional retention isolation — ADDRESSED.** Explicit dependency handling replaces it; see Codex 5.
- **Fable: incomplete deviations — ADDRESSED.** Deviations 17–28 record the requested implementation/ruling changes ([plan:3415](/home/user/job-hunter/docs/superpowers/plans/2026-10-16-m0-foundation.md:3415)).
- **Fable: disabled-schedule critical alert — ACCEPTED-AS-RULED.** Explicit M2/M5 carry-forward is appropriate for this review ([plan:3431](/home/user/job-hunter/docs/superpowers/plans/2026-10-16-m0-foundation.md:3431)).
- **Fable: frozen worker lacks automatic replacement — ACCEPTED-AS-RULED.** Manual restart is explicitly carried into the M5 runbook ([plan:3432](/home/user/job-hunter/docs/superpowers/plans/2026-10-16-m0-foundation.md:3432)).
- **Fable: stalled-job supersession documentation — ADDRESSED.** [tasks.py:331](/home/user/job-hunter/backend/src/jobhunter/worker/tasks.py:331) identifies eligible tasks and explains the attempt-specific protection for `run_collect`.

**Regression-test assessment.** The substantive fencing, auth, retention and metrics tests force the failure conditions and assert durable outcomes; their reported RED failures match the old code’s behavior. C17 strengthening intentionally passes old code—it adds missing proof rather than repairs a broken attempt predicate. Some structural/shutdown REDs are missing-API failures, not behavioral demonstrations; the useful shutdown evidence is the budget-consumption/hanging-close assertion. No sleep-based race proof was introduced.

### New breakage in the fix diff

No new Critical, Important or Minor defect identified in the inspected diff.

### Out-of-scope observations

Later-milestone handlers, production deployment and full browser workflows remain outside this review. Reported suite results were not independently reproduced.

### Verdict: APPROVE

The original blocking scenarios are closed, and the controller’s alternative resolutions are sound within M0’s scope. The fix diff introduces no identified regression requiring another change.