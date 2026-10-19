"""The Ashby board adapter (spec §5.3.5).

Request: `GET https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true`, one page.

Envelope: an object with a `jobs` array. Ashby's own error report is `{"success": false, "error": "..."}`.

Items: `native_id` is the job `id`, the key `ats:ashby:{slug}:{id}`; `publishedAt` is the posted date (exact).
The work mode is structured: `workplaceType` (`Remote`, `Hybrid`, `OnSite`), and `isRemote: true` adds remote
work. The description is `descriptionPlain` (full), with `descriptionHtml` sanitised for display;
`employmentType` is the structured employment type. The locations are `location` and every
`secondaryLocations[].location`. Postings name no company: it is the board's.
"""

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Final

from jobhunter.normalize.html import sanitize_html
from jobhunter.sources.ats.common import (
    AtsAdapter,
    field,
    id_of,
    iso_posted,
    item_list,
    locations,
    object_envelope,
    plain_text,
    rejected,
    structured_modes,
    text_of,
)
from jobhunter.sources.base import OutOfScope, Parsed, RawPage, Rejected, RequestSpec
from jobhunter.sources.parsing.employment import EmploymentType

SOURCE_ID: Final = "ashby"
_WORKPLACE: Final[Mapping[str, str]] = MappingProxyType(
    {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite"}
)
_EMPLOYMENT: Final[Mapping[str, EmploymentType]] = MappingProxyType(
    {
        "fulltime": "full_time",
        "parttime": "part_time",
        "intern": "internship",
        "contract": "contract",
        "temporary": "temporary",
    }
)


class AshbyAdapter(AtsAdapter):
    ats = SOURCE_ID

    def _first_params(self) -> dict[str, str]:
        return {"includeCompensation": "true"}

    def _url(self, spec: RequestSpec) -> str:
        return f"https://{self.host}/posting-api/job-board/{self.slug}"

    def _items(self, page: RawPage) -> Sequence[object]:
        return item_list(object_envelope(page, "jobs"), "jobs", page)

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
        workplace = _WORKPLACE.get((text_of(item.get("workplaceType")) or "").casefold())
        stated = ([workplace] if workplace else []) + (["remote"] if item.get("isRemote") is True else [])
        secondary = item.get("secondaryLocations")
        labels = [item.get("location")]
        labels += [field(entry, "location") for entry in (secondary if isinstance(secondary, list) else [])]
        employment = _EMPLOYMENT.get((text_of(item.get("employmentType")) or "").casefold())
        html = text_of(item.get("descriptionHtml"))
        posting = self._posting(
            native_id,
            title=title,
            company=None,
            places=locations(labels, source=SOURCE_ID),
            work_modes=structured_modes(stated),
            employment=(employment,) if employment else (),
            posted=iso_posted(item.get("publishedAt")),
            description=(plain_text(item.get("descriptionPlain")), sanitize_html(html) if html else None),
            link=text_of(item.get("jobUrl")),
        )
        return Parsed(index, native_id, posting.item_ts, posting)
