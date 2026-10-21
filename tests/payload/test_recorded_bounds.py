"""The input bound covers every real recording (spec §10.9 last bullet, §10.7; plan Task 6, F38).

For each stream the budgeted recorder committed (`"source": "recorded"` in its `.components.json` sidecar,
Tasks 4 and 7), the bound computed by `bounds.text_input_bound` from the byte sizes of the components it sent
(the sidecar) plus `PROMPT_OVERHEAD_TOKENS[model]` is at least the usage the stream recorded: input tokens
plus both cache counts. Hand-authored synthetic streams (Tasks 13 and 20) are no recordings and are skipped.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from jobhunter.llm import fake
from jobhunter.llm.bounds import PROMPT_OVERHEAD_TOKENS, RenderedRequest, text_input_bound

from tests.support.llm import FAKE_LLM_DIR

SIDECARS = sorted(FAKE_LLM_DIR.rglob("*.components.json"))


def _meta(path: Path) -> dict[str, Any]:
    meta = json.loads(path.read_text("utf-8"))
    assert isinstance(meta, dict)
    return meta


RECORDED = [path for path in SIDECARS if _meta(path)["source"] == "recorded"]


def _stream_usage(sidecar: Path) -> dict[str, int]:
    """The final usage the recorded stream carries: `message_start`'s, updated by `message_delta`'s."""
    stream = sidecar.with_name(sidecar.name.removesuffix(".components.json") + ".sse")
    usage: dict[str, int] = {}
    for event in fake.parse_sse(stream.read_bytes()):
        if event.name == "message_start":
            usage.update(event.data["message"]["usage"])
        elif event.name == "message_delta":
            usage.update({k: v for k, v in event.data["usage"].items() if v is not None})
    return usage


def _sized_request(sizes: dict[str, Any]) -> RenderedRequest:
    """A request whose components have the recorded byte sizes (ASCII filler: one byte per character)."""
    return RenderedRequest(
        system=tuple("s" * n for n in sizes["system"]),
        messages=tuple("m" * n for n in sizes["messages"]),
        schema_json="j" * sizes["schema"],
    )


def test_every_sidecar_is_labelled_and_there_are_real_recordings() -> None:
    assert {_meta(path)["source"] for path in SIDECARS} <= {"recorded", "synthetic"}
    assert RECORDED, "no recorded stream: the bound check would be vacuous"


@pytest.mark.parametrize("sidecar", RECORDED, ids=lambda p: str(p.relative_to(FAKE_LLM_DIR)))
def test_the_input_bound_covers_the_recorded_usage(sidecar: Path) -> None:
    meta = _meta(sidecar)
    usage = _stream_usage(sidecar)
    used = usage["input_tokens"] + usage["cache_creation_input_tokens"] + usage["cache_read_input_tokens"]
    assert used > 0
    bound = text_input_bound(meta["model"], _sized_request(meta["component_bytes"]))
    sizes = meta["component_bytes"]
    assert (
        bound
        == sum(sizes["system"])
        + sum(sizes["messages"])
        + sizes["schema"]
        + PROMPT_OVERHEAD_TOKENS[meta["model"]]
    )
    assert bound >= used, (bound, used)
    assert usage["input_tokens"] == meta["usage"]["input_tokens"]  # the sidecar agrees with its stream
