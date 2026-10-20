# tests/unit/test_watchlist_patterns.py
import pytest
from jobhunter.sources.watchlist_patterns import API_HOSTS, SLUG, Detected, Unsupported, api_host, detect


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://boards.greenhouse.io/acme/jobs/123", Detected("greenhouse", "acme", None)),
        ("https://job-boards.greenhouse.io/acme", Detected("greenhouse", "acme", None)),
        ("https://jobs.lever.co/acme/", Detected("lever", "acme", "global")),
        ("https://jobs.eu.lever.co/Acme?lang=de", Detected("lever", "Acme", "eu")),
        ("https://jobs.ashbyhq.com/n8n", Detected("ashby", "n8n", None)),
        ("https://everphone.jobs.personio.de/", Detected("personio", "everphone", None)),
        ("https://everphone.jobs.personio.com/job/1", Detected("personio", "everphone", None)),
        ("https://bunq.recruitee.com/o/x", Detected("recruitee", "bunq", None)),
        (
            "https://jobs.smartrecruiters.com/DeliveryHero/123",
            Detected("smartrecruiters", "DeliveryHero", None),
        ),
        (
            "https://careers.smartrecruiters.com/DeliveryHero",
            Detected("smartrecruiters", "DeliveryHero", None),
        ),
        ("https://apply.workable.com/acme", Unsupported("workable")),
        ("https://www.acme.com/careers", Unsupported("unsupported_pattern")),
        ("https://jobs.lever.co/../etc", Unsupported("bad_slug")),
        ("not a url", Unsupported("not_a_url")),
    ],
)
def test_detection_table(url, expected) -> None:
    assert detect(url) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # the host in any case, a fragment and a port are ignored, like the query and the path tail
        ("HTTPS://Jobs.Lever.co:443/acme/abc#apply", Detected("lever", "acme", "global")),
        ("http://boards.greenhouse.io/acme?gh_src=x", Detected("greenhouse", "acme", None)),
        ("  https://jobs.ashbyhq.com/n8n  ", Detected("ashby", "n8n", None)),
        # a board host without a slug is no board
        ("https://jobs.lever.co/", Unsupported("unsupported_pattern")),
        ("https://boards.greenhouse.io", Unsupported("unsupported_pattern")),
        ("https://jobs.personio.de/", Unsupported("unsupported_pattern")),
        ("https://recruitee.com/", Unsupported("unsupported_pattern")),
        # a sub-domain of a board host is not that board host; a deeper Personio host is not a board
        ("https://evil.jobs.lever.co/acme", Unsupported("unsupported_pattern")),
        ("https://a.b.jobs.personio.de/", Unsupported("unsupported_pattern")),
        # slugs: encoded, too long, or not starting with a letter or digit
        ("https://jobs.lever.co/%2e%2e/etc", Unsupported("bad_slug")),
        ("https://jobs.ashbyhq.com/" + "a" * 101, Unsupported("bad_slug")),
        ("https://jobs.ashbyhq.com/_acme", Unsupported("bad_slug")),
        ("https://apply.workable.com/", Unsupported("workable")),
        # not a web URL
        ("ftp://jobs.lever.co/acme", Unsupported("not_a_url")),
        ("jobs.lever.co/acme", Unsupported("not_a_url")),
        ("http://[::1", Unsupported("not_a_url")),
        ("", Unsupported("not_a_url")),
    ],
)
def test_detection_edges(url, expected) -> None:
    assert detect(url) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # fix wave F4 (ledger L151): the ATS's own site names no board
        ("https://www.recruitee.com/", Unsupported("unsupported_pattern")),
        ("https://www.recruitee.com/en/careers", Unsupported("unsupported_pattern")),
        ("https://www.jobs.personio.de/", Unsupported("unsupported_pattern")),
        ("https://WWW.Recruitee.com", Unsupported("unsupported_pattern")),
        ("https://wwwacme.recruitee.com", Detected("recruitee", "wwwacme", None)),  # only the label `www`
        # a Greenhouse embed URL names its board in `for`, never `embed`
        ("https://boards.greenhouse.io/embed/job_board?for=acme", Detected("greenhouse", "acme", None)),
        (
            "https://job-boards.greenhouse.io/embed/job_app?token=123&for=acme&b=x",
            Detected("greenhouse", "acme", None),
        ),
        ("https://boards.greenhouse.io/embed/job_board", Unsupported("unsupported_pattern")),
        ("https://boards.greenhouse.io/embed/job_board?for=", Unsupported("unsupported_pattern")),
        ("https://boards.greenhouse.io/embed/job_board?for=../etc", Unsupported("bad_slug")),
        ("https://boards.greenhouse.io/acme?for=other", Detected("greenhouse", "acme", None)),
        (
            "https://jobs.lever.co/embed?for=acme",
            Detected("lever", "embed", "global"),
        ),  # Greenhouse's form only
    ],
)
def test_the_ats_site_and_the_greenhouse_embed_form(url, expected) -> None:
    assert detect(url) == expected


def test_slug_pattern_is_anchored_on_the_whole_value() -> None:
    assert SLUG.fullmatch("a" * 100) and not SLUG.fullmatch("a" * 101)
    assert SLUG.fullmatch("Acme_Co.de-1") and not SLUG.fullmatch("-acme") and not SLUG.fullmatch("acme\n")


def test_api_hosts_are_the_fixed_allowlist() -> None:
    assert dict(API_HOSTS) == {
        "greenhouse": ("boards-api.greenhouse.io",),
        "lever": ("api.lever.co", "api.eu.lever.co"),
        "ashby": ("api.ashbyhq.com",),
        "personio": ("{slug}.jobs.personio.de",),
        "recruitee": ("{slug}.recruitee.com",),
        "smartrecruiters": ("api.smartrecruiters.com",),
    }
    assert api_host("lever", "acme", "global") == "api.lever.co"
    assert api_host("lever", "acme", "eu") == "api.eu.lever.co"
    assert api_host("personio", "everphone", None) == "everphone.jobs.personio.de"
    assert api_host("recruitee", "bunq", None) == "bunq.recruitee.com"
    assert api_host("greenhouse", "acme", None) == "boards-api.greenhouse.io"
    with pytest.raises(ValueError, match="slug"):
        api_host("personio", "evil.example/x", None)
    with pytest.raises(ValueError, match="region"):
        api_host("lever", "acme", None)
