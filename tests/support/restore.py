"""A backup set's dump restored into a database of the test server (plan M5 Task 25; spec §19.5 "Restore
order" step 4, §18.2 "Typed replay").

`dump_and_restore(server, *, source_db=..., target_db=...)` dumps `source_db` and restores it into a new
`target_db`, as a restore does, inside the server's container (its `pg_dump` and `pg_restore` are the
server's own build): the superuser creates `target_db` from `jobhunter_template` (the extensions come from
it) owned by the migrator; the migrator dumps (`pg_dump -Fc`) and restores with `pg_restore --no-owner
--no-acl --exit-on-error -L <list>`, the list being `backup.restore.restore_list` of the dump's `pg_restore
--list` (the bootstrap superuser owns the extensions, so `--exit-on-error` would stop at their entries). The
dump and its list are files of the container's own `/tmp`, removed whatever the outcome. The migrator's
password is read from its secret file inside the container (`/run/secrets`), never put on a command line.

`drop_database(server, name)` drops a database of the test server, its sessions with it (a test's teardown).
"""

import asyncio
import re
import secrets
import subprocess
from typing import Final

from jobhunter.backup.restore import restore_list

from tests.support.pg import PgServer, pg_rows

_DOCKER_TIMEOUT_S: Final = 300
_DB_NAME: Final = re.compile(r"[a-z_][a-z0-9_]{0,62}")
# Every command runs as the migrator, its password read inside the container from its secret file.
_AS_MIGRATOR: Final = 'PGPASSWORD="$(cat /run/secrets/db_migrator)" exec "$@"'
_MIGRATOR: Final = ("-U", "jobhunter_migrator")


def _checked(name: str) -> str:
    if not _DB_NAME.fullmatch(name):
        raise ValueError(f"not a plain database name: {name!r}")
    return name


def _in_container(server: PgServer, *argv: str, stdin: str | None = None) -> str:
    """Run `argv` in the server's container as the migrator; its stdout. `RuntimeError` when it fails."""
    done = subprocess.run(
        ["docker", "exec", "-i", server.container, "sh", "-c", _AS_MIGRATOR, "sh", *argv],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=_DOCKER_TIMEOUT_S,
    )
    if done.returncode != 0:
        raise RuntimeError(f"{argv[0]} failed ({done.returncode}): {done.stderr.strip()[-2000:]}")
    return done.stdout


def _dump_and_restore(server: PgServer, source_db: str, target_db: str) -> None:
    pg_rows(
        server,
        "superuser",
        f'CREATE DATABASE "{target_db}" TEMPLATE jobhunter_template OWNER jobhunter_migrator',
        db="postgres",
    )
    stem = f"/tmp/jh-restore-{secrets.token_hex(6)}"  # noqa: S108 (the container's own /tmp)
    dump, listing = f"{stem}.dump", f"{stem}.list"
    try:
        _in_container(server, "pg_dump", "-Fc", *_MIGRATOR, "-d", source_db, "-f", dump)
        entries = restore_list(_in_container(server, "pg_restore", "--list", dump))
        _in_container(server, "sh", "-c", 'cat > "$1"', "sh", listing, stdin="\n".join(entries) + "\n")
        _in_container(
            server,
            "pg_restore",
            "--no-owner",
            "--no-acl",
            "--exit-on-error",
            "-L",
            listing,
            *_MIGRATOR,
            "-d",
            target_db,
            dump,
        )
    finally:
        _in_container(server, "rm", "-f", dump, listing)


async def dump_and_restore(server: PgServer, *, source_db: str, target_db: str) -> None:
    """Dump `source_db` and restore it into the new database `target_db` (module docstring)."""
    await asyncio.to_thread(_dump_and_restore, server, _checked(source_db), _checked(target_db))


def drop_database(server: PgServer, name: str) -> None:
    """Drop the database `name` of the test server, when it exists, ending its sessions."""
    pg_rows(server, "superuser", f'DROP DATABASE IF EXISTS "{_checked(name)}" WITH (FORCE)', db="postgres")
