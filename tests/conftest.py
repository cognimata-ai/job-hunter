# `tests.support.sources` imports `tests.support.collect` (Task 15), so `collect` is registered first: a
# plugin module must not be imported before pytest registers it, or its asserts are not rewritten. For the
# same reason `tests.support.embedder` (Task 22) comes before `tests.support.fixtures`, which imports it.
# `tests.support.llm` (M2 Task 3) imports `tests.support.m2` and `tests.support.pg`, neither a plugin, and
# its fixtures use those of `tests.support.fixtures`, so it comes after them. `tests.support.profile` (M2 Task
# 8) imports `docparse.worker`, no plugin, and comes after every plugin module it may import.
# `tests.support.settings` (M2 Task 17) imports `tests.support.profile`, so it comes after it.
# `tests.support.jobs_list` (M4 Task 4) imports `tests.support.m2` and `tests.support.m3`, no plugin, and its
# fixture uses `engine` of `tests.support.fixtures`; it comes last.
pytest_plugins = [
    "tests.support.embedder",
    "tests.support.fixtures",
    "tests.support.collect",
    "tests.support.sources",
    "tests.support.llm",
    "tests.support.profile",
    "tests.support.settings",
    "tests.support.jobs_list",
]
