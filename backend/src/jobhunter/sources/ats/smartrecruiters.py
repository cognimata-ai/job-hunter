"""The SmartRecruiters board adapter: list pages and detail calls (spec §5.3.5).

List: `GET https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100&offset=N` (operation
`list`), an object with a `content` array and the integers `totalFound`, `offset` and `limit`; the `offset`
it echoes must be the offset asked for (`offset_mismatch` otherwise). Pagination by offset: `END` when the
page is empty or `offset + len(content) >= totalFound`, otherwise the cursor `offset + limit`. `totalFound`
is the board's reported total of §5.3.5 rule 4 (`board_total`, ruling T11-c): every list page of a snapshot
must report the same one and the snapshot must have exactly that many members, so a short page mid-listing,
an empty page below the total or a total that moves makes the snapshot `incomplete(count_mismatch)` (with its
members kept), never a complete snapshot that counts live postings absent. SmartRecruiters' own error report
is `{"message": "..."}`.

Detail: `GET .../postings/{id}` (operation `detail`, `detail_native_id = id`, `parent_fetch_request_id` the
list page's fetch request), an object whose `id` must be the posting asked for. The posting is normalised from
its detail alone (the full description is in `jobAd.sections`). `detail_specs` plans a detail call for each
list item whose posting is new, whose list item changed (`list_item_sha256`, `element_sha256` of the
item's element, the sha256 of its canonical JSON: the `postings.list_item_sha256` marker) or whose last
detail call is older than `DETAIL_MAX_AGE`, at the `SMARTRECRUITERS_DETAIL` limit (concurrency 4, 5 per
second). Identity writes both markers when it resolves a detail observation: the list item's
`raw_items.item_sha256` (the same `element_sha256`) and the detail's `fetched_at` (`dedup/identity.py`).
A list item positively outside Germany and not remote is `OutOfScope("not_germany")` and gets no
detail call: its structured `location.country` is another country's code (without one, every place
`location.fullLocation` names lies outside Germany, §8.12). Every other list item is
`OutOfScope("detail_call")`: the list page is not where it is normalised.

Items: `native_id` is the posting `id`, the key `ats:smartrecruiters:{slug}:{id}`; `releasedDate` is the
posted date (exact). The work mode is structured: `location.remote`, `location.hybrid`, on site otherwise.
The location is the posting's coordinates when it has them (§8.12), else its `fullLocation` text.
`typeOfEmployment.label` is the employment type and `experienceLevel.id` the structured seniority; the
structured language is `language.code` (`en`, `es-MX`) and the industry `industry.label` (`Internet`), the
§8.8 rows of `ad_language` and `industry_structured` (carry from Task 11, fix wave F1).
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Final

from jobhunter.sources.ats.common import (
    AtsAdapter,
    country_code,
    envelope_int,
    field,
    html_description,
    id_of,
    iso_posted,
    item_list,
    locations,
    object_envelope,
    provider_error,
    rejected,
    section,
    structured_modes,
    text_of,
)
from jobhunter.sources.base import (
    Continuation,
    ContractViolation,
    ItemOutcome,
    OutOfScope,
    Parsed,
    RateLimit,
    RawPage,
    Rejected,
    RequestSpec,
    element_sha256,
)
from jobhunter.sources.parsing.employment import parse_employment
from jobhunter.sources.parsing.locations import GERMANY, ParsedLocations, location_from_coordinates
from jobhunter.sources.ratelimit import ATS_HOST_LIMIT, SMARTRECRUITERS_DETAIL
from jobhunter.types import JSONValue

SOURCE_ID: Final = "smartrecruiters"
PAGE_SIZE: Final = 100
DETAIL_MAX_AGE: Final = timedelta(days=7)
DETAIL_LIMIT: Final = SMARTRECRUITERS_DETAIL
NOT_GERMANY: Final = "not_germany"
DETAIL_CALL: Final = "detail_call"  # a list item in scope: its posting is normalised from its detail
# A posting id that is safe as a path segment of the detail URL (SmartRecruiters ids are digits).
_SAFE_ID: Final = re.compile(r"[A-Za-z0-9_-]{1,100}")
_JOB_AD_SECTIONS: Final = ("companyDescription", "jobDescription", "qualifications", "additionalInformation")


def _offset(value: str) -> int:
    if not (value.isascii() and value.isdigit()):
        raise ValueError(f"a SmartRecruiters offset is a non-negative integer, got {value!r}")
    return int(value)


def _outside_germany(item: dict[str, JSONValue]) -> bool:
    """Positive evidence that a list item is outside Germany and not remote (§5.3.5, §8.12)."""
    location = item.get("location")
    if not isinstance(location, dict) or location.get("remote") is True:
        return False
    if (code := country_code(location.get("country"))) is not None:
        return code != GERMANY
    label = location.get("fullLocation") or location.get("city")
    return locations([label], source=SOURCE_ID).positively_non_de


def _work_modes(location: JSONValue) -> list[str]:
    """`location.remote` and `location.hybrid`; a stated `remote: false` without hybrid is on site."""
    stated = [mode for mode in ("remote", "hybrid") if field(location, mode) is True]
    return stated or (["onsite"] if field(location, "remote") is False else [])


def _coordinate(value: JSONValue) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _places(location: JSONValue) -> ParsedLocations:
    """The coordinates of the posting when it has a valid pair, else its location text (§8.12)."""
    label = text_of(field(location, "fullLocation")) or text_of(field(location, "city"))
    lat, lon = _coordinate(field(location, "latitude")), _coordinate(field(location, "longitude"))
    if label is not None and lat is not None and lon is not None:
        try:
            return ParsedLocations(
                [location_from_coordinates(label, lat, lon, source=SOURCE_ID)], "city", False
            )
        except ValueError:  # not a coordinate pair: the text says what it can
            pass
    return locations([label], source=SOURCE_ID)


def _description(job_ad: JSONValue) -> tuple[str, str | None]:
    sections = field(job_ad, "sections")
    blocks = [
        section(text_of(field(part, "title")), text_of(field(part, "text")))
        for part in (field(sections, name) for name in _JOB_AD_SECTIONS)
    ]
    text, sanitized, _ = html_description("".join(blocks))
    return text, sanitized


class SmartRecruitersAdapter(AtsAdapter):
    ats = SOURCE_ID
    pagination = "offset"
    page_size = PAGE_SIZE
    description = "detail_call"

    def rate_limits(self) -> list[RateLimit]:
        return [ATS_HOST_LIMIT, DETAIL_LIMIT]

    def _first_params(self) -> dict[str, str]:
        return {"limit": str(PAGE_SIZE), "offset": "0"}

    def _url(self, spec: RequestSpec) -> str:
        listing = f"https://{self.host}/v1/companies/{self.slug}/postings"
        if spec.operation == "list":
            return listing
        if spec.detail_native_id is None or _SAFE_ID.fullmatch(spec.detail_native_id) is None:
            raise ValueError(
                f"a detail call needs a posting id that is a path segment, got {spec.detail_native_id!r}"
            )
        return f"{listing}/{spec.detail_native_id}"

    # -- envelopes

    def _listing(self, page: RawPage) -> tuple[list[JSONValue], int, int, int]:
        """`content`, `totalFound`, `offset` and `limit` of a list page."""
        doc = object_envelope(page, "content", error_key="message")
        content = item_list(doc, "content", page)
        total, offset, limit = (envelope_int(doc, key, page) for key in ("totalFound", "offset", "limit"))
        if limit < 1:
            raise ContractViolation("limit_invalid", status=page.status)
        # The API's default offset is 0, the offset of a spec that names none.
        if offset != _offset(page.spec.params.get("offset", "0")):
            raise ContractViolation("offset_mismatch", status=page.status)
        return content, total, offset, limit

    def _detail(self, page: RawPage) -> tuple[dict[str, JSONValue], str]:
        """The posting of a detail page and its id, which must be the posting the spec asked for."""
        doc = page.json()
        if not isinstance(doc, dict):
            raise ContractViolation("not_object", status=page.status)
        native_id = id_of(doc.get("id"))
        if native_id is None:
            if isinstance(message := doc.get("message"), str) and message.strip():
                raise provider_error(page)
            raise ContractViolation("id_missing", status=page.status)
        asked = page.spec.detail_native_id
        if asked is not None and native_id != asked:
            raise ContractViolation("detail_id_mismatch", status=page.status)
        return doc, native_id

    def _items(self, page: RawPage) -> Sequence[object]:
        if page.spec.operation == "detail":
            return [self._detail(page)[0]]
        return self._listing(page)[0]

    def _native_id(self, item: object) -> str | None:
        return id_of(item.get("id")) if isinstance(item, dict) else None

    # -- pagination

    def board_total(self, page: RawPage) -> int | None:
        """`totalFound`: the number of postings the board holds, as this list page reports it."""
        if page.spec.operation == "detail":
            self._detail(page)
            return None
        return self._listing(page)[1]

    def continuation(self, page: RawPage) -> tuple[Continuation, str | None]:
        """`END` for an empty page or `offset + len >= totalFound`, otherwise the next offset; a detail page
        is not part of the pagination and ends there. An empty page below `totalFound` ends the board too: it
        has no more to give, and `membership` finds its snapshot short of the total."""
        if page.spec.operation == "detail":
            self._detail(page)
            return Continuation.END, None
        content, total, offset, limit = self._listing(page)
        if not content or offset + len(content) >= total:
            return Continuation.END, None
        return Continuation.CURSOR, str(offset + limit)

    def next_page(self, spec: RequestSpec, cursor: str | None) -> RequestSpec | None:
        """The list page at offset `cursor`, in the same class."""
        if not cursor or spec.operation != "list":
            return None
        return replace(spec, page_no=spec.page_no + 1, params={**spec.params, "offset": str(_offset(cursor))})

    # -- detail calls

    def list_item_sha256(self, page: RawPage, index: int) -> bytes:
        """The change marker of list item `index`'s posting: `element_sha256` of its element, equal to
        `element_sha256(elements(page)[index])` (one helper, `_canonical`, makes both)."""
        return element_sha256(self._canonical(self._listing(page)[0][index]))

    def detail_specs(
        self,
        page: RawPage,
        known: Mapping[str, tuple[bytes, datetime | None]],
        *,
        now: datetime,
        parent_fetch_request_id: int | None = None,
    ) -> list[RequestSpec]:
        """The detail calls a list page needs, in list order: one per posting in scope that is new, whose
        list item changed, or whose last detail call (`known[id]`: the stored list-item sha256 and the
        time of the last detail) is older than `DETAIL_MAX_AGE`. `parent_fetch_request_id` is the list page's
        fetch request."""
        specs: list[RequestSpec] = []
        seen: set[str] = set()
        for item in self._listing(page)[0]:
            native_id = self._native_id(item)
            if native_id is None or native_id in seen or _SAFE_ID.fullmatch(native_id) is None:
                continue
            seen.add(native_id)
            if not isinstance(item, dict) or _outside_germany(item):
                continue
            stored = known.get(native_id)
            if stored is not None:
                sha256, detailed_at = stored
                fresh = detailed_at is not None and now - detailed_at <= DETAIL_MAX_AGE
                if sha256 == element_sha256(self._canonical(item)) and fresh:
                    continue
            specs.append(
                replace(
                    page.spec,
                    operation="detail",
                    params={},
                    cursor_in=None,
                    detail_native_id=native_id,
                    parent_fetch_request_id=parent_fetch_request_id,
                )
            )
        return specs

    # -- items

    def _parse_item(self, index: int, item: object) -> Parsed | OutOfScope | Rejected:
        """A list item: `OutOfScope`, as not Germany or as the detail call's (`DETAIL_CALL`)."""
        if not isinstance(item, dict):
            return rejected(index, None, "the posting is not an object")
        native_id = self._native_id(item)
        if native_id is None:
            return rejected(index, None, "id is missing or not an id")
        if not isinstance(item.get("name"), str):
            return rejected(index, native_id, "name is not a string")
        posted = iso_posted(item.get("releasedDate"))
        released = None if posted is None else posted.at
        return OutOfScope(index, native_id, released, NOT_GERMANY if _outside_germany(item) else DETAIL_CALL)

    def parse(self, page: RawPage) -> list[ItemOutcome]:
        if page.spec.operation == "detail":
            return [self.parse_detail(page)]
        return super().parse(page)

    def parse_detail(self, page: RawPage) -> ItemOutcome:
        """The posting of a detail page."""
        job, native_id = self._detail(page)
        title = job.get("name")
        if not isinstance(title, str):
            return rejected(0, native_id, "name is not a string")
        location = job.get("location")
        employment = text_of(field(job.get("typeOfEmployment"), "label"))
        structured: dict[str, JSONValue] = {
            key: value
            for key, value in (
                ("seniority", text_of(field(job.get("experienceLevel"), "id"))),
                ("language", text_of(field(job.get("language"), "code"))),
                ("industry", text_of(field(job.get("industry"), "label"))),
            )
            if value is not None
        }
        posting = self._posting(
            native_id,
            title=title,
            company=text_of(field(job.get("company"), "name")),
            places=_places(location),
            work_modes=structured_modes(_work_modes(location)),
            employment=parse_employment(employment) if employment else (),
            posted=iso_posted(job.get("releasedDate")),
            description=_description(job.get("jobAd")),
            link=text_of(job.get("postingUrl")),
            structured=structured,
        )
        return Parsed(0, native_id, posting.item_ts, posting)
