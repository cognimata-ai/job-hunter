# Job Hunter runbook

The operations guide of spec §19.8. This file starts with the score lane's entries (M3, plan Task 27): a
paused score lane and its acknowledgement, a quarantined batch and its release, a submitted batch that cannot
be retrieved, the live schema dry-run before a prompt-schema version is activated, and the golden eval run on
the installation before an activation. M5 adds the host setup, start and stop, the health check, secret
rotation, restore and the rest of the alert table (§19.8).

Every command below runs on the host in the compose directory. `docker compose run --rm --no-deps worker
jobhunter …` starts a one-off container of the worker image with the worker's configuration and secrets; it
never starts a second worker loop (§3.3: the worker stays a singleton). The logs carry ids, counts, codes and
amounts, never ad, CV or profile text.

Until M4 the API has no alert list (`GET /api/alerts`, spec §14.2) and no needs-attention list, and the web
UI has no System view (§15.7: Health, Needs attention, the alerts' [Ack], "Release & re-score"): those come
with M4. Today an operator finds ids with read-only queries in the postgres container and acts through the
API routes below.

**A read-only query.** As the application role, over the container's local socket (it selects ids, codes,
states, times and counts only, never ad or profile text):

```
docker compose exec postgres psql -U jobhunter_app -d jobhunter -c "<the query>"
```

**An API call from the host.** Every `/api` call needs the logged-in session's cookie; a state-changing one
also an `Origin` header equal to the public origin (`JOBHUNTER_PUBLIC_ORIGIN`, here `$ORIGIN`) and
`X-JH-Request: 1` (§16.5). Log in once with the body `{"username": "…", "password": "…"}` (and `"totp":
"123456"` when TOTP is on) in a file of mode 0600 that you write with an editor, never with the password on a
command line, and delete the file afterwards:

```
curl -sS -c jh.cookies -H "Origin: $ORIGIN" -H 'Content-Type: application/json' \
  --data @login.json "$ORIGIN/api/auth/login"
rm login.json
```

Then pass `-b jh.cookies -H "Origin: $ORIGIN" -H 'X-JH-Request: 1'` to each call below, and remove
`jh.cookies` when done (`POST /api/auth/logout` ends the session).

## A paused score lane

**What you see.** No score cycle starts. The worker logs `score_cycle_not_started` with a `reason`, and a batch
preparation ends its operation with `progress = {"skipped": "paused"}`. `GET /api/system/health` shows the
reason as the score lane's `pause_reason` (with the open alerts counted by severity), and
`POST /api/system/score-cycle` (an `Idempotency-Key` header of 16 to 128 characters from `A-Z a-z 0-9 _ -`)
answers with the same `reason` at once. Scoring pauses at admission only: a cycle that is already running
finishes.

The reasons, in the order the lane checks them (`scoring.cycles.pause_reason`), and what lifts each:

| Reason | Cause | What to do |
|---|---|---|
| `reservation_bound_violated` | An open, unacknowledged `llm.reservation_bound_violated` (critical): a settled cost exceeded its reservation (§10.7). | Compare the call's usage in the alert's context with `llm/pricing.py` and the prompt overhead constant. Fix and deploy if they are wrong. Then acknowledge the alert. |
| `auth_failed` | An open, unacknowledged `llm.auth_failed` (critical): Anthropic answered 401 or 403. | Rotate or renew `anthropic_key` (§19.2), then `docker compose restart worker`: the worker reads its file secrets at start, and `up -d` would keep the running container, with the old key, when its compose configuration did not change. Then acknowledge the alert; a successful call also resolves it. |
| `cap_reached` | The month's scoring headroom is below the first reservation of an attempt (§6.9). `llm.cap_reached` (critical) is raised once per month and names any amount a quarantined batch holds. | Nothing to acknowledge: the pause lifts by itself when headroom returns (the next UTC month, or a higher `llm.cap_usd` in a settings save, `POST /api/settings`; the Settings page comes with M4). Release a quarantined batch only for the reasons below, never to win headroom. |
| `global_fence` | A global purge fence is active (§20.2). | It lifts when the purge completes. The `privacy.purge_failed` entry comes with M5's runbook (spec §19.8 L6300), with the purge procedures. |
| `schema_unverified` | The active prompt-schema version and model policy have no passed live schema dry-run (§10.11). | Run the live dry-run (below). |

**Acknowledging.** The acknowledgement lifts the two alert pauses while the alert itself stays open, so the
cause stays visible (spec §17.6 L5608–5609; M3 plan Deviation 19). Acknowledge only after the cause is fixed:
the next cycle admits targets at once, and a still wrong bound or a still refused key fails its calls again.
1. Find the alert's id with a read-only query:

   ```
   SELECT id, code, severity, first_at, last_at, occurrences, acknowledged_at FROM alerts
    WHERE code IN ('llm.auth_failed', 'llm.reservation_bound_violated') AND resolved_at IS NULL;
   ```

2. Acknowledge it: `curl -sS -b jh.cookies -H "Origin: $ORIGIN" -H 'X-JH-Request: 1' -X POST
   "$ORIGIN/api/alerts/<id>/ack"` (200 with `acknowledged_at`; 404 when no open alert has that id).
3. `GET /api/system/health` then reports no `pause_reason` (or the next one in the order above).

## A quarantined batch and its release

**What you see.** A batch submission whose create had no definite answer is `submit_ambiguous`: its targets
stay `in_batch` and its reserved spend stays counted (spec R9, §10.10). While it is quarantined, no new batch
is submitted; bulk re-scores go on through online admission. `batch_reconcile_ambiguous` (every 15 min) lists
the workspace's batches and adopts the one whose `custom_id` set equals the submission's, once it has ended.
At 25 h after the submit-start it raises `batch.submit_ambiguous_unresolved` (critical); it never moves on a
timeout. `GET /api/system/spend` shows the held amount as `quarantined_batch_usd`.

**What to do.**
1. Find the submission. The worker logged `batch_submit_ambiguous` with its `submission_id` when it became
   quarantined (and `batch_execution_lost`, with `submission_ids`, when a lost execution's recovery made it
   so). Its submit-start time and request count come from a read-only query:

   ```
   SELECT id, state, submit_started_at, request_count, reserved_usd, last_reconcile_at FROM batch_submissions
    WHERE state = 'submit_ambiguous';
   ```

2. Look for the batch in the Anthropic console (Message Batches) created within a few minutes of that
   `submit_started_at`, with that request count. A batch there will be adopted by the next pass once it has
   ended: wait for it.
3. Release only when you are certain that no such batch exists (chair decision C3: an empty listing is no
   proof):

   ```
   curl -sS -b jh.cookies -H "Origin: $ORIGIN" -H 'X-JH-Request: 1' -H 'Content-Type: application/json' \
     -H "Idempotency-Key: release-<id>-$(date +%s)" --data '{"confirm": true}' \
     "$ORIGIN/api/batches/<id>/release"
   ```

   It answers 202 with `{"submission_id", "state": "released", "reserved_usd"}`: the amount that stays counted.
   A submission that is not `submit_ambiguous` is 409 `conflict_state`. ("Release & re-score" in System →
   Needs attention comes with M4's UI.)
4. After the release the submission is `released`: its targets are `pending` again and are re-scored, its
   reserved amount stays counted as `ambiguous`, and the alert is resolved. The reconciler keeps listing for
   29 days: if the batch turns up after all, its items settle from the provider's per-item results and publish
   nothing.

A `batch.custom_id_anomaly` (critical) means a batch shares only part of the submission's ids: it is never
adopted. Report it as a bug before any release.

## A submitted batch that cannot be retrieved (`batch.retrieve_failing`)

**What it means.** The provider accepted the batch, so its submission is `submitted` with the provider's
`provider_batch_id` and an `expires_at` 24 h after the batch's creation, by which time the provider has ended
every batch. `batch_poll` (every 5 min) retrieves the batch, and once it has ended moves the submission to
`ended` and processes its results. Each failed retrieval logs `batch_retrieve_failed` (warning) with the
`submission_id`, the `error_class` and the HTTP `status`. A retrieval that still fails past `expires_at + 1 h`
raises the critical alert `batch.retrieve_failing` (its subject the submission id, its context the
`submission_id` and the `provider_batch_id`) and logs `batch_retrieve_failing`; every later failing pass raises
it again. While the submission stays `submitted`, its targets stay `in_batch`, its reserved spend stays counted
against the month, and no new batch is submitted for the tuple (a batch preparation ends `in_flight`). The
tuple's other pending targets are still scored by the online cycles.

**What to do.**
1. Find the submission and the alert with read-only queries:

   ```
   SELECT id, state, provider_batch_id, submitted_at, expires_at, request_count, reserved_usd
     FROM batch_submissions WHERE state = 'submitted';
   SELECT id, subject, first_at, last_at, occurrences FROM alerts
    WHERE code = 'batch.retrieve_failing' AND resolved_at IS NULL;
   ```

2. Read the worker's `batch_retrieve_failed` lines of that `submission_id` (`docker compose logs worker`).
   Their `status` says why the retrieval fails:
   - 401 or 403: the key is refused. Rotate or renew `anthropic_key` and restart the worker, as for
     `auth_failed` above.
   - no status (a connection failure), 429 or a 5xx: the network or the provider is failing. Wait: the poll
     retries every 5 minutes.
   - 404: the provider does not know the batch. Look for its `provider_batch_id` in the Anthropic console
     (Message Batches).
3. Once a retrieval succeeds and the batch has ended, the next poll moves the submission to `ended`, resolves
   the alert and processes the results as usual. Nothing else is needed.
4. A batch that is gone for good (404, and not in the console) has no operator path today. The batch state
   machine (`db/transitions.py`, `batch_submissions`) lets a `submitted` submission move only to `ended`, and
   only the poll makes that move, after a successful retrieval. No API route or CLI command moves it, and the
   database's transition trigger refuses any other move of its state. Do not edit the row by hand. The
   submission keeps its targets and its reserved spend until an operator path arrives in M4. Acknowledging the
   alert (`POST /api/alerts/<id>/ack`) records that you saw it; it moves nothing, and the alert stays open.

## The live schema dry-run before a prompt-schema version is activated

A prompt-schema version can be activated (`POST /api/settings/prompt-schema-versions/{id}/activate`) only once
a live schema dry-run has passed for that version and every model the active model policy uses (§10.11); the
activation, a save that changes the model policy, and the score lane's `schema_unverified` pause all read the
same record. A process on the fake LLM skips the check.

1. Make sure `anthropic_key` is set (§19.2). The run sends one minimal request per structured-output schema and
   model (effort `low`, `max_tokens` 2,000); the actual cost is below $0.10 and the worst-case reservations are
   about $0.25, drawn from the non-scoring reserve.
2. Run it for the version to activate (its id from `GET /api/settings/prompt-schema-versions`):

   ```
   docker compose run --rm --no-deps worker jobhunter schema-dry-run --live --version <id>
   ```

   For a model-policy change that is not saved yet, add `--candidate-payload <settings.json>` (the document you
   are about to save); `--model-policy <id>` names an existing policy instead.
3. The output is a table of schema, model and result, and a summary line. Exit 0 means passed; 1 means a case
   failed (read the failed cases: a refused keyword or a changed transformed schema is a bug to report before
   any activation); 2 means the run could not start (the key is missing, `JOBHUNTER_FAKE_LLM` is set, or the
   candidate is invalid). The run is stored as an `eval_runs` row of kind `schema_dry_run`.
4. Activate the version. A refusal with `schema_dry_run_required` names the (schema, model) pairs still without
   a passed live case.

The same dry-run can be started from GitHub with the manual workflow `schema-dry-run-manual.yml`.

## The golden eval run on the installation before an activation

Activating a rubric or prompt-schema version, or saving a model-policy or evaluation-policy change, must pass
the eval gate of §11.13. The gate reads the newest completed golden run of the candidate (its rubric,
prompt-schema version, model policy and evaluation policy) on this installation's golden set. With fewer than
30 active cases it is skipped ("uncalibrated"); with 30 or more and no run, it fails. A failed gate can only be
overridden by acknowledging it at the activation (`eval_override_ack`), which is stored on the settings
version. A run's own gate counts only the user's cases it evaluated that are still active when it ends: a case
added, activated or deactivated while it runs never brings a run of fewer cases to 30 (it ends uncalibrated,
`passed` empty, which the gate reads as failed once 30 cases are active). Label first, then start the run.

1. If the candidate's prompt-schema version is new, run the live dry-run above first: the golden run sends the
   candidate's real requests.
2. Start the run and wait for its end. For the version to activate, name the candidate's ids
   (`<rubric>,<prompt_schema>,<model_policy>,<evaluation_policy>,<execution_config>`), or pass the settings
   document you are about to save:

   ```
   docker compose run --rm --no-deps worker jobhunter eval run --kind golden --candidate <ids> --live
   docker compose run --rm --no-deps worker jobhunter eval run --kind golden \
     --candidate-payload <settings.json> --live
   ```

   `--candidate active` evaluates the active configuration (for a baseline). The command records the run and
   its `eval_run` operation and waits while the running worker scores it on the production route: every
   active case once, then ten of them twice, through the non-scoring reserve (about $2.70 a golden run at the
   §21 figures). `--route forced_critic` adds a diagnostic run that reviews every case; it is
   reported, never gates.
3. A run the worker could not finish (a restart, a lost operation) ends `failed` with `metrics.failed =
   "interrupted"`; it is never resumed. Start a new one. A run requested `--live` on a worker in fake mode, or
   the reverse, fails at once with `"transport_mismatch"` and sends nothing.
4. Read the result in the run's row: `passed`, and in its `metrics` the gate's state and, when it failed,
   `failed_gates`. Then activate. A failed gate raises `eval.gate_failed` (warn); do not override it without
   reading which gate failed.

The daily canary run (`canary_daily`, 06:41 UTC) re-scores the canary cases with the active configuration and
raises `scoring.canary_drift` (warn) for a canary more than 8 points from its first score; it needs no action
before an activation.
