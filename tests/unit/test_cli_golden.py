"""The `golden` and `eval` commands' parsing and exit statuses (spec §11.13, §18.6; plan Task 19):
`jobhunter golden seed-adversarial [--fixtures <dir>]` and `jobhunter eval run --kind golden (--candidate
active|<ids> | --candidate-payload <file>) [--route forced_critic] (--live | --fake)`, refused before anything
starts when the arguments name no candidate or no transport; 0 for a seed or a completed run, 1 for a failed
run, 2 when nothing could start, each refusal one line on stderr, never a traceback. No database: the
commands' database steps (`seed`, `start`, `wait`) are replaced."""

import json
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from jobhunter import cli, cli_golden
from jobhunter.scoring import golden
from jobhunter.scoring.eval_fixtures import adversarial_dir
from jobhunter.scoring.eval_runner import Candidate, CandidateInvalid, StartedRun
from jobhunter.settings.validate import FieldIssue
from jobhunter.types import JSONValue
from sqlalchemy.exc import OperationalError


def parse(*argv: str) -> object:
    return cli._build_parser().parse_args(list(argv))


def test_seed_adversarial_defaults_to_the_packaged_fixtures() -> None:
    args = parse("golden", "seed-adversarial")
    assert cli_golden.fixtures_dir(args) == adversarial_dir()  # type: ignore[arg-type]
    other = parse("golden", "seed-adversarial", "--fixtures", "/tmp/elsewhere")  # noqa: S108
    assert cli_golden.fixtures_dir(other) == Path("/tmp/elsewhere")  # type: ignore[arg-type]  # noqa: S108


def test_the_packaged_fixtures_are_ten_json_files() -> None:
    assert len(list(adversarial_dir().glob("*.json"))) == 10


@pytest.mark.parametrize(
    ("argv", "route", "transport"),
    [
        (("--candidate", "active", "--fake"), "production", "fake"),
        (("--candidate", "active", "--live", "--route", "forced_critic"), "forced_critic", "live"),
    ],
)
def test_eval_run_reads_its_route_and_transport(argv: tuple[str, ...], route: str, transport: str) -> None:
    args = parse("eval", "run", "--kind", "golden", *argv)
    assert (args.route, cli_golden.transport_of(args)) == (route, transport)  # type: ignore[attr-defined, arg-type]


def test_explicit_candidate_ids_are_read_in_order() -> None:
    assert cli_golden.candidate_ids("1,2,3,4,5") == Candidate(1, 2, 3, 4, 5)
    assert cli_golden.candidate_ids("active") is None


@pytest.mark.parametrize("text", ["1,2,3,4", "1,2,3,4,x", "1,2,3,4,5,6", "", "0,2,3,4,5"])
def test_a_malformed_candidate_is_refused(text: str) -> None:
    with pytest.raises(ValueError, match="candidate"):
        cli_golden.candidate_ids(text)


@pytest.mark.parametrize(
    "argv",
    [
        ("--fake",),  # no candidate
        ("--candidate", "active"),  # no transport
        ("--candidate", "active", "--candidate-payload", "x.json", "--fake"),  # two candidates
        ("--candidate", "active", "--fake", "--live"),  # two transports
    ],
)
def test_eval_run_without_one_candidate_and_one_transport_is_refused(argv: tuple[str, ...]) -> None:
    with pytest.raises(SystemExit) as refused:
        parse("eval", "run", "--kind", "golden", *argv)
    assert refused.value.code == 2


def test_a_candidate_payload_file_is_read_as_json(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"evaluation": {"t2_band": 7}}), encoding="utf-8")
    assert cli_golden.read_payload(path) == {"evaluation": {"t2_band": 7}}
    path.write_text("{no json", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON"):
        cli_golden.read_payload(path)


def test_the_summary_line_carries_ids_and_codes_only() -> None:
    line = cli_golden.summary(
        eval_run_id=7, status="failed", metrics={"failed": "interrupted"}, cost="0.1234"
    )
    assert line == "eval_run=7 status=failed failed=interrupted cost_usd=0.1234"


@pytest.fixture
def no_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """The commands start without the worker's config checks or its logging."""
    monkeypatch.setattr(cli_golden, "startup_or_exit", lambda role: SimpleNamespace(log_level="INFO"))
    monkeypatch.setattr(cli_golden, "configure_logging", lambda *args, **kwargs: None)


def replaced(result: object = None, *, raises: BaseException | None = None) -> Callable[..., Any]:
    """An async stand-in for a database step: it returns `result`, or raises `raises`."""

    async def step(*args: object, **kwargs: object) -> object:
        if raises is not None:
            raise raises
        return result

    return step


DATABASE_ERROR = OperationalError("SELECT 1", {}, Exception("down"))


@pytest.mark.parametrize(
    ("raises", "code", "said"),
    [
        (None, cli_golden.COMPLETED, "cases=10 jobs=10 profiles=10"),
        (DATABASE_ERROR, cli_golden.NOT_STARTED, "database error (OperationalError)"),
        (OSError("gone"), cli_golden.NOT_STARTED, "invalid fixtures (OSError)"),
        (ValueError("no fixture"), cli_golden.NOT_STARTED, "invalid fixtures (ValueError)"),
        (golden.FixtureChanged("overqualified", "label"), cli_golden.NOT_STARTED, "'overqualified' changed"),
    ],
    ids=["seeded", "database", "unreadable", "invalid", "changed"],
)
def test_the_seed_command_exits_by_its_outcome(
    no_startup: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    raises: BaseException | None,
    code: int,
    said: str,
) -> None:
    monkeypatch.setattr(cli_golden, "seed", replaced(golden.Seeded(10, 10, 10), raises=raises))
    assert cli_golden.run_golden_command(parse("golden", "seed-adversarial")) == code  # type: ignore[arg-type]
    out, err = capsys.readouterr()
    assert said in (out if raises is None else err)
    assert len((out + err).strip().splitlines()) == 1  # one line, no traceback


def eval_args(*argv: str) -> object:
    return parse("eval", "run", "--kind", "golden", *(argv or ("--candidate", "active")), "--fake")


@pytest.mark.parametrize(
    ("status", "code"), [("completed", cli_golden.COMPLETED), ("failed", cli_golden.FAILED)]
)
def test_an_eval_run_exits_by_its_runs_end(
    no_startup: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: str,
    code: int,
) -> None:
    monkeypatch.setattr(cli_golden, "start", replaced(StartedRun(eval_run_id=7, operation_id=9)))
    metrics: dict[str, JSONValue] = {"failed": "interrupted"} if status == "failed" else {}
    monkeypatch.setattr(cli_golden, "wait", replaced(cli_golden.RunEnd(status, metrics, Decimal("0.1"))))
    assert cli_golden.run_eval_command(eval_args()) == code  # type: ignore[arg-type]
    out, _ = capsys.readouterr()
    assert out.strip() == cli_golden.summary(eval_run_id=7, status=status, metrics=metrics, cost="0.1")


@pytest.mark.parametrize(
    ("argv", "raises", "said"),
    [
        (("--candidate", "1,2,3"), None, "a candidate is 'active' or five comma-separated ids"),
        ((), LookupError("active_config has no rubric or prompt-schema version"), "no rubric"),
        ((), CandidateInvalid([FieldIssue("payload.evaluation.t2_band", "out_of_range", "x")]), "t2_band"),
        ((), DATABASE_ERROR, "database error (OperationalError)"),
    ],
    ids=["malformed", "no-candidate", "invalid-payload", "database"],
)
def test_an_eval_run_that_cannot_start_exits_2_with_one_line(
    no_startup: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: tuple[str, ...],
    raises: BaseException | None,
    said: str,
) -> None:
    monkeypatch.setattr(cli_golden, "start", replaced(raises=raises))
    assert cli_golden.run_eval_command(eval_args(*argv)) == cli_golden.NOT_STARTED  # type: ignore[arg-type]
    _, err = capsys.readouterr()
    assert said in err and len(err.strip().splitlines()) == 1


def test_an_eval_run_whose_wait_fails_exits_1(
    no_startup: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli_golden, "start", replaced(StartedRun(eval_run_id=7, operation_id=9)))
    monkeypatch.setattr(cli_golden, "wait", replaced(raises=DATABASE_ERROR))
    assert cli_golden.run_eval_command(eval_args()) == cli_golden.FAILED  # type: ignore[arg-type]
    assert "database error (OperationalError)" in capsys.readouterr().err
