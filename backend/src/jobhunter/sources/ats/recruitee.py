"""The Recruitee board adapter (spec §5.3.5).

Request: `GET https://{slug}.recruitee.com/api/offers/`, one page.

Envelope: an object with an `offers` array. Recruitee's own error report is `{"error": "..."}`.

Items: `native_id` is the offer `id` (an integer), the key `ats:recruitee:{slug}:{id}`; `published_at`
(`2026-10-15 09:31:36 UTC`) is the posted date (exact). The work mode is structured: the booleans `remote`,
`hybrid` and `on_site`; for an offer that allows remote work (remote or hybrid) `country_code` is the country
it is allowed in (`remote_countries`). The description is `description` then `requirements`, HTML, sanitised.
`employment_type_code` (`fulltime_permanent`, `parttime_fixed_term`, `internship`, ...) is the structured
employment type. The location is `city`, else `location`; the company is `company_name`.
"""

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Final

from jobhunter.sources.ats.common import (
    AtsAdapter,
    country_code,
    html_description,
    id_of,
    iso_posted,
    item_list,
    locations,
    object_envelope,
    rejected,
    structured_modes,
    text_of,
)
from jobhunter.sources.base import OutOfScope, Parsed, RawPage, Rejected, RequestSpec
from jobhunter.sources.parsing.employment import EmploymentType

SOURCE_ID: Final = "recruitee"
_MODES: Final = (("remote", "remote"), ("hybrid", "hybrid"), ("on_site", "onsite"))
# The words of `employment_type_code`, split on `_`.
_EMPLOYMENT: Final[Mapping[str, EmploymentType]] = MappingProxyType(
    {
        "fulltime": "full_time",
        "parttime": "part_time",
        "fixed": "temporary",  # `..._fixed_term`
        "temporary": "temporary",
        "internship": "internship",
        "freelance": "freelance",
        "apprenticeship": "apprenticeship",
    }
)


def _employment(code: str | None) -> tuple[EmploymentType, ...]:
    words = (code or "").casefold().split("_")
    return tuple(_EMPLOYMENT[word] for word in words if word in _EMPLOYMENT)


class RecruiteeAdapter(AtsAdapter):
    ats = SOURCE_ID

    @property
    def board(self) -> str:
        """The slug is a host name label, which is case-insensitive: the key uses its lower case."""
        return self.slug.lower()

    def _url(self, spec: RequestSpec) -> str:
        return f"https://{self.host}/api/offers/"

    def _items(self, page: RawPage) -> Sequence[object]:
        return item_list(object_envelope(page, "offers"), "offers", page)

    def _native_id(self, item: object) -> str | None:
        return id_of(item.get("id")) if isinstance(item, dict) else None

    def _parse_item(self, index: int, item: object) -> Parsed | OutOfScope | Rejected:
        if not isinstance(item, dict):
            return rejected(index, None, "the offer is not an object")
        native_id = self._native_id(item)
        if native_id is None:
            return rejected(index, None, "id is missing or not an id")
        title = item.get("title")
        if not isinstance(title, str):
            return rejected(index, native_id, "title is not a string")
        modes = structured_modes(mode for key, mode in _MODES if item.get(key) is True)
        country = country_code(item.get("country_code"))
        remote_capable = modes[1] == "structured" and bool(modes[0] & {"remote", "hybrid"})
        html = "".join(part for key in ("description", "requirements") if (part := text_of(item.get(key))))
        text, sanitized, _ = html_description(html)
        posting = self._posting(
            native_id,
            title=title,
            company=text_of(item.get("company_name")),
            places=locations([item.get("city") or item.get("location")], source=SOURCE_ID),
            work_modes=modes,
            remote_countries=(country,) if country is not None and remote_capable else (),
            employment=_employment(text_of(item.get("employment_type_code"))),
            posted=iso_posted(item.get("published_at")),
            description=(text, sanitized),
            link=text_of(item.get("careers_url")),
        )
        return Parsed(index, native_id, posting.item_ts, posting)
