"""M3 acceptance smoke test (Task 28): the score lane on the dev stack, every Claude call answered by the
recorded fake (the Message Batches API included) and every source by the fixtures.

    docker compose --env-file deploy/images.lock -f deploy/compose.dev.yaml -f deploy/compose.fixtures.yaml \
        -f deploy/compose.fakellm.yaml down -v
    docker compose --env-file deploy/images.lock -f deploy/compose.dev.yaml -f deploy/compose.fixtures.yaml \
        -f deploy/compose.fakellm.yaml up -d --build --wait
    uv run python scripts/smoke_m3.py

The precondition is M2's: a FRESH dev stack with both overlays. The script creates the one user, so a stack
that has one already fails the first check with M2's hint. It never deletes anything.

It imports `scripts/smoke_m2.py` and runs its checks 1-8 as they are (the user, the login, the health, the CV
through the profile flow on the fake, the confirm and its query proposal), then seven of its own. Its compose
calls pass the three files and the digest lock as `smoke_m2.DC` does; `psql` (`scripts/smoke_m0.py`) is a
`docker compose exec` into the running `postgres` container as the bootstrap role. Each check prints exactly
one JSON line, `{"check": <name>, "ok": <bool>}` with an optional `"detail"`:

9. `targets_created`: M1's inputs (`smoke_m1.SEED_INPUTS`) and four more watchlist boards of the fixtures
   (`SEED_BOARDS`: one collection of M1's inputs alone retains about 140 revisions, fewer than
   `batch_min_targets`; with the four, 313), then a paid run on the fixtures (posted only once the worker is
   on both fakes) that succeeds; then, polled with `psql` for 120 s, the active tuple's
   `create_targets_for_tuple` (the confirm's) `succeeded` and the tuple has a target for every retained
   current revision (`RETAINED_JOB_SQL`, as `scoring.targets` defines it), each with its `similarity` set
   once `embed_profile` ran;
10. `score_cycle`: `POST /api/system/score-cycle` answers 202 `queued = true`; within 180 s the `manual` cycle
    the worker created is `completed`, at least `min(S, pending)` of its targets are `published_accepted |
    published_contested` (`S` its admission size, `pending` the tuple's pending targets at the request),
    every published result of the tuple has a current `det_results` row for the active scope, and
    `GET /api/system/spend` counts scoring spend above $0 with the `c_hat` and `critic_rate_7d` keys present;
11. `score_now`: a `pending` job's `POST /api/jobs/{id}/score-now` answers 202, and its target is published
    within 120 s with `override = 'score_now'`;
12. `feedback`: `POST /api/jobs/{id}/feedback` (a label on a published job) answers 201 with a
    `golden_case_id`, and `GET /api/golden-cases` lists that case;
13. `scope_apply`: a scope save (one location, `SCOPE_LOCATION` at `SCOPE_RADIUS_KM`) answers 201 with its
    apply operation; the apply flips within 120 s (the operation `succeeded`, the new scope active, none
    pending) and every published result of the tuple has a current instance for the new scope;
14. `batch_path` (Ruling T28-e): `jobhunter golden seed-adversarial` in the worker container exits 0 with 10
    cases; a second CV through the profile flow, its draft saved with one more target title (an unchanged
    profile activates nothing) and confirmed as `version_no` 2; then the old bulk operation is `cancelled`
    (or, when no target carries it, `completed`: `OLD_BULK`) and the new one `active`, the new tuple's
    `create_targets_for_tuple` `succeeded` with every target of the tuple carrying the new bulk operation
    (they came from that operation: Ruling T28-c), and the
    `batch_submit` it requested `succeeded` with `skipped = below_minimum` and no submission, its eligible
    targets (`ELIGIBLE_SQL`, as `scoring.batches` defines it) fewer than `batch_min_targets`. The line
    counts the eligible targets and the tuple's `expired_unscored('too_old')` ones;
15. `health`: `GET /api/system/health` has `scoring.pause_reason = null` and `scoring.running_cycle_id =
    null` (once the running cycle, if any, has ended: 120 s), and the month's counters equal the ledger
    (`COUNTERS_CLEAN`: `spend_ledger.recompute`'s formula, `_COUNTED`, over `psql`).

**Why check 14 stops at the skip.** The seeded settings already hold `scoring.batch_min_targets = 200`, the
settings model's floor, so the smoke does not lower it. The fixtures' postings keep their recorded dates,
which age past `scope.max_ad_age_days`: the batch preparation's expiry sweep moves the second confirm's
targets of those ads to `expired_unscored('too_old')`, fewer than `batch_min_targets` stay eligible, and the
preparation ends `below_minimum`. The count of eligible targets only falls as the dates age; the line prints
it with the `too_old` count. (On 2026-11-01: 313 retained revisions, 242 pending targets at the confirm, 129
moved `too_old`, 113 eligible.) The submission on the fake Batches API, its polls, the reconcile, the
decided items and the follow-up cycle are proven in process by
`tests/integration/test_m3_end_to_end.py::test_scoring_end_to_end_on_the_fake_llm` (250 items). Fixture
dates relative to the fake's clock, which would let the stack show the batch too, are carried to M4.

**The bucket** (§6.9.2; Ruling T28-f) paces the score lane at the month's rate, about $0.13 an hour on the dev
settings, and a cycle other than Score now starts only when it holds `min(min_cycle_targets x ĉ, B_max)`. The
first cycle spends it and more: its size is set by the central ĉ ($0.052646: 28 targets of the $1.50
`B_max`), while every target on the committed recordings takes six calls (about $0.28), so the bucket ends
some $6 below zero, and a cycle after a periodic one would wait days. Like M0's `UPDATE_SLOT_DUE`, which
makes a scheduled run due, the smoke advances the bucket's clock (`ADVANCE_BUCKET`: the month row's
`bucket_at` a month back, so the next accrual fills it to `B_max`) right before check 10's request, in a
quiet moment (`quiet_moment`: no cycle running, no `score_cycle` job waiting, no tick running) at a second of
the minute away from the worker's tick (`AWAY_FROM_THE_TICK`), so neither a periodic cycle nor a running
cycle's settlements take the refill first. The counters and the ledger are untouched, so check 15
recomputes them clean.

It stops at the first failed check (exit 1); after fifteen `ok` lines it exits 0. A `detail` holds ids,
states, counts, codes and amounts, and fixed text: never the password, the session cookie or its token
(`Context.scrub`, as in M0), a key, an ad's text, a CV text or a draft.
"""

import copy
import re
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

import httpx
import smoke_m2  # scripts/ is the script's directory, the first entry of sys.path
from smoke_m0 import (
    DOCKER_TIMEOUT_S,
    HTTP_TIMEOUT_S,
    ORIGIN,
    POLL_INTERVAL_S,
    CheckFailed,
    Context,
    emit,
    pick,
    stderr_tail,
)
from smoke_m1 import SEED_INPUTS, query_row, run_now, wait_succeeded
from smoke_m2 import (
    CV_FILENAME,
    EXTRACT_WITHIN_S,
    TEXT_REVIEW_WITHIN_S,
    draft_reached,
    json_object,
    mutating,
    poll,
    synthetic_pdf,
    usd,
    worker_flags,
)

DC: Final = smoke_m2.DC
M2_CHECKS: Final = 8  # smoke_m2's checks 1-8: through the confirm and its query proposal

# Check 9. Four boards of the fixtures besides M1's Greenhouse board (sources/fixtures.py ROUTES): Lever's EU
# host (100 postings), Ashby (42), Recruitee (22) and Personio (8). One statement, as M1's: the companies,
# their board instances (`instances.instance_for_company`'s key: `<region>:<slug>` for Lever) and the
# active settings version with every company appended to `watchlist[]`. Constants only.
BOARDS: Final = (
    ("lever", "leverdemo", "'eu'"),
    ("ashby", "n8n", "NULL"),
    ("recruitee", "bunq", "NULL"),
    ("personio", "everphone", "NULL"),
)
_BOARD_ROWS: Final = ", ".join(
    f"('{slug}', '{slug}', '{ats}', '{slug}', {region}, 'manual', 'ok', now())"
    for ats, slug, region in BOARDS
)
SEED_BOARDS: Final = f"""\
WITH c AS (
  INSERT INTO watchlist_companies (display_name, company_key, ats_type, board_slug, lever_region, detection,
    validation_status, validated_at)
  VALUES {_BOARD_ROWS} RETURNING id, ats_type, board_slug, lever_region
), i AS (
  INSERT INTO source_instances (source_id, instance_key, watchlist_company_id)
  SELECT c.ats_type, CASE WHEN c.ats_type = 'lever' THEN c.lever_region || ':' || c.board_slug
    ELSE c.board_slug END, c.id FROM c RETURNING id
), s AS (
  UPDATE settings_versions v SET payload = jsonb_set(v.payload, '{{watchlist}}', v.payload->'watchlist'
      || (SELECT jsonb_agg(jsonb_build_object('watchlist_company_id', c.id, 'enabled', true) ORDER BY c.id)
          FROM c))
  FROM active_config a WHERE a.id = 1 AND v.id = a.settings_version_id RETURNING v.id
)
SELECT (SELECT count(*) FROM c), (SELECT count(*) FROM i), (SELECT count(*) FROM s)"""  # noqa: S608 (constants only)
PAID_RUN_KEY: Final = "smoke-m3-run-paid-0001"
# The run embeds every revision it creates (about 330 here) on the dev embedder at about 0.55 docs/s
# (smoke_m1's COLLECT_WITHIN_S): some 10 minutes.
COLLECT_WITHIN_S: Final = 1200
TARGETS_WITHIN_S: Final = 120
# A current revision of a retained job (`jobs j`), as `scoring.targets.RETAINED_JOB_SQL` (pinned by the test).
RETAINED_JOB_SQL: Final = (
    "j.merged_into_job_id IS NULL AND j.current_revision_id IS NOT NULL AND NOT j.synthetic_eval"
)
ACTIVE: Final = "SELECT llm_tuple_id, scope_version_id FROM active_config WHERE id = 1"
# `{tuple}` is an integer the database answered. The tuple's newest `create_targets_for_tuple` operation, the
# retained current revisions, those with a target of the tuple, and those targets without a similarity.
TARGETS_CREATED: Final = f"""\
SELECT coalesce((SELECT status FROM operations WHERE kind = 'create_targets_for_tuple'
    AND (payload->>'llm_tuple_id')::bigint = {{tuple}} ORDER BY id DESC LIMIT 1), 'none'),
  (SELECT count(*) FROM jobs j WHERE {RETAINED_JOB_SQL}),
  (SELECT count(*) FROM jobs j JOIN score_targets t ON t.job_revision_id = j.current_revision_id
    AND t.llm_tuple_id = {{tuple}} WHERE {RETAINED_JOB_SQL}),
  (SELECT count(*) FROM jobs j JOIN score_targets t ON t.job_revision_id = j.current_revision_id
    AND t.llm_tuple_id = {{tuple}} WHERE {RETAINED_JOB_SQL} AND t.similarity IS NULL)"""  # noqa: S608

# Check 10: the bucket's clock a month back (the module docstring; Ruling T28-f), the month's row only, and
# neither its counters nor the ledger. The precedent is M0's `UPDATE_SLOT_DUE` (scripts/smoke_m0.py), which
# moves the schedule's clock so that a run is due now.
MONTH_SQL: Final = "CAST(date_trunc('month', now() AT TIME ZONE 'UTC') AS date)"
ADVANCE_BUCKET: Final = (
    "WITH u AS (UPDATE llm_budget SET bucket_at = bucket_at - interval '1 month' "  # noqa: S608
    f"WHERE month = {MONTH_SQL} RETURNING month) SELECT count(*) FROM u"
)
# The worker's tick runs at the top of each minute (`jh:tick`, `* * * * *`); its periodic trigger reads the
# bucket. A refill and the request that should use it happen at one of these seconds of the minute.
AWAY_FROM_THE_TICK: Final = range(10, 46)
# Running cycles; waiting `score_cycle` jobs and a running `tick` (whose periodic trigger reads the bucket, so
# a slow tick could still take a refill: Task 28 fix round 1); the newest cycle's id.
LANE_STATE: Final = """\
SELECT (SELECT count(*) FROM score_cycles WHERE status = 'running'),
  (SELECT count(*) FROM procrastinate_jobs
    WHERE (task_name = 'jh:score_cycle' AND status IN ('todo', 'doing'))
      OR (task_name = 'jh:tick' AND status = 'doing')),
  (SELECT coalesce(max(id), 0) FROM score_cycles)"""
QUIET_WITHIN_S: Final = 600
CYCLE_WITHIN_S: Final = 180
SCORE_CYCLE_KEY: Final = "smoke-m3-score-cycle-0001"
PUBLISHED: Final = "('published_accepted', 'published_contested')"
PENDING_OF: Final = "SELECT count(*) FROM score_targets WHERE llm_tuple_id = {tuple} AND state = 'pending'"
# The first cycle of `trigger` after cycle `{after}`: its id, status, admission size, and its targets
# published.
CYCLE_AFTER: Final = f"""\
SELECT coalesce(c.id, 0), coalesce(c.status, 'none'), coalesce(c.admission_size, 0),
  (SELECT count(DISTINCT a.target_id) FROM score_attempts a JOIN score_targets t ON t.id = a.target_id
    WHERE a.cycle_id = c.id AND t.state IN {PUBLISHED})
FROM (SELECT 1) one LEFT JOIN LATERAL (SELECT id, status, admission_size FROM score_cycles
  WHERE id > {{after}} AND trigger = '{{trigger}}' ORDER BY id LIMIT 1) c ON true"""  # noqa: S608
# The tuple's published targets, and those whose published result has no current instance for `{scope}`.
INSTANCES_OF: Final = f"""\
SELECT (SELECT count(*) FROM score_targets WHERE llm_tuple_id = {{tuple}} AND state IN {PUBLISHED}),
  (SELECT count(*) FROM score_targets t WHERE t.llm_tuple_id = {{tuple}} AND t.state IN {PUBLISHED}
    AND NOT EXISTS (SELECT 1 FROM det_results d WHERE d.llm_result_id = t.published_result_id
      AND d.scope_version_id = {{scope}} AND d.is_current))"""  # noqa: S608
SCORING_ROLES: Final = frozenset({"generate", "critic", "revise"})

# Check 11.
PENDING_JOB: Final = (
    "SELECT job_id, id FROM score_targets WHERE llm_tuple_id = {tuple} AND state = 'pending' "
    "AND override = 'none' ORDER BY id LIMIT 1"
)
TARGET_STATE: Final = "SELECT state, override FROM score_targets WHERE id = {target}"
SCORE_NOW_WITHIN_S: Final = 120
SCORE_NOW_KEY: Final = "smoke-m3-score-now-0001"
FAILED_TARGET: Final = re.compile(r"refused|failed_.*|cancelled")

# Check 12.
PUBLISHED_JOB: Final = (
    f"SELECT job_id, id FROM score_targets WHERE llm_tuple_id = {{tuple}} AND state IN {PUBLISHED} "  # noqa: S608
    "ORDER BY id LIMIT 1"
)
FEEDBACK: Final = {"kind": "label", "label": "good"}

# Check 13: Berlin (the gazetteer's id and point, as tests/support/settings.BERLIN) at the largest radius
# the settings allow, so the second confirm's batch keeps most of the collected jobs.
SCOPE_LOCATION: Final = {
    "label": "Berlin",
    "gazetteer_id": "geonames:2950159",
    "lat": 52.52437,
    "lon": 13.41053,
}
SCOPE_RADIUS_KM: Final = 200
SCOPE_KEY: Final = "smoke-m3-scope-0001"
APPLY_WITHIN_S: Final = 120
# The apply operation `{operation}`, the active scope and the pending one.
APPLY_STATE: Final = """\
SELECT o.status, a.scope_version_id, coalesce(a.pending_scope_version_id, 0)
FROM operations o CROSS JOIN active_config a WHERE o.id = {operation} AND a.id = 1"""
FILTERED_BY: Final = (
    "SELECT count(*) FROM score_targets WHERE llm_tuple_id = {tuple} AND state = 'filtered_out' "
    "AND filter_scope_version_id = {scope}"
)

# Check 14.
GOLDEN_SEED: Final = ["jobhunter", "golden", "seed-adversarial"]
GOLDEN_CASES: Final = 10  # §11.13: the ten adversarial fixtures
SEEDED: Final = re.compile(r"golden seed-adversarial: cases=(\d+) ")
APPROVE_KEY: Final = "smoke-m3-approve-0002"
CONFIRM_KEY: Final = "smoke-m3-confirm-0002"
ADDED_TITLE: Final = {"title": "Platform Engineer", "lang": "en"}
BATCH_MIN_TARGETS: Final = 200  # the seeded `scoring.batch_min_targets`, the settings model's floor
SUBMIT_WITHIN_S: Final = 120
# The tuple's newest `create_targets_for_tuple`, its targets, those of bulk operation `{bulk}`, and the
# retained current revisions without a target of the tuple.
BULK_TARGETS: Final = f"""\
SELECT coalesce((SELECT status FROM operations WHERE kind = 'create_targets_for_tuple'
    AND (payload->>'llm_tuple_id')::bigint = {{tuple}}
    AND (payload->>'bulk_operation_id')::bigint = {{bulk}} ORDER BY id DESC LIMIT 1), 'none'),
  (SELECT count(*) FROM score_targets WHERE llm_tuple_id = {{tuple}}),
  (SELECT count(*) FROM score_targets WHERE llm_tuple_id = {{tuple}} AND bulk_operation_id = {{bulk}}),
  (SELECT count(*) FROM jobs j WHERE {RETAINED_JOB_SQL} AND NOT EXISTS (SELECT 1 FROM score_targets t
    WHERE t.job_revision_id = j.current_revision_id AND t.llm_tuple_id = {{tuple}}))"""  # noqa: S608
BULK_OF: Final = "SELECT id, state FROM bulk_operations WHERE llm_tuple_id = {tuple} ORDER BY id DESC LIMIT 1"
# The first bulk operation `{old}` after the second confirm: its state, and how many targets carry it. The
# confirm's activation cancels the old tuple's operation while it is `active`. The heavy reconciler
# `bulk_operations` completes an operation once its work is done, and the first confirm's has none when no
# target carries it (its `create_targets_for_tuple` found no job: the collection's targets carry no bulk
# operation), so a heavy pass between the two confirms may leave it `completed` instead. An operation that
# targets carry must have been `cancelled`.
OLD_BULK: Final = """\
SELECT o.state, (SELECT count(*) FROM score_targets WHERE bulk_operation_id = {old})
FROM bulk_operations o WHERE o.id = {old}"""
# The bulk operation's newest `batch_submit`: its status and what its progress skipped; the bulk
# operation's submissions.
BATCH_SUBMIT_OF: Final = """\
SELECT coalesce(o.status, 'none'), coalesce(o.progress->>'skipped', 'none'),
  (SELECT count(*) FROM batch_submissions WHERE bulk_operation_id = {bulk})
FROM (SELECT 1) one LEFT JOIN LATERAL (SELECT status, progress FROM operations WHERE kind = 'batch_submit'
  AND (payload->>'bulk_operation_id')::bigint = {bulk} ORDER BY id DESC LIMIT 1) o ON true"""
# A batch candidate of bulk operation `:b` under evaluation policy `:policy`, as
# `scoring.batches.ELIGIBLE_SQL` (pinned by the test); `ELIGIBLE_OF` binds the bulk operation and the active
# policy.
ELIGIBLE_SQL: Final = (
    "t.bulk_operation_id = :b AND t.state = 'pending' AND (t.not_before IS NULL OR t.not_before <= now()) "
    "AND NOT is_fenced(t.job_id, t.llm_tuple_id) AND t.resume_draft_id IS NULL "
    "AND NOT (t.resume_draft_id IS NOT NULL OR EXISTS (SELECT 1 FROM score_attempts a "
    "JOIN score_drafts d ON d.attempt_id = a.id "
    "WHERE a.target_id = t.id AND a.evaluation_policy_id = :policy)) "
    "AND (t.lane IS NULL OR t.lane NOT IN ('batch_followup', 'score_now')) "
    "AND (t.state_reason IS NULL OR t.state_reason NOT IN ('batch_max_tokens', 'batch_invalid_output', "
    "'batch_errored', 'batch_canceled', 'batch_expired', 'batch_missing')) "
    "AND NOT EXISTS (SELECT 1 FROM batch_items i WHERE i.target_id = t.id AND i.result_type <> 'awaiting') "
    "AND t.charged_attempts < 4 AND t.override = 'none'"
)
ACTIVE_POLICY: Final = "(SELECT evaluation_policy_id FROM active_config WHERE id = 1)"
# `{bulk}` and `{tuple}` are integers the database answered: the eligible targets, and the tuple's targets
# whose ads are too old.
ELIGIBLE_OF: Final = (
    "SELECT (SELECT count(*) FROM score_targets t WHERE "  # noqa: S608
    + ELIGIBLE_SQL.replace(":b", "{bulk}").replace(":policy", ACTIVE_POLICY)
    + "), (SELECT count(*) FROM score_targets WHERE llm_tuple_id = {tuple} AND state = 'expired_unscored' "
    "AND state_reason = 'too_old')"
)
BELOW_MINIMUM: Final = "below_minimum"

# Check 15: `spend_ledger._COUNTED` (pinned by the test) and `_COUNTERS`' comparison for the month.
COUNTED: Final = (
    "CASE WHEN kind = 'adjustment' THEN reserved_usd WHEN state = 'billed' THEN settled_usd "
    "WHEN state IN ('reserved', 'dispatched', 'ambiguous') THEN reserved_usd ELSE 0 END"
)
COUNTERS_CLEAN: Final = f"""\
WITH sums AS (SELECT SUM({COUNTED}) AS counted,
    SUM({COUNTED}) FILTER (WHERE purpose_class = 'scoring') AS scoring,
    SUM({COUNTED}) FILTER (WHERE purpose_class = 'nonscoring') AS nonscoring
  FROM llm_spend_ledger WHERE budget_month = {MONTH_SQL})
SELECT b.counted_usd = coalesce(s.counted, 0) AND b.scoring_counted_usd = coalesce(s.scoring, 0)
    AND b.nonscoring_counted_usd = coalesce(s.nonscoring, 0), b.counted_usd, b.scoring_counted_usd
FROM llm_budget b CROSS JOIN sums s WHERE b.month = {MONTH_SQL}"""  # noqa: S608
HEALTH_WITHIN_S: Final = 120


class State:
    """What the checks hand on: the active tuple and scope, a published job, and the first bulk operation."""

    tuple_id: int | None = None
    scope_id: int | None = None
    published_job_id: int | None = None


STATE: Final = State()


def ints(fields: list[str]) -> list[int]:
    try:
        return [int(field) for field in fields]
    except ValueError:
        raise CheckFailed(f"expected integers, got {fields!r}") from None


def poll_row(
    sql: str, columns: int, done: Callable[[list[str]], tuple[bool, str]], *, within_s: int, what: str
) -> list[str]:
    """`sql`'s one row, read with `psql` until `done(row)` says so, or `within_s` seconds have passed."""
    deadline = time.monotonic() + within_s
    while True:
        row = query_row(sql, columns)
        finished, last = done(row)
        if finished:
            return row
        if time.monotonic() >= deadline:
            raise CheckFailed(f"{what} not reached within {within_s} s (last: {last})")
        time.sleep(POLL_INTERVAL_S)


def active() -> tuple[int, int]:
    tuple_id, scope_id = query_row(ACTIVE, 2)
    if not tuple_id.isdigit() or not scope_id.isdigit():
        raise CheckFailed("no active tuple or scope")
    return int(tuple_id), int(scope_id)


def await_quiet_lane() -> int:
    """Wait until no cycle runs, no `score_cycle` job waits and no tick runs; the newest cycle's id."""

    def done(row: list[str]) -> tuple[bool, str]:
        running, waiting, _ = ints(row)
        return running == 0 and waiting == 0, f"running={running} waiting={waiting}"

    row = poll_row(LANE_STATE, 3, done, within_s=QUIET_WITHIN_S, what="a quiet score lane")
    return int(row[2])


def away_from_the_tick() -> None:
    """Sleep until a second of the minute in `AWAY_FROM_THE_TICK`."""
    while datetime.now(UTC).second not in AWAY_FROM_THE_TICK:
        time.sleep(0.5)


def quiet_moment() -> int:
    """A moment, at a second in `AWAY_FROM_THE_TICK`, when no cycle runs, no `score_cycle` job waits and no
    tick runs: the newest cycle's id then. The lane is read again after each wait for the window, since the
    tick at the top of the minute may have started a cycle meanwhile (its settlements would take a refill made
    under it)."""
    while True:
        after = await_quiet_lane()
        if datetime.now(UTC).second in AWAY_FROM_THE_TICK:
            return after
        away_from_the_tick()


def advance_bucket() -> None:
    if query_row(ADVANCE_BUCKET, 1) != ["1"]:
        raise CheckFailed("the month has no budget row")


def await_cycle(after: int, trigger: str, *, within_s: int) -> tuple[int, int, int]:
    """The first `trigger` cycle after cycle `after`, once `completed`: its id, its admission size and its
    targets published; a cycle that ends otherwise fails the check."""

    def done(row: list[str]) -> tuple[bool, str]:
        if row[0] == "0":
            return False, f"no {trigger} cycle yet"
        if row[1] not in ("running", "completed"):
            raise CheckFailed(f"cycle {row[0]} ended {row[1]!r}")
        return row[1] == "completed", f"cycle {row[0]} {row[1]}"

    sql = CYCLE_AFTER.format(after=int(after), trigger=trigger)
    cycle_id, _, size, published = poll_row(
        sql, 4, done, within_s=within_s, what=f"a completed {trigger} cycle"
    )
    return int(cycle_id), int(size), int(published)


def check_targets_created(ctx: Context) -> str | None:
    if not all(query_row(SEED_INPUTS, 4)):
        raise CheckFailed("M1's inputs were not written")
    if ints(query_row(SEED_BOARDS, 3)) != [len(BOARDS), len(BOARDS), 1]:
        raise CheckFailed("the boards were not written")
    flags = worker_flags()  # a paid run on a worker without the fixtures overlay would reach the providers
    if not all(flags.values()):
        raise CheckFailed(f"the worker is not on both fakes ({flags}): no run was posted")
    run_id = run_now(ctx, include_paid=True, key=PAID_RUN_KEY)
    took = wait_succeeded(ctx, run_id, within_s=COLLECT_WITHIN_S)
    tuple_id, scope_id = active()

    def done(row: list[str]) -> tuple[bool, str]:
        status = row[0]
        retained, covered, unset = ints(row[1:])
        found = (
            f"create_targets_for_tuple={status} retained={retained} targets={covered} similarity_null={unset}"
        )
        if status == "failed":
            raise CheckFailed(found)
        return status == "succeeded" and retained > 0 and covered == retained and unset == 0, found

    row = poll_row(
        TARGETS_CREATED.format(tuple=tuple_id), 4, done, within_s=TARGETS_WITHIN_S, what="every target"
    )
    STATE.tuple_id, STATE.scope_id = tuple_id, scope_id
    return f"run {run_id} succeeded in {took:.0f} s; tuple {tuple_id}: {done(row)[1]}"


def check_score_cycle(ctx: Context) -> str | None:
    if STATE.tuple_id is None or STATE.scope_id is None:
        raise CheckFailed("no active tuple: check targets_created did not pass")
    after = quiet_moment()
    advance_bucket()  # the month row's `bucket_at` a month back: M0's `UPDATE_SLOT_DUE` precedent (T28-f)
    (pending,) = ints(query_row(PENDING_OF.format(tuple=STATE.tuple_id), 1))
    response = ctx.client.post(f"{ORIGIN}/api/system/score-cycle", headers=mutating(ctx, SCORE_CYCLE_KEY))
    body = json_object(response, 202)
    if pick(body, "queued") is not True:
        raise CheckFailed(f"expected queued = true, got {pick(body, 'queued')!r} ({pick(body, 'reason')!r})")
    cycle_id, size, published = await_cycle(after, "manual", within_s=CYCLE_WITHIN_S)
    found = f"cycle {cycle_id} completed: S={size} pending={pending} published={published}"
    if published < min(size, pending) or size < 1:
        raise CheckFailed(f"expected at least min(S, pending) published; {found}")
    total, missing = ints(query_row(INSTANCES_OF.format(tuple=STATE.tuple_id, scope=STATE.scope_id), 2))
    if missing:
        raise CheckFailed(f"{missing} of {total} published results have no current det_results row")
    spend = json_object(ctx.client.get(f"{ORIGIN}/api/system/spend", headers=ctx.auth()), 200)
    groups = pick(spend, "by_model_effort_role")
    if not isinstance(groups, list):
        raise CheckFailed("the spend has no by_model_effort_role list")
    scoring = sum(
        (usd(group, "counted") for group in groups if pick(group, "role") in SCORING_ROLES), Decimal(0)
    )
    keys = {key: key in spend for key in ("c_hat", "critic_rate_7d")}
    if scoring <= 0 or not all(keys.values()):
        raise CheckFailed(f"expected scoring spend above $0 with both keys; scoring=${scoring} keys={keys}")
    row = query_row(PUBLISHED_JOB.format(tuple=STATE.tuple_id), 2)
    STATE.published_job_id = int(row[0])
    return (
        f"{found}; {total} published results with a current instance; scoring=${scoring} "
        f"c_hat=${pick(spend, 'c_hat')} ({pick(spend, 'c_hat_source')}) "
        f"critic_rate_7d={pick(spend, 'critic_rate_7d')!r}"
    )


def check_score_now(ctx: Context) -> str | None:
    if STATE.tuple_id is None:
        raise CheckFailed("no active tuple: check targets_created did not pass")
    job_id, target_id = ints(query_row(PENDING_JOB.format(tuple=STATE.tuple_id), 2))
    response = ctx.client.post(
        f"{ORIGIN}/api/jobs/{job_id}/score-now", json={}, headers=mutating(ctx, SCORE_NOW_KEY)
    )
    body = json_object(response, 202)
    if pick(body, "target_id") != target_id:
        raise CheckFailed(f"expected target {target_id}, got {pick(body, 'target_id')!r}")

    def done(row: list[str]) -> tuple[bool, str]:
        state, override = row
        if FAILED_TARGET.fullmatch(state):
            raise CheckFailed(f"target {target_id} ended {state!r}")
        return state in ("published_accepted", "published_contested"), f"state={state} override={override}"

    row = poll_row(
        TARGET_STATE.format(target=target_id), 2, done, within_s=SCORE_NOW_WITHIN_S, what="the publication"
    )
    if row[1] != "score_now":
        raise CheckFailed(f"expected override score_now, got {row[1]!r}")
    return f"job {job_id}: target {target_id} {row[0]} with override {row[1]}"


def check_feedback(ctx: Context) -> str | None:
    if STATE.published_job_id is None:
        raise CheckFailed("no published job: check score_cycle did not pass")
    job_id = STATE.published_job_id
    response = ctx.client.post(f"{ORIGIN}/api/jobs/{job_id}/feedback", json=FEEDBACK, headers=mutating(ctx))
    body = json_object(response, 201)
    case_id = pick(body, "golden_case_id")
    if not isinstance(case_id, int):
        raise CheckFailed(f"expected a golden case id, got {type(case_id).__name__}")
    listed = json_object(
        ctx.client.get(f"{ORIGIN}/api/golden-cases", params={"limit": 100}, headers=ctx.auth()), 200
    )
    items = pick(listed, "items")
    found = [item for item in items if pick(item, "id") == case_id] if isinstance(items, list) else []
    if len(found) != 1 or pick(found[0], "job_id") != job_id:
        raise CheckFailed(f"golden case {case_id} of job {job_id} is not listed")
    case = found[0]
    return (
        f"job {job_id}: feedback {pick(body, 'feedback_id')!r}, golden case {case_id} listed "
        f"(label={pick(case, 'label')!r} source={pick(case, 'source')!r} active={pick(case, 'active')!r})"
    )


def check_scope_apply(ctx: Context) -> str | None:
    if STATE.tuple_id is None or STATE.scope_id is None:
        raise CheckFailed("no active tuple: check targets_created did not pass")
    settings = json_object(ctx.client.get(f"{ORIGIN}/api/settings", headers=ctx.auth()), 200)
    payload, base = copy.deepcopy(pick(settings, "payload")), pick(settings, "version", "id")
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("scope"), dict)
        or not isinstance(base, int)
    ):
        raise CheckFailed("the active settings have no scope or version id")
    payload["scope"]["locations"] = [{**SCOPE_LOCATION, "radius_km": SCOPE_RADIUS_KM}]
    preview = json_object(
        ctx.client.post(f"{ORIGIN}/api/settings/dry-run", json={"payload": payload}, headers=mutating(ctx)),
        200,
    )
    errors, warnings = pick(preview, "errors"), pick(preview, "warnings")
    if not isinstance(errors, list) or not isinstance(warnings, list):
        raise CheckFailed("the dry-run answered no errors and warnings lists")
    if errors:
        raise CheckFailed(f"the dry-run refused the scope: {sorted({str(pick(e, 'code')) for e in errors})}")
    codes = [str(pick(w, "code")) for w in warnings]
    saved = json_object(
        ctx.client.post(
            f"{ORIGIN}/api/settings",
            json={"payload": payload, "base_version_id": base, "acknowledged_warnings": codes},
            headers=mutating(ctx, SCOPE_KEY),
        ),
        201,
    )
    operation = pick(saved, "apply_scope_operation_id")
    if not isinstance(operation, int):
        raise CheckFailed(f"the save started no scope apply ({type(operation).__name__})")
    old_scope = STATE.scope_id

    def done(row: list[str]) -> tuple[bool, str]:
        status, scope, pending = row
        if status == "failed":
            raise CheckFailed(f"apply operation {operation} failed")
        flipped = status == "succeeded" and scope.isdigit() and int(scope) != old_scope and pending == "0"
        return flipped, f"apply={status} scope={scope} pending={pending}"

    row = poll_row(APPLY_STATE.format(operation=operation), 3, done, within_s=APPLY_WITHIN_S, what="the flip")
    new_scope = int(row[1])
    total, missing = ints(query_row(INSTANCES_OF.format(tuple=STATE.tuple_id, scope=new_scope), 2))
    if missing:
        raise CheckFailed(
            f"{missing} of {total} published results have no current instance of scope {new_scope}"
        )
    (filtered,) = ints(query_row(FILTERED_BY.format(tuple=STATE.tuple_id, scope=new_scope), 1))
    STATE.scope_id = new_scope
    return (
        f"apply operation {operation} succeeded: scope {old_scope} -> {new_scope}; {total} published results "
        f"with a current instance; {filtered} targets filtered out; warnings={codes}"
    )


def golden_seed() -> int:
    """`jobhunter golden seed-adversarial` in the running worker container: the cases it inserted."""
    process = subprocess.run(
        [*DC, "exec", "-T", "worker", *GOLDEN_SEED],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=DOCKER_TIMEOUT_S,
    )
    if process.returncode != 0:
        raise CheckFailed(f"golden seed-adversarial: exit {process.returncode}: {stderr_tail(process)}")
    seeded = SEEDED.search(process.stdout)
    if seeded is None:
        raise CheckFailed("golden seed-adversarial printed no case count")
    return int(seeded.group(1))


def second_profile(ctx: Context) -> int:
    """A second CV through the profile flow, its draft saved with `ADDED_TITLE`, confirmed: its version id."""
    response = ctx.client.post(
        f"{ORIGIN}/api/profile/cv",
        files={"file": (CV_FILENAME, synthetic_pdf(), "application/pdf")},
        headers=mutating(ctx),
    )
    draft_id = pick(json_object(response, 202), "draft_id")
    if not isinstance(draft_id, int):
        raise CheckFailed(f"expected a draft id, got {type(draft_id).__name__}")
    path = f"/api/profile/drafts/{draft_id}"
    poll(ctx, path, draft_reached("text_review"), within_s=TEXT_REVIEW_WITHIN_S, what="text_review")
    json_object(ctx.client.post(f"{ORIGIN}{path}/text-review", headers=mutating(ctx, APPROVE_KEY)), 202)
    body = poll(ctx, path, draft_reached("ready"), within_s=EXTRACT_WITHIN_S, what="ready")
    draft = copy.deepcopy(pick(body, "draft"))
    if not isinstance(draft, dict) or not isinstance(draft.get("target_titles"), list):
        raise CheckFailed(f"draft {draft_id} has no target titles")
    draft["target_titles"] = [*draft["target_titles"], ADDED_TITLE]
    json_object(ctx.client.put(f"{ORIGIN}{path}", json={"draft": draft}, headers=mutating(ctx)), 200)
    confirmed = json_object(
        ctx.client.post(
            f"{ORIGIN}{path}/confirm", json={"expected_rescore_ack": True}, headers=mutating(ctx, CONFIRM_KEY)
        ),
        201,
    )
    version_id = pick(confirmed, "profile_version_id")
    listed = json_object(ctx.client.get(f"{ORIGIN}/api/profile", headers=ctx.auth()), 200)
    if not isinstance(version_id, int) or pick(listed, "active", "id") != version_id:
        raise CheckFailed(f"draft {draft_id}'s version is not the active one")
    if pick(listed, "active", "version_no") != 2:
        raise CheckFailed(f"expected version_no 2, got {pick(listed, 'active', 'version_no')!r}")
    return version_id


def check_batch_path(ctx: Context) -> str | None:
    if STATE.tuple_id is None:
        raise CheckFailed("no active tuple: check targets_created did not pass")
    cases = golden_seed()
    if cases != GOLDEN_CASES:
        raise CheckFailed(f"expected {GOLDEN_CASES} adversarial cases, got {cases}")
    old_tuple = STATE.tuple_id
    (old_bulk, _) = query_row(BULK_OF.format(tuple=old_tuple), 2)
    version_id = second_profile(ctx)
    tuple_id, _ = active()
    if tuple_id == old_tuple:
        raise CheckFailed(f"the second confirm left tuple {tuple_id} active")
    bulk_id, bulk_state = query_row(BULK_OF.format(tuple=tuple_id), 2)
    if not bulk_id.isdigit() or bulk_state != "active":
        raise CheckFailed(f"tuple {tuple_id} has no active bulk operation ({bulk_state!r})")
    old_state, carried = query_row(OLD_BULK.format(old=int(old_bulk)), 2)
    if not (old_state == "cancelled" or (old_state == "completed" and carried == "0")):
        raise CheckFailed(
            f"the first bulk operation {old_bulk} is {old_state!r} with {carried} targets, not ended by the "
            "confirm"
        )

    def created(row: list[str]) -> tuple[bool, str]:
        status = row[0]
        targets, of_bulk, uncovered = ints(row[1:])
        found = f"create_targets_for_tuple={status} targets={targets} of_bulk={of_bulk} uncovered={uncovered}"
        if status == "failed":
            raise CheckFailed(found)
        return status == "succeeded" and targets == of_bulk > 0 and uncovered == 0, found

    row = poll_row(
        BULK_TARGETS.format(tuple=tuple_id, bulk=int(bulk_id)),
        4,
        created,
        within_s=TARGETS_WITHIN_S,
        what="the bulk operation's targets",
    )
    targets = created(row)[1]

    def ended(row: list[str]) -> tuple[bool, str]:
        status, skipped, submissions = row
        found = f"batch_submit={status} skipped={skipped} submissions={submissions}"
        return status in ("succeeded", "failed"), found

    status, skipped, submissions = poll_row(
        BATCH_SUBMIT_OF.format(bulk=int(bulk_id)), 3, ended, within_s=SUBMIT_WITHIN_S, what="the batch_submit"
    )
    eligible, too_old = ints(query_row(ELIGIBLE_OF.format(bulk=int(bulk_id), tuple=tuple_id), 2))
    batch = (
        f"batch_submit={status} skipped={skipped} submissions={submissions}: eligible={eligible} "
        f"(batch_min_targets={BATCH_MIN_TARGETS}) expired_unscored_too_old={too_old}"
    )
    if (status, skipped, submissions) != ("succeeded", BELOW_MINIMUM, "0") or eligible >= BATCH_MIN_TARGETS:
        raise CheckFailed(f"expected the batch_submit skipped below_minimum; {batch}")
    STATE.tuple_id = tuple_id
    return (
        f"{cases} adversarial cases; version {version_id} confirmed as version_no 2: tuple {old_tuple} -> "
        f"{tuple_id}, bulk {old_bulk} {old_state} -> {bulk_id} active; {targets}; {batch}"
    )


def check_health(ctx: Context) -> str | None:
    deadline = time.monotonic() + HEALTH_WITHIN_S
    while True:
        health = json_object(ctx.client.get(f"{ORIGIN}/api/system/health", headers=ctx.auth()), 200)
        running, reason = pick(health, "scoring", "running_cycle_id"), pick(health, "scoring", "pause_reason")
        found = f"running_cycle_id={running!r} pause_reason={reason!r}"
        if running is None and reason is None:
            break
        if reason is not None or time.monotonic() >= deadline:
            raise CheckFailed(f"expected no running cycle and no pause; {found}")
        time.sleep(POLL_INTERVAL_S)
    clean, counted, scoring = query_row(COUNTERS_CLEAN, 3)
    if clean != "t":
        raise CheckFailed(f"the month's counters differ from the ledger (counted=${counted})")
    pending = pick(health, "scoring", "pending_targets")
    return (
        f"{found} pending_targets={pending!r}; the counters equal the ledger: counted=${counted} "
        f"scoring=${scoring}"
    )


CHECKS: Final[list[tuple[str, Callable[[Context], str | None]]]] = [
    *smoke_m2.CHECKS[:M2_CHECKS],
    ("targets_created", check_targets_created),
    ("score_cycle", check_score_cycle),
    ("score_now", check_score_now),
    ("feedback", check_feedback),
    ("scope_apply", check_scope_apply),
    ("batch_path", check_batch_path),
    ("health", check_health),
]


def main() -> int:
    # trust_env=False: the target is the loopback devproxy; no proxy variable of the calling shell applies.
    with httpx.Client(timeout=HTTP_TIMEOUT_S, trust_env=False) as client:
        ctx = Context(client)
        for name, check in CHECKS:
            try:
                detail = check(ctx)
            except CheckFailed as exc:
                emit(ctx, name, False, str(exc))
                return 1
            except subprocess.TimeoutExpired as exc:
                emit(ctx, name, False, f"{Path(str(exc.cmd[0])).name} timed out after {exc.timeout:g} s")
                return 1
            except (httpx.HTTPError, OSError, ValueError) as exc:  # transport, a missing docker, bad JSON
                emit(ctx, name, False, type(exc).__name__)
                return 1
            emit(ctx, name, True, detail)
    return 0


if __name__ == "__main__":
    sys.exit(main())
