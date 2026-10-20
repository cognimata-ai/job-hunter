#!/usr/bin/env python3
"""The embedder's Compose healthcheck (spec §17.5, §19.1), installed as /usr/local/bin/embedder-health.

GET http://127.0.0.1:8080/health within 3 s; exit 0 iff the answer is a 200 with `"checksum_ok": true`,
otherwise 1. Standard library only, and no proxy: the check is a loopback request.
"""

import json
import sys
import urllib.error
import urllib.request
from typing import Final

HEALTH_URL: Final = "http://127.0.0.1:8080/health"
TIMEOUT_S: Final = 3.0


def check(url: str = HEALTH_URL, timeout: float = TIMEOUT_S) -> int:
    """0 when `url` answers 200 with `checksum_ok` true, 1 for anything else (an error status, no answer)."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as response:
            status = response.status
            body = json.loads(response.read())
    except urllib.error.HTTPError as exc:  # an error status, e.g. the 503 of a failed checksum
        exc.close()
        return 1
    except (OSError, ValueError):  # no answer (URLError is an OSError), or a body that is not JSON
        return 1
    return 0 if status == 200 and isinstance(body, dict) and body.get("checksum_ok") is True else 1


def main() -> None:
    sys.exit(check())


if __name__ == "__main__":
    main()
