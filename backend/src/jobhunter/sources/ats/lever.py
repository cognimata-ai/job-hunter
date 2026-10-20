"""The Lever board adapter, global and EU (spec §5.3.5).

Request: `GET https://api.lever.co/v0/postings/{site}?mode=json&skip=N&limit=100`, or `api.eu.lever.co` for a
site of the EU region. `skip` starts at 0 and is in the spec's parameters, so every page is its own request.

Envelope: an array of postings. Lever's own error report is an object `{"ok": false, "error": "..."}`.
Pagination by offset: a page of fewer than `PAGE_SIZE` postings is the last (`END`); a full page continues
with `skip + PAGE_SIZE` (the cursor). Lever reports no total.

Items: `native_id` is the posting `id`, the key `ats:lever:{region}:{site}:{id}`; `createdAt` (epoch
milliseconds) is the posted date (exact). `workplaceType` (`remote`, `hybrid`, `onsite`) is the structured
work mode, `unspecified` says nothing; for a remote posting the posting's `country` is where remote work is
allowed (`remote_countries`). The description is `descriptionPlain`, then each of `lists` (its heading and its
items), then `additional`. `categories.commitment` is free text for the employment type; the locations are
`categories.allLocations`, else `categories.location`. Postings name no company: it is the board's.
"""

from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime
from typing import Final, Literal

import httpx

from jobhunter.normalize.html import html_to_text, sanitize_html
from jobhunter.sources.ats.common import (
    AtsAdapter,
    country_code,
    field,
    id_of,
    locations,
    plain_text,
    provider_error,
    rejected,
    section,
    structured_modes,
    text_of,
)
from jobhunter.sources.base import (
    Continuation,
    ContractViolation,
    OutOfScope,
    Parsed,
    RawPage,
    Rejected,
    RequestSpec,
)
from jobhunter.sources.parsing.dates import ParsedDate, from_epoch
from jobhunter.sources.parsing.employment import parse_employment
from jobhunter.types import JSONValue

SOURCE_ID: Final = "lever"
PAGE_SIZE: Final = 100
_WORKPLACE: Final = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite", "on-site": "onsite"}


def _offset(value: str) -> int:
    if not (value.isascii() and value.isdigit()):
        raise ValueError(f"a Lever offset is a non-negative integer, got {value!r}")
    return int(value)


def _created(value: JSONValue) -> ParsedDate | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        return from_epoch(value, millis=True, precision="exact")
    except ValueError:  # outside the calendar: the date is unknown, the posting is not rejected
        return None


def _description(item: dict[str, JSONValue]) -> tuple[str, str | None]:
    """`descriptionPlain`, the `lists` and `additional`: the plain text, and all three as sanitised HTML."""
    text_parts = [plain_text(item.get("descriptionPlain"))]
    html_parts = [text_of(item.get("description")) or ""]
    lists = item.get("lists")
    for entry in lists if isinstance(lists, list) else []:
        heading, content = text_of(field(entry, "text")), text_of(field(entry, "content"))
        block = section(heading, f"<ul>{content}</ul>" if content else None)
        html_parts.append(block)
        text_parts.append(html_to_text(sanitize_html(block)))
    additional = text_of(item.get("additional"))
    if additional is not None:
        html_parts.append(additional)
        text_parts.append(html_to_text(sanitize_html(additional)))
    text = "\n".join(part for part in text_parts if part)
    html = "".join(html_parts)
    return text, sanitize_html(html) if html.strip() else None


class LeverAdapter(AtsAdapter):
    ats = SOURCE_ID
    pagination = "offset"
    page_size = PAGE_SIZE

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        instance_id: int,
        slug: str,
        region: Literal["global", "eu"],
        clock: Callable[[], datetime],
        company: str | None = None,
    ) -> None:
        self.region = region
        super().__init__(client, instance_id=instance_id, slug=slug, clock=clock, company=company)

    @property
    def board(self) -> str:
        return f"{self.region}:{self.slug}"

    def _first_params(self) -> dict[str, str]:
        return {"mode": "json", "skip": "0", "limit": str(PAGE_SIZE)}

    def _url(self, spec: RequestSpec) -> str:
        return f"https://{self.host}/v0/postings/{self.slug}"

    def _items(self, page: RawPage) -> Sequence[object]:
        doc = page.json()
        if isinstance(doc, list):
            return doc
        if isinstance(doc, dict) and isinstance(message := doc.get("error"), str) and message.strip():
            raise provider_error(page)
        raise ContractViolation("not_list", status=page.status)

    def continuation(self, page: RawPage) -> tuple[Continuation, str | None]:
        """`END` below a full page; otherwise the offset of the next page."""
        if len(self._items(page)) < PAGE_SIZE:
            return Continuation.END, None
        return Continuation.CURSOR, str(_offset(page.spec.params.get("skip", "0")) + PAGE_SIZE)

    def next_page(self, spec: RequestSpec, cursor: str | None) -> RequestSpec | None:
        """The page at offset `cursor`, in the same class."""
        if not cursor:
            return None
        return replace(spec, page_no=spec.page_no + 1, params={**spec.params, "skip": str(_offset(cursor))})

    def _native_id(self, item: object) -> str | None:
        return id_of(item.get("id")) if isinstance(item, dict) else None

    def _parse_item(self, index: int, item: object) -> Parsed | OutOfScope | Rejected:
        if not isinstance(item, dict):
            return rejected(index, None, "the posting is not an object")
        native_id = self._native_id(item)
        if native_id is None:
            return rejected(index, None, "id is missing or not an id")
        title = item.get("text")
        if not isinstance(title, str):
            return rejected(index, native_id, "text is not a string")
        categories = item.get("categories")
        every = field(categories, "allLocations")
        labels = every if isinstance(every, list) and every else [field(categories, "location")]
        workplace = (text_of(item.get("workplaceType")) or "").casefold()
        modes = structured_modes([_WORKPLACE[workplace]] if workplace in _WORKPLACE else [])
        country = country_code(item.get("country"))
        remote = modes[1] == "structured" and "remote" in modes[0]  # stated, not merely unknown
        commitment = text_of(field(categories, "commitment"))
        posting = self._posting(
            native_id,
            title=title,
            company=None,
            places=locations(labels, source=SOURCE_ID),
            work_modes=modes,
            remote_countries=(country,) if country is not None and remote else (),
            employment=parse_employment(commitment) if commitment else (),
            employment_source="text",
            posted=_created(item.get("createdAt")),
            description=_description(item),
            link=text_of(item.get("hostedUrl")),
        )
        return Parsed(index, native_id, posting.item_ts, posting)
