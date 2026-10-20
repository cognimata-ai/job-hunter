"""What the six ATS adapters share (spec §5.3.5, §8.11): the adapter base, snapshot membership, the helpers.

Every ATS board is free (`billing="free"`, no ledger entry: `settle` and `observation` are no-ops), can close
a posting (`can_close=True`: absence from a structurally complete snapshot is informative), and is requested
only at its fixed API host (`watchlist_patterns.API_HOSTS`) under the per-host limit `ATS_HOST_LIMIT` (2
concurrent requests, 5 per second; `host` names the bucket). The request goes through `http.send`, so
redirects are refused except within the Personio host family (§5.2).

The envelope gate: an adapter's `_items` is the only reader of a body, and every read path (`check_envelope`,
`peek_native_ids`, `elements`, `parse`, `continuation`, the reported total) goes through it, so no error body
is ever read as a page or as the end of a board. A body of the wrong shape is a `ContractViolation`; the
provider's own error report delivered with a 2xx (a Greenhouse `{"status", "error"}`, a Lever
`{"ok": false, "error"}`, ...) is `SourceError("transient", code="provider_error")` (ruling T9-a): a failed
attempt that counts toward the free threshold, not a broken contract. An error report beside the items, even
beside an empty item list, is that error too, never an (empty) board (ruling T11-c).

The elements of a page (`elements`, ruling T15-a) are the `_canonical` bytes of its items, in their order: the
canonical JSON of a JSON item (a SmartRecruiters detail page is one element, its posting), the C14N 2.0 form
of a Personio `<position>`.

The item contract: an item that is not an object, has no native id or has no string title is
`Rejected("item_contract")`; a native id seen earlier on the page is `DuplicateInPage`. ATS postings are not
filtered by country here: the board is the user's own choice and the hard filters judge the location (§11.1).
Only SmartRecruiters drops a list item positively outside Germany, to spare its detail call.

Snapshot membership (`membership`, §5.3.5 rules 1-5) is decided from the list pages alone, independently of
normalisation: every native id that `peek_native_ids` finds is a member, even when `parse` rejects its item.
These boards are the only source that closes postings, so any doubt makes a snapshot incomplete (ruling
T11-c): the end must be proven by the last page itself, and every total a board reports must hold.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from html import escape
from typing import ClassVar, Final, Literal, cast

import httpx

from jobhunter.db.seed import canonical_json
from jobhunter.normalize.html import html_to_text, sanitize_html
from jobhunter.normalize.keys import AdKey, ats_key
from jobhunter.normalize.quality import Quality, description_quality
from jobhunter.normalize.text import norm_text
from jobhunter.normalize.urls import canonical_url
from jobhunter.sources.base import (
    ITEM_CONTRACT,
    Capabilities,
    CellPlan,
    Continuation,
    ContractViolation,
    CoverageWindow,
    DuplicateInPage,
    ItemOutcome,
    NormalizedPosting,
    Observation,
    OutOfScope,
    Parsed,
    RateLimit,
    RawPage,
    Rejected,
    RequestClass,
    RequestSpec,
    Settlement,
    SourceError,
)
from jobhunter.sources.http import HttpOutcome, send
from jobhunter.sources.parsing.dates import ParsedDate, parse_iso
from jobhunter.sources.parsing.description import plain_description
from jobhunter.sources.parsing.employment import EmploymentType
from jobhunter.sources.parsing.locations import ParsedLocations, parse_location_text
from jobhunter.sources.ratelimit import ATS_HOST_LIMIT
from jobhunter.sources.watchlist_patterns import AtsType, api_host
from jobhunter.types import JSONValue

MAX_CONCURRENCY: Final = ATS_HOST_LIMIT.concurrency
ALL_WORK_MODES: Final = frozenset({"remote", "hybrid", "onsite"})
IncompleteReason = Literal["non_2xx", "envelope", "no_end", "count_mismatch", "missing_id"]
# The §5.3.5 rules in their order: an incomplete snapshot names the first rule that failed.
RULES: Final[tuple[IncompleteReason, ...]] = ("non_2xx", "envelope", "no_end", "count_mismatch", "missing_id")


@dataclass(frozen=True)
class SnapshotDraft:
    """The membership of one board traversal and its structural completeness (§8.11), before it is stored."""

    members: frozenset[str]
    member_count: int
    complete: bool
    incomplete_reason: str | None


# ---- envelope helpers ---------------------------------------------------------------------------------


def provider_error(page: RawPage) -> SourceError:
    """The provider's own error report delivered with a 2xx: a transient failure, not a contract break."""
    return SourceError("transient", code="provider_error", status=page.status)


def object_envelope(page: RawPage, item_key: str, *, error_key: str = "error") -> dict[str, JSONValue]:
    """The envelope object of `page`, which holds its items at `item_key`.

    An object whose `error_key` is a non-empty string is the provider's error report (`provider_error`),
    with or without items beside it (T11-c: an error next to an empty list is no empty board); any other body
    that is not an object breaks the contract.
    """
    doc = page.json()
    if not isinstance(doc, dict):
        raise ContractViolation("not_object", status=page.status)
    if isinstance(message := doc.get(error_key), str) and message.strip():
        raise provider_error(page)
    return doc


def item_list(doc: dict[str, JSONValue], key: str, page: RawPage) -> list[JSONValue]:
    items = doc.get(key)
    if not isinstance(items, list):
        raise ContractViolation(f"{key}_not_list", status=page.status)
    return items


def envelope_int(doc: dict[str, JSONValue], key: str, page: RawPage) -> int:
    value = doc.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContractViolation(f"{key}_invalid", status=page.status)
    return value


# ---- item helpers -------------------------------------------------------------------------------------


def text_of(value: JSONValue) -> str | None:
    """A non-empty string, stripped; anything else is `None`."""
    if not isinstance(value, str):
        return None
    return value.strip() or None


def id_of(value: JSONValue) -> str | None:
    """A native id: a non-empty string as it stands, or an integer in decimal; anything else is `None`."""
    if isinstance(value, str):
        return value if value.strip() else None
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def field(item: JSONValue, key: str) -> JSONValue:
    """`item[key]` of an object item, `None` for a missing key or an item that is not an object."""
    return item.get(key) if isinstance(item, dict) else None


def iso_posted(value: JSONValue) -> ParsedDate | None:
    """An ISO 8601 posted date (`exact`, the board's own timestamp), `None` when it is not one."""
    return parse_iso(value, precision="exact") if isinstance(value, str) and value.strip() else None


def html_description(html: str) -> tuple[str, str | None, Quality]:
    """The plain text, the sanitised HTML (`None` without a description) and the quality of HTML text."""
    if not html.strip():
        return "", None, description_quality("")
    sanitized = sanitize_html(html)
    text = html_to_text(sanitized)
    return text, sanitized, description_quality(text)


def plain_text(value: JSONValue) -> str:
    """A plain-text description in its stored form: one normalised line per non-empty line."""
    return plain_description(value)[0]


def section(title: str | None, html: str | None) -> str:
    """One titled block of HTML (`<h3>title</h3>` and the body), for descriptions made of sections."""
    head = f"<h3>{escape(title)}</h3>" if title else ""
    return f"{head}{html or ''}" if (head or html) else ""


def structured_modes(modes: Iterable[str]) -> tuple[frozenset[str], Literal["structured", "none"]]:
    """The work modes a structured field states; all three (unknown), source `none`, when it states none."""
    stated = frozenset(modes) & ALL_WORK_MODES
    return (stated, "structured") if stated else (ALL_WORK_MODES, "none")


def country_code(value: JSONValue) -> str | None:
    """An ISO 3166-1 alpha-2 code, upper case; anything else is `None`."""
    code = text_of(value)
    return code.upper() if code is not None and len(code) == 2 and code.isascii() and code.isalpha() else None


def locations(labels: Iterable[JSONValue], *, source: str) -> ParsedLocations:
    """The locations of the distinct non-empty location strings of an item, parsed together (§8.12)."""
    distinct: list[str] = []
    for label in labels:
        text = text_of(label)
        if text is not None and (text := norm_text(text)) and text not in distinct:
            distinct.append(text)
    if not distinct:
        return ParsedLocations([], "unknown", False)
    return parse_location_text("; ".join(distinct), source=source)


def rejected(index: int, native_id: str | None, detail: str) -> Rejected:
    return Rejected(index, native_id, None, ITEM_CONTRACT, detail)


# ---- the adapter --------------------------------------------------------------------------------------


class AtsAdapter(ABC):
    """`SourceAdapter` of one ATS board: what every ATS shares; a subclass reads its own envelope and items.

    `board` is the board part of the key namespace `ats:<type>:<board>` (the slug; `<region>:<site>` for
    Lever), `host` the board's fixed API host, `company` the company name of postings whose items name none
    (the watchlist company's display name, else the slug).
    """

    ats: ClassVar[AtsType]
    pagination: ClassVar[Literal["single", "offset"]] = "single"
    page_size: ClassVar[int] = 0  # a single-page board returns every posting at once
    description: ClassVar[Literal["full", "detail_call"]] = "full"
    region: str | None = None

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        instance_id: int,
        slug: str,
        clock: Callable[[], datetime],
        company: str | None = None,
    ) -> None:
        self.host = api_host(self.ats, slug, self.region)  # refuses a slug outside `SLUG`
        self._client = client
        self.instance_id = instance_id
        self.slug = slug
        self.company = company or slug
        self._clock = clock

    @property
    def board(self) -> str:
        return self.slug

    def key(self, posting_id: str) -> AdKey:
        """The strong key of a posting of this board, `ats:<type>:<board>:<id>` (§5.3.5)."""
        return ats_key(self.ats, self.board, posting_id)

    def rate_limits(self) -> list[RateLimit]:
        return [ATS_HOST_LIMIT]

    def capabilities(self) -> Capabilities:
        return Capabilities(
            self.ats,
            "free",
            0,
            self.pagination,
            self.page_size,
            "exact",
            self.description,
            True,
            MAX_CONCURRENCY,
            self.rate_limits(),
        )

    # -- requests

    def plan(self, cell: CellPlan) -> RequestSpec:
        """The first list page of the board, with `cell.window` and the cell's request class."""
        if cell.source_id != self.ats:
            raise ValueError(f"cell {cell.cell_id} is a {cell.source_id} cell, not a {self.ats} cell")
        if cell.instance_id != self.instance_id:
            raise ValueError(
                f"cell {cell.cell_id} is planned for instance {cell.instance_id}, not {self.instance_id}"
            )
        return self.first_spec(cell_id=cell.cell_id, window=cell.window, request_class=cell.request_class)

    def first_spec(
        self, *, cell_id: int, window: CoverageWindow | None, request_class: RequestClass
    ) -> RequestSpec:
        """The first list page of the board (also the one GET of `watchlist_validate`)."""
        return RequestSpec(
            source_instance_id=self.instance_id,
            operation="list",
            cell_id=cell_id,
            page_no=1,
            params=self._first_params(),
            cursor_in=None,
            detail_native_id=None,
            parent_fetch_request_id=None,
            window=window,
            request_class=request_class,
        )

    def _first_params(self) -> dict[str, str]:
        return {}

    @abstractmethod
    def _url(self, spec: RequestSpec) -> str: ...

    async def fetch(self, spec: RequestSpec) -> HttpOutcome:
        """One attempt at the board's fixed host with the spec's parameters; never a retry."""
        request = self._client.build_request("GET", self._url(spec), params=dict(spec.params))
        return await send(self._client, spec, request, now=self._clock)

    def next_page(self, spec: RequestSpec, cursor: str | None) -> RequestSpec | None:
        """A single-page board has no next page."""
        return None

    # -- the envelope gate

    @abstractmethod
    def _items(self, page: RawPage) -> Sequence[object]:
        """The items of the page's envelope; raises `ContractViolation` (or the provider's `SourceError`)."""

    @abstractmethod
    def _native_id(self, item: object) -> str | None: ...

    @abstractmethod
    def _parse_item(self, index: int, item: object) -> Parsed | OutOfScope | Rejected: ...

    def check_envelope(self, page: RawPage) -> None:
        self._items(page)

    def peek_native_ids(self, page: RawPage) -> list[str | None]:
        return [self._native_id(item) for item in self._items(page)]

    def _canonical(self, item: object) -> bytes:
        """The canonical bytes of one item of `_items`: its canonical JSON (a JSON board's items are JSON)."""
        return canonical_json(cast(JSONValue, item))

    def elements(self, page: RawPage) -> list[bytes]:
        return [self._canonical(item) for item in self._items(page)]

    def reported_total(self, page: RawPage) -> int | None:
        """The number of items this page must hold, where the board says so (§5.3.5 rule 4, per page)."""
        self._items(page)
        return None

    def board_total(self, page: RawPage) -> int | None:
        """The number of postings the whole board holds, as this page reports it (§5.3.5 rule 4, per board):
        every list page must report the same, and the snapshot must have that many members."""
        self._items(page)
        return None

    def parse(self, page: RawPage) -> list[ItemOutcome]:
        outcomes: list[ItemOutcome] = []
        first_seen: dict[str, int] = {}
        for index, item in enumerate(self._items(page)):
            outcome: ItemOutcome = self._parse_item(index, item)
            if isinstance(outcome, Parsed | OutOfScope):
                first = first_seen.setdefault(outcome.native_id, index)
                if first != index:
                    outcome = DuplicateInPage(index, outcome.native_id, outcome.item_ts, first)
            outcomes.append(outcome)
        return outcomes

    def continuation(self, page: RawPage) -> tuple[Continuation, str | None]:
        """A single-page board ends with its page."""
        self._items(page)
        return Continuation.END, None

    # -- free source

    def settle(self, outcome: HttpOutcome) -> Settlement:
        """A free board is never billed: no ledger entry exists for it."""
        return "not_billed", None

    def observation(self, page: RawPage) -> Observation | None:
        return None

    # -- the posting

    def _posting(
        self,
        native_id: str,
        *,
        title: str,
        company: str | None,
        places: ParsedLocations,
        work_modes: tuple[frozenset[str], Literal["structured", "text", "none"]] = (ALL_WORK_MODES, "none"),
        remote_countries: tuple[str, ...] = (),
        employment: Sequence[EmploymentType] = (),
        employment_source: Literal["structured", "text"] = "structured",
        posted: ParsedDate | None,
        description: tuple[str, str | None],
        link: str | None,
        requisition_id: str | None = None,
        structured: dict[str, JSONValue] | None = None,
    ) -> NormalizedPosting:
        """The `NormalizedPosting` of an item, with the board's key as its only strong key and a full
        description (`description` is the plain text and the sanitised HTML). `structured` is kept as
        `source_structured`; its `language`, when the board states one, is also `ad_language_structured`."""
        modes, mode_source = work_modes
        text, sanitized = description
        types = tuple(dict.fromkeys(employment))
        extras = structured or {}
        language = extras.get("language")  # Greenhouse `language`, SmartRecruiters `language.code`
        return NormalizedPosting(
            native_id=native_id,
            title_raw=title,
            company_raw=company or self.company,
            locations=places.locations,
            location_kind=places.kind,
            work_mode_possible=modes,
            work_mode_source=mode_source,
            remote_countries=remote_countries,
            employment_types=types,
            employment_source=employment_source if types else "none",
            salary=None,
            posted_at=None if posted is None else posted.at,
            posted_precision="unknown" if posted is None else posted.precision,
            posted_raw=None if posted is None else posted.raw,
            item_ts=None if posted is None else posted.at,
            description_text=text,
            description_html_sanitized=sanitized,
            description_completeness="full",
            description_quality=description_quality(text),
            ad_language_structured=language if isinstance(language, str) else None,
            primary_link=canonical_url(link) if link else None,
            outbound_url_private=None,
            publisher=None,
            raw_keys=(self.key(native_id),),
            requisition_id=requisition_id,
            source_structured=extras,
        )


def _ends(adapter: AtsAdapter, page: RawPage) -> bool:
    """Whether `page` itself says the board ends with it (a 2xx whose continuation is `END`)."""
    if not 200 <= page.status < 300:
        return False
    try:
        return adapter.continuation(page)[0] is Continuation.END
    except SourceError:
        return False


def membership(pages: Sequence[RawPage], adapter: AtsAdapter, *, reached_end: bool) -> SnapshotDraft:
    """The snapshot of a board traversal from its list pages (§5.3.5): every native id that
    `peek_native_ids` finds is a member, and the snapshot is complete only when

    1. every list page returned 2xx;
    2. its envelope passed the contract;
    3. the pagination reached `END`: the caller says so (`reached_end`), the pages are numbered 1 to n without
       a gap or a repeat, and the last of them itself says `END` (a traversal without a list page reached
       nothing);
    4. the counts hold: a page holds the items it must (`reported_total`, Greenhouse's `meta.total`), and
       every page reports the same board total (`board_total`, SmartRecruiters' `totalFound`), which equals
       the number of members;
    5. every item had a native id.

    The caller passes one page per logical page: the final attempt of each list request. Detail pages
    (SmartRecruiters) are not part of membership and are ignored. An incomplete snapshot keeps every member
    found and names the first rule that failed.
    """
    failed: set[IncompleteReason] = set()
    members: set[str] = set()
    board_totals: list[int] = []
    listed = sorted(
        (page for page in pages if page.spec.operation == "list"), key=lambda page: page.spec.page_no
    )
    numbered = [page.spec.page_no for page in listed] == list(range(1, len(listed) + 1))
    if not reached_end or not listed or not numbered or not _ends(adapter, listed[-1]):
        failed.add("no_end")
    for page in listed:
        if not 200 <= page.status < 300:
            failed.add("non_2xx")
            continue
        try:
            adapter.check_envelope(page)
            ids = adapter.peek_native_ids(page)
            total = adapter.reported_total(page)
            board = adapter.board_total(page)
        except SourceError:
            failed.add("envelope")
            continue
        members.update(native_id for native_id in ids if native_id is not None)
        if None in ids:
            failed.add("missing_id")
        if total is not None and total != len(ids):
            failed.add("count_mismatch")
        if board is not None:
            board_totals.append(board)
    if board_totals and (len(set(board_totals)) != 1 or board_totals[0] != len(members)):
        failed.add("count_mismatch")
    reason = next((rule for rule in RULES if rule in failed), None)
    return SnapshotDraft(frozenset(members), len(members), reason is None, reason)
