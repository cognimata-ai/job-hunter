"""Watchlist ATS detection by URL pattern only (spec §5.4), and the fixed API hosts of the boards (§5.3.5).

`detect` parses a careers URL against the §5.4 table and never fetches it. The host decides the ATS (case
insensitive); the slug is the first path segment (Greenhouse, Lever, Ashby, SmartRecruiters) or the board's
sub-domain (Personio, Recruitee). A trailing slash, the rest of the path, the query, the fragment and a port
are ignored, with two exceptions that read the table by its meaning rather than literally (fix wave F4,
ledger L151): the ATS's own site (`www.recruitee.com`, `www.jobs.personio.de`) names no board, and a
Greenhouse embed URL (`boards.greenhouse.io/embed/job_board?for=acme`) names its board in the `for`
parameter, not in the path (`embed` is no slug; without `for` the URL names none). The slug, the `for`
value included, is taken as written, never decoded or normalised, and must match `SLUG` as a whole
(`jobs.lever.co/../etc` and `%2e%2e` are `bad_slug`). Workable is recognised and unsupported in v1; any
other host, and a board host without a slug, is `unsupported_pattern` (the user then enters ATS and slug by
hand); a text that is not an http(s) URL with a host is `not_a_url`.

`API_HOSTS` is the compile-time allowlist of §5.4: the only hosts an ATS adapter or the validation ever
requests, with `{slug}` in the board sub-domain of Personio and Recruitee. No host is user-controlled: a
slug is admitted into a host only when it matches `SLUG`, so it stays a sub-domain of the ATS's own domain.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal, get_args
from urllib.parse import urlsplit

AtsType = Literal["greenhouse", "lever", "ashby", "personio", "recruitee", "smartrecruiters"]
LeverRegion = Literal["global", "eu"]
ATS_TYPES: Final[tuple[AtsType, ...]] = get_args(AtsType)
SLUG: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


@dataclass(frozen=True)
class Detected:
    ats: AtsType
    slug: str
    lever_region: LeverRegion | None


@dataclass(frozen=True)
class Unsupported:
    reason: Literal["workable", "unsupported_pattern", "bad_slug", "not_a_url"]


# §5.4: the hosts whose first path segment is the slug, with the ATS (and the Lever region) they name.
_PATH_HOSTS: Final[Mapping[str, tuple[AtsType, LeverRegion | None]]] = MappingProxyType(
    {
        "boards.greenhouse.io": ("greenhouse", None),
        "job-boards.greenhouse.io": ("greenhouse", None),
        "jobs.lever.co": ("lever", "global"),
        "jobs.eu.lever.co": ("lever", "eu"),
        "jobs.ashbyhq.com": ("ashby", None),
        "jobs.smartrecruiters.com": ("smartrecruiters", None),
        "careers.smartrecruiters.com": ("smartrecruiters", None),
    }
)
# §5.4: the hosts whose one sub-domain label is the slug.
_SUBDOMAIN_HOSTS: Final[tuple[tuple[re.Pattern[str], AtsType], ...]] = (
    (re.compile(r"([^.]+)\.jobs\.personio\.(?:de|com)"), "personio"),
    (re.compile(r"([^.]+)\.recruitee\.com"), "recruitee"),
)
# The sub-domains of the ATS's own site, never a board (`www.recruitee.com`).
_NOT_A_BOARD: Final = frozenset({"www"})
# Greenhouse's embed path (`/embed/job_board?for=<slug>`, `/embed/job_app?for=<slug>&token=...`): the board
# is the `for` parameter.
_GREENHOUSE_EMBED: Final = "embed"
_WORKABLE: Final = "apply.workable.com"
_WEB_SCHEMES: Final = frozenset({"http", "https"})

API_HOSTS: Final[Mapping[AtsType, tuple[str, ...]]] = MappingProxyType(
    {
        "greenhouse": ("boards-api.greenhouse.io",),
        "lever": ("api.lever.co", "api.eu.lever.co"),  # the `global` and the `eu` region
        "ashby": ("api.ashbyhq.com",),
        "personio": ("{slug}.jobs.personio.de",),
        "recruitee": ("{slug}.recruitee.com",),
        "smartrecruiters": ("api.smartrecruiters.com",),
    }
)
_LEVER_REGIONS: Final[tuple[LeverRegion, ...]] = get_args(LeverRegion)


def _board(slug: str, ats: AtsType, region: LeverRegion | None) -> Detected | Unsupported:
    if not slug:
        return Unsupported("unsupported_pattern")
    if SLUG.fullmatch(slug) is None:
        return Unsupported("bad_slug")
    return Detected(ats, slug, region)


def _query_value(query: str, name: str) -> str:
    """The value of the query's first `name` parameter as written, never decoded (`for=ac%6De` stays
    `ac%6De`, which `SLUG` refuses, as a path slug is never decoded either); `""` without one."""
    for pair in query.split("&"):
        key, _, value = pair.partition("=")
        if key == name:
            return value
    return ""


def detect(url: str) -> Detected | Unsupported:
    """The ATS and slug of a careers URL by the §5.4 table; the URL is never fetched."""
    try:
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").rstrip(".")
    except ValueError:
        return Unsupported("not_a_url")
    if parts.scheme not in _WEB_SCHEMES or not host:
        return Unsupported("not_a_url")
    if host == _WORKABLE:
        return Unsupported("workable")
    if (named := _PATH_HOSTS.get(host)) is not None:
        ats, region = named
        slug = parts.path.split("/")[1] if parts.path.startswith("/") else ""
        if ats == "greenhouse" and slug == _GREENHOUSE_EMBED:
            slug = _query_value(parts.query, "for")
        return _board(slug, ats, region)
    for pattern, ats in _SUBDOMAIN_HOSTS:
        if (match := pattern.fullmatch(host)) is not None:
            return _board("" if match[1] in _NOT_A_BOARD else match[1], ats, None)
    return Unsupported("unsupported_pattern")


def api_host(ats: AtsType, slug: str, lever_region: str | None) -> str:
    """The fixed API host of a board (`API_HOSTS`): Lever by its region, Personio and Recruitee with the slug
    as their sub-domain. Raises `ValueError` for a slug outside `SLUG` or a Lever board without its region."""
    if SLUG.fullmatch(slug) is None:
        raise ValueError(f"a board slug must match {SLUG.pattern}, got {slug!r}")
    hosts = API_HOSTS[ats]
    if ats != "lever":
        return hosts[0].format(slug=slug)
    if lever_region not in _LEVER_REGIONS:
        raise ValueError(f"a Lever board needs its region (global or eu), got {lever_region!r}")
    return hosts[_LEVER_REGIONS.index(lever_region)]
