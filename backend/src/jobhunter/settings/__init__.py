"""The settings document (spec §13): the typed model (`model`), its validation (`validate`) and the immutable
rows derived from it (`derive`).

Nothing is imported here: `run_settings` imports `model`, and `validate` imports `quota.dryrun`, which imports
`run_settings`, so importing `validate` with the package would close that cycle.
"""
