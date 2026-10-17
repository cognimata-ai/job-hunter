### Strengths

- Strong foundations: transactional queue deferral, database-enforced transitions, ownership CAS predicates, per-attempt checkpoint threads, and durable failure alerts.
- Auth correctly commits failed attempts, rotates sessions, rejects replayed TOTP steps, and rechecks password changes before issuing sessions.
- Meaningful real-Postgres and process-crash tests, including lock barriers and committed-state assertions. Deployment uses pinned images, restricted roles, non-root processes, and scoped secret mounts.

I independently reviewed `cddb776..b2dc1dc` against the spec, plan, ledger, and known items. The repository remains unchanged. I did not rerun tests or start containers; the supplied green CI results do not cover the interleavings below.

### Issues

#### Critical (Must Fix)

**1. Operation takeover skips precisely the owners that need fencing.**  
[worker/operations.py:142](/home/user/job-hunter/backend/src/jobhunter/worker/operations.py:142)

The older-epoch sweep uses `FOR UPDATE SKIP LOCKED`. Unlike periodic stale recovery, takeover must invalidate **every** older running operation before committing.

**Failure scenario:** A holds an operation row lock during a heartbeat or owner transaction. Its singleton-lock session dies. B acquires the singleton lock, skips that operation, and commits its new epoch. A’s original `claim_id` remains valid: its heartbeat, owner lock, and finalization predicates still succeed after takeover. The watchdog’s eventual exit cannot undo those writes.

**Fix:** Wait for operation owner locks, using the existing takeover lock timeout to roll back the entire takeover if necessary. Add a held-operation-row test proving that successful takeover rejects the previous claim’s heartbeat and finalization. The current unlocked-row takeover test does not establish this.

**2. A displaced worker can obtain fresh ownership after takeover.**  
[worker/runs.py:243](/home/user/job-hunter/backend/src/jobhunter/worker/runs.py:243), [worker/operations.py:189](/home/user/job-hunter/backend/src/jobhunter/worker/operations.py:189)

Initial admission trusts the caller-supplied epoch. Neither `queued → running` nor `pending → running` verifies that it belongs to the current worker. Comparing epochs only when resuming an already-running attempt leaves this hole open.

**Failure scenario:** A pauses after fetching a job but before admission. B takes over; its sweep ignores queued runs and pending operations. A resumes before its watchdog exits and claims that work using its obsolete epoch. It receives a fresh, valid attempt/claim token and can finalize successfully. Existing owner CAS checks therefore do not fence it.

**Fix:** Serialize admission with epoch advancement using a transactional current-worker guard, with a consistent lock order. A separate preflight check is insufficient. Add a pause-before-admission takeover test for both runs and operations; C17 currently exercises an already-running attempt.

#### Important (Should Fix)

**3. Epoch claiming can overwrite—and evict—the actual singleton holder.**  
[worker/epoch.py:115](/home/user/job-hunter/backend/src/jobhunter/worker/epoch.py:115)

**Explicit escalation of the ledger’s deferred check-then-upsert minor:** the `pg_locks` check and epoch mutation use a pooled connection separate from the lock-owning connection.

**Failure scenario:** A passes `_HOLDS_LOCK`, then pauses before the upsert. Its lock session is terminated. B acquires the lock, claims an epoch, and starts work. A resumes and unconditionally advances the epoch again, sweeps B’s work, and replaces B’s heartbeat identity. B’s watchdog now exits despite B still holding the singleton lock. A subsequently exits too. This disrupts healthy replacement work and consumes recovery attempts; watchdog “self-healing” does not prevent it.

**Fix:** Tie the epoch/sweep transaction to the actual lock-owning session so losing that session prevents the transaction from committing. Test termination specifically between ownership verification and epoch mutation.

**4. TOTP secret replacement is not included in the credential recheck.**  
[auth/users.py:47](/home/user/job-hunter/backend/src/jobhunter/auth/users.py:47), [api/routes/auth.py:130](/home/user/job-hunter/backend/src/jobhunter/api/routes/auth.py:130)

`confirm_unchanged` compares the password hash and `totp_enabled`, but not the TOTP secret or a credential revision.

**Failure scenario:** A login verifies secret A and waits before its decision transaction. The operator disables TOTP and enables replacement secret B, leaving the password unchanged. The enabled flag is again true and `totp_last_step` was cleared. The pending login passes the recheck, claims its old-secret step, and receives a session without proving possession of B.

**Fix:** Carry and compare a credential revision covering password/TOTP changes, or compare the verified TOTP secret identity under the user-row lock. Add a barrier test around disable/re-enable with a different secret.

**5. Failed checkpoint cleanup can permanently orphan the remaining checkpoints.**  
[privacy/retention.py:74](/home/user/job-hunter/backend/src/jobhunter/privacy/retention.py:74), [privacy/retention.py:168](/home/user/job-hunter/backend/src/jobhunter/privacy/retention.py:168)

The runner continues to delete old runs after checkpoint deletion fails. Discovering checkpoint threads requires the run and its `run_attempts`, which run deletion cascades away.

**Failure scenario:** After extended downtime, a finished run is older than 397 days and still has checkpoints. `adelete_thread` fails transiently; the subsequent run deletion succeeds. Tomorrow’s checkpoint query cannot discover that thread, leaving its blobs/writes indefinitely. The next successful empty scan can even resolve the failure alert.

**Fix:** Preserve parent/discovery rows until dependent checkpoint cleanup succeeds, or retain a durable cleanup manifest. Test a checkpoint-rule failure with a run old enough for deletion; the existing failure-isolation test uses an unrelated rule.

**6. Concurrent login requests bypass the intended expensive-work throttle.**  
[api/routes/auth.py:239](/home/user/job-hunter/backend/src/jobhunter/api/routes/auth.py:239), [auth/passwords.py:19](/home/user/job-hunter/backend/src/jobhunter/auth/passwords.py:19)

The read-only lockout check precedes unrestricted `asyncio.to_thread` password verification. Failures are recorded only afterward.

**Failure scenario:** A burst of invalid logins all passes the precheck before the first failures commit. Each verification uses 64 MiB. On a host allowing at least 16 executor threads, concurrent verification alone reaches 1 GiB, exceeding the [API container’s limit](/home/user/job-hunter/deploy/compose.dev.yaml:102) once application overhead is included. Queued verifications also continue after lockout becomes active.

**Fix:** Bound concurrent and queued password verification before scheduling Argon2, and recheck lockout after acquiring admission. Preserve the constant verification path without holding a database transaction during hashing. Add a concurrent-request test that measures verifier admission, rather than only sequential lockout responses.

#### Minor (Nice to Have)

**7. Run metrics omit missed and overlapping slots.**  
[worker/tasks.py:89](/home/user/job-hunter/backend/src/jobhunter/worker/tasks.py:89), [worker/tick.py:224](/home/user/job-hunter/backend/src/jobhunter/worker/tick.py:224)

Tick inserts terminal `missed` and `skipped_overlap` runs but never increments `jobhunter_runs_total` for them. After downtime or sustained overlap, the database records these outcomes while the terminal-status counter omits them.

**Fix:** Consume `TickReport` after transaction commit and increment the corresponding counters only for newly inserted rows.

### Plan/spec issues

- **Critical: the prescribed stack fixture destroys the normal development database.** [Plan:3081](/home/user/job-hunter/docs/superpowers/plans/2026-10-16-m0-foundation.md:3081), implemented at [test_dev_stack.py:43](/home/user/job-hunter/tests/stack/test_dev_stack.py:43). The fixture uses the normal `jobhunter-dev` project and executes `down -v`; implementation also does this before startup. An operator who has populated the development database and runs the documented opt-in stack tests loses that database. Use a uniquely named test project, test-owned volumes and probe names, and isolated ports/networks. Fail safely on conflicts instead of removing existing development resources.

- **The zombie-admission ruling weakens the binding fence guarantee.** [progress.md:244](/home/user/job-hunter/.superpowers/sdd/2026-10-16-m0-foundation/progress.md:244) accepts resumed zombies starting jobs because “every owner write is fenced.” Issue 2 supplies the counterexample: newly acquired tokens remain valid. Acceptance must require obsolete workers to fail admission.

- **The epoch check was incorrectly treated as closing the race.** [progress.md:169](/home/user/job-hunter/.superpowers/sdd/2026-10-16-m0-foundation/progress.md:169), with the remaining race downgraded at [progress.md:175](/home/user/job-hunter/.superpowers/sdd/2026-10-16-m0-foundation/progress.md:175). Issue 3 demonstrates eviction of the healthy replacement. Require atomic ownership protection, not eventual watchdog recovery.

- **Retention error isolation needs dependency exceptions.** [progress.md:243](/home/user/job-hunter/.superpowers/sdd/2026-10-16-m0-foundation/progress.md:243) requires later rules to continue indiscriminately. Issue 5 shows why independent rules may continue while dependent parent deletion must wait.

### Declined to judge

- Source ingestion, identity resolution, embeddings, quota accounting, and full collection invariants: M1; M0 intentionally runs a no-op graph.
- CV parsing, profile activation, settings workflows, and associated deferred foreign keys/seeds: M2.
- LLM spending, scoring, batch recovery, and paid-dispatch crash guarantees: M2–M3 and later.
- Real operation handlers and source/profile-specific purge-fence expansion: staged with their owning subsystems; generic ownership was reviewed here.
- Full SPA workflows and real-browser secure-cookie behavior: M4; M0 supplies the skeleton and client.
- Backup/restore, manifest replay, identity advancement during restore, full purge workflows, and operational key rotation: M5.
- Production TLS, host firewall, target-host network guarantees, and container vulnerability scanning: M5; development deployment was reviewed.
- Health fields and alert-catalogue entries for unimplemented subsystems: explicitly staged; current database/worker readiness was reviewed.

### Assessment

**Verdict:** REJECT

**Reasoning:** The foundation has substantial engineering discipline, but successful takeover does not reliably exclude obsolete workers from valid ownership or subsequent writes. Those safety failures, the destructive stack-test workflow, and the auth/retention gaps require fixes and targeted regression tests before M0 is accepted.