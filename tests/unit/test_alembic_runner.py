# tests/unit/test_alembic_runner.py
from pathlib import Path

import pytest
from alembic import command
from jobhunter.db import alembic_runner
from jobhunter.db.alembic_runner import alembic_config

DSN = "postgresql+psycopg://jobhunter_migrator:p%2Bw%25rd@127.0.0.1:5432/jobhunter"


def test_dsn_with_percent_escapes_survives_the_config() -> None:
    # The Config keeps its options in a ConfigParser, which would read "%2B" as interpolation.
    assert alembic_config(DSN).get_main_option("sqlalchemy.url") == DSN


def test_migrations_are_package_data_next_to_the_module() -> None:
    location = Path(alembic_config(DSN).get_main_option("script_location") or "")
    assert location == Path(alembic_runner.__file__).resolve().parent / "migrations"
    assert (location / "env.py").is_file() and (location / "script.py.mako").is_file()
    assert (location / "versions" / "0001_m0_baseline.py").is_file()


def test_offline_mode_is_refused() -> None:
    with pytest.raises(RuntimeError, match="offline migrations are not supported"):
        command.upgrade(alembic_config(DSN), "base:head", sql=True)
