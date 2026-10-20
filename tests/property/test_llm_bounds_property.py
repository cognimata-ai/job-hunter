"""The reservation always covers the settlement (spec §10.7): for every model, at the full and at the batch
price, a call whose real prompt stays within its input bound and whose output stays within `max_tokens`
settles for no more than `worst_case_usd` reserved for it."""

from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st
from jobhunter.llm.bounds import RenderedRequest, native_pdf_bound, native_pdf_extra_usd, text_input_bound
from jobhunter.llm.pricing import MODELS, Model, Usage, settled_usd, worst_case_usd

models = st.sampled_from(MODELS)
texts = st.text(max_size=200)  # umlauts, ß, typographic quotes and dashes among the characters
# ASCII padding takes the bound past 100,000 tokens (the Haiku long-prompt tier, reached at 99,000 bytes
# with the 1,000-token overhead); the second branch concentrates on that boundary.
padding = st.one_of(st.integers(0, 1_500_000), st.integers(98_500, 99_500))
counted_tokens = st.one_of(st.integers(0, 1_000_000), st.integers(79_000, 81_000))  # 1.25 x 80,000 = 100,000
max_tokens_values = st.one_of(st.integers(0, 64_000), st.integers(0, 1_024))


@st.composite
def rendered_requests(draw: st.DrawFn) -> RenderedRequest:
    return RenderedRequest(
        system=(draw(texts), draw(texts)),
        messages=(draw(texts), "x" * draw(padding)),
        schema_json=draw(texts),
    )


def up_to(limit: int) -> st.SearchStrategy[int]:
    """A count of at most `limit`, drawn often at or next to it: the reservation is tight only there."""
    return st.one_of(st.just(limit), st.integers(max(0, limit - 3), limit), st.integers(0, limit))


@st.composite
def usages_within(draw: st.DrawFn, *, prompt_bound: int, max_tokens: int) -> Usage:
    """A usage whose prompt tokens (input plus both cache counts) are at most `prompt_bound`."""
    prompt = draw(up_to(prompt_bound))
    cache_creation = draw(up_to(prompt))  # often all of it: the cache-write price is the highest
    cache_read = draw(up_to(prompt - cache_creation))
    return Usage(
        input_tokens=prompt - cache_creation - cache_read,
        output_tokens=draw(up_to(max_tokens)),
        cache_creation_input_tokens=cache_creation,
        cache_read_input_tokens=cache_read,
    )


@given(models, st.booleans(), rendered_requests(), max_tokens_values, st.data())
def test_the_reservation_covers_the_settlement(
    model: Model, batch: bool, request: RenderedRequest, max_tokens: int, data: st.DataObject
) -> None:
    bound = text_input_bound(model, request)
    reserved = worst_case_usd(model, input_tokens_bound=bound, max_tokens=max_tokens, batch=batch)
    # The tokenizer emits at most one token per byte, so a real input is at most bound - overhead; the draw
    # goes up to the whole bound, which also covers the overhead tokens themselves.
    tokens_in = data.draw(up_to(bound))
    tokens_out = data.draw(up_to(max_tokens))
    settled = settled_usd(model, Usage(tokens_in, tokens_out, 0, 0), batch=batch)
    assert reserved >= settled


@given(models, st.booleans(), rendered_requests(), max_tokens_values, st.data())
def test_the_reservation_covers_every_token_class(
    model: Model, batch: bool, request: RenderedRequest, max_tokens: int, data: st.DataObject
) -> None:
    bound = text_input_bound(model, request)
    reserved = worst_case_usd(model, input_tokens_bound=bound, max_tokens=max_tokens, batch=batch)
    usage = data.draw(usages_within(prompt_bound=bound, max_tokens=max_tokens))
    assert reserved >= settled_usd(model, usage, batch=batch)


@given(models, st.booleans(), counted_tokens, st.sampled_from([Decimal("0.50"), Decimal("2.00")]), st.data())
def test_the_native_pdf_reservation_covers_every_token_class(
    model: Model, batch: bool, counted: int, buffer: Decimal, data: st.DataObject
) -> None:
    bound = native_pdf_bound(model, counted)
    reserved = worst_case_usd(
        model, input_tokens_bound=bound, max_tokens=1_024, batch=batch, extra_usd=native_pdf_extra_usd(buffer)
    )
    usage = data.draw(usages_within(prompt_bound=bound, max_tokens=1_024))
    assert reserved >= settled_usd(model, usage, batch=batch)
