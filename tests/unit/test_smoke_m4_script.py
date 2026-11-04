"""scripts/smoke_m4.py (plan M4 Task 36): its twenty-three checks in order (smoke_m3's fifteen as they are,
then its own eight), the values it copies pinned to the code's, each check's predicate on faked answers, and a
whole run against a stubbed stack that prints twenty-three `ok` lines and never the cookie, the password, a
key, an `Idempotency-Key`, an ad's text, a CV text or a draft; `subprocess` and the HTTP transport stubbed.

The stubbed stack is `test_smoke_m3_script`'s (`Stack`, `Database`: every M2 and M3 route and statement) with
the M4 routes and statements added here (`M4Stack`, `M4Database`)."""

import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Final, get_args
from urllib.parse import parse_qs

import httpx
import pytest
from jobhunter.api import deps
from jobhunter.api.edge import SECURITY_HEADERS
from jobhunter.auth.sessions import SLIDE_EVERY
from jobhunter.jobs.query import WorkMode
from jobhunter.observability.attention import AttentionOut
from jobhunter.scoring.targets import ACCEPTED_STATES, RETRYABLE_STATES

from tests.unit.test_e2e_setup_script import DESCRIPTION, detail_body
from tests.unit.test_smoke_m3_script import AD_TEXT, CONTENT, COOKIE, KEY, TOKEN, Database, Stack
from tests.unit.test_smoke_m3_script import CHECK_NAMES as M3_CHECK_NAMES

SCRIPTS: Final = Path(__file__).resolve().parents[2] / "scripts"
M4_CHECK_NAMES: Final = [
    "spa",
    "openapi_doc",
    "jobs_list",
    "job_detail",
    "actions",
    "status",
    "needs_attention",
    "labelling",
]
CHECK_NAMES: Final = [*M3_CHECK_NAMES, *M4_CHECK_NAMES]
JOB: Final = 21  # the grounded candidate
REVISION: Final = 210
SKILL: Final = "Python"
KEYED_DIR: Final = "/tmp/fake-keyed"  # noqa: S108 - the worker's tmpfs path (deploy/compose.e2e.yaml), faked
RESPONSE: Final = 55  # the seeded rejected response
ITEM: Final = 66
OPERATION: Final = 91
ASSET: Final = "/assets/index-Bx7a2Qd1.js"
INDEX_HTML: Final = (
    '<!doctype html><html lang="en"><head><meta charset="UTF-8" />'
    f'<script type="module" crossorigin src="{ASSET}"></script>'
    '<link rel="stylesheet" crossorigin href="/assets/index-C9d0e1f2.css"></head>'
    '<body><div id="root"></div></body></html>'
)
CSP: Final = SECURITY_HEADERS["Content-Security-Policy"]
FIRST_PAGE: Final = list(range(101, 106))
SECOND_PAGE: Final = list(range(106, 111))
CURSOR: Final = "eyJzb3J0Ijoic2NvcmVfbWlkIn0.c2lnbmF0dXJl"


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def smoke(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """scripts/smoke_m4.py loaded as when it runs as a script (the smoke modules, `m4_seeds` and `e2e_setup`
    beside it on `sys.path`), leaving no cached copy of any behind (`test_smoke_m3_script.import_smoke`)."""
    monkeypatch.syspath_prepend(str(SCRIPTS))
    for name in ("smoke_m0", "smoke_m1", "smoke_m2", "smoke_m3", "m4_seeds", "e2e_setup"):
        monkeypatch.delitem(sys.modules, name, raising=False)
        monkeypatch.setitem(sys.modules, name, load(name))
    return load("smoke_m4")


def card(job_id: int, *, saved: bool = False, seen: bool = False) -> dict[str, Any]:
    user = {
        "seen_at": "2026-11-03T10:00:00Z" if seen else None,
        "saved_at": "2026-11-03T10:00:00Z" if saved else None,
        "applied_at": None,
        "hidden_at": None,
        "has_note": False,
    }
    return {"job_id": job_id, "kind": "job", "title": AD_TEXT, "user": user, "score": {"mid": "71.0"}}


class M4Database(Database):
    """`Database` (M3's statements) with the M4 statements: the ranking generation, the smoke's session, the
    grounded candidates, the seeded rejected item and its replay."""

    def __init__(self, smoke: ModuleType, **m3: Any) -> None:
        super().__init__(sys.modules["smoke_m3"], **m3)
        self.smoke = smoke
        self.m4 = sys.modules["m4_seeds"]
        self.e2e = sys.modules["e2e_setup"]
        self.generation = 40
        self.idle = 1_800_000_000.0  # the session's idle expiry (epoch seconds)
        self.quiet = 120.0  # seconds since its last slide
        self.agent: str | None = None  # the sha256 of the user agent the smoke's session was created with
        self.candidates: list[dict[str, Any]] = [
            {
                "job_id": JOB,
                "state": "pending",
                "result_id": None,
                "resumes": False,
                "description": DESCRIPTION,
            }
        ]
        self.target = "published_contested|score_now"
        self.replay = "succeeded|1|linked"
        self.writes: list[str] = []

    def answer(self, sql: str) -> str:
        smoke, m4, e2e = self.smoke, self.m4, self.e2e
        if sql == smoke.GENERATION:
            self.seen.append("GENERATION")
            return str(self.generation)
        if self.agent is not None and sql == smoke.SESSION_OF.format(agent=self.agent):
            self.seen.append("SESSION_OF")
            return f"1|{self.idle}|{self.quiet}"
        if sql == smoke.CANDIDATES:
            self.seen.append("CANDIDATES")
            return json.dumps(self.candidates)
        if sql == m4.GROUNDED_INPUTS.format(job_id=JOB):
            return json.dumps({"revision_id": REVISION, "description": DESCRIPTION, "skill": SKILL})
        if sql == e2e.TARGET_OF_JOB.format(job=JOB):
            self.seen.append("TARGET_OF_JOB")
            return self.target
        if sql == m4.REJECTED_SEED:
            return ""
        if sql == m4.SEED_REJECTED_ITEM:
            self.writes.append("SEED_REJECTED_ITEM")
            return f"BEGIN\nSET\n{RESPONSE}|{ITEM}|1|40\nCOMMIT"
        if sql == smoke.REPLAY_STATE.format(operation=OPERATION, item=ITEM):
            self.seen.append("REPLAY_STATE")
            return self.replay
        if sql.startswith("/* e2e.description_quotes */"):
            return json.dumps(sys.modules["m4_seeds"].grounded_quotes(DESCRIPTION))
        return super().answer(sql)


class M4Stack(Stack):
    """`Stack` (M2's and M3's routes) with every M4 route the smoke uses, answered as the stack answers it,
    with an ad's text and a key in its answers. The variations are attributes."""

    def __init__(self, database: M4Database) -> None:
        super().__init__(database)
        self.db = database
        self.index_html = INDEX_HTML
        self.csp = CSP
        self.asset_cache = "public, max-age=31536000, immutable"
        self.deep_link_html: str | None = None
        self.nope = httpx.Response(
            404, json={"code": "not_found"}, headers={"content-type": "application/problem+json"}
        )
        self.anonymous_openapi = 401
        self.paths = ["/api/jobs", "/api/jobs/{job_id}", "/api/system/status", "/api/settings"]
        self.reordered_refused = False
        self.other_filter = httpx.Response(400, json={"code": "invalid_cursor", "status": 400})
        self.detail = detail_body(sys.modules["m4_seeds"].grounded_quotes(DESCRIPTION))
        self.history_status = 200
        self.seen_on_open_bumps = False
        self.background_slides = False
        self.status_body: dict[str, Any] = {
            "banners": [{"code": "pace_high", "severity": "warning", "params": {}}],
            "next_slots": ["2026-11-04T18:00:00Z", "2026-11-05T06:00:00Z", "2026-11-05T18:00:00Z"],
            "last_run": None,
        }
        self.listed_after_replay = False
        self.replayable = True
        self.queue: dict[str, Any] = {
            "items": [
                {
                    "job_id": 7,
                    "title": AD_TEXT,
                    "company": "Musterfirma",
                    "locations": ["Berlin"],
                    "ad_language": "de",
                    "excerpt": AD_TEXT,
                }
            ],
            "progress": {"labelled": 3, "target": 50},
        }
        self.replayed = False
        self.puts: list[tuple[int, dict[str, Any]]] = []
        self.keys: list[str] = []
        self.agents: list[str] = []
        self.slept: list[float] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        route = (request.method, request.url.path)
        agent = request.headers["user-agent"]
        self.agents.append(agent)
        self.db.agent = hashlib.sha256(agent.encode("utf-8")).hexdigest()
        if "idempotency-key" in request.headers:
            self.keys.append(request.headers["idempotency-key"])
        answer = self.public(request, route)
        if answer is None and route != ("POST", "/api/auth/login"):
            answer = self.m4(request, route)
            if answer is not None:
                assert request.headers["cookie"] == COOKIE, route
                if request.method in ("POST", "PUT"):
                    assert (request.headers["origin"], request.headers["x-jh-request"]) == (
                        "http://localhost:8080",
                        "1",
                    ), route
        if answer is None:
            return super().__call__(request)
        self.seen.append(route)
        return answer

    def public(self, request: httpx.Request, route: tuple[str, str]) -> httpx.Response | None:
        page = {"content-security-policy": self.csp, "content-type": "text/html; charset=utf-8"}
        match route:
            case ("GET", "/"):
                assert request.headers["accept"] == "text/html"
                return httpx.Response(
                    200, text=self.index_html, headers={**page, "cache-control": "no-cache"}
                )
            case ("GET", "/settings"):
                html = self.index_html if self.deep_link_html is None else self.deep_link_html
                return httpx.Response(200, text=html, headers={**page, "cache-control": "no-cache"})
            case ("GET", path) if path == ASSET:
                return httpx.Response(200, text="export {};", headers={"cache-control": self.asset_cache})
            case ("GET", "/api/openapi.json") if "cookie" not in request.headers:
                return httpx.Response(self.anonymous_openapi, json={"code": "unauthenticated"})
        return None

    def m4(self, request: httpx.Request, route: tuple[str, str]) -> httpx.Response | None:
        params = parse_qs(request.url.query.decode("ascii"))
        is_json = request.headers.get("content-type") == "application/json"
        sent: Any = json.loads(request.content) if is_json else None
        db = self.db
        match route:
            case ("GET", "/api/nope"):
                assert request.headers["accept"] == "text/html"
                return self.nope
            case ("GET", "/api/openapi.json"):
                paths: dict[str, Any] = {path: {"get": {}} for path in self.paths}
                return httpx.Response(200, json={"openapi": "3.1.0", "paths": paths})
            case ("GET", "/api/jobs"):
                return self.jobs(params)
            case ("GET", path) if path == f"/api/jobs/{JOB}":
                return httpx.Response(200, json=self.detail)
            case ("GET", path) if path == f"/api/jobs/{JOB}/history":
                return httpx.Response(
                    self.history_status, json={"items": [{"result_id": 3}], "next_cursor": None}
                )
            case ("POST", path) if path == f"/api/jobs/{JOB}/score-now":
                assert sent == {} and request.headers["idempotency-key"]
                db.target = "published_contested|score_now"
                return httpx.Response(202, json={"target_id": 9, "state": "pending"})
            case ("PUT", path) if path.startswith("/api/jobs/") and path.endswith("/actions"):
                assert "idempotency-key" not in request.headers  # §14.1 keys none of these routes
                job_id = int(path.split("/")[3])
                self.puts.append((job_id, sent))
                if sent != {"seen_on_open": True} or self.seen_on_open_bumps:
                    db.generation += 1
                saved = "2026-11-04T12:00:00Z" if sent.get("saved") else None
                job = {"job_id": job_id, "saved_at": saved, "note": AD_TEXT}
                return httpx.Response(200, json={"job": job, "members": []})
            case ("GET", "/api/system/status"):
                if request.headers.get("x-jh-background") != "1" or self.background_slides:
                    if db.quiet >= 60:
                        db.idle, db.quiet = db.idle + db.quiet, 0.0
                return httpx.Response(200, json=self.status_body)
            case ("GET", "/api/system/needs-attention"):
                sections = {
                    name: {"count": 0, "items": []}
                    for name in (
                        "identity_conflicts",
                        "failed_targets",
                        "quarantined_batches",
                        "retrieve_failing",
                        "mass_disappearances",
                        "unscorable_groups",
                        "broken_sources",
                    )
                }
                listed = not self.replayed or self.listed_after_replay
                items = [{"raw_response_id": RESPONSE, "replayable": self.replayable}] if listed else []
                body = {
                    "raw_items": {"count": len(items), "items": items},
                    **sections,
                    "paused_lane": None,
                    "scope_apply_failed": None,
                }
                return httpx.Response(200, json=body)
            case ("POST", "/api/system/raw-items/replay"):
                assert sent == {"raw_response_id": RESPONSE}
                self.replayed = True
                return httpx.Response(202, json={"operation_id": OPERATION, "items": 1})
            case ("GET", "/api/labelling/queue"):
                return httpx.Response(200, json=self.queue)
        return None

    def jobs(self, params: dict[str, list[str]]) -> httpx.Response:
        page = {"ranking_generation": self.db.generation, "ranking_changed": False}
        modes = params.get("work_mode", ["remote", "hybrid", "onsite", "unknown"])
        cursor = params.get("cursor", [None])[0]
        if cursor is None:
            items = [card(FIRST_PAGE[0], saved=True), *(card(job, seen=job == 103) for job in FIRST_PAGE[1:])]
            counts = {"total_estimate": 313, "unscored": 40, "capped": False}
            return httpx.Response(200, json={**page, "items": items, "next_cursor": CURSOR, "counts": counts})
        assert cursor == CURSOR
        if sorted(modes) != sorted(["remote", "hybrid", "onsite", "unknown"]):
            return self.other_filter
        if modes != ["remote", "hybrid", "onsite", "unknown"] and self.reordered_refused:
            return httpx.Response(400, json={"code": "invalid_cursor"})
        items = [card(job) for job in SECOND_PAGE]
        return httpx.Response(200, json={**page, "items": items, "next_cursor": None, "counts": None})


class Docker:
    """`subprocess.run` of the stubbed stack: psql answers from the database, the worker's `printenv` and
    keyed directory (`m4_seeds`: bytes), the golden seed and the user's creation."""

    def __init__(self, database: M4Database) -> None:
        self.database = database
        self.digests = ""
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        self.calls.append(args)
        out, err = "", ""
        if "psql" in args:
            out = self.database.answer(args[-1]) + "\n"
        elif "printenv" in args:
            out = f"{KEYED_DIR}\n" if args[-1] == "JOBHUNTER_FAKE_LLM_KEYED_DIR" else "1\n"
        elif "sha256sum" in " ".join(args):
            out = self.digests
        elif "tar -x" in " ".join(args):
            self.database.writes.append("keyed answers")
            with tarfile.open(fileobj=io.BytesIO(kwargs["input"])) as tar:
                self.digests = "".join(
                    f"{hashlib.sha256(tar.extractfile(member).read()).hexdigest()}  {member.name}\n"  # type: ignore[union-attr]
                    for member in tar.getmembers()
                )
        elif args[-3:] == ["jobhunter", "golden", "seed-adversarial"]:
            out, err = "golden seed-adversarial: cases=10 jobs=10 profiles=10\n", f"key={KEY} {AD_TEXT}\n"
        else:
            assert "run" in args, args  # the user's creation
        if kwargs.get("text"):
            return subprocess.CompletedProcess(args, 0, out, err)
        return subprocess.CompletedProcess(args, 0, out.encode("utf-8"), err.encode("utf-8"))


def stub(smoke: ModuleType, monkeypatch: pytest.MonkeyPatch, stack: M4Stack) -> Docker:
    """The stubbed stack under the smoke: `subprocess.run`, the HTTP transport, the refill window and the
    clock of check 21's wait (its sleep advances the session's quiet time)."""
    docker = Docker(stack.db)
    monkeypatch.setattr(subprocess, "run", docker)
    monkeypatch.setattr(sys.modules["smoke_m3"], "AWAY_FROM_THE_TICK", range(60))
    real_client = httpx.Client

    def client(**kwargs: Any) -> httpx.Client:
        return real_client(transport=httpx.MockTransport(stack), **kwargs)

    monkeypatch.setattr(smoke, "httpx", SimpleNamespace(Client=client, HTTPError=httpx.HTTPError))

    def sleep(seconds: float) -> None:
        stack.slept.append(seconds)
        stack.db.quiet += seconds

    monkeypatch.setattr(smoke, "time", SimpleNamespace(sleep=sleep, monotonic=time.monotonic))
    return docker


def run_against(
    smoke: ModuleType, monkeypatch: pytest.MonkeyPatch, stack: M4Stack, *, exit_code: int = 0
) -> Docker:
    docker = stub(smoke, monkeypatch, stack)
    assert smoke.main() == exit_code
    return docker


def printed(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def run_one(smoke: ModuleType, monkeypatch: pytest.MonkeyPatch, stack: M4Stack, check: str) -> None:
    """The login, then check `check` alone, on the stubbed stack."""
    stub(smoke, monkeypatch, stack)
    names = dict(smoke.CHECKS)
    monkeypatch.setattr(smoke, "CHECKS", [("login", names["login"]), (check, names[check])])
    smoke.main()


def outcome(capsys: pytest.CaptureFixture[str], check: str) -> dict[str, Any]:
    (line,) = [line for line in printed(capsys) if line["check"] == check]
    return line


# ---- the check list and the copied values ------------------------------------------------------------------


def test_the_twenty_three_checks_run_in_order(smoke: ModuleType) -> None:
    assert [name for name, _ in smoke.CHECKS] == CHECK_NAMES
    m3 = sys.modules["smoke_m3"]
    assert smoke.CHECKS[: len(M3_CHECK_NAMES)] == m3.CHECKS  # M3's fifteen, as they are


def test_the_copied_values_are_the_codes(smoke: ModuleType) -> None:
    """The CSP the SPA's answers carry, the work modes, the needs-attention sections, the states Score now
    accepts, the slide interval and the background header: the smoke's copies cannot drift from them."""
    assert smoke.CSP == SECURITY_HEADERS["Content-Security-Policy"]
    assert smoke.WORK_MODES == get_args(WorkMode)
    assert smoke.SECTIONS == tuple(AttentionOut.model_fields)
    assert set(smoke.SCORE_NOW_STATES) == {*ACCEPTED_STATES, *RETRYABLE_STATES} - {"deferred_duplicate"}
    assert smoke.SLIDE_EVERY_S == SLIDE_EVERY.total_seconds()
    assert smoke.BACKGROUND == {deps.BACKGROUND_HEADER: "1"}
    assert sys.modules["m4_seeds"].GROUNDED_JOB_SQL in smoke.CANDIDATES  # the E2E setup's candidate rule


# ---- a whole run -------------------------------------------------------------------------------------------


def test_a_whole_run_prints_twenty_three_ok_lines_and_never_a_secret_or_a_text(
    smoke: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stack = M4Stack(M4Database(smoke))
    run_against(smoke, monkeypatch, stack)
    out = capsys.readouterr().out
    lines = [json.loads(line) for line in out.splitlines()]
    assert [(line["check"], line["ok"]) for line in lines] == [(name, True) for name in CHECK_NAMES]
    quotes = sys.modules["m4_seeds"].grounded_quotes(DESCRIPTION)
    secrets = [TOKEN, sys.modules["smoke_m0"].PASSWORD, *stack.keys, DESCRIPTION, *quotes]
    assert [secret for secret in secrets if secret in out] == []
    assert [text for text in CONTENT if text in out] == []
    assert all(set(line) <= {"check", "ok", "detail"} for line in lines)
    # check 19 wrote the grounded answers, then asked for Score now with a fresh key, then read the detail
    assert stack.db.writes == ["keyed answers", "SEED_REJECTED_ITEM"]
    score_now = stack.seen.index(("POST", f"/api/jobs/{JOB}/score-now"))
    assert stack.seen.index(("GET", f"/api/jobs/{JOB}")) > score_now
    by_check = {line["check"]: line.get("detail") for line in lines}
    assert "5 of 5 criteria" in by_check["job_detail"]
    assert by_check["needs_attention"].startswith(f"raw response {RESPONSE}, raw item {ITEM} seeded")


def test_the_smoke_signs_in_with_a_user_agent_of_its_own(
    smoke: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Check 21 finds the smoke's session by the SHA-256 of the user agent it signed in with
    (`sessions.user_agent_sha256`), which `main` makes unique per run."""
    stack = M4Stack(M4Database(smoke))
    stub(smoke, monkeypatch, stack)
    monkeypatch.setattr(smoke, "CHECKS", [("login", dict(smoke.CHECKS)["login"])])
    assert smoke.main() == 0
    assert smoke.main() == 0
    first, second = stack.agents
    assert first.startswith("jobhunter-smoke-m4/") and second.startswith("jobhunter-smoke-m4/")
    assert first != second


# ---- 16 spa ------------------------------------------------------------------------------------------------


def test_inline_scripts_are_counted(smoke: ModuleType) -> None:
    assert smoke.inline_scripts(INDEX_HTML) == 0
    assert smoke.inline_scripts('<script>window.x = 1</script><script src="/a.js"></script>') == 1
    assert smoke.inline_scripts('<script type="module"></script>') == 1  # no `src`: an inline script
    assert smoke.inline_scripts('<SCRIPT src="/a.js">\nalert(1)\n</SCRIPT>') == 1  # code beside a `src`


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"csp": "default-src 'self'"}, "the §16.6 CSP"),
        ({"index_html": INDEX_HTML.replace('<div id="root">', "<main>")}, '<div id="root">'),
        ({"index_html": INDEX_HTML.replace("</head>", "<script>boot()</script></head>")}, "1 inline script"),
        ({"asset_cache": "no-cache"}, "immutable"),
        ({"deep_link_html": "<html>404</html>"}, "/settings"),
        ({"nope": httpx.Response(404, json={"code": "not_found"})}, "application/problem+json"),
        ({"nope": httpx.Response(200, text=INDEX_HTML)}, "/api/nope"),
    ],
    ids=["csp", "no-root", "inline-script", "asset-cached", "deep-link", "problem-type", "api-deep-linked"],
)
def test_check_16_needs_the_spa_served_safely(
    smoke: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    change: dict[str, Any],
    reason: str,
) -> None:
    stack = M4Stack(M4Database(smoke))
    for name, value in change.items():
        setattr(stack, name, value)
    run_one(smoke, monkeypatch, stack, "spa")
    line = outcome(capsys, "spa")
    assert line["ok"] is False and reason in line["detail"]


# ---- 17 openapi_doc ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"anonymous_openapi": 200}, "expected 401 without the cookie, got 200"),
        ({"paths": ["/api/jobs", "/api/settings"]}, "/api/system/status"),
    ],
    ids=["served-without-a-session", "a-path-missing"],
)
def test_check_17_needs_the_document_for_a_session_only(
    smoke: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    change: dict[str, Any],
    reason: str,
) -> None:
    stack = M4Stack(M4Database(smoke))
    for name, value in change.items():
        setattr(stack, name, value)
    run_one(smoke, monkeypatch, stack, "openapi_doc")
    line = outcome(capsys, "openapi_doc")
    assert line["ok"] is False and reason in line["detail"]


# ---- 18 jobs_list ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"reordered_refused": True}, "reordered work_mode"),
        ({"other_filter": httpx.Response(200, json={"items": [], "next_cursor": None})}, "expected 400"),
        ({"other_filter": httpx.Response(400, json={"code": "validation_failed"})}, "'validation_failed'"),
    ],
    ids=["reorder-refused", "other-filter-paged", "other-code"],
)
def test_check_18_pages_on_with_the_same_filters_only(
    smoke: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    change: dict[str, Any],
    reason: str,
) -> None:
    stack = M4Stack(M4Database(smoke))
    for name, value in change.items():
        setattr(stack, name, value)
    run_one(smoke, monkeypatch, stack, "jobs_list")
    line = outcome(capsys, "jobs_list")
    assert line["ok"] is False and reason in line["detail"]


def test_check_18_sends_the_work_modes_in_both_orders(
    smoke: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stack = M4Stack(M4Database(smoke))
    sent: list[list[str]] = []
    jobs = stack.jobs

    def recorded(params: dict[str, list[str]]) -> httpx.Response:
        sent.append(params["work_mode"])
        return jobs(params)

    stack.jobs = recorded  # type: ignore[method-assign]
    run_one(smoke, monkeypatch, stack, "jobs_list")
    assert outcome(capsys, "jobs_list")["ok"] is True
    order = list(smoke.WORK_MODES)
    assert sent == [order, order, order[::-1], order[:-1]]


# ---- 19 job_detail -----------------------------------------------------------------------------------------


def test_spanned_criteria_count_the_spans_that_slice_their_quotes(smoke: ModuleType) -> None:
    quotes = sys.modules["m4_seeds"].grounded_quotes(DESCRIPTION)
    detail = detail_body(quotes)
    assert smoke.spanned_criteria(detail) == (5, 5, 0)
    # the default recordings quote a synthetic ad: no span, so no criterion counts (F11: never vacuous)
    for criterion in detail["score"]["criteria"]:
        for evidence in criterion["evidence"]:
            evidence.update(quote="Ein erfundenes Zitat.", span_start=None, span_end=None)
    assert smoke.spanned_criteria(detail) == (0, 5, 0)
    # a span that slices another text is counted as wrong
    detail = detail_body(quotes)
    detail["score"]["criteria"][0]["evidence"][0]["span_start"] += 1
    assert smoke.spanned_criteria(detail) == (4, 5, 1)
    assert smoke.spanned_criteria({"description": {"text": DESCRIPTION}, "score": None}) == (0, 0, 0)


@pytest.mark.parametrize(
    ("spans", "history", "reason"),
    [
        ("none", 200, "0 of 5 criteria with description evidence have spans that slice their quotes"),
        ("wrong", 200, "1 description span(s) slice another text"),
        ("right", 404, "the history answered 404"),
    ],
    ids=["no-span", "a-wrong-span", "no-history"],
)
def test_check_19_fails_without_spans_that_slice_or_without_the_history(
    smoke: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    spans: str,
    history: int,
    reason: str,
) -> None:
    stack = M4Stack(M4Database(smoke))
    for criterion in stack.detail["score"]["criteria"]:
        for evidence in criterion["evidence"]:
            if spans == "none":
                evidence.update(span_start=None, span_end=None)
    if spans == "wrong":
        stack.detail["score"]["criteria"][1]["evidence"][0]["span_end"] += 2
    stack.history_status = history
    run_one(smoke, monkeypatch, stack, "job_detail")
    line = outcome(capsys, "job_detail")
    assert line["ok"] is False and reason in line["detail"]


def candidate(job_id: int, state: str, *, resumes: bool = False, result: int | None = None) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "state": state,
        "result_id": result,
        "resumes": resumes,
        "description": DESCRIPTION,
    }


def test_check_19_grounds_the_lowest_candidate_score_now_scores_afresh(
    smoke: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The candidate rule of the E2E setup's step 7 (`m4_seeds.GROUNDED_JOB_SQL`, three quotes): no candidate
    is published grounded yet, so the lowest one whose target Score now takes and that resumes no batch draft
    (a draft of the default recordings) is chosen; one being scored, filtered out or grouped is passed over,
    and so is one without three sentences. Its answers are written first, then Score now with a fresh key."""
    database = M4Database(smoke)
    short = {**candidate(JOB - 5, "pending"), "description": "Kurz. Zu kurz."}
    database.candidates = [
        short,
        candidate(JOB - 4, "pending", resumes=True),
        candidate(JOB - 3, "in_progress"),
        candidate(JOB - 2, "filtered_out"),
        candidate(JOB - 1, "deferred_duplicate"),
        candidate(JOB, "expired_unscored"),
        candidate(JOB + 1, "pending"),
    ]
    stack = M4Stack(database)
    run_one(smoke, monkeypatch, stack, "job_detail")
    line = outcome(capsys, "job_detail")
    assert line["ok"] is True, line
    assert line["detail"].startswith(f"job {JOB} (expired_unscored): grounded answers written; score-now 202")
    assert database.writes == ["keyed answers"]
    assert stack.seen.index(("POST", f"/api/jobs/{JOB}/score-now")) < stack.seen.index(
        ("GET", f"/api/jobs/{JOB}")
    )
    (key,) = stack.keys
    assert key not in line["detail"]


def test_check_19_reads_a_grounded_publish_first(
    smoke: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A candidate already published with its grounded quotes as description evidence (a stack that ran the
    E2E setup, or a second run) is read as it is: nothing written, no Score now."""
    database = M4Database(smoke)
    database.candidates = [candidate(JOB - 1, "pending"), candidate(JOB, "published_contested", result=90)]
    stack = M4Stack(database)
    run_one(smoke, monkeypatch, stack, "job_detail")
    line = outcome(capsys, "job_detail")
    assert line["ok"] is True and line["detail"].startswith(f"job {JOB}: published grounded already")
    assert database.writes == [] and stack.keys == []


def test_check_19_names_the_states_when_no_candidate_can_be_grounded(
    smoke: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    database = M4Database(smoke)
    database.candidates = [candidate(JOB, "pending", resumes=True), candidate(JOB + 1, "filtered_out")]
    stack = M4Stack(database)
    run_one(smoke, monkeypatch, stack, "job_detail")
    line = outcome(capsys, "job_detail")
    assert line["ok"] is False
    assert "candidates=2 with_three_sentences=2" in line["detail"]
    assert "states=['filtered_out', 'pending (resumes a batch draft)']" in line["detail"]
    assert database.writes == [] and stack.keys == []


# ---- 20 actions --------------------------------------------------------------------------------------------


def test_check_20_saves_and_opens_and_restores_both(
    smoke: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`saved: true` on the first unsaved card moves the generation by one; `seen_on_open` on the next card
    moves it by none; then the first is unsaved again and the second, unseen before, unseen again."""
    stack = M4Stack(M4Database(smoke))
    run_one(smoke, monkeypatch, stack, "actions")
    line = outcome(capsys, "actions")
    assert line["ok"] is True, line
    assert stack.puts == [
        (102, {"saved": True}),
        (103, {"seen_on_open": True}),
        (102, {"saved": False}),
    ]
    assert "job 103 was seen already: nothing to restore" in line["detail"]


def test_check_20_restores_an_unseen_card_and_restores_even_after_a_failure(
    smoke: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stack = M4Stack(M4Database(smoke))
    stack.seen_on_open_bumps = True
    jobs = stack.jobs

    def no_seen(params: dict[str, list[str]]) -> httpx.Response:
        answer = jobs(params)
        body = answer.json()
        for item in body["items"]:
            item["user"]["seen_at"] = None
        return httpx.Response(200, json=body)

    stack.jobs = no_seen  # type: ignore[method-assign]
    run_one(smoke, monkeypatch, stack, "actions")
    line = outcome(capsys, "actions")
    assert line["ok"] is False
    assert "seen_on_open on job 103 moved the generation by [1, 1, 1], not 0" in line["detail"]
    assert stack.puts[-2:] == [(102, {"saved": False}), (103, {"seen": False})]


def test_check_20_tolerates_a_concurrent_bump(
    smoke: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Another writer's bump between the two reads makes an attempt count 2; the next attempt decides."""
    database = M4Database(smoke)
    reads = {"n": 0}
    answer = database.answer

    def busy(sql: str) -> str:
        if sql == smoke.GENERATION:
            reads["n"] += 1
            if reads["n"] == 3:  # the read after the first save (the first is the start's)
                database.generation += 1
        return answer(sql)

    database.answer = busy  # type: ignore[method-assign]
    stack = M4Stack(database)
    run_one(smoke, monkeypatch, stack, "actions")
    line = outcome(capsys, "actions")
    assert line["ok"] is True and "saved on job 102 moved it by 1 (attempt 2)" in line["detail"]


# ---- 21 status ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"status_body": {"banners": [], "next_slots": ["a", "b"]}}, "2 next_slots"),
        ({"status_body": {"banners": [], "next_slots": ["a", "b", "c"], "ready": {"ok": True}}}, "ready"),
        ({"status_body": {"next_slots": ["a", "b", "c"]}}, "no banners list"),
        ({"background_slides": True}, "the background request moved idle_expires_at"),
    ],
    ids=["two-slots", "ready-member", "no-banners", "background-slides"],
)
def test_check_21_needs_the_header_shape_and_a_background_request_that_slides_nothing(
    smoke: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    change: dict[str, Any],
    reason: str,
) -> None:
    stack = M4Stack(M4Database(smoke))
    for name, value in change.items():
        setattr(stack, name, value)
    run_one(smoke, monkeypatch, stack, "status")
    line = outcome(capsys, "status")
    assert line["ok"] is False and reason in line["detail"]


def test_check_21_waits_until_the_session_could_slide_then_proves_it_does(
    smoke: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The first request slid the session, so the check waits the slide interval (and a margin) before the
    background request; a foreground request after it slides, so the probe could have moved it."""
    stack = M4Stack(M4Database(smoke))
    run_one(smoke, monkeypatch, stack, "status")
    line = outcome(capsys, "status")
    assert line["ok"] is True, line
    assert stack.slept == [smoke.SLIDE_EVERY_S + smoke.SLIDE_MARGIN_S]
    assert "a foreground request then slid it" in line["detail"]
    statuses = [route for route in stack.seen if route == ("GET", "/api/system/status")]
    assert len(statuses) == 3


def test_check_21_fails_when_no_request_slides_the_session(
    smoke: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A session that no request slides (the probe found another row, say) proves nothing: the check fails."""
    database = M4Database(smoke)
    stack = M4Stack(database)
    status = stack.m4

    def frozen(request: httpx.Request, route: tuple[str, str]) -> httpx.Response | None:
        idle, quiet = database.idle, database.quiet
        answer = status(request, route)
        if route == ("GET", "/api/system/status"):
            database.idle, database.quiet = idle, quiet
        return answer

    stack.m4 = frozen  # type: ignore[method-assign]
    run_one(smoke, monkeypatch, stack, "status")
    line = outcome(capsys, "status")
    assert line["ok"] is False and "a foreground request did not slide it either" in line["detail"]


# ---- 22 needs_attention ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"replayable": False}, f"raw response {RESPONSE} is not listed replayable"),
        ({"listed_after_replay": True}, f"raw response {RESPONSE} is still listed"),
        ({"db.replay": "succeeded|0|rejected"}, "progress.moved = '0'"),
        ({"db.replay": "succeeded|1|pending"}, "not reached within"),
        ({"db.replay": "failed||rejected"}, f"replay operation {OPERATION} failed"),
    ],
    ids=["not-replayable", "still-listed", "nothing-moved", "not-linked", "failed"],
)
def test_check_22_replays_the_seeded_item_to_linked(
    smoke: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    change: dict[str, Any],
    reason: str,
) -> None:
    stack = M4Stack(M4Database(smoke))
    for name, value in change.items():
        if name.startswith("db."):
            setattr(stack.db, name[3:], value)
        else:
            setattr(stack, name, value)
    monkeypatch.setattr(smoke, "REPLAY_WITHIN_S", 0)
    run_one(smoke, monkeypatch, stack, "needs_attention")
    line = outcome(capsys, "needs_attention")
    assert line["ok"] is False and reason in line["detail"]


def test_check_22_needs_every_section(smoke: ModuleType) -> None:
    body = {name: None for name in smoke.SECTIONS}
    assert smoke.missing_sections(body) == []
    del body["broken_sources"], body["paused_lane"]
    assert smoke.missing_sections(body) == ["broken_sources", "paused_lane"]


# ---- 23 labelling ------------------------------------------------------------------------------------------


def test_score_members_are_found_at_any_depth(smoke: ModuleType) -> None:
    assert smoke.score_members({"items": [{"job_id": 1, "excerpt": "a score of 90"}], "progress": {}}) == []
    body = {"items": [{"job_id": 1, "meta": {"score_mid": "71"}}, {"band": "60-75"}], "score": 3}
    assert smoke.score_members(body) == ["items[0].meta.score_mid", "items[1].band", "score"]


@pytest.mark.parametrize(
    ("queue", "reason"),
    [
        ({"items": [{"job_id": 7, "score": {"mid": "71"}}], "progress": {}}, "items[0].score"),
        ({"items": [], "progress": {"labelled": 0, "target": 50}}, "no items"),
    ],
    ids=["a-score", "empty"],
)
def test_check_23_needs_items_without_a_score(
    smoke: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    queue: dict[str, Any],
    reason: str,
) -> None:
    stack = M4Stack(M4Database(smoke))
    stack.queue = queue
    run_one(smoke, monkeypatch, stack, "labelling")
    line = outcome(capsys, "labelling")
    assert line["ok"] is False and reason in line["detail"]


def test_a_failed_check_never_shows_the_session_token(
    smoke: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A value the stack answers that a detail quotes (here a problem code) is scrubbed of the token."""
    stack = M4Stack(M4Database(smoke))
    stack.other_filter = httpx.Response(400, json={"code": TOKEN})
    run_one(smoke, monkeypatch, stack, "jobs_list")
    out = capsys.readouterr().out
    assert TOKEN not in out and "[redacted]" in out


def test_loading_the_smoke_leaves_sys_modules_as_it_was() -> None:
    names = ("smoke_m0", "smoke_m1", "smoke_m2", "smoke_m3", "m4_seeds", "e2e_setup")
    before = {name: sys.modules.get(name) for name in names}
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.syspath_prepend(str(SCRIPTS))
        for name in names:
            monkeypatch.delitem(sys.modules, name, raising=False)
            monkeypatch.setitem(sys.modules, name, load(name))
        smoke = load("smoke_m4")
        assert smoke.smoke_m3 is sys.modules["smoke_m3"] and smoke.e2e_setup is sys.modules["e2e_setup"]
    assert {name: sys.modules.get(name) for name in names} == before
