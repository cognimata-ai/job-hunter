"""Shared pytest fixtures; later tasks add their fixtures here."""

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol

import pytest
import structlog
from jobhunter.config import AppConfig, Role
from jobhunter.observability import redact

from tests.support.pg import PgServer, config_for, process_env, remove_pg_server, start_pg_server

_QUIET_LOGGERS = ("httpx", "httpcore")


@pytest.fixture(autouse=True)
def _restore_logging_state() -> Iterator[None]:
    """`configure_logging` changes process-wide state; give every test the state it found."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    quiet_levels = {name: logging.getLogger(name).level for name in _QUIET_LOGGERS}
    structlog_config = structlog.get_config()
    bound_context = structlog.contextvars.get_contextvars()
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    for name, quiet_level in quiet_levels.items():
        logging.getLogger(name).setLevel(quiet_level)
    structlog.configure(**structlog_config)
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(**bound_context)
    redact._reset_secret_values()


class ConfigFactory(Protocol):
    def __call__(self, role: Role, server: PgServer | None = None, **env: str) -> AppConfig: ...


class ProcessEnvFactory(Protocol):
    def __call__(self, role: Role, server: PgServer | None = None, **extra: str) -> dict[str, str]: ...


@pytest.fixture(scope="session")
def pg_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[PgServer]:
    """One initialised PostgreSQL container for the whole session; removed at the end even if tests failed."""
    server = start_pg_server(tmp_path_factory.mktemp("pg"))
    try:
        yield server
    finally:
        remove_pg_server(server)


@pytest.fixture
def fresh_pg_server(tmp_path: Path) -> Iterator[PgServer]:
    """A PostgreSQL container of its own, with a clean database, for one test."""
    server = start_pg_server(tmp_path / "pg")
    try:
        yield server
    finally:
        remove_pg_server(server)


@pytest.fixture
def make_config(request: pytest.FixtureRequest) -> ConfigFactory:
    """`make_config(role, server=None, **env)`: the `AppConfig` of `role`, by default against `pg_server`."""

    def make(role: Role, server: PgServer | None = None, **env: str) -> AppConfig:
        # Resolved only when no server is given, so a test with its own server does not start the shared one.
        return config_for(server or request.getfixturevalue("pg_server"), role, **env)

    return make


@pytest.fixture
def proc_env(request: pytest.FixtureRequest) -> ProcessEnvFactory:
    """`proc_env(role, server=None, **extra)`: the environment of a child process of `role`."""

    def make(role: Role, server: PgServer | None = None, **extra: str) -> dict[str, str]:
        return process_env(server or request.getfixturevalue("pg_server"), role, **extra)

    return make
