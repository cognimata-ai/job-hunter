"""M5 acceptance smoke test (plan M5 Task 33): the production compose on the compose test stack, end to end,
from a fresh stack to a restored second site that deletes everything.

    uv run python scripts/smoke_m5.py

It drives the three projects of `tests/support/prodstack.py`: `PT` (site A, `jobhunter-prodtest`), `PB` (the
restore target, site B, `jobhunter-prodtest-b`) and `S3` (the S3 stand-in, `jobhunter-s3stub`). Its
preconditions are the stack modules': the images they build (`jobhunter-app:dev`, `jobhunter-backup:dev`,
`jobhunter-docparse:dev` and the embedder's fake target `jobhunter-embedder:e2e`; the script builds nothing),
the dummy secrets of `.secrets/test` (`sudo scripts/test-secrets.sh`, which the script never runs), and no
other stack on 172.30.0.0/24 (the dev stack: `$DC down` first). It makes the stack fresh itself: whatever a
killed run left of the three projects is removed with its volumes first. No paid call and no external service:
the worker is on the fixtures and the recorded Claude streams (checked before the paid run), ntfy and
healthchecks are `stubsvc`, S3 is versitygw.

The browser is the script's own `httpx.Client` of the site's Caddy (`https://localhost:8443`, verified against
the site's internal root, `caddy_root_ca`), sending `Origin` and `X-JH-Request: 1` with every request, in a
`smoke_m0.Context` for the redaction; the session travels as an explicit `Cookie` header. Each site's backup
commands run in one-off `backup` containers (`run --rm --no-deps`), and its backup scheduler never runs: the
smoke ships by hand, so a deletion's `completed` state is seen before its shipment (`SERVICES`). Thirteen
checks run in order and each prints exactly one JSON line, `{"check": <name>, "ok": <bool>}` with an optional
`"detail"`:

1. `stack_up`: the three projects removed with their volumes (a killed run's leftovers), then `s3_up()`, then
   `$PT up -d --no-build --wait` of `SERVICES`; every role passes `jobhunter startup-check --role` in a
   one-off container of its service (`ROLES`);
2. `tls`: Caddy serves TLS verified against its internal root; `/healthz` has §16.6's headers once each
   (`SECURITY_HEADERS`), no `Server` and no `Alt-Svc`; the session route without a cookie answers 401 and
   clears the `__Host-jh_session` cookie with `Secure`, `HttpOnly`, `SameSite=Strict`, `Path=/` and no
   `Domain`;
3. `user`: `$PT run --rm -T api jobhunter user create --username smoke --password-stdin` (the password on
   stdin, which only `JOBHUNTER_ENV=prod` refuses); a login over TLS answers 204 with that cookie's
   attributes, and the session names the user;
4. `collect_and_profile`: the worker must be on both fakes (`FAKE_VARS`, compared, never printed); the smoke
   inputs (`smoke_m1.SEED_INPUTS`); a paid run (`include_paid: true`) that succeeds on the fixtures with
   Adzuna postings; the synthetic CV (`tests/support/profile.py`) through the profile flow to a confirmed
   version, active as `version_no` 1;
5. `alert_dispatch`: a warn alert (`ALERT_CODE`) inserted with `psql`; within 60 s stubsvc recorded its push:
   `Title: Job Hunter - <code>`, `Priority: 3`, `Tags: warn`, the `Click` URL of the alerts list and an
   `Authorization` header; and the row's `last_notified_at` is set;
6. `deadman`: a finished `schedule` run inserted (`INSERT_RUN`: a slot in the past that no run holds, started
   and finished, the active settings, scope and tuple, `succeeded`); within 90 s stubsvc recorded a new
   `GET /hc/ping/<uuid>` and the watermark `deadman_state.last_pinged_run_id` passed the run; `jobhunter ops
   deadman-sync --show` prints the schedule's timeout and grace (`grace_s`), unpaused, which stubsvc's check
   holds;
7. `backup_set`: `backup init` creates the repository on the fresh stand-in; `backup set` → its row
   `succeeded`, its restic snapshot listed in the S3 stand-in under the tag `set:<id>`;
8. `source_purge`: `POST /api/privacy/purge-source` with the phrase and the password → 202; within
   `PURGE_WITHIN_S` the manifest is `completed`; no Adzuna posting is left; the `deletion_unshipped` banner
   shows;
9. `manifest_shipped`: `backup ship`; within 120 s the manifest is `shipped` and the banner is gone;
10. `restore`: `$PT down -v`; `$PB up -d postgres`; `$PB --profile restore run --rm restore` restores the set
    of check 7 with both verify phases passing; the purge's manifest is replayed on B (an entry in its
    `replay_log`) and no Adzuna posting is there; `$PB up -d --wait` of `SERVICES`; a login on B works;
11. `export`: on B an export → `ready` → its zip, downloaded, names `README.txt` (only its entry count and
    size are printed);
12. `delete_everything`: on B a wrong phrase → 422 `confirmation_mismatch`; the right one with the password →
    202; the manifest `completed` (read with `psql`: the completion ends the session); `backup ship`; within
    120 s `shipped`; the old session answers 401; a new login works; the jobs list is empty; the user is kept;
13. `teardown`: `PT` and `PB` removed with their volumes, profiled services and orphans, then the stand-in.

A failed check ends the run (exit 1), but the teardown runs whatever happened before it, after a failure, a
data error or an interrupt too (`main`), and prints its own line; after thirteen `ok` lines the script exits
0. The script prints nothing but those JSON lines: what the commands print is captured, and a failure's
`detail` quotes only a command's exit status and the tail of its stderr, codes, statuses, ids and counts,
passed through `Context.scrub` (the password, every session cookie and token); a command's stderr and a
helper's failure are scrubbed before they are cut (`tail`, `helper_detail`), so a cut never leaves a part of
a secret. Never a key (the `Idempotency-Key`s, the data key ids, the dead-man check's uuid), an ad's text, a
CV or an export's content.
"""

import io
import json
import re
import ssl
import subprocess
import sys
import time
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import httpx
import pytest
from smoke_m0 import (  # scripts/ is the script's directory, the first entry of sys.path
    PASSWORD,
    USERNAME,
    CheckFailed,
    Context,
    emit,
    stderr_tail,
)
from smoke_m1 import SEED_INPUTS

REPO: Final = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:  # `tests` is a package of the repository root, not of scripts/
    sys.path.insert(1, str(REPO))

from tests.support.prodstack import (  # noqa: E402 - after the repository root is on sys.path
    CADDY_ROOT,
    ORIGIN,
    PB,
    PT,
    caddy_root_ca,
    other_stack_up,
    psql,
    s3_down,
    s3_up,
    stub_log,
)
from tests.support.profile import SYNTHETIC_CV, minimal_pdf  # noqa: E402

# A helper of tests/support/prodstack.py that fails calls `pytest.fail`, whose exception is no `Exception`.
HelperFailed: Final = pytest.fail.Exception

# The stack the smoke runs on each site: compose.test.yaml's services without `backup`, whose scheduler
# would ship every minute (the smoke ships by hand); their dependencies (migrate, postgres, embedder) start
# with them.
SERVICES: Final = ("caddy", "api", "worker", "docparse", "stubsvc")
# Each role of `config.Role`, checked in a one-off container of the service of its name.
ROLES: Final = ("api", "worker", "migrate", "backup", "restore")
# compose.test.yaml sets both on the worker: the fixtures and the recorded Claude streams.
FAKE_VARS: Final = ("JOBHUNTER_FAKE_SOURCES", "JOBHUNTER_FAKE_LLM")
USER_CREATE: Final = ["jobhunter", "user", "create", "--username", USERNAME, "--password-stdin"]

# Check 2: `api.edge.SECURITY_HEADERS` (§16.6) and the session cookie (`auth.sessions`), pinned by the test.
SECURITY_HEADERS: Final = {
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
        "form-action 'self'; object-src 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), interest-cohort=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}
SESSION_COOKIE: Final = "__Host-jh_session"
COOKIE_ATTRIBUTES: Final = frozenset({"Secure", "HttpOnly", "SameSite=Strict", "Path=/"})

# Check 5: a warn code of the catalogue that is pushed and that nothing resolves on its own (pinned by the
# test), on a subject of its own; the push's headers (`observability.ntfy`).
ALERT_CODE: Final = "api.unhandled_exception"
ALERT_TITLE: Final = f"Job Hunter - {ALERT_CODE}"
ALERT_CLICK: Final = f"{ORIGIN}/settings#alerts"
INSERT_ALERT: Final = (
    "/* smoke_m5.alert */ WITH a AS (INSERT INTO alerts (code, severity, dedup_key, message) VALUES "
    "('api.unhandled_exception', 'warn', 'api.unhandled_exception:smoke_m5', "
    "'smoke m5: a push of the alert dispatcher') RETURNING id) SELECT id FROM a"
)
ALERT_NOTIFIED: Final = (
    "/* smoke_m5.notified */ SELECT last_notified_at IS NOT NULL FROM alerts WHERE id = {alert}"
)

# Check 6: a finished scheduled run, valid under the schema (a `schedule` run has a slot, `runs_slot_uq`),
# dated 30 days back so it is neither the newest run nor the freshest success; the dead-man's watermark and
# the schedule; `deadman.grace_s` (pinned by the test); a ping and a read of stubsvc's check by its uuid.
INSERT_RUN: Final = """\
/* smoke_m5.run */ WITH s AS (SELECT date_trunc('minute', now()) - interval '30 days 7 minutes' AS slot),
r AS (INSERT INTO runs (trigger, scheduled_slot, status, include_paid, settings_version_id, scope_version_id,
    llm_tuple_id, queued_at, started_at, finished_at)
  SELECT 'schedule', s.slot, 'succeeded', false, a.settings_version_id, a.scope_version_id, a.llm_tuple_id,
    s.slot, s.slot + interval '1 minute', s.slot + interval '2 minutes'
  FROM active_config a CROSS JOIN s
  WHERE a.id = 1 AND NOT EXISTS (SELECT 1 FROM runs x WHERE x.kind = 'collect' AND x.scheduled_slot = s.slot)
  RETURNING id)
SELECT id FROM r"""
WATERMARK: Final = (
    "/* smoke_m5.watermark */ SELECT coalesce(last_pinged_run_id, 0) FROM deadman_state WHERE id = 1"
)
SCHEDULE: Final = "/* smoke_m5.schedule */ SELECT interval_s, enabled FROM schedule_state WHERE id = 1"
GRACE_MIN_S: Final = 3_600
GRACE_MAX_S: Final = 43_200
PING: Final = re.compile(r"/hc/ping/([0-9a-f-]{36})")
STUB_CHECK: Final = (
    "import sys, urllib.request; sys.stdout.write(urllib.request.urlopen("
    "'http://127.0.0.1:8099/hc/api/v3/checks/' + sys.argv[1]).read().decode())"
)

# Check 7: the set's row, and the repository's snapshots of the tags given, as `Restic.snapshots` lists them
# in a backup container (ids, tags and paths only; tests/stack/test_prod_backup.py's reader).
BACKUP_SET: Final = (
    "/* smoke_m5.set */ SELECT status, coalesce(restic_snapshot_id, '') FROM backup_sets WHERE id = {set_id}"
)
SNAPSHOTS: Final = """
import asyncio, json, sys
from jobhunter.backup.restic import Restic
from jobhunter.backup.runner import SubprocessRunner
from jobhunter.config import load_config
infos = asyncio.run(Restic(load_config("backup"), SubprocessRunner()).snapshots(tags=sys.argv[1:]))
print(json.dumps([{"id": i.id, "tags": list(i.tags), "paths": list(i.paths)} for i in infos]))
"""

# Checks 8-12: the routes' phrases (`api.routes.privacy`, pinned by the test), the banner, the manifests.
PURGE_PHRASE: Final = "PURGE ADZUNA"
DELETE_PHRASE: Final = "DELETE EVERYTHING"
WRONG_PHRASE: Final = "DELETE EVERYTHING NOW"
CONFIRMATION_MISMATCH: Final = "confirmation_mismatch"
UNSHIPPED_BANNER: Final = "deletion_unshipped"
ADZUNA_POSTINGS: Final = (
    "/* smoke_m5.adzuna */ SELECT count(*) FROM postings p JOIN source_instances s "
    "ON s.id = p.source_instance_id WHERE s.source_id = 'adzuna'"
)
RUN_STATUS: Final = "/* smoke_m5.run_status */ SELECT status FROM runs WHERE id = {run_id}"
MANIFEST: Final = (
    "/* smoke_m5.manifest */ SELECT status, manifest_uuid FROM deletion_manifests WHERE id = {manifest_id}"
)
REPLAY_ENTRIES: Final = (
    "/* smoke_m5.replayed */ SELECT count(*) FROM deletion_manifests m "
    "CROSS JOIN LATERAL jsonb_array_elements(coalesce(m.replay_log, '[]')) e "
    "WHERE m.manifest_uuid = '{manifest_uuid}'"
)
USERS: Final = "/* smoke_m5.users */ SELECT count(*) FROM users"
UUID: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
README: Final = "README.txt"

TERMINAL: Final = frozenset({"succeeded", "partial", "failed", "cancelled"})
HTTP_TIMEOUT_S: Final = 30.0
DOCKER_TIMEOUT_S: Final = 300.0  # a one-off container
UP_TIMEOUT_S: Final = 900.0
DOWN_TIMEOUT_S: Final = 300.0
BACKUP_TIMEOUT_S: Final = 900.0
RESTORE_TIMEOUT_S: Final = 1200.0
CADDY_WITHIN_S: Final = 60.0
COLLECT_WITHIN_S: Final = 900.0
DRAFT_WITHIN_S: Final = 180.0
PUSH_WITHIN_S: Final = 60.0
PING_WITHIN_S: Final = 90.0
PURGE_WITHIN_S: Final = 600.0
SHIP_WITHIN_S: Final = 120.0
EXPORT_WITHIN_S: Final = 180.0
POLL_S: Final = 2.0
HEAD_CHARS: Final = 160  # of a helper's failure (`helper_detail`)
DETAIL_CHARS: Final = 400


@dataclass
class State:
    """What the checks hand on."""

    set_id: int | None = None
    purge_id: int | None = None
    purge_uuid: str | None = None


STATE: Final = State()


# ---- helpers -----------------------------------------------------------------------------------------------


def compose(
    argv: list[str], *args: str, stdin: str | None = None, timeout: float = DOCKER_TIMEOUT_S
) -> subprocess.CompletedProcess[str]:
    """`argv` with `args`, its output captured; never raises for an exit status."""
    return subprocess.run(
        [*argv, *args],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=timeout,
    )


def tail(ctx: Context, done: subprocess.CompletedProcess[str]) -> str:
    """`smoke_m0.stderr_tail` of `done`, its stderr scrubbed first: its cut can then never leave a part of a
    secret (`emit` scrubs only the cut text)."""
    scrubbed = subprocess.CompletedProcess(done.args, done.returncode, done.stdout, ctx.scrub(done.stderr))
    return stderr_tail(scrubbed)


def succeeded(
    ctx: Context, done: subprocess.CompletedProcess[str], what: str
) -> subprocess.CompletedProcess[str]:
    if done.returncode != 0:
        raise CheckFailed(f"{what}: exit {done.returncode}: {tail(ctx, done)}")
    return done


def last_json(stdout: str) -> Any:
    """The JSON line a command printed last on stdout."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        raise CheckFailed("the command printed nothing")
    return json.loads(lines[-1])


def events(stderr: str) -> list[dict[str, Any]]:
    """The JSON log events a command wrote to stderr (lines that are no JSON object are compose's own)."""
    found = []
    for line in stderr.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and "event" in event:
            found.append(event)
    return found


def one(argv: list[str], sql: str, columns: int = 1) -> list[str]:
    """The one row of `sql` (`psql -AtX`: `|`-separated), checked to have `columns` fields."""
    lines = psql(argv, sql).strip().splitlines()
    fields = lines[0].split("|") if len(lines) == 1 else []
    if len(fields) != columns:
        raise CheckFailed(f"expected one row of {columns} fields, got {len(lines)} line(s)")
    return fields


def count(argv: list[str], sql: str) -> int:
    (value,) = one(argv, sql)
    return int(value)


def poll(probe: Callable[[], tuple[bool, str]], *, within_s: float, what: str) -> str:
    """`probe()` every `POLL_S` until it says done, within `within_s`; what it said last."""
    deadline = time.monotonic() + within_s
    while True:
        done, last = probe()
        if done:
            return last
        if time.monotonic() >= deadline:
            raise CheckFailed(f"{what} not within {within_s:g} s (last: {last})")
        time.sleep(POLL_S)


def fresh_key() -> str:
    """A new `Idempotency-Key`: never printed."""
    return f"smoke-m5-{uuid.uuid4().hex}"


def problem_code(response: httpx.Response) -> object:
    try:
        body = response.json()
    except ValueError:
        return None
    return body.get("code") if isinstance(body, dict) else None


def answer(response: httpx.Response, expected: int, what: str) -> dict[str, Any]:
    """The JSON object of `response`, whose status must be `expected` (only its status and code otherwise)."""
    if response.status_code != expected:
        raise CheckFailed(
            f"{what}: expected {expected}, got {response.status_code} (code {problem_code(response)!r})"
        )
    body = response.json()
    if not isinstance(body, dict):
        raise CheckFailed(f"{what}: expected a JSON object, got {type(body).__name__}")
    return body


def get(ctx: Context, path: str, **kwargs: Any) -> httpx.Response:
    return ctx.client.get(path, headers=ctx.auth(), **kwargs)


def post(ctx: Context, path: str, **kwargs: Any) -> httpx.Response:
    """A state-changing request: the session and a new `Idempotency-Key` (`Origin` and `X-JH-Request` are the
    client's own headers)."""
    return ctx.client.post(path, headers={**ctx.auth(), "Idempotency-Key": fresh_key()}, **kwargs)


def site_client(argv: list[str]) -> httpx.Client:
    """A TLS client of the site's Caddy, verified against its internal root (public), once Caddy made it."""
    poll(
        lambda: (compose(argv, "exec", "-T", "caddy", "test", "-f", CADDY_ROOT).returncode == 0, "absent"),
        within_s=CADDY_WITHIN_S,
        what="Caddy's internal root",
    )
    root = caddy_root_ca(argv)
    try:
        context = ssl.create_default_context(cafile=str(root))
    finally:
        root.unlink()
    return httpx.Client(
        base_url=ORIGIN,
        verify=context,
        timeout=HTTP_TIMEOUT_S,
        trust_env=False,  # the target is the loopback Caddy; no proxy variable of the calling shell applies
        headers={"Origin": ORIGIN, "X-JH-Request": "1"},
    )


def connect(ctx: Context, argv: list[str]) -> None:
    """The site's client in `ctx` (the one before closed), once `/healthz` answers 200 through Caddy."""
    ctx.client.close()
    ctx.client = site_client(argv)
    ctx.cookie = None

    def healthy() -> tuple[bool, str]:
        try:
            status = ctx.client.get("/healthz").status_code
        except httpx.TransportError as exc:
            return False, type(exc).__name__
        return status == 200, f"HTTP {status}"

    poll(healthy, within_s=CADDY_WITHIN_S, what="/healthz through Caddy")


def cookie_problems(set_cookie: str) -> list[str]:
    """What the session cookie's `Set-Cookie` lacks: its name, `COOKIE_ATTRIBUTES`, no `Domain`."""
    pair, *rest = set_cookie.split(";")
    attributes = {part.strip() for part in rest}
    problems = [] if pair.split("=", 1)[0].strip() == SESSION_COOKIE else [f"not {SESSION_COOKIE}"]
    problems += [f"no {name}" for name in sorted(COOKIE_ATTRIBUTES - attributes)]
    problems += ["a Domain"] if any(part.lower().startswith("domain") for part in attributes) else []
    return problems


def session_cookie(response: httpx.Response) -> str:
    """The `Set-Cookie` of the session cookie in `response`; `CheckFailed` without one."""
    for set_cookie in response.headers.get_list("set-cookie"):
        if set_cookie.split("=", 1)[0].strip() == SESSION_COOKIE:
            return set_cookie
    raise CheckFailed(f"no {SESSION_COOKIE} cookie in the answer")


def login(ctx: Context) -> None:
    """`POST /api/auth/login` → 204 with the session cookie, whose attributes are checked; the cookie and its
    token are kept in `ctx` and scrubbed from every detail."""
    response = ctx.client.post("/api/auth/login", json={"username": USERNAME, "password": PASSWORD})
    if response.status_code != 204:
        raise CheckFailed(
            f"login: expected 204, got {response.status_code} (code {problem_code(response)!r})"
        )
    set_cookie = session_cookie(response)
    pair = set_cookie.split(";", 1)[0].strip()
    token = pair.partition("=")[2]
    if token:
        ctx.secrets += [pair, token]
    if problems := cookie_problems(set_cookie):
        raise CheckFailed(f"the login's cookie: {', '.join(problems)}")
    if not token:
        raise CheckFailed("the login's cookie holds no token")
    ctx.cookie = pair


def session_user(ctx: Context) -> object:
    return answer(get(ctx, "/api/auth/session"), 200, "the session").get("username")


def banners(ctx: Context) -> list[object]:
    found = answer(get(ctx, "/api/system/status"), 200, "the status").get("banners")
    if not isinstance(found, list):
        raise CheckFailed("the status has no banners list")
    return [banner.get("code") if isinstance(banner, dict) else None for banner in found]


def manifest_status(ctx: Context, manifest_id: int) -> str:
    """A deletion's status as the API shows it; `CheckFailed` once it `failed`."""
    status = str(answer(get(ctx, f"/api/privacy/manifests/{manifest_id}"), 200, "the manifest").get("status"))
    if status == "failed":
        raise CheckFailed(f"manifest {manifest_id} failed")
    return status


def backup_cli(ctx: Context, argv: list[str], command: str) -> Any:
    """`jobhunter backup <command>` in a one-off backup container of the site; what it printed."""
    done = compose(
        argv,
        "run",
        "--rm",
        "--no-deps",
        "-T",
        "backup",
        "jobhunter",
        "backup",
        command,
        timeout=BACKUP_TIMEOUT_S,
    )
    return last_json(succeeded(ctx, done, f"backup {command}").stdout)


def adzuna_postings(argv: list[str]) -> int:
    return count(argv, ADZUNA_POSTINGS)


def manifest_row(argv: list[str], manifest_id: int) -> tuple[str, str]:
    status, manifest_uuid = one(argv, MANIFEST.format(manifest_id=int(manifest_id)), 2)
    if UUID.fullmatch(manifest_uuid) is None:
        raise CheckFailed(f"manifest {manifest_id} has no uuid")
    return status, manifest_uuid


def grace_s(interval_s: int) -> int:
    """`deadman.grace_s`: a quarter of the interval, within 1 h and 12 h (§17.7)."""
    return min(max(interval_s // 4, GRACE_MIN_S), GRACE_MAX_S)


def pings(entries: list[dict[str, object]]) -> list[str]:
    """The uuids of the success pings stubsvc recorded, oldest first."""
    return [
        found.group(1)
        for entry in entries
        if entry.get("method") == "GET" and (found := PING.fullmatch(str(entry.get("path")))) is not None
    ]


def worker_flags(ctx: Context, argv: list[str]) -> dict[str, bool]:
    """Whether the site's worker has each of `FAKE_VARS` set to `1`: compared here, never printed (an unset
    variable is printenv's silent exit 1); an exec that fails otherwise raises `CheckFailed`."""
    flags: dict[str, bool] = {}
    for name in FAKE_VARS:
        done = compose(argv, "exec", "-T", "worker", "printenv", name)
        if done.returncode != 0 and (done.returncode != 1 or done.stderr.strip()):
            raise CheckFailed(f"exec worker printenv: exit {done.returncode}: {tail(ctx, done)}")
        flags[name] = done.returncode == 0 and done.stdout.strip() == "1"
    return flags


def down(ctx: Context, argv: list[str]) -> None:
    """The project removed with its volumes, its profiled services and its orphans (`prodstack.down_v`)."""
    done = compose(argv, "--profile", "*", "down", "-v", "--remove-orphans", timeout=DOWN_TIMEOUT_S)
    succeeded(ctx, done, "down -v")


def up(ctx: Context, argv: list[str], *services: str) -> None:
    done = compose(argv, "up", "-d", "--no-build", "--wait", *services, timeout=UP_TIMEOUT_S)
    succeeded(ctx, done, f"up {' '.join(services)}")


# ---- 1 stack_up --------------------------------------------------------------------------------------------


def check_stack_up(ctx: Context) -> str | None:
    down(ctx, PT)  # whatever a killed run left: the stack starts fresh, and the repository empty
    down(ctx, PB)
    s3_down()
    if (holder := other_stack_up()) is not None:
        raise CheckFailed(f"{holder}: it holds 172.30.0.0/24 (the dev stack: `$DC down` first)")
    s3_up()
    up(ctx, PT, *SERVICES)
    for role in ROLES:
        done = compose(
            PT, "run", "--rm", "--no-deps", "-T", role, "jobhunter", "startup-check", "--role", role
        )
        succeeded(ctx, done, f"startup-check --role {role}")
    return f"S3 stand-in and PT up ({', '.join(SERVICES)}); startup-check passed: {', '.join(ROLES)}"


# ---- 2 tls -------------------------------------------------------------------------------------------------


def check_tls(ctx: Context) -> str | None:
    connect(ctx, PT)
    health = ctx.client.get("/healthz")
    problems = [f"HTTP {health.status_code}"] if health.status_code != 200 else []
    problems += [name for name, value in SECURITY_HEADERS.items() if health.headers.get_list(name) != [value]]
    problems += [name for name in ("server", "alt-svc") if name in health.headers]
    if problems:
        raise CheckFailed(f"/healthz: {', '.join(problems)}")
    anonymous = ctx.client.get("/api/auth/session")
    if anonymous.status_code != 401:
        raise CheckFailed(f"the session without a cookie: expected 401, got {anonymous.status_code}")
    if problems := cookie_problems(session_cookie(anonymous)):
        raise CheckFailed(f"the cleared cookie: {', '.join(problems)}")
    return (
        f"TLS verified against Caddy's root; /healthz has the {len(SECURITY_HEADERS)} §16.6 headers once "
        f"each, no Server, no Alt-Svc; 401 clears {SESSION_COOKIE} with "
        f"{', '.join(sorted(COOKIE_ATTRIBUTES))}, no Domain"
    )


# ---- 3 user ------------------------------------------------------------------------------------------------


def check_user(ctx: Context) -> str | None:
    created = compose(PT, "run", "--rm", "-T", "api", *USER_CREATE, stdin=f"{PASSWORD}\n")
    succeeded(ctx, created, "user create")
    login(ctx)
    if (user := session_user(ctx)) != USERNAME:
        raise CheckFailed(f"the session names {user!r}")
    return f"user {USERNAME} created; login over TLS 204 with {SESSION_COOKIE}; the session names the user"


# ---- 4 collect_and_profile ---------------------------------------------------------------------------------


def wait_draft(ctx: Context, draft_id: int, wanted: str) -> None:
    def reached() -> tuple[bool, str]:
        body = answer(get(ctx, f"/api/profile/drafts/{draft_id}"), 200, "the draft")
        status = body.get("status")
        if status == "failed":
            raise CheckFailed(f"draft {draft_id} failed ({body.get('failure_code')!r})")
        return status == wanted, f"status={status!r}"

    poll(reached, within_s=DRAFT_WITHIN_S, what=f"draft {draft_id} {wanted}")


def check_collect_and_profile(ctx: Context) -> str | None:
    flags = worker_flags(ctx, PT)
    shown = " ".join(f"{name.removeprefix('JOBHUNTER_').lower()}={flag}" for name, flag in flags.items())
    if not all(flags.values()):
        raise CheckFailed(f"the worker is not on both fakes ({shown}): no run was posted, no CV uploaded")
    if not all(one(PT, SEED_INPUTS, 4)):
        raise CheckFailed("an input was not written")
    queued = answer(post(ctx, "/api/runs", json={"include_paid": True}), 202, "run now")
    run = queued.get("run")
    run_id = int(run["id"]) if isinstance(run, dict) else None
    if run_id is None:
        raise CheckFailed("run now answered no run")

    def ended() -> tuple[bool, str]:
        (status,) = one(PT, RUN_STATUS.format(run_id=run_id))
        return status in TERMINAL, status

    if (status := poll(ended, within_s=COLLECT_WITHIN_S, what=f"run {run_id}'s end")) != "succeeded":
        raise CheckFailed(f"run {run_id} ended {status}")
    if (adzuna := adzuna_postings(PT)) < 1:
        raise CheckFailed(f"run {run_id} succeeded with no Adzuna posting")

    pdf = minimal_pdf(SYNTHETIC_CV.splitlines())
    uploaded = post(ctx, "/api/profile/cv", files={"file": ("Lebenslauf.pdf", pdf, "application/pdf")})
    draft_id = int(answer(uploaded, 202, "the CV upload")["draft_id"])
    wait_draft(ctx, draft_id, "text_review")
    answer(post(ctx, f"/api/profile/drafts/{draft_id}/text-review"), 202, "the text review")
    wait_draft(ctx, draft_id, "ready")
    confirmed = post(ctx, f"/api/profile/drafts/{draft_id}/confirm", json={"expected_rescore_ack": True})
    version_id = int(answer(confirmed, 201, "the confirm")["profile_version_id"])
    active = answer(get(ctx, "/api/profile"), 200, "the profile").get("active")
    found = (active.get("id"), active.get("version_no")) if isinstance(active, dict) else None
    if found != (version_id, 1):
        raise CheckFailed(f"expected version {version_id} active as version_no 1, got {found!r}")
    return (
        f"{shown}; inputs seeded; paid run {run_id} ({'created' if queued.get('created') else 'active'}) "
        f"succeeded with {adzuna} Adzuna postings; draft {draft_id} confirmed: version {version_id} active "
        "as version_no 1"
    )


# ---- 5 alert_dispatch --------------------------------------------------------------------------------------


def check_alert_dispatch(ctx: Context) -> str | None:
    (alert_id,) = one(PT, INSERT_ALERT)
    wanted = {"Priority": "3", "Tags": "warn", "Click": ALERT_CLICK, "authorization": True}

    def pushed() -> tuple[bool, str]:
        pushes = [
            entry
            for entry in stub_log(PT)
            if entry.get("method") == "POST"
            and str(entry.get("path")).startswith("/ntfy/")
            and entry.get("Title") == ALERT_TITLE
        ]
        if not pushes:
            return False, "no push of the alert"
        if wrong := sorted(name for name, value in wanted.items() if pushes[-1].get(name) != value):
            raise CheckFailed(f"the push of alert {alert_id} differs in {', '.join(wrong)}")
        (notified,) = one(PT, ALERT_NOTIFIED.format(alert=int(alert_id)))
        return notified == "t", f"pushed, last_notified_at set={notified}"

    poll(pushed, within_s=PUSH_WITHIN_S, what=f"the push of alert {alert_id}")
    return (
        f"warn alert {alert_id} ({ALERT_CODE}) pushed: Title {ALERT_TITLE!r}, Priority 3, Tags warn, Click "
        "/settings#alerts, an Authorization header; last_notified_at set"
    )


# ---- 6 deadman ---------------------------------------------------------------------------------------------


def check_deadman(ctx: Context) -> str | None:
    interval_s, enabled = one(PT, SCHEDULE, 2)
    if enabled != "t":
        raise CheckFailed("the schedule is disabled: no ping would be sent")
    before = len(pings(stub_log(PT)))
    rows = psql(PT, INSERT_RUN).split()
    if len(rows) != 1:
        raise CheckFailed("the scheduled run was not inserted (its slot is held)")
    run_id = int(rows[0])

    def pinged() -> tuple[bool, str]:
        new, watermark = len(pings(stub_log(PT))) - before, count(PT, WATERMARK)
        return new > 0 and watermark >= run_id, f"{new} new ping(s), watermark {watermark}"

    poll(pinged, within_s=PING_WITHIN_S, what=f"the ping of run {run_id}")
    shown = compose(
        PT, "run", "--rm", "--no-deps", "-T", "worker", "jobhunter", "ops", "deadman-sync", "--show"
    )
    printed = last_json(succeeded(ctx, shown, "ops deadman-sync --show").stdout)
    expected = {"timeout_s": int(interval_s), "grace_s": grace_s(int(interval_s)), "paused": False}
    got = {name: printed.get(name) for name in expected}
    if got != expected or printed.get("source") != "schedule_state":
        raise CheckFailed(f"--show printed {got} from {printed.get('source')!r}, expected {expected}")
    check_uuid = pings(stub_log(PT))[-1]  # never printed
    read = compose(PT, "exec", "-T", "stubsvc", "python", "-I", "-c", STUB_CHECK, check_uuid)
    held = last_json(succeeded(ctx, read, "the stub's check").stdout)
    stub = (held.get("timeout"), held.get("grace"), held.get("status"))
    if stub != (got["timeout_s"], got["grace_s"], "up"):
        raise CheckFailed(f"stubsvc holds timeout {stub[0]} grace {stub[1]}, {stub[2]}")
    return (
        f"scheduled run {run_id} pinged (a new GET /hc/ping/<uuid>, watermark past it); --show: timeout "
        f"{got['timeout_s']} s, grace {got['grace_s']} s, unpaused, from schedule_state, as stubsvc holds"
    )


# ---- 7 backup_set ------------------------------------------------------------------------------------------


def check_backup_set(ctx: Context) -> str | None:
    if (created := backup_cli(ctx, PT, "init")) != {"created": True}:
        raise CheckFailed(f"backup init printed {created}, expected a new repository")
    set_id = int(backup_cli(ctx, PT, "set")["set_id"])
    status, snapshot_id = one(PT, BACKUP_SET.format(set_id=set_id), 2)
    if status != "succeeded" or not snapshot_id:
        raise CheckFailed(f"set {set_id} is {status}")
    listed = compose(
        PT, "run", "--rm", "--no-deps", "-T", "backup", "python", "-I", "-c", SNAPSHOTS, f"set:{set_id}"
    )
    snapshots = last_json(succeeded(ctx, listed, "the snapshots").stdout)
    if [snapshot.get("id") for snapshot in snapshots] != [snapshot_id]:
        raise CheckFailed(f"set {set_id}: {len(snapshots)} snapshot(s) tagged set:{set_id}, not its own one")
    STATE.set_id = set_id
    return (
        f"repository created; set {set_id} succeeded; its snapshot is in the S3 stand-in, tagged set:{set_id}"
    )


# ---- 8 source_purge ----------------------------------------------------------------------------------------


def check_source_purge(ctx: Context) -> str | None:
    body = {"source": "adzuna", "confirm": PURGE_PHRASE, "password": PASSWORD}
    manifest_id = int(
        answer(post(ctx, "/api/privacy/purge-source", json=body), 202, "the purge")["manifest_id"]
    )

    def completed() -> tuple[bool, str]:
        status = manifest_status(ctx, manifest_id)
        return status == "completed", status

    poll(completed, within_s=PURGE_WITHIN_S, what=f"manifest {manifest_id} completed")
    if (adzuna := adzuna_postings(PT)) != 0:
        raise CheckFailed(f"manifest {manifest_id} completed with {adzuna} Adzuna postings left")
    if UNSHIPPED_BANNER not in (shown := banners(ctx)):
        raise CheckFailed(f"no {UNSHIPPED_BANNER} banner: {shown}")
    STATE.purge_id, STATE.purge_uuid = manifest_id, manifest_row(PT, manifest_id)[1]
    return (
        f"202: manifest {manifest_id} completed, no Adzuna posting left, the {UNSHIPPED_BANNER} banner shown"
    )


# ---- 9 manifest_shipped ------------------------------------------------------------------------------------


def check_manifest_shipped(ctx: Context) -> str | None:
    if STATE.purge_id is None:
        raise CheckFailed("no purge: check source_purge did not pass")
    manifest_id = STATE.purge_id
    printed = backup_cli(ctx, PT, "ship")

    def shipped() -> tuple[bool, str]:
        status, shown = manifest_status(ctx, manifest_id), banners(ctx)
        return status == "shipped" and UNSHIPPED_BANNER not in shown, f"{status}, banners {shown}"

    poll(shipped, within_s=SHIP_WITHIN_S, what=f"manifest {manifest_id} shipped and the banner gone")
    return f"backup ship ({printed.get('files')!r} file(s)); manifest {manifest_id} shipped, the banner gone"


# ---- 10 restore --------------------------------------------------------------------------------------------


def check_restore(ctx: Context) -> str | None:
    if STATE.set_id is None or STATE.purge_uuid is None:
        raise CheckFailed("no set or no shipped purge: checks backup_set to manifest_shipped did not pass")
    down(ctx, PT)  # site A is lost, with its volumes
    up(ctx, PB, "postgres")
    done = compose(PB, "--profile", "restore", "run", "--rm", "-T", "restore", timeout=RESTORE_TIMEOUT_S)
    printed = last_json(succeeded(ctx, done, "restore run").stdout)
    if (printed.get("restored"), printed.get("set_id")) != (True, STATE.set_id):
        raise CheckFailed(
            f"restore run printed restored={printed.get('restored')!r} set {printed.get('set_id')!r}"
        )
    verified = [
        (e.get("phase"), e.get("ok")) for e in events(done.stderr) if e["event"] == "restore_verified"
    ]
    if verified != [("restored", True), ("replayed", True)]:
        raise CheckFailed(f"the verify phases: {verified}")
    replayed = count(PB, REPLAY_ENTRIES.format(manifest_uuid=STATE.purge_uuid))
    adzuna = adzuna_postings(PB)
    if replayed < 1 or adzuna != 0:
        raise CheckFailed(f"on B the purge's replay_log has {replayed} entries, {adzuna} Adzuna postings")
    up(ctx, PB, *SERVICES)
    connect(ctx, PB)
    login(ctx)
    return (
        f"PT down -v; set {STATE.set_id} restored into PB, verify phases restored and replayed passed; the "
        f"purge's manifest replayed ({replayed} replay_log entr{'y' if replayed == 1 else 'ies'}), {adzuna} "
        "Adzuna postings; PB up; the login on B works"
    )


# ---- 11 export ---------------------------------------------------------------------------------------------


def check_export(ctx: Context) -> str | None:
    export_id = int(answer(post(ctx, "/api/privacy/export", json={}), 202, "the export")["export_id"])
    path = f"/api/privacy/exports/{export_id}"

    def ready() -> tuple[bool, str]:
        status = answer(get(ctx, path), 200, "the export").get("status")
        if status == "failed":
            raise CheckFailed(f"export {export_id} failed")
        return status == "ready", f"status={status!r}"

    poll(ready, within_s=EXPORT_WITHIN_S, what=f"export {export_id} ready")
    download = get(ctx, path, params={"download": "1"})
    kind = download.headers.get("content-type", "")
    if download.status_code != 200 or not kind.startswith("application/zip"):
        raise CheckFailed(f"the download: expected 200 application/zip, got {download.status_code} {kind!r}")
    try:
        names = zipfile.ZipFile(io.BytesIO(download.content)).namelist()
    except zipfile.BadZipFile:
        raise CheckFailed(f"export {export_id}'s download is no zip") from None
    if README not in names:
        raise CheckFailed(f"export {export_id}'s zip names no {README} ({len(names)} entries)")
    return (
        f"export {export_id} ready on B; its zip ({len(download.content)} bytes, {len(names)} entries) "
        f"names {README}"
    )


# ---- 12 delete_everything ----------------------------------------------------------------------------------


def check_delete_everything(ctx: Context) -> str | None:
    wrong = post(ctx, "/api/privacy/delete-everything", json={"confirm": WRONG_PHRASE, "password": PASSWORD})
    if (wrong.status_code, problem_code(wrong)) != (422, CONFIRMATION_MISMATCH):
        raise CheckFailed(f"a wrong phrase: got {wrong.status_code} (code {problem_code(wrong)!r})")
    old = ctx.auth()
    body = {"confirm": DELETE_PHRASE, "password": PASSWORD}
    manifest_id = int(
        answer(post(ctx, "/api/privacy/delete-everything", json=body), 202, "delete everything")[
            "manifest_id"
        ]
    )

    def reached(*wanted: str) -> Callable[[], tuple[bool, str]]:
        def probe() -> tuple[bool, str]:  # with psql: the completion ends the session
            status = manifest_row(PB, manifest_id)[0]
            if status == "failed":
                raise CheckFailed(f"manifest {manifest_id} failed")
            return status in wanted, status

        return probe

    poll(reached("completed", "shipped"), within_s=PURGE_WITHIN_S, what=f"manifest {manifest_id} completed")
    printed = backup_cli(ctx, PB, "ship")
    poll(reached("shipped"), within_s=SHIP_WITHIN_S, what=f"manifest {manifest_id} shipped")
    if (refused := ctx.client.get("/api/auth/session", headers=old).status_code) != 401:
        raise CheckFailed(f"the old session: expected 401, got {refused}")
    login(ctx)
    jobs = answer(get(ctx, "/api/jobs"), 200, "the jobs list").get("items")
    if jobs != []:
        raise CheckFailed(f"the jobs list holds {len(jobs) if isinstance(jobs, list) else jobs!r} items")
    user, users = session_user(ctx), count(PB, USERS)
    if user != USERNAME or users != 1:
        raise CheckFailed(f"the user: the session names {user!r}, users holds {users} row(s)")
    return (
        f"a wrong phrase 422 {CONFIRMATION_MISMATCH}; 202: manifest {manifest_id} completed; backup ship "
        f"({printed.get('files')!r} file(s)); shipped; the old session 401; a new login 204; the jobs list "
        f"empty; user {USERNAME} kept"
    )


# ---- 13 teardown -------------------------------------------------------------------------------------------


def check_teardown(ctx: Context) -> str | None:
    """`PT`, `PB` and the stand-in removed with their volumes, each even when an earlier one failed."""
    problems = []
    steps: list[tuple[str, Callable[[], None]]] = [
        ("PT", lambda: down(ctx, PT)),
        ("PB", lambda: down(ctx, PB)),
        ("S3", s3_down),
    ]
    for name, step in steps:
        try:
            step()
        except CheckFailed as exc:
            problems.append(f"{name}: {exc}")
        except HelperFailed as exc:
            problems.append(f"{name}: {helper_detail(ctx, exc)}")
        except subprocess.TimeoutExpired as exc:
            problems.append(f"{name}: timed out after {exc.timeout:g} s")
        except OSError as exc:
            problems.append(f"{name}: {type(exc).__name__}")
    if problems:
        raise CheckFailed("; ".join(problems))
    return "PT, PB and the S3 stand-in removed with their volumes"


CHECKS: Final[list[tuple[str, Callable[[Context], str | None]]]] = [
    ("stack_up", check_stack_up),
    ("tls", check_tls),
    ("user", check_user),
    ("collect_and_profile", check_collect_and_profile),
    ("alert_dispatch", check_alert_dispatch),
    ("deadman", check_deadman),
    ("backup_set", check_backup_set),
    ("source_purge", check_source_purge),
    ("manifest_shipped", check_manifest_shipped),
    ("restore", check_restore),
    ("export", check_export),
    ("delete_everything", check_delete_everything),
    ("teardown", check_teardown),
]


def helper_detail(ctx: Context, exc: BaseException) -> str:
    """A prodstack helper's failure, which quotes a command's whole output: the end of its first line (the
    command and its exit status), then its last two lines; scrubbed before it is cut, so a cut never leaves a
    part of a secret."""
    lines = [line for line in ctx.scrub(str(exc)).splitlines() if line.strip()] or [type(exc).__name__]
    head = lines[0] if len(lines[0]) <= HEAD_CHARS else f"...{lines[0][-HEAD_CHARS:]}"
    return " | ".join([head, *lines[1:][-2:]])[:DETAIL_CHARS]


def run_check(ctx: Context, name: str, check: Callable[[Context], str | None]) -> bool:
    """`check`, and its JSON line: whether it passed."""
    try:
        detail = check(ctx)
    except CheckFailed as exc:
        emit(ctx, name, False, str(exc))
    except HelperFailed as exc:
        emit(ctx, name, False, helper_detail(ctx, exc))
    except subprocess.TimeoutExpired as exc:
        emit(ctx, name, False, f"{Path(str(exc.cmd[0])).name} timed out after {exc.timeout:g} s")
    except (httpx.HTTPError, OSError) as exc:  # transport, a missing docker
        emit(ctx, name, False, type(exc).__name__)
    except (ValueError, KeyError, TypeError, IndexError) as exc:  # data: no JSON, a missing key, a wrong type
        emit(ctx, name, False, f"unexpected data ({type(exc).__name__})")
    else:
        emit(ctx, name, True, detail)
        return True
    return False


def main() -> int:
    ctx = Context(httpx.Client(trust_env=False))  # `tls` replaces it with the site's verified client
    *steps, (last, teardown) = CHECKS
    passed = False
    try:
        passed = all(run_check(ctx, name, check) for name, check in steps)  # stops at the first failure
    finally:
        torn_down = run_check(ctx, last, teardown)  # after a failure or an interrupt too
        ctx.client.close()
    return 0 if passed and torn_down else 1


if __name__ == "__main__":
    sys.exit(main())
