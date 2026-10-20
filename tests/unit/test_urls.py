from urllib.parse import parse_qsl, urlencode, urlsplit

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from jobhunter.normalize.urls import TRACKING_PARAMS, canonical_url


def test_per_host_rules() -> None:
    assert (
        canonical_url(
            "https://www.stepstone.de/stellenangebote--X--14607704-inline.html?utm_source=a&rltr=1#top"
        )
        == "https://stepstone.de/stellenangebote--X--14607704-inline.html"
    )
    assert (
        canonical_url("https://de.linkedin.com/jobs/view/senior-x-4480111992?trk=a")
        == "https://de.linkedin.com/jobs/view/senior-x-4480111992"
    )
    assert (
        canonical_url("https://de.indeed.com/viewjob?jk=abc123&from=serp")
        == "https://de.indeed.com/viewjob?jk=abc123"
    )
    assert (
        canonical_url("https://www.adzuna.de/details/5926833612?se=XYZ&v=123&utm_medium=api")
        == "https://adzuna.de/details/5926833612"
    )
    assert canonical_url("https://example.com/b?z=1&a=2&gclid=9") == "https://example.com/b?a=2&z=1"
    assert canonical_url("https://www.it-jobs.de/x_ers-MTIw/") == "https://it-jobs.de/x_ers-MTIw"


def test_tracking_parameter_list() -> None:
    assert TRACKING_PARAMS == ("utm_", "gclid", "fbclid", "msclkid", "_hsenc", "_hsmi", "mc_cid", "mc_eid")


@pytest.mark.parametrize(
    ("url", "canonical"),
    [
        # 1. Lowercase scheme and host, drop www., the fragment and the trailing slash.
        ("HTTPS://WWW.Example.COM/Path/Case?A=1", "https://example.com/Path/Case?A=1"),
        ("http://example.com/a/", "http://example.com/a"),
        ("https://example.com/", "https://example.com"),
        ("https://example.com", "https://example.com"),
        ("https://example.com/a#frag", "https://example.com/a"),
        ("https://example.com/a?x=1#frag", "https://example.com/a?x=1"),
        ("https://www.www.example.com/a", "https://www.example.com/a"),
        ("https://sub.www.example.com/a", "https://sub.www.example.com/a"),
        ("https://example.com:8443/a", "https://example.com:8443/a"),
        ("https://example.com:443/a", "https://example.com/a"),
        ("http://example.com:80/a", "http://example.com/a"),
        ("https://user:secret@example.com/a", "https://example.com/a"),
        ("  https://example.com/a  ", "https://example.com/a"),
        # 2. Tracking parameters are removed everywhere, whatever their case.
        ("https://example.com/a?utm_source=x&utm_medium=y&id=7", "https://example.com/a?id=7"),
        ("https://example.com/a?UTM_Campaign=x&id=7", "https://example.com/a?id=7"),
        (
            "https://example.com/a?gclid=1&fbclid=2&msclkid=3&_hsenc=4&_hsmi=5&mc_cid=6&mc_eid=7",
            "https://example.com/a",
        ),
        ("https://example.com/a?utm_source=x", "https://example.com/a"),
        ("https://example.com/a?utmx=1&gclidx=2", "https://example.com/a?gclidx=2&utmx=1"),
        # Other hosts: the rest is sorted by name, then value; blank values are kept.
        ("https://example.com/a?b=&a=1&a=0", "https://example.com/a?a=0&a=1&b="),
        ("https://example.com/a?q=a%20b&r=c+d", "https://example.com/a?q=a+b&r=c+d"),
        # The Adzuna parameters `se` and `v` are removed on Adzuna hosts only.
        ("https://example.com/a?se=1&v=2", "https://example.com/a?se=1&v=2"),
        ("https://www.adzuna.co.uk/details/123?se=1&v=2&x=3", "https://adzuna.co.uk/details/123?x=3"),
        ("https://www.adzuna.de/land/ad/5926833612?se=x&v=1", "https://adzuna.de/land/ad/5926833612"),
        # stepstone.de, xing.com, join.com, arbeitnow.com: no query.
        (
            "https://stepstone.de/stellenangebote--A--1-inline.html?x=1",
            "https://stepstone.de/stellenangebote--A--1-inline.html",
        ),
        (
            "https://www.xing.com/jobs/berlin-software-158579394?sc_o=x",
            "https://xing.com/jobs/berlin-software-158579394",
        ),
        (
            "https://join.com/companies/roboservice/16831124-robotics?pid=x",
            "https://join.com/companies/roboservice/16831124-robotics",
        ),
        (
            "https://www.arbeitnow.com/jobs/companies/x-gmbh/engineer-194025?ref=y",
            "https://arbeitnow.com/jobs/companies/x-gmbh/engineer-194025",
        ),
        # linkedin.com: a job view keeps its path and drops the query, whatever the sub-domain.
        (
            "https://www.linkedin.com/jobs/view/4480111992/?trk=x&refId=y",
            "https://linkedin.com/jobs/view/4480111992",
        ),
        (
            "https://www.linkedin.com/jobs/search/?currentJobId=4480111992&utm_source=x",
            "https://linkedin.com/jobs/search?currentJobId=4480111992",
        ),
        # indeed.*: only jk.
        (
            "https://www.indeed.com/viewjob?from=serp&jk=abc123&utm_source=x",
            "https://indeed.com/viewjob?jk=abc123",
        ),
        ("https://uk.indeed.com/viewjob?jk=abc123", "https://uk.indeed.com/viewjob?jk=abc123"),
        ("https://www.indeed.co.uk/rc/clk?jk=abc123&fccid=9", "https://indeed.co.uk/rc/clk?jk=abc123"),
        ("https://de.indeed.com/jobs?q=python&l=berlin", "https://de.indeed.com/jobs"),
        ("https://notindeed.com/viewjob?jk=1&x=2", "https://notindeed.com/viewjob?jk=1&x=2"),
        # it-jobs.de: the path carries the refnr; the query is only cleaned like any other.
        (
            "https://www.it-jobs.de/job/x_ers-MTIw?utm_source=a&b=1&a=2",
            "https://it-jobs.de/job/x_ers-MTIw?a=2&b=1",
        ),
        # ATS hosts: the query goes, the path stays.
        (
            "https://boards.greenhouse.io/acme/jobs/4567?gh_src=abc#app",
            "https://boards.greenhouse.io/acme/jobs/4567",
        ),
        (
            "https://job-boards.greenhouse.io/acme/jobs/4567?gh_jid=4567",
            "https://job-boards.greenhouse.io/acme/jobs/4567",
        ),
        (
            "https://jobs.lever.co/acme/8b1d9f0e-1111-2222-3333-444444444444/apply?lever-source=x",
            "https://jobs.lever.co/acme/8b1d9f0e-1111-2222-3333-444444444444/apply",
        ),
        (
            "https://jobs.eu.lever.co/acme/8b1d9f0e-1111-2222-3333-444444444444?lever-origin=x",
            "https://jobs.eu.lever.co/acme/8b1d9f0e-1111-2222-3333-444444444444",
        ),
        (
            "https://jobs.ashbyhq.com/acme/8b1d9f0e-1111-2222-3333-444444444444?utm_source=x&embed=js",
            "https://jobs.ashbyhq.com/acme/8b1d9f0e-1111-2222-3333-444444444444",
        ),
        ("https://everphone.jobs.personio.de/job/12?display=en", "https://everphone.jobs.personio.de/job/12"),
        (
            "https://jobs.smartrecruiters.com/Acme/743999-engineer?trid=x",
            "https://jobs.smartrecruiters.com/Acme/743999-engineer",
        ),
    ],
)
def test_canonical_url_cases(url, canonical) -> None:
    assert canonical_url(url) == canonical


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", ""),
        ("  ", ""),
        ("mailto:jobs@example.com", "mailto:jobs@example.com"),
        ("ftp://example.com/a", "ftp://example.com/a"),
        ("www.stepstone.de/x?utm_source=a", "www.stepstone.de/x?utm_source=a"),
        ("/jobs/view/123", "/jobs/view/123"),
        ("  not\N{NO-BREAK SPACE}a\N{ZERO WIDTH SPACE} url  ", "not a url"),
        ("https://", "https://"),
        ("https://exa mple.com:99999999/x", "https://exa mple.com:99999999/x"),
        ("https://[::1/x", "https://[::1/x"),
    ],
)
def test_a_value_that_is_not_an_http_url_is_returned_as_norm_text(value, expected) -> None:
    assert canonical_url(value) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://www.stepstone.de/stellenangebote--X--14607704-inline.html?utm_source=a&rltr=1#top",
        "https://de.indeed.com/viewjob?jk=abc123&from=serp",
        "https://www.adzuna.de/details/5926833612?se=XYZ&v=123&utm_medium=api",
        "https://Example.com/B/?z=1&a=2&a=1&q=a%20b&e=",
        "https://www.it-jobs.de/job/x_ers-MTIw/?b=1&a=2",
        "https://boards.greenhouse.io/acme/jobs/4567?gh_src=abc",
        "https://example.com:8443/a?x=%C3%BC",
        "mailto:jobs@example.com",
    ],
)
def test_canonical_url_is_idempotent(url) -> None:
    once = canonical_url(url)
    assert canonical_url(once) == once


HOSTS = [
    "example.com",
    "WWW.Example.COM",
    "www.stepstone.de",
    "de.linkedin.com",
    "de.indeed.com",
    "www.adzuna.de",
    "www.it-jobs.de",
    "www.xing.com",
    "boards.greenhouse.io",
    "jobs.eu.lever.co",
    "everphone.jobs.personio.de",
]
PARAM_NAMES = [
    "utm_source",
    "UTM_Medium",
    "gclid",
    "fbclid",
    "mc_cid",
    "jk",
    "se",
    "v",
    "a",
    "b",
    "q",
    "rltr",
]


@settings(deadline=None)
@given(
    scheme=st.sampled_from(["http", "https", "HTTPS"]),
    host=st.sampled_from(HOSTS),
    path=st.text(alphabet="abc/-_.%20", max_size=12),
    params=st.lists(
        st.tuples(st.sampled_from(PARAM_NAMES), st.text(alphabet="abc 012&=+/%", max_size=6)), max_size=6
    ),
    fragment=st.text(alphabet="abc", max_size=3),
)
def test_canonical_url_properties(scheme, host, path, params, fragment) -> None:
    url = f"{scheme}://{host}/{path}?{urlencode(params)}#{fragment}"
    once = canonical_url(url)
    assert canonical_url(once) == once
    parts = urlsplit(once)
    assert parts.scheme == scheme.lower() and parts.fragment == ""
    assert parts.hostname == host.lower().removeprefix("www.")
    assert not parts.path.endswith("/")
    names = [name.lower() for name, _ in parse_qsl(parts.query, keep_blank_values=True)]
    assert not any(name.startswith("utm_") or name in TRACKING_PARAMS for name in names)
    assert names == sorted(names)
