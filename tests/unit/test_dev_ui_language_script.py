"""scripts/dev_ui_language.py (plan M4 Task 20, the visual checks only): `set_language` signs in as `smoke`,
reads the active settings, dry-runs them with `display.ui_language` changed and nothing else, and saves that
over the version it read, with every warning code of the dry-run acknowledged and a fresh `Idempotency-Key`;
`main` prints `{"ui_language", "version_no"}` and never the password or the session cookie. The HTTP
transport is stubbed (`httpx.MockTransport`)."""

import copy
import importlib.util
import json
import sys
import uuid
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import httpx
import pytest

SCRIPTS: Final = Path(__file__).resolve().parents[2] / "scripts"
TOKEN: Final = "c2Vzc2lvbi10b2tlbi1vZi10aGUtc3R1Yg"  # the stub's session token
COOKIE: Final = f"__Host-jh_session={TOKEN}"
ORIGIN: Final = "http://localhost:8080"
BASE_ID: Final = 41
BASE_NO: Final = 12
BASE_VERSION: Final = {
    "id": BASE_ID,
    "version_no": BASE_NO,
    "created_at": "2026-11-02T10:00:00Z",
    "effect_classes": [],
}
ACTIVE_PAYLOAD: Final[dict[str, Any]] = {
    "display": {"ui_language": "en", "timezone": "Europe/Berlin"},
    "scoring": {"batch_min_targets": 5, "preset": "B"},
    "schedule": {"enabled": True, "interval_hours": 6},
    "queries": [{"query_id": 3, "enabled": True}],
}
WARNINGS: Final[list[dict[str, Any]]] = [
    {"code": "llm_cap_binding", "detail": {"expected_scored_per_month": 900}},
    {"code": "paid_refresh_slow", "detail": {"cadence_days": 9.5}},
]


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def script(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """scripts/dev_ui_language.py loaded as when it runs as a script (the smoke modules beside it on
    `sys.path`), leaving no cached copy of any behind (`test_smoke_m3_script.import_smoke`)."""
    monkeypatch.syspath_prepend(str(SCRIPTS))
    for name in ("smoke_m0", "smoke_m1", "smoke_m2"):
        monkeypatch.delitem(sys.modules, name, raising=False)
        monkeypatch.setitem(sys.modules, name, load(name))
    return load("dev_ui_language")


class Stack:
    """The api's answers to the four requests a language change makes, recording each request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.saved_version_no = BASE_NO

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = (request.method, request.url.path)
        if route == ("POST", "/api/auth/login"):
            return httpx.Response(204, headers={"set-cookie": f"{COOKIE}; Path=/; Secure; HttpOnly"})
        if request.headers.get("cookie") != COOKIE:
            return httpx.Response(401, json={"code": "unauthenticated"})
        if route == ("GET", "/api/settings"):
            body = {"version": BASE_VERSION, "payload": ACTIVE_PAYLOAD, "pending_scope": None}
            return httpx.Response(200, json={**body, "scope_applied": True})
        if route == ("POST", "/api/settings/dry-run"):
            return httpx.Response(
                200, json={"errors": [], "warnings": WARNINGS, "projections": {}, "effects": []}
            )
        if route == ("POST", "/api/settings"):
            self.saved_version_no += 1
            version = {"id": 100 + self.saved_version_no, "version_no": self.saved_version_no}
            return httpx.Response(201, json={"version": {**version, "effect_classes": []}})
        return httpx.Response(404)

    def bodies(self, method: str, path: str) -> list[dict[str, Any]]:
        found = [json.loads(r.content) for r in self.requests if (r.method, r.url.path) == (method, path)]
        assert all(isinstance(body, dict) for body in found)
        return found

    def keys(self) -> list[str]:
        return [
            r.headers["idempotency-key"]
            for r in self.requests
            if r.url.path == "/api/settings" and r.method == "POST"
        ]


def client(stack: Stack) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(stack))


def test_set_language_changes_only_display_ui_language_and_acknowledges_every_warning(
    script: ModuleType,
) -> None:
    stack = Stack()
    with client(stack) as c:
        version_no = script.set_language(c, "de")
    assert version_no == BASE_NO + 1
    assert [(r.method, r.url.path) for r in stack.requests] == [
        ("POST", "/api/auth/login"),
        ("GET", "/api/settings"),
        ("POST", "/api/settings/dry-run"),
        ("POST", "/api/settings"),
    ]
    expected = copy.deepcopy(ACTIVE_PAYLOAD)
    expected["display"]["ui_language"] = "de"
    [preview] = stack.bodies("POST", "/api/settings/dry-run")
    [save] = stack.bodies("POST", "/api/settings")
    assert preview == {"payload": expected}
    assert save["payload"] == expected
    assert save["base_version_id"] == BASE_ID
    assert sorted(save["acknowledged_warnings"]) == sorted(w["code"] for w in WARNINGS)
    for request in stack.requests[2:]:  # the two writes: the session, the origin and the CSRF marker
        assert request.headers["origin"] == ORIGIN and request.headers["x-jh-request"] == "1"
    [key] = stack.keys()
    assert str(uuid.UUID(key)) == key


def test_every_save_gets_a_fresh_idempotency_key(script: ModuleType) -> None:
    stack = Stack()
    with client(stack) as c:
        script.set_language(c, "de")
        script.set_language(c, "en")
    first, second = stack.keys()
    assert first != second


def test_a_language_other_than_en_or_de_is_refused_before_any_request(script: ModuleType) -> None:
    stack = Stack()
    with client(stack) as c, pytest.raises(ValueError, match="fr"):
        script.set_language(c, "fr")
    assert stack.requests == []


def test_main_prints_the_language_and_the_version_and_no_secret(
    script: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stack = Stack()
    real_client = httpx.Client
    monkeypatch.setattr(
        script.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(stack), **kw)
    )
    monkeypatch.setattr(sys, "argv", ["dev_ui_language.py", "de"])
    assert script.main() == 0
    out = capsys.readouterr().out
    assert json.loads(out) == {"ui_language": "de", "version_no": BASE_NO + 1}
    assert TOKEN not in out and sys.modules["smoke_m0"].PASSWORD not in out


@pytest.mark.parametrize("argv", [[], ["fr"], ["de", "en"]])
def test_main_needs_exactly_en_or_de(
    script: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["dev_ui_language.py", *argv])
    assert script.main() == 2
    assert "usage" in capsys.readouterr().err
