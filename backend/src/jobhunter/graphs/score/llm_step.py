"""The score mapping of a failed step (spec §9.4.2 L3551-3553, §7.16.3, §17.1; plan Task 9, A2 §2.6,
Deviation 17), the M2 `llm_outcome` pattern for the score lane: what an exception that ends a score step
means for the attempt and its target.

`error_code_of(exc)` is the code a node puts into `TargetState.error_code` (an enum, never text) when it
routes to `fail`; `outcome_for(code)` is what `fail` (Task 10) makes of it, a `StepOutcome`: the code, the
attempt's `outcome`, the target's state (`None`: the target is left as it is), the reason (the target's
`state_reason` and the attempt's `outcome_detail`) and whether the target backs off; `outcome_of(exc)` is
both at once, with a refusal's category as its reason.

The exceptions, each with its `error_code`, the attempt's outcome and the target's state:
- `Refused(category)`: `refused`, `refused`, `refused(category or 'unspecified')`;
- `MaxTokensExhausted`: `max_tokens`, `failed_max_tokens`, `failed_max_tokens`;
- `InvalidOutput('structural')`, after the repair: `invalid_output`, `failed_invalid_output`,
  `failed_invalid_output`;
- `InvalidOutput('unexpected_stop_reason')`: `unexpected_stop_reason`, `failed_invalid_output`,
  `failed_invalid_output('unexpected_stop_reason')`;
- `LLMRequestError(status)` (400, 401, 403, 404, 413, 422, ...): `llm_request_<status>`, `failed_request`,
  `failed_request('<status>')`;
- `ModelMismatch`: `model_mismatch`, `failed_request`, `failed_request('model_mismatch')`;
- `InputUnbounded`: `input_unbounded`, `failed_request`, `failed_request('input_unbounded')`;
- `LLMTransient`, `DeadlineExhausted` and a `TimeoutPolicy` cancellation (`NodeTimeoutError`):
  `llm_transient`, `transient_error`, `pending` with backoff;
- `LLMNotConfigured`: `llm_not_configured`, `transient_error`, `pending` with backoff;
- `PromptVersionUnscorable` (a `generator_v1` version, Task 4): `prompt_version_unscorable`,
  `transient_error`, `pending` with backoff;
- `BudgetExhausted`: `budget_exhausted`, `budget_exhausted`, `budget_deferred('budget_exhausted')`;
- `ClaimLost` and any other `FencedOut`: `claim_lost`, `claim_lost`, the target untouched (`None`).

A transient target backs off (`pending(not_before)`) while `charged_attempts < max_score_attempts`, and is
`failed_transient` once they are spent: `fail` decides it from the target row (§9.6, L2863). The last three
transient rows make no call (a gateway without a key raises before anything is reserved; an unscorable
version is refused by `load_context` before any request), so they charge nothing. A `FencedOut` of the worker
itself (`worker_superseded`) has no outcome here: the worker stops (Ruling T24-a), so it propagates.

A refusal's category is provider metadata (`spend_ledger.storable_text`), stored on the call
(`llm_calls.refusal_category`); `fail` reads it from there, since the state holds the code alone.
"""

from dataclasses import dataclass
from typing import Final

from langgraph.errors import NodeTimeoutError

from jobhunter.errors import FencedOut
from jobhunter.llm.errors import (
    BudgetExhausted,
    DeadlineExhausted,
    InputUnbounded,
    InvalidOutput,
    LLMNotConfigured,
    LLMRequestError,
    LLMTransient,
    MaxTokensExhausted,
    ModelMismatch,
    Refused,
)
from jobhunter.llm.gateway import INVALID_OUTPUT, MAX_TOKENS, MODEL_MISMATCH, REFUSED, UNEXPECTED_STOP_REASON
from jobhunter.llm.prompts.render import PromptVersionUnscorable
from jobhunter.scoring.routing import CLAIM_LOST

LLM_TRANSIENT: Final = "llm_transient"
LLM_NOT_CONFIGURED: Final = "llm_not_configured"
PROMPT_VERSION_UNSCORABLE: Final = "prompt_version_unscorable"
BUDGET_EXHAUSTED: Final = "budget_exhausted"
INPUT_UNBOUNDED: Final = "input_unbounded"
REQUEST_PREFIX: Final = "llm_request_"
UNSPECIFIED_CATEGORY: Final = "unspecified"
_STRUCTURAL: Final = "structural"


@dataclass(frozen=True)
class StepOutcome:
    """What a failed step means (module docstring): its code, the attempt's outcome, the target's state
    (`None`: untouched), the reason, and whether the target backs off."""

    error_code: str
    attempt_outcome: str
    target_state: str | None
    reason: str | None
    backoff: bool


_FIXED: Final[dict[str, StepOutcome]] = {
    code: StepOutcome(code, attempt, target, reason, backoff)
    for code, attempt, target, reason, backoff in (
        (MAX_TOKENS, "failed_max_tokens", "failed_max_tokens", MAX_TOKENS, False),
        (INVALID_OUTPUT, "failed_invalid_output", "failed_invalid_output", INVALID_OUTPUT, False),
        (
            UNEXPECTED_STOP_REASON,
            "failed_invalid_output",
            "failed_invalid_output",
            UNEXPECTED_STOP_REASON,
            False,
        ),
        (MODEL_MISMATCH, "failed_request", "failed_request", MODEL_MISMATCH, False),
        (INPUT_UNBOUNDED, "failed_request", "failed_request", INPUT_UNBOUNDED, False),
        (LLM_TRANSIENT, "transient_error", "pending", LLM_TRANSIENT, True),
        (LLM_NOT_CONFIGURED, "transient_error", "pending", LLM_NOT_CONFIGURED, True),
        (PROMPT_VERSION_UNSCORABLE, "transient_error", "pending", PROMPT_VERSION_UNSCORABLE, True),
        (BUDGET_EXHAUSTED, "budget_exhausted", "budget_deferred", BUDGET_EXHAUSTED, False),
        (CLAIM_LOST, CLAIM_LOST, None, None, False),
    )
}


def error_code_of(exc: BaseException) -> str:
    """The `error_code` of an exception that ends a score step (module docstring); `TypeError` for one that
    has none (a bug, which the caller lets propagate), and for the worker's own `FencedOut`."""
    if isinstance(exc, FencedOut):
        if exc.worker_superseded:
            raise TypeError("the worker's own fence has no score outcome: the worker stops")
        return CLAIM_LOST
    if isinstance(exc, InvalidOutput):
        if exc.code == _STRUCTURAL:
            return INVALID_OUTPUT
        if exc.code == UNEXPECTED_STOP_REASON:
            return UNEXPECTED_STOP_REASON
        raise TypeError(f"an unknown InvalidOutput code {exc.code!r}")
    if isinstance(exc, LLMRequestError):
        return f"{REQUEST_PREFIX}{exc.status}"
    for kind, code in _CODES:
        if isinstance(exc, kind):
            return code
    raise TypeError(f"no score outcome for {type(exc).__name__}")


_CODES: Final[tuple[tuple[type[BaseException], str], ...]] = (
    (Refused, REFUSED),
    (MaxTokensExhausted, MAX_TOKENS),
    (ModelMismatch, MODEL_MISMATCH),
    (InputUnbounded, INPUT_UNBOUNDED),
    (LLMTransient, LLM_TRANSIENT),
    (DeadlineExhausted, LLM_TRANSIENT),
    (NodeTimeoutError, LLM_TRANSIENT),
    (LLMNotConfigured, LLM_NOT_CONFIGURED),
    (PromptVersionUnscorable, PROMPT_VERSION_UNSCORABLE),
    (BudgetExhausted, BUDGET_EXHAUSTED),
)


def outcome_for(error_code: str, *, refusal_category: str | None = None) -> StepOutcome:
    """The `StepOutcome` of `error_code` (module docstring); a refusal's reason is `refusal_category`, or
    `unspecified` without one. `ValueError` for a code no step gives."""
    if error_code == REFUSED:
        return StepOutcome(REFUSED, REFUSED, REFUSED, refusal_category or UNSPECIFIED_CATEGORY, False)
    if error_code.startswith(REQUEST_PREFIX) and error_code.removeprefix(REQUEST_PREFIX).isdigit():
        status = error_code.removeprefix(REQUEST_PREFIX)
        return StepOutcome(error_code, "failed_request", "failed_request", status, False)
    found = _FIXED.get(error_code)
    if found is None:
        raise ValueError(f"no score step ends with the code {error_code!r}")
    return found


def outcome_of(exc: BaseException) -> StepOutcome:
    """`outcome_for(error_code_of(exc))`, a refusal with its own category (module docstring)."""
    category = exc.category if isinstance(exc, Refused) else None
    return outcome_for(error_code_of(exc), refusal_category=category)
