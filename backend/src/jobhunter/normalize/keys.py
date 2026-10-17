"""Strong ad keys (spec §8.4): identifiers of one posting, read from its primary link or its API id.

A key is a namespace and a value. Every namespace but `htidocid` is exclusive: one job holds one value of it.
Keys come only from the primary link (§5.3) and from native identifiers, never from secondary apply options.
"""

import base64
import json
import re
from dataclasses import dataclass
from typing import Final
from urllib.parse import parse_qsl, unquote, urlsplit

from jobhunter.normalize.urls import canonical_url, host_is, is_adzuna_host, is_indeed_host


@dataclass(frozen=True)
class AdKey:
    """A strong key: a namespace (`pub:stepstone.de`, `ats:lever:eu:acme`, `htidocid`, ...) and a value."""

    ns: str
    value: str

    @property
    def exclusive(self) -> bool:
        """One job holds one value per exclusive namespace; a job may carry several `htidocid` values."""
        return self.ns != "htidocid"


_STEPSTONE: Final = re.compile(r"--(\d+)-inline\.html$")
_LINKEDIN: Final = re.compile(r"^/jobs/view/(?:[^/]*-)?(\d+)(?:/|$)")
_XING: Final = re.compile(r"^/jobs/[^/]*-(\d+)(?:/|$)")
_JOIN: Final = re.compile(r"^/companies/[^/]+/(\d+)-")
_ADZUNA: Final = re.compile(r"^/details/(\d+)(?:/|$)")
_ARBEITNOW: Final = re.compile(r"^/jobs/companies/[^/]+/[^/]*-(\d+)(?:/|$)")
_IT_JOBS_REFNR: Final = re.compile(r"_ers-([^/]+)(?:/|$)")

_UUID: Final = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
_GREENHOUSE: Final = re.compile(r"^/([^/]+)/jobs/(\d+)(?:/|$)")
_UUID_POSTING: Final = re.compile(rf"^/([^/]+)/({_UUID})(?:/|$)")
_PERSONIO_HOST: Final = re.compile(r"^([a-z0-9][a-z0-9-]*)\.jobs\.personio\.de$")
_PERSONIO: Final = re.compile(r"^/job/(\d+)(?:/|$)")
_SMARTRECRUITERS: Final = re.compile(r"^/([^/]+)/(\d+)(?:-[^/]*)?(?:/|$)")

# Lever has two regions, and the API identity of §5.3 is `ats:lever:{region}:{site}:{id}`.
_LEVER_REGIONS: Final = {"jobs.lever.co": "global", "jobs.eu.lever.co": "eu"}


def ats_key(ats_type: str, board: str, posting_id: str) -> AdKey:
    """The key of a posting of an ATS board: namespace `ats:<type>:<board>`."""
    return AdKey(f"ats:{ats_type}:{board}", posting_id)


def jsearch_uid_key(job_uid: str) -> AdKey:
    """JSearch's `job_uid` is the Google `htidocid` (never its `job_id`, which embeds a session blob)."""
    return AdKey("htidocid", job_uid)


def _b64decode(value: str) -> bytes:
    """Standard-alphabet base64 whose padding may be missing; a character outside the alphabet is an error."""
    return base64.b64decode(value + "=" * (-len(value) % 4), validate=True)


def decode_serpapi_job_id(job_id: str) -> str | None:
    """The `htidocid` of a SerpApi `job_id` (base64 of a JSON object); the `fc` session token is discarded."""
    try:
        payload = json.loads(_b64decode(job_id))
    except (ValueError, RecursionError):
        return None
    htidocid = payload.get("htidocid") if isinstance(payload, dict) else None
    return htidocid if isinstance(htidocid, str) and htidocid else None


def decode_ba_refnr(segment: str) -> str | None:
    """The BA reference number in an it-jobs.de link segment, or None unless it decodes to printable ASCII."""
    try:
        decoded = _b64decode(unquote(segment)).decode("ascii")
    except ValueError:
        return None
    return decoded if decoded and all(" " <= c <= "~" for c in decoded) else None


def _one(ns: str, pattern: re.Pattern[str], text: str) -> list[AdKey]:
    match = pattern.search(text)
    return [AdKey(ns, match[1])] if match else []


def _ats_from_path(ats_type: str, board_prefix: str, pattern: re.Pattern[str], path: str) -> list[AdKey]:
    match = pattern.search(path)
    return [ats_key(ats_type, f"{board_prefix}{match[1]}", match[2])] if match else []


def keys_from_link(primary_link: str) -> list[AdKey]:
    """The publisher and ATS keys of a primary link (§8.4), read from its canonical form.

    An unknown host, a link without an id and the Adzuna `/land/ad/` form yield no key.
    """
    canonical = canonical_url(primary_link)
    if not canonical.startswith(("http://", "https://")):
        return []
    try:
        parts = urlsplit(canonical)
    except ValueError:
        return []
    host, path = parts.hostname or "", parts.path
    if host_is(host, "stepstone.de"):
        return _one("pub:stepstone.de", _STEPSTONE, path)
    if host_is(host, "linkedin.com"):
        return _one("pub:linkedin.com", _LINKEDIN, path)
    if is_indeed_host(host):
        jk = dict(parse_qsl(parts.query)).get("jk")
        return [AdKey("pub:indeed", jk)] if jk else []
    if host_is(host, "xing.com"):
        return _one("pub:xing.com", _XING, path)
    if host_is(host, "join.com"):
        return _one("pub:join.com", _JOIN, path)
    if is_adzuna_host(host):
        return _one("adzuna", _ADZUNA, path)
    if host_is(host, "arbeitnow.com"):
        return _one("arbeitnow", _ARBEITNOW, path)
    if host_is(host, "it-jobs.de"):
        match = _IT_JOBS_REFNR.search(path)
        refnr = decode_ba_refnr(match[1]) if match else None
        return [AdKey("pub:ba_refnr", refnr)] if refnr else []
    if host in ("boards.greenhouse.io", "job-boards.greenhouse.io"):
        return _ats_from_path("greenhouse", "", _GREENHOUSE, path)
    if host in _LEVER_REGIONS:
        return _ats_from_path("lever", f"{_LEVER_REGIONS[host]}:", _UUID_POSTING, path)
    if host == "jobs.ashbyhq.com":
        return _ats_from_path("ashby", "", _UUID_POSTING, path)
    if host == "jobs.smartrecruiters.com":
        return _ats_from_path("smartrecruiters", "", _SMARTRECRUITERS, path)
    if (personio_host := _PERSONIO_HOST.match(host)) and (match := _PERSONIO.search(path)):
        return [ats_key("personio", personio_host[1], match[1])]
    return []
