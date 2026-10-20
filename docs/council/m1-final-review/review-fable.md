# M1 final whole-branch review — Claude Fable 5.1

Range e199775..0cd6711 (53 commits, 399 files). Read-only; no mutation of the tree. Local checks run: the
fixture PII scan (`tests/contract/test_fixture_pii.py`, 146 passed), greps over docs and fixtures for
e-mails/phones, an unreferenced-definition scan over `backend/src`. No live or paid test was run, no external
API was called, and no DB test suite was started (no doubt below needed one; the three Important findings are
decided by code paths that have no alternative branch).

### Verdict

APPROVE WITH CHANGES — the crash, fence, lock-order, quota-admission and privacy properties hold end to end,
but three spec obligations are silently not met (SmartRecruiters detail re-fetch rule, the page-2 admission
rule under JSearch cursor reuse, and the T7-b SerpApi lockout alert, which is unreachable from the only path
that would need it); none is a data-corruption or double-billing defect, so none is Critical.

### Focus questions 1–8

**1. Crash safety end to end.** Reserve → marker → send → settle is one protocol in
`backend/src/jobhunter/sources/acquire.py`: the resolution order of §6.2 rule 2 is `_Acquisition.resolve`
(363–397); an own `reserved` row is continued, a superseded one released under the new owner's `FOR SHARE`
(`_reserved`, 430–458: `lock_run_owner` then `release_never_dispatched`, so a stale marker finds no predicate);
`dispatched`/`ambiguous` paid rows are closed `ambiguous` and never re-sent (392–396), free/Adzuna rows get
`attempt_seq + 1`; the marker (`ledger.mark_dispatched`, `quota/ledger.py:681–695`) is `reserved → dispatched`
under the owner row and raises `FencedOut` before any send; response persistence and settlement are one
transaction (`_settle_in`, 661–718) retried in process only on 40001/40P01 with the response in hand
(`_settle`, 626–659), so a lost connection leaves `dispatched` and the next resolution charges it. A takeover
after the marker cannot stop the send; the old owner's settlement fails `lock_run_owner` and the entry is
`ambiguous` once (counted once). `not_billed` without `SETTLEMENT_EVIDENCE` is a `ValueError` before any
write (`ledger.settle`, 723–724). Statistics are computed from durable rows once, in the finalize transaction
(`sources/coverage.py:605–708`, `graphs/collect/nodes/finalize.py:618–649`), and the run end is read back
through `runs.run_completion` so a graph that raises after its committed finalize writes nothing
(`worker/runs.py:686–712`). A run that loses its executor is picked up by `stale_runs` (`runs.py:513–544`)
until `MAX_RESUMES`, then `failed('resume_exhausted')`; every node has a `TimeoutPolicy`
(`graphs/collect/graph.py:83–97`), so a hung node ends the attempt instead of beating forever. I found no
double-send, no evidence-free refund and no run left neither finalized nor resumable. One narrow loss window
is noted as Minor 1 (`known_native_ids` is a DB read between the response and its settlement, 537–540).

**2. Lock order across modules.** Every transaction I traced follows §3.3.3 with the T19-a/T20-a settlement
of the guard position: identity `active_config FOR SHARE` → jobs `FOR UPDATE` in one ordered statement
(`dedup/merge.py:97–100`, `lock_jobs`) → `lock_run_owner` → postings → keys → groups → `ranking_state`
(`dedup/identity.py:491–575`); regroup takes the company advisory lock first and only in a savepoint
(`dedup/groups.py:517–552`), then jobs with every member of every group it may touch, then the guard, then
group rows; snapshots and the stale sweep lock the postings' jobs with co-members, then the guard, then
posting rows, then `ats_snapshots`/`source_instances` (`availability/snapshots.py:329–341`,
`availability/rules.py:217–220`); the ledger paths take runs → `quota_windows` → ledger → `fetch_requests` →
`source_instances` (`acquire.py` module docstring, `_settle_in`), and the reconciler takes `worker_heartbeat
FOR KEY SHARE` → runs `FOR UPDATE` → window (`ledger.py:851–900`), which never conflicts with the heartbeat
(`worker/epoch.py:17–24`). Finalize takes runs → `coverage_cells` → `source_instances`
(`coverage.py:503–506, 576`). No pair of transactions acquires two of these resources in opposite order:
the only cross-module pairs that share rows (identity vs `group_representatives`, identity vs
`hard_filter_completeness`, snapshot apply vs `ats_mass_events`, retention vs snapshot apply) all lock job
rows in one ordered statement first or touch different rows. I found no deadlockable pair.

**3. Coverage truthfulness.** A `covered` row is written only for a search traversal that reached `END`
(`coverage.py:295–310`) or an Arbeitnow read from page 1 to the end of the feed (`sources/arbeitnow.py`
`covered_span`, 229–239: `stopped_by != "end"` → `None`; T10-a), and `last_covered_at` advances only from a
committed `covered` row that is contiguous or whose gaps are closed/abandoned (`finalize_cells`, 655–671),
or a structurally complete snapshot's `taken_at` (672–678). Snapshot completeness demands END, contiguous page
numbers, counts and ids (`sources/ats/common.py:436–485`); an interrupted board writes no snapshot
(`nodes/fetch.py:137–163`). The 720 h abandonment splits a straddling gap and keeps its newer part open
(`coverage.py:368–388`). The JSearch cursor-reuse traversal records the *old* window, which is the window it
proves (`quota/planner.py:446–449`, `nodes/fetch.py:92–110`). Truthfulness holds. What does not hold is the
admission side of cursor reuse — Important 2: the reuse plans page 2 (and page 3, 4, …) without the ≥ 7-new
rule, so paid pages the spec forbids are sent, although what they cover is recorded correctly.

**4. Availability safety.** Closure needs either `absent_snapshots ≥ 2` and 24 h since `first_absent_at` on
a structurally complete, non-mass snapshot (`snapshots.py:136–147, 366–368`) or a confirmed/acknowledged mass
event over two complete snapshots ≥ 24 h apart (`_WINDOW`, 165–172; `_CONFIRMED_DUE`, 180–185). Absence
counting is idempotent per snapshot (`last_absent_snapshot_id IS DISTINCT FROM`, 140), saturates at 2, and an
incomplete or mass snapshot only makes postings `unknown` (360–361) without counting. A board 404 or a failed
first page yields no snapshot (`traverse.py:466`: `snapshot = … if t.pages else None`), a shutdown or takeover
mid-board yields none (`fetch.py:160`), and the mass test is on active postings on both sides (T19-b,
231–238). The stale sweep excludes ATS instances and reads only `last_seen_at`, which failures never move
(`rules.py:94–98`). `active → unknown` never moves the change time backwards (`_UNKNOWN_AT`, 130–135). No
closure path without the §8.11 evidence.

**5. Identity.** Merges happen only through `_decide` (`identity.py:305–325`): never with a `different`
decision among `J`, never under an exclusive conflict (which opens an `identity_conflicts` row instead,
`merge.py:383–421`), and only into the lowest id with the others' rows held. Postings are inserted
`ON CONFLICT (source_instance_id, native_id) DO NOTHING` and otherwise locked `FOR UPDATE`
(`identity.py:158–166, 463–483`), so no posting is duplicated; `merge_jobs` moves every posting and key of the
from-job in one statement each (`merge.py:131–132, 350–353`) and refuses an already-merged or fenced job;
unmerge moves back exactly the event's postings still on the into-job, re-owns only the keys the two jobs own
(T18-f, 174–183) and is refused once the into-job merged on (`merge.py:499–505`), so no posting is lost;
`split_member`/`confirm_group` act only on held members (`groups.py:781–853`). The job-set read-then-lock
round with a savepoint (`identity.py:366–381`) closes the window between the key lookup and the lock. I found
no path that merges distinct jobs, loses or duplicates a posting.

**6. Quota.** I3 cannot be over-spent: `used + units > limit_units` is checked under the window's
`FOR UPDATE` with concurrency 1 per provider (`quota/pacing.py:127–128`, `ledger.py:614–678`), and nothing
refunds without evidence. A lagging SerpApi read is skipped by T7-a (`quota/windows.py:319–322`) and a
rollover keeps the observation clock and hour figures (`_extrapolated_successor`, 235–248); JSearch exhaustion
is sticky until the reset with a 12 h re-probe (`pacing.py:119–122`, `windows.py:399–418`). SerpApi is not
locked out permanently in the *normal* renewal case: `plan_fallback` applies a fresh read to the projection
copy (`ledger.py:927–931`) and the first admitted reservation persists it. It **is** locked out silently in
the abnormal case T7-b was written for (the account never reports a newer renewal date): the mandated
`quota.serpapi_renewal_stale` alert lives in `check_serpapi_renewal` (`ledger.py:540–556`), which only
`observe_account` calls (584), which only `reserve` (640) and `record_account` (611) reach — and a
`window_cap` projection writes `skipped` rows and reserves nothing (`planner.py:533–558`). Important 3.

**7. The run report and alerts.** The report holds ids, counts, codes and `<source_id>/<instance_key>`
(a board as `<ats>/instance:<id>`, `finalize.py:406–410`); alert `dedup_key`s are `code:subject` with
`instance:`, `cell:`, `job:`, `conflict:`, `provider:`, `run:` subjects; no log line in the M1 code carries a
title, description, company name, e-mail or private URL (grep over every `log.*(` call; the `source_request`
line logs host, path and the credential-stripped query, `sources/http.py:241–257`, which §5.2 allows — note
that an ATS path embeds the board slug by design). Resolution: `source.*` resolve on success
(`sources/breaker.py:118–126`); `run.partial` per cause, `run.failed`/`run.orphaned_queue` on a succeeded
end and `quota.fallback_active` on a run without fallback (`finalize.py:598–615`); `embedder.unavailable`
when the backlog is empty (`embeddings.py:487–491`); `ats.mass_disappearance` on clear/confirm/acknowledge
(`snapshots.py:291–303`); `quota.exhausted` on a validated observation with remaining
(`ledger.py:771–772`); `coverage.too_broad` when covered (`coverage.py:711–714`). Alerts that stay open:
`quota.serpapi_foreign_usage` resolves only on a *persisted* SerpApi read after 24 h (`ledger.py:366–367`),
i.e. never while JSearch keeps serving (Minor 3); `dedup.ambiguous_grouping` is a known deferred minor.

**8. Plan drift.** Delivered as planned: migration 0005 with the eight machines and triggers, every adapter,
the ledger with windows/pacing/fallback/dry-run projections, receipts and the protocol, planner and fallback,
identity/merges/unmerge/conflicts/groups, availability and snapshots, the embedder image and client, the
graph with run end and report, worker wiring, retention, the `contract` CI job and the free-source smoke, the
crash cases C1–C7 (`tests/crash/test_collect_crashes.py:55–301`), e2e and worker tests, `scripts/smoke_m1.py`,
spikes V3/V7/V8/V9/V10/V17/V18, `cli_fixtures.py`. Missing: the writer of `postings.list_item_sha256` /
`detail_fetched_at` (§5.3.5; no task owns it — Important 1); Ruling T3-a's 231 stop words (recorded as
Deviation 26 and T25-1, already in the fix wave). Built beyond the row: nothing that the plan defers; the M4
functions (`unmerge`, `resolve_conflict`, `split_member`, `confirm_group`, `acknowledge`, `replay_rejected`,
`watchlist_validate`) are the plan's Deviation 20/Carry-forward 3. Dead code: Arbeitnow `plan_legs`, `step`,
`seek_page` and the `seek` leg kind are referenced by tests only (`arbeitnow.py:123–128, 185–226`;
`traverse.py:374` builds one fresh leg), and Carry-forward 7's "planned but never run" overstates it (Minor 5).

### Strengths

- One acquisition protocol for every paid and free page, with the receipts resolved in the spec's order,
  three explicit fault points per page and a breaker gate inside the reserving/marking transaction
  (`sources/acquire.py`); the crash matrix C1–C7 runs on the real worker process.
- The ledger's recovery writes (`release_never_dispatched`, `mark_ambiguous`) are the single path used by the
  acquisition, the shutdown and the reconciler, each under the entry's own window lock, with probe columns
  restored (`quota/ledger.py:793–832`).
- All job-row locking goes through one ordered statement that includes group co-members (`merge.lock_jobs`),
  with a savepoint-based re-read round in identity, snapshots and regroup; the guard position after the job
  rows is applied uniformly (T19-a).
- Finalize counts invariants, partial causes and the report from durable rows in the transaction that ends
  the run; `finalize_cells` is applied once and advances `last_covered_at` only from a committed `covered`
  row.
- Privacy: no raw recordings tracked, the fixture PII scan passes, the scrubber history was rewritten, the
  report and logs carry ids and codes only, `redirect_url` lives only in `outbound_url_private`, keys are
  added at send time and stripped from the logged query; the CI and the smoke workflow send nothing paid
  (`tests/live/test_free_sources_live.py:77–95`).
- Retention handles the shared-body CHECK and the deferred raw items correctly and isolates each rule.

### Issues

#### Critical (Must Fix)

None.

#### Important (Should Fix)

1. **SmartRecruiters detail calls are made for every in-scope posting on every run; the §5.3.5 "new, changed
   or older than 7 days" rule is dead.** `traverse._details` reads `postings.list_item_sha256` /
   `detail_fetched_at` (`backend/src/jobhunter/sources/traverse.py:98–101, 399–407`) and
   `SmartRecruitersAdapter.detail_specs` skips a posting only when `known` holds a fresh, unchanged hash
   (`sources/ats/smartrecruiters.py:253–258`). Nothing writes those two columns: they appear only in the
   migration (`db/migrations/versions/0005_m1_collection.py:391–392`) and identity inserts postings without
   them (`dedup/identity.py:158–162`); `grep -rn list_item_sha256 backend/src` finds no writer. Scenario: a
   watchlist SmartRecruiters board with 300 in-scope postings → 300 detail GETs per hourly run (7,200/day to a
   free API that the spec throttles to ≤ 5/s precisely because it is a courtesy), plus 300 `raw_responses`,
   `raw_items` and `posting_observations` rows per run, re-resolved by node 6 each time. The contract tests
   exercise `detail_specs` with a hand-built `known` only (`tests/contract/test_ats.py`), so no test sees the
   production value `known = {}`. Fix: in identity's posting insert/sighting for a SmartRecruiters detail
   observation, set `list_item_sha256 = element_sha256(list item)` and `detail_fetched_at = t` (the list
   item's hash has to travel with the normalized posting or be recomputed from the parent list page in
   `_details`); add an integration test that runs the board twice and asserts the second traversal makes no
   detail call. Also decide the plan owner (no task lists these columns in its Files).

2. **JSearch cursor reuse bypasses the page-2 admission rule and reads pages the spec never defines.**
   `finalize_cells` writes `coverage_cells.continuation` from the last succeeded list page that carries a
   cursor, whatever stopped the traversal (`sources/coverage.py:564–568, 587–598, 702–703`), and the planner
   reuses any continuation younger than 24 h at `page_no = continuation.page_no` (`quota/planner.py:401–408,
   442–449`). The per-traversal rule that page 2 needs ≥ 7 new ids and that no traversal reads past page 2
   (`sources/traverse.py:275–281, 297–300`) is not consulted on reuse. Scenario (hourly schedule, live mode):
   run 1 page 1 has 3 new of 10 and a cursor → stop `page_cap`, continuation page 2 stored; run 2 one hour
   later plans page 2 from the cursor as the cell's only request → 1 paid unit on a page the spec says must
   not be requested; it carries a cursor → continuation page 3; run 3 reads page 3; … one paid unit per run
   per cell walks an old result set deeper while nothing newer than `t1` is requested for that cell until the
   TTL lapses or END. §5.3.1 ("Page 2: requested only if ≥ 7 of the page's 10 job_uids are new") and the
   traversal's own `PAID_LAST_PAGE = 2` are both violated; Carry-forward 5's description of the steady state
   ("never reaches END") is also no longer what the code does in a paid run within 24 h. Pacing bounds the
   damage (≈ 1 unit/run), which is why this is not Critical. Fix: write a continuation only when the next
   page was admitted by the rule but not read (the page's `new_native_ids ≥ PAGE2_MIN_NEW` and
   `page_no + 1 ≤ PAID_LAST_PAGE`, i.e. the reservation was denied or the run ended), or check both in
   `_reusable_cursor`; pin it with a planner test seeded from a page-1 row with `new_native_ids = 3`.

3. **The T7-b alert `quota.serpapi_renewal_stale` is unreachable from the lockout it was mandated for.**
   `check_serpapi_renewal` (`quota/ledger.py:540–556`) runs only inside `observe_account` (584), reached only
   by `reserve` (639–640) and `record_account` (605–611). While the SerpApi window is
   `extrapolated_unvalidated` and the account keeps reporting a renewal date `≤ window_start`,
   `pacing.decide` denies `window_cap` (`quota/pacing.py:117–118`), `plan_fallback` writes
   `skipped(window_cap)` rows from the in-memory projection (`quota/planner.py:533–558`,
   `graphs/collect/nodes/plan_fallback.py:73–77`) and never reserves; no read is persisted, so the window's
   `last_observed_at` never passes 48 h after its start and the alert is never raised. The only test of the
   alert calls `ledger.reserve(account=…)` directly (`tests/integration/test_quota_ledger.py:1134–1162`).
   Scenario: the SerpApi plan is changed or the renewal field stops advancing → every JSearch-exhausted cell
   stays unserved month after month with nothing but `skipped(window_cap)` rows to show it. Fix: have
   `plan_fallback` persist its account read under the owner lock (`ledger.record_account` after
   `plan_fallback`'s projection, which is what §5.3.2 calls the read "before every search" anyway), or run
   `check_serpapi_renewal` on the stored window from the planning transaction; add a test that plans a
   fallback 49 h after a rollover with a lagging read and asserts the alert.

#### Minor (Nice to Have)

1. `known_native_ids` is a database read made between the response and its settlement transaction
   (`sources/acquire.py:537–540`); a DB error there loses a paid response that was already in hand (the entry
   then settles `ambiguous`). Move the count inside `_settle_in` (same connection) so the only loss window is
   the settlement itself.
2. I3's per-run cap is re-derived from the *current* window at finalize (`nodes/finalize.py:241–248`), not
   from the cap in force at reservation; a window refined to a longer period between a reservation and the
   finalize can lower `ceil(2 × fair_share)` by one and turn a run that spent exactly its cap into
   `failed_invariant('I3')` with a critical `quota.limit_breach`. Compare against the maximum cap over the
   run's windows, or record the admitted cap on the ledger row.
3. `quota.serpapi_foreign_usage` resolves only when a *persisted* read arrives after `conservative_until`
   (`quota/ledger.py:344–368`); `plan_fallback`'s read is never persisted, so after one detection the warn
   alert stays open until the next actual SerpApi search (indefinitely while JSearch is healthy). Same fix as
   Important 3.
4. `plan_fallback` makes the free account read even when the SerpApi instance is `auth_failed` or
   `breaker_open` (`nodes/plan_fallback.py:48–53` checks `disabled` only): an hourly 401 with a revoked key,
   logged but never counted. Gate on `breaker.available(instance, now) != "blocked"`.
5. Dead production code and a stale carry-forward: `arbeitnow.plan_legs`, `step`, `seek_page` and the
   `seek` leg kind are used by tests only (`sources/arbeitnow.py:123–128, 185–226`; the traversal builds one
   fresh leg, `sources/traverse.py:374`), and `receipts.latest_fetch_attempt`, `jsearch.cursor_of`,
   `filters.load_facts` have no caller in `backend/src`. The `arbeitnow.py` docstring (48–50) and
   Carry-forward 7 say the seek legs are "planned"; they are not. Delete or mark test-support, and correct the
   carry-forward wording.
6. `FETCH_REQUESTS` is incremented for every page outcome of a traversal, replayed pages included
   (`nodes/fetch.py:179–180`); a resumed attempt counts its replayed pages again (metrics only, not the
   report).
7. Deviation 26 / T25-1 (the 200-word German stop list) is already in the fix wave; listed here only so the
   wave closes it together with the above.

### Declined to judge

- The live behaviour of the providers (cursor lifetimes, SerpApi account fields, SmartRecruiters totals):
  taken from the spikes' recordings; no live call was made.
- The embedder image build, model download and the V7 throughput/parity figures (Deviation 47, Carry-forward
  6/8): deployment-time decisions the controller flagged to the user.
- Normalisation linguistics and the gazetteer's data quality (Tasks 2–4): reviewed per task; nothing in the
  whole-branch view changes those verdicts.
- The frontend `schema.d.ts` regeneration and the M0 modules the range does not touch.
- Items the ledger already carries as deferred minors (missing FK indexes, `instances.disable` leaving its
  breaker alert open, `dedup.ambiguous_grouping` without a resolution, the stuck-`running` reconciler skip):
  known, and none looked more severe than recorded.
