"""The embedder service (spec §2.1 U3, §3.2, §4.1, §16.8, §17.5): the pinned embedding model over HTTP.

The image (embedder/Dockerfile) bakes in `Snowflake/snowflake-arctic-embed-l-v2.0` at the revision of
`model.lock`, as the ONNX export the lock pins: fp32 for M1 (ruling T21-a, docs/spikes/V7.md: the int8
export's parity with fp32 is below 0.99, and on the V7 CPU the fp16 export is slower and larger than fp32;
the deployment run of V7 decides again). At start `main()` checks every locked file's sha256 against the
lock and exits with 78 on a mismatch, naming the file (§16.8: a failed startup check exits 78). Then it
serves on 0.0.0.0:8080:

- `GET /health`: `{"status": "ok", "model_id", "checksum_ok": true, "dim": 1024}` (503 when the check failed);
- `POST /v1/embed`: `{"input_kind": "query" | "passage", "texts": [...]}` →
  `{"model_id", "vectors": [[1024 floats]], "truncated": [bool], "tokens": [int]}`. Each text is truncated to
  1,024 tokens (flagged), a query gets the model's `query: ` prefix, a passage none, and every vector is
  L2-normalised float32. More than 16 texts, or a text over 64 KiB of UTF-8, is a 413; any other bad request
  a 422.

This file runs as `/app/app.py` in the image, beside `health.py` and `model.lock`, and imports nothing from
`jobhunter`. `OnnxModel` imports onnxruntime, tokenizers and numpy itself, so `create_app` and
`verify_checksums` work in the base environment (tests/embedder). Nothing is written at run time: the
container runs read-only. Request texts are never logged, only their counts.
"""

import hashlib
import logging
import os
import re
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Any, Final, Literal, Protocol

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

MAX_TOKENS: Final = 1024
MAX_BATCH: Final = 16
MAX_TEXT_BYTES: Final = 65_536
DIM: Final = 1024
QUERY_PREFIX: Final = "query: "

DEFAULT_MODEL_DIR: Final = "/opt/model"
DEFAULT_THREADS: Final = 3
EXIT_STARTUP_CHECK: Final = 78  # EX_CONFIG: every failed startup check exits with it (spec §16.8)
HOST: Final = "0.0.0.0"  # noqa: S104 - the container's port, on the internal network only (§16.9, §19.1)
PORT: Final = 8080
LOCK_PATH: Final = Path(__file__).resolve().with_name("model.lock")

_LOCK_META: Final = ("repo", "revision", "model_id")
_SHA256: Final = re.compile(r"sha256:([0-9a-f]{64})")
# The pooled outputs an export may have, in order of preference. arctic-embed-l-v2.0 pools the CLS token
# (its 1_Pooling/config.json: pooling_mode_cls_token), so a token-level output is pooled at position 0.
_POOLED_OUTPUT: Final = "sentence_embedding"
_TOKEN_OUTPUTS: Final = ("last_hidden_state", "token_embeddings")

InputKind = Literal["query", "passage"]

log = logging.getLogger("embedder")


class ChecksumMismatch(Exception):
    """A file of model.lock is missing from the model directory, unreadable, or has another sha256."""

    def __init__(self, file: str, detail: str) -> None:
        super().__init__(f"{file}: {detail}")
        self.file = file


def read_lock(lock: Path) -> tuple[dict[str, str], dict[str, str]]:
    """model.lock's `repo`, `revision` and `model_id`, and its files (relative path → sha256 hex).

    Each line is `key=value`; a file line is `<path relative to the model directory>=sha256:<64 hex>`.
    Anything else (a missing key, a duplicate, a path outside the model directory) is a ValueError.
    """
    meta: dict[str, str] = {}
    files: dict[str, str] = {}
    for n, line in enumerate(lock.read_text(encoding="utf-8").splitlines(), start=1):
        key, sep, value = line.partition("=")
        if not sep or not key or not value:
            raise ValueError(f"model.lock line {n}: not a key=value line")
        if key in meta or key in files:
            raise ValueError(f"model.lock line {n}: duplicate key {key}")
        if key in _LOCK_META:
            meta[key] = value
            continue
        digest = _SHA256.fullmatch(value)
        if digest is None:
            raise ValueError(f"model.lock line {n}: {key} is not sha256:<64 lowercase hex>")
        path = PurePosixPath(key)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"model.lock line {n}: {key} is outside the model directory")
        files[key] = digest.group(1)
    missing = [k for k in _LOCK_META if k not in meta]
    if missing:
        raise ValueError(f"model.lock: missing {', '.join(missing)}")
    if not files:
        raise ValueError("model.lock: no file is locked")
    return meta, files


def verify_checksums(model_dir: Path, lock: Path) -> str:
    """Check every locked file under `model_dir` against model.lock and return the lock's `model_id`.

    Raises ChecksumMismatch naming the first file that is missing, unreadable or different.
    """
    meta, files = read_lock(lock)
    for name, expected in files.items():
        try:
            with (model_dir / name).open("rb") as f:
                actual = hashlib.file_digest(f, "sha256").hexdigest()
        except FileNotFoundError:
            raise ChecksumMismatch(name, "missing from the model directory") from None
        except OSError as exc:
            raise ChecksumMismatch(name, f"unreadable ({type(exc).__name__})") from None
        if actual != expected:
            raise ChecksumMismatch(
                name, f"sha256 mismatch: model.lock sha256:{expected}, file sha256:{actual}"
            )
    return meta["model_id"]


def locked_onnx_file(lock: Path) -> str:
    """The one ONNX graph that model.lock pins (`onnx/model.onnx`).

    A graph over 2 GB keeps its weights in an external-data file, which onnxruntime opens from the graph's
    own directory: a locked `.onnx_data` file must be `<graph>_data`, beside it (`onnx/model.onnx_data`).
    """
    _, files = read_lock(lock)
    graphs = [name for name in files if name.endswith(".onnx")]
    if len(graphs) != 1:
        raise ValueError(f"model.lock: {len(graphs)} .onnx files are locked, expected exactly one")
    for name in files:
        if name.endswith(".onnx_data") and name != graphs[0] + "_data":
            raise ValueError(f"model.lock: {name} is not the external data beside {graphs[0]}")
    return graphs[0]


def threads_from_env() -> int:
    """onnxruntime's intra-op threads: EMBEDDER_THREADS, default 3 (the service's 3 CPUs, §19.1)."""
    raw = os.environ.get("EMBEDDER_THREADS", str(DEFAULT_THREADS))
    if not raw.isdecimal() or int(raw) < 1:
        raise ValueError("EMBEDDER_THREADS must be a positive integer")
    return int(raw)


class Model(Protocol):
    def embed(
        self, texts: Sequence[str], input_kind: Literal["query", "passage"]
    ) -> tuple[list[list[float]], list[bool], list[int]]:
        """The L2-normalised vectors, the truncation flags and the token counts of `texts`, in order."""
        ...


class OnnxModel(Model):
    """The ONNX export on onnxruntime's CPU provider, with the export's own tokenizer.

    Each text is truncated to 1,024 tokens and run on its own: a batch of one, so there is no padding. Spike
    V7 measured why (docs/spikes/V7.md): at 1,024 tokens, padded batches of 16 were slower on the CPU (fp16
    0.46 against 0.51 docs/s, int8 1.23 against 1.36) and needed twice to three times the memory (fp16 6.8 GB
    peak RSS against 3.2 GB, int8 5.4 against 1.7: over the service's 3.5 GB limit either way); and with the
    int8 export, which quantises activations with one scale per tensor, a text's vector moved with the texts
    padded next to it (cos 0.974). One text per run keeps a vector a function of its text alone.

    The vector is the export's pooled `sentence_embedding` when it has one (the int8, fp16 and fp32 exports
    all do: the normalised CLS token), otherwise the CLS token of its token-level output, as the model pools
    (its `1_Pooling/config.json`: `pooling_mode_cls_token`). `output_name` says which output is used.
    """

    def __init__(self, model_dir: Path, *, onnx_file: str | None = None, threads: int | None = None) -> None:
        """Load `onnx_file` (default: the export model.lock pins) and `tokenizer.json` from `model_dir`.

        onnxruntime reads a graph's external data (`onnx/model.onnx_data` of the fp32 export) from the graph's
        directory, so the lock keeps it beside the graph (`locked_onnx_file`).
        """
        # onnxruntime 1.31 on Linux has Microsoft telemetry (1DS: a device id and an event store under
        # ~/.cache/Microsoft/DeveloperTools/.onnxruntime, posted to mobile.events.data.microsoft.com); the
        # switch only counts when it is set before the import (V7). The image sets it too (Dockerfile).
        os.environ["ORT_DISABLE_TELEMETRY"] = "1"
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        ort.disable_telemetry_events()
        self._np: Any = np
        self.onnx_file: str = onnx_file if onnx_file is not None else locked_onnx_file(LOCK_PATH)
        tokenizer: Any = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        tokenizer.enable_truncation(max_length=MAX_TOKENS)  # the file's own setting is 512
        tokenizer.no_padding()  # the file pads batches; every text runs alone here
        self._tokenizer = tokenizer

        options: Any = ort.SessionOptions()
        options.intra_op_num_threads = threads if threads is not None else threads_from_env()
        options.inter_op_num_threads = 1
        session: Any = ort.InferenceSession(
            str(model_dir / self.onnx_file), sess_options=options, providers=["CPUExecutionProvider"]
        )
        self._session = session
        self._inputs: set[str] = {i.name for i in session.get_inputs()}
        if (
            not {"input_ids", "attention_mask"}
            <= self._inputs
            <= {"input_ids", "attention_mask", "token_type_ids"}
        ):
            raise ValueError(f"unexpected ONNX inputs: {sorted(self._inputs)}")
        outputs: set[str] = {o.name for o in session.get_outputs()}
        token_level = [name for name in _TOKEN_OUTPUTS if name in outputs]
        if _POOLED_OUTPUT in outputs:
            self.output_name: str = _POOLED_OUTPUT
        elif token_level:
            self.output_name = token_level[0]
        else:
            raise ValueError(f"the ONNX export has no pooled or token-level output: {sorted(outputs)}")

    def embed(
        self, texts: Sequence[str], input_kind: Literal["query", "passage"]
    ) -> tuple[list[list[float]], list[bool], list[int]]:
        if not texts:
            return [], [], []
        np = self._np
        inputs = [QUERY_PREFIX + t for t in texts] if input_kind == "query" else list(texts)
        encodings = self._tokenizer.encode_batch(inputs)
        pooled = []
        for encoding in encodings:
            ids = np.array([encoding.ids], dtype=np.int64)
            feed = {"input_ids": ids, "attention_mask": np.array([encoding.attention_mask], dtype=np.int64)}
            if "token_type_ids" in self._inputs:
                feed["token_type_ids"] = np.zeros_like(ids)
            (output,) = self._session.run([self.output_name], feed)
            pooled.append(output[0] if self.output_name == _POOLED_OUTPUT else output[0, 0, :])
        matrix = np.stack(pooled).astype(np.float32)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        vectors = (matrix / np.maximum(norms, np.float32(1e-12))).astype(np.float32)
        truncated = [bool(e.overflowing) for e in encodings]
        tokens = [len(e.ids) for e in encodings]
        return vectors.tolist(), truncated, tokens


class EmbedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_kind: InputKind
    texts: list[str] = Field(min_length=1)


class EmbedResponse(BaseModel):
    model_id: str
    vectors: list[list[float]]
    truncated: list[bool]
    tokens: list[int]


def _check_sizes(texts: Sequence[str]) -> None:
    """413 for more than 16 texts or a text over 64 KiB of UTF-8; 422 for a text that is not valid Unicode."""
    if len(texts) > MAX_BATCH:
        raise HTTPException(413, f"at most {MAX_BATCH} texts per request")
    for i, text in enumerate(texts):
        try:
            size = len(text.encode("utf-8"))
        except UnicodeEncodeError:  # a lone surrogate, from a JSON \ud800 escape
            raise HTTPException(422, f"text {i} is not valid Unicode") from None
        if size > MAX_TEXT_BYTES:
            raise HTTPException(413, f"text {i} is over {MAX_TEXT_BYTES} bytes")


def create_app(model: Model, *, model_id: str, checksum_ok: bool) -> FastAPI:
    """The HTTP service around `model`; `checksum_ok` is the startup check's result (else /health is 503)."""
    app = FastAPI(title="embedder", docs_url=None, redoc_url=None, openapi_url=None)
    # One batch at a time: concurrent batches would only share the same CPUs and add their memory peaks.
    # /v1/embed is a sync handler (FastAPI's thread pool), so /health stays answerable during a batch.
    model_lock = threading.Lock()

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI's default 422 body echoes the offending input, which may be an ad's text
        errors = [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
        return JSONResponse({"detail": errors}, status_code=422)

    @app.get("/health")
    async def health() -> JSONResponse:
        body = {
            "status": "ok" if checksum_ok else "error",
            "model_id": model_id,
            "checksum_ok": checksum_ok,
            "dim": DIM,
        }
        return JSONResponse(body, status_code=200 if checksum_ok else 503)

    @app.post("/v1/embed")
    def embed(request: EmbedRequest) -> EmbedResponse:
        _check_sizes(request.texts)
        started = time.monotonic()
        with model_lock:
            vectors, truncated, tokens = model.embed(request.texts, request.input_kind)
        n = len(request.texts)
        if (len(vectors), len(truncated), len(tokens)) != (n, n, n) or any(len(v) != DIM for v in vectors):
            log.error("embedder.bad_batch texts=%d vectors=%d", n, len(vectors))
            raise HTTPException(500, "the model returned a malformed batch")
        log.info(
            "embedder.embed input_kind=%s texts=%d tokens=%d truncated=%d ms=%d",
            request.input_kind,
            n,
            sum(tokens),
            sum(truncated),
            round((time.monotonic() - started) * 1000),
        )
        return EmbedResponse(model_id=model_id, vectors=vectors, truncated=truncated, tokens=tokens)

    return app


def main() -> None:
    """Verify the model against model.lock (exit 78 on a mismatch, naming the file), load it and serve."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s", stream=sys.stderr
    )
    model_dir = Path(os.environ.get("EMBEDDER_MODEL_DIR", DEFAULT_MODEL_DIR))
    try:
        model_id = verify_checksums(model_dir, LOCK_PATH)
        onnx_file = locked_onnx_file(LOCK_PATH)
        threads = threads_from_env()
    except ChecksumMismatch as exc:
        log.critical("embedder.checksum_mismatch file=%s %s", exc.file, exc)
        raise SystemExit(EXIT_STARTUP_CHECK) from None
    except (OSError, ValueError) as exc:  # model.lock unreadable or malformed, or EMBEDDER_THREADS invalid
        log.critical("embedder.startup_check_failed %s", exc)
        raise SystemExit(EXIT_STARTUP_CHECK) from None
    started = time.monotonic()
    model = OnnxModel(model_dir, onnx_file=onnx_file, threads=threads)  # the file verified above
    log.info(
        "embedder.ready model_id=%s onnx=%s output=%s threads=%d load_ms=%d",
        model_id,
        onnx_file,
        model.output_name,
        threads,
        round((time.monotonic() - started) * 1000),
    )
    app = create_app(model, model_id=model_id, checksum_ok=True)
    uvicorn.run(app, host=HOST, port=PORT, access_log=False, server_header=False, log_level="info")


if __name__ == "__main__":
    main()
