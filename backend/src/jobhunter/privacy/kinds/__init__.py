"""The purge kinds' procedures (plan M5 Tasks 12-17): importing this package registers each one in
`privacy.purge.KINDS` (the worker's start imports it, `worker.main._REGISTERING_MODULES`).

- `golden_case`: `golden_case_delete` (Task 12).
"""

from jobhunter.privacy.kinds import golden_case

__all__ = ["golden_case"]
