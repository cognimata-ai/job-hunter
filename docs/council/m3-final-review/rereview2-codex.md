# M3 final review, fix round 2 — re-review — Codex

Read the full two-commit diff `013d829..91b4b57`, the brief, report, prior reviews, rulings F1-a–F2-a and relevant spec/plan requirements. Ten focused routing/T0 unit cases passed using the installed interpreter; seven in-memory SQL/restoration/routing scenarios passed, including a pre-fix reproduction. All four runbook queries passed an in-memory, migration-derived column check. The prescribed focused integration run was blocked before collection by `uv`’s read-only cache lock; PostgreSQL tests were inspected, not executed. Nothing in the tree, index or git state changed. No prohibited data, keys, network or paid calls were accessed; execution used `JOBHUNTER_FAKE_LLM=1`. No sub-agents were dispatched.

### Verdict

**APPROVE** — both findings are fixed, with no regression or new finding identified in the fix range.

### Findings addressed

- **R-I1 — fixed**, [load_context.py:108](/home/user/job-hunter/backend/src/jobhunter/graphs/score/nodes/load_context.py:108): both note and draft owners must belong to the chain. Pinned by [test_a_reclaimed_follow_up_restores_only_the_critic_notes_of_its_own_chain:395](/home/user/job-hunter/tests/integration/test_score_target_nodes.py:395), with `same_policy` and `policy_changed` cases. Without the fix, the changed-policy case fails because F2 publishes on F1’s note instead of writing its own. The report records that failure; my independent SQL/routing probe reproduced the bypass.
- **R-M1 — fixed**, [RUNBOOK.md:136](/home/user/job-hunter/docs/RUNBOOK.md:136): the query selects existing columns, and the description correctly identifies `dedup_key` and `context`. No committed test was added for this documentation change. The report records all four queries passing against migrated PostgreSQL; my independent column check passed all four and rejected the original query specifically for missing `subject`.

### Regressions and new findings

Same-policy online reclaims retain their notes. A reclaim of a reclaim retains earlier reviews across revision drafts. Batch follow-ups retain their reviews when reclaimed under their own policy, including when that policy differs from the batch’s. The recursive chain remains unchanged; the added predicate removes only notes whose writers are excluded.

T0 and the three-round limit retain the chain’s complete eligible note count. The last eligible note still supplies not-a-job-ad verification and cumulative fact confirmations. Existing publication regressions cover acceptance and confirmations ([test_publish.py:496](/home/user/job-hunter/tests/integration/test_publish.py:496)), verification (`:521`), two reclaims (`:541`) and the third critic round (`:588`); these were inspected.

No remaining reachable database read independently selects an outside-chain note. [TargetDraftStore.note:310](/home/user/job-hunter/backend/src/jobhunter/graphs/score/nodes/__init__.py:310) and [publication’s _last_review:236](/home/user/job-hunter/backend/src/jobhunter/scoring/publish.py:236) read explicit IDs supplied by the corrected restoration or the current critic. Validation, revision, subsequent critic confirmations and publication therefore consume the selected chain note. Publication preserves its stored confirmations through [stored_review:543](/home/user/job-hunter/backend/src/jobhunter/scoring/steps.py:543).

#### Critical

None.

#### Important

None.

#### Minor

None.

### Challenges to rulings

None.