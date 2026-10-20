"""The Greenhouse board adapter (spec §5.3.5).

Request: `GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true`, one page with every job.

Envelope: an object with a `jobs` array and `meta.total`, an integer that must equal the number of jobs
(rule 4 of §5.3.5: otherwise the snapshot is `incomplete(count_mismatch)`). Greenhouse's own error report is
`{"status": 404, "error": "..."}`.

Items: `native_id` is the job `id` (an integer), the key `ats:greenhouse:{slug}:{id}`; `first_published` is
the posted date (exact). `content` arrives HTML-escaped: it is unescaped once, then sanitised. The location
is the job's `location.name` with its `offices[].name`; Greenhouse states no work mode (unknown).
`requisition_id` is kept as a probable-duplicate signal only, never a key (§8.4), and in `source_structured`
with the job's `language`.
"""

from collections.abc import Sequence
from typing import Final

from jobhunter.normalize.html import unescape_html
from jobhunter.sources.ats.common import (
    AtsAdapter,
    envelope_int,
    field,
    html_description,
    id_of,
    iso_posted,
    item_list,
    locations,
    object_envelope,
    rejected,
    text_of,
)
from jobhunter.sources.base import ContractViolation, OutOfScope, Parsed, RawPage, Rejected, RequestSpec
from jobhunter.types import JSONValue

SOURCE_ID: Final = "greenhouse"


class GreenhouseAdapter(AtsAdapter):
    ats = SOURCE_ID

    def _first_params(self) -> dict[str, str]:
        return {"content": "true"}

    def _url(self, spec: RequestSpec) -> str:
        return f"https://{self.host}/v1/boards/{self.slug}/jobs"

    def _envelope(self, page: RawPage) -> tuple[list[JSONValue], int]:
        doc = object_envelope(page, "jobs")
        jobs = item_list(doc, "jobs", page)
        meta = doc.get("meta")
        if not isinstance(meta, dict):
            raise ContractViolation("meta_missing", status=page.status)
        return jobs, envelope_int(meta, "total", page)

    def _items(self, page: RawPage) -> Sequence[object]:
        return self._envelope(page)[0]

    def reported_total(self, page: RawPage) -> int | None:
        return self._envelope(page)[1]

    def _native_id(self, item: object) -> str | None:
        return id_of(item.get("id")) if isinstance(item, dict) else None

    def _parse_item(self, index: int, item: object) -> Parsed | OutOfScope | Rejected:
        if not isinstance(item, dict):
            return rejected(index, None, "the job is not an object")
        native_id = self._native_id(item)
        if native_id is None:
            return rejected(index, None, "id is missing or not an id")
        title = item.get("title")
        if not isinstance(title, str):
            return rejected(index, native_id, "title is not a string")
        posted = iso_posted(item.get("first_published"))
        content = item.get("content")
        text, sanitized, _ = html_description(unescape_html(content) if isinstance(content, str) else "")
        offices = item.get("offices")
        labels = [field(item.get("location"), "name")]
        labels += [field(office, "name") for office in (offices if isinstance(offices, list) else [])]
        requisition = text_of(item.get("requisition_id"))
        language = text_of(item.get("language"))
        structured: dict[str, JSONValue] = {}
        if requisition is not None:
            structured["requisition_id"] = requisition
        if language is not None:
            structured["language"] = language
        posting = self._posting(
            native_id,
            title=title,
            company=text_of(item.get("company_name")),
            places=locations(labels, source=SOURCE_ID),
            posted=posted,
            description=(text, sanitized),
            link=text_of(item.get("absolute_url")),
            requisition_id=requisition,
            structured=structured,
        )
        return Parsed(index, native_id, posting.item_ts, posting)
