"""`gateway.classify_message` (plan Task 17, Codex r1 #26): a batch item's `succeeded` message classified as
an online try of its first stage is, by the gateway's own `classify`. Offline; every message is synthetic.
"""

import json
from typing import Any, Final

import pytest
from anthropic.types import Message
from jobhunter.llm.gateway import classify_message
from jobhunter.llm.pricing import Model, Usage
from jobhunter.llm.prompts.render import score_schema

from tests.support.llm import synthetic_message_json, v2_version
from tests.support.scoring import synthetic_c_draft

SONNET: Final[Model] = "claude-sonnet-5-5"
SCHEMA = score_schema("generate", v2_version())


def message(body: Any, **fields: Any) -> Message:
    doc = synthetic_message_json(body, model=fields.pop("model", SONNET), **fields)
    return Message.model_validate(doc)


def code_of(body: Any, **fields: Any) -> str | None:
    return classify_message(node_model=SONNET, schema=SCHEMA, message=message(body, **fields)).code


def test_a_valid_end_turn_output_is_usable_with_its_usage() -> None:
    usage = {"input_tokens": 900, "cache_creation_input_tokens": 40, "cache_read_input_tokens": 0}
    classified = classify_message(
        node_model=SONNET, schema=SCHEMA, message=message(synthetic_c_draft(), usage=usage)
    )
    assert classified.code is None and classified.parsed is not None
    assert (classified.stop_reason, classified.model_served, classified.refusal_category) == (
        "end_turn",
        SONNET,
        None,
    )
    assert classified.usage == Usage(900, 60, 40, 0)  # the API's input_tokens hold no cache tokens


@pytest.mark.parametrize(
    ("body", "fields", "code"),
    [
        (synthetic_c_draft(), {"model": "claude-opus-5-5"}, "model_mismatch"),
        (synthetic_c_draft(), {"stop_reason": "max_tokens"}, "max_tokens_first_stage"),
        (None, {"stop_reason": "refusal"}, "refused"),
        (synthetic_c_draft(), {"stop_reason": "tool_use"}, "unexpected_stop_reason"),
        ("this is no JSON", {}, "invalid_output"),
        (json.dumps(synthetic_c_draft())[:150], {}, "invalid_output"),  # cut short
        ({"not": "a draft"}, {}, "invalid_output"),  # no fit to the structural mirror
        ({**synthetic_c_draft(), "summary_en": "A NUL\x00"}, {}, "invalid_output"),  # not storable
        (
            json.dumps({**synthetic_c_draft(), "summary_en": "\ud800"}),
            {},
            "invalid_output",
        ),  # a lone surrogate
    ],
)
def test_every_other_ending_has_the_gateways_code_and_no_output(
    body: Any, fields: dict[str, Any], code: str
) -> None:
    classified = classify_message(node_model=SONNET, schema=SCHEMA, message=message(body, **fields))
    assert (classified.code, classified.parsed) == (code, None)


def test_a_refusal_keeps_its_category() -> None:
    details = {"type": "refusal", "category": "cyber", "explanation": None}
    classified = classify_message(
        node_model=SONNET,
        schema=SCHEMA,
        message=message(None, stop_reason="refusal", stop_details=details),
    )
    assert (classified.code, classified.refusal_category) == ("refused", "cyber")
    assert code_of(None, stop_reason="refusal") == "refused"
