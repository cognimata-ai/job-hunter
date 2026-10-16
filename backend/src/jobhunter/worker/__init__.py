"""The worker process (spec §3.3): the singleton lock session, its watchdog, worker epochs and the queues.

Nothing is imported here: `jobhunter worker-health` (the container HEALTHCHECK) imports `worker.lock` and must
not pay for the rest of the worker.
"""
