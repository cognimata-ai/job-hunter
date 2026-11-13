"""Command line entry point (`python -m jobhunter.cli`, installed as the `jobhunter` script)."""

import argparse
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, cast, get_args

from jobhunter.config import DbRole, Role, load_config, startup_or_exit

if TYPE_CHECKING:  # the module loads procrastinate, langgraph and alembic: `main` imports it when it runs
    from jobhunter.db.migrate import StepGroup

HTTP_CHECK_TIMEOUT_S: Final = 3
# The container healthchecks, which `main` leaves out of `disable_tracing` (M5 Task 6, Ruling T6-9): they
# import neither langchain nor langsmith, so they cannot trace, and loading the SDK only to switch it off
# would cost each of them about a quarter of a second, every few seconds. Their import guard tests pin that
# they load neither; a probe that comes to load one leaves this set.
UNTRACEABLE_PROBES: Final = frozenset({"http-check", "worker-health"})
# `migrate --db-role`: the role a `migrate` starts as, whose secrets hold that login. The restore test runs in
# the backup container, as `jobhunter_restore` (§19.5), whose password is one of the backup role's secrets.
MIGRATE_CONFIG_ROLES: Final[Mapping[DbRole, Role]] = MappingProxyType(
    {"migrator": "migrate", "restore": "backup"}
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jobhunter")
    commands = parser.add_subparsers(dest="command", required=True)
    startup_check = commands.add_parser(
        "startup-check", help="run the startup assertions of a role (exit 78 when one fails)"
    )
    startup_check.add_argument("--role", required=True, choices=get_args(Role))
    _add_migrate_command(commands)
    _add_manifests_command(commands)
    _add_api_command(commands)
    commands.add_parser(
        "worker",
        help="run the worker: singleton lock, takeover, procrastinate queues (§3.3); SIGTERM drains it",
    )
    commands.add_parser(
        "worker-health",
        help="exit 0 when the worker heartbeat is at most 60 s old, else 1 (container HEALTHCHECK)",
    )
    http_check = commands.add_parser(
        "http-check", help="exit 0 when a GET of URL answers 2xx within 3 s, else 1 (container HEALTHCHECK)"
    )
    http_check.add_argument("url", help="the http or https URL to probe, e.g. http://127.0.0.1:8000/healthz")
    _add_user_commands(commands)
    _add_sessions_commands(commands)
    _add_fixtures_commands(commands)
    dry_run = commands.add_parser(
        "schema-dry-run",
        help="send one minimal request per structured-output schema and model, record an eval run (§10.11)",
    )
    dry_run.add_argument(
        "--live",
        action="store_true",
        help="send to the Anthropic API with the worker's anthropic_key (default: the recorded-SSE fake)",
    )
    dry_run.add_argument(
        "--version",
        type=int,
        metavar="ID",
        help="the prompt-schema version to dry-run (default: the active one)",
    )
    policy = dry_run.add_mutually_exclusive_group()
    policy.add_argument(
        "--model-policy", type=int, metavar="ID", help="the model policy to dry-run (default: the active one)"
    )
    policy.add_argument(
        "--candidate-payload",
        metavar="FILE",
        help="a settings document (JSON) whose model policy is staged by hash, then dry-run",
    )
    _add_golden_commands(commands)
    _add_eval_commands(commands)
    _add_ops_commands(commands)
    _add_backup_commands(commands)
    _add_restore_commands(commands)
    _add_crypto_commands(commands)
    return parser


def _step_groups(text: str) -> "frozenset[StepGroup]":
    """`--steps`: comma-separated step groups, each one of `db.migrate.STEP_GROUPS`."""
    # Imported here: the migrate module loads procrastinate, langgraph and alembic (see `main`).
    from jobhunter.db.migrate import STEP_GROUPS

    groups = {part.strip() for part in text.split(",")}
    if unknown := sorted(groups - set(STEP_GROUPS)):
        raise argparse.ArgumentTypeError(
            f"unknown step groups {unknown}; choose from {', '.join(STEP_GROUPS)}"
        )
    return cast("frozenset[StepGroup]", frozenset(groups))


def _add_migrate_command(commands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    migrate = commands.add_parser(
        "migrate", help="bring the database to the code head and seed the defaults (§19.3)"
    )
    migrate.add_argument(
        "--database",
        metavar="DB",
        help="the database to migrate (default JOBHUNTER_DB_NAME), e.g. a scratch one",
    )
    migrate.add_argument(
        "--steps",
        type=_step_groups,
        metavar="GROUPS",
        help="run only these comma-separated step groups: sequences (steps 1 and 3), alembic (step 2), "
        "replay (step 7); default: all seven steps",
    )
    migrate.add_argument(
        "--manifest-dir",
        type=Path,
        metavar="DIR",
        help="the deletion-manifest stream (default JOBHUNTER_MANIFESTS_DIR)",
    )
    migrate.add_argument(
        "--cv-dir",
        type=Path,
        metavar="DIR",
        help="the CV store the replay removes files from (default JOBHUNTER_CV_STORE_DIR)",
    )
    migrate.add_argument(
        "--exports-dir",
        type=Path,
        metavar="DIR",
        help="the export archives the replay removes (default JOBHUNTER_EXPORTS_DIR, the mounted volume)",
    )
    migrate.add_argument(
        "--db-role",
        choices=tuple(MIGRATE_CONFIG_ROLES),
        default="migrator",
        help="the login: migrator (default) or restore (the restore test, with the backup role's secrets)",
    )


def _add_manifests_command(commands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    manifests = commands.add_parser("manifests", help="the deletion-manifest stream (§19.5)")
    manifests_commands = manifests.add_subparsers(dest="manifests_command", required=True)
    replay = manifests_commands.add_parser(
        "replay",
        help="migrate steps 1, 3 and 7 alone: advance the identities, then replay the stream (§19.5)",
    )
    replay.add_argument(
        "--manifest-dir",
        type=Path,
        metavar="DIR",
        help="the deletion-manifest stream (default JOBHUNTER_MANIFESTS_DIR)",
    )


def _add_api_command(commands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    api = commands.add_parser(
        "api",
        help="run the startup assertions of the api (exit 78 when one fails), then serve it with uvicorn",
    )
    api.add_argument("--host", default="127.0.0.1", help="the address to listen on (default 127.0.0.1)")
    api.add_argument("--port", type=int, default=8000, help="the port to listen on (default 8000)")
    api.add_argument(
        "--proxy-headers",
        action="store_true",
        help="take the client address from X-Forwarded-For when a trusted forwarder sends it (default off)",
    )
    api.add_argument(
        "--forwarded-allow-ips",
        metavar="IPS",
        help="comma-separated addresses of the trusted forwarders, e.g. 172.30.0.2 (the dev and prod proxy)",
    )


def _add_user_commands(commands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    user = commands.add_parser("user", help="manage the single user (§16.2): create, set-password, totp")
    user_commands = user.add_subparsers(dest="user_command", required=True)
    stdin_help = "read the password from one line of stdin instead of prompting (refused in prod)"
    create = user_commands.add_parser("create", help="create the single user; asks for the password twice")
    create.add_argument("--username", required=True)
    create.add_argument("--password-stdin", action="store_true", help=stdin_help)
    set_password = user_commands.add_parser(
        "set-password", help="change the password and revoke every session; asks for it twice"
    )
    set_password.add_argument("--password-stdin", action="store_true", help=stdin_help)
    totp = user_commands.add_parser("totp", help="turn TOTP (authenticator app codes) on or off")
    totp_commands = totp.add_subparsers(dest="totp_command", required=True)
    totp_commands.add_parser(
        "enable", help="show the otpauth URI and a QR code, store the secret once a code verifies"
    )
    totp_commands.add_parser("disable", help="turn TOTP off and delete the secret")


def _add_sessions_commands(commands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    sessions = commands.add_parser("sessions", help="session and lockout recovery (§16.3, §16.4)")
    sessions_commands = sessions.add_subparsers(dest="sessions_command", required=True)
    sessions_commands.add_parser("revoke-all", help="revoke every session: everyone logs in again")
    sessions_commands.add_parser(
        "unlock", help="lift every login lockout and resolve the global auth.lockout alert"
    )


def _add_golden_commands(commands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    golden = commands.add_parser("golden", help="the golden set (§11.13): the adversarial seed")
    golden_commands = golden.add_subparsers(dest="golden_command", required=True)
    seed = golden_commands.add_parser(
        "seed-adversarial",
        help="seed the adversarial golden cases (synthetic ads and profiles), idempotently",
    )
    seed.add_argument(
        "--fixtures", metavar="DIR", help="the fixture directory (default: the packaged adversarial fixtures)"
    )


def _add_eval_commands(commands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    evals = commands.add_parser("eval", help="golden-set eval runs (§11.13)")
    eval_commands = evals.add_subparsers(dest="eval_command", required=True)
    run = eval_commands.add_parser("run", help="start an eval run on the worker and wait for its end")
    run.add_argument("--kind", required=True, choices=["golden"])
    candidate = run.add_mutually_exclusive_group(required=True)
    candidate.add_argument(
        "--candidate",
        metavar="active|IDS",
        help="'active', or the rubric, prompt-schema, model-policy, evaluation-policy and execution-config "
        "ids, comma-separated",
    )
    candidate.add_argument(
        "--candidate-payload", metavar="FILE", help="a settings document (JSON) whose rows are staged by hash"
    )
    run.add_argument("--route", default="production", choices=["production", "forced_critic"])
    transport = run.add_mutually_exclusive_group(required=True)
    transport.add_argument("--live", action="store_true", help="the run is requested on the Anthropic API")
    transport.add_argument("--fake", action="store_true", help="the run is requested on the fake transport")


def _interval_hours(text: str) -> int:
    """`ops deadman-sync --interval-hours`: whole hours within `schedule_state`'s range, 1 to 168 (§3.4)."""
    try:
        hours = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("a whole number of hours") from None
    if not 1 <= hours <= 168:
        raise argparse.ArgumentTypeError("from 1 to 168 hours")
    return hours


def _add_ops_commands(commands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    ops = commands.add_parser(
        "ops",
        help="the deployment's checks of ntfy and healthchecks (V15), as the worker; never print a secret",
    )
    ops_commands = ops.add_subparsers(dest="ops_command", required=True)
    ops_commands.add_parser(
        "notify-test",
        help='push one test notification to NTFY_TOPIC_URL; print {"sent": true} or the error code',
    )
    sync = ops_commands.add_parser(
        "deadman-sync",
        help="apply the schedule's period to the dead-man check (healthchecks), under its lock",
    )
    sync.add_argument(
        "--show", action="store_true", help="print the check's timeout, grace and pause as read back"
    )
    sync.add_argument(
        "--interval-hours",
        type=_interval_hours,
        metavar="N",
        help="the period to apply when no database is reachable (before it exists), 1 to 168",
    )


def _add_backup_commands(commands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    backup = commands.add_parser(
        "backup", help="the backup container's jobs (§19.5), as the backup role; never print a secret"
    )
    backup_commands = backup.add_subparsers(dest="backup_command", required=True)
    backup_commands.add_parser(
        "init",
        help='initialise the restic repository unless it exists; print {"created": true|false} (run once)',
    )
    backup_commands.add_parser(
        "ship", help="ship the deletion manifests to the restic repository unless they are unchanged"
    )
    backup_commands.add_parser(
        "set", help='take one backup set from one exported snapshot (§19.5); print {"set_id": N}'
    )
    backup_commands.add_parser(
        "prune", help="forget the backup sets beyond the daily, weekly and monthly counts kept, and prune"
    )
    backup_commands.add_parser(
        "restore-test",
        help="restore the latest set into a scratch database as the restore role, replay and verify it; "
        'record the run (§19.5); print {"run_id": N}',
    )
    backup_commands.add_parser(
        "scheduler",
        help="the backup service's command: run the set, ship, prune and restore-test jobs on their crons "
        "until SIGTERM",
    )


def _set_id(text: str) -> int:
    """`--set`: a backup set's id, a whole number of at least 1."""
    if not (text.isascii() and text.isdigit() and int(text) >= 1):
        raise argparse.ArgumentTypeError("a backup set's id, a whole number of at least 1")
    return int(text)


def _add_restore_commands(commands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    restore = commands.add_parser(
        "restore", help="the disaster restore (§19.5), in the restore container; never print a secret"
    )
    restore_commands = restore.add_subparsers(dest="restore_command", required=True)
    run = restore_commands.add_parser(
        "run",
        help="restore the manifest stream (the union of every manifests snapshot) and a backup set into the "
        "empty database and volumes, migrate, replay and verify (the restore service's command)",
    )
    run.add_argument(
        "--set", type=_set_id, metavar="ID", help="the backup set to restore (default: the latest)"
    )
    verify = restore_commands.add_parser(
        "verify", help="check a restored database: before the replay (restored) or after it (replayed)"
    )
    verify.add_argument("--set", type=_set_id, metavar="ID", required=True, help="the backup set restored")
    verify.add_argument("--phase", required=True, choices=["restored", "replayed"])
    verify.add_argument("--database", metavar="DB", help="the database to check (default JOBHUNTER_DB_NAME)")


def _add_crypto_commands(commands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    crypto = commands.add_parser("crypto", help="the data key (§16.8), as the worker; never print a key")
    crypto_commands = crypto.add_subparsers(dest="crypto_command", required=True)
    crypto_commands.add_parser(
        "rotate",
        help="re-encrypt every ciphertext under data_key_previous with data_key, resumably, then count what "
        "remains by key id; exit 0 when only data_key remains, 3 when other old keys remain, 2 without a "
        "previous key, 1 when a ciphertext under data_key_previous remains (restart api and worker, run "
        "again) or one cannot be rotated",
    )


def _add_fixtures_commands(commands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    fixtures = commands.add_parser("fixtures", help="source fixture tooling (§18.4)")
    fixtures_commands = fixtures.add_subparsers(dest="fixtures_command", required=True)
    scrub = fixtures_commands.add_parser(
        "scrub", help="scrub raw recordings into contract fixtures (pseudonyms for PII, tokens and keys)"
    )
    scrub.add_argument("input", help="the directory of raw recordings, e.g. .fixtures-raw")
    scrub.add_argument("output", help="the fixture directory to write, e.g. tests/contract/fixtures")


def _worker_health() -> int:
    """The worker's container HEALTHCHECK: one read of `worker_heartbeat` as the application role.

    It runs every few seconds, so it stays quiet: no logging setup and no startup assertions, and a failure
    prints one stderr line that names the error class only (never a DSN, a message or a traceback).
    """
    try:
        # Imported here: only this probe (and the worker) needs psycopg and the worker modules.
        from jobhunter.worker.lock import heartbeat_is_fresh

        fresh = heartbeat_is_fresh(load_config("worker").dsn("psycopg"))
    except Exception as exc:  # noqa: BLE001 - the probe's exit status is its outcome; Docker records it
        print(f"worker-health: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0 if fresh else 1


def _http_check(url: str) -> int:
    """An api container HEALTHCHECK: one GET of `url`, with `HTTP_CHECK_TIMEOUT_S` for each socket operation.

    Quiet like `worker-health`: the standard library only (httpx is no runtime dependency), no logging setup,
    and a failure prints one stderr line that names the error class only. urllib raises `HTTPError` for every
    final status outside 2xx (after following redirects), so an answer that does not raise is a 2xx. Proxy
    variables are ignored: the probe asks the server in its own container.
    """
    try:
        # Imported here: the other commands do not need them.
        import urllib.parse
        import urllib.request

        if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
            raise ValueError("http-check probes http and https URLs only")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=HTTP_CHECK_TIMEOUT_S):
            pass
    except Exception as exc:  # noqa: BLE001 - the probe's exit status is its outcome; Docker records it
        print(f"http-check: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command != "worker" and args.command not in UNTRACEABLE_PROBES:
        # Only the worker traces (§17.8; M5 Task 6, Ruling T6-8): any other command, the api and a worker-role
        # command such as `schema-dry-run` among them, runs untraced whatever its environment says. Imported
        # here, like each command's own modules.
        from jobhunter.observability.langsmith import disable_tracing

        disable_tracing()
    if args.command == "startup-check":
        startup_or_exit(args.role)
        return 0
    if args.command == "migrate":
        # Imported here: migrate pulls in procrastinate, langgraph and alembic, which `startup-check` (the
        # healthcheck path of every container) must not load.
        from jobhunter.db.migrate import SchemaAheadIncompatible, run_migrate

        db_role: DbRole = args.db_role
        try:
            run_migrate(
                startup_or_exit(MIGRATE_CONFIG_ROLES[db_role]),
                steps=args.steps,
                database=args.database,
                manifest_dir=args.manifest_dir,
                cv_dir=args.cv_dir,
                exports_dir=args.exports_dir,
                db_role=db_role,
            )
        except SchemaAheadIncompatible:
            return 1  # logged (`migrate_schema_ahead_incompatible`): the RUNBOOK rolls back by restore
        return 0
    if args.command == "manifests":
        # Imported here, like `migrate`: `manifests replay` is its steps 1, 3 and 7 (§19.4), as the migrator.
        from jobhunter.db.migrate import SchemaAheadIncompatible, run_migrate

        try:
            run_migrate(
                startup_or_exit("migrate"),
                steps=frozenset({"sequences", "replay"}),
                manifest_dir=args.manifest_dir,
            )
        except SchemaAheadIncompatible:
            return 1  # logged, as for `migrate`
        return 0
    if args.command == "api":
        # Imported here: the api command needs uvicorn, and the app it serves FastAPI and the database layer.
        from jobhunter.cli_api import run_api

        return run_api(args)
    if args.command == "worker":
        # Imported here, like `migrate`: the worker pulls in procrastinate and langgraph.
        import asyncio

        from jobhunter.worker.main import worker_main

        raise SystemExit(asyncio.run(worker_main(startup_or_exit("worker"))))
    if args.command == "worker-health":
        return _worker_health()
    if args.command == "http-check":
        return _http_check(args.url)
    if args.command == "user":
        # Imported here: the user commands need argon2, pyotp, segno and the database layer.
        from jobhunter.cli_user import run_user_command

        return run_user_command(parser, args)
    if args.command == "sessions":
        # Imported here, like `user`: the sessions commands need the database layer.
        from jobhunter.cli_sessions import run_sessions_command

        return run_sessions_command(args)
    if args.command == "schema-dry-run":
        # Imported here: the dry-run needs the gateway, the SDK and the database layer.
        from jobhunter.cli_schema_dry_run import run_schema_dry_run

        return run_schema_dry_run(args)
    if args.command == "golden":
        # Imported here: the golden set needs the database layer and the scoring modules.
        from jobhunter.cli_golden import run_golden_command

        return run_golden_command(args)
    if args.command == "eval":
        # Imported here, like `golden`.
        from jobhunter.cli_golden import run_eval_command

        return run_eval_command(args)
    if args.command == "ops":
        # Imported here: the ops commands need httpx, the database layer and the worker's dead-man module.
        from jobhunter.cli_ops import run_ops_command

        return run_ops_command(args)
    if args.command == "backup":
        # Imported here: the backup commands need the restic wrapper, the shipper and the database layer.
        from jobhunter.cli_backup import run_backup_command

        return run_backup_command(args)
    if args.command == "restore":
        # Imported here, like `backup`: the restore needs the restic wrapper, migrate and the database layer.
        from jobhunter.cli_backup import run_restore_command, run_restore_verify_command

        if args.restore_command == "verify":
            return run_restore_verify_command(args)
        return run_restore_command(args)
    if args.command == "crypto":
        # Imported here: the rotation needs the database layer, the CV store and the export format.
        from jobhunter.crypto_rotate import run_rotate_command

        return run_rotate_command(args)
    if args.command == "fixtures":
        # Imported here: only the fixture tooling needs the scrubber.
        from jobhunter.cli_fixtures import run_fixtures_command

        return run_fixtures_command(args)
    parser.error(f"unknown command {args.command!r}")


if __name__ == "__main__":
    raise SystemExit(main())
