from datetime import UTC, datetime

import httpx
import pytest
from jobhunter.sources.base import Capabilities, RateLimit, SourceAdapter
from jobhunter.sources.serpapi import SERPAPI_HL, SerpApiAdapter
from jobhunter.types import get_object

from tests.support.sources import make_cell_plan

T = datetime(2026, 10, 16, 9, 47, tzinfo=UTC)


def adapter(instance_id: int = 2) -> SerpApiAdapter:
    return SerpApiAdapter(httpx.AsyncClient(), api_key=b"k", instance_id=instance_id, clock=lambda: T)


def test_a_city_cell_asks_google_jobs_for_the_query_and_the_city() -> None:
    spec = adapter().plan(make_cell_plan(source_id="serpapi", instance_id=2, bucket=None))
    assert dict(spec.params) == {
        "q": "python developer Berlin",
        "gl": "de",
        "hl": SERPAPI_HL,
        "engine": "google_jobs",
    }


def test_a_remote_de_cell_asks_for_homeoffice_deutschland() -> None:
    spec = adapter().plan(
        make_cell_plan(source_id="serpapi", instance_id=2, location_key="remote-de", location_label=None)
    )
    assert spec.params["q"] == "python developer Homeoffice Deutschland"


def test_location_and_uule_are_never_sent_and_the_key_is_not_in_the_spec() -> None:
    spec = adapter().plan(make_cell_plan(source_id="serpapi", instance_id=2, radius_km=50))
    assert not {"location", "uule", "api_key", "next_page_token", "date_posted"} & set(spec.params)
    assert "cursor" not in get_object(spec.to_json(), "params")


def test_the_first_page_carries_the_cells_identity_window_and_class() -> None:
    planned = make_cell_plan(source_id="serpapi", instance_id=2, cell_id=9, request_class="retry")
    spec = adapter().plan(planned)
    assert (spec.source_instance_id, spec.cell_id, spec.operation, spec.page_no) == (2, 9, "list", 1)
    assert (spec.cursor_in, spec.detail_native_id, spec.parent_fetch_request_id) == (None, None, None)
    assert spec.request_class == "retry" and spec.window == planned.window


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"query_text": None}, "query"),
        ({"location_label": None}, "city"),
        ({"source_id": "jsearch"}, "serpapi"),
        ({"instance_id": 5}, "instance"),
    ],
)
def test_a_cell_that_cannot_be_asked_for_is_refused(changes: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        adapter().plan(make_cell_plan(**{"source_id": "serpapi", "instance_id": 2, **changes}))


def test_next_page_continues_from_the_page_token_as_a_normal_request() -> None:
    a = adapter()
    first = a.plan(make_cell_plan(source_id="serpapi", instance_id=2, request_class="probe"))
    second = a.next_page(first, "TOKEN-1")
    assert second is not None
    assert (second.page_no, second.cursor_in, second.request_class) == (2, "TOKEN-1", "normal")
    assert second.params == first.params and "next_page_token" not in second.params
    assert a.next_page(first, None) is None


def test_capabilities_and_the_v3_language() -> None:
    assert adapter().capabilities() == Capabilities(
        "serpapi", "paid", 1, "cursor", 10, "relative", "full", False, 1, [RateLimit("serpapi", 1.0, 1)]
    )
    assert SERPAPI_HL == "en"  # docs/spikes/V3.md, ruling T6-a


def test_the_adapter_satisfies_the_source_adapter_protocol() -> None:
    # checked by mypy: plan(cell) -> RequestSpec, fetch(spec) -> HttpOutcome
    conforming: SourceAdapter = adapter()
    assert conforming.capabilities().source_id == "serpapi"
