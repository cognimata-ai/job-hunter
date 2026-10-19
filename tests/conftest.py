# `tests.support.sources` imports `tests.support.collect` (Task 15), so `collect` is registered first: a
# plugin module must not be imported before pytest registers it, or its asserts are not rewritten.
pytest_plugins = ["tests.support.fixtures", "tests.support.collect", "tests.support.sources"]
