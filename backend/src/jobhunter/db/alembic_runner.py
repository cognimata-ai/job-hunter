"""Alembic entry points.

The configuration is built in code. There is no `alembic.ini` and no repository-relative path: the migrations
are package data next to this module, so an installed (non-editable) wheel finds them too.
"""

from pathlib import Path
from typing import Final

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

_MIGRATIONS_DIR: Final = Path(__file__).resolve().parent / "migrations"


def _set_option(config: Config, name: str, value: str) -> None:
    # The Config stores options in a ConfigParser, which reads "%" as interpolation. A DSN has "%XX" escapes.
    config.set_main_option(name, value.replace("%", "%%"))


def _script_config() -> Config:
    config = Config()
    _set_option(config, "script_location", str(_MIGRATIONS_DIR))
    return config


def alembic_config(dsn: str) -> Config:
    """The Alembic `Config` for the migrations of this package against `dsn` (`postgresql+psycopg://...`).

    The DSN carries the database password: it is stored as the `sqlalchemy.url` option for `env.py` and is
    never logged.
    """
    config = _script_config()
    _set_option(config, "sqlalchemy.url", dsn)
    return config


def upgrade_head(dsn: str) -> None:
    """Run every pending migration (synchronous; async callers use `asyncio.to_thread`)."""
    command.upgrade(alembic_config(dsn), "head")


def code_head() -> str:
    """The head revision of the migrations in this package."""
    head = ScriptDirectory.from_config(_script_config()).get_current_head()
    if head is None:
        raise RuntimeError("the migrations directory has no revision")
    return head
