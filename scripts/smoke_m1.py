"""M1 acceptance smoke test (Task 25): the collection pipeline on the dev stack, served from the fixtures.

    docker compose --env-file deploy/images.lock -f deploy/compose.dev.yaml \
        -f deploy/compose.fixtures.yaml down -v
    docker compose --env-file deploy/images.lock -f deploy/compose.dev.yaml \
        -f deploy/compose.fixtures.yaml up -d --build --wait
    uv run python scripts/smoke_m1.py

The precondition is a FRESH dev stack with the fixtures overlay (`deploy/compose.fixtures.yaml`: the worker in
fake mode, every source answered from `tests/contract/fixtures`, so no provider is ever called). The script
creates the one user, so a stack that has one already fails the first check with the hint below. It never
deletes anything.

Its own compose calls pass both files and the digest lock as `DC` does. The M0 helpers it imports from
`scripts/smoke_m0.py` (`Context`, `emit`, `psql`, `poll_health`) are used as they are; `psql` is a
`docker compose exec` into the running `postgres` container of the project `jobhunter-dev`, which neither
creates nor recreates a container. Seven checks run in order and each prints exactly one JSON line,
`{"check": <name>, "ok": <bool>}` with an optional `"detail"`:

1. `user_create`: `jobhunter user create --password-stdin` in a one-off api container;
2. `login`: `POST /api/auth/login` answers 204 with the session cookie (M0's check);
3. `health_ready`: `GET /api/system/health` reports `ready.ok` and `embedder.ok`, with a `quota` object;
4. `seed_inputs`: with `psql` (plan Deviation 3), the query `software engineer` (`en`, `user`) and the
   watchlist company `getyourguide` (Greenhouse) with its board instance, both enabled by entries appended to
   the active settings version's `queries[]` and `watchlist[]`;
5. `run_now_paid`: first the running worker must be in fake mode (`JOBHUNTER_FAKE_SOURCES` is `1`, read with
   `docker compose exec worker printenv` and compared here: only the boolean is printed, never the value),
   so a stack without the fixtures overlay fails here, before any run is posted; then `POST /api/runs` with
   `{"include_paid": true}` answers 202 with a new run;
6. `collect_succeeded`: that run is the health's `last_run` and `succeeded` (the fixtures reject no item,
   tests/contract/README.md); then jobs exist, no raw item is `pending`, embeddings exist, the ledger holds
   4 `billed` entries (two JSearch pages, two Adzuna pages) and every invariant of the run report is `ok`;
7. `second_run_idempotent`: a second `POST /api/runs` with `{"include_paid": false}` succeeds, the job count
   is unchanged and every Arbeitnow and Greenhouse posting has been seen twice.

It stops at the first failed check (exit 1); after seven `ok` lines it exits 0. The password, the session
cookie and its token are never printed (`Context.scrub`, as in M0); a `detail` holds counts and fixed text.
"""

import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

import httpx
from smoke_m0 import (  # scripts/ is the script's directory, the first entry of sys.path
    DOCKER_TIMEOUT_S,
    HTTP_TIMEOUT_S,
    ORIGIN,
    PASSWORD,
    READY_WITHIN_S,
    USER_CREATE,
    USER_EXISTS_MESSAGE,
    CheckFailed,
    Context,
    check_login,
    emit,
    pick,
    poll_health,
    psql,
    stderr_tail,
)

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
]
FRESH_HINT: Final = (
    "stack not fresh: run `docker compose --env-file deploy/images.lock -f deploy/compose.dev.yaml "
    "-f deploy/compose.fixtures.yaml down -v` first"
)

QUERY_TEXT: Final = "software engineer"
BOARD_SLUG: Final = "getyourguide"  # Greenhouse; its fixture route answers `greenhouse_getyourguide`
PAID_RUN_KEY: Final = "smoke-m1-run-paid-0001"
FAKE_SOURCES_VAR: Final = "JOBHUNTER_FAKE_SOURCES"  # deploy/compose.fixtures.yaml sets it to 1 on the worker
FREE_RUN_KEY: Final = "smoke-m1-run-free-0001"
# The first run embeds every revision it creates (about 140 here) on the dev embedder, the fp32 export at
# about 0.55 docs/s on the V7 CPU (ruling T21-a): some 4 to 5 minutes. The second run creates no revision.
COLLECT_WITHIN_S: Final = 600
SECOND_RUN_WITHIN_S: Final = 150
BILLED_ENTRIES: Final = 4  # JSearch page 1 (the probe) and page 2, Adzuna page 1 and its empty page 2
INVARIANTS: Final = 7  # I1-I7 (§17.3)
TERMINAL: Final = frozenset({"succeeded", "partial", "failed", "cancelled"})

# One statement, so the inputs arrive together: the query, the company, its board instance (the statement of
# `instances.instance_for_company`), and the active settings version with both entries appended. Every value
# interpolated into it is a constant of this module.
SEED_INPUTS: Final = f"""\
WITH q AS (
  INSERT INTO queries (text, lang, origin) VALUES ('{QUERY_TEXT}', 'en', 'user') RETURNING id
), c AS (
  INSERT INTO watchlist_companies (display_name, company_key, ats_type, board_slug, detection,
    validation_status, validated_at)
  VALUES ('{BOARD_SLUG}', '{BOARD_SLUG}', 'greenhouse', '{BOARD_SLUG}', 'manual', 'ok', now()) RETURNING id
), i AS (
  INSERT INTO source_instances (source_id, instance_key, watchlist_company_id)
  SELECT 'greenhouse', '{BOARD_SLUG}', c.id FROM c RETURNING id
), s AS (
  UPDATE settings_versions v SET payload = jsonb_set(jsonb_set(v.payload,
      '{{queries}}', v.payload->'queries'
        || jsonb_build_array(jsonb_build_object('query_id', (SELECT id FROM q), 'enabled', true))),
      '{{watchlist}}', v.payload->'watchlist'
        || jsonb_build_array(jsonb_build_object('watchlist_company_id', (SELECT id FROM c), 'enabled', true)))
  FROM active_config a WHERE a.id = 1 AND v.id = a.settings_version_id RETURNING v.id
)
SELECT (SELECT id FROM q), (SELECT id FROM c), (SELECT id FROM i), (SELECT id FROM s)"""  # noqa: S608 (constants only)
COLLECTED: Final = """\
SELECT (SELECT count(*) FROM jobs), (SELECT count(*) FROM raw_items WHERE disposition = 'pending'),
  (SELECT count(*) FROM embeddings), (SELECT count(*) FROM quota_ledger WHERE state = 'billed'),
  (SELECT count(*) FROM runs r CROSS JOIN LATERAL jsonb_each(r.report->'execution'->'invariants') e(k, v)
    WHERE r.id = {run_id}),
  (SELECT count(*) FILTER (WHERE (v->>'ok')::boolean) FROM runs r
    CROSS JOIN LATERAL jsonb_each(r.report->'execution'->'invariants') e(k, v) WHERE r.id = {run_id})"""
SEEN_AGAIN: Final = """\
SELECT (SELECT count(*) FROM jobs), (SELECT count(*) FROM postings p JOIN source_instances s
    ON s.id = p.source_instance_id WHERE s.source_id IN ('arbeitnow', 'greenhouse')),
  (SELECT coalesce(min(p.seen_count), 0) FROM postings p JOIN source_instances s
    ON s.id = p.source_instance_id WHERE s.source_id IN ('arbeitnow', 'greenhouse'))"""


class State:
    """What the checks hand on: the first run, and the job count after it."""

    paid_run_id: int | None = None
    jobs: int | None = None


STATE: Final = State()


def query_row(sql: str, columns: int) -> list[str]:
    """The one row of `sql` (`psql -At`: `|`-separated), checked to have `columns` fields."""
    process = psql(sql)
    if process.returncode != 0:
        raise CheckFailed(f"psql exit {process.returncode}: {stderr_tail(process)}")
    lines = process.stdout.strip().splitlines()
    fields = lines[-1].split("|") if lines else []
    if len(fields) != columns:
        raise CheckFailed(f"expected one row of {columns} fields, got {len(lines)} line(s)")
    return fields


def ints(fields: list[str]) -> list[int]:
    try:
        return [int(field) for field in fields]
    except ValueError:
        raise CheckFailed(f"expected integers, got {fields!r}") from None


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


def check_health_ready(ctx: Context) -> str | None:
    def done(body: Any) -> tuple[bool, str]:
        ready, embedder, quota = pick(body, "ready", "ok"), pick(body, "embedder", "ok"), pick(body, "quota")
        return (
            ready is True and embedder is True and isinstance(quota, dict),
            f"ready.ok={ready!r} failing={pick(body, 'ready', 'failing')!r} embedder.ok={embedder!r} "
            f"quota={type(quota).__name__}",
        )

    poll_health(ctx, done, within_s=READY_WITHIN_S, what="ready.ok and embedder.ok true with a quota object")
    return None


def check_seed_inputs(ctx: Context) -> str | None:
    fields = query_row(SEED_INPUTS, 4)
    if not all(fields):
        raise CheckFailed(f"an input was not written (ids {fields!r})")
    return None


def run_now(ctx: Context, *, include_paid: bool, key: str) -> int:
    """`POST /api/runs`: the id of the new manual run it answers 202 with."""
    headers = {**ctx.auth(), "Origin": ORIGIN, "X-JH-Request": "1", "Idempotency-Key": key}
    response = ctx.client.post(f"{ORIGIN}/api/runs", json={"include_paid": include_paid}, headers=headers)
    if response.status_code != 202:
        raise CheckFailed(f"expected 202, got {response.status_code}")
    body = response.json()
    run_id, created = pick(body, "run", "id"), pick(body, "created")
    if not isinstance(run_id, int) or created is not True:
        raise CheckFailed(f"expected a new run, got id={run_id!r} created={created!r}")
    return run_id


def wait_succeeded(ctx: Context, run_id: int, *, within_s: int) -> float:
    """Poll the health until run `run_id` is its `last_run` and `succeeded`; the seconds it took."""
    started = time.monotonic()

    def done(body: Any) -> tuple[bool, str]:
        last_id, status = pick(body, "last_run", "id"), pick(body, "last_run", "status")
        if last_id == run_id and status in TERMINAL and status != "succeeded":
            raise CheckFailed(f"run {run_id} ended {status!r}")
        return last_id == run_id and status == "succeeded", f"last_run id={last_id!r} status={status!r}"

    poll_health(ctx, done, within_s=within_s, what=f"run {run_id} succeeded")
    return time.monotonic() - started


def worker_fake_mode() -> bool:
    """Whether the running worker container has `JOBHUNTER_FAKE_SOURCES=1` (the fixtures overlay). The value
    is compared here and never printed; an unset variable is `False` (printenv's silent exit 1). An exec that
    fails (any other exit, or exit 1 with a message) raises `CheckFailed` with its stderr."""
    process = subprocess.run(
        [*DC, "exec", "-T", "worker", "printenv", FAKE_SOURCES_VAR],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=DOCKER_TIMEOUT_S,
    )
    # printenv exits 1, silently, for an unset variable; docker compose exec exits non-zero with a message of
    # its own when it cannot run it (a worker that is not running exits 1 too). Either way no run is posted.
    if process.returncode != 0 and (process.returncode != 1 or process.stderr.strip()):
        # docker or the container failed: its stderr, never stdout
        raise CheckFailed(f"docker compose exec worker: exit {process.returncode}: {stderr_tail(process)}")
    return process.returncode == 0 and process.stdout.strip() == "1"


def check_run_now_paid(ctx: Context) -> str | None:
    # Task 25 review, Minor 1: a paid run on a stack without the fixtures overlay would reach the providers.
    fake = worker_fake_mode()
    if not fake:
        raise CheckFailed(f"the worker is not on the fixtures (fake_sources={fake}): no run was posted")
    STATE.paid_run_id = run_now(ctx, include_paid=True, key=PAID_RUN_KEY)
    return f"fake_sources={fake}"


def check_collect_succeeded(ctx: Context) -> str | None:
    if STATE.paid_run_id is None:
        raise CheckFailed("no paid run: check run_now_paid did not pass")
    took = wait_succeeded(ctx, STATE.paid_run_id, within_s=COLLECT_WITHIN_S)
    jobs, pending, embeddings, billed, invariants, ok = ints(
        query_row(COLLECTED.format(run_id=STATE.paid_run_id), 6)
    )
    found = (
        f"jobs={jobs} pending={pending} embeddings={embeddings} billed={billed} "
        f"invariants_ok={ok}/{invariants}"
    )
    if not (
        jobs > 0
        and pending == 0
        and embeddings > 0
        and billed == BILLED_ENTRIES
        and ok == invariants == INVARIANTS
    ):
        expected = f"jobs>0 pending=0 embeddings>0 billed={BILLED_ENTRIES} all {INVARIANTS} invariants ok"
        raise CheckFailed(f"expected {expected}; {found}")
    STATE.jobs = jobs
    return f"succeeded in {took:.0f} s; {found}"


def check_second_run_idempotent(ctx: Context) -> str | None:
    if STATE.jobs is None:
        raise CheckFailed("no job count: check collect_succeeded did not pass")
    run_id = run_now(ctx, include_paid=False, key=FREE_RUN_KEY)
    took = wait_succeeded(ctx, run_id, within_s=SECOND_RUN_WITHIN_S)
    jobs, postings, min_seen = ints(query_row(SEEN_AGAIN, 3))
    found = (
        f"jobs={jobs} (was {STATE.jobs}) arbeitnow_greenhouse_postings={postings} min_seen_count={min_seen}"
    )
    if not (jobs == STATE.jobs and postings > 0 and min_seen == 2):
        raise CheckFailed(f"expected the job count unchanged and min(seen_count)=2; {found}")
    return f"succeeded in {took:.0f} s; {found}"


CHECKS: Final[list[tuple[str, Callable[[Context], str | None]]]] = [
    ("user_create", check_user_create),
    ("login", check_login),
    ("health_ready", check_health_ready),
    ("seed_inputs", check_seed_inputs),
    ("run_now_paid", check_run_now_paid),
    ("collect_succeeded", check_collect_succeeded),
    ("second_run_idempotent", check_second_run_idempotent),
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
