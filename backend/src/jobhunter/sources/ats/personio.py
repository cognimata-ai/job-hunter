"""The Personio board adapter, over the XML feed (spec §5.3.5, §16.7).

Request: `GET https://{slug}.jobs.personio.de/xml`, one page. A redirect is followed only within the Personio
host family (`*.jobs.personio.de`, https; `http.send`).

Envelope: the XML document `<workzag-jobs>` with one `<position>` per posting, parsed with `defusedxml`
(§16.7): a document type declaration or an entity is refused (`ContractViolation("xml_entity")`) before
anything is expanded or fetched; a body that is no XML (an HTML page without a doctype, a truncated body) is
`not_xml`, and another root element breaks the contract (`root`). An HTML page with an HTML doctype is refused
as a DTD would be, and named `not_xml`. Personio reports no total.

Items: `native_id` is `<id>`, the key `ats:personio:{slug}:{id}`; `<createdAt>` is the posted date (exact).
The description is every `<jobDescriptions>/<jobDescription>`: its `<name>` as a heading and its `<value>`
(CDATA HTML), sanitised. Personio states no work mode (unknown). `<schedule>` and `<employmentType>` are the
structured employment types; `seniority`, `employmentType`, `yearsOfExperience` and `occupationCategory` are
kept in `source_structured` as Personio writes them. The locations are `<office>` and every
`<additionalOffices>/<office>`; the company is `<subcompany>`. The posting's page is
`https://{slug}.jobs.personio.de/job/{id}`, the link form whose key (§8.4) is the same as the board's.
An element of the page (`elements`, ruling T15-a) is the C14N 2.0 form of its `<position>`.
"""

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, cast
from xml.etree.ElementTree import canonicalize, tostring

from defusedxml import DefusedXmlException, DTDForbidden
from defusedxml.ElementTree import ParseError, fromstring

from jobhunter.sources.ats.common import (
    AtsAdapter,
    html_description,
    iso_posted,
    locations,
    rejected,
    section,
)
from jobhunter.sources.base import ContractViolation, OutOfScope, Parsed, RawPage, Rejected, RequestSpec
from jobhunter.sources.parsing.employment import EmploymentType
from jobhunter.types import JSONValue

if TYPE_CHECKING:
    from xml.etree.ElementTree import Element

SOURCE_ID: Final = "personio"
ROOT: Final = "workzag-jobs"
# The source_structured extras of §5.3.5, by element name.
STRUCTURED: Final = ("seniority", "employmentType", "yearsOfExperience", "occupationCategory")
_SCHEDULE: Final[Mapping[str, tuple[EmploymentType, ...]]] = MappingProxyType(
    {
        "full-time": ("full_time",),
        "part-time": ("part_time",),
        "full-or-part-time": ("full_time", "part_time"),
    }
)
_EMPLOYMENT: Final[Mapping[str, EmploymentType]] = MappingProxyType(
    {
        "intern": "internship",
        "freelance": "freelance",
        "temporary": "temporary",
        "working_student": "working_student",
    }
)


def _document(page: RawPage) -> "Element":
    """The `<workzag-jobs>` root of the page, parsed without a DTD, an entity or an external reference."""
    try:
        root = cast(
            "Element", fromstring(page.body, forbid_dtd=True, forbid_entities=True, forbid_external=True)
        )
    except DTDForbidden as exc:
        html = str(exc.name or "").casefold() == "html"
        raise ContractViolation("not_xml" if html else "xml_entity", status=page.status) from exc
    except DefusedXmlException as exc:
        raise ContractViolation("xml_entity", status=page.status) from exc
    except (ParseError, RecursionError) as exc:
        raise ContractViolation("not_xml", status=page.status) from exc
    if root.tag != ROOT:
        raise ContractViolation("root", status=page.status)
    return root


def _text(element: "Element", path: str) -> str | None:
    """The stripped text of the first element at `path`, `None` when it is absent or empty."""
    return (element.findtext(path) or "").strip() or None


class PersonioAdapter(AtsAdapter):
    ats = SOURCE_ID

    @property
    def board(self) -> str:
        """The slug is a host name label, which is case-insensitive: the key uses the lower case of links."""
        return self.slug.lower()

    def _url(self, spec: RequestSpec) -> str:
        return f"https://{self.host}/xml"

    def _items(self, page: RawPage) -> Sequence[object]:
        return _document(page).findall("position")

    def _native_id(self, item: object) -> str | None:
        return _text(cast("Element", item), "id")

    def _canonical(self, item: object) -> bytes:
        """The C14N 2.0 form of a `<position>` (ruling T15-a), UTF-8: the element alone, without the text that
        follows it."""
        return canonicalize(tostring(cast("Element", item), encoding="unicode")).encode("utf-8")

    def _parse_item(self, index: int, item: object) -> Parsed | OutOfScope | Rejected:
        position = cast("Element", item)
        native_id = self._native_id(position)
        if native_id is None:
            return rejected(index, None, "id is missing")
        title = _text(position, "name")
        if title is None:
            return rejected(index, native_id, "name is missing")
        blocks = [
            section(_text(entry, "name"), _text(entry, "value"))
            for entry in position.iterfind("jobDescriptions/jobDescription")
        ]
        text, sanitized, _ = html_description("".join(blocks))
        schedule = _SCHEDULE.get((_text(position, "schedule") or "").casefold(), ())
        kind = _EMPLOYMENT.get((_text(position, "employmentType") or "").casefold())
        extra = position.iterfind("additionalOffices/office")
        offices = [_text(position, "office"), *((office.text or "").strip() for office in extra)]
        structured: dict[str, JSONValue] = {
            name: value for name in STRUCTURED if (value := _text(position, name)) is not None
        }
        posting = self._posting(
            native_id,
            title=title,
            company=_text(position, "subcompany"),
            places=locations(offices, source=SOURCE_ID),
            employment=(*schedule, *((kind,) if kind else ())),
            posted=iso_posted(_text(position, "createdAt")),
            description=(text, sanitized),
            link=f"https://{self.host}/job/{native_id}"
            if native_id.isascii() and native_id.isdigit()
            else None,
            structured=structured,
        )
        return Parsed(index, native_id, posting.item_ts, posting)
