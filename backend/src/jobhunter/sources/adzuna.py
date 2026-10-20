"""The Adzuna adapter, a quota source used under its personal-use terms (spec §5.3.3).

Request: `GET https://api.adzuna.com/v1/api/jobs/de/search/{page}` with `results_per_page=50` (V18: 100 is
silently capped at 50), `what`, `where`, `distance` (the cell's radius), `max_days_old` (the cell's,
§5.5.3) and `sort_by=date`. A `remote-de` cell asks for `<query> remote` with neither `where` nor
`distance`. The page number is the spec's `page_no`, in the path. The credentials `app_id` and `app_key`
are added to the query when the request is sent: a spec never holds them (`SECRET_PARAMS`), and the request
log removes them.

Envelope: an object with a `results` array and an integer `count` (V18). Adzuna's own error report is an
`Adzuna::API::Response::Exception` object with an `exception` code (the `search_401` fixture); delivered
with a 2xx it is `SourceError("transient", code="provider_error")`, a failed attempt that counts toward the
breaker threshold, not a broken contract (T9-a). Any other body without the two keys is a contract
violation. `peek_native_ids`, `parse` and `continuation` apply the same check, so no path reads an error
body as a page or as the end of the results.

Pagination: `END` when the page holds fewer than 50 results or `page_no * 50 >= count`, otherwise the next
page number. The per-cell cap `PAGE_CAP` is the traversal's (Task 14: a full page at the cap ends it
`unknown`).

Items: `native_id` is `id` (a string in V18's recording), the only key is `adzuna:<id>`. `redirect_url`
carries the app id in its query, so it is stored only as the private outbound link: it is never a key, never
the primary link, and never part of an error detail or a log line. `created` is Adzuna's ingestion time
(precision `ingest`: a re-ingested ad gets a new id) and the item time. The description is plain text cut at
500 characters, always `partial`, through the quality gate. Adzuna marks the cut with a trailing `…` (U+2026),
which is no ad text and is dropped: NFKC would spell it `...`, two characters past the cut. `salary_min` and
`salary_max` are EUR per year, `predicted` when `salary_is_predicted` is `"1"`. `latitude`/`longitude` give
one location at those coordinates, `unique` when a known place lies within 50 km; without usable
coordinates `location.display_name` is parsed as text. `contract_time`/`contract_type` are structured
employment types, `category.tag` the structured industry (`unknown` says nothing). Adzuna states no work
mode.

Settlement (§5.3.3): Adzuna sends no quota headers, so every dispatched request counts as a hit, `billed`,
whatever its status and whether or not a response arrived; only a connect error raised before any byte was
sent is `not_billed`. `observation` is `None`.
"""

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from typing import Final

import httpx

from jobhunter.db.seed import canonical_json
from jobhunter.normalize.keys import AdKey
from jobhunter.sources.base import (
    REMOTE_DE,
    Capabilities,
    CellPlan,
    Continuation,
    ContractViolation,
    DuplicateInPage,
    ItemOutcome,
    NormalizedPosting,
    Observation,
    Parsed,
    RateLimit,
    RawPage,
    Rejected,
    RequestSpec,
    Settlement,
    SourceError,
)
from jobhunter.sources.http import HttpOutcome, missing_credential, send
from jobhunter.sources.parsing.dates import parse_iso
from jobhunter.sources.parsing.description import plain_description
from jobhunter.sources.parsing.employment import adzuna_employment
from jobhunter.sources.parsing.locations import (
    Location,
    LocationKind,
    location_from_coordinates,
    parse_location_text,
)
from jobhunter.sources.parsing.salary import Salary, from_bounds
from jobhunter.types import JSONValue

SOURCE_ID: Final = "adzuna"
SEARCH_URL: Final = "https://api.adzuna.com/v1/api/jobs/de/search"
RESULTS_PER_PAGE: Final = 50
PAGE_CAP: Final = 3  # pages per cell and traversal (§5.3.3)

_ALL_WORK_MODES: Final = frozenset({"remote", "hybrid", "onsite"})
_UNKNOWN_CATEGORY: Final = "unknown"
_CUT_MARK: Final = "\u2026"  # the `…` that ends a truncated description


def _document(page: RawPage) -> dict[str, JSONValue]:
    """The body as the search-results object; anything else raises (see the module docstring)."""
    doc = page.json()
    if not isinstance(doc, dict):
        raise ContractViolation("not_object", status=page.status)
    if "results" not in doc and isinstance(doc.get("exception"), str):
        # The provider's text is not copied into the error: it is neither logged nor stored.
        raise SourceError("transient", code="provider_error", status=page.status)
    return doc


def _results(page: RawPage) -> tuple[list[JSONValue], int]:
    doc = _document(page)
    results, count = doc.get("results"), doc.get("count")
    if not isinstance(results, list):
        raise ContractViolation("results_not_list", status=page.status)
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ContractViolation("count_invalid", status=page.status)
    return results, count


def _native_id(job: JSONValue) -> str | None:
    value = job.get("id") if isinstance(job, dict) else None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    return value if isinstance(value, str) and value else None


def _number(value: JSONValue) -> float | None:
    return None if isinstance(value, bool) or not isinstance(value, int | float) else float(value)


def _salary(job: dict[str, JSONValue]) -> Salary | None:
    flag = job.get("salary_is_predicted")
    predicted = not isinstance(flag, bool) and flag in ("1", 1)
    return from_bounds(
        _number(job.get("salary_min")),
        _number(job.get("salary_max")),
        currency="EUR",
        period="year",
        kind="predicted" if predicted else "stated",
        raw=None,
    )


def _locations(job: dict[str, JSONValue]) -> tuple[list[Location], LocationKind]:
    """One location at the coordinates, else the parsed `location.display_name`, else none."""
    area = job.get("location")
    name = area.get("display_name") if isinstance(area, dict) else None
    label = name if isinstance(name, str) else ""
    lat, lon = _number(job.get("latitude")), _number(job.get("longitude"))
    if lat is not None and lon is not None:
        try:
            return [location_from_coordinates(label, lat, lon, source=SOURCE_ID)], "city"
        except ValueError:  # not a coordinate pair: the label is the evidence left
            pass
    if label.strip():
        places = parse_location_text(label, source=SOURCE_ID)
        return places.locations, places.kind
    return [], "unknown"


def _industry(job: dict[str, JSONValue]) -> dict[str, JSONValue]:
    category = job.get("category")
    tag = category.get("tag") if isinstance(category, dict) else None
    return {"industry": tag} if isinstance(tag, str) and tag and tag != _UNKNOWN_CATEGORY else {}


def _rejected(index: int, native_id: str | None, detail: str) -> Rejected:
    return Rejected(index, native_id, None, "item_contract", detail)


def _parse_item(index: int, job: JSONValue) -> Parsed | Rejected:
    if not isinstance(job, dict):
        return _rejected(index, None, "the item is not an object")
    native_id = _native_id(job)
    if native_id is None:
        return _rejected(index, None, "id is missing or not a string or an integer")
    title, company = job.get("title"), job.get("company")
    if not isinstance(title, str):
        return _rejected(index, native_id, "title is not a string")
    name = company.get("display_name") if isinstance(company, dict) else None
    if not isinstance(name, str):
        return _rejected(index, native_id, "company.display_name is not a string")

    created_raw = job.get("created")
    created = parse_iso(created_raw, precision="ingest") if isinstance(created_raw, str) else None
    locations, kind = _locations(job)
    raw_description = job.get("description")
    if isinstance(raw_description, str):
        raw_description = raw_description.rstrip().removesuffix(_CUT_MARK)
    description, quality = plain_description(raw_description)
    contract_time, contract_type = job.get("contract_time"), job.get("contract_type")
    employment = tuple(
        adzuna_employment(
            contract_time if isinstance(contract_time, str) else None,
            contract_type if isinstance(contract_type, str) else None,
        )
    )
    redirect = job.get("redirect_url")
    posting = NormalizedPosting(
        native_id=native_id,
        title_raw=title,
        company_raw=name,
        locations=locations,
        location_kind=kind,
        work_mode_possible=_ALL_WORK_MODES,
        work_mode_source="none",
        remote_countries=(),
        employment_types=employment,
        employment_source="structured" if employment else "none",
        salary=_salary(job),
        posted_at=None if created is None else created.at,
        posted_precision="unknown" if created is None else created.precision,
        posted_raw=created_raw if isinstance(created_raw, str) else None,
        item_ts=None if created is None else created.at,
        description_text=description,
        description_html_sanitized=None,
        description_completeness="partial",
        description_quality=quality,
        ad_language_structured=None,
        primary_link=None,
        outbound_url_private=redirect if isinstance(redirect, str) and redirect else None,
        publisher=None,
        raw_keys=(AdKey(SOURCE_ID, native_id),),
        requisition_id=None,
        source_structured=_industry(job),
    )
    return Parsed(index, native_id, posting.item_ts, posting)


class AdzunaAdapter:
    """`SourceAdapter` for Adzuna. It holds the credentials as bytes and shows them nowhere."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        app_id: bytes,
        app_key: bytes,
        instance_id: int,
        clock: Callable[[], datetime],
    ) -> None:
        self._client = client
        self._app_id = app_id
        self._app_key = app_key
        self._instance_id = instance_id
        self._clock = clock

    def capabilities(self) -> Capabilities:
        return Capabilities(
            SOURCE_ID,
            "quota",
            1,
            "page",
            RESULTS_PER_PAGE,
            "ingest",
            "partial",
            False,
            1,
            [RateLimit(SOURCE_ID, 1.0, 1)],
        )

    def plan(self, cell: CellPlan) -> RequestSpec:
        """Page 1 of the cell: `what=<query>` in `where=<city>` within `distance=<radius>` (`<query> remote`
        without a place for `remote-de`), at most `max_days_old` days old, newest first."""
        if cell.source_id != SOURCE_ID:
            raise ValueError(f"cell {cell.cell_id} is a {cell.source_id} cell, not an {SOURCE_ID} cell")
        if cell.instance_id != self._instance_id:
            raise ValueError(
                f"cell {cell.cell_id} is planned for instance {cell.instance_id}, not {self._instance_id}"
            )
        if not cell.query_text:
            raise ValueError(f"cell {cell.cell_id} has no query text")
        if cell.max_days_old is None or cell.max_days_old < 1:
            raise ValueError(f"cell {cell.cell_id} has no max_days_old of at least 1 day")
        params = {
            "results_per_page": str(RESULTS_PER_PAGE),
            "max_days_old": str(cell.max_days_old),
            "sort_by": "date",
        }
        if cell.location_key == REMOTE_DE:
            params["what"] = f"{cell.query_text} remote"
        elif not cell.location_label:
            raise ValueError(f"cell {cell.cell_id} is a city cell without its city")
        elif cell.radius_km is None:
            raise ValueError(f"cell {cell.cell_id} is a city cell without its radius")
        else:
            params |= {"what": cell.query_text, "where": cell.location_label, "distance": str(cell.radius_km)}
        return RequestSpec(
            source_instance_id=self._instance_id,
            operation="list",
            cell_id=cell.cell_id,
            page_no=1,
            params=params,
            cursor_in=None,
            detail_native_id=None,
            parent_fetch_request_id=None,
            window=cell.window,
            request_class=cell.request_class,
        )

    async def fetch(self, spec: RequestSpec) -> HttpOutcome:
        """One attempt for page `spec.page_no`: the credentials join the query here; never a retry. Without
        both credentials nothing is built or sent (`http.missing_credential`, an `auth` failure before the
        send)."""
        if (refused := missing_credential(spec, self._app_id, self._app_key)) is not None:
            return HttpOutcome(None, refused)
        params = {
            **spec.params,
            "app_id": self._app_id.decode("utf-8"),
            "app_key": self._app_key.decode("utf-8"),
        }
        request = self._client.build_request("GET", f"{SEARCH_URL}/{spec.page_no}", params=params)
        return await send(self._client, spec, request, now=self._clock)

    def check_envelope(self, page: RawPage) -> None:
        _results(page)

    def peek_native_ids(self, page: RawPage) -> list[str | None]:
        return [_native_id(job) for job in _results(page)[0]]

    def elements(self, page: RawPage) -> list[bytes]:
        return [canonical_json(job) for job in _results(page)[0]]

    def parse(self, page: RawPage) -> list[ItemOutcome]:
        outcomes: list[ItemOutcome] = []
        first_seen: dict[str, int] = {}
        for index, job in enumerate(_results(page)[0]):
            outcome: ItemOutcome = _parse_item(index, job)
            if isinstance(outcome, Parsed):
                first = first_seen.setdefault(outcome.native_id, index)
                if first != index:
                    outcome = DuplicateInPage(index, outcome.native_id, outcome.item_ts, first)
            outcomes.append(outcome)
        return outcomes

    def continuation(self, page: RawPage) -> tuple[Continuation, str | None]:
        results, count = _results(page)
        page_no = page.spec.page_no
        if len(results) < RESULTS_PER_PAGE or page_no * RESULTS_PER_PAGE >= count:
            return Continuation.END, None
        return Continuation.CURSOR, str(page_no + 1)

    def next_page(self, spec: RequestSpec, cursor: str | None) -> RequestSpec | None:
        """The page `cursor` names (the number `continuation` gave), as a `normal` request (§6)."""
        if not cursor:
            return None
        if not (cursor.isascii() and cursor.isdigit()) or int(cursor) < 1:
            raise ValueError(f"an Adzuna cursor is a page number >= 1, got {cursor!r}")
        return replace(spec, page_no=int(cursor), cursor_in=None, request_class="normal")

    def settle(self, outcome: HttpOutcome) -> Settlement:
        error = outcome.error
        if outcome.page is None and error is not None and error.before_send:
            return "not_billed", "connect_failed_before_send"
        return "billed", None

    def observation(self, page: RawPage) -> Observation | None:
        """None: Adzuna's responses carry no quota figures (V18)."""
        return None
