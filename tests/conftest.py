# `tests.support.sources` imports `tests.support.collect` (Task 15), so `collect` is registered first: a
# plugin module must not be imported before pytest registers it, or its asserts are not rewritten. For the
# same reason `tests.support.embedder` (Task 22) comes before `tests.support.fixtures`, which imports it.
pytest_plugins = [
    "tests.support.embedder",
    "tests.support.fixtures",
    "tests.support.collect",
    "tests.support.sources",
]
