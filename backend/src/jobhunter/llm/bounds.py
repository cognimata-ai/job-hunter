"""Input bounds (spec §10.7, chair decision C1). The provider documents token counting as an estimate, so
text is not counted but bounded: by the UTF-8 byte length of every rendered component, on the stated
assumption that the tokenizer emits at most one token per byte (spike V19 verifies it), plus a fixed
per-request overhead. A native PDF, which has no byte length to bound, reserves a quarter more than the
count the provider returns, plus the same overhead and a share of the overrun buffer.
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from types import MappingProxyType
from typing import Final

from jobhunter.llm.pricing import MODELS, Model

# Protocol, role and schema-rendering tokens per request: the floor of 1,000 until spike V19 measures it
# (twice the largest framing overhead observed, Task 7).
PROMPT_OVERHEAD_TOKENS: Final[Mapping[Model, int]] = MappingProxyType(dict.fromkeys(MODELS, 1_000))

_NATIVE_PDF_FACTOR: Final = Fraction(5, 4)  # 1.25, exact: no float in the bound
_NATIVE_PDF_BUFFER_SHARE: Final = Decimal("0.25")


@dataclass(frozen=True)
class RenderedRequest:
    """Every rendered component of a request as the provider receives it: the `system` blocks, each message,
    and the `output_config` schema serialised as JSON."""

    system: tuple[str, ...]
    messages: tuple[str, ...]
    schema_json: str


def text_input_bound(model: Model, request: RenderedRequest) -> int:
    """`Σ utf8_bytes(component) + PROMPT_OVERHEAD_TOKENS[model]`, the overhead added once."""
    components = (*request.system, *request.messages, request.schema_json)
    return sum(len(component.encode("utf-8")) for component in components) + PROMPT_OVERHEAD_TOKENS[model]


def native_pdf_bound(model: Model, counted_tokens: int) -> int:
    """`ceil(1.25 x counted_tokens) + PROMPT_OVERHEAD_TOKENS[model]` for a native-PDF request."""
    return math.ceil(_NATIVE_PDF_FACTOR * counted_tokens) + PROMPT_OVERHEAD_TOKENS[model]


def native_pdf_extra_usd(overrun_buffer_usd: Decimal) -> Decimal:
    """The native-PDF request's share of the overrun buffer: `0.25 x overrun_buffer_usd`."""
    return _NATIVE_PDF_BUFFER_SHARE * overrun_buffer_usd
