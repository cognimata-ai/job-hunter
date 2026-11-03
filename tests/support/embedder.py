"""The fake embedder (Task 22): Task 21's HTTP contract, served from the test process.

`FakeEmbedder` is a `ThreadingHTTPServer` on `127.0.0.1:0` that answers `GET /health` and `POST /v1/embed` as
`embedder/app.py` does (its field names, its 413 and 422 refusals, a 503 health when the checksum failed),
with the answers of the service's own test-only model, `embedder.app.FakeModel` (M4 Task 18: one
implementation, which the stack's fake embedder serves too): a deterministic unit vector per text, seeded from
the sha256 of the text the model would see (a query with its `query: ` prefix), a text over 4,000 characters
flagged `truncated` and counted 1,024 tokens, the service's own truncation, and every other text a token per
4 characters. Its switches:

- `down`: every request is refused: the server closes each connection at once, without an answer, so the
  client sees a transport error, as for a refused connection, while the port stays bound and the URL every
  configuration carries stays valid;
- `checksum_ok`: `False` gives a 503 health (`checksum_ok: false`) and a 503 for every embed request, the
  answers of a service whose model failed its check;
- `dim` and `model_id`: what every embed answer carries (a wrong dimension, a foreign model);
- `batches`: the size of every embed request it received while up, in order;
- `connections`: how many connections it accepted, down or up (a refused one counts too), so a test can
  assert that a code path never reached it.

One server per session (`fake_embedder_server`): `make_config` and `proc_env` pass its `base_url` as
`JOBHUNTER_EMBEDDER_URL` to every configuration and every child process. The function fixture
`fake_embedder` hands it to a test after `reset()` and resets it again after the test, so no switch leaks
into the next one.
"""

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socket import socket
from typing import Any, Final

import pytest
from jobhunter.embeddings import EMBEDDING_DIM, EMBEDDING_MODEL_ID, MAX_TEXT_BYTES

from embedder.app import QUERY_PREFIX, FakeModel, fake_vector

__all__ = ["QUERY_PREFIX", "FakeEmbedder", "fake_embedder", "fake_embedder_server", "fake_vector"]

MAX_BATCH: Final = 16


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, fake: "FakeEmbedder") -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.fake = fake

    def verify_request(self, request: socket | tuple[bytes, socket], client_address: Any) -> bool:
        # A refused request is closed by the server at once (`shutdown_request`), without an answer.
        self.fake.connected()
        return not self.fake.down


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def log_message(self, format: str, *args: Any) -> None:
        pass  # quiet: the test output stays clean

    def _answer(self, status: int, body: object) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        fake = self.server.fake
        if self.path != "/health":
            self._answer(404, {"detail": "Not Found"})
            return
        body = {
            "status": "ok" if fake.checksum_ok else "error",
            "model_id": fake.model_id,
            "checksum_ok": fake.checksum_ok,
            "dim": fake.dim,
        }
        self._answer(200 if fake.checksum_ok else 503, body)

    def do_POST(self) -> None:
        fake = self.server.fake
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        if self.path != "/v1/embed":
            self._answer(404, {"detail": "Not Found"})
            return
        try:
            request = json.loads(raw)
        except ValueError:
            self._answer(422, {"detail": "not JSON"})
            return
        texts = request.get("texts") if isinstance(request, dict) else None
        kind = request.get("input_kind") if isinstance(request, dict) else None
        if kind not in ("query", "passage") or not isinstance(texts, list) or not texts:
            self._answer(422, {"detail": "bad request"})
            return
        if not all(isinstance(text, str) for text in texts):
            self._answer(422, {"detail": "texts must be strings"})
            return
        fake.record(len(texts))
        if len(texts) > MAX_BATCH or any(len(t.encode("utf-8")) > MAX_TEXT_BYTES for t in texts):
            self._answer(413, {"detail": "too large"})
            return
        if not fake.checksum_ok:
            self._answer(503, {"detail": "the model failed its checksum"})
            return
        vectors, truncated, tokens = FakeModel(dim=fake.dim).embed(texts, kind)
        self._answer(
            200, {"model_id": fake.model_id, "vectors": vectors, "truncated": truncated, "tokens": tokens}
        )


class FakeEmbedder:
    """The embedder's HTTP contract on `base_url`, with the switches of the module docstring."""

    def __init__(self) -> None:
        self.down = False
        self.checksum_ok = True
        self.dim = EMBEDDING_DIM
        self.model_id = EMBEDDING_MODEL_ID
        self.batches: list[int] = []
        self.connections = 0
        self._lock = threading.Lock()
        self._server = _Server(self)
        host, port = self._server.server_address[:2]
        self.base_url = f"http://{host!s}:{port}"
        self._thread = threading.Thread(target=self._server.serve_forever, name="fake-embedder", daemon=True)
        self._thread.start()

    def record(self, size: int) -> None:
        with self._lock:
            self.batches.append(size)

    def connected(self) -> None:
        with self._lock:
            self.connections += 1

    def reset(self) -> None:
        """Up, with a good checksum, the pinned model and dimension, and no batch or connection recorded."""
        self.down = False
        self.checksum_ok = True
        self.dim = EMBEDDING_DIM
        self.model_id = EMBEDDING_MODEL_ID
        with self._lock:
            self.batches.clear()
            self.connections = 0

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@pytest.fixture(scope="session")
def fake_embedder_server() -> Iterator[FakeEmbedder]:
    """The session's one `FakeEmbedder`; `make_config` and `proc_env` carry its URL."""
    server = FakeEmbedder()
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def fake_embedder(fake_embedder_server: FakeEmbedder) -> Iterator[FakeEmbedder]:
    """The session's `FakeEmbedder`, reset before the test and again after it."""
    fake_embedder_server.reset()
    try:
        yield fake_embedder_server
    finally:
        fake_embedder_server.reset()
