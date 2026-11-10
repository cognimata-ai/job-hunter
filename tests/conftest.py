from collections.abc import Iterator

import pytest
from langsmith import run_trees
from langsmith._internal import _context

# `tests.support.sources` imports `tests.support.collect` (Task 15), so `collect` is registered first: a
# plugin module must not be imported before pytest registers it, or its asserts are not rewritten. For the
# same reason `tests.support.embedder` (Task 22) comes before `tests.support.fixtures`, which imports it.
# `tests.support.llm` (M2 Task 3) imports `tests.support.m2` and `tests.support.pg`, neither a plugin, and
# its fixtures use those of `tests.support.fixtures`, so it comes after them. `tests.support.profile` (M2 Task
# 8) imports `docparse.worker`, no plugin, and comes after every plugin module it may import.
# `tests.support.settings` (M2 Task 17) imports `tests.support.profile`, so it comes after it.
# `tests.support.jobs_list` (M4 Task 4) imports `tests.support.m2` and `tests.support.m3`, no plugin, and its
# fixture uses `engine` of `tests.support.fixtures`. `tests.support.purges` (M5 Task 13, the purge harness)
# imports `tests.support.pg`, no plugin, and its fixtures use those of `tests.support.fixtures`; it comes
# last.
pytest_plugins = [
    "tests.support.embedder",
    "tests.support.fixtures",
    "tests.support.collect",
    "tests.support.sources",
    "tests.support.llm",
    "tests.support.profile",
    "tests.support.settings",
    "tests.support.jobs_list",
    "tests.support.purges",
]


@pytest.fixture(autouse=True)
def _langsmith_switch_restored() -> Iterator[None]:
    """`cli.main` turns the LangSmith SDK's tracing switch off for every command but `worker` (M5 Task 6,
    Ruling T6-8): a test that runs a command must not leave it so for the tests after it. The switch (its
    context variable and its process-wide value) and the process-wide client are put back as they were."""
    enabled, global_enabled, client = (
        _context._TRACING_ENABLED.get(),
        _context._GLOBAL_TRACING_ENABLED,
        run_trees._CLIENT,
    )
    yield
    _context._TRACING_ENABLED.set(enabled)
    _context._GLOBAL_TRACING_ENABLED = global_enabled
    run_trees._CLIENT = client
