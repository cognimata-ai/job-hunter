"""`jobhunter api`: the api process (ruling T18-b).

The startup assertions run first, in this process, so a failing one exits 78 like every other role
(`startup_or_exit`). Under uvicorn alone the lifespan would run them, and uvicorn turns any lifespan failure
into exit 3. Only then does uvicorn start, with `log_config=None`: uvicorn installs no handlers of its own, so
its records propagate to the redacting root handler that `configure_logging` installed (rulings T3-h, T14-b).
The app's lifespan loads and checks the configuration once more when uvicorn starts it; both see the same
environment and secret files.

Proxy headers are off unless asked for (uvicorn's own default is on): `--proxy-headers` with
`--forwarded-allow-ips` set to the one trusted forwarder makes uvicorn take the client address from
`X-Forwarded-For` only when the TCP peer is that forwarder (§16.3, V20).

uvicorn's access log is off (M5 Task 21, Ruling T21-1 (b)): it would log every request with its client
address, which is personal data (§20.6 item 2), and Caddy in front keeps no access log either. So are its
WebSockets (`ws="none"`, Ruling T21-3 I1): the api serves none, and with a WebSocket library installed (one
comes with langsmith) uvicorn would answer every upgrade Caddy passes through and log it on
`uvicorn.error`, with the client address and the path with its query string, whatever `access_log` says.
Without one, an upgrade is a plain HTTP request (a 404), and uvicorn's two warnings about it name no address.
The sign-in and re-authentication events record the address with their outcome; nothing else of a request is
logged with it.
"""

import argparse

import uvicorn

from jobhunter.config import startup_or_exit
from jobhunter.observability.logging import configure_logging

APP: str = "jobhunter.api.app:app"


def run_api(args: argparse.Namespace) -> int:
    cfg = startup_or_exit("api")
    configure_logging(cfg.log_level)  # uvicorn's own start-up records already use the configured level
    uvicorn.run(
        APP,
        host=args.host,
        port=args.port,
        proxy_headers=args.proxy_headers,
        forwarded_allow_ips=args.forwarded_allow_ips,
        log_config=None,
        access_log=False,
        ws="none",
    )
    return 0
