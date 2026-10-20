"""Input bounds of the LLM gateway (spec §10.7, chair decision C1): text is bounded by its UTF-8 bytes plus a
fixed overhead, a native PDF by 1.25 x its counted tokens plus the same overhead."""

from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest
from jobhunter.llm.bounds import (
    PROMPT_OVERHEAD_TOKENS,
    RenderedRequest,
    native_pdf_bound,
    native_pdf_extra_usd,
    text_input_bound,
)
from jobhunter.llm.pricing import MODELS

OVERHEAD = 1_000


def request(
    *, system: tuple[str, ...] = (), messages: tuple[str, ...] = (), schema_json: str = ""
) -> RenderedRequest:
    return RenderedRequest(system=system, messages=messages, schema_json=schema_json)


def test_the_overhead_is_the_floor_of_1000_for_every_model() -> None:
    assert set(PROMPT_OVERHEAD_TOKENS) == set(MODELS)
    assert dict(PROMPT_OVERHEAD_TOKENS) == dict.fromkeys(MODELS, OVERHEAD)


def test_german_text_counts_bytes_not_characters() -> None:
    text = "äß„“\u2013"  # 5 characters: 2 + 2 + 3 + 3 + 3 = 13 UTF-8 bytes
    assert len(text) == 5
    bound = text_input_bound("claude-opus-5-5", request(messages=(text,)))
    assert bound == 13 + OVERHEAD
    assert bound != len(text) + OVERHEAD
    sentence = "Größe: „Zähler“ \u2013 Straße"  # 24 characters, 34 bytes
    assert text_input_bound("claude-sonnet-5-5", request(system=(sentence,))) == 34 + OVERHEAD


def test_every_component_counts_once_and_the_overhead_is_added_once() -> None:
    req = request(system=("aa", "bbb"), messages=("c", "dddd", "ä"), schema_json="eeeee")
    for model in MODELS:
        # 2 + 3 + 1 + 4 + 2 + 5 bytes, then the overhead a single time
        assert text_input_bound(model, req) == 17 + PROMPT_OVERHEAD_TOKENS[model]


def test_each_kind_of_component_counts() -> None:
    base = text_input_bound("claude-opus-5-5", request())
    assert base == OVERHEAD  # nothing rendered: the overhead alone
    assert text_input_bound("claude-opus-5-5", request(system=("x" * 10,))) == base + 10
    assert text_input_bound("claude-opus-5-5", request(messages=("x" * 20,))) == base + 20
    assert text_input_bound("claude-opus-5-5", request(schema_json="x" * 30)) == base + 30


def test_the_schema_json_counts_with_its_multibyte_characters() -> None:
    schema = '{"title": "Größe", "type": "object"}'
    assert len(schema) == 36
    assert len(schema.encode("utf-8")) == 38  # ö and ß are two bytes each
    assert text_input_bound("claude-haiku-5-5", request(schema_json=schema)) == 38 + OVERHEAD


def test_the_native_pdf_bound_is_a_quarter_over_the_count_plus_the_overhead() -> None:
    assert native_pdf_bound("claude-opus-5-5", 1_001) == 1_252 + OVERHEAD  # ceil(1251.25) + 1000
    assert native_pdf_bound("claude-opus-5-5", 1_000) == 1_250 + OVERHEAD  # exact: no round-up
    assert native_pdf_bound("claude-opus-5-5", 1_002) == 1_253 + OVERHEAD  # ceil(1252.5)
    assert native_pdf_bound("claude-opus-5-5", 1_003) == 1_254 + OVERHEAD  # ceil(1253.75)
    assert native_pdf_bound("claude-opus-5-5", 1_004) == 1_255 + OVERHEAD  # exact again
    assert native_pdf_bound("claude-opus-5-5", 0) == OVERHEAD
    for model in MODELS:
        assert native_pdf_bound(model, 1_001) == 1_252 + PROMPT_OVERHEAD_TOKENS[model]


def test_the_native_pdf_bound_is_exact_for_counts_a_float_would_misround() -> None:
    n = 2**53 + 1  # a float cannot hold this; the integer arithmetic does
    assert native_pdf_bound("claude-opus-5-5", n) == 11_258_999_068_426_242 + OVERHEAD  # ceil(1.25 x n)


def test_the_native_pdf_extra_is_a_quarter_of_the_buffer() -> None:
    assert native_pdf_extra_usd(Decimal("0.50")) == Decimal("0.125")  # the §13.1 minimum buffer
    assert native_pdf_extra_usd(Decimal("2.00")) == Decimal("0.50")  # the default buffer
    assert native_pdf_extra_usd(Decimal(0)) == Decimal(0)
    assert isinstance(native_pdf_extra_usd(Decimal("2.00")), Decimal)


def test_a_rendered_request_is_frozen() -> None:
    req = request(messages=("x",))
    with pytest.raises(FrozenInstanceError):
        req.schema_json = "{}"  # type: ignore[misc]
