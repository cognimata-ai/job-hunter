"""M2 acceptance smoke test (Task 24): a CV through the profile flow and a settings save on the dev stack,
every Claude call answered by the recorded fake and every source by the fixtures.

    docker compose --env-file deploy/images.lock -f deploy/compose.dev.yaml -f deploy/compose.fixtures.yaml \
        -f deploy/compose.fakellm.yaml down -v
    docker compose --env-file deploy/images.lock -f deploy/compose.dev.yaml -f deploy/compose.fixtures.yaml \
        -f deploy/compose.fakellm.yaml up -d --build --wait
    uv run python scripts/smoke_m2.py

The precondition is a FRESH dev stack with both overlays: `deploy/compose.fixtures.yaml` (the worker's sources
answered from `tests/contract/fixtures`) and `deploy/compose.fakellm.yaml` (the worker's Claude calls answered
from the committed streams under `tests/contract/fixtures/anthropic`, so no request reaches Anthropic). The
script creates the one user, so a stack that has one already fails the first check with the hint below. It
never deletes anything.

Its own compose calls pass the three files and the digest lock as `DC` does. It imports the helpers of
`scripts/smoke_m0.py` (`Context`, `emit`, `psql`, `check_login`, …) and `scripts/smoke_m1.py`
(`check_health_ready`, `query_row`) and uses them as they are; `psql` is a `docker compose exec` into the
running `postgres` container, which neither creates nor recreates a container. The CV is a synthetic PDF of
`SYNTHETIC_CV` (an invented candidate), built by `tests/support/profile.minimal_pdf`. Ten checks run in order
and each prints exactly one JSON line, `{"check": <name>, "ok": <bool>}` with an optional `"detail"`:

1. `user_create`: `jobhunter user create --password-stdin` in a one-off api container;
2. `login`: `POST /api/auth/login` answers 204 with the session cookie (M0's check);
3. `health_ready`: `GET /api/system/health` reports `ready.ok` and `embedder.ok`, with a `quota` object
   (M1's check);
4. `cv_upload`: first the running worker must be on both fakes (`JOBHUNTER_FAKE_LLM` and
   `JOBHUNTER_FAKE_SOURCES` are `1`, read with `docker compose exec worker printenv` and compared here: only
   the booleans are printed, never a value), so a stack without the overlays fails here, before anything is
   uploaded; then `POST /api/profile/cv` with the synthetic PDF answers 202 with a new draft;
5. `text_review`: the draft reaches `text_review` within 90 s (the worker and docparse), and its strip report
   counts at least one e-mail address;
6. `extract_ready`: the approval (`POST …/text-review`) answers 202, and the draft is `ready` within 90 s (the
   extraction on the fake);
7. `confirm`: `POST …/confirm` with `expected_rescore_ack` answers 201, and `GET /api/profile` shows the
   confirmed version active as `version_no` 1;
8. `proposal_ready`: the query proposal the confirm requested (its id read with `psql`; a new request would
   ask for another once it is ready) is `ready` within 90 s, with 6 to 16 items;
9. `settings_save`: two of its items accepted (`POST /api/queries`, 201 each), then a dry-run of the active
   payload with both enabled and the save of it acknowledging the dry-run's warnings (201);
10. `spend`: `GET /api/system/spend` counts more than $0 and at most the cap less the overrun buffer.

It stops at the first failed check (exit 1); after ten `ok` lines it exits 0. The password, the session cookie
and its token are never printed (`Context.scrub`, as in M0), nor any CV text, profile value or query text: a
`detail` holds ids, statuses, counts, codes and amounts, and fixed text.
"""

import subprocess
import sys
import time
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final

import httpx
from smoke_m0 import (  # scripts/ is the script's directory, the first entry of sys.path
    DOCKER_TIMEOUT_S,
    HTTP_TIMEOUT_S,
    ORIGIN,
    PASSWORD,
    POLL_INTERVAL_S,
    USER_CREATE,
    USER_EXISTS_MESSAGE,
    CheckFailed,
    Context,
    check_login,
    emit,
    pick,
    stderr_tail,
)
from smoke_m1 import check_health_ready, query_row

REPO: Final = Path(__file__).resolve().parents[1]
DC: Final = [
    "docker",
    "compose",
    "--env-file",
    str(REPO / "deploy/images.lock"),
    "-f",
    str(REPO / "deploy/compose.dev.yaml"),
    "-f",
    str(REPO / "deploy/compose.fixtures.yaml"),
    "-f",
    str(REPO / "deploy/compose.fakellm.yaml"),
]
FRESH_HINT: Final = (
    "stack not fresh: run `docker compose --env-file deploy/images.lock -f deploy/compose.dev.yaml "
    "-f deploy/compose.fixtures.yaml -f deploy/compose.fakellm.yaml down -v` first"
)

# The worker's switches of the two overlays: deploy/compose.fakellm.yaml and deploy/compose.fixtures.yaml.
FAKE_VARS: Final = ("JOBHUNTER_FAKE_LLM", "JOBHUNTER_FAKE_SOURCES")
CV_FILENAME: Final = "Lebenslauf.pdf"
# The categories of a strip report (§12.3), the only keys of it the script prints.
STRIP_CATEGORIES: Final = ("email", "phone", "url", "address", "personal", "name")
TEXT_REVIEW_WITHIN_S: Final = 90
EXTRACT_WITHIN_S: Final = 90
PROPOSAL_WITHIN_S: Final = 90
PROPOSAL_ITEMS: Final = range(6, 17)  # §11.12: 6 to 16 kept items
ENABLED_QUERIES: Final = 2
# The `Idempotency-Key` of each request that needs one (§14.1).
APPROVE_KEY: Final = "smoke-m2-approve-0001"
CONFIRM_KEY: Final = "smoke-m2-confirm-0001"
SAVE_KEY: Final = "smoke-m2-settings-0001"
# The proposals of a version and the oldest one's id: the confirm's is the only one. `{version_id}` is an
# integer the api answered.
PROPOSALS_OF: Final = "SELECT count(*), min(id) FROM query_proposals WHERE profile_version_id = {version_id}"


class State:
    """What the checks hand on: the draft, the confirmed version and its proposal's kept items."""

    draft_id: int | None = None
    version_id: int | None = None
    proposal_id: int | None = None
    items: list[dict[str, Any]] | None = None


STATE: Final = State()


def mutating(ctx: Context, key: str | None = None) -> dict[str, str]:
    """The headers of a request that changes state: the session, the origin, the CSRF marker and a key."""
    headers = {**ctx.auth(), "Origin": ORIGIN, "X-JH-Request": "1"}
    return headers if key is None else {**headers, "Idempotency-Key": key}


def json_object(response: httpx.Response, expected: int) -> dict[str, Any]:
    """The JSON object of `response`, whose status must be `expected` (the status only, otherwise)."""
    if response.status_code != expected:
        raise CheckFailed(f"expected {expected}, got {response.status_code}")
    body = response.json()
    if not isinstance(body, dict):
        raise CheckFailed(f"expected a JSON object, got {type(body).__name__}")
    return body


def poll(
    ctx: Context, path: str, done: Callable[[Any], tuple[bool, str]], *, within_s: int, what: str
) -> Any:
    """Poll `GET path` until `done(body)` says so, or `within_s` seconds have passed; the last body."""
    deadline = time.monotonic() + within_s
    last = "no answer yet"
    while True:
        try:
            response = ctx.client.get(f"{ORIGIN}{path}", headers=ctx.auth())
        except httpx.HTTPError as exc:
            last = type(exc).__name__
        else:
            if response.status_code in (401, 403):
                raise CheckFailed(f"the session was refused (HTTP {response.status_code})")
            if response.status_code == 200:
                body = response.json()
                finished, last = done(body)
                if finished:
                    return body
            else:
                last = f"HTTP {response.status_code}"
        if time.monotonic() >= deadline:
            raise CheckFailed(f"{what} not reached within {within_s} s (last: {last})")
        time.sleep(POLL_INTERVAL_S)


def draft_reached(status: str) -> Callable[[Any], tuple[bool, str]]:
    """`poll`'s test for a draft in `status`; a draft that failed fails the check at once (its code only)."""

    def done(body: Any) -> tuple[bool, str]:
        found, code = pick(body, "status"), pick(body, "failure_code")
        if found == "failed":
            raise CheckFailed(f"the draft failed ({code!r})")
        return found == status, f"status={found!r}"

    return done


def synthetic_pdf() -> bytes:
    """`minimal_pdf` of `SYNTHETIC_CV`'s lines (tests/support/profile.py: an invented candidate)."""
    if str(REPO) not in sys.path:  # `tests` is a package of the repository root, not of scripts/
        sys.path.insert(1, str(REPO))
    from tests.support.profile import SYNTHETIC_CV, minimal_pdf  # only this check needs it

    return minimal_pdf(SYNTHETIC_CV.splitlines())


def worker_flags() -> dict[str, bool]:
    """Whether the running worker container has each of `FAKE_VARS` set to `1`. The values are compared here
    and never printed; an unset variable is `False` (printenv's silent exit 1). An exec that fails (any other
    exit, or exit 1 with a message) raises `CheckFailed` with its stderr."""
    flags: dict[str, bool] = {}
    for name in FAKE_VARS:
        process = subprocess.run(
            [*DC, "exec", "-T", "worker", "printenv", name],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=DOCKER_TIMEOUT_S,
        )
        # As smoke_m1's `worker_fake_mode`: printenv exits 1, silently, for an unset variable.
        if process.returncode != 0 and (process.returncode != 1 or process.stderr.strip()):
            raise CheckFailed(
                f"docker compose exec worker: exit {process.returncode}: {stderr_tail(process)}"
            )
        flags[name] = process.returncode == 0 and process.stdout.strip() == "1"
    return flags


def check_user_create(ctx: Context) -> str | None:
    process = subprocess.run(
        [*DC, "run", "--rm", "-T", "api", *USER_CREATE],
        input=f"{PASSWORD}\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=DOCKER_TIMEOUT_S,
    )
    if process.returncode == 0:
        return None
    if USER_EXISTS_MESSAGE in process.stderr:
        raise CheckFailed(FRESH_HINT)
    raise CheckFailed(f"exit {process.returncode}: {stderr_tail(process)}")


def check_cv_upload(ctx: Context) -> str | None:
    # The approval of check 6 makes a Claude call: on a stack without the fake LLM it would reach Anthropic.
    flags = worker_flags()
    shown = " ".join(f"{name.removeprefix('JOBHUNTER_').lower()}={flag}" for name, flag in flags.items())
    if not all(flags.values()):
        raise CheckFailed(f"the worker is not on both fakes ({shown}): nothing was uploaded")
    response = ctx.client.post(
        f"{ORIGIN}/api/profile/cv",
        files={"file": (CV_FILENAME, synthetic_pdf(), "application/pdf")},
        headers=mutating(ctx),
    )
    draft_id = pick(json_object(response, 202), "draft_id")
    if not isinstance(draft_id, int):
        raise CheckFailed(f"expected a draft id, got {type(draft_id).__name__}")
    STATE.draft_id = draft_id
    return f"{shown}; draft {draft_id}"


def check_text_review(ctx: Context) -> str | None:
    if STATE.draft_id is None:
        raise CheckFailed("no draft: check cv_upload did not pass")
    body = poll(
        ctx,
        f"/api/profile/drafts/{STATE.draft_id}",
        draft_reached("text_review"),
        within_s=TEXT_REVIEW_WITHIN_S,
        what="text_review",
    )
    counts = [(category, pick(body, "strip_report", category)) for category in STRIP_CATEGORIES]
    shown = "strip_report " + " ".join(f"{category}={n!r}" for category, n in counts)
    email = pick(body, "strip_report", "email")
    if not isinstance(email, int) or email < 1:
        raise CheckFailed(f"expected at least one e-mail address stripped; {shown}")
    return shown


def check_extract_ready(ctx: Context) -> str | None:
    if STATE.draft_id is None:
        raise CheckFailed("no draft: check cv_upload did not pass")
    path = f"/api/profile/drafts/{STATE.draft_id}"
    response = ctx.client.post(f"{ORIGIN}{path}/text-review", headers=mutating(ctx, APPROVE_KEY))
    json_object(response, 202)
    body = poll(ctx, path, draft_reached("ready"), within_s=EXTRACT_WITHIN_S, what="ready")
    issues = pick(body, "validation_issues")
    codes = sorted({str(pick(i, "code")) for i in issues}) if isinstance(issues, list) else []
    return f"validation issue codes={codes}"


def check_confirm(ctx: Context) -> str | None:
    if STATE.draft_id is None:
        raise CheckFailed("no draft: check cv_upload did not pass")
    response = ctx.client.post(
        f"{ORIGIN}/api/profile/drafts/{STATE.draft_id}/confirm",
        json={"expected_rescore_ack": True},
        headers=mutating(ctx, CONFIRM_KEY),
    )
    body = json_object(response, 201)
    version_id, targets = pick(body, "profile_version_id"), pick(body, "targets_to_create")
    if not isinstance(version_id, int):
        raise CheckFailed(f"expected a profile version id, got {type(version_id).__name__}")
    listed = json_object(ctx.client.get(f"{ORIGIN}/api/profile", headers=ctx.auth()), 200)
    active_id, version_no = pick(listed, "active", "id"), pick(listed, "active", "version_no")
    if (active_id, version_no) != (version_id, 1):
        raise CheckFailed(
            f"expected version {version_id} active as version_no 1, got {active_id!r}/{version_no!r}"
        )
    STATE.version_id = version_id
    return f"version {version_id} active as version_no 1; targets_to_create={targets!r}"


def check_proposal_ready(ctx: Context) -> str | None:
    if STATE.version_id is None:
        raise CheckFailed("no confirmed version: check confirm did not pass")
    count, proposal_id = query_row(PROPOSALS_OF.format(version_id=int(STATE.version_id)), 2)
    if count != "1" or not proposal_id.isdigit():
        raise CheckFailed(f"expected the confirm's one proposal, found {count!r}")

    def done(body: Any) -> tuple[bool, str]:
        status = pick(body, "status")
        if status in ("failed", "purged"):
            raise CheckFailed(f"proposal {proposal_id} ended {status!r}")
        return status == "ready", f"status={status!r}"

    body = poll(
        ctx,
        f"/api/profile/query-proposals/{proposal_id}",
        done,
        within_s=PROPOSAL_WITHIN_S,
        what=f"proposal {proposal_id} ready",
    )
    items = pick(body, "items")
    if not isinstance(items, list) or len(items) not in PROPOSAL_ITEMS:
        found = len(items) if isinstance(items, list) else None
        raise CheckFailed(f"expected 6 to 16 items, got {found!r}")
    STATE.proposal_id, STATE.items = int(proposal_id), items
    return f"proposal {proposal_id}: {len(items)} items"


def check_settings_save(ctx: Context) -> str | None:
    if STATE.proposal_id is None or STATE.items is None:
        raise CheckFailed("no ready proposal: check proposal_ready did not pass")
    query_ids = []
    for item in STATE.items[:ENABLED_QUERIES]:
        accepted = {
            "text": pick(item, "text"),
            "lang": pick(item, "lang"),
            "proposal_id": STATE.proposal_id,
            "exploration": pick(item, "exploration"),
        }
        created = json_object(
            ctx.client.post(f"{ORIGIN}/api/queries", json=accepted, headers=mutating(ctx)), 201
        )
        query_ids.append(pick(created, "id"))
    active = json_object(ctx.client.get(f"{ORIGIN}/api/settings", headers=ctx.auth()), 200)
    payload, base = pick(active, "payload"), pick(active, "version", "id")
    if not isinstance(payload, dict) or not isinstance(base, int):
        raise CheckFailed("the active settings have no payload or version id")
    payload["queries"] = [{"query_id": query_id, "enabled": True} for query_id in query_ids]
    preview = json_object(
        ctx.client.post(f"{ORIGIN}/api/settings/dry-run", json={"payload": payload}, headers=mutating(ctx)),
        200,
    )
    errors, warnings = pick(preview, "errors"), pick(preview, "warnings")
    if not isinstance(errors, list) or not isinstance(warnings, list):
        raise CheckFailed("the dry-run answered no errors and warnings lists")
    if errors:
        raise CheckFailed(
            f"the dry-run refused the payload: {sorted({str(pick(e, 'code')) for e in errors})}"
        )
    codes = [str(pick(w, "code")) for w in warnings]
    saved = json_object(
        ctx.client.post(
            f"{ORIGIN}/api/settings",
            json={"payload": payload, "base_version_id": base, "acknowledged_warnings": codes},
            headers=mutating(ctx, SAVE_KEY),
        ),
        201,
    )
    version_no, classes = pick(saved, "version", "version_no"), pick(saved, "version", "effect_classes")
    return (
        f"queries {query_ids} enabled; version_no={version_no!r} effect_classes={classes!r} warnings={codes}"
    )


def usd(body: Any, *path: str) -> Decimal:
    value = pick(body, *path)
    try:
        return Decimal(str(value))
    except InvalidOperation:
        raise CheckFailed(f"{'.'.join(path)} is no amount") from None


def check_spend(ctx: Context) -> str | None:
    spend = json_object(ctx.client.get(f"{ORIGIN}/api/system/spend", headers=ctx.auth()), 200)
    settings = json_object(ctx.client.get(f"{ORIGIN}/api/settings", headers=ctx.auth()), 200)
    counted, cap = usd(spend, "counted"), usd(spend, "cap")
    buffer = usd(settings, "payload", "llm", "overrun_buffer_usd")
    found = f"counted=${counted} cap=${cap} buffer=${buffer}"
    if not Decimal(0) < counted <= cap - buffer:
        raise CheckFailed(f"expected 0 < counted <= cap - buffer; {found}")
    return found


CHECKS: Final[list[tuple[str, Callable[[Context], str | None]]]] = [
    ("user_create", check_user_create),
    ("login", check_login),
    ("health_ready", check_health_ready),
    ("cv_upload", check_cv_upload),
    ("text_review", check_text_review),
    ("extract_ready", check_extract_ready),
    ("confirm", check_confirm),
    ("proposal_ready", check_proposal_ready),
    ("settings_save", check_settings_save),
    ("spend", check_spend),
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
