**Verdict: REJECT**

I reviewed the review brief, round-9 findings and response, revised plan and diff, binding rulings, relevant specification and research sections, and supporting code at HEAD `6c59ff2`. An isolated in-process probe confirmed the scope-pointer failure below. No network, Docker, sub-agents, or integration tests were used; no prohibited data, keys, or credentials were read; nothing changed.

**Round-9 findings — disposition**

| # | Finding | Resolved? |
|---|---|---|
| 1 | Failed retrieval permanently loses cancellation | **Yes.** P:527–528 specifies a separate bounded sender that counts retrieval failures toward three attempts and logs exhaustion before deletion. P:536 tests recovery and exhaustion. |
| 2 | Replay requires a worker-only provider client | **Yes.** P:415–416 and P:762 explicitly give replay no external-effect dependencies. P:766 and P:787 test replay and monthly restore without worker credentials or external calls. |
| 3 | Resolving a retained alert replays delete-everything | **Yes for the reported scenario.** P:528 freezes resolved-alert ids at A2; later resolution cannot change membership. However, the replacement settings postcondition introduces another replay trigger, finding 2 below. |

**New findings**

1. **MAJOR — The idempotency exemption names a nonexistent column.** P:325, P:528, P:533; S:6250; `backend/src/jobhunter/db/migrations/versions/0001_m0_baseline.py:120–130` — The new `keep: list[int]` mechanism preserves this request’s `api_idempotency` row through `keep=[its id]`. That table has no `id`: its primary key is `(route, idem_key)`, and Task 1 adds no surrogate identity. Consequently, Task 16 cannot construct or apply the specified exemption; ignoring it deletes the response needed for retries after logout. — Define an immutable exemption compatible with the actual key, or explicitly introduce a surrogate identity and wire its capture into the request/manifest interface. Test canonical serialization, live deletion and replay with the real schema.

2. **MAJOR — Reusing the retained default scope can replay a completed delete-everything.** P:529, P:532, P:762; S:6268; `backend/src/jobhunter/db/seed.py:186–200`, `backend/src/jobhunter/settings/scope_apply.py:224–237` — Step 10 deliberately retains the default scope, potentially below `max_ids.scope_versions`. After deletion, the user can apply a different scope and then save settings returning to the default. Hash reuse returns the retained id, and `set_desired_scope` places it in `pending_scope_version_id` until application finishes. This violates P:529’s “NULL or above the bound” postcondition. A migrate during that interval replays delete-everything and resets subsequent settings/profile pointers. The isolated probe of the existing function confirmed this pending-pointer assignment. — Check against the immutable scope-deletion set, allowing explicitly retained scope ids, rather than requiring every pending scope to exceed the bound. Test migrate while a return to the retained default scope is pending, asserting no replay or configuration changes.

3. **MAJOR — The new embedding cleanup writes an illegal status.** P:507; S:2057; `backend/src/jobhunter/db/migrations/versions/0005_m1_collection.py:519–532` — When another revision reuses a frozen embedding after A2, the plan sets `embedding_status = 'pending'`. The CHECK permits only `awaiting_embedding` and `embedded`. This cleanup therefore fails with a constraint violation and leaves the purge unfinished. — Set `embedding_status = 'awaiting_embedding'` and `embedding_id = NULL` together. Add the exact reuse-after-A2 test, asserting purge completion and subsequent re-embedding above the manifest bound.

### Challenges to rulings

None.