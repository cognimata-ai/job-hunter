"""scripts/e2e_setup.py (plan M4 Task 32, Codex r1 #7): the deterministic E2E setup runs its nine steps in
order and stops at the first failure; each step checks its own result before it writes, so on a stack that
has everything already it issues no write at all; and it prints only its JSON lines, never the session
cookie, its token, the password or an `Idempotency-Key`. On a fresh stack the profile step writes the
grounded candidates' answers after the extraction and before the confirm, with the draft's first skill.

The stack is a fake (`FakeStack`): `subprocess.run` answers the `docker compose exec` calls (psql, the
worker's `printenv` and its keyed directory) and an `httpx.MockTransport` the API, each from the statement
or the request it gets; every SQL statement, docker command and request that is not a known read is recorded
as a write.
"""

import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import httpx
import pytest

from tests.support.smoke_stack import DESCRIPTION, detail_body

SCRIPTS: Final = Path(__file__).resolve().parents[2] / "scripts"
TOKEN: Final = "c2Vzc2lvbi10b2tlbi1vZi10aGUtZTJlLXN0dWI"
STEPS: Final = [
    "login",
    "inputs",
    "settings",
    "collection",
    "merge",
    "profile",
    "grounded",
    "rejected_item",
    "verify",
]
TUPLE: Final = 7
SCOPE: Final = 3
JOB: Final = 21
REVISION: Final = 210
RESULT: Final = 90
RESPONSE: Final = 55
SKILL: Final = "Python"
KEYED_DIR: Final = "/tmp/fake-keyed"  # noqa: S108 - the worker's tmpfs path (deploy/compose.e2e.yaml), faked


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def setup(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """scripts/e2e_setup.py loaded as when it runs as a script (the smoke modules and `m4_seeds` beside it on
    `sys.path`), leaving no cached copy of any behind (`test_smoke_m3_script.import_smoke`)."""
    monkeypatch.syspath_prepend(str(SCRIPTS))
    for name in ("smoke_m0", "smoke_m1", "smoke_m2", "smoke_m3", "m4_seeds"):
        monkeypatch.delitem(sys.modules, name, raising=False)
        monkeypatch.setitem(sys.modules, name, load(name))
    return load("e2e_setup")


class FakeStack:
    """A dev stack on which every step's result is there already (`saved_scope` false: the E2E scope not yet
    saved, so the settings step saves it)."""

    def __init__(self, setup: ModuleType, *, saved_scope: bool = True) -> None:
        self.setup = setup
        self.m4 = sys.modules["m4_seeds"]
        self.m3 = sys.modules["smoke_m3"]
        self.saved_scope = saved_scope
        self.writes: list[str] = []
        self.keys: list[str] = []
        self.quotes = self.m4.grounded_quotes(DESCRIPTION)
        answers = self.m4.grounded_answers(REVISION, DESCRIPTION, SKILL)
        from jobhunter.llm.fake import keyed_answer_filename

        self.digests = "".join(
            f"{hashlib.sha256(body).hexdigest()}  {keyed_answer_filename(key)}\n"
            for key, body in answers.items()
        )
        pair = [
            {
                "native_id": native,
                "job_id": job,
                "group_id": 8,
                "group_status": "active",
                "representative_job_id": 31,
                "seniority_tokens": ["senior"],
                "work_modes": [],
                "employment_types": ["full_time"],
                "target_state": state,
                "deferred_group_id": deferred,
                "filter_passed": True,
                "filter_reasons": [],
            }
            for native, job, state, deferred in (
                ("5933514068", 31, "pending", None),
                ("5933514069", 32, "deferred_duplicate", 8),
            )
        ]
        candidates = [
            {"job_id": JOB, "state": "published_contested", "result_id": RESULT, "description": DESCRIPTION}
        ]
        self.answers: dict[str, str] = {
            setup.INPUTS_PRESENT: "1|1|4",
            setup.PENDING_SCOPE: "0",
            setup.PAID_RUN_DONE: "12",
            setup.JOB_COUNT: "313",
            self.m4.MERGE_DONE: "1",
            self.m3.ACTIVE: f"{TUPLE}|{SCOPE}",
            self.m3.TARGETS_CREATED.format(tuple=TUPLE): "succeeded|313|313|0",
            setup.GROUNDED_CANDIDATES: json.dumps(candidates),
            setup.DESCRIPTION_QUOTES.format(result=RESULT): json.dumps(self.quotes),
            self.m4.GROUNDED_INPUTS.format(job_id=JOB): json.dumps(
                {"revision_id": REVISION, "description": DESCRIPTION, "skill": SKILL}
            ),
            self.m4.REJECTED_SEED: f"{RESPONSE}|66",
            setup.RETAINED_COUNT: "313",
            setup.REINGEST_PAIR: json.dumps(pair),
            setup.E7_JOBS: "1|1",
            self.m3.APPLY_STATE.format(operation=77): f"succeeded|{SCOPE + 1}|0",
            setup.PERIODIC_DONE: "1",
            self.m3.LANE_STATE: "0|0|2",
            setup.PENDING_APPLY: f"77|{SCOPE}",
            self.m4.GROUNDED_JOBS: json.dumps(
                [
                    {
                        "job_id": JOB,
                        "revision_id": REVISION,
                        "description": DESCRIPTION,
                        "evidence": [self.quotes],
                    }
                ]
            ),
        }
        # answers that change once a statement was read: {statement read: {statement: its new answer}}
        self.then: dict[str, dict[str, str]] = {}
        # what `POST /api/jobs/{JOB}/score-now` answers, and a non-JSON body for `GET /api/settings`
        self.score_now = httpx.Response(202, json={"target_id": 9, "state": "pending"})
        self.settings_text: str | None = None
        self.settings: dict[str, Any] = {
            "version": {"id": 41, "version_no": 3},
            "payload": {
                "scope": {
                    "locations": [
                        {**self.m3.SCOPE_LOCATION, "radius_km": setup.E2E_RADIUS_KM if saved_scope else 200}
                    ]
                },
                "scoring": {"batch_bulk_rescore": not saved_scope, "batch_min_targets": 200},
                "sources": {
                    "enabled": dict.fromkeys(("jsearch", "serpapi", "adzuna", "arbeitnow", "ats"), True)
                },
            },
        }

    def run(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        """`subprocess.run` of a `docker compose exec`: psql, the worker's `printenv`, its keyed directory."""
        service = args[args.index("-T") + 1]
        out = ""
        if service == "postgres":
            statement = args[-1]
            if statement in self.answers:
                out = self.answers[statement] + "\n"
                self.answers.update(self.then.pop(statement, {}))
            else:
                self.writes.append(f"sql: {statement.splitlines()[0][:60]}")
        elif args[-2:-1] == ["printenv"]:
            out = f"{KEYED_DIR}\n" if args[-1] == self.m4.KEYED_DIR_VAR else "1\n"
        elif "sha256sum" in " ".join(args):
            out = self.digests
        else:
            self.writes.append(f"docker: {' '.join(args[args.index('-T') + 1 :])[:60]}")
        stdout: Any = out if kwargs.get("text") else out.encode("utf-8")
        stderr: Any = "" if kwargs.get("text") else b""
        return subprocess.CompletedProcess(args, 0, stdout, stderr)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if "idempotency-key" in request.headers:
            self.keys.append(request.headers["idempotency-key"])
        if (method, path) == ("POST", "/api/auth/login"):
            cookie = f"__Host-jh_session={TOKEN}; HttpOnly; Path=/; SameSite=strict; Secure"
            return httpx.Response(204, headers={"set-cookie": cookie})
        if (method, path) == ("GET", "/api/settings") and self.settings_text is not None:
            return httpx.Response(200, text=self.settings_text)
        if method == "GET":
            body = {
                "/api/settings": self.settings,
                "/api/profile": {"active": {"id": 5, "version_no": 1}},
                f"/api/jobs/{JOB}": detail_body(self.quotes),
                "/api/system/needs-attention": {
                    "raw_items": {"count": 1, "items": [{"raw_response_id": RESPONSE, "replayable": True}]}
                },
            }.get(path)
            return httpx.Response(200, json=body) if body is not None else httpx.Response(404)
        self.writes.append(f"{method} {path}")
        if (method, path) == ("POST", "/api/settings/dry-run"):
            return httpx.Response(200, json={"errors": [], "warnings": [{"code": "llm_cap_binding"}]})
        if (method, path) == ("POST", "/api/settings"):
            saved = copy.deepcopy(self.settings)
            saved["payload"] = json.loads(request.content)["payload"]
            saved["version"] = {"id": 42, "version_no": 4}
            self.settings = saved
            return httpx.Response(201, json={**saved, "apply_scope_operation_id": 77})
        if (method, path) == ("POST", f"/api/jobs/{JOB}/score-now"):
            return self.score_now
        return httpx.Response(500)


def run_on(setup: ModuleType, stack: FakeStack, monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(subprocess, "run", stack.run)
    monkeypatch.setattr(setup.time, "sleep", lambda seconds: None)
    with httpx.Client(transport=httpx.MockTransport(stack.handle)) as client:
        status: int = setup.run(client)
    return status


def lines(out: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in out.splitlines()]


def test_the_steps_run_in_order(
    setup: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert [name for name, _ in setup.STEPS] == STEPS
    called: list[str] = []

    def recorder(name: str, *, fails: bool = False) -> Callable[[Any], str | None]:
        def step(ctx: Any) -> str | None:
            called.append(name)
            if fails:
                raise setup.CheckFailed(f"{name} failed")
            return None

        return step

    monkeypatch.setattr(setup, "STEPS", [(name, recorder(name)) for name in STEPS])
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as client:
        assert setup.run(client) == 0
    assert called == STEPS
    assert lines(capsys.readouterr().out) == [{"step": name, "ok": True} for name in STEPS]

    # the first failure stops the run: exit 1, and no later step runs
    called.clear()
    steps = [(name, recorder(name, fails=name == "collection")) for name in STEPS]
    monkeypatch.setattr(setup, "STEPS", steps)
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as client:
        assert setup.run(client) == 1
    assert called == STEPS[:4]
    printed = lines(capsys.readouterr().out)
    assert [(p["step"], p["ok"]) for p in printed] == [*((n, True) for n in STEPS[:3]), ("collection", False)]
    assert printed[-1]["detail"] == "collection failed"


def test_each_step_checks_before_it_writes(
    setup: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stack = FakeStack(setup)
    assert run_on(setup, stack, monkeypatch) == 0
    printed = lines(capsys.readouterr().out)
    assert [(p["step"], p["ok"]) for p in printed] == [(name, True) for name in STEPS]
    assert stack.writes == [] and stack.keys == []  # every result was there: nothing written, no key made
    by_step = {p["step"]: p.get("detail") for p in printed}
    assert by_step["inputs"] == "already there"
    assert by_step["merge"] == "a user-confirmed merge stands: nothing seeded"
    assert by_step["grounded"] == (
        f"job {JOB} published with its grounded answers already; files present; "
        "m4_seeds.grounded_job_id() finds it"
    )
    assert by_step["rejected_item"] == f"raw response {RESPONSE}, raw item 66"


def test_the_grounded_answers_are_written_before_the_confirm(
    setup: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Step 6 on a stack without a profile: the CV checks, then every grounded candidate's answers (the
    draft's first skill), then the confirm; a candidate without three sentences gets none."""
    stack = FakeStack(setup)
    smoke_m2, m4 = sys.modules["smoke_m2"], sys.modules["m4_seeds"]
    calls: list[str] = []
    profiles: Iterator[dict[str, Any]] = iter([{"active": None}, {"active": {"id": 5, "version_no": 1}}])
    smoke_m2.STATE.draft_id = 17
    other = {"job_id": JOB + 1, "revision_id": REVISION + 1, "description": "Kurz. Zu kurz."}
    stack.answers[m4.GROUNDED_JOBS] = json.dumps(
        [{"job_id": JOB, "revision_id": REVISION, "description": DESCRIPTION}, other]
    )

    def get_json(ctx: Any, path: str) -> dict[str, Any]:
        if path == "/api/profile":
            return next(profiles)
        assert path == "/api/profile/drafts/17"
        return {"draft": {"skills": [{"name": SKILL}, {"name": "FastAPI"}]}}

    for name in ("check_cv_upload", "check_text_review", "check_extract_ready", "check_confirm"):
        monkeypatch.setattr(smoke_m2, name, lambda ctx, name=name: calls.append(name))
    written: list[dict[str, bytes]] = []

    def write_keyed_answers(answers: dict[str, bytes]) -> int:
        written.append(dict(answers))
        calls.append("answers")
        return len(answers)

    monkeypatch.setattr(m4, "write_keyed_answers", write_keyed_answers)
    monkeypatch.setattr(setup, "get_json", get_json)
    monkeypatch.setattr(subprocess, "run", stack.run)
    detail = setup.step_profile(object())
    assert calls == [
        "check_cv_upload",
        "check_text_review",
        "check_extract_ready",
        "answers",
        "check_confirm",
    ]
    assert written == [m4.grounded_answers(REVISION, DESCRIPTION, SKILL)]
    assert "1 grounded candidates answered (6 files)" in detail


def test_it_never_prints_a_cookie_a_key_or_the_password(
    setup: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    password = sys.modules["smoke_m0"].PASSWORD
    # A run that saves the E2E scope: the save sends a fresh key.
    stack = FakeStack(setup, saved_scope=False)
    assert run_on(setup, stack, monkeypatch) == 0
    assert stack.writes == ["POST /api/settings/dry-run", "POST /api/settings"]
    (key,) = stack.keys
    out = capsys.readouterr().out
    assert all(set(p) <= {"step", "ok", "detail"} for p in lines(out))
    for secret in (TOKEN, f"__Host-jh_session={TOKEN}", password, key):
        assert secret not in out

    # A failure whose message would quote them prints them redacted.
    def leaky(ctx: Any) -> str | None:
        raise setup.CheckFailed(f"cookie {ctx.cookie} token {TOKEN} password {password}")

    monkeypatch.setattr(setup, "STEPS", [("login", setup.step_login), ("inputs", leaky)])
    assert run_on(setup, FakeStack(setup), monkeypatch) == 1
    out = capsys.readouterr().out
    failed = lines(out)[-1]
    assert failed["ok"] is False and failed["detail"].count("[redacted]") == 3
    assert TOKEN not in out and password not in out


def pending_candidate(stack: FakeStack, setup: ModuleType) -> None:
    """The grounded job's target `pending` until it is read once, then published (as a cycle publishes it)."""
    candidates = json.loads(stack.answers[setup.GROUNDED_CANDIDATES])
    candidates[0]["state"], candidates[0]["result_id"] = "pending", None
    stack.answers[setup.GROUNDED_CANDIDATES] = json.dumps(candidates)
    stack.answers[setup.TARGET_OF_JOB.format(job=JOB)] = "published_contested|score_now"


def test_score_now_accepts_only_the_conflict_of_a_claimed_target(
    setup: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Step 7 on a pending job: a 409 `conflict_state` (a cycle claimed it since) is awaited as a 202 is;
    any other 409 fails the step, naming its code."""
    stack = FakeStack(setup)
    pending_candidate(stack, setup)
    stack.score_now = httpx.Response(409, json={"code": "conflict_state", "status": 409})
    assert run_on(setup, stack, monkeypatch) == 0
    grounded = {p["step"]: p for p in lines(capsys.readouterr().out)}["grounded"]
    assert grounded["ok"] and "published_contested" in grounded["detail"]
    assert stack.writes == [f"POST /api/jobs/{JOB}/score-now"]

    stack = FakeStack(setup)
    pending_candidate(stack, setup)
    stack.score_now = httpx.Response(409, json={"code": "budget_exhausted", "status": 409})
    assert run_on(setup, stack, monkeypatch) == 1
    failed = lines(capsys.readouterr().out)[-1]
    assert (failed["step"], failed["ok"]) == ("grounded", False)
    assert failed["detail"] == f"score-now of job {JOB}: expected 202, got 409 (code 'budget_exhausted')"


def test_a_data_error_names_the_last_answer(
    setup: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An answer that is no JSON fails its step with the class and the method, path and status of the last
    answer, never with the class alone or the answer's text."""
    stack = FakeStack(setup)
    stack.settings_text = "<html>not json, an invented page</html>"
    assert run_on(setup, stack, monkeypatch) == 1
    out = capsys.readouterr().out
    failed = lines(out)[-1]
    assert (failed["step"], failed["ok"]) == ("settings", False)
    assert failed["detail"] == "JSONDecodeError after GET /api/settings answered 200"
    assert "invented page" not in out
    assert setup.data_error(KeyError("job_id"), []) == "KeyError 'job_id' before any API answer"


def test_a_pending_scope_apply_is_awaited_not_saved_again(
    setup: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Step 3 when the E2E scope is saved but its apply is pending (an interrupted run): it polls that apply's
    flip and saves nothing."""
    stack = FakeStack(setup)
    stack.answers[setup.PENDING_SCOPE] = str(SCOPE + 1)
    stack.then[stack.m3.APPLY_STATE.format(operation=77)] = {setup.PENDING_SCOPE: "0"}
    assert run_on(setup, stack, monkeypatch) == 0
    settings = {p["step"]: p for p in lines(capsys.readouterr().out)}["settings"]
    assert settings == {
        "step": "settings",
        "ok": True,
        "detail": "already saved: version_no 3; the pending apply operation 77 succeeded: "
        f"scope {SCOPE} -> {SCOPE + 1}",
    }
    assert stack.writes == [] and stack.keys == []


def test_the_grounded_job_is_found_by_its_files_and_its_evidence(
    setup: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`m4_seeds.grounded_job_id`: the lowest candidate whose revision has all six grounded files and a
    published result (any tuple) with the grounded quotes as its description evidence; a candidate without
    three quotes, without the files or without such a result is passed over; none fails loudly."""
    m4 = sys.modules["m4_seeds"]
    stack = FakeStack(setup)
    no_files = {
        "job_id": JOB - 2,
        "revision_id": REVISION - 2,
        "description": DESCRIPTION,
        "evidence": [stack.quotes],
    }
    other_evidence = {
        "job_id": JOB - 1,
        "revision_id": REVISION,
        "description": DESCRIPTION,
        "evidence": [["Kurz."]],
    }
    short = {"job_id": JOB - 3, "revision_id": REVISION, "description": "Kurz.", "evidence": [["Kurz."]]}
    grounded = {
        "job_id": JOB,
        "revision_id": REVISION,
        "description": DESCRIPTION,
        "evidence": [[], stack.quotes[:2]],
    }
    monkeypatch.setattr(subprocess, "run", stack.run)
    stack.answers[m4.GROUNDED_JOBS] = json.dumps([short, no_files, other_evidence, grounded])
    assert m4.grounded_job_id(m4.stack_sql) == JOB
    assert m4.grounded_file_names(REVISION) == {
        f"{purpose}_{REVISION}_{n}.sse"
        for purpose, rounds in (("generate", (0,)), ("critic", (1, 2, 3)), ("revise", (1, 2)))
        for n in rounds
    }
    stack.answers[m4.GROUNDED_JOBS] = json.dumps([short, no_files, other_evidence])
    assert m4.find_grounded_job(m4.stack_sql) is None
    with pytest.raises(m4.CheckFailed, match="no greenhouse getyourguide job"):
        m4.grounded_job_id(m4.stack_sql)
