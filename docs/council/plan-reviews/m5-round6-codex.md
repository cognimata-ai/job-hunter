**Verdict: REJECT**

I reviewed the brief, round-5 review and response, revised plan, binding rulings, relevant specification and research digests, M4 conventions, and supporting code at HEAD `ca16cd1`. This was a static review: no network, Docker, or sub-agents; no `.secrets/`, `.fixtures-raw/`, keys, or credentials read; nothing changed.

**Round-5 findings — disposition**

| # | Finding | Resolved? |
|---|---|---|
| 1 | Calls created after manifest computation escape profile redaction | **Partially.** Admission cancels existing operations, but a racing proposal request can recreate one afterward. See finding 1 below. |
| 2 | Restore invalidation puts exports into states replay cannot purge | **Yes.** The state table accepts content-free terminal states, and restore invalidation follows replay (P:528, P:538, P:757). |
| 3 | Local restic repository volume is unwritable by the shipper | **Yes.** The backup image creates `/data/restic-repo` as `10001:10001`, mode `0700`, with fresh-volume coverage (P:361, P:369). |

**Remaining and new findings**

1. **MAJOR — A racing proposal request can reopen the supposedly closed call set.** P:463, P:466; S:6462, S:6473; `backend/src/jobhunter/api/routes/profile.py:146`, `:457`, `:467`; `backend/src/jobhunter/queries/proposals.py:332`; `backend/src/jobhunter/llm/spend_ledger.py:288` — The proposal route reads the active version without locking `active_config`. A request can read version V, pause while intervening activations make V purgeable, then resume after A0 cancels V’s operations. `request_proposal` checks only that V exists; it can recreate the cancelled operation of a still-running proposal. A1 waits for that request’s transaction, but does not cancel its newly queued operation. That operation can cache the profile and reserve a call after A2, escaping the immutable redaction set exactly as in round 5. — Serialize the route’s active-version selection and proposal creation with A0, taking `active_config FOR SHARE` before idempotency and domain locks, or refuse a profile fence inside `request_proposal` under its version lock. Add a regression covering a request paused before proposal creation, followed by reservation after A2 and settlement after completion.

2. **MAJOR — The new restore-state test requires an impossible backup.** P:120, P:526, P:761; S:2858 — `test_restored_exports_follow_the_state_table` requires one `running` export named by the manifest and another `running` export not named by it, both in the same backup set. Task 1’s `UNIQUE exports_one_running ((true)) WHERE status = 'running'` rejects that fixture. A mocked database accepting it would bypass the schema constraint the restore must support. — Parameterize the named and unnamed running-export scenarios into separate valid backup sets, retaining the unique index and checking replay and invalidation against the real schema.

### Challenges to rulings

None.