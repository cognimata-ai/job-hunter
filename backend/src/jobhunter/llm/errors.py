"""The LLM layer's domain errors (spec §6.9.1, §9.4.3, §10.4, §10.5, §17.1).

`BudgetExhausted` is the spend ledger's (a reservation the monthly cap refuses), and so are
`BulkAllocationExceeded` (a batch reservation past its bulk operation's allocation, M3 Task 16) and
`ClaimLost` (a score attempt's claim no longer holds, M3); the others are the gateway's outcomes of a call
(Task 5), `LLMNotConfigured` that of a gateway without a key (Task 23). Like every `DomainError` they carry
codes, statuses, model ids and amounts only, never a prompt, a response or any CV or profile text, so their
messages are safe to log and to put into a checkpoint.
"""

from decimal import Decimal
from typing import Final

from jobhunter.errors import DomainError, FencedOut
from jobhunter.llm.budget import PurposeClass

# The owner `ClaimLost` names: the score target whose claim predicate failed (§3.3.3).
SCORE_TARGET_OWNER: Final = "score_target"


class LLMError(DomainError):
    """Base of the outcomes of an LLM reservation or call."""


class BudgetExhausted(LLMError):
    """The §6.9.1 hard-cap check refused a reservation; `headroom_usd` is what its class could still reserve
    (0 when nothing)."""

    def __init__(self, purpose_class: PurposeClass, headroom_usd: Decimal) -> None:
        super().__init__(f"LLM budget exhausted for {purpose_class} calls (headroom ${headroom_usd})")
        self.purpose_class: PurposeClass = purpose_class
        self.headroom_usd = headroom_usd


class BulkAllocationExceeded(LLMError):
    """A batch reservation (`spend_ledger.reserve_batch`) would take its bulk operation past its allocation
    (§6.9.3 L1189: `spent_usd + followup_held_usd + Σ worst cases + n x hold > budget_usd`), or the operation
    is no longer `active`. Checked under the month and bulk row locks, so nothing is written; after the
    preparation's own sizing under the same locks it never fires (a bug when it does: plan Task 16)."""

    def __init__(self, bulk_operation_id: int, shortfall_usd: Decimal) -> None:
        super().__init__(
            f"batch reservation exceeds bulk operation {bulk_operation_id}'s allocation by ${shortfall_usd}"
        )
        self.bulk_operation_id = bulk_operation_id
        self.shortfall_usd = shortfall_usd


class ClaimLost(FencedOut):
    """§17.1 `claim_lost`: the score target's claim predicate (`claim_id`, `attempt_no`, `state =
    'in_progress'`) failed under the owner lock of a reservation or a dispatch marker (§9.4.3), so nothing is
    reserved or sent and the attempt ends `claim_lost`. A `FencedOut` of one owner, never the worker's own
    (`worker_superseded` is false)."""

    def __init__(self) -> None:
        super().__init__(SCORE_TARGET_OWNER)


class LLMNotConfigured(LLMError):
    """The gateway can make no call: the worker has neither the `anthropic_key` secret nor the fake LLM (plan
    Task 23). Raised by every call before anything is reserved, counted or sent; not retryable until a key is
    mounted and the worker restarted."""

    def __init__(self) -> None:
        super().__init__("the LLM gateway is not configured: the worker has no anthropic_key secret")


class LLMTransient(LLMError):
    """The retryable failures (§10.4: 408, 409, 429, 5xx, 529, timeouts, connection errors) outlasted the
    tries a logical call may make, or the deadline left no room for the next; `code` is the last try's
    (`http_<status>`, `connect_failed`, `stream_interrupted`)."""

    def __init__(self, code: str) -> None:
        super().__init__(f"LLM call failed transiently: {code}")
        self.code = code


class LLMRequestError(LLMError):
    """A non-retryable HTTP status from the provider (§10.4)."""

    def __init__(self, status: int) -> None:
        super().__init__(f"LLM request failed with HTTP {status}")
        self.status = status


class DeadlineExhausted(LLMError):
    """Too little of the node deadline is left for another try (§10.4)."""


class MaxTokensExhausted(LLMError):
    """The enlarged `max_tokens` stage also stopped at `max_tokens` (§10.5)."""


class InputUnbounded(LLMError):
    """The input of a call could not be bounded, so nothing was reserved or sent (§10.7): `reason` is
    `count_failed` (the native-PDF `count_tokens` failed), `context_window` (it counted more than the model's
    context window) or `unencodable` (a text component cannot be encoded as UTF-8, as with a lone surrogate,
    so it has no byte bound and could not be sent either). Not retryable."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"LLM input could not be bounded: {reason}")
        self.reason = reason


class InvalidOutput(LLMError):
    """The response failed structural validation, or stopped for an unexpected reason (§10.5); `code` names
    which (`structural`, `unexpected_stop_reason`)."""

    def __init__(self, code: str) -> None:
        super().__init__(f"invalid LLM output: {code}")
        self.code = code


class Refused(LLMError):
    """The model refused (`stop_reason='refusal'`, §10.5); `category` is the provider's refusal category."""

    def __init__(self, category: str) -> None:
        super().__init__(f"LLM refused: {category}")
        self.category = category


class ModelMismatch(LLMError):
    """The provider served another model than the one requested (§10.5)."""

    def __init__(self, requested: str, served: str) -> None:
        super().__init__(f"LLM model mismatch: requested {requested}, served {served}")
        self.requested = requested
        self.served = served
