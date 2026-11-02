"""The `schema-dry-run [--live] [--version <id>] [--model-policy <id> | --candidate-payload <settings.json>]`
command (spec §10.11; M2 plan Task 7, M3 Tasks 4 and 20).

`cli.main` imports this module only when the command runs. It starts like the worker (the worker-role config
and its startup assertions), reads the active prompt-schema version, model policy and execution config from
`active_config`, and runs `llm.schema_dry_run.run` on a gateway of its own, for the prompt-schema version
`--version` names (by default the active one), so an upgraded install can dry-run version 2 before it is
active, and for the model policy of a candidate (Codex r2 B21, r3 B7, M3 Task 20): `--model-policy` names an
existing row (`eval_runner.NoCandidate` when there is none), and `--candidate-payload` a settings document
that need not be saved: it is validated as the settings dry-run validates it (`eval_runner.CandidateInvalid`)
and its derived rows, the policy among them, are found or inserted by hash in a short transaction under
`active_config FOR UPDATE` (`eval_runner.stage_candidate`: no settings version, no activation). The save
refuses an `llm_tuple` change whose policy is not verified (`schema_dry_run_required`) and creates that row
only after the check, so a refused save is unblocked by running this on the payload it was refused for: the
save finds the same row by hash. Nothing is sent before the candidate resolves. Without either flag the run is
of the active policy:
- without `--live` on the recorded-SSE fake (`JOBHUNTER_FAKE_LLM_DIR`, by default the committed recordings),
  which prod refuses, as it refuses `JOBHUNTER_FAKE_LLM`;
- with `--live` on the Anthropic API, which needs the worker's `anthropic_key` secret (not blank) and refuses
  `JOBHUNTER_FAKE_LLM`.

Either way the run records its transport (`fake` or `live`) as `metrics.transport` in its `eval_runs` row.
Its stdout is the case table only (schema, model, result) and one summary line; the logs go to stderr. Neither
ever carries content: no prompt, no output, no key. The exit status is 0 when the run passed, 1 when it did
not or failed, and 2 when it could not start (a refusal above, an unreadable or invalid candidate payload, or
an unknown model policy).
"""

import argparse
import asyncio
import dataclasses
import sys
from pathlib import Path
from typing import Final

from sqlalchemy.exc import SQLAlchemyError

from jobhunter.cli_golden import read_payload
from jobhunter.clock import utcnow
from jobhunter.config import AppConfig, startup_or_exit
from jobhunter.db.session import make_engine
from jobhunter.db.sql import q
from jobhunter.llm import schema_dry_run
from jobhunter.llm.cache_monitor import CacheMonitor
from jobhunter.llm.gateway import LLMGateway, has_api_key
from jobhunter.llm.schema_dry_run import DryRunReport
from jobhunter.llm.transport import make_http_client
from jobhunter.observability.logging import configure_logging
from jobhunter.scoring.eval_runner import CandidateInvalid, NoCandidate, stage_candidate
from jobhunter.types import JSONValue

PASSED, NOT_PASSED, NOT_STARTED = 0, 1, 2

_ACTIVE: Final = q(
    "SELECT prompt_schema_version_id, model_policy_id, execution_config_id FROM active_config WHERE id = 1"
)
_MODEL_POLICY: Final = q("SELECT EXISTS (SELECT 1 FROM model_policies WHERE id = :id)")


def start_refusal(cfg: AppConfig, *, live: bool) -> str | None:
    """Why the dry-run must not start with `cfg`, or `None`."""
    if live and cfg.fake_llm:
        return "--live sends to the Anthropic API: unset JOBHUNTER_FAKE_LLM"
    if live and not has_api_key(cfg):  # a blank key too: the gateway would send nothing (Task 23)
        return "--live needs the worker's anthropic_key secret"
    if not live and cfg.env == "prod":
        return "without --live the dry-run runs on the fake transport, which prod refuses"
    return None


def case_table(report: DryRunReport, *, transport: str) -> list[str]:
    """The printed table: one row per case, then the run's summary line; codes and ids only."""
    rows = [("schema", "model", "result")]
    for result in report.results:
        if result.case.probe:
            status = "" if result.http_status is None else f", http {result.http_status}"
            rows.append(
                (
                    f"probe:{schema_dry_run.PROBE_KEYWORD}",
                    result.case.model,
                    f"{result.outcome} (count_tokens{status}, informational)",
                )
            )
        else:
            rows.append((result.case.schema_name, result.case.model, result.outcome))
    widths = [max(len(row[i]) for row in rows) for i in range(2)]
    lines = [f"{row[0]:<{widths[0]}}  {row[1]:<{widths[1]}}  {row[2]}" for row in rows]
    lines.append(
        f"eval_run={report.eval_run_id} status={report.status} passed={str(report.passed).lower()} "
        f"cost_usd={report.cost_usd} transport={transport}"
    )
    return lines


async def _dry_run(
    cfg: AppConfig,
    *,
    transport: schema_dry_run.Transport,
    version_id: int | None = None,
    model_policy_id: int | None = None,
    payload: JSONValue | None = None,
) -> DryRunReport:
    """The run on `cfg`'s client, recorded with `transport`, which must be the one `cfg` sends to, of the
    prompt-schema version `version_id` (the active one when `None`) and of the model policy `model_policy_id`,
    or of `payload`'s, staged first (module docstring), or else the active one. `NoCandidate` for a policy id
    with no row and `CandidateInvalid` for a payload the settings dry-run refuses, before anything is sent."""
    if (transport == "fake") != cfg.fake_llm:
        raise ValueError(f"transport {transport!r} does not match the config's fake_llm={cfg.fake_llm}")
    engine = make_engine(cfg, pool_size=2)
    try:
        if payload is not None:
            async with engine.begin() as conn:
                model_policy_id = (await stage_candidate(conn, payload, now=utcnow())).model_policy_id
        async with engine.connect() as conn:
            ids = (await conn.execute(_ACTIVE)).one()
            if model_policy_id is None:
                model_policy_id = int(ids.model_policy_id)
            elif not (await conn.execute(_MODEL_POLICY, {"id": model_policy_id})).scalar_one():
                raise NoCandidate(f"model policy {model_policy_id} does not exist")
        async with make_http_client(cfg) as client:
            gateway = LLMGateway(engine, cfg, http_client=client, cache=CacheMonitor())
            return await schema_dry_run.run(
                engine,
                gateway,
                prompt_schema_version_id=int(ids.prompt_schema_version_id)
                if version_id is None
                else version_id,
                model_policy_id=model_policy_id,
                execution_config_id=int(ids.execution_config_id),
                transport=transport,
                now=utcnow(),
            )
    finally:
        await engine.dispose()


def run_schema_dry_run(args: argparse.Namespace) -> int:
    """`schema-dry-run [--live] [--version <id>] [--model-policy <id> | --candidate-payload <file>]`; the exit
    status is the outcome (module docstring). An error prints its class only: SQLAlchemy puts bound
    parameters into its messages, and an SDK error holds its request. A version that does not exist ends it
    with a `ValueError` before anything is sent."""
    cfg = startup_or_exit("worker")
    configure_logging(cfg.log_level, stream=sys.stderr)  # stdout carries the table only
    live = bool(args.live)
    if (why := start_refusal(cfg, live=live)) is not None:
        print(f"schema-dry-run: {why}; nothing sent", file=sys.stderr)
        return NOT_STARTED
    try:
        payload = None if args.candidate_payload is None else read_payload(Path(args.candidate_payload))
    except (OSError, ValueError) as exc:
        print(
            f"schema-dry-run: {exc if isinstance(exc, ValueError) else type(exc).__name__}; nothing sent",
            file=sys.stderr,
        )
        return NOT_STARTED
    transport: schema_dry_run.Transport = "live" if live else "fake"
    if not live:
        cfg = dataclasses.replace(cfg, fake_llm=True)
    run = _dry_run(
        cfg, transport=transport, version_id=args.version, model_policy_id=args.model_policy, payload=payload
    )
    try:
        report = asyncio.run(run)
    except CandidateInvalid as invalid:
        fields = ", ".join(f"{issue.field} ({issue.code})" for issue in invalid.issues)
        print(f"schema-dry-run: the candidate payload is invalid: {fields}; nothing sent", file=sys.stderr)
        return NOT_STARTED
    except NoCandidate as missing:  # its message names the kind and the id only
        print(f"schema-dry-run: {missing}; nothing sent", file=sys.stderr)
        return NOT_STARTED
    except SQLAlchemyError as exc:
        print(f"schema-dry-run: database error ({type(exc).__name__})", file=sys.stderr)
        return NOT_PASSED
    except Exception as exc:  # noqa: BLE001 - the class only: the run's own row records what it reached
        print(f"schema-dry-run: failed ({type(exc).__name__})", file=sys.stderr)
        return NOT_PASSED
    for line in case_table(report, transport=transport):
        print(line)
    return PASSED if report.passed else NOT_PASSED
