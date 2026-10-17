"""Heartbeat thresholds that more than one layer reads (spec §3.3.4).

A neutral module: the worker singleton (`worker.lock`, the takeover rule) and readiness
(`observability.health`) share the worker heartbeat's staleness without readiness importing the worker.
"""

from typing import Final

# The worker heartbeat (`worker_heartbeat.heartbeat_at`, written by the lock session every 10 s) is stale
# after this many seconds, measured on the database clock: a takeover may then terminate the holder
# (§3.3.1), and `/readyz` fails (§17.5).
HEARTBEAT_STALE_S: Final = 60
