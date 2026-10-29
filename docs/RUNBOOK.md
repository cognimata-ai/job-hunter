# Job Hunter runbook

The operations guide of spec §19.8. This file starts with the score lane's entries (M3, plan Task 27): a
paused score lane and its acknowledgement, a quarantined batch and its release, the live schema dry-run before
a prompt-schema version is activated, and the golden eval run on the installation before an activation. M5
adds the host setup, start and stop, the health check, secret rotation, restore and the rest of the alert
table (§19.8).

Every command below runs on the host in the compose directory. `docker compose run --rm --no-deps worker
jobhunter …` starts a one-off container of the worker image with the worker's configuration and secrets; it
never starts a second worker loop (§3.3: the worker stays a singleton). The logs carry ids, counts, codes and
amounts, never ad, CV or profile text.

## A paused score lane

**What you see.** No score cycle starts. The worker logs `score_cycle_not_started` with a `reason`, and a batch
preparation ends its operation with `progress = {"skipped": "paused"}`. `GET /api/system/health` shows the
score lane, and `POST /api/system/score-cycle` answers with the same `reason` at once. Scoring pauses at
admission only: a cycle that is already running finishes.

The reasons, in the order the lane checks them (`scoring.cycles.pause_reason`), and what lifts each:

| Reason | Cause | What to do |
|---|---|---|
| `reservation_bound_violated` | An open, unacknowledged `llm.reservation_bound_violated` (critical): a settled cost exceeded its reservation (§10.7). | Compare the call's usage in the alert's context with `llm/pricing.py` and the prompt overhead constant. Fix and deploy if they are wrong. Then acknowledge the alert. |
| `auth_failed` | An open, unacknowledged `llm.auth_failed` (critical): Anthropic answered 401 or 403. | Rotate or renew `anthropic_key` (§19.2), then `docker compose up -d worker`. Then acknowledge the alert; a successful call also resolves it. |
| `cap_reached` | The month's scoring headroom is below the first reservation of an attempt (§6.9). `llm.cap_reached` (critical) is raised once per month and names any amount a quarantined batch holds. | Nothing to acknowledge: the pause lifts by itself when headroom returns (the next UTC month, or a higher `llm.cap_usd` saved in Settings). Release a quarantined batch only for the reasons below, never to win headroom. |
| `global_fence` | A global purge fence is active (§20.2). | It lifts when the purge completes. If `privacy.purge_failed` is open, follow its entry (§19.8). |
| `schema_unverified` | The active prompt-schema version and model policy have no passed live schema dry-run (§10.11). | Run the live dry-run (below). |

**Acknowledging.** The acknowledgement lifts the two alert pauses while the alert itself stays open, so the
cause stays visible (spec Deviation 19). Find the alert's id in `GET /api/system/health` (its open alerts), or
in System → Health, and acknowledge it there or with `POST /api/alerts/{id}/ack` from a logged-in session.
Acknowledge only after the cause is fixed: the next cycle admits targets at once, and a still wrong bound or a
still refused key fails its calls again.

## A quarantined batch and its release

**What you see.** A batch submission whose create had no definite answer is `submit_ambiguous`: its targets
stay `in_batch` and its reserved spend stays counted (spec R9, §10.10). While it is quarantined, no new batch
is submitted; bulk re-scores go on through online admission. `batch_reconcile_ambiguous` (every 15 min) lists
the workspace's batches and adopts the one whose `custom_id` set equals the submission's, once it has ended.
At 25 h after the submit-start it raises `batch.submit_ambiguous_unresolved` (critical); it never moves on a
timeout. `GET /api/system/spend` shows the held amount as `quarantined_batch_usd`.

**What to do.**
1. Look for the batch in the Anthropic console (Message Batches) created within a few minutes of the
   submission's `submit_started_at`, with its request count. A batch there will be adopted by the next pass
   once it has ended: wait for it.
2. Release only when you are certain that no such batch exists (chair decision C3: an empty listing is no
   proof). Release it from System → Needs attention ("Release & re-score"), or with
   `POST /api/batches/{id}/release` and the body `{"confirm": true}` (an `Idempotency-Key` header is
   required). The response states the reserved amount.
3. After the release the submission is `released`: its targets are `pending` again and are re-scored, its
   reserved amount stays counted as `ambiguous`, and the alert is resolved. The reconciler keeps listing for
   29 days: if the batch turns up after all, its items settle from the provider's per-item results and publish
   nothing.

A `batch.custom_id_anomaly` (critical) means a batch shares only part of the submission's ids: it is never
adopted. Report it as a bug before any release.

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
version.

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
