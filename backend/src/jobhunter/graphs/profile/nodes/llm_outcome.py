"""What both LLM nodes of the profile graphs share (spec §9.1, §10.4, §10.5; plan Task 13, Deviation 26): the
receipts walk and the mapping of the gateway's errors. Task 20's `propose_queries` node reuses both, with the
one addition it needs, a node's own check of a parsed output (`usable`, below).

**A step** is what a node does next for its operation: `Use` a completed call's output (no request), make a
`Call` (logical call 1, or 2 for the one structural repair), `Fail` the domain row with a code, or `Requeue`
the operation for a later attempt.

**The receipts walk** (`walk`), over `spend_ledger.receipts` (the newest `completed` call per
`logical_call_no` of the operation), read under the owner lock before anything is paid again:
- the repair (logical call 2) outranks the first call, since it exists only after call 1 was invalid;
- a call with its `output_json` is used without a request;
- `invalid_output` (billed, structurally invalid) on call 1 makes the repair due; on call 2 it is
  `Fail('invalid_output')`. So the one repair is never repeated or skipped across a reclaim;
- `refused`, `model_mismatch`, `max_tokens` and `unexpected_stop_reason` are terminal: `Fail(<that code>)`;
- `max_tokens_first_stage` is neither terminal nor usable (the gateway goes on to the enlarged stage after
  it, and a crash came first): the same logical call is made again;
- no receipt: logical call 1.
A completed call with neither an output nor a known code breaks the gateway's contract (Task 5: every
completed call without an output carries its code): `InvariantViolation`.

**The error mapping** (`after_error`) of the gateway's exceptions, for the logical call that raised:
`InvalidOutput("structural")` is repaired once (`Call(2)`), a second one is `Fail('invalid_output')`;
`InvalidOutput("unexpected_stop_reason")`, `Refused`, `BudgetExhausted`, `InputUnbounded` (every reason,
`unencodable` included), `LLMRequestError(s)`, `ModelMismatch` and `MaxTokensExhausted` fail with
`unexpected_stop_reason`, `refused`, `budget_exhausted`, `input_unbounded`, `llm_request_<s>`,
`model_mismatch` and `max_tokens`; `LLMTransient` and `DeadlineExhausted` requeue the operation
(`Requeue('llm_transient')`, `REQUEUE_AFTER` from now), whose attempt limit and failure hook then end the row.

**An invalid output** (`after_invalid`), whatever found it, is the one repair after call 1 (`Call(2)`) and
`Fail('invalid_output')` after the repair: the walk's `invalid_output` receipt, the gateway's
`InvalidOutput("structural")` and a node's own refusal of a parsed output all go through it.

`drive(step, call, usable=None)` makes the calls a step asks for, with `call(logical_call_no)` (the node's
gateway call, returning the call's id), and returns the step that needs no further call. With `usable` (Task
20: the semantic check of a query set, §11.12), every `Use` it reaches, a receipt's or a new call's, is
checked first: one it refuses is an invalid output (`after_invalid`), so a refused call 1 makes the repair due
and a refused repair fails. It makes at most two calls: call 2 only after call 1 was invalid, and nothing
after call 2. Pure: no I/O but the node's own `call` and `usable`, no clock and no logging.
"""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Final

from jobhunter.errors import InvariantViolation
from jobhunter.llm.errors import (
    BudgetExhausted,
    DeadlineExhausted,
    InputUnbounded,
    InvalidOutput,
    LLMError,
    LLMRequestError,
    LLMTransient,
    MaxTokensExhausted,
    ModelMismatch,
    Refused,
)
from jobhunter.llm.gateway import (
    INVALID_OUTPUT,
    MAX_TOKENS,
    MAX_TOKENS_FIRST_STAGE,
    MODEL_MISMATCH,
    REFUSED,
    UNEXPECTED_STOP_REASON,
)
from jobhunter.llm.spend_ledger import Receipt

FIRST_CALL: Final = 1
REPAIR_CALL: Final = 2
# The codes of a completed call that end the domain row with that code and no further request (Task 5).
TERMINAL_CODES: Final = frozenset({REFUSED, MODEL_MISMATCH, MAX_TOKENS, UNEXPECTED_STOP_REASON})
BUDGET_EXHAUSTED: Final = "budget_exhausted"
INPUT_UNBOUNDED: Final = "input_unbounded"
LLM_TRANSIENT: Final = "llm_transient"
# How long a requeued operation waits after a transient failure (§10.4: the gateway's own retries are spent).
REQUEUE_AFTER: Final = timedelta(minutes=5)
_STRUCTURAL: Final = "structural"


@dataclass(frozen=True)
class Use:
    """Use the output of `logical_call_no`'s completed call `llm_call_id`; no request."""

    logical_call_no: int
    llm_call_id: int


@dataclass(frozen=True)
class Call:
    """Make logical call `logical_call_no` (1, or 2 for the repair)."""

    logical_call_no: int


@dataclass(frozen=True)
class Fail:
    """End the domain row `failed(code)` and the operation `failed(code)`, with no further request."""

    code: str


@dataclass(frozen=True)
class Requeue:
    """Give the operation back for a later attempt with `error_code=code` (`REQUEUE_AFTER` from now)."""

    code: str


type Step = Use | Call | Fail | Requeue


def walk(receipts: Mapping[int, Receipt]) -> Use | Call | Fail:
    """The step the receipts of an operation's calls give (module docstring)."""
    for logical_call_no in (REPAIR_CALL, FIRST_CALL):
        receipt = receipts.get(logical_call_no)
        if receipt is None:
            continue
        if receipt.output_json is not None:
            return Use(logical_call_no, receipt.llm_call_id)
        code = receipt.error_code
        if code == MAX_TOKENS_FIRST_STAGE:
            return Call(logical_call_no)
        if code == INVALID_OUTPUT:
            return after_invalid(logical_call_no)
        if code in TERMINAL_CODES:
            return Fail(code)
        raise InvariantViolation(
            f"llm_calls {receipt.llm_call_id} completed without an output and with code {code!r}"
        )
    return Call(FIRST_CALL)


def after_invalid(logical_call_no: int) -> Call | Fail:
    """The step after logical call `logical_call_no` gave an invalid output (module docstring)."""
    return Call(REPAIR_CALL) if logical_call_no == FIRST_CALL else Fail(INVALID_OUTPUT)


def after_error(error: LLMError, logical_call_no: int) -> Call | Fail | Requeue:
    """The step after logical call `logical_call_no` raised `error` (module docstring)."""
    if isinstance(error, InvalidOutput):
        if error.code == _STRUCTURAL:
            return after_invalid(logical_call_no)
        if error.code == UNEXPECTED_STOP_REASON:
            return Fail(UNEXPECTED_STOP_REASON)
        raise ValueError(f"an unknown InvalidOutput code {error.code!r}")
    if isinstance(error, LLMTransient | DeadlineExhausted):
        return Requeue(LLM_TRANSIENT)
    if isinstance(error, LLMRequestError):
        return Fail(f"llm_request_{error.status}")
    for kind, code in _FAILURES:
        if isinstance(error, kind):
            return Fail(code)
    raise TypeError(f"no outcome for the LLM error {type(error).__name__}")


_FAILURES: Final[tuple[tuple[type[LLMError], str], ...]] = (
    (Refused, REFUSED),
    (BudgetExhausted, BUDGET_EXHAUSTED),
    (InputUnbounded, INPUT_UNBOUNDED),
    (ModelMismatch, MODEL_MISMATCH),
    (MaxTokensExhausted, MAX_TOKENS),
)


async def drive(
    step: Step, call: Callable[[int], Awaitable[int]], *, usable: Callable[[Use], bool] | None = None
) -> Use | Fail | Requeue:
    """Make the calls `step` asks for, checking every `Use` with `usable` when given (module docstring); an
    error other than an `LLMError` propagates."""
    while True:
        if isinstance(step, Call):
            logical_call_no = step.logical_call_no
            try:
                llm_call_id = await call(logical_call_no)
            except LLMError as error:
                step = after_error(error, logical_call_no)
                continue
            step = Use(logical_call_no, llm_call_id)
        if isinstance(step, Use) and usable is not None and not usable(step):
            step = after_invalid(step.logical_call_no)
            continue
        return step
