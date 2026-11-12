"""scripts/smoke_m5.py (plan M5 Task 33): its thirteen checks in order, a whole run against a stubbed stack
that prints thirteen `ok` lines and never a password, a session cookie or token, a key, an ad's text or a CV,
the teardown after a failure, a data error or an interrupt too, the values it copies pinned to the code's, its
imports from the other smoke scripts, and its step in prodstack.yml. No stack starts: `subprocess.run` (the
script's own compose calls and `tests/support/prodstack.py`'s helpers) and the HTTP transport are stubbed
(`Stack`), and so is `time.sleep`."""

import ast
import importlib.util
import io
import json
import ssl
import subprocess
import sys
import zipfile
from collections.abc import Callable
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Final, get_args

import httpx
import pytest
import yaml
from jobhunter.api.edge import SECURITY_HEADERS
from jobhunter.api.routes import privacy
from jobhunter.auth import sessions
from jobhunter.config import Role
from jobhunter.observability import deadman, ntfy
from jobhunter.observability.catalogue import CATALOGUE, push_priority
from jobhunter.observability.status import BannerCode
from jobhunter.privacy.export import EXPORT_FILES

from tests.support import prodstack
from tests.support.profile import SYNTHETIC_CV

REPO: Final = Path(__file__).resolve().parents[2]
SCRIPTS: Final = REPO / "scripts"
CHECK_NAMES: Final = [
    "stack_up",
    "tls",
    "user",
    "collect_and_profile",
    "alert_dispatch",
    "deadman",
    "backup_set",
    "source_purge",
    "manifest_shipped",
    "restore",
    "export",
    "delete_everything",
    "teardown",
]
# The ids the stubbed stack answers with.
RUN, DRAFT, VERSION, ALERT, SCHEDULED, SET, PURGE, EXPORT, DELETE = 41, 7, 3, 55, 42, 2, 9, 4, 10
SNAPSHOT: Final = "4c3b2a1908f7e6d5c4b3a2918f7e6d5c4b3a2918f7e6d5c4b3a2918f7e6d5c4"
PURGE_UUID: Final = "0b5d0a8e-6c43-4f1b-9d2e-7a1c3b5e9f00"
DELETE_UUID: Final = "1c6e1b9f-7d54-4a2c-8e3f-8b2d4c6f0a11"
# What must never be printed: the check's uuid and the ntfy topic (secret configuration), a data key id, the
# tokens of the sessions (one per login), an ad's text and a CV's (the export's content).
CHECK_UUID: Final = "2f4e1c9a-6b1d-4c35-9a0e-5d7b8e3f1a20"
TOPIC: Final = "jobhunter-test-topic-0f3a"
KEY_ID: Final = "a1b2c3d4e5f60718"
AD_TEXT: Final = "Senior Kotlin engineer for an invented logistics start-up, an ad no fixture holds"
CV_LINES: Final = [line for line in SYNTHETIC_CV.splitlines() if len(line.strip()) > 12]
RESTORE: Final = ["--profile", "restore", "run", "--rm", "-T", "restore"]


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def smoke(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """scripts/smoke_m5.py loaded as when it runs as a script (`smoke_m0` and `smoke_m1` beside it on
    `sys.path`), leaving no cached copy of either behind (`test_smoke_m1_script.import_smoke`)."""
    monkeypatch.syspath_prepend(str(SCRIPTS))
    for name in ("smoke_m0", "smoke_m1"):
        monkeypatch.delitem(sys.modules, name, raising=False)
        monkeypatch.setitem(sys.modules, name, load(name))
    return load("smoke_m5")


def done(args: list[str], stdout: str = "", returncode: int = 0, stderr: str = "") -> Any:
    return subprocess.CompletedProcess(args, returncode, stdout, stderr)


def zip_of(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return buffer.getvalue()


class Stack:
    """Sites A (`PT`) and B (`PB`) and the S3 stand-in as the smoke sees them: every compose call and query it
    makes, and every request of its browser, answered as a healthy stack answers them. Each manifest is
    `executing` when first read, then `completed`, then `shipped` once the site's `backup ship` ran; each
    export `running`, then `ready`. `fail(predicate, answer)` makes the first call `predicate` matches answer
    `answer` instead."""

    def __init__(self, smoke: ModuleType) -> None:
        self.smoke = smoke
        self.calls: list[list[str]] = []
        self.inputs: list[str | None] = []
        self.requests: list[httpx.Request] = []
        self.site = "A"
        self.tokens: dict[str, str] = {}  # each session's token, by site
        self.logins = 0
        self.manifests: dict[int, list[str]] = {}  # each manifest's statuses still to come, the last kept
        self.exports: dict[int, list[str]] = {}
        self.stub_entries: list[dict[str, object]] = []
        self.watermark = 0
        self.purged = False
        self.failure: tuple[Callable[[list[str]], bool], Callable[[list[str]], Any]] | None = None

    # ---- subprocess ----------------------------------------------------------------------------------------

    def fail(self, predicate: Callable[[list[str]], bool], answer: Callable[[list[str]], Any]) -> None:
        self.failure = (predicate, answer)

    def run(self, args: list[str], **kwargs: Any) -> Any:
        self.calls.append(list(args))
        self.inputs.append(kwargs.get("input"))
        if self.failure is not None and self.failure[0](args):
            answer, self.failure = self.failure[1], None
            return answer(args)
        if args[:2] == ["docker", "ps"] or args[:3] == ["docker", "network", "ls"]:
            return done(args)  # other_stack_up: no other stack
        for project, argv in (("A", prodstack.PT), ("B", prodstack.PB), ("S3", prodstack.S3)):
            if args[: len(argv)] == argv:
                return self.compose(project, args, args[len(argv) :])
        raise AssertionError(f"an unexpected command: {args}")

    def compose(self, project: str, args: list[str], rest: list[str]) -> Any:
        smoke = self.smoke
        if rest[:3] == ["--profile", "*", "down"] or rest[:1] == ["down"] or rest[:1] == ["up"]:
            return done(args)
        if rest[:4] == ["exec", "-T", "postgres", "sh"]:
            return done(args, self.sql(project, rest[-1]))
        if rest[:3] == ["exec", "-T", "caddy"]:
            return done(args, "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n")
        if rest[:4] == ["exec", "-T", "worker", "printenv"]:
            return done(args, "1\n")
        if rest[:3] == ["exec", "-T", "stubsvc"]:
            if smoke.STUB_CHECK in rest:
                assert rest[-1] == CHECK_UUID
                return done(args, json.dumps({"timeout": 21600, "grace": 5400, "status": "up"}))
            return done(args, json.dumps(self.stub_entries))
        if rest[:4] == ["run", "--rm", "--no-deps", "-T"] and "startup-check" in rest:
            return done(args, '{"event": "startup_check_passed"}\n')
        if rest[:5] == ["run", "--rm", "--no-deps", "-T", "backup"]:
            return self.backup(project, args, rest[5:])
        if rest[:4] == ["run", "--rm", "--no-deps", "-T"] and rest[5:] == [
            "jobhunter",
            "ops",
            "deadman-sync",
            "--show",
        ]:
            show = {"timeout_s": 21600, "grace_s": 5400, "paused": False, "source": "schedule_state"}
            return done(args, json.dumps(show))
        if rest == ["run", "--rm", "-T", "api", *smoke.USER_CREATE]:
            return done(args)
        if rest == RESTORE:
            logged = [
                {"event": "restore_set_staged", "set_id": SET, "data_key_ids": [KEY_ID]},
                {"event": "restore_verified", "phase": "restored", "ok": True, "failures": []},
                {"event": "restore_verified", "phase": "replayed", "ok": True, "failures": []},
            ]
            stderr = "\n".join(
                ["Container jobhunter-prodtest-b-postgres-1 Running", *map(json.dumps, logged)]
            )
            printed = {"restored": True, "set_id": SET, "data_key_ids": [KEY_ID]}
            return done(args, json.dumps(printed), stderr=stderr)
        raise AssertionError(f"an unexpected compose call on {project}: {rest}")

    def backup(self, project: str, args: list[str], rest: list[str]) -> Any:
        if rest == ["jobhunter", "backup", "init"]:
            return done(args, json.dumps({"created": True}))
        if rest == ["jobhunter", "backup", "set"]:
            return done(args, json.dumps({"set_id": SET}))
        if rest == ["jobhunter", "backup", "ship"]:
            for statuses in self.manifests.values():
                if statuses == ["completed"]:
                    statuses[:] = ["shipped"]
            return done(args, json.dumps({"shipped": True, "snapshot_id": SNAPSHOT, "files": 1}))
        if rest[:3] == ["python", "-I", "-c"] and rest[3] == self.smoke.SNAPSHOTS:
            assert rest[4:] == [f"set:{SET}"]
            listed = [{"id": SNAPSHOT, "tags": ["set", f"set:{SET}"], "paths": ["/data/staging/set"]}]
            return done(args, json.dumps(listed))
        raise AssertionError(f"an unexpected backup call on {project}: {rest}")

    def status_of(self, manifest_id: int) -> str:
        statuses = self.manifests[manifest_id]
        status = statuses.pop(0) if len(statuses) > 1 else statuses[0]
        if manifest_id == DELETE and status == "completed":
            self.tokens.clear()  # delete everything's completion ends every session
        return status

    def sql(self, project: str, sql: str) -> str:
        smoke = self.smoke
        assert project in ("A", "B")
        if sql == smoke.SEED_INPUTS:
            return "1|2|3|4\n"
        if sql == smoke.RUN_STATUS.format(run_id=RUN):
            return "succeeded\n"
        if sql == smoke.ADZUNA_POSTINGS:
            return "0\n" if self.purged or project == "B" else "12\n"
        if sql == smoke.INSERT_ALERT:
            push = {
                "method": "POST",
                "path": f"/ntfy/{TOPIC}",
                "Title": smoke.ALERT_TITLE,
                "Priority": "3",
                "Tags": "warn",
                "Click": "https://localhost:8443/settings#alerts",
                "authorization": True,
                "api_key": False,
                "body_length": 41,
            }
            self.stub_entries.append(push)
            return f"{ALERT}\n"
        if sql == smoke.ALERT_NOTIFIED.format(alert=ALERT):
            return "t\n"
        if sql == smoke.SCHEDULE:
            return "21600|t\n"
        if sql == smoke.INSERT_RUN:
            ping = {"method": "GET", "path": f"/hc/ping/{CHECK_UUID}", "authorization": False}
            self.stub_entries.append(ping)
            self.watermark = SCHEDULED
            return f"{SCHEDULED}\n"
        if sql == smoke.WATERMARK:
            return f"{self.watermark}\n"
        if sql == smoke.BACKUP_SET.format(set_id=SET):
            return f"succeeded|{SNAPSHOT}\n"
        if sql == smoke.MANIFEST.format(manifest_id=PURGE):
            return f"{self.status_of(PURGE)}|{PURGE_UUID}\n"
        if sql == smoke.MANIFEST.format(manifest_id=DELETE):
            return f"{self.status_of(DELETE)}|{DELETE_UUID}\n"
        if sql == smoke.REPLAY_ENTRIES.format(manifest_uuid=PURGE_UUID):
            return "1\n"
        if sql == smoke.USERS:
            return "1\n"
        raise AssertionError(f"an unexpected statement: {sql[:80]}")

    # ---- HTTP ----------------------------------------------------------------------------------------------

    def client(self, argv: list[str]) -> httpx.Client:
        """The stubbed `site_client(argv)`: a client of site A or B."""
        site = "A" if argv == prodstack.PT else "B"
        assert argv in (prodstack.PT, prodstack.PB)

        def handler(request: httpx.Request) -> httpx.Response:
            self.site = site
            self.requests.append(request)
            return self.answer(site, request)

        return httpx.Client(
            base_url=self.smoke.ORIGIN,
            transport=httpx.MockTransport(handler),
            headers={"Origin": self.smoke.ORIGIN, "X-JH-Request": "1"},
        )

    def session_ok(self, site: str, request: httpx.Request) -> bool:
        token = self.tokens.get(site)
        return token is not None and request.headers.get("cookie") == f"__Host-jh_session={token}"

    def answer(self, site: str, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        cleared = {"set-cookie": sessions.CLEARED_SESSION_COOKIE}
        if (method, path) == ("GET", "/healthz"):
            return httpx.Response(200, headers=dict(SECURITY_HEADERS), json={})
        if (method, path) == ("POST", "/api/auth/login"):
            assert json.loads(request.content) == {"username": "smoke", "password": self.smoke.PASSWORD}
            self.logins += 1
            token = f"tok{self.logins}-QmFzZTY0VG9rZW5WYWx1ZQ"
            self.tokens[site] = token
            cookie = f"__Host-jh_session={token}; Path=/; Secure; HttpOnly; SameSite=Strict"
            return httpx.Response(204, headers={"set-cookie": cookie})
        if not self.session_ok(site, request):
            return httpx.Response(401, headers=cleared, json={"code": "unauthenticated"})
        if method == "POST":
            assert request.headers["idempotency-key"].startswith("smoke-m5-")
            assert (request.headers["origin"], request.headers["x-jh-request"]) == (self.smoke.ORIGIN, "1")
        return self.route(site, method, path, request)

    def route(self, site: str, method: str, path: str, request: httpx.Request) -> httpx.Response:
        body = (
            json.loads(request.content) if request.headers.get("content-type") == "application/json" else {}
        )
        match method, path:
            case "GET", "/api/auth/session":
                return httpx.Response(200, json={"username": "smoke", "totp_enabled": False})
            case "POST", "/api/runs":
                assert body == {"include_paid": True}
                return httpx.Response(202, json={"run": {"id": RUN, "status": "queued"}, "created": True})
            case "POST", "/api/profile/cv":
                return httpx.Response(202, json={"draft_id": DRAFT})
            case "GET", "/api/profile/drafts/7":
                ready = any(r.url.path.endswith("/text-review") for r in self.requests)
                stripped = "\n".join(CV_LINES)  # what a careless detail could quote
                return httpx.Response(
                    200, json={"status": "ready" if ready else "text_review", "text": stripped}
                )
            case "POST", "/api/profile/drafts/7/text-review":
                return httpx.Response(202, json={"status": "extracting_profile"})
            case "POST", "/api/profile/drafts/7/confirm":
                assert body == {"expected_rescore_ack": True}
                return httpx.Response(201, json={"profile_version_id": VERSION, "targets_to_create": 12})
            case "GET", "/api/profile":
                return httpx.Response(200, json={"active": {"id": VERSION, "version_no": 1}})
            case "POST", "/api/privacy/purge-source":
                assert body == {
                    "source": "adzuna",
                    "confirm": "PURGE ADZUNA",
                    "password": self.smoke.PASSWORD,
                }
                self.manifests[PURGE] = ["executing", "completed"]
                return httpx.Response(202, json={"manifest_id": PURGE})
            case "GET", "/api/privacy/manifests/9":
                status = self.status_of(PURGE)
                self.purged = self.purged or status != "executing"
                return httpx.Response(200, json={"id": PURGE, "status": status})
            case "GET", "/api/system/status":
                unshipped = site == "A" and self.manifests.get(PURGE) == ["completed"]
                banners = (
                    [{"code": "deletion_unshipped", "severity": "warn", "params": {}}] if unshipped else []
                )
                return httpx.Response(200, json={"banners": banners, "next_slots": []})
            case "POST", "/api/privacy/export":
                assert site == "B" and body == {}
                self.exports[EXPORT] = ["running", "ready"]
                return httpx.Response(202, json={"export_id": EXPORT})
            case "GET", "/api/privacy/exports/4":
                if request.url.params.get("download") == "1":
                    archive = zip_of(
                        {"README.txt": "what each file holds", "jobs.json": json.dumps([AD_TEXT])}
                    )
                    return httpx.Response(200, headers={"content-type": "application/zip"}, content=archive)
                statuses = self.exports[EXPORT]
                return httpx.Response(
                    200, json={"status": statuses.pop(0) if len(statuses) > 1 else statuses[0]}
                )
            case "POST", "/api/privacy/delete-everything":
                if body.get("confirm") != "DELETE EVERYTHING":
                    return httpx.Response(422, json={"code": "confirmation_mismatch"})
                assert body == {"confirm": "DELETE EVERYTHING", "password": self.smoke.PASSWORD}
                self.manifests[DELETE] = ["executing", "completed"]
                return httpx.Response(202, json={"manifest_id": DELETE})
            case "GET", "/api/jobs":
                gone = DELETE in self.manifests
                items = [] if gone else [{"job_id": 1, "title": AD_TEXT}]
                return httpx.Response(200, json={"items": items, "next_cursor": None})
        raise AssertionError(f"an unexpected request: {method} {path}")


@pytest.fixture
def stack(smoke: ModuleType, monkeypatch: pytest.MonkeyPatch) -> Stack:
    world = Stack(smoke)
    monkeypatch.setattr(subprocess, "run", world.run)
    monkeypatch.setattr(smoke.time, "sleep", lambda _: None)
    monkeypatch.setattr(smoke, "site_client", world.client)
    return world


def lines_of(capsys: pytest.CaptureFixture[str]) -> tuple[list[dict[str, Any]], str]:
    out = capsys.readouterr().out
    return [json.loads(line) for line in out.splitlines()], out


def leaked(stack: Stack, smoke: ModuleType, out: str) -> list[str]:
    """Which secret or content the output holds: the password, a session token, an `Idempotency-Key`, the
    check's uuid, the topic, a data key id, the ad's text or a line of the CV."""
    keys = [r.headers["idempotency-key"] for r in stack.requests if "idempotency-key" in r.headers]
    tokens = [f"tok{n}-QmFzZTY0VG9rZW5WYWx1ZQ" for n in range(1, stack.logins + 1)]
    secrets = [smoke.PASSWORD, *tokens, *keys, CHECK_UUID, TOPIC, KEY_ID, AD_TEXT, *CV_LINES]
    assert keys and tokens
    return [secret for secret in secrets if secret in out]


TEARDOWN: Final = [
    [*prodstack.PT, "--profile", "*", "down", "-v", "--remove-orphans"],
    [*prodstack.PB, "--profile", "*", "down", "-v", "--remove-orphans"],
    [*prodstack.S3, "down", "-v", "--remove-orphans"],
]


# ---- the checks --------------------------------------------------------------------------------------------


def test_the_thirteen_checks_in_order(smoke: ModuleType) -> None:
    assert [name for name, _ in smoke.CHECKS] == CHECK_NAMES


def test_a_whole_run_prints_thirteen_ok_lines_and_no_secret(
    smoke: ModuleType, stack: Stack, capsys: pytest.CaptureFixture[str]
) -> None:
    assert smoke.main() == 0
    lines, out = lines_of(capsys)
    assert [(line["check"], line["ok"]) for line in lines] == [(name, True) for name in CHECK_NAMES]
    assert leaked(stack, smoke, out) == []
    # the password went to `user create` on its stdin, never on a command line
    (created,) = [i for i, call in enumerate(stack.calls) if "create" in call]
    assert stack.inputs[created] == f"{smoke.PASSWORD}\n"
    assert not [call for call in stack.calls if smoke.PASSWORD in " ".join(call)]
    # the order the sites take: the leftovers removed and the stand-in first; A lost before B restores
    first_up = next(call for call in stack.calls if "up" in call)
    assert stack.calls[:3] == TEARDOWN and first_up == [*prodstack.S3, "up", "-d", "--wait"]
    restore = stack.calls.index([*prodstack.PB, *RESTORE])
    assert stack.calls[restore - 2] == TEARDOWN[0]  # PT down -v, then PB's postgres, then the restore
    assert stack.calls[restore - 1] == [*prodstack.PB, "up", "-d", "--no-build", "--wait", "postgres"]
    assert stack.calls[-3:] == TEARDOWN
    # the backup scheduler never runs on either site: every backup command is a one-off container
    ups = [call for call in stack.calls if call[-len(smoke.SERVICES) :] == list(smoke.SERVICES)]
    assert len(ups) == 2 and not [call for call in stack.calls if "scheduler" in call]


def test_the_checks_hand_their_results_on(
    smoke: ModuleType, stack: Stack, capsys: pytest.CaptureFixture[str]
) -> None:
    smoke.main()
    lines, _ = lines_of(capsys)
    detail = {line["check"]: line.get("detail", "") for line in lines}
    assert (
        f"paid run {RUN}" in detail["collect_and_profile"]
        and "12 Adzuna postings" in detail["collect_and_profile"]
    )
    assert "timeout 21600 s, grace 5400 s" in detail["deadman"]
    assert f"set {SET} succeeded" in detail["backup_set"]
    assert (
        f"manifest {PURGE} completed" in detail["source_purge"] and "banner shown" in detail["source_purge"]
    )
    assert f"set {SET} restored into PB" in detail["restore"] and "1 replay_log entry" in detail["restore"]
    assert "names README.txt" in detail["export"]
    assert "the old session 401" in detail["delete_everything"]
    # B's sessions: the login after the restore, then the one after delete everything
    assert stack.logins == 3


# ---- the teardown ------------------------------------------------------------------------------------------


def refuse(stderr: str) -> Callable[[list[str]], Any]:
    return lambda args: done(args, returncode=1, stderr=stderr)


def is_restore(args: list[str]) -> bool:
    return args == [*prodstack.PB, *RESTORE]


@pytest.mark.parametrize(
    ("failing", "predicate"),
    [
        ("stack_up", lambda args: "up" in args and args[-1] == "stubsvc"),  # $PT up
        ("deadman", lambda args: "deadman-sync" in args),
        ("restore", is_restore),
        ("delete_everything", lambda args: args[: len(prodstack.PB)] == prodstack.PB and args[-1] == "ship"),
    ],
)
def test_a_failed_check_still_tears_down_everything(
    smoke: ModuleType, stack: Stack, capsys: pytest.CaptureFixture[str], failing: str, predicate: Any
) -> None:
    stack.fail(predicate, refuse("Error response from daemon: boom"))
    assert smoke.main() == 1
    lines, _ = lines_of(capsys)
    index = CHECK_NAMES.index(failing)
    assert [(line["check"], line["ok"]) for line in lines] == [
        *((name, True) for name in CHECK_NAMES[:index]),
        (failing, False),
        ("teardown", True),
    ]
    assert "boom" in lines[index]["detail"]
    assert stack.calls[-3:] == TEARDOWN  # PT, PB and the stand-in, after the failure


def test_a_failure_detail_never_shows_the_password_or_a_session(
    smoke: ModuleType, stack: Stack, capsys: pytest.CaptureFixture[str]
) -> None:
    # the restore's stderr quoting the password and the session cookie of site A
    def quoting(args: list[str]) -> Any:
        token = stack.tokens["A"]
        stderr = f"line 1\npassword {smoke.PASSWORD}\ncookie __Host-jh_session={token} token {token}"
        return done(args, returncode=1, stderr=stderr)

    stack.fail(is_restore, quoting)
    assert smoke.main() == 1
    lines, out = lines_of(capsys)
    assert lines[CHECK_NAMES.index("restore")]["detail"].count("[redacted]") == 3
    assert leaked(stack, smoke, out) == []


@pytest.mark.parametrize("secret", ["password", "token"])
def test_a_secret_at_the_cut_of_a_stderr_tail_leaves_no_part_of_it(
    smoke: ModuleType, stack: Stack, capsys: pytest.CaptureFixture[str], secret: str
) -> None:
    """`smoke_m0.stderr_tail` keeps the first 400 characters of a command's last three stderr lines, and
    `emit` scrubs only what is left: a secret across that cut is scrubbed first, so not even its start is
    printed."""
    cut = 400
    head = "x" * (cut - 5)

    def straddling(args: list[str]) -> Any:
        value = smoke.PASSWORD if secret == "password" else stack.tokens["A"]
        return done(args, returncode=1, stderr=f"{head}{value} and the rest\n")

    stack.fail(is_restore, straddling)
    assert smoke.main() == 1
    lines, out = lines_of(capsys)
    value = smoke.PASSWORD if secret == "password" else stack.tokens["A"]
    # unscrubbed, the cut keeps the secret's first five characters: the case this guards against
    assert sys.modules["smoke_m0"].stderr_tail(done([], stderr=f"{head}{value}")).endswith(value[:5])
    detail = lines[CHECK_NAMES.index("restore")]["detail"]
    assert detail == f"restore run: exit 1: {head}[reda"
    assert value[:5] not in out and leaked(stack, smoke, out) == []


def test_a_helper_s_failure_is_reported_short_and_scrubbed(
    smoke: ModuleType, stack: Stack, capsys: pytest.CaptureFixture[str]
) -> None:
    # prodstack.psql fails with `pytest.fail`, quoting the command and both streams whole
    def failing(args: list[str]) -> Any:
        return done(args, "a row\n" * 50, returncode=3, stderr=f"ERROR: boom\npassword {smoke.PASSWORD}\n")

    stack.fail(lambda args: args[-1] == smoke.INSERT_ALERT, failing)
    assert smoke.main() == 1
    lines, out = lines_of(capsys)
    (alert,) = [line for line in lines if line["check"] == "alert_dispatch"]
    assert alert["ok"] is False and "exited 3" in alert["detail"] and "[redacted]" in alert["detail"]
    assert len(alert["detail"]) <= smoke.DETAIL_CHARS and "a row" not in alert["detail"]
    assert (lines[-1]["check"], lines[-1]["ok"]) == ("teardown", True)
    assert leaked(stack, smoke, out) == []


def test_a_data_error_fails_its_check_and_still_tears_down(
    smoke: ModuleType, stack: Stack, capsys: pytest.CaptureFixture[str]
) -> None:
    stack.fail(lambda args: args[-1] == "set", lambda args: done(args, "not json\n"))
    assert smoke.main() == 1
    lines, _ = lines_of(capsys)
    assert lines[-2] == {"check": "backup_set", "ok": False, "detail": "unexpected data (JSONDecodeError)"}
    assert lines[-1]["check"] == "teardown" and lines[-1]["ok"] is True
    assert stack.calls[-3:] == TEARDOWN


def test_an_interrupt_still_tears_down(
    smoke: ModuleType, stack: Stack, capsys: pytest.CaptureFixture[str]
) -> None:
    def interrupt(args: list[str]) -> Any:
        raise KeyboardInterrupt

    stack.fail(lambda args: "deadman-sync" in args, interrupt)
    with pytest.raises(KeyboardInterrupt):
        smoke.main()
    lines, _ = lines_of(capsys)
    assert lines[-1]["check"] == "teardown" and lines[-1]["ok"] is True
    assert stack.calls[-3:] == TEARDOWN


def test_the_teardown_runs_every_step_when_one_fails(
    smoke: ModuleType, stack: Stack, capsys: pytest.CaptureFixture[str]
) -> None:
    stack.calls.clear()
    stack.fail(lambda args: args == TEARDOWN[0], refuse("PT is stuck"))
    assert smoke.run_check(smoke.Context(httpx.Client()), "teardown", smoke.check_teardown) is False
    assert stack.calls == TEARDOWN  # PB and the stand-in all the same
    (line,) = [json.loads(text) for text in capsys.readouterr().out.splitlines()]
    assert line == {"check": "teardown", "ok": False, "detail": "PT: down -v: exit 1: PT is stuck"}


# ---- the site's client -------------------------------------------------------------------------------------


def test_the_client_trusts_the_site_s_caddy_root_only(
    smoke: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    present = iter([1, 0])  # the root is made a moment after Caddy started

    def run(args: list[str], **kwargs: Any) -> Any:
        calls.append(args)
        if args[-3:-1] == ["test", "-f"]:
            return done(args, returncode=next(present))
        return done(args, "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n")

    loaded: list[Path] = []
    context = ssl.create_default_context()

    def create_default_context(*, cafile: str) -> ssl.SSLContext:
        assert "BEGIN CERTIFICATE" in Path(cafile).read_text(encoding="ascii")
        loaded.append(Path(cafile))
        return context

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(smoke.time, "sleep", lambda _: None)
    monkeypatch.setattr(smoke, "ssl", SimpleNamespace(create_default_context=create_default_context))
    created: list[dict[str, Any]] = []
    monkeypatch.setattr(smoke.httpx, "Client", lambda **kwargs: created.append(kwargs))
    smoke.site_client(prodstack.PB)
    assert [call[len(prodstack.PB) :] for call in calls] == [
        ["exec", "-T", "caddy", "test", "-f", prodstack.CADDY_ROOT],
        ["exec", "-T", "caddy", "test", "-f", prodstack.CADDY_ROOT],
        ["exec", "-T", "caddy", "cat", prodstack.CADDY_ROOT],
    ]
    (root,) = loaded
    assert not root.exists()  # the copy of the root is removed once read
    (kwargs,) = created
    assert kwargs["verify"] is context and kwargs["base_url"] == "https://localhost:8443"
    assert kwargs["headers"] == {"Origin": "https://localhost:8443", "X-JH-Request": "1"}
    assert kwargs["trust_env"] is False


# ---- the values it copies and what it imports --------------------------------------------------------------


def test_the_values_it_copies_are_the_code_s(smoke: ModuleType) -> None:
    assert smoke.SECURITY_HEADERS == dict(SECURITY_HEADERS)
    attributes = {part.strip() for part in sessions.CLEARED_SESSION_COOKIE.split(";")[2:]}
    assert (smoke.SESSION_COOKIE, set(smoke.COOKIE_ATTRIBUTES)) == (sessions.SESSION_COOKIE, attributes)
    # a warn code the dispatcher pushes at priority 3 and that nothing resolves on its own
    entry = CATALOGUE[smoke.ALERT_CODE]
    assert (entry.severity, entry.pushed, entry.auto_resolves) == ("warn", True, False)
    assert push_priority("warn") == 3
    assert smoke.ALERT_TITLE == f"{ntfy.TITLE_PREFIX}{smoke.ALERT_CODE}"
    assert smoke.ALERT_CLICK == f"{smoke.ORIGIN}{ntfy.CLICK_PATH}"
    assert f"'{smoke.ALERT_CODE}', 'warn', '{smoke.ALERT_CODE}:smoke_m5'" in smoke.INSERT_ALERT
    for interval_s in (3_600, 6 * 3_600, 24 * 3_600, 168 * 3_600):
        assert smoke.grace_s(interval_s) == deadman.grace_s(interval_s)
    assert (smoke.PURGE_PHRASE, smoke.DELETE_PHRASE) == (
        privacy.PURGE_ADZUNA_PHRASE,
        privacy.DELETE_EVERYTHING_PHRASE,
    )
    assert smoke.WRONG_PHRASE != smoke.DELETE_PHRASE
    assert smoke.UNSHIPPED_BANNER in get_args(BannerCode)
    assert smoke.README in EXPORT_FILES
    assert smoke.ROLES == get_args(Role)
    assert smoke.ORIGIN == "https://localhost:8443" == prodstack.ORIGIN


class ComposeLoader(yaml.SafeLoader):
    """YAML as compose reads it: a merge tag (`!override`, `!reset`) yields its value as it is."""


def _tagged(loader: yaml.SafeLoader, suffix: str, node: yaml.Node) -> Any:
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return loader.construct_scalar(node)  # type: ignore[arg-type]


ComposeLoader.add_multi_constructor("!", _tagged)


def test_the_stack_it_runs_is_the_test_stack_without_the_backup_scheduler(smoke: ModuleType) -> None:
    text = (REPO / "deploy/compose.test.yaml").read_text(encoding="utf-8")
    services = yaml.load(text, Loader=ComposeLoader)["services"]  # noqa: S506 - a SafeLoader (above)
    assert set(smoke.SERVICES) == set(prodstack.STACK_SERVICES) - {"backup"}
    assert {services["worker"]["environment"][name] for name in smoke.FAKE_VARS} == {"1"}
    env = dict(
        line.split("=", 1)
        for line in (REPO / "deploy/compose.test.env").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    )
    assert env["JOBHUNTER_PUBLIC_ORIGIN"] == smoke.ORIGIN


def test_it_takes_only_the_named_helpers_from_the_other_smoke_scripts() -> None:
    """smoke_m0's `ORIGIN` and `DC` are the dev stack's (smoke_m1 to smoke_m4 import them by name), so the
    script takes nothing of smoke_m0 but these six names, and of smoke_m1 its inputs alone."""
    tree = ast.parse((SCRIPTS / "smoke_m5.py").read_text(encoding="utf-8"))
    imported = {
        node.module: {alias.name for alias in node.names}
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module in ("smoke_m0", "smoke_m1")
    }
    assert imported == {
        "smoke_m0": {"emit", "CheckFailed", "Context", "USERNAME", "PASSWORD", "stderr_tail"},
        "smoke_m1": {"SEED_INPUTS"},
    }
    assert not [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Import) and "smoke_m0" in {a.name for a in node.names}
    ]


def test_prodstack_runs_the_smoke_after_the_drills_before_the_teardown() -> None:
    steps = yaml.safe_load((REPO / ".github/workflows/prodstack.yml").read_text(encoding="utf-8"))["jobs"][
        "prodstack"
    ]["steps"]
    runs = [" ".join(str(step.get("run", "")).split()) for step in steps]
    smoke = runs.index("uv run python scripts/smoke_m5.py")
    drills = next(i for i, run in enumerate(runs) if run.endswith("tests/stack/test_prod_drills.py"))
    first_teardown = next(i for i, step in enumerate(steps) if "if" in step)
    assert drills < smoke < first_teardown
    assert "if" not in steps[smoke]


def test_mypy_checks_the_script() -> None:
    files = next(
        line
        for line in (REPO / "mypy.ini").read_text(encoding="utf-8").splitlines()
        if line.startswith("files")
    )
    assert "scripts/smoke_m5.py" in [part.strip() for part in files.split("=", 1)[1].split(",")]
