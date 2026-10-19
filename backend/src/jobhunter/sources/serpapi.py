"""The SerpApi Google Jobs adapter, the paid fallback (spec §5.3.2).

Request: `GET https://serpapi.com/search.json` with `engine=google_jobs`, `q`, `gl=de` and `hl=SERPAPI_HL`;
`location` and `uule` are never sent. The key (`api_key`) and the continuation token (`next_page_token`, kept
in `RequestSpec.cursor_in`) are added to the request when it is sent: a spec never holds a credential.

`account()` reads `GET https://serpapi.com/account.json`, which is free and no ledger entry. The caller reads
it before every search; a read that fails raises, so that no search is admitted without a fresh account
(§5.3.2, fail closed). A search response itself carries no quota figures, so `observation` is `None`.

Identity: the `job_id` is base64 of a JSON object; `native_id` is its `htidocid`, the same Google id JSearch
calls `job_uid` (V3: `SERPAPI_HL = "en"` keeps it identical). The `fc` token is discarded.

Primary link: the `apply_options[]` entry whose `title` is the `via` publisher, else the first entry. No other
option is stored or used for a key: they link to unrelated postings.

Dates, work mode, schedule and salary come from the `extensions[]` chips and, when present, from
`detected_extensions`, which wins (`parse_extensions`). The `hl=en` recording has no `detected_extensions`
(ruling T6-a), so the chips are the usual source. Relative dates resolve against the response `Date` header
(`fetched_at` without one). The work mode is always `text` evidence, `work_from_home` included: it says remote
work is possible, not that the job cannot be on site (T9-b). The employment types are `structured` only when
every one of them came from `detected_extensions.schedule_type`; once a chip adds one they are `text`.

A search with no jobs is an empty page, not a violation, when Google says so: `error` reads "hasn't returned
any results" or `search_information.jobs_results_state` is `Fully empty` (T9-c). A body with
`search_metadata.status == "Error"` and an `error` string is the provider's own error report, delivered with a
2xx (T9-a): `SourceError("transient", code="provider_error")`, a failed attempt that counts toward the breaker
threshold, or the `quota_exhausted` kind when the account has run out of searches. A missing `search_metadata`
or any other body without `jobs_results` is a contract violation.

Settlement (§5.3.2): a 2xx is `billed`, an empty result too; a connect error before any byte was sent is
`not_billed`; everything else is `ambiguous`.
"""

import re
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from typing import Final, Literal

import httpx

from jobhunter.db.seed import canonical_json
from jobhunter.normalize.keys import AdKey, decode_serpapi_job_id, keys_from_link
from jobhunter.normalize.text import norm_text
from jobhunter.normalize.urls import canonical_url
from jobhunter.quota.windows import AccountObservation
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
    SourceErrorKind,
)
from jobhunter.sources.http import HttpOutcome, send
from jobhunter.sources.parsing.dates import parse_iso, response_date
from jobhunter.sources.parsing.description import plain_description
from jobhunter.sources.parsing.employment import parse_employment
from jobhunter.sources.parsing.extensions import parse_extensions
from jobhunter.sources.parsing.locations import ParsedLocations, parse_location_text
from jobhunter.types import JSONValue, get_int, get_str

SOURCE_ID: Final = "serpapi"
SEARCH_URL: Final = "https://serpapi.com/search.json"
ACCOUNT_URL: Final = "https://serpapi.com/account.json"
# The Google Jobs language (spike V3, `docs/spikes/V3.md`, ruling T6-a): `en` keeps the `htidocid` of JSearch.
SERPAPI_HL: Final[Literal["de", "en"]] = "en"

_ALL_WORK_MODES: Final = frozenset({"remote", "hybrid", "onsite"})
# The `error` of a search Google answered with no jobs (SerpApi words it in English whatever `hl` is).
_NO_RESULTS: Final = re.compile(r"hasn.t returned any results", re.IGNORECASE)
# The structured form of the same fact, which does not depend on the wording of the message (T9-c).
_FULLY_EMPTY: Final = "fully empty"
_OUT_OF_SEARCHES: Final = re.compile(r"run out of searches", re.IGNORECASE)
_VIA_PREFIX: Final = re.compile(r"^via\s+", re.IGNORECASE)


def _document(page: RawPage) -> dict[str, JSONValue]:
    doc = page.json()
    if not isinstance(doc, dict):
        raise ContractViolation("not_object", status=page.status)
    return doc


def _says_no_results(doc: dict[str, JSONValue]) -> bool:
    """Google found nothing: `search_information.jobs_results_state` or the `error` message says so."""
    information = doc.get("search_information")
    state = information.get("jobs_results_state") if isinstance(information, dict) else None
    if isinstance(state, str) and state.strip().casefold() == _FULLY_EMPTY:
        return True
    error = doc.get("error")
    return isinstance(error, str) and _NO_RESULTS.search(error) is not None


def _jobs(doc: dict[str, JSONValue], status: int) -> list[JSONValue]:
    """`jobs_results`. Absent, the body is an empty page when Google says it found nothing, SerpApi's own
    error report (`SourceError`) when its status is `Error` with an `error` string, else a violation."""
    if "jobs_results" in doc:
        jobs = doc["jobs_results"]
        if not isinstance(jobs, list):
            raise ContractViolation("jobs_results_not_list", status=status)
        return jobs
    if _says_no_results(doc):
        return []
    metadata, error = doc.get("search_metadata"), doc.get("error")
    if isinstance(metadata, dict) and metadata.get("status") == "Error" and isinstance(error, str):
        # The provider's text is not copied into the error: it is neither logged nor stored.
        kind: SourceErrorKind = "quota_exhausted" if _OUT_OF_SEARCHES.search(error) else "transient"
        raise SourceError(kind, code="provider_error", status=status)
    raise ContractViolation("jobs_results_missing", status=status)


def _next_token(doc: dict[str, JSONValue], status: int) -> str | None:
    pagination = doc.get("serpapi_pagination")
    if pagination is None:
        return None
    if not isinstance(pagination, dict):
        raise ContractViolation("pagination_invalid", status=status)
    token = pagination.get("next_page_token")
    if token is None:
        return None
    if isinstance(token, str) and token:
        return token
    raise ContractViolation("pagination_invalid", status=status)


def _text(job: dict[str, JSONValue], key: str) -> str | None:
    value = job.get(key)
    return value if isinstance(value, str) else None


def _htidocid(job: JSONValue) -> str | None:
    job_id = job.get("job_id") if isinstance(job, dict) else None
    return decode_serpapi_job_id(job_id) if isinstance(job_id, str) else None


def _publisher(via: str | None) -> str | None:
    """The `via` name without its `via ` prefix."""
    name = _VIA_PREFIX.sub("", norm_text(via or ""))
    return name or None


def _primary_link(job: dict[str, JSONValue], publisher: str | None) -> str | None:
    """The link of the apply option titled like the publisher, else of the first option (§5.3.2)."""
    options = job.get("apply_options")
    candidates: list[tuple[str, str]] = []  # (folded title, link) of the options that have a link
    for option in options if isinstance(options, list) else []:
        if not isinstance(option, dict):
            continue
        link, title = option.get("link"), option.get("title")
        if isinstance(link, str) and link.strip():
            candidates.append((norm_text(title).casefold() if isinstance(title, str) else "", link.strip()))
    if not candidates:
        return None
    wanted = None if publisher is None else publisher.casefold()
    return next((link for title, link in candidates if title == wanted), candidates[0][1])


def _rejected(index: int, native_id: str | None, detail: str) -> Rejected:
    return Rejected(index, native_id, None, "item_contract", detail)


def _parse_item(index: int, job: JSONValue, reference: datetime) -> Parsed | Rejected:
    if not isinstance(job, dict):
        return _rejected(index, None, "the item is not an object")
    htidocid = _htidocid(job)
    if htidocid is None:
        return _rejected(index, None, "job_id does not decode to an htidocid")
    title, company = job.get("title"), job.get("company_name")
    if not isinstance(title, str):
        return _rejected(index, htidocid, "title is not a string")
    if not isinstance(company, str):
        return _rejected(index, htidocid, "company_name is not a string")

    chips = job.get("extensions")
    detected = job.get("detected_extensions")
    structured = detected if isinstance(detected, dict) else None
    found = parse_extensions(
        [chip for chip in chips if isinstance(chip, str)] if isinstance(chips, list) else [],
        structured,
        reference=reference,
    )
    schedule = structured.get("schedule_type") if structured else None
    scheduled = parse_employment(schedule) if isinstance(schedule, str) else []
    employment_source: Literal["structured", "text", "none"] = (
        "none"
        if not found.employment
        else "structured"
        if all(kind in scheduled for kind in found.employment)  # no chip added a type
        else "text"
    )
    # `work_from_home` says remote work is possible, which a chip says too: text evidence, never an exclusion.
    work_mode_source: Literal["text", "none"] = "none" if found.work_modes is None else "text"
    location = _text(job, "location")
    places = (
        parse_location_text(location, source=SOURCE_ID) if location else ParsedLocations([], "unknown", False)
    )
    description, quality = plain_description(job.get("description"))
    publisher = _publisher(_text(job, "via"))
    link = _primary_link(job, publisher)
    posted_at = None if found.posted is None else found.posted.at
    posting = NormalizedPosting(
        native_id=htidocid,
        title_raw=title,
        company_raw=company,
        locations=places.locations,
        location_kind=places.kind,
        work_mode_possible=_ALL_WORK_MODES if found.work_modes is None else found.work_modes,
        work_mode_source=work_mode_source,
        remote_countries=(),
        employment_types=found.employment,
        employment_source=employment_source,
        salary=found.salary,
        posted_at=posted_at,
        posted_precision="unknown" if found.posted is None else found.posted.precision,
        posted_raw=None if found.posted is None else found.posted.raw,
        item_ts=posted_at,
        description_text=description,
        description_html_sanitized=None,
        description_completeness="full",
        description_quality=quality,
        ad_language_structured=None,
        primary_link=canonical_url(link) if link else None,
        outbound_url_private=None,
        publisher=publisher,
        raw_keys=(AdKey("htidocid", htidocid), *(keys_from_link(link) if link else [])),
        requisition_id=None,
        source_structured={},
    )
    return Parsed(index, htidocid, posted_at, posting)


def _account_status_error(status: int) -> SourceError:
    kind: SourceErrorKind
    if status in (401, 403):
        kind = "auth"
    elif status == 429:
        kind = "rate_limited"
    elif status >= 500:
        kind = "transient"
    else:
        kind = "client"
    return SourceError(kind, code=f"account_http_{status}", status=status)


class SerpApiAdapter:
    """`SourceAdapter` for SerpApi Google Jobs. It holds the key as bytes and shows it nowhere."""

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
            SOURCE_ID, "paid", 1, "cursor", 10, "relative", "full", False, 1, [RateLimit(SOURCE_ID, 1.0, 1)]
        )

    def plan(self, cell: CellPlan) -> RequestSpec:
        """Page 1 of the cell: `<query> <city>` (`<query> Homeoffice Deutschland` for `remote-de`)."""
        if cell.source_id != SOURCE_ID:
            raise ValueError(f"cell {cell.cell_id} is a {cell.source_id} cell, not a {SOURCE_ID} cell")
        if cell.instance_id != self._instance_id:
            raise ValueError(
                f"cell {cell.cell_id} is planned for instance {cell.instance_id}, not {self._instance_id}"
            )
        if not cell.query_text:
            raise ValueError(f"cell {cell.cell_id} has no query text")
        if cell.location_key == REMOTE_DE:
            query = f"{cell.query_text} Homeoffice Deutschland"
        elif cell.location_label:
            query = f"{cell.query_text} {cell.location_label}"
        else:
            raise ValueError(f"cell {cell.cell_id} is a city cell without its city")
        return RequestSpec(
            source_instance_id=self._instance_id,
            operation="list",
            cell_id=cell.cell_id,
            page_no=1,
            params={"q": query, "gl": "de", "hl": SERPAPI_HL, "engine": "google_jobs"},
            cursor_in=None,
            detail_native_id=None,
            parent_fetch_request_id=None,
            window=cell.window,
            request_class=cell.request_class,
        )

    async def fetch(self, spec: RequestSpec) -> HttpOutcome:
        """One attempt: the key and the continuation token, if any, are added to the query here; no retry."""
        params = dict(spec.params)
        if spec.cursor_in is not None:
            params["next_page_token"] = spec.cursor_in
        params["api_key"] = self._api_key.decode("utf-8")
        request = self._client.build_request("GET", SEARCH_URL, params=params)
        return await send(self._client, spec, request, now=self._clock)

    async def account(self) -> AccountObservation:
        """The free `account.json` read (§5.3.2). A read that fails or breaks the contract raises."""
        spec = RequestSpec(
            source_instance_id=self._instance_id,
            operation="list",
            cell_id=0,  # no cell: an account read belongs to no traversal and has no ledger entry
            page_no=1,
            params={},
            cursor_in=None,
            detail_native_id=None,
            parent_fetch_request_id=None,
            window=None,
            request_class="free",
        )
        request = self._client.build_request(
            "GET", ACCOUNT_URL, params={"api_key": self._api_key.decode("utf-8")}
        )
        outcome = await send(self._client, spec, request, now=self._clock)
        if outcome.error is not None:
            raise outcome.error
        page = outcome.page
        if (
            page is None
        ):  # `send` returns a page or an error; this keeps a broken transport from reading as data
            raise SourceError("transient", code="account_no_response")
        if not 200 <= page.status < 300:
            raise _account_status_error(page.status)
        doc = page.json()
        try:
            renewal = parse_iso(get_str(doc, "plan_renewal_date"), precision="day")
            if renewal is None:
                raise ValueError("plan_renewal_date is not a date")
            return AccountObservation(
                observed_at=page.fetched_at,
                plan_renewal_date=renewal.at,
                searches_per_month=get_int(doc, "searches_per_month"),
                plan_searches_left=get_int(doc, "plan_searches_left"),
                this_hour_searches=get_int(doc, "this_hour_searches"),
                account_rate_limit_per_hour=get_int(doc, "account_rate_limit_per_hour"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ContractViolation("account_envelope", status=page.status) from exc

    def check_envelope(self, page: RawPage) -> None:
        doc = _document(page)
        if not isinstance(doc.get("search_metadata"), dict):
            raise ContractViolation("search_metadata_missing", status=page.status)
        _jobs(doc, page.status)
        _next_token(doc, page.status)

    def peek_native_ids(self, page: RawPage) -> list[str | None]:
        return [_htidocid(job) for job in _jobs(_document(page), page.status)]

    def elements(self, page: RawPage) -> list[bytes]:
        return [canonical_json(job) for job in _jobs(_document(page), page.status)]

    def parse(self, page: RawPage) -> list[ItemOutcome]:
        reference = response_date(page.headers, page.fetched_at)
        outcomes: list[ItemOutcome] = []
        first_seen: dict[str, int] = {}
        for index, job in enumerate(_jobs(_document(page), page.status)):
            outcome: ItemOutcome = _parse_item(index, job, reference)
            if isinstance(outcome, Parsed):
                first = first_seen.setdefault(outcome.native_id, index)
                if first != index:
                    outcome = DuplicateInPage(index, outcome.native_id, outcome.item_ts, first)
            outcomes.append(outcome)
        return outcomes

    def continuation(self, page: RawPage) -> tuple[Continuation, str | None]:
        doc = _document(page)
        # An error report has no pagination: it must not read as the end of the results.
        _jobs(doc, page.status)
        token = _next_token(doc, page.status)
        return (Continuation.END, None) if token is None else (Continuation.CURSOR, token)

    def next_page(self, spec: RequestSpec, cursor: str | None) -> RequestSpec | None:
        """The page after `spec`: the same search with the `next_page_token`, as a `normal` request."""
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
        return "ambiguous", None

    def observation(self, page: RawPage) -> Observation | None:
        """None: SerpApi's quota figures come from the account read, not from a search response."""
        return None
