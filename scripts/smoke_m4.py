"""M4 acceptance smoke test (plan M4 Task 36): the api and the SPA on the E2E stack, every Claude call
answered by the fake and every source by the fixtures.

    DC="docker compose --env-file deploy/images.lock -f deploy/compose.dev.yaml \
        -f deploy/compose.fixtures.yaml -f deploy/compose.fakellm.yaml -f deploy/compose.e2e.yaml"
    $DC down -v && sudo scripts/dev-secrets.sh && $DC up -d --build --wait
    uv run python scripts/smoke_m4.py

The precondition is M3's: a FRESH stack with the four files (`deploy/compose.e2e.yaml` last: the worker's
keyed directory, which check 19 writes). The script creates the one user, so a stack that has one already
fails the first check with M2's hint. It never deletes anything.

It imports `scripts/smoke_m3.py` and runs its fifteen checks as they are, then eight of its own, which reuse
the smoke helpers and `scripts/m4_seeds.py` as `scripts/e2e_setup.py` does (and `e2e_setup`'s step-7 helpers
for check 19). Each check prints exactly one JSON line, `{"check": <name>, "ok": <bool>}` with an optional
`"detail"`:

16. `spa`: `GET /` (`Accept: text/html`) answers 200 `text/html` with the §16.6 CSP (`CSP`, the edge's), a
    `<div id="root">` and no inline script (`inline_scripts`); the first hashed asset it names answers 200
    with an `immutable` `Cache-Control`; `GET /settings` answers that same `index.html` (a deep link); `GET
    /api/nope`, even accepting `text/html`, answers 404 `application/problem+json`;
17. `openapi_doc`: `GET /api/openapi.json` answers 401 without the cookie and 200 with it, its `paths` naming
    `/api/jobs` and `/api/system/status`;
18. `jobs_list`: `GET /api/jobs` with every work mode (`WORK_MODES`, in order) and `LIST_LIMIT` has items,
    `ranking_generation`, `counts` and a `next_cursor`; that cursor gives page 2 (other jobs); the same
    filters with the work-mode values reversed page on: to the same page 2 when both answers carry the same
    `ranking_generation`, and to a page of items when a writer bumped it in between (a publish may reorder a
    keyset page by score); the cursor with another filter (the work modes without `unknown`) is 400
    `invalid_cursor`;
19. `job_detail` (E9's grounding, F11): a grounded candidate (`grounded_job`, below) is published with its
    grounded answers; its detail (`GET /api/jobs/{id}`) has at least one criterion whose description evidence
    has spans that slice the description to its quote, and no span that slices another text
    (`spanned_criteria`: zero such criteria fail the check, which the default recordings, quoting a synthetic
    ad, would give); its history (`GET /api/jobs/{id}/history`) answers 200;
20. `actions`: on the first page of the list, `PUT /api/jobs/{id}/actions {saved: true}` on the first unsaved
    job card raises the ranking generation (`ranking_state`, read with `psql`) by one, and `{seen_on_open:
    true}` on the card after it raises it by none; each is tried up to `ACTION_ATTEMPTS` times, so a bump of
    another writer between the two reads is not counted against it. Both are restored afterwards, whatever
    the result: `{saved: false}`, and `{seen: false}` when that card was unseen;
21. `status`: `GET /api/system/status` has a `banners` list, three `next_slots` and no `ready` member; then
    the smoke's own session (`SESSION_OF`: the one created with this run's user agent,
    `sessions.user_agent_sha256`) is left idle for the slide interval (`SLIDE_EVERY_S` and a margin, counted
    from its last slide), so a request could slide it: a request with `X-JH-Background: 1` leaves its
    `idle_expires_at` unchanged, and a foreground request after it slides it (else the probe proves nothing,
    and the check fails);
22. `needs_attention`: `m4_seeds.seed_rejected_item()` (the fixtures reject no item, `scripts/smoke_m1.py`,
    so the smoke seeds one as the E2E setup does); `GET /api/system/needs-attention` has every section
    (`SECTIONS`) and lists the seeded response `replayable`; `POST /api/system/raw-items/replay
    {raw_response_id}` answers 202; within `REPLAY_WITHIN_S` its operation `succeeded` with `progress.moved =
    1` and the seeded item is `linked` (`psql`), and the response is gone from the `raw_items` section;
23. `labelling`: `GET /api/labelling/queue` has items and its progress, and no member named for a score or a
    band at any depth (`score_members`).

**Check 19's job** (`grounded_job`) follows the E2E setup's step 7 (`scripts/e2e_setup.py`): the candidate
rule of `m4_seeds.GROUNDED_JOB_SQL` (a retained Greenhouse `getyourguide` job of completeness `full`) whose
description has three quotes (`m4_seeds.grounded_quotes`), read with its active-tuple target (`CANDIDATES`).
Whether grounded answers exist yet is read first: a candidate published with its grounded quotes as its
description evidence (`e2e_setup.grounded_published`: a stack that ran the E2E setup, or a second run) is
read as it is. Otherwise the lowest candidate whose target Score now takes (`SCORE_NOW_STATES`: `pending`,
`budget_deferred`, `expired_unscored` and the retryable states; not a grouped one) and that resumes no draft
(after smoke_m3's check 14 a follow-up of the batch resumes the draft the batch made on the default
recordings, and a retry the draft an earlier attempt made, which no answer written now changes; `resumes` is
`targets.RESUMABLE_DRAFT_SQL` over any policy, so it is conservative) gets its grounded answers in the
worker's keyed directory
(`m4_seeds.write_grounded_answers`, unless they are there), then `POST /api/jobs/{id}/score-now` with a fresh
`Idempotency-Key` (a 409 `conflict_state`, a cycle having claimed it since, is awaited alike: it scores on
the same answers), and its publish is awaited (`e2e_setup.await_publish`).

It stops at the first failed check (exit 1); after twenty-three `ok` lines it exits 0. An answer of an
unexpected shape (no JSON, a missing key, a wrong type) fails its check with one JSON line too, naming the
error and the last API answer (`e2e_setup.data_error`), never a traceback. A `detail` holds ids,
states, counts, codes and paths, and fixed text: never the password, the session cookie or its token
(`Context.scrub`), an `Idempotency-Key`, a key, an ad's text, a CV text or a draft."""

import hashlib
import re
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final

import e2e_setup  # scripts/ is the script's directory, the first entry of sys.path
import httpx
import m4_seeds
import smoke_m3
from smoke_m0 import HTTP_TIMEOUT_S, ORIGIN, CheckFailed, Context, emit, pick
from smoke_m2 import json_object, mutating

USER_AGENT: Final = "jobhunter-smoke-m4"  # `main` adds a fresh id per run (check 21 finds its session by it)

# Check 16: `api.edge.SECURITY_HEADERS`' CSP (§16.6, pinned by the test).
CSP: Final = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
    "form-action 'self'; object-src 'none'"
)
PAGE: Final = {"Accept": "text/html"}
ROOT_DIV: Final = '<div id="root">'
SCRIPT_ELEMENT: Final = re.compile(r"<script\b([^>]*)>(.*?)</script\s*>", re.IGNORECASE | re.DOTALL)
SRC_ATTRIBUTE: Final = re.compile(r"\bsrc\s*=", re.IGNORECASE)
HASHED_ASSET: Final = re.compile(r'(?:src|href)="(/assets/[^"]+)"')
DEEP_LINK: Final = "/settings"
UNKNOWN_API: Final = "/api/nope"
PROBLEM_JSON: Final = "application/problem+json"

# Check 17.
OPENAPI_PATHS: Final = ("/api/jobs", "/api/system/status")

# Check 18: `jobs.query.WorkMode`'s values (pinned by the test), and a page small enough to have a next one.
WORK_MODES: Final = ("remote", "hybrid", "onsite", "unknown")
LIST_LIMIT: Final = 5
INVALID_CURSOR: Final = "invalid_cursor"

# Check 19: the states `scoring.targets.score_now` moves to `pending` without `force` (`ACCEPTED_STATES` and
# `RETRYABLE_STATES`, pinned by the test), but `deferred_duplicate` (a grouped member, whose drawer shows its
# representative's description).
SCORE_NOW_STATES: Final = (
    "pending",
    "budget_deferred",
    "expired_unscored",
    "refused",
    "failed_max_tokens",
    "failed_invalid_output",
    "failed_request",
    "failed_transient",
    "failed_attempts_exhausted",
)
# Every candidate (`m4_seeds.GROUNDED_JOB_SQL`) with its active-tuple target: its state, its published result,
# whether it resumes a draft, and its description (never printed); one JSON array, by job id. As
# `e2e_setup.GROUNDED_CANDIDATES`, with `resumes`: `targets.RESUMABLE_DRAFT_SQL` without its policy (a
# `resume_draft_id`, or a draft on any attempt of the target), so it holds wherever the scoring code's does.
RESUMES: Final = (
    "(t.resume_draft_id IS NOT NULL OR EXISTS (SELECT 1 FROM score_attempts a "
    "JOIN score_drafts d ON d.attempt_id = a.id "
    "WHERE a.target_id = t.id))"
)
CANDIDATES: Final = f"""\
/* smoke_m4.candidates */ SELECT coalesce(json_agg(json_build_object('job_id', j.id,
  'state', t.state, 'result_id', t.published_result_id, 'resumes', {RESUMES},
  'description', r.description_text) ORDER BY j.id), '[]')::text
FROM jobs j JOIN job_revisions r ON r.id = j.current_revision_id
JOIN active_config a ON a.id = 1
JOIN score_targets t ON t.job_revision_id = j.current_revision_id AND t.llm_tuple_id = a.llm_tuple_id
WHERE {m4_seeds.GROUNDED_JOB_SQL}"""  # noqa: S608 (constants only)

# Check 20.
GENERATION: Final = "/* smoke_m4.generation */ SELECT generation FROM ranking_state WHERE id = 1"
ACTION_ATTEMPTS: Final = 3

# Check 21: `auth.sessions.SLIDE_EVERY` and `api.deps.BACKGROUND_HEADER` (pinned by the test). `{agent}` is
# the hex SHA-256 of the user agent the smoke signed in with: its session, its idle expiry and the seconds
# since its last slide (epoch seconds).
SLIDE_EVERY_S: Final = 60
SLIDE_MARGIN_S: Final = 2
BACKGROUND: Final = {"X-JH-Background": "1"}
SESSION_OF: Final = (
    "/* smoke_m4.session */ SELECT count(*), coalesce(max(extract(epoch FROM idle_expires_at)), 0), "
    "coalesce(max(extract(epoch FROM now() - last_seen_at)), 0) FROM sessions "
    "WHERE user_agent_sha256 = decode('{agent}', 'hex') AND revoked_at IS NULL"
)
NEXT_SLOTS: Final = 3

# Check 22: `observability.attention.AttentionOut`'s sections (pinned by the test). `{operation}` and `{item}`
# are integers the api and the seed answered: the replay operation's status, its `progress.moved`, and the
# seeded item's disposition.
SECTIONS: Final = (
    "raw_items",
    "identity_conflicts",
    "failed_targets",
    "quarantined_batches",
    "retrieve_failing",
    "mass_disappearances",
    "unscorable_groups",
    "broken_sources",
    "paused_lane",
    "scope_apply_failed",
    "failed_purges",
)
REPLAY_STATE: Final = (
    "/* smoke_m4.replay_state */ SELECT o.status, coalesce(o.progress->>'moved', ''), "
    "coalesce((SELECT disposition FROM raw_items WHERE id = {item}), 'none') FROM operations o "
    "WHERE o.id = {operation}"
)
REPLAY_WITHIN_S: Final = 120

# Check 23: a member named for a score or a band (`score_mid`, `band`, ...).
SCORE_MEMBER: Final = re.compile(r"score|band", re.IGNORECASE)


def fresh_key() -> str:
    """A new `Idempotency-Key`: never printed."""
    return f"smoke-m4-{uuid.uuid4().hex}"


def get(ctx: Context, path: str, **kwargs: Any) -> httpx.Response:
    headers = {**ctx.auth(), **kwargs.pop("headers", {})}
    return ctx.client.get(f"{ORIGIN}{path}", headers=headers, **kwargs)


# ---- 16 spa ------------------------------------------------------------------------------------------------


def inline_scripts(html: str) -> int:
    """How many `<script>` elements of `html` hold code of their own or have no `src`."""
    return sum(
        1
        for attributes, body in SCRIPT_ELEMENT.findall(html)
        if body.strip() or not SRC_ATTRIBUTE.search(attributes)
    )


def page_problem(response: httpx.Response) -> list[str]:
    """What an `index.html` answer lacks: 200 `text/html`, the CSP, the root element, no inline script."""
    problems = []
    if response.status_code != 200:
        problems.append(f"HTTP {response.status_code}")
    if not response.headers.get("content-type", "").startswith("text/html"):
        problems.append("not text/html")
    if response.headers.get("content-security-policy") != CSP:
        problems.append("not the §16.6 CSP")
    if ROOT_DIV not in response.text:
        problems.append(f"no {ROOT_DIV}")
    if scripts := inline_scripts(response.text):
        problems.append(f"{scripts} inline script(s)")
    return problems


def check_spa(ctx: Context) -> str | None:
    index = ctx.client.get(f"{ORIGIN}/", headers=PAGE)
    if problems := page_problem(index):
        raise CheckFailed(f"GET /: {', '.join(problems)}")
    assets = HASHED_ASSET.findall(index.text)
    if not assets:
        raise CheckFailed("GET /: names no hashed asset")
    asset = ctx.client.get(f"{ORIGIN}{assets[0]}")
    cache = asset.headers.get("cache-control", "")
    if asset.status_code != 200 or "immutable" not in cache:
        raise CheckFailed(
            f"GET {assets[0]}: expected 200 with an immutable Cache-Control, got {asset.status_code} "
            f"{cache!r}"
        )
    deep = ctx.client.get(f"{ORIGIN}{DEEP_LINK}", headers=PAGE)
    problems = page_problem(deep)
    if not problems and deep.text != index.text:
        problems.append("not the index.html of GET /")
    if problems:
        raise CheckFailed(f"GET {DEEP_LINK}: {', '.join(problems)}")
    nope = get(ctx, UNKNOWN_API, headers=PAGE)
    kind = nope.headers.get("content-type", "")
    if nope.status_code != 404 or not kind.startswith(PROBLEM_JSON):
        raise CheckFailed(f"GET {UNKNOWN_API}: expected 404 {PROBLEM_JSON}, got {nope.status_code} {kind!r}")
    return (
        f"GET / is index.html with the CSP, {ROOT_DIV} and no inline script; {assets[0]} {cache!r}; "
        f"{DEEP_LINK} is index.html; {UNKNOWN_API} 404 {PROBLEM_JSON}"
    )


# ---- 17 openapi_doc ----------------------------------------------------------------------------------------


def check_openapi_doc(ctx: Context) -> str | None:
    ctx.client.cookies.clear()  # the session travels as an explicit header only (smoke_m0)
    anonymous = ctx.client.get(f"{ORIGIN}/api/openapi.json")
    if anonymous.status_code != 401:
        raise CheckFailed(f"expected 401 without the cookie, got {anonymous.status_code}")
    document = json_object(get(ctx, "/api/openapi.json"), 200)
    paths = pick(document, "paths")
    if not isinstance(paths, dict):
        raise CheckFailed("the document has no paths")
    if missing := [path for path in OPENAPI_PATHS if path not in paths]:
        raise CheckFailed(f"the document's paths lack {missing}")
    return f"401 without the cookie; 200 with it: OpenAPI {pick(document, 'openapi')!r}, {len(paths)} paths"


# ---- 18 jobs_list ------------------------------------------------------------------------------------------


def list_page(ctx: Context, modes: tuple[str, ...], cursor: str | None = None) -> httpx.Response:
    params: list[tuple[str, str | int]] = [("limit", LIST_LIMIT), *(("work_mode", mode) for mode in modes)]
    if cursor is not None:
        params.append(("cursor", cursor))
    return get(ctx, "/api/jobs", params=params)


def job_ids(page: Any) -> list[Any]:
    items = pick(page, "items")
    return [pick(item, "job_id") for item in items] if isinstance(items, list) else []


def check_jobs_list(ctx: Context) -> str | None:
    first = json_object(list_page(ctx, WORK_MODES), 200)
    generation, counts, cursor = (pick(first, key) for key in ("ranking_generation", "counts", "next_cursor"))
    if not job_ids(first) or not isinstance(generation, int) or not isinstance(counts, dict):
        raise CheckFailed("the first page has no items, ranking_generation or counts")
    if not isinstance(cursor, str):
        raise CheckFailed(f"the first page of {LIST_LIMIT} has no next_cursor")
    page_2 = json_object(list_page(ctx, WORK_MODES, cursor), 200)
    second = job_ids(page_2)
    if not second or set(second) & set(job_ids(first)):
        raise CheckFailed(
            f"page 2 has {len(second)} items, {len(set(second) & set(job_ids(first)))} of page 1"
        )
    reordered = list_page(ctx, WORK_MODES[::-1], cursor)
    again = reordered.json() if reordered.status_code == 200 else None
    # a writer's bump between the two reads may reorder a keyset page by score: then any page of items will do
    compared = again is not None and pick(again, "ranking_generation") == pick(page_2, "ranking_generation")
    if again is None or not job_ids(again) or (compared and job_ids(again) != second):
        raise CheckFailed(
            f"reordered work_mode values: expected 200 with page 2 again, got HTTP {reordered.status_code} "
            f"(code {e2e_setup.problem_code(reordered)!r})"
        )
    paged_on = (
        "the same with the work_mode values reversed"
        if compared
        else "the work_mode values reversed page on under another ranking generation (ids not compared)"
    )
    other = list_page(ctx, WORK_MODES[:-1], cursor)
    code = e2e_setup.problem_code(other)
    if other.status_code != 400 or code != INVALID_CURSOR:
        raise CheckFailed(
            f"another filter (work_mode without {WORK_MODES[-1]!r}): expected 400 {INVALID_CURSOR}, got "
            f"{other.status_code} (code {code!r})"
        )
    return (
        f"page 1: {len(job_ids(first))} items, ranking_generation {generation}, counts "
        f"total_estimate={pick(counts, 'total_estimate')!r} capped={pick(counts, 'capped')!r}; page 2: "
        f"{len(second)} other items, {paged_on}; another filter: 400 {code}"
    )


# ---- 19 job_detail -----------------------------------------------------------------------------------------


def spanned_criteria(detail: Any) -> tuple[int, int, int]:
    """Of the detail's criteria: those with a description evidence whose span slices the description to its
    quote, those with any description evidence, and the description spans that slice another text."""
    text, criteria = pick(detail, "description", "text"), pick(detail, "score", "criteria")
    if not isinstance(text, str) or not isinstance(criteria, list):
        return 0, 0, 0
    spanned = described = wrong = 0
    for criterion in criteria:
        evidence = [e for e in pick(criterion, "evidence") or [] if pick(e, "source_kind") == "description"]
        sliced = False
        for item in evidence:
            start, end = pick(item, "span_start"), pick(item, "span_end")
            if isinstance(start, int) and isinstance(end, int):
                if text[start:end] == pick(item, "quote"):
                    sliced = True
                else:
                    wrong += 1
        described += bool(evidence)
        spanned += sliced
    return spanned, described, wrong


def candidate_state(candidate: Any) -> str:
    state = str(pick(candidate, "state"))
    return f"{state} (resumes a draft)" if pick(candidate, "resumes") is True else state


def grounded_job(ctx: Context) -> tuple[int, str]:
    """Check 19's job, published with its grounded answers (module docstring): its id and what was done."""
    candidates = m4_seeds.json_line(m4_seeds.stack_sql(CANDIDATES), what="the grounded candidates")
    if not isinstance(candidates, list):
        raise CheckFailed("the grounded candidates are no list")
    described = [c for c in candidates if e2e_setup.quotes_of(pick(c, "description")) is not None]
    published = next((c for c in described if e2e_setup.grounded_published(c)), None)
    if published is not None:
        return int(published["job_id"]), f"job {published['job_id']}: published grounded already"
    chosen = next(
        (c for c in described if pick(c, "state") in SCORE_NOW_STATES and pick(c, "resumes") is False), None
    )
    if chosen is None:
        raise CheckFailed(
            f"no {m4_seeds.GROUNDED_BOARD[0]} {m4_seeds.GROUNDED_BOARD[1]} job of completeness full with "
            "three sentences of 30 to 160 code points is published grounded or has a target Score now scores "
            "afresh: "
            f"candidates={len(candidates)} with_three_sentences={len(described)} "
            f"states={sorted({candidate_state(c) for c in described})}"
        )
    job_id, state = int(chosen["job_id"]), pick(chosen, "state")
    present = m4_seeds.grounded_files_present(job_id)
    if not present:
        m4_seeds.write_grounded_answers(job_id)
    response = ctx.client.post(
        f"{ORIGIN}/api/jobs/{job_id}/score-now", json={}, headers=mutating(ctx, fresh_key())
    )
    code = e2e_setup.problem_code(response) if response.status_code == 409 else None
    if response.status_code != 202 and code != e2e_setup.CLAIMED:
        raise CheckFailed(
            f"score-now of job {job_id}: expected 202, got {response.status_code} (code {code!r})"
        )
    answered = f"{response.status_code}" + (f" ({code})" if code else "")
    published_as = e2e_setup.await_publish(job_id)
    files = "present" if present else "written"
    return job_id, f"job {job_id} ({state}): grounded answers {files}; score-now {answered}; {published_as}"


def check_job_detail(ctx: Context) -> str | None:
    job_id, how = grounded_job(ctx)
    detail = json_object(get(ctx, f"/api/jobs/{job_id}"), 200)
    spanned, described, wrong = spanned_criteria(detail)
    if wrong:
        raise CheckFailed(f"job {job_id}: {wrong} description span(s) slice another text than their quotes")
    found = f"{spanned} of {described} criteria with description evidence have spans that slice their quotes"
    if not spanned:
        raise CheckFailed(f"job {job_id}: {found}")
    history = get(ctx, f"/api/jobs/{job_id}/history")
    if history.status_code != 200:
        raise CheckFailed(f"job {job_id}: the history answered {history.status_code}")
    items = pick(history.json(), "items")
    return f"{how}; {found}; history 200 ({len(items) if isinstance(items, list) else None} items)"


# ---- 20 actions --------------------------------------------------------------------------------------------


def generation() -> int:
    found = e2e_setup.row(GENERATION, 1, what="the ranking generation")
    (value,) = m4_seeds.ints(found, what="the generation")
    return value


def put_actions(ctx: Context, job_id: int, body: dict[str, bool]) -> dict[str, Any]:
    response = ctx.client.put(f"{ORIGIN}/api/jobs/{job_id}/actions", json=body, headers=mutating(ctx))
    return json_object(response, 200)


def bump_by(ctx: Context, job_id: int, body: dict[str, bool], *, wanted: int, action: str) -> int:
    """The attempt (1 to `ACTION_ATTEMPTS`) at which `body` on the job moved the generation by `wanted`; a
    concurrent writer's bump between the reads only costs an attempt."""
    moved = []
    for attempt in range(1, ACTION_ATTEMPTS + 1):
        before = generation()
        put_actions(ctx, job_id, body)
        moved.append(generation() - before)
        if moved[-1] == wanted:
            return attempt
    raise CheckFailed(f"{action} on job {job_id} moved the generation by {moved}, not {wanted}")


def check_actions(ctx: Context) -> str | None:
    page = json_object(get(ctx, "/api/jobs", params={"limit": LIST_LIMIT}), 200)
    cards = [c for c in pick(page, "items") or [] if pick(c, "kind") == "job"]
    unsaved = next((i for i, c in enumerate(cards) if pick(c, "user", "saved_at") is None), None)
    if unsaved is None or unsaved + 1 >= len(cards):
        raise CheckFailed(
            f"the first page has no unsaved job card with another after it ({len(cards)} job cards)"
        )
    saved_job, opened_job = int(cards[unsaved]["job_id"]), int(cards[unsaved + 1]["job_id"])
    was_seen = pick(cards[unsaved + 1], "user", "seen_at") is not None
    start = generation()
    try:
        saved = bump_by(ctx, saved_job, {"saved": True}, wanted=1, action="saved")
        opened = bump_by(ctx, opened_job, {"seen_on_open": True}, wanted=0, action="seen_on_open")
    finally:
        put_actions(ctx, saved_job, {"saved": False})
        if not was_seen:
            put_actions(ctx, opened_job, {"seen": False})
    restored = (
        f"seen false on job {opened_job}"
        if not was_seen
        else f"job {opened_job} was seen already: nothing to restore"
    )
    return (
        f"generation {start}: saved on job {saved_job} moved it by 1 (attempt {saved}); seen_on_open on job "
        f"{opened_job} moved it by 0 (attempt {opened}); restored: saved false on job {saved_job}, {restored}"
    )


# ---- 21 status ---------------------------------------------------------------------------------------------


def session_of(statement: str) -> tuple[Decimal, Decimal]:
    """The smoke's session: its idle expiry and the seconds since its last slide (epoch seconds)."""
    count, idle, quiet = e2e_setup.row(statement, 3, what="the smoke's session")
    if count != "1":
        raise CheckFailed(f"expected one session of the smoke's user agent, found {count}")
    try:
        return Decimal(idle), Decimal(quiet)
    except InvalidOperation:
        raise CheckFailed("the session's times are no numbers") from None


def check_status(ctx: Context) -> str | None:
    status = json_object(get(ctx, "/api/system/status"), 200)
    banners, slots = pick(status, "banners"), pick(status, "next_slots")
    problems = []
    if not isinstance(banners, list):
        problems.append("no banners list")
    if not isinstance(slots, list) or len(slots) != NEXT_SLOTS:
        problems.append(f"{len(slots) if isinstance(slots, list) else None} next_slots")
    if "ready" in status:
        problems.append("a ready member")
    if problems:
        raise CheckFailed(f"the header status: {', '.join(problems)}")
    agent = hashlib.sha256(str(ctx.client.headers["user-agent"]).encode("utf-8")).hexdigest()
    statement = SESSION_OF.format(agent=agent)
    idle, quiet = session_of(statement)
    if quiet < SLIDE_EVERY_S + SLIDE_MARGIN_S:
        time.sleep(float(SLIDE_EVERY_S + SLIDE_MARGIN_S - quiet))
    json_object(get(ctx, "/api/system/status", headers=BACKGROUND), 200)
    after_background, waited = session_of(statement)
    if after_background != idle:
        raise CheckFailed(f"the background request moved idle_expires_at by {after_background - idle} s")
    json_object(get(ctx, "/api/system/status"), 200)
    after_foreground, _ = session_of(statement)
    if after_foreground <= idle:
        raise CheckFailed(f"after {waited:.0f} s idle a foreground request did not slide it either: no proof")
    codes = [pick(banner, "code") for banner in banners or []]
    return (
        f"banners {codes}, {NEXT_SLOTS} next_slots, no ready member; after {waited:.0f} s idle a request "
        "with "
        f"X-JH-Background: 1 left idle_expires_at unchanged, and a foreground request then slid it by "
        f"{after_foreground - idle:.0f} s"
    )


# ---- 22 needs_attention ------------------------------------------------------------------------------------


def missing_sections(attention: Any) -> list[str]:
    return [section for section in SECTIONS if not isinstance(attention, dict) or section not in attention]


def listed_responses(attention: Any) -> dict[Any, Any]:
    """The `raw_items` section's responses, each to whether it is `replayable`."""
    items = pick(attention, "raw_items", "items")
    return {pick(item, "raw_response_id"): pick(item, "replayable") for item in items or []}


def check_needs_attention(ctx: Context) -> str | None:
    response_id, item_id = m4_seeds.seed_rejected_item()
    seeded = f"raw response {response_id}, raw item {item_id} seeded"
    attention = json_object(get(ctx, "/api/system/needs-attention"), 200)
    if missing := missing_sections(attention):
        raise CheckFailed(f"{seeded}; the sections {missing} are missing")
    if listed_responses(attention).get(response_id) is not True:
        raise CheckFailed(f"{seeded}; raw response {response_id} is not listed replayable")
    queued = json_object(
        ctx.client.post(
            f"{ORIGIN}/api/system/raw-items/replay",
            json={"raw_response_id": response_id},
            headers=mutating(ctx),
        ),
        202,
    )
    operation = pick(queued, "operation_id")
    if not isinstance(operation, int):
        raise CheckFailed(f"{seeded}; the replay answered no operation id")

    def done(row: list[str]) -> tuple[bool, str]:
        status, moved, disposition = row
        if status == "failed":
            raise CheckFailed(f"replay operation {operation} failed")
        if status == "succeeded" and moved != "1":
            raise CheckFailed(
                f"replay operation {operation} succeeded with progress.moved = {moved!r}, not 1"
            )
        return (
            status == "succeeded" and disposition == "linked",
            f"operation {status} moved={moved} item {disposition}",
        )

    statement = REPLAY_STATE.format(operation=int(operation), item=int(item_id))
    e2e_setup.wait_rows(statement, 3, done, within_s=REPLAY_WITHIN_S, what="the replay's linked item")
    after = json_object(get(ctx, "/api/system/needs-attention"), 200)
    if response_id in listed_responses(after):
        raise CheckFailed(f"raw response {response_id} is still listed after the replay")
    return (
        f"{seeded}; listed replayable with all {len(SECTIONS)} sections; replay of "
        f"{pick(queued, 'items')!r} item(s) "
        f"202: operation {operation} succeeded, moved 1, the item linked, the response no longer listed"
    )


# ---- 23 labelling ------------------------------------------------------------------------------------------


def score_members(body: Any, path: str = "") -> list[str]:
    """The paths of the members of `body`, at any depth, named for a score or a band (`SCORE_MEMBER`)."""
    found: list[str] = []
    if isinstance(body, dict):
        for key, value in body.items():
            here = f"{path}.{key}" if path else str(key)
            if SCORE_MEMBER.search(str(key)):
                found.append(here)
            found += score_members(value, here)
    elif isinstance(body, list):
        for index, value in enumerate(body):
            found += score_members(value, f"{path}[{index}]")
    return found


def check_labelling(ctx: Context) -> str | None:
    queue = json_object(get(ctx, "/api/labelling/queue"), 200)
    items, progress = pick(queue, "items"), pick(queue, "progress")
    if not isinstance(items, list) or not items or not isinstance(progress, dict):
        raise CheckFailed("the queue has no items or no progress")
    if members := score_members(queue):
        raise CheckFailed(f"the queue has score members: {members}")
    return (
        f"{len(items)} items, progress {pick(progress, 'labelled')!r} of {pick(progress, 'target')!r}; no "
        "score "
        "or band member"
    )


CHECKS: Final[list[tuple[str, Callable[[Context], str | None]]]] = [
    *smoke_m3.CHECKS,
    ("spa", check_spa),
    ("openapi_doc", check_openapi_doc),
    ("jobs_list", check_jobs_list),
    ("job_detail", check_job_detail),
    ("actions", check_actions),
    ("status", check_status),
    ("needs_attention", check_needs_attention),
    ("labelling", check_labelling),
]


def main() -> int:
    # trust_env=False: the target is the loopback devproxy; no proxy variable of the calling shell applies.
    # The user agent is this run's own, so check 21 finds the session it creates.
    agent = f"{USER_AGENT}/{uuid.uuid4().hex}"
    with httpx.Client(timeout=HTTP_TIMEOUT_S, trust_env=False, headers={"User-Agent": agent}) as client:
        ctx = Context(client)
        last = e2e_setup.last_answer(client)
        for name, check in CHECKS:
            try:
                detail = check(ctx)
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
                emit(ctx, name, False, e2e_setup.data_error(exc, last))
                return 1
            emit(ctx, name, True, detail)
    return 0


if __name__ == "__main__":
    sys.exit(main())
