import base64
import json

import pytest
from jobhunter.normalize.keys import (
    AdKey,
    ats_key,
    decode_ba_refnr,
    decode_serpapi_job_id,
    jsearch_uid_key,
    keys_from_link,
)


def test_publisher_keys_from_primary_links() -> None:
    assert keys_from_link("https://www.stepstone.de/stellenangebote--A--14607704-inline.html") == [
        AdKey("pub:stepstone.de", "14607704")
    ]
    assert keys_from_link("https://de.linkedin.com/jobs/view/senior-x-4480111992") == [
        AdKey("pub:linkedin.com", "4480111992")
    ]
    assert keys_from_link("https://de.indeed.com/viewjob?jk=abc123") == [AdKey("pub:indeed", "abc123")]
    assert keys_from_link("https://www.xing.com/jobs/berlin-software-158579394") == [
        AdKey("pub:xing.com", "158579394")
    ]
    assert keys_from_link("https://join.com/companies/roboservice/16831124-robotics") == [
        AdKey("pub:join.com", "16831124")
    ]
    assert keys_from_link(
        "https://www.arbeitnow.com/jobs/companies/roboservice-gmbh/robotics-engineer-194025"
    ) == [AdKey("arbeitnow", "194025")]
    assert keys_from_link("https://www.adzuna.de/details/5926833612") == [AdKey("adzuna", "5926833612")]
    assert keys_from_link("https://www.adzuna.de/land/ad/5926833612?se=x") == []
    assert keys_from_link("https://www.it-jobs.de/job/x_ers-MTIwMTYtMTAwMDU1NDg2NDUtUyAx") == [
        AdKey("pub:ba_refnr", "12016-10005548645-S 1")
    ]
    assert keys_from_link("https://jobs.eu.lever.co/acme/8b1d9f0e-1111-2222-3333-444444444444") == [
        AdKey("ats:lever:eu:acme", "8b1d9f0e-1111-2222-3333-444444444444")
    ]
    assert (
        keys_from_link("https://boards.greenhouse.io/acme/jobs/4567")
        == [AdKey("ats:greenhouse:acme", "4567")]
        and AdKey("htidocid", "x").exclusive is False
    )


def test_serpapi_and_ba_decoders() -> None:
    job_id = base64.b64encode(
        json.dumps({"job_title": "x", "htidocid": "UMsYA14AEpQuse3eAAAAAA==", "fc": "session"}).encode()
    ).decode()
    assert (
        decode_serpapi_job_id(job_id) == "UMsYA14AEpQuse3eAAAAAA=="
        and decode_serpapi_job_id("not base64!") is None
    )
    assert (
        decode_ba_refnr("MTgyNTEtMzM3NjctUw==") == "18251-33767-S"
        and ats_key("personio", "everphone", "12").ns == "ats:personio:everphone"
    )


@pytest.mark.parametrize(
    ("link", "expected"),
    [
        # The key is read from the canonical link: tracking, fragments and trailing slashes do not matter.
        (
            "https://www.stepstone.de/stellenangebote--A--14607704-inline.html?utm_source=x&rltr=1#top",
            AdKey("pub:stepstone.de", "14607704"),
        ),
        (
            "https://stepstone.de/stellenangebote--Senior-Developer-m-w-d--Berlin--Acme--14607704-inline.html",
            AdKey("pub:stepstone.de", "14607704"),
        ),
        (
            "https://www.linkedin.com/jobs/view/4480111992/?trk=public_jobs",
            AdKey("pub:linkedin.com", "4480111992"),
        ),
        (
            "https://de.linkedin.com/jobs/view/senior-x-4480111992?trk=a",
            AdKey("pub:linkedin.com", "4480111992"),
        ),
        ("https://www.indeed.com/viewjob?from=serp&jk=abc123&vjs=3", AdKey("pub:indeed", "abc123")),
        ("https://www.indeed.co.uk/rc/clk?jk=ff00ff00ff00ff00", AdKey("pub:indeed", "ff00ff00ff00ff00")),
        (
            "https://www.xing.com/jobs/berlin-software-engineer-158579394?sc_o=x",
            AdKey("pub:xing.com", "158579394"),
        ),
        (
            "https://join.com/companies/roboservice/16831124-robotics-engineer?pid=x",
            AdKey("pub:join.com", "16831124"),
        ),
        ("https://www.adzuna.de/details/5926833612?se=XYZ&v=123", AdKey("adzuna", "5926833612")),
        ("https://www.adzuna.co.uk/details/5926833612", AdKey("adzuna", "5926833612")),
        (
            "https://www.arbeitnow.com/jobs/companies/roboservice-gmbh/robotics-engineer-194025?ref=x",
            AdKey("arbeitnow", "194025"),
        ),
        (
            "https://www.it-jobs.de/job/software-entwickler_ers-MTgyNTEtMzM3NjctUw==/",
            AdKey("pub:ba_refnr", "18251-33767-S"),
        ),
        # ATS links: greenhouse (both hosts), lever (global and EU), ashby, personio, smartrecruiters.
        ("https://job-boards.greenhouse.io/acme/jobs/4567?gh_src=x", AdKey("ats:greenhouse:acme", "4567")),
        (
            "https://jobs.lever.co/acme/8b1d9f0e-1111-2222-3333-444444444444/apply?lever-source=x",
            AdKey("ats:lever:global:acme", "8b1d9f0e-1111-2222-3333-444444444444"),
        ),
        (
            "https://jobs.eu.lever.co/acme/8b1d9f0e-1111-2222-3333-444444444444",
            AdKey("ats:lever:eu:acme", "8b1d9f0e-1111-2222-3333-444444444444"),
        ),
        (
            "https://jobs.ashbyhq.com/n8n/8b1d9f0e-1111-2222-3333-444444444444?utm_source=x",
            AdKey("ats:ashby:n8n", "8b1d9f0e-1111-2222-3333-444444444444"),
        ),
        ("https://everphone.jobs.personio.de/job/12?display=en", AdKey("ats:personio:everphone", "12")),
        (
            "https://jobs.smartrecruiters.com/Acme/743999764423563-senior-engineer",
            AdKey("ats:smartrecruiters:Acme", "743999764423563"),
        ),
        (
            "https://jobs.smartrecruiters.com/Acme/743999764423563",
            AdKey("ats:smartrecruiters:Acme", "743999764423563"),
        ),
    ],
)
def test_one_key_per_recognised_link(link, expected) -> None:
    assert keys_from_link(link) == [expected]


@pytest.mark.parametrize(
    "link",
    [
        "",
        "not a url",
        "mailto:jobs@example.com",
        "https://example.com/jobs/12345",
        "https://www.adzuna.de/land/ad/5926833612",
        "https://www.adzuna.de/land/ad/5926833612?se=x&v=1",
        "https://www.adzuna.de/details/not-a-number",
        "https://www.stepstone.de/stellenangebote--A--inline.html",
        "https://www.stepstone.de/jobs/software-developer",
        "https://www.linkedin.com/jobs/search/?currentJobId=4480111992",
        "https://www.linkedin.com/jobs/view/senior-x",
        "https://de.indeed.com/jobs?q=python",
        "https://www.xing.com/jobs/berlin",
        "https://join.com/companies/roboservice",
        "https://www.arbeitnow.com/jobs/companies/roboservice-gmbh",
        "https://boards.greenhouse.io/acme",
        "https://boards.greenhouse.io/acme/jobs/not-a-number",
        "https://jobs.lever.co/acme",
        "https://jobs.lever.co/acme/not-a-uuid",
        "https://jobs.ashbyhq.com/acme",
        "https://everphone.jobs.personio.de/",
        "https://jobs.personio.de/job/12",
        "https://jobs.smartrecruiters.com/Acme",
        # The id sits in a host this module does not own.
        "https://example.com/stellenangebote--A--14607704-inline.html",
        "https://notindeed.com/viewjob?jk=abc123",
        "https://evil.example.com/boards.greenhouse.io/acme/jobs/4567",
        "https://boards.greenhouse.io.evil.example.com/acme/jobs/4567",
    ],
)
def test_no_key_without_a_recognised_primary_link(link) -> None:
    assert keys_from_link(link) == []


@pytest.mark.parametrize(
    "link",
    [
        # A refnr that does not decode, or does not decode to printable ASCII, is not a key.
        "https://www.it-jobs.de/job/x_ers-%%%%",
        "https://www.it-jobs.de/job/x_ers-AAECAw==",  # bytes 0x00 0x01 0x02 0x03
        "https://www.it-jobs.de/job/x_ers-w6TDtg==",  # "äö": UTF-8, not ASCII
        "https://www.it-jobs.de/job/x_ers-",
        "https://www.it-jobs.de/job/no-refnr-here",
    ],
)
def test_it_jobs_link_without_a_printable_refnr_has_no_key(link) -> None:
    assert keys_from_link(link) == []


def test_ad_key_is_a_frozen_value_with_an_exclusive_flag() -> None:
    key = AdKey("pub:stepstone.de", "1")
    assert key == AdKey("pub:stepstone.de", "1") and hash(key) == hash(AdKey("pub:stepstone.de", "1"))
    assert key != AdKey("pub:stepstone.de", "2") and key != AdKey("pub:linkedin.com", "1")
    with pytest.raises(AttributeError):
        key.value = "2"  # type: ignore[misc]
    assert key.exclusive is True
    assert AdKey("htidocid", "x").exclusive is False
    for ns in ("pub:indeed", "adzuna", "arbeitnow", "ats:lever:eu:acme", "pub:ba_refnr"):
        assert AdKey(ns, "1").exclusive is True


def test_native_identifier_keys() -> None:
    assert jsearch_uid_key("UMsYA14AEpQuse3eAAAAAA==") == AdKey("htidocid", "UMsYA14AEpQuse3eAAAAAA==")
    assert jsearch_uid_key("x").exclusive is False
    assert ats_key("greenhouse", "getyourguide", "4567") == AdKey("ats:greenhouse:getyourguide", "4567")
    assert ats_key("lever", "global:leverdemo", "uuid") == AdKey("ats:lever:global:leverdemo", "uuid")
    assert ats_key("personio", "everphone", "12").exclusive is True


def _job_id(payload: object) -> str:
    return base64.b64encode(json.dumps(payload).encode()).decode()


def test_decode_serpapi_job_id() -> None:
    assert decode_serpapi_job_id(_job_id({"htidocid": "abc==", "fc": "x"})) == "abc=="
    # Padding is optional.
    assert decode_serpapi_job_id(_job_id({"htidocid": "abc", "k": 1}).rstrip("=")) == "abc"
    # Anything else is None, never an exception.
    assert decode_serpapi_job_id("") is None
    assert decode_serpapi_job_id("not base64!") is None
    assert decode_serpapi_job_id(base64.b64encode(b"not json").decode()) is None
    assert decode_serpapi_job_id(base64.b64encode(b"\xff\xfe").decode()) is None
    assert decode_serpapi_job_id(_job_id(["htidocid"])) is None
    assert decode_serpapi_job_id(_job_id({"job_title": "x"})) is None
    assert decode_serpapi_job_id(_job_id({"htidocid": 5})) is None
    assert decode_serpapi_job_id(_job_id({"htidocid": ""})) is None
    assert decode_serpapi_job_id(_job_id({"htidocid": None})) is None


def test_decode_ba_refnr() -> None:
    assert decode_ba_refnr("MTgyNTEtMzM3NjctUw==") == "18251-33767-S"
    assert decode_ba_refnr("MTgyNTEtMzM3NjctUw") == "18251-33767-S"  # padding added
    assert decode_ba_refnr("MTgyNTEtMzM3NjctUw%3D%3D") == "18251-33767-S"  # percent-encoded padding
    assert decode_ba_refnr("MTIwMTYtMTAwMDU1NDg2NDUtUyAx") == "12016-10005548645-S 1"  # a space is printable
    assert decode_ba_refnr("") is None
    assert decode_ba_refnr("%%%") is None
    assert decode_ba_refnr("not base64!") is None
    assert decode_ba_refnr("AAECAw==") is None  # control characters
    assert decode_ba_refnr("w6TDtg==") is None  # not ASCII
    assert decode_ba_refnr("fn5+fg==") is not None  # 0x7e is printable ...
    assert decode_ba_refnr("f39/fw==") is None  # ... 0x7f (DEL) is not
