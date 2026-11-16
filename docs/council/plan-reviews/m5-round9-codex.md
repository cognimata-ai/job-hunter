**Verdict: REJECT**

I reviewed the brief, round-8 review and response, revised plan, binding decisions, relevant specification and research sections, and supporting code at HEAD `f5bf19a`. An isolated in-process probe confirmed the cancellation failure described below. No network, Docker, sub-agents, or integration tests were used; no prohibited directories, keys, or credentials were read; nothing changed.

**Round-8 findings — disposition**

| # | Finding | Resolved? |
|---|---|---|
| 1 | Delete-everything can erase cancellation before delivery | **Partly.** P:521 now sends before deleting and retries after a crash. However, the chosen helper can return before attempting cancellation; see finding 1. |
| 2 | Synthetic-profile eligibility contradicts its exclusion | **Yes.** P:467 explicitly requires `NOT synthetic_eval`; P:483 tests active, inactive and absent cases. This closes the reseeding race in `backend/src/jobhunter/scoring/golden.py:647`. |

**Remaining finding**

1. **MAJOR — A failed retrieval still loses the cancellation permanently.** P:521, P:525; S:6525; `backend/src/jobhunter/scoring/batch_results.py:208–219` — `cancel_requested` returns `False` immediately when retrieval fails, before entering its three-attempt cancellation loop. The isolated probe produced one retrieval, zero cancellations and only `batch_cancel_retrieve_failed`. Task 16 then deletes the submission, eliminating future delivery. Its new failure test covers rejected cancellations, but not this prerequisite failure. — Define a purge-specific policy that preserves the cancellation intent through retrieval failures and provides the required bounded attempts and logged give-up before deletion. Test a transient retrieval failure followed by recovery, and exhaustion of that path.

**New findings**

2. **MAJOR — Replaying delete-everything now requires a worker-only provider client.** P:360, P:410, P:517–521, P:751, P:773; S:6145, S:6272; `backend/src/jobhunter/config.py:119`, `backend/src/jobhunter/llm/batches.py:347–350` — Task 25 calls the same `execute` in replay mode, but Task 16 unconditionally sends recorded cancellations using the worker’s Batches client. Neither migrate nor backup/restore has that client or its Anthropic credential; constructing an unconfigured client makes retrieval raise `LLMNotConfigured`, which `cancel_requested` does not catch. A dump containing a submitted batch followed by a delete-everything manifest therefore cannot replay as written. Supplying worker credentials would also let the monthly scratch restore affect real provider work. — Make external cancellation and dead-man delivery explicitly live-only, provide their live dependency wiring, and keep replay limited to database/file effects. Test delete-everything replay and monthly restore with a submitted batch, no worker credentials, and clients that reject any external call.

3. **MAJOR — Resolving a retained alert can replay a completed delete-everything operation.** P:338, P:520–522, P:750–751; S:6250, S:6268; `backend/src/jobhunter/observability/alerts.py:50–54` — The manifest uses bounded `row_delete ... all`, exempting currently open alerts. After completion, resolving a retained alert leaves a bounded row that no longer qualifies for that exemption. Its postcondition now fails, so the next migrate replays the entire delete-everything procedure. Step 8 unconditionally resets settings and clears the active profile/tuple, including choices made after the original deletion. — Freeze the alert deletion set in the manifest, for example as explicit ids of resolved alerts selected at computation time; use that same immutable set for execution and verification. Test completing deletion, activating a new profile/settings, resolving a retained alert, then migrating: the old manifest must remain satisfied and the new configuration unchanged.

### Challenges to rulings

None.