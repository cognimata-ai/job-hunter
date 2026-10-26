"""Input bounds (spec §10.7, chair decision C1). The provider documents token counting as an estimate, so
text is not counted but bounded: by the UTF-8 byte length of every rendered component, on the stated
assumption that the tokenizer emits at most one token per byte (spike V19 verifies it), plus a fixed
per-request overhead. A native PDF, which has no byte length to bound, reserves a quarter more than the
count the provider returns, plus the same overhead and a share of the overrun buffer.

The block between the two spike-V19 markers is written by `scripts/spikes/v19_token_bound.py` from its
measurements (`docs/spikes/V19.md`):
- `PROMPT_OVERHEAD_TOKENS[model]` covers protocol, role and schema-rendering tokens: twice the largest framing
  overhead V19 measured for the model (a whole request's `count_tokens` minus the sum of its components'
  counts, each counted on its own), and never below `PROMPT_OVERHEAD_FLOOR`;
- `NATIVE_PDF_ENABLED` is whether §12.7's native-PDF path may run: V19's one native-PDF extraction used at
  most 1.25 x its `count_tokens`. While it is false, no native-PDF request is admitted (plan Deviation 9).

V19 first measured 2,640 tokens (2 x 1,320, the framing of a request with the §11.5 generator schema, which
the API rejects). M3 Task 4 re-counted on 2026-10-26 (`--count`, `--constants`, `--analyse`; no message
sent), with the generator and critic requests rendered by `prompts.render.score_prompt` from prompt-schema
version 2 (the generator's schema is candidate C): the largest framing is now the profile extraction's 939
tokens, so the block holds 1,878 (2 x 939) for every model, and the §6.9.1 overrun-buffer bound of the seeded
settings is $1.23 against their $2.00 buffer (`docs/spikes/V19.md`, "M3 re-count").
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from types import MappingProxyType
from typing import Final

from jobhunter.llm.pricing import Model

# The overhead every model has before V19 measures it, and below which V19 never sets it (§10.7).
PROMPT_OVERHEAD_FLOOR: Final = 1_000

# --- spike V19: scripts/spikes/v19_token_bound.py writes this block (docs/spikes/V19.md) ---
PROMPT_OVERHEAD_TOKENS: Final[Mapping[Model, int]] = MappingProxyType(
    {
        "claude-opus-5-5": 1_878,
        "claude-sonnet-5-5": 1_878,
        "claude-haiku-5-5": 1_878,
        "claude-fable-5-1": 1_878,
    }
)
NATIVE_PDF_ENABLED: Final[bool] = True
# --- end of the spike V19 block ---

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
