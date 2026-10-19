"""Test helpers over the scrubbed source fixtures (spec §18.4; Task 6); Tasks 11 to 15 add fixtures here.

`load_fixture` reads a case the way the fixture transport serves it (`fixtures.load_case`); `fixture_page`
turns it into the `RawPage` an adapter parses; `mock_fixture` serves it on a respx route; `served_requests`
reads the fixture transport's request log (Task 5), each `case` reduced to its name (`search_v2_page1`);
`make_cell_plan` builds the `CellPlan` an adapter's `plan` takes (Task 9: a JSearch cell for Berlin, the
`week` bucket), with any field changed.
"""

import json
import re
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import httpx
import respx
from jobhunter.sources.base import CellPlan, RawPage, RequestSpec
from jobhunter.sources.fixtures import load_case

FIXTURES: Final = Path("tests/contract/fixtures")
_NOW: Final = datetime(2026, 10, 16, 9, 47, tzinfo=UTC)


def load_fixture(source: str, case: str) -> tuple[int, dict[str, str], bytes]:
    """The status, the headers (lower-case names, `content-type` always set) and the body of a case."""
    return load_case(FIXTURES, f"{source}/{case}")


def fixture_page(source: str, case: str, spec: RequestSpec, *, fetched_at: datetime) -> RawPage:
    """The recorded response of `<source>/<case>` as the page of `spec`, arrived at `fetched_at`."""
    status, headers, body = load_fixture(source, case)
    return RawPage(spec, status, headers, body, fetched_at)


def mock_fixture(
    respx_mock: respx.MockRouter, method: str, url_pattern: str | re.Pattern[str], source: str, case: str
) -> respx.Route:
    """A respx route answering `method url_pattern` with the case (a URL without query matches any query)."""
    status, headers, body = load_fixture(source, case)
    route = respx_mock.route(method=method, url=url_pattern)
    route.mock(return_value=httpx.Response(status, headers=headers, content=body))
    return route


def served_requests(log_path: Path) -> list[dict[str, str]]:
    """The lines of the fixture transport's request log, every value a string, `case` the case name only
    (`""` for a request that no route matched); an absent log is no request."""
    if not log_path.exists():
        return []
    served: list[dict[str, str]] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        entry = json.loads(line)
        case = entry.get("case")
        served.append(
            {
                **{key: "" if value is None else str(value) for key, value in entry.items()},
                "case": "" if case is None else str(case).rsplit("/", 1)[-1],
            }
        )
    return served


def make_cell_plan(**changes: Any) -> CellPlan:
    """A JSearch cell for `python developer` in Berlin, bucket `week`, with `changes` applied."""
    base = CellPlan(
        cell_id=7,
        source_id="jsearch",
        instance_id=1,
        query_text="python developer",
        query_lang="en",
        location_key="berlin",
        location_label="Berlin",
        radius_km=25,
        phase="primary",
        request_class="normal",
        bucket="week",
        window=(datetime(2026, 10, 9, 9, 47, tzinfo=UTC), _NOW),
        max_days_old=None,
    )
    return replace(base, **changes)
