"""Heartbeat thresholds that more than one layer reads (spec §3.3.4).

A neutral module: the worker singleton (`worker.lock`, the takeover rule) and readiness
(`observability.health`) share the worker heartbeat's staleness without readiness importing the worker.
Readiness and the api's heartbeat watch (`observability.heartbeat_watch`, M5 Task 3) also share how they read
it: `HEARTBEAT_AGE_SQL`, the heartbeat's age on the database clock, which also wrote it, and
`heartbeat_fresh`, the one freshness rule over that age, so `/readyz` and `worker.heartbeat_stale` never
disagree. The query is text, so this module imports no database library: `jobhunter worker-health`, the
container's HEALTHCHECK, imports it through `worker.lock`.
"""

from datetime import timedelta
from typing import Final

# The worker heartbeat (`worker_heartbeat.heartbeat_at`, written by the lock session every 10 s) is stale
# after this many seconds, measured on the database clock: a takeover may then terminate the holder
# (§3.3.1), and `/readyz` fails (§17.5).
HEARTBEAT_STALE_S: Final = 60
HEARTBEAT_STALE: Final = timedelta(seconds=HEARTBEAT_STALE_S)

# The heartbeat's age on the database clock: one row, none without a heartbeat (each reader wraps it, `q`).
HEARTBEAT_AGE_SQL: Final = "SELECT now() - heartbeat_at FROM worker_heartbeat WHERE id = 1"


def heartbeat_fresh(age: timedelta | None) -> bool:
    """Whether a heartbeat `age` old (`HEARTBEAT_AGE_SQL`; `None`: no heartbeat at all) is fresh: at most
    `HEARTBEAT_STALE_S` old."""
    return age is not None and age <= HEARTBEAT_STALE
