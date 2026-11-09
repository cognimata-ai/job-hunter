"""scripts/smoke_m3.py (Task 28): its fifteen checks in order (smoke_m2's checks 1-8 as they are, then its
own), the score lane's predicates it copies pinned to the code's, every compose call on the three files (the
fixtures and the fake LLM over the dev stack), the paid run posted only once the running worker is on both
fakes, and a whole run against a stubbed stack that prints fifteen `ok` lines and never the cookie, the
password, a key, an ad's text, a CV text or a draft; `subprocess` and the HTTP transport stubbed."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Final

import httpx
import pytest
from jobhunter.llm import spend_ledger
from jobhunter.scoring import batches, targets

from tests.support.profile import SYNTHETIC_PROFILE
from tests.support.smoke_stack import AD_TEXT, CONTENT, KEY, M2_CHECK_NAMES, TOKEN, Database, Stack
from tests.support.smoke_stack import M3_CHECK_NAMES as CHECK_NAMES

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
OVERLAYS: Final = ["compose.dev.yaml", "compose.fixtures.yaml", "compose.fakellm.yaml"]


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def import_smoke(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """scripts/smoke_m3.py loaded as when it runs as a script (`smoke_m0`, `smoke_m1` and `smoke_m2` beside
    it on `sys.path`), leaving no cached copy of any behind (`test_smoke_m2_script.import_smoke`)."""
    monkeypatch.syspath_prepend(str(SCRIPTS))
    for name in ("smoke_m0", "smoke_m1", "smoke_m2"):
        monkeypatch.delitem(sys.modules, name, raising=False)
        monkeypatch.setitem(sys.modules, name, load(name))
    return load("smoke_m3")


@pytest.fixture
def smoke(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    return import_smoke(monkeypatch)


def compose_files(args: list[str]) -> list[str]:
    return [Path(args[i + 1]).name for i, arg in enumerate(args) if arg == "-f"]


Answer = tuple[int, str, str]


def stub_subprocess(smoke: ModuleType, monkeypatch: pytest.MonkeyPatch, answer: Any) -> list[list[str]]:
    """Replace `subprocess.run` in the smoke and the modules it runs docker through (`smoke_m0`'s `psql`,
    `smoke_m2`'s user creation and `printenv`) with `answer(args) -> (returncode, stdout, stderr)`; the calls,
    in order."""
    calls: list[list[str]] = []

    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, *answer(args))

    stub = SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired)
    for module in (smoke, sys.modules["smoke_m0"], sys.modules["smoke_m2"]):
        monkeypatch.setattr(module, "subprocess", stub)
    return calls


def test_the_fifteen_checks_run_in_order(smoke) -> None:
    assert [name for name, _ in smoke.CHECKS] == CHECK_NAMES
    m2 = sys.modules["smoke_m2"]
    assert smoke.CHECKS[: len(M2_CHECK_NAMES)] == m2.CHECKS[: len(M2_CHECK_NAMES)]  # M2's, as they are


def test_the_copied_predicates_are_the_score_lanes(smoke) -> None:
    """The retained current revision is `create_targets_for_tuple`'s, the batch candidate the preparation's,
    and the counted amount the one `spend_ledger.recompute` sums: the smoke's `psql` copies cannot drift from
    them."""
    assert smoke.RETAINED_JOB_SQL == targets.RETAINED_JOB_SQL
    assert smoke.ELIGIBLE_SQL == batches.ELIGIBLE_SQL
    assert smoke.COUNTED == spend_ledger._COUNTED
    assert smoke.COUNTED in smoke.COUNTERS_CLEAN and smoke.RETAINED_JOB_SQL in smoke.TARGETS_CREATED


def test_the_golden_seed_runs_in_the_worker_container_on_both_overlays(smoke, monkeypatch) -> None:
    calls = stub_subprocess(smoke, monkeypatch, lambda args: (1, "", "boom\n"))
    with pytest.raises(smoke.CheckFailed, match="golden seed-adversarial: exit 1: boom"):
        smoke.golden_seed()
    (args,) = calls
    assert args[:4] == ["docker", "compose", "--env-file", str(SCRIPTS.parent / "deploy/images.lock")]
    assert compose_files(args) == OVERLAYS
    assert args[args.index("exec") :] == ["exec", "-T", "worker", "jobhunter", "golden", "seed-adversarial"]


def test_a_running_tick_keeps_the_lane_from_quiet(smoke, monkeypatch) -> None:
    """Task 28 fix round 1, item 6: a `jh:tick` job in `doing` counts as waiting (its periodic trigger reads
    the bucket, so a slow tick could still take a refill), and the quiet wait polls until it has ended."""
    assert (
        "(task_name = 'jh:score_cycle' AND status IN ('todo', 'doing'))\n"
        "      OR (task_name = 'jh:tick' AND status = 'doing')"
    ) in smoke.LANE_STATE
    rows = iter([["0", "1", "4"], ["0", "0", "4"]])  # a tick running, then none
    asked: list[str] = []

    def query_row(sql: str, columns: int) -> list[str]:
        asked.append(sql)
        return next(rows)

    monkeypatch.setattr(smoke, "query_row", query_row)
    monkeypatch.setattr(smoke, "POLL_INTERVAL_S", 0)
    assert smoke.await_quiet_lane() == 4 and asked == [smoke.LANE_STATE] * 2


def test_the_refill_moment_reads_the_lane_again_after_waiting_for_the_window(smoke, monkeypatch) -> None:
    """Smoke run 3: the lane was quiet at second 55, the tick at the top of the minute started a periodic
    cycle while the smoke waited for second 10, and that cycle's settlements took the refill. The lane is
    read again inside the window."""
    seconds = iter([55, 20])
    reads = iter([4, 5])
    waits: list[str] = []

    class Clock:
        @staticmethod
        def now(tz: Any) -> Any:
            return SimpleNamespace(second=next(seconds))

    monkeypatch.setattr(smoke, "datetime", Clock)
    monkeypatch.setattr(smoke, "await_quiet_lane", lambda: next(reads))
    monkeypatch.setattr(smoke, "away_from_the_tick", lambda: waits.append("window"))
    assert smoke.quiet_moment() == 5 and waits == ["window"]


class Posts:
    """An `httpx.Client` stand-in that records each post (none may happen)."""

    def __init__(self) -> None:
        self.posts: list[str] = []

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        self.posts.append(url)
        return httpx.Response(202, json={"run": {"id": 1}, "created": True})


@pytest.mark.parametrize(
    ("llm", "sources"),
    [((1, ""), (0, "1\n")), ((0, "1\n"), (0, "0\n"))],
    ids=["fake-llm-unset", "fake-sources-off"],
)
def test_no_paid_run_is_posted_unless_the_worker_is_on_both_fakes(
    smoke, monkeypatch, llm: tuple[int, str], sources: tuple[int, str]
) -> None:
    by_variable = {"JOBHUNTER_FAKE_LLM": llm, "JOBHUNTER_FAKE_SOURCES": sources}

    def docker(args: list[str]) -> Answer:
        if "printenv" in args:
            return (*by_variable[args[-1]], "")
        return 0, "1|2|3|4\n" if "INSERT INTO queries" in args[-1] else "4|4|1\n", ""  # the two seeds

    calls = stub_subprocess(smoke, monkeypatch, docker)
    client = Posts()
    with pytest.raises(smoke.CheckFailed, match="no run was posted"):
        smoke.check_targets_created(smoke.Context(client=client, cookie="c"))
    assert client.posts == []
    assert all(compose_files(args) == OVERLAYS for args in calls if "printenv" in args)


def run_against(
    smoke: ModuleType, monkeypatch: pytest.MonkeyPatch, stack: Stack, *, exit_code: int = 0
) -> list[list[str]]:
    """`main()` on `stack`, every docker call answered as the fresh stack answers it, ending `exit_code`; the
    docker calls."""

    def docker(args: list[str]) -> Answer:
        if "printenv" in args:
            return 0, "1\n", ""
        if "psql" in args:
            return 0, stack.database.answer(args[-1]) + "\n", ""
        if args[-3:] == ["jobhunter", "golden", "seed-adversarial"]:
            return 0, "golden seed-adversarial: cases=10 jobs=10 profiles=10\n", f"key={KEY} {AD_TEXT}\n"
        assert "run" in args, args  # the user's creation: the one other docker call
        return 0, "", ""

    calls = stub_subprocess(smoke, monkeypatch, docker)
    monkeypatch.setattr(smoke, "AWAY_FROM_THE_TICK", range(60))
    real_client = httpx.Client

    def client(**kwargs: Any) -> httpx.Client:
        return real_client(transport=httpx.MockTransport(stack), **kwargs)

    monkeypatch.setattr(smoke, "httpx", SimpleNamespace(Client=client, HTTPError=httpx.HTTPError))
    assert smoke.main() == exit_code
    return calls


def test_a_whole_run_prints_fifteen_ok_lines_and_never_a_secret_or_a_text(smoke, monkeypatch, capsys) -> None:
    stack = Stack(Database(smoke))
    calls = run_against(smoke, monkeypatch, stack)
    out = capsys.readouterr().out
    lines = [json.loads(line) for line in out.splitlines()]
    assert [(line["check"], line["ok"]) for line in lines] == [(name, True) for name in CHECK_NAMES]
    assert TOKEN not in out and sys.modules["smoke_m0"].PASSWORD not in out
    assert [text for text in CONTENT if text in out] == []
    assert all(compose_files(args) == OVERLAYS for args in calls if "psql" not in args)
    # the bucket advanced before the manual cycle's request (the stub checks it there), nowhere else
    assert stack.database.seen.count("ADVANCE_BUCKET") == 1
    assert stack.database.seen.index("SEED_INPUTS") < stack.database.seen.index("SEED_BOARDS")
    # the second draft is the first one with one more target title, so the confirm activates a new tuple
    titles = SYNTHETIC_PROFILE["target_titles"]
    assert stack.saved_draft["target_titles"] == [*titles, smoke.ADDED_TITLE]
    assert ("POST", "/api/runs") in stack.seen and stack.uploads == 2
    batch = lines[CHECK_NAMES.index("batch_path")]["detail"]
    assert "batch_min_targets=20 saved as version_no 3 (warnings=['budget_tight'])" in batch
    assert "submission 7 submitted, ended and reconciled with 243 items" in batch
    assert "each decided (followup=241,published=2); batch_followup cycle requested (1)" in batch
    assert "expired_unscored_too_old=28" in batch
    seen = stack.database.seen
    assert seen.index("BATCH_SUBMIT_OF") < seen.index("SUBMISSION_OF") < seen.index("FOLLOWUP_REQUESTED")


def test_check_14_saves_the_batch_floor_before_its_second_confirm(smoke, monkeypatch) -> None:
    """Ruling T18-a: one settings save of `scoring.batch_min_targets = SMOKE_BATCH_MIN_TARGETS` (20), with its
    own key and the dry-run's warnings acknowledged, after check 13's scope save and before the second
    confirm; nothing else of the payload changes."""
    stack = Stack(Database(smoke))
    run_against(smoke, monkeypatch, stack)
    assert smoke.SMOKE_BATCH_MIN_TARGETS == 20
    (scope_key, _), (floor_key, floor_payload) = stack.saves
    assert (scope_key, floor_key) == (smoke.SCOPE_KEY, smoke.BATCH_FLOOR_KEY)
    assert floor_payload == {  # the active payload (the stub's) with the floor changed, and nothing else
        "scope": {"locations": [], "excluded_keywords": [AD_TEXT]},
        "scoring": {"batch_min_targets": 20, "min_cycle_targets": 10},
        "anthropic": KEY,
    }
    confirms = [i for i, (method, path) in enumerate(stack.seen) if path.endswith("/confirm")]
    saves = [i for i, route in enumerate(stack.seen) if route == ("POST", "/api/settings")]
    assert len(confirms) == 2 and saves[0] < saves[1] < confirms[1]
    assert confirms[0] < saves[1]


@pytest.mark.parametrize(
    ("old_bulk", "ok"),
    [
        ("cancelled|0", True),
        ("cancelled|3", True),
        ("completed|0", True),
        ("completed|3", False),
        ("active|0", False),
    ],
    ids=[
        "cancelled-without-targets",
        "cancelled-with-targets",
        "completed-without-targets",
        "completed-with-targets",
        "still-active",
    ],
)
def test_the_first_bulk_operation_has_ended_by_the_second_confirm(
    smoke, monkeypatch, capsys, old_bulk: str, ok: bool
) -> None:
    """The activation cancels the old tuple's active operation. One that no target carries has no work, so a
    heavy pass may have completed it first; one that targets carry must be `cancelled` (Task 28 fix round 1,
    item 5: the reason, not the order of two transaction-start stamps). Any other state fails the check."""
    run_against(smoke, monkeypatch, Stack(Database(smoke, old_bulk=old_bulk)), exit_code=0 if ok else 1)
    last = json.loads(capsys.readouterr().out.splitlines()[-1 if not ok else -2])
    assert (last["check"], last["ok"]) == ("batch_path", ok)
    if ok:
        assert f"bulk 2 {old_bulk.split('|')[0]} -> 3 active" in last["detail"]


@pytest.mark.parametrize(
    ("changes", "detail"),
    [
        (
            {"batch_submit": "succeeded|below_minimum|0", "eligible": "13|129"},
            "expected the batch_submit to submit one batch; batch_submit=succeeded skipped=below_minimum "
            "submissions=0: eligible=13 (batch_min_targets=20) expired_unscored_too_old=129",
        ),
        ({"batch_submit": "failed|none|0"}, "expected the batch_submit to submit one batch"),
        ({"submission": "7|submit_ambiguous|243|f|243|243"}, "submission 7 submit_ambiguous"),
        ({"submission": "7|submit_rejected|243|f|243|0"}, "submission 7 submit_rejected"),
        ({"submission": "7|reconciled|243|f|243|0"}, "a stamp of the path is missing"),
        ({"submission": "7|reconciled|243|t|243|3"}, "an item is undecided"),
        ({"submission": "7|reconciled|243|t|240|0"}, "an item is undecided"),
        ({"submission": "7|reconciled|19|t|19|0"}, "fewer requests than the floor"),
        ({"followups": "0"}, "but no batch_followup cycle"),
    ],
    ids=[
        "below-minimum",
        "failed",
        "ambiguous",
        "rejected",
        "a-stamp-missing",
        "items-awaiting",
        "items-missing",
        "below-the-floor",
        "no-followup-cycle",
    ],
)
def test_the_batch_path_needs_a_reconciled_submission(
    smoke, monkeypatch, capsys, changes: dict[str, str], detail: str
) -> None:
    """Check 14 passes only on the full path: one submission, `reconciled` with every stamp of the path set
    and every item decided, and a `batch_followup` cycle requested; a skip below the minimum (M3's stopping
    point) now fails with the eligible and too-old counts."""
    run_against(smoke, monkeypatch, Stack(Database(smoke, **changes)), exit_code=1)
    last = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert (last["check"], last["ok"]) == ("batch_path", False)
    assert detail in last["detail"]


def test_a_detail_never_shows_the_session_token(smoke, monkeypatch, capsys) -> None:
    """A value the stack answers that a detail quotes (here a warning code) is scrubbed of the token."""
    run_against(smoke, monkeypatch, Stack(Database(smoke), warning=TOKEN))
    out = capsys.readouterr().out
    assert TOKEN not in out and "[redacted]" in out


@pytest.mark.parametrize("cached", [False, True])
def test_loading_the_smoke_leaves_sys_modules_as_it_was(cached: bool) -> None:
    names = ("smoke_m0", "smoke_m1", "smoke_m2")
    before = {name: ModuleType(name) if cached else None for name in names}
    with pytest.MonkeyPatch.context() as outer:
        for name, module in before.items():
            if module is None:
                outer.delitem(sys.modules, name, raising=False)
            else:
                outer.setitem(sys.modules, name, module)
        with pytest.MonkeyPatch.context() as monkeypatch:
            smoke = import_smoke(monkeypatch)
            assert smoke.smoke_m2 is sys.modules["smoke_m2"]
            assert smoke.Context is sys.modules["smoke_m0"].Context
        assert {name: sys.modules.get(name) for name in names} == before
