"""Input bounds of the LLM gateway (spec §10.7, chair decision C1): text is bounded by its UTF-8 bytes plus a
fixed overhead, a native PDF by 1.25 x its counted tokens plus the same overhead."""

from dataclasses import FrozenInstanceError
from decimal import Decimal
from typing import Final

import pytest
from jobhunter.llm.bounds import (
    PROMPT_OVERHEAD_FLOOR,
    PROMPT_OVERHEAD_TOKENS,
    RenderedRequest,
    native_pdf_bound,
    native_pdf_extra_usd,
    text_input_bound,
)
from jobhunter.llm.pricing import MODELS

OPUS: Final = "claude-opus-5-5"
SONNET: Final = "claude-sonnet-5-5"
HAIKU: Final = "claude-haiku-5-5"


def request(
    *, system: tuple[str, ...] = (), messages: tuple[str, ...] = (), schema_json: str = ""
) -> RenderedRequest:
    return RenderedRequest(system=system, messages=messages, schema_json=schema_json)


def test_every_model_has_an_overhead_of_at_least_the_floor_of_1000() -> None:
    # spike V19 sets the values (twice the largest framing overhead it measured); never below the floor
    assert PROMPT_OVERHEAD_FLOOR == 1_000
    assert set(PROMPT_OVERHEAD_TOKENS) == set(MODELS)
    assert all(isinstance(v, int) and v >= PROMPT_OVERHEAD_FLOOR for v in PROMPT_OVERHEAD_TOKENS.values())


def test_german_text_counts_bytes_not_characters() -> None:
    text = "äß„“\u2013"  # 5 characters: 2 + 2 + 3 + 3 + 3 = 13 UTF-8 bytes
    assert len(text) == 5
    bound = text_input_bound(OPUS, request(messages=(text,)))
    assert bound == 13 + PROMPT_OVERHEAD_TOKENS[OPUS]
    assert bound != len(text) + PROMPT_OVERHEAD_TOKENS[OPUS]
    sentence = "Größe: „Zähler“ \u2013 Straße"  # 24 characters, 34 bytes
    assert text_input_bound(SONNET, request(system=(sentence,))) == 34 + PROMPT_OVERHEAD_TOKENS[SONNET]


def test_every_component_counts_once_and_the_overhead_is_added_once() -> None:
    req = request(system=("aa", "bbb"), messages=("c", "dddd", "ä"), schema_json="eeeee")
    for model in MODELS:
        # 2 + 3 + 1 + 4 + 2 + 5 bytes, then the overhead a single time
        assert text_input_bound(model, req) == 17 + PROMPT_OVERHEAD_TOKENS[model]


def test_each_kind_of_component_counts() -> None:
    base = text_input_bound(OPUS, request())
    assert base == PROMPT_OVERHEAD_TOKENS[OPUS]  # nothing rendered: the overhead alone
    assert text_input_bound(OPUS, request(system=("x" * 10,))) == base + 10
    assert text_input_bound(OPUS, request(messages=("x" * 20,))) == base + 20
    assert text_input_bound(OPUS, request(schema_json="x" * 30)) == base + 30


def test_the_schema_json_counts_with_its_multibyte_characters() -> None:
    schema = '{"title": "Größe", "type": "object"}'
    assert len(schema) == 36
    assert len(schema.encode("utf-8")) == 38  # ö and ß are two bytes each
    assert text_input_bound(HAIKU, request(schema_json=schema)) == 38 + PROMPT_OVERHEAD_TOKENS[HAIKU]


def test_the_native_pdf_bound_is_a_quarter_over_the_count_plus_the_overhead() -> None:
    overhead = PROMPT_OVERHEAD_TOKENS[OPUS]
    assert native_pdf_bound(OPUS, 1_001) == 1_252 + overhead  # ceil(1251.25), plus the overhead
    assert native_pdf_bound(OPUS, 1_000) == 1_250 + overhead  # exact: no round-up
    assert native_pdf_bound(OPUS, 1_002) == 1_253 + overhead  # ceil(1252.5)
    assert native_pdf_bound(OPUS, 1_003) == 1_254 + overhead  # ceil(1253.75)
    assert native_pdf_bound(OPUS, 1_004) == 1_255 + overhead  # exact again
    assert native_pdf_bound(OPUS, 0) == overhead
    for model in MODELS:
        assert native_pdf_bound(model, 1_001) == 1_252 + PROMPT_OVERHEAD_TOKENS[model]


def test_the_native_pdf_bound_is_exact_for_counts_a_float_would_misround() -> None:
    n = 2**53 + 1  # a float cannot hold this; the integer arithmetic does
    assert (
        native_pdf_bound(OPUS, n) == 11_258_999_068_426_242 + PROMPT_OVERHEAD_TOKENS[OPUS]
    )  # ceil(1.25 x n)


def test_the_native_pdf_extra_is_a_quarter_of_the_buffer() -> None:
    assert native_pdf_extra_usd(Decimal("0.50")) == Decimal("0.125")  # the §13.1 minimum buffer
    assert native_pdf_extra_usd(Decimal("2.00")) == Decimal("0.50")  # the default buffer
    assert native_pdf_extra_usd(Decimal(0)) == Decimal(0)
    assert isinstance(native_pdf_extra_usd(Decimal("2.00")), Decimal)


def test_a_rendered_request_is_frozen() -> None:
    req = request(messages=("x",))
    with pytest.raises(FrozenInstanceError):
        req.schema_json = "{}"  # type: ignore[misc]
