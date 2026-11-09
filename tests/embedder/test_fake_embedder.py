"""The test-only fake embedder (M4 Task 18): `embedder.app.FakeModel` behind the real service when
`EMBEDDER_FAKE` is set, refused outside dev and test (exit 78, `fake_embedder_outside_dev`, as
`fake_mode_in_prod` refuses the fake LLM, §16.8), its vectors the old `tests/support/embedder.fake_vector`'s
and its answers those of the tests' `FakeEmbedder`; and the image's `fake` target, which has no model stage.

Everything runs in the base environment: the service is served by FastAPI's test client, `uvicorn.run`
replaced by a capture.
"""

import hashlib
import logging
import re
import struct
from pathlib import Path
from typing import Any, Final

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from embedder import app as embedder_app
from embedder.app import DIM, LOCK_PATH, QUERY_PREFIX, FakeModel, create_app, fake_vector, read_lock
from tests.support.embedder import FakeEmbedder

ROOT: Final = Path(__file__).resolve().parents[2]
MODEL_ID: Final = read_lock(LOCK_PATH)[0]["model_id"]
# The old `tests/support/embedder.fake_vector` (Task 22), captured before it moved into embedder/app.py: the
# sha256 of each text's 1,024 components as little-endian doubles.
OLD_VECTORS: Final = {
    "query: Softwareentwickler Berlin": "cfeccf96f51ec71d97a249b62afcfa0253a12eff65820489b5b13fb7d264e6f2",
    "Wir suchen eine Backend-Entwicklerin (m/w/d).": (
        "d507c6f4c0f8a28f716af0cf694a01da92b02a75a0bfc25da8e4078163244f62"
    ),
    "": "cb2662d287e2c35766b6488febe6e644fdd9fc1abf985b3981969ccde05be2d0",
    "ü" * 5000: "521ffad286c1931a0b284568b794d17732d07bdf4f52e679c3b0e6b98636cf17",
}
TEXTS: Final = [
    "Softwareentwickler Berlin",
    "Wir suchen eine Backend-Entwicklerin (m/w/d).",
    "x" * 4001,
    "abc",
]


def _digest(vector: list[float]) -> str:
    return hashlib.sha256(struct.pack(f"<{len(vector)}d", *vector)).hexdigest()


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> list[FastAPI]:
    """The app `main()` would serve, captured instead of served."""
    apps: list[FastAPI] = []

    def run(app: FastAPI, **kwargs: Any) -> None:
        apps.append(app)

    monkeypatch.setattr(uvicorn, "run", run)  # the module app.py serves with
    return apps


def test_fake_vectors_equal_the_test_supports(fake_embedder: FakeEmbedder) -> None:
    for text, digest in OLD_VECTORS.items():
        assert _digest(fake_vector(text, DIM)) == digest, text[:20]
    vectors, truncated, tokens = FakeModel().embed(["Softwareentwickler Berlin"], "query")
    assert _digest(vectors[0]) == OLD_VECTORS[QUERY_PREFIX + "Softwareentwickler Berlin"]  # the text it sees
    assert (truncated, tokens) == ([False], [6])
    # the stack's fake service and the tests' FakeEmbedder give every request the same answer
    service = TestClient(create_app(FakeModel(), model_id=MODEL_ID, checksum_ok=True, fake=True))
    for kind in ("query", "passage"):
        request = {"input_kind": kind, "texts": TEXTS}
        answered = service.post("/v1/embed", json=request)
        supported = httpx.post(f"{fake_embedder.base_url}/v1/embed", json=request, trust_env=False)
        assert answered.status_code == supported.status_code == 200
        assert answered.json() == supported.json()
        assert answered.json()["truncated"] == [False, False, True, False]
        assert answered.json()["tokens"] == [6, 11, 1024, 1]


@pytest.mark.parametrize("env", ["dev", "test"])
def test_fake_health_reports_fake(
    env: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, served: list[FastAPI]
) -> None:
    """With `EMBEDDER_FAKE` in dev or test, `main()` reads neither the model files nor their checksums (an
    empty model directory) and serves `FakeModel` with a health the api accepts: the lock's model id, the
    checksum passed, the dimension, and `fake: true`."""
    monkeypatch.setenv("EMBEDDER_FAKE", "1")
    monkeypatch.setenv("JOBHUNTER_ENV", env)
    monkeypatch.setenv("EMBEDDER_MODEL_DIR", str(tmp_path / "no-model"))
    embedder_app.main()
    (app,) = served
    client = TestClient(app)
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {
        "status": "ok",
        "model_id": MODEL_ID,
        "checksum_ok": True,
        "dim": DIM,
        "fake": True,
    }
    embedded = client.post("/v1/embed", json={"input_kind": "passage", "texts": ["abc"]}).json()
    assert embedded["model_id"] == MODEL_ID and embedded["vectors"] == [fake_vector("abc", DIM)]


def test_the_real_health_has_no_fake_field() -> None:
    health = TestClient(create_app(FakeModel(), model_id=MODEL_ID, checksum_ok=True)).get("/health").json()
    assert health == {"status": "ok", "model_id": MODEL_ID, "checksum_ok": True, "dim": DIM}


@pytest.mark.parametrize("env", ["prod", None, "staging", ""])
def test_fake_mode_refuses_to_start_outside_dev_or_test(
    env: str | None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    served: list[FastAPI],
) -> None:
    monkeypatch.setenv("EMBEDDER_FAKE", "1")
    if env is None:
        monkeypatch.delenv("JOBHUNTER_ENV", raising=False)
    else:
        monkeypatch.setenv("JOBHUNTER_ENV", env)
    with caplog.at_level(logging.CRITICAL, logger="embedder"), pytest.raises(SystemExit) as exc:
        embedder_app.main()
    assert exc.value.code == 78
    assert "fake_embedder_outside_dev" in caplog.text
    assert served == []  # nothing was served


def test_without_embedder_fake_the_model_is_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, served: list[FastAPI]
) -> None:
    """`JOBHUNTER_ENV=dev` alone changes nothing: the model directory is checked against the lock."""
    monkeypatch.delenv("EMBEDDER_FAKE", raising=False)
    monkeypatch.setenv("JOBHUNTER_ENV", "dev")
    monkeypatch.setenv("EMBEDDER_MODEL_DIR", str(tmp_path))
    with caplog.at_level(logging.CRITICAL, logger="embedder"), pytest.raises(SystemExit) as exc:
        embedder_app.main()
    assert exc.value.code == 78 and "embedder.checksum_mismatch" in caplog.text and served == []


def _stages(dockerfile: str) -> dict[str, str]:
    """Each `FROM ... AS <name>` stage's text, by name."""
    parts = re.split(r"^FROM \S+ AS (\w+)\n", dockerfile, flags=re.MULTILINE)
    return dict(zip(parts[1::2], parts[2::2], strict=True))


def test_the_fake_target_has_no_model_stage() -> None:
    """`docker build --target fake` builds the `build` stage's venv and the app files, never the `model`
    stage that downloads the model; the default target (the last stage) is still the real runtime."""
    dockerfile = (ROOT / "embedder" / "Dockerfile").read_text("utf-8")
    stages = _stages(dockerfile)
    assert list(stages)[-1] == "runtime" and "--from=model" in stages["runtime"]
    fake = stages["fake"]
    assert re.findall(r"--from=(\w+)", fake) == ["build"]
    assert "download_model" not in fake and "/opt/model" not in fake
    # otherwise the runtime image as it is: the same venv, app files, switches, user and command
    model_copy = "COPY --from=model /opt/model /opt/model"
    assert _instructions(fake) == [line for line in _instructions(stages["runtime"]) if line != model_copy]


def _instructions(stage: str) -> list[str]:
    return [
        line for line in stage.replace("\\\n", " ").splitlines() if line.strip() and not line.startswith("#")
    ]
