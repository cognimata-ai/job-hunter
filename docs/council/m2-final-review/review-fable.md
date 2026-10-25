# M2 final whole-branch review — Claude Fable 5.1

Range `ce2e42e..2ffcc8a` (80 commits, 295 files, +61,248/−1,163), read module by module against spec revision
3.4 and the plan's Deviations 1–43, Carry-forwards and Pre-flight corrections. Read-only: nothing in the tree
was changed. Local checks run: `scripts/check-openapi-drift.sh` (regenerates `frontend/src/api/schema.d.ts`
from the live OpenAPI document; "OpenAPI types are up to date"); one scratch probe test on the real PostgreSQL
fixtures (`tests.support.*` plugins, `pytest -W error`) sending a NUL character in a field of
`PUT /api/profile/drafts/{id}`; two in-process probes of `settings.validate.validate_fields`, `pydantic` and
`ConfirmedProfile` on a lone surrogate and on a NUL. No paid or network call was made, and nothing under
`.secrets/` or `.fixtures-raw/` was read.

### Verdict

APPROVE WITH CHANGES — the spend, privacy, lock-order and crash properties hold end to end and I found no
double-billing, cap-exceeding, content-leaking or deadlockable path; but one M2 route lets a NUL character
reach a JSONB write and answers 500 with a persisted `api.unhandled_exception` alert (verified on the real
stack), and two GETs answer 500 for a stored profile that no longer validates. Neither corrupts data or
spends money, so neither is Critical.

### Focus questions 1–8

**1. Spend safety.** Every paid request goes through `LLMGateway.call`
(`backend/src/jobhunter/llm/gateway.py:722–767`): per HTTP try, `_reserve` (815–852) runs
`spend_ledger.reserve` (`llm/spend_ledger.py:462–586`) in the §9.1 order — `active_config FOR SHARE`
(`current_params`, 506), the owner row `FOR SHARE` with its predicate plus the global-fence check (`_lock_owner`,
157–162), `ensure_month` (509), the month row `FOR UPDATE` accrued (510), the worst case from the *locked* row's
buffer (513–516), `budget.admits` (517; `llm/budget.py:197–212` implements `counted + r ≤ A`, the reserve rule
and the `cap_lowered_at` block verbatim) or `BudgetExhausted` before any row is written, then the ledger and
call rows and the counters (528–577). `_dispatch` (854–875) is a separate transaction that re-locks the owner,
checks the entry still belongs to this owner and moves `reserved → dispatched` by CAS (589–605); a `FencedOut`
releases the try as `not_billed(never_dispatched)` and nothing is sent. Settlement (`settle`, 801–877) locks the
reservation's own month (827), so a month rollover during the stream adjusts the right month, charges a bound
violation in full with `evidence='reservation_bound_violated'` and the critical alert (841, 875, 905–927), and
accepts a late settlement of an entry the reconciler already moved to `ambiguous` (833–834) while refusing to
touch a `billed`/`not_billed` one (830–832). A lost stream, a deadline or a cancellation is `ambiguous` and keeps
its reservation (`_settle_lost`, 1111–1127; `_settle_cancelled` catches only `SQLAlchemyError` and leaves the
entry to the reconciler, 924–935). `llm_ledger_unsettled` (1174–1206, `_recover` 1143–1164) releases a
`reserved` entry only after re-checking the recorded owner under its row `FOR UPDATE` (`owner_holds`, 165–181),
so it cannot race the owner's marker, and turns a `dispatched` online entry `ambiguous` after
`max(15 min, deadline_s + 5 min)` (Deviation 24); `budget_counters` recomputes from the §7.11 formula
(973–987) and corrects under the month lock. Receipts (`receipts`, 935–965) are matched by operation id and
`logical_call_no`, and `llm_outcome.walk` (`graphs/profile/nodes/llm_outcome.py:117–135`) uses a completed
output, repairs once after `invalid_output`, fails on a terminal code and never repeats or skips the repair
across a reclaim; the C35 tests (`tests/integration/test_c35_operation_stall.py:27,46`,
`test_c35_profile_extraction.py:61,79`) exercise exactly that. `LLMNotConfigured` is raised by
`_configured_key` (704–709) before the input bound and before any transaction, and `after_error` maps it to
`failed('llm_not_configured')` (143–168). A settlement can be *lost* only in the C3 sense: a database failure
after the response is in hand leaves the entry `dispatched`, the reconciler counts it at its reservation, and
the next attempt pays again — the safe direction. I found no path that dispatches without a reservation,
exceeds the cap or bills one logical call twice; two bounded re-payments are Minor 1 and Minor 3.

**2. Privacy.** Checkpoint state is ids, steps and a small enum (`graphs/profile/state.py:28–33, 55–60`);
every profile node is wrapped by `sanitized` (`graphs/profile/graph.py:157–171`), which replaces any error
outside `PASS_THROUGH` (106: code-only domain errors) by `NodeFailed(node, class)` raised outside the handler,
so neither `__cause__` nor `__context__` reaches `checkpoint_writes`; the LLM nodes carry `LLM_NODE_TRACE`
(104). The engine hides bound parameters in every statement error (`db/session.py:16`), the SDK's and
LangGraph's content-quoting loggers are held at WARNING with no handler of their own
(`observability/logging.py:81–90, 115–149`) and `ANTHROPIC_LOG=debug|info` is refused in prod
(`config.py:420`). Every `log.*` call I read in the M2 modules carries ids, codes, counts, amounts, hashes or
error classes; the operation payloads are `{"draft_id"}`, `{"profile_version_id"}`, `{"proposal_id",
"scope_version_id"}`, `{"scope_version_id"}`; alert contexts are ids, amounts, hashes and codes
(`llm/cache_monitor.py:160–175`, `spend_ledger.py:914–927`, `nodes/__init__.py:224–231`). Contact stripping
runs before every text call: the extraction sends exactly `reviewed_text`, which is the stripped text the user
approved (`profile_review.py:611–628`), and `extract_profile._request` re-derives `transmitted_text_sha256`
from the rendered `<cv_text>` and refuses a mismatch (`nodes/extract_profile.py:115–121`); the native PDF is the
user's explicit opt-in (`upload.py:449, 483–486`; `profile_review.py:667–679, 701–703`); the proposal sends the
confirmed profile without `user_entered` (`llm/prompts/render.py:87–96`). `profile_versions.profile_json` is
contact-stripped: `confirm_draft` runs `validate_profile` (which blanks or drops every value `scan_value` hits,
`profile/validate.py:114–137`) and refuses any `user_entered` string with a contact (`activation.py:228–259,
302–306`). Encryption at rest is AES-256-GCM with a fresh nonce, a key id for rotation and AAD that binds a
blob to its file name or its `cv_files` row (`crypto.py:61–90`, `profile/storage.py:58–63, 75–97`); files are
created `O_EXCL|O_NOFOLLOW` at 0600 and fsynced, and only names `write` makes are ever opened (70–73). The
status route returns `stripped_text_preview` and offsets, never the raw text (`profile_review.py:140–148,
436–448`). Retention does what §20.1 says: draft content and the extraction calls' `output_json` after 30 days
with `confirm_response` and `text_edits` kept (`privacy/retention.py:292–299, 435–458`), CV files of everything
but the active, the previous (in activation order) and open drafts' versions (303–337, 468–521, serialised with
confirm and retry through `lock_active(share=True)` and the drafts `FOR UPDATE`), orphans after a day,
`eval_runs` after 13 months keeping the latest completed run per kind (341–347), profile threads by their
operation's end (393–415), and unreferenced embeddings only (212–217). The fixture PII scan covers every file
under `fixtures/anthropic/` (`tests/contract/test_fixture_pii.py:26–31`). The one leak-adjacent finding is
Important 1: the 500 it produces logs the error class only, but it persists an alert a client can trigger.

**3. Lock order and concurrency.** Every transaction I traced follows §9.1. Settings save: `active_config FOR
UPDATE` as a statement of its own, then the version by id (`settings/save.py:90–92, 305, 313`), the derived and
version rows, then `llm_tuple` → `scope` (the `operations` insert, owner-row position) → `budget` (the month row,
`_apply_budget` 226–240) → `schedule_state` → `source_instances`/`coverage_cells` → `ranking_state` last
(`ACTION_ORDER` 281–294, 377–388). Confirm: the route locks `active_config` before the idempotency row
(`api/routes/profile.py:518`), then the draft, a reused version `FOR KEY SHARE`, the inserted rows, the version
`FOR NO KEY UPDATE` in `request_proposal` (`queries/proposals.py:286, 316`) and `ranking.bump` last
(`activation.py:287–288, 322–349`). Scope apply: the precompute takes job rows `FOR SHARE` in one ordered
statement, then the owner row, then the results, with `set_progress` as the last statement and **no**
`active_config` lock (`scope_apply.py:229–245`, `scoring/filters.py:452–457, 460–488`); the final transaction
takes `active_config FOR UPDATE`, the owner row, re-reads the pointer and flips (248–266). The spend ledger
takes `active_config FOR SHARE` → owner `FOR SHARE` → month → ledger, and its reconciler `active_config` →
owner `FOR UPDATE` → month (1143–1164); the takeover sweep waits on owner rows (`worker/operations.py:285–288`)
after a heartbeat update that is key-compatible with the reconcilers' `fence_epoch`. The cv-file batch takes
`active_config FOR SHARE`, drafts, then `cv_files`, the same order as the review routes (draft, then
`cv_files`: `profile_review.py:525–531, 606–611`). No pair acquires two of these resources in opposite order;
the one position swap, `add_company` touching `watchlist_companies` and `source_instances` before the
`watchlist_validate` operation insert (`api/routes/watchlist.py:292–297`), I could not turn into a cycle: that
insert waits only on an uncommitted tuple of a live row of the same dedup key, and nothing that updates such a
row holds `source_instances` first (the M1 handler's domain write precedes its finish; not re-verified here).
The Deviation 43 rule: `lock_active` splits lock and read (`settings/active.py:46–58`); `active_scopes` reads
the scope rows in a second statement (`filters.py:373–378, 404–417`); the remaining lock-plus-join statements
are `_LOCK_EXTRACT_DRAFT` (`nodes/__init__.py:76–81`, `FOR UPDATE OF d`), whose joined `storage_path` could be
stale only for a draft the batch purged, which the rechecked `status` sends to `end_obsolete` first, and
`runs._ADMISSION` (parked 5, analysed below). Lost updates: every draft write of the extract phase carries the
generation predicate on `profile_drafts.owner_cols` (`db/transitions.py:313`, `nodes/draft.py:64–82`), every
operation end is a CAS on `claim_id`, and a pending scope pointer is moved only under `active_config FOR UPDATE`
(`scope_apply.py:171–184`).

**4. Crash safety.** Operations claim with a fresh `claim_id` and the epoch (`worker/operations.py:336–367`);
every owner write is a CAS on it; `_release` (444–489) is the one copy of the attempts rule for the owner's
requeue, the stale reclaim (`SKIP LOCKED`, 278–280) and the takeover sweep (285–288), and runs the kind's
failure hook in the transaction that ends the row `failed`. The hooks (`profile/operations.py:138–152,
175–186, 207–221`) move only a row still live in that phase, so a draft never stays `uploaded`,
`extracting_text` or `extracting_profile` past its operation, nor a proposal `running`. `execute_operation`
(667–759) re-defers a fenced delivery, requeues a raised handler as `bug` and writes nothing after a `lost`
outcome; a handler that ended its row through `finish_in`/`requeue_in` is not written again. C24/C34
(`tests/integration/test_c24_c34_profile_confirm.py:28, 57`: the confirm is one transaction behind the key's
row), C25 (`tests/crash/test_c25_scope_precompute.py:30`), C30 (`test_c30_scope_save_redefer.py:23`) and C35
(both files above) are real tests on the real worker where the case needs it. Stuck-object sweep: a `reserved`
or `dispatched` ledger entry → `llm_ledger_unsettled`; a pending scope with no live apply → `scope_apply_failed`
clears it after a permanent failure or re-requests it otherwise (`scope_apply.py:316–337`); a draft in
`extracting_*` → its operation's end or hook; a `running` proposal → the hook, and `request_proposal` recreates a
missing operation (`proposals.py:318–325`); an abandoned eval run → failed after 1 h (`spend_ledger.py:1112–1127`);
a failed `embed_profile` of the active version → the `profile_vectors` reconciler (`vectors.py:218–246`). The
one state with no reconciler is the documented `profile.extract_generation_mismatch` (an `extracting_profile`
draft met in another generation, `nodes/__init__.py:216–233`), carried to M5's runbook; I agree no route makes
it. A proposal whose operation is `cancelled` (an M5 purge) would stay `running`; that is M5's to close.

**5. Hostile input.** Uploads: the multipart body streams through `python_multipart` with the file part
counted as it arrives and the whole body capped at the limit plus 64 KiB (`profile/upload.py:244–364`); the
paste body is capped at 1 MiB before FastAPI parses it (`read_body_limited` 375–388, `_CappedBodyRoute`
`api/routes/profile.py:242–262`); `check_file` runs in a thread and refuses size, type, extension, a central
directory with more than 4,000 record signatures before any parse, an unreadable ZIP, more than 1,000 entries or
50 MiB declared, `vbaProject.bin` anywhere and a missing part (143–172); no PDF parser runs in the api
(Deviation 7). docparse: a `spawn` child with `RLIMIT_CPU`, `RLIMIT_AS` and no core (`docparse/worker.py:164–174`),
one JSON answer over a pipe read against a deadline and a size cap (199–213), SIGXCPU/SIGKILL/overrun as
`parse_limit` (257–274), encryption and page count refused before a page is read (`extract.py:65–74`); the
worker reads docparse's answer as untrusted through the job directory's own descriptor, `O_NOFOLLOW`, regular
files only, size-capped, UTF-8 and length-checked (`parse_client.py:132–184`); the service has
`network_mode: none`, a read-only root, `cap_drop ALL`, its own uid and the 2770 tmpfs (`compose.dev.yaml:169–190,
228–230`). Routes: every id path is bounded at 2⁶³−1; settings refuse NUL at every depth and a lone surrogate
through pydantic's strict string (`settings/validate.py:118–144`, verified: `string_unicode`); queries, watchlist
and geo strings go through `clean_text` or `checked_text` (`api/models.py:30–38`, `queries/authoring.py:103–115`);
the paste drops NUL and refuses surrogates (`upload.py:175–187`); idempotent bodies fingerprint a surrogate
instead of raising (`api_idempotency.py:69–81`); `extra="forbid"` on every M2 body. The exception is
`PUT /api/profile/drafts/{id}`: `DraftIn.draft` is a `ConfirmedProfile`, whose strings carry no NUL check, and
`_STORE_DRAFT` writes it as JSONB (`profile_review.py:244–248, 166–170, 468–486`) — PostgreSQL refuses `\u0000`
in `jsonb`, and the probe answered 500 `internal` with `error_class: DataError` and the persisted alert
(Important 1).

**6. Settings correctness.** `Settings.to_payload()` is the one stored form (`settings/model.py:496–498`):
USD at two fixed places, rates at most two places without trailing zeros (113–143), industries folded and
companies keyed to a fixed point (154–181), set-valued lists deduplicated and declared-ordered (185–193), and
every effect class is read off the normal form (`effects.py:127–135`), so a formatting change derives no row.
Field and cross-field rules are §13.1's (`validate.py:158–299`, the §6.9.1 buffer bound 280–299 shared with
V19); the model-policy label is part of the hashed payload (Task 16 ruling 2). Derived rows are deduplicated by
content hash (`derive.py:87–98`), the `profile_relative` range resolved with the active profile at save and
confirm (`derive.py:65–84`; `activation.py:322`). DST: `local_anchor_to_utc` maps a skipped wall time to the end
of its gap and a repeated one to its first occurrence (`db/seed.py:110–149`), and a `schedule` save resets the
cursor to the latest slot at or before `now` so no catch-up run fires (`save.py:243–259`). The dry-run runs
read-only (`routes/settings.py:260`) and the save re-runs the same `dry_run` on the locked active document and
compares warning codes as a set (`save.py:316–331`; `routes/settings.py:345–346`); the dry-run refuses unknown
query and watchlist ids so preview and save agree (`dryrun.py:125–142`). The cap edit goes to the month row only
when cap or buffer changed, and a buffer-only edit keeps an earlier lowering (`save.py:226–240`;
`spend_ledger.py:322–353`). Internal saves copy the acknowledgements and still validate (`save.py:319–320`). Two
notes: Minor 4 (`display.timezone` is class `none` although the anchor is derived from it) and the
`profile_relative` scope saved before any profile keeps `profile_relative`, which the M1 filter treats as open
(Deviation 14) — correct per the plan.

**7. API contract.** Every §14.2 row of M2 is served with the summarised shape plus the Deviation 3 members:
the draft routes (`profile_review.py:414–772`), upload and paste (`profile.py:292–381`), proposals (396–465),
confirm and estimate (471–575), settings (`routes/settings.py:252–485`), queries (`routes/queries.py:85–133`),
watchlist and geo (`watchlist.py:256–338`, `geo.py:39–65`), spend (`spend.py:124–129`). The problem codes are
§14.4's plus `profile_purge_in_progress` from §12.6 (`api/errors.py:40–59`); 409 `conflict_state` covers the
wrong-state cases, `stale_base_version`, `eval_gate_failed` (reachable only once M3 calibrates),
`idempotency_*`. `Idempotency-Key` is required on exactly the five M2 POSTs §14.1 lists, each through
`run_idempotent` with the handler inside the key's transaction, and `confirm_response` returns the original
body under any key (`activation.py:291–294`). Sessions and CSRF come from the M0 edge for every router
(`require_session`, `api/deps.py:124`, a dependency of every M2 router, e.g. `routes/profile.py:100`,
`routes/settings.py:94`; CSRF refused at the edge, `api/edge.py:90–158`); the paste route's custom `APIRoute` only replays the capped
body. `scripts/check-openapi-drift.sh` regenerated `frontend/src/api/schema.d.ts` and found no difference.

**8. Plan drift.** Delivered as planned: migrations 0006/0007 with the state machines and the `profile` fence
branch (`0006_m2_llm_profile_settings.py`, the three documented departures), the gateway, ledger, budget and
reconcilers, docparse and the client, upload, stripping, review, validation, confirm, vectors, proposals,
authoring, the settings package and routes, watchlist/geo, the worker wiring (`worker/main.py:140–156`), the
retention rules, the planner split (`quota/cells.py`, `sources/spec_adapters.py`,
`tests/unit/test_quota_imports.py`), the fake-LLM overlay, `scripts/smoke_m2.py` with its test, the payload and
docparse CI jobs, the boundary lint, spikes V4/V5(local)/V11/V13/V19 with their documents, and the crash cases.
Nothing of M3–M5 is built early beyond Deviation 3: node 9 and I5 sit behind `SCORE_TARGETS_ENABLED`
(`enqueue_targets.py:19–27`), `llm.cap_reached` is only a constant, `eval_gate_state` returns `uncalibrated`,
and the activation routes, `/api/queries/stats`, `budget_release`, the purges and the dispatch are absent. The
carry-forwards are accurate; I would add to M3 the `output_json` NUL case (Minor 3) and to M5 the `running`
proposal of a cancelled operation. One gap against Task 24 step 4: the roadmap's status table still shows
M2 as `—` with no plan link (`docs/superpowers/plans/2026-10-16-job-hunter-roadmap.md:63`); the commit
message says "final review pending", so this is for the fix wave to close (Minor 5).

### Strengths

- One reserve → marker → send → settle protocol for every Claude call, each step its own transaction, the
  worst case computed from the locked month row, and `FencedOut` on the marker releasing the try before any
  byte leaves (`llm/gateway.py`, `llm/spend_ledger.py`); the counters are recomputable from the ledger and the
  two ledger CHECKs refuse NULL evidence (Deviation 30).
- Receipts keyed by operation id and `logical_call_no`, with the one structural repair made exactly once across
  reclaims, and the C35 matrix run with a second execution registry (`llm_outcome.py`, `test_c35_*`).
- The privacy posture is layered: ids-only state, `sanitized` nodes, hidden statement parameters, library
  loggers held at WARNING, content-free alerts and problem details, the generation predicate on
  `profile_drafts`, and a PII scan over every recording.
- The upload and docparse boundary: streaming limits before buffering, the central-directory pre-count, a
  spawn child under rlimits whose answer is read as hostile through fd-relative, `O_NOFOLLOW`, size-capped
  reads, and a tmpfs that only the two services mount.
- Settings in one normal form with content-hashed derived rows, the save's class actions in §9.1 order, a
  scope apply that ends obsolete only in its final locked transaction, and a reconciler that both clears a
  failed apply and re-requests a lost one.
- Retention serialised with activation and retry through the same locks the routes take, batched and fenced,
  with the activation-order "previous" version.

### Issues

#### Critical (Must Fix)

None.

#### Important (Should Fix)

1. **A NUL character in a `PUT /api/profile/drafts/{id}` field is a 500 and a persisted alert.** `DraftIn.draft`
   is a `ConfirmedProfile` (`backend/src/jobhunter/api/routes/profile_review.py:244–248`), a strict mirror whose
   strings have no `clean_text` check (`llm/schemas/profile.py`, `_base.py:58–61`); `save_draft` validates and
   stores it as JSONB (`profile_review.py:468–486`, `_STORE_DRAFT` 166–170), and PostgreSQL refuses `\u0000` in
   `jsonb`. Verified on the real fixtures: a ready draft, its own profile with `summary + "\x00"`, `PUT` →
   `500 internal`, log `unhandled_exception error_class=DataError`, and `api.unhandled_exception` written by
   `_on_unhandled` (`api/errors.py:249`). A lone surrogate is refused by pydantic (`string_unicode`, 422), so
   this is NUL only. Scenario: any client (the M4 page, a script) that pastes text with a NUL into a highlight
   gets a 500 instead of a field error, and every such request adds to a critical-looking alert. Fix: a model
   validator on `DraftIn` that walks every string of the document and refuses a NUL with a `FieldError` at its
   path (the `_strings` walker of `activation.py:216–225` already produces the paths), or run `drop_nul` over
   the strings in `_validated`; pin it with a test next to the settings one
   (`tests/integration/test_settings_save.py:342`).

2. **A stored profile that no longer validates as `ConfirmedProfile` is a 500 on three GETs** (parked item 6,
   widened). `get_draft` (`profile_review.py:429`), `get_profile` (767) and `get_query_proposal`
   (`api/routes/profile.py:464`, `ProposedQueryOut`) call `model_validate` on stored JSON and let
   `ValidationError` escape to the catch-all, while the confirm answers 409 for the same row
   (`activation.py:240–251`). Scenario: a later release tightens a `Literal` (a new `seniority` enum, M3's
   schema version 2) and every draft or version stored before it turns the Profile page and the proposal poll
   into 500s with alerts, and the only way out is a discard the UI cannot reach because the status call fails.
   Fix: in each GET catch `ValidationError` and answer a degraded body (`draft: None` with a `validation_issues`
   entry `schema_mismatch`, `profile: None` with `purged: false`, `items: None`), or 409 `conflict_state` as the
   confirm does; one test per route with a stored document the current model refuses.

#### Minor (Nice to Have)

1. A receipt `max_tokens_first_stage` makes the next execution start the logical call at the first stage
   again (`graphs/profile/nodes/llm_outcome.py:126–127`; `gateway.py:733–766` always begins at `("first",
   node.max_tokens)`). Scenario: stage 1 stops at `max_tokens` (billed, ~$0.35 at 16,000 Opus output tokens),
   the worker dies before the enlarged try, the reclaim replays stage 1, which stops at `max_tokens` again,
   then pays the enlarged stage. Bounded and rare; a `start_stage` on `CallContext` driven by that receipt
   would skip the known-insufficient stage.
2. An LLM node may make two logical calls (the call and its repair), each with the active `deadline_s`
   (`nodes/extract_profile.py:148–157`, `llm_outcome.py:171–188`), under one `LLM_NODE_TIMEOUT` of 1,860 s
   (`graphs/profile/graph.py:103`). With `deadline_s` above 930 s (§13.1 allows 1,800) a slow call followed by a
   slow repair is cut by the policy: the repair's try settles `ambiguous`, the handler's raise is requeued as
   `bug`, and the next attempt repeats the repair with a full budget. Self-healing and bounded; Deviation 28
   should say the node timeout covers one call, or the policy should be `2 × 1,800 + margin`.
3. (Uncertain likelihood.) A NUL in the model's structured output fails the settlement's `output_json` write
   (`spend_ledger.py:880–902`, `_call_set` → JSONB) in the same transaction as the ledger transition, so a billed
   try is lost to `ambiguous` by the reconciler and the next attempt pays again, up to the attempt limit; the
   same holds for `proposals.publish` (`queries/proposals.py:345–352`). Fix: `drop_nul` over the parsed output's
   strings before `model_dump` in `_response`, or treat it as `invalid_output` (the one repair).
4. `display.timezone` has effect class `none` (`settings/effects.py:72`, as §13.1 says), yet `anchor_utc` is
   computed from it at a `schedule` save (`save.py:246`). A save that changes only the zone leaves the schedule
   anchored in the old zone until the next schedule change. The code follows §13.1 literally; report to the spec
   owner (make `display.timezone` class `schedule`, or re-anchor on every save).
5. Task 24 step 4 says the roadmap's M2 status cell becomes `complete` and the spikes close; the cell is still
   `—` and the Plan column has no link (`2026-10-16-job-hunter-roadmap.md:63`). To be set when this review
   closes.
6. The `profile_vectors` reconciler re-creates `embed_profile` for the active version only
   (`profile/vectors.py:206–216`). A version whose embedding exhausted its attempts while the embedder was down
   and that is no longer active never gets its vectors; harmless for M3's ranking (the active tuple's), noted
   for §12.6 step 3's wording.

### Parked findings

1. **docparse after a parser compromise** — (b) FIX NOW: `pids_limit` on the `docparse` service in
   `deploy/compose.dev.yaml` (and the prod file) and `os.setsid()` as the child's first statement
   (`docparse/worker.py:164`) with `os.killpg` in `_reap` (216–232), so a grandchild cannot outlive the job; (a)
   CARRY to M5 with the runbook: a 0700 subdirectory the worker cannot remove is RAM until both containers
   restart, which the runbook should state, and docparse's own loop should remove leftovers under its root at
   start (it is uid 10002 and can).
2. **Recorder and LangSmith tracing** — FIX NOW: refuse `LANGSMITH_TRACING`/`LANGCHAIN_TRACING_V2` in
   `scripts/spikes/record_anthropic.py`'s `main` (two lines; no guard exists today), since M3 records V5 and V14
   with it.
3. **CI audit without the embedder group** — FIX NOW: `uv sync --frozen --no-install-project --group docparse
   --group embedder` in the `audit` job (`.github/workflows/ci.yml`); the group exists in `pyproject.toml:48`.
4. **Collect nodes can checkpoint DETAIL text** — CARRY to M3: `hide_parameters` (`db/session.py:16`) hides
   bound values but not PostgreSQL's own DETAIL (`Key (…)=(…)`), and `sanitized` wraps the profile graphs only
   (`graphs/profile/graph.py:174–188`); the collect graph's unique keys are ids, hashes and query texts, so the
   exposure is low, and M3 builds the score graph, the right moment to wrap every graph with the one generic
   `sanitized`.
5. **`runs.py:204` EvalPlanQual** — ACCEPT: `_ADMISSION` (`worker/runs.py:203–207`) can null-extend `a` only
   when a concurrent takeover or reclaim raised `runs.attempt_no`; its caller reads `worker_epoch`/`ended_at`
   only after `row.attempt_no != attempt_no` returned `superseded` (316–342), and the stale delivery always
   carries the older number, while the new attempt's job is inserted in the same transaction and cannot run
   before it commits. Splitting it per Deviation 43 is hygiene for M3.
6. **`GET /api/profile/drafts/{id}` 500 on a schema mismatch** — FIX NOW: Important 2 (it also covers
   `GET /api/profile` and the proposal GET).
7. **`company_key`/`fold` not idempotent** — CARRY to M3, before real data: make `normalize.companies.company_key`
   its own fixed point (as `settings/model.py:154–161` does) so the job side and the stored exclusion agree;
   it changes contract-fixture keys, which the fix wave should not do.
8. **Stop-list leaks `kil`** — FIX NOW: subtract `ALT_SPELLING_STOPWORDS` at token level in
   `_large_place_words` (`queries/proposals.py:219–227`) and assert at phrase level; a one-line change with a
   test.
9. **`ClientDisconnect` mid-body on `/cv` and `/cv-text`** — FIX NOW the alert noise: catch
   `starlette.requests.ClientDisconnect` in `read_multipart_limited`/`read_body_limited` (`profile/upload.py:335,
   375`) and raise a 400 `validation_failed`-class problem without the alert; ACCEPT the ~2 s of un-cancelled
   DOCX work (bounded by the entry and record caps).
10. **`spend_ledger` audit** — (a) CARRY to M3: add `admitted_buffer_usd` next to `admitted_cap_usd` in M3's first
    migration so §7.17 #6 can be replayed after a buffer edit; (b) ACCEPT: no M2 writer creates adjustment rows,
    and M3 defines their amount column when it adds one.
11. **Contact-stripping shapes** — ACCEPT: §12.3 makes the review the guarantee, and each shape errs toward
    stripping or is visible in the review; list them in M4's text-review UI test plan.
12. **Further stripping edge cases** — ACCEPT, same reason; the `Berlin Germany → [NAME]` false positive is
    worth a fixture in M4's review tests.
13. **M0's `RunNowIn` ignores unknown keys** — FIX NOW: `extra="forbid"` on `RunNowIn` (one line; the surrogate
    test at `tests/integration/test_idempotent_routes_surrogates.py:29` pins the current 202 and must flip to
    422).
14. **Missing Arbeitnow sweep test** — FIX NOW: a parametrised unit test over `h = 1…168` asserting
    `arbeitnow_coverage_risk` is absent pins Deviation 39 for the spec owner.
15. **Deferred nits** — ACCEPT; `MINIMAL_PROFILE` as a `MappingProxyType` is a trivial change if the wave
    touches that file.
16. **Recorder leaves unreadable modes** — FIX NOW: write recordings destined for
    `tests/contract/fixtures/anthropic` at 0644 in 0755 directories (`scripts/spikes/record_anthropic.py:224–227`),
    keeping 0600/0700 for `.fixtures-raw` and the key file; M3's V5 and V14 go through the same writer.
