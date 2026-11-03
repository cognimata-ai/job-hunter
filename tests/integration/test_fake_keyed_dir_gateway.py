"""The gateway selects the stack's keyed file (M4 Task 18): a real `LLMGateway.call` in fake mode, with
`JOBHUNTER_FAKE_LLM_KEYED_DIR` set, computes and sends `X-JH-Fake-Key` itself (`fake.answer_key` of its node's
purpose, the prompt's `input_refs` and the round, `llm/gateway.py`), and the fake answers that key with the
file written for it under `fake.keyed_answer_filename`, as the E2E setup writes E9's grounded answers (Task
32). A call for another revision gets the recording. The ledger steps run on the real database."""

import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Final

import pytest
from jobhunter.llm import fake
from jobhunter.llm.gateway import CallContext, NodeConfig, RenderedPrompt, SystemBlock, WireSchema
from jobhunter.llm.pricing import MODELS
from jobhunter.llm.spend_ledger import TargetAttemptOwner
from langchain_core.messages import HumanMessage
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.llm import (
    GATEWAY_BODY,
    FakeLLM,
    GatewayOut,
    active_execution_config,
    fake_llm_config,
    synthetic_sse,
)

pytestmark = pytest.mark.integration
MODEL: Final = "claude-sonnet-5-5"
SCORE_NODE: Final = NodeConfig(
    purpose="generate",
    model=MODEL,
    effort="medium",
    max_tokens=6000,
    max_tokens_retry=12000,
    deadline_s=600,
    schema=WireSchema.from_model(GatewayOut),
)
GROUNDED: Final = {"answer": "the grounded answer of the E2E setup", "score": 5}
REVISION: Final = 4242


@pytest.fixture
async def keyed_llm(tmp_path: Path) -> AsyncIterator[tuple[FakeLLM, Path]]:
    """The fake over `GatewayOut` recordings with a keyed directory, as `deploy/compose.e2e.yaml` sets one."""
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


def score_prompt(job_revision_id: int) -> RenderedPrompt:
    return RenderedPrompt(
        system=(SystemBlock("You score a job ad.", cache=True),),
        messages=(HumanMessage("A synthetic ad."),),
        template_version="score-test@1",
        input_refs={"job_revision_id": job_revision_id, "prompt_schema_version_id": 2, "truncated": False},
    )


def score_context(owner: TargetAttemptOwner, ec: int, **overrides: Any) -> CallContext:
    values: dict[str, Any] = {
        "owner": owner,
        "purpose_class": "scoring",
        "execution_config_id": ec,
        "deadline": time.monotonic() + SCORE_NODE.deadline_s,
        "attempt_id": owner.attempt_id,
        "target_id": owner.target_id,
        "round": 0,
        "logical_call_no": 1,
        **overrides,
    }
    return CallContext(**values)


async def test_the_gateway_header_selects_the_keyed_file(
    engine: AsyncEngine, keyed_llm: tuple[FakeLLM, Path], target_owner: TargetAttemptOwner
) -> None:
    fake_llm, directory = keyed_llm
    ec = await active_execution_config(engine)
    name = fake.keyed_answer_filename(fake.answer_key("generate", {"job_revision_id": REVISION}, 0))
    assert name == f"generate_{REVISION}_0.sse"
    (directory / name).write_bytes(synthetic_sse(GROUNDED, model=MODEL))
    gateway = fake_llm.gateway(engine)

    keyed = await gateway.call(SCORE_NODE, score_prompt(REVISION), score_context(target_owner, ec))
    other = await gateway.call(
        SCORE_NODE, score_prompt(REVISION + 1), score_context(target_owner, ec, logical_call_no=2)
    )

    assert keyed.parsed == GatewayOut.model_validate(GROUNDED)
    assert other.parsed == GatewayOut.model_validate(GATEWAY_BODY)  # no file for that revision: the recording
    assert [(r.key, r.scenario, r.status) for r in fake_llm.requests] == [
        (f"generate:{REVISION}:0", "ok", 200),
        (f"generate:{REVISION + 1}:0", "ok", 200),
    ]
    assert fake_llm.state.tries == 1  # the keyed try took no name of the script
