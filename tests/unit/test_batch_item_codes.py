"""The call code of a batch item the provider did not run (plan Task 17, Carry T8-b): an errored item's code
carries the provider's nested `error.error.type` after its reason, made storable and bounded, never the
message; every other kind's code is its reason alone."""

from jobhunter.llm.batches import ItemResult
from jobhunter.scoring.batch_items import PROVIDER_TYPE_MAX, call_error_code


def test_an_errored_items_code_carries_its_provider_type() -> None:
    assert call_error_code(ItemResult("c", "errored", error_type="overloaded_error")) == (
        "batch_errored:overloaded_error"
    )
    assert call_error_code(ItemResult("c", "errored", error_type="invalid_request_error")) == (
        "batch_invalid_request:invalid_request_error"
    )


def test_the_provider_type_is_made_storable_and_bounded() -> None:
    code = call_error_code(ItemResult("c", "errored", error_type="odd\x00\ud800" + "x" * 500))
    assert code == "batch_errored:odd??" + "x" * (PROVIDER_TYPE_MAX - 5)


def test_a_code_without_a_provider_type_is_its_reason_alone() -> None:
    assert call_error_code(ItemResult("c", "errored")) == "batch_errored"
    assert call_error_code(ItemResult("c", "canceled")) == "batch_canceled"
    assert call_error_code(ItemResult("c", "expired")) == "batch_expired"
