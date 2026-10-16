"""Crash-test configuration; the PostgreSQL fixtures live in `tests/support/fixtures.py`."""

import pytest


@pytest.fixture(autouse=True)
def _clean_database_when_migrated(request: pytest.FixtureRequest) -> None:
    """Every test that uses the shared migrated database gets it empty (`clean_db`), and leaves it empty."""
    if "migrated" in request.fixturenames:
        request.getfixturevalue("clean_db")
