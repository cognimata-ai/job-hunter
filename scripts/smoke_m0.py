"""M0 acceptance smoke test (Task 21): the dev stack, the CLI and the API, end to end.

    docker compose --env-file deploy/images.lock -f deploy/compose.dev.yaml down -v
    docker compose --env-file deploy/images.lock -f deploy/compose.dev.yaml up -d --build --wait
    uv run python scripts/smoke_m0.py

The precondition is a FRESH dev stack (ruling T21-a): the script creates the one user, so a stack that has one
already fails the first check with the hint above. The script never deletes anything.

It drives `deploy/compose.dev.yaml` (always with `--env-file deploy/images.lock`) with `subprocess` and
`httpx`, through the canonical dev origin `http://localhost:8080`. Six checks run in order and each prints
exactly one JSON line, `{"check": <name>, "ok": <bool>}` with an optional `"detail"`:

1. `user_create`: `jobhunter user create --password-stdin` in a one-off api container;
2. `login`: `POST /api/auth/login` answers 204 with the session cookie;
3. `health_ready`: `GET /api/system/health` reports `ready.ok` within 60 s;
4. `slot_due`: a schedule slot is made due with `psql` (dev only);
5. `scheduled_run`: the worker's tick queues it and the run succeeds (`last_run`, trigger `schedule`) within
   150 s, and exactly one `missed` run exists;
6. `run_now_idempotent`: `POST /api/runs` twice with one `Idempotency-Key` answers two identical 202 bodies.

It stops at the first failed check (exit 1); after six `ok` lines it exits 0. The password, the session cookie
and every `Set-Cookie` value are never printed: the cookie travels as an explicit `Cookie` header, because
httpx does not send a `Secure` cookie over plain HTTP (this proves the backend session only; the browser
session is M4's). The script prints nothing but those JSON lines: the output of the commands it runs is
captured, and only a failure's `detail` can quote some of it (an exit status, a line of its output). That
`detail` passes through `Context.scrub`, which replaces the exact password, cookie and token strings, and no
other form of them; the rest of each line is fixed text.
"""

import json
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import httpx

REPO: Final = Path(__file__).resolve().parents[1]
DC: Final = [
    "docker",
    "compose",
    "--env-file",
    str(REPO / "deploy/images.lock"),
    "-f",
    str(REPO / "deploy/compose.dev.yaml"),
]
FRESH_HINT: Final = (
    "stack not fresh: run `docker compose --env-file deploy/images.lock "
    "-f deploy/compose.dev.yaml down -v` first"
)
# What `jobhunter user create` says when the single user exists (cli_user.py); other errors can also say
# "already exists" (a docker network or container), and those are reported as they are.
USER_EXISTS_MESSAGE: Final = "a user already exists"

ORIGIN: Final = "http://localhost:8080"
USERNAME: Final = "smoke"
PASSWORD: Final = "Tannenbaum-Ölfass-7"  # noqa: S105 - the fixed credential of the dev smoke user (plan, Task 21)
SESSION_COOKIE: Final = "__Host-jh_session"
IDEMPOTENCY_KEY: Final = "smoke-run-now-0001"

# Every subprocess and every httpx call has a timeout, so a hung docker call or request fails a check
# instead of hanging the run.
DOCKER_TIMEOUT_S: Final = 300  # a one-off container may start its dependencies
PSQL_TIMEOUT_S: Final = 60
HTTP_TIMEOUT_S: Final = 15.0
LOGIN_TIMEOUT_S: Final = 30.0  # one Argon2 verification
READY_WITHIN_S: Final = 60
SCHEDULED_RUN_WITHIN_S: Final = 150
POLL_INTERVAL_S: Final = 2.0

# psql as the bootstrap role (unaligned, tuples only, stop at the first error) and the CLI call of check 1.
PSQL: Final = ["psql", "-U", "jobhunter_bootstrap", "-d", "jobhunter", "-v", "ON_ERROR_STOP=1", "-At"]
USER_CREATE: Final = ["jobhunter", "user", "create", "--username", USERNAME, "--password-stdin"]

UPDATE_SLOT_DUE: Final = (
    "UPDATE schedule_state SET anchor_utc = date_trunc('minute', now()), interval_s = 3600, "
    "last_evaluated_slot = date_trunc('minute', now()) - interval '2 hours'"
)
COUNT_MISSED: Final = "SELECT count(*) FROM runs WHERE status = 'missed'"


class CheckFailed(Exception):
    """A check's expectation did not hold; the message becomes the `detail` of its JSON line."""


@dataclass
class Context:
    client: httpx.Client
    cookie: str | None = field(default=None, repr=False)  # `__Host-jh_session=<token>`: a secret
    secrets: list[str] = field(default_factory=lambda: [PASSWORD], repr=False)

    def scrub(self, text: str) -> str:
        """`text` without the password, the session cookie or its token."""
        for secret in self.secrets:
            text = text.replace(secret, "[redacted]")
        return text

    def auth(self) -> dict[str, str]:
        if self.cookie is None:
            raise CheckFailed("no session cookie: the login did not run")
        return {"Cookie": self.cookie}


def emit(ctx: Context, check: str, ok: bool, detail: str | None = None) -> None:
    line: dict[str, Any] = {"check": check, "ok": ok}
    if detail is not None:
        line["detail"] = ctx.scrub(detail)
    print(json.dumps(line), flush=True)


def stderr_tail(process: subprocess.CompletedProcess[str], lines: int = 3) -> str:
    return " | ".join(process.stderr.strip().splitlines()[-lines:])[:400]


def psql(sql: str) -> subprocess.CompletedProcess[str]:
    """`sql` as the bootstrap role in the postgres container (dev only)."""
    return subprocess.run(
        [*DC, "exec", "-T", "postgres", *PSQL, "-c", sql],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=PSQL_TIMEOUT_S,
    )


def pick(body: Any, *path: str) -> Any:
    """`body[path[0]][path[1]]…`, or `None` when a step is missing or not an object."""
    for key in path:
        if not isinstance(body, dict):
            return None
        body = body.get(key)
    return body


def poll_health(ctx: Context, done: Callable[[Any], tuple[bool, str]], *, within_s: int, what: str) -> None:
    """Poll `GET /api/system/health` until `done(body)` says so, or `within_s` seconds have passed."""
    deadline = time.monotonic() + within_s
    last = "no answer yet"
    while True:
        try:
            response = ctx.client.get(f"{ORIGIN}/api/system/health", headers=ctx.auth())
        except httpx.HTTPError as exc:
            last = type(exc).__name__
        else:
            if response.status_code in (401, 403):
                raise CheckFailed(f"the session was refused (HTTP {response.status_code})")
            if response.status_code == 200:
                body = response.json()
                finished, last = done(body)
                if finished:
                    return
            else:
                last = f"HTTP {response.status_code}"
        if time.monotonic() >= deadline:
            raise CheckFailed(f"{what} not reached within {within_s} s (last: {last})")
        time.sleep(POLL_INTERVAL_S)


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


def check_login(ctx: Context) -> str | None:
    response = ctx.client.post(
        f"{ORIGIN}/api/auth/login",
        json={"username": USERNAME, "password": PASSWORD},
        headers={"Origin": ORIGIN},
        timeout=LOGIN_TIMEOUT_S,
    )
    if response.status_code != 204:
        raise CheckFailed(f"expected 204, got {response.status_code}")
    for set_cookie in response.headers.get_list("set-cookie"):
        pair = set_cookie.split(";", 1)[0].strip()
        name, _, token = pair.partition("=")
        if name == SESSION_COOKIE and token:
            ctx.cookie = pair
            ctx.secrets += [pair, token]
            return None
    raise CheckFailed(f"no {SESSION_COOKIE} cookie in the answer")


def check_health_ready(ctx: Context) -> str | None:
    def done(body: Any) -> tuple[bool, str]:
        ok = pick(body, "ready", "ok")
        return ok is True, f"ready.ok={ok!r} failing={pick(body, 'ready', 'failing')!r}"

    poll_health(ctx, done, within_s=READY_WITHIN_S, what="ready.ok true")
    return None


def check_slot_due(ctx: Context) -> str | None:
    process = psql(UPDATE_SLOT_DUE)
    if process.returncode != 0 or process.stdout.strip() != "UPDATE 1":
        raise CheckFailed(f"exit {process.returncode}, {process.stdout.strip()!r}: {stderr_tail(process)}")
    return None


def check_scheduled_run(ctx: Context) -> str | None:
    def done(body: Any) -> tuple[bool, str]:
        status, trigger = pick(body, "last_run", "status"), pick(body, "last_run", "trigger")
        return (
            status == "succeeded" and trigger == "schedule",
            f"last_run status={status!r} trigger={trigger!r}",
        )

    poll_health(ctx, done, within_s=SCHEDULED_RUN_WITHIN_S, what="a succeeded scheduled run")
    process = psql(COUNT_MISSED)
    if process.returncode != 0:
        raise CheckFailed(f"exit {process.returncode}: {stderr_tail(process)}")
    if process.stdout.strip() != "1":
        raise CheckFailed(f"expected exactly 1 missed run, found {process.stdout.strip()!r}")
    return "succeeded; 1 missed"


def check_run_now_idempotent(ctx: Context) -> str | None:
    headers = {
        **ctx.auth(),
        "Origin": ORIGIN,
        "X-JH-Request": "1",
        "Idempotency-Key": IDEMPOTENCY_KEY,
    }
    answers = [
        ctx.client.post(f"{ORIGIN}/api/runs", json={"include_paid": False}, headers=headers) for _ in range(2)
    ]
    statuses = [answer.status_code for answer in answers]
    if statuses != [202, 202]:
        raise CheckFailed(f"expected 202 twice, got {statuses}")
    if answers[0].content != answers[1].content:
        raise CheckFailed("the two answers differ")
    return None


CHECKS: Final[list[tuple[str, Callable[[Context], str | None]]]] = [
    ("user_create", check_user_create),
    ("login", check_login),
    ("health_ready", check_health_ready),
    ("slot_due", check_slot_due),
    ("scheduled_run", check_scheduled_run),
    ("run_now_idempotent", check_run_now_idempotent),
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
