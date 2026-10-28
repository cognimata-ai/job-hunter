"""The `golden` and `eval` commands' parsing (spec §11.13, §18.6; plan Task 19): `jobhunter golden
seed-adversarial [--fixtures <dir>]` and `jobhunter eval run --kind golden (--candidate active|<ids> |
--candidate-payload <file>) [--route forced_critic] (--live | --fake)`, refused before anything starts when
the arguments name no candidate or no transport. No database."""

import json
from pathlib import Path

import pytest
from jobhunter import cli, cli_golden
from jobhunter.scoring.eval_fixtures import adversarial_dir
from jobhunter.scoring.eval_runner import Candidate


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
