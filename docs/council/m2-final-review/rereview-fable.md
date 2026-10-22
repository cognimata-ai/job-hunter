# M2 final re-review — Claude Fable 5.1

Range `c845cbe..66016f5` (the two fix waves, 12 commits, 62 files, +3,536/−258), read against the ruled fix
list, the two implementer reports, my round-1 record, the Codex round-1 record and the plan's Deviation 44,
amended Deviation 28 and new carry lines. Run here, read-only: the fix waves' unit and integration modules
(`uv run --group docparse pytest -W error -q` over 27 files: 1,122 passed, 1 skipped as root); the docparse
suite (`-m docparse tests/docparse`: 53 passed); the docparse stack module
(`JOBHUNTER_STACK_PREBUILT=embedder … -m stack tests/stack/test_docparse_no_network.py`: 8 passed on the
already-built `jobhunter-docparse:dev`, every layer cached, the project torn down after); and
`scripts/check-openapi-drift.sh` ("OpenAPI types are up to date"). No network or paid call; nothing under
`.secrets/` or `.fixtures-raw/` read; the working tree is clean and no container or volume was left.

### Verdict

**APPROVE** — every round-1 Important and Minor item of both reviewers is fixed or carried as ruled, the
fixes hold against the six breakage questions of the brief (none changes a run's end status, loses or
double-counts a settlement, fails open or caps a route that legitimately needs a large body), and the two new
findings are Minor.

### Finding verdicts

**My round-1 issues**

- F-I1 (NUL in `PUT /api/profile/drafts/{id}` → 500 + alert): **FIXED**. `_unstorable` walks every string
  of the document (`api/routes/profile_review.py:500–507`, over `types.json_strings`/`unstorable`,
  `types.py:73–96`) and `save_draft` answers 422 `validation_failed` at the path before any read
  (`profile_review.py:531–532`); the surrogate case is covered too
  (`tests/integration/test_profile_review_api.py`, both cases pass here).
- F-I2 (stored document the current model refuses → 500 on three GETs): **FIXED**. `_stored_profile`
  (`profile_review.py:439–459`) gives `draft: null` with the one `schema_mismatch` issue and prepends
  `paste_text` to `repairs` (482–483); `_version_profile` (818–825) gives `profile: null` with the real
  `purged`; the proposal GET leaves failing items out (`api/routes/profile.py:463–480`). No body quotes a
  value or the validation error; `tests/integration/test_schema_drift_reads.py` passes.
- F-M1 (a `max_tokens_first_stage` receipt re-runs the paid first stage): **FIXED**. `walk` gives
  `Call(n, start_stage="enlarged")` (`graphs/profile/nodes/llm_outcome.py:136`), the nodes put it on
  `CallContext` (`extract_profile.py:160`, `propose_queries.py:176`), and the gateway skips the first stage
  (`llm/gateway.py:757`) with the resumed call's first try needing 30 s (`gateway.py:612–616`). See New
  issue M1 for the one case the receipt does not settle.
- F-M2 (one node timeout for two logical calls): **FIXED**. `LLM_NODE_TIMEOUT = 2 * 1800 + 60`
  (`graphs/profile/graph.py:108`); Deviation 28 amended (plan line 2012).
- F-M3 (a NUL in the model's output loses the billed settlement): **FIXED**. `storable(parsed)` is one more
  `invalid_output` condition decided before the settlement transaction (`gateway.py:500–506, 984`), so the
  try settles `billed` with `output_json` NULL (996) and the node's one repair applies;
  `tests/integration/test_unstorable_model_output.py` shows two billed calls and no `ambiguous` entry.
- F-M4 (`display.timezone` effect class vs the schedule anchor): **ACCEPTED-AS-RULED** — reported to the
  spec owner as a spec inconsistency; the code follows §13.1 literally.
- F-M5 (roadmap status row): **ACCEPTED-AS-RULED** — the controller sets it when this review closes; the
  row is still `—` (`docs/superpowers/plans/2026-10-16-job-hunter-roadmap.md:63`), as announced.
- F-M6 (`profile_vectors` reconciler covers the active version only): **ACCEPTED-AS-RULED** — the reason
  holds: the confirm calls `vectors.request_embedding` for the version it activates, reused or new
  (`profile/activation.py:311`), so a re-activated version gets its vectors.

**My parked-item verdicts**

1. (b) FIX NOW: **FIXED** — `os.setsid()` first in the child (`docparse/worker.py:193`), `RLIMIT_NPROC` 0
   (201), `_kill_group` on a deadline kill and once more after the exit (243–251, 270), `pids_limit: 16`
   (`deploy/compose.dev.yaml:193`); the stack module shows a fork refused with EAGAIN in the image. (a) CARRY
   M5: **ACCEPTED-AS-RULED** — the leftover sweep is in now (`worker.py:449–518`), per-job volume isolation,
   the exit-70 alert and the runbook line are the M5 carry (plan line 2061).
2. FIX NOW: **FIXED** — `TRACING_VARS` (`scripts/spikes/record_anthropic.py:116–123`) refused before the
   spike loads, in every mode (968–974).
3. FIX NOW: **FIXED** — `--group docparse --group embedder` (`.github/workflows/ci.yml:181`).
4. CARRY M3: **FIXED** instead (better than my carry) — every collection node is `guarded`
   (`graphs/collect/graph.py:166–174, 206, 213`) by the shared `graphs/sanitize.py:54–77`.
5. ACCEPT: **ACCEPTED-AS-RULED** — the Deviation 43 split of `_ADMISSION` is the M3 hygiene line (plan
   line 2046).
6. FIX NOW: **FIXED** (F-I2).
7. CARRY M3: **FIXED** instead — the filter compares the fixed point of the job's key and industry
   (`scoring/filters.py:305, 312`, `normalize/text.py:167–175`), nothing is rekeyed, and
   `DET_ALGORITHM_VERSION = "2"` (`scoring/versioning.py:10`) makes the stored rows stale once.
8. FIX NOW: **FIXED** — the stop list is subtracted in `words` (`queries/proposals.py:226–229`).
9. FIX NOW for `ClientDisconnect`: **FIXED** — both readers catch it and answer a 400 logged at info
   (`profile/upload.py:251–259, 376–377, 409–410`); the bounded CPU: **ACCEPTED-AS-RULED**.
10. (a) CARRY M3: **ACCEPTED-AS-RULED** (plan line 2046, `admitted_buffer_usd`); (b) ACCEPT:
    **ACCEPTED-AS-RULED**.
11. ACCEPT: **ACCEPTED-AS-RULED** — the shapes are in M4's text-review test plan (plan line 2050).
12. ACCEPT: **ACCEPTED-AS-RULED** — same line.
13. FIX NOW: **FIXED** — `extra="forbid"` on `RunNowIn` (`api/routes/runs.py:43`); the surrogate test flips
    to 422.
14. FIX NOW: **FIXED** — the h = 1…168 sweep (`tests/unit/test_dryrun.py`, passes).
15. ACCEPT: **ACCEPTED-AS-RULED**.
16. FIX NOW: **FIXED** — `Tree.raw` 0600/0700 and `Tree.committed` 0644/0755, directories and files set
    whatever the umask (`record_anthropic.py:247–291`); the key file unchanged.

**The Codex round-1 issues**

- C-I1 (storage-unsafe strings: lost settlement and API 500s): **FIXED** — FR-1 (a) to (d) above, plus the
  settings payload (`settings/validate.py:120–145` refuses a lone surrogate as `invalid` at its path) and the
  sweep of every other body (`tests/integration/test_unstorable_bodies.py`, passes).
- C-I2 (recovery repeats the paid first stage): **FIXED** — FR-2 above; the crash test between the first
  stage's settlement and the enlarged try shows requests `[16000, 32000]` and the first stage never sent
  again (`tests/integration/test_profile_extract_phase.py`, passes).
- C-I3 (a compromised parser survives into later jobs and keeps plaintext): **FIXED** for descendants and
  leftovers (parked 1 above: the group kill, `RLIMIT_NPROC` 0, `pids_limit`, the sweep that exits 70 when it
  cannot remove something); per-job scratch isolation **ACCEPTED-AS-RULED** as the M5 carry, which holds:
  with no process able to outlive a job and copies removed at the next sweep, the remaining window is a
  protocol-shaped copy for up to about three minutes.
- C-I4 (collect exceptions persist DETAIL text in checkpoints): **FIXED** — `guarded` outside `timed` and
  `error_handled`, `PASS_THROUGH = (DomainError, GraphBubbleUp, TimeoutError)`
  (`graphs/collect/graph.py:118`); `tests/integration/test_collect_checkpoint_errors.py` shows no marker in
  any checkpoint table and the run still `failed('bug')`, and the 40001 retry still retried.
- C-M1 → FR-3 **FIXED**; C-M2 → FR-9/FR-16 **FIXED**; C-M3 → FR-8 **FIXED**; C-M4 → FR-13 **FIXED**;
  C-M5 → FR-14 **FIXED**; C-M6 → FR-15 **FIXED**; C-M7 → FR-4 **FIXED** (`api/edge.py:80–81, 148–172,
  220–229`).

**Breakage checks the brief asks for**

- FR-4 against every route that takes a large body: only the multipart upload (10 MiB) and the paste (its
  own 1 MiB with the `text` field) do, and both are exempt by exact method and path
  (`edge.py:81, 220`); every other body is bounded by its model (`Settings`: ≤ 10 locations, 30 + 30
  industries, 200 companies, 100 keywords, 40 queries, 100 watchlist entries, `settings/model.py:301–487`;
  the text PUT ≤ 1,000 integer edits; the profile a few KB). The edge reads the body after the CSRF check and
  before routing, a declared length over the limit is refused unread, and a body within it is replayed in
  its original messages then the server's `receive` (`bounded_body`, 148–172). A client disconnect mid-body
  on a JSON route still reaches FastAPI's own 400 for an unreadable body, as before. The 413 is documented on
  the twelve JSON-body routes (`tests/unit/test_openapi_contract.py:155`); `schema.d.ts` is current.
- FR-11 racing a live job or failing open: the post-job sweep keeps the job just run and every directory
  younger than `LEFTOVER_AFTER_S` 120 s by ctime (`worker.py:107, 491–500`), which the worker's 60 s wait
  cannot exceed (`profile/parse_client.py:221`, pinned by
  `tests/integration/test_parse_client.py:366`); inside a kept directory only regular files of protocol names
  stay (108, 505–517), which is all the worker writes (`parse_client.py:116–129`); a directory's ctime
  cannot be set into the future, so a parse cannot keep a planted directory alive. The worker's own removal
  tolerates a concurrent sweep (`_empty` and `_remove` ignore a vanished entry, `parse_client.py:189–220`)
  and the sweep tolerates the worker's (`FileNotFoundError` is "gone", `worker.py:385–390, 510–511`). It
  fails closed: every entry is tried, one failure raises `SweepFailed` after the pass (449–488), `serve`
  runs the sweep outside the per-job `try` (527–541) and `main` exits 70 (565). The stack module shows the
  leftover removed in the image and the exit-70 restart.
- FR-12 and the run's end status or retries: the lifecycle classifies only `FencedOut` (`lost`,
  `worker/executions.py:149`) and `OutcomeConflict` (`failed_invariant('I7')`, `worker/runs.py:743`), both
  `DomainError`s (`graphs/outcomes.py:18`, `errors.py:10–49`) and so passed through; LangGraph's
  `NodeTimeoutError` is raised outside the node; any other error ended `failed('bug')` before and still does.
  `DB_RETRY` retries the stand-in through `retryable_db_error` (`graphs/sanitize.py:38–51`), computed from
  the original before it is dropped. `functools.wraps` keeps the `(state)` signature LangGraph reads.
- FR-1 (d) settling `billed`: `storable` runs before the settlement transaction opens (`gateway.py:984`),
  the settlement is the unchanged `settle(outcome="billed", usage=…)` with `output_json` NULL (996–1001),
  so the usage is billed exactly once and the counters do not drift
  (`tests/integration/test_gateway.py::test_an_unstorable_output_is_billed_invalid_output_and_its_settlement_commits`,
  passes); the repair re-sends the same prompt, never the unstorable output.
- FR-2's resumed call: the deadline is still fresh per logical call (`extract_profile.py:156`,
  `propose_queries.py:174`), the resumed try reserves the enlarged worst case (`gateway.py:758–775` with
  `max_tokens = max_tokens_retry`), a `max_tokens` stop there is the terminal `max_tokens`, and `cost_usd`
  counts this execution's tries as it did for any call made again after a crash. The repair after a resumed
  call starts at `first` (`llm_outcome.py:189` passes the `Call` itself; the unit test pins it).
- FR-16's one recompute: `STALE_PREDICATE` (`scoring/filters.py:451–455`) is read only by the heavy
  `hard_filter_completeness` reconciler, node 8 of a run, and the scope apply's precompute and final count
  (`settings/scope_apply.py:141, 190`); no listing or ranking path keys on the version, so nothing
  disappears while the rows are recomputed `BATCH` (500) jobs a pass. An apply in flight across the
  deployment refuses its flip while version-1 rows remain (`scope_apply.py:258`) and precomputes them in its
  next round (279–281): self-healing.

**Deviation 44 and the carry lines** are accurate against the code: every clause above is what the code
does, the Deviation 28 text matches `graph.py:108`, the M3 line names the provider-metadata strings the
storable check leaves out (exactly those `CallOutcome` writes at settlement, `gateway.py:988–998`), the M4
line lists the stripping shapes, and the M5 line names the exit-70 alert, the runbook line, per-job
isolation and docparse's `/tmp`. The one wording imprecision is New issue M2.

### New issues

#### Critical

None.

#### Important

None.

#### Minor

1. **A resumed call skips the *current* first stage, which may not be the paid one.** `walk` resumes at
   `enlarged` whenever the newest receipt is `max_tokens_first_stage`
   (`graphs/profile/nodes/llm_outcome.py:133–136`), but the node reads the *active* execution config at each
   execution (`extract_profile.py:150–161`, `propose_queries.py:168–177`) and the receipt carries no token
   limit (`llm/spend_ledger.py:945–952`). Scenario: the first stage stops at `max_tokens` 16,000 and is
   billed; the worker dies; the operator raises `extract_profile.max_tokens` to 32,000 (`max_tokens_retry`
   64,000) and the reclaim resumes at the enlarged stage, reserving the 64,000-token worst case although the
   new first stage might have sufficed. The bill is by usage, so the cost is the larger reservation only: near
   the cap it can be `BudgetExhausted` where the new first stage would have been admitted. Rare and bounded.
   Fix: `llm_calls.max_tokens` of the receipt's try is stored (`_INSERT_CALL`, `spend_ledger.py:364–373`);
   carry it on `Receipt` and let the node start at `first` when the active `max_tokens` exceeds it.
2. **Deviation 44's first sentence overstates the 422.** "Every M2 body refuses a NUL or a lone surrogate in
   any string with 422 at its path" (plan line 2028), but `POST /api/settings/dry-run` answers 200 with the
   `invalid` issue at the path (Deviation 3's preview; `settings/validate.py:120–145`, pinned by
   `tests/integration/test_settings_save.py`) and `POST /api/profile/cv-text` answers 422 `upload_rejected`
   with `not_utf8` and no path (Task 11, as the sentence's own parenthesis half says). Scenario: M4's UI author
   reads the deviation and expects a 422 from the dry-run. Fix: "…with 422 at its path (the settings dry-run
   previews it as `invalid` at the path with 200; `/cv-text` still drops NUL and refuses a surrogate as
   `upload_rejected`, Task 11)".

### Notes

- The one recompute after this deployment is a backlog of `BATCH` jobs per heavy pass, plus whatever node 8
  recomputes for the jobs a run touches; nothing alerts on the backlog, which is fine for a database that does
  not exist yet (Deviation 34). M3's `det_results` take the same version from the start.
- The docparse restart loop when a sweep keeps failing (the worker holds the tmpfs, so the leftover survives
  the restart) is visible only as parse jobs ending `failed('timeout')` until both containers are recreated;
  the M5 carry's alert closes that gap. The sweep needs `/proc` for its reopen-through-descriptor
  (`worker.py:393–400`), which the image has; without it docparse would exit 70, still fail-closed.
- After FR-12 a collection node's unclassified error is diagnosed from `collect_node_failed` (its class),
  since `run_graph_failed` and the checkpoint now name `NodeFailed`; that is the privacy trade the profile
  graphs already made (Deviation 25).
- FR-4 answers a request to a trailing-slash variant of an upload route (`POST /api/profile/cv/`) with 413
  for a body over 1 MiB where FastAPI would have answered a 307; no client sends that.
