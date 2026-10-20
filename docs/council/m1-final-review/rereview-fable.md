# M1 final re-review of the fix range 0cd6711..4b71f80 — Claude Fable 5.1

Read-only. HEAD and the working tree were at `4b71f80` (clean) throughout; no worktree, checkout, stash or edit was
made. Read: both original reviews, the ledger's rulings FR-a … FR-u, the triage, `deferred-minors.txt`, the F4/F5
re-reviews, the F6 report, the full source diff of the range (`backend/src`, excluding the regenerated gazetteer
data), the docs diff (plan Deviations 2–63 and Carry-forwards, V8.md, V17.md, `tests/contract/README.md`,
`scripts/smoke_m1.py`, `embedder/app.py`), and the code at HEAD wherever the diff left a mechanism out of view
(snapshot apply, `finalize_cells`, the ledger's account-read and recovery paths, `error_handled`, the SR marker
plumbing, `JobView.ats_boards`, the epoch lock modes). Local tests run once, after a strict check that no other
pytest process was running (the Codex reviewer's command line contains the word `pytest` but is not one):
`uv run pytest -W error -q tests/contract/test_fixture_pii.py tests/unit/test_fixture_scrub.py
tests/unit/test_arbeitnow_coverage.py tests/unit/test_group_rules.py tests/unit/test_dates.py` — **514 passed**,
the PII gate among them with zero hits over the committed fixtures. No DB suite was started (every doubt below was
settled by reading, and the F5/F6 reports record the full suite at 3305 passed on this head). No live or paid call,
no external call.

### Verdict

APPROVE — every finding of both reviews is fixed at 4b71f80 by the mechanism its ruling asked for, with an
end-to-end regression pin for each, and the fix range introduces no Critical or Important defect; the three Minors
below are uniformity and operability nits, none a safety, billing, coverage-truth or privacy regression.

### Finding verdicts

Fable's review (`review-fable.md`):

- **Important 1 — SmartRecruiters detail markers never written: FIXED.** `dedup/identity.py:116-121` (the `_ITEM`
  read joins the detail request to its parent list page and takes that page's `raw_items.item_sha256` for the same
  `native_id`, which is `element_sha256` of the same canonical element `detail_specs` hashes,
  `sources/base.py:314-317`, `sources/ats/smartrecruiters.py:231-234`), `:186-189` (`_DETAIL_MARKERS`: both columns
  together, forward only), `:543-545` (written in `_link` for every detail observation); the traversal reads them at
  `sources/traverse.py:113-115, 459` and `detail_specs` (`smartrecruiters.py:236-273`) now sees the production
  value. Pinned end to end (plan → traverse → parse → resolve, twice, then a changed item, then +8 days) by
  `tests/integration/test_traverse.py:1033`. I checked the consequence of skipped details: a complete snapshot still
  revives an `unknown` member through `rules.apply_membership` (`availability/snapshots.py:367-371`,
  `availability/rules.py:148-154`), so an unchanged posting without a detail observation is never stranded.
- **Important 2 — JSearch cursor reuse bypasses the page-2 rule: FIXED.** One statement of the rule,
  `sources/jsearch.py:84-88` (`admits_next_page`, with `PAID_LAST_PAGE` at `:72`); the traversal reads by it
  (`sources/traverse.py:304`); `finalize_cells` writes a continuation only when that rule admits the next page
  (`sources/coverage.py:130-132, 599-615, 721`; a stored document without `new_native_ids` parses as `None`,
  `:137-166`); the planner checks it again before reuse (`quota/planner.py:402-413`). Pinned by
  `tests/integration/test_plan_node.py:237`, `tests/integration/test_traverse.py:1123` and `:1219` (no continuation
  past page 2). Carry-forward 5's wording is corrected (plan, "Such a page 1's cursor is not reused either").
- **Important 3 — `quota.serpapi_renewal_stale` unreachable: FIXED.** `plan_fallback` persists its account read
  under the run's owner row in a transaction of its own (`graphs/collect/nodes/plan_fallback.py:103-106`,
  `ledger.record_account` `quota/ledger.py:630-636` → `_account_window` → `observe_account`, whose `:609` runs
  `check_serpapi_renewal` `:565-581`). Pinned by `tests/integration/test_plan_node.py:873` (planned 49 h after a
  rollover with a lagging read: the alert opens, a validating read resolves it). Deviation 28 amended.
- **Minor 1 — `known_native_ids` read between response and settlement: FIXED.** The count is made on the settlement
  transaction's connection after the owner lock (`sources/acquire.py:190`, `:713-715`; `sources/traverse.py:_known`
  takes `conn`), so no read stands between the response in hand and its settlement.
- **Minor 2 — I3's per-run cap re-derived at finalize: FIXED.** `quota_ledger.per_run_cap` (migration
  `0005_m1_collection.py:239, 251`, note 8 at `:60-64`), written at reservation (`quota/ledger.py:701`), read back
  as the run's latest admitted cap (`:188-192, 279-292`) and compared in `_i3`
  (`graphs/collect/nodes/finalize.py:232-258`). The argument "an entry only leaves the counted set, and admission at
  the latest entry used the same cross-window run count (`:679`)" holds, so the latest cap bounds the run. Pinned by
  `tests/integration/test_finalize.py:472` and `:542`. Deviation 57.
- **Minor 3 — `quota.serpapi_foreign_usage` never resolves while JSearch serves: FIXED** by the same persisted
  read (`detect_foreign_usage` runs inside `observe_account`, `quota/ledger.py:370-393, 608`). Pinned by
  `tests/integration/test_plan_node.py:937`.
- **Minor 4 — account read made on an `auth_failed`/`breaker_open` instance: FIXED.** `plan_fallback.py:68`
  (`available(instance, now) == "blocked"`, which covers `disabled`), and the read after a traversal's last search is
  gated too (`sources/traverse.py:338-347`). Pinned by `tests/integration/test_plan_node.py:914`. A read that fails
  as `auth` is now counted by the breaker (`sources/acquire.py:208-216, 521, 620`), Ruling FR-l.
- **Minor 5 — dead code and the carry-forward wording: FIXED.** `plan_legs`, `step`, `seek_page`, `Leg`,
  `receipts.latest_fetch_attempt`, `jsearch.cursor_of`/`items_of`, `filters.load_facts` have no definition left in
  `backend/src` (grep: 0 hits); the `arbeitnow.py` docstring (`:35-60`) says no seek leg is planned; plan
  Carry-forward 7 and Deviation 58 say so, and the superseded Task bodies are marked.
- **Minor 6 — `FETCH_REQUESTS` counts replayed pages: FIXED.** `TraversalResult.attempted` holds only the pages this
  attempt sent or denied (`sources/traverse.py:210-216`, `Denied`/`Ambiguous` carry `replayed`,
  `sources/acquire.py:167-185`), the node counts those (`graphs/collect/nodes/fetch.py:211-212`), and plan-time
  skips are counted once after their commit (`graphs/collect/nodes/plan.py:26-30, 52`; `plan_fallback.py:121`).
- **Minor 7 — Ruling T3-a stop words: FIXED.** `normalize/data/stopwords_de.txt` is 231 lines (`stopwords_en.txt`
  127), `normalize/language.py:6-9`, asserted at `tests/unit/test_html_quality_language.py:374-375`; Deviation 26
  rewritten.

Codex's review (`review-codex.md`):

- **C1 — real personal names in committed fixtures: FIXED** (Ruling FR-a, fix forward). The two Personio files hold
  `[person:n]` placeholders at the cited places (4 each), 24 fixture files carry placeholders, and the narrative
  audit is documented without any name in `tests/contract/README.md:13-57` (the FR-e publisher title replaced by an
  invented brand and an `.example` host). The scrubber gained `ROLE_NAME`, `INTRODUCED_NAME`, the legal-form and
  explicit title vocabularies and the suffix person-key rule (`cli_fixtures.py:222-283, 407-486, 828-838`), and the
  gate fails on any name those rules find (`tests/contract/test_fixture_pii.py:14-15, 81, 89`). I ran the gate on
  this head: zero hits. The commit messages of the fix range carry no personal name (the only trailer names the
  model); the `.superpowers/` review files are untracked. Residual, as the ruling records: the names remain in the
  pushed feature-branch history from 414f02a (squash merge or history rewrite is the user's call).
- **C2 — unfenced startup sync and retention writes: FIXED.** `sync_instances` runs its guard first
  (`sources/instances.py:117-126`, the worker's `fence_epoch`, `worker/main.py:262-267`); a superseded start
  writes nothing and exits 70 (`main.py:268-275`, Ruling FR-i; `:403` keeps 70 through the teardown), another
  owner's fence fails the start with exit 1 (`:269-270`). Every retention write is fenced
  (`privacy/retention.py:196-199` row rules, `:202-213` and `:216-222` the saver's and procrastinate's deletes under
  a fence held across them, `:241-258` the alert writes), and a `FencedOut` is never recorded as a rule failure
  (`:265-267, 293-294, 304-307`). Defence in depth: a paid adapter without its key builds and sends nothing
  (`sources/http.py:285-303`; `jsearch.py:286`, `serpapi.py:318, 341`, `adzuna.py:296`; `registry.py:86-94`).
  Pinned by `tests/integration/test_worker_wiring.py:260` (the stale-startup interleaving: the newer owner's disable
  stands), `tests/integration/test_retention.py:375, 425`, `tests/unit/test_worker_main.py:323-387`.
- **I1 — an incomplete snapshot bypasses mass-disappearance protection: FIXED** per Ruling FR-d.
  `availability/snapshots.py:135-139` (`_BASELINE_AT`: the last applied structurally complete snapshot before the
  open event's opening one, else before this one), `:243-251` (`_disappeared`: `active`, or `unknown` since that
  snapshot), `:358-361`. I traced the clear/confirm paths: a clearing snapshot is `complete` and a confirming one
  `mass_disappearance`, both in the baseline query's status list, so postings made `unknown` by the event's own
  snapshots stop counting once the event ended and the baseline was reset, and an incomplete read never lowers the
  baseline (`_RESET_BASELINE` runs for complete snapshots only, `:424-425`). Codex's sequence is the regression
  `tests/integration/test_snapshots.py:568`; `:628` and `:503` pin the open-event and below-half cases. Deviations
  45 and 52.
- **I2 — ATS-key changes leave distinct same-board jobs grouped: FIXED** per Rulings FR-j and FR-k.
  `dedup/groups.py:343-362` (`revalidation_departures`: G2–G5 against the representative, then G1 for every pair in
  join order, the representative first), used by every revalidation (`:504-513`) and by the no-representative
  member path (`:669-675`); node 6's selection adds jobs with an `ad_keys` row or a `merge_events` row since the
  run started (`:218-226`); the reconciler repairs a group with two members on one board (`:228-235, 991-993`).
  The SQL's `k2.key_ns = k1.key_ns` is the same test as `same_board` (`ats_boards` is `substr(key_ns, 5)` of the
  `ats:<type>:<board>` namespace, `:166-167, 318-320`). Pinned by `tests/integration/test_groups.py:826, 883, 905`
  and `tests/unit/test_group_rules.py:192`. Deviations 46, 53–55.
- **I3 — SmartRecruiters refresh markers never persisted: FIXED** (= Fable Important 1 above).

Counts: 15 of 15 findings FIXED (Fable 10, Codex 5); 0 PARTIAL, 0 NOT FIXED, 0 DISAGREE-WITH-RULING.

### New issues

#### Critical (Must Fix)

None.

#### Important (Should Fix)

None. In the focus areas I specifically looked for and did not find: a fence that a superseded worker's write can
bypass (every maintenance write now begins with `fence_epoch`; `_CURRENT_EPOCH` takes `FOR KEY SHARE`, which
conflicts with the claim's `FOR UPDATE`, `worker/epoch.py:92-95`); a non-worker `FencedOut` that stops the worker
(`errors.py:30-47`; the four catch sites `runs.py:310`, `operations.py:513`, `tasks.py:141, 176`, `main.py:269`
all discriminate); a mass-disappearance count that can fall below the pre-fix one; a G1 pair that survives a
revalidation (at least one member of a sharing pair always leaves, so the reconciler's repair terminates); a
continuation that plans past page 2; a per-run cap that can produce a false I3 breach; an Age value that makes
`filled_at` raise or claim coverage above the earliest fill (`arbeitnow.py:185-230`, the property test covers it);
the cell timeout turning into a handled source error (`graph.py:124-146` catches `SourceError` and `EmbedderError`
only, so the `TimeoutError` still fails the run as the LangGraph policy did); and a lock-order inversion (the
fence's `worker_heartbeat` row is always the first lock of its transaction; nothing takes it after a job, run or
instance row).

#### Minor (Nice to Have)

1. **`ledger_unsettled` still swallows the worker's own fence** — `quota/ledger.py:940-947`: `except FencedOut:
   break` returns the count so far, so the reconciler reports success and the superseded worker's cadence stops one
   step later, at `_resolve_failure`'s fence (`worker/reconcilers.py:103-106`), instead of at the reconciler as
   Ruling FR-r made every other one do (`embeddings.py:460-495`). Sequence: zombie worker A, tick cadence,
   `ledger_unsettled` fenced at its first entry → `break` → `_resolve_failure` fenced → `reconcilers_fenced`,
   `on_superseded`. Why it matters: effect identical (nothing written, the worker stops), but the module docstring
   now promises one rule and the code has two; `_recover` (`:894-923`) raises no other owner's fence (it locks the
   run row `FOR UPDATE`, and the recovery writes return `False` on conflict), so nothing else is hidden. Fix: drop
   the `try`/`except` and let the fence propagate (the `stale_runs` `except FencedOut: return 0` at
   `worker/runs.py:561-562` is the same shape, but it predates the fix range).
2. **`stalled_jobs` is the one unfenced maintenance write** — `worker/reconcilers.py:163-166` (cadences `start` and
   `tick`) calls `tasks.release_stalled_jobs` (`worker/tasks.py:399-420`), which retries procrastinate jobs whose
   worker heartbeat is 60 s stale, with no `fence_epoch`; Ruling FR-h named `queued_runs`, `operations` and
   `stale_data` only. Sequence: zombie A's tick runs `stalled_jobs` once (its own stale jobs are retried, which B's
   pass would do identically), then `_resolve_failure`'s fence stops the cadence. Why it matters: no wrong state is
   reachable (only jobs with a stale procrastinate heartbeat are touched, never B's live ones), so this is
   uniformity only. Fix: wrap the release in a fenced transaction like the other three, or record in FR-h why it is
   exempt.
3. **A fence held across a delete on another pool can block a takeover past its 10 s lock timeout** —
   `privacy/retention.py:202-213` (`_purge_threads`: one fenced transaction per thread around
   `saver.adelete_thread`) and `:216-222` (`_delete_finished_jobs`: one fenced transaction around procrastinate's
   whole `delete_old_jobs`). The fence is `FOR KEY SHARE` on `worker_heartbeat`, which the claim's `FOR UPDATE`
   waits on under `TAKEOVER_LOCK_TIMEOUT_MS = 10 s` (`worker/epoch.py:9-24, 69, 92-95`). Sequence: worker A is a
   live zombie (lost the singleton lock but still running) in its 03:17 daily pass on a large `procrastinate_jobs`
   backlog (first pass after a long outage); B starts, claims, waits 10 s on A's held fence → `takeover_blocked`,
   exit 1 → restart → retry (and again while the delete runs). Why it matters: the design is right (the fence held
   across the delete is what guarantees no takeover commits between the check and the delete), and no deadlock
   exists (A's held lock blocks nothing the delete needs), but the takeover is delayed by one restart cycle per
   long statement; on this deployment's table sizes the delete is far below 10 s. Fix if wanted: delete in batches
   (procrastinate's `delete_old_jobs` takes `max_hours`; a LIMIT-ed loop of own SQL would do), one fenced
   transaction per batch, as the row rules already are per rule.

Notes, not issues (recorded so the next reader does not re-derive them): a reuse page 2 that is denied again keeps
its continuation (`coverage.py:719-721` rewrites it only from a succeeded page), so the cursor is retried each run
until the 24 h TTL, within the rule and bounded; `_age_s` (`arbeitnow.py:198-211`) reads a response with neither
`Age` nor `cf-cache-status` as age 0 (a cache that omits `Age` would be non-compliant with RFC 9111, and the F4
re-review accepted this from V8's data), and if the edge preserved the origin's `Date` instead of rewriting it,
`Date − Age` would understate the fill and claim less, never more.

### Privacy

No real personal name was found in `tests/contract/fixtures/**`: the gate (`test_fixture_pii.py`, with its role-word,
introducing-verb and contact-window rules and the documented allow-list) passed on this head with zero hits, the two
files Codex cited carry placeholders, and the README's limits list and audit table name placeholders only. The
tracked docs changed in the range (the plan, V8.md, V17.md, `tests/contract/README.md`) quote no name; commit
messages of the range carry none. Kept by the F1 ruling and not re-judged here: the lower-case first-name team
handles and two first-name test titles of a vendor's public demo board (`tests/contract/README.md:49-52`), first
names of demo data, not identifying. Not inspected: the pushed branch history before the fix (Ruling FR-a flags it to
the user).

### Declined to judge

- The live behaviour behind the fixes (SerpApi `account.json` fields after a plan change, JSearch cursor lifetimes,
  Cloudflare's `Date`/`Age` semantics on the Arbeitnow feed): taken from the spikes' recordings; no live call.
- The full DB suite and the crash matrix on this head: not re-run (the F6 report records 3305 passed, 1 skipped on
  `4b71f80`; the collect-only count of the F5 re-review matched its head's test set); only the DB-free privacy,
  scrubber, Arbeitnow coverage, group-rule and date tests ran here.
- The regenerated gazetteer data (`de_places.tsv`, 15k changed lines, a new municipality column): the build and its
  SHA-256 attribution were accepted in F1 (V17.md); I checked the reader (`geo/gazetteer.py:49-58, 104-114`) and the
  district rule (`locations.py:271-290`), not the data.
- The controller's rulings themselves (FR-d's conservative count, FR-k's representative-stays reading, FR-t's one
  critical alert per missing paid key, FR-u's clamp): each is recorded as a Deviation with its cost, none is unsafe
  or wrong on reading, and the user note for FR-t ("three critical `source.auth_failed` alerts when running
  deliberately without paid keys, reopened at each start while missing") is in the ledger.
- The pushed branch history's names (FR-a): a user decision.
