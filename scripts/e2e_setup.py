"""The deterministic E2E setup (plan M4 Task 32, Codex r1 #7): the state the `scenarios` specs need, built on
the E2E stack after the `first-use` specs, every source answered by the fixtures and every Claude call by the
fake.

    docker compose --env-file deploy/images.lock -f deploy/compose.dev.yaml -f deploy/compose.fixtures.yaml \
        -f deploy/compose.fakellm.yaml -f deploy/compose.e2e.yaml up -d --build --wait
    uv run python scripts/e2e_setup.py

`frontend/e2e/setup/stack.setup.ts` runs it as the Playwright project `stack-setup`. The precondition is the
user `smoke` (`frontend/e2e/global-setup.ts` creates it). The smoke helpers are reused as they are
(`smoke_m0` to `smoke_m3`, and `m4_seeds` for the rows a collection does not make). Nine steps run in order;
each prints exactly one JSON line, `{"step": <name>, "ok": <bool>}` with an optional `"detail"`, and checks
its own result first, writing nothing when it is there already, so a second run prints nine `ok` lines and
writes nothing new:

1. `login`: `smoke_m0.check_login`;
2. `inputs`: `smoke_m1.SEED_INPUTS` (the query `software engineer` and the Greenhouse board) and
   `smoke_m3.SEED_BOARDS` (four more boards) with `psql` (the default settings hold no query, watchlist entry
   or location);
3. `settings`: one save through the API as `smoke_m2.check_settings_save` makes one (the dry-run, then the
   save with every warning code acknowledged and a fresh key): `scope.locations` Berlin
   (`smoke_m3.SCOPE_LOCATION`) at `E2E_RADIUS_KM` (E7, E13) and `scoring.batch_bulk_rescore = false` (the
   first confirm re-scores online; E17 turns it on); then its scope apply polled to the flip (120 s);
4. `collection`: the worker on both fakes (`smoke_m2.worker_flags`), then a paid run (`smoke_m1.run_now`)
   that succeeds within 600 s (`wait_succeeded`): JSearch, Adzuna, Arbeitnow and the five boards;
5. `merge` (E9): `m4_seeds.seed_merge_pair`, then `POST /api/duplicate-groups/{id}/confirm`; nothing when a
   user-confirmed merge stands;
6. `profile`: `smoke_m2`'s CV through the profile flow (`check_cv_upload`, `check_text_review`,
   `check_extract_ready`, `check_confirm`: profile v1 with `expected_rescore_ack`), then
   `smoke_m3.TARGETS_CREATED` polled for the tuple (120 s): its `create_targets_for_tuple` `succeeded` with a
   target for every retained current revision. Between the extraction and the confirm, every grounded
   candidate (below) is given its grounded answers (`m4_seeds.grounded_answers` with the draft's first skill
   name, which the confirm makes the active profile's), so that no score cycle can claim one before its
   answers are in place (**Why the answers come before the confirm**);
7. `grounded` (E9): the lowest job id among the grounded candidates, the Greenhouse `getyourguide` postings
   with `completeness = 'full'` and three sentences of 30 to 160 code points in the description
   (`m4_seeds.grounded_quotes`), whose active-tuple target is `pending`, being scored, or published with the
   grounded quotes as its description evidence (a cycle scored it on its answers): its answers in the worker's
   keyed directory (`m4_seeds.write_grounded_answers`, which writes only what is missing), then, for a
   `pending` one, `POST /api/jobs/{id}/score-now` (a fresh key), and its publish awaited (180 s);
8. `rejected_item` (E17): `m4_seeds.seed_rejected_item`;
9. `verify`: every prerequisite of the scenarios, each by name (`VERIFIED`), failing with the name and what
   it found. It first waits for the score lane to settle (`SETTLE_WITHIN_S`): the first periodic cycle
   completed and no cycle running or waiting.

**Why the answers come before the confirm, and why the lane settles.** The confirm opens the month's budget
row with the bucket full, so the tick at the top of the next minute starts a periodic cycle (some 30 targets:
the watchlist's jobs through always-include, the `getyourguide` candidates among them, and the oldest
targets, the re-ingest pair's representative among them, `scoring.admission`); a bucket it overspends starts
no other for days (`smoke_m3`, **The bucket**). A candidate that this cycle claimed before its answers were
written would be scored on the default recordings; with the answers in place first, whichever cycle scores
it scores it grounded. And the pair's representative is `pending` before that cycle and published after it,
so step 9 waits for the cycle and accepts either state for it.

It stops at the first failed step (exit 1); after nine `ok` lines it exits 0. It prints nothing but those
lines: never the password, the session cookie or its token (`Context.scrub`), an `Idempotency-Key`, a CV
text or an ad's text. A `detail` holds ids, states, counts and codes.
"""

import copy
import json
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

import httpx
import m4_seeds  # scripts/ is the script's directory, the first entry of sys.path
import smoke_m1
import smoke_m2
import smoke_m3
from jobhunter.normalize.titles import normalize_title
from smoke_m0 import HTTP_TIMEOUT_S, ORIGIN, POLL_INTERVAL_S, CheckFailed, Context, check_login, pick

E2E_RADIUS_KM: Final = 30
COLLECT_WITHIN_S: Final = 600
APPLY_WITHIN_S: Final = 120
TARGETS_WITHIN_S: Final = 120
PUBLISH_WITHIN_S: Final = 180
GROUNDED_BOARD: Final = m4_seeds.GROUNDED_BOARD
# E10: the fixtures' Adzuna re-ingest pair (`adzuna_reingest_pair`, tests/contract/README.md): one ad under
# two ids, which every fresh collection groups (tests/integration/test_collect_end_to_end.py).
REINGEST_IDS: Final = ("5933514068", "5933514069")
# E7: an Adzuna job without a work-mode signal, and an Ashby n8n job with a structured remote mode. The ad's
# title is `Java-Entwickler (m/w/d)`; a job (and its card) holds it as the title normaliser leaves it, the
# gender tags stripped (`normalize.titles`, spec §8.2): `Java-Entwickler`.
E7_ADZUNA_AD_TITLE: Final = "Java-Entwickler (m/w/d)"
E7_ADZUNA_TITLE: Final = normalize_title(E7_ADZUNA_AD_TITLE).title
E7_ASHBY_BOARD: Final = ("ashby", "n8n")
SETTLE_WITHIN_S: Final = 600
PUBLISHED: Final = ("published_accepted", "published_contested")
CLAIMED: Final = "conflict_state"  # score-now's 409 for a target a cycle is scoring
ENDED_UNPUBLISHED: Final = ("refused", "cancelled", "expired_unscored")

_BOARD_SLUGS: Final = ", ".join(f"'{slug}'" for _, slug, _ in smoke_m3.BOARDS)
INPUTS_PRESENT: Final = (
    "/* e2e.inputs_present */ SELECT "  # noqa: S608 (constants only)
    f"(SELECT count(*) FROM queries WHERE text = '{smoke_m1.QUERY_TEXT}'), "
    f"(SELECT count(*) FROM watchlist_companies WHERE board_slug = '{smoke_m1.BOARD_SLUG}'), "
    f"(SELECT count(*) FROM watchlist_companies WHERE board_slug IN ({_BOARD_SLUGS}))"
)
PENDING_SCOPE: Final = (
    "/* e2e.pending_scope */ SELECT coalesce(pending_scope_version_id, 0) FROM active_config WHERE id = 1"
)
PAID_RUN_DONE: Final = (
    "/* e2e.paid_run_done */ SELECT coalesce(max(id), 0) FROM runs "
    "WHERE include_paid AND status = 'succeeded'"
)
JOB_COUNT: Final = "/* e2e.job_count */ SELECT count(*) FROM jobs"
# The newest scope apply and the active scope (step 3, while a saved scope is pending).
PENDING_APPLY: Final = (
    "/* e2e.pending_apply */ SELECT coalesce((SELECT max(id) FROM operations "
    "WHERE kind = 'apply_scope'), 0), scope_version_id FROM active_config WHERE id = 1"
)
# The grounded job's candidates (step 7, `m4_seeds.GROUNDED_JOB_SQL`) with their active-tuple targets, as one
# JSON array (their descriptions included: never printed). Step 6 reads them before any target exists through
# `m4_seeds.GROUNDED_JOBS`.
GROUNDED_CANDIDATES: Final = f"""\
/* e2e.grounded_candidates */ SELECT coalesce(json_agg(json_build_object('job_id', j.id,
  'state', t.state, 'result_id', t.published_result_id, 'description', r.description_text)
  ORDER BY j.id), '[]')::text
FROM jobs j JOIN job_revisions r ON r.id = j.current_revision_id
JOIN active_config a ON a.id = 1
JOIN score_targets t ON t.job_revision_id = j.current_revision_id AND t.llm_tuple_id = a.llm_tuple_id
WHERE {m4_seeds.GROUNDED_JOB_SQL}"""  # noqa: S608
# The completed periodic cycles (step 9 waits for the first).
PERIODIC_DONE: Final = (
    "/* e2e.periodic_done */ SELECT count(*) FROM score_cycles "
    "WHERE trigger = 'periodic' AND status = 'completed'"
)
# `{result}` is an integer the database answered: the result's description quotes, distinct.
DESCRIPTION_QUOTES: Final = (
    "/* e2e.description_quotes */ SELECT coalesce(json_agg(DISTINCT quote), '[]')::text "
    "FROM result_evidence WHERE llm_result_id = {result} AND source_kind = 'description'"
)
TARGET_OF_JOB: Final = (
    "/* e2e.target_of_job */ SELECT t.state, t.override FROM jobs j JOIN active_config a ON a.id = 1 "
    "JOIN score_targets t ON t.job_revision_id = j.current_revision_id AND t.llm_tuple_id = a.llm_tuple_id "
    "WHERE j.id = {job}"
)
RETAINED_COUNT: Final = f"/* e2e.retained */ SELECT count(*) FROM jobs j WHERE {smoke_m3.RETAINED_JOB_SQL}"  # noqa: S608
_REINGEST_IN: Final = ", ".join(f"'{native}'" for native in REINGEST_IDS)
# E10's pair: each posting with its job, group, structured sets, active-tuple target and active-scope filter.
REINGEST_PAIR: Final = f"""\
/* e2e.reingest_pair */ SELECT coalesce(json_agg(json_build_object('native_id', p.native_id,
  'job_id', j.id, 'group_id', j.duplicate_group_id, 'group_status', g.status,
  'representative_job_id', g.representative_job_id, 'seniority_tokens', j.seniority_tokens,
  'work_modes', CASE WHEN j.work_mode_source = 'structured' THEN j.work_mode_possible ELSE '{{}}' END,
  'employment_types', CASE WHEN j.employment_source = 'structured' THEN j.employment_types ELSE '{{}}' END,
  'target_state', t.state, 'deferred_group_id', t.deferred_group_id,
  'filter_passed', h.passed, 'filter_reasons', h.reasons) ORDER BY p.native_id), '[]')::text
FROM postings p JOIN source_instances s ON s.id = p.source_instance_id AND s.source_id = 'adzuna'
JOIN jobs j ON j.id = p.job_id JOIN active_config a ON a.id = 1
LEFT JOIN duplicate_groups g ON g.id = j.duplicate_group_id
LEFT JOIN score_targets t ON t.job_revision_id = j.current_revision_id AND t.llm_tuple_id = a.llm_tuple_id
LEFT JOIN hard_filter_results h ON h.job_id = j.id AND h.scope_version_id = a.scope_version_id
WHERE p.native_id IN ({_REINGEST_IN})"""  # noqa: S608 (constants only)
E7_JOBS: Final = f"""\
/* e2e.e7_jobs */ SELECT
  (SELECT count(*) FROM jobs j WHERE {smoke_m3.RETAINED_JOB_SQL} AND j.availability = 'active'
    AND j.title = '{E7_ADZUNA_TITLE}' AND j.work_mode_source = 'none' AND EXISTS (SELECT 1 FROM postings p
    JOIN source_instances s ON s.id = p.source_instance_id WHERE p.job_id = j.id AND s.source_id = 'adzuna')),
  (SELECT count(*) FROM jobs j WHERE {smoke_m3.RETAINED_JOB_SQL} AND j.availability = 'active'
    AND j.work_mode_source = 'structured' AND 'remote' = ANY(j.work_mode_possible) AND EXISTS (SELECT 1
    FROM postings p JOIN source_instances s ON s.id = p.source_instance_id WHERE p.job_id = j.id
    AND s.source_id = '{E7_ASHBY_BOARD[0]}' AND s.instance_key = '{E7_ASHBY_BOARD[1]}'))"""  # noqa: S608


class State:
    """What the steps hand on: the grounded job (step 7) and the seeded rejected response (step 8)."""

    grounded_job_id: int | None = None
    rejected_response_id: int | None = None


STATE: Final = State()


def fresh_key() -> str:
    """A new `Idempotency-Key`: never printed."""
    return f"e2e-setup-{uuid.uuid4().hex}"


def emit(ctx: Context, step: str, ok: bool, detail: str | None = None) -> None:
    line: dict[str, Any] = {"step": step, "ok": ok}
    if detail is not None:
        line["detail"] = ctx.scrub(detail)
    print(json.dumps(line), flush=True)


def sql(statement: str) -> str:
    return m4_seeds.stack_sql(statement)


def row(statement: str, columns: int, *, what: str) -> list[str]:
    found = m4_seeds.one_row(sql(statement), columns, what=what)
    if found is None:
        raise CheckFailed(f"{what}: no row")
    return found


def number(statement: str, *, what: str) -> int:
    (value,) = m4_seeds.ints(row(statement, 1, what=what), what=what)
    return value


def get_json(ctx: Context, path: str) -> dict[str, Any]:
    return smoke_m2.json_object(ctx.client.get(f"{ORIGIN}{path}", headers=ctx.auth()), 200)


def wait_rows(
    statement: str, columns: int, done: Callable[[list[str]], tuple[bool, str]], *, within_s: int, what: str
) -> list[str]:
    """`statement`'s one row, read until `done(row)` says so or `within_s` seconds have passed."""
    deadline = time.monotonic() + within_s
    while True:
        found = row(statement, columns, what=what)
        finished, last = done(found)
        if finished:
            return found
        if time.monotonic() >= deadline:
            raise CheckFailed(f"{what} not reached within {within_s} s (last: {last})")
        time.sleep(POLL_INTERVAL_S)


# ---- the steps --------------------------------------------------------------------------------------------


def step_login(ctx: Context) -> str | None:
    return check_login(ctx)


def step_inputs(ctx: Context) -> str | None:
    queries, greenhouse, boards = m4_seeds.ints(row(INPUTS_PRESENT, 3, what="the inputs"), what="the inputs")
    written = []
    if (queries, greenhouse) == (0, 0):
        if not all(row(smoke_m1.SEED_INPUTS, 4, what="M1's inputs")):
            raise CheckFailed("M1's inputs were not written")
        written.append("the query and the Greenhouse board")
    elif (queries, greenhouse) != (1, 1):
        raise CheckFailed(f"M1's inputs are there in part: queries={queries} greenhouse={greenhouse}")
    if boards == 0:
        made = m4_seeds.ints(row(smoke_m3.SEED_BOARDS, 3, what="the boards"), what="the boards")
        if made != [len(smoke_m3.BOARDS), len(smoke_m3.BOARDS), 1]:
            raise CheckFailed(f"the boards were not written: {made}")
        written.append(f"{len(smoke_m3.BOARDS)} more boards")
    elif boards != len(smoke_m3.BOARDS):
        raise CheckFailed(f"the boards are there in part: {boards} of {len(smoke_m3.BOARDS)}")
    return f"written: {', '.join(written)}" if written else "already there"


def wanted_location() -> dict[str, Any]:
    return {**smoke_m3.SCOPE_LOCATION, "radius_km": E2E_RADIUS_KM}


def scope_is_wanted(payload: Any) -> bool:
    """Whether the payload's scope holds exactly Berlin at `E2E_RADIUS_KM`."""
    locations = pick(payload, "scope", "locations")
    return (
        isinstance(locations, list)
        and len(locations) == 1
        and pick(locations[0], "gazetteer_id") == smoke_m3.SCOPE_LOCATION["gazetteer_id"]
        and pick(locations[0], "radius_km") == E2E_RADIUS_KM
    )


def await_flip(operation: int, old_scope: int) -> str:
    def done(found: list[str]) -> tuple[bool, str]:
        status, scope, pending = found
        if status == "failed":
            raise CheckFailed(f"apply operation {operation} failed")
        flipped = status == "succeeded" and scope.isdigit() and int(scope) != old_scope and pending == "0"
        return flipped, f"apply={status} scope={scope} pending={pending}"

    found = wait_rows(
        smoke_m3.APPLY_STATE.format(operation=int(operation)),
        3,
        done,
        within_s=APPLY_WITHIN_S,
        what="the flip",
    )
    return f"apply operation {operation} succeeded: scope {old_scope} -> {found[1]}"


def step_settings(ctx: Context) -> str | None:
    active = get_json(ctx, "/api/settings")
    payload, base = copy.deepcopy(pick(active, "payload")), pick(active, "version", "id")
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("scoring"), dict)
        or not isinstance(base, int)
    ):
        raise CheckFailed("the active settings have no scoring group or version id")
    pending = number(PENDING_SCOPE, what="the pending scope")
    saved_already = f"already saved: version_no {pick(active, 'version', 'version_no')!r}"
    bulk_off = payload["scoring"].get("batch_bulk_rescore") is False
    if scope_is_wanted(payload) and pending:  # an earlier save's apply in flight: its flip, not a new save
        operation, active_scope = m4_seeds.ints(
            row(PENDING_APPLY, 2, what="the pending apply"), what="the apply"
        )
        if not operation:
            raise CheckFailed(f"scope {pending} is pending without a scope apply")
        flipped = await_flip(operation, active_scope)
        if bulk_off:
            return f"{saved_already}; the pending {flipped}"
        pending = 0
    if scope_is_wanted(payload) and bulk_off and not pending:
        return saved_already
    (old_scope,) = m4_seeds.ints(
        row(smoke_m3.ACTIVE, 2, what="the active tuple")[1:], what="the active scope"
    )
    payload["scope"]["locations"] = [wanted_location()]
    payload["scoring"]["batch_bulk_rescore"] = False
    saved, codes = smoke_m2.save_settings(ctx, payload, base, fresh_key(), what="the E2E scope")
    version_no, operation = pick(saved, "version", "version_no"), pick(saved, "apply_scope_operation_id")
    applied = await_flip(operation, old_scope) if isinstance(operation, int) else "no scope apply"
    return f"saved as version_no {version_no!r} (warnings={codes}); {applied}"


def step_collection(ctx: Context) -> str | None:
    flags = smoke_m2.worker_flags()  # a paid run on a worker without both fakes would reach the providers
    shown = " ".join(f"{name.removeprefix('JOBHUNTER_').lower()}={flag}" for name, flag in flags.items())
    if not all(flags.values()):
        raise CheckFailed(f"the worker is not on both fakes ({shown}): no run was posted")
    done = number(PAID_RUN_DONE, what="the paid runs")
    if done:
        return f"{shown}; paid run {done} succeeded already: {number(JOB_COUNT, what='the jobs')} jobs"
    run_id = smoke_m1.run_now(ctx, include_paid=True, key=fresh_key())
    took = smoke_m1.wait_succeeded(ctx, run_id, within_s=COLLECT_WITHIN_S)
    return f"{shown}; paid run {run_id} succeeded in {took:.0f} s: {number(JOB_COUNT, what='the jobs')} jobs"


def step_merge(ctx: Context) -> str | None:
    seeded = m4_seeds.seed_merge_pair()
    if seeded is None:
        return "a user-confirmed merge stands: nothing seeded"
    group, representative, member = seeded
    response = ctx.client.post(
        f"{ORIGIN}/api/duplicate-groups/{group}/confirm", headers=smoke_m2.mutating(ctx)
    )
    into = pick(smoke_m2.json_object(response, 202), "job_id")
    if into != representative:
        raise CheckFailed(
            f"group {group} was merged into job {into!r}, not its representative {representative}"
        )
    if number(m4_seeds.MERGE_DONE, what="the merge events") < 1:
        raise CheckFailed(f"group {group} was confirmed but no user-confirmed merge stands")
    return f"group {group}: job {member} merged into job {representative}"


def await_targets(tuple_id: int) -> str:
    def done(found: list[str]) -> tuple[bool, str]:
        status = found[0]
        retained, covered, unset = m4_seeds.ints(found[1:], what="the targets")
        shown = (
            f"create_targets_for_tuple={status} retained={retained} targets={covered} similarity_null={unset}"
        )
        if status == "failed":
            raise CheckFailed(shown)
        return status == "succeeded" and retained > 0 and covered == retained, shown

    found = wait_rows(
        smoke_m3.TARGETS_CREATED.format(tuple=int(tuple_id)),
        4,
        done,
        within_s=TARGETS_WITHIN_S,
        what="the targets",
    )
    return done(found)[1]


def ground_candidates(ctx: Context) -> str:
    """Every grounded candidate given its grounded answers before the confirm (module docstring), with the
    ready draft's first skill name: what the detail says of it."""
    draft = get_json(ctx, f"/api/profile/drafts/{smoke_m2.STATE.draft_id}")
    skills = pick(draft, "draft", "skills")
    skill = pick(skills[0], "name") if isinstance(skills, list) and skills else None
    if not isinstance(skill, str) or not skill:
        raise CheckFailed(f"draft {smoke_m2.STATE.draft_id} has no skill")
    jobs = m4_seeds.json_line(sql(m4_seeds.GROUNDED_JOBS), what="the grounded candidates")
    answers: dict[str, bytes] = {}
    grounded = 0
    for job in jobs if isinstance(jobs, list) else []:
        description = pick(job, "description")
        if quotes_of(description) is not None:
            answers |= m4_seeds.grounded_answers(int(job["revision_id"]), description, skill)
            grounded += 1
    if not grounded:
        raise CheckFailed(
            f"no grounded candidate among {len(jobs)} {GROUNDED_BOARD[1]} jobs of completeness full"
        )
    written = m4_seeds.write_keyed_answers(answers)
    return f"{grounded} grounded candidates answered ({written} files)"


def step_profile(ctx: Context) -> str | None:
    active = pick(get_json(ctx, "/api/profile"), "active")
    made = "already confirmed"
    if active is None:
        for check in (smoke_m2.check_cv_upload, smoke_m2.check_text_review, smoke_m2.check_extract_ready):
            check(ctx)
        answered = ground_candidates(ctx)
        smoke_m2.check_confirm(ctx)
        made = f"version {smoke_m2.STATE.version_id} confirmed after {answered}"
    version_no = pick(get_json(ctx, "/api/profile"), "active", "version_no")
    tuple_id, _ = smoke_m3.active()
    return f"profile version_no {version_no!r} {made}; tuple {tuple_id}: {await_targets(tuple_id)}"


def described_quotes(result_id: int) -> list[str]:
    found = m4_seeds.json_line(sql(DESCRIPTION_QUOTES.format(result=int(result_id))), what="the evidence")
    return [str(quote) for quote in found] if isinstance(found, list) else []


def quotes_of(description: Any) -> list[str] | None:
    try:
        return m4_seeds.grounded_quotes(description) if isinstance(description, str) else None
    except ValueError:
        return None


def grounded_published(candidate: Any) -> bool:
    """Whether the candidate is published with the grounded quotes as its description evidence."""
    quotes, result = quotes_of(pick(candidate, "description")), pick(candidate, "result_id")
    if pick(candidate, "state") not in PUBLISHED or not quotes or not isinstance(result, int):
        return False
    evidence = described_quotes(result)
    return bool(evidence) and set(evidence) <= set(quotes)


def await_publish(job_id: int) -> str:
    def done(found: list[str]) -> tuple[bool, str]:
        state, override = found
        if state in ENDED_UNPUBLISHED or state.startswith("failed_"):
            raise CheckFailed(f"job {job_id}'s target ended {state!r}")
        return state in PUBLISHED, f"state={state} override={override}"

    found = wait_rows(
        TARGET_OF_JOB.format(job=int(job_id)),
        2,
        done,
        within_s=PUBLISH_WITHIN_S,
        what=f"job {job_id}'s publish",
    )
    return f"{found[0]} (override {found[1]})"


def step_grounded(ctx: Context) -> str | None:
    candidates = m4_seeds.json_line(sql(GROUNDED_CANDIDATES), what="the grounded candidates")
    if not isinstance(candidates, list):
        raise CheckFailed("the grounded candidates are no list")
    described = [c for c in candidates if quotes_of(pick(c, "description")) is not None]
    chosen = next(
        (c for c in described if pick(c, "state") in ("pending", "in_progress") or grounded_published(c)),
        None,
    )
    if chosen is None:
        states = sorted({str(pick(c, "state")) for c in described})
        raise CheckFailed(
            f"no {GROUNDED_BOARD[0]} {GROUNDED_BOARD[1]} job of completeness full with three sentences of "
            f"30 to 160 code points has an active-tuple target pending, being scored or published grounded: "
            f"candidates={len(candidates)} with_three_sentences={len(described)} their_states={states}"
        )
    job_id = int(chosen["job_id"])
    STATE.grounded_job_id = job_id
    present = m4_seeds.grounded_files_present(job_id)
    if not present:
        m4_seeds.write_grounded_answers(job_id)
    files = "files present" if present else "files written"
    state = pick(chosen, "state")
    if state in PUBLISHED:
        return f"job {job_id} published with its grounded answers already; {files}; {handed_on(job_id)}"
    if state == "pending":
        response = ctx.client.post(
            f"{ORIGIN}/api/jobs/{job_id}/score-now", json={}, headers=smoke_m2.mutating(ctx, fresh_key())
        )
        code = problem_code(response) if response.status_code == 409 else None
        # 409 `conflict_state`: a cycle claimed it since, and scores it on the same answers
        if response.status_code != 202 and code != CLAIMED:
            raise CheckFailed(
                f"score-now of job {job_id}: expected 202, got {response.status_code} (code {code!r})"
            )
    published = await_publish(job_id)
    return f"job {job_id} ({state}): {files}; {published}; {handed_on(job_id)}"


def problem_code(response: httpx.Response) -> str | None:
    """The `code` of a problem answer, `None` when it has none."""
    try:
        code = pick(response.json(), "code")
    except ValueError:
        return None
    return code if isinstance(code, str) else None


def handed_on(job_id: int) -> str:
    """`m4_seeds.grounded_job_id`, the scenarios' way to E9's job, must find the job this step chose."""
    found = m4_seeds.grounded_job_id()
    if found != job_id:
        raise CheckFailed(f"the grounded job is {job_id}, but m4_seeds.grounded_job_id() finds {found}")
    return "m4_seeds.grounded_job_id() finds it"


def step_rejected_item(ctx: Context) -> str | None:
    response, item = m4_seeds.seed_rejected_item()
    STATE.rejected_response_id = response
    return f"raw response {response}, raw item {item}"


# ---- step 9 -----------------------------------------------------------------------------------------------


def grounded_problem(detail: Any) -> str | None:
    """What E9's grounded result lacks in `GET /api/jobs/{id}`'s answer, or `None` (module docstring)."""
    status = pick(detail, "score", "result_status")
    if status != "contested":
        return f"result_status={status!r}"
    text = pick(detail, "description", "text")
    criteria = pick(detail, "score", "criteria")
    if not isinstance(text, str) or not isinstance(criteria, list):
        return "no description or criteria"
    spanned = 0
    for criterion in criteria:
        evidence = pick(criterion, "evidence")
        if isinstance(evidence, list) and any(
            pick(e, "source_kind") == "description"
            and isinstance(pick(e, "span_start"), int)
            and isinstance(pick(e, "span_end"), int)
            and text[e["span_start"] : e["span_end"]] == pick(e, "quote")
            for e in evidence
        ):
            spanned += 1
    if spanned < m4_seeds.QUOTES:
        return f"{spanned} criteria with description spans that slice to their quotes, not {m4_seeds.QUOTES}"
    rounds = pick(detail, "contested", "rounds")
    if not isinstance(rounds, list) or len(rounds) != len(m4_seeds.CRITIC_ROUNDS):
        return f"critic rounds={len(rounds) if isinstance(rounds, list) else None!r}"
    first = pick(rounds[0], "triggers")
    if not isinstance(first, list) or "T1" not in first:
        return f"the first round's triggers={first!r}"
    drafts = pick(detail, "contested", "rejected_drafts")
    wanted = {
        "criterion": m4_seeds.DISPUTED_CRITERION,
        "from_level": m4_seeds.GENERATED_LEVEL,
        "to_level": m4_seeds.DISPUTED_LEVEL,
    }
    diffs = [d for draft in drafts or [] for d in pick(draft, "level_diff") or []]
    if wanted not in diffs:
        return f"no rejected draft with the level diff {wanted} ({len(drafts or [])} rejected drafts)"
    return None


def reingest_problem(pair: Any) -> str | None:
    """What E10's pair lacks (`REINGEST_PAIR`'s rows), or `None`: one active group of the two jobs, the
    member's target `deferred_duplicate` in it, the representative's `pending` or published (the first
    periodic cycle scores it: module docstring), both passing the active scope's hard filters. The failure
    names the predicate and the pair's ids, seniority tokens, structured sets, target states and filter
    reasons."""
    if not isinstance(pair, list):
        return "no rows"
    shown = "; ".join(
        f"{pick(r, 'native_id')}: job {pick(r, 'job_id')} group {pick(r, 'group_id')} "
        f"({pick(r, 'group_status')}, representative {pick(r, 'representative_job_id')}) "
        f"seniority={pick(r, 'seniority_tokens')} work_modes={pick(r, 'work_modes')} "
        f"employment={pick(r, 'employment_types')} target={pick(r, 'target_state')} "
        f"deferred_group={pick(r, 'deferred_group_id')} filter_passed={pick(r, 'filter_passed')} "
        f"reasons={pick(r, 'filter_reasons')}"
        for r in pair
    )
    jobs = {pick(r, "job_id") for r in pair}
    groups = {pick(r, "group_id") for r in pair}
    if len(pair) != len(REINGEST_IDS) or len(jobs) != len(REINGEST_IDS):
        return f"two postings of two jobs: {shown or 'none'}"
    (group,) = groups if len(groups) == 1 else (None,)
    if group is None or pick(pair[0], "group_status") != "active":
        return f"one active group: {shown}"
    representative = pick(pair[0], "representative_job_id")
    if representative not in jobs:
        return f"a representative of the pair: {shown}"
    for r in pair:
        if pick(r, "job_id") == representative:
            if pick(r, "target_state") not in ("pending", *PUBLISHED):
                return f"the representative's target pending or published: {shown}"
        elif (pick(r, "target_state"), pick(r, "deferred_group_id")) != ("deferred_duplicate", group):
            return f"the member's target deferred_duplicate in group {group}: {shown}"
        if pick(r, "filter_passed") is not True:
            return f"both pass the active scope's hard filters: {shown}"
    return None


def verify_settled(ctx: Context) -> str:
    """The score lane settled (module docstring): the first periodic cycle completed, no cycle running or
    waiting and no tick running."""
    deadline = time.monotonic() + SETTLE_WITHIN_S
    while True:
        periodic = number(PERIODIC_DONE, what="the periodic cycles")
        running, waiting, _ = m4_seeds.ints(
            row(smoke_m3.LANE_STATE, 3, what="the score lane"), what="the lane"
        )
        if periodic and not running and not waiting:
            return f"score lane settled ({periodic} periodic cycle(s) completed)"
        if time.monotonic() >= deadline:
            raise CheckFailed(
                f"score lane settled within {SETTLE_WITHIN_S} s: periodic_completed={periodic} "
                f"running={running} waiting={waiting}"
            )
        time.sleep(POLL_INTERVAL_S)


def verify_profile(ctx: Context) -> str:
    version_no = pick(get_json(ctx, "/api/profile"), "active", "version_no")
    if version_no != 1:
        raise CheckFailed(f"profile v1 active: version_no={version_no!r}")
    return "profile v1 active"


def verify_grounded(ctx: Context) -> str:
    job_id = STATE.grounded_job_id
    if job_id is None:
        raise CheckFailed("grounded result: no grounded job (step grounded did not pass)")
    problem = grounded_problem(get_json(ctx, f"/api/jobs/{job_id}"))
    if problem is not None:
        raise CheckFailed(f"grounded result of job {job_id}: {problem}")
    return f"job {job_id} contested with three critic rounds (T1) and description spans"


def verify_scope(ctx: Context) -> str:
    payload = pick(get_json(ctx, "/api/settings"), "payload")
    pending = number(PENDING_SCOPE, what="the pending scope")
    if not scope_is_wanted(payload) or pending:
        locations = pick(payload, "scope", "locations")
        found = [(pick(x, "gazetteer_id"), pick(x, "radius_km")) for x in locations or []]
        raise CheckFailed(
            f"scope Berlin at {E2E_RADIUS_KM} km, none pending: locations={found} pending={pending}"
        )
    return f"Berlin at {E2E_RADIUS_KM} km"


def verify_retained(ctx: Context) -> str:
    floor = pick(get_json(ctx, "/api/settings"), "payload", "scoring", "batch_min_targets")
    retained = number(RETAINED_COUNT, what="the retained revisions")
    if not isinstance(floor, int) or retained < floor:
        raise CheckFailed(f"retained current revisions at least batch_min_targets: {retained} < {floor!r}")
    return f"{retained} retained revisions (batch_min_targets {floor})"


def verify_merge(ctx: Context) -> str:
    merged = number(m4_seeds.MERGE_DONE, what="the merge events")
    if merged < 1:
        raise CheckFailed("the merge event: no unreversed user_confirmed_merge")
    return f"{merged} user-confirmed merge(s)"


def verify_reingest(ctx: Context) -> str:
    pair = m4_seeds.json_line(sql(REINGEST_PAIR), what="the re-ingest pair")
    problem = reingest_problem(pair)
    if problem is not None:
        raise CheckFailed(f"E10's group: {problem}")
    representative = next(r for r in pair if pick(r, "job_id") == pick(r, "representative_job_id"))
    return (
        f"re-ingest group {pick(pair[0], 'group_id')}: member deferred, representative "
        f"{pick(representative, 'target_state')}"
    )


def verify_rejected(ctx: Context) -> str:
    response_id = STATE.rejected_response_id
    items = pick(get_json(ctx, "/api/system/needs-attention"), "raw_items", "items")
    listed = [i for i in items or [] if pick(i, "raw_response_id") == response_id]
    if len(listed) != 1 or pick(listed[0], "replayable") is not True:
        shown = [(pick(i, "raw_response_id"), pick(i, "replayable")) for i in items or []]
        raise CheckFailed(f"the rejected item of response {response_id!r} replayable: listed={shown}")
    return f"response {response_id} replayable"


def verify_e7(ctx: Context) -> str:
    adzuna, ashby = m4_seeds.ints(row(E7_JOBS, 2, what="E7's jobs"), what="E7's jobs")
    if not adzuna or not ashby:
        raise CheckFailed(f"E7's jobs: adzuna_without_work_mode={adzuna} ashby_n8n_structured_remote={ashby}")
    return "E7's jobs listed"


def verify_sources(ctx: Context) -> str:
    enabled = pick(get_json(ctx, "/api/settings"), "payload", "sources", "enabled")
    if not isinstance(enabled, dict) or not enabled or not all(value is True for value in enabled.values()):
        raise CheckFailed(f"every source enabled: {enabled!r}")
    return "every source enabled"


VERIFIED: Final[list[tuple[str, Callable[[Context], str]]]] = [
    ("score_lane_settled", verify_settled),
    ("profile_v1", verify_profile),
    ("grounded_result", verify_grounded),
    ("scope", verify_scope),
    ("retained", verify_retained),
    ("merge_event", verify_merge),
    ("reingest_group", verify_reingest),
    ("rejected_item", verify_rejected),
    ("e7_jobs", verify_e7),
    ("sources_enabled", verify_sources),
]


def step_verify(ctx: Context) -> str | None:
    return "; ".join(check(ctx) for _, check in VERIFIED)


STEPS: Final[list[tuple[str, Callable[[Context], str | None]]]] = [
    ("login", step_login),
    ("inputs", step_inputs),
    ("settings", step_settings),
    ("collection", step_collection),
    ("merge", step_merge),
    ("profile", step_profile),
    ("grounded", step_grounded),
    ("rejected_item", step_rejected_item),
    ("verify", step_verify),
]


def data_error(exc: Exception, last: list[str]) -> str:
    """A data error's detail: its class (a missing key with its name) and the last API answer, never a
    value it holds."""
    what = (
        f"{type(exc).__name__} {str(exc.args[0])[:60]!r}" if isinstance(exc, KeyError) else type(exc).__name__
    )
    return f"{what} after {last[0]}" if last else f"{what} before any API answer"


def run(client: httpx.Client) -> int:
    """Every step in order on `client` (module docstring); the exit status. A data error (an answer that is
    no JSON, a missing key, a wrong type) names the last answer the API gave: its method, path and status."""
    ctx = Context(client)
    last: list[str] = []

    def note(response: httpx.Response) -> None:
        last[:] = [f"{response.request.method} {response.request.url.path} answered {response.status_code}"]

    client.event_hooks = {**client.event_hooks, "response": [*client.event_hooks["response"], note]}
    for name, step in STEPS:
        try:
            detail = step(ctx)
        except CheckFailed as exc:
            emit(ctx, name, False, str(exc))
            return 1
        except subprocess.TimeoutExpired as exc:
            emit(ctx, name, False, f"{Path(str(exc.cmd[0])).name} timed out after {exc.timeout:g} s")
            return 1
        except (httpx.HTTPError, OSError) as exc:  # transport, a missing docker
            emit(ctx, name, False, type(exc).__name__)
            return 1
        except (ValueError, KeyError, TypeError) as exc:  # data: no JSON, a missing key, a wrong type
            emit(ctx, name, False, data_error(exc, last))
            return 1
        emit(ctx, name, True, detail)
    return 0


def main() -> int:
    # trust_env=False: the target is the loopback devproxy; no proxy variable of the calling shell applies.
    with httpx.Client(timeout=HTTP_TIMEOUT_S, trust_env=False) as client:
        return run(client)


if __name__ == "__main__":
    sys.exit(main())
