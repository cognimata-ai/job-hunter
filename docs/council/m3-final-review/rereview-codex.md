# M3 final review, fix round 1 — re-review — Codex

Reviewed all seven commits in `b399afb..f93d6d0`, both first reviews, the fix brief/report, rulings F1-a–F1-e, and the relevant spec and plan requirements. Nine selected unit cases passed using `.venv/bin/python -B -m pytest -s -W error -q -p no:cacheprovider`; `uv` was blocked by its cache lock on the read-only filesystem. In-memory probes checked publication repairs, gate counting and resume SQL; three publication regression cases failed against the pre-fix module loaded in memory. PostgreSQL integration tests were inspected, not executed. Nothing in the tree, index or git state changed; no prohibited data was read, no network or paid calls were made, and `JOBHUNTER_FAKE_LLM=1` stayed set.

### Verdict

**REQUEST CHANGES** — the original defects are substantially addressed, but C introduces a recovery path that restores an old-policy critic review into a new-policy attempt.

### Findings addressed

- **Codex I1 → A — fixed.** [validate.py:385](/home/user/job-hunter/backend/src/jobhunter/settings/validate.py:385) counts distinct active user cases with a primary result belonging to this run. The [membership-change regression](/home/user/job-hunter/tests/integration/test_eval_gate.py:259) would fail before the fix because the completion-time count remains 30; the existing full-cohort test still has 30 eligible observations.
- **Codex I2 → B — fixed, within F1-d.** [publication.py:210](/home/user/job-hunter/backend/src/jobhunter/scoring/publication.py:210) carries confirmations by surviving fact position. The [publication regressions](/home/user/job-hunter/tests/unit/test_contested.py:510) passed; certification, other-language and both-confirmed collision cases failed against the pre-fix code. The inspected [recomputation regression](/home/user/job-hunter/tests/integration/test_det_results.py:390) requires 80/85/90 and a binding penalty, which the old unconfirmed map cannot produce.
- **Fable I1 → C — partly fixed.** [targets.py:266](/home/user/job-hunter/backend/src/jobhunter/scoring/targets.py:266) and [load_context.py:90](/home/user/job-hunter/backend/src/jobhunter/graphs/score/nodes/load_context.py:90) preserve the batch draft across policy changes. The [follow-up regression](/home/user/job-hunter/tests/integration/test_score_target_nodes.py:343) and [claim-link regression](/home/user/job-hunter/tests/integration/test_atomic_claim.py:185) fail under the old predicates, but omit a previously saved critic note; see R-I1.
- **Fable M1 → D — fixed.** [RUNBOOK.md:116](/home/user/job-hunter/docs/RUNBOOK.md:116) explains the alert, supported actions and missing operator recovery path. Checked against the transition registry and poll implementation; this documentation change has no executable regression test.
- **Fable M2 / Codex parked 3 → E — fixed.** The [predicate agreement test](/home/user/job-hunter/tests/unit/test_admission_allocation.py:292) and four condition-removal cases passed; each mutation is detected. The tuple exemption follows F1-c.
- **Fable M3 / Codex parked 2 → F — fixed.** [V19.md:54](/home/user/job-hunter/docs/spikes/V19.md:54) and [V19.md:184](/home/user/job-hunter/docs/spikes/V19.md:184) identify the measured corpora, separate reviser framing and explain that constants are unaffected. Verified by documentation diff; no executable regression test applies.

### Regressions and new findings

Publication probes preserved surviving confirmations and renamed evidence after a predecessor was dropped and a list tail was cut. Mixed collisions remained unconfirmed. The gate SQL counted 10 despite repeat, adversarial and other-run rows; a full primary cohort counted 30, then 29 after deactivation. No additional A/B regression was found.

C retains the same-policy restriction for ordinary online resumes. Batch eligibility and the smoke copy remain equivalent because both already exclude `resume_draft_id` targets. Hold and follow-up funding logic is unchanged; the initial follow-up now avoids the extra generator call.

#### Critical

None.

#### Important

**R-I1 — Batch draft inclusion also restores critic notes from excluded policies** — [load_context.py:91](/home/user/job-hunter/backend/src/jobhunter/graphs/score/nodes/load_context.py:91), [load_context.py:103](/home/user/job-hunter/backend/src/jobhunter/graphs/score/nodes/load_context.py:103).

A batch creates draft D under P1. Its online follow-up reviews D under P1, saves an accepting critic note, then crashes before publication. The operator changes the evaluation policy to P2 before reclaim.

The widened chain correctly includes D’s batch attempt and excludes the P1 follow-up attempt. However, `_NOTES` filters by **the draft’s owning attempt**, without checking the note’s owning attempt or policy. It therefore restores the excluded follow-up’s P1 review. [steps.py:654](/home/user/job-hunter/backend/src/jobhunter/scoring/steps.py:654) returns that saved acceptance, and [routing.py:115](/home/user/job-hunter/backend/src/jobhunter/scoring/routing.py:115) publishes immediately despite newly computed triggers. A P2 change requiring stronger critic review can thus be bypassed.

The in-memory SQL probe reproduced chain `[batch, new_attempt]` while restoring the old follow-up’s note; production routing returned `publish(accepted)` with a new T3 trigger. Filtering the note owner by policy removed the stale note. This violates §9.4.1’s same-policy requirement for resumed critic work.

**Fix:** retain the batch exception for draft selection, but constrain restored critic notes and round counts by their owning attempt’s policy. Add a regression that crashes after the follow-up’s critic-note commit, changes policy, and verifies that the draft survives while the old review does not.

#### Minor

None.

### Challenges to rulings

None.