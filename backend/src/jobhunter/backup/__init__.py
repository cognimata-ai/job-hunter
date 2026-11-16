"""The backup container's jobs (spec §19.5; plan M5 Task 10, Deviations 28-30): the child-process runner, the
restic wrapper and the deletion-manifest shipper, run as the `backup` and `restore` roles (`cli_backup`).

The package writes only its own tables (`manifest_shipments` and its items here; the backup sets and runs in
Tasks 24 and 26) and never an alert: the worker derives the backup alerts from those tables
(`observability.backup_health`), so nothing here imports `observability.alerts` (O1).
"""
