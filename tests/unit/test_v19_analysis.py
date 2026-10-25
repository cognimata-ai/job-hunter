"""The pure analysis of spike V19 (spec §10.7, §6.9.1, §22 V19; plan Task 7):
`scripts/spikes/v19_token_bound.py`.

The spike counts every component of a request on its own and the whole request with `messages.count_tokens`;
these tests pin what it concludes from the counts, offline:
- a component whose tokens exceed its UTF-8 bytes fails the one-token-per-byte check (§1.5 S3), and the §10.7
  factor rule then gives the smallest factor that restores the inequality with a 2x margin;
- `PROMPT_OVERHEAD_TOKENS[model]` is twice the largest framing overhead (whole minus the sum of the
  components) of that model's requests, never below the 1,000-token floor;
- the native-PDF check (`usage <= 1.25 x count_tokens`), the recorded-usage check against the bound, the
  §6.9.1 overrun-buffer bound of the seeded settings, and the rewrite of the generated block of `bounds.py`.
"""

import ast
import importlib.util
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from jobhunter.db.seed import load_settings_v1
from jobhunter.llm import bounds
from jobhunter.llm.pricing import MODELS

REPO = Path(__file__).resolve().parents[2]


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts/spikes" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


V19 = _load("v19_token_bound")
OPUS, SONNET = "claude-opus-5-5", "claude-sonnet-5-5"


def measured(model: str, whole: int, *components: tuple[str, int, int], kind: str = "generator") -> Any:
    """One counted request: its whole count and each component as `(name, bytes, tokens)`."""
    return V19.Measured(
        model=model,
        kind=kind,
        lang="de",
        whole=whole,
        components=tuple(V19.Component(name, size, tokens) for name, size, tokens in components),
    )


def corpus(model: str, framings: list[int]) -> list[Any]:
    """Requests of `model` whose framing overheads (whole minus the components) are `framings`."""
    return [
        measured(model, 300 + 50 + framing, ("system[0]", 1_200, 300), ("messages[0]", 200, 50))
        for framing in framings
    ]


def test_a_component_with_more_tokens_than_bytes_fails_the_check() -> None:
    ok = measured(OPUS, 130, ("system[0]", 400, 100), ("messages[0]", 40, 20))
    over = measured(OPUS, 150, ("system[0]", 400, 100), ("messages[0]", 10, 12), ("schema", 900, 250))
    analysis = V19.analyse([ok, over])
    assert analysis.tokens_within_bytes is False
    assert [(v.model, v.component, v.tokens, v.bytes) for v in analysis.violations] == [
        (OPUS, "messages[0]", 12, 10)
    ]
    # §10.7: the smallest factor that restores tokens <= bytes with a 2x margin: 2 x 12/10
    assert analysis.byte_factor == Fraction(12, 5)
    assert V19.byte_factor([V19.Violation(OPUS, "generator", "messages[0]", 12, 10)]) == Fraction(12, 5)


def test_tokens_equal_to_bytes_pass_and_no_factor_applies() -> None:
    analysis = V19.analyse([measured(OPUS, 40, ("system[0]", 30, 30), ("schema", 10, 9))])
    assert analysis.tokens_within_bytes is True
    assert analysis.violations == ()
    assert analysis.byte_factor is None


def test_the_framing_overhead_is_the_whole_minus_every_component() -> None:
    request = measured(
        OPUS, 1_000, ("system[0]", 2_000, 500), ("messages[0]", 600, 150), ("schema", 900, 270)
    )
    assert request.framing == 1_000 - 500 - 150 - 270
    assert request.bytes == 2_000 + 600 + 900


def test_the_overhead_constant_doubles_the_largest_framing_above_the_floor() -> None:
    assert V19.overhead_tokens(650) == 1_300  # 2 x 650
    assert V19.overhead_tokens(501) == 1_002
    assert V19.overhead_tokens(500) == 1_000  # 2 x 500 is the floor itself
    assert V19.overhead_tokens(120) == 1_000  # below the floor: the floor
    assert V19.overhead_tokens(-40) == 1_000  # whole below the sum of the components
    assert bounds.PROMPT_OVERHEAD_FLOOR == 1_000


def test_the_overhead_of_each_model_is_twice_its_own_largest_framing() -> None:
    analysis = V19.analyse(corpus(OPUS, [300, 650, 20]) + corpus(SONNET, [900, 880]))
    assert analysis.max_framing == {OPUS: 650, SONNET: 900}
    assert analysis.overhead == {OPUS: 1_300, SONNET: 1_800}


def test_the_constants_need_thirty_requests_of_every_model() -> None:
    full = [r for model in MODELS for r in corpus(model, [100] * V19.MIN_REQUESTS_PER_MODEL)]
    assert V19.MIN_REQUESTS_PER_MODEL == 30
    assert V19.overhead_constants(V19.analyse(full)) == dict.fromkeys(MODELS, 1_000)
    short = full[1:]  # the first model has 29
    with pytest.raises(ValueError, match="30"):
        V19.overhead_constants(V19.analyse(short))


def test_a_violation_keeps_the_constants_from_being_written() -> None:
    over = [
        measured(m, 20, ("messages[0]", 10, 12)) for m in MODELS for _ in range(V19.MIN_REQUESTS_PER_MODEL)
    ]
    with pytest.raises(ValueError, match="tokens > bytes"):
        V19.overhead_constants(V19.analyse(over))


def test_the_native_pdf_usage_may_be_at_most_a_quarter_above_its_count() -> None:
    assert V19.native_pdf_within(used=1_250, counted=1_000) is True
    assert V19.native_pdf_within(used=1_251, counted=1_000) is False
    assert V19.native_pdf_within(used=900, counted=1_000) is True
    assert V19.native_pdf_within(used=1, counted=0) is False


def test_a_recorded_usage_above_its_bound_is_reported() -> None:
    within = V19.RecordedUsage(spike="v4", case="a", model=OPUS, used=1_400, bound=1_500, native_pdf=False)
    above = V19.RecordedUsage(spike="v4", case="b", model=OPUS, used=1_501, bound=1_500, native_pdf=False)
    assert V19.usage_breaches([within, above]) == [above]


def _with_batch_items(settings: dict[str, Any], items: int) -> dict[str, Any]:
    return {**settings, "scoring": {**settings["scoring"], "batch_max_items": items}}


def test_the_overrun_buffer_bound_arithmetic() -> None:
    thousand = _with_batch_items(load_settings_v1(), 1_000)
    floor: dict[str, int] = dict.fromkeys(MODELS, 1_000)
    # §6.9.1's own example: 1,000 tokens everywhere and 1,000 batch items give about $1.28
    assert V19.overrun_buffer_bound(thousand, floor) == Decimal("1.28")
    # 5 online slots at Opus's 1,000 x $5.00 (above Sonnet's 1,600 x $2.50), and 1,000 batch items at the
    # generator's (Sonnet) 1,600 x $1.25: (25,000 + 2,000,000) / 1e6 = 2.025, rounded up to the cent
    assert V19.overrun_buffer_bound(thousand, {**floor, SONNET: 1_600}) == Decimal("2.03")
    # an unconfigured model (Fable) does not count
    fable: dict[str, int] = {**floor, "claude-fable-5-1": 50_000}
    assert V19.overrun_buffer_bound(thousand, fable) == Decimal("1.28")
    # V19's constants at 1,000 batch items: (5 x 2,640 x $5.00 + 1,000 x 2,640 x $1.25) / 1e6 = 3.366, above
    # the $2.00 default; the user's decision (spec rev 3.4, plan Deviation 34) made the default 500 items
    measured: dict[str, int] = dict.fromkeys(MODELS, 2_640)
    assert V19.overrun_buffer_bound(thousand, measured) == Decimal("3.37")
    assert V19.overrun_buffer_bound(_with_batch_items(thousand, 500), measured) == Decimal("1.72")  # 1.716
    # The seeded settings with the constants `bounds.py` holds are guarded by Task 15's
    # `test_seed_defaults_validate` (tests/unit/test_settings_model.py), the settings validation's own check.


def test_the_rewrite_replaces_only_the_generated_block_of_bounds_py() -> None:
    source = (REPO / "backend/src/jobhunter/llm/bounds.py").read_text("utf-8")
    constants = {OPUS: 1_234, SONNET: 1_000, "claude-haiku-5-5": 1_050, "claude-fable-5-1": 1_000}
    rewritten = V19.rewrite_bounds(source, constants, native_pdf_enabled=True)
    assert '"claude-opus-5-5": 1_234,' in rewritten
    assert '"claude-haiku-5-5": 1_050,' in rewritten
    assert "NATIVE_PDF_ENABLED: Final[bool] = True" in rewritten
    head, _, _ = source.partition(V19.BLOCK_START)
    _, _, tail = source.partition(V19.BLOCK_END)
    assert rewritten.startswith(head) and rewritten.endswith(tail)  # nothing outside the block changes
    assert V19.rewrite_bounds(rewritten, constants, native_pdf_enabled=True) == rewritten  # idempotent
    assigned = {
        node.target.id: node.value
        for node in ast.parse(rewritten).body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None
    }
    overhead = assigned["PROMPT_OVERHEAD_TOKENS"]
    assert isinstance(overhead, ast.Call)  # MappingProxyType({...})
    assert ast.literal_eval(overhead.args[0]) == constants
    assert ast.literal_eval(assigned["NATIVE_PDF_ENABLED"]) is True
    with pytest.raises(ValueError, match="floor"):
        V19.rewrite_bounds(source, {**constants, OPUS: 999}, native_pdf_enabled=False)
