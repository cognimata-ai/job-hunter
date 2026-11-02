"""Spike V4 (spec §22 V4, §10.11; plan Task 7): the schema dry-run's cases, sent through the budgeted
recorder; and its M3 run of prompt-schema version 2, `v4v2` (M3 Task 26, R12).

    uv run python scripts/spikes/record_anthropic.py --spike v4 --dry-run|--live|--commit-recorded
    uv run python scripts/spikes/record_anthropic.py --spike v4v2 --dry-run|--live
    uv run python scripts/spikes/v4_schema_dry_run.py --record-probe-count [--version v1|v2]

`calls()` gives the gating cases `llm.schema_dry_run.cases` makes of prompt-schema version 1 (built offline:
`V1`; M2's version) and the seeded model policy (`settings_v1.json`): the generator schema on Sonnet, then the
critic, profile and queries schemas on Opus. Each is the request the gateway sends for that case
(`schema_dry_run.prompt_for` and `node_for`): the dry-run's static system block and synthetic user turn,
effort `low`, `max_tokens` 2,000, purpose `schema_dry_run`, and the version's stored wire schema, as the
gateway passes it.

`RECORD_EVERY_ANSWER`: the recorder commits each case's live answer, a stream or an error response, as the
fake's answer and goes on to the next case (controller rulings R7-2, R7-3), so the dry-run on the fake replays
the live results. The generator case was answered 400 before this flag existed; `--commit-recorded` writes its
answer from the raw capture, sending nothing.

The informational `minimum` probe is never a paid message (R7-2): `--record-probe-count` sends the dry-run's
probe body (`schema_dry_run.probe_count_body`) to the free `messages.count_tokens` endpoint, through
`record_anthropic.counting_gateway`, and commits the answer (its status and its JSON body without the
`request_id`) where the fake serves it (`fake.count_recording_path`); the raw answer goes to
`.fixtures-raw/anthropic/v4/`.

`check(observation)` is the verdict of one call (field paths, flags and counts, never content): for a stream
the stop reason and the served model, the sent schema byte-equal to the transformed one, a parsed output, and
the paths of any value the output holds that the schema does not allow (a stripped `enum`, `anyOf` or `$ref`);
for an error response its status, type and message (API text). Its `missing` lists the checks that failed.

**v4v2** (M3 Task 26) is a gateway spike of the recorder (`record_anthropic.GATEWAY_SPIKES`): the live
schema dry-run itself, `schema_dry_run.run(..., transport="live")`, of the active prompt-schema version of the
recorder's throwaway database, which is version 2 on a fresh database (its generator schema is candidate C),
on the seeded model policy. `gateway_calls("v4v2")` plans its four cases as the run sends them (`V2`, built
offline: the generator on Sonnet, the critic, profile and queries on Opus; effort `low`, `max_tokens` 2,000);
the informational `minimum` probe goes to the free counting endpoint, never as a message. `run_gateway` runs
it on the session's gateway, so the budgeted transport authorises each HTTP attempt (4 at most), and records:
- the run's `eval_runs` row (`status`, `passed`, `metrics.transport`, which must be `live`) and its case
  table, in the report;
- each case's last answer, committed where the fake serves it (`schema_dry_run/<model>/<schema12>.sse`, an
  error response as V4's was), and the probe's count answer (`count_tokens/<model>/<schema12>.json`).
The spike ends `ok` only when the row passed with `metrics.transport = "live"`; a generator rejection is a
stop (a definitive 4xx stops every later attempt, and the run records the rest `http_400`, unsent).
"""

import argparse
import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any, Final

import anthropic
from jobhunter.cli_schema_dry_run import case_table
from jobhunter.clock import utcnow
from jobhunter.db.seed import load_settings_v1
from jobhunter.llm import fake, schema_dry_run
from jobhunter.llm.prompts import render
from jobhunter.llm.schemas import SCHEMAS_V1, transformed
from jobhunter.types import get_object

RECORD_EVERY_ANSWER: Final = True
HERE: Final = Path(__file__).resolve().parent
REPO: Final = HERE.parents[1]
KEY_VAR: Final = "JOBHUNTER_ANTHROPIC_API_KEY"


def _policy() -> dict[str, Any]:
    return get_object(load_settings_v1(), "scoring.model_policy")


def _v1() -> render.PromptVersion:
    """Prompt-schema version 1 as the M2 seed stored it (the id is never sent)."""
    templates, schemas = render.prompt_schema_v1()
    return render.PromptVersion(
        id=1,
        prompt_schema_hash=render.prompt_schema_hash(templates, schemas),
        templates={name: str(text) for name, text in templates.items()},
        schemas=schemas,
        parser_family="generator_v1",
    )


V1: Final = _v1()


def _v2() -> render.PromptVersion:
    """Prompt-schema version 2 as the M3 seed stores it (built offline; the id is never sent): candidate C.
    `V2` is the one copy of it the spikes use: `v5_stream_timing` (and through it `v12_batches`) loads it
    from here."""
    templates, schemas = render.prompt_schema_v2()
    return render.PromptVersion(
        id=2,
        prompt_schema_hash=render.prompt_schema_hash(templates, schemas),
        templates={name: str(text) for name, text in templates.items()},
        schemas=schemas,
        parser_family="generator_v2_items",
    )


V2: Final = _v2()
VERSIONS: Final = {"v1": V1, "v2": V2}
GATEWAY_SPIKE: Final = "v4v2"
_COUNT_TOKENS_PATH: Final = "/v1/messages/count_tokens"


def _file_name(label: str) -> str:
    return label.replace("/", "-").replace(":", "-")


def calls() -> list[dict[str, Any]]:
    planned: list[dict[str, Any]] = []
    for case in schema_dry_run.cases(_policy()):
        if case.probe:  # answered by the free counting endpoint, never as a message (R7-2)
            continue
        prompt = schema_dry_run.prompt_for(case)
        node = schema_dry_run.node_for(case, V1)
        (message,) = prompt.messages
        planned.append(
            {
                "case": _file_name(case.label),
                "purpose": node.purpose,
                "model": node.model,
                "effort": node.effort,
                "max_tokens": node.max_tokens,
                "system": [block.text for block in prompt.system],
                "user": message.content,
                "schema": dict(node.schema.json),  # the stored wire schema, as the gateway passes it
            }
        )
    return planned


# ---- v4v2: the live schema dry-run of prompt-schema version 2 (M3 Task 26) -------------------------------


def gateway_calls(spike: str) -> list[dict[str, Any]]:
    """The four gating cases of `schema_dry_run.cases` for version 2 on the seeded policy, as the run sends
    them: `node_for(case, V2)` and `prompt_for(case)`."""
    if spike != GATEWAY_SPIKE:
        raise ValueError(f"no gateway spike {spike!r} here")
    return [
        {
            "case": _file_name(case.label),
            "node": schema_dry_run.node_for(case, V2),
            "prompt": schema_dry_run.prompt_for(case),
        }
        for case in schema_dry_run.cases(_policy())
        if not case.probe
    ]


def dry_run_notes(spike: str) -> list[str]:
    probe = next(case for case in schema_dry_run.cases(_policy()) if case.probe)
    return [f"{probe.label} goes to the free counting endpoint (count_tokens): no message, nothing reserved"]


_EVAL_RUN: Final = "SELECT status, passed, metrics, cost_usd FROM eval_runs WHERE id = %(id)s"


async def run_gateway(spike: str, session: Any) -> dict[str, Any]:
    """The live dry-run of the throwaway database's active version (version 2) on the session's gateway; its
    report holds codes, flags, ids and amounts only (module docstring)."""
    if spike != GATEWAY_SPIKE:
        raise ValueError(f"no gateway spike {spike!r} here")
    ids = await session.active_ids()
    async with session.engine.connect() as conn:
        version = await render.load_prompt_version(conn, ids["prompt_schema_version_id"])
    if version.prompt_schema_hash != V2.prompt_schema_hash:
        return {
            "ok": False,
            "lines": ["STOP: the active version is not prompt-schema version 2; nothing sent"],
        }
    report = await schema_dry_run.run(
        session.engine,
        session.gateway,
        prompt_schema_version_id=ids["prompt_schema_version_id"],
        model_policy_id=ids["model_policy_id"],
        execution_config_id=ids["execution_config_id"],
        transport="live",
        now=utcnow(),
    )
    async with session.engine.connect() as conn:
        row = (await conn.exec_driver_sql(_EVAL_RUN, {"id": report.eval_run_id})).one()
    committed = []
    for call in gateway_calls(spike):
        exchanges = session.exchanges(call["case"])
        if exchanges and (path := session.commit_answer(exchanges[-1])) is not None:
            committed.append(str(path.relative_to(session.recorder.fixtures_out)))
    for exchange in session.exchanges():
        if exchange.path == _COUNT_TOKENS_PATH and (path := session.commit_count(exchange)) is not None:
            committed.append(str(path.relative_to(session.recorder.fixtures_out)))
    transport = (row.metrics or {}).get("transport")
    lines = [
        *case_table(report, transport="live"),
        f"eval_runs {report.eval_run_id}: status={row.status} passed={row.passed} "
        f"metrics.transport={transport}",
        *(f"committed {path}" for path in committed),
    ]
    return {
        "ok": row.passed is True and transport == "live",
        "eval_run_id": report.eval_run_id,
        "status": row.status,
        "passed": row.passed,
        "metrics_transport": transport,
        "cost_usd": str(report.cost_usd),
        "cases": [
            {
                "case": r.case.label,
                "outcome": r.outcome,
                "passed": r.passed,
                "http_status": r.http_status,
                "checks": dict(r.checks),
                "violations": list(r.violations),
            }
            for r in report.results
        ],
        "committed": committed,
        "lines": lines,
    }


def _canonical(schema: Any) -> str:
    return fake.canonical_schema_json(schema)


def _schema_name(sent: Any) -> str | None:
    """Which of `SCHEMAS_V1` a request sent."""
    return next((n for n, m in SCHEMAS_V1.items() if _canonical(sent) == _canonical(transformed(m))), None)


def check(observation: dict[str, Any]) -> dict[str, Any]:
    body = observation["request"]
    sent = body.get("output_config", {}).get("format", {}).get("schema")
    name = _schema_name(sent)
    if "events" not in observation:  # an error response
        return {
            "missing": ["http_200"],
            "case": name,
            "schema_bytes_equal": name is not None,
            "http_status": observation["status"],
            "error_type": observation["error_type"],
            "error_message": observation["error_message"],
        }
    meta = observation["response_metadata"]
    events = [fake.SSEEvent(e["name"], e["data"]) for e in observation["events"]]
    try:
        output = json.loads(fake.text_of(events))
    except ValueError:
        output = None
    violations = schema_dry_run.schema_violations(transformed(SCHEMAS_V1[name]), output) if name else ["$"]
    checks = {
        "end_turn": meta.get("stop_reason") == "end_turn",
        "model_served": meta.get("model_name") == observation["requested_model"],
        "schema_bytes_equal": name is not None,
        "parsed": bool(observation["parsed_ok"]) and output is not None,
        "enums_allowed": not violations,
    }
    usage = observation["usage_metadata"]
    return {
        "missing": [check for check, ok in checks.items() if not ok],
        "case": name,
        "stop_reason": meta.get("stop_reason"),
        "checks": checks,
        "violations": violations,
        "usage": {key: usage.get(key) for key in ("input_tokens", "output_tokens")},
    }


# ---- the probe on the counting endpoint (free) -------------------------------------------------------------


def _recorder() -> Any:
    spec = importlib.util.spec_from_file_location("jobhunter_spike_recorder", HERE / "record_anthropic.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("the recorder is missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def record_probe_count(key: str, *, fixtures: Path, raw: Path, version: str = "v1") -> str:
    """Send the probe's count body of `version` (`v1` or `v2`) to `messages.count_tokens` and commit its
    answer for the fake; the line printed names the status and the API's message only."""
    recorder = _recorder()
    case = next(c for c in schema_dry_run.cases(_policy()) if c.probe)
    async with recorder.counting_gateway(key) as gateway:
        body = schema_dry_run.probe_count_body(gateway, case, VERSIONS[version])
        try:
            counted = await gateway.count_tokens(case.model, body)
            status, answer = 200, {"input_tokens": counted}
        except anthropic.APIStatusError as exc:
            status, answer = exc.status_code, exc.body if isinstance(exc.body, dict) else {}
    answer = {k: v for k, v in answer.items() if k != "request_id"}
    recorded = {"status": status, "body": answer}
    text = json.dumps(recorded, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if key and key in text:
        raise RuntimeError("the count answer holds the key; nothing written")
    path = fake.count_recording_path(fixtures, case.model, body["output_config"]["format"]["schema"])
    for target in (path, raw / f"{_file_name(case.label)}.count.json"):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, "utf-8")
    message = answer.get("error", {}).get("message") if isinstance(answer.get("error"), dict) else None
    return f"{case.label}: count_tokens {status} {message or answer} -> {path.relative_to(REPO)}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="v4_schema_dry_run.py", description=(__doc__ or "").split("\n\n")[0]
    )
    parser.add_argument("--record-probe-count", action="store_true", required=True)
    parser.add_argument("--version", choices=sorted(VERSIONS), default="v1")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    key = os.environ.get(KEY_VAR, "").strip()
    if not key:
        print(f"v4: REFUSED, {KEY_VAR} is not set; nothing sent")
        return 4
    try:
        line = asyncio.run(
            record_probe_count(
                key,
                fixtures=REPO / "tests/contract/fixtures/anthropic",
                raw=REPO / ".fixtures-raw/anthropic/v4",
                version=args.version,
            )
        )
    except Exception as exc:  # noqa: BLE001 - the class only: an SDK error holds its request
        print(f"v4: {type(exc).__name__}")
        return 1
    print(line)
    return 0


if __name__ == "__main__":
    # As the recorder: no ANTHROPIC_* variable may steer the request (G1).
    for name in [name for name in os.environ if name.startswith("ANTHROPIC_")]:
        del os.environ[name]
    raise SystemExit(main())
