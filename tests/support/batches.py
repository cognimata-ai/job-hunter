"""The fake Message Batches API as a test drives it (M3 Task 8).

`BatchScript`, `ItemOutcome` and `StoredBatch` are defined in `jobhunter.llm.fake`, the module that serves the
batch routes (backend code never imports `tests`), and re-exported here. The builders:
- `scripted_batches(script)` (or `scripted_batches(**fields)`, a `BatchScript` of those fields) registers the
  script in the test's fake state and returns the batches the fake stores, so a test asserts what was
  submitted (`requests`, `custom_ids`, `keys`), polled (`retrievals`, `results_read`) and cancelled
  (`cancel_requested`);
- `synthetic_batch_results_jsonl(items)` writes the results JSONL the fake serves for those item outcomes and
  one answer, in the given order, for a result reader fed without the fake.

Every value is synthetic: ids, counts and the synthetic draft bodies of `tests.support.llm`.
"""

import json
from collections.abc import Mapping
from typing import Any

from jobhunter.llm import fake
from jobhunter.llm.fake import ITEM_ERRORS, BatchScript, ItemOutcome, StoredBatch

from tests.support.llm import generator_wire_body, synthetic_message_json

__all__ = ["BatchScript", "ItemOutcome", "StoredBatch", "scripted_batches", "synthetic_batch_results_jsonl"]


def scripted_batches(script: BatchScript | None = None, /, **fields: Any) -> dict[str, StoredBatch]:
    """Register `script` (else `BatchScript(**fields)`) as the test's batch script and return the batches
    the fake stores (the live mapping, by batch id, in creation order). A later call replaces the script for
    every later call of the API, stored batches included: a batch's item outcomes are decided, and then
    frozen, at its cancel or when it is first observed as ended (`jobhunter.llm.fake`)."""
    if script is not None and fields:
        raise ValueError("pass a BatchScript or its fields, not both")
    return fake.batches(BatchScript(**fields) if script is None else script).batches


def synthetic_batch_results_jsonl(
    items: Mapping[str, ItemOutcome],
    *,
    model: str = "claude-sonnet-5-5",
    body: Any = None,
    usage: dict[str, int] | None = None,
    max_tokens: int = 6000,
    message_id: str = "msg_0123456789synthetic",
) -> bytes:
    """The results JSONL of a batch whose items (by `custom_id`, in this order) end with these outcomes, each
    `succeeded*` one answering `body` (by default the C draft `generator_wire_body`) on `model` with `usage`,
    as the fake serves them: `succeeded:max_tokens` cut to half its text with `max_tokens` output tokens,
    `succeeded:refusal` without content, `succeeded:unexpected_stop` at `tool_use`, `succeeded:invalid` with
    text that is no JSON, the two `errored:*` with their provider error, and no line for a `missing` item."""
    answered = generator_wire_body() if body is None else body
    text = answered if isinstance(answered, str) else json.dumps(answered, ensure_ascii=False)
    lines: list[bytes] = []
    for custom_id, outcome in items.items():
        result: dict[str, Any]
        if outcome == "missing":
            continue
        if outcome in ("canceled", "expired"):
            result = {"type": outcome}
        elif outcome in ITEM_ERRORS:
            kind, message = ITEM_ERRORS[outcome]
            result = {
                "type": "errored",
                "error": {"type": "error", "error": {"type": kind, "message": message}},
            }
        else:
            result = {
                "type": "succeeded",
                "message": _message(outcome, text, model, usage, max_tokens, message_id),
            }
        line = {"custom_id": custom_id, "result": result}
        lines.append((json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
    return b"".join(lines)


def _message(
    outcome: ItemOutcome,
    text: str,
    model: str,
    usage: dict[str, int] | None,
    max_tokens: int,
    message_id: str,
) -> dict[str, Any]:
    common: dict[str, Any] = {"model": model, "usage": usage, "message_id": message_id}
    if outcome == "succeeded:max_tokens":
        cut = synthetic_message_json(text[: max(1, len(text) // 2)], stop_reason="max_tokens", **common)
        cut["usage"]["output_tokens"] = max_tokens
        return cut
    if outcome == "succeeded:refusal":
        details = {"type": "refusal", "category": fake.REFUSAL_CATEGORY, "explanation": None}
        return synthetic_message_json(None, stop_reason="refusal", stop_details=details, **common)
    if outcome == "succeeded:unexpected_stop":
        return synthetic_message_json(text, stop_reason=fake.UNEXPECTED_STOP_REASON, **common)
    if outcome == "succeeded:invalid":
        return synthetic_message_json(fake.PARSE_ERROR_TEXT, **common)
    return synthetic_message_json(text, **common)
