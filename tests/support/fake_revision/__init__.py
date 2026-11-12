"""A newer release's schema, for the code under test to be the older release (plan M5 Task 27).

This directory is a test-only Alembic version location: `9999_test_ahead.py` (`FAKE_HEAD`) revises this
package's head and declares it as its floor, not destructive. Alembic sees it only through `newer_config`,
whose version locations are this package's and this directory; the package's own configuration never loads
it, so the code under test keeps its own chain and head. (Alembic skips `__init__.py` in a version location.)

`migrate_newer_release` leaves a database as a newer release's `migrate` would: this package's `migrate`, then
the schema upgraded to `FAKE_HEAD` and the newer release's chain recorded in `schema_compat`, `FAKE_HEAD`'s
floor among it.
"""

import asyncio
import os
from pathlib import Path
from types import ModuleType
from typing import Final

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from jobhunter.db.alembic_runner import alembic_config
from jobhunter.db.compat import CompatInfo, compat_of, record_compat
from jobhunter.db.migrate import run_migrate_async
from jobhunter.db.session import make_null_engine

from tests.support.pg import PgServer, config_for

FAKE_HEAD: Final = "9999_test_ahead"
FAKE_TABLE: Final = "test_ahead"
FAKE_DIR: Final = Path(__file__).resolve().parent


def newer_config(dsn: str) -> Config:
    """The Alembic configuration of the newer release against `dsn`: this package's revisions and
    `FAKE_HEAD`."""
    config = alembic_config(dsn)
    versions = ScriptDirectory.from_config(config).versions
    config.set_main_option("path_separator", "os")
    config.set_main_option("version_locations", os.pathsep.join((versions, str(FAKE_DIR))))
    return config


def newer_chain(config: Config) -> tuple[CompatInfo, ...]:
    """The newer release's chain, base first, as its `migrate` records it (`db.compat.compat_of`)."""
    scripts = ScriptDirectory.from_config(config).walk_revisions()  # from the head down
    modules: list[ModuleType] = [script.module for script in reversed(list(scripts))]
    return compat_of(modules)


async def migrate_newer_release(server: PgServer) -> None:
    """`server`'s database as a newer release's `migrate` leaves it: at `FAKE_HEAD`, its chain recorded."""
    await run_migrate_async(config_for(server, "migrate"))
    dsn = server.dsn("migrator", driver="sqlalchemy")
    config = newer_config(dsn)
    await asyncio.to_thread(command.upgrade, config, "heads")
    engine = make_null_engine(dsn)
    try:
        async with engine.begin() as conn:
            await record_compat(conn, chain=newer_chain(config))
    finally:
        await engine.dispose()
