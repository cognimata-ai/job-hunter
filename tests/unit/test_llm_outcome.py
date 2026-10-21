"""The two pieces both LLM nodes share (plan Task 13; Task 20 reuses them): the receipts walk over the
completed calls of an operation (§9.1, Deviation 26), and the mapping of the gateway's errors to a failure
code, the one structural repair or a requeue. Pure: no database."""

from decimal import Decimal
from typing import Any

import pytest
from jobhunter.errors import InvariantViolation
from jobhunter.graphs.profile.nodes import llm_outcome
from jobhunter.graphs.profile.nodes.llm_outcome import Call, Fail, Requeue, Use, after_error, drive, walk
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
from jobhunter.llm.spend_ledger import Receipt

OUTPUT: Any = {"summary": "synthetic"}


def ok(call_id: int) -> Receipt:
    return Receipt(llm_call_id=call_id, output_json=OUTPUT, error_code=None)


def failed(call_id: int, code: str) -> Receipt:
    return Receipt(llm_call_id=call_id, output_json=None, error_code=code)


# --- the receipts walk -------------------------------------------------------------------------------------


def test_no_receipt_makes_the_first_call() -> None:
    assert walk({}) == Call(1)


def test_a_first_call_with_its_output_is_used_without_a_request() -> None:
    assert walk({1: ok(11)}) == Use(1, 11)


def test_a_structurally_invalid_first_call_makes_the_repair_due() -> None:
    assert walk({1: failed(11, "invalid_output")}) == Call(2)


def test_the_repair_with_its_output_is_used() -> None:
    assert walk({1: failed(11, "invalid_output"), 2: ok(12)}) == Use(2, 12)


def test_an_invalid_repair_ends_invalid_output_and_is_never_repeated() -> None:
    assert walk({1: failed(11, "invalid_output"), 2: failed(12, "invalid_output")}) == Fail("invalid_output")


@pytest.mark.parametrize("code", ["refused", "model_mismatch", "max_tokens", "unexpected_stop_reason"])
@pytest.mark.parametrize("logical_call_no", [1, 2])
def test_a_terminal_code_ends_with_that_code_and_no_request(code: str, logical_call_no: int) -> None:
    receipts = {1: failed(11, "invalid_output")} if logical_call_no == 2 else {}
    receipts[logical_call_no] = failed(10 + logical_call_no, code)
    assert walk(receipts) == Fail(code)


@pytest.mark.parametrize("logical_call_no", [1, 2])
def test_a_settled_first_stage_max_tokens_try_is_neither_terminal_nor_usable(logical_call_no: int) -> None:
    """Carried ruling (Task 5): the gateway enlarges after `max_tokens_first_stage`; a crash before the
    enlarged stage settled leaves that try the newest completed one, and the call is made again under the
    same `logical_call_no`."""
    receipts = {1: failed(11, "invalid_output")} if logical_call_no == 2 else {}
    receipts[logical_call_no] = failed(10 + logical_call_no, "max_tokens_first_stage")
    assert walk(receipts) == Call(logical_call_no)


def test_terminal_codes_are_the_four_of_the_gateway() -> None:
    assert llm_outcome.TERMINAL_CODES == {"refused", "model_mismatch", "max_tokens", "unexpected_stop_reason"}


@pytest.mark.parametrize("code", [None, "http_529", "something_new"])
def test_a_completed_call_without_output_or_a_known_code_is_an_invariant_violation(code: str | None) -> None:
    with pytest.raises(InvariantViolation):
        walk({1: failed(11, code)})  # type: ignore[arg-type]


# --- the error mapping -------------------------------------------------------------------------------------


def test_a_structural_failure_is_repaired_once() -> None:
    assert after_error(InvalidOutput("structural"), 1) == Call(2)
    assert after_error(InvalidOutput("structural"), 2) == Fail("invalid_output")


@pytest.mark.parametrize("logical_call_no", [1, 2])
def test_an_unexpected_stop_reason_is_never_repaired(logical_call_no: int) -> None:
    assert after_error(InvalidOutput("unexpected_stop_reason"), logical_call_no) == Fail(
        "unexpected_stop_reason"
    )


def test_an_unknown_invalid_output_code_is_a_bug() -> None:
    with pytest.raises(ValueError, match="unknown"):
        after_error(InvalidOutput("brand_new"), 1)


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (Refused("general_harms"), "refused"),
        (BudgetExhausted("nonscoring", Decimal(0)), "budget_exhausted"),
        (InputUnbounded("count_failed"), "input_unbounded"),
        (InputUnbounded("context_window"), "input_unbounded"),
        (InputUnbounded("unencodable"), "input_unbounded"),
        (LLMRequestError(400), "llm_request_400"),
        (LLMRequestError(401), "llm_request_401"),
        (ModelMismatch("claude-opus-5-5", "claude-sonnet-5-5"), "model_mismatch"),
        (MaxTokensExhausted("enlarged stage too"), "max_tokens"),
    ],
    ids=lambda v: type(v).__name__ if isinstance(v, LLMError) else v,
)
@pytest.mark.parametrize("logical_call_no", [1, 2])
def test_the_other_errors_end_with_their_code(error: LLMError, code: str, logical_call_no: int) -> None:
    assert after_error(error, logical_call_no) == Fail(code)


@pytest.mark.parametrize("error", [LLMTransient("http_529"), DeadlineExhausted("no room")], ids=type)
def test_transient_errors_requeue_the_operation(error: LLMError) -> None:
    assert after_error(error, 1) == Requeue("llm_transient")
    assert llm_outcome.REQUEUE_AFTER.total_seconds() == 300


# --- the calls a step asks for ---------------------------------------------------------------------------


class Calls:
    """A node's call function: each logical call answers the next of `answers` (an llm_call id or an
    error)."""

    def __init__(self, *answers: int | LLMError) -> None:
        self.answers = list(answers)
        self.made: list[int] = []

    async def __call__(self, logical_call_no: int) -> int:
        self.made.append(logical_call_no)
        answer = self.answers.pop(0)
        if isinstance(answer, LLMError):
            raise answer
        return answer


async def test_drive_returns_a_used_or_failed_step_as_it_is() -> None:
    calls = Calls()
    assert await drive(Use(1, 11), calls) == Use(1, 11)
    assert await drive(Fail("refused"), calls) == Fail("refused")
    assert calls.made == []


async def test_drive_makes_the_call_and_uses_its_output() -> None:
    calls = Calls(21)
    assert await drive(Call(1), calls) == Use(1, 21)
    assert calls.made == [1]


async def test_drive_repairs_a_structural_failure_once() -> None:
    calls = Calls(InvalidOutput("structural"), 22)
    assert await drive(Call(1), calls) == Use(2, 22)
    assert calls.made == [1, 2]


async def test_drive_stops_after_the_repair_failed_too() -> None:
    calls = Calls(InvalidOutput("structural"), InvalidOutput("structural"), 99)
    assert await drive(Call(1), calls) == Fail("invalid_output")
    assert calls.made == [1, 2]


async def test_drive_makes_only_the_repair_when_it_is_due() -> None:
    calls = Calls(InvalidOutput("structural"))
    assert await drive(Call(2), calls) == Fail("invalid_output")
    assert calls.made == [2]


async def test_drive_maps_a_transient_error_to_a_requeue() -> None:
    assert await drive(Call(1), Calls(LLMTransient("http_529"))) == Requeue("llm_transient")


async def test_drive_lets_a_non_llm_error_through() -> None:
    class Boom(Exception):
        pass

    async def call(logical_call_no: int) -> int:
        raise Boom

    with pytest.raises(Boom):
        await drive(Call(1), call)
