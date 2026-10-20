"""URL canonicalisation (spec §8.5): one stable spelling of a job link, with its tracking noise removed."""

import re
from typing import Final
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from jobhunter.normalize.text import norm_text

# `utm_` is a prefix (`utm_source`, `utm_medium`, ...); the others are exact parameter names.
TRACKING_PARAMS: Final = ("utm_", "gclid", "fbclid", "msclkid", "_hsenc", "_hsmi", "mc_cid", "mc_eid")
_TRACKING_PREFIXES: Final = tuple(p for p in TRACKING_PARAMS if p.endswith("_"))
_TRACKING_NAMES: Final = frozenset(p for p in TRACKING_PARAMS if not p.endswith("_"))

# Hosts that identify a posting by its path alone: the query is dropped.
_PATH_ONLY_SUFFIXES: Final = ("stepstone.de", "xing.com", "join.com", "arbeitnow.com")
# ATS hosts (§8.4): the query is dropped, the path is kept.
_ATS_HOSTS: Final = frozenset(
    {
        "boards.greenhouse.io",
        "job-boards.greenhouse.io",
        "jobs.lever.co",
        "jobs.eu.lever.co",
        "jobs.ashbyhq.com",
        "jobs.smartrecruiters.com",
    }
)
_ATS_SUFFIXES: Final = (".jobs.personio.de",)
_ADZUNA_PARAMS: Final = frozenset({"se", "v"})
_DEFAULT_PORTS: Final = {"http": 80, "https": 443}


def host_is(host: str, name: str) -> bool:
    """Whether `host` is `name` or one of its sub-domains (`de.linkedin.com` is on `linkedin.com`)."""
    return host == name or host.endswith("." + name)


def _brand_pattern(brand: str) -> re.Pattern[str]:
    # `<brand>.<tld>`, `<brand>.co.<cc>` or `<brand>.com.<cc>`, with any sub-domain in front.
    return re.compile(rf"(?:^|\.){brand}\.(?:(?:co|com)\.[a-z]{{2}}|[a-z]{{2,3}})$")


_INDEED: Final = _brand_pattern("indeed")
_ADZUNA: Final = _brand_pattern("adzuna")


def is_indeed_host(host: str) -> bool:
    """`indeed.*`: indeed.com, de.indeed.com, indeed.co.uk, ..."""
    return _INDEED.search(host) is not None


def is_adzuna_host(host: str) -> bool:
    """`adzuna.*`: adzuna.de, adzuna.co.uk, adzuna.com.au, ..."""
    return _ADZUNA.search(host) is not None


def _is_tracking(name: str) -> bool:
    lowered = name.lower()
    return lowered in _TRACKING_NAMES or lowered.startswith(_TRACKING_PREFIXES)


def _query(host: str, path: str, raw_query: str) -> str:
    pairs = [(k, v) for k, v in parse_qsl(raw_query, keep_blank_values=True) if not _is_tracking(k)]
    if (
        any(host_is(host, name) for name in _PATH_ONLY_SUFFIXES)
        or host in _ATS_HOSTS
        or host.endswith(_ATS_SUFFIXES)
        or (host_is(host, "linkedin.com") and path.startswith("/jobs/view/"))
    ):
        return ""
    if is_indeed_host(host):
        pairs = [(k, v) for k, v in pairs if k == "jk"]
    elif is_adzuna_host(host):
        pairs = [(k, v) for k, v in pairs if k not in _ADZUNA_PARAMS]
    return urlencode(sorted(pairs))


def canonical_url(u: str) -> str:
    """The canonical form of an `http(s)` URL (§8.5 steps 1-3); other values come back as `norm_text(u)`."""
    text = norm_text(u)
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        return text
    scheme, host = parts.scheme, (parts.hostname or "").removeprefix("www.")
    if scheme not in _DEFAULT_PORTS or not host:
        return text
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != _DEFAULT_PORTS[scheme]:
        netloc = f"{netloc}:{port}"
    path = parts.path.rstrip("/")
    return urlunsplit((scheme, netloc, path, _query(host, path, parts.query), ""))
