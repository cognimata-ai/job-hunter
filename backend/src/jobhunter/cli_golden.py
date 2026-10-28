"""The `golden` and `eval` commands (spec §11.13 L4610-4613, §18.6; M3 plan Tasks 19 and 20, R2, Codex r1 #34,
r2 B20, B21).

`cli.main` parses them (its parsers need no import of this module) and imports this module only when one of
them runs. Both start like the worker (the worker-role config
and its startup assertions) and work on the installation's database, where the golden set lives:

- `jobhunter golden seed-adversarial [--fixtures <dir>]` seeds the adversarial cases
  (`golden.seed_adversarial`) of `<dir>`, by default the packaged
  `jobhunter/scoring/eval_fixtures/adversarial/` (`eval_fixtures.adversarial_dir`), so the dev stack's
  installed wheel has them; idempotent by each fixture's key. It prints the counts it inserted.
- `jobhunter eval run --kind golden (--candidate active|<rubric>,<prompt_schema>,<model_policy>,
  <evaluation_policy>,<execution_config> | --candidate-payload <settings.json>) [--route forced_critic]
  (--live | --fake)` inserts the `eval_runs` row and its `eval_run` operation in one transaction
  (`eval_runner.start_run`, the job deferred in it) and waits until the worker has ended the run. The
  transport is the one the run requests (`metrics.transport_requested`): the worker that executes it checks it
  against its own and fails a mismatched run before any call (Codex r2 B20), so a fake run on a live worker
  pays nothing. A payload is a settings document that need not be saved: it is validated as the settings
  dry-run validates it and its derived rows are staged by hash in a short transaction of their own
  (`eval_runner.stage_candidate`, Codex r2 B21), then the run is started against those ids.

  Nothing is inserted, so nothing is paid, when a candidate id names no row or the active configuration has no
  rubric or prompt-schema version (`eval_runner.NoCandidate`), or when the run's kind has no active case
  (`eval_runner.NoActiveCases`, `eval_no_cases`; M3 Task 20, Codex r1 #34). Once the run has ended it prints
  the run's case table in Markdown (`case_table`: each case repetition's case, source, label, outcome,
  `score_mid`, binding knockouts and expected knockouts, the table the manual workflow uploads), then the
  summary line with the gate's state and the gates it failed (`metrics.failed_gates`).

The output is ids, counts, codes and amounts only (stdout: the case table and the summary; stderr: the logs
and the run's id at its start), never content or a key. An error prints its class only (SQLAlchemy puts bound
values into its messages). Exit status: 0 for a completed run or a seed, 1 for a failed run (or a wait the
database broke), 2 when nothing could start: a malformed candidate, a payload the dry-run refuses, no
candidate (`NoCandidate`, caught by name so that any other `LookupError` keeps its traceback, carry T19-e), no
active case, a fixture changed since its seed (`golden.FixtureChanged`), invalid fixtures or a database error,
each one line on stderr.
"""

import argparse
import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

from sqlalchemy.exc import SQLAlchemyError

from jobhunter.clock import utcnow
from jobhunter.config import AppConfig, startup_or_exit
from jobhunter.db.session import make_engine
from jobhunter.db.sql import q
from jobhunter.observability.logging import configure_logging
from jobhunter.scoring import eval_calibration, eval_runner, golden
from jobhunter.scoring.eval_calibration import RunCase
from jobhunter.scoring.eval_fixtures import adversarial_dir
from jobhunter.scoring.eval_runner import (
    Candidate,
    CandidateInvalid,
    NoActiveCases,
    NoCandidate,
    Route,
    RunKind,
    StartedRun,
    Transport,
)
from jobhunter.types import JSONValue
from jobhunter.worker.tasks import defer_in_tx

COMPLETED, FAILED, NOT_STARTED = 0, 1, 2
POLL_S: Final = 5.0  # how often `eval run` reads its run's status while it waits
_CANDIDATE_IDS: Final = 5

_RUN_END: Final = q("SELECT status, metrics, cost_usd FROM eval_runs WHERE id = :r")


def fixtures_dir(args: argparse.Namespace) -> Path:
    """`--fixtures`, else the packaged adversarial fixtures."""
    return adversarial_dir() if args.fixtures is None else Path(args.fixtures)


def transport_of(args: argparse.Namespace) -> Transport:
    return "live" if args.live else "fake"


def candidate_ids(text: str) -> Candidate | None:
    """`--candidate`: `None` for `active`, else the five positive ids in order; `ValueError` otherwise."""
    if text == "active":
        return None
    parts = text.split(",")
    if len(parts) != _CANDIDATE_IDS or not all(part.strip().isdigit() for part in parts):
        raise ValueError("a candidate is 'active' or five comma-separated ids")
    ids = [int(part) for part in parts]
    if min(ids) < 1:
        raise ValueError("a candidate's ids are positive")
    return Candidate(*ids)


def read_payload(path: Path) -> JSONValue:
    """`--candidate-payload`: the file's JSON document; `ValueError` for a file that holds none."""
    try:
        document: JSONValue = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"the candidate payload is no JSON (line {exc.lineno})") from None
    return document


def summary(*, eval_run_id: int, status: str, metrics: JSONValue, cost: str) -> str:
    """The summary line of a run's end: its id, status, failure code (a failed run's `metrics.failed`), the
    gate's state and the gates it failed (a completed run's `metrics.gate` and `metrics.failed_gates`) and
    cost."""
    doc = metrics if isinstance(metrics, dict) else {}
    failed, gate, gates = doc.get("failed"), doc.get("gate"), doc.get("failed_gates")
    reason = f" failed={failed}" if isinstance(failed, str) else ""
    gated = f" gate={gate}" if isinstance(gate, str) else ""
    names = f" failed_gates={','.join(str(g) for g in gates)}" if isinstance(gates, list) and gates else ""
    return f"eval_run={eval_run_id} status={status}{reason}{gated}{names} cost_usd={cost}"


_TABLE_HEAD: Final = (
    "| case | source | label | repetition | outcome | score_mid | binding knockouts | expected knockouts |",
    "|---:|---|---|---:|---|---:|---|---|",
)


def table_lines(rows: list[RunCase]) -> list[str]:
    """The run's case table in Markdown (module docstring): ids, codes and numbers only."""
    lines = list(_TABLE_HEAD)
    for row in rows:
        result = row.result
        score = "" if result.score_mid is None else str(result.score_mid)
        binding = ", ".join(result.knockouts_applied) or "-"
        expected = ", ".join(row.case.expected_knockouts) or "-"
        lines.append(
            f"| {result.golden_case_id} | {row.source} | {row.case.label} | {result.repetition} | "
            f"{result.outcome} | {score} | {binding} | {expected} |"
        )
    return lines


async def case_table(cfg: AppConfig, eval_run_id: int) -> list[str]:
    """The case table of run `eval_run_id` (`table_lines` of `eval_calibration.run_cases`)."""
    engine = make_engine(cfg, pool_size=1)
    try:
        async with engine.connect() as conn:
            return table_lines(await eval_calibration.run_cases(conn, eval_run_id))
    finally:
        await engine.dispose()


async def seed(cfg: AppConfig, directory: Path) -> golden.Seeded:
    """The adversarial seed of `directory` in a transaction of its own."""
    engine = make_engine(cfg, pool_size=1)
    try:
        async with engine.begin() as conn:
            return await golden.seed_adversarial(conn, fixtures_dir=directory, now=utcnow())
    finally:
        await engine.dispose()


async def start(
    cfg: AppConfig,
    *,
    kind: RunKind,
    route: Route,
    candidate: Candidate | None,
    payload: JSONValue | None,
    transport: Transport,
    now: datetime,
) -> StartedRun:
    """A new run (module docstring): `payload` staged first in a transaction of its own (`CandidateInvalid`
    when the dry-run refuses it), else `candidate`, else the active configuration; then the run and its
    operation in one transaction, the operation's job deferred in it."""
    engine = make_engine(cfg, pool_size=1)
    try:
        if payload is not None:
            async with engine.begin() as conn:
                candidate = await eval_runner.stage_candidate(conn, payload, now=now)
        async with engine.begin() as conn:
            chosen = await eval_runner.active_candidate(conn) if candidate is None else candidate
            return await eval_runner.start_run(
                conn,
                kind=kind,
                route=route,
                candidate=chosen,
                transport_requested=transport,
                now=now,
                defer=defer_in_tx,
            )
    finally:
        await engine.dispose()


@dataclass(frozen=True)
class RunEnd:
    status: str
    metrics: JSONValue
    cost_usd: Decimal


async def wait(
    cfg: AppConfig,
    eval_run_id: int,
    *,
    poll_s: float = POLL_S,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
) -> RunEnd:
    """Read the run's status every `poll_s` until the worker has ended it (a healthy run may take hours)."""
    engine = make_engine(cfg, pool_size=1)
    try:
        while True:
            async with engine.connect() as conn:
                row = (await conn.execute(_RUN_END, {"r": eval_run_id})).one()
            if row.status != "running":
                return RunEnd(str(row.status), row.metrics, Decimal(row.cost_usd))
            await sleep(poll_s)
    finally:
        await engine.dispose()


def run_golden_command(args: argparse.Namespace) -> int:
    """`golden seed-adversarial` (module docstring)."""
    cfg = startup_or_exit("worker")
    configure_logging(cfg.log_level, stream=sys.stderr)
    try:
        seeded = asyncio.run(seed(cfg, fixtures_dir(args)))
    except SQLAlchemyError as exc:
        print(f"golden seed-adversarial: database error ({type(exc).__name__})", file=sys.stderr)
        return NOT_STARTED
    except (OSError, ValueError) as exc:  # an unreadable directory or a fixture that is none
        print(f"golden seed-adversarial: invalid fixtures ({type(exc).__name__})", file=sys.stderr)
        return NOT_STARTED
    except golden.FixtureChanged as changed:  # its message names the fixture's key and the field only
        print(f"golden seed-adversarial: {changed}", file=sys.stderr)
        return NOT_STARTED
    print(f"golden seed-adversarial: cases={seeded.cases} jobs={seeded.jobs} profiles={seeded.profiles}")
    return COMPLETED


def run_eval_command(args: argparse.Namespace) -> int:
    """`eval run` (module docstring)."""
    cfg = startup_or_exit("worker")
    configure_logging(cfg.log_level, stream=sys.stderr)
    try:
        candidate = None if args.candidate is None else candidate_ids(args.candidate)
        payload = None if args.candidate_payload is None else read_payload(Path(args.candidate_payload))
    except (OSError, ValueError) as exc:
        print(f"eval run: {exc if isinstance(exc, ValueError) else type(exc).__name__}", file=sys.stderr)
        return NOT_STARTED
    transport = transport_of(args)
    try:
        started = asyncio.run(
            start(
                cfg,
                kind=args.kind,
                route=args.route,
                candidate=candidate,
                payload=payload,
                transport=transport,
                now=utcnow(),
            )
        )
    except CandidateInvalid as invalid:
        fields = ", ".join(f"{issue.field} ({issue.code})" for issue in invalid.issues)
        print(f"eval run: the candidate payload is invalid: {fields}", file=sys.stderr)
        return NOT_STARTED
    except NoCandidate as missing:  # its message names the kind and the id only
        print(f"eval run: no candidate to run: {missing}", file=sys.stderr)
        return NOT_STARTED
    except NoActiveCases as none:
        print(f"eval run: {none.code}: {none}", file=sys.stderr)
        return NOT_STARTED
    except SQLAlchemyError as exc:
        print(f"eval run: database error ({type(exc).__name__})", file=sys.stderr)
        return NOT_STARTED
    print(f"eval run: eval_run={started.eval_run_id} started, waiting for the worker", file=sys.stderr)
    try:
        end = asyncio.run(wait(cfg, started.eval_run_id))
        table = asyncio.run(case_table(cfg, started.eval_run_id))
    except SQLAlchemyError as exc:
        print(f"eval run: database error ({type(exc).__name__})", file=sys.stderr)
        return FAILED
    for line in table:
        print(line)
    print(
        summary(
            eval_run_id=started.eval_run_id, status=end.status, metrics=end.metrics, cost=str(end.cost_usd)
        )
    )
    return COMPLETED if end.status == "completed" else FAILED
