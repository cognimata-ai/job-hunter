"""Spike V12 (spec §22 L6664; M3 Task 26): one 3-item Message Batch on `claude-haiku-5-5`, with the real
generator schema C, through `BatchesClient`.

    uv run python scripts/spikes/record_anthropic.py --spike v12 --dry-run|--live

V12 is the budgeted recorder's batch spike (`record_anthropic.BATCH_SPIKES`, `SPIKE_CALLS["v12"] = 1`: one
batch, never a second without the controller). `batch_requests("v12")` plans its three items, `custom_id`s
`t1_a1`, `t2_a1` and `t3_a1`: the online generator request of three synthetic ads rendered by
`render.score_prompt` from prompt-schema version 2, on one synthetic profile (V5's pair 1, so the three share
the prefix and its cache marker, which the batch params keep, L6622), at the generator node of the seeded
policy with its model replaced by Haiku (its effort and first `max_tokens` stage kept). So Haiku's compilation
of schema C is proven too (A3 §3.7). The recorder makes each item's params with
`LLMGateway.batch_request_body`, reserves the batch's worst case (the sum of its items' batch worst cases),
creates the batch, polls `retrieve` every 60 s until it ends, streams the results, settles the batch at batch
prices and lists the workspace until it finds the batch (an empty listing is never proof of absence, C3).

`check_batch(docs)` reads the committed documents (`create`, `retrieve`, `results`, `list`) and returns field
paths, flags and counts, never output content: the `custom_id` pattern, every succeeded item's usage, the
cache counts of each item (§21.4's cold-cache assumption: the first item writes the prefix, the others may
read it), the item results, and whether an errored item's type sits at the SDK's nested `error.error.type`
(Carry T8-b). Every input is synthetic.
"""

import dataclasses
import importlib.util
import re
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Any, Final

from jobhunter.llm.gateway import node_config
from jobhunter.llm.prompts import render

SPIKE: Final = "v12"
MODEL: Final = "claude-haiku-5-5"
ITEMS: Final = 3
CUSTOM_ID: Final = re.compile(r"t\d+_a1")
HERE: Final = Path(__file__).resolve().parent


def _v5() -> ModuleType:
    spec = importlib.util.spec_from_file_location("jobhunter_spike_v5_inputs", HERE / "v5_stream_timing.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("v5_stream_timing.py is missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


V5: Final = _v5()


def batch_requests(spike: str) -> list[dict[str, Any]]:
    """The batch's items in order: `custom_id`, `node` (the seeded generator node on Haiku) and `prompt`."""
    if spike != SPIKE:
        raise ValueError(f"no batch spike {spike!r} here")
    node = node_config(
        "generate",
        model_policy=V5.POLICY,
        execution=V5.EXECUTION,
        schema=render.score_schema("generate", V5.V2),
    )
    haiku = dataclasses.replace(node, model=MODEL)
    common = {"version": V5.V2, "rubric": V5.load_rubric(V5.RUBRIC_V2), "profile": V5.profile(1)}
    return [
        {
            "custom_id": f"t{i}_a1",
            "node": haiku,
            "prompt": render.score_prompt("generate", **common, ad=V5.ad(i)),
        }
        for i in range(1, ITEMS + 1)
    ]


def check_batch(docs: Mapping[str, Any]) -> dict[str, Any]:
    """The verdict of the batch's documents: paths, flags and counts only (module docstring)."""
    lines = docs.get("results") or []
    results = {line.get("custom_id"): line.get("result") or {} for line in lines}
    usage: dict[str, Any] = {}
    errored: dict[str, Any] = {}
    for custom_id, result in results.items():
        if result.get("type") == "succeeded":
            u = (result.get("message") or {}).get("usage") or {}
            usage[str(custom_id)] = {
                key: u.get(key)
                for key in (
                    "input_tokens",
                    "cache_creation_input_tokens",
                    "cache_read_input_tokens",
                    "output_tokens",
                )
            }
        elif result.get("type") == "errored":
            nested = ((result.get("error") or {}).get("error") or {}).get("type")
            errored[str(custom_id)] = {
                "nested_error_type": nested,
                "top_type": (result.get("error") or {}).get("type"),
            }
    listed = (docs.get("list") or {}).get("data") or []
    retrieves = docs.get("retrieve") or []
    checks = {
        "three_items": len(results) == ITEMS,
        "custom_id_pattern": all(isinstance(c, str) and CUSTOM_ID.fullmatch(c) for c in results),
        "usage_present": all(
            all(isinstance(v, int) for k, v in u.items() if k in ("input_tokens", "output_tokens"))
            for u in usage.values()
        ),
        "ended": bool(retrieves) and retrieves[-1].get("processing_status") == "ended",
        "listed": len(listed) == 1 and bool(listed[0].get("created_at")) and "request_counts" in listed[0],
        "model_served": all(
            ((r.get("message") or {}).get("model") == MODEL)
            for r in results.values()
            if r.get("type") == "succeeded"
        ),
    }
    t8b = (
        "no errored item"
        if not errored
        else ("nested" if all(e["nested_error_type"] for e in errored.values()) else "not nested")
    )
    return {
        "expected": all(checks.values()),
        "missing": [name for name, ok in checks.items() if not ok],
        "checks": checks,
        "results": {str(c): r.get("type") for c, r in results.items()},
        "stop_reasons": {
            str(c): (r.get("message") or {}).get("stop_reason")
            for c, r in results.items()
            if r.get("type") == "succeeded"
        },
        "usage": usage,
        "errored": errored,
        "t8b": t8b,
        "request_counts": (listed[0].get("request_counts") if listed else None),
        "retrieves": len(retrieves),
        "lines": [
            f"results={ {str(c): r.get('type') for c, r in results.items()} } "
            f"missing={[n for n, ok in checks.items() if not ok]}",
            f"usage={usage}",
            f"T8-b: {t8b}",
        ],
    }
