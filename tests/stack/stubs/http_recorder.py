"""The ntfy and healthchecks stand-in of the compose test stack (plan M5 Task 21; `stubsvc` of
deploy/compose.test.yaml), standard library only, run as `python -I /stub/http_recorder.py --port 8099`.

What it answers, on the worker's `egress` network:

- `POST /ntfy/<topic>`: 200, as ntfy accepts a push;
- `GET /hc/ping/<uuid>` and `GET /hc/ping/<uuid>/fail`: 200 `OK`, as healthchecks answers a ping; a ping
  resumes a paused check, as healthchecks does;
- `POST /hc/api/v3/checks/<uuid>` with `{"timeout", "grace"}`: stores both; it and `GET` of the same path
  answer the check as JSON: `timeout`, `grace` (healthchecks' defaults, 86400 and 3600, until a POST) and
  `status`, `up` or `paused`;
- `POST /hc/api/v3/checks/<uuid>/pause`: 200, the check `paused`;
- `GET /_log`: every request recorded since the start or the last reset, oldest first (the two control routes
  excepted): its method and path, its `Title`, `Priority`, `Tags` and `Click` headers, whether it carried an
  `Authorization` or an `X-Api-Key` header, and its body's length. Never a header's value of those two, and
  never a body;
- `POST /_reset`: forgets the log and the checks.

Anything else is a 404. It logs nothing itself (the request lines of `http.server` would name the client).
"""

import argparse
import json
import re
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Final

NTFY: Final = re.compile(r"/ntfy/[A-Za-z0-9_-]{1,64}")
PING: Final = re.compile(r"/hc/ping/([0-9a-f-]{36})(/fail)?")
CHECK: Final = re.compile(r"/hc/api/v3/checks/([0-9a-f-]{36})(/pause)?")
RECORDED_HEADERS: Final = ("Title", "Priority", "Tags", "Click")
DEFAULT_TIMEOUT_S: Final = 86400
DEFAULT_GRACE_S: Final = 3600
MAX_BODY_BYTES: Final = 1 << 20

_lock = threading.Lock()
_log: list[dict[str, object]] = []
_checks: dict[str, dict[str, object]] = {}


def _check(uuid: str) -> dict[str, object]:
    """The stored check of `uuid`, made with healthchecks' defaults on first use (under `_lock`)."""
    return _checks.setdefault(uuid, {"timeout": DEFAULT_TIMEOUT_S, "grace": DEFAULT_GRACE_S, "status": "up"})


class Recorder(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        pass

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(min(length, MAX_BODY_BYTES))

    def _answer(self, status: int, payload: object | None = None, *, text: str | None = None) -> None:
        if text is not None:
            body, kind = text.encode(), "text/plain; charset=utf-8"
        else:
            body, kind = json.dumps(payload if payload is not None else {}).encode(), "application/json"
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _record(self, body: bytes) -> None:
        entry: dict[str, object] = {"method": self.command, "path": self.path}
        entry.update({name: self.headers.get(name) for name in RECORDED_HEADERS})
        entry["authorization"] = self.headers.get("Authorization") is not None
        entry["api_key"] = self.headers.get("X-Api-Key") is not None
        entry["body_length"] = len(body)
        with _lock:
            _log.append(entry)

    def do_GET(self) -> None:
        if self.path == "/_log":
            with _lock:
                self._answer(200, list(_log))
            return
        self._record(b"")
        if ping := PING.fullmatch(self.path):
            with _lock:
                _check(ping.group(1))["status"] = "up"
            self._answer(200, text="OK")
        elif (check := CHECK.fullmatch(self.path)) and check.group(2) is None:
            with _lock:
                self._answer(200, dict(_check(check.group(1))))
        else:
            self._answer(404, {"error": "not found"})

    def do_POST(self) -> None:
        body = self._body()
        if self.path == "/_reset":
            with _lock:
                _log.clear()
                _checks.clear()
            self._answer(200)
            return
        self._record(body)
        if NTFY.fullmatch(self.path):
            self._answer(200, {"event": "message"})
        elif check := CHECK.fullmatch(self.path):
            self._update_check(check.group(1), pause=check.group(2) is not None, body=body)
        else:
            self._answer(404, {"error": "not found"})

    def _update_check(self, uuid: str, *, pause: bool, body: bytes) -> None:
        if pause:
            with _lock:
                _check(uuid)["status"] = "paused"
                self._answer(200, dict(_checks[uuid]))
            return
        try:
            fields = json.loads(body or b"{}")
            timeout, grace = int(fields["timeout"]), int(fields["grace"])
        except (ValueError, KeyError, TypeError):
            self._answer(400, {"error": "timeout and grace are required"})
            return
        with _lock:
            stored = _check(uuid)
            stored.update(timeout=timeout, grace=grace)
            self._answer(200, dict(stored))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8099)
    args = parser.parse_args()
    # As PID 1 the process would ignore a SIGTERM it has no handler for, and a stop would wait for the kill.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    server = ThreadingHTTPServer(("0.0.0.0", args.port), Recorder)  # noqa: S104 - the stand-in's network
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
