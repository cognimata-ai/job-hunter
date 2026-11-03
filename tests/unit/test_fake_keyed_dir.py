"""The fake LLM's standing keyed answers on the stack (M4 Task 18): `JOBHUNTER_FAKE_LLM_KEYED_DIR`, a
directory whose `<keyed_answer_filename(K)>` answers every message try sent with `X-JH-Fake-Key: K`, read at
each try; after the state's registered keyed answers, before the scenario script and the recordings; dev and
test only.

The tries run offline through the real `_chat` stack on the fake (`tests/unit/test_fake_llm_keys.py`'s way).
"""

import base64
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any, Final, cast

import anthropic
import pytest
from jobhunter.config import StartupCheckFailed, load_config, run_startup_assertions
from jobhunter.llm import fake
from jobhunter.llm.gateway import WireSchema, _chat
from jobhunter.llm.pricing import MODELS
from langchain_core.messages import HumanMessage
from pydantic import SecretStr

from tests.support.llm import GATEWAY_BODY, FakeLLM, GatewayOut, fake_llm_config, synthetic_sse

KEY: Final = SecretStr("fake-keyed-dir-test-key-0001")  # visibly fake; the fake ignores keys
MODEL: Final = "claude-sonnet-5-5"
ONE: Final[Mapping[str, Any]] = {"answer": "the keyed file's draft", "score": 1}
TWO: Final[Mapping[str, Any]] = {"answer": "the registered draft", "score": 4}


@pytest.fixture
async def keyed(tmp_path: Path) -> AsyncIterator[tuple[FakeLLM, Path]]:
    """The fake over `GatewayOut` recordings, with a keyed directory (created empty)."""
    directory = tmp_path / "fake-keyed"
    directory.mkdir()
    cfg = fake_llm_config(
        tmp_path / "anthropic", tmp_path / "no-secrets", JOBHUNTER_FAKE_LLM_KEYED_DIR=str(directory)
    )
    fake_llm = FakeLLM(cfg)
    for model in MODELS:
        fake_llm.record("generate", model)
    try:
        yield fake_llm, directory
    finally:
        await fake_llm.aclose()


def _write(directory: Path, key: str, body: Mapping[str, Any]) -> Path:
    path = directory / fake.keyed_answer_filename(key)
    path.write_bytes(synthetic_sse(dict(body), model=MODEL))
    return path


async def _ask(fake_llm: FakeLLM, key: str | None) -> Any:
    """One structured `generate` try through `_chat` on the fake, sent with `key` as `X-JH-Fake-Key`."""
    async with fake_llm.client() as client:
        runnable = _chat(
            MODEL,
            "medium",
            1000,
            WireSchema.from_model(GatewayOut).json,
            api_key=KEY,
            http_client=client,
            purpose=cast(Any, "generate"),
            fake_key=key,
        )
        out = await runnable.ainvoke([HumanMessage("A synthetic question.")])
    assert isinstance(out, dict)
    return out["parsed"]


def test_keyed_answer_filename() -> None:
    assert fake.keyed_answer_filename("generate:123:0") == "generate_123_0.sse"
    assert fake.keyed_answer_filename("critic:None:2") == "critic_None_2.sse"
    for key in ("generate:1/2:0", "generate:..:0", "..", "a\\b", "generate:x..y:0"):
        with pytest.raises(ValueError, match="key"):
            fake.keyed_answer_filename(key)


async def test_a_keyed_file_answers_its_key_on_every_try(keyed: tuple[FakeLLM, Path]) -> None:
    fake_llm, directory = keyed
    _write(directory, "generate:7:0", ONE)
    first, second = await _ask(fake_llm, "generate:7:0"), await _ask(fake_llm, "generate:7:0")
    other = await _ask(fake_llm, "generate:8:0")  # no file for this key: the recording
    unkeyed = await _ask(fake_llm, None)
    assert (first, second, other, unkeyed) == (ONE, ONE, GATEWAY_BODY, GATEWAY_BODY)
    assert [(r.key, r.scenario, r.status) for r in fake_llm.requests] == [
        ("generate:7:0", "ok", 200),
        ("generate:7:0", "ok", 200),
        ("generate:8:0", "ok", 200),
        (None, "ok", 200),
    ]
    assert fake_llm.state.tries == 2  # the keyed tries took no name of the script


async def test_the_keyed_file_is_read_at_each_try(keyed: tuple[FakeLLM, Path]) -> None:
    """No caching: a rewritten file answers the next try, a removed one leaves the key to the recording."""
    fake_llm, directory = keyed
    path = _write(directory, "generate:7:0", ONE)
    assert await _ask(fake_llm, "generate:7:0") == ONE
    _write(directory, "generate:7:0", TWO)
    assert await _ask(fake_llm, "generate:7:0") == TWO
    path.unlink()
    assert await _ask(fake_llm, "generate:7:0") == GATEWAY_BODY


async def test_a_registered_keyed_answer_wins_over_the_file(keyed: tuple[FakeLLM, Path]) -> None:
    fake_llm, directory = keyed
    _write(directory, "generate:7:0", ONE)
    fake.answer("generate:7:0", synthetic_sse(dict(TWO), model=MODEL), consume=True)
    assert await _ask(fake_llm, "generate:7:0") == TWO  # the state's one-shot answer first
    assert await _ask(fake_llm, "generate:7:0") == ONE  # then the file
    fake.answer("generate:7:0", synthetic_sse(dict(TWO), model=MODEL))
    assert await _ask(fake_llm, "generate:7:0") == TWO  # a standing registered answer as well


async def test_the_keyed_file_answers_before_the_scenario_script(keyed: tuple[FakeLLM, Path]) -> None:
    fake_llm, directory = keyed
    _write(directory, "generate:7:0", ONE)
    fake_llm.scenario("rate_limited")
    assert await _ask(fake_llm, "generate:7:0") == ONE
    with pytest.raises(anthropic.RateLimitError):
        await _ask(fake_llm, "generate:8:0")  # no file: the script's name
    assert [(r.key, r.scenario, r.status) for r in fake_llm.requests] == [
        ("generate:7:0", "ok", 200),
        ("generate:8:0", "rate_limited", 429),
    ]


@pytest.mark.parametrize("key", ["generate:1/2:0", "generate:..:0"])
async def test_a_key_that_names_no_file_raises_at_the_lookup(keyed: tuple[FakeLLM, Path], key: str) -> None:
    """A key holding `/` or `..` is refused where the directory is read: the try is never answered by the
    recording in its place, and the fake records no try."""
    fake_llm, directory = keyed
    transport = fake.RecordedSSETransport(fake_llm.root, keyed_dir=directory)
    with pytest.raises(ValueError, match="names no file"):
        transport.keyed_answer(key)
    with pytest.raises(anthropic.APIConnectionError) as exc:
        await _ask(fake_llm, key)
    assert any(isinstance(cause, ValueError) for cause in _causes(exc.value))
    assert fake_llm.requests == [] and fake_llm.served == 0


def _causes(error: BaseException) -> list[BaseException]:
    seen, current = [], error.__cause__ or error.__context__
    while current is not None:
        seen.append(current)
        current = current.__cause__ or current.__context__
    return seen


async def test_without_a_keyed_dir_no_file_is_read(fake_llm: FakeLLM, tmp_path: Path) -> None:
    """The variable unset (every test's fake, and the dev stack's three files): a file named like a key's
    answer anywhere answers nothing."""
    assert fake_llm.cfg.fake_llm_keyed_dir is None
    (tmp_path / fake.keyed_answer_filename("generate:7:0")).write_bytes(synthetic_sse(dict(ONE), model=MODEL))
    assert await _ask(fake_llm, "generate:7:0") == GATEWAY_BODY


BASE_ENV: Final = {
    "JOBHUNTER_ENV": "prod",
    "JOBHUNTER_DB_NAME": "jobhunter",
    "JOBHUNTER_PUBLIC_ORIGIN": "https://jobs.example",
}


@pytest.mark.parametrize(
    "variable",
    ["JOBHUNTER_FAKE_LLM_KEYED_DIR", "JOBHUNTER_FAKE_BATCH_SCRIPT"],
    ids=["keyed_dir", "batch_script"],
)
def test_prod_refuses_the_keyed_dir(variable: str, tmp_path: Path) -> None:
    """Both stack controls of the fake LLM are fake mode (`_FAKE_MODE_VARS`): a prod process refuses to start
    with either set, even empty, and names it; dev and test read it as a path."""
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    for name, content in (("data_key", base64.b64encode(b"k" * 32)), ("db_app", b"pw")):
        (secrets / name).write_bytes(content)
        (secrets / name).chmod(0o440)
    path = str(tmp_path / "fake-x")
    for value in (path, ""):
        cfg = load_config("worker", {**BASE_ENV, variable: value}, secrets)
        with pytest.raises(StartupCheckFailed) as exc:
            run_startup_assertions(cfg)
        assert exc.value.check == "fake_mode_in_prod" and variable in exc.value.detail
    dev = load_config("worker", {**BASE_ENV, "JOBHUNTER_ENV": "dev", variable: path}, secrets)
    run_startup_assertions(dev)
    read = dev.fake_llm_keyed_dir if variable.endswith("KEYED_DIR") else dev.fake_batch_script
    assert read == Path(path)
