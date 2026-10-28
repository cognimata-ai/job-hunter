"""The dev stack's fake-LLM overlay (`deploy/compose.fakellm.yaml`; M2 Task 23, M3 Task 20 Ruling T20-c):
the worker and the api both run on the fake LLM, so the api's eval gate counts the fake eval runs the worker
makes and its settings save needs no live schema dry-run, as the worker's score lane does
(`cycles.pause_reason`)."""

from pathlib import Path
from typing import Any, Final

import yaml

OVERLAY: Final = Path(__file__).parents[2] / "deploy" / "compose.fakellm.yaml"


def test_the_fake_llm_overlay_puts_the_worker_and_the_api_on_the_fake() -> None:
    services: dict[str, Any] = yaml.safe_load(OVERLAY.read_text("utf-8"))["services"]
    assert services["worker"]["environment"]["JOBHUNTER_FAKE_LLM"] == "1"
    assert services["api"]["environment"]["JOBHUNTER_FAKE_LLM"] == "1"
