# tests/embedder/test_app.py
"""The embedder service (spec §2.1 U3, §3.2, §16.8 check 7, §17.5; Task 21).

The contract tests use a fake model, so they run in the base environment (no onnxruntime, no model). The
`embedder_model` tests load the pinned model from `.model-cache/arctic` (`embedder/download_model.sh`) and
run with `uv run --group embedder pytest tests/embedder -m embedder_model`.
"""

import hashlib
import http.server
import importlib.util
import json
import logging
import math
import os
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from embedder import app as embedder_app
from embedder import health
from embedder.app import (
    DIM,
    MAX_BATCH,
    MAX_TEXT_BYTES,
    MAX_TOKENS,
    QUERY_PREFIX,
    RUNTIME_ENV,
    ChecksumMismatch,
    create_app,
    disable_telemetry,
    locked_onnx_file,
    verify_checksums,
)

ROOT = Path(__file__).resolve().parents[2]
LOCK = ROOT / "embedder" / "model.lock"
MODEL_DIR = ROOT / ".model-cache" / "arctic"
DOWNLOAD = ROOT / "embedder" / "download_model.sh"
SPIKE = ROOT / "scripts" / "spikes" / "v7_embedder.py"


class FakeModel:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str]] = []

    def embed(self, texts: Sequence[str], input_kind: str) -> tuple[list[list[float]], list[bool], list[int]]:
        self.calls.append((list(texts), input_kind))
        vecs = [[1.0] + [0.0] * 1023 for _ in texts]
        return vecs, [len(t) > 40 for t in texts], [min(len(t.split()), 1024) for t in texts]


class ShortVectorModel(FakeModel):
    """A model whose vectors are one dimension short: the service must not pass them on."""

    def embed(self, texts: Sequence[str], input_kind: str) -> tuple[list[list[float]], list[bool], list[int]]:
        vecs, truncated, tokens = super().embed(texts, input_kind)
        return [v[:-1] for v in vecs], truncated, tokens


def test_constants_are_the_spec_values() -> None:
    assert (MAX_TOKENS, MAX_BATCH, MAX_TEXT_BYTES, DIM, QUERY_PREFIX) == (1024, 16, 65_536, 1024, "query: ")


def test_health_and_embed_contract() -> None:
    model = FakeModel()
    client = TestClient(create_app(model, model_id="m@rev/int8", checksum_ok=True))
    assert client.get("/health").json() == {
        "status": "ok",
        "model_id": "m@rev/int8",
        "checksum_ok": True,
        "dim": 1024,
    }
    r = client.post("/v1/embed", json={"input_kind": "passage", "texts": ["short", "x" * 50]})
    body = r.json()
    assert r.status_code == 200
    assert body["model_id"] == "m@rev/int8"
    assert len(body["vectors"]) == 2 and len(body["vectors"][0]) == 1024
    assert body["truncated"] == [False, True] and body["tokens"] == [1, 1]
    assert model.calls[-1] == (["short", "x" * 50], "passage")
    assert client.post("/v1/embed", json={"input_kind": "query", "texts": ["q"]}).status_code == 200
    assert model.calls[-1] == (["q"], "query")
    assert client.post("/v1/embed", json={"input_kind": "passage", "texts": ["x"] * 17}).status_code == 413
    assert client.post("/v1/embed", json={"input_kind": "document", "texts": ["x"]}).status_code == 422
    unhealthy = TestClient(create_app(model, model_id="m", checksum_ok=False)).get("/health")
    assert unhealthy.status_code == 503 and unhealthy.json()["checksum_ok"] is False


def test_embed_size_limits_count_texts_and_utf8_bytes() -> None:
    model = FakeModel()
    client = TestClient(create_app(model, model_id="m", checksum_ok=True))
    full = client.post("/v1/embed", json={"input_kind": "passage", "texts": ["x"] * MAX_BATCH})
    assert full.status_code == 200 and len(full.json()["vectors"]) == MAX_BATCH
    at_limit = client.post("/v1/embed", json={"input_kind": "passage", "texts": ["x" * MAX_TEXT_BYTES]})
    assert at_limit.status_code == 200
    calls = len(model.calls)
    over = client.post(
        "/v1/embed", json={"input_kind": "passage", "texts": ["ok", "x" * (MAX_TEXT_BYTES + 1)]}
    )
    assert over.status_code == 413 and "x" * 10 not in over.text
    # 32,769 characters but 65,538 UTF-8 bytes: the limit counts bytes
    wide = client.post("/v1/embed", json={"input_kind": "query", "texts": ["é" * (MAX_TEXT_BYTES // 2 + 1)]})
    assert wide.status_code == 413
    assert len(model.calls) == calls  # a rejected request never reaches the model


@pytest.mark.parametrize(
    "payload",
    [
        {"input_kind": "passage", "texts": []},
        {"input_kind": "passage"},
        {"texts": ["AD-TEXT"]},
        {"input_kind": "passage", "texts": "AD-TEXT"},
        {"input_kind": "passage", "texts": ["AD-TEXT", 3]},
        {"input_kind": "passage", "texts": [["AD-TEXT"]]},
        {"input_kind": "passage", "texts": ["x"], "normalize": "AD-TEXT"},
        {"input_kind": "Query", "texts": ["AD-TEXT"]},
    ],
)
def test_embed_rejects_malformed_requests_with_422(payload: dict[str, object]) -> None:
    model = FakeModel()
    client = TestClient(create_app(model, model_id="m", checksum_ok=True))
    r = client.post("/v1/embed", json=payload)
    assert r.status_code == 422 and model.calls == []
    assert r.json()["detail"] and "AD-TEXT" not in r.text  # the error names the field, never echoes a text


def test_embed_rejects_a_lone_surrogate_with_422() -> None:
    model = FakeModel()
    client = TestClient(create_app(model, model_id="m", checksum_ok=True))
    body = b'{"input_kind": "passage", "texts": ["ok \\ud800"]}'  # valid JSON, but not encodable as UTF-8
    r = client.post("/v1/embed", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 422 and model.calls == []


def test_embed_refuses_a_model_batch_of_the_wrong_shape() -> None:
    client = TestClient(
        create_app(ShortVectorModel(), model_id="m", checksum_ok=True), raise_server_exceptions=False
    )
    r = client.post("/v1/embed", json={"input_kind": "passage", "texts": ["x"]})
    assert r.status_code == 500 and "vectors" not in r.json()


def _write_lock(tmp_path: Path, files: dict[str, bytes]) -> Path:
    lines = ["repo=r", "revision=v", "model_id=r@v/int8"]
    for name, content in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(content)
        lines.append(f"{name}=sha256:{hashlib.sha256(content).hexdigest()}")
    lock = tmp_path / "model.lock"
    lock.write_text("\n".join(lines) + "\n")
    return lock


def test_checksum_verification(tmp_path: Path) -> None:
    lock = _write_lock(tmp_path, {"onnx/model_int8.onnx": b"model", "tokenizer.json": b"tok"})
    assert verify_checksums(tmp_path, lock) == "r@v/int8"
    (tmp_path / "tokenizer.json").write_bytes(b"changed")
    with pytest.raises(ChecksumMismatch, match=r"tokenizer\.json") as exc:
        verify_checksums(tmp_path, lock)
    assert exc.value.file == "tokenizer.json"
    assert hashlib.sha256(b"changed").hexdigest() in str(exc.value)
    (tmp_path / "onnx/model_int8.onnx").unlink()
    with pytest.raises(ChecksumMismatch, match=r"onnx/model_int8\.onnx"):
        verify_checksums(tmp_path, lock)


@pytest.mark.parametrize(
    "lock_text",
    [
        "repo=r\nrevision=v\nonnx/m.onnx=sha256:" + "0" * 64 + "\n",  # no model_id
        "repo=r\nrevision=v\nmodel_id=m\n",  # no file
        "repo=r\nrevision=v\nmodel_id=m\nmodel_id=n\nm.onnx=sha256:" + "0" * 64 + "\n",  # duplicate key
        "repo=r\nrevision=v\nmodel_id=m\nm.onnx=md5:" + "0" * 32 + "\n",  # not a sha256
        "repo=r\nrevision=v\nmodel_id=m\n../m.onnx=sha256:" + "0" * 64 + "\n",  # outside the model dir
        "repo=r\nrevision=v\nmodel_id=m\n/m.onnx=sha256:" + "0" * 64 + "\n",
        "repo=r\nrevision=v\nmodel_id=m\njust a line\n",
    ],
)
def test_a_malformed_lock_is_refused(tmp_path: Path, lock_text: str) -> None:
    lock = tmp_path / "model.lock"
    lock.write_text(lock_text)
    with pytest.raises(ValueError, match=r"model\.lock"):
        verify_checksums(tmp_path, lock)


def test_the_committed_lock_pins_the_spec_model() -> None:
    # Ruling T21-a (docs/spikes/V7.md): the fp32 export for M1. V7 measured the int8 export's parity with fp32
    # below 0.99, and the fp16 export slower and larger than fp32 on this CPU. The sha256 values are the
    # Hugging Face LFS object ids of the revision.
    entries = dict(line.split("=", 1) for line in LOCK.read_text().splitlines())
    assert entries == {
        "repo": "Snowflake/snowflake-arctic-embed-l-v2.0",
        "revision": "ac6544c8a46e00af67e330e85a9028c66b8cfd9a",
        "model_id": "snowflake-arctic-embed-l-v2.0@ac6544c8a46e00af67e330e85a9028c66b8cfd9a/fp32",
        "onnx/model.onnx": "sha256:f74aa79745ccfb1e75daa7e8e6552a78402d4de193eb8ca67a931358d3e0a25e",
        "onnx/model.onnx_data": "sha256:fe7d75ff258fbda10a6bea63c5422df5579d625355b1aca69ba6923c0ba604a9",
        "tokenizer.json": "sha256:39feb9863a378165ab9c5c689047203d789422966c0c58721c5309fd039a8edc",
    }
    assert locked_onnx_file(LOCK) == "onnx/model.onnx"


@pytest.mark.parametrize(
    ("files", "error"),
    [
        (["onnx/model.onnx", "model.onnx_data"], "beside"),  # external data in another directory
        (["onnx/model.onnx", "onnx/other.onnx_data"], "beside"),  # data of another graph
        (["onnx/a.onnx", "onnx/b.onnx"], "exactly one"),
        (["tokenizer.json"], "exactly one"),
    ],
)
def test_the_locked_graph_is_one_onnx_file_with_its_external_data_beside_it(
    tmp_path: Path, files: list[str], error: str
) -> None:
    lock = tmp_path / "model.lock"
    lock.write_text("repo=r\nrevision=v\nmodel_id=m\n" + "".join(f"{f}=sha256:{'0' * 64}\n" for f in files))
    with pytest.raises(ValueError, match=error):
        locked_onnx_file(lock)


def test_the_external_data_file_is_verified_like_the_graph(tmp_path: Path) -> None:
    lock = _write_lock(
        tmp_path, {"onnx/model.onnx": b"graph", "onnx/model.onnx_data": b"weights", "tokenizer.json": b"tok"}
    )
    assert locked_onnx_file(lock) == "onnx/model.onnx" and verify_checksums(tmp_path, lock) == "r@v/int8"
    (tmp_path / "onnx/model.onnx_data").write_bytes(b"other weights")
    with pytest.raises(ChecksumMismatch, match=r"onnx/model\.onnx_data"):
        verify_checksums(tmp_path, lock)


def test_main_exits_78_and_names_the_file_on_a_checksum_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("EMBEDDER_MODEL_DIR", str(tmp_path))  # an empty model directory: nothing matches
    with caplog.at_level(logging.CRITICAL, logger="embedder"), pytest.raises(SystemExit) as exc:
        embedder_app.main()
    assert exc.value.code == 78
    assert "embedder.checksum_mismatch" in caplog.text and f"file={locked_onnx_file(LOCK)}" in caplog.text


def test_main_exits_78_on_an_invalid_thread_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    lock = _write_lock(tmp_path, {"onnx/model.onnx": b"graph", "tokenizer.json": b"tok"})  # the files match
    monkeypatch.setattr(embedder_app, "LOCK_PATH", lock)
    monkeypatch.setenv("EMBEDDER_MODEL_DIR", str(tmp_path))
    monkeypatch.setenv("EMBEDDER_THREADS", "0")
    with caplog.at_level(logging.CRITICAL, logger="embedder"), pytest.raises(SystemExit) as exc:
        embedder_app.main()
    assert exc.value.code == 78
    assert "embedder.startup_check_failed" in caplog.text and "EMBEDDER_THREADS" in caplog.text


def test_main_exits_78_on_a_malformed_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    lock = tmp_path / "model.lock"
    lock.write_text("repo=r\nrevision=v\nonnx/model.onnx=sha256:" + "0" * 64 + "\n")  # no model_id
    monkeypatch.setattr(embedder_app, "LOCK_PATH", lock)
    monkeypatch.setenv("EMBEDDER_MODEL_DIR", str(tmp_path))
    with caplog.at_level(logging.CRITICAL, logger="embedder"), pytest.raises(SystemExit) as exc:
        embedder_app.main()
    assert exc.value.code == 78
    assert "embedder.startup_check_failed" in caplog.text and "model.lock: missing model_id" in caplog.text


def test_disable_telemetry_sets_every_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ORT_DISABLE_TELEMETRY", "HF_HUB_OFFLINE", "HF_HUB_DISABLE_TELEMETRY"):
        monkeypatch.delenv(name, raising=False)  # restored after the test
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    disable_telemetry()
    assert RUNTIME_ENV == {
        "ORT_DISABLE_TELEMETRY": "1",
        "HF_HUB_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
    }
    assert {name: os.environ[name] for name in RUNTIME_ENV} == RUNTIME_ENV


def test_the_image_sets_the_same_switches() -> None:
    runtime = (ROOT / "embedder" / "Dockerfile").read_text().split("AS runtime", 1)[1].replace("\\\n", " ")
    env_words = " ".join(line for line in runtime.splitlines() if line.startswith("ENV ")).split()
    assert all(f"{name}={value}" in env_words for name, value in RUNTIME_ENV.items())


_FAKE_CURL = """#!/bin/sh
# curl as download_model.sh calls it: the last argument is the URL, `--output` names the file to write
out=""; url=""
while [ "$#" -gt 0 ]; do
    case "$1" in --output) out=$2; shift 2 ;; *) url=$1; shift ;; esac
done
cp "$FAKE_HF/${url#*/resolve/*/}" "$out"
"""


def _run_download(
    tmp_path: Path, served: dict[str, bytes], locked: dict[str, bytes]
) -> subprocess.CompletedProcess[str]:
    """download_model.sh against a fake curl that serves `served`, with a lock of the sha256 of `locked`."""
    for name, content in served.items():
        (tmp_path / "hf" / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "hf" / name).write_bytes(content)
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "curl").write_text(_FAKE_CURL)
    (tmp_path / "bin" / "curl").chmod(0o755)
    lines = ["repo=o/m", f"revision={'a' * 40}", "model_id=m"]
    lines += [f"{name}=sha256:{hashlib.sha256(content).hexdigest()}" for name, content in locked.items()]
    (tmp_path / "model.lock").write_text("\n".join(lines) + "\n")
    env = {
        **os.environ,
        "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
        "MODEL_LOCK": str(tmp_path / "model.lock"),
        "FAKE_HF": str(tmp_path / "hf"),
    }
    return subprocess.run(
        ["sh", str(DOWNLOAD), str(tmp_path / "model")], env=env, capture_output=True, text=True, check=False
    )


def test_download_model_places_the_locked_files(tmp_path: Path) -> None:
    files = {"onnx/model.onnx": b"graph", "onnx/model.onnx_data": b"weights", "tokenizer.json": b"tok"}
    r = _run_download(tmp_path, served=files, locked=files)
    assert r.returncode == 0, r.stderr
    assert {
        p.relative_to(tmp_path / "model").as_posix() for p in (tmp_path / "model").rglob("*") if p.is_file()
    } == set(files)


def test_download_model_never_gives_a_mismatched_download_the_locked_name(tmp_path: Path) -> None:
    locked = {"onnx/model.onnx": b"graph", "tokenizer.json": b"tok"}
    r = _run_download(tmp_path, served={**locked, "tokenizer.json": b"tampered"}, locked=locked)
    assert r.returncode != 0
    assert "tokenizer.json" in r.stderr and hashlib.sha256(b"tampered").hexdigest() in r.stderr
    assert not (tmp_path / "model" / "tokenizer.json").exists()
    assert not (tmp_path / "model" / "tokenizer.json.part").exists()


class _HealthStub(http.server.BaseHTTPRequestHandler):
    status = 200
    body = b""

    def do_GET(self) -> None:
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def health_server() -> Iterator[tuple[str, type[_HealthStub]]]:
    handler = type("Handler", (_HealthStub,), {})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/health", handler
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (200, {"status": "ok", "model_id": "m", "checksum_ok": True, "dim": 1024}, 0),
        (200, {"status": "ok", "model_id": "m", "checksum_ok": False, "dim": 1024}, 1),
        (200, {"status": "ok"}, 1),
        (503, {"status": "error", "model_id": "m", "checksum_ok": False, "dim": 1024}, 1),
        (200, "not json", 1),
    ],
)
def test_health_check_exit_code(
    health_server: tuple[str, type[_HealthStub]], status: int, body: object, expected: int
) -> None:
    url, handler = health_server
    handler.status = status
    handler.body = body.encode() if isinstance(body, str) else json.dumps(body).encode()
    assert health.check(url) == expected


def test_health_check_fails_when_nothing_listens() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    # the socket is closed again: nothing listens on the port
    assert health.check(f"http://127.0.0.1:{port}/health", timeout=1.0) == 1


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True)) / (math.hypot(*a) * math.hypot(*b))


def _require_embedder_group() -> None:
    """The `embedder_model` tests run only with the `embedder` dependency group; they fail, never skip."""
    missing = [m for m in ("onnxruntime", "tokenizers", "numpy") if importlib.util.find_spec(m) is None]
    if missing:
        pytest.fail(
            f"the embedder dependency group is not installed ({', '.join(missing)} missing): run with"
            " `uv run --group embedder pytest tests/embedder -m embedder_model`"
        )


def _require_model() -> None:
    _require_embedder_group()
    if not (MODEL_DIR / "tokenizer.json").is_file():
        pytest.fail("the model is not downloaded: run embedder/download_model.sh .model-cache/arctic")


@pytest.mark.embedder_model
def test_real_model_vectors_are_unit_length_and_prefix_sensitive() -> None:
    _require_model()
    from embedder.app import OnnxModel

    model = OnnxModel(MODEL_DIR)
    assert (
        verify_checksums(MODEL_DIR, LOCK)
        == dict(line.split("=", 1) for line in LOCK.read_text().splitlines())["model_id"]
    )
    assert model.onnx_file == locked_onnx_file(LOCK) and model.output_name == "sentence_embedding"
    python_ad = (
        "Senior Python developer (Django, FastAPI, PostgreSQL) for the backend team of a Berlin startup."
    )
    nursing_ad = "Registered nurse for the intensive care unit of a university hospital, three-shift rota."
    long_text = " ".join(["developer"] * 2000)
    passages, p_truncated, p_tokens = model.embed(
        [python_ad, nursing_ad, "python developer", long_text], "passage"
    )
    queries, q_truncated, q_tokens = model.embed(["python developer"], "query")
    for v in passages + queries:
        assert len(v) == DIM
        assert abs(math.hypot(*v) - 1.0) <= 1e-3
    assert p_truncated == [False, False, False, True] and q_truncated == [False]
    assert p_tokens[3] == MAX_TOKENS and all(t < MAX_TOKENS for t in p_tokens[:3])
    assert q_tokens[0] > p_tokens[2]  # the `query: ` prefix is part of the query's tokens only
    query = queries[0]
    assert _cosine(query, passages[0]) > _cosine(query, passages[1])  # closer to the Python ad
    assert _cosine(query, passages[2]) < 0.99  # the same text as a passage is a different vector
    # a text's vector is its own: the same alone as next to a 1,024-token text (no padding, one run per text)
    solo, solo_truncated, solo_tokens = model.embed([python_ad], "passage")
    assert (solo_truncated, solo_tokens) == (p_truncated[:1], p_tokens[:1])
    assert _cosine(solo[0], passages[0]) > 0.99999


def _fresh_home_env(home: Path) -> dict[str, str]:
    """The environment with a fresh HOME and none of the switches.

    onnxruntime 1.31 writes a telemetry device id and event store under ~/.cache/Microsoft unless
    ORT_DISABLE_TELEMETRY is set before its import.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith(("XDG_", "ORT_", "HF_"))}
    env["HOME"] = str(home)
    return env


@pytest.mark.embedder_model
def test_real_model_load_persists_no_telemetry(tmp_path: Path) -> None:
    _require_model()
    load = "import sys, pathlib, embedder.app as e; e.OnnxModel(pathlib.Path(sys.argv[1]))"
    subprocess.run(
        [sys.executable, "-c", load, str(MODEL_DIR)], cwd=ROOT, env=_fresh_home_env(tmp_path), check=True
    )
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []


@pytest.mark.embedder_model
def test_the_v7_spike_runs_onnxruntime_without_telemetry(tmp_path: Path) -> None:
    # the spike imports onnxruntime itself (environment(), padded_run), not only through OnnxModel
    _require_embedder_group()
    run = "import runpy, sys; runpy.run_path(sys.argv[1], run_name='v7_spike')['environment'](3)"
    subprocess.run(
        [sys.executable, "-c", run, str(SPIKE)],
        cwd=ROOT,
        env=_fresh_home_env(tmp_path),
        check=True,
        capture_output=True,
    )
    assert not (tmp_path / ".cache" / "Microsoft").exists()
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []
