"""The `user` commands (spec §16.2): `create`, `set-password`, `totp enable`, `totp disable`.

`cli.main` imports this module only when a `user` command runs: it needs argon2, pyotp, segno and the database
layer, which `startup-check` and the health probes (every container's healthcheck path) must not load.

The commands start like the api role does (the data key and the application database role). Passwords, TOTP
secrets and codes never reach a log line: messages go to stderr and name rules and exception classes only, and
`totp enable` shows the provisioning URI and the QR code on stdout, the operator's terminal.
"""

import argparse
import asyncio
import getpass
import sys
import warnings
from collections.abc import Awaitable, Callable
from typing import BinaryIO

import segno
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection

from jobhunter.auth import totp, users
from jobhunter.auth.passwords import MIN_LENGTH, WeakPassword
from jobhunter.clock import utcnow
from jobhunter.config import AppConfig, startup_or_exit
from jobhunter.db.session import make_engine


def _complain(message: str) -> None:
    """One line on stderr. Messages name rules and classes, never a password, a secret or a username."""
    print(f"jobhunter: {message}", file=sys.stderr)


def _read_stdin_password(stream: BinaryIO) -> str | None:
    """The first line of `stream` as UTF-8, without its newline and nothing else.

    Spaces and tabs at either end belong to the password. A CRLF counts as one newline (a pipe from
    PowerShell ends its lines with it). `None`, after a message, when there is no line or it is not UTF-8.
    """
    raw = stream.readline()
    if not raw:
        _complain("no password on stdin")
        return None
    try:
        line = raw.decode("utf-8")
    except UnicodeDecodeError:
        _complain("the password on stdin is not valid UTF-8")
        return None
    if line.endswith("\n"):
        line = line.removesuffix("\n").removesuffix("\r")
    return line


def _prompt_new_password() -> str | None:
    """Ask for a password twice with `getpass`; `None`, after a message, unless both entries are the same.

    Nothing is echoed. When `getpass` cannot switch the echo off it would warn and read with the echo on;
    that warning is an error here, raised before any input is read.
    """
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            first = getpass.getpass("New password: ")
            second = getpass.getpass("Repeat the password: ")
    except getpass.GetPassWarning:
        _complain(
            "no terminal to read a password from without echo; "
            "use --password-stdin (not in prod) or run with a terminal (docker compose run without -T)"
        )
        return None
    except UnicodeDecodeError:
        # The terminal delivered bytes that are not valid text. The error carries them; the message does not.
        _complain("the password is not valid UTF-8")
        return None
    except (EOFError, KeyboardInterrupt):
        _complain("aborted")
        return None
    if first != second:
        _complain("the passwords do not match")
        return None
    return first


def _in_transaction[T](cfg: AppConfig, work: Callable[[AsyncConnection], Awaitable[T]]) -> T:
    """Run `work` in one transaction as the application role; it commits unless `work` raises."""

    async def run() -> T:
        engine = make_engine(cfg, pool_size=1)
        try:
            async with engine.begin() as conn:
                return await work(conn)
        finally:
            await engine.dispose()

    return asyncio.run(run())


def run_user_command(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    """`user create | set-password | totp enable | totp disable`; the exit status is the outcome.

    A database error prints its class only: SQLAlchemy puts the bound parameters, a password hash among
    them, into its messages.
    """
    from_stdin = args.user_command != "totp" and args.password_stdin
    if from_stdin and sys.stdin.isatty():
        # Before the configuration is loaded: a usage error, whatever the environment.
        parser.error(
            "--password-stdin reads a pipe, but stdin is a terminal; omit it (the prompt does not echo)"
        )
    cfg = startup_or_exit("api")
    try:
        if args.user_command == "totp":
            return _totp_enable(cfg) if args.totp_command == "enable" else _totp_disable(cfg)
        if from_stdin and cfg.env == "prod":
            parser.error("--password-stdin is refused when JOBHUNTER_ENV=prod")
        password = _read_stdin_password(sys.stdin.buffer) if from_stdin else _prompt_new_password()
        if password is None:
            return 1
        if args.user_command == "create":
            return _user_create(cfg, args.username, password)
        return _user_set_password(cfg, password)
    except users.UserMissing:
        _complain("no user exists yet; create one with `user create`")
    except SQLAlchemyError as exc:
        _complain(f"database error ({type(exc).__name__})")
    return 1


def _rejection(exc: WeakPassword | users.InvalidUsername) -> str:
    """What to tell the operator about a refused password or username."""
    if isinstance(exc, users.InvalidUsername):
        return f"the username must be {users.USERNAME_MIN_LENGTH} to {users.USERNAME_MAX_LENGTH} characters"
    if exc.reason == "too_short":
        return f"the password is too short (at least {MIN_LENGTH} characters)"
    return "the password is among the most common passwords"


def _user_create(cfg: AppConfig, username: str, password: str) -> int:
    try:
        _in_transaction(cfg, lambda conn: users.create_user(conn, username, password, now=utcnow()))
    except users.UserExists:
        _complain("a user already exists; there is only one (`user set-password` changes the password)")
        return 1
    except (WeakPassword, users.InvalidUsername) as exc:
        _complain(_rejection(exc))
        return 1
    print("user created")
    return 0


def _user_set_password(cfg: AppConfig, password: str) -> int:
    try:
        revoked = _in_transaction(cfg, lambda conn: users.set_password(conn, password, now=utcnow()))
    except WeakPassword as exc:
        _complain(_rejection(exc))
        return 1
    print(f"password changed; revoked {revoked} session(s)")
    return 0


def _totp_enable(cfg: AppConfig) -> int:
    """Show a new secret (URI and QR code, on stdout) and store it once a code from the app verifies."""
    user = _in_transaction(cfg, users.get_user)
    if user is None:
        raise users.UserMissing
    if user.totp_enabled:
        _complain("TOTP is already enabled; `user totp disable` turns it off first")
        return 1
    secret = totp.new_secret()
    uri = totp.provisioning_uri(secret, user.username)
    print("Add this account to your authenticator app, by QR code or by URI:")
    print(uri)
    segno.make_qr(uri).terminal(out=sys.stdout, compact=True)
    sys.stdout.flush()
    try:
        code = input("Current code from the app: ").strip()
    except (EOFError, KeyboardInterrupt):
        _complain("aborted; nothing was stored")
        return 1
    step = totp.matched_step(secret, code, at=utcnow())
    if step is None:
        _complain("the code did not verify; nothing was stored")
        return 1
    data_key = cfg.secret_b64("data_key")
    # The code's step is used up by the enrolment, so no login accepts that code again.
    _in_transaction(cfg, lambda conn: users.enable_totp(conn, secret, data_key=data_key, step=step))
    print("TOTP is enabled")
    return 0


def _totp_disable(cfg: AppConfig) -> int:
    _in_transaction(cfg, users.disable_totp)
    print("TOTP is disabled")
    return 0
