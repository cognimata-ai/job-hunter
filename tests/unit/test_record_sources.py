"""scripts/spikes/record_sources.py (Task 6): the budgeted, crash-safe live recorder.

Every test runs offline: the recorder's client factory is replaced by one over an `httpx.MockTransport`, and
the keys are fake values in an explicit environment (the machine's real keys are never read here).
"""

import fcntl
import importlib.util
import json
import os
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/spikes/record_sources.py"
FAKE_ENV = {  # visibly fake, and shaped like no provider key (secret scanners match keys by shape)
    "RAPIDAPI_KEY": "fake-rapidapi-key-0001",
    "SERPAPI_API_KEY": "fake-serpapi-key-0002",
    "ADZUNA_APP_ID": "fakeid03",
    "ADZUNA_APP_KEY": "fake-adzuna-key-0004",
}
T0 = datetime(2026, 10, 18, 20, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def rec() -> ModuleType:
    spec = importlib.util.spec_from_file_location("record_sources", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Clock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


async def _no_sleep(_seconds: float) -> None:
    return None


Handler = Callable[[httpx.Request], httpx.Response]


def answering(seen: list[httpx.Request], response: Callable[[], httpx.Response]) -> Handler:
    """A handler that records each request and answers it with `response()`."""

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return response()

    return handle


def run(
    rec: ModuleType,
    root: Path,
    handler: Handler,
    *args: str,
    clock: Clock | None = None,
    environ: dict[str, str] | None = None,
) -> int:
    def factory(_source_id: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    code: int = rec.main(
        list(args),
        root=root,  # test only: the CLI has no --root, so a run can never start a second budget
        environ=FAKE_ENV if environ is None else environ,
        client_factory=factory,
        now=clock or Clock(T0),
        sleep=_no_sleep,
    )
    return code


def budget(root: Path) -> Any:
    return json.loads((root / "budget.json").read_text())


def every_file(root: Path) -> Iterator[str]:
    for path in root.rglob("*"):
        if path.is_file():
            yield path.read_text(encoding="utf-8", errors="replace")


def assert_no_key(root: Path, *texts: str) -> None:
    for text in [*every_file(root), *texts]:
        for value in FAKE_ENV.values():
            assert value not in text


JSEARCH_PAGE = {
    "status": "OK",
    "request_id": "r-1",
    "data": {
        "jobs": [{"job_uid": f"uid{i:021d}==", "job_title": "Dev"} for i in range(10)],
        "cursor": "CUR123",
    },
}


def jsearch_handler(seen: list[httpx.Request], root: Path) -> Handler:
    def handle(request: httpx.Request) -> httpx.Response:
        state = budget(root)["jsearch"]
        # counted before the send: the entry of this request is already `sending`
        assert state["requests"][-1]["state"] == "sending" and state["sent"] == len(seen) + 1
        seen.append(request)
        headers = {
            "x-ratelimit-requests-limit": "200",
            "x-ratelimit-requests-remaining": str(200 - len(seen)),
            "set-cookie": "session=1",
            "content-type": "application/json",
        }
        if request.url.path == "/search":
            return httpx.Response(
                404,
                json={"message": "Endpoint '/search' does not exist"},
                headers={"x-rapidapi-proxy-response": "true"},
            )
        return httpx.Response(200, json=JSEARCH_PAGE, headers=headers)

    return handle


def test_jsearch_phase_one_counts_before_every_send_and_writes_no_key(
    rec: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[httpx.Request] = []
    assert run(rec, tmp_path, jsearch_handler(seen, tmp_path), "jsearch", "--phase", "1") == 0
    assert [r.url.path for r in seen] == ["/search", "/search-v2", "/search-v2", "/search-v2"]
    assert all(r.headers["x-rapidapi-key"] == FAKE_ENV["RAPIDAPI_KEY"] for r in seen)
    assert seen[2].url.params["date_posted"] == "today" and seen[3].url.params["cursor"] == "CUR123"
    assert (
        "cursor" not in seen[1].url.params
        and seen[1].url.params["query"] == "software engineer in Berlin, Germany"
    )
    state = budget(tmp_path)["jsearch"]
    assert state["sent"] == 4 and [(e["case"], e["state"], e["status"]) for e in state["requests"]] == [
        ("search_legacy_404", "done", 404),
        ("search_v2_page1", "done", 200),
        ("search_v2_today", "done", 200),
        ("search_v2_page2", "done", 200),
    ]
    assert state["phases"]["1"]["started_at"] == T0.isoformat() and state["phases"]["1"]["finished_at"]
    headers = json.loads((tmp_path / "jsearch/search_v2_page1.headers.json").read_text())
    assert headers["status"] == 200 and "set-cookie" not in headers["headers"]
    meta = json.loads((tmp_path / "jsearch/search_v2_page2.meta.json").read_text())
    assert meta["url"] == "https://jsearch.p.rapidapi.com/search-v2" and meta["params"]["cursor"] == "CUR123"
    assert json.loads((tmp_path / "jsearch/search_v2_page1.json").read_text()) == JSEARCH_PAGE
    out = capsys.readouterr().out
    assert "jsearch search_v2_page1 200 units_remaining=198" in out
    assert_no_key(tmp_path, out)


def test_a_request_beyond_the_budget_is_refused_unsent(rec: ModuleType, tmp_path: Path) -> None:
    (tmp_path / "budget.json").write_text(json.dumps({"adzuna": {"sent": 5, "requests": []}}))
    seen: list[httpx.Request] = []
    assert run(rec, tmp_path, answering(seen, lambda: httpx.Response(200)), "adzuna") != 0
    assert seen == [] and budget(tmp_path)["adzuna"]["sent"] == 5


def test_a_transport_failure_stays_counted_and_stops_a_paid_phase(rec: ModuleType, tmp_path: Path) -> None:
    calls: list[httpx.Request] = []

    def fail(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ReadTimeout("timed out", request=request)

    assert run(rec, tmp_path, fail, "adzuna") != 0
    state = budget(tmp_path)["adzuna"]
    assert len(calls) == 1 and state["sent"] == 1
    assert state["requests"] == [state["requests"][0]] and state["requests"][0]["state"] == "done"
    assert state["requests"][0]["status"] is None and state["requests"][0]["error"] == "ambiguous:timeout"


def test_an_unexpected_status_stops_a_paid_phase(rec: ModuleType, tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    assert (
        run(rec, tmp_path, answering(seen, lambda: httpx.Response(200, json={"status": "OK"})), "jsearch")
        != 0
    )
    assert len(seen) == 1 and budget(tmp_path)["jsearch"]["sent"] == 1  # the legacy probe answered 200: stop


def test_a_phase_never_runs_twice(rec: ModuleType, tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def ok(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"count": 120, "results": []})

    assert run(rec, tmp_path, ok, "adzuna") == 0 and len(seen) == 4
    assert run(rec, tmp_path, ok, "adzuna") != 0 and len(seen) == 4
    assert budget(tmp_path)["adzuna"]["sent"] == 4
    p = [r.url.params for r in seen]
    assert (p[0]["results_per_page"], p[1]["max_days_old"], p[2]["distance"], p[3]["distance"]) == (
        "100",
        "1",
        "10",
        "50",
    )
    assert all(r.url.params["app_key"] == FAKE_ENV["ADZUNA_APP_KEY"] for r in seen)
    assert "app_key" not in json.loads((tmp_path / "adzuna/search_p1_rpp100.meta.json").read_text())["params"]
    assert_no_key(tmp_path)


def test_a_second_invocation_fails_at_once_while_the_lock_is_held(rec: ModuleType, tmp_path: Path) -> None:
    fd = os.open(tmp_path / "budget.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        seen: list[httpx.Request] = []
        assert (
            run(rec, tmp_path, answering(seen, lambda: httpx.Response(200)), "arbeitnow", "--phase", "1") != 0
        )
        assert seen == []
    finally:
        os.close(fd)


def test_serpapi_account_reads_are_free_by_name_and_the_key_never_lands(
    rec: ModuleType, tmp_path: Path
) -> None:
    seen: list[httpx.Request] = []

    def serp(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/account.json":
            # the account read echoes the key and the e-mail: the raw file must not keep the key
            return httpx.Response(
                200, json={"api_key": FAKE_ENV["SERPAPI_API_KEY"], "total_searches_left": 249}
            )
        return httpx.Response(200, json={"search_metadata": {"id": "x"}, "jobs_results": []})

    assert run(rec, tmp_path, serp, "serpapi") == 0
    assert [r.url.path for r in seen] == ["/account.json", "/search.json", "/account.json"]
    state = budget(tmp_path)["serpapi"]
    assert state["sent"] == 1 and [e["free"] for e in state["requests"]] == [True, False, True]
    assert seen[1].url.params["hl"] == "en" and seen[1].url.params["q"] == "software engineer Berlin"
    assert "[secret:SERPAPI_API_KEY]" in (tmp_path / "serpapi/account_before.json").read_text()
    assert_no_key(tmp_path)


def test_a_second_serpapi_search_is_refused(rec: ModuleType, tmp_path: Path) -> None:
    (tmp_path / "budget.json").write_text(json.dumps({"serpapi": {"sent": 1, "requests": []}}))
    seen: list[httpx.Request] = []
    assert run(rec, tmp_path, answering(seen, lambda: httpx.Response(200, json={})), "serpapi") != 0
    assert [r.url.path for r in seen] == ["/account.json"] and budget(tmp_path)["serpapi"]["sent"] == 1


def test_jsearch_phase_two_waits_two_hours_and_reuses_the_phase_one_cursor(
    rec: ModuleType, tmp_path: Path
) -> None:
    seen: list[httpx.Request] = []
    assert run(rec, tmp_path, jsearch_handler(seen, tmp_path), "jsearch", "--phase", "1") == 0
    early = Clock(T0 + timedelta(hours=1, minutes=59))
    assert run(rec, tmp_path, jsearch_handler(seen, tmp_path), "jsearch", "--phase", "2", clock=early) != 0
    assert len(seen) == 4
    late = Clock(T0 + timedelta(hours=2, minutes=1))
    assert run(rec, tmp_path, jsearch_handler(seen, tmp_path), "jsearch", "--phase", "2", clock=late) == 0
    assert len(seen) == 5 and seen[4].url.params["cursor"] == "CUR123"
    assert (
        run(rec, tmp_path, jsearch_handler(seen, tmp_path), "jsearch", "--phase", "3", clock=late) != 0
    )  # < 24 h
    assert budget(tmp_path)["jsearch"]["sent"] == 5


def test_a_missing_cursor_stops_before_the_page_two_request(rec: ModuleType, tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def no_cursor(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/search":
            return httpx.Response(404, json={}, headers={"x-rapidapi-proxy-response": "true"})
        return httpx.Response(200, json={"status": "OK", "data": []})

    assert run(rec, tmp_path, no_cursor, "jsearch", "--phase", "1") != 0
    assert len(seen) == 3 and budget(tmp_path)["jsearch"]["sent"] == 3


def test_arbeitnow_phases_keep_their_spacing_and_late_phases_write_headers_only(
    rec: ModuleType, tmp_path: Path
) -> None:
    def feed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"slug": "a-1"}], "links": {}, "meta": {}},
            headers={"x-ratelimit-limit": "50", "x-ratelimit-remaining": "49"},
        )

    assert run(rec, tmp_path, feed, "arbeitnow", "--phase", "1") == 0
    assert run(rec, tmp_path, feed, "arbeitnow", "--phase", "2", clock=Clock(T0 + timedelta(minutes=19))) != 0
    assert run(rec, tmp_path, feed, "arbeitnow", "--phase", "2", clock=Clock(T0 + timedelta(minutes=20))) == 0
    assert run(rec, tmp_path, feed, "arbeitnow", "--phase", "3", clock=Clock(T0 + timedelta(minutes=40))) == 0
    assert (tmp_path / "arbeitnow/page2.json").exists() and not (
        tmp_path / "arbeitnow/page1_phase3.json"
    ).exists()
    assert (
        json.loads((tmp_path / "arbeitnow/page1_phase3.headers.json").read_text())["headers"][
            "x-ratelimit-remaining"
        ]
        == "49"
    )
    assert budget(tmp_path)["arbeitnow"]["sent"] == 3


def test_ats_details_follow_the_list_and_a_free_failure_does_not_stop(
    rec: ModuleType, tmp_path: Path
) -> None:
    seen: list[str] = []

    def boards(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if "/postings?" in str(request.url) or request.url.path.endswith("/postings"):
            return httpx.Response(
                200,
                json={
                    "totalFound": 2,
                    "offset": 0,
                    "limit": 100,
                    "content": [{"id": "744000001"}, {"id": "744000002"}],
                },
            )
        if "personio" in request.url.host:
            return httpx.Response(
                200, text="<?xml version='1.0'?><workzag-jobs/>", headers={"content-type": "text/xml"}
            )
        if "nonexistent" in request.url.path or request.url.host == "api.eu.lever.co":
            return httpx.Response(404, json={"error": "not found"})
        if request.url.host == "api.ashbyhq.com":
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json=[])

    assert run(rec, tmp_path, boards, "ats") != 0  # one board failed: reported, the others recorded
    assert len(seen) == 10 and seen[-2:] == [
        "https://api.smartrecruiters.com/v1/companies/DeliveryHero/postings/744000001",
        "https://api.smartrecruiters.com/v1/companies/DeliveryHero/postings/744000002",
    ]
    assert (tmp_path / "ats/personio_everphone.xml").exists() and (
        tmp_path / "ats/lever_eu_404.json"
    ).exists()
    assert budget(tmp_path)["ats"]["sent"] == 10


def test_missing_keys_refuse_before_any_send(
    rec: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[httpx.Request] = []
    env = {k: v for k, v in FAKE_ENV.items() if k != "ADZUNA_APP_KEY"}
    assert run(rec, tmp_path, answering(seen, lambda: httpx.Response(200)), "adzuna", environ=env) != 0
    assert seen == [] and "ADZUNA_APP_KEY" in capsys.readouterr().out
    assert not (tmp_path / "budget.json").exists() or budget(tmp_path).get("adzuna", {}).get("sent", 0) == 0


def test_an_internal_error_prints_its_class_only(
    rec: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def explode(request: httpx.Request) -> httpx.Response:
        raise RuntimeError(f"boom {request.url}")  # the message would carry the URL with its key

    code = run(rec, tmp_path, explode, "adzuna")
    out = capsys.readouterr().out
    assert code != 0 and "RuntimeError" in out and "boom" not in out and FAKE_ENV["ADZUNA_APP_KEY"] not in out
    assert budget(tmp_path)["adzuna"]["sent"] == 1  # counted before the send


def test_the_cursor_is_found_by_name_with_cursor_first(rec: ModuleType) -> None:
    assert rec.find_cursor({"data": {"jobs": [], "cursor": "A"}}) == (("data", "cursor"), "A")
    assert rec.find_cursor({"next_cursor": "B", "data": {"cursor": "A"}}) == (("data", "cursor"), "A")
    assert rec.find_cursor({"meta": {"next_cursor": "B"}}) == (("meta", "next_cursor"), "B")
    assert rec.find_cursor({"data": [], "cursor": ""}) is None and rec.find_cursor(None) is None


def test_the_budget_root_is_the_repository_s_whatever_the_cwd(
    rec: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 6a review, finding 4: a cwd-relative root, or a --root flag, would start a fresh budget.json
    monkeypatch.chdir(tmp_path)
    assert rec.DEFAULT_ROOT == REPO / ".fixtures-raw" and rec.DEFAULT_ROOT.is_absolute()
    seen: list[httpx.Request] = []
    with pytest.raises(SystemExit):  # refused while parsing, before any file or request
        rec.main(
            ["adzuna", "--root", str(tmp_path)],
            environ=FAKE_ENV,
            client_factory=lambda _s: httpx.AsyncClient(
                transport=httpx.MockTransport(answering(seen, lambda: httpx.Response(200)))
            ),
            now=Clock(T0),
            sleep=_no_sleep,
        )
    assert seen == [] and list(tmp_path.iterdir()) == []
