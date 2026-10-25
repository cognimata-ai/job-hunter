"""scripts/smoke_m2.py (Task 24): its ten checks in order, every compose call it makes on the three files
(the fixtures and the fake LLM over the dev stack, so no provider and no Claude call is ever reached), the
not-fresh hint naming them, the upload posted only once the running worker is on both fakes, and a whole run
against a stubbed stack that prints ten `ok` lines and never the cookie, the password, a CV text or a query
text; `subprocess` and the HTTP transport stubbed."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Final

import httpx
import pytest

from tests.support.profile import SYNTHETIC_CV, SYNTHETIC_PROFILE, SYNTHETIC_QUERIES

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
CLI_USER_EXISTS = "a user already exists; there is only one (`user set-password` changes the password)\n"
CHECK_NAMES: Final = [
    "user_create",
    "login",
    "health_ready",
    "cv_upload",
    "text_review",
    "extract_ready",
    "confirm",
    "proposal_ready",
    "settings_save",
    "spend",
]
OVERLAYS: Final = ["compose.dev.yaml", "compose.fixtures.yaml", "compose.fakellm.yaml"]
TOKEN: Final = "c2Vzc2lvbi10b2tlbi1vZi10aGUtc3R1Yg"  # the stub's session token
COOKIE: Final = f"__Host-jh_session={TOKEN}"
# Texts the stub's answers hold, as the stack's do, and the output must not: the CV, the profile and the
# proposal's query texts.
CONTENT: Final = (
    "Erika Musterfrau",
    "erika.musterfrau@example.org",
    "Musterfirma",
    SYNTHETIC_PROFILE["summary"],
    *(query["text"] for query in SYNTHETIC_QUERIES["queries"]),
)


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def import_smoke(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """scripts/smoke_m2.py loaded as when it runs as a script (`smoke_m0` and `smoke_m1` beside it on
    `sys.path`), leaving no cached copy of either behind (`test_smoke_m1_script.import_smoke`)."""
    monkeypatch.syspath_prepend(str(SCRIPTS))
    for name in ("smoke_m0", "smoke_m1"):
        monkeypatch.delitem(sys.modules, name, raising=False)
        monkeypatch.setitem(sys.modules, name, load(name))
    return load("smoke_m2")


@pytest.fixture
def smoke(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    return import_smoke(monkeypatch)


def compose_files(args: list[str]) -> list[str]:
    return [Path(args[i + 1]).name for i, arg in enumerate(args) if arg == "-f"]


def stub_subprocess(module: ModuleType, monkeypatch: pytest.MonkeyPatch, answer: Any) -> list[list[str]]:
    """Replace `module`'s `subprocess.run` with `answer(args) -> (returncode, stdout, stderr)`; the calls."""
    calls: list[list[str]] = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, *answer(args))

    monkeypatch.setattr(
        module, "subprocess", SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired)
    )
    return calls


def test_the_ten_checks_run_in_order(smoke) -> None:
    assert [name for name, _ in smoke.CHECKS] == CHECK_NAMES


def test_the_one_off_api_container_runs_on_both_overlays(smoke, monkeypatch) -> None:
    calls = stub_subprocess(smoke, monkeypatch, lambda args: (1, "", "boom\n"))
    with pytest.raises(smoke.CheckFailed, match="boom"):
        smoke.check_user_create(smoke.Context(client=None))
    (args,) = calls
    assert args[:4] == ["docker", "compose", "--env-file", str(SCRIPTS.parent / "deploy/images.lock")]
    assert compose_files(args) == OVERLAYS and args[args.index("run") :][:4] == ["run", "--rm", "-T", "api"]


def test_the_not_fresh_hint_names_both_overlays(smoke, monkeypatch) -> None:
    stub_subprocess(smoke, monkeypatch, lambda args: (1, "", CLI_USER_EXISTS))
    with pytest.raises(
        smoke.CheckFailed,
        match=r"stack not fresh: .*compose\.fixtures\.yaml -f deploy/compose\.fakellm\.yaml down -v",
    ):
        smoke.check_user_create(smoke.Context(client=None))


class Posts:
    """An `httpx.Client` stand-in that records each post (none may happen)."""

    def __init__(self) -> None:
        self.posts: list[str] = []

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        self.posts.append(url)
        return httpx.Response(202, json={"draft_id": 1})


@pytest.mark.parametrize(
    ("llm", "sources"),
    [
        ((1, ""), (0, "1\n")),
        ((0, "0\n"), (0, "1\n")),
        ((0, "1\n"), (1, "")),
        ((0, "yes-value\n"), (0, "1\n")),
    ],
    ids=["fake-llm-unset", "fake-llm-off", "fake-sources-unset", "not-the-flag-value"],
)
def test_nothing_is_uploaded_unless_the_worker_is_on_both_fakes(
    smoke, monkeypatch, llm: tuple[int, str], sources: tuple[int, str]
) -> None:
    by_variable = {"JOBHUNTER_FAKE_LLM": llm, "JOBHUNTER_FAKE_SOURCES": sources}
    calls = stub_subprocess(smoke, monkeypatch, lambda args: (*by_variable[args[-1]], ""))
    client = Posts()
    with pytest.raises(smoke.CheckFailed, match="nothing was uploaded") as failed:
        smoke.check_cv_upload(smoke.Context(client=client, cookie="c"))
    assert client.posts == []
    assert "False" in str(failed.value) and "yes-value" not in str(failed.value)  # booleans, never a value
    assert [args[args.index("exec") :] for args in calls] == [
        ["exec", "-T", "worker", "printenv", name] for name in by_variable
    ]
    assert all(compose_files(args) == OVERLAYS for args in calls)


def test_a_failing_exec_is_reported_as_such(smoke, monkeypatch) -> None:
    stub_subprocess(smoke, monkeypatch, lambda args: (1, "", 'service "worker" is not running\n'))
    client = Posts()
    with pytest.raises(smoke.CheckFailed, match="docker compose exec worker: exit 1"):
        smoke.check_cv_upload(smoke.Context(client=client, cookie="c"))
    assert client.posts == []


class Stack:
    """The api of a stubbed dev stack: every route the smoke uses, answered as the stack answers it, with the
    CV, profile and query texts in its answers. `warning` is the dry-run's one warning code."""

    def __init__(self, warning: str = "adzuna_cadence") -> None:
        self.warning = warning
        self.draft = "uploaded"
        self.queries = 0
        self.seen: list[tuple[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        route = (request.method, request.url.path)
        self.seen.append(route)
        if route == ("POST", "/api/auth/login"):
            return httpx.Response(204, headers={"set-cookie": f"{COOKIE}; Path=/; Secure; HttpOnly"})
        assert request.headers["cookie"] == COOKIE, route
        if request.method == "POST":
            assert (request.headers["origin"], request.headers["x-jh-request"]) == (
                "http://localhost:8080",
                "1",
            ), route
        return self.answer(request, route)

    def answer(self, request: httpx.Request, route: tuple[str, str]) -> httpx.Response:
        match route:
            case ("GET", "/api/system/health"):
                return httpx.Response(
                    200, json={"ready": {"ok": True}, "embedder": {"ok": True}, "quota": {}}
                )
            case ("POST", "/api/profile/cv"):
                assert b"%PDF-1.4" in request.content
                self.draft = "text_review"
                return httpx.Response(202, json={"draft_id": 11})
            case ("GET", "/api/profile/drafts/11"):
                report = {"email": 1, "phone": 0, "url": 0, "address": 0, "personal": 0, "name": 1}
                return httpx.Response(
                    200,
                    json={
                        "status": self.draft,
                        "failure_code": None,
                        "strip_report": report,
                        "stripped_text_preview": SYNTHETIC_CV,
                        "draft": SYNTHETIC_PROFILE if self.draft == "ready" else None,
                        "validation_issues": [{"path": "total_years_experience", "code": "years_mismatch"}],
                    },
                )
            case ("POST", "/api/profile/drafts/11/text-review"):
                assert request.headers["idempotency-key"]
                self.draft = "ready"
                return httpx.Response(202, json={"status": "extracting_profile"})
            case ("POST", "/api/profile/drafts/11/confirm"):
                assert json.loads(request.content) == {"expected_rescore_ack": True}
                assert request.headers["idempotency-key"]
                body = {"profile_version_id": 5, "targets_to_create": 0, "est_usd": "0.000000"}
                return httpx.Response(201, json={**body, "bulk_budget_usd": "37.240000"})
            case ("GET", "/api/profile"):
                active = {"id": 5, "version_no": 1, "purged": False, "profile": SYNTHETIC_PROFILE}
                return httpx.Response(200, json={"active": active, "versions": [active]})
            case ("GET", "/api/profile/query-proposals/7"):
                return httpx.Response(200, json={"status": "ready", "items": SYNTHETIC_QUERIES["queries"]})
            case ("POST", "/api/queries"):
                assert json.loads(request.content)["proposal_id"] == 7
                self.queries += 1
                return httpx.Response(201, json={"id": 100 + self.queries, "origin": "llm_proposal"})
            case ("GET", "/api/settings"):
                payload = {"queries": [], "llm": {"cap_usd": "100.00", "overrun_buffer_usd": "2.00"}}
                return httpx.Response(200, json={"version": {"id": 1}, "payload": payload})
            case ("POST", "/api/settings/dry-run"):
                queries = json.loads(request.content)["payload"]["queries"]
                assert queries == [{"query_id": 101, "enabled": True}, {"query_id": 102, "enabled": True}]
                return httpx.Response(200, json={"errors": [], "warnings": [{"code": self.warning}]})
            case ("POST", "/api/settings"):
                sent = json.loads(request.content)
                assert (sent["base_version_id"], sent["acknowledged_warnings"]) == (1, [self.warning])
                assert request.headers["idempotency-key"]
                saved = {"id": 2, "version_no": 2, "effect_classes": ["collection"]}
                return httpx.Response(201, json={"version": saved, "apply_scope_operation_id": None})
            case ("GET", "/api/system/spend"):
                return httpx.Response(200, json={"counted": "0.035704", "cap": "100.00"})
        raise AssertionError(f"the stub stack has no route {route}")


def run_against(smoke: ModuleType, monkeypatch: pytest.MonkeyPatch, stack: Stack) -> int:
    """`main()` on `stack`, with every docker call answered as the fresh stack answers it."""

    def docker(args: list[str]) -> tuple[int, str, str]:
        if "printenv" in args:
            return 0, "1\n", ""
        assert "run" in args, args  # the user's creation: the one other docker call of smoke_m2
        return 0, "", ""

    stub_subprocess(smoke, monkeypatch, docker)
    # smoke_m0's `psql`, which `query_row` runs: the confirm's one proposal, id 7
    stub_subprocess(sys.modules["smoke_m0"], monkeypatch, lambda args: (0, "1|7\n", ""))
    real_client = httpx.Client

    def client(**kwargs: Any) -> httpx.Client:
        return real_client(transport=httpx.MockTransport(stack), **kwargs)

    monkeypatch.setattr(smoke, "httpx", SimpleNamespace(Client=client, HTTPError=httpx.HTTPError))
    code: int = smoke.main()
    return code


def test_a_whole_run_prints_ten_ok_lines_and_never_a_secret_or_a_text(smoke, monkeypatch, capsys) -> None:
    stack = Stack()
    assert run_against(smoke, monkeypatch, stack) == 0
    out = capsys.readouterr().out
    lines = [json.loads(line) for line in out.splitlines()]
    assert [(line["check"], line["ok"]) for line in lines] == [(name, True) for name in CHECK_NAMES]
    assert TOKEN not in out and smoke.PASSWORD not in out
    assert [text for text in CONTENT if text in out] == []
    assert ("GET", "/api/profile/query-proposals/7") in stack.seen and stack.queries == 2


def test_a_detail_never_shows_the_session_token(smoke, monkeypatch, capsys) -> None:
    """A value the stack answers that a detail quotes (here a warning code) is scrubbed of the token."""
    assert run_against(smoke, monkeypatch, Stack(warning=TOKEN)) == 0
    out = capsys.readouterr().out
    assert TOKEN not in out and "[redacted]" in out


@pytest.mark.parametrize("cached", [False, True])
def test_loading_the_smoke_leaves_sys_modules_as_it_was(cached: bool) -> None:
    before = {name: ModuleType(name) if cached else None for name in ("smoke_m0", "smoke_m1")}
    with pytest.MonkeyPatch.context() as outer:
        for name, module in before.items():
            if module is None:
                outer.delitem(sys.modules, name, raising=False)
            else:
                outer.setitem(sys.modules, name, module)
        with pytest.MonkeyPatch.context() as monkeypatch:
            smoke = import_smoke(monkeypatch)
            assert smoke.Context is sys.modules["smoke_m0"].Context
            assert smoke.check_health_ready is sys.modules["smoke_m1"].check_health_ready
        assert {name: sys.modules.get(name) for name in before} == before
