from datetime import UTC, datetime, timedelta

import httpx
import pytest
from jobhunter.sources.base import Capabilities, RateLimit, SourceAdapter
from jobhunter.sources.jsearch import CURSOR_PATH, CURSOR_REUSE_TTL, ITEMS_PATH, PAGE2_MIN_NEW, JSearchAdapter
from jobhunter.types import get_object

from tests.support.sources import make_cell_plan

T = datetime(2026, 10, 16, 9, 47, tzinfo=UTC)


def adapter(instance_id: int = 1) -> JSearchAdapter:
    return JSearchAdapter(httpx.AsyncClient(), api_key=b"k", instance_id=instance_id, clock=lambda: T)


def test_a_city_cell_asks_for_the_query_in_the_city() -> None:
    spec = adapter().plan(make_cell_plan())
    assert dict(spec.params) == {
        "query": "python developer in Berlin, Germany",
        "country": "de",
        "num_pages": "1",
        "date_posted": "week",
    }


def test_a_remote_de_cell_asks_for_remote_germany_without_a_city() -> None:
    spec = adapter().plan(make_cell_plan(location_key="remote-de", location_label=None))
    assert spec.params["query"] == "python developer remote Germany"
    assert "Berlin" not in str(dict(spec.params))


@pytest.mark.parametrize("bucket", ["today", "3days", "week", "month"])
def test_date_posted_is_the_cells_bucket(bucket: str) -> None:
    assert adapter().plan(make_cell_plan(bucket=bucket)).params["date_posted"] == bucket


def test_the_first_page_carries_the_cells_identity_window_and_class() -> None:
    cell = make_cell_plan(cell_id=42, request_class="probe", instance_id=3)
    spec = adapter(instance_id=3).plan(cell)
    assert (spec.source_instance_id, spec.cell_id, spec.operation, spec.page_no) == (3, 42, "list", 1)
    assert (spec.cursor_in, spec.detail_native_id, spec.parent_fetch_request_id) == (None, None, None)
    assert spec.request_class == "probe" and spec.window == cell.window


def test_a_spec_holds_neither_a_cursor_nor_a_page_nor_a_language_nor_a_credential() -> None:
    spec = adapter().plan(make_cell_plan())
    assert set(spec.params) == {"query", "country", "num_pages", "date_posted"}
    assert "cursor" not in get_object(spec.to_json(), "params")


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"bucket": None}, "date_posted"),
        ({"query_text": None}, "query"),
        ({"query_text": ""}, "query"),
        ({"location_label": None}, "city"),
        ({"source_id": "serpapi"}, "jsearch"),
        ({"instance_id": 2}, "instance"),
    ],
)
def test_a_cell_that_cannot_be_asked_for_is_refused(changes: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        adapter().plan(make_cell_plan(**changes))


def test_next_page_continues_the_traversal_from_the_cursor_as_a_normal_request() -> None:
    a = adapter()
    first = a.plan(make_cell_plan(request_class="probe"))
    second = a.next_page(first, "CURSOR-1")
    assert second is not None
    assert (second.page_no, second.cursor_in, second.request_class) == (2, "CURSOR-1", "normal")
    assert second.params == first.params and second.window == first.window
    assert (second.cell_id, second.source_instance_id, second.operation) == (7, 1, "list")
    third = a.next_page(second, "CURSOR-2")
    assert third is not None and (third.page_no, third.cursor_in) == (3, "CURSOR-2")
    assert "cursor" not in get_object(second.to_json(), "params")


@pytest.mark.parametrize("cursor", [None, ""])
def test_next_page_without_a_cursor_is_the_end(cursor: str | None) -> None:
    a = adapter()
    assert a.next_page(a.plan(make_cell_plan()), cursor) is None


def test_capabilities_and_constants_are_the_contract() -> None:
    assert adapter().capabilities() == Capabilities(
        "jsearch", "paid", 1, "cursor", 10, "day", "full", False, 1, [RateLimit("jsearch", 1.0, 1)]
    )
    assert (ITEMS_PATH, CURSOR_PATH) == (("data", "jobs"), ("data", "cursor"))  # V9
    assert PAGE2_MIN_NEW == 7
    assert CURSOR_REUSE_TTL == timedelta(
        hours=2
    )  # the longest reuse V9 observed so far; V10 phase 3 may raise it


def test_the_adapter_satisfies_the_source_adapter_protocol() -> None:
    conforming: SourceAdapter = (
        adapter()
    )  # checked by mypy: plan(cell) -> RequestSpec, fetch(spec) -> HttpOutcome
    assert conforming.capabilities().source_id == "jsearch"
