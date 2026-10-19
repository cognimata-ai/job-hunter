# tests/unit/test_fixture_scrub.py
import base64
import json
import re
from pathlib import Path
from typing import Any, cast

import pytest
from jobhunter.cli import main
from jobhunter.cli_fixtures import (
    IDENTIFIER_FIELDS,
    PHONE_PAREN,
    URL_IN_TEXT,
    Pseudonyms,
    scrub_document,
    scrub_text,
    scrub_tree,
    valid_url,
    validate_identifier,
)
from jobhunter.observability.redact import EMAIL, KEY, PHONE
from jobhunter.types import JSONValue


def test_scrub_rules_are_deterministic_and_leave_no_pii() -> None:
    p = Pseudonyms()
    text = (
        "Ansprechpartner: Max Mustermann, max.mustermann@acme.de, Tel. +49 30 1234567. Contact Jane Doe. "
        "https://www.linkedin.com/in/max-mustermann "
        "https://adzuna.de/land/ad/1?se=AbC123&utm_source=APPID99&v=XyZ "
        "key sk-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    )
    out = scrub_text(text, p)
    assert (
        "[person:1]" in out
        and "[email:1]" in out
        and "[phone:1]" in out
        and "[person:2]" in out
        and "[user:1]" in out
    )
    assert "utm_source=scrubbed" in out and "se=[token:" in out and "v=[token:" in out and "sk-" not in out
    assert (
        not EMAIL.search(out)
        and not PHONE.search(out)
        and not KEY.search(out)
        and scrub_text(text, Pseudonyms()) == out
    )
    assert "[token:" in scrub_text(
        "ref " + "a1b2c3d4" * 6, Pseudonyms()
    )  # a 48-character hex key in free text is a key


def test_identifier_fields_are_scrubbed_by_name_not_by_shape() -> None:
    doc: dict[str, JSONValue] = {
        "search_metadata": {"id": "6b1e9b8c4f2a4d3e9c0b1a2f3e4d5c6b"},
        "serpapi_pagination": {"next_page_token": "x" * 60},
        "jobs_results": [{"title": "Dev " + "a1b2c3d4" * 6, "job_id": "notbase64"}],
    }
    out: Any = scrub_document(doc, "serpapi", Pseudonyms())
    assert out["search_metadata"]["id"].startswith("[token:") and out["serpapi_pagination"][
        "next_page_token"
    ].startswith("[token:")
    assert "[token:" in out["jobs_results"][0]["title"] and set(out) == set(
        doc
    )  # keys untouched, free text scrubbed
    link = "https://www.stepstone.de/stellenangebote--senior-python-entwickler-berlin-acme-gmbh--14607704-inline.html?utm_source=x"
    kept = cast(Any, scrub_document({"data": [{"job_apply_link": link}]}, "jsearch", Pseudonyms()))["data"][
        0
    ]["job_apply_link"]
    assert KEY.search(link) and kept == link.replace(
        "utm_source=x", "utm_source=scrubbed"
    )  # a link keeps its key-shaped path: the publisher key survives


def test_serpapi_and_jsearch_ids_keep_the_htidocid() -> None:
    job_id = base64.b64encode(
        json.dumps({"htidocid": "UMsYA14AEpQuse3eAAAAAA==", "fc": "SECRETSESSION"}).encode()
    ).decode()
    doc: Any = scrub_document({"jobs_results": [{"job_id": job_id}]}, "serpapi", Pseudonyms())
    assert json.loads(base64.b64decode(doc["jobs_results"][0]["job_id"])) == {
        "htidocid": "UMsYA14AEpQuse3eAAAAAA==",
        "fc": "scrubbed",
    }
    js = base64.b64encode(b"UMsYA14AEpQuse3eAAAAAA==:" + b"x" * 276).decode()
    out: Any = scrub_document(
        {"data": [{"job_id": js, "job_uid": "UMsYA14AEpQuse3eAAAAAA=="}]}, "jsearch", Pseudonyms()
    )
    assert (
        base64.b64decode(out["data"][0]["job_id"]) == b"UMsYA14AEpQuse3eAAAAAA==:scrubbed"
        and out["data"][0]["job_uid"] == "UMsYA14AEpQuse3eAAAAAA=="
    )


# Beyond the plan's three pins: the rules the scan (tests/contract/test_fixture_pii.py) relies on.
PHONE_DE = re.compile(r"\(0\d{2,5}\)\s?(?:\d{4,}|\d{2,}(?:[ /\-]\d{2,})+)(?:[ /\-]\d{2,})*(?!\w)")


@pytest.mark.parametrize(
    "phone",
    ["(030) 123456", "(030) 12 34 56", "(0911) 123-456", "+49 (0)30 1234567", "030/1234567", "030 1234 567"],
)
def test_german_phone_forms_are_scrubbed(phone: str) -> None:
    out = scrub_text(f"Ruf an: {phone}.", Pseudonyms())
    assert (
        "[phone:1]" in out
        and not PHONE.search(out)
        and not PHONE_DE.search(out)
        and not PHONE_PAREN.search(out)
    )


def test_a_labelled_number_without_a_separator_is_a_phone() -> None:
    assert scrub_text("Telefon: 0301234567", Pseudonyms()) == "Telefon: [phone:1]"


def test_names_before_an_email_and_in_the_contact_window() -> None:
    p = Pseudonyms()
    out = scrub_text(
        "Fragen? Anna von Schmidt (E-Mail: anna@acme.de) hilft. Kontakt: Frau Erika Musterfrau", p
    )
    assert "Anna" not in out and "Schmidt" not in out and "Erika" not in out and "Musterfrau" not in out
    assert "[email:1]" in out and out.count("[person:") == 2
    # the same name later in another text gets the same pseudonym: first appearance wins
    assert scrub_text("Your contact: Anna von Schmidt", p) == "Your contact: [person:1]"


def test_html_escaped_links_end_at_the_entity_and_keep_their_path() -> None:
    link = "https://jobs.example.com/apply/senior-backend-engineer-berlin-1234567890abcdef?utm_source=APPID99"
    text = f"&lt;a href=&quot;{link}&amp;utm_medium=x&quot;&gt;apply&lt;/a&gt;"
    out = scrub_text(text, Pseudonyms())
    assert [m.group(0) for m in URL_IN_TEXT.finditer(text)] == [link + "&amp;utm_medium=x"]
    assert "senior-backend-engineer-berlin-1234567890abcdef" in out and "APPID99" not in out
    assert "utm_source=scrubbed&amp;utm_medium=scrubbed&quot;" in out


def test_a_secret_query_value_is_a_token_and_a_url_value_is_scrubbed_recursively() -> None:
    out = scrub_text(
        "https://api.example.com/x?app_id=abcd1234&next=https%3A%2F%2Fwww.linkedin.com%2Fin%2Fjane-doe",
        Pseudonyms(),
    )
    assert "abcd1234" not in out and "jane-doe" not in out and "app_id=[token:1]" in out


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        (
            "https://www.stepstone.de/stellenangebote--senior-python-entwickler-berlin-acme-gmbh--14607704-inline.html",
            True,
        ),
        ("https://www.adzuna.de/land/ad/1?se=[token:1]&utm_source=scrubbed&v=[token:2]", True),
        ("https://www.adzuna.de/land/ad/1?se=" + "a1b2c3d4" * 6, False),  # a key in a query value
        ("https://www.example.com/x?mail=max%40acme.de", False),
        ("https://www.example.com/x?tel=%2B49%2030%201234567", False),
        ("https://user:pw@www.example.com/x", False),
        ("https://www.linkedin.com/in/max-mustermann", False),  # a profile path left in place
        ("https://www.linkedin.com/in/[user:1]", True),
        ("ftp://www.example.com/x", False),
        ("https:///nohost", False),
        ("https://www.google.com/search?q=x#fpstate=tldetail&htidocid=" + "Z" * 44, False),
        # a form-encoded query value is judged decoded: these are words, not a key
        ("https://www.google.com/search?q=software+engineer+Berlin+since+yesterday&hl=en", True),
        ("https://www.example.com/x?k=" + "a1b2c3d4%2B" * 5, False),  # a percent-encoded key is still a key
    ],
)
def test_valid_url(url: str, ok: bool) -> None:
    assert valid_url(url) is ok


def test_validate_identifier_is_strict_per_field() -> None:
    serp = base64.b64encode(
        json.dumps({"htidocid": "UMsYA14AEpQuse3eAAAAAA==", "fc": "scrubbed"}).encode()
    ).decode()
    leaked = base64.b64encode(
        json.dumps({"htidocid": "UMsYA14AEpQuse3eAAAAAA==", "fc": "x" * 50}).encode()
    ).decode()
    assert validate_identifier("serpapi", "job_id", serp) and not validate_identifier(
        "serpapi", "job_id", leaked
    )
    js = base64.b64encode(b"UMsYA14AEpQuse3eAAAAAA==:scrubbed").decode()
    assert validate_identifier("jsearch", "job_id", js)
    assert not validate_identifier(
        "jsearch", "job_id", base64.b64encode(b"UMsYA14AEpQuse3eAAAAAA==:" + b"x" * 60).decode()
    )
    assert validate_identifier("serpapi", "next_page_token", "[token:3]")
    assert not validate_identifier("serpapi", "next_page_token", "x" * 60)
    assert validate_identifier("arbeitnow", "slug", "senior-python-developer-m-w-d-berlin-acme-gmbh-123456")
    assert not validate_identifier("arbeitnow", "slug", "a1b2c3d4" * 6)  # a hex key is no slug
    assert not validate_identifier("adzuna", "id", "a1b2c3d4" * 6) and validate_identifier(
        "adzuna", "id", "4567890123"
    )
    assert validate_identifier("jsearch", "job_uid", "UMsYA14AEpQuse3eAAAAAA==")
    assert (
        IDENTIFIER_FIELDS["jsearch"]["job_apply_link"] == "url"
        and IDENTIFIER_FIELDS["serpapi"]["job_id"] == "serpapi_job_id"
    )
    assert not validate_identifier("jsearch", "job_title", "a1b2c3d4" * 6)  # not an identifier field


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_scrub_tree_scrubs_every_file_and_never_copies_the_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ADZUNA_APP_KEY", "Zq9secretvalue77")
    src, dst = tmp_path / "raw", tmp_path / "out"
    _write(src / "budget.json", '{"jsearch": {"sent": 1, "requests": []}}')
    _write(src / "budget.lock", "")
    _write(
        src / "adzuna/search.json",
        json.dumps(
            {
                "results": [
                    {
                        "id": "4567890123",
                        "description": "Kontakt: Max Mustermann, max@acme.de",
                        "redirect_url": "https://www.adzuna.de/land/ad/4567890123?se=AbC&utm_source=Zq9secretvalue77&v=1",
                    }
                ]
            }
        ),
    )
    _write(
        src / "adzuna/search.headers.json",
        json.dumps(
            {
                "status": 200,
                "headers": {
                    "Content-Type": "application/json",
                    "Set-Cookie": "s=1",
                    "X-RateLimit-Remaining": "5",
                    "Server": "nginx",
                },
            }
        ),
    )
    _write(
        src / "adzuna/search.meta.json",
        json.dumps(
            {
                "url": "https://api.adzuna.com/v1/api/jobs/de/search/1",
                "params": {"what": "x", "key": "Zq9secretvalue77"},
                "fetched_at": "2026-10-18T20:00:00Z",
            }
        ),
    )
    _write(
        src / "ats/personio_acme.xml",
        "<workzag-jobs><position><jobDescription><![CDATA[Kontakt: Erika Musterfrau, "
        "(030) 123456]]></jobDescription></position></workzag-jobs>",
    )
    _write(src / "jsearch/search_v2_html.html", "<html>Contact Jane Doe jane@x.de</html>")
    report = scrub_tree(src, dst)
    written = sorted(str(p.relative_to(dst)) for p in dst.rglob("*") if p.is_file())
    assert written == [
        "adzuna/search.headers.json",
        "adzuna/search.json",
        "adzuna/search.meta.json",
        "ats/personio_acme.xml",
        "jsearch/search_v2_html.html",
    ]
    assert report.files == 5 and report.skipped == ("budget.json", "budget.lock")
    everything = "".join(p.read_text(encoding="utf-8") for p in dst.rglob("*") if p.is_file())
    assert (
        "Zq9secretvalue77" not in everything
        and "Mustermann" not in everything
        and "Musterfrau" not in everything
    )
    assert "Jane" not in everything and not EMAIL.search(everything) and not PHONE_DE.search(everything)
    assert json.loads((dst / "adzuna/search.headers.json").read_text()) == {
        "status": 200,
        "headers": {"content-type": "application/json", "x-ratelimit-remaining": "5"},
    }
    body = json.loads((dst / "adzuna/search.json").read_text())
    assert (
        body["results"][0]["id"] == "4567890123"
        and "/land/ad/4567890123?" in body["results"][0]["redirect_url"]
    )
    again = tmp_path / "again"
    scrub_tree(src, again)
    assert all((again / p).read_bytes() == (dst / p).read_bytes() for p in written)  # deterministic


def test_cli_fixtures_scrub(tmp_path: Path) -> None:
    _write(
        tmp_path / "raw/arbeitnow/page1.json", json.dumps({"data": [{"slug": "a-b-1", "description": "x"}]})
    )
    assert main(["fixtures", "scrub", str(tmp_path / "raw"), str(tmp_path / "out")]) == 0
    assert json.loads((tmp_path / "out/arbeitnow/page1.json").read_text())["data"][0]["slug"] == "a-b-1"


def test_google_session_parameters_are_tokens_and_identity_parameters_stay() -> None:
    url = (
        "https://www.google.com/search?sca_esv=04f0bf379964c90d&sxsrf=APpeQnvBm4ZToqexceupBqOCZEY6cbdxOw:179235&"
        "q=software+engineer&ibp=htl;jobs&htidocid=UMsYA14AEpQuse3eAAAAAA%3D%3D&ved=2ahUKEwi&uds=AHn8x"
    )
    out = scrub_text(url, Pseudonyms())
    assert not any(token in out for token in ("04f0bf379964c90d", "APpeQnv", "2ahUKEwi", "AHn8x"))
    assert "q=software+engineer&ibp=htl;jobs&htidocid=UMsYA14AEpQuse3eAAAAAA%3D%3D&" in out and valid_url(out)


def test_one_source_never_renumbers_another(tmp_path: Path) -> None:
    for name in ("a", "b"):
        _write(tmp_path / f"raw1/{name}/x.json", json.dumps({"description": f"Kontakt: Max Muster{name}"}))
    _write(tmp_path / "raw2/a/0.json", json.dumps({"description": "Kontakt: Erika Neu"}))
    _write(tmp_path / "raw2/a/x.json", json.dumps({"description": "Kontakt: Max Mustera"}))
    _write(tmp_path / "raw2/b/x.json", json.dumps({"description": "Kontakt: Max Musterb"}))
    scrub_tree(tmp_path / "raw1", tmp_path / "out1")
    scrub_tree(tmp_path / "raw2", tmp_path / "out2")
    assert (tmp_path / "out1/b/x.json").read_text() == (tmp_path / "out2/b/x.json").read_text()
    assert "[person:2]" in (tmp_path / "out2/a/x.json").read_text()  # within a source: first appearance wins


def test_structured_person_fields_are_pseudonymised_by_path() -> None:
    # found in the manual review: SmartRecruiters names the recruiter of every posting in `creator`
    doc: JSONValue = {
        "content": [
            {
                "name": "Analytics Engineer II",
                "creator": {
                    "name": "Erika Musterfrau",
                    "avatarUrl": "https://img.example.com/a/9f8e7d6c5b4a",
                },
                "company": {"name": "Delivery Hero"},
            },
            {
                "name": "Lead Procurement",
                "creator": {"name": "Erika Musterfrau"},
                "recruiter": "Max Mustermann",
            },
        ]
    }
    out = cast(Any, scrub_document(doc, "ats", Pseudonyms()))
    first, second = out["content"]
    assert (
        first["creator"] == {"name": "[person:1]", "avatarUrl": "[token:1]"}
        and second["creator"]["name"] == "[person:1]"
    )
    assert second["recruiter"] == "[person:2]" and first["name"] == "Analytics Engineer II"
    assert first["company"]["name"] == "Delivery Hero"  # a company is not a person


# The manual review of the recorded fixtures found these forms: each case is the found pattern, made-up names.
@pytest.mark.parametrize(
    ("text", "names"),
    [
        # a first name alone
        ("Dein Kontakt Du hast Fragen? Dann wende dich gerne an Lena unter.", ("Lena",)),
        ("Bei Rückfragen steht euch unser Referent Herr Mustermann gerne zur Verfügung.", ("Mustermann",)),
        ("by calling Mr. Mustermann at the office", ("Mustermann",)),
        ("unser Gründercoach Prof. Dr. Erika von Musterfrau.", ("Erika", "Musterfrau")),
        (
            "Musterstraße 92, 12163 Berlin Erika Musterfrau +49 30 1234567 e@example.com",
            ("Erika", "Musterfrau"),
        ),
    ],
)
def test_review_found_names_are_pseudonymised(text: str, names: tuple[str, ...]) -> None:
    out = scrub_text(text, Pseudonyms())
    assert "[person:1]" in out and not any(name in out for name in names)


@pytest.mark.parametrize(
    "phone", ["08106 \u2013 39 80 370", "030 12 34 56", "0800 - 33 44 55", "030 \u2013 1234567"]
)
def test_review_found_phone_forms_are_scrubbed(phone: str) -> None:
    out = scrub_text(f"Noch Fragen? {phone} Wir freuen uns.", Pseudonyms())
    assert "[phone:1]" in out and not re.search(r"\d{2}", out)


# 6a review, finding 3: forms the 6b German pages can hold.
def test_a_double_escaped_separator_keeps_the_parameter_rules() -> None:
    text = "&lt;a href=&quot;https://x.example/T?data=1&amp;amp;utm_source=acme.com&amp;amp;se=AbC&quot;&gt;"
    out = scrub_text(text, Pseudonyms())
    assert "utm_source=scrubbed&amp;amp;se=[token:1]&quot;" in out and "acme.com" not in out


@pytest.mark.parametrize(
    "phone",
    ["(0)30 1234567", "01711234567", "030.1234567", "+49.30.1234567", "0049 30 1234567", "030 / 12 34 56 78"],
)
def test_unlabelled_phone_forms_are_scrubbed(phone: str) -> None:
    out = scrub_text(f"Erreichbar unter {phone} oder vor Ort.", Pseudonyms())
    assert "[phone:1]" in out and not re.search(r"\d", re.sub(r"\[\w+:\d+\]", "", out)), out


@pytest.mark.parametrize(
    "value",
    [
        "744000156183719",  # a SmartRecruiters posting id
        "5933514068",  # an Adzuna id
        "1792353656",  # epoch seconds
        "1792353656000",  # epoch milliseconds
        "3fa85f64-5717-4562-b3fc-015712345678",  # a UUID with an all-digit last group
        "/jobs/0171123456789abc",  # a path id
        "Version 0.10.2",
    ],
)
def test_ids_uuids_and_timestamps_are_no_phone(value: str) -> None:
    assert scrub_text(value, Pseudonyms()) == value


# 6b review: a name joined to its phone number or e-mail address by a word ("steht Dir <name> unter <phone>").
@pytest.mark.parametrize(
    "text",
    [
        "Für Rückfragen steht Dir Erika Muster-Frau unter 0800 1234567 (0800 ACME JOB) gerne zur Verfügung.",
        "Fragen beantwortet Max Mustermann per E-Mail an max@example.com.",
        "Questions? Reach Jane Doe at +44 20 7946 0000 or via jane@example.com.",
    ],
)
def test_a_name_joined_to_its_contact_by_a_word_is_pseudonymised(text: str) -> None:
    out = scrub_text(text, Pseudonyms())
    assert "[person:1]" in out and not any(
        n in out for n in ("Erika", "Muster-Frau", "Max", "Mustermann", "Jane", "Doe")
    )


# 6b review: founders who introduce themselves by first name as the ad's contacts.
def test_a_team_introduction_by_first_names_is_pseudonymised() -> None:
    out = scrub_text("Hinter Acme stehen wir: Lena und Jonas, zwei Informatik-Studenten.", Pseudonyms())
    assert "Lena" not in out and "Jonas" not in out and "[person:1]" in out and "Hinter Acme" in out
    english = scrub_text("Meet the founders, that's us: Lena, Jonas and Mia.", Pseudonyms())
    assert not any(name in english for name in ("Lena", "Jonas", "Mia"))
    tagged = scrub_text(
        "<p>Hinter Acme stehen wir: <strong>Lena und Jonas</strong>, zwei Studenten.</p>", Pseudonyms()
    )
    assert "Lena" not in tagged and "Jonas" not in tagged and "<strong>[person:1]</strong>" in tagged
    bracketed = scrub_text("Technik ist da, und wir (Lena &amp; Jonas) stehen dir zur Seite.", Pseudonyms())
    assert "Lena" not in bracketed and "Jonas" not in bracketed and "wir ([person:1])" in bracketed
    benefits = "Das bieten wir: Urlaub und Gehalt"  # a list of benefits after "wir:" is no team
    assert scrub_text(benefits, Pseudonyms()) == benefits
