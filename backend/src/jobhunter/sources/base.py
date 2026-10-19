"""The adapter protocol and its models (spec §5.1; plan Deviation 25).

A `RequestSpec` is what `fetch_requests.spec` persists: it never holds a secret, which an adapter adds only
when it sends (`SECRET_PARAMS` are refused as parameters). `request_key` is the §5.1 identity of a logical
request. A `RawPage` is one response as `http.send` read it; `json()` turns a body that is not JSON (an HTML
error page, a truncated body) into `ContractViolation("not_json")`, never into an exception of the parser.

`SourceAdapter` is the §5.1 protocol with the M1 signatures of Deviation 25: `plan(cell)` returns the first
page of a traversal, every later page comes from `next_page`, and `fetch` returns the transport outcome
(`http.HttpOutcome`: the page and the error together) that `settle` classifies. Ruling T15-a adds `elements`:
the canonical bytes of each element of a page's item array, whose `element_sha256` is `raw_items.item_sha256`
and whose number is `raw_responses.item_count`; `parse` returns exactly one outcome per element.
"""

import codecs
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from functools import cached_property
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal, NoReturn, Protocol, cast, get_args

import httpx
from pydantic import BaseModel, ConfigDict

from jobhunter.db.seed import canonical_json
from jobhunter.errors import DomainError
from jobhunter.normalize.keys import AdKey
from jobhunter.normalize.quality import Quality
from jobhunter.sources.parsing.dates import Precision
from jobhunter.sources.parsing.employment import EmploymentType
from jobhunter.sources.parsing.locations import Location, LocationKind
from jobhunter.sources.parsing.salary import Salary
from jobhunter.types import JSONValue, get_int, get_list, get_object, get_str, json_get

if TYPE_CHECKING:
    from jobhunter.sources.http import HttpOutcome

Billing = Literal["paid", "quota", "free"]
Operation = Literal["list", "detail"]
RequestClass = Literal["normal", "retry", "probe", "free"]
CoverageWindow = tuple[datetime, datetime]
# The query parameters that carry provider credentials (SerpApi `api_key`, Adzuna `app_id`/`app_key`). They
# are added at send time only, so a spec refuses them and the request log removes them.
SECRET_PARAMS: Final = frozenset({"api_key", "app_id", "app_key"})
# The `location_key` of the cell that asks for remote work within Germany instead of a city (§5.5).
REMOTE_DE: Final = "remote-de"


def public_query(url: httpx.URL) -> str:
    """The query of `url` without its credential parameters (`SECRET_PARAMS`, any case), encoded: what a
    log line may carry."""
    kept: list[tuple[str, str | int | float | bool | None]] = [
        (name, value) for name, value in url.params.multi_items() if name.lower() not in SECRET_PARAMS
    ]
    return str(httpx.QueryParams(kept))


class Continuation(Enum):
    CURSOR = "cursor"  # a next page exists; the cursor is set
    END = "end"  # traversal of this window is complete
    UNKNOWN = "unknown"  # completeness cannot be determined


@dataclass(frozen=True)
class RateLimit:
    """A token bucket of `per_second` refilled continuously, and a semaphore of `concurrency` (§5.3)."""

    key: str
    per_second: float
    concurrency: int


@dataclass(frozen=True)
class Capabilities:
    source_id: str  # 'jsearch' | 'serpapi' | 'adzuna' | 'arbeitnow' | ATS ids
    billing: Billing
    unit_per_page: int  # 1 for paid/quota sources, 0 for free
    pagination: Literal["cursor", "page", "offset", "single"]
    page_size: int
    date_semantics: Literal["exact", "day", "relative", "ingest"]
    description: Literal["full", "partial", "detail_call"]
    can_close: bool  # absence in a complete snapshot is informative (ATS only)
    max_concurrency: int
    rate_limits: Sequence[RateLimit]  # enforced by sources/ratelimit.py


def _one_of(value: str, allowed: object, what: str) -> str:
    """`value` when it is one of the strings of the `Literal` type `allowed`, else `ValueError`."""
    if value not in get_args(allowed):
        raise ValueError(f"{what} must be one of {', '.join(get_args(allowed))}, got {value!r}")
    return value


def _optional_str(doc: JSONValue, key: str) -> str | None:
    return None if json_get(doc, key) is None else get_str(doc, key)


def _optional_int(doc: JSONValue, key: str) -> int | None:
    return None if json_get(doc, key) is None else get_int(doc, key)


@dataclass(frozen=True)
class RequestSpec:
    """One logical request, as `fetch_requests.spec` persists it (never with a secret)."""

    source_instance_id: int
    operation: Operation
    cell_id: int
    page_no: int
    params: Mapping[str, str]  # canonical: sorted, read-only; secrets are added only at send time
    cursor_in: str | None
    detail_native_id: str | None  # SmartRecruiters detail calls
    parent_fetch_request_id: int | None  # the list page that produced a detail call
    window: CoverageWindow | None
    request_class: RequestClass

    def __post_init__(self) -> None:
        if secrets := {name for name in self.params if name.lower() in SECRET_PARAMS}:
            raise ValueError(
                f"a request spec never holds a credential parameter: {', '.join(sorted(secrets))}"
            )
        object.__setattr__(self, "params", MappingProxyType(dict(sorted(self.params.items()))))

    def to_json(self) -> dict[str, JSONValue]:
        params: dict[str, JSONValue] = {name: value for name, value in self.params.items()}
        window: JSONValue = None if self.window is None else [moment.isoformat() for moment in self.window]
        return {
            "source_instance_id": self.source_instance_id,
            "operation": self.operation,
            "cell_id": self.cell_id,
            "page_no": self.page_no,
            "params": params,
            "cursor_in": self.cursor_in,
            "detail_native_id": self.detail_native_id,
            "parent_fetch_request_id": self.parent_fetch_request_id,
            "window": window,
            "request_class": self.request_class,
        }

    @classmethod
    def from_json(cls, doc: JSONValue) -> "RequestSpec":
        """The spec `to_json` wrote; a malformed document raises `KeyError`, `TypeError` or `ValueError`."""
        window: CoverageWindow | None = None
        if json_get(doc, "window") is not None:
            lower, upper = (datetime.fromisoformat(get_str(bound, "")) for bound in get_list(doc, "window"))
            window = (lower, upper)
        return cls(
            source_instance_id=get_int(doc, "source_instance_id"),
            operation=cast(Operation, _one_of(get_str(doc, "operation"), Operation, "operation")),
            cell_id=get_int(doc, "cell_id"),
            page_no=get_int(doc, "page_no"),
            params={name: get_str(value, "") for name, value in get_object(doc, "params").items()},
            cursor_in=_optional_str(doc, "cursor_in"),
            detail_native_id=_optional_str(doc, "detail_native_id"),
            parent_fetch_request_id=_optional_int(doc, "parent_fetch_request_id"),
            window=window,
            request_class=cast(
                RequestClass, _one_of(get_str(doc, "request_class"), RequestClass, "request_class")
            ),
        )


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def request_key(spec: RequestSpec, run_id: int) -> bytes:
    """`sha256(canonical_json({source_instance_id, operation, run_id, cell_id, page_no, sha256(cursor_in),
    detail_native_id, sha256(params)}))` (§5.1); an unset field is `null`. The 32-byte digest."""
    params: dict[str, JSONValue] = {name: value for name, value in spec.params.items()}
    identity: dict[str, JSONValue] = {
        "source_instance_id": spec.source_instance_id,
        "operation": spec.operation,
        "run_id": run_id,
        "cell_id": spec.cell_id,
        "page_no": spec.page_no,
        "cursor_in_sha256": None if spec.cursor_in is None else _sha256_hex(spec.cursor_in.encode("utf-8")),
        "detail_native_id": spec.detail_native_id,
        "params_sha256": _sha256_hex(canonical_json(params)),
    }
    return hashlib.sha256(canonical_json(identity)).digest()


SourceErrorKind = Literal[
    "transient", "rate_limited", "auth", "quota_exhausted", "contract", "not_found", "client", "ambiguous"
]


class SourceError(DomainError):
    """A source call failed (§17.1). The message names the kind, the code and the status, nothing else."""

    def __init__(
        self, kind: SourceErrorKind, *, code: str, status: int | None = None, before_send: bool = False
    ) -> None:
        super().__init__(f"{kind}: {code}" if status is None else f"{kind}: {code} (HTTP {status})")
        self.kind = kind
        self.code = code
        self.status = status
        # A connect error raised before any request byte was written (`connect_failed_before_send`, §6.1).
        self.before_send = before_send


class ContractViolation(SourceError):
    """The response breaks its contract (§5.1 `check_envelope`); `code` names the check that failed."""

    def __init__(self, code: str, *, status: int | None = None) -> None:
        super().__init__("contract", code=code, status=status)


def _refuse_constant(name: str) -> NoReturn:
    raise ValueError(f"{name} is not JSON")


def _charset(content_type: str | None) -> str:
    """The `charset` of a `Content-Type` value when Python knows the codec, else UTF-8."""
    for parameter in (content_type or "").split(";")[1:]:
        name, _, value = parameter.strip().partition("=")
        if name.strip().lower() == "charset":
            try:
                return codecs.lookup(value.strip().strip('"')).name
            except LookupError:
                break
    return "utf-8"


@dataclass(frozen=True)
class RawPage:
    """One response: the status, the allowlisted headers (lower-case names), the body and its arrival time."""

    spec: RequestSpec
    status: int
    headers: Mapping[str, str]
    body: bytes
    fetched_at: datetime

    def json(self) -> JSONValue:
        """The body as JSON, parsed once; a body that is not JSON raises `ContractViolation("not_json")`."""
        return self._document

    def text(self) -> str:
        """The body decoded by its `content-type` charset (default UTF-8), undecodable bytes replaced."""
        return self.body.decode(_charset(self.headers.get("content-type")), errors="replace")

    @cached_property
    def _document(self) -> JSONValue:
        try:
            # NaN and the infinities are not JSON (RFC 8259) and jsonb refuses them; a deep nesting is a
            # `RecursionError` of the parser. Both are bodies that break the contract, not bugs.
            document: JSONValue = json.loads(self.body, parse_constant=_refuse_constant)
        except (ValueError, RecursionError) as exc:
            raise ContractViolation("not_json", status=self.status) from exc
        return document


class NormalizedPosting(BaseModel):
    """One parsed item (§5.1), before identity: what `parse_normalize` stores and identity resolves."""

    model_config = ConfigDict(frozen=True)

    native_id: str
    title_raw: str
    company_raw: str
    locations: list[Location]
    location_kind: LocationKind
    work_mode_possible: frozenset[str]  # non-empty subset of {remote, hybrid, onsite}; all three = unknown
    work_mode_source: Literal["structured", "text", "none"]
    remote_countries: tuple[str, ...]
    employment_types: tuple[EmploymentType, ...]
    employment_source: Literal["structured", "text", "none"]
    salary: Salary | None
    posted_at: datetime | None
    posted_precision: Precision
    posted_raw: str | None
    item_ts: datetime | None  # the source's own item timestamp, used for coverage
    description_text: str
    description_html_sanitized: str | None
    description_completeness: Literal["full", "partial"]
    description_quality: Quality
    ad_language_structured: str | None
    primary_link: str | None  # canonical (`canonical_url`)
    outbound_url_private: str | None
    publisher: str | None
    raw_keys: tuple[AdKey, ...]
    requisition_id: str | None
    source_structured: dict[str, JSONValue]


# The reason code of an item that breaks its contract (§5.1, §7.16.1): `Rejected(ITEM_CONTRACT)`.
ITEM_CONTRACT: Final = "item_contract"


def element_sha256(element: bytes) -> bytes:
    """The SHA-256 digest of one element's canonical bytes (`SourceAdapter.elements`): the
    `raw_items.item_sha256` of the element, and SmartRecruiters' list-item change marker."""
    return hashlib.sha256(element).digest()


# Item outcomes (§5.1): `parse` returns exactly one per array element, with `item_index` preserved.
@dataclass(frozen=True)
class Parsed:
    item_index: int
    native_id: str
    item_ts: datetime | None
    normalized: NormalizedPosting


@dataclass(frozen=True)
class OutOfScope:
    item_index: int
    native_id: str
    item_ts: datetime | None
    reason: str


@dataclass(frozen=True)
class DuplicateInPage:
    item_index: int
    native_id: str
    item_ts: datetime | None
    first_index: int


@dataclass(frozen=True)
class Rejected:
    item_index: int
    native_id: str | None
    item_ts: datetime | None
    reason_code: str
    detail: str


ItemOutcome = Parsed | OutOfScope | DuplicateInPage | Rejected


@dataclass(frozen=True)
class Observation:
    """What a response says about the provider's quota window (§6.3), with the time it was observed."""

    observed_at: datetime
    status: int | None
    reset_at: datetime | None = None
    limit: int | None = None
    remaining: int | None = None
    this_hour: int | None = None
    hourly_cap: int | None = None


# The settlement of one attempt (§5.3, §6.1): the ledger state and its evidence code.
Settlement = tuple[Literal["billed", "not_billed", "ambiguous"], str | None]


@dataclass(frozen=True)
class CellPlan:
    """What an adapter needs for the first page of a traversal; the planner builds it from its `CellRow`."""

    cell_id: int
    source_id: str
    instance_id: int
    query_text: str | None
    query_lang: str | None
    location_key: str | None
    location_label: str | None
    radius_km: int | None
    phase: Literal["primary", "fallback"]
    request_class: RequestClass
    bucket: Literal["today", "3days", "week", "month"] | None
    window: CoverageWindow
    max_days_old: int | None


class SourceAdapter(Protocol):
    """§5.1, with `plan(cell)` and `fetch -> HttpOutcome` of plan Deviation 25."""

    def capabilities(self) -> Capabilities: ...

    def plan(self, cell: CellPlan) -> RequestSpec:
        """The first page of the traversal, with `cell.window` copied into `RequestSpec.window`."""
        ...

    async def fetch(self, spec: RequestSpec) -> "HttpOutcome":
        """Exactly one HTTP attempt through `http.send` with the adapter's clock; never a retry (§5.1)."""
        ...

    def check_envelope(self, page: RawPage) -> None:
        """Raise `ContractViolation` for a response whose envelope breaks the contract; a paid source also
        raises the `SourceError` of the provider's own error report when it delivers one with a 2xx (T9-a)."""
        ...

    def peek_native_ids(self, page: RawPage) -> list[str | None]:
        """The native ids of the envelope's items, without normalisation."""
        ...

    def elements(self, page: RawPage) -> list[bytes]:
        """The canonical bytes of each element of the envelope's item array, in `item_index` order (ruling
        T15-a): `canonical_json` of a JSON element, the C14N 2.0 form of an XML one. It reads the envelope as
        `check_envelope` does, and raises what that raises."""
        ...

    def parse(self, page: RawPage) -> list[ItemOutcome]:
        """Exactly one outcome per element of `elements(page)`, the outcome of element `i` at index `i`."""
        ...

    def continuation(self, page: RawPage) -> tuple[Continuation, str | None]: ...

    def settle(self, outcome: "HttpOutcome") -> Settlement:
        """The ledger settlement of an attempt, by transport outcome and status (a no-op for free sources)."""
        ...

    def observation(self, page: RawPage) -> Observation | None:
        """The quota observation of a response (a no-op for free sources)."""
        ...

    def next_page(self, spec: RequestSpec, cursor: str | None) -> RequestSpec | None: ...
