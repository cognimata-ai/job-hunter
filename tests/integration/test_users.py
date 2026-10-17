import os
import subprocess
import sys
import unicodedata
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pyotp
import pytest
import sqlalchemy as sa
from jobhunter import cli, cli_user
from jobhunter.auth import totp, users
from jobhunter.auth.passwords import WeakPassword, verify_password
from jobhunter.clock import utcnow
from jobhunter.crypto import DecryptError, decrypt

from tests.support.pg import pg_one, pg_rows

pytestmark = pytest.mark.integration
PW = "Tannenbaum-Ölfass-7"
_INSERT_SESSION = (
    "INSERT INTO sessions (token_hmac,user_id,idle_expires_at,absolute_expires_at) "
    "VALUES ('\\x01',1,now(),now())"
)


async def test_single_user_and_username_rules(engine) -> None:
    async with engine.begin() as c:
        await users.create_user(c, "  Ada Lovelace ", PW, now=utcnow())
        with pytest.raises(users.UserExists):
            await users.create_user(c, "other", PW, now=utcnow())
        assert (await c.execute(sa.text("SELECT 1"))).scalar_one() == 1  # transaction still usable
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT username FROM users"))).scalar_one() == "Ada Lovelace"


async def test_username_match_ignores_case_and_outer_spaces(engine) -> None:  # Review Focus 3
    async with engine.begin() as c:
        await users.create_user(c, "Straße", PW, now=utcnow())
    async with engine.connect() as c:
        assert (
            await users.find_user(c, " STRASSE ") is not None and await users.find_user(c, "strasse2") is None
        )


async def test_username_match_ignores_the_unicode_form(engine) -> None:  # Review Focus 3
    async with engine.begin() as c:
        await users.create_user(c, unicodedata.normalize("NFD", " Müller "), PW, now=utcnow())
    async with engine.connect() as c:
        stored = (await c.execute(sa.text("SELECT username FROM users"))).scalar_one()
        assert stored == unicodedata.normalize("NFC", "Müller")  # stored as NFC, outer spaces gone
        for typed in ("MÜLLER", "Müller", " müller\t", "MÜLLER"):
            assert await users.find_user(c, typed) is not None, typed
        assert await users.find_user(c, "Muller") is None


async def test_username_match_is_canonical_caseless_matching(engine) -> None:
    # Lower case "ΐ" (U+0390) folds to three code points; upper case "Ϊ́" (U+03AA U+0301) folds to two, which
    # are canonically the same letter. Only NFD(casefold(NFD(x))) sees that.
    async with engine.begin() as c:
        await users.create_user(c, "\u0390abc", PW, now=utcnow())
    async with engine.connect() as c:
        assert await users.find_user(c, "\u03aa\u0301ABC") is not None
        assert await users.find_user(c, "\u0390ABD") is None


async def test_username_length_is_three_to_sixty_four_characters_after_normalising(engine) -> None:
    for bad in ("ab", "  ab  ", "", "x" * 65, " " + "x" * 65):
        async with engine.begin() as c:
            with pytest.raises(ValueError, match=r"^username_length$") as caught:
                await users.create_user(c, bad, PW, now=utcnow())
            assert isinstance(caught.value, users.InvalidUsername)
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT count(*) FROM users"))).scalar_one() == 0
    async with engine.begin() as c:
        await users.create_user(c, " " + "x" * 64 + " ", PW, now=utcnow())
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT length(username) FROM users"))).scalar_one() == 64


async def test_a_weak_password_writes_nothing(engine) -> None:
    async with engine.begin() as c:
        for weak in ("elevenchars", "Unbelievable"):
            with pytest.raises(WeakPassword):
                await users.create_user(c, "ada", weak, now=utcnow())
    async with engine.connect() as c:
        assert (await c.execute(sa.text("SELECT count(*) FROM users"))).scalar_one() == 0
    async with engine.begin() as c:
        await users.create_user(c, "ada", PW, now=utcnow())
        await c.execute(sa.text(_INSERT_SESSION))
    async with engine.begin() as c:
        with pytest.raises(WeakPassword):
            await users.set_password(c, "short", now=utcnow())
    async with engine.connect() as c:
        row = await users.find_user(c, "ada")
        assert row is not None and verify_password(row.password_hash, PW)[0]
        assert (
            await c.execute(sa.text("SELECT count(*) FROM sessions WHERE revoked_at IS NULL"))
        ).scalar_one() == 1


async def test_set_password_revokes_sessions(engine) -> None:
    async with engine.begin() as c:
        await users.create_user(c, "ada", PW, now=utcnow())
        await c.execute(sa.text(_INSERT_SESSION))
        assert await users.set_password(c, "Another-Long-Pass-9", now=utcnow()) == 1
    async with engine.connect() as c:  # committed: no live session is left
        assert (
            await c.execute(sa.text("SELECT count(*) FROM sessions WHERE revoked_at IS NULL"))
        ).scalar_one() == 0


async def test_set_password_replaces_the_hash_and_records_the_time(engine) -> None:
    created, changed = datetime(2026, 10, 16, 9, tzinfo=UTC), datetime(2026, 10, 16, 10, tzinfo=UTC)
    async with engine.begin() as c:
        await users.create_user(c, "ada", PW, now=created)
        assert await users.set_password(c, "Another-Long-Pass-9", now=changed) == 0  # no session to revoke
    async with engine.connect() as c:
        row = await users.find_user(c, "ada")
        assert row is not None
        assert verify_password(row.password_hash, "Another-Long-Pass-9")[0]
        assert not verify_password(row.password_hash, PW)[0]
        stored = (await c.execute(sa.text("SELECT password_changed_at FROM users"))).scalar_one()
        assert stored == changed


async def test_without_a_user_set_password_and_totp_changes_raise(engine) -> None:
    async with engine.begin() as c:
        with pytest.raises(users.UserMissing):
            await users.set_password(c, PW, now=utcnow())
        with pytest.raises(users.UserMissing):
            await users.enable_totp(c, totp.new_secret(), data_key=os.urandom(32))
        with pytest.raises(users.UserMissing):
            await users.disable_totp(c)
        assert await users.get_user(c) is None
        assert await users.find_user(c, "ada") is None


async def test_update_hash_only_replaces_the_hash_it_was_computed_from(engine) -> None:
    async with engine.begin() as c:
        await users.create_user(c, "ada", PW, now=utcnow())
        row = await users.get_user(c)
        assert row is not None
        await users.update_hash(c, "$argon2id$rehash-1", expected_hash=row.password_hash)
    async with engine.begin() as c:
        # A rehash computed from the previous hash must not undo a newer one (a password change in between).
        await users.update_hash(c, "$argon2id$stale", expected_hash=row.password_hash)
        current = await users.get_user(c)
        assert current is not None and current.password_hash == "$argon2id$rehash-1"
        await users.update_hash(c, "$argon2id$rehash-2")
        current = await users.get_user(c)
        assert current is not None and current.password_hash == "$argon2id$rehash-2"


async def test_totp_window_and_encrypted_storage(engine) -> None:
    key, secret, now = os.urandom(32), totp.new_secret(), datetime(2026, 10, 16, 10, tzinfo=UTC)
    assert totp.verify(secret, pyotp.TOTP(secret).at(now - timedelta(seconds=30)), at=now)
    assert not totp.verify(secret, pyotp.TOTP(secret).at(now - timedelta(seconds=90)), at=now)
    async with engine.begin() as c:
        await users.create_user(c, "ada", PW, now=utcnow())
        await users.enable_totp(c, secret, data_key=key)
        row = await users.find_user(c, "ada")
    assert row is not None and row.totp_enabled and secret.encode() not in (row.totp_secret_enc or b"")
    assert users.totp_secret(row, keys=[key]) == secret


async def test_the_totp_secret_needs_its_key_and_disabling_clears_it(engine) -> None:
    key, secret = os.urandom(32), totp.new_secret()
    async with engine.begin() as c:
        await users.create_user(c, "ada", PW, now=utcnow())
        row = await users.get_user(c)
        assert row is not None and not row.totp_enabled and users.totp_secret(row, keys=[key]) is None
        await users.enable_totp(c, secret, data_key=key)
        row = await users.get_user(c)
    assert row is not None
    assert users.totp_secret(row, keys=[os.urandom(32), key]) == secret  # a rotation keeps the old key
    with pytest.raises(DecryptError):
        users.totp_secret(row, keys=[os.urandom(32)])
    async with engine.begin() as c:
        await users.disable_totp(c)
        row = await users.get_user(c)
    assert row is not None and not row.totp_enabled and row.totp_secret_enc is None
    assert users.totp_secret(row, keys=[key]) is None


def test_totp_verifies_six_digit_codes_only_and_needs_an_aware_time() -> None:
    secret, now = totp.new_secret(), datetime(2026, 10, 16, 10, tzinfo=UTC)
    code = pyotp.TOTP(secret).at(now)
    assert totp.verify(secret, code, at=now) and totp.verify(secret, code, at=now + timedelta(seconds=30))
    assert not totp.verify(secret, code, at=now + timedelta(seconds=60))
    for malformed in ("", code[:-1], code + "0", f" {code}", "abcdef", "١٢٣٤٥٦"):
        assert not totp.verify(secret, malformed, at=now), malformed
    with pytest.raises(ValueError, match="timezone"):
        totp.verify(secret, code, at=datetime(2026, 10, 16, 10))  # noqa: DTZ001 - the naive time is the point


def test_provisioning_uri_names_the_issuer_and_the_user() -> None:
    secret = totp.new_secret()
    uri = urlsplit(totp.provisioning_uri(secret, "Ada Lovelace"))
    query = parse_qs(uri.query)
    assert uri.scheme == "otpauth" and uri.netloc == "totp"
    assert query["secret"] == [secret] and query["issuer"] == ["Job Hunter"]
    assert "Job%20Hunter:Ada%20Lovelace" in uri.path


# --- the CLI, as a child process against the shared database -----------------------------------------------


def _cli(env: dict[str, str], *args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "jobhunter.cli", *args],
        input=stdin,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )


def test_cli_create_with_stdin_never_echoes(migrated, proc_env) -> None:
    env = proc_env("api", migrated)
    r = subprocess.run(
        [
            sys.executable,
            "-m",
            "jobhunter.cli",
            "user",
            "create",
            "--username",
            "cli-user",
            "--password-stdin",
        ],
        input=PW + "\n",
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0 and PW not in r.stdout + r.stderr
    r = subprocess.run(
        [sys.executable, "-m", "jobhunter.cli", "user", "create", "--username", "x", "--password-stdin"],
        input=PW + "\n",
        env={**env, "JOBHUNTER_ENV": "prod"},
        capture_output=True,
        text=True,
    )
    assert r.returncode == 2


def test_cli_create_stores_a_verifiable_hash_and_refuses_a_second_user(migrated, proc_env) -> None:
    env = proc_env("api", migrated)
    done = _cli(env, "user", "create", "--username", " cli-user ", "--password-stdin", stdin=PW + "\n")
    assert done.returncode == 0, done.stderr
    row = pg_one(migrated, "app", "SELECT username, password_hash FROM users")
    assert row is not None and row[0] == "cli-user"
    assert verify_password(str(row[1]), PW)[0]
    again = _cli(env, "user", "create", "--username", "other", "--password-stdin", stdin=PW + "\n")
    assert again.returncode == 1 and "already exists" in again.stderr
    assert PW not in again.stdout + again.stderr
    assert pg_rows(migrated, "app", "SELECT username FROM users") == [("cli-user",)]


def test_cli_stdin_password_keeps_everything_but_the_newline(migrated, proc_env) -> None:
    env = proc_env("api", migrated)
    spaced = "  Tannenbaum Ölfass 7  "
    assert (
        _cli(env, "user", "create", "--username", "ada", "--password-stdin", stdin=spaced + "\n").returncode
        == 0
    )
    row = pg_one(migrated, "app", "SELECT password_hash FROM users")
    assert row is not None
    assert verify_password(str(row[0]), spaced)[0] and not verify_password(str(row[0]), spaced.strip())[0]


def test_cli_refuses_weak_passwords_and_bad_usernames_without_echoing(migrated, proc_env) -> None:
    env = proc_env("api", migrated)
    for username, password, reason in (
        ("ada", "elevenchars", "too short"),
        ("ada", "MasterBating", "common"),
        ("ab", PW, "username"),
    ):
        done = _cli(env, "user", "create", "--username", username, "--password-stdin", stdin=password + "\n")
        assert done.returncode == 1 and reason in done.stderr, (reason, done.stderr)
        assert password not in done.stdout + done.stderr and "Traceback" not in done.stderr
    assert pg_rows(migrated, "app", "SELECT username FROM users") == []


def test_cli_without_a_terminal_refuses_to_read_a_password(migrated, proc_env) -> None:
    env = proc_env("api", migrated)
    done = subprocess.run(
        [sys.executable, "-m", "jobhunter.cli", "user", "create", "--username", "ada"],
        stdin=subprocess.DEVNULL,
        env=env,
        capture_output=True,
        text=True,
        start_new_session=True,  # no controlling terminal: getpass cannot switch the echo off
        timeout=120,
        check=False,
    )
    assert done.returncode == 1 and "terminal" in done.stderr and "--password-stdin" in done.stderr
    assert pg_rows(migrated, "app", "SELECT username FROM users") == []


def test_cli_set_password_revokes_every_session(migrated, proc_env) -> None:
    env = proc_env("api", migrated)
    assert (
        _cli(env, "user", "set-password", "--password-stdin", stdin=PW + "\n").returncode == 1
    )  # no user yet
    assert (
        _cli(env, "user", "create", "--username", "ada", "--password-stdin", stdin=PW + "\n").returncode == 0
    )
    pg_rows(migrated, "app", _INSERT_SESSION)
    done = _cli(env, "user", "set-password", "--password-stdin", stdin="Another-Long-Pass-9\n")
    assert done.returncode == 0, done.stderr
    assert "1 session" in done.stdout and "Another-Long-Pass-9" not in done.stdout + done.stderr
    assert pg_rows(migrated, "app", "SELECT count(*) FROM sessions WHERE revoked_at IS NULL") == [(0,)]
    row = pg_one(migrated, "app", "SELECT password_hash FROM users")
    assert row is not None and verify_password(str(row[0]), "Another-Long-Pass-9")[0]


def _start_totp_enable(env: dict[str, str]) -> tuple[subprocess.Popen[str], str]:
    """Run `user totp enable`; return the process (waiting for the code) and the secret from its URI."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "jobhunter.cli", "user", "totp", "enable"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
        encoding="utf-8",
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        if line.startswith("otpauth://"):
            return proc, parse_qs(urlsplit(line.strip()).query)["secret"][0]
    proc.kill()
    raise AssertionError(
        f"no provisioning URI; exit {proc.wait()}: {proc.stderr.read() if proc.stderr else ''}"
    )


def _wrong_code(secret: str) -> str:
    """A six-digit code that no step within two steps of now produces."""
    now = utcnow()
    near = {pyotp.TOTP(secret).at(now + timedelta(seconds=30 * step)) for step in range(-2, 3)}
    return next(code for code in ("000000", "111111", "222222") if code not in near)


def test_cli_totp_enable_stores_the_secret_only_when_the_code_verifies(
    migrated, proc_env, make_config
) -> None:
    env = proc_env("api", migrated)
    assert (
        _cli(env, "user", "create", "--username", "ada", "--password-stdin", stdin=PW + "\n").returncode == 0
    )

    proc, secret = _start_totp_enable(env)
    try:
        _, err = proc.communicate(_wrong_code(secret) + "\n", timeout=60)
    finally:
        proc.kill()
    assert proc.returncode == 1 and "nothing was stored" in err and secret not in err
    assert pg_rows(migrated, "app", "SELECT totp_enabled, totp_secret_enc IS NULL FROM users") == [
        (False, True)
    ]

    proc, secret = _start_totp_enable(env)
    try:
        _, err = proc.communicate(pyotp.TOTP(secret).now() + "\n", timeout=60)
    finally:
        proc.kill()
    assert proc.returncode == 0, err
    assert secret not in err  # the secret is shown to the operator once, on stdout, and never logged
    row = pg_one(migrated, "app", "SELECT totp_enabled, totp_secret_enc FROM users")
    assert row is not None and row[0] is True and isinstance(row[1], bytes)
    data_key = make_config("api", migrated).secret_b64("data_key")
    assert decrypt([data_key], row[1], aad=b"totp").decode() == secret

    again = _cli(env, "user", "totp", "enable")
    assert again.returncode == 1 and "already enabled" in again.stderr
    assert _cli(env, "user", "totp", "disable").returncode == 0
    assert pg_rows(migrated, "app", "SELECT totp_enabled, totp_secret_enc IS NULL FROM users") == [
        (False, True)
    ]


FIXED_NOW = datetime(2026, 10, 16, 10, tzinfo=UTC)  # on a 30 s step boundary
FIXED_SECRET = "JBSWY3DPEHPK3PXP"


def test_cli_totp_enable_with_a_fixed_clock_accepts_and_rejects(
    migrated, make_config, monkeypatch, capsys
) -> None:
    """The same command as the end-to-end test, with the time, the secret and the typed code pinned.

    No wall clock is involved: the clock the CLI reads returns `FIXED_NOW`, so the accepted code is one step
    old (inside the window) and the rejected one is three steps old (outside it).
    """
    cfg = make_config("api", migrated)
    pg_rows(
        migrated,
        "app",
        "INSERT INTO users (id, username, password_hash, password_changed_at) VALUES (1, 'ada', 'h', now())",
    )
    typed: list[str] = []
    monkeypatch.setattr(cli_user, "startup_or_exit", lambda role: cfg)
    monkeypatch.setattr(cli_user, "utcnow", lambda: FIXED_NOW)
    monkeypatch.setattr(totp, "new_secret", lambda: FIXED_SECRET)
    monkeypatch.setattr("builtins.input", lambda prompt="": typed.pop(0))
    state = "SELECT totp_enabled, totp_secret_enc IS NULL FROM users"

    typed.append(pyotp.TOTP(FIXED_SECRET).at(FIXED_NOW - timedelta(seconds=90)))
    assert cli.main(["user", "totp", "enable"]) == 1
    shown = capsys.readouterr()
    assert "nothing was stored" in shown.err and FIXED_SECRET not in shown.err
    assert FIXED_SECRET in shown.out  # the operator's terminal sees the URI, once
    assert pg_rows(migrated, "app", state) == [(False, True)]

    typed.append(pyotp.TOTP(FIXED_SECRET).at(FIXED_NOW - timedelta(seconds=30)))
    assert cli.main(["user", "totp", "enable"]) == 0
    shown = capsys.readouterr()
    assert "TOTP is enabled" in shown.out and shown.err == ""
    row = pg_one(migrated, "app", "SELECT totp_enabled, totp_secret_enc FROM users")
    assert row is not None and row[0] is True and isinstance(row[1], bytes)
    assert decrypt([cfg.secret_b64("data_key")], row[1], aad=b"totp").decode() == FIXED_SECRET
    assert typed == []  # each run asked for exactly one code
