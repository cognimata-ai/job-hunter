"""The autouse fixture that gives every test of a directory a clean shared database when it uses one.

It is defined once here and imported by `tests/integration/conftest.py` and `tests/crash/conftest.py`. It is
not part of the `pytest_plugins` of the root conftest, because an autouse fixture registered there would apply
to every test, unit tests included.
"""

import pytest


@pytest.fixture(autouse=True)
def _clean_database_when_migrated(request: pytest.FixtureRequest) -> None:
    """Every test that uses the shared migrated database gets it empty (`clean_db`), and leaves it empty."""
    if "migrated" in request.fixturenames:
        request.getfixturevalue("clean_db")
