**Verdict: REJECT**

I reviewed the brief, round-6 review and response, revised plan, binding decisions, relevant specification and research digests, M4 conventions, and supporting code at HEAD `c067f1f`. This was a static review: no network, Docker, sub-agents, or tests; no `.secrets/`, `.fixtures-raw/`, keys, or credentials read; nothing changed.

**Round-6 findings — disposition**

| # | Finding | Resolved? |
|---|---|---|
| 1 | A racing proposal request can reopen the closed call set | **Yes.** Proposal creation now holds `active_config` before selecting the version; A0 evaluates eligibility under its conflicting lock and cancels existing operations. The invariant guard and race tests cover the original failure (P:463–473). |
| 2 | The restore-state test requires an impossible backup | **Yes.** Two parametrized backup sets each contain at most one running export, with the real unique index retained (P:766). |

**New findings**

1. **MAJOR — Golden-case activation can invalidate profile-purge eligibility after admission.** P:293–295, P:302, P:463, P:468–469; S:6434, S:6470–6475; `backend/src/jobhunter/scoring/golden.py:494`, `backend/src/jobhunter/api/routes/golden.py:215`, `backend/src/jobhunter/db/migrations/versions/0006_m2_llm_profile_settings.py:335` — Task 8 protects `set_flags` through the job lock and a job-fence check, with tests for jobs/source fences only. A profile fence matches through a tuple, and profile-purge admission locks the profile rather than the job. Consequently, an inactive case can become active after A0 accepts its profile for purging but before A2 selects the inactive cases. A2 then excludes that case while the purge removes its profile content, leaving an active case that cannot run and violating the retention exemption. — Serialize case activation with A0, for example by taking `active_config FOR SHARE` before the job/domain locks, and explicitly refuse activation when the case’s profile is fenced or purged. Test both orders: activation first prevents admission; admission first prevents activation.

2. **MAJOR — The shared privacy seed requires three mutually exclusive in-flight submissions.** P:445, P:447, P:515; S:2618–2619, S:5795; `backend/src/jobhunter/db/migrations/versions/0008_m3_scoring.py:465–470` — `seed_two_year_old_graph` requires simultaneous `submitted`, `preparing`, and `submit_ambiguous` submissions. `batch_one_in_flight` permits only one of these globally. Setting `released_at` cannot bypass this legally: the CHECK restricts it to `released`, `ended`, or `reconciled`. The seed and dependent deletion/replay tests therefore cannot run against the required schema. — Parameterize the seed’s live submission state and run the delete-everything mapping scenarios in separate valid databases. Keep the unique index and CHECK constraints enabled throughout.

### Challenges to rulings

None.