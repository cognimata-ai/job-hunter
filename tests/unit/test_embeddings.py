import asyncio
import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from jobhunter.embeddings import (
    BATCH,
    BATCH_TIMEOUT_S,
    EMBEDDING_DIM,
    EMBEDDING_MODEL_ID,
    HEALTH_TIMEOUT_S,
    MAX_TEXT_BYTES,
    Embedded,
    EmbedderClient,
    EmbedderError,
    EmbedderHealth,
    embedding_text,
    text_sha256,
)
from jobhunter.errors import DomainError
from jobhunter.types import JSONValue

LOCK = Path(__file__).resolve().parents[2] / "embedder" / "model.lock"
BASE = "http://embedder.test:8080"
OK_HEALTH: dict[str, Any] = {"status": "ok", "model_id": EMBEDDING_MODEL_ID, "checksum_ok": True, "dim": 1024}


def test_the_constants() -> None:
    assert (BATCH, BATCH_TIMEOUT_S, HEALTH_TIMEOUT_S, MAX_TEXT_BYTES, EMBEDDING_DIM) == (
        16,
        120.0,
        2.0,
        65_536,
        1024,
    )


def test_the_model_id_is_the_one_model_lock_pins() -> None:
    """Ruling T21-a: the fp32 export; the constant and the lock line can never drift apart."""
    lines = dict(line.split("=", 1) for line in LOCK.read_text(encoding="utf-8").splitlines())
    assert EMBEDDING_MODEL_ID == lines["model_id"]
    assert EMBEDDING_MODEL_ID == "snowflake-arctic-embed-l-v2.0@ac6544c8a46e00af67e330e85a9028c66b8cfd9a/fp32"


def test_the_embedding_text_joins_title_company_and_description() -> None:
    llm_input: dict[str, JSONValue] = {
        "title": "Data Engineer",
        "company_display": "Acme",
        "description_text": "Pipelines.",
        "x": 1,
    }
    assert embedding_text(llm_input) == "Data Engineer\nAcme\nPipelines."
    with pytest.raises(TypeError, match="company_display"):
        embedding_text({"title": "T", "company_display": None, "description_text": "D"})


def test_the_embedding_text_is_cut_to_the_services_limit_on_a_character_boundary() -> None:
    euro = "€" * 30_000  # 3 bytes each: 90,000 bytes
    text = embedding_text({"title": "T", "company_display": "C", "description_text": euro})
    encoded = text.encode("utf-8")
    assert len(encoded) <= MAX_TEXT_BYTES and len(encoded) > MAX_TEXT_BYTES - 3
    assert text.startswith("T\nC\n€") and text.endswith("€")  # no half character at the cut
    exact = embedding_text(
        {"title": "", "company_display": "", "description_text": "a" * (MAX_TEXT_BYTES - 2)}
    )
    assert len(exact.encode()) == MAX_TEXT_BYTES  # a text at the limit is kept whole


def test_text_sha256_is_of_the_utf8_text() -> None:
    assert text_sha256("Äpfel") == hashlib.sha256("Äpfel".encode()).digest()


def _vector(seed: int, dim: int = EMBEDDING_DIM) -> list[float]:
    return [1.0 if i == seed % dim else 0.0 for i in range(dim)]


def _embed_body(texts: list[str], **override: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model_id": EMBEDDING_MODEL_ID,
        "vectors": [_vector(i) for i in range(len(texts))],
        "truncated": [False] * len(texts),
        "tokens": [3] * len(texts),
    }
    return body | override


Handler = Callable[[httpx.Request], Any]


async def _call(handler: Handler, texts: list[str], **options: Any) -> list[Embedded]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False) as http:
        return await EmbedderClient(BASE + "/", http).embed(texts, input_kind="passage", **options)


async def _health(handler: Handler, **options: Any) -> EmbedderHealth:
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False) as http:
        return await EmbedderClient(BASE, http).health(**options)


async def test_an_answer_becomes_one_embedded_per_text() -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and str(request.url) == f"{BASE}/v1/embed"
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=_embed_body(["a", "b"], truncated=[False, True], tokens=[1, 1024]))

    out = await _call(handler, ["a", "b"])
    assert sent == [{"input_kind": "passage", "texts": ["a", "b"]}]
    assert out == [
        Embedded(vector=tuple(_vector(0)), truncated=False, tokens=1),
        Embedded(vector=tuple(_vector(1)), truncated=True, tokens=1024),
    ]
    assert await _call(handler, []) == [] and len(sent) == 1  # nothing to embed: no request


async def test_more_than_a_batch_is_refused_before_any_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request")

    with pytest.raises(ValueError, match="16"):
        await _call(handler, ["x"] * 17)


def _connect_error(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("refused", request=request)


def _read_timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("slow", request=request)


async def _never(request: httpx.Request) -> httpx.Response:
    await asyncio.Event().wait()
    raise AssertionError("unreachable")


def _server_error(status: int, health_status: int, health: dict[str, Any]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(health_status, json=health)
        return httpx.Response(status, json={"detail": "x"})

    return handler


async def test_a_lost_or_failing_service_is_unavailable() -> None:
    for handler, code in (
        (_connect_error, "connect"),
        (_read_timeout, "timeout"),
        (_server_error(500, 200, OK_HEALTH), "http_500"),
        (_server_error(503, 200, OK_HEALTH), "http_503"),
        (_server_error(502, 599, {}), "http_502"),  # a health that is no answer either
    ):
        with pytest.raises(EmbedderError) as raised:
            await _call(handler, ["a"])
        assert (raised.value.kind, raised.value.code) == ("unavailable", code)


async def test_the_whole_call_is_bounded_by_its_timeout() -> None:
    with pytest.raises(EmbedderError) as raised:
        await _call(_never, ["a"], timeout_s=0.01)
    assert (raised.value.kind, raised.value.code) == ("unavailable", "deadline")


async def test_a_failed_embed_with_a_503_health_is_a_checksum_error() -> None:
    bad = {"status": "error", "model_id": EMBEDDING_MODEL_ID, "checksum_ok": False, "dim": 1024}
    with pytest.raises(EmbedderError) as raised:
        await _call(_server_error(503, 503, bad), ["a"])
    assert (raised.value.kind, raised.value.code) == ("checksum", "health_503")


@pytest.mark.parametrize(
    ("respond", "code"),
    [
        (lambda texts: httpx.Response(200, content=b"<html>"), "not_json"),
        (lambda texts: httpx.Response(200, json=[1, 2]), "not_an_object"),
        (lambda texts: httpx.Response(200, json=_embed_body(texts, model_id="other")), "model_id"),
        (lambda texts: httpx.Response(200, json=_embed_body(texts, vectors=[_vector(0)])), "count"),
        (lambda texts: httpx.Response(200, json=_embed_body(texts, truncated=[False])), "count"),
        (lambda texts: httpx.Response(200, json=_embed_body(texts, tokens=[1, 2, 3])), "count"),
        (
            lambda texts: httpx.Response(200, json=_embed_body(texts, vectors=[_vector(0, 8)] * 2)),
            "dimension",
        ),
        (
            lambda texts: httpx.Response(200, json=_embed_body(texts, vectors=[[0.1] * 1023 + ["x"]] * 2)),
            "vector",
        ),
        (lambda texts: httpx.Response(200, json=_embed_body(texts, vectors=[[True] * 1024] * 2)), "vector"),
        (lambda texts: httpx.Response(200, json=_embed_body(texts, truncated=[0, 1])), "truncated"),
        (lambda texts: httpx.Response(200, json=_embed_body(texts, tokens=[1, -1])), "tokens"),
        (lambda texts: httpx.Response(200, json=_embed_body(texts, tokens=[1, 2.5])), "tokens"),
        (lambda texts: httpx.Response(413, json={"detail": "too large"}), "http_413"),
        (lambda texts: httpx.Response(422, json={"detail": []}), "http_422"),
    ],
)
async def test_a_malformed_answer_is_a_bad_response(
    respond: Callable[[list[str]], httpx.Response], code: str
) -> None:
    texts = ["a", "b"]
    with pytest.raises(EmbedderError) as raised:
        await _call(lambda request: respond(texts), texts)
    assert (raised.value.kind, raised.value.code) == ("bad_response", code)
    assert str(raised.value) == f"bad_response: {code}"  # the kind and the code, never content


def _one_hot(value: float, rest: float = 0.0) -> list[float]:
    return [value] + [rest] * (EMBEDDING_DIM - 1)


@pytest.mark.parametrize(
    ("vector", "code"),
    [
        (_one_hot(65_505.0), "out_of_range"),  # past the largest finite half (65,504): halfvec refuses it
        (_one_hot(-1e6), "out_of_range"),
        (_one_hot(65_504.0), "norm"),  # in the halfvec range, far from unit length
        (_one_hot(0.0), "norm"),  # the zero vector
        (_one_hot(1.011), "norm"),
        (_one_hot(0.989), "norm"),
        (_one_hot(0.5, 0.5), "norm"),
    ],
)
async def test_a_vector_halfvec_cannot_hold_or_not_of_unit_length_is_a_bad_response(
    vector: list[float], code: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_embed_body(["a"], vectors=[vector]))

    with pytest.raises(EmbedderError) as raised:
        await _call(handler, ["a"])
    assert (raised.value.kind, raised.value.code) == ("bad_response", code)


async def test_a_vector_within_the_norm_tolerance_is_accepted() -> None:
    for value in (1.009, 0.991):

        def handler(request: httpx.Request, value: float = value) -> httpx.Response:
            return httpx.Response(200, json=_embed_body(["a"], vectors=[_one_hot(value)]))

        (out,) = await _call(handler, ["a"])
        assert out.vector[0] == value


async def test_a_non_finite_component_is_a_bad_response() -> None:
    body = json.dumps(_embed_body(["a"])).replace("0.0", "NaN", 1).encode()  # json allows NaN and 1e999

    for content in (body, json.dumps(_embed_body(["a"])).replace("0.0", "1e999", 1).encode()):

        def handler(request: httpx.Request, content: bytes = content) -> httpx.Response:
            return httpx.Response(200, content=content, headers={"Content-Type": "application/json"})

        with pytest.raises(EmbedderError) as raised:
            await _call(handler, ["a"])
        assert (raised.value.kind, raised.value.code) == ("bad_response", "non_finite")


async def test_health_is_an_answer_never_an_error() -> None:
    def answer(status: int, body: object) -> Handler:
        def handler(request: httpx.Request) -> httpx.Response:
            assert str(request.url) == f"{BASE}/health"
            return httpx.Response(status, json=body)

        return handler

    bad_checksum = {"status": "error", "model_id": EMBEDDING_MODEL_ID, "checksum_ok": False, "dim": 1024}
    assert await _health(answer(200, OK_HEALTH)) == EmbedderHealth(True, EMBEDDING_MODEL_ID, True)
    assert await _health(answer(503, bad_checksum)) == EmbedderHealth(False, EMBEDDING_MODEL_ID, False)
    # an embedder this code cannot use: another model, or another dimension
    assert await _health(answer(200, OK_HEALTH | {"model_id": "m"})) == EmbedderHealth(False, "m", True)
    assert await _health(answer(200, OK_HEALTH | {"dim": 8})) == EmbedderHealth(
        False, EMBEDDING_MODEL_ID, True
    )
    assert await _health(answer(200, [1])) == EmbedderHealth(False, None, None)
    assert await _health(
        answer(200, {"status": "ok", "model_id": 7, "checksum_ok": "yes"})
    ) == EmbedderHealth(False, None, None)
    assert await _health(answer(500, {"detail": "x"})) == EmbedderHealth(False, None, None)
    assert await _health(_connect_error) == EmbedderHealth(False, None, None)
    assert await _health(_never, timeout_s=0.01) == EmbedderHealth(False, None, None)


def test_the_error_names_its_kind_and_code_only() -> None:
    assert str(EmbedderError("unavailable")) == "unavailable: unavailable"
    assert EmbedderError("checksum", code="health_503").code == "health_503"
    assert isinstance(EmbedderError("bad_response"), DomainError)


def test_every_embed_outcome_is_exported_at_zero_before_the_first_batch() -> None:
    """Ledger L364: `EMBED_SECONDS` has its series from the import, each at 0, in a fresh interpreter."""
    import subprocess
    import sys

    code = (
        "import json; from jobhunter import embeddings; "
        "from jobhunter.observability.metrics import REGISTRY; "
        "print(json.dumps({o: REGISTRY.get_sample_value('jobhunter_embed_seconds_count', {'outcome': o}) "
        "for o in embeddings.EMBED_OUTCOMES}))"
    )
    done = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr[-400:]
    counts = json.loads(done.stdout)
    assert counts == {"ok": 0.0, "unavailable": 0.0, "bad_response": 0.0, "checksum": 0.0, "error": 0.0}
