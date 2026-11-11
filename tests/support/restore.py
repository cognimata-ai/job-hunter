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

`dump_listing(server, *, source_db=...)` is the `pg_restore --list` output of a dump of `source_db`, the
listing `restore_list` filters. `drop_database(server, name)` drops a database of the test server, its
sessions with it (a test's teardown). `empty_database(server, name)` creates the database a disaster restore
finds: made by the bootstrap superuser from `jobhunter_template` (the extensions, nothing else), owned by the
migrator, as `deploy/postgres/init` leaves an empty `pgdata`'s. `restored_database(server, root)` is such a
database for a test (`RestoredDatabase`: its name, the migrator's engine on it, and empty `cv_store` and
`exports` directories under `root`), dropped at the end; with `create=False` it is only named, for
`dump_and_restore` to create.

`ContainerRunner(server)` (M5 Task 26) is the `CommandRunner` of the backup and restore jobs' PostgreSQL
client (`/opt/pgclient/bin/pg_dump` and `pg_restore`, which exist only in the backup image): it runs the base
name of `argv[0]` inside the server's container, the server's own build. The `--host`/`--port` pairs are
dropped (the command reaches its server on the container's socket); every host file the command reads (an
argument that names an existing file) is copied in first, and its `-f` output copied out after it; the
password reaches it as `docker exec -e PGPASSWORD`, by name only, its value in the docker client's environment
and never on a command line. `calls` records each argv as it was given; `fail` names a program that exits 1
without running.
"""

import asyncio
import os
import re
import secrets
import subprocess
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Final

from jobhunter.backup.restore import restore_list
from jobhunter.backup.runner import CommandResult
from jobhunter.db.session import make_null_engine
from jobhunter.privacy.manifests import FileRoots
from sqlalchemy.ext.asyncio import AsyncEngine

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


def _dump_listing(server: PgServer, source_db: str) -> str:
    dump = f"/tmp/jh-listing-{secrets.token_hex(6)}.dump"  # noqa: S108 (the container's own /tmp)
    try:
        _in_container(server, "pg_dump", "-Fc", *_MIGRATOR, "-d", source_db, "-f", dump)
        return _in_container(server, "pg_restore", "--list", dump)
    finally:
        _in_container(server, "rm", "-f", dump)


async def dump_listing(server: PgServer, *, source_db: str) -> str:
    """The `pg_restore --list` output of a dump of `source_db` (module docstring)."""
    return await asyncio.to_thread(_dump_listing, server, _checked(source_db))


async def dump_and_restore(server: PgServer, *, source_db: str, target_db: str) -> None:
    """Dump `source_db` and restore it into the new database `target_db` (module docstring)."""
    await asyncio.to_thread(_dump_and_restore, server, _checked(source_db), _checked(target_db))


def drop_database(server: PgServer, name: str) -> None:
    """Drop the database `name` of the test server, when it exists, ending its sessions."""
    pg_rows(server, "superuser", f'DROP DATABASE IF EXISTS "{_checked(name)}" WITH (FORCE)', db="postgres")


def empty_database(server: PgServer, name: str) -> None:
    """A database as a disaster restore finds it (module docstring)."""
    pg_rows(
        server,
        "superuser",
        f'CREATE DATABASE "{_checked(name)}" TEMPLATE jobhunter_template OWNER jobhunter_migrator',
        db="postgres",
    )


@dataclass(frozen=True)
class RestoredDatabase:
    """A database a restore loads (module docstring)."""

    name: str
    engine: AsyncEngine
    files: FileRoots


@asynccontextmanager
async def restored_database(
    server: PgServer, root: Path, *, create: bool = True
) -> AsyncIterator[RestoredDatabase]:
    """An empty database of its own (created unless `create` is false) and its empty volumes (module
    docstring); dropped at the end."""
    name = f"jh_restore_{secrets.token_hex(4)}"
    files = FileRoots(cv_store=root / "restored-cv", exports=root / "restored-exports")
    for directory in (files.cv_store, files.exports):
        directory.mkdir(mode=0o700, parents=True)
    if create:
        await asyncio.to_thread(empty_database, server, name)
    engine = make_null_engine(server.dsn("migrator", name, driver="sqlalchemy"))
    try:
        yield RestoredDatabase(name, engine, files)
    finally:
        await engine.dispose()
        drop_database(server, name)


_CLIENT: Final = frozenset({"pg_dump", "pg_restore"})
_SERVER_FLAGS: Final = frozenset({"--host", "--port"})


def _docker(*argv: str, env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["docker", *argv],
        capture_output=True,
        check=False,
        timeout=_DOCKER_TIMEOUT_S,
        env=None if env is None else {**os.environ, **env},
    )


@dataclass
class ContainerRunner:
    """`CommandRunner` of the PostgreSQL client in the test server's container (module docstring)."""

    server: PgServer
    fail: str | None = None
    calls: list[list[str]] = field(default_factory=list)

    async def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        timeout_s: float,
        stdin: bytes | None = None,
    ) -> CommandResult:
        self.calls.append(list(argv))
        return await asyncio.to_thread(self._run, list(argv), dict(env))

    def _run(self, argv: list[str], env: dict[str, str]) -> CommandResult:
        program = PurePath(argv[0]).name
        assert program in _CLIENT, argv
        if program == self.fail:
            return CommandResult(1, b"", "failed as the test asked")
        workdir = f"/tmp/jh-runner-{secrets.token_hex(6)}"  # noqa: S108 (the container's own /tmp)
        assert _docker("exec", self.server.container, "mkdir", "-p", workdir).returncode == 0
        try:
            inside: list[str] = [program]
            outputs: list[tuple[str, str]] = []
            args = iter(argv[1:])
            for arg in args:
                if arg in _SERVER_FLAGS:
                    next(args)  # the container's own server, on its socket
                elif arg == "-f":
                    host = next(args)
                    copy = f"{workdir}/{len(outputs)}-{PurePath(host).name}"
                    inside += [arg, copy]
                    outputs.append((copy, host))
                elif arg.startswith("/") and Path(arg).is_file():
                    copy = f"{workdir}/in-{len(inside)}-{PurePath(arg).name}"
                    copied = _docker("cp", arg, f"{self.server.container}:{copy}")
                    assert copied.returncode == 0, copied.stderr
                    inside.append(copy)
                else:
                    inside.append(arg)
            passed = [flag for name in env for flag in ("-e", name)]  # each value from `env`, by name
            done = _docker("exec", *passed, self.server.container, *inside, env=env)
            for copy, host in outputs:
                if done.returncode == 0:
                    copied = _docker("cp", f"{self.server.container}:{copy}", host)
                    assert copied.returncode == 0, copied.stderr
            return CommandResult(done.returncode, done.stdout, done.stderr[-4096:].decode("utf-8", "replace"))
        finally:
            _docker("exec", self.server.container, "rm", "-rf", workdir)
