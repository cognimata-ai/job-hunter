"""The JSearch adapter (RapidAPI), the primary paid aggregator (spec §5.3.1).

Request: `GET https://jsearch.p.rapidapi.com/search-v2` with the key in the `x-rapidapi-key` header, never in
the URL; the legacy `/search` route is never used. A spec holds `query`, `country=de`, `num_pages=1` and
`date_posted` (the cell's bucket); the continuation cursor travels in `RequestSpec.cursor_in` and is added to
the request when it is sent, as the key is.

Envelope (spike V9, `docs/spikes/V9.md`): the items are at `ITEMS_PATH` and the continuation cursor at
`CURSOR_PATH`. The end of the results is the cursor key present with `null` (or missing), never an empty page:
`continuation` follows the cursor whatever the page length. `check_envelope` asserts the status, the item
array and the cursor together, so a drift of any of them breaks the contract instead of reading as an empty
page. A `status` that is a string other than `OK` is the provider's own error report, delivered with a 2xx
(T9-a): `SourceError("transient", code="provider_error")`, a failed attempt that counts toward the breaker
threshold, not a broken contract. A missing or non-string `status` is a contract violation. `parse`,
`peek_native_ids` and `continuation` apply the same check, so no path reads an error body as a page.

Identity: `native_id` is `job_uid`, the Google `htidocid`. `job_id` embeds a session blob that changes from
request to request and is never read.

Settlement (§5.3.1, §6.1): a 2xx is `billed` (an empty page too: one credit per page), a 404 that the RapidAPI
gateway answered itself is `not_billed`, a connect error raised before any byte was sent is `not_billed`, and
everything else after dispatch is `ambiguous`: a timeout, a lost response, a 429 or a 5xx may have been
counted.
"""

from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Final

import httpx

from jobhunter.db.seed import canonical_json
from jobhunter.normalize.keys import AdKey, jsearch_uid_key, keys_from_link
from jobhunter.normalize.urls import canonical_url
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
from jobhunter.sources.parsing.dates import Precision, from_epoch, jsearch_precision
from jobhunter.sources.parsing.description import plain_description
from jobhunter.sources.parsing.employment import JSEARCH_TYPES, EmploymentType
from jobhunter.sources.parsing.locations import parse_jsearch_location
from jobhunter.types import JSONValue

SOURCE_ID: Final = "jsearch"
SEARCH_URL: Final = "https://jsearch.p.rapidapi.com/search-v2"
HOST: Final = "jsearch.p.rapidapi.com"

# The item array and the cursor of a `search-v2` response, as recorded by spike V9.
ITEMS_PATH: Final[tuple[str, ...]] = ("data", "jobs")
CURSOR_PATH: Final[tuple[str, ...]] = ("data", "cursor")
# Page 2 is requested only when at least this many of the page's 10 `job_uid`s are new (§5.3.1).
PAGE2_MIN_NEW: Final = 7
# How long after it was handed out a cursor may start a later run's traversal (§5.3.1; read by the planner's
# cursor reuse). It is the longest interval at which reuse was observed to work, never an extrapolation: the
# 24.04 h old page-1 cursor still returned page 2 of the query (V10 phase 3, `docs/spikes/V10.md`: 6 of its
# 8 `job_uid`s are those of `search_v2_page2`), after 2.01 h had worked in V9 phase 2.
CURSOR_REUSE_TTL: Final[timedelta] = timedelta(hours=24)

_ALL_WORK_MODES: Final = frozenset({"remote", "hybrid", "onsite"})
_GATEWAY_MARKER: Final = "x-rapidapi-proxy-response"


def _lookup(doc: JSONValue, path: Sequence[str]) -> tuple[bool, JSONValue]:
    """Whether the objects of `doc` hold `path`, and the value there."""
    node = doc
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return False, None
        node = node[key]
    return True, node


def _document(page: RawPage) -> dict[str, JSONValue]:
    """The body as an object whose `status` is `OK`; anything else raises (see the module docstring)."""
    doc = page.json()
    if not isinstance(doc, dict):
        raise ContractViolation("not_object", status=page.status)
    status = doc.get("status")
    if not isinstance(status, str):
        raise ContractViolation("status_invalid", status=page.status)
    if status != "OK":
        # The provider's text is not copied into the error: it is neither logged nor stored.
        raise SourceError("transient", code="provider_error", status=page.status)
    return doc


def _items(doc: JSONValue, status: int | None) -> list[JSONValue]:
    found, items = _lookup(doc, ITEMS_PATH)
    if not found:
        raise ContractViolation("items_missing", status=status)
    if not isinstance(items, list):
        raise ContractViolation("items_not_list", status=status)
    return items


def _cursor(doc: JSONValue, status: int | None) -> str | None:
    found, cursor = _lookup(doc, CURSOR_PATH)
    if not found or cursor is None:
        return None
    if isinstance(cursor, str) and cursor:
        return cursor
    raise ContractViolation("cursor_invalid", status=status)


def items_of(doc: JSONValue) -> list[JSONValue]:
    """The item array of a `search-v2` document (at `ITEMS_PATH`); anything else is a violation."""
    return _items(doc, None)


def cursor_of(doc: JSONValue) -> str | None:
    """The continuation cursor (at `CURSOR_PATH`); `None` when the key is missing or `null`, the end form.

    A cursor that is not a non-empty string is a violation, never the end: a malformed token must not close
    a traversal as complete.
    """
    return _cursor(doc, None)


def _text(job: dict[str, JSONValue], key: str) -> str | None:
    value = job.get(key)
    return value if isinstance(value, str) else None


def _uid(job: JSONValue) -> str | None:
    uid = job.get("job_uid") if isinstance(job, dict) else None
    return uid if isinstance(uid, str) and uid else None


def _employment(value: JSONValue) -> tuple[EmploymentType, ...]:
    """The mapped `job_employment_types`, each once; a value outside the §5.3.1 mapping is left out."""
    mapped: list[EmploymentType] = []
    for raw in value if isinstance(value, list) else []:
        kind = JSEARCH_TYPES.get(raw) if isinstance(raw, str) else None
        if kind is not None and kind not in mapped:
            mapped.append(kind)
    return tuple(mapped)


def _posted(job: dict[str, JSONValue]) -> tuple[datetime | None, Precision, str | None]:
    """`job_posted_at_timestamp` with the precision its relative text implies; `posted_raw` is the text."""
    relative = _text(job, "job_posted_at")
    stamp = job.get("job_posted_at_timestamp")
    if isinstance(stamp, bool) or not isinstance(stamp, int | float):
        return None, "unknown", relative
    try:
        moment = from_epoch(stamp, precision=jsearch_precision(relative or ""))
    except ValueError:  # a timestamp outside the calendar: the date is unknown, the ad is not rejected
        return None, "unknown", relative
    return moment.at, moment.precision, relative


def _rejected(index: int, native_id: str | None, detail: str) -> Rejected:
    return Rejected(index, native_id, None, "item_contract", detail)


def _parse_item(index: int, job: JSONValue) -> Parsed | Rejected:
    if not isinstance(job, dict):
        return _rejected(index, None, "the item is not an object")
    uid = _uid(job)
    if uid is None:
        return _rejected(index, None, "job_uid is missing or not a string")
    title, company = job.get("job_title"), job.get("employer_name")
    if not isinstance(title, str):
        return _rejected(index, uid, "job_title is not a string")
    if not isinstance(company, str):
        return _rejected(index, uid, "employer_name is not a string")

    posted_at, precision, posted_raw = _posted(job)
    places = parse_jsearch_location(
        _text(job, "job_location"), _text(job, "job_city"), _text(job, "job_country")
    )
    description, quality = plain_description(job.get("job_description"))
    employment = _employment(job.get("job_employment_types"))
    remote = job.get("job_is_remote") is True
    link = (_text(job, "job_apply_link") or "").strip()
    keys: list[AdKey] = [jsearch_uid_key(uid), *(keys_from_link(link) if link else [])]
    posting = NormalizedPosting(
        native_id=uid,
        title_raw=title,
        company_raw=company,
        locations=places.locations,
        location_kind=places.kind,
        work_mode_possible=frozenset({"remote"}) if remote else _ALL_WORK_MODES,
        work_mode_source="structured" if remote else "none",
        remote_countries=(),
        employment_types=employment,
        employment_source="structured" if employment else "none",
        salary=None,
        posted_at=posted_at,
        posted_precision=precision,
        posted_raw=posted_raw,
        item_ts=posted_at,
        description_text=description,
        description_html_sanitized=None,
        description_completeness="full",
        description_quality=quality,
        ad_language_structured=None,
        primary_link=canonical_url(link) if link else None,
        outbound_url_private=None,
        publisher=_text(job, "job_publisher"),
        raw_keys=tuple(keys),
        requisition_id=None,
        source_structured={},
    )
    return Parsed(index, uid, posted_at, posting)


def _counter(page: RawPage, name: str) -> int | None:
    """A non-negative integer response header, else `None`."""
    value = page.headers.get(name, "").strip()
    return int(value) if value.isascii() and value.isdigit() else None


class JSearchAdapter:
    """`SourceAdapter` for JSearch. It holds the key as bytes and shows it nowhere."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        api_key: bytes,
        instance_id: int,
        clock: Callable[[], datetime],
    ) -> None:
        self._client = client
        self._api_key = api_key
        self._instance_id = instance_id
        self._clock = clock

    def capabilities(self) -> Capabilities:
        return Capabilities(
            SOURCE_ID, "paid", 1, "cursor", 10, "day", "full", False, 1, [RateLimit(SOURCE_ID, 1.0, 1)]
        )

    def plan(self, cell: CellPlan) -> RequestSpec:
        """Page 1 of the cell: `<query> in <city>, Germany` (`<query> remote Germany` for `remote-de`)."""
        if cell.source_id != SOURCE_ID:
            raise ValueError(f"cell {cell.cell_id} is a {cell.source_id} cell, not a {SOURCE_ID} cell")
        if cell.instance_id != self._instance_id:
            raise ValueError(
                f"cell {cell.cell_id} is planned for instance {cell.instance_id}, not {self._instance_id}"
            )
        if not cell.query_text:
            raise ValueError(f"cell {cell.cell_id} has no query text")
        if cell.bucket is None:
            raise ValueError(f"cell {cell.cell_id} has no date_posted bucket")
        if cell.location_key == REMOTE_DE:
            query = f"{cell.query_text} remote Germany"
        elif cell.location_label:
            query = f"{cell.query_text} in {cell.location_label}, Germany"
        else:
            raise ValueError(f"cell {cell.cell_id} is a city cell without its city")
        return RequestSpec(
            source_instance_id=self._instance_id,
            operation="list",
            cell_id=cell.cell_id,
            page_no=1,
            params={"query": query, "country": "de", "num_pages": "1", "date_posted": cell.bucket},
            cursor_in=None,
            detail_native_id=None,
            parent_fetch_request_id=None,
            window=cell.window,
            request_class=cell.request_class,
        )

    async def fetch(self, spec: RequestSpec) -> HttpOutcome:
        """One attempt: the key goes in a header and the cursor, if any, in the query; never a retry. Without
        a key nothing is built or sent (`http.missing_credential`, an `auth` failure before the send)."""
        if (refused := missing_credential(spec, self._api_key)) is not None:
            return HttpOutcome(None, refused)
        params = dict(spec.params)
        if spec.cursor_in is not None:
            params["cursor"] = spec.cursor_in
        request = self._client.build_request(
            "GET",
            SEARCH_URL,
            params=params,
            headers={"x-rapidapi-key": self._api_key.decode("utf-8"), "x-rapidapi-host": HOST},
        )
        return await send(self._client, spec, request, now=self._clock)

    def check_envelope(self, page: RawPage) -> None:
        doc = _document(page)
        _items(doc, page.status)
        _cursor(doc, page.status)

    def peek_native_ids(self, page: RawPage) -> list[str | None]:
        return [_uid(job) for job in _items(_document(page), page.status)]

    def elements(self, page: RawPage) -> list[bytes]:
        return [canonical_json(job) for job in _items(_document(page), page.status)]

    def parse(self, page: RawPage) -> list[ItemOutcome]:
        outcomes: list[ItemOutcome] = []
        first_seen: dict[str, int] = {}
        for index, job in enumerate(_items(_document(page), page.status)):
            outcome: ItemOutcome = _parse_item(index, job)
            if isinstance(outcome, Parsed):
                first = first_seen.setdefault(outcome.native_id, index)
                if first != index:
                    outcome = DuplicateInPage(index, outcome.native_id, outcome.item_ts, first)
            outcomes.append(outcome)
        return outcomes

    def continuation(self, page: RawPage) -> tuple[Continuation, str | None]:
        cursor = _cursor(_document(page), page.status)
        return (Continuation.END, None) if cursor is None else (Continuation.CURSOR, cursor)

    def next_page(self, spec: RequestSpec, cursor: str | None) -> RequestSpec | None:
        """The page after `spec`: the same query with `cursor`, as a `normal` request (§6)."""
        if not cursor:
            return None
        return replace(spec, page_no=spec.page_no + 1, cursor_in=cursor, request_class="normal")

    def settle(self, outcome: HttpOutcome) -> Settlement:
        page, error = outcome.page, outcome.error
        if page is None:
            if error is not None and error.before_send:
                return "not_billed", "connect_failed_before_send"
            return "ambiguous", None
        if 200 <= page.status < 300:
            return "billed", None
        gateway = page.headers.get(_GATEWAY_MARKER, "").strip().lower() == "true"
        if page.status == 404 and gateway:
            return "not_billed", "rapidapi_gateway_404"
        return "ambiguous", None

    def observation(self, page: RawPage) -> Observation | None:
        """The quota window as the response states it; `None` when it states nothing (a gateway 404).

        A 429 is observed even without counters: the quota system exhausts the window on it (§6.3).
        """
        limit = _counter(page, "x-ratelimit-requests-limit")
        remaining = _counter(page, "x-ratelimit-requests-remaining")
        reset = _counter(page, "x-ratelimit-requests-reset")
        if page.status != 429 and limit is None and remaining is None and reset is None:
            return None
        reset_at: datetime | None = None
        if reset is not None:
            try:
                reset_at = page.fetched_at + timedelta(seconds=reset)
            except OverflowError:  # a reset further away than the calendar reaches says nothing
                reset_at = None
        return Observation(
            observed_at=page.fetched_at,
            status=page.status,
            reset_at=reset_at,
            limit=limit,
            remaining=remaining,
        )
