**Verdict: REJECT**

## A. Round-4 finding

**RESOLVED in the plan.** Reviewed commit `285a76c`, its diff from `2bdc5d2`, the branch code, and specification rev 3.3. This is not an implementation approval.

**P:** [Task 14:1128](/home/user/job-hunter/docs/superpowers/plans/2026-10-20-m2-llm-profile-settings.md:1128), [Task 23:1721–1727](/home/user/job-hunter/docs/superpowers/plans/2026-10-20-m2-llm-profile-settings.md:1721), tests at 1751–1753. **S:** [§7.16.8](/home/user/job-hunter/docs/superpowers/specs/2026-10-16-job-hunter-design.md:2985), [§20.1](/home/user/job-hunter/docs/superpowers/specs/2026-10-16-job-hunter-design.md:6294). **Repo:** [transitions.py:435](/home/user/job-hunter/backend/src/jobhunter/db/transitions.py:435), [retention.py:196](/home/user/job-hunter/backend/src/jobhunter/privacy/retention.py:196).

The shared draft-lock protocol closes the reported stale-eligibility race. Retry-first makes retention recheck and skip the reopened draft. Purge-first makes retry wait, then reject unavailable input without transitioning or creating an operation. Draft-content deletion receives the same protection. A separate retention-lifecycle issue remains below.

## B. New findings

1. **MAJOR — A native-PDF retry can regenerate content while retaining its old `content_purged_at` marker.**

   **P:** [1128](/home/user/job-hunter/docs/superpowers/plans/2026-10-20-m2-llm-profile-settings.md:1128) permits native retries when the file remains; [1077](/home/user/job-hunter/docs/superpowers/plans/2026-10-20-m2-llm-profile-settings.md:1077) publishes new content; [1721](/home/user/job-hunter/docs/superpowers/plans/2026-10-20-m2-llm-profile-settings.md:1721) excludes already-marked drafts from cleanup. No reopening step clears the marker, and it is absent from the transition’s settable columns at 278. **S:** [§7.4 CHECKs](/home/user/job-hunter/docs/superpowers/specs/2026-10-16-job-hunter-design.md:1490), [§20.1 draft-content retention](/home/user/job-hunter/docs/superpowers/specs/2026-10-16-job-hunter-design.md:6300). **Repo:** [retention.py:283](/home/user/job-hunter/backend/src/jobhunter/privacy/retention.py:283), [transitions.py:370](/home/user/job-hunter/backend/src/jobhunter/db/transitions.py:370).

   **Failure scenario:** A native-PDF draft failed 31 days ago. The `draft_content` batch commits, setting `content_purged_at`; its file has not yet been purged. A retry between the separate batches is admitted because that file exists. It reopens the draft, protecting the file, and generates new `draft_json` and call `output_json`. The user then discards or confirms it. Even after another 30 days, content retention skips it permanently because the old marker remains set.

   An in-memory evaluation of the prescribed predicates confirmed this sequence passes the specified draft CHECKs but fails subsequent cleanup eligibility.

   **Expected fix:** Clear `content_purged_at` atomically when admitting a retry that regenerates content, allowing that update through the transition registry or an explicit same-transaction update. Add a test that pauses between content and file purges, retries successfully, then verifies newly generated draft and call content is removed 30 days after the draft next ends.

No other new BLOCKER or MAJOR finding identified. Inspection was read-only and offline; no API calls were made.